"""Keelson Email SDK.

Provides ``on_receive`` decorator for inbound email handling and
``send()`` for outbound email.  The SDK automatically reads
``KEELSON_EMAIL_BASE_URL``, ``KEELSON_EMAIL_API_URL``, and
``KEELSON_EMAIL_TOKEN`` from the environment (injected by Keelson at deploy
time). The gateway base URL is preferred when it is available.

Minimal example::

    from keelson import email

    @email.on_receive
    def handle(msg: email.InboundMessage):
        print(msg.subject, msg.text)
        email.send(
            to=msg.reply_to or msg.from_,
            subject=f"Re: {msg.subject}",
            text="Got it!",
            in_reply_to=msg.provider_message_id,
        )

The ``@email.on_receive`` decorator immediately starts a webhook
server in a background non-daemon thread, keeping the process alive
as long as the server is running.  No explicit call to ``serve()``
is needed.

Both ``from keelson import email`` and ``import keelson_email as email``
are supported.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import hashlib
import inspect
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

# Explicit UA: urllib's default ``Python-urllib/3.x`` is blocked by Cloudflare
# Browser Integrity Check (Error 1010 browser_signature_banned) on the
# ``*.keelson.run`` / ``*.keelson-stage.run`` zones. See T-0595.
_SDK_USER_AGENT = "Keelson-Python-SDK/0.1.1"

_WEBHOOK_PATH = "/api/webhooks/email"
_EVENT_WEBHOOK_PATH = "/api/webhooks/email-events"
_DEFAULT_PORT = 8000
_WEBHOOK_TOLERANCE_SECONDS = 300


class EmailError(RuntimeError):
    """Raised when an Email SDK operation fails."""

    pass


# ---------------------------------------------------------------------------
# Config helpers (read from environment)
# ---------------------------------------------------------------------------


def _api_url() -> str:
    url = os.environ.get("KEELSON_EMAIL_API_URL", "").strip().rstrip("/")
    if not url:
        raise EmailError(
            "KEELSON_EMAIL_API_URL is not set. "
            "Ensure the app is deployed on Keelson with email enabled."
        )
    return url


def _gateway_base_url() -> str | None:
    url = os.environ.get("KEELSON_EMAIL_BASE_URL", "").strip().rstrip("/")
    return url or None


def _send_url() -> str:
    if base_url := _gateway_base_url():
        return f"{base_url}/__keelson/email/send"
    return f"{_api_url()}/v1/email/send"


def _token() -> str:
    token = os.environ.get("KEELSON_EMAIL_TOKEN", "").strip()
    if not token:
        raise EmailError(
            "KEELSON_EMAIL_TOKEN is not set. "
            "Ensure the app is deployed on Keelson with email enabled."
        )
    return token


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "User-Agent": _SDK_USER_AGENT,
    }


def _webhook_secret() -> str | None:
    secret = os.environ.get("KEELSON_EMAIL_WEBHOOK_SECRET", "").strip()
    return secret or None


def _mode() -> str:
    """Normalized ``KEELSON_MODE`` (lower-cased, trimmed); ``""`` when unset."""
    return os.environ.get("KEELSON_MODE", "").strip().lower()


# Platform-owned identifiers whose presence means the app is running on Keelson.
# This matches the Media SDK's platform detection.
_CORE_IDENTIFIER_ENVS = (
    "KEELSON_APP_ID",
    "KEELSON_WORKSPACE_ID",
    "KEELSON_TENANT_ID",
    "KEELSON_DEPLOY_ID",
)


def _is_platform_env() -> bool:
    """True when any platform-owned core identifier is visible (on Keelson)."""
    return any(os.environ.get(name, "").strip() for name in _CORE_IDENTIFIER_ENVS)


# Svix timestamp verification bounds replays to a ±5 minute window but does not
# reject a duplicate within it. The dispatch site
# runs a token-fenced three-state machine per ``svix-id``:
#   reserve → handler → commit(token)   (on success)
#   reserve → handler → release(token)  (on failure)
# ``reserve`` returns one of THREE states (never collapsing pending into completed):
#   * "acquired"  — a fresh reservation (nothing prior, or the prior pending lease
#     expired = crash takeover); process, then commit/release. Carries a ``token``
#     (generation) that fences commit/release.
#   * "pending"   — another attempt holds an un-expired reservation → the caller
#     returns a RETRYABLE 5xx (never a 200 duplicate), so the platform keeps
#     retrying until the in-flight attempt completes (dedup) or its lease expires
#     (re-acquire). This is what makes crash recovery actually work.
#   * "completed" — already handled → ACK 200 duplicate.
# ``commit``/``release`` take the token and are compare-and-set: a stale attempt
# (whose lease expired and was taken over) can NOT commit/release the newer
# reservation — a token mismatch is a no-op.
#
# Two backends:
#   * Default (process-local): bounded, TTL'd in-memory state machine. Best-effort
#     — a crash wipes it (so a post-crash retry re-processes), not cross-instance.
#   * Durable (pluggable): :func:`set_idempotency_store` backed by the app's DB.
#     ``reserve`` must be atomic, return the 3 states with a fencing token, expire a
#     stale pending, and RAISE on a backend error (retryable 500, never dropped).
class IdempotencyStore(Protocol):
    """Durable idempotency store (token-fenced reserve/commit/release)."""

    def reserve(self, delivery_id: str) -> tuple[str, str]:
        """Return ``(status, token)``: status in ``{"acquired","pending","completed"}``;
        ``token`` identifies the reservation for CAS commit/release (only meaningful
        for ``"acquired"``). Raise on a backend error."""
        ...

    def commit(self, delivery_id: str, token: str) -> None:
        """Promote to COMPLETED iff ``token`` still owns the reservation (else no-op)."""

    def release(self, delivery_id: str, token: str) -> None:
        """Remove the reservation iff ``token`` still owns it (else no-op)."""


_COMPLETED_TTL_SECONDS = 300
_PENDING_LEASE_SECONDS = 300
_MAX_DEDUP_ENTRIES = 10_000
# id -> (state, expiry, token): state in {"pending", "completed"}.
_seen_deliveries: dict[str, tuple[str, float, str]] = {}
_token_counter = 0
_idempotency_store: Any = None


def set_idempotency_store(store: Any) -> None:
    """Install a durable idempotency store (or ``None`` for the process-local
    backend). ``store`` must expose ``reserve(id) -> (status, token)`` /
    ``commit(id, token)`` / ``release(id, token)`` (the token-fenced 3-state
    contract). Call once at startup, before registering a handler. The webhook
    server is single-threaded, so these may be plain (sync) DB calls.
    """
    global _idempotency_store
    _idempotency_store = store


def _reserve_delivery(delivery_id: str, now: float | None = None) -> tuple[str, str]:
    """Resolve the reservation state; return ``(status, token)``.

    Delegates to the installed durable store when set (may raise on a backend
    error — the caller turns that into a retryable 500), else the process-local
    backend. Single-threaded server ⇒ no interleaving.
    """
    if _idempotency_store is not None:
        return _idempotency_store.reserve(delivery_id)
    now = time.time() if now is None else now
    # Prune expired entries (insertion order ≈ expiry order for fixed TTLs).
    for key in list(_seen_deliveries):
        if _seen_deliveries[key][1] > now:
            break
        del _seen_deliveries[key]
    existing = _seen_deliveries.get(delivery_id)
    if existing is not None and existing[1] > now:
        return ("completed" if existing[0] == "completed" else "pending", "")
    global _token_counter
    _token_counter += 1
    token = str(_token_counter)
    _seen_deliveries.pop(delivery_id, None)
    _seen_deliveries[delivery_id] = ("pending", now + _PENDING_LEASE_SECONDS, token)
    if len(_seen_deliveries) > _MAX_DEDUP_ENTRIES:
        oldest = next(iter(_seen_deliveries))
        del _seen_deliveries[oldest]
    return ("acquired", token)


def _commit_delivery(delivery_id: str, token: str, now: float | None = None) -> None:
    """Promote a reservation to COMPLETED (CAS on ``token``)."""
    if _idempotency_store is not None:
        _idempotency_store.commit(delivery_id, token)
        return
    existing = _seen_deliveries.get(delivery_id)
    if existing is None or existing[2] != token:
        return  # CAS mismatch (stale attempt) → no-op
    now = time.time() if now is None else now
    _seen_deliveries[delivery_id] = ("completed", now + _COMPLETED_TTL_SECONDS, token)


def _release_delivery(delivery_id: str, token: str) -> None:
    """Release a reservation (CAS on ``token``) so a failed delivery's retry re-processes."""
    if _idempotency_store is not None:
        _idempotency_store.release(delivery_id, token)
        return
    existing = _seen_deliveries.get(delivery_id)
    if existing is None or existing[2] != token:
        return  # CAS mismatch → no-op
    _seen_deliveries.pop(delivery_id, None)


