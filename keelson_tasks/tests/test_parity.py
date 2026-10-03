"""Parity: the shared ``tasks_*.json`` fixtures, through the real SDK paths."""

from __future__ import annotations

from dataclasses import asdict
from urllib.error import URLError

import pytest

import keelson_tasks as tasks
import keelson_tasks.client as client
from keelson_tasks.tests.helpers import (
    install_fake_api,
    install_fake_cli,
    load_fixture,
    no_sleep,
)

_MODE = load_fixture("tasks_mode_resolution.json")
_ERRORS = load_fixture("tasks_error_mapping.json")
_RESPONSES = load_fixture("tasks_get_response.json")
_LOCAL = load_fixture("tasks_local_cli_result.json")


def _ids(cases: list[dict]) -> list[str]:
    return [case["name"] for case in cases]


@pytest.mark.parametrize("case", _MODE["cases"], ids=_ids(_MODE["cases"]))
def test_mode_resolution_matches_parity_fixture(monkeypatch, case) -> None:
    for name in _MODE["env_vars"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in case["env"].items():
        monkeypatch.setenv(name, value)

    # --- Cross-language parity assertions ---
    expected = case["expected"]
    if "error_code" in expected:
        with pytest.raises(tasks.TasksError) as info:
            client._resolve_mode()
        assert info.value.code == expected["error_code"]
        assert info.value.status is None
        return
    resolved = client._resolve_mode()
    if expected["mode"] == "local":
        assert resolved is None
    else:
        assert resolved == client._Remote(
            audience=expected["audience"],
            api_base=expected["api_base"],
            app_id=expected["app_id"],
        )


_MODE_ERRORS = [case for case in _MODE["cases"] if "error_code" in case["expected"]]


@pytest.mark.parametrize("case", _MODE_ERRORS, ids=_ids(_MODE_ERRORS))
def test_public_calls_fail_closed_per_mode_fixture(monkeypatch, case) -> None:
    """enqueue and get go through the same resolution: a config error is
    raised before anything runs, never a silent local run."""
    for name in _MODE["env_vars"]:
        monkeypatch.delenv(name, raising=False)
    for name, value in case["env"].items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", "")
    for call in (lambda: tasks.enqueue("a", None), lambda: tasks.get("t")):
        with pytest.raises(tasks.TasksError) as info:
            call()
        assert info.value.code == "TASKS_NOT_CONFIGURED"


@pytest.mark.parametrize("case", _ERRORS["http"], ids=_ids(_ERRORS["http"]))
def test_http_errors_map_per_parity_fixture(monkeypatch, case) -> None:
    expected = case["expected"]

    # --- Cross-language parity assertions ---
    error, transient = client._http_error(case["status"], case["body"].encode())
    assert (error.code, error.status, transient) == (
        expected["code"],
        expected["status"],
        expected["transient"],
    )

    # The same answer through get (always retried on transient errors).
    fake = install_fake_api(monkeypatch, (case["status"], case["body"]))
    slept = no_sleep(monkeypatch)
    with pytest.raises(tasks.TasksError) as info:
        tasks.get("t-1")
    assert (info.value.code, info.value.status) == (
        expected["code"],
        expected["status"],
    )
    assert len(fake.requests) == (3 if expected["transient"] else 1)
    assert slept == ([0.5, 1.0] if expected["transient"] else [])


_TRANSPORT_FAILURES = {
    "connection_error": lambda: URLError(
        ConnectionRefusedError(111, "Connection refused")
    ),
    "timeout": lambda: TimeoutError("timed out"),
}


@pytest.mark.parametrize("case", _ERRORS["transport"], ids=_ids(_ERRORS["transport"]))
def test_transport_errors_map_per_parity_fixture(monkeypatch, case) -> None:
    expected = case["expected"]
    failure = _TRANSPORT_FAILURES[case["failure"]]
    fake = install_fake_api(monkeypatch, failure())
    slept = no_sleep(monkeypatch)

    # --- Cross-language parity assertions ---
    with pytest.raises(tasks.TasksError) as info:
        tasks.get("t-1")
    assert info.value.code == expected["code"]
    assert info.value.status is None
    assert len(fake.requests) == (3 if expected["transient"] else 1)
    assert slept == ([0.5, 1.0] if expected["transient"] else [])


def test_socket_timeout_is_transient(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, URLError(TimeoutError("timed out")))
    no_sleep(monkeypatch)
    with pytest.raises(tasks.TasksError) as info:
        tasks.get("t-1")
    assert info.value.code == "TASKS_UNAVAILABLE_TRANSIENT"
    assert len(fake.requests) == 3


@pytest.mark.parametrize("case", _RESPONSES["get"], ids=_ids(_RESPONSES["get"]))
def test_get_response_parses_per_parity_fixture(monkeypatch, case) -> None:
    install_fake_api(monkeypatch, (200, case["body"]))
    expected = case["expected"]

    # --- Cross-language parity assertions ---
    if "error_code" in expected:
        with pytest.raises(tasks.TasksError) as info:
            tasks.get("t-1")
        assert info.value.code == expected["error_code"]
        assert info.value.status == 200
        return
    status = tasks.get("t-1")
    assert isinstance(status, tasks.TaskStatus)
    assert asdict(status) == expected["status"]


@pytest.mark.parametrize("case", _RESPONSES["enqueue"], ids=_ids(_RESPONSES["enqueue"]))
def test_enqueue_response_parses_per_parity_fixture(monkeypatch, case) -> None:
    install_fake_api(monkeypatch, (case["status"], case["body"]))
    expected = case["expected"]

    # --- Cross-language parity assertions ---
    if "error_code" in expected:
        with pytest.raises(tasks.TasksError) as info:
            tasks.enqueue("generate-pdf", {"order_id": 1})
        assert info.value.code == expected["error_code"]
        assert info.value.status == case["status"]
        return
    assert tasks.enqueue("generate-pdf", {"order_id": 1}) == expected["task_id"]


def test_local_cli_fixture_argv_and_keys_match_the_sdk() -> None:
    assert _LOCAL["argv"] == [
        "dev",
        "task",
        "run",
        "<name>",
        "--payload",
        "-",
        "--json",
    ]
    # The seven get fields are exactly the task keys minus the local-only two.
    status_keys = set(tasks.TaskStatus.__dataclass_fields__)
    assert status_keys == set(_LOCAL["task_keys"]) - {"exit_code", "timed_out"}


@pytest.mark.parametrize("case", _LOCAL["cases"], ids=_ids(_LOCAL["cases"]))
def test_local_cli_result_maps_per_parity_fixture(monkeypatch, tmp_path, case) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)
    if "stdout_text" in case:
        fake.set_stdout(text=case["stdout_text"])
    else:
        fake.set_stdout(case["stdout"])
    expected = case["sdk"]

    # --- Cross-language parity assertions ---
    if "error_code" in expected:
        with pytest.raises(tasks.TasksError) as info:
            tasks.enqueue("generate-pdf", {"order_id": 1})
        assert info.value.code == expected["error_code"]
        assert info.value.status is None
        if expected["error_code"] == "TASKS_LOCAL_CLI_FAILED":
            assert "keelson upgrade" in info.value.message
        return
    task_id = tasks.enqueue("generate-pdf", {"order_id": 1})
    assert task_id == expected["task_id"]
    assert asdict(tasks.get(task_id)) == expected["status"]
    calls = fake.calls()
    assert len(calls) == 1
    assert calls[0]["argv"] == [
        "generate-pdf" if arg == "<name>" else arg for arg in _LOCAL["argv"]
    ]
