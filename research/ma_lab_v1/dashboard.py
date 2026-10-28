"""Local dashboard for the single-stock moving-average lab.

Serves one HTML page and a small JSON API on top of `leadtrend_bt.ma_lab`. No third-party packages: only the
standard library and numpy, like the rest of the project.

    python -X utf8 research/ma_lab_v1/dashboard.py --open

Then open http://127.0.0.1:8765 (the browser opens by itself with --open).
"""
from __future__ import annotations

import argparse
import errno
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from leadtrend_bt import ma_lab, ma_walk, ma_walk2  # noqa: E402
from leadtrend_db import api             # noqa: E402
import winrate                           # noqa: E402
import fast_rows                         # noqa: E402

DEFAULT_DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite')
FULL_DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_full_v1.sqlite')
WF2 = ROOT / 'research' / 'wf_v2'
TYPES = ('sma', 'ema', 'wma', 'dema', 'tema', 'hma', 'kama')
FASTS = (3, 5, 8, 10, 13, 20, 30, 50)
SLOWS = (20, 30, 50, 80, 100, 150, 200, 250, 300)
DEFAULT_START, DEFAULT_END = '2014-02-28', '2024-06-28'


def _round(values, digits=6):
    out = []
    for value in values:
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            out.append(None)
        else:
            out.append(round(float(value), digits))
    return out


def annual_returns(days: list[str], equity: list[float]) -> list[dict]:
    """Calendar-year returns; a first year that starts after January and a last year that ends before December
    are marked partial."""
    out = []
    start = 0
    total = len(days)
    for i in range(1, total + 1):
        if i == total or days[i][:4] != days[start][:4]:
            partial = (start == 0 and days[0][5:7] != '01') or (i == total and days[-1][5:7] != '12')
            # the year runs from the previous year's last close, so the first session's move is included once
            base = equity[start - 1] if start > 0 else equity[0]
            out.append(dict(year=days[start][:4], value=equity[i - 1] / base - 1, partial=bool(partial)))
            start = i
    return out


def _metric(value):
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return round(float(value), 6) if np.isfinite(value) else None
    return value


def clean(value):
    """JSON-safe copy: numpy scalars become Python ones, non-finite numbers become null."""
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    return value


