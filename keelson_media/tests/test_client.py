"""Tests for keelson_media SDK client.

Follows the mock-first pattern established in keelson_identity/tests/.
Uses shared helpers from conftest.py.
"""

from __future__ import annotations

import pytest

from keelson_media.client import (
    MediaStat,
    MediaError,
    delete,
    exists,
    get,
    put,
    stat,
    url,
)


# ---------------------------------------------------------------------------
# Local mode: filesystem-backed storage
# ---------------------------------------------------------------------------


class TestLocalMode:
    """Media SDK falls back to local filesystem in local mode."""

    def test_put_and_get_local(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"Hello, world!")
        data = get(file_id)
        assert data == b"Hello, world!"

    def test_exists_local(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"data")
        assert exists(file_id) is True

    def test_delete_local(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"data")
        assert exists(file_id) is True
        delete(file_id)
        assert exists(file_id) is False

    def test_delete_local_removes_sidecar(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"data", content_type="text/plain")
        meta_path = tmp_path / f"{file_id}.meta.json"
        assert meta_path.exists()
        delete(file_id)
        assert not meta_path.exists()

    def test_stat_local_default_content_type(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        payload = b"Hello, world!"
        file_id = put(payload)
        info = stat(file_id)
        assert isinstance(info, MediaStat)
        assert info.content_length == len(payload)
        assert info.content_type == "application/octet-stream"
        assert info.status == 200

    def test_stat_local_preserves_explicit_content_type(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"plain text", content_type="text/plain")
        info = stat(file_id)
        assert info.content_type == "text/plain"
        assert info.content_length == 10

    def test_stat_local_guesses_content_type_from_filename(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"%PDF-fake", filename="report.pdf")
        info = stat(file_id)
        assert info.content_type == "application/pdf"

    def test_stat_local_fallback_without_sidecar(self, monkeypatch, tmp_path) -> None:
        """Media written before metadata sidecar was added still work."""
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        # Simulate a file written without sidecar (legacy path)
        (tmp_path / "legacy-file").write_bytes(b"old data")
        info = stat("legacy-file")
        assert info.content_type == "application/octet-stream"
        assert info.content_length == 8

    def test_stat_local_not_found(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_INTERNAL_MEDIA_BASE_URL", raising=False)
        monkeypatch.delenv("KEELSON_APP_MEDIA_TOKEN", raising=False)
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        with pytest.raises(MediaError, match="not found"):
            stat("nonexistent")


# ---------------------------------------------------------------------------
# url() — public URL construction
# ---------------------------------------------------------------------------


class TestUrl:
    def _local(self, monkeypatch) -> None:
        # url() now resolves the runtime mode; pin explicit local mode so the
        # test is independent of any ambient platform env.
        for key in (
            "KEELSON_APP_ID",
            "KEELSON_WORKSPACE_ID",
            "KEELSON_TENANT_ID",
            "KEELSON_DEPLOY_ID",
            "KEELSON_INTERNAL_MEDIA_BASE_URL",
            "KEELSON_APP_MEDIA_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("KEELSON_MODE", "local")

    def test_default_prefix(self, monkeypatch) -> None:
        self._local(monkeypatch)
        monkeypatch.delenv("KEELSON_MEDIA_URL_PREFIX", raising=False)
        result = url("abc123")
        assert result == "/media/abc123"

    def test_custom_prefix(self, monkeypatch) -> None:
        self._local(monkeypatch)
        monkeypatch.setenv("KEELSON_MEDIA_URL_PREFIX", "/static/")
        result = url("abc123")
        assert result == "/static/abc123"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_file_id_raises(self) -> None:
        with pytest.raises(ValueError, match="file_id"):
            get("")

    def test_url_file_id_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be a URL"):
            get("http://evil.com/file")

    def test_put_non_bytes_raises(self) -> None:
        with pytest.raises(TypeError, match="bytes"):
            put("not bytes")  # type: ignore[arg-type]

    def test_stat_empty_file_id_raises(self) -> None:
        with pytest.raises(ValueError, match="file_id"):
            stat("")


# ---------------------------------------------------------------------------
# Keelson mode: contract test (mocked HTTP)
# ---------------------------------------------------------------------------


class TestKeelsonModeStat:
    """stat() in keelson mode sends HEAD and parses response headers."""

    def _setup_keelson_env(self, monkeypatch) -> None:
        monkeypatch.setenv(
            "KEELSON_INTERNAL_MEDIA_BASE_URL", "http://media.example:8000"
        )
        monkeypatch.setenv("KEELSON_APP_MEDIA_TOKEN", "test-token")

    def test_stat_keelson_parses_headers(self, monkeypatch) -> None:
        self._setup_keelson_env(monkeypatch)

        class FakeResponse:
            status = 200
            headers = {
                "Content-Type": "image/png",
                "Content-Length": "4096",
            }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        captured_requests: list = []

        def fake_urlopen(req, **kwargs):
            captured_requests.append(req)
            return FakeResponse()

        monkeypatch.setattr("keelson_media.client.urlopen", fake_urlopen)

        info = stat("abc123")
        assert info == MediaStat(content_type="image/png", content_length=4096, status=200)
        assert info.status == 200
        assert len(captured_requests) == 1
        assert captured_requests[0].get_method() == "HEAD"
        assert "Bearer test-token" in captured_requests[0].get_header("Authorization")
        assert (
            captured_requests[0].get_header("User-agent")
            == "Keelson-Python-SDK/0.1.1"
        )

    def test_stat_keelson_strips_charset(self, monkeypatch) -> None:
        self._setup_keelson_env(monkeypatch)

        class FakeResponse:
            status = 200
            headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Length": "100",
            }

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        monkeypatch.setattr("keelson_media.client.urlopen", lambda req, **kw: FakeResponse())

        info = stat("abc123")
        assert info.content_type == "text/plain"
        assert info.status == 200

    def test_stat_keelson_404_raises(self, monkeypatch) -> None:
        self._setup_keelson_env(monkeypatch)

        from urllib.error import HTTPError

        def fake_urlopen(req, **kwargs):
            raise HTTPError(req.full_url, 404, "Not Found", {}, None)

        monkeypatch.setattr("keelson_media.client.urlopen", fake_urlopen)

        with pytest.raises(MediaError, match="not found"):
            stat("missing")


# ---------------------------------------------------------------------------
# Runtime-mode contract: fail closed on Keelson, no silent local fallback
# ---------------------------------------------------------------------------


# Fixed-contract error messages (must match keelson_media/client.py verbatim).
_MSG_PARTIAL_MISSING_BASE = (
    "Incomplete remote Media configuration: both "
    "KEELSON_INTERNAL_MEDIA_BASE_URL and KEELSON_APP_MEDIA_TOKEN are "
    "required, but KEELSON_INTERNAL_MEDIA_BASE_URL is missing."
)
_MSG_PARTIAL_MISSING_TOKEN = (
    "Incomplete remote Media configuration: both "
    "KEELSON_INTERNAL_MEDIA_BASE_URL and KEELSON_APP_MEDIA_TOKEN are "
    "required, but KEELSON_APP_MEDIA_TOKEN is missing."
)
_MSG_KEELSON_UNAVAILABLE = (
    "KEELSON_MODE=keelson but Media is not configured "
    "(KEELSON_INTERNAL_MEDIA_BASE_URL and KEELSON_APP_MEDIA_TOKEN are "
    "unset); the Media capability is unavailable for this deployment."
)
_MSG_REFUSE_FALLBACK = (
    "Platform environment detected "
    "(KEELSON_APP_ID / KEELSON_WORKSPACE_ID (or deprecated KEELSON_TENANT_ID "
    "alias) / KEELSON_DEPLOY_ID set) but Media "
    "is not configured; refusing to fall back to local storage. Set "
    "KEELSON_MODE=local for local development."
)


def _msg_unknown_mode(mode: str) -> str:
    return (
        f'Unrecognized KEELSON_MODE="{mode}"; expected "keelson" or "local" '
        "(or unset for local development)."
    )


class TestModeContract:
    """The Media SDK must never silently write to ephemeral local storage
    when the platform Media configuration is missing, incomplete, or the mode
    is unrecognized. Fixed-contract error messages are asserted verbatim."""

    def _clean(self, monkeypatch) -> None:
        for key in (
            "KEELSON_MODE",
            "KEELSON_APP_ID",
            "KEELSON_WORKSPACE_ID",
            "KEELSON_TENANT_ID",
            "KEELSON_DEPLOY_ID",
            "KEELSON_INTERNAL_MEDIA_BASE_URL",
            "KEELSON_APP_MEDIA_TOKEN",
        ):
            monkeypatch.delenv(key, raising=False)

    def test_resolve_remote_in_keelson_mode(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv(
            "KEELSON_INTERNAL_MEDIA_BASE_URL", "http://media.example:8000"
        )
        monkeypatch.setenv("KEELSON_APP_MEDIA_TOKEN", "tok")
        assert _resolve_mode() == "remote"

    def test_keelson_mode_missing_env_fails_closed(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        with pytest.raises(MediaError) as ei:
            _resolve_mode()
        assert str(ei.value) == _MSG_KEELSON_UNAVAILABLE

    def test_partial_config_only_base_raises(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv(
            "KEELSON_INTERNAL_MEDIA_BASE_URL", "http://media.example:8000"
        )
        with pytest.raises(MediaError) as ei:
            _resolve_mode()
        assert str(ei.value) == _MSG_PARTIAL_MISSING_TOKEN

    def test_partial_config_only_token_raises_even_in_local_mode(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "local")
        monkeypatch.setenv("KEELSON_APP_MEDIA_TOKEN", "tok")
        with pytest.raises(MediaError) as ei:
            _resolve_mode()
        assert str(ei.value) == _MSG_PARTIAL_MISSING_BASE

    def test_explicit_local_mode_with_platform_env(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "local")
        monkeypatch.setenv("KEELSON_APP_ID", "app_123")
        assert _resolve_mode() == "local"

    def test_zero_config_local_development(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        assert _resolve_mode() == "local"

    @pytest.mark.parametrize(
        "env_key",
        [
            "KEELSON_APP_ID",
            "KEELSON_WORKSPACE_ID",
            "KEELSON_TENANT_ID",
            "KEELSON_DEPLOY_ID",
        ],
    )
    def test_platform_env_visible_refuses_local_fallback(self, monkeypatch, env_key) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv(env_key, "id_123")
        with pytest.raises(MediaError) as ei:
            _resolve_mode()
        assert str(ei.value) == _MSG_REFUSE_FALLBACK

    def test_unknown_mode_raises(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "typo")
        with pytest.raises(MediaError) as ei:
            _resolve_mode()
        assert str(ei.value) == _msg_unknown_mode("typo")

    def test_unknown_mode_raises_even_with_valid_env(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "production")
        monkeypatch.setenv(
            "KEELSON_INTERNAL_MEDIA_BASE_URL", "http://media.example:8000"
        )
        monkeypatch.setenv("KEELSON_APP_MEDIA_TOKEN", "tok")
        with pytest.raises(MediaError) as ei:
            _resolve_mode()
        assert str(ei.value) == _msg_unknown_mode("production")

    def test_unset_mode_both_env_present_is_remote(self, monkeypatch) -> None:
        from keelson_media.client import _resolve_mode

        self._clean(monkeypatch)
        monkeypatch.setenv(
            "KEELSON_INTERNAL_MEDIA_BASE_URL", "http://media.example:8000"
        )
        monkeypatch.setenv("KEELSON_APP_MEDIA_TOKEN", "tok")
        assert _resolve_mode() == "remote"

    def test_keelson_mode_missing_env_creates_no_local_file(self, monkeypatch, tmp_path) -> None:
        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_APP_ID", "app_123")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        with pytest.raises(MediaError):
            put(b"data")
        assert list(tmp_path.iterdir()) == []

    def test_url_fails_closed_when_files_disabled(self, monkeypatch) -> None:
        """url() must fail closed on a capability-unavailable Keelson deployment,
        matching the Go client (whose constructor rejects)."""
        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_APP_ID", "app_123")
        with pytest.raises(MediaError) as ei:
            url("abc123")
        assert str(ei.value) == _MSG_KEELSON_UNAVAILABLE

    def test_explicit_local_mode_full_roundtrip(self, monkeypatch, tmp_path) -> None:
        """All Media APIs — put/get/exists/stat/delete/url — pass in local mode."""
        self._clean(monkeypatch)
        monkeypatch.setenv("KEELSON_MODE", "local")
        monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
        file_id = put(b"payload", content_type="text/plain")
        assert exists(file_id) is True
        assert get(file_id) == b"payload"
        info = stat(file_id)
        assert info.content_type == "text/plain"
        assert info.content_length == 7
        assert url(file_id).startswith("/media/")
        delete(file_id)
        assert exists(file_id) is False
