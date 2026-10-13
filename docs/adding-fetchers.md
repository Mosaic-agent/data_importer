# Adding a New Data Source

Playbook for implementing and integrating a new data category into `data_importer`.

## Step 1: Create the Fetcher Adapter
Create `src/data_importer/fetchers/<name>_fetcher.py`:
```python
from datetime import date
from typing import Any
from src.data_importer.base_fetcher import Fetcher

class CustomFetcher(Fetcher):
    source_name = "custom_api"
    symbol_key = "CUSTOM"
    overlap_days = 1

    def fetch(self, from_date: date, to_date: date, *, source: str | None = None) -> list[dict[str, Any]]:
        # 1. HTTP fetch, handle pagination & rate limits
        return [...]

    def validate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # 2. Filter invalid timestamps, assert required keys
        return [r for r in rows if r.get("date")]

    def insert(self, rows: list[dict[str, Any]], ch) -> int:
        # 3. Call ClickHouseImporter insert method
        return ch.insert_custom_data(rows)

    def max_date(self, rows: list[dict[str, Any]]) -> date:
        # 4. Return watermark max date
        return max(r["date"] for r in rows)
```

## Step 2: Define Schema in ClickHouse
In `src/data_importer/clickhouse.py`:
1. Add DDL string:
```python
_DDL_CUSTOM_DATA = """
CREATE TABLE IF NOT EXISTS market_data.custom_data (
    symbol       String,
    record_date  Date,
    value        Float64,
    imported_at  DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(imported_at)
ORDER BY (symbol, record_date)
"""
```
2. Add `_DDL_CUSTOM_DATA` to `_ensure_tables()` tuple.
3. Add bulk insert method `insert_custom_data(self, rows: list[dict]) -> int`.

## Step 3: Register in Category Registry
1. In `src/data_importer/fetchers/adapters.py`:
   Add to `_build_registry()`:
   ```python
   registry["custom_data"] = CustomFetcher()
   ```
2. In `src/data_importer/registry.py`:
   Append category name `"custom_data"` to `ALL_CATEGORIES`.

## Verification
```bash
# Test single category with dry-run
python -m data_importer.cli --category custom_data --dry-run
# Test live insert
python -m data_importer.cli --category custom_data
```
