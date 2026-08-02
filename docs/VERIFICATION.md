# Verification Status

This document prevents the repository from confusing “the tests passed” with “the money moved.”

## Verified in the repository

The deterministic suite exercises domain model validation and canonical hashing, state transitions and terminal states, idempotency and approval replay protection, Buzz kind-9 envelope encoding/decoding, channel/expiry/protocol/authorship validation, `PaymentApproval` exclusion from transport, Wavelength raw RPC command construction, exact prepared send intent binding, fee/amount/expiry/payment-hash checks, recipient-side `recv` verification, sender-side `send` reconciliation, both Wavelength activity JSON envelope shapes, complete and receipt-mediated two-Hermes paths, and ambiguous execution fail-closed behavior.

Run the current suite with:

```bash
pytest -q
```

## Not proved by the repository suite

The suite does not prove that a real Buzz binary or relay is available, that two Hermes processes can share a live channel, that a Wavelength daemon is running, that an operator accepts a wallet, that a real invoice settles, that a live route is Lightning/Ark/another backend path, or that timeout recovery works on deployed infrastructure.

## P6 live gate

The live evidence now covers a real Signet coordination-and-settlement vertical: two isolated Hermes processes exchanged kind-9 `PaymentIntent`, `PaymentQuote`, and post-settlement `PaymentReceipt` messages over the hosted Buzz relay, while Wavelength settled the corresponding 2100-sat invoice. The raw `Send` initially returned `PENDING`; sender and recipient activity were reconciled without retry, and the same state root was restarted to accept the receipt.

| Gate | Evidence required | Status |
|---|---|---:|
| Two isolated Hermes instances | Separate Hermes processes/config/state roots | **Verified on Signet** — [combined live evidence](live-signet-buzz-vertical.md) |
| Real Buzz transport | Signed kind-9 events observed by both sides | **Verified on Signet** — [combined live evidence](live-signet-buzz-vertical.md) |
| Funded sender | Spendable wallet balance, not merely pending funding | **Verified on Signet** — [combined live evidence](live-signet-buzz-vertical.md) |
| Fresh prepared intent | `PrepareSend` result inspected and approved | **Verified on Signet** — prepared-hash binding recorded in abbreviated form in [live evidence](live-signet-buzz-vertical.md) |
| Exact execution | Raw `Send` consumes that prepared ID | **Verified and reconciled on Signet** — [live evidence](live-signet-buzz-vertical.md) |
| Recipient receipt | `activity --kind recv` matches reference and amount | **Verified on Signet** — Bob's kind-9 receipt is recorded in [live evidence](live-signet-buzz-vertical.md) |
| Recovery | Restart/recovery after pending or ambiguous dispatch | **Manual reconciliation verified** — automatic recovery remains open |

The remaining work is automatic recovery after an ambiguous dispatch, plus
operator-availability, production, custody, and mainnet evidence. Signet
remains a test network and not a production or mainnet claim.

## Signet evidence history

The earlier pre-settlement observation was a pending bootstrap balance and a complete `in_ark` quote. It is superseded as the current Signet status by the completed evidence in [live-signet-buzz-vertical.md](live-signet-buzz-vertical.md), which includes two Hermes processes, hosted kind-9 coordination, exact prepared execution, manual same-state-root reconciliation, matching complete Alice/Bob activity, and a Bob-authored receipt.

The original narrow Wavelength-only settlement remains available in [live-signet-payment.md](live-signet-payment.md). The separate [live Buzz transport evidence](live-buzz-transport.md) remains a historical transport-only smoke test with no settlement. The combined document is the authoritative Signet vertical evidence; automatic recovery and production claims remain open.
