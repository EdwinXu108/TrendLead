# LeadTrend 数据库 v2：各季度策略可用 Top 200 股票池（rule_version top200_v1）

> 历史版本。当前实现与修正后的验收口径见 `LeadTrend_数据库_v3_Top200.md`。本文件中的 ready、证券联集、阻断和测试数字不再代表当前正式库。

本文档是当前有效的数据库说明。它取代 `docs/LeadTrend_数据库_v1.md` 的口径（v1 文档已加更正说明）。数字以数据库、
`data/db/exports/top200_v1/` 的 CSV 与 `research/db_v1/summary.md`（只含计数）为准；核对明细见
`work/top200_v1/checks.json`（构建过程文件，不进库）。

## 1. 目标与名称

数据库回答三个问题：每个季度 VOO 所属基金（Vanguard 500 Index Fund）按持仓市值排名前 200 的是哪些公司；这些公司在
当季持有哪些符合投资者证券边界的股票类别；名单何时公开、在构建日是否仍可用于策略。

准确名称：**符合投资者证券边界的 VOO 公司持仓市值 Top 200**。它不是标普 500 市值前 200，也不是官方指数，也不按
当前市值排序。

## 2. 排名规则（rule_version top200_v1，见 `leadtrend_db/rules.py`）

1. **单位是公司。** 同一公司的多个类别合并计算金额，只占一个名额（如 GOOG/GOOGL）。选中后输出该季度基金实际持有、
   符合边界的各类别。证券数因此多于 200。
2. **先定边界，后取前 200。** 只有 `common`（普通股）和 `adr`（ADR，包括权益性质的优先股 ADR，按发行人股票类别）
   计入公司金额。REIT、合伙单位、ETF、SPAC、固定收益性质优先证券、权利、权证、CVR、其他不计入。REIT 由明确证据判定
   （OpenFIGI 类型、v05 证券类型、标题；SIC 6798 为辅助），不靠公司名称。名称含 Partners 或 preferred 不单独排除。
3. **金额用原始持仓。** 公司金额 = 当季该公司符合边界的股票行 `value_usd` 之和，单位为整数美分（`amount_cents`）。
   不用当前市值、当前 ticker，也不预先按现行存续状态过滤。负数行、非股票行不进入金额。
4. **排序与并列。** 金额从大到小；金额相同的公司标记 `tie_group=1`，顺序按稳定的 `issuer_id`（CIK）。第 200 名与第 201
   名的金额与身份写入 `quarter_checks` 与 `rank_boundary_180_220.csv`。
5. **BRK.A 不能买，BRK.B 可以。** 公司排名不因此排除。BRK.A（CUSIP 084670108）在 `top200_securities` 中标为
   `candidate_eligible=0`，原因写明“investor boundary”。
6. **原始权重与候选内权重分开。** `fund_weight_raw` 为金额除以基金当季投资总额；`pct_val_percent` 为 N-PORT 原始
   pctVal（占净资产的百分比，0–100）；`stock_weight` 为股票行金额除以当季正股票金额合计（负数与非股票不计）。
   候选池内权重不是策略权重，本版不设组合权重规则。

## 3. 时间规则（何时可知）

- `holdings_as_of`：持仓日期（季度末）。
- `available_at_utc`：包含该份完整持仓的申报的 SEC 受理时间（acceptanceDateTime）。它是名单的公开时间。
- `retrieved_at_utc`：本次取得文件的时间，不代表公开时间，不用于查询。
- 只有完整、核对通过的申报才形成版本（`pool_versions`）。每个报告期可有多个版本（如 2019-12-31 同时有 N-CSR 与
  N-PORT）。`is_canonical=1` 为该期用于历史名单的核对版；其他为替代或并存的版本，仍有完整排名，可用于时间查询。
- **查询规则**：对 UTC 时间 T，先取 `available_at_utc <= T` 的版本；取其中报告期最近的一期（较早期的晚到修订不替代
  已公开的新期）；该期内取 T 之前最后公开的版本。首份名单公开前返回“尚无可用名单”。下一期名单公开前，上一份仍有效。
- **构建截止**：只使用受理时间不晚于 `2026-10-27T00:00:00Z` 的申报（`rules.BUILD_CUTOFF_UTC`）。

查询示例（命令行，只读）：

```bash
python -m leadtrend_db query --quarter 2019-12-31
python -m leadtrend_db query --as-of 2020-03-15T09:59:59Z
```

SQL 等价写法（版本选择）：

```sql
SELECT version_id, report_period, available_at_utc, is_canonical, quarter_state
FROM pool_versions
WHERE available_at_utc <= '2020-03-15T09:59:59Z'
ORDER BY report_period DESC, available_at_utc DESC, version_id DESC
LIMIT 1;
```

