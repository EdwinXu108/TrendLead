-- schema_version 5: dated historical membership and a complete current-security projection.
-- Conventions: dates ISO 8601 (YYYY-MM-DD); timestamps UTC ISO 8601 with trailing Z; '' = unknown text;
-- NULL = no direct single source or not applicable. Foreign keys are enforced at build time (PRAGMA foreign_keys=ON)
-- and must have zero violations (checks.py). Every fact table carries source_id pointing at the raw file it came from.
-- Amounts: value_usd and cents columns are reported USD (htm filings report $000, stored as whole dollars).

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE sources (
  source_id        TEXT PRIMARY KEY,                -- 'SRC-' + first 16 hex digits of sha256
  kind             TEXT NOT NULL CHECK (kind IN ('nport','nq','ncsr','ncsrs','submissions','cik_lookup','company_tickers','filing_index','other','evidence','filing_document')),
  url              TEXT NOT NULL,
  raw_path         TEXT NOT NULL,
  sha256           TEXT NOT NULL,
  bytes            INTEGER NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  note             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE issuers (
  issuer_id            TEXT PRIMARY KEY,           -- 'CIK:' + 10-digit CIK
  cik                  TEXT NOT NULL,             -- empty only for verified non-SEC banks keyed by disclosed LEI
  name                 TEXT NOT NULL DEFAULT '',   -- SEC current name (submissions); '' if the submissions file is missing
  sic                  TEXT NOT NULL DEFAULT '',
  sic_description      TEXT NOT NULL DEFAULT '',
  state_of_incorporation TEXT NOT NULL DEFAULT '',
  lei                  TEXT NOT NULL DEFAULT '',
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

CREATE TABLE pool_exclusions (
  issuer_id TEXT PRIMARY KEY REFERENCES issuers(issuer_id),
  name TEXT NOT NULL, reason TEXT NOT NULL, decision_date TEXT NOT NULL, decider TEXT NOT NULL
);

CREATE TABLE ticker_checks (
  security_id TEXT PRIMARY KEY REFERENCES securities(security_id), current_ticker TEXT NOT NULL,
  sec_issuer_id TEXT NOT NULL, opend_code TEXT NOT NULL, opend_stock_id TEXT NOT NULL,
  sec_result TEXT NOT NULL, opend_result TEXT NOT NULL, checked_at_utc TEXT NOT NULL,
  sec_source_id TEXT REFERENCES sources(source_id), opend_source_id TEXT REFERENCES sources(source_id)
);

-- Successor links between issuers (the holdings keep the issuer of their own date; links are for the current view).
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

-- One row per security key. issuer_id is the issuer of its latest resolved line; the dated history is in
-- security_issuer_spans.
CREATE TABLE securities (
  security_id   TEXT PRIMARY KEY,                  -- 'CUSIP:<cusip>', 'ISIN:<isin>' or 'CIKCLS:<CIK>:<class>'
  issuer_id     TEXT NOT NULL REFERENCES issuers(issuer_id),
  class_code    TEXT NOT NULL,                     -- COM, CL_A, CL_B, ...
  security_type TEXT NOT NULL CHECK (security_type IN ('common','adr','reit','partnership','preferred','right','warrant','cvr','etf','spac','other','unknown')),
  type_basis    TEXT NOT NULL DEFAULT '',
  name          TEXT NOT NULL DEFAULT '',
  id_scheme     TEXT NOT NULL CHECK (id_scheme IN ('CUSIP','ISIN','CIKCLS','LEICLS')),
  notes         TEXT NOT NULL DEFAULT '',
  source_id     TEXT REFERENCES sources(source_id)
);

CREATE TABLE security_identifiers (
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  id_type     TEXT NOT NULL CHECK (id_type IN ('ticker','cusip','isin','figi','futu_code','lei')),
  id_value    TEXT NOT NULL CHECK (length(id_value) > 0),   -- empty values are never stored
  valid_from  TEXT NOT NULL DEFAULT '',             -- '' = unknown (no dated source for the code)
  valid_to    TEXT NOT NULL DEFAULT '',
  source_id   TEXT REFERENCES sources(source_id),
  PRIMARY KEY (security_id, id_type, id_value, valid_from)
);

-- Dated issuer of a security: the periods in which holdings of the security were resolved to that issuer.
CREATE TABLE security_issuer_spans (
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  issuer_id   TEXT NOT NULL REFERENCES issuers(issuer_id),
  span_no     INTEGER NOT NULL,
  first_as_of TEXT NOT NULL,
  last_as_of  TEXT NOT NULL,
  lines       INTEGER NOT NULL,
  method      TEXT NOT NULL DEFAULT '',
  source_id   TEXT REFERENCES sources(source_id),
  PRIMARY KEY (security_id, issuer_id, span_no)
);

CREATE TABLE snapshots (
  snapshot_id                    TEXT PRIMARY KEY,
  fund_series                    TEXT NOT NULL,
  form                           TEXT NOT NULL CHECK (form IN ('NPORT-P','NPORT-EX','N-Q','N-CSR','N-CSRS')),
  accession                      TEXT NOT NULL,
  primary_doc                    TEXT NOT NULL,
  holdings_as_of                 TEXT NOT NULL,
  filed_date                     TEXT NOT NULL,
  available_at_utc               TEXT NOT NULL,     -- SEC acceptance time of the filing
  is_primary                     INTEGER NOT NULL,  -- 1 = the canonical version of that report period
  completeness                   TEXT NOT NULL CHECK (completeness IN ('full','partial','summary')),
  row_count                      INTEGER NOT NULL,  -- stock lines (equity lines in the stock section, zero-value lines included)
  excluded_rows                  INTEGER NOT NULL,
  excluded_value_usd             REAL NOT NULL,
  total_investments_reported_usd REAL,
  reported_common_stock_total_usd REAL,             -- the filing's own Total Common Stocks (N-CSR/N-Q); NULL for N-PORT
  stock_total_usd                REAL NOT NULL,     -- positive stock lines that are not non-stock types (stock weight denominator)
  coverage_ratio                 REAL,              -- stock_total_usd / total investments reported
  reconciled                     INTEGER NOT NULL,  -- 1 when parsed stock and negative lines match the total within rounding
  residual_usd                   REAL,              -- parsed (stock + negative) minus the reported total, USD
  rounding_bound_usd             REAL,              -- the largest residual that rounding to whole $000 can explain
  unparsed_rows                  INTEGER NOT NULL DEFAULT 0,
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
  pct_val_percent REAL,                             -- N-PORT pctVal as reported: PERCENT of net assets (0-100); NULL for htm
  fund_weight_raw REAL,                             -- value_usd / total investments reported for the snapshot (fraction)
  stock_weight    REAL,                             -- value_usd / stock_total_usd of the snapshot; NULL for negative or non-stock lines
  security_id     TEXT,                             -- NULL when unmapped
  issuer_id       TEXT,                             -- issuer on the holding date; NULL when unmapped
  security_type   TEXT NOT NULL DEFAULT 'unknown',
  map_method      TEXT NOT NULL DEFAULT '',
  map_confidence  TEXT NOT NULL DEFAULT '',
  unmapped_reason TEXT NOT NULL DEFAULT '',
  candidate_issuers TEXT NOT NULL DEFAULT '',       -- possible CIKs of an unresolved line (space separated)
  source_id       TEXT REFERENCES sources(source_id),
  PRIMARY KEY (snapshot_id, line_no)
);

-- Versions of the quarterly pool: every complete, reconciled snapshot. available_at_utc is when it became public.
CREATE TABLE pool_versions (
  version_id           TEXT PRIMARY KEY REFERENCES snapshots(snapshot_id),
  report_period        TEXT NOT NULL,
  snapshot_id          TEXT NOT NULL REFERENCES snapshots(snapshot_id),
  form                 TEXT NOT NULL,
  accession            TEXT NOT NULL,
  filed_date           TEXT NOT NULL,
  available_at_utc     TEXT NOT NULL,
  completeness         TEXT NOT NULL,
  reconciled           INTEGER NOT NULL,
  is_canonical         INTEGER NOT NULL,            -- 1 = the version used for the historical list of that period
  supersedes_version_id TEXT REFERENCES pool_versions(version_id),
  version_note         TEXT NOT NULL DEFAULT '',
  rule_version         TEXT NOT NULL,
  quarter_state        TEXT NOT NULL CHECK (quarter_state IN ('ready','blocked','alternate_version')),
  quarter_reason       TEXT NOT NULL DEFAULT '',
  source_id            TEXT REFERENCES sources(source_id)
);

-- Company ranking of every version: all companies with an eligible line, by amount (cents), then issuer_id.
CREATE TABLE company_ranks (
  version_id           TEXT NOT NULL REFERENCES pool_versions(version_id),
  rank                 INTEGER NOT NULL,
  issuer_id            TEXT NOT NULL REFERENCES issuers(issuer_id),
  amount_cents         INTEGER NOT NULL,
  eligible_lines       INTEGER NOT NULL,
  tie_group            INTEGER NOT NULL,            -- 1 when another company has the same amount
  in_top200            INTEGER NOT NULL,
  boundary_status      TEXT NOT NULL,
  unresolved_lines     INTEGER NOT NULL,            -- unresolved lines that name this company as a possible issuer
  unresolved_cents     INTEGER NOT NULL,
  candidate_bound_cents INTEGER NOT NULL,
  PRIMARY KEY (version_id, rank)
);

CREATE TABLE top200_companies (
  version_id           TEXT NOT NULL REFERENCES pool_versions(version_id),
  rank                 INTEGER NOT NULL,
  issuer_id            TEXT NOT NULL REFERENCES issuers(issuer_id),
  issuer_name_at_period TEXT NOT NULL,              -- holding name of the largest eligible line that quarter
  amount_cents         INTEGER NOT NULL,
  fund_weight_raw      REAL,                        -- company amount / fund total investments
  tie_group            INTEGER NOT NULL,
  eligible_securities  INTEGER NOT NULL,
  blocked_members      INTEGER NOT NULL,            -- 1 when unresolved lines name the company in that quarter
  rule_version         TEXT NOT NULL,
  PRIMARY KEY (version_id, rank),
  UNIQUE (version_id, issuer_id)
);

CREATE TABLE top200_securities (
  version_id           TEXT NOT NULL REFERENCES pool_versions(version_id),
  issuer_id            TEXT NOT NULL REFERENCES issuers(issuer_id),
  security_id          TEXT NOT NULL REFERENCES securities(security_id),
  class_code           TEXT NOT NULL,
  security_type        TEXT NOT NULL,
  ticker_at_period     TEXT NOT NULL DEFAULT '',    -- v05 ticker of the security (not dated)
  holding_lines        INTEGER NOT NULL DEFAULT 0,
  amount_cents         INTEGER NOT NULL,
  fund_weight_raw      REAL,
  candidate_eligible   INTEGER NOT NULL,
  candidate_reason     TEXT NOT NULL,
  PRIMARY KEY (version_id, issuer_id, security_id)
);

CREATE TABLE strategy_companies (
  version_id           TEXT NOT NULL REFERENCES pool_versions(version_id),
  issuer_id            TEXT NOT NULL REFERENCES issuers(issuer_id),
  historical_rank      INTEGER NOT NULL,
  status_cutoff        TEXT NOT NULL,
  current_status       TEXT NOT NULL,
  resolved_current_issuer TEXT NOT NULL REFERENCES issuers(issuer_id),
  current_ticker       TEXT NOT NULL DEFAULT '',    -- current code of the resolved current issuer, '' if not established
  strategy_eligible    INTEGER NOT NULL,
  blocked_reason       TEXT NOT NULL DEFAULT '',
  excluded_reason      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (version_id, issuer_id)
);

CREATE TABLE strategy_securities (
  version_id           TEXT NOT NULL REFERENCES pool_versions(version_id),
  issuer_id            TEXT NOT NULL REFERENCES issuers(issuer_id),
  security_id          TEXT NOT NULL REFERENCES securities(security_id),
  class_code           TEXT NOT NULL,
  current_security_id  TEXT NOT NULL DEFAULT '',    -- '' when the current security is not established from the archive
  current_ticker       TEXT NOT NULL DEFAULT '',
  strategy_eligible    INTEGER NOT NULL,
  reason               TEXT NOT NULL,
  PRIMARY KEY (version_id, security_id)
);

CREATE TABLE quarter_checks (
  version_id           TEXT NOT NULL REFERENCES pool_versions(version_id),
  check_name           TEXT NOT NULL,
  status               TEXT NOT NULL CHECK (status IN ('pass','fail')),
  detail               TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (version_id, check_name)
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
  next_absent_as_of       TEXT NOT NULL DEFAULT '',
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
  counterparty   TEXT NOT NULL DEFAULT '',
  evidence       TEXT NOT NULL DEFAULT '',
  method         TEXT NOT NULL DEFAULT '',
  needs_review   INTEGER NOT NULL DEFAULT 0,
  source_id      TEXT REFERENCES sources(source_id)
);

-- Structure only in v2 (not filled).
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

-- Reserved for price data (not filled). One row per security, session and price source.
CREATE TABLE prices_daily (
  security_id  TEXT NOT NULL REFERENCES securities(security_id),
  session_date TEXT NOT NULL,
  source       TEXT NOT NULL,
  open         REAL,
  high         REAL,
  low          REAL,
  close        REAL,
  adj_close    REAL CHECK(adj_close IS NULL),
  volume       REAL,
  turnover     REAL,
  source_id    TEXT REFERENCES sources(source_id),
  PRIMARY KEY (security_id, session_date, source)
);

-- Corporate actions. ratio = new shares per old share (splits) or the documented shares of the spun-off company
-- per parent share (spinoffs, '' when no documented share ratio exists). cash_amount = USD per entitled share on
-- the ex-date. vendor_factor = the vendor's own price factor for a spinoff (value of the parent retained), used when
-- no subsidiary price is available. resolution records which rule of the 2026-10-27 action triage produced the row.
CREATE TABLE corporate_actions (
  action_id   INTEGER PRIMARY KEY,
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  action_date TEXT NOT NULL,
  action_type TEXT NOT NULL,
  ratio       REAL,
  cash_amount REAL,
  needs_review INTEGER NOT NULL DEFAULT 0 CHECK(needs_review IN (0,1)),
  currency    TEXT NOT NULL DEFAULT 'USD',
  source      TEXT NOT NULL,
  source_id   TEXT REFERENCES sources(source_id),
  notes       TEXT NOT NULL DEFAULT '',
  sub_ticker  TEXT NOT NULL DEFAULT '',
  vendor_factor REAL,
  spin_value  REAL,
  resolution  TEXT NOT NULL DEFAULT ''
);

-- Reserved: coverage of prices per security and price source (not filled).
CREATE TABLE price_coverage (
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  source      TEXT NOT NULL,
  first_date  TEXT NOT NULL DEFAULT '',
  last_date   TEXT NOT NULL DEFAULT '',
  n_days      INTEGER NOT NULL DEFAULT 0,
  detail      TEXT NOT NULL DEFAULT '',
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

-- Legacy view (schema 1 meaning, kept for comparison): security spans of issuers in the fund that are not confirmed
-- delisted. It is NOT the historical pool: it uses the current status and successor projection. Use top200_companies.
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
    AND sp.issuer_id NOT IN (SELECT issuer_id FROM pool_exclusions)
    AND res.resolved_issuer_id NOT IN (SELECT issuer_id FROM pool_exclusions)
) u;

-- Original, unadjusted price provenance and public-version coverage (schema 5).
CREATE TABLE price_calendar(session_date TEXT PRIMARY KEY,session_type TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES sources(source_id));
CREATE TABLE price_lineage(security_id TEXT NOT NULL REFERENCES securities(security_id),opend_code TEXT NOT NULL,
 first_date TEXT NOT NULL,last_date TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL,
 source_id TEXT REFERENCES sources(source_id),PRIMARY KEY(security_id,first_date,last_date));
CREATE TABLE price_quarter_coverage(version_id TEXT NOT NULL REFERENCES pool_versions(version_id),
 security_id TEXT NOT NULL REFERENCES securities(security_id),active_from TEXT NOT NULL,active_to TEXT NOT NULL,
 expected_sessions INTEGER NOT NULL,present_sessions INTEGER NOT NULL,warmup_expected INTEGER NOT NULL,warmup_present INTEGER NOT NULL,
 status TEXT NOT NULL,missing_dates TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES sources(source_id),PRIMARY KEY(version_id,security_id));
CREATE TABLE price_anomalies(security_id TEXT NOT NULL REFERENCES securities(security_id),session_date TEXT NOT NULL,
 previous_date TEXT NOT NULL,close_ratio REAL NOT NULL,detail TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES sources(source_id),PRIMARY KEY(security_id,session_date));
CREATE TABLE price_comparison(security_id TEXT NOT NULL REFERENCES securities(security_id),session_date TEXT NOT NULL,
 differences TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES sources(source_id),PRIMARY KEY(security_id,session_date));
CREATE TABLE price_continuity(security_id TEXT NOT NULL REFERENCES securities(security_id),event_date TEXT NOT NULL,
 ratio REAL NOT NULL,previous_close REAL,current_close REAL,status TEXT NOT NULL,detail TEXT NOT NULL,
 source_id TEXT REFERENCES sources(source_id),PRIMARY KEY(security_id,event_date));

-- Total return index (schema 6, method event_v1). One row per security and session with a raw close.
-- daily_return_t = (close_t * split_factor_t + cash_t + spinoff_value_t) / close_{t-1} - 1; tri starts at 1.
-- security_id is a securities.security_id, or 'BENCH:VOO' for the benchmark only (VOO is never a pool member and
-- never enters prices_daily). checks.py verifies every other value exists in securities.
CREATE TABLE tri_daily (
  security_id  TEXT NOT NULL,
  session_date TEXT NOT NULL,
  close        REAL NOT NULL,
  tri          REAL NOT NULL,
  daily_return REAL,
  method       TEXT NOT NULL,
  flags        TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (security_id, session_date)
);

-- Descriptive correlation grouping (never a trading input). method_id names the parameter set in corr_methods.
CREATE TABLE corr_methods (
  method_id  TEXT PRIMARY KEY,
  definition TEXT NOT NULL
);
CREATE TABLE corr_groups (
  version_id  TEXT NOT NULL REFERENCES pool_versions(version_id),
  method_id   TEXT NOT NULL REFERENCES corr_methods(method_id),
  security_id TEXT NOT NULL REFERENCES securities(security_id),
  group_id    INTEGER NOT NULL,
  PRIMARY KEY (version_id, method_id, security_id)
);
