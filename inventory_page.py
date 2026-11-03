"""Module 4 – Inventory and Stock Management."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from config import COLORS
from database.db import query
from modules import inventory as inv
from pages_pkg.common import store_picker
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import fmt_money


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Inventory & Stock Management",
        "Live on-hand position for every store and SKU, derived from an append-only event "
        "ledger. History is never edited — corrections are new events.",
        eyebrow="Inventory",
    )
    demo_banner()

    val = inv.inventory_valuation(store_ids)
    counts = inv.stock_status_counts(store_ids)

    def status_units(name: str) -> int:
        if counts.empty:
            return 0
        return int(counts.loc[counts["stock_status"] == name, "skus"].sum())

    c.kpi_row([
        {"label": "Units on hand", "value": f"{val['units']:,}",
         "sub": f"{val['skus']:,} store × SKU pairs"},
        {"label": "Inventory value (cost)", "value": fmt_money(val["cost_value"]),
         "sub": "at unit cost", "color": COLORS["accent"]},
        {"label": "Retail value", "value": fmt_money(val["retail_value"]),
         "sub": "at selling price", "color": COLORS["primary"]},
        {"label": "Out of stock", "value": f"{status_units('Out of Stock'):,}",
         "sub": "shelf empty now", "color": "#ef4444"},
        {"label": "Critical / low",
         "value": f"{status_units('Critical Stock') + status_units('Low Stock'):,}",
         "sub": "below safety or reorder", "color": "#f59e0b"},
    ])

    tabs = st.tabs(["Stock Position", "Event Ledger", "Adjust Stock", "Valuation"])
    with tabs[0]:
        _position(store_ids, stores_df)
    with tabs[1]:
        _ledger(store_ids, stores_df)
    with tabs[2]:
        _adjust(user, store_ids, stores_df)
    with tabs[3]:
        _valuation(store_ids)


def _position(store_ids, stores_df):
    f1, f2, f3, f4 = st.columns([1.5, 1.15, 1.1, 1.4], gap="small")
    with f1:
        scoped = store_picker(store_ids, stores_df, "inv_pos")
    with f2:
        cat = st.selectbox("Category", ["All"] + inv.categories(), key="inv_cat")
    with f3:
        status = st.selectbox("Status", ["All", "Out of Stock", "Critical Stock", "Low Stock",
                                        "In Stock"], key="inv_status")
    with f4:
        search = st.text_input("Search product, SKU or barcode", placeholder="Milk / FMCG001 / 890")

    df = inv.inventory_view(scoped, search=search,
                            category=None if cat == "All" else cat,
                            status=None if status == "All" else status)
    st.caption(f"{len(df):,} rows · {int(df['on_hand_qty'].sum()) if not df.empty else 0:,} "
               f"units on hand · {fmt_money(float((df['on_hand_qty'] * df['unit_cost']).sum()) if not df.empty else 0)} at cost")
    if df.empty:
        c.empty_state("No stock rows", "Adjust the filters above.", "▤")
        return

    view = df[["store_code", "sku_code", "barcode", "product_name", "category_name",
               "on_hand_qty", "in_transit_qty", "reorder_level", "safety_stock",
               "stock_status", "stock_value"]].rename(columns={
                   "store_code": "Store", "sku_code": "SKU", "barcode": "Barcode",
                   "product_name": "Product", "category_name": "Category",
                   "on_hand_qty": "On hand", "in_transit_qty": "In transit",
                   "reorder_level": "Reorder", "safety_stock": "Safety",
                   "stock_status": "Status", "stock_value": "Value (cost)"})
    c.table(view, height=470, column_config={
        "Value (cost)": st.column_config.NumberColumn(format="₹%.0f")})


def _ledger(store_ids, stores_df):
    section("Append-only stock event ledger", "Newest first — every movement is immutable")
    col1, col2 = st.columns([1.5, 1.5], gap="small")
    with col1:
        scoped = store_picker(store_ids, stores_df, "inv_led", allow_all=False)
    with col2:
        products = query("SELECT sku_id, sku_code, product_name FROM sku ORDER BY product_name")
        labels = ["All products"] + [f"{r.sku_code} · {r.product_name}"
                                     for r in products.itertuples()]
        pick = st.selectbox("Product", labels, key="inv_led_prod")
    if not scoped:
        return
    store_id = int(scoped[0])
    sku_id = None if pick == "All products" else int(
        products.iloc[labels.index(pick) - 1]["sku_id"])

    mv = inv.movements(store_id, sku_id=sku_id, limit=400)
    if mv.empty:
        c.empty_state("No movements", "No matching events for this store.", "▤")
        return

    v1, v2 = st.columns([1, 1.5], gap="medium")
    with v1:
        section("Movement mix", "Event types in the retained window")
        g = mv.groupby("event_type").size().reset_index(name="n")
        st.plotly_chart(c.donut(g["event_type"].tolist(), g["n"].tolist(), height=250),
                        use_container_width=True)
    with v2:
        section("Reconciliation", "Derived cache vs ledger sum")
        rec = inv.reconcile(store_id)
        if rec.empty:
            st.success("Stock cache and event ledger reconcile exactly for every SKU "
                       "in this store.")
        else:
            st.warning(f"{len(rec)} store × SKU rows differ from the ledger.")
            c.table(rec, height=200)
        if st.button("Recompute stock_on_hand from ledger"):
            n = inv.recompute_stock_on_hand(store_id)
            st.success(f"Rebuilt {n} rows from the event ledger.")
            st.rerun()

    view = mv[["event_seq", "occurred_at", "sku_code", "product_name", "event_type",
               "quantity_delta", "reason_code", "reference_id", "device_id", "is_replayed",
               "note"]].rename(columns={
                   "event_seq": "Seq", "occurred_at": "Occurred", "sku_code": "SKU",
                   "product_name": "Product", "event_type": "Type", "quantity_delta": "Δ Qty",
                   "reason_code": "Reason", "reference_id": "Reference", "device_id": "Device",
                   "is_replayed": "Replayed", "note": "Note"})
    c.table(view, height=380)


def _adjust(user, store_ids, stores_df):
    section("Record a stock adjustment", "Count corrections, damage and write-offs")
    st.caption("A correction is a NEW event with a reason code. Nothing is ever overwritten, "
               "so the audit trail remains complete.")

    scoped = store_picker(store_ids, stores_df, "inv_adj", allow_all=False)
    if not scoped:
        return
    store_id = int(scoped[0])
    products = query("""
        SELECT k.sku_id, k.sku_code, k.product_name, COALESCE(soh.on_hand_qty,0) AS on_hand
        FROM sku k LEFT JOIN stock_on_hand soh
          ON soh.sku_id = k.sku_id AND soh.store_id = :s
        ORDER BY k.product_name
    """, {"s": store_id})
    labels = [f"{r.sku_code} · {r.product_name} (on hand {int(r.on_hand)})"
              for r in products.itertuples()]

    with st.form("adjust_form"):
        a1, a2, a3 = st.columns([2, 0.9, 1], gap="small")
        with a1:
            pick = st.selectbox("Product", labels)
        with a2:
            qty = st.number_input("Quantity", min_value=1, max_value=10000, value=1)
        with a3:
            direction = st.selectbox("Direction", ["UP", "DOWN"])
        b1, b2 = st.columns([1, 2])
        with b1:
            reason = st.selectbox("Reason code", ["STOCKTAKE_CORRECTION", "EXPIRY_WASTE",
                                                 "DAMAGE_IN_TRANSIT", "SUPPLIER_SHORT",
                                                 "INTERNAL_USE", "THEFT_WRITE_OFF"])
        with b2:
            note = st.text_input("Note", placeholder="Detail for the audit trail")
        submitted = st.form_submit_button("Post adjustment", type="primary")

    if submitted:
        sku_id = int(products.iloc[labels.index(pick)]["sku_id"])
        try:
            eid = inv.adjust_stock(store_id, sku_id, int(qty), direction, reason, user, note)
            st.success(f"Adjustment recorded · event {eid[:8]}… · new available balance "
                       f"{inv.available(store_id, sku_id):,} units.")
        except Exception as exc:
            st.error(str(exc))

    section("Formula used for closing stock", "")
    st.latex(r"\text{Closing} = \text{Opening} + \text{Receipts} + "
             r"\text{Adj}^{+} - \text{Sales} - \text{Damages} - \text{Adj}^{-}")
    st.caption("In this implementation the equation is never evaluated as an update: the "
               "balance is the sum of signed deltas in stock_event, and the cache is rebuilt "
               "from it on demand.")


def _valuation(store_ids):
    counts = inv.stock_status_counts(store_ids)
    if counts.empty:
        return
    g1, g2 = st.columns([1, 1.4], gap="medium")
    with g1:
        section("Stock status distribution", "SKU count by status")
        st.plotly_chart(c.donut(counts["stock_status"].tolist(), counts["skus"].tolist(),
                                colors=counts["color"].tolist(), height=270),
                        use_container_width=True)
    with g2:
        section("Value by status", "At unit cost")
        st.plotly_chart(c.bars(counts, "stock_status", "value", horizontal=True, height=270,
                               color_map=dict(zip(counts["stock_status"], counts["color"]))),
                        use_container_width=True)

    section("Category valuation", "Inventory value and units by FMCG category")
    where = ""
    if store_ids:
        where = " WHERE soh.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
    df = query(f"""
        SELECT c.category_name, SUM(soh.on_hand_qty) AS units,
               ROUND(SUM(soh.on_hand_qty * k.unit_cost), 2) AS value,
               COUNT(*) AS pairs
        FROM stock_on_hand soh JOIN sku k ON k.sku_id = soh.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        {where}
        GROUP BY c.category_name ORDER BY value DESC
    """)
    st.plotly_chart(c.bars(df, "category_name", "value", horizontal=True, height=320),
                    use_container_width=True)
    c.table(df.rename(columns={"category_name": "Category", "units": "Units on hand",
                               "value": "Inventory value", "pairs": "Store × SKU"}),
            height=250, column_config={
                "Inventory value": st.column_config.NumberColumn(format="₹%.0f")})
