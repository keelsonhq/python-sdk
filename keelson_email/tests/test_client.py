"""Tests for keelson_email SDK client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from threading import Thread
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from keelson_email.client import (
    Address,
    Attachment,
    EmailError,
    EmailEventPayload,
    InboundAttachment,
    InboundMessage,
    _receive_handler,
    _to_address_list,
    _to_address_str,
    on_event,
    on_receive,
    send,
    serve,
    verify_event_webhook_bytes,
    verify_webhook_bytes,
)

# Deterministic fake secret for tests.
TEST_WEBHOOK_SECRET = "whsec_" + base64.b64encode(
    b"fake-test-webhook-key!!"
).decode("ascii")


def _svix_headers(payload: dict, *, secret: str = TEST_WEBHOOK_SECRET) -> dict[str, str]:
    body = json.dumps(payload).encode("utf-8")
    msg_id = "msg_test_123"
    timestamp = str(int(time.time()))
    key = base64.b64decode(secret[len("whsec_") :])
    signature = base64.b64encode(
        hmac.new(
            key,
            b".".join([msg_id.encode("utf-8"), timestamp.encode("utf-8"), body]),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{signature}",
    }


def _payload(delivery_id: str) -> dict:
    return {
        "delivery_id": delivery_id,
        "attempt": 1,
        "received_at": "2026-04-01T00:00:00Z",
        "from": {"name": "A", "address": "a@b.com"},
        "subject": "x",
    }


class _DurableStore:
    """A durable idempotency store modelling the token-fenced 3-state machine.

    ``now`` is injectable so tests can expire the pending lease deterministically.
    """

    def __init__(self, lease: float = 300.0) -> None:
        # id -> (state, expiry, token)
        self.state: dict[str, tuple[str, float, str]] = {}
        self._lease = lease
        self._counter = 0
        self.now = 1_000_000.0

    def reserve(self, delivery_id: str) -> tuple[str, str]:
        e = self.state.get(delivery_id)
        if e is not None and e[1] > self.now:
            return ("completed" if e[0] == "completed" else "pending", "")
        self._counter += 1
        token = str(self._counter)
        self.state[delivery_id] = ("pending", self.now + self._lease, token)
        return ("acquired", token)

    def commit(self, delivery_id: str, token: str) -> None:
        e = self.state.get(delivery_id)
        if e is None or e[2] != token:  # CAS mismatch → no-op
            return
        self.state[delivery_id] = ("completed", self.now + 300.0, token)

    def release(self, delivery_id: str, token: str) -> None:
        e = self.state.get(delivery_id)
        if e is None or e[2] != token:  # CAS mismatch → no-op
            return
        self.state.pop(delivery_id, None)


@pytest.fixture(autouse=True)
def _reset_dedup():
    """Clear the per-process svix-id dedup store + any installed store (module state)."""
    import keelson_email.client as _mod

    _mod._seen_deliveries.clear()
    _mod._token_counter = 0
    _mod.set_idempotency_store(None)
    yield
    _mod._seen_deliveries.clear()
    _mod._token_counter = 0
    _mod.set_idempotency_store(None)


class TestWebhookReplayDedup:
    """Verify that ``do_POST`` deduplicates signed deliveries by ``svix-id``."""

    def _drive_post(self, path: str, body: bytes, headers: dict[str, str]):
        from io import BytesIO
        from types import MethodType
        from unittest.mock import MagicMock

        from keelson_email.client import _WebhookHandler

        fake = MagicMock()
        fake.path = path
        fake.headers = {"Content-Length": str(len(body)), **headers}
        fake.rfile = BytesIO(body)
        fake.wfile = BytesIO()
        codes: list[int] = []
        fake.send_response = lambda code: codes.append(code)
        fake.end_headers = lambda: None
        # Bind the REAL dispatch/handler methods so module handlers + the
        # reserve/commit/release state machine run.
        for name in (
            "_handle_receive",
            "_handle_event",
            "_ack_duplicate",
            "_reserve_or_respond",
            "_finish_delivery",
            "_release_after_failure",
        ):
            setattr(fake, name, MethodType(getattr(_WebhookHandler, name), fake))
        _WebhookHandler.do_POST(fake)
        return codes

    def test_replayed_signed_delivery_deduped(self, monkeypatch):
        """A repeated ``svix-id`` must not invoke the handler twice."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        received: list = []
        monkeypatch.setattr(mod, "_receive_handler", received.append)

        payload = {
            "delivery_id": "d1",
            "attempt": 1,
            "received_at": "2026-04-01T00:00:00Z",
            "from": {"name": "A", "address": "a@b.com"},
            "subject": "replay",
        }
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)  # fixed svix-id

        first = self._drive_post("/api/webhooks/email", body, headers)
        second = self._drive_post("/api/webhooks/email", body, headers)

        assert first == [200]
        assert second == [200]
        # Handler ran exactly once across the two identical signed deliveries.
        assert len(received) == 1

    def test_retry_reprocessed_after_handler_failure(self, monkeypatch):
        """A retry with the same ``svix-id`` runs after handler failure."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        calls = {"n": 0}

        def _handler(_msg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("first attempt boom")

        monkeypatch.setattr(mod, "_receive_handler", _handler)

        payload = {
            "delivery_id": "d2",
            "attempt": 1,
            "received_at": "2026-04-01T00:00:00Z",
            "from": {"name": "A", "address": "a@b.com"},
            "subject": "retry",
        }
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        first = self._drive_post("/api/webhooks/email", body, headers)
        second = self._drive_post("/api/webhooks/email", body, headers)

        # First attempt failed (500) and released the reservation → retry re-runs.
        assert first == [500]
        assert second == [200]
        assert calls["n"] == 2

    def test_retry_after_no_handler_is_processed(self, monkeypatch):
        """A delivery received before handler registration remains retryable.

        The initial request returns 500 without reserving its ``svix-id``. Once
        a handler is registered, a retry with that ID is processed instead of
        being mistaken for a completed duplicate.
        """
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        monkeypatch.setattr(mod, "_receive_handler", None)

        payload = {
            "delivery_id": "d3",
            "attempt": 1,
            "received_at": "2026-04-01T00:00:00Z",
            "from": {"name": "A", "address": "a@b.com"},
            "subject": "no handler yet",
        }
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        first = self._drive_post("/api/webhooks/email", body, headers)
        assert first == [500]  # no handler → retryable, NOT reserved

        received: list = []
        monkeypatch.setattr(mod, "_receive_handler", received.append)
        second = self._drive_post("/api/webhooks/email", body, headers)
        assert second == [200]
        assert len(received) == 1  # the retry was processed, not deduped

    def test_durable_idempotency_store_dedupes_and_is_used(self, monkeypatch):
        """A shared durable store deduplicates deliveries across instances.

        ``set_idempotency_store`` installs shared state used by ``reserve``.
        Separate request-handler instances therefore agree that the second
        delivery is complete and invoke the application handler only once.
        """
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

        store = _DurableStore()
        mod.set_idempotency_store(store)
        received: list = []
        monkeypatch.setattr(mod, "_receive_handler", received.append)

        payload = _payload("d_store")
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        first = self._drive_post("/api/webhooks/email", body, headers)
        second = self._drive_post("/api/webhooks/email", body, headers)

        assert first == [200]
        assert second == [200]
        # Deduped via the durable store (reserve→commit; second reserve sees completed).
        assert len(received) == 1
        assert store.state["msg_test_123"][0] == "completed"

    def test_durable_store_handler_failure_releases_then_retry_processes(self, monkeypatch):
        """Handler failure releases the reservation so a retry is processed."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        store = _DurableStore()
        mod.set_idempotency_store(store)

        calls = {"n": 0}

        def _handler(_msg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")

        monkeypatch.setattr(mod, "_receive_handler", _handler)
        payload = _payload("d_fail")
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        first = self._drive_post("/api/webhooks/email", body, headers)
        assert first == [500]
        # Released: NOT left as a completed duplicate.
        assert "msg_test_123" not in store.state

        second = self._drive_post("/api/webhooks/email", body, headers)
        assert second == [200]
        assert calls["n"] == 2
        assert store.state["msg_test_123"][0] == "completed"

    def test_durable_store_pending_returns_503_not_duplicate(self, monkeypatch):
        """An unexpired pending reservation returns 503, not a duplicate 200."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        store = _DurableStore()
        store.reserve("msg_test_123")  # pre-existing pending from "attempt A"
        mod.set_idempotency_store(store)

        calls = {"n": 0}
        monkeypatch.setattr(mod, "_receive_handler", lambda _m: calls.__setitem__("n", calls["n"] + 1))
        payload = _payload("d_pending")
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        codes = self._drive_post("/api/webhooks/email", body, headers)
        assert codes == [503]  # retryable in-progress, NOT a 200 duplicate
        assert calls["n"] == 0  # handler NOT run

    def test_durable_store_release_failure_then_lease_expiry_retry(self, monkeypatch):
        """After release failure, a retry processes once the lease expires."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        store = _DurableStore(lease=60.0)
        release_fails = {"v": True}
        orig_release = store.release

        def _release(delivery_id, token):
            if release_fails["v"]:
                raise RuntimeError("release DB error")
            orig_release(delivery_id, token)

        store.release = _release  # type: ignore[method-assign]
        mod.set_idempotency_store(store)

        calls = {"n": 0}

        def _handler(_msg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")

        monkeypatch.setattr(mod, "_receive_handler", _handler)
        payload = _payload("d_relfail")
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        first = self._drive_post("/api/webhooks/email", body, headers)
        assert first == [500]  # handler failed; release also failed (logged)
        # Immediate retry: un-expired pending remains → retryable 503 (NOT 200 dup).
        assert self._drive_post("/api/webhooks/email", body, headers) == [503]
        assert calls["n"] == 1

        # Lease expires → later retry re-acquires and processes.
        release_fails["v"] = False
        store.now = 1_000_000.0 + 120
        assert self._drive_post("/api/webhooks/email", body, headers) == [200]
        assert calls["n"] == 2

    def test_durable_store_reserve_error_returns_500_not_duplicate(self, monkeypatch):
        """A reservation-store error returns retryable 500, not a duplicate ACK."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

        class _BrokenStore:
            def reserve(self, delivery_id: str) -> tuple[str, str]:
                raise RuntimeError("DB down")

            def commit(self, delivery_id: str, token: str) -> None: ...
            def release(self, delivery_id: str, token: str) -> None: ...

        mod.set_idempotency_store(_BrokenStore())
        calls = {"n": 0}
        monkeypatch.setattr(mod, "_receive_handler", lambda _m: calls.__setitem__("n", 1))
        payload = _payload("d_broken")
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        codes = self._drive_post("/api/webhooks/email", body, headers)
        assert codes == [500]  # retryable; NOT a 200 duplicate ACK
        assert calls["n"] == 0  # handler never ran

    def test_durable_store_commit_failure_returns_500_not_200(self, monkeypatch):
        """Commit failure after handler success returns 500, not 200.

        A 200 response without a durable completed record could allow a later
        replay to acquire the delivery and repeat side effects, so commit
        failure must remain retryable.
        """
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        store = _DurableStore()

        def _bad_commit(delivery_id, token):
            raise RuntimeError("commit DB error")

        store.commit = _bad_commit  # type: ignore[method-assign]
        mod.set_idempotency_store(store)
        calls = {"n": 0}
        monkeypatch.setattr(mod, "_receive_handler", lambda _m: calls.__setitem__("n", calls["n"] + 1))
        payload = _payload("d_commit")
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        codes = self._drive_post("/api/webhooks/email", body, headers)
        assert codes == [500]  # handler ran but commit failed → NOT 2xx
        assert calls["n"] == 1

    def test_event_durable_store_commit_failure_returns_500(self, monkeypatch):
        """The event route also returns 500 when durable commit fails."""
        import keelson_email.client as mod

        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        store = _DurableStore()

        def _bad_commit(delivery_id, token):
            raise RuntimeError("commit DB error")

        store.commit = _bad_commit  # type: ignore[method-assign]
        mod.set_idempotency_store(store)
        calls = {"n": 0}
        monkeypatch.setattr(mod, "_event_handler", lambda _e: calls.__setitem__("n", calls["n"] + 1))
        payload = {
            "event_id": "evt_commit",
            "event_type": "bounce",
            "email_address": "a@b.com",
            "timestamp": "2026-04-01T00:00:00Z",
        }
        body = json.dumps(payload).encode("utf-8")
        headers = _svix_headers(payload)

        codes = self._drive_post("/api/webhooks/email-events", body, headers)
        assert codes == [500]
        assert calls["n"] == 1

    def test_default_backend_three_states_and_token_fence(self):
        """The default backend preserves three states and token fencing."""
        import keelson_email.client as mod

        # Acquire (pending). Un-expired pending is DISTINCT from completed.
        status, token_a = mod._reserve_delivery("x", now=1000.0)
        assert status == "acquired"
        assert mod._reserve_delivery("x", now=1010.0)[0] == "pending"  # not completed
        # Lease expires → attempt B takes over with a new token.
        status_b, token_b = mod._reserve_delivery("x", now=1400.0)
        assert status_b == "acquired"
        assert token_b != token_a
        # Stale A commit/release with the OLD token → no-op (B's reservation intact).
        mod._release_delivery("x", token_a)
        assert mod._reserve_delivery("x", now=1410.0)[0] == "pending"  # still B's
        mod._commit_delivery("x", token_a, now=1420.0)
        assert mod._reserve_delivery("x", now=1430.0)[0] == "pending"  # NOT completed
        # B's own commit works → completed within the window.
        mod._commit_delivery("x", token_b, now=1440.0)
        assert mod._reserve_delivery("x", now=1450.0)[0] == "completed"


class TestWebhookSignatureFailClosed:
    """Verify that ``do_POST`` rejects unsigned deliveries in Keelson mode."""

    def _handler_cls(self):
        from keelson_email.client import _WebhookHandler

        return _WebhookHandler

    def _fake(self, *, body: bytes, headers: dict[str, str]):
        from io import BytesIO
        from unittest.mock import MagicMock

        handler_cls = self._handler_cls()
        fake = MagicMock(spec=handler_cls)
        fake.path = "/api/webhooks/email"
        merged = {"Content-Length": str(len(body)), **headers}
        fake.headers = merged
        fake.rfile = BytesIO(body)
        fake.wfile = BytesIO()
        codes: list[int] = []
        fake.send_response = lambda code: codes.append(code)
        fake.end_headers = lambda: None
        return handler_cls, fake, codes

    def test_keelson_mode_no_secret_fails_closed(self, monkeypatch):
        """Keelson mode without a secret returns 500 and skips the handler."""
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.delenv("KEELSON_EMAIL_WEBHOOK_SECRET", raising=False)
        body = json.dumps({"delivery_id": "d1"}).encode("utf-8")
        cls, fake, codes = self._fake(body=body, headers={})
        cls.do_POST(fake)
        assert codes == [500]
        fake._handle_receive.assert_not_called()

    def test_keelson_mode_secret_unsigned_rejected(self, monkeypatch):
        """Keelson mode with a secret rejects unsigned input before parsing."""
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        body = json.dumps({"delivery_id": "d1"}).encode("utf-8")
        cls, fake, codes = self._fake(body=body, headers={})
        cls.do_POST(fake)
        assert codes == [401]
        fake._handle_receive.assert_not_called()

    def test_keelson_mode_signed_accepted(self, monkeypatch):
        """Keelson mode dispatches a delivery with a valid signature."""
        monkeypatch.setenv("KEELSON_MODE", "keelson")
        monkeypatch.setenv("KEELSON_EMAIL_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
        payload = {
            "delivery_id": "d1",
            "attempt": 1,
            "received_at": "2026-04-01T00:00:00Z",
            "from": {"name": "A", "address": "a@b.com"},
            "subject": "hi",
        }
        body = json.dumps(payload).encode("utf-8")
        cls, fake, codes = self._fake(body=body, headers=_svix_headers(payload))
        cls.do_POST(fake)
        # Verification passed → dispatched to _handle_receive (no error code sent
        # by do_POST itself).
        fake._handle_receive.assert_called_once()

    def test_platform_env_no_secret_fails_closed(self, monkeypatch):
        """A platform environment without a secret returns 500."""
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.setenv("KEELSON_APP_ID", "app_123")
        monkeypatch.delenv("KEELSON_EMAIL_WEBHOOK_SECRET", raising=False)
        body = json.dumps({"delivery_id": "d1"}).encode("utf-8")
        cls, fake, codes = self._fake(body=body, headers={})
        cls.do_POST(fake)
        assert codes == [500]
        fake._handle_receive.assert_not_called()

    def test_workspace_env_no_secret_fails_closed(self, monkeypatch):
        """The canonical workspace marker also enables fail-closed behavior."""
        monkeypatch.delenv("KEELSON_MODE", raising=False)
        monkeypatch.delenv("KEELSON_APP_ID", raising=False)
        monkeypatch.setenv("KEELSON_WORKSPACE_ID", "workspace_123")
        monkeypatch.delenv("KEELSON_TENANT_ID", raising=False)
        monkeypatch.delenv("KEELSON_DEPLOY_ID", raising=False)
        monkeypatch.delenv("KEELSON_EMAIL_WEBHOOK_SECRET", raising=False)
        body = json.dumps({"delivery_id": "d1"}).encode("utf-8")
        cls, fake, codes = self._fake(body=body, headers={})
        cls.do_POST(fake)
        assert codes == [500]
        fake._handle_receive.assert_not_called()

    def test_local_mode_unsigned_accepted(self, monkeypatch):
        """Local mode permits unsigned deliveries when no secret is configured."""
        monkeypatch.setenv("KEELSON_MODE", "local")
        monkeypatch.delenv("KEELSON_EMAIL_WEBHOOK_SECRET", raising=False)
        monkeypatch.delenv("KEELSON_APP_ID", raising=False)
        monkeypatch.delenv("KEELSON_WORKSPACE_ID", raising=False)
        monkeypatch.delenv("KEELSON_TENANT_ID", raising=False)
        monkeypatch.delenv("KEELSON_DEPLOY_ID", raising=False)
        body = json.dumps({"delivery_id": "d1"}).encode("utf-8")
        cls, fake, codes = self._fake(body=body, headers={})
        cls.do_POST(fake)
        # Unsigned accepted in local dev → dispatched, no error status from do_POST.
        fake._handle_receive.assert_called_once()


# ---------------------------------------------------------------------------
# InboundMessage.from_webhook_payload
# ---------------------------------------------------------------------------


class TestInboundMessageParsing:
    """CASE: SDK: InboundMessage.from_webhook_payload"""

    SAMPLE_PAYLOAD = {
        "delivery_id": "dlv_abc123",
        "attempt": 1,
        "received_at": "2026-03-30T07:00:00Z",
        "sent_at": "2026-03-30T06:55:00Z",
        "from": {"name": "Taro", "address": "taro@example.com"},
        "to": [{"name": "Support", "address": "app@inbound.example.com"}],
        "cc": [{"name": "Manager", "address": "mgr@example.com"}],
        "reply_to": {"name": "Taro", "address": "taro-reply@example.com"},
        "subject": "Hello",
        "text": "Body",
        "html": "<p>Body</p>",
        "provider_message_id": "<msgid@example.com>",
        "in_reply_to": "<parent@example.com>",
        "references": ["<ref1@example.com>"],
        "envelope_to": "app@inbound.example.com",
        "authentication": {"spf": "pass", "dkim": "pass", "dmarc": "pass"},
        "attachments": [
            {
                "id": "att_123",
                "filename": "doc.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1024,
                "download_url": "/v1/email/attachments/att_123",
            }
        ],
    }

    def test_parses_basic_fields(self):
        msg = InboundMessage.from_webhook_payload(self.SAMPLE_PAYLOAD)
        assert msg.delivery_id == "dlv_abc123"
        assert msg.from_.name == "Taro"
        assert msg.from_.address == "taro@example.com"
        assert msg.subject == "Hello"
        assert msg.text == "Body"

    def test_parses_cc(self):
        msg = InboundMessage.from_webhook_payload(self.SAMPLE_PAYLOAD)
        assert len(msg.cc) == 1
        assert msg.cc[0].address == "mgr@example.com"

    def test_parses_reply_to(self):
        msg = InboundMessage.from_webhook_payload(self.SAMPLE_PAYLOAD)
        assert msg.reply_to is not None
        assert msg.reply_to.address == "taro-reply@example.com"

    def test_parses_threading_headers(self):
        msg = InboundMessage.from_webhook_payload(self.SAMPLE_PAYLOAD)
        assert msg.provider_message_id == "<msgid@example.com>"
        assert msg.in_reply_to == "<parent@example.com>"
        assert msg.references == ["<ref1@example.com>"]

    def test_parses_authentication(self):
        msg = InboundMessage.from_webhook_payload(self.SAMPLE_PAYLOAD)
        assert msg.authentication.spf == "pass"
        assert msg.authentication.dkim == "pass"
        assert msg.authentication.dmarc == "pass"

    def test_parses_attachments(self):
        msg = InboundMessage.from_webhook_payload(self.SAMPLE_PAYLOAD)
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.id == "att_123"
        assert att.filename == "doc.pdf"

    def test_handles_missing_optional_fields(self):
        """CASE: SDK: handles minimal payload with no optional fields"""
        minimal = {
            "delivery_id": "dlv_min",
            "attempt": 1,
            "received_at": "2026-03-30T07:00:00Z",
            "from": {"name": "", "address": "a@b.com"},
            "to": [{"name": "", "address": "x@y.com"}],
            "subject": "Min",
            "envelope_to": "x@y.com",
        }
        msg = InboundMessage.from_webhook_payload(minimal)
        assert msg.cc == []
        assert msg.reply_to is None
        assert msg.in_reply_to is None
        assert msg.references == []
        assert msg.authentication.spf is None


# ---------------------------------------------------------------------------
# on_receive decorator
# ---------------------------------------------------------------------------


class TestOnReceive:
    """CASE: SDK: on_receive decorator"""

    def test_registers_handler(self):
        import keelson_email.client as mod

        with patch("keelson_email.client.serve", return_value=None):

            @on_receive
            def handler(msg):
                pass

            assert mod._receive_handler is handler

    def test_returns_original_function(self):
        def handler(msg):
            pass

        with patch("keelson_email.client.serve", return_value=None):
            result = on_receive(handler)
        assert result is handler

    def test_starts_server_immediately(self):
        """CASE: SDK: on_receive starts webhook server in background thread"""
        import keelson_email.client as mod

        # Reset state so on_receive triggers a fresh server start.
        old_started = mod._auto_serve_started
        old_server = mod._auto_serve_server
        mod._auto_serve_started = False
        mod._auto_serve_server = None

        try:
            with patch(
                "keelson_email.client.serve", return_value=None
            ) as mock_serve:

                @on_receive
                def handler(msg):
                    pass

                # serve(blocking=False) must have been called immediately.
                mock_serve.assert_called_once_with(blocking=False)
        finally:
            mod._auto_serve_started = old_started
            mod._auto_serve_server = old_server

    def test_rejects_async_handler(self):
        """CASE: SDK: on_receive rejects async handler with TypeError"""
        with pytest.raises(TypeError, match="synchronous function"):

            @on_receive
            async def handler(msg):
                pass


# ---------------------------------------------------------------------------
# EmailEventPayload.from_webhook_payload
# ---------------------------------------------------------------------------


class TestEmailEventPayloadParsing:
    """CASE: SDK: EmailEventPayload.from_webhook_payload"""

    SAMPLE_EVENT = {
        "event_id": "evt_abc123",
        "event_type": "bounce",
        "email_address": "bounced@example.com",
        "provider": "resend",
        "send_id": "550e8400-e29b-41d4-a716-446655440000",
        "resend_email_id": "re_456",
        "bounce_type": "hard",
        "detail": "Mailbox not found",
        "timestamp": "2026-04-01T12:00:00Z",
    }

    def test_parses_all_fields(self):
        event = EmailEventPayload.from_webhook_payload(self.SAMPLE_EVENT)
        assert event.event_id == "evt_abc123"
        assert event.event_type == "bounce"
        assert event.email_address == "bounced@example.com"
        assert event.provider == "resend"
        assert event.send_id == "550e8400-e29b-41d4-a716-446655440000"
        assert event.resend_email_id == "re_456"
        assert event.bounce_type == "hard"
        assert event.detail == "Mailbox not found"
        assert event.timestamp == "2026-04-01T12:00:00Z"

    def test_handles_null_optional_fields(self):
        minimal = {
            "event_id": "evt_min",
            "event_type": "delivered",
            "email_address": "user@example.com",
            "timestamp": "2026-04-01T12:00:00Z",
        }
        event = EmailEventPayload.from_webhook_payload(minimal)
        assert event.provider is None
        assert event.send_id is None
        assert event.resend_email_id is None
        assert event.bounce_type is None
        assert event.detail is None


class TestVerificationHelpers:
    """CASE: SDK: webhook verification helpers"""

    def test_verify_webhook_bytes_success(self):
        payload = TestInboundMessageParsing.SAMPLE_PAYLOAD
        body = json.dumps(payload).encode("utf-8")
        msg = verify_webhook_bytes(body, _svix_headers(payload), TEST_WEBHOOK_SECRET)
        assert msg.subject == "Hello"
        assert msg.from_.address == "taro@example.com"

    def test_verify_event_webhook_bytes_invalid_signature(self):
        payload = TestEmailEventPayloadParsing.SAMPLE_EVENT
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "svix-id": "msg_test_123",
            "svix-timestamp": _svix_headers(payload)["svix-timestamp"],
            "svix-signature": "v1,invalid",
        }
        with pytest.raises(EmailError, match="no matching signature"):
            verify_event_webhook_bytes(body, headers, TEST_WEBHOOK_SECRET)


# ---------------------------------------------------------------------------
# on_event decorator
# ---------------------------------------------------------------------------


class TestOnEvent:
    """CASE: SDK: on_event registration"""

    def test_registers_handler(self):
        import keelson_email.client as mod

        with patch("keelson_email.client.serve", return_value=None):

            @on_event
            def handler(event):
                pass

            assert mod._event_handler is handler

    def test_returns_original_function(self):
        def handler(event):
            pass

        with patch("keelson_email.client.serve", return_value=None):
            result = on_event(handler)
        assert result is handler

    def test_starts_server_immediately(self):
        """CASE: SDK: on_event starts webhook server in background thread"""
        import keelson_email.client as mod

        old_started = mod._auto_serve_started
        old_server = mod._auto_serve_server
        mod._auto_serve_started = False
        mod._auto_serve_server = None

        try:
            with patch(
                "keelson_email.client.serve", return_value=None
            ) as mock_serve:

                @on_event
                def handler(event):
                    pass

                mock_serve.assert_called_once_with(blocking=False)
        finally:
            mod._auto_serve_started = old_started
            mod._auto_serve_server = old_server

    def test_rejects_async_handler(self):
        """CASE: SDK: on_event rejects async handler with TypeError"""
        with pytest.raises(TypeError, match="synchronous function"):

            @on_event
            async def handler(event):
                pass


# ---------------------------------------------------------------------------
# Webhook handler: event path dispatch
# ---------------------------------------------------------------------------


class TestWebhookEventDispatch:
    """CASE: SDK: _WebhookHandler dispatches /api/webhooks/email-events"""

    def _make_handler_class(self):
        """Return the _WebhookHandler class for direct testing."""
        from keelson_email.client import _WebhookHandler

        return _WebhookHandler

    def test_event_path_acks_without_handler(self):
        """CASE: SDK: event path returns 200 when no handler is registered"""
        import keelson_email.client as mod

        old_handler = mod._event_handler
        mod._event_handler = None
        try:
            from io import BytesIO

            handler_cls = self._make_handler_class()

            # Build a minimal fake request object.
            from unittest.mock import MagicMock

            request = MagicMock()
            request.path = "/api/webhooks/email-events"
            request.headers = {"Content-Length": "2"}
            request.rfile = BytesIO(b"{}")

            wfile = BytesIO()
            response_code = []

            fake_handler = MagicMock(spec=handler_cls)
            fake_handler.path = "/api/webhooks/email-events"
            fake_handler.headers = {"Content-Length": "2"}
            fake_handler.rfile = BytesIO(b"{}")
            fake_handler.wfile = wfile
            fake_handler.send_response = lambda code: response_code.append(code)
            fake_handler.end_headers = lambda: None

            # Call _handle_event directly.
            handler_cls._handle_event(fake_handler, {})
            assert response_code == [200]
        finally:
            mod._event_handler = old_handler

    def test_event_path_calls_handler(self):
        """CASE: SDK: event path calls registered handler and returns 200"""
        import keelson_email.client as mod

        received = []

        def my_handler(event):
            received.append(event)

        old_handler = mod._event_handler
        mod._event_handler = my_handler
        try:
            from io import BytesIO
            from unittest.mock import MagicMock

            handler_cls = self._make_handler_class()

            response_code = []
            wfile = BytesIO()

            fake_handler = MagicMock(spec=handler_cls)
            fake_handler.wfile = wfile
            fake_handler.send_response = lambda code: response_code.append(code)
            fake_handler.end_headers = lambda: None

            data = {
                "event_id": "evt_1",
                "event_type": "bounce",
                "email_address": "a@b.com",
                "timestamp": "2026-04-01T00:00:00Z",
            }
            handler_cls._handle_event(fake_handler, data)
            assert response_code == [200]
            assert len(received) == 1
            assert received[0].event_type == "bounce"
        finally:
            mod._event_handler = old_handler

    def test_event_path_returns_500_on_handler_error(self):
        """CASE: SDK: event path returns 500 when handler raises"""
        import keelson_email.client as mod

        def failing_handler(event):
            raise ValueError("boom")

        old_handler = mod._event_handler
        mod._event_handler = failing_handler
        try:
            from io import BytesIO
            from unittest.mock import MagicMock

            handler_cls = self._make_handler_class()

            response_code = []
            wfile = BytesIO()

            fake_handler = MagicMock(spec=handler_cls)
            fake_handler.wfile = wfile
            fake_handler.send_response = lambda code: response_code.append(code)
            fake_handler.end_headers = lambda: None

            data = {
                "event_id": "evt_2",
                "event_type": "complaint",
                "email_address": "c@d.com",
                "timestamp": "2026-04-01T00:00:00Z",
            }
            handler_cls._handle_event(fake_handler, data)
            assert response_code == [500]
        finally:
            mod._event_handler = old_handler


# ---------------------------------------------------------------------------
# send (unit: payload construction)
# ---------------------------------------------------------------------------


class TestAddressCoercion:
    """CASE: SDK: Address-to-string coercion"""

    def test_str_passthrough(self):
        assert _to_address_str("a@b.com") == "a@b.com"

    def test_address_extracts_address(self):
        addr = Address(name="Taro", address="taro@example.com")
        assert _to_address_str(addr) == "taro@example.com"

    def test_list_mixed(self):
        result = _to_address_list(
            [
                "a@b.com",
                Address(name="X", address="x@y.com"),
            ]
        )
        assert result == ["a@b.com", "x@y.com"]

    def test_single_address_to_list(self):
        addr = Address(name="Taro", address="taro@example.com")
        assert _to_address_list(addr) == ["taro@example.com"]

    def test_single_str_to_list(self):
        assert _to_address_list("a@b.com") == ["a@b.com"]


class TestSendPayload:
    """CASE: SDK: send constructs correct payload"""

    def test_send_raises_without_env(self):
        """CASE: SDK: send raises when env vars are missing"""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EmailError, match="KEELSON_EMAIL_API_URL"):
                send(to="a@b.com", subject="Hi", text="Body")

    def test_send_raises_without_token(self):
        with patch.dict(
            os.environ, {"KEELSON_EMAIL_API_URL": "http://test"}, clear=True
        ):
            with pytest.raises(EmailError, match="KEELSON_EMAIL_TOKEN"):
                send(to="a@b.com", subject="Hi", text="Body")

    def test_send_accepts_address_objects(self):
        """CASE: SDK: send accepts Address objects in to/cc/bcc"""
        addr = Address(name="Taro", address="taro@example.com")
        with patch.dict(os.environ, {}, clear=True):
            # Should fail on env var, NOT on serialisation
            with pytest.raises(EmailError, match="KEELSON_EMAIL_API_URL"):
                send(to=addr, subject="Hi", text="Body")

    @pytest.mark.parametrize("gateway_url", [None, "", "   "])
    def test_legacy_request_contract_is_unchanged(self, gateway_url):
        env = {
            "KEELSON_EMAIL_API_URL": "  http://legacy.test/  ",
            "KEELSON_EMAIL_TOKEN": " token ",
        }
        if gateway_url is not None:
            env["KEELSON_EMAIL_BASE_URL"] = gateway_url

        response = patch("keelson_email.client.urlopen")
        with patch.dict(os.environ, env, clear=True), response as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = (
                b'{"send_id":"send_1","status":"queued"}'
            )
            result = send(
                to="a@example.com",
                subject="Subject",
                text="Body",
                timeout_sec=12.5,
            )

        assert result == {"send_id": "send_1", "status": "queued"}
        request = mock_urlopen.call_args.args[0]
        assert request.full_url == "http://legacy.test/v1/email/send"
        assert request.method == "POST"
        assert request.get_header("Authorization") == "Bearer token"
        assert request.get_header("Content-type") == "application/json"
        assert request.get_header("Accept") == "application/json"
        # T-0595: urllib's default UA (Python-urllib/3.x) is blocked by
        # Cloudflare BIC (Error 1010) on the *.keelson.run zones.
        assert request.get_header("User-agent") == "Keelson-Python-SDK/0.1.1"
        assert json.loads(request.data) == {
            "to": ["a@example.com"],
            "subject": "Subject",
            "text": "Body",
        }
        assert mock_urlopen.call_args.kwargs == {"timeout": 12.5}

    def test_gateway_request_uses_trimmed_base_url(self):
        env = {
            "KEELSON_EMAIL_BASE_URL": "  http://gateway.test///  ",
            "KEELSON_EMAIL_API_URL": "http://legacy.invalid",
            "KEELSON_EMAIL_TOKEN": "token",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("keelson_email.client.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = (
                b'{"send_id":"send_1","status":"queued"}'
            )
            send(to="a@example.com", subject="Subject", text="Body")

        request = mock_urlopen.call_args.args[0]
        assert request.full_url == "http://gateway.test/__keelson/email/send"
        assert request.get_header("User-agent") == "Keelson-Python-SDK/0.1.1"

    def test_legacy_error_contract_is_unchanged(self):
        error = HTTPError(
            "http://legacy.test/v1/email/send",
            422,
            "Unprocessable Entity",
            {},
            BytesIO(b'{"error":{"code":"INVALID","message":"bad request"}}'),
        )
        env = {
            "KEELSON_EMAIL_API_URL": "http://legacy.test",
            "KEELSON_EMAIL_TOKEN": "token",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("keelson_email.client.urlopen", side_effect=error),
            pytest.raises(EmailError, match=r"^Send failed \[INVALID\]: bad request$"),
        ):
            send(to="a@example.com", subject="Subject", text="Body")


# ---------------------------------------------------------------------------
# Attachment.download (unit: URL construction)
# ---------------------------------------------------------------------------


class TestAttachmentDownload:
    """CASE: SDK: InboundAttachment.download"""

    def test_raises_without_env(self):
        att = InboundAttachment(
            id="att_1",
            filename="f.pdf",
            content_type="application/pdf",
            size_bytes=100,
            download_url="/v1/email/attachments/att_1",
        )
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EmailError, match="KEELSON_EMAIL_API_URL"):
                att.download()

    def test_legacy_download_uses_payload_url(self):
        att = InboundAttachment(
            id="att_new_id",
            filename="f.pdf",
            content_type="application/pdf",
            size_bytes=100,
            download_url="/v1/email/attachments/att_legacy_id",
        )
        env = {
            "KEELSON_EMAIL_API_URL": "http://legacy.test/",
            "KEELSON_EMAIL_TOKEN": "token",
            "KEELSON_EMAIL_BASE_URL": "   ",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("keelson_email.client.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = b"data"
            assert att.download(timeout_sec=9.0) == b"data"

        request = mock_urlopen.call_args.args[0]
        assert request.full_url == (
            "http://legacy.test/v1/email/attachments/att_legacy_id"
        )
        assert request.method == "GET"
        assert request.get_header("Authorization") == "Bearer token"
        assert request.get_header("Accept") == "application/octet-stream"
        assert request.get_header("User-agent") == "Keelson-Python-SDK/0.1.1"
        assert request.get_header("Content-type") is None
        assert request.data is None
        assert mock_urlopen.call_args.kwargs == {"timeout": 9.0}

    def test_gateway_download_uses_id_and_ignores_payload_url(self):
        att = InboundAttachment(
            id="att_from_payload_id",
            filename="f.pdf",
            content_type="application/pdf",
            size_bytes=100,
            download_url="/v1/email/attachments/must-not-be-used",
        )
        env = {
            "KEELSON_EMAIL_BASE_URL": " http://gateway.test/// ",
            "KEELSON_EMAIL_API_URL": "http://legacy.invalid",
            "KEELSON_EMAIL_TOKEN": "token",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("keelson_email.client.urlopen") as mock_urlopen,
        ):
            mock_urlopen.return_value.__enter__.return_value.read.return_value = b"data"
            assert att.download() == b"data"

        request = mock_urlopen.call_args.args[0]
        assert request.full_url == (
            "http://gateway.test/__keelson/email/attachments/att_from_payload_id"
        )
        assert "must-not-be-used" not in request.full_url
        assert request.get_header("User-agent") == "Keelson-Python-SDK/0.1.1"

    def test_legacy_download_error_contract_is_unchanged(self):
        error = HTTPError(
            "http://legacy.test/v1/email/attachments/att_1",
            404,
            "Not Found",
            {},
            BytesIO(b"attachment missing"),
        )
        att = InboundAttachment(
            id="att_1",
            filename="f.pdf",
            content_type="application/pdf",
            size_bytes=100,
            download_url="/v1/email/attachments/att_1",
        )
        env = {
            "KEELSON_EMAIL_API_URL": "http://legacy.test",
            "KEELSON_EMAIL_TOKEN": "token",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("keelson_email.client.urlopen", side_effect=error),
            pytest.raises(EmailError) as exc_info,
        ):
            att.download()

        assert str(exc_info.value) == (
            "Attachment download failed (404): attachment missing"
        )


# ---------------------------------------------------------------------------
# Namespace import
# ---------------------------------------------------------------------------


class TestNamespaceImport:
    """CASE: SDK: keelson namespace import"""

    def test_from_keelson_import_email(self):
        """CASE: SDK: ``from keelson import email`` resolves to keelson_email"""
        from keelson import email

        assert hasattr(email, "send")
        assert hasattr(email, "on_receive")
        assert hasattr(email, "InboundMessage")
        assert hasattr(email, "Attachment")
        assert hasattr(email, "Address")
