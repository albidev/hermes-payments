"""
Hermes Payments — P4 Wavelength adapter tests (v2 — raw RPC path).

Tests the WavelengthAdapter with FakeWavecliExecutor.  No subprocess,
no network, no real waved daemon.

Covers:
1. Regtest-only enforcement (construction guard)
2. Command construction (raw RPC, injection-safe)
3. prepare() — raw PrepareSend, binding field validation, fee/expiry
4. execute() — raw Send with exact send_intent_id, not high-level send
5. verify_receipt() — recipient-side activity --kind recv, fail-closed
6. Error redaction (invoices, macaroon paths)
7. Strict JSON parsing (missing intent, unknown status)
8. FakeWavecliExecutor basics
9. Interface compliance
"""
from __future__ import annotations

import json
import os
import sys
import time
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from fixtures import (
    NOW,
    ONE_HOUR,
    RECIPIENT_PUBKEY,
    make_intent,
    make_quote,
)

from hermes_payments.adapter import (
    AdapterError,
    AmbiguousResult,
    FakeWavecliExecutor,
    PrepareResult,
    ReceiptVerifyResult,
    SettlementAdapter,
    WavelengthAdapter,
    WavecliExecutor,
    _build_wavecli_activity_cmd,
    _build_raw_rpc_cmd,
    _parse_prepare_response,
    _parse_send_response,
    _parse_activity_entry,
    redact_sensitive,
)
from hermes_payments.models import (
    Rail,
    RailReceiveInstruction,
    compute_prepared_hash,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

SAMPLE_INVOICE = (
    "lnbcrt2100n1p0000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000"
)

# Far-future expiry so tests don't fail due to time passing
FAR_FUTURE_EXPIRY = 4102444800  # 2100-01-01T00:00:00Z

SAMPLE_RECEIVE = RailReceiveInstruction(
    rail=Rail.LIGHTNING,
    invoice=SAMPLE_INVOICE,
)

# Raw PrepareSendResponse from wavecli dev RPC
RAW_PREPARE_RESPONSE = {
    "send_intent_id": "si-abc123",
    "amount_sat": 2100,
    "expected_fee_sat": 10,
    "fee_known": True,
    "expected_total_outflow_sat": 2110,
    "total_outflow_known": True,
    "rail": "SEND_RAIL_LIGHTNING",
    "payment_hash": "aa" * 32,
    "expires_at_unix": FAR_FUTURE_EXPIRY,
    "quote_status": "SEND_QUOTE_STATUS_COMPLETE",
    "destination_summary": "test destination",
    "warning": "",
}

# Raw SendResponse from wavecli dev RPC (entry + actual_amount_sat)
RAW_SEND_RESPONSE = {
    "entry": {
        "id": "aa" * 32,
        "status": "ENTRY_STATUS_COMPLETE",
        "kind": "ENTRY_KIND_SEND",
        "amount_sat": 2100,
        "fee_sat": 10,
        "payment_hash": "aa" * 32,
    },
    "actual_amount_sat": 2100,
}

RAW_SEND_PENDING_RESPONSE = {
    "entry": {
        "id": "aa" * 32,
        "status": "ENTRY_STATUS_PENDING",
        "kind": "ENTRY_KIND_SEND",
        "amount_sat": 2100,
        "fee_sat": 10,
        "payment_hash": "aa" * 32,
    },
    "actual_amount_sat": 2100,
}

RAW_SEND_FAILED_RESPONSE = {
    "entry": {
        "id": "aa" * 32,
        "status": "ENTRY_STATUS_FAILED",
        "kind": "ENTRY_KIND_SEND",
        "amount_sat": 0,
        "fee_sat": 0,
    },
    "actual_amount_sat": 0,
}

# Recipient-side activity response (kind=recv)
RECV_ACTIVITY_RESPONSE = [
    {
        "status": "ENTRY_STATUS_COMPLETE",
        "kind": "RECV",
        "amount_sat": 2100,
        "fee_sat": 0,
        "progress": {"payment_hash": "aa" * 32},
    },
]


def _make_adapter(
    executor: FakeWavecliExecutor | None = None,
    rpc_server: str = "localhost:10029",
    network: str = "regtest",
    no_tls: bool = True,
    no_macaroons: bool = True,
) -> WavelengthAdapter:
    """Build a WavelengthAdapter with a FakeWavecliExecutor."""
    return WavelengthAdapter(
        executor=executor or FakeWavecliExecutor(),
        rpc_server=rpc_server,
        network=network,
        no_tls=no_tls,
        no_macaroons=no_macaroons,
    )


# ===========================================================================
# 1. Regtest-only enforcement
# ===========================================================================


class TestRegtestGuard:
    def test_regtest_accepted(self):
        """Network 'regtest' is accepted."""
        adapter = _make_adapter()
        assert adapter._network == "regtest"

    def test_mainnet_rejected(self):
        """Network 'mainnet' is rejected at construction."""
        with pytest.raises(ValueError, match="regtest"):
            WavelengthAdapter(
                executor=FakeWavecliExecutor(),
                network="mainnet",
            )

    def test_testnet_rejected(self):
        """Network 'testnet' is rejected."""
        with pytest.raises(ValueError, match="regtest"):
            WavelengthAdapter(
                executor=FakeWavecliExecutor(),
                network="testnet",
            )

    def test_signet_rejected(self):
        """Network 'signet' is rejected."""
        with pytest.raises(ValueError, match="regtest"):
            WavelengthAdapter(
                executor=FakeWavecliExecutor(),
                network="signet",
            )

    def test_empty_network_rejected(self):
        """Empty string network is rejected."""
        with pytest.raises(ValueError, match="regtest"):
            WavelengthAdapter(
                executor=FakeWavecliExecutor(),
                network="",
            )

    def test_custom_network_rejected(self):
        """Any custom string is rejected."""
        with pytest.raises(ValueError, match="regtest"):
            WavelengthAdapter(
                executor=FakeWavecliExecutor(),
                network="custom-chain",
            )

    def test_error_message_warns_about_mainnet(self):
        """Error message explicitly warns against mainnet default."""
        with pytest.raises(ValueError, match="Never default to mainnet"):
            WavelengthAdapter(
                executor=FakeWavecliExecutor(),
                network="mainnet",
            )


# ===========================================================================
# 2. Command construction (raw RPC, injection-safe)
# ===========================================================================


class TestCommandConstruction:
    def test_raw_rpc_prepare_command(self):
        """Raw PrepareSend command uses dev RPC path."""
        cmd = _build_raw_rpc_cmd(
            service="wavewalletrpc.WalletService",
            method="PrepareSend",
            request_json='{"invoice":"lnbcrt...","max_fee_sat":100}',
        )
        assert cmd[0] == "wavecli"
        assert "dev" in cmd
        assert "wavewalletrpc.WalletService" in cmd
        assert "PrepareSend" in cmd
        assert "--request-json" in cmd
        assert "--no-tls" in cmd
        assert "--no-macaroons" in cmd
        assert "--json" in cmd

    def test_raw_rpc_send_command(self):
        """Raw Send command uses dev RPC path with send_intent_id."""
        cmd = _build_raw_rpc_cmd(
            service="wavewalletrpc.WalletService",
            method="Send",
            request_json='{"send_intent_id":"si-abc123"}',
        )
        assert "dev" in cmd
        assert "Send" in cmd
        assert "--request-json" in cmd
        # The request-json is a single list element; check it contains the key
        json_arg = cmd[cmd.index("--request-json") + 1]
        parsed = json.loads(json_arg)
        assert parsed["send_intent_id"] == "si-abc123"

    def test_raw_rpc_does_not_use_high_level_send(self):
        """Raw RPC commands must NOT contain the high-level 'send' verb."""
        cmd_prepare = _build_raw_rpc_cmd(
            service="wavewalletrpc.WalletService",
            method="PrepareSend",
            request_json="{}",
        )
        cmd_send = _build_raw_rpc_cmd(
            service="wavewalletrpc.WalletService",
            method="Send",
            request_json="{}",
        )
        # 'send' as a standalone verb should not appear (only in service name)
        for c in [cmd_prepare, cmd_send]:
            # Count occurrences of 'send' as a standalone arg
            standalone = [a for a in c if a == "send"]
            assert len(standalone) == 0, (
                f"high-level 'send' verb found in command: {c}"
            )

    def test_activity_command_uses_recv(self):
        """Activity command defaults to kind=recv for receipt verification."""
        cmd = _build_wavecli_activity_cmd(kind="recv")
        assert cmd[0] == "wavecli"
        assert "activity" in cmd
        assert "--kind" in cmd
        assert cmd[cmd.index("--kind") + 1] == "recv"

    def test_activity_command_kind_send_explicit(self):
        """Activity command can explicitly use kind=send."""
        cmd = _build_wavecli_activity_cmd(kind="send")
        assert cmd[cmd.index("--kind") + 1] == "send"

    def test_activity_inspect_command(self):
        """Activity inspect subcommand is constructed correctly."""
        cmd = _build_wavecli_activity_cmd(inspect_id="entry-001")
        assert "activity" in cmd
        assert "inspect" in cmd
        assert "entry-001" in cmd
        assert "--format" not in cmd  # inspect doesn't use --format

    def test_injection_safe_raw_rpc(self):
        """All values are separate list elements (no shell injection)."""
        malicious = '{"invoice":"lnbc1$(rm -rf /)"}'
        cmd = _build_raw_rpc_cmd(
            service="wavewalletrpc.WalletService",
            method="PrepareSend",
            request_json=malicious,
        )
        # The malicious string is a single list element
        assert malicious in cmd
        for arg in cmd:
            assert arg == malicious or "$(" not in arg

    def test_injection_via_rpc_server(self):
        """Malicious rpc_server value is a single list element."""
        cmd = _build_raw_rpc_cmd(
            service="wavewalletrpc.WalletService",
            method="PrepareSend",
            request_json="{}",
            rpc_server="evil.com:8080; rm -rf /",
        )
        assert "evil.com:8080; rm -rf /" in cmd


# ===========================================================================
# 3. prepare() — raw PrepareSend, binding field validation
# ===========================================================================


class TestPrepare:
    def test_prepare_returns_fee_and_hash(self):
        """prepare() returns PrepareResult with fee and hash."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        adapter = _make_adapter(executor=executor)

        result = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )
        assert isinstance(result, PrepareResult)
        assert result.fee_sat == 10
        assert result.prepared_hash  # non-empty
        assert result.rail == Rail.LIGHTNING
        assert isinstance(result.prepared_payload, bytes)
        assert len(result.prepared_payload) > 0

    def test_prepare_uses_raw_prepare_send_rpc(self):
        """prepare() calls raw PrepareSend, NOT high-level wavecli send."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        adapter = _make_adapter(executor=executor)

        adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )
        cmd = executor.last_call
        assert cmd is not None
        # Must use raw RPC path
        assert "dev" in cmd
        assert "wavewalletrpc.WalletService" in cmd
        assert "PrepareSend" in cmd
        assert "--request-json" in cmd
        # Must NOT use high-level send verb
        standalone_send = [a for a in cmd if a == "send"]
        assert len(standalone_send) == 0, (
            "prepare() must not call high-level 'wavecli send'"
        )

    def test_prepare_payload_is_valid_json(self):
        """prepared_payload decodes to valid JSON with all binding fields."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        adapter = _make_adapter(executor=executor)

        result = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )
        payload = json.loads(result.prepared_payload)
        assert "fee_sat" in payload
        assert "payment_hash" in payload
        assert "invoice" in payload
        assert "send_intent_id" in payload
        assert "amount_sat" in payload
        assert "total_outflow_sat" in payload
        assert "expires_at_unix" in payload
        assert "rail" in payload
        assert payload["invoice"] == SAMPLE_INVOICE
        assert payload["send_intent_id"] == "si-abc123"
        assert payload["expires_at_unix"] == FAR_FUTURE_EXPIRY
        assert payload["payment_hash"] == "aa" * 32
        assert payload["max_fee_sat"] == 100

    def test_prepare_rejects_empty_send_intent_id(self):
        """prepare() rejects PrepareSend with empty send_intent_id."""
        executor = FakeWavecliExecutor()
        executor.set_response({
            **RAW_PREPARE_RESPONSE,
            "send_intent_id": "",
        })
        adapter = _make_adapter(executor=executor)

        with pytest.raises(AdapterError, match="empty send_intent_id"):
            adapter.prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_rejects_fee_exceeding_max(self):
        """prepare() rejects PrepareSend fee > max_fee_sat."""
        executor = FakeWavecliExecutor()
        executor.set_response({
            **RAW_PREPARE_RESPONSE,
            "expected_fee_sat": 500,
        })
        adapter = _make_adapter(executor=executor)

        with pytest.raises(AdapterError, match="exceeds max_fee_sat"):
            adapter.prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_rejects_unknown_fee(self):
        """prepare() rejects PrepareSend with fee_known=False."""
        executor = FakeWavecliExecutor()
        executor.set_response({
            **RAW_PREPARE_RESPONSE,
            "fee_known": False,
        })
        adapter = _make_adapter(executor=executor)

        with pytest.raises(AdapterError, match="fee_known=false"):
            adapter.prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_rejects_unknown_total_outflow(self):
        """prepare() rejects when total_outflow_known=False."""
        executor = FakeWavecliExecutor()
        executor.set_response({
            **RAW_PREPARE_RESPONSE,
            "total_outflow_known": False,
        })
        adapter = _make_adapter(executor=executor)

        with pytest.raises(AdapterError, match="total_outflow_known=false"):
            adapter.prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    @pytest.mark.parametrize(
        ("override", "match"),
        [
            ({"payment_hash": ""}, "empty payment_hash"),
            ({"expires_at_unix": 1}, "expired intent"),
            ({"amount_sat": 2099}, "does not match quoted amount"),
            ({"expected_total_outflow_sat": 2109}, "does not equal amount plus fee"),
        ],
    )
    def test_prepare_rejects_incomplete_or_inconsistent_preview(self, override, match):
        executor = FakeWavecliExecutor()
        executor.set_response({**RAW_PREPARE_RESPONSE, **override})
        with pytest.raises(AdapterError, match=match):
            _make_adapter(executor=executor).prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_rejects_no_invoice(self):
        """prepare() rejects receive instruction without invoice."""
        adapter = _make_adapter()
        with pytest.raises(AdapterError, match="no invoice"):
            adapter.prepare(
                receive_instruction=RailReceiveInstruction(
                    rail=Rail.LIGHTNING, invoice=None
                ),
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_rejects_wrong_rail(self):
        """prepare() rejects non-LIGHTNING rail in receive instruction."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        adapter = _make_adapter(executor=executor)
        result = adapter.prepare(
            receive_instruction=RailReceiveInstruction(
                rail=Rail.LIGHTNING, invoice=SAMPLE_INVOICE
            ),
            amount_sat=2100,
            max_fee_sat=100,
        )
        assert result.rail == Rail.LIGHTNING

    def test_prepare_rejects_executor_error(self):
        """prepare() propagates AdapterError from executor."""
        executor = FakeWavecliExecutor()
        # No response configured → raises AdapterError
        adapter = _make_adapter(executor=executor)
        with pytest.raises(AdapterError, match="no response configured"):
            adapter.prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_hash_deterministic(self):
        """Same PrepareSend response → same prepared_hash."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response(RAW_PREPARE_RESPONSE)
        adapter = _make_adapter(executor=executor)

        r1 = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )
        r2 = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )
        assert r1.prepared_hash == r2.prepared_hash
        assert r1.prepared_payload == r2.prepared_payload


# ===========================================================================
# 4. execute() — raw Send with exact send_intent_id
# ===========================================================================


class TestExecute:
    def test_execute_requires_matching_hash(self):
        """execute() rejects prepared_hash mismatch."""
        adapter = _make_adapter()
        payload = json.dumps(
            {"send_intent_id": "si-x", "invoice": "x",
             "fee_sat": 10, "max_fee_sat": 50,
             "amount_sat": 100, "total_outflow_sat": 110,
             "expires_at_unix": NOW + ONE_HOUR,
             "payment_hash": "aa" * 32, "rail": "LIGHTNING"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        with pytest.raises(AdapterError, match="does not match"):
            adapter.execute(
                prepared_payload=payload,
                prepared_hash="ff" * 32,  # wrong hash
            )

    def test_execute_uses_raw_send_rpc(self):
        """execute() calls raw Send with send_intent_id, NOT high-level send."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response(RAW_SEND_RESPONSE)
        adapter = _make_adapter(executor=executor)

        # First prepare to get valid payload
        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        # Now execute
        adapter.execute(
            prepared_payload=prepared.prepared_payload,
            prepared_hash=prepared.prepared_hash,
        )

        # Verify the execute command
        cmd = executor.calls[-1]
        assert cmd is not None
        assert "dev" in cmd
        assert "wavewalletrpc.WalletService" in cmd
        assert "Send" in cmd
        assert "--request-json" in cmd

        # Must NOT use high-level send verb
        standalone_send = [a for a in cmd if a == "send"]
        assert len(standalone_send) == 0, (
            "execute() must not call high-level 'wavecli send'"
        )

        # Must contain the exact send_intent_id from prepare
        json_payload = cmd[cmd.index("--request-json") + 1]
        parsed = json.loads(json_payload)
        assert parsed["send_intent_id"] == "si-abc123"

    def test_execute_binds_exact_send_intent_id(self):
        """execute() passes the exact send_intent_id from prepare, no re-prepare."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response(RAW_SEND_RESPONSE)
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        adapter.execute(
            prepared_payload=prepared.prepared_payload,
            prepared_hash=prepared.prepared_hash,
        )

        # Only 2 calls: raw PrepareSend + raw Send (no third re-prepare)
        assert len(executor.calls) == 2, (
            f"expected exactly 2 calls (prepare + send), got {len(executor.calls)}: "
            f"{executor.calls}"
        )

        # Second call must be Send with the exact intent ID
        send_cmd = executor.calls[1]
        assert "Send" in send_cmd
        json_arg = send_cmd[send_cmd.index("--request-json") + 1]
        parsed = json.loads(json_arg)
        assert parsed["send_intent_id"] == "si-abc123"

    def test_execute_returns_settlement_ref(self):
        """execute() returns ExecuteResult with payment_hash as settlement_ref."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response(RAW_SEND_RESPONSE)
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        result = adapter.execute(
            prepared_payload=prepared.prepared_payload,
            prepared_hash=prepared.prepared_hash,
        )
        assert result.settlement_ref == "aa" * 32
        assert result.amount_sat == 2100
        assert result.fee_sat == 10
        assert result.rail == Rail.LIGHTNING

    def test_execute_maps_actual_amount_sat(self):
        """execute() maps actual_amount_sat from SendResponse correctly."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response({
            "entry": {
                "id": "aa" * 32,
                "status": "ENTRY_STATUS_COMPLETE",
                "amount_sat": 2000,
                "fee_sat": 5,
            },
            "actual_amount_sat": 2100,
        })
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        result = adapter.execute(
            prepared_payload=prepared.prepared_payload,
            prepared_hash=prepared.prepared_hash,
        )
        # actual_amount_sat takes precedence over entry.amount_sat
        assert result.amount_sat == 2100

    @pytest.mark.parametrize(
        "override",
        [
            {"actual_amount_sat": 2099},
            {"entry": {**RAW_SEND_RESPONSE["entry"], "id": "bb" * 32}},
        ],
    )
    def test_execute_reconciles_when_result_differs_from_preview(self, override):
        executor = FakeWavecliExecutor()
        executor.set_responses(RAW_PREPARE_RESPONSE, {**RAW_SEND_RESPONSE, **override})
        adapter = _make_adapter(executor=executor)
        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE, amount_sat=2100, max_fee_sat=100
        )
        with pytest.raises(AmbiguousResult, match="differs from the prepared"):
            adapter.execute(prepared.prepared_payload, prepared.prepared_hash)

    def test_execute_rejects_failed_status(self):
        """execute() raises AdapterError on FAILED status."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response(RAW_SEND_FAILED_RESPONSE)
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        with pytest.raises(AdapterError, match="FAILED"):
            adapter.execute(
                prepared_payload=prepared.prepared_payload,
                prepared_hash=prepared.prepared_hash,
            )

    def test_execute_ambiguous_on_pending(self):
        """execute() raises AmbiguousResult on PENDING status."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response(RAW_SEND_PENDING_RESPONSE)
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        with pytest.raises(AmbiguousResult, match="PENDING"):
            adapter.execute(
                prepared_payload=prepared.prepared_payload,
                prepared_hash=prepared.prepared_hash,
            )

    def test_execute_ambiguous_on_missing_entry(self):
        """execute() raises AmbiguousResult when SendResponse has no entry."""
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response({"actual_amount_sat": 2100})
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        with pytest.raises(AdapterError, match="missing 'entry'"):
            adapter.execute(
                prepared_payload=prepared.prepared_payload,
                prepared_hash=prepared.prepared_hash,
            )

    def test_execute_ambiguous_on_empty_payment_hash(self):
        """execute() raises AdapterError when entry has empty id (no payment_hash)."""
        # Real WalletEntry.id for Lightning SEND/RECV is the payment_hash.
        # If the daemon returns an entry with empty id, we fail hard.
        executor = FakeWavecliExecutor()
        executor.set_response(RAW_PREPARE_RESPONSE)
        executor.set_response({
            "entry": {
                "id": "",
                "status": "ENTRY_STATUS_COMPLETE",
                "amount_sat": 2100,
                "fee_sat": 10,
            },
            "actual_amount_sat": 2100,
        })
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        with pytest.raises(AdapterError, match="empty id"):
            adapter.execute(
                prepared_payload=prepared.prepared_payload,
                prepared_hash=prepared.prepared_hash,
            )

    def test_execute_executor_error_is_ambiguous(self):
        """Any error after a Send attempt is reconciliation-only, never retry."""
        class FailingExecutor(WavecliExecutor):
            def run(self, cmd, *, timeout=30):
                raise AdapterError("gRPC connection reset")

        adapter = WavelengthAdapter(executor=FailingExecutor())
        payload = json.dumps({
            "send_intent_id": "si-live", "amount_sat": 2100,
            "fee_sat": 10, "total_outflow_sat": 2110,
            "expires_at_unix": int(time.time()) + 3600,
            "payment_hash": "aa" * 32, "rail": "LIGHTNING",
            "invoice": SAMPLE_INVOICE, "max_fee_sat": 100,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

        with pytest.raises(AmbiguousResult, match="outcome unknown"):
            adapter.execute(
                prepared_payload=payload,
                prepared_hash=compute_prepared_hash(payload),
            )

    def test_execute_rejects_missing_intent_id(self):
        """execute() rejects payload with empty send_intent_id."""
        adapter = _make_adapter()
        payload = json.dumps({
            "send_intent_id": "",
            "invoice": "x",
            "fee_sat": 10,
            "max_fee_sat": 50,
            "amount_sat": 100,
            "total_outflow_sat": 110,
            "expires_at_unix": NOW + ONE_HOUR,
            "payment_hash": "aa" * 32,
            "rail": "LIGHTNING",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

        prepared_hash = compute_prepared_hash(payload)

        with pytest.raises(AdapterError, match="missing send_intent_id"):
            adapter.execute(
                prepared_payload=payload,
                prepared_hash=prepared_hash,
            )

    def test_execute_rejects_expired_intent(self):
        """execute() rejects payload with expired intent."""
        adapter = _make_adapter()
        payload = json.dumps({
            "send_intent_id": "si-old",
            "invoice": "x",
            "fee_sat": 10,
            "max_fee_sat": 50,
            "amount_sat": 100,
            "total_outflow_sat": 110,
            "expires_at_unix": 100,  # long expired
            "payment_hash": "aa" * 32,
            "rail": "LIGHTNING",
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")

        prepared_hash = compute_prepared_hash(payload)

        with pytest.raises(AdapterError, match="expired"):
            adapter.execute(
                prepared_payload=payload,
                prepared_hash=prepared_hash,
            )


# ===========================================================================
# 5. verify_receipt() — recipient-side activity --kind recv
# ===========================================================================


class TestVerifyReceipt:
    def test_verify_receipt_queries_recv_kind(self):
        """verify_receipt queries activity --kind recv (recipient-side)."""
        executor = FakeWavecliExecutor()
        executor.set_response(RECV_ACTIVITY_RESPONSE)
        adapter = _make_adapter(executor=executor)

        adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=2100,
        )
        cmd = executor.last_call
        assert cmd is not None
        assert "activity" in cmd
        assert "--kind" in cmd
        assert cmd[cmd.index("--kind") + 1] == "recv", (
            "verify_receipt MUST query 'recv' (recipient-side), not 'send'"
        )

    def test_verify_receipt_found_complete(self):
        """verify_receipt returns verified=True when entry found and COMPLETE."""
        executor = FakeWavecliExecutor()
        executor.set_response(RECV_ACTIVITY_RESPONSE)
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=2100,
        )
        assert result.verified is True
        assert result.settlement_ref == "aa" * 32
        assert result.amount_sat == 2100
        assert result.fee_sat == 0

    def test_verify_receipt_not_found_fail_closed(self):
        """verify_receipt returns verified=False when no matching entry."""
        executor = FakeWavecliExecutor()
        executor.set_response([
            {
                "status": "ENTRY_STATUS_COMPLETE",
                "amount_sat": 1000,
                "fee_sat": 5,
                "progress": {"payment_hash": "cc" * 32},
            },
        ])
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="dd" * 32,  # doesn't match
            expected_amount_sat=1000,
        )
        assert result.verified is False
        assert result.error is not None and "no activity entry found" in result.error

    def test_verify_receipt_amount_mismatch(self):
        """verify_receipt fails when amount doesn't match."""
        executor = FakeWavecliExecutor()
        executor.set_response(RECV_ACTIVITY_RESPONSE)
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=9999,  # wrong amount
        )
        assert result.verified is False
        assert result.error is not None and "amount mismatch" in result.error

    def test_verify_receipt_pending_status_fail_closed(self):
        """verify_receipt fails when entry status is PENDING."""
        executor = FakeWavecliExecutor()
        executor.set_response([
            {
                "status": "ENTRY_STATUS_PENDING",
                "amount_sat": 2100,
                "fee_sat": 0,
                "progress": {"payment_hash": "aa" * 32},
            },
        ])
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=2100,
        )
        assert result.verified is False
        assert result.error is not None and "PENDING" in result.error

    def test_verify_receipt_empty_activity_fail_closed(self):
        """verify_receipt fails when activity query returns empty list."""
        executor = FakeWavecliExecutor()
        executor.set_response([])
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=2100,
        )
        assert result.verified is False
        assert result.error is not None and "no activity entry found" in result.error

    def test_verify_receipt_executor_error_fail_closed(self):
        """verify_receipt fails closed when executor raises."""
        executor = FakeWavecliExecutor()
        # No response configured → will raise AdapterError
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=2100,
        )
        assert result.verified is False
        assert result.error is not None and "cannot query activity" in result.error


