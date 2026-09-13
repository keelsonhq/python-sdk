"""Cross-language workspace environment resolution parity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from keelson_files.client import _workspace_id


FIXTURE = Path(__file__).resolve().parents[3] / "testdata" / "workspace_identity_parity.json"


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
