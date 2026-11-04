# 沙箱、安全与可观测性

本阶段为路线图第二、三项提供可执行实现。默认服务必须配置 digest 固定的 Linux Docker 镜像；Docker 或镜像不可用时任务失败，不回落为宿主机执行。SQLite 队列仍限定单主机多进程。

## 沙箱运行

在具备 Linux Docker/cgroups 的部署环境，用经过批准的 digest 固定基础镜像构建：

```bash
docker build -f deploy/sandbox/Dockerfile \
  --build-arg BASE_IMAGE='debian:bookworm-slim@sha256:<approved-base-digest>' \
  -t <registry>/recode-sandbox:<version> .
docker push <registry>/recode-sandbox:<version>
# 读取推送后的实际 RepoDigest，提前拉取到 worker 所在 Docker daemon。
export CRA_SANDBOX_IMAGE='<registry>/recode-sandbox@sha256:<actual-image-digest>'
export CRA_SANDBOX_MODE=docker
export CRA_JOB_ROOT=/srv/recode/jobs
export CRA_OBJECT_ROOT=/srv/recode/objects
python3 -m clang_pipeline.agent_server
```

示例中的 digest 必须替换为真实值。运行时 `--pull=never`，不允许任意 tag 或临时拉取不受控镜像。镜像构建使用 apt 仓库当前版本；完整依赖/SBOM/构建可复现性仍归发布工程，不能仅以镜像 digest 宣称供应链工作已完成。

### 执行边界

1. 仅接收 `https://github.com/owner/repository[.git]`，可指定受限分支或完整 40 位 commit。生产路径不接收本地、file、ssh、git、任意 HTTP 地址、URL 凭证、查询参数或自定义 build profile/宿主机日志路径。私有仓库暂不支持。
2. 服务通过固定的 `codeload.github.com` HTTPS 端点读取归档：不使用环境代理，不跟随重定向，拒绝非公网 DNS 结果，并限制下载字节与时间。容器始终 `--network=none`，仓库代码不能用网络拉依赖。下载错误保留为任务失败，不绕过策略。
3. 归档导入在容器内，拒绝绝对路径、`..`、符号链接、硬链接、设备和其他特殊文件；限制归档条目数和展开字节数。不会导入 `.git`、`.ssh`、`.aws`、`.env`、AGENTS.md、CLAUDE.md、`.claude`、`.codex`、Gold 路径。
4. 容器不挂载宿主机目录、Docker socket、密钥或评测集。构建上下文由 `.dockerignore` 排除 validation、测试 fixture、密钥和运行产物；镜像只复制受信任的分析器代码。
5. 容器为非 root，根文件系统只读，去掉全部 Linux capabilities，启用 no-new-privileges，使用 Docker 默认 seccomp；限制 CPU、内存及 swap、进程数、文件描述符，禁止 core dump。`/work`、`/tmp` 为有大小限制的 noexec/nosuid/nodev tmpfs。
6. 不使用仓库里的 CMake、编译数据库或 shell 命令。safe-generic 编译参数由服务代码生成；镜像不安装 git/cmake。Clang/LLVM 只在隔离环境解析源码，不加载仓库提供的插件或响应文件。依赖自定义构建/宏配置的项目可能产生更多 unknown，应在能力指标中如实体现。
7. 分析结果在执行结束后通过有配额的 Docker tar 传输导出；宿主机再次校验条目，不信任容器传回的路径、链接或特殊文件。外层和容器内 Trace 分别保存，避免相互覆盖。
8. 取消/超时/执行结束会强制移除容器，终止其所有后代进程。宿主进程崩溃时容器 PID 1 的独立最长生存时长为配置超时加 30 秒；停止后的残留容器可用标签 `recode.sandbox=true` 由运营定期清理。Docker daemon 故障时强制删除可能失败，会写日志，需运营处理。

Docker daemon、运行内核和镜像内容是受信任基础设施。容器属于操作系统隔离，不能等同于专用虚拟机隔离；更高风险部署可在独立 worker VM 或 gVisor/Kata 环境复用该执行入口。

### Prompt Injection 与旧入口

