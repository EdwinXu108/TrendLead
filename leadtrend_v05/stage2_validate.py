"""Stage-2 validation: finite scores, honest coverage, no equity or returns."""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from datetime import date

from .calendar import Calendar
from .config import load_config
from .dataio import load_inputs, sha256_file
from .eligibility import ClassSelector, build_price_index, eligibility_index
from .scoring import build_tri, momentum_score
from .selection import Candidate, select_portfolio
from .universe import choose_snapshot
from . import paths


def _write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _longest_passing(week_rows: list[dict]) -> tuple[int | None, int | None]:
    best_start = best_end = current = None
    for index, row in enumerate(week_rows + [{"passes_gate": "0"}]):
        if row["passes_gate"] == "1":
            if current is None:
                current = index
        elif current is not None:
            length = index - current
            best_length = 0 if best_start is None else best_end - best_start + 1
            if length > best_length:
                best_start, best_end = current, index - 1
            current = None
    return best_start, best_end


def _preparation_errors(project_root: Path) -> list[dict]:
    errors: list[dict] = []
    for name in ("stage2_identity_issues.csv", "stage2_corporate_action_gaps.csv"):
        path = project_root / paths.PREP / name
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            errors.extend(r for r in csv.DictReader(handle) if r.get("severity") in {"error", "fatal"})
    return errors


def _historical_unpriced(project_root: Path) -> set[str]:
    path = project_root / paths.PREP / "stage2_unpriced_members.csv"
    if not path.is_file():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {r["security_id"] for r in csv.DictReader(handle)
                if r.get("classification") == "historical_member_no_price"}


