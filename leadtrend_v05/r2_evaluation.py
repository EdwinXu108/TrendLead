"""Round 2 evaluation (plan 8.2-8.3): noise ruler, issuer exclusion, experiment-level random controls, paired bootstrap
against each anchor, board-cap curve, the 8.3 criteria, the reproduction table and the discussion pack. Numbers only.

Inputs: the committed registration research/v05/experiments/R2_robustness.json, the R2 run folder (scorecard, pairs,
explore, daily and null runs) and the robustness folders written by leadtrend_v05.robustness. Outputs go to
research/v05/experiments/R2_robustness/robustness/ and discussion_pack.md beside them.

Usage: python -X utf8 -m leadtrend_v05.r2_evaluation --run work/runs/experiments/R2_robustness \
           --robust work/runs/robustness --out research/v05/experiments/R2_robustness
"""
import argparse
import csv
import json
from pathlib import Path
from statistics import mean

import numpy as np

from .experiments import A90_ID, _metrics_or_none, load_spec, registered_points
from .perf_artifacts import read_table
from .r1_evaluation import _f, _write, bootstrap, experiment_random, top_issuer, weekly_series

SPEC = 'research/v05/experiments/R2_robustness.json'
R1_RUN = 'work/runs/experiments/R1_mechanisms'
R1_EVAL = 'research/v05/experiments/R1_mechanisms/evaluation'
NOISE = ['A90', 'WGT', 'SEC3', 'SIZE', 'G', 'C_SEC3_SIZE', 'C_SEC3_G']  # the seven configurations of 8.2 (1)-(2)
NOISE_ANCHOR = {'A90': None, 'WGT': 'A90', 'SEC3': 'A90', 'SIZE': 'A90', 'G': 'A90', 'C_SEC3_SIZE': 'SEC3', 'C_SEC3_G': 'SEC3'}
COMBOS = ('C_SEC3_SIZE', 'C_SEC3_G')
REPRO = {'SIZE': ('R_SIZE_L126', 'R_SIZE_N8', 'R_SIZE_N12'), 'G': ('R_G_L126', 'R_G_N8', 'R_G_N12'),
         'SEC': ('R_SEC_L126', 'R_SEC_N8', 'R_SEC_N12')}
SETTINGS = ('L126', 'N8', 'N12')
CAP_CURVE = (('SEC2', 2), ('SEC3', 3), ('SEC4', 4), ('A90', None))
ISSUER = 'LEI:549300S4KLFTLO7GSQ80'  # NVIDIA, CUSIP:67066G104: the top contributor of round 1
STAGE4_NAMES = {'L090_N10_b2_R2': 'A90', 'L126_N10_b2_R2': 'A126', 'L090_N08_b2_R2': 'BASE_N08',
                'L090_N12_b2_R2': 'BASE_N12', 'L060_N10_b2_R2': 'A60'}
ROUND1_STRESS = {'A90': 'A90', 'G': 'G', 'SIZE': 'SIZE'}  # round 1 stress rows that the issuer runs must reproduce
ROUND1_NAMES = ('WGT', 'SEC3', 'SIZE', 'G', 'SEC2')  # round 1 configurations that round 2 references or anchors to


def _pc(v, nd=2):
    return '' if v in ('', None) else f'{100 * float(v):.{nd}f}%'


def _r2(v):
    return '' if v in ('', None) else f'{float(v):.2f}'


