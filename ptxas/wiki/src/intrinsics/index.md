# Intrinsic Table Architecture (608 Entries)

ptxas maintains two separate intrinsic subsystems that together cover every CUDA runtime helper function, every PTX opcode requiring inline code generation, and every Blackwell+ OCG builtin operation. The first subsystem (`sub_5D1660` + `sub_5D4190` + `sub_5FF700`) handles classical CUDA intrinsics and PTX opcode dispatch through a name-to-ID hash map and a giant prototype generator. The second subsystem (`sub_6C9EB0` and its handler cluster at `0x6C0000`--`0x6CC000`) handles OCG (Optimized Code Generation) builtins for SM100+ targets. Both subsystems use the same hash map infrastructure (`sub_425CA0` / `sub_426150` / `sub_426D60`) documented in [Hash Tables & Bitvectors](../infra/hash-bitvector.md).

| | |
|---|---|
| **Master registration** | `sub_5D1660` (46KB) -- 608 CUDA intrinsics, name-to-integer-ID hash map |
| **Opcode dispatch** | `sub_5D4190` (41KB) -- ~120 PTX opcodes to codegen handlers + ~400 MMA hash entries |
| **Prototype generator** | `sub_5FF700` (354KB) -- switch generating `.weak .func` PTX declarations |
| **OCG intrinsic table** | `sub_6C9EB0` (13KB) -- `__nv_ptx_builtin_ocg_*` dispatch for SM100+ |
| **OCG router** | `sub_6CC690` (22KB) -- routes OCG calls to type-specific handlers |
| **OCG name resolver** | `sub_6C9BC0` -- resolves operation names to internal enums |
| **Hash map create** | `sub_425CA0` (initial capacity 0x80) |
| **Hash map insert** | `sub_426150(map, name, value)` |
| **Hash map lookup** | `sub_426D60` |

**Per-Family Deep Dives:**

- [Math Intrinsics](math.md) -- IEEE math software emulation (div, rcp, sqrt, rem)
- [Tensor Core Intrinsics](tensor.md) -- WMMA, MMA, WGMMA, tcgen05 lowering
- [Sync & Warp Intrinsics](sync-warp.md) -- Barriers, vote, shuffle, match, redux

## System Overview

```
PTX source
  |
  v
sub_5D4190 ─────────────────────────────────────────────────────────────────┐
  │ (1) Calls sub_5D1660 to populate intrinsic ID table (608 entries)       │
  │ (2) Registers ~120 PTX opcode -> codegen handler mappings               │
  │ (3) Registers ~400 MMA hash -> codegen handler mappings                 │
  │                                                                         │
  ├─ Hash map at a1+808  ── PTX opcode name -> codegen function pointer     │
  │    "div"     -> sub_5B76D0  (64KB)                                      │
  │    "sqrt"    -> sub_5B4040  (49KB)                                      │
  │    "wmma.mma"-> sub_5C7A50  (173KB)                                     │
  │    "mma"     -> sub_5C10A0  (120KB)                                     │
  │    ... ~116 more                                                        │
  │                                                                         │
  ├─ Hash map at a1+816  ── numeric MMA hash -> codegen function pointer    │
  │    "2644314910" -> sub_4DDB80                                           │
  │    ... ~399 more (shape/type/layout combinations)                       │
  │                                                                         │
  └─ ID table at a1+1056 ── 9728-byte array (memcpy from unk_1D4D940)      │
     Hash map at a1+1064 ── name -> integer ID (sub_5D1660, 608 entries)    │
     Count at a1+1072 = 608                                                 │
                                                                            │
sub_5FF700 (354KB) ─────────────────────────────────────────────────────────┘
  │ switch(intrinsic_case_number) with hundreds of cases
  │ Each case: allocate buffer via sub_4DA340, strcpy() PTX prototype
  │
  │ case 0:  ".weak .func (.reg .s32 %d) __cuda_sm20_div_s16
  │           (.reg .s32 %a0, .reg .s32 %a1)"
  │ case 4:  ".weak .func (.reg .u64 %rdv1) __cuda_sm20_div_u64
  │           (.reg .u64 %rda1, .reg .u64 %rda2)"
  │ case 9:  ".weak .func (.reg .f32 %fv1) __cuda_sm20_div_rn_f32
  │           (.reg .f32 %fa1, .reg .f32 %fa2)"
  │ case 25: ".weak .func (.reg .f64 %fdv1) __cuda_sm20_div_rn_f64_full
  │           (...)"
  │ ... hundreds more for rcp, sqrt, dsqrt, barrier, wmma, mma, etc.
  v
Emitted into PTX output as .weak .func declarations
(linker resolves calls to runtime helper functions)
```

## Master Registration -- `sub_5D1660`

This 46KB function is the master catalog. It allocates a 9728-byte table (`memcpy` from `unk_1D4D940`, 0x2600 bytes), creates a hash map with initial capacity 0x80 via `sub_425CA0`, then calls `sub_426150(hashmap, "name", (char*)ID)` exactly 608 times to register every CUDA runtime helper function with an integer ID. The hash map is stored at `a1+1064`, the table at `a1+1056`, and the count 608 at `a1+1072`.

### Complete ID Allocation

608 intrinsics are registered with IDs from `0x01` through `0x25F`. The ID ranges partition cleanly by SM generation and functional category.

