"""
Hermes Payments — settlement adapter boundary (v1 — P4 Wavelength seam).

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

wavecli CLI surface (source-verified):
- ``wavecli send <BOLT11> --offchain --dry-run --max-fee <sat>``
    returns a PrepareSendResponse JSON with send_intent_id, expected_fee_sat,
    rail, fee_known, payment_hash, etc.
- ``wavecli send <BOLT11> --offchain --force --max-fee <sat>``
    executes the payment and returns a sendResult JSON with status, kind,
    amount_sat, fee_sat, payment_hash, preimage, id.
- ``wavecli activity --format json --kind send``
    returns wallet activity entries for verification.
- ``wavecli activity inspect <id>``
    returns correlated swap/VTXO/ledger detail for one activity entry.
"""

from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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


def _build_wavecli_send_cmd(
    *,
    invoice: str,
    offchain: bool,
    dry_run: bool,
    force: bool,
    max_fee_sat: Optional[int] = None,
    rpc_server: Optional[str] = None,
    network: str = "regtest",
    no_tls: bool = True,
    no_macaroons: bool = True,
    json_output: bool = True,
    timeout: Optional[str] = None,
) -> List[str]:
    """Build a wavecli send command as a list of args (injection-safe).

    Every value is passed as a separate list element — no shell
    interpolation occurs.  Invoice and macaroon paths are never
    logged in full; errors/redacted via ``redact_sensitive``.
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

    cmd.extend(["send", invoice, "--offchain"])
    if dry_run:
        cmd.append("--dry-run")
    if force:
        cmd.append("--force")
    if max_fee_sat is not None:
        cmd.extend(["--max-fee", str(max_fee_sat)])
    return cmd


def _build_wavecli_activity_cmd(
    *,
    kind: str = "send",
    rpc_server: Optional[str] = None,
    network: str = "regtest",
    no_tls: bool = True,
    no_macaroons: bool = True,
    json_output: bool = True,
    inspect_id: Optional[str] = None,
) -> List[str]:
    """Build a wavecli activity [inspect] command (injection-safe)."""
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
    def run(self, cmd: List[str], *, timeout: int = 30) -> Dict[str, Any]:
        """Execute a wavecli command and return parsed JSON output.

        Parameters
        ----------
        cmd : list[str]
            The command to execute (already safely constructed).
        timeout : int
            Timeout in seconds.

        Returns
        -------
        dict
            Parsed JSON output from wavecli.

        Raises
        ------
        AdapterError
            If the command fails, times out, or returns non-JSON output.
        """
        ...


class SubprocessWavecliExecutor(WavecliExecutor):
    """Real executor that calls wavecli via subprocess."""

    def run(self, cmd: List[str], *, timeout: int = 30) -> Dict[str, Any]:
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

    def run(self, cmd: List[str], *, timeout: int = 30) -> Dict[str, Any]:
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


def _parse_dry_run_response(data: Dict[str, Any]) -> Tuple[int, Optional[str], str]:
    """Parse a wavecli send --dry-run JSON response.

    Returns (fee_sat, payment_hash, rail_label).
    Raises AdapterError on missing/unexpected fields.

    The dry-run response is the PrepareSendResponse printed as JSON.
    Source-verified fields:
    - expected_fee_sat: fee estimate
    - rail: SEND_RAIL_LIGHTNING, SEND_RAIL_ONCHAIN, etc.
    - payment_hash: Lightning payment hash (from progress or request)
    - fee_known: whether the fee is known
    """
    fee_known = data.get("fee_known")
    if fee_known is False:
        raise AdapterError(
            "dry-run returned unknown fee; cannot prepare safely"
        )

    fee_sat = data.get("expected_fee_sat", 0)
    if not isinstance(fee_sat, (int, float)):
        raise AdapterError(
            f"unexpected fee type: {type(fee_sat).__name__}"
        )
    fee_sat = int(fee_sat)

    # rail — expect SEND_RAIL_LIGHTNING or "LIGHTNING"
    rail_label = data.get("rail", "")
    if isinstance(rail_label, str):
        rail_label = rail_label.replace("SEND_RAIL_", "")
    else:
        rail_label = str(rail_label)

    # payment_hash may be nested in progress or top-level
    payment_hash: Optional[str] = data.get("payment_hash")
    if not payment_hash:
        progress = data.get("progress", {})
        if isinstance(progress, dict):
            payment_hash = progress.get("payment_hash")
    send_intent_id = data.get("send_intent_id", "")

    return fee_sat, payment_hash, rail_label


def _parse_send_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a wavecli send result (after --force dispatch).

    Returns a dict with: status, amount_sat, fee_sat, payment_hash,
    id (entry ID), kind.
    Raises AdapterError on missing fields or non-terminal status.

    Source-verified sendResult struct:
    - status: "COMPLETE" | "PENDING" | "FAILED" | "UNSPECIFIED"
    - kind: "SEND"
    - amount_sat: signed amount
    - fee_sat: fee paid
    - payment_hash: Lightning payment hash
    - id: wallet entry ID
    - preimage: hex preimage (only on COMPLETE)
    """
    status = data.get("status", "UNSPECIFIED")
    if not isinstance(status, str):
        raise AdapterError(
            f"unexpected status type: {type(status).__name__}"
        )
    status = status.upper()

    if status == "FAILED":
        raise AdapterError(
            f"wavecli send returned FAILED: "
            f"{redact_sensitive(str(data.get('error', 'unknown')))}"
        )

    if status not in ("COMPLETE", "PENDING", "UNSPECIFIED"):
        raise AmbiguousResult(
            f"unexpected send status: {status}"
        )

    entry_id = data.get("id", "")
    if not entry_id:
        raise AdapterError("wavecli send returned no entry ID")

    amount_sat = int(data.get("amount_sat", 0))
    fee_sat = int(data.get("fee_sat", 0))
    payment_hash = data.get("payment_hash", "")

    return {
        "status": status,
        "amount_sat": amount_sat,
        "fee_sat": fee_sat,
        "payment_hash": payment_hash,
        "id": entry_id,
    }


