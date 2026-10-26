"""Quarterly Top 200 candidate pool, rule_version top200_v1 (rules.py).

compute() is called by build.py with the resolved holdings of every complete, reconciled snapshot. It returns the
tables of the pool: pool versions, company ranks, the historical top 200 and their eligible securities, the strategy
list after the status cutoff, and the checks of each quarter. Amounts are integer cents of the reported value_usd.

Membership bound (why a quarter can be ready). A company outside the top 200 can enter only if its mapped amount m_c
plus the unresolved amount U_c that could belong to it reaches the 200th company's amount A200. Its mapped amount is
at most A201 (the 201st company), so the gap A200 - A201 bounds any single unknown company. For every outside company c
named as a possible issuer by an unresolved line we check m_c + U_c < A200; unresolved lines naming no candidate are
summed as one group and must stay below the gap. A violated check blocks the quarter.
"""
from __future__ import annotations

from collections import Counter

from . import rules as R
from .identity import ELIGIBLE_TYPES, NON_STOCK_TYPES

EXCLUDED_CANDIDATE_TYPES = ('reit', 'partnership', 'spac', 'preferred', 'other') + tuple(NON_STOCK_TYPES)


def cents(value_usd: float) -> int:
    return int(round(float(value_usd or 0.0) * R.CENT))


def rank_snapshot(snap: dict) -> dict:
    """Company amounts of one snapshot and the ranking. Only resolved, eligible lines add to a company."""
    comp: dict[str, dict] = {}
    unresolved: list[dict] = []
    for ln in snap['lines']:
        if ln['line_kind'] != 'stock':
            continue
        typ = ln.get('type', 'unknown')
        if typ in EXCLUDED_CANDIDATE_TYPES:
            continue
        val = cents(ln['value_usd'])
        if not ln.get('issuer'):
            if typ in ELIGIBLE_TYPES or typ == 'unknown':
                unresolved.append(dict(cents=val, cands=set(ln.get('cands') or []), name=ln['raw_name'],
                                       line_no=ln['line_no'], typ=typ))
            continue
        c = comp.setdefault(ln['issuer'], dict(cents=0, eligible_lines=0, secs={}, best=None))
        if typ in ELIGIBLE_TYPES:
            c['cents'] += val
            c['eligible_lines'] += 1
            sk = ln.get('sec_key') or ''
            c['secs'][sk] = c['secs'].get(sk, 0) + val
            if c['best'] is None or val > c['best'][0]:
                c['best'] = (val, ln['raw_name'], ln['line_no'])
        else:   # resolved issuer, type not known: the amount may or may not be eligible
            unresolved.append(dict(cents=val, cands={ln['issuer']}, name=ln['raw_name'], line_no=ln['line_no'], typ=typ))
    ranked = sorted((cik for cik in comp if comp[cik]['eligible_lines'] > 0),
                    key=lambda cik: (-comp[cik]['cents'], cik))
    return dict(comp=comp, ranked=ranked, unresolved=unresolved)


def bound(ranking: dict, top_n: int = R.TOP_N) -> dict:
    comp, ranked, unresolved = ranking['comp'], ranking['ranked'], ranking['unresolved']
    top = set(ranked[:top_n])
    a200 = comp[ranked[top_n - 1]]['cents'] if len(ranked) >= top_n else 0
    a201 = comp[ranked[top_n]]['cents'] if len(ranked) > top_n else 0
    gap = a200 - a201
    per_cand: dict[str, int] = {}
    per_cand_lines: dict[str, int] = {}
    empty_total, empty_lines = 0, 0
    for u in unresolved:
        if not u['cands']:
            empty_total += u['cents']
            empty_lines += 1
            continue
        for c in u['cands']:
            per_cand[c] = per_cand.get(c, 0) + u['cents']
            per_cand_lines[c] = per_cand_lines.get(c, 0) + 1
    blocking = []
    for c, u_c in sorted(per_cand.items()):
        if c in top or a200 == 0:
            continue
        m_c = comp[c]['cents'] if c in comp else 0
        if m_c + u_c >= a200:
            blocking.append(dict(issuer=c, mapped_cents=m_c, unresolved_cents=u_c, lines=per_cand_lines[c]))
    unknown_blocks = a200 > 0 and empty_total >= gap
    return dict(top=top, a200=a200, a201=a201, gap=gap, blocking=blocking, unknown_group_cents=empty_total,
                unknown_group_lines=empty_lines, unknown_blocks=unknown_blocks, per_cand=per_cand,
                per_cand_lines=per_cand_lines)


