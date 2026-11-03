"""
StockSense AI – Stockout Probability Prediction and Store Performance
Information System.

FreshMart Supermarkets (simulated FMCG chain).

Run:
    streamlit run app.py
"""
from __future__ import annotations

import logging
from datetime import date

import pandas as pd
import streamlit as st

from config import (CHAIN_NAME, DEMO_BANNER, NAV_ITEMS, ROLE_LABELS, SYSTEM_NAME,
                    SYSTEM_TAGLINE, SYSTEM_VERSION)
from database.db import db_ready, query, scalar
from modules import authentication as auth
from modules import stockout_prediction as pred
from pages_pkg import admin_pages, alert_inbox, executive, inventory_page
from pages_pkg import performance_page, procurement_page, products_page
from pages_pkg import reports_page, scanner_page, suppliers_page, sync_page
from ui import components as c
from ui.theme import apply_theme, demo_banner, page_header, section, sidebar_brand

st.set_page_config(
    page_title=f"{SYSTEM_NAME} · {CHAIN_NAME}",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

apply_theme()
logging.basicConfig(level=logging.INFO)


# =====================================================================
# Bootstrap guard
# =====================================================================
def first_run_notice():
    st.markdown(
        f"""<div class="ss-login">
        <div class="ss-brand"><div class="ss-logo">◈</div>
        <div><div class="n">{SYSTEM_NAME}</div><div class="v">v{SYSTEM_VERSION}</div></div></div>
        <h2 style="margin-top:1.3rem">Database not initialised</h2>
        <p style="color:#8494ad;font-size:.9rem">Run these two commands in the project folder,
        then reload this page:</p>
        <pre style="background:#0a1322;padding:.8rem;border-radius:10px;font-size:.78rem;
        overflow-x:auto;border:1px solid #1c2b45">python -m database.seed_data
python -m ml.train_model</pre>
        </div>""", unsafe_allow_html=True)
    st.stop()


if not db_ready():
    first_run_notice()


# =====================================================================
# Session / login
# =====================================================================
st.session_state.setdefault("user", None)
st.session_state.setdefault("page", "executive")


def login_screen():
    left, right = st.columns([1.15, 1], gap="large")
    with left:
        st.markdown(
            f"""<div style="padding:2.2rem 1rem 0 0">
            <div class="ss-brand"><div class="ss-logo">◈</div>
              <div><div class="n" style="font-size:1.28rem">{SYSTEM_NAME}</div>
              <div class="v">{CHAIN_NAME} · v{SYSTEM_VERSION}</div></div></div>
            <h1 style="margin-top:1.5rem;font-size:2.05rem;line-height:1.2">
              Know which shelf will be empty<br>before it is.</h1>
            <p style="color:#8494ad;font-size:.95rem;line-height:1.7;max-width:520px">
              {SYSTEM_TAGLINE}. A seven-day stockout probability for every product in every
              store, an explainable recommendation attached to each risk, and a comparative
              performance score for the whole chain.</p>
            <div style="display:flex;gap:.5rem;flex-wrap:wrap;margin-top:1.4rem">
              <span class="ss-pill"><span class="ss-dot" style="background:#14b8a6"></span>
                Trained on simulated history</span>
              <span class="ss-pill"><span class="ss-dot" style="background:#38bdf8"></span>
                8 stores · 120 SKUs · 180 days</span>
              <span class="ss-pill"><span class="ss-dot" style="background:#a78bfa"></span>
                Append-only inventory ledger</span>
            </div>
            <div class="ss-banner" style="margin-top:1.6rem">{DEMO_BANNER}</div>
            </div>""", unsafe_allow_html=True)
    with right:
        st.markdown('<div class="ss-login">', unsafe_allow_html=True)
        st.markdown(
            f'<div class="ss-brand"><div class="ss-logo">◈</div><div>'
            f'<div class="n">Sign in</div><div class="v">role-based access</div></div></div>',
            unsafe_allow_html=True)
        with st.form("login"):
            username = st.text_input("Username", placeholder="hoadmin")
            password = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Sign in", type="primary",
                                              use_container_width=True)
        if submitted:
            user, err = auth.authenticate(username, password)
            if user:
                st.session_state["user"] = user
                st.session_state["page"] = default_page(user)
                st.rerun()
            else:
                st.error(err)

        st.markdown(
            '<div style="margin-top:1.1rem;font-size:.73rem;color:#8494ad;line-height:1.9">'
            '<b style="color:#14b8a6">Demo accounts</b> — passwords are hashed with '
            'PBKDF2-SHA256 in the database; these are demonstration credentials only.<br>'
            '<code>hoadmin / Admin@123</code> — Head Office Administrator<br>'
            '<code>regional / Regional@123</code> — Regional Manager<br>'
            '<code>manager.st01 / Manager@123</code> — Store Manager (Jubilee Hills)<br>'
            '<code>employee.st01 / Employee@123</code> — Store Employee</div>',
            unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)


