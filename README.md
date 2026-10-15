# Analysis Visualization Agent

一个仓库无关的代码分析结果可视化生成器。输入标准 JSON 分析产物，输出单个可离线打开的
`visualization.html`。

## 输入

工作目录或 `--workspace` 目录可以包含以下文件：

```text
graph.json
architecture.json
key-chains.json
resource-flow.json
sync-relations.json
verification.json
run-result.json
analysis.md
```

只有 `graph.json` 是必需的，其余文件按需读取。

## 运行

```bash
cd visualization-agent
python3 -m analysis_viz --workspace fixtures/example
```

也可以指定调用图和输出文件：

```bash
python3 -m analysis_viz \
  --workspace fixtures/example \
  --graph fixtures/example/graph.json \
  --output /tmp/dashboard.html
```

## 输出

生成的页面包含四个视图：

```text
Overview  模块调用图
Modules   模块规模
Calls     调用边搜索
Findings  资源流、同步关系和关键链摘要
```

页面不依赖外部 CDN、网络或运行时服务。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
