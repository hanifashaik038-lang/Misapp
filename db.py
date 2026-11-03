"""
Database access layer.

SQLite is used for the demonstration. The schema in ``schema.py`` is written in
portable ANSI SQL (no SQLite-only types) so that the same DDL runs on
PostgreSQL with only the driver/URL changing.

Usage
-----
    from database.db import query, execute, transaction, get_engine

    df = query("SELECT * FROM stores")
    execute("UPDATE alerts SET status = :s WHERE alert_id = :a", {"s": "RESOLVED", "a": 12})
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from config import DB_PATH

log = logging.getLogger("stocksense.db")

_engine: Engine | None = None


def get_engine(echo: bool = False) -> Engine:
    """Return a process-wide SQLAlchemy engine (SQLite by default)."""
    global _engine
    if _engine is None:
        url = f"sqlite:///{DB_PATH}"
        _engine = create_engine(
            url,
            echo=echo,
            future=True,
            connect_args={"timeout": 30, "check_same_thread": False},
        )

        @event.listens_for(_engine, "connect")
        def _set_pragma(dbapi_con, _con_record):  # pragma: no cover - infra glue
            cur = dbapi_con.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return _engine


@contextlib.contextmanager
def transaction():
    """Atomic unit of work. Rolls back on any exception."""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


@contextlib.contextmanager
def write_txn():
    """Atomic write transaction yielding a raw DBAPI cursor.

    Service functions call ``cur.execute(...)`` / ``cur.fetchone()`` /
    ``cur.lastrowid`` directly. The transaction commits only if the body
    finishes without raising; any exception rolls the whole unit of work back
    (a sale can therefore never deduct stock without writing its receipt).
    """
    conn = get_engine().raw_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass


def query(sql: str, params: Mapping[str, Any] | None = None) -> pd.DataFrame:
    """Run a read-only query and return a DataFrame."""
    engine = get_engine()
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params=dict(params or {}))


def query_one(sql: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    df = query(sql, params)
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def scalar(sql: str, params: Mapping[str, Any] | None = None, default: Any = None) -> Any:
    df = query(sql, params)
    if df.empty or pd.isna(df.iloc[0, 0]):
        return default
    return df.iloc[0, 0]


def execute(sql: str, params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None) -> int:
    """Run a write statement (or executemany when a list is supplied)."""
    engine = get_engine()
    with engine.begin() as conn:
        raw = conn.connection.driver_connection  # underlying sqlite3 connection
        cur = raw.cursor()
        if isinstance(params, (list, tuple)):
            cur.executemany(sql, params)
        else:
            cur.execute(sql, dict(params or {}))
        raw.commit()
        rowcount = cur.rowcount
        cur.close()
    return rowcount


def executemany(sql: str, rows: Iterable[Sequence[Any]]) -> int:
    """Bulk insert helper – much faster than repeated ``execute`` calls."""
    rows = list(rows)
    if not rows:
        return 0
    engine = get_engine()
    with engine.begin() as conn:
        raw = conn.connection.driver_connection
        cur = raw.cursor()
        cur.executemany(sql, rows)
        raw.commit()
        cur.close()
    return len(rows)


def db_ready() -> bool:
    """True when the database exists and the core tables are populated."""
    if not Path(DB_PATH).exists():
        return False
    try:
        n = scalar("SELECT COUNT(*) FROM stores", default=0)
        return bool(n)
    except Exception:
        return False


def table_counts() -> pd.DataFrame:
    """Row counts for every user table – used by the System Settings page."""
    names = [
        "roles", "users", "stores", "product_categories", "suppliers", "sku",
        "purchase_orders", "purchase_order_items", "goods_receipts",
        "goods_receipt_items", "stock_event", "stock_on_hand",
        "sales_transactions", "sales_transaction_items", "sales_daily",
        "supplier_performance", "stockout_event", "stockout_prediction",
        "store_kpi_daily", "alerts", "synchronization_logs", "audit_logs",
        "ml_model_registry",
    ]
    out = []
    for t in names:
        try:
            out.append({"table": t, "rows": int(scalar(f"SELECT COUNT(*) FROM {t}", default=0))})
        except Exception:
            out.append({"table": t, "rows": -1})
    return pd.DataFrame(out)


def pd_read_sqlite(sql: str, params: Sequence[Any] | None = None) -> pd.DataFrame:
    """Escape hatch for raw sqlite3 access (kept for offline sync simulation)."""
    con = sqlite3.connect(DB_PATH)
    try:
        return pd.read_sql_query(sql, con, params=params or [])
    finally:
        con.close()
