"""Signal-layer statistics for the 13 M3 signals: IC, Newey-West t and the top/bottom decile excess.

Run from the repository root:

    python -X utf8 research/mom_v1/signal_stats.py [--out FILE]

Uses only data up to 2024-06-28 (the window is fixed in leadtrend_bt/signals.py).
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, '.')
from leadtrend_bt import signals

HORIZONS = (4, 13)
DB = 'data/db/leadtrend_db_v1.sqlite'
OUT = 'work/mom_v1/m3_signal_stats.json'


def spearman(x, y):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            r = (i + j) / 2.0
            for k in range(i, j + 1):
                out[order[k]] = r
            i = j + 1
        return out
    rx, ry = ranks(x), ranks(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx * vy) ** 0.5


def nw(series, lag):
    n = len(series)
    if n < 3:
        return None
    mean = sum(series) / n
    e = [v - mean for v in series]
    var = sum(v * v for v in e) / n
    for l in range(1, min(lag, n - 1) + 1):
        cov = sum(e[i] * e[i - l] for i in range(l, n)) / n
        var += 2.0 * (1.0 - l / (lag + 1.0)) * cov
    if var <= 0:
        return None
    return mean / (var / n) ** 0.5


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=OUT)
    args = parser.parse_args()
    ctx = signals.context(DB)
    days = ctx['signal_days']
    ci = ctx['ci']
    n = len(days)
    tri = ctx['tri']
    out = {}
    for name in signals.SIGNALS:
        scores = signals.compute(name, ctx)
        record = {}
        for h in HORIZONS:
            ic, top, bot = [], [], []
            for k, day in enumerate(days):
                if k + h >= n:
                    continue
                end = ci[k + h]
                rows = []
                for sid, score in scores[day].items():
                    j = ctx['col'][sid]
                    v0, v1 = tri[ci[k], j], tri[end, j]
                    if np.isfinite(v0) and np.isfinite(v1) and v0 > 0:
                        rows.append((score, v1 / v0 - 1.0))
                if len(rows) < 20:
                    continue
                sig = [r[0] for r in rows]
                fut = [r[1] for r in rows]
                ic.append(spearman(sig, fut))
                order = sorted(range(len(rows)), key=lambda x: (sig[x], x))
                m = len(order)
                k10 = max(1, m // 10)
                top.append(np.mean([fut[i] for i in order[-k10:]]) - np.mean(fut))
                bot.append(np.mean([fut[i] for i in order[:k10]]) - np.mean(fut))
            ic = [v for v in ic if v is not None]
            record[h] = dict(ic=float(np.mean(ic)) if ic else None,
                             t=nw(ic, h) if ic else None,
                             top=float(np.mean(top)) * 52.0 / h if top else None,
                             bottom=float(np.mean(bot)) * 52.0 / h if bot else None,
                             days=len(ic))
        out[name] = record
        print(name, {h: (round(v['ic'], 4) if v['ic'] is not None else None) for h, v in record.items()}, flush=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1), encoding='utf8')


if __name__ == '__main__':
    main()
