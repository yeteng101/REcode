# 第三组对接代码、修改说明与测试结果

整理日期为 2026-11-12，发布分支为 `group3`。代码基于第一组上游 `f14e4da3f872e0d24bb24389e3e8fc88c49b8a28`，纳入本地完成的事实提取和运行兼容修改。

## 对齐入口

- [第一组 REcode 修改汇总](integrations/2026-11-11-recode-side-changes.md)列出新增模块、修改函数、返回字段、覆盖边界，以及双方待确认事项。
- [C-BUG-Patch 侧修改与当前请求链](integrations/2026-11-11-c-bug-patch-side-changes.md)说明 HTTP 客户端、产物转换、公共层和专项编排的职责。
- [HTTP 接口及使用说明](integrations/recode.md)提供已有结果包、任务服务和分析入口的调用方式。

## 本分支代码

| 范围 | 文件及能力 |
| --- | --- |
| AST 同步分析 | `clang_pipeline/semantic_sync.py`，绑定受支持的对象身份、线程关系和锁状态 |
| 词法观察 | `clang_pipeline/concurrency_facts.py`，保存声明、线程和锁 API 来源及缺口 |
| 原生导出 | `native_export.py`、`race_detector.py`，将新增事实放入原生 JSON 产物 |
| 版本与运行兼容 | `repo_agent.py`、`sandbox_entry.py`、`sandbox.py`、`stages.py` |
| 定向回归 | `test_ast_fallback.py`、`test_snapshot_identity.py`、`test_concurrency_facts.py`、`test_semantic_sync.py` |

C-BUG-Patch 的消费端改动及相应源码快照位于交接材料中，实际联调使用配套的 C-BUG-Patch 工作区。

## 已有量化结果

| 验收范围 | 实际结果 |
| --- | --- |
| 11 月 11 日真实完整闭环 | 两个四文件构造 PR，两个目标检出，两份补丁均一次生成并通过批准验证 |
| 同一冻结行为测试 | 健康对照六次退出 0，原缺陷六次退出 42，修复后六次退出 0 |
| 完整程序构建与 smoke | 两例均通过，验证前后补丁哈希一致 |
| 修复后第一组重新提取 | 两例候选均从 1 降为 0 |
| 11 月 12 日合并后真实检视 | 共同锁产生 0 候选，一侧缺锁和不同锁各产生 1 候选，两次注册 Reviewer 均返回 probable / defect |
| C-BUG-Patch 最终框架回归 | 315 项及 smoke 通过 |
| REcode 完整回归 | 127 项，123 通过、2 失败、2 跳过、0 错误 |

两例完整闭环属于同一计数器竞态家族，样本内检出和补丁验证通过均为 2/2。跨缺陷类型、健康 PR 误报率和大型库修复效果需要独立评测。

完整闭环测试作者采用当前项目角色配置的显式适配，其中一例保存文件后被安全检查中断，由宿主审查并恢复报告。Reviewer 和 Patch Worker 使用注册专项角色。原始角色、执行及恢复记录随证据提供。

两项 REcode 失败的成功标准来自第一组自带测试。外部调用测试已通过七个目标名称检查，失败发生在 `external_function` 类型节点计数 `2 < 7`。报告测试失败于 `report_ready=false`。具体原因待进一步隔离定位，详见第一组侧汇总。

## 阶段文档与证据

- [完整修复闭环与量化指标](project-change-records/2026-11-11-31-recode-review-test-repair-full-flow.md#完整闭环量化指标)
- [上游合并与真实检视复测](project-change-records/2026-11-12-32-recode-upstream-merge-review-regression.md)
- [完整闭环证据清单](project-change-records/2026-11-11-recode-full-flow-final-evidence/manifest.json)
- [合并后复测证据清单](project-change-records/2026-11-12-recode-upstream-merge-final-evidence/manifest.json)

目录同时收录初次 HTTP 对接、词法观察及 AST 分析阶段的相关文档和证据。各份清单保留对应日期、镜像、代码版本及逐文件哈希，状态以对应运行记录为准。

证据中的绝对路径记录当时的运行位置。迁移执行时需要按本机重新准备工作目录、固定镜像及任务输入。目录内 `.gitattributes` 保留证据原始字节。
