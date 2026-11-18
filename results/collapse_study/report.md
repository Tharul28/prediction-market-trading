# Does a collapsing touch warn of a jump?

10 recordings, 25 markets, 84,510 snapshots. Collapse: same best price, previous size >= 50, at most 20% left, snapshots <= 15 s apart. Jump: mid moves >= 8c in 60 s.

## After a collapse (precision)

Base rate: at a random snapshot, the price moves 5c+ in a given direction within 60 s 1.7% of the time.

| Collapses | Cancelled share > 50% | n | Mean signed move 10 s / 30 s / 60 s (c) | Moved 5c+ the predicted way in 60 s | vs base rate |
|---|---|---|---|---|---|
| all | 91% | 1945 | +0.22 / +0.32 / +0.25 | 7.6% | 4.5x |
| mostly cancelled | 100% | 1779 | +0.23 / +0.32 / +0.24 | 7.8% | 4.7x |
| mostly traded | 0% | 166 | +0.14 / +0.31 / +0.28 | 5.0% | 3.0x |

By recording:

| Recording | Collapses | Mean signed 60 s move (c) | Big move rate |
|---|---|---|---|
| den_nor | 201 | +0.56 | 5.0% |
| ger_ned | 196 | +0.21 | 2.6% |
| isr_aut | 182 | +0.29 | 4.5% |
| nba_gsw_nop | 141 | +0.15 | 2.1% |
| nba_phx_den | 158 | +0.67 | 16.7% |
| nba_por_min | 133 | -0.26 | 4.5% |
| nfl_lac_bal | 469 | +0.21 | 14.4% |
| nhl_van_uta | 131 | -0.16 | 1.6% |
| pol_swe | 182 | +0.66 | 6.1% |
| wal_por | 152 | -0.09 | 5.3% |

## Before a jump (recall)

105 jumps of 8c+ across all markets. 46% had a collapse in the jump's direction in the 60 s before it started; median warning 30 s (range 3-58 s).

| Recording | Jumps | Warned | Median warning (s) |
|---|---|---|---|
| den_nor | 14 | 8 | 32 |
| ger_ned | 7 | 3 | 8 |
| isr_aut | 8 | 2 | 50 |
| nba_gsw_nop | 5 | 1 | 10 |
| nba_phx_den | 13 | 8 | 35 |
| nba_por_min | 12 | 5 | 30 |
| nfl_lac_bal | 20 | 15 | 6 |
| nhl_van_uta | 12 | 0 | - |
| pol_swe | 6 | 4 | 21 |
| wal_por | 8 | 2 | 36 |
