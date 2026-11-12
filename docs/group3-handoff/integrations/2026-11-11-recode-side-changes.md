# 第一组 REcode 修改汇总与对齐清单

整理日期为 2026-11-11。面向 REcode 维护成员，供双方核对修改、接口及部署状态。

## 代码基线与修改范围

| 项目 | 当前记录 |
| --- | --- |
| 上游仓库 | `https://github.com/yeteng101/REcode` |
| 本地目录 | `D:/JDProject/REcode` |
| 下载基线 | `e6e3b4222715cea71c150f0dd824ed990e7f567d` |
| 修改状态 | 本地工作区代码及新增文件 |
| 运行环境 | Python 3.12、Clang 14、Docker |
| 对接方 | `C-BUG-Patch/v2-common` |

HTTP 分析端点、SQLite 持久化队列、对象存储、任务状态与默认 Docker runner 属于第一组已有能力。本次在这些入口上完成兼容修复、固定版本贯通和并发事实扩展。

## 一、运行兼容与产物交接

| 文件 | 修改内容 | 解决的问题 |
| --- | --- | --- |
| `clang_pipeline/stages.py` | 可选 libclang 提取模块加载失败时使用已有编译器 JSON AST 路径，并记录实际分析器 | Clang 14 缺少部分 libclang API 时流水线无法启动 |
| `clang_pipeline/repo_agent.py` | `run_repo_analysis` 增加 `snapshot_identity` 参数，将固定仓库和完整 commit 贯穿图、导出、manifest 和结果 | 归档输入缺少 Git 元数据时，内外层仓库及版本信息不一致 |
| `clang_pipeline/sandbox_entry.py` | 将归档入口已绑定的仓库与 commit 传入 `snapshot_identity` | 容器内分析继承任务入口的固定版本 |
| `clang_pipeline/sandbox.py` | 通过容器内 Python tar 流导出运行时 tmpfs 结果，排除内部 workspace、checkout 和 `.git`，沿用配额及归档校验 | Docker Desktop archive/cp 读取 tmpfs 产物失败，以及内部临时文件导致 Windows 长路径错误 |
| `clang_pipeline/sandbox.py` | 区分 Docker 可执行文件缺失与分析产物缺失，保存相应错误信息 | 异常分类及定位信息不足 |

`snapshot_identity` 仅接受 safe-mode 场景、规定字段集合和完整 40 位 commit SHA。该参数的调用边界已加入回归。

## 二、补充带来源的词法事实

新增 `clang_pipeline/concurrency_facts.py`，由 `race_detector.py` 调用，结果随 `external/race-analysis.json` 的 `concurrency_facts` 字段导出。

| 返回内容 | 字段及含义 |
| --- | --- |
| 声明观察 | `objects` 保存简单全局变量和锁的候选声明、类型、位置及链接属性 |
| 线程 API | `thread_events` 保存创建与 join 的 API、入口表达式、句柄表达式及位置 |
| 锁 API | `lock_operations` 保存加解锁 API、锁表达式、所属函数及位置 |
| 访问关联 | `access_bindings` 保存访问与候选声明、创建位置和锁操作的关联 |
| 来源与缺口 | `evidence` 记录源码位置，`coverage` 记录尚未证明的事项 |

词法记录的声明关联保留候选性质，线程实例、重叠关系和必持锁集保留 unknown。它为后续分析提供可追溯的观察记录。

`race_detector.py` 同时修正 `thrd_create` 的入口函数参数索引，使用 C11 API 的第二个参数。

## 三、增加有边界的 AST 同步分析

新增 `clang_pipeline/semantic_sync.py`，由 `native_export.py` 调用。它读取实际 Clang JSON AST 和编译配置，生成 `race-analysis.semantic_sync`，内部版本为 `bounded-ast-sync/1`。

