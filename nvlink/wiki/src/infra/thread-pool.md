# Thread Pool

nvlink contains a custom thread pool built on pthreads. It parallelizes two distinct PTX-to-SASS assembly paths, both within the LTO pipeline:

1. **LTO split-compile** (`main()` at line ~1208) -- Splits a single PTX stream into N chunks and assembles them concurrently. Controlled by `-split-compile-extended=N`.
2. **Embedded ptxas per-kernel compilation** (`sub_1112F30` at line ~1889) -- Compiles each kernel in a multi-kernel PTX input as a separate task. Controlled by the `--split-compile` option forwarded to the embedded ptxas subsystem.

All other linker phases -- merge, layout, relocation, finalization -- run single-threaded on the main thread. Each pool instance is created, used, and destroyed within a single scope and does not persist across pipeline phases.

For the LTO split-compile path, the thread count is controlled by `-split-compile-extended=N`. When N is 0 or unspecified, the pool auto-detects via `sysconf(_SC_NPROCESSORS_ONLN)`. When N is 1, the split-compile path runs single-threaded and the pool is never created. The embedded ptxas path reads the thread count from offset +668 in the compilation driver state block.

All threads are created with the default `pthread_attr_t` (NULL), which means the default stack size applies (typically 8 MB on Linux, governed by `RLIMIT_STACK`).

## Control Block Layout

`thread_pool_create` (`sub_43FDB0` at `0x43FDB0`) allocates the pool as a 184-byte (0xB8) structure via `calloc(1, 0xB8)`. The structure holds all synchronization state, the worker thread handles, and the task queue.

```text
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

```text
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

```text
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

## Usage Site 1: LTO Split-Compile in main()

The first usage is the LTO split-compile path in `main()` at approximately line 1208 of the decompiled output. This splits a single PTX stream (produced by libnvvm from linked NVVM IR) into N chunks and assembles each chunk on a worker thread.

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

The worker function is `sub_4264B0` at `0x4264B0`, a small wrapper that unpacks a 40-byte work item and calls `sub_4BD760` (ptxas split compile):

```c
// sub_4264B0 -- split_compile_worker (18 bytes of logic)
void split_compile_worker(work_item_t *item) {
    item->result = sub_4BD760(
        item->output_ptr,      // offset  0: pointer to output slot
        item->ptx_chunk,       // offset  8: PTX chunk string
        item->sm_arch,         // offset 16: SM architecture number
        item->has_nvvm,        // offset 20: byte flag
        item->machine_64,      // offset 21: byte flag (64-bit machine)
        item->has_flag,        // offset 22: byte flag
        item->options_ptr,     // offset 24: compilation options
        item->version_num      // offset 32: version number
    );
}
```

### LTO Work Item Layout (40 bytes)

```text
split_work_item_t (40 bytes, arena-allocated via sub_426AA0)
================================================================
Offset  Size  Field          Description
----------------------------------------------------------------
  0      8    output_ptr     Pointer to output slot in results array
  8      8    ptx_chunk      Pointer to PTX chunk string
 16      4    sm_arch        SM architecture number (dword_2A5F314)
 20      1    has_nvvm       byte_2A5F2C0 != 0
 21      1    machine_64     dword_2A5F30C == 64
 22      1    has_flag       byte_2A5F310 != 0
 23      1    (padding)
 24      8    options_ptr    Pointer to compilation options string
 32      4    version_num    dword_2A5B528
 36      4    result_code    Written by worker (return value of sub_4BD760)
```

Each work item is described in [Split Compilation -- Work Item Layout](../lto/split-compilation.md#path-3-extended-split-compile-multi-threaded).

## Usage Site 2: Embedded ptxas Per-Kernel Compilation

The second usage is the embedded ptxas compilation driver `sub_1112F30` at line ~1889. When a PTX input contains multiple kernels and `--split-compile N` (with N > 1) is active, each kernel is submitted as a separate task to the thread pool for parallel DAGgen + OCG + ELF generation.

This path also supports optional GNU Make jobserver integration (see [MAKEFLAGS Jobserver Integration](#makeflags-jobserver-integration) below).

```c
// sub_1112F30, lines 1889-1944 -- embedded ptxas compilation driver
thread_count = *(int *)(state + 668);              // from --split-compile option
jobserver_status = 255;                            // default: not attempted

