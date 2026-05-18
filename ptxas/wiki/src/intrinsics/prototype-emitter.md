# Prototype Emitter (`sub_5FF700` -- 1,080-Case Dispatch)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

`sub_5FF700` is the leaf of the classical intrinsic pipeline described in [Intrinsic Table Architecture](./index.md). After the body-template registrar (`sub_5D7430`) assigns each `__cuda_*` runtime helper a dense integer ID in the range 0 -- 1,079, the prototype emitter consumes that ID and writes the corresponding **PTX declaration string** (`.weak .func ...` or `.FORCE_INLINE .func ...`) into a freshly allocated buffer. Every helper called by emitted PTX needs its declaration appended to the compilation unit before SASS lowering, and this function is where that text is materialised.

| | |
|---|---|
| **Address** | `0x5FF700` |
| **Size** | 34,362 bytes (≈ 33.6 KiB; the largest single function in the intrinsic subsystem) |
| **Basic blocks** | 1,085 |
| **Callees** | 1,080 (one per case, all chained to the small-buffer allocator `sub_4DA340` then `strcpy`) |
| **Callers** | 1 (`sub_4CCF30` at `0x4CDFC0`, the body-template materialisation driver) |
| **Switch instruction** | `0x5FF711` (jump table base immediately after the dispatch entry) |
| **Switch ID range** | 0 -- 1,079 (dense, no holes) |
| **Default branch** | `0x40592E` style sink (no-op fall-through, ID 1079 maps to `0x607D16`) |
| **String references** | 1,035 distinct .rodata pointers (some IDs reuse the same string with different leading whitespace) |
| **Output directives** | 571× `.weak .func`, 464× `.FORCE_INLINE .func` |

## Position in the Pipeline

```
sub_5D7430(ctx)        ─── populates body-template name → ID hash map
   │ id 0 = "__cuda_sm20_div_s16"
   │ id 1 = "__cuda_sm20_div_u16"
   │ id 2 = "__cuda_sm20_rem_s16"
   │ ...
   │ id 1079 = "__cuda_sm10x_tcgen05_guardrails_*"
   v
sub_4CCF30(ctx, id)    ─── body-template materialisation driver
   │ allocates a `struct ptx_decl` (header + variable text)
   │ delegates the prototype string to sub_5FF700
   v
sub_5FF700(buffer, id) ─── THIS PAGE
   │ switch(id) {
   │   case 0:   strcpy(buf, "        .weak .func (.reg .s32 %d) __cuda_sm20_div_s16 ..."); break;
   │   case 4:   strcpy(buf, "    .weak .func (.reg .u64 %rdv1) __cuda_sm20_div_u64 ..."); break;
   │   case 25:  strcpy(buf, ".weak .func (.reg .f64 %fdv1) __cuda_sm20_div_rn_f64_full ..."); break;
   │   ...
   │   case 1078: strcpy(buf, "        .weak .func (.reg .s32 %d) __cuda_sm20_div_s16 ...");
   │   default:  /* no-op, falls through */
   │ }
   v
   buffer holds a NUL-terminated PTX declaration ready for the PTX printer
```

The body-template ID assigned by `sub_5D7430` and the switch value consumed by `sub_5FF700` are the **same** integer; the two functions are paired and must be kept in lock-step. Adding a new helper means appending one entry to `sub_5D7430`'s prefix-suffix concatenation loop and one new switch case here with the matching prototype text.

## Dispatch Layout

The switch is implemented with the System V jump-table idiom: a 32-bit base pointer in `.rodata`, indexed by the helper ID, producing the target block address. Every case body is a 3-instruction sequence -- `lea buffer_ptr`, `lea string_literal_ptr`, `call sub_4DA340` -- which is why the function is so large: 1,080 such bodies inflate to ≈ 34 KiB even though no real logic lives in any individual case.

