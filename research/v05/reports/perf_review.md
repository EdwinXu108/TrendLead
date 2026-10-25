# LeadTrend v0.5 工程性能优化与统一数据复跑交接

2026-10-25。只优化工程，不改变策略规则；保留段未运行。

**实际结果：** 完整144次冷缓存双进程运行（含独立验收和额外全CSV/完整summary字节对照）CLI用时 **707.463秒（11.791分钟）**。当前网格文件 **149.069MB**，一份共享缓存 **305.803MB**，合计 **454.872MB**。144次全部复用用时 **17.917秒**；达到15分钟和1GB两个参考目标，复用尚未达到几秒目标。

**经济正确性：** 第一阶段原有全部CSV和完整summary逐字节一致；其原CLI未输出positions/receivables，另用冻结旧引擎与新引擎的只读账本观察器生成720条持仓行和10条应收行，逐字节一致，原CLI所有产物也未改变。第三阶段5/10bp的缓存路径和默认完整CLI均逐字节一致。第四阶段不止最少12配置：全部72配置×2成本的所有CSV（展开后）、完整summary及原独立验收所有字段均与冻结单进程引擎一致。新引擎的单/双进程另作24组直接字节比较。**没有排除任何summary数值字段，没有改基准通过，没有放宽验收容差。**

**数据版本修复另行归因：** 投资者最后明确授权优化完成后重跑统一版本，本轮因此全部144项均应用BBWI估值修正；与旧混合矩阵的差异见独立财务报告。性能对比始终用同一统一数据的冻结引擎作oracle，不能把数据修正的投资变化当作性能优化改变了经济结果。

## 基准冻结、确定性与测量口径

源码、数据保护清单和固定12配置在[freeze.json](D:/Workplace/LeadTrend/work/perf/baseline/freeze.json)。冻结源目录[source](D:/Workplace/LeadTrend/work/perf/baseline/source)保留原实现；原始参考输出新生成后只做经SHA验证的无损gzip归档，没有覆盖旧实验产物。`bench --mode baseline`发现既有冻结目录会拒绝覆盖。

`PYTHONHASHSEED=0`在生成参考输出之前设定，bench/run-grid会在新解释器启动时固定该值；Windows子进程用spawn并继承同一值。固定启动种子防止set/dict遍历导致日志顺序或浮点汇总残差变化；没有重排交易、修改账本累加、改精度或四舍五入。完整summary连残差字段也直接比较。手工run-dev复现使用同一环境设置。

首次144基线的部分时段和早期功能验证并发，所以不把该总时长与最终隔离时长之比宣称为严格因果加速比。又单独顺序跑了冻结引擎12配置基线，最终冷/热、单/双进程和144全量命令逐一顺序执行。阶段时间都是实际测量；并行阶段为各worker区间累计，会重叠，不能简单相加当墙钟时间。最终目标采用外层CLI墙钟时间，包含启动和退出。

## 耗时和占用总表

| 场景 | 成本运行数 | 进程 | 秒 | 运行MB | 共享缓存MB | 口径 |
|---|---|---|---|---|---|---|
| 旧日志参考，非本轮同口径实测 | 144 | — | 约6360 | 约9000 | 不含新缓存 | 历史上下文有不同等待/重复I/O |
| 冻结旧引擎完整网格 | 144 | — | 1660.297 | 9566.767 | — | 含本轮参考无损归档；部分时间与早期试验重叠 |
| 冻结旧引擎12配置，隔离测量 | 24 | — | 466.371 | 原完整CSV | — | 运行+同一验收，不含后置oracle/归档 |
| 迭代1：共享预计算，完整CSV | 24 | 1 | 151.372 | 1556.874 | 305.803 | 含独立验收；最终组含逐次全文件oracle |
| 迭代2：精简+一级决策投影 | 24 | 1 | 258.185 | 112.742 | 305.803 | 含独立验收；最终组含逐次全文件oracle |
| 最终：二级决策去重，冷缓存1进程 | 24 | 1 | 295.413 | 23.726 | 305.803 | 含独立验收；最终组含逐次全文件oracle |
| 最终：热缓存1进程 | 24 | 1 | 271.490 | 23.723 | 305.803 | 含独立验收；最终组含逐次全文件oracle |
| 最终：热缓存2进程 | 24 | 2 | 131.657 | 23.723 | 305.803 | 含独立验收；最终组含逐次全文件oracle |
| 最终144，冷缓存+2进程 | 144 | 2 | 707.463 | 149.069 | 305.803 | 含独立验收、144次全CSV+summary字节核对 |
| 相同输入/代码再次运行 | 144复用/0执行 | 2 | 17.917 | 149.069 | 305.803 | 不计算价格评分/信号/账本；完整校验已有产物哈希 |

