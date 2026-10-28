# LeadTrend 交接报告

日期 2026-10-28。写给接手的 Claude：读完这份文档，应当能独立接手这个项目，知道数据是什么、代码在哪、跑过什么、结论是什么、哪些坑不能再踩。

配套文档（按顺序读）：

1. `AGENTS.md` —— 投资者定下的边界与规则，最高优先级；
2. `docs/LeadTrend_研究入口_research_v1.md` —— 正式库与回测协议的使用说明；
3. 本文；
4. `research/mom_v1/report_2026-10-28.md` —— 最近一轮实验的完整报告。

---

## 0. 一屏速览

| 项 | 现状 |
|---|---|
| 项目 | 美股，股票池 = VOO 各季度实际持仓前 200 家公司，周五收盘选股，等权持有，基准 VOO |
| 目标 | 通过选股跑赢 VOO（10 年 VOO 年化 13.00%） |
| 正式库 | `data/db/leadtrend_db_v1.sqlite`，352 MB，schema 6，content `4c6e53e9…`，file `4e46f93f…` |
| 开发段 | 2014-02-28 至 2024-06-28（保留段 2024-07-05 起，**一次都没用过**） |
| 代码 | `leadtrend_db/`（数据+接口）、`leadtrend_bt/`（回测引擎+信号） |
| 测试 | 200 通过（tests_db 116 + tests_bt 84） |
| 最硬的结论 | **这个池子里没有可用的动量信号**（13,440 个信号格 IC 全程 <0.01） |
| 最强的苗头 | 短期反转（零成本 +3.8~+5.8%/年，但年换手 4749%）、12-1 动量（+0.4~+4.7%/年） |
| 相关性 | top50 平均两两相关 0.371，50 只 ≈ 4–6 个独立注，加名字买不到分散 |

---

## 1. 我接手时的状态（2026-10-27 凌晨）

**数据库**：schema 5，`rule_version = top200_v2`，文件 `data/db/leadtrend_db_v1.sqlite`。

已经做好的部分（不是我的功劳，是前一轮 Codex 的工作）：

- 52 个季度版本（2013-12-31 至 2026-06-30），全部 ready，含公开时间精确到秒；
- 811 只证券 / 781 个发行人 / 26,328 条持仓明细；
- 每季 Top 200 公司排名（按基金持仓金额，不是市值）；
- 策略名单 `strategy_securities`（已应用 `status_cutoff` 退市过滤）；
- 复核表、构建日志、原始来源清单与哈希。

**没做好的部分**：

| 缺口 | 后果 |
|---|---|
| `prices_daily`、`corporate_actions`、`tri_daily` 三张表全空 | 只有名单，没有行情，跑不了任何回测 |
| 没有总回报序列（TRI） | 分红、拆股、分拆全部未处理，直接算价格收益会错 |
| 没有基准 | VOO 只在旧管线里有，口径不一致 |
| 没有只读查询接口 | 研究代码必须直接摸表，容易误读历史版本 |
| 没有回测引擎 | 旧引擎在 `leadtrend_v05/`，与本库 schema 不兼容 |
| 现金与分拆未对账 | 分红金额与除权日未逐条核对、33 条分拆未估值、复核表 495 行未闭环；直接算价格收益会错 |

**当时已有的资产**：`leadtrend_v05/`（旧管线，已冻结，不得修改）、`tests_v05/`、`research/v05/`（旧实验，72+ 次已登记尝试）、`data/v05/`（旧数据，只读）。这些是**历史参照**，不是当前研究入口。

---

## 2. 数据库：从 schema 5 到 6

五个阶段，每个阶段都可独立复现。前两个阶段建数据，后三个阶段是有登记的迁移发布。

### 2.1 行情与公司行动（commit 72fb470）

- 从 OpenD 归档（`data/db/raw/`）采集 2012-12 至 2026-10 的日线：**1,131,129 行**，含开高低收、成交量；
- 公司行动 **13,937 条**：现金分红、拆股、分拆、类别分配；
- 建立 `price_coverage` / `price_quarter_coverage`（每个季度版本 × 每只证券的覆盖事实）与 `price_lineage`（价格归属链）。

