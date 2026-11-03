"""
Alert and notification service.

Implements the alert lifecycle (OPEN → ACKNOWLEDGED → ASSIGNED → RESOLVED →
CLOSED), deduplication so one unresolved risk does not spam the store team,
and a notification dispatcher with mock email / SMS / Slack transports.

Delivery SLA: the system targets alert delivery inside 5 minutes of detection
(config.ALERT_DELIVERY_TARGET_MINUTES). ``alerts`` stores ``detected_at`` and
``delivered_at`` so the achieved latency is measurable, not claimed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd

from config import ALERT_DEDUP_WINDOW_HOURS, ALERT_DELIVERY_TARGET_MINUTES
from database.db import execute, query, scalar
from modules import audit
from utils.helpers import get_logger, json_loads, safe_json

log = get_logger("stocksense.alerts")


# --------------------------------------------------------------------------
# Notification transports (mocked – integration-ready interface)
# --------------------------------------------------------------------------
class NotificationTransport:
    """Interface every channel implements. Replace ``send`` with a real API call."""

    name = "ABSTRACT"

    def send(self, alert: dict, recipient: dict | None = None) -> dict:
        raise NotImplementedError


class InAppTransport(NotificationTransport):
    name = "IN_APP"

    def send(self, alert, recipient=None):
        return {"channel": self.name, "status": "DELIVERED",
                "detail": "Visible in the Alerts inbox"}


class EmailTransport(NotificationTransport):
    name = "EMAIL"

    def send(self, alert, recipient=None):
        to = (recipient or {}).get("email") or "store.manager@freshmart.example"
        return {"channel": self.name, "status": "QUEUED_MOCK",
                "detail": f"MOCK SMTP – would email {to}: {alert['alert_ref']}"}


class SMSTransport(NotificationTransport):
    name = "SMS"

    def send(self, alert, recipient=None):
        to = (recipient or {}).get("phone") or "+91-XXXXXXXXXX"
        return {"channel": self.name, "status": "QUEUED_MOCK",
                "detail": f"MOCK SMS gateway – would text {to}"}


class SlackTransport(NotificationTransport):
    name = "SLACK"

    def send(self, alert, recipient=None):
        return {"channel": self.name, "status": "QUEUED_MOCK",
                "detail": "MOCK webhook – would post to #freshmart-ops"}


TRANSPORTS = {cls.name: cls() for cls in
              [InAppTransport, EmailTransport, SMSTransport, SlackTransport]}


def dispatch(alert: dict, channels: list[str] | None = None,
             recipient: dict | None = None) -> list[dict]:
    """Send an alert over the requested channels; returns per-channel results."""
    channels = channels or ["IN_APP", "EMAIL"]
    results = []
    for ch in channels:
        transport = TRANSPORTS.get(ch.upper())
        if not transport:
            results.append({"channel": ch, "status": "UNSUPPORTED", "detail": "No transport"})
            continue
        try:
            results.append(transport.send(alert, recipient))
        except Exception as exc:  # pragma: no cover
            results.append({"channel": ch, "status": "FAILED", "detail": str(exc)})
    return results


# --------------------------------------------------------------------------
# Deduplication
# --------------------------------------------------------------------------
def _dedup_key(store_id: int, sku_id: int, alert_type: str) -> str:
    return f"{store_id}:{sku_id}:{alert_type}"


def is_duplicate(store_id: int, sku_id: int, alert_type: str,
                 window_hours: int = ALERT_DEDUP_WINDOW_HOURS) -> dict | None:
    """Return the existing open alert if one is already live for this risk."""
    since = (datetime.now() - timedelta(hours=window_hours)).isoformat()
    df = query(
        "SELECT alert_id, alert_ref, status, detected_at FROM alerts "
        "WHERE dedup_key = :k AND status IN ('OPEN','ACKNOWLEDGED','ASSIGNED') "
        "AND detected_at >= :s ORDER BY alert_id DESC LIMIT 1",
        {"k": _dedup_key(store_id, sku_id, alert_type), "s": since})
    return None if df.empty else df.iloc[0].to_dict()


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------
def create_alert(store_id: int, sku_id: int, alert_type: str, severity: str,
                 probability: float | None, stock_at_alert: int, message: str,
                 recommended: str = "", channels: list[str] | None = None,
                 linked_po_id: int | None = None, force: bool = False) -> dict | None:
    """Create an alert unless an equivalent one is already unresolved."""
    if not force:
        dup = is_duplicate(store_id, sku_id, alert_type)
        if dup:
            log.debug("alert suppressed (dedup) for %s/%s/%s", store_id, sku_id, alert_type)
            return {"suppressed": True, "existing": dup}

    seq = int(scalar("SELECT COUNT(*) FROM alerts", 0)) + 1
    store_code = scalar("SELECT store_code FROM stores WHERE store_id = :s",
                        {"s": int(store_id)}, "ST")
    sku_code = scalar("SELECT sku_code FROM sku WHERE sku_id = :k", {"k": int(sku_id)}, "SKU")
    ref = f"ALT-{store_code.split('-')[-1]}-{sku_code[-3:]}-{seq:05d}"
    detected = datetime.now()
    key = _dedup_key(store_id, sku_id, alert_type)

    execute(
        "INSERT INTO alerts (alert_ref, store_id, sku_id, alert_type, severity, probability, "
        "stock_at_alert, message, recommended_action, status, detected_at, dedup_key, "
        "notification_channels, linked_po_id) "
        "VALUES (:r,:s,:k,:t,:sev,:p,:q,:m,:a,'OPEN',:d,:dk,:ch,:po)",
        {"r": ref, "s": int(store_id), "k": int(sku_id), "t": alert_type, "sev": severity,
         "p": None if probability is None else float(probability), "q": int(stock_at_alert),
         "m": message[:1000], "a": (recommended or "")[:1000], "d": detected.isoformat(),
         "dk": key, "ch": safe_json(channels or ["IN_APP", "EMAIL"]),
         "po": linked_po_id},
    )
    alert_id = int(scalar("SELECT MAX(alert_id) FROM alerts"))
    alert = {"alert_id": alert_id, "alert_ref": ref, "store_id": store_id, "sku_id": sku_id,
             "alert_type": alert_type, "severity": severity, "message": message}

    results = dispatch(alert, channels)
    delivered = datetime.now()
    latency_ms = int((delivered - detected).total_seconds() * 1000)
    execute("UPDATE alerts SET delivered_at = :d WHERE alert_id = :a",
            {"d": delivered.isoformat(), "a": alert_id})
    audit.record("ALERT_CREATED", entity="alerts", entity_id=alert_id, store_id=store_id,
                 detail=f"{ref} {alert_type} {severity} p={probability} "
                        f"delivery={latency_ms}ms channels={[r['channel'] for r in results]}")
    log.info("alert %s created (%s, %s) latency %sms", ref, alert_type, severity, latency_ms)
    return {"alert_id": alert_id, "alert_ref": ref, "delivery_results": results,
            "latency_ms": latency_ms, "suppressed": False}


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
VALID_TRANSITIONS = {
    "OPEN": {"ACKNOWLEDGED", "ASSIGNED", "RESOLVED", "CLOSED"},
    "ACKNOWLEDGED": {"ASSIGNED", "RESOLVED", "CLOSED"},
    "ASSIGNED": {"RESOLVED", "CLOSED"},
    "RESOLVED": {"CLOSED"},
    "CLOSED": set(),
}


def get_alert(alert_id: int) -> dict | None:
    df = query("SELECT * FROM alerts WHERE alert_id = :a", {"a": int(alert_id)})
    return None if df.empty else df.iloc[0].to_dict()


def transition(alert_id: int, new_status: str, user: dict | None = None,
               note: str = "", assign_to: int | None = None) -> None:
    alert = get_alert(alert_id)
    if not alert:
        raise ValueError("Alert not found.")
    current = alert["status"]
    if new_status not in VALID_TRANSITIONS.get(current, set()):
        raise ValueError(f"Cannot move an alert from {current} to {new_status}.")
    now = datetime.now().isoformat()
    uid = (user or {}).get("user_id")
    if new_status == "ACKNOWLEDGED":
        execute("UPDATE alerts SET status=:s, acknowledged_by=:u, acknowledged_at=:t "
                "WHERE alert_id=:a", {"s": new_status, "u": uid, "t": now, "a": int(alert_id)})
    elif new_status == "ASSIGNED":
        execute("UPDATE alerts SET status=:s, assigned_to=:u, acknowledged_by=COALESCE("
                "acknowledged_by,:u), acknowledged_at=COALESCE(acknowledged_at,:t) "
                "WHERE alert_id=:a",
                {"s": new_status, "u": assign_to or uid, "t": now, "a": int(alert_id)})
    elif new_status == "RESOLVED":
        execute("UPDATE alerts SET status=:s, resolved_by=:u, resolved_at=:t, "
                "resolution_note=:n WHERE alert_id=:a",
                {"s": new_status, "u": uid, "t": now, "n": note[:1000], "a": int(alert_id)})
    else:
        execute("UPDATE alerts SET status=:s WHERE alert_id=:a",
                {"s": new_status, "a": int(alert_id)})
    audit.record(f"ALERT_{new_status}", user=user, entity="alerts", entity_id=alert_id,
                 store_id=alert["store_id"], detail=note or f"{current} → {new_status}")


def acknowledge(alert_id: int, user: dict) -> None:
    transition(alert_id, "ACKNOWLEDGED", user)


def assign(alert_id: int, assign_to: int, user: dict) -> None:
    transition(alert_id, "ASSIGNED", user, assign_to=assign_to)


def resolve(alert_id: int, user: dict, note: str = "") -> None:
    transition(alert_id, "RESOLVED", user, note=note)


def close(alert_id: int, user: dict) -> None:
    transition(alert_id, "CLOSED", user)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------
def list_alerts(store_ids: list[int] | None = None, status: str | None = None,
                severity: str | None = None, alert_type: str | None = None,
                limit: int = 400) -> pd.DataFrame:
    sql = ["""
        SELECT a.alert_id, a.alert_ref, a.alert_type, a.severity, a.status,
               a.probability, a.stock_at_alert, a.detected_at, a.delivered_at,
               a.acknowledged_at, a.resolved_at, a.message, a.recommended_action,
               s.store_code, s.store_name, k.sku_code, k.product_name, c.category_name,
               ua.full_name AS acknowledged_by_name, ur.full_name AS resolved_by_name,
               a.resolution_note
        FROM alerts a
        JOIN stores s ON s.store_id = a.store_id
        JOIN sku k ON k.sku_id = a.sku_id
        JOIN product_categories c ON c.category_id = k.category_id
        LEFT JOIN users ua ON ua.user_id = a.acknowledged_by
        LEFT JOIN users ur ON ur.user_id = a.resolved_by
        WHERE 1=1
    """]
    p: dict = {"l": int(limit)}
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        sql.append("AND a.store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")")
    if status:
        if status == "ACTIVE":
            sql.append("AND a.status IN ('OPEN','ACKNOWLEDGED','ASSIGNED')")
        else:
            sql.append("AND a.status = :st")
            p["st"] = status
    if severity:
        sql.append("AND a.severity = :sev")
        p["sev"] = severity
    if alert_type:
        sql.append("AND a.alert_type = :at")
        p["at"] = alert_type
    sql.append("ORDER BY CASE a.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 "
               "WHEN 'MODERATE' THEN 2 ELSE 3 END, a.detected_at DESC LIMIT :l")
    return query(" ".join(sql), p)


def alert_stats(store_ids: list[int] | None = None) -> dict:
    df = list_alerts(store_ids=store_ids, limit=10000)
    if df.empty:
        return {"total": 0, "open": 0, "critical": 0, "high": 0, "resolved": 0,
                "avg_latency_s": 0, "sla_pct": 0}
    openish = df[df["status"].isin(["OPEN", "ACKNOWLEDGED", "ASSIGNED"])]
    lat = []
    for r in df.dropna(subset=["delivered_at"]).itertuples():
        try:
            lat.append((datetime.fromisoformat(str(r.delivered_at))
                        - datetime.fromisoformat(str(r.detected_at))).total_seconds())
        except Exception:
            pass
    within = sum(1 for x in lat if x <= ALERT_DELIVERY_TARGET_MINUTES * 60)
    return {
        "total": int(len(df)),
        "open": int(len(openish)),
        "critical": int((openish["severity"] == "CRITICAL").sum()),
        "high": int((openish["severity"] == "HIGH").sum()),
        "resolved": int(df["status"].isin(["RESOLVED", "CLOSED"]).sum()),
        "avg_latency_s": round(sum(lat) / len(lat), 2) if lat else 0.0,
        "sla_pct": round(within / len(lat) * 100, 1) if lat else 100.0,
    }


def alert_trend(days: int = 30, store_ids: list[int] | None = None) -> pd.DataFrame:
    where = "WHERE 1=1"
    if store_ids is not None:
        if not store_ids:
            return pd.DataFrame()
        where += " AND store_id IN (" + ",".join(str(int(s)) for s in store_ids) + ")"
    return query(f"""
        SELECT substr(detected_at,1,10) AS alert_date, severity, COUNT(*) AS n
        FROM alerts {where}
        GROUP BY alert_date, severity ORDER BY alert_date
    """)


def channel_performance() -> pd.DataFrame:
    """Measured detection→delivery latency vs the 5-minute SLA."""
    return query("""
        SELECT a.alert_type, a.severity, COUNT(*) AS alerts,
               ROUND(AVG((julianday(a.delivered_at) - julianday(a.detected_at)) * 86400), 2)
                   AS avg_latency_seconds,
               SUM(CASE WHEN (julianday(a.delivered_at) - julianday(a.detected_at)) * 86400
                        <= :sla THEN 1 ELSE 0 END) AS within_sla
        FROM alerts a WHERE a.delivered_at IS NOT NULL
        GROUP BY a.alert_type, a.severity ORDER BY alerts DESC
    """, {"sla": ALERT_DELIVERY_TARGET_MINUTES * 60})
