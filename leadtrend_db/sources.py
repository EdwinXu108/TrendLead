"""Raw archive for leadtrend_db.

Every byte the builder reads is stored under data/db/raw/ and indexed by manifest.jsonl (one JSON object per line,
append-only). A URL is fetched once; later runs read the stored file and verify its sha256. Requests to SEC are
spaced at least MIN_INTERVAL seconds apart (at most ~4.8 per second) and retried with backoff. The SEC User-Agent
is read from config/v05_stage2.json and is never printed, logged or written.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_DIR = ROOT / 'data' / 'db'
RAW_DIR = DB_DIR / 'raw'
MANIFEST = RAW_DIR / 'manifest.jsonl'
DB_PATH = DB_DIR / 'leadtrend_db_v1.sqlite'
EXPORT_DIR = DB_DIR / 'exports'
CONFIG_PATH = ROOT / 'config' / 'v05_stage2.json'
OVERRIDES_PATH = ROOT / 'config' / 'db_overrides_v1.csv'
V05_NORMALIZED = ROOT / 'data' / 'v05' / 'normalized'
V05_RAW = ROOT / 'data' / 'v05' / 'raw'

CIK = '0000036405'                 # Vanguard Index Funds (trust); the fund series is below
FUND_SERIES = 'S000002839'         # Vanguard 500 Index Fund
VOO_CLASS = 'C000092055'           # VOO share class
MIN_INTERVAL = 0.21                # seconds between SEC requests (investor cap: 5 per second)
BACKOFF = (2, 4, 8, 16)            # seconds before retry 1..4
RETRYABLE_HTTP = {403, 429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A URL could not be read after retries, or a permanent HTTP error occurred."""


class RawMissing(RuntimeError):
    """Offline build asked for a file that is not in the raw archive."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_user_agent(path: Path = CONFIG_PATH) -> str:
    """Read sec_user_agent. The value is returned to the caller only; it is never printed."""
    cfg = json.loads(Path(path).read_text(encoding='utf-8'))
    ua = str(cfg.get('sec_user_agent') or '').strip()
    if not ua:
        raise FetchError('config sec_user_agent is empty; fill it in config/v05_stage2.json')
    return ua


@dataclass
class Stats:
    requests: int = 0
    retries: int = 0
    failures: int = 0
    bytes_downloaded: int = 0
    cache_reads: int = 0
    local_copies: int = 0


class RawStore:
    """Cache-first reader for SEC and local inputs. offline=True forbids any network request."""

    def __init__(self, raw_dir: Path = RAW_DIR, user_agent: str | None = None, offline: bool = False):
        self.raw_dir = Path(raw_dir)
        self.user_agent = user_agent
        self.offline = offline
        self.stats = Stats()
        self._last_request = 0.0
        self.records: dict[str, dict] = {}
        manifest = self.raw_dir / 'manifest.jsonl'
        if manifest.exists():
            for line in manifest.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rec = json.loads(line)
                    if rec['url'] in self.records:
                        raise RuntimeError(f"manifest lists {rec['url']} twice")
                    self.records[rec['url']] = rec

    # -- archive -------------------------------------------------------------------------------------------------
    def has(self, url: str) -> bool:
        return url in self.records

    def _append(self, rec: dict) -> None:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        with (self.raw_dir / 'manifest.jsonl').open('a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + '\n')
        self.records[rec['url']] = rec

    def _read(self, url: str) -> bytes:
        rec = self.records[url]
        data = (self.raw_dir / rec['path']).read_bytes()
        if sha256_bytes(data) != rec['sha256']:
            raise RuntimeError(f"raw file {rec['path']} does not match its manifest sha256")
        self.stats.cache_reads += 1
        return data

    def _store_new(self, url: str, relpath: str, data: bytes, kind: str, source: str, note: str) -> bytes:
        path = self.raw_dir / relpath
        if path.exists():
            # written by an interrupted run before its manifest line; never archived, so it is replaced
            if any(rec['path'] == relpath.replace('\\', '/') for rec in self.records.values()):
                raise RuntimeError(f'refusing to overwrite archived raw file {relpath}')
            path.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        rec = dict(url=url, path=relpath.replace('\\', '/'), kind=kind, sha256=sha256_bytes(data), bytes=len(data),
                   retrieved_at_utc=utc_now_iso(), source=source, note=note)
        self._append(rec)
        return data

    def get(self, url: str, relpath: str, kind: str, note: str = '') -> bytes:
        """Stored bytes for url; fetched from SEC and archived on first use."""
        if url in self.records:
            return self._read(url)
        if self.offline:
            raise RawMissing(f'not in raw archive (offline build): {url}')
        return self._store_new(url, relpath, self._fetch(url), kind, 'sec_fetch', note)

    def get_optional(self, url: str, relpath: str, kind: str, note: str = '') -> bytes | None:
        """Like get(), but a 404 returns None (recorded in the archive as absent)."""
        try:
            return self.get(url, relpath, kind, note)
        except FetchError as exc:
            if str(exc).startswith('HTTP 404'):
                return None
            raise

    def get_json(self, url: str, relpath: str, kind: str, note: str = '') -> dict:
        return json.loads(self.get(url, relpath, kind, note).decode('utf-8'))

    def import_local(self, url: str, relpath: str, kind: str, src: Path, note: str) -> bytes:
        """Archive a file already on disk (for example a v05 raw file fetched from the same SEC URL)."""
        if url in self.records:
            return self._read(url)
        if self.offline:
            raise RawMissing(f'not in raw archive (offline build): {url}')
        self.stats.local_copies += 1
        return self._store_new(url, relpath, Path(src).read_bytes(), kind, 'local_copy', note)

    # -- network -------------------------------------------------------------------------------------------------
    def _throttle(self) -> None:
        wait = MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _fetch(self, url: str) -> bytes:
        if not self.user_agent:
            raise FetchError('no SEC User-Agent loaded')
        headers = {'User-Agent': self.user_agent, 'Accept-Encoding': 'gzip, deflate'}
        last = 'unknown'
        for attempt in range(len(BACKOFF) + 1):
            self._throttle()
            self.stats.requests += 1
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
                    raw = resp.read()
                    encoding = (resp.headers.get('Content-Encoding') or '').lower()
                if encoding == 'gzip' or raw[:2] == b'\x1f\x8b':
                    raw = gzip.decompress(raw)
                elif encoding == 'deflate':
                    raw = zlib.decompress(raw)
                self.stats.bytes_downloaded += len(raw)
                return raw
            except urllib.error.HTTPError as exc:
                last = f'HTTP {exc.code}'
                if exc.code not in RETRYABLE_HTTP:
                    self.stats.failures += 1
                    raise FetchError(f'{last} for {url}') from None
            except (urllib.error.URLError, TimeoutError, OSError, zlib.error) as exc:
                last = type(exc).__name__
            if attempt < len(BACKOFF):
                self.stats.retries += 1
                time.sleep(BACKOFF[attempt])
        self.stats.failures += 1
        raise FetchError(f'{last} after {len(BACKOFF)} retries for {url}')
