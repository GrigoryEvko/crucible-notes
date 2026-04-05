# Intrinsic Table Architecture (607 Registered Entries)

ptxas maintains two separate intrinsic subsystems that together cover every CUDA runtime helper function, every PTX opcode requiring inline code generation, and every Blackwell+ OCG builtin operation. The first subsystem (`sub_5D1660` + `sub_5D4190` + `sub_5D7430` + `sub_5FF700`) handles 607 classical CUDA intrinsics and PTX opcode dispatch through a name-to-ID hash map, a body template name table, and a giant prototype generator. The second subsystem (`sub_6C9EB0` and its handler cluster at `0x6C0000`--`0x6CC000`) handles OCG (Optimized Code Generation) builtins for SM100+ targets. Both subsystems use the same hash map infrastructure (`sub_425CA0` / `sub_426150` / `sub_426D60`) documented in [Hash Tables & Bitvectors](../infra/hash-bitvector.md).

| | |
|---|---|
| **Master registration** | `sub_5D1660` (46KB) -- 607 CUDA intrinsics, name-to-integer-ID hash map (608 table slots, ID 0 = null) |
| **Opcode dispatch** | `sub_5D4190` (41KB) -- ~120 PTX opcodes to codegen handlers + ~400 MMA hash entries |
| **Body template names** | `sub_5D7430` (161KB) -- 1,079 intrinsic names constructed from .rodata prefixes + type suffixes, stored in hash map at +824 |
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
sub_451730 (intrinsic lowering context constructor)
  │
  ├── sub_5D4190(ctx)  ── register PTX opcode & MMA handlers ──────────────┐
  │     │ (1) Calls sub_5D1660 to populate intrinsic ID table (607 entries) │
  │     │ (2) Registers ~120 PTX opcode -> codegen handler mappings         │
  │     │ (3) Registers ~400 MMA hash -> codegen handler mappings           │
  │     │                                                                   │
  │     ├─ Hash map at +808  ── PTX opcode name -> codegen function ptr     │
  │     │    "div"     -> sub_5B76D0  (64KB)                                │
  │     │    "sqrt"    -> sub_5B4040  (49KB)                                │
  │     │    "wmma.mma"-> sub_5C7A50  (173KB)                               │
  │     │    "mma"     -> sub_5C10A0  (120KB)                               │
  │     │    ... ~116 more                                                  │
  │     │                                                                   │
  │     ├─ Hash map at +816  ── numeric MMA hash -> codegen handler ptr     │
  │     │    "2644314910" -> sub_4DDB80                                     │
  │     │    ... ~399 more (shape/type/layout combinations)                 │
  │     │                                                                   │
  │     └─ ID table at +1056 ── 9728-byte array (memcpy from unk_1D4D940)  │
  │        Hash map at +1064 ── name -> integer ID (sub_5D1660, 607)        │
  │        Count at +1072 = 608 (includes null ID 0 slot)                   │
  │                                                                         │
  ├── sub_4CE230(ctx)  ── register modifier keywords (GUARD, PRED, ...)     │
  │                                                                         │
  ├── sub_5D7430(ctx, sregs)  ── body template name table (161KB) ──────────┤
  │     │ 1,079 entries, each constructed from:                             │
  │     │   16-byte .rodata prefix (e.g. "__cuda_sm20_div_")               │
  │     │ + 4-byte type suffix (e.g. "s16\0", "u64\0", "rn_f")            │
  │     │ → registered into hash map at +824 with sequential integer IDs    │
  │     │                                                                   │
  │     └─ Hash map at +824  ── intrinsic name -> body template ID          │
  │          "__cuda_sm20_div_s16" -> 0                                     │
  │          "__cuda_sm20_div_u16" -> 1                                     │
  │          ... 1,079 total entries                                        │
  │                                                                         │
  └── sub_451330("<fermi macros>", ...)  ── load Fermi macro library        │
                                                                            │
sub_5FF700 (354KB) ─────────────────────────────────────────────────────────┘
  │ switch(body_template_id) with hundreds of cases
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

This 46KB function is the master catalog. It allocates a 9728-byte table (`memcpy` from `unk_1D4D940`, 0x2600 bytes = 608 x 16B slots), creates a hash map with initial capacity 0x80 via `sub_425CA0`, then calls `sub_426150(hashmap, "name", (char*)ID)` exactly 607 times to register every CUDA runtime helper function with an integer ID (IDs 1--607, contiguous). The hash map is stored at `a1+1064`, the table at `a1+1056`, and the count 608 at `a1+1072` (includes the unused null ID 0 slot).

### Complete ID Allocation

607 intrinsics are registered with contiguous IDs from `0x01` through `0x25F`. The binary stores count=608 at `a1+1072` because the pre-built 9,728-byte table (608 x 16B slots) includes a null ID 0 sentinel. The ID ranges partition cleanly by SM generation and functional category.

| ID Range | Count | Prefix | Category | SM Floor |
|---|---|---|---|---|
| `0x001`--`0x011` | 17 | `__cuda_reduxsync_*` | Redux sync (b32 and/or/xor, f32 max/min/abs/NaN, s32/u32 add/max/min) | sm_70 |
| `0x012`--`0x018` | 7 | `__cuda_sanitizer_memcheck_*` | Compute-sanitizer hooks (free, generic, global, local, malloc, readmetadata, shared) | -- |
| `0x019`--`0x01F` | 7 | `__cuda_scalar_video_emulation_*` | Video instruction emulation helpers | sm_20 |
| `0x020`--`0x02A` | 11 | `__cuda_sm10x_*` | Blackwell tcgen05 guardrail traps + create_mask helper | sm_100 |
| `0x02B`--`0x03C` | 18 | `__cuda_sm1xx_*` | Bulk copy + cp.async.bulk.tensor 1D--5D tile/im2col uni/multicast | sm_100+ |
| `0x03D`--`0x082` | 70 | `__cuda_sm20_*` | IEEE math: bfe, bfi, div, rcp, sqrt, dsqrt, drsqrt, rem (all rounding modes + slowpaths) | sm_20 |
| `0x083`--`0x086` | 4 | `__cuda_sm3x_div_*` | Optimized division variants (rn_ftz_f32, rn_noftz_f32 + slowpaths) | sm_30 |
| `0x087`--`0x088` | 2 | `__cuda_sm62_dp2a/dp4a` | Integer dot product emulation | sm_62 |
| `0x089`--`0x211` | 393 | `__cuda_sm70_*` | Volta+ intrinsics (barriers, shuffle, vote, match, WMMA -- all shapes, layouts, address spaces) | sm_70 |
| `0x212`--`0x214` | 3 | `__cuda_sm80_*` | Ampere: createpolicy_fractional, createpolicy_fractional_encode, createpolicy_range_encode | sm_80 |
| `0x215`--`0x21E` | 10 | `__cuda_sm_10x_*` | Blackwell hmma/imma mdata + bit MMA (and/xor m8n8k128/m16n8k128/m16n8k256) | sm_100 |
| `0x21F`--`0x22C` | 14 | `__cuda_sm_8x_*` | Direct MMA operations (f16/f32 accum, 4 layout combos) + mma_shfl_f16/f32 | sm_80+ |
| `0x22D`--`0x25F` | 51 | `__cuda_sm_9x_*` | Hopper sub-byte + bit MMA: s4/u4 dense m16n8k32/k64 + sparse m16n8k64/k128, bit xor (m8n8k128/m16n8k128/m16n8k256) | sm_90 |

**Total: 607 registered intrinsics across 13 prefix groups. Table has 608 slots (ID 0 unused).**

### sm_70 Intrinsic Breakdown (IDs `0x89`--`0x211`)

The sm_70 block is by far the largest at 393 entries. It covers every Volta-era warp synchronous intrinsic plus the complete WMMA API. The explosion in count comes from the combinatorial product of shapes, layouts, data types, address spaces, and predicate/satfinite variants.

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

## Body Template Name Table -- `sub_5D7430`

At 161KB of machine code (0x5D7430--0x5FF700), this is the largest function in the intrinsic infrastructure by code size and the 6th largest function in the entire ptxas binary. IDA failed to decompile it; all analysis comes from raw x86-64 disassembly. The function constructs a third hash map (at context offset +824 / `0x338`) containing 1,079 entries that map dynamically constructed `__cuda_*` intrinsic names to sequential body template IDs (0--1078).

### Why 1,079 Body Templates for 607 Logical Intrinsics

The master registration table (`sub_5D1660`) maps 607 intrinsic names to logical IDs. The body template table (`sub_5D7430`) maps 1,079 variant-specific names to prototype generator case numbers. The 1.78x expansion has one dominant cause: **WMMA template proliferation across GPU generations**.

The 204 logical WMMA entries in `sub_5D1660` cover only the original sm_70 Volta shapes (m16n16k16/m32n8k16/m8n32k16 with f16/f32 types). But the body template table includes all later-generation WMMA variants -- sm7x sub-byte/bit, sm72 integer, sm8x tf32/bf16/f64 -- that were added as hardware evolved. These ~416 extra WMMA templates have no matching entry in the 607 logical ID table; they exist only in the body template hash map and the prototype generator switch.

Non-WMMA intrinsics map approximately 1:1 between logical IDs and body templates. The math operations (div, rcp, sqrt) are already fully type-specialized at the logical level -- each rounding-mode/type combination is a separate logical intrinsic.

Three sources of expansion beyond the 607 logical entries:

1. **Later-generation WMMA variants** (~416 template-only entries):
   - sm7x sub-byte WMMA (s4/u4 m8n8k32) + bit WMMA (m8n8k128): ~231 templates
   - sm72 integer WMMA (m16n16k16/m32n8k16/m8n32k16 integer types): ~105 templates
   - sm8x tf32 WMMA (m16n16k8) + bf16/f64 WMMA: ~80 templates
2. **Aligned warp sync variants** (~13 extra templates): `matchsync_aligned`, `votesync_aligned`, `votesync_ballot_groupwise`, `query_activemask`/`query_activemask_groupwise` for cooperative group support
3. **Additional SM100 specializations** (~8 extra templates): `tcgen05_alloc_two_sm`, extra guardrails check variants, `get_warp_rank`

Conversely, 18 sm1xx bulk copy intrinsics have logical IDs but zero body templates -- they bypass the template/prototype mechanism entirely and are lowered directly to inline PTX by the opcode dispatch handlers (`sub_593210`, `sub_5AB460`).

### Template Distribution Table

