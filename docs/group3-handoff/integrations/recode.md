# REcode 第一组对接

对接基于 `yeteng101/REcode` 的 `e6e3b4222715cea71c150f0dd824ed990e7f567d`。本地源码位于 `D:/JDProject/REcode`，配套修改保留在该仓库工作区。

双方对齐材料分别见[第一组侧修改汇总](2026-11-11-recode-side-changes.md)和[C-BUG-Patch 侧修改与请求链](2026-11-11-c-bug-patch-side-changes.md)。

2026-11-12 本地 REcode 已合并上游 `f14e4da`，保留原有对接修改。三个样本的取证和两个正例的真实专项检视结果见[上游合并复测记录](../project-change-records/2026-11-12-32-recode-upstream-merge-review-regression.md)。

## 调用流程

```text
review-input.v1 固定仓库、head、构建配置和目标
    → RecodeClient 提交一次分析任务
    → REcode SQLite 队列与 Docker 原生分析
    → 轮询任务状态，下载 result 和 artifacts.tar.gz
    → 校验 manifest、graph 和导出文件的版本绑定
    → 转换为既有第一组事实契约
    → 公共层准备 Memory / Concurrency ContextBundle
    → 专项确定性准备，宿主按工作流调用模型角色
```

| HTTP | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/v1/agent/analyze` | 提交固定 commit 的分析任务，附幂等键 |
| GET | `/api/v1/agent/analyze/{job_id}` | 轮询状态 |
| GET | `/api/v1/agent/analyze/{job_id}/result` | 校验外层结果 |
| GET | `/api/v1/agent/analyze/{job_id}/artifacts` | 下载原生事实文件 |

同一客户端实例对同一仓库、版本及配置复用一份快照。调用链、内存和同步查询共享该分析任务。下载文件保存在输出目录的 `recode-artifacts/` 下，记录传输信息和原始字节哈希。

公共层从资源流、共享访问和原生同步关系选择专项。多个共享访问的执行上下文缺失时，Concurrency 保留在路由中。

## 使用已有结果包

输入 `review-input.v1` 沿用既有 PR 适配器。本地源码检出须对应目标 head，且通过 Git 干净状态检查。

```powershell
py -3.12 v2-common/review_pipeline.py `
  --review runs/my-pr/review-input.json `
  --first-group-provider recode `
  --recode-artifacts D:/path/to/recode-output `
  --output-dir runs/my-pr/common
```

`--recode-artifacts` 指向包含 `manifest.json`、`graph.json`、`external/` 的目录。各导出应来自同一次原生分析。

## 使用 REcode 服务

默认 Docker 服务使用 `safe-generic` 构建配置。生成 `review-input.v1` 时将 `repository.build_profile` 设为 `safe-generic`。

```powershell
py -3.12 v2-common/review_pipeline.py `
  --review runs/my-pr/review-input.json `
  --first-group-provider recode `
  --first-group-url http://127.0.0.1:8090 `
  --recode-job-timeout 1800 `
  --output-dir runs/my-pr/common
```

已有任务可以通过 `--recode-job-id job_<32位十六进制>` 获取。客户端仍会核对结果中的仓库、commit、构建配置和图快照。失败、取消、过期和超时会返回明确错误。

## 启动配套服务

先应用本轮 REcode 修改并构建镜像。在 `D:/JDProject/REcode` 中按其 `deploy/sandbox/Dockerfile` 构建，或使用本次证据中的本地构建配方。启动时填写实际镜像 RepoDigest。

```powershell
$env:CRA_JOB_ROOT = 'D:/JDProject/recode-service/jobs'
$env:CRA_OBJECT_ROOT = 'D:/JDProject/recode-service/objects'
$env:CRA_SANDBOX_IMAGE = 'your/recode-image@sha256:<实际digest>'
$env:CRA_SANDBOX_MODE = 'docker'
py -3.12 -m clang_pipeline.agent_server --host 127.0.0.1 --port 8090
```

本次验收镜像为 `cbug/recode-integration@sha256:9b1dcd6519a72c2717f9e37a506a2594c7dd1b7568bff177106212444f13f320`。其中包含本轮内层分析代码，宿主侧还需保留 `sandbox.py` 的产物导出修改。

## 事实转换和缺口

| REcode 数据 | 公共层结果 |
| --- | --- |
| graph 的节点、边、call_site、evidence | 目标解析与有深度限制的调用子图 |
| resource-flow 的操作和路径 | resource_flow findings，保留 acquire/release/transfer 等操作 |
| sync-relations 的端点与证据 | observed_relations，供上下文追溯 |
| race-analysis 的原始访问 | 按文件与表达式分组的共享对象访问候选 |
| 对象声明、别名、执行上下文缺失 | 明确 coverage gap，候选保持 needs_more_context |
| protected、atomic、private 提示 | 待复核同步提示，保护关系需要补证 |
| 仓库外位置 | 省略相应记录，保存省略数量与记录 ID |

第一组的 race/no_race 分类和原因不会进入专项结论。旧导出和词法观察路径声明 `formal_evidence=false` 并保留 `partial`。支持范围内的完整 AST 路径使用确定性证据标记，具体范围由 scope 与 coverage 限定。

### 2026-11-11 基于 AST 的并发分析