### 2.2 总回报序列与基准（commit e88387e）

- 新建 `tri_daily`（总回报指数，**1,134,623 行**），含 `BENCH:VOO` 基准；
- 新建 `leadtrend_db/tri.py`：按公司行动把价格序列折算成总回报，分红按除权日再投资、拆股按比例调整、分拆按子公司价值分配；
- 新建 `leadtrend_db/corr.py`：按长期股价相关性做 12 组聚类（**描述性，绝不作为交易输入**）；
- 新建 `leadtrend_db/api.py`：只读接口（`pool_at`、`tri_series`、`trading_days`、`friday_signal_days`、`rf_daily`、`benchmark_bars`、`close_series`、`volume_series`…）。

**验证**：TRI 对富途复权因子一致率 99.72%（非分红日 99.997%），对旧 v05 序列（2019-05 起）99.993%。未达 99.9% 的原因已查明：富途自身的现金字段与复权因子互相矛盾，不是建库逻辑错误。

### 2.3 现金与分拆对账（`development_release_v1`，commit fe0a6ed）

这一层是**有登记的迁移发布**，不是原地改库：

| 内容 | 处理（数字以当前库为准） |
|---|---|
| 13,805 条现金分红 | 27 条按官方来源覆盖（`official_cash_override_v1`）；109 条端点金额与厂商因子不一致，已标注（`endpoint_amount_vendor_disagrees`），不阻断 |
| 33 条分拆 | 17 条用有记录的子公司收盘价 × 比例、4 条经核实的子公司收盘价；其余 12 条为代理估值 |
| 90 条拆股、7 条反向拆股、2 条类别分配 | 按比例折算 |
| Google 类分配、GE HealthCare / GE Vernova 分拆、DowDuPont 分拆 | 按子公司实际收盘价重新估值 |
| Cigna 2018、Avago 2016 | 按投资者规则恢复为"收购方延续"（代码延续、行情连续） |
| 15 行 `no_price_data` | 移出策略名单（不补位） |

输入是 2.1 / 2.2 产出的封存基库 `leadtrend_db_base_7564971e.sqlite`（content `97b55e45…`）；输出 content `092f9076…`。

**关键教训**：引擎的数据库分支必须调用 `_prepare_actions()` 装载公司行动，否则拆股与分红完全不生效。这个 bug 曾造成 **5%/年** 的假性拖累，修正后所有旧结论作废重跑。

### 2.4 排除 ACE 与 CMCSK（`pool_exclusions_v2`，commit fa488b7）

投资者决定：两只永久移出股票池，不补位。

- 做法：新增 `config/pool_exclusions_v2.csv` + `leadtrend_db/pool_exclusion_release.py`，**只改策略名单**（`strategy_securities` 45+1 行、`strategy_companies` 45 行、`corr_groups` 40 行）；
- 价格、TRI、公司行动、覆盖事实**一律未动**（物理删价格会破坏可重建性与封条）；
- 同配置新旧库对比：`daily_equity`、`trades`、`positions`、`corporate_actions` 逐字节相同；只有排名与池内公司数（174→173）变化。

### 2.5 补齐 SPGI + 缺一日容忍（`spgi_fill_v1`，commit 6c8f915）

**成因**：OpenD 的 SPGI 序列在 2016-04-28 至 05-23（改名窗口）自身缺 18 天，且 2016-05-24 是"前收盘减分红"的占位行。

**处理**：

- 用 Yahoo 的 17 天真实行情补齐（原始返回归档 `data/db/raw/yahoo/`），价格乘 1.057 抵消 Yahoo 错误记录的 1057:1000 假拆股；
- 2016-04-28 用前收盘占位（`tri_daily.flags = placeholder_carry`）；
- 2016-05-24 换成真实价 111.66；
- 重建该证券 TRI，刷新两张覆盖表（`active_missing 0`、`warmup_missing 0`）。

