# Concurrent Compilation

CICC implements a **two-phase concurrent compilation model** that is entirely absent from upstream LLVM. The optimizer runs twice over the same module: Phase I performs whole-module analysis and early IR optimizations on a single thread, then Phase II runs per-function backend optimization in parallel across a thread pool. The design exploits the fact that most backend passes (instruction selection prep, register pressure reduction, peephole) are function-local and do not require cross-function information once Phase I has completed interprocedural analysis.

The two-phase protocol lives in `sub_12E7E70` (2,118 bytes native), which calls the same master pipeline function `sub_12E54A0` twice, discriminated only by a TLS phase counter. The concurrency infrastructure spans the `0x12D4000`--`0x12EA000` address range and includes a GNU Make jobserver integration for build-system-aware parallelism throttling — a feature that allows `make -j8` to correctly limit total system load even when each cicc invocation itself wants to spawn threads.

| | |
|---|---|
| **Phase I/II orchestrator** | `sub_12E7E70` (2,118 bytes native) |
| **Phase counter (TLS)** | `qword_4FBB3B0` — values 1, 2, 3 |
| **Concurrency eligibility** | `sub_12D4250` (161 bytes native) |
| **Function sorting** | `sub_12E0CA0` (4,678 bytes native) |
| **Concurrent entry** | `sub_12E1EF0` (10,509 bytes native) |
| **Worker entry** | `sub_12E7B90` (732 bytes native) |
| **Per-function callback** | `sub_12E8D50` |
| **Per-function optimizer** | `sub_12E86C0` (1,665 bytes native) |
| **GNU jobserver init** | `sub_16832F0` |
| **MAKEFLAGS parser** | `sub_1682BF0` |
| **Thread pool create** | `sub_16D4AB0` |
| **Thread pool enqueue** | `sub_16D5230` |
| **Thread pool join** | `sub_16D4EC0` |
| **Disable env var** | `LIBNVVM_DISABLE_CONCURRENT_API` — `byte_4F92D70` |
| **Pipeline function** | `sub_12E54A0` (9,968 bytes native) — called by both phases |

## Two-Phase Architecture

Both phases call the same optimization pipeline function `sub_12E54A0(context, input, output, opts, errCb)`. The only difference is the value stored in the TLS variable `qword_4FBB3B0` before each call. Individual optimization passes read this TLS variable to decide whether to run: Phase I passes fire when the counter equals 1; Phase II passes fire when it equals 2. This avoids running codegen-oriented passes during analysis and vice versa.

### Phase Counter Protocol

The phase counter `qword_4FBB3B0` is a TLS variable accessed via `sub_16D40E0` (set) and `sub_16D40F0` (get). It stores a pointer to a heap-allocated 4-byte integer. Three values are defined:

| Value | Meaning | Set point |
|-------|---------|-----------|
| 1 | Phase I active — analysis + early IR optimization | Before first `sub_12E54A0` call |
| 2 | Phase II active — backend optimization + codegen prep | Before second `sub_12E54A0` call |
| 3 | Compilation complete for this module | After second `sub_12E54A0` returns |

### Sequential Path (sub_12E7E70)

When verbose logging is disabled and the module contains only one defined function, the orchestrator takes a fast path:

```c
// Single-function fast path: no phase counter set at all
if (!verbose && num_defined_functions <= 1) {
    sub_12E54A0(ctx, input, output, opts, errCb);  // single un-phased call
    return;
}
```

This means the optimizer runs both phases in a single invocation — passes see no phase counter and run unconditionally. For multi-function modules or when verbose logging is active, the full two-phase protocol engages:

```c
// Phase I
int *phase = malloc(4);
*phase = 1;
tls_set(qword_4FBB3B0, phase);
sub_12E54A0(ctx, input, output, opts, errCb);

if (error_reported(errCb))
    return;  // abort on Phase I error

// Concurrency decision
bool concurrent = sub_12D4250(ctx, opts);
// Diagnostic: "Concurrent=Yes" or "Concurrent=No"

// Phase II
*phase = 2;
tls_set(qword_4FBB3B0, phase);
sub_12E54A0(ctx, input, output, opts, errCb);

// Done
*phase = 3;
tls_set(qword_4FBB3B0, phase);
```

