"""Single-stock moving-average lab: one security, one window, many averages.

For a security's total-return series (2012-12-03 to the window end) and a grid of average types and lengths,
the lab runs a weekly long/flat rule and compares it with buying and holding the same security over the same
evaluation window. Registered in research/ma_lab_v1/spec.md.

    python -X utf8 -m leadtrend_bt.ma_lab --security MSFT --start 2014-02-28 --end 2024-06-28 \
        --types sma,ema --fast 5,10 --slow 50,200 --cash zero --out work/ma_lab/MSFT
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

import numpy as np

from leadtrend_db import api

FIRST_DAY = '2012-12-03'
LAST_ALLOWED_END = '2024-06-28'
TRADING_DAYS = 252


# -- averages -----------------------------------------------------------------------------------------------------

def carry_forward(values: np.ndarray) -> np.ndarray:
    """A missing session keeps the previous value; leading missing sessions stay NaN."""
    values = np.asarray(values, dtype=float)
    index = np.where(np.isfinite(values), np.arange(len(values)), -1)
    np.maximum.accumulate(index, out=index)
    out = np.full(len(values), np.nan)
    filled = index >= 0
    out[filled] = values[index[filled]]
    return out


def _first_valid(values: np.ndarray) -> int | None:
    finite = np.flatnonzero(np.isfinite(values))
    return int(finite[0]) if len(finite) else None


def sma(values, n: int) -> np.ndarray:
    values = carry_forward(values)
    out = np.full(len(values), np.nan)
    if n < 1 or len(values) < n:
        return out
    total = np.cumsum(np.nan_to_num(values))
    count = np.cumsum(np.isfinite(values).astype(int))
    sums = total[n - 1:].copy()
    sums[1:] = total[n:] - total[:-n]
    counts = count[n - 1:].copy()
    counts[1:] = count[n:] - count[:-n]
    out[n - 1:] = np.where(counts == n, sums / n, np.nan)
    return out


def ema(values, n: int) -> np.ndarray:
    values = carry_forward(values)
    out = np.full(len(values), np.nan)
    start = _first_valid(values)
    if n < 1 or start is None or len(values) < start + n:
        return out
    alpha = 2.0 / (n + 1.0)
    current = float(np.mean(values[start:start + n]))
    out[start + n - 1] = current
    for i in range(start + n, len(values)):
        if not np.isfinite(values[i]):
            out[i] = current
            continue
        current = alpha * values[i] + (1.0 - alpha) * current
        out[i] = current
    return out


def wma(values, n: int) -> np.ndarray:
    values = carry_forward(values)
    out = np.full(len(values), np.nan)
    start = _first_valid(values)
    if n < 1 or start is None or len(values) < start + n:
        return out
    weights = np.arange(1, n + 1, dtype=float)
    divisor = n * (n + 1) / 2.0
    segment = values[start:]
    filled = np.nan_to_num(segment)
    # convolution flips the kernel: the reversed weights put the largest weight on the newest session
    sums = np.convolve(filled, weights[::-1], mode='valid')
    out[start + n - 1:] = sums / divisor
    return out


def dema(values, n: int) -> np.ndarray:
    first = ema(values, n)
    return 2.0 * first - ema(first, n)


def tema(values, n: int) -> np.ndarray:
    e1 = ema(values, n)
    e2 = ema(e1, n)
    e3 = ema(e2, n)
    return 3.0 * e1 - 3.0 * e2 + e3


def hma(values, n: int) -> np.ndarray:
    half = max(1, n // 2)
    root = max(1, int(round(math.sqrt(n))))
    return wma(2.0 * wma(values, half) - wma(values, n), root)


def kama(values, n: int, fast: int = 2, slow: int = 30) -> np.ndarray:
    values = carry_forward(values)
    out = np.full(len(values), np.nan)
    start = _first_valid(values)
    if n < 1 or start is None or len(values) < start + n + 1:
        return out
    change = np.full(len(values), np.nan)
    change[n:] = np.abs(values[n:] - values[:-n])
    steps = np.full(len(values), np.nan)
    steps[1:] = np.abs(np.diff(values))
    volatility = np.full(len(values), np.nan)
    cum = np.cumsum(np.nan_to_num(steps))
    volatility[n:] = cum[n:] - cum[:-n]
    with np.errstate(invalid='ignore', divide='ignore'):
        er = np.where(volatility > 0, change / volatility, 0.0)
    fastest, slowest = 2.0 / (fast + 1.0), 2.0 / (slow + 1.0)
    sc = (er * (fastest - slowest) + slowest) ** 2
    current = values[start + n]
    out[start + n] = current
    for i in range(start + n + 1, len(values)):
        if not np.isfinite(values[i]) or not np.isfinite(sc[i]):
            out[i] = current
            continue
        current = current + sc[i] * (values[i] - current)
        out[i] = current
    return out


MOVING_AVERAGES = dict(sma=sma, ema=ema, wma=wma, dema=dema, tema=tema, hma=hma, kama=kama)


# -- data ---------------------------------------------------------------------------------------------------------

def resolve_security(security: str, db_path=None) -> str:
    """Accept a security_id or an exact current ticker; an ambiguous or unknown ticker raises with candidates."""
    conn = api.connect(db_path) if db_path else api.connect()
    if ':' in security:
        row = conn.execute('select 1 from strategy_securities where security_id=? limit 1', (security,)).fetchone()
        if row is None:
            raise ValueError(f'unknown security_id: {security}')
        return security
    rows = conn.execute("""select distinct ss.security_id, coalesce(i.name, '')
                           from strategy_securities ss left join issuers i on i.issuer_id = ss.issuer_id
                           where ss.current_ticker = ? order by 1""", (security,)).fetchall()
    ids = sorted({r[0] for r in rows})
    if not ids:
        raise ValueError(f'no security with ticker {security}')
    if len(ids) > 1:
        detail = ', '.join(f'{r[0]} ({r[1]})' for r in rows)
        raise ValueError(f'ambiguous ticker {security}: {detail}')
    return ids[0]


def _total_return(sid: str, end: str, db_path=None) -> tuple[list[str], np.ndarray]:
    rows = api.tri_series([sid], FIRST_DAY, end, db_path)[sid]
    observed = {r[0]: float(r[1]) for r in rows if r[1] is not None}
    if len(observed) < 2:
        raise ValueError(f'{sid}: no total-return history up to {end}')
    # the full trading calendar from the first observation, so a missing session (even a Friday) is carried
    # forward instead of dropping out of the index
    days = api.trading_days(min(observed), end, db_path)
    values = np.array([observed.get(d, np.nan) for d in days])
    return days, carry_forward(values)


# -- evaluation ---------------------------------------------------------------------------------------------------

def drawdown(days: list[str], values: list[float]) -> dict:
    """Maximum drawdown with its peak, trough and recovery dates (empty when never recovered)."""
    peak, peak_date = values[0], days[0]
    worst, worst_peak_date, trough_date, worst_peak = 0.0, days[0], days[0], values[0]
    longest = streak = 0
    for day, value in zip(days, values):
        if value >= peak:
            peak, peak_date, streak = value, day, 0
        else:
            streak += 1
            longest = max(longest, streak)
        drop = value / peak - 1 if peak else 0.0
        if drop < worst:
            worst, trough_date, worst_peak_date, worst_peak = drop, day, peak_date, peak
    recovery = next((d for d, v in zip(days, values) if d > trough_date and v >= worst_peak), '')
    return dict(max_drawdown=worst, peak_date=worst_peak_date, trough_date=trough_date,
                recovery_date=recovery, longest_underwater_sessions=longest)


def statistics(days: list[str], equity: list[float], rf: dict[str, float]) -> dict:
    rets = [b / a - 1 for a, b in zip(equity, equity[1:]) if a]
    sessions = len(rets)
    cagr = (equity[-1] / equity[0]) ** (TRADING_DAYS / sessions) - 1 if sessions else float('nan')
    mean = sum(rets) / sessions if sessions else 0.0
    var = sum((r - mean) ** 2 for r in rets) / (sessions - 1) if sessions > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    excess = [r - rf.get(day, 0.0) for r, day in zip(rets, days[1:])]
    ex_mean = sum(excess) / len(excess) if excess else 0.0
    ex_var = sum((r - ex_mean) ** 2 for r in excess) / (len(excess) - 1) if len(excess) > 1 else 0.0
    sharpe = ex_mean / math.sqrt(ex_var) * math.sqrt(TRADING_DAYS) if ex_var > 0 else float('nan')
    dd = drawdown(days, equity)
    return dict(cagr=cagr, total_return=equity[-1] / equity[0] - 1, volatility=vol,
                cagr_over_dd=(cagr / abs(dd['max_drawdown']) if dd['max_drawdown'] else float('nan')),
                sharpe=sharpe, **dd)


def simulate(days: list[str], values: np.ndarray, signal_days: list[str], state: list[bool],
             cash_rate: str, rf: dict[str, float]) -> tuple[list[str], list[float]]:
    """Weekly state applied from each signal close; the window starts at the first signal day's close."""
    index = {d: i for i, d in enumerate(days)}
    start = index[signal_days[0]]
    daily = np.zeros(len(days), dtype=bool)
    for k, day in enumerate(signal_days):
        daily[index[day]:] = state[k]
    daily[:start] = False
    window = days[start:]
    equity = [1.0]
    for i in range(start + 1, len(days)):
        if daily[i - 1]:
            equity.append(equity[-1] * (values[i] / values[i - 1]))
        else:
            rate = rf.get(days[i], 0.0) if cash_rate == 'dtb3' else 0.0
            equity.append(equity[-1] * (1.0 + rate))
    return window, equity


