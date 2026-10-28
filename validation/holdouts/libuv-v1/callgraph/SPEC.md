# libuv call-graph holdout specification

Scope: all function calls whose call site is in `src/unix/dl.c`.

Expected direct calls:

```text
uv_dlopen -> dlerror
uv_dlopen -> dlopen
uv_dlopen -> uv__dlerror
uv_dlclose -> uv__free
uv_dlclose -> dlclose
uv_dlsym -> dlerror
uv_dlsym -> dlsym
uv_dlsym -> uv__dlerror
uv__dlerror -> uv__free
uv__dlerror -> dlerror
uv__dlerror -> uv__strdup
```

There are no other calls in this file. Every prediction whose call-site evidence
is in `src/unix/dl.c` is scored; an extra in-scope edge is a false positive.
