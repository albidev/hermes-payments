// Command live-signet runs the explicit-approval Signet settlement example.
//
// Run it from a Wavelength checkout because this example imports the local
// Wavelength module:
//
//	go run -tags 'wavewalletrpc swapruntime' ./../hermes-payments/examples/live-signet/runner.go
//
// The example is deliberately not an autonomous payer. It waits for an
// approval file bound to the exact prepared send intent, removes that file
// before dispatch, and never retries an ambiguous SendPrepared call.
package main

import (
	"context"
	"crypto/rand"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
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

func defaultStateRoot() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return filepath.Join(".", ".hermes", "state", "hermes-payments-p6-signet")
	}
	return filepath.Join(home, ".hermes", "state", "hermes-payments-p6-signet")
}

func readOrCreateSecret(path string) ([]byte, error) {
	if data, err := os.ReadFile(path); err == nil {
		return data, nil
	}

	secret := make([]byte, 32)
	if _, err := rand.Read(secret); err != nil {
		return nil, fmt.Errorf("generate wallet password: %w", err)
	}
	value := []byte(fmt.Sprintf("%x", secret))
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, fmt.Errorf("create secret directory: %w", err)
	}
	if err := os.WriteFile(path, value, 0o600); err != nil {
		return nil, fmt.Errorf("write wallet password: %w", err)
	}
	return value, nil
}

func readText(path string) (string, bool) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	return strings.TrimSpace(string(data)), true
}

func writeText(path, value string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(value+"\n"), 0o600)
}

func startWallet(ctx context.Context, stateRoot, name string, password []byte) (*wavewalletdk.Client, error) {
	walletDir := filepath.Join(stateRoot, name)
	cfg := wavewalletdk.DefaultConfig()
	cfg.DataDir = walletDir
	cfg.Network = envOr("WAVELENGTH_NETWORK", "signet")
	cfg.ServerAddress = envOr("WAVELENGTH_OPERATOR", "signet.wavelength.lightning.finance:443")
	cfg.SwapServerAddress = envOr("WAVELENGTH_SWAP_SERVER", "swap.signet.wavelength.lightning.finance:443")
	cfg.WalletType = "lwwallet"
	cfg.WalletEsploraURL = envOr("WAVELENGTH_ESPLORA_URL", "https://mempool-signet.testnet.lightningcluster.com/api")
	cfg.WalletFeeURL = envOr("WAVELENGTH_FEE_URL", "https://nodes.lightning.computer/fees/v1/btctestnet-fee-estimates.json")
	cfg.WalletPasswordFile = filepath.Join(stateRoot, "wallet-password")
	cfg.LogWriter = io.Discard
	cfg.DebugLevel = "error"

	client, err := wavewalletdk.Start(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("start %s: %w", name, err)
	}

	marker := filepath.Join(walletDir, ".initialized")
	if _, err := os.Stat(marker); errors.Is(err, os.ErrNotExist) {
		if _, createErr := client.CreateWallet(ctx, wavewalletdk.CreateWalletRequest{
			WalletPassword: password,
		}); createErr != nil {
			if _, unlockErr := client.UnlockWallet(ctx, wavewalletdk.UnlockWalletRequest{
				WalletPassword: password,
			}); unlockErr != nil {
				_ = client.Stop()
				return nil, fmt.Errorf("initialize %s wallet: create=%v unlock=%w", name, createErr, unlockErr)
			}
		}
		if err := writeText(marker, "initialized"); err != nil {
			_ = client.Stop()
			return nil, fmt.Errorf("persist %s wallet marker: %w", name, err)
		}
	} else if err != nil {
		_ = client.Stop()
		return nil, fmt.Errorf("inspect %s wallet marker: %w", name, err)
	}

	for {
		info, err := client.GetInfo(ctx)
		if err == nil && info.WalletReady() && info.ServerConnected {
			return client, nil
		}
		select {
		case <-ctx.Done():
			_ = client.Stop()
			return nil, fmt.Errorf("wait %s readiness: %w", name, ctx.Err())
		case <-time.After(3 * time.Second):
		}
	}
}

func waitForFunding(ctx context.Context, client *wavewalletdk.Client) error {
	var last string
	for {
		balance, err := client.Balance(ctx)
		if err != nil {
			return fmt.Errorf("alice balance: %w", err)
		}
		line := fmt.Sprintf("alice confirmed_sat=%d pending_in_sat=%d", balance.ConfirmedSat, balance.PendingInSat)
		if line != last {
			fmt.Println(line)
			last = line
		}
		if balance.ConfirmedSat >= 5000 {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(15 * time.Second):
		}
	}
}

func approvalValues(path string) (map[string]string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	values := make(map[string]string)
	for _, line := range strings.Split(string(data), "\n") {
		key, value, ok := strings.Cut(strings.TrimSpace(line), "=")
		if ok && key != "" {
			values[key] = value
		}
	}
	return values, nil
}

func consumeApproval(path, sendIntentID, rail, network string, amountSat int) error {
	values, err := approvalValues(path)
	if err != nil {
		return fmt.Errorf("read approval marker: %w", err)
	}
	expected := map[string]string{
		"amount_sat":     strconv.Itoa(amountSat),
		"rail":           rail,
		"send_intent_id": sendIntentID,
		"network":        network,
	}
	for key, want := range expected {
		if values[key] != want {
			return fmt.Errorf("approval mismatch for %s: got %q want %q", key, values[key], want)
		}
	}
	if err := os.Remove(path); err != nil {
		return fmt.Errorf("consume approval marker before dispatch: %w", err)
	}
	return nil
}