// Initialize jobserver if --jobserver flag is set
if (*(byte *)(state + 993)) {                      // offset 993 = --jobserver flag
    jobserver_status = jobserver_init(&qword_2A64430, thread_count);
    if (jobserver_status == 5 || jobserver_status == 6)
        warn("GNU Jobserver support requested, but no compatible "
             "jobserver found. Ignoring '--jobserver'");
    else if (jobserver_status != 0)
        warn("Jobserver requested, but an error occurred");
}

// Create thread pool
pool = thread_pool_create(thread_count);           // sub_43FDB0

// Submit one task per kernel
for (i = 0; i < num_kernels; i++) {
    work = arena_alloc(48);                        // 48-byte work item
    populate_kernel_work(work, kernel_list[i]);
    if (jobserver_flag && jobserver_status == 0)
        work->jobserver = qword_2A64430;           // offset 40: jobserver client
    else
        work->jobserver = NULL;
    thread_pool_submit(pool, kernel_compile_worker, work);
}

// Wait and destroy
thread_pool_wait(pool);                            // sub_43FFE0
thread_pool_destroy(pool);                         // sub_43FE70

// Return remaining jobserver tokens
if (qword_2A64430 && jobserver_cleanup(&qword_2A64430))
    warn("Jobserver requested, but an error occurred");
```

### Per-Kernel Work Item Layout (48 bytes)

```text
kernel_work_item_t (48 bytes, arena-allocated via sub_4307C0)
================================================================
Offset  Size  Field          Description
----------------------------------------------------------------
  0     16    kernel_data    Copied from kernel descriptor offsets 312-327
 16      8    reserved       Zero-initialized
 24      8    kernel_desc2   Copied from kernel descriptor offset 328
 32      8    kernel_ptr     Pointer to kernel descriptor
 40      8    jobserver      Pointer to jobserver client (or NULL)
