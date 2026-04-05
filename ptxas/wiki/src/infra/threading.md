# Thread Pool & Concurrency

ptxas compiles multiple entry functions (kernels) in a single PTX input file. When `--split-compile` or `--allow-expensive-optimizations` is active, the compilation driver dispatches each kernel to a worker thread for parallel DAGgen+OCG+ELF+DebugInfo processing. The threading infrastructure lives in two address ranges: the TLS system at `0x4280xx`--`0x4286xx` (front-end) and the thread pool at `0x1CB17xx`--`0x1CB1Axx` (shared infrastructure).

| | |
|---|---|
| **TLS accessor** | `sub_4280C0` (597 bytes, 3,928 callers, 280-byte struct) |
| **TLS key init** | `ctor_001` at `0x4094C0` (static constructor) |
| **TLS destructor** | `destr_function` at `0x427F10` |
| **Thread pool ctor** | `sub_1CB18B0` (184-byte pool struct) |
| **Task submit** | `sub_1CB1A50` (24-byte task node) |
| **Worker thread** | `start_routine` at `0x1CB1780` |
| **Wait-all** | `sub_1CB1AE0` |
| **Pool destroy** | `sub_1CB1970` |
| **CPU count** | `sub_1CB1890` (`sysconf(_SC_NPROCESSORS_CONF)`) |
| **Mutex init helper** | `sub_428620` (recursive mutex factory) |
| **Global mutex lock** | `sub_4286A0` (lazy-init + lock) |
| **Jobserver client** | `sub_1CC7300` (GNU Make integration) |

## Architecture

```
                         ┌─────────────────────────────────────────┐
                         │          Compilation Driver             │
                         │           sub_446240                    │
                         │                                         │
                         │  if (thread_count > 0):                 │
                         │    pool = sub_1CB18B0(thread_count)     │
                         │    for each kernel:                     │
                         │      snapshot config → 360-byte buffer  │
                         │      copy hash maps for isolation       │
                         │      sub_1CB1A50(pool, sub_436DF0, buf) │
                         │    sub_1CB1AE0(pool)   // wait-all      │
                         │    sub_1CB1970(pool)    // destroy       │
                         └────────────┬────────────────────────────┘
                                      │ enqueue tasks
                                      ▼
                    ┌────────────────────────────────────┐
                    │         Thread Pool (184 bytes)     │
                    │                                     │
                    │  mutex ──────── guards all fields   │
                    │  cond_work ──── wake workers        │
                    │  cond_done ──── signal completion   │
                    │  work_queue ─── priority heap       │
                    │  pending ────── pending task count   │
                    │  shutdown ───── termination flag     │
                    └───┬──────┬──────┬──────────────────┘
                        │      │      │
                  ┌─────┘      │      └─────┐
                  ▼            ▼            ▼
            ┌──────────┐ ┌──────────┐ ┌──────────┐
            │ Worker 0 │ │ Worker 1 │ │ Worker N │
            │ (detached)│ │(detached)│ │(detached)│
            └──────────┘ └──────────┘ └──────────┘
                  │            │            │
                  ▼            ▼            ▼
            ┌──────────┐ ┌──────────┐ ┌──────────┐
            │ TLS ctx  │ │ TLS ctx  │ │ TLS ctx  │
            │ 280 bytes│ │ 280 bytes│ │ 280 bytes│
            │ + pool   │ │ + pool   │ │ + pool   │
            └──────────┘ └──────────┘ └──────────┘
```

## CLI Activation

Two options activate the thread pool:

| Option | Type | Behavior |
|---|---|---|
| `--split-compile N` | int | Set thread count directly. `0` = CPU count via `sysconf(83)`. `1` = serial (pool not created). `N > 1` = N worker threads. |
| `--allow-expensive-optimizations` | bool | Auto-enabled at `-O2` and above. Enables the thread pool with an automatically determined thread count. |

Both flags ultimately result in a non-zero value at offset +668 in the compilation driver's state block. The driver checks this field to decide between sequential per-kernel iteration and thread pool dispatch.

Two internal options fine-tune pool behavior:

| Option | Type | Purpose |
|---|---|---|
| `--threads-dynamic-scheduling` | bool | Enable dynamic scheduling of thread pool tasks (work stealing vs static partitioning). |
| `--threads-min-section-size` | int | Minimum section size for thread pool work partitioning; prevents excessive task granularity. |

### CPU Count Detection

```c
// sub_1CB1890 -- returns online CPU count
__int64 sub_1CB1890() {
    return sysconf(83);   // _SC_NPROCESSORS_CONF on Linux
}
```

When `--split-compile 0` is specified, the pool constructor receives the return value of `sub_1CB1890()` as its `nmemb` argument.

## Thread-Local Storage (TLS)

The TLS system is the foundation of ptxas's concurrency model. Every thread -- main and workers alike -- gets its own 280-byte context struct, accessed through the single most-called function in the binary: `sub_4280C0` (3,928 call sites).

### Initialization: `ctor_001` (`0x4094C0`)

The TLS key is created in a static constructor that runs before `main`:

```c
// ctor_001 -- .init_array entry
int ctor_001() {
    if (!qword_29FE0A0) {            // one-time guard
        pthread_key_create(&key, destr_function);
        pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
        pthread_mutex_init(&mutex, &attr);
        dword_29FE0F4 = sched_get_priority_max(SCHED_RR);
        dword_29FE0F0 = sched_get_priority_min(SCHED_RR);
        qword_29FE0A0 = &sentinel_node;  // marks "initialized"
        // ... initialize linked list sentinels
    }
    __cxa_atexit(cleanup_func, ...);
}
```

