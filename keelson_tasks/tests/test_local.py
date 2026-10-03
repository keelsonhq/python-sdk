"""Local backend: `keelson dev task run` per enqueue, results in-process."""

from __future__ import annotations

import json
import os
import threading

import pytest

import keelson_tasks as tasks
from keelson_tasks.tests.helpers import install_fake_cli, task_result

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the fake CLI is a POSIX shebang script"
)


def test_local_enqueue_runs_cli_and_get_returns_result(monkeypatch, tmp_path) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)

    task_id = tasks.enqueue("Generate-PDF", {"order_id": 1, "note": "é"})

    (call,) = fake.calls()
    # The name goes as given (the CLI normalizes it); only the payload is on stdin.
    assert call["argv"] == [
        "dev",
        "task",
        "run",
        "Generate-PDF",
        "--payload",
        "-",
        "--json",
    ]
    assert call["stdin"] == '{"order_id":1,"note":"é"}'
    assert call["cwd"] == str(tmp_path)
    status = tasks.get(task_id)
    assert status == tasks.TaskStatus(
        task_id=task_id,
        name="generate-pdf",
        status="succeeded",
        claimed_attempts=1,
        last_failure_code=None,
        created_at="2026-10-02T03:04:05.000000Z",
        finished_at="2026-10-02T03:04:06.000000Z",
    )
    assert not hasattr(status, "exit_code")


def test_local_failed_command_is_not_an_enqueue_error(monkeypatch, tmp_path) -> None:
    # The CLI exits 1 when the command failed; stdout alone decides.
    install_fake_cli(
        monkeypatch,
        tmp_path,
        task_result(status="failed", last_failure_code="exit_nonzero", exit_code=3),
    )
    monkeypatch.setenv("FAKE_KEELSON_EXIT", "1")
    task_id = tasks.enqueue("generate-pdf")
    status = tasks.get(task_id)
    assert (status.status, status.last_failure_code) == ("failed", "exit_nonzero")


def test_local_payload_defaults_to_null(monkeypatch, tmp_path) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)
    tasks.enqueue("generate-pdf")
    assert fake.calls()[0]["stdin"] == "null"


def test_local_cli_stderr_goes_to_the_app_stderr(monkeypatch, tmp_path, capfd) -> None:
    install_fake_cli(monkeypatch, tmp_path)
    tasks.enqueue("generate-pdf")
    captured = capfd.readouterr()
    assert "fake-cli-stderr" in captured.err
    assert captured.out == ""


def test_local_same_key_does_not_rerun_cli(monkeypatch, tmp_path) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)

    first = tasks.enqueue("generate-pdf", {"n": 1}, idempotency_key="k1")
    # Same normalized name + key: the existing task_id, no new run.
    assert tasks.enqueue(" Generate-PDF ", {"n": 2}, idempotency_key="k1") == first
    assert len(fake.calls()) == 1

    # A different key, a different name, or no key runs again.
    other_key = tasks.enqueue("generate-pdf", {"n": 1}, idempotency_key="k2")
    no_key_1 = tasks.enqueue("generate-pdf", {"n": 1})
    no_key_2 = tasks.enqueue("generate-pdf", {"n": 1})
    assert len(fake.calls()) == 4
    assert len({first, other_key, no_key_1, no_key_2}) == 4


def test_local_failed_cli_run_does_not_claim_the_key(monkeypatch, tmp_path) -> None:
    fake = install_fake_cli(
        monkeypatch, tmp_path, {"error": {"code": "TASK_NOT_DECLARED", "message": "no"}}
    )
    with pytest.raises(tasks.TasksError):
        tasks.enqueue("generate-pdf", None, idempotency_key="k1")
    fake.set_stdout(task_result())
    task_id = tasks.enqueue("generate-pdf", None, idempotency_key="k1")
    assert tasks.get(task_id).status == "succeeded"
    assert len(fake.calls()) == 2


def test_local_concurrent_same_key_keeps_the_first_mapping(
    monkeypatch, tmp_path
) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait()
        results.append(tasks.enqueue("generate-pdf", None, idempotency_key="k"))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Both may run the CLI; each gets its own task, and both are readable.
    assert len(results) == 2
    for task_id in results:
        assert tasks.get(task_id).status == "succeeded"
    # Later calls with the key return whichever finished first.
    again = tasks.enqueue("generate-pdf", None, idempotency_key="k")
    assert again in results
    assert len(fake.calls()) in (1, 2)


def test_local_get_unknown_id_is_not_found(monkeypatch, tmp_path) -> None:
    install_fake_cli(monkeypatch, tmp_path)
    with pytest.raises(tasks.TasksError) as info:
        tasks.get("0b9f3f8e-5c1d-4a7b-9e2f-1d2c3b4a5f60")
    assert (info.value.code, info.value.status) == ("TASK_NOT_FOUND", None)


def test_local_missing_cli_error_has_install_hint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KEELSON_MODE", "local")
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("generate-pdf", {"order_id": 1})
    assert info.value.code == "TASKS_LOCAL_CLI_NOT_FOUND"
    assert "`keelson` was not found" in info.value.message
    assert "https://keelson.dev/install.sh" in info.value.message


def test_local_unstartable_cli_is_cli_failed(monkeypatch, tmp_path) -> None:
    install_fake_cli(monkeypatch, tmp_path)
    # Executable bit set, but not a valid executable format.
    (tmp_path / "bin" / "keelson").write_bytes(b"\x00\x01garbage")
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("generate-pdf")
    assert info.value.code == "TASKS_LOCAL_CLI_FAILED"


@pytest.mark.parametrize("name", ["--json", "-x", "bad name", "a" * 64, "ä", "a_b"])
def test_local_undeclarable_name_is_not_declared_without_running_cli(
    monkeypatch, tmp_path, name
) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue(name)
    assert info.value.code == "TASK_NOT_DECLARED"
    assert fake.calls() == []


def test_local_runs_the_same_prechecks_as_remote(monkeypatch, tmp_path) -> None:
    fake = install_fake_cli(monkeypatch, tmp_path)
    for call, code in (
        (lambda: tasks.enqueue("generate-pdf", "x" * 65536), "TASK_PAYLOAD_TOO_LARGE"),
        (lambda: tasks.enqueue("generate-pdf", float("nan")), "TASK_INVALID_REQUEST"),
        (
            lambda: tasks.enqueue("generate-pdf", None, idempotency_key=""),
            "TASK_INVALID_REQUEST",
        ),
        (lambda: tasks.enqueue("", None), "TASK_INVALID_REQUEST"),
    ):
        with pytest.raises(tasks.TasksError) as info:
            call()
        assert info.value.code == code
    assert fake.calls() == []


def test_local_cli_error_message_never_contains_payload(monkeypatch, tmp_path) -> None:
    marker = "PAYLOAD-SECRET-7f3a"
    fake = install_fake_cli(
        monkeypatch,
        tmp_path,
        {"error": {"code": "task_interrupted", "message": "stopped"}},
    )
    for stdout in (
        {"error": {"code": "task_interrupted", "message": "stopped"}},
        {"error": {"code": "TASK_INVALID_REQUEST", "message": "bad"}},
        {"task": {"task_id": "x"}},
        None,
    ):
        if stdout is None:
            fake.set_stdout(text="garbage\n")
        else:
            fake.set_stdout(stdout)
        with pytest.raises(tasks.TasksError) as info:
            tasks.enqueue("generate-pdf", {"secret": marker})
        assert marker not in str(info.value)
        assert info.value.__cause__ is None
    assert all(json.loads(call["stdin"]) == {"secret": marker} for call in fake.calls())
