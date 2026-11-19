# Validation report

annotated facts only; unlabelled predictions excluded from precision

candidate facts including unconfirmed; confirmed-only metrics reported separately

Unavailable/zero-denominator metrics are N/A. No quality gate is implied.

| Category | TP | FP | FN | Precision | Recall | F1 | Type accuracy | Evidence | Coverage | Unconfirmed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| call_chain | 16 | 0 | 0 | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% | 100.00% |
| resource_flow | 11 | 0 | 6 | 100.00% | 64.71% | 78.57% | 100.00% | 100.00% | 64.71% | 100.00% |
| sync_relation | 0 | 0 | 0 | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

## Profile applicability

| Category | Prediction profile | Applicable cases | Not applicable cases |
|---|---|---:|---:|
| call_chain | libuv-macos-clang | 27 | 0 |
| resource_flow | libuv-macos-clang | 17 | 0 |
| sync_relation | libuv-macos-clang | 0 | 38 |
