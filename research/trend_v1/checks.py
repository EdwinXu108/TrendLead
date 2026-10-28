"""Account checks for two trend runs (research/trend_v1/spec.md section 4.5).

    python -X utf8 research/trend_v1/checks.py

Verifies, from the written run directories: cash + positions = equity every session, cash never negative,
share counts change only on a trade or a corporate-action date, and every trade reason is one of the three
trend reasons (corporate-action liquidations carry their own side).
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, '.')
from leadtrend_db import api

RUNS = Path('work/trend_v1/runs')
CASES = ('T_sma50_200_N10_equal_zero', 'T_macd_N20_equal_zero')
REASONS = {'signal', 'pool', 'class_switch'}


def check(run_id: str) -> dict:
    base = RUNS / run_id
    daily = list(csv.DictReader((base / 'daily_equity.csv').open(encoding='utf8')))
    positions = list(csv.DictReader((base / 'positions.csv').open(encoding='utf8')))
    trades = list(csv.DictReader((base / 'trades.csv').open(encoding='utf8')))
    actions = list(csv.DictReader((base / 'corporate_actions.csv').open(encoding='utf8')))
    worst_cash = min(float(row['cash']) for row in daily)
    worst_sum = max(abs(float(row['cash']) + float(row['positions_value']) - float(row['equity'])) for row in daily)
    trade_days = {(t['date'], t['security_id']) for t in trades}
    action_days = {(a['date'], a['security_id']) for a in actions}
    previous = {}
    unexplained = []
    for row in positions:
        key = row['security_id']
        shares = round(float(row['shares']), 6)
        if key in previous and abs(shares - previous[key]) > 1e-6:
            if (row['date'], key) not in trade_days and (row['date'], key) not in action_days:
                unexplained.append((row['date'], key, previous[key], shares))
        previous[key] = shares
    reasons = sorted({t['reason'] for t in trades if t['side'] != 'corporate_action_liquidation'})
    return dict(run_id=run_id, sessions=len(daily), min_cash=worst_cash, max_equity_gap=worst_sum,
                trades=len(trades), reasons=reasons, bad_reasons=sorted(set(reasons) - REASONS),
                unexplained_share_changes=unexplained[:5], unexplained_count=len(unexplained))


def main():
    out = [check(run) for run in CASES]
    print(json.dumps(out, indent=2, default=str))
    Path('work/trend_v1/checks.json').write_text(json.dumps(out, indent=2, default=str), encoding='utf8')


if __name__ == '__main__':
    main()