```
0x5FF700: <prologue, load id into edi, range-check>
0x5FF711: switch dispatch (indirect jmp through jump table)
0x5FF718: case 1078 body (str_sm20_div_s16)
0x5FF736: case 1077 body (str_sm20_div_u16)
0x5FF77C: case 1076 body (str_sm20_rem_s16)
...
0x607CF8: case 0    body (lowest ID, observed last in linear layout)
0x607D02: case 1079 body (highest ID, observed first in linear layout)
0x607D16: default / fall-through
```

The IDA-extracted jump table is reverse-ordered relative to the helper-ID space: the linear address-order of the case bodies decreases as the ID increases, which suggests the compiler that built ptxas emitted the switch from a sorted descending list (likely an `std::map<int,string>` walked in reverse) and the linker did not reshuffle.

## Output Format

Each switch case writes exactly one PTX prototype. The prototype always conforms to one of two templates:

**Weak external linkage** -- the implementation lives in a separate driver-shipped object that the JIT will pull in at link time:
```ptx
.weak .func (<return list>) <symbol> (<parameter list>);
```

**Force-inlined helper** -- ptxas itself inlines the body in the same translation unit, so the declaration carries the `.FORCE_INLINE` linkage attribute instead of `.weak`:
```ptx
.FORCE_INLINE .func (<return list>) <symbol> (<parameter list>);
```

The function uses `.weak` for helpers that may be supplied externally (the legacy SM20/SM3x soft-float runtime, the sanitiser hooks, video-emulation routines) and `.FORCE_INLINE` for helpers that ptxas guarantees to inline at every call site (the entire SM70+ WMMA family, SM10x tcgen05 guardrails, warp-sync shims, `createpolicy_*` builders, dp4a/dp2a).

The two directives are not interchangeable. `.weak` lets the SASS linker leave an unresolved reference; `.FORCE_INLINE` is a ptxas-specific extension that forces inlining and would cause a link error if the symbol were missing.

## Linkage Distribution Across Helper Families

| Family prefix | weak | force-inline | Notes |
|---|---:|---:|---|
| `__cuda_sm20_*` | 70 | 0 | Fermi soft-float (div/rcp/sqrt/rem on i16/i64/f32/f64) |
| `__cuda_sm3x_*` | 4 | 0 | Kepler f32 division (FTZ / non-FTZ slow paths) |
| `__cuda_sm62_*` | 2 | 0 | Pascal dp2a / dp4a -- pre-SASS lowering shim |
| `__cuda_sm70_*` (non-WMMA) | 393 | 40 | Volta barriers, votesync, shflsync, matchsync, query_activemask |
| `__cuda_sm7x_wmma_*` | 0 | 229 | Volta+ WMMA infrastructure (10 shapes × loads/stores/MMA/downconvert) |
| `__cuda_sm72_*` | 0 | 105 | Xavier MMA tile loads/stores |
| `__cuda_sm8x_*` | 0 | 80 | Ampere `mma.sync` and TF32 helpers |
| `__cuda_sm80_*` | 3 | 1 | `createpolicy_range`, `createpolicy_fractional` (+ `_encode` siblings) |
| `__cuda_sm10x_*` | 9 | 11 | Blackwell tcgen05 allocation + guardrail traps |
| `__cuda_reduxsync_*` | 17 | 0 | Warp redux (add/min/max for u/s/b types) |
| `__cuda_sanitizer_*` | 9 | 0 | Compute-sanitizer memcheck callbacks |
| `__cuda_scalar_video_*` | 7 | 0 | Scalar video-instruction emulation |
| (unnamed `__cuda_sm_*`) | 63 | 0 | Unprefixed legacy helpers |
| **Total** | **571** | **464** | (some IDs reuse strings -> 1,035 unique strings cover 1,080 cases) |

## Per-Family Prototype Patterns

### Soft-float runtime (sm20 / sm3x)

