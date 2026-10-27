"""Total-return index (method event_v1) from original prices and the triaged corporate actions.

One row per security and session that has a raw close. tri starts at 1 on the first session of the series.

    daily_return_t = (close_t * split_factor_t + cash_t + spinoff_value_t) / close_{t-1} - 1

split_factor_t is the product of the share ratios of splits and reverse splits effective on t (the price is restated
to the old share count). cash_t is the sum of the per-share cash amounts with ex-date t. spinoff_value_t is the value
of the distributed entity per parent share on the ex-date: the documented ratio times the child close when the v05
archive has it, else the vendor price factor applied to the previous close, else the ex-date opening drop.

Events that fall on a non-session day are applied on the next session of that security. A security-day whose previous
available session is not the previous NYSE session carries the flag gap (the return then spans the missing sessions).
No adjusted price series is materialised; the vendor's factors are used only as value evidence and for validation.
"""
from __future__ import annotations
import bisect, json, math
from collections import defaultdict

from .sources import ROOT

METHOD = 'event_v1'
BENCH_ID = 'BENCH:VOO'
BENCH_CONFIG = ROOT / 'config' / 'db_benchmark_v1.json'


def _f(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def build_series(sessions: list[str], bars: dict, actions: list[dict], calendar: list[str],
                 start: str, end: str) -> list[dict]:
    """TRI rows of one security. bars: session -> dict(open, close). actions: final corporate_actions rows."""
    by_date = defaultdict(list)
    for row in actions:
        if row['action_date'] <= end:
            by_date[row['action_date']].append(row)
    event_days = sorted(by_date)
    idx = 0
    pending: list[dict] = []
    rows: list[dict] = []
    tri = 1.0
    prev_close = None
    prev_day = None
    for day in sessions:
        if day < start:
            while idx < len(event_days) and event_days[idx] <= day:
                idx += 1
            continue
        while idx < len(event_days) and event_days[idx] <= day:
            pending.extend(by_date[event_days[idx]])
            idx += 1
        bar = bars[day]
        close = bar['close']
        flags = []
        if prev_close is None:
            daily = None
            tri = 1.0
        else:
            split, cash, spin = 1.0, 0.0, 0.0
            for row in pending:
                kind = row['action_type']
                if kind in ('split', 'reverse_split') and row.get('ratio'):
                    split *= float(row['ratio'])
                    flags.append('split' if kind == 'split' else 'reverse_split')
                elif kind in ('cash_dividend', 'special_dividend') and row.get('cash_amount'):
                    cash += float(row['cash_amount'])
                    flags.append('cash')
                elif kind == 'class_distribution':
                    value = row.get('spin_value')
                    if value is None:
                        raise ValueError('class distribution requires verified child valuation')
                    spin += float(value)
                    flags.append('class_distribution_child')
                elif kind == 'spinoff':
                    value = row.get('spin_value')
                    if value is not None:
                        spin += float(value)
                        flags.append('spinoff_child')
                    elif row.get('vendor_factor') is not None:
                        spin += (1 - float(row['vendor_factor'])) * prev_close
                        flags.append('vendor_factor')
                    elif bar.get('open'):
                        spin += max(0.0, prev_close - bar['open'])
                        flags.append('price_drop_implied')
                    else:
                        flags.append('spinoff_unvalued')
            daily = (close * split + cash + spin) / prev_close - 1
            tri *= (1 + daily)
            if prev_day is not None:
                i = bisect.bisect_left(calendar, prev_day)
                if i + 1 >= len(calendar) or calendar[i + 1] != day:
                    flags.append('gap')
        rows.append(dict(security_id=None, session_date=day, close=close, tri=tri, daily_return=daily,
                         method=METHOD, flags=','.join(sorted(set(flags)))))
        prev_close = close
        prev_day = day
        pending = []
    return rows


def build_all(prices: dict, actions: list[dict], calendar: list[str], start: str, end: str) -> list[dict]:
    by_sid = defaultdict(list)
    for row in actions:
        by_sid[row['security_id']].append(row)
    out = []
    for sid in sorted(prices):
        bars = {day: dict(open=bar[0]['open'], close=bar[0]['close']) for day, bar in prices[sid].items()}
        sessions = sorted(bars)
        for row in build_series(sessions, bars, by_sid.get(sid, []), calendar, start, end):
            row['security_id'] = sid
            out.append(row)
    return out


# -- benchmark -----------------------------------------------------------------------------------------------------

def benchmark_inputs(store) -> tuple[dict, list[dict]]:
    """VOO original prices and actions from the archived benchmark responses."""
    cfg = json.loads(BENCH_CONFIG.read_text(encoding='utf8'))
    index = json.loads(store._read(cfg['index_url']))
    bars: dict[str, dict] = {}
    for url in index['kline']:
        payload = json.loads(store._read(url))
        if payload['ret'] != 0:
            raise RuntimeError('archived VOO kline has ret != 0')
        if payload.get('params', {}).get('autype') not in ('NONE', 'None'):
            raise ValueError('refusing non-NONE benchmark prices')
        for row in payload['data']:
            day = str(row['time_key'])[:10]
            if cfg['start'] <= day <= cfg['end']:
                bars[day] = dict(open=_f(row.get('open')), close=_f(row.get('close')),
                                 high=_f(row.get('high')), low=_f(row.get('low')),
                                 volume=_f(row.get('volume')), turnover=_f(row.get('turnover')))
    rehab = json.loads(store._read(index['rehab']))['data']
    dividends = (json.loads(store._read(index['dividends']))['data'] or {}).get('dividend_list', [])
    actions: dict[str, dict] = {}
    for row in rehab:
        day = str(row.get('ex_div_date', ''))[:10]
        if not (cfg['start'] <= day <= cfg['end']):
            continue
        split_pair = (row.get('split_base'), row.get('split_ert'))
        join_pair = (row.get('join_base'), row.get('join_ert'))
        if split_pair[0] and split_pair[1]:
            actions[(day, 'split')] = dict(security_id=BENCH_ID, action_date=day, action_type='split',
                                           ratio=float(split_pair[1]) / float(split_pair[0]), cash_amount=None,
                                           spin_value=None, vendor_factor=None,
                                           resolution='vendor_split', notes=f'opend_get_rehab:US.VOO')
        elif join_pair[0] and join_pair[1]:
            actions[(day, 'split')] = dict(security_id=BENCH_ID, action_date=day, action_type='reverse_split',
                                           ratio=float(join_pair[1]) / float(join_pair[0]), cash_amount=None,
                                           spin_value=None, vendor_factor=None,
                                           resolution='vendor_join',
                                           notes='opend_get_rehab:US.VOO; vendor series correction on 2013-10-24: '
                                                 'NONE prices before that date are half of the fund price')
        cash = _f(row.get('per_cash_div')) or 0.0
        special = _f(row.get('special_dividend')) or 0.0
        if cash + special > 0:
            actions[(day, 'cash')] = dict(security_id=BENCH_ID, action_date=day, action_type='cash_dividend',
                                          ratio=None, cash_amount=round(cash + special, 6), spin_value=None,
                                          vendor_factor=None, resolution='rehab_per_cash_div',
                                          notes='opend_get_rehab:US.VOO')
    for row in dividends:
        day = str(row.get('ex_date', '')).replace('/', '-')[:10]
        amount = _f(str(row.get('statement', '')).split('派息')[-1].replace('USD', '')) if row.get('statement') else None
        if amount and (day, 'cash') not in actions and cfg['start'] <= day <= cfg['end']:
            actions[(day, 'cash')] = dict(security_id=BENCH_ID, action_date=day, action_type='cash_dividend',
                                          ratio=None, cash_amount=round(amount, 6), spin_value=None,
                                          vendor_factor=None, resolution='dividend_endpoint_amount',
                                          notes='opend_dividends:US.VOO')
    return bars, sorted(actions.values(), key=lambda r: (r['action_date'], r['action_type']))


def build_benchmark(store, calendar: list[str], start: str, end: str) -> list[dict]:
    bars, actions = benchmark_inputs(store)
    sessions = sorted(bars)
    rows = build_series(sessions, bars, actions, calendar, start, end)
    for row in rows:
        row['security_id'] = BENCH_ID
    return rows
