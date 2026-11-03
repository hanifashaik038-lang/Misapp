"""
Module 2 – Stockout Probability Prediction (the centrepiece).

Shows the 7-day stockout probability for every store × SKU, the evidence behind
each number, and the recommended replenishment action. Includes the model
card (algorithm comparison, confusion matrices, ROC curves, feature importance)
and a per-product detail page with risk explanation and history.
"""
from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import COLORS, PREDICTION_HORIZON_DAYS, RISK_ORDER
from modules import procurement, stockout_prediction as pred
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import RISK_COLOR, fmt_money
from utils.helpers import json_loads


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Stockout Probability Prediction",
        f"Which product, in which store, is most likely to run out within the next "
        f"{PREDICTION_HORIZON_DAYS} days — and what should you do about it.",
        eyebrow="Prediction Engine",
    )

    info = pred.model_info()
    summary = pred.prediction_summary(store_ids)
    source = summary.get("source", "n/a")

    if source == "TRAINED_MODEL":
        st.markdown(
            f'<div class="ss-banner" style="border-color:rgba(20,184,166,.45);'
            f'background:rgba(20,184,166,.07);color:#5eead4 !important">'
            f'<span class="x" style="color:#5eead4 !important">◈</span> '
            f'Trained model active: <b>{info.get("model_name")}</b> '
            f'({info.get("model_version")}) trained {str(info.get("trained_at"))[:19]} on '
            f'SIMULATED data · horizon {PREDICTION_HORIZON_DAYS} days · '
            f'{info.get("train_rows", 0):,} training rows'
            f'</div>', unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="ss-banner"><span class="x">⚠</span> No trained model found — the values '
            'below are a clearly-labelled rule-based baseline, not machine learning output. '
            'Run <code>python -m ml.train_model</code> to train.</div>',
            unsafe_allow_html=True)

    demo_banner("Risk values are model outputs on simulated history, never hand-assigned.")

    tab_predict, tab_detail, tab_model, tab_method = st.tabs(
        ["Risk Register", "Product Detail", "Model Performance", "Methodology"])

    # =====================================================================
    with tab_predict:
        _risk_register(user, store_ids, stores_df, summary)
    with tab_detail:
        _product_detail(store_ids, stores_df)
    with tab_model:
        _model_card(info)
    with tab_method:
        _methodology(info)