The Fermi/Kepler soft-float helpers were the first batch added and use a consistent two- or one-operand signature with explicit rounding-mode and FTZ-mode suffixes. Both a fast path and a `_slowpath` variant are declared for every (rounding × ftz) cross product, which is why this family alone consumes ~120 IDs:

```ptx
.weak .func (.reg .s32 %d)   __cuda_sm20_div_s16          (.reg .s32 %a0, .reg .s32 %a1);
.weak .func (.reg .u64 %rdv1) __cuda_sm20_div_u64         (.reg .u64 %rda1, .reg .u64 %rda2);
.weak .func (.reg .f32 %fv1) __cuda_sm20_div_rn_f32       (.reg .f32 %fa1, .reg .f32 %fa2);
.weak .func (.reg .f32 %fv1) __cuda_sm20_div_rn_ftz_f32_slowpath
                                                          (.reg .f32 %fa1, .reg .f32 %fa2);
.weak .func (.reg .f64 %fdv1) __cuda_sm20_div_rn_f64_fast (.reg .f64 %fdnum, .reg .f64 %fdden);
.weak .func (.reg .f64 %fdv1) __cuda_sm20_div_rn_f64_full (.reg .f64 %fdnum, .reg .f64 %fdden);
```

Cross-product: `{div, rcp, sqrt, dblrcp, drsqrt, dsqrt, rem}` × `{rn, rd, ru, rz}` × `{ftz, noftz}` × `{f32, f64}` × `{fast, full, slowpath}`. Not every combination exists (no `rem_f64`, no `dsqrt_ftz`) which is why the family count is ~70 rather than the full 144 cross-product.

The `_v2`, `_v3`, `_full`, `_fast` suffixes encode incompatibility with the original ABI: when NVIDIA upgraded the rounding semantics for IEEE compliance, the helper was renamed (not versioned) so the SASS linker could distinguish callers built against the new contract from those built against the old.

### Pascal / Volta dot-product shims

`dp4a` and `dp2a` were introduced on SM6.2 (Tegra X2 / Drive PX-2) before the SASS `IDP` instruction existed, so ptxas exposed them as runtime helpers:

```ptx
.weak .func (.reg .b32 %dst) __cuda_sm62_dp4a (.reg .b32 %arg0, .reg .b32 %arg1, .reg .b32 %arg2);
.weak .func (.reg .b32 %dst) __cuda_sm62_dp2a (.reg .b32 %arg0, .reg .b32 %arg1, .reg .b32 %arg2,
                                               .reg .b32 %offset0, .reg .b32 %offset1);
```

On SM7.0+ ptxas lowers these directly to `IDP4A` / `IDP2A` and the `__cuda_sm62_dp*` helpers are unused, but their declarations are still emitted unconditionally when a `dp*a` PTX instruction appears -- the dead-code eliminator in the linker drops the unresolved symbol later.

### WMMA family (SM70+ tensor cores)

The WMMA helpers dominate the table (382 IDs across `sm7x_wmma`, `sm70_wmma`, `sm72`, `sm8x`), one per `(shape, role, layout, memory-space, type)` tuple:

```ptx
.FORCE_INLINE .func (.reg .b32 %d0, ..., .reg .b32 %d7)
              __cuda_sm70_wmma_m16n16k16_load_a_col_shared (.reg .b64 %ptr, .reg .s32 %stride);
.FORCE_INLINE .func ()
              __cuda_sm70_wmma_m16n16k16_acc_f32_row_update_ptr (.reg .b64 %ptr, .reg .s32 %stride,
                                                                  .reg .b32 %v0, ..., .reg .b32 %v7);
.FORCE_INLINE .func (.reg .b32 %d0, .reg .b32 %d1)
              __cuda_sm70_wmma_downconvert         (.reg .b32 %s0, ..., .reg .b32 %s3);
.FORCE_INLINE .func (.reg .b32 %d0, .reg .b32 %d1)
              __cuda_sm70_wmma_downconvert_satfinite (.reg .b32 %s0, ..., .reg .b32 %s3);
```

