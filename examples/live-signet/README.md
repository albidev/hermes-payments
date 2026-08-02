# Live Signet Wavelength example

This example is a reproducible version of the first live Wavelength/Ark
settlement used by Hermes Payments. It is intentionally separate from the
two-Hermes process runner; the checked-in adapter now supports explicitly
approved Signet test runs but this example remains a historical evidence tool.

## Safety contract

- Signet only; this is not a mainnet runner.
- The program never chooses an amount silently: the quote is printed before
  approval.
- `PaymentApproval` stays local. The approval file must bind
  `amount_sat`, `rail`, `send_intent_id`, and `network` exactly.
- A stale approval marker aborts the run.
- The marker is consumed **before** `SendPrepared`; an ambiguous send is never
  retried automatically.
- Wallet state and the generated wallet password live under the configured
  state root with restrictive permissions. They are not part of this repo.
- The runner does not print the Bob invoice. It stores it locally with mode
  `0600` and only passes it to the Wavelength SDK.

## Run from a Wavelength checkout

The Wavelength SDK is a Go module. Run these commands from the root of a
compatible Wavelength checkout, not from the Python repository:

```bash
export HERMES_PAYMENTS_ROOT=/path/to/hermes-payments
export HERMES_PAYMENTS_SIGNET_STATE="$HOME/.hermes/state/hermes-payments-p6-signet"

cd /path/to/wavelength
go run -tags 'wavewalletrpc swapruntime' \
  "$HERMES_PAYMENTS_ROOT/examples/live-signet/runner.go"
```

The runner prints a persistent Alice deposit address. Fund that address through
a Signet faucet, wait for confirmed spendable liquidity, then inspect the
printed quote. Approve only that exact quote by creating the marker it prints:

```text
approved_by=operator
amount_sat=2100
rail=in_ark
send_intent_id=<exact value from the quote>
network=signet
```

The runner consumes the marker before dispatch and waits for Bob's incoming
balance. It is not a substitute for the independent activity check.

## Independent verification

After the runner reports Bob settlement, run:

```bash
cd /path/to/wavelength
go run -tags 'wavewalletrpc swapruntime' \
  "$HERMES_PAYMENTS_ROOT/examples/live-signet/verify/main.go"
```

Expected shape:

```text
wallet=alice id=<reference> kind=send status=complete amount_sat=-2100 fee_sat=0
wallet=bob id=<reference> kind=receive status=complete amount_sat=2100 fee_sat=0
```

The reference must match across the two activity views. The narrow settlement result is
recorded in [`../../docs/live-signet-payment.md`](../../docs/live-signet-payment.md),
and the combined Buzz + Wavelength Signet evidence is in
[`../../docs/live-signet-buzz-vertical.md`](../../docs/live-signet-buzz-vertical.md).

## Configuration

The defaults target the public Signet staging operator used by the verified
run. Override them for a compatible test deployment:

| Variable | Default |
|---|---|
| `HERMES_PAYMENTS_SIGNET_STATE` | `$HOME/.hermes/state/hermes-payments-p6-signet` |
| `WAVELENGTH_NETWORK` | `signet` |
| `WAVELENGTH_OPERATOR` | `signet.wavelength.lightning.finance:443` |
| `WAVELENGTH_SWAP_SERVER` | `swap.signet.wavelength.lightning.finance:443` |
| `WAVELENGTH_ESPLORA_URL` | Signet mempool API |
| `WAVELENGTH_FEE_URL` | Signet fee estimates |
