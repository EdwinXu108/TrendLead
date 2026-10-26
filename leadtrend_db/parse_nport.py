"""N-PORT XML for the Vanguard 500 Index Fund (2019-09-30 onward).

holdings_lines() keeps every equity line (assetCat EC), including lines whose CUSIP is blank or a placeholder such as
'N/A'; the v05 parser (leadtrend_v05.sec_nport.parse_nport_xml) drops blank-CUSIP lines, which would lose holdings.
Non-equity lines are counted and their value summed, so the total of all investments is still available.
screen() reads the series and class a filing reports, to choose the 500 Index Fund filing of each report date.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from leadtrend_v05.sec_nport import NS, TARGET_CLASS_ID, TARGET_SERIES_ID


def _local(tag: str) -> str:
    return tag.split('}')[-1]


def _child_text(el: ET.Element, name: str) -> str:
    for child in el:
        if _local(child.tag) == name:
            return (child.text or '').strip()
    return ''


def _identifiers(el: ET.Element) -> tuple[str, str, str]:
    """(cusip, isin, lei) from the invstOrSec element: the identifiers child holds value attributes."""
    cusip = isin = ''
    for child in el.iter():
        name = _local(child.tag)
        if name == 'cusip' and child.text and not cusip:
            cusip = child.text.strip()
        if name == 'isin' and child.get('value') and not isin:
            isin = child.get('value').strip()
    lei = _child_text(el, 'lei')
    return cusip, isin, lei


def _float(text: str) -> float:
    try:
        return float(text) if text else 0.0
    except ValueError:
        return 0.0


def holdings_lines(raw: bytes) -> dict:
    """Equity lines (EC) with every identifier as reported, plus the non-equity totals.

    Returns dict(lines=[...], non_equity_rows, non_equity_value_usd, total_investments_usd, report_date, series_id,
    class_ids). Each line: name, title, cusip (as reported), isin, lei, balance (shares), units, val_usd, pct_val,
    asset_cat, issuer_cat, line_no (position in the filing)."""
    root = ET.fromstring(raw)
    series_id = report_date = ''
    class_ids = []
    for el in root.iter():
        name = _local(el.tag)
        if name == 'seriesId' and not series_id:
            series_id = (el.text or '').strip()
        elif name == 'repPdDate':
            # repPdEnd is the fiscal year end and often precedes the actual quarter date in the XML.
            report_date = (el.text or '').strip()
        elif name == 'repPdEnd' and not report_date:
            report_date = (el.text or '').strip()
        elif name == 'classId' and el.text:
            class_ids.append(el.text.strip())
    lines, non_equity, non_equity_value, total = [], 0, 0.0, 0.0
    for n, inv in enumerate((el for el in root.iter() if _local(el.tag) == 'invstOrSec'), 1):
        val = _float(_child_text(inv, 'valUSD'))
        total += val
        asset_cat = _child_text(inv, 'assetCat')
        if asset_cat != 'EC':
            non_equity += 1
            non_equity_value += val
            continue
        cusip, isin, lei = _identifiers(inv)
        lines.append(dict(name=_child_text(inv, 'name'), title=_child_text(inv, 'title'), cusip=cusip, isin=isin,
                          lei=lei, balance=_float(_child_text(inv, 'balance')), units=_child_text(inv, 'units'),
                          val_usd=val, pct_val=_float(_child_text(inv, 'pctVal')), asset_cat=asset_cat,
                          issuer_cat=_child_text(inv, 'issuerCat'), line_no=n))
    return dict(lines=lines, non_equity_rows=non_equity, non_equity_value_usd=non_equity_value,
                total_investments_usd=total, report_date=report_date, series_id=series_id, class_ids=class_ids)


def screen(raw: bytes) -> dict:
    """Series, classes, report date and total of all investments of one filing."""
    info = holdings_lines(raw)
    return dict(series_id=info['series_id'], report_date=info['report_date'], class_ids=info['class_ids'],
                total_investments_usd=info['total_investments_usd'],
                is_target=info['series_id'] == TARGET_SERIES_ID and TARGET_CLASS_ID in info['class_ids'])


__all__ = ['holdings_lines', 'screen', 'NS']
