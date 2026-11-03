"""Input validation used by the service layer and the UI forms."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

BARCODE_RE = re.compile(r"^\d{8,14}$")
SKU_CODE_RE = re.compile(r"^FMCG\d{3}$")


class ValidationError(ValueError):
    """Raised when business input fails a validation rule."""


class DuplicateScanError(Exception):
    """Raised when the same barcode is submitted twice inside the debounce window."""


class InsufficientStockError(Exception):
    """Raised when a sale would drive available stock below zero."""


class UnknownBarcodeError(LookupError):
    """Barcode is syntactically valid but not present in the SKU master."""


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)

    def raise_if_invalid(self) -> None:
        if not self.ok:
            raise ValidationError("; ".join(self.errors))


def validate_barcode(raw: str) -> str:
    """Normalise and validate a scanned barcode.

    Keyboard-wedge scanners frequently append CR/LF or spaces – strip them.
    """
    if raw is None:
        raise ValidationError("Barcode is empty.")
    bc = str(raw).strip().replace(" ", "").replace("\r", "").replace("\n", "")
    if not bc:
        raise ValidationError("Barcode is empty.")
    if not BARCODE_RE.match(bc):
        raise ValidationError(f"'{raw}' is not a valid 8-14 digit EAN/UPC barcode.")
    return bc


def validate_quantity(value, field_name: str = "Quantity", minimum: int = 1, maximum: int = 100_000) -> int:
    try:
        qty = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} must be a whole number.")
    if qty < minimum:
        raise ValidationError(f"{field_name} must be at least {minimum}.")
    if qty > maximum:
        raise ValidationError(f"{field_name} cannot exceed {maximum:,}.")
    return qty


def validate_positive_money(value, field_name: str = "Amount") -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field_name} must be a number.")
    if v < 0:
        raise ValidationError(f"{field_name} cannot be negative.")
    return round(v, 2)


def validate_future_or_today(value, field_name: str = "Date") -> str:
    if isinstance(value, (date, datetime)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    try:
        parsed = datetime.fromisoformat(str(value)).date()
    except ValueError:
        raise ValidationError(f"{field_name} must be an ISO date (YYYY-MM-DD).")
    if parsed < date.today():
        raise ValidationError(f"{field_name} cannot be in the past.")
    return parsed.isoformat()


def validate_po_lines(lines: list[dict]) -> list[dict]:
    """Each line must carry a positive ordered quantity and a unit cost."""
    if not lines:
        raise ValidationError("A purchase order needs at least one line item.")
    clean = []
    for i, ln in enumerate(lines, start=1):
        if not ln.get("sku_id"):
            raise ValidationError(f"Line {i}: SKU is missing.")
        qty = validate_quantity(ln.get("ordered_qty"), f"Line {i} quantity")
        cost = validate_positive_money(ln.get("unit_cost"), f"Line {i} unit cost")
        clean.append({**ln, "ordered_qty": qty, "unit_cost": cost})
    return clean


def validate_sale_lines(lines: list[dict]) -> list[dict]:
    if not lines:
        raise ValidationError("A sale must contain at least one product.")
    out = []
    for ln in lines:
        qty = validate_quantity(ln.get("quantity"), "Sale quantity")
        if not ln.get("sku_id"):
            raise ValidationError("Sale line is missing a product.")
        out.append({**ln, "quantity": qty})
    return out
