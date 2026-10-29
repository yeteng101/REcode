# 外部评测集接入（2026-10-28）

> 本文保留首轮接入记录。最新协议、三值 race 判定、CG-Bench Agent baseline
> 和固定子集指标见
> [external-protocol-v1-2026-10-29.md](external-protocol-v1-2026-10-29.md)。

## 1. 评测来源

合成评测集已从正式入口移除。正式评测只使用固定版本的外部公开基准：

```text
调用图:
  CallGraphBench + CG-Bench

资源流:
  NIST Juliet C/C++ 1.3 + SV-COMP 2026 C.MemSafety

同步并发:
  DataRaceBench + SV-COMP 2026 C.Concurrency
```

版本和许可证记录在：

```text
validation/external/registry.json
```

第三方源码不进入 Git。运行：

```bash
python3 scripts/fetch_external_benchmarks.py --group call_graph
python3 scripts/fetch_external_benchmarks.py --group resource_flow
python3 scripts/fetch_external_benchmarks.py --group sync_relation
```

数据会缓存到 `.benchmark-cache/`，并按 commit 或官方 SHA-256 校验。

## 2. CallGraphBench

固定版本：

```text
commit fe003b9e9de38d06aa67cabb8643a55dbac7dcbf
license MIT
```

本轮运行全部 6 个官方 C/C++ case，使用基准自带的边级 Ground Truth 和评测器：

| 语言 | TP | FP | FN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|
| C | 11 | 0 | 6 | 100% | 64.71% | 78.57% |
| C++ | 7 | 0 | 7 | 100% | 50.00% | 66.67% |
| 合计 | 18 | 0 | 13 | 100% | 58.06% | 73.47% |

主要漏检：

```text
C function-pointer dispatch
C callback aliases
C++ constructor and const method calls
C++ virtual dispatch
multi-file callback registration
```

结果：

```text
validation/external/results/callgraphbench/report.md
validation/external/results/callgraphbench/summary.json
```

## 3. CG-Bench

固定版本：

```text
commit b277b7329f62fdddc6d4a341057d045bdf69def2
license MIT
```

使用官方 `extract_from_markdowns.py` 解析原始案例：

```text
callsites 105
targets   614
projects  curl, ffmpeg, gcc, openssh, redis, wrk, zfs
```

CG-Bench 评估的是函数指针 callsite 的目标函数集合，不是完整调用图。
它需要 Agent 或外部模型对每个 callsite 输出 target set，因此与 CallGraphBench
分开报告，不能合成一个总体准确率。

提取结果：

```text
validation/external/results/cgbench/cases.json
validation/external/results/cgbench/summary.json
```

## 4. 资源流与同步基准

已固定：

```text
NIST Juliet C/C++ 1.3:
  official SHA-256 ada9d7e1c323d283446df3f55bdee0d00bda1fed786785fe98764d58688f38eb

SV-COMP 2026:
  commit 7efe28dd29576b46927b7a34e8f742bd90966a75

DataRaceBench:
  commit 7d07c5d637fa80f1fd7c239bdc34ae53094badc2
```

这些数据集的标签主要是：

```text
Juliet: CWE-401 / CWE-404 / CWE-415 / CWE-416 等 Good/Bad 案例
SV-COMP MemSafety: valid-free / valid-deref / valid-memtrack / valid-memcleanup
SV-COMP Concurrency: no-data-race / memsafety
DataRaceBench: data-race / no-data-race
```

它们不是现成的“同步关系边”或“资源操作链”Gold。适配器必须把分析器输出映射为
对应任务判定，并分别报告，不允许把不同任务的标签混成一个总分。

已生成确定性 case index：

```text
NIST Juliet:     20 个案例，覆盖 CWE401/404/415/416
DataRaceBench:   24 个案例，12 race / 12 no-race
SV-COMP:         20 MemSafety + 20 Concurrency
```

NIST Juliet CWE-401 的首轮逐函数泄漏判定：

| Parsed cases | Skipped | TP | FP | FN | TN | Precision | Recall | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 15 | 5 | 15 | 45 | 0 | 24 | 25.00% | 100.00% | 46.43% |

5 个 Windows 专用 CWE404 案例在当前 macOS 主机无法解析，已明确记为 skipped。
当前规则把大量 `goodB2G`/`goodG2B` 也判成泄漏，False Positive 很高。

SV-COMP MemSafety 的首轮 `valid-memtrack` 资源泄漏判定：

| TP | FP | FN | TN | Precision | Recall | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 10 | 0 | 0 | 50.00% | 100.00% | 50.00% |

这说明当前规则把所有安全案例也判成了泄漏，Recall 高但 Precision 不足。

SV-COMP Concurrency 的首轮“存在同步关系即判 no-data-race”基线：

| TP | FP | FN | TN | Precision | Recall | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 10 | 0 | 0 | 50.00% | 100.00% | 50.00% |

这只是一个同步关系存在性基线，不是完整 race detector，不能当作最终并发准确率。

DataRaceBench 的首轮 source synchronization baseline：

| TP | FP | FN | TN | Precision | Recall | Accuracy |
|---:|---:|---:|---:|---:|---:|---:|
| 12 | 12 | 0 | 0 | 50.00% | 100.00% | 50.00% |

该基线只检查 OpenMP 显式同步标记，无法理解 private/reduction 等数据作用域规则，
因此所有安全案例都被误判为 race。

## 5. 当前边界

当前可执行的外部结果包括 CallGraphBench、NIST Juliet CWE-401、SV-COMP
MemSafety、DataRaceBench 和 SV-COMP Concurrency 基线。CG-Bench 已完成
官方案例提取，下一步需要接入 Agent target-set。

旧 libuv/Redis 数据继续保留为 regression suite，不再作为独立准确率证据。
