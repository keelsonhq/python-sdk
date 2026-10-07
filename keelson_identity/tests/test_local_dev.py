"""Parity tests for the local-dev contract (docs/specs/local-dev-spec.md).

Load the shared fixtures ``identity_local_roster.json``,
``identity_request_user.json`` and ``identity_local_mode_guard.json`` and
check the Python SDK against them.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from keelson_identity import (
    IdentityError,
    get_current_identity,
    get_current_user,
    get_request_user,
    get_user,
    list_groups,
    list_members,
)
from sdk_test_fixtures import parity_fixtures_dir

FIXTURES = parity_fixtures_dir(__file__)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


ROSTER = _load("identity_local_roster.json")
REQUEST_USER = _load("identity_request_user.json")
GUARD = _load("identity_local_mode_guard.json")


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str]) -> None:
    """Clear every env var the fixtures use, run in an empty cwd, apply *env*."""
    names = set(ROSTER["env_vars"]) | set(REQUEST_USER["env_vars"]) | set(GUARD["env_vars"])
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _write_users_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, roster: Any) -> None:
    path = tmp_path / "users.json"
    path.write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KEELSON_LOCAL_USERS_FILE", str(path))


@pytest.fixture
def roster_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1"})
    _write_users_file(monkeypatch, tmp_path, ROSTER["roster"])


def _asdict(value: Any) -> Any:
    return dataclasses.asdict(value)


# ---------------------------------------------------------------------------
# identity_local_roster.json
# ---------------------------------------------------------------------------


def test_roster_current_user(roster_env: None) -> None:
    assert _asdict(get_current_user()) == ROSTER["current_user"]
    assert get_current_user().id == ROSTER["fixed_user_id"]


def test_roster_request_user(roster_env: None) -> None:
    assert _asdict(get_request_user(headers={"x-keelson-user-id": "ignored"})) == ROSTER["request_user"]


@pytest.mark.parametrize("user_id", list(ROSTER["identities"]))
def test_roster_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, user_id: str
) -> None:
    # The SDK returns the fixed user's identity; a one-user roster makes
    # each fixture user the fixed user.
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1"})
    user = next(u for u in ROSTER["roster"]["users"] if u["id"] == user_id)
    _write_users_file(monkeypatch, tmp_path, {"users": [user]})

    identity = _asdict(get_current_identity())
    expected = ROSTER["identities"][user_id]
    for field in ROSTER["identity_compared_fields"]:
        assert identity[field] == expected[field], field


@pytest.mark.parametrize("case", ROSTER["list_members"], ids=lambda c: c["name"])
def test_roster_list_members(roster_env: None, case: dict) -> None:
    if "error" in case:
        with pytest.raises(IdentityError):
            list_members(**case["query"])
        return
    assert _asdict(list_members(**case["query"])) == case["response"]


@pytest.mark.parametrize("case", ROSTER["get_user"], ids=lambda c: c["user_id"])
def test_roster_get_user(roster_env: None, case: dict) -> None:
    if "error" in case:
        with pytest.raises(IdentityError, match="404"):
            get_user(case["user_id"])
        return
    assert _asdict(get_user(case["user_id"])) == case["response"]


def test_roster_groups(roster_env: None) -> None:
    assert [_asdict(g) for g in list_groups()] == ROSTER["groups"]["items"]


@pytest.mark.parametrize("case", ROSTER["fixed_user_cases"], ids=lambda c: c["name"])
def test_roster_fixed_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: dict) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1"})
    _write_users_file(monkeypatch, tmp_path, case["roster"])
    assert get_current_user().id == case["expected_user_id"]
    assert get_request_user().id == case["expected_user_id"]
    assert get_current_identity().user.id == case["expected_user_id"]


@pytest.mark.parametrize("case", ROSTER["workspace_id_cases"], ids=lambda c: c["name"])
def test_roster_workspace_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: dict) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1", **case["env"]})
    _write_users_file(monkeypatch, tmp_path, ROSTER["roster"])
    identity = get_current_identity()
    assert identity.workspace.id == case["expected_workspace_id"]
    assert identity.tenant.id == case["expected_workspace_id"]


def test_roster_ignores_local_user_and_role_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(
        monkeypatch,
        tmp_path,
        {
            "KEELSON_LOCAL_MODE": "1",
            "KEELSON_LOCAL_USER_ID": "dev-9",
            "KEELSON_LOCAL_USER_EMAIL": "nine@localhost",
            "KEELSON_LOCAL_USER_NAME": "Nine",
            "KEELSON_LOCAL_WORKSPACE_ROLE": "OWNER",
            "KEELSON_LOCAL_APP_ID": "my-app",
        },
    )
    _write_users_file(monkeypatch, tmp_path, ROSTER["roster"])
    identity = get_current_identity()
    assert _asdict(identity.user) == ROSTER["current_user"]
    assert identity.workspace.role == "ADMIN"
    assert identity.app.id == "my-app"
    assert [m.id for m in list_members().items] == [u["id"] for u in ROSTER["roster"]["users"]]


def test_default_users_file_in_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1"})
    (tmp_path / ".keelson").mkdir()
    (tmp_path / ".keelson" / "dev-users.json").write_text(
        json.dumps(ROSTER["roster"], ensure_ascii=False), encoding="utf-8"
    )
    assert get_current_user().id == ROSTER["fixed_user_id"]


def test_without_users_file_fixture_data_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1"})
    assert get_current_user().id == "local-user-001"
    assert get_current_identity().workspace.role == "OWNER"
    assert [m.id for m in list_members().items] == [
        "local-user-001",
        "local-user-002",
        "local-user-003",
        "local-user-004",
    ]
    assert [g.key for g in list_groups()] == ["everyone", "developers", "admins", "owners"]


def _invalid_roster_ids(case: dict) -> str:
    return case["reason"]


@pytest.mark.parametrize("case", ROSTER["invalid_rosters"], ids=_invalid_roster_ids)
def test_invalid_roster_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: dict) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1"})
    path = tmp_path / "users.json"
    text = case["text"] if "text" in case else json.dumps(case["roster"])
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("KEELSON_LOCAL_USERS_FILE", str(path))
    for call in (get_current_user, get_request_user, get_current_identity, list_members, list_groups):
        with pytest.raises(IdentityError, match="local users file"):
            call()
    with pytest.raises(IdentityError, match="local users file"):
        get_user("x")


def test_missing_explicit_users_file_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(
        monkeypatch,
        tmp_path,
        {"KEELSON_LOCAL_MODE": "1", "KEELSON_LOCAL_USERS_FILE": str(tmp_path / "missing.json")},
    )
    with pytest.raises(IdentityError, match="local users file"):
        get_current_user()


# ---------------------------------------------------------------------------
# identity_request_user.json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", REQUEST_USER["header_cases"], ids=lambda c: c["name"])
def test_request_user_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: dict) -> None:
    _isolate(monkeypatch, tmp_path, {})
    if "error" in case:
        assert case["error"] == "missing_user_id"
        with pytest.raises(IdentityError, match="Current user headers are missing 'x-keelson-user-id'."):
            get_request_user(headers=case["headers"])
        return
    assert _asdict(get_request_user(headers=case["headers"])) == case["expected"]


@pytest.mark.parametrize("case", REQUEST_USER["local_cases"], ids=lambda c: c["name"])
def test_request_user_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: dict) -> None:
    _isolate(monkeypatch, tmp_path, case["env"])
    if "users_file" in case:
        _write_users_file(monkeypatch, tmp_path, case["users_file"])
    assert _asdict(get_request_user(headers=case["headers"])) == case["expected"]


def test_request_user_recovers_name_ending_in_nbsp_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The last UTF-8 byte of the trailing character is 0xA0 (latin-1 NBSP);
    # recovery must happen before trimming.
    _isolate(monkeypatch, tmp_path, {})
    name = "佐藤 悠"
    assert name.encode("utf-8")[-1] == 0xA0
    headers = {"x-keelson-user-id": "u-14", "x-keelson-user-name": name.encode("utf-8").decode("latin-1")}
    assert get_request_user(headers=headers).name == name


def test_current_user_does_not_recover_latin1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path, {})
    case = next(c for c in REQUEST_USER["header_cases"] if c["name"] == "non_ascii_name_as_latin1_string")
    assert get_current_user(headers=case["headers"]).name == case["headers"]["x-keelson-user-name"]


# ---------------------------------------------------------------------------
# identity_local_mode_guard.json
# ---------------------------------------------------------------------------

_LOCAL_CALLS = {
    "get_current_user": lambda: get_current_user(),
    "get_current_identity": lambda: get_current_identity(),
    "get_request_user": lambda: get_request_user(),
    "list_members": lambda: list_members(),
    "get_user": lambda: get_user("local-user-001"),
    "list_groups": lambda: list_groups(),
}


@pytest.mark.parametrize("case", GUARD["cases"], ids=lambda c: c["name"])
def test_local_mode_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: dict) -> None:
    _isolate(monkeypatch, tmp_path, case["env"])
    expected = case["expected"]["sdk"]
    if expected == "local":
        for call in _LOCAL_CALLS.values():
            call()
        assert get_current_user().id == "local-user-001"
    elif expected == "error":
        marks = case["expected"]["marks"]
        for name, call in _LOCAL_CALLS.items():
            with pytest.raises(IdentityError) as excinfo:
                call()
            message = str(excinfo.value)
            for needle in GUARD["message_must_contain"]:
                assert needle in message, name
            assert f"({', '.join(marks)})" in message, name
            assert "unset KEELSON_LOCAL_MODE" in message, name
            for var, value in case["env"].items():
                if var != "KEELSON_LOCAL_MODE" and value.strip().lower() != "keelson":
                    assert value not in message, name
    else:
        assert expected == "not_local"
        # The production path reads headers instead of returning fixed data.
        user = get_current_user(headers={"x-keelson-user-id": "u-prod"})
        assert user.id == "u-prod"
        assert get_request_user(headers={"x-keelson-user-id": "u-prod"}).id == "u-prod"


def test_guard_also_applies_with_users_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate(monkeypatch, tmp_path, {"KEELSON_LOCAL_MODE": "1", "KEELSON_APP_ID": "app-1"})
    _write_users_file(monkeypatch, tmp_path, ROSTER["roster"])
    with pytest.raises(IdentityError, match="KEELSON_APP_ID"):
        get_current_user()
