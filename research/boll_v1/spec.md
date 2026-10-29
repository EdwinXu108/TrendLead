# 周线布林“预判点”登记（boll_v1）

2026-10-29。研究范围 `top50_v1`（83 只），库 `data/db/leadtrend_db_v2.sqlite`，开发段 2014-02-28 至 2024-06-28。
只做一件事：标出每只股票的中轨什么时候被预判即将止跌。不设参数网格，不做参数搜索，不动保留段。

## 口径

1. **数据**：证券的 `tri`（含分红总回报），`api.tri_series` 从库里最早日期取到 2024-06-28，早期数据只用于预热。
2. **周线收盘**：每周最后一个交易日的 `tri`，日期标实际交易日。
3. **去噪中轨**：20 周余弦（Hann）加权平均，`w_i = sin²(π(i+1)/21)`，`i = 0..19`（0 最老），归一化后 `m_t = Σ w_i·P_{t−19+i}`；不满 20 周为空。
4. **导数**：`s_t = m_t − m_{t−1}`，`a_t = s_t − s_{t−1}`。
5. **预判点**：`s_t < 0` 且 `s_t + a_t ≥ 0`。
6. **标准布林线**（仅对照）：20 周简单平均为中轨，上下轨 ± 2 倍 20 周总体标准差（`ddof=0`）。
7. **预判点明细**：从 `t` 往后找第一个 `s ≥ 0` 的周 `e`，报相隔周数 `e − t`、期间最多再跌 `min(P_{t..e})/P_t − 1`、
   回正那周比预判价 `P_e/P_t − 1`、回正日期；开发段结束前未回正则写 `—`。

所有量只用 `t` 及以前的数据，中轨本身是加权平均，不做任何事后重新拟合。

## 显示约定

图上与表里的价格把 `tri` 按开发段第一周的实际收盘价折算（`meta['price_scale']`），形状与指数完全一致；
日期、导数和各项比例不受影响。

## 代码与看板

- 计算：`leadtrend_bt/boll_weekly.py`（`python -X utf8 -m leadtrend_bt.boll_weekly --security META`）。
- 看板：`research/boll_v1/dashboard.py` + `dashboard.html`，`python -X utf8 research/boll_v1/dashboard.py --open`，
  默认端口 8766（原看板 8765 不动）。
- 测试：`tests_bt/test_boll_weekly.py`。

## 核对

META 8 个、MSFT 7 个预判点，日期与投资者给的清单一致（休市日按早一天的实际交易日），见
`work/handoff/boll_dashboard_delivery_2026-10-29.md`。
