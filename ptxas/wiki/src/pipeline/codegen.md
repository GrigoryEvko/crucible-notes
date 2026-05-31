# SASS Code Generation

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

This page covers the top-level compilation orchestration layer of ptxas: the code that sits between the PTX front-end (parsing, directive handling) and the 159-phase optimization pipeline. It is responsible for validating the parsed PTX, selecting a compilation strategy, computing register constraints, dispatching per-kernel compilation (either sequentially or via a thread pool), and collecting per-kernel outputs for finalization. The orchestrator is the single largest function in the front-end region at 2,141 decompiled lines.

## Key Facts

| | |
|---|---|
| **Core orchestrator** | `sub_4428E0` (13,774 bytes, 2,141 decompiled lines) |
| **Per-kernel worker** | `sub_43A400` (4,696 bytes, 647 lines) |
| **Per-kernel DAGgen+OCG** | `sub_64BAF0` (~30 KB, 1,006 decompiled lines) |
| **Per-entry output** | `sub_43CC70` (5,425 bytes, 1,077 decompiled lines) |
| **Thread pool worker** | `sub_436DF0` (485 bytes, 59 decompiled lines) |
| **Thread pool constructor** | `sub_1CB18B0` (184-byte pool struct, calls `pthread_create`) |
| **Finalization** | `sub_432500` (461 bytes, 47 decompiled lines) |
| **Regalloc finalize** | `sub_4370F0` (522 bytes, 64 decompiled lines) |
| **Compilation strategies** | 4 (normal, compile-only, debug, non-ABI) |
| **Error recovery** | `setjmp`/`longjmp` (non-local, no C++ exceptions) |

## Architecture

```text
sub_446240 (top-level driver)
  |
  v
sub_4428E0 (core orchestrator, 2141 lines)
  |
  |-- Option validation: .version/.target, --compile-only, --compile-as-tools-patch
  |-- Cache config: def-load-cache, force-load-cache, def-store-cache, force-store-cache
  |-- Strategy selection: 4 function-pointer pairs (see below)
  |-- Register constraints: sub_43B660 per kernel (via strategy function)
  |-- Compile-unit table: 48-byte per-CU entry at a1+336
  |-- Timing array: 112-byte per-kernel entry at a1+256
  |
  +-- IF single-threaded (thread_count == 0):
  |     |
  |     FOR EACH compile unit:
  |       |
  |       +-- sub_43A400 (per-kernel setup, 647 lines)
  |       |     |-- Target-specific defaults ("ptxocg.0.0", cache, texmode)
  |       |     |-- ABI configuration, fast-compile shortcuts
  |       |     +-- Error recovery via setjmp
  |       |
  |       +-- sub_432500 (finalization wrapper, 47 lines)
  |       |     |-- setjmp error guard
  |       |     +-- vtable call at a1+96: invokes the actual OCG pipeline
  |       |
  |       +-- sub_4370F0 (timing finalization, 64 lines)
  |       |     +-- Accumulates per-kernel timing into 112-byte records
  |       |
  |       +-- sub_43CC70 (per-entry output, 1077 lines)
  |             |-- Skip __cuda_dummy_entry__
  |             |-- Generate .sass and .ucode sections
  |             +-- Emit "# ============== entry %s ==============" header
  |
  +-- IF multi-threaded (thread_count > 0):
        |
        |-- sub_1CB18B0(thread_count) --> create thread pool
        |
        FOR EACH compile unit:
          |
          +-- sub_43A400 (per-kernel setup, same as above)
          +-- Snapshot 15 config vectors (v158[3]..v158[17])
          +-- Copy hash maps for thread isolation
          +-- sub_1CB1A50(pool, sub_436DF0, task_struct) --> enqueue
          |
          v
        sub_436DF0 (thread pool worker, 59 lines)
          |-- sub_430590("ptxas", ...) -- set thread-local program name
          |-- Jobserver slot check (sub_1CC6EC0)
          |-- sub_432500(...) -- finalize via vtable call (DAGgen+OCG+SASS)
          |-- Timing: wall-clock and phase timers into per-CU record
          |-- sub_693630 (release compiled output to downstream)
          +-- sub_4248B0 (free task struct)
        |
        sub_1CB1AE0(pool)  --> wait for all tasks
        sub_1CB1970(pool)  --> destroy pool
        sub_4370F0(a1, -1) --> finalize aggregate timing
```