| ID Range | Count | Prefix | Category | SM Floor |
|---|---|---|---|---|
| `0x01`--`0x11` | 17 | `__cuda_reduxsync_*` | Redux sync (b32 and/or/xor, f32 max/min/abs/NaN, s32/u32 add/max/min) | sm_70 |
| `0x12`--`0x18` | 7 | `__cuda_sanitizer_memcheck_*` | Compute-sanitizer hooks (free, generic, global, local, malloc, readmetadata, shared) | -- |
| `0x19`--`0x1F` | 7 | `__cuda_scalar_video_emulation_*` | Video instruction emulation helpers | sm_20 |
| `0x20`--`0x2A` | 11 | `__cuda_sm10x_tcgen05_guardrail_trap_*` | Blackwell tcgen05 guardrail traps + mask helper | sm_100 |
| `0x2B`--`0x3C` | 18 | `__cuda_sm1xx_*` | Bulk copy + cp.async.bulk.tensor 1D--5D tile/im2col uni/multicast | sm_100+ |
| `0x3D`--`0x82` | 70 | `__cuda_sm20_*` | IEEE math: bfe, bfi, div, rcp, sqrt, dsqrt, drsqrt, rem (all rounding modes + slowpaths) | sm_20 |
| `0x83`--`0x86` | 4 | `__cuda_sm3x_div_*` | Optimized division variants | sm_30 |
| `0x87`--`0x88` | 2 | `__cuda_sm62_dp2a/dp4a` | Integer dot product emulation | sm_62 |
| `0x89`--`0x1FA` | 370 | `__cuda_sm70_*` | Volta+ intrinsics (barriers, shuffle, vote, match, WMMA -- all shapes, layouts, address spaces) | sm_70 |
| `0x1FB`--`0x208` | 14 | `__cuda_sm80_*` | Ampere: bf16/tf32/s4/s8/b1 MMA, createpolicy | sm_80 |
| `0x209`--`0x22F` | 39 | `__cuda_sm_8x_mma_*` | sm_8x direct MMA operations | sm_80+ |
| `0x230`--`0x239` | 10 | `__cuda_sm_10x_*` | Blackwell hmma/imma mdata + bit MMA | sm_100 |
| `0x23A`--`0x25F` | 38 | `__cuda_sm_9x_mma_sub_byte_internal_*` | Hopper sub-byte MMA: s4/u4 sparse m16n8k32/k64/k128 | sm_90 |

**Total: 608 intrinsics across 13 functional groups.**

### sm_70 Intrinsic Breakdown (IDs `0x89`--`0x1FA`)

The sm_70 block is by far the largest at 370 entries. It covers every Volta-era warp synchronous intrinsic plus the complete WMMA API. The explosion in count comes from the combinatorial product of shapes, layouts, data types, address spaces, and predicate/satfinite variants.

| Sub-Category | Examples | Combinatorial Source |
|---|---|---|
| `barrier_arrive` | 0--15, with/without count | 16 barrier IDs x 2 count variants |
| `barrier_red_and/or/popc` | 0--15, with/without count | 3 reduction ops x 16 IDs x 2 count |
| `barrier_sync` | 0--15, with/without count | 16 IDs x 2 count variants |
| `matchsync_all/any_b32/b64` | with predicate variants | 2 match modes x 2 types x pred |
| `shflsync_bfly/down/idx/up` | with predicate variants | 4 shuffle modes x pred |
| `votesync_all/any/ballot/uni` | -- | 4 vote modes |
| `warpsync` | -- | 1 entry |
| `wmma_*` | m16n16k16, m32n8k16, m8n32k16 | 3 shapes x {load_a, load_b, load_c, store_d, mma} x {row, col} x {f16, f32} x {generic, global, shared} x {satfinite} |

The WMMA entries dominate the count. Each combination of shape (m16n16k16/m32n8k16/m8n32k16), operation (load_a/load_b/load_c/store_d/mma), layout (row/col for each matrix), data type (f16/f32), address space (generic/global/shared), and optional satfinite flag produces a separate intrinsic registration.

## Opcode Dispatch -- `sub_5D4190`

This 41KB function first calls `sub_5D1660(a1)` to populate the intrinsic ID table, then builds two more hash maps for PTX opcode dispatch.

### Named Opcode Table (at `a1+808`)

~120 PTX instruction names mapped to codegen handler function pointers. Each handler allocates a 50,000-byte buffer, queries instruction properties through accessor functions on the instruction object at `a1+1096`, and generates inline PTX code via sequential `sprintf()` calls.

