"""
src/data_importer/amc_holdings/importers/qsif.py
────────────────────────────────────────────────
Quant SIF (Specialized Investment Fund) portfolio holdings importer.

Directly supports:
  1. Multi-tab & Single-tab Statutory SEBI Portfolio Excel workbooks (.xlsx / .xls)
  2. Live Web API discovery via https://www.qsif.com/statutorydisclosures.aspx/displaydisclouser2
  3. Local file import via --file flag

Standard ClickHouse Target: market_data.mf_holdings FINAL
"""

from __future__ import annotations

import calendar
import hashlib
import io
import logging
import re
from datetime import date, datetime
from os.path import basename
from typing import Any

import httpx
import pandas as pd
from bs4 import BeautifulSoup

from src.data_importer.amc_holdings.base import BaseFundImporter, classify_asset

logger = logging.getLogger(__name__)

# ── Column spec (must match ClickHouse table DDL) ─────────────────────────────

_COLUMNS = [
    "scheme_code",
    "fund_name",
    "as_of_month",
    "isin",
    "security_name",
    "asset_type",
    "market_value_cr",
    "pct_of_nav",
    "imported_at",
]

_DISCLOSURE_API_URL = "https://www.qsif.com/statutorydisclosures.aspx/displaydisclouser2"

_MONTH_LOOKUP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "june": 6, "jun": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def _deterministic_isin(security_name: str) -> str:
    """Generate collision-resistant synthetic ISIN from security name."""
    h = hashlib.md5(security_name.strip().upper().encode()).hexdigest()[:12].upper()
    return f"QSIF{h}"


def resolve_fund_identity(raw_title: str) -> tuple[str, str]:
    """Resolve canonical scheme_code and fund_name for Quant SIF funds."""
    t = raw_title.upper()
    if "ACTIVE_ASSET_ALLOCATOR" in t or "ACTIVE ASSET ALLOCATOR" in t:
        return "QSIF_ACTIVE_ALLOCATOR_DIRECT", "QSIF_ACTIVE_ASSET_ALLOCATOR_LONG_SHORT"
    elif "EX_TOP_100" in t or "EX-TOP 100" in t or "EX TOP 100" in t:
        return "QSIF_EX_TOP_100_DIRECT", "QSIF_EQUITY_EX_TOP_100_LONG_SHORT"
    elif "SECTOR_ROTATION" in t or "SECTOR ROTATION" in t:
        return "QSIF_SECTOR_ROTATION_DIRECT", "QSIF_SECTOR_ROTATION_LONG_SHORT"
    elif "HYBRID" in t:
        return "QSIF_HYBRID_DIRECT", "QSIF_HYBRID_LONG_SHORT"
    elif ("EQUITY" in t and "LONG_SHORT" in t) or "LONG-SHORT" in t or "LONG SHORT" in t:
        return "QSIF_EQUITY_LS_DIRECT", "QSIF_EQUITY_LONG_SHORT"
    else:
        slug = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
        return f"QSIF_{slug}", f"QSIF_{slug}"


def _parse_banner_date(text: str) -> date | None:
    """Extract disclosure date from sheet banner (e.g. 'AS ON 31 Jul 2026')."""
    if not text:
        return None
    m = re.search(
        r"(?:as on|as at|statement as on)\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})",
        text,
        re.IGNORECASE,
    )
    if m:
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)[:3]} {m.group(3)}", "%d %b %Y"
            ).date()
        except ValueError:
            pass
    return None


def _last_month_end() -> date:
    """Return the last day of the previous month."""
    today = date.today()
    first = today.replace(day=1)
    return first - __import__("datetime").timedelta(days=1)