The diagnostic string construction between phases is notable: `v46 = 3LL - (v41 == 0)` computes the length of "Yes" (3) vs "No" (2, but expression yields 2 via `3 - 1`), then logs `"Phase II"` with the `"Concurrent=Yes/No"` annotation appended.

### Concurrent Path (sub_12E7B90)

When the thread count exceeds 1, the orchestrator dispatches to `sub_12E7B90` instead of running Phase II sequentially:

```text
sub_12E7B90(ctx, module_ptr, thread_count, opts, ...)
    |
    |-- Phase I: *phase=1, sub_12E54A0(...)        // whole-module, single thread
    |-- sub_12D4250(ctx, opts)                     // eligibility check
    |
    +-- if eligible (>1 defined function):
    |     sub_12E1EF0(...)                         // concurrent Phase II
    |     *phase = 3
    |
    +-- else (single defined function):
          *phase = 2, sub_12E54A0(...)             // sequential Phase II
          *phase = 3
```

Phase I always runs single-threaded on the whole module because interprocedural analyses (alias analysis, call graph construction, inlining decisions) require a consistent global view. Only after Phase I completes does the system split the module into per-function chunks for parallel Phase II processing.

## Eligibility Check

`sub_12D4250` (161 bytes native) determines whether the module qualifies for concurrent compilation. The check is straightforward:

```c
int sub_12D4250(Module *mod, Options *opts) {
    int defined_count = 0;
    for (Function &F : mod->functions()) {
        if (!sub_15E4F60(&F))    // !isDeclaration()
            defined_count++;
    }
    if (defined_count <= 1)
        return 0;                // not eligible: only 0 or 1 defined function

    byte force = *(byte*)(opts + 4064);   // NVVMPassOptions slot 201 (0xC9)
    if (force != 0)
        return force;            // user-forced concurrency setting

    return sub_12D3FC0(mod, opts);  // auto-determine thread count
}
```

The key gate is `defined_count > 1`. A module with a single kernel and no device functions will always compile sequentially regardless of thread count settings. The `opts + 4064` byte (NVVMPassOptions slot 201, type `BOOL_COMPACT`, default 0) allows the user to force concurrent mode on or off. When zero (default), `sub_12D3FC0` auto-determines the thread count based on module characteristics.

## Function Priority Sorting

Before distributing functions to worker threads, `sub_12E0CA0` (4,678 bytes native) sorts them by compilation priority. This step is critical for load balancing: larger or more complex functions should start compiling first so they don't become tail stragglers.

### Sorting Algorithm

The sort uses a hybrid strategy consistent with libstdc++ `std::sort`:

| Input size | Algorithm | Function |
|------------|-----------|----------|
| Small N | Insertion sort | `sub_12D48A0` |
| Large N | Introsort (quicksort + heapsort fallback) | `sub_12D57D0` |

The threshold between insertion sort and introsort is 256 bytes of element data (consistent with the libstdc++ template instantiation pattern observed elsewhere in the binary).

### Priority Source

Priority values come from function attributes extracted by `sub_12D3D20` (145 bytes native). The sorted output is a vector of `(name_ptr, name_len, priority)` tuples with 32-byte stride, used directly by the per-function dispatch loop to determine compilation order. Functions with higher priority (likely larger or more critical kernels) are submitted to the thread pool first.