def _webhook_signature_required() -> bool:
    """Return whether inbound and event deliveries require a valid signature.

    Fail-closed on Keelson, mirroring the Media runtime-mode contract:

    - ``KEELSON_MODE=keelson`` → required.
    - ``KEELSON_MODE=local`` → not required (local development accepts unsigned).
    - ``KEELSON_MODE`` unset but a platform environment is detected
      (``KEELSON_APP_ID`` / ``KEELSON_WORKSPACE_ID`` / ``KEELSON_DEPLOY_ID``;
      the former ``KEELSON_TENANT_ID`` name remains a deprecated alias) → required,
      so a misconfigured platform deploy never silently accepts unsigned.
    - ``KEELSON_MODE`` unset and no platform env → not required (zero-config dev).
    - Any other non-empty mode → required (fail closed on an unrecognized mode).
    """
    mode = _mode()
    if mode == "keelson":
        return True
    if mode == "local":
        return False
    if mode == "":
        return _is_platform_env()
    return True


# ---------------------------------------------------------------------------
# Data classes — Inbound message
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Address:
    name: str
    address: str


@dataclass(frozen=True)
class AuthenticationResult:
    spf: str | None = None
    dkim: str | None = None
    dmarc: str | None = None


@dataclass(frozen=True)
class SpamAssessment:
    """Spam score and verdict from Keelson's inbound spam filter."""

    score: float = 0.0
    verdict: str = "clean"  # "clean", "suspicious", "spam"
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class InboundAttachment:
    """Metadata for an inbound email attachment.

    Call :meth:`download` to fetch the binary content from the
    Keelson Email API.
    """

    id: str
    filename: str
    content_type: str
    size_bytes: int
    download_url: str

    def download(self, *, timeout_sec: float = 30.0) -> bytes:
        """Download the attachment content as bytes."""
        if base_url := _gateway_base_url():
            url = f"{base_url}/__keelson/email/attachments/{self.id}"
        else:
            url = urljoin(_api_url() + "/", self.download_url.lstrip("/"))
        headers = {**_auth_headers(), "Accept": "application/octet-stream"}
        req = Request(url, method="GET", headers=headers)
        try:
            with urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
                return resp.read()
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EmailError(
                f"Attachment download failed ({exc.code}): {body}"
            ) from exc
        except URLError as exc:
            raise EmailError(f"Attachment download failed: {exc}") from exc


