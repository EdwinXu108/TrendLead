"""Round 1 evaluation (plan part 3): experiment-level random controls, paired bootstrap, the four promotion criteria,
concentration and counts, the issuer-exclusion stress test, and the discussion pack. Numbers only.

The registered run folders are read as they are. The only new runs are the stress runs: the same engine and the same
authorisation as the experiment, with one issuer removed from each week's chosen set and candidates.

Usage: python -X utf8 -m leadtrend_v05.r1_evaluation --run work/runs/experiments/R1_mechanisms \
           --out research/v05/experiments/R1_mechanisms/evaluation
"""
import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import paths
from .experiments import A90_ID, _experiment_ctx, _jobs, _metrics_or_none, _run_jobs, load_spec, run_metrics
from .perf_artifacts import read_table
from .perf_cache import identity, load_cache
from .sectors import load_sector_map, sector_key

NAMES = ['ENT_POS', 'ENT_NEG', 'SEC3', 'SEC2', 'SKP90', 'SKP60', 'CAP2', 'PEER', 'G', 'VOLA', 'SIZE', 'WGT']
PROMOTION = ['ENT_POS', 'ENT_NEG', 'SKP90', 'SKP60', 'PEER', 'G', 'VOLA', 'SIZE']
SEEDS = 100
BOOT_SEED = 20261026
BOOT_BLOCK = 8
BOOT_N = 2000
EXPERIMENT_ID = 'R1_STRESS'


def _write(path, rows):
    with path.open('w', encoding='utf-8', newline='') as f:
        if not rows:
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _f(v):
    return None if v in ('', None) else float(v)


def _p(v, nd=1):
    return '' if v is None else f'{100 * v:.{nd}f}%'


# ---------------------------------------------------------------- 1. experiment-level random controls

def experiment_random(base, pid_of, anchor_of, exc10):
    best_max, best_pair = [], []
    for s in range(SEEDS):
        vals, paired = [], []
        for n in NAMES:
            pid = pid_of[n]
            m = _metrics_or_none(base / 'null' / pid / f'NULL_{pid}_s{s:03d}' / 'cost_10bps')
            if m is not None:
                vals.append(m['annualized_excess'])
                paired.append(m['annualized_excess'] - exc10[anchor_of[n]])
        best_max.append((max(vals) if len(vals) == len(NAMES) else None, len(vals)))
        best_pair.append((max(paired) if len(paired) == len(NAMES) else None, len(paired)))
    actual = max(exc10[pid_of[n]] for n in NAMES)
    actual_pair = max(exc10[pid_of[n]] - exc10[anchor_of[n]] for n in NAMES)
    full_max = [v for v, _ in best_max if v is not None]
    full_pair = [v for v, _ in best_pair if v is not None]
    summary = dict(
        seeds=SEEDS, seeds_with_all_12_controls=len(full_max),
        actual_best_excess_10bps=actual,
        percentile_of_actual_best=100 * sum(v <= actual for v in full_max) / len(full_max) if full_max else None,
        actual_best_paired_10bps=actual_pair,
        percentile_of_actual_best_paired=100 * sum(v <= actual_pair for v in full_pair) / len(full_pair) if full_pair else None,
        null_best_excess_median=float(np.median(full_max)) if full_max else None,
        null_best_paired_median=float(np.median(full_pair)) if full_pair else None,
        hypothesis='each seed s gives one draw per configuration; the draw order sha256(seed|signal date|security) is the same '
                   'for all configurations, so the twelve controls of one seed form one complete random experiment; '
                   'paired variant subtracts each configuration\'s own anchor actual excess')
    rows = [dict(seed=s, best_null_excess=best_max[s][0], controls_completed=best_max[s][1],
                 best_null_paired=best_pair[s][0]) for s in range(SEEDS)]
    return rows, summary


# ---------------------------------------------------------------- 2. paired bootstrap (exploratory)

