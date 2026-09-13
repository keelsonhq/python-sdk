"""Fail-closed ``KEELSON_MODE`` resolution contract."""

from __future__ import annotations

import pytest

from keelson_files.client import FilesError, _resolve_mode

_CLEAN = (
    "KEELSON_MODE",
    "KEELSON_APP_ID",
    "KEELSON_WORKSPACE_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
    "KEELSON_FILES_BUCKET",
    "KEELSON_FILES_PREFIX",
)

_MISSING_IDENTITY_MESSAGE = (
    "KEELSON_MODE=keelson but the platform identity is missing "
    "(KEELSON_APP_ID and KEELSON_WORKSPACE_ID must be set; "
    "KEELSON_TENANT_ID remains a deprecated alias); the Files capability is "
    "unavailable for this deployment."
)
_REFUSE_FALLBACK_MESSAGE = (
    "Platform environment detected (KEELSON_APP_ID / KEELSON_WORKSPACE_ID (or "
    "deprecated KEELSON_TENANT_ID alias) / KEELSON_DEPLOY_ID set) but "
    "KEELSON_MODE is unset; refusing to fall back to local storage. Set "
    "KEELSON_MODE=local for local development or KEELSON_MODE=keelson for "
    "platform storage."
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in _CLEAN:
        monkeypatch.delenv(var, raising=False)


def _set(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_keelson_mode_with_env_is_remote(monkeypatch) -> None:
    _set(
        monkeypatch,
        KEELSON_MODE="keelson",
        KEELSON_FILES_BUCKET="b",
        KEELSON_FILES_PREFIX="tenants/t/apps/a/files/",
        KEELSON_APP_ID="a",
        KEELSON_TENANT_ID="t",
    )
    assert _resolve_mode() == "remote"


def test_keelson_mode_with_workspace_env_is_remote(monkeypatch) -> None:
    _set(
        monkeypatch,
        KEELSON_MODE="keelson",
        KEELSON_FILES_BUCKET="b",
        KEELSON_FILES_PREFIX="workspaces/w/apps/a/files/",
        KEELSON_APP_ID="a",
        KEELSON_WORKSPACE_ID="w",
    )
    assert _resolve_mode() == "remote"


def test_keelson_mode_missing_env_fails_closed(monkeypatch) -> None:
    _set(monkeypatch, KEELSON_MODE="keelson")
    with pytest.raises(FilesError, match="capability is unavailable"):
        _resolve_mode()


def test_keelson_mode_missing_identity_fails_closed(monkeypatch) -> None:
    _set(
        monkeypatch,
        KEELSON_MODE="keelson",
        KEELSON_FILES_BUCKET="b",
        KEELSON_FILES_PREFIX="tenants/t/apps/a/files/",
    )
    with pytest.raises(FilesError) as exc_info:
        _resolve_mode()
    assert str(exc_info.value) == _MISSING_IDENTITY_MESSAGE


def test_partial_config_missing_prefix_raises(monkeypatch) -> None:
    _set(monkeypatch, KEELSON_FILES_BUCKET="b")
    with pytest.raises(FilesError, match="KEELSON_FILES_PREFIX is missing"):
        _resolve_mode()


def test_partial_config_missing_bucket_raises(monkeypatch) -> None:
    _set(monkeypatch, KEELSON_MODE="local", KEELSON_FILES_PREFIX="p/")
    with pytest.raises(FilesError, match="KEELSON_FILES_BUCKET is missing"):
        _resolve_mode()


def test_explicit_local_even_with_platform_env(monkeypatch) -> None:
    _set(monkeypatch, KEELSON_MODE="local", KEELSON_APP_ID="app_1")
    assert _resolve_mode() == "local"


def test_zero_config_local(monkeypatch) -> None:
    assert _resolve_mode() == "local"


@pytest.mark.parametrize(
    "var",
    [
        "KEELSON_APP_ID",
        "KEELSON_WORKSPACE_ID",
        "KEELSON_TENANT_ID",
        "KEELSON_DEPLOY_ID",
    ],
)
def test_unset_mode_refuses_fallback_on_platform(monkeypatch, var: str) -> None:
    _set(monkeypatch, **{var: "x"})
    with pytest.raises(FilesError) as exc_info:
        _resolve_mode()
    assert str(exc_info.value) == _REFUSE_FALLBACK_MESSAGE


def test_unset_mode_with_env_is_local_not_remote(monkeypatch) -> None:
    # KEELSON_MODE is the single mode signal: remote storage variables are not
    # consulted to infer remote when the mode is unset. With no platform env
    # visible, this is zero-config local development.
    _set(
        monkeypatch,
        KEELSON_FILES_BUCKET="b",
        KEELSON_FILES_PREFIX="tenants/t/apps/a/files/",
    )
    assert _resolve_mode() == "local"


def test_unknown_mode_raises(monkeypatch) -> None:
    _set(monkeypatch, KEELSON_MODE="production")
    with pytest.raises(FilesError, match="Unrecognized KEELSON_MODE"):
        _resolve_mode()
