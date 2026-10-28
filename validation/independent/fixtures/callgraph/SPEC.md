# Call-graph microbenchmark specification

The complete call graph for `src/callgraph.c` is:

```text
entry -> bridge
bridge -> leaf
entry -> doubled       # through APPLY_ONCE
```

There are no other function calls in the source file.

The following propositions are false:

```text
entry -> leaf
bridge -> doubled
leaf -> bridge
```

`unused` has no callers. The label set was written from this specification,
not from analyzer output.
