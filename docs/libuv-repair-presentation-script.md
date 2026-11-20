# Libuv 调用链与资源流改进汇报讲稿

建议时长 8 到 10 分钟。每页先讲结论，再补一到两个源码或数字依据，不要逐行念表格。

## 第 1 页：封面

各位老师好，这次汇报的主题是 libuv 调用链与资源流改进。

本次汇报围绕三条主线：资源流从 11 TP / 6 FN 修到 17 TP / 0 FN；CallGraphBench 当前候选从 13 条漏边修到 31 TP / 0 FN；同步关系在真实 Linux profile 下从 4 TP / 28 FN 修到 32 TP / 0 FN。最后汇报 Redis target v2 上的迁移结果和仍然存在的开放世界评测边界。

## 第 2 页：Libuv 资源流 6 个缺口

libuv 资源流的 6 个 FN 可以归成四类。

第一，`uv__make_pipe(pipefd)` 通过输出参数创建两个 fd，但分析器原来只看函数返回值，没有把 `pipefd[0]` 和 `pipefd[1]` 识别成资源。后面 `pipefd[1]` 保存到 `loop->async_wfd`，分析器也没有建立字段别名，所以 async_acquire 和 async_transfer 都漏了。

第二，`POST` 是宏，展开后实际会调用 `uv__work_submit`，发生跨线程任务提交。原来只看到宏名，没有看到资源所有权转移，所以 fs_transfer 漏检。

第三，`req->cb(req, req->retcode, req->addrinfo)` 把 addrinfo 交给 callback，后续由用户调用 `uv_freeaddrinfo`。如果分析器不继续追踪 callback 参数，就无法把 acquire 和 release 接起来。

第四，`req->ptr = dents` 把 scandir 返回的目录项数组保存到结构体字段，最后在 `uv__fs_scandir_cleanup` 中通过 `req->ptr` 释放。原来局部变量 `dents` 出了函数以后，资源身份就丢了。

因此，这 6 个 FN 不是验证集写错，而是分析器缺少资源实体、别名、所有权和路径传播。

## 第 3 页：资源流改进方案

这一页讲资源流具体怎么改。核心思路是：不再靠函数名白名单判断 acquire 和 release，而是把每一次资源操作变成一条结构化 ResourceEvent。

ResourceEvent 一共有六类：

`acquire` 表示创建或获得资源，例如 malloc、open、pipe、scandir。

`release` 表示释放资源，例如 free、close、freeaddrinfo。

`transfer` 表示资源所有权交给另一个函数、对象或线程。比如 `dents` 保存到 `req->ptr`，或者请求被提交到线程池。

`borrow` 表示只临时使用资源，但不获得释放责任。

`escape` 表示资源引用离开当前作用域，例如保存到结构体字段，或者作为 callback 参数传出去。

`invalidate` 表示指针或句柄失效，例如 free 后置空，或者 realloc 以后旧指针不能继续使用。

每个事件还要保存九个字段。可以分成四组理解：

第一组是资源身份，包括 `resource_id` 和 `resource_type`。`resource_id` 解决“这些不同表达式是不是同一个资源”，`resource_type` 说明它是 memory、file descriptor、addrinfo 还是目录项数组。

第二组是源码位置，包括 `expression`、`function`、`thread`、`file/line`。`expression` 是这个资源在当前代码点的名字，例如 pipefd[1]、loop->async_wfd、dents 或 req->ptr；function 和 file/line 说明事件发生在哪里；thread 说明它处于 event loop、worker 还是其他线程角色。

第三组是语义条件，包括 `condition` 和 `owner`。condition 说明事件在什么分支、平台或返回条件下成立；owner 说明当前谁负责这个资源，是局部变量、事件循环、request 对象还是 callback。

第四组是 `evidence_ids`，它指向真实的源码证据，包括文件、行号和 snippet，保证结论能够人工复核。

实施分四步。

第一步是资源实体识别。把 `uv__make_pipe(pipefd)` 写入的 `pipefd[0]` 和 `pipefd[1]` 提升为 fd 资源；把 `scandir(..., &dents, ...)` 返回的目录项数组提升为 memory 资源；把 `req->addrinfo` 和 `req->ptr` 对应的对象也纳入资源图谱。这里要特别处理输出参数，因为资源不是通过 return 返回，而是通过指针写回调用者变量。

第二步是别名和字段传播。`pipefd[1]` 后面会保存到 `loop->async_wfd`，`dents` 会保存到 `req->ptr`，`req->addrinfo` 会作为 callback 参数传出去。这些位置的变量名和作用域都变了，但底层资源没有变。分析器要给它们保留同一个 resource_id，并记录 owner 从局部变量转移到结构体字段或 callback。

