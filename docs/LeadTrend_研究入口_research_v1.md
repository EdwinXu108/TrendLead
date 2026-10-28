# LeadTrend 研究入口使用说明（正式库 development_release_v1 + 回测协议 research_v1）

写给任何要在本项目上写代码、跑实验的模型。2026-10-27 经 Claude 审阅。先读 `AGENTS.md`（投资边界与规则），再读本文。

## 1. 用什么

| 项 | 值 |
|---|---|
| 正式库 | `data/db/leadtrend_db_v1.sqlite`（发布标识 `development_release_v1` + `pool_exclusions_v2` + `spgi_fill_v1`，股票池规则 `top200_v2`） |
| 文件 SHA256 | `4e46f93f6c7e836dbd5e30dfe3ecfd8ebbb076180ac2d0397c4f8789381862bc` |
| 内容 SHA256 | `4c6e53e9240e11f743577160f4c7dc818eba555677e11d450da5bd67641c173b`（`meta.content_sha256`） |
| 回测协议 | `research_v1`（默认）。`legacy_v1` 只用于复现旧结果，必须显式指定 |
| 数据接口 | `leadtrend_db/api.py`（只读） |
| 回测引擎 | `leadtrend_bt/`（`engine.py` 规则，`runner.py` 输出，`cache.py` 带校验的缓存入口） |
| 开发段 | 2014-02-28 至 2024-06-28（股票池首期 2013-12-31；行情从 2012-12 起，供回看） |

**2026-10-27 投资者排除两个证券：** ACE Ltd（`CUSIP:H1467J104`）与 Comcast Special A（`CIKCLS:0001166691:CL_A_SPECIAL`）不再进入任何季度的股票池，理由与登记见 `config/pool_exclusions_v2.csv`。只改策略名单（`strategy_securities`、`strategy_companies`）与描述性的 `corr_groups`，价格、TRI、公司行动与覆盖事实一律未动；`meta.pool_exclusion_release = pool_exclusions_v2`，配置哈希记在 `meta.pool_exclusion_config_sha256`。Comcast 公司保留（CMCSA 不受影响）。

**2026-10-27 补齐 SPGI 的 2016 年改名缺口：** OpenD 在 2016-04-28 至 2016-05-23 没有数据，2016-05-24 还是“前收盘减分红”的占位行。用 Yahoo Finance 的 17 个交易日补齐（原始返回归档在 `data/db/raw/yahoo/`，价格乘以 1.057 抵消 Yahoo 错误记录的 1057:1000 拆股），2016-04-28 用前收盘占位（`tri_daily.flags = placeholder_carry`），2016-05-24 换成真实价 111.66。新增 18 行价格、重建该证券的 TRI、刷新两张覆盖表；`meta.price_fill_release = spgi_fill_v1`。

**正式库坏了或丢了**，从封存基库按三步重新迁移，结果逐字节相同：

```text
python -X utf8 -B -m leadtrend_db.development_release --base data/db/releases/leadtrend_db_base_7564971e.sqlite --out work/rebuilt_v1.sqlite --manifest work/rebuilt_v1_manifest.json
python -X utf8 -B -m leadtrend_db.pool_exclusion_release --base work/rebuilt_v1.sqlite --out work/rebuilt_v2.sqlite --manifest work/rebuilt_v2_manifest.json
python -X utf8 -B -m leadtrend_db.spgi_fill_release --base work/rebuilt_v2.sqlite --out work/rebuilt.sqlite --manifest work/rebuilt_manifest.json
```

第一步约 2 分钟。确认无误后再替换 `data/db/leadtrend_db_v1.sqlite`。`data/db/releases/` 下的两个快照（`leadtrend_db_development_v1_c1a508a5.sqlite`、`leadtrend_db_pool_exclusions_v2_83c71cbe.sqlite`）只作对照，不要用于研究。

## 2. 必须遵守

1. **不使用保留段（2024-07-05 起）。**
   - 引擎、缓存入口和 `api.benchmark_bars` 都会拒绝结束日晚于 2024-06-28；
   - 但 `api.tri_series`、`api.pool_at`、`api.open_series` 没有截止日保护，直接调用时，`end` 必须自己限定在 2024-06-28 以内。
2. **截止日之后的总回报不可直接使用。**正式库只修正了开发段：有 17 只证券的 `tri` 水平在 2024-06-28 与之后的行之间不连续（例如 DD 约 −24%）。任何跨过截止日的计算都会出错。动用保留段之前，必须先另做一次覆盖全期的数据发布。
3. **不要运行 `python -m leadtrend_db build/check/promote` 来生成或替换正式库。**
   - 完整构建用的是新的现金规则，未经全量验证，得到的会是一个不同的库；
   - `promote` 现在默认拒绝覆盖已发布的正式库；
   - 要改数据，就像 `leadtrend_db/development_release.py` 那样，做一个新的、有登记的迁移发布。
