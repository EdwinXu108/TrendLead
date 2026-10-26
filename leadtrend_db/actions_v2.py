"""Corporate-action triage (2026-10-27 round).

Input is what price_data.assemble read from the raw archive: the per-security actions it already normalised, the raw
OpenD rehabilitation and dividend records per ticker, and the accepted price rows. Output is the final
corporate_actions set used by the total-return index, with one resolution string per row.

Rules, in the investor's order of precedence:
  1. Cash on one ex-date: the vendor rehabilitation per-share cash field decides the amount. When the endpoint
     reported two amounts, the candidates are the endpoint sum, the regular amount, the special amount and their sum;
     the candidate closest to the vendor's own price adjustment (within 2%) wins. That keeps a regular-plus-special
     pair, drops a duplicate classification, and restores a component the endpoint lost.
  2. A cash amount the endpoint dropped (no pay date, or a bank feed that mixes two issuers) is restored on its
     ex-date from the rehabilitation field, else from the vendor price adjustment. The pay date is never invented and
     the total-return index never uses it.
  3. An unparsed stock dividend ("1 share pays 0.00275 shares") is a split with ratio 1.00275.
  4. A spinoff keeps the documented shares-per-parent-share and the subsidiary code when the v05 normalised archive
     has them and the resulting value agrees with the vendor adjustment within 20%; otherwise the vendor price factor
     is the value evidence; four distributions the vendor feed misses entirely are listed in
     config/spinoff_overrides_v1.csv and valued at the ex-date opening drop.
  5. One vendor row can be read as both a join and a spinoff (2026 Honeywell); the duplicate split is dropped.
  6. Every session is checked against the vendor price factor: an adjustment that the stored actions do not explain
     within 2% becomes a review item. The check never creates an action.
"""
from __future__ import annotations
import bisect
import csv
import re
from collections import defaultdict
from datetime import date, timedelta

from .sources import ROOT, V05_NORMALIZED

CASH_TYPES = ('cash_dividend', 'special_dividend')
SPINOFF_CONFIG = ROOT / 'config' / 'spinoff_overrides_v1.csv'
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


def finalize(actions: dict, action_raw: dict, prices: dict, code_of: dict, log=print):
    """Return (rows, stats, reviews). rows carry every column of corporate_actions except action_id."""
    stats = defaultdict(int)
    reviews: list[dict] = []
    children = _v05_spinoff_children()

    def add_review(kind, subject, detail):
        reviews.append(dict(review_type=kind, subject_id=subject, snapshot_id='', detail=detail, source_id=''))

    def rehab(code: str, day: str):
        """Combined view of the archived rehabilitation rows of one ex-date.

        The archive repeats rows from more than one source file, and a date can carry two distinct components (a base
        and a variable dividend). Identical rows collapse; distinct rows add their cash and compose their price
        factors (x -> A*x + B applied in order), which is what the vendor series does.
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
        merged = {}
        for row in distinct:
            for key, value in row.items():
                if merged.get(key) in (None, '') and value not in (None, ''):
                    merged[key] = value
        merged['per_cash_div'] = sum(_num(r.get('per_cash_div')) or 0.0 for r in distinct) or None
        merged['special_dividend'] = sum(_num(r.get('special_dividend')) or 0.0 for r in distinct) or None
        a_total, b_total = 1.0, 0.0
        for row in distinct:
            factor = _num(row.get('forward_adj_factorA'))
            offset = _num(row.get('forward_adj_factorB')) or 0.0
            if factor is None:
                continue
            b_total = b_total * factor + offset
            a_total *= factor
        merged['forward_adj_factorA'] = a_total
        merged['forward_adj_factorB'] = b_total
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
            cash = [r for r in group if r['action_type'] in CASH_TYPES]
            kept.extend(r for r in group if r['action_type'] not in CASH_TYPES)
            vendor = rehab(code, day) or {}
            per = _num(vendor.get('per_cash_div')) or 0.0
            special = _num(vendor.get('special_dividend')) or 0.0
            implied = _vendor_implied_cash(vendor, previous_close(sid, day))
            if day in stock_dividend_days:
                continue                       # the stock dividend is a share distribution, handled below
            if not cash and not (per or special) and not implied:
                continue
            if day not in prices.get(sid, {}):
                continue                       # an action outside the accepted price history is not used
            our_total = sum(float(r['cash_amount'] or 0) for r in cash)
            candidates = sorted({round(v, 6) for v in (our_total, per, special, per + special) if v > 1e-9})
            base = max(cash, key=lambda r: r['cash_amount'] or 0) if cash else dict(
                security_id=sid, action_date=day, action_type='cash_dividend', ratio=None, cash_amount=None,
                needs_review=0, currency='USD', source='opend', source_id=vendor.get('_source_id'), notes='')
            if our_total > 1e-9 and implied:
                best = min(candidates, key=lambda c: abs(c - implied))
                if abs(best - implied) <= MATCH * implied:
                    total, how = best, 'vendor_adjustment_confirms_candidate'
                else:
                    total, how = our_total, 'endpoint_amount_vendor_disagrees'
                    if abs(implied / our_total - 1) > 0.20:
                        add_review('price_action_scan', sid,
                                   f'{day}: endpoint cash {our_total:g} vs vendor implied {implied:.4f}; endpoint kept')
            elif our_total > 1e-9:
                total, how = our_total, 'endpoint_amount'
            elif candidates:
                total, how = candidates[-1], 'rehab_per_cash_div_exdate_only'
            else:
                total, how = implied, 'vendor_factor_implied'
                add_review('price_action_scan', sid, f'{day}: cash restored from the vendor factor only: {implied:.4f}')
            kept.append(dict(base, action_type='cash_dividend', cash_amount=round(total, 6),
                             needs_review=int(base.get('needs_review', 0) if how.startswith('endpoint_amount') else 0),
                             resolution=how,
                             notes=(f'{base.get("notes", "")};triage={how};endpoint_amounts='
                                    + '|'.join(f'{float(r["cash_amount"]):.6g}' for r in cash if r['cash_amount'])
                                    + f';rehab_per_cash={per:g};rehab_special={special:g}'
                                    + (f';vendor_implied={implied:.6f}' if implied else ''))))
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
        seen = set()
        for row in vendor_rows:                     # the archive repeats rows from more than one source file
            factor = _num(row.get('forward_adj_factorA'))
            if factor is not None:
                seen.add((str(row.get('ex_div_date', ''))[:10], factor, _num(row.get('forward_adj_factorB')) or 0.0))
        events = sorted(seen)
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