## The Core Orchestrator: sub_4428E0

This is a 2,141-line monolith that drives the entire compilation after the PTX has been parsed. Its responsibilities, in execution order:

### 1. Cache and Option Validation

The first 200+ lines read four cache-mode knobs from the options store at `a1+904`:

```c
def_load_cache   = get_knob(a1->options, "def-load-cache");
force_load_cache = get_knob(a1->options, "force-load-cache");
def_store_cache  = get_knob(a1->options, "def-store-cache");
force_store_cache = get_knob(a1->options, "force-store-cache");
```

It then validates a long series of option combinations:
- `--compile-as-tools-patch` (a1+727) incompatibility checks against shared memory, textures, surfaces, samplers, constants
- `--assyscall` (a1+627) resource allocation checks
- `--compile-only` (a1+726) vs unified functions
- Non-ABI mode (`a2+218`, `a2+235`): disables `--fast-compile`, `--extensible-whole-program`
- `--position-independent-code` vs `--extensible-whole-program` mutual exclusion
- Architecture version checks: `.target` SM version vs `--gpu-name` SM version
- `-noFwdPrg` forward-progress flag against SM version
- `--legacy-bar-warp-wide-behavior` against SM >= 70

### 2. Strategy Selection

Four compilation strategies are expressed as pairs of function pointers `(v314, v293)`, selected at lines 756-779 of the decompilation. Each strategy pair consists of a register-constraint calculator and a compile-unit builder:

| Strategy | Condition | Register Calculator (`v314`) | CU Builder (`v293`) |
|---|---|---|---|
| **Compile-only** | `--compile-only` OR `--assyscall` OR `--compile-as-tools-patch` | `sub_43C6F0` (225 lines) | `sub_4383B0` (177 lines) |
| **Debug** | `--compile-as-tools-patch` AND NOT debug mode | `sub_43CAE0` (91 lines) | `sub_4378E0` (250 lines) |
| **Non-ABI** | `--extensible-whole-program` | `sub_43C570` (77 lines) | `sub_438B50` (375 lines) |
| **Normal** | default | `sub_43CA80` (24 lines) | `sub_438B50` (375 lines) |

The selection logic:

```c
if (compile_only || assyscall || tools_patch) {
    calc_regs = sub_43C6F0;
    build_cus = sub_4383B0;
} else if (tools_patch_mode) {
    calc_regs = debug_mode ? sub_43C6F0 : sub_43CAE0;
    build_cus = debug_mode ? sub_4383B0 : sub_4378E0;
} else {
    calc_regs = extensible_whole_program ? sub_43C570 : sub_43CA80;
    build_cus = sub_438B50;
}
```

### 3. Register Constraint Calculation

Each strategy's register calculator iterates the kernel list and calls `sub_43B660` to compute per-kernel register limits. The result is stored in a hash map at `a1+1192`:

```c
// sub_43CA80 (normal strategy, 24 lines) -- simplest form
for (node = kernel_list; node; node = node->next) {
    entry = node->data;
    name  = entry->func_ptr;    // at +16
    limit = sub_43B660(a1, name, a1->opt_level, entry->thread_count);
    map_put(a1->reg_limit_map, name, limit);  // a1+1192
}
```

