# LeadTrend v0.5 使用说明

更新：2026-10-25（目录整理之后）。本文件只描述**当前**的目录、命令和规则；各阶段的历史过程见 `research/v05/reports/`，整理前的旧版说明保存在 `archive/2026-10-25_pre_reorg_code.zip`。

## 环境

- Python：`D:\Tools\miniconda3\python.exe`（3.13），命令一律加 `-X utf8`
- 依赖：标准库 + numpy/pandas；pytest 仅测试用；画热力图用的 matplotlib 放在 `vendor/stage4_plot_deps/`，不装进全局环境
- 引擎运行时固定 `PYTHONHASHSEED=0`（runner 会自动设置）
- 并行进程数用 `--workers auto`（`run-grid` 与 `bench` 都支持）。每次运行开始时取以下各项的最小值：
  - 上限 4：实测 4 进程后，8 进程只再快约 24%，内存却翻倍；
  - 保留 3 GB 可用内存给其他程序；
  - 按每个 worker 的估计峰值算出能容纳的进程数：explore 约 260 MB，daily 约 1.1 GB（实测约 1.0 GB 加余量）；
  - CPU 核数；
  - 读不到可用内存时取 1。
  选择结果与当时的可用内存记入 `measurement.json` 的 `workers_auto`。

## 目录

| 路径 | 内容 | 能否改动 |
|---|---|---|
| `leadtrend_v05/` | 代码；所有读写路径集中在 `paths.py` | 改代码须经投资者批准 |
| `tests_v05/` | 测试 | 随代码更新 |
| `config/` | 运行配置（`v05_bootstrap` 模拟数据、`v05_stage2` 数据准备、`v05_stage3` 开发段、`v05_stage4` 网格） | `v05_stage2.json` 的 `sec_user_agent` 不改 |
| `data/v05/raw/` | 原始下载（SEC N-PORT、OpenD 行情与公司行动、官方证据原件） | 只追加 |
| `data/v05/normalized/` | 规范化输入表（名单、证券、行情、事件、日历），已在实验登记中封存 | 不改 |
| `data/v05/overlays/` | 补充数据版本（VSCO 辅助行情、分拆估值、零碎股口径），带 `.sha256` 封条 | 不改 |
| `data/v05/prep/` | 数据准备的中间产物和人工核定输入（覆盖率表、评分阻断表、身份覆写等），`snapshots/` 为数据准备链的冻结中间版本 | 仅数据准备命令写入 |
| `data/v05/reference/` | 数据准备用到的行业成员证据 | 不改 |
| `data/v05/fixtures/` | 第一阶段模拟数据 | 可用 `generate-fixtures` 重建 |
| `research/v05/registry/` | 实验登记（第二阶段数据登记、第四阶段 72 配置登记及封条） | 不改 |
| `research/v05/results/` | 审阅通过的结果：`stage3_dev_run/`（第三阶段基础回测）、`stage4_unified/`（第四阶段统一数据 144 次运行、汇总、保留段决定） | 运行不会写入 |
| `research/v05/perf/cold_cache/…/` | `stage4_unified/runs` 的 144 次运行按相对路径引用的 7 个共享表（6 个窗口的评分表和名单表）；路径是原始运行时写定的，不能改名或移动 | 不改 |
| `research/v05/baseline/` | 冻结旧引擎的输出，回归模式的比对基准 | 不改 |
| `research/v05/reports/` | 各阶段交接报告 | 历史记录 |
| `work/` | 临时区：缓存、新运行、日志、测试临时目录 | 随时可以整个删除 |
| `archive/` | 整理前的代码快照、历史小文件压缩包、搬移与删除记录 | 只读 |

**所有新运行只能输出到 `work/` 下**；`research/` 和 `data/` 不会被运行命令写入（`safe_output` 强制检查）。需要把新结果升级为正式结果时，人工搬入 `research/v05/results/` 并在报告中说明。

## 常用命令

