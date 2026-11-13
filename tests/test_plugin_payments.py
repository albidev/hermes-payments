"""Unit tests for the Hermes Payments plugin.

Exercise the plugin's PaymentService against an in-memory transport hub and a
fake Wavelength executor — no real Buzz relay, no real Signet. Focus on the
policy-enforcement contract:

  - wiring of identity/approver/transport/adapter
  - hp_pay does NOT move money
  - hp_prepare returns the full prepared_hash
  - hp_execute requires a real injected human approval gate and the exact hash
  - redacted prepared hashes are rejected
  - recipient accept_and_quote requires a real bolt11 invoice
"""
from __future__ import annotations

import time

import pytest
from hermes_payments_plugin import ConfigError, PaymentService, redacted

from hermes_payments.adapter import FakeWavecliExecutor, PrepareResult, Rail, ReconcileResult
from hermes_payments.models import (
    AgentIdentity,
    PaymentIntent,
    PaymentQuote,
    compute_id,
    compute_prepared_hash,
)
from hermes_payments.peer_transport import InMemoryTransportHub

ALICE_PK = "c55bd0f67c422e60bf9cd292d6c288795373c519e4b251946277ef0bc474d230"
BOB_PK = "8ad4f9b40038585c958ddec505bdbbdc5adea57fdac1c55a4ff0470048d25d41"


def make_fake_adapter(*, network: str = "regtest"):
    class FakeAdapter:
        rail = Rail.LIGHTNING

        def __init__(self):
            self._executor = FakeWavecliExecutor()
            self.prepare_calls = 0
            self.execute_calls = 0

        def prepare(self, *, receive_instruction, amount_sat, max_fee_sat):
            self.prepare_calls += 1
            return PrepareResult(
                fee_sat=0,
                prepared_hash=compute_prepared_hash(b"prepared-payload"),
                rail=Rail.LIGHTNING,
                prepared_payload=b"prepared-payload",
            )

        def execute(self, *, prepared_payload, prepared_hash):
            self.execute_calls += 1
            from hermes_payments.adapter import ExecuteResult
            return ExecuteResult(
                settlement_ref="payment-hash-" + "b" * 60,
                amount_sat=2100,
                fee_sat=0,
                rail=Rail.LIGHTNING,
            )

        def verify_receipt(self, *, settlement_ref, expected_amount_sat):
            from hermes_payments.adapter import ReceiptVerifyResult
            return ReceiptVerifyResult(
                verified=True,
                settlement_ref=settlement_ref,
                amount_sat=expected_amount_sat,
                fee_sat=0,
            )

        def reconcile_settlement(self, *, settlement_ref, amount_sat, max_fee_sat):
            return ReconcileResult(
                status="COMPLETE",
                settlement_ref=settlement_ref,
                amount_sat=amount_sat,
                fee_sat=0,
                rail=Rail.LIGHTNING,
            )

        def verify_sender_settlement(self, *, settlement_ref, expected_amount_sat):
            from hermes_payments.adapter import ReceiptVerifyResult
            return ReceiptVerifyResult(
                verified=True,
                settlement_ref=settlement_ref,
                amount_sat=expected_amount_sat,
                fee_sat=0,
                rail=Rail.LIGHTNING,
            )

    return FakeAdapter()


class ApprovalProbe:
    """Deterministic stand-in for Hermes' real human approval queue."""

    def __init__(self, decision: bool = True):
        self.decision = decision
        self.requests = []

    def __call__(self, **request):
        self.requests.append(request)
        return self.decision


class FakeTransport:
    """Minimal in-memory transport backed by a shared hub, mimicking PeerTransport."""

    def __init__(self, peer_id, identity, hub):
        self._impl = hub.connect(peer_id=peer_id, identity=identity)

    def send(self, message) -> str:
        return self._impl.send(message)

    def receive(self, *, limit=None):
        return self._impl.receive(limit=limit)


