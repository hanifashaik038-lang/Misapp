"""Shared scoping helpers for pages (store pickers that respect RBAC)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from database.db import query


def store_picker(store_ids: list[int] | None, stores_df: pd.DataFrame, key: str,
                 allow_all: bool = True) -> list[int] | None:
    """Return a list of store ids (or ``None`` for chain-wide) honouring the user's scope."""
    if store_ids is not None:
        sel = stores_df[stores_df["store_id"].isin(store_ids)]
        st.selectbox("Store (locked to your role)",
                     [f"{r.store_code} · {r.store_name}" for r in sel.itertuples()],
                     disabled=True, key=f"{key}_locked")
        return store_ids

    labels = [f"{r.store_code} · {r.store_name}" for r in stores_df.itertuples()]
    options = (["All stores"] if allow_all else []) + labels
    pick = st.selectbox("Store", options, key=f"{key}_pick")
    if pick == "All stores":
        return None
    return [int(stores_df.iloc[labels.index(pick)]["store_id"])]


def single_store(store_ids: list[int] | None, stores_df: pd.DataFrame, key: str) -> int:
    """A single store id – required by transactional screens (POS, receiving)."""
    if store_ids is not None:
        return int(store_ids[0])
    labels = [f"{r.store_code} · {r.store_name}" for r in stores_df.itertuples()]
    pick = st.selectbox("Store", labels, key=f"{key}_single")
    return int(stores_df.iloc[labels.index(pick)]["store_id"])


def store_label(store_id: int) -> str:
    r = query("SELECT store_code, store_name FROM stores WHERE store_id = :s",
              {"s": int(store_id)})
    return f"{r.iloc[0]['store_code']} · {r.iloc[0]['store_name']}" if not r.empty else str(store_id)


def product_options(store_id: int | None = None, supplier_id: int | None = None) -> pd.DataFrame:
    sql = [
        "SELECT k.sku_id, k.sku_code, k.product_name, k.unit_cost, k.selling_price, "
        "k.reorder_level, k.max_stock_level, k.supplier_id, sup.supplier_name "
        "FROM sku k JOIN suppliers sup ON sup.supplier_id = k.supplier_id WHERE k.is_active = 1"]
    p: dict = {}
    if store_id:
        sql.append("AND 1=1")
    if supplier_id:
        sql.append("AND k.supplier_id = :sup")
        p["sup"] = int(supplier_id)
    return query(" ".join(sql) + " ORDER BY k.product_name", p)