@dataclass(frozen=True)
class InboundMessage:
    """Parsed inbound email delivered via Keelson webhook."""

    delivery_id: str
    attempt: int
    received_at: str
    sent_at: str | None
    from_: Address
    to: list[Address]
    cc: list[Address]
    reply_to: Address | None
    subject: str
    text: str | None
    html: str | None
    provider_message_id: str | None
    in_reply_to: str | None
    references: list[str]
    envelope_to: str
    authentication: AuthenticationResult
    spam: SpamAssessment
    attachments: list[InboundAttachment]

    @classmethod
    def from_webhook_payload(cls, data: dict[str, Any]) -> InboundMessage:
        """Parse a webhook JSON payload into an ``InboundMessage``."""
        from_raw = data.get("from", {})
        from_addr = Address(
            name=from_raw.get("name", ""),
            address=from_raw.get("address", ""),
        )

        to_list = [
            Address(name=t.get("name", ""), address=t.get("address", ""))
            for t in data.get("to", [])
        ]
        cc_list = [
            Address(name=c.get("name", ""), address=c.get("address", ""))
            for c in data.get("cc", [])
        ]

        reply_to_raw = data.get("reply_to")
        reply_to = None
        if reply_to_raw and isinstance(reply_to_raw, dict):
            reply_to = Address(
                name=reply_to_raw.get("name", ""),
                address=reply_to_raw.get("address", ""),
            )

        auth_raw = data.get("authentication", {})
        authentication = AuthenticationResult(
            spf=auth_raw.get("spf"),
            dkim=auth_raw.get("dkim"),
            dmarc=auth_raw.get("dmarc"),
        )

        attachments = [
            InboundAttachment(
                id=a["id"],
                filename=a["filename"],
                content_type=a["content_type"],
                size_bytes=a["size_bytes"],
                download_url=a["download_url"],
            )
            for a in data.get("attachments", [])
        ]

        spam_raw = data.get("spam", {})
        spam = SpamAssessment(
            score=spam_raw.get("score", 0.0),
            verdict=spam_raw.get("verdict", "clean"),
            reasons=spam_raw.get("reasons", []),
        )

        return cls(
            delivery_id=data.get("delivery_id", ""),
            attempt=data.get("attempt", 1),
            received_at=data.get("received_at", ""),
            sent_at=data.get("sent_at"),
            from_=from_addr,
            to=to_list,
            cc=cc_list,
            reply_to=reply_to,
            subject=data.get("subject", ""),
            text=data.get("text"),
            html=data.get("html"),
            provider_message_id=data.get("provider_message_id"),
            in_reply_to=data.get("in_reply_to"),
            references=data.get("references", []),
            envelope_to=data.get("envelope_to", ""),
            authentication=authentication,
            spam=spam,
            attachments=attachments,
        )