**同时改了评分规则**：回看窗口允许缺 1 个交易日，用两端点算动量并在 `weekly_decisions.window_missing` 标记（`max_missing_days=1`，`research_v1` 默认；`legacy_v1` 为 0）。效果：`MOM_EQ_10bp` 超额 0.33%→0.46%，REGN 2015-07-02 由"无排名强制卖出"变为"继续持有"。

### 2.6 当前库的封条与可重建性

```text
python -X utf8 -B -m leadtrend_db.development_release --base data/db/releases/leadtrend_db_base_7564971e.sqlite --out work/rebuilt_v1.sqlite --manifest work/rebuilt_v1_manifest.json
python -X utf8 -B -m leadtrend_db.pool_exclusion_release --base work/rebuilt_v1.sqlite --out work/rebuilt_v2.sqlite --manifest work/rebuilt_v2_manifest.json
python -X utf8 -B -m leadtrend_db.spgi_fill_release --base work/rebuilt_v2.sqlite --out work/rebuilt.sqlite --manifest work/rebuilt_manifest.json
```

三步跑完，结果与正式库**逐字节相同**。`data/db/releases/` 下的两张快照只作对照，不要用于研究。

| 阶段 | content_sha256 前 8 位 | 关键变化 |
|---|---|---|
| 封存基库 | `97b55e45` | schema 6，价格 + TRI + 相关性分组 |
| development_release_v1 | `092f9076` | 现金/分拆对账 |
| pool_exclusions_v2 | `82426047` | 排除 ACE/CMCSK |
| **spgi_fill_v1（正式）** | **`4c6e53e9`** | 补 SPGI + 缺一日容忍 |

---

## 3. 数据库还剩什么问题（诚实清单）

| 问题 | 范围 | 影响 | 状态 |
|---|---|---|---|
| 单日行情缺失 | BIIB 2020-11-06、REGN 2015-06-09、VRTX 2015-05-12 | Yahoo 也只有平盘占位（成交量 600/12,100/569,700），无第二来源 | 已被"缺一日容忍"覆盖 |
| 分拆按代理估值 | 33 条分拆中 12 条（8 条复权因子、4 条除权日开盘缺口） | 估值有误差，已在 `corporate_actions.resolution` 标注 | 接受 |
| 无行情而移出 | 15 行，当前对应 3 个历史代码（CB、CMCSA、JCI） | 不补位 | 投资者决定 |
| 截止日台阶 | 17 只证券的 TRI 在 2024-06-28 与 07-01 之间不连续（如 DD 约 −24%） | **只在跨截止日计算时出错** | 未修，动保留段前必须先做一次全期发布 |
| 幸存者偏差 | 池子只含截至 2026-10-26 仍在上市的公司 | 结果不含被收购/退市的公司，收益偏高 | 投资者决定，不再讨论 |
| 季度披露延迟 | 新进入 VOO 的股票最多晚一个季度入池 | 结构性限制 | 无法解决 |

**一句话**：开发段的数据在日频选股上已经够用，误差集中在 12 条分拆代理估值和 3 个单日缺口（已容忍），不存在"会算错却不知道"的隐藏问题。

---

## 4. 回测引擎 `leadtrend_bt`

| 文件 | 职责 |
|---|---|
| `engine.py` | 规则：选股、成交、现金、公司行动、子账户（K）、缓冲（b）、再平衡（R1/R2/R1W）、随机对照 |
| `signals.py` | 13 个信号，矩阵实现（动量、反转、波动、距高点、趋势、量能、非流动性、基金加仓/排名） |
| `runner.py` | 输出与核心加卫星组合 |
| `cache.py` | 带校验的运行缓存（输入不变则 `reused`） |
| `signal_study.py` | 信号层诊断（IC、NW t、分组、换手代理） |

**配置字段**：`mode`(momentum/pool/voo/random/random_size)、`ranking`(momentum/size/signal)、`L`、`N`、`b`、`K`、`rebalance`(R1/R1W/R2)、`top_n`、`top_m`、`w`、`buy_weighting`、`execution`(next_open/close)、`max_missing_days`、`cost_bps`、`fee_per_order`。

