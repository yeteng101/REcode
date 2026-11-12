# External benchmark integration

## Current analyzer revision

The group3 analyzer uses `external-v2`, defined in `protocol-v2.json` and the
active `protocol-lock.json`. It reuses the frozen v1 selections and Gold.
New predictions and reports go to `results/external-v2/` and the new baseline
record is `history/external-v2-baseline.json`. Its initial status is `not_run`.
The baseline runner checks the analyzer lock before executing the fixed subsets.

The original v1 lock, locked source files and baseline bytes are preserved in
`history/external-v1/`. `history/external-v1-baseline.json` attributes its scores
to source commit `f14e4da3f872e0d24bb24389e3e8fc88c49b8a28` and the archived lock.
The v1 scores shown below describe that historical analyzer revision.

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
protocol-v2.json
prediction.schema.json
selections/*.json
protocol-lock.json
history/external-v2-baseline.json
results/external-v2/*/predictions.json
results/external-v2/*/report.json
results/external-v2/status.json
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

## Failure audit

The frozen baseline also has a diagnosis-only audit. It opens the cases that
were not resolved, extracts source context and available tool facts, and groups
them by the analyzer capability that needs work:

```bash
python3 scripts/audit_external_failures.py
```

Outputs:

```text
validation/external/results/failure-audit.json
validation/external/results/failure-audit.md
```

The audit reads the existing Gold and prediction files for diagnosis; it does
not alter `external-v1`, its selections, predictions, or lock file.
