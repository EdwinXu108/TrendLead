"""Metrics of one backtest run (summary.json). All numbers are computed from the daily equity series."""
from __future__ import annotations

import math

TRADING_DAYS = 252
PERIOD_A = ('2014-02-28', '2019-11-29')
PERIOD_B = ('2019-11-29', '2024-06-28')


def _returns(values: list[float]) -> list[float]:
    return [b / a - 1 for a, b in zip(values, values[1:]) if a]


def _cagr(values: list[float], sessions: int) -> float:
    if len(values) < 2 or values[0] <= 0 or sessions <= 0:
        return float('nan')
    return (values[-1] / values[0]) ** (TRADING_DAYS / sessions) - 1


def _vol(returns: list[float]) -> float:
    if len(returns) < 2:
        return float('nan')
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def _drawdown(dates: list[str], values: list[float]) -> dict:
    """Maximum drawdown with its peak, trough and recovery dates, and the longest run below the running peak."""
    peak, peak_date = values[0], dates[0]
    worst, worst_peak_date, trough_date = 0.0, dates[0], dates[0]
    longest = streak = 0
    for day, value in zip(dates, values):
        if value >= peak:
            peak, peak_date, streak = value, day, 0
        else:
            streak += 1
            longest = max(longest, streak)
        drawdown = value / peak - 1 if peak else 0.0
        if drawdown < worst:
            worst, trough_date, worst_peak_date = drawdown, day, peak_date
    peak_value = dict(zip(dates, values))[worst_peak_date]
    recovery = next((day for day, value in zip(dates, values) if day > trough_date and value >= peak_value), '')
    return dict(max_drawdown=worst, peak_date=worst_peak_date, trough_date=trough_date, recovery_date=recovery,
                longest_underwater_sessions=longest)


def _days(dates: list[str], start: str, end: str) -> int:
    return max(0, dates.index(end) - dates.index(start))


def _annual(dates: list[str], values: list[float]) -> dict:
    out: dict[str, float] = {}
    if not values or not values[0]:
        return out
    year_start = values[0]
    current = dates[0][:4]
    for day, value in zip(dates[1:], values[1:]):
        if day[:4] != current:
            out[current] = value / year_start - 1
            year_start = value
            current = day[:4]
    out[current] = values[-1] / year_start - 1
    return out


def _window(dates: list[str], values: list[float], start: str, end: str) -> tuple[list[str], list[float]]:
    idx = [i for i, d in enumerate(dates) if start <= d <= end]
    if not idx:
        return [], []
    return [dates[i] for i in idx], [values[i] for i in idx]


def series_metrics(dates: list[str], values: list[float], rf_annual: dict[str, float]) -> dict:
    returns = _returns(values)
    sessions = len(values) - 1
    cagr = _cagr(values, sessions)
    vol = _vol(returns)
    rf = [rf_annual[d] for d in dates[1:] if d in rf_annual]
    rf_avg = sum(rf) / len(rf) if rf else 0.0
    drawdown = _drawdown(dates, values)
    annual = _annual(dates, values)
    intra = {}
    for year in ('2020', '2022'):
        idx = [i for i, d in enumerate(dates) if d[:4] == year]
        if len(idx) > 1:
            sub = [values[i] for i in idx]
            intra[year] = _drawdown([dates[i] for i in idx], sub)['max_drawdown']
        else:
            intra[year] = float('nan')
    out = dict(start=dates[0], end=dates[-1], sessions=sessions, total_return=values[-1] / values[0] - 1,
               max_drawdown_2020=intra['2020'], max_drawdown_2022=intra['2022'],
               cagr=cagr, volatility=vol, sharpe=(cagr - rf_avg) / vol if vol else float('nan'),
               rf_annual_average=rf_avg, **drawdown, annual_returns=annual,
               worst_year=min(annual.items(), key=lambda kv: kv[1]) if annual else ('', float('nan')))
    out['worst_year'] = dict(year=out['worst_year'][0], return_=out['worst_year'][1])
    return out


def _beta(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 3:
        return float('nan')
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)
    var = sum((y - mb) ** 2 for y in b) / (n - 1)
    return cov / var if var else float('nan')


