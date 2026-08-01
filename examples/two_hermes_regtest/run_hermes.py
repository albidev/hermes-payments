"""P6 per-process JSONL runner.

Reads JSONL commands from stdin and writes JSONL responses to stdout.  Each
process owns one state root, one audit log, one idempotency store, one Buzz
channel, and one Wavelength regtest adapter.

Environment variables (must be set externally):
    BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, BUZZ_AUTH_TAG

No secret is read, logged, or constructed here.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from examples.two_hermes_regtest.process import HermesRegtestProcess, ProcessConfig

# PYTHONPATH must include src/ and examples/
from hermes_payments.adapter import WavelengthAdapter
from hermes_payments.models import AgentIdentity
from hermes_payments.transport import BuzzTransport, SubprocessExecutor


def _build_transport(*, channel: str) -> BuzzTransport:
    """Build a real Buzz transport using the external Buzz CLI."""
    executor = SubprocessExecutor(
        buzz_bin=os.environ.get("BUZZ_BIN", "buzz"),
        timeout=30,
    )
    return BuzzTransport(executor=executor, channel=channel)


def _build_adapter() -> WavelengthAdapter:
    """Build a regtest Wavelength adapter.

    RPC server is taken from ``WAVE_RPC_SERVER`` env var; defaults to
    localhost:10029.  Credentials stay inside wavecli / the operator.
    """
    rpc_server = os.environ.get("WAVE_RPC_SERVER", "localhost:10029")
    return WavelengthAdapter(
        executor=None,  # type: ignore[arg-type]
        rpc_server=rpc_server,
        network="regtest",
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
    parser.add_argument("--buzz-bin", default=None)
    args = parser.parse_args()

    if args.network != "regtest":
        sys.stderr.write("regtest-only\n")
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

    transport = _build_transport(channel=args.channel)
    adapter = _build_adapter()

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
                {"event": "error", "error": str(exc)},
                sort_keys=True,
            )
        sys.stdout.write(response + "\n")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
