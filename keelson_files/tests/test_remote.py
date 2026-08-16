"""GCS (remote) backend over a fake metadata server + GCS JSON API."""

from __future__ import annotations

import pytest

import keelson_files as files
from keelson_files.client import FilesError
from keelson_files.tests.helpers import install_fake_gcs
from sdk_test_fixtures import parity_fixtures_dir


def test_write_uses_object_prefix(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    files.write("seen.json", "[]")
    assert fake.store["tenants/t/apps/a/files/seen.json"] == b"[]"


def test_read_roundtrip(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    files.write("k", b"payload")
    assert files.read("k") == b"payload"


def test_read_missing_returns_none_on_404(monkeypatch) -> None:
    install_fake_gcs(monkeypatch)
    assert files.read("absent") is None


def test_delete_idempotent_on_404(monkeypatch) -> None:
    install_fake_gcs(monkeypatch)
    files.delete("absent")  # must not raise


def test_delete_removes_object(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    files.write("k", "v")
    files.delete("k")
    assert "tenants/t/apps/a/files/k" not in fake.store


def test_list_strips_prefix_and_sorts(monkeypatch) -> None:
    install_fake_gcs(monkeypatch)
    files.write("b.txt", "b")
    files.write("a.txt", "a")
    files.write("cache/z.json", "z")
    assert files.list() == ["a.txt", "b.txt", "cache/z.json"]


def test_list_prefix(monkeypatch) -> None:
    install_fake_gcs(monkeypatch)
    files.write("a.txt", "a")
    files.write("cache/z.json", "z")
    assert files.list("cache/") == ["cache/z.json"]


def test_list_absorbs_paging(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    fake.page_size = 2
    for i in range(5):
        files.write(f"k{i}.txt", str(i))
    assert files.list() == ["k0.txt", "k1.txt", "k2.txt", "k3.txt", "k4.txt"]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_non_404_errors_raise_not_missing(monkeypatch, status: int) -> None:
    fake = install_fake_gcs(monkeypatch)
    fake.force_status["GET"] = status
    with pytest.raises(FilesError):
        files.read("k")


def test_token_is_cached_across_calls(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    files.write("a", "1")
    files.write("b", "2")
    files.read("a")
    assert fake.token_fetches == 1


def test_write_size_limit_enforced_before_upload(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    with pytest.raises(FilesError):
        files.write("big", b"x" * (10 * 1024 * 1024 + 1))
    assert fake.store == {}


def test_malformed_token_json_is_files_error(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    fake.bad_token_json = True
    with pytest.raises(FilesError, match="malformed access-token response"):
        files.read("k")


def test_malformed_list_json_is_files_error(monkeypatch) -> None:
    fake = install_fake_gcs(monkeypatch)
    fake.bad_list_json = True
    with pytest.raises(FilesError, match="malformed list response"):
        files.list()


@pytest.mark.parametrize(
    "body",
    [
        b"null",  # top-level JSON null
        b"[]",  # array, not an object
        b'{"items": "not-an-array"}',
        b'{"items": null}',  # items present but null
        b'{"items": [null]}',  # list item null
        b'{"items": [{}]}',  # item missing name
        b'{"items": [{"name": null}]}',  # item name null
        b'{"items": [{"name": 42}]}',  # item name not a string
        b'{"nextPageToken": null}',  # token present but null
        b'{"nextPageToken": 5}',  # numeric page token (would TypeError next loop)
    ],
)
def test_valid_json_wrong_schema_list_is_files_error(monkeypatch, body: bytes) -> None:
    fake = install_fake_gcs(monkeypatch)
    fake.list_body = body
    with pytest.raises(FilesError, match="malformed list response"):
        files.list()


def test_gcs_object_encoding_fixture(monkeypatch) -> None:
    import json

    fx = json.loads(
        (parity_fixtures_dir(__file__) / "files_gcs_object_encoding.json").read_text(
            encoding="utf-8"
        )
    )
    fake = install_fake_gcs(monkeypatch)
    monkeypatch.setenv("KEELSON_FILES_PREFIX", fx["files_prefix"])
    files.write(fx["key"], b"v")
    # (a) stored under the exact decoded object name
    assert fx["object_name"] in fake.store
    # (b) the '/' was percent-encoded on the wire (single flat object)
    assert any(fx["encoded_object"] in url for _, url in fake.calls)
    # round-trip read
    assert files.read(fx["key"]) == b"v"
