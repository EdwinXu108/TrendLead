# 领势 · LeadTrend

当前版本：**v1.0.0**（2026-10-26，软件版本）。研究方案仍为 v0.5；数据版本与实验登记沿用 `v05` 路径和封条，目录名中的 `v05` 不随软件版本变化。变更见 [CHANGELOG.md](CHANGELOG.md)。

美股强势个股量化研究：每周从当时已公开的 VOO 持仓名单中，按近期强弱选约 10 只个股，同一行业有数量上限，带保留缓冲，以 VOO 总回报为基准。只买个股，不买 ETF。

## 现状（2026-10-25）

- 第一至第四阶段和性能优化已完成并经 Claude 审阅：真实数据接入、开发段回测（2019-11 至 2024-06）、72 配置参数网格、日常与回归两级验证。
- 结果都是**存在幸存者偏差、使用当前行业分类的近似实验**，不构成策略有效性的结论。
- 保留段（2024-07-05 起）尚未动用。

## 目录

```
leadtrend_v05/   代码（路径集中在 paths.py）
tests_v05/       测试
config/          运行配置
data/v05/        输入数据：raw 原始下载、normalized 规范化表、overlays 补充数据版本、prep 数据准备记录、reference、fixtures
research/v05/    研究成果：registry 实验登记、results 审阅通过的结果、baseline 回归基准、reports 各阶段报告
docs/            研究方案、实施计划、使用说明（README_v0.5.md）；archive/ 为历史文档和已执行的任务说明
work/            临时区：缓存、新运行、日志，随时可删
archive/         整理前代码快照、历史小文件压缩包、搬移与删除记录
vendor/          绘图依赖（matplotlib）
```

## 从这里开始

- [使用说明：目录、命令、验证级别、保留段规则](docs/README_v0.5.md)
- [研究方案 v0.5](docs/LeadTrend_研究方案_v0.5.md)
- [代码实施计划 v0.5](docs/LeadTrend_代码实施计划_v0.5.md)
- [项目约定（投资者确认的要求）](AGENTS.md)
- 各阶段交接报告：`research/v05/reports/`；第四阶段统一数据结果：`research/v05/results/stage4_unified/`

## 公开库不包含的内容

公开库只放代码、测试、文档、配置模板和少量小型结果。以下内容不上传（见 `.gitignore`）：

- **原始与规范化行情**（`data/v05/raw/`、`data/v05/normalized/`）：体积大，且行情数据受供应商授权约束。
- **数据准备中间产物**（`data/v05/prep/`、`data/v05/reference/`）：体积大，含行情与证券映射的中间结果。
- **运行结果明细**（`research/v05/perf/`、`research/v05/results/stage3_dev_run/`、`research/v05/results/stage4_unified/runs/`、`work/`）：体积大，可由代码重新生成。
- **冻结基准**（`research/v05/baseline/`）：回归比对用，体积大。
- **历史压缩包**（`archive/`）与**绘图依赖**（`vendor/`）：历史快照和第三方包，体积大。
- **个人信息**（`config/v05_stage2.json`）：其中的 SEC 联系方式属于个人信息。

## 复现

- 完整复现需要本地数据，即上节未上传的原始与规范化数据。
- 首次运行数据准备前，把 `config/v05_stage2.example.json` 复制为 `config/v05_stage2.json`，并把其中的 `sec_user_agent` 改为你自己的 SEC 联系方式（SEC 要求 User-Agent 含真实的联系信息）。