Shape coverage: m16n16k16, m16n16k8, m8n8k4, m8n8k32, m8n8k128, m32n8k16, m8n32k16, m16n8k16, m16n8k32, m16n8k64, m16n8k128, m16n8k256 (12 distinct shapes). Every shape × `{load_a, load_b, load_c, mma, store_d, downconvert, acc}` × `{col, row}` × `{global, shared, generic}` × `{f16, f32, f64, bf16, tf32, s8, u8, s4, u4, s32, b1}` produces an entry. The combinatorial explosion is why this family is the single largest -- and why `.FORCE_INLINE` matters: each declared helper is inlined into the calling kernel, so the WMMA-using kernel becomes self-contained without a runtime library.

### Volta barrier family

```ptx
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive        (.reg .u32 %bar);
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive_0      ();
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive_0_count (.reg .u32 %count);
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive_1      ();
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive_1_count (.reg .u32 %count);
...
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive_15     ();
.FORCE_INLINE .func ()             __cuda_sm70_barrier_arrive_15_count (.reg .u32 %count);
```

Sixteen barrier slots × `{arrive, sync, wait}` × `{plain, _count}` = 96 entries. The hand-unrolled per-slot variants (`_0` ... `_15`) exist because ptxas emits the SASS `BAR.SYNC` immediate form when the barrier index is constant -- and matching the immediate form requires a distinct helper symbol so PTX-level CSE doesn't collapse them.

### Blackwell `tcgen05` allocation and guardrails

```ptx
.FORCE_INLINE .func (.reg .b32 dummy)
              __cuda_sm10x_tcgen05_alloc_one_sm (.reg .u32 __cuda_sm10x_tc_alloc_dst_ptr_arg,
                                                 .reg .u32 __cuda_sm10x_tc_alloc_num_cols_arg);
.FORCE_INLINE .func (.reg .b32 dummy)
              __cuda_sm10x_tcgen05_alloc_two_sm (.reg .u32 __cuda_sm10x_tc_alloc_dst_ptr_arg,
                                                 .reg .u32 __cuda_sm10x_tc_alloc_num_cols_arg);
.weak .func   __cuda_sm10x_tcgen05_guardrails_check_allocation_granularity (...);
.weak .func   __cuda_sm10x_tcgen05_guardrails_check_column_allocation (...);
.weak .func   __cuda_sm10x_tcgen05_guardrails_check_datapath_alignment (...);
.weak .func   __cuda_sm10x_tcgen05_guardrails_check_datapath_validity (...);
.weak .func   __cuda_sm10x_tcgen05_guardrail_trap_allocation_granularity_invalid (...);
.weak .func   __cuda_sm10x_tcgen05_guardrail_trap_access_out_of_physical_bounds (...);
```

The two `_alloc_*` builders are FORCE_INLINE (the SASS lowering emits `TCGEN05.ALLOC` directly), but the *guardrail traps* are weak. They live in the compute-sanitizer object: when `-Xcompiler-debug --tcgen05-guardrails` is active the linker resolves them; otherwise the symbol is dead-stripped. See [TCGen05 -- 5th Gen Tensor Cores](../targets/tcgen05.md) for the runtime semantics.

### SM80 `createpolicy_*`

```ptx
.FORCE_INLINE .func (.reg .b32 blockSizeBits, .reg .b64 blockStart, .reg .b64 blockCount)
              __cuda_sm80_createpolicy_range (.reg .b64 addr,
                                              .reg .b32 primary_size, .reg .b32 total_size);
.weak         .func (.reg .b64 image)
              __cuda_sm80_createpolicy_range_encode (.reg .b32 blockSizeBits,
                                                     .reg .b64 blockStart, .reg .b64 blockCount);
.weak         .func (.reg .b32 numerator_0_15)
              __cuda_sm80_createpolicy_fractional (.reg .b32 fraction);
.weak         .func (.reg .b64 image)
              __cuda_sm80_createpolicy_fractional_encode (.reg .b32 numerator_0_15);
```

