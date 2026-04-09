# Thread Pool

nvlink contains a custom thread pool built on pthreads. It is used exclusively for parallelizing the PTX-to-SASS assembly step during LTO split compilation (see [Split Compilation](../lto/split-compilation.md)). All other linker phases -- merge, layout, relocation, finalization -- run single-threaded on the main thread. The pool is created, used, and destroyed within a single scope in `main()`, and does not persist across pipeline phases.

The thread count is controlled by `-split-compile-extended=N`. When N is 0 or unspecified, the pool auto-detects via `sysconf(_SC_NPROCESSORS_ONLN)`. When N is 1, the split-compile path runs single-threaded and the pool is never created.

## Control Block Layout

`thread_pool_create` (`sub_43FDB0` at `0x43FDB0`) allocates the pool as a 184-byte (0xB8) structure via `calloc(1, 0xB8)`. The structure holds all synchronization state, the worker thread handles, and the task queue.

```
thread_pool_t (184 bytes, heap-allocated via calloc)
=========================================================
Offset  Size  Field              Description
---------------------------------------------------------
  0      8    thread_array       Pointer to array of 16-byte thread entries
  8      8    task_queue         Pointer to priority queue (32-byte struct)
 16      4    pending_count      Tasks enqueued but not yet dequeued by a worker
 20      4    (padding)
 24     40    mutex              pthread_mutex_t protecting all pool state
 64     48    task_cond          pthread_cond_t -- signaled when a task is submitted
112     48    done_cond          pthread_cond_t -- signaled when a task completes
                                 or a worker thread exits
160      8    active_count       Workers currently executing a task
168      8    thread_count       Live worker threads (decremented on exit)
176      1    shutdown           Shutdown flag (0 = running, 1 = shutting down)
177      7    (padding to 184)
```

Each element in `thread_array` is 16 bytes: an 8-byte slot field (unused by the pool logic) followed by an 8-byte `pthread_t`. The array is allocated as `calloc(num_threads, 16)`.

## Lifecycle

The pool follows a strict create-submit-wait-destroy lifecycle. There is no reuse or reset path.

### thread_pool_get_nproc (`sub_43FD90` at `0x43FD90`)

A one-liner that returns `sysconf(83)`, where 83 is `_SC_NPROCESSORS_ONLN` on Linux. Called from `main()` when `dword_2A5B514` (the `-split-compile-extended` value) is 0, to auto-detect the thread count:

```c
long thread_pool_get_nproc(void) {
    return sysconf(_SC_NPROCESSORS_ONLN);   // sysconf(83)
}
```

### thread_pool_create (`sub_43FDB0` at `0x43FDB0`)

Allocates the control block, initializes the mutex and both condition variables, creates the task queue, then spawns N worker threads in a loop. All threads are immediately detached via `pthread_detach`, meaning the pool does not call `pthread_join` -- shutdown synchronization is handled entirely through the `done_cond` condition variable and the `thread_count` field.

```c
pool_t *thread_pool_create(size_t num_threads) {
    pool = calloc(1, 0xB8);                         // 184-byte control block
    pool->thread_array = calloc(num_threads, 16);    // 16 bytes per thread
    pool->thread_count = num_threads;                // offset 168
    pool->pending_count = 0;                         // offset 16
    pthread_mutex_init(&pool->mutex, NULL);          // offset 24
    pthread_cond_init(&pool->task_cond, NULL);       // offset 64
    pthread_cond_init(&pool->done_cond, NULL);       // offset 112

    // Priority queue with always-true comparator -> FIFO behavior
    pool->task_queue = pqueue_create(comparator_true, 0);  // sub_44DC60

    for (i = 0; i < num_threads; i++) {
        pthread_create(&pool->thread_array[i].thread, NULL, worker_main, pool);
        pthread_detach(pool->thread_array[i].thread);
    }
    return pool;
}
```