| Category | Opcodes | Codegen Handlers |
|---|---|---|
| **Math** | `div.full`, `div`, `rem`, `rcp`, `rsqrt`, `sqrt`, `ex2`, `lg2`, `tanh` | `sub_573860`, `sub_5B76D0` (64KB), `sub_589810`, `sub_5B0CD0` (44KB), `sub_57BFC0`, `sub_5B4040` (49KB), `sub_583190`, `sub_52A5C0`, `sub_505B00` |
| **Memory** | `membar`, `_ldldu`, `prefetch` | `sub_4DB410`, `sub_4DD860`, `sub_507FB0` |
| **Conversion** | `cvt` | `sub_59F630` |
| **Bit manipulation** | `bfind`, `brev`, `bfe`, `bfi`, `clz`, `popc`, `testp`, `copysign` | `sub_590C20`, `sub_50B5A0`, `sub_578470`, `sub_52E100`, `sub_4DBCC0`, `sub_4DB210`, `sub_581A10`, `sub_50B180` |
| **Texture** | `tex`, `tex.base`, `tex.level`, `tld4`, `tex.grad` | `sub_584D10`, `sub_5879B0`, `sub_58B6A0`, `sub_56D700`, `sub_5ADDC0` (50KB) |
| **Video (SIMD)** | `vadd`/`vsub`/`vmin`/`vmax`/`vabsdiff`/`vshl`/`vshr`/`vset`/`vmad` (scalar), `vadd2`/`vmax2`/`vmin2`/`vabsdiff2`/`vset2`/`vsub2`/`vavrg2` (packed 2x16), `vadd4`/`vmin4`/`vmax4`/`vabsdiff4`/`vset4`/`vsub4`/`vavrg4` (packed 4x8) | per-instruction handlers |
| **Dot product** | `dp2a.lo`, `dp2a.hi`, `dp4a` | `sub_56BA60`, `sub_56C8D0`, `sub_577BA0` |
| **Barriers** | `bar`, `barrier`, `bar.arrive`, `barrier.arrive`, `bar.red`, `barrier.red`, `bar.cta`/`barrier.cta` (.arrive/.red variants), `bar.warp` | `sub_524FB0`, `sub_570290`, `sub_500BF0`, `sub_570940`, `sub_52D590`, `sub_5889B0`, `sub_56A5A0` |
| **Warp** | `vote`, `shfl`, `match`, `redux` | `sub_580E50`, `sub_5801D0`, `sub_58A730`, `sub_567680` |
| **Async copy** | `cp.async.mbarrier.arrive`, `cp.async.bulk`, `cp.async.bulk.tensor` | `sub_4DC180`, `sub_593210`, `sub_5AB460` (45KB) |
| **Matrix** | `ldmatrix`, `movmatrix`, `stmatrix`, `st.async`, `red.async`, `st.bulk` | `sub_50D4B0`, `sub_4DAEA0`, `sub_4F05D0`, `sub_58E9B0`, `sub_5825A0`, `sub_549430` |
| **Cache** | `createpolicy.range`, `createpolicy.fractional`, `createpolicy.cvt` | per-instruction handlers |
| **WMMA** | `wmma.load.a`, `wmma.load.b`, `wmma.load.c`, `wmma.store.d`, `wmma.mma` | `sub_5A2D10`, `sub_5A0EA0`, `sub_5A8E40`, `sub_5A6BD0`, `sub_5C7A50` (173KB) |
| **MMA** | `mma` | `sub_5C10A0` (120KB) |
| **WGMMA** | `wgmma.mma_async`, `wgmma.fence`, `wgmma.commit_group`, `wgmma.wait_group` | `sub_50AC70`, `sub_4DA380`, `sub_4DA4B0`, `sub_4DA5E0` |
| **Multimem** | `multimem.ld_reduce`, `multimem.st`, `multimem.red` | `sub_58D8B0`, `sub_57B4C0`, `sub_50A850` |
| **Tensormap** | `tensormap.replace` | `sub_57F6E0` |
| **TCGen05** | `tcgen05.alloc`, `tcgen05.relinquish_alloc_permit`, `tcgen05.dealloc`, `tcgen05.ld`, `tcgen05.ld.red`, `tcgen05.st`, `tcgen05.commit`, `tcgen05.cp`, `tcgen05.shift`, `tcgen05.mma`, `tcgen05.mma.ws` | `sub_569180`, `sub_526370`, `sub_58C7F0`, `sub_574050`, `sub_578DB0`, `sub_571FE0`, `sub_56C190`, `sub_5427F0`, `sub_4F1A90`, `sub_5BBC30` (90KB), `sub_58FA20` |
| **TCGen05 guardrails** | `_tcgen05.guardrails.is_phase_valid`, `are_columns_allocated`, `is_current_warp_valid_owner`, `in_physical_bounds`, `allocation_granularity`, `datapath_alignment`, `sp_consistency_across_idesc_mod`, `check_sparse_usage` | per-instruction handlers |

### Numeric MMA Hash Table (at `a1+816`)

~400 entries where the key is a numeric string representation of a hash value (e.g., `"2644314910"`) that encodes a specific MMA shape/type/layout combination. The hash encodes the instruction variant completely: matrix dimensions (m16n8k16, m16n8k32, etc.), data type (f16, bf16, tf32, f32, f64, s8, u8, s4, u4, b1), and layout (row/col combinations). Each entry maps to a codegen handler function pointer. This avoids a multi-dimensional lookup by collapsing the full variant space into a single hash probe.

## Instruction Property Accessors

All codegen handlers query instruction properties through accessor functions on the instruction object at `a1+1096`. These are the same accessors used by WMMA, MMA, and tcgen05 codegen.

| Accessor | Purpose | Usage Example |
|---|---|---|
| `sub_70B6E0` | Check if feature enabled | `sub_70B6E0(obj)` -- boolean feature gate |
| `sub_70B780` | Get feature parameter | Numeric feature parameter |
| `sub_70FA00` | Check instruction capability for SM | `sub_70FA00(*, 23)` = texture, `sub_70FA00(*, 29)` = tcgen05 |
| `sub_70E940` | Get operand count | Number of operands |
| `sub_70E6E0` | Get data type | Operand data type enumeration |
| `sub_70ACC0` | Get accumulator type | MMA accumulator data type |
| `sub_709860` | Get register type/size | Register class and width |
| `sub_70F460` | Get layout variant | row/col matrix layout |
| `sub_707D60` | Check MMA shape variant | m16n16k16 vs m32n8k16, etc. |
| `sub_709910` | Check sparse mode | Sparse MMA variant flag |
| `sub_70F650` | Get matrix dimension (M/N) | Matrix size parameter |
| `sub_70F600` | Get matrix dimension (K) | Alternate dimension parameter |
| `sub_70CA60` | Get operand type by index | `sub_70CA60(*, 0)` -- type of first operand (21 = specific type, 58 = f32, 59 = f64) |
| `sub_70BA40` | Texture mode query | Texture sampling mode |
| `sub_70BD50` | Sampler mode query | Texture sampler configuration |
| `sub_70BB20` | Bulk tensor mode | cp.async.bulk.tensor transfer mode |
| `sub_70F0A0` | Get sparse metadata | Sparse matrix metadata parameter |

## Prototype Generator -- `sub_5FF700`

At 354KB, this is the single largest function in the intrinsic infrastructure. It takes an intrinsic case number (`a1`) and a buffer pointer (`a2`), allocates a buffer via `sub_4DA340(size, a2)`, fills it with a PTX prototype string via `strcpy()`, and returns the result. The output is a complete `.weak .func` PTX declaration that gets emitted into the PTX output stream so the linker can resolve calls to CUDA runtime helper functions.

The function is effectively a giant `switch(a1)` with one case per intrinsic. Each case contains an inline string literal with the full PTX function signature.

### Prototype Format

Every emitted prototype follows the same structure:

```
.weak .func (<return_params>) <intrinsic_name> (<input_params>)
```

Examples from the binary:

