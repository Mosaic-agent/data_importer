"""
src/tools/shoonya_tools.py
───────────────────────────
Provides tools to fetch live stock/ETF prices and details from Shoonya API
and WebSocket feeds. Exposes:
  - get_shoonya_quotes: fetch live quotes from REST API
  - get_shoonya_live_tick: fetch a live tick from WebSocket feed

Ensures all data (including errors and timeouts) is returned in valid JSON format.
"""

import json
import logging
import threading
from typing import Any
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

def _resolve_token(api, symbol: str) -> tuple[str, str] | None:
    """Resolve symbol to exchange token and trading symbol (e.g. 'GOLDBEES' -> ('14428', 'GOLDBEES-EQ'))."""
    clean_sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    try:
        search_res = api.searchscrip(exchange="NSE", searchtext=clean_sym)
        if not search_res or search_res.get("stat") != "Ok" or not search_res.get("values"):
            return None
            
        token = None
        tsym = None
        target_tsym = f"{clean_sym}-EQ"
        for val in search_res["values"]:
            if val.get("tsym") == target_tsym or str(val.get("symname", "")).upper() == clean_sym:
                token = val.get("token")
                tsym = val.get("tsym")
                break
                
        if not token:
            return None
            
        return token, tsym
    except Exception as e:
        logger.debug("Shoonya symbol resolution error for %s: %s", symbol, e)
        return None


# Public alias for cross-module use
resolve_token = _resolve_token


@tool
def get_shoonya_quotes(symbol: str) -> str:
    """
    Fetch the latest live quotes (LTP, open, high, low, close, volume, bid/ask depth)
    for any Indian NSE stock or ETF from the Shoonya REST API.

    Use this tool when you need real-time quotes or order book depth during market hours.
    
    Args:
        symbol: The stock or ETF symbol (e.g., 'GOLDBEES', 'RELIANCE', 'TCS').
    """
    from src.data_importer.fetchers.shoonya_fetcher import get_shoonya_api
    api = get_shoonya_api()
    if not api:
        return json.dumps({
            "status": "error",
            "message": "Shoonya API is not authenticated or credentials not configured."
        }, indent=2)
        
    res = _resolve_token(api, symbol)
    if not res:
        return json.dumps({
            "status": "error",
            "message": f"Could not find NSE token for symbol '{symbol}'."
        }, indent=2)
        
    token, tsym = res
    try:
        quote = api.get_quotes(exchange="NSE", token=token)
        if not quote or quote.get("stat") != "Ok":
            return json.dumps({
                "status": "error",
                "message": f"Error fetching quotes from Shoonya: {quote}"
            }, indent=2)
        # Persist the quote to ClickHouse
        _save_quote_to_clickhouse(quote)
        return json.dumps(quote, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Error occurred fetching quotes from Shoonya REST API: {e}"
        }, indent=2)


@tool
def get_shoonya_live_tick(symbol: str) -> str:
    """
    Fetch a real-time live tick directly from the Shoonya WebSocket broadcast.
    This connects to the websocket feed, subscribes to the instrument, captures the
    next broadcasted tick (price and volume), and cleanly disconnects.

    Use this tool when you need the absolute latest live traded price or volume broadcast
    from the exchange during active market hours.

    Args:
        symbol: The stock or ETF symbol (e.g., 'GOLDBEES', 'RELIANCE').
    """
    from src.data_importer.fetchers.shoonya_fetcher import get_shoonya_api
    api = get_shoonya_api()
    if not api:
        return json.dumps({
            "status": "error",
            "message": "Shoonya API is not authenticated or credentials not configured."
        }, indent=2)
        
    res = _resolve_token(api, symbol)
    if not res:
        return json.dumps({
            "status": "error",
            "message": f"Could not find NSE token for symbol '{symbol}'."
        }, indent=2)
        
    token, tsym = res
    
    tick_captured = {}
    tick_event = threading.Event()
    
    def on_feed_update(tick_data):
        if tick_data and (tick_data.get("t") == "tk" or tick_data.get("t") == "tf"):
            tick_captured.update(tick_data)
            tick_event.set()
            
    def on_open():
        api.subscribe([f"NSE|{token}"])
        
    try:
        api.start_websocket(
            order_update_callback=lambda x: None,
            subscribe_callback=on_feed_update,
            socket_open_callback=on_open
        )
        
        # Wait up to 5 seconds for a tick
        success = tick_event.wait(timeout=5.0)
        
        # Clean up connection
        try:
            api.close_websocket()
        except Exception:
            pass
            
        if success:
            # Add status success envelope
            tick_captured["status"] = "success"
            return json.dumps(tick_captured, indent=2)
        else:
            return json.dumps({
                "status": "timeout",
                "message": f"Timeout reached. No live tick broadcasted for {tsym} within 5.0 seconds. (Is the market open and active?)"
            }, indent=2)
            
    except Exception as e:
        # Guarantee cleanup
        try:
            api.close_websocket()
        except Exception:
            pass
        return json.dumps({
            "status": "error",
            "message": f"Error occurred during Shoonya WebSocket live tick capture: {e}"
        }, indent=2)

