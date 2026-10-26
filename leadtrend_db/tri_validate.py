"""Independent validation of tri_daily (method event_v1).

(a) Against the vendor rehabilitation factors: the adjusted close of a session is close*A+B with the factors of the
    first event after that session; its daily return must equal tri_daily.daily_return.
(b) Against the v05 normalised archive from 2019-05-01: the same formula computed from data/v05/normalized/prices.csv
    and events.csv (read only).

Both comparisons report the difference distribution and every security-day above 0.5%.
"""
from __future__ import annotations

import bisect
import csv
import json
from collections import defaultdict
from pathlib import Path

from .sources import ROOT, V05_NORMALIZED, DB_PATH

OUT = ROOT / 'work' / 'bt_v1' / 'tri_validation.json'
REHAB_DIR = ROOT / 'data' / 'db' / 'raw' / 'opend'


def _num(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def vendor_series(rehab_rows, prev_close_of):
    """Vendor adjustment as a pure multiplier per event.

    The vendor stores an affine adjustment (x -> A*x + B). Composing the affine maps over many events damps later
    returns, so each event is restated as the equivalent multiplier A' = (A*prev_close + B)/prev_close on its own
    ex-date and the chain is multiplicative. That reproduces the vendor's own intent (its A-only events are already
    multipliers) without the additive drift.
    """
    events = {}
    for row in rehab_rows:
        day = str(row.get('ex_div_date', ''))[:10]
        factor = _num(row.get('forward_adj_factorA'))
        if factor is None:
            continue
        offset = _num(row.get('forward_adj_factorB')) or 0.0
        prev_close = prev_close_of(day)
        key = (day, factor, offset)
        if key in events:
            continue
        if prev_close and abs(offset) > 1e-12:
            events[key] = (factor * prev_close + offset) / prev_close
        else:
            events[key] = factor
    ordered = sorted(events.items())
    days = [k[0] for k, _ in ordered]
    n = len(ordered)
    suffix = [1.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix[i] = ordered[i][1] * suffix[i + 1]

    def adj(day, close):
        k = bisect.bisect_right(days, day)
        return close * suffix[k]
    return adj


def load_rehab(code: str) -> list[dict]:
    path = next(REHAB_DIR.glob(f'*/rehab_{code}.json'), None)
    if path is None:
        return []
    return json.loads(path.read_text(encoding='utf8')).get('data') or []


def compare_vendor(conn) -> dict:
    cash_days = defaultdict(set)
    for row in conn.execute("""select security_id, action_date from corporate_actions
                               where action_type in ('cash_dividend','special_dividend')"""):
        cash_days[row[0]].add(row[1])
    closes = defaultdict(dict)
    for row in conn.execute('select security_id, session_date, close from prices_daily'):
        closes[row[0]][row[1]] = row[2]

    def prev_close_of(sid):
        def inner(day):
            prior = [d for d in closes.get(sid, {}) if d < day]
            return closes[sid][max(prior)] if prior else None
        return inner

    cache: dict[str, object] = {}
    records = []
    per_security = defaultdict(float)
    for sid, code in conn.execute("""select distinct p.security_id, l.opend_code from prices_daily p
                                     join price_lineage l using(security_id)"""):
        code = code.replace('US.', '')
        if code not in cache:
            cache[code] = vendor_series(load_rehab(code), prev_close_of(sid))
        adj = cache[code]
        rows = conn.execute("""select session_date, close, daily_return from tri_daily
                               where security_id=? order by session_date""", (sid,)).fetchall()
        for prev, cur in zip(rows, rows[1:]):
            if not prev[1] or not cur[1] or cur[2] is None:
                continue
            vendor = adj(cur[0], cur[1]) / adj(prev[0], prev[1]) - 1
            diff = abs(cur[2] - vendor)
            records.append((cur[0] in cash_days[sid], diff))
            per_security[sid] = max(per_security[sid], diff)
    diffs = sorted(d for _, d in records)
    total = len(diffs)
    cash = [d for flag, d in records if flag]
    plain = [d for flag, d in records if not flag]
    return dict(days=total, within_001=sum(1 for d in diffs if d < 0.0001),
                within_001_pct=(sum(1 for d in diffs if d < 0.0001) / total * 100) if total else 0.0,
                cash_event_days=len(cash), cash_event_days_within_001=sum(1 for d in cash if d < 0.0001),
                non_cash_days=len(plain), non_cash_within_001=sum(1 for d in plain if d < 0.0001),
                non_cash_within_001_pct=(sum(1 for d in plain if d < 0.0001) / len(plain) * 100) if plain else 0.0,
                above_05pct=sum(1 for d in diffs if d > 0.005), max_difference=diffs[-1] if diffs else 0.0,
                median_difference=diffs[total // 2] if total else 0.0,
                worst_securities=sorted(per_security.items(), key=lambda kv: -kv[1])[:10])


def load_v05_events():
    events = defaultdict(list)
    path = V05_NORMALIZED / 'events.csv'
    if not path.exists():
        return events
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            events[row['security_id']].append(row)
    return events


def compare_v05(conn, start='2019-05-01') -> dict:
    prices_path = V05_NORMALIZED / 'prices.csv'
    if not prices_path.exists():
        return dict(status='v05 normalized archive missing')
    events = load_v05_events()
    series = defaultdict(dict)
    with prices_path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            series[row['security_id']][row['session_date']] = float(row['close'])
    diffs = []
    worst = []
    for sid, bars in series.items():
        ours = dict(conn.execute("""select session_date, daily_return from tri_daily
                                    where security_id=? and session_date>=?""", (sid, start)))
        if not ours:
            continue
        by_day = defaultdict(list)
        for row in events.get(sid, []):
            by_day[row['effective_session']].append(row)
        days = sorted(bars)
        for prev, cur in zip(days, days[1:]):
            if cur < start or cur not in ours:
                continue
            split, cash, spin = 1.0, 0.0, 0.0
            for row in by_day.get(cur, []):
                kind = row['event_type']
                if kind in ('split', 'reverse_split') and row['shares_new_per_old']:
                    split *= float(row['shares_new_per_old'])
                elif kind in ('cash_dividend', 'special_dividend') and row['cash_amount_per_share']:
                    cash += float(row['cash_amount_per_share'])
                elif kind == 'spinoff' and row['spinoff_value_per_old_share']:
                    spin += float(row['spinoff_value_per_old_share'])
            expected = (bars[cur] * split + cash + spin) / bars[prev] - 1
            diff = abs(expected - ours[cur])
            diffs.append(diff)
            if diff > 0.005:
                worst.append(dict(security_id=sid, session_date=cur, db=ours[cur], v05=expected))
    diffs_sorted = sorted(diffs)
    diff_flags = [d for d in diffs]
    diffs.sort()
    total = len(diffs)
    return dict(status='compared', start=start, days=total,
                within_001=sum(1 for d in diffs if d < 0.0001),
                within_001_pct=(sum(1 for d in diffs if d < 0.0001) / total * 100) if total else 0.0,
                above_05pct=len([d for d in diffs if d > 0.005]), max_difference=diffs[-1] if diffs else 0.0,
                median_difference=diffs[total // 2] if total else 0.0, examples=worst[:20])


def run(db_path: Path = DB_PATH) -> dict:
    import sqlite3
    conn = sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro', uri=True)
    try:
        result = dict(vendor=compare_vendor(conn), v05=compare_v05(conn))
    finally:
        conn.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding='utf8', newline='\n')
    return result


if __name__ == '__main__':
    print(json.dumps(run(), indent=2, ensure_ascii=False, default=str))
