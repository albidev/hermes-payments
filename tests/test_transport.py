"""
Hermes Payments — P3 transport boundary tests (v2: kind-9 envelope).

Tests the Buzz transport with FakeExecutor.  No network, no subprocess.
Covers:
1. FakeExecutor basics (send, get, channel/kind/time/limit filtering)
2. PaymentEnvelope codec (encode/decode round trips, approval rejection)
3. Untrusted message validation (kind, channel, protocol, schema, expiry, identity)
4. BuzzTransport send/receive through the boundary
5. PaymentApproval non-serializability (structural guarantees)
6. Channel scoping (UUID required, scoped operations)
7. Wire kind constant (kind 9 only)
8. Receipt author identity validation
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch as mock_patch

import pytest

from hermes_payments.envelope import (
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    WIRE_KIND,
    decode_content,
    encode_content,
)
from hermes_payments.models import (
    BuzzIdentity,
    MessageKind,
    PaymentApproval,
    PaymentIntent,
    PaymentMessage,
    PaymentQuote,
    PaymentReceipt,
)
from hermes_payments.peer_transport import (
    PeerMessage,
    PeerTransport,
    PeerTransportError,
)
from hermes_payments.transport import (
    BuzzTransport,
    BuzzTransportError,
    EnvelopeValidationError,
    FakeExecutor,
    RawBuzzEvent,
    SubprocessExecutor,
    _channel_from_tags,
    validate_received_event,
)
from tests.fixtures import (
    NOW,
    ONE_HOUR,
    RECIPIENT_PUBKEY,
    SENDER_PUBKEY,
    make_approval,
    make_intent,
    make_quote,
    make_receipt,
)

# Test channel UUID
CHANNEL_UUID = "550e8400-e29b-41d4-a716-446655440000"


class TestSubprocessExecutor:
    def test_send_builds_expected_buzz_command(self):
        calls = []
        content = '{"protocol":"hermes-payments"}'

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"event_id": "event-send-1"}),
                stderr="",
            )

        with mock_patch("hermes_payments.transport.subprocess.run", side_effect=fake_run):
            result = SubprocessExecutor(buzz_bin="/opt/buzz", timeout=7).send(
                channel=CHANNEL_UUID,
                content=content,
            )

        assert result.event_id == "event-send-1"
        assert calls == [
            (
                [
                    "/opt/buzz",
                    "messages",
                    "send",
                    "--channel",
                    CHANNEL_UUID,
                    "--content",
                    content,
                ],
                {"capture_output": True, "text": True, "timeout": 7},
            )
        ]

    def test_get_builds_filters_and_parses_events(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps([
                    {
                        "id": "event-get-1",
                        "pubkey": "aa" * 32,
                        "kind": 9,
                        "content": "{}",
                        "tags": [["h", CHANNEL_UUID]],
                        "created_at": NOW,
                    }
                ]),
                stderr="",
            )

        with mock_patch("hermes_payments.transport.subprocess.run", side_effect=fake_run):
            events = SubprocessExecutor(buzz_bin="/opt/buzz", timeout=9).get(
                channel=CHANNEL_UUID,
                kinds=[9],
                since=NOW - 1,
                limit=3,
            )

        assert events == [
            RawBuzzEvent(
                id="event-get-1",
                pubkey="aa" * 32,
                kind=9,
                content="{}",
                tags=[["h", CHANNEL_UUID]],
                created_at=NOW,
            )
        ]
        assert calls == [
            (
                [
                    "/opt/buzz",
                    "messages",
                    "get",
                    "--channel",
                    CHANNEL_UUID,
                    "--kinds",
                    "9",
                    "--since",
                    str(NOW - 1),
                    "--limit",
                    "3",
                ],
                {"capture_output": True, "text": True, "timeout": 9},
            )
        ]


# ---------------------------------------------------------------------------
# 1. FakeExecutor basics
# ---------------------------------------------------------------------------


class TestFakeExecutor:
    def test_send_returns_event_id(self):
        ex = FakeExecutor()
        result = ex.send(channel="test-uuid", content="hello")
        assert result.event_id
        assert len(result.event_id) == 64  # SHA-256 hex

    def test_send_stores_sent_messages(self):
        ex = FakeExecutor()
        ex.send(channel="ch-1", content="msg-1")
        ex.send(channel="ch-1", content="msg-2")
        assert len(ex.sent) == 2
        assert ex.sent[0] == ("ch-1", "msg-1")
        assert ex.sent[1] == ("ch-1", "msg-2")

    def test_send_creates_kind_9_event(self):
        """FakeExecutor creates kind-9 events (NIP-29 channel message)."""
        ex = FakeExecutor()
        ex.send(channel="ch-1", content="hello")
        events = ex.get(channel="ch-1")
        assert len(events) == 1
        assert events[0].kind == 9

    def test_send_creates_retrievable_event(self):
        ex = FakeExecutor()
        ex.send(channel="ch-1", content="hello")
        events = ex.get(channel="ch-1")
        assert len(events) == 1
        assert events[0].content == "hello"

    def test_get_returns_matching_channel(self):
        ex = FakeExecutor()
        ex.send(channel="ch-1", content="a")
        ex.send(channel="ch-2", content="b")
        results = ex.get(channel="ch-1")
        assert len(results) == 1
        assert results[0].content == "a"

    def test_get_filters_by_kind(self):
        ex = FakeExecutor()
        ex.inject_event(
            RawBuzzEvent(
                id="ev1",
                pubkey="aa" * 32,
                kind=9,
                content='{"test": true}',
                tags=[["h", "ch-1"]],
                created_at=NOW,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev2",
                pubkey="aa" * 32,
                kind=1,
                content='{"test": false}',
                tags=[["h", "ch-1"]],
                created_at=NOW,
            )
        )
        results = ex.get(channel="ch-1", kinds=[9])
        assert len(results) == 1
        assert results[0].kind == 9

    def test_get_filters_by_since(self):
        ex = FakeExecutor()
        ex.inject_event(
            RawBuzzEvent(
                id="ev1", pubkey="aa" * 32, kind=9,
                content="{}", tags=[["h", "ch-1"]], created_at=100,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev2", pubkey="aa" * 32, kind=9,
                content="{}", tags=[["h", "ch-1"]], created_at=200,
            )
        )
        results = ex.get(channel="ch-1", since=150)
        assert len(results) == 1
        assert results[0].created_at == 200

    def test_get_filters_by_limit(self):
        ex = FakeExecutor()
        for i in range(5):
            ex.inject_event(
                RawBuzzEvent(
                    id=f"ev{i}", pubkey="aa" * 32, kind=9,
                    content="{}", tags=[["h", "ch-1"]], created_at=NOW + i,
                )
            )
        results = ex.get(channel="ch-1", limit=3)
        assert len(results) == 3

    def test_get_empty_channel(self):
        ex = FakeExecutor()
        ex.send(channel="ch-1", content="a")
        results = ex.get(channel="ch-nonexistent")
        assert results == []

    def test_inject_event(self):
        ex = FakeExecutor()
        ev = RawBuzzEvent(
            id="injected", pubkey="bb" * 32, kind=9,
            content='{"injected": true}', tags=[["h", "ch-x"]], created_at=999,
        )
        ex.inject_event(ev)
        results = ex.get(channel="ch-x", kinds=[9])
        assert len(results) == 1
        assert results[0].id == "injected"


# ---------------------------------------------------------------------------
# 2. PaymentEnvelope codec
# ---------------------------------------------------------------------------


class TestEnvelopeCodec:
    def test_encode_intent_round_trip(self):
        intent = make_intent()
        content = encode_content(intent)
        data = json.loads(content)
        assert data["protocol"] == PROTOCOL_ID
        assert data["version"] == PROTOCOL_VERSION
        assert data["type"] == "payment_intent"
        assert data["payload"]["id"] == intent.id
        assert data["payload"]["amount_sat"] == intent.amount_sat

    def test_encode_quote_round_trip(self):
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        data = json.loads(content)
        assert data["type"] == "payment_quote"
        assert data["payload"]["intent_id"] == intent.id
        assert data["payload"]["fee_sat"] == quote.fee_sat

    def test_encode_receipt_round_trip(self):
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        data = json.loads(content)
        assert data["type"] == "payment_receipt"
        assert data["payload"]["settlement_ref"] == "payment_hash_abc123"
        assert data["payload"]["amount_sat"] == intent.amount_sat
        assert data["payload"]["recipient"]["pubkey"] == RECIPIENT_PUBKEY

    def test_encode_approval_raises(self):
        """PaymentApproval must NEVER be encodable."""
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)
        with pytest.raises(TypeError, match="PaymentApproval must never"):
            encode_content(approval)

    def test_decode_intent(self):
        intent = make_intent()
        content = encode_content(intent)
        decoded = decode_content(content)
        assert isinstance(decoded, PaymentIntent)
        assert decoded.id == intent.id
        assert decoded.amount_sat == intent.amount_sat

    def test_decode_quote(self):
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        decoded = decode_content(content)
        assert isinstance(decoded, PaymentQuote)
        assert decoded.intent_id == intent.id

    def test_decode_receipt(self):
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        decoded = decode_content(content)
        assert isinstance(decoded, PaymentReceipt)
        assert decoded.settlement_ref == "payment_hash_abc123"
        assert decoded.recipient.pubkey == RECIPIENT_PUBKEY

    def test_decode_invalid_protocol_raises(self):
        data = {
            "protocol": "wrong-protocol",
            "version": "1",
            "type": "payment_intent",
            "payload": {},
        }
        with pytest.raises(ValueError, match="unknown protocol"):
            decode_content(json.dumps(data))

    def test_decode_invalid_version_raises(self):
        data = {
            "protocol": "hermes-payments",
            "version": "99",
            "type": "payment_intent",
            "payload": {},
        }
        with pytest.raises(ValueError, match="unsupported protocol version"):
            decode_content(json.dumps(data))

    def test_decode_invalid_type_raises(self):
        data = {
            "protocol": "hermes-payments",
            "version": "1",
            "type": "unknown_type",
            "payload": {},
        }
        with pytest.raises(ValueError, match="unknown envelope type"):
            decode_content(json.dumps(data))

    def test_decode_invalid_json_raises(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            decode_content("not json at all")

    def test_decode_missing_payload_raises(self):
        data = {
            "protocol": "hermes-payments",
            "version": "1",
            "type": "payment_intent",
        }
        with pytest.raises(ValueError, match="missing or invalid 'payload'"):
            decode_content(json.dumps(data))

    def test_decode_schema_validation_fails(self):
        """Intent payload missing required fields should fail."""
        data = {
            "protocol": "hermes-payments",
            "version": "1",
            "type": "payment_intent",
            "payload": {"id": "x"},  # missing required fields
        }
        with pytest.raises(ValueError, match="payload validation failed"):
            decode_content(json.dumps(data))

    def test_codec_explicit_json(self):
        """The codec uses explicit JSON serialization, not pickle/protobuf."""
        intent = make_intent()
        content = encode_content(intent)
        parsed = json.loads(content)
        assert isinstance(parsed, dict)
        assert parsed["protocol"] == "hermes-payments"


# ---------------------------------------------------------------------------
# 3. validate_received_event (untrusted message validation)
# ---------------------------------------------------------------------------


class TestValidateReceivedEvent:
    def test_valid_intent(self):
        intent = make_intent()
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-001",
            pubkey=SENDER_PUBKEY,
            kind=9,
            content=content,
            tags=[["h", CHANNEL_UUID]],
            created_at=NOW,
        )
        msg = validate_received_event(
            event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(msg, PaymentIntent)
        assert msg.id == intent.id

    def test_valid_quote(self):
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-002",
            pubkey=RECIPIENT_PUBKEY,
            kind=9,
            content=content,
            tags=[["h", CHANNEL_UUID]],
            created_at=NOW,
        )
        msg = validate_received_event(
            event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(msg, PaymentQuote)
        assert msg.intent_id == intent.id

    def test_valid_receipt(self):
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        event = RawBuzzEvent(
            id="ev-003",
            pubkey=RECIPIENT_PUBKEY,
            kind=9,
            content=content,
            tags=[["h", CHANNEL_UUID]],
            created_at=NOW,
        )
        msg = validate_received_event(
            event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(msg, PaymentReceipt)
        assert msg.recipient.pubkey == RECIPIENT_PUBKEY

    def test_rejects_non_nine_kind(self):
        event = RawBuzzEvent(
            id="ev-999", pubkey="aa" * 32, kind=1,
            content="hello", tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="expected 9"):
            validate_received_event(event, expected_channel=CHANNEL_UUID)

    def test_rejects_custom_kind_40100(self):
        """The old invalid custom kind must be rejected."""
        event = RawBuzzEvent(
            id="ev-40100", pubkey="aa" * 32, kind=40100,
            content="{}", tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="expected 9"):
            validate_received_event(event, expected_channel=CHANNEL_UUID)

    def test_rejects_wrong_channel_h_tag(self):
        """h-tag channel mismatch must be rejected."""
        intent = make_intent()
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-wrong-ch",
            pubkey=SENDER_PUBKEY,
            kind=9,
            content=content,
            tags=[["h", "wrong-channel-uuid"]],
            created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_missing_h_tag(self):
        """Event with no h-tag must be rejected."""
        intent = make_intent()
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-no-h",
            pubkey=SENDER_PUBKEY,
            kind=9,
            content=content,
            tags=[],
            created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_empty_h_tag(self):
        """An empty h-tag is not valid channel scope."""
        intent = make_intent()
        event = RawBuzzEvent(
            id="ev-empty-h", pubkey=SENDER_PUBKEY, kind=9,
            content=encode_content(intent), tags=[["h", ""]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_invalid_protocol_in_content(self):
        """Content with wrong protocol must be rejected."""
        bad_content = json.dumps({
            "protocol": "wrong-protocol",
            "version": "1",
            "type": "payment_intent",
            "payload": {},
        })
        event = RawBuzzEvent(
            id="ev-bad-proto", pubkey=SENDER_PUBKEY, kind=9,
            content=bad_content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="content validation"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_invalid_json_content(self):
        event = RawBuzzEvent(
            id="ev-bad", pubkey="aa" * 32, kind=9,
            content="not json", tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="content validation"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_expired_intent(self):
        intent = make_intent(expires_at=NOW - 1)
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-exp", pubkey=SENDER_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]],
            created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_expired_quote(self):
        intent = make_intent()
        quote = make_quote(intent, expires_at=NOW - 1)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-exp", pubkey=RECIPIENT_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]],
            created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_identity_mismatch_intent(self):
        """Intent with wrong author pubkey is rejected."""
        intent = make_intent()
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-wrong", pubkey="dd" * 32, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_identity_mismatch_quote(self):
        """Quote with wrong author pubkey is rejected."""
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-wrong", pubkey="dd" * 32, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_rejects_identity_mismatch_receipt(self):
        """Receipt with wrong author pubkey is rejected."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        event = RawBuzzEvent(
            id="ev-wrong", pubkey="dd" * 32, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_accepts_valid_not_expired(self):
        """Message within validity window is accepted."""
        intent = make_intent(expires_at=NOW + ONE_HOUR)
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-ok", pubkey=SENDER_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        msg = validate_received_event(
            event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(msg, PaymentIntent)


