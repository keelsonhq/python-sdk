"""Parity: consume the shared list-ordering and missing-read fixtures."""

from __future__ import annotations

import json

import keelson_files as files
from keelson_files.tests.helpers import install_fake_gcs
from sdk_test_fixtures import parity_fixtures_dir

_FIXTURES = parity_fixtures_dir(__file__)


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_list_ordering_fixture(monkeypatch) -> None:
    fx = _load("files_list_ordering.json")
    fake = install_fake_gcs(monkeypatch)
    # Seed the object store directly with the fixture object names.
    for name in fx["object_names"]:
        fake.store[name] = b"x"
    monkeypatch.setenv("KEELSON_FILES_PREFIX", fx["files_prefix"])
    assert files.list() == fx["expected"]


def test_missing_read_fixture(monkeypatch) -> None:
    fx = _load("files_missing_read.json")
    fake = install_fake_gcs(monkeypatch)
    # 404 → None, delete of missing → success.
    assert files.read("absent") is None
    files.delete("absent")
    # error statuses must raise, never map to missing.
    for status in fx["error_statuses"]:
        fake.force_status["GET"] = status
        raised = False
        try:
            files.read("k")
        except files.FilesError:
            raised = True
        assert raised, f"status {status} must raise, not be treated as missing"
