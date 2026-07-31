"""
Hermes Payments — wire format codec (v2: kind-9 envelope).

All payment messages travel as NIP-29 channel messages (kind 9).
The ``content`` field carries an explicit versioned JSON envelope:

    {
      "protocol": "hermes-payments",
      "version": "1",
      "type": "<payment_intent|payment_quote|payment_receipt>",
      "payload": { <domain model fields> }
    }

``h`` tags (channel UUID) are managed by Buzz — we do NOT add them
manually; ``buzz messages send --channel <UUID>`` adds the ``h`` tag
automatically.

``id``, ``pubkey``, and ``sig`` are Buzz-native fields; we do NOT
produce signatures (Buzz does that).

This module is the adapter boundary between protocol domain and transport.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Literal, Optional, Type, Union

from pydantic import BaseModel, Field

from .models import (
    MessageKind,
    PaymentApproval,
    PaymentMessage,
    PaymentReceipt,
)

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------

WIRE_KIND: int = 9
"""NIP-29 channel message kind — all payment messages use this."""

PROTOCOL_ID: str = "hermes-payments"
"""Protocol identifier embedded in the envelope."""

PROTOCOL_VERSION: str = "1"
"""Envelope protocol version (independent of domain model protocol_version)."""


# ---------------------------------------------------------------------------
# Message-type discriminator maps
# ---------------------------------------------------------------------------

# Lazy import to avoid circular imports at module level
_MODEL_MAP: Optional[Dict[str, Type[PaymentMessage]]] = None


def _get_model_map() -> Dict[str, Type[PaymentMessage]]:
    """Lazy-loaded map from envelope type string to domain model class."""
    global _MODEL_MAP
    if _MODEL_MAP is None:
        from .models import PaymentIntent, PaymentQuote, PaymentReceipt as PR

        _MODEL_MAP = {
            MessageKind.INTENT.value: PaymentIntent,
            MessageKind.QUOTE.value: PaymentQuote,
            MessageKind.RECEIPT.value: PR,
        }
    return _MODEL_MAP


# ``KIND_MAP`` remains for callers that need the Nostr kind, but the
# envelope ``type`` is the sole discriminator.  Never reverse-map kind 9.
KIND_MAP: Dict[MessageKind, int] = {
    MessageKind.INTENT: WIRE_KIND,
    MessageKind.QUOTE: WIRE_KIND,
    MessageKind.RECEIPT: WIRE_KIND,
}


# ---------------------------------------------------------------------------
# Payment envelope (the JSON content format)
# ---------------------------------------------------------------------------


class PaymentEnvelope(BaseModel):
    """Versioned JSON envelope carried inside the ``content`` field of a
    NIP-29 kind-9 channel message.

    The ``type`` field discriminates which domain model the ``payload``
    deserialises to.  The ``protocol`` and ``version`` fields allow
    protocol evolution without kind-number proliferation.
    """

    protocol: Literal["hermes-payments"] = Field(default="hermes-payments")
    version: Literal["1"] = Field(default="1")
    type: MessageKind = Field(..., description="Discriminator for the payload model")
    payload: Dict[str, Any] = Field(..., description="Domain model fields")


# ---------------------------------------------------------------------------
# Encoding / decoding helpers
# ---------------------------------------------------------------------------


def encode_content(message: PaymentMessage) -> str:
    """Encode a domain message as a ``PaymentEnvelope`` JSON string.

    PaymentApproval is NEVER encodable — raises ``TypeError`` at runtime.
    """
    if isinstance(message, PaymentApproval):
        raise TypeError(
            "PaymentApproval must never be serialized or transmitted; "
            "it is strictly local human authorisation"
        )
    msg_kind = _kind_for_model(message)
    envelope = PaymentEnvelope(
        type=msg_kind,
        payload=message.model_dump(exclude_none=True, mode="python"),
    )
    return envelope.model_dump_json(exclude_none=True)


def decode_content(content: str) -> PaymentMessage:
    """Decode a ``PaymentEnvelope`` JSON string into a domain message.

    Validates protocol, version, and schema.  Raises ``ValueError`` on
    any validation failure.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"content is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("content JSON is not an object")

    # Validate protocol envelope
    protocol = data.get("protocol")
    if protocol != PROTOCOL_ID:
        raise ValueError(
            f"unknown protocol {protocol!r}; expected {PROTOCOL_ID!r}"
        )

    version = data.get("version")
    if version != PROTOCOL_VERSION:
        raise ValueError(
            f"unsupported protocol version {version!r}; expected {PROTOCOL_VERSION!r}"
        )

    type_str = data.get("type")
    if type_str is None:
        raise ValueError("envelope missing 'type' field")

    model_map = _get_model_map()
    model_cls = model_map.get(type_str)
    if model_cls is None:
        raise ValueError(f"unknown envelope type {type_str!r}")

    payload = data.get("payload")
    if payload is None or not isinstance(payload, dict):
        raise ValueError("envelope missing or invalid 'payload' field")

    try:
        return model_cls.model_validate(payload)
    except Exception as e:
        raise ValueError(f"payload validation failed for type {type_str!r}: {e}") from e


