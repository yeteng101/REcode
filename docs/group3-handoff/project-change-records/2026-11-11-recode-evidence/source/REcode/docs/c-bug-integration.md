# C-BUG-Patch 公共层对接

对接方使用 Python 3.12 调用异步任务服务，并消费 manifest、graph 和原生外部导出。适配器位于相邻仓库 `C-BUG-Patch/v2-common/recode_client.py` 与 `recode_artifacts.py`。

## 本轮兼容修改

- `stages.py` 在可选 libclang API 加载失败时使用已有编译器 JSON AST 路径，图元数据记录实际分析器。
- `run_repo_analysis` 接收 safe-mode 归档的固定身份，将仓库和完整 commit 传递给图、原生导出、manifest 和结果。
- `sandbox_entry.py` 将下载入口绑定的 GitHub 仓库和 commit 传入该身份参数。
- `sandbox.py` 在运行中的容器内通过 Python tar 流导出结果，支持 Docker Desktop 的运行时 tmpfs。导出排除 workspace、checkout 和 .git，并执行原有配额与归档校验。
- 缺失产物会记录实际路径与异常，Docker 可执行文件缺失单独分类。

## 返回数据约束

对接方校验固定 SHA、仓库、构建配置和 run_id。固定 commit 的外层结果与内层导出必须一致。默认服务配置为 `safe-generic`。

调用图、资源与同步操作提供事实和证据。对接方仅使用 race-analysis 中的原始访问，分类结论由专项 Agent 复核。对象声明、线程实例、路径敏感锁集、锁序和别名不足会保留为 partial。

## 已知验证结果

本机实际 Docker 隔离测试两项通过。默认任务服务通过 GitHub 获取本项目固定提交并完成分析，C-BUG-Patch 成功消费结果。

Clang 14 环境中的完整上游回归运行 97 项，93 项通过、2 项失败、2 项默认跳过。两个失败分别为外部调用计数不足和 report_ready 为 false。额外配置真实 Docker 镜像后，两项 Docker 测试单独通过。

完整使用说明见 `C-BUG-Patch/docs/integrations/recode.md`，验收与修改清单见其 2026-11-11 对接记录。两个仓库的修改均保留在本地工作区。
