# 第一组对象绑定、线程生命周期与锁状态分析

日期为 2026-11-11。本轮代码保存在 REcode 与 C-BUG-Patch 本地工作区。

## 已实现的任务

- 对象绑定使用 Clang 的声明引用，关联全局变量、跨文件 extern 和简单指针别名，并区分局部遮蔽及各文件的静态对象。
- 执行上下文使用线程创建位置和成功路径建立实例，分析创建与 join 的成功、失败分支，记录访问间可能重叠或明确排除的关系。
- 锁状态在结构化执行路径上逐步传播，经过直接函数调用传递，访问点记录必持锁集与可能持锁集。
- 分支中同一访问的必持锁取交集，可能持锁取并集，未知调用和预算截断保留缺口。
- 公共层消费新分析结果，按具体覆盖状态返回 succeeded 或 partial，旧原生导出继续使用兼容路径。
- 专项识别明确的线程排除关系，同时将正反关系冲突保留为未知。

## 具体实现位置

| 仓库 | 文件 | 职责 |
| --- | --- | --- |
| REcode | `clang_pipeline/semantic_sync.py` | 声明索引、别名绑定、结构化路径、直接调用传播、线程生命周期、锁状态 |
| REcode | `clang_pipeline/native_export.py` | 复用本次编译配置与完整 JSON AST，将分析结果和 evidence 写入原生导出 |
| REcode | `clang_pipeline/tests/test_semantic_sync.py` | 使用真实 Clang AST 的 22 项定向回归 |
| C-BUG-Patch | `v2-common/recode_artifacts.py` | 校验分析版本、翻译单元哈希、证据和对象/锁/上下文引用，接入覆盖状态 |
| C-BUG-Patch | `v2-concurrency/prototype/analyze_sync_flow.py` | 消费明确的不可并发关系，冲突关系返回未知 |
| C-BUG-Patch | `v2-common/verify_recode_integration.py` | 构造有线程返回检查的输入，使用实际镜像分析代码运行三种联调变体 |

外层第一组协议继续使用 1.0。新增原生字段为 `race-analysis.semantic_sync`，内部标记 `bounded-ast-sync/1`。完整 JSON AST 可用时直接复用，紧凑 AST 缺少存储类别等字段时，通过原有编译命令重新获取完整 JSON AST。

## 判定范围

当前支持有唯一 main 入口的封闭 C 程序。主线程通过直接调用创建的线程、全局标量对象、简单局部/参数指针别名、默认 POSIX mutex/rwlock，以及结构化条件分支属于本轮范围。

线程 API 同时保留成功与失败路径。验收样本在 create/join 返回失败时退出当前流程，使访问关系可以在完整路径上确定。忽略失败结果、使用未知句柄等情况可能保留缺口。

互斥锁使用已知默认初始化状态。静态初始化可直接识别，默认属性动态初始化按成功和失败路径分别传播。未知属性、未证明初始化、错误释放和重复获取等情况保留缺口。

以下情况继续保留为 partial，包括循环、递归与调用深度超限、复杂间接回调、结构体字段与数组索引、atomic 协议、嵌套线程创建、线程捕获栈对象、符号别名、嵌套多锁和未建模的外部函数。C++ 继续使用原有适配路径并记录当前模型的范围缺口。

本轮重点是数据竞态取证。嵌套多锁的锁序证明尚需补充，分析器会记录 `nested_lock_order_analysis_unavailable`，使覆盖状态保持 partial。

当前默认预算为每步最多 64 条路径、调用深度 12、语句步数 50,000。分析出现缺口时清空必持锁证明，并停止输出新的线程排除证明。完整状态的含义限定在上面的分析范围内。

## 真实联调结果

三种变体各使用四个源码文件、两次 Git 提交、两个修改文件。第一组响应通过真实 HTTP Handler、SQLite 队列与对象存储传递。验收 runner 使用新镜像内部分析代码，对明确构造的源代码执行 Clang 与原生分析。

| 场景 | 原来词法事实路径 | 新分析路径 |
| --- | --- | --- |
| 双方使用同一锁 | 3 个 needs_more_context 候选 | 0 个竞态候选，覆盖检查通过 |
| 双方使用不同锁 | 3 个 needs_more_context 候选 | 1 个 ready_for_review 候选 |
| 一侧无锁 | 3 个 needs_more_context 候选 | 1 个 ready_for_review 候选 |