早期迭代行不是隔离因果实验，且一级精简开始加入逐次完整解码oracle，不能直接把其总时间和上行相减归因为格式变化。最终单/双进程热缓存使用同一源码、同一缓存和不同新输出目录，24次均实际执行。

## 阶段耗时（秒）

下面统计每次运行中的区间，不含主进程缓存准备。验收来源载入并入“验收”，保留JSON中的单独子项。双进程行是worker累计时间，不等于墙钟。额外oracle是工程回归工作，正常run-grid不要求它，但144冷缓存实测仍将其包含在内。

| 场景 | 数据加载 | 资格/类别 | 评分 | 选股 | 执行账本及其他 | 写出 | 独立验收含源加载 | 哈希 | 字节oracle | 参考归档 |
|---|---|---|---|---|---|---|---|---|---|---|
| 冻结144 | 0.000 | 58.839 | 36.180 | 64.728 | 403.611 | 344.130 | 564.776 | 34.024 | 0.000 | 153.591 |
| 隔离旧12×2 | 0.000 | 13.988 | 83.048 | 14.783 | 108.072 | 93.964 | 149.288 | 3.164 | 0.000 | 0.000 |
| 缓存+full迭代 | 0.000 | 0.000 | 0.000 | 6.325 | 10.566 | 46.042 | 51.685 | 2.332 | 0.000 | 0.000 |
| 一级精简迭代 | 0.000 | 0.000 | 0.000 | 10.912 | 18.239 | 34.787 | 53.041 | 1.132 | 52.952 | 0.000 |
| 最终热缓存1进程 | 0.000 | 0.000 | 0.000 | 15.505 | 26.414 | 45.124 | 87.141 | 2.727 | 85.493 | 0.000 |
| 最终热缓存2进程 | 0.000 | 0.000 | 0.000 | 11.841 | 20.624 | 38.599 | 93.895 | 2.402 | 69.126 | 0.000 |
| 最终144冷缓存 | 0.000 | 0.000 | 0.000 | 75.555 | 125.679 | 223.676 | 360.315 | 14.294 | 401.501 | 0.000 |

共享准备单列（冷缓存的资格、TRI、6窗口评分只在这里发生；热缓存不重算）。未单列的构造/序列化及进程启动开销仍包含在总CLI时间，未伪造精确归属。

| 准备场景 | 准备墙钟跨度 | 读数据 | 资格/类别 | 评分 | 共享写出 | 哈希 | 其余共享构造/序列化 |
|---|---|---|---|---|---|---|---|
| 12配置冷缓存 | 91.558 | 12.630 | 0.255 | 40.503 | 0.000 | 1.008 | 37.163 |
| 12配置热缓存 | 4.511 | 0.000 | 0.000 | 0.000 | 0.000 | 0.282 | 4.229 |
| 144冷缓存 | 90.467 | 11.570 | 0.249 | 38.047 | 0.000 | 1.167 | 39.434 |

全量复用阶段：初次输入/代码哈希0.329秒、已有产物逐文件核对13.288秒、结束指纹核对0.319秒。没有反序列化prepared.pkl、重新评分或独立重放账本；凭相同键和通过验收的精简产物复用。约13秒集中在大量小文件的完整哈希检查，保留此检查，没有拿mtime冒充内容一致。

## 每项优化及测量证据

1. **共享预计算：** 每周名单/资格/类别选择时间线、TRI、每L评分和rank/candidate只计算一次并持久化。N、b、R、成本和实际持仓相关的选择、执行及账本仍逐次运行，绝未缓存持仓决定。数据与源码任何变更产生新内容键；键覆盖所有规范化CSV、原登记/封条、配置、评分阻断及覆盖输入、实际读到的辅助原始行情和补充事件/授权依赖、全包源码、Python/平台/启动种子和项目根路径。采用保守的全包源码键，改注释/纯CLI代码也可能重建缓存，未做容易漏依赖的细粒度猜测。

