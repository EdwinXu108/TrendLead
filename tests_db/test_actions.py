"""Unit tests of the corporate-action triage rules (synthetic inputs, no archive)."""
from __future__ import annotations

import pytest

from leadtrend_db import actions_v2


def bar(open_, close):
    return dict(open=open_, high=max(open_, close), low=min(open_, close), close=close, volume=1.0, turnover=1.0)


def prices_for(days):
    return {'S': {day: (bar(10.0, 10.0), 'SRC', 'TST') for day in days}}


def cash(action_date, amount, kind='cash_dividend'):
    return dict(security_id='S', action_date=action_date, action_type=kind, ratio=None, cash_amount=amount,
                needs_review=1, currency='USD', source='opend', source_id='SRC', notes='')


def rehab(day, per_cash=None, special=None, split=None, join=None, spin=None, factor=None):
    cash = (per_cash or 0.0) + (special or 0.0)
    row = dict(ex_div_date=day, per_cash_div=per_cash, special_dividend=special,
               forward_adj_factorA=factor if factor is not None else ((1 - cash / 10.0) if cash else 1.0),
               forward_adj_factorB=0.0, spin_off_ratio=spin, _source_id='SRC')
    if split:
        row['split_base'], row['split_ert'] = split
    if join:
        row['join_base'], row['join_ert'] = join
    return row


def run(actions, raw, days=('2024-01-02', '2024-01-03')):
    rows, stats, reviews = actions_v2.finalize({'S': actions}, {'TST': (raw, [])}, prices_for(days), {'S': 'TST'})
    return rows, stats, reviews


def test_multiple_unidentified_amounts_remain_unresolved_even_when_vendor_factor_matches():
    rows, stats, _ = run([cash('2024-01-03', 0.369), cash('2024-01-03', 0.451)],
                         [rehab('2024-01-03', per_cash=0.82)])
    assert len(rows) == 1
    assert rows[0]['cash_amount'] == pytest.approx(0.82)
    assert rows[0]['resolution'] == 'cash_conflict_unresolved'
    assert rows[0]['needs_review'] == 1


def test_regular_plus_special_are_added_when_rehab_separates_them():
    rows, _, _ = run([dict(cash('2024-01-03', 0.58), component_id='regular'),
                      dict(cash('2024-01-03', 0.2, 'special_dividend'), component_id='special')],
                     [rehab('2024-01-03', per_cash=0.58, special=0.2)])
    assert len(rows) == 1
    assert rows[0]['cash_amount'] == pytest.approx(0.78)
    assert rows[0]['resolution'] == 'identified_components'


def test_official_total_does_not_add_its_tax_component():
    result = actions_v2.resolve_cash_components([cash('2024-01-03', .609), cash('2024-01-03', .331)],
        {'per_cash_div': .94, 'special_dividend': .331}, {'official_cash': .94, 'source_url': 'official'})
    assert result['amount'] == pytest.approx(.94)
    assert result['needs_review'] == 0


def test_equal_independently_identified_components_are_both_included():
    items = [dict(cash('2024-01-03', .6), component_id='regular'),
             dict(cash('2024-01-03', .6), component_id='special')]
    result = actions_v2.resolve_cash_components(items, {})
    assert result['amount'] == pytest.approx(1.2)
    assert result['needs_review'] == 0


def test_revisions_are_not_added_and_order_does_not_change_resolution():
    rows = [rehab('2024-01-03', per_cash=.7, factor=.93),
            rehab('2024-01-03', per_cash=.7, factor=.931)]
    forward, _, _ = run([cash('2024-01-03', .7)], rows)
    backward, _, _ = run([cash('2024-01-03', .7)], list(reversed(rows)))
    assert forward == backward
    assert forward[0]['cash_amount'] == pytest.approx(.7)
    assert forward[0]['resolution'] == 'vendor_reported'


def test_cash_missing_from_the_endpoint_is_restored_from_rehab_on_its_ex_date():
    rows, _, _ = run([], [rehab('2024-01-03', per_cash=0.25)])
    assert len(rows) == 1
    assert rows[0]['resolution'] == 'vendor_reported'
    assert rows[0]['cash_amount'] == pytest.approx(0.25)
    assert rows[0]['action_date'] == '2024-01-03'


def test_a_stock_dividend_becomes_a_split():
    rows, _, _ = run([], [rehab('2024-01-03', per_cash=0.1)],
                     days=('2024-01-02', '2024-01-03'))
    assert len(rows) == 1
    assert rows[0]['action_type'] == 'cash_dividend'
    rows, stats, _ = actions_v2.finalize(
        {'S': []}, {'TST': ([], [dict(ex_date='2024/01/03', statement='1股派0.00275股', _source_id='SRC')])},
        prices_for(('2024-01-02', '2024-01-03')), {'S': 'TST'})
    assert len(rows) == 1
    assert rows[0]['action_type'] == 'split'
    assert rows[0]['ratio'] == pytest.approx(1.00275)
    assert rows[0]['resolution'] == 'stock_dividend_parsed'


def test_class_distribution_takes_the_vendor_split_ratio():
    rows, _, _ = run([dict(cash('2024-01-03', None, 'class_distribution'), cash_amount=None)],
                     [rehab('2024-01-03', split=(10000.0, 19981.0))])
    assert rows[0]['ratio'] == pytest.approx(1.9981)
    assert rows[0]['resolution'] == 'class_distribution_vendor_split_ratio'


def test_spinoff_without_any_evidence_is_flagged_not_invented():
    rows, stats, _ = run([dict(cash('2024-01-03', None, 'spinoff'), cash_amount=None)], [rehab('2024-01-03')])
    assert rows[0]['spin_value'] is None
    assert rows[0]['resolution'] == 'spinoff_no_value_evidence'
    assert rows[0]['needs_review'] == 1


def test_spinoff_uses_the_vendor_factor_when_no_child_price_is_documented():
    rows, _, _ = run([dict(cash('2024-01-03', None, 'spinoff'), cash_amount=None)],
                     [rehab('2024-01-03', spin=0.78)])
    assert rows[0]['vendor_factor'] == pytest.approx(0.78)
    assert rows[0]['resolution'] == 'vendor_factor'


def test_spinoff_override_file_adds_the_missing_distribution(tmp_path, monkeypatch):
    cfg = tmp_path / 'spinoff.csv'
    cfg.write_text('security_id,ticker,action_date,sub_ticker,value_basis,reason,evidence\n'
                   'S,TST,2024-01-03,CHILD,ex_date_open_drop,because,evidence\n', encoding='utf8')
    monkeypatch.setattr(actions_v2, 'SPINOFF_CONFIG', cfg)
    rows, stats, _ = run([], [])
    assert [r['action_type'] for r in rows] == ['spinoff']
    assert rows[0]['sub_ticker'] == 'CHILD'
    assert rows[0]['resolution'] == 'ex_date_open_drop'


def test_equal_unidentified_records_do_not_claim_one_component():
    result = actions_v2.resolve_cash_components([cash('2024-01-03', .6), cash('2024-01-03', .6)], {})
    assert result['needs_review'] == 1
    assert result['resolution'] == 'cash_conflict_unresolved'
