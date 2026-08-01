"""
Hermes Payments — settlement adapter boundary (v2 — P4 Wavelength seam).

Defines the interface that settlement adapters (Wavelength, future
rails) must implement.  The adapter is the ONLY component that
touches funds; it never has access to seeds, passwords, or macaroons.

Adapter contract:
- prepare() is non-mutating (dry-run).
- execute() is called ONLY after human approval of
  (intent_id, quote_id, prepared_hash).
- The adapter NEVER retries automatically.

P4: WavelengthAdapter maps the generic prepare/execute/verify interface
to wavecli's gRPC CLI surface.  REGTEST ONLY — every non-regtest
configuration is rejected at construction time.

DESIGN NOTE — raw RPC path vs high-level `wavecli send`:

  The high-level `wavecli send <invoice> --force` (cmd_send.go walletSend)
  ALWAYS calls PrepareSend again before dispatching Send, consuming a NEW
  send_intent_id each time (lines 182-208).  This means it cannot execute
  the exact intent that policy prepared and approved — a non-mutating
  prepare followed by a force execute would produce two different intents.

  The raw `wavecli dev wavewalletrpc.WalletService PrepareSend` RPC is
  non-mutating and returns a short-lived, single-use send_intent_id.  The
  raw `wavecli dev wavewalletrpc.WalletService Send --send-intent-id <ID>`
  RPC consumes that exact intent without re-preparing.

  Therefore the adapter MUST use the raw RPC path for prepare/execute to
  guarantee that the approved intent is the one executed.  The high-level
  `send` verb is unsuitable for any programmatic prepare+execute flow.

  Receipt verification queries `activity --kind recv` (recipient-side),
  NOT `activity --kind send` (sender-side).  The receipt verifier lives
  at the recipient — it must check the recipient's incoming activity.

wavecli CLI surface (source-verified):
- ``wavecli dev wavewalletrpc.WalletService PrepareSend --request-json <JSON>``
    non-mutating; returns PrepareSendResponse with send_intent_id,
    amount_sat, expected_fee_sat, fee_known, payment_hash, rail,
    expires_at_unix, expected_total_outflow_sat, total_outflow_known.
- ``wavecli dev wavewalletrpc.WalletService Send --send-intent-id <ID>``
    consumes the single-use intent; returns SendResponse with entry
    (WalletEntry) and actual_amount_sat.
- ``wavecli activity --format json --kind recv``
    returns wallet activity entries for recipient-side receipt verification.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .models import Rail, RailReceiveInstruction

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
# Command construction helpers (injection-safe)
# ---------------------------------------------------------------------------


def _build_raw_rpc_cmd(
    *,
    service: str,
    method: str,
    request_json: str,
    rpc_server: Optional[str] = None,
    network: str = "regtest",
    no_tls: bool = True,
    no_macaroons: bool = True,
    json_output: bool = True,
    timeout: Optional[str] = None,
) -> List[str]:
    """Build a wavecli dev RPC command (injection-safe list args).

    Uses the raw gRPC CLI path to bypass the high-level send verb
    which re-calls PrepareSend on every invocation.
    """
    cmd: List[str] = [
        "wavecli",
        "--network", network,
        "--rpcserver", rpc_server or "localhost:10029",
    ]
    if no_tls:
        cmd.append("--no-tls")
    if no_macaroons:
        cmd.append("--no-macaroons")
    if json_output:
        cmd.append("--json")
    if timeout is not None:
        cmd.extend(["--timeout", timeout])

    cmd.extend(["dev", service, method, "--request-json", request_json])
    return cmd


def _build_wavecli_activity_cmd(
    *,
    kind: str = "recv",
    rpc_server: Optional[str] = None,
    network: str = "regtest",
    no_tls: bool = True,
    no_macaroons: bool = True,
    json_output: bool = True,
    inspect_id: Optional[str] = None,
) -> List[str]:
    """Build a wavecli activity [inspect] command (injection-safe).

    Default kind is 'recv' — receipt verification is recipient-side.
    """
    cmd: List[str] = [
        "wavecli",
        "--network", network,
        "--rpcserver", rpc_server or "localhost:10029",
    ]
    if no_tls:
        cmd.append("--no-tls")
    if no_macaroons:
        cmd.append("--no-macaroons")
    if json_output:
        cmd.append("--json")

    cmd.append("activity")
    if inspect_id:
        cmd.extend(["inspect", inspect_id])
    else:
        cmd.extend(["--format", "json", "--kind", kind])
    return cmd


# ---------------------------------------------------------------------------
# Sensitive-data redaction
# ---------------------------------------------------------------------------

_INVOICE_PATTERN = re.compile(
    r"(lnbc[a-zA-Z0-9]+|lntb[a-zA-Z0-9]+|lnbcrt[a-zA-Z0-9]+)",
    re.IGNORECASE,
)
_MACAROON_PATTERN = re.compile(r"(/[^\s:]+\.macaroon)")
_HASH_PATTERN = re.compile(r"\b([a-f0-9]{64})\b")


def redact_sensitive(text: str) -> str:
    """Redact invoices, macaroon paths, and hex hashes from text.

    Used in error messages and audit log entries to avoid leaking
    payment identifiers or credential paths.
    """
    text = _INVOICE_PATTERN.sub("<INVOICE>", text)
    text = _MACAROON_PATTERN.sub("<MACAROON_PATH>", text)
    text = _HASH_PATTERN.sub(lambda m: m.group()[:8] + "...", text)
    return text


# ---------------------------------------------------------------------------
# WavecliExecutor — subprocess seam
# ---------------------------------------------------------------------------


class WavecliExecutor(ABC):
    """Abstract executor for wavecli subprocess calls.

    The executor is the ONLY component that touches the wavecli binary.
    It never reads, logs, or constructs private keys — credentials
    (TLS certs, macaroons) stay local.
    """

    @abstractmethod
    def run(self, cmd: List[str], *, timeout: int = 30) -> Any:
        """Execute a wavecli command and return parsed JSON output.

        Parameters
        ----------
        cmd : list[str]
            The command to execute (already safely constructed).
        timeout : int
            Timeout in seconds.

        Returns
        -------
        Any
            Parsed JSON output from wavecli.  Individual RPC parsers validate
            the expected top-level shape.

        Raises
        ------
        AdapterError
            If the command fails, times out, or returns non-JSON output.
        """
        ...


class SubprocessWavecliExecutor(WavecliExecutor):
    """Real executor that calls wavecli via subprocess."""

    def run(self, cmd: List[str], *, timeout: int = 30) -> Any:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            raise AdapterError(
                "wavecli binary not found; install wavecli"
            )
        except subprocess.TimeoutExpired:
            raise AdapterError(
                f"wavecli timed out after {timeout}s"
            )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise AdapterError(
                f"wavecli exited with code {result.returncode}: "
                f"{redact_sensitive(stderr)}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            raise AdapterError("wavecli returned empty output")

        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise AdapterError(
                f"wavecli returned invalid JSON: {e}"
            ) from e


class FakeWavecliExecutor(WavecliExecutor):
    """In-memory executor for tests — no subprocess, no network.

    Pre-load responses via ``set_response`` or ``set_responses``.
    Raises ``AdapterError`` if called without a configured response.
    """

    def __init__(self) -> None:
        self._responses: list[Dict[str, Any]] = []
        self._calls: list[List[str]] = []

    def set_response(self, response: Any) -> None:
        """Set a single response for the next call."""
        self._responses.append(response)

    def set_responses(self, *responses: Any) -> None:
        """Set multiple sequential responses."""
        self._responses.extend(responses)

    def run(self, cmd: List[str], *, timeout: int = 30) -> Any:
        self._calls.append(cmd)
        if not self._responses:
            raise AdapterError("FakeWavecliExecutor: no response configured")
        return self._responses.pop(0)

    @property
    def calls(self) -> list[List[str]]:
        """Record of all calls made (for test assertions)."""
        return list(self._calls)

    @property
    def last_call(self) -> Optional[List[str]]:
        """The most recent call, or None."""
        return self._calls[-1] if self._calls else None


# ---------------------------------------------------------------------------
# Output parsing helpers
# ---------------------------------------------------------------------------

_RAW_RPC_SERVICE = "wavewalletrpc.WalletService"


def _parse_prepare_response(data: Any) -> Dict[str, Any]:
    """Parse a raw PrepareSendResponse from wavecli dev RPC.

    Returns dict with all binding fields:
      send_intent_id, amount_sat, expected_fee_sat, fee_known,
      expected_total_outflow_sat, total_outflow_known, rail,
      payment_hash, expires_at_unix, quote_status.

    Raises AdapterError on missing send_intent_id or invalid data.
    """
    if not isinstance(data, dict):
        raise AdapterError(
            f"PrepareSend returned expected object, got {type(data).__name__}"
        )

    send_intent_id = data.get("send_intent_id", "")
    if not send_intent_id:
        raise AdapterError(
            "PrepareSend returned empty send_intent_id; "
            "cannot proceed without a valid intent token"
        )

    expires_at = data.get("expires_at_unix", 0)
    if not isinstance(expires_at, (int, float)):
        raise AdapterError(
            f"unexpected expires_at_unix type: {type(expires_at).__name__}"
        )

    fee_sat = data.get("expected_fee_sat", 0)
    if not isinstance(fee_sat, (int, float)):
        raise AdapterError(
            f"unexpected expected_fee_sat type: {type(fee_sat).__name__}"
        )

    fee_known = data.get("fee_known", False)
    if not fee_known:
        raise AdapterError(
            "PrepareSend returned fee_known=false; "
            "fee must be known before policy approval"
        )

    total_outflow_known = data.get("total_outflow_known", False)
    total_outflow_sat = data.get("expected_total_outflow_sat", 0)
    if not total_outflow_known:
        raise AdapterError(
            "PrepareSend returned total_outflow_known=false; "
            "total outflow must be known before policy approval"
        )

    payment_hash = data.get("payment_hash", "")
    if not isinstance(payment_hash, str) or not payment_hash:
        raise AdapterError(
            "PrepareSend returned empty payment_hash; "
            "cannot bind a recipient receipt safely"
        )

    rail_raw = data.get("rail", "")
    if isinstance(rail_raw, str):
        rail_label = rail_raw.replace("SEND_RAIL_", "")
    else:
        rail_label = str(rail_raw)

    amount_sat = data.get("amount_sat", 0)
    if not isinstance(amount_sat, (int, float)):
        raise AdapterError(
            f"unexpected amount_sat type: {type(amount_sat).__name__}"
        )

    return {
        "send_intent_id": send_intent_id,
        "amount_sat": int(amount_sat),
        "expected_fee_sat": int(fee_sat),
        "fee_known": bool(fee_known),
        "expected_total_outflow_sat": int(total_outflow_sat),
        "total_outflow_known": bool(total_outflow_known),
        "rail": rail_label,
        "payment_hash": payment_hash,
        "expires_at_unix": int(expires_at),
        "quote_status": data.get("quote_status", ""),
    }


def _parse_send_response(data: Any) -> Dict[str, Any]:
    """Parse a raw SendResponse from wavecli dev RPC.

    SendResponse has: entry (WalletEntry) + actual_amount_sat.
    WalletEntry.id for LIGHTNING SEND/RECV is payment_hash.

    Returns dict with: entry_id, payment_hash, amount_sat, status.
    Raises AdapterError on missing/malformed data.
    """
    if not isinstance(data, dict):
        raise AdapterError(
            f"Send returned expected object, got {type(data).__name__}"
        )

    entry = data.get("entry")
    if not isinstance(entry, dict):
        raise AdapterError(
            f"SendResponse missing 'entry' dict; got "
            f"{type(entry).__name__}"
        )

    entry_id = entry.get("id", "")
    if not entry_id:
        raise AdapterError("SendResponse entry has empty id")

    payment_hash = _extract_payment_hash(entry)
    if not payment_hash:
        # For Lightning sends, WalletEntry.id IS the payment_hash
        payment_hash = entry_id

    amount_sat = int(data.get("actual_amount_sat", 0))
    if amount_sat == 0:
        # Fallback to entry amount
        amount_sat = int(entry.get("amount_sat", 0))

    status_raw = entry.get("status", "UNSPECIFIED")
    if isinstance(status_raw, int):
        # Proto enum int — normalize to string
        _ENUM_MAP = {0: "UNSPECIFIED", 1: "PENDING", 2: "COMPLETE", 3: "FAILED"}
        status = _ENUM_MAP.get(status_raw, str(status_raw))
    elif isinstance(status_raw, str):
        status = status_raw.upper().replace("ENTRY_STATUS_", "")
    else:
        status = str(status_raw)

    fee_sat = int(entry.get("fee_sat", 0))

    return {
        "entry_id": entry_id,
        "payment_hash": payment_hash,
        "amount_sat": amount_sat,
        "fee_sat": fee_sat,
        "status": status,
    }


def _parse_activity_entry(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a wavecli activity list entry.

    Returns relevant fields for receipt verification.
    Raises AdapterError on malformed data.

    Source-verified WalletEntry fields:
    - status: COMPLETE | PENDING | FAILED | UNSPECIFIED
    - amount_sat, fee_sat
    - kind: SEND | RECV | DEPOSIT | EXIT
    - progress.payment_hash
    - progress.preimage
    """
    if not isinstance(data, dict):
        raise AdapterError(
            f"expected dict for activity entry, got {type(data).__name__}"
        )

    # The response may be wrapped in an "entry" key (InspectActivityResponse)
    # or be the raw entry itself
    entry = data
    if "entry" in data and isinstance(data["entry"], dict):
        entry = data["entry"]

    status = entry.get("status", "UNSPECIFIED")
    if isinstance(status, str):
        status = status.upper()
        status = status.replace("ENTRY_STATUS_", "")
    else:
        status = str(status)

    return {
        "status": status,
        "amount_sat": int(entry.get("amount_sat", 0)),
        "fee_sat": int(entry.get("fee_sat", 0)),
        "payment_hash": _extract_payment_hash(entry),
    }


