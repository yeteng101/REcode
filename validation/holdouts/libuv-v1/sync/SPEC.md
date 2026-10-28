# libuv synchronization holdout specification

Scope: `uv_cond_destroy` in `src/unix/thread.c`.

On the pinned macOS build, the function:

1. initializes a temporary mutex;
2. locks it;
3. performs a relative condition-variable timed wait;
4. unlocks the mutex;
5. destroys it.

Expected synchronization relations:

```text
pthread_mutex_lock -> pthread_cond_timedwait_relative_np  happens_before
pthread_cond_timedwait_relative_np -> pthread_mutex_unlock  happens_before
pthread_mutex_lock -> pthread_mutex_unlock                 happens_before
```

The expected endpoint functions are all the enclosing `uv_cond_destroy`.
No other synchronization relation belongs to this function scope.
