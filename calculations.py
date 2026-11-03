"""Reusable business calculations (pure functions – unit-testable, no I/O)."""
from __future__ import annotations

from typing import Iterable

from config import KPI_LABELS, KPI_WEIGHTS, RISK_BANDS


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------
def risk_level_from_probability(p: float) -> str:
    """Map a 0..1 probability onto the configurable risk ladder.

    Bands are inclusive on both edges of the percentage form so the documented
    thresholds read exactly as specified: 0–30 Low, 31–60 Moderate, 61–80 High,
    81–100 Critical. At a shared edge the lower band wins, which keeps 30 %
    "Low" and 60 % "Moderate" rather than promoting either one.
    """
    pct = max(0.0, min(100.0, float(p) * 100.0))
    for band in KPI_SAFE_BANDS:
        lo = round(band["min"] * 100)
        hi = round(band["max"] * 100)
        if lo <= pct <= hi:
            return band["label"]
    return RISK_BANDS[-1]["label"]


# bands ordered low -> high with half-open intervals, last band closed
KPI_SAFE_BANDS = sorted(RISK_BANDS, key=lambda b: b["min"])

RISK_COLOR = {b["label"]: b["color"] for b in RISK_BANDS}


def severity_from_risk(risk_label: str) -> str:
    return {
        "Low Risk": "LOW",
        "Moderate Risk": "MODERATE",
        "High Risk": "HIGH",
        "Critical Risk": "CRITICAL",
    }.get(risk_label, "MODERATE")


def risk_rank(risk_label: str) -> int:
    return {"Low Risk": 0, "Moderate Risk": 1, "High Risk": 2, "Critical Risk": 3}.get(risk_label, 0)


# --------------------------------------------------------------------------
# Stock
# --------------------------------------------------------------------------
def days_of_cover(current_stock: float, avg_daily_demand: float, pending_po: float = 0) -> float:
    """How many days the available stock lasts at current run-rate.

    Returns ``inf`` when there is no demand, 0.0 when there is no stock.
    """
    total = max(0.0, float(current_stock)) + max(0.0, float(pending_po))
    if avg_daily_demand is None or avg_daily_demand <= 0:
        return float("inf")
    return total / float(avg_daily_demand)


def expected_days_to_stockout(current_stock: float, avg_daily_demand: float) -> float:
    """Days until physical stock hits zero (pending POs excluded: they are not here yet)."""
    if avg_daily_demand is None or avg_daily_demand <= 0:
        return float("inf")
    return max(0.0, float(current_stock)) / float(avg_daily_demand)


def stock_status(on_hand: float, reorder_level: float, safety_stock: float) -> str:
    if on_hand <= 0:
        return "Out of Stock"
    if on_hand <= safety_stock:
        return "Critical Stock"
    if on_hand <= reorder_level:
        return "Low Stock"
    return "In Stock"


STATUS_COLORS = {
    "In Stock": "#14b8a6",
    "Low Stock": "#f59e0b",
    "Critical Stock": "#f97316",
    "Out of Stock": "#ef4444",
}


def recommended_reorder_qty(
    current_stock: float,
    avg_daily_demand: float,
    lead_time_days: int,
    safety_stock: float,
    max_stock_level: float,
    pending_po: float = 0,
) -> int:
    """Classic min-max replenishment: cover lead-time demand plus safety stock."""
    need = avg_daily_demand * (lead_time_days + 2) + safety_stock
    gap = need - (current_stock + pending_po)
    if gap <= 0:
        return 0
    cap = max(max_stock_level - current_stock - pending_po, 0)
    return int(round(min(gap, cap) if cap else gap))


def recommended_action(
    probability: float,
    current_stock: float,
    cover: float,
    lead_time_days: int,
    pending_po: float,
    reorder_qty: int,
) -> str:
    """Human-readable, explainable next step for a store manager."""
    if current_stock <= 0:
        return "Stock already exhausted – raise an emergency replenishment / inter-store transfer now."
    if probability >= 0.80:
        return (
            f"Critical: raise an urgent replenishment request for {reorder_qty} units today. "
            f"Cover is {cover:.1f} days against a {lead_time_days}-day supplier lead time."
        )
    if probability >= 0.60:
        return (
            f"High risk: place a purchase order for {reorder_qty} units within 24 hours "
            f"(cover {cover:.1f} d vs lead time {lead_time_days} d)."
        )
    if probability >= 0.30:
        extra = " Pending PO already covers part of the gap." if pending_po > 0 else ""
        return f"Monitor closely and include in the next routine order cycle ({reorder_qty} units).{extra}"
    return "No action required – stock position is healthy for the 7-day horizon."


# --------------------------------------------------------------------------
# Store performance score
# --------------------------------------------------------------------------
def min_max_normalise(values: Iterable[float], higher_is_better: bool = True) -> list[float]:
    vals = list(values)
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return [50.0 for _ in vals]
    if higher_is_better:
        return [round((v - lo) / (hi - lo) * 100, 2) for v in vals]
    return [round((hi - v) / (hi - lo) * 100, 2) for v in vals]


def performance_score(components: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """Weighted score on a 0-100 scale.

    ``components`` holds 0-100 normalised sub-scores. This is a CONFIGURABLE
    management index – it is not an industry-standard formula.
    """
    w = weights or KPI_WEIGHTS
    total_w = sum(w.values()) or 1.0
    return round(sum(components.get(k, 0.0) * w.get(k, 0.0) for k in w) / total_w, 2)


def score_grade(score: float) -> str:
    if score >= 85:
        return "A – Excellent"
    if score >= 75:
        return "B – Strong"
    if score >= 65:
        return "C – Satisfactory"
    if score >= 50:
        return "D – Needs Improvement"
    return "E – Critical"


def weight_table() -> list[dict]:
    return [
        {"Component": KPI_LABELS[k], "Weight": f"{v*100:.0f}%", "Direction": d}
        for k, v, d in [
            ("sales_performance", KPI_WEIGHTS["sales_performance"], "Higher revenue = better"),
            ("stock_availability", KPI_WEIGHTS["stock_availability"], "Fewer empty shelves = better"),
            ("inventory_efficiency", KPI_WEIGHTS["inventory_efficiency"], "Higher turnover = better"),
            ("forecast_accuracy", KPI_WEIGHTS["forecast_accuracy"], "Lower forecast error = better"),
            ("replenishment_efficiency", KPI_WEIGHTS["replenishment_efficiency"], "Faster/as-ordered delivery = better"),
        ]
    ]


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def fmt_int(v) -> str:
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return "–"


def fmt_money(v, symbol: str = "₹") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if abs(v) >= 1_00_00_000:
        return f"{symbol}{v/1_00_00_000:.2f} Cr"
    if abs(v) >= 1_00_000:
        return f"{symbol}{v/1_00_000:.2f} L"
    return f"{symbol}{v:,.0f}"


def fmt_pct(v, digits: int = 1) -> str:
    try:
        return f"{float(v):.{digits}f}%"
    except (TypeError, ValueError):
        return "–"
