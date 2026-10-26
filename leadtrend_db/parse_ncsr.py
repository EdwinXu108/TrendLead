"""Vanguard 500 Index Fund holdings from pre-NPORT htm filings (N-Q, N-CSR, N-CSRS, NPORT-EX).

The HTML table reader is leadtrend_v05.p3_external._Tables. The fund's holdings are located by several page-head
layouts used in 2014-2019 (see START_RULES). For each candidate start the schedule is parsed and reconciled with the
reported 'Total Common Stocks' amount of the same filing; the first candidate that reconciles is used. Nothing is
hard-coded to a row count.

Row rules (round 2): the name is the first cell with letters. The amounts are the numeric cells after the name, read
in position: the last two numeric cells are shares and market value ($000). A one-digit or two-digit value is a real
amount when it sits in the value position (Halyard Health: 132 shares, value 6). A footnote mark is a symbol or a
cell that is not in the amount position. An amount in parentheses is negative and kept apart (negative_rows): it is
part of the reported total but is not a holding. A name split over several table rows is joined to the row holding
its amounts. A shares figure printed inside the name cell is split off.

Reconciliation is exact up to rounding: every row is a whole thousand dollars, so the sum of the rows may differ from
the reported total by at most half a thousand dollars per row. A row that has a name and numbers but is not parsed
(unparsed) makes the schedule unreconciled, whatever the total says.
"""
from __future__ import annotations

import re

from leadtrend_v05.p3_external import _Tables

GAP = r'(?:\s|&nbsp;|&#160;|&#xa0;|\xa0|<[^>]*>)'
# candidate starts of the fund's holdings, most specific first
START_RULES = {
    'common_stocks_header': re.compile(r'500' + GAP + r'*Index' + GAP + r'*Fund' + r'[\s\S]{0,3000}?' + r'Common'
                                       + GAP + r'*Stocks' + GAP + r'*\(\d{1,3}\.\d%\)', re.I),
    'market_value_head': re.compile(r'500' + GAP + r'*Index' + GAP + r'*Fund' + GAP + r'*Market' + GAP + r'*Value', re.I),
    'statement_of_net_assets_head': re.compile(r'500' + GAP + r'*Index' + GAP + r'*Fund' + GAP + r'*Financial' + GAP
                                               + r'*Statements' + GAP + r'*Statement' + GAP + r'*of' + GAP + r'*Net'
                                               + GAP + r'*Assets', re.I),
}
FUND_HEAD = re.compile(r'500' + GAP + r'*Index' + GAP + r'*Fund', re.I)
SECTOR_PAGE = re.compile(r'Sector' + GAP + r'*Diversification', re.I)
END = re.compile(r'Total' + GAP + r'*Investments', re.I)
NUMERIC = re.compile(r'^[0-9][0-9,]*$')
NEGATIVE = re.compile(r'^\([0-9][0-9,]*\)$')
TWO_AMOUNTS = re.compile(r'^\s*(\d{1,3}(?:,\d{3})+)\s+(\d{1,3}(?:,\d{3})+)\s*$')
FOOTNOTE_CELL = re.compile(r'^[0-9,\s*]+$')
NON_STOCK_SECTION = re.compile(
    r'^(temporary cash investments|money market|u\.s\. government|corporate bonds|bonds|preferred stocks|'
    r'rights|warrants|futures|investment companies)', re.I)
# subtotal labels only: a company called 'Total System Services' must not end the stock section
SUBTOTAL = re.compile(r'^total\s+(common|temporary|investments|money|u\.s\.|preferred|rights|warrants|futures|'
                      r'bonds|corporate|investment companies)', re.I)
CLASS_TEXT = re.compile(r'\b(Class\s+[A-Z])\b')
# running-head fragments that must never be joined onto a company name
PAGE_HEAD_WORDS = {'shares', 'market', 'value', 'market value', 'shares ($000)', '($000)', 'value ($000)',
                   'market value ($000)', 'value•', 'market value •'}
LEAD_MARKS = re.compile(r'^[\s*^†‡§]+')
ROUNDING_USD = 500                                  # each row is whole $000: at most half a thousand off
MIN_RECONCILE_USD = 1000                            # slack for the reported total's own rounding


NIL_CELL = re.compile(r'^[—–\-]{1,2}$')     # an em or en dash in an amount column: nil (zero)


