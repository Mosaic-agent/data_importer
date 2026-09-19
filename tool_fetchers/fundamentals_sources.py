"""
src/data_importer/tool_fetchers/fundamentals_sources.py
─────────────────────────────────────────────────────────
Source-agnostic company fundamentals layer.

Strategy pattern (sibling to `SignalSource` in src/agents/signal_sources.py
and `Fetcher` in src/data_importer/base_fetcher.py): each `FundamentalsSource`
subclass wraps exactly one upstream provider and returns only the fields it
managed to populate — never a placeholder zero/"" for something it has no
opinion on. `fetch_company_fundamentals()` tries sources in priority order
and merges field-by-field, so callers always get the union of whatever is
reachable right now instead of an all-or-nothing result tied to a single
provider's uptime.

Why this exists: Yahoo Finance's `.info` (quoteSummary) is gated behind a
crumb/cookie handshake that Yahoo's own anti-bot WAF frequently rejects
(401 "Invalid Crumb" — a widespread, well-documented Yahoo-side issue, not
fixable by retrying or upgrading yfinance). When that happens:
  • price / market cap / 52-week range are still recoverable via Yahoo's
    `fast_info` (chart API — not crumb-gated).
  • sector / industry / P-E / P-B / dividend yield / description are still
    recoverable by scraping Screener.in (already used for earnings in
    earnings_scraper.py).
Adding a new source later (e.g. NSE quote-equity, a paid data vendor) only
requires a new `FundamentalsSource` subclass appended to `_DEFAULT_SOURCES`
— no changes needed anywhere that calls `fetch_company_fundamentals()`.
"""

from __future__ import annotations

import abc
import logging
import re
import time
from typing import Any

import requests
import yfinance as yf
from bs4 import BeautifulSoup

from config.settings import settings
from src.data_importer.tool_fetchers.earnings_scraper import HEADERS as _SCREENER_HEADERS
from src.data_importer.tool_fetchers.earnings_scraper import SCREENER_BASE

logger = logging.getLogger(__name__)

