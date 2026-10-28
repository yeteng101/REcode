# CallGraphBench Evaluation Report

- Tool: `prediction files`
- Cases: 6

## Overall

| TP | FP | FN | Precision | Recall | F1 |
|---:|---:|---:|---:|---:|---:|
| 18 | 0 | 13 | 1.000 | 0.581 | 0.735 |

## By Language

| Language | Cases | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c | 3 | 11 | 0 | 6 | 1.000 | 0.647 | 0.786 |
| cpp | 3 | 7 | 0 | 7 | 1.000 | 0.500 | 0.667 |

## By Case

| Language | Case | TP | FP | FN | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|---:|
| c | c_basic | 4 | 0 | 0 | 1.000 | 1.000 | 1.000 |
| c | c_dynamic | 3 | 0 | 4 | 1.000 | 0.429 | 0.600 |
| c | c_multifile | 4 | 0 | 2 | 1.000 | 0.667 | 0.800 |
| cpp | cpp_basic | 1 | 0 | 3 | 1.000 | 0.250 | 0.400 |
| cpp | cpp_dispatch | 3 | 0 | 2 | 1.000 | 0.600 | 0.750 |
| cpp | cpp_multifile | 3 | 0 | 2 | 1.000 | 0.600 | 0.750 |

## Mismatches

### c/c_dynamic

False negatives:
- `c:c_dynamic:process_device` -> `c:c_dynamic:tcp_open`
- `c:c_dynamic:process_device` -> `c:c_dynamic:tcp_read`
- `c:c_dynamic:process_device` -> `c:c_dynamic:udp_open`
- `c:c_dynamic:process_device` -> `c:c_dynamic:udp_read`

### c/c_multifile

False negatives:
- `c:c_multifile:run_pipeline` -> `c:c_multifile:suffix_handler`
- `c:c_multifile:run_pipeline` -> `c:c_multifile:uppercase_handler`

### cpp/cpp_basic

False negatives:
- `cpp:cpp_basic:User.welcome` -> `cpp:cpp_basic:greet`
- `cpp:cpp_basic:main` -> `cpp:cpp_basic:User.User`
- `cpp:cpp_basic:main` -> `cpp:cpp_basic:User.welcome`

### cpp/cpp_dispatch

False negatives:
- `cpp:cpp_dispatch:execute_rule` -> `cpp:cpp_dispatch:LengthRule.apply`
- `cpp:cpp_dispatch:execute_rule` -> `cpp:cpp_dispatch:PrefixRule.apply`

### cpp/cpp_multifile

False negatives:
- `cpp:cpp_multifile:dispatch` -> `cpp:cpp_multifile:JsonParser.handle`
- `cpp:cpp_multifile:dispatch` -> `cpp:cpp_multifile:TextParser.handle`
