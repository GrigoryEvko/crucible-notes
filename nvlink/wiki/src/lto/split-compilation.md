# Split Compilation

Split compilation parallelizes the PTX-to-SASS assembly step during LTO. After libnvvm compiles linked NVVM IR into a single PTX stream, nvlink can split that PTX into multiple chunks and assemble them concurrently on a thread pool. This is the only point in the nvlink pipeline that uses multi-threading; all other phases (merge, layout, relocation, finalize) are single-threaded.

Two CLI flags control the feature:

| Flag | Global (value) | Global (state) | Meaning |
|---|---|---|---|
| `-split-compile=N` | `dword_2A5B518` | `dword_2A5F260` | Number of NVVM-level splits. Forwarded to libnvvm as `-split-compile=N`. libnvvm produces N separate PTX chunks from a single compiled IR |
| `-split-compile-extended=N` | `dword_2A5B514` | — | Number of threads to use for the ptxas assembly step. When > 1, nvlink spawns a thread pool of N workers to assemble the split PTX chunks in parallel |

Both values are integers. Neither flag has a short-form alias. When `-split-compile-extended` is not specified, `dword_2A5B514` defaults to 1 (single-threaded).

## Value Consensus

Split-compile values originate from the input objects, not the command line. Each fatbin member carries NVVM compilation options as an embedded string. During fatbin extraction (`sub_42AF40` at `0x42AF40`), nvlink parses `-split-compile N` from the embedded options and accumulates a consensus across all input modules using a state machine on `dword_2A5F260`:

| State | Value | Meaning |
|---|---|---|
| 0 | (none) | No module has declared a split-compile value yet |
| 1 | (none) | At least one module had no `-split-compile` option |
| 2 | N | First module declared `-split-compile=N` |
| 3 | N | Modules disagree on presence (some have it, some do not), but the value N is consistent |
| 4 | (conflict) | Two modules declared different split-compile values. Treated as a warning |

```c
for each input module's embedded option string:
    val = parse "-split-compile " from option string
    if val found:
        if state == 0:     state = 2, value = val
        elif state == 1:   state = 3, value = val
        elif state == 2 or state == 3:
            if val != value: state = 4   // conflict
    else:
        if state == 0:     state = 1
        elif state == 2:   state = 3
```

If the final state is 4 (conflict), nvlink emits a warning diagnostic about inconsistent `-split-compile` values across inputs.

## Option Forwarding to libnvvm

The option builder (`sub_426CD0` at `0x426CD0`) constructs the argument array passed to `nvvmCompileProgram`. The split-compile forwarding logic:

```c
if split_compile_extended != 1:
    append "-split-compile-extended=<dword_2A5B514>"
    if split_compile_value == 1:
        // skip -split-compile (implicitly 1)
    elif split_compile_extended != 1:
        // warn: -split-compile vs -split-compile-extended conflict
else:
    if split_compile_value != 1:
        append "-split-compile=<dword_2A5B518>"
```

Both flags are only forwarded when their values differ from the default of 1. When `-split-compile-extended` is active, it takes precedence, and if `-split-compile` is also set to a non-default value that differs, nvlink emits a conflict warning via `sub_467460`.

## Dispatch Paths in main

After libnvvm compilation completes (via `sub_4BC6F0`), nvlink chooses one of three paths based on the compilation mode and `dword_2A5B514`:

### Path 1: Whole-Program, Single-Threaded

When `-device-c` is not set and `split_compile_extended == 1`:

```c
dword_2A5B528 = byte_2A5F225 ? 6 : 0;   // SASS=6, normal=0
options = build_ptxas_options();           // sub_429BA0
result  = ptxas_compile_whole(             // sub_4BD4E0
              &output, ptx_data, sm_version,
              addr64, is_64bit, debug, options, mode);
```

`sub_4BD4E0` at `0x4BD4E0` calls the embedded ptxas to assemble the entire PTX stream in one shot. It uses the same option-setting and compilation API as the split path but expects a single output (the compiled PTX contains one module).

### Path 2: Relocatable, Single-Threaded

