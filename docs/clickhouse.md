# ClickHouse Storage & Delta Sync

All data resides in the `market_data` ClickHouse database.

## Idempotency & ReplacingMergeTree
To support safe delta updates and backfills without duplicates, all tables use `ReplacingMergeTree`:
- Primary/Sort keys identify rows uniquely (e.g., `(symbol, trade_date)` for prices, `(symbol, quarter_date)` for shareholding).
- Inserting identical keys overwrites older versions during partition merges.
- **Query Rule**: ALWAYS append `FINAL` when querying (e.g., `SELECT * FROM market_data.daily_prices FINAL`).

## Delta-Sync & Watermarks
Ingestion tracks progress in `market_data.import_watermarks`:
- Key: `(source, symbol, dataset)`.
- Value: `last_date` (Date) and `updated_at` (DateTime).

### Window Calculation
- First run: Fetches `lookback_days` of history.
- Incremental run: Fetches from `max(last_date - overlap_days, from_date)` to `to_date`.
- `overlap_days` (default 0–3 days): Accommodates weekend market holidays and late-arriving reporting revisions without creating duplicates.

## Core Table Catalog
| Table | Order Key | Purpose |
|:---|:---|:---|
| `daily_prices` | `(symbol, trade_date)` | EOD OHLCV for stocks, ETFs, commodities, indices |
| `nse_delivery` | `(symbol, trade_date)` | Daily security delivery volumes & percentages |
| `inav_snapshots` | `(symbol, snapshot_at)` | Intraday indicative NAV and premium/discounts |
| `fii_dii_flows` | `(flow_date)` | Daily institutional net cash flows |
| `fii_dii_fno_daily` | `(trade_date, client_type)` | Participant derivative long/short contracts |
| `bulk_block_deals` | `(deal_date, symbol, client_name)` | High-volume block and bulk transactions |
| `mf_nav` | `(symbol, nav_date)` | Daily scheme NAV from mfapi.in |
| `mf_holdings` | `(scheme_code, as_of_month, isin)` | Monthly AMC portfolio holdings |
| `stock_financial_statements` | `(symbol, statement_type, period_date)` | Multi-year quarterly and yearly P&L, balance sheets |
| `stock_insider_trades` | `(symbol, transaction_date, insider_name)` | SEBI SAST insider transactions |
| `stock_shareholding_pattern` | `(symbol, quarter_date)` | 11-quarter aggregate Promoter, FII, DII, Public % |
| `stock_shareholding_entities` | `(symbol, category, holder_name, quarter_date)` | Institutional/promoter entities holding >1% |
| `ipo_pipeline` | `(slug, stage)` | Mainboard & SME IPO pipeline details |
| `concall_schedules` | `(company_key, symbol)` | Upcoming earnings conference call calendar |

## Adding Tables
Declare DDL string `_DDL_<NAME>` in `clickhouse.py`, add to `_ensure_tables()` tuple, and provide typed `insert_<name>(rows)` bulk insert helper.
