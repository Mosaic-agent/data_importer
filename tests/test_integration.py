"""
tests/test_integration.py
─────────────────────────
Integration tests for the data_importer package.

These tests verify:
  1. All top-level modules import without error
  2. The Fetcher registry loads with the expected categories
  3. Core abstractions (Fetcher ABC, ClickHouseImporter) are importable
  4. Sub-packages (amc_holdings, dsp_holdings, backfillers, maintenance) load
  5. tool_fetchers load (LangChain-decorated functions resolve)

No live network calls or database connections are made — all external I/O is
either lazy (inside methods) or guarded by the existing fetcher design.
"""

import importlib
import sys
import types
import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _import(dotted: str):
    """Import a dotted module path and return it, or re-raise ImportError."""
    return importlib.import_module(dotted)


# ── 1. top-level package imports ─────────────────────────────────────────────

def test_package_root_importable():
    import data_importer  # noqa: F401


def test_base_fetcher_importable():
    mod = _import("data_importer.base_fetcher")
    assert hasattr(mod, "Fetcher"), "Fetcher ABC missing from base_fetcher"


def test_registry_importable():
    mod = _import("data_importer.registry")
    # Registry must expose at least the ETF and stock symbol lists
    assert hasattr(mod, "ETFS") or hasattr(mod, "get_symbols_for_categories"), \
        "Expected ETFS or get_symbols_for_categories in registry"


def test_clickhouse_importable():
    mod = _import("data_importer.clickhouse")
    assert hasattr(mod, "ClickHouseImporter")


def test_freshness_importable():
    mod = _import("data_importer.freshness")
    assert hasattr(mod, "check_and_auto_import_stale_categories") or \
           hasattr(mod, "get_latest_watermark"), \
           "freshness module missing expected symbols"


def test_source_preference_importable():
    _import("data_importer.source_preference")


def test_parallel_importer_importable():
    _import("data_importer.parallel_importer")


# ── 2. fetcher registry ───────────────────────────────────────────────────────

def test_fetcher_registry_loads():
    from data_importer.fetchers.adapters import get_registry
    registry = get_registry()
    assert isinstance(registry, dict), "get_registry() must return a dict"
    assert len(registry) >= 10, f"Expected ≥10 fetchers, got {len(registry)}"


def test_fetcher_registry_contains_core_categories():
    from data_importer.fetchers.adapters import get_registry
    registry = get_registry()
    expected = {"etfs", "stocks", "fii_dii", "mf_nav", "fx_rates"}
    missing = expected - set(registry.keys())
    assert not missing, f"Registry missing core categories: {missing}"


def test_fetcher_registry_entries_are_fetcher_instances():
    from data_importer.base_fetcher import Fetcher
    from data_importer.fetchers.adapters import get_registry
    registry = get_registry()
    for name, fetcher in registry.items():
        assert isinstance(fetcher, Fetcher), \
            f"Registry entry '{name}' is not a Fetcher instance"


# ── 3. individual fetcher modules ─────────────────────────────────────────────

FETCHER_MODULES = [
    "data_importer.fetchers.yfinance_fetcher",
    "data_importer.fetchers.mfapi_fetcher",
    "data_importer.fetchers.fii_dii_fetcher",
    "data_importer.fetchers.fx_rates_fetcher",
    "data_importer.fetchers.amfi_flows_fetcher",
    "data_importer.fetchers.nse_inav_fetcher",
    "data_importer.fetchers.cot_fetcher",
    "data_importer.fetchers.etf_aum_fetcher",
    "data_importer.fetchers.mf_holdings_fetcher",
    "data_importer.fetchers.indian_macro_fetcher",
    "data_importer.fetchers.worldbank_macro_fetcher",
    "data_importer.fetchers.imf_weo_fetcher",
    "data_importer.fetchers.imf_reserves_fetcher",
    "data_importer.fetchers.nse_index_fetcher",
    "data_importer.fetchers.nse_quote_fetcher",
]


@pytest.mark.parametrize("module_path", FETCHER_MODULES)
def test_fetcher_module_importable(module_path):
    _import(module_path)


# ── 4. sub-packages ───────────────────────────────────────────────────────────

SUB_PACKAGES = [
    "data_importer.amc_holdings",
    "data_importer.amc_holdings.base",
    "data_importer.amc_holdings.factory",
    "data_importer.dsp_holdings",
    "data_importer.backfillers",
    "data_importer.maintenance",
]


@pytest.mark.parametrize("module_path", SUB_PACKAGES)
def test_sub_package_importable(module_path):
    _import(module_path)


def test_amc_factory_has_create_importer():
    mod = _import("data_importer.amc_holdings.factory")
    assert hasattr(mod, "create_importer") or hasattr(mod, "REGISTRY"), \
        "amc_holdings.factory missing create_importer/REGISTRY"


def test_amc_base_has_base_fund_importer():
    mod = _import("data_importer.amc_holdings.base")
    assert hasattr(mod, "BaseFundImporter"), \
        "amc_holdings.base missing BaseFundImporter"


# ── 5. tool_fetchers ──────────────────────────────────────────────────────────

TOOL_FETCHER_MODULES = [
    "data_importer.tool_fetchers.yahoo_finance",
    "data_importer.tool_fetchers.news_search",
    "data_importer.tool_fetchers.earnings_scraper",
    "data_importer.tool_fetchers.nse_announcements",
    "data_importer.tool_fetchers.inav_fetcher",
    "data_importer.tool_fetchers.historic_inav",
    "data_importer.tool_fetchers.newsapi_search",
    "data_importer.tool_fetchers.comex_fetcher",
]


@pytest.mark.parametrize("module_path", TOOL_FETCHER_MODULES)
def test_tool_fetcher_importable(module_path):
    _import(module_path)


def test_yahoo_finance_has_fetch_function():
    mod = _import("data_importer.tool_fetchers.yahoo_finance")
    assert hasattr(mod, "fetch_yahoo_data"), \
        "tool_fetchers.yahoo_finance missing fetch_yahoo_data"


def test_news_search_has_get_stock_news():
    mod = _import("data_importer.tool_fetchers.news_search")
    assert hasattr(mod, "get_stock_news"), \
        "tool_fetchers.news_search missing get_stock_news"
