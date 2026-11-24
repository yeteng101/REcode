# REcode 同步分析的显式库入口

- 基线 `group3` 的 `3ae5a438eb8f239d778312a8a4edf249767c1f78`
- 工作分支 `library-sync-entry`
- 状态 显式入口验收通过，2026-11-24

## 已确认的问题

HPR-048 的 libuv head 固定在 `10902ab616058198067d6c2cc414014d36f86fff`。按库构建选取编译单元时，语义同步分析找不到 `main`，记录 `unique_main_entry_unavailable` 并跳过执行路径分析。前一轮的原生产物和缺口见[打包验收复查](pinned-checkout-2026-11-24.md)。

REcode 的 CLI `--entry` 与 HTTP `entry_symbols` 已传入分析请求，也已被 Memory 路径分析使用。本次将同一请求参数接到语义同步分析。实现范围限于选择入口及其覆盖记录。对象与字段绑定、事件循环建模、原子操作和内存序由当前模型处理。

## 入口规则与产物

| 输入 | 预期 |
| --- | --- |
| 有唯一 `main` | 沿用现有程序入口及线程跟踪，即使同时传入 `entry_symbols`。 |
| 无 `main`，显式符号唯一解析到 C 函数定义 | 从该定义开始执行现有分析。重复的同名请求只分析一次。多个有效显式入口分别建立根上下文。 |
| 无 `main` 且没有可用入口 | 保留 `unique_main_entry_unavailable`。显式符号未找到或有歧义时，额外记录对应符号。 |
| 有多个 `main` | 保留现有诊断。 |

库入口参数按未知值处理。原生语义同步产物记录实际分析的入口和源码位置。库的外部调用方式仍未知，覆盖为 `partial`，已经观测到的访问和线程事件可以随产物返回。缺少依据时不标记库上下文为不可重入，也不产生互斥或禁止并发的证明。

## 验收计划

1. 在无 `main` 的小型 C 库上对比未指定入口和显式指定入口，检查后者能导出已有模型支持的访问及直接线程事实。
2. 回归未知或歧义符号、重复入口和普通 `main` 路径。检查各入口上下文身份以及 `partial` 下的结论限制。
3. 经正常 CLI 入口验证 `--entry` 到 `external/race-analysis.json` 的传递。对 HPR-048 的固定源码进行静态复查，报告所选入口、实际导出的访问和仍保留的覆盖缺口。
4. 跑现有语义同步和原生导出回归，将环境、命令范围、实际通过项及已有失败记录在此。外部评测锁与当前 `group3` 已存在差异，入口验收和外部基准重新冻结各自记录。

## 实现与验证记录

`native_export.py` 将工作区请求的 `entry_symbols` 传给 `semantic_sync.py`。无 `main` 时只执行唯一匹配且有函数体的 C 入口，保留符号缺失与歧义诊断。`external/race-analysis.json` 的 `semantic_sync.entry_points` 记录选中入口的名字、节点 ID 和源码位置。库调用范围保持 `coverage.complete=false`，缺口记为 `external_library_callers_unmodeled`。同一入口的可重入性和不同入口的执行顺序继续保持未知。

| WSL Debian 验证，Python 3.13.5 / Clang 19.1.7 | 实际结果 |
| --- | --- |
| 语义同步和 Memory 路径定向回归 | 31 项通过，覆盖显式入口、无入口、歧义、重复入口、普通 `main` 和直接线程事实。 |
| CLI 对照 | 同一小型 C 库缺省入口时零共享对象，显式 `--entry library_tick` 后导出 `counter`，覆盖仍为 `partial`。 |
| Clang 全套 | 147 项，144 通过、2 跳过、1 失败。基线同环境 144 项，141 通过、2 跳过、1 失败。两轮失败均为 `test_seven_stages_produce_evidence_graph` 的 `report_ready` 断言。 |
| HPR-048 真实源码静态验收 | 固定 head 的 libuv 库构建共 70 个编译单元，70 个 AST 读取成功。同一组 AST 分别运行缺省入口及显式 `uv_async_send`。选中入口后，原生结果记录 `src/unix/async.c:92`，`unique_main_entry_unavailable` 消失。 |

HPR-048 在选中入口后仍导出零个语义共享对象，覆盖为 `partial`。实际执行路径新增了未解析内存引用、成员表达式、循环和原子表达式的诊断。此轮验收到入口选择和第一组静态导出，第三组候选与后续修复阶段未重新运行。

本地证据位于 `D:/AgentProject/scratch/recode-library-sync-entry-validation/`，包括 `baseline-suite.log`、`updated-suite.log`、`cli-entry-summary.json` 和 `hpr048-entry-summary.json`。第三组自动提交目前只传仓库、SHA 和构建配置，联调时可直接通过 REcode CLI 或现有 HTTP `entry_symbols` 验证显式入口。
