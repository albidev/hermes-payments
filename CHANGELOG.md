# Changelog

All notable changes to this project are documented here.

## Unreleased

- Expanded repository documentation in English.
- Documented the distinction between on-chain bootstrap funding and the actual settlement rail.
- Documented the current Wavelength route-semantics gap: an invoice quote may be internally settled through Ark, while the protocol still exposes `lightning` as its only rail.
- Added versioned Mermaid flow sources and a standalone architecture diagram.

## 0.1.0

- Versioned payment domain models and canonical SHA-256 IDs.
- Rail-neutral policy engine with durable idempotency and audit hooks.
- Buzz NIP-29 kind-9 channel transport boundary.
- Regtest-only Wavelength adapter using raw `PrepareSend` and `Send` RPC calls.
- Deterministic two-Hermes integration proof with complete and reconciliation-mediated paths.
