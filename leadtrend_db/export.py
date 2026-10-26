"""CSV exports and the counts-only summary of leadtrend_db v1 (brief section 6, step 6).

Exports go to data/db/exports/ (git-ignored). research/db_v1/summary.md is the counts summary that may be committed:
it contains counts, dates and ranges only, no per-security rows.
"""
from __future__ import annotations

import csv
import sqlite3
from collections import Counter
from pathlib import Path

from . import sources as S

EXPORTS = [
    ('snapshots', 'SELECT * FROM snapshots ORDER BY holdings_as_of, form, snapshot_id'),
    ('issuers', 'SELECT * FROM issuers ORDER BY issuer_id'),
    ('securities', 'SELECT * FROM securities ORDER BY security_id'),
    ('membership_spans', 'SELECT * FROM membership_spans ORDER BY level, subject_id, span_no'),
    ('listing_status', 'SELECT * FROM listing_status ORDER BY issuer_id'),
    ('universe_v1', 'SELECT * FROM universe_v1 ORDER BY issuer_id, security_id, span_no'),
    ('review_list', 'SELECT * FROM review_items ORDER BY review_id'),
]


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f'file:{S.DB_PATH}?mode=ro', uri=True)


def export_all(log=print) -> None:
    S.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    conn = _connect()
    try:
        for name, sql in EXPORTS:
            cur = conn.execute(sql)
            cols = [c[0] for c in cur.description]
            path = S.EXPORT_DIR / f'{name}.csv'
            with path.open('w', encoding='utf-8', newline='') as f:
                w = csv.writer(f, lineterminator='\n')
                w.writerow(cols)
                w.writerows(cur.fetchall())
            log(f'exported {path.name}')
    finally:
        conn.close()


def _previous_quarter(d: str) -> str:
    """The quarter end before d (ISO date at a calendar quarter end)."""
    y, md = int(d[:4]), d[5:]
    prev = {'03-31': (y - 1, '12-31'), '06-30': (y, '03-31'), '09-30': (y, '06-30'), '12-31': (y, '09-30')}
    py, pmd = prev[md]
    return f'{py:04d}-{pmd}'