def weekly_series(base, pid, weeks):
    eq = {r['session_date']: float(r['total_equity']) for r in read_table(base / 'explore' / pid / 'cost_10bps' / 'daily_equity.csv')}
    e = np.array([eq[w] for w in weeks])
    return e[1:] / e[:-1] - 1


def bootstrap(diff):
    rng = np.random.default_rng(BOOT_SEED)
    n = len(diff)
    starts = n - BOOT_BLOCK + 1
    draws = np.empty(BOOT_N)
    for b in range(BOOT_N):
        idx = []
        while len(idx) < n:
            s = int(rng.integers(0, starts))
            idx.extend(range(s, s + BOOT_BLOCK))
        draws[b] = float(np.mean(diff[np.array(idx[:n])])) * 52
    return draws


def paired_bootstrap(base, pid_of, weeks):
    a90 = weekly_series(base, A90_ID, weeks)
    rows = []
    for n in NAMES:
        diff = weekly_series(base, pid_of[n], weeks) - a90
        d = bootstrap(diff)
        rows.append(dict(name=n, weeks=len(diff), point_annualized_diff_vs_A90=float(np.mean(diff)) * 52,
                         p05=float(np.percentile(d, 5)), p95=float(np.percentile(d, 95)),
                         method=f'moving block, block {BOOT_BLOCK} weeks, {BOOT_N} draws, seed {BOOT_SEED}; annualised as weekly mean x 52; exploratory'))
    return rows


# ---------------------------------------------------------------- 3. promotion criteria (plan 4.3)

def criteria(rows):
    out = []
    for n in PROMOTION:
        r = rows[n]
        p10, p5 = _f(r['paired_vs_anchor_10bps']), _f(r['paired_vs_anchor_5bps'])
        pa, pb = _f(r['paired_period_a']), _f(r['paired_period_b'])
        loyo = int(r['leave_one_year_out_positive'])
        low = _f(r['null_percentile_low'])
        c1 = p10 is not None and p10 >= 0.01 and p5 is not None and p5 > 0
        c2 = pa is not None and pb is not None and not (pa * pb < 0)
        c3 = loyo >= 3
        c4 = low is not None and low >= 75
        out.append(dict(name=n, paired_10bps=_p(p10, 2), paired_5bps=_p(p5, 2), period_A=_p(pa, 2), period_B=_p(pb, 2),
                        leave_one_year_out_positive=loyo, null_lower=_p(low / 100 if low is not None else None),
                        c1_gap_ge_1pp_and_5bp_same_sign=c1, c2_periods_not_opposite=c2, c3_loyo_ge_3=c3, c4_null_lower_ge_75=c4,
                        all_four=c1 and c2 and c3 and c4))
    return out


# ---------------------------------------------------------------- 4. concentration and counts

def concentration(base, pid_of, sector_map):
    rows = []
    for n in NAMES + ['A90']:
        pid = A90_ID if n == 'A90' else pid_of[n]
        folder = base / 'daily' / pid / 'cost_10bps'
        equity = {r['session_date']: float(r['total_equity']) for r in read_table(folder / 'daily_equity.csv')}
        by_day = defaultdict(lambda: defaultdict(float))
        for r in read_table(folder / 'positions.csv'):
            if r.get('asset_type') == 'actual_shares' and r['session_date'] in equity:
                by_day[r['session_date']][sector_key(r['industry'], sector_map)[0]] += float(r['value']) / equity[r['session_date']]
        daily = np.array([max(by_day[d].values()) if by_day.get(d) else 0.0 for d in sorted(equity)])
        rows.append(dict(name=n, days=len(daily), largest_sector_median=float(np.median(daily)),
                         largest_sector_p90=float(np.percentile(daily, 90)), largest_sector_max=float(daily.max()),
                         share_days_over_40=float(np.mean(daily > 0.40)), share_days_over_50=float(np.mean(daily > 0.50))))
    return rows


# ---------------------------------------------------------------- 5. stress: remove the top-contributing issuer

