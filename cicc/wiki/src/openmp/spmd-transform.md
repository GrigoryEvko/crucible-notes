# Generic-to-SPMD Transformation

The Generic-to-SPMD transformation (`sub_26968A0`, 61 KB, ~1807 lines) is cicc's most impactful OpenMP target optimization. It converts GPU kernels from Generic execution mode -- where thread 0 acts as a master running serial code through a state machine while all other threads idle at a barrier -- into SPMD mode, where every thread in the block executes the same code from the first instruction. The transformation eliminates the worker state machine loop entirely, removes warp divergence at kernel entry, replaces heavyweight generic barriers with lightweight SPMD barriers (`__syncthreads`), and enables the hardware scheduler to fill warps from the very first cycle. On real workloads this routinely yields 2-4x speedups for simple `target parallel for` regions. The pass emits diagnostic `OMP120` on success and `OMP121` when a callee's side effects prevent conversion.

## Key Facts

| Property | Value |
|---|---|
| Function address | `sub_26968A0` |
| Decompiled size | 61 KB (~1807 lines) |
| Pass registration | `openmp-opt` (pipeline slot 75, Module pass) |
| Post-link variant | `openmp-opt-postlink` (slot 76) |
| CGSCC variant | `openmp-opt-cgscc` (slot 154) |
| Parameters | `a1` = PassState, `a2` = ModuleContext, `a3` = OutputFlag |
| Eligibility flag | `*(a1+241)` -- boolean, set by prior analysis |
| Parallel region array | `*(a1+280)` base, `*(a1+288)` count |
| Diagnostic handler | `*(a2+4392)` |
| Success diagnostic | OMP120: "Transformed generic-mode kernel to SPMD-mode." |
| Failure diagnostic | OMP121: "Value has potential side effects preventing SPMD-mode execution" |

## Generic vs SPMD Execution Model

Understanding the two execution modes is essential before examining the transformation.

| Aspect | Generic Mode | SPMD Mode |
|---|---|---|
| Thread roles | Thread 0 = master; threads 1..N-1 = workers | All threads execute same code |
| Kernel entry | `__kmpc_target_init` returns tid for master, -1 for workers | `__kmpc_target_init` returns tid for all |
| Serial code | Master executes directly | Wrapped in `if (tid == 0)` guard |
| Parallel region | Master signals workers via `parallel_level`; workers wake, execute outlined fn, re-barrier | All threads already executing; outlined fn body inlined |
| Barrier type | `__kmpc_barrier_simple_generic` (poll-based state machine) | `__kmpc_barrier_simple_spmd` (maps to `bar.sync` / `__syncthreads`) |
| Worker idle loop | `while(true) { barrier(); if(parallel_level) { exec(); barrier(); } }` | No idle loop -- eliminated entirely |
| Warp divergence | Warps containing thread 0 diverge at entry gate | No divergence at entry |
| Occupancy | Lower -- workers consume registers/shared mem while idle | Higher -- all resources used productively |
| Execution mode constant | 1 (`OMP_TGT_EXEC_MODE_GENERIC`) | 2 (`OMP_TGT_EXEC_MODE_SPMD`) |
| Transition marker | -- | 3 (`OMP_TGT_EXEC_MODE_GENERIC_SPMD`, intermediate during transform) |

In Generic mode the runtime creates a CTA (Cooperative Thread Array) where only thread 0 enters user code. The remaining N-1 threads enter a polling loop: they call `__kmpc_barrier_simple_generic`, check the `parallel_level` variable, and if a parallel region has been entered by the master, they wake up, execute the outlined parallel function, then return to polling. This "state machine" pattern is the primary performance bottleneck -- it wastes cycles on barrier polling, causes massive warp divergence on the first warp (which contains both the master and worker lanes), and prevents the scheduler from issuing useful work for idle threads.

SPMD mode eliminates all of this. Every thread begins executing user code at kernel entry. Serial code sections that cannot be parallelized are protected by lightweight `tid == 0` guards, with results broadcast to all threads through shared memory and `bar.sync` barriers.

## Legality Analysis

The transformation is gated by a boolean eligibility flag at `*(a1+241)`, which is computed by a prior analysis pass (not `sub_26968A0` itself). The analysis determines eligibility based on three conditions:

### Condition 1: Kernel is Currently in Generic Mode

The execution mode bit-vector's low byte must equal 1 (Generic). This is checked at line 429 of the decompiled output:

```c
// sub_2674090/sub_2674040 read the execution mode attribute
mode_bv = get_exec_mode(a1 + 304);
if (mode_bv.size <= 64)
    mode_val = mode_bv.inline_data;
else
    mode_val = *mode_bv.data_ptr;

if ((uint8_t)mode_val != 1)  // Not Generic mode
    return;
```

### Condition 2: All Callees are SPMD-Amenable

Every call instruction reachable from the kernel's parallel regions must reference a function in the SPMD-amenable function set. This set lives at `*(a2+208) + 34952` (base pointer) with capacity at offset +34968.

```c
// SPMD-amenable lookup (open-addressing hash set)
bool is_spmd_amenable(void *func_ptr, void *table_base, uint64_t capacity) {
    uint64_t hash = ((uintptr_t)func_ptr >> 9) ^ ((uintptr_t)func_ptr >> 4);
    uint64_t slot = hash & (capacity - 1);
    while (true) {
        void *entry = table_base[slot];
        if (entry == func_ptr) return true;
        if (entry == (void*)-4096) return false;  // empty sentinel
        slot = (slot + 1) & (capacity - 1);       // linear probe
    }
}
```

