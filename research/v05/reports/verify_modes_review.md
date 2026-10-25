# 验证模式工程交接（接手续跑，2026-10-25）

**结论：本轮工程任务"通过"——父进程释放只读缓存副本的内存相关改动之后，回归模式 12 配置×2 成本与冻结基准逐字节一致，日常模式 144 次冷/热启动完整跑通且 144 次经济结果与统一版本参照零不一致（1152 个比对项全部相等），synthetic 与第三阶段 5/10 bp 回归同样与冻结基准一致；全部运行在同一源码指纹下完成，未运行保留段、未调用交易接口、未改动任何受保护文件。**

续跑背景：上一轮 12 配置回归期间机器内存占用达到 95%，随后加入"父进程释放只读缓存副本"的工程改动；该改动改变源码指纹后，原测量被开始/结束指纹防护拒绝（退出码 1），改动之后的完整验收一直未完成。本轮补齐该证明。完整历史见文末附录（原暂停记录）与 `work/perf/verify_modes/pause_progress.md`。

## 源码指纹

- 当前（本轮全部验收所依据）`leadtrend_v05/*.py` 合成 SHA256：**e8814426160ec0c34af0e9bda3f075dcc389792b1a63879a161daf46c5f6f338**
- 与暂停时 `work/perf/verify_modes/final/commands.json` 记录的指纹一致；被拒绝的旧测量指纹 903a9302… 属于内存改动之前的代码，未再使用。
- 两套执行脚本在每一步运行前都重新计算并断言该指纹；本轮未修改任何 `leadtrend_v05/` 源码，因此无需从第 0 步重来。
- 引擎固定 `PYTHONHASHSEED=0`：执行脚本显式设置，runner 亦会在新解释器启动时固定，未绕过。
- 解释器：`D:\Tools\miniconda3\python.exe -X utf8`（Python 3.13.12），工作目录 `D:\Workplace\LeadTrend`，逻辑 CPU 20。

## 每一步的实际命令、退出码、耗时、内存峰值

内存口径：系统内存占用率（GlobalMemoryStatusEx，与任务管理器一致）与"本次运行进程树"工作集峰值分别记录；机器物理内存 16.83 GB。本机无关进程（Edge、ChatGPT、WorkBuddy、Defender、微信、Memory Compression 等）常态占用约 81–82%，先单独测得基线，再解读运行峰值。原始记录：`work/perf/verify_modes/takeover_2026-10-25/mem_*.json`。

### 第一套：2 进程（commands.json）

| 步骤 | 实际命令（解释器 -X utf8，cwd=项目根） | 退出码 | 外层 CLI 墙钟 | 系统内存峰值 | 运行树峰值 | 最小可用物理内存 |
|---|---|---|---|---|---|---|
| pytest | `-m pytest -q -o addopts= tests_v05 --basetemp work/perf/verify_modes/takeover_2026-10-25/pytest_run_1792922994 -o cache_dir=work/perf/verify_modes/takeover_2026-10-25/pytest_cache` | 0 | 17.318 s | 92% | 0.36 GB（2.11%） | 1300.5 MB |
| 第1步 小网格回归 | `-m leadtrend_v05 bench --mode small_verify_post --verify regression --workers 2 --output work/perf/verify_modes/takeover_2026-10-25/small_regression` | 0 | 147.323 s | 99% | 4.03 GB（23.95%） | 17.7 MB |
| 第2步 daily 冷启动 | `-m leadtrend_v05 bench --mode grid_verify_daily_2_cold --verify daily --workers 2 --cold-cache --output work/perf/verify_modes/takeover_2026-10-25/daily_cold` | 0 | 508.201 s | 99% | 3.78 GB（22.48%） | 6.8 MB |
| 第3步 daily 热启动 | `-m leadtrend_v05 bench --mode grid_verify_daily_2_warm --verify daily --workers 2 --output work/perf/verify_modes/takeover_2026-10-25/daily_cold` | 0 | 15.876 s | 80% | 0.04 GB（0.25%） | 3220.1 MB |
| 第4步 经济比对 | `-X utf8 work/perf/verify_modes/takeover_2026-10-25/compare_economic.py` | 0 | 18.141 s | 82% | 0.03 GB | 2964.7 MB |
| 第5步 synthetic | `-m leadtrend_v05 bench --mode synthetic --verify regression --output work/perf/verify_modes/takeover_2026-10-25/synthetic` | 0 | 1.286 s | 82% | 0.04 GB | 2965.5 MB |
| 第5步 第三阶段 | `-m leadtrend_v05 bench --mode legacy --verify regression --artifacts full --output work/perf/verify_modes/takeover_2026-10-25/stage3` | 0 | 30.794 s | 91% | 1.63 GB（9.71%） | 1501.3 MB |

