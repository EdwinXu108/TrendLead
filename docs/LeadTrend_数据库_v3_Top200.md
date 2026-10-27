# LeadTrend 季度 Top 200 数据库（schema 5）

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

### 行情实际表与命令（schema 5）

`python -m leadtrend_db fetch-prices` 只收集原始返回，不提升正式库。随后按上面的 build --offline → check → promote → export 流程完成验收。`config/db_prices_v1.json` 固定本批原始索引、日期与 v05 比较参考，索引本身也在只追加清单内；收集日志/可恢复状态在 work/top200_v2，删除 work 不影响离线重建。

- prices_daily：原始 OHLC、volume、turnover，source=opend_none；兼容列 adj_close 强制 NULL。SDK 的 AuType.NONE 序列化值为字符串 None，两者均明确表示不复权，拒收 QFQ/HFQ。
- corporate_actions：action_date、action_type（split/reverse_split/spinoff/class_distribution/cash_dividend/special_dividend）、新股/旧股 ratio、每股 USD cash_amount、source_id 与 notes。现金优先使用已核对的分红端点；较早历史用 rehab 明确的 per_cash_div/special_dividend 补足，未知支付日留空。同日不同金额不盲目相加，进入复核。分拆价格调整因子不当作母股股数比例。
- price_calendar：NYSE 交易日期与早收市标记。OpenD 返回的日历截于最近十年；2012-12 至 2016 年依官方历史休市资料补足，日期集合再与三只 NYSE 股票 JNJ、KO、IBM 的原始日线独立核对；配置和来源见 config/db_nyse_calendar_v1.json。
- price_coverage：每证券的日期范围、日线行数、来源；detail 含全部公开版本的覆盖计数、252 日回看计数、缺失计数和 v05 比较计数。
- price_quarter_coverage：每个实际生效公开版本 × 可选证券，公开至下一公开版本（最后一期截至收集日）内的预期与实有 NYSE 交易日、此前 252 日预期与实有、缺失日期。公开时间早于当日 09:30 ET 可用于当日开盘；其余从下一交易日生效。完整标志也要求已核实跨越的法律延续。该标志只评价价格覆盖，不代表公司行动复核全部关闭。
- price_lineage：历史/当前证券、OpenD 当前代码、身份区间、申报价值÷股数锚点校验或隔离原因。支持的是有证据的同一股票延续，原始价格不换算为复权序列。
- price_continuity：法律/代码变更日、换股比例、前后原始收盘与 pass/unverified。TYC→JCI 为每 1 股原 Tyco 换 0.955 股新普通股；不能当作 1:1。新 JCI 代码 2016-09-06 起生效；ACE→CB 是 2016-01-15；BK→BNY 是 2026-05-21。
- price_anomalies：无相应公司行动可解释的相邻可用收盘变动超过 40%；隔日缺失的情况另注明 gap_sessions，不冒称单日变动。price_comparison 保存与 v05 逐日重叠字段的全部差异（v05 未提供 high/low，故不可比较这两项）。它们都是数据质量检查。

同一延续股票的历史证券键与当前键可以指向同一原始连续系列，price_lineage 明示其公司根及法律关系；新合并主体的最早价格不得早于其成立/上市边界。当前证券代码在厂商上可能带着其他公司的历史，基金每季金额÷股数与原始收盘不符的区间整体隔离。已确认复用的 CB/JCI 在换代码前一律拒收当前代码序列；CMCSK 类别在转为 CMCSA 以前必须有自己的价格，不能以 CMCSA 替代。旧代码取不到时保留缺口。BNY 分红端点混入旧同名封闭式基金的记录，只有银行 rehab 可印证的现金记录才能纳入。

导出新增 price_coverage.csv、price_quarter_coverage.csv、price_lineage.csv、price_continuity.csv、price_anomalies.csv、price_comparison.csv、corporate_actions.csv、price_calendar.csv、price_review_list.csv、ticker_checks.csv、pool_exclusions.csv，均在 data/db/exports/top200_v1/。价格完整度和复核结果以本次报告及数据库为准，不把“请求成功”写成“全部历史齐全”。

