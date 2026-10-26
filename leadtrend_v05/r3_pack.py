"""Round 3 evaluation tables and the discussion pack (plan 10.4-10.5). Numbers and the mechanical 10.5 checks only.

evaluate() writes the robustness tables beside the registration and discussion_pack.md. It reads the registered run folder
(scorecard, pairs, explore, daily, null) and the robustness folders written by r3_evaluation.runs.
"""
import csv
import json
from pathlib import Path

from .perf_artifacts import read_table
from .r1_evaluation import _write
from .r3_evaluation import (ANCHOR_NAME, NAMES, _ids, cap_events, criteria, exclusion_table, noise_table, pair_bootstrap,
                            random_table, timing_table, top_issuers, _tranche, _result, _folder)
from .experiments import A90_ID, _metrics_or_none


def _p(v, nd=1):
    return '' if v in (None, '') else f'{100 * float(v):.{nd}f}%'


def _r(v, nd=2):
    return '' if v in (None, '') else f'{float(v):.{nd}f}'


def _md(headers, rows):
    lines = ['| ' + ' | '.join(headers) + ' |', '|' + '---|' * len(headers)]
    lines += ['| ' + ' | '.join('' if v is None else str(v) for v in r) + ' |' for r in rows]
    return '\n'.join(lines) + '\n'


