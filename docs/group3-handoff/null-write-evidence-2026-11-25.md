# Memory 空指针写入静态证据

生命周期路径证明回答"对象何时释放"，但对"分配失败后指针仍被解引用"没有独立事实。第 3 轮在 `memory_path_proofs` 上增加可选 `null_writes` 数组，与 `traces` 互不替代：路径证明继续承担释放配对，`null_writes` 单独记录"空值指针在什么条件下被写"。

## 接口

- 每条记录包含 `null_write_id`（`ast_nullwrite_` 前缀 20 位十六进制）、入口位置、`arguments`（复用 `0`、`1`、`output_slot`、`caller_input`、`caller_pointer` 枚举）。
- `pointer` 绑定 `null_id`（`ast_null_` 前缀）、分配失败点（`allocation`，含 API 与位置）、指针声明绑定（`binding`，含声明名与位置），三者的证据分别使用 `ast_memory_null_allocation`、`ast_memory_null_binding`。
- `conditions` 至少一条 `allocation_failure` 且 `taken=true`，另有 `branch`（`!`、truth）或 `comparison`（`==`、`!=`）记录空值的来源判定，证据类型 `ast_memory_null_condition`。
- `write` 区分 `field`（命名字段写入）与 `dereference`（裸解引用写入），证据类型 `ast_memory_null_write`。
- 值域沿用受支持分配的成功与失败两种模拟；未绑定声明的写入（如强转字面量）不导出记录，改报 `null_write_binding_unverified:<null_id>` 缺口。

## 验证

定向夹具覆盖：

| 夹具 | 结果 |
| --- | --- |
| 分配失败后写入 `p->name` | 导出 1 条记录，分配、绑定、条件、写入各自绑定证据；条件顺序为 `allocation_failure`、`branch`，均 `taken`。 |
| 守卫后使用（`if (!p) return -1;`） | 0 条记录，无空指针缺口。 |
| 经由另一成功分配写入 | 0 条记录。 |
| 强转字面量直接写入 | 0 条记录，报 `null_write_binding_unverified`。 |
| `p == NULL` 判定后写入 | 导出 1 条记录，条件含 `comparison`（`==`）。 |

`clang_pipeline` Memory 定向 29 项全部通过；`clang_pipeline`+`scripts`+`validation` 回归 225 项通过、2 项跳过，4 项失败与基线 `20c5818` 在同一环境的复跑结果一致（`report_ready` 历史失败及三项 `/mnt/d` 路径相关的 validation 数据集校验），与本次改动无关。

HPR-037（libuv `2480b615`，`uv_os_environ`/`uv_os_free_environ`）继续作静态对照：r3 分析器在该真实 PR 上保持 r2 的 `partial` 判定，路径 0 条、缺口集合逐项一致（`uv__allocator` 表写入、`environ` 外部规模、`errno` 初值），且 `null_writes` 为 0——新事实类未在真实案例上引入误报。对照产物见 `r3-evidence/hpr037-null-write-static-control.json`。

Inspector 侧消费见 `cbug-inspector-null-write-r3` 的 `null-write` 夹具与 `tests/memory/test_null_write_workflow.py`：正例候选 `null_pointer_write` 达到 `candidate_decision.complete`，篡改条件、删除 `allocation_failure` 条件、裁剪写入行三类反例全部破坏完整性，评审请求保持非正式证据上限。联合包重建留待第 4 轮。
