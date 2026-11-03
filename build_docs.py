"""Render docs/PROJECT_DOCUMENTATION.md into a styled DOCX and PDF."""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor, Inches

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

BASE = Path(__file__).resolve().parent
MD = BASE / "PROJECT_DOCUMENTATION.md"
DOCX_OUT = BASE / "StockSense_AI_Project_Documentation.docx"
PDF_OUT = BASE / "StockSense_AI_Project_Documentation.pdf"

NAVY = RGBColor(0x0B, 0x15, 0x28)
TEAL = RGBColor(0x11, 0x8A, 0x7E)
GREY = RGBColor(0x4A, 0x57, 0x6B)


# ----------------------------------------------------------------------
# Markdown parsing helpers
# ----------------------------------------------------------------------
def parse_blocks(text: str):
    """Yield ('h1|h2|h3', text) ('p', text) ('code', text|lang) ('table', rows)."""
    lines = text.splitlines()
    i = 0
    buf: list[str] = []

    def flush():
        nonlocal buf
        if buf:
            para = " ".join(x.strip() for x in buf).strip()
            if para:
                yield_block = ("p", clean_inline(para))
                out.append(yield_block)
            buf = []

    out: list[tuple] = []
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            lang = line.strip("`").strip()
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append(("code", "\n".join(code)))
            i += 1
            continue
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            flush()
            rows = []
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            rows.append(header)
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(("table", rows))
            continue
        if line.startswith("### "):
            flush(); out.append(("h3", clean_inline(line[4:])))
        elif line.startswith("## "):
            flush(); out.append(("h2", clean_inline(line[3:])))
        elif line.startswith("# "):
            flush(); out.append(("h1", clean_inline(line[2:])))
        elif line.strip() in ("---", "***"):
            flush(); out.append(("rule", ""))
        elif re.match(r"^\s*[-*] ", line):
            flush(); out.append(("bullet", clean_inline(re.sub(r"^\s*[-*] ", "", line))))
        elif re.match(r"^\s*\d+\. ", line):
            flush(); out.append(("num", clean_inline(line.strip())))
        elif not line.strip():
            flush()
        else:
            buf.append(line)
        i += 1
    flush()
    return out


def clean_inline(t: str) -> str:
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", t)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    return t.strip()


# ----------------------------------------------------------------------
# DOCX
# ----------------------------------------------------------------------
def build_docx(blocks):
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(9.5)
    style.paragraph_format.space_after = Pt(5)

    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.7)
    sec.top_margin = sec.bottom_margin = Inches(0.65)

    # cover
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("StockSense AI")
    r.font.size = Pt(34); r.bold = True; r.font.color.rgb = NAVY
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run("Stockout Probability Prediction and Store Performance Information System")
    r.font.size = Pt(14); r.font.color.rgb = TEAL
    for txt in ["FreshMart Supermarkets — simulated FMCG chain of 8 stores",
                "Waterfall SDLC — requirements, design, implementation, integration, testing, deployment",
                "DEMO DATA – SIMULATED FRESHMART OPERATIONS"]:
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(txt); r.font.size = Pt(10.5); r.font.color.rgb = GREY
    doc.add_paragraph()

    for kind, payload in blocks:
        if kind == "h1":
            doc.add_heading(payload, level=0)
        elif kind == "h2":
            doc.add_heading(payload, level=1)
        elif kind == "h3":
            doc.add_heading(payload, level=2)
        elif kind == "rule":
            pass
        elif kind == "p":
            doc.add_paragraph(payload)
        elif kind == "bullet":
            doc.add_paragraph(payload, style="List Bullet")
        elif kind == "num":
            doc.add_paragraph(payload, style="List Number")
        elif kind == "code":
            p = doc.add_paragraph()
            r = p.add_run(payload)
            r.font.name = "Consolas"; r.font.size = Pt(7.5)
            r.font.color.rgb = RGBColor(0x1F, 0x33, 0x55)
        elif kind == "table":
            rows = payload
            ncols = max(len(r) for r in rows)
            t = doc.add_table(rows=0, cols=ncols)
            t.style = "Light Grid Accent 1"
            for ri, row in enumerate(rows):
                cells = t.add_row().cells
                for ci in range(ncols):
                    txt = row[ci] if ci < len(row) else ""
                    cells[ci].text = txt
                    for par in cells[ci].paragraphs:
                        for run in par.runs:
                            run.font.size = Pt(7.5)
                            if ri == 0:
                                run.bold = True
            doc.add_paragraph()
    doc.save(DOCX_OUT)
    return DOCX_OUT


