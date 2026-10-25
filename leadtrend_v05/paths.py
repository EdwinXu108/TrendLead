"""Project layout: every file or directory the package reads or writes, relative to the project root.

data/v05/      inputs: raw vendor files, normalized tables, data-preparation records, overlays, fixtures
research/v05/  registered experiments, authoritative results, the frozen regression oracle, reports
work/          scratch: caches, new runs and logs; safe to delete at any time
"""

# --- data -------------------------------------------------------------------
DATA = 'data/v05'
RAW = f'{DATA}/raw'
NORMALIZED = f'{DATA}/normalized'
FIXTURES = f'{DATA}/fixtures'
PREP = f'{DATA}/prep'              # data-preparation outputs and manually verified inputs
REFERENCE = f'{DATA}/reference'    # evidence used by data preparation (industry memberships)
OVERLAYS = f'{DATA}/overlays'      # supplemental data version applied on top of normalized

CHILD_KLINES = f'{RAW}/opend_kline_none_v1'
WEEKLY_COVERAGE = f'{PREP}/validate/weekly_coverage.csv'
SCORING_BLOCKS = f'{PREP}/stage2_scoring_blocks.csv'
CORPORATE_ACTION_GAPS = f'{PREP}/stage2_corporate_action_gaps.csv'
STAGE2_ACCEPTANCE = f'{PREP}/acceptance/acceptance_summary.json'
DATA_VERSION = f'{OVERLAYS}/data_version.json'
VSCO_EXTENSION = f'{OVERLAYS}/data_version_vsco_extension.json'
SCORE_OVERLAY = f'{OVERLAYS}/score_event_overlay.json'

# --- research ---------------------------------------------------------------
RESEARCH = 'research/v05'
REGISTRY = f'{RESEARCH}/registry'
STAGE2_REGISTRY = f'{REGISTRY}/stage2_experiment_registry.json'
STAGE4_REGISTRY = f'{REGISTRY}/stage4_registry.json'
STAGE4_REGISTRY_SEAL = f'{REGISTRY}/stage4_registry.sha256'
RESULTS = f'{RESEARCH}/results'
STAGE3_RESULTS = f'{RESULTS}/stage3_dev_run'
STAGE4_RESULTS = f'{RESULTS}/stage4_unified'
STAGE4_RUNS = f'{STAGE4_RESULTS}/runs'
HOLDOUT_DECISION = f'{STAGE4_RESULTS}/holdout_decision.json'
HOLDOUT = f'{RESEARCH}/holdout'
HOLDOUT_LOCK = f'{HOLDOUT}/execution_lock.json'
HOLDOUT_RESULTS = f'{HOLDOUT}/results.json'
BASELINE = f'{RESEARCH}/baseline'  # frozen pre-optimization engine and its outputs (regression oracle)

# --- scratch ----------------------------------------------------------------
WORK = 'work'
CACHE = f'{WORK}/cache'
COLD_CACHE = f'{WORK}/cache_cold'
RUNS = f'{WORK}/runs'
LOGS = f'{WORK}/logs'
MEASUREMENTS = f'{LOGS}/measurements'
PLOT_CACHE = f'{WORK}/plot_cache'

# --- vendored dependency ----------------------------------------------------
PLOT_DEPS = 'vendor/stage4_plot_deps'  # matplotlib for heatmaps; not installed in the base environment