```c
/* Function priority enumeration + sort, sub_12E0CA0 (4,678 bytes native).
 * Output is a contiguous array of 32-byte records; the dispatch loop
 * walks this array in priority-descending order. */
typedef struct {
    const char *name_ptr;     /* +0  -- function name string         */
    size_t      name_len;     /* +8  -- length (excludes NUL)        */
    int64_t     priority;     /* +16 -- larger == compile first      */
    void       *fn;           /* +24 -- internal Function pointer    */
} SortRec;                    /* 32-byte stride                      */

void sortFunctionsByPriority(Module *M, SortRec **out, size_t *out_n) {
    /* Phase A: enumerate via iterator callback table */
    Vec(SortRec) v = vec_new(/*stride=*/32);
    for (Node *n = M->first; !iter_end(n); n = iter_next(n)) {
        if (*(uint8_t *)((char *)n + 16) != 0) continue;       /* type 0 = Function */
        if (sub_15E4F60(n)) continue;                          /* isDeclaration()   */
        SortRec r = {
            .name_ptr = sub_1649960(n),
            .name_len = strlen(r.name_ptr),
            .priority = sub_12D3D20(n),                        /* attr query        */
            .fn       = n,
        };
        vec_push(&v, &r);
        hashmap_insert(M->name2fn, r.name_ptr, n);             /* v359 in caller    */
    }

    /* Phase B: libstdc++-style hybrid sort (descending priority).
     * Threshold 256 bytes ~= 8 records of stride 32. */
    if (v.bytes <= 256)
        sub_12D48A0(v.data, v.bytes, /*stride=*/32, cmp_priority_desc);
    else
        sub_12D57D0(v.data, v.bytes, /*stride=*/32, cmp_priority_desc);

    *out   = v.data;
    *out_n = v.bytes / 32;
}

static int cmp_priority_desc(const void *a, const void *b) {
    int64_t pa = ((const SortRec *)a)->priority;
    int64_t pb = ((const SortRec *)b)->priority;
    return (pa < pb) - (pa > pb);   /* larger priority first */
}
```

### Enumeration Phase

Before sorting, `sub_12E0CA0` enumerates all functions and globals via an iterator callback table:

| Callback | Address | Purpose |
|----------|---------|---------|
| Next function | `sub_12D3C60` | Advance to next function in module |
| Iterator advance | `sub_12D3C80` | Step iterator forward |
| End check | `sub_12D3CA0` | Test if iterator reached end |

For each function, the enumeration:
1. Checks the node type discriminator at `*(byte*)(node + 16)` — type 0 = Function, type 1 = GlobalVariable
2. For functions: calls `sub_15E4F60` (isDeclaration check), `sub_12D3D20` (priority), `sub_1649960` (name), inserts into `v359` hash table (name to function) and `v362` hash table (name to linkage type)
3. For global variables: walks the parent/linked GlobalValue chain via `sub_164A820`, inserts callee references into `v365` hash table for split-module tracking

## GNU Jobserver Integration

When `cicc` is invoked by GNU Make with `-j`, it can participate in the make jobserver protocol to avoid oversubscribing the system. The jobserver flag is passed from `nvcc` via the `-jobserver` CLI flag, which sets `opts + 3288` (NVVMPassOptions slot 163, type `BOOL_COMPACT`, default 0).

### Initialization (sub_16832F0)

The jobserver init function allocates a 296-byte state structure and calls `sub_1682BF0` to parse the `MAKEFLAGS` environment variable:

```c
int sub_16832F0(JobserverState *state, int reserved) {
    memset(state, 0, 296);
    state->flags[8] = 1;                    // initialized marker

    int err = sub_1682BF0(state);            // parse MAKEFLAGS
    if (err) return err;

    pipe(state->local_pipe);                 // local token pipe
    // state+196 = read FD, state+200 = write FD

    pthread_create(&state->thread,           // state+208
                   NULL, token_manager, state);

    reserve_vector(state, token_count);
    return 0;  // success
}
```

### MAKEFLAGS Parsing (sub_1682BF0)

The parser searches the `MAKEFLAGS` environment variable for `--jobserver-auth=` and supports two formats:

| Format | Example | Mechanism |
|--------|---------|-----------|
| Pipe FDs | `--jobserver-auth=3,4` | Read FD = 3, Write FD = 4 (classic POSIX pipe) |
| FIFO path | `--jobserver-auth=fifo:/tmp/gmake-jobserver-12345` | Named FIFO (GNU Make 4.4+) |

The pipe format uses comma-separated read/write file descriptors inherited from the parent make process. The FIFO format uses a named pipe in the filesystem. In both cases, the jobserver protocol works the same way: a thread reads tokens from the pipe/FIFO before starting each per-function compilation, and writes tokens back when the function completes. This ensures cicc never runs more concurrent compilations than make's `-j` level permits.

