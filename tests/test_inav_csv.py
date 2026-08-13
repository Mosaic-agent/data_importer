"""
tests/test_inav_csv.py
──────────────────────
Round-trip tests for data_importer.inav_csv.
"""

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from data_importer.inav_csv import (
    CSV_COLUMNS,
    inav_csv_path,
    read_inav,
    write_inav_rows,
)


@pytest.fixture
def sample_rows():
    return [
        {"symbol": "GOLDBEES", "snapshot_at": datetime(2026, 8, 4, 9, 15, 0),
         "inav": 82.15, "market_price": 82.30, "premium_discount_pct": 0.1826, "source": "NSE"},
        {"symbol": "GOLDBEES", "snapshot_at": datetime(2026, 8, 4, 15, 30, 0),
         "inav": 82.42, "market_price": 82.50, "premium_discount_pct": 0.0971, "source": "NSE"},
        {"symbol": "GOLDBEES", "snapshot_at": datetime(2026, 8, 5, 9, 15, 0),
         "inav": 82.60, "market_price": 82.55, "premium_discount_pct": -0.0605, "source": "NSE"},
        {"symbol": "SILVERBEES", "snapshot_at": datetime(2026, 8, 4, 9, 15, 0),
         "inav": 105.30, "market_price": 105.45, "premium_discount_pct": 0.1424, "source": "Yahoo"},
    ]


def test_inav_csv_path_layout(tmp_path):
    path = inav_csv_path("goldbees", datetime(2026, 8, 4, 9, 15), base_dir=tmp_path)
    assert path == tmp_path / "GOLDBEES" / "2026" / "08" / "04.csv"


def test_write_creates_expected_files(tmp_path, sample_rows):
    written = write_inav_rows(sample_rows, base_dir=tmp_path)
    expected = {
        tmp_path / "GOLDBEES" / "2026" / "08" / "04.csv": 2,
        tmp_path / "GOLDBEES" / "2026" / "08" / "05.csv": 1,
        tmp_path / "SILVERBEES" / "2026" / "08" / "04.csv": 1,
    }
    assert written == expected
    for path in expected:
        assert path.exists()


def test_write_appends_and_deduplicates_header(tmp_path, sample_rows):
    write_inav_rows(sample_rows[:1], base_dir=tmp_path)
    write_inav_rows(sample_rows[1:2], base_dir=tmp_path)
    path = tmp_path / "GOLDBEES" / "2026" / "08" / "04.csv"
    lines = path.read_text().splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS), "Header must appear once"
    assert len(lines) == 3, f"Header + 2 rows expected, got {len(lines)}"


def test_read_returns_sorted_dataframe(tmp_path, sample_rows):
    write_inav_rows(sample_rows, base_dir=tmp_path)
    df = read_inav("GOLDBEES", start="2026-08-01", end="2026-08-31", base_dir=tmp_path)
    assert len(df) == 3
    assert list(df.columns) == CSV_COLUMNS
    assert df["snapshot_at"].is_monotonic_increasing
    assert df["source"].iloc[0] == "NSE"


def test_read_date_range_filter(tmp_path, sample_rows):
    write_inav_rows(sample_rows, base_dir=tmp_path)
    df = read_inav("GOLDBEES", start="2026-08-05", end="2026-08-05", base_dir=tmp_path)
    assert len(df) == 1
    assert df["snapshot_at"].iloc[0].date().isoformat() == "2026-08-05"


def test_read_missing_symbol_returns_empty_frame(tmp_path):
    df = read_inav("NONEXISTENT", start="2026-01-01", base_dir=tmp_path)
    assert df.empty
    assert list(df.columns) == CSV_COLUMNS


def test_write_converts_naive_utc_to_ist(tmp_path):
    # Fetchers hand naive-UTC timestamps; the CSV must persist naive IST (UTC+5:30).
    row = {"symbol": "GOLDBEES", "snapshot_at": datetime(2026, 8, 4, 3, 45, 0),
           "inav": 82.15, "market_price": 82.30, "premium_discount_pct": 0.1826, "source": "NSE"}
    write_inav_rows([row], base_dir=tmp_path)
    df = read_inav("GOLDBEES", start="2026-08-01", end="2026-08-31", base_dir=tmp_path)
    assert df["snapshot_at"].iloc[0] == pd.Timestamp(2026, 8, 4, 9, 15, 0)


def test_write_rolls_folder_date_across_ist_midnight(tmp_path):
    # 19:00 UTC -> 00:30 IST the next calendar day; folder must follow IST, not UTC.
    row = {"symbol": "GOLDBEES", "snapshot_at": datetime(2026, 8, 4, 19, 0, 0),
           "inav": 82.15, "market_price": 82.30, "premium_discount_pct": 0.1826, "source": "NSE"}
    written = write_inav_rows([row], base_dir=tmp_path)
    assert tmp_path / "GOLDBEES" / "2026" / "08" / "05.csv" in written
