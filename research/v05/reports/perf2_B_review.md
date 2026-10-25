# 性能第二轮 B 段报告

## 一、改动与原因

- **perf_compare.py（闪烁测试根因）**：排序键只按配置编号，同一配置的 5bp 与 10bp 并列，并列项顺序取决于工作进程完成先后。改为按（配置, 成本）登记顺序。测试补了顺序断言。
- **stage4_acceptance.py（经审阅批准）**：`independent_quotes` 改为流式读取，只保留 open/close 原始文本（`_Quote`，带 `__slots__`，支持下标）。字段访问、过滤、覆盖规则不变。新增 `test_acceptance_quotes.py`。
- **perf_cache.py**：`SharedFrames` 只保留当前窗口表，换 L 时先释放旧表；universe 与窗口表仍只在需要时读取；identity 增加 numpy 版本。
- **perf_runner.py**：run_grid 按 L 分组派发任务；`runs` 列表按登记顺序写出；`--workers auto`（`auto_workers`）；`_MemorySampler` 记录运行期间最低可用物理内存；报告新增 `workers_auto`、`start/min_available_physical_bytes`。
- **perf_bench.py**：`available_physical()`（GlobalMemoryStatusEx）。
- **__main__.py**：`--workers` 接受 `auto`（run-grid 与 bench）。
- **stage2_prepare.py**：第 24 行语法错误修复（把 `from . import paths` 移到导入语句之外，导入内容不变）。该模块改动前即无法导入，本次只修语法，未运行。
- **layers.py（新）**：ast 导入闭包（含函数体内导入），`layer_closures()` 计算三层入口的闭包。
- **测试**：新增 `test_layers.py`（闭包规则、动态导入禁止、numpy 版本、两条分层测试以 strict xfail 记录阻塞）；`test_perf_measurement.py` 增加 auto 规则与 CLI 测试。

## 二、验证结论

| 验证 | 结果 |
|---|---|
| 闪烁测试 | 根因已修复；修复前 12 次中复现 1 次，修复后 30/30 次相等；单文件 5 次运行全部通过 |
| 全部测试（最终） | 241 通过，2 个 strict xfail（分层阻塞，见第五节） |
| legacy 回归（A 段漏跑，现补跑） | PASS，5bp 与 10bp 均与冻结基准逐字节一致 |
| 快检 small_check，`--workers auto --cold-cache` | PASS，24 次逐字节一致；auto 选 1 个 worker（可用内存 4.78 GB，保留 3.2 GB） |
| explore 全量 workers=1/4/8 与参考比对 | 每个 144 对，0 失败 |
| 复用检查 | 第一次 explore 144 次全部执行（55 s）；README 加空行后再跑，144 次全部复用（4.4 s）；README 已还原，与保存副本逐字节相同 |

## 三、测量（推荐默认值依据）

### explore 全量（144 次）

| workers | 墙钟 | 缓存 | 每 worker 峰值 | 父进程峰值 | 运行期最低可用内存 |
|---|---|---|---|---|---|
| 1 | 138.4 s（含冷建 16.4 s） | 冷建 | — | 1214 MB | 2081 MB |
| 4 | 37.8 s | 热（0.09 s） | 约 242 MB | 121 MB | 2117 MB |
| 8 | 30.4 s | 热（0.09 s） | 约 240 MB | 122 MB | 1577 MB |

加速（热缓存，w1 去掉冷建约 122 s）：w4 约 3.2×，w8 约 4.0×。4→8 只再快 24%。

### daily（small_check 24 次，workers=4）

- 墙钟 34.9 s；每 worker 峰值约 1002 MB（验收源数据改流式前约 1.8 GB）；运行期最低可用内存仅 78 MB。
- 说明在这台 16 GB 机器上，4 个 daily worker 已接近内存上限，因此 auto 必须保留 3 GB。

### 共享页

多 worker 时，可用内存降幅低于各 worker 峰值之和：w4 为 847 MB ÷ 4 ≈ 212 MB/个（峰值 243 MB）；w8 为 1413 MB ÷ 8 ≈ 177 MB/个（峰值 241 MB）。说明 npy 映射与缓存文件页确有共享。可用内存数字也受其他程序影响，仅作参考。

### 推荐默认值

- `AUTO_WORKER_CAP = 4`（4→8 边际收益明显变小，内存翻倍）。
- `AUTO_RESERVE_BYTES = 3 GB`。
- 每 worker 估计峰值：explore 260 MB，daily 1100 MB。
- auto 规则：`min(4, CPU 数, floor((可用内存 − 3 GB) / 每 worker 估计峰值))`，不可读可用内存时取 1。

## 四、A 段遗留问题的处理