```powershell
$PY = 'D:\Tools\miniconda3\python.exe'

# 测试：默认跳过 6 个标记为 slow 的用例（需要真实开发数据、会建缓存），约 10 秒，应为 331 通过、6 跳过
& $PY -X utf8 -m pytest -q -o addopts= tests_v05 --basetemp work/pytest_tmp -o cache_dir=work/pytest_cache
# 改动信号、评分或缓存代码时，加 --runslow 再跑一次，约 80 秒，应为 337 通过
& $PY -X utf8 -m pytest -q -o addopts= tests_v05 --runslow --basetemp work/pytest_tmp -o cache_dir=work/pytest_cache

# 第一阶段模拟数据演示
& $PY -X utf8 -m leadtrend_v05 demo --config config/v05_bootstrap.json --output work/runs/demo

# 第三阶段开发段基础回测（L=20,N=10,b=2,R1），默认输出 work/runs/dev；--stress 为 10 bp
& $PY -X utf8 -m leadtrend_v05 run-dev
& $PY -X utf8 -m leadtrend_v05 run-dev --stress --output work/runs/dev_stress

# 第四阶段 72 配置开发段网格，默认输出 work/runs/grid；同输入同代码的结果会自动复用
& $PY -X utf8 -m leadtrend_v05 run-grid --workers auto --verify daily

# 网格汇总、区域判定和热力图；默认读 research/v05/results/stage4_unified/runs，输出 work/runs/grid_summary
& $PY -X utf8 -m leadtrend_v05 summarize-grid

# 新网格与审阅通过的统一版本矩阵逐字节全量比对（17 张逻辑表 + 整个 summary.json + acceptance 全字段）
& $PY -X utf8 -m leadtrend_v05 compare-grid --grid work/runs/grid --output work/runs/grid_compare.json --workers 2

# 探索档网格比对（级别必须与期望一致）
& $PY -X utf8 -m leadtrend_v05 compare-grid --grid work/runs/explore_grid --expect explore --output work/runs/explore_compare.json

# 把压缩/共享的精简产物无损展开成普通 CSV（输出目录须不存在）
& $PY -X utf8 -m leadtrend_v05 export-artifacts --input research/v05/results/stage4_unified/runs/L090_N08_b2_R2/cost_5bps --output work/runs/export_one
```

## 验证级别

`run-grid` 和 `bench` 都有 `--verify daily|regression|explore`，默认 `daily`：

- `daily`：每次运行都做完整的独立账本验收（资产恒等式、现金非负、冻结、事件不重复、分红、分拆、无换股周不交易、基准对齐、保留段日期）。**修改策略或做策略实验时用它。**
- `regression`：在 `daily` 之外，再与 `research/v05/baseline/` 的冻结结果逐字节比对。**只改工程代码（缓存、并行、读写、目录等，按理不应改变任何结果）之后，必须跑一次。**
- `explore`：**探索档**，与 daily 同一条经济计算路径，只省两件事——不写全部产物（只写 `summary.json`、`daily_equity`、`benchmark`、`trades`、`weekly_checks` 等 8 个文件，写出函数与 lean 相同、物理字节与 daily 一致）、不跑独立重放验收；改为在运行结束时检查 `max_asset_identity_error` 与每日 `cash_reconciliation_error` 是否超出验收容差（常量直接引用 `stage4_acceptance`）。`completion.json`/`run_manifest.json` 标记 `verification_level='explore'`、`acceptance={"status":"NOT_RUN"}`，manifest 注明"仅供探索筛选，不得作为正式结果或保留段依据"。探索档结果按级别隔离，不与 daily 互借。`summarize-grid` 默认拒绝汇总探索档；加 `--exploration` 后才能汇总，输出文件名和内容都带 explore 标注，且任何情况下不生成、不修改 `holdout_decision`。探索汇总中，一次运行"完成"的判定是状态 completed 且 `completion.json` 中 `explore_check.status` 为 PASS（不再看 acceptance，它在探索档为 NOT_RUN）；正式汇总的规则不变。