class IssuerExcludedFrames:
    def __init__(self, frames, excluded):
        self.frames = frames
        self.excluded = excluded

    def __getitem__(self, d):
        return _ExcludedWeek(self.frames[d], self.excluded)


class _ExcludedWeek:
    def __init__(self, week, excluded):
        self.week = week
        self.excluded = excluded

    def __getitem__(self, key):
        if key == 'chosen':
            return frozenset(s for s in self.week['chosen'] if s not in self.excluded)
        if key == 'universe':
            return [r for r in self.week['universe'] if r['security_id'] not in self.excluded]
        if key == 'windows':
            return _ExcludedWindows(self.week['windows'], self.excluded)
        raise KeyError(key)


class _ExcludedWindows:
    def __init__(self, windows, excluded):
        self.windows = windows
        self.excluded = excluded

    def __getitem__(self, L):
        w = self.windows[L]
        return dict(scores=w['scores'], ranked=w['ranked'],
                    candidates=[c for c in w['candidates'] if c.security_id not in self.excluded],
                    unscored={s: r for s, r in w['unscored'].items() if s not in self.excluded})


def top_issuer(root, base, pid):
    issuer_of = {r['security_id']: r.get('issuer_id', '') for r in read_table(root / paths.NORMALIZED / 'securities.csv')}
    profit = defaultdict(float)
    for r in read_table(base / 'daily' / pid / 'cost_10bps' / 'daily_contributions.csv'):
        profit[issuer_of.get(r['security_id'], r['security_id'])] += float(r['profit_dollars'])
    total = sum(profit.values())
    issuer = max(profit, key=lambda k: (profit[k], k))
    return issuer, profit[issuer], total