pytest：**196 passed**（日志 `takeover_2026-10-25/pytest.log`）。2 进程回归/冷启动期间系统占用峰值 99%、最小可用物理内存降至 6.8–17.7 MB，超过约 90% 的阈值，因此按任务要求用 1 进程重跑第 1–3 步（第二套），2 进程结果全部保留作为附加证据（亦构成又一组串/并行一致对照）。

### 第二套：1 进程（commands_1w.json，因 2 进程超 90% 内存阈值而重跑）

| 步骤 | 实际命令 | 退出码 | 外层 CLI 墙钟 | 系统内存峰值 | 运行树峰值 | 最小可用物理内存 |
|---|---|---|---|---|---|---|
| 第1步 小网格回归 | `-m leadtrend_v05 bench --mode small_verify_post_1w --verify regression --workers 1 --output work/perf/verify_modes/takeover_2026-10-25/small_regression_1w` | 0 | 177.627 s | 98% | 2.24 GB（13.30%） | 275.5 MB |
| 第2步 daily 冷启动 | `-m leadtrend_v05 bench --mode grid_verify_daily_1_cold --verify daily --workers 1 --cold-cache --output work/perf/verify_modes/takeover_2026-10-25/daily_cold_1w` | 0 | 568.177 s | 91% | 1.98 GB（11.77%） | 1476.9 MB |
| 第3步 daily 热启动 | `-m leadtrend_v05 bench --mode grid_verify_daily_1_warm --verify daily --workers 1 --output work/perf/verify_modes/takeover_2026-10-25/daily_cold_1w` | 0 | 9.250 s | 80% | 0.04 GB | 3209.7 MB |
| 第4步 经济比对 | `compare_economic.py … daily_cold_1w … economic_byte_comparisons_1w.json` | 0（重跑后） | 5.646 s→重跑通过 | 80% | 0.03 GB | 3257.9 MB |

内存解读：运行自身进程树峰值由 2 进程的 3.78–4.03 GB 降至 1 进程的 1.98–2.24 GB（父进程只在建缓存时达到约 1.7 GB，随即释放再派生子进程，即本轮内存改动的预期行为）；最小可用物理内存由 6.8 MB 升至 1.43 GB，头部空间显著改善。系统峰值仍达 91–98% 主要由与本运行无关的常驻进程造成（实测基线 81–82%），本机在不关闭这些程序时无法把系统总量压到 90% 以下；两套运行均正常结束（退出码 0），无 OOM、无因换页导致的失败。

## daily 模式冷/热启动耗时，与上一轮 regression 全量耗时对比

| 场景 | 成本运行数 | 进程 | 外层 CLI 墙钟 | 说明 |
|---|---|---|---|---|
| 上一轮 regression 全量（历史参考） | 144 | 2 | 707.463 s（11 分 47 秒） | 含逐次全 CSV 展开 oracle 与完整 summary 字节核对 |
| 本轮 daily 冷启动 | 144 | 2 | **508.201 s**（8 分 28.2 秒） | 新冷缓存 305.803 MB，输出 148.8 MB；daily 级不做冻结 oracle 比对（stage_seconds 中 byte_comparison=0） |
| 本轮 daily 冷启动 | 144 | 1 | **568.177 s**（9 分 28.2 秒） | 同上，单进程 |
| 本轮 daily 热启动（全部复用） | 144 复用/0 执行 | 2 | **15.876 s** | 身份哈希 0.321 s＋逐文件内容核对 12.763 s＋结束指纹 0.330 s |
| 本轮 daily 热启动（全部复用） | 144 复用/0 执行 | 1 | **9.250 s** | 0.310 s＋7.257 s＋0.212 s |
| 上一轮全量复用（历史参考） | 144 | 2 | 17.917 s | — |

