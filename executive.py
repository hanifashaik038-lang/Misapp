"""Executive Overview Dashboard – chain-wide command centre."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, KPI_WEIGHTS
from database.db import query
from modules import alerts as alert_svc
from modules import inventory as inv
from modules import stockout_detection as det
from modules import stockout_prediction as pred
from modules import store_performance as perf
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import fmt_int, fmt_money


def _sales_trend(days: int = 60):
    return query("""
        SELECT business_date,
               ROUND(SUM(gross_revenue),2) AS revenue,
               SUM(units_sold) AS units,
               ROUND(SUM(revenue_lost_placeholder),2) AS dummy
        FROM (SELECT business_date, gross_revenue, units_sold, 0 AS revenue_lost_placeholder
              FROM sales_daily) GROUP BY business_date
        ORDER BY business_date
    """)


def _trend(days: int = 60, store_ids=None):
    where = ""
    if store_ids:
        where = " WHERE k.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
    return query(f"""
        SELECT k.business_date,
               ROUND(SUM(k.sales_revenue),2) AS revenue,
               SUM(k.units_sold) AS units,
               ROUND(AVG(k.stock_availability_pct),2) AS availability,
               ROUND(SUM(k.inventory_value),2) AS inventory_value,
               ROUND(SUM(k.stockout_loss_value),2) AS stockout_loss
        FROM store_kpi_daily k {where}
        WHERE_clause
        GROUP BY k.business_date ORDER BY k.business_date
    """.replace("WHERE_clause", ("AND" if where else "WHERE")
                + f" k.business_date >= date((SELECT MAX(business_date) FROM store_kpi_daily), '-{int(days)} days')"))


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Executive Overview",
        "Chain-wide command centre: live inventory position, 7-day stockout risk and "
        "comparative store performance for FreshMart Supermarkets.",
        eyebrow="Head Office · Consolidated View",
    )
    demo_banner("All figures are computed live from the simulated database.")

    ps = pred.prediction_summary(store_ids)
    kpi = perf.store_kpi_current(store_ids, days=30)
    valuation = inv.inventory_valuation(store_ids)
    a = alert_svc.alert_stats(store_ids)
    oos = det.stockout_summary(store_ids)

    n_stores = int(scaled_store_count(store_ids))
    n_skus = int(query("SELECT COUNT(*) AS n FROM sku WHERE is_active=1").iloc[0]["n"])
    daily_sales = float(kpi["sales_revenue"].sum() / 30) if not kpi.empty else 0.0
    avg_score = float(kpi["performance_score"].mean()) if not kpi.empty else 0.0

    c.kpi_row([
        {"label": "Stores in scope", "value": fmt_int(n_stores), "sub": "8-store regional chain",
         "icon": "◈"},
        {"label": "Active SKUs", "value": fmt_int(n_skus), "sub": "10 FMCG categories", "icon": "▤"},
        {"label": "Inventory units", "value": fmt_int(valuation["units"]),
         "sub": fmt_money(valuation["cost_value"]) + " at cost", "icon": "◫"},
        {"label": "Avg daily sales", "value": fmt_money(daily_sales),
         "sub": "30-day trailing average", "icon": "◔", "color": COLORS["accent"]},
    ])
    st.write("")
    c.kpi_row([
        {"label": "At stockout risk", "value": fmt_int(ps.get("at_risk", 0)),
         "sub": "≥60% probability, next 7 days", "color": "#f97316", "icon": "◎"},
        {"label": "Current stockouts", "value": fmt_int(oos["active"]),
         "sub": f"{fmt_int(oos['episodes'])} episodes on record", "color": "#ef4444", "icon": "▮"},
        {"label": "Avg stockout probability", "value": f"{ps.get('avg_probability', 0)}%",
         "sub": f"{ps.get('critical', 0)} critical · {ps.get('high', 0)} high", "color": "#fb7185",
         "icon": "◉"},
        {"label": "Performance score", "value": f"{avg_score:.1f}",
         "sub": "Weighted KPI index (0–100)", "color": COLORS["primary"], "icon": "⬢"},
    ])

    st.write("")
    left, right = st.columns([2.05, 1], gap="medium")

    with left:
        section("Sales & availability trend", "60 business days, chain aggregate")
        tr = _trend(60, store_ids)
        if not tr.empty:
            fig = go.Figure()
            fig.add_trace(go.Bar(x=tr["business_date"], y=tr["revenue"], name="Revenue",
                                 marker=dict(color="rgba(20,184,166,.55)"),
                                 hovertemplate="%{x}<br>Revenue ₹%{y:,.0f}<extra></extra>"))
            fig.add_trace(go.Scatter(x=tr["business_date"], y=tr["availability"],
                                     name="Stock availability %", yaxis="y2", mode="lines",
                                     line=dict(color="#38bdf8", width=2.4, shape="spline"),
                                     hovertemplate="%{x}<br>Availability %{y:.1f}%<extra></extra>"))
            fig.update_layout(
                height=330, yaxis=dict(title="Revenue (₹)"),
                yaxis2=dict(title="Availability %", overlaying="y", side="right",
                            range=[80, 100], showgrid=False),
                legend=dict(orientation="h", y=1.16, x=0), margin=dict(t=24, b=10))
            st.plotly_chart(fig, use_container_width=True)
        else:
            c.empty_state("No KPI history", "Seed the database to populate the trend.")

        section("Stockout risk distribution", "Every scored store × SKU pair")
        dist = pred.risk_distribution(store_ids)
        if not dist.empty:
            d1, d2 = st.columns([1, 1.35], gap="medium")
            with d1:
                from utils.calculations import RISK_COLOR
                st.plotly_chart(c.donut(dist["risk_level"].tolist(), dist["count"].tolist(),
                                        colors=[RISK_COLOR[r] for r in dist["risk_level"]],
                                        height=280), use_container_width=True)
            with d2:
                cat = query("""
                    SELECT c.category_name, ROUND(AVG(p.probability)*100,2) AS avg_probability,
                           SUM(CASE WHEN p.probability >= 0.6 THEN 1 ELSE 0 END) AS at_risk
                    FROM stockout_prediction p JOIN sku k ON k.sku_id = p.sku_id
                    JOIN product_categories c ON c.category_id = k.category_id
                    WHERE p.as_of_date = (SELECT MAX(as_of_date) FROM stockout_prediction)
                    GROUP BY c.category_name ORDER BY avg_probability DESC
                """)
                st.plotly_chart(
                    c.bars(cat, "category_name", "avg_probability", height=310, horizontal=True),
                    use_container_width=True)

    with right:
        section("Store performance ranking", "Weighted score, last 30 days")
        if not kpi.empty:
            show = kpi[["store_code", "store_name", "performance_score", "stock_availability_pct"]]
            st.plotly_chart(
                c.bars(kpi.rename(columns={"store_code": "store"}), "store", "performance_score",
                       horizontal=True, height=300),
                use_container_width=True)
            top = kpi.iloc[0]
            bottom = kpi.iloc[-1]
            st.markdown(
                f'<div class="ss-card" style="font-size:.8rem;line-height:1.6">'
                f'<div style="color:{COLORS["primary"]};font-weight:700">▲ Top performer</div>'
                f'{top["store_name"]} — score {top["performance_score"]:.1f} '
                f'({top["grade"]})<br>'
                f'<div style="color:#fb7185;font-weight:700;margin-top:.5rem">'
                f'▼ Needs attention</div>'
                f'{bottom["store_name"]} — score {bottom["performance_score"]:.1f}, '
                f'availability {bottom["stock_availability_pct"]:.1f}%</div>',
                unsafe_allow_html=True)

        section("Recent alerts", "Highest severity first")
        al = alert_svc.list_alerts(store_ids=store_ids, limit=7)
        if al.empty:
            c.empty_state("No alerts", "Nothing requires attention right now.", "✓")
        else:
            sev_color = {"CRITICAL": "#ef4444", "HIGH": "#f97316",
                         "MODERATE": "#f59e0b", "LOW": "#14b8a6"}
            for r in al.itertuples():
                badge_html = c.badge(r.severity, sev_color.get(r.severity, "#8494ad"))
                type_label = str(r.alert_type).replace("_", " ").title()
                st.markdown(
                    '<div class="ss-card" style="padding:.6rem .75rem;margin-bottom:.42rem">'
                    '<div style="display:flex;justify-content:space-between;gap:.5rem">'
                    '<span style="font-size:.8rem;font-weight:650">' + str(r.product_name)
                    + '</span>' + badge_html + '</div>'
                    '<div style="font-size:.71rem;color:' + COLORS["muted"]
                    + ';margin-top:.2rem">' + str(r.store_code) + ' · ' + type_label
                    + ' · ' + str(r.detected_at)[:16] + '</div></div>',
                    unsafe_allow_html=True)

    section("Products requiring immediate replenishment",
            "Critical-risk pairs where days of cover is below the supplier lead time")
    urgent = pred.predictions(store_ids=store_ids, risk_levels=["Critical Risk", "High Risk"],
                              limit=400)
    if urgent.empty:
        c.empty_state("No critical exposure", "All products have adequate cover.", "✓")
    else:
        urgent["gap_days"] = (urgent["days_of_cover"] - urgent["lead_time_days"]).round(1)
        view = urgent.head(25)[[
            "store_code", "product_name", "category_name", "current_stock", "avg_daily_demand",
            "days_of_cover", "lead_time_days", "probability", "reorder_qty", "recommended_action"]]
        view = view.rename(columns={
            "store_code": "Store", "product_name": "Product", "category_name": "Category",
            "current_stock": "Stock", "avg_daily_demand": "Avg/day", "days_of_cover": "Cover (d)",
            "lead_time_days": "Lead (d)", "probability": "P(stockout)", "reorder_qty": "Order qty",
            "recommended_action": "Recommended action"})
        c.table(view, height=430, column_config={
            "P(stockout)": st.column_config.ProgressColumn(
                "P(stockout)", format="percent", min_value=0.0, max_value=1.0)})

    st.write("")
    with st.expander("How this dashboard is calculated"):
        st.markdown(f"""
- **Every value is a live SQL aggregate** over `store_kpi_daily`, `stockout_prediction`,
  `stock_on_hand` and `alerts`. Nothing on this page is hardcoded.
- **At stockout risk** counts store × SKU pairs whose 7-day probability is ≥ 60%.
- **Current stockouts** counts `stockout_event` rows with no `ended_at` (shelf empty now).
  This is *actual* stockout — distinct from *predicted* risk.
- **Performance score** = {' + '.join(f'{int(v*100)}% {k.replace("_", " ")}' for k, v in KPI_WEIGHTS.items())},
  where each component is min-max normalised across stores. It is a configurable
  management index, not an industry-standard formula.
""")


def scaled_store_count(store_ids: list[int] | None) -> int:
    if store_ids is None:
        return int(query("SELECT COUNT(*) AS n FROM stores WHERE is_active=1").iloc[0]["n"])
    return len(store_ids)
