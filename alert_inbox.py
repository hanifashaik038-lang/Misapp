"""Module 3 – Stockout Detection and Alerts (inbox + lifecycle)."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import ALERT_DELIVERY_TARGET_MINUTES, COLORS
from database.db import query
from modules import alerts as alert_svc
from modules import stockout_detection as det
from ui import components as c
from ui.theme import demo_banner, page_header, section

SEV_COLORS = {"CRITICAL": "#ef4444", "HIGH": "#f97316", "MODERATE": "#f59e0b", "LOW": "#14b8a6"}


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Stockout Detection & Alerts",
        "Two distinct signals: shelves that are empty right now (actual stockout) and "
        "products projected to fail before the next delivery (potential stockout).",
        eyebrow="Detection · Alerts",
    )
    demo_banner()

    stats = alert_svc.alert_stats(store_ids)
    oos = det.stockout_summary(store_ids)

    c.kpi_row([
        {"label": "Open alerts", "value": f"{stats['open']:,}", "sub": "unresolved",
         "color": "#ef4444"},
        {"label": "Critical open", "value": f"{stats['critical']:,}",
         "sub": "shelf empty or ≥80% risk", "color": "#fb7185"},
        {"label": "Active stockouts", "value": f"{oos['active']:,}",
         "sub": "on-hand ≤ 0 now", "color": "#f97316"},
        {"label": "Avg delivery latency",
         "value": f"{stats['avg_latency_s']:.2f} s",
         "sub": f"SLA target {ALERT_DELIVERY_TARGET_MINUTES*60} s",
         "color": COLORS["primary"]},
        {"label": "Alerts within SLA", "value": f"{stats['sla_pct']:.1f}%",
         "sub": f"of {stats['total']:,} alerts", "color": COLORS["accent"]},
    ])

    tabs = st.tabs(["Alert Inbox", "Alert Detail & Workflow", "Detection Rules",
                    "Stockout Episodes", "Channels & SLA"])
    with tabs[0]:
        _inbox(user, store_ids, stores_df)
    with tabs[1]:
        _detail(user, store_ids)
    with tabs[2]:
        _rules(store_ids)
    with tabs[3]:
        _episodes(store_ids, stores_df)
    with tabs[4]:
        _channels()


# -------------------------------------------------------------------------
def _inbox(user, store_ids, stores_df):
    f1, f2, f3, f4 = st.columns([1.1, 1.1, 1.1, 1.4], gap="small")
    with f1:
        status = st.selectbox("Status", ["ACTIVE", "OPEN", "ACKNOWLEDGED", "ASSIGNED",
                                        "RESOLVED", "CLOSED"], index=0)
    with f2:
        sev = st.selectbox("Severity", ["All", "CRITICAL", "HIGH", "MODERATE", "LOW"])
    with f3:
        atype = st.selectbox("Type", ["All", "ACTUAL_STOCKOUT", "POTENTIAL_STOCKOUT", "LOW_STOCK"])
    with f4:
        search = st.text_input("Search product / store", placeholder="e.g. Milk or FM-HYD-01")

    df = alert_svc.list_alerts(store_ids=store_ids,
                               status=None if status == "ACTIVE" else status,
                               severity=None if sev == "All" else sev,
                               alert_type=None if atype == "All" else atype,
                               limit=800)
    if search and not df.empty:
        m = (df["product_name"].str.contains(search, case=False, na=False)
             | df["store_code"].str.contains(search, case=False, na=False)
             | df["sku_code"].str.contains(search, case=False, na=False))
        df = df[m]

    if df.empty:
        c.empty_state("No alerts in this view", "Try a different status or run an alert sweep.",
                      "✓")
        return

    left, right = st.columns([1, 1.6], gap="medium")
    with left:
        section("Severity mix", "Active alerts")
        mix = df.groupby("severity").size().reset_index(name="n")
        st.plotly_chart(c.donut(mix["severity"].tolist(), mix["n"].tolist(),
                                colors=[SEV_COLORS.get(s, "#8494ad") for s in mix["severity"]],
                                height=280), use_container_width=True)
    with right:
        section("Alerts per day", "Last 30 days by severity")
        tr = alert_svc.alert_trend(30, store_ids)
        if not tr.empty:
            fig = go.Figure()
            for s in ["CRITICAL", "HIGH", "MODERATE", "LOW"]:
                sub = tr[tr["severity"] == s]
                if sub.empty:
                    continue
                fig.add_trace(go.Bar(x=sub["alert_date"], y=sub["n"], name=s,
                                     marker=dict(color=SEV_COLORS[s])))
            fig.update_layout(barmode="stack", height=290,
                              legend=dict(orientation="h", y=1.14))
            st.plotly_chart(fig, use_container_width=True)

    section("Alert register", f"{len(df):,} alerts")
    view = df[["alert_ref", "detected_at", "store_code", "product_name", "alert_type",
               "severity", "status", "probability", "stock_at_alert", "delivered_at"]].copy()
    view["detected_at"] = view["detected_at"].astype(str).str[:19]
    view["delivered_at"] = view["delivered_at"].astype(str).str[:19]
    view = view.rename(columns={
        "alert_ref": "Alert ID", "detected_at": "Detected", "store_code": "Store",
        "product_name": "Product", "alert_type": "Type", "severity": "Severity",
        "status": "Status", "probability": "P", "stock_at_alert": "Stock",
        "delivered_at": "Delivered"})
    c.table(view, height=420)

    st.caption("Deduplication: an OPEN/ACKNOWLEDGED/ASSIGNED alert with the same "
               "(store, SKU, alert type) suppresses new alerts for 24 hours, so a persistent "
               "risk produces one ticket rather than a stream of duplicates.")


def _detail(user, store_ids):
    df = alert_svc.list_alerts(store_ids=store_ids, limit=500)
    if df.empty:
        c.empty_state("No alerts", "Nothing to work on.", "✓")
        return

    opts = {f"{r.alert_ref} · {r.store_code} · {r.product_name} [{r.status}]": int(r.alert_id)
            for r in df.itertuples()}
    sel = st.selectbox("Select alert", list(df["alert_ref"] + " · " + df["store_code"] + " · "
                                            + df["product_name"]))
    alert_id = int(df[(df["alert_ref"] + " · " + df["store_code"] + " · "
                       + df["product_name"]) == sel].iloc[0]["alert_id"])
    a = alert_svc.get_alert(alert_id)
    if not a:
        return

    st.write("")
    c.kpi_row([
        {"label": "Alert", "value": str(a["alert_ref"]),
         "sub": str(a["alert_type"]).replace("_", " ").title()},
        {"label": "Severity", "value": str(a["severity"]),
         "color": SEV_COLORS.get(str(a["severity"]), "#8494ad")},
        {"label": "Probability",
         "value": "n/a" if a["probability"] is None else f"{float(a['probability'])*100:.0f}%",
         "sub": "7-day horizon"},
        {"label": "Stock at alert", "value": f"{int(a['stock_at_alert'] or 0):,} units"},
        {"label": "Status", "value": str(a["status"])},
    ])

    st.write("")
    left, right = st.columns([1.5, 1], gap="medium")
    with left:
        section("Alert payload", "")
        st.markdown(f"**Message.** {a['message']}")
        st.markdown(f"**Recommended action.** {a['recommended_action'] or '—'}")
        st.markdown(
            f"**Detected** {str(a['detected_at'])[:19]} · "
            f"**Delivered** {str(a['delivered_at'])[:19] if a['delivered_at'] else '—'} · "
            f"**Channels** {a['notification_channels']}")
    with right:
        section("Lifecycle actions", "Role-gated transitions")
        note = st.text_input("Resolution note", placeholder="e.g. PO raised, stock restored")
        users = query("SELECT user_id, full_name, username FROM users WHERE is_active = 1")
        user_labels = ["{}".format(r.full_name) + " (" + r.username + ")"
                       for r in users.itertuples()]
        assignee = st.selectbox("Assign to", user_labels)
        b1, b2 = st.columns(2)
        with b1:
            if st.button("Acknowledge", use_container_width=True,
                         disabled=str(a["status"]) != "OPEN"):
                try:
                    alert_svc.acknowledge(alert_id, user)
                    st.success("Acknowledged.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
            if st.button("Resolve", type="primary", use_container_width=True,
                         disabled=str(a["status"]) in {"RESOLVED", "CLOSED"}):
                try:
                    alert_svc.resolve(alert_id, user, note or "Resolved from dashboard")
                    st.success("Resolved.")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        with b2:
            if st.button("Assign", use_container_width=True):
                try:
                    idx = [f"{r.full_name} ({r.username})" for r in users.itertuples()].index(assignee)
                    alert_svc.assign(alert_id, int(users.iloc[idx]["user_id"]), user)
                    st.success("Assigned.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Assign failed: {exc}")
            if st.button("Close", type="secondary", use_container_width=True):
                try:
                    alert_svc.close(alert_id, user)
                    st.success("Closed.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))

    section("Full record", "")
    st.json({k: (str(v) if v is not None else None) for k, v in a.items()})


def _rules(store_ids):
    section("Detection logic", "Implemented in modules/stockout_detection.py")
    st.markdown("""
