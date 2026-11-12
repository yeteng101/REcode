# v2-common：第三组专项公共框架

第一组 REcode 已提供异步任务及原生结果包适配，支持
`--first-group-provider recode`、`--recode-artifacts` 和 `--recode-job-id`。
运行方法和数据缺口见 [REcode 对接说明](../docs/integrations/recode.md)。

本目录是第三组内部由 Memory、Concurrency 和后续专项共享的公共运行层。它由面向用户的宿主主 Agent 调用或集成，负责第一组 HTTP 边界、事实协议校验、PR 目标交接、组内专项路由、受控源码加载、上下文预算、ContextBundle 和独立验证 handoff；不实现宿主主 Agent，也不做内存、竞态或其他领域缺陷结论。

```text
用户 → 宿主主 Agent → 第三组 v2-common → Memory / Concurrency → 第三组结果 → 宿主主 Agent
```

宿主主 Agent 负责用户交互、仓库/PR 使用方式和最终结果呈现。`v2-common` 的正式输入边界是宿主或其 PR 适配器提供的 `review-input.v1`；输出是专项 ContextBundle、handoff 和第三组结构化运行状态。

## 入口

- 架构与状态：`CONTEXT_DESIGN.md`
- 其他专项接入说明：`SUBAGENT_INTEGRATION.md`
- 本地第一组 HTTP Mock：`mock_first_group_server.py`
- 第三组 review/Coordinator 入口原型：`review_pipeline.py`
- GitHub PR 公共输入生成器：`acquire_github_pr.py`（输出 `review-input.json`，供 Memory/Concurrency 共用）
- 通用三阶段模型调用：`stage_runner.py`
- 候选 Patch 文件边界：`patch_contract.py`
- 验证请求/结果契约：`verification_contract.py`
- 独立验证入口：`independent_verifier.py`
- 专项登记：`specialty_registry.json`
- 示例 review：`../examples/memory-minimal/review-input.json`

当前第一组服务端是本地 fixture Mock。HTTP 客户端和三条事实接口路径已经固定，后续接真实第一组时应替换服务地址和生产协议细节，不在各专项中另写客户端。

当前 Mock registry 同时登记 Memory 最小样例和 Concurrency `LK-001` 的调用链、同步事实；Mock 支持三条同步 POST 接口和可选的异步 submit/run 查询。

Context Manager 对普通文件加载完整 head 源码；对超过预算的大文件，按 changed function、第一组 Evidence 和事实位置加载带原始行范围的受控片段。无法覆盖所有相关位置时仍会把 coverage 降级，不能用切片隐藏缺失上下文。

Coordinator 按专项隔离失败：通用调用链失败会终止本次准备；某个专项事实、Bundle 或 registry handoff 未就绪只会令报告成为 `partial` 并写入 `route_errors`，其他已经准备好的专项 Bundle 仍会输出。`review_pipeline.py` 对 `prepared`、`partial`、`failed` 分别返回进程码 0、2、1。

候选 Patch 会同时校验声明的 `changed_files` 与 unified diff 真实文件头；验证 handoff 使用 `request_id` 和 `patch_sha256` 绑定结果，并要求验证结果覆盖请求中的完整检查 ID 集合。这些约束已有自动测试覆盖。

## 实现与运行状态

- 公共框架代码：已实现，Python 3.12 公共测试通过。
- 本地第一组 HTTP Mock：已实现，HTTP smoke 已在 Python 3.12 下执行并通过。
- 公共、Memory 和 Concurrency 接入测试：已执行并通过。
- 公共独立验证 runner：已实现，专项验证后端 `not_implemented`，实际验证 `not_run`。
- Concurrency Subagent：已登记并可消费 `context-bundle.v1`；真实第一组服务端仍为外部依赖。
- GitHub PR 适配器：`acquire_github_pr.py` 固定 base/head、建立 detached checkout，生成两专项共用的 `review-input.v1`；底层 GitHub 获取逻辑暂复用 `v2-concurrency/acquisition/github.py`，原锁竞态脚本保留兼容。

默认不指定专项，由 Coordinator 保守路由。变更函数按 head 源码与 diff 变更行定位；不确定处写入 `acquisition_coverage_gaps`。完全无法定位时，宿主可提供经核实的 `--changed-functions-json`，或为纯字段/结构体改动提供 `--targets-json`；两者都没有时拒绝生成。`targets` 使用第一组的目标格式（如 `shared_object`、`function`、`callback`），必须绑定具体实体和仓库相对源码位置。实体目标适合查询同步事实；调用链仍应附上使用该实体的函数或回调目标，不能假定服务能从字段名自动枚举全部调用路径。宏和复杂 C++ 声明不能保证自动定位完整。

例如字段定义本身是唯一改动时，`--targets-json` 文件可以包含 `[{"target_id":"counter","type":"shared_object","name":"Shared.counter","location":{"file":"src/shared.h","line":20},"role":"changed_shared_object"}]`。这不会消除 `acquisition_coverage_gaps`；还需核对读写函数和第一组实际返回的覆盖范围。该能力沿用现有第一组 JSON 契约，没有新增接口字段。

```powershell
py -3.12 v2-common/acquire_github_pr.py --pr-url https://github.com/OWNER/REPO/pull/123 --mirror D:/pr-cache/repo.git --workspace D:/pr-runs/pr-123 --build-profile linux-gcc
py -3.12 v2-common/review_pipeline.py --review D:/pr-runs/pr-123/review-input.json --first-group-url http://127.0.0.1:8765 --output-dir D:/pr-runs/pr-123/coordination
```

首条命令只负责 PR 获取与公共输入生成，不声称已运行 Memory/Concurrency 模型或编译验证。真实 GitHub 获取需要网络和相应权限；当前第一组服务仍为 fixture Mock，真实事实服务需另行接通。
