# 真实数据近似实验：存在幸存者偏差、当前行业分类；72 个配置为事先登记的全部尝试

## 本轮补数时点与授权披露

**本轮补齐数据，发生在已经看到 58 个完成运行的结果之后，其中包括 L=90 当时已完成的9个5 bp运行全部跑赢VOO这一情况。零碎股的近似口径和 VSCO 的数据来源，由 Claude 提出、投资者确认，并且是在看到这 86 个运行的任何完整开发段结果之前确定的；判定规则没有任何改动。** 原先中断前的部分路径及失败原因已知，不能声称完全未见路径数据。VSCO零碎股是在本轮另行取得投资者确认后才扩展同一口径，扩展确认前未启动这20项含VSCO零碎股的新口径重跑；当时其他3事件的工程完成状态和BBWI评分阻断原因已知，新增完整收益/排名尚未查看。本轮不运行保留段。

旧报告、旧总表、旧决定及验收已存档于 `stage4_remediation/previous_summary/`。旧决定作废原因及哈希见 `decision_supersession.json`。原登记、哈希、72 配置、邻接定义、条件(a)～(d)、排序规则保持不变。补数版本与原登记数据分层，原有7个规范化文件和第二阶段登记哈希均未改。

原登记 SHA256：`fc144d5ab5d82322a1092cb9c63ddc2ae864ea92cd0da4b07910eecf9e8fea10`。辅助版本 SHA256：`2770c5dae8383a9ba3e792473e7c1402653753e33e0ca3ecb051bcfe6f24f93e`。

**数据版本需重点审阅：** 为解除BBWI评分阻断，本轮补充了真实新股估值覆盖；原58项按授权未重算，144行总表包含原58与新86的数据版本差异。哈希不变不等于旧58在新评分输入下重算也相同。详见文末局限。

## VSCO 身份、行情与辅助数据版本

缺口是查询旧代码造成的。公司2026-05-21的8-K宣布2026-06-02从VSCO改为VSXY，NYSE上市及CUSIP不变；CIK1856437、CUSIP926400102、ISIN US9264001028。已保存SEC原始8-K及新闻附件、2021分拆8-K及含926400102的2022年13G原件、OpenD证券资料和日线响应、Yahoo chart响应及URL/取得时间/SHA256。

OpenD旧US.VSCO返回“未知股票”；US.VSXY返回Victoria’s Secret、US_NYSE、正常STOCK。listing_date字段1970-01-01明显是供应商占位值，未用于上市日判断；正常交易起点取官方2021-08-03公告。以AuType.NONE获取2021-08-03至2024-06-28共731根日线，分页键None。OpenD复权表为空，Yahoo split事件为空，未发现分拆以来拆合股。

两个来源重叠 731 日，最大开盘绝对差 0.000100244141 美元，最大收盘绝对差 0.000003662109 美元。账本只用OpenD的NONE原价，Yahoo只作交叉验证，未使用adjclose。

新增 `data/v05/normalized/auxiliary_vsco_prices_v2.csv`，security_id=CHILD:VSCO、selectable=0。辅助证券只在实际分得后持有、估值、卖出；没有加入历史名单；父股仅通过下述已核实分拆估值覆盖更新评分输入。`data_version.json`列出新增文件及依赖文件哈希，加载时逐个核验；新股历史价格始终限制在开发段。补齐真实新股价值后，还通过`score_event_overlay.json`将BBWI原vendor_factor评分事件切换为58.23/3=19.41美元/母股的child_raw_close估值，沿用原5.2节公式；原事件CSV未改，未绕过持仓缺评分的停止规则。此前工程重放暴露的8次评分停止及39次重放产物全部归档，并以统一的评分覆盖输入重新运行本轮86项。

