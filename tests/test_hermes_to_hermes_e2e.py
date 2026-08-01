"""Transport-neutral Hermes-to-Hermes integration proof.

This test intentionally imports no Buzz or Wavelength transport.  It composes
independent policy engines through two ``HermesPeer`` endpoints and an
``InMemoryTransportHub``.  Buzz is tested separately as one adapter of the
same peer contract.
"""
from __future__ import annotations

import pytest

from hermes_payments.adapter import AmbiguousResult
from hermes_payments.models import PaymentIntent, PaymentQuote, PaymentReceipt
from hermes_payments.peer import HermesPeer
from hermes_payments.peer_transport import InMemoryTransportHub
from hermes_payments.policy import PaymentOrchestrator, StateError
from tests.fixtures import (
    NOW,
    RECIPIENT_IDENTITY,
    SENDER_IDENTITY,
    make_approval,
    make_intent,
    make_quote,
    make_receipt,
)
from tests.test_policy_core import StubAdapter


def _build_peers(*, alice_execute_raises=None):
    hub = InMemoryTransportHub(clock=lambda: NOW)
    alice_adapter = StubAdapter(execute_raises=alice_execute_raises)
    bob_adapter = StubAdapter()
    alice_orchestrator = PaymentOrchestrator(
        adapter=alice_adapter,
        clock=lambda: NOW,
    )
    bob_orchestrator = PaymentOrchestrator(
        adapter=bob_adapter,
        clock=lambda: NOW,
    )
    alice = HermesPeer(
        identity=SENDER_IDENTITY,
        transport=hub.connect(peer_id="alice", identity=SENDER_IDENTITY),
        orchestrator=alice_orchestrator,
    )
    bob = HermesPeer(
        identity=RECIPIENT_IDENTITY,
        transport=hub.connect(peer_id="bob", identity=RECIPIENT_IDENTITY),
        orchestrator=bob_orchestrator,
    )
    return alice, bob, alice_orchestrator, bob_orchestrator, bob_adapter


def _exchange_intent_and_quote(alice: HermesPeer, bob: HermesPeer):
    intent = make_intent()
    quote = make_quote(intent)

    alice.submit_intent(intent)
    received_intent = bob.receive()[0].message
    assert isinstance(received_intent, PaymentIntent)
    bob.accept_intent(received_intent)
    bob.publish_quote(quote)
    received_quote = alice.receive()[0].message
    assert isinstance(received_quote, PaymentQuote)
    alice.accept_quote(received_quote)
    return intent, quote


class TestHermesToHermesTransportNeutralFlow:
    def test_two_independent_hermes_peers_settle_without_buzz(self):
        alice, bob, alice_orchestrator, bob_orchestrator, _ = _build_peers()
        intent, quote = _exchange_intent_and_quote(alice, bob)

        prepared = alice_orchestrator.prepare()
        approval = make_approval(
            intent,
            quote,
            prepared_hash=prepared.prepared_hash,
        )
        alice_orchestrator.approve(approval)
        receipt = alice_orchestrator.execute()

        assert receipt is not None
        assert alice_orchestrator.state(intent.id).value == "settled"
        assert bob_orchestrator.state(intent.id).value == "submitted"
        assert prepared.prepared_hash == approval.prepared_hash

        wire_receipt = make_receipt(
            intent,
            quote,
            settlement_ref=receipt.settlement_ref,
        )
        bob.publish_receipt(wire_receipt)
        received = alice.receive()

        assert len(received) == 1
        assert received[0].message == wire_receipt
        assert received[0].author == RECIPIENT_IDENTITY

    def test_two_independent_hermes_peers_reconcile_from_receipt(self):
        alice, bob, alice_orchestrator, _, bob_adapter = _build_peers(
            alice_execute_raises=AmbiguousResult("timeout after dispatch")
        )
        intent, quote = _exchange_intent_and_quote(alice, bob)

        prepared = alice_orchestrator.prepare()
        alice_orchestrator.approve(
            make_approval(intent, quote, prepared_hash=prepared.prepared_hash)
        )
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            alice_orchestrator.execute()

        verification = bob_adapter.verify_receipt(
            settlement_ref="payment_hash_abc123",
            expected_amount_sat=intent.amount_sat,
        )
        assert verification.verified is True

        bob_receipt = make_receipt(intent, quote)
        bob.publish_receipt(bob_receipt)
        received_receipt = alice.receive()[0].message
        assert isinstance(received_receipt, PaymentReceipt)
        alice.accept_receipt(received_receipt)

        assert alice_orchestrator.state(intent.id).value == "settled"
