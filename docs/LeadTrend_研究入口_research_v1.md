# LeadTrend 研究入口使用说明（正式库 development_release_v1 + 回测协议 research_v1）

写给任何要在本项目上写代码、跑实验的模型。2026-10-27 经 Claude 审阅。先读 `AGENTS.md`（投资边界与规则），再读本文。

## 1. 用什么

| 项 | 值 |
|---|---|
| 正式库 | `data/db/leadtrend_db_v1.sqlite`（发布标识 `development_release_v1`，股票池规则 `top200_v2`） |
| 文件 SHA256 | `c1a508a5b3fb219d526d0c935e5e5b60ac385c9134c887c189909deb08bf0147` |
| 内容 SHA256 | `092f9076a27554d8d7c228f4d70595159cb3a45ad59cd5a646eb38afd6cd412e`（`meta.content_sha256`） |
| 回测协议 | `research_v1`（默认）。`legacy_v1` 只用于复现旧结果，必须显式指定 |
| 数据接口 | `leadtrend_db/api.py`（只读） |
| 回测引擎 | `leadtrend_bt/`（`engine.py` 规则，`runner.py` 输出，`cache.py` 带校验的缓存入口） |
| 开发段 | 2014-02-28 至 2024-06-28（股票池首期 2013-12-31；行情从 2012-12 起，供回看） |

**正式库坏了或丢了**，从封存基库重新迁移，约 2 分钟，结果逐字节相同：

```powershell
python -X utf8 -B -m leadtrend_db.development_release --base data/db/releases/leadtrend_db_base_7564971e.sqlite --out work/rebuilt.sqlite --manifest work/rebuilt_manifest.json
```

确认无误后再替换 `data/db/leadtrend_db_v1.sqlite`。

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
| `ranking` | `momentum`（`tri_t / tri_{t−L} − 1`，需要 L+1 日完整总回报）；`size`（按公司的基金持仓金额） | `momentum` |
| `L` | 动量回看的交易日数 | 90 |
| `N` | 每个子账户的持仓数 | 10 |
| `b` | 保留缓冲：排名不低于 b×N 就继续持有 | 2 |
| `K` | 分批子账户数（子账户 j 在第 j 个信号周建仓，之后每 K 周检查一次） | 1 |
| `rebalance` | `R2`：卖出所得买新股，保留的股票不调仓；`R1`：有换股的检查日恢复等权 | `R2` |
| `buy_weighting` | `equal`：新买入等额；`amount`：新买入按公司持仓金额分配 | `equal` |
| `top_m` | 只在持仓金额前 M 家里评分 | 不限 |
| `w` | 合成分数 = w × 规模百分位 + (1 − w) × 动量百分位 | 不用 |
| `top_n`、`weighting` | `pool` 模式：前 N 家；`cw` 按金额加权，`ew` 等权 | 全部、`cw` |
| `cost_bps`、`fee_per_order`、`capital` | 单边滑点（bp）、每笔手续费、本金 | 0、1.0、100000 |
| `start`、`end` | 信号起止日；`end` 不得晚于 2024-06-28 | — |

**共同口径：**
- 每周最后一个交易日收盘出信号，下一交易日开盘成交，先卖后买，整股（`pool`、`voo` 允许小数股）；
- 现金收益为 0；基准为 VOO 总回报，与策略在同一天开盘买入；
- 同一公司的多个类别只取持仓金额最大的可选类别。

## 5. 输出（`<out>/<run_id>/`）

`summary.json`（全部指标：CAGR、相对 VOO 超额、波动、夏普、回撤及日期、最长水下、逐年、两个时段、换手、集中度等）、`daily_equity.csv`、`trades.csv`、`positions.csv`、`weekly_decisions.csv`、`corporate_actions.csv`（每笔公司行动的现金流）、`score_counts.csv`（每个信号日可评分的样本数），以及 `complete.json`（缓存校验）。

## 6. 已知限制

- **ACE（Chubb，CUSIP:H1467J104）2015-11-30 至 2016-01-14 缺 32 个交易日行情。**当时它排在第 116–124 名：
  - `pool` 模式的 `top_n` 超过约 115 时（含全池）会报 `DataGapError` 并停止，前 10、20、50、100 家不受影响；
  - 动量配置不受影响（缺价期间不满足完整回看，不参与评分）；
  - 怎么处理（例如把这几个季度从股票池中去掉）需投资者决定。
- **遇到未解决的公司行动会报错停止，不会默默算错。**目前开发段没有未解决的公司行动。
- **少数分拆仍按有标识的代理估值**（复权因子或开盘缺口），见 `corporate_actions.resolution`。
- **新库与旧库的经济结果：**TERRA_R1 的 28 条运行已核对，复用成立（见 `work/handoff/` 中的审阅记录）。