# -------------------------------------------------------------------------
def _risk_register(user, store_ids, stores_df, summary):
    c.kpi_row([
        {"label": "Pairs scored", "value": f"{summary.get('total', 0):,}",
         "sub": f"as of {summary.get('as_of')}"},
        {"label": "Critical (81–100%)", "value": f"{summary.get('critical', 0):,}",
         "sub": "immediate action", "color": "#ef4444"},
        {"label": "High (61–80%)", "value": f"{summary.get('high', 0):,}",
         "sub": "order within 24h", "color": "#f97316"},
        {"label": "Moderate (31–60%)", "value": f"{summary.get('moderate', 0):,}",
         "sub": "monitor", "color": "#f59e0b"},
        {"label": "Avg probability", "value": f"{summary.get('avg_probability', 0)}%",
         "sub": "chain-wide mean", "color": COLORS["accent"]},
    ])

    st.write("")
    section("Filters", "Combine store, category, SKU and risk level")
    f1, f2, f3, f4 = st.columns([1.15, 1.15, 1.4, 1.3], gap="small")
    with f1:
        if store_ids is None:
            opts = stores_df["store_code"].tolist()
            sel_stores = st.multiselect("Store", opts, default=opts)
            eff_store_ids = (form_store_ids(sel_stores)
                             if len(sel_stores) < len(opts) else None)
        else:
            sel = stores_df[stores_df["store_id"].isin(store_ids)]
            st.multiselect("Store", sel["store_code"].tolist(),
                           default=sel["store_code"].tolist(), disabled=True)
            eff_store_ids = store_ids
    all_preds = pred.predictions(store_ids=eff_store_ids, limit=100000)
    with f2:
        cats = sorted(all_preds["category_name"].unique().tolist()) if not all_preds.empty else []
        sel_cats = st.multiselect("Category", cats, default=[])
    with f3:
        sel_risk = st.multiselect("Risk level", RISK_ORDER, default=["Critical Risk", "High Risk"])
    with f4:
        search = st.text_input("Search product / SKU / barcode", placeholder="e.g. Milk")

    df = pred.predictions(store_ids=eff_store_ids,
                          categories=sel_cats or None,
                          risk_levels=sel_risk or None,
                          search=search, limit=5000)
    c.risk_legend()
    st.caption(f"{len(df):,} store × SKU pairs match the current filters.")

    if df.empty:
        c.empty_state("No matching predictions",
                      "Loosen the filters or re-run prediction scoring.", "◎")
        return

    left, right = st.columns([1.7, 1], gap="medium")
    with left:
        st.plotly_chart(c.scatter_risk(df), use_container_width=True)
    with right:
        section("Category exposure", "Average probability by category")
        cat = df.groupby("category_name").agg(
            avg_p=("probability", "mean"), n=("sku_code", "count")).reset_index()
        cat["avg_pct"] = (cat["avg_p"] * 100).round(1)
        cat = cat.sort_values("avg_pct", ascending=True)
        st.plotly_chart(c.bars(cat, "category_name", "avg_pct", horizontal=True, height=330),
                        use_container_width=True)

    section("Prediction register", "Sorted by probability — highest risk first")
    view = df[["store_code", "product_name", "category_name", "current_stock",
               "avg_daily_demand", "probability", "expected_days_to_oos", "lead_time_days",
               "risk_level", "reorder_qty", "recommended_action"]].copy()
    view["probability_pct"] = (view["probability"] * 100).round(1)
    view = view.drop(columns=["probability"]).rename(columns={
        "store_code": "Store", "product_name": "Product", "category_name": "Category",
        "current_stock": "Stock", "avg_daily_demand": "Avg demand/day",
        "probability_pct": "P(stockout 7d) %", "expected_days_to_oos": "Days to OOS",
        "lead_time_days": "Lead time (d)", "risk_level": "Risk", "reorder_qty": "Order qty",
        "recommended_action": "Recommended action"})
    c.table(view, height=400, column_config={
        "P(stockout 7d) %": st.column_config.ProgressColumn(
            "P(stockout 7d) %", format="%.1f%%", min_value=0, max_value=100)})

    section("Act on the highest risk", "Generate a purchase order from the critical list")
    worst = df[df["risk_level"].isin(["Critical Risk", "High Risk"])].head(200)
    if not worst.empty:
        g1, g2 = st.columns([1.2, 2], gap="medium")
        with g1:
            max_lines = st.slider("Lines to include", 1, min(25, len(worst)), min(8, len(worst)))
            only_one_store = st.checkbox("Single store only", value=True)
        with g2:
            st.caption(
                "StockSense maps each critical SKU to its supplier and proposes order quantities "
                "using min-max replenishment (lead-time demand + safety stock, capped at max level). "
                "Purchase orders are created in the Purchase Orders module.")
        if st.button("Prepare replenishment basket", type="primary"):
            basket = worst.head(max_lines)
            if only_one_store:
                basket = basket[basket["store_code"] == basket["store_code"].iloc[0]]
            st.session_state["po_basket"] = basket[[
                "store_id", "sku_id", "product_name", "store_code", "reorder_qty",
                "unit_cost", "supplier_name"]].to_dict("records")
            st.success(f"{len(basket)} lines prepared. Open **Purchase Orders** to raise the PO.")
    st.caption(
        "Interpretation: probability is the model's estimate that this store/SKU records "
        "unmet demand on at least one day inside the next 7 days. A 94% value means the model "
        "expects the shelf to fail almost certainly — it does not mean 94% of stock is missing.")


def form_store_ids(codes: list[str]) -> list[int]:
    if not codes:
        return []
    from database.db import query
    rows = query("SELECT store_id FROM stores WHERE store_code IN ("
                 + ",".join(f"'{c}'" for c in codes) + ")")
    return [int(x) for x in rows["store_id"].tolist()]


