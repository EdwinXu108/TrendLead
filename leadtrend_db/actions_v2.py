"""Triage corporate actions using identified economic events and versioned evidence.

Vendor adjustment factors are diagnostic, not independent confirmation of cash amounts.
Ambiguous revisions or unidentified components remain explicitly under review.
"""
from __future__ import annotations
import bisect
import csv
import json
import re
from collections import defaultdict
from datetime import date, timedelta

from .sources import ROOT, V05_NORMALIZED

CASH_TYPES = ('cash_dividend', 'special_dividend')
SPINOFF_CONFIG = ROOT / 'config' / 'spinoff_overrides_v1.csv'
CASH_OVERRIDES = ROOT / 'config' / 'db_cash_overrides_v1.json'
MATCH = 0.02                     # tolerance when matching a candidate against the vendor adjustment
CHILD_TOL = 0.20                 # a child-price value this far from the vendor value is not used
STOCK_DIV = re.compile(r'(\d+(?:\.\d+)?)\s*股\s*派\s*(\d+(?:\.\d+)?)\s*股')


def _num(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def _v05_spinoff_children() -> dict[tuple[str, str], list[dict]]:
    """(parent ticker, child session date) -> documented child distributions from the v05 normalised archive."""
    sec_path = V05_NORMALIZED / 'securities.csv'
    child_path = V05_NORMALIZED / 'spinoff_child_prices.csv'
    if not (sec_path.exists() and child_path.exists()):
        return {}
    alias, ticker = {}, {}
    with sec_path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            ticker[row['security_id']] = row.get('ticker', '')
            alias[row['security_id']] = row['security_id']
            for item in str(row.get('canonical_aliases', '')).split('|'):
                if item.strip():
                    alias[item.strip()] = row['security_id']
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with child_path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            match = re.match(r'CA_SPINOFF_(.+)_(\d{4}-\d{2}-\d{2})_[0-9a-f]+$', row['event_id'])
            if not match:
                continue
            parent = ticker.get(alias.get(match.group(1), match.group(1)), '')
            shares = _num(row['shares_per_old'])
            close = _num(row['raw_close'])
            if not parent or shares is None or close is None:
                continue
            out[(parent, row['session_date'])].append(dict(child=row['child_ticker'], shares=shares, close=close,
                                                           value=shares * close))
    return out


def _spinoff_config() -> list[dict]:
    if not SPINOFF_CONFIG.exists():
        return []
    with SPINOFF_CONFIG.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def _vendor_implied_cash(vendor: dict, prev_close: float | None) -> float | None:
    """Cash per share implied by the vendor price factor of a pure cash event."""
    if not vendor:
        return None
    if any(vendor.get(k) for k in ('split_base', 'join_base', 'spin_off_base', 'spin_off_ratio')):
        return None
    factor = _num(vendor.get('forward_adj_factorA'))
    offset = _num(vendor.get('forward_adj_factorB')) or 0.0
    if factor is None or prev_close is None or (abs(factor - 1) < 1e-12 and abs(offset) < 1e-12):
        return None
    value = (1 - factor) * prev_close - offset
    return value if value > 0 else None


def resolve_cash_components(rows: list[dict], vendor: dict, override: dict | None = None) -> dict:
    """Resolve only identified economic components; vendor adjustment factors are diagnostic.

    An explicit component_id distinguishes real same-amount components. Unidentified competing
    records remain unresolved; a provisional amount is never promoted to confirmed evidence.
    """
    if override is not None:
        return dict(amount=float(override['official_cash']), resolution='official_cash_override',
                    needs_review=0, evidence=override['source_url'])
    components: dict[str, set[float]] = defaultdict(set)
    unidentified = []
    for row in rows:
        amount = _num(row.get('cash_amount'))
        if amount is None or amount <= 0:
            continue
        ident = str(row.get('component_id') or '')
        if ident:
            components[ident].add(amount)
        else:
            unidentified.append(amount)
    if components and not unidentified and all(len(v) == 1 for v in components.values()):
        return dict(amount=sum(next(iter(v)) for v in components.values()), resolution='identified_components',
                    needs_review=0, evidence='explicit component identities')
    amounts = sorted(set(unidentified))
    per, special = _num(vendor.get('per_cash_div')) or 0, _num(vendor.get('special_dividend')) or 0
    conflict = bool(components) or len(unidentified) > 1 or bool(vendor.get('_cash_revision_conflict'))
    if len(amounts) == 1:
        amount = amounts[0]
        if per and abs(per-amount) > 1e-8:
            conflict = True
        if special and abs(special-amount) > 1e-8:
            conflict = True
    elif amounts:
        amount = sum(amounts)
    elif per or special:
        amount = per if per else special
        conflict = conflict or bool(per and special)
    else:
        return dict(amount=None, resolution='no_cash_evidence', needs_review=int(conflict), evidence='')
    return dict(amount=amount, resolution='cash_conflict_unresolved' if conflict else 'vendor_reported',
                needs_review=int(conflict), evidence='reported amount; factor is not independent confirmation')


def finalize(actions: dict, action_raw: dict, prices: dict, code_of: dict, log=print):
    """Return (rows, stats, reviews). rows carry every column of corporate_actions except action_id."""
    stats = defaultdict(int)
    reviews: list[dict] = []
    children = _v05_spinoff_children()
    overrides = {}
    if CASH_OVERRIDES.exists():
        data = json.loads(CASH_OVERRIDES.read_text(encoding='utf8'))
        overrides = {(r['security_id'], r['ex_date']): r for r in data['entries']}

    def add_review(kind, subject, detail):
        reviews.append(dict(review_type=kind, subject_id=subject, snapshot_id='', detail=detail, source_id=''))

    def rehab(code: str, day: str):
        """Combined view of the archived rehabilitation rows of one ex-date.

        Deduplicate identical source records deterministically; do not add conflicting revisions or factors.
        Component identity and independent evidence are required to resolve ambiguous cash.
        """
        rows = [r for r in action_raw.get(code, ([], []))[0] if str(r.get('ex_div_date', ''))[:10] == day]
        if not rows:
            return None
        keys, distinct = set(), []
        for row in rows:
            key = tuple(_num(row.get(k)) for k in ('per_cash_div', 'special_dividend', 'forward_adj_factorA',
                                                   'forward_adj_factorB', 'split_base', 'split_ert', 'join_base',
                                                   'join_ert', 'spin_off_base', 'spin_off_ert', 'spin_off_ratio'))
            if key in keys:
                continue
            keys.add(key)
            distinct.append(row)
        distinct.sort(key=lambda r: json.dumps(r, sort_keys=True, default=str, ensure_ascii=False))
        merged = {}
        for row in distinct:
            for key, value in row.items():
                if merged.get(key) in (None, '') and value not in (None, ''):
                    merged[key] = value
        for key in ('per_cash_div', 'special_dividend'):
            vals = sorted({_num(r.get(key)) for r in distinct if _num(r.get(key)) is not None})
            merged[key] = vals[0] if vals else None
            if len(vals) > 1:
                merged['_cash_revision_conflict'] = True
        # Only a single, unambiguous factor is meaningful as a diagnostic; never compose revisions.
        factors = {(_num(r.get('forward_adj_factorA')), _num(r.get('forward_adj_factorB'))) for r in distinct}
        if len(factors) != 1:
            merged['forward_adj_factorA'] = merged['forward_adj_factorB'] = None
        return merged

    def previous_close(sid: str, day: str):
        prior = [d for d in prices.get(sid, {}) if d < day]
        return prices[sid][max(prior)][0]['close'] if prior else None

    final: dict[str, list[dict]] = {}
    for sid, rows in actions.items():
        code = code_of.get(sid, '')
        stock_dividend_days = {str(item.get('ex_date', '')).replace('/', '-')[:10]
                               for item in action_raw.get(code, ([], []))[1]
                               if STOCK_DIV.search(str(item.get('statement', '')))}
        by_date: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            by_date[row['action_date']].append(dict(row))
        vendor_dates = {str(r.get('ex_div_date', ''))[:10] for r in action_raw.get(code, ([], []))[0]}
        kept: list[dict] = []
        for day in sorted(set(by_date) | (vendor_dates & set(prices.get(sid, {})))):
            group = by_date.get(day, [])
            cash = sorted((r for r in group if r['action_type'] in CASH_TYPES),
                          key=lambda r: (r['action_type'], r.get('cash_amount') or 0, r.get('source_id') or '',
                                         r.get('notes') or ''))
            kept.extend(r for r in group if r['action_type'] not in CASH_TYPES)
            vendor = rehab(code, day) or {}
            per = _num(vendor.get('per_cash_div')) or 0.0
            special = _num(vendor.get('special_dividend')) or 0.0
            implied = _vendor_implied_cash(vendor, previous_close(sid, day))
            if day in stock_dividend_days:
                continue                       # the stock dividend is a share distribution, handled below
            if not cash and not (per or special):
                continue
            if day not in prices.get(sid, {}):
                continue                       # an action outside the accepted price history is not used
            our_total = sum(float(r['cash_amount'] or 0) for r in cash)
            base = max(cash, key=lambda r: r['cash_amount'] or 0) if cash else dict(
                security_id=sid, action_date=day, action_type='cash_dividend', ratio=None, cash_amount=None,
                needs_review=0, currency='USD', source='opend', source_id=vendor.get('_source_id'), notes='')
            resolved = resolve_cash_components(cash, vendor, overrides.get((sid, day)))
            total, how = resolved['amount'], resolved['resolution']
            if total is None:
                continue
            if resolved['needs_review']:
                add_review('cash_conflict', sid, f'{day}: competing cash records; no independent resolution')
            kept.append(dict(base, action_type='cash_dividend', cash_amount=round(total, 6),
                             needs_review=resolved['needs_review'],
                             resolution=how,
                             notes=(f'{base.get("notes", "")};triage={how};endpoint_amounts='
                                    + '|'.join(f'{float(r["cash_amount"]):.6g}' for r in cash if r['cash_amount'])
                                    + f';rehab_per_cash={per:g};rehab_special={special:g}'
                                    + (f';vendor_implied_diagnostic={implied:.6f}' if implied else '')
                                    + f';evidence={resolved["evidence"]}')))
            stats['cash_' + how] += 1
        final[sid] = kept

    # 3. unparsed stock dividends become splits
    for sid, rows in list(final.items()):
        code = code_of.get(sid, '')
        for item in action_raw.get(code, ([], []))[1]:
            statement = str(item.get('statement', ''))
            match = STOCK_DIV.search(statement)
            if not match:
                continue
            day = str(item.get('ex_date', '')).replace('/', '-')[:10]
            if day not in prices.get(sid, {}):
                continue
            if any(r['action_date'] == day and r['action_type'] in ('split', 'reverse_split') for r in rows):
                continue
            ratio = 1 + float(match.group(2)) / float(match.group(1))
            rows.append(dict(security_id=sid, action_date=day, action_type='split', ratio=round(ratio, 10),
                             cash_amount=None, needs_review=0, currency='USD', source='opend',
                             source_id=item.get('_source_id'), sub_ticker='', vendor_factor=None, spin_value=None,
                             resolution='stock_dividend_parsed', notes=f'statement={statement};ratio=1+paid/held'))
            stats['stock_dividend_split'] += 1

    # 4. spinoffs and class distributions
    for sid, rows in final.items():
        code = code_of.get(sid, '')
        for row in rows:
            if row['action_type'] not in ('spinoff', 'class_distribution'):
                continue
            vendor = rehab(code, row['action_date']) or {}
            factor = _num(vendor.get('spin_off_ratio'))
            if factor is None and _num(vendor.get('spin_off_base')) and _num(vendor.get('spin_off_ert')):
                factor = _num(vendor['spin_off_ert']) / _num(vendor['spin_off_base'])
            if factor is None and _num(vendor.get('split_base')) and _num(vendor.get('split_ert')):
                # the vendor encoded this distribution as a split (DELL/VMware 2021-11-02): its price factor is the
                # retained share of the parent, which is what the spinoff value needs
                factor = _num(vendor.get('forward_adj_factorA'))
            row['vendor_factor'] = factor
            if row['action_type'] == 'class_distribution':
                if _num(vendor.get('split_base')) and _num(vendor.get('split_ert')):
                    row['ratio'] = round(_num(vendor['split_ert']) / _num(vendor['split_base']), 10)
                    row['resolution'] = 'class_distribution_vendor_split_ratio'
                continue
            hits = children.get((code, row['action_date']), [])
            if not hits:
                for delta in (-3, -2, -1, 1, 2, 3):
                    other = (date.fromisoformat(row['action_date']) + timedelta(days=delta)).isoformat()
                    hits = children.get((code, other), [])
                    if hits:
                        break
            child_value = round(sum(h['value'] for h in hits), 6) if hits else None
            prev_close = previous_close(sid, row['action_date'])
            vendor_value = (1 - factor) * prev_close if (factor and prev_close) else None
            if child_value and vendor_value and abs(child_value / vendor_value - 1) > CHILD_TOL:
                add_review('price_action_scan', sid,
                           f'{row["action_date"]}: child-price value {child_value:.2f} vs vendor {vendor_value:.2f}; '
                           f'the vendor factor is used')
                child_value = None
                stats['spinoff_child_rejected'] += 1
            if child_value:
                row['sub_ticker'] = ','.join(h['child'] for h in hits)
                row['ratio'] = round(sum(h['shares'] for h in hits), 10) if len(hits) == 1 else None
                row['spin_value'] = child_value
                row['resolution'] = 'child_close_times_documented_ratio'
                stats['spinoff_child_value'] += 1
            elif factor:
                row['sub_ticker'] = ','.join(h['child'] for h in hits) if hits else row.get('sub_ticker', '')
                row['resolution'] = 'vendor_factor'
                stats['spinoff_vendor_factor'] += 1
            else:
                row['resolution'] = 'spinoff_no_value_evidence'
                row['needs_review'] = 1
                stats['spinoff_no_value'] += 1
    for item in _spinoff_config():
        sid = item['security_id']
        if sid not in final or item['action_date'] not in prices.get(sid, {}):
            continue
        rows = final[sid]
        if any(r['action_date'] == item['action_date'] and r['action_type'] == 'spinoff' for r in rows):
            continue
        rows.append(dict(security_id=sid, action_date=item['action_date'], action_type='spinoff', ratio=None,
                         cash_amount=None, needs_review=0, currency='USD', source='config', source_id=None,
                         sub_ticker=item['sub_ticker'], vendor_factor=None, spin_value=None,
                         resolution=item['value_basis'],
                         notes=f'{item["reason"]} | {item["evidence"]}'))
        stats['spinoff_added_from_config'] += 1

    # 5. a split recorded by the same vendor row as a spinoff is a duplicate
    for sid, rows in final.items():
        spins = {r['action_date'] for r in rows if r['action_type'] == 'spinoff'}
        keep = []
        for row in rows:
            if row['action_type'] in ('split', 'reverse_split') and row['action_date'] in spins:
                add_review('price_action_scan', sid,
                           f'{row["action_date"]}: split and spinoff on one date; the split recorded by the same '
                           f'vendor row is dropped (the spinoff factor covers the whole adjustment)')
                stats['split_dropped_beside_spinoff'] += 1
                continue
            keep.append(row)
        final[sid] = keep

    # 6. vendor-factor consistency scan
    explained, mismatched = _scan(action_raw, prices, code_of, final)
    for sid, day, vendor_adj, our_adj in mismatched:
        add_review('price_action_scan', sid,
                   f'{day}: vendor adjustment {vendor_adj:.6f} vs stored actions {our_adj:.6f}')
    stats['scan_days'] = explained + len(mismatched)
    stats['scan_mismatch'] = len(mismatched)

    out = [row for sid in sorted(final) for row in sorted(final[sid], key=lambda r: (r['action_date'],
                                                                                     r['action_type']))]
    for row in out:
        row.setdefault('sub_ticker', '')
        row.setdefault('vendor_factor', None)
        row.setdefault('spin_value', None)
        row.setdefault('resolution', '')
    return out, dict(stats), reviews


def _scan(action_raw, prices, code_of, final):
    """Compare the vendor's day-over-day price adjustment with the adjustment implied by the stored actions.

    Both sides are expressed as a return multiplier: a split with ratio r and a cash c on a previous close p give
    r and p/(p-c). An action between the two sessions (on a date without a price) belongs to the later session.
    """
    explained, mismatched = 0, []
    for sid, bars in prices.items():
        code = code_of.get(sid, '')
        vendor_rows = action_raw.get(code, ([], []))[0]
        by_date = defaultdict(set)
        for row in vendor_rows:                     # the archive repeats rows from more than one source file
            factor = _num(row.get('forward_adj_factorA'))
            if factor is not None:
                by_date[str(row.get('ex_div_date', ''))[:10]].add((factor, _num(row.get('forward_adj_factorB')) or 0.0))
        # Different factors on one date may be revisions, not separate actions; omit ambiguous diagnostics.
        events = sorted((day, *next(iter(values))) for day, values in by_date.items() if len(values) == 1)
        if not events:
            continue
        days = [e[0] for e in events]
        n = len(events)
        a_suf, b_suf = [1.0] * (n + 1), [0.0] * (n + 1)
        for i in range(n - 1, -1, -1):
            a_suf[i] = events[i][1] * a_suf[i + 1]
            b_suf[i] = events[i][2] * a_suf[i + 1] + b_suf[i + 1]

        def adj(day, close):
            k = bisect.bisect_right(days, day)
            return close * a_suf[k] + b_suf[k]

        acts = defaultdict(list)
        for row in final.get(sid, []):
            acts[row['action_date']].append(row)
        action_days = sorted(acts)
        sessions = sorted(bars)
        for prev, day in zip(sessions, sessions[1:]):
            c0, c1 = bars[prev][0]['close'], bars[day][0]['close']
            if not c0 or not c1:
                continue
            vendor_adj = adj(day, c1) / adj(prev, c0) / (c1 / c0)
            our_adj = 1.0
            for d in action_days[bisect.bisect_right(action_days, prev):bisect.bisect_right(action_days, day)]:
                for row in acts[d]:
                    if row['action_type'] in ('split', 'reverse_split') and row['ratio']:
                        our_adj *= row['ratio']
                    elif row['action_type'] in CASH_TYPES and row['cash_amount']:
                        our_adj *= c0 / max(1e-9, c0 - row['cash_amount'])
                    elif row['action_type'] == 'spinoff':
                        value = row.get('spin_value')
                        if value is None and row.get('vendor_factor'):
                            value = (1 - row['vendor_factor']) * c0
                        if value:
                            our_adj *= c0 / max(1e-9, c0 - value)
                    elif row['action_type'] == 'class_distribution' and row['ratio']:
                        our_adj *= row['ratio']
            if abs(vendor_adj / our_adj - 1) > MATCH:
                mismatched.append((sid, day, vendor_adj, our_adj))
            else:
                explained += 1
    return explained, mismatched
