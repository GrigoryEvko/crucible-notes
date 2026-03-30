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

## Call Generation Infrastructure

When any codegen pass needs a runtime function, it calls `sub_312CF50(omp_context + 400, existing_value, case_index)`. The `omp_context` object (typically at `a2+208` in the pass state) contains both the type cache (+2600..+3072) and the runtime function array. If `Module::getNamedValue` finds the symbol already declared, it is returned immediately; otherwise a new declaration is created and registered.

Once a declaration is obtained, `sub_921880` (create runtime library call instruction) builds the `CallInst` node with the argument list from current SSA values, attaches debug/source location metadata, and inserts it at the specified basic block position.

### Primary Consumers

| Pass | Address | Size | Runtime Entries Used |
|---|---|---|---|
| Generic-to-SPMD transform | `sub_26968A0` | 61 KB | 6 (thread ID), 180 (alloc_shared), 181 (free_shared), 187 (barrier_simple_spmd) |
| State machine generation | `sub_2678420` | 41 KB | 155 (target_init), 156 (target_deinit), 171 (kernel_parallel), 172 (kernel_end_parallel), 188 (barrier_simple_generic) |
| Parallel region outliner | `sub_313D1B0` | 47 KB | 7 (fork_call), 158 (parallel_51) |
| Parallel region merging | `sub_2680940` | 52 KB | 180 (alloc_shared), 181 (free_shared), 187 (barrier_simple_spmd) |
| Attributor OpenMP driver | `sub_269F530` | 63 KB | All -- identifies/folds known runtime calls by index |

## Complete Runtime Function Table

All 194 entries, organized by functional category. The "Index" column is the `switch` case in `sub_312CF50` and the slot in the context's runtime function array. Signatures use LLVM IR type syntax. The "Call Generation" column describes how and when cicc emits each call.

### Standard OpenMP Runtime (0--13)

| Index | Function | Signature | Purpose | Call Generation |
|---|---|---|---|---|
| 0 | `__kmpc_barrier` | `void(ident_t*, i32)` | Explicit barrier | Emitted for `#pragma omp barrier`. On GPU compiles to `__syncthreads()`. OpenMPOpt may replace with index 187 (SPMD barrier) |
| 1 | `__kmpc_cancel` | `i32(ident_t*, i32, i32)` | Cancel construct | Third param: cancel kind (1=parallel, 2=sections, 3=for, 4=taskgroup). Returns nonzero if cancellation pending |
| 2 | `__kmpc_cancel_barrier` | `void(ident_t*, i32)` | Implicit barrier + cancel check | Generated at end of worksharing constructs when cancel is possible |
| 3 | `__kmpc_error` | `void(ident_t*, i32, i8*)` | Runtime error | Second param: severity (1=warning, 2=fatal). Third: message string pointer |
| 4 | `__kmpc_flush` | `void(ident_t*)` | Memory fence | `#pragma omp flush`. On GPU: `__threadfence()` or scope-specific fence |
| 5 | `__kmpc_global_thread_num` | `i32(ident_t*)` | Get global thread ID | On GPU: blockIdx*blockDim+threadIdx. Emitted at start of every region needing a thread identifier |
| 6 | `__kmpc_get_hardware_thread_id_in_block` | `i32()` | threadIdx.x equivalent | Direct PTX `%tid.x` wrapper. Used by SPMD transform (`sub_26968A0`) to build `tid==0` guards. Lookup: `sub_312CF50(..., 6)` |
| 7 | `__kmpc_fork_call` | `void(ident_t*, i32, kmpc_micro, ...)` | Fork parallel region (varargs) | Second param: shared variable count. Third: outlined microtask pointer. Remaining: shared variables. On GPU Generic mode triggers worker state machine dispatch. Attribute #26 applied post-create |
| 8 | `__kmpc_fork_call_if` | `void(ident_t*, i32, i32, i8*, i32)` | Conditional fork | Third param: `if`-clause condition. If false, region executes serially |
| 9 | `__kmpc_omp_taskwait` | `void(ident_t*, i32)` | Taskwait | `#pragma omp taskwait` |
| 10 | `__kmpc_omp_taskyield` | `i32(ident_t*, i32, i32)` | Task yield point | Third param: end-of-task flag |
| 11 | `__kmpc_push_num_threads` | `void(ident_t*, i32, i32)` | Set thread count | `num_threads(N)` clause. Pushes count for next parallel region |
| 12 | `__kmpc_push_proc_bind` | `void(ident_t*, i32, i32)` | Set affinity | `proc_bind(spread/close/master)`. Third param encodes binding policy |
| 13 | `__kmpc_omp_reg_task_with_affinity` | `i32(ident_t*, i32, i8*, i32, i8*)` | Register task with affinity info | OMP 5.0 affinity clause |

