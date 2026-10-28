"""Read-only query interface of leadtrend_db for research code (backtest, diagnostics).

Every function opens the database read-only (URI mode=ro) and never writes. The database is a file; nothing here
recomputes the pool, the total-return index or the calendar.

    pool_at(day)                     eligible securities and companies effective on a NYSE session
    tri_series(ids, start, end)      total-return index rows per security (lineage is already continuous per key)
    open_prices(ids, day)            raw opening price of one session, for next-open execution
    trading_days(start, end)         NYSE sessions from price_calendar
    friday_signal_days(start, end)   last session of each ISO week
    rf_daily(start, end)             DTB3 3-month bill, daily rate = percent/100/360 per calendar day
"""
from __future__ import annotations
import bisect
import csv
import json
import hashlib
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from .sources import DB_PATH, ROOT

DTB3_PATH = ROOT / 'data' / 'v05' / 'raw' / 'external' / 'DTB3.csv'
_CONN: dict[str, sqlite3.Connection] = {}
_CONN_STAT: dict[str, tuple] = {}


def _file_identity(path: Path) -> tuple:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    db_path = DB_PATH if db_path is None else db_path
    path = Path(db_path).resolve()
    key = str(path)
    identity = _file_identity(path)
    if key in _CONN and _CONN_STAT.get(key) != identity:
        _CONN.pop(key).close()
        _CONN_STAT.pop(key, None)
    if key not in _CONN:
        conn = sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        _CONN[key] = conn
        _CONN_STAT[key] = identity
    return _CONN[key]


def close(db_path: Path | str = DB_PATH) -> None:
    key = str(Path(db_path).resolve())
    conn = _CONN.pop(key, None)
    _CONN_STAT.pop(key, None)
    if conn is not None:
        conn.close()


def database_identity(db_path: Path | str | None = None) -> tuple:
    path = Path(DB_PATH if db_path is None else db_path).resolve()
    conn = connect(path)
    row = conn.execute("select value from meta where key='content_sha256'").fetchone()
    return (str(path), _file_identity(path), row[0] if row else '')


def trading_days(start: str, end: str, db_path: Path | str = DB_PATH) -> list[str]:
    rows = connect(db_path).execute(
        'select session_date from price_calendar where session_date between ? and ? order by 1', (start, end))
    return [r[0] for r in rows]


def friday_signal_days(start: str, end: str, db_path: Path | str = DB_PATH) -> list[str]:
    """The last session of each ISO week (a week cut short by a holiday uses its last session)."""
    out: list[str] = []
    seen = None
    for day in trading_days(start, end, db_path):
        from datetime import date
        key = date.fromisoformat(day).isocalendar()[:2]
        if key != seen:
            out.append(day)
            seen = key
        else:
            out[-1] = day
    return out


def pool_at(day: str, db_path: Path | str = DB_PATH) -> dict:
    """Eligible securities and companies of the pool version effective on `day` (a NYSE session)."""
    conn = connect(db_path)
    windows = conn.execute("""select version_id, min(active_from) as first, max(active_to) as last
                              from price_quarter_coverage group by version_id""").fetchall()
    pick = None
    for row in windows:
        if row['first'] <= day <= row['last']:
            pick = row['version_id']
            break
    if pick is None:
        return dict(version_id='', report_period='', effective_from='', effective_to='', securities=[], companies=[])
    meta = conn.execute('select report_period, available_at_utc from pool_versions where version_id=?', (pick,)).fetchone()
    securities = [dict(r) for r in conn.execute("""
        select ss.security_id, ss.issuer_id, ss.class_code, ss.current_security_id, ss.current_ticker,
               s.security_type, i.name as company
        from strategy_securities ss
        join securities s on s.security_id = ss.security_id
        left join issuers i on i.issuer_id = ss.issuer_id
        where ss.version_id = ? and ss.strategy_eligible = 1
        order by ss.security_id""", (pick,))]
    companies = [dict(r) for r in conn.execute("""
        select issuer_id, historical_rank, current_ticker, resolved_current_issuer
        from strategy_companies where version_id = ? and strategy_eligible = 1
        order by historical_rank""", (pick,))]
    window = next(r for r in windows if r['version_id'] == pick)
    return dict(version_id=pick, report_period=meta['report_period'], available_at_utc=meta['available_at_utc'],
                effective_from=window['first'], effective_to=window['last'], securities=securities,
                companies=companies)


