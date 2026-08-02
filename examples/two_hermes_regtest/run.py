"""P6 supervisor — launch two real Hermes processes over Buzz on test networks.

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

Environment variables (must be set externally; values are only inherited by
the child process that needs them):
    BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG
    BUZZ_ALICE_PRIVATE_KEY, BUZZ_BOB_PRIVATE_KEY
    BUZZ_ALICE_AUTH_TAG, BUZZ_BOB_AUTH_TAG

The supervisor exits with code 1 if:
- the network option is not "regtest" or explicitly selected "signet";
- any required argument is missing;
- a child process dies;
- Buzz relay health cannot be verified (if health check enabled).
"""
from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

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
    buzz_bin: Optional[str] = None,
    wave_rpc_server: Optional[str] = None,
) -> None:
    """Write an executable shell wrapper for one Hermes process.

    The wrapper only sets PYTHONPATH and invokes the JSONL runner.  Signing
    keys stay in the Buzz CLI environment, not in this wrapper.
    """
    runner = Path(__file__).resolve().parent / "run_hermes.py"
    state_root.mkdir(parents=True, exist_ok=True)
    cmd_path = state_root / "run.sh"
    role_prefix = role.upper()
    credential_forwarding = (
        f'if [ -n "${{BUZZ_{role_prefix}_PRIVATE_KEY+x}}" ]; then\n'
        f'    export BUZZ_PRIVATE_KEY="${{BUZZ_{role_prefix}_PRIVATE_KEY}}"\n'
        "fi\n"
        f'if [ -n "${{BUZZ_{role_prefix}_AUTH_TAG+x}}" ]; then\n'
        f'    export BUZZ_AUTH_TAG="${{BUZZ_{role_prefix}_AUTH_TAG}}"\n'
        "fi\n"
    )
    buzz_option = (
        f"    --buzz-bin {shlex.quote(buzz_bin)} \\\n"
        if buzz_bin
        else ""
    )
    wave_option = (
        f"    --wave-rpc-server {shlex.quote(wave_rpc_server)} \\\n"
        if wave_rpc_server
        else ""
    )
    cmd = f"""#!/bin/sh
# Hermes Payments P6 process wrapper — auto-generated, do not edit.
export PYTHONPATH="{ROOT}:{ROOT / 'src'}:{ROOT / 'examples'}"
{credential_forwarding}exec {shlex.quote(PYTHON)} {shlex.quote(str(runner))} \\
    --role {shlex.quote(role)} \\
    --channel "{channel}" \\
    --pubkey "{pubkey}" \\
    --approver-pubkey "{approver_pubkey}" \\
    --state-root "{state_root}" \\
    --network "{network}" \\
{buzz_option}{wave_option}    "$@"
"""
    cmd_path.write_text(cmd, encoding="utf-8")
    cmd_path.chmod(0o700)