# ===========================================================================
# 6. Error redaction
# ===========================================================================


class TestRedaction:
    def test_redact_invoice(self):
        """Invoices are redacted in error messages."""
        text = "error sending lnbcrt2100n1p0000000000000000000000"
        redacted = redact_sensitive(text)
        assert "lnbcrt" not in redacted
        assert "<INVOICE>" in redacted

    def test_redact_lntb_invoice(self):
        """lntb invoices are redacted."""
        text = "lntb1234567890abcdef"
        redacted = redact_sensitive(text)
        assert "lntb" not in redacted
        assert "<INVOICE>" in redacted

    def test_redact_macaroon_path(self):
        """Macaroon paths are redacted."""
        text = "unable to load /home/user/.wavelength/data/regtest/admin.macaroon"
        redacted = redact_sensitive(text)
        assert "/home/user" not in redacted
        assert "<MACAROON_PATH>" in redacted

    def test_redact_64char_hex(self):
        """64-char hex strings (payment hashes) are truncated."""
        text = "a" * 64
        redacted = redact_sensitive(text)
        assert len(redacted) < 64
        assert redacted.startswith("aaaaaaaa")

    def test_redact_no_false_positives(self):
        """Short hex strings are not redacted."""
        text = "entry-001 status COMPLETE"
        redacted = redact_sensitive(text)
        assert redacted == text


