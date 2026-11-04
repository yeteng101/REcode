# 持久化生产任务系统

本阶段使用 SQLite WAL 持久化队列，替代进程内 ThreadPoolExecutor。面向**单主机、多个 API/worker 进程**；数据库必须放在本机持久卷，不支持 NFS/SMB 数据库或多主机共享 SQLite。多主机扩容需要后续迁移数据库/队列适配层。

## 运行

2026-11-04 后续更新：服务现已默认启用 Docker 沙箱，启动前必须配置 `CRA_SANDBOX_IMAGE`（digest 固定镜像）。下面的任务目录/worker 配置仍适用；镜像构建与运行、安全约束、Dashboard 见 [沙箱与可观测性](security-observability.md)。无 Docker 的本地调试必须显式启用该文档中的双开关开发模式。

合并模式保留原入口，默认运行 2 个 worker：

```powershell
$env:CRA_JOB_ROOT = 'D:\recode-data\jobs'
$env:CRA_OBJECT_ROOT = 'D:\recode-data\objects'
python -m clang_pipeline.agent_server --host 127.0.0.1 --port 8090
```

拆分 API 和 worker（两个终端配置相同的持久目录和存储后端）：

```powershell
# API 进程
$env:CRA_MAX_WORKERS = '0'
python -m clang_pipeline.agent_server

# 另一终端启动 worker
python -m clang_pipeline.job_worker --workers 2

# 单次过期清理；正常 API/worker 也会自动定期清理
python -m clang_pipeline.job_worker --cleanup-only
```

服务器默认禁止本地仓库路径；开发环境需显式设置 `CRA_ALLOW_LOCAL_REPOSITORIES=1`。这不代表已经实现沙箱或完整的宿主机访问隔离。

## 持久化与恢复语义

- SQLite 事务提交成功后才返回 HTTP 202。状态、请求参数、重试预算、重试时间、取消请求和生命周期事件均持久化；进程内只保留执行线程及取消令牌。
- 原子领取任务，心跳续租；默认租约 30 秒。进程崩溃后租约到期，存活或重启的 worker 会自动重新领取。API-only 进程不会执行任务，必须有 worker 才会恢复执行。
- 每次领取使用唯一令牌；过期 worker 不能覆盖当前任务状态。每次执行使用独立目录和对象键，避免旧执行污染新结果。
- 执行语义为 **at-least-once**，不是 exactly-once；自定义 runner 的外部副作用也必须幂等。已有状态接口的 queued/running/succeeded/failed/cancelled/timed_out 保留，新增 cancelling/expiring/expired。
- 正常关闭会停止领取、发出协作取消，并将未完成任务交还队列；崩溃恢复同样消耗执行次数。默认最多 3 次尝试，包括首次执行。
- 仅 retryable 的分析异常、超时及对象上传失败自动重试；普通内部异常不自动重试。使用指数退避，最多 3600 秒；取消不重试。
- 原分析器工作目录也放入执行目录；默认关闭其重复 ZIP 打包，统一导出结果对象。旧版本内存中丢失的任务无法追溯恢复。
- schema 版本为 1；未知版本拒绝启动。备份使用 SQLite backup API 或关闭全部进程后备份数据库及其持久目录，不应只复制运行中的 `.sqlite3` 文件忽略 WAL。

## 幂等提交和结果下载

```http
POST /api/v1/agent/analyze
Idempotency-Key: client-request-001
Content-Type: application/json

{"repository_url":"https://github.com/example/project.git","commit":"fixed-commit"}
```

也支持请求体 `idempotency_key`。相同键和相同规范化参数返回原任务；相同键配不同参数返回 HTTP 409。不带键则创建新任务。键最长 200 字符，不能只含空白；当前键在整个服务内唯一，未来多租户鉴权必须增加租户作用域。需要可复现结果时固定 commit；分支会随时间变化。

- `GET /api/v1/agent/analyze/{job_id}`：持久状态、执行次数、生命周期事件、错误与对象描述。
- `POST /api/v1/agent/analyze/{job_id}/cancel`：取消，可由另一 API/worker 进程处理。
- `GET /api/v1/agent/analyze/{job_id}/result`：对象存储中的结果 JSON。
- `GET /api/v1/agent/analyze/{job_id}/artifacts`：tar.gz 包，包含报告、JSON、HTML 和当前执行的工具 Trace；排除 workspace、checkout、.git 和符号链接。

成功结果仅在对象写入后发布。失败执行会尽力保存错误 JSON 与 Trace。重试的执行目录/对象保留到任务过期，最终下载接口指向最后一次已发布的对象。状态中的本地路径为兼容字段，跨进程调用方应使用下载接口。