### A. Actual stockout — the shelf is empty **now**
```
available = on_hand_qty - reserved_qty
if available <= 0  ->  open a stockout_event (ended_at NULL) + CRITICAL alert
```
Repeated evaluation does **not** open a second episode: an open episode for that
store/SKU is reused, and the alert dedup key `(store, sku, ACTUAL_STOCKOUT)` blocks
repeat notifications. The episode is closed automatically once stock returns.

### B. Potential stockout — the shelf will empty before the next delivery
| Condition | Severity | Type |
|---|---|---|
| `on_hand ≤ safety_stock` (i.e. deeply below the buffer) | HIGH | POTENTIAL_STOCKOUT |
| `on_hand ≤ reorder_level` | MODERATE | LOW_STOCK |
| model 7-day probability 61–80% | HIGH | POTENTIAL_STOCKOUT |
| model probability ≥ 81% | CRITICAL | POTENTIAL_STOCKOUT |

The two families are kept separate on purpose: an *actual* stockout has already
cost revenue; a *potential* one is still preventable.
""")

    section("Run detection now", "Batch sweep over the latest prediction set")
    col1, col2 = st.columns([1, 2])
    with col1:
        thr = st.slider("Probability threshold", 0.30, 0.95, 0.60, 0.05)
    with col2:
        st.caption("Creates POTENTIAL_STOCKOUT alerts for every store × SKU at or above the "
                   "threshold. Existing unresolved alerts are suppressed by deduplication.")
    if st.button("Run alert sweep", type="primary"):
        res = det.generate_from_predictions(store_ids=store_ids, threshold=thr)
        st.success(f"Evaluated {res['evaluated']} pairs · created {res['created']} alerts · "
                   f"{res['suppressed']} suppressed as duplicates.")
    if st.button("Reconcile stockout episodes"):
        n = det.close_resolved_stockouts()
        st.success(f"Closed {n} episodes whose stock has been restored.")

    section("Currently out of stock", "Live from stock_on_hand")
    oos = det.currently_out_of_stock(store_ids)
    if oos.empty:
        st.success("No SKU is at zero stock in the selected scope.")
    else:
        c.table(oos.rename(columns={
            "store_code": "Store", "store_name": "Store name", "sku_code": "SKU",
            "product_name": "Product", "category_name": "Category",
            "on_hand_qty": "On hand", "in_transit": "In transit",
            "reorder_level": "Reorder level"}), height=300)


def _episodes(store_ids, stores_df):
    df = det.stockout_episodes(store_ids=store_ids, limit=2000)
    if df.empty:
        c.empty_state("No episodes recorded", "The simulation produced no stockouts.", "◇")
        return
    left, right = st.columns([1, 1.5], gap="medium")
    with left:
        section("Episodes by store", "")
        g = df.groupby("store_code").size().reset_index(name="n").sort_values("n")
        st.plotly_chart(c.bars(g, "store_code", "n", horizontal=True, height=300),
                        use_container_width=True)
    with right:
        section("Lost revenue by category", "Actual stockouts only")
        g2 = (df.groupby("category_name")["revenue_lost"].sum().reset_index()
                .sort_values("revenue_lost"))
        st.plotly_chart(c.bars(g2, "category_name", "revenue_lost", horizontal=True, height=300),
                        use_container_width=True)

    view = df[["started_at", "store_code", "product_name", "category_name", "severity",
               "ended_at", "duration_hours", "units_lost", "revenue_lost",
               "root_cause"]].rename(columns={
                   "started_at": "Started", "store_code": "Store", "product_name": "Product",
                   "category_name": "Category", "severity": "Severity", "ended_at": "Ended",
                   "duration_hours": "Duration (h)", "units_lost": "Units lost",
                   "revenue_lost": "Revenue lost", "root_cause": "Root cause"})
    c.table(view, height=420,
            column_config={"Revenue lost": st.column_config.NumberColumn(format="₹%.0f")})


def _channels():
    section("Notification channels", "Integration-ready, mock transports in this build")
    st.markdown("""