# ----------------------------------------------------------------------
# PDF
# ----------------------------------------------------------------------
def build_pdf(blocks):
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=8.6, leading=11.6,
                          spaceAfter=5, textColor=colors.HexColor("#16233A"))
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=17, leading=21,
                        textColor=colors.HexColor("#0B1528"), spaceBefore=10, spaceAfter=8)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontSize=12.6, leading=16,
                        textColor=colors.HexColor("#118A7E"), spaceBefore=9, spaceAfter=5)
    h3 = ParagraphStyle("h3", parent=styles["Heading3"], fontSize=10.2, leading=13,
                        textColor=colors.HexColor("#1E3A5F"), spaceBefore=7, spaceAfter=3)
    code = ParagraphStyle("code", parent=styles["Code"], fontSize=6.9, leading=8.4,
                          textColor=colors.HexColor("#12233D"),
                          backColor=colors.HexColor("#F1F5F9"), borderPadding=4,
                          leftIndent=2, spaceBefore=3, spaceAfter=6)
    bullet = ParagraphStyle("bullet", parent=body, leftIndent=10, bulletIndent=2)
    title = ParagraphStyle("title", parent=body, fontSize=27, leading=31, alignment=TA_CENTER,
                           textColor=colors.HexColor("#0B1528"), spaceAfter=8)
    sub = ParagraphStyle("sub", parent=body, fontSize=12.5, leading=16, alignment=TA_CENTER,
                         textColor=colors.HexColor("#118A7E"), spaceAfter=10)
    small = ParagraphStyle("small", parent=body, fontSize=9.5, alignment=TA_CENTER,
                           textColor=colors.HexColor("#4A576B"))

    flow = [Spacer(1, 90 * mm), Paragraph("StockSense AI", title),
            Paragraph("Stockout Probability Prediction and Store Performance Information System", sub),
            Paragraph("FreshMart Supermarkets — simulated FMCG chain of 8 stores", small),
            Paragraph("Waterfall SDLC documentation set", small),
            Spacer(1, 8), Paragraph("DEMO DATA – SIMULATED FRESHMART OPERATIONS", small),
            PageBreak()]

    for kind, payload in blocks:
        if kind == "h1":
            flow.append(Paragraph(esc(payload), h1))
        elif kind == "h2":
            flow.append(Paragraph(esc(payload), h2))
        elif kind == "h3":
            flow.append(Paragraph(esc(payload), h3))
        elif kind == "rule":
            flow.append(Spacer(1, 4))
        elif kind == "p":
            flow.append(Paragraph(esc(payload), body))
        elif kind == "bullet":
            flow.append(Paragraph(esc(payload), bullet, bulletText="•"))
        elif kind == "num":
            flow.append(Paragraph(esc(payload), bullet))
        elif kind == "code":
            flow.append(Preformatted(payload, code))
        elif kind == "table":
            flow.append(make_table(payload, body))
            flow.append(Spacer(1, 6))

    doc = SimpleDocTemplate(str(PDF_OUT), pagesize=A4,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm,
                            title="StockSense AI – Project Documentation",
                            author="StockSense AI project team")

    def deco(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#0B1528"))
        canvas.rect(0, A4[1] - 9 * mm, A4[0], 9 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#94A3B8"))
        canvas.setFont("Helvetica", 6.6)
        canvas.drawString(14 * mm, A4[1] - 6 * mm,
                          "StockSense AI · Stockout Probability Prediction & Store Performance IS")
        canvas.drawRightString(A4[0] - 14 * mm, A4[1] - 6 * mm,
                               "DEMO DATA – SIMULATED FRESHMART OPERATIONS")
        canvas.setFont("Helvetica", 7)
        canvas.drawCentredString(A4[0] / 2, 7 * mm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(flow, onFirstPage=lambda c, d: None, onLaterPages=deco)
    return PDF_OUT


def esc(t: str) -> str:
    return (t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def make_table(rows, body):
    ncols = max(len(r) for r in rows)
    data = [[Paragraph(esc((r[c] if c < len(r) else "")), body) for c in range(ncols)]
            for r in rows]
    avail = A4[0] - 28 * mm
    t = Table(data, colWidths=[avail / ncols] * ncols, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0B1528")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 6.6),
        ("LEADING", (0, 0), (-1, -1), 8.2),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#F8FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


if __name__ == "__main__":
    blocks = parse_blocks(MD.read_text(encoding="utf-8"))
    print("blocks:", len(blocks))
    print("docx:", build_docx(blocks))
    print("pdf:", build_pdf(blocks))
