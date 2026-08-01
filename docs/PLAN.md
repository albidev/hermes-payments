# Project Plan

## Product goal

Prove one replay-safe, human-approved payment between two Hermes identities, coordinated through signed Buzz channel messages and settled by a pluggable Wavelength adapter.

## Completed implementation gates

| Gate | Scope | Status |
|---|---|---:|
| P1 | Versioned protocol contract and canonical IDs | Complete |
| P2 | Rail-neutral state machine, policy, idempotency, audit | Complete |
| P3 | Buzz transport with NIP-29 kind-9 envelopes | Complete |
| P4 | Regtest-only Wavelength prepare/execute/receipt adapter | Complete |
| P5 | Deterministic two-Hermes composition and failure paths | Complete |

## Open operational gate

| Gate | Requirement | Status |
|---|---|---:|
| P6 | Two real daemons, real Buzz channel, funded sender, real receipt, recovery evidence | Open |

P5 is a deterministic integration proof. P6 is a deployment proof and cannot be declared complete by adding more fake-executor tests.

## Live settlement milestone

The wallet settlement sub-gate is complete on Signet. See [live-signet-payment.md](live-signet-payment.md) for the exact prepared intent, boarding evidence, activity references, and explicit non-claims.

The full P6 gate remains open until the same lifecycle is driven by two Hermes processes over a real Buzz kind-9 channel, followed by recovery testing.

## Rail evolution

The first protocol receive instruction is a Lightning invoice. Wavelength can internally route an invoice through Ark when the operator and wallet support it. First-class Ark protocol semantics are a separate follow-up and require their own instruction schema, policy, receipt, and compatibility tests.

## Exit criteria

- Replaying the same intent cannot settle twice.
- Approval for one quote/prepared payload cannot authorize another.
- A receipt links to independently verifiable settlement activity.
- The complete audit chain reconstructs the lifecycle.
- No secret appears in fixtures, logs, or Buzz payloads.
- Live evidence is reported separately from deterministic evidence.