def run_lab(security: str, start: str = '2014-02-28', end: str = LAST_ALLOWED_END,
            types=('sma', 'ema'), fasts=(5, 10, 20), slows=(50, 100, 200), cash_rate: str = 'zero',
            db_path=None, save_equity: bool = False) -> dict:
    if end > LAST_ALLOWED_END:
        raise ValueError(f'end {end} is after {LAST_ALLOWED_END}: the reserved segment must not be used')
    if start < FIRST_DAY:
        raise ValueError(f'start {start} is before {FIRST_DAY}')
    if start >= end:
        raise ValueError('start must be before end')
    if cash_rate not in ('zero', 'dtb3'):
        raise ValueError("cash_rate must be 'zero' or 'dtb3'")
    for kind in types:
        if kind not in MOVING_AVERAGES:
            raise ValueError(f'unknown average type: {kind}')
    sid = resolve_security(security, db_path)
    days, values = _total_return(sid, end, db_path)
    signal_days = [d for d in api.friday_signal_days(start, end, db_path) if d >= start]
    if not signal_days:
        raise ValueError('no Friday signal day in the window')
    rf = api.rf_daily(signal_days[0], end, db_path)
    index = {d: i for i, d in enumerate(days)}
    signal_index = [index[d] for d in signal_days]

    window, equity = simulate(days, values, signal_days, [True] * len(signal_days), cash_rate, rf)
    hold = statistics(window, equity, rf)
    hold.update(kind='buy_hold', type='buy_hold', fast='', slow='', invested_share=1.0, buys=1,
                average_holding_weeks=float(len(signal_days)), first_valid_signal_day='', warmup_short=0)
    rows = []
    curves = {'buy_hold': dict(zip(window, equity))}
    cache: dict[tuple[str, int], np.ndarray] = {}

    def average(kind: str, n: int) -> np.ndarray:
        key = (kind, int(n))
        if key not in cache:
            cache[key] = MOVING_AVERAGES[kind](values, int(n))
        return cache[key]

    for kind in types:
        for fast in fasts:
            for slow in slows:
                if int(fast) >= int(slow):
                    continue
                line_fast, line_slow = average(kind, fast), average(kind, slow)
                valid = np.isfinite(line_fast) & np.isfinite(line_slow)
                state = [bool(valid[i] and line_fast[i] > line_slow[i]) for i in signal_index]
                valid_days = [d for d, i in zip(signal_days, signal_index) if valid[i]]
                first_valid = valid_days[0] if valid_days else ''
                window_i, curve = simulate(days, values, signal_days, state, cash_rate, rf)
                row = statistics(window_i, curve, rf)
                buys = sum(1 for k, on in enumerate(state) if on and (k == 0 or not state[k - 1]))
                row.update(kind=kind, type=kind, fast=int(fast), slow=int(slow),
                           invested_share=sum(state) / len(state), buys=buys,
                           average_holding_weeks=(sum(state) / buys if buys else 0.0),
                           first_valid_signal_day=first_valid,
                           warmup_short=int(bool(first_valid) and first_valid > signal_days[0]))
                row['name'] = f'{kind}_{int(fast)}_{int(slow)}'
                rows.append(row)
                if save_equity:
                    curves[row['name']] = dict(zip(window_i, curve))
    for row in rows:
        row.setdefault('name', f"{row['kind']}_{row['fast']}_{row['slow']}")
    out = dict(security=sid, start=start, end=end, cash_rate=cash_rate,
               evaluation_start=window[0], evaluation_end=window[-1],
               signal_days=len(signal_days), buy_hold=hold, rows=rows)
    if save_equity:
        out['equity'] = curves
    return out