`createpolicy_range` decomposes a (primary, total) byte-range into the tuple `(blockSizeBits, blockStart, blockCount)` -- it's an arithmetic shim, FORCE_INLINE so the surrounding kernel sees the resulting expression and constant-folds. The `_encode` siblings pack that tuple into the 64-bit policy descriptor consumed by `LDG.MC` / `STG`. They stay weak because the encoding is identical across SM80/86/89/90 and the implementation lives in a single shared runtime object.

### Warp-redux and warp-match

```ptx
.weak .func (.reg .b32 %dst) __cuda_reduxsync_b32       (.reg .b32 %src, .reg .b32 %mask);
.weak .func (.reg .b32 %dst) __cuda_reduxsync_u32_min   (.reg .b32 %src, .reg .b32 %mask);
.weak .func (.reg .b32 %dst) __cuda_reduxsync_f32_add   (.reg .f32 %src, .reg .b32 %mask);
.FORCE_INLINE .func (.reg .b32 %dst) __cuda_sm70_matchsync_aligned_all_b32 (.reg .b32 %arg, .reg .b32 %mask);
.FORCE_INLINE .func (.reg .b32 %dst, .reg .pred %p)
              __cuda_sm70_matchsync_aligned_all_b32_p (.reg .b32 %arg, .reg .b32 %mask);
```

`reduxsync_*` are weak because SM80 `REDUX` is reach-extended via the runtime on SM70/SM72/SM75. `matchsync_*` are FORCE_INLINE because the SASS `MATCH.ALL` / `MATCH.ANY` instructions are always present on the target. Note the predicate-returning variants (`*_p`) which exist as separate IDs -- the underlying PTX `match.sync` has two return slots and the second one is optional, but ptxas treats them as distinct intrinsics for hash-map purposes.

### Compute-sanitizer hooks

```ptx
.weak .func (.reg .b32 %dummy) __cuda_sanitizer_memcheck_malloc (.reg .b64 %ptr, .reg .b64 %size);
.weak .func (.reg .b32 %dummy) __cuda_sanitizer_memcheck_free   (.reg .b64 %ptr);
.weak .func (.reg .b64 %meta)  __cuda_sanitizer_memcheck_readmetadata (.reg .b64 %ptr, .reg .b32 %op);
.weak .func .param .u64 _ZZN6...   __cuda_sanitizer_memcheck_malloc_param_0;
.weak .func .param .u64 _ZZN6...   __cuda_sanitizer_memcheck_malloc_param_1;
```

The three runtime entries (`malloc`, `free`, `readmetadata`) plus their `_param_0` / `_param_1` shadow symbols (used by compute-sanitizer to interpose argument capture). All weak because the sanitizer runtime is loaded only with `compute-sanitizer --tool memcheck`.

## Allocation Path -- `sub_4DA340`

Every case body resolves to the same three-step sequence: load the destination buffer pointer (passed as the first argument), load a `.rodata` pointer to the literal prototype string, and tail-call `sub_4DA340(buffer, string)`. `sub_4DA340` is a small-string buffered allocator that:

1. Computes `len = strlen(string)`.
2. Calls the per-thread arena allocator (`sub_4D8D80`) for `len + 1` bytes.
3. `memcpy` of the string into the arena slot.
4. Writes the arena pointer into `buffer`.

Because the prototype literals live in `.rodata` (read-only) the allocation is required only so downstream passes can append per-instance metadata (e.g. a comment about which kernel pulled in the helper) without rewriting the original string in `.rodata`. The arena slot is freed when the compilation unit closes.

## QUIRK -- `.FORCE_INLINE` is a ptxas-only directive

