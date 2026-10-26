"""End-to-end tests on the built database: repeatability, invariants and the reserved-segment guard."""
from __future__ import annotations

import filecmp
import json
from pathlib import Path

import pytest

from leadtrend_bt.engine import Backtest, HoldoutError
from leadtrend_bt.runner import run
from leadtrend_db.sources import DB_PATH

pytestmark = pytest.mark.skipif(not DB_PATH.exists(), reason='database not built')

CFG = dict(run_id='test_repeat', mode='momentum', start='2023-01-03', end='2024-06-28', L=90, N=10, b=2, K=1,
           cost_bps=10, fee_per_order=1.0, capital=100000.0)


def test_run_twice_produces_identical_files(tmp_path):
    first = run(dict(CFG), out_root=tmp_path / 'a', write=True)
    second = run(dict(CFG), out_root=tmp_path / 'b', write=True)
    names = ['config.json', 'daily_equity.csv', 'trades.csv', 'positions.csv', 'weekly_decisions.csv', 'summary.json']
    for name in names:
        a, b = first['path'] / name, second['path'] / name
        assert a.read_bytes() == b.read_bytes(), f'{name} differs between two runs'
    assert first['summary']['strategy']['cagr'] == pytest.approx(second['summary']['strategy']['cagr'])


def test_invariants_hold_on_real_data():
    out = run(dict(CFG), write=False)
    daily = out['result']['daily']
    for row in daily:
        assert row['cash'] >= -1e-6
        assert row['equity'] == pytest.approx(row['cash'] + row['positions_value'], abs=1e-6)
    assert len(daily) > 300
    assert out['summary']['strategy']['total_return'] > -1


def test_reserved_segment_is_refused_on_real_data():
    with pytest.raises(HoldoutError):
        Backtest(dict(CFG, end='2024-07-05'))


def test_benchmark_leg_matches_the_tri_ratio_on_real_data():
    out = run(dict(CFG), write=False)
    bt = out['engine']
    rows = [r for r in out['result']['daily'] if r['benchmark']]
    tri = bt.tri['BENCH:VOO']
    entry = rows[0]
    for row in rows[::50]:
        assert row['benchmark'] / entry['benchmark'] == pytest.approx(
            tri[row['date']] / tri[entry['date']], rel=1e-12)
