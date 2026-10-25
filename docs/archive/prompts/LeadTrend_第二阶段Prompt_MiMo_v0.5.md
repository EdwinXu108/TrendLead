# LeadTrend v0.5：给 Xiaomi MiMo v2.6 的第二阶段 Prompt

撰写：Claude（审阅优先）。日期：2026-10-25。
用途：投资者复制分隔线之间的内容交给 MiMo。完成后交 Claude 审阅；本提示词不授权进入第三阶段。

---

你是 LeadTrend v0.5 的实现工程师。第一阶段已通过 Claude 审阅。本次任务分两部分：**先完成 A 部分的小修，再做 B 部分，把真实 VOO 名单和行情接入为第一阶段的输入格式。**工作目录固定为 `D:\Workplace\LeadTrend`。

## 零、先读规格，确认边界

先阅读：

1. `D:\Workplace\LeadTrend\AGENTS.md`
2. `D:\Workplace\LeadTrend\docs\LeadTrend_研究方案_v0.5.md`
3. `D:\Workplace\LeadTrend\docs\LeadTrend_代码实施计划_v0.5.md`（第 4 节是本阶段依据）
4. `D:\Workplace\LeadTrend\work\v05\stage1_review.md`

**本阶段授权修改或新增的路径：**`leadtrend_v05/`、`tests_v05/`、`config/v05_stage2.json`、`data/v05/raw/`、`data/v05/normalized/`、`work/v05/` 下以 `stage2` 开头的文件和目录，以及 `docs/README_v0.5.md`。不要改动旧原型（`leadtrend/`、`tests/`、`config/research_registry.json`、`pyproject.toml`、根目录 `README.md`），也不要改 `docs/` 下的规格文件。发现规格问题时，写进交接说明。

**本阶段不做：**不跑完整回测，不输出策略收益或与 VOO 的胜负对比，不选参数，不跑参数网格，不实现冻结持仓、分拆、并购（这些属于第三阶段），不接实盘，不调用任何交易接口。

---

## A 部分：先完成第一阶段留下的小修

每一项都要有测试；完成后，第一阶段的 synthetic demo 必须仍然能跑，而且除了运行时间等元数据，**经济结果与修改前完全一致**（第 4 项是纯性能改动，必须用这条做回归检查）。

**A1. 把“情景发生才检查”的测试改成必须发生。**
`tests_v05/test_corporate_actions_pipeline.py` 中“除息日开盘卖出仍获分红”和“除息日开盘买入不获分红”两个测试，以及 `tests_v05/test_input_errors.py` 中“非整数合股”测试，现在在情景没有发生时会直接通过。请调整夹具，确保情景一定发生，并先断言它发生了（例如 `assert sells_on_ex`、`assert buys_on_ex`、断言该股票在合股日前确实被持有），再检查结果。不要保留 `if ... pass` 或“断言可能为空”的分支。

**A2. 基准第一天按收盘估值。**
现在 `benchmark.py` 把基准第一天的值记为开盘投入的 100,000，而策略第一天是收盘估值，两者口径不一致。改为：第一天也记为 `买入份额 × 当日收盘价`。开盘投入的金额和日期，另外写进 summary 的 `comparison_start_value_at_open` 与 `comparison_start_session`。期末收益的计算方式不变。更新 `test_benchmark_alignment.py` 的手算例子。

**A3. 区分“历史不足”和“价格缺口”。**
`scoring.py` 的 `momentum_score`：如果窗口中缺的日期全部早于这只证券的第一个有效价格，标为 `insufficient_history`；只有在第一个有效价格之后出现缺口，才标为 `price_gap_in_window`。补一个测试：窗口中途才开始有价格的证券应得到 `insufficient_history`。

