"""Reusable UI building blocks: KPI tiles, badges, tables and chart helpers."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from config import COLORS, RISK_BANDS
from utils.calculations import RISK_COLOR, STATUS_COLORS


# --------------------------------------------------------------------------
# Tiles & badges
# --------------------------------------------------------------------------
def kpi(label: str, value: str, sub: str = "", color: str | None = None,
        icon: str = "") -> None:
    accent = color or COLORS["primary"]
    st.markdown(
        f"""<div class="ss-kpi" style="--accent-color:{accent}">
        <div class="lbl">{label}</div>
        <div class="val">{value}</div>
        {f'<div class="sub">{sub}</div>' if sub else ''}
        {f'<div class="ico">{icon}</div>' if icon else ''}
        </div>""",
        unsafe_allow_html=True)


def kpi_row(items: list[dict]) -> None:
    cols = st.columns(len(items), gap="small")
    for col, item in zip(cols, items):
        with col:
            kpi(item.get("label", ""), item.get("value", "–"), item.get("sub", ""),
                item.get("color"), item.get("icon", ""))


def badge(text: str, color: str) -> str:
    return (f'<span class="ss-badge" style="background:{color}22;color:{color};'
            f'border:1px solid {color}55">{text}</span>')


def risk_badge(risk_level: str) -> str:
    return badge(risk_level.replace(" Risk", ""), RISK_COLOR.get(risk_level, COLORS["muted"]))


def status_badge(status: str) -> str:
    colors = {
        "In Stock": "#14b8a6", "Low Stock": "#f59e0b", "Critical Stock": "#f97316",
        "Out of Stock": "#ef4444", "OPEN": "#ef4444", "ACKNOWLEDGED": "#f59e0b",
        "ASSIGNED": "#38bdf8", "RESOLVED": "#14b8a6", "CLOSED": "#64748b",
        "RECEIVED": "#14b8a6", "PARTIALLY_RECEIVED": "#f59e0b",
        "SUBMITTED": "#38bdf8", "ACKNOWLEDGED_PO": "#a78bfa", "CANCELLED": "#64748b",
        "DRAFT": "#64748b",
    }
    return badge(status.replace("_", " "), colors.get(status, COLORS["muted"]))


def pills(items: list[tuple[str, str]]) -> None:
    html = "".join(
        f'<span class="ss-pill"><span class="ss-dot" style="background:{c}"></span>{t}</span>'
        for t, c in items)
    st.markdown(f'<div style="display:flex;gap:.4rem;flex-wrap:wrap">{html}</div>',
                unsafe_allow_html=True)


def card_open(padding: str = "1rem 1.1rem") -> None:
    st.markdown(f'<div class="ss-card" style="padding:{padding}">', unsafe_allow_html=True)


def card_close() -> None:
    st.markdown("</div>", unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------
def style(fig: go.Figure, height: int = 300, legend: bool = True) -> go.Figure:
    fig.update_layout(height=height, showlegend=legend,
                      legend=dict(orientation="h", y=1.14, x=0))
    return fig


def line(df, x: str, y: str | list[str], title: str = "", height: int = 300,
         colors: list[str] | None = None, fill: bool = False) -> go.Figure:
    ys = y if isinstance(y, list) else [y]
    fig = go.Figure()
    for i, col in enumerate(ys):
        fig.add_trace(go.Scatter(
            x=df[x], y=df[col], name=col.replace("_", " ").title(), mode="lines",
            line=dict(width=2.3, color=(colors[i] if colors and i < len(colors) else None),
                      shape="spline", smoothing=0.6),
            fill="tozeroy" if fill and i == 0 else None,
            hovertemplate="%{x}<br><b>%{y:,.0f}</b><extra></extra>"))
    fig.update_layout(title=title)
    return style(fig, height, legend=len(ys) > 1)


def bars(df, x: str, y: str, title: str = "", height: int = 300, horizontal: bool = False,
         color: str | None = None, color_map: dict | None = None) -> go.Figure:
    if color_map and "color_key" in df.columns:
        colors = [color_map.get(v, COLORS["primary"]) for v in df["color_key"]]
    elif color:
        colors = color
    else:
        colors = None
    fig = go.Figure(go.Bar(
        x=df[y] if horizontal else df[x], y=df[x] if horizontal else df[y],
        orientation="h" if horizontal else "v",
        marker=dict(color=colors, line=dict(width=0)),
        text=df[y].map(lambda v: f"{v:,.0f}"), textposition="outside",
        textfont=dict(size=10.5, color=COLORS["muted"]),
        hovertemplate="%{y}<br><b>%{x:,.0f}</b><extra></extra>" if horizontal
        else "%{x}<br><b>%{y:,.0f}</b><extra></extra>"))
    fig.update_layout(title=title, margin=dict(l=10, r=30, t=38, b=10))
    return style(fig, height, legend=False)


def donut(labels: list[str], values: list[float], title: str = "",
          colors: list[str] | None = None, height: int = 300) -> go.Figure:
    fig = go.Figure(go.Pie(
        labels=labels, values=values, hole=0.62,
        marker=dict(colors=colors or [COLORS["primary"], COLORS["accent"], "#a78bfa",
                                      "#f59e0b", "#fb7185", "#22d3ee"],
                    line=dict(color=COLORS["bg"], width=2)),
        textinfo="percent", textfont=dict(size=11),
        hovertemplate="<b>%{label}</b><br>%{value:,} (%{percent})<extra></extra>"))
    fig.update_layout(title=title,
                      annotations=[dict(text=f"{sum(values):,.0f}".rjust(1), showarrow=False,
                                        font=dict(size=17, color=COLORS["text"]))])
    return style(fig, height, legend=True)


def scatter_risk(df, title: str = "Risk map – stock on hand vs 7-day stockout probability",
                 height: int = 380) -> go.Figure:
    fig = go.Figure()
    for band in RISK_BANDS:
        sub = df[df["risk_level"] == band["label"]]
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["current_stock"], y=sub["probability"] * 100,
            mode="markers", name=band["label"],
            marker=dict(size=8, color=band["color"], opacity=0.8,
                        line=dict(width=0.6, color="#07101d")),
            customdata=sub[["product_name", "store_code", "avg_daily_demand"]].to_numpy(),
            hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}<br>"
                          "Stock: %{x:,}<br>Probability: %{y:.0f}%<br>"
                          "Avg demand: %{customdata[2]:.1f}/day<extra></extra>"))
    fig.update_layout(title=title,
                      xaxis_title="Current stock on hand (units)",
                      yaxis_title="7-day stockout probability (%)")
    fig.add_hrect(y0=80, y1=100, fillcolor="#ef4444", opacity=0.06, line_width=0)
    fig.add_hrect(y0=60, y1=80, fillcolor="#f97316", opacity=0.05, line_width=0)
    return style(fig, height, legend=True)


def gauge(value: float, title: str = "", height: int = 210, suffix: str = "%") -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=value,
        number=dict(suffix=suffix, font=dict(size=26, color=COLORS["text"])),
        gauge=dict(
            axis=dict(range=[0, 100], tickcolor=COLORS["muted"],
                      tickfont=dict(size=9, color=COLORS["muted"])),
            bar=dict(color=COLORS["primary"], thickness=0.28),
            bgcolor="rgba(255,255,255,.03)", borderwidth=0,
            steps=[dict(range=[0, 30], color="rgba(20,184,166,.13)"),
                   dict(range=[30, 60], color="rgba(245,158,11,.13)"),
                   dict(range=[60, 80], color="rgba(249,115,22,.13)"),
                   dict(range=[80, 100], color="rgba(239,68,68,.13)")],
            threshold=dict(line=dict(color="#ef4444", width=3), thickness=0.78, value=80)),
        title=dict(text=title, font=dict(size=11.5, color=COLORS["muted"]))))
    return style(fig, height, legend=False)


def heatmap(df, x: str, y: str, z: str, title: str = "", height: int = 340,
            suffix: str = "") -> go.Figure:
    pivot = df.pivot_table(index=y, columns=x, values=z, aggfunc="mean")
    fig = go.Figure(go.Heatmap(
        z=pivot.to_numpy(), x=list(pivot.columns), y=list(pivot.index),
        colorscale=[[0, "#0b3b3a"], [0.35, "#0f766e"], [0.62, "#f59e0b"],
                    [0.82, "#f97316"], [1, "#ef4444"]],
        hovertemplate=f"%{{y}}<br>%{{x}}<br><b>%{{z:.1f}}{suffix}</b><extra></extra>",
        colorbar=dict(thickness=10, len=0.82, tickfont=dict(size=9))))
    fig.update_layout(title=title)
    return style(fig, height, legend=False)


def radar(categories: list[str], series: dict[str, list[float]],
          title: str = "", height: int = 340) -> go.Figure:
    fig = go.Figure()
    for name, vals in series.items():
        fig.add_trace(go.Scatterpolar(r=vals + vals[:1], theta=categories + categories[:1],
                                      fill="toself", name=name, opacity=0.55,
                                      line=dict(width=2)))
    fig.update_layout(title=title,
                      polar=dict(bgcolor="rgba(255,255,255,.02)",
                                 radialaxis=dict(range=[0, 100], gridcolor=COLORS["grid"],
                                                 tickfont=dict(size=9, color=COLORS["muted"])),
                                 angularaxis=dict(gridcolor=COLORS["grid"],
                                                  tickfont=dict(size=10,
                                                               color=COLORS["muted"]))),
                      showlegend=True)
    return style(fig, height, legend=True)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def table(df, height: int = 420, column_config: dict | None = None,
          use_container_width: bool = True) -> None:
    """Render a themed dataframe, silently tolerating older Streamlit versions."""
    if df is None or len(df) == 0:
        st.info("No rows match the current filters.")
        return
    try:
        st.dataframe(df, height=height, use_container_width=use_container_width,
                     hide_index=True, column_config=column_config or {})
    except TypeError:
        st.dataframe(df, height=height, use_container_width=use_container_width)


def metric_strip(items: list[tuple[str, str]]) -> None:
    html = "".join(
        f'<div style="min-width:120px"><div style="font-size:.65rem;letter-spacing:.11em;'
        f'text-transform:uppercase;color:{COLORS["muted"]};font-weight:650">{k}</div>'
        f'<div style="font-size:1.02rem;font-weight:700;margin-top:.16rem">{v}</div></div>'
        for k, v in items)
    st.markdown(
        f'<div class="ss-card" style="display:flex;gap:2.1rem;flex-wrap:wrap;'
        f'padding:.8rem 1.1rem">{html}</div>', unsafe_allow_html=True)


def empty_state(title: str, message: str, icon: str = "◇") -> None:
    st.markdown(
        f"""<div class="ss-card" style="text-align:center;padding:2.2rem 1rem">
        <div style="font-size:1.7rem;opacity:.5">{icon}</div>
        <div style="font-weight:700;margin-top:.4rem">{title}</div>
        <div style="color:{COLORS['muted']};font-size:.82rem;margin-top:.3rem">{message}</div>
        </div>""", unsafe_allow_html=True)


def risk_legend() -> None:
    html = "".join(
        f'<span class="ss-pill"><span class="ss-dot" style="background:{b["color"]}"></span>'
        f'{b["label"]} ({int(b["min"]*100)}–{int(b["max"]*100)}%)</span>'
        for b in RISK_BANDS)
    st.markdown(f'<div style="display:flex;gap:.4rem;flex-wrap:wrap;margin:.2rem 0 .7rem">'
                f'{html}</div>', unsafe_allow_html=True)


__all__ = ["kpi", "kpi_row", "badge", "risk_badge", "status_badge", "pills", "card_open",
           "card_close", "style", "line", "bars", "donut", "scatter_risk", "gauge", "heatmap",
           "radar", "table", "metric_strip", "empty_state", "risk_legend", "STATUS_COLORS"]
