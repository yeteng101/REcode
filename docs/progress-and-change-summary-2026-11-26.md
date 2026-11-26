# 项目进度与改动说明

日期：2026-11-26

## 1. 当前仓库状态

- 仓库：`yeteng101/REcode`
- 分支：`main`
- 已推送基线：`74def0fb44b181e8a71c75b2afaa86753c2fc513`
- P0/P1 代码、验证结果和文档当前位于工作区，尚未提交。
- P0/P1 内容见本文件第 4.5、5、6、8 节。

## 2. 本轮总体进度

本阶段工作集中在三个方面：

1. 修正 libuv 验证集的 commit、build profile、identity、负例和 evidence 口径。
2. 改进调用链、资源流和同步关系原生分析器。
3. 增加四文件 closed-world 审计，并删除无 witness 的 notification/callback 误配对。

当前 libuv Linux-profile 正式结果：

| 类别 | TP | FP | FN | TN | Precision | Recall | F1 | Evidence |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 调用链 | 16 | 0 | 0 | 11 | 100% | 100% | 100% | 75% |
| 资源流 | 17 | 0 | 0 | 0 | 100% | 100% | 100% | 100% |
| 同步关系 | 32 | 0 | 0 | 6 | 100% | 100% | 100% | 100% |

固定条件：

```text
repository: libuv/libuv
commit: 8fb9cb919489a48880680a56efecff6a7dfb4504
build_profile: libuv-linux-clang
environment: Ubuntu 24.04 + Clang 18
```

## 3. 验证集改进

### 3.1 Profile 对齐

同步 Gold 和 predictions 现在都使用 `libuv-linux-clang`。

旧版存在 Linux Gold 对 macOS graph 比较的问题。修正后：

- profile 不一致的 case 输出 `not_applicable`，不再计为 FN。
- 评测器和 quality gate 都支持 profile-aware 判断。
- source root 支持从工作区向上查找 `third_party/libuv`。
- 验证脚本支持显式 `--source-root`。

### 3.2 同步关系 identity

同步关系按以下 identity 评分：

```text
source.function
target.function
source.file
source.line
target.file
target.line
order
```

`order` 支持：

```text
happens_before
concurrent
unknown
```

### 3.3 正例与负例

libuv 同步数据源包含 32 条源码审查样本：

```text
正例样本：26
负例样本：6
```

负例会额外生成错误方向断言，因此最终评测文件包含：

```text
32 条正例断言
6 条负例断言
```

负例覆盖：

- signal 不能指定具体 worker。
- relaxed atomic 不能建立发布。
- 两个 reader 不能建立方向性 HB。
- 失败 trylock 不能建立 HB。

### 3.4 Evidence 校验

- 每条 Gold 和预测都必须有源码文件、行号和 snippet。
- TP 必须命中 required evidence anchors。
- evidence 缺失、行号错误或 snippet 不匹配会失败。
- 当前同步 Gold 的 evidence 命中率为 100%。

## 4. 分析器改进

### 4.1 调用链

新增 `FunctionTargetResolver`：

- 解析 C 结构体函数指针表。
- 追踪跨文件函数指针参数。
- 处理初始化列表和字段偏移。
- 解析 C++ 构造、成员调用和虚函数 override。
- 输出 exact target、candidate target set 或 unknown。

CallGraphBench 当前候选结果：

```text
31 TP / 0 FP / 0 FN
```

external-v1 冻结基线仍保留：

```text
18 TP / 0 FP / 13 FN
```

正式进入 external-v2 前不能覆盖历史冻结结果。

### 4.2 资源流

资源操作升级为结构化 `ResourceEvent`：

```text
acquire / release / transfer / borrow / escape / invalidate
```

每个事件记录：

```text
resource_id
resource_type
expression
function
thread
file/line
condition
owner
evidence_ids
```

重点补充：

- 输出参数资源识别。
- 结构体字段别名传播。
- callback 参数所有权。
- 宏展开后的资源 transfer。
- 跨函数 release。
- 路径 condition 保留。

libuv 资源流从：

```text
11 TP / 0 FP / 6 FN
```

提升到：

```text
17 TP / 0 FP / 0 FN
```

### 4.3 同步关系

同步分析从调用边扩展到源码事件：

```text
acquire / release / wait / notify
enqueue / dequeue
join / atomic read/write
callback / read / write
```

