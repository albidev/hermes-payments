# Diagrams

The diagrams are source-controlled so architecture changes are reviewable.

## Mermaid sources

- [`payment-sequence.mmd`](payment-sequence.mmd) — happy-path interaction sequence.
- [`reconciliation-sequence.mmd`](reconciliation-sequence.mmd) — ambiguous execution and receipt recovery.
- [`state-machine.mmd`](state-machine.mmd) — intent lifecycle and fail-closed transitions.
- [`bootstrap-settlement.mmd`](bootstrap-settlement.mmd) — on-chain bootstrap versus Ark/Lightning settlement.
- [`trust-boundaries.mmd`](trust-boundaries.mmd) — data and authorization boundaries.

GitHub renders Mermaid in Markdown files. The `.mmd` files can also be rendered with Mermaid CLI or any compatible editor.

## Standalone visual diagram

Open [`architecture.html`](architecture.html) in a browser. It is self-contained, uses inline SVG/CSS, and has no project dependencies.

```bash
open docs/diagrams/architecture.html  # macOS
```

The visual diagram is explanatory, not an executable topology. Normative behavior remains in the code and [CONTRACT.md](../CONTRACT.md).
