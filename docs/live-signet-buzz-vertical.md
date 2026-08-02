# Live Signet Buzz + Wavelength Vertical Evidence

**Run class:** live two-process coordination plus settlement evidence  \
**Network:** Bitcoin Signet  \
**Latest validation:** 2026-08-02  \
**Wavelength operator:** `signet.wavelength.lightning.finance:443`  \
**Wavelength version:** `0.1.99`  \
**Latest Buzz relay:** `wss://albi-lab.communities.buzz.xyz`

This document records the live Signet run that combined a real Buzz channel exchange with a real Wavelength settlement. The latest validation used two isolated Hermes processes, two isolated Wavelength daemons, two ephemeral Buzz identities, and the hosted `albi-lab` Buzz relay. The earlier local-relay run is retained as historical evidence. The checked-in `WavelengthAdapter` supports this only as an explicitly approved Signet test mode; it is not a production or mainnet mode.

## Latest rerun result (2026-08-02)

The P6 runner started two real Hermes processes with separate state roots and used the hosted Buzz relay. It reached the exact prepared execution gate and returned the expected post-dispatch `PENDING` result. A direct Wavelength activity query then showed the settlement complete; the same state root was restarted and reconciled without another send.

```text
Hermes processes: two, Alice + Bob, separate state roots
Buzz relay:       hosted albi-lab community
PaymentIntent:    accepted over Buzz kind 9
PaymentQuote:     accepted over Buzz kind 9
PrepareSend:      amount=2100 sats, expected_fee=0 sats
raw Send:         initial status=PENDING, automatic_retry=false
Alice activity:   SEND COMPLETE, amount=-2100 sats, fee=0 sats
Bob activity:     RECV COMPLETE, amount=2100 sats, fee=0 sats
PaymentReceipt:   verified/published by Bob, accepted by Alice
Alice final:      settled=1
Result:           PASS
```

The live runner's first activity poll exposed a driver-only parsing gap: the `--json` command returns `{"activity":{"entries": [...]}}`, while another CLI formatter returns a top-level `recent` list. The checked-in adapter and the operator test driver used for the rerun were updated to accept both forms. The payment itself was not retried; reconciliation used the already-dispatched payment and ended with Alice `settled`.

## Post-rerun recovery implementation status

The historical live rerun above predates the automatic recovery implementation.
The checked-in process boundary now performs the following after a restart;
the live crash/restart gate below validates it with the real Signet setup:

```text
EXECUTING
  → RECONCILIATION_REQUIRED
  → sender activity query
  → COMPLETE | PENDING | UNKNOWN
```

`COMPLETE` is persisted as sender-side evidence and still waits for Bob's
verified receipt. `PENDING` may be polled through the bounded `recover`
command, capped at 12 minutes; `UNKNOWN` remains fail-closed. The deterministic
suite verifies `PENDING → PENDING → COMPLETE` with zero post-restart `Send`
calls. The live crash/restart gate below verifies the same boundary with real
Signet daemons and Buzz identities.

## Live crash/restart recovery gate (2026-08-02)

A fresh 2100-sat Bob invoice was used. After Alice completed local prepare and
approval, the raw dispatch was written to the process boundary and the
supervisor was hard-killed after Alice's durable snapshot reached
`RECONCILIATION_REQUIRED`. Both Hermes processes were then restarted with the
same state roots.

```text
restart recovery:     COMPLETE after 8.335 seconds
Alice matching SEND:  1 entry, COMPLETE, -2100 sat, fee 0
Bob matching RECV:    1 entry, COMPLETE, +2100 sat, fee 0
second SEND:          none
Bob receipt:          verified and published over Buzz kind 9
Alice final state:    settled=1
Result:               PASS
```

The activity count was filtered by the fresh payment hash, and the sender and
receiver references matched. Recovery performed read-only activity queries;
the restarted process did not call `Send` again. The run used the explicit
Signet test configuration and does not constitute production or mainnet
evidence.