Functions are pre-populated in this set if they have been analyzed as side-effect free (from the caller's perspective in SPMD context), or if the programmer annotated them with `[[omp::assume("ompx_spmd_amenable")]]`. When a callee fails this check, the pass takes Path A (non-SPMD candidate path, lines 1692-1806) and emits OMP121 for each offending call:

```text
warning: Value has potential side effects preventing SPMD-mode execution.
         Add `[[omp::assume("ompx_spmd_amenable")]]` to the called function
         to override [OMP121]
```

The diagnostic is constructed via `sub_B178C0` (warning constructor), message appended via `sub_B18290`, and emitted through `sub_1049740` to the handler at `*(a2+4392)`.

### Condition 3: No Unresolvable Side Effects

The kernel must not contain operations that are inherently unsafe when executed by multiple threads simultaneously -- for example, I/O operations with ordering requirements, or accesses to thread-local storage that assumes single-thread access.

### Legality Pseudocode

```c
function is_spmd_eligible(kernel, module_ctx):
    // Check current execution mode
    mode = read_exec_mode(kernel.attributes)
    if mode != GENERIC:
        return false

    // Scan all parallel regions
    for region in kernel.parallel_regions:
        for inst in region.instructions:
            if is_call_like(inst):  // opcode 34, 52, or 86
                callee = get_callee(inst)
                if callee.is_declaration:
                    if callee not in module_ctx.spmd_amenable_set:
                        emit_diagnostic(OMP121, inst.location,
                            "Value has potential side effects...")
                        return false

    return true
```

The call-like instruction detection uses a bitmask test: `(opcode - 34) <= 0x33` followed by `bittest(0x8000000000041, opcode - 34)`, which matches opcodes 34 (call), 52 (invoke), and 86 (callbr) -- the three LLVM call-family instructions.

## Transformation Algorithm

Once eligibility is confirmed, `sub_26968A0` takes Path B (lines 407-1691). The path splits based on kernel complexity:

### Simple Case: Single Parallel Region

When `*(a1+160) == 0` and `*(a1+224) == 0`, the kernel has a single parallel region with no intervening serial code. This is the fast path (lines 432-672).

```c
function transform_simple_spmd(kernel, module_ctx):
    entry_bb = get_entry_block(kernel)
    func_scope = get_function_scope(kernel)
    thread_config = get_thread_configuration(kernel, module_ctx)

    // 1. Create new basic blocks
    user_code_bb = create_region("main.thread.user_code")
    exit_bb = create_exit_block("exit.threads")
    register_in_worklist(user_code_bb)
    register_in_worklist(exit_bb)

    // 2. Insert thread-id check at entry
    tid = call __kmpc_get_hardware_thread_id_in_block()  // runtime call ID 6
    is_main = icmp eq tid, 0
    br is_main, user_code_bb, exit_bb

    // 3. Move original parallel body into user_code_bb
    //    (all threads execute this -- the parallel outlined fn
    //     is effectively inlined into the kernel)

    // 4. Update execution mode: Generic(1) -> SPMD(2)
    //    Intermediate: set mode 3 (GENERIC_SPMD) then overwrite to 2
    bv_entry = create_bitvector_entry(*(kernel+304+8), 3, 0)
    current = read_attribute(*(kernel+304))
    *(kernel+304) = insert_attribute(current, bv_entry, key=0, value=1)

    // 5. Emit success diagnostic
    if diagnostic_handler_registered(module_ctx+4392):
        emit_remark(OMP120, "Transformed generic-mode kernel to SPMD-mode.")
```

The resulting CFG is straightforward:

```llvm
entry:
    %tid = call i32 @__kmpc_get_hardware_thread_id_in_block()
    %is_main = icmp eq i32 %tid, 0
    br i1 %is_main, label %user_code, label %exit.threads

user_code:                         ; all threads execute
    ... original parallel body ...
    br label %exit.threads

exit.threads:
    ret void
```

### Complex Case: Multiple Parallel Regions

When the kernel contains multiple parallel regions with serial code between them, the pass executes a four-phase transformation (lines 720-1676).

#### Phase 1: Deduplicate Parallel Regions (lines 720-760)

Multiple parallel regions may call the same outlined function. The pass deduplicates by function pointer using an inline hash set:

```c
function dedup_regions(parallel_regions):
    seen = HashSet()  // inline small-buffer optimization
    unique = []
    for region in parallel_regions:
        fn_ptr = region.outlined_function  // offset+40
        if fn_ptr not in seen:
            seen.insert(fn_ptr)
            unique.append(region)
    return unique
```

#### Phase 2: Identify Non-SPMD-Safe Instructions (lines 768-873)

For each parallel region, the pass walks the CFG successor chain and identifies instructions with side effects that are not SPMD-compatible:

```c
function find_guarded_ranges(region, module_ctx):
    ranges = []
    first_unsafe = null
    last_unsafe = null

    for inst in walk_cfg_successors(region):
        if is_side_effecting_call(inst):
            // Skip known-safe calls (global dtors at module_ctx+208+32432)
            if inst.callee == module_ctx.global_dtor_fn:
                continue
            // For invoke instructions: check if exception handler count is 0
            if inst.opcode == 85:  // invoke
                if get_eh_handler_count(inst) == 0:
                    continue  // can be simplified
            if first_unsafe == null:
                first_unsafe = inst
            last_unsafe = inst
        else:
            if first_unsafe != null:
                ranges.append((first_unsafe, last_unsafe))
                first_unsafe = null
                last_unsafe = null

    if first_unsafe != null:
        ranges.append((first_unsafe, last_unsafe))

    return ranges
```

The pass then calls `sub_B444E0` to insert guard instructions at each range boundary.

#### Phase 3: Build Guarded Region Descriptors (lines 876-1059)

Each parallel region is looked up in the function-to-region-tracker hash map at `*(a2+144)`. This map uses a splitmix64-variant hash:

```c
uint64_t hash_function_key(uint64_t name_hash, uint64_t addr_hash) {
    uint64_t raw = name_hash ^ (16 * addr_hash);
    uint64_t h = raw * 0xBF58476D1CE4E5B9ULL;
    h = (h >> 31) ^ (h * 0x1CE4E5B9ULL);
    return h;
}
```

The map stores 24-byte keys (module pointer, name pointer, auxiliary pointer) with a sentinel key of `(-4096, qword_4FEE4D0, qword_4FEE4D8)`. Each entry's value (at +24) points to a guarded region tracker structure:

| Offset | Type | Description |
|---|---|---|
| +472 | i32 | Work counter |
| +480 | ptr | Block pointer array base |
| +488 | i64 | Capacity |
| +492 | i32 | Current size |
| +500 | i8 | Initialized flag |

#### Phase 4: Split and Rewire CFG (lines 1060-1670)

For each `(first_instr, last_instr)` pair identified in Phase 2, the pass creates five new basic blocks and rewires the CFG:

```c
function create_guarded_region(first_instr, last_instr, module_ctx):
    parent_bb = first_instr.parent

    // 1. Split into 5 blocks
    guarded_end_bb = split_block(parent_bb, after=last_instr, name="region.guarded.end")
    barrier_bb    = split_block(guarded_end_bb, at_start, name="region.barrier")
    exit_bb       = split_block(barrier_bb, at_start, name="region.exit")
    guarded_bb    = split_block(parent_bb, at=first_instr, name="region.guarded")
    check_tid_bb  = split_block(parent_bb, at=terminator, name="region.check.tid")

    // 2. Register all blocks in worklist
    for bb in [guarded_end_bb, barrier_bb, exit_bb, guarded_bb, check_tid_bb]:
        register_in_worklist(bb)

    // 3. Handle escaping values (shared memory promotion)
    has_broadcast = false
    for inst in guarded_bb:
        outside_uses = [u for u in inst.uses if u.parent != guarded_bb]
        if outside_uses:
            has_broadcast = true

            // Allocate shared memory for output
            alloc = create_alloca(
                type = inst.type,
                address_space = 7,  // shared memory
                name = sanitize(inst.name) + ".guarded.output.alloc"
            )

            // Store result from master thread (inside guarded block)
            create_store(inst, alloc, insert_in=guarded_bb)

            // Load from all threads (after barrier)
            load = create_load(
                type = inst.type,
                ptr = alloc,
                name = sanitize(inst.name) + ".guarded.output.load",
                insert_in = barrier_successor
            )

            // Rewrite all outside uses
            replace_all_uses_outside(inst, load, guarded_bb)

    // 4. Insert thread-id check
    tid = call __kmpc_get_hardware_thread_id_in_block()  // call ID 6
    cmp = icmp eq tid, 0
    br cmp, guarded_bb, barrier_bb

    // 5. Insert SPMD barrier
    call __kmpc_barrier_simple_spmd(ident, tid)  // call ID 187

    // 6. If broadcast values exist, insert second barrier after loads
    if has_broadcast:
        call __kmpc_barrier_simple_spmd(ident, tid)  // ensures loads complete
```

The resulting CFG for a complex kernel with serial code between two parallel regions:

```llvm
entry:
    ...

region.check.tid:
    %tid = call i32 @__kmpc_get_hardware_thread_id_in_block()
    %cmp = icmp eq i32 %tid, 0
    br i1 %cmp, label %region.guarded, label %region.barrier

region.guarded:                    ; master thread only
    ... serial code ...
    store %result, %shared_mem     ; broadcast output
    br label %region.guarded.end

region.guarded.end:
    br label %region.barrier

region.barrier:
    call void @__kmpc_barrier_simple_spmd(%ident, %tid)
    %result = load %shared_mem     ; all threads read
    call void @__kmpc_barrier_simple_spmd(%ident, %tid)  ; if broadcast
    br label %region.exit

region.exit:
    ... next parallel region (all threads) ...
```

### Name Sanitization

Output variable names are sanitized for use as global symbol names. Non-alphanumeric, non-underscore characters are replaced with `.`:

```c
// Identical logic in both cicc and upstream LLVM
char sanitize_char(char c) {
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '_')
        return c;
    return '.';
}
```

## Shared Memory Output Promotion

When a value computed inside a guarded region (master-only code) is needed by all threads after the barrier, the pass promotes it through shared memory. This is the cicc implementation of what upstream LLVM calls "broadcast values." The sequence is:

1. **Allocate**: `sub_B30000` creates an address-space-7 (shared/local) allocation with suffix `.guarded.output.alloc`. The allocation node is 80 bytes, subtype 7.

2. **Store**: `sub_B4D460` emits a store from the master thread's computed value into shared memory. Placed inside the guarded block, before the branch to `region.guarded.end`.

3. **First barrier**: `__kmpc_barrier_simple_spmd` (runtime call ID 187) ensures the store is globally visible to all threads in the CTA.

4. **Load**: `sub_B4D230` emits a load from shared memory with suffix `.guarded.output.load`. Placed in the barrier successor block so all threads read the broadcast value.

5. **Second barrier**: If broadcast values exist, a second `__kmpc_barrier_simple_spmd` call ensures all threads have completed their loads before the shared memory is potentially reused.

6. **Use rewriting**: `sub_256E5A0` replaces every use of the original value outside the guarded block with the loaded value.

## State Machine Elimination

The state machine elimination is the core performance win of the SPMD transformation. Understanding the state machine that gets eliminated -- and its fallback generator -- is essential for reimplementation.

### Generic-Mode Worker State Machine (What Gets Eliminated)

In Generic mode, `__kmpc_target_init` (runtime call ID 155) returns `-1` for all threads except thread 0 (the master). The kernel entry code branches on this return value: thread 0 falls through to user code, while threads 1..N-1 jump to the worker state machine loop. This loop is the performance bottleneck that the SPMD transformation eliminates.

The complete Generic-mode kernel structure, as generated by the runtime and optionally customized by `sub_2678420`:

```c
// Generic mode kernel entry (before SPMD transformation)
void __omp_offloading_kernel(KernelEnvironmentTy *env, KernelLaunchEnvironmentTy *launch_env) {
    int ret = __kmpc_target_init(env, launch_env);  // [155]
    if (ret == -1)
        goto worker_state_machine;

    // === MASTER THREAD (thread 0) ===
    // User code: serial sections + parallel dispatch
    ...
    __kmpc_kernel_prepare_parallel(outlined_fn_ptr);  // [157] signal workers
    __kmpc_barrier_simple_generic(loc, gtid);         // [188] wake workers
    // ... workers execute outlined_fn ...
    __kmpc_barrier_simple_generic(loc, gtid);         // [188] wait for workers
    // ... more serial code ...
    __kmpc_target_deinit();                           // [156]
    return;

worker_state_machine:
    // === WORKER THREADS (threads 1..N-1) ===
    // sub_2678420 generates this structure with these exact labels:
    worker_state_machine.begin:
        __kmpc_barrier_simple_generic(loc, gtid);     // [188] poll barrier
    .is_active.check:
        bool active = __kmpc_kernel_parallel(&fn);    // [171] check for work
        if (!active)
            goto .done.barrier;
    .parallel_region.check:
        if (fn == known_outlined_fn_1)
            goto .parallel_region.execute;
        // ... more checks for known outlined functions ...
        goto .fallback.execute;
    .parallel_region.execute:
        known_outlined_fn_1(args);                    // direct call (devirtualized)
        goto .done.barrier;
    .fallback.execute:
        fn(args);                                     // indirect call (generic)
    .done.barrier:
        __kmpc_kernel_end_parallel();                 // [172] signal completion
        __kmpc_barrier_simple_generic(loc, gtid);     // [188] sync barrier
        goto worker_state_machine.begin;
    .finished:
        return;
}
```

The state machine consumes five runtime calls per parallel-region invocation per worker thread: two `__kmpc_barrier_simple_generic` (ID 188) for poll/sync barriers, one `__kmpc_kernel_parallel` (ID 171) to check for dispatched work, one indirect or direct call to the outlined function, and one `__kmpc_kernel_end_parallel` (ID 172) to signal completion. Each `__kmpc_barrier_simple_generic` call compiles to a poll loop on a shared-memory flag -- not a hardware `bar.sync` -- because the generic barrier must handle the asymmetric wakeup protocol where the master thread signals workers through `__kmpc_kernel_prepare_parallel`.

### Worker State Machine Generator: `sub_2678420` (9 KB native)

When the SPMD transformation fails (eligibility flag `*(a1+241) == 0`), cicc falls back to `sub_2678420` which builds a *customized* state machine that is more efficient than the default runtime state machine. The customization replaces the indirect `fn(args)` call in `.fallback.execute` with a direct-call dispatch table when the set of outlined parallel functions is statically known.

| Property | Value |
|---|---|
| Function address | `sub_2678420` |
| Decompiled size | 41 KB |
| Basic block labels | `worker_state_machine.begin`, `.is_active.check`, `.parallel_region.check`, `.parallel_region.execute`, `.fallback.execute`, `.done.barrier`, `.finished` |
| Diagnostics | OMP130, OMP131, OMP132, OMP133 |

The generator has two modes:

**Mode 1: Remove unused state machine (OMP130).**  When the kernel has zero parallel regions (e.g., a `#pragma omp target` with no nested `parallel`), the state machine is dead code. `sub_2678420` removes the entire worker loop and emits: `"Removing unused state machine from generic-mode kernel."` (OMP130).

**Mode 2: Rewrite with customized dispatch (OMP131).**  When the kernel has N known parallel regions, the generator builds a switch/cascade of direct-call comparisons in `.parallel_region.check` and `.parallel_region.execute`, avoiding the overhead of indirect calls through `__kmpc_kernel_parallel`'s function pointer. It emits: `"Rewriting generic-mode kernel with a customized state machine."` (OMP131).

```c
// Customized state machine pseudocode (sub_2678420 output)
function build_custom_state_machine(kernel, parallel_regions):
    // Create the 6 basic blocks with labels above
    begin_bb   = create_block("worker_state_machine.begin")
    active_bb  = create_block(".is_active.check")
    check_bb   = create_block(".parallel_region.check")
    exec_bb    = create_block(".parallel_region.execute")
    fallback_bb = create_block(".fallback.execute")
    barrier_bb = create_block(".done.barrier")
    finished_bb = create_block(".finished")

    // Entry: poll barrier
    in begin_bb:
        call __kmpc_barrier_simple_generic(loc, gtid)  // [188]
        br .is_active.check

    // Check if master dispatched work
    in active_bb:
        %active = call i1 @__kmpc_kernel_parallel(&fn)  // [171]
        br %active, .parallel_region.check, .done.barrier

    // Devirtualized dispatch: compare fn pointer against known functions
    in check_bb:
        for i, region in enumerate(parallel_regions):
            %cmp = icmp eq fn, @outlined_fn_i
            br %cmp, .parallel_region.execute.i, next_check
        br .fallback.execute  // no match -- use indirect call

    // Direct call to known function (avoids indirect branch penalty)
    in exec_bb:
        for each matched region:
            call @outlined_fn_i(args)
            br .done.barrier

    // Fallback: indirect call (should be unreachable if analysis is complete)
    in fallback_bb:
        call fn(args)  // indirect
        br .done.barrier

    // End parallel + sync barrier
    in barrier_bb:
        call __kmpc_kernel_end_parallel()  // [172]
        call __kmpc_barrier_simple_generic(loc, gtid)  // [188]
        br .worker_state_machine.begin

    // Optional: exit (reached via __kmpc_target_deinit signaling)
    in finished_bb:
        ret void
```

The runtime calls consumed by `sub_2678420`:

| Call ID | Function | Role in State Machine |
|---|---|---|
| 155 | `__kmpc_target_init` | Kernel entry; returns -1 for workers |
| 156 | `__kmpc_target_deinit` | Kernel exit cleanup |
| 157 | `__kmpc_kernel_prepare_parallel` | Master signals workers with outlined fn pointer |
| 171 | `__kmpc_kernel_parallel` | Worker checks if work is dispatched; returns fn ptr |
| 172 | `__kmpc_kernel_end_parallel` | Worker signals completion of parallel region |
| 188 | `__kmpc_barrier_simple_generic` | Poll-based barrier (shared-memory flag loop) |

### SPMD Amenability Analysis Pipeline

The eligibility flag at `*(a1+241)` -- which gates whether `sub_26968A0` attempts the SPMD transformation -- is computed by the Attributor-based OpenMP optimization driver at `sub_269F530` (10 KB native). This driver orchestrates interprocedural fixed-point analysis using the standard LLVM Attributor framework.

The analysis pipeline:

```text
sub_269F530 (OpenMP Attributor Driver, 63 KB)
  |
  +-- sub_251BBC0 (AbstractAttribute infrastructure)
  |     Creates abstract attributes for each kernel, including
  |     the SPMD-compatibility tracker that will become a1+241.
  |
  +-- sub_251CD10 (Attributor::runTillFixpoint, 53 KB)
  |     Iterates up to openmp-opt-max-iterations (default: 256)
  |     times, updating abstract attribute states until convergence.
  |
  +-- sub_26747F0 (OpenMP kernel info collector)
        Populates the PassState structure (a1) with:
          a1+72:   function handle
          a1+160:  serial-code-present flag
          a1+224:  multiple-region flag
          a1+241:  SPMD-eligible boolean  <-- the gate
          a1+280:  parallel region array base
          a1+288:  parallel region count
          a1+304:  execution mode attribute map
```

The fixed-point analysis in `sub_251CD10` converges by iterating over all abstract attributes until none change state. For SPMD eligibility, the key attribute tracks three conditions that must all hold:

1. **Execution mode is Generic (mode byte == 1).** Read via `sub_2674090`/`sub_2674040` from the kernel's attribute map at `*(a1+304)`. If the kernel is already SPMD or Bare, no transformation is needed.

2. **All reachable callees are SPMD-amenable.** The analysis walks every call/invoke/callbr instruction in every parallel region of the kernel. Each callee is looked up in the SPMD-amenable function set at `*(a2+208)+34952`. This set is populated by two sources:
   - **Automatic population**: When `sub_312CF50` (the 194-case runtime declaration factory) creates a runtime function declaration, that function is automatically added to the set if it is known to be thread-safe (most `__kmpc_*` functions, all `omp_*` query functions).
   - **User annotation**: Functions declared with `[[omp::assume("ompx_spmd_amenable")]]` are inserted into the set by the attribute parser.

   The set uses the standard DenseMap infrastructure with LLVM-layer sentinels (-4096 / -8192); see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md). If any callee fails the lookup, the analysis sets `*(a1+241) = 0` and the transformation will emit OMP121 diagnostics instead.