| 能力 | 本次实现 |
| --- | --- |
| 对象身份 | 按声明引用绑定全局标量，处理支持范围内的 extern 和简单指针别名 |
| 访问事实 | 保存实际读写位置、对象身份与 evidence |
| 执行上下文 | 沿唯一 main 及直接创建的线程分析，记录创建/join 的成功与失败路径 |
| 并发关系 | 在分析覆盖完整时记录可能重叠及可排除的上下文关系 |
| 锁状态 | 沿直接调用和支持的结构化路径传播初始化状态、必持锁及可能持锁 |
| 调用关系 | 保存带来源的直接调用图和独立调用覆盖状态 |
| 分析预算 | 保存路径数、调用深度和步数上限，达到边界时记录缺口 |

输出中包含 `findings`、`call_graph`、`coverage`、`call_coverage`、`translation_units`、`limits` 和 `api_model`。访问、声明、锁操作、线程事件及调用保留源码证据。

分析出现缺口时，具体原因进入 coverage，必持锁和线程排除证明按保守规则收回。C-BUG-Patch 根据这些覆盖信息决定候选是否具备复核条件。

当前支持范围为有唯一 main 的封闭 C 程序、全局标量、简单别名、直接调用、受支持的分支路径和默认 POSIX 同步 API。复杂循环、递归、聚合对象、栈对象跨线程、复杂间接调用、嵌套多锁及锁序、atomic 协议和 C++ 等范围继续记录缺口。

## 四、对外接口与兼容关系

### HTTP 端点

沿用第一组已有异步任务接口。

| 方法与路径 | 对接方用途 |
| --- | --- |
| `POST /api/v1/agent/analyze` | 提交固定仓库、head commit、构建配置与幂等键 |
| `GET /api/v1/agent/analyze/{job_id}` | 轮询任务状态 |
| `GET /api/v1/agent/analyze/{job_id}/result` | 读取外层结果及版本信息 |
| `GET /api/v1/agent/analyze/{job_id}/artifacts` | 下载原生产物包 |

当前 C-BUG-Patch 实际 POST 的字段为 `repository_url`、`commit`、`build_profile`、`idempotency_key`。其中 commit 对应待检视 PR 的 head。base、PR diff 和改动目标保留在对接方，用于关联 PR 及本地查询。

### 返回文件

对接方消费 `manifest.json`、`graph.json` 和以下原生产物。

- `external/call-chains.json`
- `external/resource-flow.json`
- `external/sync-relations.json`
- `external/race-analysis.json`

外层导出继续使用既有 1.0 格式。两个新增并发字段位于 race-analysis 中。当前 Schema 检查支持这些扩展，字段含义与内部版本需要双方确认后继续维护。

外层 result、manifest、图及导出文件中的仓库、commit、run_id 和构建配置须相互一致。C-BUG-Patch 会校验绑定及文件内容哈希。

第一组原有 race 分类保留在其原生文件中。当前对接方消费其事实与证据，专项结论由 Reviewer 产生。

## 五、测试与部署状态

新增回归文件包括 `test_ast_fallback.py`、`test_snapshot_identity.py`、`test_concurrency_facts.py` 和 `test_semantic_sync.py`。

| 验证 | 实际结果 |
| --- | --- |
| 新增 AST 语义回归 | 22 项通过 |
| 最新完整上游套件 | 127 项，123 通过、2 失败、2 跳过 |
| 共同锁、不同锁、一侧缺锁的真实取证联调 | 候选数分别为 0、1、1 |
| 两个正例的检视、行为测试、补丁及 Docker 验证 | 均完成，修复后候选数为 0 |
| bounded 语义代码的生产 GitHub 下载 runner 复验 | `not_run` |
| Redis/libuv 整库能力验收 | `not_run` |

两个已记录失败分别为 `external_function` 类型节点计数 `2 < 7` 和原报告链 `report_ready=false`。11 月 12 日合并后的完整套件再次执行，结果仍为 123 通过、2 失败、2 跳过，详见[最新合并复测](../project-change-records/2026-11-12-32-recode-upstream-merge-review-regression.md)。

### 两项失败的成功标准及已确认现象

成功标准来自第一组自带的 `clang_pipeline/tests/test_pipeline.py`。Git 记录可追溯至 `1dbcca63`。

