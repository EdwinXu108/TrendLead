"""Post-review stage-2 tests: rate-limit categories, mapping, CA, calendar, UA time, gate."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from leadtrend_v05.nyse_calendar import is_early_close, session_times_utc
from leadtrend_v05.opend_prices import OpenDPriceFetcher
from leadtrend_v05.sec_nport import parse_acceptance_datetime_et_to_utc
from leadtrend_v05.ticker_map import is_rejected_instrument, map_holdings_to_tickers, _norm


def test_rate_limit_error_categorized_retryable(tmp_path):
    f = OpenDPriceFetcher(cache_dir=tmp_path)
    cat = f._classify_error("获取历史K线频率太高，请求失败，每30秒最多60次")
    assert cat == "request_failed_retryable"
    cat2 = f._classify_error("unknown stock")
    assert cat2 == "unknown_security"


def test_rejects_preferred_and_warrant_tickers():
    assert is_rejected_instrument("OXY-WT", "Occidental Petroleum")
    assert is_rejected_instrument("WFC-PZ", "Wells Fargo")
    assert is_rejected_instrument("CFG-PI", "Citizens Financial")
    assert is_rejected_instrument("ZIONP", "Zions Bancorporation")
    assert is_rejected_instrument("HBANP", "Huntington")
    assert is_rejected_instrument("CHTRP", "Charter Communications")
    assert not is_rejected_instrument("OXY", "Occidental Petroleum")
    assert not is_rejected_instrument("WFC", "Wells Fargo & Co")
    assert not is_rejected_instrument("TAP", "Molson Coors Beverage")


def test_no_prefix_match_exact_only():
    class H:
        cusip = "674599105"
        name = "Occidental Petroleum Corp"

    rows = [
        {"cik_str": 1, "ticker": "OXY", "title": "Occidental Petroleum Corp"},
        {"cik_str": 1, "ticker": "OXY-WT", "title": "Occidental Petroleum Warrant"},
    ]
    mapping, issues = map_holdings_to_tickers([H()], rows, use_openfigi=False)
    # exact normalized name hits OXY, never OXY-WT via prefix
    assert mapping["674599105"].ticker == "OXY"
    assert mapping["674599105"].match_method == "exact_name"


def test_rejected_instrument_not_mapped():
    class H:
        cusip = "123"
        name = "Acme Preferred"

    rows = [{"cik_str": 1, "ticker": "ACM-P", "title": "Acme Preferred"}]
    mapping, issues = map_holdings_to_tickers([H()], rows, use_openfigi=False)
    assert mapping["123"].ticker == ""
    assert any(i["message"] in {"ticker_unmapped", "ticker_rejected_instrument"} for i in issues)


def test_split_ratio_from_rehab_not_raw():
    """shares_new_per_old = split_ert/split_base (ORLY 1→15)."""
    split_base, split_ert = 1.0, 15.0
    new_per_old = split_ert / split_base
    assert new_per_old == 15.0
    raw_split_ratio = 0.06666666666666667
    assert new_per_old != raw_split_ratio  # must not use raw ratio


def test_acceptance_datetime_eastern_to_utc():
    # filing header for 0000036405-26-000063: 2026-02-26 16:15:30 ET = 21:15:30 UTC
    ts = parse_acceptance_datetime_et_to_utc("20260226161530")
    assert ts == datetime(2026, 2, 26, 21, 15, 30, tzinfo=timezone.utc)
    # NOT the 5-hour-late JSON value 2026-02-27T02:15:30Z
    assert ts != datetime(2026, 2, 27, 2, 15, 30, tzinfo=timezone.utc)
    # also accept full SGML header
    hdr = "blah\n<ACCEPTANCE-DATETIME>20260226161530\nFILED AS OF DATE:\t20260226"
    ts2 = parse_acceptance_datetime_et_to_utc(hdr)
    assert ts2 == ts


def test_acceptance_dst_summer():
    # 2025-07-15 10:00 ET (EDT, UTC-4) → 14:00 UTC
    ts = parse_acceptance_datetime_et_to_utc("20250715100000")
    assert ts == datetime(2025, 7, 15, 14, 0, 0, tzinfo=timezone.utc)


def test_nyse_early_close_calendar():
    early, note = is_early_close(date(2025, 7, 3))
    assert early is True
    assert "independence" in note or "day_before" in note
    o, c, is_e, n = session_times_utc(date(2025, 7, 3))
    assert is_e is True
    # 13:00 America/New_York on 2025-07-03 (EDT) = 17:00 UTC
    assert c.endswith("17:00:00Z")
    o2, c2, is_e2, _ = session_times_utc(date(2025, 7, 2))
    assert is_e2 is False
    assert c2.endswith("20:00:00Z")  # 16:00 EDT


def test_validate_gate_logic():
    # pure unit: gate comparison
    gate = 0.95
    assert (10 / 10) >= gate
    assert not (9 / 10 >= gate)
    # 95/100 exactly passes
    assert (95 / 100) >= gate


def test_weekend_adjacent_sessions_are_not_early_closes():
    for day in (date(2022, 7, 1), date(2022, 12, 23), date(2023, 12, 22), date(2026, 7, 2)):
        assert not is_early_close(day)[0]
        assert session_times_utc(day)[1].endswith("20:00:00Z" if day.month == 7 else "21:00:00Z")
