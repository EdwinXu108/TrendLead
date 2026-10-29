"""Run boll_pf_v3 and write summary.json and report.md.

    python -X utf8 research/boll_pf_v3/run_pf.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import boll_pf, pick_pf  # noqa: E402

HERE = Path(__file__).resolve().parent
LINES = ('VOO',) + tuple(pick_pf.LINES)
FIELDS = ('cagr', 'excess_cagr', 'max_drawdown', 'peak_date', 'trough_date', 'recovery_date',
          'longest_underwater_sessions', 'max_drawdown_2020', 'max_drawdown_2022', 'annual_turnover',
          'average_holdings', 'total_return', 'volatility')
SAMPLE = '2022-10-14'


def pct(value):
    return '—' if value is None else f'{value * 100:.2f}%'


def main():
    result = pick_pf.run()
    rows = {}
    for name in LINES:
        row = result['metrics'][name]
        rows[name] = {key: row[key] for key in FIELDS}
        rows[name].update(worst_year=row['worst_year']['year'], worst_year_return=row['worst_year']['return_'])
    v1 = json.loads((ROOT / 'research' / 'boll_pf_v1' / 'summary.json').read_text(encoding='utf8'))['lines']
    check = {mine: (rows[mine]['cagr'], v1[name]['cagr'])
             for mine, name in (('VOO', 'VOO'), ('EW10', 'EW10'), ('TILT_B*', 'TILT_B'))}
    book, names = result['book'], boll_pf.tickers()
    j = book['weeks'].index(SAMPLE)
    sample = {rule: [names[book['market'].sids[c]] for c in result['picks'][rule][j]]
              for rule in ('WGT5', 'WB5', 'VOL10', 'BETA5')}
    summary = dict(lines=rows, yearly=result['yearly'], v1_check=check, sample={SAMPLE: sample},
                   concentrated_weeks=sum(result['state']))
    (HERE / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float),
                                       encoding='utf8', newline='\n')
    out = ['# 组合第三版结果（boll_pf_v3）：按 pick_v1 换选股规则', '',
           '口径同第一版：2014-02-28 至 2024-06-28，零成本、小数股、不持有现金；贝塔、波动用 52 周。', '', '## 各线', '',
           '| 线 | 年化 | 比 VOO 多 | 最大回撤 | 峰值 | 谷底 | 恢复 | 最长水下交易日 | 2020 内回撤 | 2022 内回撤 '
           '| 最差年 | 最差年收益 | 年换手 | 平均只数 |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for name in LINES:
        r = rows[name]
        out.append(f"| {name} | {pct(r['cagr'])} | {pct(r['excess_cagr'])} | {pct(r['max_drawdown'])} | "
                   f"{r['peak_date']} | {r['trough_date']} | {r['recovery_date'] or '—'} | "
                   f"{r['longest_underwater_sessions']} | {pct(r['max_drawdown_2020'])} | "
                   f"{pct(r['max_drawdown_2022'])} | {r['worst_year']} | {pct(r['worst_year_return'])} | "
                   f"{r['annual_turnover']:.2f} | {r['average_holdings']:.1f} |")
    years = sorted(result['yearly']['returns']['VOO'])
    out += ['', '## 逐年比 VOO 多出的收益', '', '| 年 | ' + ' | '.join(LINES[1:]) + ' |',
            '|---|' + '---|' * (len(LINES) - 1)]
    for year in years:
        out.append(f'| {year} | ' + ' | '.join(pct(result['yearly']['excess'][n].get(year)) for n in LINES[1:]) + ' |')
    out += ['', '## 核对', '']
    for mine, (a, b) in check.items():
        out.append(f'- {mine}：本版 {pct(a)}，第一版 {pct(b)}。')
    out.append(f"- 集中状态 {summary['concentrated_weeks']} 周。")
    for rule, held in sample.items():
        out.append(f'- {SAMPLE} {rule}：{", ".join(held)}。')
    (HERE / 'report.md').write_text('\n'.join(out) + '\n', encoding='utf8', newline='\n')
    for name in LINES:
        r = rows[name]
        print(f"{name:9} cagr {r['cagr']*100:6.2f}%  excess {r['excess_cagr']*100:6.2f}%  "
              f"dd {r['max_drawdown']*100:6.1f}%  dd2020 {r['max_drawdown_2020']*100:6.1f}%  "
              f"dd2022 {r['max_drawdown_2022']*100:6.1f}%  worst {r['worst_year']} "
              f"{r['worst_year_return']*100:6.1f}%  turn {r['annual_turnover']:.2f}")
    print(check)
    print(sample)


if __name__ == '__main__':
    main()
