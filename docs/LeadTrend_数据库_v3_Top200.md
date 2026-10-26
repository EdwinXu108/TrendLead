# LeadTrend 季度 Top 200 数据库（schema 4，行情阶段升为 schema 5）

当前规则为 `top200_v2`。正式库为 `data/db/leadtrend_db_v1.sqlite`；沿用文件名以保持兼容，实际 schema 版本读 `meta`。
本版本修复 v2 的证券继任投影、公开版本查询、排名边界和验收门禁。v1/v2 文档保留作历史记录。

## 名单规则

1. 输入为 Vanguard 500 Index Fund（VOO 所属基金）的季度实际股票持仓。
2. 先按当季证券类型排除 REIT、合伙单位、ETF、权利、权证、CVR 等，再按公司合并符合边界的股票类别金额，取前 200 家。
3. 金额来自基金申报，排名不取行情、不重建总市值；它不是官方标普 200 指数。金额排序使用整数美分，并列按稳定发行人标识排序。
4. 一家公司只占一个名额；只输出基金当季确实持有的类别。BRK.A 不可选，BRK.B 可选。Comcast Special A 与 A 分别保存，不能合并为同一历史证券。
5. 历史排名不受当前上市状态影响。策略名单另应用固定 `status_cutoff`：已退市排除，已核实的重组继任延续；不从第 201 名补位。
6. 证券类型按生效日期处理。例如 Crown Castle 于 2014-01-01 转为 REIT，2013-12-31 的历史排名仍按普通经营公司处理；当前候选仍排除 REIT。Equinix、Iron Mountain、SBA 的转换同样保留日期。
7. 仅附近日期有其他公司提交 8-K12B，不是法律继任证据。正式继任必须有带日期的修正记录与官方原始文件；纯收购的买方不自动代替被收购方。

## 身份与来源

`holdings.issuer_id` 是持仓当季发行人；`security_issuer_spans` 保留证券的历史归属。
`strategy_securities.security_id` 保留历史证券，`current_security_id`、`current_ticker` 是另列的当前投影，两者不可混用来回填历史行情。
新控股公司成立在最后一期持仓之后时，可用官方继任文件建立 `CIKCLS:` 当前证券，不编造 CUSIP。

一般发行人沿用 `CIK:` 主键。First Republic、Signature 是向银行监管机关申报的银行，使用 N-PORT 已披露的 `LEI:` 主键，并以 FDIC 原始资料核实；`issuers.cik` 留空，不套用同名其他实体的 CIK。
此项是为忠实记录非 SEC 申报主体而作的 schema 3 扩展。银行对应的 CUSIP、ISIN、LEI 均保留在原始持仓和标识表。

日期不明的历史 ticker 留空；已知旧代码与当前代码分别记录。不能把当前 ticker 当成当季 ticker。
所有人工身份、类型、继任、退市和类别修正只进入 `config/db_overrides_v1.csv`，每条包含理由、证据和适用日期。
源文件在 `data/db/raw/` 只追加，清单记录 URL、取得 UTC、sha256 和字节数。SEC 联系字段不进入任何输出。

## 时间查询

- `holdings_as_of` 为持仓日期，`available_at_utc` 为包含完整明细的申报受理时间，不能把季度末当成公开日期。
- N-PORT XML 的 `repPdDate` 是本次持仓日期，`repPdEnd` 是财年结束日期；核对时验证实际日期、基金系列和份额类别。数据库季度日期来自 SEC submissions，并与 28 份原始 XML 的 `repPdDate` 一致。
- 每份完整公开版本均有历史名单和策略名单，包括非 canonical 年报及 N-PORT 修订；较早版本不会被丢弃。
- 查询 T 时，取 T 前已公开的最近报告期，再取该期最新已公开版本。旧季度的晚到修订不覆盖更新季度。
- 首份公开前返回无名单；下一份公开前沿用上一份。若最近公开版本 blocked，返回明确 blocked 与非零退出码，不静默假装可用。
- canonical 仅用于每季度核对和标准 CSV 导出，不限制时间查询。当前状态过滤不是历史当时已知事实，必须同时保留 `status_cutoff`。
- 本库不声称知道两次申报之间的每日真实成分，也不代表证券每天均可交易。

```powershell
python -m leadtrend_db query --quarter 2019-12-31
python -m leadtrend_db query --as-of 2020-03-15T00:00:00Z
```

## 表与字段

完整定义见 `leadtrend_db/schema.sql`。

| 表 | 核心内容 |
|---|---|
| sources、snapshots、holdings | 原始来源、持仓日期、公开时间、原始名称及标识、股数、金额、当季发行人、当季证券类型 |
| issuers、issuer_names、issuer_links | 发行人、名称历史、带生效日期和证据的法律继任 |
| securities、security_identifiers、security_issuer_spans | 证券身份、代码、历史发行人归属；securities 中为最近已核实的类型和归属 |
| pool_versions | 公开版本、canonical 标志、替代关系、ready/blocked 及原因 |
| company_ranks | 全部符合边界公司的金额和排名，含第 200/201 名核对 |
| top200_companies、top200_securities | 当季历史 Top 200 公司及其实际持有类别 |
| strategy_companies、strategy_securities | 应用状态截止日后的保留、排除、阻断与当前证券投影 |
| quarter_checks | 每版本解析、排序、边界及策略完整性检查 |
| review_items、build_log、meta | 复核证据、构建记录、规则/代码版本、原始清单及数据内容哈希 |

