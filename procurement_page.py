"""Module 6 – Purchase Orders and Goods Receiving."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import streamlit as st

from config import COLORS
from database.db import query
from modules import procurement as proc
from modules import stockout_detection as det
from pages_pkg.common import single_store, store_picker
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.calculations import fmt_money


# =========================================================================
# Purchase Orders
# ========================================================================
def render_po(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Purchase Orders",
        "Raise, track and cancel replenishment orders. Order status is always derived from "
        "line-level received quantities — never set by hand.",
        eyebrow="Procurement",
    )
    demo_banner()

    pos = proc.list_purchase_orders(store_ids=store_ids, limit=5000)
    open_mask = pos["status"].isin(["SUBMITTED", "ACKNOWLEDGED", "PARTIALLY_RECEIVED"])
    open_value = float(pos.loc[open_mask, "total_value"].sum()) if not pos.empty else 0.0
    late = int(((open_mask) & (pos["days_variance"] > 0)).sum()) if not pos.empty else 0

    c.kpi_row([
        {"label": "Purchase orders", "value": f"{len(pos):,}", "sub": "all statuses"},
        {"label": "In flight", "value": f"{int(open_mask.sum()):,}",
         "sub": "awaiting delivery", "color": COLORS["accent"]},
        {"label": "Committed spend", "value": fmt_money(open_value),
         "sub": "open order value", "color": COLORS["primary"]},
        {"label": "Past expected date", "value": f"{late:,}",
         "sub": "supplier delay exposure", "color": "#f97316"},
        {"label": "Fully received",
         "value": f"{int((pos['status'] == 'RECEIVED').sum()):,}", "sub": "closed cleanly"},
    ])

    tabs = st.tabs(["Order Register", "Raise Purchase Order", "Replenishment Suggestions"])
    with tabs[0]:
        _register(user, store_ids)
    with tabs[1]:
        _create(user, store_ids, stores_df)
    with tabs[2]:
        _suggestions(user, store_ids, stores_df)


def _register(user, store_ids):
    f1, f2 = st.columns([1.2, 2], gap="small")
    with f1:
        status = st.selectbox("Status", ["All", "SUBMITTED", "ACKNOWLEDGED",
                                         "PARTIALLY_RECEIVED", "RECEIVED", "CANCELLED"])
    with f2:
        search = st.text_input("Search PO number / supplier", placeholder="PO-2026 or Amul")

    df = proc.list_purchase_orders(store_ids=store_ids,
                                   status=None if status == "All" else status, limit=1000)
    if search and not df.empty:
        m = (df["po_number"].str.contains(search, case=False, na=False)
             | df["supplier_name"].str.contains(search, case=False, na=False)
             | df["store_code"].str.contains(search, case=False, na=False))
        df = df[m]

    if df.empty:
        c.empty_state("No purchase orders", "Raise one from the next tab.", "▦")
        return

    view = df[["po_number", "store_code", "supplier_name", "status", "order_date",
               "expected_date", "actual_delivery_date", "ordered_units", "received_units",
               "total_value", "days_variance"]].rename(columns={
                   "po_number": "PO", "store_code": "Store", "supplier_name": "Supplier",
                   "status": "Status", "order_date": "Ordered", "expected_date": "Expected",
                   "actual_delivery_date": "Delivered", "ordered_units": "Ordered",
                   "received_units": "Received", "total_value": "Value",
                   "days_variance": "Variance (d)"})
    c.table(view, height=340, column_config={
        "Value": st.column_config.NumberColumn(format="₹%.0f")})

    section("Order drill-down", "Line detail and lifecycle")
    opts = {f"{r.po_number} · {r.store_code} · {r.supplier_name} [{r.status}]": int(r.po_id)
            for r in df.itertuples()}
    pick = st.selectbox("Purchase order", list(opts))
    po_id = opts[pick]
    head = df[df["po_id"] == po_id].iloc[0]
    c.metric_strip([
        ("PO", head["po_number"]), ("Status", head["status"]),
        ("Ordered", f"{int(head['ordered_units']):,} units"),
        ("Received", f"{int(head['received_units']):,} units"),
        ("Value", fmt_money(float(head["total_value"]))),
        ("Expected", str(head["expected_date"])),
    ])
    items = proc.po_items(po_id)
    c.table(items.rename(columns={
        "sku_code": "SKU", "product_name": "Product", "ordered_qty": "Ordered",
        "received_qty": "Received", "outstanding_qty": "Outstanding",
        "unit_cost": "Unit cost", "line_value": "Line value"}).drop(columns=["po_item_id",
                                                                           "sku_id"]),
        height=260, column_config={
            "Unit cost": st.column_config.NumberColumn(format="₹%.2f"),
            "Line value": st.column_config.NumberColumn(format="₹%.0f")})

    b1, b2, b3 = st.columns(3)
    with b1:
        if st.button("Acknowledge order", use_container_width=True,
                     disabled=head["status"] != "SUBMITTED"):
            try:
                proc.acknowledge_po(po_id, user)
                st.success("Acknowledged — awaiting the supplier ASN / delivery.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    with b2:
        if st.button("Cancel order", type="secondary", use_container_width=True,
                     disabled=head["status"] in {"RECEIVED", "CANCELLED"}):
            try:
                proc.cancel_po(po_id, user, reason="Cancelled from purchase order screen")
                st.success("Cancelled; in-transit quantities released.")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))
    with c3 if False else st.container():
        pass
    with st.container():
        if st.button("Open receiving screen for this PO", type="secondary",
                     use_container_width=True):
            st.session_state["receiving_po"] = po_id
            st.session_state["page"] = "receiving"
            st.rerun()


def _create(user, store_ids, stores_df):
    section("New purchase order", "Lines are validated before the order is created")
    suppliers = proc.suppliers()
    if suppliers.empty:
        c.empty_state("No suppliers", "Seed the supplier master first.", "⬢")
        return

    a1, a2, a3 = st.columns([1.3, 1.4, 1.1], gap="small")
    with a1:
        store_id = single_store(store_ids, stores_df, "po_create")
    with a2:
        sup_labels = [f"{r.supplier_name} · lead {int(r.lead_time_days)}d · "
                      f"reliability {float(r.reliability_score)*100:.0f}%"
                      for r in suppliers.itertuples()]
        sup_pick = st.selectbox("Supplier", sup_labels)
        sup = suppliers.iloc[sup_labels.index(sup_pick)]
    with a3:
        lead = int(sup["lead_time_days"])
        expected = st.date_input("Expected delivery",
                                 value=date.today() + timedelta(days=lead))

    products = proc.query_all() if False else _products(store_id, int(sup["supplier_id"]))
    if products.empty:
        st.warning("This supplier has no active SKUs.")
        return

    edit = products.copy()
    edit["Order qty"] = [max(0, int(r.max_stock_level) - int(r.on_hand or 0) -
                             int(r.transit or 0)) for r in edit.itertuples()]
    edit["Order qty"] = edit["Order qty"].apply(lambda v: int(round(v / 12) * 12) if v else 0)
    shown = edit[["sku_code", "product_name", "on_hand", "transit", "reorder_level",
                  "max_stock_level", "unit_cost", "Order qty"]].rename(columns={
                      "sku_code": "SKU", "product_name": "Product", "on_hand": "On hand",
                      "transit": "In transit", "reorder_level": "Reorder",
                      "max_stock_level": "Max level", "unit_cost": "Unit cost"})
    edited = st.data_editor(
        shown, use_container_width=True, height=330, hide_index=True,
        disabled=["SKU", "Product", "On hand", "In transit", "Reorder", "Max level",
                  "Unit cost"],
        column_config={
            "Order qty": st.column_config.NumberColumn("Order qty", min_value=0,
                                                       max_value=99999, step=12),
            "Unit cost": st.column_config.NumberColumn(format="₹%.2f"),
        },
        key="po_editor")
    total = float((edited["Order qty"] * edited["Unit cost"]).sum())
    st.markdown(f"**Order value:** {fmt_money(total)} &nbsp;·&nbsp; "
                f"**Lines:** {int((edited['Order qty'] > 0).sum())} "
                f"(supplier minimum {fmt_money(float(sup['min_order_value']))})")

    note = st.text_input("Notes", placeholder="Optional note for the supplier")
    if st.button("Create purchase order", type="primary"):
        lines = []
        for i, r in edited.iterrows():
            if int(r["Order qty"]) > 0:
                lines.append({"sku_id": int(products.iloc[i]["sku_id"]),
                              "ordered_qty": int(r["Order qty"]),
                              "unit_cost": float(r["Unit cost"])})
        try:
            res = proc.create_purchase_order(
                store_id, int(sup["supplier_id"]), lines,
                expected_date=expected.isoformat(), user=user, notes=note)
            st.success(f"Purchase order {res['po_number']} created for "
                       f"{fmt_money(res['total_value'])} — expected {res['expected_date']}.")
            if res.get("warning"):
                st.warning(res["warning"])
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


def _products(store_id: int, supplier_id: int) -> pd.DataFrame:
    return query("""
        SELECT k.sku_id, k.sku_code, k.product_name, k.unit_cost, k.reorder_level,
               k.max_stock_level, COALESCE(soh.on_hand_qty,0) AS on_hand,
               COALESCE(soh.in_transit_qty,0) AS transit
        FROM sku k LEFT JOIN stock_on_hand soh
          ON soh.sku_id = k.sku_id AND soh.store_id = :s
        WHERE k.supplier_id = :sup AND k.is_active = 1
        ORDER BY k.product_name
    """, {"s": int(store_id), "sup": int(supplier_id)})


def _suggestions(user, store_ids, stores_df):
    section("Replenishment suggestions",
            "Derived from today's 7-day stockout predictions, grouped by supplier")
    st.caption("Each line comes from the prediction engine: quantity = lead-time demand + "
               "safety stock, capped at the SKU's max stock level, net of in-transit stock.")

    basket = st.session_state.get("po_basket")
    if basket:
        st.info(f"{len(basket)} lines were carried over from the Stockout Prediction page.")

    store_id = single_store(store_ids, stores_df, "po_suggest")
    sugg = proc.suggested_replenishment(store_id, limit=60)
    if sugg.empty:
        st.success("No SKU is above the 60% risk threshold — no replenishment needed today.")
        return

    view = sugg[["supplier_name", "sku_code", "product_name", "current_stock",
                 "avg_daily_demand", "probability", "risk_level", "reorder_qty",
                 "unit_cost"]].copy()
    view["value"] = (view["reorder_qty"] * view["unit_cost"]).round(2)
    view["probability"] = (view["probability"] * 100).round(0)
    view = view.rename(columns={
        "supplier_name": "Supplier", "sku_code": "SKU", "product_name": "Product",
        "current_stock": "Stock", "avg_daily_demand": "Avg/day", "probability": "P %",
        "risk_level": "Risk", "reorder_qty": "Suggested qty", "unit_cost": "Unit cost",
        "value": "Value"})
    c.table(view, height=340, column_config={
        "Unit cost": st.column_config.NumberColumn(format="₹%.2f"),
        "Value": st.column_config.NumberColumn(format="₹%.0f")})

    sup_pick = st.selectbox("Raise an order for supplier",
                            sorted(sugg["supplier_name"].unique().tolist()))
    sub = sugg[sugg["supplier_name"] == sup_pick]
    st.markdown(f"**{len(sub)} lines · {int(sub['reorder_qty'].sum()):,} units · "
                f"{fmt_money(float((sub['reorder_qty'] * sub['unit_cost']).sum()))}**")
    if st.button("Create purchase order from suggestions", type="primary"):
        lines = [{"sku_id": int(r.sku_id), "ordered_qty": int(r.reorder_qty),
                  "unit_cost": float(r.unit_cost)} for r in sub.itertuples()]
        try:
            res = proc.create_purchase_order(store_id, int(sub.iloc[0]["supplier_id"]), lines,
                                             user=user, notes="Auto-generated from risk list")
            st.success(f"{res['po_number']} created for {fmt_money(res['total_value'])}.")
            st.session_state.pop("po_basket", None)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))


# =========================================================================
# Goods Receiving
# ========================================================================
def render_receiving(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Goods Receiving",
        "Post a delivery against a purchase order — full or partial. Receipts increase stock "
        "through the append-only ledger and update the PO status automatically.",
        eyebrow="Inbound",
    )
    demo_banner()

    store_id = single_store(store_ids, stores_df, "gr_store")
    open_pos = proc.open_pos(store_id)
    if open_pos.empty:
        c.empty_state("No open purchase orders for this store",
                      "Raise a PO first, or the store is fully replenished.", "⬗")
        _receipt_history(store_id)
        return

    labels = [f"{r.po_number} · {r.supplier_name} · expected {r.expected_date} [{r.status}]"
              for r in open_pos.itertuples()]
    preset = st.session_state.get("receiving_po")
    idx = 0
    if preset:
        match = [i for i, r in enumerate(open_pos.itertuples()) if r.po_id == int(preset)]
        idx = match[0] if match else 0
    pick = st.selectbox("Purchase order", labels, index=idx)
    po_id = int(open_pos.iloc[labels.index(pick)]["po_id"])

    items = proc.po_items(po_id)
    outstanding = items[items["outstanding_qty"] > 0]
    if outstanding.empty:
        st.success("Every line on this order has already been received.")
        _receipt_history(store_id)
        return

    section("Delivery lines", "Enter the quantity physically received")
    edit = outstanding[["po_item_id", "sku_code", "product_name", "ordered_qty", "received_qty",
                        "outstanding_qty"]].copy()
    edit["Receive now"] = edit["outstanding_qty"]
    edit["Reject"] = 0
    shown = edit[["po_item_id", "sku_code", "product_name", "ordered_qty", "received_qty",
                  "outstanding_qty", "Receive now", "Reject"]].rename(columns={
                      "po_item_id": "Line ID", "sku_code": "SKU", "product_name": "Product",
                      "ordered_qty": "Ordered", "received_qty": "Already received",
                      "outstanding_qty": "Outstanding"})
    edited = st.data_editor(shown, use_container_width=True, height=320, hide_index=True,
                            disabled=["Line ID", "SKU", "Product", "Ordered",
                                      "Already received", "Outstanding"],
                            column_config={
                                "Receive now": st.column_config.NumberColumn(min_value=0, step=1),
                                "Reject": st.column_config.NumberColumn(min_value=0, step=1),
                            }, key=f"gr_editor_{po_id}")

    d1, d2, d3 = st.columns([1.1, 1.2, 1.6], gap="small")
    with d1:
        dn = st.text_input("Delivery note no.", placeholder="DN-000123")
    with d2:
        note = st.text_input("Discrepancy note", placeholder="Short shipment / damaged carton")
    with d3:
        st.write("")
        st.caption(f"Receiving {int(edited['Receive now'].sum()):,} units · "
                   f"rejecting {int(edited['Reject'].sum()):,}")

    if st.button("Confirm goods receipt", type="primary"):
        lines = []
        for i, r in edited.iterrows():
            if int(r["Receive now"]) or int(r["Reject"]):
                lines.append({"po_item_id": int(r["Line ID"]),
                              "sku_id": int(outstanding.iloc[i]["sku_id"]),
                              "received_qty": int(r["Receive now"]),
                              "rejected_qty": int(r["Reject"])})
        try:
            res = proc.receive_goods(po_id, lines, user=user, delivery_note_no=dn,
                                     discrepancy_note=note)
            st.success(f"{res['receipt_number']} posted · {res['units_received']:,} units · "
                       f"PO status now {res['po_status']}.")
            if res["discrepancy"]:
                st.warning("A quantity discrepancy was recorded against this delivery.")
            st.session_state.pop("receiving_po", None)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    with st.expander("Handling rules applied by this screen"):
        st.markdown("""
