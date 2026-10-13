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
import time
from datetime import date, datetime
from typing import Any

import os
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


def fetch_stockscans_peer_valuation(company_id: str) -> list[dict[str, Any]]:
    """
    Fetch valuation multiples and financials for a company and its industry peers via /api/company/peers.
    Fast, reliable, and unthrottled.
    """
    headers = get_stockscans_headers()
    url = f"{_BASE_URL}/api/company/peers"
    try:
        resp = requests.post(
            url,
            json={"companyId": company_id},
            headers=headers,
            timeout=_TIMEOUT,
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
                "market_cap": float(c.get("Market Capitalization") or 0.0),
                "trailing_pe": float(c.get("Price To Earnings") or 0.0),
                "forward_pe": 0.0,
                "peg_ratio": 0.0,
                "price_to_book": float(c.get("Price To Book Value") or 0.0),
                "price_to_sales": float(c.get("Price To Sales") or 0.0),
                "debt_to_equity": float(c.get("Debt To Equity") or 0.0),
                "return_on_equity": float(c.get("ROE") or 0.0),
                "profit_margin": float(c.get("NPM") or 0.0),
                "operating_margin": float(c.get("OPM") or 0.0),
                "gross_margin": float(c.get("GPM") or 0.0),
                "revenue_growth": float(c.get("Revenue Growth TTM") or 0.0),
                "earnings_growth": float(c.get("PAT Growth TTM") or 0.0),
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


def fetch_stockscans_fundamentals(
    symbols: list[tuple[str, str] | str],
    source_type: str = "C",
    delay_between_calls: float = 0.3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch financial statements and valuation snapshots for a list of symbols.

    Parameters
    ----------
    symbols : list of symbol strings or (nse_symbol, yahoo_ticker) tuples
    source_type : 'C' for Consolidated (default), 'S' for Standalone
    delay_between_calls : float, delay between requests to be respectful

    Returns
    -------
    tuple of (valuation_rows, statement_rows)
    """
    headers = get_stockscans_headers()
    valuation_rows: list[dict[str, Any]] = []
    statement_rows: list[dict[str, Any]] = []
    seen_val: set[str] = set()

    clean_symbols: list[str] = []
    for item in symbols:
        if isinstance(item, tuple):
            clean_symbols.append(item[0])
        else:
            clean_symbols.append(str(item))

    for sym in clean_symbols:
        cid = to_company_id(sym)
        ticker = to_symbol(cid)
        url = f"{_BASE_URL}/api/company/fundamentals/statements/{cid}/{source_type}"

        data = None
        retries = 2
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                elif resp.status_code == 429:
                    if attempt == 0:
                        time.sleep(1.0)
                    else:
                        logger.info("StockScans statements 429 for %s; using peer valuation fallback", sym)
                        break
                else:
                    logger.debug("StockScans fundamentals status %s for %s", resp.status_code, sym)
                    break
            except Exception as exc:
                if attempt == retries - 1:
                    logger.debug("StockScans fundamentals error for %s: %s", sym, exc)
                time.sleep(0.5)

        if not data:
            # Fallback to peer valuation endpoint so valuation snapshot is captured
            if ticker not in seen_val:
                peers = fetch_stockscans_peer_valuation(cid)
                for pv in peers:
                    p_sym = pv["symbol"]
                    if p_sym not in seen_val and (p_sym == ticker or p_sym in clean_symbols):
                        valuation_rows.append(pv)
                        seen_val.add(p_sym)
            if delay_between_calls > 0:
                time.sleep(delay_between_calls)
            continue

        try:
            cur_ratios = data.get("currentRatios") or {}
            meta_ratios = data.get("metaRatios") or {}
            snap_date_str = meta_ratios.get("Price Updated At", "")[:10]
            try:
                snap_date = date.fromisoformat(snap_date_str) if snap_date_str else date.today()
            except ValueError:
                snap_date = date.today()

            # Valuation snapshot row (compatible with market_data.stock_valuation)
            val_row = {
                "_dataset": "valuation",
                "symbol": ticker,
                "snapshot_date": snap_date,
                "market_cap": float(cur_ratios.get("Market Capitalization") or 0.0),
                "trailing_pe": float(cur_ratios.get("Price To Earnings") or 0.0),
                "forward_pe": 0.0,
                "peg_ratio": 0.0,
                "price_to_book": float(cur_ratios.get("Price To Book") or 0.0),
                "price_to_sales": float(cur_ratios.get("Price To Sales") or 0.0),
                "debt_to_equity": float(cur_ratios.get("Debt To Equity") or 0.0),
                "return_on_equity": float(cur_ratios.get("ROE") or 0.0),
                "profit_margin": float(cur_ratios.get("NPM") or 0.0),
                "operating_margin": float(cur_ratios.get("OPM") or 0.0),
                "gross_margin": float(cur_ratios.get("GPM") or 0.0),
                "revenue_growth": 0.0,
                "earnings_growth": 0.0,
                "free_cashflow": 0.0,
                "beta": 0.0,
                "target_price": 0.0,
                "recommendation": "",
                "analyst_count": 0,
            }
            valuation_rows.append(val_row)
            seen_val.add(ticker)

            # Quarterly statements
            quarterly = data.get("quarterly") or []
            if len(quarterly) > 1:
                cols = quarterly[0]
                for r in quarterly[1:]:
                    r_dict = dict(zip(cols, r))
                    p_date = str(r_dict.get("Date") or "")
                    if not p_date:
                        continue
                    stmt_rows = {
                        "_dataset": "statements",
                        "symbol": ticker,
                        "statement_type": "quarterly",
                        "period_date": p_date,
                        "revenue": float(r_dict.get("Revenue") or 0.0),
                        "revenue_growth_yoy": float(r_dict.get("Revenue Growth YoY") or 0.0),
                        "operating_profit": float(r_dict.get("Operating Profit") or 0.0),
                        "opm": float(r_dict.get("OPM") or 0.0),
                        "other_income": float(r_dict.get("Other Income") or 0.0),
                        "interest": float(r_dict.get("Interest Expense") or 0.0),
                        "depreciation": float(r_dict.get("Depreciation") or 0.0),
                        "pbt": float(r_dict.get("PBT") or 0.0),
                        "tax": float(r_dict.get("Tax") or 0.0),
                        "pat": float(r_dict.get("PAT") or 0.0),
                        "pat_growth_yoy": float(r_dict.get("PAT Growth YoY") or 0.0),
                        "npm": float(r_dict.get("NPM") or 0.0),
                        "eps": float(r_dict.get("EPS") or 0.0),
                        "book_value": float(r_dict.get("Book Value") or 0.0),
                        "source": "stockscans",
                    }
                    statement_rows.append(stmt_rows)

            # Yearly statements
            yearly = data.get("yearly") or []
            if len(yearly) > 1:
                cols_y = yearly[0]
                for r in yearly[1:]:
                    r_dict = dict(zip(cols_y, r))
                    p_date = str(r_dict.get("Date") or "")
                    if not p_date:
                        continue
                    stmt_rows_y = {
                        "_dataset": "statements",
                        "symbol": ticker,
                        "statement_type": "yearly",
                        "period_date": p_date,
                        "revenue": float(r_dict.get("Revenue") or 0.0),
                        "revenue_growth_yoy": float(r_dict.get("Revenue Growth") or 0.0),
                        "operating_profit": float(r_dict.get("Operating Profit") or 0.0),
                        "opm": float(r_dict.get("OPM") or 0.0),
                        "other_income": float(r_dict.get("Other Income") or 0.0),
                        "interest": float(r_dict.get("Interest Expense") or 0.0),
                        "depreciation": float(r_dict.get("Depreciation") or 0.0),
                        "pbt": float(r_dict.get("PBT") or 0.0),
                        "tax": float(r_dict.get("Tax") or 0.0),
                        "pat": float(r_dict.get("PAT") or 0.0),
                        "pat_growth_yoy": float(r_dict.get("PAT Growth") or 0.0),
                        "npm": float(r_dict.get("NPM") or 0.0),
                        "eps": float(r_dict.get("EPS") or 0.0),
                        "book_value": float(r_dict.get("Book Value") or 0.0),
                        "source": "stockscans",
                    }
                    statement_rows.append(stmt_rows_y)

            if delay_between_calls > 0:
                time.sleep(delay_between_calls)

        except Exception as exc:
            logger.warning("Failed fetching StockScans fundamentals for %s: %s", sym, exc)

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
                "price_band_low": float(d.get("priceBandLow") or 0.0),
                "price_band_high": float(d.get("priceBandHigh") or 0.0),
                "lot_size": int(d.get("lotSize") or 0),
                "min_investment": float(d.get("minInvestment") or 0.0),
                "issue_size_cr": float(d.get("issueSizeCr") or 0.0),
                "sub_qib": float(d.get("subQib") or 0.0),
                "sub_nii": float(d.get("subNii") or 0.0),
                "sub_retail": float(d.get("subRetail") or 0.0),
                "sub_total": float(d.get("subTotal") or 0.0),
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


def fetch_stockscans_insider_trades(
    symbols: list[tuple[str, str] | str],
    delay_between_calls: float = 0.2,
) -> list[dict[str, Any]]:
    """
    Fetch Indian stock insider trading records via private StockScans API.
    Compatible with market_data.stock_insider_trades schema.
    """
    headers = get_stockscans_headers()
    rows: list[dict[str, Any]] = []

    clean_symbols: list[str] = []
    for item in symbols:
        if isinstance(item, tuple):
            clean_symbols.append(item[0])
        else:
            clean_symbols.append(str(item))

    for sym in clean_symbols:
        cid = to_company_id(sym)
        ticker = to_symbol(cid)
        url = f"{_BASE_URL}/api/company/holdings/insider-trading/{cid}"
        try:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
            if resp.status_code != 200:
                continue
            data = resp.json()
            for r in data.get("insiderTrading") or []:
                d_str = r.get("date")
                if not d_str:
                    continue
                try:
                    t_date = date.fromisoformat(str(d_str)[:10])
                except Exception:
                    continue
                rows.append({
                    "_dataset": "insider",
                    "symbol": ticker,
                    "insider_name": str(r.get("shareholderName") or ""),
                    "relation": str(r.get("shareholderCategory") or ""),
                    "transaction_date": t_date,
                    "transaction_type": str(r.get("transactionType") or ""),
                    "shares": float(r.get("shareQuantity") or 0.0),
                    "value": float(r.get("valueOfSecurities") or 0.0),  # ₹ Cr
                    "source": "stockscans",
                })
            if delay_between_calls > 0:
                time.sleep(delay_between_calls)
        except Exception as exc:
            logger.debug("Failed fetching StockScans insider trading for %s: %s", sym, exc)

    return rows


def fetch_stockscans_shareholding(symbol: str) -> dict[str, Any]:
    """
    Fetch quarterly shareholding pattern (Promoter, FII, DII, Public) and detailed entities.
    """
    headers = get_stockscans_headers()
    cid = to_company_id(symbol)
    url = f"{_BASE_URL}/api/company/holdings/shareholding/{cid}"
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
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


def fetch_stockscans_shareholding_patterns(
    symbols: list[tuple[str, str] | str],
    delay_between_calls: float = 0.2,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Fetch quarterly shareholding pattern (aggregates and major entities) for symbols.

    Returns
    -------
    tuple of (pattern_rows, entity_rows)
    """
    pattern_rows: list[dict[str, Any]] = []
    entity_rows: list[dict[str, Any]] = []

    clean_symbols: list[str] = []
    for item in symbols:
        if isinstance(item, tuple):
            clean_symbols.append(item[0])
        else:
            clean_symbols.append(str(item))

    for sym in clean_symbols:
        cid = to_company_id(sym)
        ticker = to_symbol(cid)
        data = fetch_stockscans_shareholding(cid)
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
                    "promoter_pct": float(metrics.get("promoter") or 0.0),
                    "fii_pct": float(metrics.get("fii") or 0.0),
                    "dii_pct": float(metrics.get("dii") or 0.0),
                    "government_pct": float(metrics.get("government") or 0.0),
                    "public_pct": float(metrics.get("public") or 0.0),
                    "no_of_shareholders": int(metrics.get("no of shareholders") or 0),
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
                        try:
                            fval = float(val)
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
                        except (ValueError, TypeError):
                            pass

        if delay_between_calls > 0:
            time.sleep(delay_between_calls)

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
