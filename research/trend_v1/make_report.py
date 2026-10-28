"""Build research/trend_v1/report.md from the result files (numbers only, no interpretation).

    python -X utf8 research/trend_v1/make_report.py
"""
import csv
import json
from pathlib import Path

ROOT = Path('research/trend_v1')
FASTS = (10, 20, 50)
SLOWS = (50, 100, 150, 200)
NS = (5, 10, 15, 20)


def pct(value: float) -> str:
    return f'{value * 100:+.1f}%'


def dd(value: float) -> str:
    return f'{value * 100:.1f}%'


def inv(value: float) -> str:
    return f'{value * 100:.0f}%'


def load():
    runs = list(csv.DictReader((ROOT / 'runs.csv').open(encoding='utf8')))
    for row in runs:
        for key in ('cagr', 'excess', 'max_drawdown', 'average_invested', 'turnover', 'volatility',
                    'cagr_over_dd', 'dd_2020', 'dd_2022', 'c1_cagr', 'c1_max_drawdown', 'c1_average_invested',
                    'c2_cagr', 'c2_max_drawdown', 'c2_dd_2020', 'c2_dd_2022', 'c1_turnover', 'c1_excess'):
            if row.get(key):
                row[key] = float(row[key])
    return runs


def matrix(runs, ma, n, slot, cash):
    index = {(r['ma'], int(r['fast']), int(r['slow']), int(r['top_n']), r['slot'], r['cash_rate']): r
             for r in runs if r['kind'] == 'trend'}
    lines = [f'#### {ma.upper()} · N={n} · {slot} · 现金 {cash}', '',
             '| 快线 \\ 慢线 | 50 | 100 | 150 | 200 |', '|---|---|---|---|---|']
    for fast in FASTS:
        cells = []
        for slow in SLOWS:
            row = index.get((ma, fast, slow, n, slot, cash))
            cells.append('—' if row is None else
                         f"{pct(row['excess'])} / {dd(row['max_drawdown'])} / {inv(row['average_invested'])}")
        lines.append(f'| {fast} | ' + ' | '.join(cells) + ' |')
    return lines