@tool
def initiate_shoonya_login(_: str = "") -> str:
    """
    Initiate Shoonya authentication by generating the browser authorization URL.
    Returns the URL that the user must open in their browser to log in.
    """
    from config.settings import settings
    user = getattr(settings, "shoonya_user_id", "")
    secret = getattr(settings, "shoonya_api_secret", "")
    
    if not user or not secret:
        return "Error: Shoonya credentials (SHOONYA_USER_ID, SHOONYA_API_SECRET) are not set in your .env file."

    login_url = f"https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key={user}&route_to={user}"
    return f"Please open this URL in your browser to authenticate with Shoonya:\n{login_url}\n\nAfter logging in and authorizing, copy the 'code' parameter from the redirect URL and run the complete_shoonya_login(code=...) tool."


@tool
def complete_shoonya_login(code: str) -> str:
    """
    Complete the Shoonya authentication flow by exchanging the browser authorization code
    for session tokens.
    
    Args:
        code: The auth code copied from the redirect URL after browser login.
    """
    from config.settings import settings
    from src.data_importer.fetchers.shoonya_fetcher import _save_session
    
    user = getattr(settings, "shoonya_user_id", "")
    secret = getattr(settings, "shoonya_api_secret", "")
    
    if not user or not secret:
        return "Error: Shoonya credentials (SHOONYA_USER_ID, SHOONYA_API_SECRET) are not set in your .env file."
        
    try:
        from NorenRestApiPy.NorenApi import NorenApi
        class ShoonyaApiPy(NorenApi):
            def __init__(self):
                NorenApi.__init__(
                    self,
                    host="https://api.shoonya.com/NorenWClientAPI",
                    websocket="wss://api.shoonya.com/NorenWSAPI/",
                )
    except ImportError:
        return "Error: NorenRestApiPy is not installed."

    api = ShoonyaApiPy()
    user_clean = user.replace("_U", "")
    try:
        result = api.getAccessToken(
            authcode=code,
            Secret_Code=secret,
            client_id=user,
            UID=user_clean
        )
        if result is not None:
            asc_tok, usrid, ref_tok, actid = result
            susertoken = getattr(api, '_NorenApi__susertoken', asc_tok)
            _save_session({
                "susertoken": susertoken,
                "access_token": asc_tok,
                "userid": usrid,
                "accountid": actid
            })
            return f"Success! Authenticated successfully as {usrid}. The session token has been cached and saved to ClickHouse."
        else:
            return "Error: OAuth token generation failed. The code might be expired or invalid."
    except Exception as exc:
        return f"Error during OAuth token exchange: {exc}"



