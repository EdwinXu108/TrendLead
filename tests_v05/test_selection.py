"""Selection: feasible order F, buffer line, industry cap, fill, idempotence."""

from __future__ import annotations

from leadtrend_v05.selection import Candidate, build_feasible_order, select_portfolio


def c(sid: str, score: float, industry: str = "TECH", issuer: str | None = None) -> Candidate:
    return Candidate(
        security_id=sid,
        issuer_id=issuer or f"ISS_{sid}",
        industry=industry,
        score=score,
    )


def test_feasible_order_respects_industry_cap():
    ranked = [
        c("A1", 0.9, "TECH"),
        c("A2", 0.8, "TECH"),
        c("A3", 0.7, "TECH"),
        c("A4", 0.65, "TECH"),
        c("B1", 0.6, "FIN"),
        c("C1", 0.5, "HLTH"),
    ]
    F = build_feasible_order(ranked, k=2)
    assert [x.security_id for x in F] == ["A1", "A2", "B1", "C1"]


def test_buffer_line_not_at_end_of_F():
    """len(F) > min(ceil(b*N), len(F)) so buffer is not F's last element.

    N=3, b=2 -> ceil(6)=6; with 3 industries and k=2, F can hold 6, but we give 8
    ranked names so F has exactly 6... wait we need len(F) > 6.
    Use N=3, k=2, industries=4 -> len(F) up to 8; buffer pos = min(6, 8) = 6 < 8.
    """
    ranked = []
    # 4 industries x 2 = 8 feasible slots
    for i, ind in enumerate(["TECH", "FIN", "HLTH", "ENRG"]):
        ranked.append(c(f"{ind}_1", 0.90 - i * 0.01, ind))
        ranked.append(c(f"{ind}_2", 0.80 - i * 0.01, ind))
    # extra same-industry names that cannot enter F
    ranked.append(c("TECH_3", 0.85, "TECH"))
    ranked.append(c("FIN_3", 0.75, "FIN"))

    F = build_feasible_order(ranked, k=2)
    assert len(F) == 8
    N, b = 3, 2.0
    import math

    pos = min(math.ceil(b * N), len(F))
    assert pos == 6
    assert pos < len(F)  # buffer must not be the last F element


def test_holding_above_buffer_retained_even_if_not_in_F():
    """A held name can sit outside F (industry full of higher names) but still be
    retained when its sort key is not worse than the buffer line."""
    N, k, b = 2, 1, 2.0  # buffer pos = min(4, len(F))
    # Industry TECH: three names; F will take only 1 per industry
    ranked = [
        c("T_TOP", 0.9, "TECH"),
        c("F_TOP", 0.8, "FIN"),
        c("H_TOP", 0.7, "HLTH"),
        c("T_HOLD", 0.65, "TECH"),  # held; not in F because TECH full
        c("T_LOW", 0.2, "TECH"),
    ]
    F = build_feasible_order(ranked, k=1)
    assert "T_HOLD" not in {x.security_id for x in F}
    # buffer pos = min(4, 3) = 3 -> F[2] = H_TOP score 0.7
    # T_HOLD score 0.65 < 0.7 -> below buffer -> exit
    sel_below = select_portfolio(ranked, held_ids=["T_HOLD", "F_TOP"], N=N, k=k, b=b)
    assert sel_below.buffer_security_id == "H_TOP"
    assert "T_HOLD" in {d.security_id for d in sel_below.decisions if d.action == "exit"}
    assert any(d.reason == "below_buffer_line" and d.security_id == "T_HOLD" for d in sel_below.decisions)

    # Now make T_HOLD score higher than buffer (H_TOP=0.7) while still outside F
    ranked2 = [
        c("T_TOP", 0.95, "TECH"),
        c("F_TOP", 0.8, "FIN"),
        c("H_TOP", 0.7, "HLTH"),
        c("T_HOLD", 0.75, "TECH"),  # above buffer 0.7, but F already took T_TOP for TECH
    ]
    F2 = build_feasible_order(ranked2, k=1)
    assert "T_HOLD" not in {x.security_id for x in F2}
    sel_keep = select_portfolio(ranked2, held_ids=["T_HOLD", "F_TOP"], N=N, k=k, b=b)
    assert sel_keep.buffer_security_id == "H_TOP"
    kept = {d.security_id for d in sel_keep.decisions if d.action == "retain"}
    assert "T_HOLD" in kept


