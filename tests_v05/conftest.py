"""Shared helpers for stage-1 tests."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from leadtrend_v05.config import BootstrapConfig, DEFAULTS
from leadtrend_v05.dataio import (
    CalendarRow,
    EventRow,
    Inputs,
    PriceRow,
    SecurityRow,
    SnapshotMember,
    SnapshotMeta,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "data" / "v05" / "fixtures"


def make_config(**overrides) -> BootstrapConfig:
    raw = dict(DEFAULTS)
    raw.update(overrides)
    return BootstrapConfig(
        dataset_kind=str(raw["dataset_kind"]),
        L=int(raw["L"]),
        N=int(raw["N"]),
        k=int(raw["k"]),
        b=float(raw["b"]),
        initial_capital=float(raw["initial_capital"]),
        fee_per_trade=float(raw["fee_per_trade"]),
        slippage_bps=float(raw["slippage_bps"]),
        stress_slippage_bps=float(raw["stress_slippage_bps"]),
        class_selection_turnover_days=int(raw["class_selection_turnover_days"]),
        benchmark_security_id=str(raw["benchmark_security_id"]),
        input_dir=str(raw["input_dir"]),
        timezone_internal=str(raw["timezone_internal"]),
        timezone_display=str(raw["timezone_display"]),
        currency=str(raw["currency"]),
        supported_events=tuple(raw["supported_events"]),
        unsupported_event_types=tuple(raw["unsupported_event_types"]),
        excluded_security_types=tuple(raw["excluded_security_types"]),
        excluded_tickers=tuple(raw["excluded_tickers"]),
        rebalance_policy=str(raw["rebalance_policy"]),
    )


def utc(y: int, m: int, d: int, hh: int = 16, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def cal_row(d: date, open_h: int = 14, close_h: int = 21) -> CalendarRow:
    return CalendarRow(
        session_date=d,
        open_time_utc=utc(d.year, d.month, d.day, open_h, 30),
        close_time_utc=utc(d.year, d.month, d.day, close_h, 0),
        is_early_close=False,
        note="",
    )


def sec(
    sid: str,
    issuer: str | None = None,
    stype: str = "common_stock",
    industry: str = "TECH",
    ticker: str | None = None,
    exchange: str = "NASDAQ",
) -> SecurityRow:
    return SecurityRow(
        security_id=sid,
        issuer_id=issuer or f"ISS_{sid}",
        ticker=ticker or sid,
        ticker_valid_from=date(2025, 1, 1),
        ticker_valid_to=None,
        security_type=stype,
        exchange=exchange,
        industry=industry,
        industry_source_version="test_v1",
        currency="USD",
    )


def price(sid: str, d: date, o: float, c: float | None = None, turnover: float = 1e6) -> PriceRow:
    return PriceRow(
        security_id=sid,
        session_date=d,
        open=o,
        close=c if c is not None else o,
        volume=1000.0,
        turnover=turnover,
        currency="USD",
    )


def snap(
    sid: str,
    as_of: date,
    available_at: datetime,
    precision: str = "timestamp",
    revision: str = "r0",
) -> SnapshotMeta:
    return SnapshotMeta(
        snapshot_id=sid,
        holdings_as_of=as_of,
        available_at=available_at,
        available_at_raw=available_at.isoformat(),
        availability_precision=precision,
        source="test",
        source_hash="testhash",
        retrieved_at=available_at,
        revision=revision,
    )


def member(snapshot_id: str, security_id: str) -> SnapshotMember:
    return SnapshotMember(
        snapshot_id=snapshot_id,
        security_id=security_id,
        original_name=security_id,
        weight=None,
    )


def make_inputs(
    calendar: list[CalendarRow],
    securities: list[SecurityRow],
    snapshots: list[SnapshotMeta],
    members: list[SnapshotMember],
    prices: list[PriceRow],
    events: list[EventRow] | None = None,
    config: BootstrapConfig | None = None,
) -> Inputs:
    return Inputs(
        root=Path("."),
        config=config or make_config(),
        calendar=calendar,
        securities=securities,
        snapshots=snapshots,
        members=members,
        prices=prices,
        events=events or [],
        file_hashes={"mem": "0" * 64},
        data_issues=[],
    )


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


# Tests that need local material the public repository does not ship. Each is skipped only when that
# material is absent, so a complete local checkout runs every test.
PLOT_VENDOR = ROOT / "vendor" / "stage4_plot_deps"


def _no_matplotlib() -> bool:
    import importlib.util

    return not PLOT_VENDOR.is_dir() and importlib.util.find_spec("matplotlib") is None


LOCAL_ONLY = [
    ("test_perf_fastpath_real.py", lambda: not (ROOT / "data/v05/normalized/prices.csv").exists(),
     "needs local normalized prices data/v05/normalized/prices.csv (not published)"),
    ("test_stage2_parse.py::test_stage2_config_requires_ua_field_present",
     lambda: not (ROOT / "config/v05_stage2.json").exists(),
     "needs the local personal config config/v05_stage2.json (not published; copy the .example file, see README)"),
    ("test_perf_explore.py::test_heatmaps_mark_exploration_outputs", _no_matplotlib,
     "needs vendor/stage4_plot_deps or an installed matplotlib (plotting dependency not published)"),
]


def pytest_collection_modifyitems(config, items):
    for item in items:
        for key, missing, reason in LOCAL_ONLY:
            if key in item.nodeid and missing():
                item.add_marker(pytest.mark.skip(reason=reason))