def calculate_order_book_imbalance(quote_or_row: dict[str, Any]) -> dict[str, Any]:
    """
    Compute Order Book Imbalance (OBI), 5-level queue depth, bid-ask spread,
    VWAP deviation, and circuit headroom from a Shoonya quote or ClickHouse live_quotes record.
    """
    sym = quote_or_row.get("symname") or quote_or_row.get("symbol") or ""
    tsym = quote_or_row.get("tsym") or sym

    # 1. Prices & Volume
    lp = float(quote_or_row.get("lp") or quote_or_row.get("last_price") or 0.0)
    prev_c = float(quote_or_row.get("c") or quote_or_row.get("prev_close") or 0.0)
    open_p = float(quote_or_row.get("o") or quote_or_row.get("open") or 0.0)
    high_p = float(quote_or_row.get("h") or quote_or_row.get("high") or 0.0)
    low_p = float(quote_or_row.get("l") or quote_or_row.get("low") or 0.0)
    vwap = float(quote_or_row.get("ap") or quote_or_row.get("avg_price") or 0.0)
    vol = int(quote_or_row.get("v") or quote_or_row.get("volume") or 0)

    chg = round(lp - prev_c, 2) if (lp and prev_c) else 0.0
    chg_pct = round((chg / prev_c) * 100, 2) if prev_c else 0.0

    vwap_dev = round(lp - vwap, 2) if (lp and vwap) else 0.0
    vwap_dev_pct = round(((lp - vwap) / vwap) * 100, 2) if vwap else 0.0

    # 2. Circuits
    uc = float(quote_or_row.get("uc") or quote_or_row.get("upper_circuit") or 0.0)
    lc = float(quote_or_row.get("lc") or quote_or_row.get("lower_circuit") or 0.0)
    headroom_uc = round(((uc - lp) / lp) * 100, 2) if (uc > 0 and lp > 0) else None
    headroom_lc = round(((lp - lc) / lp) * 100, 2) if (lc > 0 and lp > 0) else None

    # 3. Total Quantities
    tbq = int(quote_or_row.get("tbq") or quote_or_row.get("total_buy_qty") or 0)
    tsq = int(quote_or_row.get("tsq") or quote_or_row.get("total_sell_qty") or 0)
    tot_q = tbq + tsq
    total_obi = round(((tbq - tsq) / tot_q) * 100, 2) if tot_q > 0 else 0.0
    buy_share = round((tbq / tot_q) * 100, 2) if tot_q > 0 else 50.0
    sell_share = round((tsq / tot_q) * 100, 2) if tot_q > 0 else 50.0

    # 4. 5-Level Depth Parsing
    bids = []
    asks = []

    # If passed as lists (ClickHouse format)
    if "bid_prices" in quote_or_row and isinstance(quote_or_row["bid_prices"], (list, tuple)):
        bp_list = quote_or_row.get("bid_prices") or []
        bq_list = quote_or_row.get("bid_quantities") or []
        bo_list = quote_or_row.get("bid_orders") or []
        for i in range(min(5, len(bp_list))):
            bids.append({
                "level": i + 1,
                "price": float(bp_list[i]) if i < len(bp_list) else 0.0,
                "qty": int(bq_list[i]) if i < len(bq_list) else 0,
                "orders": int(bo_list[i]) if i < len(bo_list) else 0,
            })

        sp_list = quote_or_row.get("ask_prices") or []
        sq_list = quote_or_row.get("ask_quantities") or []
        so_list = quote_or_row.get("ask_orders") or []
        for i in range(min(5, len(sp_list))):
            asks.append({
                "level": i + 1,
                "price": float(sp_list[i]) if i < len(sp_list) else 0.0,
                "qty": int(sq_list[i]) if i < len(sq_list) else 0,
                "orders": int(so_list[i]) if i < len(so_list) else 0,
            })
    else:
        # Shoonya REST dict format
        for i in range(1, 6):
            bids.append({
                "level": i,
                "price": float(quote_or_row.get(f"bp{i}") or 0.0),
                "qty": int(quote_or_row.get(f"bq{i}") or 0),
                "orders": int(quote_or_row.get(f"bo{i}") or 0),
            })
            asks.append({
                "level": i,
                "price": float(quote_or_row.get(f"sp{i}") or 0.0),
                "qty": int(quote_or_row.get(f"sq{i}") or 0),
                "orders": int(quote_or_row.get(f"so{i}") or 0),
            })

    d5_buy_qty = sum(b["qty"] for b in bids)
    d5_sell_qty = sum(a["qty"] for a in asks)
    d5_tot = d5_buy_qty + d5_sell_qty
    depth5_obi = round(((d5_buy_qty - d5_sell_qty) / d5_tot) * 100, 2) if d5_tot > 0 else 0.0

    # 5. Spread
    best_bid = bids[0]["price"] if bids else 0.0
    best_ask = asks[0]["price"] if asks else 0.0
    spread = None
    spread_pct = None
    spread_bps = None
    if best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
        spread = round(best_ask - best_bid, 2)
        spread_pct = round((spread / best_bid) * 100, 3)
        spread_bps = round(spread_pct * 100, 1)

    # 6. Regime Classification
    primary_obi = depth5_obi if d5_tot > 0 else total_obi

    if headroom_uc is not None and 0.0 <= headroom_uc <= 0.2:
        regime = "UPPER_CIRCUIT_LOCKED"
        bias = "EXTREME_BULLISH"
        interpretation = f"Locked at / near Upper Circuit (headroom {headroom_uc:.2f}%). Sellers completely absent."
    elif headroom_lc is not None and 0.0 <= headroom_lc <= 0.2:
        regime = "LOWER_CIRCUIT_LOCKED"
        bias = "EXTREME_BEARISH"
        interpretation = f"Locked at / near Lower Circuit (headroom {headroom_lc:.2f}%). Buyers completely absent."
    elif tot_q == 0 and d5_tot == 0:
        regime = "INACTIVE_OR_POST_MARKET"
        bias = "NEUTRAL"
        interpretation = "Market closed or order queues empty. No active bid/ask imbalance."
    elif primary_obi >= 40.0:
        regime = "STRONG_BID_SQUEEZE"
        bias = "BULLISH_ABSORPTION"
        interpretation = f"Severe buy queue dominance (+{primary_obi:.1f}% OBI). Strong institutional bid absorption / supply depletion."
    elif primary_obi >= 15.0:
        regime = "MODERATE_BUY_PRESSURE"
        bias = "MILD_ACCUMULATION"
        interpretation = f"Net accumulation bias (+{primary_obi:.1f}% OBI). Bids outpace offers across depth queues."
    elif primary_obi <= -40.0:
        regime = "STRONG_ASK_OVERHANG"
        bias = "BEARISH_DISTRIBUTION"
        interpretation = f"Severe sell queue overhang ({primary_obi:.1f}% OBI). Strong institutional supply / distribution wall."
    elif primary_obi <= -15.0:
        regime = "MODERATE_SELL_PRESSURE"
        bias = "MILD_DISTRIBUTION"
        interpretation = f"Net distribution bias ({primary_obi:.1f}% OBI). Offers outnumber bids across depth queues."
    else:
        regime = "EQUILIBRIUM_TWO_WAY"
        bias = "BALANCED"
        interpretation = f"Balanced two-way liquidity ({primary_obi:+.1f}% OBI). Active two-way market making."

    return {
        "symbol": sym,
        "tsym": tsym,
        "last_price": lp,
        "prev_close": prev_c,
        "change": chg,
        "change_pct": chg_pct,
        "open": open_p,
        "high": high_p,
        "low": low_p,
        "vwap": vwap,
        "vwap_deviation": vwap_dev,
        "vwap_deviation_pct": vwap_dev_pct,
        "volume": vol,
        "upper_circuit": uc,
        "lower_circuit": lc,
        "headroom_uc_pct": headroom_uc,
        "headroom_lc_pct": headroom_lc,
        "total_buy_qty": tbq,
        "total_sell_qty": tsq,
        "total_queue_qty": tot_q,
        "buy_share_pct": buy_share,
        "sell_share_pct": sell_share,
        "total_obi_pct": total_obi,
        "depth5_buy_qty": d5_buy_qty,
        "depth5_sell_qty": d5_sell_qty,
        "depth5_queue_qty": d5_tot,
        "depth5_obi_pct": depth5_obi,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "spread_bps": spread_bps,
        "regime": regime,
        "bias": bias,
        "interpretation": interpretation,
        "bids": bids,
        "asks": asks,
    }