3. **No unresolvable side effects.** Operations that are inherently unsafe when executed by all threads simultaneously -- such as I/O with ordering requirements, thread-local storage accesses assuming single-thread semantics, or calls to external functions with unknown side-effect profiles -- prevent SPMDization.

The Attributor driver at `sub_269F530` also feeds into `sub_2678420` (state machine generator) for kernels that fail SPMD eligibility, and into `sub_2680940` (parallel region merging) for kernels that pass. The decision tree:

```text
sub_269F530 analysis complete
  |
  +-- a1+241 == 1 (SPMD-eligible)
  |     |
  |     +-- a1+160 == 0 && a1+224 == 0 --> sub_26968A0 simple path
  |     +-- otherwise                   --> sub_26968A0 complex path
  |
  +-- a1+241 == 0 (not SPMD-eligible)
        |
        +-- has parallel regions --> sub_2678420 (custom state machine)
        +-- no parallel regions  --> sub_2678420 (remove dead state machine)
```

### How the SPMD Transform Eliminates the State Machine

The actual elimination happens in `sub_26968A0` and proceeds differently for simple vs. complex kernels, but the core mechanism is the same: replace the asymmetric master/worker execution model with symmetric all-thread execution.

**Step 1: Remove the `__kmpc_target_init` return-value gate.**  In Generic mode, `__kmpc_target_init` returns `-1` for workers and the kernel branches workers to the state machine loop. In SPMD mode, the return value is not used as a gate -- all threads fall through to user code. The transformation does not literally delete the `__kmpc_target_init` call (it is still needed for runtime initialization), but changes the execution mode attribute so the runtime initializes all threads as active.

