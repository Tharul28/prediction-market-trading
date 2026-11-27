# Market-maker lab

Real fees per series, back-of-queue fills, 0.5 s latency, 2.5-hour sessions, every market traded.

## Test games (11): thin_nba_bknmil, thin_nba_chicha, thin_nba_clenyk, thin_nba_lacmem, thin_ncaamb_connduke, thin_ncaamb_uvamarq, thin_nhl_mtlcol, thin_nhl_ottfla, thin_nhl_stlnyi, thin_nhl_utamin, thin_nhl_wpgbos

| Variant | Net P&L $ | Fees $ | Contracts | Cents/contract | Profitable games | Profitable markets |
|---|---|---|---|---|---|---|
| behind_touch | -225.29 | 58.07 | 12,077 | -1.87 | 0/11 | 0/22 |
| behind_touch_guard | -65.62 | 10.90 | 2,364 | -2.78 | 2/11 | 5/22 |
| at_touch | -261.76 | 85.35 | 19,061 | -1.37 | 0/11 | 0/22 |
| at_touch_guard | -98.84 | 20.49 | 4,855 | -2.04 | 1/11 | 4/22 |
| at_touch_guard_micro | -73.04 | 18.17 | 4,242 | -1.72 | 1/11 | 7/22 |
| at_touch_guard_skew1 | -104.39 | 20.49 | 4,856 | -2.15 | 1/11 | 4/22 |
| at_touch_guard_skew6 | -86.62 | 20.89 | 5,021 | -1.73 | 1/11 | 4/22 |
| at_touch_guard_60s | -58.61 | 12.75 | 3,206 | -1.83 | 2/11 | 5/22 |
| behind_guard_60s | -41.34 | 6.74 | 1,536 | -2.69 | 2/11 | 9/22 |
| behind_guard_120s | +25.99 | 4.06 | 909 | +2.86 | 8/11 | 14/22 |
| at_touch_guard_120s | +31.42 | 7.34 | 1,768 | +1.78 | 8/11 | 15/22 |
| at_touch_guard_micro_60s | -47.42 | 11.45 | 2,770 | -1.71 | 1/11 | 5/22 |
| at_touch_guard_micro_120s | +21.92 | 6.55 | 1,534 | +1.43 | 8/11 | 13/22 |
| behind_guard_micro_120s | +24.50 | 4.25 | 971 | +2.52 | 8/11 | 14/22 |
