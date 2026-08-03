# Testing and Verification

The project separates deterministic correctness from live operational correctness.

## Test layers

### Contract invariants

`tests/test_contract_invariants.py` covers canonical IDs, model round trips, terminal states, approval binding, `PaymentApproval` transport exclusion, and envelope kind/protocol checks.

### Policy core

`tests/test_policy_core.py` covers expiry and allowlists, durable idempotency, audit sequencing, fee constraints, execution transitions, ambiguous results, read-only reconciliation, bounded polling, receipt replay, and mismatch behavior.

### Transport-neutral peer boundary

`tests/test_peer_transport.py` covers the `PeerTransport` contract, in-memory endpoint isolation, author mapping, transport IDs, delivery limits, duplicate delivery, and the local-only approval invariant.

`tests/test_peer.py` covers the role-neutral `HermesPeer` endpoint, explicit policy handoffs, local authorship checks, and receipt acceptance.

### Buzz transport adapter

`tests/test_transport.py` covers NIP-29 kind-9 channel scoping, versioned content envelopes, malformed and expired messages, wrong channel or event kind, author mismatch, and no serialized approval.

### Wavelength adapter

`tests/test_wavelength_adapter.py` covers explicit regtest/Signet construction,
unsupported-network rejection, injection-safe command lists, raw `PrepareSend`
and `Send`, exact prepared intent binding, fee/amount/expiry/payment-hash
validation, strict status parsing, recipient-side `activity --kind recv`,
sender-side `activity --kind send`, read-only settlement reconciliation,
`COMPLETE`/`PENDING`/`UNKNOWN` recovery outcomes, both `activity.entries` and
top-level `recent` JSON envelopes, redaction, and ambiguous outcomes.

### Two-Hermes composition

`tests/test_hermes_to_hermes_e2e.py` composes two independent `HermesPeer` stacks over `InMemoryTransportHub`, with no Buzz or network dependency. It demonstrates adapter-complete settlement and receipt-mediated reconciliation.

`tests/test_process_runner.py` covers the OS-process JSONL boundary, durable restart recovery, `COMPLETE`/`PENDING` polling, and the invariant that recovery never calls `Send` again.

`tests/test_two_hermes_e2e.py` remains the Buzz-envelope composition proof with separate fake Buzz and Wavelength executors. It demonstrates adapter-complete settlement, receipt-mediated reconciliation settlement, replay protection, and tamper negatives.

## Live testbook: P6 Signet + hosted Buzz

**Latest rerun:** 2026-08-02  \
**Relay:** `wss://albi-lab.communities.buzz.xyz`  \
**Channel:** `14df4f9b-cf92-4026-b057-45f7d31fd5b4`  \
**Wallets:** two isolated Wavelength daemons on `127.0.0.1:11329` (Alice) and `127.0.0.1:11339` (Bob)  \
**Network:** Signet only

This is an operational test, not part of the deterministic suite. It requires explicit human approval, funded Signet wallets, a fresh Bob invoice for exactly `2100` sat, the two role-specific Buzz keys already loaded in the shell, and the hosted relay membership already established. Never paste keys, invoices, macaroons, or full payment hashes into the test output.

### Procedure used

1. Create a fresh Bob receive invoice for `2100` sat; save the JSON locally and validate its amount without printing the invoice.
2. Start the two-process supervisor with separate Alice/Bob state roots, `--network signet`, the two Wavelength RPC endpoints, `--skip-buzz-health` (the hosted health endpoint is RBAC-protected), and the hosted Buzz relay.
3. Exchange `PaymentIntent` and `PaymentQuote` over Buzz kind 9.
4. Run `PrepareSend`, inspect the prepared payload, and approve the exact `(intent_id, quote_id, prepared_hash)` tuple locally.
5. Execute the raw prepared send exactly once. An immediate `PENDING` is expected and is fail-closed; do not retry.
6. Query both wallets until the same settlement reference is `COMPLETE` on Alice `send` and Bob `recv`.
7. Bob verifies the incoming activity and publishes `PaymentReceipt` over Buzz; Alice receives and accepts it.
8. For the rerun, the supervisor was stopped after dispatch and restarted with the same state root. The receipt was reconciled without a second send.

