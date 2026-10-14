# data_importer — Agent Guide

`AGENTS.md` is the only agent-instruction source in this directory. Do not
look for or create `CLAUDE.md` or `GEMINI.md` here.

## Rules

- Use the persistent `mosaic` service: `./mosaic.sh <command>`; do not run
  host Python, `uv`, or transient containers.
- Preserve delta imports and idempotency. ClickHouse reads require `FINAL`.
- Indian stock/ETF prices: Shoonya or NSE. ETF premium/discount: synchronous
  live AMC iNAV plus LTP, never static/EOD NAV.
- Do not calculate figures in prose; cite tool or SQL output. Do not commit
  unless the user explicitly asks.

## Navigate by task

| Task | Read |
| --- | --- |
| Fetcher/registry | [architecture](docs/architecture.md), [adding fetchers](docs/adding-fetchers.md) |
| Storage or delta sync | [ClickHouse](docs/clickhouse.md) |
| Prices/iNAV | [market rules](docs/market-rules.md) |
| AMC holdings | [AMC factory](docs/amc-factory.md) |
| Validation | [testing](docs/testing.md) |

## Commands

```bash
./mosaic.sh python -m data_importer.cli --category <category> --dry-run
./mosaic.sh python -m data_importer.amc_holdings.run <amc> --dry-run
./mosaic.sh python -m pytest src/data_importer/tests/
```
