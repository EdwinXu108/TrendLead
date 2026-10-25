"""Portfolio selection: feasible order F, retention buffer, industry cap, fill."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

from .scoring import ScoreRow


@dataclass
class Candidate:
    security_id: str
    issuer_id: str
    industry: str
    score: float

    @property
    def sort_key(self) -> tuple[float, str]:
        return (-self.score, self.security_id)


@dataclass
class Decision:
    action: str  # retain | exit | fill | skip | unexecuted_target
    security_id: str
    issuer_id: str
    industry: str
    score: float | None
    reason: str
    rank: int | None = None
    buffer_key_score: float | None = None
    buffer_key_id: str | None = None
    industry_counts_after: dict[str, int] = field(default_factory=dict)
    in_feasible_order: bool = False
    feasible_index: int | None = None


@dataclass
class SelectionResult:
    target_ids: list[str]
    decisions: list[Decision]
    buffer_score: float | None
    buffer_security_id: str | None
    feasible_order: list[str]
    filled_all: bool
    target_count: int
    notes: list[str]


def _sort_key(score: float, security_id: str) -> tuple[float, str]:
    return (-score, security_id)


def build_feasible_order(
    ranked: list[Candidate],
    k: int,
    industry_capacity: dict[str, int] | None = None,
) -> list[Candidate]:
    """Scan ranked list; accept a name if its industry still has capacity."""
    caps = dict(industry_capacity or {})
    used: dict[str, int] = {}
    F: list[Candidate] = []
    for c in ranked:
        cap = caps.get(c.industry, k)
        if used.get(c.industry, 0) < cap:
            F.append(c)
            used[c.industry] = used.get(c.industry, 0) + 1
    return F


def select_portfolio(
    candidates: list[Candidate],
    held_ids: list[str],
    N: int,
    k: int,
    b: float,
    frozen: list[Candidate] | None = None,
) -> SelectionResult:
    """Deterministic selection. Same inputs + already-compliant holdings => no churn.

    - Buffer line comes from feasible order F at position min(ceil(b*N), len(F)).
    - A held name need NOT be in F; it only needs a sort key not worse than the buffer.
    - Reduce industry overflow (lowest score first), then total overflow, then fill.
    """
    notes: list[str] = []
    frozen = frozen or []
    frozen_ids = {c.security_id for c in frozen}
    frozen_issuers = {c.issuer_id for c in frozen}
    frozen_counts: dict[str, int] = {}
    for c in frozen:
        frozen_counts[c.industry] = frozen_counts.get(c.industry, 0) + 1
    total_capacity = max(0, N-len(frozen_ids))
    caps = {ind:max(0,k-count) for ind,count in frozen_counts.items()}
    if len(frozen_ids)>N or any(x>k for x in frozen_counts.values()):
        notes.append('passive_frozen_limit_exceeded')
    candidates = [c for c in candidates if c.security_id not in frozen_ids and c.issuer_id not in frozen_issuers]
    ranked = sorted(candidates, key=lambda c: c.sort_key)
    rank_of = {c.security_id: i + 1 for i, c in enumerate(ranked)}
    by_id = {c.security_id: c for c in ranked}
    held_set = set(held_ids)-frozen_ids

    F = build_feasible_order(ranked, k, caps)
    f_index = {c.security_id: i + 1 for i, c in enumerate(F)}

    pos = min(math.ceil(b * N), len(F))
    if pos <= 0:
        buffer_score = None
        buffer_id = None
        buffer_key: tuple[float, str] | None = None
        notes.append("feasible_order_empty_no_retention")
    else:
        buf_c = F[pos - 1]
        buffer_score = buf_c.score
        buffer_id = buf_c.security_id
        buffer_key = buf_c.sort_key
        notes.append(f"buffer_from_F[{pos}]:{buffer_id}")

    # Held names still eligible & not worse than buffer.
    retain: list[Candidate] = []
    exited: list[Decision] = []

    for hid in sorted(held_set):
        c = by_id.get(hid)
        if c is None:
            # eligibility / universe handled outside; here means not in scored candidate set
            continue
        if buffer_key is None:
            continue
        if c.sort_key <= buffer_key:
            retain.append(c)
        else:
            exited.append(
                Decision(
                    action="exit",
                    security_id=c.security_id,
                    issuer_id=c.issuer_id,
                    industry=c.industry,
                    score=c.score,
                    reason="below_buffer_line",
                    rank=rank_of.get(c.security_id),
                    buffer_key_score=buffer_score,
                    buffer_key_id=buffer_id,
                    in_feasible_order=c.security_id in f_index,
                    feasible_index=f_index.get(c.security_id),
                )
            )

    retain.sort(key=lambda c: c.sort_key)

    # 1) industry cap among retains: drop lowest-score overflow
    ind_counts: dict[str, int] = {}
    for c in retain:
        ind_counts[c.industry] = ind_counts.get(c.industry, 0) + 1

    remove_ids: set[str] = set()
    for ind, cnt in sorted(ind_counts.items()):
        capacity = caps.get(ind,k)
        if cnt > capacity:
            in_ind = [c for c in retain if c.industry == ind]
            for c in in_ind[capacity:]:
                remove_ids.add(c.security_id)
                exited.append(
                    Decision(
                        action="exit",
                        security_id=c.security_id,
                        issuer_id=c.issuer_id,
                        industry=c.industry,
                        score=c.score,
                        reason="industry_cap_overflow",
                        rank=rank_of.get(c.security_id),
                        buffer_key_score=buffer_score,
                        buffer_key_id=buffer_id,
                        in_feasible_order=c.security_id in f_index,
                        feasible_index=f_index.get(c.security_id),
                    )
                )

    retain = [c for c in retain if c.security_id not in remove_ids]

    # 2) total N: drop lowest-score overflow
    if len(retain) > total_capacity:
        for c in retain[total_capacity:]:
            remove_ids.add(c.security_id)
            exited.append(
                Decision(
                    action="exit",
                    security_id=c.security_id,
                    issuer_id=c.issuer_id,
                    industry=c.industry,
                    score=c.score,
                    reason="total_cap_overflow",
                    rank=rank_of.get(c.security_id),
                    buffer_key_score=buffer_score,
                    buffer_key_id=buffer_id,
                    in_feasible_order=c.security_id in f_index,
                    feasible_index=f_index.get(c.security_id),
                )
            )
        retain = retain[:total_capacity]

    # 3) fill from highest-score unheld candidates
    keep_ids = {c.security_id for c in retain}
    fill_ind: dict[str, int] = {}
    for c in retain:
        fill_ind[c.industry] = fill_ind.get(c.industry, 0) + 1
    used_issuers = {c.issuer_id for c in retain}

    fills: list[Candidate] = []
    for c in ranked:
        if len(retain) + len(fills) >= total_capacity:
            break
        if c.security_id in keep_ids or c.security_id in held_set:
            continue
        if c.issuer_id in used_issuers:
            continue
        if fill_ind.get(c.industry, 0) >= caps.get(c.industry,k):
            continue
        fills.append(c)
        used_issuers.add(c.issuer_id)
        fill_ind[c.industry] = fill_ind.get(c.industry, 0) + 1

    target = retain + fills
    target_ids = [c.security_id for c in target]
    filled_all = len(target_ids) == total_capacity
    if not filled_all:
        notes.append(f"underfilled_target={len(target_ids)}/{N}")

    decisions: list[Decision] = []
    final_ind: dict[str, int] = {}
    for c in target:
        final_ind[c.industry] = final_ind.get(c.industry, 0) + 1

    retain_ids = {c.security_id for c in retain}
    fill_ids = {c.security_id for c in fills}
    for c in ranked:
        in_f = c.security_id in f_index
        if c.security_id in retain_ids:
            action, reason = "retain", "retained_above_buffer"
        elif c.security_id in fill_ids:
            action, reason = "fill", "fill_unheld_candidate"
        elif c.security_id in held_set:
            # exited for some reason already recorded; avoid duplicate if already appended
            continue
        else:
            action, reason = "skip", "not_selected"
        decisions.append(
            Decision(
                action=action,
                security_id=c.security_id,
                issuer_id=c.issuer_id,
                industry=c.industry,
                score=c.score,
                reason=reason,
                rank=rank_of.get(c.security_id),
                buffer_key_score=buffer_score,
                buffer_key_id=buffer_id,
                industry_counts_after=dict(final_ind),
                in_feasible_order=in_f,
                feasible_index=f_index.get(c.security_id),
            )
        )

    # held-but-unscored / lost eligibility exits are appended by caller
    decisions.extend(exited)
    decisions.sort(key=lambda d: (d.security_id, d.action))

    return SelectionResult(
        target_ids=target_ids,
        decisions=decisions,
        buffer_score=buffer_score,
        buffer_security_id=buffer_id,
        feasible_order=[c.security_id for c in F],
        filled_all=filled_all,
        target_count=len(target_ids),
        notes=notes,
    )


def draw_order(seed: int, signal_date: str, security_id: str) -> str:
    """Fixed pseudo-random order key over securities: sha256 of (seed, signal date, security)."""
    return hashlib.sha256(f"{seed}|{signal_date}|{security_id}".encode("utf-8")).hexdigest()


def select_random_matched(
    candidates: list[Candidate],
    held_ids: list[str],
    N: int,
    k: int,
    b: float,
    frozen: list[Candidate] | None = None,
    *,
    signal_date: str,
    seed: int,
    exit_counts: dict[str, int],
    held_meta: dict[str, tuple[str, str]],
) -> SelectionResult:
    """Turnover-matched random control; same frozen, industry-cap, issuer and fill rules as select_portfolio.

    Per signal week, in this order:
    a) held names with no score this week leave (random_forced_exit, as the default rule leaves them);
    b) max(0, reference full exits - those forced exits) scorable held names leave, lowest draw first (random_exit);
    c) remaining held names are kept, subject to the industry cap and N (overflow leaves as random_forced_exit);
    d) the holding is filled to N minus frozen, from scorable unheld candidates in draw order (random_fill).
    There is no retention buffer, so b (the buffer parameter) is accepted and unused.
    exit_counts maps signal date to the reference configuration's full exits that week; held_meta maps security to
    (issuer_id, industry) for every held name.
    """
    notes: list[str] = []
    frozen = frozen or []
    frozen_ids = {c.security_id for c in frozen}
    frozen_issuers = {c.issuer_id for c in frozen}
    frozen_counts: dict[str, int] = {}
    for c in frozen:
        frozen_counts[c.industry] = frozen_counts.get(c.industry, 0) + 1
    total_capacity = max(0, N - len(frozen_ids))
    caps = {ind: max(0, k - count) for ind, count in frozen_counts.items()}
    if len(frozen_ids) > N or any(x > k for x in frozen_counts.values()):
        notes.append('passive_frozen_limit_exceeded')
    candidates = [c for c in candidates if c.security_id not in frozen_ids and c.issuer_id not in frozen_issuers]
    by_id = {c.security_id: c for c in candidates}
    held_set = set(held_ids) - frozen_ids
    ranked = sorted(candidates, key=lambda c: c.sort_key)
    rank_of = {c.security_id: i + 1 for i, c in enumerate(ranked)}

    def cap_of(industry: str) -> int:
        return caps.get(industry, k)

    def draw(sid: str) -> str:
        return draw_order(seed, signal_date, sid)

    unscorable = sorted(sid for sid in held_set if sid not in by_id)
    scorable_held = sorted((sid for sid in held_set if sid in by_id), key=draw)
    n_random = max(0, exit_counts.get(signal_date, 0) - len(unscorable))
    random_exit = scorable_held[:n_random]
    retain_ids = scorable_held[n_random:]

    overflow: list[str] = []
    counts: dict[str, int] = {}
    kept: list[str] = []
    for sid in retain_ids:
        ind = by_id[sid].industry
        if counts.get(ind, 0) < cap_of(ind):
            kept.append(sid)
            counts[ind] = counts.get(ind, 0) + 1
        else:
            overflow.append(sid)
    if len(kept) > total_capacity:
        overflow.extend(kept[total_capacity:])
        kept = kept[:total_capacity]
    retain = [by_id[sid] for sid in kept]

    used_issuers = {c.issuer_id for c in retain}
    fill_counts: dict[str, int] = {}
    for c in retain:
        fill_counts[c.industry] = fill_counts.get(c.industry, 0) + 1
    fills: list[Candidate] = []
    for c in sorted(candidates, key=lambda c: draw(c.security_id)):
        if len(retain) + len(fills) >= total_capacity:
            break
        if c.security_id in held_set:  # held names, including those that exited this week, are not refilled
            continue
        if c.issuer_id in used_issuers or fill_counts.get(c.industry, 0) >= cap_of(c.industry):
            continue
        fills.append(c)
        used_issuers.add(c.issuer_id)
        fill_counts[c.industry] = fill_counts.get(c.industry, 0) + 1

    target = retain + fills
    target_ids = [c.security_id for c in target]
    filled_all = len(target_ids) == total_capacity
    if not filled_all:
        notes.append(f"underfilled_target={len(target_ids)}/{N}")
    notes.append(f"random_matched_exits={len(random_exit)}")

    final_ind: dict[str, int] = {}
    for c in target:
        final_ind[c.industry] = final_ind.get(c.industry, 0) + 1
    retained = set(kept)
    fill_ids = {c.security_id for c in fills}

    def exit_decision(sid: str, reason: str) -> Decision:
        c = by_id.get(sid)
        issuer, industry = (c.issuer_id, c.industry) if c else held_meta[sid]
        return Decision(action="exit", security_id=sid, issuer_id=issuer, industry=industry,
                        score=c.score if c else None, reason=reason, rank=rank_of.get(sid))

    exits = {sid: "random_exit" for sid in random_exit}
    exits.update({sid: "random_forced_exit" for sid in overflow})
    decisions: list[Decision] = []
    for c in ranked:
        if c.security_id in exits:
            decisions.append(exit_decision(c.security_id, exits[c.security_id]))
            continue
        if c.security_id in retained:
            action, reason = "retain", "random_retained"
        elif c.security_id in fill_ids:
            action, reason = "fill", "random_fill"
        else:
            action, reason = "skip", "not_selected"
        decisions.append(Decision(action=action, security_id=c.security_id, issuer_id=c.issuer_id, industry=c.industry,
                                  score=c.score, reason=reason, rank=rank_of.get(c.security_id),
                                  industry_counts_after=dict(final_ind), in_feasible_order=False))
    for sid in unscorable:
        decisions.append(exit_decision(sid, "random_forced_exit"))
    decisions.sort(key=lambda d: (d.security_id, d.action))

    return SelectionResult(
        target_ids=target_ids,
        decisions=decisions,
        buffer_score=None,
        buffer_security_id=None,
        feasible_order=[],
        filled_all=filled_all,
        target_count=len(target_ids),
        notes=notes,
    )


def score_rows_to_candidates(
    rows: list[ScoreRow],
    eligible_ids: set[str],
) -> list[Candidate]:
    out: list[Candidate] = []
    for r in rows:
        if r.score is None or not math.isfinite(float(r.score)):
            continue
        if r.security_id not in eligible_ids:
            continue
        out.append(
            Candidate(
                security_id=r.security_id,
                issuer_id=r.issuer_id,
                industry=r.industry,
                score=float(r.score),
            )
        )
    return out

# Selection rules by name. "default" is the reviewed rule; the others exist only for strategy experiments.
SELECTION_RULES = {"default": select_portfolio, "random_matched": select_random_matched}
