"""Hermes Payments plugin.

Lets a running Hermes agent send and receive payments over Buzz + Wavelength,
reusing the transport-neutral protocol built in P1-P6.

Policy-first and fail-closed:
  - Each step is an explicit tool call. ``hp_pay`` only submits the intent.
  - ``hp_prepare`` accepts the recipient's quote and calls ``adapter.prepare()``,
    returning the ``prepared_hash``. No money moves.
  - ``hp_execute`` requests a real local Hermes approval bound to
    ``(intent_id, quote_id, prepared_hash)``. The model cannot pass an approval
    boolean or choose a session-wide approval.
  - No automatic retry on ambiguous settlement: ``RECONCILIATION_REQUIRED``
    is surfaced to the operator.
  - The recipient's quote always requires a real invoice (bolt11) supplied by
    the operator — the plugin never mints an invoice itself.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_payments.adapter import (
    SubprocessWavecliExecutor,
    WavelengthAdapter,
    redact_sensitive,
)
from hermes_payments.approval import request_human_approval
from hermes_payments.models import (
    AgentIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentQuote,
    PaymentReceipt,
    Rail,
    RailReceiveInstruction,
    compute_id,
)
from hermes_payments.peer import HermesPeer
from hermes_payments.peer_transport import PeerMessage
from hermes_payments.policy import PaymentOrchestrator
from hermes_payments.state_machine import PaymentState
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
        approval_gate: Optional[Any] = None,
    ) -> None:
        self._clock = clock or (lambda: int(time.time()))
        self._approval_gate = approval_gate or request_human_approval
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
            state_path=str(self._state_root / "orchestrator.json"),
            clock=self._clock,
        )
        self._peer = HermesPeer(
            identity=identity,
            transport=transport,
            orchestrator=self._orchestrator,
        )
        self._inbox: List[PeerMessage] = []
        self._inbox_ids: set[str] = set()
        self._inbox_path = self._state_root / "inbox.json"
        self._receipts_path = self._state_root / "published_receipts.json"
        self._published_receipts: Dict[str, PaymentReceipt] = {}
        self._load_persistent_messages()

    # -- durable transport-side state -------------------------------------

    def _atomic_json_write(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _message_from_record(raw: Dict[str, Any]) -> Any:
        models = {
            "PaymentIntent": PaymentIntent,
            "PaymentQuote": PaymentQuote,
            "PaymentReceipt": PaymentReceipt,
        }
        message_type = raw.get("type")
        model = models.get(message_type)
        if model is None:
            raise ConfigError(f"unsupported persisted peer message type: {message_type}")
        return model.model_validate(raw["message"])

    def _load_persistent_messages(self) -> None:
        try:
            if self._inbox_path.exists():
                data = json.loads(self._inbox_path.read_text(encoding="utf-8"))
                for raw in data:
                    message = self._message_from_record(raw)
                    peer_message = PeerMessage(
                        message_id=str(raw["message_id"]),
                        message=message,
                        author=AgentIdentity.model_validate(raw["author"]),
                        published_at=int(raw["published_at"]),
                    )
                    self._inbox.append(peer_message)
                    self._inbox_ids.add(peer_message.message_id)
            if self._receipts_path.exists():
                data = json.loads(self._receipts_path.read_text(encoding="utf-8"))
                for intent_id, raw in data.items():
                    self._published_receipts[intent_id] = PaymentReceipt.model_validate(raw)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"cannot load payment plugin state safely: {exc}") from exc

    def _save_inbox(self) -> None:
        self._atomic_json_write(
            self._inbox_path,
            [
                {
                    "message_id": message.message_id,
                    "type": message.message.__class__.__name__,
                    "message": message.message.model_dump(mode="json"),
                    "author": message.author.model_dump(mode="json"),
                    "published_at": message.published_at,
                }
                for message in self._inbox
            ],
        )

    def _save_published_receipts(self) -> None:
        self._atomic_json_write(
            self._receipts_path,
            {
                intent_id: receipt.model_dump(mode="json")
                for intent_id, receipt in self._published_receipts.items()
            },
        )

    @classmethod
    def from_env(
        cls,
        env: Optional[Dict[str, str]] = None,
        *,
        approval_gate: Optional[Callable[..., bool]] = None,
    ) -> "PaymentService":
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
            approval_gate=approval_gate,
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
            msg = message.message
            intent_id = getattr(msg, "id", "") if isinstance(msg, PaymentIntent) else getattr(msg, "intent_id", "")
            quote_id = getattr(msg, "quote_id", "")
            entry: dict[str, Any] = {
                "message_id": redacted(message.message_id),
                "type": msg.__class__.__name__,
                "author": redacted(message.author.pubkey),
                "intent_id": intent_id,
                "quote_id": quote_id or None,
            }
            if isinstance(msg, PaymentReceipt):
                entry["settlement_ref"] = redacted(msg.settlement_ref)
            fresh.append(entry)
        if fresh:
            self._save_inbox()
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
        for record in self._orchestrator.records():
            existing = record.intent
            if existing.idempotency_key != idempotency_key:
                continue
            requested = (
                recipient_pubkey,
                amount_sat,
                purpose,
                max_fee_sat,
                expires_at,
            )
            recorded = (
                existing.recipient.pubkey,
                existing.amount_sat,
                existing.purpose,
                existing.max_fee_sat,
                existing.expires_at,
            )
            if requested != recorded:
                raise ValueError("idempotency_key already used with different payment data")
            return {
                "intent_id": redacted(existing.id),
                "full_intent_id": existing.id,
                "message_id": "already-submitted",
                "state": record.state.value,
            }
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
        tracked_quote = self._orchestrator.get_quote(quote.intent_id)
        if tracked_quote is not None and tracked_quote.quote_id != quote.quote_id:
            raise ValueError("quote does not match the tracked intent")
        if tracked_quote is None:
            self._peer.accept_quote(quote)
        prepared = self._orchestrator.get_prepared(quote.intent_id)
        if prepared is None:
            prepared = self._orchestrator.prepare(intent_id=quote.intent_id)
        return {
            "intent_id": redacted(quote.intent_id),
            "quote_id": redacted(quote.quote_id),
            "fee_sat": prepared.fee_sat,
            "prepared_hash": prepared.prepared_hash,
            "full_prepared_hash": prepared.prepared_hash,
            "rail": prepared.rail.value,
            "state": self._orchestrator.state(quote.intent_id).value,
        }

    def execute(self, *, intent_id: str, prepared_hash: str) -> dict[str, Any]:
        """Request local human approval and execute one prepared settlement."""
        state = self._orchestrator.state(intent_id)
        full_hash = self._resolve_prepared_hash(intent_id, prepared_hash)
        quote_id = self._quote_id_for(intent_id)
        record = next(
            record for record in self._orchestrator.records()
            if record.intent.id == intent_id
        )

        if state == PaymentState.SETTLED and record.receipt is not None:
            receipt = record.receipt
            return {
                "intent_id": redacted(receipt.intent_id),
                "settlement_ref": redacted(receipt.settlement_ref),
                "amount_sat": receipt.amount_sat,
                "fee_sat": receipt.fee_sat,
                "state": state.value,
            }

        if state == PaymentState.PREPARED:
            approved = bool(
                self._approval_gate(
                    intent_id=intent_id,
                    quote_id=quote_id,
                    prepared_hash=full_hash,
                    amount_sat=record.intent.amount_sat,
                    fee_sat=record.prepared.fee_sat if record.prepared else 0,
                    recipient_pubkey=record.intent.recipient.pubkey,
                    purpose=record.intent.purpose,
                )
            )
            if not approved:
                return {
                    "error": "human approval required — no money moved",
                    "state": state.value,
                }
            approval = PaymentApproval(
                id="placeholder",
                intent_id=intent_id,
                quote_id=quote_id,
                prepared_hash=full_hash,
                approver=self._approver,
                created_at=int(self._clock()),
            )
            approval.id = compute_id(approval)
            self._orchestrator.approve(approval)
        elif state != PaymentState.APPROVED:
            raise ValueError(f"intent {redacted(intent_id)} is not ready to execute: {state.value}")

        receipt = self._orchestrator.execute(intent_id=intent_id)
        if receipt is None:
            raise RuntimeError("adapter returned no settlement receipt")
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
        existing = self._orchestrator.get_quote(intent.id)
        if existing is not None:
            existing_invoice = existing.receive_instruction.invoice
            if existing_invoice != invoice:
                raise ValueError("intent already has a quote for a different invoice")
            return {
                "intent_id": redacted(intent.id),
                "quote_id": redacted(existing.quote_id),
                "full_quote_id": existing.quote_id,
                "message_id": "already-published",
                "state": self._orchestrator.state(intent.id).value,
            }
        self._peer.accept_intent(intent)
        quote = PaymentQuote(
            id="placeholder",
            intent_id=intent.id,
            quote_id=f"q-{intent.id[:16]}-{sha256(invoice.encode()).hexdigest()[:16]}",
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
        self._orchestrator.receive_quote(quote)
        message_id = self._peer.publish_quote(quote)
        return {
            "intent_id": redacted(intent.id),
            "quote_id": redacted(quote.quote_id),
            # Full id for cross-process coordination (Bob → Alice), mirroring
            # how pay() exposes full_intent_id. The redacted form is for logs.
            "full_quote_id": quote.quote_id,
            "message_id": redacted(message_id),
            "state": self._orchestrator.state(intent.id).value,
        }

    def receive(self, *, intent_id: str, settlement_ref: str) -> dict[str, Any]:
        """Verify a rail settlement and publish one recipient receipt."""
        intent = self._orchestrator.get_intent(intent_id)
        if intent is None:
            raise ValueError(f"intent {redacted(intent_id)} is not accepted locally")
        quote = self._orchestrator.get_quote(intent_id)
        if quote is None:
            raise ValueError(f"intent {redacted(intent_id)} has no quote")
        existing = self._published_receipts.get(intent_id)
        if existing is not None:
            if existing.settlement_ref != settlement_ref:
                raise ValueError("intent already has a receipt for a different settlement")
            return {
                "intent_id": redacted(intent_id),
                "quote_id": redacted(existing.quote_id),
                "settlement_ref": redacted(existing.settlement_ref),
                "amount_sat": existing.amount_sat,
                "fee_sat": existing.fee_sat,
                "message_id": "already-published",
                "state": self._orchestrator.state(intent_id).value,
            }

        verification = self._adapter.verify_receipt(
            settlement_ref=settlement_ref,
            expected_amount_sat=intent.amount_sat,
        )
        if not verification.verified:
            raise ValueError(
                f"settlement verification failed: {verification.error or 'unknown'}"
            )
        if verification.amount_sat != intent.amount_sat:
            raise ValueError("verified settlement amount does not match the intent")
        if verification.fee_sat > intent.max_fee_sat:
            raise ValueError("verified settlement fee exceeds max_fee_sat")

        receipt = PaymentReceipt(
            id="placeholder",
            intent_id=intent.id,
            quote_id=quote.quote_id,
            recipient=self._identity,
            settlement_ref=verification.settlement_ref,
            amount_sat=verification.amount_sat,
            fee_sat=verification.fee_sat,
            rail=quote.receive_instruction.rail,
            settled_at=int(self._clock()),
            created_at=int(self._clock()),
        )
        receipt.id = compute_id(receipt)
        message_id = self._peer.publish_receipt(receipt)
        self._published_receipts[intent_id] = receipt
        self._save_published_receipts()
        return {
            "intent_id": redacted(intent_id),
            "quote_id": redacted(receipt.quote_id),
            "settlement_ref": redacted(receipt.settlement_ref),
            "amount_sat": receipt.amount_sat,
            "fee_sat": receipt.fee_sat,
            "message_id": redacted(message_id),
            "state": self._orchestrator.state(intent_id).value,
        }

    def status(self) -> List[dict[str, Any]]:
        out: List[dict[str, Any]] = []
        for record in self._orchestrator.records():
            intent_id = record.intent.id
            out.append(
                {
                    "intent_id": redacted(intent_id),
                    "state": record.state.value,
                    "amount_sat": record.intent.amount_sat,
                    "purpose": record.intent.purpose,
                    "recipient": redacted(record.intent.recipient.pubkey),
                    "prepared_hash": (
                        redacted(record.prepared.prepared_hash)
                        if record.prepared is not None
                        else None
                    ),
                }
            )
        return out

    def balance(self) -> dict[str, Any]:
        try:
            result = self._adapter._executor.run(["wavecli", "wallet", "balance"])  # type: ignore[attr-defined]
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


def _tool_hp_receive(args, **kwargs):
    svc = _service()
    try:
        return json.dumps(
            svc.receive(
                intent_id=args["intent_id"],
                settlement_ref=args["settlement_ref"],
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
            "returns an intent_id, then poll + prepare + execute complete the flow.",
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
            "Request a real one-shot local human approval and execute a prepared payment. "
            "The approval is bound to the exact intent_id, quote_id, and prepared_hash; "
            "there is no model-controlled approval boolean.",
            {
                "intent_id": {"type": "string", "description": "The full intent_id"},
                "prepared_hash": {"type": "string", "description": "The FULL prepared_hash from hp_prepare"},
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
        "hp_receive",
        "hermes-payments",
        _tool_spec(
            "hp_receive",
            "Recipient: verify a settlement on the configured rail and publish one receipt. "
            "A failed verification never publishes a receipt.",
            {
                "intent_id": {"type": "string", "description": "The full intent_id"},
                "settlement_ref": {"type": "string", "description": "Rail settlement reference"},
            },
            ["intent_id", "settlement_ref"],
        ),
        _tool_hp_receive,
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
