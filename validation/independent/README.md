# Independent microbenchmark suite

This directory contains evaluation data built before running the analyzer on
the benchmark sources. It is intentionally independent from libuv and Redis.

## Why this exists

The legacy libuv call-chain benchmark was partly derived from the same graph
used to generate predictions. A score of 100% on that data is a regression
result, not an independent accuracy estimate.

The suite in this directory uses a stricter protocol:

1. Write synthetic C sources from a semantic specification.
2. Write all expected facts and negatives before running the analyzer.
3. Hash and freeze every source file and `ground-truth.json`.
4. Only after the freeze check passes, run the analyzer.
5. Score every in-scope prediction. Unlabelled in-scope predictions count as
   false positives in closed-world mode.

The fixtures are deliberately small enough to annotate completely. They are
not intended to estimate performance on every C/C++ program.

## Projects

| Project | Target category | Scope |
|---|---|---|
| `callgraph` | `call_chain` | direct, nested, macro-expanded, and absent calls |
| `resources` | `resource_flow` | allocation/release, alias, transfer, incomplete lifetime, fd |
| `sync` | `sync_relation` | mutex, condition variable, thread join, release/acquire, relaxed |

Resource-flow scoring uses `source_anchor_v1`:

```text
[function, file, line] + operation kind
```

It does not use the analyzer's internal resource ID. This prevents the Gold set
from depending on the implementation being evaluated.

Each project has `annotation_scope.mode=closed_world`. Inside the declared
source path, any prediction not present in Gold is a false positive. Only
predictions outside the declared microbenchmark scope are ignored.

The legacy libuv and Redis benchmarks remain available only as regression
suites. They must not be reported as independent accuracy evidence.

The new synthetic and libuv holdout labels are AI-authored from source
specifications. Their `human_review` state is `pending`; independent execution
quality is not the same as human acceptance of the Gold labels.
