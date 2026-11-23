# REcode 远端历史提交检出

本轮以 `group3` 的 `6af988f` 为基线。C-BUG-Inspector 的交付镜像使用 `849614a`，其中公开仓库的历史 SHA 检出报 `unable to read tree`。相同的检出逻辑仍存在于 `6af988f`。

## 原因与实现

开发模式使用 `git clone --depth 1`，随后直接在本地检出请求的提交。浅克隆通常只有远端分支头，当请求较早的提交时，本地缺少目标对象。

收到完整 40 位 SHA 时，先浅克隆且暂不检出工作树。若分支头与请求不同，向 `origin` 按 SHA 浅度补取目标对象。随后分离检出该提交，校验 `HEAD` 与规范化后的请求 SHA 一致。远端无法提供该对象时返回 checkout 阶段的 `RepositoryCheckoutError`。Windows 上失败路径会重试删除 Git 的只读对象文件。

未指定提交时沿用分支浅克隆。开发模式下原有短 SHA 和分支引用走既有检出路径。生产 Docker 沙箱仍通过 GitHub archive 获取固定版本。

## 验证结果

- 在由两次提交构成的本地 Git 远端上，原实现对默认分支和显式 `main` 都无法检出历史 SHA。修复后两个场景均通过，同时验证了大写 SHA 规范化、检出文件内容、浅克隆边界、默认分支头和缺失对象的失败清理。
- 直接从 GitHub 检出了验收中失败的 `libuv/libuv` 提交 `f87c8e4f70f234b952d9c47b15fb567f78e5f399`，并核对了 `HEAD` 和浅克隆标记。
- Docker 内 Python 3.12 / Clang 14 的 Clang 全套回归为 144 项，其中 141 通过、2 跳过、1 失败。`test_external_function_calls_are_preserved` 的节点数断言在 `6af988f` 基线上同样失败。
