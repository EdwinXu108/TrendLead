# 真实数据近似实验：存在幸存者偏差（历史成员缺行情）、当前行业分类、工程默认参数未经验证；仅为开发段结果，不构成策略有效性的结论

LeadTrend v0.5 第三阶段交接，2026-10-25。请 Claude 优先审阅本文件及独立验收脚本。

## 结论及边界

A部分身份、公司行动评分和分红修复完成，重新登记后，开发段基础和压力情景均运行完成，独立账本验收 **PASS**。没有调整L=20、N=10、k=3、b=2；没有参数网格、附加因子、交易接口或第四阶段。

真实计算只覆盖2019-11-29—2024-06-28的240个开发段信号周，首次成交2019-12-02开盘；最终估值和VOO基准到2024-06-28收盘。**保留段没有计算评分、排名、决策、成交、权益、基准或收益。** A部分仅在保留段按日期、报价有无和事件完整性核查数据可用性；`weekly_coverage.csv` 后121周的“n_scored”是可评分条件数量，不是已计算的评分。`sample_scores.csv`和`sample_decisions.csv`只含开发段。

## A1. 稳定身份修复

共合并 **47对**标识，其中 **29对ISIN/CUSIP**，另18对是原已映射到同一连续报价线的拆合股、更名或标识接续。证券由700条减为653条（含VOO）；规范成员652条，包括39条REIT、2条CVR、611条普通股。合格类型中544条有原始行情，67条无行情；缺行情没有从周度分母删除。

以原始N-PORT中的ISIN/CUSIP交叉字段、发行人连续性、已审阅的同一报价线核对；同一快照同时存在两个代码或类型冲突则拒绝自动合并。对合并行情逐日核对open/close/volume/turnover完全相等后去重，事件也按证券、类型、日期、比例、现金及支付日去重。保留 `stage2_security_aliases.csv` 别名链，原始manifest仍保留原件ID以便追溯，规范化名单只用统一ID。

四个共用 `CUSIP:N/A` 的issuer已修复；没有可靠LEI/CIK时使用该证券完整稳定ID，不再用CUSIP前缀冒充发行人。2019-11-29—2020-05-29共 **27个信号周**原本错误少计3个发行人，现已恢复。2019-11-29至2020年6月底共31个信号周，后4周不再使用那两份早期快照。独立检查638个有来源的shareClassFIGI，跨规范证券重复为0；重复行情代码为0，共用占位issuer为0。

## A2. 分拆估值与窗口排除

原42条供应商识别的分拆/权益分配记录，经身份去重后为34个唯一事件。原23条vendor记录中，17条改用实际子股价格、6条保留vendor；去掉其中两条别名重复后，21个待修唯一事件中16个改用实际价格、5个仍待核。最终34个事件为 **29个child_raw_close、5个vendor_factor**。

5个后备事件如下；代码明确将窗口覆盖事件日的证券周标为 `spinoff_vendor_valuation_in_window`，纳入可修复缺口，窗口走出后可以恢复评分。全样本可用性核查因此排除10个证券周，其中开发段实际评分排除4个。

| ticker | effective_session | child_ticker | raw_close | quote_outcome | source_status |
| --- | --- | --- | --- | --- | --- |
| BFH | 2021-11-08 | LYLT |  | unknown_security | failed |
| BBWI | 2021-08-03 | VSCO |  | unknown_security | failed |
| DELL | 2021-11-02 | VMW |  | unknown_security | failed |
| J | 2024-09-30 | AMTM | 32.25 | ok | failed |
| SKYD | 2026-10-14 | SKYD.WS | 2.5 | ok | ok |

LYLT、VSCO、VMW报价不可取得；J的AMTM报价已取得但本版使用的原件入口失败，保守维持未核实估值及窗口排除，不把它说成报价不存在；SKYD实际为权证分配，缺少本版能核实的适用权利比例，不能按普通股分拆强行估值。后两项都在开发段之后，仅做A部分数据核查，没有运行对应信号或账本。

额外发现LEN供应商记录用了2025-01-21记录日，官方分配日为2025-02-07，已在登记前纠正，并使用当日MRP实际报价与每两股LEN一股MRP的比例。它也不在开发段中。

