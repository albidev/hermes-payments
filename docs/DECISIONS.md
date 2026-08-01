# Architecture Decision Records

## ADR-001 — Keep the protocol rail-independent

**Status:** Accepted

The core models intent, quote, approval, receipt, policy, and lifecycle. Wavelength is an adapter, not the protocol itself. This allows future Lightning, Ark, or other adapters without moving wallet code into the policy engine.

## ADR-002 — PaymentApproval is local-only

**Status:** Accepted

Remote messages can request, quote, and report a payment. Only an explicit local human action can authorize execution. `PaymentApproval` has no transport message kind and `encode_content()` rejects it.

## ADR-003 — Use NIP-29 kind-9 channel messages

**Status:** Accepted

Payments are coordination messages inside a Buzz channel. The transport uses the actual Buzz channel command surface and one NIP-29 kind-9 event kind with a versioned content discriminator. Custom payment event kinds were rejected because they duplicated channel semantics and coupled the protocol to an unverified event contract.

## ADR-004 — Bind approval to the exact prepared intent

**Status:** Accepted

The high-level `wavecli send` command prepares internally. A prepare/approve/execute flow using that command could approve one intent and execute another. Hermes Payments uses raw `PrepareSend` and raw `Send` so the single-use `send_intent_id` survives the approval boundary.

## ADR-005 — Verify receipts on the recipient side

**Status:** Accepted

The recipient verifies its own incoming activity (`activity --kind recv`) by settlement reference and amount. Sender-side “send succeeded” output is not sufficient evidence for a recipient receipt.

## ADR-006 — Reject non-regtest Wavelength configuration

**Status:** Accepted

The adapter rejects `mainnet`, `testnet`, `signet`, and arbitrary network names. Network expansion requires a separate explicit gate, documentation, and tests.

## ADR-007 — Treat ambiguity as reconciliation

**Status:** Accepted

A timeout or unknown result during execution may mean the daemon accepted the payment. The state machine enters `RECONCILIATION_REQUIRED`; retries are forbidden until a human or verified receipt resolves the outcome.

## ADR-008 — Distinguish bootstrap funding from settlement

**Status:** Accepted

A fresh Ark-capable wallet may need an on-chain deposit to board an operator or acquire a VTXO. That is initial liquidity, not the agent-to-agent payment. Documentation keeps the two flows separate.

## ADR-009 — Do not expose Ark as a fake wire rail

**Status:** Accepted / follow-up open

Wavelength can internally choose an Ark route for a Lightning invoice. The current protocol still exposes `Rail.LIGHTNING` and an invoice receive instruction. First-class Ark wire semantics require their own instruction, policy, fee, receipt, compatibility, and recovery contracts.

## ADR-010 — Put a transport-neutral peer contract in front of Buzz

**Status:** Accepted

The Hermes-to-Hermes application flow depends on `PeerTransport`, not on Buzz commands or NIP-29 event details. `PeerMessage` carries the typed payment message plus transport ID, author, and publication timestamp. Buzz is the first concrete adapter and remains fully validated at its boundary; a future HTTP, WebSocket, Unix-socket, or other adapter can replace it without changing policy or peer orchestration. An in-memory transport proves the composition without pretending to be a live relay.
