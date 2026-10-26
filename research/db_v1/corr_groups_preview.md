# 相关性分组预览（描述性，不用于任何策略）

方法 `corr_v1`：{"demean": "equal_weight_of_eligible_securities", "distance": "1-correlation", "groups": 12, "linkage": "average", "min_days": 252, "missing": "filled_with_cross_sectional_mean", "window": 504}

分组只用版本生效日之前 504 个交易日的数据，按当日可选证券等权平均去市场，距离 = 1 − 相关系数，平均连接层次聚类切成 12 组。下表全部是事实计数。

## 1. 各版本组大小分布

- 有分组的版本：51（公开版本 52）
- 每组平均证券数：15.6
- 最大组：86；最小组：1

| 组内证券数 | 出现次数 |
|---|---|
| 1 | 47 |
| 2 | 50 |
| 3 | 58 |
| 4 | 43 |
| 5 | 47 |
| 6 | 31 |
| 7 | 23 |
| 8 | 13 |
| 9 | 14 |
| 10 | 11 |
| 11 | 18 |
| 12 | 24 |
| 13 | 14 |
| 14 | 14 |
| 15 | 6 |
| 16 | 16 |
| 17 | 10 |
| 18 | 5 |
| 19 | 7 |
| 20 | 4 |
| 21 | 5 |
| 22 | 9 |
| 23 | 4 |
| 24 | 11 |
| 25 | 6 |
| 26 | 7 |
| 27 | 4 |
| 28 | 9 |
| 29 | 3 |
| 30 | 4 |
| 31 | 5 |
| 32 | 3 |
| 33 | 3 |
| 34 | 3 |
| 35 | 1 |
| 36 | 6 |
| 37 | 6 |
| 38 | 4 |
| 39 | 5 |
| 40 | 2 |
| 41 | 5 |
| 42 | 3 |
| 43 | 2 |
| 44 | 6 |
| 46 | 4 |
| 47 | 1 |
| 48 | 6 |
| 49 | 3 |
| 52 | 2 |
| 53 | 6 |
| 54 | 2 |
| 55 | 3 |
| 56 | 1 |
| 57 | 1 |
| 58 | 3 |
| 59 | 5 |
| 60 | 1 |
| 61 | 1 |
| 63 | 1 |
| 64 | 1 |
| 65 | 1 |
| 68 | 2 |
| 69 | 2 |
| 72 | 1 |
| 73 | 1 |
| 75 | 1 |
| 76 | 1 |
| 77 | 1 |
| 81 | 1 |
| 86 | 1 |

## 2. 相邻版本的分组稳定性（ARI）

相邻两个版本中共同出现的证券，比较其组标签的调整兰德指数。

