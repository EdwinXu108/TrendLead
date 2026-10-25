"""Synthetic VOO benchmark aligned to the strategy's first actual trade session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .dataio import EventRow, PriceRow
from .scoring import TRIEventError


@dataclass
class BenchmarkPoint:
    session_date: date
    voo_tri: float
    benchmark_value: float


@dataclass
class BenchmarkResult:
    points: list[BenchmarkPoint]
    units_purchased: float
    open_price: float
    close_price_first_day: float
    start_value_at_open: float
    start_session: date


def build_benchmark(
    benchmark_security_id: str,
    prices: list[PriceRow],
    events: list[EventRow],
    session_dates: list[date],
    start_value: float,
    start_session: date,
) -> BenchmarkResult:
    """Buy a divisible VOO share amount at the OPEN of ``start_session``.

    Valuation is always at CLOSE (including the first day: units × close),
    matching the strategy's daily close mark. The open investment amount is
    reported separately as ``start_value_at_open``.

    Cash dividends reinvest at ex-date close (same convention as build_tri).
    Splits adjust share count without creating PnL.
    """
    px = {p.session_date: p for p in prices if p.security_id == benchmark_security_id}
    ev = [e for e in events if e.security_id == benchmark_security_id]
    if start_session not in px:
        raise TRIEventError(
            f"benchmark {benchmark_security_id} missing open/close on start_session {start_session}"
        )
    open0 = px[start_session].open
    close0 = px[start_session].close
    if open0 <= 0:
        raise TRIEventError(f"benchmark open price invalid on {start_session}: {open0}")

    for e in ev:
        if e.effective_session not in px:
            raise TRIEventError(
                f"benchmark event {e.event_id} effective_session {e.effective_session} "
                f"has no price; cannot skip silently"
            )

    splits = [e for e in ev if e.event_type in {"split", "reverse_split"}]
    divs = [e for e in ev if e.event_type == "cash_dividend"]

    units = start_value / open0
    out: list[BenchmarkPoint] = []
    prev_units_at_close: float | None = None
    started = False

    for d in session_dates:
        if d < start_session:
            continue
        p = px.get(d)
        if p is None:
            continue

        cash = 0.0
        if started and prev_units_at_close is not None:
            for evd in divs:
                if evd.effective_session == d:
                    if evd.cash_amount_per_share is None:
                        raise TRIEventError(
                            f"cash_dividend {evd.event_id} missing cash_amount_per_share"
                        )
                    cash += prev_units_at_close * float(evd.cash_amount_per_share)

        for evs in splits:
            if evs.effective_session == d:
                if evs.shares_new_per_old is None or evs.shares_new_per_old <= 0:
                    raise TRIEventError(
                        f"split {evs.event_id} needs positive shares_new_per_old"
                    )
                units *= float(evs.shares_new_per_old)

        if cash > 0 and p.close > 0:
            units += cash / p.close

        # Always mark at close — including the first day — so the strategy and
        # benchmark share the same valuation convention.
        value = units * p.close
        out.append(
            BenchmarkPoint(
                session_date=d,
                voo_tri=value,
                benchmark_value=value,
            )
        )
        prev_units_at_close = units
        started = True

    if not out:
        raise TRIEventError("benchmark has no sessions on/after start_session")
    return BenchmarkResult(
        points=out,
        units_purchased=units if started else start_value / open0,
        open_price=open0,
        close_price_first_day=close0,
        start_value_at_open=start_value,
        start_session=start_session,
    )
