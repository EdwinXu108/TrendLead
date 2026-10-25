"""Eligibility and share-class selection rules."""

from __future__ import annotations

from datetime import date

from leadtrend_v05.eligibility import (
    ClassSelector,
    build_price_index,
    evaluate_security,
)
from tests_v05.conftest import make_config, price, sec


def test_excluded_types_and_bkr_a():
    cfg = make_config()
    assert evaluate_security(sec("X", stype="common_stock"), cfg).eligible
    assert not evaluate_security(sec("BRKA", ticker="BRK.A"), cfg).eligible
    assert evaluate_security(sec("BRKB", ticker="BRK.B"), cfg).eligible
    assert not evaluate_security(sec("R", stype="reit"), cfg).eligible
    assert not evaluate_security(sec("S", stype="spac"), cfg).eligible
    assert not evaluate_security(sec("L", stype="partnership_unit"), cfg).eligible
    assert not evaluate_security(sec("E", stype="etf"), cfg).eligible
    # equity preferred ADR is allowed (not excluded by keyword)
    assert evaluate_security(sec("P", stype="adr_preferred_equity"), cfg).eligible
    # fixed-income preferred is not
    assert not evaluate_security(sec("PF", stype="preferred_fixed"), cfg).eligible
    assert not evaluate_security(sec("O", exchange="OTC"), cfg).eligible


def test_class_selection_prefers_higher_turnover_independent_of_scores():
    cfg = make_config(class_selection_turnover_days=3)
    days = [date(2025, 2, 10), date(2025, 2, 11), date(2025, 2, 12)]
    prices = []
    for d in days:
        prices.append(price("A_CLASS", d, 10.0, turnover=2_000_000))
        prices.append(price("B_CLASS", d, 10.0, turnover=500_000))
    idx = build_price_index(prices)
    sel = ClassSelector(cfg, idx)
    a = evaluate_security(sec("A_CLASS", issuer="ISS", stype="common_stock"), cfg)
    b = evaluate_security(sec("B_CLASS", issuer="ISS", stype="common_stock"), cfg)
    chosen = sel.select_for_issuer("ISS", [a, b], date(2025, 2, 12))
    assert chosen == "A_CLASS"


def test_class_selection_stable_under_row_order():
    cfg = make_config(class_selection_turnover_days=3)
    days = [date(2025, 2, 10), date(2025, 2, 11), date(2025, 2, 12)]
    prices = []
    for d in days:
        prices.append(price("A_CLASS", d, 10.0, turnover=2_000_000))
        prices.append(price("B_CLASS", d, 10.0, turnover=500_000))
    idx = build_price_index(prices)
    a = evaluate_security(sec("A_CLASS", issuer="ISS"), cfg)
    b = evaluate_security(sec("B_CLASS", issuer="ISS"), cfg)

    s1 = ClassSelector(cfg, idx)
    assert s1.select_for_issuer("ISS", [a, b], date(2025, 2, 12)) == "A_CLASS"
    s2 = ClassSelector(cfg, idx)
    assert s2.select_for_issuer("ISS", [b, a], date(2025, 2, 12)) == "A_CLASS"


def test_class_selection_degrades_to_stable_id_without_turnover():
    cfg = make_config()
    idx = build_price_index([])
    a = evaluate_security(sec("Z_CLASS", issuer="ISS"), cfg)
    b = evaluate_security(sec("A_CLASS", issuer="ISS"), cfg)
    sel = ClassSelector(cfg, idx)
    chosen = sel.select_for_issuer("ISS", [a, b], date(2025, 2, 12))
    assert chosen == "A_CLASS"  # min security_id
    assert sel.notes[-1]["method"].startswith("stable_security_id")


def test_class_selection_does_not_depend_on_L_or_N():
    """ClassSelector never receives L/N — timeline is shared across parameters."""
    import inspect

    sig = inspect.signature(ClassSelector.select_for_issuer)
    assert "L" not in sig.parameters
    assert "N" not in sig.parameters
