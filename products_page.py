"""Module 8 – Product and Category Analytics."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS
from database.db import query
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import fmt_money


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Product & Category Analytics",
        "Fast movers, slow movers, excess stock and the categories that generate the most "
        "stockout exposure across the chain.",
        eyebrow="Assortment",
    )
    demo_banner()

    scope = (" AND sd.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
             if store_ids else "")
    totals = query(f"""
        SELECT ROUND(SUM(sd.gross_revenue),2) AS revenue, SUM(sd.units_sold) AS units,
               COUNT(DISTINCT sd.sku_id) AS skus
        FROM sales_daily sd
        WHERE sd.business_date >= date((SELECT MAX(business_date) FROM sales_daily), '-30 days')
        {scope}
    """).iloc[0]

    c.kpi_row([
        {"label": "Revenue (30d)", "value": fmt_money(float(totals["revenue"] or 0)),
         "sub": "all categories"},
        {"label": "Units sold (30d)", "value": f"{int(totals['units'] or 0):,}",
         "sub": "chain-wide", "color": COLORS["accent"]},
        {"label": "SKUs analysed", "value": f"{int(totals['skus'] or 0):,}",
         "sub": "active assortment"},
        {"label": "Categories", "value": f"{int(query('SELECT COUNT(*) AS n FROM product_categories').iloc[0]['n'])}",
         "sub": "FMCG taxonomy", "color": "#a78bfa"},
    ])

    tabs = st.tabs(["Category Analysis", "Best & Worst Sellers", "Stockout Prone",
                    "Excess Inventory"])
    with tabs[0]:
        _by_category(scope)
    with tabs[1]:
        _movers(scope)
    with tabs[2]:
        _stockout_prone(store_ids)
    with tabs[3]:
        _excess(store_ids)


def _by_category(scope):
    df = query(f"""
        SELECT c.category_name, ROUND(SUM(sd.gross_revenue),2) AS revenue,
               SUM(sd.units_sold) AS units,
               ROUND(SUM(sd.gross_revenue) / NULLIF(SUM(sd.cost_of_goods),0), 3) AS gross_margin_x,
               ROUND(100.0 * SUM(sd.demand_unmet) / NULLIF(SUM(sd.demand_total),0), 2)
                   AS unmet_pct
        FROM sales_daily sd JOIN sku k ON k.sku_id = sd.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE sd.business_date >= date((SELECT MAX(business_date) FROM sales_daily), '-30 days')
        {scope}
        GROUP BY c.category_name ORDER BY revenue DESC
    """)
    g1, g2 = st.columns(2, gap="medium")
    with g1:
        section("Revenue by category", "Last 30 business days")
        st.plotly_chart(c.bars(df, "category_name", "revenue", horizontal=True, height=340),
                        use_container_width=True)
    with g2:
        section("Unmet demand by category", "% of demand lost to stockouts")
        st.plotly_chart(c.bars(df, "category_name", "unmet_pct", horizontal=True, height=340),
                        use_container_width=True)

    section("Category scorecard", "")
    c.table(df.rename(columns={
        "category_name": "Category", "revenue": "Revenue", "units": "Units",
        "gross_margin_x": "Revenue ÷ COGS", "unmet_pct": "Unmet demand %"}), height=300,
        column_config={"Revenue": st.column_config.NumberColumn(format="₹%.0f")})


def _movers(scope):
    df = query(f"""
        SELECT k.sku_code, k.product_name, c.category_name,
               SUM(sd.units_sold) AS units, ROUND(SUM(sd.gross_revenue),2) AS revenue,
               ROUND(AVG(sd.units_sold), 2) AS avg_daily,
               ROUND(STDDEV(sd.units_sold), 2) AS demand_std,
               ROUND(100.0 * SUM(sd.demand_unmet) / NULLIF(SUM(sd.demand_total),0), 2) AS unmet_pct
        FROM sales_daily sd JOIN sku k ON k.sku_id = sd.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE sd.business_date >= date((SELECT MAX(business_date) FROM sales_daily), '-30 days')
        {scope}
        GROUP BY k.sku_id ORDER BY units DESC
    """)
    if df.empty:
        c.empty_state("No sales in scope", "", "◇")
        return

    top = df.head(15)
    bottom = df[df["units"] > 0].tail(15).sort_values("units")
    g1, g2 = st.columns(2, gap="medium")
    with g1:
        section("Best sellers", "Units sold, last 30 days")
        st.plotly_chart(c.bars(top.iloc[::-1], "product_name", "units", horizontal=True,
                               height=380, color=COLORS["primary"]), use_container_width=True)
    with g2:
        section("Slowest movers", "Units sold, last 30 days")
        st.plotly_chart(c.bars(bottom.iloc[::-1], "product_name", "units", horizontal=True,
                               height=380, color="#fb7185"), use_container_width=True)

    section("Fast vs slow segmentation", "Demand velocity and revenue contribution")
    df["segment"] = pd.cut(df["avg_daily"], [-1, 5, 15, 30, 10_000],
                           labels=["Slow (<5/day)", "Moderate (5–15)", "Fast (15–30)",
                                   "Very fast (>30)"])
    seg = df.groupby("segment", observed=True).agg(
        skus=("sku_code", "count"), units=("units", "sum"), revenue=("revenue", "sum")
    ).reset_index()
    seg["segment"] = seg["segment"].astype(str)
    st.plotly_chart(c.bars(seg, "segment", "skus", horizontal=True, height=260),
                    use_container_width=True)
    c.table(seg.rename(columns={"segment": "Segment", "skus": "SKUs", "units": "Units",
                                "revenue": "Revenue"}), height=180,
            column_config={"Revenue": st.column_config.NumberColumn(format="₹%.0f")})


def _stockout_prone(store_ids):
    section("Products with frequent stockouts", "Actual stockout episodes, chain-wide")
    where = ""
    if store_ids:
        where = " WHERE o.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
    df = query(f"""
        SELECT k.sku_code, k.product_name, c.category_name, COUNT(*) AS episodes,
               ROUND(SUM(o.duration_hours), 1) AS hours_out,
               SUM(o.units_lost) AS units_lost,
               ROUND(SUM(o.revenue_lost), 2) AS revenue_lost,
               ROUND(AVG(o.duration_hours), 1) AS avg_hours
        FROM stockout_event o JOIN sku k ON k.sku_id = o.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        {where}
        GROUP BY k.sku_id ORDER BY episodes DESC LIMIT 25
    """)
    if df.empty:
        c.empty_state("No stockout history", "", "✓")
        return
    st.plotly_chart(c.bars(df.head(15).iloc[::-1], "product_name", "episodes", horizontal=True,
                           height=380, color="#ef4444"), use_container_width=True)
    c.table(df.rename(columns={
        "sku_code": "SKU", "product_name": "Product", "category_name": "Category",
        "episodes": "Episodes", "hours_out": "Hours out", "units_lost": "Units lost",
        "revenue_lost": "Revenue lost", "avg_hours": "Avg duration (h)"}), height=300,
        column_config={"Revenue lost": st.column_config.NumberColumn(format="₹%.0f")})

    section("Category stockout probability", "Model output aggregated by category")
    cats = query("""
        SELECT c.category_name, ROUND(AVG(p.probability) * 100, 2) AS avg_probability,
               SUM(CASE WHEN p.probability >= 0.6 THEN 1 ELSE 0 END) AS high_risk_pairs,
               COUNT(*) AS pairs
        FROM stockout_prediction p JOIN sku k ON k.sku_id = p.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE p.as_of_date = (SELECT MAX(as_of_date) FROM stockout_prediction)
        GROUP BY c.category_name ORDER BY avg_probability DESC
    """)
    if not cats.empty:
        c.table(cats.rename(columns={
            "category_name": "Category", "avg_probability": "Avg probability %",
            "high_risk_pairs": "Pairs ≥60%", "pairs": "Pairs scored"}), height=260)


def _excess(store_ids):
    section("Excess inventory", "Stock above the SKU's own max stock level")
    where = ""
    if store_ids:
        where = " AND soh.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
    df = query(f"""
        SELECT s.store_code, k.sku_code, k.product_name, c.category_name,
               soh.on_hand_qty, k.max_stock_level, k.reorder_level,
               ROUND(soh.on_hand_qty * k.unit_cost, 2) AS tied_capital,
               ROUND(soh.on_hand_qty * 1.0 / NULLIF((SELECT AVG(units_sold) FROM sales_daily sd
                    WHERE sd.store_id = soh.store_id AND sd.sku_id = soh.sku_id
                    AND sd.business_date >= date((SELECT MAX(business_date) FROM sales_daily),
                                                 '-30 days')), 0), 1) AS days_of_stock
        FROM stock_on_hand soh JOIN sku k ON k.sku_id = soh.sku_id
        JOIN stores s ON s.store_id = soh.store_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE soh.on_hand_qty > k.max_stock_level * 1.35 {where}
        ORDER BY tied_capital DESC LIMIT 60
    """)
    if df.empty:
        st.success("No SKU is carrying excess stock above 135% of its max level.")
        return
    st.caption(f"{len(df)} store × SKU rows with more than 135% of the configured max level. "
               f"Tied capital: {fmt_money(float(df['tied_capital'].sum()))}.")
    c.table(df.rename(columns={
        "store_code": "Store", "sku_code": "SKU", "product_name": "Product",
        "category_name": "Category", "on_hand_qty": "On hand",
        "max_stock_level": "Max level", "reorder_level": "Reorder",
        "tied_capital": "Capital tied", "days_of_stock": "Days of stock"}), height=350,
        column_config={"Capital tied": st.column_config.NumberColumn(format="₹%.0f")})
