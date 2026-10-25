# Synthetic fixtures for LeadTrend v0.5 stage 1

- Generator: `leadtrend_v05/fixtures.py` (`python -m leadtrend_v05 generate-fixtures`)
- Seed: `42` (Mulberry32; not Python `random`)
- dataset_kind: synthetic (NOT real VOO holdings or prices)
- Sessions: 103 explicit NYSE-like sessions 2025-01-02..2025-05-30
  - Holidays: 2025-01-20, 2025-02-17, **2025-04-18 (Friday holiday)**, 2025-05-26
  - Early closes: 2025-04-17, 2025-05-23 (13:00 America/New_York)
- Securities: 50 ids
  - 8 industries x 5 common stocks (eligible pool >= 40 so len(F) can exceed 20)
  - Dual class: SYN_DUAL_A / SYN_DUAL_B (same issuer ISS_DUAL)
  - Allowed specials: SYN_PREF_ADR (equity preferred ADR), SYN_BRK_B
  - Excluded examples: SYN_BRK_A (ticker), SYN_REIT1, SYN_SPAC1, SYN_LP1
  - Benchmark ETF: SYN_VOO (never a strategy trade)
  - Late member: SYN_NEW1
- Snapshots (explicit available_at, date precision = open of next session after that date):
  - SNAP_2025_01: as_of 2025-01-31, available after 2025-02-12
  - SNAP_2025_01_REV: same as_of, later revision available after 2025-03-05 (drops SYN_U02)
  - SNAP_2025_03: as_of 2025-03-31, available after 2025-04-11 (adds SYN_NEW1, drops SYN_M03)
  - SNAP_FUTURE: as_of 2025-05-30, available after 2025-07-15 — must NOT affect earlier signals
- Events:
  - EV_SPLIT_T01: 1->2 split on 2025-03-20
  - EV_DIV_F01: $0.50/sh ex 2025-04-10, pay 2025-04-25
  - EV_DIV_H01: $0.30/sh ex 2025-05-08, pay 2025-05-22
  - EV_DIV_E01: $0.40/sh ex 2025-03-28, pay 2025-04-11 (held name in demo)
  - EV_DIV_VOO: benchmark dividend only
- Prices: GBM-like paths with per-name drift/vol; split halfing after 2025-03-20 on SYN_T01;
  mechanical ex-date drops on dividend names. All values are synthetic.
