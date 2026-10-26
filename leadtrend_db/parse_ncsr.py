"""Vanguard 500 Index Fund holdings from pre-NPORT htm filings (N-Q, N-CSR, N-CSRS, NPORT-EX).

Reuses the HTML table reader of leadtrend_v05.p3_external (_Tables). The fund's section is found by its name followed
by the 'Common Stocks (x.x%)' header, which covers the layouts used across 2014-2019: 'Vanguard 500 Index Fund
Schedule of Investments' (2014-2018) and the running 'NNN 500 Index Fund Market Value' heads of the 2019 annual
report. The section ends at its 'Total Investments' line.

Row rules are stricter than p3_external.parse_500_index_schedule, which reads cells by fixed position and so drops
rows whose footnote mark sits in its own cell (2015 Q1 sample: 486 rows, 2.3bn of stock value missing). Here the
name is the first cell with letters, an amount is a cell of at least three digits (footnote marks are shorter), and
each row is tagged with its section, so non-stock rows are counted rather than silently dropped.
"""
from __future__ import annotations

import re

from leadtrend_v05.p3_external import _Tables

GAP = r'(?:\s|&nbsp;|&#160;|&#xa0;|\xa0|<[^>]*>)'
START = re.compile(r'500' + GAP + r'*Index' + GAP + r'*Fund' + r'[\s\S]{0,3000}?' + r'Common' + GAP + r'*Stocks'
                   + GAP + r'*\(\d{1,3}\.\d%\)', re.I)
# 2019 annual report: the holdings pages carry a 'NNN 500 Index Fund Market Value' head and no 'Common Stocks' label
START_PAGE = re.compile(r'500' + GAP + r'*Index' + GAP + r'*Fund' + GAP + r'*Market' + GAP + r'*Value', re.I)
END = re.compile(r'Total' + GAP + r'*Investments', re.I)
NUMERIC = re.compile(r'^[0-9][0-9,]*$')
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


def _amounts(cells: list[str]) -> list[int]:
    """Amounts: numeric cells with at least three digits. Shorter ones are footnote marks. Filings converted from
    PDF split digits with stray spaces ('16,922, 7 32') and write '$' as an entity, so both are removed first."""
    out = []
    for c in cells:
        pair = TWO_AMOUNTS.match(c)                 # shares and value in one cell: '88,905,773 16,457,348'
        if pair:
            out += [int(pair.group(1).replace(',', '')), int(pair.group(2).replace(',', ''))]
            continue
        digits = re.sub(r'[\s$]', '', c)
        if NUMERIC.match(digits) and len(digits.replace(',', '')) >= 3:
            out.append(int(digits.replace(',', '')))
    return out


def _label(cells: list[str]) -> tuple[str, list[str]]:
    """First cell with letters (footnote marks before it are dropped) and the cells after it."""
    for k, c in enumerate(cells):
        if re.search(r'[A-Za-z]', c) and not FOOTNOTE_CELL.match(c):
            name = re.sub(r'\s+\d$', '', LEAD_MARKS.sub('', c).strip())
            return name, cells[k + 1:]
    return '', []


def parse_500_schedule(raw: bytes) -> dict:
    """Parse the 500 Index Fund holdings. Values are returned in USD (the filings report $000)."""
    text = raw.decode('utf-8', errors='replace')
    starts = [m for m in (START.search(text), START_PAGE.search(text)) if m is not None]
    start = min(starts, key=lambda m: m.start()) if starts else None
    if start is None:
        return dict(found=False, stock_rows=[], excluded=[], total_common_stocks_usd=None,
                    total_investments_usd=None, notes=['500 Index Fund holdings section not found'])
    end = END.search(text, start.end())
    if end is None:
        return dict(found=True, stock_rows=[], excluded=[], total_common_stocks_usd=None,
                    total_investments_usd=None, notes=['no Total Investments line after the section start'])
    closes = list(re.finditer(r'</tr\s*>', text[end.end():end.end() + 20000], re.I))
    stop = end.end() + closes[min(2, len(closes) - 1)].end() if closes else end.end() + 400
    parser = _Tables()
    parser.feed(text[start.start():stop])   # the label row and the rows holding its amounts (up to two more)
    section = 'common'   # the holdings start with common stocks; a 'Common Stocks' label, if any, confirms it
    stock_rows, excluded, notes = [], [], []
    total_common = total_investments = None
    pending = None   # a total label whose amount sits in the next table row (2019 layout)
    carry = None     # first half of a company name split across table rows
    for row in parser.rows:
        cells = [c.strip() for c in row if c.strip() not in ('', '\xa0')]
        name, tail = _label(cells)
        share_in_name = re.search(r'\s([0-9][0-9,]{5,})$', name)   # shares printed inside the name cell
        if share_in_name and name[:share_in_name.start()].strip():
            tail = [share_in_name.group(1)] + tail
            name = name[:share_in_name.start()].strip()
        if not name or name.startswith('('):   # '(Cost $...)' fragments belong to the row above
            nums = _amounts(cells)
            if pending and nums:     # only the row right after the label may carry its amount
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
        if carry and numbers and section == 'common':   # a company name split over two table rows
            name, carry = f'{carry} {name}', None
        else:
            carry = None
        if not numbers and section == 'common' and '%' not in name and not lead.startswith(('total', 'common')) \
                and lead not in PAGE_HEAD_WORDS:
            carry = name                                  # first half of a split name; the amounts follow
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
            cls = CLASS_TEXT.search(name)
            stock_rows.append(dict(name=name, raw_class_text=cls.group(1) if cls else '',
                                   shares=numbers[0], value_usd=numbers[1] * 1000))
        elif section == 'common':
            notes.append(f'stock row with one amount (not used): {name}')
        elif section != 'total':
            excluded.append(dict(name=name, category=section or 'unlabeled', value_usd=numbers[-1] * 1000))
    return dict(found=True, stock_rows=stock_rows, excluded=excluded, total_common_stocks_usd=total_common,
                total_investments_usd=total_investments, notes=notes)
