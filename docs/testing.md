# Verification & Testing

Guidelines for testing, validating, and auditing data ingestion pipelines.

## Standalone Pytest Suite
Run the test suite in `src/data_importer`:
```bash
pytest src/data_importer/tests/
```
Target specific importer tests:
```bash
pytest src/data_importer/tests/test_dsp_importer.py
pytest src/data_importer/tests/test_hdfc_importer.py
pytest src/data_importer/tests/test_integration.py
```

## Python Syntax & Compilation Check
Before committing changes, verify syntax:
```bash
python -m py_compile src/data_importer/clickhouse.py \
                      src/data_importer/fetchers/adapters.py \
                      src/data_importer/registry.py \
                      src/data_importer/cli.py
```

## Dry-Run Execution
Always test newly modified fetchers using `--dry-run`:
- Validates network connection, auth tokens, rate limits, and schema parsing without modifying database records.
```bash
python -m data_importer.cli --category <category> --dry-run
```

## ClickHouse Data Sanity Checks
Run the automated sanity validator:
```bash
python src/data_importer/maintenance/run_data_sanity_check.py
```
Validates:
1. **YoY Outliers**: Detects impossible YoY returns in safe assets (>40%).
2. **Daily Outliers**: Flags anomalous daily moves (>7% price shock).
3. **Missing Partitions**: Checks for gaps in historical date sequences.
4. **Duplicate Detection**: Asserts that `ReplacingMergeTree` deduplicates properly when queried with `FINAL`.

## Database Freshness Audit
Check table watermarks and detect stale categories:
```bash
python src/data_importer/maintenance/audit_freshness.py
```