词法路径的旧记录位于第 29 阶段。新样本补充了线程 API 失败退出分支，因此此表展示阶段能力与输入条件，速度和准确率对照需要固定同一输入另行测量。

最终三组记录位于 `runs/recode-semantic-bounded-common-20261111`、`runs/recode-semantic-bounded-distinct-20261111` 和 `runs/recode-semantic-bounded-missing-20261111`。公共层、已有产物入口和 Concurrency 候选准备均执行成功。

本轮还实际调用锁竞态 Reviewer 复核一侧无锁候选，并通过协调器的 JSON、版本、证据和覆盖验收。初次记录位于 `runs/recode-semantic-missing-20261111/concurrency/coordinator-result.json`，在 hardened 镜像阶段的新 head 上再次复核并验收，记录位于 `runs/recode-semantic-acceptance-missing-20261111/concurrency/coordinator-result.json`。两次均返回 probable / defect，版本和证据绑定分别独立保存。

之后新增嵌套多锁缺口保护并构建 bounded 镜像，三种最终场景再次得到 0、1、1 个候选。该阶段沿用上面已完成的专项复核记录，同时单独保留三组新 head 的候选准备结果。

## 测试结果与已知失败

主项目完整回归最终为 305 项及 smoke 通过，使用 Python 3.12。记录位于 `runs/recode-semantic-regression-final-20261111/result.json`。

REcode 完整回归在可写的容器临时副本运行，共 127 项。其中 123 项通过、2 项失败、2 项跳过，错误数为 0。本轮新增语义分析测试 22 项全部通过。日志为 `runs/recode-semantic-regression-20261111/upstream-full-04.log`。

两个失败与此前基线一致，分别为 `test_external_function_calls_are_preserved` 的外部调用计数为 2、预期至少 7，以及 `test_seven_stages_produce_evidence_graph` 的 report_ready 为 false。新增语义分析使用独立的 AST 直接调用记录，上游原报告链的这两个失败继续保留。

首轮完整回归的测试运行环境还出现了只读 demo 无法写入上下文和缺少 scripts 模块的问题。后续在容器临时目录复制必要文件后重新运行，最终错误数为 0。所有旧日志保留。

## 运行镜像与复现

最终镜像标签为 `cbug/recode-semantic:20261111-bounded`，解析 ID 为 `sha256:bc75917d21dd90e38fb15ae9e56fb026034b2556abcb1b92401e8843b2e343ec`。基础镜像沿用本地 Python 3.12 / Clang 14 环境，禁网运行分析。

最终审计使用本地 Schema 检查 22 份产物，校验三个最终 head 与实际镜像内部的分析源码哈希。24 份源码、结果和日志快照见 [最终证据清单](2026-11-11-recode-semantic-final-evidence/manifest.json)。较早的 hardened 阶段审计和证据清单继续保留，Reviewer 结果所绑定的 head 在清单中独立记录。

```powershell
py -3.12 v2-common/verify_recode_integration.py `
  --recode-root D:/JDProject/REcode `
  --image cbug/recode-semantic:20261111-bounded `
  --image-code --checked-thread-results --lock-variant missing `
  --output runs/recode-semantic-new
```

`--lock-variant common` 与 `distinct` 分别执行健康共同锁和不同锁场景。输出目录应使用新目录以保留原记录。

## 验证状态

| 项目 | 状态 |
| --- | --- |
| 22 项实际 Clang AST 语义回归 | passed |
| 公共层、专项与其他框架回归 | passed |
| 三种场景的实际 HTTP、镜像内分析与候选准备 | passed |
| 一侧无锁的真实专项 Reviewer 与结果验收 | passed |
| 上游完整套件 | failed，保留两个已知失败及两个跳过 |
| 本轮 Patch Worker 与行为回归闭环 | not_run |
| Redis/libuv 完整大型仓库能力验收 | not_run |
| 新代码的 GitHub 下载生产 runner 复验 | not_run |

这次验证支持上述范围内的真实取证与专项检视。大型库中的复杂入口、循环、别名、平台配置和同步协议仍需要后续能力与实际样本验收。
