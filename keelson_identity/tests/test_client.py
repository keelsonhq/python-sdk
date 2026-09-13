"""Tests for keelson_identity SDK client."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from keelson_identity.client import (
    AppIdentity,
    AttributesIdentity,
    CurrentIdentity,
    GroupItem,
    IdentityError,
    MemberItem,
    PaginatedMembers,
    TenantIdentity,
    UserIdentity,
    _directory_base_url,
    _parse_identity,
    _parse_current_user_headers,
    _parse_member_item,
    _parse_paginated_members,
    _parse_group_item,
    _resolve_authorization,
    _resolve_current_identity_authorization,
    get_current_identity,
    get_current_user,
    get_user,
    list_groups,
    list_members,
)


# ---------------------------------------------------------------------------
# Current identity parsing
# ---------------------------------------------------------------------------


def test_parse_identity_with_roles_and_groups() -> None:
    """Parse a payload with permissions, roles, and ``attributes.groups``."""
    payload = {
        "user": {"id": "u-1", "email": "u@example.com", "name": "User"},
        "tenant": {"id": "t-1", "role": "BUILDER"},
        "app": {
            "id": "a-1",
            "permissions": ["manage", "view"],
            "roles": ["reviewer", "editor"],
        },
        "attributes": {
            "groups": ["developers", "everyone", "reviewers"],
        },
        "authz": {"version": 5},
    }
    identity = _parse_identity(payload)

    assert identity.app.permissions == ["manage", "view"]
    assert identity.app.roles == ["reviewer", "editor"]
    assert identity.attributes is not None
    assert identity.attributes.groups == ["developers", "everyone", "reviewers"]


def test_parse_identity_prefers_workspace_and_keeps_tenant_alias_in_sync() -> None:
    payload = {
        "user": {"id": "u-1"},
        "workspace": {"id": "w-canonical", "role": "OWNER"},
        "tenant": {"id": "w-legacy", "role": "APP_USER"},
        "app": {"id": "a-1"},
    }

    identity = _parse_identity(payload)

    assert identity.workspace.id == "w-canonical"
    assert identity.workspace is identity.tenant


def test_parse_identity_without_roles_and_groups() -> None:
    """Parse a legacy-compatible payload without roles or attributes."""
    payload = {
        "user": {"id": "u-1", "email": "u@example.com", "name": "User"},
        "tenant": {"id": "t-1", "role": "APP_USER"},
        "app": {"id": "a-1"},
        "authz": {"version": 1},
    }
    identity = _parse_identity(payload)

    assert identity.app.permissions is None
    assert identity.app.roles is None
    assert identity.attributes is None


def test_parse_identity_empty_roles() -> None:
    """Parse a payload with an empty roles list."""
    payload = {
        "user": {"id": "u-1", "email": "u@example.com", "name": "User"},
        "tenant": {"id": "t-1", "role": "APP_USER"},
        "app": {
            "id": "a-1",
            "permissions": ["view"],
            "roles": [],
        },
        "authz": {"version": 2},
    }
    identity = _parse_identity(payload)

    assert identity.app.permissions == ["view"]
    assert identity.app.roles == []
    assert identity.attributes is None


def test_parse_identity_attributes_without_groups() -> None:
    """Attributes present but without groups key."""
    payload = {
        "user": {"id": "u-1", "email": "u@example.com", "name": "User"},
        "tenant": {"id": "t-1", "role": "OWNER"},
        "app": {"id": "a-1", "permissions": ["manage", "view"], "roles": []},
        "attributes": {},
        "authz": {"version": 1},
    }
    identity = _parse_identity(payload)

    assert identity.attributes is not None
    assert identity.attributes.groups is None


# ---------------------------------------------------------------------------
# Current-user headers and full identity
# ---------------------------------------------------------------------------


def test_parse_current_user_headers_plain_mapping() -> None:
    user = _parse_current_user_headers(
        {
            "x-keelson-user-id": "u-1",
            "x-keelson-user-email": "u@example.com",
            "x-keelson-user-name": "User",
        }
    )
    assert user == UserIdentity(id="u-1", email="u@example.com", name="User")


def test_parse_current_user_headers_case_insensitive_and_list_value() -> None:
    user = _parse_current_user_headers(
        {
            "X-Keelson-User-Id": ["", "u-2"],
            "X-Keelson-User-Email": ["alice@example.com"],
        }
    )
    assert user == UserIdentity(id="u-2", email="alice@example.com", name=None)


def test_parse_current_user_headers_missing_id() -> None:
    with pytest.raises(IdentityError, match="x-keelson-user-id"):
        _parse_current_user_headers({})


def test_get_current_user_does_not_call_network() -> None:
    with patch("keelson_identity.client.urlopen") as mocked:
        user = get_current_user(headers={"x-keelson-user-id": "u-1"})
    assert user == UserIdentity(id="u-1", email=None, name=None)
    mocked.assert_not_called()


def test_resolve_current_identity_authorization_app_token() -> None:
    assert (
        _resolve_current_identity_authorization(app_token="keelson_abc")
        == "Bearer keelson_abc"
    )


def test_resolve_current_identity_authorization_uses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
    assert (
        _resolve_current_identity_authorization(app_token=None)
        == "Bearer keelson_env"
    )


def test_resolve_current_identity_authorization_requires_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEELSON_DIRECTORY_TOKEN", raising=False)
    with pytest.raises(IdentityError, match="requires app_token"):
        _resolve_current_identity_authorization(app_token=None)


# ---------------------------------------------------------------------------
# Member-list parsing
# ---------------------------------------------------------------------------


def test_parse_member_item() -> None:
    raw = {"id": "abc-123", "email": "alice@example.com", "name": "Alice"}
    member = _parse_member_item(raw)
    assert member == MemberItem(id="abc-123", email="alice@example.com", name="Alice", role=None)


def test_parse_member_item_with_role() -> None:
    raw = {"id": "abc-123", "email": "alice@example.com", "name": "Alice", "role": "ADMIN"}
    member = _parse_member_item(raw)
    assert member == MemberItem(id="abc-123", email="alice@example.com", name="Alice", role="ADMIN")


def test_parse_member_item_missing_id() -> None:
    with pytest.raises(IdentityError, match="missing 'id'"):
        _parse_member_item({"email": "a@b.com", "name": "A"})


def test_parse_member_item_missing_email() -> None:
    with pytest.raises(IdentityError, match="missing 'email'"):
        _parse_member_item({"id": "u-1", "name": "A"})


def test_parse_member_item_missing_name() -> None:
    with pytest.raises(IdentityError, match="missing 'name'"):
        _parse_member_item({"id": "u-1", "email": "a@b.com"})


def test_parse_member_item_not_object() -> None:
    with pytest.raises(IdentityError, match="not an object"):
        _parse_member_item("bad")


def test_parse_paginated_members() -> None:
    payload = {
        "items": [
            {"id": "u-1", "email": "a@b.com", "name": "A"},
            {"id": "u-2", "email": "c@d.com", "name": "C"},
        ],
        "limit": 25,
        "offset": 0,
        "next_offset": 25,
    }
    result = _parse_paginated_members(payload)

    assert isinstance(result, PaginatedMembers)
    assert len(result.items) == 2
    assert result.items[0].id == "u-1"
    assert result.items[1].email == "c@d.com"
    assert result.limit == 25
    assert result.offset == 0
    assert result.next_offset == 25


def test_parse_paginated_members_last_page() -> None:
    payload = {
        "items": [{"id": "u-1", "email": "a@b.com", "name": "A"}],
        "limit": 25,
        "offset": 0,
        "next_offset": None,
    }
    result = _parse_paginated_members(payload)
    assert result.next_offset is None


def test_parse_paginated_members_empty() -> None:
    payload = {"items": [], "limit": 25, "offset": 0, "next_offset": None}
    result = _parse_paginated_members(payload)
    assert result.items == []


def test_parse_paginated_members_missing_items() -> None:
    with pytest.raises(IdentityError, match="missing 'items'"):
        _parse_paginated_members({"limit": 25})


def test_parse_paginated_members_missing_limit() -> None:
    with pytest.raises(IdentityError, match="missing 'limit'"):
        _parse_paginated_members({"items": [], "offset": 0})


def test_parse_paginated_members_missing_offset() -> None:
    with pytest.raises(IdentityError, match="missing 'offset'"):
        _parse_paginated_members({"items": [], "limit": 25})


def test_parse_paginated_members_invalid_limit() -> None:
    with pytest.raises(IdentityError, match="invalid pagination"):
        _parse_paginated_members({"items": [], "limit": "abc", "offset": 0})


# ---------------------------------------------------------------------------
# User-lookup parsing
# ---------------------------------------------------------------------------


def test_parse_member_item_as_user_lookup() -> None:
    """get_user returns a MemberItem parsed from a flat user object."""
    raw = {"id": "u-42", "email": "bob@example.com", "name": "Bob"}
    member = _parse_member_item(raw)
    assert member.id == "u-42"
    assert member.email == "bob@example.com"
    assert member.name == "Bob"


# ---------------------------------------------------------------------------
# Group-list parsing
# ---------------------------------------------------------------------------


def test_parse_group_item() -> None:
    raw = {
        "id": "grp-dev",
        "key": "developers",
        "display_name": "Developers",
        "kind": "SYSTEM",
        "system_kind": "developers",
    }
    group = _parse_group_item(raw)
    assert group == GroupItem(
        id="grp-dev",
        key="developers",
        display_name="Developers",
        kind="SYSTEM",
        system_kind="developers",
    )


def test_parse_group_item_custom() -> None:
    raw = {
        "id": "grp-rev",
        "key": "reviewers",
        "display_name": "Reviewers",
        "kind": "CUSTOM",
        "system_kind": None,
    }
    group = _parse_group_item(raw)
    assert group.kind == "CUSTOM"
    assert group.system_kind is None


def test_parse_group_item_keyless() -> None:
    """A keyless group sends "key": null; key parses to None, id stays set."""
    raw = {
        "id": "grp-keyless",
        "key": None,
        "display_name": "Keyless Team",
        "kind": "CUSTOM",
        "system_kind": None,
    }
    group = _parse_group_item(raw)
    assert group.id == "grp-keyless"
    assert group.key is None


def test_parse_group_item_empty_key_rejected() -> None:
    """An empty "key" string is malformed; the contract is null or a real alias."""
    with pytest.raises(IdentityError, match="empty 'key'"):
        _parse_group_item(
            {"id": "g1", "key": "", "display_name": "X", "kind": "CUSTOM"}
        )


def test_parse_group_item_missing_id() -> None:
    with pytest.raises(IdentityError, match="missing 'id'"):
        _parse_group_item({"key": "devs", "display_name": "X", "kind": "CUSTOM"})


def test_parse_group_item_missing_display_name() -> None:
    with pytest.raises(IdentityError, match="missing 'display_name'"):
        _parse_group_item({"id": "g1", "key": "devs", "kind": "CUSTOM"})


def test_parse_group_item_missing_kind() -> None:
    with pytest.raises(IdentityError, match="missing 'kind'"):
        _parse_group_item({"id": "g1", "key": "devs", "display_name": "Devs"})


def test_parse_group_item_not_object() -> None:
    with pytest.raises(IdentityError, match="not an object"):
        _parse_group_item(42)


# ---------------------------------------------------------------------------
# HTTP error handling for list_members, get_user, and list_groups
# ---------------------------------------------------------------------------


def _make_http_error(code: int, body: str = "") -> HTTPError:
    fp = BytesIO(body.encode())
    return HTTPError(
        url="http://test/__keelson/members",
        code=code,
        msg="",
        hdrs={},  # type: ignore[arg-type]
        fp=fp,
    )


def _mock_urlopen_error(code: int):
    """Return a context manager patch that raises HTTPError with the given code."""
    def side_effect(*args, **kwargs):
        raise _make_http_error(code)
    return patch("keelson_identity.client.urlopen", side_effect=side_effect)


class TestListMembersErrors:
    def test_401(self) -> None:
        with _mock_urlopen_error(401):
            with pytest.raises(IdentityError, match="401"):
                list_members(base_url="http://test")

    def test_403(self) -> None:
        with _mock_urlopen_error(403):
            with pytest.raises(IdentityError, match="403"):
                list_members(base_url="http://test")


class TestGetUserErrors:
    def test_401(self) -> None:
        with _mock_urlopen_error(401):
            with pytest.raises(IdentityError, match="401"):
                get_user("u-1", base_url="http://test")

    def test_403(self) -> None:
        with _mock_urlopen_error(403):
            with pytest.raises(IdentityError, match="403"):
                get_user("u-1", base_url="http://test")

    def test_404(self) -> None:
        with _mock_urlopen_error(404):
            with pytest.raises(IdentityError, match="404"):
                get_user("u-missing", base_url="http://test")


class TestListGroupsErrors:
    def test_401(self) -> None:
        with _mock_urlopen_error(401):
            with pytest.raises(IdentityError, match="401"):
                list_groups(base_url="http://test")

    def test_403(self) -> None:
        with _mock_urlopen_error(403):
            with pytest.raises(IdentityError, match="403"):
                list_groups(base_url="http://test")


# ---------------------------------------------------------------------------
# Integration-style round trips through a mocked urlopen
# ---------------------------------------------------------------------------


def _mock_urlopen_json(payload: dict | list):
    """Patch urlopen to return a JSON response."""
    body = json.dumps(payload).encode()

    class FakeResponse:
        def read(self) -> bytes:
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    return patch("keelson_identity.client.urlopen", return_value=FakeResponse())


class TestListMembersRoundTrip:
    def test_basic(self) -> None:
        resp = {
            "items": [{"id": "u-1", "email": "a@b.com", "name": "A"}],
            "limit": 25,
            "offset": 0,
            "next_offset": None,
        }
        with _mock_urlopen_json(resp):
            result = list_members(base_url="http://test")
        assert len(result.items) == 1
        assert result.items[0].id == "u-1"
        assert result.next_offset is None


class TestGetUserRoundTrip:
    def test_basic(self) -> None:
        resp = {"id": "u-42", "email": "bob@example.com", "name": "Bob"}
        with _mock_urlopen_json(resp):
            result = get_user("u-42", base_url="http://test")
        assert result == MemberItem(id="u-42", email="bob@example.com", name="Bob")


class TestListGroupsRoundTrip:
    def test_basic(self) -> None:
        resp = {
            "items": [
                {"id": "g-everyone", "key": "everyone", "display_name": "Everyone", "kind": "SYSTEM", "system_kind": "everyone"},
                {"id": "g-my-team", "key": "my-team", "display_name": "My Team", "kind": "CUSTOM", "system_kind": None},
                {"id": "g-keyless", "key": None, "display_name": "Keyless Team", "kind": "CUSTOM", "system_kind": None},
            ]
        }
        with _mock_urlopen_json(resp):
            result = list_groups(base_url="http://test")
        assert len(result) == 3
        assert result[0].id == "g-everyone"
        assert result[0].key == "everyone"
        assert result[1].kind == "CUSTOM"
        # A keyless group carries an id but key is None.
        assert result[2].id == "g-keyless"
        assert result[2].key is None


# ---------------------------------------------------------------------------
# URL encoding
# ---------------------------------------------------------------------------


class TestListMembersUrlEncoding:
    def test_query_with_spaces(self) -> None:
        """q parameter with spaces must be URL-encoded, not break the request."""
        resp = {"items": [], "limit": 25, "offset": 0, "next_offset": None}
        captured_urls: list[str] = []

        def fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)

            class FakeResp:
                def read(self):
                    return json.dumps(resp).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass

            return FakeResp()

        with patch("keelson_identity.client.urlopen", side_effect=fake_urlopen):
            list_members(base_url="http://test", q="alice smith")

        assert len(captured_urls) == 1
        assert "q=alice+smith" in captured_urls[0] or "q=alice%20smith" in captured_urls[0]
        # Must NOT contain raw space
        assert " " not in captured_urls[0]


class TestGetUserUrlEncoding:
    def test_user_id_with_slash(self) -> None:
        """user_id with special chars must be percent-encoded in path."""
        resp = {"id": "a/b", "email": "x@y.com", "name": "X"}
        captured_urls: list[str] = []

        def fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)

            class FakeResp:
                def read(self):
                    return json.dumps(resp).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass

            return FakeResp()

        with patch("keelson_identity.client.urlopen", side_effect=fake_urlopen):
            get_user("a/b", base_url="http://test")

        assert len(captured_urls) == 1
        # The slash in user_id must be encoded
        assert "/__keelson/users/a%2Fb" in captured_urls[0]


# ---------------------------------------------------------------------------
# Group ID filtering
# ---------------------------------------------------------------------------


class TestListMembersGroupId:
    def test_group_id_forwarded_as_query_param(self) -> None:
        """group_id is sent as a query param on the members request."""
        resp = {"items": [], "limit": 25, "offset": 0, "next_offset": None}
        captured_urls: list[str] = []

        def fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)

            class FakeResp:
                def read(self):
                    return json.dumps(resp).encode()
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    pass

            return FakeResp()

        with patch("keelson_identity.client.urlopen", side_effect=fake_urlopen):
            list_members(base_url="http://test", group_id="grp_xyz")

        assert len(captured_urls) == 1
        assert "group_id=grp_xyz" in captured_urls[0]

    def test_group_id_and_group_key_together_raises(self) -> None:
        with pytest.raises(IdentityError, match="only one of group_id or group_key"):
            list_members(base_url="http://test", group_id="g1", group_key="owners")


# ---------------------------------------------------------------------------
# App-as-actor token support
# ---------------------------------------------------------------------------


def _capture_request(payload: dict | list):
    """Patch urlopen to capture the Request object and return JSON."""
    captured: list = []

    def fake_urlopen(req, **kwargs):
        captured.append(req)

        class FakeResp:
            def read(self) -> bytes:
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        return FakeResp()

    return captured, patch(
        "keelson_identity.client.urlopen", side_effect=fake_urlopen
    )


_EMPTY_MEMBERS = {"items": [], "limit": 25, "offset": 0, "next_offset": None}

_IDENTITY_RESPONSE = {
    "user": {"id": "u-1", "email": "u@example.com", "name": "User"},
    "tenant": {"id": "t-1", "role": "OWNER"},
    "app": {"id": "a-1", "permissions": ["manage"], "roles": []},
    "attributes": {"groups": ["everyone", "owners"]},
}


class TestGetCurrentIdentityRoundTrip:
    def test_calls_app_identity_endpoint_with_app_token(self) -> None:
        captured, mock = _capture_request(_IDENTITY_RESPONSE)
        with mock:
            result = get_current_identity(
                base_url="http://test",
                app_token="keelson_xyz",
                headers={"x-keelson-user-id": "u-1"},
                host="app.example.test",
            )

        assert captured[0].full_url == "http://test/__keelson/users/u-1/identity"
        assert captured[0].get_header("Authorization") == "Bearer keelson_xyz"
        assert captured[0].get_header("Cookie") is None
        assert captured[0].get_header("Host") == "app.example.test"
        assert result.user.id == "u-1"
        assert result.tenant.role == "OWNER"
        assert result.app.permissions == ["manage"]
        assert result.attributes is not None
        assert result.attributes.groups == ["everyone", "owners"]

    def test_uses_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
        captured, mock = _capture_request(_IDENTITY_RESPONSE)
        with mock:
            get_current_identity(
                base_url="http://test",
                headers={"x-keelson-user-id": "u-env"},
            )

        assert captured[0].get_header("Authorization") == "Bearer keelson_env"

    def test_ignores_cookie_and_authorization_in_subject_headers(self) -> None:
        captured, mock = _capture_request(_IDENTITY_RESPONSE)
        with mock:
            get_current_identity(
                base_url="http://test",
                app_token="keelson_xyz",
                headers={
                    "x-keelson-user-id": "u-1",
                    "cookie": "sid=browser",
                    "authorization": "Bearer ey.user.jwt",
                },
            )

        assert captured[0].get_header("Authorization") == "Bearer keelson_xyz"
        assert captured[0].get_header("Cookie") is None

    def test_encodes_user_id(self) -> None:
        captured, mock = _capture_request(_IDENTITY_RESPONSE)
        with mock:
            get_current_identity(
                base_url="http://test",
                app_token="keelson_xyz",
                headers={"x-keelson-user-id": "a/b"},
            )

        assert captured[0].full_url == "http://test/__keelson/users/a%2Fb/identity"


class TestResolveAuthorization:
    def test_app_token_becomes_bearer(self) -> None:
        assert (
            _resolve_authorization(
                authorization=None, app_token="keelson_abc", cookie=None
            )
            == "Bearer keelson_abc"
        )

    def test_explicit_authorization_passthrough(self) -> None:
        assert (
            _resolve_authorization(
                authorization="Bearer ey.jwt", app_token=None, cookie=None
            )
            == "Bearer ey.jwt"
        )

    def test_authorization_and_app_token_conflict(self) -> None:
        with pytest.raises(IdentityError, match="not both"):
            _resolve_authorization(
                authorization="Bearer ey.jwt",
                app_token="keelson_abc",
                cookie=None,
            )

    def test_cookie_and_app_token_conflict(self) -> None:
        with pytest.raises(IdentityError, match="not both"):
            _resolve_authorization(
                authorization=None, app_token="keelson_abc", cookie="sid=1"
            )

    def test_env_token_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
        assert (
            _resolve_authorization(
                authorization=None, app_token=None, cookie=None
            )
            == "Bearer keelson_env"
        )

    def test_cookie_skips_env_token_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user-as-actor call (cookie) must not pick up the env app token."""
        monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
        assert (
            _resolve_authorization(
                authorization=None, app_token=None, cookie="sid=1"
            )
            is None
        )

    def test_explicit_authorization_beats_env_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
        assert (
            _resolve_authorization(
                authorization="Bearer ey.jwt", app_token=None, cookie=None
            )
            == "Bearer ey.jwt"
        )

    def test_no_auth_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KEELSON_DIRECTORY_TOKEN", raising=False)
        assert (
            _resolve_authorization(
                authorization=None, app_token=None, cookie=None
            )
            is None
        )


