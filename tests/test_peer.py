"""Role-neutral Hermes peer endpoint tests."""
from __future__ import annotations

import pytest

from hermes_payments.adapter import AmbiguousResult
from hermes_payments.models import PaymentApproval
from hermes_payments.peer import HermesPeer, PeerProtocolError
from hermes_payments.peer_transport import (
    InMemoryTransportHub,
    PeerTransportError,
)
from hermes_payments.policy import PaymentOrchestrator, StateError, UnknownIntent
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


def _orchestrator(*, execute_raises=None) -> PaymentOrchestrator:
    return PaymentOrchestrator(
        adapter=StubAdapter(execute_raises=execute_raises),
        clock=lambda: NOW,
    )


def _peers(*, execute_raises=None):
    hub = InMemoryTransportHub(clock=lambda: NOW)
    alice_orchestrator = _orchestrator(execute_raises=execute_raises)
    bob_orchestrator = _orchestrator()
    alice_transport = hub.connect(peer_id="alice", identity=SENDER_IDENTITY)
    bob_transport = hub.connect(peer_id="bob", identity=RECIPIENT_IDENTITY)
    alice = HermesPeer(
        identity=SENDER_IDENTITY,
        transport=alice_transport,
        orchestrator=alice_orchestrator,
    )
    bob = HermesPeer(
        identity=RECIPIENT_IDENTITY,
        transport=bob_transport,
        orchestrator=bob_orchestrator,
    )
    return alice, bob, alice_orchestrator, bob_orchestrator


class TestHermesPeerBoundary:
    def test_submit_and_accept_intent_use_only_generic_transport(self):
        alice, bob, alice_orchestrator, bob_orchestrator = _peers()
        intent = make_intent()

        message_id = alice.submit_intent(intent)
        received = bob.receive()
        bob.accept_intent(received[0].message)

        assert message_id == received[0].message_id
        assert alice_orchestrator.state(intent.id).value == "submitted"
        assert bob_orchestrator.state(intent.id).value == "submitted"

    def test_duplicate_intent_delivery_is_policy_idempotent(self):
        alice, bob, _, bob_orchestrator = _peers()
        intent = make_intent()

        alice.submit_intent(intent)
        first = bob.receive()
        # Publish the same domain message again: transport IDs differ, intent ID does not.
        alice.send(intent)
        second = bob.receive()
        bob.accept_intent(first[0].message)
        bob.accept_intent(second[0].message)

        assert first[0].message_id != second[0].message_id
        assert bob_orchestrator.state(intent.id).value == "submitted"

    def test_quote_round_trip_and_explicit_policy_handoff(self):
        alice, bob, alice_orchestrator, _ = _peers()
        intent = make_intent()
        quote = make_quote(intent)

        alice.submit_intent(intent)
        bob.accept_intent(bob.receive()[0].message)
        bob.publish_quote(quote)
        received_quote = alice.receive()[0]
        alice.accept_quote(received_quote.message)

        assert alice_orchestrator.state(intent.id).value == "quoted"

    def test_receive_does_not_mutate_policy_without_explicit_accept(self):
        alice, bob, _, bob_orchestrator = _peers()
        intent = make_intent()

        alice.submit_intent(intent)
        received = bob.receive()

        assert received[0].message == intent
        with pytest.raises(UnknownIntent, match="not tracked"):
            bob_orchestrator.state(intent.id)

    def test_local_authority_is_required_for_publish_operations(self):
        alice, bob, _, _ = _peers()
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        foreign_quote = quote.model_copy(update={"recipient": SENDER_IDENTITY})
        foreign_receipt = receipt.model_copy(update={"recipient": SENDER_IDENTITY})

        with pytest.raises(PeerProtocolError, match="local peer is not the message author"):
            alice.publish_quote(quote)
        with pytest.raises(PeerProtocolError, match="local peer is not the message author"):
            alice.publish_receipt(receipt)
        with pytest.raises(PeerProtocolError, match="local peer is not the message author"):
            bob.publish_quote(foreign_quote)
        with pytest.raises(PeerProtocolError, match="local peer is not the message author"):
            bob.publish_receipt(foreign_receipt)

    def test_payment_approval_cannot_cross_peer_boundary(self):
        alice, _, _, _ = _peers()
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)

        assert isinstance(approval, PaymentApproval)
        with pytest.raises(PeerTransportError, match="PaymentApproval"):
            alice.send(approval)

    def test_accept_quote_rejects_mismatched_recipient(self):
        alice, _, _, _ = _peers()
        intent = make_intent()
        alice.submit_intent(intent)
        foreign_quote = make_quote(intent).model_copy(
            update={"recipient": SENDER_IDENTITY}
        )

        with pytest.raises(PeerProtocolError, match="quote recipient"):
            alice.accept_quote(foreign_quote)

    def test_accept_receipt_rejects_mismatched_recipient(self):
        alice, _, _, _ = _peers()
        intent = make_intent()
        quote = make_quote(intent)
        alice.submit_intent(intent)
        foreign_receipt = make_receipt(intent, quote).model_copy(
            update={"recipient": SENDER_IDENTITY}
        )

        with pytest.raises(PeerProtocolError, match="receipt recipient"):
            alice.accept_receipt(foreign_receipt)


class TestHermesPeerReceiptPath:
    def test_ambiguous_execution_is_settled_by_recipient_receipt(self):
        alice, bob, alice_orchestrator, _ = _peers(
            execute_raises=AmbiguousResult("transport timeout after dispatch")
        )
        intent = make_intent()
        quote = make_quote(intent)

        alice.submit_intent(intent)
        bob.accept_intent(bob.receive()[0].message)
        bob.publish_quote(quote)
        alice.accept_quote(alice.receive()[0].message)
        prepared = alice_orchestrator.prepare()
        alice_orchestrator.approve(make_approval(
            intent,
            quote,
            prepared_hash=prepared.prepared_hash,
        ))
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            alice_orchestrator.execute()

        receipt = make_receipt(intent, quote)
        bob.publish_receipt(receipt)
        alice.accept_receipt(alice.receive()[0].message)

        assert alice_orchestrator.state(intent.id).value == "settled"
