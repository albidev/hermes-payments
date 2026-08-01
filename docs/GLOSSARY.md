# Glossary

| Term | Meaning |
|---|---|
| **Alice** | Example sender Hermes instance in tests and diagrams. |
| **Ark** | Fast off-chain settlement/operator model used by Wavelength. Currently an internal route observation, not a first-class wire rail here. |
| **Approval triple** | `(intent_id, quote_id, prepared_hash)`, the exact binding a human approves. |
| **Boarding** | Moving initial wallet liquidity into an operator/off-chain system so it becomes spendable there. |
| **Bob** | Example recipient Hermes instance in tests and diagrams. |
| **Buzz** | External Hermes coordination and signed identity transport. |
| **BOLT11** | Lightning invoice format used as the current receive instruction. |
| **Envelope** | Versioned JSON object carried inside Buzz kind-9 content. |
| **Intent** | A sender's request to pay a recipient. |
| **Prepared payload** | Opaque adapter-owned bytes containing the exact execution binding. It never crosses Buzz. |
| **Quote** | Recipient response containing a receive instruction, fee, and expiry. |
| **Rail** | Protocol-level settlement capability exposed to policy; not necessarily the backend's internal route. |
| **Receipt** | Recipient-authored settlement evidence after independently verifying incoming activity. |
| **Reconciliation** | Manual resolution of an ambiguous execution outcome. |
| **Settlement reference** | Rail-specific identifier used to verify a payment, such as a payment hash. |
| **VTXO** | A spendable off-chain output in an Ark-like system. |
| **Wavelength** | External wallet toolkit and the first settlement adapter integrated here. |
