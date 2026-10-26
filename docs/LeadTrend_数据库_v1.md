# LeadTrend 成分股数据库 v1

本文档说明 `leadtrend_db` 的用途、目录、构建与核对方法、表结构、当前结果与未解决项。数字以
`research/db_v1/summary.md`（只含计数）和 `data/db/exports/checks.json`（核对明细）为准。修复轮的问题与修复方式见第 13 节。

## 1. 用途与范围

- 覆盖：2013-12-31 起每个季度末 VOO 所属基金（Vanguard 500 Index Fund，CIK 36405 下的 S000002839，持仓与 VOO 相同）的股票持仓，共 51 个季度。
- 每家公司、每只证券在 VOO 中的时间段（`membership_spans`），每家曾离开 VOO 的公司的上市状态（`listing_status`）。
- 股票名单 `universe_v1`：排除目前已退市的公司（投资者决定）；`eligible_v1` 按 AGENTS.md 设置。
- 只建数据，不取行情，不运行策略。`prices_daily`、`corporate_actions`、`price_coverage`、`index_changes` 只建表，全部为空。

## 2. 目录

| 位置 | 内容 | 是否进库 |
|---|---|---|
| `leadtrend_db/` | 代码：`schema.sql`、`sources.py`、`parse_ncsr.py`、`parse_nport.py`、`identity.py`、`status.py`、`build.py`、`checks.py`、`export.py`、`__main__.py` | 是 |
| `tests_db/` | 单元测试（合成数据，不联网），每项注明其防范的失败模式 | 是 |
| `config/db_overrides_v1.csv` | 人工修正（唯一修正入口；每条需理由与证据） | 是 |
| `data/db/raw/` | 原始档案：每个下载文件、`manifest.jsonl`（只追加）、`nport_screen.csv`、v05 副本 | 否（git 忽略） |
| `data/db/leadtrend_db_v1.sqlite` | 数据库 | 否 |
| `data/db/exports/` | CSV 导出、`checks.json`、`checks_*.csv`（逐行清单）、`stats.md` | 否 |
| `research/db_v1/summary.md` | 统计摘要（只有计数） | 是 |
| `work/` | 临时目录（重建、比较、日志） | 否 |

`.gitignore` 含 `data/db/`。

## 3. 命令

```bash
python -X utf8 -m leadtrend_db build                      # 缺失文件从 SEC 取得并存档，然后写数据库
python -X utf8 -m leadtrend_db build --offline            # 只读档案，不联网
python -X utf8 -m leadtrend_db build --db work/x/db.sqlite  # 写到其他位置（用于先核对再替换）
python -X utf8 -m leadtrend_db check                      # 核对，并离线重建一次比较内容哈希
python -X utf8 -m leadtrend_db stats --summary research/db_v1/summary.md
python -X utf8 -m leadtrend_db export
python -X utf8 -m pytest -q -o addopts= tests_db
```

SEC 的 User-Agent 从 `config/v05_stage2.json` 的 `sec_user_agent` 读取，程序不打印、不记录、不写入任何文件。请求间隔不少于 0.21 秒（每秒不超过约 4.8 次）；失败按 2、4、8、16 秒退避。

## 4. 构建方法

1. **原始档案**：每个下载的文件存入 `data/db/raw/`，`manifest.jsonl` 记录网址、取得时间（UTC）、sha256、字节数。同一网址只取一次；已入档文件只读，读取时校验 sha256。
2. **申报清单**：CIK 36405 的 submissions 全部分页（2,460 份申报）。
3. **季度持仓（500 基金明细）**：N-Q、N-CSR、N-CSRS 的主文件；2019-03-31 用同日 NPORT-EX（500 基金的一份，其余 11 份是其他基金，未入库）；2019-12-31 的 N-CSR 只用于衔接。明细段落按多个版式的起点规则确定（见第 13 节），每个候选起点都与申报自身的 Total Common Stocks 核对，取第一个核对成立者。负数行（括号表示，如 CDK Global 2014-09-30）单列，计入核对但不计入持仓。
4. **N-PORT（2019-09-30 起）**：`data/v05/raw/` 中的文件只读取；属于 500 基金（系列 S000002839、类别 C000092055）的 28 份复制入 `data/db/raw/nport/`。EC 资产类别的每一行都保留，包括 CUSIP 为空或 `N/A` 的行。
5. **证券身份**（不使用占位符作为键）：
   - 有效 CUSIP（校验位正确）；
   - 美国 ISIN 的第 3–11 位即 CUSIP（推导）；
   - 非美国 ISIN：若 v05 证券的来源记录中有相同 ISIN，取其 CUSIP；否则以 `ISIN:<代码>` 作为证券标识（`id_scheme=ISIN`）；
   - 以上都没有：不映射，原因写入 `unmapped_reason` 和复核项。
