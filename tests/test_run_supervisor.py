"""P6 supervisor tests — no live subprocess, no real Buzz, no Wavelength.

The tests exercise argument validation, process-wrapper generation, and the
supervisor's HTTP health-check seam with mocked responses.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from examples.two_hermes_regtest import run, run_hermes
from examples.two_hermes_regtest.run import (
    _redact_payload,
    _redacted_identifier,
    _verify_buzz_relay_health,
    _write_process_command,
    main,
)

CHANNEL = "550e8400-e29b-41d4-a716-446655440000"
VALID_KEY = "aa" * 32
VALID_APPROVER = "cc" * 32


def test_redacted_identifier_short_value_is_unchanged():
    assert _redacted_identifier("short") == "short"


def test_redacted_identifier_long_value_is_redacted():
    val = "a" * 64
    redacted = _redacted_identifier(val)
    assert redacted == "aaaaaaaa..."
    assert val not in redacted


def test_redact_payload_preserves_long_error_diagnostics():
    payload = {
        "error": "adapter prepare failed: wavecli command failed with status 1: rpc endpoint refused"
    }
    redacted = _redact_payload(payload)
    assert "wavecli command failed" in redacted["error"]
    assert "rpc endpoint refused" in redacted["error"]


def test_write_process_command_creates_executable_wrapper(tmp_path: Path):
    state_root = tmp_path / "alice"
    _write_process_command(
        state_root=state_root,
        role="alice",
        channel=CHANNEL,
        pubkey=VALID_KEY,
        approver_pubkey=VALID_APPROVER,
        buzz_bin="/opt/buzz/bin/buzz",
        network="signet",
        wave_rpc_server="localhost:11329",
    )

    wrapper = state_root / "run.sh"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    content = wrapper.read_text()
    assert "run_hermes.py" in content
    assert "--role alice" in content
    assert CHANNEL in content
    assert VALID_KEY in content
    assert VALID_APPROVER in content
    assert "--buzz-bin /opt/buzz/bin/buzz" in content
    assert '--network "signet"' in content
    assert "--wave-rpc-server localhost:11329" in content
    assert "BUZZ_ALICE_PRIVATE_KEY" in content
    assert "BUZZ_ALICE_AUTH_TAG" in content
    assert "signet" in content


def test_main_rejects_non_regtest(capsys):
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


def test_child_rejects_signet_without_explicit_rpc_server():
    with mock_patch("sys.argv", [
        "run_hermes.py",
        "--role", "alice",
        "--channel", CHANNEL,
        "--pubkey", VALID_KEY,
        "--approver-pubkey", VALID_APPROVER,
        "--state-root", "/tmp/hermes-p6-test",
        "--network", "signet",
    ]):
        assert run_hermes.main() == 1


def test_supervisor_rejects_invalid_pubkey_length():
    with mock_patch("sys.argv", [
        "run.py",
        "--channel", CHANNEL,
        "--alice-state-root", "/tmp/a",
        "--bob-state-root", "/tmp/b",
        "--alice-pubkey", "too-short",
        "--bob-pubkey", VALID_KEY,
        "--approver-pubkey", VALID_APPROVER,
        "--skip-buzz-health",
    ]):
        assert main() == 1


def test_verify_buzz_relay_health_fails_closed_when_probe_fails():
    with mock_patch.object(run, "urlopen", side_effect=OSError("unreachable")):
        assert _verify_buzz_relay_health() is False


def test_verify_buzz_relay_health_accepts_explicit_probe():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    with mock_patch.object(run, "urlopen", return_value=Response()):
        with mock_patch.dict(os.environ, {"BUZZ_HEALTH_URL": "http://health.test"}, clear=False):
            assert _verify_buzz_relay_health() is True


def test_verify_buzz_relay_health_false_on_failed_probe():
    with mock_patch.object(run, "urlopen", side_effect=OSError("relay unreachable")):
        assert _verify_buzz_relay_health() is False


def test_supervisor_starts_children_and_creates_wrappers(tmp_path: Path):
    alice_root = tmp_path / "alice"
    bob_root = tmp_path / "bob"

    def fake_popen(cmd, **kwargs):
        class FakeProcess:
            returncode = None
            pid = 1234

            def __init__(self):
                self.stdin = None
                role = Path(cmd[0]).parent.name
                self.stdout = io.StringIO(
                    json.dumps({"event": "ready", "role": role}) + "\n"
                )
                self.stderr = None

            def poll(self):
                return None

            def terminate(self):
                self.returncode = 0

            def kill(self):
                self.returncode = 0

            def wait(self, timeout=5):
                return self.returncode

        return FakeProcess()

    call_count = 0

    def fake_send_jsonl(process, payload):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise RuntimeError("loop exit")
        return {"role": payload.get("command", "unknown")}

    with mock_patch("sys.argv", [
        "run.py",
        "--channel", CHANNEL,
        "--alice-state-root", str(alice_root),
        "--bob-state-root", str(bob_root),
        "--alice-pubkey", VALID_KEY,
        "--bob-pubkey", VALID_KEY,
        "--approver-pubkey", VALID_APPROVER,
        "--skip-buzz-health",
    ]), mock_patch("subprocess.Popen", side_effect=fake_popen), mock_patch.object(
        run, "_send_jsonl", side_effect=fake_send_jsonl
    ):
        assert main() == 1

    assert (alice_root / "run.sh").exists()
    assert (bob_root / "run.sh").exists()
    assert call_count >= 2
