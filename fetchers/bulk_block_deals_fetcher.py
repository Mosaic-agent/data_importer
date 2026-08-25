"""
src/data_importer/fetchers/bulk_block_deals_fetcher.py
─────────────────────────────────────────────────────
Fetches and cleans NSE Bulk and Block Deals data using nselib and direct NSE API fallback.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
import pandas as pd
import numpy as np

log = logging.getLogger(__name__)

def _parse_date(val: Any) -> Optional[date]:
    if not val or pd.isna(val):
        return None
    val_str = str(val).strip()
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(val_str, fmt).date()
        except ValueError:
            continue
    return None

def _clean_float(val: Any) -> float:
    if val is None or pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    val_str = str(val).replace(",", "").strip()
    try:
        return float(val_str)
    except (ValueError, TypeError):
        return 0.0

def fetch_nse_bulk_and_block_deals(
    from_date: Optional[date] = None,
    to_date: Optional[date] = None,
    period: str = "1M",
) -> List[Dict[str, Any]]:
    """
    Fetches bulk and block deals from NSE for the specified date range or period.
    Period options: '1D', '1W', '1M', '3M', '6M', '1Y'.
    """
    try:
        from nselib import capital_market
    except ImportError:
        log.error("nselib is required for fetching bulk/block deals. Run `pip install nselib`.")
        return []

    records: List[Dict[str, Any]] = []
    
    # 1. Fetch Bulk Deals
    try:
        if from_date and to_date:
            f_str = from_date.strftime("%d-%m-%Y")
            t_str = to_date.strftime("%d-%m-%Y")
            df_bulk = capital_market.bulk_deal_data(from_date=f_str, to_date=t_str)
        else:
            df_bulk = capital_market.bulk_deal_data(period=period)
            
        if df_bulk is not None and not df_bulk.empty:
            df_bulk.columns = [c.strip().lstrip("﻿").strip('"') for c in df_bulk.columns]
            for _, row in df_bulk.iterrows():
                d_date = _parse_date(row.get("Date"))
                if not d_date:
                    continue
                sym = str(row.get("Symbol", "")).strip().upper()
                sec_name = str(row.get("SecurityName", "")).strip()
                client = str(row.get("ClientName", "")).strip()
                side = str(row.get("Buy/Sell", "")).strip().upper()
                qty = _clean_float(row.get("QuantityTraded", 0))
                price = _clean_float(row.get("TradePrice/Wght.Avg.Price", 0))
                remarks = str(row.get("Remarks", "")).strip()
                if remarks == "nan" or remarks == "-":
                    remarks = ""
                
                val_cr = round((qty * price) / 10_000_000.0, 4)
                
                records.append({
                    "deal_date": d_date,
                    "deal_type": "BULK",
                    "symbol": sym,
                    "security_name": sec_name,
                    "client_name": client,
                    "buy_sell": side,
                    "quantity": qty,
                    "trade_price": price,
                    "value_cr": val_cr,
                    "remarks": remarks,
                })
    except Exception as exc:
        log.warning("Failed to fetch NSE bulk deals: %s", exc)

    # 2. Fetch Block Deals
    try:
        if from_date and to_date:
            f_str = from_date.strftime("%d-%m-%Y")
            t_str = to_date.strftime("%d-%m-%Y")
            df_block = capital_market.block_deals_data(from_date=f_str, to_date=t_str)
        else:
            df_block = capital_market.block_deals_data(period=period)
            
        if df_block is not None and not df_block.empty:
            df_block.columns = [c.strip().lstrip("﻿").strip('"') for c in df_block.columns]
            for _, row in df_block.iterrows():
                d_date = _parse_date(row.get("Date"))
                if not d_date:
                    continue
                sym = str(row.get("Symbol", "")).strip().upper()
                sec_name = str(row.get("SecurityName", "")).strip()
                client = str(row.get("ClientName", "")).strip()
                side = str(row.get("Buy/Sell", "")).strip().upper()
                qty = _clean_float(row.get("QuantityTraded", 0))
                price = _clean_float(row.get("TradePrice/Wght.Avg.Price", 0))
                remarks = str(row.get("Remarks", "")).strip()
                if remarks == "nan" or remarks == "-":
                    remarks = ""
                
                val_cr = round((qty * price) / 10_000_000.0, 4)
                
                records.append({
                    "deal_date": d_date,
                    "deal_type": "BLOCK",
                    "symbol": sym,
                    "security_name": sec_name,
                    "client_name": client,
                    "buy_sell": side,
                    "quantity": qty,
                    "trade_price": price,
                    "value_cr": val_cr,
                    "remarks": remarks,
                })
    except Exception as exc:
        log.warning("Failed to fetch NSE block deals: %s", exc)

    return records