6. **发行人（CIK）**：人工覆盖 → v05 来源记录中的 SEC 对应 CIK → SEC 当前代码（经名称核对）→ 名称在 cik-lookup 中查找。每个候选必须在持仓日期**有效**：SEC 名称（含 formerNames 的起止日期）在该日期有效，且该日期前后 95 天内有定期报告（或 120 天内有任一申报）。唯一有效候选才接受；多个候选则按完全一致的 SEC 名称缩小；仍不唯一或无候选的留作未映射。
7. **2019-12-31 衔接**：N-CSR 行与 N-PORT 行按类别、股数（1% 容差）、名称（相似度不低于 0.5）配对，只配对有有效证券标识的 N-PORT 行。
8. **类型**：证据依次为 v05 来源记录中的 OpenFIGI 类型文字、持仓标题、名称、发行人 SIC（6798 为 REIT），最后为 SEC 资产类别 EC。无证据则为 `unknown`，不默认为普通股。
9. **权重**：`weight = 该行市值 ÷ 该快照股票市值合计`。股票市值合计不含权利、认股权证、或有价值权（CVR）、ETF、其他类型。N-PORT 的原始 `pctVal`（百分比，0–100）另存为 `pct_val_percent`。
10. **派生**：`membership_spans` 按季度计算；离开后又回来算新一段；缺少完整快照的季度结束所有时间段（不推断中间状态）。

## 5. 上市状态规则

两个事实分开记录：`latest_common`（发行人的普通股或 ADR 出现在最新报告期持仓中，CVR 或权利不算）与构建日的 SEC 证据（`status_as_of` 当日的交易所列表和申报）。

- `in_index`：构建日仍在 NYSE、Nasdaq 或 Cboe 上市，且在最新持仓中有普通股。
- `listed_out_of_index`：仍上市，但不在最新持仓中。
- `delisted`：不在上述交易所上市，且有 Form 25 / 25-NSE（取**最后一份**，退市日为其申报日加 10 天）或 Form 15（取最后一份申报日）。若最新持仓仍有普通股且退市日不晚于最新报告日，`needs_review=1`。
- `continued_by_successor`：前身，由继任发行人延续（`issuer_links`），显示在继任者之下。
- `unknown`：不在上市交易所、无退市申报，或身份未确认。`needs_review=1`；不因缺少交易所字段或旧的最后申报而直接判为退市。

自动继任：退市前后 120 天内另一发行人提交 8-K12B，且名称相似度不低于 0.85，才建立链接（`confidence=high`）。其余候选进入复核清单。人工链接见第 12 节。

## 6. 排除已退市公司

`universe_v1` 排除状态为 `delisted` 的发行人（投资者决定）。`eligible_v1` 为 0 的情形：类型不是普通股或 ADR（REIT、合伙单位、权利、认股权证、CVR、ETF、其他）、或 BRK.A。

## 7. 与原说明不同之处（及理由）

| 项目 | 原说明 | 实际 | 理由 |
|---|---|---|---|
| 2019-03-31 季度 | 只列 N-Q、N-CSR、N-CSRS | 增加 NPORT-EX | 该季度只有这一份申报含 500 基金明细 |
| 2019-12-31 N-CSR | 报告期至 2019-06-30 | 包括 2019-12-31 的 N-CSR（2020-03-02 公开） | 衔接需要；不作季度持仓（季度用 N-PORT） |
| 证券标识 | CUSIP 或 CIKCLS | 增加 `ISIN:` 标识（无 CUSIP 且 v05 无对应时） | 外国发行人的 N-PORT 行常无 CUSIP，只有 ISIN |
| 2019 年 N-PORT 的 EC 行 | 复用 v05 解析器 | 自写行解析，保留空 CUSIP 行 | v05 解析器丢弃空 CUSIP 行 |
| 权重 | 股票市值 ÷ 当期股票市值合计 | 同原说明（修复后）；另存原始 `pct_val_percent` | 见第 13 节第 6 项 |
| 发行人主键 | CIK | 同原说明；另有 `lei`、SIC 描述、注册地 | 原说明列出的约定字段 |
| 退市日期 | Form 25 生效日 | 最后一份 Form 25 申报日加 10 天；无则最后 Form 15 申报日 | 早期的 25-NSE 可能与最终退市无关 |
| `reason` | 尽力判断 | 只判断 `acquired_or_merged`（有 DEFM14A、SC TO-T、SC 14D9 等）与 `going_private`（SC 13E3）；其余 `unknown` | 避免猜测 |
| 状态日期 | 构建日 | `status_as_of` = 最近一次 submissions 取得日（UTC 日期） | 使构建可重复 |
| OpenD | 可选 | 未使用 | 不需要 |
| 原始文件 | 每个下载的文件入档 | 另有 v05 的 N-PORT 副本（28 份）与 v05 证券表、快照成员表、快照表的副本 | 构建不依赖 `data/v05/` |

