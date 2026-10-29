# Agent Tool Trace 与异常恢复

## 1. Trace 文件

每次 `analyze_repo` 都会生成：

```text
<output>/trace/tool-trace.jsonl
<output>/trace/tool-trace-summary.json
```

JSONL 中每个 Span 至少包含：

```text
trace_id
span_id
parent_span_id
run_id
tool
arguments
status
started_at
finished_at
duration_ms
result.sha256
result.bytes
result.preview
error
```

单项 Schema：`schemas/tool-trace.schema.json`。

参数中的 `token`、`password`、`secret`、`authorization` 等字段会被脱敏。
结果只保存摘要、哈希和截断预览，不复制完整大文件。

当前覆盖：

```text
analyze_repo
checkout_repository
inspect_repository_limits
discover_compile_commands
cmake_configure
generate_compile_commands
pipeline.01-index ... pipeline.07-report
native_export
bundle_artifacts
relativize_artifact_paths
write_visualization
```

Agent 查询工具也会记录：

```text
get_resource_flow
get_sync_relations
get_races
get_visualization
```

## 2. Job API

提交：

```bash
curl -X POST http://127.0.0.1:8090/api/v1/agent/analyze \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"https://github.com/example/project.git"}'
```

查询：

```bash
curl http://127.0.0.1:8090/api/v1/agent/analyze/<job_id>
```

取消：

```bash
curl -X POST http://127.0.0.1:8090/api/v1/agent/analyze/<job_id>/cancel
```

任务状态：

```text
queued
running
cancelling
succeeded
failed
cancelled
timed_out
```

## 3. 异常恢复

| 场景 | 行为 |
|---|---|
| 源码路径不存在 | `repository_not_found`，生成 `error.json` 和失败 Trace |
| Git 检出失败 | `repository_checkout_failed` |
| CMake 配置失败 | 记录 `cmake_configure=degraded`，回退到 generic compile commands |
| Clang/AST 阶段失败 | `clang_stage_failed`，记录阶段、stdout/stderr 摘要 |
| 阶段超时 | `analysis_timeout`，终止子进程并记录失败 Span |
| 仓库超过限制 | `repository_too_large`，返回文件数、字节数和限制值 |
| 用户取消 | `analysis_cancelled`，Job 状态变为 `cancelled` |
| 结果缺少 evidence | Agent 答案标记为 `partial`，不能当作已确认结论 |

服务配置：

```text
CRA_MAX_WORKERS
CRA_JOB_TIMEOUT_SECONDS
CRA_MAX_REPOSITORY_FILES
CRA_MAX_REPOSITORY_BYTES
CRA_TRACE_ROOT
```

## 4. 验证

```bash
python3 -m unittest \
  clang_pipeline.tests.test_agent \
  clang_pipeline.tests.test_agent_jobs \
  clang_pipeline.tests.test_agent_tool_contracts \
  clang_pipeline.tests.test_pipeline \
  clang_pipeline.tests.test_repo_agent -v
```

查看最近 Trace：

```bash
jq -c '{tool,status,duration_ms,result,error}' \
  artifacts/agent-jobs/<job_id>/trace/tool-trace.jsonl
```
