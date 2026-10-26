# LeadTrend 成分股数据库 v1

本文档说明 `leadtrend_db` 的用途、目录、构建与核对方法、表结构，以及实际实现与原说明不同之处。
数字以 `research/db_v1/summary.md`（只含计数）和 `data/db/exports/checks.json`（核对明细）为准。

## 1. 用途与范围

- 覆盖：2013-12-31 起每个季度末 VOO 所属基金（Vanguard 500 Index Fund，CIK 36405 下的 S000002839，持仓与 VOO 相同）的完整持仓。
- 每家公司、每只证券在 VOO 中的时间段（`membership_spans`），每家曾离开 VOO 的公司的上市状态（`listing_status`）。
- 股票名单 `universe_v1`：排除已退市公司（投资者决定，见第 6 节）。
- 只建数据，不取行情，不运行策略。`prices_daily`、`corporate_actions`、`price_coverage`、`index_changes` 只建表，不填数据。

## 2. 目录

| 位置 | 内容 | 是否进库 |
|---|---|---|
| `leadtrend_db/` | 代码：`schema.sql`、`sources.py`、`parse_ncsr.py`、`parse_nport.py`、`identity.py`、`status.py`、`build.py`、`checks.py`、`export.py`、`__main__.py` | 是 |
| `tests_db/` | 单元测试（合成数据，不联网） | 是 |
| `config/db_overrides_v1.csv` | 人工修正（唯一修正入口） | 是 |
| `data/db/raw/` | 原始档案：每个下载文件及 `manifest.jsonl`（只追加）、`nport_screen.csv`、v05 副本 | 否（git 忽略） |
| `data/db/leadtrend_db_v1.sqlite` | 数据库 | 否 |
| `data/db/exports/` | CSV 导出、`checks.json`、`checks.md`、`stats.md`、复核清单 | 否 |
| `research/db_v1/summary.md` | 统计摘要（只有计数） | 是 |
| `work/` | 临时目录（离线重建、日志） | 否 |

`.gitignore` 增加了 `data/db/`。

## 3. 命令

```bash
python -X utf8 -m leadtrend_db build            # 缺失的文件从 SEC 取得并存档，然后写数据库
python -X utf8 -m leadtrend_db build --offline  # 只读档案，不联网
python -X utf8 -m leadtrend_db check            # 核对，并离线重建一次比较内容哈希
python -X utf8 -m leadtrend_db stats --summary research/db_v1/summary.md
python -X utf8 -m leadtrend_db export           # CSV 导出
python -m pytest -q -o addopts= tests_db        # 单元测试
```

SEC 的 User-Agent 从 `config/v05_stage2.json` 的 `sec_user_agent` 读取，程序不打印、不记录、不写入任何文件。请求间隔不少于 0.21 秒（每秒不超过约 4.8 次）；失败按 2、4、8、16 秒退避重试。

## 4. 构建方法

1. **原始档案**：每个下载的文件存入 `data/db/raw/`，`manifest.jsonl` 记录网址、取得时间（UTC）、sha256、字节数。同一网址只取一次；档案中的文件只读，读取时校验 sha256。
2. **申报清单**：CIK 36405 的 submissions 全部分页。
3. **2014–2019 季度持仓**：N-Q（3、9 月）、N-CSRS（6 月）、N-CSR（12 月）的主文件；若主文件中找不到 500 基金明细，则检查该申报的全部 htm 文件。2019-03-31 没有 N-Q，该季度由同日申报的 NPORT-EX 补足（见第 7 节）。只取 500 基金的股票行；现金、临时现金投资（含 Vanguard Market Liquidity Fund）、国债等计入 `excluded_rows` 与 `excluded_value_usd`，不进入持仓表。
4. **2019-09-30 起的 N-PORT**：`data/v05/raw/` 中的 N-PORT 文件已在本地存在，构建时只读取、不重新下载；其中属于 500 基金（系列 S000002839，含 VOO 类别 C000092055）的 28 份复制入 `data/db/raw/nport/`。每个报告期只有一份属于 500 基金的申报。
5. **2019-12-31 衔接**：N-CSR（2020-03-02 公开）与 N-PORT 同日持仓逐只对照，按名称（相似度不低于 0.5）、类别和股数（相差不超过 1%）配对；配对成功的名称取得 CUSIP。
6. **发行人（CIK）**：
   - N-PORT 的 CUSIP：v05 证券表中的 SEC 来源注明的 CIK → v05 代码在 SEC `company_tickers.json` 中的 CIK → 仍无法对应的，用发行人名称在 `cik-lookup-data.txt` 中查找；同名多个候选时，先取 SEC 名称（现名或曾用名）完全一致者，再取对应季度前后有 10-K 或 10-Q 者；仍不唯一的列入复核清单。
   - 2019 年以前的名称：先用第 5 步的配对，其余用同样的名称查找。
   - 多类别公司（如 GOOG/GOOGL）归为同一发行人，各类别为不同证券。
