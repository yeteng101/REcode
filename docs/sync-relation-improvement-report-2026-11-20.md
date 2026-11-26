# 同步关系分析器改进汇报文稿

建议时长：8 到 10 分钟。

## 一、开场

近期同步关系的改进不是直接把 Gold 改到通过，也不是单纯调整几个函数白名单。工作分为三步：

1. 先修正验证集口径，确认真实 FN。
2. 根据真实 FN 补同步事件、资源身份、线程上下文和 witness 配对。
3. 用四文件 closed-world 审计检查 Gold 之外是否仍在生成错误关系。

最终 libuv 冻结同步 Gold 达到：

```text
32 TP / 0 FP / 0 FN / 6 TN
Precision = 100%
Recall = 100%
F1 = 100%
Evidence = 100%
```

但 100% 只代表冻结 Gold 范围全部通过，不代表整个 libuv 仓库和所有跨仓库场景都已经完全正确。

## 二、验证集原来的问题

第一，最早存在跨 profile 比较。同步 Gold 使用 `libuv-linux-clang`，但预测图一度来自 macOS。后来固定 libuv commit `8fb9cb91...`，使用 Ubuntu 24.04 和 Clang 18 重新生成 graph 和 predictions。profile 不匹配的 case 不再算 FN，而是标记为 `not_applicable`。

第二，profile 修正以后，仍然只剩 `4 TP / 28 FN`。这说明 28 个漏检不是 Gold 写错，而是分析器真实缺少能力。当时的有效基线是：

```text
4 TP / 0 FP / 28 FN / 6 TN
Precision 100%
Recall 12.50%
F1 22.22%
```

第三，原来的评分 identity 不够明确。现在同步关系的 identity 固定为：

```text
源函数 + 源文件 + 源行号
目标函数 + 目标文件 + 目标行号
order
```

`order` 可以是 `happens_before`、`concurrent` 或 `unknown`。unknown 不是“没有结论”，而是一个正式答案。

第四，验证集必须同时有正例和负例。libuv 同步数据集中有 32 条源码审查样本：

```text
正例样本：26
负例样本：6
```

其中 6 条 negative 样本会额外生成“错误方向断言”，用来检查分析器是否会过度判断。因此最终评测文件是 32 条正确顺序断言加 6 条拒绝断言，共 38 个 case。

负例主要检查：

- condition signal 是否被错误配到某个具体 worker
- relaxed atomic 是否被错误当成发布
- reader 之间是否被错误建立方向
- 失败 trylock 是否被错误当成获得锁

第五，原来的 open-world 评分只检查 Gold，未标注预测不进入 Precision 分母。这样即使仓库里有很多误报，冻结 Gold 仍可能显示 100%。因此又增加了四文件 closed-world 审计。

## 三、分析器怎么改

原来的同步分析主要依赖调用图中的 lock、unlock、wait、signal 等边，问题有三个：

- 只能看到函数调用，看不到普通读写和队列操作。
- 同一资源、同一线程实例没有完整建模。
- 容易把两个名字相似、但没有真实关联的事件配在一起。

新的分析器分为六层。

### 1. 候选事件提取

除了调用图，还会扫描源码中的普通语句，例如：

```c
uv_mutex_lock(&mutex);
slow_io_work_running--;
uv__queue_insert_tail(&wq, item);
uv_cond_signal(&cond);
```

这样就能看到调用图中没有的受保护写、赋值、入队和 callback。

### 2. 操作语义分类

根据真实 callee、API 契约、参数、返回值、内存序和源码上下文，把操作分类为：

```text
acquire / release / wait / notify
enqueue / dequeue
join / atomic_read / atomic_write
callback / read / write
```

函数名只是候选入口。例如：

```text
uv_mutex_lock -> acquire
uv_mutex_unlock -> release
uv_cond_wait -> wait
uv_cond_signal -> notify
uv__queue_insert_tail -> enqueue
uv_thread_join -> join
```

这些函数名必须匹配真实调用目标，并遵守对应 API 的语义契约，不能因为名称里出现 `lock` 就直接判断存在同步关系。

### 3. 建立上下文和资源身份

每个事件记录：

```text
function、thread、file、line
mutex、condition、queue、atomic、fd、thread handle
held_locks、condition、resource
evidence_ids
```