class Lab:
    """One security and window, with every combination's metrics and a cache of average lines."""

    def __init__(self, security: str, start: str, end: str, cash: str, db_path: str, frequency: str = 'daily'):
        if end > ma_lab.LAST_ALLOWED_END:
            raise ValueError(f'end {end} is after {ma_lab.LAST_ALLOWED_END}: the reserved segment must not be used')
        if start < ma_lab.FIRST_DAY:
            raise ValueError(f'start {start} is before {ma_lab.FIRST_DAY}')
        if start >= end:
            raise ValueError('start must be before end')
        if cash not in ('zero', 'dtb3'):
            raise ValueError("cash_rate must be 'zero' or 'dtb3'")
        self.db = db_path
        self.cash = cash
        self.frequency = frequency
        self.security = security
        self.sid = ma_lab.resolve_security(security, db_path)
        self.start, self.end = start, end
        self.days, self.values = ma_lab._total_return(self.sid, end, db_path)
        available = set(self.days)
        if frequency == 'daily':
            self.signal_days = [d for d in self.days if start <= d <= end]
        else:
            self.signal_days = [d for d in api.friday_signal_days(start, end, db_path)
                                if d >= start and d in available]
        if not self.signal_days:
            raise ValueError('no signal day in the window')
        self.index = {d: i for i, d in enumerate(self.days)}
        self.signal_index = [self.index[d] for d in self.signal_days]
        self.rf = api.rf_daily(self.signal_days[0], end, db_path)
        self.lines: dict[tuple[str, int], np.ndarray] = {}
        self._rows: list[dict] | None = None
        # the shared arrays and the 462 rows come from the fast path; run_lab stays the reference
        self.fast = fast_rows.Fast(self)
        self.window_days = self.fast.window_days
        window_start = self.fast.start
        self.window_values = self.values[window_start:]
        closes = api.close_series([self.sid], self.window_days[0], end, db_path)[self.sid]
        self.closes = [closes.get(d) for d in self.window_days]
        self.hold = self.fast.hold_row()
        self.meta = dict(ticker=self._ticker(), security_id=self.sid, name=self._name(),
                         start=start, end=end, cash=cash, frequency=frequency,
                         evaluation_start=self.window_days[0],
                         evaluation_end=self.window_days[-1],
                         signal_days=len(self.signal_days))

    def _ticker(self) -> str:
        row = api.connect(self.db).execute(
            'select current_ticker from strategy_securities where security_id=? limit 1', (self.sid,)).fetchone()
        return row[0] if row else self.sid

    def _name(self) -> str:
        row = api.connect(self.db).execute("""select coalesce(i.name, '') from strategy_securities ss
                                              left join issuers i on i.issuer_id = ss.issuer_id
                                              where ss.security_id=? limit 1""", (self.sid,)).fetchone()
        return row[0] if row else ''

    def average(self, kind: str, n: int) -> np.ndarray:
        key = (kind, int(n))
        if key not in self.lines:
            self.lines[key] = ma_lab.MOVING_AVERAGES[kind](self.values, int(n))
        return self.lines[key]

    def rows_with_winrate(self) -> list[dict]:
        """Every combination's metrics plus its trade statistics, computed once per stock and window."""
        if self._rows is None:
            self._rows = self.fast.combo_rows(self, TYPES, FASTS, SLOWS)
        return self._rows

    def combo(self, kind: str, fast: int, slow: int) -> dict:
        # the state is decided at every signal close (daily or weekly) and applied from that close
        if kind not in ma_lab.MOVING_AVERAGES:
            raise ValueError(f'unknown average type: {kind}')
        if not 0 < int(fast) < int(slow):
            raise ValueError('fast must be positive and shorter than slow')
        line_fast, line_slow = self.average(kind, fast), self.average(kind, slow)
        valid = np.isfinite(line_fast) & np.isfinite(line_slow)
        state = [bool(valid[i] and line_fast[i] > line_slow[i]) for i in self.signal_index]
        days, equity = ma_lab.simulate(self.days, self.values, self.signal_days, state, self.cash, self.rf)
        hold_days, hold_equity = ma_lab.simulate(self.days, self.values, self.signal_days,
                                                 [True] * len(self.signal_days), self.cash, self.rf)
        metrics = ma_lab.statistics(days, equity, self.rf)
        buys = sum(1 for k, on in enumerate(state) if on and (k == 0 or not state[k - 1]))
        metrics.update(invested_share=sum(state) / len(state), buys=buys,
                       average_holding_weeks=(sum(state) / buys if buys else 0.0))
        raw = winrate.segment_returns(self.days, self.values, self.signal_days, state)
        metrics.update(winrate.stats(raw))
        segments, trades = self._segments(state)
        offset = self.index[days[0]]
        return dict(key=f'{kind}_{fast}_{slow}', type=kind, fast=int(fast), slow=int(slow),
                    dates=days, equity=_round(equity), hold=_round(hold_equity),
                    tri=_round(self.window_values / self.window_values[0]),
                    fast_line=_round(line_fast[offset:] / self.window_values[0]),
                    slow_line=_round(line_slow[offset:] / self.window_values[0]),
                    trades=trades, segments=segments,
                    annual=annual_returns(days, equity), annual_hold=annual_returns(hold_days, hold_equity),
                    metrics={k: _metric(v) for k, v in metrics.items()})

    def _segments(self, state: list[bool]) -> tuple[list[dict], list[dict]]:
        """Holding periods and the buy/sell markers that bound them."""
        segments, trades = [], []
        entry = None
        for k, on in enumerate(state):
            if on and entry is None:
                entry = k
                trades.append(dict(date=self.signal_days[k], side='buy', week=k))
            elif not on and entry is not None:
                trades.append(dict(date=self.signal_days[k], side='sell', week=k))
                segments.append(self._segment(entry, k))
                entry = None
        if entry is not None:
            segments.append(self._segment(entry, len(state) - 1, open_end=True))
        for item in trades:
            item['tri'] = round(float(self.values[self.index[item['date']]] / self.window_values[0]), 6)
        return segments, trades

    def _segment(self, entry: int, exit_: int, open_end: bool = False) -> dict:
        start_i = self.index[self.signal_days[entry]]
        end_i = self.index[self.signal_days[exit_]]
        if open_end:
            end_i = len(self.days) - 1
        bars = exit_ - entry + (1 if open_end else 0)
        return dict(entry=self.signal_days[entry], exit=self.days[end_i], bars=bars,
                    weeks=round(bars * (1.0 if self.frequency == 'weekly' else 0.2), 4),
                    ret=round(float(self.values[end_i] / self.values[start_i] - 1), 6), open_end=open_end,
                    entry_tri=round(float(self.values[start_i] / self.window_values[0]), 6),
                    exit_tri=round(float(self.values[end_i] / self.window_values[0]), 6))


