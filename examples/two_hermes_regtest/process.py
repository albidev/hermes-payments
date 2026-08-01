"""Two-process P6 runner primitives.

The process boundary is deliberately small: it validates explicit regtest
configuration and carries machine-readable operator commands. Payment policy
and transport implementations remain injected by the caller.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from hermes_payments.models import AgentIdentity


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

    _KNOWN_COMMANDS = frozenset({"status", "receive", "recover"})

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


__all__ = [
    "JsonlCommand",
    "JsonlProcess",
    "ProcessConfig",
    "ProcessProtocolError",
    "redacted_identifier",
]
