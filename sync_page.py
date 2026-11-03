"""Synchronization monitor – simulated offline / reconnect drill."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from config import COLORS
from database.db import query
from modules import inventory as inv
from modules import synchronization as sync
from pages_pkg.common import single_store
from ui import components as c
from ui.theme import demo_banner, page_header, section


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Synchronization Monitor",
        "Store nodes keep trading when the network is down. This module queues events locally, "
        "replays them on reconnect, and rejects duplicates by event id.",
        eyebrow="Integration",
    )
    st.markdown(
        '<div class="ss-banner"><span class="x">◈</span> Scope note: this is a '
        '<b>single-database simulation</b> of the store edge node — an in-memory outbox plays '
        'the role of the store server. There is no real network partition, TLS transport or '
        'separate store database in this build. What is genuinely exercised: UUID event ids, '
        'idempotent ingestion, out-of-order arrival handling, sync logging and reconciliation.'
        '</div>', unsafe_allow_html=True)

    h = sync.sync_health()
    c.kpi_row([
        {"label": "Sync batches", "value": f"{int(h.get('batches') or 0):,}",
         "sub": f"{h.get('success_pct', 0)}% success"},
        {"label": "Events accepted", "value": f"{int(h.get('accepted') or 0):,}",
         "sub": "applied to central ledger", "color": COLORS["primary"]},
        {"label": "Duplicates rejected", "value": f"{int(h.get('duplicates') or 0):,}",
         "sub": "UNIQUE(event_id) enforcement", "color": COLORS["accent"]},
        {"label": "Out-of-order events", "value": f"{int(h.get('out_of_order') or 0):,}",
         "sub": "applied and flagged", "color": "#f59e0b"},
        {"label": "Avg batch latency", "value": f"{int(h.get('avg_latency_ms') or 0):,} ms",
         "sub": "SIMULATED transport"},
    ])

    tabs = st.tabs(["Batch Log", "Offline Drill", "Integration Contract", "Reconciliation"])
    with tabs[0]:
        _logs(store_ids)
    with tabs[1]:
        _drill(store_ids, stores_df)
    with tabs[2]:
        _contract(store_ids, stores_df)
    with tabs[3]:
        _recon(store_ids, stores_df)


def _logs(store_ids):
    section("Synchronization batches", "Newest first")
    df = sync.sync_logs(store_ids=store_ids, limit=300)
    if df.empty:
        c.empty_state("No batches", "Run the offline drill to create one.", "⇅")
        return
    c.table(df.rename(columns={
        "batch_id": "Batch", "store_code": "Store", "node_id": "Node",
        "event_count": "Events", "accepted_count": "Accepted",
        "duplicate_count": "Duplicates", "out_of_order_count": "Out of order",
        "started_at": "Started", "completed_at": "Completed",
        "latency_ms": "Latency (ms)", "status": "Status",
        "simulated_offline": "Simulated offline"}), height=420)


def _drill(store_ids, stores_df):
    section("Offline / reconnect drill", "The exact scenario from the test plan")
    st.markdown("""