当前规则覆盖：

- queue producer unlock 到 consumer lock。
- condition wait 的释放和重新获取。
- semaphore post/wait。
- thread create/join。
- atomic release/acquire、seq_cst、relaxed unknown。
- rwlock writer/reader、reader/reader 和失败 trylock。
- eventfd write/read、poll 和 callback。
- 锁作用域内的受保护写。

关键修复 `sync_006`：

```c
uv_mutex_lock(&mutex);
if (is_slow_work)
  slow_io_work_running--;
```

从：

```text
4 TP / 0 FP / 28 FN / 6 TN
Recall 12.50%
```

提升到：

```text
32 TP / 0 FP / 0 FN / 6 TN
Precision / Recall / F1 = 100%
Evidence = 100%
```

### 4.4 Notification 与 callback witness

旧逻辑会把 notification 和 callback 做笛卡尔式配对：

```text
每个 notification × 每个 callback
```

即使两端没有共享 condition、queue、event 或 async 流程，也可能生成关系。

现在只允许：

- producer 和 consumer 已存在队列发布关系，callback 位于 consumer 函数。
- atomic exchange 和 async callback 位于同一文件的 async 流程。
- 没有共享资源 witness 的组合直接不输出。

同时修复：

- 跳过函数定义行，避免把函数签名当成调用。
- 跳过 `uv__io_s::cb` 等 callback 伪函数。
- relaxed atomic 到 callback 只允许同一 async 文件的路径。

修复效果：

```text
通用 notification/callback 配对：355 条 -> 0 条
同步预测总数：3,461 条 -> 2,982 条
```

### 4.5 P1 算法与评测能力补强

本阶段还完成了以下 P1 改动：

- CFG/路径条件：同一函数关系区分直线顺序、互斥分支和循环回边；无法证明时输出 unknown。
- Wrapper summary：对仅封装一个已知同步 API 的函数推导 acquire、release、wait、notify 语义。
- 局部 alias：传播简单指针和字段赋值，`local = &global_mutex` 可和 `global_mutex` 配对。
- Thread identity：从 `uv_thread_create`、`pthread_create` 推导 callback 所属 thread handle，并沿直接调用传播。
- External-v2：CallGraphBench 当前生成器独立冻结为 31 TP / 0 FP / 0 FN。
- 资源 lifecycle state：输出 released、transferred、possibly_owned 和 unknown。
- 多调用展开：同一行 `free(a); free(b);` 会拆成两个释放事件。
- 指针算术归一化：`malloc(buffer)` 与 `free(buffer - 1)` 可归到同一资源。
- Redis v2 和外部基准纳入统一回归门禁。

## 5. Closed-world 审计

首批范围是四个文件：

```text
third_party/libuv/src/threadpool.c
third_party/libuv/src/unix/async.c
third_party/libuv/src/unix/thread.c
third_party/libuv/src/unix/linux.c
```

选择规则：

> source endpoint 位于上述四个文件的所有同步预测。

审计前：

```text
1,554 条预测
1,370 accepted
184 rejected
源码规则通过率 88.16%
```

主要问题：

- 177 条无共享资源 witness 的 notification/callback 配对。
- 3 条 endpoint evidence 不一致。
- 3 条跨文件 relaxed atomic 到 callback。
- 1 条 event-loop 方向错误。

完成修复后：

```text
1,277 条预测
1,277 accepted
0 rejected
0 needs_review
源码规则通过率 100%
```

审计结果位于：

```text
validation/results/libuv/sync-closed-world-v1/audit.json
validation/results/libuv/sync-closed-world-v1/report.md
validation/results/libuv/sync-closed-world-v1/review.csv
validation/results/libuv/sync-closed-world-v1/selection.json
validation/results/libuv/sync-closed-world-v1/universe.json
```

候选 universe 共 `530` 条，当前覆盖 `530` 条，`universe_recall=100%`。
这仍然是机器源码规则审计，不是新增人工确认 Gold。

## 6. 外部与跨仓验证

### 6.1 调用链

| 数据集 | TP | FP | FN | Precision | Recall | 说明 |
|---|---:|---:|---:|---:|---:|---|
| CallGraphBench v1 | 18 | 0 | 13 | 100% | 58.06% | 历史冻结基线 |
| CallGraphBench v2 | 31 | 0 | 0 | 100% | 100% | FunctionTargetResolver v2 |
| CG-Bench | 411 | 197 | 117 | 67.60% | 77.84% | unknown callsite 41，coverage 86.42% |