**投资者在 2026-10-28 改过的口径**（写入 `research/mom_v1/spec.md`，不再讨论）：

- 成交价 = **周五收盘价**（信号与成交同一时点，盘后价格忽略）；
- 零成本；小数股；绝对等权；本金 10 万美元；
- 排名跌出前 N 就卖，空出的名额买当天最高的；**无缓冲（b=1）、无分批（K=1）**；
- 不分 A/B 两段；不预设任何阈值。

**引擎里被修正过的缺陷**（都是真实 bug，会重复踩）：

1. 数据库分支漏装公司行动 → 5%/年假性拖累；
2. 随机对照持仓失控（会变成 N+1）→ 强制卖出 + 按原配置 sell 数补卖 + 补足 N，并在成交层阻止顺延卖单；
3. 集中度按子账户分别计算 → 必须先跨子账户合并同一证券再除权益（MOM_T4 5.11%→19.66%）；
4. 股票池版本变更时全额换手 → 只交易目标市值差额（413.6%→~35%）；
5. `_MARKET_CACHE` 漏带 `signal_scores` → 同一信号第二次运行不交易；
6. `DIST_52W` 数组形状错误、滚动窗口未含当日；
7. 十分位方向反了（top/bottom 互换）。

**回归纪律**：每次改引擎，旧的主运行四个 CSV + summary 必须逐字节相同。这条救过好几次。

---

## 5. 实验全景

| 批次 | 内容 | 规模 | 主要结果 | 文件 |
|---|---|---|---|---|
| bt_v1 | 6 个基线（VOO、全池等权/加权、前十大、动量） | 220 次 | 前十大加权 **+4.84%**；MOM_A90/T4 **+0.36%**；全池等权 −1.63% | `research/bt_v1/baseline_summary.md` |
| bt_v2 L0 | 规模曲线：前 10–200 家，等权/加权 | 20 次 | 加权 10 家 +4.84% → 200 家 −0.12%；等权全线更差 | `research/bt_v2/level0_size_curve.md` |
| bt_v2 L1 | 信号层诊断：7 个动量 × 3 个前向窗口 | 无回测 | 短期动量 IC 为负（−0.02）；只有前 50 家内微正 | `research/bt_v2/level1_signal_study.md` |
| bt_v2 L2 | 组合骨架曲面 L×N | 1,908 次（含 1,800 次随机） | 全期超额 −3.34% 到 **+3.39%** | `research/bt_v2/level2_surface.md` |
| bt_v2 L3 | 范围 / 合成评分 / 按金额配权 / 核心卫星 | 1,248 次 | MOMCW **+5.87%**（但单股权重 42%）；W025 +4.13% | `research/bt_v2/level3_size_momentum.md` |
| mom_v1 M1 | 信号扫描：55 回看 × 28 前向 × 3 范围 × 3 算法 | **13,440 格** | **动量 IC 全程 <0.01**；短期反转最强 | `research/mom_v1/m1_signal_scan.md`、`scan_summary.csv` |
| mom_v1 M2 | 55 回看 × 6 持股数 × 2 等权（零成本、收盘成交） | 660 次 | 每种模式只有 **29/330** 跑赢 VOO，中位 −2.2% | `research/mom_v1/m2_grid.md`、`m2_grid.csv` |
| mom_v1 M3 | 13 信号 × 6 持股数 | 78 次 | 见下 | `research/mom_v1/report_2026-10-28.md`、`m3_signals.csv` |
| corr_v1 | top50/top100 股价相关性 | 43 时点 | 平均 ρ 0.371，十年无趋势 | `research/corr_v1/report_2026-10-28.md` |

### 5.1 M3 的 13 个信号（年化超额，相对 VOO 13.00%，零成本）

