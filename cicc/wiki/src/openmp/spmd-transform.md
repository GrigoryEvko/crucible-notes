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

```
warning: Value has potential side effects preventing SPMD-mode execution.
         Add `[[omp::assume("ompx_spmd_amenable")]]` to the called function
         to override [OMP121]
```

The diagnostic is constructed via `sub_B178C0` (warning constructor), message appended via `sub_B18290`, and emitted through `sub_1049740` to the handler at `*(a2+4392)`.

### Condition 3: No Unresolvable Side Effects

The kernel must not contain operations that are inherently unsafe when executed by multiple threads simultaneously -- for example, I/O operations with ordering requirements, or accesses to thread-local storage that assumes single-thread access.

### Legality Pseudocode

```
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

```
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

```
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

```
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

```
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

```
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

```
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

The state machine elimination is the core performance win. In Generic mode, the runtime generates a worker loop:

```c
// Generic mode worker loop (eliminated by SPMD transform)
void worker_state_machine() {
    while (true) {
        __kmpc_barrier_simple_generic();      // poll-based barrier
        if (parallel_level > 0) {
            outlined_parallel_fn();           // execute work
            __kmpc_barrier_simple_generic();  // sync after work
        }
    }
}
```

The SPMD transformation eliminates this loop entirely by:
1. Removing the master-thread gate at kernel entry (or converting it to a lightweight `tid == 0` check for non-parallelizable sections).
2. Making all threads execute the same code from the first instruction.
3. Replacing `__kmpc_barrier_simple_generic` with `__kmpc_barrier_simple_spmd` -- the generic variant involves state-machine polling while the SPMD variant maps directly to PTX `bar.sync`, which is a hardware-level warp synchronization primitive.
4. Wrapping serial code in explicit `tid == 0` guards with shared-memory output promotion and barriers.

The worker state machine generator (`sub_2678420`, 41 KB) is the fallback path -- it builds a custom state machine when SPMD transformation fails. Its diagnostics are OMP130-OMP133.

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
| Hash tables | LLVM `DenseSet` / `SmallPtrSet` | Custom open-addressing with linear probing and `-4096` sentinel |
| Region merging | Separate `openmp-opt-enable-merging` flag (disabled by default) | Integrated into the complex path; always runs when needed |
| State machine fallback | `buildCustomStateMachine` in same `AAKernelInfo::manifest` | Separate function `sub_2678420` (41 KB) |
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

The transformation uses two runtime functions from the [OpenMP runtime declaration table](../openmp/runtime-table.md):

| Call ID | Function | Signature | Usage |
|---|---|---|---|
| 6 | `__kmpc_get_hardware_thread_id_in_block` | `i32()` | Thread identification for guards |
| 187 | `__kmpc_barrier_simple_spmd` | `void(ident_t*, i32)` | Lightweight SPMD barrier (maps to `bar.sync`) |

Additionally, the SPMD-amenable function set at `*(a2+208)+34952` is populated by the runtime table builder (`sub_312CF50`) during module initialization. Functions declared via `sub_312CF50` cases 0-193 are automatically considered, along with user-annotated functions.

## Function Map

| Address | Identity |
|---|---|
| `sub_26968A0` | Generic-to-SPMD transformation pass (this function) |
| `sub_2678420` | Worker state machine generation (Generic fallback) |
| `sub_2674090` | Read execution mode from attribute map |
| `sub_2674040` | Read execution mode (alternate entry) |
| `sub_250CBE0` | Get parallel region thread configuration |
| `sub_2673FD0` | Read attribute from kernel attribute map |
| `sub_2673A60` | Create secondary barrier call |
| `sub_312CF50` | OpenMP runtime call table lookup by ID |
| `sub_25096F0` | Get function entry basic block |
| `sub_BD5C60` | Get function scope / debug info |
| `sub_AA8550` | Build CFG region (start/end blocks) |
| `sub_AA4D50` | Build exit/cleanup block |
| `sub_F36960` | Split basic block |
| `sub_BD2C40` | Allocate IR instruction node |
| `sub_B4A410` | Fill instruction as runtime-call value load |
| `sub_AD64C0` | Create integer constant (zero for tid check) |
| `sub_B52500` | Create icmp instruction |
| `sub_B4C9A0` | Create branch instruction (opcode 3) |
| `sub_B30000` | Create shared-memory alloca (addr space 7) |
| `sub_B4D460` | Create store instruction |
| `sub_B4D230` | Create load instruction |
| `sub_256E5A0` | Replace all uses of a value |
| `sub_921880` | Create runtime library call instruction |
| `sub_ACD640` | Create bit-vector entry |
| `sub_AAAE30` | Insert into attribute map |
| `sub_D695C0` | Register block in pass manager worklist |
| `sub_B174A0` | Construct remark DiagnosticInfo |
| `sub_B178C0` | Construct warning DiagnosticInfo |
| `sub_B18290` | Append string to diagnostic message |
| `sub_1049740` | Emit diagnostic to handler |
| `sub_B46970` | Check if instruction is a call |
| `sub_B46420` | Check if instruction is an invoke |
| `sub_BD2BC0` | Get invoke exception handler count |
| `sub_B444E0` | Insert guard instructions at range boundary |
| `sub_AAB310` | Fast-path comparison instruction creation |
| `sub_B523C0` | Full comparison instruction creation |
| `sub_CA0F50` | Build name from debug info + suffix |

## Cross-References

- [OpenMP Runtime Declaration Table](../openmp/runtime-table.md) -- complete runtime function table (`sub_312CF50`), including `__kmpc_barrier_simple_spmd` (ID 187) and `__kmpc_get_hardware_thread_id_in_block` (ID 6)
- [Entry Point & CLI](../pipeline/entry.md) -- how OpenMP target offloading flags reach the optimizer
- [LLVM Optimizer](../pipeline/optimizer.md) -- pipeline slots 75/76/154 where `openmp-opt` runs
- [CLI Flags](../config/cli-flags.md) -- `openmp-opt-*` knob documentation
