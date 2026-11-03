"""
Barcode scanning and sales processing.

Handles keyboard-wedge (USB) scanners, manual entry and camera input from the
browser. A scan is validated, resolved to an SKU, then either added to an
open cart (duplicate scan -> quantity increment, explicitly flagged) or
committed as a sale.

Commit path is a single atomic transaction:
    sales_transactions + sales_transaction_items + stock_event(SALE) + stock_on_hand
so a crash can never leave stock and money out of step.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from database.db import query, scalar, write_txn
from modules import audit
from modules.inventory import available, find_by_barcode, get_stock, record_event
from modules.stockout_detection import detect_stockout
from utils.helpers import get_logger, new_event_id
from utils.validators import (DuplicateScanError, InsufficientStockError,
                              UnknownBarcodeError, ValidationError,
                              validate_barcode, validate_quantity)

log = get_logger("stocksense.scanning")

DUPLICATE_SCAN_DEBOUNCE_SECONDS = 2


def handle_scan(barcode: str, store_id: int, cart: list[dict] | None = None,
                quantity: int = 1) -> dict:
    """Resolve a raw scan to a cart line.

    Returns ``{"product":…, "cart":[…], "duplicate":bool, "warning":str|None}``.
    Raises UnknownBarcodeError for an unrecognised code (the UI then offers a
    "search product manually" fallback rather than crashing).
    """
    bc = validate_barcode(barcode)
    quantity = validate_quantity(quantity, "Quantity")

    sku = find_by_barcode(bc)
    if not sku:
        raise UnknownBarcodeError(f"Barcode {bc} is not in the SKU master.")

    stock = get_stock(store_id, int(sku["sku_id"]))
    avail = int(stock["on_hand_qty"] - (stock["reserved_qty"] or 0)) if stock else 0

    cart = [dict(l) for l in (cart or [])]
    existing = next((l for l in cart if l["sku_id"] == sku["sku_id"]), None)
    warning = None
    duplicate = False

    if existing:
        duplicate = True
        new_qty = existing["quantity"] + quantity
        if new_qty > avail:
            raise InsufficientStockError(
                f"Only {avail} units of {sku['product_name']} available "
                f"({existing['quantity']} already in cart).")
        existing["quantity"] = new_qty
        existing["line_amount"] = round(new_qty * existing["unit_price"], 2)
        warning = (f"Duplicate scan detected – {sku['product_name']} quantity "
                   f"increased to {new_qty} instead of adding a new line.")
    else:
        if avail < quantity:
            raise InsufficientStockError(
                f"{sku['product_name']} has only {avail} units available at this store.")
        cart.append({
            "sku_id": int(sku["sku_id"]), "sku_code": sku["sku_code"],
            "barcode": sku["barcode"], "product_name": sku["product_name"],
            "category_name": sku["category_name"],
            "unit_price": float(sku["selling_price"]), "unit_cost": float(sku["unit_cost"]),
            "quantity": quantity, "line_amount": round(quantity * float(sku["selling_price"]), 2),
        })

    return {"product": sku, "cart": cart, "duplicate": duplicate,
            "warning": warning, "available": avail}


def validate_cart(cart: list[dict], store_id: int) -> list[str]:
    """Pre-flight check run before commit – returns a list of human problems."""
    problems = []
    if not cart:
        problems.append("The cart is empty.")
        return problems
    for line in cart:
        avail = available(store_id, int(line["sku_id"]))
        if line["quantity"] > avail:
            problems.append(
                f"{line['product_name']}: {line['quantity']} requested but only {avail} available.")
        if line["quantity"] <= 0:
            problems.append(f"{line['product_name']}: quantity must be positive.")
    return problems


def _next_receipt_no(store_code: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    n = scalar("SELECT COUNT(*) FROM sales_transactions WHERE receipt_no LIKE :p",
               {"p": f"{store_code}-{stamp}%"}, 0)
    return f"{store_code}-{stamp}-{int(n)+1:02d}"


def process_sale(cart: list[dict], store_id: int, store_code: str,
                 user: dict | None = None, channel: str = "POS",
                 client_event_id: str | None = None,
                 business_date: str | None = None) -> dict:
    """Commit a sale: stock deduction + POS receipt in one transaction."""
    problems = validate_cart(cart, store_id)
    if problems:
        raise InsufficientStockError(" ".join(problems))

    receipt_no = _next_receipt_no(store_code)
    now = datetime.now()
    business_date = business_date or now.date().isoformat()
    gross = round(sum(l["line_amount"] for l in cart), 2)
    items = sum(int(l["quantity"]) for l in cart)
    eid = client_event_id or new_event_id()

    with write_txn() as cur:
        # idempotency: an offline client may re-send the same event id
        cur.execute("SELECT 1 FROM sales_transactions WHERE event_id = ?", (eid,))
        if cur.fetchone():
            raise DuplicateScanError("This sale was already recorded (duplicate event id).")

        cur.execute(
            "INSERT INTO sales_transactions (receipt_no, store_id, business_date, txn_time, "
            "cashier_id, channel, item_count, gross_amount, discount_amount, net_amount, "
            "event_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'COMPLETED')",
            (receipt_no, store_id, business_date, now.isoformat(timespec="seconds"),
             (user or {}).get("user_id"), channel, items, gross, 0.0, gross, eid),
        )
        txn_id = cur.lastrowid

        for line in cart:
            sku_id = int(line["sku_id"])
            qty = int(line["quantity"])
            cur.execute(
                "INSERT INTO sales_transaction_items (transaction_id, sku_id, quantity, "
                "unit_price, line_amount, discount_amount) VALUES (?,?,?,?,?,0)",
                (txn_id, sku_id, qty, float(line["unit_price"]), float(line["line_amount"])),
            )
            # guard against a concurrent sale emptying the shelf first
            cur.execute("SELECT on_hand_qty, COALESCE(reserved_qty,0) FROM stock_on_hand "
                        "WHERE store_id = ? AND sku_id = ?", (store_id, sku_id))
            row = cur.fetchone()
            avail = (row[0] - row[1]) if row else 0
            if avail < qty:
                raise InsufficientStockError(
                    f"Stock changed during checkout: {line['product_name']} now has {avail} units.")
            record_event(store_id, sku_id, "SALE", -qty, reason_code="POS_SALE",
                         reference_table="sales_transactions", reference_id=str(txn_id),
                         operator_id=(user or {}).get("user_id"),
                         device_id=f"POS-{store_code}", occurred_at=now.isoformat(timespec="seconds"),
                         note=f"Receipt {receipt_no}", cur=cur)

    audit.record("SALE_COMPLETED", user=user, entity="sales_transactions", entity_id=txn_id,
                 store_id=store_id, detail=f"{receipt_no}: {items} units, value {gross:.2f}")
    log.info("sale %s committed (%s units)", receipt_no, items)

    # post-sale stockout check (actual stockout / low stock)
    try:
        for line in cart:
            detect_stockout(store_id, int(line["sku_id"]), user=user)
    except Exception as exc:  # never fail a completed sale because of alerting
        log.warning("post-sale detection skipped: %s", exc)

    return {"transaction_id": int(txn_id), "receipt_no": receipt_no, "items": items,
            "gross_amount": gross, "event_id": eid, "business_date": business_date}


def sale_history(store_id: int | None = None, limit: int = 60) -> pd.DataFrame:
    sql = ("SELECT st.transaction_id, st.receipt_no, s.store_code, st.business_date, st.txn_time, "
           "st.item_count, st.net_amount, st.channel, st.status "
           "FROM sales_transactions st JOIN stores s ON s.store_id = st.store_id")
    p = {"l": int(limit)}
    if store_id:
        sql += " WHERE st.store_id = :s"
        p["s"] = store_id
    sql += " ORDER BY st.transaction_id DESC LIMIT :l"
    return query(sql, p)


def receipt_detail(transaction_id: int) -> pd.DataFrame:
    return query(
        "SELECT k.sku_code, k.product_name, i.quantity, i.unit_price, i.line_amount "
        "FROM sales_transaction_items i JOIN sku k ON k.sku_id = i.sku_id "
        "WHERE i.transaction_id = :t", {"t": int(transaction_id)})


def top_sellers_by_store(store_id: int, days: int = 7, limit: int = 10) -> pd.DataFrame:
    return query(
        "SELECT k.sku_code, k.product_name, SUM(sd.units_sold) AS units, "
        "ROUND(SUM(sd.gross_revenue),2) AS revenue FROM sales_daily sd "
        "JOIN sku k ON k.sku_id = sd.sku_id "
        "WHERE sd.store_id = :s AND sd.business_date >= date((SELECT MAX(business_date) "
        "FROM sales_daily), :d) GROUP BY k.sku_id ORDER BY units DESC LIMIT :l",
        {"s": store_id, "d": f"-{int(days)} days", "l": int(limit)})