**A4. 性能：去掉按日期逐条扫描。**
现在 `pipeline.py` 中有多处在每个 session 内线性扫描全部价格、事件和日历（取开盘价、收盘价、`next_session`、执行日对应关系、当天的事件）。真实数据约有 500 只以上证券、数年 session，这样会慢到无法运行。请改为事先建立索引：`(security_id, session_date) → 价格行`、`session_date → 当天的事件列表`、session 的位置索引，以及“信号日 → 执行日”的映射。`momentum_score` 中“截至信号日的 session 列表”也改为按索引切片。要求：
- synthetic demo 的经济结果与修改前完全一致（trades、daily_equity、scores、decisions 逐行相同）；
- 新增一个规模测试：用程序生成约 500 只证券、约 750 个 session 的 synthetic 数据，完整跑一遍 pipeline，在交接说明中报告耗时。不设硬性时间门槛，但耗时应明显可接受（例如几分钟以内）。

**A5. 缺价日上的事件只影响这只证券。**
现在任何一只证券的拆股或分红落在缺价日，整个运行都会失败。真实数据中这种情况会出现，不能因为一只从未持有的股票让整个回测失败。改为：
- 如果该证券在事件日**没有被持有**：从事件日起，这只证券标为不可评分（`unscorable_reason="unresolved_event_price_gap"`），不能成为新候选，并写进 `data_issues.csv`（severity=warning）；
- 如果该证券在事件日**正被持有**：仍然显式失败，并保存诊断（持有中的公司行动无法记账，不能继续）；
- 基准（VOO）的事件落在缺价日：仍然显式失败。

**A6. 外层异常也保留已生成的产物。**
`run_pipeline` 外层捕获 `ValueError`、`KeyError`、`TRIEventError` 时，现在丢掉了已经生成的部分产物。请把内层已经写出的 universe、scores、decisions、trades、equity 一并保留到失败结果中。

---

## B 部分：接入真实 VOO 名单和行情

### B0. 总原则

- 只使用免费数据。本阶段允许的网络访问：SEC EDGAR（`www.sec.gov`）、本机 OpenD（只用行情接口），以及下文明确提到的免费来源。不要安装大型框架；如果确实需要安装一个小型免费依赖，先在交接说明中写明理由和版本，并且只装到 `D:\Tools\miniconda3\python.exe` 这个环境。
- 所有原始响应都只追加保存在 `data/v05/raw/` 下，记录来源 URL 或接口名、请求参数、取得时间（`retrieved_at`）和 SHA256。规范化结果放在 `data/v05/normalized/`，格式与第一阶段 `data/v05/fixtures/` 完全相同（calendar、securities、snapshots、snapshot_members、prices、events），这样第一阶段的 pipeline 可以直接读取。
- 每一步都要能从缓存断点续跑：先读本地缓存，只请求缺失部分。
- `retrieved_at` 永远不能代替 `available_at`。

### B1. VOO 历史持仓名单（SEC N-PORT）

- 来源：Vanguard 500 Index Fund 的 N-PORT-P 申报。只接受 Series `S000002839`；文件中应能追到 VOO ETF 股份类别 `C000092055`。不要用 Vanguard 集团的 13F，也不要用标普 500 成分表代替。逐份核对 Series，不假定所有历史文件都由同一个 CIK 提交。
- SEC 访问规范：遵守 SEC 公开的访问要求，请求速率不超过每秒 5 次，带缓存。SEC 要求请求头中带有可识别的 User-Agent（含联系方式）。**这个值必须从 `config/v05_stage2.json` 的 `sec_user_agent` 字段读取，由投资者填写；你不得自行编造或填入任何邮箱或姓名。**该字段为空时，程序明确报错并停止，提示投资者填写。遇到 403 或 429，按退避等待重试，最多 3 次，之后记录失败，不要无限重试。
- 每份名单记录：
  - `holdings_as_of` = 申报中的报告期日期（repPdDate）；
  - `available_at` = EDGAR 记录的受理时间（acceptance datetime，美东时间，转换为 UTC 存储），`availability_precision=timestamp`；
  - 原件路径和 SHA256、accession number、Series 和 Class 标识。