func main() {
	flag.Parse()
	stateRoot := envOr("HERMES_PAYMENTS_SIGNET_STATE", defaultStateRoot())
	if err := os.MkdirAll(stateRoot, 0o700); err != nil {
		panic(err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 12*time.Hour)
	defer cancel()
	password, err := readOrCreateSecret(filepath.Join(stateRoot, "wallet-password"))
	if err != nil {
		panic(err)
	}

	alice, err := startWallet(ctx, stateRoot, "alice", password)
	if err != nil {
		panic(err)
	}
	defer alice.Stop()
	fmt.Println("alice_ready=true")

	bob, err := startWallet(ctx, stateRoot, "bob", password)
	if err != nil {
		panic(err)
	}
	defer bob.Stop()
	fmt.Println("bob_ready=true")

	aliceAddressPath := filepath.Join(stateRoot, "alice-deposit-address")
	aliceAddress, ok := readText(aliceAddressPath)
	if !ok {
		deposit, depositErr := alice.Deposit(ctx, wavewalletdk.DepositRequest{AmountSatHint: 10000})
		if depositErr != nil {
			panic(fmt.Errorf("alice deposit: %w", depositErr))
		}
		aliceAddress = deposit.Address
		if err := writeText(aliceAddressPath, aliceAddress); err != nil {
			panic(fmt.Errorf("persist alice address: %w", err))
		}
	}
	fmt.Printf("alice_deposit_address=%s\n", aliceAddress)

	invoicePath := filepath.Join(stateRoot, "bob-invoice")
	invoice, ok := readText(invoicePath)
	if !ok {
		receive, receiveErr := bob.Receive(ctx, wavewalletdk.ReceiveRequest{
			AmountSat: 2100,
			Memo:      "Hermes Payments P6 live Signet payment",
		})
		if receiveErr != nil {
			panic(fmt.Errorf("bob receive: %w", receiveErr))
		}
		invoice = receive.Invoice
		if err := writeText(invoicePath, invoice); err != nil {
			panic(fmt.Errorf("persist bob invoice: %w", err))
		}
	}
	fmt.Println("bob_invoice_ready=true")

	if err := waitForFunding(ctx, alice); err != nil {
		panic(err)
	}
	quote, err := alice.PrepareSend(ctx, wavewalletdk.PrepareSendRequest{
		Invoice:   invoice,
		Note:      "Hermes Payments P6 live Signet payment",
		MaxFeeSat: 1000,
	})
	if err != nil {
		panic(fmt.Errorf("prepare send: %w", err))
	}
	fmt.Printf("quote amount_sat=%d expected_fee_sat=%d fee_known=%t total_outflow_sat=%d total_known=%t rail=%s quote_status=%s send_intent_id=%s payment_hash=%s warning=%s\n",
		quote.AmountSat, quote.ExpectedFeeSat, quote.FeeKnown, quote.ExpectedTotalOutflowSat,
		quote.TotalOutflowKnown, quote.Rail, quote.QuoteStatus, quote.SendIntentID, quote.PaymentHash, quote.Warning)
	if credit := quote.CreditPreview; credit != nil {
		fmt.Printf("credit_preview must_use=%t applied_sat=%d shortfall_sat=%d topup_sat=%d ark_funding_sat=%d\n",
			credit.MustUseCredit, credit.CreditAppliedSat, credit.CreditShortfallSat,
			credit.CreditTopupSat, credit.ArkFundingSat)
	} else {
		fmt.Println("credit_preview=nil")
	}
	fmt.Println("quote_ready=true")

	approvalPath := filepath.Join(stateRoot, "APPROVE_SEND")
	if _, err := os.Stat(approvalPath); err == nil {
		panic("stale APPROVE_SEND marker exists; remove it before preparing a new payment")
	} else if !errors.Is(err, os.ErrNotExist) {
		panic(fmt.Errorf("inspect approval marker: %w", err))
	}
	fmt.Printf("awaiting_approval_marker=%s\n", approvalPath)
	for {
		if _, err := os.Stat(approvalPath); err == nil {
			break
		} else if !errors.Is(err, os.ErrNotExist) {
			panic(fmt.Errorf("inspect approval marker: %w", err))
		}
		select {
		case <-ctx.Done():
			panic(ctx.Err())
		case <-time.After(2 * time.Second):
		}
	}

	network := envOr("WAVELENGTH_NETWORK", "signet")
	if err := consumeApproval(approvalPath, quote.SendIntentID, string(quote.Rail), network, int(quote.AmountSat)); err != nil {
		panic(err)
	}
	result, err := alice.SendPrepared(ctx, wavewalletdk.SendPreparedRequest{SendIntentID: quote.SendIntentID})
	if err != nil {
		panic(fmt.Errorf("send prepared: %w", err))
	}
	fmt.Printf("send_dispatched=true actual_amount_sat=%d\n", result.ActualAmountSat)

	deadline := time.Now().Add(20 * time.Minute)
	for time.Now().Before(deadline) {
		balance, balanceErr := bob.Balance(ctx)
		if balanceErr != nil {
			panic(fmt.Errorf("bob balance: %w", balanceErr))
		}
		fmt.Printf("bob confirmed_sat=%d pending_in_sat=%d\n", balance.ConfirmedSat, balance.PendingInSat)
		if balance.ConfirmedSat >= quote.AmountSat || balance.PendingInSat >= quote.AmountSat {
			fmt.Println("bob_settlement_detected=true")
			return
		}
		time.Sleep(15 * time.Second)
	}
	panic("timed out waiting for Bob settlement")
}