@dataclass(frozen=True)
class EmailEventPayload:
    """Bounce/complaint/delivery event from Keelson.

    ``resend_email_id`` is deprecated; use ``send_id`` to correlate an event
    with a send.
    """

    event_id: str
    event_type: str  # "bounce", "complaint", "delivered"
    email_address: str
    resend_email_id: str | None
    bounce_type: str | None
    detail: str | None
    timestamp: str
    provider: str | None = None
    send_id: str | None = None

    @classmethod
    def from_webhook_payload(cls, data: dict[str, Any]) -> EmailEventPayload:
        return cls(
            event_id=data.get("event_id", ""),
            event_type=data.get("event_type", ""),
            email_address=data.get("email_address", ""),
            provider=data.get("provider"),
            send_id=data.get("send_id"),
            resend_email_id=data.get("resend_email_id"),
            bounce_type=data.get("bounce_type"),
            detail=data.get("detail"),
            timestamp=data.get("timestamp", ""),
        )


def _coerce_body_bytes(body: bytes | str) -> bytes:
    if isinstance(body, bytes):
        return body
    return body.encode("utf-8")


def _header_value(headers: Mapping[str, Any], name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    if value is None:
        return ""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value)


def _decode_webhook_secret(secret: str) -> bytes:
    trimmed = (secret or "").strip()
    if not trimmed:
        raise EmailError("secret is required")
    if not trimmed.startswith("whsec_"):
        raise EmailError("secret must start with whsec_")
    try:
        decoded = base64.b64decode(trimmed[len("whsec_") :], validate=True)
    except binascii.Error as exc:
        raise EmailError("secret is not valid base64") from exc
    if not decoded:
        raise EmailError("secret is not valid base64")
    return decoded


def _verify_svix_signature(
    body: bytes,
    headers: Mapping[str, Any],
    secret: str,
    *,
    fn_name: str,
) -> None:
    msg_id = _header_value(headers, "svix-id")
    timestamp = _header_value(headers, "svix-timestamp")
    signature = _header_value(headers, "svix-signature")
    if not msg_id or not timestamp or not signature:
        raise EmailError(f"{fn_name}: missing svix headers")

    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise EmailError(f"{fn_name}: invalid svix timestamp") from exc

    now = int(time.time())
    if abs(now - timestamp_int) > _WEBHOOK_TOLERANCE_SECONDS:
        raise EmailError(f"{fn_name}: timestamp outside tolerance")

    key = _decode_webhook_secret(secret)
    signed = msg_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + body
    expected = hmac.new(key, signed, hashlib.sha256).digest()

    for entry in signature.split():
        version, _, encoded = entry.partition(",")
        if version != "v1" or not encoded:
            continue
        try:
            actual = base64.b64decode(encoded, validate=True)
        except binascii.Error:
            continue
        if len(actual) == len(expected) and hmac.compare_digest(actual, expected):
            return

    raise EmailError(f"{fn_name}: no matching signature found")