The `SCHED_RR` priority range is queried but never used for thread creation (all threads are created with default attributes). This appears to be vestigial infrastructure for priority-based scheduling that was never activated.

### TLS Accessor: `sub_4280C0`

```c
char *sub_4280C0() {
    if (!qword_29FE0A0)
        goto init_once;              // fallback init (race protection)
    
    char *result = pthread_getspecific(key);
    if (result)
        return result;               // fast path: already allocated
    
    char *ctx = malloc(0x118);       // 280 bytes
    memset(ctx, 0, 0x118);
    pthread_cond_init(ctx + 128, NULL);
    pthread_mutex_init(ctx + 176, NULL);
    sem_init(ctx + 216, 0, 0);
    
    // Insert into global doubly-linked list (under global mutex)
    pthread_mutex_lock(&mutex);
    ctx->prev = sentinel;
    ctx->next = sentinel->next;
    sentinel->next->prev = ctx;
    sentinel->next = ctx;
    pthread_mutex_unlock(&mutex);
    
    pthread_setspecific(key, ctx);
    return ctx;
}
```

The global doubly-linked list at offsets +256 (`prev`) and +264 (`next`) allows the system to enumerate all live TLS contexts. This is used during cleanup to destroy all thread contexts at program exit.

### TLS Context Layout (280 bytes)

The full byte-level layout, verified against the constructor (`sub_4280C0`), destructor (`destr_function`), and the two diagnostic reporters (`sub_42F590`, `sub_42FBA0`):

| Offset | Size | Type | Purpose | Accessor |
|---|---|---|---|---|
| 0 | 1 | `byte` | `has_warning_or_error` flag -- set to 1 when severity > 2 | `sub_42F590` (direct write) |
| 1 | 1 | `byte` | `has_fatal_error` flag -- set to 1 when severity > 4 | `sub_42F590` (direct write) |
| 2 | 6 | -- | _padding_ (zeroed by `memset`) | -- |
| 8 | 8 | `jmp_buf*` | Error recovery longjmp target (installed by `sub_431ED0` before `_setjmp`) | `sub_431ED0` (save/restore) |
| 16 | 8 | `void*` | Error descriptor pointer -- set to the faulting error descriptor on `longjmp` | `sub_42F590` (write on fatal), `sub_431ED0` (propagate on re-throw) |
| 24 | 8 | `void*` | Per-thread memory pool pointer (used by `sub_424070`) | `sub_42BDD0` (swap), `sub_4287D0` (read) |
| 32 | 8 | `char*` | Program name string (e.g. `"ptxas"`) -- prepended to diagnostic messages | `sub_430590` (set), `sub_430570` (get) |
| 40 | 8 | `char*` | Diagnostic suffix string -- appended to message body when non-NULL | `sub_42F590` (read, format as `" %s"`) |
| 48 | 1 | `byte` | Info suppression flag -- suppresses severity-2 (info) messages | `sub_42F590` (check) |
| 49 | 1 | `byte` | Diagnostic suppression flag -- suppresses severity-3 (warning) messages | `sub_430560` (set) |
| 50 | 1 | `byte` | Werror promotion flag (`--Werror`) -- promotes warnings to errors | `sub_430550` (set) |
| 51 | 1 | `byte` | Machine-readable annotation flag -- adds `@E@`/`@W@`/`@O@`/`@I@` severity tags | `sub_4305A0` (get) |
| 52 | 1 | `byte` | Multi-line continuation flag -- suppresses `". "` prefix on wrapped lines | `sub_4305C0` (set) |
| 53 | 75 | -- | _padding_ (zeroed by `memset`) | -- |
| 128 | 48 | `pthread_cond_t` | Per-thread condition variable | constructor: `pthread_cond_init` |
| 176 | 40 | `pthread_mutex_t` | Per-thread mutex | constructor: `pthread_mutex_init` |
| 216 | 32 | `sem_t` | Semaphore for cross-thread signaling | constructor: `sem_init(0, 0)` |
| 248 | 8 | `void*` | Saved semaphore pointer (from pool, for `destr_function` to `sem_post`) | `destr_function` (read at qword[31]) |
| 256 | 8 | `void*` | Linked list `prev` pointer (global TLS chain) | constructor (qword[32]) |
| 264 | 8 | `void*` | Linked list `next` pointer (global TLS chain) | constructor (qword[33]) |
| 272 | 1 | `byte` | Destroyed flag (prevents double-destroy) | `destr_function` (set to 1) |
| 273 | 7 | -- | _padding_ (zeroed by `memset`) | -- |

The fields fall into seven functional groups:

**Error state (offsets 0--1).** Two byte flags set by the diagnostic reporter `sub_42F590`. Byte 0 records whether any error-or-above diagnostic was emitted; byte 1 records fatal errors specifically. The compilation driver reads these after each kernel compilation to determine the process exit code.

**Error recovery (offsets 8--16).** A `setjmp`/`longjmp` mechanism for non-local error exits. `sub_431ED0` saves the current `jmp_buf*` and error byte flags, installs a fresh `jmp_buf`, then enters the compiler. On a fatal diagnostic, `sub_42F590` stores the error descriptor at offset +16 and calls `longjmp` to the target at offset +8. If no `jmp_buf` is installed, the handler calls `sub_4275E0` (abort).

