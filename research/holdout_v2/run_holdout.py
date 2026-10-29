"""One run of the four batches on the reserved segment, 2024-07-01 to 2026-06-30.

Every state is built continuously from 2014-02-28, so the concentrated state of the week ending 2024-06-28
carries into the reserved segment; the portfolios themselves start at that close with 100,000 dollars.
Registered in research/holdout_v2/spec.md. Runs once.

    python -X utf8 research/holdout_v2/run_holdout.py
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import boll_pf, ma_lab, pick, pick_pf  # noqa: E402

HERE = Path(__file__).resolve().parent
DB = ROOT / 'data' / 'db' / 'leadtrend_db_full_v2.sqlite'
START, CUT, END = '2014-02-28', '2024-06-28', '2026-06-30'
BATCHES = ('boll_pf_v1', 'boll_pf_v2', 'boll_pf_v3', 'pick_v1')
STATES = (('zone', '关注区'), ('normal', '平时'))

spec = importlib.util.spec_from_file_location('pick_run', ROOT / 'research' / 'pick_v1' / 'run_pick.py')
pick_run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pick_run)


def percent(value, digits=2):
    return '—' if value is None or not np.isfinite(value) else f'{value * 100:.{digits}f}%'


def line_row(name, batch, metrics, dev_cagr, voo):
    annual = metrics['annual_returns']
    return dict(
        batch=batch, name=name, dev_cagr=dev_cagr, cagr=metrics['cagr'], excess_cagr=metrics['cagr'] - voo['cagr'],
        max_drawdown=metrics['max_drawdown'], peak_date=metrics['peak_date'], trough_date=metrics['trough_date'],
        recovery_date=metrics['recovery_date'], longest_underwater_sessions=metrics['longest_underwater_sessions'],
        annual={year: value for year, value in annual.items()},
        annual_excess={year: value - voo['annual_returns'].get(year, float('nan')) for year, value in annual.items()},
        annual_turnover=metrics['annual_turnover'], average_holdings=metrics['average_holdings'],
        total_return=metrics['total_return'])


def portfolio_section(v1, v3):
    dev = {batch: json.loads((ROOT / 'research' / batch / 'summary.json').read_text(encoding='utf8'))['lines']
           for batch in ('boll_pf_v1', 'boll_pf_v2', 'boll_pf_v3')}
    rows = []
    for name in ('VOO', 'EW50', 'EW10', 'CW10', 'TILT_A', 'TILT_B', 'TILT_C'):
        batch = 'boll_pf_v2' if name == 'TILT_C' else 'boll_pf_v1'
        rows.append(line_row(name, batch, v1['metrics'][name], dev[batch][name]['cagr'], v1['metrics']['VOO']))
    for name in ('VOO', 'EW10', 'TILT_B*', 'WGT5', 'WB5', 'EW10_VOL', 'WGT5_VOL', 'WB5_VOL'):
        rows.append(line_row(name, 'boll_pf_v3', v3['metrics'][name], dev['boll_pf_v3'][name]['cagr'],
                             v3['metrics']['VOO']))
    return rows


def zone_section(book):
    weeks = book['weeks']
    keep = [j for j, day in enumerate(weeks) if day >= CUT]
    flags = [book['levels'][j] >= 2 and j in set(keep) for j in range(len(weeks))]
    runs = boll_pf.runs(flags, weeks)
    index = {day: j for j, day in enumerate(weeks)}
    armed = [j for j in keep if book['armed'][j]]
    starts = [j for j in keep if book['concentrated_c'][j] and (j == 0 or not book['concentrated_c'][j - 1])]
    rows = []
    for run in runs:
        first = index[run['start']]
        inside = [j for j in keep if run['start'] <= weeks[j] <= run['end']]
        rows.append(dict(start=run['start'], end=run['end'], weeks=run['weeks'],
                         armed=[weeks[j] for j in inside if book['armed'][j]],
                         tilt_c_start=next((weeks[j] for j in inside if book['concentrated_c'][j]), None),
                         strongest=max(book['levels'][j] for j in inside)))
    cut_i = index[CUT]
    return dict(rows=rows, tilt_b_at_cut=bool(book['concentrated'][cut_i]),
                tilt_c_at_cut=bool(book['concentrated_c'][cut_i]),
                armed_at_cut=bool(book['armed'][cut_i]),
                armed_weeks=[weeks[j] for j in armed], tilt_c_starts=[weeks[j] for j in starts])


def pick_section(book, table):
    hold = np.array([day >= CUT for day in book['weeks']])
    rows, windows = [], {}
    for h in pick.HS:
        mask = pick.common_weeks(table, h) & hold
        flags = np.array([level >= pick.ZONE_LEVEL for level in book['levels']])
        weeks = [week for week, keep in zip(book['weeks'], mask) if keep]
        windows[h] = dict(first=weeks[0] if weeks else None, last=weeks[-1] if weeks else None,
                          weeks=len(weeks), zone=int(np.sum(mask & flags)), normal=int(np.sum(mask & ~flags)))
        for state, label in STATES:
            state_mask = flags if state == 'zone' else ~flags
            groups = pick.groups(book, mask, state)
            baseline = table['ALL10'][h]
            for config in pick.configs():
                item = table[config['config_id']]
                row = pick.summarise(item[h], mask & state_mask, groups, baseline)
                rows.append(dict(config_id=config['config_id'], rule=config['rule'], K=config['K'],
                                 N=config['N'], W=config['W'], H=h, state=state, state_label=label,
                                 weeks=row['weeks'], mean=row['mean'], median=row['median'],
                                 win_rate=row['win_rate'], p10=row['p10'],
                                 beat=row['beat'], groups=row['groups']))
    return rows, windows


def write_pick(path, rows):
    fields = ['config_id', 'rule', 'K', 'N', 'W', 'H', 'state', 'weeks', 'mean', 'median',
              'win_rate', 'p10', 'beat', 'groups']
    with path.open('w', encoding='utf8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ('' if row[key] is None else
                                   (f'{row[key]:.8f}' if isinstance(row[key], float) else row[key]))
                             for key in fields})


def report(lines, zones, pick_rows, windows):
    out = ['# 保留段结果（holdout_v2）', '',
           '保留段已被部分看过，结果只作参考，不作检验。', '',
           f'区间 {CUT} 收盘至 {END}，本金 100,000 美元；所有状态从 {START} 起连续计算，'
           '保留段不参与任何挑选。数据库 data/db/leadtrend_db_full_v2.sqlite。', '',
           '## 组合', '',
           '| 批次 | 线 | 开发段年化 | 保留段年化 | 保留段比 VOO 多 | 最大回撤 | 峰值 | 谷底 | 恢复 | 最长水下交易日 | '
           '2024 下半年 | 比 VOO | 2025 | 比 VOO | 2026 上半年 | 比 VOO | 年换手 |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for row in lines:
        annual = row['annual']
        excess = row['annual_excess']
        cells = []
        for year in ('2024', '2025', '2026'):
            cells += [percent(annual.get(year)), percent(excess.get(year))]
        out.append(f"| {row['batch']} | {row['name']} | {percent(row['dev_cagr'])} | {percent(row['cagr'])} | "
                   f"{percent(row['excess_cagr'])} | {percent(row['max_drawdown'])} | {row['peak_date']} | "
                   f"{row['trough_date']} | {row['recovery_date'] or '—'} | {row['longest_underwater_sessions']} | "
                   + ' | '.join(cells) + f" | {row['annual_turnover']:.2f} |")
    out += ['', '2024 为 6 月 28 日之后的部分年份，2026 到 6 月 30 日为止；2020 与 2022 年内回撤在保留段为空。', '',
            '## 保留段的市场关注区', '', '| 开始 | 结束 | 周数 | 最强等级 |', '|---|---|---|---|']
    for row in zones['rows']:
        out.append(f"| {row['start']} | {row['end']} | {row['weeks']} | {row['strongest']} |")
    out += ['', f"2024-06-28 收盘时：TILT_B 集中状态 {'是' if zones['tilt_b_at_cut'] else '否'}，"
                f"TILT_C 集中状态 {'是' if zones['tilt_c_at_cut'] else '否'}，"
                f"TILT_C 准备状态 {'是' if zones['armed_at_cut'] else '否'}。", '',
            'TILT_C 的准备周：' + ('、'.join(zones['armed_weeks']) if zones['armed_weeks'] else '无') + '。', '',
            'TILT_C 开始集中的周：' + ('、'.join(zones['tilt_c_starts']) if zones['tilt_c_starts'] else '无') + '。', '',
            '| 关注区段 | TILT_C 准备周 | TILT_C 开始集中 |', '|---|---|---|']
    for row in zones['rows']:
        out.append(f"| {row['start']}~{row['end']} | {'、'.join(row['armed']) or '—'} | {row['tilt_c_start'] or '—'} |")
    out += ['', '## pick_v1', '', '| H | 第一周 | 最后一周 | 周数 | 关注区周数 | 平时周数 |', '|---|---|---|---|---|---|']
    for h in pick.HS:
        item = windows[h]
        out.append(f"| {h} | {item['first']} | {item['last']} | {item['weeks']} | {item['zone']} | {item['normal']} |")
    pairs = [(k, n) for k in pick.KS for n in pick.NS if n < k]
    columns = [f'{k}/{n}' for k, n in pairs]
    for h in pick.HS:
        for state, label in STATES:
            out += ['', f'### H = {h} 周 · {label}', '', '#### 中位数', '']
            out += pick_run.pivot_table(pick_rows, h, state, 'median', pick_run.percent, columns, pairs)
            out += ['', '#### 胜基准（配置均值 > ALL10 同期均值）', '']
            out += pick_run.pivot_table(pick_rows, h, state, 'beat',
                                        lambda v: '—' if v is None else f'{int(v)}', columns, pairs)
            groups = next((row['groups'] for row in pick_rows if row['H'] == h and row['state'] == state), 0)
            unit = '段' if state == 'zone' else '年'
            out += ['', f'共 {groups} {unit}。', '']
    return '\n'.join(out) + '\n'


def main():
    checks = {}
    for batch in BATCHES:
        payload = ma_lab.check_holdout(END, str(DB), batch=batch)
        checks[batch] = payload['scope']

    v1 = boll_pf.run(str(DB), start=START, end=END, lines=boll_pf.LINES_V2,
                     allow_holdout=True, batch='boll_pf_v1', sim_start=CUT)
    v3 = pick_pf.run(str(DB), end=END, allow_holdout=True, batch='boll_pf_v3', sim_start=CUT)
    book = pick.load(str(DB), end=END, allow_holdout=True, batch='pick_v1')
    table = pick.baskets(book)

    lines = portfolio_section(v1, v3)
    zones = zone_section(v1['book'])
    pick_rows, windows = pick_section(book, table)

    write_pick(HERE / 'pick_results.csv', pick_rows)
    summary = dict(start=START, cut=CUT, end=END, database=str(DB.relative_to(ROOT)),
                   authorization=checks, lines=lines, zones=zones,
                   pick=dict(windows={str(k): v for k, v in windows.items()}, rows=len(pick_rows)))
    (HERE / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float),
                                       encoding='utf8', newline='\n')
    (HERE / 'report.md').write_text(report(lines, zones, pick_rows, windows), encoding='utf8', newline='\n')
    for row in lines:
        print(f"{row['batch']:11} {row['name']:9} dev {row['dev_cagr']*100:6.2f}%  hold {row['cagr']*100:6.2f}%  "
              f"excess {row['excess_cagr']*100:6.2f}%  dd {row['max_drawdown']*100:6.1f}%")
    print('zone rows', len(zones['rows']), 'pick rows', len(pick_rows))


if __name__ == '__main__':
    main()
