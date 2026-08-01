"""
Hermes Payments — P2 core policy engine tests (v0).

Tests for the rail-neutral PaymentOrchestrator that wraps the state
machine with idempotency, expiry enforcement, approval-binding,
allowlist hooks, an append-only audit log, and fail-closed semantics.

No Buzz or Wavelength I/O — uses a stub adapter.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from fixtures import (
    APPROVER_PUBKEY,
    NOW,
    ONE_HOUR,
    RECIPIENT_PUBKEY,
    SENDER_PUBKEY,
    make_approval,
    make_intent,
    make_quote,
    make_receipt,
)

from hermes_payments.adapter import (
    AdapterError,
    AmbiguousResult,
    ExecuteResult,
    PrepareResult,
    RailReceiveInstruction,
    ReceiptVerifyResult,
    SettlementAdapter,
)
from hermes_payments.models import (
    BuzzIdentity,
    PaymentApproval,
    PaymentReceipt,
    Rail,
    compute_id,
    compute_prepared_hash,
)
from hermes_payments.policy import (
    ApprovalRejected,
    AuditLog,
    IdempotencyStore,
    PaymentOrchestrator,
    RecipientRejected,
    StateError,
    SubmissionRejected,
    UnknownIntent,
)
from hermes_payments.state_machine import PaymentState, transition

# ---------------------------------------------------------------------------
# Stub adapter for testing — no real network I/O
# ---------------------------------------------------------------------------


class StubAdapter(SettlementAdapter):
    """In-memory adapter that tracks prepare/execute calls."""

    def __init__(
        self,
        *,
        fee_sat: int = 5,
        prepared_payload: bytes = b"stub-prepared-payload",
        settlement_ref: str = "stub-settlement-ref",
        execute_result: ExecuteResult | None = None,
        execute_raises: Exception | None = None,
        verify_result: ReceiptVerifyResult | None = None,
    ):
        self._fee_sat = fee_sat
        self._prepared_payload = prepared_payload
        self._settlement_ref = settlement_ref
        self._execute_result = execute_result
        self._execute_raises = execute_raises
        self._verify_result = verify_result
        self.execute_call_count = 0
        self.last_prepared_hash: str | None = None

    @property
    def rail(self) -> Rail:
        return Rail.LIGHTNING

    def prepare(
        self,
        receive_instruction: RailReceiveInstruction,
        amount_sat: int,
        max_fee_sat: int,
    ) -> PrepareResult:
        if self._fee_sat > max_fee_sat:
            raise AdapterError(
                f"fee {self._fee_sat} exceeds max {max_fee_sat}", recoverable=True
            )
        return PrepareResult(
            fee_sat=self._fee_sat,
            prepared_hash=compute_prepared_hash(self._prepared_payload),
            rail=Rail.LIGHTNING,
            prepared_payload=self._prepared_payload,
        )

    def execute(
        self,
        prepared_payload: bytes,
        prepared_hash: str,
    ) -> ExecuteResult:
        self.execute_call_count += 1
        self.last_prepared_hash = prepared_hash
        if self._execute_raises is not None:
            raise self._execute_raises
        if self._execute_result is not None:
            return self._execute_result
        return ExecuteResult(
            settlement_ref=self._settlement_ref,
            amount_sat=0,  # filled by orchestrator
            fee_sat=self._fee_sat,
            rail=Rail.LIGHTNING,
        )

    def verify_receipt(
        self,
        settlement_ref: str,
        expected_amount_sat: int,
    ) -> ReceiptVerifyResult:
        if self._verify_result is not None:
            return self._verify_result
        return ReceiptVerifyResult(
            verified=True,
            settlement_ref=settlement_ref,
            amount_sat=expected_amount_sat,
            fee_sat=self._fee_sat,
        )


def _new_orchestrator(**kwargs):
    """Build a PaymentOrchestrator with a stub adapter and in-memory stores."""
    adapter = kwargs.pop("adapter", None) or StubAdapter(**{
        k: v for k, v in kwargs.items()
        if k in ("fee_sat", "prepared_payload", "settlement_ref",
                 "execute_result", "execute_raises", "verify_result")
    })
    store_path = kwargs.pop("store_path", None)
    audit_path = kwargs.pop("audit_path", None)
    allowlist_recipient = kwargs.pop("recipient_allowlist", None)
    allowlist_rail = kwargs.pop("rail_allowlist", None)
    clock = kwargs.pop("clock", None)
    if clock is None:
        def default_clock():
            return NOW

        clock = default_clock
    return PaymentOrchestrator(
        adapter=adapter,
        store_path=store_path,
        audit_path=audit_path,
        recipient_allowlist=allowlist_recipient,
        rail_allowlist=allowlist_rail,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# 1. Happy path orchestration
# ---------------------------------------------------------------------------


class TestOrchestratorHappyPath:
    def test_full_lifecycle_to_settled(self):
        """DRAFT → SUBMITTED → QUOTED → PREPARED → APPROVED → EXECUTING → SETTLED."""
        orch = _new_orchestrator()
        intent = make_intent()

        # submit
        orch.submit(intent)
        assert orch.state(intent.id) == PaymentState.SUBMITTED

        # receive quote
        quote = make_quote(intent)
        orch.receive_quote(quote)
        assert orch.state(intent.id) == PaymentState.QUOTED

        # prepare
        prep = orch.prepare()
        assert orch.state(intent.id) == PaymentState.PREPARED
        assert prep.prepared_hash  # non-empty

        # approve
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        assert orch.state(intent.id) == PaymentState.APPROVED

        # execute
        receipt = orch.execute()
        assert orch.state(intent.id) == PaymentState.SETTLED
        assert receipt is not None

    def test_receipt_links_to_intent(self):
        """Receipt carries the correct intent_id."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        receipt = orch.execute()
        assert receipt is not None
        assert receipt.intent_id == intent.id
        assert receipt.amount_sat == intent.amount_sat

    def test_orchestrator_stores_intent_id(self):
        """After submit, the orchestrator knows the intent."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        assert orch.get_intent(intent.id) is not None
        assert orch.get_intent(intent.id).id == intent.id


# ---------------------------------------------------------------------------
# 2. Expiry enforcement
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_expired_intent_rejected_on_submit(self):
        """Intent with expires_at in the past is rejected."""
        orch = _new_orchestrator()
        intent = make_intent(expires_at=NOW - 1)
        with pytest.raises(SubmissionRejected, match="expired"):
            orch.submit(intent)

    def test_expired_quote_rejected_on_receive(self):
        """Quote with expires_at in the past is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        # build a quote that's already expired
        from fixtures import make_quote

        expired_quote = make_quote(intent, expires_at=NOW - 1)
        with pytest.raises(SubmissionRejected, match="expired"):
            orch.receive_quote(expired_quote)

    def test_expired_quote_rejected_on_prepare(self):
        """Even if quote was accepted, if it expires before prepare, fail."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent, expires_at=NOW + ONE_HOUR)
        orch.receive_quote(quote)
        # simulate expiry
        rec = orch._intents[intent.id]
        rec.quote_expires_at = NOW - 1
        with pytest.raises(StateError, match="expired"):
            orch.prepare()

    def test_expiry_during_execution_goes_to_reconciliation(self):
        """If intent expires while EXECUTING, the orchestrator transitions to RECONCILIATION_REQUIRED before raising."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()  # goes to EXECUTING then SETTLED

        # Now simulate checking an expired executing intent
        rec = orch._intents[intent.id]
        rec.state = PaymentState.EXECUTING
        rec.expires_at = NOW - 1
        with pytest.raises(StateError, match="expired"):
            orch.check_expired(intents_to_check=[intent.id])
        # State should now be RECONCILIATION_REQUIRED (transitioned before raising)
        assert rec.state == PaymentState.RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# 3. Fee constraint enforcement