2. **元数据扫描：** first_price_date从每次score里重新min扫描改为构造时记录，结果完全相同；每周行业计数字符串从逐证券重复生成改为每周生成一次。没有改评分公式、选股/缓冲/行业上限/再平衡/手续费/账本规则。旧剖析约30,276,674次调用、17.354秒；优化后暖缓存核心剖析约7,563,405次调用、3.482秒（不含前置缓存准备，不能据此宣称整个冷启动快5倍）。详见两份cProfile文本与.prof。

3. **分级及无损压缩：** 同一12配置24次的full产物1556.874MB；一级精简112.742MB；最终精简23.723MB。universe和每L scores由manifest按物理/逻辑SHA引用；决策按周公共上下文+共享评分静态字段+逐行action/reason+异常覆盖保存，同时提供decision_changes。保留线在decision_contexts，实际成交在trades。完整决策随时可还原，未靠扔掉信息节省空间。为独立验收保留少量压缩诊断（cash_flows、贡献、覆盖、相对曲线等），主重复大表只存一份。gzip固定mtime=0，不使用有损数值转换。

4. **并行：** Windows显式spawn，缓存只读，每进程写自己的目录。相同24次热缓存CLI单进程271.490秒、双进程131.657秒，实际墙钟比2.062。串并行的trades、equity、benchmark、positions、receivables、公司行动、零碎现金连压缩字节都一致，summary完整字节一致。

5. **验收：** 原始行情由独立CSV路径载入，每个worker加载一次，多个运行共用；未使用主引擎的行情对象、持仓对象或账本函数。大诊断表延迟读取，按不可变内容哈希记住共享日期扫描。现金/股数/事件/费用/滑点/基准仍逐日独立重放；检查项与容差未变。144个原验收结果全部字段完全相等；第三阶段另外用冻结旧审计器直接核对默认full输出，与新读取器审计器所有字段相等。

6. **哈希和增量：** 共享文件的签名在缓存中记录，每个进程只在首次引用时验证；每次运行只签自己的精简文件，不再逐配置反复扫描原大输入或历史58目录。启动/结束仍对数据与源码内容核对。重复键+验收PASS+文件内容签名一致才复用；修改数据、代码、配置、辅助行情、产物或留有BYTE_MISMATCH_STOP都不会被当作合格复用。--force保留旧目录为归档后重跑；数据/源码版本更换也保留旧结果。现有固定登记的数据若被改动，登记防护会先拒绝，不会在旧登记下悄悄跑新数据。

## 字节比较逐对象结果（不作舍入）

每个PASS表示列出的经济CSV完整字节一致、整个summary.json字节一致；其余原CSV包括universe、scores、完整decisions也逐文件一致。表中“验收原样”指每个JSON字段相等，不是只比较PASS字符串。原始逐文件布尔结果保存在各byte_comparisons.json，没有筛掉失败数值字段。

| 比较对象 | trades | daily_equity/benchmark | positions/receivables | summary完整字节 | 其余CSV及验收 |
|---|---|---|---|---|---|
| synthetic，原CLI | PASS | PASS | 原CLI无两表；只读观察器720/10行均PASS | PASS | 原CLI全部9项PASS |
| Stage3缓存5bp | PASS | PASS | PASS | PASS | 独立审计PASS |
| Stage3缓存10bp | PASS | PASS | PASS | PASS | 独立审计PASS |
| Stage3默认full 5bp | PASS | PASS | PASS | PASS | 冻结/新审计器全字段相同 |
| Stage3默认full 10bp | PASS | PASS | PASS | PASS | 冻结/新审计器全字段相同 |
| L010_N08_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b2_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b2_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b3_R1 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b3_R1 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b2_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b2_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N08_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N08_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N08_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N08_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N08_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N08_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N10_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N10_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N10_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N10_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N10_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N10_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L010_N12_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L020_N12_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L040_N12_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L060_N12_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L090_N12_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b3_R2 cost_5bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |
| L126_N12_b3_R2 cost_10bps | PASS | PASS | PASS | PASS | 全CSV+完整summary+验收全字段PASS |

