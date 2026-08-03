"""P7 E2E role runner — one Hermes payment identity per process.

Reads a single identity's configuration from the environment and executes
payment commands from stdin, printing JSONL responses. One process = one
BUZZ_PRIVATE_KEY (the model matches P6 and the plugin's security boundary:
each Hermes instance owns exactly one Buzz identity).

Env for the role process:
    HP_ROLE            alice | bob
    HP_PUBKEY          this identity's 64-char hex pubkey
    HP_APPROVER_PUBKEY approval pubkey (same as HP_PUBKEY for this harness)
    HP_CHANNEL         Buzz channel UUID
    HP_STATE_ROOT      isolated state dir
    HP_NETWORK         signet
    HP_WAVE_RPC_SERVER this identity's Wavelength daemon (e.g. 127.0.0.1:11329)
    BUZZ_PRIVATE_KEY   this identity's private key (nsec or hex)

Commands on stdin (one per line):
    {"cmd":"pay","recipient_pubkey":"<hex>","amount_sat":2100,"purpose":"...",
     "max_fee_sat":10,"expires_at":<unix>,"idempotency_key":"..."}
    {"cmd":"poll"}
    {"cmd":"accept_quote","intent_id":"<full id>","invoice":"<bolt11>"}
    {"cmd":"prepare","quote_id":"<full quote_id>"}
    {"cmd":"execute","intent_id":"<full id>","prepared_hash":"<full hash>"}
    {"cmd":"status"}

The full identifiers are exchanged via JSONL (never redacted) because the two
role processes coordinate over the shared Buzz channel, not through stdout.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]  # file is at examples/p7_plugin/, parents[2]=hermes-payments
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_PLUGIN_INIT = _REPO_ROOT / "plugins" / "hermes-payments" / "__init__.py"
_spec = importlib.util.spec_from_file_location("hermes_payments_plugin", _PLUGIN_INIT)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["hermes_payments_plugin"] = _module
_spec.loader.exec_module(_module)
PaymentService = _module.PaymentService


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.stderr.write(f"missing {name}\n")
        sys.exit(2)
    return val


def _file_approval_gate(**request) -> bool:
    """Wait for an operator-written, exact one-shot approval artifact."""
    approval_name = os.environ.get("HP_APPROVAL_FILE", "").strip()
    if not approval_name:
        return False

    approval_path = Path(approval_name)
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    request_path = approval_path.with_name(approval_path.name + ".request.json")
    request_path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")

    try:
        timeout = max(1.0, float(os.environ.get("HP_APPROVAL_TIMEOUT", "300")))
    except ValueError:
        timeout = 300.0
    deadline = time.monotonic() + timeout
    required = ("intent_id", "quote_id", "prepared_hash")

    while time.monotonic() < deadline:
        try:
            decision = json.loads(approval_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            time.sleep(0.1)
            continue

        matches = all(decision.get(key) == request.get(key) for key in required)
        choice = decision.get("choice")
        try:
            approval_path.unlink()
        except FileNotFoundError:
            pass
        return choice == "once" and matches

    return False


def main() -> int:
    role = _require_env("HP_ROLE")
    pubkey = _require_env("HP_PUBKEY")
    approver = _require_env("HP_APPROVER_PUBKEY")
    channel = _require_env("HP_CHANNEL")
    state_root = _require_env("HP_STATE_ROOT")
    network = os.environ.get("HP_NETWORK", "regtest")
    wave_rpc = _require_env("HP_WAVE_RPC_SERVER")
    buzz_key = _require_env("BUZZ_PRIVATE_KEY")

    env = {
        "HP_ROLE": role,
        "HP_PUBKEY": pubkey,
        "HP_APPROVER_PUBKEY": approver,
        "HP_CHANNEL": channel,
        "HP_STATE_ROOT": state_root,
        "HP_NETWORK": network,
        "HP_WAVE_RPC_SERVER": wave_rpc,
        "BUZZ_RELAY_URL": os.environ.get("BUZZ_RELAY_URL", "wss://albi-lab.communities.buzz.xyz"),
        "BUZZ_PRIVATE_KEY": buzz_key,
        # Explicit buzz CLI path + PATH so the transport's subprocess can find it.
        "BUZZ_BIN": os.environ.get("BUZZ_BIN", "/Users/albi/.local/bin/buzz"),
    }
    # The Buzz CLI reads BUZZ_PRIVATE_KEY / BUZZ_RELAY_URL from the process
    # environment when it spawns. Set them globally so the transport's
    # subprocess inherits this identity's key.
    os.environ["BUZZ_PRIVATE_KEY"] = buzz_key
    os.environ["BUZZ_RELAY_URL"] = env["BUZZ_RELAY_URL"]
    os.environ["BUZZ_BIN"] = env["BUZZ_BIN"]
    # Ensure the buzz CLI directory is on PATH for the spawned subprocess.
    _buzz_dir = str(Path(env["BUZZ_BIN"]).resolve().parent)
    os.environ["PATH"] = _buzz_dir + ":" + os.environ.get("PATH", "")
    svc = PaymentService.from_env(
        env,
        approval_gate=_file_approval_gate if os.environ.get("HP_APPROVAL_FILE") else None,
    )

    # ready handshake
    sys.stdout.write(json.dumps({"event": "ready", "role": role}) + "\n")
    sys.stdout.flush()

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
            name = cmd.get("cmd")
            result = _dispatch(svc, name, cmd)
        except Exception as exc:  # noqa: BLE001
            result = {"error": _module.redact_sensitive(str(exc))}
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        sys.stdout.flush()
    return 0


def _dispatch(svc: PaymentService, name: str, cmd: dict):
    if name == "pay":
        return svc.pay(
            recipient_pubkey=cmd["recipient_pubkey"],
            amount_sat=int(cmd["amount_sat"]),
            purpose=cmd["purpose"],
            max_fee_sat=int(cmd.get("max_fee_sat", 0)),
            expires_at=int(cmd["expires_at"]),
            idempotency_key=cmd.get("idempotency_key", ""),
        )
    if name == "poll":
        return {"received": svc.poll()}
    if name == "accept_quote":
        return svc.accept_and_quote(intent_id=cmd["intent_id"], invoice=cmd["invoice"])
    if name == "prepare":
        return svc.prepare(quote_id=cmd["quote_id"])
    if name == "execute":
        return svc.execute(
            intent_id=cmd["intent_id"],
            prepared_hash=cmd["prepared_hash"],
        )
    if name == "status":
        return {"intents": svc.status()}
    if name == "reconcile":
        return svc.reconcile(intent_id=cmd["intent_id"])
    if name == "receive":
        return svc.receive(
            intent_id=cmd["intent_id"],
            settlement_ref=cmd["settlement_ref"],
        )
    return {"error": f"unknown cmd: {name}"}


if __name__ == "__main__":
    sys.exit(main())
