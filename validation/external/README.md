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

## Protocol workflow

The candidate protocol is not just a collection of scripts:

```bash
python3 scripts/build_external_selections.py
python3 scripts/check_external_protocol.py
python3 scripts/run_external_dry_run.py
python3 scripts/run_external_baseline.py --skip-dry-run
python3 scripts/freeze_external_protocol.py --check
```

The following files are versioned together:

```text
protocol-v1.json
prediction.schema.json
selections/*.json
protocol-lock.json
history/external-v1-baseline.json
results/*/predictions.json
results/*/report.json
results/status.json
```

Every run must produce schema-valid predictions. `unknown` is retained in the
prediction and is excluded from the binary confusion matrix, while coverage is
reported separately.

## Locked external-v1 baseline

```text
CallGraphBench     P 100.00% / R 58.06% / F1 73.47%
CG-Bench           P 67.60% / R 77.84% / coverage 86.42%
Juliet CWE-401     P 100.00% / R 100.00% / coverage 100.00%
SV-COMP MemSafety  P 55.56% / R 100.00% / coverage 100.00%
DataRaceBench      P 100.00% / R 100.00% / coverage 75.00%
SV-COMP Concurrency P 100.00% / R 100.00% / coverage 100.00%
```

The race numbers are for the fixed subsets in `selections/`, not the complete
DataRaceBench or SV-COMP suites. The SV-COMP MemSafety result still has false
alarms and must not be presented as a final leak-detector accuracy.

Results:

```text
validation/external/results/status.json
validation/external/results/cgbench/report.md
validation/external/history/external-v1-baseline.json
```