```powershell
# 工程回归：模拟数据、第三阶段两种成本、固定 12 配置小网格
& $PY -X utf8 -m leadtrend_v05 bench --mode synthetic --verify regression --output work/runs/check_synthetic
& $PY -X utf8 -m leadtrend_v05 bench --mode legacy --verify regression --artifacts full --output work/runs/check_stage3
& $PY -X utf8 -m leadtrend_v05 bench --mode small_check --verify regression --workers 1 --output work/runs/check_small

# 探索档 144 次（默认输出 work/runs/explore_grid）
& $PY -X utf8 -m leadtrend_v05 bench --mode grid_explore --verify explore --workers auto --cold-cache --output work/runs/explore_grid
```

参考耗时（`--workers 4`，B 段实测）：144 次探索档热缓存约 38 秒，冷缓存另加约 16 秒（建缓存）；24 次 daily 快检约 35 秒。daily 每个 worker 峰值约 1.0 GB，4 进程时运行期间可用内存最低约 80 MB，因此在内存紧张的机器上 `auto` 会自动减少进程数。

完整 144 次的回归，用 `daily` 跑一遍网格后，用 `compare-grid` 与 `research/v05/results/stage4_unified/runs/` 做全量逐字节比对（这批结果已被证明与冻结旧引擎一致）。`compare-grid` 对探索档网格自动只比对其实写出的子集（4 张表 + summary.json）。`--expect daily|explore`（默认 `daily`）指定每个新运行必须具有的验证级别，级别不符的运行对记为 FAIL；没有级别字段的旧记录按 daily 计。比对探索档网格须加 `--expect explore`。参考数字：现有结果自比为 144 对、2736 项、0 失败；任何一项不一致或读取失败都记 FAIL 并逐项列出，不会静默跳过。

注意：冻结旧引擎的源码副本（`research/v05/baseline/source/`）写死了整理前的旧路径，**不能再执行**；它的输出仍然是有效的比对基准。`bench --mode baseline` 会因基准已存在而拒绝运行。

## 第一轮工程准备（2026-10-26）

依据 `docs/LeadTrend_策略优化方案_v1.md`。以下都是可选项，默认行为不变；第四阶段结果与基准逐字节检查均按原样通过。

- **选股规则**（`selection`，只在策略实验登记中使用）：`ent_pos`（`short_window`, `short_weight`；入场分 E = (1−w)·P_L + w·P_短，先在保留线内按 E 补位，再按可行顺序 F 补位）、`ent_neg`（`short_window`, `max_short_pct`；保留线内跳过短窗口百分位高于门槛者，第二遍不设门槛）、`sector_cap`（`sector_k`；每个大板块的上限，板块见 `config/sector_map_v1.csv`，不明行业以自身标签为板块）、`exit_cap`（`max_rank_exits`；每周因排名掉出保留线的退出最多执行这么多只，其余顺延一周）。`random_matched` 新增可选的 `sector_k`，随机组合遵守同一大板块上限。
- **信号**（`signal.kind`）：`peer`（`L`, `L2`；同板块同伴的 P_L2 中位数）、`sector_demeaned`（`L`；减去同板块 M_L 中位数，成员少于三只时减去 U_L 中位数）、`vol_adjusted`（`L`, `vol_L`；M_L ÷ 最近 vol_L 个日对数收益的样本标准差）、`size_tilt`（`L`；0.9·P_L + 0.1·发行人 VOO 权重百分位）、`voo_weight`（`L`；发行人合计 VOO 权重，只作参照）。
- **登记网格**：每个块可写 `selection`、`selection_params`；也可写 `{"points": [...]}` 的显式列表，每个配置带 `anchor`（锚点配置 ID）。配置 ID 由信号、选股规则、参数、N、b、R 确定；去重计数把选股规则和参数纳入，第四阶段 72 个配置视为默认规则。成绩单另报告与锚点的配对差、两个时段、逐年剔除一年、随机对照（每个配置一组，退出只数取自自己的 10 bp 探索档运行）、换手、最大单股权重、单一板块超过 40%/50% 的天数占比，以及 ENT 两遍买入数、CAP 的触发与顺延次数。
- **数据规则 v2**（`accounting_rules_version = "v2"`，写入 `run_manifest.json`）：
  - 规则 A：分拆、拆股等产生的不足一股权益，若没有已核实的结算记录，先记为应收证券；价格出现当日按该收盘价转为现金应收，在事件生效交易日之后第 10 个交易日到账，不收手续费，每笔标记为近似并计入 `fractional_cash`。已核实的 GE、MMM、BBWI 结算记录不变。
  - 规则 B：持仓单日缺价（且当天无公司行动）按前一交易日收盘价估值，记为 `stale_mark`；执行日缺价的指令作废、不顺延（`void_order`）；信号日或窗口内缺价的持仓当周保留并占用名额（`price_gap_retained_held`），不再因此停止；连续两个交易日缺价仍然停止。
  - 只有在发生上述事件时，`summary.json` 才新增 `accounting_rules_version`、`single_session_stale_marks`、`voided_orders` 三项；未发生时字节不变。
