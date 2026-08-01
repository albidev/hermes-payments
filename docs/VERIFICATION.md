# Verification Status

This document prevents the repository from confusing “the tests passed” with “the money moved.”

## Verified in the repository

The deterministic suite exercises domain model validation and canonical hashing, state transitions and terminal states, idempotency and approval replay protection, Buzz kind-9 envelope encoding/decoding, channel/expiry/protocol/authorship validation, `PaymentApproval` exclusion from transport, Wavelength raw RPC command construction, exact prepared send intent binding, fee/amount/expiry/payment-hash checks, recipient-side `recv` verification, complete and receipt-mediated two-Hermes paths, and ambiguous execution fail-closed behavior.

Run the current suite with:

```bash
pytest -q
```

## Not proved by the repository suite

The suite does not prove that a real Buzz binary or relay is available, that two Hermes processes can share a live channel, that a Wavelength daemon is running, that an operator accepts a wallet, that a real invoice settles, that a live route is Lightning/Ark/another backend path, or that timeout recovery works on deployed infrastructure.

## P6 live gate

The first live Wavelength settlement is now separately verified on Signet. The kind-9 Buzz transport is also verified against a real local relay. These close the wallet/operator/recipient and Buzz transport sub-gates, but not the full two-Hermes settlement gate.

| Gate | Evidence required | Status |
|---|---|---:|
| Two isolated Hermes instances | Separate process/config/state roots | Open |
| Real Buzz transport | Signed kind-9 event observed by both sides | **Verified on local relay** — [live evidence](live-buzz-transport.md) |
| Funded sender | Spendable wallet balance, not merely pending funding | **Verified on Signet** — [live evidence](live-signet-payment.md) |
| Fresh prepared intent | `PrepareSend` result inspected and approved | **Verified on Signet** — [live evidence](live-signet-payment.md) |
| Exact execution | Raw `Send` consumes that prepared ID | **Verified on Signet** — [live evidence](live-signet-payment.md) |
| Recipient receipt | `activity --kind recv` matches reference and amount | **Verified on Signet** — [live evidence](live-signet-payment.md) |
| Recovery | Pending/ambiguous path manually reconciled | Open |

The remaining work is to carry the same lifecycle through two real Hermes processes, bind the quote to a real Wavelength regtest prepare/execute operation, and publish a receipt only after Bob verifies incoming activity. Restart/reconciliation behavior remains open.

## Signet evidence history

The earlier pre-settlement observation was a pending bootstrap balance and a complete `in_ark` quote. That observation is superseded by the completed run recorded in [live-signet-payment.md](live-signet-payment.md), which includes confirmed boarding, exact `SendPrepared` execution, and matching complete Alice/Bob activity.

The live evidence still does **not** claim that Buzz and Wavelength were combined in the same Signet run. The separate [live Buzz transport evidence](live-buzz-transport.md) proves the signed kind-9 exchange against a local relay; it intentionally published no receipt because no settlement occurred in that run.