When `-device-c` is set and `split_compile_extended == 1`:

```c
options = build_ptxas_options();           // sub_429BA0
result  = ptxas_compile_split(             // sub_4BD760
              &output, ptx_data, sm_version,
              addr64, is_64bit, debug, options, mode);
```

`sub_4BD760` at `0x4BD760` is the split-aware ptxas entry point. When called with a single split, it handles the single-chunk case: set arch, set options, add input, compile, retrieve output.

### Path 3: Extended Split Compile (Multi-Threaded)

When `split_compile_extended > 1`:

```c
// Allocate work items: 40 bytes per split
work_items = arena_alloc(40 * num_splits);       // sub_426AA0
options    = build_ptxas_options();               // sub_429BA0

// Default thread count to nproc if not specified
if (split_compile_extended == 0)
    split_compile_extended = sysconf(_SC_NPROCESSORS_ONLN);  // sub_43FD90

// Create thread pool
pool = thread_pool_create(split_compile_extended);  // sub_43FDB0
if (!pool)
    fatal("Unable to create thread pool");

// Allocate output pointer array
outputs = arena_alloc(8 * num_splits);

// Submit one task per split
for (i = 0; i < num_splits; i++) {
    work_items[i].output_ptr   = &outputs[i];     // offset 0
    work_items[i].ptx_data     = split_ptx[i];    // offset 8
    work_items[i].sm_version   = sm_version;       // offset 16
    work_items[i].addr64       = addr64;           // offset 20
    work_items[i].is_64bit     = is_64bit;         // offset 21
    work_items[i].debug        = debug;            // offset 22
    work_items[i].options      = options;          // offset 24
    work_items[i].mode         = mode;             // offset 32
    thread_pool_submit(pool, split_compile_worker, &work_items[i]);
}

// Wait for all tasks, then tear down pool
thread_pool_wait(pool);     // sub_43FFE0
thread_pool_destroy(pool);  // sub_43FE70

// Merge results
for (i = 0; i < num_splits; i++) {
    check_error(work_items[i].result);            // offset 36
    validate_and_merge(elfw, outputs[i], "lto.cubin");
    if (sm > 89 && needs_mercury)
        post_link_transform(&outputs[i], ...);    // sub_4275C0
    merge_elf(elfw);                               // sub_45E7D0
}
```

Each work item is a 40-byte structure:

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `output_ptr` | Pointer to where the compiled cubin will be stored |
| 8 | 8 | `ptx_data` | Pointer to the split PTX chunk (input) |
| 16 | 4 | `sm_version` | Target SM architecture number |
| 20 | 1 | `addr64` | 64-bit addressing flag |
| 21 | 1 | `is_64bit` | 64-bit machine flag |
| 22 | 1 | `debug` | Debug info flag (`-g`) |
| 24 | 8 | `options` | Pointer to shared ptxas option string |
| 32 | 4 | `mode` | Compilation mode (0=normal, 6=SASS) |
| 36 | 4 | `result` | Return code from `sub_4BD760` (written by worker) |

## Work Item Lifecycle

A work item is a 40-byte structure that carries all inputs needed for one split's ptxas compilation and receives the result. This section traces a single work item through five phases: creation, submission, dequeue, compilation, and result collection.

### Phase 1: Creation (main thread)

After `sub_4BC6F0` (libnvvm compile) returns, the main thread has a contiguous PTX blob and a sizes array (`v362`). It allocates N work items as a contiguous block and an output-pointer array, then populates each work item from globals and per-split data.