4. **投资者的决定**（不讨论，也不提醒“不严谨”）：
   - 已退市公司与 `config/pool_exclusions_v1.csv` 中的公司不进股票池，不补位；
   - 不使用行业或板块标签。
5. **策略实验流程：**
   - 先与投资者讨论；
   - 把配置、指标、判断方式写进 `research/<批次>/spec*.md` 并提交（事先登记），并在该批的 `trials.csv` 追加配置；
   - 再改代码；改动后，旧的主运行要逐字节回归；
   - 然后运行，报告只写数字。
6. **git：**只用隐私邮箱 `133124291+EdwinXu108@users.noreply.github.com` 提交；不推送，推送需经审阅与投资者同意；`data/`、`work/` 不进库。

## 3. 跑一个回测

**命令行**（推荐，自带缓存与输出校验）：

```powershell
python -X utf8 -B -m leadtrend_bt run --config config/bt_research_confirmation_v1.json --db data/db/leadtrend_db_v1.sqlite --out work/my_runs
```

第一次运行返回 `computed`，约 30–40 秒；之后输入不变则返回 `reused`。库、配置、引擎代码或现金与事件配置有任何变化，都会自动重算；任何一个输出文件损坏，也会重算。

**Python：**

```python
from leadtrend_bt.cache import cached_run
cfg = dict(run_id='MY_RUN', mode='momentum', ranking='momentum', L=90, N=10, b=2, K=4, rebalance='R2',
           buy_weighting='equal', start='2014-02-28', end='2024-06-28', capital=100000.0,
           fee_per_order=1.0, cost_bps=10, protocol='research_v1')
out = cached_run(cfg, db='data/db/leadtrend_db_v1.sqlite', out_root='work/my_runs')
print(out['status'], out['summary']['excess_cagr'])
```

`cached_run` 只接受 `research_v1`；`run_id` 只能包含字母、数字、`_` 和 `-`。

## 4. 配置字段

| 字段 | 取值与含义 | 默认 |
|---|---|---|
| `mode` | `momentum`（按信号选股）；`pool`（被动持有前 `top_n` 家，每个版本生效后只交易差额）；`voo`（买入持有基准）；`random` / `random_size`（随机对照，需要原配置的 `plan`，见 bt_v2 的驱动脚本） | `momentum` |
| `ranking` | `momentum`（`tri_t / tri_{t−L} − 1`，需要 L+1 日完整总回报）；`size`（按公司的基金持仓金额）；`signal`（按 `signal` 指定的信号） | `momentum` |
| `signal` | `ranking='signal'` 时必填，取值见 `leadtrend_bt/signals.py` 的 13 个信号：`MOM_126`、`MOM_252`、`MOM_12_1`、`REV_5`、`REV_21`、`LOWVOL_126`、`HIGHVOL_126`、`DIST_52W`、`TREND_MA200`、`VOLUME_SURGE`、`AMIHUD_126`、`FUND_ACCUM`、`FUND_RANK_UP` | 无 |
| `L` | 动量回看的交易日数（`ranking='signal'` 时不影响信号，但配置里保留） | 90 |
| `N` | 每个子账户的持仓数 | 10 |
| `b` | 保留缓冲：排名不低于 b×N 就继续持有 | 2 |
| `K` | 分批子账户数（子账户 j 在第 j 个信号周建仓，之后每 K 周检查一次） | 1 |
| `rebalance` | `R2`：卖出所得买新股，保留的股票不调仓；`R1`：有换股的检查日恢复等权；`R1W`：每个检查日都恢复等权 | `R2` |
| `execution` | `next_open`：下一交易日开盘价成交；`close`：信号日收盘价成交（信号与成交同一时点） | `next_open` |
| `fractional` | 是否允许小数股；不设时 `pool` 与 `voo` 为真，其余为假 | 按 mode |
| `max_missing_days` | 回看窗口允许缺几个交易日（0 或 1）；不设时 `research_v1` 为 1、`legacy_v1` 为 0 | 按协议 |
| `buy_weighting` | `equal`：新买入等额；`amount`：新买入按公司持仓金额分配 | `equal` |
| `top_m` | 只在持仓金额前 M 家里评分 | 不限 |
| `w` | 合成分数 = w × 规模百分位 + (1 − w) × 动量百分位 | 不用 |
| `top_n`、`weighting` | `pool` 模式：前 N 家；`cw` 按金额加权，`ew` 等权 | 全部、`cw` |
| `cost_bps`、`fee_per_order`、`capital` | 单边滑点（bp）、每笔手续费、本金 | 0、1.0、100000 |
| `start`、`end` | 信号起止日；`end` 不得晚于 2024-06-28 | — |

