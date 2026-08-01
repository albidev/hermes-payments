# Project Plan

## Product goal

Prove one replay-safe, human-approved payment between two Hermes identities through a transport-neutral peer contract, with signed Buzz channel messages as the current adapter and a pluggable Wavelength settlement adapter.

## Completed implementation gates

| Gate | Scope | Status |
|---|---|---:|
| P1 | Versioned protocol contract and canonical IDs | Complete |
| P2 | Rail-neutral state machine, policy, idempotency, audit | Complete |
| P3 | Buzz transport with NIP-29 kind-9 envelopes | Complete |
| P4 | Regtest-only Wavelength prepare/execute/receipt adapter | Complete |
| P5 | Deterministic two-Hermes composition and failure paths | Complete |
| P5.1 | Transport-neutral `PeerTransport` and `HermesPeer` composition | Complete |

## Open operational gate

| Gate | Requirement | Status |
|---|---|---:|
| P6 | Two real Hermes processes, real Buzz channel, funded sender, real receipt, recovery evidence | **Partial on Signet; regtest and restart recovery open** |

P5 and P5.1 are deterministic integration proofs. The combined Signet run is external evidence for the coordination and settlement path, but P6 remains open until the deployment/restart requirements are exercised with two real Hermes processes and a real Wavelength regtest environment.

## Live settlement milestone

The Signet evidence now covers the full observable lifecycle: real kind-9 `PaymentIntent` and `PaymentQuote`, a real BOLT-11 invoice, local approval bound to the prepared payload, raw Wavelength execution, manual reconciliation of an initial `PENDING` result, matching Alice/Bob activity, and a Bob-authored `PaymentReceipt`. See [live-signet-buzz-vertical.md](live-signet-buzz-vertical.md). The original narrow proofs remain available in [live-signet-payment.md](live-signet-payment.md) and [live-buzz-transport.md](live-buzz-transport.md).

The full P6 gate remains open until the same lifecycle is driven by two Hermes processes over a real Buzz channel, bound to a real Wavelength regtest settlement, followed by restart/recovery testing.

## Rail evolution

The first protocol receive instruction is a Lightning invoice. Wavelength can internally route an invoice through Ark when the operator and wallet support it. First-class Ark protocol semantics are a separate follow-up and require their own instruction schema, policy, receipt, and compatibility tests.

## Exit criteria

- Replaying the same intent cannot settle twice.
- Approval for one quote/prepared payload cannot authorize another.
- A receipt links to independently verifiable settlement activity.
- The complete audit chain reconstructs the lifecycle.
- No secret appears in fixtures, logs, or Buzz payloads.
- Live evidence is reported separately from deterministic evidence.
