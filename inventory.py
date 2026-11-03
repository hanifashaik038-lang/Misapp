"""
Inventory service: stock visibility, append-only movements, adjustments.

Stock is never "set" directly. Every change is a row in ``stock_event`` with a
signed ``quantity_delta``; ``stock_on_hand`` is a cache that can be rebuilt at
any time from the ledger.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from database.db import execute, query, scalar, write_txn
from modules import audit
from utils.calculations import STATUS_COLORS, stock_status
from utils.helpers import get_logger, new_event_id
from utils.validators import ValidationError, validate_quantity

log = get_logger("stocksense.inventory")


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def inventory_view(store_ids: list[int] | None = None, search: str = "",
                   category: str | None = None, status: str | None = None) -> pd.DataFrame:
    sql = ["SELECT * FROM v_inventory_enriched WHERE 1=1"]
    p: dict = {}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND store_code IN (" + ",".join(f"'{s}'" for s in store_ids) + ")")
    if search:
        sql.append("AND (LOWER(product_name) LIKE :q OR barcode LIKE :q OR LOWER(sku_code) LIKE :q)")
        p["q"] = f"%{search.strip().lower()}%"
    if category:
        sql.append("AND category_name = :cat")
        p["cat"] = category
    if status:
        sql.append("AND stock_status = :st")
        p["st"] = status
    sql.append("ORDER BY on_hand_qty ASC")
    return query(" ".join(sql), p)


def store_ids_for_codes(codes: list[str]) -> list[int]:
    if not codes:
        return []
    rows = query("SELECT store_id FROM stores WHERE store_code IN ("
                 + ",".join(f"'{c}'" for c in codes) + ")")
    return [int(x) for x in rows["store_id"].tolist()]


def get_stock(store_id: int, sku_id: int) -> dict | None:
    df = query(
        "SELECT soh.on_hand_qty, soh.reserved_qty, soh.in_transit_qty, k.reorder_level, "
        "k.safety_stock, k.product_name, k.sku_code, k.barcode, k.selling_price, k.unit_cost, "
        "k.max_stock_level FROM stock_on_hand soh JOIN sku k ON k.sku_id = soh.sku_id "
        "WHERE soh.store_id = :s AND soh.sku_id = :k", {"s": store_id, "k": sku_id})
    return None if df.empty else df.iloc[0].to_dict()


def find_by_barcode(barcode: str) -> dict | None:
    df = query("SELECT k.sku_id, k.sku_code, k.barcode, k.product_name, k.selling_price, "
               "k.unit_cost, k.reorder_level, k.safety_stock, k.max_stock_level, "
               "c.category_name FROM sku k JOIN product_categories c ON c.category_id = k.category_id "
               "WHERE k.barcode = :b", {"b": str(barcode).strip()})
    return None if df.empty else df.iloc[0].to_dict()


def movements(store_id: int, sku_id: int | None = None, limit: int = 200) -> pd.DataFrame:
    sql = ("SELECT event_seq, event_id, occurred_at, event_type, quantity_delta, reason_code, "
           "reference_table, reference_id, device_id, is_replayed, note, k.sku_code, "
           "k.product_name FROM stock_event e JOIN sku k ON k.sku_id = e.sku_id "
           "WHERE e.store_id = :s")
    p: dict = {"s": store_id, "l": limit}
    if sku_id:
        sql += " AND e.sku_id = :k"
        p["k"] = sku_id
    sql += " ORDER BY e.event_seq DESC LIMIT :l"
    return query(sql, p)


def ledger_balance(store_id: int, sku_id: int) -> int:
    return int(scalar("SELECT COALESCE(SUM(quantity_delta),0) FROM stock_event "
                      "WHERE store_id = :s AND sku_id = :k", {"s": store_id, "k": sku_id}, 0))


def available(store_id: int, sku_id: int) -> int:
    """Sellable quantity = on hand - reserved."""
    row = get_stock(store_id, sku_id)
    if not row:
        return 0
    return int(row["on_hand_qty"]) - int(row["reserved_qty"] or 0)


def categories() -> list[str]:
    return query("SELECT category_name FROM product_categories ORDER BY category_name")[
        "category_name"].tolist()


def stock_status_counts(store_ids: list[int] | None = None) -> pd.DataFrame:
    inv = inventory_view(store_ids)
    if inv.empty:
        return pd.DataFrame(columns=["stock_status", "skus", "units", "value"])
    g = inv.groupby("stock_status").agg(
        skus=("sku_code", "count"), units=("on_hand_qty", "sum"), value=("stock_value", "sum")
    ).reset_index()
    g["color"] = g["stock_status"].map(STATUS_COLORS)
    order = {"Out of Stock": 0, "Critical Stock": 1, "Low Stock": 2, "In Stock": 3}
    return g.sort_values("stock_status", key=lambda s: s.map(order))


def inventory_valuation(store_ids: list[int] | None = None) -> dict:
    inv = inventory_view(store_ids)
    if inv.empty:
        return {"units": 0, "cost_value": 0.0, "retail_value": 0.0, "skus": 0}
    return {
        "units": int(inv["on_hand_qty"].sum()),
        "cost_value": round(float((inv["on_hand_qty"] * inv["unit_cost"]).sum()), 2),
        "retail_value": round(float((inv["on_hand_qty"] * inv["selling_price"]).sum()), 2),
        "skus": int(inv["sku_code"].nunique()),
    }


# --------------------------------------------------------------------------
# Writes – all append-only
# --------------------------------------------------------------------------
def _apply_delta(cur, store_id: int, sku_id: int, delta: int, occurred_at: str) -> None:
    cur.execute(
        "INSERT INTO stock_on_hand (store_id, sku_id, on_hand_qty, last_event_at, updated_at) "
        "VALUES (?,?,?,?,datetime('now')) "
        "ON CONFLICT(store_id, sku_id) DO UPDATE SET "
        "on_hand_qty = on_hand_qty + excluded.on_hand_qty, "
        "last_event_at = excluded.last_event_at, updated_at = datetime('now')",
        (store_id, sku_id, delta, occurred_at),
    )


def record_event(store_id: int, sku_id: int, event_type: str, delta: int,
                 *, reason_code: str | None = None, reference_table: str | None = None,
                 reference_id: str | None = None, operator_id: int | None = None,
                 device_id: str | None = None, note: str | None = None,
                 occurred_at: str | None = None, event_id: str | None = None,
                 cur=None) -> str:
    """Append one immutable stock event and update the derived cache."""
    occurred_at = occurred_at or datetime.now().isoformat(timespec="seconds")
    eid = event_id or new_event_id()
    values = (eid, store_id, sku_id, event_type, int(delta), reference_table, reference_id,
              reason_code, occurred_at, datetime.utcnow().isoformat(), device_id,
              operator_id, None, 0, note)
    sql = ("INSERT INTO stock_event (event_id, store_id, sku_id, event_type, quantity_delta, "
           "reference_table, reference_id, reason_code, occurred_at, recorded_at, device_id, "
           "operator_id, sync_batch_id, is_replayed, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    if cur is not None:
        cur.execute(sql, values)
        _apply_delta(cur, store_id, sku_id, int(delta), occurred_at)
    else:
        with write_txn() as c:
            c.execute(sql, values)
            _apply_delta(c, store_id, sku_id, int(delta), occurred_at)
    return eid


def adjust_stock(store_id: int, sku_id: int, qty: int, direction: str,
                 reason: str, user: dict | None = None, note: str = "") -> str:
    """Positive or negative manual adjustment (damage, count correction, write-off)."""
    qty = validate_quantity(qty, "Adjustment quantity")
    if direction not in {"UP", "DOWN"}:
        raise ValidationError("Direction must be UP or DOWN.")
    if direction == "DOWN" and available(store_id, sku_id) < qty:
        raise ValidationError(
            f"Cannot remove {qty} units – only {available(store_id, sku_id)} available.")
    delta = qty if direction == "UP" else -qty
    etype = "ADJUSTMENT_POS" if direction == "UP" else (
        "DAMAGE" if reason.upper() == "EXPIRY_WASTE" else "ADJUSTMENT_NEG")
    eid = record_event(store_id, sku_id, etype, delta, reason_code=reason,
                       operator_id=(user or {}).get("user_id"),
                       note=note or f"Manual {direction.lower()} adjustment",
                       device_id="WEB-UI")
    audit.record("STOCK_ADJUSTMENT", user=user, entity="stock_event", entity_id=eid,
                 store_id=store_id,
                 detail=f"sku={sku_id} {direction} {qty} reason={reason}")
    return eid


def recompute_stock_on_hand(store_id: int | None = None) -> int:
    """Rebuild the cache from the ledger – the reconciliation proof."""
    where = "1=1"
    if store_id:
        where = f"store_id = {int(store_id)}"
    with write_txn() as cur:
        cur.execute(
            f"UPDATE stock_on_hand SET on_hand_qty = COALESCE(("
            f"SELECT SUM(quantity_delta) FROM stock_event e "
            f"WHERE e.store_id = stock_on_hand.store_id AND e.sku_id = stock_on_hand.sku_id), 0) "
            f"WHERE EXISTS (SELECT 1 FROM stock_event e2 WHERE e2.store_id = stock_on_hand.store_id "
            f"AND e2.sku_id = stock_on_hand.sku_id) AND {where}")
        n = cur.rowcount
    audit.record("STOCK_RECOMPUTED", entity="stock_on_hand", detail=f"{n} rows rebuilt")
    return int(n)


def reconcile(store_id: int | None = None) -> pd.DataFrame:
    """Compare the derived cache against the ledger sum; empty result = perfect."""
    where = "WHERE 1=1" + (f" AND soh.store_id = {int(store_id)}" if store_id else "")
    return query(f"""
        SELECT s.store_code, k.sku_code, k.product_name, soh.on_hand_qty AS cached_qty,
               COALESCE(v.ledger_qty, 0) AS ledger_qty,
               soh.on_hand_qty - COALESCE(v.ledger_qty, 0) AS variance
        FROM stock_on_hand soh
        JOIN stores s ON s.store_id = soh.store_id
        JOIN sku k ON k.sku_id = soh.sku_id
        LEFT JOIN v_ledger_stock v ON v.store_id = soh.store_id AND v.sku_id = soh.sku_id
        {where}
        AND soh.on_hand_qty <> COALESCE(v.ledger_qty, 0)
        ORDER BY ABS(soh.on_hand_qty - COALESCE(v.ledger_qty, 0)) DESC
    """)


def refresh_in_transit() -> int:
    """Recompute in-transit quantities from open purchase orders."""
    with write_txn() as cur:
        cur.execute("UPDATE stock_on_hand SET in_transit_qty = COALESCE(("
                    "SELECT SUM(poi.ordered_qty - poi.received_qty) FROM purchase_order_items poi "
                    "JOIN purchase_orders po ON po.po_id = poi.po_id "
                    "WHERE po.store_id = stock_on_hand.store_id AND poi.sku_id = stock_on_hand.sku_id "
                    "AND po.status IN ('SUBMITTED','ACKNOWLEDGED','PARTIALLY_RECEIVED')), 0)")
    return 0
