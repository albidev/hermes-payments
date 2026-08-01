"""
Hermes Payments — Buzz transport boundary (v2: kind-9 envelope).

Subprocess executor seam for the Buzz CLI, with explicit JSON envelope
codec and untrusted-message validation.  No network calls in tests
(FakeExecutor).

Wire contract
-------------
- All payment messages travel as NIP-29 kind-9 channel messages.
- ``buzz messages send --channel <UUID> --content <json>`` sends kind 9.
- ``buzz messages get --channel <UUID> --kinds 9`` retrieves kind 9.
- Content is a versioned JSON envelope:
    {"protocol": "hermes-payments", "version": "1",
     "type": "<intent|quote|receipt>", "payload": {...}}
- ``h`` tag (channel UUID) is added automatically by Buzz on send;
  validated on receive against the expected channel.

Safety boundaries (non-negotiable)
-----------------------------------
- PaymentApproval is NEVER encoded, serialised, or transmitted.
- No private key is ever read, logged, transmitted, or constructed;
  live signing stays inside the buzz CLI / ACP harness.
- All received messages are treated as untrusted and validated
  (kind, channel, protocol, schema, expiry, identity) before returning
  domain objects.
- Channel scoping requires a UUID; uses ``buzz messages send/get``
  arguments, not invented relay API endpoints or kinds.
"""
from __future__ import annotations

import json
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, List, Optional

from .envelope import WIRE_KIND, decode_content, encode_content
from .models import (
    PaymentIntent,
    PaymentMessage,
    PaymentQuote,
    PaymentReceipt,
)

# ---------------------------------------------------------------------------
# Raw event from Buzz CLI
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawBuzzEvent:
    """A raw Nostr event as returned by ``buzz messages get``."""

    id: str
    pubkey: str
    kind: int
    content: str
    tags: list[list[str]]
    created_at: int


# ---------------------------------------------------------------------------
# Executor protocol (subprocess seam)
# ---------------------------------------------------------------------------


class BuzzTransportError(Exception):
    """Transport-level error (CLI failure, parse error, validation failure)."""


@dataclass(frozen=True)
class SendResult:
    """Result of a successful send via the executor."""

    event_id: str