def stress_run(root, out_base, point, issuer):
    from .perf_cache import ensure_signal_tables
    doc = identity(root, 'uniform')
    load_cache(root, doc)
    ensure_signal_tables(root, doc, [point['signal']])
    experiment = {'id': EXPERIMENT_ID, 'excluded_issuer': issuer, 'note': 'stress test; not an attempt; not for selection'}
    payload = dict(points=[point], costs=[5, 10], experiment=experiment)
    ctx = _experiment_ctx(root, doc, payload)
    excluded = {r['security_id'] for r in read_table(root / paths.NORMALIZED / 'securities.csv') if r.get('issuer_id') == issuer}
    ctx.frames = IssuerExcludedFrames(ctx.frames, excluded)
    jobs = _jobs([point], out_base, 'explore', 'explore', 'explore', [5, 10], experiment)
    _run_jobs(jobs, ctx, 1, payload)
    return {c: _metrics_or_none(out_base / 'explore' / point['id'] / f'cost_{c}bps') for c in (5, 10)}


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--spec', default='research/v05/experiments/R1_mechanisms.json')
    args = ap.parse_args(argv)
    root = Path('.').resolve()
    base = root / args.run
    out = root / args.out
    out.mkdir(parents=True, exist_ok=True)
    loaded = load_spec(root, args.spec)
    pid_of = {p['name']: p['id'] for p in loaded['grid']}
    anchor_of = {p['name']: p['anchor'] for p in loaded['grid']}
    sc = {r['id']: r for r in read_table(base / 'scorecard.csv')}
    scorecard_by_name = {n: sc[pid_of[n]] for n in NAMES}
    scorecard_by_name['A90'] = sc[A90_ID]
    exc10, m10, m5 = {}, {}, {}
    for pid in set(pid_of.values()) | {A90_ID, 'L060_N10_b2_R2', 'L090_N08_b2_R2'}:
        m10[pid] = _metrics_or_none(base / 'explore' / pid / 'cost_10bps')
        m5[pid] = _metrics_or_none(base / 'explore' / pid / 'cost_5bps')
        exc10[pid] = m10[pid]['annualized_excess']

    rows, summary = experiment_random(base, pid_of, anchor_of, exc10)
    _write(out / 'experiment_random_seeds.csv', rows)
    (out / 'experiment_random_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    weeks = sorted({r['signal_session_date'] for r in read_table(base / 'explore' / A90_ID / 'cost_10bps' / 'weekly_checks.csv')})
    boot = paired_bootstrap(base, pid_of, weeks)
    _write(out / 'bootstrap_paired_vs_A90.csv', boot)

    crit = criteria(scorecard_by_name)
    _write(out / 'criteria.csv', crit)

    sector_map = load_sector_map(root)
    conc = concentration(base, pid_of, sector_map)
    _write(out / 'concentration.csv', conc)

    passers = [c['name'] for c in crit if c['all_four']]
    from .experiments import make_point
    stress_rows, after = [], {}
    for n in ['A90'] + passers:
        pid = A90_ID if n == 'A90' else pid_of[n]
        point = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2') if n == 'A90' else next(
            p for p in loaded['grid'] if p['id'] == pid)
        issuer, issuer_profit, total = top_issuer(root, base, pid)
        stressed = stress_run(root, base / 'stress' / pid, point, issuer)
        after[n] = {c: (stressed[c]['annualized_excess'] if stressed[c] else None) for c in (5, 10)}
        stress_rows.append(dict(name=n, id=pid, issuer=issuer, issuer_cumulative_profit=issuer_profit,
                                share_of_total_profit=issuer_profit / total if total else None,
                                excess_10bps_before=m10[pid]['annualized_excess'], excess_10bps_after=after[n][10],
                                excess_5bps_before=m5[pid]['annualized_excess'], excess_5bps_after=after[n][5],
                                paired_vs_A90_before_10bps=m10[pid]['annualized_excess'] - m10[A90_ID]['annualized_excess'],
                                paired_vs_A90_after_10bps=None))
    for row in stress_rows:
        if row['name'] != 'A90' and after[row['name']][10] is not None and after['A90'][10] is not None:
            row['paired_vs_A90_after_10bps'] = after[row['name']][10] - after['A90'][10]
    _write(out / 'stress_issuer_exclusion.csv', stress_rows)
    names_by_id = {pid: n for n, pid in pid_of.items()}
    names_by_id.update({A90_ID: 'A90', 'L060_N10_b2_R2': 'A60', 'L090_N08_b2_R2': 'BASE_L090_N08'})
    pack = write_pack(base, out, names_by_id)
    print(json.dumps(dict(random=summary, passers=passers, stress=[r['name'] for r in stress_rows], pack=str(pack)), ensure_ascii=False))
    return 0



# ---------------------------------------------------------------- discussion pack (numbers only)

def _ratio(v):
    return '—' if v in ('', None) else f'{float(v):.1f}x'


