# Validation report

annotated facts only; unlabelled predictions excluded from precision

candidate facts including unconfirmed; confirmed-only metrics reported separately

Unavailable/zero-denominator metrics are N/A. No quality gate is implied.

| Category | TP | FP | FN | Precision | Recall | F1 | Type accuracy | Evidence | Coverage | Unconfirmed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| call_chain | 16 | 0 | 0 | 100.00% | 100.00% | 100.00% | 100.00% | 75.00% | 100.00% | 3.31% |
| resource_flow | 17 | 0 | 0 | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| sync_relation | 32 | 0 | 0 | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |

## Profile applicability

| Category | Prediction profile | Applicable cases | Not applicable cases |
|---|---|---:|---:|
| call_chain | libuv-linux-clang | 27 | 0 |
| resource_flow | libuv-linux-clang | 17 | 0 |
| sync_relation | libuv-linux-clang | 38 | 0 |
