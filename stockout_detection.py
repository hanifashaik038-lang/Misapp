"""
Stockout detection.

Two distinct concepts, deliberately kept apart:
  * ACTUAL stockout    – available stock has hit zero (shelf is empty now).
  * POTENTIAL stockout – stock is positive but projected to run out before the
    next delivery arrives (days of cover < supplier lead time) or the 7-day
    model probability exceeds the configured threshold.

``detect_stockout`` is called synchronously after a sale so an empty shelf
raises an alert immediately; ``generate_from_predictions`` runs the batch sweep.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from config import RISK_BANDS
from database.db import execute, query, scalar
from modules import alerts as alert_svc
from modules import audit
from utils.calculations import recommended_reorder_qty
from utils.helpers import get_logger

log = get_logger("stocksense.detection")

CRITICAL_PROBABILITY = 0.80
HIGH_PROBABILITY = 0.60


def _product_context(store_id: int, sku_id: int) -> dict | None:
    df = query(
        "SELECT k.product_name, k.sku_code, k.reorder_level, k.safety_stock, k.max_stock_level, "
        "k.unit_cost, s.store_name, s.store_code, sup.supplier_name, sup.lead_time_days, "
        "soh.on_hand_qty, COALESCE(soh.reserved_qty,0) AS reserved, "
        "COALESCE(soh.in_transit_qty,0) AS in_transit "
        "FROM sku k JOIN stores s ON s.store_id = :st "
        "JOIN suppliers sup ON sup.supplier_id = k.supplier_id "
        "LEFT JOIN stock_on_hand soh ON soh.sku_id = k.sku_id AND soh.store_id = s.store_id "
        "WHERE k.sku_id = :sk", {"st": int(store_id), "sk": int(sku_id)})
    return None if df.empty else df.iloc[0].to_dict()


def _avg_daily_demand(store_id: int, sku_id: int, days: int = 14) -> float:
    v = scalar(
        "SELECT AVG(units_sold) FROM sales_daily WHERE store_id=:s AND sku_id=:k "
        "AND business_date >= date((SELECT MAX(business_date) FROM sales_daily), :d)",
        {"s": int(store_id), "k": int(sku_id), "d": f"-{int(days)} days"}, 0.0)
    return float(v or 0.0)


def detect_stockout(store_id: int, sku_id: int, user: dict | None = None) -> dict | None:
    """Evaluate one store/SKU immediately (called after a sale or adjustment)."""
    ctx = _product_context(store_id, sku_id)
    if not ctx:
        return None
    on_hand = int(ctx["on_hand_qty"] or 0)
    avail = on_hand - int(ctx["reserved"] or 0)
    avg_d = _avg_daily_demand(store_id, sku_id)
    lead = int(ctx["lead_time_days"] or 0)
    transit = int(ctx["in_transit"] or 0)
    now = datetime.now().isoformat(timespec="seconds")

    # ---- A. actual stockout ------------------------------------------------
    if avail <= 0:
        existing = query(
            "SELECT oos_id FROM stockout_event WHERE store_id=:s AND sku_id=:k "
            "AND ended_at IS NULL ORDER BY oos_id DESC LIMIT 1",
            {"s": int(store_id), "k": int(sku_id)})
        if existing.empty:
            execute(
                "INSERT INTO stockout_event (store_id, sku_id, detection_type, severity, started_at, "
                "root_cause) VALUES (:s,:k,'ACTUAL','CRITICAL',:t,:rc)",
                {"s": int(store_id), "k": int(sku_id), "t": now,
                 "rc": "Demand exceeded available stock" if transit == 0
                       else "Ran out while replenishment in transit"})
        qty = recommended_reorder_qty(max(on_hand, 0), max(avg_d, 0.5), lead,
                                      ctx["safety_stock"], ctx["max_stock_level"], transit)
        return alert_svc.create_alert(
            store_id, sku_id, "ACTUAL_STOCKOUT", "CRITICAL", 1.0, on_hand,
            f"{ctx['product_name']} is OUT OF STOCK at {ctx['store_name']} "
            f"(0 units available, avg demand {avg_d:.1f}/day).",
            f"Restock immediately – raise an emergency order for {qty} units or transfer from "
            f"a nearby store. Supplier {ctx['supplier_name']} lead time is {lead} days.",
            channels=["IN_APP", "EMAIL", "SMS"])

    # ---- B. low stock (below reorder level) --------------------------------
    if on_hand <= int(ctx["reorder_level"] or 0):
        cover = on_hand / avg_d if avg_d > 0 else 99
        severity = "HIGH" if (cover < lead or on_hand <= int(ctx["safety_stock"] or 0)) else "MODERATE"
        qty = recommended_reorder_qty(on_hand, max(avg_d, 0.5), lead, ctx["safety_stock"],
                                      ctx["max_stock_level"], transit)
        if on_hand <= int(ctx["safety_stock"] or 0):
            return alert_svc.create_alert(
                store_id, sku_id, "POTENTIAL_STOCKOUT", severity,
                None, on_hand,
                f"{ctx['product_name']} is at CRITICAL stock ({on_hand} units, safety stock "
                f"{ctx['safety_stock']}) at {ctx['store_name']} – {cover:.1f} days of cover.",
                f"Order {qty} units now; cover {cover:.1f} d vs {lead}-day lead time.",
                channels=["IN_APP", "EMAIL"])
        return alert_svc.create_alert(
            store_id, sku_id, "LOW_STOCK", severity, None, on_hand,
            f"{ctx['product_name']} has fallen to {on_hand} units at {ctx['store_name']} "
            f"(reorder level {ctx['reorder_level']}).",
            f"Include {qty} units in the next replenishment cycle.",
            channels=["IN_APP"])
    return None


def generate_from_predictions(store_ids: list[int] | None = None,
                              threshold: float = HIGH_PROBABILITY,
                              user: dict | None = None) -> dict:
    """Batch sweep: turn high/critical 7-day probabilities into alerts."""
    sql = ["""
        SELECT p.store_id, p.sku_id, p.probability, p.risk_level, p.current_stock,
               p.days_of_cover, p.lead_time_days, p.reorder_qty, p.recommended_action,
               k.product_name, s.store_name, s.store_code, sup.supplier_name
        FROM stockout_prediction p
        JOIN sku k ON k.sku_id = p.sku_id
        JOIN stores s ON s.store_id = p.store_id
        JOIN suppliers sup ON sup.supplier_id = k.supplier_id
        WHERE p.as_of_date = (SELECT MAX(as_of_date) FROM stockout_prediction)
          AND p.probability >= :th
    """]
    p: dict = {"th": float(threshold)}
    if store_ids is not None:
        if not store_ids:
            return {"created": 0, "suppressed": 0}
        sql.append("AND p.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    sql.append("ORDER BY p.probability DESC")
    rows = query(" ".join(sql), p)

    created = suppressed = 0
    for r in rows.itertuples():
        severity = "CRITICAL" if r.probability >= CRITICAL_PROBABILITY else "HIGH"
        res = alert_svc.create_alert(
            int(r.store_id), int(r.sku_id), "POTENTIAL_STOCKOUT", severity,
            float(r.probability), int(r.current_stock),
            f"{r.product_name} has a {r.probability*100:.0f}% stockout probability over the next "
            f"7 days at {r.store_name} ({r.current_stock} units, {r.days_of_cover:.1f} d cover "
            f"vs {r.lead_time_days}-day lead time).",
            r.recommended_action,
            channels=["IN_APP", "EMAIL"] if severity == "HIGH" else ["IN_APP", "EMAIL", "SMS"])
        if res and res.get("suppressed"):
            suppressed += 1
        elif res:
            created += 1

    audit.record("ALERT_SWEEP", user=user, entity="alerts",
                 detail=f"threshold={threshold:.2f} created={created} suppressed={suppressed}")
    log.info("alert sweep: %s created, %s suppressed by dedup", created, suppressed)
    return {"created": created, "suppressed": suppressed, "evaluated": int(len(rows))}


def close_resolved_stockouts(store_id: int | None = None) -> int:
    """Close ACTUAL stockout episodes once stock is back above zero."""
    where = "AND o.store_id = " + str(int(store_id)) if store_id else ""
    rows = query(f"""
        SELECT o.oos_id, o.store_id, o.sku_id, o.started_at, o.units_lost, o.revenue_lost
        FROM stockout_event o JOIN stock_on_hand s
          ON s.store_id = o.store_id AND s.sku_id = o.sku_id
        WHERE o.ended_at IS NULL AND s.on_hand_qty > 0 {where}
    """)
    n = 0
    for r in rows.itertuples():
        started = datetime.fromisoformat(str(r.started_at))
        hours = (datetime.now() - started).total_seconds() / 3600
        execute("UPDATE stockout_event SET ended_at = :e, duration_hours = :h WHERE oos_id = :o",
                {"e": datetime.now().isoformat(timespec="seconds"), "h": round(hours, 2),
                 "o": int(r.oos_id)})
        n += 1
    return n


def stockout_episodes(store_ids: list[int] | None = None, limit: int = 300) -> pd.DataFrame:
    sql = ["""
        SELECT o.oos_id, s.store_code, s.store_name, k.sku_code, k.product_name,
               c.category_name, o.detection_type, o.severity, o.started_at, o.ended_at,
               o.duration_hours, o.units_lost, o.revenue_lost, o.root_cause
        FROM stockout_event o
        JOIN stores s ON s.store_id = o.store_id
        JOIN sku k ON k.sku_id = o.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE 1=1
    """]
    p: dict = {"l": int(limit)}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND o.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    sql.append("ORDER BY o.started_at DESC LIMIT :l")
    return query(" ".join(sql), p)


def stockout_summary(store_ids: list[int] | None = None) -> dict:
    df = stockout_episodes(store_ids=store_ids, limit=100000)
    if df.empty:
        return {"episodes": 0, "active": 0, "units_lost": 0, "revenue_lost": 0.0,
                "avg_duration_h": 0.0}
    return {
        "episodes": int(len(df)),
        "active": int(df["ended_at"].isna().sum()),
        "units_lost": int(df["units_lost"].fillna(0).sum()),
        "revenue_lost": round(float(df["revenue_lost"].fillna(0).sum()), 2),
        "avg_duration_h": round(float(df["duration_hours"].fillna(0).mean()), 2),
    }


def currently_out_of_stock(store_ids: list[int] | None = None) -> pd.DataFrame:
    sql = """
        SELECT s.store_code, s.store_name, k.sku_code, k.product_name, c.category_name,
               soh.on_hand_qty, COALESCE(soh.in_transit_qty,0) AS in_transit,
               k.reorder_level
        FROM stock_on_hand soh
        JOIN stores s ON s.store_id = soh.store_id
        JOIN sku k ON k.sku_id = soh.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE soh.on_hand_qty <= 0
    """
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql += " AND soh.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
    return query(sql + " ORDER BY s.store_code, k.product_name")