The task queue comparator is `sub_43FC70` at `0x43FC70`, an 8-byte function that unconditionally returns 1. This makes the min-heap behave as a FIFO queue (see [Task Queue](#task-queue) below).

### thread_pool_submit (`sub_43FF50` at `0x43FF50`)

Enqueues a `(function, argument)` pair. Each task is a 24-byte heap-allocated node:

```
task_node_t (24 bytes, heap-allocated via malloc)
=================================================
Offset  Size  Field    Description
-------------------------------------------------
  0      8    func     Function pointer: void (*)(void *)
  8      8    arg      Opaque argument pointer passed to func
 16      8    next     Unused (set to NULL; queue manages ordering)
```

The submit path:

```c
int thread_pool_submit(pool_t *pool, void (*func)(void *), void *arg) {
    if (!func || !pool) return 0;

    task = malloc(24);
    task->func = func;    // offset 0
    task->arg  = arg;     // offset 8
    task->next = NULL;    // offset 16

    pthread_mutex_lock(&pool->mutex);
    pqueue_push(task, pool->task_queue);         // sub_44DD10
    pool->pending_count++;
    pthread_cond_broadcast(&pool->task_cond);    // wake all sleeping workers
    pthread_mutex_unlock(&pool->mutex);
    return 1;
}
```

`pthread_cond_broadcast` is used rather than `pthread_cond_signal`, waking all waiting workers even though only one task was submitted. This is a conservative choice that avoids potential missed-wakeup scenarios at the cost of thundering-herd wakeups. In practice the pool is small (typically 4--16 threads) and all tasks are submitted in a tight loop, so the broadcast overhead is negligible.

### worker_main (`start_routine` at `0x43FC80`)

The worker thread entry point. Each thread runs an infinite loop: acquire the mutex, wait on `task_cond` if no work is available, dequeue a task, release the mutex, execute the task, then re-acquire to update accounting. The loop exits only when the `shutdown` flag is set.

```c
void *worker_main(pool_t *pool) {
    while (1) {
        pthread_mutex_lock(&pool->mutex);

        // Wait for work or shutdown
        while (pool->pending_count == 0) {
            if (pool->shutdown) goto exit;
            pthread_cond_wait(&pool->task_cond, &pool->mutex);
        }

        // Dequeue
        task = pqueue_pop(pool->task_queue);    // sub_44DE20
        pool->pending_count--;
        pool->active_count++;
        pthread_mutex_unlock(&pool->mutex);

        // Execute outside the lock
        if (task) {
            task->func(task->arg);
            free(task);                          // free the 24-byte task node
        }

        // Signal completion
        pthread_mutex_lock(&pool->mutex);
        pool->active_count--;
        if (!pool->shutdown && pool->active_count == 0 && pool->pending_count == 0)
            pthread_cond_signal(&pool->done_cond);
        pthread_mutex_unlock(&pool->mutex);
    }

exit:
    pool->thread_count--;
    pthread_cond_signal(&pool->done_cond);
    pthread_mutex_unlock(&pool->mutex);
    return NULL;
}
```

The completion signal on `done_cond` fires only when both `active_count` and `pending_count` reach zero and the pool is not shutting down. This is the condition that `thread_pool_wait` blocks on during normal operation. During shutdown, the signal fires after each thread decrements `thread_count`.

### thread_pool_wait (`sub_43FFE0` at `0x43FFE0`)

Blocks the caller until all submitted tasks have completed. The wait condition depends on whether shutdown has been initiated:

```c
void thread_pool_wait(pool_t *pool) {
    if (!pool) return;
    pthread_mutex_lock(&pool->mutex);
    while (1) {
        if (pool->pending_count == 0) {
            if (pool->shutdown) {
                if (pool->thread_count == 0) break;   // all threads exited
            } else {
                if (pool->active_count == 0) break;   // all tasks finished
            }
        }
        pthread_cond_wait(&pool->done_cond, &pool->mutex);
    }
    pthread_mutex_unlock(&pool->mutex);
}
```

During normal operation (before `thread_pool_destroy`), the break condition is `pending_count == 0 && active_count == 0`. During shutdown, it changes to `pending_count == 0 && thread_count == 0`, which ensures all workers have exited their loops before the caller proceeds to destroy synchronization primitives.

### thread_pool_destroy (`sub_43FE70` at `0x43FE70`)

Two-phase shutdown: (1) set the shutdown flag and broadcast to wake all sleeping workers, (2) wait for every worker thread to exit, (3) destroy synchronization primitives and free memory.

```c
void thread_pool_destroy(pool_t *pool) {
    if (!pool) return;

    // Phase 1: Signal shutdown
    pthread_mutex_lock(&pool->mutex);
    pqueue_destroy(pool->task_queue);              // sub_44DC40
    pool->pending_count = 0;
    pool->shutdown = 1;
    pthread_cond_broadcast(&pool->task_cond);      // wake all workers
    pthread_mutex_unlock(&pool->mutex);

    // Phase 2: Wait for all threads to exit
    pthread_mutex_lock(&pool->mutex);
    while (pool->pending_count != 0 || pool->thread_count != 0)
        pthread_cond_wait(&pool->done_cond, &pool->mutex);
    pthread_mutex_unlock(&pool->mutex);

    // Phase 3: Cleanup
    pthread_mutex_destroy(&pool->mutex);
    pthread_cond_destroy(&pool->task_cond);
    pthread_cond_destroy(&pool->done_cond);
    free(pool->thread_array);
    free(pool);
}
```

The pool control block and thread array are freed with `free()`, matching the `calloc` in `thread_pool_create`. These are not arena-allocated -- the thread pool manages its own memory independently of nvlink's arena allocator. The task queue's backing storage, however, is arena-allocated (see below).

## Task Queue

The task queue is a binary min-heap backed by a dynamic pointer array. It is a general-purpose priority queue implementation (`sub_44DC60` / `sub_44DD10` / `sub_44DE20`) that the thread pool uses with a degenerate comparator.

### Queue Structure

```
pqueue_t (32 bytes, arena-allocated)
=========================================
Offset  Size  Field         Description
-----------------------------------------
  0      8    array         Pointer to element pointer array
  8      8    count         Current number of elements
 16      8    capacity      Allocated slots in the array
 24      8    comparator    Function pointer: int (*)(void *, void *)
```

### pqueue_create (`sub_44DC60` at `0x44DC60`)

Allocates the 32-byte queue struct and the initial element array from the arena allocator. The comparator function and initial capacity are parameters. For the thread pool, the comparator is `sub_43FC70` (always returns 1) and the initial capacity is 0.

### pqueue_push (`sub_44DD10` at `0x44DD10`)

Inserts an element at position `count`, then sifts up by comparing with the parent at `(index - 1) / 2`. If the comparator returns 0 (parent should come after child), the elements are swapped and the process continues up the heap. Growth doubles the capacity when `count >= capacity`, using `sub_4313A0` (arena realloc).

Since the comparator always returns 1, the sift-up loop always breaks immediately on the first comparison -- the new element stays at the end. Combined with the sift-down behavior in pop, this produces approximate FIFO ordering: the first element pushed is always at position 0 (the root), and elements dequeue in insertion order.

### pqueue_pop (`sub_44DE20` at `0x44DE20`)

Removes and returns the root element (position 0). Moves the last element to position 0 and sifts down. At each level, compares the two children and swaps the parent with the smaller child if the comparator says the parent should come after the child.

With the always-true comparator: the parent always "beats" its children, so the sift-down loop breaks immediately. The moved element stays at position 0. On the next pop, that element is returned. The net effect is FIFO order.

### pqueue_destroy (`sub_44DC40` at `0x44DC40`)

Frees both the element array and the queue struct by calling `sub_431000` (arena free) twice. Called during `thread_pool_destroy` before the shutdown broadcast.

## Usage in main()

The thread pool appears exactly once in the nvlink pipeline, inside the LTO split-compile path in `main()` at approximately line 1208 of the decompiled output:

```c
// Auto-detect thread count if not specified
if (dword_2A5B514 == 0)
    dword_2A5B514 = thread_pool_get_nproc();     // sub_43FD90

// Create pool
pool = thread_pool_create(dword_2A5B514);         // sub_43FDB0
if (!pool)
    fatal("Unable to create thread pool");

// Allocate per-split work items (40 bytes each)
outputs = arena_alloc(8 * num_splits);

// Submit one task per split
for (i = 0; i < num_splits; i++) {
    populate_work_item(&work_items[i], split_ptx[i], sm, options, mode);
    thread_pool_submit(pool, split_compile_worker, &work_items[i]);
}

// Barrier: wait for all compilations to finish
thread_pool_wait(pool);                           // sub_43FFE0

// Teardown
thread_pool_destroy(pool);                        // sub_43FE70

// Process results sequentially
for (i = 0; i < num_splits; i++) {
    check_error(work_items[i].result);
    validate_and_merge(elfw, outputs[i], "lto.cubin");
}
```

The worker function is `sub_4264B0` at `0x4264B0`, a 48-byte wrapper that unpacks a 40-byte work item and calls `sub_4BD760` (ptxas split compile). Each work item is described in [Split Compilation -- Work Item Layout](../lto/split-compilation.md#path-3-extended-split-compile-multi-threaded).

## Memory Allocation Strategy

The pool uses a deliberate split between two allocators:

| What | Allocator | Why |
|---|---|---|
| Pool control block (184 B) | `calloc` / `free` | Must outlive any arena scope; freed explicitly in destroy |
| Thread array (16 * N bytes) | `calloc` / `free` | Same lifetime as the pool control block |
| Task nodes (24 B each) | `malloc` / `free` | Allocated in submit (any thread), freed by worker thread after execution |
| Queue struct (32 B) | Arena (`sub_4307C0`) | Lives as long as the pool; freed via arena in `pqueue_destroy` |
| Queue backing array | Arena (`sub_4313A0`) | Grows via arena realloc; freed in `pqueue_destroy` |

The task nodes use the system allocator (`malloc`/`free`) rather than the arena because they are allocated and freed from different threads. The arena allocator has per-arena mutex protection and is thread-safe, but the task nodes are short-lived and small -- using `malloc` avoids contention on the arena lock during high-throughput submission.

## Synchronization Details

All mutable pool state is protected by a single `pthread_mutex_t` at offset 24. The pool uses two condition variables:

| Condition Variable | Offset | Signaled When | Waited On By |
|---|---|---|---|
| `task_cond` | 64 | A task is submitted (`submit`) or shutdown is initiated (`destroy`) | Worker threads waiting for work |
| `done_cond` | 112 | A worker finishes a task and the pool becomes idle, or a worker exits during shutdown | `thread_pool_wait` and `thread_pool_destroy` |

The signaling discipline:
- `task_cond` uses `pthread_cond_broadcast` (wake all waiters) in both `submit` and `destroy`
- `done_cond` uses `pthread_cond_signal` (wake one waiter) because only the main thread ever waits on it

There is no spurious-wakeup protection beyond the while-loop re-check of the predicate, which is the standard pthreads pattern.

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x43FD90` | `thread_pool_get_nproc` | 18 B | Returns CPU count via `sysconf(83)` |
| `0x43FDB0` | `thread_pool_create` | 416 B | Allocates 184-byte pool, spawns N detached workers |
| `0x43FC80` | `worker_main` | 272 B | Worker loop: wait, dequeue, execute, signal |
| `0x43FF50` | `thread_pool_submit` | 144 B | Allocates 24-byte task node, pushes to queue |
| `0x43FFE0` | `thread_pool_wait` | 128 B | Blocks until `pending == 0 && active == 0` |
| `0x43FE70` | `thread_pool_destroy` | 224 B | Two-phase shutdown, frees all pool memory |
| `0x43FC70` | `comparator_true` | 8 B | Always returns 1; makes heap behave as FIFO |
| `0x44DC60` | `pqueue_create` | 192 B | Allocates 32-byte queue struct with comparator |
| `0x44DD10` | `pqueue_push` | 224 B | Heap insert with sift-up |
| `0x44DE20` | `pqueue_pop` | 288 B | Heap remove-min with sift-down |
| `0x44DC40` | `pqueue_destroy` | 48 B | Frees queue struct and backing array |

## Key Globals

| Address | Name | Type | Description |
|---|---|---|---|
| `dword_2A5B514` | `split_compile_extended` | `int` | Thread count for extended split compile. 0 = auto-detect, 1 = single-threaded (no pool created), N > 1 = N workers |

## Cross-References

**Internal (nvlink wiki):**

- [Split Compilation](../lto/split-compilation.md) -- The LTO split-compile pipeline that is the sole consumer of the thread pool, including work item layout and the `split_compile_worker` function
- [LTO Overview](../lto/overview.md) -- High-level LTO pipeline diagram showing where multi-threaded PTX-to-SASS assembly fits
- [Pipeline Entry](../pipeline/entry.md) -- `main()` thread pool lifecycle at lines ~1208--1286 of the decompiled output
- [Memory Arenas](memory-arenas.md) -- Arena allocator thread safety: the queue uses arena allocation while task nodes use `malloc`/`free`
- [Error Reporting](error-reporting.md) -- Per-thread TLS diagnostic state (`sub_44F410`) that the thread pool workers inherit
- [CLI Flags](../config/cli-flags.md) -- `-split-compile-extended=N` option controlling thread count

**Sibling wikis:**

- [ptxas: Threading](../../../ptxas/wiki/src/infra/threading.md) -- ptxas has a structurally identical thread pool (`sub_1CB18B0`, 184-byte pool struct, 24-byte task nodes, `pthread_detach` + condition-variable shutdown) used for parallel kernel compilation
- [ptxas: Memory Pools](../../../ptxas/wiki/src/infra/memory-pools.md) -- ptxas memory pool allocator that parallels nvlink's arena system

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| Pool control block is 184 bytes (0xB8) via `calloc(1, 0xB8)` | HIGH | `sub_43FDB0` decompiled: `calloc(1u, 0xB8u)` -- exact match |
| Thread array is `calloc(nmemb, 0x10)` (16 bytes per thread) | HIGH | `sub_43FDB0` decompiled: `calloc(nmemb, 0x10u)` |
| `thread_count` at offset 168 (QWORD index 21) | HIGH | `sub_43FDB0`: `*((_QWORD *)v1 + 21) = nmemb` -- offset `21 * 8 = 168` |
| `pending_count` at offset 16 (DWORD index 4) | HIGH | `sub_43FDB0`: `*((_DWORD *)v1 + 4) = 0` -- offset `4 * 4 = 16`; `sub_43FF50` increments `*(_DWORD *)(a1 + 16)` |
| Mutex at offset 24, task_cond at 64, done_cond at 112 | HIGH | `sub_43FDB0`: `pthread_mutex_init(v1 + 24)`, `pthread_cond_init(v1 + 64)`, `pthread_cond_init(v1 + 112)` |
| Shutdown flag at offset 176 (byte) | HIGH | `sub_43FE70` (destroy): `ptr[176] = 1`; `start_routine`: `if (a1[176])` |
| `active_count` at offset 160 | HIGH | `sub_43FFE0` (wait): `if (!*(_QWORD *)(a1 + 160))` break when not shutdown |
| Workers are detached via `pthread_detach` | HIGH | `sub_43FDB0` loop: `pthread_create` then `pthread_detach(v4)` |
| Task nodes are 24 bytes via `malloc(0x18)` | HIGH | `sub_43FF50`: `v4 = malloc(0x18u)` -- exact match |
| `pthread_cond_broadcast` on submit | HIGH | `sub_43FF50`: `pthread_cond_broadcast((pthread_cond_t *)(a1 + 64))` |
| `pthread_cond_signal` on done_cond | HIGH | `start_routine`: `pthread_cond_signal(v1)` where `v1 = a1 + 112` |
| `thread_pool_get_nproc` returns `sysconf(83)` | HIGH | `sub_43FD90` decompiled: `return sysconf(83);` -- exact one-liner |
| `-split-compile-extended` CLI option | HIGH | Strings `"-split-compile-extended=%d"` at `0x1d32268` and `"-split-compile-extended"` at `0x1d32283` |
| "Unable to create thread pool" error message | HIGH | String at `0x1d342db` in strings JSON |
| Task queue uses `sub_43FC70` comparator (always returns 1) | HIGH | `sub_43FDB0`: `sub_44DC60(sub_43FC70, 0)` passes comparator function; `sub_43FC70` is an 8-byte function |
| Priority queue struct is 32 bytes, arena-allocated | MEDIUM | `sub_44DC60` allocates from arena; 32-byte size inferred from field layout |
| FIFO behavior from always-true comparator | MEDIUM | Logical deduction from heap sift-up/sift-down behavior when comparator always returns 1; insertion-order preservation validated by analysis |
| Shared design with ptxas thread pool | HIGH | ptxas `sub_1CB18B0` has identical 184-byte struct, same `pthread_detach` pattern, same 24-byte task nodes, same condition-variable protocol |
