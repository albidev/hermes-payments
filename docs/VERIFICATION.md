# P5 Verification — Two-Hermes End-to-End Deterministic Integration Proof

## What this proves

The P5 test suite (`tests/test_two_hermes_e2e.py`) proves **protocol composition**
across two independent Hermes instances (Alice = sender, Bob = recipient) sharing
a single Buzz channel, using only deterministic fakes:

- **FakeExecutor** (in-memory Buzz relay seam, `transport.py`)
- **FakeWavecliExecutor** (in-memory wavecli seam, `adapter.py`)
- **WavelengthAdapter** (settlement adapter, `adapter.py`)
- **PaymentOrchestrator** (policy engine, `policy.py`)
- **BuzzTransport** (channel-scoped transport, `transport.py`)
- **validate_received_event** (untrusted message validation, `transport.py`)
- **encode_content / decode_content** (wire codec, `envelope.py`)

No waved daemon, Docker, Buzz CLI, network, subprocess, or external repos.

## Two settlement paths demonstrated

### Path A — Adapter-Complete Settlement (10 tests)

Alice's adapter returns `COMPLETE` → Alice settles immediately via `execute()`.

```
Alice                                    Bob
─────                                    ───
create PaymentIntent ────Buzz────►  validate as untrusted
                                   create PaymentQuote
validate quote ◄────Buzz────
WavelengthAdapter.prepare() (fake) → binding token
local PaymentApproval (never sent)
WavelengthAdapter.execute() (fake) → COMPLETE → SETTLED ✓
                                   verify recv activity (fake) ✓
                                   publish PaymentReceipt ◄────Buzz
```

Proves:
1. Intent flows from Alice to Bob via Buzz (kind 9 envelope)
2. Bob validates intent as untrusted (validate_received_event)
3. Quote flows from Bob to Alice via Buzz
4. Adapter.prepare() returns a deterministic binding token
5. PaymentApproval binds (intent_id, quote_id, prepared_hash) and is never serialized
6. Adapter.execute() consumes exact send_intent_id → COMPLETE → SETTLED
7. Bob's adapter verifies recv activity matches payment_hash/amount
8. Bob publishes receipt via Buzz

### Path B — Receipt-Mediated Settlement (4 tests)

Alice's adapter returns `PENDING` → `RECONCILIATION_REQUIRED` → Bob's receipt → SETTLED.

```
Alice                                    Bob
─────                                    ───
create PaymentIntent ────Buzz────►  validate as untrusted
                                   create PaymentQuote
validate quote ◄────Buzz────
WavelengthAdapter.prepare() (fake) → binding token
local PaymentApproval (never sent)
WavelengthAdapter.execute() (fake) → PENDING → RECONCILIATION_REQUIRED
                                   verify recv activity (fake) ✓
  receive receipt ◄────Buzz────     publish PaymentReceipt
validate receipt → SETTLED ✓
```

Proves:
1. PENDING/unknown adapter result → RECONCILIATION_REQUIRED (fail-closed)
2. Bob independently verifies payment via recv activity
3. Bob publishes signed receipt via Buzz
4. Alice's `receive_receipt()` verifies receipt → transitions to SETTLED

## Negative tests (17 tests)

| Category | Tests | What it proves |
|---|---|---|
| Duplicate/replay | 2 | Duplicate intent is idempotent; duplicate receipt rejected by state gate |
| Tampered/expired | 7 | Wrong kind, wrong channel, tampered pubkey, expired intent/quote, bad protocol, kind 40100 — all rejected before policy |
| Send pending/unknown | 4 | PENDING → RECONCILIATION_REQUIRED, no receipt; unknown status → same; only confirm_settled or receipt can resolve |
| Approval never in Buzz | 2 | PaymentApproval never appears in FakeExecutor sent list; encode_content raises TypeError |
| Receipt validation | 2 | Wrong payment hash → verified=False; wrong amount → verified=False with mismatch error |

## Running the tests

```bash
# Run P5 tests only
pytest tests/test_two_hermes_e2e.py -v

# Run full suite (302 tests)
pytest -v
```

## What this does NOT prove (operational gate)

This is a **deterministic integration proof**, not a live regtest proof.

To claim **live settlement**, the following operational gate must be satisfied:

1. **Two actual waved regtest daemons** — one per Hermes instance (Alice and Bob),
   each with a funded wallet on the same regtest network.

2. **Real Buzz channel** — both daemons connected to the same Buzz relay, publishing
   and receiving kind 9 messages with real cryptographic signatures.

3. **Funded sender** — Alice's waved daemon must have sufficient balance to cover
   `amount_sat + fee_sat` (2,110 sats minimum for this test).

4. **Lightning invoice** — Bob generates a real Lightning invoice (bolt11) and
   publishes it in the PaymentQuote. Alice pays it via Wavelength adapter.

5. **Real Wavelength adapter** — Alice's adapter must call `wavecli dev ... PrepareSend`
   and `wavecli dev ... Send` against a live daemon. Bob's adapter must call
   `wavecli dev ... activity --kind recv` against his own daemon.

6. **Real Buzz transport** — Messages must flow through actual Buzz relay infrastructure,
   not FakeExecutor.

Until all six conditions are met, this test proves that the protocol specification
is correct and internally consistent, but does NOT prove that settlement works
on real infrastructure.
