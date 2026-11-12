# 第一组上游更新合并与真实检视复测

日期为 2026-11-12。REcode 本地 `main` 已快进到 `f14e4da3f872e0d24bb24389e3e8fc88c49b8a28`，此前基线为 `e6e3b4222715cea71c150f0dd824ed990e7f567d`。

## 完成的任务

- 拉取第一组最新提交，合并外部基准失败审计脚本及报告。
- 备份并逐文件校验 13 个本地对接文件，合并冲突数为 0。
- 基于合并工作区构建分析镜像，重新执行三个四文件构造 PR 的真实 HTTP 取证和专项准备。
- 为两个正例分别调用注册 `lock_race_reviewer`，校验并收集真实模型结果。
- 修正影响范围 Schema 对已有 `coverage.local_limits` 字段的遗漏，加入回归及负向载荷检查。
- 保存合并记录、框架回归、原生分析、Reviewer 结果及独立审计证据。

## 第一组这次新增了什么

最新提交为 `Audit external benchmark failure cases`，涉及五个文件。

| 文件 | 上游新增或更新内容 |
| --- | --- |
| `scripts/audit_external_failures.py` | 从冻结基准的预测、Gold 和可用工具事实中整理具体失败及能力分类 |
| `validation/external/results/failure-audit.json` | 结构化失败诊断记录 |
| `validation/external/results/failure-audit.md` | 可阅读的诊断报告 |
| `validation/external/README.md` | 审计脚本用途及运行说明 |
| `README.md` | 补充审计入口 |

上游提交的报告统计为 CG-Bench 41 个 unknown 调用点、DataRaceBench 6 个 unknown 案例、CallGraphBench 13 条遗漏调用边，以及 MemSafety 8 个误报。这些数字来自第一组提交的诊断产物。

新脚本的语法检查通过。本轮重新生成外部基准失败报告的状态为 `not_run`。Gold 及参考答案仅保留在上游诊断材料中，本轮 Reviewer 输入使用新提取的候选事实和源码。

此次更新范围集中在基准诊断。合并记录核对了上游修改路径，五个路径均位于分析模块目录以外。

## 本地对接修改如何保留

合并前保存了 tracked diff、13 个本地文件的字节副本及 SHA-256。Git 执行 fast-forward 合并后，逐一核对这 13 个文件的哈希。

原有 AST 分析、并发事实、版本绑定、Clang 兼容及 Docker 产物导出修改均在当前工作区中。原始备份位于 `runs/recode-upstream-merge-20261112/before-merge`，合并与哈希记录已纳入本页证据清单。

## 实际检视结果

三个样本均为明确构造的完整四文件、两提交 C 仓库。真实 REcode HTTP Handler、SQLite 队列和对象存储参与任务交接，本地验收 runner 在新固定镜像中执行 Clang/native 分析。

| 场景 | 新样本 head | 候选数 | 实际模型复核 | 收集状态 |
| --- | --- | --- | --- | --- |
| 两边使用共同锁 | `926ed8a5ddf406e50c3dcf5ee955b13c8c3f6751` | 0 | 确定性排除后结束 | completed |
| 一侧缺锁 | `22c1e66409b78c14d8e00511e1361dcf3c900d17` | 1 | probable / defect | completed |
| 两边使用不同锁 | `e0afc7adb828e08dd89fda306cf06ac76b8eff0a` | 1 | probable / defect | completed |

三组调用与同步覆盖均为 complete。两个正例分别创建一次注册的锁竞态 Reviewer，使用各自新 head 及新图快照生成的保存请求。宿主保存实际返回 JSON，正常协调器入口对版本、候选、证据、覆盖及输出格式进行验收。

Reviewer 对两个候选都确认了同一普通全局对象的冲突写入、线程可能重叠、缺少共同保护，以及 PR 引入的同步变化。状态遵循候选请求的 `max_status=probable` 上限。

## 独立审计发现并修正的契约遗漏

原有影响范围生成器已经返回 `coverage.local_limits`，包含调用深度上限、截断边数量、前 128 个边 ID 和完整边 ID 集合哈希。影响范围独立 Schema 缺少该字段，首次审计因此拒绝输出。