@pytest.fixture
def two_peers(tmp_path):
    hub = InMemoryTransportHub()

    alice_adapter = make_fake_adapter()
    bob_adapter = make_fake_adapter()
    alice_approval = ApprovalProbe()

    alice = PaymentService(
        identity=AgentIdentity(pubkey=ALICE_PK, relay_url=None),
        approver=AgentIdentity(pubkey=ALICE_PK, relay_url=None),
        transport=FakeTransport("alice", AgentIdentity(pubkey=ALICE_PK, relay_url=None), hub),
        adapter=alice_adapter,
        channel="chan",
        state_root=tmp_path / "alice",
        network="regtest",
        approval_gate=alice_approval,
    )
    bob = PaymentService(
        identity=AgentIdentity(pubkey=BOB_PK, relay_url=None),
        approver=AgentIdentity(pubkey=BOB_PK, relay_url=None),
        transport=FakeTransport("bob", AgentIdentity(pubkey=BOB_PK, relay_url=None), hub),
        adapter=bob_adapter,
        channel="chan",
        state_root=tmp_path / "bob",
        network="regtest",
    )
    return alice, bob, alice_adapter, bob_adapter


def _full_intent_id(pay_result: dict) -> str:
    return pay_result["full_intent_id"]


def test_wiring_exposes_service(two_peers):
    alice, _, _, _ = two_peers
    assert alice._identity.pubkey == ALICE_PK
    assert alice._network == "regtest"


def test_from_env_rejects_bad_config(monkeypatch):
    monkeypatch.delenv("HP_ROLE", raising=False)
    with pytest.raises(ConfigError):
        PaymentService.from_env({})
    with pytest.raises(ConfigError):
        PaymentService.from_env({"HP_ROLE": "alice", "HP_PUBKEY": "short"})
    with pytest.raises(ConfigError):
        PaymentService.from_env(
            {"HP_ROLE": "alice", "HP_PUBKEY": ALICE_PK, "HP_APPROVER_PUBKEY": ALICE_PK,
             "HP_CHANNEL": "chan", "HP_STATE_ROOT": "/tmp/x", "HP_NETWORK": "mainnet"}
        )
    with pytest.raises(ConfigError):
        PaymentService.from_env(
            {"HP_ROLE": "alice", "HP_PUBKEY": ALICE_PK, "HP_APPROVER_PUBKEY": ALICE_PK,
             "HP_CHANNEL": "chan", "HP_STATE_ROOT": "/tmp/x", "HP_NETWORK": "signet"}
        )


def test_pay_does_not_execute(two_peers):
    alice, _, _, alice_adapter = two_peers
    result = alice.pay(
        recipient_pubkey=BOB_PK,
        amount_sat=2100,
        purpose="test",
        max_fee_sat=10,
        expires_at=int(time.time()) + 600,
        idempotency_key="k1",
    )
    assert result["state"] == "submitted"
    assert alice_adapter.prepare_calls == 0
    assert alice_adapter.execute_calls == 0


def test_full_flow_requires_approval(two_peers):
    alice, bob, alice_adapter, _ = two_peers

    # Alice submits intent
    pay = alice.pay(
        recipient_pubkey=BOB_PK, amount_sat=2100, purpose="test",
        max_fee_sat=10, expires_at=int(time.time()) + 600, idempotency_key="k2",
    )
    intent_id = _full_intent_id(pay)

    # Bob polls, accepts, quotes
    bob.poll()
    q_result = bob.accept_and_quote(intent_id=intent_id, invoice="lnbc2100...")
    # The full quote id must be exposed for cross-process coordination (P7).
    assert "full_quote_id" in q_result
    assert q_result["full_quote_id"].startswith("q-")

    # Alice polls the quote
    alice.poll()
    # Locate the quote_id by matching the invoice
    quote_id = None
    for m in alice._inbox:
        if isinstance(m.message, PaymentQuote):
            quote_id = m.message.quote_id
    assert quote_id

    # Prepare (dry run) returns full hash
    prep = alice.prepare(quote_id=quote_id)
    assert prep["state"] == "prepared"
    assert len(prep["full_prepared_hash"]) == 64
    assert alice_adapter.execute_calls == 0

    # The gate denies → fail-closed. The model cannot override this with a
    # boolean argument because hp_execute no longer accepts one.
    alice._approval_gate = ApprovalProbe(decision=False)
    no_approve = alice.execute(
        intent_id=intent_id, prepared_hash=prep["full_prepared_hash"]
    )
    assert "human approval" in no_approve["error"]
    assert alice_adapter.execute_calls == 0

    # Execute with wrong hash → rejected before asking the human.
    with pytest.raises(ValueError):
        alice.execute(intent_id=intent_id, prepared_hash="x" * 64)
    assert alice_adapter.execute_calls == 0

    # Execute with redacted hash → rejected
    with pytest.raises(ValueError):
        alice.execute(intent_id=intent_id, prepared_hash=redacted(prep["full_prepared_hash"]))
    assert alice_adapter.execute_calls == 0

    # Only the injected human gate can authorize the exact triple.
    alice._approval_gate = ApprovalProbe(decision=True)
    exec_res = alice.execute(
        intent_id=intent_id, prepared_hash=prep["full_prepared_hash"]
    )
    assert exec_res["state"] == "settled"
    assert alice_adapter.execute_calls == 1