**Step 2: Eliminate the worker loop entirely.**  The basic blocks `worker_state_machine.begin`, `.is_active.check`, `.parallel_region.check`, `.parallel_region.execute`, `.fallback.execute`, `.done.barrier`, and `.finished` become dead code once the execution mode flips to SPMD. They are not explicitly deleted by `sub_26968A0`; instead, setting mode=2 in the `KernelEnvironmentTy` means the runtime never creates the worker branch, so the dead blocks are eliminated by subsequent DCE passes.

**Step 3: Replace barrier primitives.**  Every `__kmpc_barrier_simple_generic` (ID 188) in the kernel is replaced with `__kmpc_barrier_simple_spmd` (ID 187). The difference:
- Generic barrier (ID 188): poll-based. Workers spin-check a shared-memory flag. The master writes the flag, then workers read it. This involves memory fences, cache-line bouncing, and potential bank conflicts. Compiles to a `ld.volatile.shared` + branch loop.
- SPMD barrier (ID 187): hardware-based. Maps directly to PTX `bar.sync` / CUDA `__syncthreads()`. Single instruction, handled by the warp scheduler with zero polling overhead.

**Step 4: Guard serial code.**  For the simple case (single parallel region), this is just:

```llvm
%tid = call i32 @__kmpc_get_hardware_thread_id_in_block()  ; [6]
%is_main = icmp eq i32 %tid, 0
br i1 %is_main, label %user_code, label %exit.threads
```