# ---------------------------------------------------------------------------
# 4. BuzzTransport send/receive
# ---------------------------------------------------------------------------


class TestBuzzTransport:
    def test_send_intent(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)
        intent = make_intent()
        event_id = transport.send_intent(intent)
        assert event_id
        assert len(ex.sent) == 1
        sent_content = ex.sent[0][1]
        data = json.loads(sent_content)
        assert data["type"] == "payment_intent"
        assert data["payload"]["id"] == intent.id

    def test_send_quote(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)
        intent = make_intent()
        quote = make_quote(intent)
        event_id = transport.send_quote(quote)
        assert event_id
        assert len(ex.sent) == 1
        sent_content = ex.sent[0][1]
        data = json.loads(sent_content)
        assert data["type"] == "payment_quote"
        assert data["payload"]["intent_id"] == intent.id

    def test_send_receipt(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        event_id = transport.send_receipt(receipt)
        assert event_id
        assert len(ex.sent) == 1
        sent_content = ex.sent[0][1]
        data = json.loads(sent_content)
        assert data["type"] == "payment_receipt"
        assert data["payload"]["settlement_ref"] == "payment_hash_abc123"

    def test_send_to_correct_channel(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="specific-uuid-1234", clock=lambda: NOW)
        intent = make_intent()
        transport.send_intent(intent)
        assert ex.sent[0][0] == "specific-uuid-1234"

    def test_native_subscriber_receives_since_and_closes(self):
        class Subscriber:
            def __init__(self):
                self.since = None
                self.closed = False

            def poll_events(self, *, since=None):
                self.since = since
                intent = make_intent()
                return [
                    RawBuzzEvent(
                        id="native-event",
                        pubkey=SENDER_PUBKEY,
                        kind=9,
                        content=encode_content(intent),
                        tags=[["h", CHANNEL_UUID]],
                        created_at=NOW,
                    )
                ]

            def close(self):
                self.closed = True

        subscriber = Subscriber()
        transport = BuzzTransport(
            FakeExecutor(),
            channel=CHANNEL_UUID,
            clock=lambda: NOW,
            nostr_sub=subscriber,
        )

        assert len(transport.receive_messages(since=NOW - 10)) == 1
        assert subscriber.since == NOW - 10
        transport.close()
        assert subscriber.closed

    def test_receive_valid_messages(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)

        intent = make_intent()
        quote = make_quote(intent)

        ex.inject_event(
            RawBuzzEvent(
                id="ev-intent", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev-quote", pubkey=RECIPIENT_PUBKEY, kind=9,
                content=encode_content(quote),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )

        messages = transport.receive_messages()
        assert len(messages) == 2
        assert isinstance(messages[0], PaymentIntent)
        assert isinstance(messages[1], PaymentQuote)

    def test_receive_deduplicates_across_polls_and_restart(self, tmp_path):
        ex = FakeExecutor()
        cursor_path = tmp_path / "buzz-cursor.json"
        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-once", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )

        transport = BuzzTransport(
            ex,
            channel=CHANNEL_UUID,
            clock=lambda: NOW,
            cursor_path=cursor_path,
        )
        assert len(transport.receive_messages()) == 1
        assert transport.receive_messages() == []
        assert cursor_path.exists()

        restored = BuzzTransport(
            ex,
            channel=CHANNEL_UUID,
            clock=lambda: NOW,
            cursor_path=cursor_path,
        )
        assert restored.receive_messages() == []

    def test_receive_cursor_save_failure_does_not_ack_event(self, tmp_path):
        ex = FakeExecutor()
        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-retry", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )
        transport = BuzzTransport(
            ex,
            channel=CHANNEL_UUID,
            clock=lambda: NOW,
            cursor_path=tmp_path / "cursor.json",
        )
        original_save = transport._save_cursor

        def fail_save():
            raise BuzzTransportError("cursor disk failure")

        transport._save_cursor = fail_save  # type: ignore[method-assign]
        with pytest.raises(BuzzTransportError, match="cursor disk failure"):
            transport.receive_messages()
        assert "ev-retry" not in transport._seen_event_ids

        transport._save_cursor = original_save  # type: ignore[method-assign]
        assert len(transport.receive_messages()) == 1

    def test_receive_filters_local_author(self):
        ex = FakeExecutor()
        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-local", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )
        transport = BuzzTransport(
            ex,
            channel=CHANNEL_UUID,
            clock=lambda: NOW,
            local_pubkey=SENDER_PUBKEY,
        )
        assert transport.receive_messages() == []

    def test_receive_skips_invalid_events(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)

        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-valid", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )
        # Invalid: non-kind-9
        ex.inject_event(
            RawBuzzEvent(
                id="ev-invalid", pubkey="aa" * 32, kind=1,
                content="hello", tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )
        # Invalid: expired
        expired_intent = make_intent(expires_at=NOW - 1)
        ex.inject_event(
            RawBuzzEvent(
                id="ev-expired", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(expired_intent),
                tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )

        messages = transport.receive_messages()
        assert len(messages) == 1
        assert isinstance(messages[0], PaymentIntent)

    def test_receive_channel_scoped(self):
        """Only messages from the configured channel are returned."""
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="ch-A", clock=lambda: NOW)

        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-a", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", "ch-A"]], created_at=NOW,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev-b", pubkey=SENDER_PUBKEY, kind=9,
                content=encode_content(intent),
                tags=[["h", "ch-B"]], created_at=NOW,
            )
        )

        messages = transport.receive_messages()
        assert len(messages) == 1

    def test_channel_required(self):
        with pytest.raises(ValueError, match="channel UUID is required"):
            BuzzTransport(FakeExecutor(), channel="")

    def test_transport_no_private_key_attribute(self):
        """Transport never stores or exposes a private key."""
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)
        assert not hasattr(transport, "_private_key")
        assert not hasattr(transport, "_keys")
        assert not hasattr(transport, "_secret")