Index 7 (`__kmpc_fork_call`) and index 118 (`__kmpc_fork_teams`) are the only two varargs entries. Both receive special post-processing: `sub_B994D0` sets function attribute #26 (likely the `convergent` attribute or a varargs-related marker), checked via `sub_B91C10`. This prevents the optimizer from incorrectly splitting, duplicating, or removing these calls.

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

These are the user-facing OpenMP API functions. On GPU, most return compile-time constants or trivial register reads. The Attributor-based OpenMP driver (`sub_269F530`) can fold many of these to constants when the execution mode and team configuration are statically known -- for example, `omp_get_num_threads` folds to the `blockDim.x` launch parameter.

### Begin/End (53--54)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 53 | `__kmpc_begin` | `void(ident_t*, i32)` | Library initialization (rarely used on GPU) |
| 54 | `__kmpc_end` | `void(ident_t*)` | Library shutdown |

### Master/Masked Constructs (46--49)

| Index | Function | Signature | Purpose | Call Generation |
|---|---|---|---|---|
| 46 | `__kmpc_master` | `i32(ident_t*, i32)` | Enter master region | Returns 1 for master thread (thread 0), 0 for all others. IRGen wraps user code in `if(__kmpc_master(..)) {...}` |
| 47 | `__kmpc_end_master` | `void(ident_t*, i32)` | Exit master region | Called at end of master block |
| 48 | `__kmpc_masked` | `i32(ident_t*, i32, i32)` | Enter masked region (OMP 5.1) | Third param is the filter ID (which specific thread executes). Replaces `master` in OMP 5.1 |
| 49 | `__kmpc_end_masked` | `void(ident_t*, i32)` | Exit masked region | Called at end of masked block |

### Critical Sections (50--52)

| Index | Function | Signature | Purpose | Call Generation |
|---|---|---|---|---|
| 50 | `__kmpc_critical` | `void(ident_t*, i32, kmp_critical*)` | Enter critical section | On GPU: atomic spin-lock acquire on the 32-byte lock variable |
| 51 | `__kmpc_critical_with_hint` | `void(ident_t*, i32, i32, kmp_critical*)` | Enter with lock hint | Hint encodes contention strategy (uncontended, contended, speculative, non-speculative) |
| 52 | `__kmpc_end_critical` | `void(ident_t*, i32, kmp_critical*)` | Exit critical section | Atomic release on lock variable |