For the complex case (multiple parallel regions with serial gaps), the 5-block guarded region structure is created for each serial section, with shared-memory output promotion and double-barrier synchronization as described in Phase 4 above.

**Step 5: Update execution mode.**  The kernel attribute is rewritten from Generic (1) to SPMD (2) via the intermediate GENERIC_SPMD (3) marker. This is the final, irreversible step. Once the mode is set, `__kmpc_target_init` at runtime will launch all threads into user code instead of routing N-1 threads to a state machine.

### Performance Impact of Elimination

The state machine elimination saves:

| Source of overhead | Generic mode | SPMD mode | Savings |
|---|---|---|---|
| Worker idle polling | N-1 threads spin in `__kmpc_barrier_simple_generic` | No idle threads | 100% of idle cycles |
| Barrier latency | Poll-based shared-memory loop (10s-100s of cycles) | Hardware `bar.sync` (single cycle dispatch) | ~10-100x per barrier |
| Warp divergence at entry | Warp 0 diverges (thread 0 = master, threads 1-31 = workers) | No divergence | 1 warp fully utilized |
| Indirect calls | `__kmpc_kernel_parallel` returns fn ptr for indirect dispatch | No indirect calls -- outlined fn body inlined/direct | Branch predictor pressure eliminated |
| Register pressure | Workers hold state machine registers while idle | No state machine registers | Improved occupancy |
| Shared memory | Generic barriers use shared-memory flags | Only guarded-output allocations use shared memory | Reduced shared memory pressure |

