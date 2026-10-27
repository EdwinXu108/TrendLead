"""Registered pool-membership release: two securities leave the strategy list.

The investor removed ACE Ltd (CUSIP:H1467J104) and Comcast Special A
(CIKCLS:0001166691:CL_A_SPECIAL) from the pool on 2026-10-27 because OpenD has no price
history for either code.  Prices, TRI, corporate actions and every coverage fact stay
exactly as they are: the database keeps the history, the strategy list stops using it.

Only three tables change:
  strategy_securities  the two securities become ineligible, with the investor's reason
  strategy_companies   an issuer left without any eligible class becomes ineligible
  corr_groups          the descriptive grouping rows of the two securities are dropped

Every other table is hashed before and after and must be identical.  The content hash is
recomputed and written to meta, together with the release marker and the config hash.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

from .build import content_hash

BASE_FILE = 'c1a508a5b3fb219d526d0c935e5e5b60ac385c9134c887c189909deb08bf0147'
BASE_CONTENT = '092f9076a27554d8d7c228f4d70595159cb3a45ad59cd5a646eb38afd6cd412e'
RELEASE = 'pool_exclusions_v2'
CONFIG = Path('config/pool_exclusions_v2.csv')
TOUCHED = ('strategy_securities', 'strategy_companies', 'corr_groups')


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8388608), b''):
            h.update(block)
    return h.hexdigest()


def rows_hash(conn: sqlite3.Connection, table: str) -> dict:
    cols = len(conn.execute(f'pragma table_info("{table}")').fetchall())
    order = ','.join(str(i) for i in range(1, cols + 1))
    h = hashlib.sha256()
    n = 0
    for row in conn.execute(f'select * from "{table}" order by {order}'):
        h.update(json.dumps(tuple(row), ensure_ascii=False, separators=(',', ':')).encode('utf8') + b'\n')
        n += 1
    return dict(rows=n, sha256=h.hexdigest())


def migrate(base: Path, out: Path, manifest: Path) -> dict:
    base, out, manifest = Path(base), Path(out), Path(manifest)
    if base.resolve() == out.resolve():
        raise ValueError('candidate must differ from the released database')
    if file_hash(base) != BASE_FILE:
        raise ValueError('released database file hash mismatch')
    if out.exists():
        raise FileExistsError(out)
    entries = [row for row in csv.DictReader(CONFIG.read_text(encoding='utf-8-sig').splitlines())]
    if not entries:
        raise ValueError('empty exclusion config')

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(base, out)
    conn = sqlite3.connect(out)
    conn.row_factory = sqlite3.Row
    conn.execute('pragma foreign_keys=on')
    meta = dict(conn.execute('select key,value from meta'))
    if meta.get('content_sha256') != BASE_CONTENT:
        raise ValueError('released database content hash mismatch')
    if meta.get('economic_data_version') != 'development_release_v1':
        raise ValueError('unexpected economic data version')

    tables = [r[0] for r in conn.execute(
        "select name from sqlite_master where type='table' and name not in ('meta','build_log') "
        "and name not like 'sqlite_%' order by name")]
    untouched = [t for t in tables if t not in TOUCHED]
    before = {t: rows_hash(conn, t) for t in untouched}

    changes = []
    issuers: dict[str, str] = {}
    for entry in entries:
        sid, reason = entry['security_id'], entry['reason']
        rows = conn.execute('select version_id, issuer_id, reason from strategy_securities '
                            'where security_id=? and strategy_eligible=1', (sid,)).fetchall()
        if not rows:
            raise ValueError(f'{sid}: no eligible strategy row to remove')
        conn.execute('update strategy_securities set strategy_eligible=0, reason=? '
                     'where security_id=? and strategy_eligible=1', (reason, sid))
        changes.append(dict(kind='security', security_id=sid, versions=len(rows),
                            previous_reasons=sorted({r['reason'] for r in rows})))
        for r in rows:
            issuers[r['issuer_id']] = reason

    company_rows = []
    for issuer in sorted(issuers):
        pending = conn.execute("""select version_id from strategy_companies sc
                                  where sc.issuer_id=? and sc.strategy_eligible=1
                                    and not exists (select 1 from strategy_securities ss
                                                    where ss.version_id=sc.version_id and ss.issuer_id=sc.issuer_id
                                                      and ss.strategy_eligible=1)""", (issuer,)).fetchall()
        if not pending:
            continue
        conn.execute("""update strategy_companies set strategy_eligible=0, excluded_reason=?
                        where issuer_id=? and strategy_eligible=1
                          and not exists (select 1 from strategy_securities ss
                                          where ss.version_id=strategy_companies.version_id
                                            and ss.issuer_id=strategy_companies.issuer_id
                                            and ss.strategy_eligible=1)""", (issuers[issuer], issuer))
        company_rows.append(dict(issuer_id=issuer, versions=len(pending)))

    groups = conn.execute('delete from corr_groups where security_id in (%s)'
                          % ','.join('?' * len(entries)), [e['security_id'] for e in entries]).rowcount
    conn.commit()

    integrity = conn.execute('pragma integrity_check').fetchone()[0]
    foreign = conn.execute('pragma foreign_key_check').fetchall()
    if integrity != 'ok' or foreign:
        raise RuntimeError('candidate integrity or foreign key check failed')

    after = {t: rows_hash(conn, t) for t in untouched}
    if before != after:
        diff = [t for t in untouched if before[t] != after[t]]
        raise RuntimeError(f'untouched tables changed: {diff}')

    # The pool must still be complete: every eligible company keeps an eligible security.
    orphan = conn.execute("""select count(*) from strategy_companies sc where sc.strategy_eligible=1
                             and not exists (select 1 from strategy_securities ss
                                             where ss.version_id=sc.version_id and ss.issuer_id=sc.issuer_id
                                               and ss.strategy_eligible=1)""").fetchone()[0]
    if orphan:
        raise RuntimeError(f'{orphan} eligible companies without an eligible security')

    conn.row_factory = None
    digest = content_hash(conn)
    conn.execute("insert or replace into meta(key,value) values('pool_exclusion_release',?)", (RELEASE,))
    conn.execute("insert or replace into meta(key,value) values('pool_exclusion_config_sha256',?)", (file_hash(CONFIG),))
    conn.execute("insert or replace into meta(key,value) values('content_sha256',?)", (digest,))
    conn.commit()
    conn.close()

    result = dict(release=RELEASE, base_file_sha256=BASE_FILE, base_content_sha256=BASE_CONTENT,
                  candidate_file_sha256=file_hash(out), candidate_content_sha256=digest,
                  config_sha256=file_hash(CONFIG), changes=changes, companies=company_rows,
                  corr_groups_deleted=groups, untouched_tables=before, foreign_key_violations=len(foreign),
                  integrity=integrity)
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