On GPU, critical sections use atomic operations on global memory. The `kmp_critical_name` type is `[8 x i32]` (32 bytes), used as an atomic lock variable. The `_with_hint` variant accepts a contention hint that the GPU runtime maps to different atomic strategies.

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

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 98 | `__kmpc_omp_task_alloc` | `i8*(ident_t*, i32, i32, i64, i64, kmp_routine_entry_t)` | Allocate task descriptor (6 params). Returns `kmp_task_t*`. Params: flags, sizeof_task, sizeof_shareds, task_entry |
| 99 | `__kmpc_omp_task` | `i32(ident_t*, i32, i8*)` | Submit allocated task for execution. Third param is the `kmp_task_t*` from task_alloc |
| 100 | `__kmpc_end_taskgroup` | `void(ident_t*, i32)` | End `#pragma omp taskgroup` |
| 101 | `__kmpc_taskgroup` | `void(ident_t*, i32)` | Begin taskgroup |
| 102 | `__kmpc_omp_task_begin_if0` | `void(ident_t*, i32, i8*)` | Begin immediate task (when `if` clause evaluates to false) |
| 103 | `__kmpc_omp_task_complete_if0` | `void(ident_t*, i32, i8*)` | Complete immediate task |
| 104 | `__kmpc_omp_task_with_deps` | `i32(ident_t*, i32, i8*, i32, i8*, i32, i8*)` | Task with dependency list (7 params). Params: task, ndeps, dep_list, ndeps_noalias, noalias_list |
| 105 | `__kmpc_taskloop` | `void(ident_t*, i32, i8*, i32, i64*, i64*, i64, i32, i32, i64, i8*)` | `#pragma omp taskloop` (11 params). Params: task, if_val, lb_p, ub_p, st, nogroup, sched, grainsize, task_dup |
| 106 | `__kmpc_taskloop_5` | `void(ident_t*, i32, i8*, i32, i64*, i64*, i64, i32, i32, i64, i8*, i32)` | OMP 5.1 taskloop (12 params). Extra param: modifier |
| 107 | `__kmpc_omp_target_task_alloc` | `i8*(ident_t*, i32, i32, i64, i64, kmp_routine_entry_t, i64)` | Target-offload task allocation (7 params). Extra i64: device_id |
| 108 | `__kmpc_taskred_modifier_init` | `i8*(ident_t*, i32, i32, i32, i8*)` | Init task reduction with modifier (5 params). Params: is_ws, num, data |
| 109 | `__kmpc_taskred_init` | `i8*(i32, i32, i8*)` | Init task reduction (basic) |
| 110 | `__kmpc_task_reduction_modifier_fini` | `void(ident_t*, i32, i32)` | Finalize task reduction |
| 111 | `__kmpc_task_reduction_get_th_data` | `i8*(i32, i8*, i8*)` | Get thread-local reduction data |
| 112 | `__kmpc_task_reduction_init` | `i8*(i32, i32, i8*)` | Init task reduction (alternate path) |
| 113 | `__kmpc_task_reduction_modifier_init` | `i8*(i8*, i32, i32, i32, i8*)` | Init with full modifier (5 params) |
| 114 | `__kmpc_proxy_task_completed_ooo` | `void(i8*)` | Out-of-order proxy task completion. Used for detached tasks |
| 115 | `__kmpc_omp_wait_deps` | `void(ident_t*, i32, i32, i8*, i32, i8*)` | Wait on task dependencies (6 params) |
| 116 | `__kmpc_omp_taskwait_deps_51` | `void(ident_t*, i32, i32, i8*, i32, i8*, i32)` | OMP 5.1 dependency wait (7 params). Extra param: nowait modifier |

Index 106 (`__kmpc_taskloop_5`) and index 116 (`__kmpc_omp_taskwait_deps_51`) are OMP 5.1 additions with an extra modifier parameter compared to their predecessors.

### Teams and Cancellation (117--121)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 117 | `__kmpc_cancellationpoint` | `i32(ident_t*, i32, i32)` | Cancellation point check |
| 118 | `__kmpc_fork_teams` | `void(ident_t*, i32, kmpc_micro, ...)` | Fork teams region (varargs) |
| 119 | `__kmpc_push_num_teams` | `void(ident_t*, i32, i32, i32)` | Set team count |
| 120 | `__kmpc_push_num_teams_51` | `void(ident_t*, i32, i32, i32, i32)` | Set team count (OMP 5.1, 5 params) |
| 121 | `__kmpc_set_thread_limit` | `void(ident_t*, i32, i32)` | Set per-team thread limit |

### Copyprivate and Threadprivate (122--124)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 122 | `__kmpc_copyprivate` | `void(ident_t*, i32, i64, i8*, kmp_copy_func, i32)` | `#pragma omp copyprivate`. Broadcasts private data from single thread to all others. 6 params |
| 123 | `__kmpc_threadprivate_cached` | `i8*(ident_t*, i32, i8*, i64, i8***)` | Get/allocate threadprivate variable data. 5 params |
| 124 | `__kmpc_threadprivate_register` | `void(ident_t*, i8*, kmpc_ctor, void*, void*)` | Register threadprivate with ctor, copy-ctor, dtor callbacks |

### Doacross Synchronization (125--128)

Cross-iteration dependencies for `#pragma omp ordered depend(source/sink)`.

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 125 | `__kmpc_doacross_init` | `void(ident_t*, i32, i32, i8*)` | Init doacross tracking. Params: num_dims, dims_info |
| 126 | `__kmpc_doacross_post` | `void(ident_t*, i32, i64*)` | Post (source): signal iteration completion |
| 127 | `__kmpc_doacross_wait` | `void(ident_t*, i32, i64*)` | Wait (sink): wait for iteration to complete |
| 128 | `__kmpc_doacross_fini` | `void(ident_t*, i32)` | Finalize doacross tracking |