- **命令**：

```powershell
# 中性参数等价检查：四个新规则以中性参数运行 L090_N10_b2_R2，两种成本的 summary.json 须与第四阶段逐字节相同（不登记、不计入试验次数）
& $PY -X utf8 -m leadtrend_v05 neutral-check --output work/runs/neutral_check --workers auto
# 第一轮工程准备诊断：D3R（权重与收益区间对齐）、D5R（规则 v2 下 9 个配置各 100 次随机对照）、D7（名次分段）、D8（VOO 权重十分位）
& $PY -X utf8 -m leadtrend_v05 diagnose --only D3R,D5R,D7,D8 --output work/runs/diagnostics/D1R --workers auto
```

## 代码模块

| 用途 | 模块 |
|---|---|
| 路径 | `paths` |
| 引擎核心（第一阶段） | `config`、`timeutil`、`calendar`、`dataio`、`eligibility`、`universe`、`scoring`、`selection`、`execution`、`ledger`、`benchmark`、`pipeline`、`report`、`fixtures` |
| 数据准备（第二阶段） | `sec_nport`、`opend_prices`、`ticker_map`、`industry_map`、`nyse_calendar`、`stage2_prepare`、`stage2_enrich`、`stage2_repair`、`stage2_validate` |
| 真实数据开发段运行（第三阶段） | `stage3`（`run-dev`）、`stage3_scoring`、`stage3_ledger`、`stage3_metrics` |
| 参数网格与保留段（第四阶段） | `stage4_registry`、`stage4_runner`、`stage4_summary`、`stage4_acceptance`（独立验收）、`stage4_remediation`（补充数据版本、保留段授权）、`stage4_evidence` |
| 性能（缓存、并行、精简产物、基准测量） | `perf_cache`、`perf_runner`、`perf_artifacts`、`perf_bench`、`perf_compare` |

## 数据准备

`stage2-prepare`、`stage2-enrich`、`stage2-repair`、`validate` 需要 OpenD 登录运行和 SEC 联系方式，会写入 `data/v05/prep/` 和 `data/v05/normalized/`。**重新运行它们会改变数据版本**，登记会因哈希不符而拒绝后续运行，必须重新登记并说明原因。日常策略实验不需要运行这些命令。

## 保留段

保留段（2024-07-05 起）只能运行一次，入口是 `run-holdout`。除了登记哈希、唯一入选点和全部判定证据之外，还要求投资者在 Claude 审阅之后亲自创建 `research/v05/holdout/authorization.json`；没有这个文件，命令一律拒绝。

## 已知待验证事项

- 引擎固定 `PYTHONHASHSEED=0`，尚未验证不同种子下经济结果是否完全一致。
- 分拆、冻结、现金收购等账本路径只在持有 GE、MMM、BBWI 分拆时遇到过真实样本；现金收购、换股合并尚无真实样本。
