"""Transport-neutral peer messaging for Hermes payment conversations.

This module is deliberately unaware of Buzz, Nostr, subprocesses, relays,
or settlement adapters.  Concrete transports adapt their native delivery
mechanism to :class:`PeerTransport`.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Optional, Protocol, runtime_checkable

from .models import (
    AgentIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentMessage,
    PaymentQuote,
    PaymentReceipt,
    compute_id,
)


class PeerTransportError(Exception):
    """Raised when a peer message cannot be safely delivered."""


@dataclass(frozen=True)
class PeerMessage:
    """A validated payment message plus transport delivery metadata."""

    message_id: str
    message: PaymentMessage
    author: AgentIdentity
    published_at: int


def message_author(message: object) -> AgentIdentity:
    """Return the domain author for a transportable payment message.

    The mapping is part of the payment protocol, not a Buzz rule: senders
    author intents, while recipients author quotes and settlement receipts.
    ``PaymentApproval`` is intentionally rejected because it is local-only.
    """
    if isinstance(message, PaymentIntent):
        return message.sender
    if isinstance(message, (PaymentQuote, PaymentReceipt)):
        return message.recipient
    if isinstance(message, PaymentApproval):
        raise PeerTransportError(
            "PaymentApproval is local-only and cannot cross a peer transport"
        )
    raise PeerTransportError(
        f"unsupported peer message type: {type(message).__name__}"
    )


@runtime_checkable
class PeerTransport(Protocol):
    """Minimal transport contract between two Hermes peers.

    Implementations may use Buzz, HTTP, WebSocket, a Unix socket, or any
    future delivery mechanism.  The business layer sees only typed payment
    messages and stable delivery metadata.
    """

    def send(self, message: PaymentMessage) -> str:
        """Publish a payment message and return its transport message ID."""
        ...

    def receive(self, *, limit: Optional[int] = None) -> list[PeerMessage]:
        """Consume validated peer messages from the local inbox."""
        ...


class InMemoryTransportHub:
    """Deterministic message hub used to prove transport independence."""

    def __init__(self, *, clock: Optional[Callable[[], int]] = None) -> None:
        self._clock = clock or (lambda: 0)
        self._inboxes: dict[str, list[PeerMessage]] = {}
        self._counter = 0

    def connect(self, *, peer_id: str, identity: AgentIdentity) -> "InMemoryPeerTransport":
        """Create one independent endpoint attached to this hub."""
        if not peer_id:
            raise ValueError("peer_id is required")
        if peer_id in self._inboxes:
            raise ValueError(f"peer_id already connected: {peer_id}")
        self._inboxes[peer_id] = []
        return InMemoryPeerTransport(self, peer_id=peer_id, identity=identity)

    def _send(self, *, peer_id: str, identity: AgentIdentity, message: PaymentMessage) -> str:
        author = message_author(message)
        if author.pubkey != identity.pubkey:
            raise PeerTransportError(
                "local peer is not the message author"
            )

        message_id = sha256(
            f"{peer_id}:{self._counter}:{compute_id(message)}".encode("utf-8")
        ).hexdigest()
        self._counter += 1
        delivered = PeerMessage(
            message_id=message_id,
            message=message.model_copy(deep=True),
            author=author.model_copy(deep=True),
            published_at=self._clock(),
        )
        for recipient_id, inbox in self._inboxes.items():
            if recipient_id != peer_id:
                inbox.append(delivered)
        return message_id

    def _receive(self, *, peer_id: str, limit: Optional[int]) -> list[PeerMessage]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be zero or greater")
        inbox = self._inboxes[peer_id]
        if limit == 0:
            return []
        selected = list(inbox if limit is None else inbox[:limit])
        del inbox[: len(selected)]
        return selected


class InMemoryPeerTransport:
    """One endpoint of :class:`InMemoryTransportHub`."""

    def __init__(
        self,
        hub: InMemoryTransportHub,
        *,
        peer_id: str,
        identity: AgentIdentity,
    ) -> None:
        self._hub = hub
        self._peer_id = peer_id
        self._identity = identity

    def send(self, message: PaymentMessage) -> str:
        return self._hub._send(
            peer_id=self._peer_id,
            identity=self._identity,
            message=message,
        )

    def receive(self, *, limit: Optional[int] = None) -> list[PeerMessage]:
        return self._hub._receive(peer_id=self._peer_id, limit=limit)


__all__ = [
    "InMemoryPeerTransport",
    "InMemoryTransportHub",
    "PeerMessage",
    "PeerTransport",
    "PeerTransportError",
    "message_author",
]