class TestDirectoryBaseUrl:
    def test_directory_env_preferred(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEELSON_DIRECTORY_BASE_URL", "http://directory")
        monkeypatch.setenv("KEELSON_IDENTITY_BASE_URL", "http://identity")
        assert _directory_base_url() == "http://directory"

    def test_falls_back_to_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("KEELSON_DIRECTORY_BASE_URL", raising=False)
        monkeypatch.setenv("KEELSON_IDENTITY_BASE_URL", "http://identity")
        assert _directory_base_url() == "http://identity"


class TestUserAgentHeader:
    """T-0595: urllib's default UA (Python-urllib/3.x) is blocked by Cloudflare
    Browser Integrity Check (Error 1010) on the *.keelson.run zones, so every
    outbound request must carry an explicit SDK User-Agent."""

    def test_list_members_sends_sdk_user_agent(self) -> None:
        captured, mock = _capture_request(_EMPTY_MEMBERS)
        with mock:
            list_members(base_url="http://test", app_token="keelson_xyz")
        assert captured[0].get_header("User-agent") == "Keelson-Python-SDK/0.1.1"

    def test_cookie_auth_also_sends_sdk_user_agent(self) -> None:
        captured, mock = _capture_request(_EMPTY_MEMBERS)
        with mock:
            list_members(base_url="http://test", cookie="sid=1")
        assert captured[0].get_header("User-agent") == "Keelson-Python-SDK/0.1.1"


class TestAppTokenHeader:
    def test_list_members_sends_bearer(self) -> None:
        captured, mock = _capture_request(_EMPTY_MEMBERS)
        with mock:
            list_members(base_url="http://test", app_token="keelson_xyz")
        assert captured[0].get_header("Authorization") == "Bearer keelson_xyz"

    def test_list_members_env_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
        captured, mock = _capture_request(_EMPTY_MEMBERS)
        with mock:
            list_members(base_url="http://test")
        assert captured[0].get_header("Authorization") == "Bearer keelson_env"

    def test_list_members_cookie_does_not_pick_up_env_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cookie call must stay user-as-actor even if the env token is set."""
        monkeypatch.setenv("KEELSON_DIRECTORY_TOKEN", "keelson_env")
        captured, mock = _capture_request(_EMPTY_MEMBERS)
        with mock:
            list_members(base_url="http://test", cookie="sid=1")
        assert captured[0].get_header("Authorization") is None
        assert captured[0].get_header("Cookie") == "sid=1"

    def test_list_members_conflict_raises(self) -> None:
        with pytest.raises(IdentityError, match="not both"):
            list_members(
                base_url="http://test",
                authorization="Bearer ey",
                app_token="keelson_xyz",
            )

    def test_list_members_cookie_app_token_conflict_raises(self) -> None:
        with pytest.raises(IdentityError, match="not both"):
            list_members(
                base_url="http://test",
                cookie="sid=1",
                app_token="keelson_xyz",
            )

    def test_get_user_sends_bearer(self) -> None:
        captured, mock = _capture_request(
            {"id": "u-1", "email": "a@b.com", "name": "A"}
        )
        with mock:
            get_user("u-1", base_url="http://test", app_token="keelson_xyz")
        assert captured[0].get_header("Authorization") == "Bearer keelson_xyz"

    def test_list_groups_sends_bearer(self) -> None:
        captured, mock = _capture_request({"items": []})
        with mock:
            list_groups(base_url="http://test", app_token="keelson_xyz")
        assert captured[0].get_header("Authorization") == "Bearer keelson_xyz"
