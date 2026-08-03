"""Hermes Payments plugin.

Lets a running Hermes agent send and receive payments over Buzz + Wavelength,
reusing the transport-neutral protocol built in P1-P6.

Policy-first and fail-closed:
  - Each step is an explicit tool call. ``hp_pay`` only submits the intent.
  - ``hp_prepare`` accepts the recipient's quote and calls ``adapter.prepare()``,
    returning the ``prepared_hash``. No money moves.
  - ``hp_execute`` requires ``approve: true`` AND the exact ``prepared_hash``
    returned by ``hp_prepare``. The approval triple (intent, quote, hash) is
    validated by ``PaymentOrchestrator.approve()``.
  - No automatic retry on ambiguous settlement: ``RECONCILIATION_REQUIRED``
    is surfaced to the operator.
  - The recipient's quote always requires a real invoice (bolt11) supplied by
    the operator — the plugin never mints an invoice itself.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_payments.adapter import (
    SubprocessWavecliExecutor,
    WavelengthAdapter,
    redact_sensitive,
)
from hermes_payments.models import (
    AgentIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    Rail,
    RailReceiveInstruction,
    compute_id,
)
from hermes_payments.peer import HermesPeer
from hermes_payments.peer_transport import PeerMessage
from hermes_payments.policy import PaymentOrchestrator
from hermes_payments.transport import BuzzTransport, SubprocessExecutor

# ---------------------------------------------------------------------------
# Module-level singleton state (plugins load once in the gateway process).
# ---------------------------------------------------------------------------

_singleton: Optional["PaymentService"] = None
_lock = threading.Lock()


def _service() -> "PaymentService":
    """Build (once) the payment service from environment configuration."""
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = PaymentService.from_env()
        return _singleton


def redacted(value: str) -> str:
    """Short, non-reconstructable display prefix for an identifier."""
    if len(value) <= 10:
        return value
    return f"{value[:10]}..."


class ConfigError(RuntimeError):
    """Raised when the plugin environment configuration is invalid or unsafe."""


class PaymentService:
    """Compose the payment stack for one Hermes identity.

    Owns a ``PaymentOrchestrator`` (policy), a ``HermesPeer`` (transport-neutral
    endpoint), a ``BuzzTransport`` (coordination), and a ``WavelengthAdapter``
    (settlement). Config is explicit and loaded from the environment.
    """

    def __init__(
        self,
        *,
        identity: AgentIdentity,
        approver: AgentIdentity,
        transport: Any,
        adapter: Any,
        channel: str,
        state_root: Path,
        network: str,
        clock: Optional[Any] = None,
    ) -> None:
        self._clock = clock or (lambda: int(time.time()))
        self._identity = identity
        self._approver = approver
        self._channel = channel
        self._network = network
        self._state_root = Path(state_root)
        self._state_root.mkdir(parents=True, exist_ok=True)

        self._adapter = adapter
        self._orchestrator = PaymentOrchestrator(
            adapter=adapter,
            store_path=str(self._state_root / "store.json"),
            audit_path=str(self._state_root / "audit.jsonl"),
            clock=self._clock,
        )
        self._peer = HermesPeer(
            identity=identity,
            transport=transport,
            orchestrator=self._orchestrator,
        )
        self._inbox: List[PeerMessage] = []
        self._inbox_ids: set[str] = set()

    # -- configuration ---------------------------------------------------

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "PaymentService":
        env = dict(os.environ if env is None else env)

        role = env.get("HP_ROLE", "").strip()
        pubkey = env.get("HP_PUBKEY", "").strip()
        approver_pubkey = env.get("HP_APPROVER_PUBKEY", "").strip()
        channel = env.get("HP_CHANNEL", "").strip()
        state_root = env.get("HP_STATE_ROOT", "").strip()
        network = env.get("HP_NETWORK", "regtest").strip().lower()
        wave_rpc = env.get("HP_WAVE_RPC_SERVER", "").strip() or None

        if not role or role not in {"alice", "bob"}:
            raise ConfigError("HP_ROLE must be set to 'alice' or 'bob'")
        if len(pubkey) != 64:
            raise ConfigError("HP_PUBKEY must be a 64-char hex pubkey")
        if len(approver_pubkey) != 64:
            raise ConfigError("HP_APPROVER_PUBKEY must be a 64-char hex pubkey")
        if not channel:
            raise ConfigError("HP_CHANNEL is required (Buzz channel UUID)")
        if not state_root:
            raise ConfigError("HP_STATE_ROOT is required and must be isolated")
        if network not in {"regtest", "signet"}:
            raise ConfigError("HP_NETWORK supports regtest or signet only")
        if network == "signet" and not wave_rpc:
            raise ConfigError("signet requires HP_WAVE_RPC_SERVER")

        identity = AgentIdentity(pubkey=pubkey, relay_url=None)
        approver = AgentIdentity(pubkey=approver_pubkey, relay_url=None)
        state_root_path = Path(state_root)

        buzz_bin = env.get("BUZZ_BIN", "buzz")
        transport = BuzzTransport(
            executor=SubprocessExecutor(buzz_bin=buzz_bin, timeout=30),
            channel=channel,
            cursor_path=state_root_path / "buzz_cursor.json",
            local_pubkey=pubkey,
        )

        adapter = WavelengthAdapter(
            executor=SubprocessWavecliExecutor(),
            rpc_server=wave_rpc or "localhost:10029",
            network=network,
            no_tls=True,
            no_macaroons=True,
        )

        return cls(
            identity=identity,
            approver=approver,
            transport=transport,
            adapter=adapter,
            channel=channel,
            state_root=state_root_path,
            network=network,
        )

    # -- inbox / polling ---------------------------------------------------

    def poll(self) -> List[dict[str, Any]]:
        """Read new messages from the transport into the local inbox."""
        fresh: List[dict[str, Any]] = []
        try:
            received = self._peer.receive()
        except Exception as exc:
            return [{"error": redact_sensitive(str(exc))}]
        for message in received:
            if message.message_id in self._inbox_ids:
                continue
            self._inbox_ids.add(message.message_id)
            self._inbox.append(message)
            fresh.append(
                {
                    "message_id": redacted(message.message_id),
                    "type": message.message.__class__.__name__,
                    "author": redacted(message.author.pubkey),
                    "intent_id": redacted(getattr(message.message, "id", "")),
                }
            )
        return fresh

    def _find_intent_message(self, intent_id: str) -> PaymentIntent:
        for message in self._inbox:
            msg = message.message
            if isinstance(msg, PaymentIntent) and msg.id == intent_id:
                return msg
        raise LookupError(f"intent {redacted(intent_id)} not in inbox")

    # -- sender side -------------------------------------------------------

    def pay(self, *, recipient_pubkey: str, amount_sat: int, purpose: str,
            max_fee_sat: int, expires_at: int, idempotency_key: str) -> dict[str, Any]:
        """Submit and publish a payment intent."""
        if not idempotency_key:
            idempotency_key = f"hp-{int(self._clock())}"
        intent = PaymentIntent(
            id="placeholder",
            idempotency_key=idempotency_key,
            sender=self._identity,
            recipient=AgentIdentity(pubkey=recipient_pubkey, relay_url=None),
            amount_sat=amount_sat,
            purpose=purpose,
            max_fee_sat=max_fee_sat,
            expires_at=expires_at,
            created_at=int(self._clock()),
        )
        intent.id = compute_id(intent)
        message_id = self._peer.submit_intent(intent)
        return {
            "intent_id": redacted(intent.id),
            "full_intent_id": intent.id,
            "message_id": redacted(message_id),
            "state": self._orchestrator.state(intent.id).value,
        }

    def prepare(self, *, quote_id: str) -> dict[str, Any]:
        """Accept the matching quote and call adapter.prepare() (dry run)."""
        quote = self._find_quote(quote_id)
        self._peer.accept_quote(quote)
        prepared = self._orchestrator.prepare()
        return {
            "intent_id": redacted(quote.intent_id),
            "quote_id": redacted(quote.quote_id),
            "fee_sat": prepared.fee_sat,
            "prepared_hash": prepared.prepared_hash,
            "full_prepared_hash": prepared.prepared_hash,
            "rail": prepared.rail.value,
            "state": self._orchestrator.state(quote.intent_id).value,
        }

    def execute(self, *, intent_id: str, prepared_hash: str, approve: bool) -> dict[str, Any]:
        """Approve (explicit) and execute the settlement."""
        if not approve:
            return {
                "error": "execution requires approve: true — no money moved",
                "state": self._orchestrator.state(intent_id).value,
            }
        # Reject redacted hashes — approval must bind to the FULL prepared hash.
        full_hash = self._resolve_prepared_hash(intent_id, prepared_hash)
        approval = PaymentApproval(
            id="placeholder",
            intent_id=intent_id,
            quote_id=self._quote_id_for(intent_id),
            prepared_hash=full_hash,
            approver=self._approver,
            created_at=int(self._clock()),
        )
        approval.id = compute_id(approval)
        self._orchestrator.approve(approval)
        receipt = self._orchestrator.execute()
        return {
            "intent_id": redacted(receipt.intent_id),
            "settlement_ref": redacted(receipt.settlement_ref),
            "amount_sat": receipt.amount_sat,
            "fee_sat": receipt.fee_sat,
            "state": self._orchestrator.state(receipt.intent_id).value,
        }

    def reconcile(self, *, intent_id: str, max_wait_seconds: float = 0.0) -> dict[str, Any]:
        """Reconcile a RECONCILIATION_REQUIRED intent (fail-closed, no retry)."""
        result = self._orchestrator.reconcile_settlement(intent_id)
        return {
            "intent_id": redacted(intent_id),
            "status": result.status,
            "settlement_ref": redacted(result.settlement_ref or ""),
            "state": self._orchestrator.state(intent_id).value,
        }

    # -- recipient side ----------------------------------------------------

    def accept_and_quote(self, *, intent_id: str, invoice: str) -> dict[str, Any]:
        """Accept a received intent and publish a quote with the operator's invoice."""
        intent = self._find_intent_message(intent_id)
        if not invoice.startswith("ln"):
            raise ValueError("invoice must be a valid bolt11 (starts with 'ln')")
        self._peer.accept_intent(intent)
        quote = PaymentQuote(
            id="placeholder",
            intent_id=intent.id,
            quote_id=f"q-{intent.id[:16]}-{int(self._clock())}",
            recipient=self._identity,
            receive_instruction=RailReceiveInstruction(
                rail=Rail.LIGHTNING,
                invoice=invoice,
            ),
            fee_sat=0,
            fee_constraint="max",
            expires_at=intent.expires_at,
            created_at=int(self._clock()),
        )
        quote.id = compute_id(quote)
        message_id = self._peer.publish_quote(quote)
        return {
            "intent_id": redacted(intent.id),
            "quote_id": redacted(quote.quote_id),
            "message_id": redacted(message_id),
            "state": self._orchestrator.state(intent.id).value,
        }

    # -- status ------------------------------------------------------------

    def status(self) -> List[dict[str, Any]]:
        out: List[dict[str, Any]] = []
        for intent_id, rec in self._orchestrator._intents.items():  # type: ignore[attr-defined]
            out.append(
                {
                    "intent_id": redacted(intent_id),
                    "state": rec.state.value,
                    "amount_sat": rec.intent.amount_sat,
                    "purpose": rec.intent.purpose,
                    "recipient": redacted(rec.intent.recipient.pubkey),
                    "prepared_hash": (
                        redacted(rec.prepared.prepared_hash)
                        if rec.prepared is not None
                        else None
                    ),
                }
            )
        return out

    def balance(self) -> dict[str, Any]:
        try:
            result = self._adapter._executor.run(["wallet", "balance"])  # type: ignore[attr-defined]
            return {"balance": result}
        except Exception as exc:
            return {"error": redact_sensitive(str(exc))}

    # -- internal helpers ----------------------------------------------------

    def _find_quote(self, quote_id: str) -> PaymentQuote:
        for message in self._inbox:
            msg = message.message
            if isinstance(msg, PaymentQuote) and msg.quote_id == quote_id:
                return msg
        raise LookupError(f"quote {redacted(quote_id)} not in inbox")

    def _resolve_prepared_hash(self, intent_id: str, supplied: str) -> str:
        rec = self._orchestrator._intents.get(intent_id)  # type: ignore[attr-defined]
        if rec is None or rec.prepared is None:
            raise ValueError(f"intent {redacted(intent_id)} has no prepared payload")
        full = rec.prepared.prepared_hash
        if supplied == full:
            return full
        if supplied == redacted(full):
            raise ValueError("refusing redacted prepared_hash — pass the FULL hash")
        raise ValueError("prepared_hash does not match the prepared payload")

    def _quote_id_for(self, intent_id: str) -> str:
        rec = self._orchestrator._intents.get(intent_id)  # type: ignore[attr-defined]
        if rec is None or rec.quote is None:
            raise ValueError(f"intent {redacted(intent_id)} has no quote")
        return rec.quote.quote_id


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _tool_hp_pay(args, **kwargs):
    svc = _service()
    try:
        return json.dumps(
            svc.pay(
                recipient_pubkey=args["recipient_pubkey"],
                amount_sat=int(args["amount_sat"]),
                purpose=args["purpose"],
                max_fee_sat=int(args.get("max_fee_sat", 0)),
                expires_at=int(args["expires_at"]),
                idempotency_key=args.get("idempotency_key", ""),
            ),
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps({"error": redact_sensitive(str(exc))}, sort_keys=True)


def _tool_hp_poll(args, **kwargs):
    svc = _service()
    return json.dumps({"received": svc.poll()}, sort_keys=True)


def _tool_hp_prepare(args, **kwargs):
    svc = _service()
    try:
        return json.dumps(svc.prepare(quote_id=args["quote_id"]), sort_keys=True)
    except Exception as exc:
        return json.dumps({"error": redact_sensitive(str(exc))}, sort_keys=True)


def _tool_hp_execute(args, **kwargs):
    svc = _service()
    try:
        return json.dumps(
            svc.execute(
                intent_id=args["intent_id"],
                prepared_hash=args["prepared_hash"],
                approve=bool(args.get("approve", False)),
            ),
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps({"error": redact_sensitive(str(exc))}, sort_keys=True)


def _tool_hp_accept_quote(args, **kwargs):
    svc = _service()
    try:
        return json.dumps(
            svc.accept_and_quote(
                intent_id=args["intent_id"], invoice=args["invoice"]
            ),
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps({"error": redact_sensitive(str(exc))}, sort_keys=True)


def _tool_hp_reconcile(args, **kwargs):
    svc = _service()
    try:
        return json.dumps(
            svc.reconcile(
                intent_id=args["intent_id"],
                max_wait_seconds=float(args.get("max_wait_seconds", 0)),
            ),
            sort_keys=True,
        )
    except Exception as exc:
        return json.dumps({"error": redact_sensitive(str(exc))}, sort_keys=True)


def _tool_hp_status(args, **kwargs):
    svc = _service()
    return json.dumps({"intents": svc.status()}, sort_keys=True)


def _tool_hp_balance(args, **kwargs):
    svc = _service()
    return json.dumps(svc.balance(), sort_keys=True)


# ---------------------------------------------------------------------------
# register() — Hermes plugin entry point
# ---------------------------------------------------------------------------


def _tool_spec(name: str, description: str, props: Dict[str, Any], required: List[str]):
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required,
        },
    }


def register(ctx) -> None:
    ctx.register_tool(
        "hp_pay",
        "hermes-payments",
        _tool_spec(
            "hp_pay",
            "Submit a payment intent to a recipient over Buzz+Wavelength. Does NOT move money — "
            "returns an intent_id, then poll + prepare + approve + execute complete the flow.",
            {
                "recipient_pubkey": {"type": "string", "description": "Recipient 64-char hex pubkey"},
                "amount_sat": {"type": "integer", "description": "Amount in satoshis"},
                "purpose": {"type": "string", "description": "Human-readable purpose"},
                "max_fee_sat": {"type": "integer", "description": "Max acceptable fee"},
                "expires_at": {"type": "integer", "description": "Unix epoch expiry"},
                "idempotency_key": {"type": "string", "description": "Optional idempotency key"},
            },
            ["recipient_pubkey", "amount_sat", "purpose", "expires_at"],
        ),
        _tool_hp_pay,
    )
    ctx.register_tool(
        "hp_poll",
        "hermes-payments",
        _tool_spec(
            "hp_poll",
            "Poll the Buzz channel for incoming payment messages (intents, quotes, receipts).",
            {},
            [],
        ),
        _tool_hp_poll,
    )
    ctx.register_tool(
        "hp_prepare",
        "hermes-payments",
        _tool_spec(
            "hp_prepare",
            "Accept a received quote and prepare the payment (dry run). Returns the FULL prepared_hash needed for hp_execute.",
            {"quote_id": {"type": "string", "description": "The quote_id from the inbox"}},
            ["quote_id"],
        ),
        _tool_hp_prepare,
    )
    ctx.register_tool(
        "hp_execute",
        "hermes-payments",
        _tool_spec(
            "hp_execute",
            "Approve and execute a prepared payment. REQUIRES approve:true AND the exact prepared_hash from hp_prepare. "
            "This is the only step that moves money.",
            {
                "intent_id": {"type": "string", "description": "The intent_id"},
                "prepared_hash": {"type": "string", "description": "The FULL prepared_hash from hp_prepare"},
                "approve": {"type": "boolean", "description": "MUST be true to move money"},
            },
            ["intent_id", "prepared_hash"],
        ),
        _tool_hp_execute,
    )
    ctx.register_tool(
        "hp_accept_quote",
        "hermes-payments",
        _tool_spec(
            "hp_accept_quote",
            "Recipient: accept a received payment intent and publish a quote with the given bolt11 invoice.",
            {
                "intent_id": {"type": "string", "description": "The intent_id from the inbox"},
                "invoice": {"type": "string", "description": "bolt11 invoice (starts with 'ln')"},
            },
            ["intent_id", "invoice"],
        ),
        _tool_hp_accept_quote,
    )
    ctx.register_tool(
        "hp_reconcile",
        "hermes-payments",
        _tool_spec(
            "hp_reconcile",
            "Reconcile an intent stuck in RECONCILIATION_REQUIRED (fail-closed, never auto-retries).",
            {
                "intent_id": {"type": "string", "description": "The intent_id"},
                "max_wait_seconds": {"type": "number", "description": "Max wait"},
            },
            ["intent_id"],
        ),
        _tool_hp_reconcile,
    )
    ctx.register_tool(
        "hp_status",
        "hermes-payments",
        _tool_spec(
            "hp_status",
            "Show the current state of all known payment intents.",
            {},
            [],
        ),
        _tool_hp_status,
    )
    ctx.register_tool(
        "hp_balance",
        "hermes-payments",
        _tool_spec(
            "hp_balance",
            "Query the Wavelength balance for this identity.",
            {},
            [],
        ),
        _tool_hp_balance,
    )
