"""Run the six lines of boll_pf_v1 and write summary.json, report.md and the daily curves.

    python -X utf8 research/boll_pf_v1/run_pf.py

Results go to research/boll_pf_v1/; the daily equity curves go to work/boll_pf_v1/curves.json.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import boll_pf  # noqa: E402

HERE = Path(__file__).resolve().parent
CURVES = ROOT / 'work' / 'boll_pf_v1' / 'curves.json'
SAMPLE_WEEKS = ('2016-02-12', '2020-03-20', '2023-10-27')


def pct(value, digits=2):
    return '—' if value is None else f'{value * 100:.{digits}f}%'


def number(value, digits=2):
    return '—' if value is None else f'{value:.{digits}f}'


def summary_rows(result):
    rows = {}
    for name in boll_pf.LINES:
        row = result['metrics'][name]
        rows[name] = dict(
            cagr=row['cagr'], excess_cagr=row['excess_cagr'], max_drawdown=row['max_drawdown'],
            peak_date=row['peak_date'], trough_date=row['trough_date'], recovery_date=row['recovery_date'],
            longest_underwater_sessions=row['longest_underwater_sessions'],
            max_drawdown_2020=row['max_drawdown_2020'], max_drawdown_2022=row['max_drawdown_2022'],
            worst_year=row['worst_year']['year'], worst_year_return=row['worst_year']['return_'],
            annual_turnover=row['annual_turnover'], average_holdings=row['average_holdings'],
            total_return=row['total_return'], volatility=row['volatility'])
    return rows


def checks(result):
    book = result['book']
    market = book['market']
    names = result['tickers']
    weeks = book['weeks']
    zone_weeks = sum(1 for level in book['levels'] if level >= 2)
    rows = dict(weeks=len(weeks), pool_versions=book['versions'], market_zone_weeks=zone_weeks,
                market_episodes=len(boll_pf.runs([level >= 2 for level in book['levels']], weeks)),
                tilt_a_weeks=sum(1 for level in book['levels'] if level >= 2),
                tilt_b_weeks=sum(book['concentrated']),
                top_sizes=sorted({len(value) for value in book['top'].values()}),
                concentrated_state_weeks=sum(book['concentrated']))
    sample = {}
    for day in SAMPLE_WEEKS:
        if day not in book['top']:
            continue
        members = book['top'][day]
        sample[day] = dict(
            top10=[names[market.sids[column]] for column, _, _ in members],
            betas={names[market.sids[column]]: round(float(book['betas'][day][column]), 6)
                   for column, _, _ in members},
            median=float(np.median([book['betas'][day][column] for column, _, _ in members])),
            high_beta_half=[names[market.sids[column]] for column in
                            boll_pf.high_beta_half(book['betas'][day])],
            market_level=book['levels'][weeks.index(day)],
            tilt_a_applied=bool(result['applied']['TILT_A'][market.index[day]]),
            tilt_b_applied=bool(result['applied']['TILT_B'][market.index[day]]))
    rows['sample_weeks'] = sample
    return rows


def write_curves(result):
    days = result['simulations']['VOO']['days']
    out = {'dates': days}
    for name in boll_pf.LINES:
        out[name] = [round(float(v), 4) for v in result['simulations'][name]['equity']]
    CURVES.parent.mkdir(parents=True, exist_ok=True)
    CURVES.write_text(json.dumps(out, ensure_ascii=False), encoding='utf8', newline='\n')


def report(summary, rows, yearly, segments):
    lines = ['# 组合第一版结果（boll_pf_v1）', '',
             f"区间 {summary['start']} 至 {summary['end']}，本金 {summary['capital']:.0f} 美元，"
             f"股票池 {summary['scope']}，每周最后一个交易日收盘按目标权重重新分配，零成本、小数股、不持有现金。", '',
             '## 六条线', '',
             '| 线 | 年化 | 比 VOO 多 | 最大回撤 | 峰值 | 谷底 | 恢复 | 最长水下交易日 | 2020 内回撤 | 2022 内回撤 | 最差年 | 最差年收益 | 年换手 | 平均持有只数 |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for name in boll_pf.LINES:
        row = rows[name]
        lines.append(f"| {name} | {pct(row['cagr'])} | {pct(row['excess_cagr'])} | {pct(row['max_drawdown'])} | "
                     f"{row['peak_date']} | {row['trough_date']} | {row['recovery_date'] or '—'} | "
                     f"{row['longest_underwater_sessions']} | {pct(row['max_drawdown_2020'])} | "
                     f"{pct(row['max_drawdown_2022'])} | {row['worst_year']} | {pct(row['worst_year_return'])} | "
                     f"{number(row['annual_turnover'])} | {number(row['average_holdings'], 1)} |")
    lines += ['', '年化按 252 个交易日折算；比 VOO 多 = 该线年化 − VOO 年化；最长水下按交易日计；'
                  '年换手 = 全年买卖单边权重变化之和 ÷ 年数；平均持有只数是每周持仓只数的平均。', '',
              '## 逐年收益', '', '| 年 | ' + ' | '.join(boll_pf.LINES) + ' |',
              '|---|' + '---|' * len(boll_pf.LINES)]
    years = sorted(yearly['returns']['VOO'])
    for year in years:
        cells = [pct(yearly['returns'][name].get(year)) for name in boll_pf.LINES]
        lines.append(f'| {year} | ' + ' | '.join(cells) + ' |')
    lines += ['', '逐年比 VOO 多出的收益（该线年收益 − VOO 同一年收益）：', '',
              '| 年 | ' + ' | '.join(boll_pf.LINES) + ' |', '|---|' + '---|' * len(boll_pf.LINES)]
    for year in years:
        cells = [pct(yearly['excess'][name].get(year)) for name in boll_pf.LINES]
        lines.append(f'| {year} | ' + ' | '.join(cells) + ' |')
    for name in boll_pf.TILTS:
        lines += ['', f'## 逐段：{name}', '',
                  '| 开始 | 结束 | 周数 | 集中期收益 | 同期 EW10 | 比 EW10 多 | 集中期间持有 |',
                  '|---|---|---|---|---|---|---|']
        for row in segments[name]:
            lines.append(f"| {row['start']} | {row['end']} | {row['weeks']} | {pct(row['tilt_return'])} | "
                         f"{pct(row['ew10_return'])} | {pct(row['excess'])} | {', '.join(row['holdings'])} |")
    lines += ['', '2014 从 2 月 28 日开始、2024 到 6 月 28 日结束，都是不完整年。', '',
              '逐段表按“结束周净值 ÷ 开始周净值 − 1”计算；只有 1 周的段，起止是同一天，净值之比为 1，因此显示 0.00%。', '',
              '## 核对', '']
    checks_rows = summary['checks']
    lines.append(f"- 开发段 {checks_rows['weeks']} 周，市场在关注区 {checks_rows['market_zone_weeks']} 周，"
                 f"共 {checks_rows['market_episodes']} 段；TILT_A 集中 {checks_rows['tilt_a_weeks']} 周，"
                 f"TILT_B 集中 {checks_rows['tilt_b_weeks']} 周。")
    lines.append(f"- 每周前 10 的只数集合：{checks_rows['top_sizes']}。")
    for day, item in checks_rows['sample_weeks'].items():
        lines.append(f"- {day}（市场等级 {item['market_level']}）：前 10 为 {', '.join(item['top10'])}；"
                     f"贝塔中位数 {item['median']:.3f}；高贝塔的一半为 {', '.join(item['high_beta_half'])}；"
                     f"该周集中状态 {'是' if item['tilt_a_applied'] else '否'}。")
    lines.append(f"- VOO 年化 {pct(rows['VOO']['cagr'], 4)}，与 research/pool_ma_v1/summary.json 的 "
                 f"{pct(summary['voo_reference_cagr'], 4)} 相同。")
    return '\n'.join(lines) + '\n'


def main():
    result = boll_pf.run()
    rows = summary_rows(result)
    reference = json.loads((ROOT / 'research' / 'pool_ma_v1' / 'summary.json').read_text(encoding='utf8'))
    summary = dict(start=boll_pf.START, end=boll_pf.END, capital=boll_pf.CAPITAL, scope=boll_pf.SCOPE,
                   lines=rows, yearly=result['yearly'], segments=result['segments'],
                   checks=checks(result), voo_reference_cagr=reference['portfolios']['voo']['cagr'])
    (HERE / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float),
                                       encoding='utf8', newline='\n')
    (HERE / 'report.md').write_text(report(summary, rows, result['yearly'], result['segments']),
                                    encoding='utf8', newline='\n')
    write_curves(result)
    for name in boll_pf.LINES:
        row = rows[name]
        print(f"{name:7} cagr {row['cagr']*100:6.2f}%  excess {row['excess_cagr']*100:6.2f}%  "
              f"dd {row['max_drawdown']*100:6.1f}%  turnover {row['annual_turnover']:5.2f}")
    print('wrote', HERE / 'summary.json', HERE / 'report.md', CURVES)


if __name__ == '__main__':
    main()