第三步是宏和 transfer 识别。源码里看到的是 `POST`，但宏展开后实际是 `uv__req_register` 和 `uv__work_submit`。这说明请求已经提交到线程池，资源生命周期从当前函数转移到了异步工作和完成回调。分析器必须结合宏展开 JSON 和源码行，不能只识别原始宏名。

第四步是路径选择。`uv__async_start` 可能走不同平台分支，`uv__fs_scandir_cleanup` 也可能有多个 release 分支。分析器不能只保留第一个 acquire 或最后一个 release，而要按 condition 保留每条资源路径。否则源码里有两个正确分支，模型却只看到其中一条。

举个完整例子：在 uv__async_start 中，`uv__make_pipe` 成功返回后产生 `pipefd[1]`，这是 acquire；随后 `loop->async_wfd = pipefd[1]` 建立字段别名和所有权转移；最后在 `uv__async_close` 中看到 `uv__close(loop->async_wfd)`，这是 release。这三条事件虽然 function、expression 都不同，但通过同一个 resource_id 可以连成完整生命周期。

最终结果是：libuv resource_flow 从 11 TP / 0 FP / 6 FN 提升到 17 TP / 0 FP / 0 FN，Recall、F1 和 required evidence 命中率均为 100%。这组指标说明冻结 Gold 中的资源操作链已经完整，而不是说整个仓库所有未标注资源事实都一定没有错误。

## 第 4 页：调用链漏边与改进方案

这一页解决的是调用链里的间接调用问题。CallGraphBench 的冻结基线是 18 TP、0 FP、13 FN，Recall 58.06%。13 条漏边集中在四类：C 结构体函数指针表、跨文件 callback 注册、C++ 构造函数和成员函数、C++ 虚函数分派。

为什么直接调用图不够？因为在源码里，分析器经常只能看到：

```c
handler(value);
driver->open(path);
rule.apply(value);
```

这里 Clang 知道发生了一次调用，也能知道调用的静态类型，但它不一定知道运行时到底是哪个函数。原来的分析器可能只记录了 `handler` 这个表达式，却没有把调用者传入的具体函数、结构体初始化里的函数地址或 C++ override 目标接上来，所以会形成漏边。

FunctionTargetResolver 可以理解成一个“间接调用目标解析层”。它不是简单地看到函数名就猜测，而是分五步解析：

第一步，定位 callsite 和被调用的表达式。比如调用点是 `handler(value)`，表达式是 `handler`，静态类型是 `void (*)(int)`。

第二步，确定这个表达式是什么来源。它可能是局部函数指针、函数参数、结构体字段、全局数组、结构体数组，或者 C++ 对象的成员函数。

第三步，查找赋值和注册点。对 C 代码，要检查局部赋值、`InitListExpr` 初始化列表、跨文件 callback 参数和函数返回值；对结构体表，要解析字段顺序、字段偏移和数组索引；对跨函数调用，要把实际传入的函数指针从 caller 传到 callee，再传到真正的调用点。

第四步，做候选约束。找到的目标必须通过函数签名兼容性检查；结构体实例、数组索引和调用上下文要一致；不能因为一个结构体里有很多函数指针，就把整张表的所有函数都塞进结果。

第五步，产生 target set。只有一个明确目标时，输出 exact target；有多个可能目标时，保留 candidate set；证据不足时输出 unknown。这里的 target set 是“可能目标集合”，不是要求运行时只能调用一个函数。

举个 C 的例子：

```c
static Driver tcp_driver = {"tcp", tcp_open, tcp_read};
driver->open(path);
```

Resolver 要把初始化列表的字段顺序和结构体定义对齐，知道第二个字段是 `open`，所以 `driver->open(path)` 的目标集合里应该有 `tcp_open`。如果 `driver` 还可能指向另一个 `udp_driver`，目标集合就可以同时包含 `tcp_open` 和 `udp_open`，不能随便只选一个。

C++ 场景则要额外使用类的继承关系和 override 信息。比如 `rule.apply(value)`，如果 `rule` 的静态类型是 `Rule`，但实际派生类可能是 `LengthRule` 或 `PrefixRule`，目标集合就应该是两个 override 函数。

还有一个很重要的适配问题。分析器内部可能保留：

```text
process_device -> callback -> tcp_open
```

但 CallGraphBench 的评价单位是：

```text
process_device -> tcp_open
```

因此评分适配层要把 callback 中间节点折叠成 caller 到 target 的边，同时保留注册点、触发点和源码 evidence。折叠不是丢掉中间事实，而是为了和外部的评分单位对齐。

