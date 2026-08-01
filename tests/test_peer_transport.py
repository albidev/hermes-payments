"""Transport-neutral peer contract tests."""
from __future__ import annotations

import pytest

from hermes_payments.models import PaymentApproval
from hermes_payments.peer_transport import (
    InMemoryTransportHub,
    PeerTransport,
    PeerTransportError,
    message_author,
)
from tests.fixtures import (
    NOW,
    make_approval,
    make_intent,
    make_quote,
    make_receipt,
)


class TestInMemoryPeerTransport:
    def test_endpoints_are_transport_protocols(self):
        hub = InMemoryTransportHub(clock=lambda: NOW)
        alice = hub.connect(peer_id="alice", identity=make_intent().sender)
        assert isinstance(alice, PeerTransport)

    def test_send_delivers_typed_message_with_transport_metadata(self):
        intent = make_intent()
        hub = InMemoryTransportHub(clock=lambda: NOW)
        alice = hub.connect(peer_id="alice", identity=intent.sender)
        bob = hub.connect(peer_id="bob", identity=intent.recipient)

        message_id = alice.send(intent)
        received = bob.receive()

        assert message_id
        assert received == [
            received[0]
        ]
        assert received[0].message_id == message_id
        assert received[0].message == intent
        assert received[0].author == intent.sender
        assert received[0].published_at == NOW
        assert alice.receive() == []

    def test_sender_cannot_publish_message_authored_by_another_peer(self):
        intent = make_intent()
        hub = InMemoryTransportHub(clock=lambda: NOW)
        bob = hub.connect(peer_id="bob", identity=intent.recipient)

        with pytest.raises(PeerTransportError, match="local peer is not the message author"):
            bob.send(intent)

    def test_approval_is_never_transportable(self):
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)
        hub = InMemoryTransportHub(clock=lambda: NOW)
        alice = hub.connect(peer_id="alice", identity=intent.sender)

        assert isinstance(approval, PaymentApproval)
        with pytest.raises(PeerTransportError, match="PaymentApproval"):
            alice.send(approval)

    def test_receive_limit_leaves_remaining_messages_in_inbox(self):
        first = make_intent(idempotency_key="first")
        second = make_intent(idempotency_key="second")
        third = make_intent(idempotency_key="third")
        hub = InMemoryTransportHub(clock=lambda: NOW)
        alice = hub.connect(peer_id="alice", identity=first.sender)
        bob = hub.connect(peer_id="bob", identity=first.recipient)

        alice.send(first)
        alice.send(second)
        alice.send(third)

        assert [item.message.id for item in bob.receive(limit=2)] == [
            first.id,
            second.id,
        ]
        assert [item.message.id for item in bob.receive()] == [third.id]

    def test_duplicate_delivery_has_distinct_transport_ids(self):
        intent = make_intent()
        hub = InMemoryTransportHub(clock=lambda: NOW)
        alice = hub.connect(peer_id="alice", identity=intent.sender)
        bob = hub.connect(peer_id="bob", identity=intent.recipient)

        first_id = alice.send(intent)
        second_id = alice.send(intent)
        received = bob.receive()

        assert [item.message.id for item in received] == [intent.id, intent.id]
        assert [item.message_id for item in received] == [first_id, second_id]
        assert first_id != second_id


class TestMessageAuthor:
    def test_author_is_sender_for_intent(self):
        intent = make_intent()
        assert message_author(intent) == intent.sender

    def test_author_is_recipient_for_quote_and_receipt(self):
        intent = make_intent()
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        assert message_author(quote) == quote.recipient
        assert message_author(receipt) == receipt.recipient

    def test_approval_has_no_transport_author(self):
        intent = make_intent()
        quote = make_quote(intent)
        approval = make_approval(intent, quote)
        with pytest.raises(PeerTransportError, match="PaymentApproval"):
            message_author(approval)
