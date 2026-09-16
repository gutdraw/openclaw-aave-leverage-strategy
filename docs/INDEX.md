# OpenClaw Aave leverage strategy

## Current operational tools

- `python scripts/audit_history.py --trades trades.jsonl --journal trades.sqlite3`
  produces an offline, read-only audit of cycle decisions, trade joins, journal
  coverage, and modelled P&L.
- Add `--config my-config.yml` to enrich the report with read-only transaction
  receipts and ERC-20 transfer evidence through the configured Base RPC. The
  command never creates a signer, renews an MCP session, or broadcasts a
  transaction.
- `scripts/check_health.py` remains the local supervision check. Heartbeats now
  include process/config provenance and local observation timestamps.

## Accounting interpretation

`realised_usd` is a theoretical price-delta result. It is not net wallet P&L:
gas, swap execution, Aave interest, MCP fees, and manual wallet flows require
separate evidence. The audit report therefore leaves
`reconciled_realised_usd` null until those inputs are available.

## Runtime safety boundary

The strategy, leverage, sizing, exits, and fail-closed reconciliation rules are
unchanged by the observability work. Secret/token permissions and rotation are
managed separately and are intentionally outside this change.
