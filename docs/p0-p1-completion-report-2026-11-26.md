# P0/P1 完成说明

日期：2026-11-26

## 1. 完成范围

本文记录本轮 P0/P1 的定义、实现、验证证据和剩余边界。

### P0

1. 将四文件同步审计从“只评价已生成预测”升级为冻结候选 universe。
2. 为审计增加 CI 硬门禁。

### P1

1. CFG/路径条件。
2. Wrapper summary。
3. 局部资源 alias。
4. Thread identity。
5. CallGraphBench external-v2。
6. 资源 lifecycle state、负例回归和指针算术归一化。
7. Redis v2 与外部基准统一回归门禁。

## 2. P0 候选 Universe

范围：

```text
third_party/libuv/src/threadpool.c
third_party/libuv/src/unix/async.c
third_party/libuv/src/unix/thread.c
third_party/libuv/src/unix/linux.c
```

候选来源包括：

- 同函数相邻事件。
- 同函数锁获取/释放。
- queue producer/consumer。
- condition notify/wait。
- atomic release/acquire 和 relaxed 边界。
- thread create/join。
- rwlock release/acquire。
- event notify/wait。
- 有队列 witness 的 notification callback。
- async atomic callback。
- 冻结 Gold。

结果：

```text
candidate universe: 530
covered:            530
missing:              0
universe_recall:    100%
```

候选 universe 位于：

```text
validation/results/libuv/sync-closed-world-v1/universe.json
```

### 边界

这是冻结“机制候选 universe”，不是全仓所有理论可能的任意事件配对。它保证所选范围内所有既定机制候选都被评价，但新增机制仍需扩展 universe 定义。

## 3. P0 审计结果

预测侧：

```text
predictions: 1277
accepted:    1277
rejected:       0
needs_review:   0
```

Gold：

```text
32 TP / 0 FP / 0 FN / 6 TN
Precision / Recall / F1 = 100%
Evidence = 100%
```

输出：

```text
validation/results/libuv/sync-closed-world-v1/audit.json
validation/results/libuv/sync-closed-world-v1/gate.json
validation/results/libuv/sync-closed-world-v1/report.md
validation/results/libuv/sync-closed-world-v1/review.csv
validation/results/libuv/sync-closed-world-v1/selection.json
validation/results/libuv/sync-closed-world-v1/universe.json
```

## 4. P1 CFG 与路径条件

新增源码控制区域解析：

- `if/else`
- `for/while/do`
- preprocessor `#if/#elif/#else/#endif`
- 互斥分支
- 循环回边

同一行号先后不再自动等同 HB：

- 两个事件位于互斥分支时输出 unknown。
- 同循环内反向行号可能形成回边时输出 unknown。
- 直线源码顺序继续输出 happens_before。
- condition 字段记录源码路径条件。

实现：

```text
clang_pipeline/source_context.py
clang_pipeline/sync_flow.py
```

测试：

```text
clang_pipeline/tests/test_source_context.py
```

## 5. P1 Wrapper Summary

如果函数只封装一个已知同步 API，则自动推导 wrapper 语义：

```text
my_lock    -> acquire
my_unlock  -> release
my_wait    -> wait
my_notify  -> notify
```

推导条件：

- 函数体只有一个已知同步 API 调用。
- 内部资源参数对应 wrapper 参数。
- wrapper 调用点继承内部操作类型和资源表达式。

该能力用于减少跨仓库自定义锁、信号和等待封装造成的 FN。

## 6. P1 Alias 传播

同步分析加入函数内局部 alias：

```c
uv_mutex_t* local = &global_mutex;
uv_mutex_lock(local);
uv_mutex_unlock(&global_mutex);
```

两条锁操作现在会归一到同一资源。

资源流增加：

- 同一行多调用展开：`free(a); free(b);`
- 指针算术归一化：

```c
buffer = malloc(...);
buffer++;
free(buffer - 1);
```

会归一到 `buffer`。

## 7. P1 Thread Identity

