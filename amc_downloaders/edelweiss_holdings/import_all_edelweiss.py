"""
src/data_importer/amc_downloaders/edelweiss_holdings/import_all_edelweiss.py
──────────────────────────────────────────────────────────────────────────
CLI entry-point for importing Edelweiss Mutual Fund & SIF portfolio holdings into ClickHouse.

Usage:
    python -m src.data_importer.amc_downloaders.edelweiss_holdings.import_all_edelweiss [--file path/to/portfolio.xlsx] [--dry-run]
    python src/scripts/edelweiss/import_all_edelweiss.py [--file path/to/portfolio.xlsx] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

from src.data_importer.amc_holdings.importers.edelweiss import EdelweissImporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Edelweiss Mutual Fund & SIF portfolio disclosures (.xlsx/.xls) into ClickHouse"
    )
    parser.add_argument("--file", type=str, default=None, help="Path to statutory Excel workbook (.xlsx / .xls)")
    parser.add_argument("--dry-run", action="store_true", help="Parse without inserting into ClickHouse")
    parser.add_argument("--test", action="store_true", help="Process only the first source for testing")
    parser.add_argument("--full-reimport", action="store_true", help="Re-import all discovered historical months")
    parser.add_argument("--from-year", type=int, default=2023, help="Historical discovery start year (default: 2023)")
    parser.add_argument("--month", type=str, default=None, help="Target month in YYYY-MM format")
    args = parser.parse_args()

    target_month = None
    if args.month:
        try:
            target_month = datetime.strptime(args.month, "%Y-%m").date()
        except ValueError:
            parser.error(f"Invalid --month format: '{args.month}'. Expected YYYY-MM (e.g. 2026-05)")

    importer = EdelweissImporter(
        from_year=args.from_year,
        full_reimport=args.full_reimport,
        target_month=target_month,
        excel_file=args.file,
    )

    importer.run(dry_run=args.dry_run, test=args.test)


if __name__ == "__main__":
    main()
