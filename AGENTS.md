# 项目工作约定

- 项目地址：https://github.com/yeteng101/REcode
- 用户于 2026-11-04 确认的近期任务见 [生产化路线图](docs/production-roadmap.md)。后续规划和开发先读取该文档。
- 近期主线为验证集之外的生产化与分析能力提升；不要自行把扩充验证集作为下一步主任务。
- 2026-11-04 已实现生产任务系统基础：SQLite 持久化队列、租约恢复、幂等、有限重试、File/S3 对象存储与过期清理。部署范围与限制见 docs/production-jobs.md。
- 2026-11-04 已实现第二、三项基础：默认 Docker 沙箱、安全归档与命令策略、持久遥测、Dashboard/Trace、Prometheus、告警状态和审计。边界、运行方法和验收缺口见 docs/security-observability.md；本机没有 Docker，两项真实容器测试尚待目标 Linux/CI 执行。
- 默认下一项：第四项发布工程，并推进实际 Docker、S3/MinIO、告警通道的环境验收；用户鉴权/限流仍待后续实现。
- 本地闭环和离线 baseline 通过不等于生产可上线；路线图的完成状态以明确记录为准，不能把后续待办视为已经实现。