**Per-thread allocator (offset 24).** The most performance-critical field. The pool allocator `sub_424070` reads this pointer on every allocation (accessed as `sub_4280C0()[3]`). When non-NULL, allocations go to the calling thread's own slab without any locking. `sub_42BDD0` provides an atomic swap primitive that replaces the pool pointer and returns the old value -- used during pool migration at compilation boundaries. This is used pervasively: 3,928 call sites to `sub_4280C0` are predominantly pool allocator calls that need the current thread's arena.

**Diagnostic context (offsets 32, 40).** The program name at +32 (e.g. `"ptxas"`) is prepended to all diagnostic messages. The suffix at +40 is appended after the message body. Both are set per-thread to support library mode where multiple tool names coexist in the same process.

**Diagnostic flags (offsets 48--52).** Five single-byte flags controlling diagnostic formatting and filtering. The info suppression flag (+48) silences informational messages. The diagnostic suppression flag (+49) silences warnings entirely. The Werror flag (+50) promotes warnings to errors. The annotation flag (+51) enables machine-readable severity tags (`@E@`, `@W@`, `@O@`, `@I@`). The continuation flag (+52) enables multi-line continuation mode where wrapped lines omit the `". "` prefix.

**Synchronization primitives (offsets 128--248).** The condvar, mutex, and semaphore are used by the thread pool for task coordination and cross-thread signaling. The saved semaphore pointer at +248 is set by the pool when assigning work to a thread; on thread exit, the destructor calls `sem_post` on it to notify the pool's shutdown logic.

**Global linked list (offsets 256--272).** A doubly-linked list threading through all live TLS contexts, protected by the global mutex at `0x29FE0xx`. Used by the atexit handler to enumerate and destroy all contexts. The destroyed flag at +272 prevents double-destroy when contexts move to the free list for recycling.

### TLS Destructor: `destr_function` (`0x427F10`)

When a worker thread terminates, the POSIX TLS destructor fires:

```c
void destr_function(char *ctx) {
    if (!ctx) return;
    
    pthread_mutex_lock(&global_mutex);
    
    if (ctx[272]) {                    // already destroyed?
        pthread_mutex_unlock(&global_mutex);
        return;
    }
    
    sem_t *sem = ctx->saved_semaphore; // offset +248
    
    // Unlink from global TLS chain
    ctx->prev->next = ctx->next;
    ctx->next->prev = ctx->prev;
    
    // Destroy sync primitives
    pthread_cond_destroy(ctx + 128);
    pthread_mutex_destroy(ctx + 176);
    sem_destroy(ctx + 216);
    
    // Move to free list (recycling, not freed)
    ctx[272] = 1;                      // mark destroyed
    ctx->next = free_list_sentinel;
    ctx->prev = free_list_head;
    free_list_head->next = ctx;
    free_list_head = ctx;
    
    pthread_mutex_unlock(&global_mutex);
    
    if (sem)
        sem_post(sem);                 // notify pool that worker exited
}
```

The destructor does not `free()` the 280-byte struct. Instead, it moves it to a free list rooted at a second sentinel node (`unk_29FDC40` / `unk_29FDD60`). This is a deliberate choice: the destructor runs during `pthread_exit()` or thread detachment, and ptxas reuses TLS structs across multiple pool lifetimes within a single process invocation (library mode).

The `sem_post` at the end notifies the pool shutdown code (`sub_1CB1970`) that a worker has fully terminated.

## Thread Pool Implementation

### Pool Struct (184 bytes)

```c
struct ThreadPool {           // calloc(1, 0xB8) = 184 bytes
    void       *thread_handles;  // +0:   array of (pthread_t, 16 bytes each)
    void       *work_queue;      // +8:   priority heap (from sub_1CBEC10)
    int32_t     pending;         // +16:  count of tasks awaiting execution
    // padding
    pthread_mutex_t mutex;       // +24:  guards all mutable pool state (40 bytes)
    pthread_cond_t  cond_work;   // +64:  broadcast when new work arrives (48 bytes)
    pthread_cond_t  cond_done;   // +112: signaled when all work completes (48 bytes)
    int64_t     active_count;    // +160: workers currently executing tasks
    int64_t     max_threads;     // +168: total worker count (= nmemb)
    int8_t      shutdown;        // +176: set to 1 to terminate all workers
};
```

### Constructor: `sub_1CB18B0`

```c
ThreadPool *sub_1CB18B0(size_t nmemb) {
    ThreadPool *pool = calloc(1, 0xB8);            // 184 bytes, zero-init
    pool->thread_handles = calloc(nmemb, 0x10);    // 16 bytes per thread
    pool->max_threads = nmemb;
    pool->pending = 0;
    
    pthread_mutex_init(&pool->mutex, NULL);         // default (non-recursive)
    pthread_cond_init(&pool->cond_work, NULL);
    pthread_cond_init(&pool->cond_done, NULL);
    
    // Create priority heap for task ordering
    pool->work_queue = sub_1CBEC10(sub_1CB1770, 0);
    
    // Spawn nmemb detached worker threads
    for (int i = 0; i < nmemb; i++) {
        pthread_create(&pool->thread_handles[i].tid, NULL,
                       start_routine, pool);
        pthread_detach(pool->thread_handles[i].tid);
    }
    return pool;
}
```

