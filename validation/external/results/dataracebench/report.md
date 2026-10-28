# DataRaceBench synchronization baseline

| TP | FP | FN | TN | Precision | Recall | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|
| 12 | 12 | 0 | 0 | 50.00% | 100.00% | 50.00% |

This source-only baseline predicts `race` when no explicit OpenMP
synchronization marker is present. It is not a full race detector.