上表149个比较对象的原始证明：[144次全量逐文件结果](D:/Workplace/LeadTrend/work/v05/stage4_unified_perf/runs/byte_comparisons.json)、[Stage3缓存两成本](D:/Workplace/LeadTrend/work/perf/runs/legacy_final/byte_comparisons.json)、[Stage3默认CLI两成本](D:/Workplace/LeadTrend/work/perf/standalone_stage3_byte_proof.json)、[synthetic完整账本观察证明](D:/Workplace/LeadTrend/work/perf/synthetic_observed_optimized/observer_proof.json)。本轮固定面板的GE分拆/零碎现金与BBWI→VSCO覆盖见[mandatory_event_coverage.json](D:/Workplace/LeadTrend/work/perf/mandatory_event_coverage.json)。

单/双进程另逐对象直接核对如下（每个均为完整summary及八个经济/公司行动表的压缩字节一致，不靠解压后近似比较）：

| 配置 | 成本bp | 直接串并行字节比较 |
|---|---|---|
| L010_N08_b2_R1 | 5 | PASS |
| L010_N08_b2_R1 | 10 | PASS |
| L010_N08_b2_R2 | 5 | PASS |
| L010_N08_b2_R2 | 10 | PASS |
| L020_N10_b2_R1 | 5 | PASS |
| L020_N10_b2_R1 | 10 | PASS |
| L020_N10_b2_R2 | 5 | PASS |
| L020_N10_b2_R2 | 10 | PASS |
| L040_N08_b2_R1 | 5 | PASS |
| L040_N08_b2_R1 | 10 | PASS |
| L040_N10_b3_R2 | 5 | PASS |
| L040_N10_b3_R2 | 10 | PASS |
| L060_N12_b2_R1 | 5 | PASS |
| L060_N12_b2_R1 | 10 | PASS |
| L060_N10_b3_R2 | 5 | PASS |
| L060_N10_b3_R2 | 10 | PASS |
| L090_N08_b2_R1 | 5 | PASS |
| L090_N08_b2_R1 | 10 | PASS |
| L090_N08_b2_R2 | 5 | PASS |
| L090_N08_b2_R2 | 10 | PASS |
| L126_N08_b2_R1 | 5 | PASS |
| L126_N08_b2_R1 | 10 | PASS |
| L126_N08_b2_R2 | 5 | PASS |
| L126_N08_b2_R2 | 10 | PASS |

full调试命令两成本的全文件证明在[debug_full_byte_proof.json](D:/Workplace/LeadTrend/work/perf/debug_full_byte_proof.json)；单次导出完整CSV后再独立验收并逐字节核对的证明在[export_full_byte_proof.json](D:/Workplace/LeadTrend/work/perf/export_full_byte_proof.json)。

## 缓存失效、保护和测试

192项pytest通过，包括：改数据但保持大小/mtime仍改变键；改代码、配置、原来缺失的辅助源出现后改变键；改变配置/哈希或验收失败不复用；损坏产物不复用；亏损/日期条件未放宽；gzip和共享决策异常行（含未评分新股、冻结覆盖）精确往返；历史目录拒绝写入。原有保留段拒绝测试仍通过。

最终保护检查：旧work/v05共13867个文件的存在、大小和mtime均未变；所有原始/规范化文件元数据未变，已登记规范化内容和登记/封条SHA未变；冻结源码SHA未变。真实计算全在开发段，未调用交易接口。证明见[final_checks.json](D:/Workplace/LeadTrend/work/perf/final_checks.json)。

## 曾停止的辅助检查及处理

一次辅助剖析的严格比较因输出目录名而停止：冻结验收scenario=cost_5bps，辅助目录名导致scenario=profile_optimized。全部18项CSV/summary已一致，唯一差异为标签；失败记录保留，未删字段、未改容差、未改oracle，另用命名正确的新成本目录复核全字段通过。之后续跑辅助脚本还出现一次import作用域错误，修正的是work/perf辅助脚本，未改金融引擎；已复用先前通过的Stage3证明，保留全部失败日志。synthetic账本观察探针最初挂在外层run_pipeline而未捕获日循环，计数自检失败；改为只读观察实际内层日循环后，冻结旧/新引擎均生成720/10行并精确相同，原CLI输出同时未变。这些失败不隐藏，也不将它们记成收益差异。