| Channel | Status in this build | Production implementation |
|---|---|---|
| In-app inbox | **Fully functional** — alerts persist in the `alerts` table | Streamlit / web inbox, WebSocket push |
| Email | Mock transport (`EmailTransport`) — logs the exact message it would send | SMTP / SendGrid / SES with a queue |
| SMS | Mock transport | Twilio / Gupshup with an opt-out registry |
| Slack | Mock transport | Incoming webhook to `#freshmart-ops` |
| Push | Not implemented | FCM / APNs for the store app |
""")
    section("Measured delivery latency", "Detection → delivery, against the 5-minute SLA")
    perf = alert_svc.channel_performance()
    if perf.empty:
        c.empty_state("No delivery data", "Create alerts first.", "◇")
    else:
        perf = perf.rename(columns={
            "alert_type": "Alert type", "severity": "Severity", "alerts": "Alerts",
            "avg_latency_seconds": "Avg latency (s)", "within_sla": "Within SLA"})
        perf["SLA compliance %"] = (perf["Within SLA"] / perf["Alerts"] * 100).round(1)
        c.table(perf, height=260)
        st.caption("In this single-process demonstration the transport is in-process, so latency "
                   "is milliseconds. The column exists so the SLA claim is measured, not asserted — "
                   "a real deployment with an external SMS gateway would show seconds-to-minutes.")