# ---------------------------------------------------------------------------
# 5. PaymentApproval non-serializability
# ---------------------------------------------------------------------------


class TestApprovalNonSerializable:
    def test_approval_not_in_payment_message_union(self):
        """PaymentApproval is not part of the PaymentMessage union."""
        from hermes_payments.models import PaymentMessage
        assert PaymentApproval not in PaymentMessage.__args__

    def test_approval_not_in_message_kind_enum(self):
        """MessageKind enum does not include APPROVAL."""
        kind_names = [k.name for k in MessageKind]
        assert "APPROVAL" not in kind_names

    def test_encode_content_rejects_approval(self):
        """encode_content() must reject PaymentApproval at runtime."""
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)
        with pytest.raises(TypeError, match="PaymentApproval must never"):
            encode_content(approval)

    def test_approval_no_envelope_kind(self):
        """No Nostr kind is assigned for PaymentApproval."""
        from hermes_payments.envelope import KIND_MAP
        assert len(KIND_MAP) == 3
        for msg_kind in MessageKind:
            assert msg_kind.name != "APPROVAL"


# ---------------------------------------------------------------------------
# 6. Channel scoping
# ---------------------------------------------------------------------------


class TestChannelScoping:
    def test_channel_required_for_transport(self):
        with pytest.raises(ValueError, match="channel UUID is required"):
            BuzzTransport(FakeExecutor(), channel="")

    def test_channel_uuid_forwarded_to_executor_send(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="uuid-1234")
        intent = make_intent()
        transport.send_intent(intent)
        assert ex.sent[0][0] == "uuid-1234"

    def test_different_channels_isolated(self):
        ex = FakeExecutor()

        intent_a = make_intent()
        intent_b = make_intent(idempotency_key="other-key")

        transport_a = BuzzTransport(ex, channel="ch-A", clock=lambda: NOW)
        transport_b = BuzzTransport(ex, channel="ch-B", clock=lambda: NOW)

        transport_a.send_intent(intent_a)
        transport_b.send_intent(intent_b)

        assert ex.sent[0][0] == "ch-A"
        assert ex.sent[1][0] == "ch-B"


