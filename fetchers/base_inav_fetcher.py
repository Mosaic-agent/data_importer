"""
src/importer/fetchers/base_inav_fetcher.py
───────────────────────────────────────────
Template Method base class for AMC iNAV fetchers.

All four AMC fetchers (Nippon, Zerodha, Mirae, Motilal) share the same
pipeline structure:

    1. Normalise requested symbols against the fetcher's known set
    2. HTTP call to the AMC API  →  raw response
    3. Parse raw response  →  {nse_symbol: api_item} map
    4. Batch-fetch market prices from yfinance
    5. Build output rows (inav, market_price, premium_discount_pct, source)

Steps 1, 4, and 5 are identical across fetchers and live here.
Steps 2 and 3 differ per AMC and are implemented by concrete subclasses.

Public API (unchanged from before):
    fetch_inav_nippon / fetch_inav_mirae / fetch_inav_zerodha / fetch_inav_motilal
    — each is a thin module-level wrapper calling the corresponding class.

Adding a new AMC fetcher:
    1. Subclass BaseInavFetcher
    2. Implement the 4 abstract methods
    3. Export a module-level function that calls instance.fetch_inav()
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

logger = logging.getLogger(__name__)

_COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _safe(val: Any, default: float = 0.0) -> float:
    """Parse a possibly-stringified number, stripping commas and percent signs."""
    try:
        return float(str(val).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return default


def _fetch_market_prices(symbols: list[str]) -> dict[str, float]:
    """
    Fetch the most recent secondary market price for each NSE ETF/stock.
    Rule: ALWAYS use Shoonya or NSE first; use ClickHouse daily_prices as verified DB source;
    yfinance 5d as last resort. Never substitute declared NAV as market price.
    """
    if not symbols:
        return {}

    prices: dict[str, float] = {}

    # 1. Try Shoonya REST API live quote
    try:
        from src.data_importer.fetchers.shoonya_fetcher import get_shoonya_api
        from src.data_importer.tool_fetchers.shoonya_tools import _resolve_token
        api = get_shoonya_api()
        if api:
            for sym in symbols:
                res = _resolve_token(api, sym)
                if res:
                    token, _ = res
                    quote = api.get_quotes(exchange="NSE", token=token)
                    if quote and quote.get("stat") == "Ok":
                        lp = _safe(quote.get("lp"))
                        if lp > 0:
                            prices[sym] = lp
    except Exception as exc:
        logger.debug("Shoonya price fetch failed: %s", exc)

    # 2. Try NSE Quote API for remaining symbols
    remaining = [s for s in symbols if s not in prices]
    if remaining:
        try:
            from src.data_importer.fetchers.nse_quote_fetcher import _NSE_HEADERS, _NSE_QUOTE_URL, _NSE_WARMUP, _TIMEOUT
            import httpx
            with httpx.Client(headers=_NSE_HEADERS, follow_redirects=True, timeout=_TIMEOUT) as client:
                try:
                    client.get(_NSE_WARMUP, timeout=5)
                except Exception:
                    pass
                for sym in remaining:
                    try:
                        resp = client.get(_NSE_QUOTE_URL, params={"symbol": sym.upper()}, timeout=_TIMEOUT)
                        if resp.status_code == 200:
                            data = resp.json()
                            p_info = data.get("priceInfo", {})
                            close = _safe(p_info.get("lastPrice") or p_info.get("close"))
                            if close > 0:
                                prices[sym] = close
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("NSE quote fetch failed: %s", exc)

    # 3. Try ClickHouse daily_prices (populated via NSE/Shoonya)
    remaining = [s for s in symbols if s not in prices]
    if remaining:
        try:
            from src.db.pool import query_df
            sym_list = "', '".join(remaining)
            df = query_df(f"""
                SELECT symbol, argMax(close, trade_date) as last_close
                FROM market_data.daily_prices FINAL
                WHERE symbol IN ('{sym_list}')
                GROUP BY symbol
            """)
            for _, r in df.iterrows():
                val = float(r["last_close"])
                if val > 0:
                    prices[r["symbol"]] = val
        except Exception as exc:
            logger.debug("ClickHouse daily_prices fetch failed: %s", exc)

    # 4. Fall back to yfinance (period='5d' for weekend/after-hours resilience)
    remaining = [s for s in symbols if s not in prices]
    if remaining:
        try:
            yf_syms = [f"{s}.NS" for s in remaining]
            data = yf.download(yf_syms, period="5d", progress=False)
            if not data.empty and "Close" in data.columns:
                close_df = data["Close"]
                for sym in remaining:
                    yf_sym = f"{sym}.NS"
                    series = None
                    if hasattr(close_df, "columns"):
                        if yf_sym in close_df.columns:
                            series = close_df[yf_sym]
                        elif len(yf_syms) == 1:
                            series = close_df.iloc[:, 0]
                    else:
                        series = close_df
                    if series is not None:
                        series = series.dropna()
                        if not series.empty:
                            val = series.iloc[-1]
                            if hasattr(val, "iloc"):
                                val = val.iloc[-1]
                            p = float(val)
                            if p > 0:
                                prices[sym] = p
        except Exception as exc:
            logger.debug("yfinance batch price fetch failed: %s", exc)

    return prices


class BaseInavFetcher(ABC):
    """
    Template Method base for AMC iNAV fetchers.

    Subclasses must set `source_label` and `symbols` as class attributes,
    and implement the four abstract methods below.
    """

    #: Source label written into each output row, e.g. "nippon_amc_live"
    source_label: str

    #: Set of NSE symbols this fetcher covers
    symbols: frozenset[str]

    # ── Abstract hooks ────────────────────────────────────────────────────

    @abstractmethod
    def _fetch_raw(self) -> Any:
        """
        Perform the HTTP request(s) to the AMC API.

        Returns the parsed JSON body (dict or list).
        Must raise an exception on failure (caught by the template method).
        """

    @abstractmethod
    def _match_symbols(self, raw: Any, target: set[str]) -> dict[str, Any]:
        """
        Extract the subset of items from the raw API response that correspond
        to symbols in `target`.

        Returns {nse_symbol (upper): api_item}.
        """

    @abstractmethod
    def _extract_inav(self, item: Any) -> float | None:
        """
        Pull the iNAV value out of a single api_item.

        Return None to skip the row entirely.
        """

    @abstractmethod
    def _extract_timestamp(self, item: Any) -> datetime:
        """
        Parse the timestamp from a single api_item and return a naive UTC datetime.
        """

    def _extract_fallback_price(self, item: Any) -> float | None:
        """
        Return the declared NAV (or any other price) to use when yfinance has
        no data.  Default: None (falls back to iNAV itself).
        """
        return None

    def _staleness_check(self, sym: str, snapshot_at: datetime, inav: float) -> bool:
        """
        Return True to *keep* this row, False to discard it.

        Default: always keep.  Override to add staleness gates (e.g. Motilal).
        """
        return True

    # ── Template method ───────────────────────────────────────────────────

    def fetch_inav(self, symbols: list[str]) -> list[dict[str, Any]]:
        """
        Full pipeline: filter → fetch raw → match → yfinance prices → build rows.

        Parameters
        ----------
        symbols : NSE symbols requested by the caller

        Returns
        -------
        list of row dicts: symbol, snapshot_at, inav, market_price,
        premium_discount_pct, source
        """
        target = {s.upper().replace(".NS", "") for s in symbols} & set(self.symbols)
        if not target:
            return []

        logger.info("%s iNAV: fetching for %s", self.source_label, sorted(target))

        try:
            raw = self._fetch_raw()
        except Exception as exc:
            logger.warning("%s iNAV: fetch failed: %s", self.source_label, exc)
            return []

        matched = self._match_symbols(raw, target)
        if not matched:
            logger.info("%s iNAV: no matching symbols in API response", self.source_label)
            return []

        prices = _fetch_market_prices(list(matched))

        rows: list[dict[str, Any]] = []
        for sym, item in matched.items():
            raw_inav = self._extract_inav(item)
            if raw_inav is None:
                continue
            inav = _safe(raw_inav)
            if inav <= 0:
                continue

            snapshot_at = self._extract_timestamp(item)

            if not self._staleness_check(sym, snapshot_at, inav):
                continue

            market_price = prices.get(sym)
            if market_price is None or market_price <= 0:
                logger.warning(
                    "%s: no secondary market price resolved from Shoonya, NSE, or daily_prices. Skipping to avoid corrupt premium.",
                    sym,
                )
                continue

            prem_disc = (market_price - inav) / inav * 100

            rows.append({
                "symbol":               sym,
                "snapshot_at":          snapshot_at,
                "inav":                 inav,
                "market_price":         market_price,
                "premium_discount_pct": round(prem_disc, 4),
                "source":               self.source_label,
            })

        logger.info("%s iNAV: compiled %d row(s)", self.source_label, len(rows))
        return rows