| Case | Prototype |
|---|---|
| 0 | `.weak .func (.reg .s32 %d) __cuda_sm20_div_s16 (.reg .s32 %a0, .reg .s32 %a1)` |
| 4 | `.weak .func (.reg .u64 %rdv1) __cuda_sm20_div_u64 (.reg .u64 %rda1, .reg .u64 %rda2)` |
| 9 | `.weak .func (.reg .f32 %fv1) __cuda_sm20_div_rn_f32 (.reg .f32 %fa1, .reg .f32 %fa2)` |
| 25 | `.weak .func (.reg .f64 %fdv1) __cuda_sm20_div_rn_f64_full (.reg .f64 %fda1, .reg .f64 %fda2)` |

The `.weak` linkage means these declarations are overridable: if the user provides their own implementation of `__cuda_sm20_div_s16`, the linker will use that instead of the built-in runtime implementation. This mechanism supports both default CUDA runtime math and user-supplied replacements.

### Register Naming Convention

The prototype register names encode the data type and role:

| Prefix | Meaning |
|---|---|
| `%d` | 32-bit integer return value |
| `%a0`, `%a1` | 32-bit integer input parameters |
| `%rdv1` | 64-bit integer return value |
| `%rda1`, `%rda2` | 64-bit integer input parameters |
| `%fv1` | f32 return value |
| `%fa1`, `%fa2` | f32 input parameters |
| `%fdv1` | f64 return value |
| `%fda1`, `%fda2` | f64 input parameters |

## Major Codegen Handlers

The four largest codegen handlers together represent ~500KB of code and cover the tensor core instruction families.

### `sub_5C7A50` -- WMMA.MMA Codegen (173KB)

The largest codegen handler. Generates inline PTX code for `wmma.mma` instructions across all variant combinations.

- Allocates a 50,000-byte buffer for code generation
- Covers shapes: m16n16k16, m32n8k16, m8n32k16
- Data types: f16, f32, bf16, tf32, s8, u8, s4, u4, b1
- Layouts: row/col for each of the A, B, C, D matrices (4 layout combinations)
- Satfinite variants for each configuration
- Address spaces: generic, global, shared

### `sub_5C10A0` -- MMA Codegen (120KB)

Handles the newer `mma.sync` API (non-WMMA). Covers the post-Volta PTX MMA instructions.

- Shapes: m8n8k4, m16n8k8, m16n8k16, m16n8k32, m16n8k64, m16n8k128, m16n8k256
- Types: f16, bf16, tf32, f32, f64, s8, u8, s4, u4, b1
- Sparse variants for sm_80+ and sm_90+ (structured sparsity 2:4)

### `sub_5BBC30` -- TCGen05.MMA Codegen (90KB)

Blackwell 5th-generation tensor core MMA code generation. Handles the `tcgen05.mma` instruction family introduced in sm_100.

- Allocates a 50,000-byte buffer
- Queries `sub_70FA00(*, 29)` to validate tcgen05 capability
- Handles standard, sparse, and warp-shared (`.ws`) variants
- Uses `sub_70F0A0` for sparse metadata parameter extraction
- Generates code for tcgen05-specific tensor memory addressing

### `sub_5B76D0` -- Division Codegen (64KB)

Generates inline PTX code for all `div` variants.

- Integer division: s16, s64, u16, u64
- Floating-point division: f32, f64 with all rounding modes (rn, rd, ru, rz)
- Flush-to-zero (ftz) variants for f32
- Checks operand type via `sub_70CA60(*(_QWORD *)(a1+1096), 0) == 21`
- Emits both fastpath and slowpath (Newton-Raphson) code sequences

## OCG Intrinsic System -- `sub_6C9EB0`

The OCG (Optimized Code Generation) intrinsic subsystem is a separate, parallel dispatch mechanism for SM100+ builtin operations. While the classical system at `sub_5D1660` maps CUDA runtime helper names to integer IDs, the OCG system maps `__nv_ptx_builtin_ocg_*` function names to type-specific handler functions that validate parameters and emit SASS instructions directly.

### Initialization

`sub_6C9EB0` initializes a 10,664-byte (0x29A8) lookup table and sets the vtable pointer to `off_202CF48`. The operation name prefix is stored at `*(_QWORD *)(a1 + 120) = "__nv_ptx_builtin_ocg_"`. The table contains 44 operations in 248-byte slots starting at offset 128. Each slot holds the operation name followed by up to 30 sub-operation/modifier string pointers (unused slots are NULL from the memset).

### OCG Builtin Name Table -- Complete (44 Operations)

The complete OCG builtin table extracted from `sub_6C9EB0`. Thirty numeric string pointers that IDA left unresolved were recovered by reading null-terminated strings from the ptxas binary at `addr - 0x400000` (ELF LOAD virtual address base). The table size 0x29A8 and 248-byte slot stride are verified against the `memset` in the decompiled code.

#### Arithmetic and ALU Operations

| Slot | Offset | OCG Name | Sub-Operations / Types | SASS Equivalent |
|---|---|---|---|---|
| 0 | 128 | `add` | s32, f32, s64, f64, sat | IADD3 / FADD |
| 28 | 7072 | `mnmx` | s32, u32, s64, u64 | IMNMX / FMNMX |
| 15 | 3848 | `viadd` | 32, f16x2 | VIADD |

#### Vector Integer Operations (SM100+ VIMNMX family)

All six vector integer operations share the same type set: s32, u32, s16x2, u16x2 with an optional `relu` modifier for ReLU clamping.

| Slot | Offset | OCG Name | SASS Equivalent | Description |
|---|---|---|---|---|
| 16 | 4096 | `viaddmax` | VIADDMNMX | fused add + max |
| 17 | 4344 | `viaddmin` | VIADDMNMX | fused add + min |
| 18 | 4592 | `vimax` | VIMNMX | vector integer max |
| 19 | 4840 | `vimin` | VIMNMX | vector integer min |
| 20 | 5088 | `vimax3` | VIMNMX3 | 3-way vector integer max |
| 21 | 5336 | `vimin3` | VIMNMX3 | 3-way vector integer min |

#### Packed Float Operations (f16x2 arithmetic)

All three packed operations share the same modifier set: `ftz` (flush-to-zero) and rounding modes `rn`, `rm`, `rp`, `rz`.

