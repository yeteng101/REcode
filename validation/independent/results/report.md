# Independent microbenchmark results

independent synthetic C microbenchmarks; not libuv or Redis

| Project | Category | TP | FP | FN | TN | Precision | Recall | F1 | Labelled accuracy |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| callgraph | call_chain | 3 | 0 | 0 | 3 | 100.00% | 100.00% | 100.00% | 100.00% |
| resources | resource_flow | 5 | 0 | 1 | 0 | 100.00% | 83.33% | 90.91% | 83.33% |
| sync | sync_relation | 6 | 2 | 2 | 0 | 75.00% | 75.00% | 75.00% | 60.00% |

## Micro total

```text
TP=14 FP=2 FN=3 TN=3
Precision=87.50%
Recall=82.35%
F1=84.85%
Labelled accuracy=77.27%
```

Gate status: `passed`.

These results cover only the frozen synthetic microbenchmarks.
The labels are AI-authored; human review remains pending.
