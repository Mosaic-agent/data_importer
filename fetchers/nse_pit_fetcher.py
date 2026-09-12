"""
src/data_importer/fetchers/nse_pit_fetcher.py
──────────────────────────────────────────────
Fetch insider transaction history for NSE-listed stocks via NSE's
Prohibition of Insider Trading (SEBI PIT Regulations) disclosure feed.

yfinance's `Ticker.insider_transactions` is built on US SEC Form 4 data and
never returns anything for `.NS` symbols; NSE's own disclosure API is the
correct source for Indian insider trades.

Endpoint:
    https://www.nseindia.com/api/corporates-pit?index=equities&symbol=<SYMBOL>

Same warm-up/session pattern as nse_corporate_actions_fetcher.py — NSE
blocks cookie-less requests, so a homepage GET is required before the API
call.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_NSE_PIT_URL = "https://www.nseindia.com/api/corporates-pit"
_NSE_WARMUP  = "https://www.nseindia.com/"
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}
_TIMEOUT = 15


def _parse_date(s: str) -> date | None:
    """Parse NSE date strings: '28-Nov-2025', '28-Nov-2025 22:47'."""
    if not s or s.strip() in ("", "-"):
        return None
    s = s.strip().split(" ")[0]
    try:
        return datetime.strptime(s, "%d-%b-%Y").date()
    except ValueError:
        logger.debug("Could not parse NSE PIT date string: %r", s)
        return None


def _fetch_symbol(client: httpx.Client, nse_symbol: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    resp = client.get(
        _NSE_PIT_URL,
        params={"index": "equities", "symbol": nse_symbol},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return rows

    for item in data:
        tdate = _parse_date(str(item.get("intimDt") or item.get("acqtoDt") or ""))
        if tdate is None:
            continue

        # secAcq/secVal is the total securities transacted (present for all
        # disclosure types); buyQuantity/sellquantity/buyValue/sellValue are
        # only populated for on-market trades and are 0 for off-market
        # transfers, ESOP exercises, etc. — prefer secAcq/secVal.
        def _num(key: str) -> float:
            try:
                return float(str(item.get(key) or 0).replace(",", ""))
            except ValueError:
                return 0.0

        buy_qty  = _num("buyQuantity")
        sell_qty = _num("sellquantity")
        shares   = _num("secAcq") or (buy_qty if buy_qty else sell_qty)
        value    = _num("secVal") or (_num("buyValue") if buy_qty else _num("sellValue"))
        txn_type = str(item.get("tdpTransactionType") or "").strip()

        rows.append({
            "symbol":            nse_symbol,
            "insider_name":      str(item.get("acqName") or "").strip(),
            "relation":          str(item.get("personCategory") or "").strip(),
            "transaction_date":  tdate,
            "transaction_type":  txn_type,
            "shares":            shares,
            "value":             value,
        })

    return rows


def fetch_insider_trades(symbols: list[tuple[str, str]]) -> list[dict]:
    """
    Fetch insider buy/sell transactions for the given NSE-listed symbols.

    Parameters
    ----------
    symbols : list of (nse_symbol, yahoo_ticker) — only nse_symbol is used.

    Returns
    -------
    list of dicts with keys:
        symbol, insider_name, relation, transaction_date,
        transaction_type, shares, value
    """
    rows: list[dict] = []

    try:
        with httpx.Client(
            headers=_NSE_HEADERS,
            follow_redirects=True,
            timeout=_TIMEOUT,
        ) as client:
            client.get(_NSE_WARMUP, timeout=10)
            time.sleep(0.8)

            for nse_symbol, _yahoo in symbols:
                try:
                    symbol_rows = _fetch_symbol(client, nse_symbol.strip().upper())
                    if not symbol_rows:
                        logger.warning("No insider transaction data for %s", nse_symbol)
                        continue
                    rows.extend(symbol_rows)
                    logger.info(
                        "Fetched %d insider records for %s", len(symbol_rows), nse_symbol
                    )
                except Exception as e:
                    logger.error("Error fetching insider trades for %s: %s", nse_symbol, e)
                time.sleep(0.4)

    except Exception as e:
        logger.error("NSE PIT session setup failed: %s", e)

    return rows
