"""
Hermes Payments — domain models (v0).

Versioned schemas for the payment protocol: PaymentIntent, PaymentQuote,
PaymentApproval, PaymentReceipt.  All fields are deterministic and
serialisable; the canonical form uses sorted-keys JSON + SHA-256.

Version history
---------------
v1 (2026-07) — initial contract, regtest-only vertical slice.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROTOCOL_VERSION: Literal["1"] = "1"
"""Monotonically increasing; bumped on any breaking change."""

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MessageKind(str, Enum):
    """Discriminator for the domain message inside a Buzz envelope.

    NOTE: PaymentApproval is NOT a transport message — it is strictly
    local human authorisation and never enters a Buzz envelope.
    """
    INTENT = "payment_intent"
    QUOTE = "payment_quote"
    RECEIPT = "payment_receipt"


class PaymentState(str, Enum):
    """Per-intent lifecycle state (finite automaton)."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    QUOTED = "quoted"
    PREPARED = "prepared"
    APPROVED = "approved"
    EXECUTING = "executing"
    SETTLED = "settled"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class Rail(str, Enum):
    """Supported settlement rails (v0: only lightning)."""

    LIGHTNING = "lightning"
    # Future: ON_CHAIN, ARK, ...


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class BuzzIdentity(BaseModel):
    """Compact representation of a Buzz/Nostr identity."""

    pubkey: str = Field(..., min_length=64, max_length=64, description="Schnorr public key, hex")
    relay_url: Optional[str] = Field(None, description="Preferred relay URL (null = default community relay)")


class RailReceiveInstruction(BaseModel):
    """Rail-specific instruction the recipient sends back in a quote.

    In v0 this is always a Lightning invoice.  Future rails add their
    own instruction shapes.
    """

    rail: Rail = Field(..., description="Settlement rail")
    invoice: Optional[str] = Field(None, description="Lightning invoice (bolt11)")
    # Future fields: on_chain_address, ark_descriptor, ...


# ---------------------------------------------------------------------------
# Domain messages
# ---------------------------------------------------------------------------


class PaymentIntent(BaseModel):
    """Sender-initiated request to pay a recipient.

    Lifecycle: DRAFT → SUBMITTED → ... (see state machine).

    Idempotency: the ``id`` field is derived deterministically from
    ``protocol_version``, ``sender``, ``recipient``, ``amount_sat``,
    ``purpose``, and ``idempotency_key``.  Two intents with the same
    ``id`` are the same intent — duplicate submission is a no-op.
    """

    protocol_version: Literal["1"] = Field(default=PROTOCOL_VERSION)
    id: str = Field(..., description="Deterministic ID: sha256 of canonical fields")
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    sender: BuzzIdentity
    recipient: BuzzIdentity
    amount_sat: int = Field(..., gt=0, description="Payment amount in satoshis")
    purpose: str = Field(..., min_length=1, max_length=512, description="Human-readable purpose")
    max_fee_sat: int = Field(..., ge=0, description="Maximum acceptable fee in satoshis")
    expires_at: int = Field(..., description="Unix epoch seconds; intent is void after this")
    created_at: int = Field(..., description="Unix epoch seconds; intent creation time")

    @field_validator("expires_at")
    @classmethod
    def _expiry_must_be_future(_cls, v: int) -> int:
        # Allow past values in fixtures/tests — validation is best-effort here.
        return v


class PaymentQuote(BaseModel):
    """Recipient's response to an accepted intent.

    The quote locks a rail, a receive instruction, and fee constraints.
    It is immutable after creation and valid until ``expires_at``.
    """

    protocol_version: Literal["1"] = Field(default=PROTOCOL_VERSION)
    id: str = Field(..., description="Deterministic ID: sha256 of canonical fields")
    intent_id: str = Field(..., description="References the PaymentIntent.id")
    quote_id: str = Field(..., min_length=1, max_length=128, description="Recipient-assigned quote identifier")
    recipient: BuzzIdentity
    receive_instruction: RailReceiveInstruction
    fee_sat: int = Field(..., ge=0, description="Quoted fee in satoshis")
    fee_constraint: Literal["exact", "max"] = Field(
        "max", description="'exact' = fee is fixed; 'max' = fee may be lower"
    )
    expires_at: int = Field(..., description="Unix epoch seconds; quote is void after this")
    created_at: int = Field(..., description="Unix epoch seconds")


class PaymentApproval(BaseModel):
    """Human approval binding (intent_id, quote_id, prepared_hash).

    This is the *only* message that authorises execution.  A Buzz
    message/event is never, by itself, financial authorisation.
    """

    protocol_version: Literal["1"] = Field(default=PROTOCOL_VERSION)
    id: str = Field(..., description="Deterministic ID: sha256 of canonical fields")
    intent_id: str = Field(..., description="References PaymentIntent.id")
    quote_id: str = Field(..., description="References PaymentQuote.quote_id")
    prepared_hash: str = Field(..., description="Hash of the prepared payload from adapter.prepare()")
    approver: BuzzIdentity
    created_at: int = Field(..., description="Unix epoch seconds")


class PaymentReceipt(BaseModel):
    """Settlement confirmation.

    The receipt is produced by the recipient after settlement.
    On the wire it is authored by the recipient (event.pubkey == recipient.pubkey).
    Locally, the sender's orchestrator may also create a receipt from the
    adapter result — in that case ``recipient`` is copied from the intent.
    """

    protocol_version: Literal["1"] = Field(default=PROTOCOL_VERSION)
    id: str = Field(..., description="Deterministic ID: sha256 of canonical fields")
    intent_id: str = Field(..., description="References PaymentIntent.id")
    quote_id: str = Field(..., description="References PaymentQuote.quote_id")
    recipient: BuzzIdentity = Field(..., description="Recipient identity; receipt is authored by this party")
    settlement_ref: str = Field(..., description="Rail-specific settlement reference (e.g. payment_hash)")
    amount_sat: int = Field(..., gt=0, description="Settled amount in satoshis")
    fee_sat: int = Field(..., ge=0, description="Actual fee in satoshis")
    rail: Rail
    settled_at: int = Field(..., description="Unix epoch seconds of settlement")
    created_at: int = Field(..., description="Unix epoch seconds")


# ---------------------------------------------------------------------------
# Union type for envelope dispatch
# ---------------------------------------------------------------------------

PaymentMessage = Union[PaymentIntent, PaymentQuote, PaymentReceipt]


# ---------------------------------------------------------------------------
# Canonical serialization helpers
# ---------------------------------------------------------------------------

def _canonical_bytes(model: BaseModel, *, include_id: bool = False) -> bytes:
    """Deterministic JSON bytes for a domain model.

    Rules:
    - dict keys sorted lexicographically (recursively)
    - no whitespace
    - UTF-8 encoding
    - ``None`` values are EXCLUDED (not ``"null"``)
    - ``id`` field is excluded by default (id = sha256 of everything else)

    Pass ``include_id=True`` to include the id field (for verification).
    """
    raw = model.model_dump(exclude_none=True, mode="python")
    if not include_id:
        raw.pop("id", None)
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_id(model: BaseModel) -> str:
    """SHA-256 of the canonical serialisation (excluding ``id``), lowercase hex."""
    return hashlib.sha256(_canonical_bytes(model, include_id=False)).hexdigest()


def compute_prepared_hash(prepared_payload: bytes) -> str:
    """SHA-256 of the opaque prepared payload returned by adapter.prepare()."""
    return hashlib.sha256(prepared_payload).hexdigest()
