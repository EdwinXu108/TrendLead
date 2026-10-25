"""Total-return scoring series and momentum ranks. Score series ≠ account cash."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .dataio import EventRow, PriceRow


class TRIEventError(ValueError):
    """Corporate-action event cannot be applied because of missing/invalid data."""


@dataclass
class ScoreRow:
    security_id: str
    as_of: date
    score: float | None
    tri_end: float | None
    tri_start: float | None
    window_sessions: int
    rank: int | None
    unscorable_reason: str
    issuer_id: str
    industry: str


@dataclass
class TRIResult:
    tri: dict[date, float] = field(default_factory=dict)
    valid_dates: list[date] = field(default_factory=list)
    # Events that could not be applied because the effective session has no price.
    unresolved_events: list[dict] = field(default_factory=list)
    # First date from which TRI is unreliable (inclusive).
    invalid_from: date | None = None


def build_tri(
    prices: list[PriceRow],
    events: list[EventRow],
    session_dates: list[date],
) -> TRIResult:
    """Rebuild a buy-and-hold total-return index with split + cash-dividend reinvestment.

    Dividend entitlement uses share count at the close of the session before ex-date.
    Reinvestment assumption (score path only): buy at ex-date close.

    If a split/dividend effective session has no price for this security, the event
    is recorded in ``unresolved_events`` and TRI becomes invalid from that date.
    The caller decides whether that is fatal (held name / benchmark) or merely
    blocks scoring (unheld name).
    """
    px = {p.session_date: p for p in prices}
    splits = [e for e in events if e.event_type in {"split", "reverse_split"}]
    divs = [e for e in events if e.event_type == "cash_dividend"]
    splits_by_date: dict[date, list[EventRow]] = {}
    divs_by_date: dict[date, list[EventRow]] = {}
    spins_by_date: dict[date, list[EventRow]] = {}
    for event in events:
        if event.event_type == "spinoff":
            spins_by_date.setdefault(event.effective_session, []).append(event)
    for event in splits:
        splits_by_date.setdefault(event.effective_session, []).append(event)
    for event in divs:
        divs_by_date.setdefault(event.effective_session, []).append(event)

    result = TRIResult()
    session_set = set(session_dates)

    # Detect events that cannot be applied.
    blocked_dates: list[date] = []
    for ev in events:
        if ev.event_type == "spinoff" and (ev.spinoff_value_per_old_share is None
                or not math.isfinite(ev.spinoff_value_per_old_share) or ev.spinoff_value_per_old_share < 0):
            result.unresolved_events.append(
                {
                    "event_id": ev.event_id,
                    "security_id": ev.security_id,
                    "event_type": ev.event_type,
                    "effective_session": ev.effective_session.isoformat(),
                    "reason": "unsupported_spinoff_accounting",
                }
            )
            blocked_dates.append(ev.effective_session)
            continue
        if ev.effective_session not in session_set:
            result.unresolved_events.append(
                {
                    "event_id": ev.event_id,
                    "security_id": ev.security_id,
                    "event_type": ev.event_type,
                    "effective_session": ev.effective_session.isoformat(),
                    "reason": "effective_session_not_in_calendar",
                }
            )
            blocked_dates.append(ev.effective_session)
            continue
        if ev.effective_session not in px:
            result.unresolved_events.append(
                {
                    "event_id": ev.event_id,
                    "security_id": ev.security_id,
                    "event_type": ev.event_type,
                    "effective_session": ev.effective_session.isoformat(),
                    "reason": "event_price_gap",
                }
            )
            blocked_dates.append(ev.effective_session)

    if blocked_dates:
        result.invalid_from = min(blocked_dates)

    shares = 1.0
    prev_shares_at_close: float | None = None

    for i, d in enumerate(session_dates):
        if result.invalid_from is not None and d >= result.invalid_from:
            break

        p = px.get(d)
        if p is None:
            continue

        cash = 0.0
        # Distribution entitlement is per old parent share. Its value is
        # reinvested at this session's parent close, only in the scoring index.
        for ev in spins_by_date.get(d, []):
            cash += (prev_shares_at_close if prev_shares_at_close is not None else shares) * ev.spinoff_value_per_old_share
        if i > 0 and prev_shares_at_close is not None:
            for ev in divs_by_date.get(d, []):
                if ev.effective_session == d:
                    if (ev.cash_amount_per_share is None
                            or not math.isfinite(ev.cash_amount_per_share)):
                        raise TRIEventError(
                            f"cash_dividend {ev.event_id} missing/non-finite cash_amount_per_share"
                        )
                    if ev.cash_basis not in {"per_share_at_entitlement", "pre_event_share"}:
                        raise TRIEventError(
                            f"cash_dividend {ev.event_id} unsupported cash_basis={ev.cash_basis!r}"
                        )
                    cash += prev_shares_at_close * float(ev.cash_amount_per_share)

        for ev in splits_by_date.get(d, []):
            if ev.effective_session == d:
                if (ev.shares_new_per_old is None or not math.isfinite(ev.shares_new_per_old)
                        or ev.shares_new_per_old <= 0):
                    raise TRIEventError(
                        f"split {ev.event_id} needs positive shares_new_per_old"
                    )
                shares *= float(ev.shares_new_per_old)

        if cash > 0 and p.close > 0:
            shares += cash / p.close

        value = shares * p.close
        if not math.isfinite(value) or not math.isfinite(shares):
            raise TRIEventError(f"non-finite TRI for {p.security_id} at {d}")
        result.tri[d] = value
        result.valid_dates.append(d)
        prev_shares_at_close = shares

    return result


def momentum_score(
    tri: dict[date, float],
    as_of: date,
    L: int,
    session_dates: list[date],
    session_index: dict[date, int] | None = None,
    first_valid_price_date: date | None = None,
    unresolved_from: date | None = None,
) -> tuple[float | None, float | None, float | None, int, str]:
    """Return (score, tri_end, tri_start, window_sessions, unscorable_reason).

    The L+1 scoring points must be exactly the last L+1 consecutive calendar
    sessions up to and including ``as_of``, and every one of them must have a
    valid TRI value.

    - ``insufficient_history``: missing points are all strictly before the
      security's first valid price (the name simply has not started yet).
    - ``price_gap_in_window``: a missing point is on/after the first valid price
      (a true hole in an already-started series).
    - ``unresolved_event_price_gap``: a corporate action on a missing-price day
      makes TRI unreliable from that date onward.
    """
    if unresolved_from is not None and as_of >= unresolved_from:
        return None, None, None, 0, "unresolved_event_price_gap"

    # Fast path: slice the last L+1 sessions up to as_of via index.
    if session_index is not None and as_of in session_index:
        end_idx = session_index[as_of]
        start_idx = end_idx - L
        if start_idx < 0:
            return None, tri.get(as_of), None, end_idx + 1, "insufficient_history"
        window = session_dates[start_idx : end_idx + 1]
    else:
        sessions_upto = [d for d in session_dates if d <= as_of]
        if as_of not in sessions_upto:
            return None, None, None, 0, "missing_price_at_signal"
        if len(sessions_upto) < L + 1:
            return None, tri.get(as_of), None, len(sessions_upto), "insufficient_history"
        window = sessions_upto[-(L + 1) :]

    if window[-1] != as_of:
        return None, tri.get(as_of), None, len(window), "missing_price_at_signal"

    missing = [d for d in window if d not in tri]
    if missing:
        if as_of not in tri:
            # distinguish "no prices at all" from "prices exist but not on signal day"
            if not tri:
                return None, None, None, len(window), "no_price_data"
            return None, None, None, len(window), "missing_price_at_signal"
        # Distinguish "not yet started" from "started then gapped".
        if first_valid_price_date is not None and all(d < first_valid_price_date for d in missing):
            return None, tri.get(as_of), tri.get(window[0]), len(window), "insufficient_history"
        return (
            None,
            tri.get(as_of),
            tri.get(window[0]),
            len(window),
            "price_gap_in_window",
        )

    t_start = window[0]
    t_end = window[-1]
    start_val = tri[t_start]
    end_val = tri[t_end]
    if any(not math.isfinite(tri[d]) for d in window):
        return None, end_val, start_val, len(window), "non_finite_tri"
    if start_val == 0:
        return None, end_val, start_val, len(window), "invalid_tri_start"
    score = end_val / start_val - 1.0
    if not math.isfinite(score):
        return None, end_val, start_val, len(window), "non_finite_tri"
    return score, end_val, start_val, L + 1, ""
