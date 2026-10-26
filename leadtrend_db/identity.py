"""Identity helpers: identifiers, names, share classes, security types, SEC lookups and the override file.

Identifier rules. A CUSIP or ISIN is used as a key only when it is a real value: placeholders such as 'N/A', blanks
and values failing the check digit are rejected. A US ISIN contains its CUSIP (characters 3 to 11), so that CUSIP is
derived; a non-US ISIN is linked to a CUSIP only through the v05 security table, whose evidence records the ISIN.

Security type comes from explicit evidence, strongest first: the OpenFIGI type text and the v05 security type of the
security (REIT, right, CVR, ETF), the holding's own title, the holding's name, the issuer's SIC (6798 REIT, 6770
blank-check/SPAC). The SEC asset category EC (common equity) is only the last basis, so an explicit REIT, right or CVR
is never overridden by it. An unidentified type is 'unknown', never common by default.

Names. name_key() removes what is only legal form or a state of incorporation written at the end of a name: SEC
state suffixes such as '\\DE\\', '/DE/', '/ MA'; the apostrophe; 'PLC' and 'PUBLIC LTD CO'; 'NV' and 'N.V.'. It does not
delete other words. name_signature() is order-free and space-free, so 'O REILLY' and 'OReilly' agree.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

CLASS_LETTER = re.compile(r'\bClass\s+([A-Z])\b')
PLACEHOLDERS = {'', 'N/A', 'NA', 'NONE', 'NULL', '-', '0', '000000000', 'UNKNOWN'}
CUSIP_SHAPE = re.compile(r'^[0-9A-Z]{9}$')
ISIN_SHAPE = re.compile(r'^[A-Z]{2}[0-9A-Z]{9}[0-9]$')
TICKER_SHAPE = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$')
OVERRIDE_KINDS = ('cusip_cik', 'name_cik', 'link', 'status', 'security_type', 'security_cusip', 'security_end',
                  'security_ticker', 'external_issuer', 'successor_review', 'security_class', 'security_link',
                  'issuer_start', 'issuer_end', 'link_rejected', 'continuity_ratio', 'price_start')

# security type evidence, most specific first
TYPE_RULES = [
    ('cvr', re.compile(r'\bCVRS?\b|CONTINGENT VALUE', re.I)),
    ('right', re.compile(r'\bRIGHTS?\b', re.I)),
    ('warrant', re.compile(r'\bWARRANTS?\b|\bWTS?\b', re.I)),
    ('etf', re.compile(r'\bETFS?\b|EXCHANGE[- ]TRADED FUND', re.I)),
    ('reit', re.compile(r'\bREIT\b|REAL ESTATE INVESTMENT TRUST', re.I)),
    ('partnership', re.compile(r'\bL\.?P\.?(?![A-Za-z])|LIMITED PARTNERSHIP|\bCOMMON UNITS?\b|\bPARTNERSHIP UNITS?\b|'
                               r'\bUNITS REPRESENTING\b|\bLP UNITS?\b', re.I)),
    ('adr', re.compile(r'\bADRS?\b|\bADSS?\b|AMERICAN DEPOSITARY', re.I)),
    ('preferred', re.compile(r'PREFERRED|\bPFD\b', re.I)),
]
V05_TYPE = {'reit': 'reit', 'contingent_value_right': 'cvr', 'etf': 'etf'}
EXCLUDED_TYPES = ('right', 'warrant', 'cvr', 'etf', 'reit', 'partnership', 'spac', 'preferred', 'other')
ELIGIBLE_TYPES = ('common', 'adr')   # investor boundary: common stock and ADRs of operating companies
# outside the stock weight denominator of the fund, non-stock types only (REIT stays in the fund's stock total)
NON_STOCK_TYPES = ('right', 'warrant', 'cvr', 'etf', 'other')

_STATE_TAIL = re.compile(r'\\[A-Z]{2}\\\s*$|\s*/\s?[A-Z]{2,4}/?\s*$')
_LEGAL_WORDS = {'INC', 'INCORPORATED', 'CORP', 'CORPORATION', 'CO', 'COMPANY', 'COS', 'LTD', 'LIMITED', 'PLC', 'NV',
                'SA', 'SE', 'AG', 'LLC', 'THE', 'AND', 'COMPANIES', 'CL', 'CLASS'}   # 'HOLDING', 'GROUP' are substantive


def issuer_id(cik: str | int) -> str:
    if str(cik).startswith('LEI:'):
        return str(cik)
    return f'CIK:{int(cik):010d}'


def cik10(cik: str | int) -> str:
    if str(cik).startswith('LEI:'):
        return str(cik)
    return f'{int(cik):010d}'


def cusip_security(cusip: str) -> str:
    return f'CUSIP:{cusip}'


def isin_security(isin: str) -> str:
    return f'ISIN:{isin}'


def cikcls_security(cik: str, class_code: str) -> str:
    if str(cik).startswith('LEI:'):
        return f'LEICLS:{str(cik)[4:]}:{class_code}'
    return f'CIKCLS:{cik10(cik)}:{class_code}'


def is_placeholder(text: str | None) -> bool:
    return (text or '').strip().upper() in PLACEHOLDERS


def cusip_check_ok(cusip: str) -> bool:
    """CUSIP check digit (standard algorithm): characters 1-8 weighted 1,2,1,2,... with digit-sum reduction."""
    s = (cusip or '').strip().upper()
    if len(s) != 9 or not CUSIP_SHAPE.match(s):
        return False
    total = 0
    for i, ch in enumerate(s[:8]):
        v = int(ch) if ch.isdigit() else (ord(ch) - 55 if ch.isalpha() else {'*': 36, '@': 37, '#': 38}.get(ch, -1))
        if v < 0:
            return False
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return (10 - total % 10) % 10 == int(s[8])


def isin_check_ok(isin: str) -> bool:
    """ISIN check digit (Luhn over the letter-expanded string)."""
    s = (isin or '').strip().upper()
    if not ISIN_SHAPE.match(s):
        return False
    digits = ''.join(str(int(c, 36)) for c in s[:-1])
    total, double = 0, True
    for ch in reversed(digits):
        d = int(ch)
        if double:
            d *= 2
            d = d // 10 + d % 10
        total += d
        double = not double
    return (10 - total % 10) % 10 == int(s[-1])


def valid_cusip(text: str | None) -> str:
    """The CUSIP if the value is a real one (not a placeholder and check digit correct), else ''."""
    s = (text or '').strip().upper()
    return s if not is_placeholder(s) and cusip_check_ok(s) else ''


def valid_isin(text: str | None) -> str:
    s = (text or '').strip().upper()
    return s if not is_placeholder(s) and isin_check_ok(s) else ''


def us_isin_cusip(isin: str) -> str:
    """For a US ISIN the CUSIP is characters 3-11 by definition; '' otherwise or if that CUSIP fails its check."""
    if isin.startswith('US') and cusip_check_ok(isin[2:11]):
        return isin[2:11]
    return ''


def valid_ticker(text: str | None) -> str:
    """A ticker-shaped code (letters, optional class suffix). Numeric or dated vendor codes are rejected."""
    s = (text or '').strip().upper().replace('-', '.')
    return s if TICKER_SHAPE.match(s) else ''


def class_code_from_text(text: str) -> str:
    special = re.search(r'\bSpecial\s+Class\s+([A-Z])\b|\bClass\s+([A-Z])\s+Special\b', text or '', re.I)
    if special:
        return f'CL_{(special.group(1) or special.group(2)).upper()}_SPECIAL'
    m = CLASS_LETTER.search(text or '')
    return f'CL_{m.group(1)}' if m else 'COM'


SPECIAL_CLASS = re.compile(r'\bSpecial\s+Class\s+[A-Z]\b', re.I)


def core_name(name: str) -> str:
    """Name with share-class text removed (used for matching and clustering): 'Class A' and 'Special Class A'."""
    return CLASS_LETTER.sub(' ', SPECIAL_CLASS.sub(' ', name or ''))


def name_tokens(text: str) -> list[str]:
    """Normalised words of a company name. Only legal-form words, state suffixes at the end and the apostrophe are
    removed; single letters written apart are joined (U.S. -> US)."""
    t = (text or '').upper().replace('’', "'").replace('&', ' AND ')
    t = _STATE_TAIL.sub(' ', t.strip())
    t = re.sub(r'\bPUBLIC\s+(LIMITED\s+COMPANY|LTD\.?(\s+CO\.?)?)\s*$', ' ', t)   # 'PUBLIC LTD CO' ends a name
    t = re.sub(r'\bN\s*\.?\s*V\s*\.?\s*$', ' ', t)                                  # 'N.V.' ends a name
    t = t.replace("'", '')
    words = [w for w in re.sub(r'[^A-Z0-9]+', ' ', t).split() if w not in _LEGAL_WORDS]
    out, run = [], ''
    for w in words:
        if len(w) == 1:
            run += w
            continue
        if run:
            out.append(run)
            run = ''
        out.append(w)
    if run:
        out.append(run)
    return out


def name_key(text: str) -> str:
    """Normalised name for matching: the words of name_tokens joined by one space."""
    return ' '.join(name_tokens(text))


def norm_core(name: str) -> str:
    return name_key(core_name(name))


def name_signature(text: str) -> str:
    """Order-free and space-free signature: 'O REILLY AUTOMOTIVE' and 'OREILLY AUTOMOTIVE' agree; so do word orders."""
    return ''.join(sorted(name_tokens(text)))


def name_ratio(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, norm_core(a), norm_core(b)).ratio()


def security_type(name: str, title: str = '', sic: str = '', evidence_type: str = '', asset_cat: str = '',
                  v05_type: str = '') -> tuple[str, str]:
    """(security_type, type_basis). Explicit evidence first: the v05 security type, the OpenFIGI type text, the
    holding's own title, the name, the issuer's SIC (6798 REIT, 6770 blank check). Only then the SEC asset category
    EC, which means common equity and never overrides an explicit kind."""
    if v05_type in V05_TYPE:
        return V05_TYPE[v05_type], f'v05 security type {v05_type}'
    for basis, text in (('openfigi_type', evidence_type), ('title', title), ('name', name)):
        for kind, pattern in TYPE_RULES:
            if text and pattern.search(text):
                return kind, f'{basis} matches {kind}'
    if sic == '6798':
        return 'reit', 'issuer SIC 6798 (REIT)'
    if sic == '6770':
        return 'spac', 'issuer SIC 6770 (blank check)'
    if asset_cat == 'EC':
        return 'common', 'SEC asset category EC (common equity), no explicit exclusion evidence'
    return 'unknown', 'no evidence of type'


def parse_cik_lookup(text: str) -> dict[str, set[str]]:
    """SEC cik-lookup-data.txt ('name:CIK:' lines) as normalised name -> set of 10-digit CIKs."""
    out: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = re.match(r'^(.*):(\d{1,10}):?\s*$', line.strip())
        if not m:
            continue
        key = name_key(m.group(1))
        if key:
            out.setdefault(key, set()).add(cik10(m.group(2)))
    return out


def parse_company_tickers(obj: dict) -> dict[str, dict]:
    """company_tickers.json as ticker -> {cik, title}."""
    out = {}
    for key in sorted(obj, key=lambda k: int(k)):
        row = obj[key]
        out[str(row['ticker']).upper()] = dict(cik=cik10(row['cik_str']), title=str(row.get('title', '')))
    return out


def ticker_key(ticker: str) -> str:
    return (ticker or '').strip().upper().replace('.', '-')


def v05_evidence_fields(evidence: str) -> dict[str, str]:
    """Key=value pieces of a v05 source_evidence string (cusip=, isin=, lei=, type=, 'SEC ticker corroboration CIK='
    and so on). openfigi_type and evidence_cik are the two values the identity code uses."""
    out = {}
    for part in (evidence or '').split(';'):
        if '=' in part:
            k, v = part.split('=', 1)
            out.setdefault(k.strip(), v.strip())
    m = re.search(r'type=([^;]*)', evidence or '')
    out['openfigi_type'] = m.group(1).strip() if m else ''
    cik = out.get('SEC ticker corroboration CIK', '')
    out['evidence_cik'] = cik10(cik) if cik.isdigit() else ''
    return out


def load_overrides(path: Path) -> list[dict]:
    """config/db_overrides_v1.csv. Columns: override_kind, key, value, field, reason, evidence (see docs). For
    cusip_cik and name_cik the field may hold a validity window 'YYYY-MM-DD..YYYY-MM-DD' (blank: no limit)."""
    if not Path(path).exists():
        return []
    with Path(path).open(encoding='utf-8', newline='') as f:
        rows = [{k: (v or '').strip() for k, v in r.items()} for r in csv.DictReader(f)]
    for r in rows:
        if r.get('override_kind') not in OVERRIDE_KINDS:
            raise ValueError(f"unknown override_kind {r.get('override_kind')!r} in {path}")
        if not r.get('reason') or not r.get('evidence'):
            raise ValueError(f"override for {r.get('key')} needs reason and evidence")
    return rows


def overrides_of(rows: list[dict], kind: str) -> dict[str, dict]:
    return {r['key']: r for r in rows if r['override_kind'] == kind}


def window_of(text: str) -> tuple[str, str]:
    """('from', 'to') of a validity window field; empty strings mean no limit."""
    if not text or '..' not in text:
        return '', ''
    a, b = text.split('..', 1)
    return a.strip(), b.strip()