def test_payment_state_survives_restart(two_peers, tmp_path):
    alice, _, alice_adapter, _ = two_peers
    pay = alice.pay(
        recipient_pubkey=BOB_PK, amount_sat=2100, purpose="restart",
        max_fee_sat=10, expires_at=int(time.time()) + 600, idempotency_key="restart",
    )
    intent_id = _full_intent_id(pay)

    # Build the quote directly in the sender's policy engine for this
    # persistence-focused test.
    quote = PaymentQuote(
        id="placeholder",
        intent_id=intent_id,
        quote_id="q-restart",
        recipient=AgentIdentity(pubkey=BOB_PK, relay_url=None),
        receive_instruction={"rail": Rail.LIGHTNING, "invoice": "lnbc2100"},
        fee_sat=0,
        expires_at=int(time.time()) + 600,
        created_at=int(time.time()),
    )
    quote.id = compute_id(quote)
    alice._orchestrator.receive_quote(quote)
    prepared = alice._orchestrator.prepare(intent_id=intent_id)
    assert prepared.prepared_hash

    restarted = PaymentService(
        identity=AgentIdentity(pubkey=ALICE_PK, relay_url=None),
        approver=AgentIdentity(pubkey=ALICE_PK, relay_url=None),
        transport=alice._peer._transport,
        adapter=alice_adapter,
        channel="chan",
        state_root=tmp_path / "alice",
        network="regtest",
        approval_gate=ApprovalProbe(decision=True),
    )
    assert restarted._orchestrator.state(intent_id).value == "prepared"
    result = restarted.execute(intent_id=intent_id, prepared_hash=prepared.prepared_hash)
    assert result["state"] == "settled"


def test_accept_quote_requires_bolt11(two_peers):
    _, bob, _, _ = two_peers
    # Seed Bob's inbox with an intent directly (the hub would reject a send
    # authored by Alice through Bob's transport, so bypass the hub).
    hub_intent = PaymentIntent(
        id="placeholder", idempotency_key="k3",
        sender=AgentIdentity(pubkey=ALICE_PK, relay_url=None),
        recipient=AgentIdentity(pubkey=BOB_PK, relay_url=None),
        amount_sat=100, purpose="x", max_fee_sat=5,
        expires_at=int(time.time()) + 600, created_at=int(time.time()),
    )
    hub_intent.id = compute_id(hub_intent)
    from hermes_payments.peer_transport import PeerMessage
    bob._inbox.append(
        PeerMessage(
            message_id="m-intent-1",
            message=hub_intent,
            author=AgentIdentity(pubkey=ALICE_PK, relay_url=None),
            published_at=int(time.time()),
        )
    )
    bob._inbox_ids.add("m-intent-1")
    intent_id = hub_intent.id

    with pytest.raises(ValueError):
        bob.accept_and_quote(intent_id=intent_id, invoice="not-an-invoice")


def test_reconcile_unknown_intent_fails_closed(two_peers):
    alice, _, _, _ = two_peers
    from hermes_payments.policy import UnknownIntent
    with pytest.raises(UnknownIntent):
        alice.reconcile(intent_id="nonexistent")


def test_redacted_shortens_only_long(two_peers):
    assert redacted("short") == "short"
    assert len(redacted("a" * 64)) == 13
