"""
Hermes Payments — P5 TWO-HERMES end-to-end deterministic integration test.

This is a DETERMINISTIC INTEGRATION PROOF, NOT a live regtest proof.
No waved daemon, no Docker, no Buzz CLI, no network, no subprocess.
All seams are FakeExecutor / FakeWavecliExecutor (already implemented in
transport.py and adapter.py).

The test proves protocol composition across two independent Hermes instances
(Alice = sender, Bob = recipient) sharing a single Buzz channel.
Two settlement paths are demonstrated:

PATH A — ADAPTER-COMPLETE SETTLEMENT:
  Alice                                    Bob
  ─────                                    ───
  create PaymentIntent ────Buzz────►  validate as untrusted
                                     create PaymentQuote
  validate quote ◄────Buzz────
  WavelengthAdapter.prepare() (fake)
  local PaymentApproval (never sent)
  WavelengthAdapter.execute() (fake) → COMPLETE → SETTLED
                                     verify recv activity (fake) ✓
                                     publish PaymentReceipt ◄────Buzz

PATH B — RECEIPT-MEDIATED SETTLEMENT (RECONCILIATION_REQUIRED):
  Alice                                    Bob
  ─────                                    ───
  create PaymentIntent ────Buzz────►  validate as untrusted
                                     create PaymentQuote
  validate quote ◄────Buzz────
  WavelengthAdapter.prepare() (fake)
  local PaymentApproval (never sent)
  WavelengthAdapter.execute() (fake) → PENDING → RECONCILIATION_REQUIRED
                                     verify recv activity (fake) ✓
  receive receipt ◄────Buzz────
  validate receipt → SETTLED ✓

Relay simulation: each party has its own FakeExecutor (like separate relay
connections). After each send, events are manually injected into the
counterparty's executor with the correct author pubkey — this simulates
relay delivery while keeping authorship validation deterministic.

Source-grounded surfaces used (all existing):
  - FakeExecutor (transport.py) — in-memory Buzz CLI seam
  - FakeWavecliExecutor (adapter.py) — in-memory wavecli seam
  - WavelengthAdapter (adapter.py) — settlement adapter
  - PaymentOrchestrator (policy.py) — policy engine
  - BuzzTransport (transport.py) — channel-scoped transport
  - validate_received_event (transport.py) — untrusted message validation
  - encode_content / decode_content (envelope.py) — wire codec
"""
from __future__ import annotations

import pytest

