"""Run one backtest configuration and write its outputs to work/bt_runs/<run_id>/."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from leadtrend_db import api

from . import metrics
from .engine import Backtest

OUT_ROOT = Path('work/bt_runs')


def _num(value, digits=8):
    if value is None or value == '':
        return ''
    if isinstance(value, float):
        return f'{value:.{digits}f}'
    return value


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(header)
        for row in rows:
            writer.writerow([_num(row.get(h)) for h in header])


def sell_plan(result: dict) -> dict[str, int]:
    """Number of sells the original configuration decided per (signal day, tranche), from weekly_decisions.

    The random control uses this count, never the filled trades: a decision that could not be filled (no opening
    price) must not inflate the number of names the random book is allowed to drop.
    """
    plan: dict[str, int] = {}
    for decision in result['decisions']:
        if decision['action'] != 'sell':
            continue
        key = f"{decision['signal_date']}|{decision['tranche']}"
        plan[key] = plan.get(key, 0) + 1
    return plan


def run(cfg: dict, out_root: Path | str = OUT_ROOT, db_path=None, write: bool = True) -> dict:
    bt = Backtest(cfg, db_path=db_path)
    result = bt.run()
    rf = api.rf_daily(cfg['start'], cfg['end'], db_path) if db_path else api.rf_daily(cfg['start'], cfg['end'])
    summary = metrics.compute(result, rf)
    summary['config'] = dict(cfg)
    summary['tranches'] = bt.K
    if not write:
        return dict(summary=summary, result=result, engine=bt)
    out = Path(out_root) / cfg['run_id']
    out.mkdir(parents=True, exist_ok=True)
    (out / 'config.json').write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding='utf8', newline='\n')
    write_csv(out / 'daily_equity.csv', ['date', 'cash', 'positions_value', 'equity', 'benchmark'], result['daily'])
    write_csv(out / 'trades.csv', ['date', 'tranche', 'security_id', 'side', 'shares', 'price', 'fee', 'reason',
                                   'signal_date'], result['trades'])
    write_csv(out / 'positions.csv', ['date', 'tranche', 'security_id', 'shares', 'close', 'value'],
              result['positions'])
    write_csv(out / 'weekly_decisions.csv', ['signal_date', 'tranche', 'security_id', 'action', 'rank', 'score'],
              result['decisions'])
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding='utf8',
                                      newline='\n')
    return dict(summary=summary, result=result, engine=bt, path=out)
