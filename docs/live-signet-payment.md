# Live Signet Payment Evidence

**Run class:** external live settlement evidence<br>
**Network:** Bitcoin Signet<br>
**Date:** 2026-08-01<br>
**Repository commit before the run:** `ec42184`

This document records the first successful live Wavelength payment used by the Hermes Payments project. It is the narrow **settlement-only** proof. The later [combined Signet Buzz + Wavelength vertical](live-signet-buzz-vertical.md) records a real kind-9 intent/quote/receipt exchange bound to a separate live settlement.

## Result

```text
sender wallet:       Alice
recipient wallet:    Bob
principal:           2100 sats
prepared route:      in_ark
expected fee:        0 sats
quote status:        complete
execution:           SendPrepared succeeded
recipient activity:  complete
```

The payment was executed only after explicit local approval of the exact prepared intent. The approval marker was removed after dispatch to prevent a duplicate send on restart.

## Verifiable handles

| Item | Value |
|---|---|
| `send_intent_id` | `3cb1685f1805c8e140e6161a783eec1e` |
| payment/activity reference | `8dc01eaf...` |
| amount | `2100 sat` |
| fee | `0 sat` |
| route reported by Wavelength | `in_ark` |
| runner result | `send_dispatched=true actual_amount_sat=2100` |
| recipient result | `bob_settlement_detected=true` |

The same activity reference was independently observed in both persistent wallet activity views:

```text
Alice: id=8dc01eaf... kind=send    status=complete amount_sat=-2100 fee_sat=0
Bob:   id=8dc01eaf... kind=receive status=complete amount_sat=2100  fee_sat=0
```

Only the public prefix is included here; the full reference remains in the local activity logs.

## Bootstrap evidence

Alice was funded through two confirmed Signet faucet transactions:

```text
5b27d3e5...
7e8439fc...
```

Both deposits were confirmed at block height `315704`. Wavelength then consumed the deposit output in the boarding transaction:

```text
4072a34c...
```

That boarding transaction was confirmed at block height `315705`. The persistent Alice wallet subsequently reported:

```text
confirmed_sat=10490
pending_in_sat=0
credit_available_sat=0
```

Only txid prefixes are included in this document; the full references remain in the local activity logs. The on-chain transactions above are **bootstrap evidence**. The 2100-sat agent payment itself was detected as wallet activity and used the `in_ark` route reported by Wavelength; it was not an on-chain payment to Bob.

## What this proves

- Signet connectivity to the configured Wavelength operator and swap server worked.
- On-chain bootstrap funding can become spendable wallet liquidity.
- `PrepareSend` returned a complete quote with known fee and total outflow.
- The exact prepared `send_intent_id` was consumed by `SendPrepared`.
- The recipient wallet observed a matching complete receive entry.
- The payment reference matched on sender and recipient activity logs.
- The live route selected by Wavelength was reported as `in_ark`.

## What this original settlement-only run does not prove

- A real Buzz binary or live Buzz channel was used in this particular run.
- Two Hermes processes exchanged a real kind-9 `PaymentIntent`, `PaymentQuote`, or `PaymentReceipt` in this particular run.
- The checked-in Python `WavelengthAdapter` supports Signet; it remains intentionally regtest-only.
- Restart/reconciliation behavior was exercised after an ambiguous live dispatch.
- Any mainnet or production safety claim.

The combined live Signet coordination and settlement evidence is now recorded separately in [live-signet-buzz-vertical.md](live-signet-buzz-vertical.md).
