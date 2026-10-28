"""Independent recomputation of four trend configurations without the backtest engine.

Straight-line implementation of research/trend_v1/spec.md section 5: weekly Friday close, fill at that close,
zero cost, fractional shares, no resizing of held names, dividends to cash, splits on shares, spinoffs valued
the same way the engine values them. Signals use their own moving-average code, not leadtrend_bt.trend, so the
two implementations check each other.

    python -X utf8 research/trend_v1/independent_check.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, '.')
from leadtrend_db import api

DB = 'data/db/leadtrend_db_v1.sqlite'
START, END = '2014-02-28', '2024-06-28'
CAPITAL = 100000.0
BENCH = 'BENCH:VOO'


def carried(values: list) -> list:
    out, last = [], None
    for value in values:
        if value is None:
            out.append(last)
        else:
            last = value
            out.append(value)
    return out


def moving_average(values: list, n: int, kind: str) -> list:
    clean = carried(values)
    out = [None] * len(clean)
    if kind == 'sma':
        for i in range(n - 1, len(clean)):
            window = clean[i - n + 1:i + 1]
            if all(v is not None for v in window):
                out[i] = sum(window) / n
        return out
    alpha = 2.0 / (n + 1.0)
    current = None
    for i in range(n - 1, len(clean)):
        window = clean[i - n + 1:i + 1]
        if current is None:
            if all(v is not None for v in window):
                current = sum(window) / n
                out[i] = current
            continue
        if clean[i] is None:
            out[i] = current
            continue
        current = alpha * clean[i] + (1 - alpha) * current
        out[i] = current
    return out


def long_flags(tri: dict, days: list, ma: str, fast: int, slow: int) -> dict:
    if ma == 'always':
        return {d: True for d in days}
    values = [tri.get(d) for d in days]
    if ma == 'macd':
        line = [a - b if a is not None and b is not None else None
                for a, b in zip(moving_average(values, 12, 'ema'), moving_average(values, 26, 'ema'))]
        signal = moving_average(line, 9, 'ema')
        return {d: bool(f is not None and s is not None and f > s) for d, f, s in zip(days, line, signal)}
    f, s = moving_average(values, fast, ma), moving_average(values, slow, ma)
    return {d: bool(a is not None and b is not None and a > b) for d, a, b in zip(days, f, s)}


def pool_of(day: str, top_n: int) -> list:
    pool = api.pool_at(day, DB)
    amounts = api.pool_amounts(pool['version_id'], DB)
    company = api.pool_company_amounts(pool['version_id'], DB)
    best = {}
    for sid, row in amounts.items():
        issuer = row['issuer_id']
        if issuer not in best or row['amount_cents'] > best[issuer][0]:
            best[issuer] = (float(row['amount_cents']), sid)
    ranked = sorted(best.items(), key=lambda kv: (-company.get(kv[0], 0.0), kv[1][1]))[:top_n]
    return [(sid, issuer, company.get(issuer, 0.0)) for issuer, (_, sid) in ranked]


def simulate(ma: str, fast: int, slow: int, top_n: int, slot: str, universe: str = 'pool') -> dict:
    days = api.trading_days('2012-12-01', END)
    signal_days = api.friday_signal_days(START, END)
    pools = {d: ([(BENCH, BENCH, 1.0)] if universe == 'VOO' else pool_of(d, top_n)) for d in signal_days}
    sids = sorted({sid for rows in pools.values() for sid, _, _ in rows})
    tri = {sid: {d: v for d, v, _ in rows} for sid, rows in api.tri_series(sids, days[0], END, DB).items()}
    closes = api.close_series([s for s in sids if s != BENCH], days[0], END, DB)
    if BENCH in sids:
        closes[BENCH] = {d: b['close'] for d, b in api.benchmark_bars(days[0], END, DB).items()}
    flags = {sid: long_flags(tri[sid], days, ma, fast, slow) for sid in sids}
    actions = {}
    for sid in sids:
        if sid == BENCH:
            actions[sid] = []
            continue
        rows = [dict(r) for r in api.connect(DB).execute(
            """select action_type, action_date, ratio, cash_amount, spin_value, vendor_factor, notes
               from corporate_actions where security_id=? order by action_date""", (sid,))]
        for row in rows:
            marker = 'verified_entitlement='
            if marker in (row['notes'] or ''):
                evidence = json.loads(row['notes'].split(marker, 1)[1])
                row.update(child_close=evidence['child_close'], child_ratio=evidence['child_ratio'])
        actions[sid] = rows
    cash = CAPITAL
    shares, issuer_of, previous_close = {}, {}, {}
    equity_curve, dates = [], []
    for day in days:
        for sid in list(shares):
            for row in actions[sid]:
                if row['action_date'] != day:
                    continue
                if row['action_type'] in ('split', 'reverse_split') and row['ratio']:
                    shares[sid] *= float(row['ratio'])
                elif row['action_type'] in ('cash_dividend', 'special_dividend') and row['cash_amount']:
                    cash += shares[sid] * float(row['cash_amount'])
                elif row['action_type'] in ('spinoff', 'class_distribution'):
                    value = None
                    if row.get('child_close') is not None:
                        value = float(row['child_close']) * float(row.get('child_ratio') or 1)
                    if value is None and row['spin_value'] is not None:
                        value = float(row['spin_value'])
                    if value is None and row['vendor_factor'] is not None:
                        value = (1 - float(row['vendor_factor'])) * previous_close.get(sid, 0.0)
                    if value is None:
                        value = max(0.0, previous_close.get(sid, 0.0) - api.open_prices([sid], day, DB).get(sid, 0.0))
                    cash += shares[sid] * value
        for sid in shares:
            if closes.get(sid, {}).get(day):
                previous_close[sid] = closes[sid][day]
        if day in pools:
            rows = pools[day]
            target = {sid for sid, _, _ in rows}
            by_issuer = {issuer: sid for sid, issuer, _ in rows}
            sells, switch = [], {}
            for sid in list(shares):
                issuer = issuer_of.get(sid, '')
                if sid in target:
                    if not flags[sid].get(day, False):
                        sells.append(sid)
                elif issuer in by_issuer:
                    sells.append(sid)
                    switch[by_issuer[issuer]] = sid
                else:
                    sells.append(sid)
            switch_amount = {}
            for sid in sells:
                price = closes.get(sid, {}).get(day) or previous_close.get(sid, 0.0)
                proceeds = shares.pop(sid) * price
                cash += proceeds
                issuer_of.pop(sid, None)
                for new_sid, old_sid in switch.items():
                    if old_sid == sid:
                        switch_amount[new_sid] = proceeds
            held = set(issuer_of.values())
            equity = cash + sum(sh * (closes.get(s, {}).get(day) or previous_close.get(s, 0.0))
                                for s, sh in shares.items())
            total = sum(amount for _, _, amount in rows) or 1.0
            buys = []
            for new_sid, old_sid in switch.items():
                if flags[new_sid].get(day, False):
                    buys.append((new_sid, switch_amount.get(new_sid, 0.0)))
            for sid, issuer, amount in rows:
                if sid in switch or issuer in held or not flags[sid].get(day, False):
                    continue
                buys.append((sid, equity * (amount / total if slot == 'amount' else 1.0 / top_n)))
            wanted = sum(amount for _, amount in buys)
            scale = min(1.0, cash / wanted) if wanted > 0 else 1.0
            for sid, amount in buys:
                price = closes.get(sid, {}).get(day)
                budget = min(amount * scale, cash)
                if not price or budget <= 0:
                    continue
                shares[sid] = shares.get(sid, 0.0) + budget / price
                issuer_of[sid] = dict((s, i) for s, i, _ in rows)[sid]
                cash -= budget
        if day >= START:
            equity_curve.append(cash + sum(sh * (closes.get(s, {}).get(day) or previous_close.get(s, 0.0))
                                           for s, sh in shares.items()))
            dates.append(day)
    years = (len(equity_curve) - 1) / 252
    return dict(cagr=(equity_curve[-1] / CAPITAL) ** (1 / years) - 1, dates=dates, equity=equity_curve)


CASES = (
    ('sma', 50, 200, 10, 'equal', 'pool', 'T_sma50_200_N10_equal_zero'),
    ('ema', 20, 100, 5, 'amount', 'pool', 'T_ema20_100_N5_amount_zero'),
    ('macd', 12, 26, 20, 'equal', 'pool', 'T_macd_N20_equal_zero'),
    ('always', 0, 0, 10, 'amount', 'pool', 'C1_always_N10_amount_zero'),
)


def main():
    out = []
    for ma, fast, slow, n, slot, universe, run_id in CASES:
        result = simulate(ma, fast, slow, n, slot, universe)
        engine = json.loads((Path('work/trend_v1/runs') / run_id / 'summary.json').read_text(encoding='utf8'))
        diff = result['cagr'] - engine['strategy']['cagr']
        out.append(dict(run_id=run_id, independent_cagr=result['cagr'], engine_cagr=engine['strategy']['cagr'],
                        difference_pp=diff * 100))
        print(f"{run_id:34s} independent {result['cagr']:.6f}  engine {engine['strategy']['cagr']:.6f}  "
              f"diff {diff * 100:+.4f} pp")
    Path('work/trend_v1/independent_check.json').write_text(json.dumps(out, indent=2), encoding='utf8')


if __name__ == '__main__':
    main()
