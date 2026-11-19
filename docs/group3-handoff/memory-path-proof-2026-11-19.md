# Memory 有界生命周期路径证明

词法资源流的 `complete` 表示同一对象的分配与释放已经在受支持的直线路径上配对。取消时缺失释放的路径需要单独表达，因此原生 `external/resource-flow.json` 新增可选字段 `memory_path_proofs`，保留已有资源条目、`complete` 和全局覆盖状态的含义。

## 接口

- `schema_version` 使用 `recode-memory-path/1`，`basis` 使用 `clang-json-ast-paths`。
- `binding` 绑定 `repository`、`commit`、`build_profile` 和 `run_id`，与同批原生导出一致。
- 每条 `traces` 记录入口源码、具体的 `arguments` 和有序 `events`。事件有源码位置和证据 ID，覆盖分配、字段绑定、分支、调用、容器释放、对象释放和入口退出。
- 间接调用事件绑定同一存活容器的字段、函数身份和定义位置。退出事件列出仍存活且没有被其他存活对象持有的分配。被泄漏对象持有的内存作为间接损失一并解释。
- 入口使用现有 `--entry` 参数显式指定。例如此案例使用 `--entry service_run`。当前支持 C 的无参入口和至多两个 `int` 参数，枚举每个参数的 0、1，模拟受支持的分配成功和失败。只为完整求值的输入导出路径，未建模操作和预算限制记入 `gaps`。该产物的结论限于列出的具体路径。

## 验证

从同一组编译单元用 Clang 19 重新导出缺陷版本 `2e6c03010b9aa8401d7993886f351c2db2ee44e3` 和健康版本 `e9af0f3800b49e5c96c13d7e1235d466dc9ce08d`。两份产物通过资源流 Schema 验证，各导出 10 条有界路径，全局状态仍为 `partial`。

| 版本及入口参数 | 原生路径结果 |
| --- | --- |
| 缺陷版 `service_run(1)` | 实参进入 `worker.c:20` 的 `job->request`，`worker.c:53` 释放 `Job`。到达 `service.c:21` 时同一 `Request` 仍存活。 |
| 缺陷版 `service_run(0)` | `worker.c:36` 的间接调用解析为 `on_job_complete`，同一 `Request` 在 `service.c:23` 退出前已释放。 |
| 健康版 `service_run(1)` | `service.c:21` 退出前已释放同一 `Request`。健康版 10 条已建模路径均无存活的根分配。 |

两项生命周期定向测试通过，含把正常回调换成未知外部函数后拒绝导出该输入的证明。完整回归共 143 项，140 项通过、2 项跳过，历史报告测试仍失败于 `report_ready=false`。本次验证范围为原生事实导出和消费端准入。
