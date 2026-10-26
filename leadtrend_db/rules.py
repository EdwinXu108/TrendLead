"""Versioned rules of the quarterly Top 200 candidate pool (rule_version top200_v1).

The pool is the VOO-holding company ranking of the fund's own reported stock value. It is not a total-market-cap
ranking and not an official S&P index. Nothing here reads prices or market capitalisation.

Steps of one quarter (applied to one holdings version):
  1. Each holding line is resolved to an issuer on its holding date (identity.py, build.py).
  2. A stock line is eligible for the ranking when its security type is in ELIGIBLE_TYPES (common stock and ADRs of
     operating companies). Negative lines, non-stock lines and every excluded type (REIT, partnership, ETF, SPAC,
     preferred, right, warrant, CVR, other) are not counted, and the excluded lines are kept in the database.
  3. A company's amount is the sum of its eligible lines in integer cents (value_usd is whole thousands, or exact
     cents in N-PORT). A company is one slot, however many classes it holds.
  4. Companies are ranked by amount, largest first. Equal amounts share a tie_group and the order inside the group
     is the stable issuer_id order (never ticker, insertion order or current status).
  5. Historical top 200 = the first TOP_N companies. Companies without an eligible line are not in the ranking.
  6. The security boundary is applied after ranking: a selected company contributes the classes it held that quarter
     that are eligible (ELIGIBLE_TYPES and not FORBIDDEN_CANDIDATE). BRK.A is never a candidate; BRK.B is.
  7. Strategy list = historical list filtered by the status at STATUS_CUTOFF (a delisted company is excluded with a
     recorded reason, never replaced by rank 201 or later). Blocked members stay in the list as blocked.
  8. A quarter is ready only when no unresolved line can move a company across the 200th place (see top200.bound).

Public time rule. A version becomes known at available_at_utc (SEC acceptanceDateTime of its filing). A query at a
UTC time returns the latest report period whose version is public by then, and within that period the latest
version public by then.
"""
from __future__ import annotations
import csv
import hashlib
from pathlib import Path

RULE_VERSION = 'top200_v2'
EXCLUSIONS_PATH = Path(__file__).resolve().parent.parent / 'config' / 'pool_exclusions_v1.csv'
PERMANENT_EXCLUSION_REASON = 'investor_permanent_exclusion'
MERGER_RULE = ('A rename or a single-company fixed-ratio holding-company/redomiciliation succession continues. '
               'In a merger only the legally surviving continuously listed party continues; acquired parties and '
               'both predecessors of a newly formed merger issuer are retired. New issuers start with their own holdings.')


def permanent_exclusions(path: Path = EXCLUSIONS_PATH) -> dict[str, dict]:
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    result = {r['issuer_id']: r for r in rows}
    if len(result) != len(rows) or any(not r['issuer_id'].startswith('CIK:') for r in rows):
        raise ValueError('permanent exclusion IDs must be unique CIK issuer IDs')
    return result
TOP_N = 200
BOUNDARY_RANGE = (180, 220)                 # rank range exported for review
ELIGIBLE_TYPES = ('common', 'adr')          # security types that may be candidates
FORBIDDEN_CANDIDATE = {'BRK.A': 'Berkshire class A: investor forbids purchase (company still ranks)'}
FORBIDDEN_CUSIPS = {'084670108': 'BRK.A'}
# fixed dates so that the database can be rebuilt offline with the same result
STATUS_CUTOFF = '2026-10-26'                # status of current listings and delisting notices as of this date
BUILD_CUTOFF_UTC = '2026-10-27T00:00:00Z'   # filings accepted after this are not part of the build
EARLIEST_QUARTER = '2013-12-31'
CENT = 100                                  # ranking unit: integer cents


def describe() -> dict:
    """The rule as data (written to the meta table and to the exports' documentation)."""
    return dict(rule_version=RULE_VERSION, top_n=TOP_N, boundary_range=list(BOUNDARY_RANGE),
                permanent_exclusions=permanent_exclusions(),
                pool_exclusions_sha256=hashlib.sha256(EXCLUSIONS_PATH.read_bytes()).hexdigest(),merger_rule=MERGER_RULE,
                eligible_types=list(ELIGIBLE_TYPES), forbidden_candidate=FORBIDDEN_CANDIDATE,
                status_cutoff=STATUS_CUTOFF, build_cutoff_utc=BUILD_CUTOFF_UTC, earliest_quarter=EARLIEST_QUARTER,
                amount_unit='integer cents of reported value_usd', ranking='company amount descending, '
                'tie by issuer_id')