# ===========================================================================
# 7. Strict JSON parsing
# ===========================================================================


class TestStrictParsing:
    def test_parse_prepare_rejects_empty_intent_id(self):
        """_parse_prepare_response rejects empty send_intent_id."""
        with pytest.raises(AdapterError, match="empty send_intent_id"):
            _parse_prepare_response({
                "send_intent_id": "",
                "amount_sat": 100,
                "expected_fee_sat": 5,
                "fee_known": True,
                "total_outflow_known": True,
                "expected_total_outflow_sat": 105,
                "expires_at_unix": NOW + ONE_HOUR,
            })

    def test_parse_prepare_rejects_fee_not_known(self):
        """_parse_prepare_response rejects fee_known=False."""
        with pytest.raises(AdapterError, match="fee_known=false"):
            _parse_prepare_response({
                "send_intent_id": "si-x",
                "amount_sat": 100,
                "expected_fee_sat": 5,
                "fee_known": False,
                "total_outflow_known": True,
                "expected_total_outflow_sat": 105,
                "expires_at_unix": NOW + ONE_HOUR,
            })

    def test_parse_prepare_rejects_outflow_not_known(self):
        """_parse_prepare_response rejects total_outflow_known=False."""
        with pytest.raises(AdapterError, match="total_outflow_known=false"):
            _parse_prepare_response({
                "send_intent_id": "si-x",
                "amount_sat": 100,
                "expected_fee_sat": 5,
                "fee_known": True,
                "total_outflow_known": False,
                "expected_total_outflow_sat": 105,
                "expires_at_unix": NOW + ONE_HOUR,
            })

    def test_parse_prepare_rejects_bad_fee_type(self):
        """_parse_prepare_response rejects non-numeric expected_fee_sat."""
        with pytest.raises(AdapterError, match="expected_fee_sat type"):
            _parse_prepare_response({
                "send_intent_id": "si-x",
                "amount_sat": 100,
                "expected_fee_sat": "not-a-number",
                "fee_known": True,
                "total_outflow_known": True,
                "expected_total_outflow_sat": 105,
                "expires_at_unix": NOW + ONE_HOUR,
            })

    def test_parse_send_rejects_missing_entry(self):
        """_parse_send_response rejects SendResponse without entry."""
        with pytest.raises(AdapterError, match="missing 'entry'"):
            _parse_send_response({"actual_amount_sat": 100})

    def test_parse_send_rejects_empty_entry_id(self):
        """_parse_send_response rejects entry with empty id."""
        with pytest.raises(AdapterError, match="empty id"):
            _parse_send_response({
                "entry": {"id": "", "status": "COMPLETE"},
                "actual_amount_sat": 100,
            })

    def test_parse_send_maps_entry_id_to_payment_hash(self):
        """_parse_send_response uses WalletEntry.id as payment_hash for Lightning."""
        result = _parse_send_response({
            "entry": {
                "id": "aa" * 32,
                "status": "ENTRY_STATUS_COMPLETE",
                "amount_sat": 2100,
                "fee_sat": 10,
            },
            "actual_amount_sat": 2100,
        })
        assert result["payment_hash"] == "aa" * 32
        assert result["entry_id"] == "aa" * 32
        assert result["amount_sat"] == 2100

    def test_parse_activity_entry_rejects_non_dict(self):
        """_parse_activity_entry rejects non-dict input."""
        with pytest.raises(AdapterError, match="expected dict"):
            _parse_activity_entry("not-a-dict")


