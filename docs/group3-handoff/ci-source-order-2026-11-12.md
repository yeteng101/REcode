# 同步验证工作流的源码准备顺序修复

日期为 2026-11-12，提交分支为 `group3`。

## 问题与定位

提交 `4a90ddc` 触发的 Regression 工作流在 `sync-validation` 任务中失败。32 条 libuv 同步标注已通过完整性检查，后续 35 项验证测试有一项失败。

失败项为 `validation/tests/test_simulated_review.py` 中的 `test_category_decision_counts`，Redis 的 `Confirmed with condition` 记录预期为 4，实际为 0。

该测试通过源码文件、行号和片段校验证据。工作流当时先下载 libuv，运行全部验证测试，之后才下载 Redis。测试执行时 Redis 源码缺失，相应记录进入 `Needs fix`。

## 修改

在 `.github/workflows/regression.yml` 的 `jobs.sync-validation.steps` 中，将 Redis 固定版本下载与 SHA 校验移到验证测试之前。原来的下载及复现组合步骤拆为独立下载步骤和基准复现步骤。

当前顺序如下。

```text
下载并核对 libuv → 下载并核对 Redis → 标注及测试校验 → 基准复现 → 质量门槛
```

## 本地验收

使用 Python 3.12，分别核对以下固定源码版本。

- libuv `8fb9cb919489a48880680a56efecff6a7dfb4504`
- Redis `d2c8a4b91e8c0e6aefd1f5bc0bf582cddbe046b7`

| 检查 | 结果 |
| --- | --- |
| `check_sync_validation.py` | 32 条标注，verified，errors 为空 |
| `unittest discover -s validation/tests -v` | 35 项全部通过 |
| 原失败的分类计数测试 | 通过 |

本地记录覆盖原失败步骤。提交后的 GitHub Actions 结果由对应提交的工作流记录提供。

- [汇总结果](test-results/ci-source-order-20261112/result.json)
- [标注校验报告](test-results/ci-source-order-20261112/sync-validation-report.json)
- [35 项测试日志](test-results/ci-source-order-20261112/validation-tests.log)