## On-chain verification boundary

The latest Wavelength activity record reports `txid=""`, `confirmation_height=0`, and a virtual `vtxo_outpoint`; the payment/activity reference is not an on-chain Bitcoin transaction. Querying the payment reference and the virtual outpoint against the Mempool Signet API returned HTTP 404, as expected for the internal `in_ark` route.

The wallet's separate bootstrap deposit is on-chain and independently verifiable:

```text
bootstrap deposit txid: cf6aca79286fa51459280352eac93bbd6a447074dc078aebad9283a3666bc9a
network:                Signet
block height:           315726
Mempool:                https://mempool.space/signet/tx/cf6aca79286fa51459280352eac93bbd6a447074dc078aebad9283a3666bc9a
```

That txid proves wallet funding only. It is not the 2100-sat agent payment, which has no separate mempool transaction in this run.

## Baseline result (2026-08-01)


```text
PaymentIntent:   accepted over Buzz kind 9
PaymentQuote:    accepted over Buzz kind 9
PrepareSend:     amount=2100 sats, expected_fee=0 sats
raw Send:        initial status=PENDING, actual_amount=2100 sats
reconciliation:  Alice SEND COMPLETE, Bob RECV COMPLETE
PaymentReceipt:  published by Bob over Buzz kind 9
```

The initial `PENDING` response was treated as ambiguous. No automatic retry was made. The final outcome was established from independent sender and recipient activity, and the receipt was published only after Bob's incoming entry was `COMPLETE`.

## Participants and channel

| Item | Value |
|---|---|
| Buzz channel | latest `14df4f9b-cf92-4026-b057-45f7d31fd5b4`; historical baseline `3e66c65a-d5cb-4cc4-b186-03e27959dfea` |
| Alice public key | latest `c55bd0f6...`; historical baseline `3cd5f417...` |
| Bob public key | latest `8ad4f9b4...`; historical baseline `3e90b200...` |
| Channel scope | NIP-29 `h` tag equal to the channel UUID |
| Relay | hosted Buzz relay `wss://albi-lab.communities.buzz.xyz` (latest run); local `127.0.0.1:3030` for the historical baseline |

Only public identifiers and abbreviated evidence handles are recorded. Private keys, wallet passwords, the BOLT-11 invoice, the full prepared hash, and the full payment hash remain local and are not reproduced here. The shared payment-hash prefix is included only to show that the independently observed sender and recipient records refer to the same settlement.

## Buzz protocol exchange

All three events were stored by the real hosted relay as NIP-29 kind-9 messages with protocol `hermes-payments`, version `1`, and the latest channel `h` tag.

### Latest rerun (2026-08-02)

| Message | Event/message ID prefix | Domain/receipt ID prefix | Author |
|---|---|---|---|
| `payment_intent` | `4a68d232...` | `c04d22d7...` | Alice |
| `payment_quote` | `fbc32c6a...` | `p6-signe...` | Bob |
| `payment_receipt` | `892f5bfd...` | `80133d55...` | Bob |

### Historical baseline (2026-08-01)

| Message | Event ID | Domain ID | Author |
|---|---|---|---|
| `payment_intent` | `bddaa1a3...` | `16b013f0...` | Alice |
| `payment_quote` | `791dd59a...` | `27fc5faa...` | Bob |
| `payment_receipt` | `e4db7082...` | `5f21a755...` | Bob |

The latest intent requested `amount_sat=2100` and `max_fee_sat=0`. The quote carried a real BOLT-11 receive instruction, `rail=lightning`, and `fee_sat=0`. The latest application quote label used the `p6-signet-live-quote-<timestamp>` prefix; the network was Signet. The historical `testnet` label in the baseline run was an application idempotency label and did not mean Bitcoin testnet3.

`PaymentApproval` was not sent over Buzz. It remained a local-only approval binding, as required by the protocol contract.

