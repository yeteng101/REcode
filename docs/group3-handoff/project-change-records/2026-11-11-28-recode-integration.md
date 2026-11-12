# 2026-11-11 第一组 REcode 对接验收

## 完成内容

已将 `https://github.com/yeteng101/REcode.git` 拉取到 `D:/JDProject/REcode`，固定下载版本为 `e6e3b4222715cea71c150f0dd824ed990e7f567d`。

梳理了 37 个流水线与服务模块的结构，重点核对 CLI、七阶段 Clang 流水线、原生资源与同步提取、原始访问记录、任务队列、对象存储、默认 Docker runner、HTTP/MCP 与部署文档。

新增 REcode HTTP 客户端和原生结果包适配器，接入 `v2-common/review_pipeline.py` 的正常入口。支持提交、轮询、已有 job_id、结果下载、版本校验和事实转换。同一输入的三类事实复用一份快照。使用方法见 [对接说明](../integrations/recode.md)。

## 两仓库改动

| 仓库 | 文件 | 改动 |
| --- | --- | --- |
| C-BUG-Patch | `v2-common/recode_client.py` | 对接实际异步任务及下载端点 |
| C-BUG-Patch | `v2-common/recode_artifacts.py` | 原生数据转换、版本绑定、证据索引、目标解析和缺口保留，同族资源按独立 flow ID 区分 |
| C-BUG-Patch | `v2-common/review_pipeline.py` | 新增 recode provider、结果包和已有任务入口 |
| C-BUG-Patch | `v2-common/coordinator.py` | 原生同步关系与未知上下文进入保守自动路由，资源流命中时继续考虑 Concurrency |
| C-BUG-Patch | `v2-concurrency/prototype/analyze_sync_flow.py` | 接受缺失声明并记录缺口，保留生产者的非正式证据声明 |
| C-BUG-Patch | 两个 verify_recode 脚本 | 可复现小型联调和默认服务验收 |
| C-BUG-Patch | common/concurrency 新测试 | 覆盖版本混用、产物变动、歧义目标、原始分类隔离、系统头位置、省略声明和非正式证据传播 |
| REcode | `clang_pipeline/stages.py` | libclang 可选 API 不可用时回退到已有编译器 JSON AST，并记录实际分析器 |
| REcode | `repo_agent.py`、`sandbox_entry.py` | 将固定归档的仓库与 SHA 贯穿图、原生导出、manifest 和外层结果 |
| REcode | `sandbox.py` | 用容器内 Python tar 流导出 tmpfs 结果，排除内部 workspace，改善缺失产物错误分类 |
| REcode | 两个新增测试及对接文档 | 验证 AST 回退、固定归档身份边界并说明对接方式 |

## 实际联调结果

### 四文件集成样例

验收输入是明确构造的四文件、两提交仓库，其中两个 C 文件发生修改。通过真实 REcode HTTP Handler、SQLite 队列和对象存储，使用本地验收 runner 在 Docker 中执行真实 Clang 七阶段及原生分析。

产出 1 条资源流、6 条同步关系和原始共享访问。HTTP 与已有结果包两种公共层入口均通过，Memory 与 Concurrency 的 handoff 均准备成功。Concurrency 生成 3 份 `needs_more_context` 请求，保留对象声明、线程身份、别名和锁集缺口。事实由实际分析器生成。

取消专项强制选择后，自动路由仍选择 Memory 和 Concurrency。最终记录位于 `runs/recode-integration-auto-20261111/summary.json`。此前 `-01`、`-02`、`-03` 等失败或中间记录保留，`-04` 已完成首轮通过。

### 默认 Docker 服务

使用配置生成的默认 Docker runner，通过公网 GitHub 获取 REcode 自身的固定提交，完成容器分析、对象发布、HTTP 下载及三类事实转换。