### Error Handling

```c
if (jobserver_init_error) {
    if (error_code == 5 || error_code == 6) {
        // Warning: jobserver pipe not accessible (probably not in make context)
        emit_warning(severity=1);
        // Fall through: continue without jobserver
    } else {
        // Fatal: "GNU Jobserver support requested, but an error occurred"
        sub_16BD130("GNU Jobserver support requested, but an error occurred", 1);
    }
}
```

Error codes 5 and 6 are non-fatal (the jobserver pipe may not be available if cicc is invoked outside a make context). All other errors are fatal.

## Thread Pool Management

### Creation (sub_16D4AB0)

The thread pool is LLVM's standard `ThreadPool` (the binary contains `"llvm-worker-{0}"` thread naming at `sub_23CE0C0`). Creation occurs at line 799 of `sub_12E1EF0`:

```c
int actual_threads = min(requested_threads, num_functions);
sub_16D4AB0(thread_pool, actual_threads);
```

The thread count is clamped to the number of functions — there is no point spawning more threads than there are work items.

### Thread Count Resolution

Thread count is resolved through a fallback chain in `sub_12E7E70`:

```c
int thread_count = opts[1026];    // NVVMPassOptions slot 203 (offset 4104), default -1
if (thread_count < 0)
    thread_count = opts[1036];    // NVVMPassOptions slot 205 (offset 4144), default -1
if (thread_count == 0)
    thread_count = sub_22420F0(); // get_nprocs() -- number of online CPUs
```

| Source | Slot | Offset | Default | Meaning |
|--------|------|--------|---------|---------|
| Primary | 203 (0xCB) | 4104 | -1 (auto) | Explicit thread count |
| Fallback | 205 (0xCD) | 4144 | -1 (auto) | Secondary thread count |
| System | — | — | — | `get_nprocs()` return value |

The `-1` sentinel means "auto-detect." When both slots are `-1`, the system falls back to the number of online CPUs. In practice, this means a 16-core machine will spawn 16 threads for concurrent Phase II compilation unless the user or calling tool overrides the count.

### Per-Function Context Packaging

Before submitting each function to the thread pool, `sub_12E1EF0` builds a self-contained compilation context (4,632 bytes) containing everything the worker needs:

1. **Bitcode extraction**: `sub_1AB9F40(&buffer, *module_bc, &iterators, sub_12D4BD0, &filter_state)` — extracts the bitcode for a single function using `sub_12D4BD0` as a filter callback that checks the function name against the work hash table
2. **Bitcode parsing**: `sub_153BF40(module_data, &context, 0, 0, 0, 0)` — parses the extracted bitcode into an LLVM Module
3. **Context copy**: Copies all iterator state (`v391`--`v395`), context/options/error callbacks (`v405`--`v409`), module name string, the full 4,512-byte NVVMPassOptions struct, and module bitcode vectors

Each function gets its own independent copy of the options struct and module — there is no shared mutable state between worker threads during Phase II.

## Worker Entry and Completion

### Work Submission

Each function is submitted to the thread pool with two callbacks:

```c
v373 = sub_12E8D50;    // completion callback (runs the optimizer)
v372 = sub_12D4D90;    // destructor callback (cleanup)
sub_16D5230(work_item, thread_pool, context);  // enqueue
```

### Per-Function Callback (sub_12E8D50)

The completion callback extracts the module from the packaged context and calls the Phase II per-function optimizer:

```c
void sub_12E8D50(Context *ctx) {
    Module *mod = extract_module(ctx);
    sub_12E86C0(ctx, function_index, opts, module_name);
}
```

### Per-Function Phase II Optimizer (sub_12E86C0, 1,665 bytes native)

This function sets the TLS phase counter to 2 and runs the pass pipeline on the individual function's module:

```c
void sub_12E86C0(Context *ctx, int func_idx, Options *opts, StringRef name) {
    int *phase = malloc(4);
    *phase = 2;
    tls_set(qword_4FBB3B0, phase);
    // Run Phase II pass pipeline on this function's module
    sub_12E54A0(ctx, ...);
}
```

