"""The signal-layer protocol as code, and that it reproduces the archived M2/M3 configurations."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from leadtrend_bt.signal_layer import SIGNAL_LAYER, signal_layer_config

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'research' / 'mom_v1'))
import run_m2                                          # noqa: E402
import run_m3                                          # noqa: E402


def test_defaults_are_the_investor_protocol():
    assert SIGNAL_LAYER == dict(mode='momentum', b=1, K=1, rebalance='R1W', fractional=True, cost_bps=0,
                                fee_per_order=0.0, execution='close', start='2014-02-28', end='2024-06-28',
                                capital=100000.0, protocol='research_v1')


def test_m3_config_matches_the_archived_run_exactly():
    expected = dict(run_id='M3_REV_5_N10', mode='momentum', ranking='signal', signal='REV_5', L=90, N=10, b=1,
                    K=1, rebalance='R1W', fractional=True, cost_bps=0, fee_per_order=0.0, execution='close',
                    start='2014-02-28', end='2024-06-28', capital=100000.0, protocol='research_v1')
    assert run_m3.config_for('REV_5', 10) == expected


def test_m2_config_matches_the_archived_run_exactly():
    expected = dict(run_id='M2_R1W_L090_N10', mode='momentum', ranking='momentum', L=90, N=10, b=1, K=1,
                    rebalance='R1W', fractional=True, cost_bps=0, fee_per_order=0.0, execution='close',
                    start='2014-02-28', end='2024-06-28', capital=100000.0, protocol='research_v1')
    assert run_m2.config_for(90, 10, 'R1W') == expected


def test_task_lists_have_the_registered_size():
    assert len(run_m3.tasks()) == 78
    assert len(run_m2.tasks()) == 660


def test_protocol_fields_cannot_be_overridden():
    for field in ('b', 'K', 'cost_bps', 'execution', 'end', 'fractional', 'fee_per_order', 'capital', 'protocol'):
        with pytest.raises(ValueError):
            signal_layer_config('x', **{field: SIGNAL_LAYER[field] if field == 'b' else 2})


def test_rebalance_is_overridable_because_the_m2_grid_registered_both_modes():
    assert signal_layer_config('x', rebalance='R1')['rebalance'] == 'R1'
    assert signal_layer_config('x')['rebalance'] == 'R1W'


def test_reserved_segment_is_rejected():
    with pytest.raises(ValueError):
        signal_layer_config('x', end='2024-07-05')
