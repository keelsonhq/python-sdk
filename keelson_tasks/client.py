"""Keelson Tasks SDK — enqueue background tasks declared under ``tasks:``.

API:

- ``enqueue(name, payload=None, idempotency_key=None)`` — the ``task_id``.
- ``get(task_id)`` — a :class:`TaskStatus` (the runtime API's seven fields).

Backends:

- Keelson (``remote``): ``POST`` / ``GET`` on the runtime API at
  ``KEELSON_TASKS_BASE_URL``, authenticated with an OIDC id token from the
  metadata server whose audience is that URL. The command then runs later on a
  separate instance, with platform retries.
- ``local``: ``enqueue`` starts ``keelson dev task run <name> --payload -
  --json`` (the CLI on PATH), which runs the declared command once,
  synchronously, and prints one JSON result. Results are kept in this process
  only, so ``get`` knows only tasks enqueued here. No retry.

Mode resolution: ``KEELSON_MODE`` is the single mode signal, and the SDK never
silently falls back to local execution on Keelson. See :func:`_resolve_mode`.

Errors are one type, :class:`TasksError`, branched on ``code``: the runtime
API's codes pass through (``TASK_NOT_DECLARED``, ``TASKS_UNAVAILABLE``, ...),
and the SDK's own codes start with ``TASKS_`` (``TASKS_UNAVAILABLE_TRANSIENT``,
``TASKS_FORBIDDEN``, ...). The payload never appears in an error message.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

_SDK_USER_AGENT = "Keelson-Python-SDK/0.1.1"

# Per HTTP call (metadata server and runtime API).
_TIMEOUT = 15

# The platform's limit, measured on the whole request body the server receives.
MAX_BODY_BYTES = 65536
_IDEMPOTENCY_KEY_MAX_CHARS = 128

# Transient failures: 3 attempts in total, waiting 0.5 s then 1 s.
_RETRY_DELAYS = (0.5, 1.0)

# The identity endpoint, up to (not including) ``?audience=``. The env override
# is an SDK-internal test seam, NOT part of the platform-injected env contract.
_DEFAULT_METADATA_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

_INSTALL_URL = "https://keelson.dev/install.sh"

# Platform-owned identifiers whose presence means the app is running on Keelson.
_CORE_IDENTIFIER_ENVS = (
    "KEELSON_APP_ID",
    "KEELSON_WORKSPACE_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
)

# Declared task names (keelson-yaml-spec § 4A), after trim + lowercase.
_TASK_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)

_RFC3339_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"(?:Z|[+-](\d{2}):(\d{2}))",
    re.ASCII,
)

# CLI error codes that mean the same as the runtime API's and pass through.
_LOCAL_PASSTHROUGH_CODES = frozenset(
    {"TASK_NOT_DECLARED", "TASK_INVALID_REQUEST", "TASK_PAYLOAD_TOO_LARGE"}
)


class TasksError(RuntimeError):
    """Every Tasks failure. Branch on ``code``; ``status`` is the HTTP status
    of the runtime API response, or ``None`` when there was none."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class TaskStatus:
    """A task's state, with the runtime API's field names (§ 9.3). Timestamps
    are RFC 3339 strings."""

    task_id: str
    name: str
    status: str
    claimed_attempts: int
    last_failure_code: str | None
    created_at: str
    finished_at: str | None


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Remote:
    audience: str
    api_base: str
    app_id: str


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _not_configured(message: str) -> TasksError:
    return TasksError("TASKS_NOT_CONFIGURED", message)


