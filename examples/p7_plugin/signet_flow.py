"""P7 E2E harness — run the Hermes Payments plugin flow on Signet.

Cables two in-process ``PaymentService`` instances (alice, bob) using the real
Buzz hosted relay and the real Wavelength daemons on Signet, then drives the
full flow through the same functions the plugin tools expose:

    alice.pay -> bob.poll -> bob.accept_and_quote -> alice.poll
             -> alice.prepare -> alice.execute(approve=True) -> settled

The flow requires the Alice/Bob private keys via environment variables, exactly
like the P6 runner. Keys are never stored or transmitted — they are read from
the invoking shell only.

Required env (set these in the shell that runs this script):
    BUZZ_ALICE_PRIVATE_KEY, BUZZ_ALICE_AUTH_TAG
    BUZZ_BOB_PRIVATE_KEY,   BUZZ_BOB_AUTH_TAG
    BUZZ_RELAY_URL          (default wss://albi-lab.communities.buzz.xyz)
    HP_CHANNEL              (Buzz channel UUID)
    HP_STATE_ROOT           (isolated state dir; default ./p7-state)
    HP_NETWORK=signet
    HP_WAVE_RPC_ALICE, HP_WAVE_RPC_BOB (default localhost:11329 / 11339)
    HP_TEST_INVOICE         (a real bolt11 invoice from Bob's Wavelength wallet)

Usage:
    HP_NETWORK=signet ... python examples/p7_plugin/signet_flow.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

# PYTHONPATH must include repo root and src/.
from hermes_payments.models import AgentIdentity

# The plugin dir has a hyphen (required by Hermes discovery) so it is not a
# valid Python module name — load it under an alias.
_PLUGIN_INIT = Path(__file__).resolve().parent.parents[1] / "plugins" / "hermes-payments" / "__init__.py"
_spec = importlib.util.spec_from_file_location("hermes_payments_plugin", _PLUGIN_INIT)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
sys.modules["hermes_payments_plugin"] = _module
_spec.loader.exec_module(_module)
PaymentService = _module.PaymentService
redact_sensitive = _module.redact_sensitive

ALICE_PK = "c55bd0f67c422e60bf9cd292d6c288795373c519e4b251946277ef0bc474d230"
BOB_PK = "8ad4f9b40038585c958ddec505bdbbdc5adea57fdac1c55a4ff0470048d25d41"


def _build_service(role: str, *, pubkey: str, wave_rpc: str, state_root: Path, channel: str) -> PaymentService:
    # Build via from_env with a synthetic env so wiring matches production.
    env = {
        "HP_ROLE": role,
        "HP_PUBKEY": pubkey,
        "HP_APPROVER_PUBKEY": pubkey,
        "HP_CHANNEL": channel,
        "HP_STATE_ROOT": str(state_root),
        "HP_NETWORK": os.environ.get("HP_NETWORK", "signet"),
        "HP_WAVE_RPC_SERVER": wave_rpc,
        "BUZZ_RELAY_URL": os.environ.get("BUZZ_RELAY_URL", "wss://albi-lab.communities.buzz.xyz"),
        # BuzzTransport reads the single private key from BUZZ_PRIVATE_KEY;
        # the role-specific mapping must be exported by the caller.
        "BUZZ_PRIVATE_KEY": os.environ.get(
            "BUZZ_ALICE_PRIVATE_KEY" if role == "alice" else "BUZZ_BOB_PRIVATE_KEY", ""
        ),
    }
    return PaymentService.from_env(env)


def _poll_until(svc: PaymentService, pred, timeout_s: float = 30.0, label: str = "message") -> list:
    """Poll the transport until pred(inbox) is true or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        svc.poll()
        if pred(svc):
            return svc._inbox
        time.sleep(0.5)
    raise TimeoutError(f"timed out waiting for {label}")


