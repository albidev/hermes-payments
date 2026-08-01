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

The adapter accepts only:

```python
WavelengthAdapter(
    executor=...,
    rpc_server="localhost:10029",
    network="regtest",
    no_tls=True,
    no_macaroons=True,
)
```

Any other network is rejected at construction time. The no-TLS/no-macaroon options are for a local regtest harness only; they are not a production security recommendation.

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

## Buzz boundary

The live transport uses the Buzz CLI surface:

```text
buzz messages send --channel <channel-uuid> --content <envelope-json>
buzz messages get --channel <channel-uuid> --kinds 9
```

Buzz owns signing and channel tags. Hermes Payments never reads or constructs the private key. Credentials supplied by an ACP/harness environment must stay outside message content and audit payloads.

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
