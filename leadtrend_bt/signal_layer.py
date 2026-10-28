"""The investor's signal-layer protocol as code (AGENTS.md, 2026-10-28).

Signal layer is the default protocol for signal research from 2026-10-28: the Friday close produces the score and
the same close fills the order, zero cost, fractional shares, absolute equal weight, no buffer (b=1), no tranching
(K=1), 2014-02-28 to 2024-06-28 as one segment, numbers only. It is meant to answer "is there a signal" quickly and
is explicitly allowed to be loose.

The backtest layer (costs, execution timing, buffer, tranching, sub-periods) is not defined yet, so the defaults in
`research_v1` do not change: they stay `next_open`, with cost. Nothing here alters an engine default.
"""
from __future__ import annotations

LAST_ALLOWED_END = '2024-06-28'

# Fixed parts of the protocol. A caller supplies ranking / signal / L / N (and, for the archived M2 grid, the
# equal-weight mode); everything below is the protocol itself and is not a tunable parameter.
SIGNAL_LAYER = dict(
    mode='momentum',
    b=1,
    K=1,
    rebalance='R1W',
    fractional=True,
    cost_bps=0,
    fee_per_order=0.0,
    execution='close',
    start='2014-02-28',
    end='2024-06-28',
    capital=100000.0,
    protocol='research_v1',
)

FIXED = tuple(SIGNAL_LAYER)
# `rebalance` is the one protocol field a caller may vary: the archived M2 grid registered both R1 and R1W to
# measure what restoring equal weight every week does. Everything else in SIGNAL_LAYER is the protocol itself.
OVERRIDABLE = ('ranking', 'signal', 'L', 'N', 'rebalance', 'top_m', 'w', 'buy_weighting')


def signal_layer_config(run_id: str, **overrides) -> dict:
    """A complete run configuration in the signal-layer protocol.

    `run_id` names the run; `overrides` may set ranking, signal, L, N, rebalance, top_m, w and buy_weighting.
    The rest of `SIGNAL_LAYER` is fixed: passing it raises, because changing it means leaving the signal layer.
    """
    bad = sorted(set(overrides) & (set(FIXED) - set(OVERRIDABLE)))
    if bad:
        raise ValueError(f'not overridable in the signal layer: {", ".join(bad)}')
    cfg = dict(SIGNAL_LAYER)
    cfg.update(overrides)
    cfg['run_id'] = run_id
    if cfg['protocol'] != 'research_v1':
        raise ValueError('signal layer runs under research_v1')
    if cfg['end'] > LAST_ALLOWED_END:
        raise ValueError(f'end {cfg["end"]} is in the reserved segment')
    if cfg['start'] >= cfg['end']:
        raise ValueError('start must be before end')
    return cfg
