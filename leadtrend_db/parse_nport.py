"""N-PORT XML for the Vanguard 500 Index Fund (2019-09-30 onward).

The holdings parser is leadtrend_v05.sec_nport.parse_nport_xml (equity rows with a CUSIP, USD values, pctVal).
This module adds the screening of a filing (which series and class it reports) and the total of all investments,
which the v05 parser does not keep.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from leadtrend_v05.sec_nport import NS, TARGET_CLASS_ID, TARGET_SERIES_ID, parse_nport_xml


def _text(root: ET.Element, tag: str) -> str:
    el = root.find(f'.//{{{NS["nport"]}}}{tag}')
    if el is None:
        el = root.find(f'.//{tag}')
    return (el.text or '').strip() if el is not None else ''


def screen(raw: bytes) -> dict:
    """Series, classes, report date and total of all investments, read without interpreting the holdings."""
    root = ET.fromstring(raw)
    class_ids = [(el.text or '').strip() for el in root.iter() if el.tag.split('}')[-1] == 'classId']
    total = 0.0
    for el in root.iter():
        if el.tag.split('}')[-1] != 'invstOrSec':
            continue
        for child in el:
            if child.tag.split('}')[-1] == 'valUSD' and child.text:
                total += float(child.text)
    return dict(series_id=_text(root, 'seriesId'), series_name=_text(root, 'seriesName'),
                report_date=_text(root, 'repPdDate') or _text(root, 'repPdEnd'), class_ids=class_ids,
                total_investments_usd=total,
                is_target=_text(root, 'seriesId') == TARGET_SERIES_ID and TARGET_CLASS_ID in class_ids)


def parse_holdings(raw: bytes, meta: dict, local_path: str):
    """v05 parser: equity rows with a CUSIP. meta needs available_at_utc (ISO) and filing_date."""
    return parse_nport_xml(raw, meta, local_path)
