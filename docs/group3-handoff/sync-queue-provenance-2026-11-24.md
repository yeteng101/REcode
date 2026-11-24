# REcode 队列成员来源追踪

- 基线 `sync-cross-function-binding` 的 `1dc879d`，继承本地 `group3` 的 `e52eedd`。
- 工作分支 `sync-queue-provenance`。
- 状态 队列可能来源实现与验收完成，2026-11-24。

## 目标和边界

复用 Clang AST、直接调用解析和字段绑定，追踪 C 侵入式队列的嵌入成员。按函数体的读写结构识别受支持的入队、转移和取首操作。只在队列存储、嵌入成员、容器类型和恢复地址相符时，将取出的字段访问关联到可能的注册来源。具名队列存储分别识别，参数接收者按成员声明给出类型级可能来源，保留实例关系未知。 [直接调用绑定](sync-cross-function-binding-2026-11-24.md)

在原生 `semantic_sync.field_bindings` 保存队列来源及源码证据。记录为可能来源，覆盖受支持的结构和局部语句。执行顺序、具体队首、跨调用实例相等性、线程并发及同步结论需要额外证据。保持第三组接口和已有语义候选的含义。

`queue_provenance.bindings` 的每条记录关联现有字段访问 ID，证据从同一 `field_bindings.evidence` 解析。识别同编译单元中的直接调用和有限的单链、双向侵入式链表结构。容器恢复核对 Clang 的指针减法、`OffsetOfExpr`、源码实参及转发宏定义。位置、类型、嵌入成员和队列存储必须匹配。记录有每队列、每访问和总数上限，转移最多 3 跳。

## 验收

1. 用真实 Clang 验证通用的入队、转移、取首、按成员地址恢复容器以及字段访问。用两个同类型独立队列、错误嵌入成员和未识别操作做反例。
2. 用正式 CLI 验证原生导出及证据引用。跑字段、同步、内存定向测试和完整基线对照。
3. 用 HPR-048 的固定 libuv 提交静态核对 `uv_async_init` 注册到 `uv__async_io` 的来源，记录队列和 handle 能确认的关系以及余下的未知。分支独立保留以供审阅。

## HPR-048 静态取证

在 libuv `10902ab616058198067d6c2cc414014d36f86fff` 上以 Clang 19.1.7 读取全部 70 个编译单元。REcode 对 `src/unix/async.c` 的 `uv__async_io` 第 208 行原子访问导出一条 `possible_registration_source`。队列来源和恢复依据都来自编译器对应的源码位置。

| 源码位置 | 证据 |
| --- | --- |
| 第 85 行 | `uv_async_init` 将 `&handle->queue` 插入 `&loop->async_handles` |
| 第 198 行 | 匹配同一队列成员声明，将链转移到局部 `queue` |
| 第 200 行 | 取出局部队列的首成员 |
| 第 201 行 | 以 `uv_async_t.queue` 的 `offsetof` 从成员地址恢复 `h` |
| 第 207–208 行 | 经 `&h->pending` 关联到原有的原子字段绑定 |

该关系以队列成员声明为依据，记录 `parameter_member` 层级的可能来源。不同 `loop` 参数的实例关系、实际队首和 `uv_async_send` 外部实参的对象身份仍待证据，执行上下文也还没有绑定。语义同步状态仍为 `partial`，已绑定执行上下文的共享对象为 0。第三组候选生成和端到端验收本轮未执行。

## 回归结果

- 真实 Clang 样例分别追溯两个同类型队列中的两份注册对象。空队列、用错嵌入成员、宏体与实参不一致、未建模函数和中途覆盖的对照未生成队列来源。重复编译结果去重。
- 正式 `repo` CLI 导出队列来源和源码证据，证据 ID 可解析。
- 42 项定向回归通过。基线 157 项中 154 通过，修改版 158 项中 155 通过。两边均有两项 Docker 测试跳过，余下一项失败均为 `test_seven_stages_produce_evidence_graph` 的 `report_ready` 断言。

本机取证保存在 `D:/AgentProject/scratch/recode-sync-queue-provenance-validation` 的 `hpr048-queue-provenance.json`、`cli-queue-provenance.json` 和回归日志。
