"""
Reporting and export service.

Produces genuine files from live queries:
  * CSV      – pandas
  * Excel    – openpyxl with a styled header, freeze panes, autofilter, number formats
  * PDF      – reportlab platypus, A4 landscape tables with headers/footers

Every function returns ``bytes`` so Streamlit's download_button can serve it
without leaving files behind on disk.
"""
from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from config import CHAIN_NAME, DEMO_BANNER, SYSTEM_NAME, SYSTEM_VERSION
from database.db import query
from modules import procurement, stockout_prediction, store_performance, supplier_analytics
from modules.alerts import list_alerts
from modules.inventory import inventory_view
from modules.stockout_detection import stockout_episodes
from utils.helpers import get_logger

log = get_logger("stocksense.reports")

NAVY = "0D1526"
TEAL = "14B8A6"


# --------------------------------------------------------------------------
# Report catalogue
# --------------------------------------------------------------------------
def report_catalogue() -> list[dict]:
    return [
        {"key": "prediction", "name": "Stockout Probability Report",
         "desc": "7-day stockout probability for every store × SKU, with recommended actions."},
        {"key": "inventory", "name": "Current Stock Report",
         "desc": "On-hand, in-transit, reorder level, stock status and valuation."},
        {"key": "performance", "name": "Store Performance Report",
         "desc": "KPI snapshot, normalised sub-scores and the weighted performance score."},
        {"key": "supplier", "name": "Supplier Performance Report",
         "desc": "On-time delivery, fulfilment, lead-time variance and stockout contribution."},
        {"key": "movement", "name": "Inventory Movement Report",
         "desc": "Append-only stock event ledger extract."},
        {"key": "alerts", "name": "Alert History",
         "desc": "Full alert lifecycle with delivery latency and resolution notes."},
        {"key": "kpi_daily", "name": "Daily KPI Report",
         "desc": "Day-by-day store KPIs across the reporting window."},
        {"key": "monthly", "name": "Monthly KPI Report",
         "desc": "Store KPIs aggregated by calendar month."},
        {"key": "stockouts", "name": "Stockout Episode Report",
         "desc": "Actual stockout episodes with duration and lost revenue."},
    ]


