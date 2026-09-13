"""Cross-language parity tests for Identity / Directory.

These tests load the shared fixtures from sdk/fixtures/parity/ and
assert the same semantic field values that Go and Node parity tests assert.
"""

from __future__ import annotations

import json

from keelson_identity.client import (
    _parse_group_item,
    _parse_identity,
    _parse_paginated_members,
)
from sdk_test_fixtures import parity_fixtures_dir

FIXTURES = parity_fixtures_dir(__file__)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parity_current_user() -> None:
    """Parse shared fixture with same field values as Go and Node."""
    payload = _load("identity_current_user.json")
    identity = _parse_identity(payload)

    # --- Cross-language parity assertions ---
    assert identity.user.id == "usr_parity01"
    assert identity.user.email == "taro@example.com"
    assert identity.user.name == "Taro Yamada"

    assert identity.workspace.id == "workspace_001"
    assert identity.workspace.role == "admin"
    assert identity.workspace is identity.tenant

    assert identity.app.id == "app_xyz"
    assert identity.app.permissions == ["manage", "view"]
    assert identity.app.roles == ["editor"]

    assert identity.attributes is not None
    # attributes.groups includes the user's system groups plus custom groups
    # bound to this app; unbound custom groups are excluded. Values are sorted
    # by key.
    assert identity.attributes.groups == ["admins", "everyone", "sales"]


def test_parity_members() -> None:
    """Parse shared fixture with same field values as Go and Node."""
    payload = _load("identity_members.json")
    result = _parse_paginated_members(payload)

    # --- Cross-language parity assertions ---
    assert result.limit == 25
    assert result.offset == 0
    assert result.next_offset is None
    assert len(result.items) == 2

    assert result.items[0].id == "usr_m01"
    assert result.items[0].email == "alice@example.com"
    assert result.items[0].name == "Alice"
    assert result.items[0].role == "admin"

    assert result.items[1].id == "usr_m02"
    assert result.items[1].email == "bob@example.com"
    # Python: role "" in JSON → "" (empty string preserved)
    assert result.items[1].role == ""


def test_parity_groups() -> None:
    """Parse shared fixture with same field values as Go and Node."""
    payload = _load("identity_groups.json")
    items = payload["items"]
    groups = [_parse_group_item(g) for g in items]

    # --- Cross-language parity assertions ---
    assert len(groups) == 3

    assert groups[0].id == "grp_admins01"
    assert groups[0].key == "admins"
    assert groups[0].display_name == "Administrators"
    assert groups[0].kind == "system"
    assert groups[0].system_kind == "admin"

    assert groups[1].key == "editors"
    assert groups[1].display_name == "Editors"
    assert groups[1].kind == "custom"
    assert groups[1].system_kind is None

    # A keyless group carries a stable id but key is None.
    assert groups[2].id == "grp_keyless01"
    assert groups[2].key is None
