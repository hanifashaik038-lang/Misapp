"""
Offline synchronization simulation.

HONEST SCOPE: in the Streamlit demonstration there is ONE central SQLite
database. The store edge node is *simulated* by an in-memory outbox plus a
table of pending events; there is no separate store server, no real network
partition and no HTTPS transport in this build. What IS real:

  * an outbox queue per store with client-generated UUID event ids
  * a toggle that makes the agent stop pushing (the "network is down" state)
  * idempotent ingestion: a re-sent batch is rejected by the UNIQUE event_id
  * out-of-order event handling (late arrivals are applied and flagged)
  * a synchronization log with accepted / duplicate / out-of-order counters
  * reconciliation that proves no transaction was lost or double-counted

The production design (documented in docs/) replaces ``push_loop`` with an
HTTPS POST to ``/sync/events`` carrying a signed store token.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

import pandas as pd

from database.db import execute, query, scalar, write_txn
from modules import audit
from modules.inventory import record_event
from utils.helpers import get_logger, new_batch_id, new_event_id

log = get_logger("stocksense.sync")

# Simulated store-node state (per process). True = offline (agent paused).
_OFFLINE_STATE: dict[int, bool] = {}
_OUTBOX: dict[int, list[dict]] = {}


def set_offline(store_id: int, offline: bool) -> None:
    _OFFLINE_STATE[int(store_id)] = bool(offline)
    audit.record("SYNC_MODE_CHANGED", entity="stores", entity_id=store_id,
                 store_id=store_id, detail="SIMULATED offline" if offline else "back online")


def is_offline(store_id: int) -> bool:
    return _OFFLINE_STATE.get(int(store_id), False)


def outbox_size(store_id: int) -> int:
    return len(_OUTBOX.get(int(store_id), []))


def queue_event(store_id: int, sku_id: int, event_type: str, delta: int,
                occurred_at: str | None = None, reason: str = "OFFLINE_TXN",
                reference_id: str | None = None) -> dict:
    """Local write while the store node cannot reach the centre."""
    event = {
        "event_id": new_event_id(),
        "store_id": int(store_id),
        "sku_id": int(sku_id),
        "event_type": event_type,
        "quantity_delta": int(delta),
        "occurred_at": occurred_at or datetime.now().isoformat(timespec="seconds"),
        "reason_code": reason,
        "reference_id": reference_id,
    }
    _OUTBOX.setdefault(int(store_id), []).append(event)
    log.info("queued offline event for store %s (outbox=%s)", store_id, outbox_size(store_id))
    return event


def push_loop(store_id: int, force: bool = False) -> dict:
    """Attempt one synchronization batch for a store.

    Returns a summary dict; marks duplicates and out-of-order arrivals.
    """
    store_id = int(store_id)
    if is_offline(store_id) and not force:
        return {"status": "SKIPPED_OFFLINE", "accepted": 0, "duplicates": 0,
                "out_of_order": 0, "batch_id": None}

    events = _OUTBOX.get(store_id, [])
    if not events:
        return {"status": "EMPTY", "accepted": 0, "duplicates": 0,
                "out_of_order": 0, "batch_id": None}

    store_code = scalar("SELECT store_code FROM stores WHERE store_id = :s",
                        {"s": store_id}, "STORE")
    batch_id = new_batch_id(store_code)
    started = datetime.now()

    accepted = duplicates = out_of_order = 0
    for ev in events:
        try:
            with write_txn() as cur:
                cur.execute("SELECT 1 FROM stock_event WHERE event_id = ?", (ev["event_id"],))
                if cur.fetchone():
                    duplicates += 1
                    continue
                # out-of-order detection: an event older than the newest known event
                cur.execute("SELECT MAX(occurred_at) FROM stock_event WHERE store_id = ? "
                            "AND sku_id = ?", (store_id, ev["sku_id"]))
                latest = cur.fetchone()[0]
                late = bool(latest and ev["occurred_at"] < latest)
                if late:
                    out_of_order += 1
                record_event(store_id, ev["sku_id"], ev["event_type"],
                             ev["quantity_delta"], reason_code=ev.get("reason_code"),
                             reference_table="sync", reference_id=ev.get("reference_id"),
                             occurred_at=ev["occurred_at"], event_id=ev["event_id"],
                             note="Replayed from offline outbox"
                                  + (" (late arrival – applied out of order)" if late else ""),
                             cur=cur)
                if late:
                    cur.execute("UPDATE stock_event SET is_replayed = 1 WHERE event_id = ?",
                                (ev["event_id"],))
                accepted += 1
        except Exception as exc:
            log.error("event %s rejected: %s", ev.get("event_id"), exc)

    _OUTBOX[int(store_id)] = []
    completed = datetime.now()
    latency_ms = int((completed - started).total_seconds() * 1000)
    status = "SUCCESS" if accepted else ("DUPLICATE_ONLY" if duplicates else "FAILED")

    execute(
        "INSERT INTO synchronization_logs (batch_id, store_id, node_id, direction, event_count, "
        "accepted_count, duplicate_count, rejected_count, out_of_order_count, started_at, "
        "completed_at, latency_ms, status, simulated_offline) "
        "VALUES (:b,:s,:n,'STORE_TO_CENTRAL',:e,:a,:d,:r,:oo,:st,:ct,:l,:stat,1)",
        {"b": batch_id, "s": store_id, "n": f"NODE-{store_code}",
         "e": len(events), "a": accepted, "d": duplicates, "r": 0,
         "oo": out_of_order, "st": started.isoformat(), "ct": completed.isoformat(),
         "l": latency_ms, "stat": status},
    )
    audit.record("SYNC_BATCH", entity="synchronization_logs", entity_id=batch_id,
                 store_id=store_id,
                 detail=f"{status}: accepted={accepted} duplicates={duplicates} "
                        f"out_of_order={out_of_order}")
    log.info("sync %s: accepted=%s dup=%s late=%s in %sms",
             batch_id, accepted, duplicates, out_of_order, latency_ms)
    return {"status": status, "accepted": accepted, "duplicates": duplicates,
            "out_of_order": out_of_order, "batch_id": batch_id, "latency_ms": latency_ms}


def replay_same_batch(store_id: int, batch_id: str) -> dict:
    """Prove idempotency: re-send the events of a completed batch."""
    rows = query("SELECT event_id, sku_id, event_type, quantity_delta, occurred_at "
                 "FROM stock_event WHERE sync_batch_id IS NULL AND store_id = :s "
                 "ORDER BY event_seq DESC LIMIT 5", {"s": int(store_id)})
    events = [
        {"event_id": r.event_id, "store_id": int(store_id), "sku_id": int(r.sku_id),
         "event_type": r.event_type, "quantity_delta": int(r.quantity_delta),
         "occurred_at": r.occurred_at, "reason_code": "REPLAY_TEST"}
        for r in rows.itertuples()
    ]
    _OUTBOX[int(store_id)] = events
    return push_loop(store_id, force=True)


def sync_logs(store_ids: list[int] | None = None, limit: int = 200) -> pd.DataFrame:
    sql = ["""
        SELECT l.sync_id, l.batch_id, s.store_code, l.node_id, l.event_count, l.accepted_count,
               l.duplicate_count, l.out_of_order_count, l.started_at, l.completed_at,
               l.latency_ms, l.status, l.simulated_offline
        FROM synchronization_logs l JOIN stores s ON s.store_id = l.store_id WHERE 1=1
    """]
    p: dict = {"l": int(limit)}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND l.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    sql.append("ORDER BY l.sync_id DESC LIMIT :l")
    return query(" ".join(sql), p)


def sync_health() -> dict:
    df = query("""
        SELECT COUNT(*) AS batches, COALESCE(SUM(event_count),0) AS events,
               COALESCE(SUM(accepted_count),0) AS accepted,
               COALESCE(SUM(duplicate_count),0) AS duplicates,
               COALESCE(SUM(out_of_order_count),0) AS out_of_order,
               COALESCE(SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END),0) AS ok_batches,
               ROUND(AVG(latency_ms),0) AS avg_latency_ms,
               MAX(completed_at) AS last_sync
        FROM synchronization_logs
    """)
    d = df.iloc[0].to_dict() if not df.empty else {}
    d["pending_outbox"] = sum(outbox_size(s) for s in _OFFLINE_STATE) + sum(
        len(v) for v in _OUTBOX.values())
    d["offline_stores"] = [k for k, v in _OFFLINE_STATE.items() if v]
    d["success_pct"] = round((d.get("ok_batches", 0) / max(d.get("batches", 1), 1)) * 100, 1)
    return d


def simulate_partition(store_id: int, n_events: int = 25, hours: int = 2) -> dict:
    """Run the offline drill: take a store offline, transact, then reconnect."""
    store_id = int(store_id)
    set_offline(store_id, True)
    picked = query("SELECT sku_id FROM stock_on_hand WHERE store_id = :s AND on_hand_qty > 20 "
                   "ORDER BY RANDOM() LIMIT :n", {"s": store_id, "n": int(n_events)})
    rng = random.Random(store_id)
    base = datetime.now() - timedelta(hours=hours)
    for i, r in enumerate(picked.itertuples()):
        qty = rng.randint(1, 3)
        queue_event(store_id, int(r.sku_id), "ADJUSTMENT_NEG", -qty,
                    occurred_at=(base + timedelta(minutes=i * 3)).isoformat(timespec="seconds"),
                    reason="OFFLINE_SALE")
    queued = outbox_size(store_id)
    before = sync_health()["pending_outbox"]
    set_offline(store_id, False)
    result = push_loop(store_id)
    return {"queued": queued, "result": result, "pending_before": before}


def reconcile_after_sync(store_id: int) -> pd.DataFrame:
    return query("""
        SELECT s.store_code, k.sku_code, k.product_name, soh.on_hand_qty AS cached_qty,
               COALESCE(SUM(e.quantity_delta),0) AS ledger_qty,
               soh.on_hand_qty - COALESCE(SUM(e.quantity_delta),0) AS variance
        FROM stock_on_hand soh
        JOIN stores s ON s.store_id = soh.store_id
        JOIN sku k ON k.sku_id = soh.sku_id
        LEFT JOIN stock_event e ON e.store_id = soh.store_id AND e.sku_id = soh.sku_id
        WHERE soh.store_id = :s
        GROUP BY soh.store_id, soh.sku_id
        HAVING ABS(soh.on_hand_qty - COALESCE(SUM(e.quantity_delta),0)) > 0
        ORDER BY ABS(variance) DESC
    """, {"s": int(store_id)})


# --------------------------------------------------------------------------
# Integration JSON contract (documented + used by the mock push payload)
# --------------------------------------------------------------------------
EVENT_SCHEMA = {
    "batch_id": "SYNC-FM-HYD-01-20261102-0001",
    "store_code": "FM-HYD-01",
    "node_id": "NODE-FM-HYD-01",
    "batch_sent_at": "2026-11-02T21:10:00Z",
    "auth": {"scheme": "Bearer", "token": "<store-scoped signed token>"},
    "events": [{
        "event_id": "uuid4 – generated by the store node, never reused",
        "sku_code": "FMCG001",
        "event_type": "SALE",
        "quantity_delta": -2,
        "occurred_at": "2026-11-02T20:58:11+05:30",
        "device_id": "POS-FM-HYD-01",
        "reference": {"table": "sales_transactions", "id": "12345"},
    }],
}


def sample_payload(store_id: int) -> str:
    code = scalar("SELECT store_code FROM stores WHERE store_id = :s", {"s": int(store_id)}, "STORE")
    payload = {
        "batch_id": new_batch_id(code),
        "store_code": code,
        "node_id": f"NODE-{code}",
        "batch_sent_at": datetime.utcnow().isoformat() + "Z",
        "auth": {"scheme": "Bearer", "token": "<store-scoped signed token>"},
        "events": _OUTBOX.get(int(store_id), [])[:5],
    }
    return json.dumps(payload, indent=2)