def _amount(text: str) -> int | None:
    """A cell in amount form: digits with optional commas, optional $ and parentheses for negative values. A dash
    in an amount column is a reported nil (zero); the row is still a holding."""
    digits = re.sub(r'[\s$]', '', text)
    if NIL_CELL.match(digits):
        return 0
    if NEGATIVE.match(digits):
        value = int(digits.strip('()').replace(',', ''))
        return -value if value else None
    if NUMERIC.match(digits):
        return int(digits.replace(',', ''))
    return None


def _amounts(cells: list[str]) -> list[int]:
    """Signed amounts in cells, in order. Filings converted from PDF split digits with stray spaces ('16,922, 7 32')
    and write '$' as an entity; a cell holding two amounts ('88,905,773 16,457,348') is split."""
    out = []
    for c in cells:
        pair = TWO_AMOUNTS.match(c)
        if pair:
            out += [int(pair.group(1).replace(',', '')), int(pair.group(2).replace(',', ''))]
            continue
        value = _amount(c)
        if value is not None:
            out.append(value)
    return out


def _label(cells: list[str]) -> tuple[str, list[str]]:
    """First cell with letters (footnote marks before it are dropped) and the cells after it."""
    for k, c in enumerate(cells):
        if re.search(r'[A-Za-z]', c) and not FOOTNOTE_CELL.match(c):
            name = LEAD_MARKS.sub('', c).strip()
            return name, cells[k + 1:]
    return '', []


def _candidate_starts(text: str) -> list[tuple[str, int]]:
    """Possible positions where the fund's holdings begin, one per rule (earliest match of each)."""
    out = []
    for rule, pattern in START_RULES.items():
        m = pattern.search(text)
        if m:
            out.append((rule, m.start()))
    # a fund's holdings follow its 'Sector Diversification' page: the first fund head after the last such page
    anchor = None
    for m in SECTOR_PAGE.finditer(text):
        if FUND_HEAD.search(text, max(0, m.start() - 1500), m.start()):
            anchor = m.end()
    if anchor is not None:
        after = FUND_HEAD.search(text, anchor)
        if after:
            out.append(('after_sector_page', after.start()))
    return out