| Slot | Offset | OCG Name | SASS Equivalent | Description |
|---|---|---|---|---|
| 25 | 6328 | `fadd2` | HADD2 / FADD.PACKED | packed f16 addition |
| 26 | 6576 | `ffma2` | HFMA2 / FFMA.PACKED | packed f16 fused multiply-add |
| 27 | 6824 | `fmul2` | HMUL2 / FMUL.PACKED | packed f16 multiplication |
| 29 | 7320 | `fmax3` | FMNMX3 | 3-way float max (ftz, nan modifiers) |
| 30 | 7568 | `fmin3` | FMNMX3 | 3-way float min (ftz, nan modifiers) |

#### Async Copy and TMA Operations

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 1 | 376 | `cp_async_commit` | mem, bulk, shared, global | LDGDEPBAR |
| 2 | 624 | `cp_async_wait` | mem, bulk, shared, global, read, write | DEPBAR |
| 10 | 2608 | `cp_async_bulk` | mbarrier, counted, shared, global, multicast, sequenced, bytemask | UBLKCP |
| 11 | 2856 | `cp_red_async_bulk` | mbarrier, counted, shared, global; types: u32/s32/u64/s64/f16/f32/f32ftz/f64/bf16; ops: add/min/max/inc/dec/and/or/xor | UBLKCP.RED |
| 12 | 3104 | `cp_async_tensor` | mbarrier, shared, global, 1d/2d/3d/4d/5d, im2col, multicast | UTMAKCP |
| 13 | 3352 | `cp_async_prefetch_tensor` | global, 1d/2d/3d/4d/5d, im2col | UTMAPF |

Note: The SASS mnemonics `UBLKCP` and `UTMAKCP` do not appear as strings in the ptxas binary. These are SASS assembler-level names visible only in cuobjdump output; the OCG names (`cp_async_bulk`, `cp_async_tensor`) are the canonical internal form.

#### Load, Store, and Cache Operations

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 3 | 872 | `cache` | tensor, pf (prefetch), iv (invalidate), ivall (invalidate all) | CCTL / PREFETCH |
| 4 | 1120 | `ld_mc` | ops: add/min/max/f32add/and/or/xor; types: f16x2/f16x4/f16x8/bf16x2/bf16x4/bf16x8/f32/f32x2/f32x4/f64/u32/s32/s64/u64 | LDG.MC |
| 5 | 1368 | `ldc` | u32, u64 | LDC |
| 6 | 1616 | `s2r` | (none -- register 0-255) | S2R |
| 22 | 5584 | `write_async` | release; shared/global; gpu/sys/mmio; v2/v4; u8/s8/u16/s16/b32/b64/u32/f64 | STG.ASYNC |
| 23 | 5832 | `cctl_c` | ldc/ldcu, shallow/deep, iv/ivall | CCTL |

#### Async Reduction and Fence Operations

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 9 | 2360 | `red_async` | release; shared/global; gpu/sys/mmio; v2/v4; u32/s32/u64; add/min/max/inc/dec/and/or/xor | RED.ASYNC |
| 14 | 3600 | `fence_view_async` | all, global, shared, dshared, tensor | FENCE.VIEW.ASYNC |

#### Tensor Core Operations (Blackwell TC family)

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 31 | 7816 | `tcbar` | cta1/cta2, a1t0/a0tx, flush, multicast, b32 | TCBAR |
| 32 | 7880 | `mmareadshma` | (none) | LDSM variant |
| 33 | 8064 | `tccp` | 128dp256bit/4dp256bit/128dp128bit/2x64dp128bitlw02lw13/2x64dp128bitlw01lw23/4x32dp128bit/u4x16p64/u6x16p32; cta1/cta2; b32/b64 | TCCP |
| 34 | 8312 | `tcmma` | gdesc/tmem; h/i/q/o/mxq; cta1/cta2; ashift/scale/lutb; areuse/akeep/breuse/bkeep; ws; buffer0-3; 2x/4x/blockscale/impl; b32/b64/u32 | TCMMA |
| 35 | 8560 | `tcshift` | cta1/cta2, b32 | TCSHIFT |
| 37 | 9056 | `tcatomsws` | and/or/findandset/align/cas; cta1/cta2; b32/b64 | TCATOM.SWS |
| 38 | 9304 | `tcldsws` | cta1/cta2 | TCLD.SWS |
| 39 | 9552 | `tcstsws` | cta1/cta2; b32/b64 | TCST.SWS |

The `tcmma` operation at slot 34 is the primary Blackwell MMA instruction, successor to HMMA/IMMA/DMMA. Its sub-operations encode:
- **Descriptor mode**: `gdesc` (global descriptor via UR), `tmem` (tensor memory direct)
- **Input formats**: `h` (half/f16), `i` (integer), `q` (quarter/fp8), `o` (output descriptor), `mxq` (MX-format quarter for microscaled block-scaling)
- **Operand reuse**: `areuse`/`akeep` (A matrix), `breuse`/`bkeep` (B matrix) -- register reuse hints
- **Warp-shared**: `ws` -- warp-shared execution across 2 warps
- **Block scaling**: `blockscale` with `2x`/`4x` multipliers and `impl` (implementation-defined) -- FP4/FP6 microscaled format support
- **Buffers**: `buffer0`-`buffer3` -- double/quad buffering for pipelined execution

The SWS (Software Scoreboard) operations (`tcatomsws`, `tcldsws`, `tcstsws`) are a Blackwell synchronization mechanism for tensor core pipelines that replaces hardware scoreboards with software-managed tracking.

#### Tensor Memory Load/Store (Blackwell native)

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 42 | 10296 | `ldtm` | formats: 16dp128bit/16dp256bit/32dp32bit/16dp64bit/16dp32bitt0t15/16dp32bitt16t31/16dp32bit; scale: x1-x128; pack16bit; fused/stat; statistics: nan/max/maxabs/min/minabs; types: u32/s32/f32/b32; sparsity: sparsify/u2/spfactor2to4 | LDTM |
| 43 | 10544 | `sttm` | formats: (same 7 as ldtm); scale: x1-x128; expand16bit; fused; b32 | STTM |

