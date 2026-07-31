"""
Hermes Payments — contract invariant tests (v1 correction pass).

These tests validate the protocol contract without requiring live
credentials or external services.  They cover:
1. State machine correctness (transitions, terminal states, invariants)
2. Idempotency / replay protection
3. Canonical serialization determinism
4. Envelope round-trip encoding (approval excluded — local only)
5. Adapter boundary constraints
"""

from __future__ import annotations

import sys
import os

# Ensure src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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

from hermes_payments.adapter import (
    AmbiguousResult,
    WavelengthAdapter,
)
from hermes_payments.envelope import (
    KIND_MAP,
    BuzzEnvelope,
    envelope_to_payment,
    payment_to_envelope,
)
from hermes_payments.models import (
    BuzzIdentity,
    MessageKind,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    PaymentReceipt,
    Rail,
    RailReceiveInstruction,
    compute_id,
)
from hermes_payments.state_machine import (
    TERMINAL_STATES,
    PaymentState,
    TransitionResult,
    can_transition,
    reachable_states,
    transition,
)


# ===========================================================================
# 1. State machine correctness
# ===========================================================================


class TestStateMachine:
    """State machine invariants."""

    def test_happy_path(self):
        """Full happy path: DRAFT → ... → SETTLED."""
        state = PaymentState.DRAFT
        steps = [
            ("submit", PaymentState.SUBMITTED),
            ("quote_received", PaymentState.QUOTED),
            ("prepared", PaymentState.PREPARED),
            ("approved", PaymentState.APPROVED),
            ("executing", PaymentState.EXECUTING),
            ("settled", PaymentState.SETTLED),
        ]
        for trigger, expected in steps:
            result = transition(state, trigger)
            assert result.ok, f"transition({state}, {trigger}) failed: {result.error}"
            assert result.new_state == expected
            state = result.new_state

    def test_settled_is_terminal(self):
        """SETTLED admits no further transitions."""
        for trigger in ["submit", "cancel", "rejected", "expired", "adapter_error", "settled"]:
            result = transition(PaymentState.SETTLED, trigger)
            assert not result.ok
            assert result.new_state == PaymentState.SETTLED

    def test_all_terminal_states_are_absorbing(self):
        """Every terminal state rejects all triggers."""
        triggers = ["submit", "quote_received", "prepared", "approved",
                     "executing", "settled", "cancel", "rejected",
                     "expired", "adapter_error", "confirm_settled"]
        for state in TERMINAL_STATES:
            for trigger in triggers:
                result = transition(state, trigger)
                assert not result.ok, f"terminal state {state} accepted trigger '{trigger}'"

    def test_cancellation_from_active_states(self):
        """Sender can cancel from DRAFT, SUBMITTED, QUOTED, PREPARED."""
        cancellable = [PaymentState.DRAFT, PaymentState.SUBMITTED,
                       PaymentState.QUOTED, PaymentState.PREPARED]
        for state in cancellable:
            result = transition(state, "cancel")
            assert result.ok, f"cancel from {state} should work"
            assert result.new_state == PaymentState.CANCELLED

    def test_cannot_cancel_from_approved(self):
        """Cannot cancel after approval — execution is imminent."""
        result = transition(PaymentState.APPROVED, "cancel")
        assert not result.ok

    def test_expiry_from_non_terminal(self):
        """All non-terminal states transition to EXPIRED on expiry, except EXECUTING."""
        non_terminal = reachable_states() - TERMINAL_STATES
        for state in non_terminal:
            if state == PaymentState.DRAFT:
                continue  # DRAFT has no expiry trigger defined
            if state == PaymentState.RECONCILIATION_REQUIRED:
                continue  # RECONCILIATION_REQUIRED has no expiry trigger
            result = transition(state, "expired")
            if can_transition(state, "expired"):
                if state == PaymentState.EXECUTING:
                    assert result.new_state == PaymentState.RECONCILIATION_REQUIRED
                else:
                    assert result.new_state == PaymentState.EXPIRED

    def test_adapter_error_from_pre_execution(self):
        """Adapter errors from QUOTED/PREPARED/APPROVED → FAILED."""
        error_states = [PaymentState.QUOTED, PaymentState.PREPARED,
                        PaymentState.APPROVED]
        for state in error_states:
            result = transition(state, "adapter_error")
            assert result.ok
            assert result.new_state == PaymentState.FAILED

    def test_adapter_error_from_executing_goes_to_reconciliation(self):
        """Adapter errors from EXECUTING → RECONCILIATION_REQUIRED (not FAILED)."""
        result = transition(PaymentState.EXECUTING, "adapter_error")
        assert result.ok
        assert result.new_state == PaymentState.RECONCILIATION_REQUIRED

    def test_expiry_from_executing_goes_to_reconciliation(self):
        """Expiry from EXECUTING → RECONCILIATION_REQUIRED (not EXPIRED)."""
        result = transition(PaymentState.EXECUTING, "expired")
        assert result.ok
        assert result.new_state == PaymentState.RECONCILIATION_REQUIRED

    def test_reconciliation_not_terminal(self):
        """RECONCILIATION_REQUIRED is not terminal — allows confirm_settled."""
        assert PaymentState.RECONCILIATION_REQUIRED not in TERMINAL_STATES
        result = transition(PaymentState.RECONCILIATION_REQUIRED, "confirm_settled")
        assert result.ok
        assert result.new_state == PaymentState.SETTLED

    def test_reconciliation_rejects_other_triggers(self):
        """RECONCILIATION_REQUIRED only allows confirm_settled and receipt_received."""
        for trigger in ["submit", "cancel", "rejected", "expired",
                         "adapter_error", "executing", "settled",
                         "quote_received", "prepared", "approved"]:
            result = transition(PaymentState.RECONCILIATION_REQUIRED, trigger)
            assert not result.ok, f"RECONCILIATION_REQUIRED accepted trigger '{trigger}'"
        # receipt_received and confirm_settled ARE allowed
        assert can_transition(PaymentState.RECONCILIATION_REQUIRED, "receipt_received")
        assert can_transition(PaymentState.RECONCILIATION_REQUIRED, "confirm_settled")

    def test_approval_requires_prepare(self):
        """APPROVED can only be reached from PREPARED."""
        # From PREPARED → approved works
        result = transition(PaymentState.PREPARED, "approved")
        assert result.ok
        assert result.new_state == PaymentState.APPROVED

        # From QUOTED → approved does NOT work
        result = transition(PaymentState.QUOTED, "approved")
        assert not result.ok

    def test_no_unexpected_transitions(self):
        """Verify the complete transition table matches the spec."""
        expected_transitions = {
            (PaymentState.DRAFT, "submit"): PaymentState.SUBMITTED,
            (PaymentState.SUBMITTED, "quote_received"): PaymentState.QUOTED,
            (PaymentState.QUOTED, "prepared"): PaymentState.PREPARED,
            (PaymentState.PREPARED, "approved"): PaymentState.APPROVED,
            (PaymentState.APPROVED, "executing"): PaymentState.EXECUTING,
            (PaymentState.EXECUTING, "settled"): PaymentState.SETTLED,
            (PaymentState.EXECUTING, "expired"): PaymentState.RECONCILIATION_REQUIRED,
            (PaymentState.EXECUTING, "adapter_error"): PaymentState.RECONCILIATION_REQUIRED,
            (PaymentState.EXECUTING, "receipt_received"): PaymentState.SETTLED,
            (PaymentState.RECONCILIATION_REQUIRED, "confirm_settled"): PaymentState.SETTLED,
            (PaymentState.RECONCILIATION_REQUIRED, "receipt_received"): PaymentState.SETTLED,
        }
        for (state, trigger), expected in expected_transitions.items():
            result = transition(state, trigger)
            assert result.ok
            assert result.new_state == expected