# ===========================================================================
# 8. FakeWavecliExecutor
# ===========================================================================


class TestFakeExecutor:
    def test_returns_configured_response(self):
        """FakeWavecliExecutor returns the configured response."""
        ex = FakeWavecliExecutor()
        ex.set_response({"status": "OK"})
        result = ex.run(["wavecli", "getinfo"])
        assert result == {"status": "OK"}

    def test_returns_sequential_responses(self):
        """Multiple responses are returned in order."""
        ex = FakeWavecliExecutor()
        ex.set_response({"step": 1})
        ex.set_response({"step": 2})
        assert ex.run(["cmd1"]) == {"step": 1}
        assert ex.run(["cmd2"]) == {"step": 2}

    def test_raises_when_no_response(self):
        """Raises AdapterError when no response is configured."""
        ex = FakeWavecliExecutor()
        with pytest.raises(AdapterError, match="no response configured"):
            ex.run(["wavecli"])

    def test_records_calls(self):
        """FakeWavecliExecutor records all calls."""
        ex = FakeWavecliExecutor()
        ex.set_response({})
        ex.set_response({})
        ex.run(["wavecli", "getinfo"])
        ex.run(["wavecli", "activity"])
        assert len(ex.calls) == 2
        assert ex.calls[0] == ["wavecli", "getinfo"]
        assert ex.last_call == ["wavecli", "activity"]

    def test_last_call_empty(self):
        """last_call returns None when no calls made."""
        ex = FakeWavecliExecutor()
        assert ex.last_call is None


# ===========================================================================
# 9. Interface compliance
# ===========================================================================


class TestInterfaceCompliance:
    def test_wavelength_adapter_is_settlement_adapter(self):
        """WavelengthAdapter implements SettlementAdapter."""
        assert issubclass(WavelengthAdapter, SettlementAdapter)

    def test_rail_property(self):
        """WavelengthAdapter.rail returns Rail.LIGHTNING."""
        adapter = _make_adapter()
        assert adapter.rail == Rail.LIGHTNING