## 4. 历史名单与策略名单（两份，不混）

- **historical_top200**：只按当季持仓、当季发行人身份和证券边界选出。`top200_companies` 与 `top200_securities`，
  不因今天是否退市而改变排名。
- **strategy_top200**：在历史名单上应用构建日 `status_cutoff = 2026-10-26`（`rules.STATUS_CUTOFF`）的存续状态。
  当前退市（`delisted`）的公司写入 `excluded_reason`，不补位，也不从第 201 名起补充。退市但仍有未解决的继任候选
  （核对清单中的 `successor_candidate`，即退市前后 120 天内有 8-K12B 而名称不能确定）的公司不计为退市排除，写入
  `blocked_reason`，因为其继任身份未能排除；未知或需复核的公司同样写入 `blocked_reason`。被阻断的成员保留在名单中但不可选。`strategy_companies` 每期一行历史成员；`current_status`、
  `resolved_current_issuer`、`current_ticker` 来自现行发行人（继任链之后），与历史代码分开。
- 策略名单的状态截止只是构建日的过滤，不是历史当时已经知道的事实。

## 5. 身份模型

- **按持仓日期确定发行人。** 每一行持仓的发行人依据该行的持仓日期判定，不沿用同一证券键第一次出现时的名称。
  `holdings.issuer_id` 为该日的发行人；`security_issuer_spans` 记录证券在各时段对应的发行人。
- **发行人必须存在。** 候选 CIK 须在持仓日期前后有定期报告（10-K/10-Q/20-F/40-F，报告期在 95 天之内），并且不早于其
  后继注册日（其 8-K12B 的受理日，见下），也不晚于由覆盖表给出的前身失效日。