这一层解决“两端是不是同一个对象”。只看函数名相同不够，必须确认是同一把 mutex、同一个 condition、同一个 queue、同一个 atomic 或同一个 fd。

### 4. 建立 witness

witness 是两端可以配对的证据。例如队列发布需要：

```text
同一个 queue
同一把 mutex
producer 先 enqueue
producer 后 unlock
consumer 后 lock
consumer 后 dequeue
两端都有源码 evidence
```

condition variable 需要同一 condition、同一 mutex 和 predicate 上下文。atomic 需要同一 atomic 对象和正确的内存序。thread join 需要同一 thread handle。

没有 witness 时，不能把 notification 和 callback 任意组合。

### 5. 分机制建立规则

队列：producer unlock 到 consumer lock 建立 HB。

条件变量：wait 会释放并在返回后重新获取 mutex，signal 不指定具体 worker，唤醒后必须重新检查 predicate。

线程：只有成功 join 后，才能建立被 join 线程完成到后续操作的顺序。

atomic：release/acquire 和 seq_cst 可以建立发布；relaxed 不能发布任意用户内存，应输出 unknown。

rwlock：writer release 到后续 reader/writer acquire 可以建立 HB；两个 reader 允许 concurrent；失败 trylock 输出 unknown。

eventfd：`write -> read/poll -> callback` 可以建立事件因果链，但不能自动证明任意用户内存完成 C++/C11 发布。

锁作用域：锁获取后、释放前的首个受保护写，可以和锁获取建立程序顺序。

### 6. 三值输出和 evidence

最终输出三种顺序：

```text
happens_before
concurrent
unknown
```

不能为了 Recall 把所有未知场景强行改成 HB。每条 relation 都必须附带函数、文件、行号、snippet 和 evidence_ids。

### 7. P1 路径和身份补强

本轮进一步增加：

- CFG 路径判断：互斥分支和循环回边不再默认生成 HB。
- Wrapper summary：自定义 lock/unlock/wait/notify wrapper 可继承内部 API 语义。
- 局部 alias：简单指针和字段别名可以归一到同一资源。
- Thread identity：从 thread create 的 callback 推导实际 thread handle，并沿直接调用传播。
- 候选 universe：四文件生成 530 条机制候选，不再只评价分析器已经输出的预测。

## 四、两个关键例子

### 例子一：sync_006

源码是：

```c
uv_mutex_lock(&mutex);
if (is_slow_work)
  slow_io_work_running--;
```

分析器已经识别到：

```text
threadpool.c:134 uv_mutex_lock
threadpool.c:137 slow_io_work_running--
```

但中间夹着 `if`，旧规则每类只取第一个后继，所以真正的写操作被截断。新规则增加“锁获取到释放之间的首个受保护写”，最终生成：

```text
134 mutex_lock -> 137 protected write
```

### 例子二：notification callback 误配对

旧逻辑会把 notification 和 callback 做笛卡尔式组合。例如 10 个 notification 和 20 个 callback 会生成 200 条关系，即使两端没有共享资源。

现在只允许两种情况：

- producer 和 consumer 已有队列发布关系，并且 callback 位于 consumer 函数。
- atomic exchange 和 async callback 位于同一文件的 async 流程。

其他没有 witness 的 notification callback 组合直接不输出。

修复后：

```text
通用 notification callback 配对：355 条 -> 0 条
同步预测总数：3,461 条 -> 2,982 条
```

## 五、结果

### libuv 冻结同步 Gold

| 指标 | 结果 |
|---|---:|
| Gold 正例 | 32 |
| Gold 负例 | 6 |
| TP | 32 |
| FP | 0 |
| FN | 0 |
| TN | 6 |
| Precision | 100% |
| Recall | 100% |
| F1 | 100% |
| Accuracy | 100% |
| Evidence | 100% |

### 四文件 closed-world 审计

范围是：

```text
threadpool.c
unix/async.c
unix/thread.c
unix/linux.c
```

修复 notification witness 之前：

```text
1,554 条预测
1,370 accepted
184 rejected
源码规则通过率 88.16%
```

修复 notification witness 之后：

```text
1,277 条预测
1,277 accepted
0 rejected
0 needs_review
源码规则通过率 100%
```

候选 universe：

```text
530 条机制候选
530 条 covered
0 条 missing
universe_recall = 100%
```

