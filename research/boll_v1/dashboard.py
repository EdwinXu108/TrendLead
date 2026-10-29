"""Local dashboard for the weekly Bollinger prediction points and monthly/quarterly zones.

A copy of `research/ma_lab_v1/dashboard.py` cut down to one page: for every stock in the top 50 the weekly
close, the 20-week Hann mid line, the weeks where the mid line is predicted to stop falling, and the four
monthly/quarterly zone lines with the weeks that sit near or below them. Only the standard library and
numpy, like the rest of the project.

    python -X utf8 research/boll_v1/dashboard.py --open

Then open http://127.0.0.1:8766 (the browser opens by itself with --open).

No restart is ever needed after a code change: the page is read from disk on every request, the
calculation module is reloaded when its file changes, and launching the dashboard again while an older
copy holds the port shuts that copy down and starts the current one.
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import importlib
import json
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import boll_weekly, ma_walk  # noqa: E402

DEFAULT_DB = boll_weekly.DEFAULT_DB
DEFAULT_STOCK = 'META'
PORT = 8766
SERVER_FILE = Path(__file__).resolve()
MODULE_FILE = Path(boll_weekly.__file__).resolve()


def code_version() -> str:
    """Fingerprint of the server and calculation code; a running copy with another one is out of date."""
    h = hashlib.sha256()
    for path in (SERVER_FILE, MODULE_FILE):
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


VERSION = code_version()


def _round(values, digits=6):
    out = []
    for value in values:
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            out.append(None)
        else:
            out.append(round(float(value), digits))
    return out


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


class Store:
    def __init__(self, db_path: str):
        self.db = db_path
        self.cache: dict[tuple, dict] = {}
        self.lock = threading.Lock()
        self._stocks: list[dict] | None = None
        self.depths: dict[int, dict] = {}
        self.module_mtime = MODULE_FILE.stat().st_mtime

    def _fresh(self):
        """Reload the calculation module when its file changed, so an edit shows up without a restart."""
        mtime = MODULE_FILE.stat().st_mtime
        if mtime != self.module_mtime:
            importlib.reload(boll_weekly)
            self.cache.clear()
            self.depths.clear()
            self.module_mtime = mtime

    def _depth_table(self, quarter_n: int) -> dict:
        """{ticker: depth series} plus {week: every stock's depth that week}, built once per quarter length."""
        key = int(quarter_n)
        if key not in self.depths:
            table = {row['ticker']: boll_weekly.depth_series(row['ticker'], self.db, quarter_n=key)
                     for row in self.stocks()}
            by_week: dict[str, list] = {}
            for item in table.values():
                for week, value in zip(item['weeks'], item['depth']):
                    by_week.setdefault(week, []).append(value)
            self.depths[key] = dict(table=table, by_week=by_week)
        return self.depths[key]

    def rank(self, week: str, quarter_n: int, width: float = boll_weekly.DEFAULT_WIDTH) -> dict:
        """Every stock's depth and 关注度 in one week, deepest first."""
        key = int(quarter_n)
        with self.lock:
            self._fresh()
            depths = self._depth_table(key)
            market = boll_weekly.market_zone(self.db, key, float(width))
        table = depths['table']
        level = market['level'].get(week, 0)
        week_values = depths['by_week'].get(week, [])
        rows = []
        for row in self.stocks():
            item = table[row['ticker']]
            try:
                k = item['weeks'].index(week)
            except ValueError:
                continue
            if item['depth'][k] is None:
                continue
            rows.append(dict(ticker=row['ticker'], name=row['name'], depth=round(item['depth'][k], 4),
                             gap=None if item['gap'][k] is None else round(item['gap'][k], 6),
                             trend=item['trend'][k],
                             attention=round(boll_weekly.attention(item['depth'][k], week_values, level), 1)))
        rows.sort(key=lambda r: (r['depth'], r['ticker']))
        for k, row in enumerate(rows, 1):
            row['rank'] = k
        return dict(week=week, quarter_n=key, count=len(rows), rows=rows, market_level=level,
                    market_names=market['names'].get(week, []))

    def stocks(self) -> list[dict]:
        if self._stocks is None:
            self._stocks = ma_walk.stock_list(self.db, scope=boll_weekly.SCOPE)
        return self._stocks

    def boll(self, ticker: str, quarter_n: int = boll_weekly.DEFAULT_QUARTER_N,
             width: float = boll_weekly.DEFAULT_WIDTH) -> dict:
        ticker = ticker.upper()
        if ticker not in {row['ticker'].upper() for row in self.stocks()}:
            raise ValueError(f'{ticker} is outside {boll_weekly.SCOPE}')
        key = (ticker, int(quarter_n), float(width))
        with self.lock:
            self._fresh()
            if key not in self.cache:
                if len(self.cache) > 12:
                    self.cache.clear()
                data = boll_weekly.series(ticker, self.db, quarter_n=int(quarter_n), width=float(width))
                name = next((row['name'] for row in self.stocks() if row['ticker'].upper() == ticker), '')
                data['meta']['name'] = name
                data['s_pct'] = _round(data['s_pct'])
                for field in ('close', 'mid', 's'):
                    data[field] = _round(data[field])
                for field in ('upper', 'lower', 'mid'):
                    data['band'][field] = _round(data['band'][field])
                for mark in data['marks']:
                    mark['mid'] = round(mark['mid'], 6)
                    mark['close'] = round(mark['close'], 6)
                for field, values in data['lines'].items():
                    data['lines'][field] = _round(values)
                for row in data['zone_rows']:
                    row['start_close'] = round(row['start_close'], 6)
                    row['min_close'] = round(row['min_close'], 6)
                by_week = self._depth_table(int(quarter_n))['by_week']
                data['attention'] = [round(boll_weekly.attention(d, by_week.get(w, []), m), 1)
                                     for w, d, m in zip(data['weeks'], data['depth'], data['market_level'])]
                self.cache[key] = data
            return self.cache[key]


STORE: Store
SERVER: HTTPServer | None = None


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
                self._json(dict(stocks=STORE.stocks(), default=DEFAULT_STOCK,
                                start=boll_weekly.DEV_START, end=boll_weekly.DEV_END,
                                quarters=list(boll_weekly.QUARTER_NS), widths=list(boll_weekly.WIDTHS),
                                default_quarter=boll_weekly.DEFAULT_QUARTER_N,
                                default_width=boll_weekly.DEFAULT_WIDTH))
            elif parsed.path == '/api/boll':
                self._json(STORE.boll(query.get('security', DEFAULT_STOCK),
                                      int(query.get('quarter', boll_weekly.DEFAULT_QUARTER_N)),
                                      float(query.get('d', boll_weekly.DEFAULT_WIDTH))))
            elif parsed.path == '/api/rank':
                self._json(STORE.rank(query['week'], int(query.get('quarter', boll_weekly.DEFAULT_QUARTER_N)),
                                      float(query.get('d', boll_weekly.DEFAULT_WIDTH))))
            elif parsed.path == '/api/version':
                self._json(dict(version=VERSION))
            elif parsed.path == '/api/shutdown':
                # asked by a newer launch of this dashboard; the server only listens on 127.0.0.1
                self._json(dict(ok=True))
                if SERVER is not None:
                    threading.Thread(target=SERVER.shutdown, daemon=True).start()
            else:
                self._json(dict(error='not found'), 404)
        except KeyError as error:
            self._json(dict(error=f'missing parameter: {error}'), 400)
        except Exception as error:                      # surface the message instead of a stack trace
            self._json(dict(error=f'{type(error).__name__}: {error}'), 400)


def _get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return json.loads(response.read().decode('utf8'))
    except Exception:
        return None


def main():
    global STORE, SERVER
    parser = argparse.ArgumentParser(description='weekly Bollinger prediction-point dashboard')
    parser.add_argument('--port', type=int, default=PORT)
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    STORE = Store(args.db)
    # One thread on purpose: the sqlite connection behind api.connect belongs to the thread that opened it.
    # No address reuse: on Windows two servers can otherwise share the port and answer with stale code.
    class Server(HTTPServer):
        allow_reuse_address = False

    url = f'http://127.0.0.1:{args.port}/'

    def bind():
        try:
            return Server(('127.0.0.1', args.port), Handler)
        except OSError as error:
            if getattr(error, 'winerror', None) == 10048 or error.errno in (errno.EADDRINUSE, 48):
                return None
            raise

    server = bind()
    if server is None:
        running = _get(url + 'api/version')
        if running and running.get('version') == VERSION:
            # the same code is already serving: just point the browser at it
            print(f'the dashboard is already running at {url}', flush=True)
            if args.open:
                webbrowser.open(url)
            return
        # an older copy holds the port: ask it to stop, then take the port over
        print(f'an older dashboard is running at {url}; replacing it', flush=True)
        _get(url + 'api/shutdown')
        for _ in range(50):
            time.sleep(0.1)
            server = bind()
            if server is not None:
                break
        if server is None:
            print(f'port {args.port} is held by a copy that cannot be stopped from here; close its window '
                  f'(or end that python process) and run this again', flush=True)
            return
    SERVER = server
    print(f'boll dashboard on {url}  (Ctrl+C to stop)', flush=True)
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
