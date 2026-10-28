# libuv post-freeze holdout results

pinned libuv v1.50.0 holdout; labels frozen before native extraction

| Project | Category | TP | FP | FN | TN | Precision | Recall | F1 | Labelled accuracy |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| callgraph | call_chain | 11 | 0 | 0 | 0 | 100.00% | 100.00% | 100.00% | 100.00% |
| resources | resource_flow | 5 | 0 | 0 | 0 | 100.00% | 100.00% | 100.00% | 100.00% |
| sync | sync_relation | 1 | 0 | 2 | 0 | 100.00% | 33.33% | 50.00% | 33.33% |

## Holdout total

```text
TP=17 FP=0 FN=2 TN=0
Precision=100.00%
Recall=89.47%
F1=94.44%
Labelled accuracy=89.47%
```

Gate status: `passed`.

Human review of this new holdout remains pending.