class Store:
    def __init__(self, db_path: str):
        self.db = db_path
        self.cache: dict[tuple, Lab] = {}
        self.walks: dict[tuple, dict] = {}
        self.walks2: dict[tuple, dict] = {}
        self.lock = threading.Lock()

    def lab(self, security: str, start: str, end: str, cash: str, frequency: str = 'daily') -> Lab:
        key = (security, start, end, cash, frequency)
        with self.lock:
            if key not in self.cache:
                if len(self.cache) > 6:
                    self.cache.clear()
                self.cache[key] = Lab(security, start, end, cash, self.db, frequency)
            return self.cache[key]

    def walk(self, security: str, start: str, end: str, cash: str, frequency: str = 'daily') -> dict:
        key = (security, start, end, cash, frequency)
        with self.lock:
            if key not in self.walks:
                if len(self.walks) > 6:
                    self.walks.clear()
                out = ma_walk.walk(security, start=start, end=end, cash_rate=cash,
                                   frequency=frequency, db_path=self.db, with_curves=True)
                curves = out.pop('curves', None) or {}
                out['dates'] = curves.get('dates', [])
                out['curves'] = {name: _round(values, 6) for name, values in curves.items() if name != 'dates'}
                out['ticker'] = out.get('ticker', security)
                out['name'] = next((s['name'] for s in self.stocks() if s['ticker'] == out['ticker']), '')
                self.walks[key] = out
            return self.walks[key]

    def wf2(self, security: str, segment: str = 'dev') -> dict:
        """Six walk-forward lines for one stock; the assignments come from the cached batch results."""
        key = ('wf2', security.upper(), segment)
        with self.lock:
            if key not in self.walks2:
                if len(self.walks2) > 6:
                    self.walks2.clear()
                if segment == 'dev':
                    data = json.loads((WF2 / 'wf2_dev.json').read_text(encoding='utf8'))
                    stock = next((row for row in data['stocks'] if row['ticker'].upper() == security.upper()), None)
                    if stock is None:
                        raise ValueError(f'{security} is not in the wf_v2 development results')
                    out = ma_walk2.walk2(security, db_path=self.db,
                                         b_assignment={int(year): int(combo)
                                                       for year, combo in stock['b'].items()},
                                         c_assignments={int(seed): {int(year): (None if combo is None
                                                                                else int(combo))
                                                                    for year, combo in years.items()}
                                                        for seed, years in stock['c'].items()},
                                         with_curves=True)
                else:
                    frozen = json.loads((WF2 / 'frozen_2024-06-28.json').read_text(encoding='utf8'))
                    dev = json.loads((WF2 / 'wf2_dev.json').read_text(encoding='utf8'))
                    stock = next((row for row in frozen['stocks'] if row['ticker'].upper() == security.upper()), None)
                    dev_stock = next((row for row in dev['stocks']
                                      if row['ticker'].upper() == security.upper()), None)
                    if stock is None or dev_stock is None:
                        raise ValueError(f'{security} has no frozen pick or no development result')
                    walk = ma_walk2.load_walk(security, FULL_DB, '2014-02-28', ma_lab.HOLDOUT_END, True)
                    out = ma_walk2.holdout_lines(walk, int(stock['combo']), int(frozen['common']['combo']),
                                                 {int(seed): (None if combo is None else int(combo))
                                                  for seed, combo in frozen['borrow'][stock['ticker']].items()},
                                                 float(dev_stock['mix_share']))
                    out.update(ticker=walk.ticker, security_id=walk.sid)
                out['segment'] = segment
                out['combo_names'] = [f'{kind.upper()} {fast}/{slow}' for kind, fast, slow in ma_walk2.COMBOS]
                dates = out.pop('dates', [])
                out['dates'] = dates
                out['curves'] = {name: _round(values, 6) for name, values in out.pop('curves', {}).items()}
                self.walks2[key] = out
            return self.walks2[key]

    def stocks(self) -> list[dict]:
        return ma_walk.stock_list(self.db)