from hermes_payments.adapter import (
    FakeWavecliExecutor,
    WavelengthAdapter,
)
from hermes_payments.envelope import encode_content
from hermes_payments.models import (
    BuzzIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    PaymentReceipt,
    Rail,
    compute_id,
)
from hermes_payments.policy import (
    PaymentOrchestrator,
    StateError,
)
from hermes_payments.transport import (
    BuzzTransport,
    EnvelopeValidationError,
    FakeExecutor,
    RawBuzzEvent,
    validate_received_event,
)
from tests.fixtures import (
    APPROVER_PUBKEY,
    NOW,
    RECIPIENT_PUBKEY,
    SENDER_PUBKEY,
    make_intent,
    make_quote,
    make_receipt,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHANNEL_UUID = "550e8400-e29b-41d4-a716-446655440000"
FAR_FUTURE_EXPIRY = 4102444800  # 2100-01-01T00:00:00Z — intent never expires
PAYMENT_HASH = "aa" * 32
PAYMENT_AMOUNT = 2100
FEE_SAT = 10
BOB_PUBKEY = RECIPIENT_PUBKEY  # "bb" * 32 — Bob is recipient in fixtures
ALICE_PUBKEY = SENDER_PUBKEY  # "aa" * 32 — Alice is sender in fixtures
APPROVER_PUB = APPROVER_PUBKEY  # "cc" * 32

# ---------------------------------------------------------------------------
# Shared raw RPC response data (deterministic, far-future expiry)
# ---------------------------------------------------------------------------

RAW_PREPARE_RESPONSE = {
    "send_intent_id": "si-e2e-test-001",
    "amount_sat": PAYMENT_AMOUNT,
    "expected_fee_sat": FEE_SAT,
    "fee_known": True,
    "expected_total_outflow_sat": PAYMENT_AMOUNT + FEE_SAT,
    "total_outflow_known": True,
    "rail": "SEND_RAIL_LIGHTNING",
    "payment_hash": PAYMENT_HASH,
    "expires_at_unix": FAR_FUTURE_EXPIRY,
    "quote_status": "SEND_QUOTE_STATUS_COMPLETE",
}

RAW_SEND_RESPONSE_COMPLETE = {
    "entry": {
        "id": PAYMENT_HASH,
        "status": "ENTRY_STATUS_COMPLETE",
        "kind": "ENTRY_KIND_SEND",
        "amount_sat": PAYMENT_AMOUNT,
        "fee_sat": FEE_SAT,
        "payment_hash": PAYMENT_HASH,
    },
    "actual_amount_sat": PAYMENT_AMOUNT,
}

RAW_SEND_RESPONSE_PENDING = {
    "entry": {
        "id": PAYMENT_HASH,
        "status": "ENTRY_STATUS_PENDING",
        "kind": "ENTRY_KIND_SEND",
        "amount_sat": PAYMENT_AMOUNT,
        "fee_sat": FEE_SAT,
        "payment_hash": PAYMENT_HASH,
    },
    "actual_amount_sat": PAYMENT_AMOUNT,
}

RECV_ACTIVITY_RESPONSE = {
    "entries": [
        {
            "status": "ENTRY_STATUS_COMPLETE",
            "kind": "RECV",
            "amount_sat": PAYMENT_AMOUNT,
            "fee_sat": 0,
            "progress": {"payment_hash": PAYMENT_HASH},
        },
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wavelength_adapter(
    executor: FakeWavecliExecutor,
    rpc_server: str = "localhost:10029",
) -> WavelengthAdapter:
    """Build a WavelengthAdapter backed by a FakeWavecliExecutor."""
    return WavelengthAdapter(
        executor=executor,
        rpc_server=rpc_server,
        network="regtest",
        no_tls=True,
        no_macaroons=True,
    )


def _make_orchestrator(
    adapter: WavelengthAdapter,
    *,
    clock=None,
    store_path=None,
    audit_path=None,
) -> PaymentOrchestrator:
    """Build a PaymentOrchestrator with a frozen clock."""
    return PaymentOrchestrator(
        adapter=adapter,
        store_path=store_path,
        audit_path=audit_path,
        clock=clock or (lambda: NOW),
    )


def _relay_last_event(
    source_exec: FakeExecutor,
    dest_exec: FakeExecutor,
    *,
    channel: str,
    author_pubkey: str,
) -> None:
    """Simulate relay delivery: copy the last event from source to dest
    with the correct author pubkey."""
    events = source_exec.get(channel=channel)
    if not events:
        return
    last = events[-1]
    dest_exec.inject_event(
        RawBuzzEvent(
            id=last.id,
            pubkey=author_pubkey,
            kind=last.kind,
            content=last.content,
            tags=list(last.tags),
            created_at=last.created_at,
        )
    )


def _drive_alice_to_prepared(alice_orch, intent):
    """Drive Alice's intent through DRAFT → PREPARED.

    Returns (quote, prepared).
    """
    alice_orch.submit(intent)
    quote = make_quote(intent, fee_sat=FEE_SAT, expires_at=FAR_FUTURE_EXPIRY)
    alice_orch.receive_quote(quote)
    prepared = alice_orch.prepare()
    return quote, prepared


def _approve_and_drive_to_settled(alice_orch, intent, quote, prepared):
    """Apply approval, execute with COMPLETE adapter, return receipt."""
    approval = PaymentApproval(
        id="placeholder",
        intent_id=intent.id,
        quote_id=quote.quote_id,
        prepared_hash=prepared.prepared_hash,
        approver=BuzzIdentity(pubkey=APPROVER_PUB, relay_url=None),
        created_at=NOW,
    )
    approval.id = compute_id(approval)
    alice_orch.approve(approval)
    receipt = alice_orch.execute()
    return approval, receipt


def _drive_to_reconciliation(alice_orch, intent, quote, prepared):
    """Apply approval, execute with PENDING adapter → RECONCILIATION_REQUIRED."""
    approval = PaymentApproval(
        id="placeholder",
        intent_id=intent.id,
        quote_id=quote.quote_id,
        prepared_hash=prepared.prepared_hash,
        approver=BuzzIdentity(pubkey=APPROVER_PUB, relay_url=None),
        created_at=NOW,
    )
    approval.id = compute_id(approval)
    alice_orch.approve(approval)
    with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
        alice_orch.execute()


# ---------------------------------------------------------------------------
# Path A: Adapter-Complete Settlement
# ---------------------------------------------------------------------------


def _run_path_a():
    """Full Path A: adapter COMPLETE → Alice SETTLED, Bob verifies receipt.

    Returns all artifacts for assertion.
    """
    # --- Alice stack: adapter returns PREPARE then COMPLETE ---
    alice_wavecli = FakeWavecliExecutor()
    alice_wavecli.set_responses(RAW_PREPARE_RESPONSE, RAW_SEND_RESPONSE_COMPLETE)
    alice_adapter = _make_wavelength_adapter(alice_wavecli)
    alice_orch = _make_orchestrator(adapter=alice_adapter, clock=lambda: NOW)
    alice_buzz = FakeExecutor()
    alice_transport = BuzzTransport(alice_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)

    # --- Bob stack: adapter has recv activity for verification ---
    bob_wavecli = FakeWavecliExecutor()
    bob_wavecli.set_response(RECV_ACTIVITY_RESPONSE)
    bob_wavecli.set_response(RECV_ACTIVITY_RESPONSE)  # second for test assertion
    bob_adapter = _make_wavelength_adapter(bob_wavecli)
    bob_orch = _make_orchestrator(adapter=bob_adapter, clock=lambda: NOW)
    bob_buzz = FakeExecutor()
    bob_transport = BuzzTransport(bob_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)

    # Step 1: Alice creates and submits intent
    intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
    alice_orch.submit(intent)

    # Step 2: Alice sends intent via Buzz → relay → Bob
    alice_transport.send_intent(intent)
    _relay_last_event(alice_buzz, bob_buzz, channel=CHANNEL_UUID, author_pubkey=ALICE_PUBKEY)

    # Bob receives and validates intent as untrusted
    bob_received = bob_transport.receive_messages()
    intents = [m for m in bob_received if isinstance(m, PaymentIntent)]
    assert len(intents) == 1
    received_intent = intents[0]
    assert received_intent.id == intent.id
    assert received_intent.amount_sat == PAYMENT_AMOUNT

    # Step 3: Bob creates a 2,100 sat PaymentQuote and sends it back
    bob_quote = make_quote(intent, fee_sat=FEE_SAT, expires_at=FAR_FUTURE_EXPIRY)
    bob_transport.send_quote(bob_quote)
    _relay_last_event(bob_buzz, alice_buzz, channel=CHANNEL_UUID, author_pubkey=BOB_PUBKEY)

    # Alice receives Bob's quote (filter for Quote type)
    alice_received = alice_transport.receive_messages()
    quotes = [m for m in alice_received if isinstance(m, PaymentQuote)]
    assert len(quotes) == 1
    received_quote = quotes[0]
    alice_orch.receive_quote(received_quote)

    # Step 4: Alice adapter.prepare() → binding token
    prepared = alice_orch.prepare()
    assert prepared.fee_sat == FEE_SAT
    assert prepared.prepared_hash
    assert isinstance(prepared.prepared_payload, bytes)

    # Step 4b: LOCAL PaymentApproval (never sent)
    approval = PaymentApproval(
        id="placeholder",
        intent_id=intent.id,
        quote_id=bob_quote.quote_id,
        prepared_hash=prepared.prepared_hash,
        approver=BuzzIdentity(pubkey=APPROVER_PUB, relay_url=None),
        created_at=NOW,
    )
    approval.id = compute_id(approval)
    assert approval.intent_id == intent.id
    assert approval.quote_id == bob_quote.quote_id
    assert approval.prepared_hash == prepared.prepared_hash
    with pytest.raises(TypeError, match="must never be serialized"):
        encode_content(approval)
    alice_orch.approve(approval)

    # Step 5: Alice execute() → adapter consumes exact ID → COMPLETE → SETTLED
    receipt = alice_orch.execute()
    assert receipt is not None
    assert receipt.intent_id == intent.id
    assert receipt.amount_sat == PAYMENT_AMOUNT
    assert receipt.settlement_ref == PAYMENT_HASH
    assert alice_orch.state(intent.id).value == "settled"

    # Step 6: Bob verifies recv activity (fake) matches
    verify = bob_adapter.verify_receipt(
        settlement_ref=PAYMENT_HASH,
        expected_amount_sat=PAYMENT_AMOUNT,
    )
    assert verify.verified is True
    assert verify.settlement_ref == PAYMENT_HASH
    assert verify.amount_sat == PAYMENT_AMOUNT

    # Step 7: Bob publishes PaymentReceipt via Buzz
    bob_receipt = make_receipt(
        intent, bob_quote,
        settlement_ref=PAYMENT_HASH,
        fee_sat=0, created_at=NOW, settled_at=NOW,
    )
    bob_transport.send_receipt(bob_receipt)
    _relay_last_event(bob_buzz, alice_buzz, channel=CHANNEL_UUID, author_pubkey=BOB_PUBKEY)

    # Alice receives Bob's receipt (validates content — she's already SETTLED)
    alice_msgs = alice_transport.receive_messages()
    receipts = [m for m in alice_msgs if isinstance(m, PaymentReceipt)]
    assert len(receipts) == 1
    received_receipt = receipts[0]
    assert received_receipt.intent_id == intent.id
    assert received_receipt.settlement_ref == PAYMENT_HASH
    assert received_receipt.amount_sat == PAYMENT_AMOUNT

    return {
        "intent": intent,
        "quote": received_quote,
        "prepared": prepared,
        "approval": approval,
        "receipt": receipt,
        "alice_orch": alice_orch,
        "bob_orch": bob_orch,
        "bob_receipt": bob_receipt,
        "alice_buzz": alice_buzz,
        "bob_buzz": bob_buzz,
    }


# ---------------------------------------------------------------------------
# Path B: Receipt-Mediated Settlement (RECONCILIATION_REQUIRED → receipt → SETTLED)
# ---------------------------------------------------------------------------


def _run_path_b():
    """Full Path B: adapter PENDING → RECONCILIATION_REQUIRED → receipt → SETTLED.

    Returns all artifacts for assertion.
    """
    # --- Alice stack: adapter returns PREPARE then PENDING ---
    alice_wavecli = FakeWavecliExecutor()
    alice_wavecli.set_responses(RAW_PREPARE_RESPONSE, RAW_SEND_RESPONSE_PENDING)
    alice_wavecli.set_response(RECV_ACTIVITY_RESPONSE)  # for receive_receipt → verify_receipt
    alice_adapter = _make_wavelength_adapter(alice_wavecli)
    alice_orch = _make_orchestrator(adapter=alice_adapter, clock=lambda: NOW)
    alice_buzz = FakeExecutor()
    alice_transport = BuzzTransport(alice_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)

    # --- Bob stack: adapter has recv activity ---
    bob_wavecli = FakeWavecliExecutor()
    bob_wavecli.set_response(RECV_ACTIVITY_RESPONSE)
    bob_wavecli.set_response(RECV_ACTIVITY_RESPONSE)  # second for test assertion
    bob_adapter = _make_wavelength_adapter(bob_wavecli)
    bob_orch = _make_orchestrator(adapter=bob_adapter, clock=lambda: NOW)
    bob_buzz = FakeExecutor()
    bob_transport = BuzzTransport(bob_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)

    # Steps 1-2: Alice sends intent → Bob receives
    intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
    alice_orch.submit(intent)
    alice_transport.send_intent(intent)
    _relay_last_event(alice_buzz, bob_buzz, channel=CHANNEL_UUID, author_pubkey=ALICE_PUBKEY)

    bob_received = bob_transport.receive_messages()
    assert any(isinstance(m, PaymentIntent) for m in bob_received)

    # Step 3: Bob creates quote → Alice receives
    bob_quote = make_quote(intent, fee_sat=FEE_SAT, expires_at=FAR_FUTURE_EXPIRY)
    bob_transport.send_quote(bob_quote)
    _relay_last_event(bob_buzz, alice_buzz, channel=CHANNEL_UUID, author_pubkey=BOB_PUBKEY)
    alice_received = alice_transport.receive_messages()
    quotes = [m for m in alice_received if isinstance(m, PaymentQuote)]
    assert len(quotes) == 1
    alice_orch.receive_quote(quotes[0])

    # Step 4: Alice adapter.prepare() → token, approval (local)
    prepared = alice_orch.prepare()
    approval = PaymentApproval(
        id="placeholder", intent_id=intent.id, quote_id=bob_quote.quote_id,
        prepared_hash=prepared.prepared_hash,
        approver=BuzzIdentity(pubkey=APPROVER_PUB, relay_url=None),
        created_at=NOW,
    )
    approval.id = compute_id(approval)
    alice_orch.approve(approval)

    # Step 5: Alice execute() → adapter PENDING → RECONCILIATION_REQUIRED
    with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
        alice_orch.execute()
    assert alice_orch.state(intent.id).value == "reconciliation_required"

    # Step 6: Bob verifies recv activity → confirmed
    verify = bob_adapter.verify_receipt(
        settlement_ref=PAYMENT_HASH,
        expected_amount_sat=PAYMENT_AMOUNT,
    )
    assert verify.verified is True

    # Step 7: Bob publishes receipt → Buzz → Alice
    bob_receipt = make_receipt(
        intent, bob_quote,
        settlement_ref=PAYMENT_HASH,
        fee_sat=0, created_at=NOW, settled_at=NOW,
    )
    bob_transport.send_receipt(bob_receipt)
    _relay_last_event(bob_buzz, alice_buzz, channel=CHANNEL_UUID, author_pubkey=BOB_PUBKEY)

    # Alice receives receipt → validate_received_event → receive_receipt → SETTLED
    alice_msgs = alice_transport.receive_messages()
    receipts = [m for m in alice_msgs if isinstance(m, PaymentReceipt)]
    assert len(receipts) == 1
    confirmed_receipt = alice_orch.receive_receipt(receipts[0])
    assert confirmed_receipt.intent_id == intent.id
    assert alice_orch.state(intent.id).value == "settled"

    return {
        "intent": intent,
        "quote": quotes[0],
        "prepared": prepared,
        "approval": approval,
        "receipt": confirmed_receipt,
        "alice_orch": alice_orch,
        "bob_orch": bob_orch,
        "bob_receipt": bob_receipt,
        "alice_buzz": alice_buzz,
        "bob_buzz": bob_buzz,
    }


# =========================================================================
# PATH A — ADAPTER-COMPLETE SETTLED
# =========================================================================


class TestPathAAdapterComplete:
    """Path A: Alice settles via adapter COMPLETE. Bob verifies independently."""

    def test_full_flow_to_settled(self):
        """Alice sends 2,100 sats; adapter returns COMPLETE; Alice → SETTLED."""
        a = _run_path_a()
        assert a["alice_orch"].state(a["intent"].id).value == "settled"

    def test_intent_amount_matches_quote(self):
        """Bob's quote references Alice's exact intent."""
        a = _run_path_a()
        assert a["quote"].intent_id == a["intent"].id
        assert a["quote"].fee_sat == FEE_SAT

    def test_approval_binds_exact_triple(self):
        """PaymentApproval binds (intent_id, quote_id, prepared_hash)."""
        a = _run_path_a()
        ap = a["approval"]
        assert ap.intent_id == a["intent"].id
        assert ap.quote_id == a["quote"].quote_id
        assert ap.prepared_hash == a["prepared"].prepared_hash

    def test_approval_never_in_buzz(self):
        """PaymentApproval never appears in FakeExecutor sent list."""
        a = _run_path_a()
        for _ch, content in a["alice_buzz"].sent:
            assert "payment_approval" not in content.lower()
            assert "Approval" not in content

    def test_settlement_ref_is_payment_hash(self):
        """Receipt settlement_ref matches the payment_hash from prepare."""
        a = _run_path_a()
        assert a["receipt"].settlement_ref == PAYMENT_HASH

    def test_adapter_execute_consumes_exact_id(self):
        """Execute consumes the exact send_intent_id from prepare."""
        a = _run_path_a()
        import json
        payload = json.loads(a["prepared"].prepared_payload)
        assert payload["send_intent_id"] == "si-e2e-test-001"

    def test_adapter_prepare_returns_binding_token(self):
        """WavelengthAdapter.prepare() returns a deterministic prepared_hash."""
        a = _run_path_a()
        assert len(a["prepared"].prepared_hash) == 64  # SHA-256 hex
        assert isinstance(a["prepared"].prepared_payload, bytes)

    def test_bob_verify_receipt_matches(self):
        """Bob's adapter verify_receipt returns verified=True for correct hash."""
        a = _run_path_a()
        verify = a["bob_orch"]._adapter.verify_receipt(
            settlement_ref=PAYMENT_HASH,
            expected_amount_sat=PAYMENT_AMOUNT,
        )
        assert verify.verified is True

    def test_bob_validates_intent_as_untrusted(self):
        """Bob validates Alice's intent via validate_received_event (not policy)."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-e2e", pubkey=ALICE_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        msg = validate_received_event(
            event, expected_channel=CHANNEL_UUID, clock=lambda: NOW
        )
        assert isinstance(msg, PaymentIntent)
        assert msg.id == intent.id

    def test_bob_creates_quote_with_lightning_rail(self):
        """Bob's quote specifies Lightning rail."""
        a = _run_path_a()
        assert a["quote"].receive_instruction.rail == Rail.LIGHTNING


# =========================================================================
# PATH B — RECEIPT-MEDIATED SETTLED
# =========================================================================


class TestPathBReceiptMediated:
    """Path B: adapter PENDING → RECONCILIATION_REQUIRED → receipt → SETTLED."""

    def test_full_flow_to_settled_via_receipt(self):
        """Adapter returns PENDING; Bob's receipt transitions Alice → SETTLED."""
        a = _run_path_b()
        assert a["alice_orch"].state(a["intent"].id).value == "settled"

    def test_receipt_settles_from_reconciliation(self):
        """receive_receipt transitions RECONCILIATION_REQUIRED → SETTLED."""
        a = _run_path_b()
        assert a["receipt"].intent_id == a["intent"].id
        assert a["receipt"].settlement_ref == PAYMENT_HASH

    def test_receipt_amount_matches_intent(self):
        """Bob's receipt amount matches Alice's original intent."""
        a = _run_path_b()
        assert a["receipt"].amount_sat == PAYMENT_AMOUNT

    def test_bob_verify_and_publish(self):
        """Bob verifies recv activity then publishes receipt via Buzz."""
        a = _run_path_b()
        # Bob's adapter verified successfully
        verify = a["bob_orch"]._adapter.verify_receipt(
            settlement_ref=PAYMENT_HASH,
            expected_amount_sat=PAYMENT_AMOUNT,
        )
        assert verify.verified is True
        # Receipt was sent via Buzz
        sent_events = [
            content for _ch, content in a["bob_buzz"].sent
        ]
        assert any(PAYMENT_HASH in c for c in sent_events)


# =========================================================================
# NEGATIVE: DUPLICATE / REPLAY
# =========================================================================


class TestDuplicateReplayRejection:
    """Duplicate or replayed intent/receipt cannot dispatch twice."""

    def test_duplicate_intent_not_received_twice(self):
        """The transport deduplicates a replayed event before policy input."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        alice_buzz = FakeExecutor()
        alice_transport = BuzzTransport(alice_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)
        bob_buzz = FakeExecutor()

        alice_transport.send_intent(intent)
        _relay_last_event(alice_buzz, bob_buzz, channel=CHANNEL_UUID, author_pubkey=ALICE_PUBKEY)
        _relay_last_event(alice_buzz, bob_buzz, channel=CHANNEL_UUID, author_pubkey=ALICE_PUBKEY)

        bob_transport = BuzzTransport(bob_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)
        received = bob_transport.receive_messages()
        assert len(received) == 1
        assert isinstance(received[0], PaymentIntent)
        received_intent = received[0]

        wavecli = FakeWavecliExecutor()
        adapter = WavelengthAdapter(executor=wavecli, network="regtest")
        orch = PaymentOrchestrator(adapter=adapter, clock=lambda: NOW)
        orch.submit(received_intent)
        assert orch.state(intent.id).value == "submitted"
        orch.submit(received_intent)  # policy remains idempotent if called again
        assert orch.state(intent.id).value == "submitted"

    def test_duplicate_receipt_cannot_settle_twice(self):
        """Second receipt for same intent is rejected by state gate (SETTLED);
        state stays SETTLED and receipt is never accepted."""
        a = _run_path_b()
        assert a["alice_orch"].state(a["intent"].id).value == "settled"
        # SETTLED state blocks receipt before replay check
        with pytest.raises(StateError, match="cannot receive receipt in state SETTLED"):
            a["alice_orch"].receive_receipt(a["bob_receipt"])
        assert a["alice_orch"].state(a["intent"].id).value == "settled"


# =========================================================================
# NEGATIVE: TAMPERED / EXPIRED CHANNEL EVENT
# =========================================================================


class TestTamperedExpiredRejection:
    """Tampered or expired channel events never reach the policy engine."""

    def test_wrong_kind_never_reaches_policy(self):
        """Kind 1 event is silently skipped by receive_messages."""
        bob_buzz = FakeExecutor()
        bob_transport = BuzzTransport(bob_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)
        bob_buzz.inject_event(
            RawBuzzEvent(
                id="ev-kind1", pubkey=ALICE_PUBKEY, kind=1,
                content="not a payment", tags=[["h", CHANNEL_UUID]], created_at=NOW,
            )
        )
        assert bob_transport.receive_messages() == []

    def test_wrong_channel_never_reaches_policy(self):
        """Event with wrong h-tag is silently skipped."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        bob_buzz = FakeExecutor()
        bob_transport = BuzzTransport(bob_buzz, channel=CHANNEL_UUID, clock=lambda: NOW)
        content = encode_content(intent)
        bob_buzz.inject_event(
            RawBuzzEvent(
                id="ev-wrong-ch", pubkey=ALICE_PUBKEY, kind=9,
                content=content, tags=[["h", "wrong-channel"]], created_at=NOW,
            )
        )
        assert bob_transport.receive_messages() == []

    def test_tampered_sender_pubkey_rejected(self):
        """Intent with wrong sender pubkey is rejected (identity validation)."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        tampered = intent.model_copy(
            update={"sender": BuzzIdentity(pubkey="dd" * 32, relay_url=None)}
        )
        tampered.id = compute_id(tampered)
        content = encode_content(tampered)
        event = RawBuzzEvent(
            id="ev-tampered", pubkey=ALICE_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="does not match"):
            validate_received_event(event, expected_channel=CHANNEL_UUID, clock=lambda: NOW)

    def test_expired_intent_rejected_on_receive(self):
        """Expired intent is rejected before reaching policy."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=NOW - 1)
        content = encode_content(intent)
        event = RawBuzzEvent(
            id="ev-expired", pubkey=ALICE_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(event, expected_channel=CHANNEL_UUID, clock=lambda: NOW)

    def test_expired_quote_rejected_on_receive(self):
        """Expired quote is rejected before reaching policy."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        quote = make_quote(intent, expires_at=NOW - 1, fee_sat=FEE_SAT)
        content = encode_content(quote)
        event = RawBuzzEvent(
            id="ev-exp-quote", pubkey=BOB_PUBKEY, kind=9,
            content=content, tags=[["h", CHANNEL_UUID]], created_at=NOW - 100,
        )
        with pytest.raises(EnvelopeValidationError, match="expired"):
            validate_received_event(event, expected_channel=CHANNEL_UUID, clock=lambda: NOW)

    def test_invalid_envelope_protocol_rejected(self):
        """Envelope with wrong protocol is rejected."""
        bad_content = '{"protocol":"wrong","version":"1","type":"payment_intent","payload":{}}'
        event = RawBuzzEvent(
            id="ev-bad-proto", pubkey=ALICE_PUBKEY, kind=9,
            content=bad_content, tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="content validation"):
            validate_received_event(event, expected_channel=CHANNEL_UUID, clock=lambda: NOW)

    def test_non_payment_kind_40100_rejected(self):
        """The old invalid kind 40100 must be rejected."""
        event = RawBuzzEvent(
            id="ev-40100", pubkey=ALICE_PUBKEY, kind=40100,
            content="{}", tags=[["h", CHANNEL_UUID]], created_at=NOW,
        )
        with pytest.raises(EnvelopeValidationError, match="expected 9"):
            validate_received_event(event, expected_channel=CHANNEL_UUID, clock=lambda: NOW)


# =========================================================================
# NEGATIVE: SEND PENDING / UNKNOWN → RECONCILIATION_REQUIRED
# =========================================================================


class TestPendingUnknownReconciliation:
    """Send PENDING or unknown keeps Alice RECONCILIATION_REQUIRED, emits no receipt."""

    def _make_orch_with_send_status(self, send_response):
        """Build orchestrator whose adapter returns the given send status."""
        wavecli = FakeWavecliExecutor()
        wavecli.set_responses(RAW_PREPARE_RESPONSE, send_response)
        adapter = _make_wavelength_adapter(wavecli)
        return _make_orchestrator(adapter=adapter, clock=lambda: NOW)

    def _drive_to_approved(self, orch):
        """Drive intent to APPROVED, ready for execute."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        orch.submit(intent)
        quote = make_quote(intent, fee_sat=FEE_SAT, expires_at=FAR_FUTURE_EXPIRY)
        orch.receive_quote(quote)
        prepared = orch.prepare()
        approval = PaymentApproval(
            id="placeholder", intent_id=intent.id, quote_id=quote.quote_id,
            prepared_hash=prepared.prepared_hash,
            approver=BuzzIdentity(pubkey=APPROVER_PUB, relay_url=None),
            created_at=NOW,
        )
        approval.id = compute_id(approval)
        orch.approve(approval)
        return intent

    def test_pending_send_keeps_reconciliation_required(self):
        """When raw Send returns PENDING, Alice → RECONCILIATION_REQUIRED."""
        orch = self._make_orch_with_send_status(RAW_SEND_RESPONSE_PENDING)
        intent = self._drive_to_approved(orch)
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            orch.execute()
        assert orch.state(intent.id).value == "reconciliation_required"

    def test_pending_send_emits_no_receipt(self):
        """RECONCILIATION_REQUIRED state means no receipt was created."""
        orch = self._make_orch_with_send_status(RAW_SEND_RESPONSE_PENDING)
        intent = self._drive_to_approved(orch)
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            orch.execute()
        rec = orch._intents.get(intent.id)
        assert rec is not None
        assert rec.receipt is None
        assert rec.state.value == "reconciliation_required"

    def test_unknown_send_status_keeps_reconciliation_required(self):
        """Unknown send status → AmbiguousResult → RECONCILIATION_REQUIRED."""
        orch = self._make_orch_with_send_status({
            "entry": {
                "id": PAYMENT_HASH, "status": "ENTRY_STATUS_UNKNOWN_WEIRD",
                "kind": "ENTRY_KIND_SEND", "amount_sat": PAYMENT_AMOUNT,
                "fee_sat": FEE_SAT, "payment_hash": PAYMENT_HASH,
            },
            "actual_amount_sat": PAYMENT_AMOUNT,
        })
        intent = self._drive_to_approved(orch)
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            orch.execute()
        assert orch.state(intent.id).value == "reconciliation_required"

    def test_reconciliation_requires_manual_confirm_or_receipt(self):
        """From RECONCILIATION_REQUIRED, only confirm_settled or receipt_received
        can resolve — other triggers are rejected."""
        orch = self._make_orch_with_send_status(RAW_SEND_RESPONSE_PENDING)
        intent = self._drive_to_approved(orch)
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            orch.execute()
        with pytest.raises(StateError):
            orch.cancel(intent.id)
        with pytest.raises(StateError):
            orch.execute()


# =========================================================================
# NEGATIVE: APPROVAL NEVER IN BUZZ
# =========================================================================


class TestApprovalNeverInBuzz:
    """PaymentApproval is never serialised or transmitted via Buzz."""

    def test_approval_not_in_buzz_sent_events(self):
        """FakeExecutor sent list contains no approval content."""
        a = _run_path_a()
        for _ch, content in a["alice_buzz"].sent:
            assert "payment_approval" not in content
            assert "Approval" not in content

    def test_encode_content_rejects_approval(self):
        """encode_content raises TypeError for PaymentApproval."""
        intent = make_intent(amount_sat=PAYMENT_AMOUNT, expires_at=FAR_FUTURE_EXPIRY)
        quote = make_quote(intent, fee_sat=FEE_SAT, expires_at=FAR_FUTURE_EXPIRY)
        approval = PaymentApproval(
            id="placeholder", intent_id=intent.id, quote_id=quote.quote_id,
            prepared_hash="aa" * 32,
            approver=BuzzIdentity(pubkey=APPROVER_PUB, relay_url=None),
            created_at=NOW,
        )
        approval.id = compute_id(approval)
        with pytest.raises(TypeError, match="must never be serialized"):
            encode_content(approval)


# =========================================================================
# NEGATIVE: RECEIPT VALIDATION FAILURES
# =========================================================================


class TestReceiptValidationFailures:
    """Receipt with mismatched payment hash or amount is rejected."""

    def test_receipt_wrong_payment_hash_rejected(self):
        """Receipt with wrong settlement_ref fails verify_receipt."""
        wavecli = FakeWavecliExecutor()
        wavecli.set_response(RECV_ACTIVITY_RESPONSE)
        adapter = _make_wavelength_adapter(wavecli)
        verify = adapter.verify_receipt(
            settlement_ref="wrong_hash" * 4,
            expected_amount_sat=PAYMENT_AMOUNT,
        )
        assert verify.verified is False

    def test_receipt_wrong_amount_rejected(self):
        """Receipt with wrong amount fails verify_receipt."""
        wavecli = FakeWavecliExecutor()
        wavecli.set_response(RECV_ACTIVITY_RESPONSE)
        adapter = _make_wavelength_adapter(wavecli)
        verify = adapter.verify_receipt(
            settlement_ref=PAYMENT_HASH,
            expected_amount_sat=9999,
        )
        assert verify.verified is False
        assert verify.error is not None
        assert "mismatch" in verify.error.lower()