def format_order_book_ascii(data: dict[str, Any]) -> str:
    """Format Order Book Imbalance and depth as a top-down Unicode box table."""
    def _pad(t: str, w: int) -> str:
        return t[:w-3] + "..." if len(t) > w else t + " " * (w - len(t))

    w_l, w_r = 42, 43
    w_full = w_l + w_r + 1  # 86

    sym = data.get("symbol") or data.get("tsym") or "UNKNOWN"
    tsym = data.get("tsym") or sym
    lp = data.get("last_price", 0.0)
    chg = data.get("change", 0.0)
    chg_pct = data.get("change_pct", 0.0)
    vwap = data.get("vwap", 0.0)
    vwap_dev_pct = data.get("vwap_deviation_pct", 0.0)
    low_p = data.get("low", 0.0)
    high_p = data.get("high", 0.0)
    vol = data.get("volume", 0)
    uc = data.get("upper_circuit", 0.0)
    lc = data.get("lower_circuit", 0.0)
    h_uc = data.get("headroom_uc_pct")
    h_lc = data.get("headroom_lc_pct")
    tbq = data.get("total_buy_qty", 0)
    tsq = data.get("total_sell_qty", 0)
    tot_obi = data.get("total_obi_pct", 0.0)
    d5_obi = data.get("depth5_obi_pct", 0.0)
    buy_share = data.get("buy_share_pct", 50.0)
    sell_share = data.get("sell_share_pct", 50.0)
    spread = data.get("spread")
    spread_bps = data.get("spread_bps")
    regime = data.get("regime", "UNKNOWN")
    interpretation = data.get("interpretation", "")

    uc_str = f"+{h_uc:.1f}%" if h_uc is not None else "N/A"
    lc_str = f"-{h_lc:.1f}%" if h_lc is not None else "N/A"
    spread_str = f"₹{spread:.2f} ({spread_bps:.1f} bps)" if spread is not None else "N/A"

    bids = data.get("bids", [])
    asks = data.get("asks", [])

    lines = [
        "┌" + "─" * w_full + "┐",
        "│" + _pad(f" 🏛️ ORDER BOOK MICROSTRUCTURE & QUEUE DEPTH: {sym} ({tsym})", w_full) + "│",
        "├" + "─" * w_l + "┬" + "─" * w_r + "┤",
        "│" + _pad(f" Last Price : ₹{lp:,.2f} ({chg:+.2f} / {chg_pct:+.2f}%)", w_l) + "│" + _pad(f" VWAP (AP)  : ₹{vwap:,.2f} ({vwap_dev_pct:+.2f}%)", w_r) + "│",
        "│" + _pad(f" Day Range  : ₹{low_p:,.2f} – ₹{high_p:,.2f}", w_l) + "│" + _pad(f" Volume     : {vol:,} ({vol/1e6:.2f}M)", w_r) + "│",
        "│" + _pad(f" Upper Lmt  : ₹{uc:,.2f} ({uc_str})", w_l) + "│" + _pad(f" Lower Lmt  : ₹{lc:,.2f} ({lc_str})", w_r) + "│",
        "├" + "─" * w_l + "┼" + "─" * w_r + "┤",
        "│" + _pad(f" Total Buy  : {tbq:,} ({buy_share:.1f}%)", w_l) + "│" + _pad(f" Total Sell : {tsq:,} ({sell_share:.1f}%)", w_r) + "│",
        "│" + _pad(f" Total OBI  : {tot_obi:+.2f}%", w_l) + "│" + _pad(f" Depth5 OBI : {d5_obi:+.2f}%", w_r) + "│",
        "│" + _pad(f" Spread     : {spread_str}", w_l) + "│" + _pad(f" Regime     : {regime}", w_r) + "│",
        "├" + "─" * w_l + "┼" + "─" * w_r + "┤",
        "│" + f"  {'Orders':^6}  {'Bid Qty':^14}  {'Bid Price':^14}  " + "│" + f"  {'Ask Price':^14}  {'Ask Qty':^14}  {'Orders':^6}   " + "│",
        "├" + "─" * w_l + "┼" + "─" * w_r + "┤",
    ]

    for i in range(5):
        b = bids[i] if i < len(bids) else {"orders": 0, "qty": 0, "price": 0.0}
        a = asks[i] if i < len(asks) else {"orders": 0, "qty": 0, "price": 0.0}

        b_ord = f"{b['orders']}" if b["orders"] > 0 else "-"
        b_qty = f"{b['qty']:,}" if b["qty"] > 0 else "-"
        b_prc = f"₹{b['price']:,.2f}" if b["price"] > 0 else "-"

        a_prc = f"₹{a['price']:,.2f}" if a["price"] > 0 else "-"
        a_qty = f"{a['qty']:,}" if a["qty"] > 0 else "-"
        a_ord = f"{a['orders']}" if a["orders"] > 0 else "-"

        row_l = f"  {b_ord:^6}  {b_qty:>14}  {b_prc:>14}  "
        row_r = f"  {a_prc:>14}  {a_qty:>14}  {a_ord:^6}   "
        lines.append(f"│{row_l}│{row_r}│")

    lines.append("├" + "─" * w_full + "┤")
    lines.append("│" + _pad(f" 🎯 Microstructure Signal: {interpretation}", w_full) + "│")
    lines.append("└" + "─" * w_full + "┘")
    return "\n".join(lines)



