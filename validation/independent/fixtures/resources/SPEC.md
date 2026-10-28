# Resource-flow microbenchmark specification

The source file contains these resource operations:

```text
create_record:           malloc -> acquire
destroy_record:          free   -> release
transfer_and_release:    create_record call -> transfer
open_and_close:          open   -> acquire
open_and_close:          close  -> release
leak_on_visible_path:    malloc -> acquire
```

The first three operations form a visible acquire-transfer-release lifetime.
The `open`/`close` pair forms a complete file-descriptor lifetime.
The allocation in `leak_on_visible_path` has no visible release.

No other allocation, open, or release API is called in this file.

Scoring identity is the source operation position:

```text
[function, file, line] + operation kind
```

The analyzer's internal resource ID is intentionally excluded so Gold does not
depend on analyzer implementation details.