# -------------------------------------------------------------------------
def _product_detail(store_ids, stores_df):
    df = pred.predictions(store_ids=store_ids, limit=100000)
    if df.empty:
        c.empty_state("No predictions", "Score the model first.", "◎")
        return

    a, b = st.columns([1.5, 1.6], gap="medium")
    with a:
        store_opts = df["store_code"].unique().tolist()
        sel_store = st.selectbox("Store", sorted(store_opts))
    sub = df[df["store_code"] == sel_store].sort_values("probability", ascending=False)
    with b:
        prod_opts = sub["product_name"].tolist()
        sel_prod = st.selectbox("Product", prod_opts)

    row = sub[sub["product_name"] == sel_prod].iloc[0].to_dict()
    detail = pred.single_prediction(int(row["store_id"]), int(row["sku_id"]))
    if not detail:
        c.empty_state("Detail unavailable", "Re-score predictions.", "◎")
        return

    st.write("")
    c.kpi_row([
        {"label": "Stockout probability", "value": f"{detail['probability']*100:.0f}%",
         "sub": f"{detail['risk_level']} · next {detail['horizon_days']} days",
         "color": RISK_COLOR.get(detail["risk_level"], "#14b8a6")},
        {"label": "Current stock", "value": f"{int(detail['current_stock']):,} units",
         "sub": f"reorder level {int(detail['reorder_level']):,}"},
        {"label": "Days of cover", "value": f"{detail['days_of_cover']:.1f} d",
         "sub": f"avg {detail['avg_daily_demand']:.1f}/day",
         "color": "#f59e0b" if detail["days_of_cover"] < detail["lead_time_days"] else None},
        {"label": "Supplier lead time", "value": f"{int(detail['lead_time_days'])} d",
         "sub": detail["supplier_name"], "color": COLORS["accent"]},
        {"label": "Recommended order", "value": f"{int(detail['reorder_qty']):,} units",
         "sub": fmt_money(int(detail["reorder_qty"]) * float(detail["unit_cost"])) + " at cost"},
    ])

    st.write("")
    g1, g2 = st.columns([1.55, 1], gap="medium")
    with g1:
        section("Historical sales and stock", "Last 120 business days")
        hist = pred.sku_history(int(row["store_id"]), int(row["sku_id"]), days=120)
        if not hist.empty:
            fig = go.Figure()
            fig.add_trace(go.Bar(x=hist["business_date"], y=hist["units_sold"], name="Units sold",
                                 marker=dict(color="rgba(20,184,166,.62)")))
            fig.add_trace(go.Bar(x=hist["business_date"], y=hist["demand_unmet"],
                                 name="Unmet demand (lost sales)",
                                 marker=dict(color="rgba(239,68,68,.75)")))
            fig.add_trace(go.Scatter(x=hist["business_date"], y=hist["closing_stock"],
                                     name="Closing stock", yaxis="y2", mode="lines",
                                     line=dict(color="#38bdf8", width=2.2)))
            fig.update_layout(barmode="stack", height=330,
                              yaxis=dict(title="Units"), yaxis2=dict(
                                  title="Stock", overlaying="y", side="right", showgrid=False),
                              legend=dict(orientation="h", y=1.16))
            st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Red bars are days where demand exceeded available stock — the ground truth this "
            "model is trained to anticipate.")

    with g2:
        section("Risk explanation", "Why the model produced this number")
        drivers = json_loads(detail.get("top_drivers"), [])
        for d in drivers:
            up = "increases" in d.get("impact", "")
            col = "#fb7185" if up else "#14b8a6"
            arrow = "▲" if up else "▼"
            st.markdown(
                f'<div class="ss-card" style="padding:.55rem .7rem;margin-bottom:.4rem;'
                f'border-left:3px solid {col}">'
                f'<div style="font-size:.79rem;font-weight:650">{arrow} {d["driver"]}</div>'
                f'<div style="font-size:.72rem;color:{COLORS["muted"]};margin-top:.16rem">'
                f'{d["detail"]} <i>({d["impact"]})</i></div></div>',
                unsafe_allow_html=True)

    section("Recommended action", "Explainable, decision-ready")
    st.info(detail["recommended_action"])

    section("Prediction provenance", "Full audit trail for this value")
    st.markdown(f"""
| Field | Value |
|---|---|
| Model | {detail['model_name']} ({detail['model_version']}) |
| Prediction source | `{detail['prediction_source']}` |
| As-of date | {detail['as_of_date']} |
| Horizon | {detail['horizon_days']} days |
| Demeanour of demand | mean {detail['avg_daily_demand']:.2f}/day, σ {detail['demand_std']:.2f} |
| Pending purchase order | {int(detail['pending_po_qty']):,} units in transit |
| Expected days to stockout | {detail['expected_days_to_oos']:.1f} days |
""")


