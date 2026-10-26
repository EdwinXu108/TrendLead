"""Vanguard 500 Index Fund holdings from pre-NPORT htm filings (N-Q, N-CSR, N-CSRS, NPORT-EX).

The HTML table reader is leadtrend_v05.p3_external._Tables. The fund's holdings are located by several page-head
layouts used in 2014-2019 (see START_RULES). For each candidate start the schedule is parsed and reconciled with the
reported 'Total Common Stocks' amount of the same filing; the first candidate that reconciles is used. Nothing is
hard-coded to a row count.

Row rules: the name is the first cell with letters; amounts are numeric cells of at least three digits (shorter
cells are footnote marks); an amount in parentheses is negative and is kept apart (negative_rows), because it is
part of the reported total but is not a holding; a name split over two table rows is joined to the row holding its
amounts; a shares figure printed inside the name cell is split off; running-head fragments never join a name.
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
RECONCILE_TOL = 0.001


def _amounts(cells: list[str]) -> list[int]:
    """Signed amounts in cells, in order. Cells of fewer than three digits are footnote marks. Filings converted from
    PDF split digits with stray spaces ('16,922, 7 32') and write '$' as an entity; both are normalised first."""
    out = []
    for c in cells:
        pair = TWO_AMOUNTS.match(c)                       # shares and value in one cell: '88,905,773 16,457,348'
        if pair:
            out += [int(pair.group(1).replace(',', '')), int(pair.group(2).replace(',', ''))]
            continue
        digits = re.sub(r'[\s$]', '', c)
        if NEGATIVE.match(digits) and len(digits.strip('()').replace(',', '')) >= 3:
            out.append(-int(digits.strip('()').replace(',', '')))
        elif NUMERIC.match(digits) and len(digits.replace(',', '')) >= 3:
            out.append(int(digits.replace(',', '')))
    return out


def _label(cells: list[str]) -> tuple[str, list[str]]:
    """First cell with letters (footnote marks before it are dropped) and the cells after it."""
    for k, c in enumerate(cells):
        if re.search(r'[A-Za-z]', c) and not FOOTNOTE_CELL.match(c):
            name = re.sub(r'\s+\d$', '', LEAD_MARKS.sub('', c).strip())
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
        return dict(stock_rows=[], negative_rows=[], excluded=[], total_common_stocks_usd=None,
                    total_investments_usd=None, notes=['no Total Investments line after the section start'])
    closes = list(re.finditer(r'</tr\s*>', text[end.end():end.end() + 20000], re.I))
    stop = end.end() + closes[min(2, len(closes) - 1)].end() if closes else end.end() + 400
    parser = _Tables()
    parser.feed(text[start:stop])   # the Total Investments row and up to two rows holding its amounts
    section = 'common'              # holdings start with common stocks; a 'Common Stocks' label confirms it
    stock_rows, negative_rows, excluded, notes = [], [], [], []
    total_common = total_investments = None
    pending = None                  # a total label whose amount sits in the next table row (2019 layout)
    carry = None                    # first half of a company name split across table rows
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
        pending = None
        lead = name.lower()
        numbers = _amounts(tail)
        if carry and numbers and section == 'common':
            name, carry = f'{carry} {name}', None
        else:
            carry = None
        if not numbers and section == 'common' and '%' not in name and not SUBTOTAL.match(lead) \
                and not lead.startswith('common stocks') and lead not in PAGE_HEAD_WORDS:
            carry = name                                   # first half of a split name; its amounts follow
            continue
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
                notes.append(f'stock row with {len(numbers)} amounts: {name}')
            shares, value = numbers[0], numbers[1]
            cls = CLASS_TEXT.search(name)
            if shares < 0 or value < 0:
                negative_rows.append(dict(name=name, raw_class_text=cls.group(1) if cls else '',
                                          shares=shares, value_usd=value * 1000))
            else:
                stock_rows.append(dict(name=name, raw_class_text=cls.group(1) if cls else '',
                                       shares=shares, value_usd=value * 1000))
        elif section == 'common':
            notes.append(f'stock row with one amount (not used): {name}')
        elif section != 'total':
            excluded.append(dict(name=name, category=section or 'unlabeled', value_usd=numbers[-1] * 1000))
    return dict(stock_rows=stock_rows, negative_rows=negative_rows, excluded=excluded,
                total_common_stocks_usd=total_common, total_investments_usd=total_investments, notes=notes)


def _reconciles(res: dict) -> bool:
    total = res['total_common_stocks_usd']
    if not total:
        return False
    value = sum(r['value_usd'] for r in res['stock_rows']) + sum(r['value_usd'] for r in res['negative_rows'])
    return abs(value - total) <= RECONCILE_TOL * total


def parse_500_schedule(raw: bytes) -> dict:
    """Parse the 500 Index Fund holdings of one filing. Values are USD (the filings report $000).

    Each candidate start is parsed; the first that reconciles to the filing's own Total Common Stocks is used.
    If none reconciles, the candidate with the most stock rows is returned and marked as not reconciled."""
    text = raw.decode('utf-8', errors='replace')
    starts = _candidate_starts(text)
    if not starts:
        return dict(found=False, stock_rows=[], negative_rows=[], excluded=[], total_common_stocks_usd=None,
                    total_investments_usd=None, notes=['500 Index Fund holdings section not found'], rule=None,
                    reconciled=False)
    results = []
    for rule, pos in starts:
        res = _run(text, pos)
        res.update(found=True, rule=rule, start=pos, reconciled=_reconciles(res))
        results.append(res)
    chosen = next((r for r in results if r['reconciled']), None)
    if chosen is None:
        chosen = max(results, key=lambda r: len(r['stock_rows']))
    return chosen