标签事件完整差异见[profile_metadata_incident.json](D:/Workplace/LeadTrend/work/perf/profile_metadata_incident.json)；原失败目录[profile_optimized](D:/Workplace/LeadTrend/work/perf/profile_optimized)仍在。辅助重试与观察器日志均保留。

## 修改文件、依赖与使用

新增perf_bench.py（计时/冻结/cProfile）、perf_cache.py（内容键/只读预计算）、perf_artifacts.py（gzip/共享/无损投影/导出）、perf_runner.py（spawn/独立验收/增量/安全新目录）；修改__main__.py、stage4_runner.py接入新入口，stage3.py接入缓存和格式、stage3_scoring.py缓存最早日期、stage4_acceptance.py适配读取/共享日期扫描、stage4_summary.py接受新目录和压缩读取；新增tests_v05/test_perf.py；更新docs/README_v0.5.md。原scoring.py、selection.py、execution.py、ledger.py、stage3_ledger.py、eligibility.py、universe.py、stage3_metrics.py、benchmark.py源码哈希未变。

没有新增第三方依赖。引擎只用标准库csv/gzip/pickle/multiprocessing/cProfile；--artifacts full可退回原CSV。PNG沿用项目现有免费Matplotlib/NumPy（不是金融计算依赖），未向全局安装任何包。

```powershell
$env:PYTHONHASHSEED = "0"
python -m leadtrend_v05 bench --mode small_serial_warm --workers 1 --artifacts lean
python -m leadtrend_v05 run-grid --workers 2 --artifacts lean --output work/v05/stage4_next_engineering/runs
python work/perf/debug_one.py --configuration L126_N08_b2_R2 --artifacts full --output work/v05/stage4_debug_new
python -m leadtrend_v05 export-artifacts --input work/v05/stage4_unified_perf/runs/L126_N08_b2_R2/cost_5bps --output work/perf/export_debug_new
```

基准只创建一次，不要用--mode baseline覆盖本轮oracle。改变策略须先按投资者授权及实验登记流程处理；本轮不加入新研究规则。测量并行请用不同输出目录或--force，避免把全量复用误称并行加速。

## 实际命令与退出码

| 命令/脚本（解释器D:/Tools/miniconda3/python.exe -X utf8） | 退出码 | 证据 |
|---|---|---|
| bench --mode baseline | 0 | [baseline_command.log](D:/Workplace/LeadTrend/work/perf/baseline_command.log) |
| 冻结源 baseline_small_isolated.py | 0 | [baseline_small_isolated_command.log](D:/Workplace/LeadTrend/work/perf/baseline_small_isolated_command.log) |
| 早期cache_full/cache_lean | 0 / 0 | 相应*_command.log |
| -m pytest -q -o addopts= tests_v05 --basetemp work/perf/pytest_engine_final -o cache_dir=work/perf/pytest_cache | 0 | [pytest_final_engine.log](D:/Workplace/LeadTrend/work/perf/pytest_final_engine.log) |
| -m leadtrend_v05 bench --mode synthetic | 0 | [synthetic_final.log](D:/Workplace/LeadTrend/work/perf/synthetic_final.log) |
| -m leadtrend_v05 bench --mode legacy --output work/perf/runs/legacy_final --artifacts full | 0 | [legacy_final.log](D:/Workplace/LeadTrend/work/perf/legacy_final.log) |
| -m leadtrend_v05 bench --mode small_cold_v2 --workers 1 --artifacts lean | 0 | [small_cold_v2.log](D:/Workplace/LeadTrend/work/perf/small_cold_v2.log) |
| -m leadtrend_v05 bench --mode small_serial_warm --workers 1 --artifacts lean | 0 | [small_serial_warm.log](D:/Workplace/LeadTrend/work/perf/small_serial_warm.log) |
| -m leadtrend_v05 bench --mode small_parallel_v2 --workers 2 --artifacts lean | 0 | [small_parallel_v2.log](D:/Workplace/LeadTrend/work/perf/small_parallel_v2.log) |
| -m leadtrend_v05 bench --mode grid_unified --workers 2 --artifacts lean --cold-cache --output work/v05/stage4_unified_perf/runs | 0 | [grid_unified_cold.log](D:/Workplace/LeadTrend/work/perf/grid_unified_cold.log) |
| -m leadtrend_v05 bench --mode grid_unified --workers 2 --artifacts lean --output work/v05/stage4_unified_perf/runs | 0 | [grid_unified_reuse.log](D:/Workplace/LeadTrend/work/perf/grid_unified_reuse.log) |
| post_checks.py（初次/第二次/最终） | 1 / 1 / 0 | post_checks.log / post_checks_retry.log / post_checks_final.log |
| synthetic_ledger_probe旧/新（探针初版） | 1 / 1 | 失败目录和初始日志保留 |
| synthetic_ledger_probe旧/新（修正观察器） | 0 / 0 | *_observer_*_final.log |
| 冻结第三阶段审计器对照 | 0 | [frozen_auditor_comparison.log](D:/Workplace/LeadTrend/work/perf/frozen_auditor_comparison.log) |
| debug_one full与gold对照 | 0 | [debug_full_one.log](D:/Workplace/LeadTrend/work/perf/debug_full_one.log) |
| 默认Stage3 full两成本 | 0 / 0 | stage3_default_5.log / stage3_default_10.log |
| summarize-grid（新目录） | 0 | [unified_summary_command.log](D:/Workplace/LeadTrend/work/perf/unified_summary_command.log) |