def _register_experiment(project_root: Path, inputs, week_rows: list[dict],
                         best_start: int | None, best_end: int | None, gate: float,
                         blocking_issues: int = 0) -> None:
    passing = week_rows[best_start:best_end + 1] if best_start is not None and best_end is not None else []
    cut = int(len(passing) * 2 / 3)
    dev = passing[:cut]
    holdout = passing[cut:]
    registry = {
        "dataset_kind": "real_partial",
        "status": "blocked_data_quality" if blocking_issues or len(passing) < 2 else "registered_before_viewing_any_real_returns",
        "blocking_issues": blocking_issues,
        "execution_authorized": False,
        "purpose": "Stage-2 data coverage registration; Stage-3 must use development segment only",
        "sample_start": passing[0]["signal_session_date"] if passing else None,
        "sample_end": passing[-1]["signal_session_date"] if passing else None,
        "valid_signal_weeks": len(passing),
        "development": {
            "start": dev[0]["signal_session_date"] if dev else None,
            "end": dev[-1]["signal_session_date"] if dev else None,
            "n_weeks": len(dev),
        },
        "holdout": {
            "start": holdout[0]["signal_session_date"] if holdout else None,
            "end": holdout[-1]["signal_session_date"] if holdout else None,
            "n_weeks": len(holdout),
        },
        "dev_holdout_split": {"method": "first floor(2/3) passing signal weeks = development"},
        "data_version": {p.name: sha256_file(p) for p in sorted(inputs.root.glob("*.csv"))},
        "engineering_defaults_unvalidated": {"L": 20, "N": 10, "k": 3, "b": 2},
        "score_coverage_gate": gate,
        "gate_note": "engineering gate, unvalidated",
        "no_real_returns_viewed": True,
        "coverage_policy": {"remediable_gap_max_ratio": inputs.config.extra.get("remediable_gap_max_ratio", .01), "historical_no_price_max_ratio": None,
                            "weekly_scored_industry_min_ratio": inputs.config.extra.get("weekly_scored_industry_min_ratio", .95), "original_score_95pct_is_diagnostic_only": True},
        "historical_no_price_ratio_summary": {
            "min": min((float(r.get("historical_no_price_ratio",0)) for r in passing), default=None),
            "median": statistics.median([float(r.get("historical_no_price_ratio",0)) for r in passing]) if passing else None,
            "max": max((float(r.get("historical_no_price_ratio",0)) for r in passing), default=None)},
    }
    path = project_root / paths.STAGE2_REGISTRY
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        import hashlib
        old = path.read_bytes()
        archive = path.with_name("stage2_experiment_registry_void_" + hashlib.sha256(old).hexdigest()[:12] + ".json")
        if not archive.exists():
            archive.write_bytes(old)
            archive.with_suffix(".void_reason.json").write_text(json.dumps({
                "reason": "superseded: A1 canonical ISIN/CUSIP succession and placeholder issuer repair; A2 actual child quotes/window exclusions; A3 official cash verification; new normalized hashes",
                "archive": archive.name}, indent=2) + "\n", encoding="utf-8")
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_validate(project_root: Path, config_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_cfg = json.loads(config_path.read_text(encoding="utf-8"))
    gate = float(raw_cfg.get("score_coverage_gate", 0.95))
    repair_gate = float(raw_cfg.get("remediable_gap_max_ratio", .01))
    industry_gate = float(raw_cfg.get("weekly_scored_industry_min_ratio", .95))
    cfg = load_config(config_path)
    inputs = load_inputs(cfg, project_root, score_only=True)
    errors = [i for i in inputs.data_issues if i.get("severity") in {"error", "fatal"}]
    preparation_errors = _preparation_errors(project_root)
    blocked_sids = {r["security_id"] for r in preparation_errors
                    if r.get("security_id") and r["security_id"] != "*"}
    dated_blocks = defaultdict(list)
    block_path = project_root / paths.SCORING_BLOCKS
    if block_path.is_file():
        with block_path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                dated_blocks[row["security_id"]].append((row["effective_session"], row["reason"]))

    cal = Calendar(inputs.calendar)
    session_dates = [s.session_date for s in cal.sessions]
    session_pos = {d: i for i, d in enumerate(session_dates)}
    prices_idx = build_price_index(inputs.prices)
    elig = eligibility_index(inputs)
    events_by_sid: dict[str, list] = defaultdict(list)
    for event in inputs.events:
        events_by_sid[event.security_id].append(event)

    tri_results = {}
    guard = date.fromisoformat(raw_cfg['holdout_guard_date']) if raw_cfg.get('holdout_guard_date') else None
    guarded_engine = None
    if guard is not None:
        from .stage3_scoring import ScoreEngine
        guarded_engine = ScoreEngine(inputs, session_dates, project_root, guard)
    first_price = {}
    for sid, rows in prices_idx.items():
        if rows:
            first_price[sid] = rows[0].session_date
        if sid != cfg.benchmark_security_id and guard is None:
            tri_results[sid] = build_tri(rows, events_by_sid.get(sid, []), session_dates)

    sigs = cal.week_signal_sessions()
    week_rows: list[dict] = []
    score_rows: list[dict] = []
    reason_counter: Counter = Counter()
    historical_unpriced = _historical_unpriced(project_root)
    class_selector = ClassSelector(cfg, prices_idx)

    for sess in sigs:
        d = sess.session_date
        choice = choose_snapshot(inputs, sess.close_time_utc)
        if choice.snapshot is None:
            week_rows.append({"signal_session_date": d.isoformat(), "n_eligible": 0, "n_scored": 0,
                              "score_ratio": 0.0, "passes_gate": "0", "n_no_price": 0,
                              "n_historical_no_price": 0, "historical_no_price_ratio": 0.0,
                              "reason": "no_available_snapshot"})
            continue
        by_issuer: dict[str, list] = {}
        for member in choice.members:
            item = elig.get(member.security_id)
            if (item and item.eligible and member.security_id != cfg.benchmark_security_id
                    and item.security_type != "reit"):
                by_issuer.setdefault(item.issuer_id, []).append(item)
        selected = []
        for issuer, candidates in sorted(by_issuer.items()):
            chosen = class_selector.select_for_issuer(issuer, candidates, d)
            if chosen:
                selected.append(chosen)

        n_eligible = len(selected)
        n_scored = n_no_price = n_historical = n_known_industry = 0
        reasons: Counter = Counter()
        for sid in selected:
            item = elig[sid]
            if guarded_engine is not None:
                reason=guarded_engine.availability(sid,d,cfg.L)
                if d<=guard:
                    score,_,_,_,reason=guarded_engine.score(sid,d,cfg.L)
                else:
                    score=None  # Only data availability; no holdout score, rank or decision.
                available = not reason
                if reason=='no_price_data':
                    n_no_price+=1
                    if sid in historical_unpriced:n_historical+=1
                if available:
                    n_scored+=1
                    if not item.industry.startswith('UNKNOWN:'):n_known_industry+=1
                else:
                    reasons[reason]+=1;reason_counter[reason]+=1
                if d<=guard:
                    score_rows.append({'signal_session_date':d.isoformat(),'security_id':sid,
                        'issuer_id':item.issuer_id,'industry':item.industry,
                        'score':'' if score is None else f'{score:.10g}','unscorable_reason':reason})
                continue
            result = tri_results.get(sid)
            tri = result.tri if result else {}
            active_block = next((why for since, why in sorted(dated_blocks[sid]) if since <= d.isoformat()), None)
            if active_block:
                reason, score = active_block, None
            elif sid in blocked_sids:
                reason = "unresolved_corporate_action_input"
                score = None
            elif not tri:
                reason = "no_price_data"
                n_no_price += 1
                if sid in historical_unpriced:
                    n_historical += 1
                score = None
            else:
                score, _, _, _, reason = momentum_score(
                    tri, d, cfg.L, session_dates, session_index=session_pos,
                    first_valid_price_date=first_price.get(sid),
                    unresolved_from=result.invalid_from,
                )
            finite = score is not None and math.isfinite(float(score))
            if not finite and score is not None:
                reason = "non_finite_tri"
                score = None
            if score is not None:
                n_scored += 1
                if not item.industry.startswith("UNKNOWN:"):
                    n_known_industry += 1
            else:
                reasons[reason or "unknown"] += 1
                reason_counter[reason or "unknown"] += 1
            score_rows.append({
                "signal_session_date": d.isoformat(), "security_id": sid,
                "issuer_id": item.issuer_id, "industry": item.industry,
                "score": "" if score is None else f"{score:.10g}",
                "unscorable_reason": reason,
            })
        ratio = n_scored / n_eligible if n_eligible else 0.0
        n_remediable = n_eligible - n_scored - n_historical
        repair_ratio = n_remediable / n_eligible if n_eligible else 1.0
        industry_ratio = n_known_industry / n_scored if n_scored else 0.0
        passes = n_eligible > 0 and repair_ratio <= repair_gate and industry_ratio >= industry_gate
        week_rows.append({
            "signal_session_date": d.isoformat(), "n_eligible": n_eligible, "n_scored": n_scored,
            "score_ratio": f"{ratio:.10f}", "passes_gate": "1" if passes else "0",
            "n_no_price": n_no_price, "n_historical_no_price": n_historical,
            "historical_no_price_ratio": f"{(n_historical / n_eligible) if n_eligible else 0.0:.10f}",
            "n_remediable_unscorable": n_remediable, "remediable_gap_ratio": f"{repair_ratio:.10f}",
            "n_scored_known_industry": n_known_industry, "scored_industry_ratio": f"{industry_ratio:.10f}",
            "passes_original_score_gate": "1" if ratio >= gate else "0",
            "reason": ";".join(f"{k}:{v}" for k, v in reasons.most_common()),
        })

    best_start, best_end = _longest_passing(week_rows)
    _write_csv(out_dir / "sample_scores.csv",
               ["signal_session_date", "security_id", "issuer_id", "industry", "score", "unscorable_reason"],
               score_rows)
    _write_csv(out_dir / "weekly_coverage.csv",
               ["signal_session_date", "n_eligible", "n_scored", "score_ratio", "passes_gate",
                "n_no_price", "n_historical_no_price", "historical_no_price_ratio", "n_remediable_unscorable",
                "remediable_gap_ratio", "n_scored_known_industry", "scored_industry_ratio", "passes_original_score_gate", "reason"], week_rows)

    scores_by_week: dict[str, list[dict]] = defaultdict(list)
    for row in score_rows:
        scores_by_week[row["signal_session_date"]].append(row)
    pass_idx = [i for i, row in enumerate(week_rows) if row["passes_gate"] == "1"]
    decision_idx = [i for i in pass_idx if guard is None or sigs[i].session_date<=guard]
    sample_idx = sorted({decision_idx[0], decision_idx[len(decision_idx) // 2], decision_idx[-1]}) if decision_idx else []
    decision_rows: list[dict] = []
    for index in sample_idx:
        signal_date = sigs[index].session_date.isoformat()
        candidates = []
        for row in scores_by_week[signal_date]:
            if row["score"]:
                value = float(row["score"])
                if math.isfinite(value):
                    candidates.append(Candidate(row["security_id"], row["issuer_id"], row["industry"], value))
        selection = select_portfolio(candidates, [], cfg.N, cfg.k, cfg.b)
        for decision in selection.decisions:
            decision_rows.append({
                "signal_session_date": signal_date, "action": decision.action,
                "security_id": decision.security_id, "industry": decision.industry,
                "score": "" if decision.score is None else f"{decision.score:.10g}",
                "reason": decision.reason, "target_count": selection.target_count,
            })
    _write_csv(out_dir / "sample_decisions.csv",
               ["signal_session_date", "action", "security_id", "industry", "score", "reason", "target_count"],
               decision_rows)
    _write_csv(out_dir / "sample_signals.csv", ["signal_session_date", "coverage_passes_gate"],
               [{"signal_session_date": sigs[i].session_date.isoformat(), "coverage_passes_gate": "1"}
                for i in sample_idx])

    n_pass = len(pass_idx)
    ratios = [float(r["score_ratio"]) for r in week_rows if r["n_eligible"] > 0]
    total_blocking = len(errors) + len(preparation_errors)
    review_pending = []
    gap_path = project_root / paths.CORPORATE_ACTION_GAPS
    if gap_path.is_file():
        with gap_path.open(encoding="utf-8-sig", newline="") as handle:
            review_pending = [r for r in csv.DictReader(handle)
                              if (r.get("message") == "abs_close_change_ge_40pct_without_event" and r.get("conclusion") in {"", "unresolved"})
                              or r.get("message") == "distinct_cash_dividends_require_source_review"]
    best_length = best_end - best_start + 1 if best_start is not None else 0
    sufficient = best_length >= 2  # a development and a holdout segment must both exist
    # Pending evidence is reflected in dated, unscorable repair gaps. It is
    # not an additional hidden global zero-gap requirement.
    ready = not total_blocking and sufficient
    eligible_weeks = [r for r in week_rows if r["n_eligible"] > 0]
    by_year = defaultdict(list)
    for row in eligible_weeks:
        by_year[row["signal_session_date"][:4]].append(float(row["historical_no_price_ratio"]))
    annual = [{"year": y,"signal_weeks":len(vals),"min":min(vals),"median":statistics.median(vals),"max":max(vals)}
              for y,vals in sorted(by_year.items())]
    _write_csv(out_dir / "historical_no_price_by_year.csv", ["year","signal_weeks","min","median","max"], annual)
    summary = {
        "status": "failed" if not ready else "ok",
        "stage3_data_ready": ready,
        "pending_action_reviews": len(review_pending),
        "pending_dividend_groups": sum(len(rows) for rows in dated_blocks.values()),
        "can_partition_development_and_holdout": sufficient,
        "load_only_no_backtest": True,
        "holdout_guard_date": guard.isoformat() if guard else None,
        "holdout_coverage_is_availability_only": guard is not None,
        "score_coverage_gate": gate,
        "gate_note": "engineering gate, unvalidated",
        "remediable_gap_max_ratio": repair_gate, "historical_no_price_max_ratio": None,
        "scored_industry_min_ratio_gate": industry_gate,
        "remediable_gap_ratio_max": max((float(r["remediable_gap_ratio"]) for r in eligible_weeks), default=None),
        "remediable_gap_ratio_median": statistics.median([float(r["remediable_gap_ratio"]) for r in eligible_weeks]) if eligible_weeks else None,
        "historical_no_price_ratio_max": max((float(r["historical_no_price_ratio"]) for r in eligible_weeks), default=None),
        "historical_no_price_ratio_median": statistics.median([float(r["historical_no_price_ratio"]) for r in eligible_weeks]) if eligible_weeks else None,
        "scored_industry_ratio_min": min((float(r["scored_industry_ratio"]) for r in eligible_weeks), default=None),
        "historical_no_price_by_year": annual,
        "calendar_sessions": len(inputs.calendar), "securities": len(inputs.securities),
        "snapshots": len(inputs.snapshots), "signal_weeks": len(week_rows),
        "weeks_pass_gate": n_pass, "weeks_fail_gate": len(week_rows) - n_pass,
        "coverage_ratio_min": min(ratios) if ratios else 0.0,
        "coverage_ratio_median": statistics.median(ratios) if ratios else 0.0,
        "unscorable_reasons": dict(reason_counter),
        "longest_passing_interval": {
            "start": week_rows[best_start]["signal_session_date"] if best_start is not None else None,
            "end": week_rows[best_end]["signal_session_date"] if best_end is not None else None,
            "n_weeks": best_end - best_start + 1 if best_start is not None else 0,
        },
        "blocking_issues": total_blocking,
        "input_errors": len(errors), "preparation_errors": len(preparation_errors),
        "note": "scores/decisions only; NO equity curve, NO strategy return, NO VOO comparison",
    }
    (out_dir / "validate_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _register_experiment(project_root, inputs, week_rows, best_start, best_end, gate,
                         total_blocking)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ready else 1