### 6.2 资源流

| 数据集 | TP | FP | FN | TN | Precision | Recall | 说明 |
|---|---:|---:|---:|---:|---:|---:|---|
| Juliet CWE-401 v2 | 5 | 0 | 0 | 23 | 100% | 100% | lifecycle-state 未回退 |
| SV-COMP MemSafety v2 | 10 | 7 | 0 | 3 | 58.82% | 100% | FP 从 8 降到 7 |

### 6.3 同步/并发

| 数据集 | TP | FP | FN | TN | Unknown | Precision | Recall | Coverage |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| DataRaceBench | 8 | 0 | 0 | 10 | 6 | 100% | 100% | 75% |
| SV-COMP Concurrency | 10 | 0 | 0 | 10 | 0 | 100% | 100% | 100% |

### 6.4 Redis target v2

| 类别 | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 调用链 | 108 | 0 | 2 | 10 | 100% | 98.18% | 99.08% |
| 资源流 | 26 | 0 | 1 | 0 | 100% | 96.30% | 98.11% |
| 同步关系 | 9 | 0 | 13 | 3 | 100% | 40.91% | 58.06% |

Redis target v2 的源证据已验证，但资源人工复核仍为 pending。同步 Recall 偏低是跨仓泛化的主要风险。

## 7. 已推送提交

### `ff11180 Expand native analyzers and closed-world validation`

主要内容：

- 增加 `FunctionTargetResolver`。
- 改进资源流生命周期传播。
- 修复 Linux-profile 同步关系。
- 增加四文件 closed-world 审计。
- 增加 Redis target v2 Gold 和预测结果。
- 更新质量门禁和汇报材料。

### `84c0721 Normalize AST fallback test root path`

修复远端新增测试在 macOS 下 `/var` 与 `/private/var` 规范化不一致的问题。

### `74def0f Require witnesses for notification callback relations`

主要内容：

- 删除无 witness 的 notification/callback 笛卡尔配对。
- 保留有队列 witness 的 notification/callback unknown 边界。
- 保留同一 async 流程中的原子通知 callback 边界。
- 过滤函数定义行和 callback 伪函数。
- 更新 Linux-profile predictions、quality gate 和 closed-world 审计。
- 更新相关文档和汇报稿中的指标。

## 8. 当前验证状态

已执行：

- 完整 Agent/分析器测试：115 项通过，2 项 Linux Docker 测试跳过。
- 验证器测试：50 项通过。
- strict quality gate：通过。
- closed-world 审计重复生成：结果一致，530 条候选 universe 全部覆盖。
- external-v1 artifacts lock、external-v2 protocol lock、外部回归检查：通过。
- libuv strict gate 和 Redis v2 gate：通过。
- `git diff --check`：通过。

当前验证边界：

- libuv Gold 是全量冻结回归，但不是完整仓库真值。
- 四文件审计覆盖全部预测，但还未穷举四个文件中所有可能成立的同步关系。
- 仓库内仍有 2,950 条同步预测没有完整人工真值。
- Redis v2 资源 Gold 人工复核 pending。
- 两项 Linux Docker 集成测试尚未在当前机器执行。

## 9. 下一步

1. 扩大独立同步 Gold，覆盖更多函数和跨文件场景。
2. 将 closed-world 范围从四个文件扩展到更多 libuv 模块。
3. 继续优化跨函数队列 identity、复杂字段 alias 和 dynamic registration。
4. 为 CG-Bench、Juliet 和 race datasets 建立各自 external-v2。
5. 补完 Redis target v2 资源 Gold 人工复核。
6. 在 Linux 目标环境执行 Docker 沙箱和真实对象存储验收。

## 10. 结论

当前项目已经形成可复现的调用链、资源流和同步关系验证闭环。libuv Linux-profile 的三类冻结 Gold 在已标注范围内全部通过，同步关系进一步完成了四文件 closed-world 预测审计，并清除了无 witness 的 notification/callback 误配对。

后续重点不再是继续修改已有 38 条同步 Gold，而是扩大跨仓独立 Gold、完成其余 external-v2 数据集，并验证更复杂别名和 interprocedural ownership。
