# Hermes Payments — Protocol Contract v1

> **Status:** Implementation-ready v0 contract.
> **Scope:** Regtest-only vertical slice between two Hermes identities.
> **Non-negotiable:** A Buzz message/event is never, by itself, financial authorisation.

---

## 1. Protocol Overview

Hermes Payments is a **rail-independent** payment protocol. The core
lives between sender and recipient identities; settlement adapters
(Wavelength, future rails) execute the actual funds movement.

```
Sender (Hermes A)                    Buzz                     Recipient (Hermes B)
      |                               |                              |
      |--- PaymentIntent ────────────>|--- PaymentIntent ──────────>|
      |                               |<-- PaymentQuote ───────────-|
      |<-- PaymentQuote ─────────────|                              |
      |                               |                              |
      |  [adapter.prepare()]          |                              |
      |--- PaymentApproval ──────────>|--- PaymentApproval ────────>|
      |    (intent, quote,            |                              |
      |     prepared_hash)            |    [adapter.execute()]       |
      |                               |<-- PaymentReceipt ─────────-|
      |<-- PaymentReceipt ───────────|                              |
```

### Design principles

1. **Buzz is coordination/audit transport** — do not couple business
   state to a specific Nostr event kind until the contract is proven.
2. **Adapters are pluggable** — the protocol never calls Wavelength
   (or any rail) directly.
3. **Human approval is explicit** — every outgoing payment requires
   local human approval of `(intent_id, quote_id, prepared_hash)`.
4. **Fail-closed on ambiguity** — ambiguous settlement results are
   never retried automatically.

---

## 2. Domain Messages

### 2.1 PaymentIntent

Sender-initiated request to pay a recipient.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `protocol_version` | `"1"` | yes | Protocol version (currently `"1"`) |
| `id` | string | yes | Deterministic: `sha256(canonical_form)` (see §5) |
| `idempotency_key` | string | yes | Sender-assigned unique key (1–128 chars) |
| `sender` | BuzzIdentity | yes | Sender's Buzz pubkey |
| `recipient` | BuzzIdentity | yes | Recipient's Buzz pubkey |
| `amount_sat` | int | yes | Payment amount in satoshis (>0) |
| `purpose` | string | yes | Human-readable purpose (1–512 chars) |
| `max_fee_sat` | int | yes | Maximum acceptable fee (≥0) |
| `expires_at` | int | yes | Unix epoch seconds; void after this |
| `created_at` | int | yes | Unix epoch seconds |

**Idempotency:** Two intents with the same `id` are the same intent.
Duplicate submission is a no-op. The `id` is derived from
`(protocol_version, sender, recipient, amount_sat, purpose,
idempotency_key)`.

### 2.2 PaymentQuote

Recipient's response to an accepted intent. Locks a rail and receive
instruction.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `protocol_version` | `"1"` | yes | Protocol version |
| `id` | string | yes | Deterministic ID |
| `intent_id` | string | yes | References `PaymentIntent.id` |
| `quote_id` | string | yes | Recipient-assigned quote identifier |
| `recipient` | BuzzIdentity | yes | Recipient's Buzz pubkey |
| `receive_instruction` | RailReceiveInstruction | yes | Rail-specific receive details |
| `fee_sat` | int | yes | Quoted fee (≥0) |
| `fee_constraint` | `"exact"` \| `"max"` | yes | Whether fee is fixed or a maximum |
| `expires_at` | int | yes | Unix epoch seconds |
| `created_at` | int | yes | Unix epoch seconds |

**RailReceiveInstruction (v0):**

| Field | Type | Description |
|-------|------|-------------|
| `rail` | `"lightning"` | Settlement rail |
| `invoice` | string \| null | Lightning invoice (bolt11) |

### 2.3 PaymentApproval

Human approval binding. This is the **only** message that authorises
execution.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `protocol_version` | `"1"` | yes | Protocol version |
| `id` | string | yes | Deterministic ID |
| `intent_id` | string | yes | References `PaymentIntent.id` |
| `quote_id` | string | yes | References `PaymentQuote.quote_id` |
| `prepared_hash` | string | yes | SHA-256 of the prepared payload |
| `approver` | BuzzIdentity | yes | Approving identity's Buzz pubkey |
| `created_at` | int | yes | Unix epoch seconds |

