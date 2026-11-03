"""Module 5 – Barcode Scanning and Sales Processing (keyboard-wedge compatible)."""
from __future__ import annotations

import time

import streamlit as st

from config import COLORS
from database.db import query
from modules import inventory as inv
from modules import scanning as scan
from ui import components as c
from ui.theme import demo_banner, page_header, section
from utils.validators import (DuplicateScanError, InsufficientStockError,
                              UnknownBarcodeError, ValidationError)
from utils.calculations import fmt_money


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Barcode Scanner & Sales",
        "USB keyboard-wedge scanning, manual barcode entry and cart checkout. A committed "
        "sale deducts stock atomically and immediately re-checks stockout risk.",
        eyebrow="Point of Sale",
    )
    demo_banner("Scanner hardware is simulated — the input box accepts real wedge output "
                "(digits + Enter).")

    if store_ids is not None:
        store_id = int(store_ids[0])
        store = stores_df[stores_df["store_id"] == store_id].iloc[0]
    else:
        pick = st.selectbox("Store", [f"{r.store_code} · {r.store_name}"
                                      for r in stores_df.itertuples()])
        idx = [f"{r.store_code} · {r.store_name}" for r in stores_df.itertuples()].index(pick)
        store = stores_df.iloc[idx]
        store_id = int(store["store_id"])

    st.session_state.setdefault("cart", [])
    st.session_state.setdefault("last_scan_msg", None)
    st.session_state.setdefault("last_receipt", None)

    left, right = st.columns([1.35, 1], gap="medium")

    with left:
        section("Scan a product", f"{store['store_code']} · {store['store_name']}")
        st.caption("Point the USB scanner at the field below, or type an 8–14 digit EAN/UPC "
                   "barcode and press Enter. Duplicate scans increase the line quantity "
                   "instead of creating a second line.")

        with st.form("scan_form", clear_on_submit=True):
            s1, s2, s3 = st.columns([2.1, 0.8, 1], gap="small")
            with s1:
                barcode = st.text_input("Barcode", placeholder="8900000000001",
                                        label_visibility="visible")
            with s2:
                qty = st.number_input("Qty", min_value=1, max_value=999, value=1)
            with s3:
                st.write("")
                submitted = st.form_submit_button("Scan", type="primary",
                                                  use_container_width=True)

        if submitted:
            t0 = time.perf_counter()
            try:
                res = scan.handle_scan(barcode, store_id,
                                       cart=st.session_state["cart"], quantity=int(qty))
                st.session_state["cart"] = res["cart"]
                elapsed_ms = (time.perf_counter() - t0) * 1000
                st.session_state["last_scan_msg"] = (
                    "warning" if res["duplicate"] else "ok",
                    (res["warning"] or
                     f"{res['product']['product_name']} added · "
                     f"{fmt_money(float(res['product']['selling_price']))} each"),
                    elapsed_ms)
            except UnknownBarcodeError as e:
                st.session_state["last_scan_msg"] = ("error", str(e), 0)
            except InsufficientStockError as e:
                st.session_state["last_scan_msg"] = ("error", str(e), 0)
            except ValidationError as e:
                st.session_state["last_scan_msg"] = ("error", str(e), 0)

        msg = st.session_state.get("last_scan_msg")
        if msg:
            kind, text, ms = msg
            if kind == "ok":
                st.success(f"✓ {text}")
            elif kind == "warning":
                st.warning(f"⚠ {text}")
            else:
                st.error(f"✕ {text}")
            if ms:
                colour = "#14b8a6" if ms < 1000 else "#ef4444"
                st.markdown(
                    f'<div style="font-size:.73rem;color:{COLORS["muted"]}">'
                    f'Scan→resolve latency <b style="color:{colour}">{ms:.0f} ms</b> '
                    f'(NFR target &lt; 1000 ms)</div>', unsafe_allow_html=True)

        section("Quick pick", "Common FMCG fast-movers at this store")
        quick = query("""
            SELECT k.sku_code, k.barcode, k.product_name, k.selling_price FROM sku k
            ORDER BY k.sku_id LIMIT 8
        """)
        cols = st.columns(4)
        for i, r in enumerate(quick.itertuples()):
            with cols[i % 4]:
                if st.button(f"{r.product_name}", key=f"qp_{i}", use_container_width=True,
                             type="secondary"):
                    try:
                        res = scan.handle_scan(r.barcode, store_id,
                                               cart=st.session_state["cart"])
                        st.session_state["cart"] = res["cart"]
                        st.session_state["last_scan_msg"] = ("ok", f"{r.product_name} added", 0)
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

    with right:
        section("Cart", f"{len(st.session_state['cart'])} line(s)")
        cart = st.session_state["cart"]
        if not cart:
            c.empty_state("Cart is empty", "Scan a product or use quick pick.", "▮")
        else:
            total = sum(l["line_amount"] for l in cart)
            for i, line in enumerate(cart):
                st.markdown(
                    f'<div class="ss-card" style="padding:.55rem .7rem;margin-bottom:.4rem">'
                    f'<div style="display:flex;justify-content:space-between;gap:.6rem">'
                    f'<span style="font-size:.8rem;font-weight:640">{line["product_name"]}</span>'
                    f'<span style="font-size:.8rem;color:{COLORS["primary"]};font-weight:700">'
                    f'{fmt_money(line["line_amount"])}</span></div>'
                    f'<div style="font-size:.71rem;color:{COLORS["muted"]};margin-top:.15rem">'
                    f'{line["sku_code"]} · {line["quantity"]} × '
                    f'{fmt_money(line["unit_price"])}</div></div>',
                    unsafe_allow_html=True)
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;padding:.5rem 0;'
                f'border-top:1px solid {COLORS["border"]};font-weight:700">'
                f'<span>Total</span><span>{fmt_money(total)}</span></div>',
                unsafe_allow_html=True)

            problems = scan.validate_cart(cart, store_id)
            for p in problems:
                st.warning(p)

            b1, b2 = st.columns(2)
            with b1:
                if st.button("Confirm sale", type="primary", use_container_width=True,
                             disabled=bool(problems)):
                    try:
                        res = scan.process_sale(cart, store_id, str(store["store_code"]), user)
                        st.session_state["cart"] = []
                        st.session_state["last_receipt"] = res
                        st.session_state["last_scan_msg"] = None
                        st.success(f"Sale {res['receipt_no']} committed · "
                                   f"{res['items']} units · {fmt_money(res['gross_amount'])}")
                        st.rerun()
                    except (InsufficientStockError, DuplicateScanError, ValidationError) as e:
                        st.error(str(e))
            with b2:
                if st.button("Clear cart", type="secondary", use_container_width=True):
                    st.session_state["cart"] = []
                    st.rerun()

    receipt = st.session_state.get("last_receipt")
    if receipt:
        section("Last committed sale", receipt["receipt_no"])
        detail = scan.receipt_detail(receipt["transaction_id"])
        c.metric_strip([
            ("Receipt", receipt["receipt_no"]),
            ("Units", f"{receipt['items']:,}"),
            ("Value", fmt_money(receipt["gross_amount"])),
            ("Event ID", receipt["event_id"][:13] + "…"),
        ])
        c.table(detail.rename(columns={"sku_code": "SKU", "product_name": "Product",
                                       "quantity": "Qty", "unit_price": "Unit price",
                                       "line_amount": "Line amount"}), height=200)

    section("Recent transactions", "Item-level POS receipts")
    hist = scan.sale_history(store_id, limit=40)
    if hist.empty:
        c.empty_state("No transactions", "Scan a sale to create one.", "◇")
    else:
        c.table(hist.rename(columns={
            "transaction_id": "ID", "receipt_no": "Receipt", "store_code": "Store",
            "business_date": "Date", "txn_time": "Time", "item_count": "Items",
            "net_amount": "Amount", "channel": "Channel", "status": "Status"}), height=320,
            column_config={"Amount": st.column_config.NumberColumn(format="₹%.2f")})

    with st.expander("How duplicate / unknown barcode / insufficient stock are handled"):
        st.markdown("""
| Case | Behaviour |
|---|---|
| **Duplicate scan** (same SKU already in cart) | Quantity is incremented on the existing line, never a second row. The UI shows an explicit warning so the cashier knows. |
| **Duplicate commit** (same sale event id re-sent) | Rejected by the `UNIQUE(event_id)` constraint on `sales_transactions` — offline replays cannot double-charge. |
| **Unknown barcode** | `UnknownBarcodeError` — a friendly message plus the manual product search fallback; no partial stock movement. |
| **Insufficient stock** | Checked twice: at scan time and again inside the commit transaction. The second check closes the race where another till sold the last unit. |
| **Invalid barcode format** | Validated against `^\\d{8,14}$` after stripping CR/LF and spaces that wedge scanners append. |
| **Negative stock** | Impossible: the delta insert is guarded by an availability re-read inside the same SQLite transaction; a failure rolls the whole sale back. |
""")
