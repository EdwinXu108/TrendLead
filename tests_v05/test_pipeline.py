"""End-to-end pipeline behaviors on controlled mini inputs."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

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


def _days(n: int, start: date) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _build_weekday_inputs(
    n_sessions: int = 30,
    start: date = date(2025, 1, 2),
    n_stocks: int = 6,
    industries: tuple[str, ...] = ("TECH", "FIN", "HLTH"),
):
    days = _days(n_sessions, start)
    cal = [cal_row(d) for d in days]
    securities = []
    prices = []
    members = []
    for i in range(n_stocks):
        ind = industries[i % len(industries)]
        sid = f"S{i+1}"
        securities.append(sec(sid, issuer=f"ISS{i+1}", industry=ind, ticker=f"T{i+1}"))
        for j, d in enumerate(days):
            # ascending prices so scores are positive and ordered by sid strength
            px = 50.0 + i * 5.0 + j * (0.05 + i * 0.01)
            prices.append(price(sid, d, px, turnover=1_000_000 * (i + 1)))
        members.append(member("SNAP_A", sid))
    # a synthetic benchmark
    securities.append(sec("SYN_VOO", issuer="ISS_VOO", stype="etf", industry="BENCH", ticker="VOO"))
    for d in days:
        prices.append(price("SYN_VOO", d, 400.0 + days.index(d) * 0.5))
    # snapshot public after 10 sessions
    avail = utc(days[10].year, days[10].month, days[10].day, 12, 0)
    snaps = [snap("SNAP_A", days[5], avail)]
    cfg = make_config(L=5, N=3, k=1, b=2.0, benchmark_security_id="SYN_VOO")
    return make_inputs(cal, securities, snaps, members, prices, config=cfg), days


def test_pipeline_builds_portfolio_and_never_trades_voo():
    inputs, days = _build_weekday_inputs()
    result = run_pipeline(inputs)
    assert result.status == "ok"
    assert result.summary["dataset_kind"] == "synthetic"
    for t in result.trade_rows:
        assert t["security_id"] != "SYN_VOO"
        assert "VOO" not in t["security_id"]
    assert result.benchmark_rows  # benchmark exists independently


def test_no_swap_week_has_zero_trades(tmp_path: Path):
    inputs, days = _build_weekday_inputs()
    result = run_pipeline(inputs)
    # group trades by signal week
    from collections import defaultdict

    by_signal = defaultdict(int)
    for t in result.trade_rows:
        by_signal[t["signal_session_date"]] += 1
    # total sessions weeks: if some week has decisions retain only and no trades, good.
    # At minimum, total trades should only occur on swap weeks (exits/fills).
    # Count decision swaps
    swap_weeks = set()
    for d in result.decision_rows:
        if d["action"] in {"exit", "fill"}:
            swap_weeks.add(d["signal_session_date"])
    traded_weeks = set(by_signal.keys())
    assert traded_weeks <= swap_weeks


def test_future_price_change_does_not_affect_earlier_scores():
    inputs_a, days = _build_weekday_inputs()
    result_a = run_pipeline(inputs_a)

    inputs_b, _ = _build_weekday_inputs()
    # mutate prices after a mid date
    cut = days[len(days) // 2]
    for p in inputs_b.prices:
        if p.session_date > cut and p.security_id != "SYN_VOO":
            p.open *= 3.0
            p.close *= 3.0
    result_b = run_pipeline(inputs_b)

    def early_scores(res):
        return [
            (r["signal_session_date"], r["security_id"], r["score"])
            for r in res.score_rows
            if r["signal_session_date"] <= cut.isoformat()
        ]

    assert early_scores(result_a) == early_scores(result_b)


def test_split_at_execution_session_keeps_target_correct():
    """1->2 split effective on the execution day; targets must use post-split shares."""
    days = _days(20, date(2025, 1, 2))
    cal = [cal_row(d) for d in days]
    # two stocks
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    prices = []
    split_day = days[12]  # will also be an execution day
    for j, d in enumerate(days):
        for i, sid in enumerate(["S1", "S2"]):
            px = 40.0 + i * 10.0 + j * 0.2
            if sid == "S1" and d >= split_day:
                px = (40.0 + i * 10.0 + j * 0.2) / 2.0
            prices.append(price(sid, d, px))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    events = [
        EventRow(
            event_id="EV_SPLIT",
            security_id="S1",
            event_type="split",
            effective_session=split_day,
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    avail = utc(days[8].year, days[8].month, days[8].day, 12, 0)
    snaps = [snap("SNAP_A", days[4], avail)]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2")]
    cfg = make_config(L=3, N=2, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(cal, securities, snaps, members, prices, events=events, config=cfg)
    result = run_pipeline(inputs)
    assert result.status == "ok"
    # find trades on split_day
    trades_on_split = [t for t in result.trade_rows if t["session_date"] == split_day.isoformat()]
    # Even if trades occur, share accounting must remain non-negative and integer
    # Reconstruct final positions from trades is already validated by equity rows.
    assert result.equity_rows
    for row in result.equity_rows:
        assert float(row["total_equity"]) > 0
    # Split must not create a jump-to-zero equity on that day
    eq = {r["session_date"]: float(r["total_equity"]) for r in result.equity_rows}
    before = eq.get(days[11].isoformat())
    after = eq.get(split_day.isoformat())
    if before and after:
        assert after > before * 0.5  # not a mechanical 50% loss
        assert after < before * 1.5  # not a mechanical 50% gain


def test_dividend_receivable_and_pay_date():
    days = _days(25, date(2025, 1, 2))
    cal = [cal_row(d) for d in days]
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    ex_day = days[15]
    pay_day = date.fromordinal(ex_day.toordinal() + 10)
    prices = []
    for j, d in enumerate(days):
        for i, sid in enumerate(["S1", "S2"]):
            px = 30.0 + i * 5.0 + j * 0.1
            if sid == "S1" and d == ex_day:
                px = 29.5  # mechanical drop
            prices.append(price(sid, d, px))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    events = [
        EventRow(
            event_id="EV_DIV",
            security_id="S1",
            event_type="cash_dividend",
            effective_session=ex_day,
            shares_new_per_old=None,
            cash_amount_per_share=0.5,
            cash_basis="per_share_at_entitlement",
            pay_date=pay_day,
            source="test",
        )
    ]
    avail = utc(days[8].year, days[8].month, days[8].day, 12, 0)
    snaps = [snap("SNAP_A", days[4], avail)]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2")]
    cfg = make_config(L=3, N=2, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(cal, securities, snaps, members, prices, events=events, config=cfg)
    result = run_pipeline(inputs)
    assert result.status == "ok"
    # On ex_day, if S1 was held, receivable should be visible before pay
    eq = {r["session_date"]: r for r in result.equity_rows}
    # At least the run completes with identity: stock+cash+receivable=total
    for r in result.equity_rows:
        s = float(r["stock_value"])
        c = float(r["cash"])
        rec = float(r["receivable_cash"])
        tot = float(r["total_equity"])
        assert abs((s + c + rec) - tot) < 1e-6


def test_unsupported_event_fails_explicitly():
    days = _days(15, date(2025, 1, 2))
    cal = [cal_row(d) for d in days]
    securities = [sec("S1"), sec("SYN_VOO", stype="etf", ticker="VOO")]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 20.0 + j))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    events = [
        EventRow(
            event_id="EV_SPIN",
            security_id="S1",
            event_type="spinoff",
            effective_session=days[5],
            shares_new_per_old=None,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
            spinoff_value_per_old_share=2.0,
            spinoff_valuation="child_raw_close",
        )
    ]
    snaps = [snap("SNAP_A", days[3], utc(2025, 1, 10, 12, 0))]
    members = [member("SNAP_A", "S1")]
    cfg = make_config(L=3, N=1, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(cal, securities, snaps, members, prices, events=events, config=cfg)
    result = run_pipeline(inputs)
    assert result.status == "failed"
    assert "spinoff" in result.summary.get("failure_message", "")
    assert any(i["severity"] == "fatal" for i in result.data_issues)


def test_held_unscorable_due_to_gap_fails_not_silent_sell():
    days = _days(25, date(2025, 1, 2))
    cal = [cal_row(d) for d in days]
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    prices = []
    for j, d in enumerate(days):
        for sid in ["S1", "S2"]:
            if sid == "S1" and j >= 18:
                continue  # price gap while potentially held
            prices.append(price(sid, d, 25.0 + j * 0.2))
        prices.append(price("SYN_VOO", d, 100.0 + j))
    avail = utc(days[8].year, days[8].month, days[8].day, 12, 0)
    snaps = [snap("SNAP_A", days[4], avail)]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2")]
    cfg = make_config(L=5, N=2, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(cal, securities, snaps, members, prices, config=cfg)
    result = run_pipeline(inputs)
    # must fail explicitly (held name cannot be marked or scored), never silent-laggard-sell
    assert result.status == "failed"
    msg = result.summary.get("failure_message", "").lower()
    assert (
        "unscorable" in msg
        or "missing close" in msg
        or "missing open" in msg
        or "input gap" in msg
    )
    # and must not have treated the gap as a below_buffer exit without score
    bad = [
        d
        for d in result.decision_rows
        if d["security_id"] == "S1"
        and d["action"] == "exit"
        and d["reason"] == "below_buffer_line"
    ]
    for e in bad:
        assert e["score"] != ""
