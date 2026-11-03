"""
Store Performance Analytics.

The Store Performance Score is a CONFIGURABLE management index built from
normalised KPI sub-scores – not an industry-standard formula. The weights live
in ``config.KPI_WEIGHTS`` and are always displayed next to the ranking so a
reviewer can see exactly how a number was produced.

Crucially, the score does NOT reward sales volume alone: a store with high
revenue but poor availability or heavy waste is penalised by the other four
components.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from config import KPI_LABELS, KPI_WEIGHTS
from database.db import query
from utils.calculations import min_max_normalise, performance_score, score_grade
from utils.helpers import get_logger

log = get_logger("stocksense.kpi")


def latest_kpi_date() -> str | None:
    v = query("SELECT MAX(business_date) AS d FROM store_kpi_daily")
    return None if v.empty or pd.isna(v.iloc[0]["d"]) else str(v.iloc[0]["d"])


def store_kpi_current(store_ids: list[int] | None = None, days: int = 30) -> pd.DataFrame:
    """Trailing-window KPI snapshot per store (default: last 30 business days)."""
    sql = ["""
        WITH win AS (
            SELECT * FROM store_kpi_daily
            WHERE business_date >= date((SELECT MAX(business_date) FROM store_kpi_daily), :d)
        )
        SELECT s.store_id, s.store_code, s.store_name, s.region, s.store_format,
               COUNT(w.kpi_id) AS days_observed,
               ROUND(SUM(w.sales_revenue), 2) AS sales_revenue,
               SUM(w.units_sold) AS units_sold,
               ROUND(AVG(NULLIF(w.inventory_turnover, 0)), 2) AS inventory_turnover,
               ROUND(AVG(w.stockout_frequency), 2) AS stockout_frequency,
               ROUND(AVG(w.stock_availability_pct), 2) AS stock_availability_pct,
               ROUND(AVG(w.forecast_accuracy_pct), 2) AS forecast_accuracy_pct,
               ROUND(AVG(w.replenishment_efficiency), 2) AS replenishment_efficiency,
               ROUND(AVG(w.avg_inventory_value), 2) AS avg_inventory_value,
               ROUND(SUM(w.waste_value), 2) AS waste_value,
               ROUND(SUM(w.stockout_loss_value), 2) AS stockout_loss_value
        FROM stores s JOIN win w ON w.store_id = s.store_id
        WHERE 1=1
    """]
    p: dict = {"d": f"-{int(days)} days"}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND s.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    sql.append("GROUP BY s.store_id ORDER BY sales_revenue DESC")
    df = query(" ".join(sql), p)
    if df.empty:
        return df
    return _score(df)


def _score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["n_sales"] = min_max_normalise(out["sales_revenue"], True)
    out["n_availability"] = min_max_normalise(out["stock_availability_pct"], True)
    out["n_turnover"] = min_max_normalise(out["inventory_turnover"].fillna(0), True)
    out["n_forecast"] = min_max_normalise(out["forecast_accuracy_pct"].fillna(0), True)
    out["n_replenishment"] = min_max_normalise(out["replenishment_efficiency"].fillna(0), True)

    out["performance_score"] = [
        performance_score({
            "sales_performance": r.n_sales,
            "stock_availability": r.n_availability,
            "inventory_efficiency": r.n_turnover,
            "forecast_accuracy": r.n_forecast,
            "replenishment_efficiency": r.n_replenishment,
        })
        for r in out.itertuples()
    ]
    out["rank"] = out["performance_score"].rank(ascending=False, method="min").astype(int)
    out["grade"] = out["performance_score"].map(score_grade)
    out["availability_flag"] = np.where(
        out["stock_availability_pct"] < 93, "Availability below target", "OK")
    out = out.sort_values("performance_score", ascending=False).reset_index(drop=True)
    return out


def score_with_custom_weights(store_ids: list[int] | None = None,
                              weights: dict[str, float] | None = None,
                              days: int = 30) -> pd.DataFrame:
    """Re-score the same KPI snapshot with reviewer-supplied weights (what-if)."""
    base = store_kpi_current(store_ids, days)
    if base.empty:
        return base
    w = {**(weights or KPI_WEIGHTS)}
    tot = sum(w.values()) or 1.0
    base["performance_score"] = [
        round((r.n_sales * w["sales_performance"]
               + r.n_availability * w["stock_availability"]
               + r.n_turnover * w["inventory_efficiency"]
               + r.n_forecast * w["forecast_accuracy"]
               + r.n_replenishment * w["replenishment_efficiency"]) / tot, 2)
        for r in base.itertuples()
    ]
    base["rank"] = base["performance_score"].rank(ascending=False, method="min").astype(int)
    base["grade"] = base["performance_score"].map(score_grade)
    return base.sort_values("performance_score", ascending=False).reset_index(drop=True)


def kpi_trend(store_ids: list[int] | None = None, days: int = 90) -> pd.DataFrame:
    sql = ["""
        SELECT k.business_date, s.store_code, s.store_name, k.sales_revenue, k.units_sold,
               k.stock_availability_pct, k.stockout_frequency, k.inventory_turnover,
               k.forecast_accuracy_pct, k.replenishment_efficiency, k.performance_score,
               k.waste_value, k.stockout_loss_value
        FROM store_kpi_daily k JOIN stores s ON s.store_id = k.store_id
        WHERE k.business_date >= date((SELECT MAX(business_date) FROM store_kpi_daily), :d)
    """]
    p: dict = {"d": f"-{int(days)} days"}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND k.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    sql.append("ORDER BY k.business_date")
    return query(" ".join(sql), p)


def regional_summary(days: int = 30) -> pd.DataFrame:
    base = store_kpi_current(days=days)
    if base.empty:
        return base
    g = base.groupby("region").agg(
        stores=("store_code", "count"),
        sales_revenue=("sales_revenue", "sum"),
        avg_availability=("stock_availability_pct", "mean"),
        avg_score=("performance_score", "mean"),
        stockout_loss=("stockout_loss_value", "sum"),
        waste=("waste_value", "sum"),
    ).reset_index()
    g["avg_availability"] = g["avg_availability"].round(2)
    g["avg_score"] = g["avg_score"].round(2)
    return g.sort_values("avg_score", ascending=False)


def weight_table() -> pd.DataFrame:
    return pd.DataFrame([
        {"KPI component": KPI_LABELS[k], "Weight": f"{KPI_WEIGHTS[k]*100:.0f}%",
         "How it is measured": d}
        for k, d in [
            ("sales_performance", "Revenue in the trailing window, min-max normalised across stores"),
            ("stock_availability", "100 − stockout frequency; share of SKU-days with product on shelf"),
            ("inventory_efficiency", "Annualised inventory turnover (COGS ÷ average inventory)"),
            ("forecast_accuracy", "100 − MAPE of a 7-day moving-average demand forecast"),
            ("replenishment_efficiency", "Share of purchase orders delivered on or before expected date"),
        ]
    ])


def needs_attention(days: int = 30, limit: int = 3) -> pd.DataFrame:
    base = store_kpi_current(days=days)
    if base.empty:
        return base
    base["risk_score"] = (
        (100 - base["stock_availability_pct"]) * 2
        + base["stockout_frequency"] * 5
        + base["waste_value"] / max(base["waste_value"].max(), 1) * 12
    )
    return base.nlargest(limit, "risk_score")[
        ["store_code", "store_name", "stock_availability_pct", "stockout_frequency",
         "stockout_loss_value", "waste_value", "performance_score", "risk_score"]]
