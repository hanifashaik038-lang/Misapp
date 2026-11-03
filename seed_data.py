"""
StockSense AI – deterministic synthetic operations generator.

IMPORTANT: every number produced here is SIMULATED. The generator is
seeded (``config.RANDOM_SEED``) so results are reproducible, and the
whole chain is fictional ("FreshMart Supermarkets").

How the simulation works
------------------------
For all 8 stores x ~120 SKUs x 180 business days we step through time and
carry a *real* inventory state:

    available = closing_stock(t-1) + receipts(t)
    sold      = min(true_demand(t), available)
    unmet     = true_demand(t) - sold          -> lost sales (demand_unmet)
    closing   = available - sold - damaged(t)

A replenishment order is placed the moment stock falls to/below the reorder
level, arrives after a supplier lead time that is *randomly late* with
probability (1 - supplier reliability), and therefore some combinations of
store/SKU genuinely run out of stock. The stockout label used for machine
learning is therefore an emergent property of the simulation, not an
assigned value.

Deliberate behavioural differences between stores
-------------------------------------------------
* URBAN / MALL stores carry a higher demand factor and a stronger weekend spike.
* RESIDENTIAL stores are more stable (lower demand CV).
* SUBURBAN stores are smaller and more exposed to supplier delay.
* Beverages / frozen get a heat-sensitivity lift, dairy and bakery are
  perishable (daily waste), festival windows lift snacks, staples and
  personal care.

Run:
    python -m database.seed_data          # build (drops existing tables)
    python -m database.seed_data --keep   # only build if empty
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import date, datetime, time, timedelta

import numpy as np
import pandas as pd

from config import (
    ALERT_DEDUP_WINDOW_HOURS,
    DEMO_USERS,
    HISTORY_DAYS,
    KPI_WEIGHTS,
    LEDGER_WINDOW_DAYS,
    POS_DETAIL_DAYS,
    RANDOM_SEED,
)
from database.db import executemany, execute, query, scalar
from database.schema import create_schema
from utils.calculations import performance_score
from utils.helpers import get_logger, hash_password, new_event_id

log = get_logger("stocksense.seed")

rng = np.random.default_rng(RANDOM_SEED)
py_rng = random.Random(RANDOM_SEED)

END_DATE = date.today()
START_DATE = END_DATE - timedelta(days=HISTORY_DAYS - 1)


# ==========================================================================
# Master data definitions
# ==========================================================================
STORES = [
    # code, name, city, region, format, area, demand_factor, cv, lat, lon
    ("FM-HYD-01", "FreshMart Jubilee Hills",  "Hyderabad",    "Hyderabad Central", "URBAN",       3200, 1.55, 0.20, 17.4239, 78.4120),
    ("FM-HYD-02", "FreshMart Gachibowli",     "Hyderabad",    "Hyderabad West",    "URBAN",       2900, 1.48, 0.22, 17.4401, 78.3489),
    ("FM-HYD-03", "FreshMart Kukatpally",     "Hyderabad",    "Hyderabad North",   "RESIDENTIAL", 2400, 1.25, 0.16, 17.4948, 78.3996),
    ("FM-HYD-04", "FreshMart Secunderabad",   "Secunderabad", "Secunderabad",      "MALL",        3600, 1.62, 0.24, 17.4399, 78.4983),
    ("FM-HYD-05", "FreshMart LB Nagar",       "Hyderabad",    "Hyderabad East",    "RESIDENTIAL", 2100, 1.10, 0.15, 17.3457, 78.5522),
    ("FM-HYD-06", "FreshMart Miyapur",        "Hyderabad",    "Hyderabad West",    "SUBURBAN",    1800, 0.92, 0.18, 17.4967, 78.3577),
    ("FM-HYD-07", "FreshMart Banjara Hills",  "Hyderabad",    "Hyderabad Central", "URBAN",       2700, 1.40, 0.21, 17.4126, 78.4392),
    ("FM-HYD-08", "FreshMart Uppal",          "Hyderabad",    "Hyderabad East",    "SUBURBAN",    1950, 0.98, 0.19, 17.4056, 78.5590),
]

# name, shelf_life_days, demand_mult, weekend_factor, heat_sensitivity, festival_factor
CATEGORIES = [
    ("Dairy",              7,  1.30, 1.06, 0.10, 1.20),
    ("Bakery",             4,  1.05, 1.12, 0.00, 1.18),
    ("Beverages",         180, 1.45, 1.10, 0.45, 1.22),
    ("Packaged Foods",    270, 1.20, 1.05, 0.00, 1.15),
    ("Snacks",            150, 1.15, 1.22, 0.05, 1.45),
    ("Personal Care",     730, 0.70, 1.04, 0.00, 1.25),
    ("Home Care",         730, 0.62, 1.02, 0.00, 1.10),
    ("Staples",           365, 1.35, 0.98, 0.00, 1.30),
    ("Frozen Foods",      240, 0.80, 1.15, 0.25, 1.20),
    ("Household Essentials", 900, 0.45, 1.01, 0.00, 1.08),
]

# code, name, city, lead_time, variability, reliability, min_order, asn
SUPPLIERS = [
    ("SUP-01", "Godrej Consumer Supply Co.",   "Hyderabad", 3, 0.25, 0.94, 15000, 1),
    ("SUP-02", "Amul Distributors South",      "Hyderabad", 2, 0.20, 0.97, 10000, 1),
    ("SUP-03", "Hatsun Dairy Depot",           "Vijayawada", 4, 0.30, 0.90, 12000, 1),
    ("SUP-04", "Britannia Wholesale Hub",      "Hyderabad", 3, 0.22, 0.95, 12000, 1),
    ("SUP-05", "PepsiCo Beverages India",      "Hyderabad", 4, 0.25, 0.93, 20000, 1),
    ("SUP-06", "Coca-Cola Refreshments",       "Secunderabad", 5, 0.30, 0.88, 20000, 1),
    ("SUP-07", "Nestle India Distributor",     "Hyderabad", 4, 0.20, 0.96, 18000, 1),
    ("SUP-08", "ITC Foods Trade Partner",      "Hyderabad", 3, 0.25, 0.95, 15000, 1),
    ("SUP-09", "Parle Products Agency",        "Hyderabad", 3, 0.28, 0.92, 10000, 1),
    ("SUP-10", "HUL Personal Care Depot",      "Hyderabad", 5, 0.30, 0.91, 25000, 1),
    ("SUP-11", "P&G Home Care Wholesale",      "Secunderabad", 6, 0.35, 0.86, 25000, 0),
    ("SUP-12", "Dabur Ayurvedic Traders",      "Hyderabad", 4, 0.25, 0.94, 12000, 1),
    ("SUP-13", "Adani Wilmar Oils",            "Hyderabad", 5, 0.25, 0.93, 20000, 1),
    ("SUP-14", "Sri Balaji Rice Traders",      "Nizamabad", 7, 0.40, 0.82, 15000, 0),
    ("SUP-15", "Telangana Farm Fresh Produce", "Hyderabad", 2, 0.35, 0.89, 8000, 1),
    ("SUP-16", "Mother Dairy Frozen Foods",    "Hyderabad", 4, 0.30, 0.90, 14000, 1),
    ("SUP-17", "Nilgiri Bakery Suppliers",     "Hyderabad", 2, 0.25, 0.92, 6000, 1),
    ("SUP-18", "General Merchandise Mart",     "Hyderabad", 6, 0.35, 0.87, 10000, 0),
]

# (product_name, pack_size, selling_price, base_daily_demand, supplier_index,
#  shelf_life_days|None, is_perishable)
PRODUCTS: dict[str, list[tuple]] = {
    "Dairy": [
        ("Milk 1L", "1 L", 68.0, 46, 1, 5, 1),
        ("Milk 500ml", "500 ml", 36.0, 38, 1, 5, 1),
        ("Butter 500g", "500 g", 285.0, 9, 1, 60, 0),
        ("Curd 400g", "400 g", 45.0, 32, 15, 4, 1),
        ("Paneer 200g", "200 g", 92.0, 14, 15, 5, 1),
        ("Cheese Slices 200g", "200 g", 135.0, 8, 2, 45, 0),
        ("Ghee 500ml", "500 ml", 340.0, 7, 1, 180, 0),
        ("Flavoured Yogurt 100g", "100 g", 25.0, 22, 15, 6, 1),
        ("Lassi 200ml", "200 ml", 30.0, 18, 15, 4, 1),
        ("Fresh Cream 200ml", "200 ml", 78.0, 9, 1, 8, 1),
        ("Milkshake 180ml", "180 ml", 40.0, 16, 2, 20, 0),
        ("Khoya 200g", "200 g", 105.0, 6, 15, 4, 1),
    ],
    "Bakery": [
        ("Bread 400g", "400 g", 42.0, 40, 17, 3, 1),
        ("Brown Bread 400g", "400 g", 50.0, 18, 17, 3, 1),
        ("Multigrain Bread 400g", "400 g", 62.0, 11, 17, 3, 1),
        ("Burger Buns 6pc", "6 pc", 45.0, 13, 17, 3, 1),
        ("Pav 6pc", "6 pc", 30.0, 15, 17, 3, 1),
        ("Croissant 2pc", "2 pc", 75.0, 6, 17, 3, 1),
        ("Fruit Cake 250g", "250 g", 120.0, 5, 17, 12, 1),
        ("Rusk 300g", "300 g", 48.0, 12, 4, 60, 0),
        ("Cream Bun 100g", "100 g", 22.0, 14, 17, 2, 1),
        ("Pizza Base 2pc", "2 pc", 55.0, 8, 17, 5, 1),
        ("Muffin 120g", "120 g", 38.0, 7, 17, 5, 1),
        ("Cookies 200g", "200 g", 65.0, 11, 4, 90, 0),
    ],
    "Beverages": [
        ("Mineral Water 1L", "1 L", 20.0, 62, 5, 180, 0),
        ("Mineral Water 500ml", "500 ml", 12.0, 55, 5, 180, 0),
        ("Orange Juice 1L", "1 L", 115.0, 14, 5, 21, 0),
        ("Apple Juice 1L", "1 L", 125.0, 10, 5, 21, 0),
        ("Cola 750ml", "750 ml", 45.0, 34, 6, 180, 0),
        ("Lemon Soda 600ml", "600 ml", 35.0, 24, 6, 180, 0),
        ("Mango Drink 600ml", "600 ml", 40.0, 26, 6, 120, 0),
        ("Energy Drink 250ml", "250 ml", 110.0, 11, 6, 240, 0),
        ("Iced Tea 500ml", "500 ml", 55.0, 9, 5, 120, 0),
        ("Coconut Water 200ml", "200 ml", 42.0, 12, 5, 90, 0),
        ("Tonic Water 300ml", "300 ml", 60.0, 5, 6, 180, 0),
        ("Sparkling Water 500ml", "500 ml", 70.0, 6, 5, 180, 0),
    ],
    "Packaged Foods": [
        ("Instant Noodles 70g", "70 g", 14.0, 58, 7, 150, 0),
        ("Pasta 500g", "500 g", 85.0, 13, 7, 240, 0),
        ("Tomato Ketchup 500g", "500 g", 110.0, 12, 7, 270, 0),
        ("Cornflakes 500g", "500 g", 190.0, 9, 7, 300, 0),
        ("Oats 1kg", "1 kg", 165.0, 8, 7, 270, 0),
        ("Vermicelli 400g", "400 g", 48.0, 10, 8, 270, 0),
        ("Ready Soup 50g", "50 g", 45.0, 7, 7, 210, 0),
        ("Noodle Cup 70g", "70 g", 35.0, 16, 7, 150, 0),
        ("Mayonnaise 250g", "250 g", 95.0, 6, 7, 180, 0),
        ("Peanut Butter 340g", "340 g", 245.0, 4, 7, 270, 0),
        ("Jam 200g", "200 g", 88.0, 5, 7, 240, 0),
        ("Upma Mix 200g", "200 g", 52.0, 8, 8, 210, 0),
    ],
    "Snacks": [
        ("Potato Chips 100g", "100 g", 30.0, 42, 9, 120, 0),
        ("Biscuits 200g", "200 g", 45.0, 38, 4, 180, 0),
        ("Salted Peanuts 200g", "200 g", 40.0, 15, 9, 150, 0),
        ("Namkeen Mix 200g", "200 g", 42.0, 19, 8, 150, 0),
        ("Chocolate Bar 50g", "50 g", 40.0, 24, 7, 240, 0),
        ("Wafer 75g", "75 g", 25.0, 21, 4, 180, 0),
        ("Popcorn 90g", "90 g", 35.0, 9, 9, 180, 0),
        ("Banana Chips 150g", "150 g", 55.0, 8, 9, 120, 0),
        ("Cream Biscuits 150g", "150 g", 38.0, 26, 4, 180, 0),
        ("Nachos 150g", "150 g", 65.0, 7, 9, 180, 0),
        ("Bhujia 200g", "200 g", 48.0, 14, 8, 150, 0),
        ("Candy Pack 100g", "100 g", 20.0, 18, 9, 300, 0),
    ],
    "Personal Care": [
        ("Shampoo 180ml", "180 ml", 165.0, 9, 10, 730, 0),
        ("Toothpaste 150g", "150 g", 95.0, 11, 10, 730, 0),
        ("Bath Soap 125g", "125 g", 42.0, 16, 10, 900, 0),
        ("Face Wash 100ml", "100 ml", 180.0, 5, 10, 730, 0),
        ("Body Lotion 200ml", "200 ml", 220.0, 4, 10, 730, 0),
        ("Deodorant 150ml", "150 ml", 195.0, 4, 10, 730, 0),
        ("Shaving Cream 70g", "70 g", 88.0, 3, 10, 540, 0),
        ("Hair Oil 200ml", "200 ml", 130.0, 7, 12, 730, 0),
        ("Sanitary Pads 8pc", "8 pc", 90.0, 6, 10, 900, 0),
        ("Hand Wash 250ml", "250 ml", 99.0, 8, 10, 730, 0),
        ("Talcum Powder 100g", "100 g", 75.0, 5, 12, 900, 0),
        ("Toothbrush 2pc", "2 pc", 60.0, 4, 10, 1080, 0),
    ],
    "Home Care": [
        ("Laundry Detergent 1kg", "1 kg", 215.0, 9, 11, 730, 0),
        ("Dish Wash Liquid 500ml", "500 ml", 105.0, 10, 11, 730, 0),
        ("Floor Cleaner 1L", "1 L", 165.0, 7, 11, 730, 0),
        ("Toilet Cleaner 500ml", "500 ml", 98.0, 6, 11, 730, 0),
        ("Glass Cleaner 500ml", "500 ml", 115.0, 3, 11, 730, 0),
        ("Fabric Softener 500ml", "500 ml", 145.0, 5, 11, 730, 0),
        ("Scrub Pad 3pc", "3 pc", 45.0, 8, 18, 1080, 0),
        ("Garbage Bags 30pc", "30 pc", 130.0, 6, 18, 1080, 0),
        ("Air Freshener 300ml", "300 ml", 175.0, 4, 11, 730, 0),
        ("Bleach 500ml", "500 ml", 55.0, 4, 11, 540, 0),
        ("Mop Refill 1pc", "1 pc", 85.0, 3, 18, 1080, 0),
        ("Aluminium Foil 10m", "10 m", 95.0, 5, 18, 1080, 0),
    ],
    "Staples": [
        ("Rice 5kg", "5 kg", 380.0, 12, 14, 365, 0),
        ("Wheat Flour 5kg", "5 kg", 245.0, 14, 8, 180, 0),
        ("Cooking Oil 1L", "1 L", 145.0, 24, 13, 270, 0),
        ("Sugar 1kg", "1 kg", 52.0, 20, 14, 365, 0),
        ("Toor Dal 1kg", "1 kg", 165.0, 15, 14, 270, 0),
        ("Salt 1kg", "1 kg", 24.0, 18, 14, 730, 0),
        ("Tea Powder 500g", "500 g", 260.0, 11, 8, 365, 0),
        ("Coffee 100g", "100 g", 210.0, 6, 7, 365, 0),
        ("Turmeric Powder 100g", "100 g", 38.0, 9, 8, 365, 0),
        ("Chilli Powder 200g", "200 g", 72.0, 8, 8, 365, 0),
        ("Basmati Rice 1kg", "1 kg", 155.0, 10, 14, 365, 0),
        ("Rajma 500g", "500 g", 95.0, 6, 14, 270, 0),
    ],
    "Frozen Foods": [
        ("Frozen Green Peas 500g", "500 g", 110.0, 8, 16, 240, 0),
        ("Frozen Fries 420g", "420 g", 145.0, 7, 16, 240, 0),
        ("Veg Spring Roll 300g", "300 g", 165.0, 4, 16, 180, 0),
        ("Chicken Nuggets 400g", "400 g", 285.0, 5, 16, 180, 0),
        ("Frozen Paratha 5pc", "5 pc", 120.0, 9, 16, 150, 0),
        ("Ice Cream Tub 700ml", "700 ml", 260.0, 6, 16, 180, 0),
        ("Frozen Corn 400g", "400 g", 95.0, 5, 16, 240, 0),
        ("Fish Fingers 250g", "250 g", 310.0, 3, 16, 180, 0),
        ("Frozen Pizza 300g", "300 g", 225.0, 4, 16, 180, 0),
        ("Frozen Momos 250g", "250 g", 155.0, 7, 16, 150, 0),
        ("Kulfi 4pc", "4 pc", 130.0, 5, 16, 120, 0),
        ("Frozen Spinach 500g", "500 g", 85.0, 4, 16, 240, 0),
    ],
    "Household Essentials": [
        ("Matchbox 10pc", "10 pc", 25.0, 14, 18, 1080, 0),
        ("Candles 6pc", "6 pc", 40.0, 6, 18, 1080, 0),
        ("Paper Plates 25pc", "25 pc", 95.0, 7, 18, 1080, 0),
        ("Tissue Roll 4pc", "4 pc", 120.0, 9, 18, 1080, 0),
        ("Kitchen Towel 2pc", "2 pc", 145.0, 5, 18, 1080, 0),
        ("Cling Film 20m", "20 m", 85.0, 4, 18, 1080, 0),
        ("Battery AA 4pc", "4 pc", 160.0, 6, 18, 1080, 0),
        ("Storage Bags 20pc", "20 pc", 110.0, 4, 18, 1080, 0),
        ("Mosquito Repellent 45ml", "45 ml", 95.0, 8, 12, 730, 0),
        ("Phenyl 1L", "1 L", 88.0, 7, 18, 730, 0),
        ("Broom 1pc", "1 pc", 130.0, 3, 18, 1080, 0),
        ("Dustbin Liners 30pc", "30 pc", 105.0, 6, 18, 1080, 0),
    ],
}

# Festival windows inside the simulated period (name, start, end, intensity)
FESTIVALS = [
    ("Diwali", date(END_DATE.year, 10, 26), date(END_DATE.year, 11, 6), 1.0),
    ("Independence Day", date(END_DATE.year, 8, 14), date(END_DATE.year, 8, 16), 0.45),
    ("Raksha Bandhan", date(END_DATE.year, 8, 27), date(END_DATE.year, 8, 29), 0.5),
    ("Ganesh Chaturthi", date(END_DATE.year, 9, 13), date(END_DATE.year, 9, 17), 0.6),
    ("Dussehra", date(END_DATE.year, 10, 19), date(END_DATE.year, 10, 21), 0.7),
]


def _festive_intensity(d: date) -> float:
    for _, s, e, i in FESTIVALS:
        if s <= d <= e:
            return i
    return 0.0


def _heat_intensity(d: date) -> float:
    """0..1 seasonal heat curve peaking late May (Hyderabad summer)."""
    day_of_year = d.timetuple().tm_yday
    return max(0.0, math.sin((day_of_year - 60) / 365 * 2 * math.pi) * 0.5 + 0.5)


# ==========================================================================
# Seeding steps
# ==========================================================================
def seed_reference_data() -> dict[str, int]:
    log.info("seeding reference data")
    ids: dict[str, int] = {}

    execute("DELETE FROM roles")
    for rid, code, name, desc, perms in [
        (1, "STORE_EMPLOYEE", "Store Employee", "Front-of-store scanning, receiving and stock lookup",
         "scan:sale,receive:goods,inventory:read,alerts:read"),
        (2, "STORE_MANAGER", "Store Manager", "Full control of a single store",
         "scan:sale,receive:goods,inventory:read,inventory:adjust,po:create,po:approve,"
         "alerts:manage,prediction:read,reports:export,kpi:read"),
        (3, "REGIONAL_MANAGER", "Regional Manager", "Multi-store comparison and oversight",
         "inventory:read,po:approve,alerts:manage,prediction:read,reports:export,kpi:read,supplier:read"),
        (4, "HEAD_OFFICE_ADMIN", "Head Office Administrator", "Chain-wide administration",
         "*,users:manage,settings:manage,sync:manage"),
    ]:
        execute(
            "INSERT INTO roles (role_id, role_code, role_name, description, permissions) "
            "VALUES (:a,:b,:c,:d,:e)",
            {"a": rid, "b": code, "c": name, "d": desc, "e": perms},
        )
    ids["role_count"] = 4

    # categories
    for name, shelf, dm, wk, heat, fest in CATEGORIES:
        execute(
            "INSERT INTO product_categories (category_name, shelf_life_days, demand_multiplier, "
            "weekend_factor, heat_sensitivity, festival_factor) VALUES (:a,:b,:c,:d,:e,:f)",
            {"a": name, "b": shelf, "c": dm, "d": wk, "e": heat, "f": fest},
        )

    # suppliers
    for code, name, city, lead, var, rel, mov, asn in SUPPLIERS:
        execute(
            "INSERT INTO suppliers (supplier_code, supplier_name, contact_email, contact_phone, city, "
            "lead_time_days, lead_time_variability, reliability_score, min_order_value, supports_asn) "
            "VALUES (:a,:b,:c,:d,:e,:f,:g,:h,:i,:j)",
            {
                "a": code, "b": name,
                "c": code.lower() + "@supplier.example",
                "d": "+91-40-4" + str(py_rng.randint(1000000, 9999999)),
                "e": city, "f": lead, "g": var, "h": rel, "i": mov, "j": asn,
            },
        )

    # stores
    for (code, name, city, region, fmt, area, dem, cv, lat, lon) in STORES:
        execute(
            "INSERT INTO stores (store_code, store_name, city, state, region, store_format, "
            "floor_area_sqm, opening_date, latitude, longitude, local_node_id) "
            "VALUES (:a,:b,:c,'Telangana',:d,:e,:f,:g,:h,:i,:j)",
            {
                "a": code, "b": name, "c": city, "d": region, "e": fmt, "f": area,
                "g": (END_DATE - timedelta(days=py_rng.randint(900, 3000))).isoformat(),
                "h": lat, "i": lon, "j": f"NODE-{code}",
            },
        )

    # sku master
    cat_ids = {r["category_name"]: r["category_id"] for r in
               query("SELECT category_id, category_name FROM product_categories").to_dict("records")}
    sup_ids = {r["supplier_code"]: r["supplier_id"] for r in
               query("SELECT supplier_id, supplier_code FROM suppliers").to_dict("records")}
    sup_codes = [s[0] for s in SUPPLIERS]

    sku_rows = []
    sku_no = 0
    for cat_name, products in PRODUCTS.items():
        for (pname, pack, price, base_demand, sup_idx, shelf, perish) in products:
            sku_no += 1
            code = f"FMCG{sku_no:03d}"
            barcode = f"890{sku_no:010d}"
            supplier_code = sup_codes[sup_idx - 1]
            unit_cost = round(price * py_rng.uniform(0.66, 0.80), 2)
            lead = SUPPLIERS[sup_idx - 1][3]
            reorder = max(8, int(round(base_demand * lead * 0.85)))
            safety = max(4, int(round(reorder * 0.35)))
            max_level = int(round(base_demand * (lead + 14) * 1.25))
            sku_rows.append((
                code, barcode, pname, cat_ids[cat_name], sup_ids[supplier_code], pack, "EA",
                unit_cost, price, round(price * 1.08, 2), reorder, safety, max_level,
                py_rng.choice([6, 12, 12, 24]), shelf, perish,
                (END_DATE - timedelta(days=py_rng.randint(120, 1500))).isoformat(),
            ))
    executemany(
        "INSERT INTO sku (sku_code, barcode, product_name, category_id, supplier_id, pack_size, uom, "
        "unit_cost, selling_price, mrp, reorder_level, safety_stock, max_stock_level, case_pack, "
        "shelf_life_days, is_perishable, launch_date) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        sku_rows,
    )
    ids["sku_count"] = len(sku_rows)

    # users
    store_ids = {r["store_code"]: r["store_id"] for r in
                 query("SELECT store_id, store_code FROM stores").to_dict("records")}
    for u in DEMO_USERS:
        execute(
            "INSERT INTO users (username, password_hash, full_name, email, role_id, store_id) "
            "VALUES (:a,:b,:c,:d,(SELECT role_id FROM roles WHERE role_code = :e),:f)",
            {
                "a": u["username"], "b": hash_password(u["password"]), "c": u["full_name"],
                "d": u["email"], "e": u["role"], "f": store_ids.get(u["store_code"]),
            },
        )
    ids["user_count"] = len(DEMO_USERS)

    # system config values
    for k, v in [
        ("chain_name", "FreshMart Supermarkets"),
        ("prediction_horizon_days", "7"),
        ("risk_band_low_max", "0.30"),
        ("risk_band_moderate_max", "0.60"),
        ("risk_band_high_max", "0.80"),
        ("alert_sla_minutes", "5"),
        ("data_classification", "SIMULATED_DEMO_DATA"),
    ]:
        execute("INSERT INTO app_config (config_key, config_value, updated_by) VALUES (:k,:v,'seed')",
                {"k": k, "v": v})
    return ids


def simulate_operations() -> pd.DataFrame:
    """Step the inventory state machine for every store x SKU x day."""
    log.info("simulating %d days of store operations …", HISTORY_DAYS)
    stores = query("SELECT store_id, store_code, store_name, region, store_format FROM stores "
                   "ORDER BY store_id").to_dict("records")
    sku = query(
        "SELECT k.sku_id, k.sku_code, k.barcode, k.product_name, k.category_id, k.supplier_id, "
        "k.unit_cost, k.selling_price, k.reorder_level, k.safety_stock, k.max_stock_level, "
        "k.case_pack, k.is_perishable, k.shelf_life_days, c.category_name, c.demand_multiplier, "
        "c.weekend_factor, c.heat_sensitivity, c.festival_factor, s.lead_time_days, "
        "s.lead_time_variability, s.reliability_score "
        "FROM sku k JOIN product_categories c ON c.category_id = k.category_id "
        "JOIN suppliers s ON s.supplier_id = k.supplier_id ORDER BY k.sku_id"
    )
    sku_by_id = {r["sku_id"]: r for r in sku.to_dict("records")}
    base_demand = {}
    n = 0
    for cat_name, products in PRODUCTS.items():
        for p in products:
            n += 1
            base_demand[n] = p[3]

    store_meta = {s[0]: s for s in STORES}
    days = [START_DATE + timedelta(days=i) for i in range(HISTORY_DAYS)]
    ledger_start_idx = HISTORY_DAYS - LEDGER_WINDOW_DAYS

    daily_rows: list[tuple] = []
    ledger_rows: list[tuple] = []
    po_log: list[dict] = []

    for st in stores:
        meta = store_meta[st["store_code"]]
        demand_factor, cv_store = meta[6], meta[7]
        for sk in sku_by_id.values():
            sid, kid = st["store_id"], sk["sku_id"]
            bd = base_demand[kid]
            base = bd * demand_factor * sk["demand_multiplier"] * py_rng.uniform(0.85, 1.15)
            lead = int(sk["lead_time_days"])
            var = float(sk["lead_time_variability"])
            reliability = float(sk["reliability_score"])
            reorder = sk["reorder_level"]
            safety = sk["safety_stock"]
            max_level = sk["max_stock_level"]
            perishable = bool(sk["is_perishable"])
            shelf_life = sk["shelf_life_days"] or 3650

            # opening stock for day 0
            stock = int(round(max_level * py_rng.uniform(0.55, 0.92)))

            # promo calendar for this store/SKU (discrete promotional bursts)
            promo_days: set[int] = set()
            for _ in range(py_rng.randint(3, 6)):
                start = py_rng.randint(0, HISTORY_DAYS - 6)
                for k in range(start, min(HISTORY_DAYS, start + py_rng.randint(3, 6))):
                    promo_days.add(k)

            outstanding: list[dict] = []   # POs in flight
            opening_day = stock

            for t, d in enumerate(days):
                heat = _heat_intensity(d)
                fest = _festive_intensity(d)
                dow = d.weekday()

                mult = 1.0
                mult *= sk["weekend_factor"] if dow >= 5 else (0.96 if dow == 0 else 1.0)
                mult *= 1.0 + sk["heat_sensitivity"] * (heat - 0.45) * 1.4
                mult *= 1.0 + (sk["festival_factor"] - 1.0) * fest
                is_promo = 1 if t in promo_days else 0
                if is_promo:
                    mult *= py_rng.uniform(1.35, 1.85)

                # true (unconstrained) demand
                mu = max(0.4, base * mult)
                sigma = mu * (cv_store * py_rng.uniform(0.85, 1.2))
                demand = int(max(0, round(rng.normal(mu, sigma))))

                # ---- replenishment decision (before the day's sales) ----------
                pending_before = sum(o["qty"] for o in outstanding if o["arrive_idx"] > t)
                if stock + pending_before <= reorder:
                    qty = max(0, max_level - (stock + pending_before))
                    if qty > 0:
                        qty = int(math.ceil(qty / sk["case_pack"]) * sk["case_pack"])
                        late = py_rng.random() > reliability
                        delay = py_rng.randint(1, 4) if late else 0
                        arrive = t + lead + delay
                        outstanding.append({
                            "order_idx": t, "arrive_idx": arrive, "expected_idx": t + lead,
                            "qty": qty, "received": 0,
                        })
                        po_log.append({
                            "store_id": sid, "sku_id": kid, "supplier_id": sk["supplier_id"],
                            "order_idx": t, "expected_idx": t + lead, "arrive_idx": arrive,
                            "qty_ordered": qty, "qty_received": 0,
                            "unit_cost": sk["unit_cost"],
                        })

                # ---- receipts --------------------------------------------------
                receipts = 0
                for o in outstanding:
                    if o["arrive_idx"] == t and o["received"] == 0:
                        short = 0
                        if py_rng.random() < 0.06:            # short delivery
                            short = int(round(o["qty"] * py_rng.uniform(0.05, 0.18)))
                        got = max(0, o["qty"] - short)
                        o["received"] = got
                        o["qty_received"] = got
                        receipts += got
                        for pl in reversed(po_log):
                            if (pl["store_id"] == sid and pl["sku_id"] == kid
                                    and pl["order_idx"] == o["order_idx"] and pl["arrive_idx"] == t):
                                pl["qty_received"] = got
                                break

                available = stock + receipts
                sold = min(demand, available)
                unmet = demand - sold
                stock_after_sales = available - sold

                # ---- waste / damage on perishables ----------------------------
                damaged = 0
                if perishable and stock_after_sales > 0:
                    waste_rate = 0.004 + 0.010 * (1 - min(shelf_life, 10) / 10)
                    if py_rng.random() < waste_rate * 3:
                        damaged = min(stock_after_sales, py_rng.randint(1, max(1, int(base * 0.12))))
                stock_after = stock_after_sales - damaged
                oos = 1 if (sold < demand or stock_after_sales <= 0) else 0

                pending_po = sum(o["qty"] - o["received"] for o in outstanding
                                 if o["arrive_idx"] > t and o["received"] == 0)

                daily_rows.append((
                    sid, kid, d.isoformat(), sold, round(sold * sk["selling_price"], 2),
                    round(sold * sk["unit_cost"], 2), unmet, demand + 0, stock_after,
                    opening_day, receipts, is_promo, oos, 1 if receipts > 0 else 0,
                    pending_po, damaged,
                ))

                # ---- append-only ledger (only the retained window) -------------
                if t == ledger_start_idx:
                    ledger_rows.append((
                        new_event_id(), sid, kid, "OPENING", opening_day, None, None,
                        "PERIOD_OPENING", f"{d.isoformat()}T00:05:00", datetime.utcnow().isoformat(),
                        f"NODE-{st['store_code']}", None, None, 0,
                        f"Opening balance carried into {LEDGER_WINDOW_DAYS}-day ledger window",
                    ))
                if t >= ledger_start_idx:
                    if receipts > 0:
                        ledger_rows.append((
                            new_event_id(), sid, kid, "RECEIPT", receipts, "goods_receipts", None,
                            "PO_RECEIPT", f"{d.isoformat()}T{py_rng.randint(7, 11):02d}:{py_rng.randint(0,59):02d}:00",
                            datetime.utcnow().isoformat(), f"NODE-{st['store_code']}", None, None, 0, None,
                        ))
                    if sold > 0:
                        ledger_rows.append((
                            new_event_id(), sid, kid, "SALE", -sold, "sales_daily", f"{sid}-{kid}-{d.isoformat()}",
                            "POS_DAILY_SALES", f"{d.isoformat()}T{py_rng.randint(9, 22):02d}:{py_rng.randint(0,59):02d}:00",
                            datetime.utcnow().isoformat(), f"NODE-{st['store_code']}", None, None, 0, None,
                        ))
                    if damaged > 0:
                        ledger_rows.append((
                            new_event_id(), sid, kid, "DAMAGE", -damaged, None, None,
                            "EXPIRY_WASTE", f"{d.isoformat()}T22:30:00", datetime.utcnow().isoformat(),
                            f"NODE-{st['store_code']}", None, None, 0, "Perishable write-off",
                        ))

                opening_day = stock_after
                stock = stock_after
                outstanding = [o for o in outstanding if o["received"] == 0 or o["arrive_idx"] > t]

    df = pd.DataFrame(daily_rows, columns=[
        "store_id", "sku_id", "business_date", "units_sold", "gross_revenue", "cost_of_goods",
        "demand_unmet", "demand_total", "closing_stock", "opening_stock", "receipts_qty",
        "promo_flag", "stockout_flag", "was_replenished", "pending_po_qty", "damage_qty",
    ])
    log.info("simulation complete: %s store-SKU-day rows, %s ledger events",
             f"{len(df):,}", f"{len(ledger_rows):,}")
    return df, ledger_rows, po_log


def persist_daily(df: pd.DataFrame) -> None:
    log.info("writing sales_daily …")
    cols = ["store_id", "sku_id", "business_date", "units_sold", "gross_revenue", "cost_of_goods",
            "demand_unmet", "demand_total", "closing_stock", "opening_stock", "receipts_qty",
            "promo_flag", "stockout_flag", "was_replenished", "pending_po_qty", "damage_qty"]
    executemany(
        f"INSERT INTO sales_daily ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
        list(df[cols].itertuples(index=False, name=None)),
    )


def persist_ledger(ledger_rows: list[tuple]) -> None:
    log.info("writing append-only stock_event ledger (%s rows) …", f"{len(ledger_rows):,}")
    executemany(
        "INSERT INTO stock_event (event_id, store_id, sku_id, event_type, quantity_delta, "
        "reference_table, reference_id, reason_code, occurred_at, recorded_at, device_id, "
        "operator_id, sync_batch_id, is_replayed, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ledger_rows,
    )


def persist_stock_on_hand(df: pd.DataFrame) -> None:
    log.info("deriving stock_on_hand …")
    last = df.sort_values("business_date").groupby(["store_id", "sku_id"]).tail(1)
    rows = [(int(r.store_id), int(r.sku_id), int(r.closing_stock), 0, 0,
             r.business_date) for r in last.itertuples()]
    executemany(
        "INSERT INTO stock_on_hand (store_id, sku_id, on_hand_qty, reserved_qty, in_transit_qty, "
        "last_event_at) VALUES (?,?,?,?,?,?)",
        rows,
    )


def persist_purchase_orders(po_log: list[dict]) -> None:
    """Historical POs from the simulation + a set of live open POs for today."""
    log.info("writing purchase orders and goods receipts …")
    sku = query("SELECT sku_id, sku_code, unit_cost, lead_time_days FROM sku k "
                "JOIN suppliers s ON s.supplier_id = k.supplier_id").set_index("sku_id")
    storecode = query("SELECT store_id, store_code FROM stores").set_index("store_id")["store_code"]

    # group historical orders into PO documents per (store, supplier, order date)
    buckets: dict[tuple, list[dict]] = {}
    for pl in po_log:
        if pl["order_idx"] < HISTORY_DAYS - 45:
            continue
        key = (pl["store_id"], pl["supplier_id"], pl["order_idx"])
        buckets.setdefault(key, []).append(pl)

    po_rows, item_rows, receipts, receipt_items = [], [], [], []
    po_seq = 0
    rc_seq = 0
    today_idx = HISTORY_DAYS - 1

    for (sid, sup_id, oidx), lines in sorted(buckets.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        po_seq += 1
        order_date = START_DATE + timedelta(days=oidx)
        expected = START_DATE + timedelta(days=lines[0]["expected_idx"])
        arrived = max(l["arrive_idx"] for l in lines)
        received_all = all(l["qty_received"] > 0 for l in lines)
        partial = any(l["qty_received"] > 0 for l in lines) and not received_all

        if received_all:
            status = "RECEIVED"
        elif partial:
            status = "PARTIALLY_RECEIVED"
        elif arrived <= today_idx:
            status = "SUBMITTED"
        else:
            status = "SUBMITTED"

        total_value = round(sum(l["qty_ordered"] * l["unit_cost"] for l in lines), 2)
        actual_delivery = (START_DATE + timedelta(days=arrived)).isoformat() if received_all else None
        po_rows.append((
            f"PO-{order_date.strftime('%Y%m')}-{po_seq:05d}", sid, sup_id, status,
            order_date.isoformat(), expected.isoformat(), actual_delivery, total_value,
            None, f"ASN-{po_seq:06d}" if py_rng.random() < 0.8 else None, 
            "Simulated replenishment",
        ))

    executemany(
        "INSERT INTO purchase_orders (po_number, store_id, supplier_id, status, order_date, "
        "expected_date, actual_delivery_date, total_value, raised_by, asn_number, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        po_rows,
    )

    po_ids = query("SELECT po_id, po_number FROM purchase_orders").set_index("po_number")["po_id"]
    po_seq = 0
    for (sid, sup_id, oidx), lines in sorted(buckets.items(), key=lambda kv: (kv[0][2], kv[0][0])):
        po_seq += 1
        order_date = START_DATE + timedelta(days=oidx)
        po_number = f"PO-{order_date.strftime('%Y%m')}-{po_seq:05d}"
        pid = int(po_ids[po_number])
        for l in lines:
            item_rows.append((pid, l["sku_id"], l["qty_ordered"], l["qty_received"],
                              l["unit_cost"], round(l["qty_ordered"] * l["unit_cost"], 2)))
        if any(l["qty_received"] > 0 for l in lines):
            rc_seq += 1
            arrived_idx = max(l["arrive_idx"] for l in lines)
            rdate = START_DATE + timedelta(days=arrived_idx)
            receipt_id_holder = f"GRN-{rdate.strftime('%Y%m%d')}-{rc_seq:05d}"
            short = any(l["qty_received"] < l["qty_ordered"] for l in lines)
            receipts.append((
                receipt_id_holder, pid, sid, sup_id,
                f"{rdate.isoformat()}T{py_rng.randint(8, 11):02d}:{py_rng.randint(0,59):02d}:00",
                None, f"DN-{rc_seq:06d}", "CONFIRMED", 1 if short else 0,
                "Short delivery vs ordered quantity" if short else None, new_event_id(),
            ))
            for l in lines:
                receipt_items.append((receipt_id_holder, l["sku_id"], 0,
                                      max(0, l["qty_received"]),
                                      max(0, l["qty_ordered"] - l["qty_received"]), None, None))

    executemany(
        "INSERT INTO goods_receipts (receipt_number, po_id, store_id, supplier_id, received_at, "
        "received_by, delivery_note_no, status, discrepancy_flag, discrepancy_note, event_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        receipts,
    )
    gr_ids = query("SELECT receipt_id, receipt_number FROM goods_receipts").set_index("receipt_number")["receipt_id"]
    # items keyed by receipt number -> need receipt_id
    rc_map = {}
    for row in receipts:
        rc_map[row[0]] = int(gr_ids[row[0]])
    rows_gr_items = []
    for r in receipt_items:
        rows_gr_items.append((rc_map[r[0]], r[1], r[2], r[3], r[4], r[5], r[6]))
    executemany(
        "INSERT INTO goods_receipt_items (receipt_id, sku_id, po_item_id, expected_qty, "
        "received_qty, rejected_qty, batch_code, expiry_date) VALUES (?,?,?,?,?,?,?,?)",
        [(r[0], r[1], None, 0, max(0, r[3]), max(0, r[4]), None, None) for r in rows_gr_items],
    )
    log.info("  %s purchase orders, %s goods receipts", f"{len(po_rows):,}", f"{len(receipts):,}")


def create_open_purchase_orders() -> None:
    """Live POs in flight for today – these populate in_transit / pending-PO features."""
    log.info("creating open purchase orders for today …")
    need = query(
        "SELECT soh.store_id, soh.sku_id, soh.on_hand_qty, k.reorder_level, k.max_stock_level, "
        "k.unit_cost, k.supplier_id, s.lead_time_days, s.reliability_score, st.store_code "
        "FROM stock_on_hand soh JOIN sku k ON k.sku_id = soh.sku_id "
        "JOIN suppliers s ON s.supplier_id = k.supplier_id "
        "JOIN stores st ON st.store_id = soh.store_id "
        "WHERE soh.on_hand_qty <= k.reorder_level * 1.15 "
        "ORDER BY soh.on_hand_qty * 1.0 / NULLIF(k.reorder_level,0)"
    )
    if need.empty:
        return
    # cap so the demo has a realistic number of open POs
    need = need.head(90)
    groups: dict[tuple, list] = {}
    for r in need.to_dict("records"):
        groups.setdefault((r["store_id"], r["supplier_id"]), []).append(r)

    seq = scalar("SELECT COUNT(*) FROM purchase_orders", default=0)
    po_rows, item_rows, transit = [], [], []
    for (sid, sup_id), lines in groups.items():
        seq += 1
        order_date = END_DATE
        lead = max(int(l["lead_time_days"]) for l in lines)
        expected = END_DATE + timedelta(days=lead)
        total = sum(max(0, l["max_stock_level"] - l["on_hand_qty"]) * l["unit_cost"] for l in lines)
        po_rows.append((
            f"PO-{order_date.strftime('%Y%m')}-{seq:05d}", sid, sup_id, "SUBMITTED",
            order_date.isoformat(), expected.isoformat(), None, round(total, 2),
            None, None, "Auto-generated replenishment request (simulated)",
        ))
        for l in lines:
            qty = int(max(0, l["max_stock_level"] - l["on_hand_qty"]))
            item_rows.append((f"PO-{order_date.strftime('%Y%m')}-{seq:05d}", l["sku_id"], qty,
                              l["unit_cost"], round(qty * l["unit_cost"], 2)))
            transit.append((qty, sid, l["sku_id"]))
    executemany(
        "INSERT INTO purchase_orders (po_number, store_id, supplier_id, status, order_date, "
        "expected_date, actual_delivery_date, total_value, raised_by, asn_number, notes) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        po_rows,
    )
    po_ids = query("SELECT po_id, po_number FROM purchase_orders").set_index("po_number")["po_id"]
    rows = []
    for (po_number, sku_id, qty, cost, val) in item_rows:
        rows.append((int(po_ids[po_number]), sku_id, qty, 0, cost, val))
    executemany(
        "INSERT INTO purchase_order_items (po_id, sku_id, ordered_qty, received_qty, unit_cost, "
        "line_value) VALUES (?,?,?,?,?,?)", rows,
    )
    for qty, sid, kid in transit:
        execute("UPDATE stock_on_hand SET in_transit_qty = in_transit_qty + :q "
                "WHERE store_id = :s AND sku_id = :k", {"q": qty, "s": sid, "k": kid})
    log.info("  %s open PO documents created", len(po_rows))


def persist_pos_transactions(df: pd.DataFrame) -> None:
    """Item-level POS receipts for the most recent days (operational drill-down)."""
    log.info("generating POS transactions for the last %d days …", POS_DETAIL_DAYS)
    sku = query("SELECT sku_id, selling_price FROM sku").set_index("sku_id")["selling_price"].to_dict()
    storecode = query("SELECT store_id, store_code FROM stores").set_index("store_id")["store_code"].to_dict()
    start = (END_DATE - timedelta(days=POS_DETAIL_DAYS - 1)).isoformat()
    recent = df[df["business_date"] >= start]

    txn_rows, item_rows = [], []
    txn_seq = 0
    for (sid, bdate), grp in recent.groupby(["store_id", "business_date"]):
        tokens: list[tuple[int, int, float]] = []
        for r in grp.itertuples():
            q = int(r.units_sold)
            while q > 0:
                c = min(q, py_rng.randint(1, 5))
                tokens.append((int(r.sku_id), c, sku.get(int(r.sku_id), 0.0)))
                q -= c
        if not tokens:
            continue
        py_rng.shuffle(tokens)
        i = 0
        while i < len(tokens):
            size = py_rng.randint(5, 12)
            chunk = tokens[i:i + size]
            i += size
            txn_seq += 1
            gross = round(sum(c * p for _, c, p in chunk), 2)
            receipt_no = f"{storecode[sid]}-{bdate.replace('-', '')}-{txn_seq:06d}"
            item_count = sum(c for _, c, _ in chunk)
            txn_time = f"{bdate}T{py_rng.randint(8, 22):02d}:{py_rng.randint(0,59):02d}:{py_rng.randint(0,59):02d}"
            txn_rows.append((receipt_no, int(sid), bdate, txn_time, None, "POS", item_count,
                             gross, 0.0, gross, new_event_id(), "COMPLETED"))
            for kid, c, p in chunk:
                item_rows.append((receipt_no, kid, c, p, round(c * p, 2), 0.0))

    executemany(
        "INSERT INTO sales_transactions (receipt_no, store_id, business_date, txn_time, cashier_id, "
        "channel, item_count, gross_amount, discount_amount, net_amount, event_id, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", txn_rows,
    )
    tx_ids = query("SELECT transaction_id, receipt_no FROM sales_transactions "
                   "WHERE receipt_no LIKE :p", {"p": f"{END_DATE.year}%"})
    txn_map = query("SELECT transaction_id, receipt_no FROM sales_transactions"
                    ).set_index("receipt_no")["transaction_id"].to_dict()
    rows = []
    for (receipt_no, kid, c, p, amt, disc) in item_rows:
        tid = txn_map.get(receipt_no)
        if tid is None:
            continue
        rows.append((int(tid), kid, c, p, amt, disc))
    executemany(
        "INSERT INTO sales_transaction_items (transaction_id, sku_id, quantity, unit_price, "
        "line_amount, discount_amount) VALUES (?,?,?,?,?,?)", rows,
    )
    log.info("  %s transactions / %s items", f"{len(txn_rows):,}", f"{len(rows):,}")


def persist_stockout_events(df: pd.DataFrame) -> None:
    """Convert consecutive zero-stock runs into stockout episodes."""
    log.info("deriving stockout episodes …")
    d = df.sort_values(["store_id", "sku_id", "business_date"]).copy()
    d["oos"] = (d["closing_stock"] <= 0).astype(int)
    d["run"] = (d["oos"] != d.groupby(["store_id", "sku_id"])["oos"].shift()).cumsum()
    price = query("SELECT sku_id, selling_price FROM sku").set_index("sku_id")["selling_price"].to_dict()
    rows = []
    for (sid, kid, run), g in d[d["oos"] == 1].groupby(["store_id", "sku_id", "run"]):
        lost = int(g["demand_unmet"].sum())
        rows.append((
            int(sid), int(kid), "ACTUAL", "CRITICAL", g["business_date"].min(),
            g["business_date"].max(), float(len(g)) * 24.0, lost,
            round(lost * price.get(int(kid), 0.0), 2), None, None,
        ))
    executemany(
        "INSERT INTO stockout_event (store_id, sku_id, detection_type, severity, started_at, "
        "ended_at, duration_hours, units_lost, revenue_lost, root_cause, linked_alert_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    log.info("  %s stockout episodes", f"{len(rows):,}")


def persist_store_kpis(df: pd.DataFrame) -> None:
    """Daily, per-store KPI fact with a cross-sectional normalised score."""
    log.info("computing store KPI daily …")
    sku_cost = query("SELECT sku_id, unit_cost, selling_price FROM sku").set_index("sku_id")
    d = df.copy()
    d["inv_value"] = d["closing_stock"] * d["sku_id"].map(sku_cost["unit_cost"])
    d["inv_units"] = d["closing_stock"]
    d["cogs"] = d["cost_of_goods"]
    d["waste_value"] = d["damage_qty"] * d["sku_id"].map(sku_cost["unit_cost"])
    d["loss_value"] = d["demand_unmet"] * d["sku_id"].map(sku_cost["selling_price"])

    agg = d.groupby(["store_id", "business_date"]).agg(
        sales_revenue=("gross_revenue", "sum"),
        units_sold=("units_sold", "sum"),
        inventory_value=("inv_value", "sum"),
        inventory_units=("inv_units", "sum"),
        cogs=("cogs", "sum"),
        waste_value=("waste_value", "sum"),
        stockout_loss_value=("loss_value", "sum"),
        skus=("sku_id", "count"),
        oos_skus=("stockout_flag", "sum"),
    ).reset_index()
    agg["stockout_frequency"] = (agg["oos_skus"] / agg["skus"] * 100).round(2)
    agg["stock_availability_pct"] = (100 - agg["stockout_frequency"]).round(2)

    # trailing 28-day views for turnover, availability and forecast accuracy
    agg = agg.sort_values(["store_id", "business_date"])
    g = agg.groupby("store_id", sort=False)
    agg["avg_inventory_value"] = g["inventory_value"].transform(
        lambda s: s.rolling(28, min_periods=7).mean()).round(2)
    agg["cogs_28"] = g["cogs"].transform(lambda s: s.rolling(28, min_periods=7).sum())
    agg["turnover_28"] = (agg["cogs_28"] / agg["avg_inventory_value"].replace(0, np.nan) * 365 / 28).round(2)
    agg["avail_28"] = g["stock_availability_pct"].transform(
        lambda s: s.rolling(7, min_periods=3).mean()).round(2)

    # forecast accuracy: 7-day moving average forecast vs realised units (store level)
    agg["fcst"] = g["units_sold"].transform(lambda s: s.shift(1).rolling(7, min_periods=3).mean())
    err = (agg["units_sold"] - agg["fcst"]).abs() / agg["units_sold"].replace(0, np.nan)
    agg["fc"] = (100 - (err.fillna(0) * 100)).clip(lower=0, upper=100)
    agg["forecast_accuracy_pct"] = g["fc"].transform(
        lambda s: s.rolling(7, min_periods=3).mean()).round(2)

    # replenishment efficiency: share of POs received on/before expected date (trailing)
    po = query("SELECT store_id, order_date, expected_date, actual_delivery_date FROM purchase_orders "
               "WHERE actual_delivery_date IS NOT NULL")
    if not po.empty:
        po["on_time"] = (po["actual_delivery_date"] <= po["expected_date"]).astype(int)
        po_by_day = po.groupby(["store_id", "order_date"]).agg(
            ot=("on_time", "sum"), n=("on_time", "count")).reset_index()
        po_by_day = po_by_day.rename(columns={"order_date": "business_date"})
        agg = agg.merge(po_by_day, on=["store_id", "business_date"], how="left")
        agg["repl_eff"] = (agg["ot"] / agg["n"] * 100)
    else:
        agg["repl_eff"] = np.nan
    agg["repl_eff"] = agg.groupby("store_id")["repl_eff"].transform(
        lambda s: s.rolling(30, min_periods=1).mean())
    agg["repl_eff"] = agg["repl_eff"].fillna(agg["avail_28"]).round(2)

    # ---- cross-sectional normalisation per day, then weighted score -------
    def norm(col, higher_better=True):
        def _f(s):
            lo, hi = s.min(), s.max()
            if not np.isfinite(lo) or hi == lo:
                return pd.Series([50.0] * len(s), index=s.index)
            z = (s - lo) / (hi - lo) * 100 if higher_better else (hi - s) / (hi - lo) * 100
            return z.clip(0, 100)
        return agg.groupby("business_date")[col].transform(_f)

    agg["n_sales"] = norm("sales_revenue", True)
    agg["n_avail"] = norm("avail_28", True)
    agg["n_turn"] = norm("turnover_28", True)
    agg["n_fc"] = norm("forecast_accuracy_pct", True)
    agg["n_repl"] = norm("repl_eff", True)
    agg["performance_score"] = (
        agg["n_sales"] * KPI_WEIGHTS["sales_performance"]
        + agg["n_avail"] * KPI_WEIGHTS["stock_availability"]
        + agg["n_turn"] * KPI_WEIGHTS["inventory_efficiency"]
        + agg["n_fc"] * KPI_WEIGHTS["forecast_accuracy"]
        + agg["n_repl"] * KPI_WEIGHTS["replenishment_efficiency"]
    ).round(2)

    # warm-up days have no 28-day history -> persist 0 instead of NULL
    for _c in ["avg_inventory_value", "turnover_28", "avail_28",
               "forecast_accuracy_pct", "repl_eff", "performance_score"]:
        agg[_c] = pd.to_numeric(agg[_c], errors="coerce").fillna(0.0)

    rows = []
    for r in agg.itertuples():
        rows.append((
            int(r.store_id), r.business_date, round(float(r.sales_revenue), 2), int(r.units_sold), 0,
            round(float(r.inventory_value), 2), int(r.inventory_units),
            round(float(r.avg_inventory_value), 2), float(r.turnover_28),
            float(r.stockout_frequency), float(r.avail_28),
            float(r.forecast_accuracy_pct), float(r.repl_eff),
            round(float(r.waste_value), 2), round(float(r.stockout_loss_value), 2),
            float(r.performance_score),
        ))
    executemany(
        "INSERT INTO store_kpi_daily (store_id, business_date, sales_revenue, units_sold, transactions, "
        "inventory_value, inventory_units, avg_inventory_value, inventory_turnover, stockout_frequency, "
        "stock_availability_pct, forecast_accuracy_pct, replenishment_efficiency, waste_value, "
        "stockout_loss_value, performance_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
    )
    log.info("  %s KPI rows", f"{len(rows):,}")


def persist_supplier_performance() -> None:
    log.info("computing supplier performance …")
    sup = query("SELECT supplier_id, supplier_name, lead_time_days FROM suppliers")
    po = query(
        "SELECT po.store_id, po.supplier_id, po.order_date, po.expected_date, "
        "po.actual_delivery_date, poi.ordered_qty, poi.received_qty "
        "FROM purchase_orders po JOIN purchase_order_items poi ON poi.po_id = po.po_id"
    )
    oos = query("SELECT sku_id, COUNT(*) AS n FROM stockout_event GROUP BY sku_id")
    sku_sup = query("SELECT sku_id, supplier_id FROM sku").set_index("sku_id")["supplier_id"].to_dict()
    oos_sup: dict[int, int] = {}
    for r in oos.to_dict("records"):
        s = sku_sup.get(int(r["sku_id"]))
        if s:
            oos_sup[s] = oos_sup.get(s, 0) + int(r["n"])

    if po.empty:
        return
    po["on_time"] = (po["actual_delivery_date"].notna()
                     & (po["actual_delivery_date"] <= po["expected_date"])).astype(int)
    po["delivered"] = po["actual_delivery_date"].notna().astype(int)

    rows = []
    for r in sup.to_dict("records"):
        sub = po[po["supplier_id"] == r["supplier_id"]]
        if sub.empty:
            continue
        orders = sub.groupby(["store_id", "order_date"]).ngroups
        delivered = sub[sub["delivered"] == 1]
        on_time = int(delivered["on_time"].sum())
        late = len(delivered) - on_time
        lead_actual = []
        for (sid, od, ed, ad, _, _, _) in delivered[
                ["store_id", "order_date", "expected_date", "actual_delivery_date",
                 "ordered_qty", "received_qty", "supplier_id"]].itertuples(index=False, name=None):
            try:
                lead_actual.append((date.fromisoformat(ad) - date.fromisoformat(od)).days)
            except Exception:
                pass
        ordered = float(sub["ordered_qty"].sum())
        received = float(sub["received_qty"].sum())
        fulfilment = 100 * received / ordered if ordered else 0
        discrepancy = int(max(0, ordered - received))
        on_time_pct = 100 * on_time / len(delivered) if len(delivered) else 0
        rows.append((
            r["supplier_id"], None, START_DATE.isoformat(), END_DATE.isoformat(), orders,
            round(float(np.mean(lead_actual)) if lead_actual else r["lead_time_days"], 2),
            on_time, late, round(on_time_pct, 2), round(fulfilment, 2), discrepancy,
            oos_sup.get(r["supplier_id"], 0),
            round(on_time_pct * 0.6 + fulfilment * 0.4, 2),
        ))
    executemany(
        "INSERT INTO supplier_performance (supplier_id, store_id, period_start, period_end, orders_count, "
        "avg_lead_time_days, on_time_deliveries, late_deliveries, on_time_pct, fulfilment_pct, "
        "qty_discrepancy_units, stockout_contribution, otif_score) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    log.info("  %s supplier performance rows", len(rows))


def write_audit(action: str, detail: str) -> None:
    execute(
        "INSERT INTO audit_logs (user_id, username, action, entity, entity_id, store_id, detail, result) "
        "VALUES (NULL,'system',:a,'SYSTEM','seed',NULL,:d,'SUCCESS')",
        {"a": action, "d": detail},
    )


def build_demo_transaction_logs(df: pd.DataFrame) -> None:
    """A few synchronization batches so the Sync Monitor has real history."""
    log.info("writing simulated synchronization history …")
    stores = query("SELECT store_id, store_code FROM stores").to_dict("records")
    for st in stores:
        for back in range(6, 0, -1):
            d = END_DATE - timedelta(days=back)
            n = int(df[(df["store_id"] == st["store_id"]) &
                       (df["business_date"] == d.isoformat())]["units_sold"].sum() // 3)
            batch = f"SYNC-{st['store_code']}-{d.strftime('%Y%m%d')}-{py_rng.randint(1000,9999)}"
            execute(
                "INSERT INTO synchronization_logs (batch_id, store_id, node_id, direction, event_count, "
                "accepted_count, duplicate_count, rejected_count, out_of_order_count, started_at, "
                "completed_at, latency_ms, status, simulated_offline) "
                "VALUES (:b,:s,:n,'STORE_TO_CENTRAL',:e,:e,0,0,:oo,:st,:ct,:l,'SUCCESS',:off)",
                {
                    "b": batch, "s": st["store_id"], "n": f"NODE-{st['store_code']}",
                    "e": max(1, n), "oo": py_rng.choice([0, 0, 0, 1]),
                    "st": f"{d.isoformat()}T23:30:00", "ct": f"{d.isoformat()}T23:30:{py_rng.randint(5,50):02d}",
                    "l": py_rng.randint(320, 2100), "off": py_rng.choice([0, 0, 0, 1]),
                },
            )


def export_sample_csvs(df: pd.DataFrame) -> None:
    """Write tidy samples used by the README / dataset dictionary."""
    from config import DATA_DIR
    prod = query("""
        SELECT k.sku_code, k.barcode, k.product_name, c.category_name, s.supplier_name,
               k.unit_cost, k.selling_price, k.reorder_level, k.safety_stock, k.max_stock_level,
               s.lead_time_days, k.shelf_life_days
        FROM sku k JOIN product_categories c ON c.category_id = k.category_id
        JOIN suppliers s ON s.supplier_id = k.supplier_id ORDER BY k.sku_id
    """)
    prod.to_csv(DATA_DIR / "sample_products.csv", index=False)
    query("""SELECT store_code, store_name, city, region, store_format, floor_area_sqm, latitude, longitude
             FROM stores ORDER BY store_id""").to_csv(DATA_DIR / "sample_stores.csv", index=False)
    sample = df.merge(query("SELECT store_id, store_code FROM stores"), on="store_id") \
               .merge(query("SELECT sku_id, sku_code FROM sku"), on="sku_id")
    sample = sample[sample["business_date"] >= (END_DATE - timedelta(days=13)).isoformat()]
    sample[["business_date", "store_code", "sku_code", "units_sold", "gross_revenue",
            "demand_unmet", "closing_stock", "stockout_flag"]].head(5000).to_csv(
        DATA_DIR / "sample_sales.csv", index=False)
    log.info("sample CSV extracts written to %s", DATA_DIR)


def run(reset: bool = True) -> None:
    log.info("=" * 78)
    log.info("StockSense AI – building SIMULATED FreshMart dataset")
    log.info("window: %s → %s (%d days)  seed=%d", START_DATE, END_DATE, HISTORY_DAYS, RANDOM_SEED)
    log.info("=" * 78)
    if reset:
        create_schema(drop_first=True)
    seed_reference_data()
    df, ledger_rows, po_log = simulate_operations()
    persist_daily(df)
    persist_ledger(ledger_rows)
    persist_stock_on_hand(df)
    persist_purchase_orders(po_log)
    create_open_purchase_orders()
    persist_pos_transactions(df)
    persist_stockout_events(df)
    persist_store_kpis(df)
    persist_supplier_performance()
    build_demo_transaction_logs(df)
    export_sample_csvs(df)
    write_audit("SEED_DATASET", f"Simulated {HISTORY_DAYS} days for 8 stores / 120 SKUs")

    # integrity proof: ledger sum must equal the derived stock cache
    mism = query("""
        SELECT COUNT(*) AS n FROM stock_on_hand soh
        JOIN v_ledger_stock v ON v.store_id = soh.store_id AND v.sku_id = soh.sku_id
        WHERE soh.on_hand_qty <> v.ledger_qty
    """).iloc[0]["n"]
    log.info("ledger reconciliation check: %s mismatching store-SKU rows", mism)

    for t in ["stores", "sku", "suppliers", "sales_daily", "stock_event", "stock_on_hand",
              "purchase_orders", "goods_receipts", "sales_transactions", "stockout_event",
              "store_kpi_daily", "supplier_performance"]:
        log.info("  %-22s %10s rows", t, f"{scalar(f'SELECT COUNT(*) FROM {t}', default=0):,}")
    log.info("dataset build complete.")


if __name__ == "__main__":  # pragma: no cover
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do not drop existing tables")
    args = ap.parse_args()
    run(reset=not args.keep)