def _rows(path):
    with Path(path).open(encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def _spread(values):
    return dict(mean=mean(values) if values else '', min=min(values) if values else '', max=max(values) if values else '',
                range=(max(values) - min(values)) if values else '')


def noise_rows(robust, ids):
    rows = []
    for name in NOISE:
        pid, anchor = ids[name], NOISE_ANCHOR[name]
        aid = ids[anchor] if anchor else None
        for k in range(5):
            m = _metrics_or_none(robust / 'offsets' / f'offset_{k}' / pid / 'cost_10bps')
            a = _metrics_or_none(robust / 'offsets' / f'offset_{k}' / aid / 'cost_10bps') if aid else None
            rows.append(dict(name=name, id=pid, anchor=anchor or '', start_offset=k,
                             excess_10bps=m['annualized_excess'] if m else '',
                             anchor_excess_10bps=a['annualized_excess'] if a else '',
                             paired_10bps=(m['annualized_excess'] - a['annualized_excess']) if (m and a) else ''))
    return rows


def noise_summary(rows):
    out = []
    for name in NOISE:
        r = [x for x in rows if x['name'] == name]
        ex = [x['excess_10bps'] for x in r if x['excess_10bps'] != '']
        pr = [x['paired_10bps'] for x in r if x['paired_10bps'] != '']
        later = [x['paired_10bps'] for x in r if x['paired_10bps'] != '' and x['start_offset'] >= 1]
        e, p = _spread(ex), _spread(pr)
        out.append(dict(name=name, excess_mean=e['mean'], excess_min=e['min'], excess_max=e['max'], excess_range=e['range'],
                        paired_mean=p['mean'], paired_min=p['min'], paired_max=p['max'], paired_range=p['range'],
                        paired_k1_4_mean=mean(later) if later else '', paired_k1_4_positive=sum(1 for v in later if v > 0),
                        paired_k1_4_count=len(later)))
    return out


def consistency(run, robust, ids):
    """Identity checks: the k = 0 runs equal the R2 explore runs, and round 2 reproduces round 1's explore runs."""
    rows = []
    for name in NOISE:
        pid = ids[name]
        main = run / 'explore' / pid / 'cost_10bps' / 'summary.json'
        k0 = robust / 'offsets' / 'offset_0' / pid / 'cost_10bps' / 'summary.json'
        r1 = Path(R1_RUN) / 'explore' / pid / 'cost_10bps' / 'summary.json'
        rows.append(dict(name=name, id=pid,
                         offset0_equals_r2_explore=(k0.read_bytes() == main.read_bytes()) if k0.exists() and main.exists() else '',
                         r2_explore_equals_r1_explore=(main.read_bytes() == r1.read_bytes()) if r1.exists() and main.exists() else ''))
    return rows


def issuer_check(run, robust, ids):
    """Round 1's stress rows against the issuer runs at 10 bp: the same numbers, to the last digit."""
    stress = {r['name']: r for r in _rows(Path(R1_EVAL) / 'stress_issuer_exclusion.csv')}
    out = []
    for name, r1name in ROUND1_STRESS.items():
        after = _metrics_or_none(robust / 'issuer' / ids[name] / 'cost_10bps')
        r1_value = float(stress[r1name]['excess_10bps_after'])
        out.append(dict(name=name, round1_excess_10bps_after=r1_value, round2_excess_10bps_after=after['annualized_excess'],
                        difference=after['annualized_excess'] - r1_value))
    return out


def issuer_rows(run, robust, ids):
    rows = []
    for name in NOISE:
        pid, anchor = ids[name], NOISE_ANCHOR[name]
        aid = ids[anchor] if anchor else None
        for cost in (5, 10):
            before = _metrics_or_none(run / 'explore' / pid / f'cost_{cost}bps')
            after = _metrics_or_none(robust / 'issuer' / pid / f'cost_{cost}bps')
            ab = _metrics_or_none(run / 'explore' / aid / f'cost_{cost}bps') if aid else None
            aa = _metrics_or_none(robust / 'issuer' / aid / f'cost_{cost}bps') if aid else None
            rows.append(dict(
                name=name, id=pid, anchor=anchor or '', cost_bps=cost,
                excess_before=before['annualized_excess'] if before else '', excess_after=after['annualized_excess'] if after else '',
                anchor_before=ab['annualized_excess'] if ab else '', anchor_after=aa['annualized_excess'] if aa else '',
                paired_before=(before['annualized_excess'] - ab['annualized_excess']) if (before and ab) else '',
                paired_after=(after['annualized_excess'] - aa['annualized_excess']) if (after and aa) else ''))
    return rows


def issuer_profit_share(root, run, pid, issuer):
    issuer_of = {r['security_id']: r.get('issuer_id', '') for r in read_table(root / 'data/v05/normalized/securities.csv')}
    profit, total = 0.0, 0.0
    for r in read_table(run / 'daily' / pid / 'cost_10bps' / 'daily_contributions.csv'):
        v = float(r['profit_dollars'])
        total += v
        if issuer_of.get(r['security_id'], r['security_id']) == issuer:
            profit += v
    return profit / total if total else None


def top_contributors(root, run, ids):
    rows = []
    for name in NOISE:
        pid = ids[name]
        issuer, profit, total = top_issuer(root, run, pid)
        rows.append(dict(name=name, id=pid, top_issuer=issuer, top_issuer_profit=profit, total_profit=total,
                         top_issuer_share=profit / total if total else None))
    return rows


def cap_rows(sc, ids):
    rows = []
    for name, cap in CAP_CURVE:
        r = sc[ids[name]]
        rows.append(dict(name=name, sector_cap=cap if cap else 'none', excess_10bps=r['annualized_excess_10bps'],
                         ret_over_vol=r['ret_over_vol'], max_drawdown=r['max_drawdown'], dd_2022=r['dd_2022'],
                         sector_over_40_share=r['sector_over_40_share'], sector_over_50_share=r['sector_over_50_share'],
                         max_single_weight=r['max_single_weight']))
    return rows


def criteria83(sc, ids, nz):
    rows = []
    for name in COMBOS:
        r = sc[ids[name]]
        p10, p5 = _f(r['paired_vs_anchor_10bps']), _f(r['paired_vs_anchor_5bps'])
        pa, pb = _f(r['paired_period_a']), _f(r['paired_period_b'])
        loyo = int(r['leave_one_year_out_positive'])
        low, point = _f(r['null_percentile_low']), _f(r['null_percentile'])
        n = next(x for x in nz if x['name'] == name)
        c1 = p10 is not None and p10 >= 0.01 and p5 is not None and p5 > 0
        c2 = pa is not None and pb is not None and not (pa * pb < 0)
        c3 = loyo >= 3
        c4 = low is not None and low >= 75
        c5 = n['paired_k1_4_mean'] != '' and n['paired_k1_4_mean'] >= 0.01 and n['paired_k1_4_positive'] >= 3
        rows.append(dict(name=name, paired_10bps=p10, paired_5bps=p5, period_a=pa, period_b=pb, loyo_positive=loyo,
                         null_percentile=point, null_percentile_low=low, noise_paired_mean_k1_4=n['paired_k1_4_mean'],
                         noise_positive_k1_4=n['paired_k1_4_positive'],
                         c1_gap_ge_1pp_and_5bp_positive=c1, c2_periods_not_opposite=c2, c3_loyo_ge_3=c3,
                         c4_null_lower_ge_75=c4, c5_noise_mean_ge_1pp_and_3_positive=c5, all_five=c1 and c2 and c3 and c4 and c5))
    return rows


def reproduction(sc, ids):
    setting_rows, summary = [], []
    for mech, names in REPRO.items():
        paired, sec50 = [], []
        for setting, name in zip(SETTINGS, names):
            r = sc[ids[name]]
            p = _f(r['paired_vs_anchor_10bps'])
            paired.append(p)
            sec50.append(_f(r['sector_over_50_share']))
            setting_rows.append(dict(mechanism=mech, setting=setting, name=name, anchor=r['anchor'], paired_10bps=p,
                                     sector_over_50_share=_f(r['sector_over_50_share'])))
        positive = sum(1 for v in paired if v is not None and v > 0)
        lowest = min(v for v in paired if v is not None)
        concentration_ok = all(v <= 0.05 for v in sec50) if mech == 'SEC' else None
        reproduced = positive >= 2 and lowest >= -0.02 and (concentration_ok is not False)
        summary.append(dict(mechanism=mech, positive_settings=positive, lowest_paired_10bps=lowest,
                            concentration_ok_each_setting=concentration_ok if concentration_ok is not None else '',
                            reproduced=reproduced))
    return setting_rows, summary


def write_pack(out, data):
    lines = ['# R2_robustness 讨论资料包', '',
             '只含数字与逐条核对结果，不含结论。开发段（2019-11-29 至 2024-06-28），幸存者偏差与当前行业分类仍存在。'
             '不含个股价格序列；个股层面只出现证券编号、发行人编号与名称。', '']
    lines += ['## 一、成绩单主表（探索档，10 bp 与 5 bp，相对 VOO 的年化超额）', '',
              '| 配置 | 名称 | 锚点 | 10bp 超额 | 5bp 超额 | 相对锚点 10bp | 相对锚点 5bp | 相对 A90 10bp | 年单边换手 | 最大回撤 | 随机百分位（点估计及下界） |',
              '|---|---|---|---|---|---|---|---|---|---|---|']
    for r in data['scorecard']:
        lines.append(f"| {r['id']} | {data['name'][r['id']]} | {data['anchor_name'][r['id']]} | {_pc(r['annualized_excess_10bps'])} | "
                     f"{_pc(r['annualized_excess_5bps'])} | {_pc(r['paired_vs_anchor_10bps'])} | {_pc(r['paired_vs_anchor_5bps'])} | "
                     f"{_pc(r['paired_vs_A90_10bps'])} | {_r2(r['turnover_one_way'])} | {_pc(r['max_drawdown'])} | "
                     f"{_pc(r['null_percentile'], 1)}（{_pc(r['null_percentile_low'], 1)}）|")
    lines += ['', '## 二、风险列（同一批 daily 运行，10 bp；相对 WGT 的差为本配置减 WGT）', '',
              '| 配置 | 名称 | 年化收益 | 年化波动 | β | 跟踪误差 | 收益/波动 | 收益/回撤 | 2022 年最大回撤 | 最大单股权重 | 单板块超40%天数 | 单板块超50%天数 | 相对WGT超额差 | 相对WGT波动差 | 相对WGT回撤差 | 相对WGT收益/波动差 |',
              '|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in data['scorecard']:
        lines.append(f"| {r['id']} | {data['name'][r['id']]} | {_pc(r['annualized_return_10bps'])} | {_pc(r['vol_annual'])} | {_r2(r['beta'])} | "
                     f"{_pc(r['tracking_error'])} | {_r2(r['ret_over_vol'])} | {_r2(r['ret_over_dd'])} | {_pc(r['dd_2022'])} | "
                     f"{_pc(r['max_single_weight'])} | {_pc(r['sector_over_40_share'], 1)} | {_pc(r['sector_over_50_share'], 1)} | "
                     f"{_pc(r['paired_vs_WGT_10bps'])} | {_pc(r['vol_vs_WGT'])} | {_pc(r['dd_vs_WGT'])} | {_r2(r['ret_over_vol_vs_WGT'])} |")
    lines += ['', '## 三、配对表（配置减锚点，年化超额之差；探索档）', '',
              '| 配对 | 10bp 差 | 5bp 差 | 时段A差(10bp) | 时段B差(10bp) |', '|---|---|---|---|---|']
    for r in data['pairs']:
        lines.append(f"| {r['pair']} | {_pc(r['diff_10bps'])} | {_pc(r['diff_5bps'])} | {_pc(r['diff_period_a'])} | {_pc(r['diff_period_b'])} |")
    lines += ['', '## 四、按方案 8.3 逐条核对（组合，相对 SEC3）', '',
              '| 配置 | 1: 10bp 配对差≥+1pp 且 5bp 为正 | 2: 两时段不相反 | 3: 逐年剔除一年后为正 ≥3 | 4: 随机百分位下界 ≥75% | 5: 噪音（k=1–4 配对差均值≥+1pp 且正的 ≥3） | 全部达到 |',
              '|---|---|---|---|---|---|---|']
    for c in data['criteria']:
        yn = lambda v: '达到' if v else '未达到'  # noqa: E731
        lines.append(f"| {c['name']} | {yn(c['c1_gap_ge_1pp_and_5bp_positive'])}（10bp {_pc(c['paired_10bps'])}，5bp {_pc(c['paired_5bps'])}） | "
                     f"{yn(c['c2_periods_not_opposite'])}（A {_pc(c['period_a'])}，B {_pc(c['period_b'])}） | "
                     f"{yn(c['c3_loyo_ge_3'])}（{c['loyo_positive']}/4） | {yn(c['c4_null_lower_ge_75'])}（点估计 {_pc(c['null_percentile'], 1)}，下界 {_pc(c['null_percentile_low'], 1)}） | "
                     f"{yn(c['c5_noise_mean_ge_1pp_and_3_positive'])}（均值 {_pc(c['noise_paired_mean_k1_4'])}，正 {c['noise_positive_k1_4']}/4） | "
                     f"{yn(c['all_five'])} |")
    lines += ['', '## 五、复现汇总（三个设定的 10bp 配对差；达到的标准：正的设定 ≥2 个，且没有低于 −2.0pp；SEC 另须每个设定单板块超50%天数 ≤5%）', '',
              '| 机制 | L126 配对差 | N=8 配对差 | N=12 配对差 | 正的设定数 | 最低配对差 | SEC 集中度（每个设定 ≤5%） | 复现 |',
              '|---|---|---|---|---|---|---|---|']
    by_mech = {}
    for s in data['repro_rows']:
        by_mech.setdefault(s['mechanism'], {})[s['setting']] = s
    for m in data['repro']:
        cells = by_mech[m['mechanism']]
        lines.append(f"| {m['mechanism']} | {_pc(cells['L126']['paired_10bps'])} | {_pc(cells['N8']['paired_10bps'])} | "
                     f"{_pc(cells['N12']['paired_10bps'])} | {m['positive_settings']} | {_pc(m['lowest_paired_10bps'])} | "
                     f"{'' if m['concentration_ok_each_setting'] == '' else ('是' if m['concentration_ok_each_setting'] else '否')} | "
                     f"{'达到' if m['reproduced'] else '未达到'} |")
    lines += ['', '## 六、噪音尺（起点推迟 k 周；k = 0 为原起点；10bp 探索档）', '',
              '| 配置 | k=0 | k=1 | k=2 | k=3 | k=4 | 均值 | 最小 | 最大 | 极差 |', '|---|---|---|---|---|---|---|---|---|---|']
    for name in NOISE:
        ex = {r['start_offset']: r['excess_10bps'] for r in data['noise'] if r['name'] == name}
        s = next(x for x in data['noise_summary'] if x['name'] == name)
        lines.append(f"| {name} | " + ' | '.join(_pc(ex[k]) for k in range(5)) +
                     f" | {_pc(s['excess_mean'])} | {_pc(s['excess_min'])} | {_pc(s['excess_max'])} | {_pc(s['excess_range'])} |")
    lines += ['', '配对差（相对各自锚点，同一起点）：', '', '| 配置 | k=0 | k=1 | k=2 | k=3 | k=4 | 均值 | 最小 | 最大 | 极差 |', '|---|---|---|---|---|---|---|---|---|---|']
    for name in NOISE:
        if NOISE_ANCHOR[name] is None:
            continue
        pr = {r['start_offset']: r['paired_10bps'] for r in data['noise'] if r['name'] == name}
        s = next(x for x in data['noise_summary'] if x['name'] == name)
        lines.append(f"| {name} | " + ' | '.join(_pc(pr[k]) for k in range(5)) +
                     f" | {_pc(s['paired_mean'])} | {_pc(s['paired_min'])} | {_pc(s['paired_max'])} | {_pc(s['paired_range'])} |")
    lines += ['', '组合在 k = 1–4 下的配对差：'] + [
        f"- {x['name']}：均值 {_pc(x['paired_k1_4_mean'])}，为正的起点 {x['paired_k1_4_positive']}/{x['paired_k1_4_count']}"
        for x in data['noise_summary'] if x['name'] in COMBOS]
    lines += ['', '## 七、剔除 NVIDIA（发行人 LEI:549300S4KLFTLO7GSQ80，全部证券类别从每周股票池中移除）', '',
              '| 配置 | 成本 | 剔除前超额 | 剔除后超额 | 锚点（剔除前） | 锚点（剔除后） | 相对锚点差（剔除前） | 相对锚点差（剔除后） |',
              '|---|---|---|---|---|---|---|---|']
    for r in data['issuer']:
        lines.append(f"| {r['name']} | {r['cost_bps']} bp | {_pc(r['excess_before'])} | {_pc(r['excess_after'])} | "
                     f"{_pc(r['anchor_before'])} | {_pc(r['anchor_after'])} | {_pc(r['paired_before'])} | {_pc(r['paired_after'])} |")
    lines += ['', '各配置自己的头号贡献发行人（10 bp daily 运行的累计盈亏）：', '',
              '| 配置 | 头号贡献发行人 | 其累计盈亏 | 占总盈亏 | NVIDIA 占总盈亏 |', '|---|---|---|---|---|']
    for t in data['top']:
        lines.append(f"| {t['name']} | {t['top_issuer']} | {t['top_issuer_profit']:.0f} | {_pc(t['top_issuer_share'], 1)} | "
                     f"{_pc(data['nvda_share'][t['id']], 1)} |")
    lines += ['', '## 八、板块上限曲线（N=10，L=90；上限 2、3、4 与不设上限）', '',
              '| 名称 | 板块上限 | 10bp 超额 | 收益/波动 | 最大回撤 | 2022 年最大回撤 | 单板块超40%天数 | 单板块超50%天数 | 最大单股权重 |',
              '|---|---|---|---|---|---|---|---|---|']
    for c in data['cap']:
        lines.append(f"| {c['name']} | {c['sector_cap']} | {_pc(c['excess_10bps'])} | {_r2(c['ret_over_vol'])} | {_pc(c['max_drawdown'])} | "
                     f"{_pc(c['dd_2022'])} | {_pc(c['sector_over_40_share'], 1)} | {_pc(c['sector_over_50_share'], 1)} | {_pc(c['max_single_weight'])} |")
    lines += ['', '## 九、实验级随机对照与区间估计', '',
              f"- 种子数 {data['random_summary']['seeds']}，每个种子 12 个配置全部完成：{data['random_summary']['seeds_with_all_12_controls']} 个。",
              f"- 实际 12 个配置中最高的 10bp 超额：{_pc(data['random_summary']['actual_best_excess_10bps'])}；"
              f"在 100 个“每种子取最大值”的随机分布中的百分位：{data['random_summary']['percentile_of_actual_best']:.1f}%。",
              f"- 配对版本（随机结果减各自锚点的实际超额）：实际最大值 {_pc(data['random_summary']['actual_best_paired_10bps'])}，"
              f"百分位 {data['random_summary']['percentile_of_actual_best_paired']:.1f}%。",
              f"- 随机最大值的中位数：超额 {_pc(data['random_summary']['null_best_excess_median'])}，配对 {_pc(data['random_summary']['null_best_paired_median'])}。",
              '', '| 配置 | 锚点 | 周数 | 年化配对差（周均差 × 52） | 5% | 95% |', '|---|---|---|---|---|---|']
    for r in data['bootstrap']:
        lines.append(f"| {r['name']} | {r['anchor']} | {r['weeks']} | {_pc(r['point_annualized_diff_vs_anchor'])} | {_pc(r['p05'])} | {_pc(r['p95'])} |")
    lines += ['', '## 十、工程核对', '',
              '| 配置 | 起点 0 的运行与主运行的 summary 逐字节相同 | R2 探索档与 R1 探索档的 summary 逐字节相同 |', '|---|---|---|']
    for c in data['consistency']:
        yn = lambda v: '' if v == '' else ('是' if v else '否')  # noqa: E731
        lines.append(f"| {c['name']} | {yn(c['offset0_equals_r2_explore'])} | {yn(c['r2_explore_equals_r1_explore'])} |")
    lines += ['', '| 第一轮剔除 NVIDIA 的 10bp 超额（原表） | 第二轮重跑 | 差 |', '|---|---|---|']
    for c in data['issuer_check']:
        lines.append(f"| {c['name']} {_pc(c['round1_excess_10bps_after'], 4)} | {_pc(c['round2_excess_10bps_after'], 4)} | {c['difference']:.2e} |")
    lines += ['', '## 十一、口径说明', '',
              '- 超额：年化超额 = 策略 CAGR 减 VOO CAGR（与成绩单相同）；配对差 = 本配置减锚点，同一成本、同一时段。',
              '- 锚点：A90 为 L090_N10_b2_R2，A126 为 L126_N10_b2_R2，BASE_N08、BASE_N12 为 N=8、N=12 的 L=90 默认配置；SEC3 为第一轮的 SEC3。',
              '- 起点推迟：开发段的第 k 个信号周（2019-11-29 起算，结束日 2024-06-28 不变）；k = 0 为原起点。配对差与锚点使用同一 k。',
              '- 剔除发行人：该发行人全部证券类别从每周的候选与已选名单中移除，其余设定不变，跑 5 bp 与 10 bp 探索档。',
              '- 风险列：来自 daily 运行；年化波动与跟踪误差按 252 个交易日；β 为相对 VOO 日收益的斜率；2022 年最大回撤的峰值取 2022 年内的最高点。',
              '- 随机对照：每个配置 100 次（种子 0–99），退出只数取自该配置自己的 10 bp 探索档；百分位为“不高于随机结果的比例”；本轮各点 100 次全部完成，下界与点估计相同。',
              '- 区间估计：每周权益收益之差（相对各自锚点），移动块长 8 周，2000 次，种子 20261026；年化为周均差 × 52，与超额的 CAGR 口径不同，只作参考，探索性。',
              '- 板块上限曲线的 SEC2、SEC3 为第一轮配置，在本轮作为参照重新运行；其余数字来自同一批运行。',
              '- 所有数字来自已登记的运行与评估脚本 leadtrend_v05/r2_evaluation.py，没有新增策略配置。', '']
    out.mkdir(parents=True, exist_ok=True)
    (out / 'discussion_pack.md').write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--run', required=True)
    ap.add_argument('--robust', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--spec', default=SPEC)
    args = ap.parse_args(argv)
    root = Path('.').resolve()
    run, robust = root / args.run, root / args.robust
    out = root / args.out / 'robustness'
    out.mkdir(parents=True, exist_ok=True)
    loaded = load_spec(root, args.spec)
    names12 = [p['name'] for p in loaded['grid']]
    pid_of = {p['name']: p['id'] for p in loaded['grid']}
    anchor_of = {p['name']: p['anchor'] for p in loaded['grid']}
    sc_rows = _rows(run / 'scorecard.csv')
    sc = {r['id']: r for r in sc_rows}
    registry = registered_points(root)
    # Names to IDs: the twelve configurations, round 1's named points (anchor or reference) and the stage 4 anchors.
    ids = dict(pid_of)
    ids.update({name: pid for pid, name in STAGE4_NAMES.items()})
    ids.update({p['name']: pid for pid, p in registry.items() if p.get('name') in ROUND1_NAMES})
    name_of = {pid: name for name, pid in ids.items()}
    anchor_name = {r['id']: name_of.get(r['anchor'], r['anchor']) for r in sc_rows}
    for n in NOISE + ['SEC2', 'SEC4', 'A90']:
        assert ids[n] in sc, n
    # 1. noise ruler and its summary
    nz_rows = noise_rows(robust, ids)
    nz_sum = noise_summary(nz_rows)
    _write(out / 'noise_ruler.csv', nz_rows)
    _write(out / 'noise_summary.csv', nz_sum)
    # 2. issuer exclusion (5 and 10 bp), top contributors, and the identity checks
    iss_rows = issuer_rows(run, robust, ids)
    _write(out / 'issuer_exclusion.csv', iss_rows)
    top = top_contributors(root, run, ids)
    nvda = {ids[n]: issuer_profit_share(root, run, ids[n], ISSUER) for n in NOISE}
    _write(out / 'top_contributors.csv', [dict(t, nvda_share=nvda[t['id']]) for t in top])
    cons = consistency(run, robust, ids)
    _write(out / 'consistency.csv', cons)
    issuer_chk = issuer_check(run, robust, ids)
    _write(out / 'issuer_check_round1.csv', issuer_chk)
    # 3. experiment-level random controls over the twelve configurations
    exc10 = {}
    for pid in sc:
        m = _metrics_or_none(run / 'explore' / pid / 'cost_10bps')
        if m:
            exc10[pid] = m['annualized_excess']
    rnd_rows, rnd_summary = experiment_random(run, pid_of, anchor_of, exc10, names=names12)
    _write(out / 'experiment_random_seeds.csv', rnd_rows)
    (out / 'experiment_random_summary.json').write_text(json.dumps(rnd_summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    # 4. paired bootstrap against each configuration's own anchor
    weeks = sorted({r['signal_session_date'] for r in read_table(run / 'explore' / A90_ID / 'cost_10bps' / 'weekly_checks.csv')})
    boot = []
    for n in names12:
        diff = weekly_series(run, pid_of[n], weeks) - weekly_series(run, anchor_of[n], weeks)
        draws = bootstrap(diff)
        boot.append(dict(name=n, anchor=anchor_name.get(anchor_of[n], anchor_of[n]), weeks=len(diff),
                         point_annualized_diff_vs_anchor=float(np.mean(diff)) * 52,
                         p05=float(np.percentile(draws, 5)), p95=float(np.percentile(draws, 95)),
                         method='moving block, block 8 weeks, 2000 draws, seed 20261026; annualised as weekly mean x 52; exploratory'))
    _write(out / 'bootstrap_paired_vs_anchor.csv', boot)
    # 5. the 8.3 criteria, the reproduction table, the cap curve
    crit = criteria83(sc, ids, nz_sum)
    _write(out / 'criteria_8_3.csv', crit)
    repro_rows, repro_sum = reproduction(sc, ids)
    _write(out / 'reproduction.csv', repro_rows)
    _write(out / 'reproduction_summary.csv', repro_sum)
    cap = cap_rows(sc, ids)
    _write(out / 'cap_curve.csv', cap)
    # 6. discussion pack: copies of the registered tables, then the numbers
    pairs = _rows(run / 'pairs.csv')
    name_for = {pid: name_of.get(pid, '锚点/参照') for pid in sc}
    data = dict(scorecard=sc_rows, pairs=pairs, criteria=crit, repro=repro_sum, repro_rows=repro_rows, noise=nz_rows,
                noise_summary=nz_sum, issuer=iss_rows, top=top, nvda_share=nvda, cap=cap, random_summary=rnd_summary,
                bootstrap=boot, consistency=cons, issuer_check=issuer_chk, name=name_for, anchor_name=anchor_name)
    write_pack(root / args.out, data)
    print(json.dumps(dict(criteria=[c['all_five'] for c in crit], repro=[r['reproduced'] for r in repro_sum]), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
