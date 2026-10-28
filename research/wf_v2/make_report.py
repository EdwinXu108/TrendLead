"""Write report_dev.md and report_holdout.md from the cached wf_v2 results. Numbers only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import ma_walk2     # noqa: E402

LINES = (('a', 'A 本股挑选'), ('b', 'B 共同参数'), ('c', 'C 借参数'), ('d', 'D 固定 50/200'),
         ('e', 'E 一直持有'), ('f', 'F 同仓位'))
CONTROLS = (('b', 'B 共同参数'), ('c', 'C 借参数'), ('d', 'D 固定 50/200'), ('e', 'E 一直持有'),
            ('f', 'F 同仓位'))
FIELDS = (('cagr', '年化差'), ('max_drawdown', '回撤差'), ('cagr_over_dd', '收益÷回撤差'))
PERIODS = ('2024H2', '2025', '2026H1')


def pct(value, digits=1, sign=False):
    if value is None:
        return '—'
    return (f'{value * 100:+.{digits}f}%' if sign else f'{value * 100:.{digits}f}%')


def num(value, digits=2, sign=False):
    if value is None:
        return '—'
    return f'{value:+.{digits}f}' if sign else f'{value:.{digits}f}'


def combo_label(index: int) -> str:
    kind, fast, slow = ma_walk2.COMBOS[int(index)]
    return f'{kind.upper()} {fast}/{slow}'


def line_table(summary: dict) -> list[str]:
    out = ['| 线 | 年化 | 最大回撤 | 收益÷回撤 | 夏普 |', '|---|---|---|---|---|']
    for key, label in LINES:
        row = summary['lines'][key]
        out.append(f"| {label} | {pct(row['cagr'])} | {pct(row['max_drawdown'])} | "
                   f"{num(row['cagr_over_dd'])} | {num(row['sharpe'])} |")
    return out


def pair_tables(summary: dict, title: str) -> list[str]:
    out = [f'### {title}', '',
           '| 对照 | 差值 | 中位 | 25% | 75% | 胜 | 负 | 平 | 不可比 |', '|---|---|---|---|---|---|---|---|---|']
    for key, label in CONTROLS:
        for field, name in FIELDS:
            row = summary['pairs'][key][field]
            digits = 2 if field == 'cagr_over_dd' else 1
            unit = (lambda v: num(v, digits, True)) if field == 'cagr_over_dd' else (lambda v: pct(v, digits, True))
            out.append(f"| {label} | {name} | {unit(row['median'])} | {unit(row['q25'])} | {unit(row['q75'])} | "
                       f"{row['win']} | {row['loss']} | {row['tie']} | {row['incomparable']} |")
    return out


def msft_block(stocks: list[dict]) -> list[str]:
    stock = next((item for item in stocks if item['ticker'] == 'MSFT'), None)
    if stock is None:
        return ['（没有 MSFT）']
    out = ['| 年份 | 可参选组合 | A 本股挑选 | B 共同参数 | C 借参数（种子 0） |',
           '|---|---|---|---|---|']
    for pick in stock['picks']:
        year = pick['year']
        b_combo = stock['b'].get(str(year), stock['b'].get(year))
        c_combo = stock['c']['0'].get(str(year), stock['c']['0'].get(year))
        out.append(f"| {year} | {stock['eligible_by_year'].get(str(year), stock['eligible_by_year'].get(year))} | "
                   f"{pick['type'].upper()} {pick['fast']}/{pick['slow']} | "
                   f"{combo_label(b_combo) if b_combo is not None else '—'} | "
                   f"{combo_label(c_combo) if c_combo is not None else '—'} |")
    out += ['', '| 线 | 年化 | 最大回撤 | 收益÷回撤 | 夏普 | 在场 | 买入 |', '|---|---|---|---|---|---|---|']
    for key, label in LINES:
        row = stock['lines'][key]
        out.append(f"| {label} | {pct(row['cagr'])} | {pct(row['max_drawdown'])} | {num(row['cagr_over_dd'])} | "
                   f"{num(row['sharpe'])} | {pct(row['invested_share'], 0)} | {row['buys']} |")
    empties = '；'.join(f"{label} 尚无信号 {stock['empty'][key]['no_signal']}、有效为空 "
                        f"{stock['empty'][key]['flat_signal']}" for key, label in LINES
                        if key in stock['empty'] and stock['empty'][key])
    out += ['', f"评估 {stock['evaluation_start']} 至 {stock['evaluation_end']}；"
                f"F 的仓位比例 {pct(stock['mix_share'], 1)}；{empties}。"]
    return out


def never_block(payload: dict) -> list[str]:
    info = payload['never_eligible']
    counts = info['counts']
    combos = info['combos']
    by_type = {}
    for combo in combos:
        by_type.setdefault(combo['type'], []).append(f"{combo['fast']}/{combo['slow']}")
    out = [f"- 每股永远不参选的组合数：{counts}（所有股票相同）。共 {len(combos)} 组："]
    for kind in sorted(by_type):
        out.append(f"  - {kind.upper()}：{', '.join(by_type[kind])}")
    return out


def dev_report(payload: dict) -> str:
    lines = ['# wf_v2 开发段结果', '',
             f"- 股票 {payload['summary']['all']['stocks']} 只（评估从 2016 年开始 "
             f"{payload['summary']['from2016']['stocks']} 只），跳过 {len(payload['skipped'])} 只；"
             f"网格 {payload['combos']} 组；C 种子 {payload['seeds']} 个；耗时 {payload['elapsed_seconds']} 秒。",
             f"- 口径：每日信号、现金不计息；S = 2014-02-28 或上市后第 301 个交易日；回看 ≥ "
             f"{ma_walk2.MIN_HISTORY} 个交易日；生成 {payload['built']}。", '']
    lines += ['## 1. 六条线的中位数', '', '### 评估从 2016 年开始', '']
    lines += line_table(payload['summary']['from2016'])
    lines += ['', '### 全部股票', '']
    lines += line_table(payload['summary']['all'])
    lines += ['', '## 2. 逐股配对（A − 对照）', '']
    lines += pair_tables(payload['summary']['from2016'], '评估从 2016 年开始')
    lines += ['']
    lines += pair_tables(payload['summary']['all'], '全部股票')
    lines += ['', '## 3. MSFT', '']
    lines += msft_block(payload['stocks'])
    lines += ['', '## 4. 稳定性', '',
              '- 被挑中的均线类型次数：' +
              '，'.join(f'{k.upper()} {v}' for k, v in payload['summary']['type_counts'].items()),
              '- 组合更换次数（每股不同组合数 − 1）：' +
              '，'.join(f'{k} 次 {v} 只' for k, v in payload['summary']['change_distribution'].items()),
              '']
    lines += never_block(payload)
    lines.append('')
    return '\n'.join(lines)


def holdout_report(payload: dict, dev: dict) -> str:
    lines = ['# wf_v2 保留段结果', '',
             f"- 窗口 2024-06-28 收盘 = 1 至 2026-06-30；股票 {payload['summary']['stocks']} 只；"
             f"冻结于 2024-06-28；跳过 {len(payload['skipped'])} 只；耗时 {payload['elapsed_seconds']} 秒。",
             f"- 共同参数：{payload['common']['type'].upper()} {payload['common']['fast']}/"
             f"{payload['common']['slow']}（回看中位 {num(payload['common']['median'])}，覆盖 "
             f"{pct(payload['common']['coverage'], 1)}）。", '']
    lines += ['## 1. 六条线的中位数', '']
    lines += line_table(payload['summary'])
    lines += ['', '## 2. 逐股配对（A − 对照）', '']
    lines += pair_tables(payload['summary'], '保留段全部股票')
    lines += ['', '## 3. 开发段对比保留段（中位数并列）', '',
              '| 线 | 开发段 年化 | 保留段 年化 | 开发段 回撤 | 保留段 回撤 | 开发段 收益÷回撤 | 保留段 收益÷回撤 |',
              '|---|---|---|---|---|---|---|']
    dev_lines = dev['summary']['from2016']['lines']
    for key, label in LINES:
        lines.append(f"| {label} | {pct(dev_lines[key]['cagr'])} | {pct(payload['summary']['lines'][key]['cagr'])} | "
                     f"{pct(dev_lines[key]['max_drawdown'])} | {pct(payload['summary']['lines'][key]['max_drawdown'])} | "
                     f"{num(dev_lines[key]['cagr_over_dd'])} | {num(payload['summary']['lines'][key]['cagr_over_dd'])} |")
    lines += ['', '## 4. 分段收益（各股中位数）', '',
              '| 线 | 2024 下半年 | 2025 | 2026 上半年 |', '|---|---|---|---|']
    for key, label in LINES:
        cells = []
        for period in PERIODS:
            values = []
            for stock in payload['stocks']:
                for item in stock['splits'][key]:
                    if item['period'] == period and item['value'] is not None:
                        values.append(item['value'])
            cells.append(pct(sorted(values)[len(values) // 2]) if values else '—')
        lines.append(f"| {label} | " + ' | '.join(cells) + ' |')
    lines += ['', '## 5. MSFT', '',
              '| 线 | 年化 | 最大回撤 | 收益÷回撤 | 夏普 | 在场 | 买入 |', '|---|---|---|---|---|---|---|']
    stock = next((item for item in payload['stocks'] if item['ticker'] == 'MSFT'), None)
    if stock:
        for key, label in LINES:
            row = stock['lines'][key]
            lines.append(f"| {label} | {pct(row['cagr'])} | {pct(row['max_drawdown'])} | {num(row['cagr_over_dd'])} | "
                         f"{num(row['sharpe'])} | {pct(row['invested_share'], 0)} | {row['buys']} |")
        lines += ['', f"评估 {stock['evaluation_start']} 至 {stock['evaluation_end']}；"
                      f"F 的仓位比例 {pct(stock['mix_share'], 1)}。"]
    lines.append('')
    return '\n'.join(lines)


def main():
    dev_path = HERE / 'wf2_dev.json'
    dev = json.loads(dev_path.read_text(encoding='utf8'))
    (HERE / 'report_dev.md').write_text(dev_report(dev), encoding='utf8', newline='\n')
    print('wrote report_dev.md')
    holdout_path = HERE / 'wf2_holdout.json'
    if holdout_path.exists():
        holdout = json.loads(holdout_path.read_text(encoding='utf8'))
        (HERE / 'report_holdout.md').write_text(holdout_report(holdout, dev), encoding='utf8', newline='\n')
        print('wrote report_holdout.md')


if __name__ == '__main__':
    main()
