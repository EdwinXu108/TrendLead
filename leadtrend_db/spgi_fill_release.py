"""Registered price fill for SPGI's 2016 rename gap (spgi_fill_v1).

OpenD returns no bars for 2016-04-28..2016-05-23, and its 2016-05-24 row is a synthetic
"previous close minus dividend" stub.  Yahoo Finance has the 17 sessions from 2016-04-29
onwards; its history is scaled by a spurious 1057:1000 split recorded on 2026-07-01, so the
raw bars are multiplied by 1.057 (verified to the cent on 2016-04-27 and 2016-05-25..27).

2016-04-28 itself has no real bar in either vendor; it is carried forward from the previous
close and flagged `placeholder_carry`, which keeps the cumulative path exact from 04-29 on.

The fill inserts 18 price rows, corrects 2016-05-24, rebuilds the security's TRI and refreshes
its two coverage tables.  Every other table is hashed before and after and must not change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from .build import content_hash

BASE_FILE = '83c71cbea1f3a3bbaf40405a2c9cbcae0b754d9e10cbf773beb85fc0d6d840ae'
BASE_CONTENT = '82426047a567879ab22e9e122395fe7b9dbc4adea2d9e28fafc0b011f9bd7b8d'
RELEASE = 'spgi_fill_v1'
CONFIG = Path('config/spgi_fill_v1.json')
TOUCHED = ('prices_daily', 'tri_daily', 'price_coverage', 'price_quarter_coverage', 'sources')
LAST_REBUILT = '2016-05-25'          # the last session whose return depends on a changed close


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8388608), b''):
            h.update(block)
    return h.hexdigest()


def rows_hash(conn, table: str) -> dict:
    cols = len(conn.execute(f'pragma table_info("{table}")').fetchall())
    order = ','.join(str(i) for i in range(1, cols + 1))
    h = hashlib.sha256()
    n = 0
    for row in conn.execute(f'select * from "{table}" order by {order}'):
        h.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode('utf8') + b'\n')
        n += 1
    return dict(rows=n, sha256=h.hexdigest())


def rebuild_tri(conn, sid: str, first: str, last_rebuilt: str) -> dict:
    calendar = [r[0] for r in conn.execute(
        "select session_date from price_calendar where session_date between ? and '2026-10-26' order by 1", (first,))]
    prices = {r[0]: r[1] for r in conn.execute(
        'select session_date, close from prices_daily where security_id=? and source=\'opend_none\'', (sid,))}
    cash = {}
    for day, amount in conn.execute("""select action_date, cash_amount from corporate_actions
                                       where security_id=? and action_type in ('cash_dividend','special_dividend')
                                         and action_date between ? and ?""", (sid, first, last_rebuilt)):
        cash[day] = cash.get(day, 0.0) + (amount or 0.0)
    splits = {day: ratio for day, ratio in conn.execute(
        "select action_date, ratio from corporate_actions where security_id=? and action_type in ('split','reverse_split') "
        "and action_date between ? and ?", (sid, first, last_rebuilt)) if ratio}
    spins = {day: value for day, value in conn.execute(
        "select action_date, spin_value from corporate_actions where security_id=? and action_type='spinoff' "
        "and action_date between ? and ?", (sid, first, last_rebuilt)) if value}
    old = {r[0]: dict(tri=r[1], ret=r[2]) for r in conn.execute(
        'select session_date, tri, daily_return from tri_daily where security_id=?', (sid,))}
    previous = [d for d in sorted(prices) if d < first]
    if not previous:
        raise ValueError(f'{sid}: no close before {first}')
    prev_day = previous[-1]
    prev_close = prices[prev_day]
    prev_tri = old[prev_day]['tri']
    built = {}
    for day in calendar:
        if day > last_rebuilt:
            break
        close = prices.get(day)
        if not close:
            raise ValueError(f'{sid} {day}: missing close inside the rebuild window')
        gross = (close * splits.get(day, 1.0) + cash.get(day, 0.0) + spins.get(day, 0.0)) / prev_close
        prev_tri = prev_tri * gross
        built[day] = dict(close=close, tri=prev_tri, ret=gross - 1.0)
        prev_close = close
    factor = built[last_rebuilt]['tri'] / old[last_rebuilt]['tri']
    return dict(built=built, factor=factor)


def refresh_coverage(conn, sid: str) -> dict:
    calendar = [r[0] for r in conn.execute('select session_date from price_calendar order by 1')]
    index = {d: i for i, d in enumerate(calendar)}
    prices = {r[0] for r in conn.execute("select session_date from prices_daily where security_id=? and source='opend_none'", (sid,))}
    rows = conn.execute('select * from price_quarter_coverage where security_id=? order by active_from', (sid,)).fetchall()
    quarters = []
    for row in rows:
        active = [d for d in calendar if row['active_from'] <= d <= row['active_to']]
        missing_active = [d for d in active if d not in prices]
        i = index[row['active_from']]
        warm = calendar[max(0, i - row['warmup_expected']):i]
        missing_warm = [d for d in warm if d not in prices]
        present = len(active) - len(missing_active)
        warm_present = len(warm) - len(missing_warm)
        status = 'complete' if present == row['expected_sessions'] and warm_present == row['warmup_expected'] else 'incomplete'
        conn.execute('update price_quarter_coverage set present_sessions=?, warmup_present=?, status=?, missing_dates=? '
                     'where version_id=? and security_id=?',
                     (present, warm_present, status,
                      json.dumps(dict(active=missing_active, warmup=missing_warm), separators=(',', ':')),
                      row['version_id'], sid))
        quarters.append(dict(version_id=row['version_id'], active_from=row['active_from'], active_to=row['active_to'],
                             expected_sessions=row['expected_sessions'], present_sessions=present, status=status,
                             warmup_expected=row['warmup_expected'], warmup_present=warm_present))
    coverage = conn.execute('select detail, status from price_coverage where security_id=?', (sid,)).fetchone()
    detail = json.loads(coverage[0])
    detail['active_missing_sessions'] = sum(len(q) for q in [json.loads(r[0])['active'] for r in
                                                            conn.execute('select missing_dates from price_quarter_coverage where security_id=?', (sid,))])
    detail['warmup_missing_sessions'] = sum(len(q) for q in [json.loads(r[0])['warmup'] for r in
                                                            conn.execute('select missing_dates from price_quarter_coverage where security_id=?', (sid,))])
    detail['quarters'] = quarters
    conn.execute("update price_coverage set n_days=?, detail=? where security_id=? and source='opend_none'",
                 (len(prices), json.dumps(detail, separators=(',', ':')), sid))
    return dict(rows=len(rows), active_missing=detail['active_missing_sessions'],
                warmup_missing=detail['warmup_missing_sessions'], n_days=len(prices))


def migrate(base: Path, out: Path, manifest: Path) -> dict:
    base, out, manifest = Path(base), Path(out), Path(manifest)
    if base.resolve() == out.resolve():
        raise ValueError('candidate must differ from the released database')
    if file_hash(base) != BASE_FILE:
        raise ValueError('released database file hash mismatch')
    if out.exists():
        raise FileExistsError(out)
    cfg = json.loads(CONFIG.read_text(encoding='utf8'))
    raw = Path('data/db/raw') / cfg['source']['raw_path']
    if file_hash(raw) != cfg['source']['sha256']:
        raise ValueError('archived Yahoo response does not match its hash')
    sid = cfg['security_id']
    fills = cfg['rows']

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base, out)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    conn.execute('pragma foreign_keys=on')
    meta = dict(conn.execute('select key,value from meta'))
    if meta.get('content_sha256') != BASE_CONTENT:
        raise ValueError('released database content hash mismatch')

    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' and name not in ('meta','build_log') "
        "and name not like 'sqlite_%' order by name")]
    untouched = [t for t in tables if t not in TOUCHED]
    before = {t: rows_hash(conn, t) for t in untouched}

    calendar = {r[0] for r in conn.execute('select session_date from price_calendar')}
    for row in fills:
        if row['date'] not in calendar:
            raise ValueError(f'{row["date"]} is not a session')
        if row['high'] < max(row['open'], row['close']) - 1e-6 or row['low'] > min(row['open'], row['close']) + 1e-6:
            raise ValueError(f'{row["date"]}: inconsistent OHLC')

    source = cfg['source']
    conn.execute("""insert or replace into sources(source_id,kind,url,raw_path,sha256,bytes,retrieved_at_utc,note)
                    values(?,?,?,?,?,?,?,?)""",
                 (source['source_id'], source['kind'], source['url'], source['raw_path'], source['sha256'],
                  source['bytes'], source['retrieved_at_utc'], source['note']))

    inserted, updated = 0, 0
    for row in fills:
        existing = conn.execute("select close from prices_daily where security_id=? and session_date=? and source='opend_none'",
                                (sid, row['date'])).fetchone()
        if existing:
            if row['date'] != '2016-05-24':
                raise ValueError(f'{row["date"]}: row already present')
            conn.execute("""update prices_daily set open=?,high=?,low=?,close=?,volume=?,source_id=?
                            where security_id=? and session_date=? and source='opend_none'""",
                         (row['open'], row['high'], row['low'], row['close'], row['volume'], source['source_id'],
                          sid, row['date']))
            updated += 1
        else:
            conn.execute("""insert into prices_daily(security_id,session_date,source,open,high,low,close,adj_close,
                            volume,turnover,source_id) values(?,?,'opend_none',?,?,?,?,NULL,?,NULL,?)""",
                         (sid, row['date'], row['open'], row['high'], row['low'], row['close'], row['volume'],
                          source['source_id']))
            inserted += 1

    rebuilt = rebuild_tri(conn, sid, fills[0]['date'], LAST_REBUILT)
    for day, item in rebuilt['built'].items():
        if day > '2016-05-24':
            continue
        kind = 'placeholder_carry' if day == '2016-04-28' else 'filled_yahoo'
        flags = 'cash,' + kind if conn.execute(
            "select 1 from corporate_actions where security_id=? and action_date=? and action_type in "
            "('cash_dividend','special_dividend')", (sid, day)).fetchone() else kind
        if conn.execute('select 1 from tri_daily where security_id=? and session_date=?', (sid, day)).fetchone():
            conn.execute('update tri_daily set close=?, tri=?, daily_return=?, flags=? where security_id=? and session_date=?',
                         (item['close'], item['tri'], item['ret'], flags, sid, day))
        else:
            conn.execute("insert into tri_daily(security_id,session_date,close,tri,daily_return,method,flags) "
                         "values(?,?,?,?,?,'event_v1',?)",
                         (sid, day, item['close'], item['tri'], item['ret'], flags))
    # 2016-05-25 keeps its old return chain from 05-26 on: rescale the level by the new 05-25 value
    conn.execute('update tri_daily set tri=?, daily_return=? where security_id=? and session_date=?',
                 (rebuilt['built'][LAST_REBUILT]['tri'], rebuilt['built'][LAST_REBUILT]['ret'], sid, LAST_REBUILT))
    conn.execute('update tri_daily set tri=tri*? where security_id=? and session_date>?',
                 (rebuilt['factor'], sid, LAST_REBUILT))
    conn.commit()

    coverage = refresh_coverage(conn, sid)
    integrity = conn.execute('pragma integrity_check').fetchone()[0]
    foreign = conn.execute('pragma foreign_key_check').fetchall()
    if integrity != 'ok' or foreign:
        raise RuntimeError('candidate integrity or foreign key check failed')
    after = {t: rows_hash(conn, t) for t in untouched}
    if before != after:
        raise RuntimeError(f'untouched tables changed: {[t for t in untouched if before[t] != after[t]]}')

    broken = conn.execute("""select count(*) from tri_daily t join tri_daily p
        on p.security_id=t.security_id and p.session_date=(select max(session_date) from tri_daily x
        where x.security_id=t.security_id and x.session_date<t.session_date)
        where t.security_id=? and t.daily_return is not null
          and abs(t.tri - p.tri*(1+t.daily_return)) > 1e-9*p.tri""", (sid,)).fetchone()[0]

    conn.row_factory = None
    digest = content_hash(conn)
    conn.execute("insert or replace into meta(key,value) values('price_fill_release',?)", (RELEASE,))
    conn.execute("insert or replace into meta(key,value) values('price_fill_config_sha256',?)", (file_hash(CONFIG),))
    conn.execute("insert or replace into meta(key,value) values('content_sha256',?)", (digest,))
    conn.commit()
    conn.close()

    result = dict(release=RELEASE, base_file_sha256=BASE_FILE, base_content_sha256=BASE_CONTENT,
                  candidate_file_sha256=file_hash(out), candidate_content_sha256=digest,
                  config_sha256=file_hash(CONFIG), security_id=sid, inserted=inserted, updated=updated,
                  tri_factor=rebuilt['factor'], coverage=coverage, broken_chain=broken,
                  untouched_tables=before, integrity=integrity, foreign_key_violations=len(foreign))
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf8')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args(argv)
    result = migrate(Path(args.base), Path(args.out), Path(args.manifest))
    print(json.dumps({k: v for k, v in result.items() if k != 'untouched_tables'}, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
