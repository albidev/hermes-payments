# Flow Catalogue

The diagrams in this document describe protocol behavior. Mermaid source files are versioned under [`docs/diagrams/`](diagrams/).

## 1. Happy path

```mermaid
sequenceDiagram
    participant A as Hermes A / Alice
    participant BZ as Buzz channel
    participant B as Hermes B / Bob
    participant W as Wavelength adapter
    participant D as Wavelength daemon
    A->>A: Create PaymentIntent
    A->>A: Policy checks expiry, recipient, amount, max fee
    A->>BZ: kind 9: payment_intent envelope
    BZ->>B: Signed event
    B->>B: Validate kind, h-tag, envelope, expiry, author
    B->>BZ: kind 9: payment_quote envelope
    BZ->>A: Signed event
    A->>A: Validate quote and fee constraint
    A->>W: prepare(invoice, amount, max_fee)
    W->>D: Raw PrepareSend RPC
    D-->>W: send_intent_id + exact preview
    W-->>A: prepared_payload + prepared_hash
    A->>A: Local human approves exact triple
    A->>W: execute(prepared_payload, prepared_hash)
    W->>D: Raw Send with same send_intent_id
    D-->>W: COMPLETE + settlement reference
    W-->>A: ExecuteResult
    B->>D: Query activity --kind recv
    D-->>B: Matching COMPLETE entry
    B->>BZ: kind 9: payment_receipt envelope
    BZ->>A: Signed receipt
    A->>A: Validate receipt and settle state
```

## 2. Receipt-mediated path

The adapter may return `PENDING`, an unknown status, or an error after dispatch. The sender must stop:

```mermaid
sequenceDiagram
    participant A as Alice policy
    participant W as Adapter
    participant D as Daemon
    participant B as Bob verifier
    participant BZ as Buzz
    A->>W: execute exact prepared intent
    W->>D: Send
    D-->>W: timeout / PENDING / unknown
    W-->>A: AmbiguousResult
    A->>A: EXECUTING → RECONCILIATION_REQUIRED
    Note over A: No retry. No second Send.
    B->>D: Verify recipient recv activity
    D-->>B: COMPLETE matching entry
    B->>BZ: PaymentReceipt
    BZ->>A: Receipt
    A->>A: Verify receipt → SETTLED
```

If Bob cannot independently verify the payment, the intent remains in reconciliation.

## 3. Lifecycle state machine

```mermaid
stateDiagram-v2
    [*] --> DRAFT
    DRAFT --> SUBMITTED: submit
    SUBMITTED --> QUOTED: quote_received
    QUOTED --> PREPARED: prepared
    PREPARED --> APPROVED: approved
    APPROVED --> EXECUTING: executing
    EXECUTING --> SETTLED: settled
    EXECUTING --> RECONCILIATION_REQUIRED: timeout / unknown / adapter_error / expiry
    RECONCILIATION_REQUIRED --> SETTLED: receipt_received
    RECONCILIATION_REQUIRED --> SETTLED: confirm_settled
    DRAFT --> CANCELLED: cancel
    SUBMITTED --> CANCELLED: cancel
    QUOTED --> CANCELLED: cancel
    PREPARED --> CANCELLED: cancel
    SUBMITTED --> REJECTED: rejected
    QUOTED --> REJECTED: rejected
    PREPARED --> REJECTED: rejected
    APPROVED --> REJECTED: rejected
    SUBMITTED --> EXPIRED: expired
    QUOTED --> EXPIRED: expired
    PREPARED --> EXPIRED: expired
    APPROVED --> EXPIRED: expired
    QUOTED --> FAILED: adapter_error
    PREPARED --> FAILED: adapter_error
    APPROVED --> FAILED: adapter_error
```

`SETTLED`, `FAILED`, `REJECTED`, `EXPIRED`, and `CANCELLED` are terminal. `RECONCILIATION_REQUIRED` is intentionally non-terminal.

## 4. Bootstrap versus settlement

```mermaid
flowchart LR
    F[On-chain faucet or existing liquidity] --> U[Wallet deposit / boarding]
    U --> V[Spendable VTXO or operator credit]
    V --> P[Prepare invoice payment]
    P --> R{Wavelength route}
    R -->|same operator / compatible| A[Ark in-operator settlement]
    R -->|network route| L[Lightning settlement]
    A --> Q[Bob incoming activity]
    L --> Q
    Q --> C[Receipt]
```

The on-chain step is not the agent-to-agent payment. It is the initial liquidity/boarding step for a wallet that otherwise has nothing spendable. If an existing VTXO or operator credit is available, it can be skipped.

## 5. What is never in the flow

```mermaid
flowchart LR
    Buzz[Remote Buzz event] -. cannot authorize .-> Spend[Wallet spend]
    LLM[LLM suggestion] -. cannot authorize .-> Spend
    Retry[Automatic retry] -. forbidden after ambiguity .-> Spend
    Approval[Local approval of exact prepared hash] --> Spend
```
