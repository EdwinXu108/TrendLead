# LeadTrend v0.5 第一阶段交接与审阅说明（Claude 审阅修复后）

日期：2026-10-25  
实现：Xiaomi MiMo v2.6  
审阅：Claude（优先）→ 本轮按 Claude 必修 4 项 + 小问题 2 项修复，等待 Claude 再审  
范围：仅第一阶段。**未**进入第二阶段、**未**下载真实数据、**未**跑参数搜索、**未**接实盘。

---

## 0. 本轮实际执行的命令与退出码

```powershell
& 'D:\Tools\miniconda3\python.exe' -m leadtrend_v05 generate-fixtures --output data/v05/fixtures
# fixtures written (103 sessions, 50 securities)

& 'D:\Tools\miniconda3\python.exe' -m pytest -q tests_v05
# 83 passed
# EXIT: 0

& 'D:\Tools\miniconda3\python.exe' -m leadtrend_v05 demo --config config/v05_bootstrap.json --output work/v05/stage1_demo
# status=ok
# run_id=stage1-33abf13c60f8
# dataset_kind=synthetic
# inputs_hash=33abf13c60f864591dfbfdc7392ae004ae379c71f092115694be32902bc51b6f
# n_trades=134
# final_equity=107508.371543
# EXIT: 0
```

旧原型未改动：`leadtrend/`、`tests/`、`config/research_registry.json`、`pyproject.toml`、根目录 `README.md`。

---

## 1. 逐条修复说明

### 1.1 基准起点对齐（必修 #1）

**改动文件：** `leadtrend_v05/benchmark.py`、`leadtrend_v05/pipeline.py`  
**新增测试：** `tests_v05/test_benchmark_alignment.py`（3 个）

- 基准不再从数据第一天收盘开始。
- 以策略**第一次实际成交**的 session 为 `comparison_start_session`，用初始本金按 VOO **当天开盘价**买入可分割份额；首日 `benchmark_value = initial_capital`。
- 之后按收盘价估值；分红在除息日收盘再投资（与 `build_tri` 同口径）；拆股只换份额不造损益。
- 无任何成交时：`comparison_available=false`，不输出对比收益数字。
- `summary.json` 新增：`comparison_start_session`、`comparison_end_session`、`comparison_available`、`comparison_note`、`strategy_comparison_return`。

**demo 比较结果（对齐后）：**

| 项 | 值 |
|---|---|
| comparison_start_session | 2025-02-18（首笔成交日） |
| comparison_end_session | 2025-05-30 |
| 起点本金 | 100,000 |
| 策略比较期收益 | **+7.508%**（107,508.37） |
| VOO 合成基准收益 | **+2.696%**（102,695.82） |
| 说明 | 第一次成交前的现金期不计入比较 |

`benchmark.csv` 首行：`session_date=2025-02-18, benchmark_value=100000.000000`。

**手算例（`test_benchmark_open_purchase_handcalc`）：**  
开盘价 100 买入 10,000 → 100 份；次日收盘 110 且每股分红 10 → 份数 `100 + 1000/110`，当日收盘价值 `100×110 + 1000 = 12,000`。

### 1.2 评分窗口连续 session（必修 #2）

**改动文件：** `leadtrend_v05/scoring.py`  
**新增测试：** `tests_v05/test_scoring_window.py`（7 个）

- `momentum_score(..., session_dates)`：窗口必须是**交易日历中截至信号日的最后 L+1 个连续 session**，且每一天都有 TRI。
- 窗口中间缺价 → `unscorable_reason="price_gap_in_window"`（不再跳过缺口凑满 L+1 点）。
- 信号日自身缺价 → `missing_price_at_signal`；日历不足 L+1 → `insufficient_history`。
- 删除未使用的 `gaps` 变量，改为真正的连续性检查。
- `build_tri`：拆股/分红生效日若无该证券价格，抛 `TRIEventError`，**不静默跳过**；由 pipeline 转为结构化 fatal 失败。

### 1.3 输入错误必须失败（必修 #3）

