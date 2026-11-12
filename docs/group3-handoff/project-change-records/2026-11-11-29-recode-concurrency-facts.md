# 第一组并发事实补充

日期为 2026-11-11。两个仓库的本轮修改保留在本地工作区。

## 任务与结果

给 REcode 补充带源码来源的对象声明、线程创建与等待、具体加解锁调用，并让公共层和锁竞态专项候选准备消费这些信息。

| 部分 | 本轮实现 |
| --- | --- |
| REcode 原生导出 | 新增 `race-analysis.concurrency_facts`，外层版本仍为 1.0 |
| 对象声明 | 简单全局标量或锁变量的候选声明、类型、位置、链接属性与 evidence |
| 执行来源 | pthread/thrd 创建、join、直接入口函数、句柄表达式与 evidence |
| 锁操作 | API、加锁或解锁、具体表达式、候选声明、所属函数与 evidence |
| C11 参数 | 修正 `thrd_create` 入口函数使用第二个参数 |
| 公共层适配 | 读取新记录、校验来源及引用、补充访问上下文，保留旧导出兼容 |
| 证据缺口 | 分别记录别名、线程实例重叠、路径敏感锁集缺口 |

## 分析边界

新增提取基于源码词法观察。简单声明提供候选对象位置，置信度上限为 0.65，整体保持 `formal_evidence=false`。

- 局部同名变量和形参可能遮蔽全局变量，检测到歧义时保留空绑定。
- 不同文件内的同名声明分别记录，跨文件 extern 与别名仍待解析。
- 创建与 join 记录保留源码顺序，具体线程实例和可能重叠关系均为 unknown。
- 锁操作提供函数内的调用位置，条件分支、循环及调用失败路径仍需 CFG 分析。
- 必持锁集保持空值并明确标记 unknown，保护判断继续受覆盖检查约束。
- 原始访问仍沿用上游现有提取，锁参数中的名字出现等情况需要 AST 绑定进一步区分。

## 实际验证

### 框架回归

`runs/recode-facts-regression-20261111/result.json` 记录 293 项通过，smoke 通过。各子套件分别为 common 39、memory 4、concurrency 94、PR 适配及路由 12、Concurrency 脚本 144。使用 Python 3.12.10，离线运行记录的外部网络尝试为 0。

REcode 的新增事实与既有 race detector 定向测试共 13 项通过，其中新增测试 8 项。Windows Python 3.12 与 Docker Python 3.12 各运行一次，日志分别为 `producer-windows.log` 和 `producer-docker.log`。

新增适配器回归 6 项，覆盖新事实传递、证据引用校验、位置不一致、身份冲突、未知版本，以及词法信息保持原有证据强度。

独立确定性审计使用本地 Schema 校验 18 份原始导出与专项产物，通过固定 head 校验及三个变体共 9 名候选的保守状态检查。结果位于 `runs/recode-facts-audit-20261111/result.json`。远程 Schema 获取关闭。

本轮 22 份源码、结果与日志快照保存在 [证据清单](2026-11-11-recode-concurrency-evidence/manifest.json)，逐文件 SHA-256 已核对。前一轮证据目录保留原样。

### 真实 HTTP 与 Docker 联调

各变体使用四文件、两提交 Git 样本，两个修改文件。真实 REcode HTTP 服务、SQLite 队列和对象存储使用本地验收 runner，在 Docker 中执行 Clang 与原生分析。HTTP 消费与已有产物目录消费均通过公共入口，Memory 和 Concurrency 路由均准备成功，随后进入 Concurrency 候选准备。

| 变体 | 声明数 | 创建与 join 事件 | 锁操作数 | 候选数 | 候选状态 |
| --- | --- | --- | --- | --- | --- |
| 双方使用同一锁 | 2 | 4 | 4 | 3 | needs_more_context |
| 双方使用不同锁 | 3 | 4 | 4 | 3 | needs_more_context |
| 一侧未加锁 | 2 | 4 | 2 | 3 | needs_more_context |

记录目录分别为 `runs/recode-facts-common-lock-20261111`、`runs/recode-facts-distinct-locks-20261111`、`runs/recode-facts-missing-lock-20261111`。每个目录保存 summary、原始导出、公共上下文、候选和请求。

源代码是明确构造的联调输入，第一组响应由真实分析生成。整体仍为 partial。同一锁场景也保留未知候选，因为当前分析尚未证明线程重叠及访问时的必持锁集。

### 镜像与运行记录

新镜像标签为 `cbug/recode-facts:20261111`，实际解析 ID 为 `sha256:ed00270b614a6ef1d40b091202be414bedb6cd7b007e77f23e3e9d4832673f37`。基础镜像解析 ID 为 `sha256:ecbd38ee8dcada16fcebc2aba0cd0b8e1ff92839dd75605d8e995e747af19af3`。

首次联调因非 root 镜像对 `/work` 临时目录无写权限而停止，记录保留于 `runs/recode-facts-integration-20261111`。验收 runner 将自身临时目录配置为 mode 1777 后复验通过，仍保持容器只读根目录、禁网及能力限制。

首次容器单元测试入口因镜像构建规则排除了测试目录而导入失败。后续将测试目录只读挂载后，13 项全部通过。

## 验证状态

| 项目 | 状态 |
| --- | --- |
| 框架回归与 smoke | passed |
| REcode 定向测试 | passed |
| 三种本地样本的实际 HTTP 与 Docker 事实传递 | passed |
| 新代码的默认生产下载服务复验 | not_run |
| 本轮模型 Reviewer 复核 | not_run |
| 本轮 Patch Worker 生成补丁及产品测试 | not_run |
| 完整上游回归重新执行 | not_run |

此前上游完整回归的两个已知失败继续按前一轮记录保留。本轮定向通过只覆盖此处列出的范围。

## 后续所需能力

需要以 AST 绑定对象引用，再对创建成功与 join 路径建立线程生命周期关系，并对 CFG 传播具体锁状态。这样才能在候选层证明并发关系和共同保护，减少当前保留的未知候选。