修改 `v2-concurrency/schemas/lock-race-impact-scope.schema.json`，为该已有字段加入明确的对象结构。

- 深度及截断数量采用非负整数。
- 边 ID 列表最多 128 项且唯一。
- 集合哈希采用 64 位小写十六进制字符串。
- 子对象必需字段明确，额外字段继续拒绝。
- 字段整体保持可选，兼容历史影响范围记录。

`v2-concurrency/scripts/test_race_pair_correctness.py` 新增回归，核对实际生成字段与 Schema 定义。独立审计还检查六种非法载荷，包括负深度、错误数量类型、重复边 ID、非法哈希、未知字段及缺失必需字段，均应被拒绝。

首次审计错误、修正前 Schema 及修正后 Schema分别保存。实际分析事实、候选请求和模型结果按原始字节验收。

## 回归结果

| 检查 | 结果 |
| --- | --- |
| 合并前后 13 个本地文件哈希 | 一致 |
| 初次主项目离线回归 | 314 项及 smoke 通过 |
| Schema 修正后主项目离线回归 | 315 项及 smoke 通过 |
| 三个样本的 HTTP 与已有产物入口 | 通过 |
| 两次真实专项 Reviewer 及正常收集入口 | 通过 |
| 独立原生及专项 Schema 校验 | 28 份通过 |
| `local_limits` 非法载荷拒绝 | 6 种通过 |
| REcode 完整容器回归 | 127 项，123 通过、2 失败、2 跳过、0 错误 |

上游两项失败与此前记录一致，分别为 `test_external_function_calls_are_preserved` 的 `external_function` 类型节点计数不足，以及 `test_seven_stages_produce_evidence_graph` 的 `report_ready=false`。完整套件状态为 failed。

### 两项断言的来源与定位边界

两项成功标准来自第一组已有的 `clang_pipeline/tests/test_pipeline.py`，Git 记录可追溯至 `1dbcca63`。

- 外部函数测试使用第一组自带 fixtures。`external_flow.c` 包含三个本地辅助函数及七种外部 API 调用。测试先核对七个目标名称，再要求至少七个 `external_function` 类型节点。本次名称检查通过，失败发生在类型计数 `2 < 7`。当前确认的是节点类型计数不符合预期，具体分类原因仍待定位。
- 报告测试使用第一组 `demo/sample`。`report_ready` 的实现条件为已确认调用边比例至少 90%，并且检查记录中没有 `refuted`。当前日志确认该标记为 false，具体触发条件仍待定位。

两个失败样本与本项目四文件联调仓库各自独立。“既有”指 11 月 11 日本地联调已经记录同名失败，引入原因需要进一步进行版本和环境隔离对照。完整说明见[第一组对齐文档](../integrations/2026-11-11-recode-side-changes.md#两项失败的成功标准及已确认现象)。

新分析镜像为 `cbug/recode-semantic:20261112-merged`，解析 ID 为 `sha256:72e94d1851afe57935cefc3a56e817e990fa51ff10b5b90ea92b76c65a6eeb0c`。镜像内八个对接及分析模块逐一与当前源码哈希核对。

## 验证范围与记录

本轮验证范围为第一组取证、公共层、Concurrency 候选及真实专项检视。新测试作者、产品补丁生成和修复 Docker 验证状态为 `not_run`。此前第 31 阶段的完整修复记录继续独立保留。

大型 Redis/libuv 仓库能力评测、新语义代码的默认生产下载 runner 复验，以及新外部基准审计报告重生成的状态均为 `not_run`。

- [最终合并与检视证据清单](2026-11-12-recode-upstream-merge-final-evidence/manifest.json)
- [前一阶段完整修复闭环](2026-11-11-31-recode-review-test-repair-full-flow.md)
- [第一组侧修改汇总](../integrations/2026-11-11-recode-side-changes.md)
- [本项目侧修改及请求链](../integrations/2026-11-11-c-bug-patch-side-changes.md)