| 前一版本 | 后一版本 | 共同证券 | ARI |
|---|---|---|---|
| 2013-12-31 | 2014-03-31 | 159 | 0.840 |
| 2014-03-31 | 2014-06-30 | 160 | 0.880 |
| 2014-06-30 | 2014-09-30 | 158 | 0.786 |
| 2014-09-30 | 2014-12-31 | 160 | 0.807 |
| 2014-12-31 | 2015-03-31 | 159 | 0.724 |
| 2015-03-31 | 2015-06-30 | 165 | 0.791 |
| 2015-06-30 | 2015-09-30 | 158 | 0.495 |
| 2015-09-30 | 2015-12-31 | 164 | 0.603 |
| 2015-12-31 | 2016-03-31 | 164 | 0.773 |
| 2016-03-31 | 2016-06-30 | 165 | 0.705 |
| 2016-06-30 | 2016-09-30 | 168 | 0.747 |
| 2016-09-30 | 2016-12-31 | 167 | 0.591 |
| 2016-12-31 | 2017-03-31 | 168 | 0.759 |
| 2017-03-31 | 2017-06-30 | 172 | 0.777 |
| 2017-06-30 | 2017-09-30 | 175 | 0.752 |
| 2017-09-30 | 2017-12-31 | 174 | 0.566 |
| 2017-12-31 | 2018-03-31 | 174 | 0.720 |
| 2018-03-31 | 2018-06-30 | 176 | 0.693 |
| 2018-06-30 | 2018-09-30 | 180 | 0.695 |
| 2018-09-30 | 2018-12-31 | 182 | 0.752 |
| 2018-12-31 | 2019-03-31 | 183 | 0.775 |
| 2019-03-31 | 2019-06-30 | 182 | 0.700 |
| 2019-06-30 | 2019-09-30 | 183 | 0.650 |
| 2019-09-30 | 2019-12-31 | 188 | 0.875 |
| 2019-12-31 | 2019-12-31 | 194 | 1.000 |
| 2019-12-31 | 2020-03-31 | 176 | 0.391 |
| 2020-03-31 | 2020-06-30 | 185 | 0.833 |
| 2020-06-30 | 2020-09-30 | 187 | 0.858 |
| 2020-09-30 | 2020-12-31 | 186 | 0.841 |
| 2020-12-31 | 2021-03-31 | 188 | 0.887 |
| 2021-03-31 | 2021-06-30 | 191 | 0.892 |
| 2021-06-30 | 2021-09-30 | 187 | 0.875 |
| 2021-09-30 | 2021-12-31 | 187 | 0.867 |
| 2021-12-31 | 2022-03-31 | 185 | 0.548 |
| 2022-03-31 | 2022-06-30 | 191 | 0.668 |
| 2022-06-30 | 2022-09-30 | 192 | 0.868 |
| 2022-09-30 | 2022-12-31 | 194 | 0.770 |
| 2022-12-31 | 2023-03-31 | 186 | 0.713 |
| 2023-03-31 | 2023-06-30 | 189 | 0.826 |
| 2023-06-30 | 2023-09-30 | 190 | 0.788 |
| 2023-09-30 | 2023-12-31 | 189 | 0.713 |
| 2023-12-31 | 2024-03-31 | 190 | 0.671 |
| 2024-03-31 | 2024-06-30 | 190 | 0.729 |
| 2024-06-30 | 2024-09-30 | 189 | 0.844 |
| 2024-09-30 | 2024-12-31 | 187 | 0.669 |
| 2024-12-31 | 2025-03-31 | 189 | 0.803 |
| 2025-03-31 | 2025-06-30 | 190 | 0.906 |
| 2025-06-30 | 2025-09-30 | 191 | 0.650 |
| 2025-09-30 | 2025-12-31 | 194 | 0.719 |
| 2025-12-31 | 2026-03-31 | 187 | 0.799 |
| 2026-03-31 | 2026-06-30 | 186 | 0.831 |

ARI 均值 0.753，最小 0.391，最大 1.000，相邻版本对数 51。

## 3. 三期分组成员（代码）

### 2016-12-31（版本 NCSR_2016-12-31_0000932471-17-003352，生效 2017-02-28，175 只证券）