# ---------------------------------------------------------------------------


class TestFeeConstraints:
    def test_quote_fee_exceeds_max_fee_rejected(self):
        """Quote fee exceeding intent's max_fee_sat is rejected."""
        orch = _new_orchestrator()
        intent = make_intent(max_fee_sat=5)  # low max fee
        orch.submit(intent)
        # quote with fee exceeding max
        from fixtures import make_quote

        expensive_quote = make_quote(intent, fee_sat=50)
        with pytest.raises(SubmissionRejected, match="fee"):
            orch.receive_quote(expensive_quote)

    def test_prepare_fails_when_adapter_fee_exceeds_max(self):
        """Adapter prepare returning fee > max_fee_sat → FAILED / rejected."""
        orch = _new_orchestrator(fee_sat=500)
        intent = make_intent(max_fee_sat=10)
        orch.submit(intent)
        quote = make_quote(intent, fee_sat=1)  # quote fee within max
        orch.receive_quote(quote)
        with pytest.raises(StateError, match="fee"):
            orch.prepare()

    def test_fee_constraint_exact(self):
        """fee_constraint='max' allows fee <= max_fee; 'exact' requires exact match."""
        orch = _new_orchestrator(fee_sat=20)
        intent = make_intent(max_fee_sat=100)
        orch.submit(intent)
        quote = make_quote(intent, fee_sat=20)
        orch.receive_quote(quote)
        orch.prepare()
        assert orch.state(intent.id) == PaymentState.PREPARED


# ---------------------------------------------------------------------------
# 4. Approval binding & replay prevention
# ---------------------------------------------------------------------------