def _q(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[tuple]:
    return conn.execute(sql, args).fetchall()


def stats_markdown(conn: sqlite3.Connection) -> str:
    lines = ['# leadtrend_db v1: build summary (counts only)', '',
             '数据库：`data/db/leadtrend_db_v1.sqlite`（不进库）。以下只有计数、日期范围与比例，不含逐只证券数据。', '']

    lines += ['## 快照', '', '| 来源 | 完整性 | 主用 | 快照数 | 持仓日期 | 公开日期 | 行数（最小–最大） | 覆盖率（最小–最大） |',
              '|---|---|---|---|---|---|---|---|']
    for form, comp, prim, n, d0, d1, a0, a1, r0, r1, c0, c1 in _q(conn, """
        SELECT form, completeness, is_primary, COUNT(*), MIN(holdings_as_of), MAX(holdings_as_of),
               MIN(substr(available_at_utc,1,10)), MAX(substr(available_at_utc,1,10)),
               MIN(row_count), MAX(row_count), MIN(coverage_ratio), MAX(coverage_ratio)
        FROM snapshots GROUP BY form, completeness, is_primary ORDER BY form, completeness, is_primary"""):
        lines.append(f'| {form} | {comp} | {"是" if prim else "否"} | {n} | {d0}–{d1} | {a0}–{a1} | {r0}–{r1} | '
                     f'{c0:.4f}–{c1:.4f} |' if c0 is not None else
                     f'| {form} | {comp} | {"是" if prim else "否"} | {n} | {d0}–{d1} | {a0}–{a1} | {r0}–{r1} | – |')
    lines.append('')

    spans = _q(conn, "SELECT subject_id, issuer_id, first_as_of, next_absent_as_of, span_no FROM membership_spans "
                      "WHERE level = 'issuer'")
    observed = {r[0] for r in _q(conn, 'SELECT holdings_as_of FROM snapshots WHERE is_primary = 1')}
    first_q = min(observed)
    entered, left = Counter(), Counter()
    leavers: dict[str, set] = {}
    for subject, issuer, first, absent, n in spans:
        # an entry needs the previous quarter to be observed; a span that starts after a missing quarter is unknown
        if first != first_q and _previous_quarter(first) in observed:
            entered[first[:4]] += 1
        if absent:
            left[absent[:4]] += 1
            leavers.setdefault(issuer, set()).add(absent[:4])
    status = dict(_q(conn, 'SELECT issuer_id, status FROM listing_status'))
    lines += ['## 每年进入与离开 VOO 的发行人（发行人段数；进入需前一季度有完整快照，首个季度的成员不计）', '',
              '| 年 | 进入 | 离开 |', '|---|---|---|']
    years = sorted(set(entered) | set(left))
    for y in years:
        lines.append(f'| {y} | {entered.get(y, 0)} | {left.get(y, 0)} |')
    lines.append('')

    by_status = Counter(status.get(i, 'none') for i in leavers)
    reasons = Counter(r for (r,) in _q(conn, "SELECT reason FROM listing_status WHERE status = 'delisted' AND "
                                              "issuer_id IN (SELECT DISTINCT issuer_id FROM membership_spans WHERE "
                                              "level='issuer' AND next_absent_as_of <> '')"))
    lines += ['## 离开者（至少一次离开 VOO 的发行人）', '',
              f'- 离开过 VOO 的发行人：{len(leavers)}',
              f'- delisted：{by_status.get("delisted", 0)}；listed_out_of_index：{by_status.get("listed_out_of_index", 0)}；'
              f'continued_by_successor：{by_status.get("continued_by_successor", 0)}；'
              f'unknown：{by_status.get("unknown", 0)}；in_index：{by_status.get("in_index", 0)}',
              '- delisted 按原因：' + '；'.join(f'{k} {v}' for k, v in sorted(reasons.items())), '']

    total_issuers = _q(conn, "SELECT COUNT(DISTINCT issuer_id) FROM membership_spans WHERE level='issuer'")[0][0]
    total_secs = _q(conn, "SELECT COUNT(DISTINCT subject_id) FROM membership_spans WHERE level='security'")[0][0]
    delisted_n = _q(conn, "SELECT COUNT(*) FROM listing_status WHERE status='delisted' AND issuer_id IN "
                          "(SELECT issuer_id FROM membership_spans WHERE level='issuer')")[0][0]
    cont_n = _q(conn, "SELECT COUNT(*) FROM listing_status WHERE status='continued_by_successor' AND issuer_id IN "
                      "(SELECT issuer_id FROM membership_spans WHERE level='issuer')")[0][0]
    u_iss, u_sec = _q(conn, 'SELECT COUNT(DISTINCT issuer_id), COUNT(DISTINCT security_id) FROM universe_v1')[0]
    u_elig_sec = _q(conn, 'SELECT COUNT(DISTINCT security_id) FROM universe_v1 WHERE eligible_v1 = 1')[0][0]
    u_elig_rows = _q(conn, 'SELECT COUNT(*) FROM universe_v1 WHERE eligible_v1 = 1')[0][0]
    lines += ['## 总计', '',
              f'- 曾在 VOO 中的发行人：{total_issuers}；证券：{total_secs}',
              f'- delisted 发行人：{delisted_n}；continued_by_successor 发行人：{cont_n}',
              f'- universe_v1 发行人：{u_iss}；证券：{u_sec}；其中 eligible_v1=1 的证券：{u_elig_sec}'
              f'（段行数 {u_elig_rows}）', '']

    rv = _q(conn, 'SELECT review_type, COUNT(*) FROM review_items GROUP BY review_type ORDER BY review_type')
    lines += ['## 复核清单（条数，按类型）', '', '| 类型 | 条数 |', '|---|---|']
    lines += [f'| {t} | {n} |' for t, n in rv]
    lines.append('')
    return '\n'.join(lines)


def write_stats(summary_path: str | None = None, log=print) -> None:
    conn = _connect()
    try:
        text = stats_markdown(conn)
    finally:
        conn.close()
    S.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    (S.EXPORT_DIR / 'stats.md').write_text(text, encoding='utf-8')
    log(f'stats written to {S.EXPORT_DIR / "stats.md"}')
    if summary_path:
        target = Path(summary_path)
        if not target.is_absolute():
            target = S.ROOT / target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        log(f'summary written to {target}')