| 信号 | N=5 | N=10 | N=30 | 年换手(N=10) | 判断 |
|---|---|---|---|---|---|
| `REV_5` | +3.76 | +4.50 | +5.82 | **4749%** | 最强，但成本吃掉 |
| `REV_21` | −2.31 | +1.84 | +4.64 | 2607% | 同上 |
| `MOM_12_1` | +4.69 | +1.61 | +0.58 | 975% | 唯一全正的动量 |
| `HIGHVOL_126` | +0.59 | +0.09 | +1.54 | 479% | 与低波异象相反 |
| `MOM_126` | +1.41 | −3.66 | −1.81 | 1223% | 无 |
| `MOM_252` | −1.27 | −3.59 | −1.79 | 933% | 无 |
| `LOWVOL_126` | −5.13 | −3.56 | −2.10 | 516% | 全负 |
| `DIST_52W` | −7.83 | −5.74 | −3.62 | 3444% | 52 周高点反向 |
| `FUND_ACCUM` | −11.61 | −8.07 | −1.21 | 420% | 最差 |

### 5.2 相关性（corr_v1）

| 范围 | 平均 ρ | 最低 | 最高 | 独立注（算术 / 特征值） |
|---|---|---|---|---|
| top50 | **0.371** | 0.152 | 0.631 | 2.83 / 6.41 |
| top100 | 0.360 | 0.150 | 0.613 | 2.96 / 7.10 |
| 全池 | 0.349 | 0.153 | 0.590 | 3.05 / 7.61 |

结构性配对：MA–V 0.862、BAC–JPM 0.862、CVX–XOM 0.797、KO–PEP 0.715。最挤 2020-06-02（0.631，89% 配对 >0.5），最松 2017-11-30（0.152）、2024-05-30（0.186）。

---

## 6. 文件索引

| 要找什么 | 去哪 |
|---|---|
| 投资边界与规则 | `AGENTS.md` |
| 正式库怎么用、有哪些坑 | `docs/LeadTrend_研究入口_research_v1.md` |
| 数据库规则（Top200 怎么来的） | `docs/LeadTrend_数据库_v3_Top200.md` |
| 引擎字段与输出 | `docs/LeadTrend_回测引擎_bt_v1.md` |
| 最近一轮实验完整报告 | `research/mom_v1/report_2026-10-28.md` |
| 所有登记过的配置 | `research/*/trials.csv`、`research/*/spec*.md` |
| 相关性 | `research/corr_v1/` |
| 数据只读接口 | `leadtrend_db/api.py` |
| 运行输出 | `work/`（不进 git，可随时删；已从 8.2 GB 清到约 400 MB） |

---

## 7. 我的判断

**1. 数据层已经不是瓶颈。** 开发段 10 年、336 只证券、113 万行日线、含分红拆股的总回报、可逐字节重建。剩下的误差（12 条分拆代理、3 个单日缺口）量级远小于任何策略差异。

**2. 池子本身限制了上限。** 这是 162–200 只超大市值、季度才更新、只含幸存者的名单。很多经典异象（低波、52 周高点、动量）在这里不成立，不是实现问题——信号层和组合层互相印证：13,440 格 IC 全程 <0.01，13 个信号 78 个组合里动量全在零附近。

**3. 唯一稳定的正结果是"短期反转"，但它被换手吃掉。** REV_5 零成本 +4.5%/年，年换手 4749%，10bp 单边就是 4.7%/年成本。这不是"发现了 alpha"，是"发现了一个刚好被成本抵消的效应"。

**4. 12-1 动量是唯一值得继续看的动量变体。** +0.39~+4.69%，6 个持股数全为正，换手 975%。它与"短期反转、中期动量"的经典结构一致，也是唯一在信号层和组合层都勉强站得住的。

**5. 前十大金额加权的 +4.84% 不是 alpha，是集中度。** 单股权重 27%、前五家 78%，β 1.12。它在 2023 年赚 28% 超额、2022 年亏 17%。MOMCW 的 +5.87% 单股权重 42%，同理。这两个数字不能当作"策略有效"。

**6. 相关性解释了为什么加名字没用。** 50 只 ≈ 4–6 个独立注，把持股数从 10 加到 50 只多 0.5 个注。要真正分散，必须按相关性分组限制，而不是增加数量。

**7. 最该警惕的是多重检验。** 至今跑过约 1.7 万次（13,440 个信号格 + 660 网格 + 78 组合 + bt_v1/v2 的 3,376 次回测）。任何单个 +4~+6% 的配置，都在这个规模下属于正常噪声。