生产分析链是离线工具流水线，不调用模型，不加载仓库中的 Agent 指令，不向模型开放宿主机工具。系统提示同时明确源码/注释/日志/工具输出为不可信数据，不能改变权限或证据标准；源码读取工具校验真实路径必须位于 repo_root 内。

旧 `/api/v1/agent/ask` 默认返回 403，因为它允许宿主机 workspace 和 Claude Code 等工具，不能伪装成已经隔离。仅开发环境显式同时设置以下两项才可恢复旧的宿主机行为：

```bash
export CRA_ENV=development
export CRA_SANDBOX_MODE=development
```

开发模式不具有生产隔离保证。生产环境的本地仓库开关不能绕过 Docker runner 的 GitHub URL 策略。

| 配置 | 默认 | 说明 |
|---|---|---|
| CRA_SANDBOX_IMAGE | 无，必须配置 | image@sha256:digest |
| CRA_SANDBOX_MODE | docker | 生产隔离模式 |
| CRA_SANDBOX_CPUS | 2 | CPU 配额 |
| CRA_SANDBOX_MEMORY_MB | 2048 | 内存上限，swap 不额外增加 |
| CRA_SANDBOX_DISK_MB | 1024 | /work tmpfs 上限，计入容器内存 |
| CRA_SANDBOX_PIDS | 128 | 进程数上限 |
| CRA_JOB_TIMEOUT_SECONDS | 1800 | 每次任务期限，另有容器独立生存期限 |

源码导入、checkout 副本、分析 workspace 和结果共同占用 /work 配额；超额即失败，不向宿主磁盘溢出。网络下载的压缩归档上限默认 128MiB。

## Dashboard、Trace 和指标

启动服务后访问 `/dashboard`。静态页面无第三方脚本/字体/CDN，带 CSP、no-store 和 nosniff。无内置浏览器工具时本次使用 Playwright Chromium 验证了 1440px 桌面和 390px 手机布局、任务筛选、Trace 筛选、详情展开与无 JS 错误。

- `GET /api/v1/observability`：指标、告警、最近任务、资源采样与审计。
- `GET /api/v1/agent/analyze/{job_id}/trace?offset=0`：全部尝试的工具 Span（每页最多 500）与生命周期事件。前端支持加载更多、按工具/状态筛选及 JSON 详情。运行中的 Span 在结束写入后可见；容器内 Trace 在产物导出后可见。
- `GET /metrics`：Prometheus 文本格式；规则示例在 `deploy/alerts.yml`，需在目标 Prometheus/Alertmanager 中加载并配置通知接收者。本次未向任何外部接收者发送通知。

管理接口默认只允许 loopback 访问。部署时设置 `CRA_ADMIN_TOKEN`，所有指标与 Trace 请求必须带 `Authorization: Bearer ...`；Dashboard 中输入令牌后仅保存在页面内存，不写浏览器存储。应通过 HTTPS 反向代理访问。此处是运维接口保护，不代表已经实现完整用户鉴权、多租户或限流。

### 指标口径与持久化

- 任务成功率：当前保留的 succeeded / (succeeded + failed + timed_out + cancelled)，不含 expired；保留周期改变会影响这个比例。
- 工具失败率与阶段耗时：近 24h 已写入的 Span；error/failed/timed_out/cancelled 计为失败，degraded 单独保留状态。阶段表提供调用数、失败数、平均及总耗时；嵌套阶段不可直接相加为任务总耗时。
- Token：OpenAI Chat/Responses 使用响应 usage，Claude Code 使用返回 usage/total_cost_usd。离线生产分析本身不调用模型，因此没有模型成本是正常状态。无实际 usage 时不估算 token。
- 成本：提供商返回成本优先；否则按 `CRA_MODEL_PRICES_JSON` 配置每模型的输入/输出每百万 token 单价。未配置价格的调用成本为 null，Dashboard 显示已计价调用覆盖率，不能把未知成本算作 0。当前估价未细分缓存或特殊计价 token；准确账单以提供商为准。
- unknown rate：近 24h 各执行尝试导出的 race-analysis 中有 label 的 items，若无 items 则使用顶层 label。unknown / 全部有 label 的判定；不冒充调用图、资源流等所有分析任务的综合 unknown 指标。
- 资源：容器 CPU、内存、进程、BlockIO 的执行后采样以及配置的硬限制。不是峰值或完整时序；失败/失联容器可能无采样，页面显示缺失，不填假 0。
- SQLite 新增 spans、attempts、usage、audit、alert_state 表，兼容现有队列 schema v1。采集由执行收尾及 Trace 查看触发，按 job/attempt/span 去重；进程重启不丢已采集指标。独立清理循环默认每 60 秒评估告警并保留 30 天遥测。
- 审计记录 HTTP 方法、无 query 的路径、状态码，以及提交、取消、执行结果和告警变化；不记录请求体、问答、Authorization 或幂等键。工具 Trace 继续按字段与文本模式脱敏。应用审计不是防篡改 WORM 日志，生产环境仍需外部日志留存策略。

