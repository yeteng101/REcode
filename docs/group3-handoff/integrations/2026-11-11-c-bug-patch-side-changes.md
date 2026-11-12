# C-BUG-Patch 侧修改汇总与当前请求链

整理日期为 2026-11-11。面向本项目维护者及第一组对接成员。

## 范围与工作状态

代码位于 `D:/JDProject/C-BUG-Patch`，工作分支为 `dww`。本页汇总行为测试前置和 REcode 对接期间的本地修改。第一组仓库的修改单独记录在[REcode 侧汇总](2026-11-11-recode-side-changes.md)。

## 一、接入真实第一组服务

| 文件 | 修改及职责 |
| --- | --- |
| `v2-common/recode_client.py` | 新增异步任务客户端，提交固定 head，轮询、下载，支持已有 job_id，同一实例复用事实快照 |
| `v2-common/recode_artifacts.py` | 新增原生产物适配器，核对版本及哈希，解析目标、转换调用/资源/同步事实并保留缺口 |
| `v2-common/review_pipeline.py` | 增加 `recode` provider，支持 HTTP、已有 job_id 和本地产物目录 |
| `v2-common/coordinator.py` | 将原生资源及同步事实纳入专项选择，共享访问上下文未知时保留 Concurrency 路由 |
| `v2-common/verify_recode_integration.py` | 通过构造仓库、真实 HTTP 服务组件和 Docker 原生分析验证对接 |
| `v2-common/verify_recode_service.py` | 使用默认服务下载固定公开提交，核对产物发布及事实转换 |

原生产物适配包括以下边界。

- 校验 result、manifest、graph 和导出的仓库、commit、构建配置及 run_id。
- 下载后保存原始文件字节哈希，查询前检查内容一致性。
- 核对目标及 evidence 引用，保存未解析目标和省略记录。
- 优先消费支持范围内的 `semantic_sync`，兼容词法 `concurrency_facts` 和旧导出。
- 按覆盖情况保留 `partial`、unknown 及非正式证据声明。
- 资源 flow 保留独立身份，原生 race 标签通过专项 Reviewer 另行判定。

Memory 和 Concurrency 都保留公共层入口。当前 REcode 内存资源转换继续保留原分析覆盖缺口，最新两组端到端验收执行 Concurrency 专项。

## 二、修正候选准备及 PR 版本传递

| 文件 | 修改 |
| --- | --- |
| `v2-concurrency/prototype/analyze_sync_flow.py` | 接受缺失对象声明的生产输入并记录对应缺口，传播分析器的证据强度 |
| `v2-common/pr_adapter.py` | 识别常见指针/引用返回函数，生成输入时保留真实 base/head |
| `v2-concurrency/impact/lock_scope.py` | 从已经校验的宿主影响范围补齐生产者 head-only 响应缺失的版本字段 |
| `v2-concurrency/patching/pipeline.py` | 创建产品 worktree 前检查完整 base SHA |

PR diff、变更文件和目标由 Git 适配器准备。REcode 分析 head 对应代码，PR 关联及修复范围由本项目继续维护。

旧运行中出现的 `base_sha=null` 已保留原始记录。修复传递源头后，重新执行第一组取证和三个模型阶段，新产物完成正式 Schema 验收。

## 三、把行为测试安排在修复之前

| 模块 | 修改 |
| --- | --- |
| `v2-concurrency/patching/behavior.py` | 新增行为策略、复现凭据及冻结计划校验 |
| `v2-concurrency/orchestrator/workflow.py` | 默认执行 Reviewer、测试作者、复现验收、Patch Worker 和修复验证的顺序 |
| `v2-concurrency/patching/test_tasks.py` | 管理测试请求、宿主审查、捕获快照、重复执行及产物约束 |
| `v2-concurrency/patching/pipeline.py` | 产品 worktree 准备与修复计划绑定已验收的行为测试 |
| `v2-concurrency/patching/verifier.py` | 验证前复核凭据及计划，完整执行输出先用于失败分类，再保存有界摘要 |
| `v2-concurrency/patching/diagnostics.py` | 根据行为结果及范围信息区分产品、测试装置、环境和未知失败 |
| `v2-concurrency/mcp/server.py` | 向宿主暴露测试提交及编排状态交互 |
| 工作流、补丁和测试相关 Schema | 描述行为策略、凭据、审查及产物绑定 |
| CLI、Skill 和角色配置 | 统一入口参数、模型边界、测试职责及历史任务恢复规则 |