On a typical `#pragma omp target parallel for` kernel, the SPMD transformation eliminates 5 runtime calls per parallel-region per worker-thread per iteration of the state machine loop. For a 256-thread CTA with one parallel region, that is 255 threads x 5 calls = 1,275 eliminated runtime calls per kernel invocation.

## Execution Mode Update

When the transformation succeeds, the kernel's execution mode attribute is updated from Generic (1) to SPMD (2). The update goes through an intermediate GENERIC_SPMD (3) state:

```c
// At LABEL_227 (shared success path)
bv_entry = sub_ACD640(*(a1+304+8), /*mode=*/3, /*aux=*/0);  // create mode-3 entry
current  = sub_2673FD0(*(a1+304));                           // read current attrs
*(a1+304) = sub_AAAE30(current, bv_entry, {key=0}, 1);      // write SPMD mode
```

The execution mode encoding matches upstream LLVM's `OMPTgtExecModeFlags`:

| Value | Name | Meaning |
|---|---|---|
| 0 | `OMP_TGT_EXEC_MODE_BARE` | Bare mode (no runtime) |
| 1 | `OMP_TGT_EXEC_MODE_GENERIC` | Generic (state machine) |
| 2 | `OMP_TGT_EXEC_MODE_SPMD` | SPMD (all threads active) |
| 3 | `OMP_TGT_EXEC_MODE_GENERIC_SPMD` | Generic | SPMD (transformation marker) |

The mode is stored in the `KernelEnvironmentTy` global variable that `__kmpc_target_init` reads at kernel launch. Setting it to SPMD tells the runtime to skip the state machine setup and launch all threads directly into user code.

## Limitations: What Prevents SPMDization

The following constructs cause the pass to emit OMP121 and fall back to Generic mode:

- **Calls to non-SPMD-amenable functions**: Any callee not in the SPMD-amenable set blocks transformation. The user override is `[[omp::assume("ompx_spmd_amenable")]]`.
- **Nested parallelism**: Kernels with nested `#pragma omp parallel` regions inside a target region cannot be SPMDized because the worker threads are already participating.
- **Tasking constructs**: `#pragma omp task`, `taskloop`, and taskgroup create runtime-managed work units incompatible with the SPMD execution model.
- **Critical sections and ordered regions**: These constructs require specific thread-identity semantics that conflict with SPMD guards.
- **Unresolvable side effects**: Calls to external functions whose side-effect profile is unknown (no declaration with `convergent` or `spmd_amenable` annotations).
- **Exception handling with unresolvable handlers**: Invoke instructions with non-zero exception handler counts that cannot be simplified block the transformation (checked via `sub_BD2BC0`).

## Comparison with Upstream LLVM OpenMPOpt

The cicc SPMD transformation in `sub_26968A0` is a proprietary reimplementation that predates upstream LLVM's SPMDization and differs in several significant ways:

