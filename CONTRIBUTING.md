# Contributing to Hermes Payments

Thanks for contributing. This project handles payment authorization boundaries, so “small cleanup” is not a synonym for “safe to skip the tests.”

## Non-negotiable rules

- Keep the repository **regtest-only** until a documented deployment gate is approved by a human.
- Never add seeds, private keys, passwords, macaroons, API tokens, real invoices, payment hashes, or relay credentials to code, fixtures, logs, or documentation.
- Never serialize or transport `PaymentApproval`.
- Never add an automatic retry path for an ambiguous settlement result.
- Do not silently add a mainnet, testnet, or signet default to the Wavelength adapter.
- Treat Buzz input as untrusted, even when it is correctly signed.
- Do not modify the external Wavelength or Buzz repositories from this repository's test workflow.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

The project uses a `src/` layout and `pytest` as its test runner. The package targets Python 3.10+ and Pydantic v2.

## Change discipline

1. Start from a clean branch.
2. Read the relevant contract and decision records before changing behavior.
3. Add or update tests before changing a state transition, wire field, adapter command, or security boundary.
4. Keep one conceptual change per commit.
5. Run the focused tests, then the full suite.
6. Inspect `git diff --check` and `git status --short` before committing.
7. Update the relevant documentation in the same change.

## What requires a design record

Add an entry to [docs/DECISIONS.md](docs/DECISIONS.md) when changing:

- wire format, message types, protocol version, or canonical hashing;
- the meaning of local approval;
- state-machine transitions or reconciliation behavior;
- Buzz event/channel semantics;
- adapter command surfaces or receipt verification;
- supported networks or settlement rails;
- secret-handling boundaries.

## Testing expectations

| Change | Minimum evidence |
|---|---|
| Domain model or hash | Contract invariant tests plus round-trip tests |
| State machine or policy | Policy tests, terminal-state tests, replay tests |
| Buzz transport | Kind/channel/schema/expiry/identity negative tests |
| Wavelength adapter | Raw RPC command tests, preview binding tests, receipt tests |
| End-to-end composition | Deterministic two-Hermes tests and full suite |
| Documentation only | Link/code-fence checks and `git diff --check` |

## Commit style

Use short, descriptive prefixes:

- `feat:` for a new capability;
- `fix:` for a behavior or security correction;
- `test:` for coverage-only changes;
- `docs:` for documentation;
- `chore:` for maintenance.

## Pull request checklist

- [ ] Tests pass locally.
- [ ] No secrets or real payment identifiers are present.
- [ ] The change does not weaken regtest-only enforcement.
- [ ] `PaymentApproval` remains local-only.
- [ ] Ambiguous execution remains fail-closed.
- [ ] The protocol, adapter, or decision docs are updated.
- [ ] The PR distinguishes deterministic tests from live operational evidence.