A negative preflight was also exercised: a `1`-sat Bob invoice was rejected against the exact `2100`-sat quote before `Send`; no payment was dispatched. The successful rerun used a fresh `2100`-sat invoice.

### Latest observed result

```text
PaymentIntent:       accepted by Bob
PaymentQuote:        accepted by Alice
PrepareSend:         2100 sat, fee 0 sat
raw Send:            PENDING, no automatic retry
Alice activity:      SEND COMPLETE, -2100 sat, fee 0 sat
Bob activity:        RECV COMPLETE, 2100 sat, fee 0 sat
PaymentReceipt:      Bob verified/published; Alice accepted
Alice final state:   settled=1
Outcome:             PASS
```

The raw Wavelength JSON was observed as `{"activity":{"entries":[...]}}`; another formatter exposes a top-level `recent` list. Both forms are now covered by the adapter and regression tests. The internal settlement route was `in_ark`; this agent-to-agent payment therefore has a Wavelength payment/activity reference, not a separate on-chain mempool transaction. Bootstrap funding transactions are distinct evidence and must not be reported as the payment transaction.

### Post-checkpoint deterministic recovery gate

The process boundary now supports a read-only recovery command. After a
restart, `EXECUTING` is converted to `RECONCILIATION_REQUIRED`; recovery may
query sender activity and persist the observed status without ever calling
`Send` again:

```json
{"target":"alice","command":"recover","max_wait_seconds":720,"poll_interval_seconds":2}
```

`COMPLETE` records sender-side evidence but still waits for Bob's independently
verified receipt. `PENDING` is polled only inside the bounded window, while
`UNKNOWN` remains fail-closed. The deterministic restart test observed
`PENDING → PENDING → COMPLETE` and confirmed zero post-restart execute calls.
The live Signet gate repeated the same boundary with a real supervisor kill:
recovery observed `COMPLETE` in 8.335 seconds, activity showed exactly one
matching Alice `send` and one Bob `recv`, and the final receipt-mediated state
was `settled=1`.

## P7 plugin gate — implementation closed, live settlement blocked

The P7 plugin is tested separately from the P6 supervisor. The deterministic
suite covers tool registration, explicit schemas, local approval binding,
prepared-hash enforcement, receipt verification, and fail-closed ambiguous
dispatch behavior. The two-process harness is
`examples/p7_plugin/e2e_two_process.py` and uses isolated Alice/Bob state roots
with the hosted Buzz relay.

The latest live attempt completed intent, quote, prepare, approval, and one raw
dispatch. Wavelength then exposed the same settlement reference as:

```text
Alice: SEND PENDING
Bob:   RECV PENDING
Alice: RECONCILIATION_REQUIRED
Bob:   receipt rejected — status is PENDING, expected COMPLETE
```

The harness did not retry. Inspection showed Alice had only 1,345 spendable
sats for the 2,100-sat attempt; Wavelength returned
`ResourceExhausted: insufficient spendable funds` but left the swap at
`FUNDING_INITIATED` and both activity rows at `PENDING`. The engine fix
terminalizes this authoritative balance rejection as `FAILED`; the live gate
still requires one fresh run with a funded sender. Do not start another live
payment until the fixed daemon is deployed and the sender balance is verified.

## Commands

```bash
pytest -q
pytest -q tests/test_contract_invariants.py
pytest -q tests/test_policy_core.py
pytest -q tests/test_peer_transport.py tests/test_peer.py
pytest -q tests/test_hermes_to_hermes_e2e.py
pytest -q tests/test_transport.py
pytest -q tests/test_wavelength_adapter.py
pytest -q tests/test_two_hermes_e2e.py
git diff --check
python -m compileall -q src tests
```

## What a green suite means

A green suite proves that typed boundaries compose under deterministic inputs and that dangerous paths fail closed in tested cases. It does not prove that Buzz, a relay, Wavelength, Docker, an operator, a real invoice, or Signet is reachable. Those are operational gates, not unit-test assertions.