这里的 100% 是机器源码规则审计，不是新增人工确认 Gold 后的最终 Precision。

### 其他同步相关验证集

| 验证集 | Precision | Recall | Coverage | 说明 |
|---|---:|---:|---:|---|
| DataRaceBench | 100% | 100% | 75% | 6 个 unknown 被保留 |
| SV-COMP C.Concurrency | 100% | 100% | 100% | 当前固定子集 |
| Redis target v2 同步迁移 | 100% | 40.91% | 40.91% | 跨仓 Recall 仍是明显短板 |

这些数据集不能和 libuv Gold 合成一个总分，因为评价单位不同。

## 六、当前局限

第一，libuv 同步 Gold 只有 38 个评测 case，适合回归，不足以代表完整仓库。

第二，四文件审计只覆盖预测侧，尚未穷举四个文件中所有可能成立的同步关系，因此完整 closed-world Recall 还不能计算。

第三，仓库中仍有 2,950 条同步预测没有完整人工真值。

第四，用户自定义 wrapper、函数指针、复杂别名和跨函数资源实例仍需要更强的程序分析。

第五，Redis 同步 Recall 只有 40.91%，说明能力迁移到其他仓库仍不充分。

## 七、下一步

第一，扩大独立同步 Gold，减少对同一 graph 和历史标注的依赖。

第二，把四文件审计扩展到更多函数范围，并逐步升级为人工确认 Gold。

第三，增加 wrapper summary、字段别名、跨函数队列身份和线程实例分析。

第四，继续完善 eventfd、poll、callback 合并和 dynamic registration。

第五，以 DataRaceBench、SV-COMP Concurrency、Redis 等结果跟踪跨仓泛化，不只关注 libuv 回归分数。

## 八、可直接使用的总结

> 同步关系这部分，我先修正 profile、identity、负例和 evidence 规则，确认 Linux profile 下确实有 28 个真实 FN。随后把分析器从调用边扩展到源码事件，并加入资源身份、线程上下文、held locks、condition 和 witness。针对队列、condition variable、atomic、rwlock、thread join 和 eventfd 分别建立规则，同时保留 happens-before、concurrent、unknown 三值语义。最终 libuv 冻结 Gold 达到 32 TP、0 FP、0 FN、6 TN，Precision、Recall、F1 和 evidence 都是 100%。进一步做四文件 closed-world 审计后，1,277 条预测全部通过源码规则检查，530 条候选 universe 全部覆盖。但跨仓库上 Redis 同步 Recall 仍只有 40.91%，所以下一步重点是独立 Gold、跨仓迁移和更完整的函数间资源身份传播。

## 九、200 字左右短版

> 近期同步关系工作先修正了 libuv Gold 的 profile、identity、负例和 evidence 口径。Linux profile 对齐后确认仍有 28 个真实漏检，说明问题在分析器而不是 Gold。随后分析器增加源码级同步事件、资源身份、线程上下文、held locks、condition 和 witness，对队列、condition variable、atomic、rwlock、thread join、eventfd 分别建模，并输出 happens-before、concurrent、unknown 三值结果。最终冻结 Gold 达到 32 TP、0 FP、0 FN、6 TN，Precision、Recall、F1 和 evidence 均为 100%。四文件 closed-world 审计覆盖 1,277 条预测，530 条候选 universe 全部覆盖。但 Redis 同步 Recall 仍为 40.91%，下一步需要扩大独立 Gold 和跨仓库验证。

## 十、可能的追问

### 为什么 100% 还不能说明全仓正确？

冻结 Gold 只评价 32 条正例和 6 条负例。仓库里另有 2,950 条同步预测没有完整人工真值，它们不进入 Precision 分母。

### 为什么保留 unknown？

unknown 表示源码事实不足，不能证明 HB，也不能证明不存在关系。把 unknown 强行改成 HB 会提高表面 Recall，但会产生错误同步结论。

### witness 和 confidence 有什么区别？

confidence 是数值。witness 是可以解释配对关系的源码事实，例如同一 queue、同一 mutex、unlock 后 lock，以及两端 evidence。

### 为什么 Redis 迁移 Recall 低？

Redis 的同步原语、宏、wrapper、队列和 callback 写法与 libuv 不同。当前规则对 libuv 的四文件覆盖较好，但跨仓库仍缺少 wrapper summary、别名传播和更通用的资源 identity。
