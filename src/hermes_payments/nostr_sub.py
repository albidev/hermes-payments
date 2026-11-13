"""Optional native Nostr subscription for Buzz-compatible relays.

This module is deliberately a transport-only boundary:

* it never accepts, reads, logs, or stores a private key;
* NIP-42 authentication is delegated to an injected ``auth_signer``;
* every received event is checked against its NIP-01 event id and Schnorr
  signature before it is exposed to the payment transport;
* a broken relay connection is closed and retried with bounded backoff;
* ``close()`` permanently disables reconnects for the instance.

The normal Hermes Payments plugin still uses the Buzz CLI.  This subscriber is
available to integrations that already have a safe external signing boundary.
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any, Optional

from .transport import BuzzTransportError, RawBuzzEvent

AuthSigner = Callable[[str, str], Mapping[str, Any]]
ConnectFactory = Callable[[str], Any]


def compute_event_id(event: Mapping[str, Any]) -> str:
    """Compute the NIP-01 event id from its canonical serialization."""
    try:
        canonical = [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"],
        ]
    except KeyError as exc:
        raise BuzzTransportError(f"Nostr event missing {exc.args[0]!r}") from exc
    encoded = json.dumps(
        canonical,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def verify_event_signature(event: Mapping[str, Any]) -> bool:
    """Verify the NIP-01 id and BIP-340 Schnorr signature of an event."""
    try:
        import secp256k1
    except ImportError as exc:
        raise BuzzTransportError(
            "native Nostr verification requires the 'native-nostr' extra"
        ) from exc

    event_id_value = event.get("id")
    pubkey_value = event.get("pubkey")
    signature_value = event.get("sig")
    if (
        not isinstance(event_id_value, str)
        or not isinstance(pubkey_value, str)
        or not isinstance(signature_value, str)
    ):
        return False
    event_id = event_id_value
    pubkey = pubkey_value
    signature = signature_value
    if len(event_id) != 64 or len(pubkey) != 64 or len(signature) != 128:
        return False
    try:
        if any(c not in "0123456789abcdef" for c in event_id + pubkey + signature):
            return False
        if compute_event_id(event) != event_id:
            return False
        # Nostr publishes the x-only BIP-340 pubkey.  The compressed prefix is
        # ignored by Schnorr verification; using 02 selects the x-only point.
        public_key = secp256k1.PublicKey(bytes.fromhex("02" + pubkey), raw=True)
        return bool(
            public_key.schnorr_verify(
                bytes.fromhex(event_id),
                bytes.fromhex(signature),
                None,
                True,
            )
        )
    except (KeyError, TypeError, ValueError, RuntimeError):
        return False


def _tag_value(tags: Any, name: str) -> Optional[str]:
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            value = tag[1]
            return value if isinstance(value, str) else None
    return None


def _default_connect(relay_url: str) -> Any:
    """Create a synchronous WebSocket connection lazily."""
    from websockets.sync.client import connect

    return connect(relay_url, open_timeout=15, close_timeout=5)


class NostrSubscription:
    """Persistent NIP-01 subscription with NIP-42 challenge handling.

    ``auth_signer`` is intentionally a callback instead of a private-key
    argument. It receives ``(relay_url, challenge)`` and returns a complete,
    signed kind-22242 event. The callback belongs to the caller's approved
    signing boundary (for example, Buzz/ACP); this class never sees a secret.
    """

    def __init__(
        self,
        *,
        relay_url: str,
        channel: str,
        kinds: Optional[list[int]] = None,
        auth_signer: Optional[AuthSigner] = None,
        connect_factory: Optional[ConnectFactory] = None,
        reconnect_initial_delay: float = 0.5,
        reconnect_max_delay: float = 30.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not relay_url.startswith(("ws://", "wss://")):
            raise ValueError("relay_url must use ws:// or wss://")
        if not channel:
            raise ValueError("channel is required")
        effective_kinds = list(kinds or [9])
        if not effective_kinds or any(
            not isinstance(kind, int) or kind < 0 for kind in effective_kinds
        ):
            raise ValueError("kinds must contain at least one non-negative integer")
        if reconnect_initial_delay < 0 or reconnect_max_delay < reconnect_initial_delay:
            raise ValueError("invalid reconnect backoff bounds")

        self._relay_url = relay_url
        self._channel = channel
        self._kinds = effective_kinds
        self._auth_signer = auth_signer
        self._connect_factory = connect_factory or _default_connect
        self._initial_delay = reconnect_initial_delay
        self._max_delay = reconnect_max_delay
        self._clock = clock or time.monotonic
        self._ws: Any = None
        self._closed = False
        self._subscription_id = ""
        self._current_since: Optional[int] = None
        self._next_connect_at = 0.0
        self._backoff = reconnect_initial_delay
        self._last_auth_event_id: Optional[str] = None

    @property
    def connected(self) -> bool:
        """Whether the subscriber currently owns an open WebSocket."""
        return self._ws is not None and not self._closed

    def connect(self, *, since: Optional[int] = None) -> None:
        """Open a connection and send the initial REQ filter."""
        if self._closed:
            raise BuzzTransportError("NostrSubscription is closed")
        if self._ws is not None:
            self._set_subscription_filter(since)
            return
        try:
            self._ws = self._connect_factory(self._relay_url)
            self._subscription_id = f"hermes-payments-{uuid.uuid4().hex}"
            self._current_since = since
            self._send_req()
            self._backoff = self._initial_delay
            self._next_connect_at = self._clock()
        except Exception as exc:
            self._disconnect(schedule_retry=True)
            raise BuzzTransportError(f"Nostr relay connection failed: {exc}") from exc

    def poll_events(
        self,
        *,
        timeout: float = 0.2,
        since: Optional[int] = None,
    ) -> list[RawBuzzEvent]:
        """Read pushed events, reconnecting after a dropped connection."""
        if self._closed:
            return []
        if self._ws is None:
            if self._clock() < self._next_connect_at:
                return []
            try:
                self.connect(since=since)
            except BuzzTransportError:
                return []
        else:
            self._set_subscription_filter(since)

        events: list[RawBuzzEvent] = []
        deadline = self._clock() + max(timeout, 0.0)
        while self._ws is not None:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                message = self._ws.recv(timeout=max(0.01, remaining))
            except TimeoutError:
                break
            except (OSError, EOFError):
                self._disconnect(schedule_retry=True)
                break
            except Exception:
                # WebSocket implementations use different exception classes;
                # an unexpected receive failure is still a broken connection.
                self._disconnect(schedule_retry=True)
                break

            parsed = self._parse_message(message)
            if parsed is None:
                continue
            message_type = parsed[0]
            if message_type == "AUTH":
                self._answer_auth(parsed)
                continue
            if message_type == "EVENT":
                event = self._parse_event(parsed)
                if event is not None:
                    events.append(event)
                continue
            if message_type == "OK":
                self._handle_ok(parsed)
                continue
            if message_type in {"EOSE", "NOTICE"}:
                continue
            if message_type == "CLOSED":
                self._disconnect(schedule_retry=True)
                break
        return events

    def close(self) -> None:
        """Close the WebSocket and permanently disable reconnects."""
        self._closed = True
        self._disconnect(schedule_retry=False)

    def _send_req(self) -> None:
        if self._ws is None:
            return
        filters: dict[str, Any] = {
            "kinds": self._kinds,
            "#h": [self._channel],
        }
        if self._current_since is not None:
            filters["since"] = self._current_since
        self._ws.send(json.dumps(["REQ", self._subscription_id, filters], separators=(",", ":")))

    def _set_subscription_filter(self, since: Optional[int]) -> None:
        if since == self._current_since:
            return
        if self._ws is None:
            return
        old_id = self._subscription_id
        self._ws.send(json.dumps(["CLOSE", old_id], separators=(",", ":")))
        self._subscription_id = f"hermes-payments-{uuid.uuid4().hex}"
        self._current_since = since
        self._send_req()

    def _answer_auth(self, message: list[Any]) -> None:
        if len(message) < 2 or not isinstance(message[1], str):
            raise BuzzTransportError("relay sent a malformed AUTH challenge")
        if self._auth_signer is None:
            self._disconnect(schedule_retry=False)
            raise BuzzTransportError("relay requires NIP-42 auth_signer")
        challenge = message[1]
        try:
            auth_event = dict(self._auth_signer(self._relay_url, challenge))
        except Exception as exc:
            self._disconnect(schedule_retry=False)
            raise BuzzTransportError(f"NIP-42 auth signer failed: {exc}") from exc
        try:
            self._validate_auth_event(auth_event, challenge)
        except BuzzTransportError:
            self._disconnect(schedule_retry=False)
            raise
        self._last_auth_event_id = str(auth_event["id"])
        if self._ws is not None:
            self._ws.send(json.dumps(["AUTH", auth_event], separators=(",", ":")))

    def _validate_auth_event(self, event: Mapping[str, Any], challenge: str) -> None:
        if event.get("kind") != 22242:
            raise BuzzTransportError("NIP-42 signer returned the wrong event kind")
        if _tag_value(event.get("tags"), "relay") != self._relay_url:
            raise BuzzTransportError("NIP-42 auth event has the wrong relay tag")
        if _tag_value(event.get("tags"), "challenge") != challenge:
            raise BuzzTransportError("NIP-42 auth event has the wrong challenge tag")
        if not verify_event_signature(event):
            raise BuzzTransportError("NIP-42 signer returned an invalid event signature")

    def _handle_ok(self, message: list[Any]) -> None:
        if len(message) < 3 or message[1] != self._last_auth_event_id:
            return
        if message[2] is not True:
            reason = message[3] if len(message) > 3 else "relay rejected AUTH"
            self._disconnect(schedule_retry=False)
            raise BuzzTransportError(f"NIP-42 authentication rejected: {reason}")

    def _parse_message(self, message: Any) -> Optional[list[Any]]:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        if not isinstance(message, str):
            return None
        try:
            parsed = json.loads(message)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, list) and parsed and isinstance(parsed[0], str) else None

    def _parse_event(self, message: list[Any]) -> Optional[RawBuzzEvent]:
        if len(message) < 3 or not isinstance(message[2], Mapping):
            return None
        event = message[2]
        if event.get("kind") not in self._kinds:
            return None
        if _tag_value(event.get("tags"), "h") != self._channel:
            return None
        if not verify_event_signature(event):
            return None
        try:
            return RawBuzzEvent(
                id=str(event["id"]),
                pubkey=str(event["pubkey"]),
                kind=int(event["kind"]),
                content=str(event["content"]),
                tags=[list(tag) for tag in event["tags"]],
                created_at=int(event["created_at"]),
                sig=str(event["sig"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _disconnect(self, *, schedule_retry: bool) -> None:
        ws, self._ws = self._ws, None
        self._subscription_id = ""
        self._current_since = None
        self._last_auth_event_id = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if schedule_retry and not self._closed:
            self._next_connect_at = self._clock() + self._backoff
            if self._backoff > 0:
                self._backoff = min(self._max_delay, max(self._initial_delay, self._backoff * 2))