| Logical Group | Logical | Template | Factor |
|---|---|---|---|
| SM20 IEEE math (div, rem, rcp, sqrt, bfe/bfi) | 70 | 70 | 1.0x |
| SM3x optimized division | 4 | 4 | 1.0x |
| SM62 integer dot product | 2 | 2 | 1.0x |
| SM70 barriers | 170 | 170 | 1.0x |
| SM70 warp sync (match, vote, shfl, query) | 19 | 32 | 1.7x |
| SM70 WMMA (f16/f32 original Volta) | 204 | 249 | 1.2x |
| SM7x WMMA extended (sub-byte, bit) | 0 | 231 | tmpl-only |
| SM72 WMMA (integer) | 0 | 105 | tmpl-only |
| SM8x WMMA (tf32, bf16, f64) | 0 | 80 | tmpl-only |
| SM80 cache policy | 3 | 4 | 1.3x |
| SM8x direct MMA | 14 | 14 | 1.0x |
| SM9x Hopper sub-byte/bit MMA | 51 | 52 | 1.0x |
| SM10x Blackwell MMA metadata | 10 | 10 | 1.0x |
| SM100 tcgen05 + guardrails | 11 | 19 | 1.7x |
| SM100+ bulk copy / TMA | 18 | 0 | (no templates) |
| Redux sync primitives | 17 | 17 | 1.0x |
| Compute-sanitizer hooks | 7 | 7 | 1.0x |
| Video instruction emulation | 7 | 7 | 1.0x |
| **Total** | **607** | **1,073** | **1.78x** |

WMMA subtotal: 204 logical entries expand to 665 body templates (3.3x). Non-WMMA: 403 logical entries map to 408 templates (~1.0x). The remaining 7 templates (1,080 prototype switch cases minus 1,073 classified) are sanitizer/cache variants where IDA produced `qmemcpy` instead of `strcpy`, preventing exact name extraction.

The sm70 WMMA group itself expands from 204 to 249 templates because the prototype generator includes `update_ptr` and `desc` (descriptor-based addressing) variants of certain load/store operations that the logical table does not separate.

The three "tmpl-only" WMMA rows (sm7x/sm72/sm8x) are the single largest contributor to the expansion. They represent ~416 templates with zero logical ID counterparts. These families use `.FORCE_INLINE .func` linkage in their prototypes instead of the `.weak .func` used by the original sm70 WMMA entries:

```
sm70 (original):   .weak .func (...) __cuda_sm70_wmma_m16n16k16_load_a_col (...)
sm72 (integer):    .FORCE_INLINE .func (...) __cuda_sm72_Integer_wmma_m16n16k16_load_a_row (...)
sm7x (sub-byte):   .FORCE_INLINE .func (...) __cuda_sm7x_sub_byte_wmma_m8n8k32_load_a_row (...)
sm8x (tf32):       .FORCE_INLINE .func (...) __cuda_sm8x_tf32_wmma_m16n16k8_load_a_row (...)
```

The `.FORCE_INLINE` directive forces inlining at every call site rather than emitting a separate callable function. The later-gen WMMA implementations are more complex and performance-sensitive, making per-call-site specialization profitable.

### Name Construction Algorithm

The function contains zero string references because it constructs all 1,079 names at runtime. For each entry:

1. **Allocate** a 20-byte buffer via `sub_424070(allocator, 20)`
2. **Copy prefix** (16 bytes) from `.rodata` via SSE `movdqa` + `movups` (e.g., `"__cuda_sm20_div_"`)
3. **Append suffix** (4 bytes) via `movl` immediate at offset +16 (e.g., `"s16\0"`, `"u64\0"`, `"rn_f"`)
4. **Register** via `sub_426150(context+824, buffer, template_id)` with sequential integer IDs

The 533 unique .rodata prefix addresses fan out through multiple suffixes per prefix:

```
.rodata prefix (16B)       suffix (4B)     result (20B buffer)
───────────────────────    ───────────     ──────────────────────
"__cuda_sm20_div_"    +    "s16\0"    =   "__cuda_sm20_div_s16"
"__cuda_sm20_div_"    +    "u16\0"    =   "__cuda_sm20_div_u16"
"__cuda_sm20_div_"    +    "u64\0"    =   "__cuda_sm20_div_u64"
"__cuda_sm20_div_"    +    "s64\0"    =   "__cuda_sm20_div_s64"
"__cuda_sm20_div_"    +    "rn_f"     =   "__cuda_sm20_div_rn_f" (truncated)
"__cuda_sm20_rem_"    +    "s16\0"    =   "__cuda_sm20_rem_s16"
"__cuda_sm20_rcp_"    +    "rn_f"     =   "__cuda_sm20_rcp_rn_f" (truncated)
"__cuda_sm70_barr"    +    "ier_"     =   "__cuda_sm70_barrier_" (prefix chain)
```

Names truncated at the 20-byte buffer limit are still sufficient for hash map lookup -- the full untruncated name appears only inside the prototype string in `sub_5FF700`.

### Worked Example: Division (Cases 0--26)

The `__cuda_sm20_div` operation illustrates the template-to-prototype mapping. Division has 19 logical IDs and 19 body templates (1:1 ratio) because each type/rounding/precision variant is already a separate logical intrinsic. The suffix encodes the type specialization:

| Case | Body Template Name | Type Suffix | PTX Signature |
|---|---|---|---|
| 0 | `__cuda_sm20_div_s16` | `s16` | `(.reg .s32 %d) ... (.reg .s32 %a0, .reg .s32 %a1)` |
| 1 | `__cuda_sm20_div_u16` | `u16` | `(.reg .u32 %d) ... (.reg .u32 %a0, .reg .u32 %a1)` |
| 4 | `__cuda_sm20_div_u64` | `u64` | `(.reg .u64 %rdv1) ... (.reg .u64 %rda1, .reg .u64 %rda2)` |
| 5 | `__cuda_sm20_div_s64` | `s64` | `(.reg .u64 %rdv1) ... (.reg .u64 %rda1, .reg .u64 %rda2)` |
| 9 | `__cuda_sm20_div_rn_f32` | `rn_f` | `(.reg .f32 %fv1) ... (.reg .f32 %fa1, .reg .f32 %fa2)` |
| 10 | `__cuda_sm20_div_rd_f32` | `rd_f` | `(.reg .f32 %fv1) ... (.reg .f32 %fa1, .reg .f32 %fa2)` |
| 14 | `__cuda_sm20_div_rn_ftz_f32` | `rn_f` | `(.reg .f32 %fv1) ... (.reg .f32 %fa1, .reg .f32 %fa2)` |
| 22 | `__cuda_sm20_div_ru_f64_v2` | `ru_f` | `(.reg .f64 %fdv1) ... (.reg .f64 %fda1, .reg .f64 %fda2)` |
| 25 | `__cuda_sm20_div_rn_f64_full` | `rn_f` | `(.reg .f64 %fdv1) ... (.reg .f64 %fda1, .reg .f64 %fda2)` |

Cases 2--3 (rem s16/u16), 6--7 (rem s64/u64) are interleaved between the division entries. Cases 8, 13 are `_slowpath` variants that implement Newton-Raphson refinement fallbacks. Cases 18--21 are the sm3x-optimized division variants with the same suffix scheme. Note: s16/u16 division uses `.s32`/`.u32` register types because PTX has no 16-bit register class; the 16-bit operation is performed by 32-bit hardware with appropriate sign/zero extension.

### Statistics

| Metric | Value |
|---|---|
| Machine code size | 164,560 bytes (0x5D7430--0x5FF700) |
| `sub_426150` calls | 1,079 |
| Unique .rodata prefix addresses | 533 |
| Hash map destination | context+824 (0x338) |
| Buffer size per entry | 20 bytes |
| IDA decompilation | Failed (function too large/repetitive) |

### Context Hash Map Summary

The intrinsic lowering context object holds five hash maps and one flat table:

| Offset | Field | Builder | Contents | Entries |
|---|---|---|---|---|
| +808 | opcode handlers | `sub_5D4190` | PTX opcode name -> codegen fn ptr | ~120 |
| +816 | MMA hash handlers | `sub_5D4190` | numeric hash -> codegen fn ptr | ~400 |
| +824 | **body templates** | **`sub_5D7430`** | **intrinsic name -> template ID** | **1,079** |
| +1056 | descriptor table | `sub_5D1660` | 608 x 16B intrinsic descriptor slots | 608 |
| +1064 | ID map | `sub_5D1660` | intrinsic name -> logical ID (1-607) | 607 |
| +1072 | count | `sub_5D1660` | 608 (includes null slot 0) | -- |

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

### OCG Intrinsic Lowering Pipeline -- `sub_6A97B0` + `sub_6CC690`

The full end-to-end flow that takes a PTX `call.uni __nv_ptx_builtin_ocg_*` intrinsic and produces a binary SASS instruction passes through five stages. Three are data-structure manipulation (matching, cleanup), two are instruction encoding (operand assembly, SASS emission).

