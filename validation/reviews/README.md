# Simulated Review

`simulated-review-2026-10-16.json` contains a row-level AI-assisted review of
the 356 entries in `validation/人工复核表-2026-10-16.xlsx`.

The review checks every evidence anchor against the pinned libuv/Redis source
checkouts, then applies category rules. The project owner reviewed the generated
workbook and confirmed it on 2026-10-28:

```text
human_review = confirmed
method = simulated_manual_review_by_codex
human_confirmed_at = 2026-10-28
```

Human sign-off record:

```text
validation/reviews/HUMAN_REVIEW_COMPLETED.md
validation/reviews/human-review-status.json
```

Decisions:

```text
Confirmed
Confirmed with condition
Rejected
Needs fix
```

The reviewed Excel workbook is:

```text
validation/人工复核表-2026-10-16-simulated.xlsx
```

`Source evidence` / `Target evidence` keep repository-relative local paths for
automated checking. `Source remote evidence` / `Target remote evidence` point
to the same line at the pinned GitHub commit:

```text
libuv v1.50.0 -> github.com/libuv/libuv
Redis 7.2.4  -> github.com/redis/redis
project fixtures -> github.com/yeteng101/code_ana_sys
```

Reviewed Gold snapshots are retained for audit alongside the confirmed
benchmarks:

```text
validation/benchmarks/libuv/ground-truth.simulated.json
validation/benchmarks/redis/ground-truth.simulated.json
validation/benchmarks/libuv/source-check.simulated.json
validation/benchmarks/redis/source-check.simulated.json
```

The generator is `scripts/simulate_validation_review.py`.

Reproduce without Excel dependencies:

```bash
python3 scripts/simulate_validation_review.py --skip-workbook
```

To regenerate the Excel file, run it with a Python environment that has
`openpyxl` installed:

```bash
python3 scripts/simulate_validation_review.py
```
