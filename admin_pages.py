"""User management and system settings (head-office only)."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from config import (ALERT_DEDUP_WINDOW_HOURS, ALERT_DELIVERY_TARGET_MINUTES,
                    COLORS, DEMO_USERS, KPI_WEIGHTS, PREDICTION_HORIZON_DAYS,
                    RISK_BANDS, SCAN_RESPONSE_TARGET_MS, SYSTEM_VERSION)
from database.db import execute, query, scalar, table_counts
from modules import audit
from modules import authentication as auth
from modules import inventory as inv
from modules import stockout_prediction as pred
from ui import components as c
from ui.theme import demo_banner, page_header, section


# =========================================================================
def render_users(user: dict, store_ids, stores_df):
    page_header("User Management", "Accounts, roles and access scope.",
                eyebrow="Administration")
    demo_banner()

    users = auth.list_users()
    c.kpi_row([
        {"label": "Accounts", "value": f"{len(users):,}", "sub": "all roles"},
        {"label": "Active", "value": f"{int(users['is_active'].sum()):,}",
         "sub": "can sign in", "color": COLORS["primary"]},
        {"label": "Roles", "value": "4", "sub": "RBAC tiers"},
        {"label": "Store-scoped",
         "value": f"{int(users['store_code'].notna().sum()):,}",
         "sub": "locked to one store", "color": COLORS["accent"]},
    ])

    section("Accounts", "")
    view = users.copy()
    view["is_active"] = view["is_active"].map({1: "Yes", 0: "No"})
    view["last_login_at"] = view["last_login_at"].astype(str).str[:19]
    c.table(view.rename(columns={
        "user_id": "ID", "username": "Username", "full_name": "Full name", "email": "Email",
        "role_code": "Role", "role_name": "Role name", "store_code": "Store",
        "is_active": "Active", "last_login_at": "Last login"}), height=300)

    section("Create account", "")
    with st.form("new_user"):
        a1, a2, a3 = st.columns(3, gap="small")
        with a1:
            username = st.text_input("Username")
            full_name = st.text_input("Full name")
        with a2:
            password = st.text_input("Password", type="password",
                                     help="Minimum 8 characters; hashed with PBKDF2-SHA256.")
            email = st.text_input("Email")
        with a3:
            role = st.selectbox("Role", ["STORE_EMPLOYEE", "STORE_MANAGER",
                                        "REGIONAL_MANAGER", "HEAD_OFFICE_ADMIN"])
            store_choice = st.selectbox(
                "Store", ["(chain level)"] + [f"{r.store_code} · {r.store_name}"
                                              for r in stores_df.itertuples()])
        submitted = st.form_submit_button("Create user", type="primary")
    if submitted:
        if store_choice == "(chain level)":
            store_id = None
        else:
            labels = [f"{r.store_code} · {r.store_name}" for r in stores_df.itertuples()]
            store_id = int(stores_df.iloc[labels.index(store_choice)]["store_id"])
        try:
            auth.create_user(username, password, full_name, role, store_id, email, actor=user)
            st.success(f"Account '{username}' created with role {role}.")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    section("Enable / disable", "")
    labels = [f"{r.username} · {r.full_name} [{r.role_code}]" for r in users.itertuples()]
    pick = st.selectbox("Account", labels)
    target = users.iloc[labels.index(pick)]
    state = bool(int(target["is_active"]))
    b1, b2 = st.columns([1, 3])
    with b1:
        if st.button("Disable" if state else "Enable", type="secondary"):
            auth.set_active(int(target["user_id"]), not state, actor=user)
            st.success("Updated.")
            st.rerun()
    with b2:
        st.caption("Disabling blocks login immediately. Historical actions keep the original "
                   "username in the audit log; nothing is anonymised or deleted.")

    section("Role permission matrix", "")
    st.markdown("""
