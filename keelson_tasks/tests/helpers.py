"""Shared test helpers: a fake metadata server + runtime API, and a fake CLI.

``FakeApi`` replaces ``keelson_tasks.client._open`` so the remote backend runs
without network access. ``install_fake_cli`` puts an executable ``keelson`` on
PATH that records its argv and stdin and prints a scripted stdout. On Windows
it is ``keelson.cmd`` (``shutil.which`` only finds PATHEXT names there) running
the same Python body.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from email.message import Message
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from sdk_test_fixtures import parity_fixtures_dir

import keelson_tasks.client as client

FIXTURES = parity_fixtures_dir(__file__)

BASE_URL = "https://keelson-runtime-api-abc123-an.a.run.app"
METADATA_URL = "http://metadata.example/identity"
TOKEN = "header.claims.signature"

REMOTE_ENV = {
    "KEELSON_MODE": "keelson",
    "KEELSON_TASKS_BASE_URL": BASE_URL,
    "KEELSON_APP_ID": "app_123",
    "KEELSON_TASKS_METADATA_URL": METADATA_URL,
}

TASK_ENVS = (
    "KEELSON_MODE",
    "KEELSON_TASKS_BASE_URL",
    "KEELSON_APP_ID",
    "KEELSON_WORKSPACE_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
    "KEELSON_TASKS_METADATA_URL",
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> bool:
        return False


# A scripted runtime API answer: (status, body) or an exception to raise.
Answer = tuple[int, bytes | str] | BaseException


class FakeApi:
    """Records every request; answers from ``answers`` (FIFO, last one sticks)."""

    def __init__(self, *answers: Answer) -> None:
        self.answers: list[Answer] = list(answers) or [(202, '{"task_id":"t-1"}')]
        self.requests: list[dict] = []
        self.token_requests: list[dict] = []
        self.token_answer: Answer = (200, TOKEN)

    def __call__(self, req, timeout: float):
        url = req.full_url
        headers = {k.lower(): v for k, v in req.header_items()}
        if url.startswith(METADATA_URL):
            self.token_requests.append(
                {"url": url, "headers": headers, "timeout": timeout}
            )
            return self._answer(self.token_answer, url)
        self.requests.append(
            {
                "method": req.get_method(),
                "url": url,
                "headers": headers,
                "body": req.data,
                "timeout": timeout,
            }
        )
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        return self._answer(answer, url)

    @staticmethod
    def _answer(answer: Answer, url: str):
        if isinstance(answer, BaseException):
            raise answer
        status, body = answer
        raw = body.encode("utf-8") if isinstance(body, str) else body
        if 200 <= status < 300:
            return FakeResponse(status, raw)
        raise HTTPError(url, status, "", Message(), BytesIO(raw))

    def audiences(self) -> list[str]:
        out = []
        for request in self.token_requests:
            query = parse_qs(urlparse(request["url"]).query)
            out.extend(query["audience"])
        return out


def install_fake_api(monkeypatch, *answers: Answer) -> FakeApi:
    for name, value in REMOTE_ENV.items():
        monkeypatch.setenv(name, value)
    fake = FakeApi(*answers)
    monkeypatch.setattr(client, "_open", fake)
    return fake


def no_sleep(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(client, "_sleep", slept.append)
    return slept


_FAKE_CLI = """import json, os, sys, uuid
stdin = sys.stdin.buffer.read()
with open(os.environ["FAKE_KEELSON_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin.decode("utf-8"),
                          "cwd": os.getcwd()}}) + "\\n")
sys.stderr.write("fake-cli-stderr\\n")
with open(os.environ["FAKE_KEELSON_STDOUT"], encoding="utf-8") as out:
    sys.stdout.write(out.read().replace("@ID@", str(uuid.uuid4())))
sys.exit(int(os.environ.get("FAKE_KEELSON_EXIT", "0")))
"""


# Windows runs a .cmd through cmd.exe; it hands stdin/stdout/stderr and the
# exit code of the Python body straight through.
_FAKE_CLI_CMD = '@"{python}" "%~dp0keelson.py" %*\r\n@exit /b %ERRORLEVEL%\r\n'


class FakeCli:
    def __init__(self, root: Path) -> None:
        self.bin_dir = root / "bin"
        # The file `shutil.which("keelson")` resolves to.
        self.executable = self.bin_dir / (
            "keelson.cmd" if os.name == "nt" else "keelson"
        )
        self.log = root / "cli.log"
        self.stdout_file = root / "cli.stdout"

    def set_stdout(
        self, value: object | None = None, *, text: str | None = None
    ) -> None:
        if text is None:
            text = json.dumps(value, separators=(",", ":")) + "\n"
        self.stdout_file.write_text(text, encoding="utf-8")

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]


def task_result(
    *,
    task_id: str = "@ID@",
    name: str = "generate-pdf",
    status: str = "succeeded",
    last_failure_code: str | None = None,
    exit_code: int | None = 0,
) -> dict:
    return {
        "task": {
            "task_id": task_id,
            "name": name,
            "status": status,
            "claimed_attempts": 1,
            "last_failure_code": last_failure_code,
            "created_at": "2026-10-02T03:04:05.000000Z",
            "finished_at": "2026-10-02T03:04:06.000000Z",
            "exit_code": exit_code,
            "timed_out": False,
        }
    }


def install_fake_cli(monkeypatch, root: Path, stdout: object | None = None) -> FakeCli:
    fake = FakeCli(root)
    fake.bin_dir.mkdir(parents=True, exist_ok=True)
    body = _FAKE_CLI.format()
    if os.name == "nt":
        (fake.bin_dir / "keelson.py").write_text(body, encoding="utf-8")
        fake.executable.write_text(
            _FAKE_CLI_CMD.format(python=sys.executable), encoding="utf-8", newline=""
        )
    else:
        script = fake.executable
        script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    fake.set_stdout(stdout if stdout is not None else task_result())
    monkeypatch.setenv("PATH", str(fake.bin_dir))
    monkeypatch.setenv("FAKE_KEELSON_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_KEELSON_STDOUT", str(fake.stdout_file))
    monkeypatch.setenv("KEELSON_MODE", "local")
    return fake