def _kind_for_model(message: PaymentMessage) -> MessageKind:
    """Map a model instance to its MessageKind."""
    class_name = type(message).__name__
    mapping = {
        "PaymentIntent": MessageKind.INTENT,
        "PaymentQuote": MessageKind.QUOTE,
        "PaymentReceipt": MessageKind.RECEIPT,
    }
    result = mapping.get(class_name)
    if result is None:
        raise TypeError(f"{class_name} is not a transportable payment message")
    return result


# ---------------------------------------------------------------------------
# Backward-compat: BuzzEnvelope (Nostr event shape for tests)
# ---------------------------------------------------------------------------


class BuzzEnvelope(BaseModel):
    """Nostr event envelope for a payment message.

    Updated to use kind 9 (NIP-29 channel message) instead of custom kinds.
    The ``content`` field carries a ``PaymentEnvelope`` JSON string.
    """

    id: str = Field(..., description="Nostr event ID (sha256 of serialised event)")
    pubkey: str = Field(..., min_length=64, max_length=64, description="Author's Schnorr pubkey, hex")
    kind: int = Field(default=WIRE_KIND, description="Nostr event kind (always 9 for payments)")
    tags: List[List[str]] = Field(default_factory=list, description="Nostr tags")
    content: str = Field(..., description="PaymentEnvelope JSON string")
    sig: str = Field(..., min_length=128, max_length=128, description="Schnorr sig over id")


def payment_to_envelope(
    message: PaymentMessage,
    *,
    author_pubkey: str,
    event_id: str,
    event_sig: str,
) -> BuzzEnvelope:
    """Encode a domain message into a BuzzEnvelope (kind 9).

    The caller (Buzz transport adapter) provides ``author_pubkey``,
    ``event_id``, and ``event_sig`` — we do not sign; Buzz does.
    """
    return BuzzEnvelope(
        id=event_id,
        pubkey=author_pubkey,
        kind=WIRE_KIND,
        tags=_build_tags(message),
        content=encode_content(message),
        sig=event_sig,
    )


def envelope_to_payment(env: BuzzEnvelope) -> PaymentMessage:
    """Decode a BuzzEnvelope back into a domain message.

    Raises ``ValueError`` if the kind is not 9 or the content does not
    match the expected model.
    """
    if env.kind != WIRE_KIND:
        raise ValueError(f"kind {env.kind} is not a payment kind; expected {WIRE_KIND}")
    return decode_content(env.content)


# ---------------------------------------------------------------------------
# Tag conventions (metadata tags only — h-tag is added by Buzz)
# ---------------------------------------------------------------------------


def _build_tags(message: PaymentMessage) -> List[List[str]]:
    """Build Nostr metadata tags for a payment message.

    These are informational tags added to the content envelope.
    The ``h`` tag (channel UUID) is added automatically by Buzz when
    using ``buzz messages send --channel <UUID>``.
    """
    tags: List[List[str]] = [
        ["protocol", "hermes-payments-v1"],
    ]

    tags.append(["intent", getattr(message, "intent_id", message.id)])

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