**Invariant:** Each `(intent_id, quote_id, prepared_hash)` tuple may
be approved at most once. Replaying the same approval is rejected.

### 2.4 PaymentReceipt

Settlement confirmation produced after successful execution.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `protocol_version` | `"1"` | yes | Protocol version |
| `id` | string | yes | Deterministic ID |
| `intent_id` | string | yes | References `PaymentIntent.id` |
| `quote_id` | string | yes | References `PaymentQuote.quote_id` |
| `settlement_ref` | string | yes | Rail-specific reference (e.g. payment_hash) |
| `amount_sat` | int | yes | Settled amount (>0) |
| `fee_sat` | int | yes | Actual fee paid (≥0) |
| `rail` | `"lightning"` | yes | Settlement rail used |
| `settled_at` | int | yes | Unix epoch seconds of settlement |
| `created_at` | int | yes | Unix epoch seconds |

---

## 3. State Machine

Each `PaymentIntent` follows a deterministic lifecycle:

```
DRAFT ──submit──> SUBMITTED ──quote_received──> QUOTED ──prepared──> PREPARED ──approved──> APPROVED ──executing──> EXECUTING ──settled──> SETTLED
  │                  │              │                │                │
  │cancel            │cancel        │cancel          │cancel          │
  v                  v              v                v                v
CANCELLED          CANCELLED     CANCELLED        CANCELLED        EXPIRED / FAILED
                   │              │                │
                   │rejected      │rejected        │rejected
                   v              v                v
                 REJECTED       REJECTED         REJECTED
```

### Terminal states

No further transitions are possible from:

- `SETTLED` — payment completed
- `FAILED` — adapter error
- `REJECTED` — recipient or human rejection
- `EXPIRED` — intent/quote expiry
- `CANCELLED` — sender cancellation

### Valid transitions

| From | Trigger | To |
|------|---------|----|
| DRAFT | `submit` | SUBMITTED |
| SUBMITTED | `quote_received` | QUOTED |
| QUOTED | `prepared` | PREPARED |
| PREPARED | `approved` | APPROVED |
| APPROVED | `executing` | EXECUTING |
| EXECUTING | `settled` | SETTLED |
| DRAFT–PREPARED | `cancel` | CANCELLED |
| SUBMITTED–APPROVED | `rejected` | REJECTED |
| SUBMITTED–EXECUTING | `expired` | EXPIRED |
| QUOTED–EXECUTING | `adapter_error` | FAILED |

### Invariant: approval requires prepare

`APPROVED` can only be reached from `PREPARED`. The adapter must have
run `prepare()` before human approval is possible.

---

## 4. Idempotency & Replay Rules

### Intent idempotency

- The `id` is deterministic: same fields → same `id`.
- Submitting the same intent twice is a no-op (same `id`, same state).
- Different `idempotency_key` values produce different `id` values,
  even if all other fields are identical.

### Quote binding

- Each quote carries `intent_id` — it belongs to exactly one intent.
- A new quote from the recipient replaces any previous quote for the
  same intent (latest-wins).

### Approval binding

- The approval binds `(intent_id, quote_id, prepared_hash)`.
- Each triple may be approved at most once.
- A different `prepared_hash` requires a new approval (the adapter
  must re-prepare).

### Receipt uniqueness

- Each receipt carries `intent_id` and `settlement_ref`.
- The state machine transitions to `SETTLED` exactly once per intent.
- A second receipt for the same intent is rejected (terminal state).

---

## 5. Canonical Serialisation & Hashing

### Canonical form

Domain messages are serialised to canonical JSON for ID computation:

1. `model_dump(exclude_none=True)` — Pydantic v2 dict, no None values
2. **Exclude the `id` field** (id = hash of everything else)
3. Sort all dict keys lexicographically (recursively)
4. Compact JSON: `separators=(",", ":")` — no whitespace
5. UTF-8 encoding