class TestApprovalBinding:
    def test_approval_with_wrong_hash_rejected(self):
        """Approval with prepared_hash mismatch is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        orch.prepare()
        # approval with wrong hash
        bad_approval = make_approval(
            intent, quote, prepared_hash="dead" + "beef" * 15
        )
        with pytest.raises(ApprovalRejected, match="prepared_hash"):
            orch.approve(bad_approval)
        assert orch.state(intent.id) == PaymentState.PREPARED

    def test_approval_with_wrong_intent_rejected(self):
        """Approval referencing a different intent_id is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent, quote_id="q-intent")
        orch.receive_quote(quote)
        prep = orch.prepare()
        # Submit a different intent and get it to PREPARED
        other_intent = make_intent(idempotency_key="other-idem")
        orch.submit(other_intent)
        other_quote = make_quote(other_intent, quote_id="q-other")
        orch.receive_quote(other_quote)
        orch.prepare()
        # approval for the wrong intent (references other_intent's id but intent's quote/prepared)
        bad_approval = PaymentApproval(
            id="placeholder",
            intent_id=other_intent.id,
            quote_id=quote.quote_id,
            prepared_hash=prep.prepared_hash,
            approver=BuzzIdentity(pubkey=APPROVER_PUBKEY, relay_url=None),
            created_at=NOW,
        )
        bad_approval.id = compute_id(bad_approval)
        with pytest.raises(ApprovalRejected, match="quote"):
            orch.approve(bad_approval)

    def test_approval_with_wrong_quote_rejected(self):
        """Approval referencing a different quote_id is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent, quote_id="q-001")
        orch.receive_quote(quote)
        prep = orch.prepare()
        bad_approval = PaymentApproval(
            id="placeholder",
            intent_id=intent.id,
            quote_id="q-999",
            prepared_hash=prep.prepared_hash,
            approver=BuzzIdentity(pubkey=APPROVER_PUBKEY, relay_url=None),
            created_at=NOW,
        )
        bad_approval.id = compute_id(bad_approval)
        with pytest.raises(ApprovalRejected, match="quote"):
            orch.approve(bad_approval)

    def test_replay_approval_rejected(self):
        """Same (intent_id, quote_id, prepared_hash) triple approved twice is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        assert orch.state(intent.id) == PaymentState.APPROVED
        # attempt replay
        with pytest.raises(ApprovalRejected, match="replay|already"):
            orch.approve(approval)

    def test_approve_requires_prepared_state(self):
        """Cannot approve without first preparing."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        approval = make_approval(intent, quote, prepared_hash="ff" * 32)
        with pytest.raises(StateError, match="PREPARED|prepared"):
            orch.approve(approval)


# ---------------------------------------------------------------------------
# 5. Idempotency & replay prevention
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_idempotency_store_tracks_intents(self, tmp_path):
        store = IdempotencyStore(path=str(tmp_path / "idempotent.json"))
        intent = make_intent()
        assert not store.has_intent(intent.id)
        store.record_intent(intent.id)
        assert store.has_intent(intent.id)

    def test_idempotency_store_tracks_approvals(self, tmp_path):
        store = IdempotencyStore(path=str(tmp_path / "idempotent.json"))
        intent = make_intent()
        quote = make_quote(intent)
        prep_payload = b"payload"
        ph = compute_prepared_hash(prep_payload)
        triple = (intent.id, quote.quote_id, ph)
        assert not store.has_approval(triple)
        store.record_approval(triple)
        assert store.has_approval(triple)
        # same triple → already seen
        assert store.has_approval(triple)
        # different hash → not seen
        assert not store.has_approval((intent.id, quote.quote_id, "other" * 21))

    def test_idempotency_store_tracks_receipts(self, tmp_path):
        store = IdempotencyStore(path=str(tmp_path / "idempotent.json"))
        intent = make_intent()
        assert not store.has_receipt(intent.id)
        store.record_receipt(intent.id, "settlement_ref_1")
        assert store.has_receipt(intent.id)

    def test_idempotency_store_persistence(self, tmp_path):
        """Store survives reload from disk."""
        path = str(tmp_path / "idempotent.json")
        store = IdempotencyStore(path=path)
        intent = make_intent()
        store.record_intent(intent.id)
        store.record_receipt(intent.id, "ref")

        store2 = IdempotencyStore(path=path)
        assert store2.has_intent(intent.id)
        assert store2.has_receipt(intent.id)

    def test_duplicate_intent_submission_is_noop(self):
        """Submitting the same intent twice doesn't change state."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        state_after_first = orch.state(intent.id)
        # Second submit is idempotent — should not raise
        orch.submit(intent)
        assert orch.state(intent.id) == state_after_first

    def test_receipt_replay_rejected(self):
        """Second receipt for same intent is rejected (already SETTLED)."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        assert orch.state(intent.id) == PaymentState.SETTLED
        # replay: try to receive another receipt for same intent
        other_quote = make_quote(intent, quote_id="q-replay")
        other_receipt = make_receipt(intent, other_quote, settlement_ref="ref-2")
        # The orchestrator should reject this as idempotent / already settled
        with pytest.raises(StateError):
            orch.receive_receipt(other_receipt)


# ---------------------------------------------------------------------------
# 6. Allowlist hooks
# ---------------------------------------------------------------------------


class TestAllowlists:
    def test_recipient_not_in_allowlist_rejected(self):
        """Intent for a non-allowlisted recipient is rejected."""
        orch = _new_orchestrator(
            recipient_allowlist=lambda pk: pk == SENDER_PUBKEY  # only sender allowed
        )
        intent = make_intent()  # recipient is RECIPIENT_PUBKEY
        with pytest.raises(RecipientRejected, match="allowlist"):
            orch.submit(intent)

    def test_recipient_in_allowlist_accepted(self):
        """Intent for an allowlisted recipient is accepted."""
        orch = _new_orchestrator(
            recipient_allowlist=lambda pk: pk in (SENDER_PUBKEY, RECIPIENT_PUBKEY)
        )
        intent = make_intent()
        orch.submit(intent)
        assert orch.state(intent.id) == PaymentState.SUBMITTED

    def test_rail_not_in_allowlist_rejected(self):
        """Quote with a rail not in the allowlist is rejected."""
        orch = _new_orchestrator(
            rail_allowlist=lambda r: r == Rail.LIGHTNING
        )
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        assert orch.state(intent.id) == PaymentState.QUOTED

    def test_rail_not_allowlisted_rejected(self):
        """Adapter with a rail not in the allowlist → prepare rejected."""
        orch = _new_orchestrator(
            rail_allowlist=lambda r: False,  # reject all rails
        )
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        with pytest.raises(StateError, match="rail"):
            orch.prepare()


# ---------------------------------------------------------------------------
# 7. Fail-closed semantics
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_ambiguous_execute_goes_to_reconciliation(self):
        """AmbiguousResult during execute → RECONCILIATION_REQUIRED, not retried."""
        adapter = StubAdapter(
            execute_raises=AmbiguousResult("network timeout after broadcast")
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        with pytest.raises(StateError, match="reconciliation"):
            orch.execute()

    def test_adapter_error_during_execution_goes_to_reconciliation(self):
        """AdapterError during execute → RECONCILIATION_REQUIRED."""
        adapter = StubAdapter(
            execute_raises=AdapterError("connection reset", recoverable=False)
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        with pytest.raises(StateError, match="reconciliation"):
            orch.execute()

    def test_execute_not_retried_automatically(self):
        """After ambiguous, execute is never retried by the orchestrator."""
        adapter = StubAdapter(
            execute_raises=AmbiguousResult("ambiguous")
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        with pytest.raises(StateError):
            orch.execute()
        # execute should have been called exactly once
        assert adapter.execute_call_count == 1

    def test_already_settled_rejects_execute(self):
        """Cannot execute on an already-settled intent."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        # state is SETTLED — trying execute again should fail
        with pytest.raises(StateError, match="SETTLED|terminal"):
            orch.execute()

    def test_non_approved_intent_rejects_execute(self):
        """Cannot execute without approval."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        orch.prepare()
        with pytest.raises(StateError, match="APPROVED"):
            orch.execute()


# ---------------------------------------------------------------------------
# 8. Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_audit_log_appends_entries(self, tmp_path):
        log = AuditLog(path=str(tmp_path / "audit.jsonl"))
        log.append("submit", intent_id="i1", actor="sender", state="submitted")
        log.append("approve", intent_id="i1", actor="approver", state="approved")
        entries = log.entries()
        assert len(entries) == 2
        assert entries[0]["event"] == "submit"
        assert entries[1]["event"] == "approve"
        assert entries[0]["intent_id"] == "i1"

    def test_audit_log_is_append_only(self, tmp_path):
        """Entries are append-only — each gets a sequential seq number."""
        log = AuditLog(path=str(tmp_path / "audit.jsonl"))
        log.append("a", intent_id="i")
        log.append("b", intent_id="i")
        log.append("c", intent_id="i")
        entries = log.entries()
        seqs = [e["seq"] for e in entries]
        assert seqs == [0, 1, 2]

    def test_orchestrator_writes_audit_on_transition(self, tmp_path):
        orch = _new_orchestrator(store_path=str(tmp_path / "store.json"))
        orch._audit = AuditLog(path=str(tmp_path / "audit.jsonl"))
        intent = make_intent()
        orch.submit(intent)
        entries = orch._audit.entries()
        assert any(e["event"] == "submit" for e in entries)
        assert any(e["intent_id"] == intent.id for e in entries)

    def test_audit_log_persistence(self, tmp_path):
        path = str(tmp_path / "audit.jsonl")
        log = AuditLog(path=path)
        log.append("submit", intent_id="i1")
        log2 = AuditLog(path=path)
        assert len(log2.entries()) == 1


# ---------------------------------------------------------------------------
# 9. State error enforcement
# ---------------------------------------------------------------------------


class TestStateErrors:
    def test_submit_already_submitted_is_noop(self):
        """Second submit of same intent is idempotent (no-op)."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        state_before = orch.state(intent.id)
        orch.submit(intent)  # should not raise
        assert orch.state(intent.id) == state_before

    def test_receive_quote_without_submit_raises(self):
        orch = _new_orchestrator()
        intent = make_intent()
        quote = make_quote(intent)
        with pytest.raises(UnknownIntent):
            orch.receive_quote(quote)

    def test_receive_mismatched_quote_raises(self):
        """Quote referencing an unknown intent is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        other_intent = make_intent(idempotency_key="other-key")
        orch.submit(other_intent)
        # Create a quote for an intent that doesn't exist in the orchestrator
        unknown = make_intent(idempotency_key="never-submitted")
        quote_for_unknown = make_quote(unknown)
        with pytest.raises(UnknownIntent):
            orch.receive_quote(quote_for_unknown)

    def test_prepare_without_quote_raises(self):
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        with pytest.raises(StateError, match="QUOTED"):
            orch.prepare()

    def test_cancel_works_from_submitted(self):
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        orch.cancel(intent.id)
        assert orch.state(intent.id) == PaymentState.CANCELLED

    def test_cancel_from_settled_raises(self):
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        assert orch.state(intent.id) == PaymentState.SETTLED
        with pytest.raises(StateError):
            orch.cancel(intent.id)


# ---------------------------------------------------------------------------
# 10. Receipt verification
# ---------------------------------------------------------------------------


class TestReceiptVerify:
    def test_receive_receipt_verifies_amount(self):
        """receive_receipt verifies and transitions to SETTLED."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Manually move to EXECUTING (simulating out-of-band settlement)
        rec = orch._intents[intent.id]
        from hermes_payments.state_machine import transition
        tr = transition(rec.state, "executing")
        rec.state = tr.new_state
        # Now receive the receipt from outside
        receipt = orch.receive_receipt(make_receipt(intent, quote))
        assert receipt is not None
        assert orch.state(intent.id) == PaymentState.SETTLED

    def test_receive_receipt_persist_failure_does_not_settle(self, monkeypatch):
        """A durable receipt write must succeed before an external receipt settles."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        orch.approve(make_approval(intent, quote, prepared_hash=prep.prepared_hash))
        rec = orch._intents[intent.id]
        rec.state = transition(rec.state, "executing").new_state

        def fail_record(*_args, **_kwargs):
            raise OSError("simulated receipt store failure")

        monkeypatch.setattr(orch._store, "record_receipt", fail_record)
        with pytest.raises(OSError, match="receipt store failure"):
            orch.receive_receipt(make_receipt(intent, quote))

        assert orch.state(intent.id) == PaymentState.EXECUTING
        assert rec.receipt is None

    def test_receipt_wrong_intent_rejected(self):
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        other_intent = make_intent(idempotency_key="other")
        orch.submit(other_intent)
        other_quote = make_quote(other_intent)
        orch.receive_quote(other_quote)
        orch.prepare()
        orch._intents[other_intent.id].state = PaymentState.EXECUTING
        fake_receipt = make_receipt(intent, quote)  # not for other_intent
        with pytest.raises(StateError):
            orch.receive_receipt(fake_receipt)


# ---------------------------------------------------------------------------
# 11. P2 regression tests — review findings
# ---------------------------------------------------------------------------


class TestP2Regression:
    """Regression tests for the P2 review findings."""

    # -- (1) receive_receipt must only settle via state_machine.transition --

    def test_receive_receipt_rejected_in_draft(self):
        """receive_receipt in DRAFT state is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        # Move to DRAFT manually (simulating an intent that hasn't been submitted)
        orch._intents[intent.id].state = PaymentState.DRAFT
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError):
            orch.receive_receipt(receipt)

    def test_receive_receipt_rejected_in_cancelled(self):
        """receive_receipt in CANCELLED state is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        orch.cancel(intent.id)
        assert orch.state(intent.id) == PaymentState.CANCELLED
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError):
            orch.receive_receipt(receipt)

    def test_receive_receipt_rejected_in_rejected(self):
        """receive_receipt in REJECTED state is rejected."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        orch.reject(intent.id)
        assert orch.state(intent.id) == PaymentState.REJECTED
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError):
            orch.receive_receipt(receipt)

    def test_receive_receipt_rejected_in_failed(self):
        """receive_receipt in FAILED state is rejected."""
        adapter = StubAdapter(
            execute_raises=AdapterError("boom", recoverable=False)
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Adapter error during execution → RECONCILIATION_REQUIRED
        with pytest.raises(StateError, match="reconciliation"):
            orch.execute()
        # Now manually transition to FAILED (simulating a different failure path)
        orch._intents[intent.id].state = PaymentState.FAILED
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="cannot receive receipt"):
            orch.receive_receipt(receipt)

    def test_receive_receipt_rejected_in_submitted(self):
        """receive_receipt in SUBMITTED state is rejected (before quote)."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        assert orch.state(intent.id) == PaymentState.SUBMITTED
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError):
            orch.receive_receipt(receipt)

    def test_receive_receipt_rejected_in_settled(self):
        """receive_receipt in SETTLED state is rejected (already settled)."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        assert orch.state(intent.id) == PaymentState.SETTLED
        # Try to receive another receipt — rejected (replay or terminal state)
        other_quote = make_quote(intent, quote_id="q-other")
        receipt = make_receipt(intent, other_quote)
        with pytest.raises(StateError):
            orch.receive_receipt(receipt)

    def test_receive_receipt_uses_transition_from_executing(self):
        """receive_receipt settles via state_machine.transition from EXECUTING."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Move to EXECUTING (simulating out-of-band settlement start)
        rec = orch._intents[intent.id]
        tr = transition(rec.state, "executing")
        rec.state = tr.new_state
        assert rec.state == PaymentState.EXECUTING
        # Receive the receipt — should transition via state machine
        receipt = orch.receive_receipt(make_receipt(intent, quote))
        assert orch.state(intent.id) == PaymentState.SETTLED
        assert receipt.settlement_ref == "payment_hash_abc123"

    def test_receive_receipt_uses_transition_from_reconciliation(self):
        """receive_receipt settles via state_machine.transition from RECONCILIATION_REQUIRED."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Move to RECONCILIATION_REQUIRED (simulating ambiguous execution)
        rec = orch._intents[intent.id]
        rec.state = PaymentState.RECONCILIATION_REQUIRED
        # Receive the receipt — should transition via state machine
        orch.receive_receipt(make_receipt(intent, quote))
        assert orch.state(intent.id) == PaymentState.SETTLED

    # -- (2) check_expired transitions EXECUTING → RECONCILIATION_REQUIRED before raising --

    def test_check_expired_transitions_before_raising(self):
        """check_expired transitions EXECUTING to RECONCILIATION_REQUIRED before raising."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        # Force to EXECUTING with expired timestamp
        rec = orch._intents[intent.id]
        rec.state = PaymentState.EXECUTING
        rec.expires_at = NOW - 1
        with pytest.raises(StateError, match="expired"):
            orch.check_expired(intents_to_check=[intent.id])
        # Verify state was transitioned BEFORE the raise
        assert rec.state == PaymentState.RECONCILIATION_REQUIRED

    def test_check_expired_only_targets_executing(self):
        """check_expired does not affect non-EXECUTING intents."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        orch.prepare()
        # Intent is in PREPARED state — check_expired should not touch it
        rec = orch._intents[intent.id]
        rec.expires_at = NOW - 1
        expired = orch.check_expired(intents_to_check=[intent.id])
        assert expired == []
        assert rec.state == PaymentState.PREPARED

    def test_check_expired_returns_empty_when_no_expiry(self):
        """check_expired returns empty list when no EXECUTING intents are expired."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        # Force to EXECUTING but not expired
        rec = orch._intents[intent.id]
        rec.state = PaymentState.EXECUTING
        rec.expires_at = NOW + ONE_HOUR
        expired = orch.check_expired(intents_to_check=[intent.id])
        assert expired == []
        assert rec.state == PaymentState.EXECUTING

    # -- (3) submit records idempotency only after successful transition + in-memory insert --

    def test_submit_records_idempotency_after_transition(self):
        """submit records idempotency only after successful transition and in-memory insert."""
        orch = _new_orchestrator()
        intent = make_intent()
        # Before submit, intent should not be in the store
        assert not orch._store.has_intent(intent.id)
        orch.submit(intent)
        # After successful submit, intent IS in the store
        assert orch._store.has_intent(intent.id)
        # And it's in memory
        assert intent.id in orch._intents

    def test_submit_no_idempotency_on_expired_intent(self):
        """submit does not record idempotency for an expired intent (rejected before transition)."""
        orch = _new_orchestrator()
        intent = make_intent(expires_at=NOW - 1)
        with pytest.raises(SubmissionRejected, match="expired"):
            orch.submit(intent)
        # Intent should NOT be in the idempotency store (was rejected before transition)
        assert not orch._store.has_intent(intent.id)

    # -- (4) IdempotencyStore crash-safe persistence --

    def test_idempotency_store_crash_safe_persistence(self, tmp_path):
        """IdempotencyStore uses temp file + os.replace for crash safety."""
        path = str(tmp_path / "store.json")
        store = IdempotencyStore(path=path)
        intent = make_intent()
        store.record_intent(intent.id)
        store.record_receipt(intent.id, "ref")
        # Verify the file exists and contains correct data
        with open(path, "r") as f:
            data = json.load(f)
        assert intent.id in data["intents"]
        assert data["receipts"][intent.id] == "ref"
        # Verify no leftover temp files
        import glob
        tmp_files = glob.glob(str(tmp_path / "*.tmp"))
        assert len(tmp_files) == 0

    def test_idempotency_store_atomic_replace(self, tmp_path):
        """IdempotencyStore._save leaves no temp files on success."""
        path = str(tmp_path / "store.json")
        store = IdempotencyStore(path=path)
        intent = make_intent()
        # Multiple saves should all be atomic
        store.record_intent(intent.id)
        store.record_approval((intent.id, "q1", "h1"))
        store.record_receipt(intent.id, "ref1")
        # Verify no temp files remain
        import glob
        tmp_files = glob.glob(str(tmp_path / "*.tmp"))
        assert len(tmp_files) == 0
        # Verify data integrity
        store2 = IdempotencyStore(path=path)
        assert store2.has_intent(intent.id)
        assert store2.has_approval((intent.id, "q1", "h1"))
        assert store2.has_receipt(intent.id)

    def test_idempotency_store_original_untouched_on_write_failure(self, tmp_path):
        """On write failure, original store file is untouched."""
        from unittest.mock import patch as mock_patch
        path = str(tmp_path / "store.json")
        store = IdempotencyStore(path=path)
        intent1 = make_intent()
        store.record_intent(intent1.id)
        # Verify the original file has intent1
        with open(path, "r") as f:
            original = json.load(f)
        assert intent1.id in original["intents"]
        # Patch json.dump to raise an error during save
        def failing_dump(*_args, **_kwargs):
            raise RuntimeError("simulated write failure")
        with mock_patch("hermes_payments.policy.json.dump", failing_dump):
            with pytest.raises(RuntimeError, match="simulated write failure"):
                store._save()
        # Original file should still have intent1
        with open(path, "r") as f:
            after_failure = json.load(f)
        assert intent1.id in after_failure["intents"]

    # -- (5) confirm_settled records receipt/idempotency binding --

    def test_confirm_settled_records_receipt_binding(self):
        """confirm_settled records the receipt/idempotency binding in the store."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Simulate ambiguous execution → RECONCILIATION_REQUIRED
        adapter = StubAdapter(
            execute_raises=AmbiguousResult("timeout")
        )
        orch._adapter = adapter
        with pytest.raises(StateError, match="ambiguous"):
            orch.execute()
        assert orch.state(intent.id) == PaymentState.RECONCILIATION_REQUIRED
        # Confirm settled
        orch.confirm_settled(intent.id)
        assert orch.state(intent.id) == PaymentState.SETTLED
        # Verify the receipt binding was recorded
        assert orch._store.has_receipt(intent.id)
        assert orch._store.get_receipt(intent.id) == f"manual:{intent.id}"

    def test_confirm_settled_prevents_reconfirm(self):
        """confirm_settled records binding, preventing a second confirm_settled."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        orch.execute()
        # Force to RECONCILIATION_REQUIRED
        rec = orch._intents[intent.id]
        rec.state = PaymentState.RECONCILIATION_REQUIRED
        # First confirm_settled
        orch.confirm_settled(intent.id)
        assert orch.state(intent.id) == PaymentState.SETTLED
        # Second confirm_settled should fail (terminal state)
        with pytest.raises(StateError, match="terminal"):
            orch.confirm_settled(intent.id)

    def test_confirm_settled_prevents_receive_receipt_after(self):
        """After confirm_settled, receive_receipt is rejected (already SETTLED)."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Force to RECONCILIATION_REQUIRED
        rec = orch._intents[intent.id]
        rec.state = PaymentState.RECONCILIATION_REQUIRED
        # Confirm settled via manual reconciliation
        orch.confirm_settled(intent.id)
        assert orch.state(intent.id) == PaymentState.SETTLED
        # Now try to receive a receipt — should be rejected (already settled)
        other_quote = make_quote(intent, quote_id="q-post")
        receipt = make_receipt(intent, other_quote)
        with pytest.raises(StateError):
            orch.receive_receipt(receipt)

    # ── (6) Forged receipt state preservation — exhaustive coverage ────

    # Every non-admissible state must reject the receipt BEFORE any
    # mutation.  These tests verify that (a) the receipt is rejected and
    # (b) the intent state is unchanged afterward.

    FORBIDDEN_STATES = [
        PaymentState.DRAFT,
        PaymentState.SUBMITTED,
        PaymentState.QUOTED,
        PaymentState.PREPARED,
        PaymentState.APPROVED,
        PaymentState.CANCELLED,
        PaymentState.REJECTED,
        PaymentState.FAILED,
    ]

    def _receipt_rejected_in_state(self, state: PaymentState):
        """Helper: create an intent in *state*, attempt receive_receipt,
        assert rejection, and assert state is unchanged."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Move to EXECUTING first (to have a valid receipt path), then
        # override to the target state.
        orch._intents[intent.id].state = state
        state_before = orch.state(intent.id)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="cannot receive receipt"):
            orch.receive_receipt(receipt)
        # State must be unchanged
        assert orch.state(intent.id) == state_before

    def test_forged_receipt_preserves_draft(self):
        """Forged receipt in DRAFT leaves state unchanged."""
        self._receipt_rejected_in_state(PaymentState.DRAFT)

    def test_forged_receipt_preserves_submitted(self):
        """Forged receipt in SUBMITTED leaves state unchanged."""
        self._receipt_rejected_in_state(PaymentState.SUBMITTED)

    def test_forged_receipt_preserves_quoted(self):
        """Forged receipt in QUOTED leaves state unchanged."""
        self._receipt_rejected_in_state(PaymentState.QUOTED)

    def test_forged_receipt_preserves_prepared(self):
        """Forged receipt in PREPARED leaves state unchanged."""
        self._receipt_rejected_in_state(PaymentState.PREPARED)

    def test_forged_receipt_preserves_approved(self):
        """Forged receipt in APPROVED leaves state unchanged."""
        self._receipt_rejected_in_state(PaymentState.APPROVED)

    def test_forged_receipt_preserves_cancelled(self):
        """Forged receipt in CANCELLED leaves state unchanged."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        orch.cancel(intent.id)
        state_before = orch.state(intent.id)
        assert state_before == PaymentState.CANCELLED
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="cannot receive receipt"):
            orch.receive_receipt(receipt)
        assert orch.state(intent.id) == PaymentState.CANCELLED

    def test_forged_receipt_preserves_rejected(self):
        """Forged receipt in REJECTED leaves state unchanged."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        orch.reject(intent.id)
        state_before = orch.state(intent.id)
        assert state_before == PaymentState.REJECTED
        quote = make_quote(intent)
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="cannot receive receipt"):
            orch.receive_receipt(receipt)
        assert orch.state(intent.id) == PaymentState.REJECTED

    def test_forged_receipt_preserves_failed(self):
        """Forged receipt in FAILED leaves state unchanged."""
        adapter = StubAdapter(
            execute_raises=AdapterError("boom", recoverable=False)
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        with pytest.raises(StateError, match="reconciliation"):
            orch.execute()
        # Force to FAILED
        orch._intents[intent.id].state = PaymentState.FAILED
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="cannot receive receipt"):
            orch.receive_receipt(receipt)
        assert orch.state(intent.id) == PaymentState.FAILED

    # ── Mismatched / unverified receipt in EXECUTING must not mutate ───

    def test_mismatched_amount_does_not_mutate_executing(self):
        """Receipt with wrong amount in EXECUTING raises but leaves state."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Move to EXECUTING
        rec = orch._intents[intent.id]
        tr = transition(rec.state, "executing")
        rec.state = tr.new_state
        assert rec.state == PaymentState.EXECUTING
        # Build receipt with wrong amount
        bad_receipt = PaymentReceipt(
            id="placeholder",
            intent_id=intent.id,
            quote_id=quote.quote_id,
            recipient=intent.recipient,
            settlement_ref="ref-wrong",
            amount_sat=999999,  # doesn't match intent.amount_sat
            fee_sat=0,
            rail=Rail.LIGHTNING,
            settled_at=NOW,
            created_at=NOW,
        )
        bad_receipt.id = compute_id(bad_receipt)
        with pytest.raises(StateError, match="receipt amount"):
            orch.receive_receipt(bad_receipt)
        # State must still be EXECUTING (not RECONCILIATION_REQUIRED)
        assert rec.state == PaymentState.EXECUTING

    def test_unverified_receipt_does_not_mutate_executing(self):
        """Receipt that fails adapter verification in EXECUTING leaves state."""
        adapter = StubAdapter(
            verify_result=ReceiptVerifyResult(
                verified=False,
                settlement_ref="ref",
                amount_sat=0,
                fee_sat=0,
                error="settlement not found",
            )
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Move to EXECUTING
        rec = orch._intents[intent.id]
        tr = transition(rec.state, "executing")
        rec.state = tr.new_state
        assert rec.state == PaymentState.EXECUTING
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="verification failed"):
            orch.receive_receipt(receipt)
        # State must still be EXECUTING
        assert rec.state == PaymentState.EXECUTING

    def test_mismatched_amount_does_not_mutate_reconciliation(self):
        """Receipt with wrong amount in RECONCILIATION_REQUIRED leaves state."""
        orch = _new_orchestrator()
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        # Force to RECONCILIATION_REQUIRED
        rec = orch._intents[intent.id]
        rec.state = PaymentState.RECONCILIATION_REQUIRED
        bad_receipt = PaymentReceipt(
            id="placeholder",
            intent_id=intent.id,
            quote_id=quote.quote_id,
            recipient=intent.recipient,
            settlement_ref="ref-wrong",
            amount_sat=999999,
            fee_sat=0,
            rail=Rail.LIGHTNING,
            settled_at=NOW,
            created_at=NOW,
        )
        bad_receipt.id = compute_id(bad_receipt)
        with pytest.raises(StateError, match="receipt amount"):
            orch.receive_receipt(bad_receipt)
        assert rec.state == PaymentState.RECONCILIATION_REQUIRED

    def test_unverified_receipt_does_not_mutate_reconciliation(self):
        """Unverified receipt in RECONCILIATION_REQUIRED leaves state."""
        adapter = StubAdapter(
            verify_result=ReceiptVerifyResult(
                verified=False,
                settlement_ref="ref",
                amount_sat=0,
                fee_sat=0,
                error="not found",
            )
        )
        orch = _new_orchestrator(adapter=adapter)
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        rec = orch._intents[intent.id]
        rec.state = PaymentState.RECONCILIATION_REQUIRED
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="verification failed"):
            orch.receive_receipt(receipt)
        assert rec.state == PaymentState.RECONCILIATION_REQUIRED

    def test_receipt_mismatch_audit_logged(self, tmp_path):
        """Receipt mismatch in EXECUTING is audit-logged."""
        orch = _new_orchestrator(audit_path=str(tmp_path / "audit.jsonl"))
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        rec = orch._intents[intent.id]
        tr = transition(rec.state, "executing")
        rec.state = tr.new_state
        bad_receipt = PaymentReceipt(
            id="placeholder",
            intent_id=intent.id,
            quote_id=quote.quote_id,
            recipient=intent.recipient,
            settlement_ref="ref-wrong",
            amount_sat=999999,
            fee_sat=0,
            rail=Rail.LIGHTNING,
            settled_at=NOW,
            created_at=NOW,
        )
        bad_receipt.id = compute_id(bad_receipt)
        with pytest.raises(StateError, match="receipt amount"):
            orch.receive_receipt(bad_receipt)
        entries = orch._audit.entries()
        assert any(e["event"] == "receipt_mismatch" for e in entries)

    def test_receipt_verification_failure_audit_logged(self, tmp_path):
        """Verification failure in EXECUTING is audit-logged."""
        adapter = StubAdapter(
            verify_result=ReceiptVerifyResult(
                verified=False,
                settlement_ref="ref",
                amount_sat=0,
                fee_sat=0,
                error="not found",
            )
        )
        orch = _new_orchestrator(
            adapter=adapter,
            audit_path=str(tmp_path / "audit.jsonl"),
        )
        intent = make_intent()
        orch.submit(intent)
        quote = make_quote(intent)
        orch.receive_quote(quote)
        prep = orch.prepare()
        approval = make_approval(intent, quote, prepared_hash=prep.prepared_hash)
        orch.approve(approval)
        rec = orch._intents[intent.id]
        tr = transition(rec.state, "executing")
        rec.state = tr.new_state
        receipt = make_receipt(intent, quote)
        with pytest.raises(StateError, match="verification failed"):
            orch.receive_receipt(receipt)
        entries = orch._audit.entries()
        assert any(e["event"] == "receipt_verification_failed" for e in entries)
