# DataRaceBench tri-state race baseline

| TP | FP | FN | TN | Unknown | Race Precision | Race Recall | False Race Rate | No-race Recall | Coverage |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 8 | 0 | 0 | 10 | 6 | 100.00% | 100.00% | 0.00% | 100.00% | 75.00% |

Unknown is excluded from the binary confusion matrix and is not
converted to no-race. Accuracy is intentionally omitted unless
the unknown count is zero.