def fetch_and_calculate_obi(symbol: str) -> dict[str, Any] | None:
    """
    Fetch live or latest quote for symbol and calculate Order Book Imbalance.
    First checks Shoonya REST API; falls back to ClickHouse market_data.live_quotes.
    """
    from src.data_importer.fetchers.shoonya_fetcher import get_shoonya_api
    api = get_shoonya_api()
    clean_sym = symbol.strip().upper().replace(".NS", "").replace(".BO", "")

    quote = None
    if api:
        res = _resolve_token(api, clean_sym)
        if res:
            token, tsym = res
            try:
                raw_quote = api.get_quotes(exchange="NSE", token=token)
                if raw_quote and raw_quote.get("stat") == "Ok":
                    quote = raw_quote
                    _save_quote_to_clickhouse(quote)
            except Exception as e:
                logger.debug("Error fetching quote for %s from Shoonya: %s", clean_sym, e)

    if not quote:
        # Fallback to ClickHouse market_data.live_quotes
        try:
            from src.db.pool import get_pool
            pool = get_pool()
            ch_df = pool.query_df(f"""
                SELECT *
                FROM market_data.live_quotes FINAL
                WHERE symbol = '{clean_sym}'
                ORDER BY imported_at DESC
                LIMIT 1
            """)
            if not ch_df.empty:
                quote = ch_df.iloc[0].to_dict()
        except Exception as e:
            logger.debug("Error querying ClickHouse live_quotes for %s: %s", clean_sym, e)

    if not quote:
        return None

    return calculate_order_book_imbalance(quote)


