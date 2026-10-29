"""Run boll_pf_v2: the six lines of v1 plus TILT_C, and write summary.json and report.md.

    python -X utf8 research/boll_pf_v2/run_pf.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import boll_pf  # noqa: E402

HERE = Path(__file__).resolve().parent
LINES = boll_pf.LINES_V2
FIELDS = ('cagr', 'excess_cagr', 'max_drawdown', 'peak_date', 'trough_date', 'recovery_date',
          'longest_underwater_sessions', 'max_drawdown_2020', 'max_drawdown_2022', 'annual_turnover',
          'average_holdings', 'total_return', 'volatility')


def pct(value):
    return '—' if value is None else f'{value * 100:.2f}%'


def arming(book):
    """Every market episode: its first week, and the week TILT_C actually started concentrating."""
    weeks, rows = book['weeks'], []
    for run in boll_pf.runs([level >= boll_pf.ZONE_LEVEL for level in book['levels']], weeks):
        k = weeks.index(run['start'])
        while k < len(weeks) and not (book['armed'][k] and book['permit'][k]):
            k += 1
        rows.append(dict(start=run['start'], end=run['end'], weeks=run['weeks'],
                         trigger=weeks[k] if k < len(weeks) else None,
                         wait=k - weeks.index(run['start']) if k < len(weeks) else None))
    return rows


def main():
    result = boll_pf.run(lines=LINES)
    rows = {}
    for name in LINES:
        row = result['metrics'][name]
        rows[name] = {key: row[key] for key in FIELDS}
        rows[name].update(worst_year=row['worst_year']['year'], worst_year_return=row['worst_year']['return_'])
    v1 = json.loads((ROOT / 'research' / 'boll_pf_v1' / 'summary.json').read_text(encoding='utf8'))['lines']
    same = {name: abs(rows[name]['cagr'] - v1[name]['cagr']) < 1e-12 for name in boll_pf.LINES}
    episodes = arming(result['book'])
    summary = dict(lines=rows, yearly=result['yearly'],
                   segments={name: result['segments'][name] for name in ('TILT_B', 'TILT_C')},
                   episodes=episodes, v1_cagr_unchanged=same,
                   tilt_c_weeks=sum(result['book']['concentrated_c']))
    (HERE / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float),
                                       encoding='utf8', newline='\n')

    out = ['# 组合第二版结果（boll_pf_v2）：等 VOO 周线再集中', '',
           '口径同第一版：2014-02-28 至 2024-06-28，零成本、小数股、不持有现金。', '', '## 七条线', '',
           '| 线 | 年化 | 比 VOO 多 | 最大回撤 | 峰值 | 谷底 | 恢复 | 最长水下交易日 | 2020 内回撤 | 2022 内回撤 | 最差年 | 最差年收益 | 年换手 |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for name in LINES:
        r = rows[name]
        out.append(f"| {name} | {pct(r['cagr'])} | {pct(r['excess_cagr'])} | {pct(r['max_drawdown'])} | "
                   f"{r['peak_date']} | {r['trough_date']} | {r['recovery_date'] or '—'} | "
                   f"{r['longest_underwater_sessions']} | {pct(r['max_drawdown_2020'])} | "
                   f"{pct(r['max_drawdown_2022'])} | {r['worst_year']} | {pct(r['worst_year_return'])} | "
                   f"{r['annual_turnover']:.2f} |")
    years = sorted(result['yearly']['returns']['VOO'])
    out += ['', '## 逐年比 VOO 多出的收益', '', '| 年 | ' + ' | '.join(LINES[1:]) + ' |',
            '|---|' + '---|' * (len(LINES) - 1)]
    for year in years:
        out.append(f'| {year} | ' + ' | '.join(pct(result['yearly']['excess'][n].get(year)) for n in LINES[1:]) + ' |')
    out += ['', '## 每段关注区：准备与开始集中', '', '| 关注区开始 | 结束 | 周数 | TILT_C 开始集中 | 等了几周 |',
            '|---|---|---|---|---|']
    for e in episodes:
        out.append(f"| {e['start']} | {e['end']} | {e['weeks']} | {e['trigger'] or '—'} | "
                   f"{'—' if e['wait'] is None else e['wait']} |")
    for name in ('TILT_B', 'TILT_C'):
        out += ['', f'## 逐段：{name}', '', '| 开始 | 结束 | 周数 | 集中期收益 | 同期 EW10 | 比 EW10 多 | 集中期间持有 |',
                '|---|---|---|---|---|---|---|']
        for s in result['segments'][name]:
            out.append(f"| {s['start']} | {s['end']} | {s['weeks']} | {pct(s['tilt_return'])} | "
                       f"{pct(s['ew10_return'])} | {pct(s['excess'])} | {', '.join(s['holdings'])} |")
    out += ['', '## 核对', '', f"- 第一版六条线年化与 research/boll_pf_v1/summary.json 相同：{all(same.values())}。",
            f"- TILT_C 集中 {summary['tilt_c_weeks']} 周。"]
    (HERE / 'report.md').write_text('\n'.join(out) + '\n', encoding='utf8', newline='\n')
    for name in LINES:
        r = rows[name]
        print(f"{name:7} cagr {r['cagr']*100:6.2f}%  excess {r['excess_cagr']*100:6.2f}%  "
              f"dd {r['max_drawdown']*100:6.1f}%  dd2022 {r['max_drawdown_2022']*100:6.1f}%  "
              f"worst {r['worst_year']} {r['worst_year_return']*100:6.1f}%")
    print('v1 unchanged', same)
    for e in episodes:
        print(e)


if __name__ == '__main__':
    main()
