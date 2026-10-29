"""Cross-sectional signals computed from the released database.

Every signal returns {signal_day: {security_id: score}} where a higher score ranks first.
Only data up to the signal day is used.  Registered in research/mom_v1/spec_m3.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from leadtrend_db import api

LAST = '2024-06-28'
START = '2014-02-28'
LOOKBACK_START = '2012-12-01'

SIGNALS = {
    'MOM_126': '过去 126 个交易日的总回报',
    'MOM_252': '过去 252 个交易日的总回报',
    'MOM_12_1': '过去 252 天但跳过最近 21 天',
    'REV_5': '过去 5 天涨幅取负（买跌得最多的）',
    'REV_21': '过去 21 天涨幅取负',
    'LOWVOL_126': '过去 126 天日收益标准差取负（买波动最低的）',
    'HIGHVOL_126': '过去 126 天日收益标准差（买波动最高的，对照组）',
    'DIST_52W': '收盘价 ÷ 过去 252 天最高收盘价',
    'TREND_MA200': '收盘价在 200 日均线上方才参与，按 126 天动量排',
    'VOLUME_SURGE': '近 20 日均量 ÷ 近 252 日均量',
    'AMIHUD_126': '过去 126 天平均(|日收益| ÷ 成交额)（越高越不流动）',
    'FUND_ACCUM': '本季基金持仓金额 ÷ 上季 − 1',
    'FUND_RANK_UP': '上季排名 − 本季排名（越大表示名次上升越多）',
}


def context(db_path=None, scope=None) -> dict:
    """Matrices and per-day pool membership for every signal."""
    days = api.trading_days(LOOKBACK_START, LAST, db_path) if db_path else api.trading_days(LOOKBACK_START, LAST)
    index = {d: i for i, d in enumerate(days)}
    signal_days = api.friday_signal_days(START, LAST, db_path) if db_path else api.friday_signal_days(START, LAST)
    pools: dict[str, dict[str, str]] = {}
    version_of: dict[str, str] = {}
    for day in signal_days:
        pool = api.pool_at(day, db_path, scope)
        amounts = api.pool_amounts(pool['version_id'], db_path, scope)
        best: dict[str, tuple[float, str]] = {}
        for sid, row in amounts.items():
            issuer = row['issuer_id']
            amount = float(row['amount_cents'])
            if issuer not in best or amount > best[issuer][0]:
                best[issuer] = (amount, sid)
        pools[day] = {sid: issuer for issuer, (_, sid) in best.items()}
        version_of[day] = pool['version_id']
    sids = sorted({sid for members in pools.values() for sid in members})
    col = {sid: j for j, sid in enumerate(sids)}
    shape = (len(days), len(sids))
    tri = np.full(shape, np.nan)
    close = np.full(shape, np.nan)
    volume = np.full(shape, np.nan)
    tri_raw = api.tri_series(sids, LOOKBACK_START, LAST, db_path) if db_path else api.tri_series(sids, LOOKBACK_START, LAST)
    for sid, rows in tri_raw.items():
        j = col[sid]
        for d, v, _ in rows:
            tri[index[d], j] = v
    closes = api.close_series(sids, LOOKBACK_START, LAST, db_path) if db_path else api.close_series(sids, LOOKBACK_START, LAST)
    for sid, rows in closes.items():
        j = col[sid]
        for d, v in rows.items():
            close[index[d], j] = v
    vols = api.volume_series(sids, LOOKBACK_START, LAST, db_path) if db_path else api.volume_series(sids, LOOKBACK_START, LAST)
    for sid, rows in vols.items():
        j = col[sid]
        for d, v in rows.items():
            volume[index[d], j] = v
    # Split-adjusted volume: only VOLUME_SURGE needs it. AMIHUD uses close x volume (dollar volume), which a split
    # does not change, so it keeps the raw series.
    vols_adj = (api.volume_series(sids, LOOKBACK_START, LAST, db_path, split_adjusted=True) if db_path
                else api.volume_series(sids, LOOKBACK_START, LAST, split_adjusted=True))
    volume_adj = np.full(shape, np.nan)
    for sid, rows in vols_adj.items():
        j = col[sid]
        for d, v in rows.items():
            volume_adj[index[d], j] = v
    ci = np.array([index[d] for d in signal_days])
    versions = api.version_amounts(db_path) if db_path else api.version_amounts()
    order = [v['version_id'] for v in versions]
    amounts = {v['version_id']: v['amounts'] for v in versions}
    ranks = {}
    for v in versions:
        ranked = sorted(v['amounts'].items(), key=lambda kv: (-kv[1], kv[0]))
        ranks[v['version_id']] = {issuer: i + 1 for i, (issuer, _) in enumerate(ranked)}
    return dict(days=days, signal_days=signal_days, sids=sids, col=col, pools=pools, version_of=version_of,
                tri=tri, close=close, volume=volume, volume_adj=volume_adj, ci=ci, versions=order,
                amounts=amounts, ranks=ranks)


def _ratio(numer, denom):
    with np.errstate(invalid='ignore', divide='ignore'):
        out = numer / denom - 1.0
    return np.where(np.isfinite(out), out, np.nan)


def _rolling_std(rets, ci, window):
    filled = np.where(np.isfinite(rets), rets, 0.0)
    c1 = np.cumsum(np.vstack([np.zeros((1, rets.shape[1])), filled]), axis=0)
    c2 = np.cumsum(np.vstack([np.zeros((1, rets.shape[1])), filled ** 2]), axis=0)
    base = ci + 1 - window
    ok = base >= 0
    base = np.maximum(base, 0)
    s1 = c1[ci + 1] - c1[base]
    s2 = c2[ci + 1] - c2[base]
    var = s2 / window - (s1 / window) ** 2
    out = np.where(var >= 0, np.sqrt(var), np.nan)
    return np.where(ok[:, None], out, np.nan)


def _rolling_mean(matrix, ci, window):
    filled = np.where(np.isfinite(matrix), matrix, np.nan)
    c1 = np.cumsum(np.vstack([np.zeros((1, matrix.shape[1])), np.nan_to_num(filled)]), axis=0)
    count = np.cumsum(np.vstack([np.zeros((1, matrix.shape[1])), np.isfinite(filled).astype(float)]), axis=0)
    base = ci + 1 - window
    ok = base >= 0
    base = np.maximum(base, 0)
    total = c1[ci + 1] - c1[base]
    n = count[ci + 1] - count[base]
    with np.errstate(invalid='ignore', divide='ignore'):
        out = np.where(n > window * 0.8, total / n, np.nan)
    return np.where(ok[:, None], out, np.nan)


def compute(name: str, ctx: dict) -> dict[str, dict[str, float]]:
    if name not in SIGNALS:
        raise ValueError(f'unknown signal: {name}')
    tri, close, volume, ci = ctx['tri'], ctx['close'], ctx['volume'], ctx['ci']
    rets = np.full_like(tri, np.nan)
    rets[1:] = tri[1:] / tri[:-1] - 1
    if name == 'MOM_126':
        values = _ratio(tri[ci], tri[ci - 126])
    elif name == 'MOM_252':
        values = _ratio(tri[ci], tri[ci - 252])
    elif name == 'MOM_12_1':
        values = _ratio(tri[ci - 21], tri[ci - 252])
    elif name == 'REV_5':
        values = -_ratio(tri[ci], tri[ci - 5])
    elif name == 'REV_21':
        values = -_ratio(tri[ci], tri[ci - 21])
    elif name == 'LOWVOL_126':
        values = -_rolling_std(rets, ci, 126)
    elif name == 'HIGHVOL_126':
        values = _rolling_std(rets, ci, 126)
    elif name == 'DIST_52W':
        peak = np.full((len(ci), close.shape[1]), np.nan)
        import warnings
        for k, i in enumerate(ci):
            window = close[max(0, i - 252):i + 1]
            with warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                peak[k] = np.nanmax(window, axis=0) if window.size else np.nan
        with np.errstate(invalid='ignore', divide='ignore'):
            values = np.where(peak > 0, close[ci] / peak, np.nan)
    elif name == 'TREND_MA200':
        ma = _rolling_mean(close, ci, 200)
        mom = _ratio(tri[ci], tri[ci - 126])
        values = np.where(close[ci] > ma, mom, np.nan)
    elif name == 'VOLUME_SURGE':
        adjusted = ctx.get('volume_adj', volume)
        short = _rolling_mean(adjusted, ci, 20)
        long = _rolling_mean(adjusted, ci, 252)
        with np.errstate(invalid='ignore', divide='ignore'):
            values = np.where(long > 0, short / long, np.nan)
    elif name == 'AMIHUD_126':
        dollar = close * volume
        with np.errstate(invalid='ignore', divide='ignore'):
            illiq = np.where((dollar > 0) & np.isfinite(rets), np.abs(rets) / dollar, np.nan)
        values = _rolling_mean(illiq, ci, 126)
    elif name in ('FUND_ACCUM', 'FUND_RANK_UP'):
        values = np.full((len(ci), tri.shape[1]), np.nan)
        order = ctx['versions']
        position = {v: i for i, v in enumerate(order)}
        for k, day in enumerate(ctx['signal_days']):
            version = ctx['version_of'][day]
            i = position[version]
            if i == 0:
                continue
            prev = order[i - 1]
            for sid, issuer in ctx['pools'][day].items():
                now = ctx['amounts'][version].get(issuer)
                before = ctx['amounts'][prev].get(issuer)
                if not now or not before:
                    continue
                j = ctx['col'][sid]
                if name == 'FUND_ACCUM':
                    values[k, j] = now / before - 1.0
                else:
                    r1 = ctx['ranks'][version].get(issuer)
                    r0 = ctx['ranks'][prev].get(issuer)
                    if r1 and r0:
                        values[k, j] = r0 - r1
    else:                                                     # pragma: no cover
        raise ValueError(name)
    out: dict[str, dict[str, float]] = {}
    for k, day in enumerate(ctx['signal_days']):
        row = {}
        for sid in ctx['pools'][day]:
            value = values[k, ctx['col'][sid]]
            if np.isfinite(value):
                row[sid] = float(value)
        out[day] = row
    return out


_CACHE: dict[tuple, dict] = {}


def scores(name: str, db_path=None, scope=None) -> dict[str, dict[str, float]]:
    """Cached per (database, signal) inside one process."""
    key = (api.database_identity(db_path), name, scope)
    if key not in _CACHE:
        _CACHE[key] = compute(name, context(db_path, scope))
    return _CACHE[key]
