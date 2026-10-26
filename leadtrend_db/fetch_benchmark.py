"""Archive the VOO benchmark series from OpenD (original NONE prices plus rehabilitation records).

VOO is the comparison benchmark only. It is never a pool security and never enters prices_daily; the builder reads
these archived responses and writes tri_daily rows with security_id 'BENCH:VOO'. Offline rebuilds read the archive.

Only quote interfaces are used: request_history_kline (autype NONE), get_rehab, get_corporate_actions_dividends and
get_history_kl_quota. No trade, order or account context is created.
"""
from __future__ import annotations
import ast, json, time
from datetime import datetime, timezone
from pathlib import Path

from .sources import RawStore, ROOT

WORK = ROOT / 'work' / 'benchmark_v1'
CONFIG = ROOT / 'config' / 'db_benchmark_v1.json'
START = '2012-12-01'
CODE = 'VOO'


def collect() -> str:
    import futu
    WORK.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    end = datetime.now(timezone.utc).date().isoformat()
    store = RawStore(offline=False)
    last = [0.0]
    urls: dict[str, object] = {'kline': [], 'stamp': stamp, 'start': START, 'end': end}

    def call(tag, method, **kwargs):
        key = f'opend://benchmark/{stamp}/{tag}'
        if key in store.records:
            cached = json.loads(store._read(key))
            if isinstance(cached.get('page'), str) and cached['page'].startswith("b'"):
                cached['page'] = ast.literal_eval(cached['page'])
            return cached, key
        for attempt in range(3):
            time.sleep(max(0, 0.63 - (time.monotonic() - last[0])))
            last[0] = time.monotonic()
            try:
                result = method(**kwargs)
                ret, data = int(result[0]), result[1]
                records = json.loads(data.to_json(orient='records')) if hasattr(data, 'to_json') else data
                page = result[2] if len(result) > 2 else None
                payload = dict(ret=ret, data=records, error=str(data) if ret else '', page=page,
                               params={k: str(v) for k, v in kwargs.items() if k != 'page_req_key'})
            except Exception as exc:                      # recorded, never silently dropped
                payload = dict(ret=-1, data=None, error=type(exc).__name__ + ': ' + str(exc), page=None, params={})
            raw = json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False).encode()
            store._store_new(key if attempt == 0 else key + f'_retry{attempt}',
                             f'opend/{stamp}/benchmark_{tag}{"" if attempt == 0 else f"_retry{attempt}"}.json',
                             raw, 'other', 'opend_sdk', tag)
            if payload['ret'] == 0:
                return payload, key
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        return payload, key

    ctx = futu.OpenQuoteContext(host='127.0.0.1', port=11111)
    try:
        p, u = call('quota_before', ctx.get_history_kl_quota, get_detail=True)
        urls['quota_before'] = u
        page = None
        for n in range(12):
            p, u = call(f'kline_{n}', ctx.request_history_kline, code='US.' + CODE, start=START, end=end,
                        ktype=futu.KLType.K_DAY, autype=futu.AuType.NONE, fields=[futu.KL_FIELD.ALL],
                        max_count=1000, page_req_key=page, extended_time=False)
            urls['kline'].append(u)
            if p['ret'] != 0:
                raise RuntimeError('VOO kline request failed: ' + p['error'])
            page = p['page']
            if not page:
                break
        for tag, func in (('rehab', ctx.get_rehab), ('dividends', ctx.get_corporate_actions_dividends)):
            p, u = call(tag, func, code='US.' + CODE)
            urls[tag] = u
            if p['ret'] != 0:
                raise RuntimeError(f'VOO {tag} request failed: ' + p['error'])
        p, u = call('quota_after', ctx.get_history_kl_quota, get_detail=True)
        urls['quota_after'] = u
    finally:
        ctx.close()
    raw = json.dumps(urls, ensure_ascii=False, sort_keys=True).encode()
    index_key = f'opend://benchmark/{stamp}/index'
    store._store_new(index_key, f'opend/{stamp}/benchmark_index.json', raw, 'other', 'opend_sdk', 'index')
    CONFIG.write_text(json.dumps(dict(index_url=index_key, start=START, end=end, code=CODE), indent=2),
                      encoding='utf8', newline='\n')
    print('COMPLETE', index_key, flush=True)
    return index_key


if __name__ == '__main__':
    collect()
