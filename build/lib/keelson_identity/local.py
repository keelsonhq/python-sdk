"""Deterministic local-development provider for ``keelson_identity``.

Set ``KEELSON_LOCAL_MODE=1`` to return fixture data without making HTTP calls.
The current user can be customized with ``KEELSON_LOCAL_USER_ID``,
``KEELSON_LOCAL_USER_EMAIL``, ``KEELSON_LOCAL_USER_NAME``,
``KEELSON_LOCAL_TENANT_ID``, ``KEELSON_LOCAL_TENANT_ROLE``, and
``KEELSON_LOCAL_APP_ID``.

The configured current user is always included in member-directory results,
even when its ID or email differs from the built-in companion users. This keeps
``get_current_user()`` and ``get_user()`` consistent in local mode. Member
records include the tenant role, and group membership is derived from that role.
"""

from __future__ import annotations

import os

from .client import (
    AppIdentity,
    AttributesIdentity,
    CurrentIdentity,
    GroupItem,
    MemberItem,
    PaginatedMembers,
    TenantIdentity,
    UserIdentity,
)


def is_local_mode() -> bool:
    """Return True when local development mode is enabled."""
    return os.environ.get("KEELSON_LOCAL_MODE", "").strip() in ("1", "true", "yes")


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


def _local_user_id() -> str:
    return _env("KEELSON_LOCAL_USER_ID", "local-user-001")


def _local_user_email() -> str:
    return _env("KEELSON_LOCAL_USER_EMAIL", "dev@localhost")


def _local_user_name() -> str:
    return _env("KEELSON_LOCAL_USER_NAME", "Local Developer")


def _local_tenant_id() -> str:
    return _env("KEELSON_LOCAL_TENANT_ID", "local-tenant-001")


def _local_tenant_role() -> str:
    return _env("KEELSON_LOCAL_TENANT_ROLE", "OWNER")


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

# Maps each tenant role to the system groups it implies.
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


def _build_members() -> list[dict[str, str]]:
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
    return UserIdentity(
        id=_local_user_id(),
        email=_local_user_email(),
        name=_local_user_name(),
    )


def local_get_current_identity() -> CurrentIdentity:
    """Return a deterministic full identity for local development.

    The ``attributes.groups`` list is derived from the configured tenant role.
    """
    role = _local_tenant_role()
    groups = _ROLE_GROUP_MAP.get(role, ["everyone"])
    return CurrentIdentity(
        user=UserIdentity(
            id=_local_user_id(),
            email=_local_user_email(),
            name=_local_user_name(),
        ),
        tenant=TenantIdentity(
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

    filtered = _build_members()
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
        match = next((g for g in _FIXTURE_GROUPS if g.id == group_id), None)
        if match is not None and match.key is not None:
            resolved_group_key = match.key
        else:
            group_unmatched = True

    if group_unmatched:
        filtered = []
    elif resolved_group_key:
        # System groups: filter by role.  Unknown group_key → empty.
        qualifying_roles = _GROUP_ROLE_MAP.get(resolved_group_key)
        if qualifying_roles is not None:
            filtered = [m for m in filtered if m["role"] in qualifying_roles]
        else:
            filtered = []

    page = filtered[effective_offset:effective_offset + effective_limit]
    has_next = len(filtered) > effective_offset + effective_limit
    items = [
        MemberItem(id=m["id"], email=m["email"], name=m["name"], role=m["role"])
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

    Resolves against the full member list (env user + companions).
    """
    from .client import IdentityError

    for m in _build_members():
        if m["id"] == user_id:
            return MemberItem(id=m["id"], email=m["email"], name=m["name"], role=m["role"])
    raise IdentityError(f"Identity API returned 404 (not found). user_id={user_id}")


def local_list_groups() -> list[GroupItem]:
    """Return deterministic group list for local development."""
    return list(_FIXTURE_GROUPS)