Workers are detached immediately after creation. This means the pool does not use `pthread_join` for cleanup -- instead, it relies on the `cond_done` / `max_threads` countdown protocol in `sub_1CB1970`.

### Work Queue: Priority Heap

The work queue at pool offset +8 is not a simple linked list -- it is a binary min-heap (priority queue) backed by a dynamically-resized array.

**Heap structure (32 bytes):**

| Offset | Size | Type | Field |
|---|---|---|---|
| 0 | 8 | `void**` | Array of element pointers |
| 8 | 8 | `int64` | Current element count |
| 16 | 8 | `int64` | Allocated capacity |
| 24 | 8 | `fn_ptr` | Comparator function |

**Constructor** (`sub_1CBEC10`): Allocates the heap struct from the pool allocator, sets the comparator to `sub_1CB1770` (which always returns 1, making the heap behave as a FIFO -- every parent "beats" every child, so new elements sink to the end).

**Enqueue** (`sub_1CBECC0`): Standard heap push with sift-up. Appends element, then bubbles up through parent comparisons. Auto-grows the backing array (doubles capacity) when full via `sub_424C50` (realloc equivalent).

**Dequeue** (`sub_1CBEDD0`): Standard heap pop with sift-down. Extracts root, moves last element to root, then percolates down via comparator-guided child selection.

Since the comparator `sub_1CB1770` unconditionally returns 1, the heap degenerates into FIFO order. Tasks are dispatched in submission order.

### Task Nodes (24 bytes)

```c
struct TaskNode {              // malloc(0x18) = 24 bytes
    void (*func)(void *arg);   // +0:  task function pointer
    void  *arg;                // +8:  argument passed to func
    void  *reserved;           // +16: zeroed (unused)
};
```

### Task Submit: `sub_1CB1A50`

```c
int sub_1CB1A50(ThreadPool *pool, void (*func)(void*), void *arg) {
    if (!func || !pool)
        return 0;
    
    TaskNode *task = malloc(0x18);     // 24 bytes
    task->func     = func;
    task->arg      = arg;
    task->reserved = NULL;
    
    pthread_mutex_lock(&pool->mutex);
    sub_1CBECC0(task, pool->work_queue);  // heap push
    ++pool->pending;
    pthread_cond_broadcast(&pool->cond_work);
    pthread_mutex_unlock(&pool->mutex);
    return 1;
}
```

The broadcast wakes all sleeping workers, not just one. This is correct for the use case: multiple tasks are typically submitted in a burst (one per kernel), and all idle workers should attempt to pick up work immediately.

### Worker Thread: `start_routine` (`0x1CB1780`)

```c
void *start_routine(ThreadPool *pool) {
    pthread_mutex_t *mtx = &pool->mutex;
    pthread_cond_t  *done = &pool->cond_done;
    
    while (1) {
        pthread_mutex_lock(mtx);
        
        // Wait for work or shutdown
        while (pool->pending == 0 && !pool->shutdown)
            pthread_cond_wait(&pool->cond_work, mtx);
        
        if (pool->shutdown)
            goto exit;
        
        // Dequeue task
        TaskNode *task = (TaskNode *)sub_1CBEDD0(pool->work_queue);
        --pool->pending;
        ++pool->active_count;
        pthread_mutex_unlock(mtx);
        
        // Execute task outside the lock
        if (task) {
            task->func(task->arg);
            free(task);
        }
        
        // Post-execution bookkeeping
        pthread_mutex_lock(mtx);
        --pool->active_count;
        if (!pool->shutdown && pool->active_count == 0 && pool->pending == 0)
            pthread_cond_signal(done);   // all work complete
        pthread_mutex_unlock(mtx);
    }
    
exit:
    --pool->max_threads;               // decrement live worker count
    pthread_cond_signal(done);          // notify shutdown waiter
    pthread_mutex_unlock(mtx);
    return NULL;
}
```

The completion signal on `cond_done` fires only when both `active_count == 0` and `pending == 0`. This is the condition that `sub_1CB1AE0` (wait-all) blocks on.

### Wait-All: `sub_1CB1AE0`

Blocks until all submitted tasks complete:

```c
void sub_1CB1AE0(ThreadPool *pool) {
    if (!pool) return;
    
    pthread_mutex_lock(&pool->mutex);
    while (pool->pending > 0 ||
           (pool->shutdown ? pool->max_threads > 0 : pool->active_count > 0))
        pthread_cond_wait(&pool->cond_done, &pool->mutex);
    pthread_mutex_unlock(&pool->mutex);
}
```

In the non-shutdown case, it waits for `pending == 0 && active_count == 0`. During shutdown, it waits for `max_threads == 0` (all workers have exited).

### Destroy: `sub_1CB1970`

Initiates graceful shutdown:

```c
void sub_1CB1970(ThreadPool *pool) {
    if (!pool) return;
    
    pthread_mutex_lock(&pool->mutex);
    
    // Drain any remaining queued tasks
    sub_1CBEBF0(pool->work_queue);     // free heap contents
    pool->pending = 0;
    pool->shutdown = 1;
    
    // Wake all workers so they see the shutdown flag
    pthread_cond_broadcast(&pool->cond_work);
    pthread_mutex_unlock(&pool->mutex);
    
    // Wait for all workers to exit
    pthread_mutex_lock(&pool->mutex);
    while (pool->pending > 0 ||
           (pool->shutdown ? pool->max_threads > 0 : pool->active_count > 0))
        pthread_cond_wait(&pool->cond_done, &pool->mutex);
    pthread_mutex_unlock(&pool->mutex);
    
    // Destroy synchronization primitives
    pthread_mutex_destroy(&pool->mutex);
    pthread_cond_destroy(&pool->cond_work);
    pthread_cond_destroy(&pool->cond_done);
    free(pool->thread_handles);
    free(pool);
}
```