def _run(text: str, start: int) -> dict:
    end = END.search(text, start)
    if end is None:
        return dict(stock_rows=[], negative_rows=[], excluded=[], unparsed=[], total_common_stocks_usd=None,
                    total_investments_usd=None, notes=['no Total Investments line after the section start'])
    closes = list(re.finditer(r'</tr\s*>', text[end.end():end.end() + 20000], re.I))
    stop = end.end() + closes[min(2, len(closes) - 1)].end() if closes else end.end() + 400
    parser = _Tables()
    segment = text[start:stop]
    # Use actual HTML superscript structure for numeric notes. Unadorned digits can be part of a company name
    # (Phillips 66, or the first line 'Level 3' of a split name) and must never be removed by a name suffix regex.
    segment = re.sub(r'<sup\b[^>]*>[\s\S]*?</sup>',
                     lambda m: '' if re.fullmatch(r'[\d,\s]+', re.sub(r'<[^>]*>', '', m.group())) else m.group(),
                     segment, flags=re.I)
    parser.feed(segment)
    section = 'common'              # holdings start with common stocks; a 'Common Stocks' label confirms it
    stock_rows, negative_rows, excluded, unparsed, notes = [], [], [], [], []
    total_common = total_investments = None
    pending = None                  # a total label whose amount sits in the next table row (2019 layout)
    carry = None                    # name fragments of a company name split across table rows
    for row in parser.rows:
        cells = [c.strip() for c in row if c.strip() not in ('', '\xa0')]
        name, tail = _label(cells)
        share_in_name = re.search(r'\s([0-9][0-9,]{5,})$', name)   # shares printed inside the name cell
        if share_in_name and name[:share_in_name.start()].strip():
            tail = [share_in_name.group(1)] + tail
            name = name[:share_in_name.start()].strip()
        if not name or name.startswith('('):   # '(Cost $...)' fragments belong to the row above
            nums = _amounts(cells)
            if pending and nums:
                if pending == 'common':
                    total_common = nums[-1] * 1000
                else:
                    total_investments = nums[-1] * 1000
                    break
            pending = None
            carry = None
            continue
        numbers = _amounts(tail)
        lead = name.lower()
        if not numbers and section == 'common' and '%' not in name and not SUBTOTAL.match(lead) \
                and not lead.startswith('common stocks') and lead not in PAGE_HEAD_WORDS:
            carry = f'{carry} {name}' if carry else name   # a name part; its amounts follow on a later row
            continue
        if carry and section == 'common' and numbers:
            name = f'{carry} {name}'
            lead = name.lower()
        carry = None
        pending = None
        if lead.startswith('total investments'):
            if numbers:
                total_investments = numbers[-1] * 1000
                break
            pending = 'investments'
            continue
        if lead.startswith('total common stocks'):
            if numbers:
                total_common = numbers[-1] * 1000
            else:
                pending = 'common'
            section = 'total'
            continue
        if lead.startswith('common stocks'):
            section = 'common'
            continue
        if SUBTOTAL.match(lead):
            section = 'total'
            continue
        if NON_STOCK_SECTION.match(name) and len(numbers) < 2:
            section = name.split('(')[0].strip().lower()   # heading; a footnote number may follow it
            continue
        if not numbers:
            continue                                        # sector headings, column labels, page heads
        if section == 'common' and len(numbers) >= 2:
            if len(numbers) != 2:
                notes.append(f'stock row with {len(numbers)} amounts, last two used: {name}')
            shares, value = numbers[-2], numbers[-1]
            cls = CLASS_TEXT.search(name)
            if shares < 0 or value < 0:
                negative_rows.append(dict(name=name, raw_class_text=cls.group(1) if cls else '',
                                          shares=shares, value_usd=value * 1000))
            else:
                stock_rows.append(dict(name=name, raw_class_text=cls.group(1) if cls else '',
                                       shares=shares, value_usd=value * 1000))
        elif section == 'common':
            unparsed.append(name)
            notes.append(f'stock row with one amount (unparsed): {name}')
        elif section != 'total':
            excluded.append(dict(name=name, category=section or 'unlabeled', value_usd=numbers[-1] * 1000))
    return dict(stock_rows=stock_rows, negative_rows=negative_rows, excluded=excluded, unparsed=unparsed,
                total_common_stocks_usd=total_common, total_investments_usd=total_investments, notes=notes)


def reconciliation(res: dict) -> dict:
    """Residual of the parsed common-stock rows (positive and negative) against the filing's own total, and the
    rounding bound that the residual must stay within."""
    total = res['total_common_stocks_usd']
    rows = res['stock_rows'] + res['negative_rows']
    value = sum(r['value_usd'] for r in rows)
    bound = ROUNDING_USD * len(rows) + MIN_RECONCILE_USD
    residual = None if total is None else value - total
    return dict(parsed_sum_usd=value, residual_usd=residual, rounding_bound_usd=bound,
                unparsed_rows=len(res['unparsed']))


def _reconciles(res: dict) -> bool:
    rec = reconciliation(res)
    if rec['residual_usd'] is None or rec['unparsed_rows']:
        return False
    return abs(rec['residual_usd']) <= rec['rounding_bound_usd']


def parse_500_schedule(raw: bytes) -> dict:
    """Parse the 500 Index Fund holdings of one filing. Values are USD (the filings report $000).

    Each candidate start is parsed; the first that reconciles (see _reconciles) is used. If none reconciles, the
    candidate with the smallest residual is returned and marked as not reconciled."""
    text = raw.decode('utf-8', errors='replace')
    starts = _candidate_starts(text)
    if not starts:
        return dict(found=False, stock_rows=[], negative_rows=[], excluded=[], unparsed=[],
                    total_common_stocks_usd=None, total_investments_usd=None,
                    notes=['500 Index Fund holdings section not found'], rule=None, reconciled=False,
                    **reconciliation(dict(stock_rows=[], negative_rows=[], unparsed=[],
                                          total_common_stocks_usd=None)))
    results = []
    for rule, pos in starts:
        res = _run(text, pos)
        res.update(found=True, rule=rule, start=pos, reconciled=_reconciles(res))
        res.update(reconciliation(res))
        results.append(res)
    chosen = next((r for r in results if r['reconciled']), None)
    if chosen is None:
        with_total = [r for r in results if r['residual_usd'] is not None]
        chosen = min(with_total, key=lambda r: abs(r['residual_usd'])) if with_total else results[0]
    return chosen
