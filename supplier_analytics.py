"""Supplier performance analytics: lead time, OTIF, fulfilment, stockout blame."""
from __future__ import annotations

import pandas as pd

from database.db import query
from utils.calculations import min_max_normalise
from utils.helpers import get_logger

log = get_logger("stocksense.supplier")


def supplier_scorecard() -> pd.DataFrame:
    df = query("""
        SELECT sup.supplier_id, sup.supplier_code, sup.supplier_name, sup.city,
               sup.lead_time_days AS contracted_lead_time,
               sp.orders_count, sp.avg_lead_time_days, sp.on_time_deliveries, sp.late_deliveries,
               sp.on_time_pct, sp.fulfilment_pct, sp.qty_discrepancy_units,
               sp.stockout_contribution, sp.otif_score,
               (SELECT COUNT(*) FROM sku k WHERE k.supplier_id = sup.supplier_id) AS skus_supplied
        FROM suppliers sup
        LEFT JOIN supplier_performance sp ON sp.supplier_id = sup.supplier_id
        ORDER BY sp.on_time_pct DESC
    """)
    if df.empty:
        return df
    df["lead_time_gap"] = (df["avg_lead_time_days"] - df["contracted_lead_time"]).round(2)
    df["n_on_time"] = min_max_normalise(df["on_time_pct"].fillna(0), True)
    df["n_fulfil"] = min_max_normalise(df["fulfilment_pct"].fillna(0), True)
    df["n_lead"] = min_max_normalise(df["avg_lead_time_days"].fillna(99), False)
    df["n_stockout"] = min_max_normalise(df["stockout_contribution"].fillna(0), False)
    df["supplier_score"] = (
        df["n_on_time"] * 0.35 + df["n_fulfil"] * 0.30
        + df["n_lead"] * 0.20 + df["n_stockout"] * 0.15
    ).round(2)
    df["rating"] = pd.cut(df["supplier_score"], [-1, 45, 65, 80, 101],
                          labels=["Needs Attention", "Watch", "Good", "Preferred"])
    return df.sort_values("supplier_score", ascending=False).reset_index(drop=True)


def supplier_detail(supplier_id: int) -> dict:
    sup = query("SELECT * FROM suppliers WHERE supplier_id = :s", {"s": int(supplier_id)})
    if sup.empty:
        return {}
    po = query("""
        SELECT po.po_number, s.store_code, po.status, po.order_date, po.expected_date,
               po.actual_delivery_date,
               CAST(julianday(COALESCE(po.actual_delivery_date, 'now')) - julianday(po.expected_date)
                    AS INTEGER) AS days_variance, po.total_value
        FROM purchase_orders po JOIN stores s ON s.store_id = po.store_id
        WHERE po.supplier_id = :s ORDER BY po.order_date DESC LIMIT 60
    """, {"s": int(supplier_id)})
    oos = query("""
        SELECT s.store_code, k.sku_code, k.product_name, o.started_at, o.duration_hours,
               o.units_lost, o.revenue_lost
        FROM stockout_event o JOIN sku k ON k.sku_id = o.sku_id
        JOIN stores s ON s.store_id = o.store_id
        WHERE k.supplier_id = :s ORDER BY o.started_at DESC LIMIT 40
    """, {"s": int(supplier_id)})
    return {"supplier": sup.iloc[0].to_dict(), "purchase_orders": po, "stockouts": oos}


def supplier_stockout_contribution() -> pd.DataFrame:
    return query("""
        SELECT sup.supplier_name, SUM(sp.stockout_contribution) AS stockout_episodes,
               SUM(sp.orders_count) AS orders,
               ROUND(AVG(sp.on_time_pct), 2) AS on_time_pct,
               ROUND(AVG(sp.fulfilment_pct), 2) AS fulfilment_pct
        FROM suppliers sup JOIN supplier_performance sp ON sp.supplier_id = sup.supplier_id
        GROUP BY sup.supplier_id ORDER BY stockout_episodes DESC
    """)


def lead_time_vs_actual() -> pd.DataFrame:
    return query("""
        SELECT sup.supplier_name, sup.lead_time_days AS contracted,
               ROUND(AVG(sp.avg_lead_time_days), 2) AS actual,
               ROUND(AVG(sp.avg_lead_time_days) - sup.lead_time_days, 2) AS gap
        FROM suppliers sup JOIN supplier_performance sp ON sp.supplier_id = sup.supplier_id
        GROUP BY sup.supplier_id ORDER BY gap DESC
    """)
