"""Deterministic local-development provider for ``keelson_identity``.

Set ``KEELSON_LOCAL_MODE=1`` to return fixture data without making HTTP calls.
The current user can be customized with ``KEELSON_LOCAL_USER_ID``,
``KEELSON_LOCAL_USER_EMAIL``, ``KEELSON_LOCAL_USER_NAME``,
``KEELSON_LOCAL_WORKSPACE_ID``, ``KEELSON_LOCAL_WORKSPACE_ROLE`` (with the
legacy ``TENANT`` names as fallbacks), and
``KEELSON_LOCAL_APP_ID``.

The configured current user is always included in member-directory results,
even when its ID or email differs from the built-in companion users. This keeps
``get_current_user()`` and ``get_user()`` consistent in local mode. Member
records include the workspace role, and group membership is derived from that role.

A local users file (``KEELSON_LOCAL_USERS_FILE``, or ``./.keelson/dev-users.json``
when it exists) replaces that fixture data: the fixed user, members, groups and
full identity are then built from the file. Local mode refuses to run when a
Keelson deployment is detected (see ``docs/specs/local-dev-spec.md``).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import (
    AppIdentity,
    AttributesIdentity,
    CurrentIdentity,
    GroupItem,
    IdentityError,
    MemberItem,
    PaginatedMembers,
    RequestUser,
    UserIdentity,
    WorkspaceIdentity,
)


def is_local_mode() -> bool:
    """Return True when local development mode is enabled."""
    return os.environ.get("KEELSON_LOCAL_MODE", "").strip() in ("1", "true", "yes")


# Env vars that only a Keelson deployment sets, in error-message order.
_PRODUCTION_MARK_VARS = (
    "KEELSON_APP_ID",
    "KEELSON_WORKSPACE_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
    "KEELSON_APP_URL",
)


def _production_marks() -> list[str]:
    marks: list[str] = []
    if os.environ.get("KEELSON_MODE", "").strip().lower() == "keelson":
        marks.append("KEELSON_MODE=keelson")
    marks.extend(name for name in _PRODUCTION_MARK_VARS if os.environ.get(name, "").strip())
    return marks


def _use_local_mode() -> bool:
    """Return True when local mode applies; raise if this is a Keelson deployment.

    Local mode never returns fixed data, and never silently falls back to the
    production path, when a production mark is present.
    """
    if not is_local_mode():
        return False
    marks = _production_marks()
    if marks:
        raise IdentityError(
            "KEELSON_LOCAL_MODE is set, but this is a Keelson deployment "
            f"({', '.join(marks)}). Local mode is for local development only; "
            "unset KEELSON_LOCAL_MODE."
        )
    return True


# -- Local users file --

_DEFAULT_USERS_FILE = Path(".keelson") / "dev-users.json"
_ALLOWED_PERMS = ("view", "manage")


@dataclass(frozen=True)
class _RosterUser:
    id: str
    email: str
    name: str
    perms: list[str]  # normalized: ["view"] or ["view", "manage"]
    image_url: str | None

    @property
    def manages(self) -> bool:
        return "manage" in self.perms

    @property
    def role(self) -> str:
        return "ADMIN" if self.manages else "APP_USER"


def _invalid(path: Path, reason: str) -> IdentityError:
    return IdentityError(f"Invalid local users file {path}: {reason}")


def _parse_roster_user(raw: Any, index: int, path: Path) -> _RosterUser:
    where = f"users[{index}]"
    if not isinstance(raw, dict):
        raise _invalid(path, f"{where} must be an object")
    user_id = raw.get("id")
    if not isinstance(user_id, str) or not user_id:
        raise _invalid(path, f"{where}.id must be a non-empty string")
    for field in ("email", "name"):
        if not isinstance(raw.get(field), str):
            raise _invalid(path, f"{where}.{field} must be a string")
    perms = raw.get("perms")
    if (
        not isinstance(perms, list)
        or "view" not in perms
        or any(p not in _ALLOWED_PERMS for p in perms)
        or len(set(perms)) != len(perms)
    ):
        raise _invalid(
            path, f'{where}.perms must contain "view", only "view" / "manage", without duplicates'
        )
    image_url = raw.get("image_url")
    if image_url is not None and not isinstance(image_url, str):
        raise _invalid(path, f"{where}.image_url must be a string or null")
    return _RosterUser(
        id=user_id,
        email=raw["email"],
        name=raw["name"],
        perms=["view", "manage"] if "manage" in perms else ["view"],
        image_url=image_url,
    )


def _load_roster() -> list[_RosterUser] | None:
    """Read the local users file, or return None when there is none.

    An explicitly named file that is missing, unreadable, or malformed raises
    ``IdentityError``; it never falls back to the built-in fixture data.
    """
    explicit = os.environ.get("KEELSON_LOCAL_USERS_FILE", "").strip()
    path = Path(explicit) if explicit else _DEFAULT_USERS_FILE
    if not explicit and not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IdentityError(f"Cannot read local users file {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise _invalid(path, f"not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise _invalid(path, "the top level must be an object")
    users_raw = data.get("users")
    if not isinstance(users_raw, list) or not users_raw:
        raise _invalid(path, '"users" must be a non-empty array')
    users = [_parse_roster_user(raw, i, path) for i, raw in enumerate(users_raw)]
    if len({u.id for u in users}) != len(users):
        raise _invalid(path, "user ids must be unique")
    return users


def _fixed_roster_user(roster: list[_RosterUser]) -> _RosterUser:
    """The first user with ``manage``, else the first user."""
    return next((u for u in roster if u.manages), roster[0])


def _blank_to_none(value: str) -> str | None:
    return value if value.strip() else None


_ROSTER_GROUPS: list[GroupItem] = [
    GroupItem(id="local-group-admins", key="admins", display_name="Admins", kind="SYSTEM", system_kind="admins"),
    GroupItem(id="local-group-everyone", key="everyone", display_name="Everyone", kind="SYSTEM", system_kind="everyone"),
]

# Roster roles are ADMIN (with manage) or APP_USER, so admins == ADMIN.
_ROSTER_GROUP_ROLE_MAP: dict[str, list[str]] = {
    "admins": ["ADMIN"],
    "everyone": ["ADMIN", "APP_USER"],
}


def _roster_member(user: _RosterUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "image_url": user.image_url,
    }


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


def _local_user_id() -> str:
    return _env("KEELSON_LOCAL_USER_ID", "local-user-001")


def _local_user_email() -> str:
    return _env("KEELSON_LOCAL_USER_EMAIL", "dev@localhost")


def _local_user_name() -> str:
    return _env("KEELSON_LOCAL_USER_NAME", "Local Developer")


def _local_tenant_id() -> str:
    return os.environ.get("KEELSON_LOCAL_WORKSPACE_ID", "").strip() or _env(
        "KEELSON_LOCAL_TENANT_ID", "local-tenant-001"
    )


def _local_tenant_role() -> str:
    return os.environ.get("KEELSON_LOCAL_WORKSPACE_ROLE", "").strip() or _env(
        "KEELSON_LOCAL_TENANT_ROLE", "OWNER"
    )


def _local_app_id() -> str:
    return _env("KEELSON_LOCAL_APP_ID", "local-app-001")


# -- Built-in companion members (always present alongside the env user) --

_COMPANION_MEMBERS: list[dict[str, str]] = [
    {
        "id": "local-user-002",
        "email": "alice@localhost",
        "name": "Alice (local)",
        "role": "ADMIN",
    },
    {
        "id": "local-user-003",
        "email": "bob@localhost",
        "name": "Bob (local)",
        "role": "BUILDER",
    },
    {
        "id": "local-user-004",
        "email": "carol@localhost",
        "name": "Carol (local)",
        "role": "APP_USER",
    },
]

_FIXTURE_GROUPS: list[GroupItem] = [
    GroupItem(id="local-group-everyone", key="everyone", display_name="Everyone", kind="SYSTEM", system_kind="everyone"),
    GroupItem(id="local-group-developers", key="developers", display_name="Developers", kind="SYSTEM", system_kind="developers"),
    GroupItem(id="local-group-admins", key="admins", display_name="Admins", kind="SYSTEM", system_kind="admins"),
    GroupItem(id="local-group-owners", key="owners", display_name="Owners", kind="SYSTEM", system_kind="owners"),
]

# Maps each workspace role to the system groups it implies.
_ROLE_GROUP_MAP: dict[str, list[str]] = {
    "OWNER": ["everyone", "developers", "admins", "owners"],
    "ADMIN": ["everyone", "developers", "admins"],
    "BUILDER": ["everyone", "developers"],
    "APP_USER": ["everyone"],
}

# Inverse: system_kind → qualifying roles.
_GROUP_ROLE_MAP: dict[str, list[str]] = {
    "everyone": ["OWNER", "ADMIN", "BUILDER", "APP_USER"],
    "developers": ["OWNER", "ADMIN", "BUILDER"],
    "admins": ["OWNER", "ADMIN"],
    "owners": ["OWNER"],
}


def _build_members() -> list[dict[str, Any]]:
    """Build the member list, always including the env-configured user."""
    env_user = {
        "id": _local_user_id(),
        "email": _local_user_email(),
        "name": _local_user_name(),
        "role": _local_tenant_role(),
    }
    # Start with env user, then add companions that don't collide on id.
    env_id = env_user["id"]
    members = [env_user]
    for m in _COMPANION_MEMBERS:
        if m["id"] != env_id:
            members.append(m)
    return members


def local_get_current_user() -> UserIdentity:
    """Return a deterministic current user for local development."""
    roster = _load_roster()
    if roster is not None:
        user = _fixed_roster_user(roster)
        return UserIdentity(id=user.id, email=_blank_to_none(user.email), name=_blank_to_none(user.name))
    return UserIdentity(
        id=_local_user_id(),
        email=_local_user_email(),
        name=_local_user_name(),
    )


def local_get_request_user() -> RequestUser:
    """Return the fixed user with its app permissions for local development."""
    roster = _load_roster()
    if roster is not None:
        user = _fixed_roster_user(roster)
        return RequestUser(
            id=user.id,
            email=_blank_to_none(user.email),
            name=_blank_to_none(user.name),
            perms=list(user.perms),
        )
    return RequestUser(
        id=_local_user_id(),
        email=_local_user_email(),
        name=_local_user_name(),
        perms=["view", "manage"],
    )


def local_get_current_identity() -> CurrentIdentity:
    """Return a deterministic full identity for local development.

    Without a users file, ``attributes.groups`` is derived from the configured
    workspace role. With one, role, permissions and groups come from the fixed
    user's ``perms``.
    """
    roster = _load_roster()
    if roster is not None:
        user = _fixed_roster_user(roster)
        return CurrentIdentity(
            user=UserIdentity(id=user.id, email=user.email, name=user.name),
            workspace=WorkspaceIdentity(id=_local_tenant_id(), role=user.role),
            app=AppIdentity(id=_local_app_id(), permissions=sorted(user.perms), roles=[]),
            attributes=AttributesIdentity(groups=["admins", "everyone"] if user.manages else ["everyone"]),
        )
    role = _local_tenant_role()
    groups = _ROLE_GROUP_MAP.get(role, ["everyone"])
    return CurrentIdentity(
        user=UserIdentity(
            id=_local_user_id(),
            email=_local_user_email(),
            name=_local_user_name(),
        ),
        workspace=WorkspaceIdentity(
            id=_local_tenant_id(),
            role=role,
        ),
        app=AppIdentity(
            id=_local_app_id(),
            permissions=["manage", "view"],
            roles=[],
        ),
        attributes=AttributesIdentity(groups=groups),
    )


def _directory() -> tuple[list[dict[str, Any]], list[GroupItem], dict[str, list[str]]]:
    """Members, groups, and group → qualifying roles, from the users file if any."""
    roster = _load_roster()
    if roster is not None:
        return [_roster_member(u) for u in roster], _ROSTER_GROUPS, _ROSTER_GROUP_ROLE_MAP
    return _build_members(), _FIXTURE_GROUPS, _GROUP_ROLE_MAP


def local_list_members(
    *,
    limit: int | None = None,
    offset: int | None = None,
    q: str | None = None,
    role: str | None = None,
    group_key: str | None = None,
    group_id: str | None = None,
) -> PaginatedMembers:
    """Return deterministic member list for local development.

    Supports ``q``, ``role``, ``group_key``, and ``group_id`` filters so
    that apps using group-filtered assignee/candidate UIs can be tested
    locally.
    """
    effective_limit = limit if limit is not None else 25
    effective_offset = offset if offset is not None else 0

    filtered, groups, group_role_map = _directory()
    if q:
        q_lower = q.lower()
        filtered = [
            m for m in filtered
            if q_lower in m["name"].lower() or q_lower in m["email"].lower()
        ]
    if role:
        filtered = [m for m in filtered if m["role"] == role]

    # Resolve group_id to its key; an unknown id matches no group. The
    # caller (list_members) rejects group_id + group_key together, so at
    # most one is set here.
    resolved_group_key = group_key
    group_unmatched = False
    if group_id is not None:
        match = next((g for g in groups if g.id == group_id), None)
        if match is not None and match.key is not None:
            resolved_group_key = match.key
        else:
            group_unmatched = True

    if group_unmatched:
        filtered = []
    elif resolved_group_key:
        # System groups: filter by role.  Unknown group_key → empty.
        qualifying_roles = group_role_map.get(resolved_group_key)
        if qualifying_roles is not None:
            filtered = [m for m in filtered if m["role"] in qualifying_roles]
        else:
            filtered = []

    page = filtered[effective_offset:effective_offset + effective_limit]
    has_next = len(filtered) > effective_offset + effective_limit
    items = [
        MemberItem(
            id=m["id"], email=m["email"], name=m["name"], role=m["role"], image_url=m.get("image_url")
        )
        for m in page
    ]
    return PaginatedMembers(
        items=items,
        limit=effective_limit,
        offset=effective_offset,
        next_offset=effective_offset + effective_limit if has_next else None,
    )


def local_get_user(user_id: str) -> MemberItem:
    """Return a deterministic user for local development.

    Resolves against the users file when there is one, else the full member
    list (env user + companions).
    """
    members, _, _ = _directory()
    for m in members:
        if m["id"] == user_id:
            return MemberItem(
                id=m["id"], email=m["email"], name=m["name"], role=m["role"], image_url=m.get("image_url")
            )
    raise IdentityError(f"Identity API returned 404 (not found). user_id={user_id}")


def local_list_groups() -> list[GroupItem]:
    """Return deterministic group list for local development."""
    _, groups, _ = _directory()
    return list(groups)
