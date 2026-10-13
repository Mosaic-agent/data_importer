# System Architecture & Adapter Engine

`data_importer` isolates data extraction, parsing, validation, and storage from downstream quant models and agent reasoning.

## Package Layout
```
data_importer/
├── fetchers/        # 32 Fetcher adapters implementing Fetcher ABC
│   ├── adapters.py  # Central registry mapping categories to Fetchers
│   └── *.py         # Dedicated source extractors (NSE, Shoonya, StockScans, etc.)
├── amc_holdings/    # Multi-AMC mutual fund holdings factory (17 AMCs)
├── tool_fetchers/   # LangChain/ReAct agent tools wrapping raw fetchers
├── base_fetcher.py  # Fetcher base class
├── clickhouse.py    # ClickHouseImporter bulk client & DDL definitions
├── registry.py      # Universe symbol definitions (STOCKS, ETFS, INDICES)
├── freshness.py     # Watermark audits & stale category detection
└── cli.py           # Unified CLI runner
```

## The Fetcher ABC Pattern
Every data category implements `Fetcher` (`base_fetcher.py`):
1. `fetch(from_date, to_date, source=None) -> list[dict]`:
   Pulls data from upstream APIs, handles pagination/rate limits, returns raw dictionaries.
2. `validate(rows: list[dict]) -> list[dict]`:
   Enforces schema typing, drops invalid timestamps, rejects NaN prices.
3. `insert(rows: list[dict], ch) -> int`:
   Routes rows to appropriate `ClickHouseImporter` insertion methods. Returns row count.
4. `max_date(rows: list[dict]) -> date`:
   Extracts highest date for watermark advancement.

## Adapter Registry
`fetchers/adapters.py` builds the registry dictionary:
- Maps category string (e.g. `etfs`, `stockscans_insider`) to instantiated `Fetcher`.
- Exposes `get_registry()` used by `cli.py`.
- Adding a category requires adding a mapping in `_build_registry()` and registering the name in `registry.py:ALL_CATEGORIES`.

## Execution Flow
```
CLI invocation (--category <cat>)
  ├── 1. Read watermark from market_data.import_watermarks
  ├── 2. Calculate fetch window: (last_date - overlap_days) to today
  ├── 3. Execute Fetcher.fetch() -> Fetcher.validate()
  ├── 4. Execute Fetcher.insert() into ClickHouse (ReplacingMergeTree)
  └── 5. Update watermark on success; fire EventBus notifications
```
