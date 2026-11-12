# 默认生产入口验收

## 2026-11-12 实际验收结果

实现提交 `e9b31eabe33803aaac3f63078802c3046d800a0c` 的
[Linux CI 运行 43187356251](https://github.com/yeteng101/REcode/actions/runs/43187356251)
已成功，含真实容器边界、完整分析和默认生产 HTTP/Docker/S3 验收。
产物 `production-entry-evidence` 中 `evidence.json` 的状态为 `passed`，记录：

- 排队后 API 重启及幂等检查通过。
- 真实 Docker 分析、S3 对象及各层 JSON 身份检查通过。
- 完成后 API 再次重启，下载字节/哈希保持一致。
- 自动过期后 S3 对象删除，幂等 tombstone 保留。

同提交的 Python regression、Neo4j E2E 通过；原有 sync-validation
`test_category_decision_counts` 仍为 `0 != 4`，因此不能声称整个项目 CI 全绿。
本次没有改动该验证集或 Gold。

## 复现方法

运行入口：`python scripts/accept_production_entry.py --output /tmp/recode-acceptance-UNIQUE`。
需要 Linux Docker、Python 3.12、`requirements-jobs-s3.txt`，以及可访问公开 GitHub 的宿主网络。
输出目录必须不存在；不要使用生产数据库或对象前缀。脚本使用独立任务目录，S3 使用专用测试桶/前缀。

必需环境变量：

- `CRA_SANDBOX_TEST_IMAGE`：已构建的 `image@sha256:...` 沙箱镜像。
- `CRA_OBJECT_BACKEND=s3`、`CRA_S3_ENDPOINT_URL`、`CRA_S3_BUCKET`、`CRA_S3_PREFIX`。
- 标准 AWS SDK 凭证及区域配置；桶需事先创建。

脚本启动真实 API 和独立 Worker，不设置 development 模式，不 mock 下载、分析器或存储。
输入是固定公开 GitHub 提交：
`cbitosc/CBIT-Hacktoberfest-Hackathon-Hello-World@4125ced214965f0bd3715fb696d68ee1017116f8`。
这里只分析其 C 源码，不执行仓库脚本，不新增评测 Gold。

验收断言包括：

1. 默认拒绝本地路径和宿主问答。
2. API 无 Worker 时持久排队；API 重启后任务及幂等键保留，冲突返回 409。
3. 独立 Worker 经默认 Docker 沙箱完成真实分析，通过真实 S3 发布结果。
4. HTTP 下载与 S3 对象逐字节一致；实际 main 节点、HTML、报告及 Trace 存在。
5. result、manifest、graph、四类 external JSON 的提交、仓库、run_id 和安全 profile 一致。
6. 完成状态在 API 重启后保留，下载哈希一致。
7. 30 秒保留期后自动删除 S3 对象，下载不可用，幂等 tombstone 不重新执行。

成功或失败均写 `evidence.json` 和子进程日志；失败退出非零，不允许缺 Docker/S3 时跳过。
CI `.github/workflows/sandbox.yml` 构建 digest 镜像、启动临时 MinIO，执行该脚本并上传证据。
MinIO 从上游固定提交 `0d7408fc9969caf07de6a8c3a84f9fbb10a6739e`、Go 1.24.2
和只读模块锁构建，不依赖已无法公开拉取的上游容器镜像；仅用于 CI 测试服务。
现有 Docker 边界测试继续检查无网络、无挂载、只读根目录、用户身份和资源限制。

兼容修复参考第三组分支：tmpfs 从容器内导出、归档身份绑定、可选 libclang 回退；
主线另补大写 SHA 归一化及真实 Clang 的产物一致性回归。
第三组并发分析及冻结协议问题见 https://github.com/yeteng101/REcode/issues/1 ，不在本次合并范围。

此验收覆盖默认服务入口的基础闭环；不等同于真实下游 C-BUG-Patch 联调、
Worker 执行中强杀恢复、外部告警通道、鉴权限流或全生产上线批准。
重试、租约恢复和取消仍由持久任务系统回归覆盖。CI 实际结论以对应提交的运行结果及证据为准。