def _tickers_now(build, cik: str) -> str:
    """Current ticker of an issuer: the v05 ticker of its own eligible CUSIP security if any, else the one SEC ticker
    of the issuer (only when there is exactly one)."""
    for sk, f in sorted(build.sec_facts.items()):
        if f.get('issuer') == cik and sk.startswith('CUSIP:') and build.sec_type.get(sk, ('',))[0] in ELIGIBLE_TYPES:
            tk = build.v05.get(sk[6:], {}).get('ticker', '')
            if tk and tk != 'BRK.A':
                return tk
    tks = sorted(k for k, v in build.tickers.items() if v['cik'] == cik and '-' not in k)
    return tks[0] if len(tks) == 1 else ''


def compute(build, snaps: list[dict], primary: list[dict], info: dict, final: dict, resolved: dict,
            status_as_of: str) -> dict:
    out = {k: [] for k in ('pool_versions', 'company_ranks', 'top200_companies', 'top200_securities',
                           'strategy_companies', 'strategy_securities', 'quarter_checks')}
    versions = sorted((s for s in snaps if s['completeness'] == 'full' and s['reconciled']),
                      key=lambda s: (s['report_date'], s['acceptance'], s['accession']))
    canonical = {s['snapshot_id'] for s in primary}
    supersedes: dict[str, str] = {}
    by_period: dict[str, list[dict]] = {}
    for s in versions:
        by_period.setdefault(s['report_date'], []).append(s)
    for vs in by_period.values():
        vs = sorted(vs, key=lambda s: (s['acceptance'], s['accession']))
        for prev, cur in zip(vs, vs[1:]):
            supersedes[cur['snapshot_id']] = prev['snapshot_id']
    for s in versions:
        vid = s['snapshot_id']
        is_canon = vid in canonical
        rk = rank_snapshot(s)
        bd = bound(rk)
        tot = s['total'] or 0.0
        counts = Counter(rk['comp'][c]['cents'] for c in rk['ranked'])
        reasons = []
        if (s['form'] != 'NPORT-P' and s['residual'] is None) or s.get('unparsed', 0):
            reasons.append('parse_not_reconciled')
        if len(rk['ranked']) < R.TOP_N:
            reasons.append(f'fewer_than_{R.TOP_N}_eligible_companies')
        if bd['blocking']:
            reasons.append('unresolved_lines_could_change_membership:' + ','.join(b['issuer'] for b in bd['blocking'][:5]))
        if bd['unknown_blocks']:
            reasons.append('unresolved_lines_without_candidate_reach_gap')
        if bd['a200'] and bd['a200'] == bd['a201']:
            reasons.append('tie_at_boundary_200_201')
        if not is_canon:
            state, why = 'alternate_version', 'the same report period has a canonical version'
        elif reasons:
            state, why = 'blocked', '; '.join(reasons)
        else:
            state, why = 'ready', 'membership bound holds; boundary identities resolved'
        out['pool_versions'].append(dict(
            version_id=vid, report_period=s['report_date'], snapshot_id=vid, form=s['form'], accession=s['accession'],
            filed_date=s['filing_date'], available_at_utc=s['acceptance'], completeness=s['completeness'],
            reconciled=s['reconciled'], is_canonical=int(is_canon), supersedes_version_id=supersedes.get(vid),
            version_note=('later filing of the same period' if vid in supersedes else 'first filing of the period'),
            rule_version=R.RULE_VERSION, quarter_state=state, quarter_reason=why, source_id=build_source(s)))
        top = bd['top']
        for n, cik in enumerate(rk['ranked'], 1):
            c = rk['comp'][cik]
            status = 'in_top200' if cik in top else (
                'boundary_outside' if R.BOUNDARY_RANGE[0] <= n <= R.BOUNDARY_RANGE[1] else 'outside')
            out['company_ranks'].append(dict(
                version_id=vid, rank=n, issuer_id=f'CIK:{cik}', amount_cents=c['cents'], eligible_lines=c['eligible_lines'],
                tie_group=int(counts[c['cents']] > 1), in_top200=int(cik in top), boundary_status=status,
                unresolved_lines=bd['per_cand_lines'].get(cik, 0), unresolved_cents=bd['per_cand'].get(cik, 0),
                candidate_bound_cents=bd['per_cand'].get(cik, 0)))
        # the top-200 membership of every public version (a time query may return a later filing of a period); the
        # strategy list below is built for the canonical version of each period only
        for n, cik in enumerate(rk['ranked'][:R.TOP_N], 1):
            c = rk['comp'][cik]
            member_blocked = bd['per_cand_lines'].get(cik, 0) > 0
            out['top200_companies'].append(dict(
                version_id=vid, rank=n, issuer_id=f'CIK:{cik}', issuer_name_at_period=c['best'][1] if c['best'] else '',
                amount_cents=c['cents'], fund_weight_raw=(c['cents'] / R.CENT / tot) if tot else None,
                tie_group=int(counts[c['cents']] > 1), eligible_securities=len(c['secs']),
                blocked_members=int(member_blocked), rule_version=R.RULE_VERSION))
            for sk, amt in sorted(c['secs'].items()):
                cusip = sk[6:] if sk.startswith('CUSIP:') else ''
                forbidden = security_block(build, sk)
                out['top200_securities'].append(dict(
                    version_id=vid, issuer_id=f'CIK:{cik}', security_id=sk,
                    class_code=build.sec_facts.get(sk, {}).get('cls', ''),
                    security_type=build.sec_type.get(sk, ('unknown', ''))[0],
                    ticker_at_period=build.v05.get(cusip, {}).get('ticker', '') if cusip else '',
                    holding_lines=0, amount_cents=amt, fund_weight_raw=(amt / R.CENT / tot) if tot else None,
                    candidate_eligible=0 if forbidden else 1,
                    candidate_reason=(f'blocked: {forbidden}' if forbidden else 'eligible class held in the quarter')))
        out['quarter_checks'] += quarter_checks(vid, s, rk, bd, out)
        if not is_canon:
            continue
        # strategy: the historical list filtered by the status at the cutoff; blocked members kept as blocked
        seen_sec: dict[str, str] = {}
        conflicts: list[str] = []
        open_succ = {r['subject_id'] for r in build.review if r['review_type'] == 'successor_candidate'}
        for n, cik in enumerate(rk['ranked'][:R.TOP_N], 1):
            rc_cik = resolved.get(cik, (cik, ''))[0]
            rc = f'CIK:{rc_cik}'
            st = final.get(rc_cik, {})
            cur = st.get('status', 'unknown')
            reason_block, reason_excl = '', ''
            if cur == 'delisted' and (cik in open_succ or rc_cik in open_succ):
                reason_block = 'delisted at the cutoff but a successor candidate is open for review (not excluded)'
            elif cur == 'delisted':
                reason_excl = f'delisted at status cutoff {R.STATUS_CUTOFF}: {st.get("evidence", "")}'
            elif cur == 'unknown' or st.get('needs_review'):
                reason_block = f'status {cur} needs review at cutoff {R.STATUS_CUTOFF}'
            elif bd['per_cand_lines'].get(cik, 0):
                reason_block = 'unresolved line in this quarter names the company'
            eligible = int(not reason_block and not reason_excl)
            out['strategy_companies'].append(dict(
                version_id=vid, issuer_id=f'CIK:{cik}', historical_rank=n, status_cutoff=R.STATUS_CUTOFF,
                current_status=cur, resolved_current_issuer=rc, current_ticker=_tickers_now(build, rc_cik),
                strategy_eligible=eligible, blocked_reason=reason_block, excluded_reason=reason_excl))
            for sk, amt in sorted(c_secs(rk, cik).items()):
                cusip = sk[6:] if sk.startswith('CUSIP:') else ''
                forbidden = security_block(build, sk)
                sec_issuer = build.sec_facts.get(sk, {}).get('issuer')
                same = sec_issuer == rc_cik
                if sk in seen_sec:      # one security key in two companies of one quarter is an identity conflict
                    conflicts.append(f'{sk} in {seen_sec[sk]} and {cik}')
                    continue
                seen_sec[sk] = cik
                out['strategy_securities'].append(dict(
                    version_id=vid, issuer_id=f'CIK:{cik}', security_id=sk,
                    class_code=build.sec_facts.get(sk, {}).get('cls', ''),
                    current_security_id=sk if same else '',
                    current_ticker=build.v05.get(cusip, {}).get('ticker', '') if (cusip and same) else '',
                    strategy_eligible=int(eligible and not forbidden and same),
                    reason=(f'blocked: {forbidden}' if forbidden else
                            ('predecessor security; successor security not identified in archive' if not same else
                             ('company not eligible' if not eligible else 'eligible class at the cutoff')))))
        out['quarter_checks'].append(dict(
            version_id=vid, check_name='strategy_security_single_company', status='fail' if conflicts else 'pass',
            detail='; '.join(conflicts[:5]) if conflicts else 'no security key belongs to two companies of the quarter'))
        if conflicts:      # an identity conflict is not a ready quarter
            for pv in out['pool_versions']:
                if pv['version_id'] == vid:
                    pv['quarter_state'] = 'blocked'
                    pv['quarter_reason'] = (pv['quarter_reason'] + '; security key in two companies').lstrip('; ')
    return out


