"""Native Nostr WebSocket subscription for the Buzz transport.

Nostr is event-driven: a client opens a persistent WebSocket (NIP-01) and the
relay PUSHES matching events as they arrive. This module implements the
NIP-01 ``REQ`` subscription plus NIP-42 ``AUTH`` (challenge/response signed
with Schnorr) so the hosted Buzz relay accepts the subscription and streams
kind-9 channel events in real time — no polling, no history accumulation.

The subscriber is synchronous on top of ``websockets.sync``: it holds one
connection, and ``poll_events()`` reads whatever events the relay has pushed
since the last call (with a short timeout). This keeps the transport's
receive path simple while remaining genuinely push-based on the wire.

Security: this module signs the NIP-42 challenge only (a single ephemeral
AUTH event per connection). It never reads or logs the private key beyond
signing, and never constructs payment messages — it only yields raw events
for the existing validation layer.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import List, Optional

from .transport import BuzzTransportError, RawBuzzEvent


def _nsec_to_secret(nsec_or_hex: str) -> bytes:
    """Decode a Nostr secret (nsec bech32 or 64-char hex) to 32 raw bytes."""
    s = nsec_or_hex.strip()
    if s.startswith("nsec1"):
        import bech32

        hrp, data = bech32.bech32_decode(s)
        if hrp != "nsec":
            raise BuzzTransportError("invalid nsec secret")
        bits = bech32.convertbits(data, 5, 8, False)
        if bits is None:
            raise BuzzTransportError("invalid nsec encoding")
        return bytes(bits)
    if len(s) == 64:
        try:
            return bytes.fromhex(s)
        except ValueError as e:
            raise BuzzTransportError("invalid hex secret") from e
    raise BuzzTransportError("secret must be nsec or 64-char hex")


def _event_id(event: dict) -> str:
    """Compute the Nostr event id (sha256 of the canonical serialization)."""
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def _sign_nip42(secret: bytes, challenge: str, relay_url: str) -> dict:
    """Build and sign a NIP-42 AUTH event (kind 22242)."""
    import secp256k1

    pubkey = secp256k1.PrivateKey(secret).pubkey
    pub_hex = pubkey.serialize()[1:].hex() if pubkey is not None else ""  # x-only
    created = int(time.time())
    tags = [["relay", relay_url], ["challenge", challenge]]
    event = {
        "pubkey": pub_hex,
        "created_at": created,
        "kind": 22242,
        "tags": tags,
        "content": challenge,
    }
    event["id"] = _event_id(event)
    digest = bytes.fromhex(event["id"])
    event["sig"] = secp256k1.PrivateKey(secret).schnorr_sign(digest, None, True).hex()
    return event


class NostrSubscription:
    """A persistent NIP-01 subscription with NIP-42 auth to a Buzz relay.

    Parameters
    ----------
    relay_url : str
        WebSocket relay URL (e.g. ``wss://host``).
    channel : str
        NIP-29 channel UUID to subscribe to.
    secret : str
        The identity's Nostr secret (nsec or hex) used to sign NIP-42 auth.
    kinds : list[int], optional
        Event kinds to subscribe to. Defaults to kind 9 (channel messages).
    """

    def __init__(
        self,
        relay_url: str,
        channel: str,
        secret: str,
        kinds: Optional[List[int]] = None,
    ) -> None:
        self._relay = relay_url
        self._channel = channel
        self._secret = secret
        self._kinds = kinds or [9]
        self._ws = None
        self._sub_id = f"hermes-payments-{int(time.time()*1000)}"

    def connect(self) -> None:
        """Open the WebSocket, complete NIP-42 auth, and send the REQ."""
        from websockets.sync.client import connect

        ws_url = self._relay.replace("http://", "ws://").replace("https://", "wss://")
        self._ws = connect(ws_url, open_timeout=15)
        secret = _nsec_to_secret(self._secret)

        # 1. The relay sends an AUTH challenge first (NIP-42).
        challenge = None
        try:
            first = json.loads(self._ws.recv(timeout=15))
            if first and first[0] == "AUTH":
                challenge = first[1]
        except Exception:
            challenge = None

        if challenge:
            auth_event = _sign_nip42(secret, challenge, self._relay)
            self._ws.send(json.dumps(["AUTH", auth_event]))

        # 2. Subscribe: kind-9 for the channel.
        self._ws.send(
            json.dumps(
                ["REQ", self._sub_id, {"kinds": self._kinds, "#h": [self._channel]}]
            )
        )

    def poll_events(self, timeout: float = 0.2) -> List[RawBuzzEvent]:
        """Read any events the relay has pushed since the last call.

        Returns
        -------
        list[RawBuzzEvent]
            Raw events received (empty if none yet). Deduplicates by event id.
        """
        if self._ws is None:
            return []
        events: List[RawBuzzEvent] = []
        seen: set[str] = set()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = json.loads(self._ws.recv(timeout=max(0.05, deadline - time.monotonic())))
            except Exception:
                break
            if not isinstance(msg, list) or not msg:
                continue
            kind = msg[0]
            if kind == "EVENT":
                ev = msg[2]
                eid = ev.get("id", "")
                if eid in seen:
                    continue
                seen.add(eid)
                events.append(
                    RawBuzzEvent(
                        id=eid,
                        pubkey=ev.get("pubkey", ""),
                        kind=ev.get("kind", 0),
                        content=ev.get("content", ""),
                        tags=ev.get("tags", []),
                        created_at=ev.get("created_at", 0),
                    )
                )
            elif kind == "EOSE":
                continue
            elif kind in ("NOTICE", "CLOSED"):
                break
        return events

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
