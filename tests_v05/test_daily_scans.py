"""Quadratic daily/weekly scans: incremental structures must equal the full scans."""
from collections import defaultdict
from datetime import date

from leadtrend_v05.ledger import AccountState, confirm_dividend
from leadtrend_v05.stage3_ledger import DevBook
from leadtrend_v05.dataio import EventRow


def dividend(event_id, sid, ex, pay, amount=0.5):
    return EventRow(event_id, sid, 'cash_dividend', ex, None, amount, 'per_share_at_entitlement', pay, 'test')


def spinoff(event_id, sid, effective, per_old=0.25):
    return EventRow(event_id, sid, 'spinoff', effective, None, None, '', None, 'test',
                    spinoff_value_per_old_share=1.0, spinoff_valuation='actual')


def test_payment_tail_equals_full_scan_for_every_day():
    book = DevBook(AccountState(1000.0, {'P': 100}))
    days = [date(2020, 1, 2), date(2020, 1, 3), date(2020, 1, 6), date(2020, 1, 7), date(2020, 1, 8)]
    confirm_dividend(book.account, 'P', 'D1', 0.5, date(2020, 1, 2), date(2020, 1, 6), 100)
    confirm_dividend(book.account, 'P', 'D2', 0.25, date(2020, 1, 3), date(2020, 1, 7), 100)
    confirm_dividend(book.account, 'Q', 'D3', 1.0, date(2020, 1, 6), date(2020, 1, 8), 0)
    for d in days:
        before = len(book.cash_flows)
        book.pay(d)
        iso = d.isoformat()
        # old: scan every historical flow for today's date
        old = defaultdict(float)
        for f in book.cash_flows:
            if f['session_date'] == iso:
                old[f['security_id']] += f['cash_delta']
        # new: only the flows appended by today's pay
        new = book.payments_on(d, before)
        assert new == old
        # stronger invariant: no pre-tail flow carries today's date
        assert all(f['session_date'] != iso for f in book.cash_flows[:before])


def test_spinoff_structures_equal_the_event_row_full_scan():
    book = DevBook(AccountState(1000.0, {'P': 100, 'R': 40}))
    children = {'E1': [dict(security_id='C1', shares_per_old='0.25', source='test')],
                'E2': [dict(security_id='C2', shares_per_old='0.5', source='test'),
                       dict(security_id='C3', shares_per_old='0.1', source='test')]}
    sec = {'P': None, 'R': None, 'C1': None, 'C2': None, 'C3': None}
    marks = {'P': 10.0, 'R': 5.0, 'C1': 4.0, 'C2': 2.0, 'C3': 1.0}
    book.spinoff(spinoff('E1', 'P', date(2020, 2, 3)), children['E1'], sec, marks, entitled_shares=100)
    book.spinoff(spinoff('E2', 'R', date(2020, 3, 2)), children['E2'], sec, marks, entitled_shares=40)
    reference_rows = [r for r in book.event_rows if r['event_type'] == 'spinoff']
    assert book.spinoff_rows == reference_rows
    assert book.spinoff_children == {r['child_security_id'] for r in reference_rows}
    for signal in ('2020-01-01', '2020-02-03', '2020-02-04', '2020-03-02', '2020-03-03', '2024-01-01'):
        expected = {r['child_security_id'] for r in reference_rows if r['session_date'] > signal}
        assert book.children_after_signal(signal) == expected
        for sid in ('P', 'R', 'C1', 'C2', 'C3', 'ZZZ'):
            assert (sid in book.spinoff_children) == any(
                r.get('child_security_id') == sid for r in reference_rows)
