"""
Hermes Payments — test fixtures (v0).

Deterministic fixtures for contract invariant testing.
All pubkeys are fake 64-char hex strings.  No real keys.
"""

from __future__ import annotations

import hashlib
import time

from hermes_payments.models import (
    BuzzIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    PaymentReceipt,
    Rail,
    RailReceiveInstruction,
    compute_id,
)

# ---------------------------------------------------------------------------
# Deterministic identities (fake pubkeys)
# ---------------------------------------------------------------------------

SENDER_PUBKEY = "aa" * 32  # 64 hex chars
RECIPIENT_PUBKEY = "bb" * 32
APPROVER_PUBKEY = "cc" * 32

SENDER_IDENTITY = BuzzIdentity(pubkey=SENDER_PUBKEY, relay_url=None)
RECIPIENT_IDENTITY = BuzzIdentity(pubkey=RECIPIENT_PUBKEY, relay_url=None)
APPROVER_IDENTITY = BuzzIdentity(pubkey=APPROVER_PUBKEY, relay_url=None)

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

NOW = 1785500000  # fixed epoch for deterministic tests
ONE_HOUR = 3600


# ---------------------------------------------------------------------------
# Sample messages
# ---------------------------------------------------------------------------


def make_intent(
    *,
    amount_sat: int = 2100,
    idempotency_key: str = "idem-001",
    max_fee_sat: int = 100,
    created_at: int = NOW,
    expires_at: int = NOW + ONE_HOUR,
) -> PaymentIntent:
    """Create a sample PaymentIntent with deterministic ID."""
    intent = PaymentIntent(
        id="placeholder",  # overwritten below
        idempotency_key=idempotency_key,
        sender=SENDER_IDENTITY,
        recipient=RECIPIENT_IDENTITY,
        amount_sat=amount_sat,
        purpose="Test payment",
        max_fee_sat=max_fee_sat,
        expires_at=expires_at,
        created_at=created_at,
    )
    intent.id = compute_id(intent)
    return intent


def make_quote(
    intent: PaymentIntent,
    *,
    fee_sat: int = 10,
    quote_id: str = "q-001",
    created_at: int = NOW,
    expires_at: int = NOW + ONE_HOUR,
) -> PaymentQuote:
    """Create a PaymentQuote referencing an intent."""
    quote = PaymentQuote(
        id="placeholder",
        intent_id=intent.id,
        quote_id=quote_id,
        recipient=RECIPIENT_IDENTITY,
        receive_instruction=RailReceiveInstruction(
            rail=Rail.LIGHTNING,
            invoice="lnbcrt2100n1p0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
        ),
        fee_sat=fee_sat,
        fee_constraint="max",
        expires_at=expires_at,
        created_at=created_at,
    )
    quote.id = compute_id(quote)
    return quote


def make_approval(
    intent: PaymentIntent,
    quote: PaymentQuote,
    *,
    prepared_hash: str = "aa" * 32,
    created_at: int = NOW,
) -> PaymentApproval:
    """Create a PaymentApproval binding (intent, quote, prepared_hash)."""
    approval = PaymentApproval(
        id="placeholder",
        intent_id=intent.id,
        quote_id=quote.quote_id,
        prepared_hash=prepared_hash,
        approver=APPROVER_IDENTITY,
        created_at=created_at,
    )
    approval.id = compute_id(approval)
    return approval


def make_receipt(
    intent: PaymentIntent,
    quote: PaymentQuote,
    *,
    settlement_ref: str = "payment_hash_abc123",
    fee_sat: int = 10,
    created_at: int = NOW,
    settled_at: int = NOW,
) -> PaymentReceipt:
    """Create a PaymentReceipt confirming settlement."""
    receipt = PaymentReceipt(
        id="placeholder",
        intent_id=intent.id,
        quote_id=quote.quote_id,
        settlement_ref=settlement_ref,
        amount_sat=intent.amount_sat,
        fee_sat=fee_sat,
        rail=Rail.LIGHTNING,
        settled_at=settled_at,
        created_at=created_at,
    )
    receipt.id = compute_id(receipt)
    return receipt
