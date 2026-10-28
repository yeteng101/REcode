# libuv post-freeze holdout

This holdout was labeled only after the independent synthetic suite had been
frozen and evaluated. It uses the pinned libuv source and does not reuse the
legacy `validation/benchmarks/libuv` labels.

The holdout is split into three category-specific projects:

| Project | Category | Source scope |
|---|---|---|
| `callgraph` | `call_chain` | complete call graph of `src/unix/dl.c` |
| `resources` | `resource_flow` | complete resource operations in `src/unix/dl.c` |
| `sync` | `sync_relation` | complete synchronization relations in `uv_cond_destroy` |

The input graph is `demo/libuv/graph.json`, pinned by SHA-256 in each manifest.
The native resource and synchronization extractors run only after all labels
and hashes are frozen.

These labels were authored from the libuv source after the synthetic suite was
complete. Human review of this new holdout remains a separate step.