### 本批实际计数

51季度、52公开版本均ready；canonical可选公司季度行9536，可选历史证券联集337。原始日线1,124,521行、340个历史/当前证券键；覆盖及252日回看同时完整9707/9789，其余82条留有明确缺口。13,883条公司行动中60条同日现金项目标记needs_review。34条未被公司行动解释的40%变动保留；v05重叠608,004个证券交易日比较差异0。详细事实见work/handoff/codex_top200_v2.md（本地交付报告）。

## schema 6（2026-10-27）：行动收尾、总回报序列、基准与查询接口

规则版本仍为 `top200_v2`；schema 升到 6。新增内容不改变名单、排名、排除与策略资格口径，只补齐公司行动、加入总回报序列与基准、加入只读查询接口，并把"可选期内没有任何行情"的证券季度移出策略名单。

### 收购方延续（投资者 2026-10-27 补充规则）

收购方股东按固定比例（通常 1:1）换成新设控股公司的股票、且上市代码与行情延续的，收购方算延续；被收购方仍算退市。据此恢复两条继任链接，写在 `config/db_overrides_v1.csv`：

- `CIK:0000701221`（Cigna Holding Co，旧 Cigna）→ `CIK:0001739940`（Cigna Group），2018-12-20，比例 1:1；
- `CIK:0001441634`（Avago Technologies）→ `CIK:0001649338`（Broadcom Ltd，新加坡），2016-02-01，比例 1:1；再经已有链接到 `CIK:0001730168`（Broadcom Inc）。

被收购方 Express Scripts（`CIK:0001532063`）与 Broadcom Corp（`CIK:0001054374`）仍在投资者永久排除名单内，保持退市，不恢复。恢复前用数据库原始收盘核对：当前代码 CI 与 AVGO 的日线在旧主体各季度连续覆盖，事件日前后单日收盘变动绝对值均小于 20%。

### 公司行动（`corporate_actions` 新列）

| 列 | 含义 |
|---|---|
| `sub_ticker` | 分拆出的子公司代码，多个用逗号分隔；无法确定时为空 |
| `vendor_factor` | 富途复权记录中该分拆的价格因子（母公司保留的比例），仅作估值证据 |
| `spin_value` | 除权日每股母公司分得的子公司价值（美元），= 比例 × 子公司当日收盘价 |
| `resolution` | 该行金额或比例所用的判定方法（见下） |

判定顺序（投资者 2026-10-27 要求）：先看富途 rehab 的每股现金字段；同日多条金额时，候选为端点合计、常规、特别、两者之和，取与富途自身价格调整最接近（2% 以内）的一个；仍不能确定的用复权因子隐含金额。支付日未知留空，总回报序列只用除权日。股票分红叙述（`1股派0.00275股`）解析为 1.00275 的拆股。分拆优先用 v05 归一化档案中"每股子公司股数 × 子公司收盘价"，与复权因子隐含价值相差 20% 以上时改用复权因子。复权记录完全缺失的四个分拆（BAX 2015-07-01、EBAY 2015-07-20、HPQ 2015-11-02、YUM 2016-11-01）登记在 `config/spinoff_overrides_v1.csv`，按除权日开盘缺口估值。

每交易日的"原始收盘比 vs 复权因子隐含调整"逐条比对，差异超过 2% 的写入 `review_items`（`price_action_scan`）。

### 总回报序列（`tri_daily`，method `event_v1`）

字段：`security_id, session_date, close, tri, daily_return, method, flags`。首日 `tri = 1`，其后

```
daily_return_t = (close_t × 拆股因子_t + 现金分红_t + 分拆价值_t) ÷ close_{t−1} − 1
```

拆股与合股在除权日按比例还原前一日价格；现金分红按上节确定的金额在除权日计入；分拆按 `spin_value`，缺失时用复权因子隐含价值（flags `vendor_factor`），再缺失时用除权日开盘缺口（flags `price_drop_implied`）。非交易日的行动顺延到该证券的下一个交易日。缺日的证券日标 `gap`。