完整参数数组、每条命令的外层CLI耗时及日志见[final_commands.json](D:/Workplace/LeadTrend/work/perf/final_commands.json)；各阶段、每成本运行、内容键和缓存准备记录在work/perf/measurements/，原始cProfile在baseline/single.prof和optimized_single.prof。

## 占用、可清理建议与保留事项

| 空间项目 | 实际GB（10^9字节） | 处理 |
|---|---|---|
| 原stage4_grid | 9.566524 | 完整保留，未压缩/移动/删除 |
| 新网格+一份活动共享缓存 | 0.454872 | 日常需求；含工程校验元数据 |
| 本轮work/perf全部验证材料 | 6.360716 | 含冻结基准、完整对照、多轮缓存，不能和日常网格混算 |
| 多轮cache/cold_cache全部副本 | 1.573892 | 部分为工程迭代旧键 |
| 早期24次full验证 | 1.557045 | 完整CSV保留 |
| 冻结基准及隔离复测归档 | 2.443703 | 只读oracle，建议保留 |

建议1：投资者批准后，可将原9.567GB网格逐文件lossless gzip/按完整文件SHA去重，先验证解压SHA再归档；不要只按L去重旧混合数据矩阵。参考本轮冻结144矩阵：逻辑9.567GB、无损归档2.058GB，可作为空间量级参考，原网格实际压缩收益尚未执行测量。

建议2：早期full验证1.557GB可经SHA核对后压缩归档；旧代码键缓存的prepared.pkl可再生成，但先保留被报告引用的shared CSV、cache.json、数据和源码。多轮缓存累计1.574GB，不代表单次日常缓存；具体文件清单见cache目录。没有执行任何旧产物清理，释放空间由投资者决定。

复用18秒尚未达到几秒参考值。可以在后续工程任务测试小文件散列I/O并发或单运行容器化，不能通过略去产物内容校验来宣称达标。本轮不再为参考值改动已完成的冻结引擎。

## 请Claude重点审阅与停止点

请检查：完整依赖键及原登记防护；周度class-selector状态是否完全复现；决策共享字段和异常覆盖的无损还原；独立审计确实不调用主账本；Windows spawn、固定启动种子、输入开始/结束哈希和worker只读；144组完整summary/CSV和逐字段审计证明；原始13,867文件保护清单；统一数据与旧混合矩阵的归因区分；基线测量并发污染和最终隔离测量口径；未达到几秒复用目标的如实披露。

统一数据投资结果另见[unified_review.md](D:/Workplace/LeadTrend/work/v05/stage4_unified_perf/unified_review.md)。完成后停止，等待Claude审阅，不运行保留段。
