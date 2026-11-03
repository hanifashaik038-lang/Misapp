"""
Authentication and role-based access control.

Demo credentials are seeded with PBKDF2-SHA256 hashes (200k iterations,
per-user salt). No plain-text password is ever stored, and the demo passwords
in ``config.DEMO_USERS`` exist only so a reviewer can log in – production
deployment would replace them with an SSO/IdP flow.
"""
from __future__ import annotations

from datetime import datetime

from database.db import execute, query
from modules import audit
from utils.helpers import get_logger, verify_password

log = get_logger("stocksense.auth")

ROLE_RANK = {
    "STORE_EMPLOYEE": 1,
    "STORE_MANAGER": 2,
    "REGIONAL_MANAGER": 3,
    "HEAD_OFFICE_ADMIN": 4,
}


def authenticate(username: str, password: str) -> tuple[dict | None, str]:
    """Return ``(user_dict, error_message)``."""
    username = (username or "").strip()
    if not username or not password:
        return None, "Enter both username and password."

    rows = query(
        "SELECT u.user_id, u.username, u.password_hash, u.full_name, u.email, u.store_id, "
        "u.is_active, r.role_code, r.role_name, s.store_code, s.store_name "
        "FROM users u JOIN roles r ON r.role_id = u.role_id "
        "LEFT JOIN stores s ON s.store_id = u.store_id WHERE u.username = :u",
        {"u": username},
    )
    if rows.empty:
        audit.record("LOGIN_FAILED", detail=f"unknown username '{username}'", result="FAILURE")
        return None, "Invalid username or password."

    row = rows.iloc[0].to_dict()
    if not int(row["is_active"]):
        return None, "This account is disabled. Contact an administrator."
    if not verify_password(password, row["password_hash"]):
        audit.record("LOGIN_FAILED", detail=f"bad password for '{username}'",
                     result="FAILURE", user=row)
        return None, "Invalid username or password."

    execute("UPDATE users SET last_login_at = :t WHERE user_id = :u",
            {"t": datetime.utcnow().isoformat(), "u": int(row["user_id"])})
    user = {k: row[k] for k in
            ("user_id", "username", "full_name", "email", "store_id", "role_code",
             "role_name", "store_code", "store_name")}
    user["user_id"] = int(user["user_id"])
    user["store_id"] = int(user["store_id"]) if user["store_id"] is not None else None
    audit.record("LOGIN_SUCCESS", user=user, detail=f"role={user['role_code']}")
    log.info("login ok: %s (%s)", username, user["role_code"])
    return user, ""


# --------------------------------------------------------------------------
# Capability checks
# --------------------------------------------------------------------------
def can(user: dict | None, capability: str) -> bool:
    """Simple capability gate used by the UI to hide/show actions."""
    if not user:
        return False
    role = user.get("role_code")
    matrix = {
        "STORE_EMPLOYEE": {"scan", "receive", "inventory_read", "alerts_read"},
        "STORE_MANAGER": {"scan", "receive", "inventory_read", "inventory_adjust",
                          "po_create", "alerts_manage", "prediction_read", "reports",
                          "kpi_read", "supplier_read"},
        "REGIONAL_MANAGER": {"inventory_read", "po_approve", "alerts_manage",
                             "prediction_read", "reports", "kpi_read", "supplier_read",
                             "sync_manage"},
        "HEAD_OFFICE_ADMIN": {"*"},
    }
    perms = matrix.get(role, set())
    return "*" in perms or capability in perms


def is_chain_level(user: dict | None) -> bool:
    return bool(user) and user.get("role_code") in {"HEAD_OFFICE_ADMIN", "REGIONAL_MANAGER"}


def allowed_store_ids(user: dict | None) -> list[int] | None:
    """``None`` means chain-wide access; otherwise a one-store list."""
    if not user:
        return []
    if is_chain_level(user):
        return None
    sid = user.get("store_id")
    return [sid] if sid else []


def scope_clause(user: dict, column: str = "s.store_id") -> tuple[str, dict]:
    """SQL fragment + params that restrict a query to the user's scope."""
    ids = allowed_store_ids(user)
    if ids is None:
        return "1=1", {}
    if not ids:
        return "1=0", {}
    return f"{column} IN ({','.join(str(int(i)) for i in ids)})", {}


def create_user(username: str, password: str, full_name: str, role_code: str,
                store_id: int | None, email: str = "", actor: dict | None = None) -> int:
    from utils.helpers import hash_password
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if query("SELECT 1 FROM users WHERE username = :u", {"u": username}).shape[0]:
        raise ValueError(f"Username '{username}' already exists.")
    execute(
        "INSERT INTO users (username, password_hash, full_name, email, role_id, store_id) "
        "VALUES (:u,:p,:f,:e,(SELECT role_id FROM roles WHERE role_code = :r),:s)",
        {"u": username, "p": hash_password(password), "f": full_name, "e": email,
         "r": role_code, "s": store_id},
    )
    audit.record("USER_CREATED", user=actor, entity="users",
                 detail=f"{username} / {role_code}")
    return int(query("SELECT user_id FROM users WHERE username = :u", {"u": username})
               .iloc[0]["user_id"])


def set_active(user_id: int, active: bool, actor: dict | None = None) -> None:
    execute("UPDATE users SET is_active = :a WHERE user_id = :u",
            {"a": 1 if active else 0, "u": int(user_id)})
    audit.record("USER_STATUS_CHANGED", user=actor, entity="users", entity_id=user_id,
                 detail=f"active={active}")


def list_users() -> "object":
    return query(
        "SELECT u.user_id, u.username, u.full_name, u.email, r.role_code, r.role_name, "
        "s.store_code, u.is_active, u.last_login_at "
        "FROM users u JOIN roles r ON r.role_id = u.role_id "
        "LEFT JOIN stores s ON s.store_id = u.store_id ORDER BY r.role_id DESC, u.username"
    )
