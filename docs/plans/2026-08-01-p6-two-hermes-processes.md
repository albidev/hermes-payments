# P6 Two-Hermes Processes Implementation Plan

> **For Hermes:** Execute this plan task-by-task with strict TDD. Keep the live gate regtest-only and never create or transmit secrets through the process protocol.

**Goal:** Drive one complete, replay-safe payment through two independently launched Hermes processes, a real Buzz kind-9 transport, and the regtest Wavelength adapter, including restart/reconciliation evidence.

**Architecture:** The checked-in payment domain remains unchanged. A small example process runner owns only process lifecycle, JSONL control input/output, isolated state roots, and dependency construction. Alice and Bob communicate through `BuzzTransport`; operator commands never become payment messages. Live infrastructure is injected through environment/configuration and is never hard-coded into the domain.

**Tech Stack:** Python 3.10+, `HermesPeer`, `BuzzTransport`, `WavelengthAdapter`, subprocesses, JSONL over stdin/stdout, pytest seams.

---

## Scope and safety gates

- Regtest only for the live P6 gate.
- No automatic wallet creation, funding, approval, or settlement.
- `PaymentApproval` is supplied only through an explicit local operator command/file and is never emitted on stdout as a transport message.
- Each process gets its own state root, audit path, and configuration.
- An ambiguous `Send` result stops execution and requires reconciliation; no retry.
- Signet evidence remains historical validation, not the regtest gate.

## Tasks

### Task 1 — Process contract RED

Create `tests/test_process_runner.py` with tests for:

1. JSONL command parsing rejects malformed or unknown commands without mutating policy state.
2. A process configuration requires an explicit role, identity, channel, network, and state root.
3. Process output contains only redacted lifecycle events; no private material or full settlement identifiers.
4. Restarting a process reloads its durable orchestrator state from its isolated state root.

Run:

```bash
pytest -q tests/test_process_runner.py
```

Expected first result: FAIL because the process runner module does not exist.

### Task 2 — Minimal process runner GREEN

Create `examples/two-hermes-regtest/process_runner.py` with:

- `ProcessConfig` parsed from explicit arguments/environment.
- `JsonlCommand` validation using Pydantic models already used by the protocol.
- `HermesProcess` that constructs one `PaymentOrchestrator`, one `BuzzTransport`, and one `HermesPeer`.
- A command dispatcher for `status`, `receive`, and role-specific handoff operations.
- Atomic/redacted JSONL responses suitable for a supervising harness.

No live settlement command is implemented until the process boundary tests pass.

### Task 3 — Lifecycle tracer

Add a deterministic process-level test that launches two local process instances with fake executors and drives:

```text
intent -> quote -> local approval -> prepare -> execute -> receipt
```

Include duplicate delivery and an ambiguous execution path. The test must prove that process restart reloads the state machine and does not retry a dispatched payment.

### Task 4 — Real Buzz adapter wiring

Add an explicit configuration example under `examples/two-hermes-regtest/` and a supervisor script that starts Alice and Bob as separate OS processes. The supervisor must:

- refuse non-regtest configuration;
- refuse missing channel/identity/state-root configuration;
- keep stdout machine-readable and logs redacted;
- stop on either child process failure;
- never pass private keys to the Python runner.

### Task 5 — Live prerequisite check

Before running a payment:

- verify the Buzz relay health and signed kind-9 path;
- verify both regtest Wavelength daemons and operator connectivity;
- verify Bob can receive and Alice has spendable balance;
- record a preflight report without secrets.

If Buzz is unavailable, stop. Do not silently fall back to the in-memory transport.

### Task 6 — Live P6 execution and recovery

Run one explicitly approved regtest payment. Capture only redacted evidence:

- abbreviated intent/quote/receipt IDs;
- state transitions and timestamps;
- prepared-hash prefix;
- sender/recipient activity status and amounts;
- restart point and reconciliation result.

Do not publish live identifiers or receipts externally without explicit approval.

### Task 7 — Verification and documentation

Run the full suite, Ruff, mypy, vulture, codespell, build, and an independent review. Update `docs/VERIFICATION.md`, `docs/OPERATIONS.md`, `docs/FLOWS.md`, and `CHANGELOG.md` only with verified results. Commit and push the P6 implementation branch; merge/release only after explicit approval.