@tool
def get_order_book_imbalance(symbol: str) -> str:
    """
    Fetch and compute the real-time Order Book Imbalance (OBI) and 5-level market depth
    for any Indian NSE stock or ETF using Shoonya market microstructure feeds.

    Returns the buy/sell queue ratio, Depth-5 OBI %, bid-ask spread (bps), VWAP deviation,
    circuit headroom, and institutional supply/demand regime classification, along with
    a formatted terminal ASCII depth grid.

    Args:
        symbol: The stock or ETF symbol (e.g., 'RELIANCE', 'GOLDBEES', 'TCS', 'HDFCBANK').
    """
    obi_data = fetch_and_calculate_obi(symbol)
    if not obi_data:
        return json.dumps({
            "status": "error",
            "message": f"Unable to fetch order book depth or quotes for symbol '{symbol}'. Verify Shoonya connection or symbol name."
        }, indent=2)

    ascii_grid = format_order_book_ascii(obi_data)

    return json.dumps({
        "status": "success",
        "symbol": obi_data["symbol"],
        "last_price": obi_data["last_price"],
        "change_pct": obi_data["change_pct"],
        "vwap": obi_data["vwap"],
        "vwap_deviation_pct": obi_data["vwap_deviation_pct"],
        "total_obi_pct": obi_data["total_obi_pct"],
        "depth5_obi_pct": obi_data["depth5_obi_pct"],
        "buy_share_pct": obi_data["buy_share_pct"],
        "sell_share_pct": obi_data["sell_share_pct"],
        "spread": obi_data["spread"],
        "spread_bps": obi_data["spread_bps"],
        "regime": obi_data["regime"],
        "bias": obi_data["bias"],
        "interpretation": obi_data["interpretation"],
        "circuit_headroom": {
            "upper_circuit": obi_data["upper_circuit"],
            "headroom_uc_pct": obi_data["headroom_uc_pct"],
            "lower_circuit": obi_data["lower_circuit"],
            "headroom_lc_pct": obi_data["headroom_lc_pct"]
        },
        "ascii_order_book": ascii_grid
    }, indent=2)


