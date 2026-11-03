"""
StockSense AI – stockout probability model training pipeline.

Trains three candidate classifiers on SIMULATED FreshMart history using a
strictly chronological split, evaluates them on a held-out future window, and
persists the best model (by F1, tie-broken on ROC-AUC) with joblib.

Run:
    python -m ml.train_model
    python -m ml.train_model --skip-registry

Design notes
------------
* No shuffling. Train on the first ~80% of as-of dates, test on the final ~20%,
  plus a 7-day purge gap so no training label can overlap the test period.
* Features come from ``ml.features.window_features`` – the same function used at
  inference time.
* Metrics are written to ``ml/saved_model/metrics.json`` AND to the
  ``ml_model_registry`` table so the UI can prove what was trained when.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, confusion_matrix, f1_score,
                             precision_score, recall_score, roc_auc_score, roc_curve)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from config import METRICS_PATH, MODEL_DIR, MODEL_PATH, PREDICTION_HORIZON_DAYS
from database.db import execute, query
from ml.features import NUMERIC_FEATURES, build_training_frame, label_rate
from utils.helpers import get_logger, safe_json

log = get_logger("stocksense.train")

MODEL_VERSION = "v1.0"


def load_training_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    sales = query(
        "SELECT store_id, sku_id, business_date, units_sold, closing_stock, receipts_qty, "
        "pending_po_qty, promo_flag, stockout_flag, demand_unmet, demand_total "
        "FROM sales_daily ORDER BY store_id, sku_id, business_date"
    )
    sku = query(
        "SELECT k.sku_id, k.reorder_level, k.safety_stock, k.selling_price, k.unit_cost, "
        "s.lead_time_days FROM sku k JOIN suppliers s ON s.supplier_id = k.supplier_id"
    )
    stores = query(
        "SELECT store_id, store_format, "
        "CASE store_format WHEN 'URBAN' THEN 1.50 WHEN 'MALL' THEN 1.60 "
        "WHEN 'RESIDENTIAL' THEN 1.15 ELSE 0.95 END AS demand_factor "
        "FROM stores"
    )
    return sales, sku, stores


def chronological_split(key_df: pd.DataFrame, test_frac: float = 0.20,
                        purge_days: int = PREDICTION_HORIZON_DAYS) -> tuple[np.ndarray, np.ndarray]:
    """Time-aware split: earliest dates train, latest dates test, purge gap between."""
    dates = pd.to_datetime(key_df["as_of_date"])
    uniq = np.sort(dates.unique())
    cut = int(len(uniq) * (1 - test_frac))
    train_end = pd.Timestamp(uniq[cut - 1])
    test_start = pd.Timestamp(uniq[cut]) + pd.Timedelta(days=purge_days)
    train_mask = (dates <= train_end).to_numpy()
    test_mask = (dates >= test_start).to_numpy()
    return train_mask, test_mask


def build_candidates() -> dict[str, Pipeline]:
    return {
        "Logistic Regression": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=2000, C=0.6, class_weight="balanced",
                                       random_state=42)),
        ]),
        "Random Forest": Pipeline([
            ("clf", RandomForestClassifier(
                n_estimators=320, max_depth=14, min_samples_leaf=8,
                max_features="sqrt", class_weight="balanced_subsample",
                n_jobs=-1, random_state=42)),
        ]),
        "Gradient Boosting": Pipeline([
            ("clf", GradientBoostingClassifier(
                n_estimators=260, learning_rate=0.06, max_depth=4,
                min_samples_leaf=12, subsample=0.85, random_state=42)),
        ]),
    }


def evaluate(model, X_test, y_test, X_train, y_train) -> dict:
    proba = model.predict_proba(X_test)[:, 1]
    pred = (proba >= 0.5).astype(int)
    cm = confusion_matrix(y_test, pred, labels=[0, 1])
    auc = roc_auc_score(y_test, proba) if y_test.nunique() > 1 else float("nan")
    fpr, tpr, _ = roc_curve(y_test, proba) if y_test.nunique() > 1 else ([0, 1], [0, 1], None)
    train_acc = accuracy_score(y_train, model.predict(X_train))
    return {
        "accuracy": round(float(accuracy_score(y_test, pred)), 4),
        "precision": round(float(precision_score(y_test, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y_test, pred, zero_division=0)), 4),
        "f1": round(float(f1_score(y_test, pred, zero_division=0)), 4),
        "roc_auc": round(float(auc), 4) if auc == auc else None,
        "train_accuracy": round(float(train_acc), 4),
        "confusion_matrix": cm.tolist(),
        "roc_curve": {
            "fpr": [round(float(x), 4) for x in fpr],
            "tpr": [round(float(x), 4) for x in tpr],
        },
        "positive_prediction_rate": round(float(pred.mean()), 4),
    }


def feature_importance(model, feature_names: list[str]) -> list[dict]:
    est = model.named_steps["clf"]
    if hasattr(est, "feature_importances_"):
        vals = est.feature_importances_
    elif hasattr(est, "coef_"):
        vals = np.abs(est.coef_[0])
    else:
        return []
    pair = sorted(zip(feature_names, vals), key=lambda t: t[1], reverse=True)
    total = sum(v for _, v in pair) or 1.0
    return [{"feature": f, "importance": round(float(v), 4),
             "share_pct": round(float(v) / total * 100, 2)} for f, v in pair]


def train(test_frac: float = 0.20, write_registry: bool = True) -> dict:
    log.info("loading training inputs from the database …")
    sales, sku, stores = load_training_inputs()
    log.info("  %s daily fact rows", f"{len(sales):,}")

    X, y, key_df = build_training_frame(sales, sku, stores)
    log.info("  %s labelled feature rows (positive rate %.2f%%)",
             f"{len(X):,}", label_rate(y) * 100)
    if len(X) < 1000:
        raise RuntimeError("Not enough training rows – run the seed script first.")

    train_mask, test_mask = chronological_split(key_df, test_frac)
    X_train, y_train = X.iloc[train_mask][NUMERIC_FEATURES], y.iloc[train_mask]
    X_test, y_test = X.iloc[test_mask][NUMERIC_FEATURES], y.iloc[test_mask]
    log.info("  train=%s rows (%s → %s)", f"{len(X_train):,}",
             key_df.iloc[train_mask]["as_of_date"].min(), key_df.iloc[train_mask]["as_of_date"].max())
    log.info("  test =%s rows (%s → %s)", f"{len(X_test):,}",
             key_df.iloc[test_mask]["as_of_date"].min(), key_df.iloc[test_mask]["as_of_date"].max())

    results: dict[str, dict] = {}
    fitted: dict[str, Pipeline] = {}
    for name, model in build_candidates().items():
        log.info("training %s …", name)
        model.fit(X_train, y_train)
        fitted[name] = model
        results[name] = evaluate(model, X_test, y_test, X_train, y_train)
        r = results[name]
        log.info("  %-20s acc=%.3f prec=%.3f rec=%.3f f1=%.3f auc=%s",
                 name, r["accuracy"], r["precision"], r["recall"], r["f1"],
                 f"{r['roc_auc']:.3f}" if r["roc_auc"] else "n/a")

    best_name = max(results, key=lambda k: (results[k]["f1"], results[k]["roc_auc"] or 0))
    best = fitted[best_name]
    log.info("selected model: %s", best_name)

    imp = feature_importance(best, NUMERIC_FEATURES)
    payload = {
        "model_name": best_name,
        "model_version": MODEL_VERSION,
        "trained_at": datetime.utcnow().isoformat(),
        "horizon_days": PREDICTION_HORIZON_DAYS,
        "feature_names": NUMERIC_FEATURES,
        "n_features": len(NUMERIC_FEATURES),
        "rows_total": int(len(X)),
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "train_start": key_df.iloc[train_mask]["as_of_date"].min(),
        "train_end": key_df.iloc[train_mask]["as_of_date"].max(),
        "test_start": key_df.iloc[test_mask]["as_of_date"].min(),
        "test_end": key_df.iloc[test_mask]["as_of_date"].max(),
        "label_definition": "1 if the SKU recorded unmet demand (lost sales) on any day "
                            "within the next 7 days",
        "dataset_note": "Trained on SIMULATED FreshMart Supermarket data – not real operations.",
        "positive_rate_test": round(float(y_test.mean()), 4),
        "positive_rate_train": round(float(y_train.mean()), 4),
        "metrics": results,
        "best_model": best_name,
        "feature_importance": feature_importance(best, NUMERIC_FEATURES),
        "all_importances": {n: feature_importance(m, NUMERIC_FEATURES) for n, m in fitted.items()},
        "split_strategy": f"chronological {int((1-test_frac)*100)}/{int(test_frac*100)} "
                          f"with a {PREDICTION_HORIZON_DAYS}-day purge gap, no shuffling",
    }

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "model": best,
            "feature_names": NUMERIC_FEATURES,
            "model_name": best_name,
            "model_version": MODEL_VERSION,
            "trained_at": payload["trained_at"],
            "horizon_days": PREDICTION_HORIZON_DAYS,
        },
        MODEL_PATH,
        compress=3,
    )
    METRICS_PATH.write_text(json.dumps(payload, indent=2))
    log.info("model saved → %s", MODEL_PATH)
    log.info("metrics saved → %s", METRICS_PATH)

    if write_registry:
        execute("UPDATE ml_model_registry SET is_active = 0")
        for name, r in results.items():
            execute(
                "INSERT INTO ml_model_registry (model_name, model_version, algorithm, trained_at, "
                "training_rows, test_rows, train_start, train_end, test_start, test_end, accuracy, "
                "precision_score, recall_score, f1_score, roc_auc, is_active, notes) "
                "VALUES (:mn,:mv,:al,:ta,:tr,:te,:ts,:ten,:tst,:ten2,:acc,:pr,:rc,:f1,:auc,:act,:nt)",
                {
                    "mn": f"Stockout 7-Day – {name}", "mv": MODEL_VERSION, "al": name,
                    "ta": payload["trained_at"], "tr": payload["train_rows"],
                    "te": payload["test_rows"], "ts": payload["train_start"],
                    "ten": payload["train_end"], "tst": payload["test_start"],
                    "ten2": payload["test_end"], "acc": r["accuracy"], "pr": r["precision"],
                    "rc": r["recall"], "f1": r["f1"], "auc": r["roc_auc"],
                    "act": 1 if name == best_name else 0,
                    "nt": "SIMULATED data | chronological split | "
                          + ("SELECTED for scoring" if name == best_name else "candidate"),
                },
            )
        log.info("model registry updated")

    return payload


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-registry", action="store_true")
    a = ap.parse_args()
    out = train(write_registry=not a.no_registry)
    print(json.dumps({k: out[k] for k in
                      ("best_model", "train_rows", "test_rows", "metrics")}, indent=2)[:2500])