`fund_weight_raw` 为申报持仓金额除以基金总投资；`pct_val_percent` 为 N-PORT 原始净资产百分比；`stock_weight` 为正股票金额归一化。负数行单列，不能进入正股票分母或公司排名。REIT 虽被候选池排除，仍属于基金股票金额。

## 构建和验收

```powershell
python -m leadtrend_db build --offline --db work/top200_codex/candidate.sqlite
python -m leadtrend_db check --db work/top200_codex/candidate.sqlite --skip-rebuild
python -m leadtrend_db promote --from work/top200_codex/candidate.sqlite
python -m leadtrend_db export
```

`promote` 核对档案、数据库完整性、所有季度检查、公司与证券对应，并执行一次离线重建。任一期 blocked、任一季度检查失败、可选证券缺当前标识/代码，均拒绝提升。
日期和规则固定于 `rules.py`；本次状态截止 2026-10-26，申报截止 2026-10-27T00:00:00Z。更新批次须明确更换截止日期和来源版本，不覆盖既有原始档案。

核验包括：SQL 独立重算所有公开版本排名；独立遍历原始 N-PORT XML 比对每一股票行的名称、标识、股数和金额；2019-12-31 双来源公司及证券核对；边界与类别检查。
名称模糊候选不是穷尽证明。未解析金额保守地考虑同时归入同一公司、以及改变入选公司内部名次的可能性。只要无法证明不影响，就阻断。
当前正式结果见计数摘要及本次交付报告，不以旧版文档数字作为依据。

## 导出

`data/db/exports/top200_v1/` 下：

- `eligible_quarterly_stocks.csv`：最直接的全季度可选股票名单，每季度 canonical 版本，含公司排名、历史身份、当前投影和公开时间。
- `latest_eligible_stocks.csv`：最新一期可选股票。
- `strategy_top200_companies.csv`、`strategy_top200_securities.csv`：canonical 名单，包含排除及原因，不补位。
- `historical_top200_companies.csv`：全部公开版本，带 canonical 标志；按季度核对时必须区分版本。
- `historical_top200_securities.csv`、`pool_versions.csv`：历史类别及版本来源。
- `rank_boundary_180_220.csv`、`quarter_checks.csv`、`blocking_review.csv`、`unresolved_lines.csv`：核对明细。
- `security_union.csv`：各季度可选历史证券联集，用于后续准备数据，不能作为每季共同名单。

后续填行情时以历史 `security_id`、实际日期及有证据的代码历史为准，不能把当前继任股票的价格直接填到前身证券。
第一阶段行情表为空；第二阶段由 OpenD 原始不复权日线与公司行动填充。精确指数调整表仍为空。未运行策略、实验或保留段。

## 投资者永久排除名单与合并规则（top200_v2）

唯一配置为 `config/pool_exclusions_v1.csv`，含全部 15 个发行人主键、投资者决定与日期。构建读取该文件，写入 `pool_exclusions` 及 `meta.rules`、配置 sha256。所有公开版本均先应用永久排除，原因固定为 `investor_permanent_exclusion`，不补位；原始持仓、金额和历史排名不变。历史审计表包含排除记录，策略输入只使用 `strategy_eligible=1` 或 query 的 `eligible_companies` / `eligible_securities`。

同 CIK 改名、换代码，以及单一公司股东固定比例换股的迁册或控股重组算延续。合并只保留法律存续且上市行情不断的一方；被收购方、并入新主体的对等合并各方视为退市。新合并公司自首次实际持仓起算，不继承前身季度。`issuer_start` / `issuer_end` 独立保留法律主体的生效区间，删除继任关系不改变历史持仓归属；`continuity_ratio` 记录已核实的延续换股比例。其余判断与证据写入 `db_overrides_v1.csv`。

SEC 与 OpenD 的本次原始观察由 `config/db_verification_sources_v2.json` 固定，`ticker_checks` 保存来源与逐只核验。三份证据一致支持 Skydance 的 SKYD，无 PSKY。XOM 当前为 ExxonMobil Holdings Corp（CIK 2115436，NYSE）；其 2026-07-01 接替 CIK 34088。最新持仓仍为 2026-06-30，故新发行人在下一份自身持仓公开前显示 listed_out_of_index，历史股票通过核实的重组关系投影 XOM。

2019-12-31 年报与 N-PORT 股数约 0.3% 的差异保留，不做修补；公司集合和排名核对不因此改变。

## 原始行情与公司行动

股票池排名和价格来源分开。行情自 2012-12-01 起，OpenD 日线严格 `AuType.NONE`，保存 open/high/low/close/volume/turnover 与来源主键，不保存厂商复权价。原始返回及复用的 v05 NONE 原文件只追加到 `data/db/raw/opend/`，清单记录 sha256、UTC、字节数；可完全离线重建。行情配置和收集命令见后续同节追加的实际实现。

拆股、合股、分拆、现金与特别分红存 `corporate_actions`。价格覆盖按公开时间至下一公开时间，以及此前 252 个 NYSE 交易日分别记录；新上市历史不足、不可取代码、无法核实的历史序列不填造。当前 ticker 复用不等于历史证券相同，必须保存日期与身份核验结果。保留段行情只做覆盖、身份与异常检查，不计算策略信号或收益。
