"""Module 7 – Store Performance Analytics (ranking, KPIs, regional view)."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, KPI_LABELS, KPI_WEIGHTS
from database.db import query
from modules import store_performance as perf
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import fmt_money

COMPONENTS = [
    ("Sales performance", "n_sales", "sales_performance", "#14b8a6"),
    ("Stock availability", "n_availability", "stock_availability", "#38bdf8"),
    ("Inventory efficiency", "n_turnover", "inventory_efficiency", "#a78bfa"),
    ("Forecast accuracy", "n_forecast", "forecast_accuracy", "#f59e0b"),
    ("Replenishment", "n_replenishment", "replenishment_efficiency", "#fb7185"),
]


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Store Performance Analytics",
        "Eight stores compared on revenue, availability, inventory efficiency, forecast "
        "accuracy and replenishment — with a transparent, configurable composite score.",
        eyebrow="Performance",
    )
    demo_banner("The Store Performance Score is a management index, not an industry standard.")

    days = st.select_slider("Trailing window", options=[7, 14, 30, 60, 90], value=30,
                            format_func=lambda d: f"last {d} days")
    kpi = perf.store_kpi_current(store_ids, days=days)
    if kpi.empty:
        c.empty_state("No KPI history", "Seed the database to populate store KPIs.", "◉")
        return

    best, worst = kpi.iloc[0], kpi.iloc[-1]
    c.kpi_row([
        {"label": "Stores compared", "value": f"{len(kpi):,}", "sub": f"last {days} days"},
        {"label": "Top score", "value": f"{best['performance_score']:.1f}",
         "sub": f"{best['store_code']} · {best['grade']}", "color": COLORS["primary"]},
        {"label": "Lowest score", "value": f"{worst['performance_score']:.1f}",
         "sub": f"{worst['store_code']} · availability "
                f"{worst['stock_availability_pct']:.1f}%", "color": "#fb7185"},
        {"label": "Chain revenue", "value": fmt_money(float(kpi["sales_revenue"].sum())),
         "sub": "in the window", "color": COLORS["accent"]},
        {"label": "Avg availability", "value": f"{kpi['stock_availability_pct'].mean():.2f}%",
         "sub": "chain average", "color": "#a78bfa"},
    ])

    tabs = st.tabs(["Ranking", "KPI Comparison", "Trends", "Regional", "Score Lab"])
    with tabs[0]:
        _ranking(kpi)
    with tabs[1]:
        _comparison(kpi, days)
    with tabs[2]:
        _trends(store_ids, days)
    with tabs[3]:
        _regional(days)
    with tabs[4]:
        _score_lab(store_ids, days)


def _ranking(kpi: pd.DataFrame):
    section("Store ranking", "Weighted composite score, 0–100")
    left, right = st.columns([1, 1.5], gap="medium")
    with left:
        ranked = kpi.sort_values("performance_score")
        fig = go.Figure(go.Bar(
            x=ranked["performance_score"], y=ranked["store_code"], orientation="h",
            marker=dict(color=ranked["performance_score"],
                        colorscale=[[0, "#ef4444"], [0.45, "#f59e0b"],
                                    [0.75, "#14b8a6"], [1, "#38bdf8"]]),
            text=ranked["performance_score"].map(lambda v: f"{v:.1f}"),
            textposition="outside", textfont=dict(size=11, color=COLORS["muted"])))
        fig.update_layout(height=330, xaxis=dict(range=[0, 108], title="Score"))
        st.plotly_chart(fig, use_container_width=True)
    with right:
        section("Score composition", "Each KPI sub-score × its weight")
        fig = go.Figure()
        for label, col, wkey, colour in COMPONENTS:
            fig.add_trace(go.Bar(
                x=kpi["store_code"], y=(kpi[col] * KPI_WEIGHTS[wkey]).round(2),
                name=f"{label} ({int(KPI_WEIGHTS[wkey]*100)}%)",
                marker=dict(color=colour), hovertemplate="%{x}<br>%{y:.1f} pts<extra></extra>"))
        fig.update_layout(barmode="stack", height=330, yaxis=dict(title="Score points"),
                          legend=dict(orientation="h", y=-0.28, x=0))
        st.plotly_chart(fig, use_container_width=True)

    c.table(pd.DataFrame({
        "Rank": kpi["rank"], "Store": kpi["store_code"], "Name": kpi["store_name"],
        "Region": kpi["region"], "Format": kpi["store_format"],
        "Revenue": kpi["sales_revenue"].map(lambda v: f"₹{v:,.0f}"),
        "Availability %": kpi["stock_availability_pct"],
        "Turnover": kpi["inventory_turnover"],
        "Forecast %": kpi["forecast_accuracy_pct"],
        "Replenish %": kpi["replenishment_efficiency"],
        "Stockout freq %": kpi["stockout_frequency"],
        "Waste ₹": kpi["waste_value"].map(lambda v: f"₹{v:,.0f}"),
        "Score": kpi["performance_score"], "Grade": kpi["grade"],
    }), height=320)
    st.caption("A high-revenue store with empty shelves is deliberately NOT ranked first: "
               "availability (25%), inventory efficiency (20%) and forecast accuracy (15%) "
               "together carry 60% of the weight.")


def _comparison(kpi: pd.DataFrame, days: int):
    section("KPI comparison", "Normalised 0–100 sub-scores, all stores at once")
    labels = [c_[0].replace(" performance", "") for c_ in COMPONENTS]
    fig = go.Figure()
    for r in kpi.itertuples():
        vals = [getattr(r, c_[1]) for c_ in COMPONENTS]
        fig.add_trace(go.Scatterpolar(r=vals + vals[:1], theta=labels + labels[:1],
                                      fill="toself", name=str(r.store_code), opacity=0.4,
                                      line=dict(width=1.6)))
    fig.update_layout(height=430, polar=dict(
        bgcolor="rgba(255,255,255,.02)",
        radialaxis=dict(range=[0, 100], gridcolor=COLORS["grid"],
                        tickfont=dict(size=9, color=COLORS["muted"])),
        angularaxis=dict(gridcolor=COLORS["grid"],
                         tickfont=dict(size=10, color=COLORS["muted"]))),
        legend=dict(orientation="h", y=-0.12, x=0))
    st.plotly_chart(fig, use_container_width=True)

    section("Absolute KPI values", "The raw operational numbers behind the normalisation")
    c.table(pd.DataFrame({
        "Store": kpi["store_code"],
        "Revenue": kpi["sales_revenue"],
        "Units sold": kpi["units_sold"],
        "Turnover": kpi["inventory_turnover"],
        "Stockout freq %": kpi["stockout_frequency"],
        "Availability %": kpi["stock_availability_pct"],
        "Forecast acc %": kpi["forecast_accuracy_pct"],
        "Replenishment %": kpi["replenishment_efficiency"],
        "Avg inventory ₹": kpi["avg_inventory_value"],
        "Waste ₹": kpi["waste_value"],
        "Stockout loss ₹": kpi["stockout_loss_value"],
        "Score": kpi["performance_score"],
    }), height=300, column_config={
        "Revenue": st.column_config.NumberColumn(format="₹%.0f"),
        "Avg inventory ₹": st.column_config.NumberColumn(format="₹%.0f"),
        "Waste ₹": st.column_config.NumberColumn(format="₹%.0f"),
        "Stockout loss ₹": st.column_config.NumberColumn(format="₹%.0f")})

    section("Stores requiring attention", "Availability + stockout frequency + waste exposure")
    att = perf.needs_attention(days=days, limit=3)
    if att.empty:
        c.empty_state("No outliers", "All stores are within tolerance.", "✓")
    else:
        c.table(att.rename(columns={
            "store_code": "Store", "store_name": "Name",
            "stock_availability_pct": "Availability %",
            "stockout_frequency": "Stockout freq %",
            "stockout_loss_value": "Stockout loss", "waste_value": "Waste",
            "performance_score": "Score", "risk_score": "Risk index"}), height=180)


def _trends(store_ids, days):
    section("Performance trend", "Daily composite score per store")
    tr = perf.kpi_trend(store_ids, days=max(days, 60))
    if tr.empty:
        c.empty_state("No trend data", "", "◇")
        return
    fig = go.Figure()
    for code, g in tr.groupby("store_code"):
        fig.add_trace(go.Scatter(x=g["business_date"], y=g["performance_score"], name=str(code),
                                 mode="lines", line=dict(width=1.9)))
    fig.update_layout(height=330, yaxis=dict(title="Score", range=[0, 100]),
                      legend=dict(orientation="h", y=-0.22, x=0))
    st.plotly_chart(fig, use_container_width=True)

    g1, g2 = st.columns(2, gap="medium")
    with g1:
        section("Availability trend", "% of SKU-store pairs in stock")
        fig = go.Figure()
        for code, g in tr.groupby("store_code"):
            fig.add_trace(go.Scatter(x=g["business_date"], y=g["stock_availability_pct"],
                                     name=str(code), mode="lines", line=dict(width=1.6)))
        fig.update_layout(height=300, showlegend=False, yaxis=dict(title="Availability %"))
        st.plotly_chart(fig, use_container_width=True)
    with g2:
        section("Stockout loss trend", "₹ of lost sales per day (chain)")
        daily = (tr.groupby("business_date")["stockout_loss_value"].sum()
                   .reset_index().rename(columns={"stockout_loss_value": "loss"}))
        st.plotly_chart(c.line(daily, "business_date", "loss", height=300, fill=True),
                        use_container_width=True)


def _regional(days):
    section("Regional summary", "Regions aggregated across their stores")
    reg = perf.regional_summary(days=days)
    if reg.empty:
        c.empty_state("No regional data", "", "◇")
        return
    c.table(reg.rename(columns={
        "region": "Region", "stores": "Stores", "sales_revenue": "Revenue",
        "avg_availability": "Avg availability %", "avg_score": "Average score",
        "stockout_loss": "Stockout loss", "waste": "Waste value"}), height=250,
        column_config={"Revenue": st.column_config.NumberColumn(format="₹%.0f"),
                       "Stockout loss": st.column_config.NumberColumn(format="₹%.0f"),
                       "Waste value": st.column_config.NumberColumn(format="₹%.0f")})
    st.plotly_chart(c.bars(reg, "region", "avg_score", horizontal=True, height=280),
                    use_container_width=True)


def _score_lab(store_ids, days):
    section("Score lab", "Change the weights and watch the ranking move")
    st.caption("This is the value of a configurable index: management can decide that "
               "availability matters more than sales, and the ranking responds immediately.")
    cols = st.columns(5)
    w: dict[str, float] = {}
    for col, (key, label) in zip(cols, KPI_LABELS.items()):
        with col:
            w[key] = st.slider(label, 0, 60, int(KPI_WEIGHTS[key] * 100), 5, key=f"w_{key}") / 100.0
    total = sum(w.values())
    if total == 0:
        st.warning("At least one weight must be greater than zero.")
        return
    st.markdown(f"**Weight total: {total*100:.0f}%** "
                f"({'normalised internally to 100%' if abs(total - 1) > 0.001 else 'balanced'})")

    base = perf.store_kpi_current(store_ids, days=days)
    res = perf.score_with_custom_weights(store_ids, weights=w, days=days)
    if res.empty:
        return
    left, right = st.columns([1, 1.2], gap="medium")
    with left:
        merged = pd.DataFrame({
            "Store": base["store_code"], "Default score": base["performance_score"],
            "Default rank": base["rank"], "New score": res["performance_score"],
            "New rank": res["rank"], "Grade": res["grade"]})
        merged["Rank move"] = merged["Default rank"] - merged["New rank"]
        c.table(merged, height=300)
    with right:
        fig = go.Figure()
        fig.add_trace(go.Bar(x=base["store_code"], y=base["performance_score"],
                             name="Default weights", marker=dict(color="#334155")))
        fig.add_trace(go.Bar(x=res["store_code"], y=res["performance_score"],
                             name="Your weights", marker=dict(color=COLORS["primary"])))
        fig.update_layout(barmode="group", height=300, yaxis=dict(title="Score", range=[0, 100]),
                          legend=dict(orientation="h", y=1.15))
        st.plotly_chart(fig, use_container_width=True)

    section("Weight documentation", "Shipped inside the app so the score is never a black box")
    c.table(perf.weight_table(), height=230)
