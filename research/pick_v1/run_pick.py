"""Run the pick_v1 grid and write results.csv, report.md and the weekly basket numbers.

    python -X utf8 research/pick_v1/run_pick.py

Statistics only. Results go to research/pick_v1/; the weekly basket numbers go to work/pick_v1/baskets.csv.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import pick  # noqa: E402

HERE = Path(__file__).resolve().parent
BASKETS = ROOT / 'work' / 'pick_v1' / 'baskets.csv'
STATES = (('zone', '关注区'), ('normal', '平时'))


def percent(value, digits=2):
    return '—' if value is None or not np.isfinite(value) else f'{value * 100:.{digits}f}%'


def build():
    book = pick.load()
    table = pick.baskets(book)
    rows = []
    windows = {}
    for h in pick.HS:
        mask = pick.common_weeks(table, h)
        flags = np.array([level >= pick.ZONE_LEVEL for level in book['levels']])
        weeks = [week for week, keep in zip(book['weeks'], mask) if keep]
        windows[h] = dict(first=weeks[0], last=weeks[-1], weeks=len(weeks),
                          zone=int(np.sum(mask & flags)), normal=int(np.sum(mask & ~flags)))
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
    return book, table, rows, windows


def write_results(rows):
    path = HERE / 'results.csv'
    fields = ['config_id', 'rule', 'K', 'N', 'W', 'H', 'state', 'weeks', 'mean', 'median',
              'win_rate', 'p10', 'beat', 'groups']
    with path.open('w', encoding='utf8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ('' if row[key] is None else
                                   (f'{row[key]:.8f}' if isinstance(row[key], float) else row[key]))
                             for key in fields})
    return path


def write_baskets(book, table):
    BASKETS.parent.mkdir(parents=True, exist_ok=True)
    with BASKETS.open('w', encoding='utf8', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(['week', 'config_id', 'H', 'basket_return', 'voo_return', 'excess'])
        for h in pick.HS:
            mask = pick.common_weeks(table, h)
            for config in pick.configs():
                item = table[config['config_id']]
                for j, week in enumerate(book['weeks']):
                    if not mask[j] or not np.isfinite(item[h][j]):
                        continue
                    excess = float(item[h][j])
                    voo = float(book['voo_forward'][h][j])
                    writer.writerow([week, config['config_id'], h, f'{excess + voo:.8f}',
                                     f'{voo:.8f}', f'{excess:.8f}'])
    return BASKETS


def cells(rows, h, state, field):
    """{(rule, W): {config_id: value}} for one horizon and state."""
    out = {}
    for row in rows:
        if row['H'] != h or row['state'] != state:
            continue
        out.setdefault((row['rule'], row['W']), {})[row['config_id']] = row[field]
    return out


def lookup(index_map, rule, w, k, n):
    """One cell: the WGT rows have no K or W, the measured rows carry both."""
    if rule == 'WGT':
        return index_map.get((rule, w), {}).get(f'WGT_N{n}')
    return index_map.get((rule, w), {}).get(f'{rule}_K{k}_N{n}_W{w}')


def pivot_table(rows, h, state, field, formatter, columns, pairs):
    index_map = cells(rows, h, state, field)
    lines = ['| 依据 + 历史长度 | ' + ' | '.join(columns) + ' |', '|---|' + '---|' * len(columns)]
    for rule in pick.BASES:
        widths = (None,) if rule == 'WGT' else pick.WS
        for w in widths:
            label = rule if w is None else f'{rule} W{w}'
            line = [formatter(lookup(index_map, rule, w, k, n)) for k, n in pairs]
            lines.append(f'| {label} | ' + ' | '.join(line) + ' |')
    for name, _ in pick.ALLS:
        value = index_map.get((name, None), {}).get(name)
        lines.append(f'| {name} | ' + ' | '.join([formatter(value)] * len(pairs)) + ' |')
    return lines


def report(rows, windows):
    pairs = [(k, n) for k in pick.KS for n in pick.NS if n < k]
    columns = [f'{k}/{n}' for k, n in pairs]
    lines = ['# 选股网格统计（pick_v1）', '',
             '每周从当周前 50 里按规则挑 N 只等权买入、持有 H 周，与 VOO 同期收益之差为一个观测；'
             '只用所有 259 个配置都有效的共同周。零成本、小数股、总回报。', '',
             '## 共同周', '', '| H | 第一周 | 最后一周 | 周数 | 关注区周数 | 平时周数 |', '|---|---|---|---|---|---|']
    for h in pick.HS:
        item = windows[h]
        lines.append(f"| {h} | {item['first']} | {item['last']} | {item['weeks']} | {item['zone']} | {item['normal']} |")
    lines += ['', '关注区 = 市场关注区等级 ≥ 2 的周；平时 = 其余周。', '']
    for h in pick.HS:
        for state, label in STATES:
            lines += [f'## H = {h} 周 · {label}', '', '### 中位数', '']
            lines += pivot_table(rows, h, state, 'median', percent, columns, pairs)
            lines += ['', '### 胜基准（配置均值 > ALL10 同期均值）', '']
            lines += pivot_table(rows, h, state, 'beat',
                                 lambda v: '—' if v is None else f'{int(v)}', columns, pairs)
            groups = next((row['groups'] for row in rows if row['H'] == h and row['state'] == state), 0)
            if state == 'zone':
                lines += ['', f'关注区按段分组，共 {groups} 段；表里写赢了几段。', '']
            else:
                lines += ['', f'平时按自然年分组，共 {groups} 年；表里写赢了的年数。', '']
    return '\n'.join(lines) + '\n'


def main():
    book, table, rows, windows = build()
    path = write_results(rows)
    baskets_path = write_baskets(book, table)
    (HERE / 'report.md').write_text(report(rows, windows), encoding='utf8', newline='\n')
    (HERE / 'summary.json').write_text(json.dumps(dict(windows={str(k): v for k, v in windows.items()},
                                                       configs=len(pick.configs()),
                                                       weeks=len(book['weeks']),
                                                       versions=book['versions']),
                                                  ensure_ascii=False, indent=1), encoding='utf8', newline='\n')
    print('rows', len(rows), '->', path)
    print('baskets ->', baskets_path)
    for h in pick.HS:
        item = windows[h]
        print(h, item)


if __name__ == '__main__':
    main()
