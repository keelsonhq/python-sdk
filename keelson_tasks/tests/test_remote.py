"""Remote (runtime API) backend: token, request shape, validation, retries."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse

import pytest

import keelson_tasks as tasks
from keelson_tasks.tests.helpers import (
    BASE_URL,
    METADATA_URL,
    TOKEN,
    install_fake_api,
    no_sleep,
)

_TRANSIENT_503 = (503, "Service Unavailable")
_ACCEPTED = (202, '{"task_id":"t-new"}')


def test_enqueue_sends_payload_and_key_with_identity_token(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)

    assert (
        tasks.enqueue(
            "Generate-PDF", {"order_id": 1, "note": "é"}, idempotency_key="k-1"
        )
        == "t-new"
    )

    (request,) = fake.requests
    assert request["method"] == "POST"
    # The name is URL-encoded as given; the server normalizes it.
    assert (
        request["url"] == f"{BASE_URL}/internal/apps/app_123/tasks/Generate-PDF/enqueue"
    )
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert request["headers"]["content-type"] == "application/json"
    assert (
        request["body"]
        == '{"payload":{"order_id":1,"note":"é"},"idempotency_key":"k-1"}'.encode()
    )
    assert request["timeout"] == 15
    (token_request,) = fake.token_requests
    assert token_request["headers"]["metadata-flavor"] == "Google"
    assert token_request["timeout"] == 15


def test_enqueue_without_key_omits_it_and_defaults_payload_to_null(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    tasks.enqueue("a")
    assert fake.requests[0]["body"] == b'{"payload":null}'


def test_identity_token_audience_is_the_base_url_verbatim(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    # A trailing slash stays in the audience but not in the request URL.
    monkeypatch.setenv("KEELSON_TASKS_BASE_URL", f"{BASE_URL}/")
    tasks.enqueue("a", None)

    assert fake.audiences() == [f"{BASE_URL}/"]
    token_url = fake.token_requests[0]["url"]
    assert token_url.startswith(f"{METADATA_URL}?audience=")
    # The audience is fully URL-encoded in the query.
    assert "audience=https%3A%2F%2F" in token_url
    assert fake.requests[0]["url"].startswith(f"{BASE_URL}/internal/apps/")


def test_identity_token_is_fetched_for_every_http_call(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _TRANSIENT_503, _ACCEPTED)
    no_sleep(monkeypatch)
    tasks.enqueue("a", None, idempotency_key="k")
    assert len(fake.token_requests) == len(fake.requests) == 2


def test_get_url_encodes_the_task_id(monkeypatch) -> None:
    body = {
        "task_id": "a/b c",
        "name": "a",
        "status": "queued",
        "claimed_attempts": 0,
        "last_failure_code": None,
        "created_at": "2026-10-02T03:04:05Z",
        "finished_at": None,
    }
    fake = install_fake_api(monkeypatch, (200, json.dumps(body)))
    assert tasks.get("a/b c").task_id == "a/b c"
    assert fake.requests[0]["method"] == "GET"
    assert (
        fake.requests[0]["url"] == f"{BASE_URL}/internal/apps/app_123/tasks/a%2Fb%20c"
    )
    assert fake.requests[0]["body"] is None


def test_enqueue_without_key_does_not_retry_transient_errors(monkeypatch) -> None:
    for answer in (
        _TRANSIENT_503,
        (502, ""),
        (504, ""),
        URLError(ConnectionRefusedError(111, "Connection refused")),
        TimeoutError("timed out"),
    ):
        fake = install_fake_api(monkeypatch, answer, _ACCEPTED)
        slept = no_sleep(monkeypatch)
        with pytest.raises(tasks.TasksError) as info:
            tasks.enqueue("a", {"x": 1})
        assert info.value.code == "TASKS_UNAVAILABLE_TRANSIENT"
        assert "idempotency_key" in info.value.message
        assert len(fake.requests) == 1
        assert slept == []


def test_enqueue_with_key_retries_transient_errors_up_to_three_attempts(
    monkeypatch,
) -> None:
    # Two transient failures, then success on the third attempt.
    fake = install_fake_api(
        monkeypatch,
        _TRANSIENT_503,
        URLError(ConnectionResetError(104, "reset")),
        _ACCEPTED,
    )
    slept = no_sleep(monkeypatch)
    assert tasks.enqueue("a", {"x": 1}, idempotency_key="k") == "t-new"
    assert len(fake.requests) == 3
    assert slept == [0.5, 1.0]
    # Every attempt sends the same body.
    assert len({request["body"] for request in fake.requests}) == 1

    # Three transient failures: no fourth attempt.
    fake = install_fake_api(monkeypatch, _TRANSIENT_503)
    slept = no_sleep(monkeypatch)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", {"x": 1}, idempotency_key="k")
    assert (info.value.code, info.value.status) == ("TASKS_UNAVAILABLE_TRANSIENT", 503)
    assert len(fake.requests) == 3
    assert slept == [0.5, 1.0]


@pytest.mark.parametrize(
    "answer, code",
    [
        ((403, "<html>Forbidden</html>"), "TASKS_FORBIDDEN"),
        ((401, ""), "TASKS_UNAUTHORIZED"),
        (
            (429, '{"error":{"code":"TASK_BACKLOG_LIMIT_EXCEEDED","message":"x"}}'),
            "TASK_BACKLOG_LIMIT_EXCEEDED",
        ),
        (
            (503, '{"error":{"code":"TASKS_UNAVAILABLE","message":"x"}}'),
            "TASKS_UNAVAILABLE",
        ),
        ((500, ""), "TASKS_SERVER_ERROR"),
    ],
)
def test_enqueue_with_key_does_not_retry_permanent_errors(
    monkeypatch, answer, code
) -> None:
    fake = install_fake_api(monkeypatch, answer, _ACCEPTED)
    slept = no_sleep(monkeypatch)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", None, idempotency_key="k")
    assert info.value.code == code
    assert len(fake.requests) == 1
    assert slept == []


def test_forbidden_message_explains_permission_propagation(monkeypatch) -> None:
    install_fake_api(monkeypatch, (403, "<html>Forbidden</html>"))
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", None)
    assert "few minutes" in info.value.message
    assert "tasks:" in info.value.message


def test_envelope_message_is_surfaced(monkeypatch) -> None:
    install_fake_api(
        monkeypatch,
        (
            404,
            '{"error":{"code":"TASK_NOT_DECLARED","message":"Task is not declared."}}',
        ),
    )
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("nope", None)
    assert (info.value.code, info.value.status, info.value.message) == (
        "TASK_NOT_DECLARED",
        404,
        "Task is not declared.",
    )


def test_payload_limit_is_checked_on_exact_body_bytes(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    key = "order-1"
    overhead = len(b'{"payload":"","idempotency_key":"order-1"}')
    # "é" is 2 bytes in UTF-8 (ensure_ascii=False): counting characters or
    # \u-escaped bytes would both measure differently.
    fill = 65536 - overhead
    payload = "é" * (fill // 2) + "x" * (fill % 2)

    tasks.enqueue("a", payload, idempotency_key=key)
    assert len(fake.requests[0]["body"]) == 65536

    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", payload + "x", idempotency_key=key)
    assert info.value.code == "TASK_PAYLOAD_TOO_LARGE"
    assert info.value.status is None
    # The key counts too: the same payload without it fits.
    tasks.enqueue("a", payload + "x")
    assert len(fake.requests) == 2
    assert (
        len(fake.requests[1]["body"])
        == 65536 - len(b',"idempotency_key":"order-1"') + 1
    )


@pytest.mark.parametrize(
    "key",
    ["", "x" * 129, "tab\there", "naïve", "line\n", "\x7f"],
)
def test_invalid_idempotency_key_is_rejected_before_sending(monkeypatch, key) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", None, idempotency_key=key)
    assert info.value.code == "TASK_INVALID_REQUEST"
    assert fake.requests == [] and fake.token_requests == []


def test_idempotency_key_boundaries_are_accepted(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    for key in (" ", "~", "x" * 128):
        tasks.enqueue("a", None, idempotency_key=key)
    assert len(fake.requests) == 3


@pytest.mark.parametrize(
    "payload",
    [float("nan"), {"x": float("inf")}, {1, 2}, object(), "\ud800"],
    ids=["nan", "infinity", "set", "object", "lone-surrogate"],
)
def test_unserializable_payload_is_invalid_request(monkeypatch, payload) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", payload)
    assert info.value.code == "TASK_INVALID_REQUEST"
    assert fake.requests == []


def test_circular_payload_is_invalid_request(monkeypatch) -> None:
    install_fake_api(monkeypatch, _ACCEPTED)
    loop: list = []
    loop.append(loop)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", loop)
    assert info.value.code == "TASK_INVALID_REQUEST"


@pytest.mark.parametrize("name", ["", "   ", None, 3])
def test_empty_name_is_invalid_request(monkeypatch, name) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue(name, None)  # type: ignore[arg-type]
    assert info.value.code == "TASK_INVALID_REQUEST"
    assert fake.requests == []


@pytest.mark.parametrize("task_id", ["", None])
def test_empty_task_id_is_invalid_request(monkeypatch, task_id) -> None:
    fake = install_fake_api(monkeypatch, (200, "{}"))
    with pytest.raises(tasks.TasksError) as info:
        tasks.get(task_id)  # type: ignore[arg-type]
    assert info.value.code == "TASK_INVALID_REQUEST"
    assert fake.requests == []


@pytest.mark.parametrize(
    "token_answer",
    [
        (404, "not found"),
        (200, ""),
        (200, "two words"),
        URLError(OSError("Name or service not known")),
        TimeoutError("timed out"),
    ],
    ids=["http-error", "empty", "whitespace", "unreachable", "timeout"],
)
def test_identity_token_failure_is_not_retried(monkeypatch, token_answer) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    fake.token_answer = token_answer
    slept = no_sleep(monkeypatch)
    with pytest.raises(tasks.TasksError) as info:
        tasks.get("t-1")
    assert info.value.code == "TASKS_IDENTITY_TOKEN_ERROR"
    assert info.value.status is None
    assert len(fake.token_requests) == 1
    assert fake.requests == [] and slept == []


def test_error_message_never_contains_payload(monkeypatch) -> None:
    marker = "PAYLOAD-SECRET-7f3a"
    payload = {"secret": marker, "nan": float("nan")}
    install_fake_api(monkeypatch, _TRANSIENT_503)
    no_sleep(monkeypatch)
    errors: list[tasks.TasksError] = []

    def capture(call) -> None:
        with pytest.raises(tasks.TasksError) as info:
            call()
        errors.append(info.value)

    capture(lambda: tasks.enqueue("a", payload))  # not serializable
    capture(lambda: tasks.enqueue("a", {"secret": marker * 10000}))  # too large
    capture(lambda: tasks.enqueue("a", {"secret": marker}, idempotency_key="\n"))
    capture(lambda: tasks.enqueue("a", {"secret": marker}))  # transient, no key
    capture(lambda: tasks.enqueue("a", {"secret": marker}, idempotency_key="k"))
    install_fake_api(
        monkeypatch, (400, '{"error":{"code":"TASK_INVALID_REQUEST","message":"bad"}}')
    )
    capture(lambda: tasks.enqueue("a", {"secret": marker}))
    install_fake_api(monkeypatch, (202, "not json"))
    capture(lambda: tasks.enqueue("a", {"secret": marker}))

    assert len(errors) == 7
    for error in errors:
        assert marker not in str(error)
        assert marker not in error.message
        assert marker not in repr(error.args)
        # No chained exception carries the payload either.
        assert error.__cause__ is None


# ---------------------------------------------------------------------------
# Against a real local HTTP server (the real urllib opener)
# ---------------------------------------------------------------------------


class _Server:
    def __init__(self) -> None:
        self.hits: list[tuple[str, str, dict, bytes]] = []
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.hits.append((self.command, self.path, dict(self.headers), body))
                path = urlparse(self.path).path
                status, headers, payload = outer.routes.get(path, (404, {}, b""))
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve
            do_POST = _serve

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _use_server(monkeypatch, server: _Server) -> None:
    monkeypatch.setenv("KEELSON_MODE", "keelson")
    monkeypatch.setenv("KEELSON_APP_ID", "app_123")
    monkeypatch.setenv("KEELSON_TASKS_BASE_URL", server.url)
    monkeypatch.setenv("KEELSON_TASKS_METADATA_URL", f"{server.url}/identity")
    server.routes["/identity"] = (200, {}, TOKEN.encode())


def test_real_http_round_trip(monkeypatch) -> None:
    with _Server() as server:
        _use_server(monkeypatch, server)
        server.routes["/internal/apps/app_123/tasks/a/enqueue"] = (
            202,
            {},
            b'{"task_id":"t-9"}',
        )
        assert tasks.enqueue("a", [1, 2], idempotency_key="k") == "t-9"
        server.routes["/internal/apps/app_123/tasks/t-9"] = (
            404,
            {"Content-Type": "application/json"},
            b'{"error":{"code":"TASK_NOT_FOUND","message":"no"}}',
        )
        with pytest.raises(tasks.TasksError) as info:
            tasks.get("t-9")
        assert (info.value.code, info.value.status) == ("TASK_NOT_FOUND", 404)

    token_hit, enqueue_hit = server.hits[0], server.hits[1]
    assert parse_qs(urlparse(token_hit[1]).query)["audience"] == [server.url]
    assert token_hit[2]["Metadata-Flavor"] == "Google"
    assert enqueue_hit[2]["Authorization"] == f"Bearer {TOKEN}"
    assert enqueue_hit[3] == b'{"payload":[1,2],"idempotency_key":"k"}'


def test_redirect_is_not_followed(monkeypatch) -> None:
    with _Server() as server:
        _use_server(monkeypatch, server)
        server.routes["/internal/apps/app_123/tasks/a/enqueue"] = (
            302,
            {"Location": f"{server.url}/elsewhere"},
            b"",
        )
        server.routes["/elsewhere"] = (202, {}, b'{"task_id":"stolen"}')
        with pytest.raises(tasks.TasksError) as info:
            tasks.enqueue("a", None, idempotency_key="k")
    assert (info.value.code, info.value.status) == ("TASKS_HTTP_ERROR", 302)
    assert all(urlparse(hit[1]).path != "/elsewhere" for hit in server.hits)


def test_resolution_happens_on_every_call(monkeypatch) -> None:
    fake = install_fake_api(monkeypatch, _ACCEPTED)
    tasks.enqueue("a", None)
    monkeypatch.setenv("KEELSON_APP_ID", "app_456")
    tasks.enqueue("a", None)
    assert [r["url"].split("/")[5] for r in fake.requests] == ["app_123", "app_456"]
    monkeypatch.delenv("KEELSON_TASKS_BASE_URL")
    with pytest.raises(tasks.TasksError) as info:
        tasks.enqueue("a", None)
    assert info.value.code == "TASKS_NOT_CONFIGURED"
    assert "tasks:" in info.value.message
