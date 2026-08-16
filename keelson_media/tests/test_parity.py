"""Cross-language parity tests for Media.

These tests load the shared fixtures from sdk/fixtures/parity/ and
assert the same semantic field values that Go and Node parity tests assert.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from keelson_media.client import MediaStat, stat
from sdk_test_fixtures import parity_fixtures_dir

FIXTURES = parity_fixtures_dir(__file__)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_parity_file_stat(monkeypatch) -> None:
    """Parse shared fixture with same field values as Go and Node."""
    fixture = _load("media_stat.json")

    monkeypatch.setenv("KEELSON_INTERNAL_MEDIA_BASE_URL", "http://mock-files")
    monkeypatch.setenv("KEELSON_APP_MEDIA_TOKEN", "test-token")

    class FakeResponse:
        status = 200
        headers = {
            "Content-Type": fixture["content_type"],
            "Content-Length": str(fixture["content_length"]),
        }

        def read(self) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    with patch("keelson_media.client.urlopen", return_value=FakeResponse()):
        info = stat("parity-test-file")

    # --- Cross-language parity assertions ---
    # All SDKs strip parameters such as "; charset=utf-8" from Content-Type.
    assert isinstance(info, MediaStat)
    assert info.content_type == "text/plain"
    assert info.content_length == 204800
