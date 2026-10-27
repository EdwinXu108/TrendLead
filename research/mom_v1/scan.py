"""Signal-layer scan: momentum lookback x forward horizon x range x definition.

Only measures; no trading, no costs. Registered in research/mom_v1/spec.md.
Panels (range x definition) run in parallel.
"""
import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np

sys.path.insert(0, '.')
from leadtrend_bt import signal_study as ss

LOOKBACKS = sorted(set(range(5, 251, 5)) | {21, 63, 126, 189, 252})
HORIZONS = list(range(1, 27)) + [39, 52]
RANGES = (('all', None), ('top100', 100), ('top50', 50))
DEFINITIONS = ('raw', 'skip1m', 'riskadj')


def build_arrays():
    panel = ss.build_panel()
    days = panel['calendar']
    day_index = {d: i for i, d in enumerate(days)}
    signal_days = panel['signal_days']
    secs = sorted({sid for u in panel['universe'].values() for sid in u})
    col = {sid: j for j, sid in enumerate(secs)}
    tri = np.full((len(days), len(secs)), np.nan)
    for sid, series in panel['tri'].items():
        j = col[sid]
        for d, v in series.items():
            tri[day_index[d], j] = v
    masks = {}
    for name, cap in RANGES:
        m = np.zeros((len(signal_days), len(secs)), dtype=bool)
        for i, day in enumerate(signal_days):
            items = sorted(panel['universe'][day].items(), key=lambda kv: (-kv[1], kv[0]))
            for sid, _ in (items if cap is None else items[:cap]):
                m[i, col[sid]] = True
        masks[name] = m
    return dict(days=days, signal_days=signal_days, secs=secs, tri=tri, masks=masks,
                ci=np.array([day_index[d] for d in signal_days]))


def row_ranks(values, valid):
    filled = np.where(valid, values, np.inf)
    order = np.argsort(filled, axis=1)
    ranks = np.empty(order.shape, dtype=float)
    np.put_along_axis(ranks, order, np.broadcast_to(np.arange(order.shape[1], dtype=float), order.shape), axis=1)
    return np.where(valid, ranks, 0.0), order


def nw_t(x, lag):
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return 0.0
    mu = x.mean()
    e = x - mu
    var = float(e @ e) / n
    for l in range(1, min(lag, n - 1) + 1):
        var += 2.0 * (1.0 - l / (lag + 1.0)) * float(e[l:] @ e[:-l]) / n
    if var <= 0:
        return 0.0
    return float(mu / np.sqrt(var / n))


def row_ic(sig, fwd, mask):
    valid = mask & np.isfinite(sig) & np.isfinite(fwd)
    count = valid.sum(1)
    ra, order = row_ranks(sig, valid)
    rb, _ = row_ranks(fwd, valid)
    ma = ra.sum(1) / np.maximum(count, 1)
    mb = rb.sum(1) / np.maximum(count, 1)
    cov = (((ra - ma[:, None]) * (rb - mb[:, None])) * valid).sum(1) / np.maximum(count, 1)
    va = (((ra - ma[:, None]) ** 2) * valid).sum(1) / np.maximum(count, 1)
    vb = (((rb - mb[:, None]) ** 2) * valid).sum(1) / np.maximum(count, 1)
    with np.errstate(invalid='ignore', divide='ignore'):
        ic = cov / np.sqrt(va * vb)
    ic[count < 20] = np.nan
    return ic, valid, order, count


def decile_excess(sig_order, count, fwd, valid):
    n, m = fwd.shape
    k = np.maximum(1, count // 10)
    pos = np.arange(m)[None, :]
    top = np.zeros((n, m), dtype=bool)
    bottom = np.zeros((n, m), dtype=bool)
    np.put_along_axis(top, sig_order, (pos >= (count - k)[:, None]) & (pos < count[:, None]), axis=1)
    np.put_along_axis(bottom, sig_order, pos < k[:, None], axis=1)
    top &= valid
    bottom &= valid
    f0 = np.where(valid, fwd, 0.0)
    mean = f0.sum(1) / np.maximum(valid.sum(1), 1)
    top_mean = (f0 * top).sum(1) / np.maximum(top.sum(1), 1)
    bottom_mean = (f0 * bottom).sum(1) / np.maximum(bottom.sum(1), 1)
    return top_mean - mean, bottom_mean - mean, top


def panel_rows(job):
    rname, definition, tri, ci, mask = job
    n = len(ci)
    rets = np.full_like(tri, np.nan)
    rets[1:] = tri[1:] / tri[:-1] - 1
    rets = np.where(np.isfinite(rets), rets, 0.0)
    c1 = np.cumsum(np.vstack([np.zeros((1, tri.shape[1])), rets]), axis=0)
    c2 = np.cumsum(np.vstack([np.zeros((1, tri.shape[1])), rets ** 2]), axis=0)
    rows = []
    for L in LOOKBACKS:
        base = ci - L
        if (base < 0).any():
            continue
        with np.errstate(invalid='ignore', divide='ignore'):
            raw = tri[ci] / tri[base] - 1
        if definition == 'raw':
            sig = raw
        elif definition == 'skip1m':
            if L <= 21:
                continue
            with np.errstate(invalid='ignore', divide='ignore'):
                sig = tri[ci - 21] / tri[base] - 1
        else:
            s1 = c1[ci] - c1[base]
            s2 = c2[ci] - c2[base]
            var = s2 / L - (s1 / L) ** 2
            var = np.where(var > 0, var, np.nan)
            with np.errstate(invalid='ignore', divide='ignore'):
                sig = raw / np.sqrt(var)
        for h in HORIZONS:
            if h >= n:
                continue
            fi = np.full(n, -1)
            fi[:n - h] = ci[h:]
            ok = fi >= 0
            fwd = np.full((n, tri.shape[1]), np.nan)
            with np.errstate(invalid='ignore', divide='ignore'):
                fwd[ok] = tri[fi[ok]] / tri[ci[ok]] - 1
            ic, valid, order, count = row_ic(sig, fwd, mask)
            values = ic[np.isfinite(ic)]
            if len(values) < 30:
                continue
            top_x, bot_x, top = decile_excess(order, count, fwd, valid)
            sizes = top.sum(1)
            overlap = (top[1:] & top[:-1]).sum(1)
            with np.errstate(invalid='ignore', divide='ignore'):
                turnover = float(np.nanmean(1 - overlap / np.maximum(sizes[1:], 1)))
            rows.append(dict(range=rname, definition=definition, L=L, h=h,
                             ic=float(values.mean()), t=nw_t(values, h),
                             positive=float((values > 0).mean()),
                             top_excess=float(np.nanmean(top_x) * 52.0 / h),
                             bottom_excess=float(np.nanmean(bot_x) * 52.0 / h),
                             turnover=turnover, days=int(len(values))))
    print(f'{rname} {definition}: {len(rows)} rows', flush=True)
    return rows


def main():
    started = time.time()
    data = build_arrays()
    print(f'data ready {time.time() - started:.0f}s', flush=True)
    jobs = [(rname, definition, data['tri'], data['ci'], mask)
            for rname, mask in data['masks'].items() for definition in DEFINITIONS]
    with Pool(min(9, os.cpu_count() or 4)) as pool:
        results = pool.map(panel_rows, jobs)
    rows = [row for block in results for row in block]
    Path('work/mom_v1/scan_rows.json').write_text(json.dumps(rows), encoding='utf8')
    print('rows', len(rows), 'elapsed', round(time.time() - started, 1))


if __name__ == '__main__':
    main()