The `ldtm`/`sttm` format strings encode the tensor memory data layout:
- `16dp128bit` -- 16 data-points, 128-bit total (e.g., 16x fp8)
- `16dp256bit` -- 16 data-points, 256-bit total (e.g., 16x fp16)
- `32dp32bit` -- 32 data-points, 32-bit total (e.g., 32x 1-bit)
- `16dp32bitt0t15` / `16dp32bitt16t31` -- 16 data-points in thread groups 0-15 / 16-31
- Scale factors `x1` through `x128` control the number of consecutive elements loaded
- `sparsify` and `spfactor2to4` enable structured 2:4 sparsity metadata generation
- `stat` with `nan`/`max`/`maxabs`/`min`/`minabs` enables online statistics collection during load

#### Synchronization and Control

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 7 | 1864 | `acqblk` | (none) | barrier acquire block |
| 8 | 2112 | `preexit` | (none) | EXIT.KEEPREFCOUNT |
| 24 | 6080 | `getnextworkid` | selfcast, broadcast | work distribution primitive |
| 36 | 8808 | `virtcount` | u32 | virtual warp counter |
| 40 | 9800 | `memclear` | b32, b64 | MEMCLEAR |
| 41 | 10048 | `acqshminit` | (none) | shared memory init barrier |

### Category Summary

| Category | Count | Operations |
|---|---|---|
| Arithmetic / ALU | 3 | add, mnmx, viadd |
| Packed float | 5 | fadd2, ffma2, fmul2, fmax3, fmin3 |
| Vector integer | 6 | viaddmax, viaddmin, vimax, vimin, vimax3, vimin3 |
| Async copy / TMA | 6 | cp_async_commit, cp_async_wait, cp_async_bulk, cp_red_async_bulk, cp_async_tensor, cp_async_prefetch_tensor |
| Load / store / cache | 6 | ld_mc, ldc, s2r, write_async, cctl_c, cache |
| Async reduction / fence | 2 | red_async, fence_view_async |
| Tensor core (TC) | 8 | tcbar, mmareadshma, tccp, tcmma, tcshift, tcatomsws, tcldsws, tcstsws |
| Tensor memory (TM) | 2 | ldtm, sttm |
| Sync / control | 6 | acqblk, preexit, getnextworkid, virtcount, memclear, acqshminit |
| **Total** | **44** | |

### Handler Functions

The OCG handler cluster at `0x6C0000`--`0x6CC000` contains ~25--30 specialized handler/validator functions. Each validates parameters, types, sub-operations, and memory domains before delegating to the SASS encoding engine.

| Address | Size | Handler | Confidence |
|---|---|---|---|
| `sub_6C0D90` | 19KB | Atomic reduction (atom.add/min/max/cas, scope, memory order, vector width) | 90% |
| `sub_6C1CF0` | 16KB | Mbarrier (arrive, wait, test, counted, bytemask variants) | 88% |
| `sub_6C2AE0` | 10KB | cp.async (basic async copy) | 85% |
| `sub_6C3470` | 20KB | cp.async.bulk (bulk async copy with type validation) | 85% |
| `sub_6C46B0` | -- | cp.red.async.bulk (bulk async reduction) | 85% |
| `sub_6C4DA0` | 15KB | Load/store (scope, memory order, domain validation) | 85% |
| `sub_6C5A40` | 8KB | Cache control (CCTL: shallow/deep, iv/ivall, ldc/ldcu) | 85% |
| `sub_6C60B0` | 7KB | Distributed shared memory (selfcast/broadcast) | 80% |
| `sub_6C8100` | 9KB | cp.async.tensor / TMA (1--5D, multicast, tile/im2col) | 85% |
| `sub_6C9BC0` | -- | Name resolver (operation name -> internal enum) | 80% |
| `sub_6CC690` | 22KB | Router (dispatches to type-specific handlers via vtable) | 80% |

### OCG Validation Strings

The OCG handlers share a consistent validation pattern. Notable error messages (NVIDIA consistently misspells "intrinsic" as "instrinsic" throughout the codebase):

| Error String | Handler | Meaning |
|---|---|---|
| `"Op {add, min, max, inc, dec, and, or, xor} not specified"` | Atomic | Missing reduction operation |
| `"Domain param '_shared' or '_global' required"` | Atomic/LS | No memory domain specified |
| `"Unsupported non _add global memory reduction"` | Atomic | Only `add` supported for global reductions |
| `"Deprecated scope without memory order semantics"` | Memory order | Legacy scope usage |
| `"Required scope with memory order semantics"` | Memory order | Missing scope on memory-ordered op |
| `"byte mask not allowed with counted"` | Mbarrier | Conflicting mbarrier modifiers |
| `"Exactly one of the 'shallow' or 'deep' modifiers must be used."` | CCTL | Missing cache depth modifier |
| `"Cannot use both the selfcast and the broadcast modifier."` | Dshmem | Conflicting multicast mode |
| `"Unexpected instrinsic name (%s)"` | Name resolver | Unknown OCG operation name |
| `"Unexpected instrinsic subop (%s)"` | Name resolver | Unknown sub-operation |
| `"Unexpected instrinsic type (%s) instead of (%s) in param (%d)"` | Type validator | Parameter type mismatch |
| `"LDC requires a constant/immediate bank number"` | LDC/S2R | Missing constant bank operand |
| `"S2R register must be between 0 and 255 inclusive"` | LDC/S2R | System register out of range |

### OCG SASS-Level Handlers

Separate from the validation layer, the SASS encoding zone at `0x6D0000`--`0x6E0000` contains MMA-specific handlers that operate during final instruction encoding:

| Address | Size | Handler | Confidence |
|---|---|---|---|
| `sub_6D4350` | 30KB | MMA intrinsic lowering (HMMA, IMMA, DMMA) | 90% |
| `sub_6D5CB0` | 16KB | MMA operand encoder (matrix fragments, accumulator registers) | 80% |
| `sub_6D7AF0` | 19KB | TCGen05 MMA handler (SM100 5th-gen tensor core encoding) | 90% |
| `sub_6D69B0` | 12KB | TCGen05 MMA validator (parameter validation only) | 80% |

Notable validation strings from the tcgen05 MMA handler:
- `"fused and l16dp32bit must be specified together"`
- `"Inputs vector length is inconsistent with layout and num modifiers"`

## Intrinsic Families by SM Generation

Each SM generation introduces new intrinsic families while preserving all earlier ones. The per-SM intrinsic table initializer functions (`sub_60AXXX` cluster, registered in Map 3 of the [capability dispatch](../targets/index.md)) control which intrinsics are available on each target.

### sm_20 -- Software IEEE Math (70 entries)

The foundation layer. 70 intrinsics providing IEEE-754-compliant software implementations of math operations that either lack hardware support or need exact rounding guarantees. All later SM targets inherit these.

- **Division**: `div_s16`, `div_u64`, `div_rn_f32`, `div_rn_f64_full`, etc. -- all rounding modes (rn/rd/ru/rz) and types (s16/s64/u16/u64/f32/f64)
- **Reciprocal**: `rcp_rn_f32`, `rcp_rn_f64`, etc. -- all rounding modes
- **Square root**: `sqrt_rn_f32`, `sqrt_rn_f64`, etc. -- all rounding modes
- **Double-precision sqrt**: `dsqrt_rn`, `dsqrt_rd`, `dsqrt_ru`, `dsqrt_rz`
- **Double-precision reciprocal sqrt**: `drsqrt_rn`
- **Bit extract/insert**: `bfe` (bit field extract), `bfi` (bit field insert)
- **Remainder**: `rem_s32`, `rem_u32`, `rem_s64`, `rem_u64`

Codegen handlers: `sub_5B76D0` (div, 64KB), `sub_5B0CD0` (rcp, 44KB), `sub_5B4040` (sqrt, 49KB).

### sm_3x -- Optimized Division (4 entries)

Four optimized division variants introduced on Kepler to improve throughput on common division patterns.

### sm_62 -- Integer Dot Product (2 entries)

`dp2a` and `dp4a` integer dot product intrinsics introduced on Pascal (GP10x). Software emulation of the hardware instructions added in sm_61/sm_62.

### sm_70 -- Volta Warp-Synchronous + WMMA (370 entries)

The largest single block. Volta introduced mandatory warp-synchronous programming with explicit sync masks and the first generation of tensor core (WMMA) instructions.

**Synchronization primitives:**
- `barrier_arrive` / `barrier_sync` / `barrier_red` (0--15, with/without count)
- `matchsync_all/any_b32/b64` with predicate variants
- `shflsync_bfly/down/idx/up` with predicate variants
- `votesync_all/any/ballot/uni`
- `warpsync`

**WMMA (Warp Matrix Multiply-Accumulate):**
- Shapes: m16n16k16, m32n8k16, m8n32k16
- Operations per shape: `load_a`, `load_b`, `load_c`, `store_d`, `mma`
- Layouts: row/col combinations for A and B matrices
- Types: f16, f32 (with satfinite optional)
- Address spaces: generic, global, shared

### sm_80 -- Ampere Extensions (14 entries)

- `createpolicy` for L2 cache management
- Extended MMA: bf16, tf32, s4, s8, b1 data types
- `mma_shfl` for direct register-to-register MMA shuffle

### sm_8x -- Direct MMA (39 entries)

39 `mma_*` intrinsics for sm_8x providing direct MMA operations bypassing the WMMA wrapper API.

### sm_9x -- Sub-Byte Sparse MMA (38 entries)

38 Hopper-era intrinsics for sub-byte sparse matrix operations: s4/u4 data types with structured sparsity (2:4 pattern) at shapes m16n8k32, m16n8k64, and m16n8k128.

### sm_10x -- Blackwell Tensor Memory + Guardrails (21 entries)

- 10 hmma/imma metadata + bit MMA intrinsics
- 11 tcgen05 guardrail trap intrinsics for debug validation of tensor memory operations

### sm_1xx -- Bulk Copy (18 entries)

18 bulk copy and `cp.async.bulk.tensor` intrinsics covering 1D through 5D tensor copies with tile and im2col addressing modes, both unicast and multicast variants.

## Intrinsic Lookup Flow

The lookup path from a function call in PTX source to the codegen handler follows this sequence:

```
PTX source: call.uni __cuda_sm70_warpsync, (%mask);
                    |
                    v
            sub_5D1660 hash map (a1+1064)
            key: "__cuda_sm70_warpsync"
            value: integer ID (within 0x89..0x1FA range)
                    |
                    v
            sub_5FF700 switch(ID)
            Emits: .weak .func __cuda_sm70_warpsync (.reg .u32 %a0)
                    |
                    v
            sub_5D4190 named opcode hash map (a1+808)
            key: PTX opcode (e.g., "shfl", "vote", "barrier")
            value: codegen handler function pointer
                    |
                    v
            Codegen handler (e.g., sub_5801D0 for "shfl")
            Queries instruction properties via sub_70XXXX accessors
            Generates inline PTX code into 50KB buffer
```

For OCG intrinsics on SM100+:

```
PTX source: call.uni __nv_ptx_builtin_ocg_tcmma, (%args...);
                    |
                    v
            sub_6C9EB0 OCG table lookup
            Strips "__nv_ptx_builtin_ocg_" prefix
            Looks up operation name in 10,664-byte table
                    |
                    v
            sub_6CC690 OCG router
            Dispatches to type-specific handler via vtable
                    |
                    v
            Handler (e.g., sub_6C8100 for tensor ops)
            Validates parameters, types, memory domains
            Reports errors via "Unexpected instrinsic..." strings
                    |
                    v
            SASS encoding (sub_6D9690, 94KB)
            Encodes validated intrinsic into binary SASS
```

## Per-SM Intrinsic Initializers

