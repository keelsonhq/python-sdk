"""Tests for keelson_identity local development mock provider."""

from __future__ import annotations

import pytest

from keelson_identity.client import (
    CurrentIdentity,
    GroupItem,
    IdentityError,
    MemberItem,
    PaginatedMembers,
    UserIdentity,
    get_current_identity,
    get_current_user,
    get_user,
    list_groups,
    list_members,
)
from keelson_identity.local import (
    is_local_mode,
    local_get_current_identity,
    local_get_current_user,
    local_get_user,
    local_list_groups,
    local_list_members,
)


# ---------------------------------------------------------------------------
# is_local_mode
# ---------------------------------------------------------------------------


class TestIsLocalMode:
    def test_enabled_with_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        assert is_local_mode() is True

    def test_enabled_with_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "true")
        assert is_local_mode() is True

    def test_enabled_with_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "yes")
        assert is_local_mode() is True

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KEELSON_LOCAL_MODE", raising=False)
        assert is_local_mode() is False

    def test_disabled_with_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "0")
        assert is_local_mode() is False


# ---------------------------------------------------------------------------
# local_get_current_user
# ---------------------------------------------------------------------------


class TestLocalGetCurrentUser:
    def test_returns_user(self) -> None:
        user = local_get_current_user()
        assert isinstance(user, UserIdentity)
        assert user.id == "local-user-001"
        assert user.email == "dev@localhost"
        assert user.name == "Local Developer"

    def test_custom_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_USER_ID", "custom-u")
        monkeypatch.setenv("KEELSON_LOCAL_USER_EMAIL", "custom@test")
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ROLE", "BUILDER")
        user = local_get_current_user()
        assert user.id == "custom-u"
        assert user.email == "custom@test"


# ---------------------------------------------------------------------------
# local_get_current_identity
# ---------------------------------------------------------------------------


class TestLocalGetCurrentIdentity:
    def test_returns_identity(self) -> None:
        identity = local_get_current_identity()
        assert isinstance(identity, CurrentIdentity)
        assert identity.user.id == "local-user-001"
        assert identity.user.email == "dev@localhost"
        assert identity.user.name == "Local Developer"
        assert identity.tenant.id == "local-tenant-001"
        assert identity.tenant.role == "OWNER"
        assert identity.workspace is identity.tenant

    def test_workspace_env_takes_precedence_over_tenant_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_WORKSPACE_ID", "workspace-id")
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ID", "tenant-id")
        monkeypatch.setenv("KEELSON_LOCAL_WORKSPACE_ROLE", "OWNER")
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ROLE", "BUILDER")

        identity = local_get_current_identity()

        assert identity.workspace.id == "workspace-id"
        assert identity.workspace.role == "OWNER"
        assert identity.workspace is identity.tenant
        assert identity.app.id == "local-app-001"
        assert identity.app.permissions == ["manage", "view"]
        assert identity.attributes is not None
        assert "everyone" in identity.attributes.groups

    def test_custom_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_USER_ID", "custom-u")
        monkeypatch.setenv("KEELSON_LOCAL_USER_EMAIL", "custom@test")
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ROLE", "BUILDER")
        identity = local_get_current_identity()
        assert identity.user.id == "custom-u"
        assert identity.user.email == "custom@test"
        assert identity.tenant.role == "BUILDER"
        assert identity.workspace is identity.tenant

    def test_groups_reflect_role_owner(self) -> None:
        """OWNER gets all four system groups."""
        identity = local_get_current_identity()
        assert identity.attributes is not None
        assert set(identity.attributes.groups) == {
            "everyone", "developers", "admins", "owners",
        }

    def test_groups_reflect_role_builder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BUILDER gets only everyone + developers."""
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ROLE", "BUILDER")
        identity = local_get_current_identity()
        assert identity.attributes is not None
        assert set(identity.attributes.groups) == {"everyone", "developers"}

    def test_groups_reflect_role_app_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """APP_USER gets only everyone."""
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ROLE", "APP_USER")
        identity = local_get_current_identity()
        assert identity.attributes is not None
        assert identity.attributes.groups == ["everyone"]


# ---------------------------------------------------------------------------
# local_list_members
# ---------------------------------------------------------------------------


class TestLocalListMembers:
    def test_returns_all_fixture_members(self) -> None:
        result = local_list_members()
        assert isinstance(result, PaginatedMembers)
        assert len(result.items) == 4
        assert result.items[0].id == "local-user-001"
        assert result.items[0].role == "OWNER"

    def test_filter_by_role(self) -> None:
        result = local_list_members(role="ADMIN")
        assert len(result.items) == 1
        assert result.items[0].email == "alice@localhost"

    def test_search_by_name(self) -> None:
        result = local_list_members(q="bob")
        assert len(result.items) == 1
        assert result.items[0].name == "Bob (local)"

    def test_pagination(self) -> None:
        result = local_list_members(limit=2, offset=0)
        assert len(result.items) == 2
        assert result.next_offset == 2

        result2 = local_list_members(limit=2, offset=2)
        assert len(result2.items) == 2
        assert result2.next_offset is None

    def test_group_key_filter_system_everyone(self) -> None:
        """group_key=everyone returns all members (all roles qualify)."""
        result = local_list_members(group_key="everyone")
        assert len(result.items) == 4

    def test_group_key_filter_system_developers(self) -> None:
        """group_key=developers returns OWNER + ADMIN + BUILDER."""
        result = local_list_members(group_key="developers")
        roles = {m.role for m in result.items}
        assert "APP_USER" not in roles
        assert len(result.items) == 3

    def test_group_key_filter_system_owners(self) -> None:
        """group_key=owners returns only OWNER."""
        result = local_list_members(group_key="owners")
        assert len(result.items) == 1
        assert result.items[0].role == "OWNER"

    def test_group_key_filter_unknown(self) -> None:
        """Unknown group_key returns empty list."""
        result = local_list_members(group_key="nonexistent")
        assert result.items == []

    def test_group_id_filter_resolves_to_key(self) -> None:
        """group_id resolves to the same fixture group as its key."""
        result = local_list_members(group_id="local-group-owners")
        assert len(result.items) == 1
        assert result.items[0].role == "OWNER"

    def test_group_id_filter_unknown(self) -> None:
        """Unknown group_id matches no group → empty list."""
        result = local_list_members(group_id="no-such-id")
        assert result.items == []


# ---------------------------------------------------------------------------
# Env override consistency: current user always in directory
# ---------------------------------------------------------------------------


class TestEnvOverrideConsistency:
    """When KEELSON_LOCAL_USER_* env vars change the current user,
    the directory must still include that user."""

    def test_custom_user_in_list_members(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_USER_ID", "custom-u")
        monkeypatch.setenv("KEELSON_LOCAL_USER_EMAIL", "custom@test")
        monkeypatch.setenv("KEELSON_LOCAL_USER_NAME", "Custom User")
        monkeypatch.setenv("KEELSON_LOCAL_TENANT_ROLE", "BUILDER")

        result = local_list_members()
        ids = [m.id for m in result.items]
        assert "custom-u" in ids

        custom = next(m for m in result.items if m.id == "custom-u")
        assert custom.email == "custom@test"
        assert custom.name == "Custom User"
        assert custom.role == "BUILDER"

    def test_custom_user_resolvable_by_get_user(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_USER_ID", "custom-u")
        monkeypatch.setenv("KEELSON_LOCAL_USER_EMAIL", "custom@test")

        member = local_get_user("custom-u")
        assert member.id == "custom-u"
        assert member.email == "custom@test"

    def test_companions_still_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Changing the env user doesn't remove the companion members."""
        monkeypatch.setenv("KEELSON_LOCAL_USER_ID", "custom-u")
        result = local_list_members()
        ids = {m.id for m in result.items}
        assert "local-user-002" in ids
        assert "local-user-003" in ids
        assert "local-user-004" in ids


