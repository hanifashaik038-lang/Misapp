"""
StockSense AI – shared feature engineering.

THE SINGLE SOURCE OF TRUTH for the 7-day stockout model.

Both the offline training pipeline (``ml/train_model.py``) and online scoring
(``modules/stockout_prediction.py``) call :func:`window_features` with the same
numpy arrays. There is therefore no possibility of train/serve skew.

Leakage rules honoured here
---------------------------
* Every feature for an *as-of* day ``t`` uses data from ``t`` and earlier only.
* The label looks strictly at ``t+1 … t+H``.
* The train/test split is chronological – no shuffling, no random rows leaking
  future behaviour into training.
* ``demand_unmet`` (lost sales, only knowable ex-post) is used to build the
  LABEL, never as a feature. Features use observed ``units_sold`` only.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from config import PREDICTION_HORIZON_DAYS

# Numeric features (order is fixed – the saved model depends on it).
NUMERIC_FEATURES = [
    "current_stock",
    "avg_demand_7",
    "avg_demand_14",
    "avg_demand_28",
    "demand_std_28",
    "demand_cv_28",
    "trend_ratio_7_28",
    "days_of_cover",
    "stock_to_reorder_ratio",
    "stock_vs_safety_ratio",
    "pending_po_qty",
    "pending_po_cover_days",
    "receipts_qty_14",
    "promo_days_7",
    "stockout_days_28",
    "unmet_ratio_28",
    "supplier_lead_time",
    "reorder_level",
    "safety_stock",
    "dow_next",
    "is_weekend_next",
    "season_sin",
    "season_cos",
    "store_demand_factor",
    "store_format_code",
    "sku_price",
    "sku_margin_pct",
]

MIN_HISTORY = 28  # days of history required before a feature row is emitted


def store_format_code(fmt: str) -> int:
    return {"URBAN": 3, "MALL": 4, "RESIDENTIAL": 2, "SUBURBAN": 1}.get(str(fmt).upper(), 0)


def demand_factor_from_format(fmt: str) -> float:
    """Mirror of the demand multipliers used by the simulator."""
    return {"URBAN": 1.50, "MALL": 1.60, "RESIDENTIAL": 1.15,
            "SUBURBAN": 0.95}.get(str(fmt).upper(), 1.0)


def window_features(arrays: dict[str, np.ndarray], i: int, meta: dict, next_date: date) -> dict:
    """Build one feature row from history ending at index ``i`` (inclusive).

    ``arrays`` holds numpy arrays for the same (store, sku) series:
    units_sold, closing_stock, receipts_qty, pending_po_qty, promo_flag,
    stockout_flag, demand_unmet, demand_total.
    """
    def win(key, n):
        lo = max(0, i - n + 1)
        return arrays[key][lo:i + 1]

    sold7, sold14, sold28 = win("units_sold", 7), win("units_sold", 14), win("units_sold", 28)
    avg7 = float(sold7.mean()) if sold7.size else 0.0
    avg14 = float(sold14.mean()) if sold14.size else 0.0
    avg28 = float(sold28.mean()) if sold28.size else 0.0
    std28 = float(sold28.std(ddof=1)) if sold28.size > 1 else 0.0

    stock = float(arrays["closing_stock"][i])
    reorder = max(1.0, float(meta["reorder_level"]))
    safety = max(1.0, float(meta["safety_stock"]))
    lead = float(meta["supplier_lead_time"])
    pending = float(arrays["pending_po_qty"][i])
    receipts14 = float(win("receipts_qty", 14).sum())
    promo7 = float(win("promo_flag", 7).sum())
    oos28 = float(win("stockout_flag", 28).sum())
    unmet28 = float(win("demand_unmet", 28).sum())
    demand28 = float(win("demand_total", 28).sum())

    epsilon = 1e-6
    cover = stock / max(avg7, epsilon) if avg7 > 0 else 60.0
    cover = min(cover, 60.0)

    doy = next_date.timetuple().tm_yday
    return {
        "current_stock": stock,
        "avg_demand_7": round(avg7, 3),
        "avg_demand_14": round(avg14, 3),
        "avg_demand_28": round(avg28, 3),
        "demand_std_28": round(std28, 3),
        "demand_cv_28": round(std28 / max(avg28, epsilon), 3),
        "trend_ratio_7_28": round(avg7 / max(avg28, epsilon), 3),
        "days_of_cover": round(cover, 3),
        "stock_to_reorder_ratio": round(stock / reorder, 3),
        "stock_vs_safety_ratio": round(stock / safety, 3),
        "pending_po_qty": pending,
        "pending_po_cover_days": round(pending / max(avg7, epsilon), 3),
        "receipts_qty_14": receipts14,
        "promo_days_7": promo7,
        "stockout_days_28": oos28,
        "unmet_ratio_28": round(unmet28 / max(demand28, epsilon), 4),
        "supplier_lead_time": lead,
        "reorder_level": float(meta["reorder_level"]),
        "safety_stock": float(meta["safety_stock"]),
        "dow_next": float(next_date.weekday()),
        "is_weekend_next": 1.0 if next_date.weekday() >= 5 else 0.0,
        "season_sin": round(math.sin(2 * math.pi * doy / 365.25), 4),
        "season_cos": round(math.cos(2 * math.pi * doy / 365.25), 4),
        "store_demand_factor": float(meta["store_demand_factor"]),
        "store_format_code": float(store_format_code(meta["store_format"])),
        "sku_price": float(meta["selling_price"]),
        "sku_margin_pct": round(
            (meta["selling_price"] - meta["unit_cost"]) / max(meta["selling_price"], epsilon) * 100, 2),
    }


def _arrays_from_frame(g: pd.DataFrame) -> dict[str, np.ndarray]:
    return {
        "units_sold": g["units_sold"].to_numpy(dtype=float),
        "closing_stock": g["closing_stock"].to_numpy(dtype=float),
        "receipts_qty": g["receipts_qty"].to_numpy(dtype=float),
        "pending_po_qty": g["pending_po_qty"].to_numpy(dtype=float),
        "promo_flag": g["promo_flag"].to_numpy(dtype=float),
        "stockout_flag": g["stockout_flag"].to_numpy(dtype=float),
        "demand_unmet": g["demand_unmet"].to_numpy(dtype=float),
        "demand_total": g["demand_total"].to_numpy(dtype=float),
    }


def build_training_frame(
    sales: pd.DataFrame,
    sku_meta: pd.DataFrame,
    store_meta: pd.DataFrame,
    horizon: int = PREDICTION_HORIZON_DAYS,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Expand the daily fact table into a supervised learning problem.

    Returns ``(X, y, keys)`` where ``keys`` records store/sku/as_of for audit.
    Target: ``1`` if the SKU recorded unmet demand (i.e. genuinely could not
    serve a customer) on ANY day in ``t+1 … t+H``.
    """
    sku_meta = sku_meta.set_index("sku_id")
    store_meta = store_meta.set_index("store_id")

    rows, labels, keys = [], [], []
    grouped = sales.sort_values("business_date").groupby(["store_id", "sku_id"], sort=False)

    for (store_id, sku_id), g in grouped:
        if len(g) < MIN_HISTORY + horizon + 5:
            continue
        arr = _arrays_from_frame(g)
        dates = [date.fromisoformat(d) for d in g["business_date"]]
        sk = sku_meta.loc[sku_id]
        st = store_meta.loc[store_id]
        meta = {
            "reorder_level": sk["reorder_level"],
            "safety_stock": sk["safety_stock"],
            "supplier_lead_time": sk["lead_time_days"],
            "selling_price": sk["selling_price"],
            "unit_cost": sk["unit_cost"],
            "store_demand_factor": st["demand_factor"],
            "store_format": st["store_format"],
        }
        unmet = arr["demand_unmet"]

        for i in range(MIN_HISTORY - 1, len(g) - horizon):
            fut = unmet[i + 1:i + 1 + horizon]
            y = 1 if fut.sum() > 0 else 0
            feats = window_features(arr, i, meta, dates[i] + timedelta(days=1))
            rows.append(feats)
            labels.append(y)
            keys.append({
                "store_id": int(store_id), "sku_id": int(sku_id),
                "as_of_date": dates[i].isoformat(), "business_date": dates[i].isoformat(),
            })

    X = pd.DataFrame(rows)
    y = pd.Series(labels, name="label")
    key_df = pd.DataFrame(keys)
    return X, y, key_df


def label_rate(y: pd.Series) -> float:
    return float(y.mean()) if len(y) else 0.0


def risk_band(p: float) -> str:
    if p <= 0.30:
        return "Low Risk"
    if p <= 0.60:
        return "Moderate Risk"
    if p <= 0.80:
        return "High Risk"
    return "Critical Risk"
