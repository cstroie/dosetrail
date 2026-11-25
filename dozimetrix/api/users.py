"""User administration and the audit-log viewer (admin role).

Rules:
* passwords: at least 10 characters, stored as scrypt hashes, never logged;
* roles: viewer, exporter, pid (identified data), importer, admin;
* an administrator cannot disable themselves or remove their own admin role, so the
  system always keeps at least the one administrator making the change;
* a username can be renamed (same rules as at creation); the login cookie holds the user id,
  so the user stays signed in, while past audit rows keep the name they were written with;
* only a user without the admin role can be removed (take the role away first); their audit
  rows stay, with the username they were written with.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db.models import AuditLog, User
from ..security import hash_password

ROLES = ("viewer", "exporter", "pid", "importer", "admin")
ROLE_HELP = {"viewer": "see studies, statistics and reports (pseudonyms)",
             "exporter": "download exports and reports",
             "pid": "see patient identifiers (each reveal is logged)",
             "importer": "import files and run query/retrieve",
             "admin": "devices, DICOM nodes, mappings, rules, users"}


class UserError(ValueError):
    pass


def as_dict(u: User) -> dict:
    return {"username": u.username, "name": u.display_name, "roles": sorted(u.role_set()),
            "active": u.is_active,
            "last_login": u.last_login_at.isoformat(timespec="minutes") if u.last_login_at
            else None,
            "created": u.created_at.isoformat(timespec="minutes") if u.created_at else None}


def _roles(roles: list[str] | None) -> str:
    rs = [r.strip() for r in (roles or []) if r.strip()]
    bad = [r for r in rs if r not in ROLES]
    if bad:
        raise UserError(f"unknown role(s): {', '.join(bad)}")
    return ",".join(sorted(set(rs) or {"viewer"}))


def _password(pw: str) -> str:
    if len(pw or "") < 10:
        raise UserError("the password needs at least 10 characters")
    return hash_password(pw)


def change_own_password(user: User, current: str, new: str) -> None:
    """A user's own password: the current one must be right; the same rules as for an admin."""
    from ..security import verify_password

    if not verify_password(current or "", user.password_hash):
        raise UserError("the current password is wrong")
    if new == current:
        raise UserError("the new password is the same as the current one")
    user.password_hash = _password(new)


def listing(session: Session) -> list[dict]:
    return [as_dict(u) for u in session.scalars(select(User).order_by(User.username))]


def valid_username(session: Session, raw: str | None) -> str:
    username = (raw or "").strip()
    if not username or len(username) > 64 or not username.isascii() or " " in username:
        raise UserError("username: 1–64 ASCII characters, no spaces")
    if session.scalar(select(User).filter_by(username=username)) is not None:
        raise UserError(f"user {username!r} exists")
    return username


def create(session: Session, data: dict) -> User:
    username = valid_username(session, data.get("username"))
    u = User(username=username, display_name=(data.get("name") or "").strip() or None,
             roles=_roles(data.get("roles")), password_hash=_password(data.get("password")),
             is_active=True)
    session.add(u)
    session.flush()
    return u


def update(session: Session, acting: User, username: str, patch: dict) -> tuple[User, dict]:
    u = session.scalar(select(User).filter_by(username=username))
    if u is None:
        raise UserError("user not found")
    changes: dict = {}
    if patch.get("username") and patch["username"].strip() != u.username:
        new = valid_username(session, patch["username"])
        changes["username"] = {"from": u.username, "to": new}
        u.username = new
    if "name" in patch:
        name = (patch["name"] or "").strip() or None
        if name != u.display_name:
            changes["name"] = {"from": u.display_name, "to": name}
            u.display_name = name
    if "roles" in patch:
        roles = _roles(patch["roles"])
        if u.id == acting.id and "admin" not in roles.split(","):
            raise UserError("you cannot remove your own admin role")
        if roles != u.roles:
            changes["roles"] = {"from": u.roles, "to": roles}
            u.roles = roles
    if "active" in patch and bool(patch["active"]) != u.is_active:
        if u.id == acting.id and not patch["active"]:
            raise UserError("you cannot disable your own account")
        changes["active"] = {"from": u.is_active, "to": bool(patch["active"])}
        u.is_active = bool(patch["active"])
    if patch.get("password"):
        u.password_hash = _password(patch["password"])
        changes["password"] = "changed"  # never the value
    session.flush()
    return u, changes


def remove(session: Session, acting: User, username: str) -> dict:
    """Delete a non-admin account; returns what it was, for the audit row."""
    u = session.scalar(select(User).filter_by(username=username))
    if u is None:
        raise UserError("user not found")
    if u.id == acting.id:
        raise UserError("you cannot remove your own account")
    if "admin" in u.role_set():
        raise UserError("an administrator cannot be removed; take away the admin role first")
    gone = {"name": u.display_name, "roles": u.roles}
    session.delete(u)
    session.flush()
    return gone


def audit(session: Session, limit: int = 200, action: str | None = None,
          user: str | None = None, text: str | None = None,
          since: dt.date | None = None, before_id: int | None = None) -> dict:
    q = select(AuditLog)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    if user:
        q = q.where(AuditLog.username == user)
    if text:
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.where(or_(AuditLog.target.like(like, escape="\\"),
                        AuditLog.action.like(like, escape="\\")))
    if since:
        q = q.where(AuditLog.at >= dt.datetime.combine(since, dt.time()))
    if before_id:
        q = q.where(AuditLog.id < before_id)
    rows = list(session.scalars(q.order_by(AuditLog.id.desc()).limit(limit)))
    actions = [a for (a,) in session.execute(
        select(AuditLog.action).group_by(AuditLog.action).order_by(func.count().desc()))]
    return {"rows": [{"id": r.id, "at": r.at.isoformat(timespec="seconds") if r.at else None,
                      "user": r.username, "action": r.action, "target": r.target,
                      "detail": r.detail, "from": r.remote_addr} for r in rows],
            "actions": actions, "more": len(rows) == limit}