class BuzzExecutor(ABC):
    """Abstract executor for Buzz CLI subprocess calls.

    The executor is the ONLY component that touches the Buzz CLI.
    It never reads, logs, or constructs private keys — signing is
    handled inside the Buzz binary via ``BUZZ_PRIVATE_KEY`` env var.
    """

    @abstractmethod
    def send(self, *, channel: str, content: str) -> SendResult:
        """Send a message to a Buzz channel.

        Parameters
        ----------
        channel : str
            Channel UUID (required).
        content : str
            Message content (PaymentEnvelope JSON).
        """
        ...

    @abstractmethod
    def get(
        self,
        *,
        channel: str,
        kinds: Optional[List[int]] = None,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[RawBuzzEvent]:
        """Retrieve messages from a Buzz channel.

        Parameters
        ----------
        channel : str
            Channel UUID (required).
        kinds : list[int], optional
            Nostr event kinds to filter.
        since : int, optional
            Unix timestamp — only return messages after this time.
        limit : int, optional
            Maximum number of messages to return.
        """
        ...


# ---------------------------------------------------------------------------
# Subprocess executor (real Buzz CLI)
# ---------------------------------------------------------------------------


class SubprocessExecutor(BuzzExecutor):
    """Executor that calls the real ``buzz`` CLI via subprocess.

    Environment variables ``BUZZ_RELAY_URL``, ``BUZZ_PRIVATE_KEY``, and
    ``BUZZ_AUTH_TAG`` must be set in the process environment (injected
    by the ACP harness).  This class never reads or touches them.

    ``buzz messages send --channel <UUID> --content <text>``
        defaults to kind 9 (NIP-29 channel message).
    ``buzz messages get --channel <UUID> --kinds 9``
        retrieves kind-9 channel messages.
    """

    def __init__(self, *, buzz_bin: str = "buzz", timeout: int = 30):
        self._buzz_bin = buzz_bin
        self._timeout = timeout

    def send(self, *, channel: str, content: str) -> SendResult:
        cmd = [
            self._buzz_bin,
            "messages",
            "send",
            "--channel",
            channel,
            "--content",
            content,
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if result.returncode != 0:
            raise BuzzTransportError(
                f"buzz messages send failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        try:
            resp = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise BuzzTransportError(
                f"buzz messages send: invalid JSON output: {e}"
            ) from e
        event_id = resp.get("event_id") or resp.get("id") or ""
        if not event_id:
            raise BuzzTransportError(
                "buzz messages send: response missing event_id"
            )
        return SendResult(event_id=event_id)

    def get(
        self,
        *,
        channel: str,
        kinds: Optional[List[int]] = None,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[RawBuzzEvent]:
        cmd = [
            self._buzz_bin,
            "messages",
            "get",
            "--channel",
            channel,
        ]
        if kinds:
            cmd.extend(["--kinds", ",".join(str(k) for k in kinds)])
        if since is not None:
            cmd.extend(["--since", str(since)])
        if limit is not None:
            cmd.extend(["--limit", str(limit)])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self._timeout,
        )
        if result.returncode != 0:
            raise BuzzTransportError(
                f"buzz messages get failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        try:
            events = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            raise BuzzTransportError(
                f"buzz messages get: invalid JSON output: {e}"
            ) from e
        if not isinstance(events, list):
            raise BuzzTransportError(
                f"buzz messages get: expected JSON array, "
                f"got {type(events).__name__}"
            )
        parsed = []
        for ev in events:
            try:
                parsed.append(
                    RawBuzzEvent(
                        id=ev["id"],
                        pubkey=ev["pubkey"],
                        kind=ev["kind"],
                        content=ev["content"],
                        tags=ev.get("tags", []),
                        created_at=ev.get("created_at", 0),
                    )
                )
            except (KeyError, TypeError) as e:
                raise BuzzTransportError(
                    f"buzz messages get: malformed event: {e}"
                ) from e
        return parsed


# ---------------------------------------------------------------------------
# Fake executor (testing — no network, no subprocess)
# ---------------------------------------------------------------------------


class FakeExecutor(BuzzExecutor):
    """In-memory executor for tests."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._events: list[RawBuzzEvent] = []
        self._next_id = 0

    def send(self, *, channel: str, content: str) -> SendResult:
        event_id = sha256(
            f"{channel}:{self._next_id}:{content}".encode()
        ).hexdigest()
        self._next_id += 1
        self._events.append(
            RawBuzzEvent(
                id=event_id,
                pubkey="aa" * 32,
                kind=9,
                content=content,
                tags=[["h", channel]],
                created_at=int(time.time()),
            )
        )
        self.sent.append((channel, content))
        return SendResult(event_id=event_id)

    def get(
        self,
        *,
        channel: str,
        kinds: Optional[List[int]] = None,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[RawBuzzEvent]:
        result = [
            e
            for e in self._events
            if _channel_from_tags(e.tags) == channel
        ]
        if kinds:
            kind_set = set(kinds)
            result = [e for e in result if e.kind in kind_set]
        if since is not None:
            result = [e for e in result if e.created_at >= since]
        if limit is not None:
            result = result[:limit]
        return result

    def inject_event(self, event: RawBuzzEvent) -> None:
        """Inject a raw event for testing receive paths."""
        self._events.append(event)


def _channel_from_tags(tags: list[list[str]]) -> str:
    """Extract channel UUID from NIP-29 h-tags."""
    for tag in tags:
        if len(tag) >= 2 and tag[0] == "h":
            return tag[1]
    return ""


# ---------------------------------------------------------------------------
# Envelope validation for received (untrusted) events
# ---------------------------------------------------------------------------


class EnvelopeValidationError(BuzzTransportError):
    """Raised when a received event fails validation."""


def validate_received_event(
    event: RawBuzzEvent,
    *,
    expected_channel: str,
    clock: Optional[Callable[[], int]] = None,
) -> PaymentMessage:
    """Validate a raw Buzz event and decode it to a domain message.

    Treats ALL input as untrusted.  Checks:
    1. Event kind is 9 (NIP-29 channel message).
    2. ``h`` tag matches ``expected_channel``.
    3. Content parses as a valid PaymentEnvelope (protocol, version, schema).
    4. Domain message has not expired (``expires_at > now``).
    5. Event pubkey matches the message's author identity:
       - Intent: ``event.pubkey == intent.sender.pubkey``
       - Quote: ``event.pubkey == quote.recipient.pubkey``
       - Receipt: ``event.pubkey == receipt.recipient.pubkey``

    Returns the validated domain message, or raises EnvelopeValidationError.
    """
    now = (clock or (lambda: int(time.time())))()

    # 1. Kind validation — must be NIP-29 channel message (kind 9)
    if event.kind != WIRE_KIND:
        raise EnvelopeValidationError(
            f"kind {event.kind} is not a payment kind; expected {WIRE_KIND}"
        )

    # 2. Channel h-tag validation
    event_channel = _channel_from_tags(event.tags)
    if event_channel != expected_channel:
        raise EnvelopeValidationError(
            f"h-tag channel {event_channel!r} does not match "
            f"expected channel {expected_channel!r}"
        )

    # 3. Content parsing — PaymentEnvelope with protocol/version/schema
    try:
        message = decode_content(event.content)
    except (ValueError, json.JSONDecodeError) as e:
        raise EnvelopeValidationError(
            f"content validation failed: {e}"
        ) from e

    # 4. Expiry validation
    if hasattr(message, "expires_at") and message.expires_at:
        if now > message.expires_at:
            raise EnvelopeValidationError(
                f"message has expired "
                f"(expires_at={message.expires_at}, now={now})"
            )

    # 5. Identity validation — event author must match the domain identity
    if isinstance(message, PaymentIntent):
        if event.pubkey != message.sender.pubkey:
            raise EnvelopeValidationError(
                f"event pubkey {event.pubkey} does not match "
                f"intent sender {message.sender.pubkey}"
            )
    elif isinstance(message, PaymentQuote):
        if event.pubkey != message.recipient.pubkey:
            raise EnvelopeValidationError(
                f"event pubkey {event.pubkey} does not match "
                f"quote recipient {message.recipient.pubkey}"
            )
    elif isinstance(message, PaymentReceipt):
        if event.pubkey != message.recipient.pubkey:
            raise EnvelopeValidationError(
                f"event pubkey {event.pubkey} does not match "
                f"receipt recipient {message.recipient.pubkey}"
            )

    return message


# ---------------------------------------------------------------------------
# Buzz transport (domain ↔ Buzz boundary)
# ---------------------------------------------------------------------------


class BuzzTransport:
    """Channel-scoped transport boundary between payment domain and Buzz.

    All messages are sent/received through the executor (subprocess seam).
    The transport never touches private keys — signing is inside buzz CLI.

    Parameters
    ----------
    executor : BuzzExecutor
        The subprocess executor (SubprocessExecutor or FakeExecutor).
    channel : str
        Required channel UUID.  All operations are scoped to this channel.
    clock : callable, optional
        Time function for testing (default: time.time).
    """

    def __init__(
        self,
        executor: BuzzExecutor,
        *,
        channel: str,
        clock: Optional[Callable[[], int]] = None,
    ):
        if not channel:
            raise ValueError("channel UUID is required")
        self._executor = executor
        self._channel = channel
        self._clock = clock or (lambda: int(time.time()))

    def send_intent(self, intent: PaymentIntent) -> str:
        """Send a PaymentIntent via Buzz. Returns the event ID."""
        content = encode_content(intent)
        result = self._executor.send(channel=self._channel, content=content)
        return result.event_id

    def send_quote(self, quote: PaymentQuote) -> str:
        """Send a PaymentQuote via Buzz. Returns the event ID."""
        content = encode_content(quote)
        result = self._executor.send(channel=self._channel, content=content)
        return result.event_id

    def send_receipt(self, receipt: PaymentReceipt) -> str:
        """Send a PaymentReceipt via Buzz. Returns the event ID."""
        content = encode_content(receipt)
        result = self._executor.send(channel=self._channel, content=content)
        return result.event_id

    def receive_messages(
        self,
        *,
        since: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[PaymentMessage]:
        """Fetch and validate payment messages from the channel.

        Uses ``buzz messages get --channel <UUID> --kinds 9`` to retrieve
        only kind-9 channel messages.  All received events are treated as
        untrusted and validated (kind, channel, protocol/version, schema,
        expiry, identity) before returning domain objects.
        Invalid events are silently skipped.
        """
        raw_events = self._executor.get(
            channel=self._channel,
            kinds=[WIRE_KIND],
            since=since,
            limit=limit,
        )
        messages: list[PaymentMessage] = []
        for event in raw_events:
            try:
                msg = validate_received_event(
                    event,
                    expected_channel=self._channel,
                    clock=self._clock,
                )
                messages.append(msg)
            except EnvelopeValidationError:
                continue
        return messages
