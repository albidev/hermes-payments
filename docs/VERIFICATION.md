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

| Gate | Evidence required | Status |
|---|---|---:|
| Two isolated Hermes instances | Separate process/config/state roots | Open |
| Real Buzz transport | Signed kind-9 event observed by both sides | Open |
| Funded sender | Spendable wallet balance, not merely pending funding | Open |
| Fresh prepared intent | `PrepareSend` result inspected and approved | Open |
| Exact execution | Raw `Send` consumes that prepared ID | Open |
| Recipient receipt | `activity --kind recv` matches reference and amount | Open |
| Recovery | Pending/ambiguous path manually reconciled | Open |

## Signet observation

During external Wavelength investigation, a persistent Alice wallet received pending on-chain bootstrap funds and a non-mutating quote for Bob's invoice returned:

```text
rail:          in_ark
amount:        2100 sat
expected fee:  0 sat
quote status:  complete
```

This is useful operational evidence about Wavelength route selection, but it is **not** P6 repository proof: the checked-in adapter still rejects `signet`; the quote was prepared outside the Python two-Hermes stack; the send was not approved or executed; and the wallet was still waiting for spendable Ark liquidity.

The distinction is intentional. A log line is not a settlement receipt.