| 项 | 结果 |
| --- | --- |
| 固定提交 | `e6e3b4222715cea71c150f0dd824ed990e7f567d` |
| 构建配置 | `safe-generic` |
| 固定镜像 | `cbug/recode-integration@sha256:9b1dcd6519a72c2717f9e37a506a2594c7dd1b7568bff177106212444f13f320` |
| HTTP 查询 | 三类均通过，复用一份事实快照 |
| 目标解析 | `solution.cpp` 的 `main` 解析成功 |
| 调用图 findings | 1 |
| 资源流 findings | 4 |
| 同步 facts finding | 1 |
| 仓库内证据 | 190 条 |
| 外部或不可用记录省略 | 55 项，保存 ID 和数量 |
| 服务联调用时 | 20.884 秒，包含下载、排队、分析、传输与校验 |

随后使用原提交的干净 detached worktree，以同一结果包运行公共层正式入口。Memory 的 blast-radius 请求准备成功。Concurrency 对显式选定的入口返回 0 个候选与 `partial`，保留未到达共享对象和事实覆盖缺口。该入口选择用于验证集成，不作为 PR 缺陷检出评测。

记录位于 `runs/recode-production-service-20261111-08/` 和 `runs/recode-production-context-20261111/`。

### 回归检查

| 检查 | 结果 |
| --- | --- |
| C-BUG-Patch 全套离线框架回归 | 287 项通过，smoke 通过 |
| Common | 33 项通过 |
| Memory | 4 项通过 |
| Concurrency | 94 项通过 |
| PR adapter/routing | 12 项通过 |
| Concurrency scripts | 144 项通过 |
| 离线 Python 外部连接阻止事件 | 0 |
| REcode 对接相关宿主检查 | 17 项通过 |
| REcode 实际 Docker 隔离检查 | 2 项通过 |
| REcode Clang 14 完整回归 | 97 项中 93 通过、2 失败、2 默认跳过 |

REcode 的两个未通过断言是外部调用数量 `2 < 7`，以及七阶段样例的 `report_ready=false`。错误记录保留在 `upstream-tests-final.log`。两项 Docker 检查配置真实镜像后另行执行并通过。

完整上游回归的初次运行还遇到两个只读目录写入错误，改用容器内可写副本执行后，两项错误消失。

## 实际发现并处理的问题

1. Clang 14 缺少 `clang_getCursorBinaryOperatorKind`，原代码在模块导入时失败。增加兼容回退后，原生分析可以启动。
2. 默认归档流程内层记录 `repository/local`，外层结果显示请求 SHA，导致版本校验拒绝。固定身份参数贯穿各层后，绑定一致。
3. Docker Desktop 的 archive/cp 入口无法读取运行时 tmpfs 中的结果。改为容器内 tar 流后成功导出。
4. 内部 AST 工作区导出到 Windows 长路径时发生文件创建失败。导出与对象包一致地排除内部 workspace 后消除该错误。
5. 结果包含系统头文件的绝对位置。适配器省略对应记录并保存覆盖缺口，继续消费仓库内事实。
6. Windows Git 的换行转换导致强制关闭 autocrlf 的干净检查误判。检出检查使用仓库自身 Git 规则，源码内容仍由现有加载器处理。
7. 资源流命中且共享访问上下文未知时，旧路由可能只选择 Memory。补充原生同步关系与未知上下文的路由信号后，两个专项可以自动交接。

## 覆盖与工作区状态

当前 REcode 导出仍缺少完整对象声明、线程实例、路径敏感 must-lockset、锁序及别名事实。适配器保留这些缺口，事实为 `partial` 且 `formal_evidence=false`。原始 race/no_race 标签不会成为专项 verdict。

本轮完成第一组、公共层与专项准备入口的对接验收。模型 Reviewer、测试作者与 Patch Worker 的新调用、真实缺陷修复正确率评估标记为 `not_run`。既有修复工作流继续接收相同 ContextBundle 及请求契约。

两个仓库的代码和文档均保留在本地工作区。C-BUG-Patch 原有 11 月 6 日修改与证据保留。

配套证据位于 [快照清单](2026-11-11-recode-evidence/manifest.json)，其中记录两仓库源文件、补丁、结果和日志的字节哈希。