def compute(result: dict, rf_daily: dict[str, float], periods=(PERIOD_A, PERIOD_B)) -> dict:
    daily = [r for r in result['daily'] if r['benchmark'] > 0]      # from the benchmark entry session on
    dates = [r['date'] for r in daily]
    equity = [r['equity'] for r in daily]
    bench = [r['benchmark'] for r in daily]
    rf_annual = {d: rate * 360 for d, rate in rf_daily.items()}      # DTB3 percent, annualised
    strat = series_metrics(dates, equity, rf_annual)
    voo = series_metrics(dates, bench, rf_annual)
    sr, br = _returns(equity), _returns(bench)
    n = min(len(sr), len(br))
    diff = [a - b for a, b in zip(sr[-n:], br[-n:])]
    te = _vol(diff)
    weights = []
    for row in daily:
        weights.append(row['positions_value'] / row['equity'] if row['equity'] else 0.0)
    # Concentration is measured on the merged account: the market value of one security is summed over the tranches
    # of the same day first, then divided by that day's merged equity.
    equity_by_day = {row['date']: row['equity'] for row in daily}
    value_by_key: dict[tuple[str, str], float] = {}
    rows_by_day: dict[str, int] = {}
    for row in result['positions']:
        key = (row['date'], row['security_id'])
        value_by_key[key] = value_by_key.get(key, 0.0) + row['value']
        rows_by_day[row['date']] = rows_by_day.get(row['date'], 0) + 1
    distinct_by_day: dict[str, int] = {}
    for day, _ in value_by_key:
        distinct_by_day[day] = distinct_by_day.get(day, 0) + 1
    max_weight = 0.0
    for (day, _), value in value_by_key.items():
        eq = equity_by_day.get(day, 0.0)
        if eq:
            max_weight = max(max_weight, value / eq)
    issuers = result.get('issuers', {})
    issuer_by_day: dict[str, dict[str, float]] = {}
    for (day, sid), value in value_by_key.items():
        bucket = issuer_by_day.setdefault(day, {})
        issuer = issuers.get(sid, sid)
        bucket[issuer] = bucket.get(issuer, 0.0) + value
    max_top5 = 0.0
    for day, bucket in issuer_by_day.items():
        eq = equity_by_day.get(day, 0.0)
        if eq:
            max_top5 = max(max_top5, sum(sorted(bucket.values(), reverse=True)[:5]) / eq)
    positions_count = [distinct_by_day.get(d, 0) for d in dates]
    positions_sum = [rows_by_day.get(d, 0) for d in dates]
    traded = sum(abs(t['shares'] * t['price']) for t in result['trades'])
    years = len(dates) / TRADING_DAYS
    avg_equity = sum(equity) / len(equity)
    turnover = (traded / 2) / avg_equity / years if years and avg_equity else float('nan')
    pnl = result['pnl']
    total_pnl = sum(pnl.values()) or 1.0
    top = sorted(pnl.items(), key=lambda kv: -kv[1])[:5]
    periods_out = {}
    for name, (start, end) in zip(('A', 'B'), periods):
        d, e = _window(dates, equity, start, end)
        _, b = _window(dates, bench, start, end)
        if len(d) > 1:
            periods_out[name] = dict(start=d[0], end=d[-1],
                                     cagr=_cagr(e, len(e) - 1), voo_cagr=_cagr(b, len(b) - 1),
                                     excess=_cagr(e, len(e) - 1) - _cagr(b, len(b) - 1),
                                     total_return=e[-1] / e[0] - 1)
    return dict(
        strategy=strat, benchmark=voo,
        excess_cagr=strat['cagr'] - voo['cagr'],
        beta=_beta(sr, br), tracking_error=te,
        annual_excess={year: strat['annual_returns'][year] - voo['annual_returns'].get(year, 0.0)
                       for year in strat['annual_returns']},
        periods=periods_out,
        turnover_one_way_annual=turnover,
        average_positions=sum(positions_count) / len(positions_count),
        average_positions_sum=sum(positions_sum) / len(positions_sum),
        max_single_weight=max_weight, max_top5_issuer_weight=max_top5,
        average_invested_ratio=sum(weights) / len(weights),
        trades=len(result['trades']), fees=sum(t['fee'] for t in result['trades']),
        top_issuers=[dict(issuer=k, pnl=v, share=v / total_pnl) for k, v in top],
        total_pnl=sum(pnl.values()),
    )