STORE: Store


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):        # keep the console readable
        pass

    def _send(self, body: bytes, kind: str, status: int = 200):
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        body = json.dumps(clean(payload), ensure_ascii=False, allow_nan=False)
        self._send(body.encode('utf8'), 'application/json; charset=utf-8', status)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if parsed.path in ('/', '/index.html'):
                self._send((HERE / 'dashboard.html').read_bytes(), 'text/html; charset=utf-8')
            elif parsed.path == '/api/stocks':
                self._json(dict(stocks=STORE.stocks(), types=TYPES, fasts=FASTS, slows=SLOWS))
            elif parsed.path == '/api/wf':
                walk = STORE.walk(query.get('security', 'MSFT'), query.get('start', DEFAULT_START),
                                  query.get('end', DEFAULT_END), query.get('cash', 'zero'),
                                  query.get('freq', 'daily'))
                self._json(walk)
            elif parsed.path == '/api/wf2':
                self._json(STORE.wf2(query.get('security', 'MSFT'), query.get('segment', 'dev')))
            elif parsed.path == '/api/wf2board':
                name = f"wf2_{query.get('segment', 'dev')}.json"
                path = WF2 / name
                if not path.exists():
                    self._json(dict(error=f'wf_v2 缓存还没生成（{name}）',
                                    hint='python -X utf8 research/wf_v2/run_wf2.py'), 404)
                else:
                    self._send(path.read_bytes(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/wfboard':
                name = f"wf_{query.get('freq', 'daily')}_{query.get('cash', 'zero')}.json"
                path = ROOT / 'research' / 'wf_v1' / name
                if not path.exists():
                    self._json(dict(error=f'滚动验证缓存还没生成（{name}）',
                                    hint='python -X utf8 research/wf_v1/run_wf.py'), 404)
                else:
                    self._send(path.read_bytes(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/leaderboard':
                name = f"leaderboard_{query.get('freq', 'daily')}_{query.get('cash', 'zero')}.json"
                path = HERE / name
                if not path.exists():
                    self._json(dict(error=f'排行榜还没生成（{name}）',
                                    hint='python -X utf8 research/ma_lab_v1/build_leaderboard.py'), 404)
                else:
                    self._send(path.read_bytes(), 'application/json; charset=utf-8')
            elif parsed.path == '/api/overview':
                lab = STORE.lab(query.get('security', 'MSFT'), query.get('start', DEFAULT_START),
                                query.get('end', DEFAULT_END), query.get('cash', 'zero'),
                                query.get('freq', 'daily'))
                self._json(dict(meta=lab.meta, buy_hold=lab.hold, combos=lab.rows_with_winrate(),
                                price=dict(dates=lab.window_days, tri=_round(lab.window_values / lab.window_values[0]),
                                           close=_round(lab.closes))))
            elif parsed.path == '/api/combo':
                lab = STORE.lab(query.get('security', 'MSFT'), query.get('start', DEFAULT_START),
                                query.get('end', DEFAULT_END), query.get('cash', 'zero'),
                                query.get('freq', 'daily'))
                self._json(lab.combo(query['type'], int(query['fast']), int(query['slow'])))
            else:
                self._json(dict(error='not found'), 404)
        except KeyError as error:
            self._json(dict(error=f'missing parameter: {error}'), 400)
        except Exception as error:                      # surface the message instead of a stack trace
            self._json(dict(error=f'{type(error).__name__}: {error}'), 400)


def main():
    global STORE
    parser = argparse.ArgumentParser(description='single-stock moving-average dashboard')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    STORE = Store(args.db)
    # One thread on purpose: the sqlite connection behind api.connect belongs to the thread that opened it.
    # No address reuse: on Windows two servers can otherwise share the port and answer with stale code.
    class Server(HTTPServer):
        allow_reuse_address = False

    url = f'http://127.0.0.1:{args.port}/'
    try:
        server = Server(('127.0.0.1', args.port), Handler)
    except OSError as error:
        busy = getattr(error, 'winerror', None) == 10048 or error.errno in (errno.EADDRINUSE, 48)
        if not busy:
            raise
        # a second launch is not an error: the dashboard is already up, so just point the browser at it
        print(f'port {args.port} is already in use; the dashboard is already running at {url}', flush=True)
        if args.open:
            webbrowser.open(url)
        return
    print(f'ma_lab dashboard on {url}  (Ctrl+C to stop)', flush=True)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