```
sub_6B5F30 (intrinsic lowering driver)
  |
  ├─ sub_6B40C0 ── pre-processing
  |
  ├─ sub_6A97B0 (LowerIntrinsicOp, 26KB) ──────────────────────────────┐
  |     │                                                               |
  |     │ Phase 1: SASS instruction matching                            |
  |     │   For each intrinsic call node in linked list [a1+32..a1+40): |
  |     │     Walk operand tree at node+288                             |
  |     │     For each leaf: read instruction ID at leaf+24             |
  |     │     Search RB-tree at context+760 for matching SASS defn      |
  |     │     On match: store ptr at node+464, back-link at SASS+440    |
  |     │                                                               |
  |     │ Phase 2: Unmatched node garbage collection                    |
  |     │   If node+464 == 0 (no SASS match):                          |
  |     │     Walk use-def chain at node+40..48                         |
  |     │     Delete matching RB-tree entries (full rebalance via       |
  |     │       sub_6A92E0)                                             |
  |     │     Unlink node from work list                                |
  |     │     Release internal resources (operands, types)              |
  |     │     Return node to free pool at a1+80                         |
  |     │                                                               |
  |     │ Phase 3: Secondary cleanup (re-scan remaining nodes)          |
  |     │   Nodes with SASS match but no definition link:               |
  |     │     Clear back-pointer, clean up, recycle to free pool        |
  |     │                                                               |
  |     │ Key data: context+760 = RB-tree root (SASS instruction defs)  |
  |     │           context+768/776 = min/max pointers                  |
  |     │           context+784 = tree node count                       |
  |     │           context+792 = tree free list                        |
  |                                                                     |
  ├─ (post-processing: sub_693D00 per remaining node) ─────────────────┘
  |
  v
sub_6D9690 (master SASS encoder, 94KB)
  |
  ├─ sub_6D9290 (OCG vtable entry point) ────────────────────────────────┐
  |     │                                                                |
  |     │ 1. Extract intrinsic name from IR node                         |
  |     │ 2. Call sub_6C9BC0(this+120, name)  ── ParseOCGBuiltinName     |
  |     │      Strips "__nv_ptx_builtin_ocg_" prefix                     |
  |     │      Iterates 43 operation slots (248B each) in OCG table      |
  |     │      Matches operation name, then parses '_'-delimited sub-ops |
  |     │      Output: this+10688 = operation enum (0..42)               |
  |     │              this+10704 = int[] of sub-op indices              |
  |     │              this+10712 = sub-op count                         |
  |     │ 3. Fall through to sub_6D8B20 for secondary dispatch           |
  |                                                                      |
  ├─ sub_6CC690 (OCGRouter, 22KB) ──────────────────────────────────────┘
  |     │
  |     │ Input: (self, instruction_node, sass_descriptor)
  |     │
  |     │ 1. Read SASS opcode from descriptor+8
  |     │ 2. Read target profile from context+1584
  |     │    Key profile fields:
  |     │      +503  = operand decode flag
  |     │      +1012 = target SM enum
  |     │      +1020 = extended address mode
  |     │      +1021 = barrier mode
  |     │      +1041 = memory order capabilities bitmask
  |     │
  |     │ 3. Vtable dispatch (off_202CF48):
  |     │    vtable[2]  = OpcodeValidator   (default: sub_6BC1D0)
  |     │    vtable[24] = ScopeValidator    (default: sub_6BCE50)
  |     │    vtable[25] = MemOrderValidator (default: sub_6BBEC0)
  |     │    Each is compared by address; if overridden, calls the
  |     │    custom validator; if default, uses inline fast-path.
  |     │
  |     │ 4. Opcode-range dispatch (descriptor+8):
  |     │    178..189: Memory ops (ld_mc, st)  -> SASS enum 243/245/247
  |     │    416..420, 434: Reduction/atomic    -> SASS enum 243/246/261
  |     │    445..448: Barrier/fence            -> memory op path
  |     │    467: cp.async.tensor/special       -> SASS enum 70 or 257
  |     │    default: zero-init modifiers, use raw descriptor
  |     │
  |     │ 5. Operand assembly into v134[] (312-byte buffer):
  |     │    sub_6CAFD0: decode src/dst registers -> v134[8..10]
  |     │    sub_6CAE80: encode uniform operands  -> v134[16]
  |     │    sub_6CAF50: encode scope/mem-order   -> v134[13]
  |     │    sub_6CBA50: encode barrier level     -> v134[26..28]
  |     │
  |     │ 6. Build control words:
  |     │    v134[26] = 0x60000000 | modifier_bits
  |     │    v134[27] = 0x60000000 | ordering | barrier | write_mask
  |     │    v134[28] = 0x60000000 | scope_flags | 0x81000
  |     │
  |     v
  sub_6CB8A0 (EmitSASS)
        │
        │ Input: (self, sass_opcode_enum, instr_node, v134[], flags...)
        │ 1. Read SM version from profile+372 (>> 12)
        │ 2. sub_6CB4B0: final operand validation
        │ 3. sub_C3F490(opcode, ...): look up SASS encoding template
        │ 4. Encode instruction word from template + v134[] operands
        │ 5. sub_9253C0: commit encoded instruction to output
```

**Internal SASS opcode enum values** assigned by the router (not binary SASS opcodes -- these are routing keys that `sub_C3F490` maps to encoding templates):

| Enum | Hex | Meaning |
|---|---|---|
| 70 | `0x46` | Memory-ordered load/store/atomic (with barrier) |
| 243 | `0xF3` | Default memory operation |
| 245 | `0xF5` | Load variant (LD/LDG/LDS) |
| 246 | `0xF6` | Reduction/atomic default |
| 247 | `0xF7` | Fenced memory operation (LDGSTS) |
| 257 | `0x101` | Async copy without memory order |
| 261 | `0x105` | Atomic with pre-existing value read |

**Operand buffer layout** (v134[], 39 QWORDs passed to `sub_6CB8A0`):

| Slot | Content |
|---|---|
| 0--3 | Reserved (zero) |
| 4 | Barrier register (`0x90000000 \| reg`) |
| 5--7 | Extra source operands (from instruction node) |
| 8--10 | Primary operands (from `sub_6CAFD0` decode) |
| 11 | Secondary operand (LDC, conditional loads) |
| 12 | Predicate thread operand |
| 13 | Scope / memory-order (from `sub_6CAF50`) |
| 14 | Cache mode operand |
| 15 | Memory fence operand |
| 16 | Uniform / extended operand (from `sub_6CAE80`) |
| 17 | Memory ordering constant / barrier tracking |
| 19--21 | Source address (bulk/tensor ops) |
| 22--24 | Destination address (bulk/tensor ops) |
| 25 | Extra predicate (opcode 187 only) |
| 26 | Control word 0: `0x60000000 \| modifier_bits` |
| 27 | Control word 1: `0x60000000 \| ordering \| flags` |
| 28 | Control word 2: `0x60000000 \| scope \| 0x81000` |
| 29 | Write mask operand (conditional) |

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

### sm_70 -- Volta Warp-Synchronous + WMMA (393 entries)

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

### sm_80 -- Ampere Cache Policy (3 entries)

Three `createpolicy` intrinsics for L2 cache management: `createpolicy_fractional`, `createpolicy_fractional_encode`, `createpolicy_range_encode`.

### sm_10x -- Blackwell MMA Metadata + Bit MMA (10 entries)

10 `hmma_mdata`/`imma_mdata` + bit MMA intrinsics for sm_100: metadata variants at m16n8k16/k32/k64 shapes, and 1-bit AND/XOR MMA at m8n8k128/m16n8k128/m16n8k256.

### sm_8x -- Direct MMA (14 entries)

14 `mma_*` intrinsics for sm_8x: 12 direct MMA operations (f16/f32 accumulator x 4 layout combinations of col/row for A and B) plus `mma_shfl_f16` and `mma_shfl_f32` for register-to-register MMA shuffle.

### sm_9x -- Sub-Byte + Bit MMA (51 entries)

51 Hopper-era intrinsics: 3 bit-XOR MMA (m8n8k128/m16n8k128/m16n8k256), 24 dense sub-byte MMA (s4/u4 at m16n8k32/m16n8k64/m8n8k32, with satfinite), 8 sparse m16n8k128, and 16 sparse m16n8k64 (with _0/_1 split variants and satfinite).

### sm_10x (via `__cuda_sm10x_*`) -- Blackwell Tensor Memory + Guardrails (11 entries)

- 1 `create_mask_from_bit_idx_and_alloc_size_v1` helper
- 10 `tcgen05_guardrail_trap_*` intrinsics for debug validation of tensor memory operations

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
            value: integer ID (within 0x89..0x211 range)
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
            sub_6A97B0 (LowerIntrinsicOp, 26KB)
            Matches call node to SASS instruction via RB-tree at ctx+760
            Garbage-collects unmatched nodes
                    |
                    v
            sub_6D9290 -> sub_6C9BC0 (ParseOCGBuiltinName)
            Strips "__nv_ptx_builtin_ocg_" prefix
            Parses op name + sub-ops from 43-slot table (sub_6C9EB0)
                    |
                    v
            sub_6CC690 (OCGRouter, 22KB)
            Vtable dispatch: validates opcode, scope, memory order
            Decodes operands into 312-byte buffer via sub_6CAFD0 cluster
            Builds control words (0x60000000 | modifier_bits)
                    |
                    v
            sub_6CB8A0 (EmitSASS)
            Looks up encoding template via sub_C3F490(sass_opcode_enum)
            Encodes instruction word, commits to output via sub_9253C0
