"""Registered full-period data release for the 2024-07-01 TRI seam (tri_seam_v1).

Seventeen securities carry a total-return level that jumps at 2024-07-01 while their own `daily_return`
column stays correct. The largest are DD -25.2%, PGR +4.4%, CRH -2.3%, AEP +1.9%, HON +1.0%, CME +0.8%,
GE -1.4% and LIN (two securities) -1.2%; nine more move by 0.1-0.8%. The development segment was built
from one vendor snapshot and the holdout segment from another, so the two levels do not meet.

The fix rebuilds each security's TRI from the 2024-06-28 close forward with its own daily returns:
tri_t = tri_{t-1} * (1 + daily_return_t), to the last stored session. Nothing before the anchor changes.

The formal database is opened read-only and never written; the result is a new file. Every table is
hashed before and after: only tri_daily may differ, and inside tri_daily only those securities after
the anchor.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

from .build import content_hash

BASE_FILE = '4e46f93f6c7e836dbd5e30dfe3ecfd8ebbb076180ac2d0397c4f8789381862bc'
BASE_CONTENT = '4c6e53e9240e11f743577160f4c7dc818eba555677e11d450da5bd67641c173b'
RELEASE = 'tri_seam_v1'
CONFIG = Path('config/tri_seam_v1.json')
ANCHOR = '2024-06-28'
LAST_SESSION = '2026-10-26'
EXCLUDED = ('meta', 'build_log')


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8388608), b''):
            h.update(block)
    return h.hexdigest()


def _order(conn, table: str) -> str:
    cols = len(conn.execute(f'pragma table_info("{table}")').fetchall())
    return ','.join(str(i) for i in range(1, cols + 1))


def rows_hash(conn, table: str, where: str = '', params: tuple = ()) -> dict:
    clause = f' where {where}' if where else ''
    h = hashlib.sha256()
    n = 0
    for row in conn.execute(f'select * from "{table}"{clause} order by {_order(conn, table)}', params):
        h.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode('utf8') + b'\n')
        n += 1
    return dict(rows=n, sha256=h.hexdigest())


def broken_chain(conn, start: str = '2012-12-03', end: str = LAST_SESSION) -> int:
    row = conn.execute("""select count(*) from (
        select tri, lag(tri) over (partition by security_id order by session_date) as previous, daily_return
        from tri_daily where session_date between ? and ?)
        where daily_return is not null and previous is not null
          and abs(tri - previous * (1 + daily_return)) > 1e-9 * previous""", (start, end)).fetchone()
    return int(row[0])


def migrate(base: Path, out: Path, manifest: Path) -> dict:
    base, out, manifest = Path(base), Path(out), Path(manifest)
    if base.resolve() == out.resolve():
        raise ValueError('candidate must differ from the released database')
    if out.exists():
        raise FileExistsError(out)
    if file_hash(base) != BASE_FILE:
        raise ValueError('released database file hash mismatch')

    cfg = json.loads(CONFIG.read_text(encoding='utf8'))
    securities = [row['security_id'] for row in cfg['securities']]
    if len(securities) != len(set(securities)):
        raise ValueError('duplicate security id in the config')

    base_conn = sqlite3.connect(f'file:{base.as_posix()}?mode=ro', uri=True)
    base_conn.row_factory = sqlite3.Row
    meta = dict(base_conn.execute('select key,value from meta'))
    if meta.get('content_sha256') != BASE_CONTENT:
        raise ValueError('released database content hash mismatch')
    tables = [r[0] for r in base_conn.execute(
        "select name from sqlite_master where type='table' and name not in ('meta','build_log') "
        "and name not like 'sqlite_%' order by name")]
    before = {t: rows_hash(base_conn, t) for t in tables}
    before_dev = rows_hash(base_conn, 'tri_daily', 'session_date <= ?', (ANCHOR,))
    marks = ','.join('?' for _ in securities)
    before_post = rows_hash(base_conn, 'tri_daily',
                            f'session_date > ? and security_id not in ({marks})', (ANCHOR, *securities))
    before_broken = broken_chain(base_conn)
    base_conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base, out)
    # the released file is marked read-only; copy2 keeps that flag, and sqlite refuses to write to it
    os.chmod(out, stat.S_IREAD | stat.S_IWRITE)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    conn.execute('pragma foreign_keys=on')

    updated = {}
    for sid in securities:
        rows = conn.execute("""select session_date, tri, daily_return from tri_daily
                               where security_id=? and session_date>=? order by session_date""",
                            (sid, ANCHOR)).fetchall()
        if not rows or rows[0]['session_date'] != ANCHOR:
            raise ValueError(f'{sid}: no row on the anchor {ANCHOR}')
        previous = float(rows[0]['tri'])
        changes = []
        for row in rows[1:]:
            if row['daily_return'] is None:
                raise ValueError(f'{sid} {row["session_date"]}: missing daily_return')
            previous = previous * (1.0 + float(row['daily_return']))
            changes.append((previous, sid, row['session_date']))
        conn.executemany('update tri_daily set tri=? where security_id=? and session_date=?', changes)
        updated[sid] = len(changes)
    conn.commit()

    after = {t: rows_hash(conn, t) for t in tables}
    after_dev = rows_hash(conn, 'tri_daily', 'session_date <= ?', (ANCHOR,))
    after_post = rows_hash(conn, 'tri_daily',
                           f'session_date > ? and security_id not in ({marks})', (ANCHOR, *securities))
    changed_tables = [t for t in tables if before[t] != after[t]]
    if changed_tables != ['tri_daily']:
        raise RuntimeError(f'unexpected table changes: {changed_tables}')
    if before_dev != after_dev:
        raise RuntimeError('development segment of tri_daily changed')
    if before_post != after_post:
        raise RuntimeError('post-anchor rows outside the eight securities changed')

    for sid in securities:
        row = conn.execute("""select count(*) from (
            select tri, lag(tri) over (order by session_date) as previous, daily_return
            from tri_daily where security_id=? and session_date>=?)
            where daily_return is not null and previous is not null
              and abs(tri - previous*(1+daily_return)) > 1e-12*previous""", (sid, ANCHOR)).fetchone()
        if row[0]:
            raise RuntimeError(f'{sid}: {row[0]} rows still break the return chain')
    after_broken = broken_chain(conn)
    if after_broken:
        raise RuntimeError(f'{after_broken} rows still break the return chain')

    integrity = conn.execute('pragma integrity_check').fetchone()[0]
    foreign = conn.execute('pragma foreign_key_check').fetchall()
    if integrity != 'ok' or foreign:
        raise RuntimeError('candidate integrity or foreign key check failed')

    conn.row_factory = None
    digest = content_hash(conn)
    conn.execute("insert or replace into meta(key,value) values('tri_seam_release',?)", (RELEASE,))
    conn.execute("insert or replace into meta(key,value) values('tri_seam_config_sha256',?)",
                 (file_hash(CONFIG),))
    conn.execute("insert or replace into meta(key,value) values('content_sha256',?)", (digest,))
    conn.commit()
    conn.close()

    result = dict(release=RELEASE, anchor=ANCHOR, last_session=LAST_SESSION,
                  base_file_sha256=BASE_FILE, base_content_sha256=BASE_CONTENT,
                  candidate_file_sha256=file_hash(out), candidate_content_sha256=digest,
                  config_sha256=file_hash(CONFIG), securities=securities, updated_rows=updated,
                  total_updated=sum(updated.values()),
                  tables_compared=len(tables), changed_tables=changed_tables,
                  dev_tri_rows=before_dev['rows'], dev_tri_sha256=before_dev['sha256'],
                  post_tri_other_rows=before_post['rows'], post_tri_other_sha256=before_post['sha256'],
                  broken_chain_before=before_broken, broken_chain_after=after_broken,
                  integrity=integrity, foreign_key_violations=len(foreign),
                  table_hashes={t: after[t] for t in tables})
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf8', newline='\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--manifest', required=True)
    args = parser.parse_args(argv)
    result = migrate(Path(args.base), Path(args.out), Path(args.manifest))
    print(json.dumps({k: v for k, v in result.items() if k != 'table_hashes'}, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
