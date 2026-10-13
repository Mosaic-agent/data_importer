# data_importer — Operational Reference & Index

Canonical data-ingestion engine for [Mosaic Fund Agent](https://github.com/Mosaic-agent/Mosaic-fund-agent). Consolidates all market-data fetchers, AMC fund scrapers, ML backfillers, and ClickHouse ingestion into a standalone package.

## Core Invariants (Non-Negotiable)
1. **No LLM Math**: Never calculate numbers in reasoning. Citing SQL/tool outputs verbatim is mandatory.
2. **Commit Style**: Clean single-author conventional commits. Never add `Co-Authored-By:` trailers. Never commit without user prompt.
3. **Idempotent Storage**: All ClickHouse tables are `ReplacingMergeTree`. Queries must include `FINAL`. Re-imports must never produce duplicates.
4. **Price Authority**: Indian equity/ETF prices must use Shoonya or NSE. Never use Yahoo Finance or static NAV for secondary Indian ETF prices.
5. **iNAV Precedence**: Use live AMC iNAV for ETF premium/discount. Never use lagged/static NSE EOD declared NAV.

## Supported Sources & AMCs
- **17 AMCs**: DSP, ICICI (MF & Index), Nippon, HDFC, Kotak, Bajaj, Quant, QSIF, Edelweiss, Abakkus, Helios, Invesco, Canara Robeco, Mirae, Axis, Motilal, Altiva SIF ([docs/amc-factory.md](docs/amc-factory.md)).
- **Upstream Sources**: Shoonya (Finvasia), NSE (Direct/Bhavcopy/iNAV), StockScans (REST + Private JWT), Sensibull, MFAPI.in, Morningstar, CFTC, IMF, World Bank, RBI/MOSPI, Yahoo Finance ([docs/clickhouse.md](docs/clickhouse.md)).
- **Dynamic Discovery**:
  - `python -c "from data_importer.fetchers.adapters import get_registry; print(list(get_registry()))"`
  - `python -c "from data_importer.amc_holdings.factory import REGISTRY; print(list(REGISTRY))"`

## CLI Quick Reference
```bash
python -m data_importer.cli --category <cat> [--lookback <days>] [--dry-run]
python -m data_importer.cli --category stockscans_shareholding,stockscans_insider
python -m data_importer.amc_holdings.run <amc> [--month YYYY-MM] [--dry-run]
pytest tests/
```

## Modular Index & Sub-Category Guides

Read targeted sub-guides based on task requirements:

- **[System Architecture](docs/architecture.md)**
  Package layout, `Fetcher` ABC contract (`fetch`, `validate`, `insert`, `max_date`), and registry dispatch (`adapters.py`).
- **[ClickHouse Storage & Delta Sync](docs/clickhouse.md)**
  ClickHouse table catalog, ReplacingMergeTree engines, and the `import_watermarks` delta-sync protocol.
- **[Market Data & Pricing Rules](docs/market-rules.md)**
  Primary vs fallback feeds, live AMC iNAV vs secondary LTP, and promoter dilution vs sale verification.
- **[Adding a New Fetcher](docs/adding-fetchers.md)**
  Standard 3-step playbook for implementing, wiring, and registering a new data category.
- **[AMC Holdings Factory](docs/amc-factory.md)**
  The `BaseFundImporter` lifecycle, scrapers across 17 asset managers, and monthly holding ingestion.
- **[Verification & Testing](docs/testing.md)**
  Running unit tests, py_compile checks, ClickHouse sanity validators, and dry-run assertions.
