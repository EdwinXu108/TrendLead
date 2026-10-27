"""Hand calculations for the TERRA_R1 accounting corrections."""
import pytest

from leadtrend_bt.engine import Backtest, Tranche
from leadtrend_bt.metrics import _annual, compute
from tests_bt.test_engine import base_cfg, market, three_series


def test_pnl_attributes_cash_flows_and_open_market_value_once():
    result = Backtest(base_cfg(), market=market(three_series())).run()
    # 475 A at 1.05, still held at 1.19; 486 B bought at 1.025 and
    # sold at 1.075; 417 C bought at 1.25 and still held at 1.45.
    # Each of the four transactions costs $1.
    assert result['pnl']['IA'] == pytest.approx(475 * (1.19 - 1.05) - 1)
    assert result['pnl']['IB'] == pytest.approx(486 * (1.075 - 1.025) - 2)
    assert result['pnl']['IC'] == pytest.approx(417 * (1.45 - 1.25) - 1)
    assert sum(result['pnl'].values()) == pytest.approx(result['daily'][-1]['equity'] - 1000)


def test_flat_prices_and_no_cost_have_zero_pnl():
    data = {'A': [10.0] * 20, 'BENCH:VOO': [1.0] * 20}
    result = Backtest(base_cfg(N=1, fee_per_order=0), market=market(data)).run()
    assert sum(result['pnl'].values()) == pytest.approx(0)
    assert result['daily'][-1]['equity'] == pytest.approx(1000)


def test_calendar_year_ends_at_last_close_not_next_year_first_close():
    dates = ['2022-12-30', '2023-01-03', '2023-12-29', '2024-01-02']
    values = [100, 110, 121, 108.9]
    assert _annual(dates, values) == pytest.approx({'2022': 0, '2023': .21, '2024': -.1})


def test_representatives_use_eligible_class_but_rank_by_total_company_amount():
    classes = {'A1': {'issuer_id': 'IA', 'amount_cents': 60},
               'A2': {'issuer_id': 'IA', 'amount_cents': 40},
               'B': {'issuer_id': 'IB', 'amount_cents': 80}}
    reps = Backtest._representatives(classes, {'IA': 100, 'IB': 80})
    assert [(r['security_id'], r['company_amount_cents']) for r in reps] == [('A1', 100), ('B', 80)]
    bt = Backtest(base_cfg(protocol='terra_r1_v1', mode='pool', top_n=1, weighting='cw'))
    bt.pools['2024-01-01'] = {'amounts': classes, 'company_amounts': {'IA': 100, 'IB': 80}}
    assert bt._pool_targets('2024-01-01') == {'A1': 1.0}


def test_size_and_momentum_share_complete_history_requirement():
    from datetime import date, timedelta
    days = [(date(2023, 1, 1) + timedelta(days=i)).isoformat() for i in range(95)]
    pool = {'securities': [{'security_id': 'A', 'issuer_id': 'IA', 'amount_cents': 20},
                           {'security_id': 'B', 'issuer_id': 'IB', 'amount_cents': 30}]}
    results = []
    for ranking in ('size', 'momentum'):
        bt = Backtest(base_cfg(protocol='terra_r1_v1', L=90, ranking=ranking))
        bt.full_calendar = days
        bt.issuer = {'A': 'IA', 'B': 'IB'}
        bt.tri = {'A': dict.fromkeys(days, 1.0), 'B': dict.fromkeys(days, 1.0)}
        del bt.tri['B'][days[50]]
        results.append([sid for sid, _ in bt._scored(days[-1], pool)])
    assert results == [['A'], ['A']]


def test_distribution_receives_child_and_is_not_spendable_at_open():
    bt = Backtest(base_cfg(protocol='terra_r1_v1', cost_bps=10))
    bt.tranches = [Tranche(0, 100, positions={'A': 2}, issuer_of={'A': 'IA'})]
    bt.actions = {'A': [dict(action_date='2024-01-08', action_type='class_distribution', ratio=1.9981,
                             child_security_id='C', child_ratio=1, cash_amount=None)]}
    bt.action_sids = ['A']
    bt.closes = {'C': {'2024-01-08': 40}}
    bt._apply_actions('2024-01-08')
    assert bt.tranches[0].positions['A'] == 2
    assert bt.tranches[0].cash == 100
    bt._liquidate_distributions('2024-01-08')
    assert bt.tranches[0].cash == pytest.approx(100 + 80 * .999 - 1)
    assert bt.event_flows[0]['cash_available'] == 'next_session_open'


