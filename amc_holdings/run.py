"""
src/scripts/fund_imports/run.py
────────────────────────────
Unified CLI entry point for all fund importers.

Usage
─────
    python src/scripts/fund_imports/run.py icici [--dry-run] [--test]
    python src/scripts/fund_imports/run.py nippon [--dry-run] [--test] [--from-year 2020] [--full]
    python src/scripts/fund_imports/run.py icici-index [--dry-run] [--test]
    python src/scripts/fund_imports/run.py all [--dry-run]

Run from the project root:
    PYTHONPATH=. python src/scripts/fund_imports/run.py nippon --from-year 2024
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.append(os.getcwd())

from rich.console import Console

from src.data_importer.amc_holdings.factory import REGISTRY, create_importer

_KWARGS_NAMES = ("nippon", "icici", "dsp", "quant", "bajaj", "kotak", "hdfc")


def _unique_amc_names() -> list[str]:
    """REGISTRY maps several alias keys (canara/canara_robeco/canara-robeco, etc.)
    to the same importer class — pick one canonical name per class so 'all'
    doesn't re-scrape the same AMC 2-3x under different aliases."""
    seen: set[type] = set()
    names: list[str] = []
    for name, cls in REGISTRY.items():
        if cls not in seen:
            seen.add(cls)
            names.append(name)
    return names


def _importer_kwargs(name: str, args: argparse.Namespace) -> dict:
    if name in _KWARGS_NAMES:
        return {"from_year": args.from_year, "full_reimport": args.full}
    return {}


def _run_refresh(names: list[str], args: argparse.Namespace) -> None:
    """Two-phase refresh: discover which AMCs have new data, then import only those."""
    console = Console()
    console.rule("[bold cyan]Phase 1 — Discovering new holdings availability[/bold cyan]")

    pending: dict[str, int] = {}
    for name in names:
        importer = create_importer(name, **_importer_kwargs(name, args))
        try:
            sources = importer.discover_pending()
        except Exception as exc:
            console.print(f"[red]{name}: discovery failed — {exc}[/red]")
            continue
        if sources:
            console.print(f"[yellow]{name}: {len(sources)} new source(s) available[/yellow]")
            pending[name] = len(sources)
        else:
            console.print(f"[dim]{name}: up to date[/dim]")

    if not pending:
        console.print("[bold green]✓ All AMCs up to date — nothing to import.[/bold green]")
        return

    console.rule(f"[bold cyan]Phase 2 — Importing {len(pending)} AMC(s) with new data[/bold cyan]")
    for name in pending:
        importer = create_importer(name, **_importer_kwargs(name, args))
        importer.run(dry_run=False, test=args.test)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AMC fund holdings importer (factory pattern)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "importers:",
            "  icici        ICICI Prudential MF via Morningstar API (snapshot)",
            "  nippon       Nippon India AMC monthly XLS files (2017–present)",
            "  icici-index  ICICI Prudential index constituents (Azure Blob)",
            "  kotak        Kotak Mahindra AMC fund holdings",
            "  hdfc         HDFC Asset Management Company fund holdings",
            "  all          Run all importers in sequence",
        ]),
    )
    parser.add_argument(
        "name",
        choices=[*list(REGISTRY), "all"],
        metavar="name",
        help="Importer to run: " + ", ".join([*list(REGISTRY), "all"]),
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print counts; skip DB insert")
    parser.add_argument("--test", action="store_true",
                        help="Process first source only; implies --dry-run behaviour")
    parser.add_argument("--from-year", type=int, default=2020,
                        help="Earliest year to import (default: 2020)")
    parser.add_argument("--full", action="store_true",
                        help="Reimport all months, ignoring watermarks")
    parser.add_argument("--refresh", action="store_true",
                        help="Discover which AMCs have new data first (fast, no parsing), "
                             "then import only those — skips slow re-parses of up-to-date AMCs")
    args = parser.parse_args()

    names = _unique_amc_names() if args.name == "all" else [args.name]

    if args.refresh:
        _run_refresh(names, args)
        return

    for name in names:
        importer = create_importer(name, **_importer_kwargs(name, args))
        importer.run(dry_run=args.dry_run, test=args.test)


if __name__ == "__main__":
    main()