```
canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

### ID computation

```
id = sha256(canonical_bytes).hexdigest()
```

The `id` is a 64-character lowercase hex SHA-256 hash of the canonical
form (excluding the `id` field itself).

### Prepared hash

The adapter's `prepare()` returns an opaque `prepared_payload` bytes.
The human approves `prepared_hash = sha256(prepared_payload).hexdigest()`.

---

## 6. Buzz Transport Envelope Mapping

Each domain message maps to a signed Buzz/Nostr event:

```json
{
  "id": "<sha256 of serialised event>",
  "pubkey": "<author's Schnorr pubkey>",
  "kind": 40100,
  "tags": [["h", "hermes-payments"], ["intent", "<intent_id>"], ...],
  "content": "<JSON-encoded PaymentMessage>",
  "sig": "<Schnorr signature>"
}
```

### Event kinds (Buzz custom range 40000–49999)

| Kind | Message | Description |
|------|---------|-------------|
| 40100 | PaymentIntent | Sender-initiated payment request |
| 40101 | PaymentQuote | Recipient's quote with receive instruction |
| 40102 | PaymentApproval | Human approval binding |
| 40103 | PaymentReceipt | Settlement confirmation |

### Tag conventions

Every payment event includes:

- `["h", "hermes-payments"]` — community/protocol identifier
- `["protocol", "hermes-payments-v1"]` — protocol version

Message-specific tags:

| Message | Additional tags |
|---------|----------------|
| Intent | `["intent", id]`, `["p", recipient_pubkey]` |
| Quote | `["intent", intent_id]`, `["quote", quote_id]`, `["p", recipient_pubkey]` |
| Approval | `["intent", intent_id]`, `["quote", quote_id]` |
| Receipt | `["intent", intent_id]`, `["settlement", settlement_ref]` |

### Signing model

- **Buzz signs** all events (the relay enforces valid signatures).
- **Hermes Payments never signs** — it produces the content and
  tags; the Buzz transport layer handles cryptographic signing.
- Credentials (TLS, macaroons, seeds) never transit Buzz events.

### Status events

State transitions emit lightweight `StatusEvent` objects for the
audit trail. These are separate Buzz events (not PaymentMessages)
recording `(intent_id, old_state, new_state, trigger, timestamp)`.

---

## 7. Adapter Boundary

### SettlementAdapter interface

Settlement adapters implement the `SettlementAdapter` ABC:

```python
class SettlementAdapter(ABC):
    @property
    def rail(self) -> Rail: ...

    def prepare(
        self,
        receive_instruction: RailReceiveInstruction,
        amount_sat: int,
        max_fee_sat: int,
    ) -> PrepareResult: ...

    def execute(
        self,
        prepared_payload: bytes,
        prepared_hash: str,
    ) -> ExecuteResult: ...

    def verify_receipt(
        self,
        settlement_ref: str,
        expected_amount_sat: int,
    ) -> ReceiptVerifyResult: ...