| 失败项 | 测试输入及成功标准 | 本次已确认的失败点 |
| --- | --- | --- |
| `test_external_function_calls_are_preserved` | 使用第一组 `clang_pipeline/tests/fixtures`，要求七个外部调用目标名称存在，且至少有七个 `external_function` 类型节点 | 七个名称的检查通过，类型计数为 2，低于预期 7 |
| `test_seven_stages_produce_evidence_graph` | 使用第一组 `demo/sample`，要求 `report_ready=true`，调用边确认比例至少为 0.9，并检查后续报告产物 | 实际在 `report_ready` 断言处失败，后续断言受该失败中断 |

第一个样本中的 `external_flow.c` 自身定义了三个本地辅助函数，内部调用七种外部 API，分别为 `open`、`close`、`getaddrinfo`、`freeaddrinfo`、`pthread_mutex_lock`、`pthread_cond_wait` 和 `pthread_mutex_unlock`。预期数字 7 对应这七种外部 API。该输入与本项目四文件联调仓库各自独立。

源码依据分别位于 `clang_pipeline/tests/fixtures/external_flow.c`、`clang_pipeline/tests/test_pipeline.py:170` 和 `clang_pipeline/tests/test_pipeline.py:182`。当前明确的是外部函数节点类型计数不符合预期。节点分类及其与编译器环境的关系仍需定位。

第二项的就绪条件来自 `clang_pipeline/stages.py:1448`，要求已确认调用边占全部待验证调用边的比例至少 90%，并且全部检查记录中没有 `refuted`。该比例描述分析器内部调用边验证状态。`report_ready=false` 对应的具体未确认边或被否定检查仍需进一步定位。

“既有失败”指 11 月 11 日本地联调已记录同名失败。引入原因还需要对原始版本、对接修改和编译器环境做隔离对照。

### 完整闭环的量化结果

11 月 11 日最终两个四文件构造 PR 共检视两个目标竞态，两个目标均得到 `probable / defect`。两份实际补丁均一次生成，并通过各自冻结行为测试、全程序编译和 smoke。健康对照、原缺陷、修复版本各执行六次，结果分别为六次通过、六次命中目标断言、六次通过，共十八次行为执行。

样本内目标检出为 2/2，批准验证计划下的补丁通过为 2/2，均为 100%。两个样本属于同一计数器竞态家族。详细分母、逐例结果、计时范围及角色适配记录见[完整闭环量化记录](../project-change-records/2026-11-11-31-recode-review-test-repair-full-flow.md#完整闭环量化指标)。

最终本地镜像标签为 `cbug/recode-semantic:20261111-bounded`，解析 ID 为 `sha256:bc75917d21dd90e38fb15ae9e56fb026034b2556abcb1b92401e8843b2e343ec`。镜像中三个分析模块的哈希已与本地源码核对一致。

部署时需要构建包含新增 Python 模块的分析镜像，宿主侧服务代码同步纳入 `sandbox.py` 等兼容修改。镜像解析 ID 的使用方式应与部署端当前配置校验规则一致。

## 六、与第一组对齐的事项

- 确认 `concurrency_facts`、`semantic_sync` 的归属、字段语义和版本维护方式。
- 确认固定仓库、commit、构建配置及 run_id 在内外层产物中的一致性约定。
- 确认词法观察、AST 证明及覆盖缺口的表达，尤其是缺口时必持锁和排除关系的处理。
- 确认 `snapshot_identity` 的受信调用入口及 tmpfs 结果导出方案。
- 在第一组部署配置下构建新镜像，复验默认 GitHub 下载任务到结果发布的链路。
- 单独排查两个原报告链回归失败，并商定后续复杂 C/C++ 分析的优先级。

## 配套文档与证据

- [C-BUG-Patch 侧修改汇总](2026-11-11-c-bug-patch-side-changes.md)
- [现有对接与启动说明](recode.md)
- [最终闭环报告](../project-change-records/2026-11-11-31-recode-review-test-repair-full-flow.md)
- [最终证据清单](../project-change-records/2026-11-11-recode-full-flow-final-evidence/manifest.json)

完整闭环使用本地验收 runner、真实 HTTP 服务组件和固定镜像。两个四文件构造 PR 的详细版本、模型角色适配记录、冻结测试和补丁均保存在上述证据中。
