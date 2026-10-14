from __future__ import annotations

import time as time_module
from unittest.mock import MagicMock, patch

import pytest

from src.data_importer.fetchers import stockscans_fetcher as ss
from src.data_importer.fetchers.stockscans_fetcher import (
    _as_float,
    _clean_symbols,
    fetch_stockscans_fundamentals,
    fetch_stockscans_insider_trades,
    fetch_stockscans_peer_valuation,
    fetch_stockscans_shareholding_patterns,
)


@pytest.fixture(autouse=True)
def _reset_shared_rate_limiter():
    """The throttle/breaker gate is module-global; isolate tests from each other."""
    ss._next_slot_at = 0.0
    ss._backoff_until = 0.0
    yield
    ss._next_slot_at = 0.0
    ss._backoff_until = 0.0


def _response(payload: dict, status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def test_symbol_cleaning_is_stable_and_de_duplicates() -> None:
    assert _clean_symbols([("RELIANCE", "RELIANCE.NS"), "TCS", "NSE:RELIANCE"]) == ["RELIANCE", "TCS"]


def test_number_parser_accepts_stockscans_display_values() -> None:
    assert _as_float("1,234.5") == 1234.5
    assert _as_float("12.5%") == 12.5
    assert _as_float("₹ 1,234 Cr") == 1234.0
    assert _as_float("—") == 0.0
    assert _as_float("not-a-number") == 0.0


@patch("src.data_importer.fetchers.stockscans_fetcher.time.sleep")
@patch("src.data_importer.fetchers.stockscans_fetcher._session")
def test_fundamentals_reuses_session_and_skips_duplicate_symbols(
    mock_session_factory: MagicMock,
    _mock_sleep: MagicMock,
) -> None:
    session = MagicMock()
    mock_session_factory.return_value = session
    session.get.side_effect = [
        _response({
            "currentRatios": {"Market Capitalization": "1,234.5"},
            "quarterly": [["Date", "Revenue", "PAT Growth YoY"], ["2026-03-31", "9,876", "12.5%"]],
            "yearly": [],
        }),
        _response({"currentRatios": {}, "quarterly": [], "yearly": []}),
    ]

    valuations, statements = fetch_stockscans_fundamentals(
        [("RELIANCE", "RELIANCE.NS"), "TCS", "RELIANCE"],
        delay_between_calls=0,
    )

    assert session.get.call_count == 2
    assert [row["symbol"] for row in valuations] == ["RELIANCE", "TCS"]
    assert statements[0]["revenue"] == 9876.0
    assert statements[0]["pat_growth_yoy"] == 12.5
    session.close.assert_called_once()


def test_peer_valuation_uses_supplied_session_and_parses_formatted_numbers() -> None:
    session = MagicMock()
    session.post.return_value = _response({
        "companies": [{
            "companyId": "NSE:RELIANCE",
            "Market Capitalization": "2,500",
            "ROE": "15.1%",
        }]
    })

    rows = fetch_stockscans_peer_valuation("NSE:RELIANCE", session=session)

    assert rows[0]["market_cap"] == 2500.0
    assert rows[0]["return_on_equity"] == 15.1
    session.post.assert_called_once()


@patch("src.data_importer.fetchers.stockscans_fetcher._session")
def test_insider_batch_reuses_session_and_normalizes_values(mock_session_factory: MagicMock) -> None:
    session = MagicMock()
    mock_session_factory.return_value = session
    session.get.return_value = _response({"insiderTrading": [{
        "date": "2026-01-01", "shareQuantity": "1,000", "valueOfSecurities": "₹ 20 Cr",
    }]})

    rows = fetch_stockscans_insider_trades(["RELIANCE", "NSE:RELIANCE"], delay_between_calls=0)

    assert len(rows) == 1
    assert rows[0]["shares"] == 1000.0
    assert rows[0]["value"] == 20.0
    session.get.assert_called_once()
    session.close.assert_called_once()


def test_fundamentals_concurrent_mode_preserves_symbol_order() -> None:
    """max_workers > 1 must still return rows in input-symbol order, even
    though the underlying ThreadPoolExecutor completes them out of order."""
    payloads = {
        "RELIANCE": {"currentRatios": {"Market Capitalization": "100"}, "quarterly": [], "yearly": []},
        "TCS": {"currentRatios": {"Market Capitalization": "200"}, "quarterly": [], "yearly": []},
        "INFY": {"currentRatios": {"Market Capitalization": "300"}, "quarterly": [], "yearly": []},
    }

    def fake_get(url, timeout=None):
        sym = url.rsplit("/", 2)[1].split(":")[-1]
        return _response(payloads[sym])

    session = MagicMock()
    session.get.side_effect = fake_get

    with patch.object(ss, "_session", return_value=session):
        valuations, _ = fetch_stockscans_fundamentals(
            ["RELIANCE", "TCS", "INFY"], max_workers=3,
        )

    assert [row["symbol"] for row in valuations] == ["RELIANCE", "TCS", "INFY"]
    assert [row["market_cap"] for row in valuations] == [100.0, 200.0, 300.0]
    session.close.assert_called_once()


def test_trip_breaker_pauses_every_caller_until_cooldown_elapses() -> None:
    """A 429 seen by any one worker must stall every subsequent _throttle()
    call process-wide, not just retries within that worker's own loop."""
    assert ss._backoff_until == 0.0
    ss._trip_breaker(cooldown=0.1)
    assert ss._backoff_until > time_module.monotonic()

    t0 = time_module.monotonic()
    ss._throttle()
    assert time_module.monotonic() - t0 >= 0.08


@patch("src.data_importer.fetchers.stockscans_fetcher._session")
def test_shareholding_batch_reuses_session_and_parses_formatted_percentages(
    mock_session_factory: MagicMock,
) -> None:
    session = MagicMock()
    mock_session_factory.return_value = session
    session.get.return_value = _response({
        "aggregate": [["Metric", "202603"], ["Promoter", "50.5%"], ["No Of Shareholders", "1,000"]],
        "details": {},
    })

    patterns, entities = fetch_stockscans_shareholding_patterns(
        ["RELIANCE", "NSE:RELIANCE"], delay_between_calls=0
    )

    assert entities == []
    assert patterns[0]["promoter_pct"] == 50.5
    assert patterns[0]["no_of_shareholders"] == 1000
    session.get.assert_called_once()
    session.close.assert_called_once()
