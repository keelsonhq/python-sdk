from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class IdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserIdentity:
    id: str
    email: str | None
    name: str | None


@dataclass(frozen=True)
class TenantIdentity:
    id: str
    role: str


@dataclass(frozen=True)
class AppIdentity:
    id: str
    permissions: list[str] | None = None
    roles: list[str] | None = None


@dataclass(frozen=True)
class AttributesIdentity:
    groups: list[str] | None = None


@dataclass(frozen=True)
class MemberItem:
    id: str
    email: str
    name: str
    role: str | None = None


@dataclass(frozen=True)
class GroupItem:
    # ``key`` is the code-facing identifier for referencing a group: it is
    # always present (the server guarantees a non-null, normalized key),
    # stable, and immutable — prefer ``key`` for code references. ``id`` is a
    # stable UUID for machine integration / internal wiring. ``key`` stays
    # typed as optional for backward compatibility, but groups returned by the
    # Directory API always carry one.
    id: str
    display_name: str
    kind: str
    key: str | None = None
    system_kind: str | None = None


@dataclass(frozen=True)
class PaginatedMembers:
    items: list[MemberItem]
    limit: int
    offset: int
    next_offset: int | None = None


@dataclass(frozen=True)
class CurrentIdentity:
    user: UserIdentity
    tenant: TenantIdentity
    app: AppIdentity
    attributes: AttributesIdentity | None = None


# App tokens used for app-as-actor Directory access are read from this env
# var when no explicit app_token argument is given.
_DIRECTORY_TOKEN_ENV = "KEELSON_DIRECTORY_TOKEN"


def _identity_base_url() -> str:
    return os.environ.get("KEELSON_IDENTITY_BASE_URL", "").strip().rstrip("/")


def _directory_base_url() -> str:
    """Resolve the Directory API base URL.

    Prefers ``KEELSON_DIRECTORY_BASE_URL`` (set for app-as-actor deployments)
    and falls back to ``KEELSON_IDENTITY_BASE_URL`` for compatibility with the
    existing user-as-actor configuration.
    """
    directory = (
        os.environ.get("KEELSON_DIRECTORY_BASE_URL", "").strip().rstrip("/")
    )
    return directory or _identity_base_url()