def _csv(path):
    with path.open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def write_pack(base, out, names_by_id):
    """discussion_pack.md: the tables for the next round's discussion. Numbers only, no conclusions."""
    sc = _csv(base / 'scorecard.csv')
    pairs = _csv(base / 'pairs.csv')
    crit = _csv(out / 'criteria.csv')
    conc = _csv(out / 'concentration.csv')
    boot = _csv(out / 'bootstrap_paired_vs_A90.csv')
    stress = _csv(out / 'stress_issuer_exclusion.csv')
    summary = json.loads((out / 'experiment_random_summary.json').read_text(encoding='utf-8'))

    def pc(v, nd=2):
        return '' if v in ('', None) else f'{100 * float(v):.{nd}f}%'

    lines = ['# R1_mechanisms 讨论资料包', '',
             '只含数字与口径，不含结论。开发段（2019-11-29 至 2024-06-28），幸存者偏差与当前行业分类仍存在。'
             '不含个股价格序列；个股层面只出现证券编号与发行人编号。', '',
             '## 一、成绩单主表（探索档，年化超额 = 相对 VOO；10 bp 与 5 bp）', '',
             '| 配置 | 名称 | 锚点 | 10bp 年化收益 | 10bp 超额 | 5bp 超额 | 相对锚点差 10bp | 相对锚点差 5bp | 相对A90差 10bp | 年单边换手 | 最大回撤 |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for r in sc:
        name = names_by_id.get(r['id'], '锚点/基线')
        lines.append(f"| {r['id']} | {name} | {r['anchor'] or '—'} | {pc(r['annualized_return_10bps'])} | "
                     f"{pc(r['annualized_excess_10bps'])} | {pc(r['annualized_excess_5bps'])} | "
                     f"{pc(r['paired_vs_anchor_10bps'])} | {pc(r['paired_vs_anchor_5bps'])} | {pc(r['paired_vs_A90_10bps'])} | "
                     f"{float(r['turnover_one_way']):.1f} | {pc(r['max_drawdown'])} |")
    lines += ['', '## 二、配对表（a 减 b，年化超额之差）', '', '| 配对 | 10bp | 5bp | 时段A | 时段B |', '|---|---|---|---|---|']
    for r in pairs:
        lines.append(f"| {r['pair']} | {pc(r['diff_10bps'])} | {pc(r['diff_5bps'])} | {pc(r['diff_period_a'])} | {pc(r['diff_period_b'])} |")
    lines += ['', '## 三、逐条晋级核对（方案 4.3；达到/未达到，括号内为数字）', '',
              '| 配置 | 10bp 配对差 ≥1pp，且 5bp 同向为正 | 两时段配对差不相反 | 逐年剔除一年后为正的次数 ≥3 | 随机对照百分位下界 ≥75% | 全部达到 |',
              '|---|---|---|---|---|---|']
    for r in crit:
        def yn(v):
            return '达到' if v == 'True' else '未达到'
        lines.append(f"| {r['name']} | {yn(r['c1_gap_ge_1pp_and_5bp_same_sign'])}（10bp {r['paired_10bps']}，5bp {r['paired_5bps']}） | "
                     f"{yn(r['c2_periods_not_opposite'])}（A {r['period_A']}，B {r['period_B']}） | "
                     f"{yn(r['c3_loyo_ge_3'])}（{r['leave_one_year_out_positive']}/4） | {yn(r['c4_null_lower_ge_75'])}（{r['null_lower']}） | "
                     f"{'达到' if r['all_four'] == 'True' else '未达到'} |")
    lines += ['', '## 四、集中度（逐日最大大板块权重，探索档之外的日频运行）', '',
              '| 配置 | 中位数 | 90 分位 | 最大值 | 超过40%的天数占比 | 超过50%的天数占比 |', '|---|---|---|---|---|---|']
    for r in conc:
        lines.append(f"| {r['name']} | {pc(r['largest_sector_median'], 1)} | {pc(r['largest_sector_p90'], 1)} | {pc(r['largest_sector_max'], 1)} | "
                     f"{pc(r['share_days_over_40'], 1)} | {pc(r['share_days_over_50'], 1)} |")
    lines += ['', '## 五、CAP 与 ENT 的专属计数，及随机对照（完成数、换手中位数）', '',
              '| 配置 | 触发周数 | 顺延次数 | ENT 一遍买入 | ENT 二遍买入 | 随机完成数 | 随机换手中位数 | 实际换手 |', '|---|---|---|---|---|---|---|---|']
    for r in sc:
        if not r['id'] or r['id'] not in names_by_id:
            continue
        lines.append(f"| {names_by_id[r['id']]} | {r['cap_trigger_weeks'] or '—'} | {r['cap_deferred'] or '—'} | {r['pass1_buys'] or '—'} | "
                     f"{r['pass2_buys'] or '—'} | {r['null_completed']} | {_ratio(r['null_turnover_median'])} | {_ratio(r['turnover_one_way'])} |")
    lines += ['', '## 六、实验级随机对照', '',
              f"- 种子数 {summary['seeds']}，每个种子 12 个配置全部完成：{summary['seeds_with_all_12_controls']} 个。",
              f"- 实际 12 个配置中最高的 10bp 年化超额：{pc(summary['actual_best_excess_10bps'])}；"
              f"它在 100 个“每种子取最大值”的随机分布中的百分位：{summary['percentile_of_actual_best']:.1f}%。",
              f"- 配对版本（随机结果减各自锚点的实际超额）：实际最大值 {pc(summary['actual_best_paired_10bps'])}，"
              f"百分位 {summary['percentile_of_actual_best_paired']:.1f}%。",
              f"- 随机最大值的中位数：超额 {pc(summary['null_best_excess_median'])}，配对 {pc(summary['null_best_paired_median'])}。",
              '- 假设：随机组合各自遵守本配置的结构约束（细分行业 k、大板块上限）和退出节奏；同一种子下抽签顺序相同。', '']
    lines += ['## 七、区间估计（探索性，非显著性检验）', '',
              '| 配置 | 周数 | 年化配对差（相对 A90，周均差 × 52） | 5% | 95% |', '|---|---|---|---|---|']
    for r in boot:
        lines.append(f"| {r['name']} | {r['weeks']} | {pc(r['point_annualized_diff_vs_A90'])} | {pc(r['p05'])} | {pc(r['p95'])} |")
    lines += ['', '## 八、压力测试（剔除各自头号贡献发行人；只作压力测试，不计入试验次数，不用于挑选）', '',
              '| 对象 | 剔除发行人 | 其累计盈亏占总盈亏 | 10bp 超额 前 → 后 | 5bp 超额 前 → 后 | 相对 A90 差 10bp 前 → 后（A90 同样剔除自身头号） |',
              '|---|---|---|---|---|---|']
    for r in stress:
        after = '' if r['paired_vs_A90_after_10bps'] == '' else pc(r['paired_vs_A90_after_10bps'])
        lines.append(f"| {r['name']} | {r['issuer']} | {pc(r['share_of_total_profit'], 1)} | "
                     f"{pc(r['excess_10bps_before'])} → {pc(r['excess_10bps_after'])} | "
                     f"{pc(r['excess_5bps_before'])} → {pc(r['excess_5bps_after'])} | "
                     f"{pc(r['paired_vs_A90_before_10bps'])} → {after or '—'} |")
    lines += ['', '## 九、口径说明', '',
              '- 超额：年化超额 = 策略 CAGR 减 VOO CAGR（stage4_summary.interval 与 run_metrics）。',
              '- 随机对照：每个配置 100 次（种子 0–99），退出只数取自该配置自己的 10 bp 探索档；百分位为“不高于随机结果的比例”；下界/上界按停止运行的假设计算（本轮无停止，二者与点估计相同）。',
              '- 配对差：本配置减锚点，同一成本、同一时段；时段 A 为 2019-11-29 至 2021-12-31，时段 B 为 2021-12-31 之后至 2024-06-28。',
              '- 逐年剔除一年：用其余三个年份（2020–2023 中）复利得到策略与 VOO 的年化收益之差，统计为正的个数。',
              '- 区间估计：每周权益收益之差（相对 A90），移动块长 8 周，2000 次，种子 20261026；年化为周均差 × 52，与超额的 CAGR 口径不同，只作参考。',
              '- 集中度：每个交易日各大板块权重之和（按 positions 的市值除以总权益）取最大值。',
              '- 压力测试：剔除的是该发行人的全部证券类别，从每周的候选与已选名单中移除；其余设定不变；只跑探索档 5 bp 与 10 bp。',
              '- 所有数字来自已登记的运行与评估脚本 leadtrend_v05/r1_evaluation.py，没有新增策略配置。', '']
    (out / 'discussion_pack.md').write_text('\n'.join(lines), encoding='utf-8')
    return out / 'discussion_pack.md'

if __name__ == '__main__':
    raise SystemExit(main())