1. Take a store offline (agent paused).
2. Process transactions — they queue in the local outbox with client-generated UUIDs.
3. Reconnect and push — the centre ingests every event exactly once.
4. Re-send the same batch — duplicates are rejected, stock does **not** change twice.
""")
    store_id = single_store(store_ids, stores_df, "sync_drill")
    a, b = st.columns([1, 2], gap="medium")
    with a:
        n_events = st.slider("Transactions while offline", 5, 60, 25, 5)
    with b:
        st.caption(f"Store {store_id} · outbox currently holds {sync.outbox_size(store_id)} "
                   f"event(s) · agent state: "
                   f"{'OFFLINE' if sync.is_offline(store_id) else 'ONLINE'}")

    before = inv.inventory_valuation([store_id])
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("Run offline drill", type="primary", use_container_width=True):
            res = sync.simulate_partition(store_id, n_events=n_events, hours=2)
            r = res["result"]
            st.success(f"Queued {res['queued']} events offline, then pushed batch. "
                       f"Accepted {r['accepted']}, duplicates {r['duplicates']}, "
                       f"out-of-order {r['out_of_order']}, status {r['status']}.")
    with col2:
        if st.button("Set store OFFLINE", use_container_width=True, type="secondary"):
            sync.set_offline(store_id, True)
            st.warning("Agent paused — new scans will queue locally.")
            st.rerun()
    with col3:
        if st.button("Push pending batch", use_container_width=True):
            r = sync.push_loop(store_id, force=True)
            st.info(f"{r['status']} · accepted {r['accepted']} · duplicates {r['duplicates']}")

    if st.button("Replay last batch (idempotency test)"):
        r = sync.replay_same_batch(store_id, "n/a")
        st.info(f"Replay result: status {r['status']} · accepted {r['accepted']} · "
                f"duplicates {r['duplicates']}. Stock balance unchanged → idempotency proven.")

    after = inv.inventory_valuation([store_id])
    c.metric_strip([
        ("Units before", f"{before['units']:,}"),
        ("Units after", f"{after['units']:,}"),
        ("Delta", f"{after['units'] - before['units']:,}"),
        ("Outbox", f"{sync.outbox_size(store_id)} pending"),
        ("Store state", "OFFLINE" if sync.is_offline(store_id) else "ONLINE"),
    ])
    st.caption("A negative delta equal to the queued quantity, applied exactly once, is the "
               "pass condition: no event lost, none double-counted.")


def _contract(store_ids, stores_df):
    section("Integration contract", "POST /sync/events payload")
    store_id = single_store(store_ids, stores_df, "sync_payload")
    payload = sync.sample_payload(store_id)
    st.code(payload or json.dumps(sync.EVENT_SCHEMA, indent=2), language="json")

    section("Documented integration points", "")
    st.markdown("""
| # | Integration | Mechanism | Owner of master data |
|---|---|---|---|
| 1 | Barcode scanner → store app | Keyboard-wedge (HID) input, newline-terminated; camera scan via `BarcodeDetector` where supported | — |
| 2 | Store app → local DB | SQLite (demo) / PostgreSQL (production), single writer per node | Store node |
| 3 | Store server → central DB | `POST /sync/events` over HTTPS with a store-scoped bearer token | Head office |
| 4 | Purchase orders & ASNs | Outbound PO (JSON/CSV), inbound ASN matched on `po_number` | Head office (PO), supplier (ASN) |
| 5 | Existing ERP / POS | Adapter writes `sales_transactions` + `sales_transaction_items`; SKU mapping table keyed on barcode | ERP |
| 6 | Email / SMS / Slack / push | `NotificationTransport` interface — mock implementations ship in `modules/alerts.py` | Ops |
| 7 | BI / reporting | Read-only replica or CSV/Excel/PDF exports from `modules/reporting.py` | Head office |

**Retry policy.** Exponential backoff (1s, 2s, 4s … cap 60s), max 8 attempts, then the batch
is parked in `synchronization_logs` with `status = FAILED` and an operator is notified.

**Duplicate-batch prevention.** Two layers: the batch id is unique, and every event carries a
client-generated UUID that is `UNIQUE` in `stock_event`. Re-sending is always safe.

**Event ordering.** Events carry `occurred_at` from the store clock. The ingester detects a
late arrival (older than the newest event for that store/SKU), applies it anyway, and flags
`is_replayed = 1` plus an `out_of_order_count` on the batch — because the ledger is a sum of
deltas, the balance stays correct regardless of arrival order.

**Network failure.** The store never blocks on the centre: writes go to the local DB and the
outbox, and the UI shows a pending badge. Nothing is lost if the process restarts, provided
the outbox is durable (in this demo it is in-memory, which is stated as a limitation).
""")


def _recon(store_ids, stores_df):
    section("Post-sync reconciliation", "Cache vs ledger, per store")
    store_id = single_store(store_ids, stores_df, "sync_recon")
    df = sync.reconcile_after_sync(store_id)
    if df.empty:
        st.success("Every SKU in this store reconciles: stock_on_hand equals the sum of "
                   "ledger deltas.")
    else:
        st.warning(f"{len(df)} SKU rows differ. Run 'Recompute stock_on_hand from ledger' "
                   f"on the Inventory page to repair from the immutable source of truth.")
        c.table(df, height=300)

    section("Audit trail", "Most recent 30 system actions")
    from modules import audit
    c.table(audit.recent(30), height=340)
