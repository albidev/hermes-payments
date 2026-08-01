"""P6 supervisor tests — no live subprocess, no real Buzz, no Wavelength.

The tests exercise argument validation, process-wrapper generation, and the
supervisor's health-check seam with mocked subprocess responses.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch as mock_patch

import pytest

from examples.two_hermes_regtest import run
from examples.two_hermes_regtest.run import (
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


def test_write_process_command_creates_executable_wrapper(tmp_path: Path):
    state_root = tmp_path / "alice"
    _write_process_command(
        state_root=state_root,
        role="alice",
        channel=CHANNEL,
        pubkey=VALID_KEY,
        approver_pubkey=VALID_APPROVER,
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
    assert "regtest" in content


def test_main_rejects_non_regtest(capsys):
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


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


def test_verify_buzz_relay_health_true_on_missing_binary():
    env = os.environ.copy()
    env["BUZZ_BIN"] = "definitely-not-a-real-buzz-binary-xyz"
    with mock_patch.dict(os.environ, env, clear=True):
        assert _verify_buzz_relay_health() is True


def test_verify_buzz_relay_health_false_on_failed_relay_info():
    def failing_run(*_args, **_kwargs):
        class Result:
            returncode = 1
            stderr = "relay unreachable"
        return Result()

    with mock_patch("subprocess.run", side_effect=failing_run):
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
                self.stdout = None
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