The shutdown sequence is: (1) drain the queue, (2) set the shutdown flag, (3) broadcast on `cond_work` so all sleeping workers wake up and check the flag, (4) wait on `cond_done` until `max_threads` reaches zero (each exiting worker decrements it), (5) destroy synchronization primitives and free memory.

## Multi-Kernel Parallel Compilation

The compilation driver (`sub_446240`) decides between serial and parallel execution based on the thread count at offset +668:

### Serial Path (default)

```
for each kernel in compile_unit:
    sub_43A400(kernel)          // target configuration
    sub_43CC70(kernel)          // DAGgen → OCG → ELF → DebugInfo
```

### Parallel Path (`--split-compile` / `--allow-expensive-optimizations`)

```
pool = sub_1CB18B0(thread_count)

for each kernel in compile_unit:
    sub_43A400(kernel)          // target config (still serial)
    buf = allocate 360-byte work buffer from pool
    snapshot 15 config vectors into buf
    deep-copy hash maps for thread isolation
    sub_1CB1A50(pool, sub_436DF0, buf)

sub_1CB1AE0(pool)               // block until all kernels done
sub_1CB1970(pool)               // destroy pool
```

Each task runs `sub_436DF0`, which performs the per-kernel backend pipeline:

1. Set thread-local program name via `sub_430590`
2. Acquire jobserver token (if `--jobserver` active): `sub_1CC6EC0()`
3. Record start time
4. `sub_432500` -- run the full DAGgen+OCG pipeline
5. Record end time, write to timing array at `a1->timing[112 * cu_index]`
6. Update peak wall-clock counter (under lock via `sub_607D70` / `sub_607D90`)
7. Release jobserver token: `sub_1CC7040()`
8. Free the 360-byte work buffer

### Thread Isolation Strategy

Each worker thread operates on an isolated copy of compilation state:

| Resource | Isolation Mechanism |
|---|---|
| Memory pool | Per-thread pool pointer at TLS offset +24. Each thread's allocations go to a separate arena, eliminating heap contention. |
| Error state | Per-thread flags at TLS offsets 0--1 (error bytes), 8 (longjmp target), 16 (error descriptor), 48--52 (diagnostic control). Each thread tracks its own errors independently. |
| Hash maps | Deep-copied from the master compilation context before task submission. Workers never share mutable lookup tables. |
| Config vectors | Snapshot of 15 configuration vectors into a 360-byte per-task buffer. Workers read their own copy. |
| Timing data | Per-kernel slots in a pre-allocated timing array (112 bytes per kernel). Each worker writes only to its own kernel's slot. |

The **only shared mutable state** during parallel compilation is the peak wall-clock counter at offset +224 in the compilation driver's state block, protected by a global lock (lock index 6, via `sub_607D70`/`sub_607D90`). This lock is acquired briefly at the end of each per-kernel task to compare-and-update the maximum observed wall-clock time.

## GNU Make Jobserver Integration

When both `--jobserver` and `--split-compile` are active, ptxas participates in GNU Make's parallel job token protocol. The compilation driver creates the jobserver client object before spawning the thread pool, and each per-kernel worker task must acquire a token before starting and release it when done. This throttles ptxas to never exceed the `make -j N` budget, even when `--split-compile` would otherwise use more threads.

### Jobserver Object (296 bytes)

The jobserver state is a 296-byte heap object allocated once per compilation, stored at global `qword_29FE128`. The constructor (`sub_1CC7AF0`) is called from the compilation driver (`sub_4428E0`) when `*(_BYTE*)(context + 993)` is set (the `--jobserver` CLI flag).

| Offset | Size | Type | Field |
|---|---|---|---|
| 0 | 4 | `int32` (atomic) | State code (0=OK; see state table below) |
| 4 | 4 | `int32` | Saved `errno` from last failed syscall |
| 8 | 1 | `byte` | Implicit token available (1=unconsumed) |
| 16 | 8 | `int64` | Pending waiters (threads blocked in acquire) |
| 24 | 8 | `int64` | Active count (tokens currently held) |
| 32 | 8 | `void*` | Token buffer base (`std::vector<char>` data) |
| 40 | 8 | `void*` | Token buffer cursor (stack top) |
| 48 | 8 | `void*` | Token buffer capacity end |
| 56 | 40 | `pthread_mutex_t` | Inner mutex (guards token accounting) |
| 96 | 40 | `pthread_mutex_t` | Write mutex (guards `write()` to Make pipe) |
| 136 | 48 | `pthread_cond_t` | Condition variable (wakes acquire waiters and reader thread) |
| 184 | 1 | `byte` | Token-ready flag (set by reader thread / release handoff) |
| 185 | 1 | `byte` | Last byte read from Make pipe |
| 188 | 4 | `int32` | Read fd (Make pipe/FIFO read end) |
| 192 | 4 | `int32` | Write fd (Make pipe/FIFO write end) |
| 196 | 4 | `int32` | Internal pipe read fd (shutdown wakeup) |
| 200 | 4 | `int32` | Internal pipe write fd (shutdown wakeup) |
| 204 | 1 | `byte` | Opened-fds flag (1=ptxas opened the Make fds itself) |
| 205 | 1 | `byte` | Shutdown flag |
| 208 | 8 | `void*` | Reader thread handle (`std::thread`) |
| 216 | 80 | bytes | Outer mutexes (serializing full acquire/release operations) |

