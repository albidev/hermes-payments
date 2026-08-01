# Live Buzz Transport Evidence

**Run class:** local live relay transport evidence<br>
**Relay:** real Buzz `buzz-relay` on `127.0.0.1:3030`<br>
**Date:** 2026-08-01<br>
**Channel lifetime:** ephemeral, 300 seconds

This run exercised the checked-in Python `SubprocessExecutor` against a real compiled Buzz CLI and a real local Buzz relay. Two ephemeral Nostr identities were generated inside the shell; their private keys were never printed or written to the repository. It is a transport-only smoke test; the later [combined Signet vertical](live-signet-buzz-vertical.md) adds a real invoice, Wavelength settlement, reconciliation, and recipient receipt.

## Infrastructure

The relay passed:

```text
GET http://127.0.0.1:3030/health       -> 200, ok
GET http://127.0.0.1:8088/_readiness    -> 200, {"status":"ready"}
Buzz CLI build                             -> cargo build --locked -p buzz-cli
```

The local relay used isolated test containers for Postgres and Redis, plus MinIO for the relay's object-store conformance probe. No staging or production relay was used.

## Channel and identities

| Item | Value |
|---|---|
| channel | `238e871b-ff37-43fa-b785-344fb75b1a55` |
| Alice public key | `f77a93f9...` |
| Bob public key | `46ce30af...` |
| membership | Bob added to the channel by Alice |

Only public identifiers are recorded here. The channel was ephemeral and the identities were test-only.

## Exchange

Alice sent a `PaymentIntent`; Bob validated it and sent a `PaymentQuote`; Alice validated the quote:

| Message | Event ID | Domain ID | Author |
|---|---|---|---|
| `payment_intent` | `3fd64992...` | `d6080a18...` | Alice |
| `payment_quote` | `a91839a2...` | `p6-live-buzz-quote-20260801` | Bob |

Both events were observed as:

```text
kind:       9
h tag:      238e871b-ff37-43fa-b785-344fb75b1a55
protocol:   hermes-payments
version:    1
```

The intent carried `amount_sat=2100` and `max_fee_sat=0`. The quote carried `rail=lightning` and `fee_sat=0`.

## What was verified

- The real Buzz CLI accepted both signed writes.
- The real relay stored and returned both NIP-29 kind-9 events.
- The channel `h` tag matched the expected UUID.
- The event author matched the domain sender/recipient identity rules.
- The Python transport decoded and validated both envelopes.
- The quote referenced the exact intent ID.

## Explicit non-claims

- No `PaymentApproval` was sent; approvals remain local-only.
- No Wavelength `PrepareSend` or `SendPrepared` call was made by this transport smoke test.
- No `PaymentReceipt` was published, because no settlement occurred in this run.
- The quote used a test-only receive instruction and must not be treated as a payable invoice.
- This is not yet the two-Hermes-process regtest gate.

This document remains intentionally transport-only: its quote used a test-only receive instruction and no settlement occurred in that smoke test. The later [combined Signet vertical](live-signet-buzz-vertical.md) demonstrates the same kind-9 boundary with a real invoice, reconciled Wavelength settlement, and a `PaymentReceipt` emitted only after Bob's verified incoming activity. The two-deployed-Hermes-process regtest gate and restart testing remain open.
