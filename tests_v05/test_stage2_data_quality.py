"""Tests that exercise the actual Stage-2 normalizers and validator offline."""

from __future__ import annotations

import csv
import json
import math
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from leadtrend_v05.dataio import load_inputs
from leadtrend_v05.opend_prices import (holding_key, normalize_dividend_rows,
                                        normalize_rehab_rows, parse_dividend_statement)
from leadtrend_v05.scoring import momentum_score
from leadtrend_v05.stage2_validate import run_validate
from conftest import make_config


def test_dividend_only_rehab_row_does_not_create_split():
    gaps = []
    rows = normalize_rehab_rows([
        {"ex_div_date": "2024-05-10", "split_base": None, "split_ert": float("nan"),
         "join_base": None, "join_ert": None, "per_cash_div": 0.25},
    ], "CUSIP:A", "US.A", date(2020, 1, 1), date(2026, 12, 31), gaps)
    assert rows == []


def test_rehab_split_reverse_and_spinoff_are_distinct():
    rows = normalize_rehab_rows([
        {"ex_div_date": "2020-08-31", "split_base": 1, "split_ert": 4},
        {"ex_div_date": "2021-08-02", "split_base": float("nan"), "split_ert": None,
         "join_base": 8, "join_ert": 1},
        {"ex_div_date": "2021-11-04", "spin_off_base": 100000000, "spin_off_ert": 104694000,
         "spin_off_ratio": 1.04694},
    ], "CUSIP:X", "US.X", date(2020, 1, 1), date(2026, 12, 31), [])
    assert [(r["event_type"], r["shares_new_per_old"]) for r in rows] == [
        ("split", "4"), ("reverse_split", "0.125"), ("spinoff", ""),
    ]
    assert "price_factor=1.04694" in rows[-1]["source"]


def test_vendor_split_fields_can_encode_verified_spinoff():
    for ticker, session, base, ert in [("IBM", "2021-11-04", 50000, 52347),
                                      ("MRK", "2021-06-03", 100000, 104873),
                                      ("DELL", "2021-11-02", 50000, 98839),
                                      ("BFH", "2021-11-08", 12500, 15671)]:
        rows = normalize_rehab_rows([{"ex_div_date": session, "split_base": base, "split_ert": ert}],
                                    "CUSIP:TEST", "US." + ticker, date(2019, 1, 1), date(2026, 12, 31), [])
        assert len(rows) == 1
        assert rows[0]["event_type"] == "spinoff"
        assert rows[0]["shares_new_per_old"] == ""


def test_identical_dividends_dedup_but_regular_plus_special_remain():
    items = [
        {"ex_date": "2022/11/17", "dividend_payable_date": "2022/12/01", "statement": "1股派息0.6125USD"},
        {"ex_date": "2022/11/17", "dividend_payable_date": "2022/12/01", "statement": "1股派息0.6125USD"},
        {"ex_date": "2022/11/17", "dividend_payable_date": "2022/12/01", "statement": "1股派息0.10USD"},
    ]
    gaps = []
    rows = normalize_dividend_rows(items, "CUSIP:X", "US.X", date(2022, 1, 1), date(2022, 12, 31), gaps)
    assert len(rows) == 2
    assert sum(g["message"] == "duplicate_cash_dividend_removed" for g in gaps) == 1
    assert len({r["event_id"] for r in rows}) == 2


def test_total_and_component_dividend_are_not_added_twice():
    items = [{"ex_date": "2024/01/11", "dividend_payable_date": "2024/02/01", "statement": f"1股派息{v}USD"}
             for v in ("0.075", "0.15")]
    gaps = []
    rows = normalize_dividend_rows(items, "CUSIP:35671D857", "US.FCX", date(2024, 1, 1), date(2024, 12, 31), gaps)
    assert len(rows) == 1 and float(rows[0]["cash_amount_per_share"]) == 0.15
    assert any(g["message"] == "dividend_component_overlap_removed" for g in gaps)