| Aspect | Upstream LLVM OpenMPOpt | cicc `sub_26968A0` |
|---|---|---|
| Framework | Attributor-based (`AAKernelInfo`) | Standalone pass, direct IR mutation |
| Analysis approach | Fixed-point iteration via `SPMDCompatibilityTracker` | Pre-computed boolean flag at `a1+241` |
| Guarded regions | `insertInstructionGuardsHelper` using `SplitBlock` | Custom 5-block split with explicit worklist registration |
| Broadcast mechanism | `GlobalVariable` in shared memory (internal linkage, `UndefValue` init) | `alloca` in address space 7 (shared) via `sub_B30000` |
| Barrier | `__kmpc_barrier_simple_spmd` | Same: `__kmpc_barrier_simple_spmd` (call ID 187) |
| Hash tables | LLVM `DenseSet` / `SmallPtrSet` | Custom open-addressing with `-4096` sentinel ([details](../infra/hash-infrastructure.md)) |
| Region merging | Separate `openmp-opt-enable-merging` flag (disabled by default) | Integrated into the complex path; always runs when needed |
| State machine fallback | `buildCustomStateMachine` in same `AAKernelInfo::manifest` | Separate function `sub_2678420` (9 KB native) |
| Diagnostic IDs | OMP120, OMP121 (identical) | OMP120, OMP121 (identical) |
| `ompx_spmd_amenable` override | Same attribute name | Same attribute name |

The key architectural difference is that upstream LLVM uses the Attributor framework's fixed-point iteration to converge on SPMD compatibility, while cicc separates the analysis (which sets `a1+241`) from the transformation (which is `sub_26968A0`). This separation allows cicc to make a single pass over the IR for the transformation rather than iterating to a fixpoint, at the cost of less flexibility in handling interdependent kernels.

Upstream's region merging is behind `openmp-opt-enable-merging` and disabled by default. cicc's complex path (Phase 3a-3d) performs region merging unconditionally when a kernel has multiple parallel regions with serial gaps, suggesting NVIDIA found merging beneficial enough for GPU targets to enable it by default.

## Configuration Knobs

All knobs are standard LLVM `cl::opt` registrations present in the cicc binary. These match upstream LLVM options:

| Knob | Type | Default | Effect |
|---|---|---|---|
| `openmp-opt-disable` | bool | false | Disables all OpenMP optimizations |
| `openmp-opt-disable-spmdization` | bool | false | Disables SPMD transformation specifically |
| `openmp-opt-disable-deglobalization` | bool | false | Disables device memory deglobalization |
| `openmp-opt-disable-folding` | bool | false | Disables OpenMP folding optimizations |
| `openmp-opt-disable-state-machine-rewrite` | bool | false | Disables custom state machine generation |
| `openmp-opt-disable-barrier-elimination` | bool | false | Disables barrier elimination optimizations |
| `openmp-opt-disable-internalization` | bool | false | Disables function internalization |
| `openmp-opt-enable-merging` | bool | false | Enables parallel region merging (upstream default; cicc complex path always merges) |
| `openmp-opt-inline-device` | bool | false | Inlines all applicable device functions |
| `openmp-opt-verbose-remarks` | bool | false | Enables more verbose optimization remarks |
| `openmp-opt-max-iterations` | unsigned | 256 | Maximum attributor fixpoint iterations |
| `openmp-opt-shared-limit` | unsigned | UINT_MAX | Maximum shared memory usage for broadcast values |
| `openmp-opt-print-module-before` | bool | false | Dumps IR before OpenMP optimizations |
| `openmp-opt-print-module-after` | bool | false | Dumps IR after OpenMP optimizations |

Note: The `openmp-opt-shared-limit` knob controls how much shared memory can be consumed by broadcast value allocations in guarded regions. If the limit is exceeded, the transformation will not proceed for additional guarded outputs. The default of `UINT_MAX` effectively means no limit.

## Diagnostic Strings

| Code | Severity | Message | Trigger |
|---|---|---|---|
| OMP120 | Remark | "Transformed generic-mode kernel to SPMD-mode." | Successful transformation (both simple and complex paths) |
| OMP121 | Warning | "Value has potential side effects preventing SPMD-mode execution. Add `[[omp::assume(\"ompx_spmd_amenable\")]]` to the called function to override" | Callee not in SPMD-amenable set |
| OMP130-OMP133 | Various | State machine diagnostics | `sub_2678420` (fallback, not this pass) |
| OMP150 | Remark | Parallel region merging | `sub_2697xxx` (separate merging diagnostics) |

Diagnostics are emitted only when a handler is registered at `*(a2+4392)` and the handler's `isEnabled` virtual method (vtable offset +48) returns true. The construction follows the pattern: `sub_B174A0` (remark) or `sub_B178C0` (warning) builds a `DiagnosticInfo`, `sub_B18290` appends the message text, and `sub_1049740` emits to the handler.

## Runtime Call Dependencies

The transformation uses these runtime functions from the [OpenMP runtime declaration table](../openmp/runtime-table.md):

| Call ID | Function | Signature | Usage |
|---|---|---|---|
| 6 | `__kmpc_get_hardware_thread_id_in_block` | `i32()` | Thread identification for `tid == 0` guards |
| 180 | `__kmpc_alloc_shared` | `i8*(i64)` | Allocate shared memory for guarded output promotion (complex path) |
| 181 | `__kmpc_free_shared` | `void(i8*, i64)` | Free shared memory allocations at kernel exit (complex path) |
| 187 | `__kmpc_barrier_simple_spmd` | `void(ident_t*, i32)` | Lightweight SPMD barrier (maps to PTX `bar.sync`) |

The state machine fallback (`sub_2678420`) uses a different set of runtime calls, all of which become dead code after successful SPMD transformation:

| Call ID | Function | Signature | Eliminated by SPMD |
|---|---|---|---|
| 155 | `__kmpc_target_init` | `i32(KernelEnvironmentTy*, KernelLaunchEnvironmentTy*)` | Return value no longer gates workers |
| 156 | `__kmpc_target_deinit` | `void()` | Retained (still needed for cleanup) |
| 157 | `__kmpc_kernel_prepare_parallel` | `void(i8*)` | Eliminated -- no worker dispatch needed |
| 171 | `__kmpc_kernel_parallel` | `i1(i8**)` | Eliminated -- no worker polling loop |
| 172 | `__kmpc_kernel_end_parallel` | `void()` | Eliminated -- no worker completion signal |
| 188 | `__kmpc_barrier_simple_generic` | `void(ident_t*, i32)` | Replaced with ID 187 (SPMD barrier) |