def test_dividend_paid_only_to_ex_date_opening_owner():
    bt = Backtest(base_cfg(protocol='terra_r1_v1'))
    bt.tranches = [Tranche(0, 100)]
    bt.actions = {'A': [dict(action_date='2024-01-08', action_type='cash_dividend', ratio=None,
                             cash_amount=2)]}
    bt.action_sids = ['A']
    bt.issuer = {'A': 'IA'}
    bt.opens = {'A': {'2024-01-08': 10}}
    bt.pending = [dict(tranche=0, security_id='A', side='buy', reason='signal', signal_date='2024-01-05', weight=1)]
    bt._apply_actions('2024-01-08')
    bt._execute('2024-01-08')
    assert bt.tranches[0].income == {}
    assert bt.tranches[0].positions['A'] == 9
    assert bt.tranches[0].cash == 9


def test_split_converts_pending_partial_sale_quantity_and_previous_close():
    bt = Backtest(base_cfg(protocol='terra_r1_v1'))
    bt.tranches = [Tranche(0, 0, positions={'A': 10}, issuer_of={'A': 'IA'})]
    bt.actions = {'A': [dict(action_date='2024-01-08', action_type='split', ratio=2, cash_amount=None)]}
    bt.action_sids = ['A']
    bt.last_close = {'A': 100}
    bt.pending = [dict(tranche=0, security_id='A', side='sell', shares=3)]
    bt._apply_actions('2024-01-08')
    assert bt.tranches[0].positions['A'] == 20
    assert bt.pending[0]['shares'] == 6
    assert bt.last_close['A'] == 50


def test_delayed_sale_defers_new_position_until_slot_is_free():
    bt = Backtest(base_cfg(protocol='terra_r1_v1', N=1, fee_per_order=0))
    bt.tranches = [Tranche(0, 0, positions={'A': 10}, issuer_of={'A': 'IA'})]
    bt.issuer = {'A': 'IA', 'B': 'IB'}
    bt.opens = {'A': {'2024-01-09': 10}, 'B': {'2024-01-08': 10, '2024-01-09': 10}}
    bt.pending = [dict(tranche=0, security_id='A', side='sell', reason='signal', signal_date='2024-01-05'),
                  dict(tranche=0, security_id='B', side='buy', reason='signal', signal_date='2024-01-05', weight=1)]
    bt._execute('2024-01-08')
    assert bt.tranches[0].positions == {'A': 10}
    bt._execute('2024-01-09')
    assert bt.tranches[0].positions == {'B': 10}


def test_first_open_to_close_is_included_in_return_and_drawdown():
    result = dict(config={'protocol': 'terra_r1_v1', 'capital': 100},
                  daily=[dict(date='2014-03-03', cash=0, positions_value=80, equity=80, benchmark=90),
                         dict(date='2014-03-04', cash=0, positions_value=88, equity=88, benchmark=99)],
                  positions=[], trades=[], pnl={'IA': -12}, issuers={})
    summary = compute(result, {})
    assert summary['strategy']['total_return'] == pytest.approx(-.12)
    assert summary['strategy']['max_drawdown'] == pytest.approx(-.2)
    assert summary['strategy']['sessions'] == 2
    assert summary['benchmark']['total_return'] == pytest.approx(-.01)


def test_complete_synthetic_distribution_reconciles_without_early_spending():
    mk = market({'A': [10.0] * 20, 'BENCH:VOO': [1.0] * 20}, signal_idx=(4, 19))
    days = mk['calendar']
    for d in days[7:]:
        mk['opens']['A'][d] = mk['closes']['A'][d] = 5.0
    mk['closes']['C'] = {days[7]: 5.0}
    mk['actions'] = {'A': [dict(action_date=days[7], action_type='class_distribution', ratio=1.9981,
        cash_amount=None, child_security_id='C', child_ratio=1)]}
    cfg = base_cfg(protocol='terra_r1_v1', N=1, L=1)
    out = Backtest(cfg, market=mk).run()
    assert out['event_flows'][0]['net'] == pytest.approx(99 * 5 - 1)
    assert out['daily'][-1]['equity'] == pytest.approx(998)
    assert sum(out['pnl'].values()) == pytest.approx(-2)


