# Hermes Payments

A policy-first, multi-rail payment protocol for Hermes agents.

- **Buzz** supplies signed counterpart identity, coordination and audit events.
- **Hermes Payments** owns intent state, local policy, idempotency and human approval.
- **Settlement adapters** perform funds movement. Wavelength is the first adapter; Ark is its first advanced rail capability.

## v0 scope

A regtest-only vertical slice between two Hermes identities:

`PaymentIntent → PaymentQuote → PaymentApproval → PaymentReceipt`

The initial settlement path is a Wavelength-backed Lightning payment. No mainnet, autonomous spending, seed handling, or private credentials in Buzz events.

## Status

Planning and controlled implementation. See `docs/PLAN.md`.

## Related

- Wavelength source and test evidence: `/Users/albi/Projects/wavelength`
- Wavelength architecture notes: `/Users/albi/Documents/Hermes/projects/wavelength/HERMES-PAYMENTS-ARCHITECTURE.md`
- Buzz: https://github.com/albidev/buzz
