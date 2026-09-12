"""
src/data_importer/amc_holdings/importers/altiva_sif.py
────────────────────────────────────────────────────────
Edelweiss "Altiva" Specialized Investment Fund (SIF) portfolio holdings
importer.

Altiva SIF is a distinct product line from regular Edelweiss Mutual Fund
schemes (different SEBI registration, different disclosure microsite) and is
NOT returned by the encrypted getProductListData API that EdelweissImporter
uses — hence a separate importer.

Supports:
  1. Statutory SEBI portfolio Excel workbook (.xlsx) — one tab per scheme
  2. Live discovery via the public disclosures page (plain HTML, no auth/
     encryption needed — unlike the main Edelweiss MF API)
  3. Local file import via --file flag

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
import pdfplumber

from src.data_importer.amc_holdings.base import BaseFundImporter

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

_DISCLOSURES_URL = "https://www.edelweissmf.com/altivasif/statutory/disclosures"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.edelweissmf.com/",
}

_MONTH_LOOKUP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# Portfolio workbook links look like:
#   .../altivaSIF/docs/Edelweiss MF_Altiva Equity Hybrid Long Short Fund
#   Portfolio Monthly Notes 31-October-2025_12112025_104710_AM.xlsx
_FILE_LINK_RE = re.compile(
    r'["\'](https?://[^"\']+altivaSIF/docs/[^"\']*\.xlsx)["\']', re.IGNORECASE
)
_FILENAME_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]+)-(\d{4})")
_BANNER_DATE_RE = re.compile(
    r"AS ON\s+([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", re.IGNORECASE
)
# Day-first variant, e.g. "Portfolio as on 31st January, 2026" (Fund Update PDFs)
_BANNER_DATE_DAY_FIRST_RE = re.compile(
    r"as\s+on\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})", re.IGNORECASE
)

_SKIP_SHEETS = {"index", "summary", "instructions", "disclaimer", "cover"}

# ── "Fund Update" PDF fact-sheet parsing (fallback source) ────────────────────
#
# The /altivasif/statutory/disclosures page (scraped above) is not kept
# current — newer months are published as "Fund Update" PDFs under
# /Files/SIF/Downloads/Factsheet/Factsheets/ instead, an Angular SPA path with
# no crawlable directory listing and unpredictable upload-timestamp filename
# suffixes. There is no reliable way to auto-discover *future* PDFs there, so
# known URLs are tracked in _KNOWN_PDF_SOURCES below and merged in — append to
# this list by hand (or via --file) whenever a new "Fund Update" PDF is found.
#
# Unlike the statutory xlsx, this fact-sheet format has no per-security ISIN
# or market value — only % of NAV grouped by strategy section, plus a
# month-end AUM figure used to back into market_value_cr.

_AUM_RE = re.compile(r"AUM Month End:\s*Rs\.?\s*([\d,]+)\s*cr", re.IGNORECASE)
# The reliable anchor for the portfolio's as-of date — some editions contain
# an earlier, unrelated "as on <date>" sentence in a performance caveat (a
# trailing-return disclosure for a different date than the holdings), so a
# bare first-match search over the whole document can pick the wrong date.
_PORTFOLIO_DATE_RE = re.compile(
    r"Portfolio summary as on\s+(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9}),?\s+(\d{4})",
    re.IGNORECASE,
)
_PDF_SECTION_RE = re.compile(r"^([A-Z][A-Za-z0-9/&\s]{2,40}?):\s*(-?[\d.]+)%$")
# Trailing "%" on each value is optional — present in some monthly editions
# (e.g. "HDFC Bank Ltd. 4.00%") and absent in others (e.g. "Bharti Airtel
# Ltd. 2.09").
_PDF_NAME_PCT_RE = re.compile(r"^(.*?)\s+(-?\d+\.\d+)%?$")
_PDF_LONE_PCT_RE = re.compile(r"^\(?(-?\d+\.\d+)%?\)?$")
_PDF_COL_SPLIT_X = 280.0
_PDF_RESET_MARKERS = (
    "Debt portfolio quants", "What makes this fund", "Scheme details",
    "Portfolio Commentary", "Cess and surcharge",
)
# Chart/legend captions observed sharing a line with the holdings table in
# some editions (not real securities) — extend as new editions surface more.
_PDF_KNOWN_CAPTIONS = {"Fixed Income Allocation"}
_PDF_SECTION_ASSET_TYPE = {
    "EQUITY & EQUITY RELATED": "equity",
    "INDEX/STOCK OPTIONS": "other",
    "INDEX/STOCK FUTURES": "other",
    "FIXED INCOME": "bond",
    "REITS/INVITS": "other",
    "OTHERS": "cash",
}

# Stable, non-timestamped "latest factsheet" alias URLs — Edelweiss
# overwrites these in place each month. They must never be skipped by the
# watermark-based delta sync below just because their *placeholder* date
# (set when this entry was added) happens to already be watermarked; the
# real as_of_month is only known after downloading and parsing the current
# content, which is how this importer keeps discovering new months without
# needing a new URL added to _KNOWN_PDF_SOURCES each time.
_STABLE_ALIAS_FILENAMES = {"Altiva_Hybrid_LongShort_Fund_Factsheet.pdf"}

_KNOWN_PDF_SOURCES: list[tuple[date, str, str, str]] = [
    (
        date(2026, 1, 31),
        "Altiva Hybrid Long-Short Fund Portfolio Update January 2026",
        "Altiva_Hybrid_LongShort_Fund_Portfolio_Update_January_2026_13022026170530.pdf",
        "https://www.edelweissmf.com/Files/SIF/Downloads/Factsheet/Factsheets/"
        "Altiva_Hybrid_LongShort_Fund_Portfolio_Update_January_2026_13022026170530.pdf",
    ),
    (
        date(2026, 5, 31),
        "Altiva Hybrid Long-Short Fund Portfolio Update May 2026",
        "Altiva_Hybrid_LongShort_Fund_Portfolio_Update_May_2026_08062026182111.pdf",
        "https://www.edelweissmf.com/Files/SIF/Downloads/Factsheet/Factsheets/"
        "Altiva_Hybrid_LongShort_Fund_Portfolio_Update_May_2026_08062026182111.pdf",
    ),
    (
        # Stable, non-timestamped "latest factsheet" alias — Edelweiss
        # overwrites this in place each month, so its content (not this
        # placeholder date) determines the real as_of_month once parsed.
        # Confirmed at the time this was added to contain the August 2026
        # update; keep this entry's date roughly current so it isn't
        # silently dropped by from_year filtering as months pass.
        date(2026, 8, 31),
        "Altiva Hybrid Long-Short Fund Portfolio Update (latest)",
        "Altiva_Hybrid_LongShort_Fund_Factsheet.pdf",
        "https://www.edelweissmf.com/Files/altivaSIF/Altiva_Hybrid_LongShort_Fund_Factsheet.pdf",
    ),
]


def _cluster_lines(words: list[dict], tol: float = 3.5) -> list[list[dict]]:
    """Group pdfplumber words into visual lines by y-position proximity.

    A simple ``round(top)`` bucket is not safe here — digit glyphs in this
    fact-sheet PDF sometimes render 1-2px off the baseline of the adjacent
    name text, which splits a single row into two buckets under naive
    rounding and silently drops values. Proximity clustering (running
    average, not fixed bucket) avoids that.
    """
    words_sorted = sorted(words, key=lambda w: w["top"])
    lines: list[list[dict]] = []
    cur: list[dict] = []
    cur_top = None
    for w in words_sorted:
        if cur and abs(w["top"] - cur_top) > tol:
            lines.append(cur)
            cur = []
        cur.append(w)
        cur_top = sum(x["top"] for x in cur) / len(cur)
    if cur:
        lines.append(cur)
    return lines


def _deterministic_isin(security_name: str) -> str:
    """Generate a collision-resistant synthetic ISIN from security name."""
    h = hashlib.md5(security_name.strip().upper().encode()).hexdigest()[:12].upper()
    return f"ALTV{h}"


def resolve_fund_identity(raw_title: str) -> tuple[str, str]:
    """Resolve canonical (scheme_code, fund_name) for an Altiva SIF scheme.

    Only "Hybrid Long-Short" is live today; the other branches anticipate
    additional Altiva SIF strategies launching later (mirrors quant's qSIF
    lineup, which Edelweiss's Altiva brand is modelled on).
    """
    t = raw_title.upper()
    if "HYBRID" in t:
        return "ALTIVA_HYBRID_DIRECT", "ALTIVA_HYBRID_LONG_SHORT"
    elif "EX TOP 100" in t or "EX-TOP 100" in t or "EX_TOP_100" in t:
        return "ALTIVA_EX_TOP_100_DIRECT", "ALTIVA_EQUITY_EX_TOP_100_LONG_SHORT"
    elif "SECTOR ROTATION" in t or "SECTOR_ROTATION" in t:
        return "ALTIVA_SECTOR_ROTATION_DIRECT", "ALTIVA_SECTOR_ROTATION_LONG_SHORT"
    elif "EQUITY" in t and ("LONG SHORT" in t or "LONG-SHORT" in t or "LONG_SHORT" in t):
        return "ALTIVA_EQUITY_LS_DIRECT", "ALTIVA_EQUITY_LONG_SHORT"
    else:
        slug = re.sub(r"[^A-Z0-9]+", "_", t).strip("_")
        return f"ALTIVA_{slug}", f"ALTIVA_{slug}"


def _parse_banner_date(text: str) -> date | None:
    """Extract disclosure date from the sheet banner, e.g.

    ``PORTFOLIO STATEMENT OF ALTIVA EQUITY HYBRID LONG SHORT FUND AS ON
    OCTOBER 31, 2025``.
    """
    m = _BANNER_DATE_RE.search(text or "")
    if m:
        month_str, day_str, year_str = m.groups()
    else:
        m = _BANNER_DATE_DAY_FIRST_RE.search(text or "")
        if not m:
            return None
        day_str, month_str, year_str = m.groups()

    month = _MONTH_LOOKUP.get(month_str[:3].lower()) or _MONTH_LOOKUP.get(month_str.lower())
    if month is None:
        return None
    try:
        return date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def _parse_portfolio_date(full_text: str) -> date | None:
    """Extract the portfolio holdings' as-of date from a Fund Update PDF.

    Anchors specifically on "Portfolio summary as on <date>" rather than a
    generic "as on <date>" scan, since some editions mention an earlier,
    unrelated date first (e.g. a trailing-return caveat referencing a
    different month than the actual holdings snapshot).
    """
    m = _PORTFOLIO_DATE_RE.search(full_text or "")
    if not m:
        return None
    day_str, month_str, year_str = m.groups()
    month = _MONTH_LOOKUP.get(month_str[:3].lower()) or _MONTH_LOOKUP.get(month_str.lower())
    if month is None:
        return None
    try:
        return date(int(year_str), month, int(day_str))
    except ValueError:
        return None


def _last_month_end() -> date:
    today = date.today()
    first = today.replace(day=1)
    return first - __import__("datetime").timedelta(days=1)


class AltivaSifImporter(BaseFundImporter):
    """Edelweiss Altiva SIF (Specialized Investment Fund) holdings importer.

    Discovers monthly statutory portfolio workbooks from the public Altiva
    SIF disclosures page and parses them into ClickHouse rows.
    """

    AMC_NAME = "Edelweiss Altiva SIF"
    REQUEST_DELAY: float = 1.0

    def __init__(
        self,
        from_year: int = 2025,
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
        return self.AMC_NAME

    def table_name(self) -> str:
        return "market_data.mf_holdings"

    def column_names(self) -> list[str]:
        return _COLUMNS

    def watermark_source(self) -> str:
        return "mf_holdings"

    # ── Source Discovery ──────────────────────────────────────────────────

    def fetch_sources(self) -> list[tuple[date, str, str, str]]:
        """Discover monthly portfolio workbooks from local path or the
        public Altiva SIF disclosures page.

        Returns:
            Sorted (descending by date) list of
            ``(as_of_date, title, filename, url)`` tuples.
        """
        if self._excel_file:
            as_of = self._target_month or _last_month_end()
            fname = basename(self._excel_file)
            self._console.print(
                f"[dim]Altiva SIF: using local file [bold]{fname}[/bold] "
                f"as {as_of.strftime('%Y-%m')}[/dim]"
            )
            return [(as_of, "Local File", fname, self._excel_file)]

        self._console.print(
            "[dim]Altiva SIF: discovering portfolio files via disclosures page…[/dim]"
        )
        try:
            with httpx.Client(headers=_HEADERS, timeout=30.0, follow_redirects=True) as http:
                resp = http.get(_DISCLOSURES_URL)
                resp.raise_for_status()
                html = resp.text
        except Exception as exc:
            self._console.print(f"[red]Failed to load Altiva SIF disclosures page: {exc}[/red]")
            return []

        sources: list[tuple[date, str, str, str]] = []
        seen: set[str] = set()
        for m in _FILE_LINK_RE.finditer(html):
            url = m.group(1)
            if url in seen:
                continue
            seen.add(url)

            fname = urllib.parse.unquote(url.split("/")[-1])
            if "portfolio" not in fname.lower():
                continue

            dm = _FILENAME_DATE_RE.search(fname)
            if not dm:
                continue
            day_str, month_str, year_str = dm.groups()
            month = _MONTH_LOOKUP.get(month_str.lower())
            if month is None:
                continue
            try:
                as_of = date(int(year_str), month, int(day_str))
            except ValueError:
                continue
            if as_of.year < self.from_year:
                continue

            sources.append((as_of, fname, fname, url))

        # ── Merge in known "Fund Update" PDFs (see module docstring above) ──
        seen_urls = {s[3] for s in sources}
        for as_of, title, fname, url in _KNOWN_PDF_SOURCES:
            if url in seen_urls or as_of.year < self.from_year:
                continue
            sources.append((as_of, title, fname, url))

        sources.sort(key=lambda s: s[0], reverse=True)

        if not self.full_reimport and not self._target_month and sources:
            latest_month = sources[0][0]
            sources = [
                s for s in sources
                if s[0] == latest_month or s[2] in _STABLE_ALIAS_FILENAMES
            ]

        logger.info("Found %d Altiva SIF disclosure source(s) to process.", len(sources))
        return sources

    # ── Watermark-based delta sync ────────────────────────────────────────

    def filter_sources(
        self,
        sources: list[tuple[date, str, str, str]],
        client: Any,
    ) -> list[tuple[date, str, str, str]]:
        if self.full_reimport:
            return sources

        existing_months: set[date] = set()
        try:
            rows = client.query(
                "SELECT DISTINCT toDate(last_date) "
                "FROM market_data.import_watermarks "
                "WHERE source = 'mf_holdings' "
                "  AND symbol LIKE 'ALTIVA_%'"
            ).result_rows
            for (dt,) in rows:
                if isinstance(dt, date):
                    existing_months.add(dt.replace(day=1))
        except Exception as exc:
            self._console.print(f"[yellow]Failed to query Altiva SIF watermarks: {exc}[/yellow]")
            return sources

        if not existing_months:
            return sources

        filtered = [
            s for s in sources
            if s[2] in _STABLE_ALIAS_FILENAMES or s[0].replace(day=1) not in existing_months
        ]
        skipped = len(sources) - len(filtered)
        if skipped:
            self._console.print(
                f"[dim]Delta sync: {skipped} Altiva SIF file(s) already in DB, "
                f"{len(filtered)} to fetch.[/dim]"
            )
        return filtered

    def source_month(self, source: Any) -> date | None:
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
        """Download and parse one Altiva SIF statutory Excel workbook."""
        as_of_date, title, filename, url_or_path = source
        logger.info("Processing Altiva SIF source: %s (%s)", title, as_of_date)

        try:
            content = self._load_workbook_bytes(url_or_path, http)
        except Exception as exc:
            self._console.print(f"  [red]Failed to load {filename}: {exc}[/red]")
            return []

        if filename.lower().endswith(".pdf"):
            try:
                rows = self._parse_pdf(content, as_of_date, title)
            except Exception as exc:
                self._console.print(f"  [red]Failed to parse PDF {filename}: {exc}[/red]")
                return []
            logger.info("  %s (%s): %d holdings parsed (PDF)", title, as_of_date, len(rows))
            return rows

        try:
            excel = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
        except Exception as exc:
            self._console.print(f"  [red]Failed to open Excel file {filename}: {exc}[/red]")
            return []

        rows: list[dict[str, Any]] = []
        for sheet_name in excel.sheet_names:
            if sheet_name.strip().lower() in _SKIP_SHEETS:
                continue
            try:
                rows.extend(self._parse_sheet(excel, sheet_name, as_of_date))
            except Exception as exc:
                logger.warning("Error parsing sheet '%s' in %s: %s", sheet_name, filename, exc)

        logger.info("  %s (%s): %d holdings parsed", title, as_of_date, len(rows))
        return rows

    def _parse_pdf(
        self,
        content: bytes,
        fallback_date: date,
        title: str,
    ) -> list[dict[str, Any]]:
        """Parse a "Fund Update" fact-sheet PDF (see module docstring).

        Two-column layout, section headers (e.g. ``EQUITY & EQUITY RELATED:
        33.43%``) sometimes paired side-by-side on one line for two different
        sections at once (e.g. ``INDEX/STOCK FUTURES: -3.22% FIXED INCOME:
        37.20%``) — each physical half of the page is tracked as an
        independent column with its own active section. No ISIN or per-
        security market value is published; ``market_value_cr`` is derived
        from % of NAV against the disclosed month-end AUM.
        """
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            full_text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            aum_m = _AUM_RE.search(full_text)
            if not aum_m:
                logger.warning("Altiva SIF PDF %s: could not find AUM figure, skipping.", title)
                return []
            aum_cr = float(aum_m.group(1).replace(",", ""))

            as_of_date = _parse_portfolio_date(full_text) or fallback_date
            scheme_code, fund_name = resolve_fund_identity(f"{title} {full_text[:2000]}")

            parsed: list[tuple[str, str, float]] = []
            left_section = right_section = None
            pending_left = pending_right = None

            def _emit(side_text: str, section: str | None, pending: str | None) -> str | None:
                if not side_text:
                    return pending
                lone = _PDF_LONE_PCT_RE.match(side_text)
                if lone and pending and section:
                    parsed.append((section, pending, float(lone.group(1))))
                    return None
                m = _PDF_NAME_PCT_RE.match(side_text)
                if m and section:
                    parsed.append((section, m.group(1).strip(), float(m.group(2))))
                    return None
                return f"{pending} {side_text}".strip() if pending else side_text.strip()

            for page in pdf.pages:
                words = page.extract_words(x_tolerance=1, y_tolerance=3)
                if not words:
                    continue
                for row in _cluster_lines(words):
                    row = sorted(row, key=lambda w: w["x0"])
                    row_text = " ".join(w["text"] for w in row)

                    if any(p in row_text for p in _PDF_RESET_MARKERS):
                        left_section = right_section = None
                        pending_left = pending_right = None
                        continue
                    if row_text.startswith(("Data ason", "Dataason")):
                        continue
                    if "Issuer Name Total" in row_text or "Security Name Total" in row_text:
                        continue

                    left_words = [w for w in row if w["x0"] < _PDF_COL_SPLIT_X]
                    right_words = [w for w in row if w["x0"] >= _PDF_COL_SPLIT_X]
                    left_text = " ".join(w["text"] for w in left_words)
                    right_text = " ".join(w["text"] for w in right_words)

                    m_single = _PDF_SECTION_RE.match(row_text)
                    m_left = _PDF_SECTION_RE.match(left_text) if left_text else None
                    m_right = _PDF_SECTION_RE.match(right_text) if right_text else None

                    if m_single:
                        left_section = right_section = m_single.group(1).strip()
                        pending_left = pending_right = None
                        continue
                    if m_left or m_right:
                        if m_left:
                            left_section = m_left.group(1).strip()
                        if m_right:
                            right_section = m_right.group(1).strip()
                        pending_left = pending_right = None
                        continue

                    pending_left = _emit(left_text, left_section, pending_left)
                    pending_right = _emit(right_text, right_section, pending_right)

        imported_at = datetime.utcnow()
        rows: list[dict[str, Any]] = []
        for section, sec_name, pct_val in parsed:
            # Some editions overlay a credit-quality/allocation donut-chart
            # legend near the Fixed Income table, at a y-position close
            # enough to the table rows that it gets clustered into the same
            # line and mis-paired as a "security" by the pending-name
            # buffer (e.g. name="9.3%" paired with an unrelated stray
            # value). A "name" with no letters is never a real security —
            # drop it rather than insert visibly wrong rows. (Do NOT guard
            # on magnitude alone: legitimate TREPS/cash positions routinely
            # exceed 20-30% of NAV in these funds.)
            if not any(c.isalpha() for c in sec_name) or sec_name in _PDF_KNOWN_CAPTIONS:
                logger.warning("Altiva SIF PDF %s: dropping non-security row %r (%.2f%%)", title, sec_name, pct_val)
                continue

            asset_type = _PDF_SECTION_ASSET_TYPE.get(section.upper(), "other")
            if pct_val < 0:
                asset_type = "other"  # short positions
            # The same issuer can appear across Equity/Options/Futures/etc. in
            # this fact-sheet (long equity + short future on the same stock,
            # say) — the synthetic ISIN must be scoped by section too, or the
            # ReplacingMergeTree table (keyed on scheme_code, as_of_month,
            # isin) silently collapses the distinct positions into one.
            rows.append({
                "scheme_code": scheme_code,
                "fund_name": fund_name,
                "as_of_month": as_of_date,
                "isin": _deterministic_isin(f"{section}|{sec_name}"),
                "security_name": sec_name,
                "asset_type": asset_type,
                "market_value_cr": round(pct_val / 100.0 * aum_cr, 4),
                "pct_of_nav": round(pct_val, 4),
                "imported_at": imported_at,
            })
        return rows

    def _load_workbook_bytes(self, url_or_path: str, http: httpx.Client) -> bytes:
        if not url_or_path.startswith(("http://", "https://")):
            from pathlib import Path

            path = Path(url_or_path)
            if not path.exists():
                raise FileNotFoundError(f"Local file not found: {url_or_path}")
            return path.read_bytes()

        safe_url = url_or_path.replace(" ", "%20")
        resp = http.get(safe_url, headers=_HEADERS)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} downloading {url_or_path}")
        return resp.content

    def _parse_sheet(
        self,
        excel: pd.ExcelFile,
        sheet_name: str,
        fallback_date: date,
    ) -> list[dict[str, Any]]:
        """Parse one scheme tab of an Altiva SIF statutory workbook.

        Column layout (0-indexed, col 0 is always blank):
          1=Name of the Instrument, 2=ISIN, 3=Rating/Industry, 4=Quantity,
          5=Market/Fair Value (Rs. In Lacs), 6=% to Net Assets (fraction,
          e.g. 0.0566 == 5.66%), 7=Yield.

        The statement is grouped into labelled sections (Equity & Equity
        related / Debt Instruments / Money Market Instruments / TREPS /
        Derivatives) rather than a single flat table — asset_type is
        assigned from the section header rather than per-security keyword
        matching, which is more reliable for this format.
        """
        df = excel.parse(sheet_name, header=None)
        if len(df) < 10 or df.shape[1] < 7:
            return []

        banner_text = " ".join(
            " ".join(str(x) for x in df.iloc[r].values if pd.notna(x))
            for r in range(min(4, len(df)))
        )
        as_of_date = _parse_banner_date(banner_text) or fallback_date
        scheme_code, fund_name = resolve_fund_identity(banner_text or sheet_name)

        rows: list[dict[str, Any]] = []
        current_section = "equity"
        imported_at = datetime.utcnow()

        for i in range(len(df)):
            row = df.iloc[i]
            name_val = row.iloc[1] if len(row) > 1 else None
            if pd.isna(name_val):
                continue
            sec_name = str(name_val).strip()
            if len(sec_name) < 3:
                continue
            sec_upper = sec_name.upper()

            mval_raw = row.iloc[5] if len(row) > 5 else None
            pct_raw = row.iloc[6] if len(row) > 6 else None

            # ── Section-header / label rows (no market value or % NAV) ────
            if pd.isna(mval_raw) and pd.isna(pct_raw):
                if "DEBT INSTRUMENT" in sec_upper:
                    current_section = "bond"
                elif any(k in sec_upper for k in ("MONEY MARKET", "TREASURY BILL", "TREPS", "REVERSE REPO")):
                    current_section = "cash"
                elif any(k in sec_upper for k in ("DERIVATIVE", "FUTURE", "OPTION")):
                    current_section = "other"
                elif "EQUITY" in sec_upper:
                    current_section = "equity"
                continue

            if any(kw in sec_upper for kw in ("TOTAL", "SUBTOTAL", "NET ASSETS")):
                continue

            try:
                mval_cr = float(mval_raw) / 100.0  # Lacs → Cr
                pct_val = float(pct_raw) * 100.0   # fraction → percent
            except (TypeError, ValueError):
                continue

            isin_val = str(row.iloc[2]).strip() if len(row) > 2 and pd.notna(row.iloc[2]) else ""
            if not isin_val or isin_val.lower() in ("nan", "nil", "-", "none", "n.a.", "na") or len(isin_val) < 6:
                isin_val = _deterministic_isin(sec_name)

            asset_type = current_section
            if mval_cr < 0 or pct_val < 0:
                asset_type = "other"  # short positions

            rows.append({
                "scheme_code": scheme_code,
                "fund_name": fund_name,
                "as_of_month": as_of_date,
                "isin": isin_val,
                "security_name": sec_name,
                "asset_type": asset_type,
                "market_value_cr": round(mval_cr, 4),
                "pct_of_nav": round(pct_val, 4),
                "imported_at": imported_at,
            })

        return rows