def build_dataset(key: str, store_ids: list[int] | None = None) -> pd.DataFrame:
    """Return the DataFrame behind a report key."""
    if key == "prediction":
        df = stockout_prediction.predictions(store_ids=store_ids, limit=100000)
        if df.empty:
            return df
        return df[["as_of_date", "store_code", "store_name", "sku_code", "product_name",
                   "category_name", "current_stock", "avg_daily_demand", "probability",
                   "risk_level", "expected_days_to_oos", "days_of_cover", "lead_time_days",
                   "pending_po_qty", "reorder_qty", "recommended_action", "model_name",
                   "prediction_source"]].rename(columns={
                       "as_of_date": "As of", "store_code": "Store", "store_name": "Store name",
                       "sku_code": "SKU", "product_name": "Product",
                       "category_name": "Category", "current_stock": "Current stock",
                       "avg_daily_demand": "Avg daily demand",
                       "probability": "Stockout probability (7d)", "risk_level": "Risk level",
                       "expected_days_to_oos": "Days to OOS", "days_of_cover": "Days of cover",
                       "lead_time_days": "Lead time (d)", "pending_po_qty": "Pending PO qty",
                       "reorder_qty": "Recommended qty", "recommended_action": "Recommended action",
                       "model_name": "Model", "prediction_source": "Prediction source"})

    if key == "inventory":
        df = inventory_view(store_ids)
        if df.empty:
            return df
        return df[["store_code", "store_name", "sku_code", "barcode", "product_name",
                   "category_name", "supplier_name", "on_hand_qty", "in_transit_qty",
                   "reorder_level", "safety_stock", "stock_status", "unit_cost",
                   "selling_price", "stock_value"]].rename(columns={
                       "store_code": "Store", "store_name": "Store name", "sku_code": "SKU",
                       "barcode": "Barcode", "product_name": "Product",
                       "category_name": "Category", "supplier_name": "Supplier",
                       "on_hand_qty": "On hand", "in_transit_qty": "In transit",
                       "reorder_level": "Reorder level", "safety_stock": "Safety stock",
                       "stock_status": "Status", "unit_cost": "Unit cost",
                       "selling_price": "Selling price", "stock_value": "Stock value (cost)"})

    if key == "performance":
        df = store_performance.store_kpi_current(store_ids, days=30)
        if df.empty:
            return df
        return df[["rank", "store_code", "store_name", "region", "store_format",
                   "sales_revenue", "units_sold", "inventory_turnover", "stockout_frequency",
                   "stock_availability_pct", "forecast_accuracy_pct",
                   "replenishment_efficiency", "waste_value", "stockout_loss_value",
                   "performance_score", "grade"]].rename(columns={
                       "rank": "Rank", "store_code": "Store", "store_name": "Store name",
                       "region": "Region", "store_format": "Format",
                       "sales_revenue": "Revenue (30d)", "units_sold": "Units sold",
                       "inventory_turnover": "Inventory turnover",
                       "stockout_frequency": "Stockout frequency %",
                       "stock_availability_pct": "Availability %",
                       "forecast_accuracy_pct": "Forecast accuracy %",
                       "replenishment_efficiency": "Replenishment efficiency %",
                       "waste_value": "Waste value", "stockout_loss_value": "Stockout loss",
                       "performance_score": "Performance score", "grade": "Grade"})

    if key == "supplier":
        df = supplier_analytics.supplier_scorecard()
        if df.empty:
            return df
        return df[["supplier_code", "supplier_name", "city", "skus_supplied", "orders_count",
                   "contracted_lead_time", "avg_lead_time_days", "lead_time_gap",
                   "on_time_pct", "fulfilment_pct", "qty_discrepancy_units",
                   "stockout_contribution", "supplier_score", "rating"]].rename(columns={
                       "supplier_code": "Supplier code", "supplier_name": "Supplier",
                       "city": "City", "skus_supplied": "SKUs",
                       "orders_count": "Orders",
                       "contracted_lead_time": "Contracted lead time (d)",
                       "avg_lead_time_days": "Actual lead time (d)",
                       "lead_time_gap": "Lead-time gap (d)",
                       "on_time_pct": "On-time %", "fulfilment_pct": "Fulfilment %",
                       "qty_discrepancy_units": "Qty discrepancy",
                       "stockout_contribution": "Stockout episodes",
                       "supplier_score": "Supplier score", "rating": "Rating"})

    if key == "movement":
        sql = """
            SELECT e.event_seq, e.occurred_at, s.store_code, k.sku_code, k.product_name,
                   e.event_type, e.quantity_delta, e.reason_code, e.reference_table,
                   e.reference_id, e.device_id, e.is_replayed, e.note
            FROM stock_event e JOIN stores s ON s.store_id = e.store_id
            JOIN sku k ON k.sku_id = e.sku_id
        """
        params: dict = {}
        if store_ids is not None:
            if not store_ids:
                return pd.DataFrame()
            sql += " WHERE e.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
        sql += " ORDER BY e.event_seq DESC LIMIT 50000"
        df = query(sql, params)
        return df.rename(columns={
            "event_seq": "Seq", "occurred_at": "Occurred at", "store_code": "Store",
            "sku_code": "SKU", "product_name": "Product", "event_type": "Event type",
            "quantity_delta": "Qty delta", "reason_code": "Reason",
            "reference_table": "Ref table", "reference_id": "Ref id", "device_id": "Device",
            "is_replayed": "Replayed", "note": "Note"})

    if key == "alerts":
        df = list_alerts(store_ids=store_ids, limit=10000)
        if df.empty:
            return df
        return df[["alert_ref", "detected_at", "store_code", "sku_code", "product_name",
                   "alert_type", "severity", "status", "probability", "stock_at_alert",
                   "delivered_at", "acknowledged_at", "resolved_at", "resolution_note"]].rename(
            columns={"alert_ref": "Alert", "detected_at": "Detected",
                     "store_code": "Store", "sku_code": "SKU", "product_name": "Product",
                     "alert_type": "Type", "severity": "Severity", "status": "Status",
                     "probability": "Probability", "stock_at_alert": "Stock at alert",
                     "delivered_at": "Delivered", "acknowledged_at": "Acknowledged",
                     "resolved_at": "Resolved", "resolution_note": "Resolution"})

    if key == "kpi_daily":
        df = query("""
            SELECT k.business_date, s.store_code, s.store_name, k.sales_revenue, k.units_sold,
                   k.inventory_value, k.inventory_turnover, k.stockout_frequency,
                   k.stock_availability_pct, k.forecast_accuracy_pct,
                   k.replenishment_efficiency, k.waste_value, k.stockout_loss_value,
                   k.performance_score
            FROM store_kpi_daily k JOIN stores s ON s.store_id = k.store_id
            ORDER BY k.business_date, s.store_code
        """)
        return df.rename(columns={
            "business_date": "Date", "store_code": "Store", "store_name": "Store name",
            "sales_revenue": "Revenue", "units_sold": "Units", "inventory_value": "Inventory value",
            "inventory_turnover": "Turnover", "stockout_frequency": "Stockout freq %",
            "stock_availability_pct": "Availability %",
            "forecast_accuracy_pct": "Forecast accuracy %",
            "replenishment_efficiency": "Replenishment eff %", "waste_value": "Waste value",
            "stockout_loss_value": "Stockout loss", "performance_score": "Score"})

    if key == "monthly":
        return query("""
            SELECT substr(k.business_date,1,7) AS month, s.store_code, s.store_name,
                   ROUND(SUM(k.sales_revenue),2) AS revenue, SUM(k.units_sold) AS units,
                   ROUND(AVG(k.stock_availability_pct),2) AS availability_pct,
                   ROUND(AVG(k.stockout_frequency),2) AS stockout_freq_pct,
                   ROUND(AVG(k.inventory_turnover),2) AS turnover,
                   ROUND(SUM(k.waste_value),2) AS waste_value,
                   ROUND(SUM(k.stockout_loss_value),2) AS stockout_loss,
                   ROUND(AVG(k.performance_score),2) AS avg_score
            FROM store_kpi_daily k JOIN stores s ON s.store_id = k.store_id
            GROUP BY month, s.store_id ORDER BY month DESC, revenue DESC
        """).rename(columns={
            "month": "Month", "store_code": "Store", "store_name": "Store name",
            "revenue": "Revenue", "units": "Units", "availability_pct": "Availability %",
            "stockout_freq_pct": "Stockout freq %", "turnover": "Turnover",
            "waste_value": "Waste value", "stockout_loss": "Stockout loss",
            "avg_score": "Avg score"})

    if key == "stockouts":
        df = stockout_episodes(store_ids=store_ids, limit=20000)
        if df.empty:
            return df
        return df[["started_at", "ended_at", "store_code", "sku_code", "product_name",
                   "category_name", "detection_type", "severity", "duration_hours",
                   "units_lost", "revenue_lost", "root_cause"]].rename(columns={
                       "started_at": "Started", "ended_at": "Ended", "store_code": "Store",
                       "sku_code": "SKU", "product_name": "Product",
                       "category_name": "Category", "detection_type": "Type",
                       "severity": "Severity", "duration_hours": "Duration (h)",
                       "units_lost": "Units lost", "revenue_lost": "Revenue lost",
                       "root_cause": "Root cause"})

    raise ValueError(f"Unknown report key: {key}")


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
def to_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------
def to_excel(df: pd.DataFrame, sheet_name: str = "Report",
             title: str = "StockSense AI Report") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]

    ws["A1"] = f"{SYSTEM_NAME} – {title}"
    ws["A1"].font = Font(size=15, bold=True, color=f"FF{TEAL}")
    ws["A2"] = f"{CHAIN_NAME}  |  {DEMO_BANNER}"
    ws["A2"].font = Font(size=9, italic=True, color="FF8494AD")
    ws["A3"] = f"Generated {datetime.now():%Y-%m-%d %H:%M}  |  system {SYSTEM_VERSION}  |  " \
               f"{len(df):,} rows"
    ws["A3"].font = Font(size=9, color="FF8494AD")

    header_fill = PatternFill("solid", fgColor=f"FF{NAVY}")
    header_font = Font(color="FFFFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="FF1C2B45")
    start = 5
    for j, col in enumerate(df.columns, start=1):
        c = ws.cell(row=start, column=j, value=str(col))
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = Border(bottom=thin)
    for i, row in enumerate(df.itertuples(index=False), start=start + 1):
        for j, val in enumerate(row, start=1):
            if isinstance(val, (pd.Timestamp, datetime)):
                val = val.isoformat()[:19]
            elif pd.isna(val):
                val = ""
            c = ws.cell(row=i, column=j, value=val)
            c.border = Border(bottom=thin)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                hdr = str(df.columns[j - 1]).lower()
                if any(k in hdr for k in ("value", "revenue", "amount", "cost",
                                          "price", "loss", "waste")):
                    c.number_format = "#,##0.00"
                elif "probability" in hdr:
                    c.number_format = "0.0%"
                elif "pct" in hdr or "(%)" in hdr:
                    c.number_format = "0.00"
                else:
                    c.number_format = "#,##0"

    for j, col in enumerate(df.columns, start=1):
        width = max(10, min(42, int(max([len(str(col))] +
                                        [len(str(v)) for v in df[col].head(120).astype(str)]) * 1.15)))
        ws.column_dimensions[get_column_letter(j)].width = width

    ws.freeze_panes = ws.cell(row=start + 1, column=1)
    if len(df):
        ws.auto_filter.ref = f"A{start}:{get_column_letter(len(df.columns))}{start + len(df)}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def to_pdf(df: pd.DataFrame, title: str, subtitle: str = "",
           max_rows: int = 400) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm, bottomMargin=12 * mm,
        title=title, author=f"{SYSTEM_NAME} / {CHAIN_NAME}",
    )
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1x", parent=styles["Title"], fontSize=17,
                        textColor=colors.HexColor("#0b1220"), spaceAfter=2)
    sub = ParagraphStyle("subx", parent=styles["Normal"], fontSize=8.5,
                         textColor=colors.HexColor("#4b5b73"), spaceAfter=10)

    body = [Paragraph(f"{SYSTEM_NAME} – {title}", h1),
            Paragraph(f"{CHAIN_NAME} · {DEMO_BANNER} · generated {datetime.now():%Y-%m-%d %H:%M}"
                      f"{' · ' + subtitle if subtitle else ''}", sub)]

    view = df.head(max_rows)
    if view.empty:
        body.append(Paragraph("No data available for this report.", styles["Normal"]))
    else:
        cell = ParagraphStyle("cell", parent=styles["Normal"], fontSize=6.4, leading=7.6)
        head = ParagraphStyle("head", parent=styles["Normal"], fontSize=6.6, leading=8,
                             textColor=colors.white, fontName="Helvetica-Bold")
        data = [[Paragraph(str(c), head) for c in view.columns]]
        for row in view.itertuples(index=False):
            cells = []
            for v in row:
                if isinstance(v, float):
                    cells.append(Paragraph(f"{v:,.2f}", cell))
                elif isinstance(v, int):
                    cells.append(Paragraph(f"{v:,}", cell))
                else:
                    cells.append(Paragraph(str(v)[:140], cell))
            data.append(cells)

        ncols = len(view.columns)
        avail = doc.width
        weights = []
        for c in view.columns:
            lc = str(c).lower()
            weights.append(2.6 if any(k in lc for k in ("action", "note", "message", "resolution"))
                           else 1.35 if any(k in lc for k in ("product", "name", "supplier",
                                                              "recommended"))
                           else 0.95)
        total = sum(weights)
        widths = [avail * w / total for w in weights]
        tbl = Table(data, colWidths=widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{NAVY}")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d3e0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f2f6fa")]),
            ("LEFTPADDING", (0, 0), (-1, -1), 2.5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2.5),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        body.append(tbl)
        if len(df) > max_rows:
            body.append(Spacer(1, 6))
            body.append(Paragraph(
                f"Showing the first {max_rows:,} of {len(df):,} rows. "
                f"The CSV/Excel exports contain the complete dataset.", sub))

    def _footer(canv, doc_):
        canv.saveState()
        canv.setFont("Helvetica", 6.6)
        canv.setFillColor(colors.HexColor("#7c8aa0"))
        canv.drawString(12 * mm, 7 * mm,
                        f"{SYSTEM_NAME} {SYSTEM_VERSION} · simulated demonstration data")
        canv.drawRightString(landscape(A4)[0] - 12 * mm, 7 * mm, f"Page {doc_.page}")
        canv.restoreState()

    doc.build(body, onFirstPage=_footer, onLaterPages=_footer)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Convenience