新严格运行默认使用 `behavior_before_patch`。测试作者从源码及证据中推导可观察行为，宿主独立审查实际断言及调度。原缺陷至少三次命中目标断言后，才进入产品修复。健康对照属于可选输入，需要针对当前行为的适用依据。

测试冻结后，修复版本复用同一份测试，同时执行批准的构建和项目回归。测试或环境问题先进入对应诊断与审查。产品修订保留一次受控反馈预算。

历史记录保持其已有策略，显式 `legacy_optional` 支持兼容运行。

## 四、扩展固定 C 测试执行模板

| 文件 | 修改 |
| --- | --- |
| `patching/test_tasks.py` | v0.2 policy 支持宿主选择 `redis_bio_c` 或 `c_source`，核对作者产物模板与请求一致 |
| `scripts/run_generated_redis_test.py` | 从冻结测试记录选择固定模板 |
| `scripts/run_redis_protocol_regression.py` | 通用 C 模板使用 `/source` include 根目录，临时目录兼容非 root 镜像 |
| `scripts/run_c_docker_verification.py` | 新增宿主控制的全 C 翻译单元编译、链接及程序 smoke 验证 |
| 测试 policy/case Schema、Test Worker 配置和 Skill | 同步描述模板、行为断言及执行边界 |

上述 scripts 路径位于 `v2-concurrency` 下。镜像、命令、挂载和编译配置由宿主固定。测试作者只编写指定的驱动与元数据。

## 五、修正 CRLF 捕获和验证

`patching/pipeline.py` 与 `patching/verifier.py` 在相关 Git 子进程中设置 `cr-at-eol`，接受源码原有的 CRLF，同时继续检查真实尾随空格。实际源码、补丁字节及仓库配置由原有绑定和 guards 维护。

## 六、测试、文档与角色适配

| 范围 | 主要文件 |
| --- | --- |
| REcode 对接回归 | `tests/common/test_recode_integration.py` |
| 事实缺口传播 | `tests/concurrency/test_recode_fact_gaps.py` |
| 行为测试前置 | `tests/concurrency/test_behavior_before_patch.py` 及严格验证、测试作者、验证范围回归 |
| CRLF | `tests/concurrency/test_patch_crlf.py` |
| 函数与版本绑定 | `v2-common/test_pr_adapter.py`、`v2-concurrency/scripts/test_audit_improvements.py` |
| 角色及迁移说明 | `.codex/agents/lock-race-test-worker.toml`、Claude Code 适配说明和两个工作流 Skill |
| 阶段记录 | `docs/project-change-records` 中 11 月 6 日行为验收及 11 月 11 日第 28—31 阶段 |

本轮 Reviewer 和 Patch Worker 使用注册专项角色。测试作者使用显式加载当前项目配置的 `worker` 适配，注册测试角色对新模板的直接调用验证为 `not_run`。

不同锁测试作者在完整保存两个文件后被工具安全检查中断。宿主检查保存内容并明确记录恢复报告的来源，随后通过相同产物捕获与运行验收。对应记录随证据保留。

## 七、当前请求链

### 1. 准备输入

宿主调用 PR/Git 适配器，生成 `review-input.v1`，包含仓库、base、head、构建配置、PR diff 和改动目标。目标源码检出需要与 head 一致。

### 2. 公共层请求事实

`review_pipeline.py` 创建 `ReviewCoordinator`，后者构建 `get_call_chains`、`get_resource_flows`、`get_sync_flows` 等内部查询。指定专项时按选定范围请求，自动路由时先取得相关事实再选择专项。

当 provider 为 recode，内部查询交给 `RecodeClient`。当前客户端首次获取该输入的快照时发出以下 HTTP 请求。

```http
POST /api/v1/agent/analyze
```

```json
{
  "repository_url": "https://github.com/<owner>/<repository>.git",
  "commit": "<完整 PR head SHA>",
  "build_profile": "safe-generic",
  "idempotency_key": "cbug-recode-<输入哈希>"
}
```

以上为字段示意，实际构建配置由任务与服务部署决定。

### 3. 第一组执行分析并发布结果

第一组返回 `job_id`，任务进入 SQLite 队列，runner 调用固定分析镜像，在目标 head 上执行 Clang/native 分析，将结果发布到对象存储。

客户端轮询状态，成功后依次获取 `/result` 和 `/artifacts`。下载产物包经过绑定及内容检查，保存到本次运行目录。

