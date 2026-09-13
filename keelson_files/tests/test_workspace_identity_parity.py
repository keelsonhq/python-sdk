"""Cross-language workspace environment resolution parity."""

from __future__ import annotations

import json

import pytest
from sdk_test_fixtures import parity_fixtures_dir

from keelson_files.client import _workspace_id

FIXTURE = parity_fixtures_dir(__file__) / "workspace_identity_parity.json"


@pytest.mark.parametrize(
    "case",
    json.loads(FIXTURE.read_text())["cases"],
    ids=lambda case: case["name"],
)
def test_workspace_identity_parity(
    monkeypatch: pytest.MonkeyPatch, case: dict[str, str | None]
) -> None:
    for env_name, key in (
        ("KEELSON_WORKSPACE_ID", "workspace_id"),
        ("KEELSON_TENANT_ID", "tenant_id"),
    ):
        value = case[key]
        if value is None:
            monkeypatch.delenv(env_name, raising=False)
        else:
            monkeypatch.setenv(env_name, value)

    assert _workspace_id() == (case["expected"] or "")
