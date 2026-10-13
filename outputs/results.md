## Closed loop, one year, crew budget 6 jobs/week (mean of seeds)

| policy | uptime | failures | planned jobs | precision at budget | alert within 30d of failure | median lead (d) | failures caught | cost |
|---|---|---|---|---|---|---|---|---|
| run-to-failure | 98.85% | 223.6 | 0.0 | - | - | - | 0.0% | 5.27M |
| fixed-interval | 99.00% | 111.2 | 310.0 | 3.7% | 8.6% | 7.7 | 9.3% | 4.01M |
| alarm-limits | 99.30% | 58.2 | 279.6 | 30.8% | 49.4% | 5.6 | 59.6% | 2.70M |
| weibull-age | 98.84% | 223.2 | 8.0 | 12.5% | 12.5% | 4.3 | 0.4% | 5.29M |
| cox-sensors | 99.19% | 128.6 | 110.2 | 48.2% | 85.2% | 6.1 | 29.3% | 3.40M |
| gbm-sensors | 99.47% | 44.2 | 211.4 | 45.6% | 84.3% | 7.7 | 68.7% | 2.02M |
| oracle | 99.65% | 2.0 | 237.4 | 100.0% | 100.0% | 12.4 | 99.2% | 1.12M |

## Paired difference, gbm-sensors minus each policy (same seeds)

| vs | uptime pp (mean) | min..max | seeds gbm better | cost difference |
|---|---|---|---|---|
| run-to-failure | +0.62 | +0.58..+0.67 | 5 | -3,252k |
| fixed-interval | +0.47 | +0.42..+0.50 | 5 | -1,994k |
| alarm-limits | +0.17 | +0.15..+0.19 | 5 | -680k |
| weibull-age | +0.63 | +0.59..+0.68 | 5 | -3,273k |
| cox-sensors | +0.28 | +0.25..+0.33 | 5 | -1,386k |

## Crew budget sweep (uptime / precision at budget)

| policy | 3/wk | 6/wk | 10/wk |
|---|---|---|---|
| fixed-interval | 98.98% / 7% | 99.00% / 4% | 99.03% / 6% |
| alarm-limits | 99.24% / 63% | 99.30% / 31% | 99.28% / 27% |
| gbm-sensors | 99.37% / 74% | 99.47% / 46% | 99.47% / 44% |
| oracle | 99.41% / 100% | 99.65% / 100% | 99.65% / 100% |

## Offline, held-out year (latent base rate 3.17%)

| policy | avg precision | weekly precision at budget | gradual failures flagged >=2d ahead | median lead (d) | sudden flagged | C-index |
|---|---|---|---|---|---|---|
| fixed-interval | - | 7.3% | 54.8% | 30.0 | 42.9% | - |
| alarm-limits | 0.282 | 51.0% | 89.7% | 12.0 | 33.3% | 0.647 |
| weibull-age | 0.061 | - | 0.0% | 0.0 | 0.0% | 0.660 |
| cox-sensors | 0.438 | 79.5% | 49.2% | 15.0 | 0.0% | 0.779 |
| gbm-sensors | 0.681 | 77.3% | 97.6% | 15.0 | 0.0% | 0.753 |

Weibull AFT remaining-life C-index (age, type, duty only): 0.742

## gbm-sensors in the closed loop, by latent failure mode

| mode | caught | would have failed | share |
|---|---|---|---|
| bearing | 152 | 156 | 97.4% |
| seal | 136 | 146 | 93.2% |
| overheating | 85 | 97 | 87.6% |
| valve | 26 | 31 | 83.9% |
| fouling | 42 | 44 | 95.5% |
| tube_leak | 40 | 40 | 100.0% |
| sudden | 1 | 189 | 0.5% |