### 4. 本地适配与专项上下文准备

同一个 `RecodeClient` 实例为相同仓库与配置复用快照。三个内部查询由 `RecodeArtifactsClient` 在本地从已下载的图和导出文件中回答。

目标及深度限制用于本地产物选择。当前 REcode POST 请求只提交上述四个字段，第一组服务执行固定版本分析。

公共层组合来源事实、PR diff、源码片段、来源记录及覆盖缺口，生成 Memory/Concurrency `ContextBundle` 和交接记录。宿主根据交接结果调用专项准备入口。

### 5. 锁竞态专项检视

确定性专项代码计算影响范围、对象访问配对、共同保护和锁序信息，生成候选及逐候选请求。宿主通过 MCP 创建/读取运行，按 `next_action=invoke_lock_race_reviewer` 创建 Reviewer。

Reviewer 返回结论 JSON，宿主通过 `submit_lock_race_reviewer_result` 提交。工具校验候选、版本、证据及覆盖绑定。

### 6. 测试先行

接受的缺陷进入 `invoke_lock_race_test_worker`。宿主创建测试作者，独立审查实际断言后，通过 `submit_lock_race_test_worker_result` 提交报告及审查。

`advance_lock_race_run_until_boundary` 执行冻结、原缺陷重复复现及可选健康对照。复现验收通过后准备 Patch Worker 请求。

### 7. 修复与验收

宿主按 `invoke_lock_race_patch_worker` 创建修复角色。它在固定 head 的隔离 worktree 内修改许可文件，返回报告。宿主通过 `submit_lock_race_patch_worker_result` 提交。

确定性推进捕获真实 Git diff、执行批准的 Docker 构建/项目测试及同一冻结行为测试，核对验证前后差异哈希，然后记录结果或产生受控反馈。

### 8. 本轮额外复验

本轮验收还把已验证的修复源码放入独立验证快照，重新请求第一组，核对候选从 1 降为 0。这一步由本轮宿主验收脚本执行，常规编排器在补丁验证后收尾。

## 八、调用关系总览

```text
宿主调用 PR/Git 适配器
  → review-input.v1
  → 公共层 ReviewCoordinator
  → RecodeClient 提交 HTTP 分析任务
  → 第一组队列、runner、Clang/native、对象存储
  → 客户端轮询并下载结果包
  → 本地产物适配器回答内部事实查询
  → 公共层选择专项并准备 ContextBundle
  → Concurrency 确定性候选准备
  → MCP 编排状态
  → 宿主创建 Reviewer
  → 宿主创建 Test Worker，工具执行修复前复现
  → 宿主创建 Patch Worker，工具捕获并验证补丁
  → 最终报告与证据
```

宿主持有实际模型调用能力。MCP 提供工具接口，编排器持有状态、顺序与预算，第一组提供分析事实。

## 九、当前验收结果与范围

| 项目 | 结果 |
| --- | --- |
| Python 3.12 主项目回归 | 314 项及 smoke 通过 |
| 最终独立 Schema 审计 | 30 份通过 |
| 最终证据快照 | 104 份，附逐文件 SHA-256 及两个 Git bundle |
| 一侧缺锁和不同锁两个四文件构造 PR | 均完成真实检视、测试、修复及 Docker 验证 |
| 健康、原缺陷、修复版本的目标行为测试 | 每例分别为 0/0/0、42/42/42、0/0/0 |
| 修复后第一组重新分析 | 两例候选均为 0 |
| 大型 Redis/libuv 仓库的本轮复验 | `not_run` |
| 新语义代码的生产下载 runner 复验 | `not_run` |

第一组最新完整回归保留 2 个既有失败和 2 个跳过，详见第一组侧汇总。两组端到端结果的适用范围是本页列出的构造 PR 及受支持语义。

两例的汇总指标为目标检出 2/2、实际补丁产出 2/2、批准验证计划下补丁通过 2/2。十八次健康/原缺陷/修复行为执行均符合各自预期。阶段耗时、分母及统计边界见[完整闭环量化指标](../project-change-records/2026-11-11-31-recode-review-test-repair-full-flow.md#完整闭环量化指标)。

- [完整闭环报告](../project-change-records/2026-11-11-31-recode-review-test-repair-full-flow.md)
- [最终证据清单](../project-change-records/2026-11-11-recode-full-flow-final-evidence/manifest.json)
- [对接入口和启动说明](recode.md)
