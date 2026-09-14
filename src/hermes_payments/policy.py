"""
Hermes Payments — P2 core policy engine (v0).

Rail-neutral orchestrator that wraps the state machine with:

- **IdempotencyStore** — persistent idempotency/replay prevention for
  intents, approvals (by intent_id + quote_id + prepared_hash triple),
  and receipts.
- **AuditLog** — append-only JSONL audit trail; each entry carries a
  monotonically increasing ``seq`` and a real timestamp.
- **PaymentOrchestrator** — drives a single PaymentIntent through its
  lifecycle, enforcing:
  - expiry validation (intent + quote)
  - recipient allowlist hook
  - rail allowlist hook
  - fee constraints (fee <= max_fee_sat)
  - approval binding to (intent_id, quote_id, prepared_hash)
  - fail-closed on ambiguous adapter results (→ RECONCILIATION_REQUIRED)

No Buzz or Wavelength I/O — all network interaction is behind the
``SettlementAdapter`` interface, which is injected.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapter import (
    AdapterError,
    AmbiguousResult,
    PrepareResult,
    ReconcileResult,
    SettlementAdapter,
    redact_sensitive,
)
from .models import (
    AgentIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    PaymentReceipt,
    Rail,
    compute_id,
    compute_prepared_hash,
)
from .state_machine import (
    PaymentState,
    transition,
)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PolicyError(Exception):
    """Base class for all policy engine errors."""


class SubmissionRejected(PolicyError):
    """Intent or quote rejected by policy (expiry, fee, allowlist)."""


class RecipientRejected(PolicyError):
    """Recipient not in the allowlist."""


class ApprovalRejected(PolicyError):
    """Approval failed validation (mismatch or replay)."""


class StateError(PolicyError):
    """State machine violation — action not permitted in current state."""


class UnknownIntent(PolicyError):
    """Referenced intent_id is not tracked by this orchestrator."""


# ---------------------------------------------------------------------------
# Idempotency store (persistent, file-backed, append-only records)
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """Tracks intent IDs, approval triples, and receipt bindings.

    Persisted as JSON so replay prevention survives process restarts.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._intents: set[str] = set()
        self._approvals: set[str] = set()
        self._receipts: Dict[str, str] = {}
        self._load()

    # -- internal --

    def _load(self) -> None:
        if self._path and os.path.exists(self._path):
            with open(self._path, "r") as f:
                data = json.load(f)
            self._intents = set(data.get("intents", []))
            self._approvals = set(data.get("approvals", []))
            self._receipts = dict(data.get("receipts", {}))

    def _save(self) -> None:
        if not self._path:
            return
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        # Write to a temp file first, then atomically replace to prevent
        # corruption on crash (crash-safe persistence).
        dir_name = os.path.dirname(self._path) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "intents": sorted(self._intents),
                        "approvals": sorted(self._approvals),
                        "receipts": self._receipts,
                    },
                    f,
                    sort_keys=True,
                )
            os.replace(tmp_path, self._path)
        except Exception:
            # Clean up temp file on failure; original is untouched.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- intent idempotency --

    def has_intent(self, intent_id: str) -> bool:
        return intent_id in self._intents

    def record_intent(self, intent_id: str) -> None:
        self._intents.add(intent_id)
        self._save()

    # -- approval idempotency (triple binding) --

    @staticmethod
    def _approval_key(intent_id: str, quote_id: str, prepared_hash: str) -> str:
        return f"{intent_id}:{quote_id}:{prepared_hash}"

    def has_approval(self, triple: Tuple[str, str, str]) -> bool:
        intent_id, quote_id, prepared_hash = triple
        return self._approval_key(intent_id, quote_id, prepared_hash) in self._approvals

    def record_approval(self, triple: Tuple[str, str, str]) -> None:
        intent_id, quote_id, prepared_hash = triple
        key = self._approval_key(intent_id, quote_id, prepared_hash)
        self._approvals.add(key)
        self._save()

    # -- receipt idempotency --

    def has_receipt(self, intent_id: str) -> bool:
        return intent_id in self._receipts

    def record_receipt(self, intent_id: str, settlement_ref: str) -> None:
        self._receipts[intent_id] = settlement_ref
        self._save()

    def get_receipt(self, intent_id: str) -> Optional[str]:
        return self._receipts.get(intent_id)


