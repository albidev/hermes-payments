# Live Signet Buzz + Wavelength Vertical Evidence

**Run class:** live coordination plus settlement evidence  \
**Network:** Bitcoin Signet  \
**Date:** 2026-08-01  \
**Wavelength operator:** `signet.wavelength.lightning.finance:443`  \
**Wavelength version:** `0.1.99`

This document records the live Signet run that combined a real Buzz channel exchange with a real Wavelength settlement. It is deliberately narrower than a production claim: the run used two isolated Wavelength daemons, two ephemeral Buzz identities, and a local Buzz relay. The checked-in `WavelengthAdapter` remains regtest-only.

## Result

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
| Buzz channel | `3e66c65a-d5cb-4cc4-b186-03e27959dfea` |
| Alice public key | `3cd5f417343c3cb7b7808c3b82051b3637324586d9c13c27d2736b71b28dda26` |
| Bob public key | `3e90b2004ea559578bcc905508bcf9558566a607555ec21a7ed8b074e614d216` |
| Channel scope | NIP-29 `h` tag equal to the channel UUID |
| Relay | local Buzz relay at `127.0.0.1:3030` |

Only public identifiers are recorded. Private keys, wallet passwords, and the BOLT-11 invoice remain local and are not reproduced here.

## Buzz protocol exchange

All three events were stored by the real relay as NIP-29 kind-9 messages with protocol `hermes-payments`, version `1`, and the channel `h` tag above.

| Message | Event ID | Domain ID | Author |
|---|---|---|---|
| `payment_intent` | `bddaa1a323bb06fce401cba2346beb6d87656b4c179caf9425df3ce6fd1fb107` | `16b013f077fcc581bd32112627a961a6132a7c2f5b1861a1bb847e969082f14f` | Alice |
| `payment_quote` | `791dd59a24fb4a43ef31770e6a243ec281a9057f38e79939f607a552c44a8d34` | `27fc5faae89ba8c082c5978effd3c5d9b99b5aab7f9f3c4d1adbd56a2542cbd6` | Bob |
| `payment_receipt` | `e4db7082868fa5a181cea7c250ba0d07fea100e822ea42fee680a5337f5e5b6a` | `5f21a755bd886618fda51f2173eecf1345697eaa9f24236f4edf25dcea2f9948` | Bob |

The intent requested `amount_sat=2100` and `max_fee_sat=0`. The quote carried a real BOLT-11 receive instruction, `rail=lightning`, and `fee_sat=0`. The quote identifier is `hermes-payments-testnet-quote-1785586627`; the legacy `testnet` label is an application idempotency label and does not mean Bitcoin testnet3 was used. The network for this run was Signet.

`PaymentApproval` was not sent over Buzz. It remained a local-only approval binding, as required by the protocol contract.

## Prepared settlement and reconciliation

The sender used the raw Wavelength RPC path:

```text
PrepareSend → send_intent_id=2ecd9983847ac484c5eaaf93b1e93849
prepared_hash=e49fce9f3b7c50168c192d37150cd833d699d19ed9d67dfdbeedec7da9178853
amount_sat=2100
expected_fee_sat=0
expected_total_outflow_sat=2100
internal route=IN_ARK
payment_hash=69ea1051c382a972ad5f7d925d62edbc34de018bb145467686fa83a0258306c3
```

The local approval was bound to the exact `(intent_id, quote_id, prepared_hash)` tuple. The raw `Send` consumed the prepared `send_intent_id` and returned `ENTRY_STATUS_PENDING` with `actual_amount_sat=2100`. Because a post-dispatch pending result is ambiguous, the runner did not retry.

Independent activity inspection then produced:

```text
Alice: kind=send  status=complete amount_sat=-2100 fee_sat=0 payment_hash=69ea1051...
Bob:   kind=recv  status=complete amount_sat=2100  fee_sat=0 payment_hash=69ea1051...
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

- The checked-in Python `WavelengthAdapter` supports Signet; it intentionally rejects non-regtest construction.
- Two deployed Hermes agent processes, rather than the live driver and two isolated wallet daemons, were exercised end to end.
- Restart recovery after the ambiguous dispatch was tested. This run performed manual activity reconciliation only.
- Any mainnet, production, custody, operator-availability, or economic-safety claim.

The narrow original Wavelength-only settlement remains documented in [live-signet-payment.md](live-signet-payment.md). The local relay transport smoke test remains documented in [live-buzz-transport.md](live-buzz-transport.md). This file is the combined Signet evidence; it does not erase the distinction between live evidence and the deterministic offline suite.
