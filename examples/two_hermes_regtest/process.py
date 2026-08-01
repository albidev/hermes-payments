"""Two-process P6 runner primitives.

The process boundary is deliberately small: it validates explicit regtest
configuration, carries machine-readable operator commands, and delegates all
payment policy to the injected ``PaymentOrchestrator``.  Payment messages
travel only through the injected ``PeerTransport`` (Buzz in production,
InMemoryPeerTransport in deterministic tests).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from hermes_payments.models import (
    AgentIdentity,
    PaymentApproval,
    PaymentIntent,
    PaymentMessage,
    PaymentQuote,
    PaymentReceipt,
    compute_id,
)
from hermes_payments.peer import HermesPeer
from hermes_payments.peer_transport import PeerMessage, PeerTransport, message_author
from hermes_payments.policy import PaymentOrchestrator
from hermes_payments.state_machine import PaymentState


class ProcessProtocolError(ValueError):
    """Raised when a process command violates the runner protocol."""


class ProcessConfig(BaseModel):
    """Explicit, safe configuration for one Hermes process."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    role: Literal["alice", "bob"]
    identity: AgentIdentity
    channel: str
    state_root: Path
    network: str
    buzz_bin: str = "buzz"
    audit_path: Optional[Path] = None
    store_path: Optional[Path] = None
    approver: Optional[AgentIdentity] = None

    def model_post_init(self, __context: Any) -> None:
        if self.audit_path is None:
            self.audit_path = Path(self.state_root) / "audit.jsonl"
        if self.store_path is None:
            self.store_path = Path(self.state_root) / "store.json"

    @field_validator("network")
    @classmethod
    def _regtest_only(cls, value: str) -> str:
        if value != "regtest":
            raise ValueError("P6 process runner is regtest-only")
        return value

    @field_validator("channel")
    @classmethod
    def _channel_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("channel is required")
        return value

    @field_validator("state_root", mode="before")
    @classmethod
    def _state_root_required(cls, value: object) -> object:
        if value is None or str(value).strip() in {"", "."}:
            raise ValueError("state_root is required and must be isolated")
        return value


def redacted_identifier(value: str) -> str:
    """Return a short, non-reconstructable display prefix for an identifier."""
    if len(value) <= 8:
        return value
    return f"{value[:8]}..."


class JsonlCommand:
    """Validated operator command exchanged over one JSONL stream."""

    _KNOWN_COMMANDS = frozenset({
        "status",
        "receive",
        "submit_intent",
        "accept_intent",
        "publish_quote",
        "accept_quote",
        "prepare",
        "approve",
        "execute",
        "publish_receipt",
        "accept_receipt",
        "recover",
        "submit_quote_local",
    })

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = arguments

    @classmethod
    def parse(cls, line: str) -> "JsonlCommand":
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProcessProtocolError("command must be valid JSON object") from exc
        if not isinstance(data, dict):
            raise ProcessProtocolError("command must be valid JSON object")

        name = data.get("command")
        if not isinstance(name, str) or not name:
            raise ProcessProtocolError("command name is required")
        if name not in cls._KNOWN_COMMANDS:
            raise ProcessProtocolError(f"unknown command: {name}")

        arguments = {key: value for key, value in data.items() if key != "command"}
        return cls(name=name, arguments=arguments)

    def to_json(self) -> str:
        return json.dumps(
            {"command": self.name, **self.arguments},
            sort_keys=True,
            separators=(",", ":"),
        )