```c
// Allocate work item array: 40 bytes * num_splits (arena memory)
work_items_base = arena_alloc(40 * num_splits);    // sub_426AA0

// Allocate output pointer array: 8 bytes * num_splits (arena memory)
outputs = arena_alloc(8 * num_splits);              // sub_426AA0

// Build shared ptxas option string (once, shared across all items)
options = build_ptxas_options();                     // sub_429BA0

// Populate each work item from globals and per-split PTX pointer
cursor = work_items_base;
for (i = 0; i < num_splits; i++) {
    // offset 0: pointer to this split's slot in the outputs array
    *(uint64_t *)(cursor + 0)  = &outputs[i];

    // offset 8: pointer to this split's null-terminated PTX string
    //   (copied from libnvvm's contiguous output into arena memory,
    //    sliced by the sizes array v362)
    *(uint64_t *)(cursor + 8)  = split_ptx[i];

    // offset 16: target SM architecture (e.g. 89, 100)
    *(uint32_t *)(cursor + 16) = dword_2A5F314;     // sm_version

    // offset 20: 64-bit addressing flag (inverted byte_2A5F2C0)
    *(uint8_t  *)(cursor + 20) = (byte_2A5F2C0 == 0);

    // offset 21: 64-bit machine flag (dword_2A5F30C == 64)
    *(uint8_t  *)(cursor + 21) = (dword_2A5F30C == 64);

    // offset 22: debug info flag (inverted byte_2A5F310)
    *(uint8_t  *)(cursor + 22) = (byte_2A5F310 != 0);

    // offset 24: shared ptxas option string (same pointer for all items)
    *(uint64_t *)(cursor + 24) = options;

    // offset 32: compilation mode (0=normal, 6=SASS-only via byte_2A5F225)
    *(uint32_t *)(cursor + 32) = dword_2A5B528;

    // offset 36: result code (uninitialized -- written by worker)
    // Not set during creation; worker writes here

    cursor += 40;
}
```

Key observations from the decompiled main at line 1224--1248:
- The `addr64` flag at offset 20 is the *inverse* of `byte_2A5F2C0` (the `v85 = byte_2A5F2C0 == 0` pattern).
- The `debug` flag at offset 22 is the *inverse* of `byte_2A5F310` (same negation pattern).
- The `options` pointer at offset 24 is shared across all work items. It points to a single option string built by `sub_429BA0`. This is safe because workers only read it.
- The `mode` at offset 32 comes from `dword_2A5B528`, which was set earlier: 6 if `byte_2A5F225` (SASS mode), 0 otherwise.

### Phase 2: Submission (main thread)

Each populated work item is submitted to the thread pool as a `(function, arg)` pair:

```c
for (i = 0; i < num_splits; i++) {
    if (!thread_pool_submit(pool, split_compile_worker, &work_items[i]))
        fatal("Call to ptxjit failed in extended split compile mode");
}
```

Inside `thread_pool_submit` (`sub_43FF50`), each call:

1. Allocates a 24-byte task node via `malloc(0x18)`:
   - Offset 0: function pointer (`split_compile_worker` = `sub_4264B0`)
   - Offset 8: argument pointer (address of this work item within the contiguous block)
   - Offset 16: next pointer (set to NULL, unused by the priority queue)
2. Locks the pool mutex
3. Pushes the task node into the priority queue (`sub_44DD10`)
4. Increments `pending_count`
5. Broadcasts on `task_cond` to wake all sleeping workers
6. Unlocks the pool mutex

The broadcast (not signal) wakes all workers, not just one. With N tasks submitted in rapid succession, this means up to N workers can wake simultaneously on the first submission. Subsequent submissions find workers already awake and dequeuing.

### Phase 3: Dequeue (worker thread)

Each worker thread runs `start_routine` at `0x43FC80` in an infinite loop. On dequeue:

```c
// Worker is holding pool->mutex
task = priority_queue_pop(pool->task_queue);   // sub_44DE20
pool->pending_count--;
pool->active_count++;
pthread_mutex_unlock(&pool->mutex);
```

The task node contains the function pointer and the work item address. The worker calls:

```c
task->func(task->arg);   // split_compile_worker(&work_items[i])
free(task);              // frees the 24-byte malloc'd task node
```

After execution, the worker re-acquires the mutex, decrements `active_count`, and signals `done_cond` if both `active_count` and `pending_count` are zero (the "all done" condition).

### Phase 4: Compilation (worker thread)

`sub_4264B0` (the split-compile worker, 48 bytes) is a thin unpacker. It reads each field from the work item structure at its known offset and forwards them as arguments to `sub_4BD760`:

```c
// sub_4264B0 -- decompiled form
void split_compile_worker(work_item *item) {
    *(int32_t *)((char *)item + 36) = sub_4BD760(
        *(uint64_t *)((char *)item + 0),    // output_ptr
        *(uint64_t *)((char *)item + 8),    // ptx_data
        *(uint32_t *)((char *)item + 16),   // sm_version
        *(uint8_t  *)((char *)item + 20),   // addr64
        *(uint8_t  *)((char *)item + 21),   // is_64bit
        *(uint8_t  *)((char *)item + 22),   // debug
        *(uint64_t *)((char *)item + 24),   // options
        *(uint32_t *)((char *)item + 32)    // mode
    );
}
```

The return value of `sub_4BD760` is an elfLink error code (0--13), written directly into offset 36 of the work item. This is the only field the worker writes; all other fields are read-only inputs.

Inside `sub_4BD760`, the compilation proceeds through the embedded ptxas API:

```text
sub_4CDD60(&ctx)              create compiler context
sub_4CE3B0(ctx, mode)         set compilation mode (0 or 6)
sub_4CE2F0(ctx, sm_version)   set target SM architecture
sub_4CE380(ctx)               enable 64-bit addressing (if addr64)
sub_4CE640(ctx, 1)            enable 64-bit machine mode (if is_64bit)
sub_4CE3E0(ctx, options)      add ptxas option string
sub_4CE070(ctx, ptx_data)     add PTX input data
sub_4CE8C0(ctx)               compile
sub_4CE670(ctx, &buf, &count, &size)  retrieve output chunks
```

After compilation, `sub_4CE670` returns the output chunk count. Two paths:

**Multi-chunk path** (count != 1): The PTX was split by libnvvm. The function enters the `setjmp`-protected output copy path. It allocates arena memory via `sub_4307C0`, copies the compiled binary with `memcpy`, and stores the pointer through `output_ptr` (writing to `outputs[i]`). The `setjmp`/`longjmp` wrapper catches arena allocation failures or memcpy errors and maps them to error code 1 (`ELFLINK_INTERNAL`).

**Single-chunk fallback** (count == 1): The PTX was not actually split (e.g., libnvvm decided splitting was not beneficial). The function adds extra architecture options (`-m64` or `-m32`) and a debug option (address `30616008` which is the string `"-g"`), then re-retrieves via `sub_4BE350`. This fallback handles the case where split-compile was requested but libnvvm produced only one chunk.

Return codes from `sub_4BD760`:

| Code | Condition |
|---:|---|
| 0 | Success, output written to `*output_ptr` |
| 1 | Success with warning (longjmp recovery set a warning flag) |
| 5 | Compilation failed (`sub_4CE8C0` error, or output retrieval failed, or option-setting failed) |
| 7 | NVVM error (`sub_4CE8C0` returned 3, mapping to `ELFLINK_NVVM_ERROR`) |
| 8 | Output not relocatable (cubin retrieval via `sub_4BE350` failed and no error string) |

### Phase 5: Result Collection (main thread)

After all submissions, the main thread waits for completion and then iterates:

```c
thread_pool_wait(pool);       // sub_43FFE0 -- blocks until active==0 && pending==0
thread_pool_destroy(pool);    // sub_43FE70 -- shutdown flag, wait for threads, free

for (i = 0; i < num_splits; i++) {
    // 1. Check error code at offset 36 of each work item
    check_ptxas_error(work_items_base[i * 40 + 36], "<lto ptx>");  // sub_4297B0

    // 2. Retrieve compiled cubin from output array
    cubin = outputs[i];

    // 3. Validate and add to output ELF
    if (!validate_and_add(elfw, cubin, "lto.cubin", &is_mercury))  // sub_426570
        fatal("Ptxjit compilation failed in extended split compile mode");

    // 4. Mercury post-link transform (sm > 89 only)
    if (sm_version > 0x59 && (!sass_mode || needs_mercury(cubin)) && !is_mercury)
        mercury_post_link(&cubin, "lto.cubin", sm_version, &env, 0);  // sub_4275C0

    // 5. Merge into output ELF
    merge_elf(elfw);           // sub_45E7D0

    // 6. Free the split PTX string (arena memory)
    arena_free(split_ptx[i]);  // sub_431000
}

// Free the work items block and split PTX pointer array
arena_free(work_items_base);   // sub_431000
arena_free(split_ptx_array);   // sub_431000
```

