from __future__ import annotations

import pytest

import keelson_tasks.client as client
from keelson_tasks.tests.helpers import TASK_ENVS


@pytest.fixture(autouse=True)
def _clean_tasks_state(monkeypatch):
    for name in TASK_ENVS:
        monkeypatch.delenv(name, raising=False)
    client._reset_local_state()
    yield
    client._reset_local_state()