# ---------------------------------------------------------------------------
# 10. Transport-neutral adapter surface
# ---------------------------------------------------------------------------


class TestGenericPeerTransport:
    def test_buzz_transport_implements_peer_transport(self):
        transport = BuzzTransport(
            FakeExecutor(), channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(transport, PeerTransport)

    def test_generic_send_and_receive_preserve_peer_metadata(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)
        intent = make_intent()

        message_id = transport.send(intent)
        received = transport.receive()

        assert message_id
        assert len(received) == 1
        assert isinstance(received[0], PeerMessage)
        assert received[0].message_id == message_id
        assert received[0].message == intent
        assert received[0].author == intent.sender
        assert received[0].published_at == ex.get(channel=CHANNEL_UUID)[0].created_at

    def test_legacy_send_helpers_delegate_to_generic_send(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel=CHANNEL_UUID, clock=lambda: NOW)
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)

        assert transport.send_intent(intent)
        assert transport.send_quote(quote)
        assert transport.send_receipt(receipt)

    def test_generic_send_rejects_approval_at_peer_boundary(self):
        transport = BuzzTransport(FakeExecutor(), channel=CHANNEL_UUID)
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)

        with pytest.raises(PeerTransportError, match="PaymentApproval"):
            transport.send(cast(PaymentMessage, approval))


