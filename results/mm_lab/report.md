# Market-maker lab

Real fees per series, back-of-queue fills, 0.5 s latency, 2.5-hour sessions, every market traded.

## Test games (8): bmu_koe, nba_tor_det, nba_phx_uta, lfc_mun, nfl_tb_det, nfl_mia_buf, nfl_lv_den, nfl_min_sf

| Variant | Net P&L $ | Fees $ | Contracts | Cents/contract | Profitable games | Profitable markets |
|---|---|---|---|---|---|---|
| behind_touch | -154.63 | 47.50 | 10,221 | -1.51 | 1/8 | 4/18 |
| behind_touch_guard | -49.57 | 8.80 | 2,139 | -2.32 | 2/8 | 6/18 |
| at_touch | -217.51 | 74.90 | 18,064 | -1.20 | 1/8 | 1/18 |
| at_touch_guard | -77.89 | 18.94 | 4,919 | -1.58 | 0/8 | 2/18 |
| at_touch_guard_micro | -74.38 | 14.58 | 3,632 | -2.05 | 1/8 | 3/18 |
| at_touch_guard_skew1 | -77.79 | 18.94 | 4,899 | -1.59 | 0/8 | 2/18 |
| at_touch_guard_skew6 | -76.27 | 18.88 | 4,902 | -1.56 | 0/8 | 3/18 |
| at_touch_guard_60s | -32.67 | 9.38 | 2,592 | -1.26 | 3/8 | 6/18 |
| behind_guard_60s | -17.00 | 3.79 | 1,005 | -1.69 | 3/8 | 9/18 |
| behind_guard_120s | -12.80 | 2.12 | 603 | -2.12 | 4/8 | 7/18 |
| at_touch_guard_120s | -11.00 | 5.62 | 1,591 | -0.69 | 3/8 | 10/18 |
| at_touch_guard_micro_60s | -30.95 | 6.77 | 1,853 | -1.67 | 3/8 | 8/18 |
| at_touch_guard_micro_120s | -13.77 | 3.61 | 1,050 | -1.31 | 4/8 | 10/18 |
| behind_guard_micro_120s | -6.52 | 2.26 | 613 | -1.06 | 4/8 | 8/18 |