```

### Per-Kernel Worker Function (`sub_1107420` at `0x1107420`)

The worker acquires a jobserver token (if available), compiles the kernel via `sub_1102B30`, records timing metrics, then releases the jobserver token:

```c
void kernel_compile_worker(kernel_work_item_t *item) {
    // Acquire jobserver token (if jobserver is active)
    if (item->jobserver && jobserver_acquire() != 0)
        warn("Jobserver requested, but an error occurred");

    // Compile the kernel
    sub_1102B30(item->kernel_ptr, item->kernel_desc2 + 64,
                ...);

    // Record timing metrics
    timing_entry = kernel_ptr->timing_array + 112 * kernel_id;
    timing_entry->compile_time = end_time - start_time;

    // Release jobserver token (if jobserver is active)
    if (item->jobserver && jobserver_release() != 0)
        warn("Jobserver requested, but an error occurred");

    arena_free(item);
}
```

## MAKEFLAGS Jobserver Integration

The embedded ptxas compilation path (usage site 2) optionally integrates with the GNU Make jobserver protocol. When `--jobserver` is passed as an embedded ptxas option, the thread pool workers coordinate with GNU Make to respect the global `-j N` parallelism limit. This prevents oversubscription when nvlink is invoked as part of a larger `make -j` build.

### Activation

The `--jobserver` flag is registered in `sub_1103030` as a boolean option and stored at offset +609 in the embedded ptxas options struct. In the outer compilation driver state block (`sub_1112F30`), this maps to offset +993.

### Jobserver Client Initialization (`sub_1D1EF30` at `0x1D1EF30`)

When the jobserver flag is active, `sub_1D1EF30` is called with a pointer to the global `qword_2A64430` and the thread count. It allocates a 296-byte jobserver client struct via `sub_1D26B40(296, ...)`, then calls `sub_1D1E740` to parse `MAKEFLAGS`. The struct also creates an internal pipe (`pipe()` syscall) and spawns a reader thread for async token notification.

### MAKEFLAGS Parser (`sub_1D1E740` at `0x1D1E740`)

Reads `MAKEFLAGS` from the environment and extracts the jobserver connection parameters. Only the `--jobserver-auth=` token format is recognized (string at `0x245F2BC`). The older `--jobserver-fds=` variant from GNU Make < 4.2 is not supported.

Two transport protocols are handled:

**FIFO mode** (`--jobserver-auth=fifo:<path>`): Opens the named pipe at `<path>` with flags `O_RDWR | O_NONBLOCK` (value 0x802). The single file descriptor is stored at both offsets +188 (read) and +192 (write) of the jobserver struct.

**Pipe mode** (`--jobserver-auth=<read_fd>,<write_fd>`): Parses two comma-separated integers as file descriptor numbers. Both substrings are validated to contain only digits via `sub_1D27410`. Each fd is `dup()`'d and the duplicate has `FD_CLOEXEC` set via `fcntl(fd, F_SETFD, FD_CLOEXEC)`. If either `dup` or `fcntl` fails, the read fd is closed and status is set to 7.

### Status Codes

All status updates use `_InterlockedCompareExchange(status, new, 0)` for atomic initialization.

| Status | Meaning |
|---|---|
| 0 | Success -- jobserver initialized |
| 5 | `MAKEFLAGS` environment variable not set |
| 6 | `--jobserver-auth=` token not found in `MAKEFLAGS` |
| 7 | Parse error, `open` failure, `dup` failure, or `fcntl` failure |
| 11 | Write/read error on jobserver pipe |
| 12 | Token release failure |

### Token Protocol

Workers call `sub_1D1E300` (acquire) before compiling a kernel and `sub_1D1E480` (release) after. The acquire function reads one byte from the jobserver pipe to claim a token; the release function writes one byte back. Internal coordination uses a condition variable and counters within the 296-byte struct to handle the case where all tokens are in use (workers block until a token is returned).

The first token is "free" -- the initial-token flag at offset +8 of the jobserver struct is set to 1, so the first worker skips the pipe read. Subsequent workers must acquire real tokens from the Make jobserver.

### Error Messages

| Address | String |
|---|---|
| `0x1F440A8` | `GNU Jobserver support requested, but no compatible jobserver found. Ignoring '--jobserver'` |
| `0x1F44108` | `Jobserver requested, but an error occurred` |

### Relationship to ptxas

ptxas has an identical jobserver client at `sub_1CC7300` (see [ptxas: Threading -- Jobserver](../../ptxas/infra/threading.html)). Both use the same `sub_1D1E740` parser and `sub_1D1EF30` initialization code, compiled from NVIDIA's shared `generic_jobserver_impl` infrastructure.

## Worked Example: Thread Pool Lifecycle

This traces a concrete execution of the LTO split-compile path with 4 PTX chunks and 2 worker threads:

```text
Thread 0 (main)                     Thread 1 (worker)       Thread 2 (worker)
═══════════════                     ═════════════════       ═════════════════
dword_2A5B514 = 2
pool = calloc(1, 0xB8)
  thread_array = calloc(2, 16)
  mutex_init, cond_init x2
  pqueue = sub_44DC60(always_1, 0)
  pthread_create → T1              ──→ lock(mutex)
  pthread_detach(T1)                   pending==0, wait(task_cond)
  pthread_create → T2                                      ──→ lock(mutex)
  pthread_detach(T2)                                           pending==0, wait(task_cond)
  return pool
                                    [sleeping on task_cond] [sleeping on task_cond]

submit(pool, worker, &item[0])
  task0 = malloc(24)
  lock(mutex)
  pqueue_push(task0)
  pending = 1
  broadcast(task_cond)             ──→ wake up!             ──→ wake up!
  unlock(mutex)                        pending=1, pop→task0     pending=0, back to wait
                                       pending=0, active=1
submit(pool, worker, &item[1])         unlock(mutex)
  task1 = malloc(24)                   task0->func(arg)         [sleeping]
  lock(mutex)                          ...compiling...
  pqueue_push(task1)
  pending = 1
  broadcast(task_cond)                                      ──→ wake up!
  unlock(mutex)                                                 pop→task1, active=2
                                                                unlock(mutex)
submit(pool, worker, &item[2])                                  task1->func(arg)
  task2 = malloc(24)
  lock(mutex)                      ...finishes task0...
  pqueue_push(task2)               lock(mutex)
  pending = 1 (now 2 total)        active=1
  broadcast(task_cond)             pop→task2
  unlock(mutex)                    pending=0, active=2
                                   unlock(mutex)
submit(pool, worker, &item[3])     task2->func(arg)
  task3 = malloc(24)
  lock(mutex)
  pqueue_push(task3)                                        ...finishes task1...
  pending = 1                                               lock(mutex)
  broadcast(task_cond)                                      active=1
  unlock(mutex)                                             pop→task3
                                                            pending=0, active=2
