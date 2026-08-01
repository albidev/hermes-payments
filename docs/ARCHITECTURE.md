# Architecture

Hermes Payments is a set of narrow boundaries rather than one large “payment agent.” Each boundary has a different trust level and a different job.

## System view

```mermaid
flowchart TB
    subgraph H1[Hermes A — sender]
        intent[PaymentIntent]
        policy[PaymentOrchestrator\npolicy + state]
        approval[Local human approval]
        adapter[SettlementAdapter]
        intent --> policy
        policy --> approval
        approval --> adapter
    end
    subgraph PT[Transport-neutral peer boundary]
        peer[PeerTransport\ntyped messages + metadata]
    end
    subgraph B[Buzz adapter — current transport]
        channel[NIP-29 channel\nkind 9 messages]
    end
    subgraph H2[Hermes B — recipient]
        validate[Untrusted event validation]
        quote[PaymentQuote]
        verify[Recipient-side receipt verification]
        receipt[PaymentReceipt]
        validate --> quote
        verify --> receipt
    end
    policy <-->|Intent / Quote / Receipt| peer
    peer <--> channel
    channel <--> validate
    adapter --> wavelength[WavelengthAdapter]
    wavelength --> rpc[wavecli raw RPC]
    rpc --> daemon[waved / wallet daemon]
    daemon --> rail[Settlement rail]
    rail --> verify
    receipt --> channel
```

## Layers

### Domain layer — `models.py`

Defines typed `PaymentIntent`, `PaymentQuote`, `PaymentApproval`, `PaymentReceipt`, transport-neutral `AgentIdentity`, `Rail`, `RailReceiveInstruction`, canonical serialization, and SHA-256 identifiers. Models do not call Buzz, Wavelength, subprocesses, or the network. `BuzzIdentity` remains a source-compatible alias for protocol-v1 callers; it is not the domain's transport contract.

### State layer — `state_machine.py`

The explicit finite automaton is the source of truth for lifecycle transitions. The key rule is:

```text
EXECUTING + timeout/unknown/error → RECONCILIATION_REQUIRED
```

It never becomes a normal retryable failure after execution may have started.

### Policy layer — `policy.py`

`PaymentOrchestrator` composes the state machine with intent idempotency, approval-triple replay protection, receipt uniqueness, durable JSON storage, append-only JSONL audit records, expiry checks, allowlists, fee limits, and adapter invocation only after approval.

### Peer boundary — `peer_transport.py` and `peer.py`

`PeerTransport` is the application-facing contract between two Hermes peers. It carries typed payment messages and returns `PeerMessage` metadata (`message_id`, author, and publication timestamp). `HermesPeer` composes that contract with local policy and exposes explicit intent, quote, and receipt handoffs. It does not know whether delivery uses Buzz, HTTP, WebSocket, a Unix socket, or an in-memory test hub.

`PaymentApproval` is rejected before it can enter this boundary. Transport message IDs are retained so a relay duplicate can be distinguished from a domain duplicate; policy idempotency remains the authority for replay safety.

`PaymentOrchestrator` currently supports one active payment flow per instance. It can retain historical intent records for idempotency and audit, but it is not a concurrent multi-payment scheduler: lifecycle operations expect one matching active intent by state. Deployments needing concurrency must isolate flows or introduce explicit intent selection and locking. See [Known limitations](KNOWN_LIMITATIONS.md).

### Buzz adapter — `envelope.py` and `transport.py`

Buzz is the current concrete `PeerTransport` adapter. Three domain message types travel through a single NIP-29 kind-9 channel message. The content field is a versioned JSON envelope:

```json
{
  "protocol": "hermes-payments",
  "version": "1",
  "type": "payment_intent",
  "payload": {}
}
```

The `h` channel tag is managed by Buzz. Received events are treated as untrusted and checked for kind, channel, protocol/version, schema, expiry, and author identity. Those checks stay inside the adapter; the peer and policy layers see only validated `PeerMessage` objects.

### Settlement layer — `adapter.py`

`SettlementAdapter` exposes:

1. `prepare()` — non-mutating preview and opaque binding payload;
2. `execute()` — exact prepared payload, after local approval;
3. `verify_receipt()` — independent recipient-side settlement check.

`WavelengthAdapter` is the current concrete implementation. It is hard-gated to `regtest` and uses raw RPC because high-level `wavecli send` prepares a fresh intent internally.

## Trust boundaries

| Boundary | Untrusted input | Enforcement |
|---|---|---|
| Buzz → adapter | Event JSON, tags, author, kind | `validate_received_event()` |
| Peer transport → policy | Expired or mismatched messages | `PeerMessage`, model, expiry, identity checks |
| Policy → adapter | Adapter results | State machine, fee, rail checks |
| Adapter → wallet CLI | CLI JSON and exit status | Strict parsing, redaction, ambiguity mapping |
| Activity → receipt | Wallet activity entries | Reference, amount, and `COMPLETE` match |

## Persistence

The policy core supports two optional local stores:

- **Idempotency store:** JSON, atomically replaced, containing intent IDs, approval triples, and receipt bindings.
- **Audit log:** append-only JSONL with sequence number, timestamp, event name, and selected identifiers.

These are local policy artifacts. They are not Buzz messages and do not contain the opaque prepared payload.

## Current rail caveat

The protocol `Rail` enum currently contains `LIGHTNING` only. Wavelength's raw prepare response exposes an internal route label. In the external Signet experiment, an invoice quote returned `in_ark` with zero expected fee. The current adapter preserves the raw route inside its opaque payload but reports the protocol rail as `LIGHTNING`.

That is a documented limitation, not an invisible conversion. First-class Ark semantics require a contract extension and new tests; see [RAILS.md](RAILS.md) and [DECISIONS.md](DECISIONS.md).

## Deterministic versus live architecture

The transport-neutral proof uses two independent `HermesPeer` instances, an `InMemoryTransportHub`, and separate policy engines. The Buzz proof uses `FakeExecutor` and the Wavelength proof uses `FakeWavecliExecutor`. This proves composition without making Buzz part of the application contract. A live proof additionally needs two daemons, real Buzz delivery, funded wallets, real receipt activity, and recovery evidence. See [VERIFICATION.md](VERIFICATION.md).