def test_holding_below_buffer_exits_and_fill_takes_best_unheld():
    ranked = [
        c("A1", 0.9, "TECH"),
        c("B1", 0.8, "FIN"),
        c("C1", 0.7, "HLTH"),
        c("D1", 0.6, "ENRG"),
        c("H_OLD", 0.1, "TECH"),  # held, way below buffer
        c("NEW1", 0.55, "CONS"),
    ]
    sel = select_portfolio(ranked, held_ids=["H_OLD"], N=2, k=1, b=2.0)
    assert "H_OLD" not in sel.target_ids
    assert set(sel.target_ids) == {"A1", "B1"}
    assert any(d.security_id == "H_OLD" and d.reason == "below_buffer_line" for d in sel.decisions)
    assert any(d.security_id == "A1" and d.action == "fill" for d in sel.decisions)


def test_industry_cap_reduces_low_score_retains_before_total():
    ranked = [
        c("T1", 0.9, "TECH"),
        c("T2", 0.85, "TECH"),
        c("T3", 0.8, "TECH"),
        c("T4", 0.75, "TECH"),
        c("F1", 0.5, "FIN"),
    ]
    # hold all four TECH; N=3, k=2
    sel = select_portfolio(
        ranked,
        held_ids=["T1", "T2", "T3", "T4"],
        N=3,
        k=2,
        b=2.0,
    )
    # industry overflow drops T3 and T4 (lowest among TECH retains)
    exits = {d.security_id for d in sel.decisions if d.action == "exit"}
    assert "T3" in exits and "T4" in exits
    assert set(sel.target_ids) == {"T1", "T2", "F1"}


def test_selection_idempotent_no_churn():
    ranked = [
        c("A1", 0.9, "TECH"),
        c("A2", 0.88, "TECH"),
        c("B1", 0.8, "FIN"),
        c("B2", 0.78, "FIN"),
        c("C1", 0.7, "HLTH"),
        c("D1", 0.6, "ENRG"),
        c("E1", 0.5, "CONS"),
        c("F1", 0.4, "INDU"),
    ]
    sel1 = select_portfolio(ranked, held_ids=[], N=5, k=2, b=2.0)
    held = list(sel1.target_ids)
    sel2 = select_portfolio(ranked, held_ids=held, N=5, k=2, b=2.0)
    assert sorted(sel2.target_ids) == sorted(held)
    assert all(d.action in {"retain", "skip"} for d in sel2.decisions if d.security_id in held)
    # no exit/fill on second application
    assert not any(d.action in {"exit", "fill"} for d in sel2.decisions if d.security_id in set(held))


def test_underfill_reported_not_padded():
    ranked = [c("A1", 0.9, "TECH"), c("B1", 0.8, "FIN")]
    sel = select_portfolio(ranked, held_ids=[], N=5, k=3, b=2.0)
    assert sel.target_count == 2
    assert sel.filled_all is False
    assert any("underfilled" in n for n in sel.notes)


def test_duplicate_issuer_not_filled_twice():
    ranked = [
        c("X1", 0.9, "TECH", issuer="ISS_X"),
        c("X2", 0.89, "TECH", issuer="ISS_X"),
        c("Y1", 0.5, "FIN", issuer="ISS_Y"),
    ]
    sel = select_portfolio(ranked, held_ids=[], N=2, k=3, b=2.0)
    assert sel.target_ids.count("X1") + sel.target_ids.count("X2") <= 1
    assert "X1" in sel.target_ids
