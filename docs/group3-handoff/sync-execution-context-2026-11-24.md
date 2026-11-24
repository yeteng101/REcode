# REcode 调用方执行上下文

- 基线 `sync-client-object-binding` 的 `100a889`，继承本地 `group3` 的 `e52eedd`。
- 工作分支 `sync-execution-context`。
- 状态 执行来源与条件窗口实现及静态验收完成，2026-11-24。

## 本轮范围

在已有同对象条件关联上附上两边的执行来源。用编译器的直接调用定义及可核查的形参转发，把线程入口追溯到 POSIX / C11 线程创建。支持常见局部函数指针适配，包括没有逃逸且单次写入的局部联合体适配。沿调用方到消费访问的直接调用栈记录另一边路径。 [调用方对象证据](sync-client-object-binding-2026-11-24.md)

仅在识别同一创建者、线程句柄以及受支持的直线创建、消费调用、等待顺序时，导出有条件的线程存活窗口。调用图路径保留可能分支，调度、创建成功、队列来源和实际先后仍需满足对应条件。无法解析的回调或先后顺序保留 unknown。现有语义覆盖、锁集、同步证明和第三组协议保持原有含义。

HPR-048 库端消费者位于 `uv_run` 的直接分发路径。原调用方 `test/test-async.c` 从准备回调启动线程。仓库另有 `test/test-async-null-cb.c`，直接在同一函数中创建线程、调用事件循环并等待线程。两者分别用静态 AST 对照，记录已证实的上下文及缺口。

新增范围位于 `semantic_sync.field_bindings.execution_contexts`，引用已有同对象关联和新源码证据。线程创建和等待最多跟踪 4 层调用，消费者及工作线程使用最多 8 条直接调用边。消费者保留至多 4 条不同的最短函数链。线程窗口要求创建、消费调用、同句柄存储等待在同一调用者的相邻语句中。这个窗口记录源码顺序，实际 `overlap` 仍为 `unknown`。联合体记录实际函数指针适配的位置，目标平台能否按该调用约定执行依赖 ABI。

## 验收

1. 用中性 C 样例验证线程入口、包装参数转发、调用栈和直线存活窗口。加入提前 join、不同线程句柄、未知适配和只有潜在栈路径的对照。
2. 经正式 CLI 验证新 JSON 和证据引用，运行定向和完整基线对照。
3. 用固定 libuv 提交和单一 CMake 编译目标分别静态验证两个仓库调用方。分支独立保留以供审阅。

## 回归结果

- 真实 Clang 样例验证了两层包装下的线程入口、直接消费调用栈和有条件的窗口。提前等待、不同句柄、条件创建及在宏式语句中提前等待均未产生窗口。适配器地址逃逸的样例未产出线程入口绑定。
- 正式 `repo` CLI 导出新契约，所有引用的证据可从同一 `field_bindings.evidence` 解析。
- 45 项定向回归通过。基线 159 项中 156 通过，修改版 161 项中 158 通过。两边均有 2 项 Docker 测试跳过，失败均为已有的 `test_seven_stages_produce_evidence_graph` 的 `report_ready` 断言。

## HPR-048 静态验收

固定 libuv `10902ab616058198067d6c2cc414014d36f86fff` 和 Clang 19.1.7。选用 CMake 的 Unix `uv` 共享库目标，读取 35 个库编译单元，再分别附上仓库两个调用方的编译单元。37 份 AST 均读取成功，验证只进行静态分析。

| 调用方 | 同对象关联 | 执行上下文 | 条件窗口 |
| --- | ---: | ---: | ---: |
| `test/test-async.c` | 3 | 3 | 0 |
| `test/test-async-null-cb.c` | 3 | 3 | 3 |

两份输入均能追溯发送端 `thread_cb`，经 `uv_thread_create`、`uv_thread_create_ex` 到 `pthread_create`。消费端有包含 `uv_run → uv__io_poll → uv__io_cb → uv__async_io` 的直接分发链。另有经 `uv__run_pending` 的调用图路径，其 watcher 分支的实际可达性仍未知。

原调用方的创建在 `prepare_cb` 第 102 行，准备回调的调度和 join 之间的关系仍待证据。另一个调用方第 58–60 行在同一调用者中依次创建线程、运行事件循环和等待同一具名线程句柄存储。三条关联共享这个条件窗口。成功创建、工作线程仍在运行、消费路径到达且取到同一注册对象，都是判断实际重叠需要的条件。

两个场景的 `overlap` 都保留 `unknown`。语义同步均为 `partial`，已有 `shared_objects` 中带已绑定执行上下文的共享对象均为 0。第三组候选和端到端验证本轮未执行。

本机证据位于 `D:/AgentProject/scratch/recode-sync-execution-context-validation`，包括 `hpr048-execution-contexts.json`、`cli-execution-contexts.json` 和回归日志。
