"""Append-only audit trail. Every state-changing action funnels through here."""
from __future__ import annotations

from typing import Any

from database.db import execute
from utils.helpers import get_logger

log = get_logger("stocksense.audit")


def record(
    action: str,
    *,
    user: dict | None = None,
    entity: str | None = None,
    entity_id: Any = None,
    store_id: int | None = None,
    detail: str | None = None,
    result: str = "SUCCESS",
) -> None:
    """Write one audit row. Never raises – auditing must not break a business action."""
    try:
        execute(
            "INSERT INTO audit_logs (user_id, username, action, entity, entity_id, store_id, "
            "detail, result) VALUES (:u,:n,:a,:e,:i,:s,:d,:r)",
            {
                "u": (user or {}).get("user_id"),
                "n": (user or {}).get("username", "system"),
                "a": action, "e": entity,
                "i": None if entity_id is None else str(entity_id),
                "s": store_id or (user or {}).get("store_id"),
                "d": (detail or "")[:2000], "r": result,
            },
        )
    except Exception as exc:  # pragma: no cover
        log.warning("audit write failed: %s", exc)


def recent(limit: int = 40):
    from database.db import query
    return query(
        "SELECT occurred_at, username, action, entity, entity_id, store_id, result, detail "
        "FROM audit_logs ORDER BY audit_id DESC LIMIT :l", {"l": int(limit)}
    )
