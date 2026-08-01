# Hermes-to-Hermes Transport Abstraction Implementation Plan

> **For Hermes:** Implement this plan task-by-task with strict TDD and keep the transport boundary free of Buzz-specific concepts.

**Goal:** Make the Hermes-to-Hermes payment conversation depend on a transport-neutral peer contract, with Buzz as the first adapter and an in-memory transport proving that the business flow does not depend on Buzz.

**Architecture:** `PeerTransport` carries typed payment messages and returns transport metadata without naming Buzz, Nostr, or a relay. `BuzzTransport` adapts its existing kind-9 envelope and validation into that contract. `HermesPeer` is a role-neutral protocol endpoint that composes a `PaymentOrchestrator` with any `PeerTransport`; it owns no transport or settlement implementation details. `InMemoryPeerTransport` provides deterministic two-peer delivery tests without subprocesses or network access.

**Tech Stack:** Python 3.10+, Pydantic v2, `typing.Protocol`, pytest, existing JSON envelope and settlement adapter seams.

---

## Non-negotiable invariants

- `PaymentApproval` never enters `PeerTransport`.
- The payment domain and `HermesPeer` never import Buzz, Nostr, subprocess, or Wavelength.
- Buzz kind 9 remains an adapter detail, not a peer-protocol API.
- Received messages retain a transport message ID for replay/audit handling.
- Existing wire JSON remains compatible with protocol version `1`.
- Existing `BuzzTransport` helpers remain source-compatible while the generic API becomes canonical.

## Implementation tasks

### Task 1: Add the transport-neutral contract

Files:
- Create `src/hermes_payments/peer_transport.py`.
- Modify `src/hermes_payments/models.py` only if needed for the transport-neutral identity alias.
- Test in `tests/test_peer_transport.py`.

Implement:
- `PeerTransport` protocol with `send(message)` and `receive(limit=...)`.
- `PeerMessage` containing `message_id`, typed payment message, author identity, and publication timestamp.
- `PeerTransportError`.
- `message_author()` helper for intent, quote, and receipt.
- `InMemoryTransportHub` and endpoint implementation for deterministic delivery.

TDD:
1. Write tests for delivery, sender metadata, message IDs, independent endpoint inboxes, limit handling, and approval rejection.
2. Run the focused tests and verify RED.
3. Implement the smallest contract and fake transport.
4. Run focused tests, then the existing suite.

### Task 2: Adapt Buzz to the generic contract

Files:
- Modify `src/hermes_payments/transport.py`.
- Test in `tests/test_transport.py`.

Implement:
- `BuzzTransport` conforms to `PeerTransport` through generic `send()` and `receive()` methods.
- `receive()` returns `PeerMessage` metadata after existing kind/channel/schema/expiry/author validation.
- Existing `send_intent`, `send_quote`, `send_receipt`, and `receive_messages` remain compatibility helpers and delegate to the generic surface.
- No Buzz symbol is imported by `peer_transport.py` or `peer.py`.

TDD:
1. Add failing tests proving the generic interface works with `FakeExecutor` and that the returned message ID/author are preserved.
2. Implement the adapter.
3. Run transport tests and the full suite.

### Task 3: Add a role-neutral Hermes peer endpoint

Files:
- Create `src/hermes_payments/peer.py`.
- Test in `tests/test_peer.py`.

Implement `HermesPeer` with explicit, narrow operations:
- `submit_intent()` — local sender validation, policy submit, generic transport send.
- `publish_quote()` — local recipient validation, generic transport send.
- `publish_receipt()` — local recipient validation, generic transport send.
- `receive()` — returns untrusted `PeerMessage` objects; no automatic hidden state mutation.
- `accept_quote()` and `accept_receipt()` — explicit handoff into the injected policy orchestrator.

The endpoint must not know whether transport is Buzz, an HTTP relay, WebSocket, Unix socket, or another future adapter.

### Task 4: Prove Hermes ↔ Hermes without Buzz

Files:
- Create `tests/test_hermes_to_hermes_e2e.py`.
- Optionally refactor shared fixtures only when duplication is proven.

Build two independent `HermesPeer` instances connected by `InMemoryTransportHub` and drive:
- Alice intent → Bob receives;
- Bob quote → Alice accepts;
- Alice prepares and locally approves;
- Alice executes through the existing fake settlement adapter;
- Bob verifies and publishes receipt;
- Alice accepts receipt or confirms the already-settled path.

Add negative coverage for:
- approval attempted through transport;
- wrong local author publishing a quote/receipt;
- duplicate transport delivery retaining distinct transport IDs but policy idempotency remaining safe;
- receipt-mediated reconciliation path.

### Task 5: Update documentation

Files:
- Modify `README.md`, `docs/ARCHITECTURE.md`, `docs/CONTRACT.md`, `docs/FLOWS.md`, `docs/TESTING.md`.
- Add `docs/diagrams/hermes-peer-transport.mmd` if the existing diagrams directory conventions support it.

Document:
- `PeerTransport` as the application boundary.
- Buzz as today's adapter, not the protocol itself.
- In-memory transport as deterministic proof only.
- Future adapter examples without promising an implementation.
- Message IDs and replay handling.

### Task 6: Quality gates and release hygiene

Run:
- `pytest -q`
- `ruff check src tests`
- `mypy src --ignore-missing-imports --no-incremental`
- `vulture src tests --min-confidence 80`
- `codespell`
- package build
- `git diff --check`

Commit the implementation separately from documentation if the diff is large enough to make review clearer. Do not create a new release tag unless the public package version changes.