def _start_process(state_root: Path, role: str, *, input_lines: Optional[list[str]] = None) -> subprocess.Popen:
    """Launch a child Hermes process and return its handle."""
    wrapper = state_root / "run.sh"
    if not wrapper.exists():
        raise RuntimeError(f"process wrapper not found: {wrapper}")
    env = os.environ.copy()
    # Do NOT inject or inspect secret values here.  The generated wrapper
    # selects role-specific variable names before starting the child.
    process = subprocess.Popen(
        [str(wrapper)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    # run_hermes emits exactly one ready event before accepting commands.
    # Consume it here so the first operator command receives its own response
    # instead of accidentally reading the readiness banner.
    assert process.stdout is not None
    ready_line = process.stdout.readline().strip()
    if not ready_line:
        _stop_process(process)
        raise RuntimeError(f"process {role} closed stdout before ready")
    try:
        ready = json.loads(ready_line)
    except json.JSONDecodeError as exc:
        _stop_process(process)
        raise RuntimeError(f"process {role} emitted invalid ready JSON") from exc
    if ready.get("event") != "ready" or ready.get("role") != role:
        _stop_process(process)
        raise RuntimeError(f"process {role} emitted unexpected ready event")

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


_REDACT_IDENTIFIER_KEYS = frozenset({
    "id",
    "intent_id",
    "quote_id",
    "message_id",
    "prepared_hash",
    "settlement_ref",
    "payment_hash",
    "author",
    "pubkey",
    "channel",
    "state_root",
})


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact identifiers without destroying diagnostic error messages."""
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if (
            isinstance(value, str)
            and len(value) >= 32
            and key in _REDACT_IDENTIFIER_KEYS
        ):
            redacted[key] = _redacted_identifier(value)
        elif isinstance(value, dict):
            redacted[key] = _redact_payload(value)
        elif isinstance(value, list):
            redacted[key] = [
                _redact_payload(item) if isinstance(item, dict) else item
                for item in value
            ]
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
    """Verify a Buzz HTTP health probe without requiring signing credentials."""
    explicit = os.environ.get("BUZZ_HEALTH_URL")
    relay_url = explicit or os.environ.get("BUZZ_RELAY_URL", "http://127.0.0.1:3000")
    parsed = urlsplit(relay_url)
    if parsed.scheme in {"ws", "wss"}:
        parsed = parsed._replace(scheme="http" if parsed.scheme == "ws" else "https")
    base = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
    if explicit:
        urls = [relay_url]
    else:
        # Buzz exposes probes on the dedicated health listener (:8080 by
        # default), not on the Nostr/WebSocket listener (:3000).
        health_netloc = (
            f"[{parsed.hostname}]:8080"
            if parsed.hostname and ":" in parsed.hostname
            else f"{parsed.hostname}:8080"
            if parsed.hostname
            else parsed.netloc
        )
        health_base = urlunsplit(
            (parsed.scheme, health_netloc, "", "", "")
        ).rstrip("/")
        urls = [
            f"{health_base}/_readiness",
            f"{health_base}/_liveness",
            f"{base}/_readiness",
            f"{base}/_liveness",
        ]

    for url in urls:
        try:
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=5) as response:
                if 200 <= response.status < 300:
                    return True
        except Exception:
            continue

    sys.stderr.write("Buzz relay health check failed\n")
    return False


def _check_children(children: dict[str, subprocess.Popen]) -> None:
    """Raise if a child exited; the supervisor fails closed."""
    for name, process in children.items():
        if process.poll() is not None:
            raise RuntimeError(f"process {name} exited with {process.returncode}")


def _run_operator_loop(children: dict[str, subprocess.Popen]) -> int:
    """Forward redacted JSONL operator commands to one child process.

    Input format:
        {"target":"alice","command":"status"}

    The supervisor never accepts a payment approval implicitly.  ``approve``
    remains an explicit child command carrying the locally supplied binding.
    """
    while True:
        _check_children(children)
        readable, _, _ = select.select([sys.stdin], [], [], 0.25)
        if not readable:
            continue

        line = sys.stdin.readline()
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError("supervisor input must be JSON") from exc
        if not isinstance(command, dict):
            raise RuntimeError("supervisor input must be a JSON object")
        if command.get("command") == "shutdown":
            return 0

        target = command.pop("target", None)
        if target not in children:
            raise RuntimeError("supervisor command target must be alice or bob")
        response = _send_jsonl(children[target], command)
        sys.stdout.write(
            json.dumps(
                {"event": "response", "target": target, "response": _redact_payload(response)},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 two-Hermes process supervisor")
    parser.add_argument("--channel", required=True)
    parser.add_argument("--alice-state-root", required=True)
    parser.add_argument("--bob-state-root", required=True)
    parser.add_argument("--alice-pubkey", required=True)
    parser.add_argument("--bob-pubkey", required=True)
    parser.add_argument("--approver-pubkey", required=True)
    parser.add_argument("--network", default="regtest")
    parser.add_argument("--alice-wave-rpc-server", default=None)
    parser.add_argument("--bob-wave-rpc-server", default=None)
    parser.add_argument("--skip-buzz-health", action="store_true")
    parser.add_argument("--buzz-bin", default=None)
    args = parser.parse_args()

    if args.network not in {"regtest", "signet"}:
        sys.stderr.write("P6 supervisor supports regtest or explicitly selected signet\n")
        return 1

    if args.network == "signet" and (
        not args.alice_wave_rpc_server or not args.bob_wave_rpc_server
    ):
        sys.stderr.write(
            "signet requires --alice-wave-rpc-server and "
            "--bob-wave-rpc-server\n"
        )
        return 1

    for pk_name, pk in (
        ("alice-pubkey", args.alice_pubkey),
        ("bob-pubkey", args.bob_pubkey),
        ("approver-pubkey", args.approver_pubkey),
    ):
        if len(pk) != 64:
            sys.stderr.write(f"{pk_name} must be a 64-character hex string\n")
            return 1

    if args.alice_pubkey != args.bob_pubkey:
        missing_identity_env = [
            name
            for name in ("BUZZ_ALICE_PRIVATE_KEY", "BUZZ_BOB_PRIVATE_KEY")
            if name not in os.environ
        ]
        if missing_identity_env:
            sys.stderr.write(
                "distinct Hermes identities require external variables: "
                + ", ".join(missing_identity_env)
                + "\n"
            )
            return 1

    if not args.skip_buzz_health and not _verify_buzz_relay_health():
        return 1

    alice_wave_rpc = args.alice_wave_rpc_server or "localhost:10029"
    bob_wave_rpc = args.bob_wave_rpc_server or "localhost:10029"

    alice_root = Path(args.alice_state_root)
    bob_root = Path(args.bob_state_root)

    _write_process_command(
        state_root=alice_root,
        role="alice",
        channel=args.channel,
        pubkey=args.alice_pubkey,
        approver_pubkey=args.approver_pubkey,
        network=args.network,
        buzz_bin=args.buzz_bin,
        wave_rpc_server=alice_wave_rpc,
    )
    _write_process_command(
        state_root=bob_root,
        role="bob",
        channel=args.channel,
        pubkey=args.bob_pubkey,
        approver_pubkey=args.approver_pubkey,
        network=args.network,
        buzz_bin=args.buzz_bin,
        wave_rpc_server=bob_wave_rpc,
    )
    alice: Optional[subprocess.Popen] = None
    bob: Optional[subprocess.Popen] = None
    exit_code = 0

    try:
        alice = _start_process(alice_root, "alice")
        bob = _start_process(bob_root, "bob")

        # Verify both children answer a command after their readiness event.
        for proc, name in ((alice, "alice"), (bob, "bob")):
            status = _send_jsonl(proc, {"command": "status"})
            sys.stdout.write(
                json.dumps(
                    {
                        "event": "status",
                        "role": name,
                        "status": _redact_payload(status),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            sys.stdout.flush()

        return _run_operator_loop({"alice": alice, "bob": bob})
    except KeyboardInterrupt:
        sys.stderr.write("interrupted, stopping children\n")
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