# -------------------------------------------------------------------------
def _model_card(info: dict):
    if not info.get("available") or not info.get("metrics"):
        c.empty_state("No trained model", 
                      "Run: python -m ml.train_model — a baseline heuristic is being used "
                      "in the meantime.", "◇")
        return

    section("Model card", "Trained on simulated FreshMart history with a time-aware split")
    m = info["metrics"]
    best = info.get("best_model")
    rows = []
    for name, r in m.items():
        rows.append({
            "Algorithm": name + (" ★" if name == best else ""),
            "Accuracy": r["accuracy"], "Precision": r["precision"], "Recall": r["recall"],
            "F1": r["f1"], "ROC-AUC": r["roc_auc"],
            "Train accuracy": r["train_accuracy"],
            "Predicted positive rate": r["positive_prediction_rate"],
        })
    comp = pd.DataFrame(rows)

    c.kpi_row([
        {"label": "Selected model", "value": str(best).replace(" ", "\n"), "sub": "highest F1",
         "color": COLORS["primary"]},
        {"label": "F1 score", "value": f"{m[best]['f1']:.3f}", "sub": "held-out test window"},
        {"label": "ROC-AUC", "value": f"{m[best]['roc_auc']:.3f}", "sub": "discrimination"},
        {"label": "Recall", "value": f"{m[best]['recall']:.3f}",
         "sub": "share of true stockouts caught", "color": "#38bdf8"},
        {"label": "Precision", "value": f"{m[best]['precision']:.3f}",
         "sub": "false-alarm control", "color": "#a78bfa"},
    ])

    st.write("")
    section("Algorithm comparison", "Logistic Regression vs Random Forest vs Gradient Boosting")
    st.dataframe(comp.style.format({c_: "{:.3f}" for c_ in comp.columns if c_ != "Algorithm"}),
                 use_container_width=True, hide_index=True)

    g1, g2 = st.columns(2, gap="medium")
    with g1:
        section("Confusion matrix", f"{best} on the test window")
        cm = m[best]["confusion_matrix"]
        fig = go.Figure(go.Heatmap(
            z=cm, x=["Predicted: no stockout", "Predicted: stockout"],
            y=["Actual: no stockout", "Actual: stockout"],
            colorscale=[[0, "#0a1322"], [1, "#14b8a6"]], showscale=False,
            text=[[f"{v:,}" for v in row] for row in cm], texttemplate="%{text}",
            textfont=dict(size=15, color="#e6edf7"),
            hovertemplate="%{y}<br>%{x}<br><b>%{z:,}</b><extra></extra>"))
        fig.update_layout(height=300, yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig, use_container_width=True)
        tn, fp = cm[0]
        fn, tp = cm[1]
        st.caption(f"True negatives {tn:,} · false positives {fp:,} · "
                   f"false negatives {fn:,} · true positives {tp:,}. "
                   f"A false negative is an undetected stockout — the costliest error here, "
                   f"which is why recall is prioritised.")

    with g2:
        section("ROC curves", "All three candidates")
        fig = go.Figure()
        palette = {"Logistic Regression": "#38bdf8", "Random Forest": "#a78bfa",
                   "Gradient Boosting": "#14b8a6"}
        for name, r in m.items():
            rc = r.get("roc_curve") or {}
            if rc.get("fpr"):
                fig.add_trace(go.Scatter(
                    x=rc["fpr"], y=rc["tpr"], mode="lines",
                    name=f"{name} (AUC {r['roc_auc']:.3f})",
                    line=dict(width=2.2, color=palette.get(name))))
        fig.add_trace(go.Scatter(x=[0, 1], y=[0, 1], mode="lines", name="Random",
                                 line=dict(dash="dash", width=1, color="#4b5b73")))
        fig.update_layout(height=300, xaxis_title="False positive rate",
                          yaxis_title="True positive rate",
                          legend=dict(orientation="h", y=-0.28, x=0))
        st.plotly_chart(fig, use_container_width=True)

    section("Feature importance", "What the selected model actually uses")
    imp = pd.DataFrame(info.get("feature_importance", [])[:18])
    if not imp.empty:
        imp = imp.sort_values("share_pct")
        st.plotly_chart(c.bars(imp, "feature", "share_pct", horizontal=True, height=420),
                        use_container_width=True)

    section("Split, leakage controls and provenance", "")
    st.markdown(f"""
| Item | Value |
|---|---|
| Training rows | {info.get('train_rows', 0):,} |
| Test rows | {info.get('test_rows', 0):,} |
| Train window | {info.get('train_start')} → {info.get('train_end')} |
| Test window | {info.get('test_start')} → {info.get('test_end')} |
| Split strategy | {info.get('split_strategy')} |
| Label definition | {info.get('label_definition')} |
| Positive rate (test) | {info.get('positive_rate_test')} |
| Dataset | {info.get('dataset_note')} |
""")
    st.caption("The 7-day purge gap guarantees that no training label window overlaps the "
               "test period, so the reported scores are not inflated by leakage.")


