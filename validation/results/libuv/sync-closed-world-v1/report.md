# Libuv synchronization closed-world audit

This is an independent source-rule audit of predictions originating in the selected files. It is not a human-confirmed expansion of Gold.

## Scope

- Repository: `libuv/libuv`
- Commit: `8fb9cb919489a48880680a56efecff6a7dfb4504`
- Build profile: `libuv-linux-clang`
- Source files: 4
- In-scope predictions: 1554

## Gold regression

- TP: 32
- FP: 0
- FN: 0

## Prediction-side audit

| Decision | Count | Share |
|---|---:|---:|
| accepted | 1370 | 88.16% |
| rejected | 184 | 11.84% |
| needs_review | 0 | 0.00% |

- Rule-audit pass rate: 88.16%
- Decided share: 100.00%
- Unresolved after source rules: 0

## By source file

| File | Accepted | Rejected | Needs review |
|---|---:|---:|---:|
| `third_party/libuv/src/threadpool.c` | 397 | 87 | 0 |
| `third_party/libuv/src/unix/async.c` | 270 | 4 | 0 |
| `third_party/libuv/src/unix/thread.c` | 202 | 89 | 0 |
| `third_party/libuv/src/unix/linux.c` | 501 | 4 | 0 |

## By relation type

| Relation type | Accepted | Rejected | Needs review |
|---|---:|---:|---:|
| `atomic` | 48 | 3 | 0 |
| `condition_variable` | 39 | 131 | 0 |
| `event_notify` | 15 | 47 | 0 |
| `event_wait` | 28 | 1 | 0 |
| `happens_before` | 1100 | 2 | 0 |
| `mutex_lock` | 107 | 0 | 0 |
| `rwlock` | 19 | 0 | 0 |
| `thread_join` | 14 | 0 | 0 |

## Audit reasons

| Reason | Count |
|---|---:|
| same-function source order is consistent | 1293 |
| generic notification/callback pair has no shared resource witness | 177 |
| matches a frozen positive Gold identity and order | 32 |
| queue producer, consumer and protected queue witness agree | 10 |
| semaphore post/wait direction verified | 10 |
| condition notification/wait endpoints share the declared resource | 7 |
| rwlock release/acquire direction verified | 6 |
| atomic release/acquire uses the same resource token | 5 |
| endpoint is not backed by one of the item's evidence anchors | 3 |
| relaxed atomic boundary lacks a shared async/resource witness | 3 |
| thread create/join direction verified | 3 |
| async notification/wait endpoint direction verified | 2 |
| event-loop pair does not contain notify then wait endpoints | 1 |
| failed try-lock unknown boundary verified | 1 |
| specific same-function concurrency boundary | 1 |

## Interpretation

`accepted` means an independent local source rule supports the emitted claim. `rejected` means the emitted endpoint/direction/resource pairing contradicts the rule. `needs_review` means the available source facts do not prove or refute the claim. Only the frozen Gold result is a confirmed regression metric; the prediction-side audit must not be described as human-confirmed precision.