thread_pool_wait(pool)                                      unlock(mutex)
  lock(mutex)                                               task3->func(arg)
  pending=0 but active=2
  wait(done_cond)                  ...finishes task2...
                                   lock(mutex)
                                   active=1                 ...finishes task3...
                                   unlock(mutex)            lock(mutex)
                                   [sleep on task_cond]     active=0, pending=0
                                                            signal(done_cond) ──→ wake main
  ──→ active=0, pending=0, break                            unlock(mutex)
  unlock(mutex)                                             [sleep on task_cond]

thread_pool_destroy(pool)
  lock(mutex)
  pqueue_destroy
  pending=0, shutdown=1
  broadcast(task_cond)             ──→ shutdown! exit loop  ──→ shutdown! exit loop
  unlock(mutex)                        thread_count--           thread_count--
                                       signal(done_cond)        signal(done_cond)
  lock(mutex)                          unlock(mutex)            unlock(mutex)
  thread_count==0, break               return NULL              return NULL
  unlock(mutex)
  mutex_destroy, cond_destroy x2
  free(thread_array)
  free(pool)
```

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

### Thread Pool Core

| Address | Name | Size | Role |
|---|---|---|---|
| `0x43FD90` | `thread_pool_get_nproc` | 18 B | Returns CPU count via `sysconf(83)` |
| `0x43FDB0` | `thread_pool_create` | 416 B | Allocates 184-byte pool, spawns N detached workers |
| `0x43FC80` | `worker_main` | 272 B | Worker loop: wait, dequeue, execute, signal |
| `0x43FF50` | `thread_pool_submit` | 144 B | Allocates 24-byte task node, pushes to queue |
| `0x43FFE0` | `thread_pool_wait` | 128 B | Blocks until `pending == 0 && active == 0` |
| `0x43FE70` | `thread_pool_destroy` | 224 B | Two-phase shutdown, frees all pool memory |
| `0x43FC70` | `comparator_true` | 8 B | Always returns 1; makes heap behave as FIFO |

### Priority Queue

| Address | Name | Size | Role |
|---|---|---|---|
| `0x44DC60` | `pqueue_create` | 192 B | Allocates 32-byte queue struct with comparator |
| `0x44DD10` | `pqueue_push` | 224 B | Heap insert with sift-up |
| `0x44DE20` | `pqueue_pop` | 288 B | Heap remove-min with sift-down |
| `0x44DC40` | `pqueue_destroy` | 48 B | Frees queue struct and backing array |

### Worker Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `0x4264B0` | `split_compile_worker` | ~48 B | LTO split-compile: unpacks 40-byte work item, calls `sub_4BD760` |
| `0x1107420` | `kernel_compile_worker` | ~240 B | Per-kernel: acquires jobserver token, calls `sub_1102B30`, releases token |

### Jobserver Integration

| Address | Name | Size | Role |
|---|---|---|---|
| `0x1D1E740` | `parse_makeflags` | ~600 B | Parses `MAKEFLAGS` for `--jobserver-auth=` |
| `0x1D1EF30` | `jobserver_init` | ~560 B | Allocates 296-byte jobserver client, calls `parse_makeflags`, creates internal pipe |
| `0x1D1E300` | `jobserver_acquire` | ~320 B | Acquires one token from the jobserver (reads 1 byte from pipe) |
| `0x1D1E480` | `jobserver_release` | ~400 B | Releases one token back to the jobserver (writes 1 byte to pipe) |
| `0x1D1E060` | `jobserver_cleanup` | -- | Returns remaining tokens and cleans up jobserver state |

### Embedded ptxas Compilation Driver

| Address | Name | Size | Role |
|---|---|---|---|
| `0x1112F30` | `compilation_driver` | ~9 KB | Embedded ptxas main compilation loop; usage site 2 for thread pool |
| `0x1104950` | `ptxas_option_parse` | ~7 KB | Parses embedded ptxas CLI flags (including `--jobserver` at offset +609) |
| `0x1103030` | `ptxas_option_register` | ~1 KB | Registers embedded ptxas CLI flag definitions |

## Key Globals

| Address | Name | Type | Description |
|---|---|---|---|
| `dword_2A5B514` | `split_compile_extended` | `int` | Thread count for LTO extended split compile. 0 = auto-detect, 1 = single-threaded (no pool created), N > 1 = N workers |
| `qword_2A64430` | `jobserver_client` | `void *` | Pointer to 296-byte jobserver client struct (NULL when jobserver not active) |

## Cross-References

**Internal (nvlink wiki):**

- [Split Compilation](../lto/split-compilation.md) -- The LTO split-compile pipeline, including work item layout and the `split_compile_worker` function
- [LTO Overview](../lto/overview.md) -- High-level LTO pipeline diagram showing where multi-threaded PTX-to-SASS assembly fits
- [Pipeline Entry](../pipeline/entry.md) -- `main()` thread pool lifecycle at lines ~1208--1286 of the decompiled output
- [Memory Arenas](memory-arenas.md) -- Arena allocator thread safety: the queue uses arena allocation while task nodes use `malloc`/`free`
- [Error Reporting](error-reporting.md) -- Per-thread TLS diagnostic state (`sub_44F410`) that the thread pool workers inherit
- [CLI Flags](../config/cli-flags.md) -- `-split-compile-extended=N` option controlling thread count
- [Environment Variables](../config/env-vars.md) -- `MAKEFLAGS` environment variable documentation with full MAKEFLAGS parser analysis

**Sibling wikis:**

- [ptxas: Threading](../../ptxas/infra/threading.html) -- ptxas has a structurally identical thread pool (`sub_1CB18B0`, 184-byte pool struct, 24-byte task nodes, `pthread_detach` + condition-variable shutdown) used for parallel kernel compilation. The pool constructor, worker loop, submit, wait, and destroy functions are compiled from the same source.
- [ptxas: Memory Pools](../../ptxas/infra/memory-pools.html) -- ptxas memory pool allocator that parallels nvlink's arena system

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
| Default pthread stack size (NULL attr) | HIGH | `sub_43FDB0`: `pthread_create(..., 0, ...)` -- NULL attr argument |
| `-split-compile-extended` CLI option | HIGH | Strings `"-split-compile-extended=%d"` at `0x1d32268` and `"-split-compile-extended"` at `0x1d32283` |
| "Unable to create thread pool" error message | HIGH | String at `0x1d342db` in strings JSON |
| Task queue uses `sub_43FC70` comparator (always returns 1) | HIGH | `sub_43FDB0`: `sub_44DC60(sub_43FC70, 0)` passes comparator function; `sub_43FC70` is an 8-byte function |
| Priority queue struct is 32 bytes, arena-allocated | MEDIUM | `sub_44DC60` allocates from arena; 32-byte size inferred from field layout |
| FIFO behavior from always-true comparator | MEDIUM | Logical deduction from heap sift-up/sift-down behavior when comparator always returns 1; insertion-order preservation validated by analysis |
| Shared design with ptxas thread pool | HIGH | ptxas `sub_1CB18B0` has identical 184-byte struct, same `pthread_detach` pattern, same 24-byte task nodes, same condition-variable protocol |
| Second usage site in `sub_1112F30` | HIGH | `sub_43FDB0`/`sub_43FF50`/`sub_43FFE0`/`sub_43FE70` calls at decompiled lines 1906/1935/1943/1944 |
| `--jobserver` flag at embedded ptxas offset +609 | HIGH | `sub_1104950` line 284: `sub_42E390(v11, "jobserver", (a3 + 609), 1u)` |
| Jobserver client struct is 296 bytes | HIGH | `sub_1D1EF30`: `sub_1D26B40(296, ...)` -- exact allocation size |
| MAKEFLAGS parsed by `sub_1D1E740` | HIGH | `getenv("MAKEFLAGS")` at line 57 of decompiled `sub_1D1E740` |
| `--jobserver-auth=` is the only recognized token format | HIGH | `sub_1D27380(&ptr, "--jobserver-auth=", -1, 17)` -- string literal match |
| FIFO mode opens with `O_RDWR | O_NONBLOCK` (0x802) | HIGH | `open(file, 2050)` in `sub_1D1E740` -- 2050 decimal = 0x802 |
| Pipe mode uses `dup()` + `fcntl(FD_CLOEXEC)` | HIGH | `dup(v31)` then `fcntl(v32, 2, 2048)` in `sub_1D1E740` |
| Worker `sub_1107420` acquires/releases jobserver tokens | HIGH | Calls `sub_1D1E300()` at line 20 and `sub_1D1E480()` at line 57 |
| Per-kernel work item is 48 bytes | HIGH | `sub_4307C0(v203, 48)` at `sub_1112F30` line 1918 |