## Prepared settlement and reconciliation

### Latest rerun (2026-08-02)

```text
PaymentIntent:       c04d22d7...
PaymentQuote:        p6-signet-live-quote-<timestamp>
prepared_hash:       dd392aaf...
amount_sat:          2100
expected_fee_sat:    0
internal route:      IN_ARK
payment_hash:        <redacted; public prefix 99e25e44 only>
```

The local approval was bound to the exact `(intent_id, quote_id, prepared_hash)` tuple. The raw `Send` consumed the exact prepared intent and returned `ENTRY_STATUS_PENDING` with `actual_amount_sat=2100`. Because a post-dispatch pending result is ambiguous, the runner did not retry.

Independent activity inspection then produced:

```text
Alice: kind=send  status=complete amount_sat=-2100 fee_sat=0 payment_hash=<redacted; public prefix 99e25e44>
Bob:   kind=recv  status=complete amount_sat=2100  fee_sat=0 payment_hash=<redacted; public prefix 99e25e44>
```

### Historical baseline (2026-08-01)

The sender used the raw Wavelength RPC path:

```text
PrepareSend → send_intent_id=2ecd9983847ac484c5eaaf93b1e93849
prepared_hash=e49fce9f...
amount_sat=2100
expected_fee_sat=0
expected_total_outflow_sat=2100
internal route=IN_ARK
payment_hash=<redacted; public prefix 69ea1051 only>
```

The local approval was bound to the exact `(intent_id, quote_id, prepared_hash)` tuple. The raw `Send` consumed the prepared `send_intent_id` and returned `ENTRY_STATUS_PENDING` with `actual_amount_sat=2100`. Because a post-dispatch pending result is ambiguous, the runner did not retry.

Independent activity inspection then produced:

```text
Alice: kind=send  status=complete amount_sat=-2100 fee_sat=0 payment_hash=<redacted; public prefix 69ea1051>
Bob:   kind=recv  status=complete amount_sat=2100  fee_sat=0 payment_hash=<redacted; public prefix 69ea1051>
```

The same payment hash, amount, and zero fee were present on both sides. Only after Bob's `RECV COMPLETE` was verified did Bob publish the receipt above. The receipt was independently read back from the relay as `kind=9`, authored by Bob, with `amount_sat=2100` and `fee_sat=0`.

## What this proves

- A real Buzz relay accepted and returned a signed kind-9 `PaymentIntent` and `PaymentQuote` scoped to one channel.
- The quote carried a real BOLT-11 invoice that was consumed by the Wavelength wallet; the invoice itself is intentionally not disclosed.
- A local human approval bound the exact prepared payload hash and was not transmitted through Buzz.
- The raw Wavelength `Send` used the exact prepared intent rather than silently preparing a second one.
- An initial `PENDING` result was handled fail-closed: no automatic retry occurred.
- Sender-side and recipient-side activity independently reconciled to the same complete settlement.
- Bob issued a signed `PaymentReceipt` only after verifying incoming activity.
- The 2100-sat agent payment settled through Wavelength's internal `in_ark` route; the on-chain transactions were bootstrap funding, not the agent-to-agent payment.

## What this does not prove

- The checked-in Python `WavelengthAdapter` supports Signet only when explicitly
  selected and intentionally rejects mainnet, testnet, and arbitrary networks.
- Automatic restart recovery is implemented, deterministically tested, and verified live on Signet. Production, custody, operator-availability, and mainnet claims remain open.
- Any economic-safety claim beyond the explicit test configuration.

The narrow original Wavelength-only settlement remains documented in [live-signet-payment.md](live-signet-payment.md). The historical local relay transport smoke test remains documented in [live-buzz-transport.md](live-buzz-transport.md). This file is now the authoritative combined Signet two-process evidence; it does not erase the distinction between live evidence and the deterministic offline suite.
