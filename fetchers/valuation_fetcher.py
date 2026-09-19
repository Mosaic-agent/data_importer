"""
src/importer/fetchers/valuation_fetcher.py
───────────────────────────────────────────
Fetch current valuation snapshot (PE, PB, margins, analyst consensus, etc.)
for a list of symbols.

market_cap / trailing_pe / price_to_book are resolved via the source-agnostic
fetch_company_fundamentals() layer (fundamentals_sources.py), which falls
back across Yahoo's fast_info and Screener.in when Yahoo's crumb-gated
.info endpoint 401s — see that module's docstring for the full rationale.
The remaining fields below (forward P/E, PEG, price/sales, debt/equity, ROE,
margins, revenue/earnings growth, free cash flow, beta, analyst target/
recommendation) have no free non-.info alternative anywhere in this
codebase, so they still come straight from yfinance .info and will read as
0/"" whenever that endpoint is blocked.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import yfinance as yf

from src.data_importer.tool_fetchers.fundamentals_sources import fetch_company_fundamentals

logger = logging.getLogger(__name__)

# Fields with no non-.info fallback source — read directly from yfinance
# .info only; these stay 0.0 whenever Yahoo's crumb handshake is blocked.
_INFO_ONLY_FLOAT_FIELDS = {
    "forward_pe":        "forwardPE",
    "peg_ratio":         "pegRatio",
    "price_to_sales":    "priceToSalesTrailing12Months",
    "debt_to_equity":    "debtToEquity",
    "return_on_equity":  "returnOnEquity",
    "profit_margin":     "profitMargins",
    "operating_margin":  "operatingMargins",
    "gross_margin":      "grossMargins",
    "revenue_growth":    "revenueGrowth",
    "earnings_growth":   "earningsGrowth",
    "free_cashflow":     "freeCashflow",
    "beta":              "beta",
    "target_price":      "targetMeanPrice",
}


def _exchange_for(yahoo_ticker: str) -> str:
    """Infer exchange from a Yahoo ticker suffix (.NS/.BO) — else assume US."""
    if yahoo_ticker.endswith(".NS"):
        return "NSE"
    if yahoo_ticker.endswith(".BO"):
        return "BSE"
    return "US"


def fetch_valuation(symbols: list[tuple[str, str]]) -> list[dict]:
    """
    Fetch today's valuation snapshot for each symbol.

    Parameters
    ----------
    symbols : list of (internal_name, yahoo_ticker)

    Returns
    -------
    list with one dict per symbol:
        symbol, snapshot_date, market_cap, trailing_pe, forward_pe, peg_ratio,
        price_to_book, price_to_sales, debt_to_equity, return_on_equity,
        profit_margin, operating_margin, gross_margin, revenue_growth,
        earnings_growth, free_cashflow, beta, target_price,
        recommendation, analyst_count
    """
    rows: list[dict] = []
    today = date.today()

    for internal, yahoo in symbols:
        exchange = _exchange_for(yahoo)
        clean_symbol = re.sub(r"\.(NS|BO)$", "", yahoo, flags=re.I).upper()

        try:
            fundamentals = fetch_company_fundamentals(clean_symbol, yahoo, exchange)
        except Exception as exc:
            logger.warning("fetch_company_fundamentals failed for %s: %s", yahoo, exc)
            fundamentals = {}
        sources_used = fundamentals.pop("_sources_used", [])

        try:
            info = yf.Ticker(yahoo).info or {}
        except Exception as exc:
            logger.warning("yfinance .info fetch failed for %s: %s", yahoo, exc)
            info = {}

        if not fundamentals.get("market_cap") and not info:
            logger.warning("No valuation data available for %s from any source (rate-limited or unavailable)", yahoo)
            continue

        row: dict = {"symbol": internal, "snapshot_date": today}
        row["market_cap"]    = fundamentals.get("market_cap", 0.0)
        row["trailing_pe"]   = fundamentals.get("pe_ratio", 0.0)
        row["price_to_book"] = fundamentals.get("pb_ratio", 0.0)

        for col, key in _INFO_ONLY_FLOAT_FIELDS.items():
            val = info.get(key)
            row[col] = float(val) if val is not None else 0.0

        row["recommendation"] = str(info.get("recommendationKey") or "")
        row["analyst_count"] = int(info.get("numberOfAnalystOpinions") or 0)

        rows.append(row)
        logger.info(
            "Fetched valuation snapshot for %s (market_cap/PE/PB via: %s)",
            yahoo, "+".join(sources_used) or "none",
        )

    return rows