| Capability | Store Employee | Store Manager | Regional Manager | Head Office Admin |
|---|:--:|:--:|:--:|:--:|
| Scan & sell | ✓ | ✓ | | ✓ |
| Receive goods | ✓ | ✓ | | ✓ |
| View own store inventory | ✓ | ✓ | ✓ | ✓ |
| View all stores | | | ✓ | ✓ |
| Adjust stock | | ✓ | | ✓ |
| Raise purchase order | | ✓ | | ✓ |
| Acknowledge / approve PO | | ✓ | ✓ | ✓ |
| Manage alerts | | ✓ | ✓ | ✓ |
| Export reports | | ✓ | ✓ | ✓ |
| Supplier analytics | | ✓ | ✓ | ✓ |
| Sync monitor | | | ✓ | ✓ |
| User management | | | | ✓ |
| System settings | | | | ✓ |
""")
    st.caption("Enforced in two places: the sidebar only renders permitted pages, and each "
               "service re-checks the store scope before writing (see "
               "`authentication.allowed_store_ids`).")


# =========================================================================
def render_settings(user: dict, store_ids, stores_df):
    page_header("System Settings", "Runtime configuration, data health and audit.",
                eyebrow="Administration")
    demo_banner()

    info = pred.model_info()
    counts = table_counts()
    total_rows = int(counts[counts["rows"] > 0]["rows"].sum())

    c.kpi_row([
        {"label": "System version", "value": SYSTEM_VERSION, "sub": "StockSense AI"},
        {"label": "Database rows", "value": f"{total_rows:,}", "sub": "across all tables"},
        {"label": "Active model", "value": ("Trained" if info["available"] else "Baseline"),
         "sub": str(info.get("model_name"))[:34],
         "color": COLORS["primary"] if info["available"] else "#f59e0b"},
        {"label": "Horizon", "value": f"{PREDICTION_HORIZON_DAYS} days", "sub": "prediction window"},
        {"label": "Alert SLA", "value": f"{ALERT_DELIVERY_TARGET_MINUTES} min",
         "sub": f"dedup window {ALERT_DEDUP_WINDOW_HOURS}h", "color": COLORS["accent"]},
    ])

    tabs = st.tabs(["Configuration", "Data Health", "Model Registry", "Audit Log",
                    "Maintenance"])
    with tabs[0]:
        _config(user)
    with tabs[1]:
        _health(counts)
    with tabs[2]:
        _registry(info)
    with tabs[3]:
        _audit()
    with tabs[4]:
        _maintenance(user)


def _config(user: dict = None):
    section("Prediction and alert configuration", "Editable at runtime, persisted to app_config")
    rows = query("SELECT config_key, config_value, updated_at, updated_by FROM app_config "
                 "ORDER BY config_key")
    c.table(rows.rename(columns={"config_key": "Key", "config_value": "Value",
                                 "updated_at": "Updated", "updated_by": "By"}), height=280)

    a, b = st.columns([1, 1])
    with a:
        key = st.selectbox("Setting", rows["config_key"].tolist() if not rows.empty else [])
        value = st.text_input("New value",
                              value=str(rows.loc[rows["config_key"] == key, "config_value"]
                                        .iloc[0]) if not rows.empty else "")
        if st.button("Save setting"):
            execute("UPDATE app_config SET config_value = :v, updated_at = datetime('now'), "
                    "updated_by = :u WHERE config_key = :k",
                    {"v": value, "u": str(user.get("username", "admin")), "k": key})
            audit.record("CONFIG_CHANGED", entity="app_config", entity_id=key, detail=value)
            st.success(f"{key} updated.")
            st.rerun()
    with b:
        section("Risk bands", "Applied by utils.calculations.risk_level_from_probability")
        c.table(pd.DataFrame([{
            "Band": b_["label"], "From %": int(b_["min"] * 100),
            "To %": int(b_["max"] * 100)} for b_ in RISK_BANDS]), height=190)

    section("KPI weights", "Store Performance Score composition")
    c.table(pd.DataFrame([{"Component": k.replace("_", " ").title(),
                           "Weight": f"{v*100:.0f}%"} for k, v in KPI_WEIGHTS.items()]),
            height=210)
    st.caption("Weights are defined in `config.KPI_WEIGHTS` and can be re-tested live on the "
               "Store Performance → Score Lab tab before being changed here.")


def _health(counts: pd.DataFrame):
    section("Table inventory", "Row counts straight from the database")
    c.table(counts.rename(columns={"table": "Table", "rows": "Rows"}), height=420)

    section("Referential and ledger integrity", "")
    checks = []
    try:
        v = query("""SELECT COUNT(*) AS n FROM stock_on_hand soh
                     LEFT JOIN v_ledger_stock l ON l.store_id = soh.store_id
                       AND l.sku_id = soh.sku_id
                     WHERE soh.on_hand_qty <> COALESCE(l.ledger_qty, 0)""").iloc[0]["n"]
        checks.append({"Check": "stock_on_hand matches ledger sum", "Mismatches": int(v),
                       "Status": "PASS" if int(v) == 0 else "FAIL"})
    except Exception as exc:
        checks.append({"Check": "ledger reconciliation", "Mismatches": -1, "Status": str(exc)})
    for label, sql in [
        ("Sales transactions without items",
         "SELECT COUNT(*) AS n FROM sales_transactions t WHERE NOT EXISTS "
         "(SELECT 1 FROM sales_transaction_items i WHERE i.transaction_id = t.transaction_id)"),
        ("Purchase orders without lines",
         "SELECT COUNT(*) AS n FROM purchase_orders p WHERE NOT EXISTS "
         "(SELECT 1 FROM purchase_order_items i WHERE i.po_id = p.po_id)"),
        ("Stock events referencing unknown SKU",
         "SELECT COUNT(*) AS n FROM stock_event e WHERE NOT EXISTS "
         "(SELECT 1 FROM sku k WHERE k.sku_id = e.sku_id)"),
        ("Alerts with invalid probability",
         "SELECT COUNT(*) AS n FROM alerts WHERE probability IS NOT NULL "
         "AND (probability < 0 OR probability > 1)"),
        ("Negative on-hand balances",
         "SELECT COUNT(*) AS n FROM stock_on_hand WHERE on_hand_qty < 0"),
    ]:
        n = int(scalar(sql, default=0))
        checks.append({"Check": label, "Mismatches": n,
                       "Status": "PASS" if n == 0 else "FAIL"})
    c.table(pd.DataFrame(checks), height=280)


def _registry(info: dict):
    section("Model registry", "Every training run, kept for audit")
    df = query("SELECT model_name, model_version, algorithm, trained_at, training_rows, "
               "test_rows, train_start, train_end, test_start, test_end, accuracy, "
               "precision_score, recall_score, f1_score, roc_auc, is_active "
               "FROM ml_model_registry ORDER BY model_id DESC")
    if df.empty:
        c.empty_state("No training runs", "Run: python -m ml.train_model", "◇")
    else:
        c.table(df.rename(columns={
            "model_name": "Model", "model_version": "Version", "algorithm": "Algorithm",
            "trained_at": "Trained", "training_rows": "Train rows", "test_rows": "Test rows",
            "train_start": "Train from", "train_end": "Train to", "test_start": "Test from",
            "test_end": "Test to", "accuracy": "Accuracy", "precision_score": "Precision",
            "recall_score": "Recall", "f1_score": "F1", "roc_auc": "AUC",
            "is_active": "Active"}), height=300)

    section("Active artifact", "")
    st.json({
        "model_file": "ml/saved_model/stockout_model.joblib",
        "metrics_file": "ml/saved_model/metrics.json",
        "available": info["available"],
        "model_name": info.get("model_name"),
        "model_version": info.get("model_version"),
        "trained_at": info.get("trained_at"),
        "features": len(info.get("feature_importance", [])) or None,
        "split_strategy": info.get("split_strategy"),
        "dataset_note": info.get("dataset_note"),
    })

    if st.button("Re-score all predictions now", type="primary"):
        with st.spinner("Scoring every store × SKU …"):
            dfp = pred.score_all(write=True)
        st.success(f"Scored {len(dfp):,} store × SKU pairs with "
                   f"{(dfp['prediction_source'].iloc[0] if not dfp.empty else 'n/a')}.")


def _audit():
    section("Audit trail", "Append-only record of every state change")
    df = audit.recent(200)
    if df.empty:
        c.empty_state("No audit rows", "", "◇")
    else:
        c.table(df.rename(columns={
            "occurred_at": "When", "username": "User", "action": "Action",
            "entity": "Entity", "entity_id": "Entity ID", "store_id": "Store",
            "result": "Result", "detail": "Detail"}), height=460)
    st.caption("Audit rows are written by every service call (sales, receipts, adjustments, "
               "alert transitions, config changes, logins). Failed logins are recorded too.")


def _maintenance(user: dict):
    section("Repair and rebuild", "All operations are idempotent and audited")
    a, b = st.columns(2)
    with a:
        if st.button("Recompute stock_on_hand from ledger", use_container_width=True):
            n = inv.recompute_stock_on_hand()
            st.success(f"Rebuilt {n:,} rows from the immutable ledger.")
        if st.button("Refresh in-transit from open POs", use_container_width=True):
            inv.refresh_in_transit()
            st.success("In-transit quantities re-derived from open purchase orders.")
    with b:
        if st.button("Close resolved stockout episodes", use_container_width=True):
            from modules import stockout_detection as det
            n = det.close_resolved_stockouts()
            st.success(f"Closed {n} episodes whose stock is restored.")
        if st.button("Generate alert sweep", use_container_width=True):
            from modules import stockout_detection as det
            res = det.generate_from_predictions(threshold=0.60, user=user)
            st.success(f"Created {res['created']} alerts, suppressed {res['suppressed']}.")

    section("Backup", "Copy the SQLite file for a point-in-time restore")
    st.code("cp database/stocksense.db backups/stocksense_$(date +%Y%m%d_%H%M).db",
            language="bash")
    st.caption("Production: nightly logical dump plus WAL archiving to object storage, "
               "with a documented restore drill before go-live (see docs, Hardening phase).")

    section("Environment", "")
    st.json({
        "python": "3.12",
        "database": "SQLite (demo) / PostgreSQL-compatible schema (production)",
        "orm": "SQLAlchemy 2.x",
        "ui": "Streamlit",
        "ml": "scikit-learn (LogisticRegression / RandomForest / GradientBoosting)",
        "scan_target_ms": SCAN_RESPONSE_TARGET_MS,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    })
