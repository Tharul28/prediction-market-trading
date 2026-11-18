# Signal research

91584 snapshots from 10 recordings, 22 markets, days 2026-11-16, 2026-11-17.

## 10-second horizon

Split: by day: train 2026-11-16..2026-11-16, test 2026-11-17..2026-11-17, 10 s embargo.

| Model | Out-of-sample R² | Direction right (non-zero moves) | Predictions beating half-spread + fee | Avg move when they do (c) |
|---|---|---|---|---|
| A: zero | -0.0000 | 0.0% | 0.00% | n/a |
| B: OLS on imbalance | 0.0008 | 64.6% | 0.00% | n/a |
| OLS, all features | -0.2611 | 54.6% | 0.54% | 0.11 |
| Ridge | -0.0305 | 54.2% | 0.08% | -0.62 |
| Lasso | 0.0006 | 64.5% | 0.00% | n/a |
| Elastic Net | 0.0007 | 64.5% | 0.00% | n/a |

Lasso set to zero: mid, spread, imb_3, micro_dev, flow_10s, flow_60s, flow_rel_10s, flow_rel_60s, ret_10s, ret_60s, log_hours_to_expiry.
Lasso kept (standardised coefficient, cents per standard deviation): imb_1 +0.010.

Out-of-sample R² by market:

| Market | Rows | Zero moves | B: OLS on imbalance | Lasso | Ridge |
|---|---|---|---|---|---|
| KXFEDDECISION-26DEC-H25 | 2486 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXFEDDECISION-26DEC-H26 | 2486 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXNBA-27-MIN | 2487 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXNBA-27-PHI | 2487 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXNBAGAME-26NOV16GSWNOP-GSW | 1009 | 92% | -0.0007 | -0.0015 | -0.0082 |
| KXNBAGAME-26NOV16GSWNOP-NOP | 1008 | 92% | 0.0202 | 0.0073 | 0.0055 |
| KXNBAGAME-26NOV16PHXDEN-DEN | 1519 | 92% | 0.0071 | 0.0024 | -0.0018 |
| KXNBAGAME-26NOV16PHXDEN-PHX | 1519 | 92% | 0.0096 | 0.0040 | -0.0196 |
| KXNBAGAME-26NOV16PORMIN-MIN | 1089 | 89% | 0.0062 | 0.0026 | 0.0067 |
| KXNBAGAME-26NOV16PORMIN-POR | 1087 | 91% | 0.0016 | 0.0010 | -0.0006 |
| KXNFLGAME-26NOV16LACBAL-BAL | 5043 | 85% | 0.0004 | -0.0002 | 0.0044 |
| KXNFLGAME-26NOV16LACBAL-LAC | 5042 | 85% | 0.0010 | 0.0000 | -0.0513 |
| KXNHLGAME-26NOV16VANUTA-UTA | 1878 | 95% | -0.0003 | -0.0001 | -0.1230 |
| KXNHLGAME-26NOV16VANUTA-VAN | 1878 | 94% | 0.0004 | 0.0003 | -0.0052 |
| KXPRESPERSON-28-JOSS | 2485 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXPRESPERSON-28-RDES | 796 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXSB-27-CIN | 2479 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXSB-27-MIN | 2479 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXSB-27-SF | 2479 | 100% | 0.0000 | 0.0000 | 0.0000 |

Out-of-sample R² by day:

| Day | Rows | Zero moves | B: OLS on imbalance | Lasso | Ridge |
|---|---|---|---|---|---|
| 2026-11-17 | 41736 | 94% | 0.0008 | 0.0006 | -0.0305 |

Out-of-sample R² by recording:

| Recording | Rows | Zero moves | B: OLS on imbalance | Lasso | Ridge |
|---|---|---|---|---|---|
| cont_fed | 4972 | 100% | 0.0000 | 0.0000 | 0.0000 |
| cont_nba | 4974 | 100% | 0.0000 | 0.0000 | 0.0000 |
| cont_pres | 3281 | 100% | 0.0000 | 0.0000 | 0.0000 |
| cont_sb | 7437 | 100% | 0.0000 | 0.0000 | 0.0000 |
| nba_gsw_nop | 2017 | 92% | 0.0122 | 0.0054 | 0.0012 |
| nba_phx_den | 3038 | 92% | 0.0087 | 0.0036 | -0.0105 |
| nba_por_min | 2176 | 90% | 0.0042 | 0.0019 | 0.0034 |
| nfl_lac_bal | 10085 | 85% | 0.0014 | 0.0005 | -0.0232 |
| nhl_van_uta | 3756 | 94% | 0.0001 | 0.0002 | -0.0686 |

