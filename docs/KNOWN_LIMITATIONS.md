# Known Limitations

This file records current design constraints that are intentional, visible in the implementation, and not yet covered by a broader production architecture.

## One active payment flow per `PaymentOrchestrator`

The current `PaymentOrchestrator` is designed to drive **one active payment flow at a time per orchestrator instance**.

It tracks multiple intent records for idempotency and audit purposes, but lifecycle operations resolve the active intent by state. The current implementation explicitly expects a single matching intent in states such as `SUBMITTED`, `QUOTED`, `APPROVED`, or `EXECUTING`; it is not a concurrent multi-payment scheduler.

### What this means

- Do not use one `PaymentOrchestrator` instance as a general-purpose concurrent payment queue.
- Do not assume that two overlapping payment flows can safely share the same orchestration context.
- A deployment that needs concurrent payments must isolate flows behind separate orchestrator instances and state roots, or add a coordinator that selects intents by explicit `intent_id`.
- The transport-neutral `PeerTransport` and `HermesPeer` boundaries do not remove this policy-layer limitation.

### Why it is declared here

This constraint is deliberate and currently keeps the state-machine and approval-binding behavior easy to audit. It is safer to expose the limitation than to imply concurrency support from the fact that the store can retain more than one historical intent.

### Future work

A future multi-payment design should define and test:

1. explicit `intent_id` selection for every lifecycle operation;
2. per-intent locks or an equivalent concurrency model;
3. independent approval and prepared-payload bindings per flow;
4. restart/recovery semantics when several flows are active;
5. fair scheduling and backpressure at the process boundary;
6. audit and reconciliation behavior for interleaved transport messages.

Until those guarantees exist, this repository should be evaluated as a **single-active-flow orchestrator**.

## Live crash/restart recovery is validated only on Signet

The code now implements read-only automatic recovery for an interrupted
dispatch. On restart it converts `EXECUTING` to
`RECONCILIATION_REQUIRED`, queries sender activity, and can poll a matching
`PENDING` entry for a bounded window of at most 12 minutes. It never retries
`Send`; `COMPLETE` evidence still requires the recipient's verified receipt
before the intent becomes `SETTLED`.

The deterministic suite covers this behavior, including a simulated
`PENDING → COMPLETE` sequence and zero post-restart execute calls. The live
Signet gate also covered a supervisor kill after dispatch, same-state-root
restart, `COMPLETE` recovery, exactly one matching sender/receiver activity
pair, and receipt-mediated settlement. This is operational evidence for the
explicit Signet test setup only; it is not production, mainnet, or custody
evidence.
