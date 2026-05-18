# `__nv_*` Builtin Intrinsic Names

The string table of cudafe++ contains 110 identifiers that begin with `__nv_` but are **not** CUDA attributes. They look like attributes only because a naive scan of the binary collected every `__nv_*` symbol into one bucket — the extraction tooling stored them under `unknown_nv_attributes` in `cuda_attributes.json` for triage. In reality these names are intrinsic function names, template helpers from synthesized lambda preheaders, and one-off compiler internal hooks. cudafe++ does not own their semantics; it recognises the names during parsing, threads them through name lookup, and passes them on to cicc, which lowers each one to a specific PTX instruction or runtime hook. This page catalogues the 110 names, groups them by role, and points at the binary evidence for each group.

The dispatch path for these intrinsics differs from the attribute path. CUDA attributes are owned by the kind-byte switch (`attribute_display_name` at `sub_40A310`) and have an `apply_*_attr` handler each (`sub_413240` dispatcher). Intrinsics, by contrast, are matched as identifiers inside the expression parser (`sub_537BF0`, the `adjust_sync_atomic_builtin` transformation) or are referenced as members of synthesized templates (`emit_lambda_preamble` at `0x6BCC20`). The only thing they share with attributes is the `__nv_` prefix, which is why they ended up commingled.

## Why They Live Under `unknown_nv_attributes`

`cuda_attributes.json` is produced by a sweep that:
1. enumerates every `.rodata` string starting with `__nv_`,
2. cross-checks each against the kind-byte switch and the apply-handler table,
3. drops anything with a matched kind code into `attributes[...]`,
4. dumps the residue into the `unknown_nv_attributes` array as a TODO.

A separate inspection of the cross-references resolves each residue entry into one of three categories:

| Bucket | Count | Where the binary uses them |
|---|---|---|
| Intrinsic function names (atomic / cluster / cvta / memcpy_async / fence) | 90 | Identifier table → `adjust_sync_atomic_builtin` and similar lowering paths |
| Lambda-machinery template names (preheader text) | 14 | Verbatim chunks in `.rodata`, concatenated by the lambda preamble emitter |
| One-off compiler hooks (`__nv_init_managed_rt_with_module`, `__nv_tex_surf_handler`, `__nv_static_`, `__nv_associate_access_property_impl`, `__nv_isClusterShared_impl`, `__nv_p2r`, `__nv_r2p`) | 6 | Special-cased identifier matchers and code-emitter paths |

> ⚡ **QUIRK — "Unknown attribute" is a dumper artifact, not a real ambiguity**
> Reading `unknown_nv_attributes` literally would imply 110 undocumented CUDA attributes. Zero of them are. cudafe++ never invokes `apply_one_attribute` for any of these names, never assigns them a kind byte, and never writes flags into the entity node on their behalf. Confusing them with real attributes (`__nv_pure__`, `__nv_register_params__`, `__grid_constant__`) leads downstream consumers astray — for example, a tool that tried to surface `__nv_atomic_add` as an attribute on a function declaration would build a contract that the compiler does not honour. Treat the array as a residue list, not a discovery list.

Confidence: **HIGH** (string-anchored at the addresses listed below; no kind-byte case in the `attribute_display_name` switch).

---

## Group 1 — Scoped Typed Atomics (`__nv_atomic_*`, 70 names)