新镜像通过 `race-analysis.semantic_sync` 返回声明引用绑定、跨文件 extern、简单指针别名、线程创建/join 路径和访问点锁集。外层协议继续使用 1.0。

分析覆盖完整时，调用与同步查询可以返回 succeeded，专项候选可以进入 ready_for_review。共同保护和成功 join 形成的明确排除关系可以在候选准备时生效。缺口和截断继续使结果保持 partial。

当前实现面向有 main 入口的封闭 C 程序和默认 POSIX 线程/锁操作。循环、复杂回调、聚合对象访问、栈对象跨线程、嵌套多锁、atomic 和 C++ 等范围保留明确缺口。完整说明和真实验证见 [第 30 阶段记录](../project-change-records/2026-11-11-30-recode-bounded-semantic-analysis.md)。

部署应重新构建分析镜像。本轮最终镜像为 `cbug/recode-semantic:20261111-bounded`，解析 ID 为 `sha256:bc75917d21dd90e38fb15ae9e56fb026034b2556abcb1b92401e8843b2e343ec`。

### 2026-11-11 补充并发事实

本地 REcode 新增 `race-analysis.concurrency_facts`，外层协议继续使用 1.0。适配器将这些记录放入同步结果的 `source_observations`，同时补充访问的声明位置、线程创建来源及同函数锁操作。旧导出仍可读取。

| 信息 | 当前返回内容 | 仍待补充的证明 |
| --- | --- | --- |
| 共享对象 | 简单全局变量的候选声明、类型、链接属性、源码位置与 evidence | AST 引用绑定、别名、跨文件 extern 解析 |
| 执行上下文 | pthread/thrd 创建与 join 位置、直接入口函数、句柄表达式 | 创建成功路径、具体线程实例、生命周期是否重叠 |
| 锁保护 | 加解锁 API、锁表达式、候选锁声明、所属函数、源码 evidence | 访问发生时各路径上的必持锁集、锁初始化状态、锁序 |

导出器采用源码词法观察，声明候选置信度上限为 0.65。适配器保持 `must_lockset=[]` 和未知状态，并在候选中传播来源。源码出现加锁调用时，路径敏感的保护判断仍需进一步分析。

本轮镜像 `cbug/recode-facts:20261111` 的解析 ID 为 `sha256:ed00270b614a6ef1d40b091202be414bedb6cd7b007e77f23e3e9d4832673f37`。构建基于本地 `cbug/libuv-build:bookworm`，生产服务部署时需构建包含这些修改的新镜像。

完整改动与实测范围见 [并发事实补充记录](../project-change-records/2026-11-11-29-recode-concurrency-facts.md)。

对象声明允许缺失，专项记录 `object_declaration_unavailable`，可以继续准备取证请求。未知或歧义目标保留在 `unresolved_targets`。

## 复现联调

小型联调使用四个源码文件、两个提交、两个修改文件。真实 HTTP 服务组件通过本地验收 runner 执行 Docker Clang 分析，检查公共层两个入口和专项交接。

```powershell
py -3.12 v2-common/verify_recode_integration.py `
  --recode-root D:/JDProject/REcode `
  --image cbug/recode-semantic:20261111-bounded `
  --image-code --checked-thread-results --lock-variant missing `
  --output runs/recode-integration-new
```

默认服务验收通过 GitHub 下载固定公开提交，执行配置生成的 Docker runner，再查询三类事实。

```powershell
py -3.12 v2-common/verify_recode_service.py `
  --recode-root D:/JDProject/REcode `
  --image cbug/recode-integration@sha256:9b1dcd6519a72c2717f9e37a506a2594c7dd1b7568bff177106212444f13f320 `
  --repository yeteng101/REcode `
  --commit e6e3b4222715cea71c150f0dd824ed990e7f567d `
  --function main --file solution.cpp `
  --output runs/recode-service-new
```

选择新输出目录可以保留已有记录。模型检视和产品修复继续使用各专项已有工作流。

小型联调支持 `--lock-variant common`、`distinct` 和 `missing`。配合上面的 AST 镜像和线程返回值检查，支持范围内的三种场景分别产生 0、1、1 个候选。

## 检视、测试与修复实际验收

2026-11-11 已用两个四文件构造 PR 完成真实第一组取证、公共层、Reviewer、测试作者、Patch Worker、stdio MCP 和 Docker 闭环。健康对照各三次退出 0，原缺陷各三次触发行为断言 42，修复后同一冻结测试各三次退出 0。全程序编译及 smoke 均通过，重新提取修复源码后候选数为 0。

PR 输入保留 base/head，修复请求在创建 worktree 前检查完整 base。生成测试采用宿主固定的 `c_source` 模板。宿主依据编排器 `next_action` 调用模型，MCP 执行确定性提交和推进。

测试作者使用当前项目角色配置的显式 `worker` 适配，其中一例在文件保存后遇到安全中断，由宿主记录并恢复报告。Reviewer 和 Patch Worker 使用已注册专项角色。大型库及生产下载 runner 的本轮复验状态为 `not_run`。详细范围、已知问题、真实补丁和证据见[第 31 阶段记录](../project-change-records/2026-11-11-31-recode-review-test-repair-full-flow.md)。