### Memory Allocators (129--136)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 129 | `__kmpc_alloc` | `i8*(i32, i64, i8*)` | OpenMP allocator alloc. Params: gtid, size, allocator |
| 130 | `__kmpc_aligned_alloc` | `i8*(i32, i64, i64, i8*)` | Aligned allocation. Params: gtid, align, size, allocator |
| 131 | `__kmpc_free` | `void(i32, i8*, i8*)` | Free allocated memory. Params: gtid, ptr, allocator |
| 132 | `__tgt_interop_init` | `void(ident_t*, i32, i8**, i32, i32, i32, i8*, i32)` | OMP 5.1 foreign runtime interop init (8 params) |
| 133 | `__tgt_interop_destroy` | `void(ident_t*, i32, i8**, i32, i32, i32, i8*)` | Destroy interop object (7 params) |
| 134 | `__tgt_interop_use` | `void(ident_t*, i32, i8**, i32, i32, i32, i8*)` | Use interop object (7 params) |
| 135 | `__kmpc_init_allocator` | `i8*(i32, i32, i8*, i8*)` | Init OpenMP allocator. Params: gtid, memspace, num_traits, traits |
| 136 | `__kmpc_destroy_allocator` | `void(i32, i8*)` | Destroy allocator |

### Target Offloading (137--153)

18 entries implementing the host-side target offloading protocol. These are primarily used when cicc compiles host code that launches GPU kernels, not within device code itself:

| Index | Function | Signature | Params | Purpose |
|---|---|---|---|---|
| 137 | `__kmpc_push_target_tripcount_mapper` | `void(ident_t*, i64, i64)` | 3 | Set iteration count for target region. Params: device_id, trip_count |
| 138 | `__tgt_target_mapper` | `i32(ident_t*, i64, i8*, i32, i8**, i8**, i64*, i64*, i8**, i8**)` | 10 | Launch target region with data mapping |
| 139 | `__tgt_target_nowait_mapper` | (14 params) | 14 | Async target launch. Adds depobj count/list, noalias count/list |
| 140 | `__tgt_target_teams_mapper` | (12 params) | 12 | Target teams launch. Adds num_teams, thread_limit, mappers |
| 141 | `__tgt_target_teams_nowait_mapper` | (16 params) | 16 | Async target teams. Most complex host-side offload call |
| 142 | `__tgt_target_kernel` | `i32(ident_t*, i64, i32, i32, i8*, __tgt_kernel_args*)` | 6 | New-style kernel launch (takes `__tgt_kernel_arguments*`) |
| 143 | `__tgt_target_kernel_nowait` | (10 params) | 10 | Async new-style launch. Adds depobj info |
| 144 | `__tgt_target_data_begin_mapper` | (9 params) | 9 | Map data to device |
| 145 | `__tgt_target_data_begin_nowait_mapper` | (13 params) | 13 | Async map-to |
| 146 | `__tgt_target_data_begin_mapper_issue` | (10 params) | 10 | Split-phase issue for async map-to |
| 147 | `__tgt_target_data_begin_mapper_wait` | `void(i64, __tgt_async_info*)` | 2 | Split-phase wait for async map-to |
| 148 | `__tgt_target_data_end_mapper` | (9 params) | 9 | Map data from device |
| 149 | `__tgt_target_data_end_nowait_mapper` | (13 params) | 13 | Async map-from |
| 150 | `__tgt_target_data_update_mapper` | (9 params) | 9 | Data update (host-to-device or device-to-host) |
| 151 | `__tgt_target_data_update_nowait_mapper` | (13 params) | 13 | Async data update |
| 152 | `__tgt_mapper_num_components` | `i64(i8*)` | 1 | Query user-defined mapper component count |
| 153 | `__tgt_push_mapper_component` | `void(i8*, i8*, i8*, i64, i64, i8*)` | 6 | Register mapper component. Params: handle, base, begin, size, type, name |

### Task Completion Event (154)

| Index | Function | Signature | Purpose |
|---|---|---|---|
| 154 | `__kmpc_task_allow_completion_event` | `i8*(ident_t*, i32, i8*)` | Allow completion event for detached tasks (OMP 5.0) |

### GPU Kernel Lifecycle (155--158)

These are the most important entries for device-side GPU OpenMP code.

