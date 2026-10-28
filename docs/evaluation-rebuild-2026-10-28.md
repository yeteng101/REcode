# 评测集重建与独立结果（2026-10-28）

## 1. 旧 100% 为什么无效

旧 libuv 调用链 Gold 的一部分是从同一张预测图选择的，预测与答案不是独立生成。
这种数据只能证明实现没有回退，不能证明识别准确。

旧结果还存在第二个问题：

```text
precision 只在已标注 identity 上计算
未标注 prediction 被完全排除
```

因此旧 `precision=100%` 的含义只是“已检查的 identity 没有 FP”，不是全仓准确率。

从本次起：

- 旧 libuv/Redis 集仅作为 `legacy-open-world-regression`。
- 新结果必须来自先冻结 Gold、后运行分析器的封闭世界评测。
- 未标注但落在封闭评测范围内的 prediction 必须计 FP。

## 2. 新评测协议

流程固定为：

```text
写源码语义规范
-> 先写 Gold
-> 计算源码/Gold/Spec SHA-256
-> 冻结 manifest
-> 才允许运行分析器
-> 评分
```

运行脚本会先验证：

- 源码文件集合和 SHA-256 未变化；
- `ground-truth.json` 的 SHA-256 未变化；
- `SPEC.md` 的 SHA-256 未变化；
- manifest 状态为 `frozen`；
- libuv holdout 的固定 commit 和输入图哈希未变化。

资源流使用 `source_anchor_v1`：

```text
[function, file, line] + operation kind
```

不使用分析器内部 resource ID，避免 Gold 依赖被评测实现。

## 3. 独立合成集结果

合成集不包含 libuv 或 Redis 代码，三个 micro-project 分别为：

- `callgraph`：直接调用、嵌套调用、宏展开和非调用控制。
- `resources`：malloc/free、alias、跨函数 transfer、错误路径未释放、fd。
- `sync`：mutex、condition variable、thread join、release/acquire、relaxed。

结果：

| Project | Category | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| callgraph | call_chain | 3 | 0 | 0 | 3 | 100.00% | 100.00% | 100.00% |
| resources | resource_flow | 5 | 0 | 1 | 0 | 100.00% | 83.33% | 90.91% |
| sync | sync_relation | 6 | 2 | 2 | 0 | 75.00% | 75.00% | 75.00% |
| 合计 | 三类 | 14 | 2 | 3 | 3 | 87.50% | 82.35% | 84.85% |

合成集标签由 Codex 按源码规范编写，`human_review` 仍为 `pending`。
这能排除 libuv 数据泄漏，但不能替代人工确认。

主要误差：

- 资源流没有输出 `leak_on_visible_path` 的 malloc acquire，形成 1 条 FN。
- 同步关系的 atomic acquire 端点被错误标成 `atomic_load_explicit`，没有标成外层函数
  `observe_mailbox`/`relaxed_observe`，因此同一关系同时形成 FP 和 FN。

## 4. 后续 libuv holdout

libuv holdout 在合成集冻结并完成后才编写标签：

| Project | Category | TP | FP | FN | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|---:|
| callgraph | call_chain | 11 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| resources | resource_flow | 5 | 0 | 0 | 100.00% | 100.00% | 100.00% |
| sync | sync_relation | 1 | 0 | 2 | 100.00% | 33.33% | 50.00% |
| 合计 | 三类 | 17 | 0 | 2 | 100.00% | 89.47% | 94.44% |

这里的 100% 只适用于：

```text
src/unix/dl.c 的调用链
src/unix/dl.c 的资源操作
uv_cond_destroy 内的同步关系
```

它不是整个 libuv 的准确率。同步关系仍然漏掉了
`pthread_cond_timedwait_relative_np` 两侧的程序顺序关系，说明 Recall 仍有明显缺口。

新 holdout 的人工复核状态为 `pending`，不能写成人工确认后的最终基准。

## 5. 可复现命令

冻结独立合成集：

```bash
python3 scripts/freeze_independent_benchmark.py
```

运行独立合成集：

```bash
python3 scripts/run_independent_benchmark.py
```

冻结 libuv holdout：

```bash
python3 scripts/freeze_libuv_holdout.py
```

运行 libuv holdout：

```bash
python3 scripts/run_libuv_holdout.py
```

结果：

```text
validation/independent/results/report.md
validation/independent/results/summary.json
validation/holdouts/libuv-v1/results/report.md
validation/holdouts/libuv-v1/results/summary.json
```

门禁结果：

```text
validation/independent/results/gate.json        passed
validation/holdouts/libuv-v1/results/gate.json  passed
```

门禁阈值是工程回归下限，不是根据本次结果反推的“100% 目标”。