def security_block(build, sk: str) -> str:
    """Why a security may not be a candidate, whatever its issuer: the investor's security boundary (BRK.A) or a
    dated end of the security (evidence in config/db_overrides_v1.csv). Empty string when nothing blocks it."""
    cusip = sk[6:] if sk.startswith('CUSIP:') else ''
    if R.FORBIDDEN_CUSIPS.get(cusip):
        return R.FORBIDDEN_CUSIPS[cusip] + ' (investor boundary)'
    ended = build.end_over.get(sk)
    if ended:
        return f'security ended {ended["field"]}: {ended["reason"]}'
    return ''


def c_secs(rk: dict, cik: str) -> dict:
    return rk['comp'][cik]['secs']


def build_source(s: dict) -> str:
    """source_id of the raw document a snapshot was parsed from (same rule as build.source_id)."""
    from .sources import sha256_bytes
    return 'SRC-' + sha256_bytes(s['raw'])[:16]


def quarter_checks(vid: str, s: dict, rk: dict, bd: dict, out: dict) -> list[dict]:
    rows = []

    def add(name, ok, detail):
        rows.append(dict(version_id=vid, check_name=name, status='pass' if ok else 'fail', detail=detail))
    if s['form'] == 'NPORT-P':   # structured XML: complete when the schedule covers the fund's stock total
        add('parse_reconciled', s['completeness'] == 'full', f'N-PORT lines={len(s["lines"])} completeness={s["completeness"]}')
    else:
        add('parse_reconciled', s['residual'] is not None and not s.get('unparsed', 0),
            f'residual_usd={s["residual"]} bound_usd={s["bound"]} unparsed={s.get("unparsed", 0)}')
    add('ranked_companies_200', len(rk['ranked']) >= R.TOP_N, f'eligible companies={len(rk["ranked"])}')
    a200 = rk['ranked'][R.TOP_N - 1] if len(rk['ranked']) >= R.TOP_N else ''
    a201 = rk['ranked'][R.TOP_N] if len(rk['ranked']) > R.TOP_N else ''
    add('boundary_200_201_distinct', bd['a200'] != bd['a201'] or not a201,
        f'rank200={a200} cents={bd["a200"]}; rank201={a201} cents={bd["a201"]}; gap_cents={bd["gap"]}')
    add('membership_bound', not bd['blocking'] and not bd['unknown_blocks'],
        f'blocking candidates={len(bd["blocking"])}; unknown group cents={bd["unknown_group_cents"]} '
        f'in {bd["unknown_group_lines"]} lines; gap={bd["gap"]}')
    return rows