SHOONYA_TOOLS = [
    get_shoonya_quotes,
    get_shoonya_live_tick,
    get_order_book_imbalance,
    initiate_shoonya_login,
    complete_shoonya_login,
]



def _save_quote_to_clickhouse(quote: dict[str, Any]) -> None:
    """Parse Shoonya API quote response and save it to market_data.live_quotes in ClickHouse."""
    try:
        from datetime import datetime
        from src.data_importer.clickhouse import ClickHouseImporter
        
        # Extract bid-ask arrays
        bid_prices = []
        bid_quantities = []
        bid_orders = []
        ask_prices = []
        ask_quantities = []
        ask_orders = []
        
        for i in range(1, 6):
            bid_prices.append(float(quote.get(f"bp{i}", 0.0)))
            bid_quantities.append(int(quote.get(f"bq{i}", 0)))
            bid_orders.append(int(quote.get(f"bo{i}", 0)))
            
            ask_prices.append(float(quote.get(f"sp{i}", 0.0)))
            ask_quantities.append(int(quote.get(f"sq{i}", 0)))
            ask_orders.append(int(quote.get(f"so{i}", 0)))

        # Parse trade date/time
        try:
            if quote.get("lut"):
                last_trade_time = datetime.fromtimestamp(int(quote["lut"]))
            elif quote.get("ltd") and quote.get("ltt"):
                last_trade_time = datetime.strptime(f"{quote['ltd']} {quote['ltt']}", "%d-%m-%Y %H:%M:%S")
            else:
                last_trade_time = datetime.now()
        except Exception:
            last_trade_time = datetime.now()

        parsed = {
            "symbol": quote.get("symname", ""),
            "exchange": quote.get("exch", ""),
            "company_name": quote.get("cname", ""),
            "isin": quote.get("isin", ""),
            "last_trade_time": last_trade_time,
            "last_price": float(quote.get("lp", 0.0)),
            "prev_close": float(quote.get("c", 0.0)),
            "open": float(quote.get("o", 0.0)),
            "high": float(quote.get("h", 0.0)),
            "low": float(quote.get("l", 0.0)),
            "avg_price": float(quote.get("ap", 0.0)),
            "volume": int(quote.get("v", 0)),
            "last_traded_qty": int(quote.get("ltq", 0)),
            
            "upper_circuit": float(quote.get("uc", 0.0)),
            "lower_circuit": float(quote.get("lc", 0.0)),
            "week52_high": float(quote.get("wk52_h", 0.0)),
            "week52_low": float(quote.get("wk52_l", 0.0)),
            "issued_capital": float(quote.get("issuecap", 0.0)),
            "total_buy_qty": int(quote.get("tbq", 0)),
            "total_sell_qty": int(quote.get("tsq", 0)),
            
            "bid_prices": bid_prices,
            "bid_quantities": bid_quantities,
            "bid_orders": bid_orders,
            "ask_prices": ask_prices,
            "ask_quantities": ask_quantities,
            "ask_orders": ask_orders,
        }
        
        with ClickHouseImporter() as ch:
            ch.insert_live_quotes([parsed])
    except Exception as e:
        logger.warning("Failed to save Shoonya quote to ClickHouse: %s", e)