def _parse_activity_entry(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a wavecli activity inspect entry.

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
# Wavelength adapter (concrete, v1 — P4)
# ---------------------------------------------------------------------------


class WavelengthAdapter(SettlementAdapter):
    """First settlement adapter: Wavelength Lightning (REGTEST ONLY).

    Maps the generic prepare/execute/verify interface to wavecli's
    gRPC CLI surface.  The adapter:

    - Rejects every non-regtest configuration at construction time.
    - Uses dry-run for prepare (non-mutating).
    - Requires --force for execute (human approval gate).
    - Constructs all CLI commands as injection-safe list args.
    - Redacts invoices and macaroon paths from errors/audit.
    - Parses machine JSON strictly; unknown statuses → AmbiguousResult.
    - Verifies receipts only against adapter-owned activity data.

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
        """Non-mutating dry-run via ``wavecli send --dry-run``.

        Returns an opaque prepared payload containing the fee, invoice,
        payment hash, and send intent ID for the eventual execute().
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

        cmd = _build_wavecli_send_cmd(
            invoice=invoice,
            offchain=True,
            dry_run=True,
            force=False,
            max_fee_sat=max_fee_sat,
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
                f"wavecli dry-run failed: {redact_sensitive(str(e))}"
            ) from e

        # Strict JSON parsing — never assume success
        fee_sat, payment_hash, rail_label = _parse_dry_run_response(data)

        if fee_sat > max_fee_sat:
            raise AdapterError(
                f"dry-run fee {fee_sat} exceeds max_fee_sat {max_fee_sat}"
            )

        # Build opaque prepared payload (adapter-internal, never leaves sender)
        prepared_payload = json.dumps(
            {
                "fee_sat": fee_sat,
                "payment_hash": payment_hash or "",
                "rail": rail_label,
                "invoice": invoice,
                "send_intent_id": data.get("send_intent_id", ""),
                "amount_sat": amount_sat,
                "max_fee_sat": max_fee_sat,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        from .models import compute_prepared_hash
        prepared_hash = compute_prepared_hash(prepared_payload)

        return PrepareResult(
            fee_sat=fee_sat,
            prepared_hash=prepared_hash,
            rail=Rail.LIGHTNING,
            prepared_payload=prepared_payload,
        )

    def execute(
        self,
        prepared_payload: bytes,
        prepared_hash: str,
    ) -> ExecuteResult:
        """Execute the settlement via ``wavecli send --force``.

        Requires a valid prepared_payload matching prepared_hash
        (identity check).  The --force flag is ONLY appended after
        the orchestrator has verified human approval of the triple
        (intent_id, quote_id, prepared_hash).
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

        invoice = payload.get("invoice", "")
        if not invoice:
            raise AdapterError("prepared payload missing invoice")

        max_fee_sat = payload.get("max_fee_sat", 0)

        # ── Build the send command (force after approval) ───────────────
        cmd = _build_wavecli_send_cmd(
            invoice=invoice,
            offchain=True,
            dry_run=False,
            force=True,  # <-- only after policy approval
            max_fee_sat=max_fee_sat,
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
                f"wavecli send failed: {redact_sensitive(str(e))}"
            ) from e

        # ── Strict JSON parsing — never assume success ──────────────────
        result = _parse_send_result(data)

        # ── Durability: settlement_ref is the payment_hash ──────────────
        # If payment_hash is empty but we have a preimage, use that.
        settlement_ref = result["payment_hash"]
        if not settlement_ref:
            # Check preimage — a completed Lightning send has it
            preimage = data.get("preimage", "")
            if preimage:
                settlement_ref = preimage
            else:
                # No payment_hash and no preimage — ambiguous
                raise AmbiguousResult(
                    "send returned no payment_hash or preimage; "
                    "settlement status unknown"
                )

        # For PENDING status, the settlement may not be complete yet.
        # The orchestrator should treat this as RECONCILIATION_REQUIRED.
        if result["status"] == "PENDING":
            raise AmbiguousResult(
                f"send dispatched but status is PENDING (entry {result['id']}); "
                f"settlement not confirmed — manual verification required"
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
        """Verify a receipt against adapter-owned activity/status.

        Uses ``wavecli activity inspect <id>`` or ``wavecli activity --format json``
        to look up the settlement reference (payment_hash) in wallet activity.

        Source-grounded ID mapping:
        - settlement_ref must be a payment_hash that exists in wallet activity.
        - If no matching entry is found, we fail closed (verified=False).
        - If the entry exists but status is not COMPLETE, we fail closed.

        The adapter cannot verify entries it doesn't own — it only
        checks against activity that was settled through this adapter.
        """
        # ── Try to look up by payment_hash in activity ──────────────
        # First: list all send activity and find matching payment_hash
        cmd = _build_wavecli_activity_cmd(
            kind="send",
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
            entries = data
        elif isinstance(data, dict):
            entries = data.get("entries", data.get("items", []))
            if not entries and "entry" in data:
                entries = [data["entry"]]

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
            if parsed["status"] not in ("COMPLETE", "FINISHED"):
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
        # This means either:
        # (a) The settlement never happened through this adapter, or
        # (b) The activity query returned empty/malformed data.
        # In both cases, we MUST NOT assume success.
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