7. **上市状态**（以最新 submissions 为准，见第 5 节）与 `issuer_links`。
8. **派生**：`membership_spans` 按季度计算，离开后又回来算新一段；缺少完整快照的季度会结束当前时间段；`universe_v1` 为视图。

## 5. 上市状态规则

- `in_index`：在最新快照中。
- `listed_out_of_index`：不在最新快照中，但 submissions 列有 NYSE、Nasdaq 或 Cboe 上市。
- `continued_by_successor`：前身退市，由继任发行人延续（见 `issuer_links`）。
- `delisted`：不在上述交易所上市（被收购、合并、私有化、破产、转 OTC、仅余债券申报等）。`delisting_date` 取最后一份 Form 25 或 25-NSE 的申报日加 10 天；没有 Form 25 时取最后一份 Form 15 的申报日，`method` 注明依据。`reason` 只在有并购委托书（DEFM14A）、要约（SC TO-T、SC 14D9）或私有化（SC 13E3）申报时填写为 `acquired_or_merged` 或 `going_private`，其余为 `unknown`。
- `unknown`：无可用证据，或一年内仍有定期报告而无 Form 25。`needs_review=1` 的进入复核清单。

继任者的自动识别：某发行人退市前后 120 天内，另一发行人（在 VOO 股票池中）提交 8-K12B，且名称相似度不低于 0.85 时，才自动建立 `successor_reorganization` 链接（`confidence=high`）；其余候选进入复核清单。改名（同一 CIK）不建链接。分拆（`spinoff_from`）未自动识别，本版没有填入。

## 6. 排除已退市公司

`universe_v1` 排除状态为 `delisted` 的发行人（投资者决定）。`eligible_v1` 按 AGENTS.md 设置：REIT（SIC 6798 或证券类型）、合伙单位、BRK.A 为 0，其余为 1。

## 7. 与原说明不同之处（及理由）

| 项目 | 原说明 | 实际 | 理由 |
|---|---|---|---|
| 2019-03-31 季度 | 只列 N-Q、N-CSR、N-CSRS | 增加 NPORT-EX（同日申报 `vgindex500fnd003119.htm`） | 该季度只有这一份申报含 500 基金明细，否则出现空缺 |
| 2019-12-31 N-CSR | 报告期至 2019-06-30 | 包括报告期 2019-12-31 的 N-CSR（2020-03-02 公开） | 衔接步骤需要它；它不作为季度持仓（季度用 N-PORT） |
| 解析器 | 复用 `p3_external.parse_500_index_schedule` | 复用其 HTML 表格读取器；行的规则另写 | 原解析器按固定位置读取单元格，会丢行（2015 年样本丢 486 行中的约 2.3bn 股票市值）；另写的版本能处理名称跨行、数字拆开等版式，与报告的股票合计对齐（2015 年样本、2014 年年报均为 100.0%） |
| N-PORT 的股票名单 | 复用 `sec_nport.py` | 复用 `parse_nport_xml`；权重存为 pctVal/100（见下一行） | 与 v05 的字段一致；v05 存的是 pctVal 本身 |
| CIK 备用查找 | N-PORT 只用 LEI 或 CUSIP | 另加：无 CIK 的 CUSIP 按名称在 cik-lookup 中查找，规则同 2019 年以前的名称 | 2020 年后退市的公司不在当前 ticker 列表中；不加则 2019–2026 年的成员大量丢失 |
| 权重 | 未规定 | N-PORT 行存 pctVal/100；2014–2018 行存 股票市值 / 报告的总投资 | `weight_basis` 字段注明；与 v05 逐行核对时 v05 的 pctVal 除以 100 |
| 状态日期 | 构建日 | `status_as_of` = 最近一次 submissions 取得日（UTC） | 使构建可重复 |
| OpenD | 可选 | 未使用 | 本次只需要名称与 SEC 信息，OpenD 查询不必要 |
| 原始文件 | 每个下载的文件存入档案 | 另有 v05 的 N-PORT 副本（28 份）与 v05 证券表、快照成员表的副本 | 构建不得依赖 `data/v05/`；副本注明来源路径 |

## 8. 表结构

以 `leadtrend_db/schema.sql` 为准。主要表：

