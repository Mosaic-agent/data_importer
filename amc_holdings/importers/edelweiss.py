"""
src/data_importer/amc_holdings/importers/edelweiss.py
────────────────────────────────────────────────────
Edelweiss Mutual Fund & SIF portfolio holdings importer.

Supports:
  1. Multi-tab SEBI statutory portfolio Excel workbooks (.xlsx / .xls)
  2. Live API discovery via api.edelweissmf.com → getProductListData / getProductInformation
  3. Local file import via --excel-file flag

All file downloads are standard HTTPS GETs (no decryption needed).
API catalogue endpoints use HMAC-encrypted payloads decrypted via
``get_authenticated_data`` from the shared crypto utility.

Standard ClickHouse Target: market_data.mf_holdings FINAL
"""

from __future__ import annotations

import calendar
import hashlib
import io
import logging
import re
import urllib.parse
from datetime import date, datetime
from os.path import basename
from typing import Any

import httpx
import pandas as pd

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

# ── Month lookup for URL-path date parsing ────────────────────────────────────

_MONTH_LOOKUP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# ── Sheets to skip in consolidated workbooks ──────────────────────────────────

_SKIP_SHEETS = {"index", "summary", "instructions", "disclaimer", "cover"}

# ── Row-level labels to skip (totals, sub-headings) ──────────────────────────

_SKIP_KEYWORDS = {"TOTAL", "SUBTOTAL", "GRAND TOTAL", "NET ASSETS"}

_SKIP_PREFIXES = re.compile(r"^\((?:[a-z]|[ivx]+)\)", re.IGNORECASE)

