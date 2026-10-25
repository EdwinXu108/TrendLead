"""Stage-1/2 offline pipeline: universe -> score -> select -> execute -> ledger -> report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .benchmark import build_benchmark
from .calendar import Calendar
from .dataio import Inputs, manifest_inputs_hash
from .eligibility import (
    ClassSelector,
    build_price_index,
    eligibility_index,
)
from .execution import plan_equal_weight_trades
from .ledger import (
    AccountState,
    apply_split,
    confirm_dividend,
    pay_receivables,
)
from .scoring import TRIEventError, ScoreRow, build_tri, momentum_score
from .selection import Candidate, select_portfolio
from .universe import choose_snapshot


@dataclass
class PipelineResult:
    run_id: str
    dataset_kind: str
    config: dict[str, Any]
    file_hashes: dict[str, str]
    inputs_hash: str
    status: str
    universe_rows: list[dict[str, Any]]
    score_rows: list[dict[str, Any]]
    decision_rows: list[dict[str, Any]]
    trade_rows: list[dict[str, Any]]
    equity_rows: list[dict[str, Any]]
    benchmark_rows: list[dict[str, Any]]
    summary: dict[str, Any]
    data_issues: list[dict[str, Any]]
    class_notes: list[dict[str, Any]]
    failure_context: dict[str, Any] | None = None


@dataclass
class _RunState:
    universe_rows: list[dict[str, Any]] = field(default_factory=list)
    score_rows: list[dict[str, Any]] = field(default_factory=list)
    decision_rows: list[dict[str, Any]] = field(default_factory=list)
    trade_rows: list[dict[str, Any]] = field(default_factory=list)
    equity_rows: list[dict[str, Any]] = field(default_factory=list)
    data_issues: list[dict[str, Any]] = field(default_factory=list)
    class_notes: list[dict[str, Any]] = field(default_factory=list)


def _partial_payload(state: _RunState) -> dict[str, Any]:
    return {
        "universe_rows": list(state.universe_rows),
        "score_rows": list(state.score_rows),
        "decision_rows": list(state.decision_rows),
        "trade_rows": list(state.trade_rows),
        "equity_rows": list(state.equity_rows),
    }


def _unsupported_fail(
    inputs: Inputs,
    message: str,
    context: dict[str, Any],
    state: _RunState | None = None,
    run_id: str | None = None,
    extra_issues: list[dict[str, Any]] | None = None,
) -> PipelineResult:
    issue = {
        "category": "unsupported_or_invalid",
        "severity": "fatal",
        "security_id": str(context.get("security_id", "")),
        "snapshot_id": str(context.get("snapshot_id", "")),
        "event_id": str(context.get("event_id", "")),
        "session_date": str(context.get("session_date", "")),
        "message": message,
        "context": str(context),
    }
    issues = list(state.data_issues) if state is not None else list(inputs.data_issues)
    if extra_issues:
        issues = issues + list(extra_issues)
    issues.append(issue)
    inputs_hash = manifest_inputs_hash(inputs.file_hashes)
    rid = run_id or f"stage1-fail-{inputs_hash[:12]}"
    payload = _partial_payload(state) if state is not None else {
        "universe_rows": [],
        "score_rows": [],
        "decision_rows": [],
        "trade_rows": [],
        "equity_rows": [],
    }
    class_notes = list(state.class_notes) if state is not None else []
    return PipelineResult(
        run_id=rid,
        dataset_kind=inputs.config.dataset_kind,
        config=inputs.config.to_dict(),
        file_hashes=dict(inputs.file_hashes),
        inputs_hash=inputs_hash,
        status="failed",
        universe_rows=payload["universe_rows"],
        score_rows=payload["score_rows"],
        decision_rows=payload["decision_rows"],
        trade_rows=payload["trade_rows"],
        equity_rows=payload["equity_rows"],
        benchmark_rows=[],
        summary={
            "status": "failed",
            "dataset_kind": inputs.config.dataset_kind,
            "failure_message": message,
            "run_id": rid,
            "supported_events": list(inputs.config.supported_events),
            "unsupported": list(inputs.config.unsupported_event_types),
            "comparison_start_session": None,
            "comparison_end_session": None,
            "comparison_available": False,
            "comparison_start_value_at_open": None,
            "comparison_note": "run failed; comparison not available",
        },
        data_issues=issues,
        class_notes=class_notes,
        failure_context=context,
    )


def _fail_on_input_errors(
    inputs: Inputs,
    run_id: str | None = None,
) -> PipelineResult | None:
    blocking = [
        i
        for i in inputs.data_issues
        if str(i.get("severity", "")).lower() in {"error", "fatal"}
    ]
    if not blocking:
        return None
    messages = "; ".join(sorted({i.get("message", "") for i in blocking}))
    state = _RunState(data_issues=list(inputs.data_issues))
    return _unsupported_fail(
        inputs,
        f"input validation failed: {messages}",
        {"blocking_issue_count": len(blocking), "messages": messages},
        state=state,
        run_id=run_id,
    )


def run_pipeline(inputs: Inputs, run_id: str | None = None) -> PipelineResult:
    early = _fail_on_input_errors(inputs, run_id=run_id)
    if early is not None:
        return early

    state = _RunState(data_issues=list(inputs.data_issues))
    try:
        return _run_pipeline_inner(inputs, state, run_id=run_id)
    except (ValueError, KeyError, TRIEventError) as ex:
        return _unsupported_fail(
            inputs,
            f"{type(ex).__name__}: {ex}",
            {"error": str(ex), "error_type": type(ex).__name__},
            state=state,
            run_id=run_id,
        )


def _run_pipeline_inner(
    inputs: Inputs,
    state: _RunState,
    run_id: str | None = None,
) -> PipelineResult:
    cfg = inputs.config
    calendar = Calendar(inputs.calendar)
    sessions = calendar.sessions
    session_dates = [s.session_date for s in sessions]
    session_pos = {d: i for i, d in enumerate(session_dates)}

    # ---- precomputed indexes (A4) ----
    price_by_key = {(p.security_id, p.session_date): p for p in inputs.prices}
    events_by_date: dict[date, list] = {}
    for e in inputs.events:
        events_by_date.setdefault(e.effective_session, []).append(e)
    next_session_of: dict[date, date | None] = {}
    for i, d in enumerate(session_dates):
        next_session_of[d] = session_dates[i + 1] if i + 1 < len(session_dates) else None

    for ev in inputs.events:
        if ev.event_type in cfg.unsupported_event_types:
            return _unsupported_fail(
                inputs,
                f"unsupported event type {ev.event_type!r} must not be silently ignored",
                {
                    "event_id": ev.event_id,
                    "security_id": ev.security_id,
                    "session_date": ev.effective_session.isoformat(),
                    "event_type": ev.event_type,
                },
                state=state,
                run_id=run_id,
            )
        if ev.event_type not in cfg.supported_events:
            return _unsupported_fail(
                inputs,
                f"unknown event type {ev.event_type!r}",
                {
                    "event_id": ev.event_id,
                    "security_id": ev.security_id,
                    "session_date": ev.effective_session.isoformat(),
                    "event_type": ev.event_type,
                },
                state=state,
                run_id=run_id,
            )

    prices_idx = build_price_index(inputs.prices)
    elig = eligibility_index(inputs)
    class_sel = ClassSelector(cfg, prices_idx)
    state.class_notes = class_sel.notes

    tri_by_sec: dict[str, dict[date, float]] = {}
    first_price_date: dict[str, date] = {}
    unresolved_from: dict[str, date] = {}

    for sid, rows in prices_idx.items():
        if rows:
            first_price_date[sid] = rows[0].session_date

    for sid, rows in prices_idx.items():
        if sid == cfg.benchmark_security_id:
            continue
        evs = [e for e in inputs.events if e.security_id == sid]
        try:
            tr = build_tri(rows, evs, session_dates)
        except TRIEventError as ex:
            return _unsupported_fail(
                inputs,
                str(ex),
                {"security_id": sid, "error": str(ex)},
                state=state,
                run_id=run_id,
            )
        tri_by_sec[sid] = tr.tri
        if tr.unresolved_events:
            u_from = tr.invalid_from or session_dates[0]
            unresolved_from[sid] = u_from
            for u in tr.unresolved_events:
                state.data_issues.append(
                    {
                        "category": "events",
                        "severity": "warning",
                        "security_id": u.get("security_id", sid),
                        "snapshot_id": "",
                        "event_id": u.get("event_id", ""),
                        "session_date": u.get("effective_session", ""),
                        "message": f"unresolved_event_price_gap:{u.get('reason', '')}",
                        "context": (
                            "event effective session has no price; "
                            "if unheld, security is unscorable from that date"
                        ),
                    }
                )

    # Benchmark event on a missing-price session is always fatal (A5).
    for ev in inputs.events:
        if ev.security_id != cfg.benchmark_security_id:
            continue
        if (ev.security_id, ev.effective_session) not in price_by_key:
            return _unsupported_fail(
                inputs,
                f"benchmark event {ev.event_id} on session without price",
                {
                    "event_id": ev.event_id,
                    "security_id": ev.security_id,
                    "session_date": ev.effective_session.isoformat(),
                },
                state=state,
                run_id=run_id,
            )

    account = AccountState(cash=cfg.initial_capital)
    prev_close_shares: dict[str, int] = {}

    signal_sessions = calendar.week_signal_sessions()
    signal_date_set = {s.session_date for s in signal_sessions}

    exec_for_signal: dict[date, date | None] = {}
    for sig in signal_sessions:
        exec_for_signal[sig.session_date] = next_session_of.get(sig.session_date)
    # reverse: execution day -> signal day
    signal_for_exec: dict[date, date] = {}
    for sig_d, ex_d in exec_for_signal.items():
        if ex_d is not None:
            signal_for_exec[ex_d] = sig_d

    week_swap_flag: dict[date, bool] = {}
    week_targets: dict[date, list[str]] = {}
    week_target_count: dict[date, int] = {}

    first_trade_session: date | None = None

    for sess in sessions:
        d = sess.session_date
        todays_events = events_by_date.get(d, [])

        # ---- corporate actions at open (before trades) ----
        for ev in todays_events:
            if ev.security_id == cfg.benchmark_security_id:
                continue

            has_price = (ev.security_id, d) in price_by_key
            held_qty = account.shares_of(ev.security_id)
            entitled_shares = int(prev_close_shares.get(ev.security_id, 0))

            if ev.event_type in {"split", "reverse_split"}:
                if ev.shares_new_per_old is None:
                    return _unsupported_fail(
                        inputs,
                        "split event missing shares_new_per_old",
                        {"event_id": ev.event_id, "security_id": ev.security_id},
                        state=state,
                        run_id=run_id,
                    )
                if not has_price:
                    if held_qty > 0:
                        return _unsupported_fail(
                            inputs,
                            "held security has corporate action on a missing-price session",
                            {
                                "event_id": ev.event_id,
                                "security_id": ev.security_id,
                                "session_date": d.isoformat(),
                                "event_type": ev.event_type,
                            },
                            state=state,
                            run_id=run_id,
                        )
                    prev_u = unresolved_from.get(ev.security_id)
                    if prev_u is None or d < prev_u:
                        unresolved_from[ev.security_id] = d
                    continue
                apply_split(account, ev.security_id, float(ev.shares_new_per_old), ev.event_id)

            elif ev.event_type == "cash_dividend":
                if not has_price:
                    if entitled_shares > 0 or held_qty > 0:
                        return _unsupported_fail(
                            inputs,
                            "held security has cash_dividend on a missing-price session",
                            {
                                "event_id": ev.event_id,
                                "security_id": ev.security_id,
                                "session_date": d.isoformat(),
                                "event_type": ev.event_type,
                            },
                            state=state,
                            run_id=run_id,
                        )
                    prev_u = unresolved_from.get(ev.security_id)
                    if prev_u is None or d < prev_u:
                        unresolved_from[ev.security_id] = d
                    continue
                confirm_dividend(
                    account,
                    ev.security_id,
                    ev.event_id,
                    float(ev.cash_amount_per_share or 0.0),
                    ex_date=d,
                    pay_date=ev.pay_date,
                    entitled_shares=entitled_shares,
                )

        pay_receivables(account, d)

        # ---- execution for prior week's signal ----
        exec_signal_date = signal_for_exec.get(d)
        if exec_signal_date is not None and exec_signal_date in week_targets:
            targets = week_targets[exec_signal_date]
            is_swap = week_swap_flag.get(exec_signal_date, False)
            needed = set(targets) | set(account.positions.keys())
            missing = [
                sid for sid in sorted(needed) if (sid, d) not in price_by_key
            ]
            if missing:
                return _unsupported_fail(
                    inputs,
                    "missing open price on execution session for held/target security",
                    {
                        "session_date": d.isoformat(),
                        "missing": missing,
                        "signal_date": exec_signal_date.isoformat(),
                    },
                    state=state,
                    run_id=run_id,
                )
            open_prices = {
                sid: price_by_key[(sid, d)].open for sid in needed
            }
            new_trades = plan_equal_weight_trades(
                account=account,
                target_ids=targets,
                open_prices=open_prices,
                fee_per_trade=cfg.fee_per_trade,
                slippage_rate=cfg.slippage_rate,
                session_date=d,
                is_swap_week=is_swap,
                reason_prefix="",
            )
            for t in new_trades:
                state.trade_rows.append(
                    {
                        "session_date": t.session_date.isoformat(),
                        "security_id": t.security_id,
                        "side": t.side,
                        "shares": t.shares,
                        "reference_price": f"{t.reference_price:.6f}",
                        "fill_price": f"{t.fill_price:.6f}",
                        "fee": f"{t.fee:.2f}",
                        "cash_delta": f"{t.cash_delta:.6f}",
                        "reason": t.reason,
                        "signal_session_date": exec_signal_date.isoformat(),
                    }
                )
                if first_trade_session is None:
                    first_trade_session = d

        # ---- end of day mark to market ----
        close_marks: dict[str, float] = {}
        for sid in list(account.positions.keys()):
            pr = price_by_key.get((sid, d))
            if pr is None:
                return _unsupported_fail(
                    inputs,
                    "missing close mark for held security",
                    {"session_date": d.isoformat(), "security_id": sid},
                    state=state,
                    run_id=run_id,
                )
            close_marks[sid] = pr.close

        stock_value, cash, receivable, total = account.equity_components(close_marks)
        state.equity_rows.append(
            {
                "session_date": d.isoformat(),
                "stock_value": f"{stock_value:.6f}",
                "cash": f"{cash:.6f}",
                "receivable_cash": f"{receivable:.6f}",
                "total_equity": f"{total:.6f}",
                "n_positions": len(account.positions),
            }
        )

        prev_close_shares = dict(account.positions)

        # ---- weekly signal at this session's close ----
        if d not in signal_date_set:
            continue

        signal_at = sess.close_time_utc
        choice = choose_snapshot(inputs, signal_at)
        snap = choice.snapshot
        member_ids = [m.security_id for m in choice.members]
        member_set = set(member_ids)

        if snap is None:
            state.universe_rows.append(
                {
                    "signal_session_date": d.isoformat(),
                    "snapshot_id": "",
                    "holdings_as_of": "",
                    "available_at": "",
                    "security_id": "",
                    "issuer_id": "",
                    "in_snapshot": "0",
                    "eligible": "0",
                    "eligibility_reason": "no_available_snapshot",
                    "class_selected": "0",
                }
            )
            week_targets[d] = list(account.positions.keys())
            week_swap_flag[d] = False
            week_target_count[d] = 0
            continue

        for mid in member_ids:
            e = elig.get(mid)
            if e is None:
                state.universe_rows.append(
                    {
                        "signal_session_date": d.isoformat(),
                        "snapshot_id": snap.snapshot_id,
                        "holdings_as_of": snap.holdings_as_of.isoformat(),
                        "available_at": snap.available_at.isoformat(),
                        "security_id": mid,
                        "issuer_id": "",
                        "in_snapshot": "1",
                        "eligible": "0",
                        "eligibility_reason": "unknown_security_id",
                        "class_selected": "0",
                    }
                )
                continue
            state.universe_rows.append(
                {
                    "signal_session_date": d.isoformat(),
                    "snapshot_id": snap.snapshot_id,
                    "holdings_as_of": snap.holdings_as_of.isoformat(),
                    "available_at": snap.available_at.isoformat(),
                    "security_id": mid,
                    "issuer_id": e.issuer_id,
                    "in_snapshot": "1",
                    "eligible": "1" if e.eligible else "0",
                    "eligibility_reason": e.reason,
                    "class_selected": "0",
                }
            )

        for hid in sorted(account.positions.keys()):
            if hid not in member_set and hid != cfg.benchmark_security_id:
                e = elig.get(hid)
                state.universe_rows.append(
                    {
                        "signal_session_date": d.isoformat(),
                        "snapshot_id": snap.snapshot_id,
                        "holdings_as_of": snap.holdings_as_of.isoformat(),
                        "available_at": snap.available_at.isoformat(),
                        "security_id": hid,
                        "issuer_id": e.issuer_id if e else "",
                        "in_snapshot": "0",
                        "eligible": "0",
                        "eligibility_reason": "left_latest_snapshot",
                        "class_selected": "0",
                    }
                )

        eligible_members = [
            elig[mid]
            for mid in member_ids
            if mid in elig and elig[mid].eligible and mid != cfg.benchmark_security_id
        ]
        by_issuer: dict[str, list] = {}
        for e in eligible_members:
            by_issuer.setdefault(e.issuer_id, []).append(e)

        selected_classes: dict[str, str] = {}
        for issuer, cands in sorted(by_issuer.items()):
            cid = class_sel.select_for_issuer(issuer, cands, d)
            if cid:
                selected_classes[issuer] = cid

        selected_ids = set(selected_classes.values())
        for row in state.universe_rows:
            if row["signal_session_date"] == d.isoformat() and row["security_id"] in selected_ids:
                row["class_selected"] = "1"

        score_map: dict[str, ScoreRow] = {}
        for sid in sorted(selected_ids):
            e = elig[sid]
            tri = tri_by_sec.get(sid, {})
            u_from = unresolved_from.get(sid)
            # unresolved_event_price_gap from the event date onward (A5)
            sc, tri_end, tri_start, win, reason = momentum_score(
                tri,
                d,
                cfg.L,
                session_dates,
                session_index=session_pos,
                first_valid_price_date=first_price_date.get(sid),
                unresolved_from=u_from,
            )
            score_map[sid] = ScoreRow(
                security_id=sid,
                as_of=d,
                score=sc,
                tri_end=tri_end,
                tri_start=tri_start,
                window_sessions=win,
                rank=None,
                unscorable_reason=reason,
                issuer_id=e.issuer_id,
                industry=e.industry,
            )

        held_now = {sid: q for sid, q in account.positions.items() if q > 0}
        for hid in sorted(held_now.keys()):
            if hid in member_set and hid in selected_ids:
                sr = score_map.get(hid)
                if sr is not None and sr.score is None:
                    return _unsupported_fail(
                        inputs,
                        "held security unscorable due to input gap; not treated as laggard exit",
                        {
                            "signal_session_date": d.isoformat(),
                            "security_id": hid,
                            "reason": sr.unscorable_reason,
                        },
                        state=state,
                        run_id=run_id,
                    )

        scored_list = sorted(
            score_map.values(),
            key=lambda r: (r.score is None, -(r.score or 0.0), r.security_id),
        )
        for i, sr in enumerate(scored_list, start=1):
            rank = i if sr.score is not None else None
            state.score_rows.append(
                {
                    "signal_session_date": d.isoformat(),
                    "security_id": sr.security_id,
                    "issuer_id": sr.issuer_id,
                    "industry": sr.industry,
                    "window_sessions": sr.window_sessions,
                    "tri_start": "" if sr.tri_start is None else f"{sr.tri_start:.8f}",
                    "tri_end": "" if sr.tri_end is None else f"{sr.tri_end:.8f}",
                    "score": "" if sr.score is None else f"{sr.score:.8f}",
                    "rank": "" if rank is None else rank,
                    "unscorable_reason": sr.unscorable_reason,
                    "L": cfg.L,
                }
            )

        candidates = [
            Candidate(
                security_id=sr.security_id,
                issuer_id=sr.issuer_id,
                industry=sr.industry,
                score=float(sr.score),
            )
            for sr in scored_list
            if sr.score is not None
        ]

        forced_exits: list[tuple[str, str]] = []
        for hid in sorted(held_now.keys()):
            if hid == cfg.benchmark_security_id:
                forced_exits.append((hid, "benchmark_not_tradable"))
                continue
            if hid not in member_set:
                forced_exits.append((hid, "left_latest_snapshot"))
                continue
            e = elig.get(hid)
            if e is None or not e.eligible:
                forced_exits.append((hid, "became_excluded"))
                continue
            if hid not in selected_ids:
                forced_exits.append((hid, "class_not_selected"))

        held_for_select = [
            hid
            for hid in sorted(held_now.keys())
            if hid not in {x[0] for x in forced_exits}
        ]

        sel = select_portfolio(
            candidates=candidates,
            held_ids=held_for_select,
            N=cfg.N,
            k=cfg.k,
            b=cfg.b,
        )

        for hid, reason in forced_exits:
            e = elig.get(hid)
            state.decision_rows.append(
                {
                    "signal_session_date": d.isoformat(),
                    "action": "exit",
                    "security_id": hid,
                    "issuer_id": e.issuer_id if e else "",
                    "industry": e.industry if e else "",
                    "score": "",
                    "rank": "",
                    "reason": reason,
                    "buffer_score": "" if sel.buffer_score is None else f"{sel.buffer_score:.8f}",
                    "buffer_security_id": sel.buffer_security_id or "",
                    "in_feasible_order": "",
                    "feasible_index": "",
                    "target_count": sel.target_count,
                }
            )

        for dec in sel.decisions:
            state.decision_rows.append(
                {
                    "signal_session_date": d.isoformat(),
                    "action": dec.action,
                    "security_id": dec.security_id,
                    "issuer_id": dec.issuer_id,
                    "industry": dec.industry,
                    "score": "" if dec.score is None else f"{dec.score:.8f}",
                    "rank": "" if dec.rank is None else dec.rank,
                    "reason": dec.reason,
                    "buffer_score": "" if sel.buffer_score is None else f"{sel.buffer_score:.8f}",
                    "buffer_security_id": sel.buffer_security_id or "",
                    "in_feasible_order": "1" if dec.in_feasible_order else "0",
                    "feasible_index": "" if dec.feasible_index is None else dec.feasible_index,
                    "target_count": sel.target_count,
                }
            )

        final_targets = list(sel.target_ids)
        held_ids_set = set(held_now.keys())
        target_set = set(final_targets)
        exits_now = held_ids_set - target_set
        fills_now = target_set - held_ids_set
        is_swap = bool(exits_now or fills_now)

        week_targets[d] = final_targets
        week_swap_flag[d] = is_swap
        week_target_count[d] = len(final_targets)

        if exec_for_signal.get(d) is None and (exits_now or fills_now):
            for hid in sorted(exits_now):
                state.decision_rows.append(
                    {
                        "signal_session_date": d.isoformat(),
                        "action": "unexecuted_exit",
                        "security_id": hid,
                        "issuer_id": "",
                        "industry": "",
                        "score": "",
                        "rank": "",
                        "reason": "no_next_session_for_execution",
                        "buffer_score": "",
                        "buffer_security_id": "",
                        "in_feasible_order": "",
                        "feasible_index": "",
                        "target_count": sel.target_count,
                    }
                )
            for fid in sorted(fills_now):
                state.decision_rows.append(
                    {
                        "signal_session_date": d.isoformat(),
                        "action": "unexecuted_fill",
                        "security_id": fid,
                        "issuer_id": "",
                        "industry": "",
                        "score": "",
                        "rank": "",
                        "reason": "no_next_session_for_execution",
                        "buffer_score": "",
                        "buffer_security_id": "",
                        "in_feasible_order": "",
                        "feasible_index": "",
                        "target_count": sel.target_count,
                    }
                )

    # ---- benchmark aligned to first actual trade session (A2: close valuation) ----
    comparison_available = first_trade_session is not None
    benchmark_rows: list[dict[str, Any]] = []
    bench_last = None
    bench_ret = None
    comparison_start = None
    comparison_end = None
    comparison_start_value_at_open = None
    comparison_note = (
        "cash period before first trade is excluded from comparison; "
        "benchmark buys divisible VOO units at the open of the first trade session "
        "and is marked at close (including day one)"
    )

    if comparison_available:
        comparison_start = first_trade_session.isoformat()
        try:
            bench = build_benchmark(
                cfg.benchmark_security_id,
                inputs.prices,
                inputs.events,
                session_dates,
                start_value=cfg.initial_capital,
                start_session=first_trade_session,
            )
        except Exception as ex:  # noqa: BLE001
            return _unsupported_fail(
                inputs,
                f"benchmark build failed: {ex}",
                {"error": str(ex)},
                state=state,
                run_id=run_id,
            )
        benchmark_rows = [
            {
                "session_date": p.session_date.isoformat(),
                "voo_tri": f"{p.voo_tri:.8f}",
                "benchmark_value": f"{p.benchmark_value:.6f}",
            }
            for p in bench.points
        ]
        comparison_end = benchmark_rows[-1]["session_date"]
        comparison_start_value_at_open = bench.start_value_at_open
        bench_last = float(benchmark_rows[-1]["benchmark_value"])
        strategy_at_start = cfg.initial_capital
        strategy_at_end = (
            float(state.equity_rows[-1]["total_equity"])
            if state.equity_rows
            else strategy_at_start
        )
        bench_ret = bench_last / cfg.initial_capital - 1.0
        strategy_ret_cmp = strategy_at_end / strategy_at_start - 1.0
    else:
        comparison_note = (
            "no trades occurred in this run; benchmark and return comparison are not available"
        )
        strategy_ret_cmp = None

    total_equity_last = (
        float(state.equity_rows[-1]["total_equity"]) if state.equity_rows else None
    )
    n_trades = len(state.trade_rows)
    fee_total = sum(float(t["fee"]) for t in state.trade_rows)
    ret_full = (
        total_equity_last / cfg.initial_capital - 1.0
        if total_equity_last is not None
        else None
    )

    n_signal_weeks = len(signal_sessions)
    n_weeks_no_target = sum(1 for v in week_target_count.values() if v == 0)
    n_weeks_with_target = sum(1 for v in week_target_count.values() if v > 0)

    last_sig = signal_sessions[-1].session_date if signal_sessions else None
    last_ids = week_targets.get(last_sig, []) if last_sig else []

    inputs_hash = manifest_inputs_hash(inputs.file_hashes)
    summary = {
        "status": "ok",
        "dataset_kind": cfg.dataset_kind,
        "run_id": run_id,
        "initial_capital": cfg.initial_capital,
        "final_equity": total_equity_last,
        "total_return": ret_full,
        "comparison_available": comparison_available,
        "comparison_start_session": comparison_start,
        "comparison_end_session": comparison_end,
        "comparison_start_value_at_open": comparison_start_value_at_open,
        "comparison_note": comparison_note,
        "strategy_comparison_return": strategy_ret_cmp if comparison_available else None,
        "benchmark_final_value": bench_last if comparison_available else None,
        "benchmark_total_return": bench_ret if comparison_available else None,
        "n_sessions": len(state.equity_rows),
        "n_trades": n_trades,
        "fee_total": fee_total,
        "n_signal_weeks": n_signal_weeks,
        "n_signal_weeks_with_target": n_weeks_with_target,
        "n_signal_weeks_without_target": n_weeks_no_target,
        "L": cfg.L,
        "N": cfg.N,
        "k": cfg.k,
        "b": cfg.b,
        "fee_per_trade": cfg.fee_per_trade,
        "slippage_bps": cfg.slippage_bps,
        "rebalance_policy": cfg.rebalance_policy,
        "target_ids_at_last_signal": last_ids,
        "target_count_at_last_signal": len(last_ids),
        "supported_events": list(cfg.supported_events),
        "unsupported_event_types": list(cfg.unsupported_event_types),
        "limitations": [
            "dataset_kind=synthetic; not a real VOO backtest",
            "engineering defaults L/N/k/b are unvalidated starting values",
            "fills use open price with adverse slippage; not a guarantee of real fills",
            "stale snapshots between publications are accepted by design",
            "sector map is synthetic/static in stage 1",
            comparison_note,
        ],
        "inputs_hash": inputs_hash,
    }

    if run_id is None:
        run_id = f"stage1-{inputs_hash[:12]}"
        summary["run_id"] = run_id

    return PipelineResult(
        run_id=run_id,
        dataset_kind=cfg.dataset_kind,
        config=cfg.to_dict(),
        file_hashes=dict(inputs.file_hashes),
        inputs_hash=inputs_hash,
        status="ok",
        universe_rows=state.universe_rows,
        score_rows=state.score_rows,
        decision_rows=state.decision_rows,
        trade_rows=state.trade_rows,
        equity_rows=state.equity_rows,
        benchmark_rows=benchmark_rows,
        summary=summary,
        data_issues=state.data_issues,
        class_notes=state.class_notes,
    )
