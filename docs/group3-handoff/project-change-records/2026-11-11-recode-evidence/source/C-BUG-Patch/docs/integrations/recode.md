# REcode 第一组对接

对接基于 `yeteng101/REcode` 的 `e6e3b4222715cea71c150f0dd824ed990e7f567d`。本地源码位于 `D:/JDProject/REcode`，配套修改保留在该仓库工作区。

## 调用流程

```text
review-input.v1 固定仓库、head、构建配置和目标
    → RecodeClient 提交一次分析任务
    → REcode SQLite 队列与 Docker 原生分析
    → 轮询任务状态，下载 result 和 artifacts.tar.gz
    → 校验 manifest、graph 和导出文件的版本绑定
    → 转换为既有第一组事实契约
    → 公共层准备 Memory / Concurrency ContextBundle
    → 专项确定性准备，宿主按工作流调用模型角色
```

| HTTP | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/v1/agent/analyze` | 提交固定 commit 的分析任务，附幂等键 |
| GET | `/api/v1/agent/analyze/{job_id}` | 轮询状态 |
| GET | `/api/v1/agent/analyze/{job_id}/result` | 校验外层结果 |
| GET | `/api/v1/agent/analyze/{job_id}/artifacts` | 下载原生事实文件 |

同一客户端实例对同一仓库、版本及配置复用一份快照。调用链、内存和同步查询共享该分析任务。下载文件保存在输出目录的 `recode-artifacts/` 下，记录传输信息和原始字节哈希。

公共层从资源流、共享访问和原生同步关系选择专项。多个共享访问的执行上下文缺失时，Concurrency 保留在路由中。

## 使用已有结果包

输入 `review-input.v1` 沿用既有 PR 适配器。本地源码检出须对应目标 head，且通过 Git 干净状态检查。

```powershell
py -3.12 v2-common/review_pipeline.py `
  --review runs/my-pr/review-input.json `
  --first-group-provider recode `
  --recode-artifacts D:/path/to/recode-output `
  --output-dir runs/my-pr/common
```

`--recode-artifacts` 指向包含 `manifest.json`、`graph.json`、`external/` 的目录。各导出应来自同一次原生分析。

## 使用 REcode 服务

默认 Docker 服务使用 `safe-generic` 构建配置。生成 `review-input.v1` 时将 `repository.build_profile` 设为 `safe-generic`。

```powershell
py -3.12 v2-common/review_pipeline.py `
  --review runs/my-pr/review-input.json `
  --first-group-provider recode `
  --first-group-url http://127.0.0.1:8090 `
  --recode-job-timeout 1800 `
  --output-dir runs/my-pr/common
```

已有任务可以通过 `--recode-job-id job_<32位十六进制>` 获取。客户端仍会核对结果中的仓库、commit、构建配置和图快照。失败、取消、过期和超时会返回明确错误。

## 启动配套服务

先应用本轮 REcode 修改并构建镜像。在 `D:/JDProject/REcode` 中按其 `deploy/sandbox/Dockerfile` 构建，或使用本次证据中的本地构建配方。启动时填写实际镜像 RepoDigest。

```powershell
$env:CRA_JOB_ROOT = 'D:/JDProject/recode-service/jobs'
$env:CRA_OBJECT_ROOT = 'D:/JDProject/recode-service/objects'
$env:CRA_SANDBOX_IMAGE = 'your/recode-image@sha256:<实际digest>'
$env:CRA_SANDBOX_MODE = 'docker'
py -3.12 -m clang_pipeline.agent_server --host 127.0.0.1 --port 8090
```

本次验收镜像为 `cbug/recode-integration@sha256:9b1dcd6519a72c2717f9e37a506a2594c7dd1b7568bff177106212444f13f320`。其中包含本轮内层分析代码，宿主侧还需保留 `sandbox.py` 的产物导出修改。

## 事实转换和缺口

| REcode 数据 | 公共层结果 |
| --- | --- |
| graph 的节点、边、call_site、evidence | 目标解析与有深度限制的调用子图 |
| resource-flow 的操作和路径 | resource_flow findings，保留 acquire/release/transfer 等操作 |
| sync-relations 的端点与证据 | observed_relations，供上下文追溯 |
| race-analysis 的原始访问 | 按文件与表达式分组的共享对象访问候选 |
| 对象声明、别名、执行上下文缺失 | 明确 coverage gap，候选保持 needs_more_context |
| protected、atomic、private 提示 | 待复核同步提示，保护关系需要补证 |
| 仓库外位置 | 省略相应记录，保存省略数量与记录 ID |

第一组的 race/no_race 分类和原因不会进入专项结论。转换结果声明 `formal_evidence=false`，专项保留该证据级别。当前导出缺少完整对象身份、must-lockset 和锁序证明，转换结果保持 `partial`。

对象声明允许缺失，专项记录 `object_declaration_unavailable`，可以继续准备取证请求。未知或歧义目标保留在 `unresolved_targets`。

## 复现联调

小型联调使用四个源码文件、两个提交、两个修改文件。真实 HTTP 服务组件通过本地验收 runner 执行 Docker Clang 分析，检查公共层两个入口和专项交接。

```powershell
py -3.12 v2-common/verify_recode_integration.py `
  --recode-root D:/JDProject/REcode `
  --image cbug/libuv-build:bookworm `
  --output runs/recode-integration-new
```

默认服务验收通过 GitHub 下载固定公开提交，执行配置生成的 Docker runner，再查询三类事实。

```powershell
py -3.12 v2-common/verify_recode_service.py `
  --recode-root D:/JDProject/REcode `
  --image cbug/recode-integration@sha256:9b1dcd6519a72c2717f9e37a506a2594c7dd1b7568bff177106212444f13f320 `
  --repository yeteng101/REcode `
  --commit e6e3b4222715cea71c150f0dd824ed990e7f567d `
  --function main --file solution.cpp `
  --output runs/recode-service-new
```

选择新输出目录可以保留已有记录。模型检视和产品修复继续使用各专项已有工作流。
