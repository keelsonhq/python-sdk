from __future__ import annotations

import io
import json
import mimetypes
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

_CROCKFORD_BASE32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SDK_USER_AGENT = "Keelson-Python-SDK/0.1.0"


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MediaStat:
    """Metadata for a stored file."""

    content_type: str
    content_length: int
    status: int


def _encode_crockford(value: int, *, length: int) -> str:
    encoded = ["0"] * length
    for index in range(length - 1, -1, -1):
        encoded[index] = _CROCKFORD_BASE32[value & 31]
        value >>= 5
    return "".join(encoded)


def _new_ulid() -> str:
    millis = int(time.time() * 1000)
    entropy = secrets.randbits(80)
    return f"{_encode_crockford(millis, length=10)}{_encode_crockford(entropy, length=16)}"


def _normalize_file_id(file_id: str) -> str:
    value = (file_id or "").strip()
    if not value:
        raise ValueError("file_id is required.")
    if value.startswith("http://") or value.startswith("https://"):
        raise ValueError("file_id must not be a URL.")
    value = value.lstrip("/")
    if not value or "/" in value or value in {".", ".."}:
        raise ValueError("file_id is invalid.")
    return value


def _media_dir() -> Path:
    return Path(os.environ.get("MEDIA_DIR", "./media")).resolve()


def _internal_base_url() -> str:
    return os.environ.get("KEELSON_INTERNAL_MEDIA_BASE_URL", "").strip()


def _media_token() -> str:
    return os.environ.get("KEELSON_APP_MEDIA_TOKEN", "").strip()


def _mode() -> str:
    return os.environ.get("KEELSON_MODE", "").strip().lower()


# Platform-owned identifiers whose presence means the app is running on Keelson.
_CORE_IDENTIFIER_ENVS = (
    "KEELSON_APP_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
)


def _is_platform_env() -> bool:
    """True when any platform-owned core identifier is visible (running on Keelson)."""
    return any(os.environ.get(name, "").strip() for name in _CORE_IDENTIFIER_ENVS)


def _resolve_mode() -> str:
    """Resolve the storage mode: ``"remote"`` or ``"local"``.

    Enforces the fail-closed runtime-mode contract so that a Keelson
    deployment never silently writes to ephemeral local storage:

    - ``KEELSON_MODE=keelson`` requires both Media env values; missing env
      raises ``MediaError`` (covers both a config regression and a
      ``files_enabled=false`` deployment).
    - An incomplete remote config (exactly one of base URL / token) raises
      ``MediaError`` regardless of mode.
    - ``KEELSON_MODE=local`` always uses local filesystem storage.
    - When ``KEELSON_MODE`` is unset (local development), remote is used if
      both env values are present, otherwise local — unless a platform
      environment is visible (any of ``KEELSON_APP_ID`` / ``KEELSON_TENANT_ID``
      / ``KEELSON_DEPLOY_ID`` set), in which case the silent local fallback is
      refused.
    - Any other non-empty ``KEELSON_MODE`` is a misconfiguration and raises
      ``MediaError`` (an unknown mode never resolves to local).
    """
    has_base = bool(_internal_base_url())
    has_token = bool(_media_token())
    mode = _mode()

    # Partial remote configuration is always an error, regardless of mode.
    if has_base != has_token:
        missing = "KEELSON_APP_MEDIA_TOKEN" if has_base else "KEELSON_INTERNAL_MEDIA_BASE_URL"
        raise MediaError(
            "Incomplete remote Media configuration: both "
            "KEELSON_INTERNAL_MEDIA_BASE_URL and KEELSON_APP_MEDIA_TOKEN are "
            f"required, but {missing} is missing."
        )

    if mode == "keelson":
        if has_base and has_token:
            return "remote"
        raise MediaError(
            "KEELSON_MODE=keelson but Media is not configured "
            "(KEELSON_INTERNAL_MEDIA_BASE_URL and KEELSON_APP_MEDIA_TOKEN are "
            "unset); the Media capability is unavailable for this deployment."
        )

    if mode == "local":
        # Explicit local development mode: always use the local filesystem.
        return "local"

    if mode == "":
        # KEELSON_MODE unset — local development / backward compatibility.
        if has_base and has_token:
            return "remote"
        if _is_platform_env():
            raise MediaError(
                "Platform environment detected "
                "(KEELSON_APP_ID / KEELSON_TENANT_ID / KEELSON_DEPLOY_ID set) but Media "
                "is not configured; refusing to fall back to local storage. Set "
                "KEELSON_MODE=local for local development."
            )
        return "local"

    # Any other non-empty KEELSON_MODE is a misconfiguration; fail closed
    # rather than treating an unknown mode as local development.
    raise MediaError(
        f'Unrecognized KEELSON_MODE="{mode}"; expected "keelson" or "local" '
        "(or unset for local development)."
    )


def _public_prefix() -> str:
    prefix = os.environ.get("KEELSON_MEDIA_URL_PREFIX", "/media/").strip()
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if not prefix.endswith("/"):
        prefix += "/"
    return prefix


def _internal_url(file_id: str) -> str:
    base = _internal_base_url().rstrip("/")
    return f"{base}/__keelson/internal/files/{quote(file_id, safe='')}"


