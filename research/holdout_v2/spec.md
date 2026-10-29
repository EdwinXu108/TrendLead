# 保留段一次性运行登记（holdout_v2）

2026-10-30。投资者授权 `research/holdout/authorization.json`：`scope` 含 `wf_v2`、`boll_pf_v1`、`boll_pf_v2`、
`boll_pf_v3`、`pick_v1`，`window` 为 `["2024-07-01", "2026-06-30"]`。

**保留段已被部分看过（wf_v2 跑过一次，另外有几次描述性统计用到过这段数据），所以这次的结果只作参考，不作检验。**
所有参数和规则冻结为开发段登记的样子，保留段不参与任何挑选。

**只跑一次。跑完发现代码错误，停下写进交付报告，不得自行重跑。报告只写数字。**

## 1. 背景

开发段（2014-02-28 至 2024-06-28）已经做完四批实验。投资者要看它们原样放到保留段的表现：
前 10 权重优先、关注区里集中到高联动这一路（boll_pf_v1、v2、v3），以及选股网格（pick_v1）。

## 2. 数据库

`data/db/leadtrend_db_full_v2.sqlite`，由 `data/db/leadtrend_db_full_v1.sqlite`（含 `tri_seam_v1`）加 `top50_v1`
的 `scope_members` 生成，`file_sha256` 为 `dba9da7ec4d7b2bf320a13fec68284fabd1d1600e5e1b3874a10c4527b901ba1`。
`scope='top50_v1'`，每周用当周生效的版本；同一家公司只取一个代表证券。

## 3. 保留段口径

- **历史连续计算**：所有的量都从 2014-02-28（以及更早的库内数据）一路连续算到 2026-06-30，包括周收益、贝塔、
  波动、Hann 中轨、市场关注区等级、TILT_B 与 TILT_C 的集中状态和“准备”状态。不在 2024-07-01 重新开始，
  所以 2024-06-28 那周的集中状态原样带进保留段。
- **组合（v1、v2、v3 的各条线）**：以 2024-06-28 收盘为起点，本金 100,000 美元，按当周目标权重建仓；
  之后每周和开发段一样重新分配，算到 2026-06-30；VOO 线同样从 2024-06-28 起买入持有。
  指标用 `ma_pool.portfolio_metrics`。“2020 年内、2022 年内回撤”在保留段为空；
  另报 2024（7 月起）、2025、2026（到 6 月）三个自然年的收益和比 VOO 多出的收益。
- **选股网格（pick_v1 的 259 个配置）**：信号周从 2024-06-28 起，k+H 不晚于 2026-06-30 最后一个信号周的才用；
  共同周、关注区分段、按自然年分组都与开发段规则一样；胜基准的基准仍是 ALL10。

## 4. 要跑的配置（全部照各批登记，不新增、不删减）

| 批次 | 模块 | 配置 | 登记 |
|---|---|---|---|
| boll_pf_v1 | `leadtrend_bt/boll_pf.py` | VOO、EW50、EW10、CW10、TILT_A、TILT_B 共 6 条 | `research/boll_pf_v1/configs.csv` |
| boll_pf_v2 | 同上（TILT_C） | TILT_C 1 条 | `research/boll_pf_v2/configs.csv` |
| boll_pf_v3 | `leadtrend_bt/pick_pf.py` | VOO、EW10、TILT_B*、WGT5、WB5、EW10_VOL、WGT5_VOL、WB5_VOL 共 8 条 | `research/boll_pf_v3/configs.csv` |
| pick_v1 | `leadtrend_bt/pick.py` | 259 个配置（WGT 4 + 7 种依据 × 12 × 3 W + ALL10/ALL20/ALL50） | `research/pick_v1/configs.csv` |

## 5. 输出

- `research/holdout_v2/summary.json`、`report.md`；
- pick_v1 的全表 `research/holdout_v2/pick_results.csv`，列与 `research/pick_v1/results.csv` 相同；
- 报告开头写“保留段已被部分看过，结果只作参考，不作检验”，只写数字，不写“有效 / 无效”。

## 6. 代码

`leadtrend_bt/ma_lab.check_holdout` 增加 `batch` 参数（默认 `'wf_v2'`）；`ma_pool.load_market`、
`boll_weekly.market_zone`、`boll_pf.load/run`、`pick.load`、`pick_pf.run` 增加 `allow_holdout` 与 `batch`，
默认关闭，开发段结果一个数字都不变。运行脚本 `research/holdout_v2/run_holdout.py`，测试 `tests_bt/test_holdout_v2.py`。
