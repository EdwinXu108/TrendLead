"""Write research/pool_ma_v1/report.md from summary.json and the week checks. Numbers only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = ROOT / 'work' / 'pool_ma_v1'

ROWS = (('long_full', '看多满仓'), ('long_plus_cash', '看多 + 现金'), ('all_equal', '全部等权'))


def pct(value, digits=1, sign=False):
    if value is None:
        return '—'
    return f'{value * 100:+.{digits}f}%' if sign else f'{value * 100:.{digits}f}%'


def num(value, digits=2):
    return '—' if value is None else f'{value:.{digits}f}'


def main():
    summary = json.loads((HERE / 'summary.json').read_text(encoding='utf8'))
    checks = json.loads((WORK / 'checks.json').read_text(encoding='utf8')) if (WORK / 'checks.json').exists() else None
    portfolios, random = summary['portfolios'], summary['random']
    voo = portfolios['voo']

    def row(name, data, extra=None):
        cells = [pct(data['cagr']), pct(data['excess_vs_voo'], 1, True) if 'excess_vs_voo' in data
                 else pct(data['cagr'] - voo['cagr'], 1, True), pct(data['max_drawdown']),
                 data.get('trough_date', '—'), str(data['longest_underwater_sessions']),
                 pct(data['max_drawdown_2020']), pct(data['max_drawdown_2022']),
                 pct(data['worst_year']['return_']) if 'worst_year' in data else extra, num(data['cagr_over_dd'])]
        return '| ' + name + ' | ' + ' | '.join(cells) + ' |'

    lines = ['# pool_ma_v1：按当时名单的组合均线实验', '',
             f"- 时段 {summary['config']['start']} 至 {summary['config']['end']}，每周最后一个交易日收盘调仓，"
             f"零成本、小数股、本金 100,000 美元、现金不计息；固定 SMA 50/200。",
             f"- 共 {len(portfolios['all_equal']['annual_returns'])} 个自然年；"
             f"生成 {summary['config']['built']}；耗时 {summary['elapsed_seconds']} 秒。", '',
             '## 1. 五条线与 VOO', '',
             '| 组合 | 年化 | 比 VOO 多（少） | 最大回撤 | 谷底 | 最长水下（交易日） | 2020 年内回撤 | 2022 年内回撤 | 最差自然年 | 收益÷回撤 |',
             '|---|---|---|---|---|---|---|---|---|---|']
    for key, label in ROWS:
        lines.append(row(label, portfolios[key]))
    median = random['metrics']
    lines.append('| 随机同数量（中位） | ' + ' | '.join([
        pct(median['cagr']['median']), pct(median['cagr']['median'] - voo['cagr'], 1, True),
        pct(median['max_drawdown']['median']), '—',
        num(median['longest_underwater_sessions']['median'], 0),
        '—', '—', pct(median['worst_year_return']['median']),
        num(median['cagr_over_dd']['median'])]) + ' |')
    lines.append('| 随机同数量（5%） | ' + ' | '.join([
        pct(random['metrics']['cagr']['p05']), pct(random['metrics']['cagr']['p05'] - voo['cagr'], 1, True),
        pct(random['metrics']['max_drawdown']['p05']), '—',
        num(random['metrics']['longest_underwater_sessions']['p05'], 0),
        '—', '—', pct(random['metrics']['worst_year_return']['p05']),
        num(random['metrics']['cagr_over_dd']['p05'])]) + ' |')
    lines.append('| 随机同数量（95%） | ' + ' | '.join([
        pct(random['metrics']['cagr']['p95']), pct(random['metrics']['cagr']['p95'] - voo['cagr'], 1, True),
        pct(random['metrics']['max_drawdown']['p95']), '—',
        num(random['metrics']['longest_underwater_sessions']['p95'], 0),
        '—', '—', pct(random['metrics']['worst_year_return']['p95']),
        num(random['metrics']['cagr_over_dd']['p95'])]) + ' |')
    lines.append(row('VOO', voo))

    lines += ['', '随机线的谷底日期与年度回撤不报分位，只报上表的指标。', '',
              '## 2. 看多满仓胜过多少条随机线（满分 100）', '',
              '| 指标 | 胜过 |', '|---|---|',
              f"| 年化 | {random['beats']['cagr']} |",
              f"| 最大回撤 | {random['beats']['max_drawdown']} |",
              f"| 最长水下 | {random['beats']['longest_underwater_sessions']} |",
              f"| 收益÷回撤 | {random['beats']['cagr_over_dd']} |", '',
              '## 3. 逐年收益', '',
              '| 年份 | 看多满仓 | 看多 + 现金 | 全部等权 | VOO | 随机中位 |', '|---|---|---|---|---|---|']
    years = sorted(portfolios['all_equal']['annual_returns'])
    for year in years:
        cells = [pct(portfolios[key]['annual_returns'].get(year)) for key, _ in ROWS]
        cells.append(pct(voo['annual_returns'].get(year)))
        cells.append(pct(random['annual'].get(year, {}).get('median')))
        lines.append(f'| {year} | ' + ' | '.join(cells) + ' |')

    diag = summary['diagnostics']
    lines += ['', '## 4. 诊断', '',
              f"- 池子只数 M：均值 {diag['M']['mean']:.1f}，最小 {diag['M']['min']}，最大 {diag['M']['max']}。",
              f"- 看多只数 K：均值 {diag['K']['mean']:.1f}，最小 {diag['K']['min']}，最大 {diag['K']['max']}；"
              f"K = 0 的周数 {diag['k_zero_weeks']}。",
              f"- 池版本 {diag['pool_versions']} 个，切换 {diag['version_switches']} 次，切换时掉出池的"
              f"「证券 × 次」合计 {diag['dropped_names']}。",
              f"- 信号日没有 `tri` 的「证券 × 日」：{diag['missing_tri_cells']}（涉及 "
              f"{diag['missing_tri_securities']} 只证券）。",
              f"- 持有中 `tri` 提前结束的「证券 × 周」：{diag['frozen_holding_periods']}。", '']

    if checks:
        lines += ['## 5. 核对', '',
                  '### 5.1 三个信号周的手工重算', '',
                  '| 周 | 组合 | 手工 | 模块 | 差 | K | M |', '|---|---|---|---|---|---|---|']
        for item in checks['weeks']:
            lines.append(f"| {item['week']} | {item['portfolio']} | {item['hand']:+.8f} | {item['module']:+.8f} | "
                         f"{item['difference']:.2e} | {item['K']} | {item['M']} |")
        lines += ['', f"最大差 {checks['worst_difference']:.2e}。", '',
                  '### 5.2 与引擎最接近的配置对照', '',
                  '| 口径 | 年化 | 最大回撤 | 相对 VOO |', '|---|---|---|---|',
                  f"| 引擎 signal layer，N=200，每周等权 | {pct(checks['proxy']['cagr'], 3)} | "
                  f"{pct(checks['proxy']['max_drawdown'], 3)} | {pct(checks['proxy']['excess'], 3, True)} |",
                  f"| 全部等权（本实验） | {pct(checks['all_equal']['cagr'], 3)} | "
                  f"{pct(checks['all_equal']['max_drawdown'], 3)} | {pct(checks['all_equal']['excess'], 3, True)} |",
                  '', '两者差在：引擎按金额取前 N=200 家、池子不足 200 时全持，权重与再平衡由引擎的 R1W 处理；'
                  '本实验对当天的全部代表证券取 1/M。池子只数均值 ' f"{diag['M']['mean']:.1f}，两者持股几乎相同。", '']

    (HERE / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf8', newline='\n')
    print('wrote report.md')


if __name__ == '__main__':
    main()
