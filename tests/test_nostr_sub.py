"""Deterministic tests for the optional native Nostr subscriber."""
from __future__ import annotations

import hashlib
import json
from collections import deque

import pytest

from hermes_payments.nostr_sub import NostrSubscription, compute_event_id
from hermes_payments.transport import BuzzTransportError, RawBuzzEvent

secp256k1 = pytest.importorskip("secp256k1")

SECRET = bytes.fromhex("11" * 32)
RELAY = "wss://relay.example"
CHANNEL = "channel-uuid"


def _pubkey(secret: bytes = SECRET) -> str:
    return secp256k1.PrivateKey(secret).pubkey.serialize()[1:].hex()


def _signed_event(
    *,
    kind: int = 9,
    tags: list[list[str]] | None = None,
    content: str = "payload",
    created_at: int = 1_700_000_000,
    secret: bytes = SECRET,
) -> dict:
    event = {
        "pubkey": _pubkey(secret),
        "created_at": created_at,
        "kind": kind,
        "tags": tags or [["h", CHANNEL]],
        "content": content,
    }
    event["id"] = compute_event_id(event)
    event["sig"] = secp256k1.PrivateKey(secret).schnorr_sign(
        bytes.fromhex(event["id"]), None, True
    ).hex()
    return event


class FakeWebSocket:
    def __init__(self, messages: list[list[object]], *, fail_on_empty: Exception | None = None):
        self.messages = deque(json.dumps(message) for message in messages)
        self.sent: list[list[object]] = []
        self.closed = False
        self.fail_on_empty = fail_on_empty

    def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def recv(self, timeout: float | None = None) -> str:
        if self.messages:
            return self.messages.popleft()
        if self.fail_on_empty is not None:
            raise self.fail_on_empty
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def test_compute_event_id_uses_nip01_canonical_serialization() -> None:
    event = {
        "pubkey": "aa" * 32,
        "created_at": 123,
        "kind": 9,
        "tags": [["h", CHANNEL]],
        "content": "hello",
    }
    expected = hashlib.sha256(
        json.dumps(
            [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert compute_event_id(event) == expected


def test_auth_challenge_is_answered_and_signed_event_is_verified() -> None:
    event = _signed_event()
    ws = FakeWebSocket(
        [
            ["AUTH", "challenge-1"],
            ["OK", "auth-event-id", True, ""],
            ["EVENT", "subscription", event],
            ["EOSE", "subscription"],
        ]
    )
    calls: list[tuple[str, str]] = []

    def signer(relay_url: str, challenge: str) -> dict:
        calls.append((relay_url, challenge))
        return _signed_event(
            kind=22242,
            tags=[["relay", relay_url], ["challenge", challenge]],
            content="",
        )

    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        auth_signer=signer,
        connect_factory=lambda _: ws,
    )
    received = subscriber.poll_events(timeout=0.01, since=100)

    assert received == [
        RawBuzzEvent(
            id=event["id"],
            pubkey=event["pubkey"],
            kind=event["kind"],
            content=event["content"],
            tags=event["tags"],
            created_at=event["created_at"],
            sig=event["sig"],
        )
    ]
    assert calls == [(RELAY, "challenge-1")]
    assert [message[0] for message in ws.sent] == ["REQ", "AUTH"]
    assert ws.sent[0][2]["since"] == 100
    assert ws.sent[1][1]["kind"] == 22242


def test_invalid_event_signature_is_dropped() -> None:
    invalid = _signed_event()
    invalid["content"] = "tampered-after-signing"
    valid = _signed_event(content="valid")
    ws = FakeWebSocket(
        [["EVENT", "subscription", invalid], ["EVENT", "subscription", valid]]
    )
    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        connect_factory=lambda _: ws,
    )

    received = subscriber.poll_events(timeout=0.01)

    assert [item.id for item in received] == [valid["id"]]


def test_failed_connection_reconnects_on_next_poll() -> None:
    first = FakeWebSocket([], fail_on_empty=ConnectionError("dropped"))
    event = _signed_event(content="after-reconnect")
    second = FakeWebSocket([["EVENT", "subscription", event]])
    sockets = deque([first, second])

    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        connect_factory=lambda _: sockets.popleft(),
        reconnect_initial_delay=0,
    )

    assert subscriber.poll_events(timeout=0.01) == []
    assert subscriber.poll_events(timeout=0.01)[0].id == event["id"]
    assert first.closed


def test_auth_challenge_without_signer_fails_closed() -> None:
    ws = FakeWebSocket([["AUTH", "challenge-1"]])
    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        connect_factory=lambda _: ws,
    )

    with pytest.raises(BuzzTransportError, match="auth_signer"):
        subscriber.poll_events(timeout=0.01)
    assert ws.closed


def test_invalid_auth_event_closes_connection() -> None:
    ws = FakeWebSocket([["AUTH", "challenge-1"]])

    def signer(relay_url: str, challenge: str) -> dict:
        return _signed_event(
            kind=22242,
            tags=[["relay", relay_url], ["challenge", "wrong-challenge"]],
        )

    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        auth_signer=signer,
        connect_factory=lambda _: ws,
    )

    with pytest.raises(BuzzTransportError, match="wrong challenge"):
        subscriber.poll_events(timeout=0.01)
    assert ws.closed


def test_relay_rejecting_auth_closes_connection() -> None:
    def signer(relay_url: str, challenge: str) -> dict:
        return _signed_event(
            kind=22242,
            tags=[["relay", relay_url], ["challenge", challenge]],
        )

    auth_event = signer(RELAY, "challenge-1")
    ws = FakeWebSocket(
        [["AUTH", "challenge-1"], ["OK", auth_event["id"], False, "bad auth"]]
    )
    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        auth_signer=signer,
        connect_factory=lambda _: ws,
    )

    with pytest.raises(BuzzTransportError, match="authentication rejected"):
        subscriber.poll_events(timeout=0.01)
    assert ws.closed


def test_subscriber_does_not_store_signing_material() -> None:
    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        auth_signer=lambda relay_url, challenge: {},
        connect_factory=lambda _: FakeWebSocket([]),
    )

    for attribute in ("_secret", "_private_key", "_keys", "_sk"):
        assert not hasattr(subscriber, attribute)


def test_close_disables_reconnect() -> None:
    ws = FakeWebSocket([])
    subscriber = NostrSubscription(
        relay_url=RELAY,
        channel=CHANNEL,
        connect_factory=lambda _: ws,
    )
    subscriber.connect()
    subscriber.close()

    assert ws.closed
    assert subscriber.poll_events(timeout=0.01) == []
