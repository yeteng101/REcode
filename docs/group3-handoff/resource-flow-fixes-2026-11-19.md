# REcode 资源流的对象与路径归属

本轮以 `group3` 的 `849614a` 为基线。既有第三组对接工作记录在 [11 月 12 日交接](README.md) 和 [第一组改动汇总](integrations/2026-11-11-recode-side-changes.md)。本次针对 `native_resource_flow_v1` 的 Memory 输出。

## 已确认的问题

用 `cbug-local/sample-source` 的 `2e6c03010b9aa8401d7993886f351c2db2ee44e3` 固定检出验证。历史原生产物 `external/resource-flow.json` 的 SHA-256 为 `5ea6da392ebaa34a93fb3d6966ec31f42bdc5459bcdbb13276f318429af66470`。

| 问题 | 观察到的输出 | 验收条件 |
| --- | --- | --- |
| 对象混流 | `rf_b443168d98bf` 混入了 `Buffer` 的分配、`Request` 的分配调用和 `Job` 的释放 | 通过具体分配证据、调用点和局部绑定区分对象 |
| 包装函数分配丢失 | `job_allocate()` 和 `buffer_acquire()` 只有 `transfer`，调用方缺少分配来源 | 回溯到实际分配点，并保留调用方接收值的证据 |
| 释放归属错误 | `worker.c:53` 的 `free(job)` 让 `request` 路径标为 `release_found` | 该释放只能绑定到传入的 `Job` |
| 路径结论过强 | 一条 `visible_graph_path` 把多个位置当作一条完整生命周期 | 只在证明同一对象及路径时标记 `release_found`；保留未解析的分支和所有权 |

## 改进范围

跨函数证据要保留分配源、调用方接收位置、已绑定的释放参数和各自的证据编号。普通赋值、包装函数和明确的参数转发优先处理。间接回调、复杂别名、循环及不完整控制流仍需更强的分析，原生产物应保留具体缺口。

## 实现与契约

- `resource_instance_id` 绑定分配位置和可观测的调用点。同一分配包装函数的不同调用点各自生成一条流。包装函数的 `transfer` 携带 `allocation_origin`，并保留实际分配操作。
- 局部值按所在函数和源位置追踪。跨函数释放只沿明确的参数位置和直接调用传播，输出 `via_calls` 和对应证据。分支定义、重复赋值及无法定位的参数可以保留未解析的释放观察。
- 直线代码里通过顺序和赋值证据的同实例释放沿用 `release_found`。跨函数或缺控制流证据时使用 `release_path_unproven`，观察到的返回点另行记录。释放观察缺少可信分配来源时使用 `unresolved_acquire`。原生 schema 版本保持 `1.0`。

## 验证结果

- WSL Debian 和 Clang 19 对同一固定 head 重新取证。基线原生重跑把 `free(job)` 混入 `request->payload` 流。修复后 `Request`、`Job`、`Buffer` 分别保留 `request.c:10`、`worker.c:12`、`allocator.c:7` 的分配源。取消分支的 `free(job)` 在 `Job` 流通过 `cancel_service → worker_cancel` 绑定。 `Request` 在 `service.c:21` 的返回点保留 `ownership_path_unresolved`。
- 原生导出通过 `resource-flow.schema.json` 检查。实际导出 14 条资源流，整体仍为 `partial`。本次验证产物 `external/resource-flow.json` 的 SHA-256 为 `11fcf1c0f0ce50ca52ff32225e3b12dda51469ed896a5a459e45f593ae8f5bd4`。
- C-BUG-Patch 已接收原生产物，完成 Memory ContextBundle 和候选准备。仍保留间接回调和生命周期路径覆盖缺口，候选继续等待补证。
- 新增 5 项资源绑定回归通过。带固定 libuv 和 Redis 的 validation 套件 40 项通过，call-chain demo 11 项通过。Clang 全套运行 141 项，138 通过、2 跳过；`test_seven_stages_produce_evidence_graph` 的 `report_ready` 断言在 `849614a` 与修复版上均失败。
