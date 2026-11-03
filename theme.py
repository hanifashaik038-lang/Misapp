"""Visual identity: dark navy + teal enterprise analytics theme."""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

from config import CHAIN_NAME, COLORS, DEMO_BANNER, SYSTEM_NAME, SYSTEM_TAGLINE, SYSTEM_VERSION

FONT_STACK = ("'Inter', 'Segoe UI', -apple-system, BlinkMacSystemFont, 'Helvetica Neue', "
              "Arial, sans-serif")

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

:root {{
  --bg: {COLORS['bg']};
  --surface: {COLORS['surface']};
  --surface-alt: {COLORS['surface_alt']};
  --border: {COLORS['border']};
  --primary: {COLORS['primary']};
  --primary-dim: {COLORS['primary_dim']};
  --accent: {COLORS['accent']};
  --text: {COLORS['text']};
  --muted: {COLORS['muted']};
}}

html, body, [class*="css"], .stApp {{
  font-family: {FONT_STACK};
  background: radial-gradient(1200px 620px at 12% -8%, #10203a 0%, {COLORS['bg']} 46%),
              radial-gradient(900px 500px at 96% 4%, #0b2a2c 0%, rgba(7,13,27,0) 60%),
              {COLORS['bg']};
  color: {COLORS['text']};
}}
.stApp p, .stApp span, .stApp label, .stApp div {{ color: {COLORS['text']}; }}
#MainMenu, footer, header[data-testid="stHeader"] {{ visibility: hidden; height: 0; }}
[data-testid="stToolbar"] {{ display: none; }}
[data-testid="stDecoration"] {{ display: none; }}
[data-testid="stStatusWidget"] {{ display: none; }}
.block-container {{ padding: 1.1rem 1.9rem 3.4rem 1.9rem; max-width: 1560px; }}

/* ---------------- sidebar ---------------- */
section[data-testid="stSidebar"] {{
  background: linear-gradient(180deg, #0a1424 0%, #08101d 100%);
  border-right: 1px solid {COLORS['border']};
}}
section[data-testid="stSidebar"] > div {{ padding-top: .55rem; }}
section[data-testid="stSidebar"] .stRadio label {{
  padding: .38rem .6rem; border-radius: 9px; font-size: .86rem; font-weight: 500;
  color: {COLORS['muted']} !important; transition: all .16s ease; width: 100%;
}}
section[data-testid="stSidebar"] .stRadio label:hover {{
  background: rgba(20,184,166,.09); color: {COLORS['text']} !important;
}}
section[data-testid="stSidebar"] .stRadio [data-checked="true"] ~ div p {{ color: #5eead4 !important; }}
section[data-testid="stSidebar"] .stRadio [role="radiogroup"] {{ gap: .1rem; }}

/* ---------------- typography ---------------- */
h1, h2, h3, h4 {{ font-family: {FONT_STACK}; letter-spacing: -.35px; }}
h1 {{ font-size: 1.72rem !important; font-weight: 700 !important; margin-bottom: .15rem !important; }}
h2 {{ font-size: 1.18rem !important; font-weight: 650 !important; }}
h3 {{ font-size: 1.0rem !important; font-weight: 600 !important; }}

.ss-pagehead {{ display:flex; align-items:flex-start; justify-content:space-between;
  gap:1rem; margin: .1rem 0 1.15rem 0; flex-wrap: wrap; }}
.ss-pagehead .t {{ font-size:1.66rem; font-weight:750; letter-spacing:-.5px; line-height:1.18; }}
.ss-pagehead .s {{ font-size:.86rem; color:{COLORS['muted']}; margin-top:.28rem; max-width:820px; }}
.ss-eyebrow {{ font-size:.66rem; letter-spacing:.19em; text-transform:uppercase;
  color:{COLORS['primary']} !important; font-weight:700; margin-bottom:.32rem; }}

/* ---------------- cards ---------------- */
.ss-card {{
  background: linear-gradient(160deg, {COLORS['surface_alt']} 0%, {COLORS['surface']} 100%);
  border: 1px solid {COLORS['border']}; border-radius: 14px; padding: 1rem 1.1rem;
  box-shadow: 0 10px 26px -20px rgba(0,0,0,.95);
}}
.ss-kpi {{
  position: relative; overflow: hidden;
  background: linear-gradient(158deg, {COLORS['surface_alt']} 0%, #0b1220 100%);
  border: 1px solid {COLORS['border']}; border-radius: 14px;
  padding: .92rem 1rem .82rem 1rem; height: 100%;
  transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease;
}}
.ss-kpi:hover {{ transform: translateY(-2px); border-color: rgba(20,184,166,.5);
  box-shadow: 0 16px 30px -22px rgba(20,184,166,.6); }}
.ss-kpi:before {{ content:''; position:absolute; left:0; top:0; height:3px; width:100%;
  background: linear-gradient(90deg, var(--accent-color, {COLORS['primary']}), transparent 92%); }}
.ss-kpi .lbl {{ font-size:.665rem; letter-spacing:.13em; text-transform:uppercase;
  color:{COLORS['muted']} !important; font-weight:650; }}
.ss-kpi .val {{ font-size:1.62rem; font-weight:730; line-height:1.25; margin-top:.34rem;
  letter-spacing:-.6px; color:#f3f8ff !important; }}
.ss-kpi .sub {{ font-size:.72rem; color:{COLORS['muted']} !important; margin-top:.24rem; }}
.ss-kpi .ico {{ position:absolute; right:.85rem; top:.72rem; font-size:1.02rem; opacity:.34; }}

/* ---------------- badges / pills ---------------- */
.ss-badge {{ display:inline-block; padding:.16rem .55rem; border-radius:999px;
  font-size:.68rem; font-weight:700; letter-spacing:.04em; }}
.ss-pill {{ display:inline-flex; align-items:center; gap:.34rem; padding:.2rem .62rem;
  border-radius:999px; font-size:.7rem; font-weight:650; border:1px solid {COLORS['border']};
  background: rgba(255,255,255,.02); color:{COLORS['muted']} !important; }}
.ss-dot {{ width:7px; height:7px; border-radius:50%; display:inline-block; }}

.ss-banner {{ display:flex; align-items:center; gap:.6rem; padding:.5rem .85rem;
  border-radius:10px; border:1px dashed rgba(245,158,11,.45);
  background: rgba(245,158,11,.07); font-size:.74rem; color:#fcd34d !important;
  font-weight:600; letter-spacing:.03em; margin-bottom:1rem; }}
.ss-banner .x {{ color:#fbbf24 !important; }}

/* ---------------- metrics fallback ---------------- */
[data-testid="stMetric"] {{
  background: linear-gradient(158deg, {COLORS['surface_alt']}, #0b1220);
  border: 1px solid {COLORS['border']}; border-radius: 13px; padding: .8rem .95rem;
}}
[data-testid="stMetricLabel"] p {{ font-size:.7rem !important; letter-spacing:.1em;
  text-transform:uppercase; color:{COLORS['muted']} !important; font-weight:650; }}
[data-testid="stMetricValue"] {{ font-size:1.42rem !important; font-weight:700 !important; }}

/* ---------------- forms & inputs ---------------- */
.stTextInput input, .stNumberInput input, .stDateInput input, .stTextArea textarea,
.stSelectbox div[data-baseweb="select"] > div, .stMultiSelect div[data-baseweb="select"] > div {{
  background: #0a1322 !important; border: 1px solid {COLORS['border']} !important;
  border-radius: 9px !important; color: {COLORS['text']} !important; font-size:.86rem !important;
}}
.stTextInput input:focus {{ border-color: {COLORS['primary']} !important;
  box-shadow: 0 0 0 2px rgba(20,184,166,.16) !important; }}
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {{
  background: linear-gradient(135deg, {COLORS['primary']} 0%, #0d9488 100%);
  color: #04121a !important; border: none; border-radius: 9px; font-weight: 700;
  font-size: .82rem; letter-spacing: .02em; padding: .44rem 1.05rem;
  transition: filter .16s ease, transform .16s ease; box-shadow: 0 8px 20px -14px #14b8a6;
}}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {{
  filter: brightness(1.1); transform: translateY(-1px); color:#04121a !important; }}
.stButton > button[kind="secondary"] {{
  background: rgba(255,255,255,.04); color:{COLORS['text']} !important;
  border: 1px solid {COLORS['border']}; box-shadow:none; }}
.stButton > button[kind="secondary"]:hover {{ border-color: {COLORS['primary']};
  color:#5eead4 !important; }}

/* ---------------- tabs ---------------- */
.stTabs [data-baseweb="tab-list"] {{ gap:.3rem; border-bottom:1px solid {COLORS['border']}; }}
.stTabs [data-baseweb="tab"] {{ background: transparent; border-radius: 8px 8px 0 0;
  padding: .45rem .9rem; font-size:.83rem; font-weight:600; color:{COLORS['muted']}; }}
.stTabs [aria-selected="true"] {{ background: rgba(20,184,166,.1) !important;
  color:#5eead4 !important; border-bottom:2px solid {COLORS['primary']}; }}

/* ---------------- tables ---------------- */
[data-testid="stDataFrame"] {{ border:1px solid {COLORS['border']}; border-radius:11px;
  overflow:hidden; }}
[data-testid="stDataFrame"] * {{ font-size:.79rem !important; }}
.stDataFrame thead tr th {{ background:#0b1526 !important; color:{COLORS['muted']} !important;
  font-weight:650 !important; }}

/* ---------------- misc ---------------- */
hr {{ border-color: {COLORS['border']} !important; margin:.9rem 0 !important; }}
[data-testid="stExpander"] {{ border:1px solid {COLORS['border']} !important;
  border-radius:11px !important; background:{COLORS['surface']}; }}
[data-testid="stExpander"] summary {{ font-size:.84rem; font-weight:600; }}
.stAlert {{ border-radius:10px; font-size:.82rem; }}
.stProgress > div > div > div > div {{ background: {COLORS['primary']}; }}
[data-testid="stSidebarNav"] {{ display:none; }}
div[data-testid="stVerticalBlockBorderWrapper"] {{ border-color: {COLORS['border']}; }}
.stSlider [data-baseweb="slider"] div[role="slider"] {{ background:{COLORS['primary']}; }}
.stCheckbox label {{ font-size:.84rem; }}
.stCaption, [data-testid="stCaptionContainer"] p {{ color:{COLORS['muted']} !important;
  font-size:.73rem !important; }}
a {{ color:#5eead4 !important; }}

.ss-section {{ display:flex; align-items:baseline; gap:.6rem; margin:1.35rem 0 .6rem 0; }}
.ss-section .bar {{ width:3px; height:15px; border-radius:2px; background:{COLORS['primary']}; }}
.ss-section .txt {{ font-size:.95rem; font-weight:670; letter-spacing:-.2px; }}
.ss-section .hint {{ font-size:.74rem; color:{COLORS['muted']}; }}

.ss-login {{ max-width: 430px; margin: 3.2rem auto 0 auto; padding: 2rem 2.1rem;
  background: linear-gradient(165deg, #101d33 0%, #0a1220 100%);
  border:1px solid {COLORS['border']}; border-radius:18px;
  box-shadow: 0 40px 80px -50px rgba(0,0,0,1), inset 0 1px 0 rgba(255,255,255,.03); }}
.ss-brand {{ display:flex; align-items:center; gap:.65rem; }}
.ss-logo {{ width:38px; height:38px; border-radius:11px; display:grid; place-items:center;
  background: linear-gradient(135deg,#14b8a6,#0e7490); color:#03121a; font-weight:800;
  font-size:1.02rem; box-shadow:0 10px 22px -14px #14b8a6; }}
.ss-brand .n {{ font-size:1.02rem; font-weight:750; letter-spacing:-.3px; }}
.ss-brand .v {{ font-size:.66rem; color:{COLORS['muted']}; letter-spacing:.1em;
  text-transform:uppercase; }}
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Plotly theme
# --------------------------------------------------------------------------
def register_plotly_theme() -> None:
    tpl = go.layout.Template()
    tpl.layout = go.Layout(
        font=dict(family="Inter, Segoe UI, sans-serif", size=12.5, color=COLORS["text"]),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        colorway=["#14b8a6", "#38bdf8", "#a78bfa", "#f59e0b", "#fb7185", "#22d3ee",
                  "#84cc16", "#f472b6"],
        margin=dict(l=10, r=14, t=34, b=10),
        xaxis=dict(gridcolor=COLORS["grid"], zerolinecolor=COLORS["grid"],
                   linecolor=COLORS["border"], tickfont=dict(color=COLORS["muted"], size=11),
                   title=dict(font=dict(color=COLORS["muted"], size=11.5))),
        yaxis=dict(gridcolor=COLORS["grid"], zerolinecolor=COLORS["grid"],
                   linecolor=COLORS["border"], tickfont=dict(color=COLORS["muted"], size=11),
                   title=dict(font=dict(color=COLORS["muted"], size=11.5))),
        legend=dict(bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=COLORS["muted"]),
                    orientation="h", y=1.13, x=0),
        hoverlabel=dict(bgcolor="#0a1526", bordercolor=COLORS["border"],
                        font=dict(color=COLORS["text"], size=11.5)),
        title=dict(font=dict(size=13.5, color=COLORS["text"])),
    )
    pio.templates["stocksense"] = tpl
    pio.templates.default = "stocksense"


def apply_theme() -> None:
    inject_css()
    register_plotly_theme()


def page_header(title: str, subtitle: str = "", eyebrow: str = "",
                right_html: str = "") -> None:
    st.markdown(
        f"""<div class="ss-pagehead"><div>
        {f'<div class="ss-eyebrow">{eyebrow}</div>' if eyebrow else ''}
        <div class="t">{title}</div>
        {f'<div class="s">{subtitle}</div>' if subtitle else ''}
        </div><div>{right_html}</div></div>""",
        unsafe_allow_html=True,
    )


def demo_banner(extra: str = "") -> None:
    st.markdown(
        f'<div class="ss-banner"><span class="x">◆</span> {DEMO_BANNER}'
        f'{(" · " + extra) if extra else ""}</div>', unsafe_allow_html=True)


def section(title: str, hint: str = "") -> None:
    st.markdown(
        f'<div class="ss-section"><div class="bar"></div>'
        f'<div class="txt">{title}</div>'
        f'{f"<div class=hint>{hint}</div>" if hint else ""}</div>',
        unsafe_allow_html=True)


def sidebar_brand() -> None:
    st.markdown(
        f"""<div class="ss-brand"><div class="ss-logo">◈</div>
        <div><div class="n">{SYSTEM_NAME}</div>
        <div class="v">v{SYSTEM_VERSION} · {CHAIN_NAME}</div></div></div>""",
        unsafe_allow_html=True)
