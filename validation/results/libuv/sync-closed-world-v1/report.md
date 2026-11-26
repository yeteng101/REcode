# Libuv synchronization closed-world audit

This is an independent source-rule audit of predictions originating in the selected files. It is not a human-confirmed expansion of Gold.

## Scope

- Repository: `libuv/libuv`
- Commit: `8fb9cb919489a48880680a56efecff6a7dfb4504`
- Build profile: `libuv-linux-clang`
- Source files: 4
- In-scope predictions: 1277
- Audit gate: passed

## Gold regression

- TP: 32
- FP: 0
- FN: 0

## Prediction-side audit

| Decision | Count | Share |
|---|---:|---:|
| accepted | 1277 | 100.00% |
| rejected | 0 | 0.00% |
| needs_review | 0 | 0.00% |

## Candidate universe

- Total: 530
- Covered: 530
- Missing: 0
- Recall: 100.00%
- Same-function candidates: 495

- Rule-audit pass rate: 100.00%
- Decided share: 100.00%
- Unresolved after source rules: 0

## By source file

| File | Accepted | Rejected | Needs review |
|---|---:|---:|---:|
| `third_party/libuv/src/threadpool.c` | 396 | 0 | 0 |
| `third_party/libuv/src/unix/async.c` | 262 | 0 | 0 |
| `third_party/libuv/src/unix/thread.c` | 118 | 0 | 0 |
| `third_party/libuv/src/unix/linux.c` | 501 | 0 | 0 |

## By relation type

| Relation type | Accepted | Rejected | Needs review |
|---|---:|---:|---:|
| `atomic` | 47 | 0 | 0 |
| `condition_variable` | 29 | 0 | 0 |
| `event_notify` | 12 | 0 | 0 |
| `event_wait` | 28 | 0 | 0 |
| `happens_before` | 1053 | 0 | 0 |
| `mutex_lock` | 92 | 0 | 0 |
| `rwlock` | 5 | 0 | 0 |
| `thread_join` | 11 | 0 | 0 |

## Audit reasons

| Reason | Count |
|---|---:|
| same-function source order is consistent | 1201 |
| matches a frozen positive Gold identity and order | 32 |
| specific same-function unknown boundary | 21 |
| queue producer, consumer and protected queue witness agree | 10 |
| queue publication supplies the notification/callback witness | 4 |
| thread create/join direction verified | 3 |
| atomic release/acquire uses the same resource token | 2 |
| condition notification/wait resource verified | 2 |
| async notification/wait endpoint direction verified | 1 |
| semaphore post/wait direction verified | 1 |

## Gate checks

| Check | Status | Actual | Requirement |
|---|---|---:|---:|
| rejected | passed | 0 | <= 0 |
| needs_review | passed | 0 | <= 0 |
| accepted | passed | 1277 | >= 1277 |
| gold_precision | passed | 1.0 | >= 1.0 |
| gold_recall | passed | 1.0 | >= 1.0 |
| universe_recall | passed | 1.0 | >= 1.0 |
| generic_notification_pairs | passed | 0 | == 0 |

## Interpretation

`accepted` means an independent local source rule supports the emitted claim. `rejected` means the emitted endpoint/direction/resource pairing contradicts the rule. `needs_review` means the available source facts do not prove or refute the claim. Only the frozen Gold result is a confirmed regression metric; the prediction-side audit must not be described as human-confirmed precision.