def default_page(user: dict) -> str:
    return {"STORE_EMPLOYEE": "scanner", "STORE_MANAGER": "prediction",
            "REGIONAL_MANAGER": "performance", "HEAD_OFFICE_ADMIN": "executive"}.get(
        user.get("role_code"), "executive")


user = st.session_state.get("user")
if not user:
    login_screen()
    st.stop()


# =====================================================================
# Sidebar
# =====================================================================
stores_df = query("SELECT store_id, store_code, store_name, region, store_format "
                  "FROM stores WHERE is_active = 1 ORDER BY store_id")
store_ids = auth.allowed_store_ids(user)

with st.sidebar:
    sidebar_brand()
    st.markdown(
        f'<div style="margin:.9rem 0 .2rem 0;padding:.6rem .7rem;border-radius:10px;'
        f'background:rgba(20,184,166,.07);border:1px solid rgba(20,184,166,.24)">'
        f'<div style="font-size:.78rem;font-weight:700">{user["full_name"]}</div>'
        f'<div style="font-size:.68rem;color:#8494ad;margin-top:.15rem">'
        f'{ROLE_LABELS.get(user["role_code"], user["role_code"])}'
        f'{" · " + str(user["store_code"]) if user.get("store_code") else " · chain-wide"}</div>'
        f'</div>', unsafe_allow_html=True)

    visible = [(k, l, i) for (k, l, i, roles) in NAV_ITEMS if user["role_code"] in roles]
    labels = [f"{icon}  {label}" for _, label, icon in visible]
    keys = [k for k, _, _ in visible]
    current = st.session_state.get("page", "executive")
    idx = keys.index(current) if current in keys else 0
    pick = st.radio("Navigation", labels, index=idx, label_visibility="collapsed")
    st.session_state["page"] = keys[labels.index(pick)]

    st.markdown("<hr>", unsafe_allow_html=True)
    ps = pred.prediction_summary(store_ids)
    st.markdown(
        f'<div style="font-size:.68rem;letter-spacing:.11em;text-transform:uppercase;'
        f'color:#8494ad;font-weight:650;margin-bottom:.4rem">Chain pulse</div>'
        f'<div style="font-size:.78rem;line-height:2">'
        f'<span class="ss-dot" style="background:#ef4444"></span> '
        f'Critical risk <b>{ps.get("critical", 0)}</b><br>'
        f'<span class="ss-dot" style="background:#f97316"></span> '
        f'High risk <b>{ps.get("high", 0)}</b><br>'
        f'<span class="ss-dot" style="background:#14b8a6"></span> '
        f'Avg probability <b>{ps.get("avg_probability", 0)}%</b></div>',
        unsafe_allow_html=True)

    st.markdown("<hr>", unsafe_allow_html=True)
    if st.button("Sign out", use_container_width=True, type="secondary"):
        st.session_state["user"] = None
        st.session_state["page"] = "executive"
        st.rerun()
    st.markdown(
        f'<div style="font-size:.65rem;color:#5b6a80;margin-top:.8rem;line-height:1.7">'
        f'{DEMO_BANNER}<br>{SYSTEM_NAME} v{SYSTEM_VERSION}</div>',
        unsafe_allow_html=True)


# =====================================================================
# Router
# =====================================================================
PAGE = st.session_state["page"]

ROUTES = {
    "executive": executive.render,
    "prediction": lambda u, s, d: __import__(
        "pages_pkg.prediction", fromlist=["render"]).render(u, s, d),
    "alerts": alert_inbox.render,
    "inventory": inventory_page.render,
    "scanner": scanner_page.render,
    "procurement": procurement_page.render_po,
    "receiving": procurement_page.render_receiving,
    "performance": performance_page.render,
    "products": products_page.render,
    "suppliers": suppliers_page.render,
    "reports": reports_page.render,
    "sync": sync_page.render,
    "users": admin_pages.render_users,
    "settings": admin_pages.render_settings,
}

try:
    ROUTES[PAGE](user, store_ids, stores_df)
except Exception as exc:  # surface page errors without killing the session
    st.error(f"This page could not be rendered: {exc}")
    with st.expander("Traceback"):
        import traceback
        st.code(traceback.format_exc())

st.markdown(
    f'<div style="margin-top:2.6rem;padding-top:.9rem;border-top:1px solid #1c2b45;'
    f'font-size:.68rem;color:#5b6a80;display:flex;justify-content:space-between;'
    f'flex-wrap:wrap;gap:.6rem">'
    f'<span>{SYSTEM_NAME} v{SYSTEM_VERSION} · {CHAIN_NAME} · {SYSTEM_TAGLINE}</span>'
    f'<span>{DEMO_BANNER}</span></div>', unsafe_allow_html=True)