`sub_43B660` (3,843 bytes) balances register pressure against occupancy by considering `.maxnreg`, `--maxrregcount`, `.minnctapersm`, and `.maxntid`. String evidence: `".minnctapersm and .maxntid"`, `"threads per SM"`, `"computed using thread count"`, `"of .maxnreg"`, `"of maxrregcount option"`, `"global register limit specified"`.

### 4. Compile-Unit Table Construction

The CU builder (`v293`) constructs a linked list of 72-byte compile-unit descriptors. Each descriptor contains:

```c
struct CompileUnitDescriptor {  // 72 bytes
    int32   index;              // +0:  CU ordinal (monotonic counter)
    // +4..7: alignment hole (OWORD zero-init covers it)
    void*   dep_list;           // +8:  sub_42D150 set handle used as
                                //       per-CU dependency/symbol tracking set
    void*   entry_ptr;          // +16: pointer to entry-list node
                                //       (entry symbol reachable via ->func_attr at +80)
    bool    is_duplicate;       // +24: set by sub_42D6F0(a1+1176, entry) check;
                                //       also ORs into global flag at orchestrator+1304
    bool    is_entry;           // +25: **func_attr[0] -- true for .entry, false for .func
    // +26..27: padding
    int32   regalloc_a;         // +28: regalloc scratch slot A, zero-init, populated by
                                //       register allocator
    int32   regalloc_b;         // +32: regalloc scratch slot B, zero-init
    bool    has_shared_mem;     // +36: func_attr[+4]
    bool    has_surfaces;       // +37: func_attr[+5]
    bool    has_textures;       // +38: func_attr[+6]
    bool    has_samplers;       // +39: func_attr[+7]
    int16   cap_flags;          // +40: WORD from func_attr[+240] -- capability flags
    // +42..43: padding
    int32   min_regs;           // +44: 0 or 32, forced to 32 when profiling is enabled
                                //       (func_attr[+166] && opt>26 or debug_mode)
    void*   smem_info;          // +48: pointer to 24-byte shared-memory sub-struct
                                //       allocated by sub_424070(pool, 24); layout:
                                //         +0  bool  has_smem_decl
                                //         +8  qword smem_size (func_attr[+168])
                                //         +16 qword entry_name_ptr
    uint8   attr_flags[8];      // +56..63: 8 independent OR-accumulated byte flags,
                                //       each fed from a DWORD at func_attr[+208..+236]
                                //       (stride 4, truncated to low byte on store).
                                //       In the multi-CU merge path (sub_438B50) these
                                //       are OR-merged across every node in the group.
    void*   cg_symtab;          // +64: per-CU codegen symbol/string bag, allocated
                                //       by sub_693570 (1048-byte hashmap wrapped in
                                //       a 40-byte vtable handle at sub_6933A0).
                                //       Zero-init in CU builders; populated by
                                //       sub_43A400 line 144 before OCG runs;
                                //       released by sub_693630 at orchestrator
                                //       sub_4428E0 line 1668 (or on longjmp in
                                //       sub_43A400 line 578).
    int32   _reserved_68;       // +68: zero-init (sub_4383B0 line 92), no other
                                //       reads/writes found -- tail padding to round
                                //       struct up to a multiple of 8 bytes.
};
```

The builder allocates this via `sub_424070(pool, 72)`, populates it from the function attribute struct at `entry_ptr+80`, and enqueues it into the output list via `sub_42CA60`. Decompilation evidence for field layout: `sub_4383B0` lines 85-167 (normal path constructor, simplest form); `sub_4378E0` lines 142-225 (tools-patch variant, identical layout); `sub_438B50` lines 155-248 (extensible-whole-program merge path, uses same offsets but OR-merges attribute flags across every kernel in a CU group). The +64 `cg_symtab` slot is written in `sub_43A400` line 144 (`*((_QWORD *)a3 + 8) = sub_693570();`) and released at `sub_4428E0` line 1667-1668 (`v5 = *((_QWORD *)v229 + 8); sub_693630(v5);`).

### 5. Per-Kernel Dispatch