def _resolve_mode() -> _Remote | None:
    """Resolve the backend: a :class:`_Remote` for Keelson, ``None`` for local.

    - ``KEELSON_MODE=keelson`` → remote. ``KEELSON_TASKS_BASE_URL`` and
      ``KEELSON_APP_ID`` are required; either missing → ``TASKS_NOT_CONFIGURED``.
    - ``KEELSON_MODE=local`` → local.
    - ``KEELSON_MODE`` unset → local, unless a platform identifier is visible,
      in which case the silent local fallback is refused. The remote env is
      **not** consulted here.
    - Any other value → ``TASKS_NOT_CONFIGURED``.
    """
    mode = _env("KEELSON_MODE").lower()
    if mode == "keelson":
        base_url = _env("KEELSON_TASKS_BASE_URL")
        if not base_url:
            raise _not_configured(
                "KEELSON_MODE=keelson but KEELSON_TASKS_BASE_URL is unset; the "
                "platform injects it when keelson.yaml declares tasks: and the "
                "app is deployed."
            )
        app_id = _env("KEELSON_APP_ID")
        if not app_id:
            raise _not_configured(
                "KEELSON_MODE=keelson but KEELSON_APP_ID is unset; the Tasks "
                "capability is unavailable for this deployment."
            )
        return _Remote(audience=base_url, api_base=base_url.rstrip("/"), app_id=app_id)

    if mode == "local":
        return None

    if mode == "":
        if any(_env(name) for name in _CORE_IDENTIFIER_ENVS):
            raise _not_configured(
                "Platform environment detected (KEELSON_APP_ID / "
                "KEELSON_WORKSPACE_ID (or deprecated KEELSON_TENANT_ID alias) / "
                "KEELSON_DEPLOY_ID set) but KEELSON_MODE is unset; refusing to "
                "fall back to running tasks locally. Set KEELSON_MODE=local for "
                "local development or KEELSON_MODE=keelson on the platform."
            )
        return None

    raise _not_configured(
        f'Unrecognized KEELSON_MODE="{mode}"; expected "keelson" or "local" '
        "(or unset for local development)."
    )


# ---------------------------------------------------------------------------
# Request validation (both modes)
# ---------------------------------------------------------------------------


def _invalid(message: str) -> TasksError:
    return TasksError("TASK_INVALID_REQUEST", message)


def _check_name(name: object) -> str:
    if not isinstance(name, str) or not name.strip():
        raise _invalid("The task name must be a non-empty string.")
    return name


def _check_idempotency_key(key: object) -> str | None:
    if key is None:
        return None
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= _IDEMPOTENCY_KEY_MAX_CHARS
        or any(not 0x20 <= ord(ch) <= 0x7E for ch in key)
    ):
        raise _invalid(
            "The idempotency key must be 1-128 printable ASCII characters "
            "(U+0020-U+007E)."
        )
    return key