## 对象存储

默认 FileObjectStore 在独立对象目录原子落盘，适合持久卷部署。S3/MinIO 后端需安装：

```powershell
python -m pip install -r requirements-jobs-s3.txt
$env:CRA_OBJECT_BACKEND = 's3'
$env:CRA_S3_BUCKET = 'recode-results'
$env:CRA_S3_PREFIX = 'recode'
# 可选，仅在 S3 兼容服务需要时设置
$env:CRA_S3_ENDPOINT_URL = 'https://object-storage.example.com'
```

凭证由 boto3 默认凭证链提供，不写入队列或代码。bucket 需预先创建，并允许上传、读取、列举前缀及删除对象。建议为未完成的 multipart upload 配置 bucket 生命周期清理。

## 过期清理

默认终态保留 7 天，每 60 秒检查。到期先标记 expiring 禁止新下载，再删除任务的全部对象和工作目录，最后标记 expired。失败会留在 expiring 并在下一轮重试；已部分删除可重复执行。保留最小任务元信息和幂等键墓碑，防止客户端旧请求再次触发执行；墓碑不自动删除。删除文件不等于 SQLite 文件立即收缩，数据库备份、压缩和墓碑长期归档需按运营策略管理。

清理与刚开始的下载可能相遇，调用方应在到期前下载并保存所需产物。长时间失去租约、又不遵守协作取消的自定义 runner 仍可能写入旧目录；可靠强制终止依赖下一阶段的进程/容器沙箱。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| CRA_JOB_ROOT | artifacts/agent-jobs | 持久任务工作根目录 |
| CRA_JOB_DATABASE | JOB_ROOT/jobs.sqlite3 | SQLite 文件 |
| CRA_MAX_WORKERS | 2 | API 进程内 worker 数，0 为仅 API |
| CRA_JOB_MAX_ATTEMPTS | 3 | 首次执行加重试的总次数 |
| CRA_JOB_RETRY_DELAY_SECONDS | 5 | 首次重试退避基数 |
| CRA_JOB_LEASE_SECONDS | 30 | worker 租约 |
| CRA_JOB_TIMEOUT_SECONDS | 1800 | 每次执行的协作超时 |
| CRA_JOB_RETENTION_SECONDS | 604800 | 终态结果保留时长 |
| CRA_JOB_CLEANUP_INTERVAL_SECONDS | 60 | 自动清理间隔 |
| CRA_OBJECT_BACKEND | file | file 或 s3 |
| CRA_OBJECT_ROOT | JOB_ROOT/objects | 文件对象存储目录 |
| CRA_S3_BUCKET | 无 | S3 bucket |
| CRA_S3_PREFIX | recode | S3 对象键前缀 |
| CRA_S3_ENDPOINT_URL | SDK 默认 | S3 兼容 endpoint |
| CRA_ALLOW_LOCAL_REPOSITORIES | 0 | 服务器本地路径开关 |

API 和 worker 必须指向一致的数据库、工作根目录、对象存储及租约/保留策略。请使用专用持久目录；任务执行目录属于服务管理的临时产物。

## 验证范围

专项测试覆盖持久化重启、真实进程 kill 后恢复、领取互斥、租约隔离、幂等并发冲突、重试预算/退避、跨进程管理器取消、对象上传故障、过期删除及删除失败恢复、HTTP 状态与下载。S3 适配通过替身客户端测试，尚未连接实际 S3/MinIO 服务做集成验证。

本阶段完成生产任务系统基础；沙箱与可观测性已由后续更新实现基础（目标环境验收待做）；用户鉴权、限流和渐进发布仍是后续工作，不构成可对公网开放的上线结论。

### 本次本地验证（Windows / Python 3.12.1）

- 任务系统、持久化和 HTTP 专项：25 个测试全部通过。
- call_chain_demo 回归：11 个测试全部通过。
- Python 编译检查与 Git diff 空白检查通过。
- 完整 clang_pipeline 回归首轮共 59 项：46 通过、8 因缺少 clang/clang++ 等工具跳过、5 失败。随后新增的 3 个队列用例包含在上述 25 项专项通过结果中。
- 5 个失败分别为 test_nl_repo 的绝对路径提取，以及 test_repo_agent 的 CMake 回退、C++ URL、file URL、仓库文件限额用例。已将 HEAD 基线导出到临时目录并复跑同样 5 项，全部同样失败；与本次修改无关，原因是既有 Windows 路径处理和缺少 CMake。未为使本次测试通过而跳过或改写这些既有测试。
