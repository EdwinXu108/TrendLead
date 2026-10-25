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