# -- output -------------------------------------------------------------------------------------------------------

FIELDS = ['type', 'fast', 'slow', 'cagr', 'total_return', 'volatility', 'max_drawdown', 'peak_date', 'trough_date',
          'recovery_date', 'cagr_over_dd', 'sharpe', 'invested_share', 'buys', 'average_holding_weeks',
          'first_valid_signal_day', 'warmup_short']


def _num(value, digits=8):
    if isinstance(value, float):
        return '' if math.isnan(value) else f'{value:.{digits}f}'
    return value


def write_results(path: Path, out: dict) -> None:
    with path.open('w', encoding='utf8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator='\n')
        writer.writeheader()
        for row in [out['buy_hold']] + out['rows']:
            writer.writerow({key: _num(row.get(key, '')) for key in FIELDS})


def write_report(path: Path, out: dict) -> None:
    hold = out['buy_hold']
    lines = [f"# ma_lab：{out['security']}", '',
             f"- 区间：{out['start']} 至 {out['end']}；评估区间 {out['evaluation_start']} 至 "
             f"{out['evaluation_end']}，共 {out['signal_days']} 个信号周。",
             f"- 现金口径：{out['cash_rate']}；组合数：{len(out['rows'])}。",
             '- 单元格：年化 / 最大回撤 / 年化÷|回撤| / 夏普。', '',
             '## 一直持有', '',
             '| 年化 | 总收益 | 年化波动 | 最大回撤 | 峰值 | 谷底 | 恢复 | 夏普 |',
             '|---|---|---|---|---|---|---|---|',
             f"| {hold['cagr'] * 100:+.1f}% | {hold['total_return'] * 100:+.1f}% | {hold['volatility'] * 100:.1f}% | "
             f"{hold['max_drawdown'] * 100:.1f}% | {hold['peak_date']} | {hold['trough_date']} | "
             f"{hold['recovery_date'] or '—'} | {hold['sharpe']:.2f} |", '']
    index = {(r['kind'], r['fast'], r['slow']): r for r in out['rows']}
    fasts = sorted({r['fast'] for r in out['rows']})
    slows = sorted({r['slow'] for r in out['rows']})
    for kind in sorted({r['kind'] for r in out['rows']}):
        lines += [f'## {kind.upper()}', '', '| 快线 \\ 慢线 | ' + ' | '.join(str(s) for s in slows) + ' |',
                  '|' + '---|' * (len(slows) + 1)]
        for fast in fasts:
            cells = []
            for slow in slows:
                row = index.get((kind, fast, slow))
                cells.append('—' if row is None else
                             f"{row['cagr'] * 100:+.1f}% / {row['max_drawdown'] * 100:.1f}% / "
                             f"{row['cagr_over_dd']:.2f} / {row['sharpe']:.2f}")
            lines.append(f'| {fast} | ' + ' | '.join(cells) + ' |')
        lines.append('')
    ranked = sorted(out['rows'], key=lambda r: (r['cagr_over_dd'] if math.isfinite(r['cagr_over_dd']) else -9e9),
                    reverse=True)
    for title, subset in (('收益 ÷ 最大回撤 前 10', ranked[:10]), ('收益 ÷ 最大回撤 后 10', ranked[-10:])):
        lines += [f'## {title}', '',
                  '| 组合 | 年化 | 最大回撤 | 年化÷\\|回撤\\| | 夏普 | 在场比例 | 买入次数 | 平均持有周 | 首个有效信号日 | 预热不足 |',
                  '|---|---|---|---|---|---|---|---|---|---|']
        for row in subset:
            lines.append(f"| {row['kind']}_{row['fast']}_{row['slow']} | {row['cagr'] * 100:+.1f}% | "
                         f"{row['max_drawdown'] * 100:.1f}% | {row['cagr_over_dd']:.2f} | {row['sharpe']:.2f} | "
                         f"{row['invested_share'] * 100:.0f}% | {row['buys']} | {row['average_holding_weeks']:.1f} | "
                         f"{row['first_valid_signal_day'] or '—'} | {row['warmup_short']} |")
        lines.append('')
    path.write_text('\n'.join(lines) + '\n', encoding='utf8')


def write_equity(path: Path, out: dict) -> None:
    curves = out['equity']
    names = ['buy_hold'] + [r['name'] for r in out['rows']]
    dates = sorted(curves['buy_hold'])
    with path.open('w', encoding='utf8', newline='') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(['date'] + names)
        for day in dates:
            writer.writerow([day] + [f"{curves[n][day]:.6f}" if n in curves and day in curves[n] else ''
                                     for n in names])


def main(argv=None):
    parser = argparse.ArgumentParser(description='single-stock moving-average lab')
    parser.add_argument('--security', required=True)
    parser.add_argument('--start', default='2014-02-28')
    parser.add_argument('--end', default=LAST_ALLOWED_END)
    parser.add_argument('--types', default='sma,ema')
    parser.add_argument('--fast', default='5,10,20')
    parser.add_argument('--slow', default='50,100,200')
    parser.add_argument('--cash', default='zero', choices=('zero', 'dtb3'))
    parser.add_argument('--out', required=True)
    parser.add_argument('--save-equity', action='store_true')
    parser.add_argument('--db', default='data/db/leadtrend_db_v1.sqlite')
    args = parser.parse_args(argv)
    out = run_lab(args.security, start=args.start, end=args.end, types=args.types.split(','),
                  fasts=[int(x) for x in args.fast.split(',')], slows=[int(x) for x in args.slow.split(',')],
                  cash_rate=args.cash, db_path=args.db, save_equity=args.save_equity)
    directory = Path(args.out)
    directory.mkdir(parents=True, exist_ok=True)
    write_results(directory / 'results.csv', out)
    write_report(directory / 'report.md', out)
    if args.save_equity:
        write_equity(directory / 'equity_daily.csv', out)
    print(f"{out['security']} {out['evaluation_start']}..{out['evaluation_end']} combos={len(out['rows'])} "
          f"buy_hold_cagr={out['buy_hold']['cagr']:.4f}")
    return out


if __name__ == '__main__':
    main()
