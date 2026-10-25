"""Accounting rules v2: unverified fractional entitlements settle as cash at the first close (rule A).

The engine-level cases use the synthetic 2019 segment from test_stage3; the ledger cases call DevBook directly.
"""
import json
from datetime import date, timedelta

import pytest

from leadtrend_v05.dataio import EventRow
from leadtrend_v05.ledger import AccountState, pay_receivables
from leadtrend_v05.stage3_ledger import DevBook
from test_stage3 import _bounded_run_fixture, _rows


def _tenth(created):
    """Ten weekday sessions after created, as the synthetic calendar counts them (no holidays in the fixture)."""
    d, n = created, 0
    while n < 10:
        d = d + timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return d


def test_split_fraction_is_cash_at_first_close_and_paid_tenth_session():
    book = DevBook(AccountState(0, {'P': 3}))
    d = date(2024, 1, 2)
    book.split(EventRow('S', 'P', 'split', d, 1.5, None, '', None, 'official'), 80)
    assert book.account.shares_of('P') == 4 and book.security_receivables[0].shares == pytest.approx(.5)
    book.settle_fractions(d, {'P': 80}, _tenth)
    assert book.security_receivables == []
    assert book.account.receivable_cash == pytest.approx(40)
    assert book.components({'P': 80})['total_equity'] == pytest.approx(4.5 * 80)
    assert pay_receivables(book.account, _tenth(d) - timedelta(days=1)) == []
    assert [r.amount for r in pay_receivables(book.account, _tenth(d))] == [pytest.approx(40)]
    assert book.account.cash == pytest.approx(40) and book.account.receivable_cash == pytest.approx(0)


def test_no_price_means_no_cash_yet_and_the_slot_stays_frozen():
    book = DevBook(AccountState(0, {'P': 3}))
    d = date(2024, 1, 2)
    book.split(EventRow('S', 'P', 'split', d, 1.5, None, '', None, 'official'), 80)
    book.settle_fractions(d + timedelta(days=1), {}, _tenth)
    assert book.account.receivable_cash == 0 and book.frozen_ids() == {'P'}
    book.settle_fractions(d + timedelta(days=2), {'P': 70}, _tenth)
    assert book.security_receivables == [] and book.account.receivable_cash == pytest.approx(35)
    # The pay date counts from the effective session, not from the day the price appeared.
    assert book.account.receivables[0].pay_date == _tenth(d)
    assert book.account.receivables[0].pay_date != _tenth(d + timedelta(days=2))


def test_unverified_spinoff_fraction_becomes_cash_when_child_prices():
    book = DevBook(AccountState(0, {'P': 3}))
    d = date(2024, 1, 2)
    book.spinoff(EventRow('E', 'P', 'spinoff', d, None, None, '', None, 'official', 20, 'child_raw_close'),
                 [dict(security_id='C', shares_per_old=.2, source='official')], {}, {'P': 80, 'C': 100})
    assert book.security_receivables[0].shares == pytest.approx(.6) and book.frozen_ids() == {'C'}
    book.settle_fractions(d, {'P': 80, 'C': 100}, _tenth)
    assert book.security_receivables == [] and book.account.receivable_cash == pytest.approx(60)
    assert book.general_fraction_ids == {'E|C|fractional_cash'}
    assert book.fractional_rows[0]['fractional_cash_valuation'] == 'approximation'


def test_engine_splits_fraction_of_held_name_without_stopping(tmp_path, monkeypatch):
    # A 1.5-for-1 split on a held name leaves a half share. Under v2 the half share is cash at that close,
    # so the segment completes instead of stopping at the next weekly allocation.
    split = EventRow('SPLIT:S0:2019-12-03', 'S0', 'split', date(2019, 12, 3), 1.5, None, '', None, 'official')
    code, out = _bounded_run_fixture(tmp_path, monkeypatch, events=[split])
    assert code == 0
    recv = [r for r in _rows(out / 'receivables.csv') if r['operation'] == 'fractional_cash_receivable_created']
    assert len(recv) == 1 and recv[0]['security_id'] == 'S0' and recv[0]['session_date'] == '2019-12-03'
    assert float(recv[0]['shares']) == pytest.approx(.5)
    close = float(next(r for r in _rows(out / 'positions.csv') if r['session_date'] == '2019-12-03' and r['security_id'] == 'S0')['mark'])
    assert float(recv[0]['amount']) == pytest.approx(.5 * close)
    manifest = json.loads((out / 'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['accounting_rules_version'] == 'v2'


def test_window_gap_forces_exit_at_the_next_execution_and_keeps_out_of_exit_cap(tmp_path, monkeypatch):
    # S0 is held from the 12-02 execution. Its only missing session, 12-05, lies inside the 20-session window of the
    # 12-06 signal, so S0 is an unscorable held name that week: it leaves at the 12-09 open with its own reason.
    code, out = _bounded_run_fixture(tmp_path, monkeypatch, gaps=[('S0', date(2019, 12, 5))])
    assert code == 0
    decision = [r for r in _rows(out / 'decisions.csv') if r['signal_session_date'] == '2019-12-06' and r['security_id'] == 'S0']
    assert decision and decision[0]['action'] == 'exit' and decision[0]['reason'] == 'unscorable_price_gap_exit'
    sells = [r for r in _rows(out / 'trades.csv') if r['security_id'] == 'S0' and r['session_date'] == '2019-12-09']
    assert sells and all(r['side'] == 'sell' for r in sells)
    assert float(sells[-1]['shares_after']) == 0
    summary = json.loads((out / 'summary.json').read_text(encoding='utf-8'))
    assert summary['unscorable_price_gap_exits'] >= 1


def test_no_rebuy_while_the_scoring_window_still_contains_the_gap(tmp_path, monkeypatch):
    code, out = _bounded_run_fixture(tmp_path, monkeypatch, gaps=[('S0', date(2019, 12, 5))])
    assert code == 0
    buys = [r for r in _rows(out / 'trades.csv') if r['security_id'] == 'S0' and r['side'] == 'buy']
    assert all(r['signal_session_date'] < '2019-12-06' for r in buys)  # only the purchase before the gap
    decisions = [r for r in _rows(out / 'decisions.csv') if r['security_id'] == 'S0' and r['signal_session_date'] == '2019-12-13']
    assert all(r['action'] != 'fill' for r in decisions)