当前候选重跑已经达到 31 TP、0 FP、0 FN，Precision、Recall、F1 都是 100%。但这是当前候选结果，external-v1 的 18/0/13 仍然是冻结历史基线，正式发布前需要进入 external-v2 重新冻结。

## 第 5 页：两个调用图外部验证集

两个外部集的分工不同。

CallGraphBench 评价 caller 到 callee 的调用边，主要检查调用图边是否完整。它适合判断函数指针、callback、C++ 分派有没有被恢复成正确的边。

CG-Bench 评价 callsite 到 target set，检查一个间接调用点到底可能指向哪些函数。当前 Precision 67.60%、Recall 77.84%，还有 41 个 unknown callsite、197 个 FP 和 117 个 FN。

CG-Bench 的两个主要问题：候选集合过宽会产生 FP，例如把整张函数表都当成目标集合；候选集合过窄会产生 FN，例如只看到一个 callback 而漏掉其他注册路径。

后续改进是候选生成、候选约束和分层输出：exact target 只有一个明确目标；candidate set 保留多个可能目标；evidence 不足时输出 unknown。同时按 callback_parameter_flow、struct/table dispatch、cast、dynamic registration 和 virtual dispatch 分别报告指标。

## 第 6 页：Redis 迁移结果

Redis target v2 用来验证这套能力能否迁移到新的目标仓库。

调用链结果是 108 TP、0 FP、2 FN、10 TN，Precision 100%、Recall 98.18%、F1 99.08%。剩余两条是 sigemptyset 和 sigaddset，其中 sigaddset 被解析成外部符号 __sigbits。

资源流结果是 26 TP、0 FP、1 FN，Precision 100%、Recall 96.30%、F1 98.11%。唯一漏项是 processInlineBuffer 到 sdssplitargs，位置在 networking.c 2160。

target v2 一共 172 条命题，包括 120 条调用链、27 条资源事实和 25 条同步断言。source check 已验证 10 个源码文件和 172 条证据，但资源事实的人工复核仍然是 pending。这里不能说成已经完全人工确认的 Gold。

## 第 7 页：同步关系 profile 与改进

同步验证集这次先修的是评测口径。数据集本身是 32 条 `libuv-linux-clang` 样本，并用 Ubuntu 24.04、Clang 18 重新构建 libuv，生成了同 profile 的 graph 和 sync predictions。profile 和源码根目录问题都已经修复，所以下面的结果不再是 macOS graph 对 Linux Gold 的跨 profile 比较。

修复前的真实基线是 4 TP、0 FP、28 FN、6 TN，Precision 100%，Recall 12.50%，F1 22.22%。这里的 28 条都是 Linux profile 下的真实漏检，不是 Gold 错误，也不是跨 profile 造成的分母污染。修复后的结果是 32 TP、0 FP、0 FN、6 TN，Precision、Recall、F1 和 required evidence 命中率均为 100%。

同步关系为什么难？因为分析器不能只看到 `lock`、`unlock`、`signal` 或 `atomic` 这个单词就建立一条关系。它必须同时知道五类信息：

第一，是什么同步事件。是 mutex、condition variable、atomic、thread join、poll、eventfd 还是 callback。还要知道操作是否成功，例如 trylock 成功和失败的含义完全不同。

第二，是哪两个线程角色。是 producer 到 worker，main 到 worker，还是同一个线程内部的程序顺序。没有线程身份，就无法判断是跨线程同步还是单线程顺序。

第三，是不是同一个资源实例。两端必须是同一把 mutex、同一个 condition、同一个 queue item、同一个 atomic 或同一个共享对象。只看名字相同不够。

第四，关系成立的条件是什么。包括 held_locks、读取到了哪个 atomic 值、是否属于同一批次、是否等到所有 worker 完成、是否成功获得锁。比如 signal 只表示发出了通知，它不能指定具体哪个 worker 消费。

第五，最后输出哪种语义。是能够证明的 happens-before，还是允许并发的 concurrent，还是证据不足的 unknown。三者不能混写。

本轮已经按机制实现以下规则。

队列发布方面，生产者在同一队列的 mutex 内完成入队，然后 unlock；消费者之后 acquire 同一把 mutex 并出队，这时才建立 HB。不能只看 producer 调用了 post、worker 调用了 signal，就推出某一次 job 被执行。

程序顺序方面，新增源码级事件扫描，不再只看函数调用边。最后一轮漏检 `sync_006` 的根因是：分析器已经找到了 `threadpool.c:134` 的 `uv_mutex_lock` 和 `:137` 的 `slow_io_work_running--`，但中间夹着 `if (is_slow_work)`，旧的“每类只取第一个后继”规则把真正的临界区写操作截断了。现在会识别锁获取后、释放前的首个受保护写操作，把 `134 -> 137` 单独建边，并保留两端 evidence。