def main() -> int:
    root = Path(__file__).resolve().parent
    repo_root = root.parents[1]
    sys.path.insert(0, str(repo_root))
    sys.path.insert(0, str(repo_root / "src"))

    channel = os.environ.get("HP_CHANNEL", "")
    state_root = Path(os.environ.get("HP_STATE_ROOT", str(repo_root / "p7-state")))
    wave_alice = os.environ.get("HP_WAVE_RPC_ALICE", "localhost:11329")
    wave_bob = os.environ.get("HP_WAVE_RPC_BOB", "localhost:11339")

    if not channel:
        print("HP_CHANNEL is required", file=sys.stderr)
        return 1

    # Precondition: private keys present in the invoking shell.
    for var in ("BUZZ_ALICE_PRIVATE_KEY", "BUZZ_BOB_PRIVATE_KEY"):
        if var not in os.environ:
            print(f"missing {var} in environment", file=sys.stderr)
            return 1

    alice = _build_service("alice", pubkey=ALICE_PK, wave_rpc=wave_alice,
                           state_root=state_root / "alice", channel=channel)
    bob = _build_service("bob", pubkey=BOB_PK, wave_rpc=wave_bob,
                         state_root=state_root / "bob", channel=channel)

    print("=== P7 plugin Signet E2E ===")
    print(f"channel: {channel}  network: {os.environ.get('HP_NETWORK','signet')}")

    # 1. Alice submits an intent
    amount = 2100
    pay = alice.pay(
        recipient_pubkey=BOB_PK,
        amount_sat=amount,
        purpose="P7 plugin live test",
        max_fee_sat=10,
        expires_at=int(time.time()) + 7200,
        idempotency_key=f"p7-{int(time.time())}",
    )
    intent_id = pay["full_intent_id"]
    print(f"[alice] intent submitted: {pay['state']} id={pay['intent_id']}")

    # 2. Bob receives and quotes
    _poll_until(bob, lambda s: any(
        m.message.__class__.__name__ == "PaymentIntent" for m in s._inbox
    ), label="intent")
    print(f"[bob]   intent received (inbox={len(bob._inbox)})")

    # Bob's quote must carry a real bolt11 invoice. The plugin never mints an
    # invoice; the operator supplies one from Bob's Wavelength wallet via env.
    invoice = os.environ.get("HP_TEST_INVOICE", "")
    if not invoice.startswith("ln"):
        print("HP_TEST_INVOICE (a real bolt11 from Bob's wallet) is required", file=sys.stderr)
        return 3
    quote = bob.accept_and_quote(intent_id=intent_id, invoice=invoice)
    print(f"[bob]   quote published: {quote['state']} q={quote['quote_id']}")

    # 3. Alice receives the quote
    _poll_until(alice, lambda s: any(
        m.message.__class__.__name__ == "PaymentQuote" for m in s._inbox
    ), label="quote")
    quote_id = next(
        m.message.quote_id for m in alice._inbox
        if m.message.__class__.__name__ == "PaymentQuote"
    )
    print(f"[alice] quote received: q={quote_id[:16]}...")

    # 4. Alice prepares (dry run) -> returns full hash
    prep = alice.prepare(quote_id=quote_id)
    print(f"[alice] prepared: fee={prep['fee_sat']} state={prep['state']}")
    full_hash = prep["full_prepared_hash"]

    # 5. Execute WITHOUT approval must fail closed
    no_approve = alice.execute(intent_id=intent_id, prepared_hash=full_hash, approve=False)
    print(f"[alice] execute w/o approval: {no_approve.get('error','OK')}")

    # 6. Approve + execute -> settled
    exec_res = alice.execute(intent_id=intent_id, prepared_hash=full_hash, approve=True)
    print(f"[alice] executed: state={exec_res['state']} ref={exec_res['settlement_ref']}")

    # 7. Final status
    print("[alice] status:", json.dumps(alice.status(), indent=2))
    return 0 if exec_res.get("state") == "settled" else 2


if __name__ == "__main__":
    sys.exit(main())