| Index | Function | Signature | Purpose | Call Generation |
|---|---|---|---|---|
| 155 | `__kmpc_target_init` | `i32(KernelEnvironmentTy*, KernelLaunchEnvironmentTy*)` | Kernel entry | First call in every GPU OpenMP kernel. State machine generator (`sub_2678420`) emits this at entry. `KernelEnvironmentTy` carries `ConfigurationEnvironmentTy` (first byte = execution mode) |
| 156 | `__kmpc_target_deinit` | `void()` | Kernel exit | Last call in every GPU OpenMP kernel. Emitted by state machine generator |
| 157 | `__kmpc_kernel_prepare_parallel` | `void(i8*)` | Generic: signal workers | Master thread writes outlined function pointer to shared memory, then signals workers to execute it. Replaced by `__kmpc_parallel_51` after SPMD conversion |
| 158 | `__kmpc_parallel_51` | `void(ident_t*, i32, i32, i32, i32, i8*, i8*, i8**, i64)` | OMP 5.1 GPU parallel dispatch | 9 params: if_expr, num_threads, proc_bind, fn, wrapper_fn, shared_args, num_shared_args. Used by parallel region outliner (`sub_313D1B0`) on SPMD kernels. Replaces `fork_call` for GPU |

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

## Execution Mode Call Patterns

The execution mode fundamentally determines which runtime functions appear in generated IR. These pseudocode patterns show the exact call sequences emitted by the state machine generator (`sub_2678420`) and the SPMD transformation (`sub_26968A0`).

### Generic Mode Kernel (mode byte = 1)

```
entry:
    ret = __kmpc_target_init(KernelEnv, LaunchEnv)   // [155]
    if (ret == -1) goto worker_loop                   // worker threads
    // master thread: user code
    __kmpc_kernel_prepare_parallel(outlined_fn_ptr)   // [157]
    __kmpc_barrier_simple_generic(loc, gtid)          // [188]
    // ... more serial + parallel sections ...
    __kmpc_target_deinit()                            // [156]
worker_loop:
    while (true) {
        __kmpc_barrier_simple_generic(loc, gtid)      // [188]
        if (__kmpc_kernel_parallel(&fn))               // [171]
            fn(args);
            __kmpc_kernel_end_parallel()               // [172]
        __kmpc_barrier_simple_generic(loc, gtid)      // [188]
    }
```

### SPMD Mode Kernel -- Simple (mode byte = 2, single parallel region)

After successful Generic-to-SPMD transformation:

```
entry:
    __kmpc_target_init(KernelEnv, LaunchEnv)          // [155], returns 0 for all
    tid = __kmpc_get_hardware_thread_id_in_block()    // [6]
    is_main = (tid == 0)
    br is_main, user_code, exit.threads
user_code:
    // all threads: user code
    __kmpc_parallel_51(loc, gtid, ...)                // [158], for nested
    __kmpc_barrier_simple_spmd(loc, gtid)             // [187]
exit.threads:
    __kmpc_target_deinit()                            // [156]
```

### SPMD Mode Kernel -- Complex (guarded regions, multiple parallel regions)

```
entry:
    __kmpc_target_init(...)                           // [155]
region.check.tid:
    tid = __kmpc_get_hardware_thread_id_in_block()    // [6]
    cmp = icmp eq tid, 0
    br cmp, region.guarded, region.barrier
region.guarded:
    ... master-only serial code ...
    shared_ptr = __kmpc_alloc_shared(sizeof(result))  // [180]
    store result -> shared_ptr
region.guarded.end:
    br region.barrier
region.barrier:
    __kmpc_barrier_simple_spmd(loc, gtid)             // [187]
    result = load from shared_ptr
    __kmpc_barrier_simple_spmd(loc, gtid)             // [187], post-load
    __kmpc_free_shared(shared_ptr, size)              // [181]
    ... all threads continue with result ...
exit:
    __kmpc_target_deinit()                            // [156]
```

The SPMD transformation eliminates the worker state machine entirely. Workers no longer idle-spin in a polling loop; they participate in computation from the kernel's first instruction. Serial sections between parallel regions are wrapped in `tid==0` guards with shared-memory output promotion and barriers.

## SPMD-Amenable Function Table

The SPMD transformation maintains a hash set of functions that are safe to call from all threads simultaneously, located at `*(omp_context + 208) + 34952` (base pointer), `+34968` (capacity).

| Property | Value |
|---|---|
| Hash function | Open-addressing with linear probing |
| Slot computation | `((addr >> 9) ^ (addr >> 4)) & (capacity - 1)` |
| Sentinel | `-4096` (empty slot marker) |
| Contents | Functions pre-analyzed or annotated with `[[omp::assume("ompx_spmd_amenable")]]` |

When a call instruction references a function not in this set, the SPMD transformation fails for that kernel and emits OMP121: `"Value has potential side effects preventing SPMD-mode execution. Add [[omp::assume(\"ompx_spmd_amenable\")]] to the called function to override"`.