- 名单明细：只保留权益类持仓（例如 assetCat 为股票类别的行）。现金、期货、回购、抵押品等不进入股票池，但要保留在原件里，并在 `holdings_manifest.csv` 中按类别计数。每行保留申报中的原始名称、CUSIP、ISIN、LEI、ticker（如果申报中有）。VOO 原始持仓权重可以保存，但不用于评分。
- 范围：先列出这个 Series 可取得的全部 N-PORT-P 申报（官方结构化入口大约从 2019 年开始）。全部解析，不要只解析两份。如果某些年份取不到，如实记录缺期。
- 在 `work/v05/stage2_snapshot_lag.csv` 中，为每份名单报告：`holdings_as_of`、`available_at`、两者相隔的天数、与上一份名单相比新增和移除的证券数量。这用来量化名单滞后，第三阶段报告会用到。

### B2. 证券身份映射

- 为名单中出现过的每只证券，建立稳定的 `security_id` 和 `issuer_id`，并映射到可以取得行情的代码（富途代码）。映射证据（CUSIP、ISIN、名称、来源）写进 `securities` 表的来源字段，或单独的映射证据表。
- 可以使用的免费映射来源：申报中自带的 ticker、SEC 的公司代码对照表、OpenFIGI 的免费接口（遵守其速率限制）、OpenD 的证券信息。**对不上的证券不要猜**：写进 `work/v05/stage2_identity_issues.csv`，留在名单里，标为身份未确定。
- 改名、换代码的证券（例如历史上的 FB → META）必须映射到同一个 `security_id`，代码按有效期记录，价格序列不能断开或被当成两只证券。
- 证券类型与排除：
  - 标普 500 中约有 30 只 REIT，需要识别并排除。证据可以来自 SEC 申报主体的 SIC 代码（6798）、行业分类和人工核对名单，并保存在 `work/v05/stage2_reit_review.csv` 中；有疑问的单独列出，不要只靠名称关键词判断。
  - 同一发行人有多个股票类别的（例如 GOOGL/GOOG、FOXA/FOX、NWSA/NWS）：建立同一个 `issuer_id`，交给第一阶段已经实现的类别选择规则处理。
  - BRK.A 若出现，排除；BRK.B 正常参与。
- 行业：使用一个固定来源的主行业字段（例如富途行业），记录来源和取得日期，并在结果中标注“当前分类，非历史分类”。查不到的证券记为 `UNKNOWN:<issuer_id>`。批量查询漏返回的证券，要逐只重查一次后再判定为未知。

### B3. 行情与公司行动（OpenD，只读）

- 开始下载前，先查询并记录 OpenD 历史 K 线额度（已用和剩余）。根据 B1 统计出的需要下载的去重证券数（名单中曾出现过的全部证券，加上 VOO，加上评分所需的预热期），判断额度是否足够：
  - 足够：下载全部；
  - 不足：不要超额请求，也不要只挑一部分股票。改为选一个额度能覆盖的、最近的连续区间（例如最近 2～3 年的名单及其成员），在交接说明中写明这一取舍和剩余缺口，然后停下等投资者决定。
- 下载内容：每只证券的**不复权**日 K（开盘、收盘、成交量、成交额），以及公司行动（拆合股、现金分红的除息日、派息日、每股金额）。复权因子只用于核对，不直接用来计算收益。
- 规范化为 events：拆合股写成“每一旧股变成多少新股”（`shares_new_per_old`），并用除权日前后的原始价格做合理性核对，不要直接套用接口中名为 `split_ratio` 之类的字段而不加转换。现金分红写明除息日、派息日、每股金额和金额口径。
- 富途查不到的证券（尤其是已经退市或被收购的历史成员）：不要从名单中删除，写进 `work/v05/stage2_price_gaps.csv`，注明缺失原因（查询返回无效、证券 ID 为 0、超出覆盖范围等）。接口“成功返回”但内容是“未知股票”或证券 ID 为 0 的，按无数据处理。2015-10-12 之前的成交额为零，按缺失处理，不当作真实的零成交。
- 公司行动数据中缺少派息日、金额不明、比例无法核对的事件，写进 `work/v05/stage2_corporate_action_gaps.csv`。
- 记录每次请求的数量、额度变化、失败原因和取得时间。

### B4. NYSE 交易日历

