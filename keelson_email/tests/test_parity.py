"""Cross-language parity tests for Email.

These tests load the shared fixtures from sdk/fixtures/parity/ and
assert the same semantic field values that Go and Node parity tests assert.

Tests go through verify_webhook_bytes / verify_event_webhook_bytes to
exercise the real SDK entry point, not just from_webhook_payload.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import time

from keelson_email.client import verify_event_webhook_bytes, verify_webhook_bytes
from sdk_test_fixtures import parity_fixtures_dir

FIXTURES = parity_fixtures_dir(__file__)

# Deterministic fake secret for tests.
_SECRET_RAW = b"fake-test-webhook-key!!"
_SECRET = f"whsec_{base64.b64encode(_SECRET_RAW).decode()}"


def _sign(body: bytes) -> dict[str, str]:
    msg_id = "msg_parity_001"
    timestamp = str(math.floor(time.time()))
    signed = f"{msg_id}.{timestamp}.".encode() + body
    sig = base64.b64encode(
        hmac.new(_SECRET_RAW, signed, hashlib.sha256).digest()
    ).decode()
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{sig}",
    }


def test_parity_inbound_email() -> None:
    """Parse shared fixture through verify_webhook_bytes."""
    body = (FIXTURES / "email_inbound.json").read_bytes()
    headers = _sign(body)

    msg = verify_webhook_bytes(body, headers, _SECRET)

    # --- Cross-language parity assertions ---
    assert msg.delivery_id == "dlv_parity01"
    assert msg.attempt == 1
    assert msg.received_at == "2026-01-15T10:00:00Z"
    assert msg.sent_at == "2026-01-15T09:59:00Z"

    assert msg.from_.name == "Sender"
    assert msg.from_.address == "sender@example.com"

    assert len(msg.to) == 1
    assert msg.to[0].address == "receiver@example.com"

    assert len(msg.cc) == 0
    assert msg.reply_to is None

    assert msg.subject == "Parity test"
    assert msg.text == "Hello from parity test"
    assert msg.html is None

    assert msg.provider_message_id == "msg_provider_01"
    assert msg.in_reply_to is None
    assert msg.references == ["ref-001"]
    assert msg.envelope_to == "receiver@example.com"

    # Authentication
    assert msg.authentication.spf == "pass"
    assert msg.authentication.dkim == "pass"
    assert msg.authentication.dmarc == "pass"

    # Spam
    assert msg.spam.score == 0.1
    assert msg.spam.verdict == "clean"
    assert msg.spam.reasons == []

    # Attachments
    assert len(msg.attachments) == 1
    assert msg.attachments[0].id == "att_001"
    assert msg.attachments[0].filename == "document.pdf"
    assert msg.attachments[0].content_type == "application/pdf"
    assert msg.attachments[0].size_bytes == 1024


def test_parity_email_event() -> None:
    """Parse shared fixture through verify_event_webhook_bytes."""
    body = (FIXTURES / "email_event.json").read_bytes()
    headers = _sign(body)

    evt = verify_event_webhook_bytes(body, headers, _SECRET)

    # --- Cross-language parity assertions ---
    assert evt.event_id == "evt_parity01"
    assert evt.event_type == "bounce"
    assert evt.email_address == "bounced@example.com"
    assert evt.provider == "resend"
    assert evt.send_id == "550e8400-e29b-41d4-a716-446655440000"
    assert evt.resend_email_id == "re_001"
    assert evt.bounce_type == "hard"
    assert evt.detail == "Mailbox not found"
    assert evt.timestamp == "2026-01-15T11:00:00Z"