Each SM target has its own intrinsic table initializer function registered in Map 3 of the capability dispatch (`sub_607DB0`). These functions control which subset of the 608 intrinsics are available on each target.

| SM | Initializer | SM | Initializer |
|---|---|---|---|
| sm_75 | `sub_60A2E0` | sm_100 | `sub_60A910` |
| sm_80 | `sub_60A3E0` | sm_110 | `sub_60AA20` |
| sm_86 | `sub_60AC30` | sm_103 | `sub_60A700` |
| sm_87 | `sub_60AD30` | sm_120 | `sub_608DF0` |
| sm_88 | `sub_60AB30` | sm_121 | `sub_60A4E0` |
| sm_89 | `sub_60A810` | | |
| sm_90 | `sub_60A5F0` | | |

Sub-variants (e.g., sm_100a, sm_100f) share the same initializer as their base SM since they represent the same silicon with different feature exposure levels.

## Diagnostic Strings

| String | Location | Context |
|---|---|---|
| `"__nv_ptx_builtin_ocg_"` | `sub_6C9EB0` (0x6c9ecf) | OCG builtin name prefix |
| `"instrinsic"` (sic) | Multiple OCG handlers | Consistent NVIDIA typo for "intrinsic" |
| `".weak .func"` | `sub_5FF700` (354KB) | Prototype declaration prefix |
| `"__cuda_sm20_*"`, `"__cuda_sm70_*"`, etc. | `sub_5D1660` | Intrinsic name patterns in registration |
| `"__cuda_sanitizer_memcheck_*"` | `sub_5D1660` | Compute-sanitizer integration hooks |
| `"__cuda_sm10x_tcgen05_guardrail_trap_*"` | `sub_5D1660` | Blackwell debug trap intrinsics |
| `".RELU not allowed with unsigned type"` | `sub_6BEC60` | OCG LDC/S2R handler |

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_5D1660` | 46KB | Master intrinsic registration -- 608 name-to-ID entries | 99% |
| `sub_5D4190` | 41KB | Opcode dispatch -- ~120 named + ~400 MMA hash entries | 99% |
| `sub_5FF700` | 354KB | Prototype generator -- `.weak .func` PTX declarations | 99% |
| `sub_5C7A50` | 173KB | `wmma.mma` codegen (all shapes/types/layouts) | 98% |
| `sub_5C10A0` | 120KB | `mma` codegen (mma.sync API, post-Volta) | 98% |
| `sub_5BBC30` | 90KB | `tcgen05.mma` codegen (Blackwell 5th-gen tensor core) | 98% |
| `sub_5B76D0` | 64KB | `div` codegen (integer + FP, all rounding modes) | 95% |
| `sub_5ADDC0` | 50KB | `tex.grad` codegen (1D/2D/3D gradient textures) | 95% |
| `sub_5B4040` | 49KB | `sqrt` codegen (f32/f64, all rounding modes) | 95% |
| `sub_5AB460` | 45KB | `cp.async.bulk.tensor` codegen (1D--5D, tile/im2col) | 95% |
| `sub_5B0CD0` | 44KB | `rcp` codegen (f32/f64 reciprocal, all rounding modes) | 95% |
| `sub_6C9EB0` | 13KB | OCG intrinsic table init (`__nv_ptx_builtin_ocg_*`) | 95% |
| `sub_6CC690` | 22KB | OCG intrinsic router (vtable dispatch) | 80% |
| `sub_6C9BC0` | -- | OCG name resolver (op name -> enum) | 80% |
| `sub_6C0D90` | 19KB | OCG atomic/reduction handler | 90% |
| `sub_6C1CF0` | 16KB | OCG mbarrier handler | 88% |
| `sub_6C3470` | 20KB | OCG cp.async.bulk handler | 85% |
| `sub_6C4DA0` | 15KB | OCG load/store handler | 85% |
| `sub_6C5A40` | 8KB | OCG cache control handler | 85% |
| `sub_6C60B0` | 7KB | OCG distributed shared memory handler | 80% |
| `sub_6C8100` | 9KB | OCG cp.async.tensor / TMA handler | 85% |
| `sub_6D4350` | 30KB | MMA intrinsic lowering (SASS encoding) | 90% |
| `sub_6D7AF0` | 19KB | TCGen05 MMA handler (SASS encoding) | 90% |
| `sub_6D5CB0` | 16KB | MMA operand encoder | 80% |
| `sub_6D69B0` | 12KB | TCGen05 MMA validator | 80% |
| `sub_6BDE20` | 7KB | Intrinsic operand expansion | 88% |
| `sub_6BEC60` | 5.8KB | LDC/S2R intrinsic handlers | 90% |
| `sub_6A97B0` | 26KB | Intrinsic lowering main (switch-based) | 85% |

## Cross-References

- [SM Architecture Map](../targets/index.md) -- Per-SM capability dispatch tables and intrinsic initializer assignments
- [Math Intrinsics](math.md) -- Detailed coverage of sm_20 IEEE math intrinsic codegen (div, rcp, sqrt, rem)
- [Tensor Core Intrinsics](tensor.md) -- WMMA, MMA, WGMMA, tcgen05 instruction lowering
- [Sync & Warp Intrinsics](sync-warp.md) -- Barrier, vote, shuffle, match, redux intrinsics
- [Newton-Raphson Templates](../codegen/templates.md) -- Software math slowpath sequences used by div/rcp/sqrt
- [TCGen05 -- 5th Gen Tensor Cores](../targets/tcgen05.md) -- Blackwell tensor core ISA detail
- [Hash Tables & Bitvectors](../infra/hash-bitvector.md) -- Hash map infrastructure (`sub_425CA0` / `sub_426150` / `sub_426D60`)
- [Mercury Encoder](../codegen/mercury.md) -- Master SASS encoder `sub_6D9690` (94KB) that encodes validated intrinsics
- [SASS Instruction Encoding](../codegen/encoding.md) -- Instruction encoding infrastructure
- [Pipeline Overview](../pipeline/overview.md) -- OCG-time measurement covers intrinsic lowering
