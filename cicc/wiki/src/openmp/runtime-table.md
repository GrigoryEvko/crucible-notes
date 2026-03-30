# OpenMP Runtime Declaration Table

cicc embeds a 194-entry table of OpenMP runtime function declarations at `sub_312CF50` (0x312CF50, 117 KB decompiled). This single function is the authoritative source for every `__kmpc_*`, `omp_*`, and `__tgt_*` device-runtime call the compiler can emit into NVPTX IR. It defines the complete ABI contract between compiler-generated GPU code and the OpenMP device runtime library (libomptarget / libomp). The function takes an integer case index (0--193), constructs the corresponding `FunctionType`, checks whether the symbol already exists in the module via `Module::getNamedValue`, and if absent, creates a `Function::Create` with `ExternalLinkage`. The result is registered into a context-local array so that any later codegen pass can reference a runtime function by its numeric index without reconstructing the type.

Upstream LLVM defines the same runtime function set declaratively in `llvm/include/llvm/Frontend/OpenMP/OMPKinds.def` using the `__OMP_RTL` macro, which the `OMPIRBuilder` expands at construction time. cicc's table is a procedural equivalent: a giant `switch(a3)` with 194 cases that does exactly what `OMPKinds.def` + `OMPIRBuilder::initialize()` do, but compiled into the binary rather than generated from a `.def` file. The ordering of cases 0--193 matches the upstream `OMPRTL_` enum one-to-one, confirming that cicc v13.0 tracks LLVM 18.x's OpenMP runtime interface.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_312CF50` @ `0x312CF50` |
| Decompiled size | 117 KB |
| Total entries | 194 (indices 0--193) |
| Sentinel | index 193 = `__last` (void function, marks table end) |
| Varargs entries | 2: index 7 (`__kmpc_fork_call`), index 118 (`__kmpc_fork_teams`) |
| Linkage for all entries | `ExternalLinkage` (encoded as 0x103 = 259) |
| Special attribute | Attribute #26 applied to indices 7 and 118 post-creation |
| Registration helper | `sub_3122A50(context, index, funcDecl)` |
| Type construction | `sub_BCF480` = `FunctionType::get` |
| Symbol lookup | `sub_BA8CB0` = `Module::getNamedValue` |
| Function creation | `sub_B2C660` = `Function::Create` |
| Upstream equivalent | `OMPKinds.def` `__OMP_RTL` entries + `OMPIRBuilder::initialize()` |

## Context Object Type Cache

The first parameter `a1` points to the OpenMP runtime context object. Starting at offset +2600, it contains a pre-allocated cache of LLVM types used to construct function signatures, avoiding redundant `Type::get*` calls:

| Offset | Type | LLVM equivalent |
|---|---|---|
| +2600 | `void` | `Type::getVoidTy` |
| +2608 | `i1` | `Type::getInt1Ty` |
| +2616 | `i8` | `Type::getInt8Ty` |
| +2624 | `i16` | `Type::getInt16Ty` |
| +2632 | `i32` | `Type::getInt32Ty` |
| +2640 | `i64` | `Type::getInt64Ty` |
| +2648 | `i8*` | `PointerType::get(i8, 0)` |
| +2664 | `i32*` | `PointerType::get(i32, 0)` |
| +2672 | `i64*` | `PointerType::get(i64, 0)` |
| +2680 | `double` | `Type::getDoubleTy` |
| +2688 | `i64` / `size_t` | `DataLayout::getIntPtrType` |
| +2704 | `i8*` (generic ptr) | `PointerType::get(i8, 0)` |
| +2712 | `i8**` | `PointerType::get(i8*, 0)` |
| +2720 | `i8***` | `PointerType::get(i8**, 0)` |
| +2752 | `kmp_critical_name*` | `[8 x i32]*` |
| +2784 | `ident_t*` | `{i32, i32, i32, i32, i8*}*` |
| +2800 | `__tgt_kernel_arguments*` | 13-field struct pointer |
| +2816 | `__tgt_async_info*` | `{i8*}*` |
| +2896 | `KernelEnvironmentTy*` | `{ConfigEnv, ident_t*, DynEnv*}*` |
| +2912 | `KernelLaunchEnvironmentTy*` | `{i32, i32}*` |
| +2928 | `kmpc_micro` | `void(i32*, i32*, ...)*` (varargs microtask) |
| +2944 | `kmp_reduce_func` | `void(i8*, i8*)*` |
| +2960 | `kmp_copy_func` | `void(i8*, i8*)*` |
| +3008 | `kmpc_ctor` | `i8*(i8*)*` |
| +3024 | `kmp_routine_entry_t` | `i32(i32, i8*)*` |
| +3040 | `kmp_ShuffleReductFctPtr` | `void(i8*, i16, i16, i16)*` |
| +3056 | `kmp_InterWarpCopyFctPtr` | `void(i8*, i32)*` |
| +3072 | `kmp_ListGlobalFctPtr` | `void(i8*, i32, i8*)*` |

This layout mirrors the `OMP_TYPE`, `OMP_STRUCT_TYPE`, and `OMP_FUNCTION_TYPE` sections of upstream `OMPKinds.def`. The struct type definitions for `ident_t`, `KernelEnvironmentTy`, and `__tgt_kernel_arguments` match the upstream `__OMP_STRUCT_TYPE` declarations exactly.

## Execution Modes: SPMD vs Generic

GPU OpenMP kernels operate in one of two execution modes, and the choice fundamentally determines which runtime functions the compiler emits:

| Mode | Value | Description | Worker threads |
|---|---|---|---|
| Generic | 1 | Master-worker state machine. Only thread 0 runs serial code; workers spin in a polling loop (`__kmpc_barrier_simple_generic`). Parallel regions are dispatched via `__kmpc_kernel_prepare_parallel` / `__kmpc_kernel_parallel`. | Idle until parallel region |
| SPMD | 2 | All threads execute the same code from kernel entry. Serial sections between parallel regions are guarded by `tid == 0` checks with shared-memory output promotion and `__kmpc_barrier_simple_spmd` barriers. | Active from first instruction |
| Generic-SPMD | 3 | Transient state during the Generic-to-SPMD transformation. Never observed at runtime. | N/A |

The execution mode is encoded in a bit-vector attached to the kernel function's metadata. The runtime function `__kmpc_target_init` (index 155) reads the `KernelEnvironmentTy` struct which embeds the `ConfigurationEnvironmentTy` -- the first byte of that inner struct encodes the execution mode. `__kmpc_is_spmd_exec_mode` (index 186) queries it at runtime.

The SPMD-vs-Generic distinction affects which runtime calls appear in the generated IR:

- **Generic mode** kernels call `__kmpc_kernel_prepare_parallel`, `__kmpc_kernel_parallel`, `__kmpc_kernel_end_parallel`, `__kmpc_barrier_simple_generic`, and the full `__kmpc_fork_call` microtask dispatch.
- **SPMD mode** kernels call `__kmpc_parallel_51` (index 158) for nested parallelism, `__kmpc_barrier_simple_spmd` for synchronization, and `__kmpc_alloc_shared` / `__kmpc_free_shared` for shared-memory output promotion between guarded and parallel sections.
- Both modes call `__kmpc_target_init` / `__kmpc_target_deinit` for kernel lifecycle management.

## Complete Runtime Function Table

All 194 entries, organized by functional category. The "Index" column is the `switch` case in `sub_312CF50` and the slot in the context's runtime function array. Signatures use LLVM IR type syntax.

### Standard OpenMP Runtime (0--13)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 0 | `__kmpc_barrier` | `void(ident_t*, i32)` | Explicit barrier |
| 1 | `__kmpc_cancel` | `i32(ident_t*, i32, i32)` | Cancel construct |
| 2 | `__kmpc_cancel_barrier` | `void(ident_t*, i32)` | Implicit barrier with cancellation check |
| 3 | `__kmpc_error` | `void(ident_t*, i32, i8*)` | Runtime error reporting |
| 4 | `__kmpc_flush` | `void(ident_t*)` | Memory fence |
| 5 | `__kmpc_global_thread_num` | `i32(ident_t*)` | Get global thread ID |
| 6 | `__kmpc_get_hardware_thread_id_in_block` | `i32()` | GPU: threadIdx within CTA |
| 7 | `__kmpc_fork_call` | `void(ident_t*, i32, kmpc_micro, ...)` | Fork parallel region (varargs) |
| 8 | `__kmpc_fork_call_if` | `void(ident_t*, i32, i32, i8*, i32)` | Conditional fork |
| 9 | `__kmpc_omp_taskwait` | `void(ident_t*, i32)` | Taskwait |
| 10 | `__kmpc_omp_taskyield` | `i32(ident_t*, i32, i32)` | Task yield point |
| 11 | `__kmpc_push_num_threads` | `void(ident_t*, i32, i32)` | Set thread count for next parallel |
| 12 | `__kmpc_push_proc_bind` | `void(ident_t*, i32, i32)` | Set affinity for next parallel |
| 13 | `__kmpc_omp_reg_task_with_affinity` | `i32(ident_t*, i32, i8*, i32, i8*)` | Register task with affinity info |

Index 7 (`__kmpc_fork_call`) and index 118 (`__kmpc_fork_teams`) are the only two varargs entries. Both receive special post-processing: `sub_B994D0` sets function attribute #26 (likely the `convergent` attribute or a varargs-related marker), checked via `sub_B91C10`.

### Hardware Query (14--16)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 14 | `__kmpc_get_hardware_num_blocks` | `i32()` | gridDim.x equivalent |
| 15 | `__kmpc_get_hardware_num_threads_in_block` | `i32()` | blockDim.x equivalent |
| 16 | `__kmpc_get_warp_size` | `i32()` | Warp size (32 on NVIDIA) |

These three functions have no parameters -- they are direct wrappers around PTX special registers (`%nctaid.x`, `%ntid.x`, and a compile-time constant 32).

### OMP Standard Library API (17--45)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 17 | `omp_get_thread_num` | `i32()` | Thread ID within team |
| 18 | `omp_get_num_threads` | `i32()` | Threads in current team |
| 19 | `omp_get_max_threads` | `i32()` | Max threads available |
| 20 | `omp_in_parallel` | `i32()` | Inside parallel region? |
| 21 | `omp_get_dynamic` | `i32()` | Dynamic adjustment enabled? |
| 22 | `omp_get_cancellation` | `i32()` | Cancellation enabled? |
| 23 | `omp_get_nested` | `i32()` | Nested parallelism enabled? |
| 24 | `omp_get_schedule` | `void(i32*, i32*)` | Query loop schedule |
| 25 | `omp_get_thread_limit` | `i32()` | Max total threads |
| 26 | `omp_get_supported_active_levels` | `i32()` | Max supported nesting |
| 27 | `omp_get_max_active_levels` | `i32()` | Current max nesting |
| 28 | `omp_get_level` | `i32()` | Current nesting depth |
| 29 | `omp_get_ancestor_thread_num` | `i32(i32)` | Ancestor thread ID |
| 30 | `omp_get_team_size` | `i32(i32)` | Team size at nesting level |
| 31 | `omp_get_active_level` | `i32()` | Active parallel nesting |
| 32 | `omp_in_final` | `i32()` | Inside final task? |
| 33 | `omp_get_proc_bind` | `i32()` | Current binding policy |
| 34 | `omp_get_num_places` | `i32()` | Number of places |
| 35 | `omp_get_num_procs` | `i32()` | Available processors |
| 36 | `omp_get_place_proc_ids` | `void(i32, i32*)` | Processor IDs in place |
| 37 | `omp_get_place_num` | `i32()` | Current place number |
| 38 | `omp_get_partition_num_places` | `i32()` | Places in partition |
| 39 | `omp_get_partition_place_nums` | `void(i32*)` | Place numbers in partition |
| 40 | `omp_get_wtime` | `double()` | Wall clock time |
| 41 | `omp_set_num_threads` | `void(i32)` | Set thread count |
| 42 | `omp_set_dynamic` | `void(i32)` | Enable/disable dynamic |
| 43 | `omp_set_nested` | `void(i32)` | Enable/disable nesting |
| 44 | `omp_set_schedule` | `void(i32, i32)` | Set loop schedule |
| 45 | `omp_set_max_active_levels` | `void(i32)` | Set max nesting |

These are the user-facing OpenMP API functions. On GPU, most return compile-time constants or trivial register reads.

### Master/Masked Constructs (46--49)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 46 | `__kmpc_master` | `i32(ident_t*, i32)` | Enter master region (returns 1 for master thread) |
| 47 | `__kmpc_end_master` | `void(ident_t*, i32)` | Exit master region |
| 48 | `__kmpc_masked` | `i32(ident_t*, i32, i32)` | Enter masked region (OMP 5.1, filtered thread) |
| 49 | `__kmpc_end_masked` | `void(ident_t*, i32)` | Exit masked region |

### Critical Sections (50--52)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 50 | `__kmpc_critical` | `void(ident_t*, i32, kmp_critical*)` | Enter critical section |
| 51 | `__kmpc_critical_with_hint` | `void(ident_t*, i32, i32, kmp_critical*)` | Enter with lock hint |
| 52 | `__kmpc_end_critical` | `void(ident_t*, i32, kmp_critical*)` | Exit critical section |

On GPU, critical sections use atomic operations on global memory. The `kmp_critical_name` type is `[8 x i32]` (32 bytes), used as an atomic lock variable. The `_with_hint` variant accepts a contention hint (e.g., speculative, non-speculative, uncontended) that the GPU runtime maps to different atomic strategies.

### Reduction (55--58)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 55 | `__kmpc_reduce` | `i32(ident_t*, i32, i32, i64, i8*, kmp_reduce_func, kmp_critical*)` | Begin reduction (blocking) |
| 56 | `__kmpc_reduce_nowait` | `i32(ident_t*, i32, i32, i64, i8*, kmp_reduce_func, kmp_critical*)` | Begin reduction (non-blocking) |
| 57 | `__kmpc_end_reduce` | `void(ident_t*, i32, kmp_critical*)` | End reduction (blocking) |
| 58 | `__kmpc_end_reduce_nowait` | `void(ident_t*, i32, kmp_critical*)` | End reduction (non-blocking) |

These are the standard reduction protocol entries. On GPU, the compiler typically prefers the NVIDIA-specific shuffle-based reductions (indices 176--178) which are significantly faster.

### Static Loop Scheduling (61--70)

| Index | Function | Signature |
|---|---|---|
| 61--64 | `__kmpc_for_static_init_{4,4u,8,8u}` | `void(ident_t*, i32, i32, i32*, {i32,i64}*, {i32,i64}*, {i32,i64}*, {i32,i64}*, {i32,i64}, {i32,i64})` |
| 65 | `__kmpc_for_static_fini` | `void(ident_t*, i32)` |
| 66--69 | `__kmpc_distribute_static_init_{4,4u,8,8u}` | Same 9-param shape as 61--64 |
| 70 | `__kmpc_distribute_static_fini` | `void(ident_t*, i32)` |

The `_4` / `_4u` / `_8` / `_8u` suffixes indicate signed-32, unsigned-32, signed-64, unsigned-64 loop variable types respectively. All `static_init` functions take 9 parameters: location, thread ID, schedule type, pointer to is-last flag, pointers to lower/upper/stride/incr bounds, and chunk size.

### Dynamic Dispatch (71--87)

Indices 71--74 handle `distribute` + dynamic dispatch initialization. Indices 75--82 handle standard `dispatch_init` and `dispatch_next` for the four integer widths. Indices 83--87 are dispatch finalization. Total: 17 entries covering the full dynamic loop scheduling interface.

### Team Static & Combined Distribute-For (88--95)

Indices 88--91 (`__kmpc_team_static_init_{4,4u,8,8u}`) handle team-level static work distribution. Indices 92--95 (`__kmpc_dist_for_static_init_{4,4u,8,8u}`) are the combined `distribute parallel for` static init, taking 10 parameters (the extra parameter is the `distribute` upper bound pointer).

### Tasking (98--116)

19 entries covering the full OpenMP tasking interface:

| Index | Function | Key detail |
|---|---|---|
| 98 | `__kmpc_omp_task_alloc` | Returns `i8*` (task descriptor), 6 params |
| 99 | `__kmpc_omp_task` | Submit allocated task for execution |
| 100--101 | `__kmpc_end_taskgroup` / `__kmpc_taskgroup` | Task group synchronization |
| 102--103 | `__kmpc_omp_task_begin_if0` / `complete_if0` | Immediate (if(0)) task execution |
| 104 | `__kmpc_omp_task_with_deps` | Task with dependency list (7 params) |
| 105--106 | `__kmpc_taskloop` / `__kmpc_taskloop_5` | Taskloop construct (11/12 params) |
| 107 | `__kmpc_omp_target_task_alloc` | Target-offload task allocation (7 params) |
| 108--113 | `__kmpc_taskred_*` / `__kmpc_task_reduction_*` | Task reduction infrastructure |
| 114 | `__kmpc_proxy_task_completed_ooo` | Out-of-order proxy task completion |
| 115--116 | `__kmpc_omp_wait_deps` / `_deps_51` | Dependency wait (OMP 5.0/5.1) |

Index 106 (`__kmpc_taskloop_5`) and index 116 (`__kmpc_omp_taskwait_deps_51`) are OMP 5.1 additions with an extra modifier parameter compared to their predecessors.

### Teams and Cancellation (117--121)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 117 | `__kmpc_cancellationpoint` | `i32(ident_t*, i32, i32)` | Cancellation point check |
| 118 | `__kmpc_fork_teams` | `void(ident_t*, i32, kmpc_micro, ...)` | Fork teams region (varargs) |
| 119 | `__kmpc_push_num_teams` | `void(ident_t*, i32, i32, i32)` | Set team count |
| 120 | `__kmpc_push_num_teams_51` | `void(ident_t*, i32, i32, i32, i32)` | Set team count (OMP 5.1, 5 params) |
| 121 | `__kmpc_set_thread_limit` | `void(ident_t*, i32, i32)` | Set per-team thread limit |

### Target Offloading (137--153)

18 entries implementing the host-side target offloading protocol. These are primarily used when cicc compiles host code that launches GPU kernels, not within device code itself:

| Index | Function | Params | Purpose |
|---|---|---|---|
| 137 | `__kmpc_push_target_tripcount_mapper` | 3 | Set iteration count for target region |
| 138--141 | `__tgt_target_*_mapper` / `_nowait_mapper` | 10--16 | Launch target region with data mapping |
| 142--143 | `__tgt_target_kernel` / `_nowait` | 6/10 | New-style kernel launch (takes `__tgt_kernel_arguments*`) |
| 144--151 | `__tgt_target_data_*_mapper` / `_nowait_mapper` | 9--13 | Data map-to/from/update operations |
| 152--153 | `__tgt_mapper_num_components` / `push_mapper_component` | 1/6 | User-defined mapper support |

### GPU Kernel Lifecycle (155--158)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 155 | `__kmpc_target_init` | `i32(KernelEnvironmentTy*, KernelLaunchEnvironmentTy*)` | Kernel entry: initialize runtime, returns thread role |
| 156 | `__kmpc_target_deinit` | `void()` | Kernel exit: cleanup |
| 157 | `__kmpc_kernel_prepare_parallel` | `void(i8*)` | Generic mode: signal workers to execute outlined function |
| 158 | `__kmpc_parallel_51` | `void(ident_t*, i32, i32, i32, i32, i8*, i8*, i8**, i64)` | OMP 5.1 GPU parallel dispatch (9 params) |

`__kmpc_target_init` is the first runtime call in every GPU OpenMP kernel. In Generic mode, it returns -1 for worker threads (which should enter the polling loop) and 0 for the master thread. In SPMD mode, it returns 0 for all threads. The `KernelEnvironmentTy` struct carries the `ConfigurationEnvironmentTy` which encodes the execution mode, team sizes, and runtime configuration.

### New-Style Static Loops, OMP 5.1+ (159--170)

12 entries implementing the callback-based loop interface introduced in OpenMP 5.1:

| Index | Function | Signature |
|---|---|---|
| 159--162 | `__kmpc_for_static_loop_{4,4u,8,8u}` | `void(ident_t*, i8*, i8*, {i32,i64}, {i32,i64}, {i32,i64})` |
| 163--166 | `__kmpc_distribute_static_loop_{4,4u,8,8u}` | `void(ident_t*, i8*, i8*, {i32,i64}, {i32,i64})` |
| 167--170 | `__kmpc_distribute_for_static_loop_{4,4u,8,8u}` | `void(ident_t*, i8*, i8*, {i32,i64}, {i32,i64}, {i32,i64}, {i32,i64})` |

Unlike the old-style `_init`/`_fini` pairs, these new-style loops take function pointer callbacks (`i8*` for the loop body and data pointer) and handle initialization + execution + finalization in a single call.

### Legacy Kernel-Mode Parallel (171--174)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 171 | `__kmpc_kernel_parallel` | `i1(i8**)` | Generic mode: worker checks if parallel work available |
| 172 | `__kmpc_kernel_end_parallel` | `void()` | Generic mode: worker signals completion |
| 173 | `__kmpc_serialized_parallel` | `void(ident_t*, i32)` | Execute parallel region serially (if(0) parallel) |
| 174 | `__kmpc_end_serialized_parallel` | `void(ident_t*, i32)` | End serialized parallel |

These are the Generic-mode worker-side functions. `__kmpc_kernel_parallel` returns `true` when the master thread has dispatched work via `__kmpc_kernel_prepare_parallel`, writing the outlined function pointer into the output parameter.

### Warp-Level Primitives (175, 179, 189--190)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 175 | `__kmpc_shuffle_int32` | `i32(i32, i16, i16)` | Warp shuffle for 32-bit value |
| 179 | `__kmpc_shuffle_int64` | `i64(i64, i16, i16)` | Warp shuffle for 64-bit value |
| 189 | `__kmpc_warp_active_thread_mask` | `i64()` | Active lane mask (PTX `activemask`) |
| 190 | `__kmpc_syncwarp` | `void(i64)` | Warp-level barrier with mask |

The shuffle functions take `(value, lane_offset, warp_size)` and implement butterfly-pattern data exchange for intra-warp reductions. These compile down to PTX `shfl.sync` instructions.

### NVIDIA Device Reduction (176--178)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 176 | `__kmpc_nvptx_parallel_reduce_nowait_v2` | `i32(ident_t*, i64, i8*, ShuffleReductFctPtr, InterWarpCopyFctPtr)` | Intra-CTA parallel reduction |
| 177 | `__kmpc_nvptx_teams_reduce_nowait_v2` | `i32(ident_t*, i32, i8*, i64, i8*, ShuffleReductFctPtr, InterWarpCopyFctPtr, ListGlobalFctPtr, ListGlobalFctPtr, ListGlobalFctPtr, ListGlobalFctPtr)` | Cross-CTA team reduction (11 params) |
| 178 | `__kmpc_reduction_get_fixed_buffer` | `i8*()` | Get global reduction scratch buffer |

These are the GPU-specific reduction entries -- the single most important performance-critical runtime calls for OpenMP on NVIDIA GPUs. The parallel reduction (index 176) uses a two-phase approach: (1) intra-warp reduction via shuffle, then (2) inter-warp reduction via shared memory copy. The compiler generates the `ShuffleReductFctPtr` and `InterWarpCopyFctPtr` callback functions as outlined helpers that the runtime calls during the reduction tree.

The teams reduction (index 177) adds four `ListGlobalFctPtr` callbacks for managing global memory buffers across CTAs, plus an extra size parameter. This is the most complex runtime call in the entire table, with 11 parameters.

### Shared Memory Management (180--184)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 180 | `__kmpc_alloc_shared` | `i8*(i64)` | Dynamic shared memory allocation |
| 181 | `__kmpc_free_shared` | `void(i8*, i64)` | Free shared memory |
| 182 | `__kmpc_begin_sharing_variables` | `void(i8***, i64)` | Begin variable sharing protocol |
| 183 | `__kmpc_end_sharing_variables` | `void()` | End sharing protocol |
| 184 | `__kmpc_get_shared_variables` | `i8**()` | Get shared variable array |

`__kmpc_alloc_shared` / `__kmpc_free_shared` are heavily used in the SPMD transformation's guarded output mechanism: values computed by the master thread that are needed by all threads are stored into dynamically-allocated shared memory, synchronized via barrier, then loaded by all threads.

### SPMD Mode Detection (185--188)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 185 | `__kmpc_parallel_level` | `i16(ident_t*, i32)` | Current parallel nesting depth |
| 186 | `__kmpc_is_spmd_exec_mode` | `i8()` | Returns 1 if SPMD, 0 if Generic |
| 187 | `__kmpc_barrier_simple_spmd` | `void(ident_t*, i32)` | Lightweight barrier for SPMD mode (`bar.sync`) |
| 188 | `__kmpc_barrier_simple_generic` | `void(ident_t*, i32)` | State-machine barrier for Generic mode |

The two barrier variants reflect the fundamental mode difference. `__kmpc_barrier_simple_spmd` compiles to a single `bar.sync` instruction. `__kmpc_barrier_simple_generic` involves polling a shared-memory flag because workers are in a state-machine loop that must check for new work after each barrier.

### Profiling (191--192) and Sentinel (193)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 191 | `__llvm_profile_register_function` | `void(i8*)` | PGO: register function for profiling |
| 192 | `__llvm_profile_register_names_function` | `void(i8*, i64)` | PGO: register name table |
| 193 | `__last` | `void()` | Sentinel marking table end |

The two `__llvm_profile_*` entries support profile-guided optimization instrumentation on GPU. The `__last` sentinel at index 193 is a void-to-void function that marks the end of the table; it is never called at runtime.

## Declaration Construction Protocol

For each runtime function, `sub_312CF50` follows an identical protocol:

```c
// Pseudocode for a typical case (e.g., case 0: __kmpc_barrier)
case 0: {
    // 1. Build parameter type array from cached types
    Type *params[] = { ctx->ident_t_ptr, ctx->i32_ty };  // a1+2784, a1+2632

    // 2. Construct FunctionType
    FunctionType *fty = FunctionType::get(
        ctx->void_ty,   // return type (a1+2600)
        params, 2,       // param array + count
        /*isVarArg=*/false
    );

    // 3. Check if symbol already exists in module
    Value *existing = Module::getNamedValue("__kmpc_barrier");
    if (existing == a2)  // a2 is the existing-check value
        return existing;

    // 4. Create new function declaration
    Function *decl = Function::Create(
        fty,
        259,             // linkage = ExternalLinkage (0x103)
        "__kmpc_barrier",
        module
    );

    // 5. Register in context table
    registerRuntimeFunction(a1, /*index=*/0, decl);  // sub_3122A50

    return decl;
}
```

The linkage value 259 (0x103) decodes as `ExternalLinkage` with the DLLImport storage class flag set. This is consistent across all 194 entries.

For the two varargs entries (indices 7 and 118), the `FunctionType::get` call passes `isVarArg=true`, and after `Function::Create`, the code calls `sub_B994D0` to add attribute #26 and `sub_B91C10` to verify it was applied. Attribute #26 likely corresponds to a convergent-or-varargs marker that prevents the optimizer from incorrectly transforming these calls.

## Comparison with Upstream LLVM OMPKinds.def

cicc's table maps one-to-one with the `__OMP_RTL` entries in LLVM 18.x's `OMPKinds.def`. The ordering is identical: the enum `OMPRTL___kmpc_barrier` = 0 corresponds to cicc's case 0, and so on through `OMPRTL___last` = 193 at case 193.

Key differences from upstream:

1. **Procedural vs declarative.** Upstream uses X-macros (`__OMP_RTL`) expanded by `OMPIRBuilder::initialize()` to lazily create declarations on first use. cicc's `sub_312CF50` is a compiled switch statement that eagerly creates declarations when requested by case index.

2. **Type representation.** Upstream uses opaque pointer types (`PointerType::get(Ctx, 0)`) throughout. cicc preserves typed pointers (`i8*`, `i32*`, `i64*`, struct pointers) in its type cache, consistent with LLVM's pre-opaque-pointer era. This is because cicc's internal IR (NVVM IR) still uses typed pointers even though upstream LLVM has migrated to opaque pointers.

3. **Missing entries.** cicc lacks `__kmpc_push_num_threads_strict` (present in latest upstream) and uses `__kmpc_parallel_51` where upstream LLVM 18.x defines `__kmpc_parallel_60` with a slightly different signature. The `_51` name indicates cicc v13.0 targets the OMP 5.1 runtime ABI, not the OMP 6.0 draft.

4. **Attribute handling.** Upstream `OMPKinds.def` includes extensive attribute sets (`GetterAttrs`, `SetterAttrs`, etc.) that annotate runtime functions with `nounwind`, `nosync`, `nofree`, `willreturn`, and memory effect attributes for optimization. cicc applies only attribute #26 to the two varargs functions and otherwise relies on the OpenMPOpt pass to infer attributes.

5. **The `__tgt_interop_*` entries** (indices 132--134) in cicc take a slightly different parameter list than upstream: cicc includes an extra `i32` parameter at the end that upstream encodes differently, reflecting a minor ABI divergence in the interop interface.

## Configuration Knobs

All LLVM `cl::opt` knobs related to OpenMP optimization, as found in the cicc binary:

| Knob | Type | Default | Effect |
|---|---|---|---|
| `openmp-opt-disable` | `bool` | `false` | Disable all OpenMP optimizations |
| `openmp-opt-enable-merging` | `bool` | `false` | Enable parallel region merging |
| `openmp-opt-disable-internalization` | `bool` | `false` | Skip function internalization |
| `openmp-opt-disable-deglobalization` | `bool` | `false` | Skip global-to-local promotion |
| `openmp-opt-disable-spmdization` | `bool` | `false` | Skip Generic-to-SPMD transformation |
| `openmp-opt-disable-folding` | `bool` | `false` | Skip ICV folding |
| `openmp-opt-disable-state-machine-rewrite` | `bool` | `false` | Skip state machine optimization |
| `openmp-opt-disable-barrier-elimination` | `bool` | `false` | Skip redundant barrier removal |
| `openmp-opt-inline-device` | `bool` | varies | Inline device runtime calls |
| `openmp-opt-verbose-remarks` | `bool` | `false` | Emit detailed optimization remarks |
| `openmp-opt-max-iterations` | `int` | varies | Fixed-point iteration limit for analysis |
| `openmp-opt-shared-limit` | `int` | varies | Max shared memory for SPMD output promotion |
| `openmp-opt-print-module-after` | `bool` | `false` | Dump module IR after OpenMP optimization |
| `openmp-opt-print-module-before` | `bool` | `false` | Dump module IR before OpenMP optimization |
| `openmp-deduce-icv-values` | `bool` | varies | Deduce Internal Control Variable values |
| `openmp-print-icv-values` | `bool` | `false` | Print deduced ICV values |
| `openmp-print-gpu-kernels` | `bool` | `false` | Print identified GPU kernels |
| `openmp-hide-memory-transfer-latency` | `bool` | `false` | Overlap data transfers with computation |

The `openmp-opt-shared-limit` knob is particularly relevant for the SPMD transformation: it caps the total amount of shared memory allocated for guarded output promotion. If the serial sections between parallel regions produce too many live-out values, the SPMD transformation may be abandoned when the shared memory budget is exceeded.

## Diagnostic Strings

The OpenMP subsystem emits two diagnostics during SPMD transformation:

| Code | Severity | Message |
|---|---|---|
| OMP120 | Remark | `"Transformed generic-mode kernel to SPMD-mode."` |
| OMP121 | Warning | `"Value has potential side effects preventing SPMD-mode execution. Add [[omp::assume(\"ompx_spmd_amenable\")]] to the called function to override"` |

OMP120 is emitted by `sub_26968A0` on successful Generic-to-SPMD conversion. OMP121 is emitted for each call instruction that references a function not in the SPMD-amenable set, explaining why the transformation failed and providing the user with the override attribute.

## Pipeline Integration

The OpenMP passes are registered in the pipeline under three names:

| Pipeline ID | Pass Name | Level | Description |
|---|---|---|---|
| 75 | `openmp-opt` | Module | Pre-link OpenMP optimization |
| 76 | `openmp-opt-postlink` | Module | Post-link OpenMP optimization |
| 154 | `openmp-opt-cgscc` | CGSCC | Call-graph-level OpenMP optimization |

The runtime declaration table (`sub_312CF50`) is invoked lazily from any of these passes when they need to emit a runtime call. The SPMD transformation is part of the module-level `openmp-opt` pass.

## Function Map

| Address | Identity |
|---|---|
| `0x312CF50` | `sub_312CF50` -- OpenMP runtime declaration factory (194-case switch) |
| `0x3122A50` | `sub_3122A50` -- `registerRuntimeFunction(context, index, funcDecl)` |
| `0x26968A0` | `sub_26968A0` -- Generic-to-SPMD transformation (61 KB) |
| `0xBCF480` | `sub_BCF480` -- `FunctionType::get(retTy, paramTys, count, isVarArg)` |
| `0xBA8CB0` | `sub_BA8CB0` -- `Module::getNamedValue(name)` |
| `0xB2C660` | `sub_B2C660` -- `Function::Create(funcTy, linkage, name, module)` |
| `0xB994D0` | `sub_B994D0` -- `addAttribute(26, value)` -- set function attribute |
| `0xB91C10` | `sub_B91C10` -- `hasAttribute(26)` -- check function attribute |
| `0xB9C770` | `sub_B9C770` -- Attribute construction (varargs attribute) |
| `0xB8C960` | `sub_B8C960` -- Attribute kind construction |
| `0xB2BE50` | `sub_B2BE50` -- `Function::getContext()` |

## Cross-References

- [Generic-to-SPMD Transformation](./spmd-transform.md) -- the primary consumer of the runtime table, performing mode conversion using entries 6, 155, 156, 187, 188
- [Pipeline & Ordering](../llvm/pipeline.md) -- where `openmp-opt` / `openmp-opt-cgscc` sit in the pass pipeline
- [CLI Flags](../config/cli-flags.md) -- compiler flags that control OpenMP code generation
- [LLVM Knobs](../config/knobs.md) -- the `openmp-opt-*` knobs listed above
- [Kernel Metadata](../pipeline/ir-generation.md) -- how `KernelEnvironmentTy` and execution mode are set during IR generation