- **闪烁测试**：根因是 compare_grid 的排序键设计（见第一节）。不是并行或时序问题，已修复并加了顺序断言。
- **单进程 600 MB 目标**：未达。daily 峰值由独立验收的行情源数据决定（改流式后约 1.0 GB/worker，仍超过目标）；冷建峰值 1.2–1.3 GB 来自数据加载器。审阅已决定不再追此目标。

## 五、B2 未完成：需要 Claude 决定

按 ast 导入闭包实测（含函数体内导入），scoring 层与 run 层的闭包是同一组 40 个模块，包括 `__main__`、`selection`、`stage3`、`stage4_summary`、`perf_runner`。原因：

1. `perf_cache` 在函数内导入 `selection`（precompute_frames 中的 Candidate）与 `stage3`（load_children）。
2. `stage4_runner` 在模块顶层导入 `stage3`；`PreparedGrid` 在函数内导入 `stage4_summary.verify_holdout_claim`，而 `stage4_summary.run_holdout` 又导入 `stage4_runner`。
3. `perf_bench` 在函数内导入 `__main__`（benchmark/baseline 入口）。

已完成：`layers.py`、numpy 版本进入 identity、动态导入禁止测试（通过）、闭包包含函数内导入（通过）、两条分层测试（strict xfail）。

未完成：三层标识（数据/评分/运行）、读文件追踪测试、分层行为测试（改 stage4_summary 注释、改 selection、改 scoring、改规范化 CSV）。在现有导入结构下，"改 selection.py 只有运行层变化" 不成立，改选股代码会重建评分缓存。

要让分层成立，需要拆分（每项都需要你批准）：

1. 把 `Candidate` 从 selection.py 移到独立模块。
2. 把 `load_children` 从 stage3.py 移到数据层模块。
3. 把 `PreparedGrid` 从 stage4_runner.py 移到不导入 stage3 的模块，并断开它对 `stage4_summary.verify_holdout_claim` 的导入。这一步触及保留段相关的校验函数，任务说明禁止碰保留段，所以需要你的明确批准。
4. 把 perf_bench 中的 benchmark/baseline 入口移到单独模块。

如果第 3 项不批准，分层无法按现有保留段边界成立；此时建议把分层要求降为"只按运行入口分层"，并接受评分层随运行代码失效。

## 六、偏离与注意

- 改动后 identity 加入了 numpy 版本，缓存键变化。B1 的 explore 测量在该改动之前完成；B 收尾的 small_check、legacy、复用检查在之后完成。两组结果均与冻结基准或参考逐字节一致。
- 为复用检查临时改了 docs/README_v0.5.md，已还原并核对逐字节一致。
- 为测量额外跑了：两次 explore 参考比对、一次 daily 4-worker 快检、若干诊断脚本（在 scratchpad，未入库）。诊断时误建的一个缓存目录已删除。
- B2 未实现分层，strict xfail 测试会在分层成立后失败，届时需要去掉 xfail。
- 未运行 stage2-prepare、validate、run-holdout；未创建 holdout authorization 文件（已核对不存在）。

## 收尾（按 Claude 决定执行）

Claude 决定：B2 分层不做，两个选项都不采用。保留现有的整包代码哈希缓存键。以下为收尾改动，之后停止。

1. **删除** `leadtrend_v05/layers.py` 与 `tests_v05/test_layers.py`（包括两个 strict xfail）。identity 中的 numpy 版本保留。
   - 随 `test_layers.py` 删除的还有：动态导入禁止测试、闭包规则测试、numpy 版本测试。numpy 版本仍在 identity 中，但现在没有测试覆盖它。
   - `layers.py` 位于包内，删除会改变整包代码哈希，下次运行将重建缓存（约 16 秒）。
2. **新增** `tests_v05/test_package_syntax.py`：用 `ast.parse` 解析 `leadtrend_v05/` 下每个 `.py`（递归），全部能解析才通过。已验证：对改动前备份的 `stage2_prepare.py` 会报第 24 行语法错误。
3. **更新** `docs/README_v0.5.md`：
   - 删掉"本机内存紧张时用 `--workers 1`"，改为 `--workers auto` 的规则（上限 4、保留 3 GB、按每 worker 估计峰值、CPU 核数、读不到内存时取 1，结果记入 `workers_auto`）；
   - 常用命令中的 run-grid（daily）与 explore 示例改为 `--workers auto`；
   - 增加参考耗时：144 次探索档热缓存 4 进程约 38 秒，冷缓存另加约 16 秒；24 次 daily 快检 4 进程约 35 秒；并注明 daily 每 worker 约 1.0 GB、4 进程时可用内存最低约 80 MB。
4. **测试**：全部测试一次，239 通过，0 失败。未运行任何网格、快检或回归。
5. 未触碰 research/、data/、config/；未运行 run-holdout；未创建 holdout authorization 文件。
