# Security Policy

Hermes Payments is an early-stage payment protocol. Treat it as security-sensitive experimental software, not as a production wallet or autonomous treasury.

## Scope

The security boundary includes payment authorization, replay protection, Buzz event validation, Wavelength command construction, receipt verification, and reconciliation. It does not include the cryptographic implementation of Buzz, Bitcoin, Lightning, Ark, or Wavelength themselves.

## Reporting a vulnerability

Do not open a public issue containing credentials, private keys, real invoices, payment hashes, relay URLs with sensitive query parameters, or exploitable details. Contact the repository maintainers through a private channel and include the affected version, minimal reproduction, impact, and whether funds could move, duplicate, or become unreconcilable. Never send secrets in a report.

## Security invariants

1. The Wavelength adapter accepts only `regtest` or an explicitly selected
   Signet test run; mainnet, testnet, and arbitrary networks are rejected.
2. Buzz messages are coordination and audit data, not spending authorization.
3. `PaymentApproval` never crosses the transport boundary.
4. Approval binds exactly `(intent_id, quote_id, prepared_hash)`.
5. The adapter executes the exact prepared send intent; it must not silently prepare a second one.
6. Duplicate intents, approvals, and receipts are rejected or treated as idempotent according to the contract.
7. A `PENDING`, unknown, timeout, or post-dispatch error never becomes an automatic retry.
8. Receipt verification happens against recipient-side incoming activity and checks settlement reference and amount.
9. Errors redact invoices, macaroon paths, and long hexadecimal identifiers.
10. No component in this repository reads or constructs private credentials.

## Threat model

| Threat | Response |
|---|---|
| Forged/replayed Buzz event | Validate kind, channel, envelope, expiry, and author identity. |
| Valid event requesting an unsafe payment | Apply expiry, recipient allowlist, rail allowlist, and fee constraints. |
| Approval copied to another payment | Bind approval to intent, quote, and prepared payload hash. |
| CLI silently re-prepares | Use raw `PrepareSend`, then raw `Send` with the exact ID. |
| Broadcast result is ambiguous | Enter `RECONCILIATION_REQUIRED`; do not retry. |
| False receipt | Verify recipient-side `recv` activity, reference, amount, and `COMPLETE`. |
| Sensitive data in errors | Redact invoices, macaroon paths, and 64-character hashes. |
| Mainnet accidentally selected | Reject unsupported configuration at adapter construction; no mainnet default exists. |

## Known limitations

- The idempotency store is local JSON, not a multi-process database.
- The audit log is append-only JSONL but is not a tamper-evident ledger.
- Buzz signing is delegated to the Buzz CLI/ACP boundary.
- The checked-in integration proof uses fake executors.
- The protocol has a Lightning-only `Rail` enum today. Wavelength may select an internal Ark route while paying an invoice; first-class Ark wire semantics remain open.

## Operational rule

If settlement outcome is uncertain, stop. Verify recipient-side activity and reconcile the intent manually. Do not “try once more.”