口径提醒：上一轮 707.463 s 为 regression 级（含 oracle），与本轮 daily 冷启动不同级，不能直接当加速比；同口径的本轮 daily 冷/热数据即上表。144 次全部识别为"复用"（executed=0、reused=144、cache_hit=True），没有把部分运行当作完成；热启动仍执行完整内容校验，未用修改时间冒充一致。本轮 daily 冷/热输出合计占用：`takeover_2026-10-25/daily_cold`（148.8 MB）与 `daily_cold_1w`（148.8 MB）及两份新冷缓存（各 305.8 MB）。

## 第 4 步经济比对：总项数与不一致数

- 比对对象：第 2 步 daily 冷启动 144 次结果 vs `work/v05/stage4_unified_perf/runs/` 对应 144 次（该批已被证明与冻结旧引擎逐字节一致）。
- 内容：trades、daily_equity、benchmark、positions、receivables、corporate_actions、fractional_cash 共 7 表（先解压/还原为逻辑字节再比对）＋整个 summary.json 字节（涵盖全部经济数字，该文件不含时间戳）；另记录 acceptance.json 全字段相等作为附加证据。
- **结果：144 对 × 8 项 = 1152 个比对项，不一致数 0**；2 进程与 1 进程两套 daily 冷启动结果分别比对，均为 1152/0；144 对的 acceptance 全字段亦全部相等。
- 原始结果文件：
  - `work/perf/verify_modes/takeover_2026-10-25/economic_byte_comparisons.json`（2 进程，144 条逐项记录）
  - `work/perf/verify_modes/takeover_2026-10-25/economic_byte_comparisons_1w.json`（1 进程，144 条逐项记录）
  - 汇总内嵌于 `takeover_2026-10-25/commands.json` 与 `commands_1w.json` 的 economic_compare 步骤。
- 逐项记录含双方 SHA256 与字节数；无任何筛除字段。任何一项不一致都会写 `BYTE_MISMATCH_STOP.json` 并中止，本轮未触发。

## 第 1、5 步与冻结基准一致的证明

- 第 1 步（`--verify regression`，12 配置×2 成本=24 次）：每次运行内部即对 `work/perf/baseline/runs/<配置>/cost_<bp>bps` 做全 CSV 展开后 SHA、完整 summary 字节及独立验收全字段比对，24/24 全部 PASS（1 进程重跑同样 24/24）。面板与 `work/perf/baseline/freeze.json` 的 `small_ids` 一致（已断言）。原始证明：`takeover_2026-10-25/small_regression/byte_comparisons.json`、`small_regression_1w/byte_comparisons.json`（各 24 条，每条覆盖 18 个文件＋验收全字段）。
- 第 5 步：synthetic demo 与 `work/perf/baseline/synthetic` 全部 9 个产物字节一致（`takeover_2026-10-25/synthetic/byte_comparisons.json`，PASS）；第三阶段 `L020_N10_b2_R1` 5 bp 与 10 bp（`--artifacts full`）与 `work/perf/baseline/stage3/cost_5bps`、`cost_10bps` 全部文件字节一致、验收全字段相等（`takeover_2026-10-25/stage3/byte_comparisons.json`，2/2 PASS）。
- 以上均在当前源码指纹 e8814426… 下实际跑完并实际比对，非计划值。

## 保护与未动事项（本轮实际检查）