# ---------------------------------------------------------------------------
# Append-only audit log
# ---------------------------------------------------------------------------


class AuditLog:
    """Append-only JSONL audit trail.

    Each entry gets a sequential ``seq`` (monotonically increasing per file)
    and a wall-clock ``ts`` timestamp.
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path
        self._seq = 0
        self._load_seq()

    def _load_seq(self) -> None:
        if self._path and os.path.exists(self._path):
            with open(self._path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entry = json.loads(line)
                        self._seq = max(self._seq, entry.get("seq", 0) + 1)

    def append(self, event: str, **kwargs: Any) -> None:
        entry = {
            "seq": self._seq,
            "ts": time.time(),
            "event": event,
        }
        entry.update(kwargs)
        if self._path:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(entry, sort_keys=True) + "\n")
        self._seq += 1

    def entries(self) -> List[dict[str, Any]]:
        if not self._path or not os.path.exists(self._path):
            return []
        result: List[dict[str, Any]] = []
        with open(self._path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    result.append(json.loads(line))
        return result


# ---------------------------------------------------------------------------
# Intent record (in-memory state tracking)
# ---------------------------------------------------------------------------


@dataclass
class IntentRecord:
    intent: PaymentIntent
    state: PaymentState = PaymentState.DRAFT
    quote: Optional[PaymentQuote] = None
    prepared: Optional[PrepareResult] = None
    approval: Optional[PaymentApproval] = None
    receipt: Optional[PaymentReceipt] = None
    reconciliation: Optional[ReconcileResult] = None
    quote_expires_at: int = 0
    expires_at: int = 0


# ---------------------------------------------------------------------------
# PaymentOrchestrator — the rail-neutral policy engine
# ---------------------------------------------------------------------------


class PaymentOrchestrator:
    """Drives one or more PaymentIntents through their lifecycle.

    Parameters
    ----------
    adapter : SettlementAdapter
        The rail-specific adapter. Only touched after human approval.
    store_path : optional path to a JSON idempotency store.
    audit_path : optional path to a JSONL audit log.
    recipient_allowlist : optional callable(pubkey) -> bool.
    rail_allowlist : optional callable(Rail) -> bool.
    clock : optional callable() -> int, for time injection (testing).
    """

    def __init__(
        self,
        adapter: SettlementAdapter,
        store_path: Optional[str] = None,
        audit_path: Optional[str] = None,
        state_path: Optional[str] = None,
        recipient_allowlist: Optional[Callable[[str], bool]] = None,
        rail_allowlist: Optional[Callable[[Rail], bool]] = None,
        clock: Optional[Callable[[], int]] = None,
    ):
        self._adapter = adapter
        self._store = IdempotencyStore(path=store_path)
        self._audit = AuditLog(path=audit_path)
        self._state_path = state_path
        self._recipient_allowlist = recipient_allowlist
        self._rail_allowlist = rail_allowlist
        self._clock: Callable[[], int] = clock or (lambda: int(time.time()))
        self._intents: Dict[str, IntentRecord] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # Public read interface
    # ------------------------------------------------------------------

    def state(self, intent_id: str) -> PaymentState:
        rec = self._get_record(intent_id)
        return rec.state

    def get_intent(self, intent_id: str) -> Optional[PaymentIntent]:
        rec = self._intents.get(intent_id)
        return rec.intent if rec else None

    def has_intent(self, intent_id: str) -> bool:
        """Return whether this process has a durable record for an intent."""
        return intent_id in self._intents

    def get_quote(self, intent_id: str) -> Optional[PaymentQuote]:
        """Return the tracked quote for an intent, if one exists."""
        rec = self._intents.get(intent_id)
        return rec.quote if rec else None

    def get_prepared(self, intent_id: str) -> Optional[PrepareResult]:
        """Return the tracked prepared result for an intent, if one exists."""
        rec = self._intents.get(intent_id)
        return rec.prepared if rec else None

    def records(self) -> List[IntentRecord]:
        """Return a snapshot of tracked records for status rendering."""
        return list(self._intents.values())

    @property
    def _audit_log(self) -> AuditLog:
        return self._audit

    def now(self) -> int:
        return self._clock()

    # ------------------------------------------------------------------
    # Durable lifecycle state
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_prepared(prepared: PrepareResult) -> dict[str, Any]:
        """Encode an adapter prepare result without losing opaque bytes."""
        return {
            "fee_sat": prepared.fee_sat,
            "prepared_hash": prepared.prepared_hash,
            "rail": prepared.rail.value,
            "prepared_payload": base64.b64encode(prepared.prepared_payload).decode("ascii"),
        }

    @staticmethod
    def _decode_prepared(data: dict[str, Any]) -> PrepareResult:
        """Decode and integrity-check a persisted adapter prepare result."""
        try:
            payload = base64.b64decode(data["prepared_payload"], validate=True)
            prepared_hash = str(data["prepared_hash"])
            if compute_prepared_hash(payload) != prepared_hash:
                raise ValueError("prepared payload hash mismatch")
            return PrepareResult(
                fee_sat=int(data["fee_sat"]),
                prepared_hash=prepared_hash,
                rail=Rail(str(data["rail"])),
                prepared_payload=payload,
            )
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise StateError(f"invalid persisted prepared payload: {exc}") from exc

    @staticmethod
    def _encode_reconciliation(
        result: Optional[ReconcileResult],
    ) -> Optional[dict[str, Any]]:
        if result is None:
            return None
        return {
            "status": result.status,
            "settlement_ref": result.settlement_ref,
            "amount_sat": result.amount_sat,
            "fee_sat": result.fee_sat,
            "rail": result.rail.value if result.rail is not None else None,
            "verified": result.verified,
            "error": result.error,
        }

    @staticmethod
    def _decode_reconciliation(
        data: Optional[dict[str, Any]],
    ) -> Optional[ReconcileResult]:
        if data is None:
            return None
        try:
            rail = data.get("rail")
            return ReconcileResult(
                status=str(data["status"]),
                settlement_ref=str(data.get("settlement_ref", "")),
                amount_sat=int(data.get("amount_sat", 0)),
                fee_sat=int(data.get("fee_sat", 0)),
                rail=Rail(str(rail)) if rail is not None else None,
                verified=bool(data.get("verified", False)),
                error=data.get("error"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StateError(f"invalid persisted reconciliation result: {exc}") from exc

    def _state_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "intents": {
                intent_id: {
                    "intent": rec.intent.model_dump(mode="json"),
                    "state": rec.state.value,
                    "quote": rec.quote.model_dump(mode="json") if rec.quote else None,
                    "prepared": self._encode_prepared(rec.prepared) if rec.prepared else None,
                    "approval": rec.approval.model_dump(mode="json") if rec.approval else None,
                    "receipt": rec.receipt.model_dump(mode="json") if rec.receipt else None,
                    "reconciliation": self._encode_reconciliation(rec.reconciliation),
                    "quote_expires_at": rec.quote_expires_at,
                    "expires_at": rec.expires_at,
                }
                for intent_id, rec in self._intents.items()
            },
        }

    def _save_state(self) -> None:
        """Atomically persist lifecycle state, or raise."""
        if not self._state_path:
            return
        path = os.path.abspath(self._state_path)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state_payload(), handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_state(self) -> None:
        """Restore lifecycle state and fail closed if it is corrupt."""
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if data.get("version") != 1 or not isinstance(data.get("intents"), dict):
                raise ValueError("unsupported lifecycle state format")
            for intent_id, raw in data["intents"].items():
                intent = PaymentIntent.model_validate(raw["intent"])
                if intent.id != intent_id:
                    raise ValueError("intent key does not match intent.id")
                quote_data = raw.get("quote")
                approval_data = raw.get("approval")
                receipt_data = raw.get("receipt")
                rec = IntentRecord(
                    intent=intent,
                    state=PaymentState(str(raw["state"])),
                    quote=PaymentQuote.model_validate(quote_data) if quote_data else None,
                    prepared=(
                        self._decode_prepared(raw["prepared"])
                        if raw.get("prepared")
                        else None
                    ),
                    approval=(
                        PaymentApproval.model_validate(approval_data)
                        if approval_data
                        else None
                    ),
                    receipt=(
                        PaymentReceipt.model_validate(receipt_data)
                        if receipt_data
                        else None
                    ),
                    reconciliation=self._decode_reconciliation(raw.get("reconciliation")),
                    quote_expires_at=int(raw.get("quote_expires_at", 0)),
                    expires_at=int(raw.get("expires_at", intent.expires_at)),
                )
                self._intents[intent_id] = rec
                # Repair a partial restore without ever treating a durable
                # lifecycle record as a new intent.
                if not self._store.has_intent(intent_id):
                    self._store.record_intent(intent_id)
                if rec.approval is not None:
                    triple = (
                        intent_id,
                        rec.approval.quote_id,
                        rec.approval.prepared_hash,
                    )
                    if not self._store.has_approval(triple):
                        self._store.record_approval(triple)
                if rec.receipt is not None and not self._store.has_receipt(intent_id):
                    self._store.record_receipt(intent_id, rec.receipt.settlement_ref)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, StateError) as exc:
            self._intents.clear()
            raise StateError(f"cannot load lifecycle state safely: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_record(self, intent_id: str) -> IntentRecord:
        rec = self._intents.get(intent_id)
        if rec is None:
            raise UnknownIntent(f"intent {intent_id} is not tracked")
        return rec

    def _check_intent_expired(self, rec: IntentRecord) -> None:
        now = self.now()
        if rec.expires_at and now > rec.expires_at:
            raise SubmissionRejected(f"intent {rec.intent.id} has expired")

    def _check_quote_expired(self, rec: IntentRecord) -> None:
        now = self.now()
        if rec.quote_expires_at and now > rec.quote_expires_at:
            raise StateError(f"quote for intent {rec.intent.id} has expired")

    def _validate_recipient(self, recipient: AgentIdentity) -> None:
        if self._recipient_allowlist is not None:
            if not self._recipient_allowlist(recipient.pubkey):
                raise RecipientRejected(
                    f"recipient {recipient.pubkey} is not in the allowlist"
                )

    def _validate_rail(self, rail: Rail) -> None:
        if self._rail_allowlist is not None:
            if not self._rail_allowlist(rail):
                raise StateError(f"rail {rail.value} is not in the allowlist")

    def _find_by_state(self, state: PaymentState) -> IntentRecord:
        """Find the single intent currently in *state*.

        Raises StateError if none or more than one is found — this
        orchestrator is designed for a single active payment flow.
        """
        matches = [r for r in self._intents.values() if r.state == state]
        if not matches:
            # Build helpful diagnostic: what states do tracked intents have?
            actual = [r.state.name for r in self._intents.values()]
            if actual:
                raise StateError(
                    f"no intent in state {state.name} — "
                    f"tracked intents are in states {actual}"
                )
            raise StateError(
                f"no intent in state {state.name} — cannot proceed"
            )
        return matches[0]

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

    def submit(self, intent: PaymentIntent) -> None:
        """Submit a payment intent.

        - Validates expiry and recipient allowlist.
        - Rejects duplicates (idempotency no-op).
        - Transitions DRAFT → SUBMITTED.
        """
        if self._store.has_intent(intent.id):
            # Idempotent: already seen. Return silently.
            return

        # Validate intent expiry
        rec = IntentRecord(
            intent=intent,
            state=PaymentState.DRAFT,
            expires_at=intent.expires_at,
        )
        self._check_intent_expired(rec)
        self._validate_recipient(intent.recipient)

        # Transition DRAFT → SUBMITTED
        tr = transition(rec.state, "submit")
        if not tr.ok:
            raise StateError(f"cannot submit: {tr.error}")
        rec.state = tr.new_state

        self._intents[intent.id] = rec

        # Record for idempotency ONLY after successful transition + in-memory insert.
        # This ensures idempotency is not recorded if the transition or insert fails.
        self._store.record_intent(intent.id)

        self._audit.append(
            "submit",
            intent_id=intent.id,
            actor=intent.sender.pubkey,
            state=rec.state.value,
        )
        self._save_state()

    def receive_quote(self, quote: PaymentQuote) -> None:
        """Receive a quote from the recipient.

        - Validates the intent exists and the quote references it.
        - Validates quote fee <= max_fee_sat.
        - Validates quote is not expired.
        - Transitions SUBMITTED → QUOTED.
        """
        rec = self._get_record(quote.intent_id)

        if quote.fee_sat > rec.intent.max_fee_sat:
            raise SubmissionRejected(
                f"quote fee {quote.fee_sat} exceeds max_fee_sat {rec.intent.max_fee_sat}"
            )

        # Validate quote expiry against the clock
        now = self.now()
        if now > quote.expires_at:
            raise SubmissionRejected(f"quote {quote.quote_id} has expired")

        rec.quote = quote
        rec.quote_expires_at = quote.expires_at

        tr = transition(rec.state, "quote_received")
        if not tr.ok:
            raise SubmissionRejected(
                f"cannot receive quote: state is {rec.state.value}: {tr.error}"
            )
        rec.state = tr.new_state
        self._audit.append(
            "quote_received",
            intent_id=rec.intent.id,
            quote_id=quote.quote_id,
            fee_sat=quote.fee_sat,
            state=rec.state.value,
        )
        self._save_state()

    def prepare(self, intent_id: Optional[str] = None) -> PrepareResult:
        """Call the adapter's prepare() — non-mutating dry run.

        - Validates the intent is in QUOTED state.
        - Validates the quote is not expired.
        - Validates the adapter rail is allowlisted.
        - Validates the adapter's fee <= max_fee_sat.
        - Transitions QUOTED → PREPARED.
        """
        rec = (
            self._get_record(intent_id)
            if intent_id is not None
            else self._find_by_state(PaymentState.QUOTED)
        )
        if rec.state != PaymentState.QUOTED:
            raise StateError(
                f"cannot prepare intent {rec.intent.id} from state {rec.state.value}"
            )
        self._check_quote_expired(rec)
        self._validate_rail(self._adapter.rail)

        tr = transition(rec.state, "prepared")
        if not tr.ok:
            raise StateError(f"cannot transition to PREPARED: {tr.error}")

        if rec.quote is None:
            raise StateError("prepare requires a quote")

        try:
            prep = self._adapter.prepare(
                receive_instruction=rec.quote.receive_instruction,
                amount_sat=rec.intent.amount_sat,
                max_fee_sat=rec.intent.max_fee_sat,
            )
        except AdapterError as e:
            raise StateError(f"adapter prepare failed: {e}") from e

        if compute_prepared_hash(prep.prepared_payload) != prep.prepared_hash:
            raise StateError("adapter returned a prepared_hash that does not match its payload")

        if prep.fee_sat > rec.intent.max_fee_sat:
            raise StateError(
                f"adapter fee {prep.fee_sat} exceeds max_fee_sat {rec.intent.max_fee_sat}"
            )

        self._validate_rail(prep.rail)

        rec.prepared = prep
        rec.state = tr.new_state
        self._audit.append(
            "prepared",
            intent_id=rec.intent.id,
            prepared_hash=prep.prepared_hash,
            fee_sat=prep.fee_sat,
            state=rec.state.value,
        )
        self._save_state()
        return prep

    def approve(self, approval: PaymentApproval) -> None:
        """Validate and apply human approval.

        - Validates the approval triple (intent_id, quote_id, prepared_hash)
          matches what the orchestrator prepared.
        - Rejects replay: each triple may be approved at most once.
        - Validates the intent is in PREPARED state.
        - Transitions PREPARED → APPROVED.
        """
        rec = self._get_record(approval.intent_id)

        if rec.quote is None:
            raise StateError("cannot approve: no quote exists")
        if rec.quote.quote_id != approval.quote_id:
            raise ApprovalRejected(
                f"approval quote_id {approval.quote_id} does not match "
                f"prepared quote {rec.quote.quote_id}"
            )

        if rec.prepared is None:
            raise StateError("cannot approve: no prepared payload exists")
        if rec.prepared.prepared_hash != approval.prepared_hash:
            raise ApprovalRejected(
                f"approval prepared_hash {approval.prepared_hash} does not match "
                f"prepared_hash {rec.prepared.prepared_hash}"
            )

        # Replay prevention — each triple approved at most once
        triple = (approval.intent_id, approval.quote_id, approval.prepared_hash)
        if self._store.has_approval(triple):
            raise ApprovalRejected(
                "approval triple already used: replay rejected"
            )

        if rec.state != PaymentState.PREPARED:
            raise StateError(
                f"cannot approve: intent is in state {rec.state.name}, expected PREPARED"
            )

        self._store.record_approval(triple)
        rec.approval = approval

        tr = transition(rec.state, "approved")
        if not tr.ok:
            raise StateError(f"cannot transition to APPROVED: {tr.error}")
        rec.state = tr.new_state
        self._audit.append(
            "approved",
            intent_id=rec.intent.id,
            quote_id=approval.quote_id,
            prepared_hash=approval.prepared_hash,
            approver=approval.approver.pubkey,
            state=rec.state.value,
        )
        self._save_state()

    def execute(self, intent_id: Optional[str] = None) -> Optional[PaymentReceipt]:
        """Execute the settlement after approval.

        - Validates the intent is in APPROVED state.
        - Calls adapter.execute() with the prepared payload.
        - On AmbiguousResult / AdapterError during execution →
          RECONCILIATION_REQUIRED (fail-closed, no retry).
        - On success → transitions EXECUTING → SETTLED, records receipt.
        - Returns the PaymentReceipt on success.
        """
        rec = (
            self._get_record(intent_id)
            if intent_id is not None
            else self._find_by_state(PaymentState.APPROVED)
        )
        if rec.state != PaymentState.APPROVED:
            raise StateError(
                f"cannot execute intent {rec.intent.id} from state {rec.state.value}"
            )

        if rec.prepared is None:
            raise StateError("cannot execute: no prepared payload")

        # Move to EXECUTING first
        tr = transition(rec.state, "executing")
        if not tr.ok:
            raise StateError(f"cannot transition to EXECUTING: {tr.error}")
        rec.state = tr.new_state
        self._audit.append(
            "executing",
            intent_id=rec.intent.id,
            state=rec.state.value,
        )
        # Persist EXECUTING before touching the adapter. If the process dies
        # after dispatch, restart remains fail-closed and can reconcile.
        self._save_state()

        now = self.now()
        try:
            exec_result = self._adapter.execute(
                prepared_payload=rec.prepared.prepared_payload,
                prepared_hash=rec.prepared.prepared_hash,
            )
        except AmbiguousResult as e:
            rec.state = PaymentState.RECONCILIATION_REQUIRED
            self._audit.append(
                "adapter_ambiguous",
                intent_id=rec.intent.id,
                error=str(e),
                state=rec.state.value,
            )
            self._save_state()
            raise StateError(
                f"ambiguous settlement result for intent {rec.intent.id}: {e}; "
                f"transitioned to RECONCILIATION_REQUIRED — manual reconciliation required"
            ) from e
        except AdapterError as e:
            rec.state = PaymentState.RECONCILIATION_REQUIRED
            self._audit.append(
                "adapter_error",
                intent_id=rec.intent.id,
                error=str(e),
                state=rec.state.value,
            )
            self._save_state()
            raise StateError(
                f"adapter error during execution for intent {rec.intent.id}: {e}; "
                f"transitioned to RECONCILIATION_REQUIRED — manual reconciliation required"
            ) from e

        # Success — build receipt
        receipt = PaymentReceipt(
            id="placeholder",
            intent_id=rec.intent.id,
            quote_id=rec.quote.quote_id if rec.quote else "",
            recipient=rec.intent.recipient,
            settlement_ref=exec_result.settlement_ref,
            amount_sat=rec.intent.amount_sat,
            fee_sat=exec_result.fee_sat,
            rail=exec_result.rail,
            settled_at=now,
            created_at=now,
        )
        receipt.id = compute_id(receipt)

        self._store.record_receipt(receipt.intent_id, receipt.settlement_ref)
        rec.receipt = receipt

        # Transition EXECUTING → SETTLED
        final = transition(rec.state, "settled")
        if not final.ok:
            raise StateError(f"cannot transition to SETTLED: {final.error}")
        rec.state = final.new_state
        self._audit.append(
            "settled",
            intent_id=rec.intent.id,
            settlement_ref=receipt.settlement_ref,
            amount_sat=receipt.amount_sat,
            state=rec.state.value,
        )
        self._save_state()
        return receipt

    def receive_receipt(self, receipt: PaymentReceipt) -> PaymentReceipt:
        """Receive an externally-produced receipt.

        - Validates the intent exists and the receipt references it.
        - Validates the receipt is only admissible from EXECUTING or
          RECONCILIATION_REQUIRED (fail-closed: forged receipts in any
          other state are rejected *before* any mutation or adapter call).
        - Validates no prior receipt exists (replay prevention).
        - Validates the receipt amount matches the intent.
        - Verifies the receipt against sender-side settlement activity.
        - Transitions to SETTLED via state_machine.transition.
        """
        rec = self._get_record(receipt.intent_id)

        # ── Gate 1: admissible-state check (fail-closed) ───────────────
        # This MUST come before any mutation, adapter call, or mismatch
        # handling.  A forged receipt in DRAFT / SUBMITTED / QUOTED /
        # PREPARED / APPROVED / CANCELLED / REJECTED / FAILED must never
        # be able to force the intent into RECONCILIATION_REQUIRED or
        # SETTLED — that would bypass the state machine.
        if rec.state not in (PaymentState.EXECUTING, PaymentState.RECONCILIATION_REQUIRED):
            raise StateError(
                f"cannot receive receipt in state {rec.state.name}; "
                f"expected EXECUTING or RECONCILIATION_REQUIRED"
            )

        # ── Gate 2: replay prevention ─────────────────────────────────
        if self._store.has_receipt(receipt.intent_id):
            raise StateError(
                f"receipt already recorded for intent {receipt.intent_id}"
            )

        # ── Gate 3: amount / adapter verification ─────────────────────
        # Only reached when we are already in an admissible state.  On
        # mismatch or verification failure we raise without mutating state
        # — the intent stays in EXECUTING or RECONCILIATION_REQUIRED so
        # manual reconciliation can investigate.
        if receipt.amount_sat != rec.intent.amount_sat:
            self._audit.append(
                "receipt_mismatch",
                intent_id=rec.intent.id,
                expected_amount=rec.intent.amount_sat,
                actual_amount=receipt.amount_sat,
                state=rec.state.value,
            )
            raise StateError(
                f"receipt amount {receipt.amount_sat} != intent amount {rec.intent.amount_sat}"
            )

        if rec.quote is None or rec.quote.quote_id != receipt.quote_id:
            raise StateError(
                f"receipt quote_id {receipt.quote_id} does not match "
                f"current quote {rec.quote.quote_id if rec.quote else 'none'}"
            )

        # Verify sender-side settlement activity. Recipient-side verification
        # already happened before the peer published this receipt; calling
        # verify_receipt here would incorrectly query recv on the sender.
        verify = self._adapter.verify_sender_settlement(
            settlement_ref=receipt.settlement_ref,
            expected_amount_sat=receipt.amount_sat,
        )
        if not verify.verified:
            self._audit.append(
                "receipt_verification_failed",
                intent_id=rec.intent.id,
                error=verify.error or "verification failed",
                state=rec.state.value,
            )
            raise StateError(
                f"receipt verification failed: {verify.error or 'unknown'}"
            )

        # ── Transition to SETTLED ─────────────────────────────────────
        tr = transition(rec.state, "receipt_received")
        if not tr.ok:
            raise StateError(
                f"cannot settle via receipt from state {rec.state.name}: {tr.error}"
            )
        # Persist replay protection before mutating in-memory settlement state.
        # If persistence fails, the payment remains EXECUTING/RECONCILIATION_REQUIRED
        # and cannot appear settled without a durable receipt binding.
        self._store.record_receipt(receipt.intent_id, receipt.settlement_ref)
        rec.receipt = receipt
        rec.state = tr.new_state
        self._audit.append(
            "receipt_confirmed",
            intent_id=rec.intent.id,
            settlement_ref=receipt.settlement_ref,
            state=rec.state.value,
        )
        self._save_state()
        return receipt

    def confirm_settled(self, intent_id: str) -> None:
        """Manual reconciliation: confirm settlement from RECONCILIATION_REQUIRED.

        Order: calculate transition → persist idempotency binding → mutate
        state + audit.  This ensures crash between persist and mutate leaves
        the intent still in RECONCILIATION_REQUIRED (safe for retry) and the
        idempotency record prevents a subsequent receive_receipt or
        confirm_settled from double-settling.
        """
        rec = self._get_record(intent_id)
        tr = transition(rec.state, "confirm_settled")
        if not tr.ok:
            raise StateError(
                f"cannot confirm_settled from state {rec.state.value}: {tr.error}"
            )
        # Persist the receipt/idempotency binding BEFORE mutating state so
        # that a crash after persist but before state change leaves the
        # intent in RECONCILIATION_REQUIRED with the binding already
        # recorded — safe for a retry on next process start.
        self._store.record_receipt(intent_id, f"manual:{intent_id}")
        rec.state = tr.new_state
        self._audit.append(
            "confirm_settled",
            intent_id=rec.intent.id,
            state=rec.state.value,
        )
        self._save_state()

    def reconcile_settlement(self, intent_id: str) -> ReconcileResult:
        """Inspect an ambiguous settlement without ever retrying dispatch.

        A ``COMPLETE`` result records sender-side evidence but deliberately
        leaves the intent in ``RECONCILIATION_REQUIRED`` until the recipient's
        independently verified receipt arrives.  ``PENDING`` and ``UNKNOWN``
        are both fail-closed and can be queried again by a bounded supervisor
        loop.
        """
        rec = self._get_record(intent_id)
        if rec.state != PaymentState.RECONCILIATION_REQUIRED:
            raise StateError(
                f"cannot reconcile from state {rec.state.value}; "
                f"expected RECONCILIATION_REQUIRED"
            )
        if rec.prepared is None:
            raise StateError("cannot reconcile: no prepared payload")

        try:
            result = self._adapter.reconcile_settlement(
                prepared_payload=rec.prepared.prepared_payload,
                prepared_hash=rec.prepared.prepared_hash,
                expected_amount_sat=rec.intent.amount_sat,
            )
        except Exception as exc:
            # Recovery must never turn an adapter/query failure into a retry.
            result = ReconcileResult(
                status="UNKNOWN",
                error=f"reconciliation query failed: {redact_sensitive(str(exc))}",
            )

        rec.reconciliation = result
        self._audit.append(
            "settlement_reconciled",
            intent_id=rec.intent.id,
            status=result.status,
            amount_sat=result.amount_sat,
            fee_sat=result.fee_sat,
            state=rec.state.value,
        )
        self._save_state()
        return result

    def cancel(self, intent_id: str) -> None:
        """Cancel an intent from an active state."""
        rec = self._get_record(intent_id)
        tr = transition(rec.state, "cancel")
        if not tr.ok:
            raise StateError(
                f"cannot cancel from state {rec.state.value}: {tr.error}"
            )
        rec.state = tr.new_state
        self._audit.append(
            "cancelled",
            intent_id=rec.intent.id,
            state=rec.state.value,
        )

    def reject(self, intent_id: str) -> None:
        """Reject an intent from an active state."""
        rec = self._get_record(intent_id)
        tr = transition(rec.state, "rejected")
        if not tr.ok:
            raise StateError(
                f"cannot reject from state {rec.state.value}: {tr.error}"
            )
        rec.state = tr.new_state
        self._audit.append(
            "rejected",
            intent_id=rec.intent.id,
            state=rec.state.value,
        )

    # ------------------------------------------------------------------
    # Expiry sweep
    # ------------------------------------------------------------------

    def check_expired(self, *, intents_to_check: Optional[List[str]] = None) -> List[str]:
        """Check EXECUTING intents for expiry.

        For any intent in EXECUTING whose expires_at has passed, this
        transitions it to RECONCILIATION_REQUIRED and raises StateError.
        Returns an empty list when no matching intent is expired.
        """
        now = self.now()
        expired: List[str] = []
        for iid, rec in self._intents.items():
            if intents_to_check is not None and iid not in intents_to_check:
                continue
            if (
                rec.state == PaymentState.EXECUTING
                and rec.expires_at
                and now > rec.expires_at
            ):
                expired.append(iid)
        if expired:
            # Transition each expired EXECUTING intent to RECONCILIATION_REQUIRED
            # BEFORE raising, so state is consistent for manual review.
            for iid in expired:
                rec = self._intents[iid]
                tr = transition(rec.state, "expired")
                if tr.ok:
                    rec.state = tr.new_state
                    self._audit.append(
                        "expired_during_execution",
                        intent_id=iid,
                        state=rec.state.value,
                    )
            raise StateError(
                f"intents expired during execution: {expired}; "
                f"transition to RECONCILIATION_REQUIRED required"
            )
        return expired