从以下 API 推导 callback 所属 thread handle：

```text
uv_thread_create
uv_thread_create_ex
pthread_create
```

thread label 会沿直接调用传播，例如：

```text
worker -> thread:threads + i
```

该身份用于：

- 区分相同函数在不同线程中的实例。
- 改善跨线程 HB 和 concurrent 判断。
- 避免只靠函数名猜测 worker/main/event-loop。

## 8. P1 资源 Lifecycle State

资源组新增：

```text
released
transferred
possibly_owned
unknown
```

分类规则：

- 有 acquire 且 release 数足够：released / no_leak。
- 有 acquire 但未观察到足够 release：possible_leak。
- 发生 transfer 但仍无 release：保留 possible_leak，不把 transfer 自动当成释放。
- 仅有 release 没有 acquire：unknown。

SV-COMP MemSafety v2 结果：

```text
TP:        10
FP:         7
FN:         0
TN:         3
precision: 58.82%
recall:    100%
coverage:  100%
```

v1 为 `10 TP / 8 FP / 0 FN / 2 TN`。本轮减少 1 个 FP，Recall 保持 100%。

Juliet CWE-401 v2：

```text
TP:        5
FP:        0
FN:        0
TN:       23
precision: 100%
recall:    100%
```

## 9. P1 External-v2

external-v1 保持不变。新生成器结果冻结在：

```text
validation/external-v2/
```

### CallGraphBench v2

```text
31 TP / 0 FP / 0 FN
Precision / Recall / F1 = 100%
```

### Juliet CWE-401 v2

```text
5 TP / 0 FP / 0 FN / 23 TN
```

### SV-COMP MemSafety v2

```text
10 TP / 7 FP / 0 FN / 3 TN
```

锁文件：

```text
validation/external-v2/protocol-lock-v2.json
```

## 10. Redis 与外部回归门禁

CI 现在包含：

```text
libuv:
  call recall    = 100%
  resource recall= 100%
  sync recall    = 100%
  max FP         = 0

Redis v2:
  call recall    >= 98%
  resource recall>= 96%
  sync recall    >= 40.9%
  max FP         = 0
```

外部回归脚本：

```text
scripts/check_external_regressions.py
```

external-v1 artifacts lock：

```bash
python3 scripts/freeze_external_protocol.py --check --artifacts-only
```

external-v2 lock：

```bash
python3 scripts/freeze_external_v2_protocol.py --check
```

## 11. 当前 libuv 结果

| 类别 | TP | FP | FN | TN | Precision | Recall | F1 | Evidence |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 调用链 | 16 | 0 | 0 | 11 | 100% | 100% | 100% | 75% |
| 资源流 | 17 | 0 | 0 | 0 | 100% | 100% | 100% | 100% |
| 同步关系 | 32 | 0 | 0 | 6 | 100% | 100% | 100% | 100% |

同步预测：

```text
total:      2,982
unlabelled: 2,950
```

## 12. 验证证据

最终执行：

- Agent/分析器测试：115 项通过，2 项 Linux Docker 测试跳过。
- 验证器/外部协议测试：50 项通过。
- libuv strict quality gate：通过。
- Redis v2 gate：通过。
- external-v1 artifacts lock：通过。
- external-v2 protocol lock：通过。
- 外部指标回归检查：通过。
- universe 重复生成 diff：无差异。
- `compileall`：通过。
- `git diff --check`：通过。

## 13. 当前 Git 状态

已推送基线：

```text
74def0fb44b181e8a71c75b2afaa86753c2fc513
```

P0/P1 代码、验证结果和文档仍在本地工作区，尚未提交或推送。

## 14. 未包含范围

- CG-Bench external-v2 尚未建立。
- DataRaceBench 和 SV-COMP Concurrency external-v2 尚未建立。
- 四文件 universe 需要人工确认后才能升级为人确认 Gold。
- Windows/macOS profile 未纳入本轮 Linux Gold。
- 两项真实 Linux Docker 集成测试仍依赖目标 CI/部署环境。