# ---------------------------------------------------------------------------
# 7. Wire kind constant
# ---------------------------------------------------------------------------


class TestWireKind:
    def test_wire_kind_is_nine(self):
        """All payment messages use NIP-29 kind 9."""
        assert WIRE_KIND == 9

    def test_kind_map_all_nine(self):
        """Every payment type maps to kind 9."""
        from hermes_payments.envelope import KIND_MAP
        for kind in KIND_MAP.values():
            assert kind == 9

    def test_kind_map_count(self):
        """Three payment types: intent, quote, receipt."""
        from hermes_payments.envelope import KIND_MAP
        assert len(KIND_MAP) == 3


# ---------------------------------------------------------------------------
# 8. Receipt author identity
# ---------------------------------------------------------------------------


class TestReceiptIdentity:
    def test_receipt_has_recipient_field(self):
        """PaymentReceipt includes recipient identity."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        assert hasattr(receipt, "recipient")
        assert isinstance(receipt.recipient, BuzzIdentity)
        assert receipt.recipient.pubkey == RECIPIENT_PUBKEY

    def test_receipt_recipient_in_envelope(self):
        """Recipient is included in the PaymentEnvelope payload."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        data = json.loads(content)
        assert "recipient" in data["payload"]
        assert data["payload"]["recipient"]["pubkey"] == RECIPIENT_PUBKEY

    def test_receipt_author_validated(self):
        """Receipt with wrong author pubkey is rejected."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        event = RawBuzzEvent(
            id="ev-r-wrong", pubkey="dd" * 32, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
            )

    def test_receipt_author_accepted(self):
        """Receipt authored by recipient is accepted."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        event = RawBuzzEvent(
            id="ev-r-ok", pubkey=RECIPIENT_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        msg = validate_received_event(
            event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(msg, PaymentReceipt)
        assert msg.recipient.pubkey == RECIPIENT_PUBKEY


# ---------------------------------------------------------------------------
# 9. Helper: _channel_from_tags
# ---------------------------------------------------------------------------


class TestChannelFromTags:
    def test_extracts_h_tag(self):
        tags = [["h", "my-channel"], ["p", "some-pubkey"]]
        assert _channel_from_tags(tags) == "my-channel"

    def test_returns_empty_if_no_h_tag(self):
        tags = [["p", "some-pubkey"], ["e", "some-event"]]
        assert _channel_from_tags(tags) == ""

    def test_empty_tags(self):
        assert _channel_from_tags([]) == ""

    def test_first_h_tag_wins(self):
        tags = [["h", "first"], ["h", "second"]]
        assert _channel_from_tags(tags) == "first"
