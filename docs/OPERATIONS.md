# Operations

This document describes how to run the repository safely and how to interpret the evidence it produces.

## Local deterministic run

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest -q
```

This path uses no network, no Buzz CLI, no Wavelength daemon, and no external credentials.

## Wavelength adapter boundary

The adapter accepts only `regtest` by default or an explicitly approved
`signet` live test:

```python
WavelengthAdapter(
    executor=...,
    rpc_server="localhost:10029",
    network="regtest",
    no_tls=True,
    no_macaroons=True,
)
```

Any other network is rejected at construction time. The no-TLS/no-macaroon
options are for the local regtest or isolated Signet test daemons only; they
are not a production security recommendation.

For the current approved Signet run, use separate local RPC endpoints for the
two wallet daemons: Alice `localhost:11329`, Bob `localhost:11339`.

### Prepare

The adapter uses the raw RPC path, not the high-level `wavecli send` verb:

```text
wavecli --network regtest --rpcserver localhost:10029 --no-tls --no-macaroons --json \
  dev wavewalletrpc.WalletService PrepareSend \
  --request-json '{"invoice":"<local test invoice>","max_fee_sat":100}'
```

The response must contain `send_intent_id`, exact amount, known fee, known total outflow, payment hash, non-expired timestamp, and route/status information.

### Execute

```text
wavecli --network regtest --rpcserver localhost:10029 --no-tls --no-macaroons --json \
  dev wavewalletrpc.WalletService Send \
  --request-json '{"send_intent_id":"<opaque prepared id>"}'
```

The exact `send_intent_id` returned by `PrepareSend` must be used. Do not call high-level `send` after approval: that path prepares a fresh intent and breaks approval binding.

### Receipt verification

The recipient verifies incoming activity:

```text
wavecli --network regtest --rpcserver localhost:10029 --no-tls --no-macaroons --json \
  activity --format json --kind recv
```

The verifier searches for a `COMPLETE` entry with the expected settlement reference and exact amount. Sender-side `send` activity is not sufficient evidence for a recipient receipt.

### Ambiguous dispatch recovery

After a process restart, an interrupted `EXECUTING` intent is recovered as
`RECONCILIATION_REQUIRED`. The process can query sender-side activity without
retrying the payment:

```json
{"target":"alice","command":"recover","max_wait_seconds":720,"poll_interval_seconds":2}
```

The default `recover` call uses a zero-second wait and performs one query.
`max_wait_seconds` is capped at 720 seconds; `poll_interval_seconds` must be
between 0.01 and 60 seconds. `COMPLETE` is recorded as evidence but does not
settle the intent until Bob's verified `PaymentReceipt` arrives. `PENDING` and
`UNKNOWN` remain fail-closed. No recovery path calls `Send`.

## Buzz boundary

The live transport uses the Buzz CLI surface:

```text
buzz messages send --channel <channel-uuid> --content <envelope-json>
buzz messages get --channel <channel-uuid> --kinds 9
```

Buzz owns signing and channel tags. Hermes Payments never reads or constructs the private key. Credentials supplied by an ACP/harness environment must stay outside message content and audit payloads.

## P6 two-process runner

The process-boundary example launches Alice and Bob as separate OS processes
with independent state roots. A real two-identity run requires credentials to
be supplied by the external Buzz environment under role-specific names:

```text
BUZZ_RELAY_URL
BUZZ_ALICE_PRIVATE_KEY
BUZZ_BOB_PRIVATE_KEY
```

Optional role-specific `BUZZ_ALICE_AUTH_TAG` and `BUZZ_BOB_AUTH_TAG` values are
inherited by the corresponding child. They are never written to the generated
wrapper or emitted in JSONL output. Do not replace the two role-specific keys
with one shared key: that would test a self-loop, not two Hermes identities.

Start the supervisor only after the relay health probe and both wallet daemons
are ready:

```bash
python examples/two_hermes_regtest/run.py \
  --channel <channel-uuid> \
  --alice-state-root /tmp/hermes-payments-alice \
  --bob-state-root /tmp/hermes-payments-bob \
  --alice-pubkey <alice-pubkey-hex> \
  --bob-pubkey <bob-pubkey-hex> \
  --approver-pubkey <approver-pubkey-hex> \
  --network signet \
  --alice-wave-rpc-server localhost:11329 \
  --bob-wave-rpc-server localhost:11339 \
  --buzz-bin <path-to-buzz>
```

Supervisor input is JSONL, for example:

```json
{"target":"alice","command":"status"}
{"target":"bob","command":"status"}
{"command":"shutdown"}
```

`prepare` returns a display-safe prepared-hash prefix. Passing that prefix
back to the `approve` command resolves it only against the prepared record for
the explicitly named intent; the full binding remains inside Alice's process.
The supervisor never falls back to the in-memory transport.

## Live operational gate

A live claim requires two independently configured Hermes instances, two actual wallet daemons, real Buzz delivery, funded sender liquidity, recipient-side incoming activity, a receipt delivered through Buzz, and replay/expiry/timeout evidence on the same deployment.

## Bootstrap and live funding

If a fresh wallet needs an on-chain deposit to board an Ark operator, document that step as **bootstrap funding**. Do not describe it as the agent payment. Wait for spendable liquidity, refresh the quote, and verify the actual route before approval:

```text
funding pending → confirmation/boarding → spendable balance
→ fresh PrepareSend → inspect route + fee → local approval → Send
```

Never approve a stale prepared intent after a long boarding delay; re-prepare it.

## Stop conditions

Stop immediately when the network is not explicitly allowed, the quote is expired, the fee exceeds policy, route/rail is unknown or unexpectedly downgraded, `fee_known` or `total_outflow_known` is false, send returns `PENDING`/unknown/timeout, receipt verification fails, or a credential/real payment identifier appears in logs.
