"""P6 per-process JSONL runner.

Reads JSONL commands from stdin and writes JSONL responses to stdout.  Each
process owns one state root, one audit log, one idempotency store, one Buzz
channel, and one Wavelength adapter on the explicitly selected test network.

Configuration supplied externally:
    BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG
    BUZZ_BIN (the Wavelength RPC endpoint is passed explicitly)

Per-role Buzz credentials are selected by the generated shell wrapper and
inherited by the corresponding child process; they are never written to the
wrapper itself.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from examples.two_hermes_regtest.process import HermesRegtestProcess, ProcessConfig

# PYTHONPATH must include the repository root and src/.
from hermes_payments.adapter import SubprocessWavecliExecutor, WavelengthAdapter, redact_sensitive
from hermes_payments.models import AgentIdentity
from hermes_payments.transport import BuzzTransport, SubprocessExecutor


def _build_transport(
    *,
    channel: str,
    pubkey: str,
    cursor_path: Path,
    buzz_bin: str,
) -> BuzzTransport:
    """Build a real Buzz transport using the external Buzz CLI."""
    executor = SubprocessExecutor(
        buzz_bin=buzz_bin,
        timeout=30,
    )
    return BuzzTransport(
        executor=executor,
        channel=channel,
        cursor_path=cursor_path,
        local_pubkey=pubkey,
    )


def _build_adapter(*, network: str, rpc_server: str | None) -> WavelengthAdapter:
    """Build a Wavelength adapter for an explicitly selected test network.

    Credentials stay inside wavecli / the operator.
    """
    return WavelengthAdapter(
        executor=SubprocessWavecliExecutor(),
        rpc_server=rpc_server or "localhost:10029",
        network=network,
        no_tls=True,
        no_macaroons=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 Hermes JSONL runner")
    parser.add_argument("--role", required=True, choices=["alice", "bob"])
    parser.add_argument("--channel", required=True)
    parser.add_argument("--pubkey", required=True)
    parser.add_argument("--approver-pubkey", required=True)
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--network", default="regtest")
    parser.add_argument("--wave-rpc-server", default=None)
    parser.add_argument("--buzz-bin", default=None)
    args = parser.parse_args()

    if args.network not in {"regtest", "signet"}:
        sys.stderr.write("only regtest or explicitly selected signet is supported\n")
        return 1
    if args.network == "signet" and not args.wave_rpc_server:
        sys.stderr.write("signet requires --wave-rpc-server\n")
        return 1

    state_root = Path(args.state_root)
    state_root.mkdir(parents=True, exist_ok=True)

    identity = AgentIdentity(pubkey=args.pubkey, relay_url=None)
    approver = AgentIdentity(pubkey=args.approver_pubkey, relay_url=None)

    config = ProcessConfig(
        role=args.role,
        identity=identity,
        channel=args.channel,
        state_root=state_root,
        network=args.network,
        approver=approver,
    )

    buzz_bin = args.buzz_bin or os.environ.get("BUZZ_BIN", "buzz")
    transport = _build_transport(
        channel=args.channel,
        pubkey=args.pubkey,
        cursor_path=state_root / "buzz_cursor.json",
        buzz_bin=buzz_bin,
    )
    adapter = _build_adapter(
        network=args.network,
        rpc_server=args.wave_rpc_server,
    )

    process = HermesRegtestProcess(
        config=config,
        adapter=adapter,
        transport=transport,
    )

    sys.stdout.write(json.dumps({"event": "ready", "role": args.role}) + "\n")
    sys.stdout.flush()

    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            response = process.handle_line(line)
        except Exception as exc:
            response = json.dumps(
                {"event": "error", "error": redact_sensitive(str(exc))},
                sort_keys=True,
            )
        sys.stdout.write(response + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