| 组 | 数量 | 成员 |
|---|---|---|
| 1 | 53 | ACE, AEP, AFL, ALL, AON, AZO, BA, BRK.B, CL, CME, COST, CVS, D, DUK, ED, EIX, EXC, GD, GIS, HD, ICE, JNJ, KHC, KMB, KO, KR, LMT, LOW, MCD, MDLZ, MO, MRSH, NEE, NOC, ORLY, PCG, PEP, PG, PM, PPL, ROST, RTX, SO, SRE, STZ, SYY, T, TGT, TJX, TRV, VZ, WM, WMT |
| 2 | 23 | ABBV, ABT, AMGN, BDX, BIIB, BMY, BSX, CAH, CI, ELV, GILD, HUM, ISRG, LLY, MCK, MDT, MRK, PFE, REGN, SYK, TMO, UNH, ZTS |
| 3 | 22 | AIG, AXP, BAC, BK, C, COF, CUSIP:09247X101, F, GM, GS, JPM, MET, MS, MTB, PNC, PRU, SCHW, STT, SYF, TFC, USB, WFC |
| 4 | 18 | APD, CAT, CSX, DE, ECL, EMR, ETN, FDX, GE, HON, ITW, MMM, NSC, PCAR, PPG, TEL, UNP, UPS |
| 5 | 17 | AAPL, ADBE, AMZN, BKNG, CRM, GOOG, GOOGL, INTU, MA, META, MSFT, NFLX, NKE, PYPL, SBUX, SPGI, V |
| 6 | 15 | ADM, COP, CUSIP:037411105, CVX, DVN, EOG, HAL, KMI, LYB, MPC, OXY, PSX, SLB, VLO, XOM |
| 7 | 11 | AMAT, CIKCLS:0001649338:COM, EBAY, GLW, HPE, HPQ, INTC, MU, NVDA, QCOM, TXN |
| 8 | 5 | ACN, ADP, CTSH, FIS, FISV |
| 9 | 4 | AAL, DAL, LUV, MAR |
| 10 | 3 | CHTR, CMCSA, DIS |
| 11 | 3 | CSCO, IBM, ORCL |
| 12 | 1 | DHR |

### 2020-12-31（版本 NPORT_2020-12-31_0001752724-21-045740，生效 2021-03-02，194 只证券）

| 组 | 数量 | 成员 |
|---|---|---|
| 1 | 55 | A, ABBV, ABT, AEP, AMGN, BAX, BDX, BIIB, BMY, BRK.B, CHTR, CL, CMCSA, COST, D, DG, DHR, DUK, EBAY, EXC, GD, GILD, GIS, JNJ, KMB, KO, LHX, LLY, LMT, MDLZ, MNST, MO, MRK, NEE, NEM, NOC, PEP, PFE, PG, PGR, PM, REGN, ROP, SO, SRE, T, TGT, TMO, TMUS, VRSK, VRTX, VZ, WM, WMT, XEL |
| 2 | 53 | AIG, ALGN, APTV, AXP, BA, BAC, BK, BKNG, C, CAT, CMI, COF, COP, CSX, CTAS, CVX, DD, DE, DIS, DOW, EMR, ETN, F, FCX, GE, GM, GS, HCA, HON, HPQ, ITW, JCI, JPM, MAR, MET, MS, NSC, PH, PNC, PPG, ROST, RTX, SCHW, SYY, TDG, TEL, TFC, TJX, TT, UNP, USB, WFC, XOM |
| 3 | 28 | AAPL, ADBE, ADI, ADSK, AMAT, AMD, AMZN, AVGO, CDNS, CRM, GOOG, GOOGL, INTC, INTU, KLAC, LRCX, MCHP, META, MSFT, MU, NFLX, NOW, NVDA, PYPL, QCOM, SNPS, TSLA, TXN |
| 4 | 25 | AON, BSX, CMG, DXCM, EW, FIS, FISV, GPN, IDXX, ILMN, IQV, ISRG, MA, MCD, MCO, MDT, MRSH, MSCI, SBUX, SPGI, SYK, V, YUM, ZBH, ZTS |
| 5 | 7 | ACE, ACN, ADP, ALL, CME, ICE, TRV |
| 6 | 6 | CI, CNC, CVS, ELV, HUM, UNH |
| 7 | 6 | CSCO, FDX, IBM, MMM, ORCL, UPS |
| 8 | 4 | APD, ECL, LIN, STZ |
| 9 | 4 | HD, LOW, ORLY, SHW |
| 10 | 2 | APH, CTSH |
| 11 | 2 | CUSIP:09247X101, TROW |
| 12 | 2 | EL, NKE |

### 2024-06-30（版本 NPORT_2024-06-30_0001752724-24-194904，生效 2024-08-28，199 只证券）

