# 项目工作约定

- 2026-11-12 默认生产 HTTP→持久队列→Docker→S3 验收脚本与 CI 已加入，运行与边界见 docs/production-entry-acceptance.md；实际通过状态以该提交 CI 证据为准。

- 项目地址：https://github.com/yeteng101/REcode
- 用户于 2026-11-04 确认的近期任务见 [生产化路线图](docs/production-roadmap.md)。后续规划和开发先读取该文档。
- 近期主线为验证集之外的生产化与分析能力提升；不要自行把扩充验证集作为下一步主任务。
- 2026-11-04 已实现生产任务系统基础：SQLite 持久化队列、租约恢复、幂等、有限重试、File/S3 对象存储与过期清理。部署范围与限制见 docs/production-jobs.md。
- 2026-11-04 已实现第二、三项基础：默认 Docker 沙箱、安全归档与命令策略、持久遥测、Dashboard/Trace、Prometheus、告警状态和审计。边界、运行方法和验收缺口见 docs/security-observability.md；本机没有 Docker，两项真实容器测试尚待目标 Linux/CI 执行。
- 2026-11-06 已完成生产任务系统与沙箱安全的基础实现并通过本机核心测试；两项真实 Docker 测试仍需 Linux 目标环境执行。
- 当前下一阶段主线：可观测性生产化、发布工程、Agent 产品质量、产品闭环。
- 实际 Docker、S3/MinIO、告警通道验收作为可观测性与发布工程的前置条件；用户鉴权/限流纳入产品闭环。
- 本地闭环和离线 baseline 通过不等于生产可上线；路线图的完成状态以明确记录为准，不能把后续待办视为已经实现。