---

## 8. 交给 Claude 的规则与坑

1. **不碰保留段**（2024-07-05 起）。引擎和 `api.benchmark_bars` 会拒绝，但 `api.tri_series` / `pool_at` / `open_series` **没有保护**，`end` 必须自己限死在 2024-06-28。
2. **不交易**。不创建 `research/v05/holdout/authorization.json`，不调用任何下单接口。
3. **不改** `leadtrend_v05/`、`tests_v05/`、`data/v05/`、`research/v05/`、`AGENTS.md`、`research/bt_v1/`、`research/bt_v2/` 的已有文件。
4. **不用 `build/check/promote` 生成或覆盖正式库**。要改数据就像 `spgi_fill_release.py` 那样做有登记的迁移发布。
5. **先登记再跑**：写 `research/<批次>/spec.md` → 提交 → 再改代码 → 再运行。改引擎后旧主运行必须逐字节回归。
6. **只用本地提交**，作者 `EdwinXu108`，提交后 `git status` 必须干净；不推送。
7. **不用行业或板块标签**（投资者已否决，改用长期股价相关性）。
8. **零付费数据**。yfinance 只能临时装在 `work/pyext`，不能污染环境、不能进依赖。
9. **Python 用 `D:\Tools\miniconda3\python.exe -X utf8`**，不新增第三方依赖。
10. **不要把"跑赢"当成"有效"**。这个项目里每个正结果都要先过随机对照、起点稳健、成本敏感三关。

**操作坑（我踩过）**：

- 用 `python script.py | head` 会因 BrokenPipe 中途杀死进程，CSV 停在上一版。重跑必须重定向到日志文件。
- `api.pool_amounts` 里 Alphabet 的 GOOGL/GOOG 同属一个 issuer（`CIK:0001652044`），任何"按公司取前 N"的代码都必须按 `issuer_id` 去重，否则同一家公司会以 ρ≈0.99 混进配对。
- `tests_v05` 需要把 `TMPDIR` 指到 `work/` 才能全通过（系统临时目录在仓库外会被 safe_output 拒绝）。
- 单轮删除超过 50 个文件会被 safe-delete 钩子拦下，需先 rename 到仓库外再清理。

---

## 9. 建议的下一步（按优先级）

| 优先级 | 事项 | 理由 |
|---|---|---|
| 1 | **成本敏感度**：REV_5 / MOM_12_1 / HIGHVOL 在 5/10/20/30bp 下的超额 | 现在所有正结果都是零成本，这是最可能翻盘的地方 |
| 2 | **相关性分组约束**：按 1−ρ 分组，每组最多买 m 只，重跑 M3 的 78 个组合 | 已有分组方法与数据，能直接回答"分散能不能改善结果" |
| 3 | **12-1 动量的稳健性**：换 N、换窗口、起点推迟、随机对照 | 唯一全正的动量变体，值得一次正式检验 |
| 4 | 若要做择时：只在投资者把回撤列为首要目标后再做 | 当前主指标是相对 VOO 的超额，不是回撤 |
| 5 | 保留段：全部策略轮次结束后，**只用一个事先选定的配置检验一次** | 规则见 `AGENTS.md`，任何模型不得提前动用 |

---

## 10. 复现清单

```bash
# 测试
D:\Tools\miniconda3\python.exe -X utf8 -m pytest tests_db tests_bt -q      # 200 passed

# 一次回测
D:\Tools\miniconda3\python.exe -X utf8 -B -m leadtrend_bt run --config config/bt_research_confirmation_v1.json --db data/db/leadtrend_db_v1.sqlite --out work/my_runs

# 相关性分析
D:\Tools\miniconda3\python.exe -X utf8 research/corr_v1/corr_study.py

# 信号层扫描（已归档，不必重跑，约 2 分钟）
D:\Tools\miniconda3\python.exe -X utf8 research/mom_v1/scan.py
```

工作区干净，未推送。本文档随最新一次提交进入仓库。
