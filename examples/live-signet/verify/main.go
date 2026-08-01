// Command verify-live-signet reads the sender and recipient activity views.
//
// Run from a Wavelength checkout with the same state root used by runner.go:
//
//	go run -tags 'wavewalletrpc swapruntime' ./../hermes-payments/examples/live-signet/verify
package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	wavewalletdk "github.com/lightninglabs/wavelength/sdk/wavewalletdk"
)

func envOr(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func stateRoot() string {
	if value := strings.TrimSpace(os.Getenv("HERMES_PAYMENTS_SIGNET_STATE")); value != "" {
		return value
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".", ".hermes", "state", "hermes-payments-p6-signet")
	}
	return filepath.Join(home, ".hermes", "state", "hermes-payments-p6-signet")
}

func startWallet(ctx context.Context, name string) (*wavewalletdk.Client, error) {
	root := stateRoot()
	cfg := wavewalletdk.DefaultConfig()
	cfg.DataDir = filepath.Join(root, name)
	cfg.Network = envOr("WAVELENGTH_NETWORK", "signet")
	cfg.ServerAddress = envOr("WAVELENGTH_OPERATOR", "signet.wavelength.lightning.finance:443")
	cfg.SwapServerAddress = envOr("WAVELENGTH_SWAP_SERVER", "swap.signet.wavelength.lightning.finance:443")
	cfg.WalletType = "lwwallet"
	cfg.WalletEsploraURL = envOr("WAVELENGTH_ESPLORA_URL", "https://mempool-signet.testnet.lightningcluster.com/api")
	cfg.WalletFeeURL = envOr("WAVELENGTH_FEE_URL", "https://nodes.lightning.computer/fees/v1/btctestnet-fee-estimates.json")
	cfg.WalletPasswordFile = filepath.Join(root, "wallet-password")
	cfg.LogWriter = io.Discard
	cfg.DebugLevel = "error"
	return wavewalletdk.Start(ctx, cfg)
}

func waitReady(ctx context.Context, client *wavewalletdk.Client) error {
	for {
		info, err := client.GetInfo(ctx)
		if err == nil && info.WalletReady() && info.ServerConnected {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
}

func printEntries(name string, list *wavewalletdk.ListResult) {
	if list == nil || list.Activity == nil {
		fmt.Printf("wallet=%s activity=nil\n", name)
		return
	}
	for _, entry := range list.Activity.Entries {
		fmt.Printf("wallet=%s id=%s kind=%s status=%s amount_sat=%d fee_sat=%d\n",
			name, entry.ID, entry.Kind, entry.Status, entry.AmountSat, entry.FeeSat)
	}
}

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	alice, err := startWallet(ctx, "alice")
	if err != nil {
		panic(fmt.Errorf("start alice: %w", err))
	}
	defer alice.Stop()
	if err := waitReady(ctx, alice); err != nil {
		panic(fmt.Errorf("alice readiness: %w", err))
	}

	bob, err := startWallet(ctx, "bob")
	if err != nil {
		panic(fmt.Errorf("start bob: %w", err))
	}
	defer bob.Stop()
	if err := waitReady(ctx, bob); err != nil {
		panic(fmt.Errorf("bob readiness: %w", err))
	}

	aliceActivity, err := alice.List(ctx, wavewalletdk.ListRequest{
		View:  wavewalletdk.ListViewActivity,
		Kinds: []wavewalletdk.EntryKind{wavewalletdk.EntryKindSend},
		Limit: 20,
	})
	if err != nil {
		panic(fmt.Errorf("list alice activity: %w", err))
	}
	bobActivity, err := bob.List(ctx, wavewalletdk.ListRequest{
		View:  wavewalletdk.ListViewActivity,
		Kinds: []wavewalletdk.EntryKind{wavewalletdk.EntryKindReceive},
		Limit: 20,
	})
	if err != nil {
		panic(fmt.Errorf("list bob activity: %w", err))
	}
	printEntries("alice", aliceActivity)
	printEntries("bob", bobActivity)
}