富途额度由本轮开始时used=596/remain=404到used=605/remain=395，新增9个独立证券；重复运行复用缓存。来源和失败原因见 `stage2_spinoff_price_attempts.csv`，不能把网页失败当报价不存在。

## A3. 六组分红官方核对

全部6组解除阻断：BX 2024-02-02每股0.609+0.331=0.94，官方说明两者为总分配的不同税务组成，均属于同一0.94的经济分配；账户税负仍按用户假设0，不据此推导税法。EXE四组分别为2022-05-18的2.34、2022-08-16的2.32、2022-11-14的3.16、2023-03-06的1.29，基础加浮动分红均真实；HP 2023-08-16为0.25+0.235=0.485。

对应派息日依次为BX 2024-02-12；EXE 2022-06-02、2022-09-01、2022-12-01、2023-03-23；HP 2023-08-31。原复权表不一致不再强行优先于发行人公告。保留两笔现金组成并在相同派息日到账，不重复增加总额。见 `stage2_dividend_official_review.csv` 和更新的70组核对表：64组sum_rehab、6组sum_official、0组one、0组unresolved。

证据保存在 `data/v05/raw/stage3_official/`，包含URL、取得UTC时间及SHA256。BX有PDF原件；HP另取得SEC 8-K原HTML；EXE在直接HTTP403时保存官方网页完整表格的工具提取记录，**不是原HTML字节文件**，此获取限制如实保留。官方主来源：[BX分配说明](https://s23.q4cdn.com/714267708/files/doc_downloads/2024/02/TBG-Memo.pdf)、[EXE官网分红历史](https://investors.expandenergy.com/stock-information/dividends)、[HP发行人SEC公告](https://www.sec.gov/Archives/edgar/data/46765/000004676523000034/hp-20230607.htm)。取得时间仅用于来源审计，不当作当时可交易时间。

## A4. 数据门槛与登记

每周可修复缺口最大 **0.424%**、中位 **0.000%**；可评分成员行业覆盖最低 **100%**。历史无行情比例最大 **12.262%**、中位 **5.945%**。原全历史615/699行业覆盖保留为合并前参考，不能与当前周度门槛混用。

有效周仍为361，边界未变：开发段2019-11-29—2024-06-28（240周）；保留段2024-07-05—2026-10-23（121周）。在首次真实运行之前重新生成规范化全部CSV哈希和默认值登记，旧登记存档并写明A1—A3作废原因。**首次真实运行以来规范化输入和当前登记均未改变**；结果manifest记录登记SHA256，可逐一核对。

历史无行情年度分布如下。后121周只做数据完整性计数，未计算收益：

| 年份 | 周数 | 最小 | 中位 | 最大 |
| --- | --- | --- | --- | --- |
| 2019 | 5 | 12.262% | 12.262% | 12.262% |
| 2020 | 53 | 10.593% | 11.465% | 12.262% |
| 2021 | 52 | 8.421% | 9.514% | 10.828% |
| 2022 | 52 | 6.823% | 7.627% | 8.421% |
| 2023 | 52 | 5.096% | 5.720% | 6.823% |
| 2024 | 52 | 3.602% | 4.459% | 5.096% |
| 2025 | 52 | 1.274% | 2.548% | 3.602% |
| 2026 | 43 | 0.212% | 0.637% | 1.274% |

## B. 实现和实际支持范围

新增 `leadtrend_v05/stage3.py` 开发段入口、`stage3_scoring.py`共用受限评分、`stage3_ledger.py`分项资产、`stage3_metrics.py`指标；新增 `config/v05_stage3.json`、`tests_v05/test_stage3.py`。`selection.py`新增冻结数/发行人/行业剩余容量，`execution.py`新增可配置名额参数，默认调用保持第一阶段行为；`__main__.py`增加`stage2-repair`和`run-dev`。A修复在`stage2_repair.py`，`stage2_validate.py`增加开发段评分边界和保留段仅可用性检查；两个验收脚本和源核对适配稳定ID。规格、AGENTS、旧原型和根README均未修改，`sec_user_agent`值哈希核对不变。

账本分别记录可交易股票、冻结股票、可用现金、应收现金及应收证券。评分TRI仅用于排名，绝不增加账本现金。除息前持仓确认应收，按已核实派息日到账；交易日拆股先换股数再下单。分拆代码把新股列入实际持仓/行业暴露，不能评分且不具成员资格的新股在下周按资格退出；未核实零碎股变现款保留为受限证券权益，以可观察新股价格估值，不编造现金或到账日期。**尚未核实的部分交付**若同时存在实际整股和应收零碎股，合并只占一名额，并在下一次周度配置前显式停止，保存全部权益，等待核实处置及到账；本版不擅自选择卖整股或冻结整股来绕过容量问题。该限制本次真实运行未触发，合成测试确认停止而不删除资产。

冻结股占总名额/行业名额，补位排除冻结发行人；超限先校正行业再总数量，冻结达到N不新增，超N被动超限保留资产。有换股时可交易部分以 **可配置资金÷(N−冻结数)** 为目标金额；应收及冻结价值不进入可配置资金。无换股不交易。下一次执行日重读原始开盘报价，整股、先卖后买、每单1美元、不利滑点；报价可观测且成交可得是理想化研究假设，未声称实盘能以该价成交。

**本次真正持有时只遇到138个现金分红和3个拆股**。基础情景股数为：AAPL 2020-08-31 16→64股；ANET 2021-11-18 21→84股；NVDA 2024-06-10 9→90股。压力情景股数详见其独立的`corporate_actions.csv`，不能假定成本变化后数量相同。未实际持有跨越分拆、现金收购、换股并购、停牌或退市；实际冻结/被动超限为0。故这些路径不能宣称已被真实事件验证。分拆、冻结、受限权益和未知断点由独立合成测试覆盖；没有为了凑样本增加真实交易。现金收购/换股交割没有实际触发，未构造通用并购框架，未来若持有时遇到未核实事件会明确停止。

未知的已持有行情断点会保存最后可解释状态，保留资产，返回2，不输出完整收益结论；测试实际构造该情况确认不是删除资产后继续跑。本次两情景没有未能处理的**已持有事件**，所以完成到开发段终点。

## 开发段结果

只改变滑点5→10bp/边，其余参数、日期、规范化数据、名单、评分、决策和VOO基准一致；四类共同文件SHA256逐字节相同。压力情景因为股数取整及净额调整，交易笔数/固定费用可以不同。

| 指标 | 5bp基础 | 10bp压力 |
| --- | --- | --- |
| 总收益 | 2.361% | -7.496% |
| 年化收益 | 0.512% | -1.690% |
| VOO总回报 | 85.741% | 85.741% |
| 相对VOO累计差异（百分点） | -83.381 | -93.238 |
| 相对净值累计变化 | -44.891% | -50.198% |
| 年换手：双边成交额/平均权益（倍/年） | 43.596 | 43.534 |
| 年换手：上述单边口径（倍/年） | 21.798 | 21.767 |
| 交易笔数 | 3081 | 3052 |
| 手续费合计（美元） | 3081.00 | 3052.00 |
| 滑点成本合计（美元） | 9349.70 | 17787.38 |
| 低于500美元交易笔数 | 814 | 812 |
| 小单手续费（美元） | 814.00 | 812.00 |
| 小单滑点成本（美元） | 96.60 | 191.41 |
| 完整持有区间平均周数 | 2.385 | 2.385 |
| 含期末未平仓区间的平均周数 | 2.381 | 2.381 |
| 滚动52周相对收益连续为负最长周数 | 54 | 134 |
| 最大回撤 | -41.081% | -43.416% |
| 最长策略水下期（自然日，期末未恢复则为已观察长度） | 1296 | 1296 |
| 相对净值从峰值至恢复/终点最长自然日 | 1563 | 1563 |

基础末值 **$102,360.81**，压力末值 **$92,503.83**（本金$100,000）。最大回撤峰值2020-12-10、谷底2022-10-12，两情景至2024-06-28均未恢复，谷底至恢复时间应为未完成，不能填0。相对净值从高点至恢复的最长时间，在期末未恢复时报告已观察下限，并标记未恢复；滚动52周相对收益按 `(策略52周倍数)/(VOO52周倍数)-1` 的符号计算，包含重叠窗口。

持有时长按首次建仓至彻底退出的一段持有期计算，部分增减不重置，期末开放区间作为右删失另列。换手同时报告双边和减半单边口径，分母为平均日权益，年数从首次成交到终点。贡献按每证券每日原始市值、应收及实际现金流归集，费用归属于证券；分拆若发生则以子股价值作为母子间转移，避免把分拆赠股当作凭空收益。全部证券贡献与期末损益对账误差见验收。

基础情景最大5个正贡献如下（是收益百分点，非权重；其余证券可抵消这些收益）：

| ticker | security_id | 贡献收益百分点 |
| --- | --- | --- |
| FANG | CUSIP:25278X109 | 9.5952 |
| NVDA | CUSIP:67066G104 | 8.1366 |
| HAL | CUSIP:406216101 | 6.5657 |
| BBWI | CUSIP:070830104 | 4.9790 |
| TPR | CUSIP:876030107 | 4.2308 |

压力情景最大5个正贡献：

| ticker | security_id | 贡献收益百分点 |
| --- | --- | --- |
| FANG | CUSIP:25278X109 | 9.2617 |
| NVDA | CUSIP:67066G104 | 7.2193 |
| HAL | CUSIP:406216101 | 6.2722 |
| BBWI | CUSIP:070830104 | 4.8410 |
| TPR | CUSIP:876030107 | 4.0619 |

## 代表周：为什么买卖

选取与收益高低无关的三个周：首次建仓2019-11-29、首次真实拆股所在周2020-09-04、首个无换股周2021-08-06。本次无真实分拆、冻结或被动超限周，因此没有虚构此类代表周。

逐只股票的排名、缓冲保留线、行业计数、持仓名额和决定理由见 `stage3_dev_run/representative_weeks.csv`；完整未选证券和跳过原因在`decisions.csv`，全部评分在`scores.csv`。其中`rank`为原始得分排名，保留线取考虑行业容量的F，不可直接把rank≤20当作保留规则。

### 2019-11-29

| ticker | rank | industry | action | reason | buffer_score |
| --- | --- | --- | --- | --- | --- |
| ADSK | 4 | US_LIST2470 | fill | fill_unheld_candidate | 0.14286505286816986 |
| DXC | 1 | US_LIST2252 | fill | fill_unheld_candidate | 0.14286505286816986 |
| DVA | 5 | US_LIST2486 | fill | fill_unheld_candidate | 0.14286505286816986 |
| FTNT | 2 | US_LIST2508 | fill | fill_unheld_candidate | 0.14286505286816986 |
| NKTR | 9 | US_LIST2069 | fill | fill_unheld_candidate | 0.14286505286816986 |
| REGN | 7 | US_LIST2069 | fill | fill_unheld_candidate | 0.14286505286816986 |
| SCHW | 6 | US_LIST2260 | fill | fill_unheld_candidate | 0.14286505286816986 |
| SPB | 3 | US_LIST2003 | fill | fill_unheld_candidate | 0.14286505286816986 |
| TGT | 10 | US_LIST2240 | fill | fill_unheld_candidate | 0.14286505286816986 |
| CPRI | 8 | US_LIST2276 | fill | fill_unheld_candidate | 0.14286505286816986 |

### 2020-09-04

| ticker | rank | industry | action | reason | buffer_score |
| --- | --- | --- | --- | --- | --- |
| DAL | 15 | US_LIST2090 | retain | retained_above_buffer | 0.14756446991403993 |
| FDX | 7 | US_LIST2500 | retain | retained_above_buffer | 0.14756446991403993 |
| GAP | 5 | US_LIST2494 | retain | retained_above_buffer | 0.14756446991403993 |
| LYV | 10 | US_LIST2491 | fill | fill_unheld_candidate | 0.14756446991403993 |
| MGM | 6 | US_LIST2046 | retain | retained_above_buffer | 0.14756446991403993 |
| MAR | 32 | US_LIST2263 | exit | below_buffer_line | 0.14756446991403993 |
| MOS | 60 | US_LIST2010 | exit | below_buffer_line | 0.14756446991403993 |
| PVH | 8 | US_LIST2049 | fill | fill_unheld_candidate | 0.14756446991403993 |
| PWR | 31 | US_LIST2033 | exit | below_buffer_line | 0.14756446991403993 |
| CRM | 4 | US_LIST2470 | retain | retained_above_buffer | 0.14756446991403993 |
| CCL | 3 | US_LIST2498 | fill | fill_unheld_candidate | 0.14756446991403993 |
| NCLH | 2 | US_LIST2498 | retain | retained_above_buffer | 0.14756446991403993 |
| RCL | 1 | US_LIST2498 | retain | retained_above_buffer | 0.14756446991403993 |

### 2021-08-06

| ticker | rank | industry | action | reason | buffer_score |
| --- | --- | --- | --- | --- | --- |
| AMD | 3 | US_LIST2015 | retain | retained_above_buffer | 0.12257465683737023 |
| ALB | 1 | US_LIST2068 | retain | retained_above_buffer | 0.12257465683737023 |
| CARR | 10 | US_LIST2483 | retain | retained_above_buffer | 0.12257465683737023 |
| CMG | 6 | US_LIST2225 | retain | retained_above_buffer | 0.12257465683737023 |
| DXCM | 8 | US_LIST2280 | retain | retained_above_buffer | 0.12257465683737023 |
| HCA | 13 | US_LIST2486 | retain | retained_above_buffer | 0.12257465683737023 |
| MPWR | 4 | US_LIST2015 | retain | retained_above_buffer | 0.12257465683737023 |
| RVTY | 5 | US_LIST2011 | retain | retained_above_buffer | 0.12257465683737023 |
| WST | 14 | US_LIST2014 | retain | retained_above_buffer | 0.12257465683737023 |
| YUM | 17 | US_LIST2225 | retain | retained_above_buffer | 0.12257465683737023 |

## B7 独立验收实际输出

脚本直接读取CSV产物和规范化原始报价，独立重放股数、除息应收、到账、现金和成交，不读取评分TRI来记现金；并独立从首次成交开盘构建VOO。以下为机器验收关键数字原样摘录（非舍入后的宣传指标）：

```json
{
  "status": "PASS",
  "scenarios": [
    {
      "status": "PASS",
      "scenario": "stage3_dev_run",
      "daily_asset_identity_max_error": 0.0,
      "independent_cash_receivable_replay_max_error": 5.684341886080802e-14,
      "independent_share_replay_max_error": 0.0,
      "independent_position_valuation_max_error": 2.9103830456733704e-11,
      "dividend_entitlement_max_error": 0.0,
      "trade_price_fee_slippage_cash_max_error": 0.0,
      "cash_minimum": 101.80515785024764,
      "frozen_actual_position_observations": 0,
      "frozen_sales": 0,
      "frozen_share_changes": 0,
      "event_applications": 141,
      "duplicate_event_applications": 0,
      "held_split_events": 3,
      "split_share_conservation_max_error": 0.0,
      "split_mechanical_value_max_error": 0.0,
      "held_spinoff_components": 0,
      "spinoff_visibility": "NOT EXERCISED in real run; synthetic tests cover it",
      "held_cash_dividend_events": 138,
      "no_swap_weeks": 5,
      "trades_in_no_swap_weeks": 0,
      "benchmark_start": "2019-12-02",
      "first_trade": "2019-12-02",
      "benchmark_end": "2024-06-28",
      "independent_benchmark_max_error": 0.0,
      "latest_economic_artifact_date": "2024-06-28",
      "holdout_rows": 0,
      "return_contribution_reconciliation_error": 5.3660187404602766e-11,
      "weekly_missing_history_report_mismatches": 0,
      "stage2_stage3_score_max_rounding_difference": 8.72484307024024e-11,
      "manual_trade_checks": [
        {
          "session_date": "2019-12-02",
          "security_id": "CUSIP:052769106",
          "side": "buy",
          "shares": 55.0,
          "raw_open": 180.69,
          "fill_price_computed": 180.78034499999998,
          "fee": 1,
          "cash_delta_computed": -9943.918974999999,
          "cash_after_computed": 90056.081025,
          "maximum_error": 0.0
        },
        {
          "session_date": "2019-12-02",
          "security_id": "CUSIP:23355L106",
          "side": "buy",
          "shares": 267.0,
          "raw_open": 37.35,
          "fill_price_computed": 37.368674999999996,
          "fee": 1,
          "cash_delta_computed": -9978.436225,
          "cash_after_computed": 80077.64480000001,
          "maximum_error": 0.0
        },
        {
          "session_date": "2019-12-02",
          "security_id": "CUSIP:23918K108",
          "side": "buy",
          "shares": 139.0,
          "raw_open": 71.78,
          "fill_price_computed": 71.81589,
          "fee": 1,
          "cash_delta_computed": -9983.40871,
          "cash_after_computed": 70094.23609,
          "maximum_error": 0.0
        },
        {
          "session_date": "2019-12-09",
          "security_id": "CUSIP:23918K108",
          "side": "sell",
          "shares": 139.0,
          "raw_open": 72.56,
          "fill_price_computed": 72.52372000000001,
          "fee": 1,
          "cash_delta_computed": 10079.797080000002,
          "cash_after_computed": 10297.175700000009,
          "maximum_error": 0.0
        }
      ],
      "failures": []
    },
    {
      "status": "PASS",
      "scenario": "stress_10bps",
      "daily_asset_identity_max_error": 0.0,
      "independent_cash_receivable_replay_max_error": 2.2737367544323206e-13,
      "independent_share_replay_max_error": 0.0,
      "independent_position_valuation_max_error": 2.9103830456733704e-11,
      "dividend_entitlement_max_error": 0.0,
      "trade_price_fee_slippage_cash_max_error": 0.0,
      "cash_minimum": 86.82444580020501,
      "frozen_actual_position_observations": 0,
      "frozen_sales": 0,
      "frozen_share_changes": 0,
      "event_applications": 141,
      "duplicate_event_applications": 0,
      "held_split_events": 3,
      "split_share_conservation_max_error": 0.0,
      "split_mechanical_value_max_error": 0.0,
      "held_spinoff_components": 0,
      "spinoff_visibility": "NOT EXERCISED in real run; synthetic tests cover it",
      "held_cash_dividend_events": 138,
      "no_swap_weeks": 5,
      "trades_in_no_swap_weeks": 0,
      "benchmark_start": "2019-12-02",
      "first_trade": "2019-12-02",
      "benchmark_end": "2024-06-28",
      "independent_benchmark_max_error": 0.0,
      "latest_economic_artifact_date": "2024-06-28",
      "holdout_rows": 0,
      "return_contribution_reconciliation_error": 2.8194335754960775e-11,
      "weekly_missing_history_report_mismatches": 0,
      "stage2_stage3_score_max_rounding_difference": 8.72484307024024e-11,
      "manual_trade_checks": [
        {
          "session_date": "2019-12-02",
          "security_id": "CUSIP:052769106",
          "side": "buy",
          "shares": 55.0,
          "raw_open": 180.69,
          "fill_price_computed": 180.87068999999997,
          "fee": 1,
          "cash_delta_computed": -9948.887949999998,
          "cash_after_computed": 90051.11205,
          "maximum_error": 0.0
        },
        {
          "session_date": "2019-12-02",
          "security_id": "CUSIP:23355L106",
          "side": "buy",
          "shares": 267.0,
          "raw_open": 37.35,
          "fill_price_computed": 37.38735,
          "fee": 1,
          "cash_delta_computed": -9983.42245,
          "cash_after_computed": 80067.6896,
          "maximum_error": 0.0
        },
        {
          "session_date": "2019-12-02",
          "security_id": "CUSIP:23918K108",
          "side": "buy",
          "shares": 139.0,
          "raw_open": 71.78,
          "fill_price_computed": 71.85177999999999,
          "fee": 1,
          "cash_delta_computed": -9988.39742,
          "cash_after_computed": 70079.29218,
          "maximum_error": 0.0
        },
        {
          "session_date": "2019-12-09",
          "security_id": "CUSIP:23918K108",
          "side": "sell",
          "shares": 139.0,
          "raw_open": 72.56,
          "fill_price_computed": 72.48744,
          "fee": 1,
          "cash_delta_computed": 10074.75416,
          "cash_after_computed": 10242.271400000012,
          "maximum_error": 0.0
        }
      ],
      "failures": []
    }
  ],
  "base_and_stress_same_universe_scores_decisions_benchmark": {
    "universe": true,
    "scores": true,
    "decisions": true,
    "benchmark": true
  },
  "note": "zero actual frozen/spinoff observations are reported as unexercised, not empirical proof of those paths"
}
```

资产恒等式、独立现金/股数重放及分红归属均通过；冻结/分拆的真实观测数为0时明确记NOT EXERCISED，不能用“0个违规”充当真实事件证明。至少3笔买卖的原价、股数、1美元费用、不利滑点及前后现金已重算，表中误差为0；基准起点、终点和全部经济记录日期都通过检查。来源的取得日期和审计时间可晚于2024年，它们是溯源元数据，不是保留段交易或权益。

## 实际命令与退出码

本轮首次A修复用等价Python调用，CLI后补为`stage2-repair`；未在看过收益后改变数据或参数。各命令详情和标准输出保存在`work/v05/stage2*_before_stage3.log`、`stage2_acceptance_final.log`、`stage3_*command.log`、`stage3_pytest_final.log`。

| 命令 | 退出码 | 结果 |
| --- | ---: | --- |
| stage2-repair 等价调用 | 0 | A1—A3完成，原始响应只追加 |
| validate --config config/v05_stage2.json | 0 | A4登记在首次真实运行之前生成，保留段不计算评分 |
| stage2_acceptance/run_acceptance.py | 0 | 原9项及新增身份/分红/窗口门槛通过 |
| stage2_acceptance/audit_sources.py | 0 | 源快照和追加原件哈希检查通过 |
| pytest tests_v05 | 0 | 160 passed |
| synthetic demo | 0 | 原第一阶段经济文件SHA256全部保持不变 |
| run-dev（5bp） | 0 | 完整开发段 |
| run-dev --stress（10bp） | 0 | 同参数完整开发段 |
| stage3_acceptance/run_acceptance.py | 0 | 两情景独立数据重放PASS |
| run-dev --end-date 2024-07-05 | 2（预期） | 明确holdout protection报错，输出目录不存在 |

新增测试覆盖身份连续/冲突、供应商窗口出入、冻结占位/行业/发行人/超限、可配置金额分母、分拆新股与零碎股可见、同日合股权益、分红到账幂等、无换股不交易、持仓未知断点停机、配置/命令行超范围、实际小闭环产物无保留段日期、评分引擎不构建保留段TRI。旧139项继续通过。开发段入口先查登记和截止日，再载入数据，并物理截断行情/事件/日历；未来结束日期在载入前被拒绝。

复现：保留冻结A基线`stage2_before_stage3/`及所有raw，运行`stage2-repair`→`validate`→`finalize_before_stage3.py`→第二阶段验收→`run-dev`→`run-dev --stress`→第三阶段验收→本报告生成器。不要把旧`stage2-enrich`或上一轮报告脚本混到当前修复之后。已查看本次开发段结果，此后的任何数据/策略修改须另记修订，不能伪装成未见结果的首次实验。

## 请Claude重点审阅

1. 47对身份连续性与四个issuer修复，尤其早期跨ISIN及后续拆合股/改名接续；源标识没有从原件删除。638个可核对FIGI检查不是对所有未映射证券的身份保证。
2. 29个实际子股估值、5个保守窗口排除，以及LEN记录日/分配日纠正；EXE网页提取证据与HP原始8-K；未把供应商不一致强行调成同值。
3. 账本与评分分离、执行日拆股换算、除息日前持股归属、成本及非负现金；真实事件清单只有138个分红和3个拆股，其他路径目前没有实证覆盖。
4. 冻结/应收资产不释放可配置现金、N减冻结数的容量/金额分母、受限零碎股权利估值假设，以及未知行情断点确实保留资产并停机。
5. 两种最长相对落后定义、右删失恢复时间、换手口径和证券贡献归集；结果表现较弱，不能据此临时改变参数、剔除年份或提前查看保留段。
6. 先登记再真实运行的SHA256记录、holdout保护及实际拒绝测试。

完成后停止，等待Claude审阅；没有运行保留段、参数网格或第四阶段。
