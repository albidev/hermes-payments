"""
Hermes Payments — Buzz transport envelope mapping (v0).

Defines how domain messages map to/from signed Buzz/Nostr events.

Envelope design:
- Each PaymentMessage is serialised as JSON ``content`` inside a
  Nostr event.
- ``kind`` selects the message type (see ``KIND_MAP``).
- ``tags`` carry protocol-relevant metadata (intent-id, quote-id, etc.)
  without duplicating the content.
- ``id``, ``pubkey``, and ``sig`` are Buzz-native fields; we do NOT
  produce signatures (Buzz does that) — we only define the shape.

This module is the adapter boundary between protocol domain and transport.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, Field

from .models import (
    MessageKind,
    PaymentApproval,
    PaymentIntent,
    PaymentMessage,
    PaymentQuote,
    PaymentReceipt,
)


# ---------------------------------------------------------------------------
# Buzz custom kinds for payments (reserved range 40000–49999)
# ---------------------------------------------------------------------------

# These are the Nostr ``kind`` integers used in Buzz events.
# They live in the Buzz custom-kinds range and must NOT collide with
# existing Buzz kinds.

KIND_PAYMENT_INTENT: int = 40100
KIND_PAYMENT_QUOTE: int = 40101
KIND_PAYMENT_APPROVAL: int = 40102
KIND_PAYMENT_RECEIPT: int = 40103

KIND_MAP: Dict[MessageKind, int] = {
    MessageKind.INTENT: KIND_PAYMENT_INTENT,
    MessageKind.QUOTE: KIND_PAYMENT_QUOTE,
    MessageKind.APPROVAL: KIND_PAYMENT_APPROVAL,
    MessageKind.RECEIPT: KIND_PAYMENT_RECEIPT,
}

REVERSE_KIND_MAP: Dict[int, MessageKind] = {v: k for k, v in KIND_MAP.items()}

MODEL_MAP: Dict[MessageKind, Type[PaymentMessage]] = {
    MessageKind.INTENT: PaymentIntent,
    MessageKind.QUOTE: PaymentQuote,
    MessageKind.APPROVAL: PaymentApproval,
    MessageKind.RECEIPT: PaymentReceipt,
}


# ---------------------------------------------------------------------------
# Envelope (Nostr event shape)
# ---------------------------------------------------------------------------


class BuzzEnvelope(BaseModel):
    """Nostr event envelope for a payment message.

    This is the *wire shape* — what Buzz stores and distributes.
    The ``content`` field carries the JSON-encoded domain message.
    """

    id: str = Field(..., description="Nostr event ID (sha256 of serialised event)")
    pubkey: str = Field(..., min_length=64, max_length=64, description="Author's Schnorr pubkey, hex")
    kind: int = Field(..., description="Nostr event kind (40100–40103)")
    tags: List[List[str]] = Field(default_factory=list, description="Nostr tags")
    content: str = Field(..., description="JSON-encoded PaymentMessage")
    sig: str = Field(..., min_length=128, max_length=128, description="Schnorr sig over id")


# ---------------------------------------------------------------------------
# Encoding / decoding helpers
# ---------------------------------------------------------------------------


def payment_to_envelope(
    message: PaymentMessage,
    *,
    author_pubkey: str,
    event_id: str,
    event_sig: str,
) -> BuzzEnvelope:
    """Encode a domain message into a Buzz envelope.

    The caller (Buzz transport adapter) provides ``author_pubkey``,
    ``event_id``, and ``event_sig`` — we do not sign; Buzz does.
    """
    msg_kind = _kind_for_model(message)
    return BuzzEnvelope(
        id=event_id,
        pubkey=author_pubkey,
        kind=KIND_MAP[msg_kind],
        tags=_build_tags(message),
        content=message.model_dump_json(exclude_none=True),
        sig=event_sig,
    )


def envelope_to_payment(env: BuzzEnvelope) -> PaymentMessage:
    """Decode a Buzz envelope back into a domain message.

    Raises ``ValueError`` if the kind is not a payment kind or the
    content does not match the expected model.
    """
    msg_kind = REVERSE_KIND_MAP.get(env.kind)
    if msg_kind is None:
        raise ValueError(f"kind {env.kind} is not a payment kind")
    model_cls = MODEL_MAP[msg_kind]
    return model_cls.model_validate_json(env.content)


def _kind_for_model(message: PaymentMessage) -> MessageKind:
    """Map a model instance to its MessageKind."""
    class_name = type(message).__name__
    mapping = {
        "PaymentIntent": MessageKind.INTENT,
        "PaymentQuote": MessageKind.QUOTE,
        "PaymentApproval": MessageKind.APPROVAL,
        "PaymentReceipt": MessageKind.RECEIPT,
    }
    return mapping[class_name]


# ---------------------------------------------------------------------------
# Tag conventions
# ---------------------------------------------------------------------------

def _build_tags(message: PaymentMessage) -> List[List[str]]:
    """Build Nostr tags for a payment message.

    Tags provide indexed metadata for Buzz filtering/subscription
    without parsing content.
    """
    tags: List[List[str]] = [
        ["h", "hermes-payments"],          # community tag
        ["protocol", "hermes-payments-v1"], # protocol identifier
    ]

    if isinstance(message, PaymentIntent):
        tags.append(["intent", message.id])
        tags.append(["p", message.recipient.pubkey])
    elif isinstance(message, PaymentQuote):
        tags.append(["intent", message.intent_id])
        tags.append(["quote", message.quote_id])
        tags.append(["p", message.recipient.pubkey])
    elif isinstance(message, PaymentApproval):
        tags.append(["intent", message.intent_id])
        tags.append(["quote", message.quote_id])
    elif isinstance(message, PaymentReceipt):
        tags.append(["intent", message.intent_id])
        tags.append(["settlement", message.settlement_ref])

    return tags


# ---------------------------------------------------------------------------
# Status event (lightweight state change notification)
# ---------------------------------------------------------------------------


class StatusEvent(BaseModel):
    """Lightweight state-change event for audit trail.

    Not a PaymentMessage — this is a separate Buzz event that
    records state transitions for observability.
    """

    intent_id: str
    old_state: str
    new_state: str
    trigger: str
    timestamp: int = Field(..., description="Unix epoch seconds")
    actor: Optional[str] = Field(None, description="Pubkey of the actor who triggered the transition")
