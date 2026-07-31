"""
Hermes Payments — settlement adapter boundary (v0).

Defines the interface that settlement adapters (Wavelength, future
rails) must implement.  The adapter is the ONLY component that
touches funds; it never has access to seeds, passwords, or macaroons.

Adapter contract:
- prepare() is non-mutating (dry-run).
- execute() is called ONLY after human approval of
  (intent_id, quote_id, prepared_hash).
- The adapter NEVER retries automatically.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from .models import BuzzIdentity, PaymentQuote, Rail, RailReceiveInstruction


# ---------------------------------------------------------------------------
# Adapter results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrepareResult:
    """Result of a non-mutating prepare call.

    ``prepared_hash`` is the SHA-256 of the opaque prepared payload.
    The human must approve this exact hash before execute() is called.
    """

    fee_sat: int
    prepared_hash: str
    rail: Rail
    prepared_payload: bytes  # opaque, adapter-specific; never leaves the sender


@dataclass(frozen=True)
class ExecuteResult:
    """Result of a successful execute call.

    ``settlement_ref`` is a rail-specific reference that can be used
    to independently verify the settlement (e.g. Lightning payment_hash).
    """

    settlement_ref: str
    amount_sat: int
    fee_sat: int
    rail: Rail


@dataclass(frozen=True)
class ReceiptVerifyResult:
    """Result of verifying a receipt against the rail."""

    verified: bool
    settlement_ref: str
    amount_sat: int
    fee_sat: int
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Abstract adapter interface
# ---------------------------------------------------------------------------


class SettlementAdapter(ABC):
    """Abstract base for settlement adapters.

    Subclass this to integrate a new rail.  The adapter:
    - Receives opaque instructions from PaymentQuote.receive_instruction.
    - Does NOT know about PaymentIntent, approval, or Buzz.
    - Does NOT have access to seeds, passwords, or macaroons.
    - Does NOT retry automatically.
    """

    @property
    @abstractmethod
    def rail(self) -> Rail:
        """The rail this adapter handles."""
        ...

    @abstractmethod
    def prepare(
        self,
        receive_instruction: RailReceiveInstruction,
        amount_sat: int,
        max_fee_sat: int,
    ) -> PrepareResult:
        """Non-mutating dry-run.  Returns fee estimate and prepared payload.

        May raise ``AdapterError`` if the prepare fails (e.g. invoice
        expired, insufficient balance).
        """
        ...

    @abstractmethod
    def execute(
        self,
        prepared_payload: bytes,
        prepared_hash: str,
    ) -> ExecuteResult:
        """Execute the settlement.

        MUST be called exactly once for a given prepared_hash.
        MUST be called ONLY after human approval.

        May raise ``AdapterError`` on failure.  On ambiguous results
        (e.g. network timeout after broadcast), the adapter raises
        ``AmbiguousResult`` — the caller must NOT retry.
        """
        ...

    @abstractmethod
    def verify_receipt(
        self,
        settlement_ref: str,
        expected_amount_sat: int,
    ) -> ReceiptVerifyResult:
        """Verify a receipt against the rail.

        Used by the recipient to confirm that a settlement actually
        happened and matches the expected amount.
        """
        ...


# ---------------------------------------------------------------------------
# Adapter errors
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Base class for adapter errors.

    The state machine transitions to FAILED on AdapterError.
    """

    def __init__(self, message: str, recoverable: bool = False):
        super().__init__(message)
        self.recoverable = recoverable


class AmbiguousResult(AdapterError):
    """Raised when the adapter cannot determine the outcome.

    The caller MUST NOT retry.  The state machine transitions to
    RECONCILIATION_REQUIRED and the human must investigate manually
    to verify whether settlement occurred.
    """

    def __init__(self, message: str = "ambiguous settlement result"):
        super().__init__(message, recoverable=False)


# ---------------------------------------------------------------------------
# Wavelength adapter (concrete, v0)
# ---------------------------------------------------------------------------


class WavelengthAdapter(SettlementAdapter):
    """First settlement adapter: Wavelength Lightning (regtest-only).

    Maps the generic prepare/execute/verify interface to Wavelength's
    MCP tool surface.  The specific wavecli command mapping is DEFERRED
    to Gate 4, which will verify flags/tools against a live daemon.

    Credentials (TLS, macaroons) stay local and are NEVER passed
    through Buzz events or the adapter boundary.
    """

    def __init__(self, *, rpc_url: str = "localhost:10029", network: str = "regtest"):
        self._rpc_url = rpc_url
        self._network = network

    @property
    def rail(self) -> Rail:
        return Rail.LIGHTNING

    def prepare(
        self,
        receive_instruction: RailReceiveInstruction,
        amount_sat: int,
        max_fee_sat: int,
    ) -> PrepareResult:
        """Non-mutating dry-run.

        Wavecli command mapping DEFERRED to Gate 4 (needs live daemon verification).
        """
        raise NotImplementedError(
            "WavelengthAdapter.prepare() requires a live Wavelength daemon. "
            "Command mapping deferred to Gate 4."
        )

    def execute(
        self,
        prepared_payload: bytes,
        prepared_hash: str,
    ) -> ExecuteResult:
        """Execute the settlement.

        Wavecli command mapping DEFERRED to Gate 4 (needs live daemon verification).
        """
        raise NotImplementedError(
            "WavelengthAdapter.execute() requires a live Wavelength daemon. "
            "Command mapping deferred to Gate 4."
        )

    def verify_receipt(
        self,
        settlement_ref: str,
        expected_amount_sat: int,
    ) -> ReceiptVerifyResult:
        """Verify a receipt against the rail.

        Wavecli command mapping DEFERRED to Gate 4 (needs live daemon verification).
        """
        raise NotImplementedError(
            "WavelengthAdapter.verify_receipt() requires a live Wavelength daemon. "
            "Command mapping deferred to Gate 4."
        )