def _internal_request(
    method: str,
    file_id: str,
    *,
    data: bytes | None = None,
    content_type: str | None = None,
    allow_not_found: bool = False,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    url = _internal_url(file_id)
    headers = {
        "Authorization": f"Bearer {_media_token()}",
        "User-Agent": _SDK_USER_AGENT,
    }
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req) as resp:  # noqa: S310
            body = resp.read() if method != "HEAD" else b""
            return resp.status, body
    except HTTPError as exc:
        if allow_not_found and exc.code == 404:
            return exc.code, b""
        body = exc.read().decode("utf-8", errors="replace")
        raise MediaError(f"{method} {url} failed with {exc.code}: {body}") from exc
    except URLError as exc:
        raise MediaError(f"{method} {url} failed: {exc}") from exc


def _internal_head(file_id: str) -> tuple[int, dict[str, str]]:
    """Send HEAD request and return (status, headers dict)."""
    url = _internal_url(file_id)
    headers = {
        "Authorization": f"Bearer {_media_token()}",
        "User-Agent": _SDK_USER_AGENT,
    }
    req = Request(url, method="HEAD", headers=headers)
    try:
        with urlopen(req) as resp:  # noqa: S310
            return resp.status, dict(resp.headers)
    except HTTPError as exc:
        if exc.code == 404:
            raise MediaError(f"File not found: {file_id}") from exc
        body = exc.read().decode("utf-8", errors="replace")
        raise MediaError(f"HEAD {url} failed with {exc.code}: {body}") from exc
    except URLError as exc:
        raise MediaError(f"HEAD {url} failed: {exc}") from exc


def stat(file_id: str) -> MediaStat:
    """Return metadata for a file without downloading its content.

    Raises ``MediaError`` if the file does not exist.
    """
    normalized_file_id = _normalize_file_id(file_id)
    if _resolve_mode() == "remote":
        status, headers = _internal_head(normalized_file_id)
        content_type = headers.get("Content-Type", "application/octet-stream")
        # Strip charset or parameters (e.g. "text/plain; charset=utf-8" → "text/plain")
        if ";" in content_type:
            content_type = content_type.split(";", 1)[0].strip()
        try:
            content_length = int(headers.get("Content-Length", "0"))
        except (ValueError, TypeError):
            content_length = 0
        return MediaStat(
            content_type=content_type,
            content_length=content_length,
            status=status,
        )

    path = _media_dir() / normalized_file_id
    if not path.exists():
        raise MediaError(f"File not found: {normalized_file_id}")
    content_type = "application/octet-stream"
    meta_path = _media_dir() / f"{normalized_file_id}.meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            content_type = meta.get("content_type", content_type)
        except (json.JSONDecodeError, OSError):
            pass
    return MediaStat(
        content_type=content_type,
        content_length=path.stat().st_size,
        status=200,
    )


def put(
    data: bytes,
    *,
    content_type: str | None = None,
    filename: str | None = None,
) -> str:
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise TypeError("data must be bytes-like.")

    file_id = _new_ulid()
    guessed_type = mimetypes.guess_type(filename or "")[0] if filename else None
    effective_type = content_type or guessed_type or "application/octet-stream"
    if _resolve_mode() == "remote":
        headers = {}
        if filename:
            headers["X-Keelson-Filename"] = filename
        _internal_request(
            "PUT",
            file_id,
            data=data,
            content_type=effective_type,
            extra_headers=headers,
        )
        _internal_request("HEAD", file_id)
        return file_id

    target = _media_dir() / file_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    meta_path = _media_dir() / f"{file_id}.meta.json"
    meta_path.write_text(
        json.dumps({"content_type": effective_type}),
        encoding="utf-8",
    )
    return file_id


def get(file_id: str) -> bytes:
    normalized_file_id = _normalize_file_id(file_id)
    if _resolve_mode() == "remote":
        _, body = _internal_request("GET", normalized_file_id)
        return body
    return (_media_dir() / normalized_file_id).read_bytes()


def open(file_id: str) -> BinaryIO:
    normalized_file_id = _normalize_file_id(file_id)
    if _resolve_mode() == "remote":
        return io.BytesIO(get(normalized_file_id))
    return (_media_dir() / normalized_file_id).open("rb")


def delete(file_id: str) -> None:
    normalized_file_id = _normalize_file_id(file_id)
    if _resolve_mode() == "remote":
        _internal_request("DELETE", normalized_file_id, allow_not_found=True)
        return
    target = _media_dir() / normalized_file_id
    if target.exists():
        target.unlink()
    meta_path = _media_dir() / f"{normalized_file_id}.meta.json"
    if meta_path.exists():
        meta_path.unlink()


def exists(file_id: str) -> bool:
    normalized_file_id = _normalize_file_id(file_id)
    if _resolve_mode() == "remote":
        status, _ = _internal_request("HEAD", normalized_file_id, allow_not_found=True)
        return status == 200
    return (_media_dir() / normalized_file_id).exists()


def url(file_id: str) -> str:
    # Resolve the runtime mode first so a misconfigured / capability-unavailable
    # Keelson deployment fails closed here too, matching the Go client (whose
    # constructor rejects) and the rest of the Media API. In explicit local mode
    # and normal remote mode it returns the path as usual.
    _resolve_mode()
    normalized_file_id = _normalize_file_id(file_id)
    return f"{_public_prefix()}{quote(normalized_file_id, safe='')}"