Additionally, the SPMD-amenable function set at `*(a2+208)+34952` is populated by the runtime table builder (`sub_312CF50`) during module initialization. Functions declared via `sub_312CF50` cases 0-193 are automatically considered, along with user-annotated functions.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Generic-to-SPMD transformation pass (this function, 61 KB) | `sub_26968A0` | -- | -- |
| Worker state machine generation (Generic fallback, 41 KB) | `sub_2678420` | -- | -- |
| Attributor-based OpenMP optimization driver (63 KB, sets `a1+241`) | `sub_269F530` | -- | -- |
| Parallel region merging (52 KB) | `sub_2680940` | -- | -- |
| AbstractAttribute infrastructure (Attributor framework) | `sub_251BBC0` | -- | -- |
| Attributor::runTillFixpoint (53 KB, fixed-point iteration engine) | `sub_251CD10` | -- | -- |
| OpenMP kernel info collector (populates PassState) | `sub_26747F0` | -- | -- |
| Attributor Module Pass entry point (51 KB) | `sub_2591C20` | -- | -- |
| Read execution mode from attribute map | `sub_2674090` | -- | -- |
| Read execution mode (alternate entry) | `sub_2674040` | -- | -- |
| Get parallel region thread configuration | `sub_250CBE0` | -- | -- |
| Read attribute from kernel attribute map | `sub_2673FD0` | -- | -- |
| Create secondary barrier call | `sub_2673A60` | -- | -- |
| OpenMP runtime call table lookup by ID (194-case switch, 117 KB) | `sub_312CF50` | -- | -- |
| registerRuntimeFunction (registers declaration in table) | `sub_3122A50` | -- | -- |
| Parallel region outliner (47 KB, creates `.omp_par` functions) | `sub_313D1B0` | -- | -- |
| Get function entry basic block | `sub_25096F0` | -- | -- |
| Get function scope / debug info | `sub_BD5C60` | -- | -- |
| Build CFG region (start/end blocks) | `sub_AA8550` | -- | -- |
| Build exit/cleanup block | `sub_AA4D50` | -- | -- |
| Split basic block | `sub_F36960` | -- | -- |
| Allocate IR instruction node | `sub_BD2C40` | -- | -- |
| Fill instruction as runtime-call value load | `sub_B4A410` | -- | -- |
| Create integer constant (zero for tid check) | `sub_AD64C0` | -- | -- |
| Create integer constant (alternate entry, used in complex path) | `sub_AD6530` | -- | -- |
| Create icmp instruction | `sub_B52500` | -- | -- |
| Create branch instruction (opcode 3) | `sub_B4C9A0` | -- | -- |
| Create shared-memory alloca (addr space 7) | `sub_B30000` | -- | -- |
| Create store instruction | `sub_B4D460` | -- | -- |
| Create load instruction | `sub_B4D230` | -- | -- |
| Replace all uses of a value | `sub_256E5A0` | -- | -- |
| Create runtime library call instruction | `sub_921880` | -- | -- |
| Create bit-vector entry | `sub_ACD640` | -- | -- |
| Insert into attribute map | `sub_AAAE30` | -- | -- |
| Register block in pass manager worklist | `sub_D695C0` | -- | -- |
| Construct remark DiagnosticInfo | `sub_B174A0` | -- | -- |
| Construct warning DiagnosticInfo | `sub_B178C0` | -- | -- |
| Append string to diagnostic message | `sub_B18290` | -- | -- |
| Emit diagnostic to handler | `sub_1049740` | -- | -- |
| Check if instruction is a call | `sub_B46970` | -- | -- |
| Check if instruction is an invoke | `sub_B46420` | -- | -- |
| Get invoke exception handler count | `sub_BD2BC0` | -- | -- |
| Insert guard instructions at range boundary | `sub_B444E0` | -- | -- |
| Fast-path comparison instruction creation | `sub_AAB310` | -- | -- |
| Full comparison instruction creation | `sub_B523C0` | -- | -- |
| Build name from debug info + suffix | `sub_CA0F50` | -- | -- |
| Ref-count increment on metadata/debug-info | `sub_B96E90` | -- | -- |
| Ref-count decrement on metadata/debug-info | `sub_B91220` | -- | -- |
| Transfer metadata ownership between blocks | `sub_B976B0` | -- | -- |
| Get terminator's successor block pointer | `sub_986580` | -- | -- |
| Add operand bundle to instruction | `sub_B99FD0` | -- | -- |
| Duplicate metadata reference | `sub_266EF50` | -- | -- |
| Process entry block terminator successor | `sub_B491C0` | -- | -- |
| Get instruction value type | `sub_ACA8A0` | -- | -- |
| Get IR node name | `sub_BD5D20` | -- | -- |
| Vector push_back (dynamic arrays) | `sub_C8CC70` | -- | -- |
| Vector reserve/grow | `sub_C8D5F0` | -- | -- |

## Cross-References

- [OpenMP Runtime Declaration Table](../openmp/runtime-table.md) -- complete runtime function table (`sub_312CF50`), including `__kmpc_barrier_simple_spmd` (ID 187) and `__kmpc_get_hardware_thread_id_in_block` (ID 6)
- [Entry Point & CLI](../pipeline/entry.md) -- how OpenMP target offloading flags reach the optimizer
- [LLVM Optimizer](../pipeline/optimizer.md) -- pipeline slots 75/76/154 where `openmp-opt` runs
- [CLI Flags](../config/cli-flags.md) -- `openmp-opt-*` knob documentation
