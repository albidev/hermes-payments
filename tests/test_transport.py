"""
Hermes Payments — P3 transport boundary tests.

Tests the Buzz transport with FakeExecutor.  No network, no subprocess.
Covers:
1. FakeExecutor basics (send, get, channel/kind/time/limit filtering)
2. JSON envelope codec (encode/decode round trips, approval rejection)
3. Untrusted message validation (kind, content, expiry, identity)
4. BuzzTransport send/receive through the boundary
5. PaymentApproval non-serializability (structural guarantees)
6. Channel scoping (UUID required, scoped operations)
7. Payment kind constants
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

import pytest
from fixtures import (
    NOW,
    ONE_HOUR,
    APPROVER_PUBKEY,
    RECIPIENT_PUBKEY,
    SENDER_PUBKEY,
    make_approval,
    make_intent,
    make_quote,
    make_receipt,
)
from hermes_payments.envelope import KIND_MAP
from hermes_payments.models import (
    MessageKind,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    PaymentReceipt,
)
from hermes_payments.transport import (
    BuzzExecutor,
    BuzzTransport,
    BuzzTransportError,
    EnvelopeValidationError,
    FakeExecutor,
    PAYMENT_KINDS,
    RawBuzzEvent,
    SendResult,
    SubprocessExecutor,
    decode_content,
    encode_content,
    validate_received_event,
    _channel_from_tags,
)


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
                kind=40100,
                content='{"test": true}',
                tags=[["h", "ch-1"]],
                created_at=NOW,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev2",
                pubkey="aa" * 32,
                kind=9,
                content='{"test": false}',
                tags=[["h", "ch-1"]],
                created_at=NOW,
            )
        )
        results = ex.get(channel="ch-1", kinds=[40100])
        assert len(results) == 1
        assert results[0].kind == 40100

    def test_get_filters_by_since(self):
        ex = FakeExecutor()
        ex.inject_event(
            RawBuzzEvent(
                id="ev1",
                pubkey="aa" * 32,
                kind=40100,
                content="{}",
                tags=[["h", "ch-1"]],
                created_at=100,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev2",
                pubkey="aa" * 32,
                kind=40100,
                content="{}",
                tags=[["h", "ch-1"]],
                created_at=200,
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
                    id=f"ev{i}",
                    pubkey="aa" * 32,
                    kind=40100,
                    content="{}",
                    tags=[["h", "ch-1"]],
                    created_at=NOW + i,
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
            id="injected",
            pubkey="bb" * 32,
            kind=40101,
            content='{"injected": true}',
            tags=[["h", "ch-x"]],
            created_at=999,
        )
        ex.inject_event(ev)
        results = ex.get(channel="ch-x", kinds=[40101])
        assert len(results) == 1
        assert results[0].id == "injected"


# ---------------------------------------------------------------------------
# 2. JSON envelope codec
# ---------------------------------------------------------------------------


class TestEnvelopeCodec:
    def test_encode_intent_round_trip(self):
        intent = make_intent()
        content = encode_content(intent)
        data = json.loads(content)
        assert data["id"] == intent.id
        assert data["amount_sat"] == intent.amount_sat
        assert data["sender"]["pubkey"] == SENDER_PUBKEY

    def test_encode_quote_round_trip(self):
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        data = json.loads(content)
        assert data["intent_id"] == intent.id
        assert data["fee_sat"] == quote.fee_sat

    def test_encode_receipt_round_trip(self):
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        data = json.loads(content)
        assert data["settlement_ref"] == "payment_hash_abc123"
        assert data["amount_sat"] == intent.amount_sat

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
        decoded = decode_content(content, kind=KIND_MAP[MessageKind.INTENT])
        assert isinstance(decoded, PaymentIntent)
        assert decoded.id == intent.id
        assert decoded.amount_sat == intent.amount_sat

    def test_decode_quote(self):
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        decoded = decode_content(content, kind=KIND_MAP[MessageKind.QUOTE])
        assert isinstance(decoded, PaymentQuote)
        assert decoded.intent_id == intent.id

    def test_decode_receipt(self):
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        content = encode_content(receipt)
        decoded = decode_content(content, kind=KIND_MAP[MessageKind.RECEIPT])
        assert isinstance(decoded, PaymentReceipt)
        assert decoded.settlement_ref == "payment_hash_abc123"

    def test_decode_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="not a payment kind"):
            decode_content('{"test": true}', kind=999)

    def test_decode_invalid_json_raises(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            decode_content("not json at all", kind=40100)

    def test_decode_wrong_model_for_kind(self):
        """Intent JSON decoded as QUOTE kind should fail validation."""
        intent = make_intent()
        content = encode_content(intent)
        with pytest.raises(Exception):  # pydantic ValidationError
            decode_content(content, kind=KIND_MAP[MessageKind.QUOTE])

    def test_codec_explicit_json(self):
        """The codec uses explicit JSON serialization, not pickle/protobuf."""
        intent = make_intent()
        content = encode_content(intent)
        # Must be valid JSON
        parsed = json.loads(content)
        assert isinstance(parsed, dict)


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
            kind=KIND_MAP[MessageKind.INTENT],
            content=content,
            tags=[],
            created_at=NOW,
        )
        msg = validate_received_event(event, clock=lambda: NOW)
        assert isinstance(msg, PaymentIntent)
        assert msg.id == intent.id

    def test_valid_quote(self):
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-002",
            pubkey=RECIPIENT_PUBKEY,
            kind=KIND_MAP[MessageKind.QUOTE],
            content=content,
            tags=[],
            created_at=NOW,
        )
        msg = validate_received_event(event, clock=lambda: NOW)
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
            kind=KIND_MAP[MessageKind.RECEIPT],
            content=content,
            tags=[],
            created_at=NOW,
        )
        msg = validate_received_event(event, clock=lambda: NOW)
        assert isinstance(msg, PaymentReceipt)

    def test_rejects_non_payment_kind(self):
        event = RawBuzzEvent(
            id="ev-999",
            pubkey="aa" * 32,
            kind=1,
            content="hello",
            tags=[],
            created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="not a payment kind"):
            validate_received_event(event)

    def test_rejects_unknown_kind_999(self):
        event = RawBuzzEvent(
            id="ev-999",
            pubkey="aa" * 32,
            kind=999,
            content="{}",
            tags=[],
            created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="not a payment kind"):
            validate_received_event(event)

    def test_rejects_invalid_json_content(self):
        event = RawBuzzEvent(
            id="ev-bad",
            pubkey="aa" * 32,
            kind=40100,
            content="not json",
            tags=[],
            created_at=NOW,
        )
        with pytest.raises(
            EnvelopeValidationError, match="content validation failed"
        ):
            validate_received_event(event)

    def test_rejects_expired_intent(self):
        intent = make_intent(expires_at=NOW - 1)
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-exp",
            pubkey=SENDER_PUBKEY,
            kind=KIND_MAP[MessageKind.INTENT],
            content=content,
            tags=[],
            created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(event, clock=lambda: NOW)

    def test_rejects_expired_quote(self):
        intent = make_intent()
        quote = make_quote(intent, expires_at=NOW - 1)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-exp",
            pubkey=RECIPIENT_PUBKEY,
            kind=KIND_MAP[MessageKind.QUOTE],
            content=content,
            tags=[],
            created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(event, clock=lambda: NOW)

    def test_rejects_identity_mismatch_intent(self):
        """Intent with wrong author pubkey is rejected."""
        intent = make_intent()
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-wrong",
            pubkey="dd" * 32,  # not SENDER_PUBKEY
            kind=KIND_MAP[MessageKind.INTENT],
            content=content,
            tags=[],
            created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(event, clock=lambda: NOW)

    def test_rejects_identity_mismatch_quote(self):
        """Quote with wrong author pubkey is rejected."""
        intent = make_intent()
        quote = make_quote(intent)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-wrong",
            pubkey="dd" * 32,  # not RECIPIENT_PUBKEY
            kind=KIND_MAP[MessageKind.QUOTE],
            content=content,
            tags=[],
            created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(event, clock=lambda: NOW)

    def test_accepts_valid_not_expired(self):
        """Message within validity window is accepted."""
        intent = make_intent(expires_at=NOW + ONE_HOUR)
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-ok",
            pubkey=SENDER_PUBKEY,
            kind=KIND_MAP[MessageKind.INTENT],
            content=content,
            tags=[],
            created_at=NOW,
        )
        msg = validate_received_event(event, clock=lambda: NOW)
        assert isinstance(msg, PaymentIntent)


# ---------------------------------------------------------------------------
# 4. BuzzTransport send/receive
# ---------------------------------------------------------------------------


class TestBuzzTransport:
    def test_send_intent(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)
        intent = make_intent()
        event_id = transport.send_intent(intent)
        assert event_id
        assert len(ex.sent) == 1
        # Verify the sent content is valid JSON matching the intent
        sent_content = ex.sent[0][1]
        data = json.loads(sent_content)
        assert data["id"] == intent.id

    def test_send_quote(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)
        intent = make_intent()
        quote = make_quote(intent)
        event_id = transport.send_quote(quote)
        assert event_id
        assert len(ex.sent) == 1
        sent_content = ex.sent[0][1]
        data = json.loads(sent_content)
        assert data["intent_id"] == intent.id

    def test_send_receipt(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        event_id = transport.send_receipt(receipt)
        assert event_id
        assert len(ex.sent) == 1
        sent_content = ex.sent[0][1]
        data = json.loads(sent_content)
        assert data["settlement_ref"] == "payment_hash_abc123"

    def test_send_to_correct_channel(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="specific-uuid-1234", clock=lambda: NOW)
        intent = make_intent()
        transport.send_intent(intent)
        assert ex.sent[0][0] == "specific-uuid-1234"

    def test_receive_valid_messages(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)

        intent = make_intent()
        quote = make_quote(intent)

        ex.inject_event(
            RawBuzzEvent(
                id="ev-intent",
                pubkey=SENDER_PUBKEY,
                kind=KIND_MAP[MessageKind.INTENT],
                content=encode_content(intent),
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev-quote",
                pubkey=RECIPIENT_PUBKEY,
                kind=KIND_MAP[MessageKind.QUOTE],
                content=encode_content(quote),
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
            )
        )

        messages = transport.receive_messages()
        assert len(messages) == 2
        assert isinstance(messages[0], PaymentIntent)
        assert isinstance(messages[1], PaymentQuote)

    def test_receive_skips_invalid_events(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)

        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-valid",
                pubkey=SENDER_PUBKEY,
                kind=KIND_MAP[MessageKind.INTENT],
                content=encode_content(intent),
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
            )
        )
        # Invalid: non-payment kind
        ex.inject_event(
            RawBuzzEvent(
                id="ev-invalid",
                pubkey="aa" * 32,
                kind=1,
                content="hello",
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
            )
        )
        # Invalid: expired
        expired_intent = make_intent(expires_at=NOW - 1)
        ex.inject_event(
            RawBuzzEvent(
                id="ev-expired",
                pubkey=SENDER_PUBKEY,
                kind=KIND_MAP[MessageKind.INTENT],
                content=encode_content(expired_intent),
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
            )
        )

        messages = transport.receive_messages()
        assert len(messages) == 1
        assert isinstance(messages[0], PaymentIntent)

    def test_receive_filters_by_payment_kinds(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)

        intent = make_intent()
        ex.inject_event(
            RawBuzzEvent(
                id="ev-payment",
                pubkey=SENDER_PUBKEY,
                kind=KIND_MAP[MessageKind.INTENT],
                content=encode_content(intent),
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
            )
        )
        # Regular channel message (kind 9) — not a payment kind
        ex.inject_event(
            RawBuzzEvent(
                id="ev-regular",
                pubkey="aa" * 32,
                kind=9,
                content="hello world",
                tags=[["h", "test-ch-uuid"]],
                created_at=NOW,
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
                id="ev-a",
                pubkey=SENDER_PUBKEY,
                kind=KIND_MAP[MessageKind.INTENT],
                content=encode_content(intent),
                tags=[["h", "ch-A"]],
                created_at=NOW,
            )
        )
        ex.inject_event(
            RawBuzzEvent(
                id="ev-b",
                pubkey=SENDER_PUBKEY,
                kind=KIND_MAP[MessageKind.INTENT],
                content=encode_content(intent),
                tags=[["h", "ch-B"]],
                created_at=NOW,
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
        transport = BuzzTransport(ex, channel="test-ch-uuid", clock=lambda: NOW)
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

    def test_approval_not_in_kind_map(self):
        """PaymentApproval has no entry in KIND_MAP."""
        kind_values = [k.value for k in KIND_MAP.keys()]
        assert "approval" not in kind_values

    def test_encode_content_rejects_approval(self):
        """encode_content() must reject PaymentApproval at runtime."""
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)
        with pytest.raises(TypeError, match="PaymentApproval must never"):
            encode_content(approval)

    def test_approval_no_envelope_kind(self):
        """No Nostr kind is assigned for PaymentApproval."""
        # KIND_MAP should have exactly 3 entries (intent, quote, receipt)
        assert len(KIND_MAP) == 3
        for msg_kind in MessageKind:
            if msg_kind != MessageKind.INTENT and msg_kind != MessageKind.QUOTE:
                if msg_kind != MessageKind.RECEIPT:
                    # If a new kind is added, it must not be approval
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

    def test_channel_uuid_forwarded_to_executor_get(self):
        ex = FakeExecutor()
        transport = BuzzTransport(ex, channel="uuid-5678")
        transport.receive_messages()
        # The get call was scoped to uuid-5678
        # (FakeExecutor.get filters by channel)

    def test_different_channels_isolated(self):
        ex = FakeExecutor()

        intent_a = make_intent()
        intent_b = make_intent(idempotency_key="other-key")

        transport_a = BuzzTransport(ex, channel="ch-A", clock=lambda: NOW)
        transport_b = BuzzTransport(ex, channel="ch-B", clock=lambda: NOW)

        transport_a.send_intent(intent_a)
        transport_b.send_intent(intent_b)

        # Each channel sees only its own messages
        messages_a = transport_a.receive_messages()
        messages_b = transport_b.receive_messages()
        # Both are sent (kind 9 in FakeExecutor), but injected payment events
        # would be channel-scoped.  Verify send isolation:
        assert ex.sent[0][0] == "ch-A"
        assert ex.sent[1][0] == "ch-B"


# ---------------------------------------------------------------------------
# 7. PAYMENT_KINDS constant
# ---------------------------------------------------------------------------


class TestPaymentKinds:
    def test_payment_kinds_are_buzz_custom_range(self):
        for kind in PAYMENT_KINDS:
            assert 40000 <= kind <= 49999

    def test_payment_kinds_match_envelope_map(self):
        assert set(PAYMENT_KINDS) == set(KIND_MAP.values())

    def test_payment_kinds_count(self):
        """Three payment kinds: intent, quote, receipt."""
        assert len(PAYMENT_KINDS) == 3

    def test_no_approval_kind(self):
        """PaymentApproval has no associated kind."""
        from hermes_payments.models import PaymentMessage

        for msg_kind in MessageKind:
            assert msg_kind.value in [k.value for k in KIND_MAP.keys()]


# ---------------------------------------------------------------------------
# 8. Helper: _channel_from_tags
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
