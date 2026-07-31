# Hermes Payments — Agent Guide

## Non-negotiable safety boundaries

1. Regtest only until a human explicitly approves a future deployment gate.
2. Never read, log, transmit, or request seeds, passwords, macaroons, private keys, or API tokens.
3. A Buzz message/event is not payment authorization. Execution requires an explicit local human approval bound to `(intent_id, quote_id, prepared_hash)`.
4. Every intent has expiry, `max_fee_sat`, recipient identity, and idempotency key.
5. Ambiguous settlement state is fail-closed: do not retry a send automatically.
6. No mainnet configuration, no autonomous spending, no external publication.

## Architecture

- Core protocol is rail-independent.
- Buzz is coordination/audit transport; do not couple business state to a specific Nostr event kind until the contract card is complete.
- `WavelengthAdapter` is the first settlement adapter.
- The Hermes plugin enforces policy; the skill documents correct use and cannot be treated as enforcement.

## Workflow

- Work in small commits on an isolated Kanban worktree.
- Tests first for state-machine and policy rules.
- Run the full relevant test suite before completion.
- Do not modify `/Users/albi/Projects/wavelength` or `/Users/albi/Projects/buzz`; treat both as external integrations.