After building the CU list, the orchestrator enters one of two dispatch modes based on the thread count at `a1+668`:

#### Single-Threaded Path (thread_count == 0)

The loop at decompilation lines 1607-1686 iterates each CU sequentially:

```c
for (node = cu_list; node; node = node->next) {
    cu_desc = node->data;
    // Record start time in 112-byte timing record
    timing_record[cu_desc->index].start = get_time();
    
    // Allocate 360-byte work buffer
    work = pool_alloc(pool, 360);
    memset(work, 0, 360);
    
    // Per-kernel setup: target config, cache defaults, ABI
    sub_43A400(a1, parser_state, cu_desc, elf_builder, work);
    
    // Finalization: runs the actual DAGgen + OCG pipeline
    sub_432500(a1, cu_desc + 16, work[0], work[1]);
    
    // Timing finalization for this kernel
    sub_4370F0(a1, cu_desc->index);
    
    // Per-entry output: .sass/.ucode sections
    sub_43CC70(a1, parser_state, cu_desc, work);
    
    pool_free(work);
}
```

#### Multi-Threaded Path (thread_count > 0)

The thread pool path (decompilation lines 1709-1929) uses the pthread-based thread pool:

```c
// Phase 1: prepare all tasks
pool_obj = sub_1CB18B0(thread_count);  // create pool with N threads

for (node = cu_list; node; node = node->next) {
    cu_desc = node->data;
    
    // Allocate 360-byte work buffer (same as single-threaded)
    work = pool_alloc(pool, 360);
    
    // Extra per-thread state: 3 hash maps for thread isolation
    work[288] = hashmap_new(8);   // per-thread reg constraints
    work[296] = hashmap_new(8);   // per-thread symbol copies
    work[304] = hashmap_new(8);   // per-thread attribute copies
    
    sub_43A400(a1, parser_state, cu_desc, elf_builder, work);
    
    // Snapshot 15 config vectors from global state (a1+1072..a1+1296)
    // into work[48]..work[288] for thread-safe access
    for (i = 0; i < 15; i++)
        work[48 + 16*i] = load_128bit(a1 + 1072 + 16*i);
    
    // Copy hash maps from shared state into per-thread copies
    // (reg constraints, symbol tables, attribute maps)
    
    // Enqueue: sub_1CB1A50(pool, sub_436DF0, task_struct)
    task = malloc(48);
    task->pool_data = work;
    task->timing_base = ...;
    sub_1CB1A50(pool_obj, sub_436DF0, task);
}

// Phase 2: wait for all tasks
sub_1CB1AE0(pool_obj);   // wait-for-all
sub_1CB1970(pool_obj);   // destroy pool

// Phase 3: aggregate timing
sub_4370F0(a1, -1);      // -1 = aggregate all CUs
```

##### Jobserver Integration

When `--split-compile` is active with GNU Make, the thread pool integrates with Make's jobserver protocol. The worker function `sub_436DF0` checks `sub_1CC6EC0()` (jobserver slot acquire) before starting compilation and calls `sub_1CC7040()` (jobserver slot release) after completion. This prevents ptxas from exceeding the `-j` slot limit during parallel builds.

## Per-Kernel Worker: sub_43A400

This 647-line function sets up target-specific defaults for each kernel before the OCG pipeline runs. Key responsibilities:

1. **Timing instrumentation** -- records start timestamps, wall-clock time
2. **Target configuration** -- reads `"ptxocg.0.0"` defaults, sets cache mode, texturing mode, `"specified texturing mode"` string evidence
3. **Fast-compile shortcuts** -- when `--fast-compile` is active, reduces optimization effort
4. **ABI setup** -- configures parameter passing, return address register, scratch registers
5. **Error recovery** -- establishes `setjmp` point for fatal errors during kernel compilation

