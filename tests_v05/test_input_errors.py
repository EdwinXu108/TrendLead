"""Fix 3: input error/fatal issues fail the run; runtime errors become structured failures."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from leadtrend_v05.dataio import EventRow
from leadtrend_v05.pipeline import run_pipeline
from leadtrend_v05.report import write_outputs
from tests_v05.conftest import (
    cal_row,
    make_config,
    make_inputs,
    member,
    price,
    sec,
    snap,
    utc,
)


def _days(n: int = 20, start: date = date(2025, 1, 2)) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _mini_inputs(days, securities, prices, snaps, members, events=None, issues=None):
    cfg = make_config(L=3, N=2, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        events=events,
        config=cfg,
    )
    if issues is not None:
        inputs.data_issues = list(issues)
    return inputs


def test_duplicate_price_row_fails_run():
    days = _days()
    securities = [sec("S1", issuer="I1"), sec("SYN_VOO", stype="etf", ticker="VOO")]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 20.0 + j))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    # inject a duplicate price row via data_issues path: load_inputs would create this;
    # simulate the issue list that load_inputs produces.
    issues = [
        {
            "category": "prices",
            "severity": "error",
            "security_id": "S1",
            "snapshot_id": "",
            "event_id": "",
            "session_date": days[3].isoformat(),
            "message": "duplicate price key",
            "context": "prices.csv:10",
        }
    ]
    snaps = [snap("SNAP_A", days[3], utc(2025, 1, 10, 12, 0))]
    members = [member("SNAP_A", "S1")]
    inputs = _mini_inputs(days, securities, prices, snaps, members, issues=issues)
    result = run_pipeline(inputs)
    assert result.status == "failed"
    assert any(
        i.get("message") == "duplicate price key" and i.get("severity") == "error"
        for i in result.data_issues
    )


def test_duplicate_event_id_fails_run():
    days = _days()
    securities = [sec("S1", issuer="I1"), sec("SYN_VOO", stype="etf", ticker="VOO")]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 20.0 + j))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    issues = [
        {
            "category": "events",
            "severity": "error",
            "security_id": "S1",
            "snapshot_id": "",
            "event_id": "EV1",
            "session_date": "",
            "message": "duplicate event_id",
            "context": "events.csv:5",
        }
    ]
    snaps = [snap("SNAP_A", days[3], utc(2025, 1, 10, 12, 0))]
    members = [member("SNAP_A", "S1")]
    inputs = _mini_inputs(days, securities, prices, snaps, members, issues=issues)
    result = run_pipeline(inputs)
    assert result.status == "failed"
    assert any("duplicate event_id" in i.get("message", "") for i in result.data_issues)


def test_unknown_snapshot_reference_fails_run():
    days = _days()
    securities = [sec("S1"), sec("SYN_VOO", stype="etf", ticker="VOO")]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 20.0 + j))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    issues = [
        {
            "category": "universe",
            "severity": "error",
            "security_id": "S1",
            "snapshot_id": "NOPE",
            "event_id": "",
            "session_date": "",
            "message": "snapshot_members references unknown snapshot_id",
            "context": "snapshot_members.csv:9",
        }
    ]
    snaps = [snap("SNAP_A", days[3], utc(2025, 1, 10, 12, 0))]
    members = [member("SNAP_A", "S1")]
    inputs = _mini_inputs(days, securities, prices, snaps, members, issues=issues)
    result = run_pipeline(inputs)
    assert result.status == "failed"


def test_info_issue_does_not_fail_run():
    days = _days()
    securities = [sec("S1"), sec("SYN_VOO", stype="etf", ticker="VOO")]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 20.0 + j))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    issues = [
        {
            "category": "universe",
            "severity": "info",
            "security_id": "",
            "snapshot_id": "SNAP_X",
            "event_id": "",
            "session_date": "2025-07-15",
            "message": "date-precision available_at has no later session in calendar",
            "context": "snapshots.csv:5",
        }
    ]
    snaps = [snap("SNAP_A", days[3], utc(2025, 1, 10, 12, 0))]
    members = [member("SNAP_A", "S1")]
    inputs = _mini_inputs(days, securities, prices, snaps, members, issues=issues)
    result = run_pipeline(inputs)
    assert result.status == "ok"


def test_reverse_split_non_integer_shares_fails_structured():
    """7 shares with 3->1 reverse split yields 7/3: must fail, not crash.

    Deterministic: capital 100_000, single name at price 14_000, N=1
    => ideal = floor((100000-1)/14000) = 7 shares.
    """
    days = _days(25)
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", stype="etf", ticker="VOO"),
    ]
    prices = []
    for j, d in enumerate(days):
        # S1 flat at 14_000 so hand-calc buy = floor((100000-1)/14000) = 7 shares
        prices.append(price("S1", d, 14000.0))
        prices.append(price("S2", d, 40.0 + j * 0.1))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    split_day = days[14]  # after the first build trade, while S1 is still held
    events = [
        EventRow(
            event_id="EV_RSPLIT",
            security_id="S1",
            event_type="reverse_split",
            effective_session=split_day,
            shares_new_per_old=1.0 / 3.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    snaps = [snap("SNAP_A", days[4], utc(days[4].year, days[4].month, days[4].day, 12, 0))]
    # Only S1 is in the universe so N=1 must buy it (scenario guaranteed).
    members = [member("SNAP_A", "S1")]
    cfg = make_config(
        L=3, N=1, k=1, b=1.0,
        fee_per_trade=1.0,
        slippage_bps=0.0,
        benchmark_security_id="SYN_VOO",
    )
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        events=events,
        config=cfg,
    )
    result = run_pipeline(inputs)

    # The scenario must have happened: S1 was bought with exactly 7 shares.
    buys_s1 = [
        t for t in result.trade_rows if t["security_id"] == "S1" and t["side"] == "buy"
    ]
    assert buys_s1, "expected S1 to be bought before the reverse split"
    assert int(buys_s1[0]["shares"]) == 7, "hand-check: 7 shares expected"

    assert result.status == "failed"
    assert result.failure_context is not None
    msg = result.summary.get("failure_message", "")
    assert "non-integer" in msg or "ValueError" in msg or "split" in msg.lower()
    assert any(i.get("severity") == "fatal" for i in result.data_issues)


def test_cash_dividend_missing_pay_date_is_error_in_loader(tmp_path: Path):
    import csv

    from leadtrend_v05.config import load_config
    from leadtrend_v05.dataio import load_inputs

    root = tmp_path
    (root / "data" / "v05" / "fixtures").mkdir(parents=True)
    fix = root / "data" / "v05" / "fixtures"
    (fix / "calendar.csv").write_text(
        "session_date,open_time_utc,close_time_utc,is_early_close,note\n"
        "2025-01-02,2025-01-02T14:30:00Z,2025-01-02T21:00:00Z,0,\n"
        "2025-01-03,2025-01-03T14:30:00Z,2025-01-03T21:00:00Z,0,\n",
        encoding="utf-8",
    )
    (fix / "securities.csv").write_text(
        "security_id,issuer_id,ticker,ticker_valid_from,ticker_valid_to,security_type,exchange,industry,industry_source_version,currency\n"
        "S1,I1,S1,2025-01-02,,common_stock,NASDAQ,TECH,v1,USD\n",
        encoding="utf-8",
    )
    (fix / "snapshots.csv").write_text(
        "snapshot_id,holdings_as_of,available_at,availability_precision,source,source_hash,retrieved_at,revision\n"
        "S,2025-01-02,2025-01-02T12:00:00Z,timestamp,src,h,2025-01-02T12:00:00Z,r0\n",
        encoding="utf-8",
    )
    (fix / "snapshot_members.csv").write_text(
        "snapshot_id,security_id,original_name,weight\nS,S1,S1,\n",
        encoding="utf-8",
    )
    (fix / "prices.csv").write_text(
        "security_id,session_date,open,close,volume,turnover,currency\n"
        "S1,2025-01-02,10,10,1,10,USD\n"
        "S1,2025-01-03,10,10,1,10,USD\n",
        encoding="utf-8",
    )
    (fix / "events.csv").write_text(
        "event_id,security_id,event_type,effective_session,shares_new_per_old,cash_amount_per_share,cash_basis,pay_date,source\n"
        "EV_DIV,S1,cash_dividend,2025-01-03,,0.5,per_share_at_entitlement,,test\n",
        encoding="utf-8",
    )
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "v05_bootstrap.json").write_text(
        '{"dataset_kind":"synthetic","L":2,"N":1,"k":1,"b":1,"benchmark_security_id":"SYN_VOO","input_dir":"data/v05/fixtures"}',
        encoding="utf-8",
    )
    cfg = load_config(root / "config" / "v05_bootstrap.json")
    inputs = load_inputs(cfg, root)
    assert any(
        i.get("severity") == "error" and "pay_date" in i.get("message", "")
        for i in inputs.data_issues
    )
    result = run_pipeline(inputs)
    assert result.status == "failed"


def test_failed_run_still_writes_diagnostics(tmp_path: Path):
    days = _days()
    securities = [sec("S1"), sec("SYN_VOO", stype="etf", ticker="VOO")]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 20.0 + j))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    issues = [
        {
            "category": "prices",
            "severity": "error",
            "security_id": "S1",
            "snapshot_id": "",
            "event_id": "",
            "session_date": days[1].isoformat(),
            "message": "duplicate price key",
            "context": "prices.csv:3",
        }
    ]
    snaps = [snap("SNAP_A", days[3], utc(2025, 1, 10, 12, 0))]
    members = [member("SNAP_A", "S1")]
    inputs = _mini_inputs(days, securities, prices, snaps, members, issues=issues)
    result = run_pipeline(inputs)
    assert result.status == "failed"
    out = tmp_path / "fail_out"
    write_outputs(result, out)
    assert (out / "data_issues.csv").is_file()
    text = (out / "data_issues.csv").read_text(encoding="utf-8")
    assert "duplicate price key" in text
    assert (out / "summary.json").is_file()
    assert (out / "run_manifest.json").is_file()
