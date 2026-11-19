# libuv Linux profile validation

- Repository: `libuv/libuv`
- Commit: `8fb9cb919489a48880680a56efecff6a7dfb4504`
- Build profile: `libuv-linux-clang`
- Environment: Ubuntu 24.04, Clang 18.1.3, arm64 container
- Graph: 909 nodes / 2469 edges

## Result

| Category | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| call_chain | 16 | 0 | 0 | 11 | 100% | 100% | 100% |
| resource_flow | 17 | 0 | 0 | 0 | 100% | 100% | 100% |
| sync_relation | 32 | 0 | 0 | 6 | 100% | 100% | 100% |

Gold and predictions both use `libuv-linux-clang`. The synchronization
analyzer now covers source-level queue publication, condition and semaphore
ordering, thread join, atomic exchange/order, rwlock boundaries, eventfd
causality and protected writes inside a lexical critical section.

This is an open-world benchmark result: it means every labelled Gold identity
and negative case in the frozen dataset is matched. It does not mean every
emitted relation in the whole repository has been proven globally correct;
3,429 synchronization predictions currently fall outside the labelled set.

## Closed-world source audit

The first closed-world audit covers every synchronization prediction whose
source endpoint is in `threadpool.c`, `unix/async.c`, `unix/thread.c` or
`unix/linux.c`.

| Decision | Count | Share |
|---|---:|---:|
| source-rule accepted | 1,370 | 88.16% |
| rejected | 184 | 11.84% |
| needs review | 0 | 0.00% |

The frozen Gold regression remains `32 TP / 0 FP / 0 FN`. The largest defect
group is 177 generic condition/event notification-to-callback pairs without a
shared resource witness. Three further predictions have endpoint evidence that
does not resolve to the claimed source anchor.

This is a reproducible machine audit, not a human-confirmed expansion of Gold.
See `validation/results/libuv/sync-closed-world-v1/report.md` and `review.csv`.

Outputs:

```text
baseline/report.md
baseline/report.json
quality-gate/gate.json
predictions/call-chains.json
predictions/resource-flow.json
predictions/sync-relations.json
```
