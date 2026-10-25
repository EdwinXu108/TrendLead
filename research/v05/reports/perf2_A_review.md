# 性能第二轮 A 段报告

## 一、改动文件与原因

- **stage4_summary.py**：`evaluate_regions(exploration=)`，探索汇总中"完成"= completed 且 `explore_check.status==PASS`；非探索路径不变。热力图标题改为英文。
- **perf_compare.py / __main__.py**：`compare-grid --expect daily|explore`（默认 daily），级别不符即该对 FAIL；无级别字段按 daily。
- **perf_runner.py**：`explore_check` 增加现金下限（`stage4_acceptance.CASH_FLOOR` 直接引用）；`worker_record` 把 PID 与峰值工作集只放进返回值（不进 completion.json）；run_grid 报告新增父/worker 峰值、当前工作集、`prepared_pkl_bytes`、`hot_cache_load_seconds`（冷建后立即真实热加载一次）。
- **perf_bench.py**：`working_sets()`（ctypes GetProcessMemoryInfo，当前与峰值）。
- **perf_cache.py（重写）**：
  - `FastScores`：闭式比率路径，仅在 TRI 窗口完整、无评分阻断、无拆股/分拆、无未定价事件、起点非零时使用；其余回退 `ScoreEngine.score`。
  - 价格改为 `px_open/px_close/px_present.npy`，`QuoteView`/`MarkView` 按需取值（KeyError 文本与 defaultdict 空日语义保持）。
  - pickle 只留小对象：engine、frames、价格 dict 不入库；`full.prices` 仅基准行，`full.members` 清空。
  - `SharedFrames`：从已哈希的共享 CSV 重建，每次运行只物化本次 L；universe 行仅 full 产物使用时读取。
- **stage3.py**：`tables['universe']` 仅在 `artifact_mode=='full'` 时扩展（lean/explore 不写该表）。
- **README_v0.5.md**：`--expect` 用法，探索汇总的完成判定。
- **测试**：改 `test_perf_explore.py`（fixture 增加 cash 列、`_explore_grid` 增加 explore_check）；改 `test_perf_compare.py`（探索子集测试显式 `expect='explore'`，新增级别不符、旧记录、非法 expect 等用例）；新增 `test_perf_arrays.py`（视图语义、合成引擎边界上 fast==ScoreEngine 逐格比较、SharedFrames 往返、Python 类型）、`test_perf_fastpath_real.py`（真实开发段数据逐格 `==` 与 `repr` 全部相等，24 s）、`test_perf_measurement.py`。
- 删除 `work/pytest_tmp_seg1b` 至 `seg1o`（14 个）。

## 二、验证结论

| 验证 | 结果 |
|---|---|
| 全部测试（最终代码） | 234 通过，0 失败 |
| A1：非探索 summarize 的 all_results.csv、region_criteria.csv | 与 `research/v05/results/stage4_unified/summary/` 逐字节相同 |
| 快检 small_check cold（A3，`work/runs/A3_small`） | PASS，24 次全部与冻结基准逐字节一致 |
| 7 个共享文件物理 SHA256（新缓存） | 与任务表 7 个值完全一致 |
| 探索全量 run-grid explore（`work/runs/A3_explore`） | 144 次 completed |
| compare-grid --expect explore | PASS，144 对，0 失败，逐对 4 表 + summary.json |
| 热缓存 daily 快检（`A3_small_hot`） | PASS，24 次逐字节一致，cache_hit=True |
| 热缓存 explore 快检（`A3_small_hot_explore`） | PASS，24 次完成 |
| 输出文本扫描 numpy 痕迹 | 0 处 |
| research/、data/、config/ 修改 | 无（与改动前快照比对） |
| holdout authorization.json | 不存在 |

## 三、内存与耗时对照

| 指标 | 改动前（任务说明参考） | A2 后（冷建） | A3 后（冷建） | A3 热缓存 |
|---|---|---|---|---|
| 建缓存 | 38–70 s | 38.2 s | **16.4 s**（<30，达） | — |
| 热缓存加载 | — | 3.8 s | **0.10 s**（<5，达） | — |
| prepared.pkl | ~264 MB | 240 MB | **3.1 MB** | 3.1 MB |
| 父进程峰值工作集（daily small） | ~1.9 GB | 2107 MB | 1808 MB（冷） | 1801 MB |
| 父进程峰值工作集（explore small/全量） | — | — | 1272 MB（explore 全量冷建） | **829 MB**（explore small 热） |

**未达标：单进程峰值 <600 MB。** 原因已定位，两部分均不在本段可安全改动的范围内：

1. **daily/regression 路径**：`stage4_acceptance.independent_quotes` 每个进程一次性把 `prices.csv`（68.7 万行）读成 dict 并常驻（`_SOURCE_QUOTES`），峰值约 1.8 GB，与缓存无关。改写它属于独立验收，任务说明第五节排除。**需要 Claude 决定**是否在不改算法的前提下改为流式/数组化读取。
2. **explore 路径（热 829 MB）**：每个 L 的 score 行约 11.3 万条（每周入选约 490 只 × 230 周），以 dict 常驻，并为每条构造 `Candidate`。可改为按周惰性物化，预计可明显下降。本段未做，列为后续建议。
3. 冷建峰值（1.3–1.8 GB）来自 `load_inputs` 把 CSV 解析为对象，属于数据加载器，本段未动。

## 四、快速路径与回退格子数

- 快速路径 **560,013** 格，回退 **119,103** 格，合计 679,116 = 6 窗口 × 信号周 × 入选证券。
- 回退主要来自：分拆/拆股相关证券、窗口含未定价日、起点为零或窗口不完整（新上市）。

## 五、偏离与遗留问题

- 为避免与既有决策文件冲突，`summarize-grid` 的 A1 检查加了 `--decision work/runs/A_summary_check/holdout_decision.json`，决策文件仍只写在 work/ 内。
- 修改了既有测试的期望：`test_explore_level_compares_only_the_written_subset` 现显式 `expect='explore'`（新规则下默认 daily 会判该对 FAIL）；`test_perf_explore` 的 fixture 增加 cash 列与 explore_check。
- 因测量需要多跑了 3 次验证（`A3_small_hot`、`A3_small_hot_explore` 及 `mem_probe` 诊断，诊断脚本在 scratchpad，未入库）。诊断时误建的一个缓存目录已删除。
- `test_parallel_workers_match_single_process` 在改动前即偶发失败（约 2/4 次），本轮最终全量测试中通过；未定位根因，列为待查。
- `summarize-grid --exploration` 遇到任一 status 非 completed 的探索运行即整体拒绝（现有行为，本段未改）。
- 未运行 B 段；run_grid 的 runs 列表仍按完成顺序写入 measurement.json（B1 要求固定顺序，留待 B 段）。

## 六、待审阅决定

1. 是否允许把 `independent_quotes` 改为流式或数组读取（daily 峰值的主因）。
2. 是否在 B 段前把 score 行改为按周惰性物化（explore 峰值的主因）。
3. 第 2 条若同意，再决定是否继续 A3 之外的加速。