def tri_series(ids, start: str, end: str, db_path: Path | str = DB_PATH) -> dict[str, list[tuple[str, float, float]]]:
    """{security_id: [(session_date, tri, daily_return), ...]} for the requested ids inside [start, end]."""
    if isinstance(ids, str):
        ids = [ids]
    conn = connect(db_path)
    out: dict[str, list[tuple[str, float, float]]] = {}
    for sid in ids:
        rows = conn.execute("""select session_date, tri, daily_return from tri_daily
                               where security_id = ? and session_date between ? and ? order by session_date""",
                            (sid, start, end))
        out[sid] = [(r[0], r[1], r[2]) for r in rows]
    return out


def open_prices(ids, day: str, db_path: Path | str = DB_PATH) -> dict[str, float]:
    """Raw opening price of one session; a security without a bar that day is absent from the result."""
    if isinstance(ids, str):
        ids = [ids]
    conn = connect(db_path)
    out: dict[str, float] = {}
    for sid in ids:
        row = conn.execute("select open from prices_daily where security_id=? and session_date=? and source='opend_none'",
                           (sid, day)).fetchone()
        if row is not None and row[0]:
            out[sid] = float(row[0])
    return out


@lru_cache(maxsize=8)
def _dtb3(end: str) -> tuple[list[str], list[float]]:
    days: list[str] = []
    values: list[float] = []
    with DTB3_PATH.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if row['observation_date'] > end:
                break
            text = (row.get('DTB3') or '').strip()
            if not text:
                continue
            days.append(row['observation_date'])
            values.append(float(text))
    order = sorted(range(len(days)), key=lambda i: days[i])
    return [days[i] for i in order], [values[i] for i in order]


def rf_daily(start: str, end: str, db_path: Path | str = DB_PATH) -> dict[str, float]:
    """Daily risk-free rate per calendar day (DTB3 percent / 100 / 360), forward filled to every NYSE session."""
    days, values = _dtb3(end)
    out: dict[str, float] = {}
    for day in trading_days(start, end, db_path):
        i = bisect.bisect_right(days, day) - 1
        if i < 0:
            continue
        out[day] = values[i] / 100.0 / 360.0
    return out


def open_series(ids, start: str, end: str, db_path: Path | str = DB_PATH) -> dict[str, dict[str, float]]:
    """{security_id: {session_date: open}} for raw opening prices inside [start, end]."""
    if isinstance(ids, str):
        ids = [ids]
    conn = connect(db_path)
    out: dict[str, dict[str, float]] = {}
    for sid in ids:
        rows = conn.execute("""select session_date, open from prices_daily
                               where security_id=? and source='opend_none' and session_date between ? and ?
                               order by session_date""", (sid, start, end))
        out[sid] = {r[0]: float(r[1]) for r in rows if r[1]}
    return out


def pool_windows(start: str, end: str, db_path: Path | str = DB_PATH) -> list[dict]:
    """Pool versions whose effective window overlaps [start, end], in effective order."""
    conn = connect(db_path)
    rows = conn.execute("""select version_id, min(active_from) as effective_from, max(active_to) as effective_to,
                                  count(*) as securities
                           from price_quarter_coverage group by version_id
                           having max(active_to) >= ? and min(active_from) <= ?
                           order by effective_from""", (start, end)).fetchall()
    periods = {r['version_id']: r for r in conn.execute('select version_id, report_period from pool_versions')}
    out = []
    for row in rows:
        item = dict(row)
        item['report_period'] = periods[row['version_id']]['report_period']
        out.append(item)
    return out


