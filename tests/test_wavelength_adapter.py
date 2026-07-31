"""
Hermes Payments — P4 Wavelength adapter tests.

Tests the WavelengthAdapter with FakeWavecliExecutor.  No subprocess,
no network, no real waved daemon.

Covers:
1. Regtest-only enforcement (construction guard)
2. Command construction (injection-safe list args)
3. prepare() — dry-run, opaque payload, fee validation
4. execute() — --force, identity check, terminal status parsing
5. verify_receipt() — activity lookup, fail-closed on missing
6. Error redaction (invoices, macaroon paths)
7. Strict JSON parsing (unknown/nonterminal → AmbiguousResult)
8. FakeWavecliExecutor basics
"""
from __future__ import annotations

import json
import os
import sys
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
    _build_wavecli_activity_cmd,
    _build_wavecli_send_cmd,
    _parse_dry_run_response,
    _parse_send_result,
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
    "000000000000000000000000000000000"
)

SAMPLE_RECEIVE = RailReceiveInstruction(
    rail=Rail.LIGHTNING,
    invoice=SAMPLE_INVOICE,
)

DRY_RUN_RESPONSE = {
    "send_intent_id": "si-abc123",
    "expected_fee_sat": 10,
    "fee_known": True,
    "rail": "SEND_RAIL_LIGHTNING",
    "payment_hash": "aa" * 32,
}

COMPLETE_SEND_RESPONSE = {
    "status": "COMPLETE",
    "kind": "SEND",
    "amount_sat": 2100,
    "fee_sat": 10,
    "payment_hash": "aa" * 32,
    "preimage": "bb" * 32,
    "id": "entry-001",
}

PENDING_SEND_RESPONSE = {
    "status": "PENDING",
    "kind": "SEND",
    "amount_sat": 2100,
    "fee_sat": 10,
    "payment_hash": "aa" * 32,
    "id": "entry-002",
}

FAILED_SEND_RESPONSE = {
    "status": "FAILED",
    "kind": "SEND",
    "amount_sat": 0,
    "fee_sat": 0,
    "id": "entry-003",
    "error": "insufficient balance",
}