- 未运行保留段：`work/v05/stage4_holdout` 不存在，执行锁文件不存在；未调用任何交易接口。
- `stage4_registry.verify()` 通过；freeze.json 的 5826 个数据/raw/登记保护项（大小＋mtime，封条项含 SHA）全部未变。
- 冻结基准源码副本 `work/perf/baseline/source/leadtrend_v05/` 38 个文件与 freeze.json 逐一相符；`work/perf/baseline/`、`work/v05/stage4_unified_perf/`、`data/v05/`、`work/cleanup/` 未做任何写入或删除。
- 现行源码相对优化前快照只差性能轮次改过的 7 个工程接入文件（perf_bench、stage3、stage3_scoring、stage4_acceptance、stage4_runner、stage4_summary、__main__）及新增 perf_runner.py 等，评分/选股/执行/账本等策略文件未动，与 `work/perf/perf_review.md` 记录一致。
- 记录：`takeover_2026-10-25/preservation_checks.json`。

## 遇到的问题和处理方式（含全部失败/中断记录，未删除）

1. **本轮第 1 次尝试（attempt1）：pytest 43 项 setup 错误、退出 1。** 原因：pytest 的 tmp_path 工厂在会话开始时清理 `--basetemp` 下保留的旧临时目录（上一轮独立试跑留下的 349 个文件），沙箱 safe-delete 防护把超过 50 文件的 rmtree 拦截为 `SystemExit(1)`；测试本身无问题（basetemp 为空时 196 全过）。处理：每次 pytest 使用新建的空 basetemp 目录，不在尝试间删除任何东西。记录保留：`takeover_2026-10-25/commands_attempt1.json`、`attempt1_note.json`、`pytest.log`。
2. **本轮 1 进程套 economic_compare_1w 第 1 次退出 1。** 原因：比对脚本收到相对路径，`NEW.relative_to(R)` 在写出 JSON 前抛 ValueError；属脚本缺陷而非经济不一致（比对已算完但未落盘）。处理：改为解析绝对路径后重跑，结果 1152 项 0 不一致。记录保留：`takeover_2026-10-25/economic_compare_1w_rerun.json`、`economic_compare_1w.log`；`commands_1w.json` 中该步骤原样保留。
3. **2 进程运行系统内存峰值 99%（最小可用 6.8 MB），超过约 90% 阈值。** 处理：按任务要求以 1 进程重跑第 1–3 步并单独记录（commands_1w.json）；2 进程结果保留为附加证据。系统峰值主要来自与本运行无关的常驻进程（实测基线 81–82%），已在内存表中分别列明系统总量与运行树峰值，避免把无关占用算到本次运行头上。
4. **历史（上一轮，附录）：** small_regression_post 退出 1——内存改动落在运行中途，开始/结束指纹防护拒绝认证（24 次成本运行已全部算完但未获认证）；随后投资者额度不足停止，final/ 第 4 步小回归被打断（0 字节日志）。本轮在其之上续跑完成。

## 第 6 步（可选）：并行计算哈希——未实施

未实施。理由：热启动复用阶段的逐文件内容核对为 7.3–12.8 s（1/2 进程），并行化需改动 `perf_runner.py`，将产生新源码指纹并按任务要求从第 0 步全部重验；上一轮性能报告亦明确"本轮不再为参考值改动已完成的冻结引擎"，且本任务定位为收尾验收、不做新功能。复用阶段保留完整内容校验，未用修改时间代替。

## 结论与停止点

第 0–5 步全部实际完成且通过（详见上表与各项原始 JSON）；第 6 步未实施。本轮证明：父进程释放只读缓存副本的工程改动未改变任何经济结果——回归模式与冻结基准逐字节一致、日常模式 144 次与统一版本参照 1152 项零不一致、synthetic 与第三阶段两成本回归一致。保留段未运行，未调用交易接口。完成后停止，等待 Claude 审阅。

---

## 附录：原暂停交接记录（历史，2026-10-25 暂停时）

# 验证模式工程交接：暂停，尚未最终验收

投资者因额度要求停止。完整当前进度见[暂停记录](verify_modes/pause_progress.md)。新增模式已实现，但最终完整144次daily/regression比较及性能数据尚未完成；不得据此认定通过。
