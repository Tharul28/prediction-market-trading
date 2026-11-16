| Scenario | Net P&L $ (mean) | Net P&L $ (std) | Gross P&L $ | Fees $ | Liquidation P&L $ | Profitable sessions | Fills | Max abs inventory | Max drawdown $ |
|---|---|---|---|---|---|---|---|---|---|
| MM: baseline (back of queue, fees on) | -3.82 | 3.14 | -1.58 | 2.24 | -3.87 | 10% | 75 | 22 | 4.81 |
| MM: fees off | -1.58 | 2.95 | -1.58 | 0.00 | -1.63 | 27% | 75 | 22 | 3.55 |
| MM: no inventory skew | 0.74 | 7.27 | 3.92 | 3.18 | 0.43 | 50% | 114 | 74 | 9.66 |
| MM: front of queue (unrealistic) | 3.08 | 3.42 | 12.01 | 8.93 | 3.03 | 77% | 342 | 32 | 3.49 |
| MM: front of queue, fees off | 12.01 | 3.50 | 12.01 | 0.00 | 11.96 | 100% | 342 | 32 | 2.71 |
| MM: half-spread 0.5c | -36.77 | 5.35 | -9.80 | 26.98 | -36.83 | 0% | 668 | 24 | 36.97 |
| MM: half-spread 2c | -3.38 | 2.68 | -2.83 | 0.55 | -3.47 | 3% | 14 | 24 | 4.45 |
| MM: half-spread 3c | -3.22 | 3.18 | -2.88 | 0.34 | -3.36 | 7% | 7 | 25 | 4.73 |
| Arb: fees on | -0.53 | 2.26 | 0.17 | 0.70 | -0.84 | 47% | 5 | 19 | 3.50 |
| Arb: fees off | 0.34 | 4.27 | 0.34 | 0.00 | -0.73 | 57% | 18 | 50 | 9.70 |
| Arb: fees off, no exposure cap | 19.82 | 15.66 | 19.82 | 0.00 | 18.14 | 97% | 582 | 100 | 20.22 |