条件变量方面，必须建模 `pthread_cond_wait` 会在等待时释放 mutex，返回后重新获得 mutex；还可能有虚假唤醒，所以需要重新检查 while 条件。signal 不能指定具体 worker，因此“某次 signal 对应某次 job 执行”这种命题必须有条件，条件不足时输出 unknown。

thread join 方面，只有在成功 join 之后，才能把被 join 线程完成前的写入与后续 destroy 或继续执行建立 HB。启动阶段的 semaphore post/wait 只能证明初始化握手，不能用来证明用户 work 已经完成。

atomic 方面，默认 seq_cst 的 exchange/load 在观察到发送值之后可以建立 release/acquire 关系；relaxed load 本身不能发布任意用户内存，多次 send 合并成一次 callback 时也不能做一一配对，这两类都应输出 unknown。atomic busy/pending 和 spin/cleanup 则要按普通语句和原子操作的事件顺序处理。

rwlock 方面，writer unlock 和之后成功获取 read lock 或 write lock 的线程可以建立 HB；两个 reader 持有共享读锁时是 concurrent，不能强行推出谁先谁后。失败的 trylock 没有获得锁，不能建立 HB，应该输出 unknown。

还要单独说明 eventfd 和轮询场景。`write -> read/poll -> callback` 可以建立事件因果链，但它不能自动证明发送者的任意用户内存都完成了 C11 发布，除非还有对应的 atomic 或锁证据。

改进后的验收不只看 Recall 是否上升，还要检查四件事：每个漏检是否都能映射到一个具体机制；每条关系是否有线程身份、资源 identity、condition 和 evidence；新增规则是否引入新的 FP；unknown 和 concurrent 是否仍然被保留。不能在看到 Gold 后把 unknown 改成 HB 来提高分数。

因此 100% 的含义是：32 条冻结正例和 6 条负例全部匹配，而且 32 条 TP 的 required evidence 全部命中。它不表示仓库内另外 2,955 条未标注同步输出已经得到全仓真值。四文件 closed-world 审计目前覆盖 1,282 条输出，全部通过源码规则检查；下一步是继续扩大独立 Gold 和 closed-world scope。

## 第 8 页：总结与下一步

总结一下。

调用链方面，FunctionTargetResolver 已经把 CallGraphBench 的 13 个 FN 收口到当前候选 31/0/0，下一步是进入 external-v2 冻结。

资源流方面，ResourceEvent 和四步传播方案已经把 libuv 的 6 个 FN 收到 0，当前结果为 17 TP / 0 FP / 0 FN，required evidence 命中率 100%。

同步关系方面，已经完成 Linux profile 对齐和真实 FN 修复；结果从 4 TP / 28 FN 提升到 32 TP / 0 FN，required evidence 命中率 100%。四文件 closed-world 审计覆盖 1,282 条预测，源码规则通过率 100%。下一步是扩大独立 Gold 和跨仓同步案例。

Redis target v2 证明调用链和资源流能力可以迁移，但资源 Gold 仍需人工确认。最后所有分析器变化进入 external-v2，不覆盖已经冻结的 external-v1。

## 可能的追问

### 为什么 CallGraphBench 当前是 31/0/0，但还说 13 FN？

13 FN 是 external-v1 冻结基线。FunctionTargetResolver 之后当前候选重跑达到 31/0/0，但还没有进入 external-v2 重新冻结，所以两个状态要分开说。

### 为什么资源流只有 6 个 FN，还要引入 ResourceEvent？

6 个 FN 不是六个孤立错误，而是同一个底层问题：资源身份无法跨输出参数、字段、宏、callback 和队列保留。ResourceEvent 是统一修复这一类问题。

### 为什么同步 Recall 从 12.50% 提升到了 100%？

28 个 FN 逐个对应到了队列发布、源码程序顺序、条件变量、thread join、atomic、rwlock、eventfd 和 unknown 边界。最后一轮修复的是控制流中间的写操作：锁和写都识别到了，但 `if` 条件占用了唯一的后继位置，真正的 `134 -> 137` 没有生成。现在加入锁作用域内的受保护写规则后，冻结 Gold 已全部命中；但未标注输出仍有全仓真值缺口，不能把 100% 外推成全仓准确率。

### unknown 和 FN 有什么区别？

unknown 是系统没有给出二元结论，单独降低 coverage；FN 是 Gold 明确存在，但分析器没有找到。两者不能混用。