def _scorecard(run):
    with (Path(run) / 'scorecard.csv').open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def evaluate(root, run, robust, out):
    root, run, robust, out = Path(root), Path(run), Path(robust), Path(out)
    rob = out / 'robustness'
    rob.mkdir(parents=True, exist_ok=True)
    loaded, ids, anchors = _ids(root)
    name_of = {v: k for k, v in ids.items()}
    sc = _scorecard(run)
    sc_by = {r['id']: r for r in sc}
    weeks = sorted({r['signal_session_date'] for r in read_table(run / 'explore' / A90_ID / 'cost_10bps' / 'weekly_checks.csv')})

    noise = noise_table(root, run, robust, ids, anchors)
    _write(rob / 'noise_ruler.csv', noise)
    timing = timing_table(root, run)
    _write(rob / 'timing_subaccounts.csv', timing)
    exclusion = exclusion_table(root, robust, ids, anchors)
    _write(rob / 'top3_exclusion.csv', exclusion)
    sets = json.loads((robust / 'top_sets.json').read_text(encoding='utf-8'))
    rnd_rows, rnd_summary = random_table(root, run, ids, {ids[n]: _excess10(root, run, ids[n]) for n in NAMES + ['A90', 'SEC3', 'G']})
    _write(rob / 'experiment_random_seeds.csv', rnd_rows)
    (rob / 'experiment_random_summary.json').write_text(json.dumps(rnd_summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    boot = pair_bootstrap(root, run, ids, weeks)
    _write(rob / 'bootstrap_paired.csv', boot)
    crit = criteria(root, run, robust, ids, noise, exclusion)
    _write(rob / 'criteria_base.csv', crit['base_rows'])
    _write(rob / 'criteria_add.csv', crit['add_rows'])
    cap = cap_events(root, run, ids)
    _write(rob / 'cap15_trims.csv', [cap])

    pairs = list(csv.DictReader((run / 'pairs.csv').open(encoding='utf-8', newline='')))
    lines = ['# 第三轮（R3_structure）讨论资料包', '',
             '只有数字和按第 10.5 节机械套用的核对结果，不含结论。开发段，探索档（10 bp）为主，daily 确认见各表注明。', '']

    lines += ['## 1. 成绩单主表（10 bp，相对 VOO 年化超额；探索档）', '']
    head = ['配置', 'ID', '锚点', '10bp 超额', '5bp 超额', '相对锚点差(10bp)', '相对锚点差(5bp)', '随机对照百分位', '随机完成']
    rows = []
    for n in NAMES + ['A90', 'SEC3', 'G']:
        r = sc_by.get(ids[n])
        if r is None:
            continue
        rows.append([n, ids[n], anchors.get(n, ''), _p(r['annualized_excess_10bps'], 2), _p(r['annualized_excess_5bps'], 2),
                     _p(r['paired_vs_anchor_10bps'], 2), _p(r['paired_vs_anchor_5bps'], 2), r['null_cell'], r['null_completed']])
    lines += [_md(head, rows)]

    lines += ['## 2. 风险与回撤（第 9.2 节全部项目，10 bp，含同期 VOO 与夏普比）', '']
    head = ['配置', '最大回撤', '峰值日', '谷底日', '恢复日', '最长水下(日历日)', '2020 年最大回撤', '2022 年最大回撤',
            '最差自然年收益', '最差年份', '夏普比(扣 DTB3)', '收益/回撤', '头号发行人占总盈亏']
    rows = []
    for n in NAMES + ['A90']:
        r = sc_by[ids[n]]
        rows.append([n, _p(r['max_drawdown']), r['dd_peak_date'], r['dd_trough_date'], r['dd_recovery_date'],
                     r['longest_underwater_days'], _p(r['dd_2020']), _p(r['dd_2022']), _p(r['worst_year_return']),
                     r['worst_year'], _r(r['sharpe_dtb3']), _r(r['ret_over_dd']), _p(r['top_issuer_share'])])
    r = sc_by[ids['A90']]
    rows.append(['VOO', _p(r['voo_max_drawdown']), r['voo_dd_peak_date'], r['voo_dd_trough_date'], r['voo_dd_recovery_date'],
                 r['voo_longest_underwater_days'], _p(r['voo_dd_2020']), _p(r['voo_dd_2022']), _p(r['voo_worst_year_return']),
                 r['voo_worst_year'], _r(r['voo_sharpe_dtb3']), _r(r['voo_ret_over_dd']), ''])
    lines += [_md(head, rows)]
    head = ['配置', '年化波动', 'β(对VOO)', '跟踪误差', '最大单股权重', '单板块超 40% 天数占比', '单板块超 50% 天数占比', '年单边换手(x)']
    rows = [[n, _p(sc_by[ids[n]]['vol_annual']), _r(sc_by[ids[n]]['beta']), _p(sc_by[ids[n]]['tracking_error']),
             _p(sc_by[ids[n]]['max_single_weight']), _p(sc_by[ids[n]]['sector_over_40_share']),
             _p(sc_by[ids[n]]['sector_over_50_share']), _r(sc_by[ids[n]]['turnover_one_way'], 1)] for n in NAMES + ['A90']]
    lines += [_md(head, rows)]

    lines += ['## 3. 配对表（a 减 b，年化超额之差；探索档）', '']
    rows = [[p['pair'], _p(p['diff_10bps'], 2), _p(p['diff_5bps'], 2), _p(p['diff_period_a'], 2), _p(p['diff_period_b'], 2)]
            for p in pairs]
    lines += [_md(['配对', '10bp 差', '5bp 差', '时段A差(10bp)', '时段B差(10bp)'], rows)]

    lines += ['## 4. 评判标准逐条核对（第 10.5 节，机械套用，不加解读）', '']
    rows = [[b['structure'], _p(b['mean10_paired_vs_A90'], 2), '达到' if b['c1'] else '未达到',
             _p(b['mean5_paired_vs_A90'], 2), _p(b['range10'], 2), _p(b['range10_A90'], 2), '达到' if b['c2'] else '未达到',
             _p(b['paired_excluded'], 2), '达到' if b['c3'] else '未达到', '达到' if b['all_three'] else '未达到']
            for b in crit['base_rows']]
    lines += ['### 4.1 结构能否成为第四轮底座（对照 A90，5 个起点 k=0–4）', '',
              _md(['结构', '10bp 平均配对差', '条件1(10bp)', '5bp 平均配对差', '10bp 超额极差', 'A90 极差', '条件2',
                   '剔除前3名后配对差', '条件3', '三条全部满足'], rows),
              f"按规则选出的底座：**{crit['base'] if crit['base'] != 'A90' else '仍为 A90'}**", '']
    rows = [[a['structure'], _p(a['mean10_paired_vs_T4'], 2), '达到' if a['s1'] else '未达到',
             f"{a['positive_starts']}/5", '达到' if a['s2'] else '未达到',
             _p(a['period_a_paired'], 2), _p(a['period_b_paired'], 2), '达到' if a['s3'] else '未达到',
             '达到' if a['all_three'] else '未达到'] for a in crit['add_rows']]
    lines += ['### 4.2 SEC3 或 G 能否加入底座（对照 T4）', '',
              _md(['配置', '10bp 平均配对差(对T4)', '条件1', '配对差为正的起点', '条件2', '时段A配对差(k=0)', '时段B配对差(k=0)',
                   '条件3', '三条全部满足'], rows),
              f"底座为 T4：{'是' if crit['base'] == 'T4' else '否（不适用，本项不可加入）'}；加入结果：**{crit['admitted'] or '无'}**", '']

    lines += ['## 5. 噪音尺（起点 k=0–4，10bp；5bp 见第 5.2 节）', '']
    rows = [[r['object'], r['id'], _p(r['excess10_k0'], 2), _p(r['excess10_k1'], 2), _p(r['excess10_k2'], 2),
             _p(r['excess10_k3'], 2), _p(r['excess10_k4'], 2), _p(r['mean_k0_4'], 2), _p(r['min_k0_4'], 2),
             _p(r['max_k0_4'], 2), _p(r['range_k0_4'], 2), r.get('anchor', ''), _p(r.get('paired10_mean'), 2)]
            for r in noise]
    lines += [_md(['对象', 'ID', 'k=0', 'k=1', 'k=2', 'k=3', 'k=4', '平均', '最小', '最大', '极差', '锚点', '平均配对差(10bp)'], rows)]
    rows = [[r['object'], _p(r.get('excess5_k0'), 2), _p(r.get('excess5_k1'), 2), _p(r.get('excess5_k2'), 2),
             _p(r.get('excess5_k3'), 2), _p(r.get('excess5_k4'), 2), _p(r.get('excess5_mean'), 2), _p(r.get('paired5_mean'), 2)]
            for r in noise if 'excess5_k0' in r]
    lines += ['### 5.2 5 bp（结构与 A90）', '', _md(['对象', 'k=0', 'k=1', 'k=2', 'k=3', 'k=4', '平均', '平均配对差(5bp，对锚点)'], rows)]
    rows = [[b['name'], b['anchor'], _p(b['point_annualized_diff'], 2), _p(b['p05'], 2), _p(b['p95'], 2)] for b in boot]
    lines += ['### 5.3 配对差的块自助区间（块长 8 周，2000 次，种子 20261026）', '',
              _md(['配置', '对照', '点估计(年化)', '5%', '95%'], rows)]

    lines += ['## 6. 调仓时点（T4 系列，各子账户，10 bp，起点 k=0）', '']
    rows = [[t['structure'], t['sub_account'], t['first_trade'], _p(t['annualized_return'], 2),
             _p(t['benchmark_annualized'], 2), _p(t['annualized_excess'], 2), _p(t['max_drawdown'], 2)] for t in timing]
    lines += [_md(['结构', '子账户', '首次交易日', '年化收益', 'VOO 同窗口年化', '年化超额', '最大回撤'], rows)]

    lines += ['## 7. 剔除前 3 名发行人（10 bp，配置与其锚点、A90 同时剔除）', '']
    rows = [[n, '; '.join(f"{r['issuer']}({_p(r['share'])})" for r in sets[n])] for n in NAMES + ['A90']]
    lines += ['### 7.1 各配置的前 3 名（按自己 10 bp daily 运行的发行人盈亏）', '', _md(['配置', '前 3 名（盈亏占比）'], rows)]
    rows = [[e['owner'], e['id'], _p(e['excess10_excluded'], 2), _p(e['A90_excluded'], 2), _p(e['paired_vs_A90_excluded'], 2)]
            for e in exclusion if e['id'] == ids[e['owner']]]
    lines += ['### 7.2 剔除后的超额与对 A90 的配对差', '', _md(['配置', 'ID', '剔除后超额', '剔除后 A90 超额', '对 A90 的配对差'], rows)]

    lines += ['## 8. CAP15 的减仓（单股上限 c=0.15）', '']
    lines += [_md(['减仓次数', '发生减仓的信号周数', '减仓金额（卖出名义，美元）', '首次减仓日', '末次减仓日'],
                  [[cap['trims'], cap['weeks_with_trims'], _r(cap['sold_notional'], 0), cap['first_trim'], cap['last_trim']]])]

    lines += ['## 9. N20 的集中度（单一账户 20 只，10 bp）', '']
    r = sc_by[ids['N20']]
    lines += [_md(['最大单股权重', '单板块超 40% 天数占比', '单板块超 50% 天数占比', '头号发行人占总盈亏'],
                  [[_p(r['max_single_weight']), _p(r['sector_over_40_share']), _p(r['sector_over_50_share']), _p(r['top_issuer_share'])]])]

    lines += ['## 10. 实验级随机对照与区间估计', '']
    lines += [_md(['项目', '数值'], [[k, _p(v, 2) if isinstance(v, float) and abs(v) < 5 else v] for k, v in rnd_summary.items()
                                    if k not in ('hypothesis',)])]

    lines += ['## 11. 口径说明', '']
    lines += ['- 成绩单与配对表来自 run-experiment（work/runs/experiments/R3_structure，复制到本目录）。',
              '- 分批（T4 系列）：4 个子账户各 10 只，子账户 j 在第 j 个信号周建仓，之后每 4 周检查；每个子账户 1/4 资本；'
              '合并账户为四者相加，超额与回撤按合并账户计算。',
              '- 集中度与头号发行人按合并账户的实际持仓与盈亏计算。',
              '- 起点推迟（k=1–4）时，整个子账户日程随之推迟。剔除发行人对全部子账户同时生效。',
              '- 单股上限（CAP15）：减仓股数按上一交易日收盘价与上一交易日总权益计算，拆股当日前收盘价按拆股比例调整。',
              '- 随机对照：每个子账户用自己 10 bp 探索档的退出只数各自抽签（种子 0–99），合并为一个账户后比较。',
              '- 夏普比扣除 DTB3（FRED 三个月国库券利率）；VOO 数字同样口径。',
              '- 开发段结束于 2024-06-28；未使用保留段。']

    (out / 'discussion_pack.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    dump_summary = dict(base=crit['base'], admitted=crit['admitted'], cap=cap, random=rnd_summary)
    (rob / 'summary.json').write_text(json.dumps(dump_summary, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    return dump_summary


def _excess10(root, run, pid):
    K = _tranche(root, pid)
    m = _metrics_or_none(_result(_folder(run, pid, 10), K))
    return m['annualized_excess'] if m else None
