# Synchronization microbenchmark specification

The complete set of synchronization relations for `src/sync.c` is:

```text
worker mutex lock        -> worker mutex unlock         happens_before
worker condition signal  -> wait_worker condition wait   happens_before
start_worker create      -> start_worker join             happens_before
wait_worker lock         -> wait_worker condition wait   happens_before
wait_worker wait         -> wait_worker unlock           happens_before
wait_worker lock         -> wait_worker unlock           happens_before
worker atomic release    -> observe_mailbox acquire      happens_before
relaxed_publish store    -> relaxed_observe load         unknown
```

The `unknown` relation is intentional: a relaxed atomic does not establish a
cross-thread release/acquire happens-before edge.

The source contains no other mutex, condition-variable, thread, event, or
atomic synchronization operations. Labels were written from this semantic
specification before analyzer execution.