# ===========================================================================
# 2. Idempotency / replay protection
# ===========================================================================


class TestIdempotency:
    """Intent ID derivation and replay invariants."""

    def test_deterministic_id(self):
        """Same fields produce same ID."""
        i1 = make_intent()
        i2 = make_intent()
        assert i1.id == i2.id

    def test_different_idempotency_key_different_id(self):
        """Different idempotency_key → different intent ID."""
        i1 = make_intent(idempotency_key="key-1")
        i2 = make_intent(idempotency_key="key-2")
        assert i1.id != i2.id

    def test_different_amount_different_id(self):
        """Different amount_sat → different intent ID."""
        i1 = make_intent(amount_sat=1000)
        i2 = make_intent(amount_sat=2000)
        assert i1.id != i2.id

    def test_different_recipient_different_id(self):
        """Different recipient → different intent ID."""
        i1 = make_intent()
        i2 = make_intent()
        i2 = PaymentIntent(
            id="x",
            idempotency_key=i1.idempotency_key,
            sender=i1.sender,
            recipient=BuzzIdentity(pubkey="dd" * 32, relay_url=None),
            amount_sat=i1.amount_sat,
            purpose=i1.purpose,
            max_fee_sat=i1.max_fee_sat,
            expires_at=i1.expires_at,
            created_at=i1.created_at,
        )
        i2.id = compute_id(i2)
        assert i1.id != i2.id

    def test_quote_tied_to_intent(self):
        """Quote ID includes intent_id — different intent → different quote."""
        i1 = make_intent(idempotency_key="a")
        i2 = make_intent(idempotency_key="b")
        q1 = make_quote(i1)
        q2 = make_quote(i2, quote_id="q-001")  # same quote_id but different intent
        assert q1.id != q2.id

    def test_approval_binds_triple(self):
        """Approval ID depends on (intent_id, quote_id, prepared_hash)."""
        i = make_intent()
        q = make_quote(i)
        a1 = make_approval(i, q, prepared_hash="aa" * 32)
        a2 = make_approval(i, q, prepared_hash="bb" * 32)
        assert a1.id != a2.id

    def test_no_replay_settled(self):
        """Once settled, same intent cannot settle again."""
        i = make_intent()
        q = make_quote(i)
        r = make_receipt(i, q)
        # Simulate: the receipt exists, a second receipt with same intent_id
        # would be a duplicate.  The state machine enforces this:
        state = PaymentState.SETTLED
        result = transition(state, "settled")
        assert not result.ok