- `meta`、`build_log`：版本与构建步骤（不进内容哈希）。
- `sources`：每份原始文件（kind、网址、路径、sha256、字节数、取得时间）。`source_id = SRC-` 加 sha256 前 16 位。
- `issuers`、`issuer_names`、`issuer_links`、`issuer_resolution`、`listing_status`。
- `securities`、`security_identifiers`：`security_id` 为 `CUSIP:<cusip>`（沿用 v05）或 `CIKCLS:<10 位 CIK>:<类别>`。代码（ticker）来自 v05 证券表（N-PORT 时期）或当前 SEC 列表（类别为普通股、且该 CIK 只有一个代码）；未知的留空。FIGI、富途代码未填。
- `snapshots`：`is_primary` 为 1 的是该季度用于成员的快照。`completeness`：`full`（行数不少于 400 且股票合计与报告值相差不超过 0.1%）、`partial`、`summary`（找不到明细）。只有 `full` 参与成员计算。
- `holdings`：每行一只股票；未映射的行保留，`unmapped_reason` 说明原因。
- `membership_spans`：发行人与证券两级。
- `review_items`：复核清单。
- `universe_v1`（视图）、`index_changes`、`prices_daily`、`corporate_actions`、`price_coverage`（只建表）。

## 9. 核对结果（摘要）

完整数字见 `data/db/exports/checks.json`，构建后由 `check` 生成。主要结论：

- 49 个季度有完整主快照（共 51 个季度；2018-12-31 与 2019-06-30 缺）。
- 股票行数 480–520 与股票市值占报告总投资 98%–100% 在所有主快照内成立；权重合计在 0.993–1.001 之间。
- 与 v05 `snapshot_members.csv` 的 2019-09-30 之后 28 个快照逐行比较：权重完全一致（0 处差异）；数据库有而 v05 没有的 183 行、v05 有而数据库未映射的 349 行，见 `checks.json` 的 `5_v05_agreement`。
- 离线重建的内容哈希与首次构建相同（check 7）。
- 单元测试 16 项通过。

## 10. 已知限制

- 2019 年 12 月 31 日的 N-CSR（2020-03-02 公开）只解析出 447 行，股票合计仅与报告值的 81% 相符（版式未完全识别，见 `checks.json`），因此只用于名称配对，不作主快照。2018-12-31（N-CSR）、2019-06-30（N-CSRS）两个季度因此缺完整明细，列入复核清单。
- 未映射到发行人的行保留在 `holdings` 中，并列入 `checks_unmapped_rows.csv`。2014–2019 年的 N-Q、N-CSR、N-CSRS 与 2019-12-31 的 N-CSR 合计 690 行未映射，各快照映射率在 91.4%–98.7% 之间（2013-12-31 最低）。N-PORT 时期（含 2019-03-31 的 NPORT-EX）合计 140 行未映射，映射率在 95.9%–100% 之间（2019-09-30 为 98.0%）。因此"每期映射 100%"的目标没有达到。
- 重新设立公司（redomicile，新 CIK）的前身与继任者只有在提交 8-K12B 且名称相似度不低于 0.85 时才自动链接。2020 年 3 月进入的 23 家（如 Allegion、Linde、Trane、TechnipFMC）多数属此情况，未链接，因此前身时间段不延续。这些候选在复核清单中。
- `reason` 多数为 `unknown`。分拆子公司未识别。
- 2019 年 N-CSR 与 N-PORT 之间的股数相差约 0.3%（比值中位数 0.997），本版按 1% 容差配对。

## 11. 复核清单类型

`bridge_unmatched`（2019-12-31 配对不上）、`nport_cusip_unmapped`（N-PORT 的 CUSIP 无 CIK）、`successor_candidate`（继任候选，未链接）、`status_review` 与 `status_unknown`、`missing_quarter`、`snapshot_not_full`。具体条数见 `research/db_v1/summary.md`。

## 12. 人工修正

只能写入 `config/db_overrides_v1.csv`，列为：`override_kind`（`cusip_cik`、`name_cik`、`link`、`status`）、`key`、`value`、`field`、`reason`、`evidence`。`reason` 与 `evidence` 必填。`link` 的 `key` 为 `前身CIK>继任CIK`，`value` 为关系，`field` 为生效日。本版已填入四条继任关系（Google→Alphabet、Walgreen Co.→Walgreens Boots Alliance、Medtronic Inc.→Medtronic plc、BlackRock Finance→BlackRock, Inc.），证据为 submissions 中的申报日期。另有一条自动链接（Praxair→Linde plc，8-K12B 2018-11-02，名称相似度 1.0）。