| 组 | 数量 | 成员 |
|---|---|---|
| 1 | 69 | ABBV, ABT, ACE, ADP, AEP, AJG, ALL, AMGN, AON, APD, AZO, BDX, BMY, BRK.B, CI, CL, CME, COST, CTAS, CVS, D, DUK, ECL, ELV, GD, GILD, HON, HUM, ICE, JNJ, KMB, KO, LHX, LIN, LLY, LMT, MCD, MCK, MDLZ, MDT, MO, MRK, MRSH, MSI, NEE, NOC, ORLY, PEP, PFE, PG, PGR, PM, REGN, ROP, ROST, RSG, RTX, SO, SRE, STZ, T, TJX, TMUS, TRV, UNH, VRTX, VZ, WM, WMT |
| 2 | 44 | AAPL, ABNB, ADBE, ADI, ADSK, AMAT, AMD, AMZN, ANET, APH, AVGO, BKNG, CDNS, CMG, CPRT, CRM, CRWD, DIS, GOOG, GOOGL, HLT, INTC, INTU, KLAC, LRCX, MAR, MCHP, META, MPWR, MSFT, MU, NFLX, NOW, NVDA, NXPI, ORCL, PANW, QCOM, SMCI, SNPS, TEL, TSLA, TXN, UBER |
| 3 | 33 | AFL, AIG, AMP, AXP, BAC, BK, C, COF, COP, CVX, EOG, F, FCX, GM, GS, JPM, MET, MPC, MS, NEM, OKE, OXY, PNC, PRU, PSX, SCHW, SLB, TFC, USB, VLO, WFC, WMB, XOM |
| 4 | 14 | BX, CUSIP:09247X101, DHI, DHR, HD, IDXX, KKR, LOW, MCO, PYPL, SHW, SPGI, TMO, ZTS |
| 5 | 14 | CARR, CAT, CEG, DE, EMR, ETN, GE, ITW, JCI, PCAR, PH, TDG, TT, URI |
| 6 | 7 | CSX, FDX, MMM, NSC, TGT, UNP, UPS |
| 7 | 6 | BSX, DXCM, EW, HCA, ISRG, SYK |
| 8 | 4 | ACN, CMCSA, CSCO, IBM |
| 9 | 4 | FIS, FISV, MA, V |
| 10 | 2 | NKE, SBUX |
| 11 | 1 | BA |
| 12 | 1 | MRNA |

## 4. 与富途行业标签的交叉表（只作对照）

行业标签来自 v05 归一化档案的当前快照（非历史），只覆盖当时股票池内的证券；未匹配的记为未标注。

| 组 | 信息技术 | 公用事业 | 医疗保健 | 可选消费 | 工业 | 必需消费 | 材料 | 能源 | 通信服务 | 金融 |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 130 | 406 | 612 | 188 | 245 | 631 | 68 | 18 | 162 | 343 |
| 2 | 551 | 40 | 207 | 169 | 278 | 57 | 42 | 71 | 93 | 451 |
| 3 | 277 | 8 | 149 | 108 | 232 | 24 | 46 | 117 | 61 | 482 |
| 4 | 161 | 0 | 160 | 72 | 165 | 8 | 46 | 110 | 19 | 161 |
| 5 | 69 | 2 | 143 | 59 | 118 | 16 | 30 | 117 | 18 | 92 |
| 6 | 57 | 1 | 99 | 104 | 68 | 31 | 23 | 97 | 26 | 17 |
| 7 | 76 | 0 | 81 | 82 | 65 | 26 | 6 | 23 | 14 | 19 |
| 8 | 69 | 0 | 37 | 42 | 73 | 9 | 15 | 0 | 8 | 28 |
| 9 | 45 | 0 | 39 | 25 | 40 | 2 | 34 | 0 | 10 | 24 |
| 10 | 46 | 2 | 17 | 30 | 18 | 4 | 10 | 3 | 21 | 15 |
| 11 | 27 | 1 | 1 | 24 | 18 | 5 | 5 | 6 | 26 | 6 |
| 12 | 21 | 0 | 11 | 15 | 7 | 7 | 4 | 0 | 5 | 3 |

匹配到行业标签的证券-版本行 9605，未匹配 154。