def _dumps(value: Any) -> bytes:
    """Serialize to compact UTF-8 JSON. Never quotes the value in an error."""
    try:
        text = json.dumps(
            value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        return text.encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        # ValueError covers NaN / Infinity, circular references and lone
        # surrogates (UnicodeEncodeError).
        raise _invalid(
            "The task payload cannot be serialized as JSON (NaN, Infinity, "
            "circular references, lone surrogates and non-JSON types are not "
            "allowed)."
        ) from None


def _request_body(payload: Any, idempotency_key: str | None) -> bytes:
    body: dict[str, Any] = {"payload": payload}
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    data = _dumps(body)
    if len(data) > MAX_BODY_BYTES:
        raise TasksError(
            "TASK_PAYLOAD_TOO_LARGE",
            f"The enqueue request body is {len(data)} bytes; the limit is "
            f"{MAX_BODY_BYTES}. Store large data elsewhere (for example in the "
            "database) and pass its ID in the payload.",
        )
    return data


# ---------------------------------------------------------------------------
# Response parsing (both modes)
# ---------------------------------------------------------------------------


def _is_rfc3339(value: object) -> bool:
    if not isinstance(value, str):
        return False
    m = _RFC3339_RE.fullmatch(value)
    if m is None:
        return False
    year, month, day, hour, minute, second, off_h, off_m = m.groups()
    try:
        datetime(int(year), int(month), int(day), int(hour), int(minute), int(second))
    except ValueError:
        return False
    if off_h is not None and (int(off_h) > 23 or int(off_m) > 59):
        return False
    return True


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _parse_status(obj: object) -> TaskStatus | None:
    """Map a § 9.3 object to :class:`TaskStatus`, or ``None`` when invalid.
    Unknown fields are ignored; an unknown ``status`` value is kept."""
    if not isinstance(obj, dict):
        return None
    attempts = obj.get("claimed_attempts")
    last_failure = obj.get("last_failure_code", 0)
    finished = obj.get("finished_at", 0)
    if not (
        _is_nonempty_str(obj.get("task_id"))
        and _is_nonempty_str(obj.get("name"))
        and _is_nonempty_str(obj.get("status"))
        and type(attempts) is int
        and attempts >= 0
        and _is_rfc3339(obj.get("created_at"))
        and (last_failure is None or isinstance(last_failure, str))
        and (finished is None or _is_rfc3339(finished))
    ):
        return None
    return TaskStatus(
        task_id=obj["task_id"],
        name=obj["name"],
        status=obj["status"],
        claimed_attempts=attempts,
        last_failure_code=last_failure,
        created_at=obj["created_at"],
        finished_at=finished,
    )


def _loads(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None


def _unexpected(status: int | None, what: str) -> TasksError:
    return TasksError(
        "TASKS_UNEXPECTED_RESPONSE",
        f"The Tasks API returned an unexpected {what} response.",
        status,
    )


def _parse_get_response(status: int, raw: bytes) -> TaskStatus:
    parsed = _parse_status(_loads(raw))
    if parsed is None:
        raise _unexpected(status, "get")
    return parsed


def _parse_enqueue_response(status: int, raw: bytes) -> str:
    obj = _loads(raw)
    task_id = obj.get("task_id") if isinstance(obj, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise _unexpected(status, "enqueue")
    return task_id


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------

_TRANSIENT_STATUSES = frozenset({502, 503, 504})


def _envelope(raw: bytes) -> tuple[str, str] | None:
    """``(code, message)`` from a ``{"error": {"code": ...}}`` body, or None."""
    obj = _loads(raw)
    if not isinstance(obj, dict):
        return None
    error = obj.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str) or not code:
        return None
    message = error.get("message")
    return code, message if isinstance(message, str) and message else code


def _transient_error(status: int | None, detail: str) -> TasksError:
    return TasksError(
        "TASKS_UNAVAILABLE_TRANSIENT",
        f"The Tasks API is temporarily unreachable ({detail}).",
        status,
    )


def _http_error(status: int, raw: bytes) -> tuple[TasksError, bool]:
    """Classify a non-success response. Returns ``(error, transient)``; the
    order is fixed (T-1037 design 3) and the first hit wins."""
    if status == 401:
        return (
            TasksError(
                "TASKS_UNAUTHORIZED",
                "The Tasks API rejected the identity token (401).",
                status,
            ),
            False,
        )
    if status == 403:
        return (
            TasksError(
                "TASKS_FORBIDDEN",
                "This app's service account may not call the Tasks API (403). "
                "Right after the first deploy that declares tasks:, the "
                "permission can take a few minutes to propagate.",
                status,
            ),
            False,
        )
    envelope = _envelope(raw)
    if envelope is not None:
        code, message = envelope
        return TasksError(code, message, status), False
    if status in _TRANSIENT_STATUSES:
        return _transient_error(status, f"HTTP {status}"), True
    if 500 <= status <= 599:
        return (
            TasksError(
                "TASKS_SERVER_ERROR",
                f"The Tasks API failed with HTTP {status}.",
                status,
            ),
            False,
        )
    return (
        TasksError(
            "TASKS_HTTP_ERROR", f"The Tasks API answered with HTTP {status}.", status
        ),
        False,
    )


# ---------------------------------------------------------------------------
# Remote (runtime API) backend
# ---------------------------------------------------------------------------


class _NoRedirect(HTTPRedirectHandler):
    """Never follow a redirect: it would carry the bearer token elsewhere."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = build_opener(_NoRedirect)


def _open(req: Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _metadata_url() -> str:
    return os.environ.get("KEELSON_TASKS_METADATA_URL", "").strip() or (
        _DEFAULT_METADATA_URL
    )


def _identity_token(audience: str) -> str:
    """Fetch a fresh OIDC id token (never cached) for *audience*."""
    url = f"{_metadata_url()}?audience={quote(audience, safe='')}"
    req = Request(
        url, headers={"Metadata-Flavor": "Google", "User-Agent": _SDK_USER_AGENT}
    )
    try:
        with _open(req, _TIMEOUT) as resp:
            raw = resp.read()
    except HTTPError as exc:
        raise TasksError(
            "TASKS_IDENTITY_TOKEN_ERROR",
            f"The metadata server refused the identity token request (HTTP {exc.code}).",
        ) from None
    except (URLError, OSError, http.client.HTTPException) as exc:
        raise TasksError(
            "TASKS_IDENTITY_TOKEN_ERROR",
            f"Could not reach the metadata server for an identity token: {exc}",
        ) from None
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        token = ""
    if not token or any(ch.isspace() for ch in token):
        raise TasksError(
            "TASKS_IDENTITY_TOKEN_ERROR",
            "The metadata server returned no usable identity token.",
        )
    return token


def _call(
    remote: _Remote,
    method: str,
    path: str,
    *,
    body: bytes | None,
    ok_statuses: frozenset[int],
    retry: bool,
    no_retry_hint: str = "",
) -> tuple[int, bytes]:
    """One runtime API call, retried on transient failures when *retry*."""
    url = f"{remote.api_base}{path}"
    attempts = len(_RETRY_DELAYS) + 1 if retry else 1
    error: TasksError | None = None
    for attempt in range(attempts):
        if attempt:
            _sleep(_RETRY_DELAYS[attempt - 1])
        headers = {
            "Authorization": f"Bearer {_identity_token(remote.audience)}",
            "User-Agent": _SDK_USER_AGENT,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = Request(url, data=body, method=method, headers=headers)
        try:
            with _open(req, _TIMEOUT) as resp:
                status, raw = resp.status, resp.read()
        except HTTPError as exc:
            try:
                status, raw = exc.code, exc.read()
            except (OSError, http.client.HTTPException):
                status, raw = exc.code, b""
        except (URLError, OSError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            error = _transient_error(None, f"{type(exc).__name__}: {reason}")
            continue
        if status in ok_statuses:
            return status, raw
        error, transient = _http_error(status, raw)
        if not transient:
            raise error
    assert error is not None
    if not retry and no_retry_hint:
        error = TasksError(error.code, f"{error.message} {no_retry_hint}", error.status)
    raise error


def _enqueue_remote(
    remote: _Remote, name: str, body: bytes, idempotency_key: str | None
) -> str:
    status, raw = _call(
        remote,
        "POST",
        f"/internal/apps/{quote(remote.app_id, safe='')}/tasks/"
        f"{quote(name, safe='')}/enqueue",
        body=body,
        ok_statuses=frozenset({200, 202}),
        # Without a key, a lost response may hide an accepted task: a resend
        # could enqueue it twice.
        retry=idempotency_key is not None,
        no_retry_hint=(
            "Not retried: without an idempotency key the task may already "
            "have been accepted. Pass an idempotency_key so the SDK can retry "
            "safely."
        ),
    )
    return _parse_enqueue_response(status, raw)


def _get_remote(remote: _Remote, task_id: str) -> TaskStatus:
    status, raw = _call(
        remote,
        "GET",
        f"/internal/apps/{quote(remote.app_id, safe='')}/tasks/"
        f"{quote(task_id, safe='')}",
        body=None,
        ok_statuses=frozenset({200}),
        retry=True,
    )
    return _parse_get_response(status, raw)


# ---------------------------------------------------------------------------
# Local backend — `keelson dev task run` per enqueue, results kept in-process
# ---------------------------------------------------------------------------

_local_lock = threading.Lock()
_local_results: dict[str, TaskStatus] = {}
_local_keys: dict[tuple[str, str], str] = {}


def _reset_local_state() -> None:
    with _local_lock:
        _local_results.clear()
        _local_keys.clear()


def _cli_failed(detail: str) -> TasksError:
    return TasksError(
        "TASKS_LOCAL_CLI_FAILED",
        f"`keelson dev task run` failed: {detail} If the CLI is older than this "
        "SDK, run `keelson upgrade`.",
    )


def _run_local_cli(name: str, payload: bytes) -> TaskStatus:
    cli = shutil.which("keelson")
    not_found = TasksError(
        "TASKS_LOCAL_CLI_NOT_FOUND",
        "Local tasks run through the Keelson CLI, but `keelson` was not found "
        f"on PATH. Install it with `curl -fsSL {_INSTALL_URL} | sh`, or set "
        "KEELSON_MODE=keelson on the platform.",
    )
    if cli is None:
        raise not_found
    argv = [cli, "dev", "task", "run", name, "--payload", "-", "--json"]
    try:
        # stderr is inherited: the task's logs show up in the dev server's
        # terminal. The exit code is not consulted; stdout alone decides.
        proc = subprocess.run(  # noqa: S603
            argv, input=payload, stdout=subprocess.PIPE, check=False
        )
    except FileNotFoundError:
        raise not_found from None
    except OSError as exc:
        raise _cli_failed(f"could not start {cli} ({exc.strerror or exc}).") from None

    obj = _loads(proc.stdout.strip())
    if not isinstance(obj, dict):
        raise _cli_failed(
            f"it exited with code {proc.returncode} without a JSON result on stdout."
        )
    if "task" in obj:
        result = _parse_status(obj["task"])
        if result is None:
            raise _cli_failed("its JSON result is not a valid task.")
        return result
    error = obj.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(error, dict) or not isinstance(code, str) or not code:
        raise _cli_failed("its JSON output has neither a task nor an error code.")
    message = error.get("message")
    hint = error.get("hint")
    detail = " ".join(
        part for part in (message, hint) if isinstance(part, str) and part
    )
    if code in _LOCAL_PASSTHROUGH_CODES:
        raise TasksError(code, detail or code)
    raise _cli_failed(f"{code}: {detail}" if detail else f"{code}.")


def _enqueue_local(name: str, payload: bytes, idempotency_key: str | None) -> str:
    normalized = name.strip().lower()
    if _TASK_NAME_RE.fullmatch(normalized) is None:
        # No declaration can have this name; also keeps a name like "--json"
        # from being read as a CLI flag.
        raise TasksError(
            "TASK_NOT_DECLARED",
            f"Task {normalized!r} is not declared in keelson.yaml (task names "
            "are 1-63 of a-z, 0-9 and '-').",
        )
    key = (normalized, idempotency_key) if idempotency_key is not None else None
    if key is not None:
        with _local_lock:
            existing = _local_keys.get(key)
        if existing is not None:
            return existing
    # The lock is not held while the command runs: tasks may enqueue tasks.
    result = _run_local_cli(name, payload)
    with _local_lock:
        _local_results[result.task_id] = result
        if key is not None:
            _local_keys.setdefault(key, result.task_id)
    return result.task_id


def _get_local(task_id: str) -> TaskStatus:
    with _local_lock:
        result = _local_results.get(task_id)
    if result is None:
        raise TasksError(
            "TASK_NOT_FOUND",
            "No task with this ID was enqueued in this process (local mode keeps "
            "results in memory only).",
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enqueue(name: str, payload: Any = None, idempotency_key: str | None = None) -> str:
    """Enqueue one run of the task *name* declared under ``tasks:`` and return
    its ``task_id``.

    *payload* is any JSON value; the command reads it from stdin. With the
    same *idempotency_key* (1-128 printable ASCII characters), a repeat returns
    the existing ``task_id`` instead of enqueueing again, and the SDK retries
    transient failures. In local mode the command has already finished when
    this returns (its failure is reported by :func:`get`, not raised).
    """
    remote = _resolve_mode()
    name = _check_name(name)
    idempotency_key = _check_idempotency_key(idempotency_key)
    body = _request_body(payload, idempotency_key)
    if remote is not None:
        return _enqueue_remote(remote, name, body, idempotency_key)
    return _enqueue_local(name, _dumps(payload), idempotency_key)


def get(task_id: str) -> TaskStatus:
    """Return the current state of the task *task_id*."""
    remote = _resolve_mode()
    if not isinstance(task_id, str) or not task_id:
        raise _invalid("task_id must be a non-empty string.")
    if remote is not None:
        return _get_remote(remote, task_id)
    return _get_local(task_id)