The PTX ISA documentation does **not** list `.FORCE_INLINE` as a linkage attribute -- the public document only defines `.func`, `.weak .func`, `.entry`, and `.extern .func`. `.FORCE_INLINE` is a private extension recognised only by ptxas's PTX parser (`sub_50B4F0`, the directive-handling table) and stripped before the resulting cubin is emitted. If you hand-write PTX with `.FORCE_INLINE` and feed it to a non-NVIDIA PTX parser (LLVM NVPTX, Open64), it will fail to parse. The 464 occurrences of this directive in `sub_5FF700` are the only documented use sites; they vanish from the cubin because the inliner runs before the SASS printer.

## QUIRK -- Helper IDs are not stable across ptxas versions

The dense ID space (0 -- 1,079) is populated by the order in which `sub_5D7430` walks its prefix-suffix tables, and that order has changed three times in the public CUDA 12.x -> 13.x window. A cubin embedded from CUDA 12.0 referencing helper ID 942 will not resolve to the same `__cuda_*` symbol when re-encoded by CUDA 13.0 ptxas. This is harmless because the IDs are an in-memory artefact -- they never appear in the cubin -- but it does break any third-party tool that scrapes the body-template table and tries to memoise by integer. Always rekey by symbol name, never by ID. The MEMORY-pickle method in [`cicc/wiki/methodology.md`](../../../../cicc/wiki/src/methodology.md) (sister-tool extraction recipe) implicitly assumes name keying for exactly this reason.

## QUIRK -- The `0` case is at the highest address in the binary

The IDA jump table for `0x5FF711` lists case bodies in **descending** ID order, which leaves case 0 (`__cuda_sm20_div_s16`) at the linearly *latest* address `0x607CF8`, and case 1079 (`__cuda_sm10x_tcgen05_guardrails_*`) immediately after the dispatch at `0x5FF718`. The most-likely explanation is that the compiler that built ptxas walked an `std::map<int, ...>` in reverse iteration order while emitting the switch. The default branch is `0x607D16`, which sits **between** case 1079's body and the next function -- IDs above 1079 are unreachable from `sub_4CCF30` because the body-template registrar caps the ID space, but the layout means the default branch is a 5-byte no-op tail. Any binary-diff tool that compares ptxas builds by linear address will report wholesale code movement here even when the only change is "added one new helper at ID 0 of the new ordering".

## QUIRK -- `_param_0` / `_param_1` shadow symbols for sanitiser hooks

The compute-sanitizer hooks (e.g. `__cuda_sanitizer_memcheck_malloc`) come with paired `_param_0` and `_param_1` symbols that look like the parameters of the helper but are declared **as separate `.weak .func`** entries with no body and an empty signature. These are not callable -- they are address-only sentinels that the sanitiser instrumentation pass binds to during PTX rewriting, so that the rewriter can replace `call malloc, %addr, %size` with `call malloc, %_param_0, %_param_1`. The `_param_*` symbols never appear in the cubin (the sanitiser pass replaces them with concrete `.param` slot bindings) but they must be present in the intrinsic table so the PTX parser accepts them without complaining about undefined references.

## Cross-References

- [Intrinsic Table Architecture](./index.md) -- master intrinsic registration, the upstream `sub_5D1660` / `sub_5D7430` pair that produces the IDs consumed here.
- [Math Intrinsics](./math.md) -- soft-float runtime helpers (sm20/sm3x family).
- [Tensor Core Intrinsics](./tensor.md) -- WMMA / MMA / WGMMA lowering and how the FORCE_INLINE prototype is consumed by the inliner.
- [Sync & Warp Intrinsics](./sync-warp.md) -- barrier, vote, shuffle, match, redux helpers.
- [TCGen05 -- 5th Gen Tensor Cores](../targets/tcgen05.md) -- guardrail trap semantics referenced from the sm10x family.
- [Memory Pool Allocator](../infra/memory-pools.md) -- arena used by `sub_4DA340` to copy the prototype literal.
