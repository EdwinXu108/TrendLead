"""Export every M2 and M3 configuration to research/mom_v1/configs_m2_m3.csv.

The rows come from the same task lists the runners use (run_m2.tasks / run_m3.tasks), so the file cannot drift
from what was executed. 660 + 78 = 738 rows.

    python -X utf8 research/mom_v1/export_configs.py
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, '.')
sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_m2
import run_m3

OUT = Path('research/mom_v1/configs_m2_m3.csv')


def main():
    rows = []
    for L, N, mode in run_m2.tasks():
        cfg = run_m2.config_for(L, N, mode)
        rows.append((cfg['run_id'], 'm2', cfg))
    for signal, N in run_m3.tasks():
        cfg = run_m3.config_for(signal, N)
        rows.append((cfg['run_id'], 'm3', cfg))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open('w', encoding='utf8', newline='') as f:
        writer = csv.writer(f, lineterminator='\n')
        writer.writerow(['run_id', 'batch', 'config'])
        for run_id, batch, cfg in rows:
            writer.writerow([run_id, batch, json.dumps(cfg, sort_keys=True)])
    print('rows', len(rows), 'unique', len({r[0] for r in rows}))


if __name__ == '__main__':
    main()