## 8. 表结构

以 `leadtrend_db/schema.sql` 为准。外键在写库时启用，违规数为 0 才写入成功（`checks.json` 的 `8_integrity`）。

- `meta`：`schema_version`、`build_id`、`code_commit`、`raw_manifest_sha256`、`built_at_utc`、`content_sha256`。
- `sources`：每份原始文件；`source_id = SRC-` 加 sha256 前 16 位。
- `issuers`：`cik`、`name`（SEC 当前名称）、`sic`、`sic_description`、`state_of_incorporation`、`lei`（来自 N-PORT）。
- `issuer_names`：`sec_current`、`sec_former`（带 `valid_from` / `valid_to` 起止日期）、`holdings_observed`（带首末报告期）。
- `issuer_links`、`issuer_resolution`、`listing_status`。
- `securities`：`security_type`、`type_basis`（分类依据）、`id_scheme`（CUSIP / ISIN / CIKCLS）。
- `security_identifiers`：`cusip`、`isin`、`lei`、`ticker`（非空值才写入）。
- `snapshots`：`is_primary`、`completeness`、`reconciled`、`start_rule`、`reported_common_stock_total_usd`、`stock_total_usd`、`coverage_ratio`。
- `holdings`：`line_kind`（stock / negative）、`cusip_reported` 等原始字段原样保留、`pct_val_percent`、`weight`、`security_id`、`issuer_id`、`unmapped_reason`。
- `membership_spans`：发行人与证券两级。
- `review_items`：复核清单（`source_id` 可为空）。
- 预留（全部为空）：`prices_daily`（主键 `security_id, session_date, source`）、`price_coverage`（主键 `security_id, source`）、`corporate_actions`、`index_changes`。
- 视图 `universe_v1`：`ticker` 为该证券自身代码（仍属于解析后的发行人时）；若证券属于前身，显示继任者同类别的现行代码（不沿用前身旧代码）。

## 9. 核对结果（摘要）

完整数字见 `data/db/exports/checks.json`，修复后的运行生成。

- 主快照 51 个，全部完整（2013-12-31 至 2026-06-30 无缺失季度）。股票行数 502–511；股票市值占报告总投资 98.7%–99.9%；每个快照的权重合计为 1（误差 1e-15 以内）。
- 2019-12-31 两源逐股核对：N-CSR 508 行（配对 495，未配对 13，歧义 0）；N-PORT 509 行；股数比中位数 0.997，最小 0.996，最大 1.000。
- 与 v05 的 N-PORT 快照（28 个）：原始 pctVal 在 13,912 个共同证券上 0 处差异；归一化权重在同一证券集合上比较，同样 0 处差异，最大差 5.6e-12（容差 1e-9，源于 pctVal 的数值精度）。v05 独有 263 行（其中 22 行在数据库中无对应行，其余为未映射发行人），数据库独有 0 行；逐行清单见 `checks_v05_only.csv`、`checks_v05_db_only.csv`。
- 未映射股票行 525 行（`checks_unmapped_rows.csv`）：`no_candidate_valid_at_date` 312、`no_cik_candidate` 140、`ambiguous_candidates` 57、无有效标识 16（CVR 等，无 ISIN）。全部 52 个快照中没有一个达到 100% 映射（最低 96.4%）。
- 进出：相邻主快照 50 段，进入中位数 6；超过 15 只的仅 2017-03-31（进入 16、离开 11），已在第 13 节第 3 项说明。
- 发行人状态：离开过 VOO 的发行人 265 家，全部有状态；delisted 146（acquired_or_merged 116、going_private 2、unknown 28）、listed_out_of_index 103、in_index 11、continued_by_successor 5。需复核 1 家（Apache，最后 Form 25 为 2021-03-04，但最新持仓仍有普通股）。
- 证券类型：common 743、reit 40、right 1；`eligible_v1=1` 的证券 589 个（段行 621），其中无代码 16 个（段行 17 个；`checks_eligible_no_ticker.csv`）。
- 完整性：外键违规 0；预留表行数全部为 0；标识空值 0；离线重建内容哈希一致，网络请求 0。