def _decode_json(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IdentityError("Identity API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise IdentityError("Identity API returned non-object JSON.")
    return payload


def _parse_identity(payload: dict[str, Any]) -> CurrentIdentity:
    user_raw = payload.get("user")
    tenant_raw = payload.get("tenant")
    app_raw = payload.get("app")
    if not isinstance(user_raw, dict):
        raise IdentityError("Identity response is missing 'user'.")
    if not isinstance(tenant_raw, dict):
        raise IdentityError("Identity response is missing 'tenant'.")
    if not isinstance(app_raw, dict):
        raise IdentityError("Identity response is missing 'app'.")

    user_id = str(user_raw.get("id") or "").strip()
    tenant_id = str(tenant_raw.get("id") or "").strip()
    tenant_role = str(tenant_raw.get("role") or "").strip()
    app_id = str(app_raw.get("id") or "").strip()
    if not user_id:
        raise IdentityError("Identity response is missing user.id.")
    if not tenant_id:
        raise IdentityError("Identity response is missing tenant.id.")
    if not tenant_role:
        raise IdentityError("Identity response is missing tenant.role.")
    if not app_id:
        raise IdentityError("Identity response is missing app.id.")

    email = user_raw.get("email")
    if email is not None:
        email = str(email)
    name = user_raw.get("name")
    if name is not None:
        name = str(name)

    # Parse optional app permissions and roles when the response includes them.
    app_permissions = app_raw.get("permissions")
    if isinstance(app_permissions, list):
        app_permissions = [str(p) for p in app_permissions]
    else:
        app_permissions = None

    app_roles = app_raw.get("roles")
    if isinstance(app_roles, list):
        app_roles = [str(r) for r in app_roles]
    else:
        app_roles = None

    # Parse optional identity attributes when the response includes them.
    attributes_identity: AttributesIdentity | None = None
    attributes_raw = payload.get("attributes")
    if isinstance(attributes_raw, dict):
        groups_raw = attributes_raw.get("groups")
        groups = [str(g) for g in groups_raw] if isinstance(groups_raw, list) else None
        attributes_identity = AttributesIdentity(groups=groups)

    return CurrentIdentity(
        user=UserIdentity(id=user_id, email=email, name=name),
        tenant=TenantIdentity(id=tenant_id, role=tenant_role),
        app=AppIdentity(id=app_id, permissions=app_permissions, roles=app_roles),
        attributes=attributes_identity,
    )


def _normalize_header_value(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            normalized = _normalize_header_value(item)
            if normalized:
                return normalized
        return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None

    getter = getattr(headers, "get", None)
    if callable(getter):
        value = _normalize_header_value(getter(name))
        if value:
            return value

    if isinstance(headers, Mapping):
        lower_name = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lower_name:
                return _normalize_header_value(value)

    return None


def _parse_current_user_headers(headers: Any) -> UserIdentity:
    user_id = _read_header(headers, "x-keelson-user-id")
    if not user_id:
        raise IdentityError("Current user headers are missing 'x-keelson-user-id'.")
    return UserIdentity(
        id=user_id,
        email=_read_header(headers, "x-keelson-user-email"),
        name=_read_header(headers, "x-keelson-user-name"),
    )


def get_current_user(
    *,
    headers: Any = None,
    base_url: str | None = None,
    timeout_sec: float = 5.0,
) -> UserIdentity:
    from .local import is_local_mode, local_get_current_user

    if is_local_mode():
        return local_get_current_user()

    _ = (base_url, timeout_sec)
    return _parse_current_user_headers(headers)


def _resolve_current_identity_authorization(
    *,
    app_token: str | None,
) -> str:
    if app_token is not None:
        token = app_token.strip()
        if token:
            return f"Bearer {token}"

    env_token = os.environ.get(_DIRECTORY_TOKEN_ENV, "").strip()
    if env_token:
        return f"Bearer {env_token}"

    raise IdentityError(
        "get_current_identity requires app_token or KEELSON_DIRECTORY_TOKEN."
    )


def get_current_identity(
    *,
    headers: Any = None,
    base_url: str | None = None,
    app_token: str | None = None,
    host: str | None = None,
    timeout_sec: float = 5.0,
) -> CurrentIdentity:
    from .local import is_local_mode, local_get_current_identity

    if is_local_mode():
        return local_get_current_identity()

    user = _parse_current_user_headers(headers)
    authorization = _resolve_current_identity_authorization(app_token=app_token)
    resolved_base = _resolve_base(base_url)
    url = f"{resolved_base}/__keelson/users/{quote(user.id, safe='')}/identity"
    raw = _do_get(
        url,
        _build_headers(authorization=authorization, host=host),
        timeout_sec,
    )
    payload = _decode_json(raw)
    return _parse_identity(payload)


def _build_headers(
    *,
    cookie: str | None = None,
    authorization: str | None = None,
    host: str | None = None,
) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie.strip()
    if authorization:
        headers["Authorization"] = authorization.strip()
    if host:
        headers["Host"] = host.strip()
    return headers


def _resolve_base(base_url: str | None) -> str:
    resolved = (base_url or _directory_base_url()).strip().rstrip("/")
    if not resolved:
        raise IdentityError(
            "base_url is required. Pass base_url or set "
            "KEELSON_DIRECTORY_BASE_URL / KEELSON_IDENTITY_BASE_URL."
        )
    return resolved


def _resolve_authorization(
    *,
    authorization: str | None,
    app_token: str | None,
    cookie: str | None,
) -> str | None:
    """Resolve the Authorization header value for a Directory request.

    An ``app_token`` (or the ``KEELSON_DIRECTORY_TOKEN`` fallback when no
    explicit credential is given) is sent as ``Bearer <token>`` for
    app-as-actor access. An explicit ``authorization`` takes precedence.

    A provided ``cookie`` signals user-as-actor intent: the env-token fallback
    is skipped so a stray ``KEELSON_DIRECTORY_TOKEN`` cannot silently turn the
    call into an app-as-actor request (the Keelson auth gateway resolves a
    ``keelson_`` Bearer token as an app actor before the user fallback). Passing
    ``app_token`` together with ``authorization`` or ``cookie`` is rejected.
    """
    if authorization is not None and app_token is not None:
        raise IdentityError(
            "Pass either authorization or app_token, not both."
        )
    if cookie is not None and app_token is not None:
        raise IdentityError("Pass either cookie or app_token, not both.")
    if authorization is not None:
        return authorization
    if app_token is not None:
        token = app_token.strip()
        return f"Bearer {token}" if token else None
    if cookie is not None:
        return None
    env_token = os.environ.get(_DIRECTORY_TOKEN_ENV, "").strip()
    return f"Bearer {env_token}" if env_token else None


def _do_get(url: str, headers: dict[str, str], timeout_sec: float) -> str:
    req = Request(url, method="GET", headers=headers)
    try:
        with urlopen(req, timeout=max(0.1, float(timeout_sec))) as resp:  # noqa: S310
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise IdentityError("Identity API returned 401 (unauthenticated).") from exc
        if exc.code == 403:
            raise IdentityError("Identity API returned 403 (not authorized).") from exc
        if exc.code == 404:
            raise IdentityError("Identity API returned 404 (not found).") from exc
        raise IdentityError(f"GET {url} failed with {exc.code}: {detail}") from exc
    except URLError as exc:
        raise IdentityError(f"GET {url} failed: {exc}") from exc


def _parse_member_item(raw: Any) -> MemberItem:
    if not isinstance(raw, dict):
        raise IdentityError("Member item is not an object.")
    mid = str(raw.get("id") or "").strip()
    if not mid:
        raise IdentityError("Member item is missing 'id'.")
    email = raw.get("email")
    if email is None:
        raise IdentityError("Member item is missing 'email'.")
    name = raw.get("name")
    if name is None:
        raise IdentityError("Member item is missing 'name'.")
    role_raw = raw.get("role")
    role = str(role_raw) if role_raw is not None else None
    return MemberItem(id=mid, email=str(email), name=str(name), role=role)


def _parse_paginated_members(payload: dict[str, Any]) -> PaginatedMembers:
    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        raise IdentityError("Members response is missing 'items'.")
    items = [_parse_member_item(m) for m in items_raw]
    limit_raw = payload.get("limit")
    if limit_raw is None:
        raise IdentityError("Members response is missing 'limit'.")
    offset_raw = payload.get("offset")
    if offset_raw is None:
        raise IdentityError("Members response is missing 'offset'.")
    next_offset_raw = payload.get("next_offset")
    try:
        limit = int(limit_raw)
        offset = int(offset_raw)
        next_offset = int(next_offset_raw) if next_offset_raw is not None else None
    except (TypeError, ValueError) as exc:
        raise IdentityError("Members response has invalid pagination fields.") from exc
    return PaginatedMembers(items=items, limit=limit, offset=offset, next_offset=next_offset)


def _parse_group_item(raw: Any) -> GroupItem:
    if not isinstance(raw, dict):
        raise IdentityError("Group item is not an object.")
    group_id = str(raw.get("id") or "").strip()
    if not group_id:
        raise IdentityError("Group item is missing 'id'.")
    display_name = raw.get("display_name")
    if display_name is None:
        raise IdentityError("Group item is missing 'display_name'.")
    kind = raw.get("kind")
    if kind is None:
        raise IdentityError("Group item is missing 'kind'.")
    # ``key`` is the group's code-facing identifier and is present on every
    # group the server returns. The null-tolerance here is kept for backward
    # compatibility; the contract is a non-empty validated key or null, so an
    # empty/blank string is a malformed response.
    key_raw = raw.get("key")
    key: str | None = None
    if key_raw is not None:
        key = str(key_raw)
        if not key.strip():
            raise IdentityError(
                "Group item has an empty 'key' (use null for keyless groups)."
            )
    system_kind_raw = raw.get("system_kind")
    system_kind = str(system_kind_raw) if system_kind_raw is not None else None
    return GroupItem(
        id=group_id,
        key=key,
        display_name=str(display_name),
        kind=str(kind),
        system_kind=system_kind,
    )


def list_members(
    *,
    base_url: str | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    app_token: str | None = None,
    host: str | None = None,
    timeout_sec: float = 5.0,
    limit: int | None = None,
    offset: int | None = None,
    q: str | None = None,
    role: str | None = None,
    group_key: str | None = None,
    group_id: str | None = None,
) -> PaginatedMembers:
    """List tenant members.

    ``group_key`` and ``group_id`` both narrow results to a single group.
    Prefer ``group_key`` for code references: every group has a key, and keys
    are stable and immutable. ``group_id`` is the UUID for machine integration
    / internal use. Passing both raises :class:`IdentityError`.
    """
    from .local import is_local_mode, local_list_members

    if group_key is not None and group_id is not None:
        # The Directory API rejects naming the same group two ways; fail
        # fast client-side so local mode behaves identically.
        raise IdentityError("Specify only one of group_id or group_key.")

    if is_local_mode():
        return local_list_members(
            limit=limit,
            offset=offset,
            q=q,
            role=role,
            group_key=group_key,
            group_id=group_id,
        )

    resolved_auth = _resolve_authorization(
        authorization=authorization, app_token=app_token, cookie=cookie
    )
    resolved_base = _resolve_base(base_url)
    params: dict[str, str | int] = {}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    if q is not None:
        params["q"] = q
    if role is not None:
        params["role"] = role
    if group_key is not None:
        params["group_key"] = group_key
    if group_id is not None:
        params["group_id"] = group_id
    url = f"{resolved_base}/__keelson/members"
    if params:
        url = f"{url}?{urlencode(params)}"
    headers = _build_headers(cookie=cookie, authorization=resolved_auth, host=host)
    raw = _do_get(url, headers, timeout_sec)
    payload = _decode_json(raw)
    return _parse_paginated_members(payload)


def get_user(
    user_id: str,
    *,
    base_url: str | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    app_token: str | None = None,
    host: str | None = None,
    timeout_sec: float = 5.0,
) -> MemberItem:
    from .local import is_local_mode, local_get_user

    if is_local_mode():
        return local_get_user(user_id)

    resolved_auth = _resolve_authorization(
        authorization=authorization, app_token=app_token, cookie=cookie
    )
    resolved_base = _resolve_base(base_url)
    uid = str(user_id).strip()
    if not uid:
        raise IdentityError("user_id is required.")
    url = f"{resolved_base}/__keelson/users/{quote(uid, safe='')}"
    headers = _build_headers(cookie=cookie, authorization=resolved_auth, host=host)
    raw = _do_get(url, headers, timeout_sec)
    payload = _decode_json(raw)
    return _parse_member_item(payload)


def list_groups(
    *,
    base_url: str | None = None,
    cookie: str | None = None,
    authorization: str | None = None,
    app_token: str | None = None,
    host: str | None = None,
    timeout_sec: float = 5.0,
) -> list[GroupItem]:
    from .local import is_local_mode, local_list_groups

    if is_local_mode():
        return local_list_groups()

    resolved_auth = _resolve_authorization(
        authorization=authorization, app_token=app_token, cookie=cookie
    )
    resolved_base = _resolve_base(base_url)
    url = f"{resolved_base}/__keelson/groups"
    headers = _build_headers(cookie=cookie, authorization=resolved_auth, host=host)
    raw = _do_get(url, headers, timeout_sec)
    payload = _decode_json(raw)
    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        raise IdentityError("Groups response is missing 'items'.")
    return [_parse_group_item(g) for g in items_raw]
