# Hermes Payments v0 — Kanban Plan

**Goal:** prove one replay-safe, human-approved Hermes-to-Hermes regtest payment coordinated through signed Buzz events and settled by Wavelength.

## Five gates

1. **Protocol contract** — define versioned intent, quote, approval and receipt schemas; enumerate terminal/error states and invariants.
2. **Core policy engine** — implement rail-neutral state machine, idempotency store and approval binding under tests.
3. **Buzz transport adapter** — map signed Buzz messages/events to the contract without treating them as authorization.
4. **Wavelength adapter** — regtest-only prepare/execute/receipt boundary; credentials stay local and out of messages.
5. **Two-agent E2E + guardian** — two isolated Hermes identities complete one 2,100-sat payment; assert no double-pay and demonstrate failure/expiry handling.

## Exit criteria

- Replaying the same intent cannot settle twice.
- Approval for one quote/prepared payload cannot authorize another.
- A receipt links to a verifiable settlement reference.
- Full event/audit chain reconstructs the transaction.
- No secret appears in test fixtures, logs or Buzz payloads.
