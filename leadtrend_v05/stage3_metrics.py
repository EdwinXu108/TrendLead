"""Metrics for an already bounded development run, with explicit time definitions."""
from collections import defaultdict
from datetime import date
import math
import statistics

def longest_drawdown(values, dates):
    high=values[0];peak_date=dates[0];under=None;maximum=0.;longest=0;recovered=[]
    worst_peak=worst_trough=None;worst_peak_value=None;recovery_date=None
    for x,d in zip(values,dates):
        drawdown=x/high-1
        if drawdown<maximum:
            maximum=drawdown;worst_peak=peak_date;worst_trough=d;worst_peak_value=high;recovery_date=None
        if worst_trough is not None and d>worst_trough and recovery_date is None and x>=worst_peak_value:
            recovery_date=d
        if x>=high:
            if under is not None:
                recovered.append((d-under).days);longest=max(longest,(d-under).days);under=None
            high=x
            peak_date=d
        elif under is None:under=peak_date
    if under is not None:longest=max(longest,(dates[-1]-under).days)
    return {'max_drawdown':maximum,'longest_underwater_calendar_days':longest,
            'max_completed_recovery_calendar_days':max(recovered,default=0),'unrecovered_at_end':under is not None,
            'maximum_drawdown_peak_date':worst_peak.isoformat() if worst_peak else None,
            'maximum_drawdown_trough_date':worst_trough.isoformat() if worst_trough else None,
            'maximum_drawdown_recovery_date':recovery_date.isoformat() if recovery_date else None,
            'maximum_drawdown_recovery_from_trough_days':(recovery_date-worst_trough).days if recovery_date else None}

def summarize(equity, benchmark, trades, weeks, contributions, initial, start, end):
    values=[float(r['total_equity']) for r in equity];dates=[date.fromisoformat(r['session_date']) for r in equity]
    base={r['session_date']:float(r['benchmark_value']) for r in benchmark}
    paired=[r for r in equity if r['session_date'] in base]
    rd=[date.fromisoformat(r['session_date']) for r in paired]
    rel=[float(r['total_equity'])/base[r['session_date']] for r in paired]
    endvalue=values[-1];years=(end-start).days/365.25
    strategy_return=endvalue/initial-1;benchmark_return=float(benchmark[-1]['benchmark_value'])/initial-1
    week_last={r['signal_session_date']:r for r in weeks}
    weekly=[r for r in equity if r['session_date'] in week_last and r['session_date'] in base]
    negative_run=max_negative=0;rolling_rows=[]
    for i,r in enumerate(weekly):
        if i<52:continue
        prev=weekly[i-52]
        relative=(float(r['total_equity'])/float(prev['total_equity']))/(base[r['session_date']]/base[prev['session_date']])-1
        negative_run=negative_run+1 if relative<0 else 0;max_negative=max(max_negative,negative_run)
        rolling_rows.append({'session_date':r['session_date'],'rolling_52_week_relative_return':relative})
    gross=sum(abs(float(t['shares'])*float(t['reference_price'])) for t in trades)
    avg=statistics.mean(values)
    durations=[];opened={}
    for t in trades:
        sid=t['security_id'];d=date.fromisoformat(t['session_date'])
        if t['side']=='buy' and float(t['shares_before'])==0:opened[sid]=d
        if t['side']=='sell' and float(t['shares_after'])==0 and sid in opened:durations.append((d-opened.pop(sid)).days/7)
    censored=[(end-d).days/7 for d in opened.values()]
    small=[t for t in trades if abs(float(t['shares'])*float(t['fill_price']))<500]
    contribution_rows=sorted([{'security_id':sid,'profit_dollars':value,'return_contribution_percentage_points':value/initial*100} for sid,value in contributions.items()],key=lambda r:-r['profit_dollars'])
    summary={'status':'completed','dataset_kind':'real_partial','experiment_label':'真实数据近似实验：存在幸存者偏差、当前行业分类、工程默认参数未经验证；仅开发段，不构成策略有效性结论',
        'comparison_start_session':start.isoformat(),'comparison_end_session':end.isoformat(),
        'initial_capital':initial,'final_equity':endvalue,'total_return':strategy_return,
        'annualized_return':(endvalue/initial)**(1/years)-1 if years>0 else None,
        'benchmark_total_return':benchmark_return,'cumulative_difference_vs_voo_percentage_points':(strategy_return-benchmark_return)*100,
        'relative_nav_cumulative_return':(1+strategy_return)/(1+benchmark_return)-1,
        'drawdown':longest_drawdown([initial]+values,[start]+dates),
        'relative_nav_underwater':longest_drawdown([1.]+rel,[start]+rd),
        'longest_negative_rolling_52_week_relative_return_weeks':max_negative,
        'annual_turnover_two_sided_notional_over_mean_equity':gross/avg/years,
        'annual_turnover_one_way_half_two_sided':gross/2/avg/years,
        'n_trades':len(trades),'average_completed_holding_weeks':statistics.mean(durations) if durations else None,
        'average_all_holding_spells_weeks_including_right_censored':statistics.mean(durations+censored) if durations+censored else None,
        'completed_holding_spells':len(durations),'open_right_censored_spells':len(censored),
        'fees_total':sum(float(t['fee']) for t in trades),'sub_500_dollar_trades':len(small),'sub_500_dollar_trade_fees':sum(float(t['fee']) for t in small),
        'slippage_cost_dollars':sum(float(t['shares'])*abs(float(t['fill_price'])-float(t['reference_price'])) for t in trades),
        'sub_500_dollar_trade_slippage_cost':sum(float(t['shares'])*abs(float(t['fill_price'])-float(t['reference_price'])) for t in small),
        'top_five_return_contributions':contribution_rows[:5],
        'contribution_reconciliation_error':sum(contributions.values())-(endvalue-initial),
        'no_parameter_search':True,'no_holdout_computed':True}
    return summary,rolling_rows,contribution_rows