- 优先方案：用 OpenD 的交易日查询得到 session 日期，再根据 NYSE 官方公布的休市和提前收盘日（下午 1 点收盘）建立一个本地文件，文件中注明官方来源 URL 和核对日期。session 的开盘和收盘时间以美东时间生成，再转换为 UTC，夏令时由 `America/New_York` 时区处理，不要写死时差。
- 如果改用免费的日历依赖包，记录包名和版本，并与 OpenD 的交易日做一次交叉核对，差异写进交接说明。
- 不得用“周一到周五”的规则代替日历。

### B5. 覆盖报告与可运行性检查

- 输出 `work/v05/stage2_coverage_report.json`，至少包含：名单份数和日期范围、缺期、去重证券数、身份已确定和未确定的数量、REIT 排除数量、价格覆盖（完整、部分、无数据的证券数）、公司行动缺口数量、历史 K 线额度的使用前后数值，以及**可以运行的最长连续区间**及其理由。
- 新增 CLI 命令 `validate`：读取 `data/v05/normalized/`，执行第一阶段已有的全部输入校验，并对可运行区间内抽取的几个信号周计算评分和选股名单（输出到 `work/v05/stage2_validate/`）。**不要计算或输出权益曲线、策略收益和与 VOO 的比较。**这一步只证明真实数据能被第一阶段的程序正确读入并运行到选股这一步。
- 根据可运行区间，写一份实验登记草稿 `work/v05/stage2_experiment_registry.json`：样本起止日期、按有效信号周大约前 2/3 为开发段、后 1/3 为保留段的具体分界日期、数据版本（规范化文件的 SHA256）、使用的工程默认值（L=20、N=10、k=3、b=2，未验证）。这份登记在查看任何真实收益之前写定，第三阶段只运行开发段。

### B6. 手工抽查

至少抽查 3 份名单：用原件核对其中 5 只证券的名称、CUSIP、`holdings_as_of`、`available_at`，以及解析结果；抽查 3 次拆股或分红：用原始价格核对比例或金额。把抽查结果写进交接说明。

---

## C. 测试与运行

- 新增测试至少覆盖：N-PORT 解析（用保存在 `tests_v05` 里的一小段真实或截取的申报样本，不要在测试中联网）；Series 不符时拒绝；`available_at` 取受理时间而非下载时间；改名证券映射为同一个 `security_id`；退市证券留在名单中并进入缺口报告；拆股比例的规范化与核对；交易日历中的提前收盘；`sec_user_agent` 为空时明确报错。
- 测试不得联网，不得调用 OpenD；联网和 OpenD 只在数据下载命令中使用。
- 实际运行并报告退出码：

```powershell
& 'D:\Tools\miniconda3\python.exe' -m pytest -q tests_v05
& 'D:\Tools\miniconda3\python.exe' -m leadtrend_v05 demo --config config/v05_bootstrap.json --output work/v05/stage1_demo
& 'D:\Tools\miniconda3\python.exe' -m leadtrend_v05 validate --config config/v05_stage2.json --output work/v05/stage2_validate
```

数据下载命令的名称由你决定，在 `docs/README_v0.5.md` 中写明完整命令、需要投资者事先准备的事项（OpenD 已登录并运行、`sec_user_agent` 已填写）以及断点续跑的方法。

---

## D. 交接

完成后保存 `D:\Workplace\LeadTrend\work\v05\stage2_review.md`，包含：

1. A 部分每一项改了哪些文件、新增了哪些测试；第 4 项的回归结果（demo 经济结果是否逐行一致）和规模测试耗时。
2. B 部分实际取得的名单份数、日期范围、去重证券数、身份与价格覆盖情况、额度使用前后数值，以及可运行的连续区间。
3. 实际运行的命令、退出码、测试数量。不要编造未执行的检查。
4. B6 的手工抽查结果。
5. 所有缺口、未解决项和偏离规格之处。
6. 请 Claude 重点审阅的位置。

完成后停止，等待 Claude 审阅。不要开始第三阶段，不要运行完整回测，不要输出任何真实收益数字。

---