# ── HTTP defaults ─────────────────────────────────────────────────────────────

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/octet-stream, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, */*",
    "Referer": "https://www.edelweissmf.com/",
}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _deterministic_isin(security_name: str) -> str:
    """Generate a collision-resistant synthetic ISIN from security name.

    Uses MD5 of the normalised name (stripped + uppercased) to avoid the
    truncation collisions of the old ``EDEL_<name[:20]>`` approach.
    """
    h = hashlib.md5(security_name.strip().upper().encode()).hexdigest()[:12].upper()
    return f"EDEL{h}"


def _normalize_scheme_name(raw: str) -> tuple[str, str]:
    """Return ``(scheme_code, fund_name)`` from a raw sheet/scheme title.

    Ensures the human-readable name begins with *Edelweiss* and derives a
    unique slug for ``scheme_code``.
    """
    cleaned = raw.strip()
    if not cleaned.lower().startswith("edelweiss"):
        cleaned = f"Edelweiss {cleaned}"
    slug = re.sub(r"[^A-Z0-9]+", "_", cleaned.upper()).strip("_")
    return slug, cleaned


def _parse_url_date(url: str) -> date | None:
    """Extract month-end date from an Edelweiss CDN file path.

    Expected URL pattern: ``/…/(2024)/(January)/…`` or ``/…/(2025)/(Mar)/…``
    """
    m = re.search(
        r"/(20[2-9]\d)/([A-Za-z]+)/",
        urllib.parse.unquote(url),
    )
    if not m:
        return None
    year = int(m.group(1))
    month_str = m.group(2).lower()
    month = _MONTH_LOOKUP.get(month_str)
    if month is None:
        return None
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, last_day)


def _parse_sheet_date(text: str) -> date | None:
    """Extract disclosure date from sheet banner text.

    Handles patterns like:
    - ``PORTFOLIO AS ON 31st May 2025``
    - ``AS OF 30 June 2024``
    """
    if not text:
        return None
    m = re.search(
        r"(?:as on|as of)\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\s+(\d{4})",
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
    return (first - __import__("datetime").timedelta(days=1))


# ── Importer ──────────────────────────────────────────────────────────────────


class EdelweissImporter(BaseFundImporter):
    """Edelweiss Mutual Fund & SIF portfolio holdings importer.

    Discovers monthly SEBI statutory portfolio Excel workbooks published by
    Edelweiss AMC and parses them into ClickHouse-ready row dicts.

    Supports both local file import and live API-driven discovery.
    """

    AMC_NAME = "Edelweiss Mutual Fund"
    REQUEST_DELAY: float = 1.5  # Edelweiss CDN is moderately rate-limited

    def __init__(
        self,
        from_year: int = 2023,
        full_reimport: bool = False,
        target_month: date | None = None,
        excel_file: str | None = None,
    ) -> None:
        super().__init__(target_month=target_month)
        self.from_year = from_year
        self.full_reimport = full_reimport
        self._excel_file = excel_file

    # ── Abstract interface implementation ─────────────────────────────────

    def fund_name(self) -> str:
        """Human-readable AMC label used in progress bars and logs."""
        return self.AMC_NAME

    def table_name(self) -> str:
        """Target ClickHouse table (fully qualified)."""
        return "market_data.mf_holdings"

    def column_names(self) -> list[str]:
        """Ordered column names matching the ClickHouse table schema."""
        return _COLUMNS

    def watermark_source(self) -> str:
        """Value for the ``source`` column in ``import_watermarks``."""
        return "mf_holdings"

    # ── Source Discovery ──────────────────────────────────────────────────

    def fetch_sources(self) -> list[tuple[date, str, str, str]]:
        """Discover monthly portfolio files from local path or Edelweiss API.

        Returns:
            Sorted (descending by date) list of
            ``(as_of_date, title, filename, url_or_path)`` tuples.
        """
        # ── Local file shortcut ───────────────────────────────────────────
        if self._excel_file:
            as_of = self._target_month or _last_month_end()
            fname = basename(self._excel_file)
            self._console.print(
                f"[dim]Edelweiss: using local file [bold]{fname}[/bold] "
                f"as {as_of.strftime('%Y-%m')}[/dim]"
            )
            return [(as_of, "Local File", fname, self._excel_file)]

        # ── Live API discovery ────────────────────────────────────────────
        from src.utils.edelweiss_crypto import get_authenticated_data

        self._console.print("[dim]Edelweiss: discovering portfolio files via API…[/dim]")

        # Step 1 — get all funds
        product_data = get_authenticated_data("third-party/getProductListData")
        funds: list[dict] = product_data.get("Value", [])
        if not funds:
            logger.warning("Edelweiss API returned no funds from getProductListData.")
            return []

        self._console.print(
            f"[dim]  Found {len(funds)} fund(s). Scanning FileDetails for xlsx/xls…[/dim]"
        )

        sources: list[tuple[date, str, str, str]] = []
        seen_urls: set[str] = set()

        for fund in funds:
            fund_id = fund.get("Id")
            fund_title = (fund.get("FundName") or "").strip()
            if not fund_id:
                continue

            try:
                info = get_authenticated_data(
                    "third-party/getProductInformation",
                    params={"id": fund_id},
                )
            except Exception as exc:
                logger.warning(
                    "Failed to fetch product info for fund %s (%s): %s",
                    fund_id, fund_title, exc,
                )
                continue

            file_details: list[dict] = info.get("FileDetails") or []

            for fd in file_details:
                file_path: str = fd.get("FilePath", "")
                if not file_path:
                    continue

                # ── Only xlsx/xls (NO PDFs) ───────────────────────────────
                ext_lower = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
                if ext_lower not in ("xlsx", "xls"):
                    continue

                # Skip non-portfolio collateral or cash component files
                low_p = file_path.lower()
                low_f = (fd.get("FileName") or "").lower()
                if any(k in low_p or k in low_f for k in ["cash component", "other2", "leaflet", "presentation", "sid", "kim", "form"]):
                    continue

                # ── Parse date from URL path ──────────────────────────────
                as_of = _parse_url_date(file_path)
                if not as_of:
                    continue
                if as_of.year < self.from_year:
                    continue


                # ── Build download URL ────────────────────────────────────
                download_url = (
                    f"https://www.edelweissmf.com{urllib.parse.quote(file_path)}"
                    if file_path.startswith("/")
                    else file_path
                )
                if download_url in seen_urls:
                    continue
                seen_urls.add(download_url)

                fname = file_path.split("/")[-1]
                title = fd.get("FileName") or fund_title or fname
                sources.append((as_of, title, fname, download_url))

        # ── Step 2: Check getExcel API for Bharat Bond series ─────────────
        try:
            excel_data = get_authenticated_data("third-party/getExcel")
            for k, v in excel_data.items():
                if k.startswith("Portfolio") and not k.startswith("PortfolioSummary") and isinstance(v, list) and len(v) > 0:
                    as_of = self._target_month or _last_month_end()
                    sources.append((as_of, f"BHARAT Bond {k.replace('Portfolio', '')}", f"{k}.json", f"api://getExcel/{k}"))
        except Exception as e:
            logger.debug("Error checking getExcel: %s", e)

        # ── Sort descending by date ───────────────────────────────────────
        sources.sort(key=lambda s: s[0], reverse=True)

        # ── Default delta-sync: only latest month ─────────────────────────
        if not self.full_reimport and not self._target_month and sources:
            latest_month = sources[0][0]
            sources = [s for s in sources if s[0] == latest_month]

        # ── Group by month, keep one consolidated file per month ──────────
        by_month: dict[date, list[tuple[date, str, str, str]]] = {}
        for s in sources:
            by_month.setdefault(s[0], []).append(s)

        filtered: list[tuple[date, str, str, str]] = []
        for month_date, month_sources in by_month.items():
            # Separate API sources from web files
            api_sources = [s for s in month_sources if s[3].startswith("api://")]
            web_sources = [s for s in month_sources if not s[3].startswith("api://")]

            if api_sources:
                filtered.extend(api_sources)

            if web_sources:
                preferred = [
                    s for s in web_sources
                    if any(
                        k in s[1].lower() or k in s[2].lower()
                        for k in ("portfolio", "statutory", "monthly")
                    )
                ]
                filtered.extend(preferred[:1] if preferred else web_sources[:1])

        filtered.sort(key=lambda s: s[0], reverse=True)
        logger.info(
            "Edelweiss: %d source(s) to process (from_year=%d, full_reimport=%s).",
            len(filtered), self.from_year, self.full_reimport,
        )
        return filtered


    # ── Watermark-based delta sync ────────────────────────────────────────

    def filter_sources(
        self,
        sources: list[tuple[date, str, str, str]],
        client: Any,
    ) -> list[tuple[date, str, str, str]]:
        """Skip sources whose month is already recorded in import_watermarks.

        Args:
            sources: Candidate source tuples from ``fetch_sources``.
            client:  Active ClickHouse client.

        Returns:
            Sources not yet present in the watermark table.
        """
        if self.full_reimport:
            return sources

        existing_months: set[date] = set()
        try:
            rows = client.query(
                "SELECT DISTINCT toDate(last_date) "
                "FROM market_data.import_watermarks "
                "WHERE source = 'mf_holdings' "
                "  AND symbol LIKE 'EDELWEISS_%'"
            ).result_rows
            for (dt,) in rows:
                if isinstance(dt, date):
                    existing_months.add(dt.replace(day=1))
        except Exception as exc:
            self._console.print(
                f"[yellow]Failed to query Edelweiss watermarks: {exc}[/yellow]"
            )
            return sources

        if not existing_months:
            return sources

        filtered = [
            s for s in sources
            if s[0].replace(day=1) not in existing_months
        ]

        skipped = len(sources) - len(filtered)
        if skipped:
            self._console.print(
                f"[dim]Delta sync: {skipped} Edelweiss file(s) already in DB, "
                f"{len(filtered)} to fetch.[/dim]"
            )
        return filtered

    # ── Source month extraction ────────────────────────────────────────────

    def source_month(self, source: Any) -> date | None:
        """Extract first-of-month date from source tuple."""
        if isinstance(source, (list, tuple)) and len(source) >= 1:
            val = source[0]
            if isinstance(val, date):
                return val.replace(day=1)
        return None

    # ── Source Parsing ────────────────────────────────────────────────────

    def parse_source(
        self,
        source: tuple[date, str, str, str],
        http: httpx.Client,
    ) -> list[dict[str, Any]]:
        """Download and parse one Edelweiss statutory Excel workbook.

        Each workbook contains multiple tabs — one per scheme.  The parser:
          1. Iterates all sheets (skipping Index/Summary/Cover/etc.)
          2. Scans first 10 rows for ``as on`` / ``as of`` disclosure date
          3. Locates the header row via ISIN or (NAME + NAV/%) heuristics
          4. Extracts holdings rows with valid security names and pct > 0

        Args:
            source: ``(as_of_date, title, filename, url_or_path)`` tuple.
            http:   Shared ``httpx.Client`` from the base ``run()`` loop.

        Returns:
            List of row dicts matching ``_COLUMNS``.
        """
        as_of_date, title, filename, url_or_path = source
        logger.debug("Processing Edelweiss source: %s (%s)", title, as_of_date)

        # ── Handle api://getExcel sources ─────────────────────────────────
        if url_or_path.startswith("api://getExcel/"):
            series_key = url_or_path.split("/")[-1]
            from src.utils.edelweiss_crypto import get_authenticated_data
            excel_data = get_authenticated_data("third-party/getExcel")
            portfolio_rows = excel_data.get(series_key, [])
            fund_title = f"EDELWEISS_BHARAT_BOND_{series_key.replace('Portfolio', '')}"
            imported_at = datetime.utcnow()
            rows = []
            for row in portfolio_rows:
                sec_name = str(row.get("SecurityName", "")).strip()
                isin_val = str(row.get("ISIN", "")).strip()
                exp_val = float(str(row.get("Exposure", "0")).replace("%", "").strip() or 0)
                if sec_name and exp_val > 0:
                    rows.append({
                        "scheme_code": fund_title,
                        "fund_name": fund_title,
                        "as_of_month": as_of_date,
                        "isin": isin_val or _deterministic_isin(sec_name),
                        "security_name": sec_name,
                        "asset_type": "bond",
                        "market_value_cr": 0.0,
                        "pct_of_nav": round(exp_val, 4),
                        "imported_at": imported_at,
                    })
            logger.debug("  %s: %d holdings extracted from getExcel API", title, len(rows))
            return rows


        # ── Load workbook bytes ───────────────────────────────────────────
        try:
            content = self._load_workbook_bytes(url_or_path, http)
        except Exception as exc:
            self._console.print(
                f"  [red]Failed to load {filename}: {exc}[/red]"
            )
            return []


        # ── Open with pandas ──────────────────────────────────────────────
        try:
            engine = "openpyxl" if filename.lower().endswith((".xlsx", ".xlsm")) else "xlrd"
            excel = pd.ExcelFile(io.BytesIO(content), engine=engine)
        except Exception as exc:
            self._console.print(
                f"  [red]Failed to open Excel file {filename}: {exc}[/red]"
            )
            return []

        # ── Parse each sheet ──────────────────────────────────────────────
        rows: list[dict[str, Any]] = []

        for sheet_name in excel.sheet_names:
            if sheet_name.strip().lower() in _SKIP_SHEETS:
                continue

            try:
                sheet_rows = self._parse_sheet(excel, sheet_name, as_of_date)
                rows.extend(sheet_rows)
            except Exception as exc:
                logger.warning(
                    "Error parsing sheet '%s' in %s: %s",
                    sheet_name, filename, exc,
                )

        logger.info(
            "  %s (%s): %d holdings parsed across %d sheet(s)",
            title, as_of_date, len(rows),
            len([s for s in excel.sheet_names if s.strip().lower() not in _SKIP_SHEETS]),
        )
        return rows

    # ── Private helpers ───────────────────────────────────────────────────

    def _load_workbook_bytes(self, url_or_path: str, http: httpx.Client) -> bytes:
        """Load workbook bytes from local disk or remote URL.

        Local files are identified by the absence of ``http://`` / ``https://``
        prefix.  Remote files are downloaded with basic browser headers — the
        actual file content is not encrypted.
        """
        if not url_or_path.startswith(("http://", "https://")):
            # Local file
            from pathlib import Path

            path = Path(url_or_path)
            if not path.exists():
                raise FileNotFoundError(f"Local file not found: {url_or_path}")
            return path.read_bytes()

        # Remote download
        resp = http.get(url_or_path, headers=_DEFAULT_HEADERS)
        if resp.status_code != 200:
            raise RuntimeError(
                f"HTTP {resp.status_code} downloading {url_or_path}"
            )
        return resp.content

    def _parse_sheet(
        self,
        excel: pd.ExcelFile,
        sheet_name: str,
        fallback_date: date,
    ) -> list[dict[str, Any]]:
        """Parse a single scheme sheet from the consolidated workbook.

        Args:
            excel:         Open ``pd.ExcelFile`` handle.
            sheet_name:    Tab name (used as scheme identifier).
            fallback_date: Date from the source tuple, used when sheet
                           header doesn't contain an ``as on`` banner.

        Returns:
            List of row dicts matching ``_COLUMNS``.
        """
        df = excel.parse(sheet_name, header=None)
        if len(df) < 5:
            return []

        # ── 1. Scheme identity from sheet name ────────────────────────────
        scheme_code, scheme_fund_name = _normalize_scheme_name(sheet_name)

        # ── 2. Scan first 10 rows for disclosure date ─────────────────────
        as_of_date = fallback_date
        for r_i in range(min(10, len(df))):
            row_str = " ".join(
                str(x).strip() for x in df.iloc[r_i].values if pd.notna(x)
            )
            parsed = _parse_sheet_date(row_str)
            if parsed:
                as_of_date = parsed
                break

        # ── 3. Find header row ────────────────────────────────────────────
        header_row = self._find_header_row(df)
        if header_row is None:
            return []

        # ── 4. Map columns ────────────────────────────────────────────────
        h_vals = [str(x).strip() for x in df.iloc[header_row].values]
        name_col, isin_col, pct_col, mval_col, industry_col = (
            self._map_columns(h_vals)
        )

        if name_col is None or pct_col is None:
            return []

        # ── 5. Extract data rows ──────────────────────────────────────────
        imported_at = datetime.utcnow()
        rows: list[dict[str, Any]] = []

        for r_i in range(header_row + 1, len(df)):
            row = df.iloc[r_i]
            row_dict = self._extract_holding_row(
                row, name_col, isin_col, pct_col, mval_col, industry_col,
                scheme_code, scheme_fund_name, as_of_date, imported_at,
            )
            if row_dict is not None:
                rows.append(row_dict)

        return rows

    def _find_header_row(self, df: pd.DataFrame) -> int | None:
        """Locate the header row by scanning for ISIN or NAME + NAV columns.

        Scans the first 15 rows of the raw dataframe.
        """
        for r_i in range(min(15, len(df))):
            vals = [
                str(x).strip().upper()
                for x in df.iloc[r_i].values
                if pd.notna(x)
            ]
            has_isin = any("ISIN" in v for v in vals)
            has_name = any(
                k in v for v in vals for k in ("NAME", "SECURITY", "COMPANY", "ISSUER")
            )
            has_value = any(
                k in v for v in vals for k in ("NAV", "WEIGHT", "%", "PERCENTAGE")
            )

            if has_isin or (has_name and has_value):
                return r_i

        return None

    @staticmethod
    def _map_columns(
        header_vals: list[str],
    ) -> tuple[int | None, int | None, int | None, int | None, int | None]:
        """Map header cell values to logical column indices.

        Returns:
            ``(name_col, isin_col, pct_col, mval_col, industry_col)`` —
            any may be ``None`` if not found.
        """
        name_col = isin_col = pct_col = mval_col = industry_col = None

        for idx, val in enumerate(header_vals):
            upper = val.upper()
            if name_col is None and any(
                k in upper for k in ("NAME", "SECURITY", "COMPANY", "ISSUER")
            ):
                name_col = idx
            elif "ISIN" in upper and isin_col is None:
                isin_col = idx
            elif pct_col is None and any(
                k in upper for k in ("NAV", "WEIGHT", "%", "PERCENTAGE", "EXPOSURE")
            ):
                pct_col = idx
            elif mval_col is None and any(
                k in upper for k in ("MARKET VALUE", "VALUE", "AMOUNT")
            ):
                mval_col = idx
            elif industry_col is None and any(
                k in upper for k in ("INDUSTRY", "SECTOR")
            ):
                industry_col = idx

        return name_col, isin_col, pct_col, mval_col, industry_col

    def _extract_holding_row(
        self,
        row: pd.Series,
        name_col: int,
        isin_col: int | None,
        pct_col: int,
        mval_col: int | None,
        industry_col: int | None,
        scheme_code: str,
        scheme_fund_name: str,
        as_of_date: date,
        imported_at: datetime,
    ) -> dict[str, Any] | None:
        """Extract a single holding row, or ``None`` if it should be skipped."""
        # ── Security name ─────────────────────────────────────────────────
        raw_name = row.iloc[name_col] if name_col < len(row) else None
        if pd.isna(raw_name):
            return None
        sec_name = str(raw_name).strip()
        if len(sec_name) < 3:
            return None

        # Skip totals and sub-heading labels
        sec_upper = sec_name.upper()
        if any(kw in sec_upper for kw in _SKIP_KEYWORDS):
            return None
        if _SKIP_PREFIXES.match(sec_name):
            return None

        # ── % of NAV ─────────────────────────────────────────────────────
        try:
            pct_raw = row.iloc[pct_col] if pct_col < len(row) else None
            if pd.isna(pct_raw):
                return None
            pct_val = float(str(pct_raw).replace("%", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return None

        if pct_val <= 0:
            return None

        # ── Market value (crores) ─────────────────────────────────────────
        mv_cr = 0.0
        if mval_col is not None and mval_col < len(row) and pd.notna(row.iloc[mval_col]):
            try:
                mv_cr = float(
                    str(row.iloc[mval_col]).replace(",", "").strip()
                )
            except (ValueError, TypeError):
                mv_cr = 0.0

        # ── ISIN ──────────────────────────────────────────────────────────
        isin = ""
        if isin_col is not None and isin_col < len(row) and pd.notna(row.iloc[isin_col]):
            isin = str(row.iloc[isin_col]).strip()
        # Validate: real ISINs are 12 chars, alphanumeric
        if not isin or isin.lower() in ("nan", "nil", "-", "none", "n.a.", "na") or len(isin) < 6:
            isin = _deterministic_isin(sec_name)

        # ── Industry / asset classification ───────────────────────────────
        industry = ""
        if industry_col is not None and industry_col < len(row) and pd.notna(row.iloc[industry_col]):
            industry = str(row.iloc[industry_col]).strip()

        asset_type = classify_asset(sec_name, industry)

        return {
            "scheme_code": scheme_code,
            "fund_name": scheme_fund_name,
            "as_of_month": as_of_date,
            "isin": isin,
            "security_name": sec_name,
            "asset_type": asset_type,
            "market_value_cr": round(mv_cr, 4),
            "pct_of_nav": round(pct_val, 4),
            "imported_at": imported_at,
        }