## 10. 已知限制（截至本轮）

- 映射未完成：未映射的股票行共 525 行（见第 9 节）。其中部分是 2019 年以前已离开、名称在 SEC 查找中无有效候选的公司；部分是 2019 年后 N-PORT 的证券，其名称与 SEC 记录的差异超出当前规则。
- 与独立名单的差异（见第 13 节第 4 项）：独立名单中有 12 个发行人在数据库中没有对应发行人（主要为身份未映射，例如 U.S. Bancorp、Vertex、Invesco、LyondellBasell、APA、ExxonMobil Holdings 等）；数据库中有 6 个发行人不在独立名单中（上市状态的判定口径不同，需由投资者决定）。
- 2019-12-31 的两源股数存在约 0.3% 的系统性差异（比值的分布紧，但不是完全相等）；按 1% 容差配对，13 行未配对。
- 未识别自动分拆（`spinoff_from`）；重新设立注册地的公司（新 CIK）除少数人工链接外不自动链接；`reason` 多数为 `unknown`。

## 11. 复核清单类型

`bridge_unmatched`（2019-12-31 衔接未配对）、`nport_security_unmapped`（N-PORT 证券无 CIK）、`pre_name_unmapped`（2019 年以前的名称无 CIK）、`unidentified_security`（N-PORT 行无有效标识，如 CVR）、`successor_candidate`（继任候选，未链接）、`status_review`（需复核的状态）、`multiple_complete_snapshots`（同一季度有两份完整快照，已选定主快照）。

## 12. 人工修正

只能写入 `config/db_overrides_v1.csv`，列为：`override_kind`（`cusip_cik`、`name_cik`、`link`、`status`、`security_type`、`security_cusip`）、`key`、`value`、`field`、`reason`、`evidence`。`reason` 与 `evidence` 必填。`link` 的 `key` 为 `前身CIK>继任CIK`，`value` 为关系，`field` 为生效日。当前有 5 条：

- Google Inc. → Alphabet Inc.（2015-10-02）；
- Walgreen Co. → Walgreens Boots Alliance（2014-12-31）；
- Medtronic Inc. → Medtronic plc（2015-01-27）；
- BlackRock Finance → BlackRock, Inc.（2024-10-01）；
- Paramount Global（原 CBS Corp.）→ Paramount Skydance Corp，现名 Skydance Corp（2025-08-07；2041610 的 8-K12B 与 813828 的 Form 25 为证据）。

## 13. 修复轮（本轮）

以下对应投资者审阅的八类问题。每项写明修复方式、修复前后的数字，以及仍未满足之处。

