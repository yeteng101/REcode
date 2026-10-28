# External benchmark integration

The official evaluation sources are pinned in `registry.json`:

```text
call graph:
  CallGraphBench + CG-Bench

resource flow:
  NIST Juliet C/C++ 1.3 + SV-COMP 2026 C.MemSafety

synchronization:
  DataRaceBench + SV-COMP 2026 C.Concurrency
```

Large benchmark sources are not committed. Fetch them into the git-ignored
`.benchmark-cache/`:

```bash
python3 scripts/fetch_external_benchmarks.py --group call_graph
python3 scripts/fetch_external_benchmarks.py --group resource_flow
python3 scripts/fetch_external_benchmarks.py --group sync_relation
```

Every Git source is checked out at a pinned commit. The NIST Juliet archive is
checked against the SHA-256 published by NIST SARD.

External labels are kept in their original benchmark semantics. The adapters
must not rewrite labels to match analyzer output.

## Current status

```text
CallGraphBench   runnable; official C/C++ edge-level baseline produced
CG-Bench         official parser and callsite extraction complete
NIST Juliet      CWE-401 baseline produced; 5 platform-specific cases skipped
SV-COMP MemSafety 20-case baseline produced
DataRaceBench    24-case source baseline produced
SV-COMP Concurrency 20-case baseline produced
```

CallGraphBench result:

```text
validation/external/results/callgraphbench/report.md
```

CG-Bench extraction:

```text
validation/external/results/cgbench/summary.json
```