def pool_amounts(version_id: str, db_path: Path | str = DB_PATH) -> dict[str, dict]:
    """{security_id: {issuer_id, amount_cents}} of the eligible securities of one version."""
    conn = connect(db_path)
    rows = conn.execute("""select ts.security_id, ts.issuer_id, ts.amount_cents, ts.class_code
                           from top200_securities ts
                           join strategy_securities ss on ss.version_id = ts.version_id
                                                        and ss.security_id = ts.security_id
                           where ts.version_id = ? and ts.candidate_eligible = 1 and ss.strategy_eligible = 1
                           order by ts.amount_cents desc, ts.security_id""", (version_id,))
    return {r['security_id']: dict(r) for r in rows}


def pool_company_amounts(version_id: str, db_path: Path | str = DB_PATH) -> dict[str, int]:
    """Historical ranking amounts, including qualifying classes that cannot be purchased."""
    conn = connect(db_path)
    return {r['issuer_id']: r['amount_cents'] for r in conn.execute(
        'select issuer_id, amount_cents from top200_companies where version_id = ?', (version_id,))}


def issuer_continuity(db_path: Path | str = DB_PATH) -> list[dict]:
    """Dated, verified single-company continuations, for company-level statistics only."""
    conn = connect(db_path)
    return [dict(r) for r in conn.execute("""select predecessor_issuer_id, successor_issuer_id, effective_date
        from issuer_links where relation in ('successor_reorganization','rename')
        and confidence='high' and effective_date <> '' and effective_date <= '2024-06-28'
        order by effective_date, predecessor_issuer_id""")]


