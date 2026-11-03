"""
Purchase orders and goods receiving.

A goods receipt is applied inside one transaction:
    goods_receipts + goods_receipt_items + purchase_order_items.received_qty
    + purchase_orders.status + stock_event(RECEIPT) + stock_on_hand
Partial deliveries are supported: the PO stays PARTIALLY_RECEIVED until every
line is satisfied. Quantity mismatches are recorded, never silently ignored.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from database.db import query, scalar, write_txn
from modules import audit
from modules.inventory import record_event
from utils.helpers import get_logger, new_event_id
from utils.validators import ValidationError, validate_po_lines, validate_quantity

log = get_logger("stocksense.procurement")


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def suppliers(active_only: bool = True) -> pd.DataFrame:
    where = "WHERE is_active = 1" if active_only else ""
    return query(
        f"SELECT supplier_id, supplier_code, supplier_name, lead_time_days, reliability_score, "
        f"min_order_value, supports_asn, city FROM suppliers {where} ORDER BY supplier_name")


def list_purchase_orders(store_ids: list[int] | None = None, status: str | None = None,
                         limit: int = 300) -> pd.DataFrame:
    sql = ["""
        SELECT po.po_id, po.po_number, s.store_code, s.store_name, sup.supplier_name,
               po.status, po.order_date, po.expected_date, po.actual_delivery_date,
               po.total_value, po.asn_number,
               (SELECT COUNT(*) FROM purchase_order_items i WHERE i.po_id = po.po_id) AS lines,
               (SELECT COALESCE(SUM(i.ordered_qty),0) FROM purchase_order_items i
                 WHERE i.po_id = po.po_id) AS ordered_units,
               (SELECT COALESCE(SUM(i.received_qty),0) FROM purchase_order_items i
                 WHERE i.po_id = po.po_id) AS received_units,
               CASE WHEN po.actual_delivery_date IS NOT NULL
                    THEN CAST(julianday(po.actual_delivery_date) - julianday(po.expected_date) AS INTEGER)
                    ELSE CAST(julianday('now') - julianday(po.expected_date) AS INTEGER) END AS days_variance
        FROM purchase_orders po
        JOIN stores s ON s.store_id = po.store_id
        JOIN suppliers sup ON sup.supplier_id = po.supplier_id
        WHERE 1=1
    """]
    p: dict = {"l": int(limit)}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND po.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    if status:
        sql.append("AND po.status = :st")
        p["st"] = status
    sql.append("ORDER BY po.po_id DESC LIMIT :l")
    return query(" ".join(sql), p)


def po_items(po_id: int) -> pd.DataFrame:
    return query(
        "SELECT poi.po_item_id, k.sku_id, k.sku_code, k.product_name, poi.ordered_qty, "
        "poi.received_qty, (poi.ordered_qty - poi.received_qty) AS outstanding_qty, "
        "poi.unit_cost, poi.line_value "
        "FROM purchase_order_items poi JOIN sku k ON k.sku_id = poi.sku_id "
        "WHERE poi.po_id = :p ORDER BY k.product_name", {"p": int(po_id)})


def open_pos(store_id: int) -> pd.DataFrame:
    return query(
        "SELECT po.po_id, po.po_number, po.status, po.expected_date, sup.supplier_name, "
        "sup.lead_time_days FROM purchase_orders po JOIN suppliers sup "
        "ON sup.supplier_id = po.supplier_id "
        "WHERE po.store_id = :s AND po.status IN ('SUBMITTED','ACKNOWLEDGED','PARTIALLY_RECEIVED') "
        "ORDER BY po.expected_date", {"s": int(store_id)})


def goods_receipts(store_id: int | None = None, limit: int = 120) -> pd.DataFrame:
    sql = ("SELECT gr.receipt_id, gr.receipt_number, gr.received_at, s.store_code, "
           "sup.supplier_name, po.po_number, gr.status, gr.discrepancy_flag, "
           "gr.discrepancy_note, gr.delivery_note_no "
           "FROM goods_receipts gr JOIN stores s ON s.store_id = gr.store_id "
           "JOIN suppliers sup ON sup.supplier_id = gr.supplier_id "
           "LEFT JOIN purchase_orders po ON po.po_id = gr.po_id")
    p = {"l": int(limit)}
    if store_id:
        sql += " WHERE gr.store_id = :s"
        p["s"] = int(store_id)
    sql += " ORDER BY gr.receipt_id DESC LIMIT :l"
    return query(sql, p)


# --------------------------------------------------------------------------
# Purchase order creation
# --------------------------------------------------------------------------
def create_purchase_order(store_id: int, supplier_id: int, lines: list[dict],
                          expected_date: str | None = None, user: dict | None = None,
                          notes: str = "", asn_number: str | None = None) -> dict:
    """Create a SUBMITTED purchase order with validated lines."""
    lines = validate_po_lines(lines)
    sup = query("SELECT lead_time_days, min_order_value, reliability_score FROM suppliers "
                "WHERE supplier_id = :s", {"s": int(supplier_id)})
    if sup.empty:
        raise ValidationError("Unknown supplier.")
    lead = int(sup.iloc[0]["lead_time_days"])
    expected = expected_date or (date.today() + timedelta(days=lead)).isoformat()
    total = round(sum(l["ordered_qty"] * l["unit_cost"] for l in lines), 2)
    min_ov = float(sup.iloc[0]["min_order_value"])
    warning = None
    if total < min_ov:
        warning = (f"Order value ₹{total:,.0f} is below the supplier minimum of ₹{min_ov:,.0f}; "
                   f"the supplier may consolidate or reject this order.")

    seq = int(scalar("SELECT COUNT(*) FROM purchase_orders", 0)) + 1
    po_number = f"PO-{date.today().strftime('%Y%m')}-{seq:05d}"

    with write_txn() as cur:
        cur.execute(
            "INSERT INTO purchase_orders (po_number, store_id, supplier_id, status, order_date, "
            "expected_date, total_value, raised_by, asn_number, notes) "
            "VALUES (?,?,?,'SUBMITTED',?,?,?,?,?,?)",
            (po_number, int(store_id), int(supplier_id), date.today().isoformat(), expected,
             total, (user or {}).get("user_id"), asn_number, notes or "Raised in StockSense AI"),
        )
        po_id = cur.lastrowid
        for l in lines:
            cur.execute(
                "INSERT INTO purchase_order_items (po_id, sku_id, ordered_qty, received_qty, "
                "unit_cost, line_value) VALUES (?,?,?,0,?,?)",
                (po_id, int(l["sku_id"]), int(l["ordered_qty"]), float(l["unit_cost"]),
                 round(l["ordered_qty"] * l["unit_cost"], 2)),
            )
            cur.execute(
                "UPDATE stock_on_hand SET in_transit_qty = COALESCE(in_transit_qty,0) + ? "
                "WHERE store_id = ? AND sku_id = ?",
                (int(l["ordered_qty"]), int(store_id), int(l["sku_id"])),
            )

    audit.record("PO_CREATED", user=user, entity="purchase_orders", entity_id=po_id,
                 store_id=store_id, detail=f"{po_number}: {len(lines)} lines, ₹{total:,.2f}")
    log.info("PO %s created for store %s (%s lines)", po_number, store_id, len(lines))
    return {"po_id": int(po_id), "po_number": po_number, "total_value": total,
            "expected_date": expected, "warning": warning}


def acknowledge_po(po_id: int, user: dict | None = None) -> None:
    execute_ok = query("SELECT status FROM purchase_orders WHERE po_id = :p", {"p": int(po_id)})
    if execute_ok.empty:
        raise ValidationError("Purchase order not found.")
    if execute_ok.iloc[0]["status"] not in {"SUBMITTED"}:
        raise ValidationError("Only SUBMITTED orders can be acknowledged.")
    from database.db import execute
    execute("UPDATE purchase_orders SET status = 'ACKNOWLEDGED' WHERE po_id = :p", {"p": int(po_id)})
    audit.record("PO_ACKNOWLEDGED", user=user, entity="purchase_orders", entity_id=po_id)


def cancel_po(po_id: int, user: dict | None = None, reason: str = "") -> None:
    from database.db import execute
    items = po_items(po_id)
    status = query("SELECT status, store_id FROM purchase_orders WHERE po_id = :p",
                   {"p": int(po_id)})
    if status.empty:
        raise ValidationError("Purchase order not found.")
    if status.iloc[0]["status"] in {"RECEIVED", "CANCELLED"}:
        raise ValidationError("This order can no longer be cancelled.")
    store_id = int(status.iloc[0]["store_id"])
    for r in items.itertuples():
        execute("UPDATE stock_on_hand SET in_transit_qty = MAX(0, COALESCE(in_transit_qty,0) - :q) "
                "WHERE store_id = :s AND sku_id = :k",
                {"q": int(r.outstanding_qty), "s": store_id, "k": int(r.sku_id)})
    execute("UPDATE purchase_orders SET status = 'CANCELLED', notes = :n WHERE po_id = :p",
            {"p": int(po_id), "n": f"Cancelled: {reason}"[:500]})
    audit.record("PO_CANCELLED", user=user, entity="purchase_orders", entity_id=po_id,
                 store_id=store_id, detail=reason)


def update_po_status(po_id: int) -> str:
    """Re-derive the PO status from its line-level received quantities."""
    lines = po_items(po_id)
    if lines.empty:
        return "DRAFT"
    ordered = float(lines["ordered_qty"].sum())
    received = float(lines["received_qty"].sum())
    if received >= ordered:
        new_status = "RECEIVED"
    elif received > 0:
        new_status = "PARTIALLY_RECEIVED"
    else:
        new_status = "SUBMITTED"
    from database.db import execute
    execute("UPDATE purchase_orders SET status = :s WHERE po_id = :p",
            {"s": new_status, "p": int(po_id)})
    return new_status


# --------------------------------------------------------------------------
# Goods receiving
# --------------------------------------------------------------------------
def receive_goods(po_id: int, receipt_lines: list[dict], user: dict | None = None,
                  delivery_note_no: str = "", discrepancy_note: str = "",
                  received_at: str | None = None) -> dict:
    """Post a (possibly partial) delivery against a purchase order.

    ``receipt_lines``: ``[{"po_item_id":int, "sku_id":int, "received_qty":int,
                          "rejected_qty":int(optional), "batch_code":str, "expiry_date":str}]``
    """
    po = query("SELECT po_id, po_number, store_id, supplier_id, status FROM purchase_orders "
               "WHERE po_id = :p", {"p": int(po_id)})
    if po.empty:
        raise ValidationError("Purchase order not found.")
    po = po.iloc[0].to_dict()
    if po["status"] in {"RECEIVED", "CANCELLED"}:
        raise ValidationError(f"Order {po['po_number']} is {po['status']} – nothing left to receive.")

    items = po_items(po_id).set_index("po_item_id")
    clean = []
    for ln in receipt_lines:
        item_id = int(ln["po_item_id"])
        if item_id not in items.index:
            raise ValidationError(f"Line {item_id} does not belong to this purchase order.")
        qty = int(ln.get("received_qty") or 0)
        rejected = int(ln.get("rejected_qty") or 0)
        if qty < 0 or rejected < 0:
            raise ValidationError("Received and rejected quantities cannot be negative.")
        outstanding = int(items.loc[item_id, "outstanding_qty"])
        if qty + rejected > outstanding:
            raise ValidationError(
                f"{items.loc[item_id, 'product_name']}: receiving {qty + rejected} units but only "
                f"{outstanding} are outstanding on this order.")
        if qty + rejected > 0:
            clean.append({"po_item_id": item_id, "sku_id": int(items.loc[item_id, "sku_id"]),
                          "received_qty": qty, "rejected_qty": rejected,
                          "batch_code": ln.get("batch_code"), "expiry_date": ln.get("expiry_date")})
    if not clean:
        raise ValidationError("No quantities entered – nothing to receive.")

    now = received_at or datetime.now().isoformat(timespec="seconds")
    seq = int(scalar("SELECT COUNT(*) FROM goods_receipts", 0)) + 1
    receipt_no = f"GRN-{date.today().strftime('%Y%m%d')}-{seq:05d}"
    total_received = sum(c["received_qty"] for c in clean)
    mismatch = any(c["received_qty"] + c["rejected_qty"] <
                   int(items.loc[c["po_item_id"], "outstanding_qty"]) for c in clean)
    gr_event_id = new_event_id()

    with write_txn() as cur:
        cur.execute(
            "INSERT INTO goods_receipts (receipt_number, po_id, store_id, supplier_id, received_at, "
            "received_by, delivery_note_no, status, discrepancy_flag, discrepancy_note, event_id) "
            "VALUES (?,?,?,?,?,?,?,'CONFIRMED',?,?,?)",
            (receipt_no, int(po_id), int(po["store_id"]), int(po["supplier_id"]), now,
             (user or {}).get("user_id") or 0, delivery_note_no or f"DN-{seq:06d}",
             1 if mismatch else 0,
             discrepancy_note or ("Short / partial delivery recorded" if mismatch else None),
             gr_event_id),
        )
        receipt_id = cur.lastrowid
        for c in clean:
            cur.execute(
                "INSERT INTO goods_receipt_items (receipt_id, sku_id, po_item_id, expected_qty, "
                "received_qty, rejected_qty, batch_code, expiry_date) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (receipt_id, c["sku_id"], c["po_item_id"],
                 int(items.loc[c["po_item_id"], "ordered_qty"]),
                 c["received_qty"], c["rejected_qty"], c["batch_code"], c["expiry_date"]),
            )
            cur.execute("UPDATE purchase_order_items SET received_qty = received_qty + ? "
                        "WHERE po_item_id = ?", (c["received_qty"], c["po_item_id"]))
            cur.execute("UPDATE stock_on_hand SET in_transit_qty = MAX(0, COALESCE(in_transit_qty,0) "
                        " - ?) WHERE store_id = ? AND sku_id = ?",
                        (c["received_qty"] + c["rejected_qty"], int(po["store_id"]), c["sku_id"]))
            if c["received_qty"] > 0:
                record_event(int(po["store_id"]), c["sku_id"], "RECEIPT", c["received_qty"],
                             reason_code="PO_RECEIPT", reference_table="goods_receipts",
                             reference_id=str(receipt_id),
                             operator_id=(user or {}).get("user_id"),
                             device_id="RECEIVING-DESK", occurred_at=now,
                             note=f"{receipt_no} against {po['po_number']}", cur=cur)

    status = update_po_status(po_id)
    if status == "RECEIVED":
        from database.db import execute
        execute("UPDATE purchase_orders SET actual_delivery_date = :d WHERE po_id = :p",
                {"d": now[:10], "p": int(po_id)})

    audit.record("GOODS_RECEIVED", user=user, entity="goods_receipts", entity_id=receipt_id,
                 store_id=int(po["store_id"]),
                 detail=f"{receipt_no}: {total_received} units against {po['po_number']} "
                        f"(status → {status})")
    log.info("goods receipt %s: %s units, PO status %s", receipt_no, total_received, status)
    return {"receipt_id": int(receipt_id), "receipt_number": receipt_no,
            "units_received": total_received, "po_status": status,
            "discrepancy": mismatch, "event_id": gr_event_id}


def suggested_replenishment(store_id: int, limit: int = 40) -> pd.DataFrame:
    """Turn today's high-risk predictions into draft PO lines."""
    return query("""
        SELECT p.store_id, p.sku_id, k.sku_code, k.product_name, sup.supplier_id,
               sup.supplier_name, p.current_stock, p.avg_daily_demand, p.probability,
               p.risk_level, p.reorder_qty, k.unit_cost, sup.min_order_value
        FROM stockout_prediction p
        JOIN sku k ON k.sku_id = p.sku_id
        JOIN suppliers sup ON sup.supplier_id = k.supplier_id
        WHERE p.store_id = :s AND p.probability >= 0.60 AND p.reorder_qty > 0
        ORDER BY p.probability DESC LIMIT :l
    """, {"s": int(store_id), "l": int(limit)})