The error check at step 1 (`sub_4297B0`) handles three cases:
- Code 0: success, no action
- Code 7 (`ELFLINK_NVVM_ERROR`): emits a fatal diagnostic unless `byte_2A5F298` is set (cudadevrt tolerance mode) or the source contains "cudadevrt"
- All other non-zero codes: translates via `sub_4BC270` to a human-readable string and emits a fatal diagnostic

Steps 3--5 run serially in the main thread. The `validate_and_add` function (`sub_426570`) performs architecture verification: it checks the compiled cubin's ELF headers against the target SM, validates the machine class (32/64-bit), and verifies format compatibility. The Mercury post-link step (`sub_4275C0`) is only invoked for sm > 89 (Blackwell and later) and transforms the cubin for the Mercury execution model.

### Lifecycle Diagram

```text
main thread                          worker threads (N)
===========                          ==================

libnvvm compile
  sub_4BC6F0()
    |
    v
split PTX into N chunks
    |
    v
allocate work_items[N]               (sleeping on task_cond)
allocate outputs[N]
    |
    v
for each split i:
  populate work_items[i]
  submit(worker, &items[i])  ------->  wake on task_cond
    |                                  dequeue task
    |                                  call split_compile_worker()
    |                                    read offsets 0..32
    |                                    sub_4BD760() -- ptxas compile
    |                                    write offset 36 (result)
    |                                    write *output_ptr (cubin)
    |                                  free task node
    |                                  decrement active_count
    |                                  signal done_cond if all done
    v                                    |
thread_pool_wait()  <----(done_cond)----+
thread_pool_destroy()
    |
    v
for each split i:
  check work_items[i].result
  validate outputs[i]
  mercury transform (if sm>89)
  merge into output ELF
  free split PTX[i]
    |
    v
free work_items, free split_ptx_array
```

### Thread Safety Notes

The design achieves thread safety through strict partitioning:

1. **No shared mutable state between workers.** Each worker reads from its own 40-byte work item and writes to two locations: offset 36 (result code) in its own item, and `*output_ptr` (its own slot in the output array). No two workers touch the same memory.

2. **Read-only shared data.** The `options` pointer at offset 24 points to a single string built by the main thread before any submission. All workers read it; none write it.

3. **Arena allocator thread safety.** `sub_4BD760` allocates output memory via `sub_4307C0` (arena allocator). The arena uses per-memspace mutexes (visible at offset 7128 in the memspace structure in `sub_431000`), so concurrent arena allocations from different workers are safe.

4. **No synchronization inside the worker.** `sub_4264B0` contains no locks, atomics, or synchronization primitives. It is a pure function of its input (the work item pointer) plus the thread-safe ptxas API calls.

5. **The `setjmp`/`longjmp` in `sub_4BD760` is thread-local.** The `env` buffer is on the stack of each worker thread, so concurrent longjmps do not interfere.

## Worker Function

`sub_4264B0` at `0x4264B0` is the per-split worker. It unpacks the 40-byte work item and calls `sub_4BD760`:

```c
void split_compile_worker(work_item *item) {
    item->result = ptxas_compile_split(
        item->output_ptr,        // offset 0
        item->ptx_data,          // offset 8
        item->sm_version,        // offset 16
        item->addr64,            // offset 20
        item->is_64bit,          // offset 21
        item->debug,             // offset 22
        item->options,           // offset 24
        item->mode               // offset 32
    );
}
```

`sub_4BD760` at `0x4BD760` is the split-compile ptxas entry point. Unlike the whole-program `sub_4BD4E0`, it produces multiple output chunks when libnvvm has generated split PTX. The function:

1. Creates a ptxas compiler context (`sub_4CDD60`)
2. Sets the target architecture (`sub_4CE3B0`, `sub_4CE2F0`)
3. Optionally sets addr64 (`sub_4CE380`), 64-bit mode (`sub_4CE640`), and debug (`sub_4CE310`)
4. Adds compilation options (`sub_4CE3E0`)
5. Adds the PTX input data (`sub_4CE070`)
6. Runs compilation (`sub_4CE8C0`)
7. Retrieves output via `sub_4CE670` — checks the output count (`v35`)
8. If count == 1: the PTX was not split, adds architecture-specific options (`-m32`/`-m64`) and re-retrieves via `sub_4BE350`
9. Copies the compiled binary into arena memory and returns it through the output pointer

Error handling uses `setjmp`/`longjmp` to catch compilation failures and return status codes.

## Thread Pool Implementation

The thread pool is a custom implementation using pthreads. The pool control structure is 184 bytes (0xB8):

| Offset | Size | Field | Description |
|---|---|---|---|
| 0 | 8 | `thread_array` | Pointer to array of 16-byte thread entries `[slot, pthread_t]` |
| 8 | 8 | `task_queue` | Priority queue for pending tasks (min-heap) |
| 16 | 4 | `pending_count` | Number of tasks queued but not yet dequeued |
| 24 | 40 | `mutex` | `pthread_mutex_t` protecting the pool state |
| 64 | 48 | `task_cond` | `pthread_cond_t` signaled when a task is available |
| 112 | 48 | `done_cond` | `pthread_cond_t` signaled when a task completes or pool is shutting down |
| 160 | 8 | `active_count` | Number of workers currently executing a task |
| 168 | 8 | `thread_count` | Number of live worker threads (decremented as threads exit) |
| 176 | 1 | `shutdown` | Shutdown flag; set to 1 to signal workers to exit |

### thread_pool_get_nproc (`sub_43FD90` at `0x43FD90`)

Returns the number of online processors via `sysconf(_SC_NPROCESSORS_ONLN)` (sysconf parameter 83). Used as the default thread count when `-split-compile-extended` is specified without a value (or is 0).

### thread_pool_create (`sub_43FDB0` at `0x43FDB0`)

```c
pool_t *thread_pool_create(size_t num_threads) {
    pool = calloc(1, 0xB8);                        // 184-byte control block
    pool->thread_array = calloc(num_threads, 16);   // 16 bytes per thread
    pool->thread_count = num_threads;
    pool->pending_count = 0;
    pthread_mutex_init(&pool->mutex, NULL);
    pthread_cond_init(&pool->task_cond, NULL);
    pthread_cond_init(&pool->done_cond, NULL);
    pool->task_queue = priority_queue_create(comparator_always_true, 0);

    for (i = 0; i < num_threads; i++) {
        pthread_create(&pool->thread_array[i].thread, NULL, worker_main, pool);
        pthread_detach(pool->thread_array[i].thread);
    }
    return pool;
}
```

All threads are created detached. The priority queue uses `sub_43FC70` as its comparator, which always returns 1 — this makes the heap behave as a FIFO queue (insertion order preserved since all priorities are "equal").

### thread_pool_submit (`sub_43FF50` at `0x43FF50`)

```c
int thread_pool_submit(pool_t *pool, void (*func)(void *), void *arg) {
    if (!func || !pool) return 0;
    task = malloc(24);         // 24-byte task node
    task->func = func;         // offset 0: function pointer
    task->arg  = arg;          // offset 8: argument pointer
    task->next = NULL;         // offset 16: unused (queue manages ordering)

    pthread_mutex_lock(&pool->mutex);
    priority_queue_push(task, pool->task_queue);  // sub_44DD10
    pool->pending_count++;
    pthread_cond_broadcast(&pool->task_cond);     // wake all waiting workers
    pthread_mutex_unlock(&pool->mutex);
    return 1;
}
```