def _bounded_benchmark_archive(start: str, end: str) -> dict[str, dict[str, float]]:
    """Read only JSON records up to the requested date, stopping before decoding later price rows."""
    from .sources import RawStore
    config = ROOT / 'config' / 'db_benchmark_v1.json'
    if not config.exists():
        return {}
    cfg = json.loads(config.read_text(encoding='utf8'))
    store = RawStore(offline=True)
    index = json.loads(store._read(cfg['index_url']))
    bars: dict[str, dict[str, float]] = {}
    for url in index['kline']:
        rec = store.records[url]
        path = store.raw_dir / rec['path']
        digest = hashlib.sha256()
        with path.open('rb') as raw:
            for block in iter(lambda: raw.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != rec['sha256']:
            raise RuntimeError(f'benchmark raw hash mismatch: {rec["path"]}')
        decoder = json.JSONDecoder()
        with path.open(encoding='utf8') as f:
            buffer = ''
            while '"data"' not in buffer or '[' not in buffer[buffer.index('"data"'):]:
                part = f.read(4096)
                if not part:
                    raise ValueError('benchmark archive has no data array')
                buffer += part
            buffer = buffer[buffer.index('[', buffer.index('"data"'))+1:]
            previous = ''
            while True:
                buffer = buffer.lstrip(' \r\n\t,')
                if not buffer:
                    buffer = f.read(4096)
                    if not buffer:
                        break
                    continue
                if buffer[0] == ']':
                    break
                match = re.search(r'"time_key"\s*:\s*"(\d{4}-\d{2}-\d{2})', buffer)
                while match is None:
                    part = f.read(4096)
                    if not part:
                        raise ValueError('benchmark record missing time key')
                    buffer += part
                    match = re.search(r'"time_key"\s*:\s*"(\d{4}-\d{2}-\d{2})', buffer)
                day = match.group(1)
                if day > end:
                    break
                if previous and day < previous:
                    raise ValueError('benchmark archive is not date ordered')
                previous = day
                while True:
                    try:
                        row, used = decoder.raw_decode(buffer)
                        break
                    except json.JSONDecodeError:
                        part = f.read(4096)
                        if not part:
                            raise
                        buffer += part
                buffer = buffer[used:]
                if day >= start:
                    bars[day] = dict(open=float(row['open']), close=float(row['close']))
    return bars


def benchmark_bars(start: str, end: str, db_path: Path | str | None = None) -> dict[str, dict[str, float]]:
    """VOO raw bars from the selected development database, with bounded legacy archive fallback."""
    if end > '2024-06-28':
        raise ValueError('benchmark query exceeds development cutoff')
    conn = connect(db_path)
    if conn.execute("select 1 from sqlite_master where type='table' and name='benchmark_bars_development'").fetchone():
        return {r['session_date']: dict(open=r['open'], close=r['close']) for r in conn.execute(
            'select session_date,open,close from benchmark_bars_development where session_date between ? and ? order by session_date', (start, end))}
    return _bounded_benchmark_archive(start, end)


def close_series(ids, start: str, end: str, db_path: Path | str = DB_PATH) -> dict[str, dict[str, float]]:
    """{security_id: {session_date: raw close}} inside [start, end]."""
    if isinstance(ids, str):
        ids = [ids]
    conn = connect(db_path)
    out: dict[str, dict[str, float]] = {}
    for sid in ids:
        rows = conn.execute("""select session_date, close from prices_daily
                               where security_id=? and source='opend_none' and session_date between ? and ?
                               order by session_date""", (sid, start, end))
        out[sid] = {r[0]: float(r[1]) for r in rows if r[1]}
    return out


def volume_series(ids, start: str, end: str, db_path: Path | str = DB_PATH,
                  split_adjusted: bool = False) -> dict[str, dict[str, float]]:
    """{security_id: {session_date: share volume}} inside [start, end].

    `split_adjusted=True` restates every session to the latest share count (see `apply_split_factors`), so a
    volume ratio is not distorted by a split inside the window. The default keeps the raw OpenD shares.
    """
    if isinstance(ids, str):
        ids = [ids]
    conn = connect(db_path)
    events = split_events(ids, db_path, end=end) if split_adjusted else {}
    out: dict[str, dict[str, float]] = {}
    for sid in ids:
        rows = conn.execute("""select session_date, volume from prices_daily
                               where security_id=? and source='opend_none' and session_date between ? and ?
                               order by session_date""", (sid, start, end))
        series = {r[0]: float(r[1]) for r in rows if r[1] is not None}
        out[sid] = apply_split_factors(series, events.get(sid, [])) if split_adjusted else series
    return out


def split_events(ids, db_path: Path | str = DB_PATH, end: str | None = None) -> dict[str, list[tuple[str, float]]]:
    """{security_id: [(ex_date, ratio), ...]} for splits and reverse splits, oldest first.

    `ratio` is new shares per old share, the same convention as `leadtrend_db/tri.py`: 20.0 for a 20:1 split,
    0.5 for a 1:2 reverse split. `end` drops events after that date (pass the series end, so no reserved-segment
    row is read).
    """
    if isinstance(ids, str):
        ids = [ids]
    conn = connect(db_path)
    out: dict[str, list[tuple[str, float]]] = {}
    for sid in ids:
        rows = conn.execute("""select action_date, ratio from corporate_actions
                               where security_id=? and action_type in ('split','reverse_split')
                               and action_date <= ? order by action_date""", (sid, end or '9999-12-31'))
        out[sid] = [(r[0], float(r[1])) for r in rows if r[1]]
    return out


def apply_split_factors(series: dict[str, float], events: list[tuple[str, float]]) -> dict[str, float]:
    """Restate a share-volume series to the latest share count.

    A session before an ex-date is multiplied by the ratio of every split on or after it, so 5 million shares
    traded before a 20:1 split becomes 100 million in post-split units and the series stays continuous.
    """
    if not events:
        return dict(series)
    out: dict[str, float] = {}
    for day, value in series.items():
        factor = 1.0
        for ex_date, ratio in events:
            if day < ex_date:
                factor *= ratio
        out[day] = value * factor
    return out


def version_amounts(db_path: Path | str = DB_PATH) -> list[dict]:
    """Every pool version with its report period and company amounts, oldest first."""
    conn = connect(db_path)
    versions = [dict(r) for r in conn.execute(
        'select version_id, report_period from pool_versions order by report_period, version_id')]
    for item in versions:
        item['amounts'] = pool_company_amounts(item['version_id'], db_path)
    return versions