- **名称比对。** 名称规范化只去除法律形式、SEC 州别后缀（`\DE\`、`/DE/`、`/ MA`）、撇号、`PLC`/`PUBLIC LTD CO`、`NV`/`N.V.`；
  其余词保留（`HOLDING`、`GROUP` 不去除）。同一词序不同的名称（CR Bard 与 BARD C R）可作为候选，但仍须通过有效期检查。
  当名称签名相同而有多个候选时，以原样全名（保留法律形式）唯一匹配为准。
- **曾用名的生效期。** 曾用名在其结束日之后 45 天内仍可能出现在基金报告中（`NAME_LAG_DAYS`）。当前名称的生效日只取同名的
  曾用名结束日，不取无关实体的曾用名（修复 Invesco 的问题）。
- **证券证据优先于名称。** v05 证券记录中的 SEC 佐证 CIK（`SEC ticker corroboration CIK`）在该 CIK 于持仓日期存在时采用；
  名称不一致不推翻证券身份证据（修复 APA、Wabtec、U.S. Bancorp 等）。
- **同一证券的传播。** 同一 CUSIP（或同一名称与类别）在相邻日期的发行人一致时，未能直接判定的行采用相邻日期的发行人；
  发行人在相邻日期不同时，只在后继注册日之前采用旧发行人（`cusip_propagated_*`）。
- **继任。** 后继注册的依据是后继人的 8-K12B（Rule 12g-3）。只有在该 CIK 此前并未以自身名义定期报告时，其首个 8-K12B 才
  计为继任（排除 Cisco 2021 的情形）。人工链接写入 `config/db_overrides_v1.csv`，附理由与证据：
  Walgreen（2014-12-31）、Medtronic（2015-01-27）、Google→Alphabet（2015-10-02）、BlackRock Finance→BlackRock Inc（2024-10-01）、
  Paramount→Skydance（2025-08-07）、ExxonMobil→ExxonMobil Holdings（2026-07-01，8-K12B accession 0001193125-26-291990，
  已归档 `data/db/raw/evidence/xom_8k12b_2115436.htm`）、E.I. du Pont 与 Dow→DowDuPont（2017-08-31，8-K12B 已归档）。
  历史持仓保留当时的 CIK；现行名单另给出继任者。
- **证券的结束。** `security_end` 覆盖：California Resources 的旧股在 2020-10-27 注销，新股自 2020-10-28 起交易，二者不是同一证券
  （`CIKCLS:0001609253:COM` 结束于 2020-10-27，附 SEC 新闻稿证据）。
- **代码。** v05 证券代码（格式检查通过者）优先；没有时，若该发行人在 SEC 列表中只有一个不含连字符的代码，且证券为普通股或 ADR，
  则采用该代码（不限于 COM 类别）。历史起止日未知的代码 `valid_from` 留空，不猜测。

## 6. 原始申报的解析与核对

- **金额位置。** 股票行的金额为名称之后的数字单元格，取最后两个为股数与市值（千美元）。一位、两位数的市值是真实金额，
  不当作脚注（Halyard 6、News Corp 72、Wyndham 10）。破折号 `—` 为申报的零值，行保留。括号为负数，单列为 `negative_rows`。
- **跨行名称。** 公司名可跨多行，连续无金额的名称部分全部拼接（Fidelity National / Information Services / Inc.）。
- **核对。** 每份 htm 申报的股票行与负数行之和，与申报的 Total Common Stocks 之差（residual）须在“每行至多半个千美元”的舍入界
  之内，且无未解析行（`unparsed_rows=0`）。全部 24 份 htm 申报通过。
  残差（residual）逐期记录于 `snapshots.residual_usd`；2018-03-31 为 −1,000 美元，在舍入界之内。
  2018-12-31 的 82,000 美元差额已由两个漏行（News Corp 72,000、Wyndham 10,000）解释，不是舍入。

## 7. 季度结果

51 个季度（2013-12-31 至 2026-06-30），每季一个核对版（canonical），共 52 个版本（2019-12-31 另有一份 N-CSR 版本）。

- **ready：45 个季度。** 成员排名的边界条件成立（见第 8 节），并无身份冲突。
- **blocked：6 个季度。** 2019-06-30、2021-12-31、2022-03-31、2022-06-30、2022-09-30、2022-12-31。原因：未能判定身份的股票行，
  其合计金额超过第 200 名与第 201 名之间的差距，而这些行没有任何候选 CIK（见第 9 节）。

每季的公司数均为 200；历史证券行（canonical）10,301 行，其中符合候选边界的 10,268 行；策略名单中可选公司 9,560 行
（每季 164–200 家；历史名单中未入选的 640 个公司季度行全部有原因：明确退市排除 29 行，退市但继任候选未定而阻断 611 行）；
策略名单去重后的可选证券为 332 只（`security_union.csv`，每只证券注明所在季度与发行人）。

## 8. 边界与核验

- `rank_boundary_180_220.csv` 输出各 ready 季度第 180–220 名（金额、并列、未解析行数、候选金额上界）。
- `quarter_checks.csv` 每版每项检查：`parse_reconciled`、`ranked_companies_200`、`boundary_200_201_distinct`、
  `membership_bound`、`strategy_security_single_company`。
- **候选边界条件。** 对每个不在前 200 的公司 c，若存在未解析行把 c 列为可能发行人，则要求 `m_c + U_c < A200`
  （m_c 为 c 已解析金额，U_c 为把 c 列为候选的未解析行金额之和，A200 为第 200 名金额）。无候选的未解析行合计
  必须小于 `A200 − A201`。不满足即 blocked。
- **独立重算。** `checks.py` 用 SQL 从 `holdings` 重算每个核对版的公司金额、排序、第 200 名与第 201 名，并与
  `company_ranks`、`top200_companies` 比对（0 处不一致）；候选金额与 SQL 重算一致（0 处不一致）。
- **2019-12-31 两个来源。** N-CSR 与 N-PORT 按同一公司口径比较，200 家公司完全相同，名次全部相同。金额差异来自申报本身：
  同一日期的股数差约 0.3%（如 Apple：N-CSR 83,444,529 股，N-PORT 83,192,032 股），最大金额差 74.1 百万美元。
  逐家差额见 `bridge_2019_12_31_company_compare.csv`。
- **v05 对照。** 28 个 N-PORT 快照中，与 v05 共同的 CUSIP 共 14,135 个，占 N-PORT 原始 pctVal（百分比）的差异为 0；
  v05 独有 40 行（见 `checks.json` 的 `v05`）。v05 的权重单位也是百分比，与本库 `pct_val_percent` 相同。

## 9. 未解决项（只列实际存在的）

未解析的股票行共 58 行：普通股 42 行，合计 132.2 亿美元（以持仓金额计）；另有 16 行为 CVR（Hologic 与 ABIOMED），
已按类型排除，金额可忽略。普通股 42 行按原因如下：

- **First Republic Bank**，19 行，约 81.8 亿美元：2018-12-31 至 2023-03-31 的持仓。该银行向 FDIC 而非 SEC 申报，
  候选 CIK（1097256、1132979）在这些日期没有可核验的定期报告，身份不能确定。
- **Signature Bank**，6 行，约 12.8 亿美元（2021-12-31 至 2023-03-31）：无 SEC 发行人记录，没有候选。
- **Bunge Ltd**，3 行，约 10.6 亿美元（2023-03-31 至 2023-09-30）：2023 年迁册为 Bunge Global SA；缓存中没有其 8-K12B，
  未建链接，原候选 1144519 在该日已不成立。
- **Level Communications Inc.**，4 行，约 7.6 亿美元（2014-12-31 至 2016-12-31）：报告名称与 SEC 名称表中的任何名称都不相同，
  未建立身份；只有名称相似的候选用于边界计算。
- **Wabtec Corp.**，3 行，约 7.1 亿美元（2019 年）：报告名称与 SEC 在该日的名称不一致，候选 943452 的名称检查未通过。
- **Discovery Communications Inc. Class A**，6 行，约 5.3 亿美元（2018-06-30 至 2019-12-31）：报告仍使用 2018 年 3 月前的旧名，
  与 CIK 1437107 的名称有效期不符；另一候选 1024333 在该日没有定期报告。
- **Fisery Inc.**，1 行，约 7.0 亿美元（2019-06-30）：报告名称拼写有误（应为 Fiserv），无 CUSIP，无候选。

这些行仍保留在原始记录中，并列入 `unresolved_lines.csv`。受影响的季度见第 7 节；`blocking_review.csv` 列出每个 blocked 季度的
候选公司与无候选组的金额。

## 10. 核对结果汇总

- 档案：2,607 个原始文件，全部与清单的 sha256 与字节数一致；清单前缀与基线一致（只追加）。
- 外键违规 0；SQLite 完整性检查通过；预留的价格与公司行动表为空；无空标识。
- 离线重建：内容哈希与构建结果一致；网络请求 0（见 `checks.json` 的 `rebuild`）。
- 测试：`tests_db` 53 项，`tests_v05` 快速测试 369 项，合计 422 项通过、7 项跳过（测试临时目录位于 `work/`）。

## 11. CSV 字段（`data/db/exports/top200_v1/`，UTF-8，按报告期与名次排序）

| 文件 | 内容 |
|---|---|
| `pool_versions.csv` | 每个版本：报告期、受理时间、表格、完整性、核对状态、是否核对版、替代关系、ready/blocked 与原因 |
| `historical_top200_companies.csv` | 每个版本前 200 名：名次、CIK、当期持仓名称、现行名称、金额（美元与美分）、并列标记 |
| `historical_top200_securities.csv` | 入选公司的证券：类别、证券类型、v05 代码（非日期代码）、持仓行数、金额、是否候选、原因、快照与来源 |
| `strategy_top200_companies.csv` | 历史名次、构建日状态、现行发行人、现行代码、是否可选、阻断或排除原因 |
| `strategy_top200_securities.csv` | 策略证券：历史证券、现行证券（无法确定时留空）、现行代码、是否可选 |
| `rank_boundary_180_220.csv` | 第 180–220 名的金额、并列、是否入选、未解析行与上界 |
| `exclusions.csv` | 策略名单中被排除的公司与证券，附原因 |
| `blocking_review.csv` | blocked 季度的候选公司与无候选组，附未解析金额 |
| `unresolved_lines.csv` | 全部未解析的股票行（含 CVR）及其候选 CIK |
| `quarter_checks.csv` | 每版每项检查的结果 |
| `security_union.csv` | 策略可选证券的去重联集：首末季度、季度数、季度列表与历史发行人；不是每季共同名单 |
| `bridge_2019_12_31_company_compare.csv` | 2019-12-31 两个来源的公司比较 |

## 12. 构建与版本

- `schema_version` 2，`rule_version` top200_v1，`status_cutoff` 2026-10-26，`build_cutoff_utc` 2026-10-27T00:00:00Z。
- 构建：`python -m leadtrend_db build --offline --db work/top200_v1/candidate.sqlite`；核对：`python -m leadtrend_db check
  --db ...`；导出：`python -m leadtrend_db export --db ...`；核对通过后方可 `promote`（替换 `data/db/leadtrend_db_v1.sqlite`，
  旧库保存于 `work/top200_v1/previous_db_v1.sqlite`）。
- 内容哈希不含时间戳与构建元数据（meta 与 build_log 除外）；同一档案与同一规则重建结果相同。

## 13. 已知限制

- 名单只在构建截止时可知；本版不建每日成分库，不声称知道两次申报之间每日的真实成分。
- 季度候选不代表每一天都能交易；未来策略接入时还需交易日上市与公司行动数据（本版为空表）。
- 分拆（spinoff）未识别；未在缓存中的继任（如 Bunge 的新 CIK）未建链接。
- v05 的已知缺陷未被复制；v05 只用作对照（第 8 节）。
- 本版没有行情，未用价格或市值排序，也未做任何策略运行。
