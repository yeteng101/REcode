# C-BUG-Patch 公共层对接

## 当前交接入口

2026-11-12 的 group3 交接包见[修改说明与测试结果](group3-handoff/README.md)。其中包含第一组与 C-BUG-Patch 两侧的独立修改说明、两例真实修复闭环的量化结果，以及最新上游合并后的检视复测。

以下内容保留各实施阶段的记录，当前验收范围及已知失败以交接包中的日期和证据清单为依据。

对接方使用 Python 3.12 调用异步任务服务，并消费 manifest、graph 和原生外部导出。适配器位于相邻仓库 `C-BUG-Patch/v2-common/recode_client.py` 与 `recode_artifacts.py`。

## 本轮兼容修改

- `stages.py` 在可选 libclang API 加载失败时使用已有编译器 JSON AST 路径，图元数据记录实际分析器。
- `run_repo_analysis` 接收 safe-mode 归档的固定身份，将仓库和完整 commit 传递给图、原生导出、manifest 和结果。
- `sandbox_entry.py` 将下载入口绑定的 GitHub 仓库和 commit 传入该身份参数。
- `sandbox.py` 在运行中的容器内通过 Python tar 流导出结果，支持 Docker Desktop 的运行时 tmpfs。导出排除 workspace、checkout 和 .git，并执行原有配额与归档校验。
- 缺失产物会记录实际路径与异常，Docker 可执行文件缺失单独分类。

## 返回数据约束

对接方校验固定 SHA、仓库、构建配置和 run_id。固定 commit 的外层结果与内层导出必须一致。默认服务配置为 `safe-generic`。

调用图、资源与同步操作提供事实和证据。对接方仅使用 race-analysis 中的原始访问，分类结论由专项 Agent 复核。对象声明、线程实例、路径敏感锁集、锁序和别名不足会保留为 partial。

### 并发事实补充

`race_detector.py` 调用 `concurrency_facts.py`，为每个源文件导出 `concurrency_facts`。记录包含简单全局声明、pthread/thrd 创建和 join、锁 API 位置，以及与原始访问的关联。所有声明和调用位置均有 evidence，随原有原生导出与 HTTP 产物传递。

此能力基于源码词法观察。声明匹配是候选关联，别名及跨文件绑定保持缺口。线程创建位置保留实际入口表达式，线程实例与重叠关系为 unknown。锁操作保留具体表达式，must-lockset 为 unknown。C-BUG-Patch 适配器保留这些证据强度约束。

同时修正 `thrd_create` 的入口参数索引，使用第二个参数识别入口函数。

Python 3.12 定向回归共 13 项通过，其中 8 项覆盖本轮新增事实。相同 13 项在 Docker 内通过。四文件、两提交样本的共同锁、不同锁、一侧无锁三个变体经实际 HTTP 服务和 Docker 原生分析进入公共层与专项候选准备，候选均保持 needs_more_context。

新增字段保持外层 schema_version 1.0，部署时须重建分析镜像。本轮新代码的默认生产下载服务复验状态为 not_run。

## 已知验证结果

### 对象绑定、线程生命周期与锁集分析

`semantic_sync.py` 在受支持的 C 程序路径中使用完整 Clang JSON AST，按声明身份绑定全局对象和简单指针别名，沿直接调用传播锁状态，记录线程创建与 join 成功/失败路径。

`native_export.py` 将结果写入 `race-analysis.semantic_sync`，内部版本为 `bounded-ast-sync/1`。声明、访问、锁操作、线程事件和直接调用都带源码 evidence。覆盖不完整时保留具体缺口，停止输出必持锁和线程排除证明。

本轮实现范围为有唯一 main 入口的 C 程序、全局标量、简单别名、结构化分支、直接创建的线程和默认 POSIX 锁。循环、递归、复杂间接调用、聚合对象和 atomic 协议等仍需补充。C++ 使用原有适配路径并记录当前分析范围缺口。

22 项新增语义回归通过。完整上游回归运行 127 项，123 项通过、2 项已知失败、2 项 Docker 集成测试跳过。共同锁、不同锁及一侧无锁三种真实 HTTP 与镜像内分析联调分别产生 0、1、1 个竞态候选，两个正例候选均为 ready_for_review。一侧无锁还经过实际锁竞态 Reviewer 与协调器验收，得到 probable / defect。

嵌套多锁和锁序证明在本轮保留明确缺口。最终镜像为 `cbug/recode-semantic:20261111-bounded`，具体代码哈希与绑定记录见相邻仓库的最终证据清单。

完整结果与范围见相邻 C-BUG-Patch 仓库第 30 阶段记录。新生产下载 runner、完整 Redis/libuv 验收及本轮补丁闭环均为 not_run。

本机实际 Docker 隔离测试两项通过。默认任务服务通过 GitHub 获取本项目固定提交并完成分析，C-BUG-Patch 成功消费结果。

Clang 14 环境中的完整上游回归运行 97 项，93 项通过、2 项失败、2 项默认跳过。两个失败分别为外部调用计数不足和 report_ready 为 false。额外配置真实 Docker 镜像后，两项 Docker 测试单独通过。

完整使用说明见 `C-BUG-Patch/docs/integrations/recode.md`，验收与修改清单见其 2026-11-11 对接记录。两个仓库的修改均保留在本地工作区。