官方来源：[VSCO更名8-K](https://www.sec.gov/Archives/edgar/data/1856437/000185643726000007/vsco-20260521.htm)、[更名公告](https://www.sec.gov/Archives/edgar/data/1856437/000185643726000007/ex991vscomay212026pressrel.htm)、[分拆公告](https://www.sec.gov/Archives/edgar/data/1856437/000114036121026649/nt10026698x8_ex99-1.htm)。原件和元数据在`sources/`，完整SHA256清单见`evidence_sha256.json`。

## 零碎股现金：来源、口径与金额

官方优先核查公司公告、分拆FAQ、Form 8937及附件、8-K。GEHC/GEV Form8937给的是税基分配参考价，不能冒充实际零碎股售出价；SOLV Form8937明确合并出售并分配现金，但没有给统一支付日。OCC曾披露GEHC期权交割的58.85204美元价格，但其期权结算安排不能证明本普通股账户的到账日，因此未套用为已核实的普通股结算。本轮各事件均如实标记approximation。原件含GEHC/GEV Form8937及附件、SOLV Form8937及公司分拆文件，均有URL/取得时间/SHA256；OCC信息未用于估值。

| 子股/有效交易日 | 估值价（美元/整股） | 可用现金日 | 口径 |
|---|---:|---|---|
| CUSIP:36266G107 / 2023-01-04 | 60.49000000 | 2023-01-19 | approximation |
| CUSIP:36828A101 / 2024-04-02 | 140.00000000 | 2024-04-16 | approximation |
| CUSIP:83444M101 / 2024-04-01 | 69.10000000 | 2024-04-15 | approximation |
| CHILD:VSCO / 2021-08-03 | 58.23000000 | 2021-08-17 | approximation |

现金=不足1股的数量×首个regular-way原始收盘价，不扣费用。有效分配交易日确认应收，严格在其后第10个交易日转可用现金；不把when-issued的第一条报价当作regular-way价格。整股正常入持仓、行业暴露，并于下一次合法周检参加去留；母股股数不变。

GEHC官方公司法分配是在2023-01-03收盘后，现有事件规范化为2023-01-04的有效交易日；本轮按投资者明确给定的2023-01-04及10个后续交易日执行。VSCO同理2021-08-02收盘后对应2021-08-03有效交易日。这是本模型交易日口径，不声称实际代理到账如此。

每笔/每运行的金额、确认时权益、占权益比例、到账日、近似标记见`fractional_cash_events.csv`；每运行近似总额见`fractional_cash_per_run.csv`及各运行`fractional_cash.csv`、`summary.json`。

| 子股 | 证券/成本运行事件数 | 单笔现金最小/最大（美元） | 占确认权益最高比例 |
|---|---:|---:|---:|
| CHILD:VSCO | 20 | 19.41000000 / 38.82000000 | 0.02493014% |
| CUSIP:36266G107 | 8 | 20.16333333 / 40.32666667 | 0.04282078% |
| CUSIP:36828A101 | 36 | 35.00000000 / 105.00000000 | 0.09777947% |
| CUSIP:83444M101 | 18 | 17.27500000 / 51.82500000 | 0.04424044% |

超过权益0.1%的单笔数：0。不同配置/成本是互斥实验，跨运行金额相加不代表某个账户的实际收入。

## 只重跑原未完成运行与账本验收

原58个完成运行未重跑，逐文件哈希保持不变；原86个未完成运行各自重跑，旧现场移动归档而非删除；另有39次同点工程重放归档，详见score_input_repair_001.json及engineering_replay_001。最终144成本运行状态：{'completed': 144}。72个参数配置仍是全部策略尝试；补数重跑不算新参数尝试。原170次开跑日志保留，另有39次补充评分输入前的工程开跑（调度命令中断后归档，成本运行都有finish记录），以及改为分阶段核对58目录哈希时的一次审计/检查点中断与断点续跑（该成本计算已结束，原件保留，额外重放一次）；详情见verification_cadence_restart_002.json，完成检查点均直接续用，不重跑已完成结果。累计start/finish=296/296。

全部完成运行独立账本验收PASS。每日资产恒等式最大误差0；最低现金$0.0332704000739；零碎股现金独立复算最大误差0。新审计独立按原价、股数、交易日计算零碎金额和到期日，逐日重建应收与可用现金；同ID不重复确认/支付；到账前资金不进入买入预算。保留原9类账本/基准/日期检查。

未完成原因：
无。

## 按窗口分组的完成情况与年化超额收益

年化超额=策略CAGR−同区间VOO CAGR。下表单位是百分点，未完成不填补收益；完整原始小数及144行逐点数字见总表和`by_window.csv`。

| L | 滑点bp | 完成/12 | 未完成 | 跑赢数 | 年化超额最小 | 中位数 | 最大 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10 | 5 | 12 | 0 | 0 | -12.73085223 | -10.58445106 | -10.08531984 |
| 10 | 10 | 12 | 0 | 0 | -15.83191254 | -13.69548197 | -13.20339070 |
| 20 | 5 | 12 | 0 | 0 | -13.99052880 | -12.52823133 | -11.32088439 |
| 20 | 10 | 12 | 0 | 0 | -16.19194221 | -14.58512007 | -13.62997494 |
| 40 | 5 | 12 | 0 | 0 | -11.84094410 | -9.17079698 | -1.52120810 |
| 40 | 10 | 12 | 0 | 0 | -13.26838817 | -10.67849015 | -2.67028245 |
| 60 | 5 | 12 | 0 | 0 | -10.66314225 | -9.27658344 | -6.94496088 |
| 60 | 10 | 12 | 0 | 0 | -11.78046000 | -10.20013575 | -7.83513162 |
| 90 | 5 | 12 | 0 | 11 | -0.65303562 | 2.84559686 | 6.40449662 |
| 90 | 10 | 12 | 0 | 10 | -1.30087135 | 2.07572565 | 5.44111940 |
| 126 | 5 | 12 | 0 | 1 | -5.10418303 | -2.25754370 | 3.26833660 |
| 126 | 10 | 12 | 0 | 1 | -5.55246715 | -2.85357759 | 2.71493927 |

## R1 与 R2 同参数成对比较

完整72组成对比较见`R1_R2_paired.csv`，逐项列L/N/b/成本、两者年换手与年化超额、R2减R1差值。下面仅对同一参数点双边都完整的对子统计，防止用不同子样本冒充配对。换手为单边年换手倍数；超额差单位百分点。

| 成本bp | 完整对子/36 | R2换手更低对子 | 换手差中位数(R2−R1) | R2超额更高对子 | 超额差最小/中位/最大 |
|---:|---:|---:|---:|---:|---:|
| 5 | 36 | 36 | -0.75266615 | 32 | -0.28706317 / 0.49121104 / 3.86953632 |
| 10 | 36 | 36 | -0.75497438 | 34 | -0.19491070 / 0.63589424 / 3.96540076 |

## 原规则重新判定与保留段

条件满足数量：a=11；b=2；c=7；d=72。候选区域点数=1；唯一选定点=['L090_N08_b2_R2']。

**出现满足原登记条件的唯一选定点；本轮不授权运行保留段。**

该点10bp年化超额为+5.44111940个百分点，但自身及相邻点的10bp平均年化超额仍为−0.10308142个百分点。原登记没有要求这个平均值必须为正，因此没有自行加门槛或放宽门槛；这也不应被描述为整个邻域都跑赢VOO。

所有72点的逐项证据、相邻点、平均值均写入`region_criteria.csv`。若有唯一选定点，它只是原规则选择的代表点，不是向投资者推荐最佳参数。决定文件明确`holdout_execution_authorized=false`、`holdout_permitted=false`，待Claude审阅和投资者确认；命令和底层上下文都有本轮硬性拒绝。没有调用run-holdout，没有保留段输出目录或执行锁，没有计算保留段信号、成交、权益、收益或基准。

候选点原始判据：

| 配置 | 压力年化超额pp | 5bp相邻获胜/总数 | 压力获胜完整年数 | 自身+邻点压力超额均值pp |
|---|---:|---:|---:|---:|
| L090_N08_b2_R2 | 5.44111940 | 2/3 | 3 | -0.10308142 |

热力图PNG及对应CSV已按原4组更新；仍未完成的点保持NA，未放宽邻点分母或均值规则。

## 已完成58个运行逐项哈希核对

封存文件`before_rerun.json`保存每个产物相对路径及完整SHA256；下面每行比较该目录的全部文件集合与每个哈希（包括命令日志、配置、所有CSV/JSON）。不只比较收益数字。

| 原完成运行 | 文件数 | 逐文件SHA256及集合 |
|---|---:|---|
| L010_N08_b2_R2/cost_10bps | 21 | PASS |
| L010_N08_b2_R2/cost_5bps | 21 | PASS |
| L010_N08_b3_R2/cost_10bps | 21 | PASS |
| L010_N08_b3_R2/cost_5bps | 21 | PASS |
| L010_N10_b3_R1/cost_5bps | 21 | PASS |
| L020_N08_b2_R1/cost_10bps | 21 | PASS |
| L020_N08_b2_R1/cost_5bps | 21 | PASS |
| L020_N08_b2_R2/cost_10bps | 21 | PASS |
| L020_N08_b2_R2/cost_5bps | 21 | PASS |
| L020_N08_b3_R1/cost_10bps | 21 | PASS |
| L020_N08_b3_R1/cost_5bps | 21 | PASS |
| L020_N08_b3_R2/cost_10bps | 21 | PASS |
| L020_N08_b3_R2/cost_5bps | 21 | PASS |
| L020_N10_b2_R1/cost_10bps | 22 | PASS |
| L020_N10_b2_R1/cost_5bps | 22 | PASS |
| L020_N10_b2_R2/cost_10bps | 21 | PASS |
| L020_N10_b2_R2/cost_5bps | 21 | PASS |
| L020_N10_b3_R1/cost_10bps | 21 | PASS |
| L020_N10_b3_R1/cost_5bps | 21 | PASS |
| L020_N10_b3_R2/cost_10bps | 21 | PASS |
| L020_N10_b3_R2/cost_5bps | 21 | PASS |
| L020_N12_b2_R1/cost_10bps | 21 | PASS |
| L020_N12_b2_R1/cost_5bps | 21 | PASS |
| L020_N12_b2_R2/cost_10bps | 21 | PASS |
| L020_N12_b2_R2/cost_5bps | 21 | PASS |
| L020_N12_b3_R1/cost_10bps | 21 | PASS |
| L020_N12_b3_R1/cost_5bps | 21 | PASS |
| L020_N12_b3_R2/cost_10bps | 21 | PASS |
| L020_N12_b3_R2/cost_5bps | 21 | PASS |
| L040_N08_b2_R2/cost_10bps | 21 | PASS |
| L040_N10_b3_R1/cost_5bps | 21 | PASS |
| L040_N12_b2_R2/cost_5bps | 21 | PASS |
| L040_N12_b3_R1/cost_5bps | 21 | PASS |
| L040_N12_b3_R2/cost_5bps | 21 | PASS |
| L060_N08_b2_R2/cost_10bps | 21 | PASS |
| L060_N10_b2_R1/cost_5bps | 21 | PASS |
| L060_N10_b3_R1/cost_5bps | 21 | PASS |
| L060_N12_b2_R1/cost_10bps | 21 | PASS |
| L060_N12_b3_R1/cost_5bps | 21 | PASS |
| L090_N08_b2_R1/cost_10bps | 21 | PASS |
| L090_N08_b2_R1/cost_5bps | 21 | PASS |
| L090_N08_b2_R2/cost_10bps | 21 | PASS |
| L090_N08_b2_R2/cost_5bps | 21 | PASS |
| L090_N08_b3_R1/cost_10bps | 21 | PASS |
| L090_N08_b3_R1/cost_5bps | 21 | PASS |
| L090_N08_b3_R2/cost_10bps | 21 | PASS |
| L090_N08_b3_R2/cost_5bps | 21 | PASS |
| L090_N10_b2_R1/cost_10bps | 21 | PASS |
| L090_N10_b2_R1/cost_5bps | 21 | PASS |
| L090_N10_b2_R2/cost_10bps | 21 | PASS |
| L090_N10_b2_R2/cost_5bps | 21 | PASS |
| L090_N10_b3_R1/cost_10bps | 21 | PASS |
| L090_N10_b3_R1/cost_5bps | 21 | PASS |
| L090_N10_b3_R2/cost_10bps | 21 | PASS |
| L090_N10_b3_R2/cost_5bps | 21 | PASS |
| L090_N12_b2_R1/cost_10bps | 21 | PASS |
| L090_N12_b2_R1/cost_5bps | 21 | PASS |
| L090_N12_b2_R2/cost_10bps | 21 | PASS |

## 修改文件、测试与实际命令

修改`stage3_ledger.py`加入授权的现金零碎结算科目（默认关闭）；`stage3.py`仅Stage4上下文启用补数及输出；`stage4_runner.py`加载独立版本的辅助行情；`stage4_acceptance.py`独立重建零碎应收/支付；`stage4_summary.py`增加近似现金披露和本轮禁止保留段执行字段；新增`stage4_remediation.py`用于数据/授权哈希校验、58运行保护及保留段拒绝。测试文件`tests_v05/test_stage4_remediation.py`覆盖金额、只记一次、到账前后资金、未授权停止、保留段拒绝及辅助证券不入池。原选股/缓冲/行业上限/再平衡代码未改。

全部命令在项目根目录，解释器`D:\Tools\miniconda3\python.exe -X utf8`；网络核查只调用OpenQuoteContext，没有任何交易上下文或交易接口。

| 实际命令（省略解释器前缀） | 退出码 | 日志 |
|---|---:|---|
| work/v05/stage4_remediation/probe_opend.py | 0 | stock_basic/history_none/rehab原始响应（旧代码不识别是数据结果） |
| work/v05/stage4_remediation/probe_opend_vsxy.py | 0 | VSXY_*原始响应 |
| work/v05/stage4_remediation/probe_yahoo.py | 0 | yahoo_*响应及meta |
| download_sources.py / download_sources_2.py / download_sources_3.py | 0 / 0 / 0 | 同名log；403/404响应也留存，实际采用SEC及公司官方可成功原件 |
| rerun_incomplete.py non_vsco（仅账本辅助行情版本） | 1，中断 | rerun_non_vsco.log；工程重放归档 |
| rerun_incomplete.py vsco_exact（仅账本辅助行情版本） | 0 | rerun_vsco_exact.log；8次评分停止归档 |
| rerun_incomplete.py non_vsco（含评分覆盖层，断点续跑） | 初次1、中断后0 | rerun_non_vsco_replay001.log / rerun_non_vsco_resumed002.log |
| rerun_incomplete.py vsco_exact（含评分覆盖层） | 0 | rerun_vsco_exact_replay001.log |
| rerun_incomplete.py vsco_fractional（断点续跑） | 初次1、中断后0 | rerun_vsco_fractional.log / rerun_vsco_fractional_resumed002.log |
| reproduce_baselines.py | 0 | reproduce_baselines.log、baseline_proof.json |
| -m leadtrend_v05 demo --config config/v05_bootstrap.json --output work/v05/stage4_remediation/demo | 0 | demo.log |
| -m pytest -q -o addopts= tests_v05 --basetemp work/v05/stage4_remediation/pytest_sealed -o cache_dir=work/v05/stage4_remediation/pytest_cache | 0 | pytest_final.log |
| work/v05/stage4_remediation/finalize.py | 0 | finalize.log |

pytest实际输出：`185 passed in 16.62s`。选股/评分公式/执行/资格/行业源文件哈希与上一轮相同，逐项证明见`unchanged_rule_code.json`。

Stage3两成本独立目录重新运行，trades/daily_equity/benchmark逐字节与原产物相同，summary经济数字完全一致（`baseline_proof.json`）；Stage1 demo经济文件哈希核对见`demo_proof.json`。初始普通沙箱OpenD/Yahoo访问及PDF读取依赖探测失败均未产生策略结果；重新以批准的只读网络权限完成查询。

## 局限及请Claude重点审阅

幸存者偏差、当前行业分类、72个配置多重比较、工程默认值未经验证仍存在。零碎现金为明确标记的近似，不是实际券商流水；+10交易日与不扣费用会产生偏差，逐笔权益比例已给出。旧58运行与新86运行使用的数据版本不同；旧58产物按授权保持完整不变，没有重跑。尤其BBWI真实新股估值覆盖会改变分拆窗口内母股的评分可用性及排名，不能把旧58文件哈希不变等同于它们在新评分输入下重算也相同。本表严格按投资者要求保留旧58并更新原未完成86；这一数据版本混合须由Claude重点审阅，不能声称144项已在完全统一的新数据上全部重算。

请重点审阅：VSCO↔VSXY身份链与AuType.NONE/无拆合股核验；新辅助行情未入资格池；首个regular-way价和第10个后续交易日的选择；零碎应收不进入可配置资金与独立重建；58目录全部文件哈希；候选邻接与排序严格沿用原登记；本轮保留段硬性禁止。

本轮完成后停止，等待Claude审阅；不运行保留段。