## Functional Category Summary

| Category | Count | Indices |
|---|---|---|
| Thread hierarchy and hardware query | 20 | 0--6, 14--16, 17--45 |
| Work sharing / loop scheduling | 48 | 61--95, 159--170 |
| Tasking | 19 | 98--116, 154 |
| Synchronization | 12 | 0, 2, 4, 50--52, 59--60, 96--97, 187--188, 190 |
| Target offloading / data mapping | 18 | 137--153 |
| GPU execution mode | 10 | 155--158, 171--174, 185--186 |
| Warp primitives | 4 | 175, 179, 189--190 |
| NVIDIA device reduction | 3 | 176--178 |
| Shared memory management | 5 | 180--184 |
| Memory allocators | 8 | 129--136 |
| Copyprivate / threadprivate | 3 | 122--124 |
| Doacross synchronization | 4 | 125--128 |
| Teams / cancellation | 5 | 117--121 |
| Master / masked | 4 | 46--49 |
| Reduction (standard) | 4 | 55--58 |
| Begin / end | 2 | 53--54 |
| Profiling | 2 | 191--192 |
| Sentinel | 1 | 193 |
| **Total** | **194** | |

## Function Map

| Address | Identity |
|---|---|
| `0x312CF50` | `sub_312CF50` -- OpenMP runtime declaration factory (194-case switch) |
| `0x3122A50` | `sub_3122A50` -- `registerRuntimeFunction(context, index, funcDecl)` |
| `0x2686D90` | `sub_2686D90` -- OpenMP runtime declaration table (215 KB, outer wrapper) |
| `0x26968A0` | `sub_26968A0` -- Generic-to-SPMD transformation (61 KB) |
| `0x2680940` | `sub_2680940` -- Parallel region merging (52 KB) |
| `0x2678420` | `sub_2678420` -- State machine generation for Generic mode (41 KB) |
| `0x269F530` | `sub_269F530` -- Attributor-based OpenMP optimization driver (63 KB) |
| `0x313D1B0` | `sub_313D1B0` -- Parallel region outliner (47 KB) |
| `0xBCF480` | `sub_BCF480` -- `FunctionType::get(retTy, paramTys, count, isVarArg)` |
| `0xBA8CB0` | `sub_BA8CB0` -- `Module::getNamedValue(name)` |
| `0xB2C660` | `sub_B2C660` -- `Function::Create(funcTy, linkage, name, module)` |
| `0xB994D0` | `sub_B994D0` -- `addAttribute(26, value)` -- set function attribute |
| `0xB91C10` | `sub_B91C10` -- `hasAttribute(26)` -- check function attribute |
| `0xB9C770` | `sub_B9C770` -- Attribute construction (varargs attribute) |
| `0xB8C960` | `sub_B8C960` -- Attribute kind construction |
| `0xB2BE50` | `sub_B2BE50` -- `Function::getContext()` |
| `0x921880` | `sub_921880` -- Create runtime library call instruction |
| `0x5FB5C0` | `sub_5FB5C0` -- OpenMP variant processing (`%s$$OMP_VARIANT%06d`) |

## OpenMP Variant Processing

cicc also supports OpenMP variant dispatch during EDG front-end processing. The function `sub_5FB5C0` at `0x5FB5C0` handles mangled names with the format `%s$$OMP_VARIANT%06d`, which the front-end generates for `#pragma omp declare variant` constructs. This is separate from the runtime declaration table and operates at the source-level AST rather than at the LLVM IR level.

## Cross-References

- [Generic-to-SPMD Transformation](./spmd-transform.md) -- the primary consumer of the runtime table, performing mode conversion using entries 6, 155, 156, 180, 181, 187, 188
- [Pipeline & Ordering](../llvm/pipeline.md) -- where `openmp-opt` (ID 75), `openmp-opt-postlink` (ID 76), and `openmp-opt-cgscc` (ID 154) sit in the pass pipeline
- [CLI Flags](../config/cli-flags.md) -- compiler flags that control OpenMP code generation
- [LLVM Knobs](../config/knobs.md) -- the `openmp-opt-*` knobs listed above
- [Kernel Metadata](../pipeline/ir-generation.md) -- how `KernelEnvironmentTy` and execution mode are set during IR generation
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- the open-addressing hash table pattern used by the SPMD-amenable function set
- [GPU Execution Model](../gpu-execution-model.md) -- broader context on SPMD vs Generic execution