The largest family. cudafe++ rewrites every `__sync_fetch_and_*` and every `__atomic_*` GCC builtin it encounters in device code into one of these names. The replacement is performed inside `adjust_sync_atomic_builtin` (`sub_537BF0`, 1,108 lines — the largest single function in the expression parser) and is documented from a different angle in the [Expression Parser page](../edg/expression-parser.md#__sync-atomic-remapping). The rewriting front-loads instruction selection: the size (`_1` / `_2` / `_4` / `_8` / `_16` bytes) and the signedness/float discriminator (`_s` / `_u` / `_f`) are baked into the name, so cicc can pick a PTX `atom.*` instruction by string match without re-running type analysis.

### Sub-family A — Load / Store

| Intrinsic | Width | Arity | Memory effect | SM tier |
|---|---|---|---|---|
| `__nv_atomic_load` | generic | 4 (ptr, order, scope, addr_space) | atomic read | sm_60+ |
| `__nv_atomic_load_1` | 1 B | 3 (ptr, order, scope) | atomic read | sm_60+ |
| `__nv_atomic_load_2` | 2 B | 3 | atomic read | sm_60+ |
| `__nv_atomic_load_4` | 4 B | 3 | atomic read | sm_60+ |
| `__nv_atomic_load_8` | 8 B | 3 | atomic read | sm_60+ |
| `__nv_atomic_load_16` | 16 B | 3 | atomic read | sm_70+ (128-bit atomic load/store requires sm_70) |
| `__nv_atomic_load_n` | N (template) | 3 | atomic read | depends on N |
| `__nv_atomic_store` | generic | 4 | atomic write | sm_60+ |
| `__nv_atomic_store_1` | 1 B | 3 | atomic write | sm_60+ |
| `__nv_atomic_store_2` | 2 B | 3 | atomic write | sm_60+ |
| `__nv_atomic_store_4` | 4 B | 3 | atomic write | sm_60+ |
| `__nv_atomic_store_8` | 8 B | 3 | atomic write | sm_60+ |
| `__nv_atomic_store_16` | 16 B | 3 | atomic write | sm_70+ |
| `__nv_atomic_store_n` | N | 3 | atomic write | depends on N |

The `_n` variants are the un-specialised templates. They survive the rewrite when the operand width is not yet known (template-dependent context). cicc later resolves them once the surrounding template is instantiated. Confidence: **HIGH** for `_1/_2/_4/_8`, **HIGH** for the sm_70 128-bit gate (`arch-gating.md` carries the diagnostic string), **MED** for `_n` (inferred from the pattern but no diagnostic confirms the lowering rule).

### Sub-family B — Exchange / Compare-Exchange

| Intrinsic | Width | Operation |
|---|---|---|
| `__nv_atomic_exchange` | generic | atomic swap |
| `__nv_atomic_exchange_4` / `_8` / `_16` | 4 / 8 / 16 B | atomic swap |
| `__nv_atomic_exchange_n` | template | atomic swap |
| `__nv_atomic_compare_exchange` | generic | CAS |
| `__nv_atomic_compare_exchange_2` / `_4` / `_8` / `_16` | 2 / 4 / 8 / 16 B | CAS |
| `__nv_atomic_compare_exchange_n` | template | CAS |

128-bit exchange/CAS (`_16`) was added with the sm_90 ISA; the `arch-gating.md` summary lists it under "sm_90 / sm_90a". 16-bit CAS (`__nv_atomic_compare_exchange_2`) is the original sm_70 introduction. Confidence: **HIGH** for sm_70 16-bit CAS, **HIGH** for sm_90 128-bit gate (both have direct diagnostic strings), **MED** for the generic-template variants.

### Sub-family C — Fetch-OP (typed)

This is where the type suffix matters: an integer `add` and a float `add` lower to different PTX instructions, so the rewrite must propagate the operand type into the name. The convention is `__nv_atomic_fetch_<op>_<width>_<sign>` where `<sign>` is `s` (signed int), `u` (unsigned int), or `f` (floating point).

| Op | 32-bit signed | 32-bit unsigned | 32-bit float | 64-bit signed | 64-bit unsigned | 64-bit float |
|---|---|---|---|---|---|---|
| `add` | `__nv_atomic_fetch_add_4_s` | `_4_u` | `_4_f` | `_8_s` | `_8_u` | `_8_f` |
| `sub` | `__nv_atomic_fetch_sub_4_s` | `_4_u` | `_4_f` | `_8_s` | `_8_u` | `_8_f` |
| `min` | `__nv_atomic_fetch_min_4_s` | `_4_u` | `_4_f` | `_8_s` | `_8_u` | `_8_f` |
| `max` | `__nv_atomic_fetch_max_4_s` | `_4_u` | `_4_f` | `_8_s` | `_8_u` | `_8_f` |

The generic `__nv_atomic_fetch_<op>` (without suffix) and the bitwise variants (which do not need a sign axis) cover the rest:

| Op | Generic | 4-byte | 8-byte |
|---|---|---|---|
| `and` | `__nv_atomic_fetch_and` | `__nv_atomic_fetch_and_4` | `__nv_atomic_fetch_and_8` |
| `or` | `__nv_atomic_fetch_or` | `__nv_atomic_fetch_or_4` | `__nv_atomic_fetch_or_8` |
| `xor` | `__nv_atomic_fetch_xor` | `__nv_atomic_fetch_xor_4` | `__nv_atomic_fetch_xor_8` |

The non-prefetched aliases (`__nv_atomic_add`, `__nv_atomic_and`, `__nv_atomic_or`, `__nv_atomic_xor`, `__nv_atomic_min`, `__nv_atomic_max`, `__nv_atomic_sub`) discard the returned previous value. They lower to the same PTX instruction but allow cicc to skip the result write-back.

> ⚡ **QUIRK — Type-suffixed atomics front-load instruction selection**
> The GCC builtin `__sync_fetch_and_add(int*, int)` has no way to express "this is a float add" — the operand type is encoded only in the C++ type system. By the time PTX emission happens in cicc, the C++ type tree is gone. Rather than dragging the type information through the IR, cudafe++ chooses the lowered name at parse time and bakes `_4_f` (or `_8_s`, etc.) directly into the identifier. cicc then matches the literal name when it picks an `atom.add.f32` vs `atom.add.s32` PTX instruction. This is the same trick `libgcc` uses for `__sync_*` size variants, but extended along the signedness/float axis.

Confidence: **HIGH** — the rewrite logic is in `sub_537BF0` and the names are anchored at `0x8a5645` (`__nv_atomic_fetch_add_4_u`) and neighbouring addresses in `.rodata`.

### Sub-family D — Thread Fence

| Intrinsic | Arity | Effect |
|---|---|---|
| `__nv_atomic_thread_fence` | 2 (order, scope) | `membar` / `fence` |

The `scope` argument (`cta`, `gpu`, `sys`, `cluster`) selects which membar variant cicc emits. The `cluster` scope requires sm_90+ — the arch-gating note "cluster scope atomics" sits under sm_90/sm_90a. Confidence: **HIGH**.

### Sub-family E — The Trailing Trailing-Underscore (`__nv_atomic_`)

A bare `__nv_atomic_` (one entry, length 12 bytes, address `0xa8359d`) shows up alongside `__nv_static_`. Both are prefix strings, not complete intrinsic names. They are concatenation roots used by the rewrite engine: `__nv_atomic_` + op + width + sign produces the final name. cudafe++ likely stores these as the static base of a stringbuilder so the rewriter does not duplicate the prefix across the 70 variants. Confidence: **MED** (inferred from length and adjacency; the rewriter source is not visible).

C pseudocode for representative members:

```c
// Generated by the parser after __atomic_fetch_add(ptr, val, memory_order_seq_cst):
//   when ptr : int32_t* (signed)
T __nv_atomic_fetch_add_4_s(volatile int32_t* ptr,
                            int32_t          val,
                            int              memory_order,
                            int              memory_scope);

// when ptr : float*
float __nv_atomic_fetch_add_4_f(volatile float* ptr,
                                float           val,
                                int             memory_order,
                                int             memory_scope);

// 128-bit CAS — only on sm_90+
__int128 __nv_atomic_compare_exchange_16(volatile __int128* ptr,
                                         __int128*          expected,
                                         __int128           desired,
                                         int                success_order,
                                         int                failure_order,
                                         int                memory_scope);
```

---

## Group 2 — Thread Block Cluster Intrinsics (`__nv_cluster*`, 12 names)

Names ending in `_impl` that implement the public `cooperative_groups::__cluster_*` API. All require sm_90+ — outside of `arch-gating.md`'s sm_90 row they have no defined meaning. They live behind `_impl` because the public API headers wrap them in an inline trampoline that adds compute-capability gating.

| Intrinsic | Returns | Effect |
|---|---|---|
| `__nv_clusterDim_impl` | `dim3` | cluster geometry (x, y, z) |
| `__nv_clusterDimIsSpecifed_impl` | `int` | 1 if `__cluster_dims__` was applied to the launching kernel, else 0 |
| `__nv_clusterGridDimInClusters_impl` | `dim3` | grid dimensions measured in clusters, not blocks |
| `__nv_clusterIdx_impl` | `dim3` | this block's cluster index within the grid |
| `__nv_clusterRelativeBlockIdx_impl` | `dim3` | this block's position within its cluster |
| `__nv_clusterRelativeBlockRank_impl` | `unsigned` | linearised cluster rank |
| `__nv_clusterSizeInBlocks_impl` | `unsigned` | total blocks in the cluster |
| `__nv_cluster_barrier_arrive_impl` | `void` | arrive on cluster barrier |
| `__nv_cluster_barrier_arrive_relaxed_impl` | `void` | arrive, relaxed ordering |
| `__nv_cluster_barrier_wait_impl` | `void` | wait on cluster barrier |
| `__nv_cluster_map_shared_rank_impl` | `void*` | map shared-memory pointer into a different rank's address |
| `__nv_cluster_query_shared_rank_impl` | `unsigned` | reverse of map: rank of an inbound shared pointer |

The "specifed" typo in `__nv_clusterDimIsSpecifed_impl` (it should read "specified") is **preserved verbatim in the binary at `0x911c30`** and the surrounding addresses. Renaming would break ABI for any external tool that grepped for the symbol, so the typo is now part of the stable surface. (The header that exposes the public wrapper is free to expose `__cluster_dim_is_specified()`.)

```c
// Equivalent C signatures inferred from .rodata strings + the diagnostic
// "cluster intrinsics require sm_90 or above"
dim3     __nv_clusterDim_impl(void);
dim3     __nv_clusterIdx_impl(void);
unsigned __nv_clusterSizeInBlocks_impl(void);
void*    __nv_cluster_map_shared_rank_impl(const void* p, unsigned rank);
unsigned __nv_cluster_query_shared_rank_impl(const void* p);
void     __nv_cluster_barrier_arrive_impl(void);
void     __nv_cluster_barrier_wait_impl(void);
```

> ⚡ **QUIRK — Cluster intrinsics are sm_90+-only but always parseable**
> cudafe++ does not block compilation when these names appear in code targeting sm_80 or below. Parsing succeeds, name lookup succeeds, and the call enters the IL stream. The arch gate fires later — `arch-gating.md` lists "thread block clusters" under sm_90/sm_90a and the matching diagnostic message refers to the public wrapper, not the `_impl` intrinsic. This means a sm_80 build that imports cooperative_groups but never calls cluster functions will compile cleanly, while a build that calls them gets a diagnostic pointing at the wrapper. The `_impl` symbol stays invisible to user error messages on purpose: it is an implementation detail of the public API, not part of the supported surface.

Confidence: **HIGH** (the sm_90 row of the arch-gating page enumerates exactly this family).

---

## Group 3 — Address-Space Conversion (`__nv_cvta_*`, 8 names)

cicc's PTX backend uses the `cvta` instruction family to convert between the generic 64-bit address space and the four specialised spaces (global, local, shared, constant). cudafe++ exposes each direction as a dedicated intrinsic so that user-space inline assembly does not have to write `asm("cvta.to.global.u64 ...")`.

| Intrinsic | Direction | PTX |
|---|---|---|
| `__nv_cvta_generic_to_global_impl` | generic → global | `cvta.to.global.u64` |
| `__nv_cvta_generic_to_local_impl` | generic → local | `cvta.to.local.u64` |
| `__nv_cvta_generic_to_shared_impl` | generic → shared | `cvta.to.shared.u64` |
| `__nv_cvta_generic_to_constant_impl` | generic → constant | `cvta.to.const.u64` |
| `__nv_cvta_global_to_generic_impl` | global → generic | `cvta.global.u64` |
| `__nv_cvta_local_to_generic_impl` | local → generic | `cvta.local.u64` |
| `__nv_cvta_shared_to_generic_impl` | shared → generic | `cvta.shared.u64` |
| `__nv_cvta_constant_to_generic_impl` | constant → generic | `cvta.const.u64` |

All eight are arity-1 (one `void*` in, one `void*` out) and have no memory effect. They are pure pointer-bit rearrangement and can be CSE'd freely. cicc treats them as `readnone willreturn` LLVM attributes. No SM gate — these have been available since the unified address space landed in sm_20. Confidence: **HIGH** (each name is its own anchored `.rodata` symbol; the PTX mapping is direct).

```c
void* __nv_cvta_generic_to_global_impl(const void* generic_ptr);
void* __nv_cvta_global_to_generic_impl(const void* global_ptr);
// ... 6 more symmetric variants
```

---

## Group 4 — Async DMA Helpers (`__nv_memcpy_async_shared_global_*`, 3 names)

These implement the `cp.async` PTX instruction family (sm_80+) used by `cuda::memcpy_async` and `cuda::pipeline`.

| Intrinsic | Transfer size | PTX |
|---|---|---|
| `__nv_memcpy_async_shared_global_4_impl` | 4 B | `cp.async.ca.shared.global` (cache-all) |
| `__nv_memcpy_async_shared_global_8_impl` | 8 B | `cp.async.ca.shared.global` |
| `__nv_memcpy_async_shared_global_16_impl` | 16 B | `cp.async.cg.shared.global` (cache-global, bypass L1) |

Arity is 2 (destination shared pointer, source global pointer) plus an implicit completion-barrier handle that the surrounding C++ helper threads through. The size is part of the name, again to make instruction selection a name-match in cicc. Confidence: **HIGH** for the name → PTX map, **MED** for the cache-mode split (the 16-byte variant aligns with PTX's documented behaviour but no diagnostic in the binary confirms it).

```c
// Asynchronous DMA from global memory to shared memory, sm_80+.
// The completion is observed through a separate barrier object.
void __nv_memcpy_async_shared_global_16_impl(void*       smem_dst,
                                             const void* gmem_src);
```

---

## Group 5 — One-Off Compiler Hooks (7 names)

Each of these stands alone — different role, different code path, no family. They share the residue list only because of the `__nv_` prefix.

| Name | Role | Caller | Confidence |
|---|---|---|---|
| `__nv_threadfence_cluster_impl` | sm_90 `fence.cluster` PTX wrapper | Cooperative groups `cluster_group::sync()` | HIGH |
| `__nv_isClusterShared_impl` | Predicate: is a generic pointer pointing into another rank's shared space? | `cluster_group::map_shared_rank` precondition | HIGH |
| `__nv_associate_access_property_impl` | Carries an L2 access-property hint (persisting / streaming) into the load instruction | `cuda::access_property` API | MED |
| `__nv_tex_surf_handler` | Demangler hook used when an instance reference to a texture/surface object handle is taken | `binary-layout.md` lists the CUDA-aware demangler at `0x7CABB0` recognising special prefixes; `__nv_tex_surf_handler` is the symbol attached to the resulting opaque handle accessor | MED |
| `__nv_p2r` | "Predicate to register" — reads a 1-bit predicate register into a 32-bit GPR | Inline `setp` → integer materialisation idioms | HIGH (matches PTX `selp` lowering) |
| `__nv_r2p` | "Register to predicate" — the inverse | `__builtin_expect`-style hot-path tests | HIGH |
| `__nv_init_managed_rt_with_module` | Runtime initialiser for `__managed__` variables; takes the fatbin handle | Emitted by the `.int.c` writer alongside `__nv_init_managed_rt` and `__nv_save_fatbinhandle_for_managed_rt` — see [Managed Variables](managed-variables.md) | HIGH |

> ⚡ **QUIRK — `__nv_p2r` and `__nv_r2p` are not in the residue list because they're broken; they're in it because they're tiny**
> Each is a single-instruction lowering (`setp.ne.u32 %p, %r, 0` and `selp.u32 %r, 1, 0, %p`) wrapped as an intrinsic so user code can write `unsigned bit = __nv_p2r(condition);` without inline asm. The dumper sees the name in `.rodata`, fails to find a kind byte for it (because there isn't one), and dumps it into `unknown_nv_attributes`. The behaviour is well-defined and used by the cooperative-groups and warp-vote header files.

### `__nv_static_` — A Prefix, Not a Name

`__nv_static_` is twelve bytes long and ends in an underscore. It is not a callable symbol but a mangling prefix used when cudafe++ emits a synthesised static variable into the host-side `.int.c` output (for example, the `static char __nv_inited_managed_rt = 0;` line shown in the [Managed Variables](managed-variables.md) preheader). The emitter writes the prefix followed by a counter or a user-visible name. Confidence: **MED** (the suffix never appears in `.rodata` because it is built at write time).

---

## Group 6 — Lambda Trait Helpers (`__nv_lambda_*`, `__nv_hdl_*`, `__nv_extended_*`, 14 names)

These are not callable functions at all. They are **template names** that cudafe++ synthesises as C++ source text and emits into the `.int.c` preheader the first time a translation unit uses an extended lambda. The full source body for the preheader lives in `.rodata` as multi-kilobyte string blobs (anchored at `0xa81c88` (1,236 B), `0xa82288` (880 B), `0xa82600` (645 B), `0xa831b0` (498 B), and several smaller chunks) and is assembled by `emit_lambda_preamble` at `0x6BCC20`. The [Host-Device Lambda Wrapper](../lambda/host-device-wrapper.md) and [Preamble Injection](../lambda/preamble-injection.md) pages dissect the assembly; this section only catalogues which names belong to the family.

| Name | Kind | Role |
|---|---|---|
| `__nv_hdl_helper` | template struct | Storage for `fp_caller`, `fp_copier`, `fp_deleter`, `fp_noobject_caller` function-pointer slots |
| `__nv_hdl_helper_trait` | template struct | Trait specialisation per `operator()` signature (const / non-const / noexcept / non-noexcept) |
| `__nv_hdl_helper_trait_outer` | template struct | Capture-args wrapper around `__nv_hdl_helper_trait` |
| `__nv_hdl_wrapper_t` | template struct | The wrapper passed to `__global__`; documented in [host-device-wrapper.md](../lambda/host-device-wrapper.md) |
| `__nv_hdl_create_wrapper_t` | template struct | Factory that the trailing-return `decltype` chain refers to |
| `__nv_hdl_create_wrapper` | static method | The factory entry point — `static auto __nv_hdl_create_wrapper(Lambda&&, CaptureArgs...)` |
| `__nv_hdl_helper` member `fp_caller` / `fp_copier` / `fp_deleter` / `fp_noobject_caller` | static data | One slot per closure operation |
| `__nv_extended_device_lambda_trait_helper` | template struct | Trait that returns `true` for `__nv_dl_wrapper_t<…>`, else `false` |
| `__nv_extended_device_lambda_with_trailing_return_trait_helper` | template struct | Trait specialised on `__nv_dl_wrapper_t<__nv_dl_trailing_return_tag<…>, …>` |
| `__nv_extended_host_device_lambda_trait_helper` | template struct | Trait that returns `true` for `__nv_hdl_wrapper_t<…>` |
| `__nv_lambda_array_wrapper` | template struct | Carries an array capture (handles up to 8D arrays) |
| `__nv_lambda_field_type` | template struct | Strips top-level `const` for field-type resolution |
| `__nv_lambda_trait_remove_const` | template struct | Type trait, no const |
| `__nv_lambda_trait_remove_volatile` | template struct | Type trait, no volatile |
| `__nv_lambda_trait_remove_dl_wrapper` | template struct | Strips the `__nv_dl_wrapper_t<…>` envelope |

The macros `__nv_is_extended_device_lambda_closure_type(X)`, `__nv_is_extended_device_lambda_with_preserved_return_type(X)`, and `__nv_is_extended_host_device_lambda_closure_type(X)` are emitted by the same preamble. They are textual `#define`s and so do not appear individually in the residue array — only the trait struct names they expand to.

```cpp
// Emitted verbatim into the .int.c preheader (excerpt, formatted):
namespace {
  template <typename Tag, typename OpFuncR, typename ...OpFuncArgs>
  struct __nv_hdl_helper {
    typedef void*    (*fp_copier_t)(void*);
    typedef OpFuncR  (*fp_caller_t)(void*, OpFuncArgs...);
    typedef void     (*fp_deleter_t)(void*);
    typedef OpFuncR  (*fp_noobject_caller_t)(OpFuncArgs...);
    static fp_copier_t          fp_copier;
    static fp_caller_t          fp_caller;
    static fp_deleter_t         fp_deleter;
    static fp_noobject_caller_t fp_noobject_caller;
  };
} // anonymous namespace
```

> ⚡ **QUIRK — Lambda trait helpers exist only as compiler-injected source**
> No `cooperative_groups.h`, no `nv/target`, no header anywhere ships these names. They are constructed at compile time by the cudafe++ wrapup pass and inserted into the `.int.c` output ahead of user code. As a result, grepping the CUDA Toolkit `include/` tree for `__nv_hdl_helper` finds nothing — but every `.int.c` that contains an extended lambda starts with the preamble. The user-visible footprint is the public name-mangling tag (`Unvhdl`, recognised by the demangler at `0x7CABB0` per [binary-layout.md](../binary-layout.md)), not the internal struct.

Confidence: **HIGH** for the entire group — the preamble source is in `.rodata` and the emitter is documented.

---

## Group 7 — Quick Lookup Table

A single sorted index in case you arrive at this page from a grep result and need to find the section.

| Prefix | Count | Section |
|---|---|---|
| `__nv_associate_access_property_impl` | 1 | Group 5 (access property) |
| `__nv_atomic_*` (load/store/exchange/CAS/fetch_OP/fence/aliases) | 70 | Group 1 |
| `__nv_atomic_` (bare prefix) | 1 | Group 1, Sub-family E |
| `__nv_cluster*_impl` | 12 | Group 2 |
| `__nv_cvta_*_impl` | 8 | Group 3 |
| `__nv_extended_*_lambda_trait_helper` | 3 | Group 6 |
| `__nv_hdl_helper` / `_trait` / `_trait_outer` | 3 | Group 6 |
| `__nv_hdl_wrapper_t` | 1 | Group 6 |
| `__nv_hdl_create_wrapper` / `_t` | 2 | Group 6 |
| `__nv_init_managed_rt_with_module` | 1 | Group 5 |
| `__nv_isClusterShared_impl` | 1 | Group 5 |
| `__nv_lambda_array_wrapper` | 1 | Group 6 |
| `__nv_lambda_field_type` | 1 | Group 6 |
| `__nv_lambda_trait_remove_const` | 1 | Group 6 |
| `__nv_lambda_trait_remove_volatile` | 1 | Group 6 |
| `__nv_lambda_trait_remove_dl_wrapper` | 1 | Group 6 |
| `__nv_memcpy_async_shared_global_*_impl` | 3 | Group 4 |
| `__nv_p2r` / `__nv_r2p` | 2 | Group 5 |
| `__nv_static_` | 1 | Group 5 (prefix, not a complete name) |
| `__nv_tex_surf_handler` | 1 | Group 5 |
| `__nv_threadfence_cluster_impl` | 1 | Group 5 |
| **Total** | **110** | |

---

## How the Rewriter Picks the Suffix

A worked example tying together Groups 1, 3, and 4 — the families that are produced by an active rewrite step in cudafe++ rather than just consumed by name. The rewriter is part of the expression parser (`sub_537BF0`, `adjust_sync_atomic_builtin`) and runs as soon as a call expression resolves its callee to a known builtin.

Input C++ source:

```cpp
__device__ int example(int* p, int v) {
    return __atomic_fetch_add(p, v, __ATOMIC_RELAXED);
}
```

What the parser does, step by step:

1. The callee `__atomic_fetch_add` matches the builtin table. The remap function is invoked.
2. The argument list is inspected to recover the operand type of `*p`. Here it is `int` (signed, 4 bytes).
3. A `stringbuilder` is initialised with the prefix `__nv_atomic_` (the bare-prefix string at `0xa8359d`, Group 1 sub-family E).
4. The operation name `fetch_add` is appended.
5. The width suffix `_4` is appended (sizeof(int) == 4).
6. The sign suffix `_s` is appended (signed integer; `_u` for unsigned, `_f` for float).
7. The full string `__nv_atomic_fetch_add_4_s` is looked up as an identifier. The lookup succeeds against the symbol at `0x8a5645`'s neighbouring address.
8. The call expression is rewritten in place: the callee is replaced, but the argument list is preserved verbatim (the memory order arg already matches the intrinsic's expected position).

By the time control returns to the parser caller, the IL tree already references the type-specialised intrinsic. cicc, downstream, performs no further analysis: it matches the literal name to the PTX instruction `atom.add.s32`. If the user had written `__atomic_fetch_add(pf, vf, __ATOMIC_RELAXED)` with `pf : float*`, the same rewriter would have produced `__nv_atomic_fetch_add_4_f`, which cicc lowers to `atom.add.f32`. The decision between integer and float PTX instructions happens here, at name-rewrite time, before the IL is even built.

This is why the residue list contains every cross-product of (op × width × sign): the rewriter needs a target identifier for each combination it can possibly produce. Combinations that the rewriter never emits — for instance, `__nv_atomic_fetch_xor_4_f` — are absent. Bitwise operations have no float lowering in PTX, so the residue list has only `__nv_atomic_fetch_xor_4` and `__nv_atomic_fetch_xor_8` (no `_s` / `_u` / `_f` axis). The presence of a name in the residue is, indirectly, evidence that the corresponding PTX instruction exists.

> ⚡ **QUIRK — The residue list is a lower bound on the rewriter's range**
> Every name in the residue corresponds to an actually-emittable rewrite output. Names that are theoretically possible but never emitted (because the PTX instruction does not exist, or because the rewriter does not produce them) do not appear. This makes the residue list a fingerprint of the rewriter's emit set, which is itself a fingerprint of the PTX atomic-instruction repertoire of the compiler version. A diff between cudafe++ v12 and v13 residue lists is a diff of PTX atomic capabilities — for example, the appearance of `__nv_atomic_compare_exchange_16` in v12+ marks the introduction of 128-bit CAS on sm_90.

Confidence: **HIGH** for the rewriter mechanism (the `adjust_sync_atomic_builtin` function is documented in `expression-parser.md`), **MED** for the "lower-bound fingerprint" claim (it follows from the rewrite mechanism but the v12 vs v13 diff has not been run).

---

## Cross-References

- [Expression Parser](../edg/expression-parser.md) — `adjust_sync_atomic_builtin` (`sub_537BF0`) does the `__sync_*` → `__nv_atomic_*` rewrite that produces 70 of the 110 names in this catalogue.
- [Architecture Feature Gating](../cuda/arch-gating.md) — sm_60 / sm_70 / sm_80 / sm_90 thresholds for the atomic, async-memcpy, and cluster intrinsic families.
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) — diagnostic strings such as `__nv_atomic_* functions are not supported on arch < sm_60` and `nv_atomic_function_address_taken` belong to this family.
- [Host-Device Lambda Wrapper](../lambda/host-device-wrapper.md) — assembly of the `__nv_hdl_wrapper_t<…>` preamble that defines 14 of the 110 names in this catalogue.
- [Preamble Injection](../lambda/preamble-injection.md) — `emit_lambda_preamble` (`sub_6BCC20`) and the multi-kilobyte `.rodata` blobs at `0xa81c88` / `0xa82288` / `0xa82600` / `0xa831b0`.
- [Managed Variables](managed-variables.md) — `__nv_init_managed_rt_with_module` and the surrounding host-side runtime trampolines.
- [Binary Layout](../binary-layout.md) — CUDA-aware demangler at `0x7CABB0` that handles the `Unvdl` / `Unvdtl` / `Unvhdl` template prefixes underlying the lambda machinery names.
- [Minor Attributes](minor-attributes.md) — the small set of `__nv_*` identifiers that **are** real attributes (`__nv_pure__`, `__nv_register_params__`); contrast with this page's contents.

## Open Follow-Ups

1. `__nv_static_` is suspected to be a string-builder prefix, but no caller has been disassembled to confirm whether it is the only prefix or one of several. A pass through the `.int.c` writer (the function map page lists `sub_5565E0` and neighbours) would resolve this.
2. The cache-mode split for `__nv_memcpy_async_shared_global_*` (`.ca` vs `.cg`) is inferred from PTX documentation, not from a diagnostic in cudafe++. A driver-tools cross-check against `nvdisasm` output of a known `cuda::memcpy_async<16>` call would tie it down.
3. The `__nv_atomic_*_n` template forms (`load_n`, `store_n`, `exchange_n`, `compare_exchange_n`) need a worked example to confirm that cicc indeed defers width selection until template instantiation rather than rejecting them outright.
4. `__nv_tex_surf_handler` is attached to texture/surface handle accessors per `binary-layout.md`'s demangler note, but the specific lowering (does it become a `tex.1d` reference, a runtime API call, or a metadata-only marker?) remains uninvestigated.
5. `__nv_associate_access_property_impl` accepts an L2 cache property handle; the exact encoding of the property (persisting / streaming / normal) and whether it threads into the `ld.global.L2::*` PTX hints needs follow-up against `cuda::access_property` SDK headers.
