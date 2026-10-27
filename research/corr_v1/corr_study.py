"""Correlation of the pool's largest holdings at every quarterly pool version (research/corr_v1/spec.md).

Reads only tri_daily daily total returns. Measures; never trades. Output goes to work/corr_v1/.
"""
import csv
import json
import sys
import warnings
from pathlib import Path

import numpy as np

sys.path.insert(0, '.')
from leadtrend_db import api

WINDOW = 252
MIN_COMMON = 200
RANGES = (('top50', 50), ('top100', 100), ('all', None))
OUT = Path('work/corr_v1')
DEV_END = '2024-06-28'


def load_members():
    """{version_id: [(amount_cents, security_id, ticker, issuer_id), ...] sorted by amount desc}.

    One security per issuer (the class with the largest holding), so a company with two share classes is
    counted once and its two tickers cannot enter as a 0.99-correlated pair.
    """
    versions = api.pool_windows('2014-01-01', DEV_END)
    out = {}
    for v in versions:
        pool = api.pool_at(v['effective_from'])
        if pool['version_id'] != v['version_id']:
            raise RuntimeError(f'pool_at mismatch on {v["effective_from"]}')
        ticker = {s['security_id']: s['current_ticker'] for s in pool['securities']}
        amounts = api.pool_amounts(v['version_id'])
        best = {}
        for sid, a in amounts.items():
            if sid not in ticker:
                continue
            row = (a['amount_cents'], sid, ticker[sid], a['issuer_id'])
            if a['issuer_id'] not in best or row[0] > best[a['issuer_id']][0]:
                best[a['issuer_id']] = row
        rows = sorted(best.values(), key=lambda r: (-r[0], r[1]))
        out[v['version_id']] = dict(version=v, rows=rows, dropped_classes=len(amounts) - len(rows))
    return out


def load_matrix(ids):
    """Dense (sessions x securities) daily total-return matrix over the whole development segment."""
    calendar = api.trading_days('2013-01-01', DEV_END)
    index = {d: i for i, d in enumerate(calendar)}
    col = {sid: j for j, sid in enumerate(ids)}
    mat = np.full((len(calendar), len(ids)), np.nan)
    series = api.tri_series(ids, calendar[0], calendar[-1])
    for sid, rows in series.items():
        j = col[sid]
        for day, _tri, ret in rows:
            if ret is not None:
                mat[index[day], j] = ret
    return calendar, index, col, mat


def pairwise_corr(mat):
    """Pearson correlation with pairwise-complete observations; a zero-variance pair is reported as 0."""
    valid = np.isfinite(mat)
    filled = np.where(valid, mat, 0.0)
    weight = valid.astype(float)
    n = weight.T @ weight
    s1 = weight.T @ filled
    s2 = weight.T @ (filled * filled)
    sxy = filled.T @ filled
    with np.errstate(invalid='ignore', divide='ignore'):
        mx = s1 / n
        my = s1.T / n
        vx = s2 / n - mx * mx
        vy = s2.T / n - my * my
        cov = sxy / n - mx * my
        rho = cov / np.sqrt(np.maximum(vx, 0.0) * np.maximum(vy, 0.0))
    rho = np.where(np.isfinite(rho), rho, 0.0)
    rho = np.clip(rho, -1.0, 1.0)
    np.fill_diagonal(rho, 1.0)
    return rho, n


