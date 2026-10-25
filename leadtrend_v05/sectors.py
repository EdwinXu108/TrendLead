"""Large-sector keys for the structured selection rules and the sector-based signals.

The mapping is the frozen config/sector_map_v1.csv. An industry with no mapping, or one mapped to UNKNOWN, keeps its own
label as its sector: an unclassified name is never merged with other unclassified names.
"""
import csv
from pathlib import Path

SECTOR_MAP = 'config/sector_map_v1.csv'


def load_sector_map(root):
    lines = (Path(root) / SECTOR_MAP).read_text(encoding='utf-8').splitlines()
    rows = csv.DictReader(line for line in lines if not line.startswith('#'))
    return {r['industry_value']: r['sector'] for r in rows}


def sector_key(industry, mapping):
    """(sector, known): the sector of an industry, and whether it is a mapped sector rather than the own label."""
    sector = mapping.get(industry)
    if sector is None or sector == 'UNKNOWN':
        return industry, False
    return sector, True
