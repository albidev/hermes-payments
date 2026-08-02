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
| P6 | Two real Hermes processes, real Buzz channel, funded sender, real receipt, recovery evidence | **Complete on Signet: live crash/restart recovery verified; strict regtest and production remain open** |

P5 and P5.1 are deterministic integration proofs. The combined Signet run is
external evidence for the coordination and settlement path. P6 now has a
verified two-Hermes Signet payment, a read-only restart recovery implementation,
and both deterministic and live coverage for `PENDING → COMPLETE` polling
without a second send. Strict regtest, production, custody, and mainnet remain
separate gates.

## Live settlement milestone

The Signet evidence now covers the full observable lifecycle: real kind-9 `PaymentIntent` and `PaymentQuote`, a real BOLT-11 invoice, local approval bound to the prepared payload, raw Wavelength execution, an initial `PENDING` result, matching Alice/Bob activity, crash/restart recovery, and a Bob-authored `PaymentReceipt`. See [live-signet-buzz-vertical.md](live-signet-buzz-vertical.md). The original narrow proofs remain available in [live-signet-payment.md](live-signet-payment.md) and [live-buzz-transport.md](live-buzz-transport.md).

The full Signet payment and recovery lifecycle is green: two Hermes processes
exchanged the protocol over the hosted Buzz channel, used the two isolated
Signet Wavelength daemons, survived a crash immediately after dispatch, and
accepted a real receipt after restart. The recovery path only queries sender
activity, records `COMPLETE`, `PENDING`, or `UNKNOWN`, and never calls `Send`
again. It waits at most 12 minutes when the operator explicitly requests
bounded polling. The next work is strict regtest reproducibility and production
hardening, not another happy-path payment.

## Rail evolution

The first protocol receive instruction is a Lightning invoice. Wavelength can internally route an invoice through Ark when the operator and wallet support it. First-class Ark protocol semantics are a separate follow-up and require their own instruction schema, policy, receipt, and compatibility tests.

## Exit criteria

- Replaying the same intent cannot settle twice.
- Approval for one quote/prepared payload cannot authorize another.
- A receipt links to independently verifiable settlement activity.
- The complete audit chain reconstructs the lifecycle.
- No secret appears in fixtures, logs, or Buzz payloads.
- Live evidence is reported separately from deterministic evidence.