### MAKEFLAGS Parser: `sub_1CC7300`

Called during object construction to detect the Make jobserver channel:

```c
// sub_1CC7300 -- parse MAKEFLAGS, open pipe/FIFO
void sub_1CC7300(JobserverObject *obj) {
    char *flags = getenv("MAKEFLAGS");
    if (!flags) {
        CAS(&obj->state, 5, 0);       // no MAKEFLAGS
        return;
    }
    std::string s(flags);
    size_t pos = s.find("--jobserver-auth=");
    if (pos == npos) {
        CAS(&obj->state, 6, 0);       // no --jobserver-auth=
        return;
    }
    size_t val = pos + 17;             // skip "--jobserver-auth="

    if (s.substr(val, 5) == "fifo:") {
        // --- FIFO mode ---
        std::string path = s.substr(val + 5, next_space);
        int fd = open(path.c_str(), O_RDWR | O_NONBLOCK);  // 0x802
        if (fd == -1) { CAS(&obj->state, 7, 0); return; }
        obj->read_fd  = fd;            // same fd for both
        obj->write_fd = fd;
        obj->opened_fds = 1;
    } else {
        // --- Pipe mode ---
        // parse "R,W" -- e.g. "3,4"
        std::string r_str = s.substr(val, comma_pos - val);
        std::string w_str = s.substr(comma_pos + 1, ...);
        // validate: digits only
        if (r_str.find_first_not_of("0123456789") != npos ||
            w_str.find_first_not_of("0123456789") != npos) {
            CAS(&obj->state, 7, 0); return;
        }
        int rd = dup(stoi(r_str));     // private copy
        if (fcntl(rd, F_SETFD, FD_CLOEXEC) == -1) {
            CAS(&obj->state, 7, 0); return;
        }
        int wd = dup(stoi(w_str));
        if (fcntl(wd, F_SETFD, FD_CLOEXEC) == -1) {
            close(rd);
            CAS(&obj->state, 7, 0); return;
        }
        obj->read_fd  = rd;
        obj->write_fd = wd;
        obj->opened_fds = 1;
    }
}
```

| Protocol | `--jobserver-auth=` value | Detection | fd Setup |
|---|---|---|---|
| FIFO | `fifo:/path/to/fifo` | Prefix match on `fifo:` | `open(path, O_RDWR\|O_NONBLOCK)` -- single fd for both read and write |
| Pipe | `R,W` (e.g. `3,4`) | Comma-separated integers after auth= | `dup()` each fd + `fcntl(F_SETFD, FD_CLOEXEC)` -- prevents fd leak to children |

### Object Construction: `sub_1CC7AF0`

After `sub_1CC7300` succeeds (state == 0), the constructor continues:

1. Creates an **internal wakeup pipe** via `pipe()` -- fds stored at +196/+200
2. Spawns the **reader thread** (`sub_1CC6720`) -- passed as a `std::thread` functor via `off_2406838`
3. Pre-allocates the **token buffer** vector to hold `thread_count` bytes

If state is 5 or 6 (no MAKEFLAGS, no auth string), the caller (`sub_4428E0`) emits: `"GNU Jobserver support requested, but no compatible jobserver found. Ignoring '--jobserver'"` and proceeds without throttling.

### Reader Thread: `sub_1CC6720`

A dedicated background thread that reads tokens from the Make pipe/FIFO and buffers them for the acquire function:

```
loop:
    if state != 0 or shutdown → exit

    lock(mutex_inner)
    while pending_waiters == 0 and not shutdown:
        cond_wait(cond, mutex_inner)     // sleep until someone needs a token
    unlock(mutex_inner)

    fd_set = { read_fd, internal_pipe_read }
    select(max_fd + 1, &fd_set, NULL, NULL, NULL)    // block

    if shutdown → exit

    n = read(read_fd, &byte, 1)
    if n == 1:
        if pending_waiters > 0:
            lock(mutex_inner + mutex_write)
            push byte onto token_buffer
            token_ready = 1
            unlock(mutex_write)
            cond_signal(cond)            // wake one acquire waiter
        else:
            write(write_fd, &byte, 1)    // no waiter → return token immediately
    else if errno == EAGAIN:
        continue                         // expected for non-blocking fd
    else:
        state = 11; exit                 // I/O error
```

The `select()` monitors two fds simultaneously: the Make pipe (for incoming tokens) and the internal wakeup pipe (for shutdown notification). The internal pipe avoids a race between checking the shutdown flag and blocking in `select()`.

### Token Acquire: `sub_1CC6EC0`

Called by each per-kernel worker before compilation begins. Returns 0 on success.

