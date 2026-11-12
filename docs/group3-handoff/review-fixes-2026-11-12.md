# 第一组反馈的三项修复

日期为 2026-11-12，目标分支为 `group3`。

## P1 join 输出参数及局部抽象值

`semantic_sync.py` 原先记录创建/join 生命周期，join 第二个参数对应的内存副作用遗漏。

现在在成功 join 的路径上定位第二个参数指向的对象。全局对象导出写访问，位置及证据绑定实际 join 调用。写入时将已等待线程从活动集合移除，其他活动线程继续参与并发关系判断。

局部目标的抽象值更新为 unknown，后续条件分支保留两种可能。分析器目前以 unknown 表达线程返回内容。失败路径的局部结果值也保守失效。空输出参数保留原有处理，无法解析输出身份时记录 `unresolved_join_output` 覆盖缺口。

新增真实 Clang 回归覆盖共享 `pthread_join` 输出与另一线程读取、`thrd_join` 更新后的局部分支、pthread 局部指针结果及无法解析的输出参数。

## P1 冻结版本及历史 baseline 归属

原冻结记录绑定旧版 `race_detector.py`，group3 引入新导出后与记录不一致。按照仓库版本规则，新分析器进入 `external-v2`。

- 原 v1 锁、27 份冻结文件和原始 baseline 字节保存在 `validation/external/history/external-v1/`。
- 归属记录指向上游 `f14e4da3f872e0d24bb24389e3e8fc88c49b8a28`，保留原锁摘要及逐文件哈希。
- 六份选择清单保留原有哈希，Gold 身份继续沿用固定输入。
- 当前 `protocol-lock.json` 绑定 `external-v2`，覆盖 32 份文件，包含新增 AST、词法事实及原生导出模块。
- v2 文本哈希明确采用 LF 换行规范化，支持 Windows CRLF 检出，历史 v1 归档保留原始字节。
- 新预测、报告和状态输出到 `validation/external/results/external-v2/`。
- 新 baseline 记录位于 `history/external-v2-baseline.json`，提交前状态为 `not_run`。
- baseline 运行器先检查锁，再执行固定子集，完成后写入本次版本的记录。
- 默认预测验证要求当前协议；历史预测可显式使用归档 v1 Schema 验证。

外部基准 CI 的上传范围指向 v2 结果和对应冻结元数据。后续运行的结果以该提交关联的 Actions 产物为依据。

## P2 commit SHA 规范化

增加 `normalize_commit_sha`，对完整十六进制 SHA 统一使用小写。任务提交在校验及幂等指纹计算前完成规范化，下载 URL、直接 runner、容器入口、快照身份、图、manifest 和外层结果使用同一值。

容器结果发布前，核对外层结果、manifest 和 graph 中的 commit 与请求一致。

新增回归覆盖大小写请求复用同一任务、下载 URL 规范化、直接 runner 与容器入口，以及真实 Clang 流水线生成的结果/图/manifest 三方绑定。

## 本地验证

使用 Python 3.12。Clang 相关测试在固定基础镜像中读取本次源码副本执行。

| 检查 | 结果 |
| --- | --- |
| join、SHA、安全与任务定向回归 | 48/48 通过，包含实际 Clang AST 和流水线 |
| validation 验证测试 | 40/40 通过 |
| 当前冻结记录校验 | 32 份文件通过，宿主与 Linux 容器均检查 |
| 历史归档与选择清单哈希 | 通过 |
| 新分析模块改动的冻结拒绝回归 | 通过 |
| v2 单例 CG-Bench 生成、版本及评分冒烟 | 通过 |
| 完整 Clang 14 上游套件 | 136 项，132 通过、2 失败、2 跳过 |

两项完整套件失败继续为外部函数类型节点计数和 `report_ready`，详细日志保留。本地完整外部基准执行状态为 `not_run`，单例冒烟记录只用于接口检查。

原 Regression 工作流在基准复现阶段发现的保存产物差异，继续作为单独问题记录。

- [本地验证汇总](test-results/review-fixes-20261112/result.json)
- [48 项定向测试](test-results/review-fixes-20261112/targeted-docker-final.log)
- [40 项验证测试](test-results/review-fixes-20261112/validation-tests-release.log)
- [完整上游日志](test-results/review-fixes-20261112/full-upstream.log)
- [历史冻结来源](../../validation/external/history/external-v1/attribution.json)