```

### Adapter contract

| Rule | Description |
|------|-------------|
| Non-mutating prepare | `prepare()` must not move funds |
| One execution | `execute()` called exactly once per `prepared_hash` |
| No automatic retry | Ambiguous results raise `AmbiguousResult`; human investigates |
| No credential transit | Seeds, passwords, macaroons never leave the adapter |
| No protocol knowledge | Adapter knows rail instructions, not intent/approval |

### Wavelength adapter mapping (v0)

| Generic operation | Wavelength MCP tool |
|-------------------|---------------------|
| `prepare()` | `send.prepare` (dry run) |
| `execute()` | `send` with `--yes` |
| `verify_receipt()` | `activity` + `balance` |

The adapter calls `wavecli --no-tls --no-macaroons --network=regtest`
for regtest. Credentials stay local and are never passed through
Buzz events or the adapter boundary.

### Error handling

| Error | State transition | Behaviour |
|-------|-----------------|-----------|
| `AdapterError` | → FAILED | Human must investigate |
| `AmbiguousResult` | → EXPIRED | No retry; human investigates |
| Adapter crash | → FAILED or stays in EXECUTING | Recovery via state persistence |

---

## 8. Security Invariants

1. **Regtest only** until explicit human approval of a future gate.
2. **Never** read, log, transmit, or request seeds, passwords,
   macaroons, private keys, or API tokens.
3. **Buzz message ≠ authorisation.** Execution requires explicit local
   human approval of `(intent_id, quote_id, prepared_hash)`.
4. **Every intent** has expiry, `max_fee_sat`, recipient identity,
   and idempotency key.
5. **Ambiguous settlement = fail-closed.** No automatic retry.
6. **No mainnet configuration, no autonomous spending, no external
   publication.**

---

## 9. Testable Invariants

The following invariants are validated by `tests/test_contract_invariants.py`:

| Category | Invariant | Test |
|----------|-----------|------|
| State machine | Happy path DRAFT→SETTLED completes | `test_happy_path` |
| State machine | SETTLED is terminal (no transitions out) | `test_settled_is_terminal` |
| State machine | All terminal states reject all triggers | `test_all_terminal_states_are_absorbing` |
| State machine | Cancel works from DRAFT–PREPARED only | `test_cancellation_from_active_states` |
| State machine | Cannot cancel after approval | `test_cannot_cancel_from_approved` |
| State machine | Approval requires prepare | `test_approval_requires_prepare` |
| State machine | Adapter errors → FAILED | `test_adapter_error_from_active` |
| Idempotency | Same fields → same ID | `test_deterministic_id` |
| Idempotency | Different key → different ID | `test_different_idempotency_key_different_id` |
| Idempotency | Different amount → different ID | `test_different_amount_different_id` |
| Idempotency | Quote tied to intent | `test_quote_tied_to_intent` |
| Idempotency | Approval binds triple | `test_approval_binds_triple` |
| Idempotency | No replay after settle | `test_no_replay_settled` |
| Serialization | Canonical bytes deterministic | `test_deterministic_canonical_bytes` |
| Serialization | ID = SHA-256 of canonical form | `test_id_matches_canonical_hash` |
| Serialization | None values excluded | `test_none_values_excluded` |
| Envelope | Round-trip for all message types | `test_*_round_trip` |
| Envelope | Non-payment kind rejected | `test_non_payment_kind_rejected` |
| Envelope | Kind constants in Buzz custom range | `test_kind_constants_are_in_buzz_custom_range` |
| Adapter | Wavelength handles LIGHTNING | `test_wavelength_rail_is_lightning` |
| Adapter | AmbiguousResult is AdapterError subclass | `test_ambiguous_result_is_adapter_error` |
| Version | All messages share version `"1"` | `test_all_messages_share_version` |
| Version | Version change → different ID | `test_version_affects_id` |

---

## 10. File Layout

```
src/hermes_payments/
├── __init__.py          # Package, version
├── models.py            # Domain schemas (PaymentIntent, etc.)
├── state_machine.py     # State transitions and terminal states
├── envelope.py          # Buzz envelope encoding/decoding
└── adapter.py           # SettlementAdapter ABC + Wavelength stub

tests/
├── test_contract_invariants.py   # 34 invariant tests
└── fixtures/
    └── __init__.py               # Deterministic test fixtures

docs/
├── CONTRACT.md          # This document
└── PLAN.md              # Project plan (gate 1 of 5)
```

---

## 11. Implementation Roadmap

This contract (Gate 1) is complete. Next gates:

| Gate | Scope | Status |
|------|-------|--------|
| **1. Protocol contract** | This document + testable invariants | ✅ Done |
| 2. Core policy engine | State machine implementation, idempotency store, approval binding | Pending |
| 3. Buzz transport adapter | Map signed Buzz messages to contract | Pending |
| 4. Wavelength adapter | Regtest prepare/execute/receipt with live daemon | Pending |
| 5. Two-agent E2E + guardian | Full vertical slice with no double-pay | Pending |