# -------------------------------------------------------------------------
def _methodology(info):
    section("What the model predicts", "")
    st.markdown(f"""
**Target.** For a given store × SKU and an *as-of* day `t`, the label is `1` if that pair
recorded **unmet demand — i.e. a genuine lost sale — on any day in `t+1 … t+{PREDICTION_HORIZON_DAYS}`**.
It is derived from the simulation's stock trajectory, not hand-assigned.

**Horizon.** {PREDICTION_HORIZON_DAYS} days, matching the longest supplier lead time in the
chain (7 days, Sri Balaji Rice Traders) so that a prediction always arrives before it is too
late to act.

**Why "unmet demand" and not "stock = 0"?** A shelf can read zero because a delivery simply
has not been unpacked yet, or because true demand was zero that day. Lost sales are the
business-meaningful event: a customer wanted the product and it was not there.
""")

    section("Input features", "26 signals per prediction")
    st.markdown("""
| Group | Features |
|---|---|
| Stock position | current stock, stock ÷ reorder level, stock ÷ safety stock, days of cover |
| Demand level | 7-, 14- and 28-day mean demand, 28-day demand σ, coefficient of variation |
| Demand trend | 7-day ÷ 28-day ratio, promotional days in the last 7 |
| Replenishment | pending PO quantity, pending PO days of cover, receipts in the last 14 days, lead time, reorder level, safety stock |
| Risk history | stockout days in the last 28, unmet-demand share of total demand |
| Calendar | day of week, weekend flag, annual seasonality (sin/cos) |
| Context | store demand factor, store format, selling price, margin % |
""")

    section("Leakage controls", "Enforced in code, not just asserted")
    st.markdown("""
1. **Feature timestamps** — every feature for as-of day `t` is computed from days `≤ t`
   inside `ml/features.py:window_features()`.
2. **Label isolation** — the target reads `t+1 … t+7` only; `demand_unmet` never appears as a
   feature.
3. **Chronological split** — the earliest ~80% of as-of dates train, the final ~20% test,
   with a 7-day purge gap. No shuffling anywhere.
4. **One shared feature function** — training and inference call the identical code path, so
   train/serve skew is structurally impossible.
""")

    section("Fallback behaviour", "")
    st.markdown("""
If `ml/saved_model/stockout_model.joblib` is absent, the system does **not** invent model
output. It labels every row `prediction_source = BASELINE_HEURISTIC` and scores with a
transparent logistic rule over days-of-cover, lead time, pending POs and 28-day stockout
frequency. The UI banner changes accordingly, so a reviewer can always tell which path produced
a number.
""")

    section("Limitations of this model", "")
    st.markdown("""
- Trained on **simulated** data. Real FreshMart history would need re-training and
  validation before any operational use.
- Rich behavioural drivers (competitor promotions, local events, weather shocks, planogram
  changes) are not represented.
- Probabilities are **point-in-time**; after a delivery or a large sale, re-scoring is
  required (the app does this on every scan and on demand).
- No per-store calibration yet — a store with systematically different demand noise may be
  over- or under-warned.
""")