- **Partial delivery** — enter less than the outstanding quantity; the PO becomes
  `PARTIALLY_RECEIVED` and stays open for the balance.
- **Over-receipt** — blocked: received + rejected may never exceed the outstanding quantity.
- **Rejected units** — recorded on the receipt, removed from in-transit, and **not** added to
  stock (they are the supplier's responsibility).
- **Stock impact** — one `RECEIPT` event per line; `stock_on_hand` and `in_transit_qty` are
  updated in the same transaction, so the two can never disagree.
- **Completeness** — when every line is satisfied the PO flips to `RECEIVED` and the actual
  delivery date is stamped, which feeds supplier OTIF reporting.
""")

    _receipt_history(store_id)


def _receipt_history(store_id: int):
    section("Recent goods receipts", "Store-level history")
    gr = proc.goods_receipts(store_id, limit=40)
    if gr.empty:
        c.empty_state("No receipts", "Nothing has been received at this store yet.", "◇")
        return
    c.table(gr.rename(columns={
        "receipt_number": "Receipt", "received_at": "Received", "store_code": "Store",
        "supplier_name": "Supplier", "po_number": "PO", "status": "Status",
        "discrepancy_flag": "Discrepancy", "discrepancy_note": "Note",
        "delivery_note_no": "Delivery note"}).drop(columns=["receipt_id"]), height=280)