# ---------------------------------------------------------------------------
# local_get_user
# ---------------------------------------------------------------------------


class TestLocalGetUser:
    def test_found(self) -> None:
        member = local_get_user("local-user-002")
        assert isinstance(member, MemberItem)
        assert member.email == "alice@localhost"
        assert member.role == "ADMIN"

    def test_not_found(self) -> None:
        with pytest.raises(IdentityError, match="404"):
            local_get_user("nonexistent")


# ---------------------------------------------------------------------------
# local_list_groups
# ---------------------------------------------------------------------------


class TestLocalListGroups:
    def test_returns_system_groups(self) -> None:
        groups = local_list_groups()
        assert len(groups) == 4
        keys = [g.key for g in groups]
        assert "everyone" in keys
        assert "developers" in keys
        assert all(isinstance(g, GroupItem) for g in groups)
        # Every fixture group carries a stable id.
        assert all(g.id for g in groups)


# ---------------------------------------------------------------------------
# Integration: SDK functions delegate to local mode
# ---------------------------------------------------------------------------


class TestLocalModeIntegration:
    """Verify that public SDK functions use local mode when enabled."""

    def test_get_current_user_uses_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        # Should NOT require base_url or network
        user = get_current_user()
        assert user.id == "local-user-001"

    def test_get_current_identity_uses_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        identity = get_current_identity()
        assert identity.user.id == "local-user-001"
        assert identity.attributes is not None

    def test_list_members_uses_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        result = list_members()
        assert len(result.items) == 4

    def test_get_user_uses_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        member = get_user("local-user-001")
        assert member.email == "dev@localhost"

    def test_list_groups_uses_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        groups = list_groups()
        assert len(groups) == 4

    def test_current_user_resolvable_via_get_user(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The user returned by get_current_user must be resolvable by get_user."""
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        user = get_current_user()
        member = get_user(user.id)
        assert member.id == user.id

    def test_current_user_in_list_members(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The user returned by get_current_user must appear in list_members."""
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        user = get_current_user()
        result = list_members()
        ids = [m.id for m in result.items]
        assert user.id in ids

    def test_custom_env_user_resolvable(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even with custom KEELSON_LOCAL_USER_ID, get_user must resolve it."""
        monkeypatch.setenv("KEELSON_LOCAL_MODE", "1")
        monkeypatch.setenv("KEELSON_LOCAL_USER_ID", "custom-u")
        user = get_current_user()
        assert user.id == "custom-u"
        member = get_user("custom-u")
        assert member.id == "custom-u"