价格示例（仅格式示意，需填入实际采用的模型与单价）：

```bash
export CRA_MODEL_PRICES_JSON='{"your-model":{"input_per_million":1,"output_per_million":2}}'
```

### 告警

告警状态持久化，仅 firing/resolved 变化追加审计，稳定状态不重复发事件。Dashboard 展示全部规则；Prometheus 可通过 recode_alert_active 采集。

| 配置 | 默认 | 含义 |
|---|---|---|
| CRA_ALERT_MIN_JOBS | 5 | 任务比例告警最小样本数 |
| CRA_ALERT_SUCCESS_FLOOR | 0.8 | 任务成功率下限 |
| CRA_ALERT_TOOL_FAILURE_CEILING | 0.2 | 工具失败率上限（至少 5 次调用） |
| CRA_ALERT_UNKNOWN_CEILING | 0.3 | unknown 比例上限（至少 5 个判定） |
| CRA_ALERT_DAILY_COST_USD | 100 | 24h 已计价成本阈值 |
| CRA_ALERT_QUEUE_LIMIT | 20 | 等待任务数量阈值 |
| CRA_ALERT_MEMORY_PERCENT | 90 | 近 24h 最近执行采样的内存占比阈值 |

## 验证与剩余验收

本机没有 Docker：策略、归档、配额、命令注入、防越界、脱敏、故障关闭、遥测、HTTP 测试已执行；真实容器隔离和完整容器分析用例明确跳过。新增 `.github/workflows/sandbox.yml` 在 Linux 构建 digest 固定测试镜像并执行这两项真实测试，必须在部署前通过。

```bash
python3 -m unittest \
  clang_pipeline.tests.test_agent \
  clang_pipeline.tests.test_agent_tool_contracts \
  clang_pipeline.tests.test_agent_jobs \
  clang_pipeline.tests.test_durable_jobs \
  clang_pipeline.tests.test_job_http \
  clang_pipeline.tests.test_security \
  clang_pipeline.tests.test_observability \
  clang_pipeline.tests.test_sandbox_integration -v
```

本地安全/可观测性功能已实现；目标 Docker 环境隔离验收、实际对象存储联调、真实告警通道、用户鉴权/限流和发布工程仍需完成，不能据此直接对公网开放。

### 本次实际执行结果（2026-11-04）

- clang_pipeline 全集：83 项，68 通过、10 跳过、5 既有失败。失败仍为已在原始基线复现的 Windows 路径处理/CMake 缺失 5 项；跳过中 8 项缺少 Clang 工具，2 项缺少 Linux Docker。
- call_chain_demo：11 项全部通过。
- Python compileall、JavaScript node --check、git diff --check 通过。
- Playwright Chromium：1440×1100、390×844，任务状态筛选、Trace 筛选/详情/关闭、无匹配状态、6 项告警展示、资源文本、无 JS 错误及无页面水平溢出通过；截图位于本地可视化目录，不提交为生产数据。检查了页面布局、标题层级、指标字号、配色、表格间距、移动布局与操作状态。ImageGen/内置浏览器工具不可用，因此没有生成或声称对照已批准的视觉概念；采用代码页面与真实浏览器截图核验。