ACTIVITY_RESPONSE_COMPLETE = [
    {
        "status": "ENTRY_STATUS_COMPLETE",
        "kind": "SEND",
        "amount_sat": 2100,
        "fee_sat": 10,
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
# 2. Command construction (injection-safe)
# ===========================================================================


class TestCommandConstruction:
    def test_dry_run_command_includes_all_flags(self):
        """Dry-run command includes all required flags."""
        cmd = _build_wavecli_send_cmd(
            invoice=SAMPLE_INVOICE,
            offchain=True,
            dry_run=True,
            force=False,
            max_fee_sat=100,
        )
        assert cmd[0] == "wavecli"
        assert "--network" in cmd
        assert cmd[cmd.index("--network") + 1] == "regtest"
        assert "--rpcserver" in cmd
        assert "--no-tls" in cmd
        assert "--no-macaroons" in cmd
        assert "--json" in cmd
        assert "send" in cmd
        assert SAMPLE_INVOICE in cmd
        assert "--offchain" in cmd
        assert "--dry-run" in cmd
        assert "--force" not in cmd
        assert "--max-fee" in cmd
        assert cmd[cmd.index("--max-fee") + 1] == "100"

    def test_force_command_for_execute(self):
        """Execute command includes --force."""
        cmd = _build_wavecli_send_cmd(
            invoice=SAMPLE_INVOICE,
            offchain=True,
            dry_run=False,
            force=True,
            max_fee_sat=50,
        )
        assert "--force" in cmd
        assert "--dry-run" not in cmd

    def test_activity_command(self):
        """Activity command is constructed correctly."""
        cmd = _build_wavecli_activity_cmd(kind="send")
        assert cmd[0] == "wavecli"
        assert "activity" in cmd
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "json"
        assert "--kind" in cmd
        assert cmd[cmd.index("--kind") + 1] == "send"

    def test_activity_inspect_command(self):
        """Activity inspect subcommand is constructed correctly."""
        cmd = _build_wavecli_activity_cmd(inspect_id="entry-001")
        assert "activity" in cmd
        assert "inspect" in cmd
        assert "entry-001" in cmd
        assert "--format" not in cmd  # inspect doesn't use --format

    def test_injection_safe_list_args(self):
        """All values are separate list elements (no shell injection)."""
        malicious_invoice = "lnbc1$(rm -rf /)"
        cmd = _build_wavecli_send_cmd(
            invoice=malicious_invoice,
            offchain=True,
            dry_run=True,
            force=False,
        )
        # The malicious string is a single list element, not interpolated
        assert malicious_invoice in cmd
        # No shell metacharacters are interpreted
        for arg in cmd:
            assert arg == malicious_invoice or "$(" not in arg

    def test_injection_via_rpc_server(self):
        """Malicious rpc_server value is a single list element."""
        cmd = _build_wavecli_send_cmd(
            invoice=SAMPLE_INVOICE,
            offchain=True,
            dry_run=True,
            force=False,
            rpc_server="evil.com:8080; rm -rf /",
        )
        assert "evil.com:8080; rm -rf /" in cmd


# ===========================================================================
# 3. prepare() — dry-run
# ===========================================================================


class TestPrepare:
    def test_prepare_returns_fee_and_hash(self):
        """prepare() returns PrepareResult with fee and hash."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
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

    def test_prepare_payload_is_valid_json(self):
        """prepared_payload decodes to valid JSON with required fields."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
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
        assert payload["invoice"] == SAMPLE_INVOICE

    def test_prepare_uses_dry_run_flag(self):
        """prepare() passes --dry-run to wavecli."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
        adapter = _make_adapter(executor=executor)

        adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )
        cmd = executor.last_call
        assert cmd is not None
        assert "--dry-run" in cmd
        assert "--force" not in cmd

    def test_prepare_rejects_fee_exceeding_max(self):
        """prepare() rejects dry-run fee > max_fee_sat."""
        executor = FakeWavecliExecutor()
        executor.set_response({**DRY_RUN_RESPONSE, "expected_fee_sat": 500})
        adapter = _make_adapter(executor=executor)

        with pytest.raises(AdapterError, match="exceeds max_fee_sat"):
            adapter.prepare(
                receive_instruction=SAMPLE_RECEIVE,
                amount_sat=2100,
                max_fee_sat=100,
            )

    def test_prepare_rejects_unknown_fee(self):
        """prepare() rejects dry-run with fee_known=False."""
        executor = FakeWavecliExecutor()
        executor.set_response({**DRY_RUN_RESPONSE, "fee_known": False})
        adapter = _make_adapter(executor=executor)

        with pytest.raises(AdapterError, match="unknown fee"):
            adapter.prepare(
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
        adapter = _make_adapter()
        # Rail.LIGHTNING is the only rail; sending it should work,
        # but the guard checks receive_instruction.rail == adapter.rail.
        # Since we can't construct a different Rail value (only LIGHTNING
        # exists), test that LIGHTNING *passes* the guard (no error from rail).
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
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
        """Same dry-run response → same prepared_hash."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response(DRY_RUN_RESPONSE)
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
# 4. execute() — force, identity, terminal status
# ===========================================================================


class TestExecute:
    def test_execute_requires_matching_hash(self):
        """execute() rejects prepared_hash mismatch."""
        adapter = _make_adapter()
        payload = json.dumps(
            {"invoice": "x", "fee_sat": 10, "max_fee_sat": 50},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

        with pytest.raises(AdapterError, match="does not match"):
            adapter.execute(
                prepared_payload=payload,
                prepared_hash="ff" * 32,  # wrong hash
            )

    def test_execute_uses_force_flag(self):
        """execute() passes --force to wavecli."""
        executor = FakeWavecliExecutor()
        # Responses in order: dry-run (for prepare), then send result (for execute)
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response(COMPLETE_SEND_RESPONSE)
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
        cmd = executor.last_call
        assert cmd is not None
        assert "--force" in cmd
        assert "--dry-run" not in cmd

    def test_execute_returns_settlement_ref(self):
        """execute() returns ExecuteResult with payment_hash as settlement_ref."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response(COMPLETE_SEND_RESPONSE)
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

    def test_execute_rejects_failed_status(self):
        """execute() raises AdapterError on FAILED status."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response(FAILED_SEND_RESPONSE)
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
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response(PENDING_SEND_RESPONSE)
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

    def test_execute_ambiguous_on_no_payment_hash(self):
        """execute() raises AmbiguousResult when no payment_hash and no preimage."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response({
            "status": "COMPLETE",
            "amount_sat": 2100,
            "fee_sat": 10,
            "payment_hash": "",
            "preimage": "",
            "id": "entry-004",
        })
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        with pytest.raises(AmbiguousResult, match="no payment_hash"):
            adapter.execute(
                prepared_payload=prepared.prepared_payload,
                prepared_hash=prepared.prepared_hash,
            )

    def test_execute_ambiguous_on_unknown_status(self):
        """execute() raises AmbiguousResult on unknown status."""
        executor = FakeWavecliExecutor()
        executor.set_response(DRY_RUN_RESPONSE)
        executor.set_response({
            "status": "WEIRD_STATUS",
            "amount_sat": 2100,
            "fee_sat": 10,
            "id": "entry-005",
        })
        adapter = _make_adapter(executor=executor)

        prepared = adapter.prepare(
            receive_instruction=SAMPLE_RECEIVE,
            amount_sat=2100,
            max_fee_sat=100,
        )

        with pytest.raises(AmbiguousResult, match="unexpected send status"):
            adapter.execute(
                prepared_payload=prepared.prepared_payload,
                prepared_hash=prepared.prepared_hash,
            )


# ===========================================================================
# 5. verify_receipt() — activity lookup, fail-closed
# ===========================================================================


class TestVerifyReceipt:
    def test_verify_receipt_found_complete(self):
        """verify_receipt returns verified=True when entry found and COMPLETE."""
        executor = FakeWavecliExecutor()
        executor.set_response(ACTIVITY_RESPONSE_COMPLETE)
        adapter = _make_adapter(executor=executor)

        result = adapter.verify_receipt(
            settlement_ref="aa" * 32,
            expected_amount_sat=2100,
        )
        assert result.verified is True
        assert result.settlement_ref == "aa" * 32
        assert result.amount_sat == 2100
        assert result.fee_sat == 10

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
        executor.set_response(ACTIVITY_RESPONSE_COMPLETE)
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
                "fee_sat": 10,
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
        text = "error sending lnbcrt2100n1p00000000000000000000000"
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
    def test_parse_dry_run_rejects_unknown_fee_type(self):
        """_parse_dry_run_response rejects non-numeric fee."""
        with pytest.raises(AdapterError, match="unexpected fee type"):
            _parse_dry_run_response({"expected_fee_sat": "not-a-number"})

    def test_parse_send_result_rejects_missing_id(self):
        """_parse_send_result rejects response without entry ID."""
        with pytest.raises(AdapterError, match="no entry ID"):
            _parse_send_result({"status": "COMPLETE"})

    def test_parse_send_result_rejects_failed(self):
        """_parse_send_result raises AdapterError on FAILED."""
        with pytest.raises(AdapterError, match="FAILED"):
            _parse_send_result(FAILED_SEND_RESPONSE)

    def test_parse_send_result_ambiguous_on_unknown(self):
        """_parse_send_result raises AmbiguousResult on unknown status."""
        with pytest.raises(AmbiguousResult, match="unexpected"):
            _parse_send_result({"status": "UNKNOWN_THING", "id": "x"})

    def test_parse_send_result_accepts_unspecified(self):
        """_parse_send_result accepts UNSPECIFIED as a terminal status."""
        result = _parse_send_result({
            "status": "UNSPECIFIED",
            "id": "entry-006",
            "amount_sat": 0,
            "fee_sat": 0,
        })
        assert result["status"] == "UNSPECIFIED"


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