## 60-second horizon

Split: by day: train 2026-11-16..2026-11-16, test 2026-11-17..2026-11-17, 60 s embargo.

| Model | Out-of-sample R² | Direction right (non-zero moves) | Predictions beating half-spread + fee | Avg move when they do (c) |
|---|---|---|---|---|
| A: zero | -0.0000 | 0.0% | 0.00% | n/a |
| B: OLS on imbalance | 0.0018 | 58.2% | 0.00% | n/a |
| OLS, all features | -0.0549 | 53.4% | 1.85% | -0.04 |
| Ridge | -0.0000 | 54.1% | 0.07% | -0.96 |
| Lasso | -0.0000 | 50.5% | 0.00% | n/a |
| Elastic Net | -0.0000 | 50.5% | 0.00% | n/a |

Lasso set to zero: mid, spread, imb_1, imb_3, micro_dev, flow_10s, flow_60s, flow_rel_10s, flow_rel_60s, ret_10s, ret_60s, log_hours_to_expiry.
Lasso kept (standardised coefficient, cents per standard deviation): nothing.

Out-of-sample R² by market:

| Market | Rows | Zero moves | B: OLS on imbalance | Lasso | Ridge |
|---|---|---|---|---|---|
| KXFEDDECISION-26DEC-H25 | 2476 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXFEDDECISION-26DEC-H26 | 2477 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXNBA-27-MIN | 2477 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXNBA-27-PHI | 2477 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXNBAGAME-26NOV16GSWNOP-GSW | 1007 | 72% | -0.0490 | -0.0399 | -0.0421 |
| KXNBAGAME-26NOV16GSWNOP-NOP | 1006 | 71% | -0.0145 | -0.0364 | -0.0276 |
| KXNBAGAME-26NOV16PHXDEN-DEN | 1514 | 71% | -0.0092 | -0.0110 | -0.0122 |
| KXNBAGAME-26NOV16PHXDEN-PHX | 1514 | 71% | -0.0009 | -0.0117 | -0.0098 |
| KXNBAGAME-26NOV16PORMIN-MIN | 1086 | 56% | -0.0024 | -0.0097 | -0.0055 |
| KXNBAGAME-26NOV16PORMIN-POR | 1084 | 63% | -0.0090 | -0.0085 | -0.0080 |
| KXNFLGAME-26NOV16LACBAL-BAL | 5032 | 56% | -0.0046 | -0.0051 | -0.0076 |
| KXNFLGAME-26NOV16LACBAL-LAC | 5031 | 57% | -0.0007 | -0.0050 | -0.0071 |
| KXNHLGAME-26NOV16VANUTA-UTA | 1872 | 77% | -0.0056 | -0.0033 | 0.0058 |
| KXNHLGAME-26NOV16VANUTA-VAN | 1873 | 78% | 0.0004 | -0.0028 | -0.0024 |
| KXPRESPERSON-28-JOSS | 2476 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXPRESPERSON-28-RDES | 786 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXSB-27-CIN | 2470 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXSB-27-MIN | 2470 | 100% | 0.0000 | 0.0000 | 0.0000 |
| KXSB-27-SF | 2469 | 100% | 0.0000 | 0.0000 | 0.0000 |

Out-of-sample R² by day:

| Day | Rows | Zero moves | B: OLS on imbalance | Lasso | Ridge |
|---|---|---|---|---|---|
| 2026-11-17 | 41597 | 82% | 0.0018 | -0.0000 | -0.0000 |

Out-of-sample R² by recording:

| Recording | Rows | Zero moves | B: OLS on imbalance | Lasso | Ridge |
|---|---|---|---|---|---|
| cont_fed | 4953 | 100% | 0.0000 | 0.0000 | 0.0000 |
| cont_nba | 4954 | 100% | 0.0000 | 0.0000 | 0.0000 |
| cont_pres | 3262 | 100% | 0.0000 | 0.0000 | 0.0000 |
| cont_sb | 7409 | 100% | 0.0000 | 0.0000 | 0.0000 |
| nba_gsw_nop | 2013 | 71% | 0.0066 | -0.0000 | 0.0033 |
| nba_phx_den | 3028 | 71% | 0.0062 | -0.0000 | 0.0004 |
| nba_por_min | 2170 | 60% | 0.0034 | -0.0000 | 0.0023 |
| nfl_lac_bal | 10063 | 56% | 0.0024 | -0.0000 | -0.0023 |
| nhl_van_uta | 3745 | 78% | 0.0006 | -0.0000 | 0.0045 |

