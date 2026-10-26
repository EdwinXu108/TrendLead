"""leadtrend_db v1: long-lived VOO constituent database (membership, identity, listing status).

Raw inputs live in data/db/raw/ (append-only, indexed by manifest.jsonl). The build reads only that archive plus
config/db_overrides_v1.csv, so a rebuild from the archive is deterministic. See docs/LeadTrend_数据库_v1.md.
"""

SCHEMA_VERSION = 1
