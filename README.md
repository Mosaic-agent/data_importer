# data_importer

Canonical data-ingestion package for the [Mosaic](https://github.com/Mosaic-agent/Mosaic-fund-agent) quantitative research platform. Consolidates all market-data fetchers, AMC/DSP fund importers, ML backfillers, and LangChain-wrapped agent tools into a single importable Python package.

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
│   └── importers/      # HDFC, DSP, Kotak, Nippon, ICICI, Quant, AMFI, Bajaj
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
```

## Supported data categories

| Category | Fetcher class | Source |
|---|---|---|
| `etfs` / `stocks` | `NSElibFetcher`, `YFinanceFetcher` | NSE direct / Yahoo Finance |
| `fii_dii` | `FIIDIIFetcher` | Sensibull API |
| `mf_nav` | `MFNavFetcher` | mfapi.in |
| `mf_holdings` | `MfHoldingsFetcher` | mfapi.in + BSE |
| `amfi_flows` | `AmfiCategoryFlowsFetcher` | AMFI portal |
| `fx_rates` | `FXRatesFetcher` | yfinance + ECB CSV |
| `cot` | `COTGoldFetcher` | CFTC public data |
| `cb_reserves` | `CbReservesFetcher` | WGC + World Bank |
| `etf_aum` | `EtfAumFetcher` | NSE |
| `world_bank` / `imf_weo` | `WorldBankMacroFetcher`, `IMFWEOFetcher` | World Bank / IMF APIs |
| `inav` | `NseEodFetcher` + AMC fetchers | NSE iNAV API + Zerodha/Mirae/Nippon/Motilal |

## Compat shims

The Mosaic main repo keeps `src/importer/` and the relevant `src/tools/*.py` files as thin `sys.modules` re-export shims so all existing callers (60+ files, 13 YAML playbook references) continue to work without any changes.

## License

[Apache 2.0](../LICENSE)

