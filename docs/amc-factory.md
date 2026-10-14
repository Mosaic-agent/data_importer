# AMC Holdings Factory

Automates monthly mutual fund portfolio disclosure imports across 17 Indian Asset Management Companies into `market_data.mf_holdings`.

## Factory Architecture
```
amc_holdings/
├── base.py       # BaseFundImporter ABC
├── factory.py    # AMC mapping & create_importer() dispatcher
├── run.py        # CLI entry point
└── importers/    # 17 AMC implementation classes
```

## Supported AMCs (17)
- **DSP**: `dsp.py`
- **ICICI Prudential**: `icici_mf.py` & `icici_index.py`
- **Nippon India**: `nippon.py`
- **HDFC**: `hdfc.py`
- **Kotak Mahindra**: `kotak.py`
- **Bajaj Finserv**: `bajaj.py`
- **Quant & QSIF**: `quant.py` & `qsif.py`
- **Edelweiss**: `edelweiss.py`
- **Abakkus**: `abakkus.py`
- **Helios**: `helios.py`
- **Invesco**: `invesco.py`
- **Canara Robeco**: `canara_robeco.py`
- **Mirae Asset**: `mirae.py`
- **Axis**: `axis.py`
- **Motilal Oswal**: `motilal.py`
- **Altiva SIF**: `altiva_sif.py`

## The BaseFundImporter Contract
Each AMC importer subclasses `BaseFundImporter` (`amc_holdings/base.py`):
- `amc_name: str`: AMC identifier.
- `supported_schemes: dict`: Schemes and AMFI scheme codes.
- `download_portfolio(month: date) -> bytes | Path`: Retrieves Excel/XML/CSV disclosure from AMC portal or AMFI.
- `parse_portfolio(file_data, month: date) -> list[dict]`: Normalizes rows to canonical schema:
  - `scheme_code`, `fund_name`, `as_of_month`, `isin`, `security_name`, `asset_type`, `market_value_cr`, `pct_of_nav`.

## Ingestion Invariant
- Table: `market_data.mf_holdings FINAL`.
- Schema: `(scheme_code, as_of_month, isin)` uniqueness.
- Idempotent: Re-importing a month overwrites previous snapshots.
- After insert: Triggers Qdrant vectorization for semantic similarity search across fund books.

## Execution
```bash
# Start with a non-writing validation run.
./mosaic.sh python -m data_importer.amc_holdings.run dsp --dry-run

# Import one AMC's supported history or refresh every AMC.
./mosaic.sh python -m data_importer.amc_holdings.run dsp
./mosaic.sh python -m data_importer.amc_holdings.run all --refresh
```

`run.py` has no universal `--month` option. Use only flags exposed by its CLI
(`--from-year` for supported AMCs, `--full`, `--refresh`, `--test`), and keep
execution inside the persistent `mosaic` service.