class QsifImporter(BaseFundImporter):
    """Quant SIF (Specialized Investment Fund) portfolio holdings importer.

    Discovers monthly statutory disclosures via the QSIF statutory disclosures API
    and parses Excel workbooks (.xlsx / .xls) into ClickHouse rows.
    """

    AMC_NAME = "Quant SIF"
    REQUEST_DELAY: float = 1.0

    def __init__(
        self,
        from_year: int = 2024,
        full_reimport: bool = False,
        target_month: date | None = None,
        excel_file: str | None = None,
    ) -> None:
        super().__init__(target_month=target_month)
        self.from_year = from_year
        self.full_reimport = full_reimport
        self._excel_file = excel_file

    def fund_name(self) -> str:
        return self.AMC_NAME

    def table_name(self) -> str:
        return "market_data.mf_holdings"

    def column_names(self) -> list[str]:
        return _COLUMNS

    def watermark_source(self) -> str:
        return "mf_holdings"

    # ── Source Discovery ──────────────────────────────────────────────────

    def fetch_sources(self) -> list[tuple[date, str, str, str]]:
        """Discover monthly portfolio files from local path or QSIF disclosure API.

        Returns:
            list of (as_of_date, title, filename, url_or_path) tuples.
        """
        if self._excel_file:
            as_of = self._target_month or _last_month_end()
            fname = basename(self._excel_file)
            self._console.print(
                f"[dim]Quant SIF: using local file [bold]{fname}[/bold] as {as_of.strftime('%Y-%m')}[/dim]"
            )
            return [(as_of, "Local File", fname, self._excel_file)]

        self._console.print("[dim]Quant SIF: discovering portfolio files via qsif.com API…[/dim]")
        sources: list[tuple[date, str, str, str]] = []
        seen_urls: set[str] = set()

        current_year = date.today().year
        years = [str(y) for y in range(current_year, self.from_year - 1, -1)]

        with httpx.Client(headers=_HEADERS, timeout=15.0, follow_redirects=True) as http:
            for cat in ["MONTHLY PORTFOLIO - FUND - WISE", "HALF YEARLY PORTFOLIO"]:
                for year in years:
                    for month_id in range(1, 13):
                        payload = {"id": str(month_id), "cat": cat, "tab": year}
                        try:
                            r = http.post(_DISCLOSURE_API_URL, json=payload)
                            if r.status_code != 200:
                                continue
                            html = r.json().get("d", "")
                            if not html:
                                continue
                            soup = BeautifulSoup(html, "html.parser")
                            for a in soup.find_all("a", href=True):
                                href = a["href"]
                                title = a.get_text(strip=True)
                                if not any(href.lower().endswith(ext) for ext in [".xlsx", ".xls"]):
                                    continue

                                full_url = (
                                    f"https://www.qsif.com{href}"
                                    if href.startswith("/")
                                    else f"https://www.qsif.com/{href}"
                                )
                                if full_url in seen_urls:
                                    continue
                                seen_urls.add(full_url)

                                # Approximate month-end date from year and month_id
                                y_int = int(year)
                                last_day = calendar.monthrange(y_int, month_id)[1]
                                approx_date = date(y_int, month_id, last_day)

                                fname = href.split("/")[-1]
                                sources.append((approx_date, title, fname, full_url))
                        except Exception as e:
                            logger.debug("Error probing QSIF disclosure %s-%s: %s", year, month_id, e)

        # Sort descending by date
        sources.sort(key=lambda s: s[0], reverse=True)

        if not self.full_reimport and not self._target_month and sources:
            latest_month = sources[0][0]
            sources = [s for s in sources if s[0] == latest_month]

        logger.info("Found %d Quant SIF disclosure source(s) to process.", len(sources))
        return sources

    # ── Watermark Filter ──────────────────────────────────────────────────

    def filter_sources(
        self,
        sources: list[tuple[date, str, str, str]],
        client: Any,
    ) -> list[tuple[date, str, str, str]]:
        """Skip sources whose month is already recorded in import_watermarks."""
        if self.full_reimport:
            return sources

        existing_months: set[date] = set()
        try:
            rows = client.query(
                "SELECT DISTINCT toDate(last_date) "
                "FROM market_data.import_watermarks "
                "WHERE source = 'mf_holdings' "
                "  AND symbol LIKE 'QSIF_%'"
            ).result_rows
            for (dt,) in rows:
                if isinstance(dt, date):
                    existing_months.add(dt.replace(day=1))
        except Exception as exc:
            self._console.print(f"[yellow]Failed to query QSIF watermarks: {exc}[/yellow]")
            return sources

        if not existing_months:
            return sources

        filtered = [s for s in sources if s[0].replace(day=1) not in existing_months]
        skipped = len(sources) - len(filtered)
        if skipped:
            self._console.print(
                f"[dim]Delta sync: {skipped} QSIF file(s) already in DB, {len(filtered)} to fetch.[/dim]"
            )
        return filtered

    def source_month(self, source: Any) -> date | None:
        if isinstance(source, (list, tuple)) and len(source) >= 1:
            val = source[0]
            if isinstance(val, date):
                return val.replace(day=1)
        return None

    # ── Parsing ───────────────────────────────────────────────────────────

    def parse_source(
        self,
        source: tuple[date, str, str, str],
        http: httpx.Client,
    ) -> list[dict[str, Any]]:
        """Download and parse a Quant SIF statutory Excel workbook."""
        as_of_date, title, filename, url_or_path = source
        logger.info("Processing Quant SIF source: %s (%s)", title, as_of_date)

        try:
            content = self._load_workbook_bytes(url_or_path, http)
        except Exception as exc:
            self._console.print(f"  [red]Failed to load {filename}: {exc}[/red]")
            return []

        try:
            engine = "openpyxl" if filename.lower().endswith((".xlsx", ".xlsm")) else "xlrd"
            excel = pd.ExcelFile(io.BytesIO(content), engine=engine)
        except Exception as exc:
            self._console.print(f"  [red]Failed to open Excel {filename}: {exc}[/red]")
            return []

        rows: list[dict[str, Any]] = []
        for sheet_name in excel.sheet_names:
            if sheet_name.strip().lower() in {"index", "summary", "instructions", "disclaimer", "cover"}:
                continue
            try:
                sheet_rows = self._parse_sheet(excel, sheet_name, title, as_of_date)
                rows.extend(sheet_rows)
            except Exception as exc:
                logger.warning("Error parsing sheet '%s' in %s: %s", sheet_name, filename, exc)

        logger.info("  %s (%s): %d holdings parsed", title, as_of_date, len(rows))
        return rows

    def _load_workbook_bytes(self, url_or_path: str, http: httpx.Client) -> bytes:
        if not url_or_path.startswith(("http://", "https://")):
            from pathlib import Path
            p = Path(url_or_path)
            if not p.exists():
                raise FileNotFoundError(f"Local file not found: {url_or_path}")
            return p.read_bytes()

        resp = http.get(url_or_path, headers=_HEADERS)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} downloading {url_or_path}")
        return resp.content

    def _parse_sheet(
        self,
        excel: pd.ExcelFile,
        sheet_name: str,
        file_title: str,
        fallback_date: date,
    ) -> list[dict[str, Any]]:
        df = excel.parse(sheet_name, header=None)
        if len(df) < 5:
            return []

        # 1. Date scanning
        as_of_date = fallback_date
        for r in range(min(12, len(df))):
            row_str = " ".join(str(x).strip() for x in df.iloc[r].values if pd.notna(x))
            d = _parse_banner_date(row_str)
            if d:
                as_of_date = d
                break

        # 2. Header row scanning
        header_idx = None
        for i, row in df.iterrows():
            vals = [str(x).upper() for x in row.values if pd.notna(x)]
            if any("ISIN" in v for v in vals) and any("INSTRUMENT" in v or "SECURITY" in v or "NAME" in v for v in vals):
                header_idx = i
                break

        if header_idx is None:
            return []

        # Scheme identification
        scheme_target = sheet_name if sheet_name.strip().upper() not in ["SHEET1", "PAGE 1"] else file_title
        scheme_code, fund_name = resolve_fund_identity(scheme_target)

        records: list[dict[str, Any]] = []
        current_section = "EQUITY"
        imported_at = datetime.utcnow()

        for i in range(header_idx + 1, len(df)):
            row = df.iloc[i]
            c1 = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else ""
            c2 = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
            c6 = row.iloc[6] if len(row) > 6 else None
            c7 = row.iloc[7] if len(row) > 7 else None

            row_str = " ".join([str(x).upper() for x in row.values if pd.notna(x)])

            if "DERIVATIVES" in row_str:
                current_section = "DERIVATIVES"
                continue
            elif "DEBT INSTRUMENTS" in row_str or "MONEY MARKET" in row_str:
                current_section = "DEBT"
                continue
            elif "FIXED DEPOSITS" in row_str or "OTHERS" in row_str or "TRI PARTY REPO" in row_str or "TREPS" in row_str:
                current_section = "OTHER"
                continue
            elif "EQUITY & EQUITY RELATED" in row_str:
                current_section = "EQUITY"
                continue
            elif "COMMODITY" in row_str:
                current_section = "COMMODITY"
                continue
            elif "GRAND TOTAL" in row_str or "DISCLOSURE FOR INVESTMENT" in row_str or "NOTES :-" in row_str:
                break

            if "SUB TOTAL" in row_str or "TOTAL" in row_str or not c2:
                continue

            if pd.notna(c6) and pd.notna(c7):
                try:
                    mkt_val_lakhs = float(str(c6).replace(",", "").strip())
                    pct_nav = float(str(c7).replace(",", "").replace("%", "").strip())
                    mkt_val_cr = mkt_val_lakhs / 100.0

                    sec_name = c2.strip()
                    if len(sec_name) < 3:
                        continue

                    # Validate real ISIN
                    isin = c1 if c1 and len(c1) == 12 and not c1.isdigit() and c1.lower() != "nan" else _deterministic_isin(sec_name)

                    # Classify asset
                    if current_section == "DERIVATIVES" or "FUTURES" in sec_name.upper() or "OPTION" in sec_name.upper() or mkt_val_cr < 0 or pct_nav < 0:
                        asset_type = "other"
                    elif current_section == "DEBT" or "TREPS" in sec_name.upper() or "BILL" in sec_name.upper() or "REPO" in sec_name.upper():
                        asset_type = "bond"
                    elif "GOLD" in sec_name.upper() or "SILVER" in sec_name.upper() or current_section == "COMMODITY":
                        asset_type = "gold"
                    elif "NET CURRENT ASSETS" in sec_name.upper() or "CASH" in sec_name.upper():
                        asset_type = "cash"
                    else:
                        asset_type = classify_asset(sec_name)

                    records.append({
                        "scheme_code": scheme_code,
                        "fund_name": fund_name,
                        "as_of_month": as_of_date,
                        "isin": isin,
                        "security_name": sec_name,
                        "asset_type": asset_type,
                        "market_value_cr": round(mkt_val_cr, 4),
                        "pct_of_nav": round(pct_nav, 4),
                        "imported_at": imported_at,
                    })
                except Exception:
                    pass

        return records