The function allocates a `_jmp_buf` on the stack for error recovery. If any phase in the downstream pipeline calls the fatal diagnostic path (`sub_42F590` with severity >= 6), execution longjmps back to sub_43A400's recovery handler, which cleans up the partially-compiled kernel and continues to the next.

String evidence: `"def-load-cache"`, `"force-load-cache"`, `"--sw4575628"`, `"NVIDIA"`, `"ptxocg.0.0"`, `"specified texturing mode"`, `"Indirect Functions or Extern Functions"`.

## Finalization: sub_432500

This 47-line wrapper function is the bridge between the orchestrator and the actual DAGgen+OCG pipeline. It:

1. Retrieves the thread-local context via `sub_4280C0`
2. Saves and replaces the `jmp_buf` pointer in the TLS struct (for nested error recovery)
3. Saves the current error/warning flags
4. Clears the error flags to create a clean compilation context
5. Calls through a vtable pointer at `a1+96` to invoke the actual compilation:

```c
// sub_432500 -- simplified
bool finalize(Context* ctx, CUDesc* cu, void* sass_out, void* ucode_out) {
    char* tls = get_tls();
    jmp_buf* old_jmp = tls->jmp_buf;
    tls->jmp_buf = &local_jmp;
    char saved_err = tls->error_flags;
    char saved_warn = tls->warning_flags;
    tls->error_flags = 0;
    tls->warning_flags = 0;
    
    if (setjmp(local_jmp)) {
        // Error path: restore state, cleanup, report ICE
        tls->jmp_buf = old_jmp;
        tls->error_flags = 1;  tls->warning_flags = 1;
        release_output(ucode_out);
        release_output(cu->output);
        report_internal_error();
        return false;
    }
    
    // Normal path: invoke the pipeline
    bool ok = ctx->vtable->compile(ctx->state, sass_out, ctx + 384);
    if (!ok) report_internal_error();
    
    // Merge error flags
    tls->jmp_buf = old_jmp;
    tls->error_flags = saved_err ? true : (tls->error_flags != 0);
    tls->warning_flags = saved_warn ? true : (tls->warning_flags != 0);
    return ok;
}
```

The vtable call at `a1+96` is the entry point into `sub_64BAF0` (the 1,006-line function that runs DAGgen, the 159-phase OCG pipeline, and Mercury SASS encoding for a single kernel).

## Regalloc Finalization: sub_4370F0

This 64-line function accumulates per-kernel timing results into the master timing array at `a1+256`. Each entry in this array is a 112-byte record:

```c
struct KernelTimingRecord {  // 112 bytes, at a1->timing_array + 112*index
    char*   kernel_name;     // +0
    float   ocg_time;        // +20
    float   total_time;      // +36
    float   cumulative;      // +40
    double  wall_clock;      // +72
    // ... other timing fields
};
```

When called with `index == -1` (aggregate mode after multi-threaded compilation), it sums all per-kernel records into the global timing counters at `a1+176` (total parse time), `a1+184` (total OCG time), and `a1+208` (peak wall-clock).

## Per-Entry Output: sub_43CC70

This 1,077-line function produces the final per-kernel output artifacts. Key behaviors:

1. **Skip dummy entries** -- checks for `"__cuda_dummy_entry__"` and returns immediately
2. **Section generation** -- creates `.sass` and `.ucode` ELF sections for each kernel
3. **Entry banner** -- emits `"\n# ============== entry %s ==============\n"` to the SASS text output
4. **Register map** -- calls `"reg-fatpoint"` to annotate the register allocation
5. **Verbose SASS output** -- when `--verbose` is active, formats and writes human-readable SASS text
6. **Multiple output paths** -- supports mercury, capmerc, and direct SASS output modes

## Thread Pool Worker: sub_436DF0

The worker function dispatched to each thread pool thread is compact (59 lines) but carefully structured for thread safety:

```c
void thread_worker(Context* a1, TaskStruct* task) {
    set_thread_program_name("ptxas", task);
    
    // Acquire jobserver slot if applicable
    if (a1->jobserver_enabled && !jobserver_acquire())
        report_fatal_error();  // Cannot get build slot
    
    float time_before = get_pool_time(a1->timer);
    double wall_before = get_wall_time();
    
    // Run the actual compilation
    sub_432500(a1->state, task + 64, task->sass_output, task->ucode_output);
    
    float time_after = get_pool_time(a1->timer);
    double wall_after = get_wall_time();
    
    // Record timing in per-CU record
    int cu_index = *(int*)task->cu_desc;
    TimingRecord* rec = &a1->timing_array[cu_index];
    rec->ocg_time = time_after - time_before;
    rec->cumulative += (time_after - time_before);
    if (wall_after - wall_before > 0)
        rec->wall_clock = wall_after - wall_before;
    
    // Peak wall-clock tracking (under lock)
    if (a1->compiler_stats && a1->per_kernel_stats) {
        lock_timing(6);
        double peak = a1->peak_wall_clock;
        if (get_wall_time() - a1->start_time > peak)
            a1->peak_wall_clock = get_wall_time() - a1->start_time;
        unlock_timing(6);
    }
    
    // Emit compiled output downstream
    if (a1->dump_sass)
        dump_sass(task->ucode_output);
    release_output(task->ucode_output);
    
    // Transfer kernel name to output
    task->output->name = **(task->cu_desc->entry_ptr + 88);
    
    // Release jobserver slot
    if (a1->jobserver_enabled && !jobserver_release())
        report_fatal_error();
    
    pool_free(task);
}
```

The timing lock at index 6 (`sub_607D70(6)` / `sub_607D90(6)`) serializes access to the peak wall-clock counter across threads. This is the only shared mutable state in the multi-threaded path -- all other per-kernel state is isolated in the 360-byte work buffer and per-thread hash map copies.

## Data Flow Summary

```text
PTX text
  |
  v (parsed by sub_451730 into AST at parser_state+88)
  |
sub_4428E0: strategy_calc(kernel_list)  --> reg_limit_map (a1+1192)
sub_4428E0: strategy_build(kernel_list) --> cu_descriptor_list (72-byte nodes)
  |
  v (for each CU descriptor)
  |
sub_43A400: target_config(cu_desc) --> 360-byte work buffer
  |
sub_432500: vtable->compile()
  |  invokes sub_64BAF0 (DAGgen + 159-phase OCG + Mercury)
  |    |
  |    +-- Ori IR construction (DAGgen phase)
  |    +-- 159 phases via PhaseManager (sub_C62720 / sub_C64F70)
  |    +-- Mercury SASS encoding (phases 113-122)
  |    |
  v    v
work[0] = .sass output    work[1] = .ucode output
  |
sub_4370F0: record timing
  |
sub_43CC70: emit .sass/.ucode sections, entry banner, verbose output
  |
  v
ELF builder (sub_612DE0)
```

## Cross-References

- [Pipeline Overview](overview.md) -- end-to-end compilation flow
- [Entry Point & CLI](entry.md) -- the top-level driver that calls sub_4428E0
- [Optimization Pipeline (159 Phases)](optimizer.md) -- the OCG pipeline invoked per-kernel
- [Code Generation Overview](../codegen/overview.md) -- detailed codegen subsystem
- [SASS Instruction Encoding](../codegen/encoding.md) -- Mercury encoding phases
- [Register Allocation](../regalloc/overview.md) -- Fatpoint algorithm invoked at phase 101
- [Thread Pool & Concurrency](../infra/threading.md) -- thread pool struct and jobserver
- [Memory Pool Allocator](../infra/memory-pools.md) -- pool allocator used throughout
- [Knobs System](../config/knobs.md) -- cache-mode knobs read by sub_4428E0

## Function Map