```c
int sub_1CC6EC0(JobserverObject *obj) {
    if (!obj) return 4;
    lock(outer_mutex_0);
    if (obj->state) { unlock; return obj->state; }

    lock(mutex_inner);
    if (obj->implicit_token_available) {
        // Fast path: consume the implicit token (no pipe I/O)
        obj->implicit_token_available = 0;
        obj->active_count++;
        unlock_all;
        return 0;
    }
    // Slow path: wait for reader thread to supply a token
    obj->pending_waiters++;
    if (obj->pending_waiters == 1)
        cond_signal(cond);               // wake reader thread
    while (!obj->token_ready && !obj->shutdown)
        cond_wait(cond, mutex_inner);
    if (obj->shutdown) { unlock_all; return 3; }
    obj->token_ready = 0;
    obj->pending_waiters--;
    obj->active_count++;
    unlock_all;
    return 0;
}
```

The **implicit token** is the standard GNU Make convention: the parent Make gives the first child an implicit token (no byte in the pipe). The first worker to call acquire consumes it for free; subsequent workers must wait for bytes to be read from the pipe.

### Token Release: `sub_1CC7040`

Called by each per-kernel worker after compilation completes. Returns 0 on success.

```c
int sub_1CC7040(JobserverObject *obj) {
    if (!obj) return 4;
    lock(outer_mutex_1);
    if (obj->state) { unlock; return obj->state; }

    lock(mutex_inner);
    lock(mutex_write);
    if (token_buffer not empty) {
        // Path A: write a buffered byte back to Make pipe
        byte = pop(token_buffer);
        if (write(obj->write_fd, &byte, 1) == 1) {
            obj->active_count--;
            unlock_all;
            return 0;
        }
        // write error → set state 11 or 2
    }
    unlock(mutex_write);

    if (obj->pending_waiters > 0) {
        // Path B: hand off directly to a waiting acquirer
        obj->token_ready = 1;
        obj->active_count--;
        cond_signal(cond);
        unlock_all;
        return 0;
    }
    if (!obj->implicit_token_available && obj->active_count == 1) {
        // Path C: return the implicit token
        obj->implicit_token_available = 1;
        obj->active_count = 0;
        unlock_all;
        return 0;
    }
    // Protocol error (double-free)
    CAS(&obj->state, 12, 0);
    unlock_all;
    return 12;
}
```

Release has three paths, in priority order:

| Path | Condition | Action |
|---|---|---|
| A | Token buffer non-empty | Pop byte, `write()` back to Make pipe |
| B | No buffered token but waiters exist | Set `token_ready`, signal condvar (avoids pipe round-trip) |
| C | No buffered token, no waiters, last active | Restore implicit token flag |

### Per-Kernel Worker Integration

In `sub_436DF0` (the per-kernel compilation task submitted to the thread pool):

```c
void sub_436DF0(int64_t *task) {
    sub_430590("ptxas", kernel_name);     // set TLS program name
    if (task[5] && sub_1CC6EC0(task[5]))  // acquire token if jobserver active
        sub_42F590(FATAL);                // acquire failed → fatal error
    // ... sub_432500(): full DAGgen + OCG pipeline ...
    if (!task[5] || !sub_1CC7040(task[5]))  // release token
        return;                             // normal return
    sub_42F590(FATAL);                    // release failed → fatal error
}
```

`task[5]` is populated from `qword_29FE128` during task dispatch in `sub_4428E0`. When `--jobserver` is not active, `task[5] == 0` and both acquire/release calls are skipped.

### Destroy: `sub_1CC6C20`

Called after `sub_1CB1AE0` (wait-all) and `sub_1CB1970` (pool destroy) complete:

1. Set shutdown flag (+205) via `_InterlockedCompareExchange8`
2. Lock inner mutex, signal condvar (wake reader thread), unlock
3. Write 1 byte to internal pipe write end (+200) -- unblocks `select()` in reader thread
4. Join reader thread
5. Lock inner mutex, drain all buffered tokens by writing each byte back to write_fd
6. Unlock inner mutex
7. Close Make fds if `opened_fds` is set (for FIFO: close once since read_fd == write_fd; for pipe: close both if different)
8. Close internal pipe fds (+196, +200)
9. Destroy condvar, free token buffer, free 296-byte object

### State Machine

All state transitions use `_InterlockedCompareExchange(state, new_value, 0)` -- only the first error sticks; subsequent errors are silently dropped.

| State | Meaning | Set by |
|---|---|---|
| 0 | OK (operational) | Constructor |
| 2 | Unexpected I/O (short write/read) | Release, reader thread |
| 5 | No `MAKEFLAGS` environment variable | `sub_1CC7300` |
| 6 | No `--jobserver-auth=` in MAKEFLAGS | `sub_1CC7300` |
| 7 | `open()`/`dup()`/`fcntl()` failed | `sub_1CC7300` |
| 11 | I/O error (errno recorded at +4) | Reader thread, release, constructor |
| 12 | Protocol error (double-free of token) | Release |

### Throttling Semantics

With `make -jN` and `--split-compile M` where M > N:

```
ptxas creates M worker threads in the pool
but only N-1 pipe tokens exist + 1 implicit token = N total
workers that cannot acquire a token block in cond_wait
→ at most N kernels compile simultaneously, matching Make's budget
→ as each kernel finishes and releases its token, a blocked worker wakes
```

Without `--jobserver`, all M workers run freely with no external throttling.

## Pool Allocator Thread Safety

The pool allocator (`sub_424070`) achieves thread safety through a combination of per-thread arenas and a global mutex:

1. **Per-thread arena**: The TLS context at offset +24 holds a pointer to the current thread's memory pool. `sub_424070` reads `sub_4280C0()[3]` to obtain this pointer. When non-NULL, allocations come from the thread-local slab without any locking.