class ProcessStateStore:
    """Durable snapshot of in-memory intent records for restart recovery.

    The idempotency store and audit log are already durable; this store adds
    just enough in-memory state (intent payload, current state, quote) so a
    restarted process can continue without re-running adapter calls.  It is
    written atomically after every state-changing command.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, records: dict[str, Any]) -> None:
        """Atomically persist the process state snapshot."""
        os.makedirs(self._path.parent, exist_ok=True)
        dir_name = self._path.parent
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(records, f, sort_keys=True)
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load(self) -> dict[str, Any]:
        """Load the latest snapshot, or return an empty dict."""
        if not self._path.exists():
            return {}
        with open(self._path, "r") as f:
            return json.load(f)


class JsonlProcess:
    """Small JSONL process boundary with injected side-effect handlers."""

    def __init__(
        self,
        config: ProcessConfig,
        *,
        receive_handler: Any = None,
        recover_handler: Any = None,
    ) -> None:
        self.config = config
        self._receive_handler = receive_handler
        self._recover_handler = recover_handler

    def handle_line(self, line: str) -> str:
        command = JsonlCommand.parse(line)
        if command.name == "status":
            return json.dumps(
                {
                    "event": "status",
                    "role": self.config.role,
                    "network": self.config.network,
                    "channel": self.config.channel,
                    "state_root": str(self.config.state_root),
                },
                sort_keys=True,
                separators=(",", ":"),
            )

        handler = {
            "receive": self._receive_handler,
            "recover": self._recover_handler,
        }[command.name]
        if handler is None:
            raise ProcessProtocolError(f"{command.name} handler is not configured")

        result = handler(command.arguments)
        if not isinstance(result, dict):
            raise ProcessProtocolError(f"{command.name} handler must return an object")
        return json.dumps(
            {"event": command.name, **result},
            sort_keys=True,
            separators=(",", ":"),
        )


def _serialize_message(message: PaymentMessage) -> dict[str, Any]:
    """Return a display-safe summary of a peer message for JSONL output."""
    base = {
        "type": message.__class__.__name__,
        "id": redacted_identifier(getattr(message, "id", "")),
        "protocol_version": getattr(message, "protocol_version", "1"),
        "author": redacted_identifier(message_author(message).pubkey),
    }
    if isinstance(message, PaymentIntent):
        base["intent_id"] = redacted_identifier(message.id)
        base["amount_sat"] = message.amount_sat
    elif isinstance(message, (PaymentQuote, PaymentReceipt)):
        base["intent_id"] = redacted_identifier(getattr(message, "intent_id", ""))
    return base


def _state_count_summary(orchestrator: PaymentOrchestrator) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rec in orchestrator._intents.values():
        counts[rec.state.value] = counts.get(rec.state.value, 0) + 1
    return counts


class HermesRegtestProcess:
    """One JSONL-controlled Hermes process with durable, isolated state."""

    def __init__(
        self,
        *,
        config: ProcessConfig,
        adapter: Any,
        transport: PeerTransport,
        clock: Optional[Any] = None,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: 0)
        self._orchestrator = PaymentOrchestrator(
            adapter=adapter,
            store_path=str(config.store_path) if config.store_path else None,
            audit_path=str(config.audit_path) if config.audit_path else None,
            clock=self._clock,
        )
        self._peer = HermesPeer(
            identity=config.identity,
            transport=transport,
            orchestrator=self._orchestrator,
        )
        self._inbox: list[PeerMessage] = []
        self._state_store = ProcessStateStore(
            config.state_root / "process_state.json"
        )
        self._maybe_recover()

    # ------------------------------------------------------------------
    # Public JSONL interface
    # ------------------------------------------------------------------

    def handle_line(self, line: str) -> str:
        command = JsonlCommand.parse(line)
        handler = getattr(self, f"_handle_{command.name}", None)
        if handler is None:
            raise ProcessProtocolError(f"command not implemented: {command.name}")
        result = handler(command.arguments)
        if not isinstance(result, dict):
            raise ProcessProtocolError(f"{command.name} handler must return an object")
        self._snapshot_state()
        return json.dumps(
            {"event": command.name, **result},
            sort_keys=True,
            separators=(",", ":"),
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return {
            "role": self.config.role,
            "network": self.config.network,
            "channel": self.config.channel,
            "state_root": str(self.config.state_root),
            "state_count": _state_count_summary(self._orchestrator),
        }

    def _handle_receive(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = args.get("limit")
        if limit is not None:
            limit = int(limit)
        messages = self._peer.receive(limit=limit)
        self._inbox.extend(messages)
        return {
            "messages": [
                {
                    "message_id": redacted_identifier(msg.message_id),
                    **_serialize_message(msg.message),
                }
                for msg in messages
            ],
            "inbox_size": len(self._inbox),
        }

    def _handle_submit_intent(self, args: dict[str, Any]) -> dict[str, Any]:
        intent = PaymentIntent.model_validate(args["intent"])
        message_id = self._peer.submit_intent(intent)
        return {
            "intent_id": redacted_identifier(intent.id),
            "message_id": redacted_identifier(message_id),
            "state": self._orchestrator.state(intent.id).value,
        }

    def _handle_accept_intent(self, args: dict[str, Any]) -> dict[str, Any]:
        redacted_id = args["message_id"]
        msg = self._find_inbox_message(redacted_id)
        if not isinstance(msg.message, PaymentIntent):
            raise ProcessProtocolError("message is not a PaymentIntent")
        self._peer.accept_intent(msg.message)
        return {
            "intent_id": redacted_identifier(msg.message.id),
            "state": self._orchestrator.state(msg.message.id).value,
        }

    def _handle_publish_quote(self, args: dict[str, Any]) -> dict[str, Any]:
        quote = PaymentQuote.model_validate(args["quote"])
        message_id = self._peer.publish_quote(quote)
        return {
            "quote_id": redacted_identifier(quote.quote_id),
            "message_id": redacted_identifier(message_id),
            "state": self._orchestrator.state(quote.intent_id).value,
        }

    def _handle_accept_quote(self, args: dict[str, Any]) -> dict[str, Any]:
        redacted_id = args["message_id"]
        msg = self._find_inbox_message(redacted_id)
        if not isinstance(msg.message, PaymentQuote):
            raise ProcessProtocolError("message is not a PaymentQuote")
        self._peer.accept_quote(msg.message)
        return {
            "intent_id": redacted_identifier(msg.message.intent_id),
            "state": self._orchestrator.state(msg.message.intent_id).value,
        }

    def _handle_submit_quote_local(self, args: dict[str, Any]) -> dict[str, Any]:
        """Inject a quote locally without crossing the transport.

        Used only in deterministic single-process tests where Alice needs a
        quote in her orchestrator but the transport path is exercised
        separately.
        """
        quote = PaymentQuote.model_validate(args["quote"])
        self._orchestrator.receive_quote(quote)
        return {
            "quote_id": redacted_identifier(quote.quote_id),
            "state": self._orchestrator.state(quote.intent_id).value,
        }

    def _handle_prepare(self, _args: dict[str, Any]) -> dict[str, Any]:
        prepared = self._orchestrator.prepare()
        return {
            "fee_sat": prepared.fee_sat,
            "prepared_hash": prepared.prepared_hash,
            "rail": prepared.rail.value,
        }

    def _handle_approve(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.config.approver is None:
            raise ProcessProtocolError("approver identity is not configured")
        approval = PaymentApproval(
            id="placeholder",
            intent_id=args["intent_id"],
            quote_id=args["quote_id"],
            prepared_hash=args["prepared_hash"],
            approver=self.config.approver,
            created_at=self._clock(),
        )
        approval.id = compute_id(approval)
        self._orchestrator.approve(approval)
        return {
            "intent_id": redacted_identifier(approval.intent_id),
            "quote_id": redacted_identifier(approval.quote_id),
            "prepared_hash": redacted_identifier(approval.prepared_hash),
            "state": self._orchestrator.state(approval.intent_id).value,
        }

    def _handle_execute(self, _args: dict[str, Any]) -> dict[str, Any]:
        receipt = self._orchestrator.execute()
        return {
            "state": self._orchestrator.state(receipt.intent_id).value,
            "settlement_ref": redacted_identifier(receipt.settlement_ref),
            "amount_sat": receipt.amount_sat,
            "fee_sat": receipt.fee_sat,
        }

    def _handle_publish_receipt(self, args: dict[str, Any]) -> dict[str, Any]:
        receipt = PaymentReceipt.model_validate(args["receipt"])
        message_id = self._peer.publish_receipt(receipt)
        return {
            "receipt_id": redacted_identifier(receipt.id),
            "message_id": redacted_identifier(message_id),
            "intent_id": redacted_identifier(receipt.intent_id),
        }

    def _handle_accept_receipt(self, args: dict[str, Any]) -> dict[str, Any]:
        redacted_id = args["message_id"]
        msg = self._find_inbox_message(redacted_id)
        if not isinstance(msg.message, PaymentReceipt):
            raise ProcessProtocolError("message is not a PaymentReceipt")
        self._peer.accept_receipt(msg.message)
        return {
            "intent_id": redacted_identifier(msg.message.intent_id),
            "state": self._orchestrator.state(msg.message.intent_id).value,
        }

    def _handle_recover(self, _args: dict[str, Any]) -> dict[str, Any]:
        self._maybe_recover()
        return {
            "intents": [
                {
                    "intent_id": redacted_identifier(rec.intent.id),
                    "state": rec.state.value,
                }
                for rec in self._orchestrator._intents.values()
            ],
            "state_count": _state_count_summary(self._orchestrator),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_inbox_message(self, redacted_id: str) -> PeerMessage:
        for msg in self._inbox:
            if redacted_identifier(msg.message_id) == redacted_id:
                return msg
        raise ProcessProtocolError(f"message {redacted_id} not found in inbox")

    def _snapshot_state(self) -> None:
        snapshot: dict[str, Any] = {}
        for intent_id, rec in self._orchestrator._intents.items():
            snapshot[intent_id] = {
                "state": rec.state.value,
                "intent": (
                    rec.intent.model_dump(exclude_none=True, mode="python")
                    if rec.intent else None
                ),
                "quote": (
                    rec.quote.model_dump(exclude_none=True, mode="python")
                    if rec.quote else None
                ),
                "prepared_hash": (
                    rec.prepared.prepared_hash if rec.prepared else None
                ),
                "approval_id": rec.approval.id if rec.approval else None,
                "receipt": (
                    rec.receipt.model_dump(exclude_none=True, mode="python")
                    if rec.receipt else None
                ),
            }
        self._state_store.save(snapshot)

    def _maybe_recover(self) -> None:
        snapshot = self._state_store.load()
        from hermes_payments.policy import IntentRecord
        for intent_id, data in snapshot.items():
            record = IntentRecord(
                intent=PaymentIntent.model_validate(data["intent"]),
                state=PaymentState(data["state"]),
            )
            if data.get("quote"):
                record.quote = PaymentQuote.model_validate(data["quote"])
            if data.get("prepared_hash"):
                # Prepared payload is adapter-specific and not persisted; the
                # hash is enough to prove an approval already happened.
                record.prepared = None
            if data.get("receipt"):
                record.receipt = PaymentReceipt.model_validate(data["receipt"])
            self._orchestrator._intents[intent_id] = record


__all__ = [
    "HermesRegtestProcess",
    "JsonlCommand",
    "JsonlProcess",
    "ProcessConfig",
    "ProcessProtocolError",
    "ProcessStateStore",
    "redacted_identifier",
]