```

See [OCG Intrinsic Lowering Pipeline](#ocg-intrinsic-lowering-pipeline----sub_6a97b0--sub_6cc690) for the full five-stage breakdown with operand buffer layout and internal SASS opcode enum values.

## Per-SM Intrinsic Initializers

Each SM target has its own intrinsic table initializer function registered in Map 3 of the capability dispatch (`sub_607DB0`). These functions control which subset of the 607 intrinsics are available on each target.

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
| `sub_5D1660` | 46KB | Master intrinsic registration -- 607 name-to-ID entries (608 table slots) | 99% |
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
| `sub_6CC690` | 22KB | OCG router -- vtable-dispatched operand assembly and SASS emission | 90% |
| `sub_6C9BC0` | -- | OCG name parser -- decomposes `__nv_ptx_builtin_ocg_X_Y_Z` into enum + sub-op array | 95% |
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
| `sub_6A97B0` | 26KB | LowerIntrinsicOp -- SASS matching and unmatched-node GC | 90% |
| `sub_6B5F30` | -- | Intrinsic lowering driver (calls `sub_6B40C0` then `sub_6A97B0`) | 90% |
| `sub_6D9290` | -- | OCG vtable entry point (calls `sub_6C9BC0` then `sub_6D8B20`) | 85% |
| `sub_6CB8A0` | -- | SASS instruction emitter (template lookup via `sub_C3F490`) | 80% |
| `sub_6CAFD0` | -- | Operand decoder (registers into v134[] slots) | 85% |
| `sub_6CAE80` | -- | Uniform operand encoder | 85% |
| `sub_6CAF50` | -- | Scope / memory-order encoder | 85% |
| `sub_6CBA50` | -- | Barrier-level encoder | 85% |
| `sub_6CB4B0` | -- | Operand validator (called by `sub_6CB8A0`) | 80% |
| `sub_6A92E0` | -- | RB-tree fixup (rotation/recolor after deletion) | 90% |
| `sub_6BC1D0` | -- | Default opcode validator (vtable[2] of `off_202CF48`) | 90% |
| `sub_6BCE50` | -- | Default scope validator (vtable[24]) | 90% |
| `sub_6BBEC0` | -- | Default memory-order validator (vtable[25]) | 90% |

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

## Appendix: Complete Intrinsic Name Catalog (607 Entries)

Every intrinsic registered by `sub_5D1660`, extracted from the decompiled source. IDs are contiguous 1--607 (0x001--0x25F). The suffix after stripping the prefix encodes the operation, data type, rounding mode, address space, and optional modifiers.

### `__cuda_reduxsync_*` -- Redux sync (17 entries, `0x001`--`0x011`, sm_70)

| ID | Hex | Name |
|---|---|---|
| 1 | `0x001` | `__cuda_reduxsync_b32_and` |
| 2 | `0x002` | `__cuda_reduxsync_b32_or` |
| 3 | `0x003` | `__cuda_reduxsync_b32_xor` |
| 4 | `0x004` | `__cuda_reduxsync_f32_max` |
| 5 | `0x005` | `__cuda_reduxsync_f32_max_NaN` |
| 6 | `0x006` | `__cuda_reduxsync_f32_max_abs` |
| 7 | `0x007` | `__cuda_reduxsync_f32_max_abs_NaN` |
| 8 | `0x008` | `__cuda_reduxsync_f32_min` |
| 9 | `0x009` | `__cuda_reduxsync_f32_min_NaN` |
| 10 | `0x00A` | `__cuda_reduxsync_f32_min_abs` |
| 11 | `0x00B` | `__cuda_reduxsync_f32_min_abs_NaN` |
| 12 | `0x00C` | `__cuda_reduxsync_s32_add` |
| 13 | `0x00D` | `__cuda_reduxsync_s32_max` |
| 14 | `0x00E` | `__cuda_reduxsync_s32_min` |
| 15 | `0x00F` | `__cuda_reduxsync_u32_add` |
| 16 | `0x010` | `__cuda_reduxsync_u32_max` |
| 17 | `0x011` | `__cuda_reduxsync_u32_min` |

### `__cuda_sanitizer_memcheck_*` -- Compute-sanitizer hooks (7 entries, `0x012`--`0x018`, --)

| ID | Hex | Name |
|---|---|---|
| 18 | `0x012` | `__cuda_sanitizer_memcheck_free` |
| 19 | `0x013` | `__cuda_sanitizer_memcheck_generic` |
| 20 | `0x014` | `__cuda_sanitizer_memcheck_global` |
| 21 | `0x015` | `__cuda_sanitizer_memcheck_local` |
| 22 | `0x016` | `__cuda_sanitizer_memcheck_malloc` |
| 23 | `0x017` | `__cuda_sanitizer_memcheck_readmetadata` |
| 24 | `0x018` | `__cuda_sanitizer_memcheck_shared` |

### `__cuda_scalar_video_emulation_*` -- Video emulation (7 entries, `0x019`--`0x01F`, sm_20)

| ID | Hex | Name |
|---|---|---|
| 25 | `0x019` | `__cuda_scalar_video_emulation_operandExtractAndSignExtend01` |
| 26 | `0x01A` | `__cuda_scalar_video_emulation_operandExtractAndSignExtend11` |
| 27 | `0x01B` | `__cuda_scalar_video_emulation_operandExtractAndSignExtend12` |
| 28 | `0x01C` | `__cuda_scalar_video_emulation_operandExtractAndSignExtend22` |
| 29 | `0x01D` | `__cuda_scalar_video_emulation_optionalMerge32` |
| 30 | `0x01E` | `__cuda_scalar_video_emulation_saturate64` |
| 31 | `0x01F` | `__cuda_scalar_video_emulation_secondOp64` |

### `__cuda_sm10x_*` -- Blackwell tcgen05 guardrails + mask (11 entries, `0x020`--`0x02A`, sm_100)

| ID | Hex | Name |
|---|---|---|
| 32 | `0x020` | `__cuda_sm10x_create_mask_from_bit_idx_and_alloc_size_v1` |
| 33 | `0x021` | `__cuda_sm10x_tcgen05_guardrail_trap_access_out_of_physical_bounds` |
| 34 | `0x022` | `__cuda_sm10x_tcgen05_guardrail_trap_allocation_granularity_invalid` |
| 35 | `0x023` | `__cuda_sm10x_tcgen05_guardrail_trap_col_being_dealloced_not_returned_by_alloc` |
| 36 | `0x024` | `__cuda_sm10x_tcgen05_guardrail_trap_current_warp_owner_invalid` |
| 37 | `0x025` | `__cuda_sm10x_tcgen05_guardrail_trap_invalid_datapath_alignment` |
| 38 | `0x026` | `__cuda_sm10x_tcgen05_guardrail_trap_phase_invalid_during_alloc` |
| 39 | `0x027` | `__cuda_sm10x_tcgen05_guardrail_trap_sp_used_in_unsupported_env` |
| 40 | `0x028` | `__cuda_sm10x_tcgen05_guardrail_trap_sparse_mismatch_between_idesc_mod` |
| 41 | `0x029` | `__cuda_sm10x_tcgen05_guardrail_trap_unallocated_columns_access` |
| 42 | `0x02A` | `__cuda_sm10x_tcgen05_guardrail_trap_unallocated_columns_being_dealloced` |

### `__cuda_sm1xx_*` -- Bulk copy + cp.async.bulk.tensor (18 entries, `0x02B`--`0x03C`, sm_100+)

| ID | Hex | Name |
|---|---|---|
| 43 | `0x02B` | `__cuda_sm1xx_bulk_copy_multicast` |
| 44 | `0x02C` | `__cuda_sm1xx_bulk_copy_unicast` |
| 45 | `0x02D` | `__cuda_sm1xx_cp_async_bulk_tensor_1d_tile_multicast` |
| 46 | `0x02E` | `__cuda_sm1xx_cp_async_bulk_tensor_1d_tile_unicast` |
| 47 | `0x02F` | `__cuda_sm1xx_cp_async_bulk_tensor_2d_tile_multicast` |
| 48 | `0x030` | `__cuda_sm1xx_cp_async_bulk_tensor_2d_tile_unicast` |
| 49 | `0x031` | `__cuda_sm1xx_cp_async_bulk_tensor_3d_im2col_multicast` |
| 50 | `0x032` | `__cuda_sm1xx_cp_async_bulk_tensor_3d_im2col_unicast` |
| 51 | `0x033` | `__cuda_sm1xx_cp_async_bulk_tensor_3d_tile_multicast` |
| 52 | `0x034` | `__cuda_sm1xx_cp_async_bulk_tensor_3d_tile_unicast` |
| 53 | `0x035` | `__cuda_sm1xx_cp_async_bulk_tensor_4d_im2col_multicast` |
| 54 | `0x036` | `__cuda_sm1xx_cp_async_bulk_tensor_4d_im2col_unicast` |
| 55 | `0x037` | `__cuda_sm1xx_cp_async_bulk_tensor_4d_tile_multicast` |
| 56 | `0x038` | `__cuda_sm1xx_cp_async_bulk_tensor_4d_tile_unicast` |
| 57 | `0x039` | `__cuda_sm1xx_cp_async_bulk_tensor_5d_im2col_multicast` |
| 58 | `0x03A` | `__cuda_sm1xx_cp_async_bulk_tensor_5d_im2col_unicast` |
| 59 | `0x03B` | `__cuda_sm1xx_cp_async_bulk_tensor_5d_tile_multicast` |
| 60 | `0x03C` | `__cuda_sm1xx_cp_async_bulk_tensor_5d_tile_unicast` |

### `__cuda_sm20_*` -- IEEE math (70 entries, `0x03D`--`0x082`, sm_20)

| ID | Hex | Name |
|---|---|---|
| 61 | `0x03D` | `__cuda_sm20_bfe_s64_` |
| 62 | `0x03E` | `__cuda_sm20_bfe_u64_` |
| 63 | `0x03F` | `__cuda_sm20_bfi_u64_` |
| 64 | `0x040` | `__cuda_sm20_dblrcp_rn_slowpath_v3` |
| 65 | `0x041` | `__cuda_sm20_div_rd_f32` |
| 66 | `0x042` | `__cuda_sm20_div_rd_f64_v2` |
| 67 | `0x043` | `__cuda_sm20_div_rd_ftz_f32` |
| 68 | `0x044` | `__cuda_sm20_div_rn_f32` |
| 69 | `0x045` | `__cuda_sm20_div_rn_f64_fast` |
| 70 | `0x046` | `__cuda_sm20_div_rn_f64_full` |
| 71 | `0x047` | `__cuda_sm20_div_rn_ftz_f32` |
| 72 | `0x048` | `__cuda_sm20_div_rn_ftz_f32_slowpath` |
| 73 | `0x049` | `__cuda_sm20_div_rn_noftz_f32_slowpath` |
| 74 | `0x04A` | `__cuda_sm20_div_ru_f32` |
| 75 | `0x04B` | `__cuda_sm20_div_ru_f64_v2` |
| 76 | `0x04C` | `__cuda_sm20_div_ru_ftz_f32` |
| 77 | `0x04D` | `__cuda_sm20_div_rz_f32` |
| 78 | `0x04E` | `__cuda_sm20_div_rz_f64_v2` |
| 79 | `0x04F` | `__cuda_sm20_div_rz_ftz_f32` |
| 80 | `0x050` | `__cuda_sm20_div_s16` |
| 81 | `0x051` | `__cuda_sm20_div_s64` |
| 82 | `0x052` | `__cuda_sm20_div_u16` |
| 83 | `0x053` | `__cuda_sm20_div_u64` |
| 84 | `0x054` | `__cuda_sm20_drsqrt_f64_slowpath_v2` |
| 85 | `0x055` | `__cuda_sm20_drsqrt_f64_v2` |
| 86 | `0x056` | `__cuda_sm20_dsqrt_rd_f64` |
| 87 | `0x057` | `__cuda_sm20_dsqrt_rn_f64_mediumpath_v1` |
| 88 | `0x058` | `__cuda_sm20_dsqrt_rn_f64_v3` |
| 89 | `0x059` | `__cuda_sm20_dsqrt_ru_f64` |
| 90 | `0x05A` | `__cuda_sm20_dsqrt_rz_f64` |
| 91 | `0x05B` | `__cuda_sm20_rcp_f64_v3` |
| 92 | `0x05C` | `__cuda_sm20_rcp_rd_f32` |
| 93 | `0x05D` | `__cuda_sm20_rcp_rd_f32_slowpath` |
| 94 | `0x05E` | `__cuda_sm20_rcp_rd_f64` |
| 95 | `0x05F` | `__cuda_sm20_rcp_rd_ftz_f32` |
| 96 | `0x060` | `__cuda_sm20_rcp_rd_ftz_f32_slowpath` |
| 97 | `0x061` | `__cuda_sm20_rcp_rn_f32` |
| 98 | `0x062` | `__cuda_sm20_rcp_rn_f32_slowpath` |
| 99 | `0x063` | `__cuda_sm20_rcp_rn_ftz_f32` |
| 100 | `0x064` | `__cuda_sm20_rcp_rn_ftz_f32_slowpath` |
| 101 | `0x065` | `__cuda_sm20_rcp_ru_f32` |
| 102 | `0x066` | `__cuda_sm20_rcp_ru_f32_slowpath` |
| 103 | `0x067` | `__cuda_sm20_rcp_ru_f64` |
| 104 | `0x068` | `__cuda_sm20_rcp_ru_ftz_f32` |
| 105 | `0x069` | `__cuda_sm20_rcp_ru_ftz_f32_slowpath` |
| 106 | `0x06A` | `__cuda_sm20_rcp_rz_f32` |
| 107 | `0x06B` | `__cuda_sm20_rcp_rz_f32_slowpath` |
| 108 | `0x06C` | `__cuda_sm20_rcp_rz_f64` |
| 109 | `0x06D` | `__cuda_sm20_rcp_rz_ftz_f32` |
| 110 | `0x06E` | `__cuda_sm20_rcp_rz_ftz_f32_slowpath` |
| 111 | `0x06F` | `__cuda_sm20_rem_s16` |
| 112 | `0x070` | `__cuda_sm20_rem_s64` |
| 113 | `0x071` | `__cuda_sm20_rem_u16` |
| 114 | `0x072` | `__cuda_sm20_rem_u64` |
| 115 | `0x073` | `__cuda_sm20_sqrt_rd_f32` |
| 116 | `0x074` | `__cuda_sm20_sqrt_rd_f32_slowpath` |
| 117 | `0x075` | `__cuda_sm20_sqrt_rd_ftz_f32` |
| 118 | `0x076` | `__cuda_sm20_sqrt_rd_ftz_f32_slowpath` |
| 119 | `0x077` | `__cuda_sm20_sqrt_rn_f32` |
| 120 | `0x078` | `__cuda_sm20_sqrt_rn_f32_slowpath` |
| 121 | `0x079` | `__cuda_sm20_sqrt_rn_ftz_f32` |
| 122 | `0x07A` | `__cuda_sm20_sqrt_rn_ftz_f32_slowpath` |
| 123 | `0x07B` | `__cuda_sm20_sqrt_ru_f32` |
| 124 | `0x07C` | `__cuda_sm20_sqrt_ru_f32_slowpath` |
| 125 | `0x07D` | `__cuda_sm20_sqrt_ru_ftz_f32` |
| 126 | `0x07E` | `__cuda_sm20_sqrt_ru_ftz_f32_slowpath` |
| 127 | `0x07F` | `__cuda_sm20_sqrt_rz_f32` |
| 128 | `0x080` | `__cuda_sm20_sqrt_rz_f32_slowpath` |
| 129 | `0x081` | `__cuda_sm20_sqrt_rz_ftz_f32` |
| 130 | `0x082` | `__cuda_sm20_sqrt_rz_ftz_f32_slowpath` |

### `__cuda_sm3x_*` -- Optimized division (4 entries, `0x083`--`0x086`, sm_30)

| ID | Hex | Name |
|---|---|---|
| 131 | `0x083` | `__cuda_sm3x_div_rn_ftz_f32` |
| 132 | `0x084` | `__cuda_sm3x_div_rn_ftz_f32_slowpath` |
| 133 | `0x085` | `__cuda_sm3x_div_rn_noftz_f32` |
| 134 | `0x086` | `__cuda_sm3x_div_rn_noftz_f32_slowpath` |

### `__cuda_sm62_*` -- Integer dot product (2 entries, `0x087`--`0x088`, sm_62)

| ID | Hex | Name |
|---|---|---|
| 135 | `0x087` | `__cuda_sm62_dp2a` |
| 136 | `0x088` | `__cuda_sm62_dp4a` |

### `__cuda_sm70_*` -- Volta sync/warp/WMMA (393 entries, `0x089`--`0x211`, sm_70)

| ID | Hex | Name |
|---|---|---|
| 137 | `0x089` | `__cuda_sm70_barrier_arrive` |
| 138 | `0x08A` | `__cuda_sm70_barrier_arrive_0` |
| 139 | `0x08B` | `__cuda_sm70_barrier_arrive_0_count` |
| 140 | `0x08C` | `__cuda_sm70_barrier_arrive_1` |
| 141 | `0x08D` | `__cuda_sm70_barrier_arrive_10` |
| 142 | `0x08E` | `__cuda_sm70_barrier_arrive_10_count` |
| 143 | `0x08F` | `__cuda_sm70_barrier_arrive_11` |
| 144 | `0x090` | `__cuda_sm70_barrier_arrive_11_count` |
| 145 | `0x091` | `__cuda_sm70_barrier_arrive_12` |
| 146 | `0x092` | `__cuda_sm70_barrier_arrive_12_count` |
| 147 | `0x093` | `__cuda_sm70_barrier_arrive_13` |
| 148 | `0x094` | `__cuda_sm70_barrier_arrive_13_count` |
| 149 | `0x095` | `__cuda_sm70_barrier_arrive_14` |
| 150 | `0x096` | `__cuda_sm70_barrier_arrive_14_count` |
| 151 | `0x097` | `__cuda_sm70_barrier_arrive_15` |
| 152 | `0x098` | `__cuda_sm70_barrier_arrive_15_count` |
| 153 | `0x099` | `__cuda_sm70_barrier_arrive_1_count` |
| 154 | `0x09A` | `__cuda_sm70_barrier_arrive_2` |
| 155 | `0x09B` | `__cuda_sm70_barrier_arrive_2_count` |
| 156 | `0x09C` | `__cuda_sm70_barrier_arrive_3` |
| 157 | `0x09D` | `__cuda_sm70_barrier_arrive_3_count` |
| 158 | `0x09E` | `__cuda_sm70_barrier_arrive_4` |
| 159 | `0x09F` | `__cuda_sm70_barrier_arrive_4_count` |
| 160 | `0x0A0` | `__cuda_sm70_barrier_arrive_5` |
| 161 | `0x0A1` | `__cuda_sm70_barrier_arrive_5_count` |
| 162 | `0x0A2` | `__cuda_sm70_barrier_arrive_6` |
| 163 | `0x0A3` | `__cuda_sm70_barrier_arrive_6_count` |
| 164 | `0x0A4` | `__cuda_sm70_barrier_arrive_7` |
| 165 | `0x0A5` | `__cuda_sm70_barrier_arrive_7_count` |
| 166 | `0x0A6` | `__cuda_sm70_barrier_arrive_8` |
| 167 | `0x0A7` | `__cuda_sm70_barrier_arrive_8_count` |
| 168 | `0x0A8` | `__cuda_sm70_barrier_arrive_9` |
| 169 | `0x0A9` | `__cuda_sm70_barrier_arrive_9_count` |
| 170 | `0x0AA` | `__cuda_sm70_barrier_arrive_count` |
| 171 | `0x0AB` | `__cuda_sm70_barrier_red_and` |
| 172 | `0x0AC` | `__cuda_sm70_barrier_red_and_0` |
| 173 | `0x0AD` | `__cuda_sm70_barrier_red_and_0_count` |
| 174 | `0x0AE` | `__cuda_sm70_barrier_red_and_1` |
| 175 | `0x0AF` | `__cuda_sm70_barrier_red_and_10` |
| 176 | `0x0B0` | `__cuda_sm70_barrier_red_and_10_count` |
| 177 | `0x0B1` | `__cuda_sm70_barrier_red_and_11` |
| 178 | `0x0B2` | `__cuda_sm70_barrier_red_and_11_count` |
| 179 | `0x0B3` | `__cuda_sm70_barrier_red_and_12` |
| 180 | `0x0B4` | `__cuda_sm70_barrier_red_and_12_count` |
| 181 | `0x0B5` | `__cuda_sm70_barrier_red_and_13` |
| 182 | `0x0B6` | `__cuda_sm70_barrier_red_and_13_count` |
| 183 | `0x0B7` | `__cuda_sm70_barrier_red_and_14` |
| 184 | `0x0B8` | `__cuda_sm70_barrier_red_and_14_count` |
| 185 | `0x0B9` | `__cuda_sm70_barrier_red_and_15` |
| 186 | `0x0BA` | `__cuda_sm70_barrier_red_and_15_count` |
| 187 | `0x0BB` | `__cuda_sm70_barrier_red_and_1_count` |
| 188 | `0x0BC` | `__cuda_sm70_barrier_red_and_2` |
| 189 | `0x0BD` | `__cuda_sm70_barrier_red_and_2_count` |
| 190 | `0x0BE` | `__cuda_sm70_barrier_red_and_3` |
| 191 | `0x0BF` | `__cuda_sm70_barrier_red_and_3_count` |
| 192 | `0x0C0` | `__cuda_sm70_barrier_red_and_4` |
| 193 | `0x0C1` | `__cuda_sm70_barrier_red_and_4_count` |
| 194 | `0x0C2` | `__cuda_sm70_barrier_red_and_5` |
| 195 | `0x0C3` | `__cuda_sm70_barrier_red_and_5_count` |
| 196 | `0x0C4` | `__cuda_sm70_barrier_red_and_6` |
| 197 | `0x0C5` | `__cuda_sm70_barrier_red_and_6_count` |
| 198 | `0x0C6` | `__cuda_sm70_barrier_red_and_7` |
| 199 | `0x0C7` | `__cuda_sm70_barrier_red_and_7_count` |
| 200 | `0x0C8` | `__cuda_sm70_barrier_red_and_8` |
| 201 | `0x0C9` | `__cuda_sm70_barrier_red_and_8_count` |
| 202 | `0x0CA` | `__cuda_sm70_barrier_red_and_9` |
| 203 | `0x0CB` | `__cuda_sm70_barrier_red_and_9_count` |
| 204 | `0x0CC` | `__cuda_sm70_barrier_red_and_count` |
| 205 | `0x0CD` | `__cuda_sm70_barrier_red_or` |
| 206 | `0x0CE` | `__cuda_sm70_barrier_red_or_0` |
| 207 | `0x0CF` | `__cuda_sm70_barrier_red_or_0_count` |
| 208 | `0x0D0` | `__cuda_sm70_barrier_red_or_1` |
| 209 | `0x0D1` | `__cuda_sm70_barrier_red_or_10` |
| 210 | `0x0D2` | `__cuda_sm70_barrier_red_or_10_count` |
| 211 | `0x0D3` | `__cuda_sm70_barrier_red_or_11` |
| 212 | `0x0D4` | `__cuda_sm70_barrier_red_or_11_count` |
| 213 | `0x0D5` | `__cuda_sm70_barrier_red_or_12` |
| 214 | `0x0D6` | `__cuda_sm70_barrier_red_or_12_count` |
| 215 | `0x0D7` | `__cuda_sm70_barrier_red_or_13` |
| 216 | `0x0D8` | `__cuda_sm70_barrier_red_or_13_count` |
| 217 | `0x0D9` | `__cuda_sm70_barrier_red_or_14` |
| 218 | `0x0DA` | `__cuda_sm70_barrier_red_or_14_count` |
| 219 | `0x0DB` | `__cuda_sm70_barrier_red_or_15` |
| 220 | `0x0DC` | `__cuda_sm70_barrier_red_or_15_count` |
| 221 | `0x0DD` | `__cuda_sm70_barrier_red_or_1_count` |
| 222 | `0x0DE` | `__cuda_sm70_barrier_red_or_2` |
| 223 | `0x0DF` | `__cuda_sm70_barrier_red_or_2_count` |
| 224 | `0x0E0` | `__cuda_sm70_barrier_red_or_3` |
| 225 | `0x0E1` | `__cuda_sm70_barrier_red_or_3_count` |
| 226 | `0x0E2` | `__cuda_sm70_barrier_red_or_4` |
| 227 | `0x0E3` | `__cuda_sm70_barrier_red_or_4_count` |
| 228 | `0x0E4` | `__cuda_sm70_barrier_red_or_5` |
| 229 | `0x0E5` | `__cuda_sm70_barrier_red_or_5_count` |
| 230 | `0x0E6` | `__cuda_sm70_barrier_red_or_6` |
| 231 | `0x0E7` | `__cuda_sm70_barrier_red_or_6_count` |
| 232 | `0x0E8` | `__cuda_sm70_barrier_red_or_7` |
| 233 | `0x0E9` | `__cuda_sm70_barrier_red_or_7_count` |
| 234 | `0x0EA` | `__cuda_sm70_barrier_red_or_8` |
| 235 | `0x0EB` | `__cuda_sm70_barrier_red_or_8_count` |
| 236 | `0x0EC` | `__cuda_sm70_barrier_red_or_9` |
| 237 | `0x0ED` | `__cuda_sm70_barrier_red_or_9_count` |
| 238 | `0x0EE` | `__cuda_sm70_barrier_red_or_count` |
| 239 | `0x0EF` | `__cuda_sm70_barrier_red_popc` |
| 240 | `0x0F0` | `__cuda_sm70_barrier_red_popc_0` |
| 241 | `0x0F1` | `__cuda_sm70_barrier_red_popc_0_count` |
| 242 | `0x0F2` | `__cuda_sm70_barrier_red_popc_1` |
| 243 | `0x0F3` | `__cuda_sm70_barrier_red_popc_10` |
| 244 | `0x0F4` | `__cuda_sm70_barrier_red_popc_10_count` |
| 245 | `0x0F5` | `__cuda_sm70_barrier_red_popc_11` |
| 246 | `0x0F6` | `__cuda_sm70_barrier_red_popc_11_count` |
| 247 | `0x0F7` | `__cuda_sm70_barrier_red_popc_12` |
| 248 | `0x0F8` | `__cuda_sm70_barrier_red_popc_12_count` |
| 249 | `0x0F9` | `__cuda_sm70_barrier_red_popc_13` |
| 250 | `0x0FA` | `__cuda_sm70_barrier_red_popc_13_count` |
| 251 | `0x0FB` | `__cuda_sm70_barrier_red_popc_14` |
| 252 | `0x0FC` | `__cuda_sm70_barrier_red_popc_14_count` |
| 253 | `0x0FD` | `__cuda_sm70_barrier_red_popc_15` |
| 254 | `0x0FE` | `__cuda_sm70_barrier_red_popc_15_count` |
| 255 | `0x0FF` | `__cuda_sm70_barrier_red_popc_1_count` |
| 256 | `0x100` | `__cuda_sm70_barrier_red_popc_2` |
| 257 | `0x101` | `__cuda_sm70_barrier_red_popc_2_count` |
| 258 | `0x102` | `__cuda_sm70_barrier_red_popc_3` |
| 259 | `0x103` | `__cuda_sm70_barrier_red_popc_3_count` |
| 260 | `0x104` | `__cuda_sm70_barrier_red_popc_4` |
| 261 | `0x105` | `__cuda_sm70_barrier_red_popc_4_count` |
| 262 | `0x106` | `__cuda_sm70_barrier_red_popc_5` |
| 263 | `0x107` | `__cuda_sm70_barrier_red_popc_5_count` |
| 264 | `0x108` | `__cuda_sm70_barrier_red_popc_6` |
| 265 | `0x109` | `__cuda_sm70_barrier_red_popc_6_count` |
| 266 | `0x10A` | `__cuda_sm70_barrier_red_popc_7` |
| 267 | `0x10B` | `__cuda_sm70_barrier_red_popc_7_count` |
| 268 | `0x10C` | `__cuda_sm70_barrier_red_popc_8` |
| 269 | `0x10D` | `__cuda_sm70_barrier_red_popc_8_count` |
| 270 | `0x10E` | `__cuda_sm70_barrier_red_popc_9` |
| 271 | `0x10F` | `__cuda_sm70_barrier_red_popc_9_count` |
| 272 | `0x110` | `__cuda_sm70_barrier_red_popc_count` |
| 273 | `0x111` | `__cuda_sm70_barrier_sync` |
| 274 | `0x112` | `__cuda_sm70_barrier_sync_0` |
| 275 | `0x113` | `__cuda_sm70_barrier_sync_0_count` |
| 276 | `0x114` | `__cuda_sm70_barrier_sync_1` |
| 277 | `0x115` | `__cuda_sm70_barrier_sync_10` |
| 278 | `0x116` | `__cuda_sm70_barrier_sync_10_count` |
| 279 | `0x117` | `__cuda_sm70_barrier_sync_11` |
| 280 | `0x118` | `__cuda_sm70_barrier_sync_11_count` |
| 281 | `0x119` | `__cuda_sm70_barrier_sync_12` |
| 282 | `0x11A` | `__cuda_sm70_barrier_sync_12_count` |
| 283 | `0x11B` | `__cuda_sm70_barrier_sync_13` |
| 284 | `0x11C` | `__cuda_sm70_barrier_sync_13_count` |
| 285 | `0x11D` | `__cuda_sm70_barrier_sync_14` |
| 286 | `0x11E` | `__cuda_sm70_barrier_sync_14_count` |
| 287 | `0x11F` | `__cuda_sm70_barrier_sync_15` |
| 288 | `0x120` | `__cuda_sm70_barrier_sync_15_count` |
| 289 | `0x121` | `__cuda_sm70_barrier_sync_1_count` |
| 290 | `0x122` | `__cuda_sm70_barrier_sync_2` |
| 291 | `0x123` | `__cuda_sm70_barrier_sync_2_count` |
| 292 | `0x124` | `__cuda_sm70_barrier_sync_3` |
| 293 | `0x125` | `__cuda_sm70_barrier_sync_3_count` |
| 294 | `0x126` | `__cuda_sm70_barrier_sync_4` |
| 295 | `0x127` | `__cuda_sm70_barrier_sync_4_count` |
| 296 | `0x128` | `__cuda_sm70_barrier_sync_5` |
| 297 | `0x129` | `__cuda_sm70_barrier_sync_5_count` |
| 298 | `0x12A` | `__cuda_sm70_barrier_sync_6` |
| 299 | `0x12B` | `__cuda_sm70_barrier_sync_6_count` |
| 300 | `0x12C` | `__cuda_sm70_barrier_sync_7` |
| 301 | `0x12D` | `__cuda_sm70_barrier_sync_7_count` |
| 302 | `0x12E` | `__cuda_sm70_barrier_sync_8` |
| 303 | `0x12F` | `__cuda_sm70_barrier_sync_8_count` |
| 304 | `0x130` | `__cuda_sm70_barrier_sync_9` |
| 305 | `0x131` | `__cuda_sm70_barrier_sync_9_count` |
| 306 | `0x132` | `__cuda_sm70_barrier_sync_count` |
| 307 | `0x133` | `__cuda_sm70_matchsync_all_b32` |
| 308 | `0x134` | `__cuda_sm70_matchsync_all_b32_p` |
| 309 | `0x135` | `__cuda_sm70_matchsync_all_b64` |
| 310 | `0x136` | `__cuda_sm70_matchsync_all_b64_p` |
| 311 | `0x137` | `__cuda_sm70_matchsync_any_b32` |
| 312 | `0x138` | `__cuda_sm70_matchsync_any_b64` |
| 313 | `0x139` | `__cuda_sm70_shflsync_bfly` |
| 314 | `0x13A` | `__cuda_sm70_shflsync_bfly_p` |
| 315 | `0x13B` | `__cuda_sm70_shflsync_down` |
| 316 | `0x13C` | `__cuda_sm70_shflsync_down_p` |
| 317 | `0x13D` | `__cuda_sm70_shflsync_idx` |
| 318 | `0x13E` | `__cuda_sm70_shflsync_idx_p` |
| 319 | `0x13F` | `__cuda_sm70_shflsync_up` |
| 320 | `0x140` | `__cuda_sm70_shflsync_up_p` |
| 321 | `0x141` | `__cuda_sm70_votesync_all` |
| 322 | `0x142` | `__cuda_sm70_votesync_any` |
| 323 | `0x143` | `__cuda_sm70_votesync_ballot` |
| 324 | `0x144` | `__cuda_sm70_votesync_uni` |
| 325 | `0x145` | `__cuda_sm70_warpsync` |
| 326 | `0x146` | `__cuda_sm70_wmma_m16n16k16_load_a_col` |
| 327 | `0x147` | `__cuda_sm70_wmma_m16n16k16_load_a_col_global` |
| 328 | `0x148` | `__cuda_sm70_wmma_m16n16k16_load_a_col_shared` |
| 329 | `0x149` | `__cuda_sm70_wmma_m16n16k16_load_a_row` |
| 330 | `0x14A` | `__cuda_sm70_wmma_m16n16k16_load_a_row_global` |
| 331 | `0x14B` | `__cuda_sm70_wmma_m16n16k16_load_a_row_shared` |
| 332 | `0x14C` | `__cuda_sm70_wmma_m16n16k16_load_b_col` |
| 333 | `0x14D` | `__cuda_sm70_wmma_m16n16k16_load_b_col_global` |
| 334 | `0x14E` | `__cuda_sm70_wmma_m16n16k16_load_b_col_shared` |
| 335 | `0x14F` | `__cuda_sm70_wmma_m16n16k16_load_b_row` |
| 336 | `0x150` | `__cuda_sm70_wmma_m16n16k16_load_b_row_global` |
| 337 | `0x151` | `__cuda_sm70_wmma_m16n16k16_load_b_row_shared` |
| 338 | `0x152` | `__cuda_sm70_wmma_m16n16k16_load_c_col_f16` |
| 339 | `0x153` | `__cuda_sm70_wmma_m16n16k16_load_c_col_f16_global` |
| 340 | `0x154` | `__cuda_sm70_wmma_m16n16k16_load_c_col_f16_shared` |
| 341 | `0x155` | `__cuda_sm70_wmma_m16n16k16_load_c_col_f32` |
| 342 | `0x156` | `__cuda_sm70_wmma_m16n16k16_load_c_col_f32_global` |
| 343 | `0x157` | `__cuda_sm70_wmma_m16n16k16_load_c_col_f32_shared` |
| 344 | `0x158` | `__cuda_sm70_wmma_m16n16k16_load_c_row_f16` |
| 345 | `0x159` | `__cuda_sm70_wmma_m16n16k16_load_c_row_f16_global` |
| 346 | `0x15A` | `__cuda_sm70_wmma_m16n16k16_load_c_row_f16_shared` |
| 347 | `0x15B` | `__cuda_sm70_wmma_m16n16k16_load_c_row_f32` |
| 348 | `0x15C` | `__cuda_sm70_wmma_m16n16k16_load_c_row_f32_global` |
| 349 | `0x15D` | `__cuda_sm70_wmma_m16n16k16_load_c_row_f32_shared` |
| 350 | `0x15E` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f16_f16` |
| 351 | `0x15F` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f16_f16_satfinite` |
| 352 | `0x160` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f16_f32` |
| 353 | `0x161` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f16_f32_satfinite` |
| 354 | `0x162` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f32_f16` |
| 355 | `0x163` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f32_f16_satfinite` |
| 356 | `0x164` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f32_f32` |
| 357 | `0x165` | `__cuda_sm70_wmma_m16n16k16_mma_col_col_f32_f32_satfinite` |
| 358 | `0x166` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f16_f16` |
| 359 | `0x167` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f16_f16_satfinite` |
| 360 | `0x168` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f16_f32` |
| 361 | `0x169` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f16_f32_satfinite` |
| 362 | `0x16A` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f32_f16` |
| 363 | `0x16B` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f32_f16_satfinite` |
| 364 | `0x16C` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f32_f32` |
| 365 | `0x16D` | `__cuda_sm70_wmma_m16n16k16_mma_col_row_f32_f32_satfinite` |
| 366 | `0x16E` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f16_f16` |
| 367 | `0x16F` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f16_f16_satfinite` |
| 368 | `0x170` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f16_f32` |
| 369 | `0x171` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f16_f32_satfinite` |
| 370 | `0x172` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f32_f16` |
| 371 | `0x173` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f32_f16_satfinite` |
| 372 | `0x174` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f32_f32` |
| 373 | `0x175` | `__cuda_sm70_wmma_m16n16k16_mma_row_col_f32_f32_satfinite` |
| 374 | `0x176` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f16_f16` |
| 375 | `0x177` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f16_f16_satfinite` |
| 376 | `0x178` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f16_f32` |
| 377 | `0x179` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f16_f32_satfinite` |
| 378 | `0x17A` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f32_f16` |
| 379 | `0x17B` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f32_f16_satfinite` |
| 380 | `0x17C` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f32_f32` |
| 381 | `0x17D` | `__cuda_sm70_wmma_m16n16k16_mma_row_row_f32_f32_satfinite` |
| 382 | `0x17E` | `__cuda_sm70_wmma_m16n16k16_store_d_col_f16` |
| 383 | `0x17F` | `__cuda_sm70_wmma_m16n16k16_store_d_col_f16_global` |
| 384 | `0x180` | `__cuda_sm70_wmma_m16n16k16_store_d_col_f16_shared` |
| 385 | `0x181` | `__cuda_sm70_wmma_m16n16k16_store_d_col_f32` |
| 386 | `0x182` | `__cuda_sm70_wmma_m16n16k16_store_d_col_f32_global` |
| 387 | `0x183` | `__cuda_sm70_wmma_m16n16k16_store_d_col_f32_shared` |
| 388 | `0x184` | `__cuda_sm70_wmma_m16n16k16_store_d_row_f16` |
| 389 | `0x185` | `__cuda_sm70_wmma_m16n16k16_store_d_row_f16_global` |
| 390 | `0x186` | `__cuda_sm70_wmma_m16n16k16_store_d_row_f16_shared` |
| 391 | `0x187` | `__cuda_sm70_wmma_m16n16k16_store_d_row_f32` |
| 392 | `0x188` | `__cuda_sm70_wmma_m16n16k16_store_d_row_f32_global` |
| 393 | `0x189` | `__cuda_sm70_wmma_m16n16k16_store_d_row_f32_shared` |
| 394 | `0x18A` | `__cuda_sm70_wmma_m32n8k16_load_a_col` |
| 395 | `0x18B` | `__cuda_sm70_wmma_m32n8k16_load_a_col_global` |
| 396 | `0x18C` | `__cuda_sm70_wmma_m32n8k16_load_a_col_shared` |
| 397 | `0x18D` | `__cuda_sm70_wmma_m32n8k16_load_a_row` |
| 398 | `0x18E` | `__cuda_sm70_wmma_m32n8k16_load_a_row_global` |
| 399 | `0x18F` | `__cuda_sm70_wmma_m32n8k16_load_a_row_shared` |
| 400 | `0x190` | `__cuda_sm70_wmma_m32n8k16_load_b_col` |
| 401 | `0x191` | `__cuda_sm70_wmma_m32n8k16_load_b_col_global` |
| 402 | `0x192` | `__cuda_sm70_wmma_m32n8k16_load_b_col_shared` |
| 403 | `0x193` | `__cuda_sm70_wmma_m32n8k16_load_b_row` |
| 404 | `0x194` | `__cuda_sm70_wmma_m32n8k16_load_b_row_global` |
| 405 | `0x195` | `__cuda_sm70_wmma_m32n8k16_load_b_row_shared` |
| 406 | `0x196` | `__cuda_sm70_wmma_m32n8k16_load_c_col_f16` |
| 407 | `0x197` | `__cuda_sm70_wmma_m32n8k16_load_c_col_f16_global` |
| 408 | `0x198` | `__cuda_sm70_wmma_m32n8k16_load_c_col_f16_shared` |
| 409 | `0x199` | `__cuda_sm70_wmma_m32n8k16_load_c_col_f32` |
| 410 | `0x19A` | `__cuda_sm70_wmma_m32n8k16_load_c_col_f32_global` |
| 411 | `0x19B` | `__cuda_sm70_wmma_m32n8k16_load_c_col_f32_shared` |
| 412 | `0x19C` | `__cuda_sm70_wmma_m32n8k16_load_c_row_f16` |
| 413 | `0x19D` | `__cuda_sm70_wmma_m32n8k16_load_c_row_f16_global` |
| 414 | `0x19E` | `__cuda_sm70_wmma_m32n8k16_load_c_row_f16_shared` |
| 415 | `0x19F` | `__cuda_sm70_wmma_m32n8k16_load_c_row_f32` |
| 416 | `0x1A0` | `__cuda_sm70_wmma_m32n8k16_load_c_row_f32_global` |
| 417 | `0x1A1` | `__cuda_sm70_wmma_m32n8k16_load_c_row_f32_shared` |
| 418 | `0x1A2` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f16_f16` |
| 419 | `0x1A3` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f16_f16_satfinite` |
| 420 | `0x1A4` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f16_f32` |
| 421 | `0x1A5` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f16_f32_satfinite` |
| 422 | `0x1A6` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f32_f16` |
| 423 | `0x1A7` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f32_f16_satfinite` |
| 424 | `0x1A8` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f32_f32` |
| 425 | `0x1A9` | `__cuda_sm70_wmma_m32n8k16_mma_col_col_f32_f32_satfinite` |
| 426 | `0x1AA` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f16_f16` |
| 427 | `0x1AB` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f16_f16_satfinite` |
| 428 | `0x1AC` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f16_f32` |
| 429 | `0x1AD` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f16_f32_satfinite` |
| 430 | `0x1AE` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f32_f16` |
| 431 | `0x1AF` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f32_f16_satfinite` |
| 432 | `0x1B0` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f32_f32` |
| 433 | `0x1B1` | `__cuda_sm70_wmma_m32n8k16_mma_col_row_f32_f32_satfinite` |
| 434 | `0x1B2` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f16_f16` |
| 435 | `0x1B3` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f16_f16_satfinite` |
| 436 | `0x1B4` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f16_f32` |
| 437 | `0x1B5` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f16_f32_satfinite` |
| 438 | `0x1B6` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f32_f16` |
| 439 | `0x1B7` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f32_f16_satfinite` |
| 440 | `0x1B8` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f32_f32` |
| 441 | `0x1B9` | `__cuda_sm70_wmma_m32n8k16_mma_row_col_f32_f32_satfinite` |
| 442 | `0x1BA` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f16_f16` |
| 443 | `0x1BB` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f16_f16_satfinite` |
| 444 | `0x1BC` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f16_f32` |
| 445 | `0x1BD` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f16_f32_satfinite` |
| 446 | `0x1BE` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f32_f16` |
| 447 | `0x1BF` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f32_f16_satfinite` |
| 448 | `0x1C0` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f32_f32` |
| 449 | `0x1C1` | `__cuda_sm70_wmma_m32n8k16_mma_row_row_f32_f32_satfinite` |
| 450 | `0x1C2` | `__cuda_sm70_wmma_m32n8k16_store_d_col_f16` |
| 451 | `0x1C3` | `__cuda_sm70_wmma_m32n8k16_store_d_col_f16_global` |
| 452 | `0x1C4` | `__cuda_sm70_wmma_m32n8k16_store_d_col_f16_shared` |
| 453 | `0x1C5` | `__cuda_sm70_wmma_m32n8k16_store_d_col_f32` |
| 454 | `0x1C6` | `__cuda_sm70_wmma_m32n8k16_store_d_col_f32_global` |
| 455 | `0x1C7` | `__cuda_sm70_wmma_m32n8k16_store_d_col_f32_shared` |
| 456 | `0x1C8` | `__cuda_sm70_wmma_m32n8k16_store_d_row_f16` |
| 457 | `0x1C9` | `__cuda_sm70_wmma_m32n8k16_store_d_row_f16_global` |
| 458 | `0x1CA` | `__cuda_sm70_wmma_m32n8k16_store_d_row_f16_shared` |
| 459 | `0x1CB` | `__cuda_sm70_wmma_m32n8k16_store_d_row_f32` |
| 460 | `0x1CC` | `__cuda_sm70_wmma_m32n8k16_store_d_row_f32_global` |
| 461 | `0x1CD` | `__cuda_sm70_wmma_m32n8k16_store_d_row_f32_shared` |
| 462 | `0x1CE` | `__cuda_sm70_wmma_m8n32k16_load_a_col` |
| 463 | `0x1CF` | `__cuda_sm70_wmma_m8n32k16_load_a_col_global` |
| 464 | `0x1D0` | `__cuda_sm70_wmma_m8n32k16_load_a_col_shared` |
| 465 | `0x1D1` | `__cuda_sm70_wmma_m8n32k16_load_a_row` |
| 466 | `0x1D2` | `__cuda_sm70_wmma_m8n32k16_load_a_row_global` |
| 467 | `0x1D3` | `__cuda_sm70_wmma_m8n32k16_load_a_row_shared` |
| 468 | `0x1D4` | `__cuda_sm70_wmma_m8n32k16_load_b_col` |
| 469 | `0x1D5` | `__cuda_sm70_wmma_m8n32k16_load_b_col_global` |
| 470 | `0x1D6` | `__cuda_sm70_wmma_m8n32k16_load_b_col_shared` |
| 471 | `0x1D7` | `__cuda_sm70_wmma_m8n32k16_load_b_row` |
| 472 | `0x1D8` | `__cuda_sm70_wmma_m8n32k16_load_b_row_global` |
| 473 | `0x1D9` | `__cuda_sm70_wmma_m8n32k16_load_b_row_shared` |
| 474 | `0x1DA` | `__cuda_sm70_wmma_m8n32k16_load_c_col_f16` |
| 475 | `0x1DB` | `__cuda_sm70_wmma_m8n32k16_load_c_col_f16_global` |
| 476 | `0x1DC` | `__cuda_sm70_wmma_m8n32k16_load_c_col_f16_shared` |
| 477 | `0x1DD` | `__cuda_sm70_wmma_m8n32k16_load_c_col_f32` |
| 478 | `0x1DE` | `__cuda_sm70_wmma_m8n32k16_load_c_col_f32_global` |
| 479 | `0x1DF` | `__cuda_sm70_wmma_m8n32k16_load_c_col_f32_shared` |
| 480 | `0x1E0` | `__cuda_sm70_wmma_m8n32k16_load_c_row_f16` |
| 481 | `0x1E1` | `__cuda_sm70_wmma_m8n32k16_load_c_row_f16_global` |
| 482 | `0x1E2` | `__cuda_sm70_wmma_m8n32k16_load_c_row_f16_shared` |
| 483 | `0x1E3` | `__cuda_sm70_wmma_m8n32k16_load_c_row_f32` |
| 484 | `0x1E4` | `__cuda_sm70_wmma_m8n32k16_load_c_row_f32_global` |
| 485 | `0x1E5` | `__cuda_sm70_wmma_m8n32k16_load_c_row_f32_shared` |
| 486 | `0x1E6` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f16_f16` |
| 487 | `0x1E7` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f16_f16_satfinite` |
| 488 | `0x1E8` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f16_f32` |
| 489 | `0x1E9` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f16_f32_satfinite` |
| 490 | `0x1EA` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f32_f16` |
| 491 | `0x1EB` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f32_f16_satfinite` |
| 492 | `0x1EC` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f32_f32` |
| 493 | `0x1ED` | `__cuda_sm70_wmma_m8n32k16_mma_col_col_f32_f32_satfinite` |
| 494 | `0x1EE` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f16_f16` |
| 495 | `0x1EF` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f16_f16_satfinite` |
| 496 | `0x1F0` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f16_f32` |
| 497 | `0x1F1` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f16_f32_satfinite` |
| 498 | `0x1F2` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f32_f16` |
| 499 | `0x1F3` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f32_f16_satfinite` |
| 500 | `0x1F4` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f32_f32` |
| 501 | `0x1F5` | `__cuda_sm70_wmma_m8n32k16_mma_col_row_f32_f32_satfinite` |
| 502 | `0x1F6` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f16_f16` |
| 503 | `0x1F7` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f16_f16_satfinite` |
| 504 | `0x1F8` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f16_f32` |
| 505 | `0x1F9` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f16_f32_satfinite` |
| 506 | `0x1FA` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f32_f16` |
| 507 | `0x1FB` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f32_f16_satfinite` |
| 508 | `0x1FC` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f32_f32` |
| 509 | `0x1FD` | `__cuda_sm70_wmma_m8n32k16_mma_row_col_f32_f32_satfinite` |
| 510 | `0x1FE` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f16_f16` |
| 511 | `0x1FF` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f16_f16_satfinite` |
| 512 | `0x200` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f16_f32` |
| 513 | `0x201` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f16_f32_satfinite` |
| 514 | `0x202` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f32_f16` |
| 515 | `0x203` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f32_f16_satfinite` |
| 516 | `0x204` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f32_f32` |
| 517 | `0x205` | `__cuda_sm70_wmma_m8n32k16_mma_row_row_f32_f32_satfinite` |
| 518 | `0x206` | `__cuda_sm70_wmma_m8n32k16_store_d_col_f16` |
| 519 | `0x207` | `__cuda_sm70_wmma_m8n32k16_store_d_col_f16_global` |
| 520 | `0x208` | `__cuda_sm70_wmma_m8n32k16_store_d_col_f16_shared` |
| 521 | `0x209` | `__cuda_sm70_wmma_m8n32k16_store_d_col_f32` |
| 522 | `0x20A` | `__cuda_sm70_wmma_m8n32k16_store_d_col_f32_global` |
| 523 | `0x20B` | `__cuda_sm70_wmma_m8n32k16_store_d_col_f32_shared` |
| 524 | `0x20C` | `__cuda_sm70_wmma_m8n32k16_store_d_row_f16` |
| 525 | `0x20D` | `__cuda_sm70_wmma_m8n32k16_store_d_row_f16_global` |
| 526 | `0x20E` | `__cuda_sm70_wmma_m8n32k16_store_d_row_f16_shared` |
| 527 | `0x20F` | `__cuda_sm70_wmma_m8n32k16_store_d_row_f32` |
| 528 | `0x210` | `__cuda_sm70_wmma_m8n32k16_store_d_row_f32_global` |
| 529 | `0x211` | `__cuda_sm70_wmma_m8n32k16_store_d_row_f32_shared` |

### `__cuda_sm80_*` -- Ampere createpolicy (3 entries, `0x212`--`0x214`, sm_80)

| ID | Hex | Name |
|---|---|---|
| 530 | `0x212` | `__cuda_sm80_createpolicy_fractional` |
| 531 | `0x213` | `__cuda_sm80_createpolicy_fractional_encode` |
| 532 | `0x214` | `__cuda_sm80_createpolicy_range_encode` |

### `__cuda_sm_10x_*` -- Blackwell hmma/imma/bit MMA (10 entries, `0x215`--`0x21E`, sm_100)

| ID | Hex | Name |
|---|---|---|
| 533 | `0x215` | `__cuda_sm_10x_hmma_mdata_m16n8k16` |
| 534 | `0x216` | `__cuda_sm_10x_hmma_mdata_m16n8k32` |
| 535 | `0x217` | `__cuda_sm_10x_imma_mdata_m16n8k32` |
| 536 | `0x218` | `__cuda_sm_10x_imma_mdata_m16n8k64` |
| 537 | `0x219` | `__cuda_sm_10x_mma_bit_internal_and_m16n8k128` |
| 538 | `0x21A` | `__cuda_sm_10x_mma_bit_internal_and_m16n8k256` |
| 539 | `0x21B` | `__cuda_sm_10x_mma_bit_internal_and_m8n8k128` |
| 540 | `0x21C` | `__cuda_sm_10x_mma_bit_internal_xor_m16n8k128` |
| 541 | `0x21D` | `__cuda_sm_10x_mma_bit_internal_xor_m16n8k256` |
| 542 | `0x21E` | `__cuda_sm_10x_mma_bit_internal_xor_m8n8k128` |

### `__cuda_sm_8x_*` -- Direct MMA + shfl (14 entries, `0x21F`--`0x22C`, sm_80+)

| ID | Hex | Name |
|---|---|---|
| 543 | `0x21F` | `__cuda_sm_8x_mma_col_col_f16_f16_f16_f16` |
| 544 | `0x220` | `__cuda_sm_8x_mma_col_col_f32_f16_f16_f16` |
| 545 | `0x221` | `__cuda_sm_8x_mma_col_col_f32_f16_f16_f32` |
| 546 | `0x222` | `__cuda_sm_8x_mma_col_row_f16_f16_f16_f16` |
| 547 | `0x223` | `__cuda_sm_8x_mma_col_row_f32_f16_f16_f16` |
| 548 | `0x224` | `__cuda_sm_8x_mma_col_row_f32_f16_f16_f32` |
| 549 | `0x225` | `__cuda_sm_8x_mma_row_col_f16_f16_f16_f16` |
| 550 | `0x226` | `__cuda_sm_8x_mma_row_col_f32_f16_f16_f16` |
| 551 | `0x227` | `__cuda_sm_8x_mma_row_col_f32_f16_f16_f32` |
| 552 | `0x228` | `__cuda_sm_8x_mma_row_row_f16_f16_f16_f16` |
| 553 | `0x229` | `__cuda_sm_8x_mma_row_row_f32_f16_f16_f16` |
| 554 | `0x22A` | `__cuda_sm_8x_mma_row_row_f32_f16_f16_f32` |
| 555 | `0x22B` | `__cuda_sm_8x_mma_shfl_f16` |
| 556 | `0x22C` | `__cuda_sm_8x_mma_shfl_f32` |

### `__cuda_sm_9x_*` -- Hopper sub-byte/bit MMA (51 entries, `0x22D`--`0x25F`, sm_90)

| ID | Hex | Name |
|---|---|---|
| 557 | `0x22D` | `__cuda_sm_9x_mma_bit_internal_xor_m16n8k128` |
| 558 | `0x22E` | `__cuda_sm_9x_mma_bit_internal_xor_m16n8k256` |
| 559 | `0x22F` | `__cuda_sm_9x_mma_bit_internal_xor_m8n8k128` |
| 560 | `0x230` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_s4_s4` |
| 561 | `0x231` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_s4_s4_satfinite` |
| 562 | `0x232` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_s4_u4` |
| 563 | `0x233` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_s4_u4_satfinite` |
| 564 | `0x234` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_u4_s4` |
| 565 | `0x235` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_u4_s4_satfinite` |
| 566 | `0x236` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_u4_u4` |
| 567 | `0x237` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k32_u4_u4_satfinite` |
| 568 | `0x238` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_s4_s4` |
| 569 | `0x239` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_s4_s4_satfinite` |
| 570 | `0x23A` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_s4_u4` |
| 571 | `0x23B` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_s4_u4_satfinite` |
| 572 | `0x23C` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_u4_s4` |
| 573 | `0x23D` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_u4_s4_satfinite` |
| 574 | `0x23E` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_u4_u4` |
| 575 | `0x23F` | `__cuda_sm_9x_mma_sub_byte_internal_m16n8k64_u4_u4_satfinite` |
| 576 | `0x240` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_s4_s4` |
| 577 | `0x241` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_s4_s4_satfinite` |
| 578 | `0x242` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_s4_u4` |
| 579 | `0x243` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_s4_u4_satfinite` |
| 580 | `0x244` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_u4_s4` |
| 581 | `0x245` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_u4_s4_satfinite` |
| 582 | `0x246` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_u4_u4` |
| 583 | `0x247` | `__cuda_sm_9x_mma_sub_byte_internal_m8n8k32_u4_u4_satfinite` |
| 584 | `0x248` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_s4_s4` |
| 585 | `0x249` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_s4_s4_satfinite` |
| 586 | `0x24A` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_s4_u4` |
| 587 | `0x24B` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_s4_u4_satfinite` |
| 588 | `0x24C` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_u4_s4` |
| 589 | `0x24D` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_u4_s4_satfinite` |
| 590 | `0x24E` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_u4_u4` |
| 591 | `0x24F` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_u4_u4_satfinite` |
| 592 | `0x250` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_s4_0` |
| 593 | `0x251` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_s4_1` |
| 594 | `0x252` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_s4_satfinite_0` |
| 595 | `0x253` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_s4_satfinite_1` |
| 596 | `0x254` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_u4_0` |
| 597 | `0x255` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_u4_1` |
| 598 | `0x256` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_u4_satfinite_0` |
| 599 | `0x257` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_s4_u4_satfinite_1` |
| 600 | `0x258` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_s4_0` |
| 601 | `0x259` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_s4_1` |
| 602 | `0x25A` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_s4_satfinite_0` |
| 603 | `0x25B` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_s4_satfinite_1` |
| 604 | `0x25C` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_u4_0` |
| 605 | `0x25D` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_u4_1` |
| 606 | `0x25E` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_u4_satfinite_0` |
| 607 | `0x25F` | `__cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k64_u4_u4_satfinite_1` |
