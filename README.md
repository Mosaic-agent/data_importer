# data_importer

Canonical data-ingestion package for the main project: [Mosaic Fund Agent](https://github.com/Mosaic-agent/Mosaic-fund-agent). Consolidates all market-data fetchers, AMC/DSP fund importers, ML backfillers, and LangChain-wrapped agent tools into a single importable Python package.

> **Main Project:** This repository serves as the core data engine for the main quantitative research and portfolio platform at [https://github.com/Mosaic-agent/Mosaic-fund-agent](https://github.com/Mosaic-agent/Mosaic-fund-agent).

## Package layout

```
data_importer/
├── fetchers/           # 32 Fetcher classes — formal adapter pattern
│   ├── adapters.py     # Central registry (NSE, Yahoo, MF NAV, FII/DII, FX, COT, ETF AUM, macro …)
│   └── *.py            # One file per data source
│
├── tool_fetchers/      # 11 LangChain-wrapped agent tools
│   ├── yahoo_finance.py
│   ├── news_search.py
│   ├── earnings_scraper.py
│   ├── shoonya_tools.py
│   └── …
│
├── amc_holdings/       # AMC mutual-fund holdings importers
│   ├── base.py         # BaseFundImporter ABC
│   ├── factory.py      # create_importer() dispatcher
│   ├── run.py          # CLI entry point
│   └── importers/      # 17 AMCs: HDFC, DSP, Kotak, Nippon, ICICI, Quant, QSIF, Bajaj, Edelweiss, Abakkus, Helios, Invesco, Canara, Mirae, Axis, Motilal, Altiva SIF
│
├── dsp_holdings/       # DSP Morningstar ZIP importers (backfill + latest)
│
├── backfillers/        # ML prediction & iNAV snapshot backfill scripts
│
├── maintenance/        # DB utilities (fix_bad_data, audit_freshness, backup/restore)
│
├── base_fetcher.py     # Fetcher ABC — fetch() / validate() / insert() / max_date()
├── registry.py         # Symbol registry (ETFs, stocks, indices, FX pairs)
├── clickhouse.py       # ClickHouseImporter — bulk insert client
├── freshness.py        # Watermark checks & auto-import triggers
├── parallel_importer.py
├── source_preference.py
└── cli.py              # `python -m data_importer` CLI dispatcher
```

## Quick start

```bash
# Install into any project
pip install git+https://github.com/Mosaic-agent/data_importer.git

# Or use as a submodule (Mosaic default)
git submodule add https://github.com/Mosaic-agent/data_importer src/data_importer
```

```python
# Fetch ETF OHLCV via the registry
from data_importer.fetchers.adapters import get_registry

registry = get_registry()
fetcher  = registry["etfs"]
rows     = fetcher.fetch(from_date, to_date)

# Batch import via CLI
python -m data_importer.cli --category etfs,stocks,fii_dii
python -m data_importer.cli --category stockscans_shareholding,stockscans_insider
python -m data_importer.cli --category dsp,icici,quant,hdfc
```

## Supported data categories

Data ingestion is split into registry-driven fetchers, special-cased market streams, and the AMC Holdings Factory. All data lands in ClickHouse `market_data.*` (ReplacingMergeTree for idempotent updates).

### 1. Secondary Market Feeds (Equities, ETFs, Indices & FX)

| Category | Fetcher class | Source | Target ClickHouse Table | Scope / Description |
|:---|:---|:---|:---|:---|
| `etfs` | `ShoonyaFetcher` | Shoonya → nselib → Yahoo Finance | `market_data.daily_prices` | Daily OHLCV for 23 tracked Indian ETFs |
| `stocks` | `StocksFetcher` | Shoonya → nselib → Yahoo Finance | `market_data.daily_prices` | Daily OHLCV for 63 tracked Indian equities |
| `us_stocks` | `StocksFetcher` | Yahoo Finance | `market_data.daily_prices` | Daily OHLCV for US macro watchlist |
| `commodities` | `YFinanceFetcher` | Yahoo Finance | `market_data.daily_prices` | Daily OHLCV for Gold, Silver, Crude, Copper |
| `indices` | `YFinanceFetcher` | Yahoo Finance | `market_data.daily_prices` | Nifty 50, Bank Nifty, Sensex & global benchmark indices |
| `nse_indices` | `NseIndexFetcher` | NSE direct via nselib | `market_data.daily_prices` | Daily OHLCV for 32 NSE thematic & sector indices |
| `nse_eod` | `NseEodFetcher` | NSE direct via nselib | `market_data.daily_prices` | Whole-universe EOD bhavcopy (ETFs + stocks) |
| `nse_delivery` | `NseDeliveryFetcher` | NSE bhavcopy delivery position | `market_data.nse_delivery` | Daily security-wise delivery volume and % |
| `fx_rates` | `FXRatesFetcher` | Yahoo Finance + ECB CSV | `market_data.fx_rates` | Daily USD/INR, EUR/INR, GBP/INR, JPY/INR currency pairs |
| `inav` | `NseInavFetcher` + AMC fetchers | Live AMC feeds + NSE API | `market_data.inav_snapshots` | Real-time indicative NAV, market price & premium/discount |

### 2. Institutional Flows, Deals & Positioning (Whale Intelligence)

| Category | Fetcher class | Source | Target ClickHouse Table | Scope / Description |
|:---|:---|:---|:---|:---|
| `fii_dii` | `FIIDIIFetcher` | Sensibull / NSE direct | `market_data.fii_dii_flows`<br>`market_data.fii_dii_fno_daily`<br>`market_data.fii_dii_monthly` | Daily FII/DII net cash equity & derivative F&O positions |
| `bulk_deals` / `events` | `BulkBlockDealsFetcher` | NSE bulk & block deals API | `market_data.bulk_block_deals` | Large institutional and promoter block transactions |
| `amfi_flows` | `AmfiCategoryFlowsFetcher` | AMFI portal | `market_data.amfi_category_flows` | Category-wise monthly mutual fund net inflows, outflows & AUM |
| `cot` | `COTGoldFetcher` | CFTC weekly commitments report | `market_data.cot_gold` | Managed money net long/short positioning for COMEX gold |
| `cb_reserves` | `CbReservesFetcher` | IMF IFS / World Gold Council | `market_data.cb_gold_reserves` | Global official central bank gold reserve holdings (tonnes) |
| `etf_aum` | `EtfAumFetcher` | Yahoo Finance | `market_data.etf_aum` | Daily AUM snapshots for GLD, IAU, SGOL, PHYS |

### 3. StockScans Indian Equity Intelligence & Private APIs

| Category | Fetcher class | Source | Target ClickHouse Table | Scope / Description |
|:---|:---|:---|:---|:---|
| `stockscans` | `StockScansFetcher` | StockScans REST API | `market_data.stock_valuation`<br>`market_data.stock_financial_statements`<br>`market_data.ipo_pipeline`<br>`market_data.concall_schedules` | Full suite: valuation snapshots, multi-year statements, IPOs & concalls |
| `stockscans_fundamentals` | `StockScansFetcher` | StockScans REST API | `market_data.stock_valuation`<br>`market_data.stock_financial_statements` | Multi-year quarterly & annual P&L, balance sheets, cash flows & ratios |
| `stockscans_insider` | `StockScansFetcher` | StockScans Private API (authtoken) | `market_data.stock_insider_trades` | SEBI SAST promoter, director & KMP insider acquisitions & disposals |
| `stockscans_shareholding`<br>`shareholding_pattern`<br>`shareholding` | `StockScansFetcher` | StockScans Private API (authtoken) | `market_data.stock_shareholding_pattern`<br>`market_data.stock_shareholding_entities` | 11-quarter aggregate Promoter, FII, DII, Govt, Public % and shareholder count, plus entity disclosures (>1%) |
| `stockscans_ipo` / `ipo` | `StockScansFetcher` | StockScans REST API | `market_data.ipo_pipeline` | Indian Mainboard & SME IPO pipeline, price bands, issue size & subscription status |
| `stockscans_concalls` / `concalls` | `StockScansFetcher` | StockScans REST API | `market_data.concall_schedules` | Scheduled today and upcoming 30-day earnings conference calls |

### 4. Global Macro & Economic Indicators

| Category | Fetcher class | Source | Target ClickHouse Table | Scope / Description |
|:---|:---|:---|:---|:---|
| `indian_macro` | `IndianMacroFetcher` | RBI & MOSPI public series | `market_data.indian_macro_indicators` | Indian CPI, Repo Rate, IIP, and quarterly GDP growth |
| `world_bank` | `WorldBankMacroFetcher` | World Bank WDI API | `market_data.macro_indicators` | Annual macroeconomic indicators (GDP, inflation, external debt) for India & G4 peers |
| `imf_weo` | `IMFWEOFetcher` | IMF WEO API | `market_data.macro_indicators` | Multi-year IMF GDP projections and macro forecasts |

### 5. Mutual Fund Disclosures & AMC Factory (17 AMCs)

| Category | Fetcher class | Source | Target ClickHouse Table | Scope / Description |
|:---|:---|:---|:---|:---|
| `mf` | `MFNavFetcher` | MFAPI.in | `market_data.mf_nav` | Daily historical and latest NAV for mutual fund schemes |
| `mf_holdings` | `MfHoldingsFetcher` | Morningstar / BSE | `market_data.mf_holdings`<br>`market_data.mf_holding_summaries` | Monthly fund portfolio holdings (auto-vectorized into Qdrant) |
| `dsp`, `icici`, `icici-index`, `nippon`, `hdfc`, `kotak`, `bajaj`, `quant`, `qsif`, `edelweiss`, `abakkus`, `helios`, `invesco`, `canara`, `mirae`, `axis`, `motilal`, `altiva` | AMC Fund Importers (`amc_holdings/`) | Direct AMC monthly disclosures (Excel / XML / API) | `market_data.mf_holdings` | Comprehensive monthly equity & debt portfolio holdings across 17 major Indian asset managers |

### 6. US Watchlist Disclosures

| Category | Fetcher class | Source | Target ClickHouse Table | Scope / Description |
|:---|:---|:---|:---|:---|
| `earnings` | `EarningsFetcher` | Yahoo Finance | `market_data.stock_earnings` | US quarterly earnings history and surprise % |
| `insider` | `InsiderFetcher` | Yahoo Finance | `market_data.stock_insider_trades` | US executive and director insider transactions |
| `valuation` | `ValuationFetcher` | Yahoo Finance | `market_data.stock_valuation` | US valuation metrics (PE, PB, ROE, margins) |

## Compat shims

The Mosaic main project repo ([Mosaic Fund Agent](https://github.com/Mosaic-agent/Mosaic-fund-agent)) keeps `src/importer/` and the relevant `src/tools/*.py` files as thin `sys.modules` re-export shims so all existing callers (60+ files, 13 YAML playbook references) continue to work without any changes.

## License

[Apache 2.0](../LICENSE)

