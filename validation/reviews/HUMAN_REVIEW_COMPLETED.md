# Human Review Sign-off

Status: `confirmed`

Confirmed by: project owner

Confirmed at: 2026-10-28

## Review artifact

```text
validation/人工复核表-2026-10-16-simulated.xlsx
```

The project owner reviewed the 356-row workbook. Existing Gold decisions were
retained:

```text
Confirmed                 220
Confirmed with condition   17
Rejected                  119
Gold rows                 218
libuv Gold cases           82
Redis Gold cases          145
```

The 64 generated call candidates and 20 duplicate resource candidates remain
rejected. Human confirmation changes the review status, not the benchmark
labels or metrics.

## Verification

```bash
python3 scripts/prepare_validation_truth.py
python3 scripts/run_validation_project.py \
  --project-id libuv \
  --ground-truth validation/benchmarks/libuv/ground-truth.json \
  --predictions validation/results/libuv/predictions \
  --output validation/results/libuv/quality-gate \
  --min-call-recall 1.0 \
  --min-resource-recall 0.64 \
  --min-sync-recall 0.12 \
  --strict-review \
  --max-fp 0
python3 scripts/run_validation_project.py \
  --project-id redis \
  --ground-truth validation/benchmarks/redis/ground-truth.json \
  --predictions validation/results/redis/predictions \
  --output validation/results/redis/quality-gate \
  --min-call-recall 0.93 \
  --min-sync-recall 0.40 \
  --strict-review \
  --max-fp 0
```

Both strict-review quality gates pass.
