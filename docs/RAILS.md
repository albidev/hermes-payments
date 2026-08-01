# Rails and Settlement Semantics

The project uses “rail” in two related but non-identical senses:

1. the **protocol rail** exposed in typed messages and policy allowlists;
2. the **wallet route** selected internally by a settlement backend.

Keeping those separate prevents a useful implementation detail from becoming a fake protocol promise.

## Current protocol rail

Today the Python domain model exposes:

```python
class Rail(str, Enum):
    LIGHTNING = "lightning"
```

`RailReceiveInstruction` carries a BOLT11 invoice for that rail. The Wavelength adapter validates the invoice, prepares it with the raw wallet RPC, and returns a prepared payload bound to the exact send intent.

This is the implemented and tested contract.

## Wavelength route semantics

Wavelength's raw `PrepareSend` response has its own route/status fields. A BOLT11 invoice is a receive instruction; it does not, by itself, prove that the eventual value transfer used public Lightning hops.

Depending on wallet/operator state, Wavelength can choose an internal route. In the external Signet probe used during P6 investigation, the returned quote was:

```text
amount:        2100 sat
expected fee:  0 sat
quote status:  complete
route:         in_ark
```

That means the actual Wavelength settlement route was Ark-native for that quote, even though the current Hermes Payments wire instruction remained a Lightning invoice.

The current adapter intentionally does not claim `ARK` at the protocol layer yet. It normalizes the adapter result to the existing `Rail.LIGHTNING` contract while preserving the raw route inside the opaque prepared payload.

## On-chain is not the payment rail here

The current protocol has no `ON_CHAIN` rail. An on-chain faucet deposit may still be required to bootstrap a fresh wallet:

```text
on-chain funding → operator boarding → spendable VTXO → fast settlement
```

That deposit is wallet funding, not a `PaymentIntent` settlement. The bootstrap can be avoided only if there is already a spendable VTXO, operator-provided credit, or a funded wallet controlled by the test harness.

## Lightning

Lightning remains the first protocol-level capability because a BOLT11 invoice is a portable recipient instruction, the receive side can verify a payment hash and amount, the adapter boundary is straightforward to test, and Wavelength exposes a prepared send plus receipt activity surface.

“Lightning invoice” describes the receive instruction. The settlement backend may optimize the actual route.

## Ark

Ark is a natural next protocol-level rail when compatible Ark instructions are available. A first-class `ARK` or `IN_ARK` extension must define:

- an Ark receive instruction, not just an invoice string;
- route and operator identity binding;
- expiry and fee semantics;
- prepare/execute binding;
- recipient-side verification;
- receipt reference format;
- replay and recovery tests;
- compatibility and network gates.

Until those exist, do not add an enum value just to make the README look complete.

## Rail selection policy

The policy engine must not silently downgrade:

```text
requested/quoted Ark → Lightning
requested/quoted Lightning → on-chain
```

Any route conversion must be explicit in the quote and bound into the prepared payload. The receipt must state what was actually verified.

## Network scope

The checked-in `WavelengthAdapter` rejects `mainnet`, `testnet`, `signet`, and arbitrary network names. It is intentionally `regtest` only. The external Signet runner is an operational experiment, not a supported repository adapter mode.
