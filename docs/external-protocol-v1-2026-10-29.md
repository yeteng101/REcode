# External-v1 评测协议与 Agent 闭环（2026-10-29）

## 1. 当前目标

本轮不再以 libuv/Redis 小样本分数作为独立准确率证据，而是建立可复现的
外部评测协议：

```text
仓库选择 -> 数据版本锁定 -> Agent 预测 -> schema 校验
-> 独立 Gold 评分 -> unknown/coverage 报告 -> CI 门禁
```

六套协议分别是：

```text
CallGraphBench      调用边
CG-Bench            callsite target-set
Juliet CWE-401      函数级泄漏
SV-COMP MemSafety   task/property 泄漏
DataRaceBench       case 级 race/no_race/unknown
SV-COMP Concurrency task/property race/no_race/unknown
```

## 2. 固定子集结果

| 数据集 | 评价单位 | 结果 |
|---|---|---|
| CallGraphBench | caller-callee edge | P 100.00%，R 58.06%，F1 73.47% |
| CG-Bench | callsite-target pair | P 67.60%，R 77.84%，F1 72.36%，coverage 86.42% |
| Juliet CWE-401 | function | P 100.00%，R 100.00%，coverage 100.00% |
| SV-COMP MemSafety | task/property | P 55.56%，R 100.00%，coverage 100.00% |
| DataRaceBench | case | 已回答样本 P/R 100%，unknown 25.00%，coverage 75.00% |
| SV-COMP Concurrency | task/property | P/R 100%，coverage 100.00% |

这些结果只对应 `validation/external/selections/` 中的固定子集，不是完整
CallGraphBench、DataRaceBench 或 SV-COMP 的总体结论。尤其 MemSafety 仍有
明显误报，不能写成最终泄漏准确率。

## 3. CG-Bench Agent

Agent 的输入只有 callsite、函数指针表达式和源码上下文，不接收官方 targets。
工具循环依次检查：

```text
pointer assignment
pointer table
struct initializer
callback registration
macro dispatch
```

每个 callsite 输出：

```text
predicted target set
unknown
evidence
candidate reasons
tool trace
```

当上下文不足以确定目标函数时输出 `unknown`，不猜测。评分器将 target set
展开为 `(callsite, target)` 对，只有 `predicted` callsite 进入 TP/FP/FN；
`unknown` 单独报告并降低 coverage。

## 4. Race 判定

`clang_pipeline/race_detector.py` 当前实现的是保守静态分析，不是完整动态
race detector。它覆盖：

- OpenMP parallel-for 迭代索引和 private/reduction clause
- 同一语句中的并行索引冲突
- 线程 create/join 生命周期
- mutex lock/unlock、`__VERIFIER_atomic_begin/end`
- atomic global 和原子访问
- 无法证明别名或不重叠时输出 `unknown`

输出必须满足：

```text
race
no_race
unknown
```

`unknown` 不会被转换成 `no_race`。

## 5. Agent 服务入口

启动本地服务：

```bash
python3 -m clang_pipeline.agent_server --host 127.0.0.1 --port 8090
```

提交仓库 URL：

```bash
curl -sS -X POST http://127.0.0.1:8090/api/v1/agent/analyze \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"https://github.com/example/project.git","commit":"<sha>"}'
```

查询任务：

```bash
curl -sS http://127.0.0.1:8090/api/v1/agent/analyze/<job_id>
```

结果目录包含：

```text
manifest.json
visualization.html
external/call-chains.json
external/resource-flow.json
external/sync-relations.json
external/race-analysis.json
external/knowledge-graph.json
```

## 6. 发布顺序

1. 生成 selection、protocol 和 prediction schema。
2. 运行 dry run，修复格式和身份映射。
3. 运行完整固定子集 baseline。
4. 检查输入哈希并生成 `protocol-lock.json`。
5. 在 CI 中执行协议校验、外部 benchmark 和 Agent 单元测试。
6. 冻结 `external-v1`；后续修改进入 `external-v2`。

## 7. 仍未完成

```text
跨函数资源所有权和复杂别名
完整 CFG / reads-from / happens-before
平台条件调用和更多 builtin/macro 规则
更大规模人工复核
生产级鉴权、队列、沙箱、限流和回滚演练
```
