"""Identity helpers: identifiers, names, share classes, security types, SEC lookups and the override file.

Identifier rules (repair round). A CUSIP or ISIN is used as a key only when it is a real value: placeholders such as
'N/A', blanks and values failing the check digit are rejected. A US ISIN contains its CUSIP (characters 3 to 11), so
that CUSIP is derived; a non-US ISIN is linked to a CUSIP only through the v05 security table, whose evidence records
the ISIN of each security. Security type comes from evidence text (OpenFIGI type in the v05 evidence, the holding's
title or name) and from the SEC asset category; an unidentified type is 'unknown', never common by default.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

from leadtrend_v05.p3_external import norm_name

CLASS_LETTER = re.compile(r'\bClass\s+([A-Z])\b')
PLACEHOLDERS = {'', 'N/A', 'NA', 'NONE', 'NULL', '-', '0', '000000000', 'UNKNOWN'}
CUSIP_SHAPE = re.compile(r'^[0-9A-Z]{9}$')
ISIN_SHAPE = re.compile(r'^[A-Z]{2}[0-9A-Z]{9}[0-9]$')
TICKER_SHAPE = re.compile(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$')
OVERRIDE_KINDS = ('cusip_cik', 'name_cik', 'link', 'status', 'security_type', 'security_cusip')

# security type evidence, most specific first
TYPE_RULES = [
    ('cvr', re.compile(r'\bCVRS?\b|CONTINGENT VALUE', re.I)),
    ('right', re.compile(r'\bRIGHTS?\b', re.I)),
    ('warrant', re.compile(r'\bWARRANTS?\b|\bWTS?\b', re.I)),
    ('etf', re.compile(r'\bETFS?\b|EXCHANGE[- ]TRADED FUND', re.I)),
    ('partnership', re.compile(r'\bL\.?P\.?(?![A-Za-z])|LIMITED PARTNERSHIP|\bCOMMON UNITS?\b|\bPARTNERSHIP UNITS?\b|'
                               r'\bUNITS REPRESENTING\b|\bLP UNITS?\b', re.I)),
    ('adr', re.compile(r'\bADRS?\b|\bADSS?\b|AMERICAN DEPOSITARY', re.I)),
    ('preferred', re.compile(r'PREFERRED|\bPFD\b', re.I)),
]
EXCLUDED_TYPES = ('right', 'warrant', 'cvr', 'etf', 'other')   # outside the stock weight denominator


def issuer_id(cik: str | int) -> str:
    return f'CIK:{int(cik):010d}'


def cik10(cik: str | int) -> str:
    return f'{int(cik):010d}'


def cusip_security(cusip: str) -> str:
    return f'CUSIP:{cusip}'


def isin_security(isin: str) -> str:
    return f'ISIN:{isin}'


def cikcls_security(cik: str, class_code: str) -> str:
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
    m = CLASS_LETTER.search(text or '')
    return f'CL_{m.group(1)}' if m else 'COM'


def core_name(name: str) -> str:
    """Name with share-class text removed (used for matching and clustering)."""
    return CLASS_LETTER.sub(' ', name or '')


SEC_SUFFIX = re.compile(r'/[A-Za-z]{2,4}')   # SEC state or era suffix pieces: /DE/ /MO/ /NY/ /NEW/ /OLD/ /The


def name_key(text: str) -> str:
    """Normalised name for matching. Apostrophes go (McDonald's -> mcdonalds); SEC suffixes such as /DE/, /MO/, /NEW/
    go (MATTEL INC /DE/ -> MATTEL INC); '&' and 'and' go; initials written apart are joined (U.S. -> US, C.H. -> CH);
    then norm_name drops the corporate words and punctuation."""
    t = (text or '').replace("'", '').replace('’', '')
    t = SEC_SUFFIX.sub(' ', t).replace('&', ' ')
    words = [w for w in norm_name(t).split() if w not in ('AND', 'COS', 'COMPANIES')]
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
    return ' '.join(out)


def norm_core(name: str) -> str:
    return name_key(core_name(name))


def name_signature(text: str) -> str:
    """Order-free signature of a name: 'LILLY ELI & CO' and 'ELI LILLY & CO' give the same signature."""
    return ' '.join(sorted(name_key(text).split()))


def name_ratio(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, norm_core(a), norm_core(b)).ratio()


def security_type(name: str, title: str = '', sic: str = '', evidence_type: str = '', asset_cat: str = '') -> tuple[str, str]:
    """(security_type, type_basis). Evidence in order: the OpenFIGI type text, the holding's own title, the name,
    the issuer's SIC (REIT). An SEC common-equity asset category alone is the last basis for 'common'."""
    if sic == '6798':
        return 'reit', 'issuer SIC 6798 (REIT)'
    for basis, text in (('openfigi_type', evidence_type), ('title', title), ('name', name)):
        for kind, pattern in TYPE_RULES:
            if text and pattern.search(text):
                return kind, f'{basis} matches {kind}'
    if re.search(r'REAL ESTATE INVESTMENT TRUST|\bREIT\b', f'{name} {title}', re.I):
        return 'reit', 'name/title says REIT'
    if asset_cat == 'EC':
        return 'common', 'SEC asset category EC (common equity), no excluding evidence'
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
    """Key=value pieces of a v05 source_evidence string (cusip=, isin=, lei=, type= and so on)."""
    out = {}
    for part in (evidence or '').split(';'):
        if '=' in part:
            k, v = part.split('=', 1)
            out.setdefault(k.strip(), v.strip())
    m = re.search(r'type=([^;]*)', evidence or '')
    out['openfigi_type'] = m.group(1).strip() if m else ''
    return out


def load_overrides(path: Path) -> list[dict]:
    """config/db_overrides_v1.csv. Columns: override_kind, key, value, field, reason, evidence (see docs)."""
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