**共同口径：**
- 每周最后一个交易日收盘出信号，下一交易日开盘成交（`execution='close'` 时改为信号日收盘成交），先卖后买，整股（`pool`、`voo` 允许小数股）；
- 现金收益为 0；基准为 VOO 总回报，与策略在同一天同一价格买入；
- 同一公司的多个类别只取持仓金额最大的可选类别。

## 4.1 信号层默认口径（2026-10-28 投资者决定）

以后所有信号研究默认用这一层，目的是快速确认"有没有信号"，**接受不严谨**。定义见 `AGENTS.md`，代码见 `leadtrend_bt/signal_layer.py`：

| 项 | 值 |
|---|---|
| 成交 | 周五收盘出信号，当天收盘价成交（`execution='close'`） |
| 成本 | 零（`cost_bps=0`、`fee_per_order=0.0`） |
| 股数与权重 | 小数股、绝对等权（`fractional=True`） |
| 缓冲与分批 | 无（`b=1`、`K=1`） |
| 等权维护 | 每个检查日拉回 1/N（`rebalance='R1W'`） |
| 区间 | 2014-02-28 至 2024-06-28，一整段，不分时段 |
| 本金 | 100,000 美元 |

`signal_layer_config(run_id, **overrides)` 生成完整配置，固定字段不可覆盖（只有 `ranking`、`signal`、`L`、`N`、`rebalance`、`top_m`、`w`、`buy_weighting` 可以给）。`research_v1` 的默认值不变（仍是 `next_open` 且带成本），因为回测层还没定。

跑一个零成本组合的最短代码（信号层模拟一般直接调 `runner.run`，`files=False` 只写 `config.json` 与 `summary.json`，不必走缓存）：

```python
from leadtrend_bt.runner import run
from leadtrend_bt.signal_layer import signal_layer_config

cfg = signal_layer_config('REV_5_N10', ranking='signal', signal='REV_5', N=10)
out = run(cfg, out_root='work/my_runs', db_path='data/db/leadtrend_db_v1.sqlite', write=True, files=False)
print(out['summary']['excess_cagr'])
```

整批实验用 `research/mom_v1/run_m2.py`、`run_m3.py`（支持 `--only RUN_ID[,RUN_ID...]`），全部配置见 `research/mom_v1/configs_m2_m3.csv`。

## 5. 输出（`<out>/<run_id>/`）

`summary.json`（全部指标：CAGR、相对 VOO 超额、波动、夏普、回撤及日期、最长水下、逐年、两个时段、换手、集中度等）、`daily_equity.csv`、`trades.csv`、`positions.csv`、`weekly_decisions.csv`、`corporate_actions.csv`（每笔公司行动的现金流）、`score_counts.csv`（每个信号日可评分的样本数），以及 `complete.json`（缓存校验）。

## 6. 已知限制

- **ACE 与 CMCSK 已于 2026-10-27 移出股票池**（见第 1 节），它们原先的缺价不再影响任何运行。
- **SPGI 的 2016 年缺口已补齐**（见第 1 节）：17 天用 Yahoo 真实行情，2016-04-28 用前收盘占位。`pool` 模式不再因它报 `DataGapError`。
- **剩余的单日缺口：** BIIB 2020-11-06、REGN 2015-06-09、VRTX 2015-05-12。yfinance 复核确认 Yahoo 也只有平盘占位行（成交量 600 / 12,100 / 569,700），没有第二来源。自 2026-10-27 起，评分规则允许回看窗口缺 1 个交易日（`max_missing_days=1`，`research_v1` 默认），分数仍用两端点，并在 `weekly_decisions.csv` 的 `window_missing` 标 1；这三个缺口不再导致整只证券被排除 L 个交易日。持有中缺 1 天仍按最后收盘价标市（`positions.csv` 的 `stale`），连续缺 2 天报错停止。
- **遇到未解决的公司行动会报错停止，不会默默算错。**目前开发段没有未解决的公司行动。
- **少数分拆仍按有标识的代理估值**（复权因子或开盘缺口），见 `corporate_actions.resolution`。
- **新库与旧库的经济结果：**TERRA_R1 的 28 条运行已核对，复用成立（见 `work/handoff/` 中的审阅记录）。2026-10-27 之后的两项变更会改变结果：排除 ACE/CMCSK（对 `MOM_EQ_10bp` 只改排名与池内公司数）与“缺一日容忍”（同一配置的 10bp 超额由 0.33% 变为 0.46%，成交 3830→3820 笔，17 个“信号日 × 证券”用到该容忍）。旧结果不再与当前引擎逐字节相同。