| Address | Size | Lines | Identity | Confidence |
|---|---|---|---|---|
| `sub_4428E0` | 13,774 B | 2,141 | Core compilation orchestrator | HIGH |
| `sub_43CA80` | 192 B | 24 | Normal strategy: register calculator | HIGH |
| `sub_438B50` | 2,419 B | 375 | Normal/non-ABI strategy: CU builder | HIGH |
| `sub_43C6F0` | 1,600 B | 225 | Compile-only strategy: register calculator | HIGH |
| `sub_4383B0` | 1,320 B | 177 | Compile-only/debug strategy: CU builder | HIGH |
| `sub_43CAE0` | 648 B | 91 | Debug strategy: register calculator | HIGH |
| `sub_4378E0` | 2,010 B | 250 | Debug strategy: CU builder | HIGH |
| `sub_43C570` | 577 B | 77 | Non-ABI strategy: register calculator | HIGH |
| `sub_43A400` | 4,696 B | 647 | Per-kernel worker (target config + setup) | HIGH |
| `sub_64BAF0` | ~30 KB | 1,006 | DAGgen + OCG + SASS (per-kernel pipeline) | MEDIUM |
| `sub_43CC70` | 5,425 B | 1,077 | Per-entry output (.sass/.ucode sections) | HIGH |
| `sub_436DF0` | 485 B | 59 | Thread pool worker function | HIGH |
| `sub_432500` | 461 B | 47 | Finalization wrapper (setjmp + vtable call) | HIGH |
| `sub_4370F0` | 522 B | 64 | Timing finalization (per-kernel + aggregate) | HIGH |
| `sub_43B660` | 3,843 B | ~300 | Register/resource constraint calculator | HIGH |
| `sub_1CB18B0` | ~200 B | 33 | Thread pool constructor (184-byte struct) | HIGH |
| `sub_1CB1A50` | ~200 B | 21 | Thread pool task submit | HIGH |
| `sub_1CB1AE0` | -- | -- | Thread pool wait-for-all | HIGH |
| `sub_1CB1970` | -- | -- | Thread pool destructor | HIGH |
| `sub_1CC7300` | 2,027 B | -- | GNU Make jobserver client | HIGH |

## Diagnostic Strings

| String | Location | Purpose |
|---|---|---|
| `"def-load-cache"` | sub_4428E0 | Cache mode knob read |
| `"force-load-cache"` | sub_4428E0 | Cache mode knob read |
| `"def-store-cache"` | sub_4428E0 | Cache mode knob read |
| `"force-store-cache"` | sub_4428E0 | Cache mode knob read |
| `"--compile-only"` | sub_4428E0 | Option validation |
| `"--compile-as-tools-patch"` | sub_4428E0 | Option validation |
| `"--extensible-whole-program"` | sub_4428E0 | Option validation |
| `"calls without ABI"` | sub_4428E0 | Non-ABI mode diagnostic |
| `"compilation without ABI"` | sub_4428E0 | Non-ABI mode diagnostic |
| `"unified Functions"` | sub_4428E0 | Compile-only restriction |
| `"suppress-debug-info"` | sub_4428E0 | Debug info suppression warning |
| `"position-independent-code"` | sub_4428E0 | PIC mode configuration |
| `"__cuda_dummy_entry__"` | sub_43CC70 | Dummy entry skip check |
| `"reg-fatpoint"` | sub_43CC70 | Register map annotation |
| `".sass"`, `".ucode"` | sub_43CC70 | Output section names |
| `"# ============== entry %s =="` | sub_43CC70 | Per-entry SASS banner |
| `"ptxocg.0.0"` | sub_43A400 | Target config identifier |
| `"specified texturing mode"` | sub_43A400 | Texturing mode diagnostic |
| `".local_maxnreg"` | sub_438B50 | Per-function register limit |
| `"device functions"` | sub_438B50 | Compile-only device function handling |
| `"--compile-only option"` | sub_438B50 | Compile-only restriction |
| `"ptxas"` | sub_436DF0 | Thread-local program name |