# Fields a FundamentalsSource may populate (a partial subset is fine).
ALL_FIELDS = (
    "sector",
    "industry",
    "market_cap",
    "pe_ratio",
    "pb_ratio",
    "dividend_yield",
    "fifty_two_week_high",
    "fifty_two_week_low",
    "current_price",
    "description",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely coerce a value to float, returning default on failure."""
    try:
        if value is None:
            return default
        f = float(value)
        return f if not (f != f) else default  # NaN guard
    except (TypeError, ValueError):
        return default


class FundamentalsSource(abc.ABC):
    """One upstream provider of company fundamentals."""

    name: str = "unknown"

    @abc.abstractmethod
    def fetch(self, symbol: str, yf_symbol: str, exchange: str) -> dict[str, Any]:
        """
        Return only the fields this source could actually resolve.
        Omit (don't zero-fill) any field it has no data for, so the
        composite merge below can tell "this source has no opinion" apart
        from "this field is genuinely zero".
        """
        raise NotImplementedError


class YahooInfoSource(FundamentalsSource):
    """
    Yahoo's quoteSummary `.info` — the richest single source when it's
    reachable (covers every field), but gated behind a crumb/cookie
    handshake that Yahoo's WAF frequently rejects with a 401.
    """

    name = "yahoo_info"

    def fetch(self, symbol: str, yf_symbol: str, exchange: str) -> dict[str, Any]:
        try:
            info = yf.Ticker(yf_symbol).info or {}
        except Exception as exc:
            logger.debug("%s: fetch failed for %s: %s", self.name, yf_symbol, exc)
            return {}

        if not info.get("sector") and not info.get("industry") and not info.get("marketCap"):
            # Empty across the board almost always means the crumb handshake
            # was rejected, not that this stock has none of these fields.
            logger.debug("%s: .info came back empty for %s (likely crumb/auth failure)", self.name, yf_symbol)
            return {}

        out: dict[str, Any] = {}
        if info.get("sector"):
            out["sector"] = info["sector"]
        if info.get("industry"):
            out["industry"] = info["industry"]
        if _safe_float(info.get("marketCap")):
            out["market_cap"] = _safe_float(info.get("marketCap"))
        if _safe_float(info.get("trailingPE")):
            out["pe_ratio"] = _safe_float(info.get("trailingPE"))
        if _safe_float(info.get("priceToBook")):
            out["pb_ratio"] = _safe_float(info.get("priceToBook"))
        # yfinance now returns dividendYield already as a percent (e.g. 5.52, not 0.0552)
        if _safe_float(info.get("dividendYield")):
            out["dividend_yield"] = _safe_float(info.get("dividendYield"))
        if _safe_float(info.get("fiftyTwoWeekHigh")):
            out["fifty_two_week_high"] = _safe_float(info.get("fiftyTwoWeekHigh"))
        if _safe_float(info.get("fiftyTwoWeekLow")):
            out["fifty_two_week_low"] = _safe_float(info.get("fiftyTwoWeekLow"))
        price = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
        if price:
            out["current_price"] = price
        if info.get("longBusinessSummary"):
            out["description"] = info["longBusinessSummary"]
        return out


class YahooFastInfoSource(FundamentalsSource):
    """
    Yahoo's `fast_info` (chart API) — not crumb-gated, so it keeps working
    even when `.info` is blocked. Only covers price / market cap / 52-week
    range; sector, industry, P/E, P/B, dividend yield, and the business
    description all live exclusively behind `.info`.
    """

    name = "yahoo_fast_info"

    def fetch(self, symbol: str, yf_symbol: str, exchange: str) -> dict[str, Any]:
        try:
            fi = yf.Ticker(yf_symbol).fast_info
        except Exception as exc:
            logger.debug("%s: fetch failed for %s: %s", self.name, yf_symbol, exc)
            return {}

        out: dict[str, Any] = {}
        price = _safe_float(fi.get("lastPrice"))
        if price:
            out["current_price"] = price
        mc = _safe_float(fi.get("marketCap"))
        if mc:
            out["market_cap"] = mc
        high = _safe_float(fi.get("yearHigh"))
        if high:
            out["fifty_two_week_high"] = high
        low = _safe_float(fi.get("yearLow"))
        if low:
            out["fifty_two_week_low"] = low
        return out


class ScreenerSource(FundamentalsSource):
    """
    Screener.in — free NSE/BSE fundamentals site already used for quarterly
    earnings in earnings_scraper.py. Covers sector/industry (via its
    category breadcrumb), P/E, P/B (derived from Book Value + price),
    dividend yield, and a business description ("About" section). Only
    meaningful for Indian (NSE/BSE) symbols — a no-op for US tickers.
    """

    name = "screener"

    def fetch(self, symbol: str, yf_symbol: str, exchange: str) -> dict[str, Any]:
        is_indian = exchange.upper() in ("NSE", "BSE") or yf_symbol.endswith((".NS", ".BO"))
        if not is_indian:
            return {}

        clean_symbol = re.sub(r"\.(NS|BO)$", "", yf_symbol).upper()
        time.sleep(settings.scrape_delay_seconds)

        soup = self._fetch_page(clean_symbol)
        if soup is None:
            return {}

        ratios: dict[str, float] = {}
        for li in soup.select("#top-ratios li"):
            name_el = li.select_one(".name")
            num_el = li.select_one(".value .number")
            if name_el and num_el:
                ratios[name_el.get_text(strip=True)] = _safe_float(
                    num_el.get_text(strip=True).replace(",", "")
                )

        out: dict[str, Any] = {}
        if ratios.get("Stock P/E"):
            out["pe_ratio"] = ratios["Stock P/E"]
        if ratios.get("Dividend Yield"):
            out["dividend_yield"] = ratios["Dividend Yield"]
        if ratios.get("Market Cap"):
            out["market_cap"] = ratios["Market Cap"] * 1e7  # Screener reports in ₹ Crore
        if ratios.get("Current Price"):
            out["current_price"] = ratios["Current Price"]
        # Screener doesn't show P/B directly — derive it from Book Value + price
        book_value = ratios.get("Book Value")
        price = ratios.get("Current Price")
        if book_value and price:
            out["pb_ratio"] = round(price / book_value, 2)

        # Category breadcrumb, most-general → most-specific, e.g.
        # Energy > Oil, Gas & Consumable Fuels > Petroleum Products > ...
        breadcrumb = [
            a.get_text(strip=True)
            for a in soup.find_all("a", href=lambda h: h and "/market/IN" in h)
        ]
        if breadcrumb:
            out["sector"] = breadcrumb[0]
        if len(breadcrumb) > 1:
            out["industry"] = breadcrumb[1]

        about = soup.select_one(".company-profile .about p")
        if about and about.get_text(strip=True):
            out["description"] = about.get_text(strip=True)

        return out

    @staticmethod
    def _fetch_page(clean_symbol: str) -> Any:
        """Try the consolidated page first, fall back to the standalone page
        (same fallback pattern as fetch_from_screener() in earnings_scraper.py)."""
        for path in (f"/company/{clean_symbol}/consolidated/", f"/company/{clean_symbol}/"):
            try:
                r = requests.get(f"{SCREENER_BASE}{path}", headers=_SCREENER_HEADERS, timeout=15)
            except Exception as exc:
                logger.debug("%s: request failed for %s: %s", "screener", clean_symbol, exc)
                continue
            if r.status_code == 200 and r.text:
                return BeautifulSoup(r.text, "html.parser")
        return None


# Priority order: cheapest/richest-first. `fast_info` piggybacks on the same
# yfinance session at no extra network cost, so it's tried before Screener;
# Screener is last since it's an extra hop to a third-party site and should
# only be hit when Yahoo genuinely can't answer.
_DEFAULT_SOURCES: list[FundamentalsSource] = [
    YahooInfoSource(),
    YahooFastInfoSource(),
    ScreenerSource(),
]


def fetch_company_fundamentals(
    symbol: str,
    yf_symbol: str,
    exchange: str = "NSE",
    sources: list[FundamentalsSource] | None = None,
) -> dict[str, Any]:
    """
    Merge company fundamentals across sources, filling gaps in priority
    order until every field in ALL_FIELDS is resolved or every source has
    been tried.

    Returns a dict with whatever subset of ALL_FIELDS could be resolved,
    plus "_sources_used" (list of source names that contributed at least
    one field) — surfaced as `data_source` on YahooFinanceData for
    provenance (see src/models/portfolio.py).
    """
    merged: dict[str, Any] = {}
    sources_used: list[str] = []

    for source in sources or _DEFAULT_SOURCES:
        missing = [f for f in ALL_FIELDS if f not in merged]
        if not missing:
            break  # every field already resolved — no need to hit more sources
        try:
            partial = source.fetch(symbol, yf_symbol, exchange)
        except Exception as exc:
            logger.warning("%s: unexpected error for %s: %s", source.name, yf_symbol, exc)
            continue
        if not partial:
            continue
        sources_used.append(source.name)
        for field in missing:
            if partial.get(field):
                merged[field] = partial[field]

    merged["_sources_used"] = sources_used
    return merged
