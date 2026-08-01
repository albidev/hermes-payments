# Testing and Verification

The project separates deterministic correctness from live operational correctness.

## Test layers

### Contract invariants

`tests/test_contract_invariants.py` covers canonical IDs, model round trips, terminal states, approval binding, `PaymentApproval` transport exclusion, and envelope kind/protocol checks.

### Policy core

`tests/test_policy_core.py` covers expiry and allowlists, durable idempotency, audit sequencing, fee constraints, execution transitions, ambiguous results, receipt replay, and mismatch behavior.

### Buzz transport

`tests/test_transport.py` covers NIP-29 kind-9 channel scoping, versioned content envelopes, malformed and expired messages, wrong channel or event kind, author mismatch, and no serialized approval.

### Wavelength adapter

`tests/test_wavelength_adapter.py` covers regtest-only construction, injection-safe command lists, raw `PrepareSend` and `Send`, exact prepared intent binding, fee/amount/expiry/payment-hash validation, strict status parsing, recipient-side `activity --kind recv`, redaction, and ambiguous outcomes.

### Two-Hermes composition

`tests/test_two_hermes_e2e.py` composes two independent stacks with separate fake Buzz and Wavelength executors. It demonstrates adapter-complete settlement, receipt-mediated reconciliation settlement, replay protection, and tamper negatives.

## Commands

```bash
pytest -q
pytest -q tests/test_contract_invariants.py
pytest -q tests/test_policy_core.py
pytest -q tests/test_transport.py
pytest -q tests/test_wavelength_adapter.py
pytest -q tests/test_two_hermes_e2e.py
git diff --check
python -m compileall -q src tests
```

## What a green suite means

A green suite proves that typed boundaries compose under deterministic inputs and that dangerous paths fail closed in tested cases. It does not prove that Buzz, a relay, Wavelength, Docker, an operator, a real invoice, or Signet is reachable. Those are operational gates, not unit-test assertions.