`security_id` 为 `securities.security_id`，基准为 `BENCH:VOO`（VOO 不进股票池、不写 `prices_daily`）。

验证：(a) 与富途复权因子推出的调整后收盘逐日比较；(b) 2019-05 起与 `data/v05/normalized` 按同一公式逐日比较。结果见交付报告与 `work/bt_v1/tri_validation.json`。

### 无行情证券季度

可选期内一个价格都没有的"证券 × 季度"从策略名单移除：`strategy_securities.reason = no_price_data`，公司若因此没有可用类别则 `strategy_companies.excluded_reason = no_price_data`，不补位。只是回看期不足 252 日的证券保留，仅在 `price_quarter_coverage` 标 `incomplete`。

### 相关性分组（`corr_methods`、`corr_groups`）

`corr_groups(version_id, method_id, security_id, group_id)` 记录每个股票池版本生效日的描述性分组。`method_id = corr_v1`，参数写在 `corr_methods.definition`：504 个交易日窗口、至少 252 个观测、按当日可选证券等权平均去市场、距离 = 1 − 相关系数、平均连接、12 组。分组只用于描述，不进入任何策略或交易。

### 查询接口（`leadtrend_db/api.py`）

全部只读（SQLite `mode=ro`）：

| 函数 | 返回 |
|---|---|
| `pool_at(day)` | 该交易日生效的可选证券与公司、版本号、生效区间 |
| `tri_series(ids, start, end)` | 每只证券的总回报序列 `(日期, tri, 日收益)` |
| `open_prices(ids, day)` / `open_series(ids, start, end)` | 原始开盘价（次日开盘成交用） |
| `trading_days(start, end)` | NYSE 交易日 |
| `friday_signal_days(start, end)` | 每周最后一个交易日 |
| `rf_daily(start, end)` | DTB3 日利率（百分比 / 100 / 360，按日历日） |
| `pool_windows(start, end)` / `pool_amounts(version_id)` | 版本生效区间与基金持仓金额（权重组合用） |


## 开发段数据发布 `development_release_v1`

本发布只修复至 2024-06-28 的开发段及必要前置历史，后段行以机械摘要保留；不得据此使用保留段。持久封存基库、`config/db_cash_overrides_v1.json`、`config/db_corporate_events_v1.json` 和 `research/bt_v3/TERRA_R1/development_release_v1.json` 是版本化迁移输入。旧的无界 build/check/promote 会触及全库，不用于此发布。

从封存基库重建候选（需要现有 VOO 原始档案；输出目录必须为空）：

```powershell
python -B -m leadtrend_db.development_release --base data/db/releases/leadtrend_db_base_7564971e.sqlite --out work/closeout_v1/rebuilt.sqlite --manifest work/closeout_v1/rebuilt_manifest.json
```

公共回测默认为 `research_v1`，要求指定该发布数据库并拒绝超出开发截止日。历史口径必须显式指定 `legacy_v1`，仅用于识别旧结果。新协议读取已迁移事件，不再叠加旧 TERRA 私有补丁。某些未核实分拆估值仍是明确标记的代理；冲突如进入评分窗口或持仓则停止。ACE 2015-11-30 至 2016-01-14 的价格缺口尚未补齐，涉及的全池路径安全阻断，不补位。

一次已登记配置的冷启动/复用示例（不依赖旧 work manifest）：

```powershell
python -B -m leadtrend_bt run --config config/bt_research_confirmation_v1.json --db data/db/leadtrend_db_v1.sqlite --out work/closeout_v1/confirmation
```

每次运行以 `run_id` 选择单个配置。完成缓存校验配置、数据库身份、经济代码与输入摘要和全部输出摘要；任一不符则重新计算，不缓存失败。数据库同路径切换后只读连接和内存数据按身份失效。运行明细和本地数据库不提交、不推送。