**改动文件：** `leadtrend_v05/dataio.py`、`leadtrend_v05/pipeline.py`  
**新增测试：** `tests_v05/test_input_errors.py`（7 个）

- `data_issues` 中 `severity=error|fatal` → `run_pipeline` 返回 `status=failed`，并仍写出全部诊断产物（`data_issues.csv`、`summary.json`、`run_manifest.json` 等）。
- `severity=info` 不影响运行状态（如 SNAP_FUTURE 日历外日期精度）。
- `ValueError` / `KeyError` / `TRIEventError`（非整数合股、分红缺金额、缺价等）统一转为 `_unsupported_fail` 结构化失败，保留已生成的部分行，不直接崩溃。
- `cash_dividend` 缺 `pay_date`：在 `dataio.load_inputs` 记 `severity=error`，运行随之失败。
- 测试覆盖：重复价格行、重复事件 ID、未知名单引用、info 不失败、1 合 3 导致 7 股非整数、缺派息日、失败仍写诊断。

### 1.4 分红 / 拆股流程级测试（必修 #4）

**改动文件：** `leadtrend_v05/fixtures.py`（新增 `EV_DIV_E01`）、`data/v05/fixtures/*`  
**新增测试：**

| 文件 | 内容 |
|---|---|
| `tests_v05/test_corporate_actions_pipeline.py` | (a) 持有跨除息日 → 应收=持股×每股分红，派息日现金增加且应收归零；(b) 除息日=执行日开盘卖出仍得分红；(c) 除息日=执行日开盘买入不得分红 |
| `tests_v05/test_split_exec_day.py` | (d) 拆股在执行日生效：持仓股数与当日成交股数与手算完全一致 |

**demo 分红应收与到账手工核对（SYN_E01）：**

| 日期 | 事件 | 数字 |
|---|---|---|
| 2025-03-27 收盘 | 持有 SYN_E01 | 69 股 |
| 2025-03-28 | 除息日 EV_DIV_E01 $0.40/股 | 应收现金 **27.60** = 69 × 0.40 |
| 2025-03-28～04-10 | 应收挂着 | `receivable_cash=27.60` |
| 2025-04-10 | 派息日前一日 cash | 634.954102 |
| 2025-04-11 | 派息日 | cash **662.554102**（+27.60），`receivable_cash=0` |

恒等式 `股票+现金+应收=总权益` 全程成立。

**拆股执行日手算（`test_split_at_exec_day_exact_shares`）：**

- 第一周买入 S1 999 股（`floor((100000-1)/100)=999`），现金剩 99。
- 第二周执行日 1 拆 2：999 → 1998 股；开盘 S1=50、S2=50。
- 可配置权益 `1998×50+99=99999`，名额预算 49999.5，目标各 999 股。
- 当日：S1 **卖出 999**（净差，不先卖后买）、S2 **买入 999**；断言与手算一致。

### 1.5 summary 字段更名（小问题 #5）

**改动文件：** `leadtrend_v05/pipeline.py`

- `target_ids_at_last_signal`：股票 ID 列表
- `target_count_at_last_signal`：整数数量

### 1.6 信号周口径统一（小问题 #6）

`summary.json` 现包含：

| 字段 | 值 | 含义 |
|---|---|---|
| `n_signal_weeks` | **22** | 日历内全部周度信号 session |
| `n_signal_weeks_without_target` | **6** | 尚无可用名单（或无法形成目标） |
| `n_signal_weeks_with_target` | **16** | 产生了目标持仓 |
| 其中有实际成交 | **14** | 16 中 14 周有 trades |
| 无成交 | **2** | `2025-02-21`（无换股周零交易）、`2025-05-30`（末周无下一 session，意图记 unexecuted） |

因此旧说法「16 个信号周、14 周有成交」是指**形成目标的 16 周**；全样本信号周为 **22**，其中 **6** 周因名单尚未公开而无目标。

---

## 2. 本轮新增 / 修改路径

