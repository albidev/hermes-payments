"""P7 E2E orchestrator — two-identity Hermes payment flow on Signet.

Launches two ``hp_role.py`` subprocesses (alice, bob), each with its own
BUZZ_PRIVATE_KEY and Wavelength daemon, then drives the full plugin flow over
the real hosted Buzz relay:

    alice.pay -> bob.poll+accept_quote -> alice.poll+prepare
             -> alice.prepare -> alice.execute() -> local human approval

This matches the P6 model (one process per identity) and the plugin security
boundary: each Hermes instance owns exactly one Buzz identity, and approval is
an explicit local step bound to the exact prepared hash.

Reads the shared env file for BUZZ_ALICE_PRIVATE_KEY / BUZZ_BOB_PRIVATE_KEY
and the channel. No secrets are printed.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ALICE_PK = "cb4d650fb43711a4d7cd67dcadb05ccadbdeeaad27a81f539926d6f51c357e2b"
BOB_PK = "be78d419c53f3c3eeb6670b84d98bd2306215fd2bae438a1a0b2e6abb44f486f"

_REPO_ROOT = Path("/Users/albi/Projects/hermes-payments")  # absolute; subprocess-safe
ROLE_RUNNER = _REPO_ROOT / "examples" / "p7_plugin" / "hp_role.py"
# Fresh state root per run so the Buzz cursor starts clean (a reused cursor
# skips already-seen event ids, starving new messages on the hosted relay).
STATE_ROOT = _REPO_ROOT / "p7-e2e-state" / f"run-{int(time.time())}"
CHANNEL = "14df4f9b-cf92-4026-b057-45f7d31fd5b4"


class Role:
    def __init__(self, name: str, env: dict):
        self.name = name
        self.env = env
        self.proc = subprocess.Popen(
            [sys.executable, str(ROLE_RUNNER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        # wait for ready
        self._read_line()

    def _read_line(self, timeout: float = 30) -> dict:
        import select
        if sys.platform != "darwin":
            raise RuntimeError("select-based read not implemented")
        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError(f"{self.name} no response in {timeout}s")
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            raise RuntimeError(f"{self.name} exited: {err[-800:]}")
        return json.loads(line)

    def cmd(self, payload: dict, timeout: float = 60) -> dict:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return self._read_line(timeout)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def load_env() -> dict:
    envfile = Path(os.path.expanduser("~/.hermes/state/p7-e2e.env"))
    out = {}
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def role_env(base: dict, *, role: str, pubkey: str, wave_rpc: str) -> dict:
    env = {
        name: os.environ[name]
        for name in ("HOME", "PATH", "TMPDIR")
        if os.environ.get(name)
    }
    env.update(base)
    key = "BUZZ_ALICE_PRIVATE_KEY" if role == "alice" else "BUZZ_BOB_PRIVATE_KEY"
    auth_tag = "BUZZ_ALICE_AUTH_TAG" if role == "alice" else "BUZZ_BOB_AUTH_TAG"
    env["HP_ROLE"] = role
    env["HP_PUBKEY"] = pubkey
    env["HP_APPROVER_PUBKEY"] = pubkey
    env["HP_CHANNEL"] = CHANNEL
    env["HP_STATE_ROOT"] = str(STATE_ROOT / role)
    env["HP_NETWORK"] = "signet"
    env["HP_WAVE_RPC_SERVER"] = wave_rpc
    env["HP_APPROVAL_FILE"] = str(STATE_ROOT / role / "operator-approval.json")
    env["BUZZ_PRIVATE_KEY"] = env[key]
    env["BUZZ_AUTH_TAG"] = env.get(auth_tag, "")
    return env


def _write_operator_approval(*, intent_id: str, quote_id: str, prepared_hash: str) -> None:
    path = STATE_ROOT / "alice" / "operator-approval.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "choice": "once",
                "intent_id": intent_id,
                "quote_id": quote_id,
                "prepared_hash": prepared_hash,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--approve-live",
        action="store_true",
        help="write one exact local approval and allow the 2100-sat send",
    )
    args = parser.parse_args()

    base = load_env()
    if not base.get("BUZZ_ALICE_PRIVATE_KEY") or not base.get("BUZZ_BOB_PRIVATE_KEY"):
        print("missing keys in ~/.hermes/state/p7-e2e.env", file=sys.stderr)
        return 2

    alice = Role("alice", role_env(base, role="alice", pubkey=ALICE_PK, wave_rpc="127.0.0.1:11329"))
    bob = Role("bob", role_env(base, role="bob", pubkey=BOB_PK, wave_rpc="127.0.0.1:11339"))
    print("=== P7 two-process Signet E2E (real Buzz relay) ===")

    try:
        # 1. Alice submits intent
        pay = alice.cmd({
            "cmd": "pay",
            "recipient_pubkey": BOB_PK,
            "amount_sat": 2100,
            "purpose": "P7 plugin two-process live test",
            "max_fee_sat": 10,
            "expires_at": int(time.time()) + 7200,
            "idempotency_key": f"p7-{int(time.time())}",
        })
        intent_id = pay.get("full_intent_id")
        print(f"[alice] intent submitted: state={pay.get('state')} id={str(intent_id)[:10]}...")
        if not intent_id:
            print("[alice] NO full_intent_id:", pay)
            return 3

        # 2. Bob polls until he receives the intent (hosted relay has latency)
        invoice = _make_invoice(base, "127.0.0.1:11339")
        if not invoice:
            print("invoice generation failed", file=sys.stderr)
            return 3
        quote = _retry_quote(bob, intent_id, invoice)
        print(f"[bob]   quote published: state={quote.get('state')} q={str(quote.get('quote_id'))[:12]}...")
        quote_id = quote.get("full_quote_id")
        if not quote_id:
            print("[bob] NO quote_id:", quote)
            return 3

        # 4. Alice polls until she has the quote, then prepares
        prep = _retry_prepare(alice, quote_id)
        print(f"[alice] prepared: state={prep.get('state')} fee={prep.get('fee_sat')}")
        full_hash = prep.get("full_prepared_hash")
        if not full_hash:
            print("[alice] NO prepared hash:", prep)
            return 3

        if not args.approve_live:
            print(
                "[operator] approval required: rerun with --approve-live "
                "after reviewing this exact prepared payment; no money moved"
            )
            return 5
        _write_operator_approval(
            intent_id=intent_id,
            quote_id=quote_id,
            prepared_hash=full_hash,
        )
        exec_res = alice.cmd(
            {"cmd": "execute", "intent_id": intent_id, "prepared_hash": full_hash}
        )
        print(f"[alice] execute: {exec_res.get('error', exec_res.get('state'))}")

        # 6. Final status
        st = alice.cmd({"cmd": "status"})
        print("[alice] status:", json.dumps(st, indent=2))
        return 0 if exec_res.get("state") == "settled" else 4
    finally:
        alice.close()
        bob.close()


def _retry_quote(bob: "Role", intent_id: str, invoice: str, timeout_s: float = 150) -> dict:
    """Poll until Bob receives the intent, then accept+quote."""
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        bob.cmd({"cmd": "poll"})
        last = bob.cmd({"cmd": "accept_quote", "intent_id": intent_id, "invoice": invoice})
        if last.get("quote_id"):
            return last
        time.sleep(2)
    print("[bob]   quote retry exhausted:", last, file=sys.stderr)
    return last


def _retry_prepare(alice: "Role", quote_id: str, timeout_s: float = 150) -> dict:
    """Poll until Alice has the quote, then prepare."""
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        alice.cmd({"cmd": "poll"})
        last = alice.cmd({"cmd": "prepare", "quote_id": quote_id})
        if last.get("full_prepared_hash"):
            return last
        time.sleep(2)
    print("[alice] prepare retry exhausted:", last, file=sys.stderr)
    return last


def _make_invoice(base: dict, wave_rpc: str, amount: int = 2100) -> str:
    import json as _json
    import subprocess as _sp
    wavecli = "/tmp/wavecli-hermes-testnet"
    cmd = [wavecli, f"--rpcserver={wave_rpc}", "--no-tls", "--no-macaroons",
           "recv", "--offchain", "--amt", str(amount), "--memo", "p7-test"]
    try:
        p = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.PIPE, text=True)
        try:
            out, _ = p.communicate(timeout=15)
        except _sp.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        return _json.loads(out).get("invoice", "")
    except Exception as e:
        print(f"recv failed: {e}", file=sys.stderr)
        return ""


if __name__ == "__main__":
    sys.exit(main())
