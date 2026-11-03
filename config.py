"""
Central configuration for StockSense AI.

Everything that a reviewer might reasonably want to tune (risk thresholds,
KPI weights, chain metadata, paths) lives here rather than being scattered
through the code base.
"""
from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_DIR = BASE_DIR / "database"
DB_PATH = DB_DIR / "stocksense.db"
MODEL_DIR = BASE_DIR / "ml" / "saved_model"
MODEL_PATH = MODEL_DIR / "stockout_model.joblib"
METRICS_PATH = MODEL_DIR / "metrics.json"
EXPORT_DIR = BASE_DIR / "exports"

for _p in (DATA_DIR, DB_DIR, MODEL_DIR, EXPORT_DIR):
    _p.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------
# Chain metadata
# --------------------------------------------------------------------------
CHAIN_NAME = "FreshMart Supermarkets"
SYSTEM_NAME = "StockSense AI"
SYSTEM_TAGLINE = "Stockout Probability Prediction & Store Performance Information System"
SYSTEM_VERSION = "1.0.0"
DEMO_BANNER = "DEMO DATA – SIMULATED FRESHMART OPERATIONS"

# Simulation window (days of history that get generated)
HISTORY_DAYS = 180
LEDGER_WINDOW_DAYS = 60      # append-only event ledger retention window
POS_DETAIL_DAYS = 60         # item-level POS retention window
RANDOM_SEED = 20261102

# --------------------------------------------------------------------------
# Prediction configuration
# --------------------------------------------------------------------------
PREDICTION_HORIZON_DAYS = 7

RISK_BANDS = [
    {"label": "Low Risk",      "min": 0.00, "max": 0.30, "color": "#14b8a6", "key": "LOW"},
    {"label": "Moderate Risk", "min": 0.30, "max": 0.60, "color": "#f59e0b", "key": "MODERATE"},
    {"label": "High Risk",     "min": 0.60, "color": "#f97316", "max": 0.80, "key": "HIGH"},
    {"label": "Critical Risk", "min": 0.80, "max": 1.00, "color": "#ef4444", "key": "CRITICAL"},
]

RISK_ORDER = ["Low Risk", "Moderate Risk", "High Risk", "Critical Risk"]
SEVERITY_COLORS = {
    "LOW": "#22d3ee",
    "MODERATE": "#f59e0b",
    "HIGH": "#fb7185",
    "CRITICAL": "#ef4444",
}

# Store Performance Score – a CONFIGURABLE management index, not an
# industry standard. Weights must always be displayed to the user.
KPI_WEIGHTS = {
    "sales_performance": 0.30,
    "stock_availability": 0.25,
    "inventory_efficiency": 0.20,
    "forecast_accuracy": 0.15,
    "replenishment_efficiency": 0.10,
}

KPI_LABELS = {
    "sales_performance": "Sales Performance",
    "stock_availability": "Stock Availability",
    "inventory_efficiency": "Inventory Efficiency",
    "forecast_accuracy": "Forecast Accuracy",
    "replenishment_efficiency": "Replenishment Efficiency",
}

# --------------------------------------------------------------------------
# Operational rules
# --------------------------------------------------------------------------
ALERT_DEDUP_WINDOW_HOURS = 24      # one open alert per (store, sku, type) per window
ALERT_DELIVERY_TARGET_MINUTES = 5  # NFR: alert delivered within 5 minutes
SCAN_RESPONSE_TARGET_MS = 1000     # NFR: scan response < 1 s
STOCK_UNAVAILABLE_THRESHOLD = 0     # "actual stockout" = available stock <= 0

# --------------------------------------------------------------------------
# Demo users  (passwords are hashed at seed time – never stored in plain text)
# --------------------------------------------------------------------------
DEMO_USERS = [
    {"username": "hoadmin",    "password": "Admin@123",   "role": "HEAD_OFFICE_ADMIN",
     "full_name": "Priya Raghavan",  "store_code": None,          "email": "priya.raghavan@freshmart.example"},
    {"username": "regional",   "password": "Regional@123", "role": "REGIONAL_MANAGER",
     "full_name": "Arun Deshmukh",   "store_code": None,          "email": "arun.deshmukh@freshmart.example"},
    {"username": "manager.st01", "password": "Manager@123", "role": "STORE_MANAGER",
     "full_name": "Meera Iyer",      "store_code": "FM-HYD-01",    "email": "meera.iyer@freshmart.example"},
    {"username": "manager.st03", "password": "Manager@123", "role": "STORE_MANAGER",
     "full_name": "Rohit Bhatt",     "store_code": "FM-HYD-03",    "email": "rohit.bhatt@freshmart.example"},
    {"username": "employee.st01", "password": "Employee@123", "role": "STORE_EMPLOYEE",
     "full_name": "Sandeep Kumar",   "store_code": "FM-HYD-01",    "email": "sandeep.kumar@freshmart.example"},
    {"username": "employee.st03", "password": "Employee@123", "role": "STORE_EMPLOYEE",
     "full_name": "Anjali Verma",    "store_code": "FM-HYD-03",    "email": "anjali.verma@freshmart.example"},
]

ROLE_LABELS = {
    "STORE_EMPLOYEE": "Store Employee",
    "STORE_MANAGER": "Store Manager",
    "REGIONAL_MANAGER": "Regional Manager",
    "HEAD_OFFICE_ADMIN": "Head Office Administrator",
}

# Navigation: (page key, label, icon, roles allowed)
NAV_ITEMS = [
    ("executive",   "Executive Dashboard",   "◈", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("prediction",  "Stockout Prediction",   "◎", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("alerts",      "Stockout Alerts",       "◔", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("inventory",   "Inventory Management",  "▤", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER", "STORE_EMPLOYEE"}),
    ("scanner",     "Barcode Scanner",       "▮", {"HEAD_OFFICE_ADMIN", "STORE_MANAGER", "STORE_EMPLOYEE"}),
    ("procurement", "Purchase Orders",       "▦", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("receiving",   "Goods Receiving",       "⬗", {"HEAD_OFFICE_ADMIN", "STORE_MANAGER", "STORE_EMPLOYEE"}),
    ("performance", "Store Performance",     "◉", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("products",    "Product Analytics",     "◫", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("suppliers",   "Supplier Analytics",    "⬢", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER"}),
    ("reports",     "Reports & Export",      "⇩", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER", "STORE_MANAGER"}),
    ("sync",        "Sync Monitor",          "⇅", {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER"}),
    ("users",       "User Management",       "☗", {"HEAD_OFFICE_ADMIN"}),
    ("settings",    "System Settings",       "⚙", {"HEAD_OFFICE_ADMIN"}),
]

# --------------------------------------------------------------------------
# Visual identity (navy + teal)
# --------------------------------------------------------------------------
COLORS = {
    "bg": "#070d1b",
    "surface": "#0d1526",
    "surface_alt": "#111c31",
    "border": "#1c2b45",
    "primary": "#14b8a6",
    "primary_dim": "#0f766e",
    "accent": "#38bdf8",
    "text": "#e6edf7",
    "muted": "#8494ad",
    "grid": "#16233a",
}

CHART_SEQUENCE = ["#14b8a6", "#38bdf8", "#a78bfa", "#f59e0b", "#fb7185", "#22d3ee", "#84cc16", "#f472b6"]