def verify_webhook(body: bytes | str, headers: Mapping[str, Any], secret: str) -> InboundMessage:
    return verify_webhook_bytes(_coerce_body_bytes(body), headers, secret)


def verify_webhook_bytes(
    body: bytes,
    headers: Mapping[str, Any],
    secret: str,
) -> InboundMessage:
    _verify_svix_signature(body, headers, secret, fn_name="verify_webhook_bytes")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmailError("verify_webhook_bytes: decode payload: invalid JSON") from exc
    return InboundMessage.from_webhook_payload(data)


def verify_event_webhook(
    body: bytes | str,
    headers: Mapping[str, Any],
    secret: str,
) -> EmailEventPayload:
    return verify_event_webhook_bytes(_coerce_body_bytes(body), headers, secret)


def verify_event_webhook_bytes(
    body: bytes,
    headers: Mapping[str, Any],
    secret: str,
) -> EmailEventPayload:
    _verify_svix_signature(body, headers, secret, fn_name="verify_event_webhook_bytes")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise EmailError(
            "verify_event_webhook_bytes: decode payload: invalid JSON"
        ) from exc
    return EmailEventPayload.from_webhook_payload(data)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------


@dataclass
class Attachment:
    """Outbound email attachment."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


def _to_address_str(value: str | Address) -> str:
    """Coerce an ``Address`` or plain string to a plain email string."""
    if isinstance(value, Address):
        return value.address
    return value


def _to_address_list(value: str | Address | list[str | Address]) -> list[str]:
    """Normalise a single or list of addresses into a list of strings."""
    if isinstance(value, (str, Address)):
        return [_to_address_str(value)]
    return [_to_address_str(v) for v in value]


def send(
    *,
    to: str | Address | list[str | Address],
    subject: str,
    text: str | None = None,
    html: str | None = None,
    cc: str | Address | list[str | Address] | None = None,
    bcc: str | Address | list[str | Address] | None = None,
    from_address: str | None = None,
    from_name: str | None = None,
    reply_to: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[Attachment] | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    """Send an email via the Keelson Email API.

    ``to``, ``cc``, and ``bcc`` accept :class:`Address` objects (as
    returned in ``InboundMessage.from_``, ``.reply_to``, etc.) in
    addition to plain email strings, so you can write::

        email.send(to=msg.reply_to or msg.from_, ...)

    Returns the API response dict (contains ``send_id`` and ``status``).
    """
    to_list = _to_address_list(to)

    payload: dict[str, Any] = {
        "to": to_list,
        "subject": subject,
    }
    if text is not None:
        payload["text"] = text
    if html is not None:
        payload["html"] = html
    if cc is not None:
        payload["cc"] = _to_address_list(cc)
    if bcc is not None:
        payload["bcc"] = _to_address_list(bcc)
    if from_address is not None:
        payload["from"] = from_address
    if from_name is not None:
        payload["from_name"] = from_name
    if reply_to is not None:
        payload["reply_to"] = reply_to
    if in_reply_to is not None:
        payload["in_reply_to"] = in_reply_to
    if references is not None:
        payload["references"] = references
    if attachments:
        payload["attachments"] = [
            {
                "filename": att.filename,
                "content": base64.b64encode(att.content).decode("ascii"),
                "content_type": att.content_type,
            }
            for att in attachments
        ]

    url = _send_url()
    body = json.dumps(payload).encode("utf-8")
    headers = {
        **_auth_headers(),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    req = Request(url, data=body, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            error_body = json.loads(raw)
            error_detail = error_body.get("error", {})
            code = error_detail.get("code", "UNKNOWN")
            message = error_detail.get("message", raw)
        except (json.JSONDecodeError, AttributeError):
            code = f"HTTP_{exc.code}"
            message = raw
        raise EmailError(f"Send failed [{code}]: {message}") from exc
    except URLError as exc:
        raise EmailError(f"Send failed: {exc}") from exc


# ---------------------------------------------------------------------------
# on_receive — decorator + webhook server
# ---------------------------------------------------------------------------

_receive_handler: Callable[[InboundMessage], Any] | None = None
_event_handler: Callable[[EmailEventPayload], Any] | None = None
_auto_serve_started = False
_auto_serve_server: HTTPServer | None = None


def on_receive(fn: Callable[[InboundMessage], Any]) -> Callable[[InboundMessage], Any]:
    """Decorator that registers *fn* as the inbound email handler.

    The first time a handler is registered the SDK immediately starts
    a webhook HTTP server in a background daemon thread.  This means
    writing ``@email.on_receive`` is sufficient — no explicit call to
    ``serve()`` is needed.

    The server listens on ``PORT`` (default 8000) at
    ``POST /api/webhooks/email``.
    """
    global _receive_handler, _auto_serve_started, _auto_serve_server
    if inspect.iscoroutinefunction(fn):
        raise TypeError(
            "on_receive handler must be a synchronous function, "
            "got async function. The webhook server runs in a "
            "synchronous thread and cannot await coroutines."
        )
    _receive_handler = fn

    if not _auto_serve_started:
        _auto_serve_started = True
        _auto_serve_server = serve(blocking=False)

    return fn


def on_event(fn: Callable[[EmailEventPayload], Any]) -> Callable[[EmailEventPayload], Any]:
    """Register a handler for bounce/complaint/delivery events.

    Like ``on_receive``, the first registration automatically starts
    the webhook server if it is not already running.
    """
    global _event_handler, _auto_serve_started, _auto_serve_server
    if inspect.iscoroutinefunction(fn):
        raise TypeError(
            "on_event handler must be a synchronous function, "
            "got async function."
        )
    _event_handler = fn
    if not _auto_serve_started:
        _auto_serve_started = True
        _auto_serve_server = serve(blocking=False)
    return fn


class _WebhookHandler(BaseHTTPRequestHandler):
    """HTTP handler for the inbound email webhook."""

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in (_WEBHOOK_PATH, _EVENT_WEBHOOK_PATH):
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        secret = _webhook_secret()
        if _webhook_signature_required() and not secret:
            # Fail closed: Keelson mode or a visible platform environment without
            # an injected signing secret refuses unverifiable payloads. Return 500
            # so the platform retries; the payload is neither processed nor
            # acknowledged.
            self.send_response(500)
            self.end_headers()
            self.wfile.write(
                b'{"error": "KEELSON_EMAIL_WEBHOOK_SECRET is not configured; '
                b'refusing to accept an unsigned webhook in Keelson mode"}'
            )
            return
        try:
            # svix-id is present only on signed deliveries and is the stable
            # deduplication key. Reservation happens inside the handlers after
            # the handler-registered check, so a delivery with no handler (500,
            # retryable) is never reserved — its retry must be processed, not
            # deduped as an already-handled success.
            svix_id = _header_value(self.headers, "svix-id") if secret else ""
            if self.path == _EVENT_WEBHOOK_PATH:
                data: dict[str, Any] | EmailEventPayload
                if secret:
                    data = verify_event_webhook_bytes(raw_body, self.headers, secret)
                else:
                    data = json.loads(raw_body)
                self._handle_event(data, svix_id=svix_id)
            else:
                inbound: dict[str, Any] | InboundMessage
                if secret:
                    inbound = verify_webhook_bytes(raw_body, self.headers, secret)
                else:
                    inbound = json.loads(raw_body)
                self._handle_receive(inbound, svix_id=svix_id)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid JSON"}')
        except EmailError as exc:
            if "decode payload" in str(exc):
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Invalid JSON"}')
                return
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid signature"}')

    def _ack_duplicate(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok", "duplicate": true}')

    def _reserve_or_respond(self, svix_id: str) -> str | None:
        """Resolve the reservation. Return a token to proceed (``""`` when unsigned,
        i.e. no reservation), or ``None`` when the response is already sent.

        - ``completed`` → ACK 200 duplicate, return ``None``.
        - ``pending``   → an un-expired reservation is in flight; return a
          **retryable 503** (never a 200 duplicate — that would falsely mark the
          delivery done and break crash recovery), return ``None``.
        - store backend error → 500 (retryable), return ``None``.
        - ``acquired``  → return the fencing token.
        """
        if not svix_id:
            return ""
        try:
            status, token = _reserve_delivery(svix_id)
        except Exception:
            logger.exception("Idempotency store reserve failed")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Idempotency store error"}')
            return None
        if status == "completed":
            self._ack_duplicate()
            return None
        if status == "pending":
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"error": "Delivery already in progress"}')
            return None
        return token

    def _finish_delivery(self, svix_id: str, token: str) -> bool:
        """Commit the reservation (CAS on ``token``) after a successful handler.

        Returns ``True`` when the completed record is durable, ``False`` on a commit
        backend failure. Fail CLOSED: the caller returns a retryable 500 on ``False``
        rather than 200, so the platform does not mark a delivery ``delivered`` with
        no completed reservation (a replay would otherwise re-acquire and
        double-process). The platform retries until a commit succeeds.
        """
        if not svix_id or not token:
            return True
        try:
            _commit_delivery(svix_id, token)
        except Exception:
            logger.exception("Idempotency store commit failed")
            return False
        return True

    def _release_after_failure(self, svix_id: str, token: str) -> None:
        if not svix_id or not token:
            return
        try:
            _release_delivery(svix_id, token)
        except Exception:
            logger.exception("Idempotency store release failed")

    def _handle_receive(
        self, data: dict[str, Any] | InboundMessage, *, svix_id: str = ""
    ) -> None:
        if _receive_handler is None:
            # No handler → 500 (retryable). Reserve NOTHING: the platform retries
            # with the same svix-id, and that retry must be processed once a handler
            # exists, not deduped as an already-handled success.
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "No handler registered"}')
            return

        # reserve → handler → commit(token) (release on failure).
        token = self._reserve_or_respond(svix_id)
        if token is None:
            return
        try:
            msg = (
                data
                if isinstance(data, InboundMessage)
                else InboundMessage.from_webhook_payload(data)
            )
            _receive_handler(msg)
        except Exception:
            logger.exception("Error in email receive handler")
            self._release_after_failure(svix_id, token)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Handler error"}')
            return
        if not self._finish_delivery(svix_id, token):
            # Handler succeeded but commit failed → fail closed (retryable 500).
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Idempotency commit failed"}')
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def _handle_event(
        self, data: dict[str, Any] | EmailEventPayload, *, svix_id: str = ""
    ) -> None:
        if _event_handler is None:
            # ACK silently when no handler is registered — events are droppable by
            # design, so no reservation is taken.
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')
            return

        token = self._reserve_or_respond(svix_id)
        if token is None:
            return
        try:
            event = (
                data
                if isinstance(data, EmailEventPayload)
                else EmailEventPayload.from_webhook_payload(data)
            )
            _event_handler(event)
        except Exception:
            logger.exception("Error in email event handler")
            self._release_after_failure(svix_id, token)
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Handler error"}')
            return
        if not self._finish_delivery(svix_id, token):
            # Handler succeeded but commit failed → fail closed (retryable 500).
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "Idempotency commit failed"}')
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def do_GET(self) -> None:  # noqa: N802
        # Health check endpoint.
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)


def serve(*, port: int | None = None, blocking: bool = True) -> HTTPServer | None:
    """Start the webhook server.

    If *blocking* is ``True`` (default), blocks the calling thread.
    If *blocking* is ``False``, starts in a daemon thread and returns
    the server instance.
    """
    resolved_port = port or int(os.environ.get("PORT", str(_DEFAULT_PORT)))
    server = HTTPServer(("0.0.0.0", resolved_port), _WebhookHandler)  # noqa: S104
    logger.info("Keelson email webhook server listening on port %d", resolved_port)
    if blocking:
        server.serve_forever()
        return None
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    return server
