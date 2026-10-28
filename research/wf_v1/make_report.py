"""Write research/wf_v1/report.md from the four cached result files. Numbers only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIGS = (('daily', 'zero'), ('daily', 'dtb3'), ('weekly', 'zero'), ('weekly', 'dtb3'))
LINES = (('wf', '滚动验证'), ('hold', '一直持有'), ('fixed', '固定 SMA 50/200'), ('ex_post', '事后最优'))


def pct(value, digits=1):
    return '—' if value is None else f'{value * 100:+.{digits}f}%'


def pct0(value, digits=1):
    return '—' if value is None else f'{value * 100:.{digits}f}%'


def num(value, digits=2):
    return '—' if value is None else f'{value:.{digits}f}'


def summary_table(payload: dict) -> list[str]:
    complete, everything = payload['summary']['complete'], payload['summary']['all']
    rows = [
        ('股票数', lambda s: str(s['stocks'])),
        ('WF 胜过固定 50/200（收益÷回撤）',
         lambda s: f"{s['beats_fixed']}（{pct0(s['beats_fixed'] / s['stocks'])}）"),
        ('WF 胜过一直持有（收益÷回撤）',
         lambda s: f"{s['beats_hold_ratio']}（{pct0(s['beats_hold_ratio'] / s['stocks'])}）"),
        ('WF 胜过一直持有（年化）',
         lambda s: f"{s['beats_hold_cagr']}（{pct0(s['beats_hold_cagr'] / s['stocks'])}）"),
        ('中位数 年化：WF / 持有 / 50-200',
         lambda s: ' / '.join(pct0(s['medians'][k]['cagr']) for k in ('wf', 'hold', 'fixed'))),
        ('中位数 最大回撤：WF / 持有 / 50-200',
         lambda s: ' / '.join(pct(s['medians'][k]['max_drawdown']) for k in ('wf', 'hold', 'fixed'))),
        ('中位数 收益÷回撤：WF / 持有 / 50-200',
         lambda s: ' / '.join(num(s['medians'][k]['cagr_over_dd']) for k in ('wf', 'hold', 'fixed'))),
        ('中位数 夏普：WF / 持有 / 50-200',
         lambda s: ' / '.join(num(s['medians'][k]['sharpe']) for k in ('wf', 'hold', 'fixed'))),
        ('事后最优 − WF 的收益÷回撤（中位数）', lambda s: num(s['median_ex_post_gap'])),
    ]
    out = ['| 指标 | 完整期 | 全部 |', '|---|---|---|']
    for title, fn in rows:
        out.append(f'| {title} | {fn(complete)} | {fn(everything)} |')
    return out


def distribution(counts: dict, label: str) -> str:
    parts = [f'{key} 次：{value} 只' for key, value in sorted(counts.items(), key=lambda kv: int(kv[0]))]
    return f'- {label}：' + '，'.join(parts)


def msft_tables(payload: dict) -> list[str]:
    stock = next((s for s in payload['stocks'] if s['ticker'] == 'MSFT'), None)
    if stock is None:
        return ['（没有 MSFT）']
    out = ['| 线 | 年化 | 总收益 | 年化波动 | 最大回撤 | 收益÷回撤 | 夏普 | 在场 | 买入 |',
           '|---|---|---|---|---|---|---|---|---|']
    for key, label in LINES:
        row = stock['lines'].get(key, {})
        if not row or row.get('cagr') is None:
            out.append(f'| {label} | — | — | — | — | — | — | — | — |')
            continue
        out.append(f"| {label} | {pct0(row['cagr'])} | {pct(row['total_return'], 0)} | "
                   f"{pct0(row['volatility'])} | {pct(row['max_drawdown'])} | {num(row['cagr_over_dd'])} | "
                   f"{num(row['sharpe'])} | {pct0(row['invested_share'], 0)} | {row['buys']} |")
    out += ['', f"评估区间 {stock['evaluation_start']} 至 {stock['evaluation_end']}，"
                f"共 {stock['evaluation_sessions']} 个交易日；事后最优 "
                f"{stock['ex_post']['type'].upper()} {stock['ex_post']['fast']}/{stock['ex_post']['slow']}。",
            '', '| 挑选日 | 挑中的组合 | 回看 收益÷回撤 | 下一年 WF | 下一年 持有 | 下一年 50/200 |',
            '|---|---|---|---|---|---|']
    for row in stock['selections']:
        period = row['period']
        cells = []
        for key in ('wf', 'hold', 'fixed'):
            item = period[key]
            cells.append(f"{pct(item['ret'])} / {pct(item['max_drawdown'])}")
        out.append(f"| {row['date']} | {row['type'].upper()} {row['fast']}/{row['slow']} | "
                   f"{num(row['lookback_cagr_over_dd'])} | " + ' | '.join(cells) + ' |')
    out += ['', f"8 次挑选里换了 {stock['stability']['changes']} 次组合；类型分布 " +
            '、'.join(f'{k.upper()}×{v}' for k, v in sorted(stock['stability']['types'].items())) + '。']
    return out


def main():
    lines = ['# wf_v1：均线滚动验证结果', '',
             '每年最后一个信号日收盘，只用之前的数据，从 462 组里按 年化÷|回撤| 挑一组，下一年用它；'
             '与一直持有、固定 SMA 50/200 比较。事后最优是同一评估区间上直接挑出的最好一组，只作参照。',
             '', '单元格「收益 / 区间内最大回撤」。', '']
    for frequency, cash in CONFIGS:
        path = HERE / f'wf_{frequency}_{cash}.json'
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding='utf8'))
        title = f"{'每日' if frequency == 'daily' else '每周'} · 现金 {'0 收益' if cash == 'zero' else '国债'}"
        lines += [f'## {title}', '',
                  f"- 股票：{payload['summary']['all']['stocks']} 只（完整期 "
                  f"{payload['summary']['complete']['stocks']} 只，"
                  f"跳过 {len(payload['skipped'])} 只）；网格 {payload['combos']} 组；"
                  f"生成 {payload['built']}。", '']
        lines += summary_table(payload)
        lines += ['', distribution(payload['summary']['complete']['change_distribution'], '完整期组合更换次数'),
                  '- 完整期各均线类型被挑中的次数：' +
                  '，'.join(f'{k.upper()} {v}' for k, v in sorted(payload['summary']['complete']['type_counts'].items())),
                  '', '### MSFT', '']
        lines += msft_tables(payload)
        lines.append('')
    (HERE / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf8')
    print(f'wrote {HERE / "report.md"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
