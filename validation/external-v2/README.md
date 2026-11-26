# External validation v2

`external-v2` is a new frozen protocol. It does not modify or overwrite
`validation/external/` (`external-v1`).

The first dataset moved to v2 is CallGraphBench, because the native pipeline
now uses `FunctionTargetResolver` and collapses callback intermediates into
CallGraphBench caller-target edges.

## CallGraphBench v2

```text
cases:      6
gold edges: 31
TP:         31
FP:          0
FN:          0
precision: 100.00%
recall:    100.00%
F1:        100.00%
```

Outputs:

```text
protocol-v2.json
protocol-lock-v2.json
selections/callgraphbench-v2.json
results/callgraphbench/predictions.json
results/callgraphbench/summary.json
results/callgraphbench/report.md
```

CG-Bench and the race datasets stay on the v1 baseline until a separate v2
run is completed and accepted.

## SV-COMP MemSafety v2

The resource-flow lifecycle state machine and multi-call release expansion are
frozen independently from v1:

```text
TP:        10
FP:         7
FN:         0
TN:         3
precision: 58.82%
recall:    100.00%
coverage:  100.00%
```

Compared with v1, FP decreased from 8 to 7 without reducing recall. This is
still a fixed subset and not a full SV-COMP result.

## Juliet CWE-401 v2

The lifecycle-state/multi-call resource-flow generator preserves the v1
function-level result:

```text
TP:        5
FP:        0
FN:        0
TN:       23
precision: 100.00%
recall:    100.00%
coverage:  100.00%
```