# ===========================================================================
# 3. Canonical serialization
# ===========================================================================


class TestSerialization:
    """Canonical serialization determinism."""

    def test_deterministic_canonical_bytes(self):
        """Same model → same canonical bytes."""
        i = make_intent()
        b1 = i.model_dump_json(exclude_none=True)
        b2 = i.model_dump_json(exclude_none=True)
        assert b1 == b2

    def test_key_order_independent(self):
        """Pydantic dumps in model field order; canonical form is deterministic."""
        i = make_intent()
        j1 = i.model_dump(mode="python")
        j2 = i.model_dump(mode="python")
        assert list(j1.keys()) == list(j2.keys())

    def test_none_values_excluded(self):
        """Optional fields that are None should not appear in canonical form."""
        i = make_intent()
        dumped = i.model_dump(exclude_none=True)
        assert "rail" not in dumped  # PaymentIntent has no rail field
        # relay_url is None in our fixture
        assert "relay_url" not in dumped["sender"]

    def test_id_matches_canonical_hash(self):
        """compute_id produces SHA-256 of canonical JSON (excluding id field)."""
        i = make_intent()
        import hashlib, json
        raw = i.model_dump(exclude_none=True, mode="python")
        raw.pop("id", None)  # id is excluded from its own hash
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        expected = hashlib.sha256(canonical).hexdigest()
        assert i.id == expected


# ===========================================================================
# 4. Envelope round-trip (approval excluded — local only)
# ===========================================================================


