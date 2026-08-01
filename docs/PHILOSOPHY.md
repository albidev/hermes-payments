# Philosophy

Hermes Payments is built around a simple idea:

> **An agent may negotiate a payment, but it must not be allowed to silently turn a message into money movement.**

That sounds obvious until the system has a relay, a wallet, a CLI, retries, timeouts, and an LLM in the room. Then every shortcut becomes a possible authorization bug.

## 1. Coordination is not authorization

Buzz is useful because agents need identity, delivery, and an audit trail. A signed message answers “who authored this event?” It does not answer “should this wallet spend money right now?”

Remote messages and local authorization therefore live in separate universes. A `PaymentIntent`, `PaymentQuote`, or `PaymentReceipt` can cross Buzz. `PaymentApproval` cannot.

## 2. Prepare before approve

A human should not approve a vague promise such as “pay Bob.” The adapter first performs a non-mutating prepare operation and returns an opaque payload containing the exact send intent, amount, fee, payment reference, and expiry.

The approval binds:

```text
(intent_id, quote_id, prepared_hash)
```

The adapter then consumes the same prepared intent. If the payload changes, the approval is no longer valid. Less convenient than a one-shot `send`; considerably more defensible.

## 3. Ambiguity is a first-class state

The worst payment failure is not “the command returned an error.” It is: “the command timed out after the daemon may have accepted it.” Treating that as an ordinary error and retrying is how double payments happen.

Hermes Payments models this as `RECONCILIATION_REQUIRED`. The system stops, recipient-side activity is checked, and only a human or a verified receipt can resolve the intent.

## 4. Receipts are evidence, not ceremony

A receipt is useful only if tied to something independently observable. The recipient verifies its own incoming activity by settlement reference and amount before publishing `PaymentReceipt`.

A signed receipt without independent verification is just a nicely formatted assertion.

## 5. Multi-rail means multi-assumption

Lightning, Ark, and on-chain settlement do not have identical instructions, fees, expiry behavior, or receipt semantics. A rail-neutral core should not erase those differences; it should isolate them behind a strict adapter boundary.

The current protocol supports a Lightning invoice receive instruction. Wavelength may internally choose an Ark route for that invoice. That is useful evidence, but not a reason to pretend that every Ark primitive is already a wire-level feature.

A new rail becomes real only when it has:

- a receive-instruction schema;
- policy and fee semantics;
- prepare/execute binding;
- receipt verification;
- replay and expiry tests;
- explicit operational documentation.

## 6. On-chain bootstrap is not on-chain settlement

A wallet may need an initial on-chain deposit to enter an Ark operator or acquire spendable liquidity. That deposit is a funding and boarding event. It is not the agent payment protocol.

```text
on-chain deposit → wallet/operator bootstrap → spendable VTXO
                                      ↓
                         agent-to-agent Ark settlement
```

If an operator provides credit or the wallet already has a VTXO, the bootstrap deposit can disappear from the test altogether. What cannot disappear is the need for some initial liquidity somewhere. Physics remains annoyingly non-negotiable.

## 7. Small honest scope

The first vertical slice is intentionally narrow:

- one payment intent;
- two identities;
- one channel;
- one adapter;
- one human approval;
- one receipt;
- regtest only.

The project earns broader scope by proving each boundary, not by putting “multi-rail autonomous commerce” in the README before the first live daemon has survived a timeout.

## 8. The LLM is not the policy engine

An LLM may propose a payment or explain a quote. It must not be the final source of truth for recipient identity, fee limits, expiry, approval binding, retry policy, or receipt validity. Those decisions belong to typed models, deterministic code, and explicit human policy.