The task node is 24 bytes, heap-allocated with `malloc` (not the arena allocator). The queue push (`sub_44DD10`) inserts into a min-heap backed by a dynamic array with doubling growth. Since the comparator always returns 1, elements are dequeued in insertion order.

### worker_main (`start_routine` at `0x43FC80`)

```c
void *worker_main(pool_t *pool) {
    while (1) {
        pthread_mutex_lock(&pool->mutex);

        // Wait for work
        while (pool->pending_count == 0) {
            if (pool->shutdown) goto exit;
            pthread_cond_wait(&pool->task_cond, &pool->mutex);
        }

        // Dequeue task
        task = priority_queue_pop(pool->task_queue);  // sub_44DE20
        pool->pending_count--;
        pool->active_count++;
        pthread_mutex_unlock(&pool->mutex);

        // Execute task
        if (task) {
            task->func(task->arg);
            free(task);
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

The worker loops indefinitely, sleeping on `task_cond` when no work is available. When the shutdown flag is set, it decrements the thread count and signals `done_cond` before exiting. The completion signal at the end of each task is only fired when both `active_count` and `pending_count` are zero — this is the condition that `thread_pool_wait` blocks on.

### thread_pool_wait (`sub_43FFE0` at `0x43FFE0`)

```c
void thread_pool_wait(pool_t *pool) {
    if (!pool) return;
    pthread_mutex_lock(&pool->mutex);
    while (1) {
        if (pool->pending_count == 0) {
            if (pool->shutdown) {
                if (pool->thread_count == 0) break;
            } else {
                if (pool->active_count == 0) break;
            }
        }
        pthread_cond_wait(&pool->done_cond, &pool->mutex);
    }
    pthread_mutex_unlock(&pool->mutex);
}
```

Two wait modes: during normal operation, waits until `pending_count == 0 && active_count == 0` (all submitted tasks finished). During shutdown, waits until `pending_count == 0 && thread_count == 0` (all threads exited).

### thread_pool_destroy (`sub_43FE70` at `0x43FE70`)

```c
void thread_pool_destroy(pool_t *pool) {
    if (!pool) return;

    // Signal shutdown
    pthread_mutex_lock(&pool->mutex);
    priority_queue_destroy(pool->task_queue);  // sub_44DC40
    pool->pending_count = 0;
    pool->shutdown = 1;
    pthread_cond_broadcast(&pool->task_cond);  // wake all workers
    pthread_mutex_unlock(&pool->mutex);

    // Wait for all threads to exit
    pthread_mutex_lock(&pool->mutex);
    while (pool->pending_count != 0 || pool->thread_count != 0)
        pthread_cond_wait(&pool->done_cond, &pool->mutex);
    pthread_mutex_unlock(&pool->mutex);

    // Cleanup
    pthread_mutex_destroy(&pool->mutex);
    pthread_cond_destroy(&pool->task_cond);
    pthread_cond_destroy(&pool->done_cond);
    free(pool->thread_array);
    free(pool);
}
```

The destroy sequence is two-phase: first set the shutdown flag and broadcast to wake all sleeping workers, then wait for every thread to decrement `thread_count` to zero. Only then are the synchronization primitives destroyed and memory freed. The pool itself and the thread array are `calloc`/`free`-managed (not arena-allocated), matching the `calloc` in `thread_pool_create`.

## Priority Queue

The task queue (`sub_44DC60` / `sub_44DD10` / `sub_44DE20`) is a binary min-heap stored in a dynamic array:

| Offset | Size | Field |
|---|---|---|
| 0 | 8 | `array` — pointer to element pointer array |
| 8 | 8 | `count` — number of elements |
| 16 | 8 | `capacity` — allocated slots |
| 24 | 8 | `comparator` — function pointer for ordering |

Push (`sub_44DD10`) inserts at the end and sifts up. Pop (`sub_44DE20`) moves the last element to position 0 and sifts down. The comparator `sub_43FC70` always returns 1, so every parent is always "less than or equal" to its children — the heap degenerates into approximate FIFO behavior. Growth doubles the capacity when full, using `sub_4313A0` (arena realloc).

## Error Handling

After all tasks complete:

```c
for (i = 0; i < num_splits; i++) {
    sub_4297B0(work_items[i].result, "<lto ptx>");  // check error code
    output = outputs[i];
    if (!validate_and_add(elfw, output, "lto.cubin", &is_mercury))
        fatal("Ptxjit compilation failed in extended split compile mode");
    // Mercury post-link if sm > 89
    merge_elf(elfw);
}
```

`sub_4297B0` checks the return code from `sub_4BD760`. A non-zero result for any split causes a fatal error. Each compiled split is independently validated against the target architecture, potentially transformed by the Mercury finalizer, and then merged into the output ELF.

## Function Map

| Address | Name | Size | Role |
|---|---|---|---|
| `0x43FD90` | `thread_pool_get_nproc` | 18 B | Returns CPU count via `sysconf(83)` |
| `0x43FDB0` | `thread_pool_create` | 416 B | Allocates pool, spawns N detached worker threads |
| `0x43FC80` | `worker_main` | 272 B | Worker loop: dequeue, execute, signal completion |
| `0x43FF50` | `thread_pool_submit` | 144 B | Enqueues a `(function, arg)` task into the pool |
| `0x43FFE0` | `thread_pool_wait` | 128 B | Blocks until all submitted tasks complete |
| `0x43FE70` | `thread_pool_destroy` | 224 B | Signals shutdown, waits for threads, frees resources |
| `0x4264B0` | `split_compile_worker` | 48 B | Unpacks work item, calls `sub_4BD760` |
| `0x4BD760` | `ptxas_compile_split` | 2,656 B | Split-aware ptxas entry: compile one PTX chunk to cubin |
| `0x4BD4E0` | `ptxas_compile_whole` | 640 B | Whole-program ptxas entry: compile single PTX to cubin |
| `0x426CD0` | `lto_option_builder` | 4,768 B | Builds libnvvm argument array, forwards split-compile flags |
| `0x43FC70` | `comparator_true` | 8 B | Priority queue comparator, always returns 1 |
| `0x44DC60` | `pqueue_create` | 192 B | Creates priority queue with comparator and initial capacity |
| `0x44DD10` | `pqueue_push` | 224 B | Inserts element, sifts up |
| `0x44DE20` | `pqueue_pop` | 288 B | Removes root element, sifts down |
| `0x4297B0` | `check_ptxas_error` | ~320 B | Checks elfLink return code, emits fatal diagnostic on failure |
| `0x426570` | `validate_and_add` | ~1,200 B | Validates compiled cubin arch/class, adds to output ELF writer |
| `0x4275C0` | `mercury_post_link` | ~512 B | Mercury finalizer post-link transform (sm > 89) |
| `0x431000` | `arena_free` | ~720 B | Returns arena-allocated memory to the memspace free list |
| `0x4BC6F0` | `libnvvm_compile` | — | Compiles linked NVVM IR, produces PTX and split metadata |

## Key Globals

| Address | Name | Type | Description |
|---|---|---|---|
| `dword_2A5B514` | `split_compile_extended` | `int` | Thread count for extended split compile. Default 1 (single-threaded). Value 0 triggers auto-detect via `sysconf` |
| `dword_2A5B518` | `split_compile_value` | `int` | Number of splits requested from libnvvm |
| `dword_2A5F260` | `split_compile_state` | `int` | Consensus state machine: 0=none, 1=absent, 2=present, 3=mixed, 4=conflict |

## Cross-References

- [LTO Overview](overview.md) — pipeline context showing where split compilation fits (Step 3: PTX Assembly)
- [Option Forwarding](option-forwarding.md) — how `-split-compile` and `-split-compile-extended` are forwarded to cicc
- [libnvvm Integration](libnvvm-integration.md) — libnvvm produces the PTX that split compilation parallelizes
- [Whole vs Partial LTO](whole-vs-partial.md) — split compilation interacts with partial mode (Path 3 dispatch)
- [Merge Phase](../pipeline/merge.md) — per-split cubins are merged via `merge_elf` after assembly