def _extract_payment_hash(entry: Dict[str, Any]) -> str:
    """Extract payment_hash from an activity entry.

    The payment hash may be at the top level, in progress, or in
    the request sub-message.
    """
    # Top-level
    ph = entry.get("payment_hash", "")
    if ph:
        return str(ph)

    # Nested in progress
    progress = entry.get("progress", {})
    if isinstance(progress, dict):
        ph = progress.get("payment_hash", "")
        if ph:
            return str(ph)

    # Nested in request → lightning_invoice → payment_hash
    request = entry.get("request", {})
    if isinstance(request, dict):
        ln = request.get("lightning_invoice", {})
        if isinstance(ln, dict):
            ph = ln.get("payment_hash", "")
            if ph:
                return str(ph)

    return ""


# ---------------------------------------------------------------------------
# Wavelength adapter (concrete, v2 — P4 RAW RPC PATH)
# ---------------------------------------------------------------------------


class WavelengthAdapter(SettlementAdapter):
    """First settlement adapter: Wavelength Lightning (REGTEST ONLY).

    Maps the generic prepare/execute/verify interface to wavecli's
    gRPC CLI surface.  The adapter:
    - Rejects every non-regtest configuration at construction time.
    - Uses raw PrepareSend RPC for prepare (non-mutating).
    - Uses raw Send RPC for execute (single-use send_intent_id).
    - Verifies receipts against recipient-side activity (kind=recv).
    - Constructs all CLI commands as injection-safe list args.
    - Redacts invoices and macaroon paths from errors/audit.
    - Parses machine JSON strictly; unknown statuses → AmbiguousResult.

    DESIGN: This adapter uses the raw `wavecli dev` RPC path rather than
    the high-level `wavecli send` verb.  The high-level `send` verb
    (cmd_send.go walletSend) ALWAYS re-calls PrepareSend before dispatch,
    consuming a NEW send_intent_id.  This means the execute step would
    never dispatch the intent that was prepared and approved — a fatal
    design flaw for any programmatic prepare→approve→execute flow.

    The raw PrepareSend RPC is non-mutating and returns a short-lived,
    single-use send_intent_id.  The raw Send RPC consumes exactly that
    intent without re-preparing.

    Receipt verification queries `activity --kind recv` (recipient-side),
    not `activity --kind send` (sender-side).  The receipt verifier
    lives at the recipient — it must check the recipient's incoming
    activity for a matching payment_hash and amount.

    Parameters
    ----------
    executor : WavecliExecutor
        The subprocess executor (SubprocessWavecliExecutor or FakeWavecliExecutor).
    rpc_server : str
        wavecli --rpcserver value (default: localhost:10029).
    network : str
        Must be "regtest".  Any other value is rejected.
    no_tls : bool
        Pass --no-tls to wavecli (required for local regtest).
    no_macaroons : bool
        Pass --no-macaroons to wavecli (required for local regtest).
    """

    def __init__(
        self,
        *,
        executor: WavecliExecutor,
        rpc_server: str = "localhost:10029",
        network: str = "regtest",
        no_tls: bool = True,
        no_macaroons: bool = True,
    ):
        # ── Non-negotiable: REGTEST ONLY ────────────────────────────
        if network != "regtest":
            raise ValueError(
                f"WavelengthAdapter only supports regtest; got '{network}'. "
                "Never default to mainnet."
            )
        self._executor = executor
        self._rpc_server = rpc_server
        self._network = network
        self._no_tls = no_tls
        self._no_macaroons = no_macaroons

    @property
    def rail(self) -> Rail:
        return Rail.LIGHTNING

    def prepare(
        self,
        receive_instruction: RailReceiveInstruction,
        amount_sat: int,
        max_fee_sat: int,
    ) -> PrepareResult:
        """Non-mutating prepare via raw PrepareSend RPC.

        Uses the raw gRPC PrepareSend RPC which is non-mutating and
        returns a short-lived send_intent_id.  This avoids the
        high-level `wavecli send --dry-run` which, while non-mutating
        in dry-run mode, goes through a code path that would be
        unsuitable for the execute step (re-calls PrepareSend).

        Returns an opaque prepared payload containing all binding
        fields: send_intent_id, amount_sat, fee, total_outflow,
        payment_hash, expires_at_unix, rail.
        The payload is SHA-256 hashed to produce the ``prepared_hash``
        that the human must approve before execution.
        """
        if receive_instruction.rail != Rail.LIGHTNING:
            raise AdapterError(
                f"unsupported rail: {receive_instruction.rail.value}"
            )
        invoice = receive_instruction.invoice
        if not invoice:
            raise AdapterError("no invoice in receive instruction")

        # Build raw PrepareSend request JSON
        prepare_req = json.dumps({
            "invoice": invoice,
            "max_fee_sat": max_fee_sat,
        }, sort_keys=True)

        cmd = _build_raw_rpc_cmd(
            service=_RAW_RPC_SERVICE,
            method="PrepareSend",
            request_json=prepare_req,
            rpc_server=self._rpc_server,
            network=self._network,
            no_tls=self._no_tls,
            no_macaroons=self._no_macaroons,
            json_output=True,
        )

        try:
            data = self._executor.run(cmd)
        except AdapterError:
            raise
        except Exception as e:
            raise AdapterError(
                f"raw PrepareSend failed: {redact_sensitive(str(e))}"
            ) from e

        # Strict parsing — validates send_intent_id, fee_known,
        # total_outflow_known, expiry, and all binding fields
        prepared = _parse_prepare_response(data)

        # The exact daemon preview, not merely its fee, is what policy approves.
        # A stale or inconsistent preview must never produce an approval.
        if prepared["expires_at_unix"] <= int(time.time()):
            raise AdapterError("PrepareSend returned an expired intent")
        if prepared["amount_sat"] != amount_sat:
            raise AdapterError(
                f"PrepareSend amount {prepared['amount_sat']} does not match "
                f"quoted amount {amount_sat}"
            )
        if prepared["expected_fee_sat"] > max_fee_sat:
            raise AdapterError(
                f"raw PrepareSend fee {prepared['expected_fee_sat']} "
                f"exceeds max_fee_sat {max_fee_sat}"
            )
        if prepared["expected_total_outflow_sat"] != (
            prepared["amount_sat"] + prepared["expected_fee_sat"]
        ):
            raise AdapterError(
                "PrepareSend total outflow does not equal amount plus fee"
            )

        # Build opaque prepared payload with ALL binding fields
        # (adapter-internal, never leaves the sender)
        prepared_payload = json.dumps({
            "send_intent_id": prepared["send_intent_id"],
            "amount_sat": prepared["amount_sat"],
            "fee_sat": prepared["expected_fee_sat"],
            "total_outflow_sat": prepared["expected_total_outflow_sat"],
            "payment_hash": prepared["payment_hash"],
            "expires_at_unix": prepared["expires_at_unix"],
            "rail": prepared["rail"],
            "quote_status": prepared["quote_status"],
            "invoice": invoice,
            "max_fee_sat": max_fee_sat,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

        from .models import compute_prepared_hash
        prepared_hash = compute_prepared_hash(prepared_payload)

        return PrepareResult(
            fee_sat=prepared["expected_fee_sat"],
            prepared_hash=prepared_hash,
            rail=Rail.LIGHTNING,
            prepared_payload=prepared_payload,
        )

    def execute(
        self,
        prepared_payload: bytes,
        prepared_hash: str,
    ) -> ExecuteResult:
        """Execute settlement via raw Send RPC with saved send_intent_id.

        This uses the raw `wavecli dev wavewalletrpc.WalletService Send`
        RPC which consumes the single-use send_intent_id from prepare.
        It does NOT re-call PrepareSend — the exact intent that policy
        approved is the one that gets dispatched.

        Any transport error after dispatch is AmbiguousResult (no retry).
        """
        # ── Identity check: prepared_payload must match prepared_hash ──
        from .models import compute_prepared_hash
        actual_hash = compute_prepared_hash(prepared_payload)
        if actual_hash != prepared_hash:
            raise AdapterError(
                "prepared_payload does not match prepared_hash; "
                "execute called with tampered or wrong payload"
            )

        # ── Decode the prepared payload ────────────────────────────────
        try:
            payload = json.loads(prepared_payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise AdapterError(
                f"cannot decode prepared payload: {e}"
            ) from e

        send_intent_id = payload.get("send_intent_id", "")
        if not send_intent_id:
            raise AdapterError(
                "prepared payload missing send_intent_id; "
                "cannot dispatch without a valid intent token"
            )

        # ── Validate expiry before dispatch ─────────────────────────────
        expires_at = payload.get("expires_at_unix", 0)
        if not isinstance(expires_at, int) or expires_at <= int(time.time()):
            raise AdapterError(
                "prepared intent is missing, malformed, or expired; "
                "re-prepare before executing"
            )

        # ── Build raw Send RPC command ──────────────────────────────────
        send_req = json.dumps({
            "send_intent_id": send_intent_id,
        }, sort_keys=True)

        cmd = _build_raw_rpc_cmd(
            service=_RAW_RPC_SERVICE,
            method="Send",
            request_json=send_req,
            rpc_server=self._rpc_server,
            network=self._network,
            no_tls=self._no_tls,
            no_macaroons=self._no_macaroons,
            json_output=True,
        )

        try:
            data = self._executor.run(cmd)
        except Exception as e:
            # Send was attempted.  A CLI/gRPC error cannot prove that the
            # daemon did not accept it, so execution errors are reconciliation
            # cases, never a retryable failure.
            raise AmbiguousResult(
                f"raw Send outcome unknown: {redact_sensitive(str(e))}"
            ) from e

        # ── Parse raw SendResponse — entry + actual_amount_sat ──────────
        result = _parse_send_response(data)

        # ── Map status to terminal/ambiguous ────────────────────────────
        status = result["status"]

        if status == "FAILED":
            raise AdapterError(
                "raw Send returned FAILED: entry "
                f"{redact_sensitive(result['entry_id'])}"
            )

        if status == "PENDING":
            # Funds were dispatched but settlement not confirmed
            raise AmbiguousResult(
                f"raw Send dispatched but status is PENDING "
                f"(entry {redact_sensitive(result['entry_id'])}); "
                f"settlement not confirmed — manual verification required"
            )
        if status != "COMPLETE":
            raise AmbiguousResult(f"unknown send status: {status}")

        # COMPLETE is the sole terminal success state.
        # settlement_ref = payment_hash (for Lightning, WalletEntry.id = payment_hash)
        settlement_ref = result["payment_hash"]
        if not settlement_ref:
            raise AmbiguousResult(
                "raw Send returned no payment_hash; "
                "settlement status unknown"
            )

        # The daemon result must match the exact preview approved by policy.
        if result["amount_sat"] != payload.get("amount_sat"):
            raise AmbiguousResult(
                "raw Send amount differs from the prepared preview"
            )
        if result["entry_id"] != payload.get("payment_hash"):
            raise AmbiguousResult(
                "raw Send entry ID differs from the prepared payment hash"
            )
        if result["payment_hash"] != payload.get("payment_hash"):
            raise AmbiguousResult(
                "raw Send payment hash differs from the prepared preview"
            )

        return ExecuteResult(
            settlement_ref=settlement_ref,
            amount_sat=result["amount_sat"],
            fee_sat=result["fee_sat"],
            rail=Rail.LIGHTNING,
        )

    def verify_receipt(
        self,
        settlement_ref: str,
        expected_amount_sat: int,
    ) -> ReceiptVerifyResult:
        """Verify a receipt against recipient-side activity (kind=recv).

        The receipt verifier lives at the recipient.  It must query the
        recipient's own incoming activity (kind=recv), NOT the sender's
        outgoing activity (kind=send).

        Uses ``wavecli activity --format json --kind recv`` to list
        recipient incoming activity and find a COMPLETE entry matching
        the expected payment_hash and amount_sat.

        Source-grounded ID mapping:
        - settlement_ref is a payment_hash that must exist in
          recipient recv activity.
        - If no matching entry is found, we fail closed (verified=False).
        - If the entry exists but status is not COMPLETE, we fail closed.
        - Amount must match exactly.
        """
        # ── Query recipient-side recv activity ─────────────────────
        cmd = _build_wavecli_activity_cmd(
            kind="recv",
            rpc_server=self._rpc_server,
            network=self._network,
            no_tls=self._no_tls,
            no_macaroons=self._no_macaroons,
            json_output=True,
        )

        try:
            data = self._executor.run(cmd)
        except AdapterError as e:
            # Cannot reach activity — fail closed
            return ReceiptVerifyResult(
                verified=False,
                settlement_ref=settlement_ref,
                amount_sat=0,
                fee_sat=0,
                error=f"cannot query activity: {redact_sensitive(str(e))}",
            )
        except Exception as e:
            return ReceiptVerifyResult(
                verified=False,
                settlement_ref=settlement_ref,
                amount_sat=0,
                fee_sat=0,
                error=f"activity query failed: {redact_sensitive(str(e))}",
            )

        # ── Parse activity response ─────────────────────────────────
        # The response may be a raw proto (list of entries) or a
        # structured response with an "entries" key.
        entries: List[Dict[str, Any]] = []
        if isinstance(data, list):
            entries = [entry for entry in data if isinstance(entry, dict)]
        elif isinstance(data, dict):
            candidate = data.get("entries", data.get("items", []))
            if not candidate and "entry" in data:
                candidate = [data["entry"]]
            if isinstance(candidate, list):
                entries = [entry for entry in candidate if isinstance(entry, dict)]
            elif isinstance(candidate, dict):
                entries = [candidate]

        # ── Find matching entry by payment_hash ─────────────────────
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                parsed = _parse_activity_entry(entry)
            except AdapterError:
                continue

            if parsed["payment_hash"] != settlement_ref:
                continue

            # ── Found a match — check status and amount ─────────────
            if parsed["status"] != "COMPLETE":
                return ReceiptVerifyResult(
                    verified=False,
                    settlement_ref=settlement_ref,
                    amount_sat=parsed["amount_sat"],
                    fee_sat=parsed["fee_sat"],
                    error=(
                        f"entry found but status is {parsed['status']}; "
                        f"expected COMPLETE"
                    ),
                )

            if parsed["amount_sat"] != expected_amount_sat:
                return ReceiptVerifyResult(
                    verified=False,
                    settlement_ref=settlement_ref,
                    amount_sat=parsed["amount_sat"],
                    fee_sat=parsed["fee_sat"],
                    error=(
                        f"amount mismatch: expected {expected_amount_sat}, "
                        f"got {parsed['amount_sat']}"
                    ),
                )

            return ReceiptVerifyResult(
                verified=True,
                settlement_ref=settlement_ref,
                amount_sat=parsed["amount_sat"],
                fee_sat=parsed["fee_sat"],
            )

        # ── No matching entry found — fail closed ───────────────────
        return ReceiptVerifyResult(
            verified=False,
            settlement_ref=settlement_ref,
            amount_sat=0,
            fee_sat=0,
            error=(
                f"no activity entry found for settlement_ref "
                f"{settlement_ref[:8]}...; cannot verify receipt "
                f"without source-grounded ID mapping (fail closed)"
            ),
        )