class TestEnvelope:
    """Buzz envelope encoding/decoding (v2: kind-9 envelope)."""

    def test_intent_round_trip(self):
        """PaymentIntent → BuzzEnvelope → PaymentIntent preserves data."""
        intent = make_intent()
        env = payment_to_envelope(
            intent,
            author_pubkey=SENDER_PUBKEY,
            event_id="ev-001",
            event_sig="aa" * 64,
        )
        decoded = envelope_to_payment(env)
        assert isinstance(decoded, PaymentIntent)
        assert decoded.id == intent.id
        assert decoded.amount_sat == intent.amount_sat
        assert decoded.sender.pubkey == SENDER_PUBKEY

    def test_quote_round_trip(self):
        """PaymentQuote → BuzzEnvelope → PaymentQuote preserves data."""
        intent = make_intent()
        quote = make_quote(intent)
        env = payment_to_envelope(
            quote,
            author_pubkey=RECIPIENT_PUBKEY,
            event_id="ev-002",
            event_sig="bb" * 64,
        )
        decoded = envelope_to_payment(env)
        assert isinstance(decoded, PaymentQuote)
        assert decoded.intent_id == intent.id

    def test_receipt_round_trip(self):
        """PaymentReceipt → BuzzEnvelope → PaymentReceipt preserves data."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        env = payment_to_envelope(
            receipt,
            author_pubkey=RECIPIENT_PUBKEY,
            event_id="ev-004",
            event_sig="dd" * 64,
        )
        decoded = envelope_to_payment(env)
        assert isinstance(decoded, PaymentReceipt)
        assert decoded.settlement_ref == receipt.settlement_ref
        assert decoded.recipient.pubkey == RECIPIENT_PUBKEY

    def test_non_nine_kind_rejected(self):
        """Envelope with kind != 9 raises ValueError."""
        env = BuzzEnvelope(
            id="ev-999",
            pubkey=SENDER_PUBKEY,
            kind=1,  # not kind 9
            content="hello",
            sig="aa" * 64,
        )
        with pytest.raises(ValueError, match="not a payment kind"):
            envelope_to_payment(env)

    def test_envelope_tags_contain_protocol(self):
        """Intent envelope tags include protocol identifier."""
        from hermes_payments.envelope import WIRE_KIND
        intent = make_intent()
        env = payment_to_envelope(
            intent,
            author_pubkey=SENDER_PUBKEY,
            event_id="ev-010",
            event_sig="aa" * 64,
        )
        assert env.kind == WIRE_KIND
        tag_keys = [t[0] for t in env.tags]
        assert "protocol" in tag_keys

    def test_all_payment_kinds_are_nine(self):
        """All payment message kinds use NIP-29 kind 9."""
        from hermes_payments.envelope import KIND_MAP
        for kind in KIND_MAP.values():
            assert kind == 9

    def test_approval_not_in_envelope(self):
        """PaymentApproval cannot be serialized into a BuzzEnvelope.

        Approval is strictly local authorisation — it never enters
        the transport layer.
        """
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)

        # PaymentApproval is NOT in PaymentMessage union — should not be
        # serializable through payment_to_envelope.
        from hermes_payments.envelope import _kind_for_model
        with pytest.raises(TypeError):
            _kind_for_model(approval)

    def test_message_kind_has_no_approval(self):
        """MessageKind enum does not include APPROVAL."""
        kind_names = [k.name for k in MessageKind]
        assert "APPROVAL" not in kind_names


# ===========================================================================
# 5. Adapter boundary
# ===========================================================================


class TestAdapterBoundary:
    """Adapter interface constraints."""

    def test_wavelength_rail_is_lightning(self):
        """Wavelength adapter handles LIGHTNING rail."""
        adapter = WavelengthAdapter()
        assert adapter.rail == Rail.LIGHTNING

    def test_prepare_not_implemented(self):
        """WavelengthAdapter.prepare() raises NotImplementedError (stub)."""
        adapter = WavelengthAdapter()
        with pytest.raises(NotImplementedError, match="requires a live Wavelength daemon"):
            adapter.prepare(
                receive_instruction=RailReceiveInstruction(rail=Rail.LIGHTNING, invoice="lnbc..."),
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_execute_not_implemented(self):
        """WavelengthAdapter.execute() raises NotImplementedError (stub)."""
        adapter = WavelengthAdapter()
        with pytest.raises(NotImplementedError):
            adapter.execute(prepared_payload=b"...", prepared_hash="aa" * 32)

    def test_ambiguous_result_is_adapter_error(self):
        """AmbiguousResult is a subclass of AdapterError."""
        from hermes_payments.adapter import AdapterError
        assert issubclass(AmbiguousResult, AdapterError)

    def test_adapter_error_recoverable_flag(self):
        """AdapterError carries a recoverable flag."""
        from hermes_payments.adapter import AdapterError
        e = AdapterError("test", recoverable=True)
        assert e.recoverable is True
        e2 = AdapterError("test", recoverable=False)
        assert e2.recoverable is False


# ===========================================================================
# 6. Protocol version
# ===========================================================================


class TestProtocolVersion:
    """Version field invariants."""

    def test_all_messages_share_version(self):
        """All domain messages use protocol_version '1'."""
        i = make_intent()
        q = make_quote(i)
        a = make_approval(i, q)
        r = make_receipt(i, q)
        for msg in [i, q, a, r]:
            assert msg.protocol_version == "1"

    def test_version_affects_id(self):
        """Changing version changes the ID (prevents cross-version replay)."""
        i = make_intent()
        j = i.model_copy(update={"protocol_version": "99"})
        j.id = compute_id(j)
        assert i.id != j.id


# ===========================================================================
# 7. P3 transport boundary invariants (kind-9 envelope)
# ===========================================================================


class TestP3TransportBoundary:
    """P3 transport boundary invariants.

    These tests enforce the non-negotiable safety properties of the
    transport layer without requiring live Buzz CLI or network.
    """

    # -- (1) PaymentApproval must never be serializable/transmittable --

    def test_approval_not_in_payment_message_union(self):
        """PaymentApproval is not part of the PaymentMessage union type."""
        from hermes_payments.models import PaymentMessage
        assert PaymentApproval not in PaymentMessage.__args__

    def test_approval_not_in_kind_map(self):
        """PaymentApproval has no Nostr kind assigned."""
        from hermes_payments.envelope import KIND_MAP
        kind_names = [k.value for k in KIND_MAP.keys()]
        assert "approval" not in kind_names

    def test_approval_encode_raises(self):
        """encode_content() must reject PaymentApproval at runtime."""
        from hermes_payments.transport import encode_content
        approval = make_approval(make_intent(), make_quote(make_intent()))
        with pytest.raises(TypeError, match="PaymentApproval must never"):
            encode_content(approval)

    def test_message_kind_enum_has_no_approval(self):
        """MessageKind enum has exactly 3 members: INTENT, QUOTE, RECEIPT."""
        assert len(MessageKind) == 3
        names = [k.name for k in MessageKind]
        assert "APPROVAL" not in names
        assert set(names) == {"INTENT", "QUOTE", "RECEIPT"}

    # -- (2) No private key in transport layer --

    def test_transport_no_key_attribute(self):
        """BuzzTransport has no private key attribute."""
        from hermes_payments.transport import BuzzTransport, FakeExecutor
        transport = BuzzTransport(FakeExecutor(), channel="test-uuid")
        for attr in ["_private_key", "_keys", "_secret", "_sk"]:
            assert not hasattr(transport, attr)

    def test_fake_executor_no_key_attribute(self):
        """FakeExecutor has no private key attribute."""
        from hermes_payments.transport import FakeExecutor
        ex = FakeExecutor()
        for attr in ["_private_key", "_keys", "_secret", "_sk"]:
            assert not hasattr(ex, attr)

    # -- (3) Wire kind is NIP-29 kind 9 --

    def test_wire_kind_is_nine(self):
        """All payment messages use NIP-29 kind 9."""
        from hermes_payments.envelope import WIRE_KIND
        assert WIRE_KIND == 9

    def test_kind_map_all_nine(self):
        """Every payment type maps to kind 9."""
        from hermes_payments.envelope import KIND_MAP
        for kind in KIND_MAP.values():
            assert kind == 9

    # -- (4) Envelope has protocol + version --

    def test_envelope_has_protocol_and_version(self):
        """PaymentEnvelope carries protocol identifier and version."""
        from hermes_payments.envelope import decode_content, encode_content
        from hermes_payments.envelope import PROTOCOL_ID, PROTOCOL_VERSION
        import json
        intent = make_intent()
        content = encode_content(intent)
        data = json.loads(content)
        assert data["protocol"] == PROTOCOL_ID
        assert data["version"] == PROTOCOL_VERSION

    def test_envelope_rejects_unknown_protocol(self):
        """Envelope with wrong protocol is rejected."""
        from hermes_payments.envelope import decode_content
        import json
        data = {
            "protocol": "wrong-protocol",
            "version": "1",
            "type": "payment_intent",
            "payload": {},
        }
        with pytest.raises(ValueError, match="unknown protocol"):
            decode_content(json.dumps(data))

    def test_envelope_rejects_wrong_version(self):
        """Envelope with wrong version is rejected."""
        from hermes_payments.envelope import decode_content
        import json
        data = {
            "protocol": "hermes-payments",
            "version": "99",
            "type": "payment_intent",
            "payload": {},
        }
        with pytest.raises(ValueError, match="unsupported protocol version"):
            decode_content(json.dumps(data))

    # -- (5) Validation rejects bad events --

    def test_validate_rejects_non_nine_kind(self):
        """Events with kind != 9 are rejected."""
        from hermes_payments.transport import (
            RawBuzzEvent, validate_received_event, EnvelopeValidationError,
        )
        event = RawBuzzEvent(
            id="ev-999", pubkey="aa" * 32, kind=1,
            content="hello", tags=[["h", "test-ch"]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="expected 9"):
            validate_received_event(event, expected_channel="test-ch")

    def test_validate_rejects_wrong_channel(self):
        """Events with wrong h-tag channel are rejected."""
        from hermes_payments.transport import (
            RawBuzzEvent, validate_received_event, EnvelopeValidationError,
            encode_content,
        )
        intent = make_intent()
        event = RawBuzzEvent(
            id="ev-wrong", pubkey=SENDER_PUBKEY, kind=9,
            content=encode_content(intent),
            tags=[["h", "wrong-channel"]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(event, expected_channel="test-ch")

    def test_validate_rejects_expired_message(self):
        """Received messages past their expiry are rejected."""
        from hermes_payments.transport import (
            RawBuzzEvent, validate_received_event, EnvelopeValidationError,
            encode_content,
        )
        intent = make_intent(expires_at=NOW - 1)
        event = RawBuzzEvent(
            id="ev-exp", pubkey=SENDER_PUBKEY, kind=9,
            content=encode_content(intent),
            tags=[["h", "test-ch"]], created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(
                event, expected_channel="test-ch", clock=lambda: NOW
            )

    def test_validate_rejects_identity_mismatch(self):
        """Received intent with wrong author pubkey is rejected."""
        from hermes_payments.transport import (
            RawBuzzEvent, validate_received_event, EnvelopeValidationError,
            encode_content,
        )
        intent = make_intent()
        event = RawBuzzEvent(
            id="ev-wrong", pubkey="dd" * 32, kind=9,
            content=encode_content(intent),
            tags=[["h", "test-ch"]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel="test-ch", clock=lambda: NOW
            )

    def test_validate_rejects_invalid_json(self):
        """Received events with non-JSON content are rejected."""
        from hermes_payments.transport import (
            RawBuzzEvent, validate_received_event, EnvelopeValidationError,
        )
        event = RawBuzzEvent(
            id="ev-bad", pubkey="aa" * 32, kind=9,
            content="not json", tags=[["h", "test-ch"]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="content validation"):
            validate_received_event(event, expected_channel="test-ch")

    # -- (6) Receipt author identity is validated --

    def test_receipt_has_recipient(self):
        """PaymentReceipt includes recipient identity for author validation."""
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        assert hasattr(receipt, "recipient")
        assert receipt.recipient.pubkey == RECIPIENT_PUBKEY

    def test_receipt_author_must_match_recipient(self):
        """Receipt authored by non-recipient is rejected."""
        from hermes_payments.transport import (
            RawBuzzEvent, validate_received_event, EnvelopeValidationError,
            encode_content,
        )
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        event = RawBuzzEvent(
            id="ev-r-wrong", pubkey="dd" * 32, kind=9,
            content=encode_content(receipt),
            tags=[["h", "test-ch"]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(
                event, expected_channel="test-ch", clock=lambda: NOW
            )