def main():
    runs = load()
    trend = [r for r in runs if r['kind'] == 'trend']
    c1 = [r for r in runs if r['kind'] == 'c1']
    c3 = [r for r in runs if r['kind'] == 'c3']
    out = ['# trend_v1：个股均线趋势实验（第一轮）', '',
           '2026-10-28 运行。登记见 `spec.md`。信号层口径：周五收盘出信号、当天收盘价成交、零成本、小数股、'
           '本金 100,000 美元、2014-02-28 至 2024-06-28 一整段。本文件只写数字。', '',
           f'- 趋势组合 {len(trend)} 次，C1 {len(c1)} 次，C3 {len(c3)} 次。',
           '- 每个单元格：**相对 VOO 的年化超额 / 最大回撤 / 平均仓位**。',
           '- 每行的完整字段（年化、波动、峰值/谷底/恢复日、最长水下、2020 与 2022 年内回撤、逐年收益、'
           '按原因分类的年度交易次数、换手、C1 与 C2 并列）见 `runs.csv`。', '',
           '## 1. SMA 交叉', '']
    for cash in ('zero', 'dtb3'):
        for n in NS:
            for slot in ('equal', 'amount'):
                out += matrix(runs, 'sma', n, slot, cash) + ['']
    out += ['## 2. EMA 交叉', '']
    for cash in ('zero', 'dtb3'):
        for n in NS:
            for slot in ('equal', 'amount'):
                out += matrix(runs, 'ema', n, slot, cash) + ['']
    out += ['## 3. MACD(12,26,9)', '',
            '| N | 名额 | 现金 | 年化 | 超额 | 最大回撤 | 年化/\\|回撤\\| | 平均仓位 | 年换手 | C1 年化 | C1 回撤 | C2 年化 | C2 回撤 |',
            '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for n in NS:
        for slot in ('equal', 'amount'):
            for cash in ('zero', 'dtb3'):
                row = next(r for r in trend if r['ma'] == 'macd' and int(r['top_n']) == n
                           and r['slot'] == slot and r['cash_rate'] == cash)
                out.append(f"| {n} | {slot} | {cash} | {pct(row['cagr'])} | {pct(row['excess'])} | "
                           f"{dd(row['max_drawdown'])} | {row['cagr_over_dd']:.2f} | {inv(row['average_invested'])} | "
                           f"{row['turnover'] * 100:.0f}% | {pct(row['c1_cagr'])} | {dd(row['c1_max_drawdown'])} | "
                           f"{pct(row['c2_cagr'])} | {dd(row['c2_max_drawdown'])} |")
    out += ['', '## 4. C1：一直持有（同一池子、同样不调仓）', '',
            '| N | 名额 | 现金 | 年化 | 超额 | 最大回撤 | 2020 内 | 2022 内 | 平均仓位 | 平均只数 | 最大单只 | 年换手 | 成交笔数 |',
            '|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for n in NS:
        for slot in ('equal', 'amount'):
            for cash in ('zero', 'dtb3'):
                row = next(r for r in c1 if int(r['top_n']) == n and r['slot'] == slot and r['cash_rate'] == cash)
                out.append(f"| {n} | {slot} | {cash} | {pct(row['cagr'])} | {pct(row['excess'])} | "
                           f"{dd(row['max_drawdown'])} | {dd(row['dd_2020'])} | {dd(row['dd_2022'])} | "
                           f"{inv(row['average_invested'])} | {float(row['average_positions']):.1f} | "
                           f"{float(row['max_single_weight']) * 100:.0f}% | {row['turnover'] * 100:.0f}% | {row['trades']} |")
    out += ['', '## 5. C3：VOO 上的均线（为多全仓 VOO，为空现金）', '',
            '| 规则 | 现金 | 年化 | 超额 | 最大回撤 | 年化/\\|回撤\\| | 平均仓位 | 年换手 | 成交笔数 |',
            '|---|---|---|---|---|---|---|---|---|']
    for row in c3:
        tag = 'macd' if row['ma'] == 'macd' else f"{row['ma']}{row['fast']}_{row['slow']}"
        out.append(f"| {tag} | {row['cash_rate']} | {pct(row['cagr'])} | {pct(row['excess'])} | "
                   f"{dd(row['max_drawdown'])} | {row['cagr_over_dd']:.2f} | {inv(row['average_invested'])} | "
                   f"{row['turnover'] * 100:.0f}% | {row['trades']} |")
    episodes = list(csv.DictReader((ROOT / 'episodes.csv').open(encoding='utf8')))
    out += ['', '## 6. 三段大跌', '']
    for name in ('2018Q4', '2020Q1', '2022'):
        rows = [e for e in episodes if e['episode'] == name]
        if not rows:
            continue
        head = rows[0]
        out += [f"### {name}：VOO 峰值 {head['voo_peak_date']} → 谷底 {head['voo_trough_date']}"
                f"（{float(head['voo_drawdown']) * 100:.1f}%），恢复 {head['voo_recovery_date']}", '',
                '| 组合 | 段内收益 | 谷到恢复 | 最低仓位 | 首次低于 50% | 回到 50% 以上 |', '|---|---|---|---|---|---|']
        trows = [e for e in rows if e['kind'] == 'trend']
        for label, subset in (('趋势 368 次 段内最差', [min(trows, key=lambda e: float(e['return_peak_to_trough']))]),
                              ('趋势 368 次 中位', [sorted(trows, key=lambda e: float(e['return_peak_to_trough']))[len(trows) // 2]]),
                              ('趋势 368 次 段内最好', [max(trows, key=lambda e: float(e['return_peak_to_trough']))])):
            for e in subset:
                out.append(f"| {label}（{e['run_id']}） | {pct(float(e['return_peak_to_trough']))} | "
                           f"{pct(float(e['return_trough_to_recovery'])) if e['return_trough_to_recovery'] else '—'} | "
                           f"{inv(float(e['min_invested']))} | {e['first_below_50'] or '—'} | {e['first_back_above_50'] or '—'} |")
        for e in rows:
            if e['kind'] == 'c1' and e['run_id'] in ('C1_always_N10_equal_zero', 'C1_always_N10_amount_zero'):
                out.append(f"| {e['run_id']} | {pct(float(e['return_peak_to_trough']))} | "
                           f"{pct(float(e['return_trough_to_recovery'])) if e['return_trough_to_recovery'] else '—'} | "
                           f"{inv(float(e['min_invested']))} | {e['first_below_50'] or '—'} | {e['first_back_above_50'] or '—'} |")
        c3rows = [e for e in rows if e['kind'] == 'c3']
        if c3rows:
            med = sorted(c3rows, key=lambda e: float(e['return_peak_to_trough']))[len(c3rows) // 2]
            out.append(f"| C3 中位（{med['run_id']}） | {pct(float(med['return_peak_to_trough']))} | "
                       f"{pct(float(med['return_trough_to_recovery'])) if med['return_trough_to_recovery'] else '—'} | "
                       f"{inv(float(med['min_invested']))} | {med['first_below_50'] or '—'} | {med['first_back_above_50'] or '—'} |")
        out.append('')
    summary = list(csv.DictReader((ROOT / 'per_stock_summary.csv').open(encoding='utf8')))
    stocks = len({r['security_id'] for r in csv.DictReader((ROOT / 'per_stock.csv').open(encoding='utf8'))})
    out += ['## 7. 单只股票层面', '',
            f'范围：开发段内曾进入前 20 家的 {stocks} 只证券，各用自己在 2014-02-28 至 2024-06-28 的完整历史，'
            '不受是否在池中限制。', '',
            '| 规则 | 年化/\\|回撤\\| 高于持有 的股票占比 | 回撤小于持有 的股票占比 | 为多−为空（1 周） | （4 周） | （13 周） | 为多组 13 周均值 | 为空组 13 周均值 |',
            '|---|---|---|---|---|---|---|---|']
    for ma, fast, slow in [('sma', f, s) for f, s in ((10, 50), (10, 100), (10, 150), (10, 200), (20, 50), (20, 100),
                                                      (20, 150), (20, 200), (50, 100), (50, 150), (50, 200))] + \
                          [('ema', f, s) for f, s in ((10, 50), (10, 100), (10, 150), (10, 200), (20, 50), (20, 100),
                                                      (20, 150), (20, 200), (50, 100), (50, 150), (50, 200))] + [('macd', 12, 26)]:
        tag = 'macd' if ma == 'macd' else f'{ma}{fast}_{slow}'
        rows = {int(r['horizon']): r for r in summary if r['signal'] == tag}
        if not rows:
            continue
        out.append(f"| {tag} | {float(rows[13]['share_ratio_above_hold']) * 100:.0f}% | "
                   f"{float(rows[13]['share_dd_lower']) * 100:.0f}% | "
                   f"{float(rows[1]['mean_difference_across_stocks']) * 100:+.2f}% | "
                   f"{float(rows[4]['mean_difference_across_stocks']) * 100:+.2f}% | "
                   f"{float(rows[13]['mean_difference_across_stocks']) * 100:+.2f}% | "
                   f"{float(rows[13]['mean_long']) * 100:+.2f}% | {float(rows[13]['mean_flat']) * 100:+.2f}% |")
    out += ['', '每只股票 × 每条规则的明细见 `per_stock.csv` 与 `per_stock_forward.csv`。', '',
            '## 8. 文件', '',
            '| 文件 | 内容 |', '|---|---|',
            '| `spec.md` | 运行前登记 |', '| `configs.csv` | 430 次运行的完整配置 |',
            '| `runs.csv` | 每次运行的全部指标，C1、C2 并列 |', '| `episodes.csv` | 三段大跌逐段 |',
            '| `per_stock.csv` | 每只股票 × 每条规则 vs 一直持有 |',
            '| `per_stock_forward.csv` | 每只股票 × 每条规则 × 1/4/13 周的前向收益 |',
            '| `per_stock_summary.csv` | 上面两项的按规则汇总 |',
            '| `equity_daily.csv` | 430 次运行的日权益 |', '| `run_trend.py` | 运行脚本（支持 `--only`） |',
            '| `independent_check.py` | 不调用引擎的独立重算 |', '| `checks.py` | 账目检查 |', '']
    (ROOT / 'report.md').write_text('\n'.join(out) + '\n', encoding='utf8')
    print('lines', len(out))


if __name__ == '__main__':
    main()