| 路径 | 动作 |
|---|---|
| `leadtrend_v05/scoring.py` | 修改：连续窗口；TRIEventError |
| `leadtrend_v05/benchmark.py` | 重写：首笔成交开盘买入起算 |
| `leadtrend_v05/pipeline.py` | 修改：输入失败、异常捕获、comparison 字段、target 字段 |
| `leadtrend_v05/dataio.py` | 修改：分红缺派息日记 error |
| `leadtrend_v05/fixtures.py` | 修改：EV_DIV_E01 |
| `data/v05/fixtures/` | 重新生成 |
| `tests_v05/test_benchmark_alignment.py` | 新增 |
| `tests_v05/test_scoring_window.py` | 新增 |
| `tests_v05/test_input_errors.py` | 新增 |
| `tests_v05/test_corporate_actions_pipeline.py` | 新增 |
| `tests_v05/test_split_exec_day.py` | 新增 |
| `tests_v05/test_demo.py` | 新增 comparison / 分红 / target 字段测试 |
| `work/v05/stage1_demo/` | 重新运行 |
| `work/v05/stage1_review.md` | 本文件 |

旧原型仍未改动。

---

## 3. 产物路径

`D:\Workplace\LeadTrend\work\v05\stage1_demo\`

仍包含：`run_manifest.json`、`universe.csv`、`scores.csv`、`decisions.csv`、`trades.csv`、`daily_equity.csv`、`benchmark.csv`、`summary.json`、`data_issues.csv`（及 `class_selection_notes.csv`）。

---

## 4. 请 Claude 再审时重点看

1. **基准对齐**：`benchmark.py` 开盘买入 + 收盘估值 + 分红再投资；`pipeline.py` comparison 字段；`test_benchmark_alignment.py` 手算例。
2. **连续评分窗口**：`scoring.py` 的 `price_gap_in_window`；`build_tri` 缺价事件 fatal。
3. **输入失败路径**：`dataio.py` pay_date；`pipeline._fail_on_input_errors`；`test_input_errors.py`。
4. **分红/拆股流水级测试**：`test_corporate_actions_pipeline.py`、`test_split_exec_day.py`；demo 中 27.60 应收→到账手核。
5. **summary 字段与 22/16/14 口径**：是否与 `decisions.csv` / `trades.csv` 一致。

---

## 5. 输入 / 配置哈希（修复后）

**配置 `config/v05_bootstrap.json`**  
`1865cbeab65b0add51c0885f8643d67116bd02cc4dd88daca62b66986b8debb5`

**夹具 `data/v05/fixtures/`**

| 文件 | SHA256 |
|---|---|
| calendar.csv | `2eed5c4f1ab5d6a86c67f2f3d6fb15697bbca28485f6b84f30ef66ca46d4ef0a` |
| events.csv | `96074dbe1e5ce86f410735245067035651309906c456515196c2f53935fe5de3` |
| prices.csv | `8903c66a466ea1e89cf11c3e68ed4b225b307e3347b073160f5265d14e69080a` |
| securities.csv | `1bdbfaf6800a7b9e19674e0dbbb4c2c0eceb3fc79cdc0d68ccd9a918d237a5f2` |
| snapshot_members.csv | `484eeac520694e5472499d071ed565b1d2a1e22a6a11632656b727253daf30c4` |
| snapshots.csv | `f3a9f46a76f7b5ec4a66c9a35f453fc0b631de54683f7dc4bbe4f0f904f43fbe` |
| GENERATION.md | `bd9686b6ff2d131d349d5e06644dd92bc764936a41a87a81babf6194a1ccc5c6` |

**运行 inputs_hash**  
`33abf13c60f864591dfbfdc7392ae004ae379c71f092115694be32902bc51b6f`

**run_id**  
`stage1-33abf13c60f8`

---

## 6. 本次未做（按指示）

- 不优化性能  
- 不改「现金不足时按顺序买入」逻辑  
- 不加真实数据、参数网格、新策略功能  

修完即停，等待 Claude 再次审阅。