def test_dowdupont_children_and_reverse_split_use_pre_split_entitlement():
    bt = Backtest(base_cfg(protocol='terra_r1_v1', start='2019-01-01', end='2019-12-31'))
    sid = 'CUSIP:26614N201'
    bt.tri = {sid: {'2019-04-01': 1, '2019-04-02': 1, '2019-06-03': 1}}
    bt.closes = {sid: {'2019-04-02': 36.49, '2019-06-03': 76.1},
                 'CUSIP:260557103': {'2019-04-02': 56.25},
                 'CUSIP:22052L104': {'2019-06-03': 24.81}}
    bt.actions = {sid: [dict(action_date='2019-04-01', action_type='split', ratio=1.02492),
                        dict(action_date='2019-04-02', action_type='split', ratio=1.48742),
                        dict(action_date='2019-06-03', action_type='reverse_split', ratio=1/3)]}
    bt._prepare_terra_event_explanation()
    assert [r['action_type'] for r in bt.actions[sid]] == ['spinoff', 'spinoff', 'reverse_split']
    assert bt.tri[sid]['2019-04-01'] == pytest.approx(1/1.02492)
    bt.tranches = [Tranche(0, 0, positions={sid: 9}, issuer_of={sid: 'I'})]
    bt.action_sids = [sid]
    bt.applied[sid] = 1
    bt._apply_actions('2019-06-03')
    assert bt.tranches[0].positions[sid] == pytest.approx(3)
    assert bt.pending_distributions[0]['shares'] == 9
    assert bt.pending_distributions[0]['per_share'] == pytest.approx(24.81/3)


def test_pool_open_uses_previous_close_when_open_is_missing():
    bt = Backtest(base_cfg(protocol='terra_r1_v1', mode='pool', fee_per_order=0))
    t = Tranche(0, 0, positions={'A': 10}, issuer_of={'A': 'IA'})
    bt.tranches = [t]
    bt.last_close = {'A': 10}
    bt.opens = {'B': {'2024-01-08': 10}}
    bt.closes = {'A': {'2024-01-08': 100}}
    bt._queue_target_values(t, '2024-01-08', {'A': .5, 'B': .5}, 'version')
    assert next(o for o in bt.pending if o['security_id'] == 'B')['notional'] == 50


def test_vernova_uses_verified_child_close_in_signal_and_ledger():
    bt = Backtest(base_cfg(protocol='terra_r1_v1'))
    sid = 'CUSIP:369604301'
    bt.tri = {sid: {'2024-04-01': 1, '2024-04-02': 1}}
    bt.closes = {sid: {'2024-04-01': 175.36, '2024-04-02': 136.47},
                 'CUSIP:36828A101': {'2024-04-02': 140}}
    bt.actions = {sid: [dict(action_date='2024-04-02', action_type='spinoff',
                             ratio=None, cash_amount=None, vendor_factor=.7975308)]}
    bt._prepare_terra_event_explanation()
    expected = (136.47 + 35) / (136.47 + (1-.7975308)*175.36)
    assert bt.tri[sid]['2024-04-02'] == pytest.approx(expected)
    bt.tranches = [Tranche(0, 0, positions={sid: 4}, issuer_of={sid: 'I'})]
    bt.action_sids = [sid]
    bt._apply_actions('2024-04-02')
    assert bt.pending_distributions[0]['per_share'] == 35


def test_gehc_archive_close_repairs_scoring_and_entitlement():
    bt = Backtest(base_cfg(protocol='terra_r1_v1', start='2022-01-01', end='2023-12-31'))
    sid = 'CUSIP:369604301'
    bt.tri = {sid: {'2023-01-03': 1, '2023-01-04': 1}}
    bt.closes = {sid: {'2023-01-03': 84.16, '2023-01-04': 66.75}}
    bt.actions = {sid: [dict(action_date='2023-01-04', action_type='spinoff',
                             ratio=None, cash_amount=None, vendor_factor=.7803407)]}
    bt._prepare_terra_event_explanation()
    expected = (66.75 + 60.49/3) / (66.75 + (1-.7803407)*84.16)
    assert bt.tri[sid]['2023-01-04'] == pytest.approx(expected)
    bt.tranches = [Tranche(0, 0, positions={sid: 3}, issuer_of={sid: 'I'})]
    bt.action_sids = [sid]
    bt._apply_actions('2023-01-04')
    assert bt.pending_distributions[0]['per_share'] == pytest.approx(60.49/3)
