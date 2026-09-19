"""
src/data_importer/portfolio_sync.py
─────────────────────────────────────
Delta-sync service for Zerodha portfolio holdings.

Fetches live holdings via Kite MCP, diffs them against the last known
snapshot in ClickHouse `user_holdings`, and:
  - always refreshes `user_holdings` (current-state snapshot — prices move
    daily even when nothing structurally changed)
  - writes one `portfolio_holding_events` row per *structural* change
    (opened / increased / decreased / avg-price-changed / closed) — this
    sparse audit log is both the "what changed since last sync" report and
    the source for holding-period math (first OPENED not yet followed by a
    more recent CLOSED, per symbol).

Bypasses the `Fetcher`/`run_fetcher()` watermark framework on purpose: that
plumbing is date-range shaped (OHLCV-style), whereas a holdings sync is a
full point-in-time snapshot with no natural "from_date"/"to_date".
"""

from __future__ import annotations

import logging
from typing import Any

from src.clients.mcp_client import KiteMCPClient, fetch_authenticated_profile
from src.data_importer.clickhouse import ClickHouseImporter
from src.data_importer.tool_fetchers.zerodha_mcp_tools import _parse_holdings
from src.db.pool import query_df
from src.events.bus import DataImportedEvent, get_event_bus
from src.models.portfolio import Holding

logger = logging.getLogger(__name__)

# Ignore float noise, not real changes.
_QTY_EPS = 1e-6
_PRICE_EPS = 0.01


def _event(
    h: Holding, event_type: str, previous_quantity: float, previous_average_price: float
) -> dict[str, Any]:
    return {
        "tradingsymbol": h.tradingsymbol,
        "isin": h.isin,
        "event_type": event_type,
        "quantity": h.quantity,
        "average_price": h.average_price,
        "previous_quantity": previous_quantity,
        "previous_average_price": previous_average_price,
    }


async def sync_portfolio_holdings() -> dict[str, Any]:
    """
    Sync live Kite holdings into ClickHouse and detect what changed since
    the last sync.

    Returns
    -------
    dict with keys: opened, increased, decreased, avg_price_changed, closed
    (each a list of tradingsymbols) and unchanged_count (int).
    """
    async with KiteMCPClient() as client:
        await fetch_authenticated_profile(client)
        raw = await client.get_holdings()
    live = _parse_holdings(raw)

    ch = ClickHouseImporter()
    ch.ensure_schema()

    known_df = query_df(
        "SELECT tradingsymbol, isin, exchange, quantity, average_price "
        "FROM market_data.user_holdings FINAL WHERE status = 'OPEN'"
    )
    known = (
        {row["tradingsymbol"]: row for _, row in known_df.iterrows()}
        if not known_df.empty
        else {}
    )

    live_symbols = {h.tradingsymbol for h in live}
    events: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "opened": [], "increased": [], "decreased": [],
        "avg_price_changed": [], "closed": [], "unchanged_count": 0,
    }

    for h in live:
        prev = known.get(h.tradingsymbol)
        if prev is None:
            events.append(_event(h, "OPENED", 0.0, 0.0))
            summary["opened"].append(h.tradingsymbol)
            continue

        qty_changed = abs(h.quantity - prev["quantity"]) > _QTY_EPS
        price_changed = abs(h.average_price - prev["average_price"]) > _PRICE_EPS
        if qty_changed:
            event_type = "INCREASED" if h.quantity > prev["quantity"] else "DECREASED"
            events.append(_event(h, event_type, prev["quantity"], prev["average_price"]))
            summary[event_type.lower()].append(h.tradingsymbol)
        elif price_changed:
            # Quantity unchanged but average cost moved — usually a corporate
            # action adjustment, not a routine trade. Worth flagging distinctly.
            events.append(_event(h, "AVG_PRICE_CHANGED", prev["quantity"], prev["average_price"]))
            summary["avg_price_changed"].append(h.tradingsymbol)
        else:
            summary["unchanged_count"] += 1

    closed_rows: list[dict[str, Any]] = []
    for symbol, prev in known.items():
        if symbol in live_symbols:
            continue
        events.append({
            "tradingsymbol": symbol,
            "isin": prev["isin"],
            "event_type": "CLOSED",
            "quantity": 0.0,
            "average_price": prev["average_price"],
            "previous_quantity": prev["quantity"],
            "previous_average_price": prev["average_price"],
        })
        summary["closed"].append(symbol)
        closed_rows.append({
            "tradingsymbol": symbol,
            "exchange": prev["exchange"],
            "isin": prev["isin"],
            "quantity": 0.0,
            "average_price": prev["average_price"],
            "last_price": 0.0,
            "pnl": 0.0,
            "day_change": 0.0,
            "day_change_percentage": 0.0,
            "status": "CLOSED",
        })

    # Always refresh the current-state snapshot for every live holding —
    # prices move daily even when nothing structurally changed.
    snapshot_rows = [{**h.model_dump(), "status": "OPEN"} for h in live]
    if snapshot_rows or closed_rows:
        ch.insert_user_holdings(snapshot_rows + closed_rows)
    if events:
        ch.insert_portfolio_holding_events(events)
        get_event_bus().publish(DataImportedEvent(
            source="kite_holdings",
            category="holdings",
            symbol_key="PORTFOLIO",
            n_rows=len(live),
        ))

    logger.info(
        "Portfolio sync: %d opened, %d increased, %d decreased, %d avg-price-changed, "
        "%d closed, %d unchanged",
        len(summary["opened"]), len(summary["increased"]), len(summary["decreased"]),
        len(summary["avg_price_changed"]), len(summary["closed"]), summary["unchanged_count"],
    )
    return summary
