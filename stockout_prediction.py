"""
Stockout probability prediction – inference / model serving.

Reads the persisted model and scores the CURRENT state of every store x SKU
pair, writing the result to ``stockout_prediction``.

If no trained model exists the service falls back to a transparent,
clearly-labelled heuristic baseline (``prediction_source = BASELINE_HEURISTIC``)
so the UI never pretends a model has been trained.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import joblib
import numpy as np
import pandas as pd

from config import METRICS_PATH, MODEL_PATH, PREDICTION_HORIZON_DAYS
from database.db import execute, executemany, query, scalar
from ml.features import NUMERIC_FEATURES, demand_factor_from_format, window_features
from modules import audit
from utils.calculations import (expected_days_to_stockout, recommended_action,
                                recommended_reorder_qty, risk_level_from_probability)
from utils.helpers import get_logger, safe_json

log = get_logger("stocksense.predict")

MODEL_CACHE: dict = {}


# --------------------------------------------------------------------------
# Model loading
# --------------------------------------------------------------------------
def model_available() -> bool:
    return MODEL_PATH.exists()


def load_model() -> dict | None:
    """Load the persisted bundle once per process."""
    if "bundle" in MODEL_CACHE:
        return MODEL_CACHE["bundle"]
    if not MODEL_PATH.exists():
        return None
    bundle = joblib.load(MODEL_PATH)
    MODEL_CACHE["bundle"] = bundle
    return bundle


def model_info() -> dict:
    info = {"available": False, "model_name": "Baseline heuristic (no model trained)",
            "model_version": "n/a", "trained_at": None, "metrics": None,
            "feature_importance": [], "best_model": None}
    if METRICS_PATH.exists():
        try:
            m = json.loads(METRICS_PATH.read_text())
            info.update({
                "available": True,
                "model_name": m.get("best_model"),
                "model_version": m.get("model_version"),
                "trained_at": m.get("trained_at"),
                "metrics": m.get("metrics"),
                "best_model": m.get("best_model"),
                "feature_importance": m.get("feature_importance", []),
                "all_importances": m.get("all_importances", {}),
                "train_rows": m.get("train_rows"),
                "test_rows": m.get("test_rows"),
                "train_start": m.get("train_start"),
                "train_end": m.get("train_end"),
                "test_start": m.get("test_start"),
                "test_end": m.get("test_end"),
                "split_strategy": m.get("split_strategy"),
                "label_definition": m.get("label_definition"),
                "dataset_note": m.get("dataset_note"),
                "positive_rate_test": m.get("positive_rate_test"),
            })
        except Exception as exc:  # pragma: no cover
            log.warning("metrics unreadable: %s", exc)
    if MODEL_PATH.exists():
        b = load_model()
        if b:
            info["available"] = True
            info["model_name"] = b.get("model_name", info["model_name"])
            info["model_version"] = b.get("model_version")
            info["trained_at"] = info["trained_at"] or b.get("trained_at")
    return info


# --------------------------------------------------------------------------
# Baseline heuristic
# --------------------------------------------------------------------------
def baseline_probability(days_cover: float, lead_time: float, stockout_days_28: float,
                         pending_cover: float) -> float:
    """Transparent rule-based risk used ONLY when no model is present.

    Risk rises as days of cover falls below the supplier lead time; it is
    amplified by recent stockout history and softened by inbound stock.
    """
    total_cover = days_cover + pending_cover
    if total_cover <= 0:
        base = 0.98
    else:
        ratio = lead_time / max(total_cover, 0.25)
        base = 1.0 / (1.0 + np.exp(-(ratio - 1.0) * 1.6))
    history = min(0.25, stockout_days_28 / 28.0 * 0.5)
    return float(np.clip(base * 0.85 + history, 0.02, 0.99))


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def _load_series() -> pd.DataFrame:
    return query(
        "SELECT store_id, sku_id, business_date, units_sold, closing_stock, receipts_qty, "
        "pending_po_qty, promo_flag, stockout_flag, demand_unmet, demand_total "
        "FROM sales_daily ORDER BY store_id, sku_id, business_date")


def _load_meta() -> tuple[pd.DataFrame, pd.DataFrame]:
    sku = query(
        "SELECT k.sku_id, k.reorder_level, k.safety_stock, k.max_stock_level, k.selling_price, "
        "k.unit_cost, k.shelf_life_days, s.lead_time_days AS lead_time_days, "
        "s.reliability_score FROM sku k JOIN suppliers s ON s.supplier_id = k.supplier_id")
    stores = query("SELECT store_id, store_format FROM stores")
    return sku, stores


def score_all(write: bool = True, as_of: str | None = None) -> pd.DataFrame:
    """Score every store/SKU as of the latest business date (or ``as_of``)."""
    bundle = load_model()
    sales = _load_series()
    if sales.empty:
        return pd.DataFrame()
    sku_meta, store_meta = _load_series_meta()
    as_of_date = as_of or str(sales["business_date"].max())

    stock = query("SELECT store_id, sku_id, on_hand_qty, COALESCE(in_transit_qty,0) AS in_transit "
                  "FROM stock_on_hand").set_index(["store_id", "sku_id"])["on_hand_qty"].to_dict()
    transit = query("SELECT store_id, sku_id, COALESCE(in_transit_qty,0) AS it FROM stock_on_hand"
                    ).set_index(["store_id", "sku_id"])["it"].to_dict()

    rows, keys, meta_rows = [], [], []
    for (store_id, sku_id), g in sales.groupby(["store_id", "sku_id"], sort=False):
        g = g[g["business_date"] <= as_of_date]
        if len(g) < 28:
            continue
        sk = sku_meta.loc[sku_id]
        st = store_meta.loc[store_id]
        meta = {
            "reorder_level": sk["reorder_level"], "safety_stock": sk["safety_stock"],
            "supplier_lead_time": sk["lead_time_days"], "selling_price": sk["selling_price"],
            "unit_cost": sk["unit_cost"],
            "store_demand_factor": demand_factor_from_format(st["store_format"]),
            "store_format": st["store_format"],
        }
        arrays = {
            "units_sold": g["units_sold"].to_numpy(float),
            "closing_stock": g["closing_stock"].to_numpy(float),
            "receipts_qty": g["receipts_qty"].to_numpy(float),
            "pending_po_qty": g["pending_po_qty"].to_numpy(float),
            "promo_flag": g["promo_flag"].to_numpy(float),
            "stockout_flag": g["stockout_flag"].to_numpy(float),
            "demand_unmet": g["demand_unmet"].to_numpy(float),
            "demand_total": g["demand_total"].to_numpy(float),
        }
        i = len(g) - 1
        # live stock cache overrides the simulated closing stock
        cur_stock = int(stock.get((int(store_id), int(sku_id)), arrays["closing_stock"][i]))
        arrays["closing_stock"][i] = float(cur_stock)
        arrays["pending_po_qty"][i] = float(transit.get((int(store_id), int(sku_id)),
                                                        arrays["pending_po_qty"][i]))
        feats = window_features(arrays, i, meta,
                                date.fromisoformat(as_of_date) + timedelta(days=1))
        rows.append([feats[f] for f in NUMERIC_FEATURES])
        keys.append({"store_id": int(store_id), "sku_id": int(sku_id), "current_stock": cur_stock})
        meta_rows.append((feats, meta, sk))

    if not rows:
        return pd.DataFrame()

    X = np.asarray(rows, dtype=float)
    if bundle is not None:
        proba = bundle["model"].predict_proba(X)[:, 1]
        source = "TRAINED_MODEL"
        model_name = bundle.get("model_name", "Gradient Boosting")
        model_version = bundle.get("model_version", "v1.0")
    else:
        proba = np.array([baseline_probability(
            r[0]["days_of_cover"], r[1]["supplier_lead_time"],
            r[0]["stockout_days_28"], r[0]["pending_po_cover_days"]) for r in
            [(m[0], m[1], m[2]) for m in meta_rows]])
        source = "BASELINE_HEURISTIC"
        model_name = "Baseline heuristic (no model trained)"
        model_version = "n/a"

    out = []
    for k, (feats, meta, sk), p in zip(keys, meta_rows, proba):
        p = float(p)
        avg_d = max(feats["avg_demand_7"], 0.01)
        cover = feats["current_stock"] / avg_d
        lead = int(meta["supplier_lead_time"])
        pending = feats["pending_po_qty"]
        reorder_qty = recommended_reorder_qty(
            feats["current_stock"], avg_d, lead, meta["safety_stock"], sk["max_stock_level"], pending)
        risk = risk_level_from_probability(p)
        out.append({
            "as_of_date": as_of_date, "store_id": k["store_id"], "sku_id": k["sku_id"],
            "horizon_days": PREDICTION_HORIZON_DAYS, "probability": round(p, 4),
            "risk_level": risk, "model_name": model_name, "model_version": model_version,
            "prediction_source": source, "current_stock": int(feats["current_stock"]),
            "avg_daily_demand": round(avg_d, 2), "demand_std": round(feats["demand_std_28"], 2),
            "days_of_cover": round(float(cover), 2), "lead_time_days": lead,
            "pending_po_qty": int(pending),
            "expected_days_to_oos": round(expected_days_to_stockout(feats["current_stock"], avg_d), 2),
            "recommended_action": recommended_action(
                p, feats["current_stock"], cover, lead, pending, reorder_qty),
            "reorder_qty": int(reorder_qty),
            "top_drivers": safe_json(_drivers(feats, p)),
        })
    df = pd.DataFrame(out)

    if write and not df.empty:
        execute("DELETE FROM stockout_prediction WHERE as_of_date = :d", {"d": as_of_date})
        executemany(
            "INSERT INTO stockout_prediction (as_of_date, store_id, sku_id, horizon_days, "
            "probability, risk_level, model_name, model_version, prediction_source, current_stock, "
            "avg_daily_demand, demand_std, days_of_cover, lead_time_days, pending_po_qty, "
            "expected_days_to_oos, recommended_action, reorder_qty, top_drivers) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["as_of_date"], r["store_id"], r["sku_id"], r["horizon_days"], r["probability"],
              r["risk_level"], r["model_name"], r["model_version"], r["prediction_source"],
              r["current_stock"], r["avg_daily_demand"], r["demand_std"], r["days_of_cover"],
              r["lead_time_days"], r["pending_po_qty"], r["expected_days_to_oos"],
              r["recommended_action"], r["reorder_qty"], r["top_drivers"])
             for r in df.to_dict("records")],
        )
        log.info("scored %s store-SKU pairs with %s", f"{len(df):,}", source)
        audit.record("PREDICTIONS_GENERATED", entity="stockout_prediction",
                     detail=f"{len(df)} rows, source={source}, as_of={as_of_date}")
    return df


def _load_series_meta() -> tuple[pd.DataFrame, pd.DataFrame]:
    sku = query(
        "SELECT k.sku_id, k.reorder_level, k.safety_stock, k.max_stock_level, k.selling_price, "
        "k.unit_cost, s.lead_time_days FROM sku k JOIN suppliers s ON s.supplier_id = k.supplier_id"
    ).set_index("sku_id")
    stores = query("SELECT store_id, store_format FROM stores").set_index("store_id")
    return sku, stores


def _drivers(feats: dict, p: float) -> list[dict]:
    """Explainable, headline-level reason codes for one prediction."""
    d = []
    cover = feats["days_of_cover"]
    lead = feats["supplier_lead_time"]
    if cover < lead:
        d.append({"driver": "Days of cover below supplier lead time",
                  "detail": f"{cover:.1f} d cover vs {lead:.0f} d lead time", "impact": "increases risk"})
    if feats["stock_vs_safety_ratio"] < 1:
        d.append({"driver": "Stock below safety stock",
                  "detail": f"{feats['stock_vs_safety_ratio']:.2f}× safety stock", "impact": "increases risk"})
    if feats["trend_ratio_7_28"] > 1.15:
        d.append({"driver": "Accelerating demand",
                  "detail": f"7-day demand is {feats['trend_ratio_7_28']:.2f}× the 28-day average",
                  "impact": "increases risk"})
    if feats["promo_days_7"] > 0:
        d.append({"driver": "Promotion in the last 7 days",
                  "detail": f"{int(feats['promo_days_7'])} promotional days", "impact": "increases risk"})
    if feats["stockout_days_28"] > 2:
        d.append({"driver": "Repeat stockouts",
                  "detail": f"{int(feats['stockout_days_28'])} stockout days in the last 28",
                  "impact": "increases risk"})
    if feats["pending_po_cover_days"] > 0:
        d.append({"driver": "Inbound purchase order",
                  "detail": f"{feats['pending_po_cover_days']:.1f} days of cover in transit",
                  "impact": "reduces risk"})
    if feats["demand_cv_28"] > 0.5:
        d.append({"driver": "Volatile demand",
                  "detail": f"coefficient of variation {feats['demand_cv_28']:.2f}",
                  "impact": "increases risk"})
    if not d:
        d.append({"driver": "Healthy stock position",
                  "detail": f"{cover:.1f} days of cover against a {lead:.0f}-day lead time",
                  "impact": "reduces risk"})
    return d


# --------------------------------------------------------------------------
# Reads used by the UI
# --------------------------------------------------------------------------
def predictions(store_ids: list[int] | None = None, categories: list[str] | None = None,
                risk_levels: list[str] | None = None, search: str = "",
                min_probability: float = 0.0, limit: int = 2000) -> pd.DataFrame:
    sql = ["""
        SELECT p.prediction_id, p.as_of_date, p.store_id, p.sku_id, s.store_code, s.store_name,
               s.region, k.sku_code, k.product_name, c.category_name,
               p.current_stock, p.avg_daily_demand, p.probability, p.risk_level,
               p.expected_days_to_oos, p.days_of_cover, p.lead_time_days, p.pending_po_qty,
               p.recommended_action, p.reorder_qty, p.model_name, p.prediction_source,
               p.top_drivers, k.selling_price, k.unit_cost
        FROM stockout_prediction p
        JOIN stores s ON s.store_id = p.store_id
        JOIN sku k ON k.sku_id = p.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        WHERE p.as_of_date = (SELECT MAX(as_of_date) FROM stockout_prediction)
    """]
    p: dict = {"l": int(limit)}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND p.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    if categories:
        sql.append("AND c.category_name IN (" + ",".join(f"'{c}'" for c in categories) + ")")
    if risk_levels:
        sql.append("AND p.risk_level IN (" + ",".join(f"'{r}'" for r in risk_levels) + ")")
    if search:
        sql.append("AND (LOWER(k.product_name) LIKE :q OR LOWER(k.sku_code) LIKE :q "
                   "OR k.barcode LIKE :q)")
        p["q"] = f"%{search.strip().lower()}%"
    if min_probability > 0:
        sql.append("AND p.probability >= :mp")
        p["mp"] = float(min_probability)
    sql.append("ORDER BY p.probability DESC LIMIT :l")
    return query(" ".join(sql), p)


def prediction_summary(store_ids: list[int] | None = None) -> dict:
    df = predictions(store_ids=store_ids)
    if df.empty:
        return {"total": 0, "critical": 0, "high": 0, "moderate": 0, "low": 0,
                "avg_probability": 0.0, "at_risk": 0, "source": "n/a", "as_of": None}
    return {
        "total": int(len(df)),
        "critical": int((df["risk_level"] == "Critical Risk").sum()),
        "high": int((df["risk_level"] == "High Risk").sum()),
        "moderate": int((df["risk_level"] == "Moderate Risk").sum()),
        "low": int((df["risk_level"] == "Low Risk").sum()),
        "avg_probability": round(float(df["probability"].mean()) * 100, 1),
        "at_risk": int((df["probability"] >= 0.60).sum()),
        "source": df["prediction_source"].iloc[0],
        "as_of": str(df["as_of_date"].iloc[0]),
        "model_name": df["model_name"].iloc[0],
    }


def risk_distribution(store_ids: list[int] | None = None) -> pd.DataFrame:
    df = predictions(store_ids=store_ids)
    if df.empty:
        return pd.DataFrame(columns=["risk_level", "count"])
    order = ["Low Risk", "Moderate Risk", "High Risk", "Critical Risk"]
    g = df.groupby("risk_level").size().reindex(order, fill_value=0).reset_index(name="count")
    return g


def sku_history(store_id: int, sku_id: int, days: int = 120) -> pd.DataFrame:
    return query(
        "SELECT business_date, units_sold, demand_unmet, closing_stock, receipts_qty, "
        "stockout_flag, promo_flag FROM sales_daily WHERE store_id = :s AND sku_id = :k "
        "AND business_date >= date((SELECT MAX(business_date) FROM sales_daily), :d) "
        "ORDER BY business_date",
        {"s": int(store_id), "k": int(sku_id), "d": f"-{int(days)} days"})


def single_prediction(store_id: int, sku_id: int) -> dict | None:
    df = query(
        "SELECT p.*, k.product_name, k.sku_code, k.barcode, k.reorder_level, k.safety_stock, "
        "k.selling_price, k.unit_cost, s.store_code, s.store_name, sup.supplier_name, "
        "sup.lead_time_days, c.category_name "
        "FROM stockout_prediction p JOIN sku k ON k.sku_id = p.sku_id "
        "JOIN stores s ON s.store_id = p.store_id "
        "JOIN suppliers sup ON sup.supplier_id = k.supplier_id "
        "JOIN product_categories c ON c.category_id = k.category_id "
        "WHERE p.store_id = :s AND p.sku_id = :k "
        "AND p.as_of_date = (SELECT MAX(as_of_date) FROM stockout_prediction)",
        {"s": int(store_id), "k": int(sku_id)})
    return None if df.empty else df.iloc[0].to_dict()
