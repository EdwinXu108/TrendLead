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


def buy_plan(result: dict) -> dict[str, list[str]]:
    """Buys decided by the original configuration per (signal day, tranche), in decision order."""
    plan: dict[str, list[str]] = {}
    for decision in result['decisions']:
        if decision['action'] == 'buy':
            plan.setdefault(f"{decision['signal_date']}|{decision['tranche']}", []).append(decision['security_id'])
    return plan


def merge_results(core: dict, satellite: dict, core_capital: float, satellite_capital: float) -> dict:
    """Add two independent accounts day by day; a date before a sleeve starts is that sleeve's cash."""
    core_daily = {row['date']: row for row in core['daily']}
    sat_daily = {row['date']: row for row in satellite['daily']}
    daily = []
    for day in sorted(set(core_daily) | set(sat_daily)):
        c = core_daily.get(day) or dict(date=day, cash=core_capital, positions_value=0.0, equity=core_capital,
                                        benchmark=0.0)
        s = sat_daily.get(day) or dict(date=day, cash=satellite_capital, positions_value=0.0,
                                       equity=satellite_capital, benchmark=0.0)
        daily.append(dict(date=day, cash=c['cash'] + s['cash'],
                          positions_value=c['positions_value'] + s['positions_value'],
                          equity=c['equity'] + s['equity'], benchmark=c['benchmark'] + s['benchmark']))
    positions: dict[tuple[str, str], dict] = {}
    for row in core['positions'] + satellite['positions']:
        key = (row['date'], row['security_id'])
        item = positions.setdefault(key, dict(date=row['date'], tranche=0, security_id=row['security_id'],
                                             shares=0.0, close=row['close'], value=0.0))
        item['shares'] += row['shares']
        item['value'] += row['value']
    trades = []
    for sleeve, result in (('core', core), ('satellite', satellite)):
        for trade in result['trades']:
            trades.append(dict(trade, sleeve=sleeve))
    trades.sort(key=lambda t: (t['date'], t['sleeve'], t['security_id'], t['side']))
    decisions = []
    for sleeve, result in (('core', core), ('satellite', satellite)):
        for decision in result['decisions']:
            decisions.append(dict(decision, sleeve=sleeve))
    decisions.sort(key=lambda d: (d['signal_date'], d['sleeve'], d['security_id'], d['action']))
    pnl: dict[str, float] = {}
    for result in (core, satellite):
        for issuer, value in result['pnl'].items():
            pnl[issuer] = pnl.get(issuer, 0.0) + value
    issuers = dict(core.get('issuers', {}))
    issuers.update(satellite.get('issuers', {}))
    return dict(daily=daily, positions=sorted(positions.values(), key=lambda r: (r['date'], r['security_id'])),
                trades=trades, decisions=decisions, pnl=pnl, issuers=issuers, config={})


def run_core_satellite(core_cfg: dict, sat_cfg: dict, out_root: Path | str = OUT_ROOT,
                       db_path=None) -> dict:
    """Run the two sleeves and write the merged account under core_cfg['run_id']."""
    core = run(core_cfg, out_root=out_root, db_path=db_path, write=False)
    satellite = run(sat_cfg, out_root=out_root, db_path=db_path, write=False)
    merged = merge_results(core['result'], satellite['result'], core_cfg['capital'], sat_cfg['capital'])
    # The benchmark is the whole capital in VOO from the merged account's first trade, not the sum of two
    # separately-timed entries: a later satellite start would otherwise add capital to the benchmark series.
    engine = core['engine']
    entry = core['result']['trades'][0]['date']
    tri = engine.tri['BENCH:VOO']
    units = (core_cfg['capital'] + sat_cfg['capital']) / (tri[entry] * engine.opens['BENCH:VOO'][entry]
                                                          / engine.closes['BENCH:VOO'][entry])
    for row in merged['daily']:
        row['benchmark'] = units * tri.get(row['date'], 0.0)
    rf = api.rf_daily(core_cfg['start'], core_cfg['end'], db_path) if db_path else api.rf_daily(
        core_cfg['start'], core_cfg['end'])
    summary = metrics.compute(merged, rf)
    summary['config'] = dict(core=core_cfg, satellite=sat_cfg)
    summary['tranches'] = 0
    out = Path(out_root) / core_cfg['run_id']
    out.mkdir(parents=True, exist_ok=True)
    (out / 'config.json').write_text(json.dumps(summary['config'], indent=2, sort_keys=True), encoding='utf8',
                                     newline='\n')
    write_csv(out / 'daily_equity.csv', ['date', 'cash', 'positions_value', 'equity', 'benchmark'], merged['daily'])
    write_csv(out / 'positions.csv', ['date', 'tranche', 'security_id', 'shares', 'close', 'value'],
              merged['positions'])
    write_csv(out / 'trades.csv', ['date', 'sleeve', 'tranche', 'security_id', 'side', 'shares', 'price', 'fee',
                                   'reason', 'signal_date'], merged['trades'])
    write_csv(out / 'weekly_decisions.csv', ['signal_date', 'sleeve', 'tranche', 'security_id', 'action', 'rank',
                                             'score'], merged['decisions'])
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding='utf8',
                                      newline='\n')
    return dict(summary=summary, result=merged, path=out)


def run(cfg: dict, out_root: Path | str = OUT_ROOT, db_path=None, write: bool = True,
        files: bool = True) -> dict:
    cfg = dict(cfg)
    cfg.setdefault('protocol', 'research_v1')
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
    if files:                                   # random controls keep only config.json and summary.json
        write_csv(out / 'daily_equity.csv', ['date', 'cash', 'positions_value', 'equity', 'benchmark'],
                  result['daily'])
        write_csv(out / 'trades.csv', ['date', 'tranche', 'security_id', 'side', 'shares', 'price', 'fee', 'reason',
                                       'signal_date'], result['trades'])
        write_csv(out / 'positions.csv', ['date', 'tranche', 'security_id', 'shares', 'close', 'value'],
                  result['positions'])
        write_csv(out / 'weekly_decisions.csv', ['signal_date', 'tranche', 'security_id', 'action', 'rank', 'score'],
                  result['decisions'])
        if cfg['protocol'] in ('research_v1', 'terra_r1_v1'):
            write_csv(out / 'corporate_actions.csv', ['date','tranche','security_id','issuer','method','shares','ratio',
                'per_share','child_security_id','gross','net','fee','cost','cash_available','source'], result['event_flows'])
            write_csv(out / 'score_counts.csv', ['date','pool_companies','scoreable','insufficient_history','missing_days'],
                      result['score_counts'])
    (out / 'summary.json').write_text(json.dumps(summary, indent=2, sort_keys=True, default=str), encoding='utf8',
                                      newline='\n')
    return dict(summary=summary, result=result, engine=bt, path=out)
