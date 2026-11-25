# Memory 有界生命周期路径证明

词法资源流的 `complete` 表示同一对象的分配与释放已经在受支持的直线路径上配对。取消时缺失释放的路径需要单独表达，因此原生 `external/resource-flow.json` 新增可选字段 `memory_path_proofs`，保留已有资源条目、`complete` 和全局覆盖状态的含义。

## 接口

- `schema_version` 使用 `recode-memory-path/1`，`basis` 使用 `clang-json-ast-paths`。
- `binding` 绑定 `repository`、`commit`、`build_profile` 和 `run_id`，与同批原生导出一致。
- 每条 `traces` 记录入口源码、具体的 `arguments` 和有序 `events`。事件有源码位置和证据 ID，覆盖分配、字段绑定、分支、调用、容器释放、对象释放和入口退出。
- 间接调用事件绑定同一存活容器的字段、函数身份和定义位置。退出事件列出仍存活且没有被其他存活对象持有的分配。被泄漏对象持有的内存作为间接损失一并解释。
- 入口使用现有 `--entry` 参数显式指定。例如此案例使用 `--entry service_run`。入口支持至多两个参数。`int` 枚举 0、1，`int *` 和指针出参用 `output_slot` 表示调用方提供的独立、非空槽位。模拟仅覆盖受支持的分配成功和失败。指针槽位的写入由 `bind_output` 绑定到存活分配或空值。退出时，仍能从调用方槽位访问的分配及其子对象属于调用方。未初始化的读取保留为取证缺口。只为完整求值的输入导出路径，未建模操作和预算限制记入 `gaps`。该产物的结论限于列出的具体路径。

## 验证

从同一组编译单元用 Clang 19 重新导出缺陷版本 `2e6c03010b9aa8401d7993886f351c2db2ee44e3` 和健康版本 `e9af0f3800b49e5c96c13d7e1235d466dc9ce08d`。两份产物通过资源流 Schema 验证，各导出 10 条有界路径，全局状态仍为 `partial`。

| 版本及入口参数 | 原生路径结果 |
| --- | --- |
| 缺陷版 `service_run(1)` | 实参进入 `worker.c:20` 的 `job->request`，`worker.c:53` 释放 `Job`。到达 `service.c:21` 时同一 `Request` 仍存活。 |
| 缺陷版 `service_run(0)` | `worker.c:36` 的间接调用解析为 `on_job_complete`，同一 `Request` 在 `service.c:23` 退出前已释放。 |
| 健康版 `service_run(1)` | `service.c:21` 退出前已释放同一 `Request`。健康版 10 条已建模路径均无存活的根分配。 |

两项生命周期定向测试通过，含把正常回调换成未知外部函数后拒绝导出该输入的证明。完整回归共 143 项，140 项通过、2 项跳过，历史报告测试仍失败于 `report_ready=false`。本次验证范围为原生事实导出和消费端准入。

## 分配器表与调用方输入（2026-11-25）

libuv `2480b615` 的 Memory 全流程产物出现了 13 个审查候选，但 `memory_path_proofs.traces` 为 0，缺口集中在三处：外部 `environ` 未绑定、清理函数参数未建模、间接分配器调用解析不足。补齐后仍沿用 `recode-memory-path/1`，只扩展取值而不是新增旁路结构。

- 静态记录表：文件作用域的记录变量若以函数指针列表初始化，按字段保存函数身份。`uv__allocator.local_malloc` 这类字段调用解析为具体分配器，`call` 事件带 `callback.container`、`callback.field` 和 `callback.kind=record`，即使目标是 libc 也保留表字段来源。匿名声明的 typedef 记录按声明区间归属，`uv__allocator_t` 和 `uv_env_item_t` 都能落到同一 TU 的记录声明。
- 入口参数：整数类型（含 `size_t`）枚举 0、1；`T **` 与 `int *` 仍是 `output_slot`；指向记录的 `T *` 表示调用方提供的定长区域，事件序列以 `caller_input` 开头，字段初值是调用方值；`void *`、`char *` 等其余指针表示调用方不透明指针，参数取值为 `caller_pointer`。`arguments` 枚举相应扩展为 `0`、`1`、`output_slot`、`caller_input`、`caller_pointer`。
- 外部全局：只有声明、没有定义的全局指针（如 `environ`）绑定为外部引用，不再让整个入口报 `unbound_value`。解引用或下标读取报告 `external_value_extent_unmodeled:<符号>`；作为释放对象时记录 `release` 并标记 `external=true`。
- 退出判定：返回给调用方的分配记为 `returned` 并从 `live_roots` 中排除；调用方区域不计入本函数的存活根，但其字段中仍存活的分配按逃逸处理。
- 索引缺口的生效范围收窄到函数体：静态局部变量、别名存储和符号别名只让调用该函数的入口失败（`index_gap_in_callee`），不再因为同一 TU 内无关函数而丢弃全部路径。歧义函数定义仍然全局生效。

### 验证

在 `libuv/libuv@2480b615` 的 `src/uv-common.c` 与 `src/unix/core.c` 上用 Clang 19 导出，入口为影响域给出的 `uv_os_environ`、`uv_os_free_environ`。

| 入口及参数 | 原生路径结果 |
| --- | --- |
| `uv_os_free_environ(caller_input, 0)` | 调用方区域在 `uv-common.c:798` 释放，退出没有存活根。 |
| `uv_os_free_environ(caller_input, 1)` | `uv-common.c:795` 释放 `envitems[0].name`，经 `uv-common.c:88` 的分配器表字段调用落到 `free`，退出没有存活根。 |
| `uv_os_environ(output_slot, output_slot)` | 缺口 `external_value_extent_unmodeled:environ`；`environ[i]` 的长度无法从源码确定，不导出路径。 |

定向回归覆盖分配器表间接调用、错误返回时同一对象的存活状态、容器释放后子对象仍存活、清理入口的调用方区域，以及未知分配器仍报 `unmodeled_call`。剩余缺口：外部全局数组的规模需要入口级假设契约；`strchr`、`realloc` 等 libc 调用未建模；`uv_os_environ` 的错误路径需要先确定 `environ` 长度才能取证。