1. **N/A 被当作 CUSIP**：`CUSIP:N/A` 不再是任何证券的键（修复前 1 个证券键、87 条持仓被串联到 Hologic 等公司；修复后 0）。占位符、空值、校验位错误的 CUSIP 不作键。唯一的 CVR 类行（无 ISIN）保留为未映射并列入复核（16 行）。已解决的标识方式：美国 ISIN 推导（修复后 24 个证券由 v05 ISIN 对应取得 CUSIP，2 个证券只有 ISIN）。Hologic 修复后只剩普通股一个证券，其 CVR 不再使其显示为在指数中。
2. **三份旧报告漏行**：修复前 2018-12-31 447 行、2019-06-30 为 0（summary）、2019-12-31 447 行。修复后：2018-12-31 507 行，股票合计 398,664,398,000 美元，与报告合计 398,664,480,000 美元相差 0.00002%；2019-06-30 507 行，480,560,094,000 与 480,560,193,000 相差 0.00002%；2019-12-31 508 行，534,365,449,000 与 534,365,463,000 相差 0.000003%。差异来自各行以千美元取整。2013-12-31 的 N-CSR 完全一致。起点规则为多个候选，逐个与申报合计核对。负数（CDK Global 2014-09-30）单列。全部 24 份 htm 申报（2013–2019 年）的 `reconciled` 为 1，即其股票行与申报合计一致；N-PORT 快照没有可核对的申报合计，其 `reconciled` 字段固定为 1，覆盖率见 `coverage_ratio`。
3. **名称唯一候选未核验时期**：每个候选须在持仓日期有效（SEC 名称或 formerNames 起止日期，且有定期报告）。修复后 Mattel 为 CIK 63276（代码 MAT，Nasdaq 上市）；ConAgra 为 CIK 23217（ConAgra Brands，原 ConAgra Foods 的 formerNames 期间）；Charter 为 CIK 1091667（Nasdaq，在指数中）；Zions 为 CIK 109380（Nasdaq）。已映射的名称由 647 个增至 678 个（2019 年以前名称簇）。名称规范化：去除 SEC 州别后缀（/DE/、/MO/、/NEW/）、`&` 与 `and`、分开书写的首字母（U.S. 与 US）、COMPANIES。
4. **已知 CUSIP 未补回**：标识字段保留第一个有效值，之后的有效值不被空值遮蔽；不写空标识。空值标识行：修复前 462，修复后 0。无代码的 `eligible_v1=1` 证券：修复前 403，修复后 16（多代码发行人的认股权证代码已排除）。Linde plc 的代码由 v05 的数字代码 9995214D 改为 SEC 当前代码 LIN（代码格式检查）。universe 中前身证券显示继任者的现行代码。
5. **最新快照覆盖退市事实**：区分 `latest_common` 与构建日的上市证据。修复前 EA、WBD、Hologic 为 `in_index`；修复后均为 `delisted`（最后 Form 25 分别为 2026-08-04、2026-10-06、2026-04-07，退市日为申报日加 10 天）。Paramount Global 经 `issuer_links` 延续至 Skydance Corp（在指数中，NYSE）。全部发行人中，修复后 `in_index` 为 460 家（修复前 498 家，其中含已退市的 EA、WBD 与 Hologic 等）；离开过 VOO 的发行人中仍在指数内的为 11 家。仍有 1 家（Apache，最后 Form 25 为 2021-03-04）标记 `needs_review`。
6. **权重未归一化**：修复后 `weight = 该行市值 ÷ 股票市值合计`（权利、认股权证、CVR、ETF、其他不计入分母）。原 N-PORT `pctVal` 另存为 `pct_val_percent`。与 v05 的核对分两种口径：原始 pctVal 0 处差异（13,686 对）；归一化在相同证券集合上最大差 5.6e-12。
7. **非普通股被当作普通股**：TMUSR（T-Mobile 权利，v05 来源记录中的 OpenFIGI 类型为 Right）修复后为 `right`，不计入 `eligible_v1`。v05 自身的类型标注 `common_stock` 不被采用（同一记录的 OpenFIGI 文本为 Right）。CVR 类行修复后排除。
8. **来源外键与预留结构**：外键违规修复前 1,613 条，修复后 0；写库时外键检查不通过即中止。`sources`、`review_items` 等列为空时使用 NULL。`meta` 补充 `build_id`、`code_commit`、`raw_manifest_sha256`。`issuers` 补充 LEI、SIC 描述、注册地。`prices_daily` 主键改为 `security_id, session_date, source`；`price_coverage` 主键为 `security_id, source`。预留表全部为空。

9. **v05 证券别名归并**（本轮追加，代码提交 `08e79e4`）：v05 的别名表（`data/v05/prep/stage2_security_aliases.csv`，47 条）记录了同一证券的旧 CUSIP 或 ISIN 与规范 CUSIP 的归并。修复前，N-PORT 的旧标识被当作另一只证券（如 Apartment Investment & Management 的 03748R754 与 v05 规范标识 03748R747）；修复后按 v05 的归并使用规范标识（`+v05_alias`）。与 v05 的核对：`only_in_db` 由 199 降至 0，`only_in_v05` 由 489 降至 263，共同证券 13,912 个，原始 pctVal 与归一化权重均 0 处差异。

**仍未满足之处**：映射率未达 100%（第 9 节，525 行）；2019-12-31 两源有约 0.3% 的股数差异（已按容差配对并如实记录）；自动分拆与多数重新注册的继任关系未识别；`reason` 多数为 `unknown`；与独立名单的差异未全部解决（第 10 节）。

## 14. 最后一次核对的结论位置

- `data/db/exports/checks.json`：全部核对结果（第 9 节所列数字的来源）。
- `data/db/exports/checks_unmapped_rows.csv`、`checks_v05_only.csv`、`checks_v05_db_only.csv`、`checks_status_review.csv`：逐行清单。
- `data/db/exports/review_list.csv`：全部复核项。
- `research/db_v1/summary.md`：只有计数的统计摘要（已提交）。
