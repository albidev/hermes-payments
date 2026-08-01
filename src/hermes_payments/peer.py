"""Role-neutral Hermes payment peer endpoint.

A ``HermesPeer`` composes local policy with an injected ``PeerTransport``.
It deliberately does not know whether messages travel through Buzz, HTTP,
WebSocket, a Unix socket, or an in-memory test hub.
"""
from __future__ import annotations

from typing import Optional

from .models import (
    AgentIdentity,
    PaymentIntent,
    PaymentMessage,
    PaymentQuote,
    PaymentReceipt,
)
from .peer_transport import (
    PeerMessage,
    PeerTransport,
    PeerTransportError,
    message_author,
)
from .policy import PaymentOrchestrator


class PeerProtocolError(PeerTransportError):
    """Raised when a peer attempts an operation outside its role."""


class HermesPeer:
    """A transport-neutral Hermes endpoint for one payment participant."""

    def __init__(
        self,
        *,
        identity: AgentIdentity,
        transport: PeerTransport,
        orchestrator: PaymentOrchestrator,
    ) -> None:
        self._identity = identity
        self._transport = transport
        self._orchestrator = orchestrator

    @property
    def identity(self) -> AgentIdentity:
        """The local peer identity used for author and recipient checks."""
        return self._identity

    @property
    def orchestrator(self) -> PaymentOrchestrator:
        """The injected local policy engine."""
        return self._orchestrator

    def send(self, message: PaymentMessage) -> str:
        """Publish a locally-authored transportable payment message."""
        self._require_local_author(message)
        return self._transport.send(message)

    def receive(self, *, limit: Optional[int] = None) -> list[PeerMessage]:
        """Read validated messages without mutating local policy state."""
        return self._transport.receive(limit=limit)

    def submit_intent(self, intent: PaymentIntent) -> str:
        """Submit a local intent and publish it to the remote peer."""
        self._require_local_author(intent)
        self._orchestrator.submit(intent)
        return self._transport.send(intent)

    def accept_intent(self, intent: PaymentIntent) -> None:
        """Accept a received intent into the local policy engine."""
        self._require_local_recipient(intent)
        self._orchestrator.submit(intent)

    def publish_quote(self, quote: PaymentQuote) -> str:
        """Publish a quote authored by this recipient peer."""
        return self.send(quote)

    def accept_quote(self, quote: PaymentQuote) -> None:
        """Pass a received quote into the local sender policy engine."""
        intent = self._orchestrator.get_intent(quote.intent_id)
        if intent is not None and quote.recipient.pubkey != intent.recipient.pubkey:
            raise PeerProtocolError("quote recipient does not match the intent")
        self._orchestrator.receive_quote(quote)

    def publish_receipt(self, receipt: PaymentReceipt) -> str:
        """Publish a receipt authored by this recipient peer."""
        return self.send(receipt)

    def accept_receipt(self, receipt: PaymentReceipt) -> PaymentReceipt:
        """Pass a received receipt into the local sender policy engine."""
        intent = self._orchestrator.get_intent(receipt.intent_id)
        if intent is not None and receipt.recipient.pubkey != intent.recipient.pubkey:
            raise PeerProtocolError("receipt recipient does not match the intent")
        return self._orchestrator.receive_receipt(receipt)

    def _require_local_author(self, message: object) -> None:
        author = message_author(message)
        if author.pubkey != self._identity.pubkey:
            raise PeerProtocolError("local peer is not the message author")

    def _require_local_recipient(self, intent: PaymentIntent) -> None:
        if intent.recipient.pubkey != self._identity.pubkey:
            raise PeerProtocolError("message recipient is not local")


__all__ = ["HermesPeer", "PeerProtocolError"]
