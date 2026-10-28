# SV-COMP Concurrency synchronization baseline

| TP | FP | FN | TN | Precision | Recall | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 10 | 0 | 0 | 50.00% | 100.00% | 50.00% |

This is a baseline that predicts `no-data-race` when at least one
synchronization relation is extracted. It is not a full race detector.