# --------------------------------------------------------------------------
def export_all(key: str, store_ids: list[int] | None = None) -> dict:
    df = build_dataset(key, store_ids)
    name = next(r["name"] for r in report_catalogue() if r["key"] == key)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    safe = name.lower().replace(" ", "_").replace("/", "-")
    return {
        "dataframe": df,
        "rows": len(df),
        "name": name,
        "csv": (to_csv(df), f"{safe}_{stamp}.csv"),
        "xlsx": (to_excel(df, sheet_name=name[:31], title=name), f"{safe}_{stamp}.xlsx"),
        "pdf": (to_pdf(df, name, subtitle=f"{len(df):,} rows"), f"{safe}_{stamp}.pdf"),
    }


def executive_snapshot(store_ids: list[int] | None = None) -> pd.DataFrame:
    """One-page PDF summary used on the Reports page."""
    rows = []
    pred = stockout_prediction.prediction_summary(store_ids)
    kpi = store_performance.store_kpi_current(store_ids, days=30)
    for label, value in [
        ("Stores in scope", len(kpi) if not kpi.empty else 0),
        ("SKU-store pairs scored", pred.get("total", 0)),
        ("Critical risk (≥80%)", pred.get("critical", 0)),
        ("High risk (61-80%)", pred.get("high", 0)),
        ("Average stockout probability", f"{pred.get('avg_probability', 0)}%"),
        ("Revenue (30 days)", f"₹{kpi['sales_revenue'].sum():,.0f}" if not kpi.empty else "0"),
        ("Average availability", f"{kpi['stock_availability_pct'].mean():.2f}%"
         if not kpi.empty else "0"),
        ("Average performance score", f"{kpi['performance_score'].mean():.1f}"
         if not kpi.empty else "0"),
        ("Stockout loss (30 days)", f"₹{kpi['stockout_loss_value'].sum():,.0f}"
         if not kpi.empty else "0"),
        ("Waste value (30 days)", f"₹{kpi['waste_value'].sum():,.0f}" if not kpi.empty else "0"),
    ]:
        rows.append({"Metric": label, "Value": value})
    return pd.DataFrame(rows)
