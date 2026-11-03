"""Module 9 – Supplier Performance."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS
from modules import supplier_analytics as sup
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import fmt_money


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Supplier Performance",
        "On-time delivery, fulfilment accuracy, lead-time variance and how much upstream "
        "unreliability actually contributed to downstream stockouts.",
        eyebrow="Supply Chain",
    )
    demo_banner()

    sc = sup.supplier_scorecard()
    if sc.empty:
        c.empty_state("No supplier data", "Run the seed script first.", "⬢")
        return

    best = sc.iloc[0]
    worst = sc.iloc[-1]
    c.kpi_row([
        {"label": "Suppliers", "value": f"{len(sc):,}", "sub": "active vendor master"},
        {"label": "Best OTIF", "value": f"{best['on_time_pct']:.1f}%",
         "sub": f"{best['supplier_name']}", "color": COLORS["primary"]},
        {"label": "Weakest OTIF", "value": f"{worst['on_time_pct']:.1f}%",
         "sub": f"{worst['supplier_name']}", "color": "#ef4444"},
        {"label": "Avg fulfilment", "value": f"{sc['fulfilment_pct'].mean():.1f}%",
         "sub": "received ÷ ordered", "color": COLORS["accent"]},
        {"label": "Qty discrepancies",
         "value": f"{int(sc['qty_discrepancy_units'].fillna(0).sum()):,}",
         "sub": "units short-delivered", "color": "#f59e0b"},
    ])

    tabs = st.tabs(["Scorecard", "Lead Time", "Stockout Contribution", "Supplier Detail"])
    with tabs[0]:
        _scorecard(sc)
    with tabs[1]:
        _lead_time()
    with tabs[2]:
        _contribution()
    with tabs[3]:
        _detail(sc)


def _scorecard(sc: pd.DataFrame):
    section("Supplier scorecard", "Weighted from on-time %, fulfilment %, lead time and stockout impact")
    st.caption("Weights: 35% on-time delivery · 30% fulfilment · 20% lead-time adherence · "
               "15% stockout contribution. A composite vendor score, not a contractual SLA.")

    left, right = st.columns([1.3, 1], gap="medium")
    with left:
        d = sc.sort_values("supplier_score")
        st.plotly_chart(c.bars(d, "supplier_name", "supplier_score", horizontal=True,
                               height=440), use_container_width=True)
    with right:
        section("On-time vs fulfilment", "Bubble size = orders analysed")
        fig = go.Figure(go.Scatter(
            x=sc["on_time_pct"], y=sc["fulfilment_pct"],
            mode="markers+text", text=sc["supplier_code"], textposition="top center",
            textfont=dict(size=8.5, color=COLORS["muted"]),
            marker=dict(size=(sc["orders_count"].fillna(1) ** 0.42) * 4,
                        color=sc["supplier_score"],
                        colorscale=[[0, "#ef4444"], [0.5, "#f59e0b"], [1, "#14b8a6"]],
                        line=dict(width=0.6, color="#07101d"), opacity=0.88),
            hovertemplate="<b>%{text}</b><br>On time %{x:.1f}%<br>"
                          "Fulfilment %{y:.1f}%<extra></extra>"))
        fig.update_layout(height=440, xaxis=dict(title="On-time delivery %"),
                          yaxis=dict(title="Order fulfilment %"))
        st.plotly_chart(fig, use_container_width=True)

    c.table(pd.DataFrame({
        "Supplier": sc["supplier_name"], "Code": sc["supplier_code"], "City": sc["city"],
        "SKUs": sc["skus_supplied"], "Orders": sc["orders_count"],
        "Contracted lead (d)": sc["contracted_lead_time"],
        "Actual lead (d)": sc["avg_lead_time_days"],
        "Lead gap (d)": sc["lead_time_gap"],
        "On time %": sc["on_time_pct"], "Fulfilment %": sc["fulfilment_pct"],
        "Qty short": sc["qty_discrepancy_units"],
        "Stockout episodes": sc["stockout_contribution"],
        "Score": sc["supplier_score"], "Rating": sc["rating"].astype(str),
    }), height=360)


def _lead_time():
    section("Contracted vs actual lead time", "Positive gap = supplier is slower than promised")
    df = sup.lead_time_vs_actual()
    if df.empty:
        c.empty_state("No lead-time history", "", "◇")
        return
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["supplier_name"], y=df["contracted"],
                         name="Contracted", marker=dict(color="#334155")))
    fig.add_trace(go.Bar(x=df["supplier_name"], y=df["actual"],
                         name="Observed average", marker=dict(color=COLORS["primary"])))
    fig.update_layout(barmode="group", height=360, yaxis=dict(title="Days"),
                      xaxis=dict(tickangle=-38, tickfont=dict(size=9)),
                      legend=dict(orientation="h", y=1.12))
    st.plotly_chart(fig, use_container_width=True)

    d = df.sort_values("gap", ascending=False)
    st.plotly_chart(c.bars(d, "supplier_name", "gap", horizontal=True, height=380,
                           color="#f97316"), use_container_width=True)
    st.caption("Every day of unpredicted lead time feeds straight into safety stock. Suppliers "
               "with a persistent positive gap should either have their contracted lead time "
               "corrected in the master data or be escalated commercially.")


def _contribution():
    section("Stockout contribution", "Episodes attributed to products each supplier owns")
    df = sup.supplier_stockout_contribution()
    if df.empty:
        c.empty_state("No contribution data", "", "◇")
        return
    left, right = st.columns(2, gap="medium")
    with left:
        st.plotly_chart(c.bars(df.head(12), "supplier_name", "stockout_episodes",
                               horizontal=True, height=360, color="#ef4444"),
                        use_container_width=True)
    with right:
        fig = go.Figure(go.Scatter(
            x=df["on_time_pct"], y=df["stockout_episodes"], mode="markers",
            marker=dict(size=11, color=COLORS["accent"], opacity=0.8,
                        line=dict(width=0.6, color="#07101d")),
            text=df["supplier_name"],
            hovertemplate="<b>%{text}</b><br>On time %{x:.1f}%<br>"
                          "Episodes %{y:,}<extra></extra>"))
        fig.update_layout(height=360, xaxis=dict(title="On-time delivery %"),
                          yaxis=dict(title="Stockout episodes"))
        st.plotly_chart(fig, use_container_width=True)
    st.caption("Correlation is shown, not causation: a supplier with many stockout episodes may "
               "simply own high-velocity SKUs. The causal weight is capped at 15% inside the "
               "supplier score for that reason.")


def _detail(sc: pd.DataFrame):
    section("Supplier drill-down", "Purchase order history and stockouts on their SKUs")
    labels = [f"{r.supplier_name} · {r.supplier_code}" for r in sc.itertuples()]
    pick = st.selectbox("Supplier", labels)
    sid = int(sc.iloc[labels.index(pick)]["supplier_id"])
    d = sup.supplier_detail(sid)
    if not d:
        return
    s = d["supplier"]
    c.metric_strip([
        ("Supplier", s["supplier_name"]), ("City", s["city"]),
        ("Lead time", f"{int(s['lead_time_days'])} days"),
        ("Reliability", f"{float(s['reliability_score'])*100:.0f}%"),
        ("Min order", fmt_money(float(s["min_order_value"]))),
        ("ASN capable", "Yes" if int(s["supports_asn"]) else "No"),
    ])
    g1, g2 = st.columns(2, gap="medium")
    with g1:
        section("Recent purchase orders", "")
        po = d["purchase_orders"]
        if po.empty:
            c.empty_state("No orders", "", "◇")
        else:
            c.table(po.rename(columns={
                "po_number": "PO", "store_code": "Store", "status": "Status",
                "order_date": "Ordered", "expected_date": "Expected",
                "actual_delivery_date": "Delivered", "days_variance": "Variance (d)",
                "total_value": "Value"}), height=280, column_config={
                "Value": st.column_config.NumberColumn(format="₹%.0f")})
    with g2:
        section("Stockouts on this supplier's SKUs", "")
        oo = d["stockouts"]
        if oo.empty:
            c.empty_state("No stockouts", "", "✓")
        else:
            c.table(oo.rename(columns={
                "store_code": "Store", "sku_code": "SKU", "product_name": "Product",
                "started_at": "Started", "duration_hours": "Hours",
                "units_lost": "Units lost", "revenue_lost": "Revenue lost"}), height=280,
                column_config={"Revenue lost": st.column_config.NumberColumn(format="₹%.0f")})
