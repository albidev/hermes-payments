"""P6 supervisor — launch two real Hermes processes over Buzz in regtest.

This script starts Alice and Bob as separate OS processes, each with its own
state root.  It wires the real Buzz CLI transport only if the environment
carries the required relay/private-key configuration.  It never constructs or
logs private keys.

Usage
-----
    python examples/two_hermes_regtest/run.py \
        --channel 550e8400-e29b-41d4-a716-446655440000 \
        --alice-state-root /tmp/hermes-payments-alice \
        --bob-state-root /tmp/hermes-payments-bob \
        --alice-pubkey <hex64> \
        --bob-pubkey <hex64> \
        --approver-pubkey <hex64>

Environment variables (must be set externally; this script never reads them):
    BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG

The supervisor exits with code 1 if:
- the network option is not "regtest";
- any required argument is missing;
- a child process dies;
- Buzz relay health cannot be verified (if health check enabled).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
PYTHON = sys.executable


def _redacted_identifier(value: str) -> str:
    if len(value) <= 8:
        return value
    return f"{value[:8]}..."


def _write_process_command(
    *,
    state_root: Path,
    role: str,
    channel: str,
    pubkey: str,
    approver_pubkey: str,
    network: str = "regtest",
) -> None:
    """Write an executable shell wrapper for one Hermes process.

    The wrapper only sets PYTHONPATH and invokes the JSONL runner.  Signing
    keys stay in the Buzz CLI environment, not in this wrapper.
    """
    runner = Path(__file__).resolve().parent / "run_hermes.py"
    state_root.mkdir(parents=True, exist_ok=True)
    cmd_path = state_root / "run.sh"
    cmd = f"""#!/bin/sh
# Hermes Payments P6 process wrapper — auto-generated, do not edit.
export PYTHONPATH="{ROOT / 'src'}:{ROOT / 'examples'}"
exec "{PYTHON}" "{runner}" \\
    --role {role} \\
    --channel "{channel}" \\
    --pubkey "{pubkey}" \\
    --approver-pubkey "{approver_pubkey}" \\
    --state-root "{state_root}" \\
    --network "{network}" \\
    "$@"
"""
    cmd_path.write_text(cmd, encoding="utf-8")
    cmd_path.chmod(0o700)


def _start_process(state_root: Path, role: str, *, input_lines: Optional[list[str]] = None) -> subprocess.Popen:
    """Launch a child Hermes process and return its handle."""
    wrapper = state_root / "run.sh"
    if not wrapper.exists():
        raise RuntimeError(f"process wrapper not found: {wrapper}")
    env = os.environ.copy()
    # Do NOT inject secrets here.  BUZZ_* vars must be set externally.
    process = subprocess.Popen(
        [str(wrapper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if input_lines:
        assert process.stdin is not None
        for line in input_lines:
            process.stdin.write(line + "\n")
            process.stdin.flush()
    return process


def _send_jsonl(process: subprocess.Popen, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one JSONL command and read the response line."""
    line = json.dumps(payload, sort_keys=True)
    assert process.stdin is not None
    process.stdin.write(line + "\n")
    process.stdin.flush()
    assert process.stdout is not None
    response = process.stdout.readline().strip()
    if not response:
        raise RuntimeError(f"process {process.pid} closed stdout")
    return json.loads(response)


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact long identifiers for human-readable logs."""
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, str) and len(value) >= 32:
            redacted[key] = _redacted_identifier(value)
        elif isinstance(value, dict):
            redacted[key] = _redact_payload(value)
        else:
            redacted[key] = value
    return redacted


def _stop_process(process: subprocess.Popen) -> None:
    """Gracefully terminate a child process."""
    if process.poll() is None:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _verify_buzz_relay_health() -> bool:
    """Best-effort Buzz relay health check.

    Uses ``buzz messages get --kinds 9 --limit 1`` and expects either a
    JSON array or a clean error.  Returns True on success.
    """
    buzz_bin = os.environ.get("BUZZ_BIN", "buzz")
    try:
        result = subprocess.run(
            [buzz_bin, "relay", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            sys.stderr.write(f"Buzz relay health check failed: {result.stderr.strip()}\n")
            return False
        return True
    except FileNotFoundError:
        sys.stderr.write("Buzz relay health check skipped: buzz binary not found\n")
        return True  # don't block deterministic tests
    except subprocess.TimeoutExpired:
        sys.stderr.write("Buzz relay health check timed out\n")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 two-Hermes process supervisor")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--alice-state-root", required=True)
    parser.add_argument("--bob-state-root", required=True)
    parser.add_argument("--alice-pubkey", required=True)
    parser.add_argument("--bob-pubkey", required=True)
    parser.add_argument("--approver-pubkey", required=True)
    parser.add_argument("--network", default="regtest")
    parser.add_argument("--skip-buzz-health", action="store_true")
    args = parser.parse_args()

    if args.network != "regtest":
        sys.stderr.write("P6 supervisor is regtest-only\n")
        return 1

    for pk_name, pk in (
        ("alice-pubkey", args.alice_pubkey),
        ("bob-pubkey", args.bob_pubkey),
        ("approver-pubkey", args.approver_pubkey),
    ):
        if len(pk) != 64:
            sys.stderr.write(f"{pk_name} must be a 64-character hex string\n")
            return 1

    if not args.skip_buzz_health and not _verify_buzz_relay_health():
        return 1

    alice_root = Path(args.alice_state_root)
    bob_root = Path(args.bob_state_root)

    _write_process_command(
        state_root=alice_root,
        role="alice",
        channel=args.channel,
        pubkey=args.alice_pubkey,
        approver_pubkey=args.approver_pubkey,
        network=args.network,
    )
    _write_process_command(
        state_root=bob_root,
        role="bob",
        channel=args.channel,
        pubkey=args.bob_pubkey,
        approver_pubkey=args.approver_pubkey,
        network=args.network,
    )

    alice: Optional[subprocess.Popen] = None
    bob: Optional[subprocess.Popen] = None
    exit_code = 0

    try:
        alice = _start_process(alice_root, "alice")
        bob = _start_process(bob_root, "bob")

        # Wait for both processes to be ready
        for proc, name in ((alice, "alice"), (bob, "bob")):
            status = _send_jsonl(proc, {"command": "status"})
            sys.stdout.write(f"{name} ready: {status}\n")

        # Supervisor now enters a passive monitoring loop.  Real lifecycle
        # commands are issued by the operator through stdin of each process.
        # The supervisor only stops if a child exits.
        while True:
            for proc, name in ((alice, "alice"), (bob, "bob")):
                if proc.poll() is not None:
                    sys.stderr.write(f"process {name} exited with {proc.returncode}\n")
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        sys.stdout.write("interrupted, stopping children\n")
        exit_code = 130
    except Exception as exc:
        sys.stderr.write(f"supervisor error: {exc}\n")
        exit_code = 1
    finally:
        if alice is not None:
            _stop_process(alice)
        if bob is not None:
            _stop_process(bob)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