2. **Global pool mutex**: The pool struct contains a `pthread_mutex_t` at offset +7128 (within the ~7,200-byte pool descriptor). This mutex is acquired only for operations that modify the global pool state: slab acquisition from the parent pool, large-block allocation via `mmap`/`malloc`, and pool destruction.

3. **Slab-level lockfree**: Within a thread-local slab (56-byte descriptor), bump-pointer allocation requires no synchronization. The allocator advances a pointer and returns; only when the slab is exhausted does it acquire the global lock to request a new slab.

### Recursive Mutex Pattern

All mutexes created by `sub_428620` (the mutex factory used throughout ptxas) are `PTHREAD_MUTEX_RECURSIVE`:

```c
bool sub_428620(pthread_mutex_t *mutex) {
    pthread_mutexattr_settype(&attr, PTHREAD_MUTEX_RECURSIVE);
    return pthread_mutex_init(mutex, &attr) == 0;
}
```

This is necessary because ptxas functions may re-enter locking code paths through the diagnostic emitter (`sub_42FBA0`) or pool allocator (`sub_424070`), both of which are called from virtually everywhere.

## Global Synchronization Objects

### Global TLS Mutex (`mutex` at `0x29FE0xx`)

Protects the global doubly-linked list of TLS contexts. Acquired during:
- TLS context allocation (`sub_4280C0`)
- TLS context destruction (`destr_function`)
- `sub_4286A0` (explicit lock for cross-thread operations)

This is a recursive mutex (initialized in `ctor_001`).

### Global Lock Array (`sub_607D70` / `sub_607D90`)

A global array of locks accessed by index. Lock index 6 is used to protect the peak wall-clock counter during parallel compilation. The total number of lock indices and their complete purpose map is not fully recovered; index 6 is the only one observed in the threading hot path.

```c
sub_607D70(6);    // acquire lock 6
// update peak wall-clock
sub_607D90(6);    // release lock 6
```

## Function Map

| Address | Size | Callers | Identity |
|---|---|---|---|
| `0x4094C0` | 204 B | 0 | `ctor_001` -- TLS key + global mutex init (`.init_array`) |
| `0x427F10` | 376 B | 0 | `destr_function` -- TLS destructor (via `pthread_key_create`) |
| `0x4280C0` | 597 B | 3,928 | TLS context accessor (280-byte struct, lazy alloc) |
| `0x428600` | 27 B | -- | Mutex destroy + free wrapper |
| `0x428620` | 62 B | -- | Recursive mutex init factory |
| `0x428670` | 6 B | -- | `pthread_mutex_destroy` PLT thunk |
| `0x428680` | 6 B | -- | `pthread_mutex_lock` PLT thunk |
| `0x428690` | 6 B | -- | `pthread_mutex_unlock` PLT thunk |
| `0x4286A0` | 163 B | -- | Global mutex lazy-init + lock |
| `0x1CB1770` | 8 B | 1 | Priority comparator (always returns 1 = FIFO) |
| `0x1CB1780` | 202 B | 0 | `start_routine` -- worker thread main loop |
| `0x1CB1890` | 11 B | -- | CPU count via `sysconf(_SC_NPROCESSORS_CONF)` |
| `0x1CB18B0` | 159 B | -- | Thread pool constructor (184-byte struct) |
| `0x1CB1970` | 168 B | -- | Thread pool graceful destroy |
| `0x1CB1A50` | 90 B | -- | Task submit (24-byte task node, heap push, broadcast) |
| `0x1CB1AE0` | 109 B | -- | Wait-all (block until pending=0, active=0) |
| `0x1CBEBF0` | -- | 1 | Heap drain (free all queued elements) |
| `0x1CBEC10` | -- | 1 | Priority heap constructor (32-byte struct) |
| `0x1CBECC0` | -- | -- | Priority heap push (sift-up) |
| `0x1CBEDD0` | -- | -- | Priority heap pop (sift-down) |
| `0x1CC6720` | ~700 B | 1 | Jobserver reader thread (`select` loop, pushes tokens to buffer) |
| `0x1CC6C20` | ~300 B | 1 | Jobserver destroy (drain tokens, close fds, free 296-byte object) |
| `0x1CC6EC0` | 384 B | 1 | Jobserver token acquire (consume implicit or wait for pipe token) |
| `0x1CC7040` | ~280 B | 1 | Jobserver token release (write byte back or hand off to waiter) |
| `0x1CC7300` | 2,027 B | 1 | Jobserver MAKEFLAGS parser (FIFO vs pipe detection, fd setup) |
| `0x1CC7AF0` | ~700 B | 1 | Jobserver constructor (alloc 296B, spawn reader thread) |

## Cross-References

- [Entry Point & CLI](../pipeline/entry.md) -- `ctor_001` TLS init, `sub_446240` serial vs parallel dispatch
- [CLI Options](../config/cli-options.md) -- `--split-compile`, `--allow-expensive-optimizations`, `--jobserver`
- [Memory Pool Allocator](memory-pools.md) -- per-thread arena via TLS offset +24, global pool mutex at +7128
- [Pipeline Overview](../pipeline/overview.md) -- per-kernel compilation phases run as pool tasks
- [Code Generation Overview](../pipeline/codegen.md) -- `sub_436DF0` per-kernel worker, timing lock 6
