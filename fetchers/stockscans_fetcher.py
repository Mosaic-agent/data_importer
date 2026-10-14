"""
src/data_importer/fetchers/stockscans_fetcher.py
─────────────────────────────────────────────────
Data fetcher for StockScans.in (https://www.stockscans.in/).

Provides direct access to:
  1. Indian Equity Financial Statements (Quarterly & Yearly P&L, Balance Sheet, Cash Flow)
  2. Valuation Snapshot (P/E, P/B, P/S, EV/EBITDA, ROCE, ROE, Debt/Equity, Margins)
  3. Mainboard & SME IPO Tracking Board (Pipeline, Price Bands, Subscription Multiples)
  4. Earnings Conference Call (Concall) Schedules (Today & Upcoming)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, TypeVar

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.stockscans.in"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Referer": "https://www.stockscans.in/",
}
_TIMEOUT = 12
T = TypeVar("T")

# StockScans enforces a rolling-window request budget, not a simple concurrent-
# connection cap: 4-way concurrency on a 30-symbol batch ran clean (0 429s) in
# live testing, but 8-way tripped it partway through, and once tripped even
# slow sequential requests kept failing for 30s+. So instead of retrying hot,
# every worker thread shares one pacing gate and one circuit breaker below.
_rate_lock = threading.Lock()
_next_slot_at = 0.0
_backoff_until = 0.0
_MIN_REQUEST_INTERVAL = 0.15


def get_stockscans_headers() -> dict[str, str]:
    """Return base headers enriched with authentication token cookie if configured."""
    headers = dict(_HEADERS)
    token = os.getenv("STOCKSCANS_AUTH_TOKEN", "").strip()
    if not token:
        try:
            from config.settings import settings
            token = getattr(settings, "stockscans_auth_token", "").strip()
        except Exception:
            pass
    if token:
        headers["Cookie"] = f"authtoken={token}"
        headers["Authorization"] = f"Bearer {token}"
    return headers


def to_company_id(symbol: str) -> str:
    """Normalize a symbol to StockScans companyId format, e.g. RELIANCE -> NSE:RELIANCE."""
    s = symbol.upper().replace(".NS", "").replace(".BO", "").strip()
    return s if ":" in s else f"NSE:{s}"


def to_symbol(company_id: str) -> str:
    """Extract clean ticker symbol from companyId, e.g. NSE:RELIANCE -> RELIANCE."""
    return company_id.split(":")[-1].strip().upper()


def _clean_symbols(symbols: list[tuple[str, str] | str]) -> list[str]:
    """Return stable, de-duplicated NSE symbols for one import batch."""
    return list(dict.fromkeys(
        to_symbol(to_company_id(item[0] if isinstance(item, tuple) else str(item)))
        for item in symbols
    ))


def _as_float(value: Any) -> float:
    """Parse common StockScans number formats without discarding a ticker."""
    if value in (None, "", "-", "—", "N/A"):
        return 0.0
    try:
        return float(
            str(value).replace(",", "").replace("%", "").replace("₹", "")
            .replace("Cr", "").replace("cr", "").strip()
        )
    except (TypeError, ValueError):
        return 0.0


def _session(headers: dict[str, str]) -> requests.Session:
    """Build one keep-alive session per logical batch of StockScans requests."""
    session = requests.Session()
    session.headers.update(headers)
    return session


def _throttle() -> None:
    """Block the calling thread until it may issue one StockScans request.

    Shared across every worker thread in the process: enforces a minimum
    spacing between *any* two requests (not just within one thread) and
    honors a cooldown window set by `_trip_breaker` after a 429.
    """
    global _next_slot_at
    while True:
        with _rate_lock:
            now = time.monotonic()
            wait_for = max(_backoff_until, _next_slot_at) - now
            if wait_for <= 0:
                _next_slot_at = now + _MIN_REQUEST_INTERVAL
                return
        time.sleep(min(wait_for, 1.0))


def _trip_breaker(cooldown: float = 8.0) -> None:
    """Pause every thread for `cooldown` seconds after any of them sees a 429."""
    global _backoff_until
    with _rate_lock:
        _backoff_until = max(_backoff_until, time.monotonic() + cooldown)


def _run_concurrent(items: list[str], worker: Callable[[str], T], max_workers: int) -> list[T]:
    """Run `worker` over `items`, preserving input order in the result list."""
    if max_workers <= 1 or len(items) <= 1:
        return [worker(item) for item in items]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(worker, items))


def fetch_stockscans_peer_valuation(
    company_id: str,
    *,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch valuation multiples and financials for a company and its industry peers via /api/company/peers.
    Fast, reliable, and unthrottled.
    """
    url = f"{_BASE_URL}/api/company/peers"
    try:
        client = session or requests
        request_kwargs = {
            "json": {"companyId": company_id},
            "timeout": _TIMEOUT,
        }
        if session is None:
            request_kwargs["headers"] = get_stockscans_headers()
        resp = client.post(
            url,
            **request_kwargs,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        val_rows: list[dict[str, Any]] = []
        snap_date = date.today()
        for c in data.get("companies") or []:
            ticker = to_symbol(c.get("companyId", ""))
            val_rows.append({
                "_dataset": "valuation",
                "symbol": ticker,
                "snapshot_date": snap_date,
                "market_cap": _as_float(c.get("Market Capitalization")),
                "trailing_pe": _as_float(c.get("Price To Earnings")),
                "forward_pe": 0.0,
                "peg_ratio": 0.0,
                "price_to_book": _as_float(c.get("Price To Book Value")),
                "price_to_sales": _as_float(c.get("Price To Sales")),
                "debt_to_equity": _as_float(c.get("Debt To Equity")),
                "return_on_equity": _as_float(c.get("ROE")),
                "profit_margin": _as_float(c.get("NPM")),
                "operating_margin": _as_float(c.get("OPM")),
                "gross_margin": _as_float(c.get("GPM")),
                "revenue_growth": _as_float(c.get("Revenue Growth TTM")),
                "earnings_growth": _as_float(c.get("PAT Growth TTM")),
                "free_cashflow": 0.0,
                "beta": 0.0,
                "target_price": 0.0,
                "recommendation": "",
                "analyst_count": 0,
            })
        return val_rows
    except Exception as exc:
        logger.debug("StockScans peer valuation fetch failed for %s: %s", company_id, exc)
        return []


def _fetch_fundamentals_one(
    sym: str,
    session: requests.Session,
    source_type: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Fetch one symbol's valuation + statement rows for the concurrent path.

    Used only when `max_workers > 1`; paces itself and reports 429s through
    the shared `_throttle`/`_trip_breaker` gate instead of sleeping locally,
    so every worker thread backs off together.
    """
    cid = to_company_id(sym)
    ticker = to_symbol(cid)
    url = f"{_BASE_URL}/api/company/fundamentals/statements/{cid}/{source_type}"
    data = None
    for attempt in range(2):
        _throttle()
        try:
            response = session.get(url, timeout=_TIMEOUT)
            if response.status_code == 200:
                data = response.json()
                break
            if response.status_code == 429:
                _trip_breaker()
                if attempt == 0:
                    continue
            logger.debug("StockScans fundamentals status %s for %s", response.status_code, sym)
            break
        except Exception as exc:
            if attempt == 1:
                logger.debug("StockScans fundamentals error for %s: %s", sym, exc)

    if not data:
        return None, []

    cur_ratios = data.get("currentRatios") or {}
    meta_ratios = data.get("metaRatios") or {}
    snap_date_str = str(meta_ratios.get("Price Updated At") or "")[:10]
    try:
        snap_date = date.fromisoformat(snap_date_str) if snap_date_str else date.today()
    except ValueError:
        snap_date = date.today()
    valuation_row = {
        "_dataset": "valuation", "symbol": ticker, "snapshot_date": snap_date,
        "market_cap": _as_float(cur_ratios.get("Market Capitalization")),
        "trailing_pe": _as_float(cur_ratios.get("Price To Earnings")),
        "forward_pe": 0.0, "peg_ratio": 0.0,
        "price_to_book": _as_float(cur_ratios.get("Price To Book")),
        "price_to_sales": _as_float(cur_ratios.get("Price To Sales")),
        "debt_to_equity": _as_float(cur_ratios.get("Debt To Equity")),
        "return_on_equity": _as_float(cur_ratios.get("ROE")),
        "profit_margin": _as_float(cur_ratios.get("NPM")),
        "operating_margin": _as_float(cur_ratios.get("OPM")),
        "gross_margin": _as_float(cur_ratios.get("GPM")),
        "revenue_growth": 0.0, "earnings_growth": 0.0, "free_cashflow": 0.0,
        "beta": 0.0, "target_price": 0.0, "recommendation": "", "analyst_count": 0,
    }

    statement_rows: list[dict[str, Any]] = []
    try:
        for statement_type, revenue_growth_key, pat_growth_key, rows in (
            ("quarterly", "Revenue Growth YoY", "PAT Growth YoY", data.get("quarterly") or []),
            ("yearly", "Revenue Growth", "PAT Growth", data.get("yearly") or []),
        ):
            if len(rows) < 2:
                continue
            for values in rows[1:]:
                row = dict(zip(rows[0], values))
                period_date = str(row.get("Date") or "")
                if not period_date:
                    continue
                statement_rows.append({
                    "_dataset": "statements", "symbol": ticker,
                    "statement_type": statement_type, "period_date": period_date,
                    "revenue": _as_float(row.get("Revenue")),
                    "revenue_growth_yoy": _as_float(row.get(revenue_growth_key)),
                    "operating_profit": _as_float(row.get("Operating Profit")),
                    "opm": _as_float(row.get("OPM")),
                    "other_income": _as_float(row.get("Other Income")),
                    "interest": _as_float(row.get("Interest Expense")),
                    "depreciation": _as_float(row.get("Depreciation")),
                    "pbt": _as_float(row.get("PBT")), "tax": _as_float(row.get("Tax")),
                    "pat": _as_float(row.get("PAT")),
                    "pat_growth_yoy": _as_float(row.get(pat_growth_key)),
                    "npm": _as_float(row.get("NPM")), "eps": _as_float(row.get("EPS")),
                    "book_value": _as_float(row.get("Book Value")), "source": "stockscans",
                })
    except Exception as exc:
        logger.warning("Failed fetching StockScans fundamentals for %s: %s", sym, exc)

    return valuation_row, statement_rows


def fetch_stockscans_fundamentals(
    symbols: list[tuple[str, str] | str],
    source_type: str = "C",
    delay_between_calls: float = 0.3,
    max_workers: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch financial statements and valuation snapshots for a list of symbols.

    Parameters
    ----------
    symbols : list of symbol strings or (nse_symbol, yahoo_ticker) tuples
    source_type : 'C' for Consolidated (default), 'S' for Standalone
    delay_between_calls : float, delay between requests; only used when max_workers == 1
    max_workers : concurrent in-flight requests. Live-tested safe at 4 on a
        30-symbol batch (0 429s); 8+ tripped StockScans' rate limiter. All
        workers share one pacing gate and circuit breaker (`_throttle` /
        `_trip_breaker`) regardless of this value, so raising it is safer
        than it looks but still bounded by what was actually verified.

    Returns
    -------
    tuple of (valuation_rows, statement_rows)
    """
    valuation_rows: list[dict[str, Any]] = []
    statement_rows: list[dict[str, Any]] = []
    seen_val: set[str] = set()
    clean_symbols = _clean_symbols(symbols)
    session = _session(get_stockscans_headers())
    try:
        if max_workers > 1 and len(clean_symbols) > 1:
            results = _run_concurrent(
                clean_symbols,
                lambda sym: _fetch_fundamentals_one(sym, session, source_type),
                max_workers,
            )
            needs_fallback: list[str] = []
            for sym, (valuation_row, stmt_rows) in zip(clean_symbols, results):
                ticker = to_symbol(to_company_id(sym))
                if valuation_row is not None:
                    valuation_rows.append(valuation_row)
                    seen_val.add(ticker)
                    statement_rows.extend(stmt_rows)
                else:
                    needs_fallback.append(sym)

            for sym in needs_fallback:
                cid = to_company_id(sym)
                ticker = to_symbol(cid)
                if ticker in seen_val:
                    continue
                for peer in fetch_stockscans_peer_valuation(cid, session=session):
                    peer_symbol = peer["symbol"]
                    if peer_symbol not in seen_val and (
                        peer_symbol == ticker or peer_symbol in clean_symbols
                    ):
                        valuation_rows.append(peer)
                        seen_val.add(peer_symbol)
            return valuation_rows, statement_rows

        for sym in clean_symbols:
            cid = to_company_id(sym)
            ticker = to_symbol(cid)
            url = f"{_BASE_URL}/api/company/fundamentals/statements/{cid}/{source_type}"
            data = None
            for attempt in range(2):
                try:
                    response = session.get(url, timeout=_TIMEOUT)
                    if response.status_code == 200:
                        data = response.json()
                        break
                    if response.status_code == 429 and attempt == 0:
                        time.sleep(1.0)
                        continue
                    logger.debug("StockScans fundamentals status %s for %s", response.status_code, sym)
                    break
                except Exception as exc:
                    if attempt == 1:
                        logger.debug("StockScans fundamentals error for %s: %s", sym, exc)
                    else:
                        time.sleep(0.5)

            if not data:
                if ticker not in seen_val:
                    for peer in fetch_stockscans_peer_valuation(cid, session=session):
                        peer_symbol = peer["symbol"]
                        if peer_symbol not in seen_val and (
                            peer_symbol == ticker or peer_symbol in clean_symbols
                        ):
                            valuation_rows.append(peer)
                            seen_val.add(peer_symbol)
                if delay_between_calls > 0:
                    time.sleep(delay_between_calls)
                continue

            try:
                cur_ratios = data.get("currentRatios") or {}
                meta_ratios = data.get("metaRatios") or {}
                snap_date_str = str(meta_ratios.get("Price Updated At") or "")[:10]
                try:
                    snap_date = date.fromisoformat(snap_date_str) if snap_date_str else date.today()
                except ValueError:
                    snap_date = date.today()
                valuation_rows.append({
                    "_dataset": "valuation", "symbol": ticker, "snapshot_date": snap_date,
                    "market_cap": _as_float(cur_ratios.get("Market Capitalization")),
                    "trailing_pe": _as_float(cur_ratios.get("Price To Earnings")),
                    "forward_pe": 0.0, "peg_ratio": 0.0,
                    "price_to_book": _as_float(cur_ratios.get("Price To Book")),
                    "price_to_sales": _as_float(cur_ratios.get("Price To Sales")),
                    "debt_to_equity": _as_float(cur_ratios.get("Debt To Equity")),
                    "return_on_equity": _as_float(cur_ratios.get("ROE")),
                    "profit_margin": _as_float(cur_ratios.get("NPM")),
                    "operating_margin": _as_float(cur_ratios.get("OPM")),
                    "gross_margin": _as_float(cur_ratios.get("GPM")),
                    "revenue_growth": 0.0, "earnings_growth": 0.0, "free_cashflow": 0.0,
                    "beta": 0.0, "target_price": 0.0, "recommendation": "", "analyst_count": 0,
                })
                seen_val.add(ticker)

                for statement_type, revenue_growth_key, pat_growth_key, rows in (
                    ("quarterly", "Revenue Growth YoY", "PAT Growth YoY", data.get("quarterly") or []),
                    ("yearly", "Revenue Growth", "PAT Growth", data.get("yearly") or []),
                ):
                    if len(rows) < 2:
                        continue
                    for values in rows[1:]:
                        row = dict(zip(rows[0], values))
                        period_date = str(row.get("Date") or "")
                        if not period_date:
                            continue
                        statement_rows.append({
                            "_dataset": "statements", "symbol": ticker,
                            "statement_type": statement_type, "period_date": period_date,
                            "revenue": _as_float(row.get("Revenue")),
                            "revenue_growth_yoy": _as_float(row.get(revenue_growth_key)),
                            "operating_profit": _as_float(row.get("Operating Profit")),
                            "opm": _as_float(row.get("OPM")),
                            "other_income": _as_float(row.get("Other Income")),
                            "interest": _as_float(row.get("Interest Expense")),
                            "depreciation": _as_float(row.get("Depreciation")),
                            "pbt": _as_float(row.get("PBT")), "tax": _as_float(row.get("Tax")),
                            "pat": _as_float(row.get("PAT")),
                            "pat_growth_yoy": _as_float(row.get(pat_growth_key)),
                            "npm": _as_float(row.get("NPM")), "eps": _as_float(row.get("EPS")),
                            "book_value": _as_float(row.get("Book Value")), "source": "stockscans",
                        })
            except Exception as exc:
                logger.warning("Failed fetching StockScans fundamentals for %s: %s", sym, exc)
            if delay_between_calls > 0:
                time.sleep(delay_between_calls)
    finally:
        session.close()
    return valuation_rows, statement_rows


def fetch_stockscans_ipos() -> list[dict[str, Any]]:
    """
    Fetch the Indian IPO tracking board (Mainboard and SME IPOs).

    Returns
    -------
    list of dicts matching market_data.ipo_pipeline schema
    """
    url = f"{_BASE_URL}/api/scans/ipo/board"
    rows: list[dict[str, Any]] = []
    headers = get_stockscans_headers()

    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("StockScans IPO board returned %s", resp.status_code)
            return rows

        data = resp.json()
        cols = data.get("cols") or []
        raw_rows = data.get("rows") or []

        def parse_date(v: Any) -> date | None:
            if not v or v == "-":
                return None
            try:
                return date.fromisoformat(str(v)[:10])
            except Exception:
                return None

        for r in raw_rows:
            d = dict(zip(cols, r))
            slug = str(d.get("slug") or "")
            if not slug:
                continue

            row = {
                "_dataset": "ipo",
                "company_key": str(d.get("companyKey") or ""),
                "symbol": to_symbol(str(d.get("companyId") or "")),
                "slug": slug,
                "name": str(d.get("name") or ""),
                "sector": str(d.get("sector") or ""),
                "board": str(d.get("board") or "mainboard").lower(),
                "stage": str(d.get("stage") or "upcoming").lower(),
                "open_date": parse_date(d.get("openDate")),
                "close_date": parse_date(d.get("closeDate")),
                "allotment_date": parse_date(d.get("allotmentDate")),
                "listing_date": parse_date(d.get("listingDate")),
                "price_band_low": _as_float(d.get("priceBandLow")),
                "price_band_high": _as_float(d.get("priceBandHigh")),
                "lot_size": int(_as_float(d.get("lotSize"))),
                "min_investment": _as_float(d.get("minInvestment")),
                "issue_size_cr": _as_float(d.get("issueSizeCr")),
                "sub_qib": _as_float(d.get("subQib")),
                "sub_nii": _as_float(d.get("subNii")),
                "sub_retail": _as_float(d.get("subRetail")),
                "sub_total": _as_float(d.get("subTotal")),
            }
            rows.append(row)

    except Exception as exc:
        logger.warning("Failed fetching StockScans IPO board: %s", exc)

    return rows


def fetch_stockscans_concalls(days_ahead: int = 30) -> list[dict[str, Any]]:
    """
    Fetch today's and upcoming corporate conference call schedules.

    Returns
    -------
    list of dicts matching market_data.concall_schedules schema
    """
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    headers = get_stockscans_headers()

    # 1. Today's concalls
    try:
        resp_today = requests.get(f"{_BASE_URL}/api/scans/concall/today", headers=headers, timeout=_TIMEOUT)
        if resp_today.status_code == 200:
            for item in resp_today.json().get("rows") or []:
                if len(item) < 4:
                    continue
                ckey, cid, cname, dt_str = item[0], item[1], item[2], item[3]
                sym = to_symbol(str(cid or ""))
                key_tuple = (str(ckey), sym)
                if key_tuple in seen:
                    continue
                seen.add(key_tuple)

                call_time = None
                if dt_str:
                    try:
                        call_time = datetime.fromisoformat(dt_str)
                    except Exception:
                        pass

                rows.append({
                    "_dataset": "concalls",
                    "company_key": str(ckey or ""),
                    "symbol": sym,
                    "company_name": str(cname or ""),
                    "call_time": call_time,
                    "status": "scheduled",
                })
    except Exception as exc:
        logger.warning("Failed fetching StockScans today concalls: %s", exc)

    # 2. Upcoming concalls
    try:
        payload = {"start": str(date.today()), "days": days_ahead, "watchlistIds": []}
        resp_up = requests.post(
            f"{_BASE_URL}/api/scans/concall/upcoming",
            headers=headers,
            json=payload,
            timeout=_TIMEOUT,
        )
        if resp_up.status_code == 200:
            for item in resp_up.json().get("rows") or []:
                if len(item) < 4:
                    continue
                ckey, cid, cname, dt_str = item[0], item[1], item[2], item[3]
                sym = to_symbol(str(cid or ""))
                key_tuple = (str(ckey), sym)
                if key_tuple in seen:
                    continue
                seen.add(key_tuple)

                call_time = None
                if dt_str:
                    try:
                        call_time = datetime.fromisoformat(dt_str)
                    except Exception:
                        pass

                rows.append({
                    "_dataset": "concalls",
                    "company_key": str(ckey or ""),
                    "symbol": sym,
                    "company_name": str(cname or ""),
                    "call_time": call_time,
                    "status": "scheduled",
                })
    except Exception as exc:
        logger.warning("Failed fetching StockScans upcoming concalls: %s", exc)

    return rows


def _fetch_insider_one(sym: str, session: requests.Session) -> list[dict[str, Any]]:
    """Fetch one symbol's insider trades for the concurrent path (see `_fetch_fundamentals_one`)."""
    cid = to_company_id(sym)
    ticker = to_symbol(cid)
    url = f"{_BASE_URL}/api/company/holdings/insider-trading/{cid}"
    rows: list[dict[str, Any]] = []
    _throttle()
    try:
        resp = session.get(url, timeout=_TIMEOUT)
        if resp.status_code == 429:
            _trip_breaker()
        if resp.status_code != 200:
            return rows
        for record in resp.json().get("insiderTrading") or []:
            d_str = record.get("date")
            if not d_str:
                continue
            try:
                t_date = date.fromisoformat(str(d_str)[:10])
            except (TypeError, ValueError):
                continue
            rows.append({
                "_dataset": "insider", "symbol": ticker,
                "insider_name": str(record.get("shareholderName") or ""),
                "relation": str(record.get("shareholderCategory") or ""),
                "transaction_date": t_date,
                "transaction_type": str(record.get("transactionType") or ""),
                "shares": _as_float(record.get("shareQuantity")),
                "value": _as_float(record.get("valueOfSecurities")),
                "source": "stockscans",
            })
    except Exception as exc:
        logger.debug("Failed fetching StockScans insider trading for %s: %s", sym, exc)
    return rows


def fetch_stockscans_insider_trades(
    symbols: list[tuple[str, str] | str],
    delay_between_calls: float = 0.2,
    max_workers: int = 1,
) -> list[dict[str, Any]]:
    """
    Fetch Indian stock insider trading records via private StockScans API.
    Compatible with market_data.stock_insider_trades schema.

    max_workers : concurrent in-flight requests (see `fetch_stockscans_fundamentals`
        for the live-tested safe range and the shared circuit breaker).
    """
    rows: list[dict[str, Any]] = []
    session = _session(get_stockscans_headers())
    try:
        clean_symbols = _clean_symbols(symbols)
        if max_workers > 1 and len(clean_symbols) > 1:
            for batch_rows in _run_concurrent(
                clean_symbols, lambda sym: _fetch_insider_one(sym, session), max_workers
            ):
                rows.extend(batch_rows)
            return rows

        for sym in clean_symbols:
            cid = to_company_id(sym)
            ticker = to_symbol(cid)
            url = f"{_BASE_URL}/api/company/holdings/insider-trading/{cid}"
            try:
                resp = session.get(url, timeout=_TIMEOUT)
                if resp.status_code != 200:
                    continue
                for record in resp.json().get("insiderTrading") or []:
                    d_str = record.get("date")
                    if not d_str:
                        continue
                    try:
                        t_date = date.fromisoformat(str(d_str)[:10])
                    except (TypeError, ValueError):
                        continue
                    rows.append({
                        "_dataset": "insider", "symbol": ticker,
                        "insider_name": str(record.get("shareholderName") or ""),
                        "relation": str(record.get("shareholderCategory") or ""),
                        "transaction_date": t_date,
                        "transaction_type": str(record.get("transactionType") or ""),
                        "shares": _as_float(record.get("shareQuantity")),
                        "value": _as_float(record.get("valueOfSecurities")),
                        "source": "stockscans",
                    })
                if delay_between_calls > 0:
                    time.sleep(delay_between_calls)
            except Exception as exc:
                logger.debug("Failed fetching StockScans insider trading for %s: %s", sym, exc)
    finally:
        session.close()

    return rows


def fetch_stockscans_shareholding(
    symbol: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """
    Fetch quarterly shareholding pattern (Promoter, FII, DII, Public) and detailed entities.
    """
    cid = to_company_id(symbol)
    url = f"{_BASE_URL}/api/company/holdings/shareholding/{cid}"
    try:
        client = session or requests
        request_kwargs: dict[str, Any] = {"timeout": _TIMEOUT}
        if session is None:
            request_kwargs["headers"] = get_stockscans_headers()
        resp = client.get(url, **request_kwargs)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:
        logger.debug("Failed fetching StockScans shareholding for %s: %s", symbol, exc)
    return {}


def parse_quarter_str(q_str: str) -> date | None:
    """Convert YYYYMM string (e.g. 202403) to quarter-end Date (e.g. 2024-03-31)."""
    import calendar
    try:
        s = str(q_str).strip()
        if len(s) == 6 and s.isdigit():
            y = int(s[:4])
            m = int(s[4:])
            last_day = calendar.monthrange(y, m)[1]
            return date(y, m, last_day)
    except Exception:
        pass
    return None


def _fetch_shareholding_one(
    sym: str, session: requests.Session
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch one symbol's shareholding pattern + entities for the concurrent path."""
    cid = to_company_id(sym)
    ticker = to_symbol(cid)
    pattern_rows: list[dict[str, Any]] = []
    entity_rows: list[dict[str, Any]] = []

    _throttle()
    data = fetch_stockscans_shareholding(cid, session=session)
    if not data:
        return pattern_rows, entity_rows

    # 1. Aggregate quarterly patterns
    agg = data.get("aggregate") or []
    if len(agg) > 1:
        quarters = agg[0][1:]
        by_quarter: dict[str, dict[str, Any]] = {q: {} for q in quarters}
        for r in agg[1:]:
            metric = str(r[0]).strip().lower()
            for q, val in zip(quarters, r[1:]):
                by_quarter[q][metric] = val

        for q_str, metrics in by_quarter.items():
            qd = parse_quarter_str(q_str)
            if not qd:
                continue

            pattern_rows.append({
                "_dataset": "shareholding_pattern",
                "symbol": ticker,
                "quarter_date": qd,
                "quarter_str": str(q_str),
                "promoter_pct": _as_float(metrics.get("promoter")),
                "fii_pct": _as_float(metrics.get("fii")),
                "dii_pct": _as_float(metrics.get("dii")),
                "government_pct": _as_float(metrics.get("government")),
                "public_pct": _as_float(metrics.get("public")),
                "no_of_shareholders": int(_as_float(metrics.get("no of shareholders"))),
                "source": "stockscans",
            })

    # 2. Detailed major entities (>1% holdings)
    details = data.get("details") or {}
    for category, cat_rows in details.items():
        if not cat_rows or len(cat_rows) < 2:
            continue
        cat_quarters = cat_rows[0][1:]
        for r in cat_rows[1:]:
            holder_name = str(r[0]).strip()
            for q_str, val in zip(cat_quarters, r[1:]):
                if val is not None:
                    fval = _as_float(val)
                    if fval > 0.0:
                        qd = parse_quarter_str(q_str)
                        if qd:
                            entity_rows.append({
                                "_dataset": "shareholding_entities",
                                "symbol": ticker,
                                "category": str(category),
                                "holder_name": holder_name,
                                "quarter_date": qd,
                                "quarter_str": str(q_str),
                                "holding_pct": fval,
                                "source": "stockscans",
                            })

    return pattern_rows, entity_rows


def fetch_stockscans_shareholding_patterns(
    symbols: list[tuple[str, str] | str],
    delay_between_calls: float = 0.2,
    max_workers: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch quarterly shareholding pattern (aggregates and major entities) for symbols.

    max_workers : concurrent in-flight requests (see `fetch_stockscans_fundamentals`
        for the live-tested safe range and the shared circuit breaker).

    Returns
    -------
    tuple of (pattern_rows, entity_rows)
    """
    pattern_rows: list[dict[str, Any]] = []
    entity_rows: list[dict[str, Any]] = []

    session = _session(get_stockscans_headers())
    try:
        clean_symbols = _clean_symbols(symbols)
        if max_workers > 1 and len(clean_symbols) > 1:
            for p_rows, e_rows in _run_concurrent(
                clean_symbols, lambda sym: _fetch_shareholding_one(sym, session), max_workers
            ):
                pattern_rows.extend(p_rows)
                entity_rows.extend(e_rows)
            return pattern_rows, entity_rows

        for sym in clean_symbols:
            cid = to_company_id(sym)
            ticker = to_symbol(cid)
            data = fetch_stockscans_shareholding(cid, session=session)
            if not data:
                continue

            # 1. Aggregate quarterly patterns
            agg = data.get("aggregate") or []
            if len(agg) > 1:
                quarters = agg[0][1:]
                by_quarter: dict[str, dict[str, Any]] = {q: {} for q in quarters}
                for r in agg[1:]:
                    metric = str(r[0]).strip().lower()
                    for q, val in zip(quarters, r[1:]):
                        by_quarter[q][metric] = val

                for q_str, metrics in by_quarter.items():
                    qd = parse_quarter_str(q_str)
                    if not qd:
                        continue

                    pattern_rows.append({
                        "_dataset": "shareholding_pattern",
                        "symbol": ticker,
                        "quarter_date": qd,
                        "quarter_str": str(q_str),
                        "promoter_pct": _as_float(metrics.get("promoter")),
                        "fii_pct": _as_float(metrics.get("fii")),
                        "dii_pct": _as_float(metrics.get("dii")),
                        "government_pct": _as_float(metrics.get("government")),
                        "public_pct": _as_float(metrics.get("public")),
                        "no_of_shareholders": int(_as_float(metrics.get("no of shareholders"))),
                        "source": "stockscans",
                    })

            # 2. Detailed major entities (>1% holdings)
            details = data.get("details") or {}
            for category, cat_rows in details.items():
                if not cat_rows or len(cat_rows) < 2:
                    continue
                cat_quarters = cat_rows[0][1:]
                for r in cat_rows[1:]:
                    holder_name = str(r[0]).strip()
                    for q_str, val in zip(cat_quarters, r[1:]):
                        if val is not None:
                            fval = _as_float(val)
                            if fval > 0.0:
                                qd = parse_quarter_str(q_str)
                                if qd:
                                    entity_rows.append({
                                        "_dataset": "shareholding_entities",
                                        "symbol": ticker,
                                        "category": str(category),
                                        "holder_name": holder_name,
                                        "quarter_date": qd,
                                        "quarter_str": str(q_str),
                                        "holding_pct": fval,
                                        "source": "stockscans",
                                    })

            if delay_between_calls > 0:
                time.sleep(delay_between_calls)
    finally:
        session.close()

    return pattern_rows, entity_rows


def fetch_stockscans_bulk_deals(symbol: str) -> list[dict[str, Any]]:
    """
    Fetch bulk & block deals for a specific stock via private StockScans API.
    """
    headers = get_stockscans_headers()
    cid = to_company_id(symbol)
    url = f"{_BASE_URL}/api/company/holdings/bulk-block-deals/{cid}"
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("bulkBlockDeals") or []
    except Exception as exc:
        logger.debug("Failed fetching StockScans bulk deals for %s: %s", symbol, exc)
    return []
