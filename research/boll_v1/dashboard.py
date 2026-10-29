"""Local dashboard for the weekly Bollinger prediction points.

A copy of `research/ma_lab_v1/dashboard.py` cut down to one page: for every stock in the top 50 the weekly
close, the 20-week Hann mid line and the weeks where the mid line is predicted to stop falling. Only the
standard library and numpy, like the rest of the project.

    python -X utf8 research/boll_v1/dashboard.py --open

Then open http://127.0.0.1:8766 (the browser opens by itself with --open).
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

from leadtrend_bt import boll_weekly, ma_walk  # noqa: E402

DEFAULT_DB = boll_weekly.DEFAULT_DB
DEFAULT_STOCK = 'META'
PORT = 8766


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
        self.cache: dict[str, dict] = {}
        self.lock = threading.Lock()
        self._stocks: list[dict] | None = None

    def stocks(self) -> list[dict]:
        if self._stocks is None:
            self._stocks = ma_walk.stock_list(self.db, scope=boll_weekly.SCOPE)
        return self._stocks

    def boll(self, ticker: str) -> dict:
        key = ticker.upper()
        if key not in {row['ticker'].upper() for row in self.stocks()}:
            raise ValueError(f'{ticker} is outside {boll_weekly.SCOPE}')
        with self.lock:
            if key not in self.cache:
                if len(self.cache) > 12:
                    self.cache.clear()
                data = boll_weekly.series(key, self.db)
                name = next((row['name'] for row in self.stocks() if row['ticker'].upper() == key), '')
                data['meta']['name'] = name
                data['s_pct'] = _round(data['s_pct'])
                for field in ('close', 'mid', 's'):
                    data[field] = _round(data[field])
                for field in ('upper', 'lower', 'mid'):
                    data['band'][field] = _round(data['band'][field])
                for mark in data['marks']:
                    mark['mid'] = round(mark['mid'], 6)
                    mark['close'] = round(mark['close'], 6)
                self.cache[key] = data
            return self.cache[key]


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
                self._json(dict(stocks=STORE.stocks(), default=DEFAULT_STOCK,
                                start=boll_weekly.DEV_START, end=boll_weekly.DEV_END))
            elif parsed.path == '/api/boll':
                self._json(STORE.boll(query.get('security', DEFAULT_STOCK)))
            else:
                self._json(dict(error='not found'), 404)
        except KeyError as error:
            self._json(dict(error=f'missing parameter: {error}'), 400)
        except Exception as error:                      # surface the message instead of a stack trace
            self._json(dict(error=f'{type(error).__name__}: {error}'), 400)


def main():
    global STORE
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
