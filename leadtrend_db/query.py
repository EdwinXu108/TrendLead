"""Queries of the Top 200 pool by quarter or by UTC time.

Public-time rule. A version is known from its available_at_utc (the SEC acceptance time). Query at a UTC time T:
  1. candidates = versions with available_at_utc <= T;
  2. the report period is the latest report_period among them (a late filing of an older period never replaces a
     newer period that was already public);
  3. within that period the version is the latest one available by T (a later filing or revision of the same period is
     not visible before its own available_at_utc).
If no version is public by T the answer is 'no list yet'. A missing newer filing never empties the list: the last
public version is still returned.

Historical and strategy members exist for every public version. A blocked version is returned for inspection with
status='blocked' and a failing CLI exit code; it is never silently passed to a strategy as a usable pool.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import rules as R


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)


def version_as_of(conn: sqlite3.Connection, utc: str) -> dict | None:
    """The version that was public at UTC time utc ('YYYY-MM-DDTHH:MM:SSZ'), or None before the first list."""
    d = datetime.fromisoformat(utc.replace('Z', '+00:00'))
    if d.tzinfo is None:
        raise ValueError('query time must include a UTC offset')
    utc = d.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    row = conn.execute("""select version_id, report_period, available_at_utc, is_canonical, quarter_state, form
                          from pool_versions where available_at_utc <= ?
                          order by report_period desc, available_at_utc desc, version_id desc limit 1""", (utc,)).fetchone()
    if row is None:
        return None
    return dict(zip(('version_id', 'report_period', 'available_at_utc', 'is_canonical', 'quarter_state', 'form'), row))


def members_as_of(conn: sqlite3.Connection, utc: str) -> dict:
    v = version_as_of(conn, utc)
    if v is None:
        return dict(query_utc=utc, version=None, status='no list public yet', companies=[], strategy=[], eligible_securities=[])
    companies = conn.execute("""select rank, issuer_id, issuer_name_at_period, amount_cents, fund_weight_raw, tie_group,
                                       blocked_members from top200_companies where version_id=? order by rank""",
                             (v['version_id'],)).fetchall()
    strategy = conn.execute("""select historical_rank, issuer_id, current_status, resolved_current_issuer,
                                          current_ticker, strategy_eligible, blocked_reason, excluded_reason
                                   from strategy_companies where version_id=? order by historical_rank""",
                                (v['version_id'],)).fetchall()
    return dict(query_utc=utc, version=v, status='ok' if v['quarter_state'] == 'ready' else 'blocked',
                companies=companies, strategy=strategy,
                eligible_securities=eligible_securities(conn, v['version_id']) if v['quarter_state']=='ready' else [])


def eligible_securities(conn, version_id):
    """Historical security keys are the strategy input; current identifiers are a separate display/projection."""
    return conn.execute("""select issuer_id,security_id,class_code,current_security_id,current_ticker
                           from strategy_securities where version_id=? and strategy_eligible=1
                           order by issuer_id,security_id""", (version_id,)).fetchall()


def quarter(conn: sqlite3.Connection, report_period: str) -> dict:
    """The canonical list of one report period and its quality state (ready or blocked)."""
    v = conn.execute("""select version_id, report_period, available_at_utc, quarter_state, quarter_reason
                        from pool_versions where report_period=? and is_canonical=1""", (report_period,)).fetchone()
    if v is None:
        return dict(report_period=report_period, status='no canonical version')
    companies = conn.execute("""select rank, issuer_id, issuer_name_at_period, amount_cents, tie_group, blocked_members
                                from top200_companies where version_id=? order by rank""", (v[0],)).fetchall()
    return dict(report_period=report_period, version_id=v[0], available_at_utc=v[2], quarter_state=v[3],
                quarter_reason=v[4], companies=companies,
                eligible_securities=eligible_securities(conn,v[0]) if v[3]=='ready' else [])


def format_rows(header: list[str], rows: list[tuple]) -> str:
    lines = [' | '.join(header)]
    lines += [' | '.join('' if v is None else str(v) for v in r) for r in rows]
    return '\n'.join(lines)


def main_query(db_path: Path, quarter_period: str | None, as_of: str | None) -> int:
    conn = _connect(db_path)
    try:
        if quarter_period:
            res = quarter(conn, quarter_period)
            if 'companies' not in res:
                print(res['status'])
                return 1
            print(f"report period {res['report_period']} version {res['version_id']} "
                  f"available {res['available_at_utc']} state {res['quarter_state']}")
            print(format_rows(['rank', 'issuer_id', 'holding_name', 'amount_cents', 'tie_group', 'blocked_members'],
                              res['companies']))
            print(format_rows(['issuer_id','historical_security_id','class','current_security_id','current_ticker'],
                              res['eligible_securities']))
            return 0 if res['companies'] and res['quarter_state'] == 'ready' else 1
        if as_of:
            res = members_as_of(conn, as_of)
            if res['version'] is None:
                print(res['status'])
                return 1
            v = res['version']
            print(f"query {as_of}: report period {v['report_period']} version {v['version_id']} "
                  f"available {v['available_at_utc']} canonical {v['is_canonical']} state {v['quarter_state']}")
            print(format_rows(['rank', 'issuer_id', 'holding_name', 'amount_cents', 'tie_group', 'blocked_members'],
                              res['companies']))
            if res['strategy']:
                print(f'strategy (status cutoff {R.STATUS_CUTOFF})')
                print(format_rows(['historical_rank', 'issuer_id', 'current_status', 'resolved_current_issuer',
                                   'current_ticker', 'strategy_eligible', 'blocked_reason', 'excluded_reason'],
                                  res['strategy']))
            print(format_rows(['issuer_id','historical_security_id','class','current_security_id','current_ticker'],
                              res['eligible_securities']))
            return 0 if res['status'] == 'ok' else 1
    finally:
        conn.close()
    return 2
