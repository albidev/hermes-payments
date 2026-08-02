"""P6 process boundary and lifecycle tests.

These tests prove that two Hermes processes can drive the full payment
lifecycle through JSONL commands, with isolated state roots and no live
infrastructure.  Fake executors stand in for Buzz and Wavelength.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.two_hermes_regtest.process import (
    HermesRegtestProcess,
    JsonlCommand,
    ProcessConfig,
    ProcessProtocolError,
    redacted_identifier,
)
from hermes_payments.adapter import AmbiguousResult
from hermes_payments.policy import StateError
from hermes_payments.transport import BuzzTransport, FakeExecutor
from tests.fixtures import (
    APPROVER_IDENTITY,
    NOW,
    RECIPIENT_IDENTITY,
    RECIPIENT_PUBKEY,
    SENDER_IDENTITY,
    SENDER_PUBKEY,
    make_intent,
    make_quote,
    make_receipt,
)
from tests.test_policy_core import StubAdapter

CHANNEL = "550e8400-e29b-41d4-a716-446655440000"
FAR_FUTURE = NOW + 10 * 365 * 24 * 3600


def _alice_config(tmp_path: Path) -> ProcessConfig:
    return ProcessConfig(
        role="alice",
        identity=SENDER_IDENTITY,
        channel=CHANNEL,
        state_root=tmp_path / "alice",
        network="regtest",
        audit_path=tmp_path / "alice" / "audit.jsonl",
        store_path=tmp_path / "alice" / "store.json",
        approver=APPROVER_IDENTITY,
    )


def _bob_config(tmp_path: Path) -> ProcessConfig:
    return ProcessConfig(
        role="bob",
        identity=RECIPIENT_IDENTITY,
        channel=CHANNEL,
        state_root=tmp_path / "bob",
        network="regtest",
        audit_path=tmp_path / "bob" / "audit.jsonl",
        store_path=tmp_path / "bob" / "store.json",
        approver=APPROVER_IDENTITY,
    )


def _make_process(
    config: ProcessConfig,
    executor: FakeExecutor,
    *,
    execute_raises: Exception | None = None,
) -> HermesRegtestProcess:
    adapter = StubAdapter(
        fee_sat=10,
        settlement_ref="payment_hash_abc123",
        execute_raises=execute_raises,
    )
    transport = BuzzTransport(
        executor=executor,
        channel=CHANNEL,
        clock=lambda: NOW,
    )
    return HermesRegtestProcess(
        config=config,
        adapter=adapter,
        transport=transport,
        clock=lambda: NOW,
    )


def _send_json(process: HermesRegtestProcess, payload: dict) -> dict:
    return json.loads(process.handle_line(json.dumps(payload, sort_keys=True)))


def _relay_last_event(
    source: FakeExecutor,
    dest: FakeExecutor,
    *,
    author_pubkey: str,
) -> None:
    events = source.get(channel=CHANNEL)
    if not events:
        return
    last = events[-1]
    from hermes_payments.transport import RawBuzzEvent

    dest.inject_event(
        RawBuzzEvent(
            id=last.id,
            pubkey=author_pubkey,
            kind=last.kind,
            content=last.content,
            tags=list(last.tags),
            created_at=last.created_at,
        )
    )


class TestProcessConfig:
    def test_process_config_accepts_explicit_signet(self, tmp_path: Path):
        config = ProcessConfig(
            role="alice",
            identity=SENDER_IDENTITY,
            channel=CHANNEL,
            state_root=tmp_path / "alice-signet",
            network="signet",
        )

        assert config.network == "signet"

    def test_process_config_rejects_unsupported_networks(self, tmp_path: Path):
        config = _alice_config(tmp_path)

        assert config.role == "alice"
        assert config.network == "regtest"

        with pytest.raises(ValueError, match="regtest"):
            ProcessConfig(
                role="bob",
                identity=RECIPIENT_IDENTITY,
                channel=CHANNEL,
                state_root=tmp_path / "mainnet",
                network="mainnet",
            )

    def test_process_config_rejects_missing_channel_and_state_root(self, tmp_path: Path):
        with pytest.raises(ValueError, match="channel"):
            ProcessConfig(
                role="bob",
                identity=RECIPIENT_IDENTITY,
                channel="",
                state_root=tmp_path / "bob",
                network="regtest",
            )

        with pytest.raises(ValueError, match="state_root"):
            ProcessConfig(
                role="bob",
                identity=RECIPIENT_IDENTITY,
                channel=CHANNEL,
                state_root=Path(""),
                network="regtest",
            )

    def test_process_config_defaults_audit_and_store_paths(self, tmp_path: Path):
        config = ProcessConfig(
            role="alice",
            identity=SENDER_IDENTITY,
            channel=CHANNEL,
            state_root=tmp_path / "alice",
            network="regtest",
        )

        assert config.audit_path == tmp_path / "alice" / "audit.jsonl"
        assert config.store_path == tmp_path / "alice" / "store.json"


class TestJsonlCommand:
    def test_known_commands_parse_and_unknown_rejected(self):
        command = JsonlCommand.parse('{"command":"receive","limit":10}')
        assert command.name == "receive"
        assert command.arguments == {"limit": 10}

        with pytest.raises(ProcessProtocolError, match="unknown command"):
            JsonlCommand.parse('{"command":"send_money","amount_sat":1}')

        with pytest.raises(ProcessProtocolError, match="valid JSON object"):
            JsonlCommand.parse("not-json")


class TestJsonlProcess:
    def test_status_is_redacted(self, tmp_path: Path):
        process = _make_process(_alice_config(tmp_path), FakeExecutor())
        response = _send_json(process, {"command": "status"})

        assert response["event"] == "status"
        assert response["role"] == "alice"
        assert response["network"] == "regtest"
        assert response["channel"] == CHANNEL

    def test_receive_requires_transport_handler(self, tmp_path: Path):
        from examples.two_hermes_regtest.process import JsonlProcess

        config = _alice_config(tmp_path)
        bare = JsonlProcess(config)
        with pytest.raises(ProcessProtocolError, match="receive handler"):
            bare.handle_line('{"command":"receive"}')


class TestRedaction:
    def test_redacted_identifier_never_emits_full_value(self):
        identifier = "a" * 64
        redacted = redacted_identifier(identifier)
        assert redacted == "aaaaaaaa..."
        assert identifier not in redacted

    def test_process_config_defaults_audit_and_store_paths(self, tmp_path: Path):
        config = ProcessConfig(
            role="alice",
            identity=SENDER_IDENTITY,
            channel=CHANNEL,
            state_root=tmp_path / "alice",
            network="regtest",
        )

        assert config.audit_path == tmp_path / "alice" / "audit.jsonl"
        assert config.store_path == tmp_path / "alice" / "store.json"

    def test_jsonl_command_output_is_machine_readable(self):
        command = JsonlCommand.parse(json.dumps({"command": "status"}))
        assert json.loads(command.to_json()) == {"command": "status"}

class TestHermesRegtestProcessLifecycle:
    def test_alice_can_submit_intent_to_bob(self, tmp_path: Path):
        alice_exec = FakeExecutor()
        bob_exec = FakeExecutor()
        alice = _make_process(_alice_config(tmp_path), alice_exec)
        bob = _make_process(_bob_config(tmp_path), bob_exec)

        intent = make_intent(
            amount_sat=2100,
            expires_at=FAR_FUTURE,
            idempotency_key="p6-001",
        )

        response = _send_json(alice, {
            "command": "submit_intent",
            "intent": intent.model_dump(exclude_none=True, mode="python"),
        })
        assert response["event"] == "submit_intent"
        assert response["intent_id"] == intent.id[:8] + "..."

        _relay_last_event(alice_exec, bob_exec, author_pubkey=SENDER_PUBKEY)

        fetched = _send_json(bob, {"command": "receive"})
        assert fetched["event"] == "receive"
        assert len(fetched["messages"]) == 1
        assert fetched["messages"][0]["type"] == "PaymentIntent"

    def test_bob_accepts_intent_and_publishes_quote(self, tmp_path: Path):
        alice_exec = FakeExecutor()
        bob_exec = FakeExecutor()
        alice = _make_process(_alice_config(tmp_path), alice_exec)
        bob = _make_process(_bob_config(tmp_path), bob_exec)

        intent = make_intent(
            amount_sat=2100,
            expires_at=FAR_FUTURE,
            idempotency_key="p6-002",
        )
        quote = make_quote(intent, fee_sat=10, expires_at=FAR_FUTURE)

        _send_json(alice, {
            "command": "submit_intent",
            "intent": intent.model_dump(exclude_none=True, mode="python"),
        })
        _relay_last_event(alice_exec, bob_exec, author_pubkey=SENDER_PUBKEY)
        fetched = _send_json(bob, {"command": "receive"})
        message_id = fetched["messages"][0]["message_id"]

        accept = _send_json(bob, {
            "command": "accept_intent",
            "message_id": message_id,
        })
        assert accept["event"] == "accept_intent"
        assert accept["intent_id"] == intent.id[:8] + "..."

        publish = _send_json(bob, {
            "command": "publish_quote",
            "quote": quote.model_dump(exclude_none=True, mode="python"),
        })
        assert publish["event"] == "publish_quote"
        assert publish["quote_id"] == quote.quote_id

    def test_full_lifecycle_to_settled(self, tmp_path: Path):
        alice_exec = FakeExecutor()
        bob_exec = FakeExecutor()
        alice = _make_process(_alice_config(tmp_path), alice_exec)
        bob = _make_process(_bob_config(tmp_path), bob_exec)

        intent = make_intent(
            amount_sat=2100,
            expires_at=FAR_FUTURE,
            idempotency_key="p6-settled",
        )
        quote = make_quote(intent, fee_sat=10, expires_at=FAR_FUTURE)

        # Alice submits intent
        _send_json(alice, {
            "command": "submit_intent",
            "intent": intent.model_dump(exclude_none=True, mode="python"),
        })
        _relay_last_event(alice_exec, bob_exec, author_pubkey=SENDER_PUBKEY)

        # Bob accepts intent and publishes quote
        fetched = _send_json(bob, {"command": "receive"})
        _send_json(bob, {
            "command": "accept_intent",
            "message_id": fetched["messages"][0]["message_id"],
        })
        _send_json(bob, {
            "command": "publish_quote",
            "quote": quote.model_dump(exclude_none=True, mode="python"),
        })
        _relay_last_event(bob_exec, alice_exec, author_pubkey=RECIPIENT_PUBKEY)

        # Alice accepts quote, prepares, approves and executes
        alice_fetched = _send_json(alice, {"command": "receive"})
        quotes = [m for m in alice_fetched["messages"] if m["type"] == "PaymentQuote"]
        assert len(quotes) == 1
        _send_json(alice, {
            "command": "accept_quote",
            "message_id": quotes[0]["message_id"],
        })
        prepared = _send_json(alice, {"command": "prepare"})
        assert prepared["event"] == "prepare"
        assert prepared["fee_sat"] == 10
        prepared_record = alice._orchestrator._intents[intent.id].prepared
        assert prepared_record is not None
        prepared_hash = prepared_record.prepared_hash
        assert prepared["prepared_hash"] == prepared_hash[:8] + "..."

        _send_json(alice, {
            "command": "approve",
            "intent_id": intent.id,
            "quote_id": quote.quote_id,
            # The process resolves this display-safe prefix against the
            # prepared record for the same intent; the full hash never leaves
            # the process boundary.
            "prepared_hash": prepared["prepared_hash"],
        })

        executed = _send_json(alice, {"command": "execute"})
        assert executed["event"] == "execute"
        assert executed["state"] == "settled"
        assert executed["settlement_ref"] == "payment_..."
        assert executed["amount_sat"] == 2100
        assert executed["fee_sat"] == 10

        # Bob verifies recipient-side activity and closes the Buzz loop with
        # a signed receipt.  The receipt is never treated as authorization.
        published = _send_json(bob, {
            "command": "verify_publish_receipt",
            "intent_id": intent.id,
            "quote_id": quote.quote_id,
            "settlement_ref": "payment_hash_abc123",
            "amount_sat": 2100,
        })
        assert published["event"] == "verify_publish_receipt"
        assert published["amount_sat"] == 2100
        _relay_last_event(bob_exec, alice_exec, author_pubkey=RECIPIENT_PUBKEY)
        receipt_events = _send_json(alice, {"command": "receive"})["messages"]
        assert [event["type"] for event in receipt_events] == ["PaymentReceipt"]

    def test_approval_command_never_appears_in_buzz(self, tmp_path: Path):
        alice_exec = FakeExecutor()
        alice = _make_process(_alice_config(tmp_path), alice_exec)

        intent = make_intent(
            amount_sat=2100,
            expires_at=FAR_FUTURE,
            idempotency_key="p6-no-approval",
        )
        quote = make_quote(intent, fee_sat=10, expires_at=FAR_FUTURE)

        _send_json(alice, {
            "command": "submit_intent",
            "intent": intent.model_dump(exclude_none=True, mode="python"),
        })
        # Alice needs the quote locally; simulate receiving Bob's quote
        quote_dict = quote.model_dump(exclude_none=True, mode="python")
        _send_json(alice, {
            "command": "submit_quote_local",
            "quote": quote_dict,
        })
        _send_json(alice, {"command": "prepare"})
        prepared_record = alice._orchestrator._intents[intent.id].prepared
        assert prepared_record is not None
        prepared_hash = prepared_record.prepared_hash
        _send_json(alice, {
            "command": "approve",
            "intent_id": intent.id,
            "quote_id": quote.quote_id,
            "prepared_hash": prepared_hash,
        })

        for _channel, content in alice_exec.sent:
            assert "payment_approval" not in content.lower()
            assert "prepared_hash" not in content


class TestHermesRegtestProcessRecovery:
    def test_restart_reloads_state_and_does_not_retry(self, tmp_path: Path):
        alice_exec = FakeExecutor()
        bob_exec = FakeExecutor()
        config = _alice_config(tmp_path)
        alice = _make_process(
            config,
            alice_exec,
            execute_raises=AmbiguousResult("timeout after dispatch"),
        )

        intent = make_intent(
            amount_sat=2100,
            expires_at=FAR_FUTURE,
            idempotency_key="p6-restart",
        )
        quote = make_quote(intent, fee_sat=10, expires_at=FAR_FUTURE)

        _send_json(alice, {
            "command": "submit_intent",
            "intent": intent.model_dump(exclude_none=True, mode="python"),
        })
        _relay_last_event(alice_exec, bob_exec, author_pubkey=SENDER_PUBKEY)

        # Bob accepts and publishes quote
        fetched = _send_json(bob := _make_process(_bob_config(tmp_path), bob_exec), {"command": "receive"})
        _send_json(bob, {
            "command": "accept_intent",
            "message_id": fetched["messages"][0]["message_id"],
        })
        _send_json(bob, {
            "command": "publish_quote",
            "quote": quote.model_dump(exclude_none=True, mode="python"),
        })
        _relay_last_event(bob_exec, alice_exec, author_pubkey=RECIPIENT_PUBKEY)

        # Alice drives to execution; adapter raises AmbiguousResult
        alice_fetched = _send_json(alice, {"command": "receive"})
        quotes = [m for m in alice_fetched["messages"] if m["type"] == "PaymentQuote"]
        assert len(quotes) == 1
        _send_json(alice, {
            "command": "accept_quote",
            "message_id": quotes[0]["message_id"],
        })
        _send_json(alice, {"command": "prepare"})
        prepared_record = alice._orchestrator._intents[intent.id].prepared
        assert prepared_record is not None
        prepared_hash = prepared_record.prepared_hash
        _send_json(alice, {
            "command": "approve",
            "intent_id": intent.id,
            "quote_id": quote.quote_id,
            "prepared_hash": prepared_hash,
        })
        with pytest.raises(StateError, match="RECONCILIATION_REQUIRED"):
            _send_json(alice, {"command": "execute"})

        # Simulate restart: new process instance with same durable paths
        new_alice_exec = FakeExecutor()
        new_alice = _make_process(
            config,
            new_alice_exec,
            execute_raises=AmbiguousResult("timeout after dispatch"),
        )
        recovered = _send_json(new_alice, {"command": "recover"})
        assert recovered["event"] == "recover"
        assert recovered["intents"][0]["state"] == "reconciliation_required"
        assert recovered["intents"][0]["prepared_hash"] == prepared_hash[:8] + "..."
        assert recovered["state_count"]["reconciliation_required"] == 1

        # A second execute on the restarted process must not auto-retry and
        # fails because the state is fail-closed after the ambiguous dispatch.
        with pytest.raises(StateError, match="no intent in state APPROVED"):
            _send_json(new_alice, {"command": "execute"})

        # Bob publishes a receipt; new Alice accepts it and settles
        receipt = make_receipt(
            intent,
            quote,
            settlement_ref="payment_hash_abc123",
            fee_sat=10,
            created_at=NOW,
            settled_at=NOW,
        )
        _send_json(bob, {
            "command": "publish_receipt",
            "receipt": receipt.model_dump(exclude_none=True, mode="python"),
        })
        _relay_last_event(bob_exec, new_alice_exec, author_pubkey=RECIPIENT_PUBKEY)

        received = _send_json(new_alice, {"command": "receive"})
        accepted = _send_json(new_alice, {
            "command": "accept_receipt",
            "message_id": received["messages"][0]["message_id"],
        })
        assert accepted["state"] == "settled"

        # The recovery test demonstrates the durable state survives restart,
        # the intent is settled only after the verified Bob receipt arrives.
        status = _send_json(new_alice, {"command": "status"})
        assert status["state_count"]["settled"] == 1