def nw_trend(y, lag=4):
    """OLS slope of y on time with Newey-West standard errors; returns (slope per node, t)."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n < 5:
        return 0.0, 0.0
    x = np.arange(n, dtype=float)
    X = np.column_stack([np.ones(n), x])
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    resid = y - X @ beta
    u = X * resid[:, None]
    S = u.T @ u
    for l in range(1, min(lag, n - 1) + 1):
        gamma = u[l:].T @ u[:-l]
        S += (1.0 - l / (lag + 1.0)) * (gamma + gamma.T)
    bread = np.linalg.inv(X.T @ X)
    cov = bread @ S @ bread
    se = float(np.sqrt(max(cov[1, 1], 0.0)))
    return float(beta[1]), (float(beta[1] / se) if se > 0 else 0.0)


def stats_block(rho, n_common, labels):
    k = rho.shape[0]
    iu = np.triu_indices(k, 1)
    off = rho[iu]
    eig = np.linalg.eigvalsh(rho)
    eig = np.clip(eig, 0.0, None)
    neff_eig = float(eig.sum() ** 2 / (eig ** 2).sum()) if (eig ** 2).sum() > 0 else float(k)
    mean = float(off.mean())
    neff_arith = k / (1.0 + (k - 1) * mean) if k > 1 else 1.0
    top = int(np.argmax(off))
    return dict(n_sec=k, n_pairs=int(len(off)), mean_rho=mean, median_rho=float(np.median(off)),
                std_rho=float(off.std(ddof=1)), p05=float(np.percentile(off, 5)),
                p25=float(np.percentile(off, 25)), p75=float(np.percentile(off, 75)),
                p95=float(np.percentile(off, 95)), share_gt_05=float((off > 0.5).mean()),
                share_gt_07=float((off > 0.7).mean()), share_lt_0=float((off < 0).mean()),
                max_rho=float(off[top]), max_pair=f'{labels[iu[0][top]]}/{labels[iu[1][top]]}',
                n_eff_arith=float(neff_arith), n_eff_eig=neff_eig,
                avg_common_days=float(n_common[iu].mean()))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    members = load_members()
    all_ids = sorted({r[1] for m in members.values() for r in m['rows']})
    calendar, index, col, mat = load_matrix(all_ids)
    print(f'{len(members)} versions, {len(all_ids)} securities, {len(calendar)} sessions', flush=True)

    node_rows, pair_rows = [], []
    pair_acc = {}
    for vi, (vid, item) in enumerate(sorted(members.items(), key=lambda kv: kv[1]['version']['effective_from']), 1):
        v = item['version']
        day = v['effective_from']
        i = index[day]
        if i + 1 < WINDOW:
            print(f'skip {day}: only {i + 1} sessions')
            continue
        window = mat[i - WINDOW + 1:i + 1]
        for rname, cap in RANGES:
            picked = item['rows'] if cap is None else item['rows'][:cap]
            ids = [r[1] for r in picked]
            labels = [r[2] for r in picked]
            sub = window[:, [col[s] for s in ids]]
            observed = np.isfinite(sub).sum(axis=0)
            keep = observed >= MIN_COMMON
            if keep.sum() < 3:
                print(f'{day} {rname}: only {int(keep.sum())} securities with {MIN_COMMON} days')
                continue
            sub = sub[:, keep]
            labels = [t for t, k in zip(labels, keep) if k]
            rho, n_common = pairwise_corr(sub)
            iu = np.triu_indices(rho.shape[0], 1)
            off = rho[iu]
            row = dict(node=vi, date=day, version_id=vid, report_period=v['report_period'],
                       range=rname, window_days=int(sub.shape[0]), dropped=int((~keep).sum()))
            row.update(stats_block(rho, n_common, labels))
            # demeaned: remove the equal-weight average of this range on each day
            with np.errstate(invalid='ignore'), warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)
                center = np.nanmean(sub, axis=1)
            resid = np.where(np.isfinite(sub), sub - center[:, None], np.nan)
            rho_d, _ = pairwise_corr(resid)
            iu = np.triu_indices(rho_d.shape[0], 1)
            offd = rho_d[iu]
            row['de_mean_rho'] = float(offd.mean())
            row['de_std_rho'] = float(offd.std(ddof=1))
            row['de_share_gt_05'] = float((offd > 0.5).mean())
            node_rows.append(row)
            order = np.argsort(-off)[:30]
            for rank, p in enumerate(order, 1):
                pair_rows.append(dict(date=day, range=rname, rank=rank,
                                      ticker_a=labels[iu[0][p]], ticker_b=labels[iu[1][p]],
                                      rho=round(float(off[p]), 6)))
            if cap is not None:
                for p in range(len(off)):
                    key = (rname, tuple(sorted((labels[iu[0][p]], labels[iu[1][p]]))))
                    pair_acc.setdefault(key, []).append((day, float(off[p])))
        print(f'{day} {v["version_id"]}', flush=True)

    write_csv(OUT / 'node_stats.csv', node_rows)
    write_csv(OUT / 'pairs_top.csv', pair_rows)

    # structural pairs: every pair of the top50 / top100 ranges, averaged over the nodes where both appear
    mean_rows = []
    for (rname, (a, b)), vals in pair_acc.items():
        if len(vals) < 10:
            continue
        rhos = [v for _, v in vals]
        mean_rows.append(dict(range=rname, ticker_a=a, ticker_b=b, nodes=len(vals),
                              mean_rho=round(float(np.mean(rhos)), 4),
                              min_rho=round(float(np.min(rhos)), 4), max_rho=round(float(np.max(rhos)), 4),
                              first=min(d for d, _ in vals), last=max(d for d, _ in vals)))
    mean_rows.sort(key=lambda r: (r['range'], -r['mean_rho']))
    write_csv(OUT / 'pair_mean.csv', mean_rows)

    summary = {}
    for rname, _ in RANGES:
        rows = [r for r in node_rows if r['range'] == rname]
        if not rows:
            continue
        series = np.array([r['mean_rho'] for r in rows])
        slope, tstat = nw_trend(series, 4)
        years = {}
        for r in rows:
            years.setdefault(r['date'][:4], []).append(r['mean_rho'])
        summary[rname] = dict(nodes=len(rows), mean=float(series.mean()), sd=float(series.std(ddof=1)),
                              min=float(series.min()), max=float(series.max()),
                              q25=float(np.percentile(series, 25)), q75=float(np.percentile(series, 75)),
                              slope_per_year=float(slope * 4), nw_t=float(tstat),
                              mean_neff_arith=float(np.mean([r['n_eff_arith'] for r in rows])),
                              mean_neff_eig=float(np.mean([r['n_eff_eig'] for r in rows])),
                              by_year={y: round(float(np.mean(v)), 3) for y, v in sorted(years.items())},
                              highest=[dict(date=r['date'], mean_rho=r['mean_rho'])
                                       for r in sorted(rows, key=lambda r: -r['mean_rho'])[:5]],
                              lowest=[dict(date=r['date'], mean_rho=r['mean_rho'])
                                      for r in sorted(rows, key=lambda r: r['mean_rho'])[:5]])
    if 'top50' in summary and 'top100' in summary:
        a = {r['date']: r['mean_rho'] for r in node_rows if r['range'] == 'top50'}
        b = {r['date']: r['mean_rho'] for r in node_rows if r['range'] == 'top100'}
        diff = np.array([a[d] - b[d] for d in sorted(set(a) & set(b))])
        summary['top50_minus_top100'] = dict(mean=float(diff.mean()), sd=float(diff.std(ddof=1)),
                                             t=float(diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))),
                                             positive=float((diff > 0).mean()), n=int(len(diff)))
    (OUT / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf8')
    print(json.dumps(summary, indent=2))


def write_csv(path, rows):
    if not rows:
        path.write_text('', encoding='utf8')
        return
    with path.open('w', encoding='utf8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


if __name__ == '__main__':
    main()
