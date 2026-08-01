"""P6 process boundary contract tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.two_hermes_regtest.process import (
    JsonlCommand,
    ProcessConfig,
    ProcessProtocolError,
    redacted_identifier,
)
from tests.fixtures import RECIPIENT_IDENTITY

CHANNEL = "550e8400-e29b-41d4-a716-446655440000"


def test_process_config_is_explicit_and_regtest_only(tmp_path: Path):
    config = ProcessConfig(
        role="bob",
        identity=RECIPIENT_IDENTITY,
        channel=CHANNEL,
        state_root=tmp_path / "bob",
        network="regtest",
    )

    assert config.role == "bob"
    assert config.channel == CHANNEL
    assert config.network == "regtest"
    assert config.state_root == tmp_path / "bob"

    with pytest.raises(ValueError, match="regtest"):
        ProcessConfig(
            role="bob",
            identity=RECIPIENT_IDENTITY,
            channel=CHANNEL,
            state_root=tmp_path / "signet",
            network="signet",
        )


def test_process_config_rejects_missing_channel_and_state_root(tmp_path: Path):
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


def test_jsonl_command_accepts_known_commands_and_rejects_unknown():
    command = JsonlCommand.parse('{"command":"receive","limit":10}')

    assert command.name == "receive"
    assert command.arguments == {"limit": 10}

    with pytest.raises(ProcessProtocolError, match="unknown command"):
        JsonlCommand.parse('{"command":"send_money","amount_sat":1}')

    with pytest.raises(ProcessProtocolError, match="valid JSON object"):
        JsonlCommand.parse("not-json")


def test_jsonl_process_reports_redacted_status(tmp_path: Path):
    from examples.two_hermes_regtest.process import JsonlProcess

    config = ProcessConfig(
        role="bob",
        identity=RECIPIENT_IDENTITY,
        channel=CHANNEL,
        state_root=tmp_path / "bob",
        network="regtest",
    )

    response = json.loads(JsonlProcess(config).handle_line('{"command":"status"}'))

    assert response["event"] == "status"
    assert response["role"] == "bob"
    assert response["network"] == "regtest"
    assert response["channel"] == CHANNEL
    assert response["state_root"] == str(tmp_path / "bob")


def test_jsonl_process_rejects_receive_without_transport(tmp_path: Path):
    from examples.two_hermes_regtest.process import JsonlProcess

    config = ProcessConfig(
        role="bob",
        identity=RECIPIENT_IDENTITY,
        channel=CHANNEL,
        state_root=tmp_path / "bob",
        network="regtest",
    )

    with pytest.raises(ProcessProtocolError, match="receive handler"):
        JsonlProcess(config).handle_line('{"command":"receive"}')
def test_redacted_identifier_never_emits_full_value():
    identifier = "a" * 64

    redacted = redacted_identifier(identifier)

    assert redacted == "aaaaaaaa..."
    assert identifier not in redacted


def test_jsonl_command_output_is_machine_readable():
    command = JsonlCommand.parse(json.dumps({"command": "status"}))

    assert json.loads(command.to_json()) == {"command": "status"}
