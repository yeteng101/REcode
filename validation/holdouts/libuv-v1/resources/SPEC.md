# libuv resource-flow holdout specification

Scope: all resource API operations whose call site is in `src/unix/dl.c`.

Expected operations:

```text
uv_dlopen:      dlopen     -> acquire
uv_dlclose:     uv__free   -> release
uv_dlclose:     dlclose    -> release
uv__dlerror:    uv__free   -> release
uv__dlerror:    uv__strdup -> acquire
```

Scoring uses the source operation position `[function, file, line]` and the
operation kind. Internal analyzer resource IDs are not part of Gold.