def test_bny_ticker_reuse_cannot_import_closed_end_fund_dividends():
    from leadtrend_v05.opend_prices import verify_bny_dividend_identity
    cash = [{"effective_session": "2024-04-25", "cash_amount_per_share": "0.42"},
            {"effective_session": "2024-04-15", "cash_amount_per_share": "0.043"}]
    rehab = [{"ex_div_date": "2024-04-25", "per_cash_div": 0.42},
             {"ex_div_date": "2019-10-28", "per_cash_div": 0.31}]
    gaps = []
    kept = verify_bny_dividend_identity(cash, rehab, "BANK", date(2019, 1, 1), date(2026, 12, 31), gaps)
    assert kept == [cash[0]]
    assert any(g["message"] == "wrong_issuer_dividend_quarantined" for g in gaps)
    assert any(g["message"] == "bank_dividend_missing_in_truncated_mixed_feed" and g["severity"] == "error" for g in gaps)


def test_shareclass_crosswalk_preserves_distinct_voting_classes(tmp_path):
    from leadtrend_v05.ticker_map import resolve_shareclass_links
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"OLD": {"ticker": "", "status": "no_hit"}}))
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "test.meta.json").write_text(json.dumps({"jobs": json.dumps([
        {"idType": "ID_CUSIP", "idValue": "OLD"}, {"idType": "ID_CUSIP", "idValue": "NEW"}])}))
    (raw / "test.json").write_text(json.dumps([
        {"data": [{"shareClassFIGI": "CLASS_A", "marketSector": "Equity", "exchCode": "JP", "ticker": "ABC"}]},
        {"data": [{"shareClassFIGI": "CLASS_A", "marketSector": "Equity", "exchCode": "US", "ticker": "A", "securityType": "Common Stock"},
                  {"shareClassFIGI": "CLASS_C", "marketSector": "Equity", "exchCode": "US", "ticker": "C", "securityType": "Common Stock"}]}]))
    resolve_shareclass_links(cache, raw)
    mapped = json.loads(cache.read_text())
    assert mapped["OLD"]["ticker"] == "A"
    assert "shareClassFIGI=CLASS_A" in mapped["OLD"]["evidence"]


def test_dividend_statement_supports_per_n_shares():
    assert parse_dividend_statement("10股派息1.25USD") == 0.125
    assert parse_dividend_statement("1股派息0.27USD") == 0.27


def _copy_fixture(tmp_path: Path) -> None:
    for path in Path("data/v05/fixtures").glob("*.csv"):
        shutil.copyfile(path, tmp_path / path.name)


def test_loader_rejects_nan_split_ratio(tmp_path: Path):
    _copy_fixture(tmp_path)
    path = tmp_path / "events.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0])
    split = next(r for r in rows if r["event_type"] in {"split", "reverse_split"})
    split["shares_new_per_old"] = "nan"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    inputs = load_inputs(make_config(input_dir=str(tmp_path)), Path("."))
    assert any("invalid numeric event" in i["message"] and i["severity"] == "error" for i in inputs.data_issues)


def test_loader_rejects_nan_price(tmp_path: Path):
    _copy_fixture(tmp_path)
    path = tmp_path / "prices.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0])
    rows[0]["close"] = "inf"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    inputs = load_inputs(make_config(input_dir=str(tmp_path)), Path("."))
    assert any("invalid numeric price" in i["message"] and i["severity"] == "error" for i in inputs.data_issues)


def test_non_finite_tri_is_unscorable_not_a_score():
    d1, d2 = date(2025, 1, 2), date(2025, 1, 3)
    score, _, _, _, reason = momentum_score({d1: 1.0, d2: math.nan}, d2, 1, [d1, d2])
    assert score is None
    assert reason == "non_finite_tri"


def test_non_finite_tri_inside_window_is_unscorable():
    days = [date(2025, 1, d) for d in (2, 3, 6)]
    score, _, _, _, reason = momentum_score(dict(zip(days, [1.0, math.nan, 2.0])), days[-1], 2, days)
    assert score is None and reason == "non_finite_tri"


def test_na_cusip_uses_isin_and_does_not_merge_members():
    a = SimpleNamespace(cusip="N/A", isin="US0000000001", lei="A", name="One")
    b = SimpleNamespace(cusip="N/A", isin="US0000000002", lei="B", name="Two")
    assert holding_key(a) != holding_key(b)
    assert holding_key(a).startswith("ISIN:")


