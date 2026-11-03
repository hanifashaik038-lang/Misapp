"""Module 10 – Reports and Export (CSV / Excel / PDF)."""
from __future__ import annotations

from datetime import datetime

import streamlit as st

from config import COLORS
from modules import reporting as rep
from pages_pkg.common import store_picker
from ui import components as c
from ui.theme import demo_banner, page_header, section


def render(user: dict, store_ids: list[int] | None, stores_df: pd.DataFrame) -> None:
    page_header(
        "Reports & Export",
        "Every report is generated live from the database and can be downloaded as CSV, "
        "styled Excel or typeset PDF.",
        eyebrow="Reporting",
    )
    demo_banner("Exports contain the simulated dataset only.")

    catalogue = rep.report_catalogue()
    section("Choose a report", "Nine operational and management reports")
    labels = [f"{r['name']} — {r['desc']}" for r in catalogue]
    pick = st.selectbox("Report", labels)
    chosen = catalogue[labels.index(pick)]

    f1, f2 = st.columns([1.6, 1.4], gap="small")
    with f1:
        scoped = store_picker(store_ids, stores_df, "rep_scope")
    with f2:
        preview_rows = st.slider("Preview rows", 10, 200, 25, 5)

    with st.spinner("Generating report …"):
        try:
            out = rep.export_all(chosen["key"], scoped)
        except Exception as exc:
            st.error(f"Report generation failed: {exc}")
            return

    df = out["dataframe"]
    c.kpi_row([
        {"label": "Report", "value": chosen["name"].split(" Report")[0],
         "sub": f"{out['rows']:,} rows"},
        {"label": "CSV", "value": f"{len(out['csv'][0])/1024:.0f} KB",
         "sub": "UTF-8 with BOM (Excel-safe)", "color": COLORS["accent"]},
        {"label": "Excel", "value": f"{len(out['xlsx'][0])/1024:.0f} KB",
         "sub": "openpyxl, styled + filtered", "color": COLORS["primary"]},
        {"label": "PDF", "value": f"{len(out['pdf'][0])/1024:.0f} KB",
         "sub": "reportlab, A4 landscape"},
        {"label": "Generated", "value": datetime.now().strftime("%H:%M:%S"),
         "sub": datetime.now().strftime("%d %b %Y")},
    ])

    st.write("")
    d1, d2, d3 = st.columns(3)
    with d1:
        st.download_button("⬇ Download CSV", out["csv"][0], file_name=out["csv"][1],
                           mime="text/csv", use_container_width=True)
    with d2:
        st.download_button("⬇ Download Excel", out["xlsx"][0], file_name=out["xlsx"][1],
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
    with d3:
        st.download_button("⬇ Download PDF", out["pdf"][0], file_name=out["pdf"][1],
                           mime="application/pdf", use_container_width=True)

    section("Preview", f"First {min(preview_rows, len(df)):,} rows")
    if df.empty:
        c.empty_state("Nothing to preview", "This report has no rows for the current scope.", "◇")
    else:
        c.table(df.head(preview_rows), height=430)

    section("One-page executive snapshot", "Always available, regardless of the report chosen")
    snap = rep.executive_snapshot(scoped)
    c.table(snap, height=400)
    snap_bytes = rep.to_pdf(rep.executive_snapshot(scoped), "Executive Snapshot",
                            subtitle="chain-wide summary")
    st.download_button("⬇ Executive snapshot PDF", snap_bytes,
                       file_name=f"executive_snapshot_{datetime.now():%Y%m%d}.pdf",
                       mime="application/pdf")

    section("Report catalogue", "")
    c.table(c_table_rows(catalogue), height=380)


def c_table_rows(catalogue):
    import pandas as pd
    return pd.DataFrame([{"Report": r["name"], "Description": r["desc"]} for r in catalogue])
