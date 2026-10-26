-- leadtrend_db v1, schema_version 1 (repair round: see docs/LeadTrend_数据库_v1.md, section 13).
-- Conventions: dates ISO 8601 (YYYY-MM-DD); timestamps UTC ISO 8601 with trailing Z; '' = unknown text;
-- NULL = no direct single source or not applicable. Foreign keys are enforced at build time (PRAGMA foreign_keys=ON)
-- and must have zero violations (checks.py). Every fact table carries source_id pointing at the raw file it came from.

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE sources (
  source_id        TEXT PRIMARY KEY,                -- 'SRC-' + first 16 hex digits of sha256
  kind             TEXT NOT NULL CHECK (kind IN ('nport','nq','ncsr','ncsrs','submissions','cik_lookup','company_tickers','filing_index','other')),
  url              TEXT NOT NULL,
  raw_path         TEXT NOT NULL,
  sha256           TEXT NOT NULL,
  bytes            INTEGER NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  note             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE issuers (
  issuer_id            TEXT PRIMARY KEY,           -- 'CIK:' + 10-digit CIK
  cik                  TEXT NOT NULL,
  name                 TEXT NOT NULL DEFAULT '',   -- SEC current name (submissions); '' if the submissions file is missing
  sic                  TEXT NOT NULL DEFAULT '',
  sic_description      TEXT NOT NULL DEFAULT '',
  state_of_incorporation TEXT NOT NULL DEFAULT '',
  lei                  TEXT NOT NULL DEFAULT '',   -- from the N-PORT line of the issuer's security; '' if none reported
  source_id            TEXT REFERENCES sources(source_id)
);

CREATE TABLE issuer_names (
  issuer_id  TEXT NOT NULL REFERENCES issuers(issuer_id),
  name       TEXT NOT NULL,
  name_kind  TEXT NOT NULL CHECK (name_kind IN ('sec_current','sec_former','holdings_observed')),
  valid_from TEXT NOT NULL DEFAULT '',               -- SEC formerNames interval; '' if unknown
  valid_to   TEXT NOT NULL DEFAULT '',
  first_seen TEXT NOT NULL DEFAULT '',               -- holdings: first and last report date the name appeared
  last_seen  TEXT NOT NULL DEFAULT '',
  source_id  TEXT REFERENCES sources(source_id),
  PRIMARY KEY (issuer_id, name, name_kind, valid_from)
);

-- For spinoff_from the predecessor is the parent and the successor is the new child issuer.
CREATE TABLE issuer_links (
  predecessor_issuer_id TEXT NOT NULL REFERENCES issuers(issuer_id),
  successor_issuer_id   TEXT NOT NULL REFERENCES issuers(issuer_id),
  relation              TEXT NOT NULL CHECK (relation IN ('successor_reorganization','rename','merger_survivor','spinoff_from')),
  confidence            TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
  via_override          INTEGER NOT NULL DEFAULT 0,
  effective_date        TEXT NOT NULL DEFAULT '',
  evidence              TEXT NOT NULL DEFAULT '',
  source_id             TEXT REFERENCES sources(source_id),
  PRIMARY KEY (predecessor_issuer_id, successor_issuer_id, relation)
);

CREATE TABLE securities (
  security_id   TEXT PRIMARY KEY,                  -- 'CUSIP:<cusip>', 'ISIN:<isin>' (no CUSIP anywhere) or 'CIKCLS:<CIK>:<class>'
  issuer_id     TEXT NOT NULL REFERENCES issuers(issuer_id),
  class_code    TEXT NOT NULL,                     -- COM, CL_A, CL_B, CL_C, ...
  security_type TEXT NOT NULL CHECK (security_type IN ('common','adr','reit','partnership','preferred','right','warrant','cvr','etf','other','unknown')),
  type_basis    TEXT NOT NULL DEFAULT '',          -- the evidence used for security_type
  name          TEXT NOT NULL DEFAULT '',
  id_scheme     TEXT NOT NULL CHECK (id_scheme IN ('CUSIP','ISIN','CIKCLS')),
  notes         TEXT NOT NULL DEFAULT '',
  source_id     TEXT REFERENCES sources(source_id)
);

CREATE TABLE security_identifiers (
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  id_type     TEXT NOT NULL CHECK (id_type IN ('ticker','cusip','isin','figi','futu_code','lei')),
  id_value    TEXT NOT NULL CHECK (length(id_value) > 0),   -- empty values are never stored
  valid_from  TEXT NOT NULL DEFAULT '',             -- '' = unknown
  valid_to    TEXT NOT NULL DEFAULT '',             -- '' = unknown or still valid
  source_id   TEXT REFERENCES sources(source_id),
  PRIMARY KEY (security_id, id_type, id_value, valid_from)
);

CREATE TABLE snapshots (
  snapshot_id                    TEXT PRIMARY KEY,
  fund_series                    TEXT NOT NULL,
  form                           TEXT NOT NULL CHECK (form IN ('NPORT-P','NPORT-EX','N-Q','N-CSR','N-CSRS')),
  accession                      TEXT NOT NULL,
  primary_doc                    TEXT NOT NULL,
  holdings_as_of                 TEXT NOT NULL,
  filed_date                     TEXT NOT NULL,
  available_at_utc               TEXT NOT NULL,
  is_primary                     INTEGER NOT NULL,  -- 1 = the snapshot used for membership of that quarter
  completeness                   TEXT NOT NULL CHECK (completeness IN ('full','partial','summary')),
  row_count                      INTEGER NOT NULL,  -- stock lines (all equity lines in the stock section)
  excluded_rows                  INTEGER NOT NULL,
  excluded_value_usd             REAL NOT NULL,
  total_investments_reported_usd REAL,
  reported_common_stock_total_usd REAL,             -- the filing's own Total Common Stocks (N-CSR/N-Q); NULL for N-PORT
  stock_total_usd                REAL NOT NULL,     -- sum of stock lines used as the weight denominator
  coverage_ratio                 REAL,              -- stock_total_usd / total investments reported
  reconciled                     INTEGER NOT NULL,  -- 1 when stock lines + negative lines equal the reported total (pre-2019)
  start_rule                     TEXT NOT NULL DEFAULT '',
  source_id                      TEXT REFERENCES sources(source_id),
  notes                          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE holdings (
  snapshot_id     TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  line_no         INTEGER NOT NULL,
  line_kind       TEXT NOT NULL CHECK (line_kind IN ('stock','negative')),
  raw_name        TEXT NOT NULL,
  raw_class_text  TEXT NOT NULL DEFAULT '',
  cusip_reported  TEXT NOT NULL DEFAULT '',         -- as reported ('' or 'N/A' included); never used as a key by itself
  isin_reported   TEXT NOT NULL DEFAULT '',
  lei_reported    TEXT NOT NULL DEFAULT '',
  title_reported  TEXT NOT NULL DEFAULT '',
  shares          REAL,
  value_usd       REAL,
  pct_val_percent REAL,                             -- N-PORT pctVal as reported, in PERCENT of net assets (0-100); NULL for htm
  weight          REAL,                             -- value_usd / stock_total_usd of the snapshot; NULL if excluded type
  security_id     TEXT,                             -- NULL when unmapped
  issuer_id       TEXT,                             -- NULL when unmapped
  map_method      TEXT NOT NULL DEFAULT '',
  map_confidence  TEXT NOT NULL DEFAULT '',
  unmapped_reason TEXT NOT NULL DEFAULT '',
  source_id       TEXT REFERENCES sources(source_id),
  PRIMARY KEY (snapshot_id, line_no)
);

-- Derived. A missing quarter (no complete snapshot) ends a span; a span restarts when a subject returns.
CREATE TABLE membership_spans (
  level                   TEXT NOT NULL CHECK (level IN ('issuer','security')),
  subject_id              TEXT NOT NULL,            -- issuer_id or security_id
  issuer_id               TEXT NOT NULL REFERENCES issuers(issuer_id),
  span_no                 INTEGER NOT NULL,
  first_as_of             TEXT NOT NULL,
  last_as_of              TEXT NOT NULL,
  first_available_at_utc  TEXT NOT NULL,
  last_available_at_utc   TEXT NOT NULL,
  quarters_present        INTEGER NOT NULL,
  next_absent_as_of       TEXT NOT NULL DEFAULT '', -- '' = still present at the last snapshot, or the span ends at a gap
  PRIMARY KEY (level, subject_id, span_no)
);

CREATE TABLE issuer_resolution (
  issuer_id          TEXT PRIMARY KEY REFERENCES issuers(issuer_id),
  resolved_issuer_id TEXT NOT NULL REFERENCES issuers(issuer_id),  -- successor if continued, else itself
  method             TEXT NOT NULL
);

CREATE TABLE listing_status (
  issuer_id      TEXT PRIMARY KEY REFERENCES issuers(issuer_id),
  status         TEXT NOT NULL CHECK (status IN ('in_index','listed_out_of_index','delisted','continued_by_successor','unknown')),
  status_as_of   TEXT NOT NULL,
  delisting_date TEXT NOT NULL DEFAULT '',
  reason         TEXT NOT NULL DEFAULT 'unknown',
  counterparty   TEXT NOT NULL DEFAULT '',          -- successor issuer_id when continued
  evidence       TEXT NOT NULL DEFAULT '',
  method         TEXT NOT NULL DEFAULT '',
  needs_review   INTEGER NOT NULL DEFAULT 0,
  source_id      TEXT REFERENCES sources(source_id)
);

-- Structure only in v1 (not filled).
CREATE TABLE index_changes (
  change_id   INTEGER PRIMARY KEY,
  change_date TEXT NOT NULL,
  change_type TEXT NOT NULL CHECK (change_type IN ('add','remove')),
  issuer_id   TEXT REFERENCES issuers(issuer_id),
  security_id TEXT REFERENCES securities(security_id),
  snapshot_id TEXT REFERENCES snapshots(snapshot_id),
  source_id   TEXT REFERENCES sources(source_id),
  notes       TEXT NOT NULL DEFAULT ''
);

-- Reserved for price data (not filled in v1). One row per security, session and price source.
CREATE TABLE prices_daily (
  security_id  TEXT NOT NULL REFERENCES securities(security_id),
  session_date TEXT NOT NULL,
  source       TEXT NOT NULL,
  open         REAL,
  high         REAL,
  low          REAL,
  close        REAL,
  adj_close    REAL,
  volume       REAL,
  source_id    TEXT REFERENCES sources(source_id),
  PRIMARY KEY (security_id, session_date, source)
);

-- Reserved for corporate actions (not filled in v1).
CREATE TABLE corporate_actions (
  action_id   INTEGER PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  action_date TEXT NOT NULL,
  action_type TEXT NOT NULL,
  ratio       REAL,
  cash_amount REAL,
  currency    TEXT NOT NULL DEFAULT 'USD',
  source      TEXT NOT NULL,
  source_id   TEXT REFERENCES sources(source_id),
  notes       TEXT NOT NULL DEFAULT ''
);

-- Reserved: coverage of prices per security and price source (not filled in v1).
CREATE TABLE price_coverage (
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  source      TEXT NOT NULL,
  first_date  TEXT NOT NULL DEFAULT '',
  last_date   TEXT NOT NULL DEFAULT '',
  n_days      INTEGER NOT NULL DEFAULT 0,
  status      TEXT NOT NULL DEFAULT 'not_fetched',
  source_id   TEXT REFERENCES sources(source_id),
  PRIMARY KEY (security_id, source)
);

CREATE TABLE review_items (
  review_id   INTEGER PRIMARY KEY,
  review_type TEXT NOT NULL,
  subject_id  TEXT NOT NULL DEFAULT '',
  snapshot_id TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT '',
  source_id   TEXT REFERENCES sources(source_id)
);

CREATE TABLE build_log (
  step_no INTEGER PRIMARY KEY,
  ts_utc  TEXT NOT NULL,
  step    TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT ''
);

-- Universe: security spans of issuers that were in the fund and are not confirmed delisted. An issuer continued by a
-- successor is shown under the successor, with its predecessor spans kept (spans_issuer_id).
CREATE VIEW universe_v1 AS
SELECT u.*,
       CASE WHEN u.security_type NOT IN ('common','adr') OR u.security_type IS NULL THEN 0
            WHEN u.ticker = 'BRK.A' THEN 0
            ELSE 1 END AS eligible_v1
FROM (
  SELECT res.resolved_issuer_id AS issuer_id,
         sp.issuer_id          AS spans_issuer_id,
         sp.subject_id        AS security_id,
         s.class_code,
         s.security_type,
         -- own ticker when the security still belongs to the resolved issuer; otherwise the current ticker of the
         -- successor's security of the same class (never the predecessor's old code)
         COALESCE(
           CASE WHEN sp.issuer_id = res.resolved_issuer_id THEN
             (SELECT i.id_value FROM security_identifiers i
               WHERE i.security_id = sp.subject_id AND i.id_type = 'ticker'
               ORDER BY i.valid_from DESC, i.id_value LIMIT 1) END,
           (SELECT i.id_value FROM security_identifiers i JOIN securities s2 ON s2.security_id = i.security_id
             WHERE s2.issuer_id = res.resolved_issuer_id AND s2.class_code = s.class_code AND i.id_type = 'ticker'
             ORDER BY i.id_value LIMIT 1)) AS ticker,
         (SELECT i.id_value FROM security_identifiers i
           WHERE i.security_id = sp.subject_id AND i.id_type = 'futu_code' LIMIT 1) AS futu_code,
         ls.status             AS issuer_status,
         ls.needs_review       AS issuer_needs_review,
         sp.span_no,
         sp.first_as_of,
         sp.last_as_of,
         sp.first_available_at_utc,
         sp.next_absent_as_of
  FROM membership_spans sp
  JOIN securities s ON s.security_id = sp.subject_id
  JOIN issuer_resolution res ON res.issuer_id = sp.issuer_id
  JOIN listing_status ls ON ls.issuer_id = res.resolved_issuer_id
  WHERE sp.level = 'security' AND ls.status <> 'delisted'
) u;