def test_validate_coverage_denominator_includes_unpriced_member(tmp_path: Path):
    root = tmp_path
    normalized = root / "normalized"
    normalized.mkdir()
    start = date(2025, 1, 1)
    sessions = [start + timedelta(days=i) for i in range(30) if (start + timedelta(days=i)).weekday() < 5]
    def write(name, fields, rows):
        with (normalized / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    write("calendar.csv", ["session_date", "open_time_utc", "close_time_utc", "is_early_close", "note"], [
        {"session_date": d.isoformat(), "open_time_utc": f"{d.isoformat()}T14:30:00Z",
         "close_time_utc": f"{d.isoformat()}T21:00:00Z", "is_early_close": "0", "note": "test"} for d in sessions])
    write("securities.csv", ["security_id", "issuer_id", "ticker", "ticker_valid_from", "ticker_valid_to",
                              "security_type", "exchange", "industry", "industry_source_version", "currency", "source_evidence"], [
        {"security_id": sid, "issuer_id": f"ISS_{sid}", "ticker": sid, "ticker_valid_from": "", "ticker_valid_to": "",
         "security_type": stype, "exchange": "NASDAQ", "industry": "TECH", "industry_source_version": "test",
         "currency": "USD", "source_evidence": "test"}
        for sid, stype in [("A", "common_stock"), ("B", "common_stock"), ("VOO_ETF", "etf")]])
    write("snapshots.csv", ["snapshot_id", "holdings_as_of", "available_at", "availability_precision", "source",
                             "source_hash", "retrieved_at", "revision"], [
        {"snapshot_id": "S", "holdings_as_of": "2024-12-31", "available_at": "2024-12-31T15:00:00Z",
         "availability_precision": "timestamp", "source": "test", "source_hash": "x",
         "retrieved_at": "2025-01-01T00:00:00Z", "revision": "r"}])
    write("snapshot_members.csv", ["snapshot_id", "security_id", "original_name", "weight"], [
        {"snapshot_id": "S", "security_id": "A", "original_name": "A", "weight": "1"},
        {"snapshot_id": "S", "security_id": "B", "original_name": "B", "weight": "1"}])
    price_rows = []
    for sid in ("A", "VOO_ETF"):
        for index, d in enumerate(sessions, 1):
            price_rows.append({"security_id": sid, "session_date": d.isoformat(), "open": str(index),
                               "close": str(index), "volume": "100", "turnover": "1000", "currency": "USD"})
    write("prices.csv", ["security_id", "session_date", "open", "close", "volume", "turnover", "currency"], price_rows)
    write("events.csv", ["event_id", "security_id", "event_type", "effective_session", "shares_new_per_old",
                          "cash_amount_per_share", "cash_basis", "pay_date", "source"], [])
    config = {"dataset_kind": "real_partial", "input_dir": "normalized", "benchmark_security_id": "VOO_ETF",
              "L": 2, "N": 10, "k": 3, "b": 2, "score_coverage_gate": 0.95}
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    assert run_validate(root, config_path, root / "out") == 1
    with (root / "out" / "weekly_coverage.csv").open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    populated = [r for r in rows if int(r["n_eligible"]) > 0]
    assert populated
    assert all(r["n_eligible"] == "2" and r["n_scored"] in {"0", "1"} for r in populated)
    assert all(float(r["score_ratio"]) <= 0.5 for r in populated)
    with (root / "out" / "sample_scores.csv").open("r", encoding="utf-8") as handle:
        scores = list(csv.DictReader(handle))
    assert any(r["security_id"] == "B" and r["unscorable_reason"] == "no_price_data" for r in scores)


def test_history_kline_explicitly_requests_unadjusted_prices(tmp_path: Path, monkeypatch):
    import pandas as pd
    # Offline test must not import the SDK (import itself opens a user log).
    import sys
    AuType = SimpleNamespace(NONE="NONE")
    RET_OK = 0
    monkeypatch.setitem(sys.modules, "futu", SimpleNamespace(AuType=AuType, RET_OK=RET_OK))
    from leadtrend_v05.opend_prices import OpenDPriceFetcher

    class FakeContext:
        def __init__(self):
            self.kwargs = None

        def request_history_kline(self, code, **kwargs):
            self.kwargs = kwargs
            frame = pd.DataFrame([{"time_key": "2025-01-02", "open": 100.0, "close": 101.0,
                                   "high": 102.0, "low": 99.0, "volume": 1000, "turnover": 101000}])
            return RET_OK, frame, None

    fetcher = OpenDPriceFetcher(tmp_path)
    context = FakeContext()
    fetcher._ctx = context
    monkeypatch.setattr(fetcher, "_pace", lambda: None)
    outcome = fetcher.fetch_one("X", "CUSIP:X", "2025-01-01", "2025-01-03")
    assert outcome.ok and outcome.n_bars == 1
    assert context.kwargs["autype"] == AuType.NONE
    assert fetcher._cache_path("X", "2025-01-01", "2025-01-03").name.startswith("NONE_")


def test_share_class_ticker_conversion_preserves_class(tmp_path: Path):
    from leadtrend_v05.ticker_map import map_holdings_to_tickers

    holding = SimpleNamespace(cusip="084670702", isin="US0846707026", lei="X", name="Berkshire Hathaway Inc")
    cache = tmp_path / "figi.json"
    cache.write_text(json.dumps({"084670702": {"ticker": "BRK/B", "status": "resolved", "evidence": "test"}}),
                     encoding="utf-8")
    mapping, _ = map_holdings_to_tickers([holding], [], figi_cache=cache, use_openfigi=True)
    assert mapping["084670702"].ticker == "BRK.B"


def test_openfigi_uses_documented_identifier_types():
    from leadtrend_v05.ticker_map import _openfigi_job

    assert _openfigi_job("037833100") == {"idType": "ID_CUSIP", "idValue": "037833100"}
    assert _openfigi_job("ISIN:US0378331005") == {"idType": "ID_ISIN", "idValue": "US0378331005"}


def test_openfigi_cik_is_corroborated_without_switching_class(tmp_path):
    from leadtrend_v05.ticker_map import map_holdings_to_tickers
    holding = SimpleNamespace(cusip="02079K305", isin="US02079K3059", lei="X", name="Alphabet Inc")
    cache = tmp_path / "figi.json"
    cache.write_text(json.dumps({"02079K305": {"ticker": "GOOGL", "status": "resolved", "evidence": "CUSIP test"}}))
    rows = [{"ticker": "GOOG", "cik_str": 1652044, "title": "Alphabet Inc"},
            {"ticker": "GOOGL", "cik_str": 1652044, "title": "Alphabet Inc"}]
    mapping, _ = map_holdings_to_tickers([holding], rows, figi_cache=cache, use_openfigi=True)
    assert mapping["02079K305"].ticker == "GOOGL"
    assert mapping["02079K305"].cik == 1652044


def test_openfigi_retries_delisted_identifiers_explicitly(tmp_path, monkeypatch):
    from leadtrend_v05 import ticker_map
    holding = SimpleNamespace(cusip="00507V109", isin="US00507V1098", lei="X", name="Activision")
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"00507V109": {"ticker": "", "status": "no_hit"}}))
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps([{"data": [{"ticker": "ATVI", "exchCode": "US", "securityType": "Common Stock", "marketSector": "Equity"}]}]).encode()
    def fetch(request, **kwargs):
        requests.append(json.loads(request.data))
        return Response()
    monkeypatch.setattr(ticker_map.urllib.request, "urlopen", fetch)
    result = ticker_map.prefetch_openfigi([holding], cache, tmp_path / "raw", include_unlisted=True)
    assert requests[0][0]["includeUnlistedEquities"] is True
    assert result["00507V109"]["ticker"] == "ATVI"
    ticker_map.prefetch_openfigi([holding], cache, tmp_path / "raw", include_unlisted=True)
    assert len(requests) == 1


def test_missing_market_quantity_is_not_fabricated_zero():
    import pytest
    from leadtrend_v05.opend_prices import optional_market_number
    assert optional_market_number("") is None
    assert optional_market_number(0) == 0
    for value in ("nan", "inf", "oops", -1):
        with pytest.raises(ValueError):
            optional_market_number(value)