Because `qword_4FBB3B0` is TLS, each worker thread has its own phase counter. All worker threads see phase=2 concurrently without interference.

### Post-Compilation Merge

After all worker threads complete (`sub_16D4EC0` joins the thread pool):

1. **Jobserver cleanup**: `sub_1682740` checks for jobserver errors and releases tokens
2. **Error check**: If any per-function callback reported an error, the compilation fails
3. **Normal mode** (opt_level >= 0): Appends a null byte to the output buffer (bitcode stream terminator)
4. **Split-compile mode** (opt_level < 0): Re-reads each function's bitcode via `sub_153BF40`, links all per-function modules via `sub_12F5610` (the LLVM module linker), and restores linkage attributes from the `v362` hash table. Specifically:
   - Linkage values 7–8: set only low 6 bits (external linkage types)
   - Other values: set low 4 bits, then check `(value & 0x30) != 0` for visibility bits
   - Sets `byte+33 |= 0x40` (dso_local flag)

## Configuration

### Environment Variables

| Variable | Check | Effect |
|----------|-------|--------|
| `LIBNVVM_DISABLE_CONCURRENT_API` | `getenv() != NULL` | Sets `byte_4F92D70 = 1`. Disables concurrent/thread-safe LibNVVM API usage entirely. Any non-NULL value triggers it. Checked in global constructor `ctor_104` at `0x4A5810`. |
| `MAKEFLAGS` | Parsed by `sub_1682BF0` | Searched for `--jobserver-auth=` to enable GNU Make jobserver integration |

### NVVMPassOptions Slots

| Slot | Offset | Type | Default | Purpose |
|------|--------|------|---------|---------|
| 163 (0xA3) | 3288 | `BOOL_COMPACT` | 0 | Jobserver integration requested (set by `-jobserver` flag) |
| 201 (0xC9) | 4064 | `BOOL_COMPACT` | 0 | Force concurrency on/off (0 = auto) |
| 203 (0xCB) | 4104 | `INTEGER` | -1 | Primary thread count (-1 = auto) |
| 205 (0xCD) | 4144 | `INTEGER` | -1 | Fallback thread count (-1 = auto) |

### CLI Flags

| Flag | Route | Effect |
|------|-------|--------|
| `-jobserver` | `opt "-jobserver"` | Enables GNU jobserver integration (sets slot 163) |
| `-split-compile=<N>` | `opt "-split-compile=<N>"` | Enables split-module compilation (opt_level set to -1) |
| `-split-compile-extended=<N>` | `opt "-split-compile-extended=<N>"` | Extended split-compile (also sets `+1644 = 1`) |
| `--sw2837879` | Internal | Concurrent ptxStaticLib workaround flag |

### Phase State Machine

```text
  START
    |
    v
  [phase=1] --> sub_12E54A0 (Phase I: whole-module analysis)
    |
    v
  error? --yes--> RETURN (abort)
    |no
    v
  count_defined_functions()
    |
    +--(1 func)--> [phase=2] --> sub_12E54A0 (Phase II sequential)
    |                                |
    |                                v
    |                            [phase=3] --> DONE
    |
    +--(N funcs, threads>1)--> sub_12E1EF0 (concurrent)
    |                             |
    |                             +-- sort functions by priority
    |                             +-- create thread pool
    |                             +-- init jobserver (if requested)
    |                             +-- for each function:
    |                             |     extract per-function bitcode
    |                             |     parse into independent Module
    |                             |     [phase=2] per-function (TLS)
    |                             |     submit to thread pool
    |                             +-- join all threads
    |                             +-- link split modules (if split-compile)
    |                             +-- [phase=3] --> DONE
    |
    +--(N funcs, threads<=1)--> [phase=2] --> sub_12E54A0 (sequential)
                                    |
                                    v
                                [phase=3] --> DONE
```

## Differences from Upstream LLVM

Upstream LLVM has no two-phase compilation model. The standard LLVM pipeline runs all passes in a single invocation with no phase discrimination. CICC's approach is entirely custom:

1. **Phase counter TLS variable**: Upstream LLVM passes have no concept of reading a global phase counter to decide whether to run. Every pass in CICC must check `qword_4FBB3B0` and early-return if it belongs to the wrong phase.

2. **Per-function module splitting**: Upstream LLVM's `splitModule()` (in `llvm/Transforms/Utils/SplitModule.h`) exists for ThinLTO and GPU offloading, but CICC's splitting at `sub_1AB9F40` with the `sub_12D4BD0` filter callback is a custom implementation integrated with the NVVMPassOptions system.

3. **GNU jobserver integration**: No upstream LLVM tool participates in the GNU Make jobserver protocol. This is entirely NVIDIA-specific, implemented to play nicely with `make -j` in CUDA build systems.

4. **Function priority sorting**: Upstream LLVM processes functions in module iteration order. CICC's priority-based sorting via `sub_12E0CA0` ensures that expensive functions start compiling first, reducing tail latency in the thread pool.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Function iterator: next | `sub_12D3C60` | ~200 | — |
| Function iterator: advance | `sub_12D3C80` | ~230 | — |
| Function iterator: end check | `sub_12D3CA0` | ~260 | — |
| Function attribute/priority query | `sub_12D3D20` | 585 | — |
| Auto thread count determination | `sub_12D3FC0` | 3,600 | — |
| Concurrency eligibility check | `sub_12D4250` | 626 | — |
| Insertion sort (small N) | `sub_12D48A0` | — | — |
| Per-function bitcode filter callback | `sub_12D4BD0` | 2,384 | — |
| Work item destructor callback | `sub_12D4D90` | 2,742 | — |
| Introsort (large N) | `sub_12D57D0` | — | — |
| Function sorting and enumeration | `sub_12E0CA0` | 23,422 | — |
| Concurrent compilation top-level entry | `sub_12E1EF0` | 51,325 | — |
| Master pipeline assembly (both phases) | `sub_12E54A0` | 49,800 | — |
| Concurrent worker entry | `sub_12E7B90` | 2,997 | — |
| Phase I/II orchestrator | `sub_12E7E70` | 9,405 | — |
| Per-function Phase II optimizer | `sub_12E86C0` | 7,687 | — |
| Per-function completion callback | `sub_12E8D50` | — | — |
| LLVM module linker (post-merge) | `sub_12F5610` | 7,339 | — |
| Bitcode reader/verifier | `sub_153BF40` | — | — |
| `isDeclaration()` check | `sub_15E4F60` | — | — |
| Get function name | `sub_1649960` | — | — |
| Walk to parent GlobalValue | `sub_164A820` | — | — |
| Jobserver error check/cleanup | `sub_1682740` | — | — |
| MAKEFLAGS `--jobserver-auth=` parser | `sub_1682BF0` | — | — |
| GNU jobserver init (296-byte state) | `sub_16832F0` | — | — |
| TLS set (`qword_4FBB3B0`) | `sub_16D40E0` | — | — |
| TLS get (`qword_4FBB3B0`) | `sub_16D40F0` | — | — |
| Thread pool create | `sub_16D4AB0` | — | — |
| Thread pool join | `sub_16D4EC0` | — | — |
| Thread pool enqueue work item | `sub_16D5230` | — | — |
| Per-function bitcode extraction | `sub_1AB9F40` | — | — |
| `get_nprocs()` wrapper | `sub_22420F0` | — | — |

## Cross-References

- [Entry Point & CLI](../pipeline/entry.md) — pipeline dispatch that leads to the optimizer, including `-jobserver` flag routing
- [Optimizer Pipeline](../pipeline/optimizer.md) — `sub_12E54A0`, the pipeline function called by both phases
- [NVVMPassOptions](../config/knobs.md) — the 221-slot options table including thread count and jobserver slots
- [Environment Variables](../config/env-vars.md) — `LIBNVVM_DISABLE_CONCURRENT_API` and `MAKEFLAGS`
- [CLI Flags](../config/cli-flags.md) — `-jobserver`, `-split-compile`, `-split-compile-extended`
- [Bitcode I/O](../infra/bitcode-io.md) — `sub_153BF40` bitcode reader used for per-function module extraction
