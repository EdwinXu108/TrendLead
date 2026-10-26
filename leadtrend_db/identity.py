"""Identity helpers: names, share classes, security types, CIK lookups and the manual override file.

Name normalisation for pre-NPORT holdings reuses leadtrend_v05.p3_external.norm_name. Share-class text is removed
before normalising, so 'Alphabet Inc. Class A' and 'Alphabet Inc. Class C' form one name cluster with two classes.
"""
from __future__ import annotations

import csv
import difflib
import re
from pathlib import Path

from leadtrend_v05.p3_external import norm_name

CLASS_LETTER = re.compile(r'\bClass\s+([A-Z])\b')
REIT_TEXT = re.compile(r'\bREIT\b|REAL ESTATE INVESTMENT TRUST', re.I)
PARTNERSHIP_TEXT = re.compile(r'\bL\.?P\.?(?![A-Za-z])|LIMITED PARTNERSHIP|\bCOMMON UNITS?\b|\bPARTNERSHIP UNITS?\b|'
                              r'\bUNITS REPRESENTING\b|\bLP UNITS?\b', re.I)
ADR_TEXT = re.compile(r'\bADRS?\b|\bADSS?\b|AMERICAN DEPOSITARY', re.I)
PREFERRED_TEXT = re.compile(r'PREFERRED|\bPFD\b', re.I)
OVERRIDE_KINDS = ('cusip_cik', 'name_cik', 'link', 'status')


def issuer_id(cik: str | int) -> str:
    return f'CIK:{int(cik):010d}'


def cik10(cik: str | int) -> str:
    return f'{int(cik):010d}'


def cusip_security(cusip: str) -> str:
    return f'CUSIP:{cusip}'


def cikcls_security(cik: str, class_code: str) -> str:
    return f'CIKCLS:{cik10(cik)}:{class_code}'


def class_code_from_text(text: str) -> str:
    m = CLASS_LETTER.search(text or '')
    return f'CL_{m.group(1)}' if m else 'COM'


def core_name(name: str) -> str:
    """Name with share-class text removed (used for matching and clustering)."""
    return CLASS_LETTER.sub(' ', name or '')


def name_key(text: str) -> str:
    """Normalised name for matching: apostrophes are dropped (McDonald's -> mcdonalds), then norm_name."""
    return norm_name((text or '').replace("'", '').replace('’', ''))


def norm_core(name: str) -> str:
    return name_key(core_name(name))


def name_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_core(a), norm_core(b)).ratio()


def security_type(name: str, title: str = '', sic: str = '') -> str:
    text = f'{name} {title}'
    if sic == '6798' or REIT_TEXT.search(text):
        return 'reit'
    if PARTNERSHIP_TEXT.search(text):
        return 'partnership'
    if ADR_TEXT.search(text):
        return 'adr'
    if PREFERRED_TEXT.search(text):
        return 'preferred'
    return 'common'


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
    """company_tickers.json as ticker -> {cik, title}. Tickers are upper case, class dashes kept as in SEC."""
    out = {}
    for key in sorted(obj, key=lambda k: int(k)):
        row = obj[key]
        out[str(row['ticker']).upper()] = dict(cik=cik10(row['cik_str']), title=str(row.get('title', '')))
    return out


def ticker_key(ticker: str) -> str:
    return (ticker or '').strip().upper().replace('.', '-')


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
