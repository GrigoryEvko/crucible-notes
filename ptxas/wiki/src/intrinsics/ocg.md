# OCG Intrinsic System (44 Builtin Operations, SM100+)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The OCG (Optimized Code Generation) intrinsic subsystem is a separate, parallel dispatch mechanism for SM100+ builtin operations. While the classical intrinsic system at `sub_5D1660` maps `__cuda_*` runtime helper names to integer IDs and emits inline PTX code via body templates, the OCG system maps `__nv_ptx_builtin_ocg_*` function names to type-specific handler functions that validate parameters and emit SASS instructions directly — bypassing the PTX intermediate step entirely.

| | |
|---|---|
| **OCG intrinsic table** | `sub_6C9EB0` (13KB) — `__nv_ptx_builtin_ocg_*` dispatch for SM100+ |
| **OCG router** | `sub_6CC690` (22KB) — routes OCG calls to type-specific handlers |
| **OCG name resolver** | `sub_6C9BC0` — resolves operation names to internal enums |

## Initialization — `sub_6C9EB0`

`sub_6C9EB0` initializes a 10,664-byte (0x29A8) lookup table and sets the vtable pointer to `off_202CF48`. The operation name prefix is stored at `*(_QWORD *)(a1 + 120) = "__nv_ptx_builtin_ocg_"`. The table contains 44 operations in 248-byte slots starting at offset 128. Each slot holds the operation name followed by up to 30 sub-operation/modifier string pointers (unused slots are NULL from the memset).

## OCG Builtin Name Table — Complete (44 Operations)

The complete OCG builtin table extracted from `sub_6C9EB0`. Thirty numeric string pointers that IDA left unresolved were recovered by reading null-terminated strings from the ptxas binary at `addr - 0x400000` (ELF LOAD virtual address base). The table size 0x29A8 and 248-byte slot stride are verified against the `memset` in the decompiled code.

### Arithmetic and ALU Operations

| Slot | Offset | OCG Name | Sub-Operations / Types | SASS Equivalent |
|---|---|---|---|---|
| 0 | 128 | `add` | s32, f32, s64, f64, sat | IADD3 / FADD |
| 28 | 7072 | `mnmx` | s32, u32, s64, u64 | IMNMX / FMNMX |
| 15 | 3848 | `viadd` | 32, f16x2 | VIADD |

### Vector Integer Operations (SM100+ VIMNMX family)

All six vector integer operations share the same type set: s32, u32, s16x2, u16x2 with an optional `relu` modifier for ReLU clamping.

| Slot | Offset | OCG Name | SASS Equivalent | Description |
|---|---|---|---|---|
| 16 | 4096 | `viaddmax` | VIADDMNMX | fused add + max |
| 17 | 4344 | `viaddmin` | VIADDMNMX | fused add + min |
| 18 | 4592 | `vimax` | VIMNMX | vector integer max |
| 19 | 4840 | `vimin` | VIMNMX | vector integer min |
| 20 | 5088 | `vimax3` | VIMNMX3 | 3-way vector integer max |
| 21 | 5336 | `vimin3` | VIMNMX3 | 3-way vector integer min |

### Packed Float Operations (f16x2 arithmetic)

All three packed operations share the same modifier set: `ftz` (flush-to-zero) and rounding modes `rn`, `rm`, `rp`, `rz`.

| Slot | Offset | OCG Name | SASS Equivalent | Description |
|---|---|---|---|---|
| 25 | 6328 | `fadd2` | HADD2 / FADD.PACKED | packed f16 addition |
| 26 | 6576 | `ffma2` | HFMA2 / FFMA.PACKED | packed f16 fused multiply-add |
| 27 | 6824 | `fmul2` | HMUL2 / FMUL.PACKED | packed f16 multiplication |
| 29 | 7320 | `fmax3` | FMNMX3 | 3-way float max (ftz, nan modifiers) |
| 30 | 7568 | `fmin3` | FMNMX3 | 3-way float min (ftz, nan modifiers) |

### Async Copy and TMA Operations

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 1 | 376 | `cp_async_commit` | mem, bulk, shared, global | LDGDEPBAR |
| 2 | 624 | `cp_async_wait` | mem, bulk, shared, global, read, write | DEPBAR |
| 10 | 2608 | `cp_async_bulk` | mbarrier, counted, shared, global, multicast, sequenced, bytemask | UBLKCP |
| 11 | 2856 | `cp_red_async_bulk` | mbarrier, counted, shared, global; types: u32/s32/u64/s64/f16/f32/f32ftz/f64/bf16; ops: add/min/max/inc/dec/and/or/xor | UBLKCP.RED |
| 12 | 3104 | `cp_async_tensor` | mbarrier, shared, global, 1d/2d/3d/4d/5d, im2col, multicast | UTMAKCP |
| 13 | 3352 | `cp_async_prefetch_tensor` | global, 1d/2d/3d/4d/5d, im2col | UTMAPF |

Note: The SASS mnemonics `UBLKCP` and `UTMAKCP` do not appear as strings in the ptxas binary. These are SASS assembler-level names visible only in cuobjdump output; the OCG names (`cp_async_bulk`, `cp_async_tensor`) are the canonical internal form.

### Load, Store, and Cache Operations

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 3 | 872 | `cache` | tensor, pf (prefetch), iv (invalidate), ivall (invalidate all) | CCTL / PREFETCH |
| 4 | 1120 | `ld_mc` | ops: add/min/max/f32add/and/or/xor; types: f16x2/f16x4/f16x8/bf16x2/bf16x4/bf16x8/f32/f32x2/f32x4/f64/u32/s32/s64/u64 | LDG.MC |
| 5 | 1368 | `ldc` | u32, u64 | LDC |
| 6 | 1616 | `s2r` | (none — register 0-255) | S2R |
| 22 | 5584 | `write_async` | release; shared/global; gpu/sys/mmio; v2/v4; u8/s8/u16/s16/b32/b64/u32/f64 | STG.ASYNC |
| 23 | 5832 | `cctl_c` | ldc/ldcu, shallow/deep, iv/ivall | CCTL |

### Async Reduction and Fence Operations

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 9 | 2360 | `red_async` | release; shared/global; gpu/sys/mmio; v2/v4; u32/s32/u64; add/min/max/inc/dec/and/or/xor | RED.ASYNC |
| 14 | 3600 | `fence_view_async` | all, global, shared, dshared, tensor | FENCE.VIEW.ASYNC |

### Tensor Core Operations (Blackwell TC family)

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
- **Operand reuse**: `areuse`/`akeep` (A matrix), `breuse`/`bkeep` (B matrix) — register reuse hints
- **Warp-shared**: `ws` — warp-shared execution across 2 warps
- **Block scaling**: `blockscale` with `2x`/`4x` multipliers and `impl` (implementation-defined) — FP4/FP6 microscaled format support
- **Buffers**: `buffer0`-`buffer3` — double/quad buffering for pipelined execution

The SWS (Software Scoreboard) operations (`tcatomsws`, `tcldsws`, `tcstsws`) are a Blackwell synchronization mechanism for tensor core pipelines that replaces hardware scoreboards with software-managed tracking.

### Tensor Memory Load/Store (Blackwell native)

| Slot | Offset | OCG Name | Sub-Operations | SASS Equivalent |
|---|---|---|---|---|
| 42 | 10296 | `ldtm` | formats: 16dp128bit/16dp256bit/32dp32bit/16dp64bit/16dp32bitt0t15/16dp32bitt16t31/16dp32bit; scale: x1-x128; pack16bit; fused/stat; statistics: nan/max/maxabs/min/minabs; types: u32/s32/f32/b32; sparsity: sparsify/u2/spfactor2to4 | LDTM |
| 43 | 10544 | `sttm` | formats: (same 7 as ldtm); scale: x1-x128; expand16bit; fused; b32 | STTM |

The `ldtm`/`sttm` format strings encode the tensor memory data layout:
- `16dp128bit` — 16 data-points, 128-bit total (e.g., 16x fp8)
- `16dp256bit` — 16 data-points, 256-bit total (e.g., 16x fp16)
- `32dp32bit` — 32 data-points, 32-bit total (e.g., 32x 1-bit)
- `16dp32bitt0t15` / `16dp32bitt16t31` — 16 data-points in thread groups 0-15 / 16-31
- Scale factors `x1` through `x128` control the number of consecutive elements loaded
- `sparsify` and `spfactor2to4` enable structured 2:4 sparsity metadata generation
- `stat` with `nan`/`max`/`maxabs`/`min`/`minabs` enables online statistics collection during load

### Synchronization and Control

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

## Handler Functions

The OCG handler cluster at `0x6C0000`--`0x6CC000` contains ~25–30 specialized handler/validator functions. Each validates parameters, types, sub-operations, and memory domains before delegating to the SASS encoding engine.

| Address | Size | Handler | Confidence |
|---|---|---|---|
| `sub_6C0D90` | 19KB | Atomic reduction (atom.add/min/max/cas, scope, memory order, vector width) | 90% |
| `sub_6C1CF0` | 16KB | Mbarrier (arrive, wait, test, counted, bytemask variants) | 88% |
| `sub_6C2AE0` | 10KB | cp.async (basic async copy) | 85% |
| `sub_6C3470` | 20KB | cp.async.bulk (bulk async copy with type validation) | 85% |
| `sub_6C46B0` | — | cp.red.async.bulk (bulk async reduction) | 85% |
| `sub_6C4DA0` | 15KB | Load/store (scope, memory order, domain validation) | 85% |
| `sub_6C5A40` | 8KB | Cache control (CCTL: shallow/deep, iv/ivall, ldc/ldcu) | 85% |
| `sub_6C60B0` | 7KB | Distributed shared memory (selfcast/broadcast) | 80% |
| `sub_6C8100` | 9KB | cp.async.tensor / TMA (1–5D, multicast, tile/im2col) | 85% |
| `sub_6C9BC0` | — | Name resolver (operation name -> internal enum) | 80% |
| `sub_6CC690` | 22KB | Router (dispatches to type-specific handlers via vtable) | 80% |

## OCG Validation Strings

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

## OCG SASS-Level Handlers

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

## OCG Intrinsic Lowering Pipeline — `sub_6A97B0` + `sub_6CC690`

The full end-to-end flow that takes a PTX `call.uni __nv_ptx_builtin_ocg_*` intrinsic and produces a binary SASS instruction passes through five stages. Three are data-structure manipulation (matching, cleanup), two are instruction encoding (operand assembly, SASS emission).

```text
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

**Internal SASS opcode enum values** assigned by the router (not binary SASS opcodes — these are routing keys that `sub_C3F490` maps to encoding templates):

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
| 0–3 | Reserved (zero) |
| 4 | Barrier register (`0x90000000 \| reg`) |
| 5–7 | Extra source operands (from instruction node) |
| 8–10 | Primary operands (from `sub_6CAFD0` decode) |
| 11 | Secondary operand (LDC, conditional loads) |
| 12 | Predicate thread operand |
| 13 | Scope / memory-order (from `sub_6CAF50`) |
| 14 | Cache mode operand |
| 15 | Memory fence operand |
| 16 | Uniform / extended operand (from `sub_6CAE80`) |
| 17 | Memory ordering constant / barrier tracking |
| 19–21 | Source address (bulk/tensor ops) |
| 22–24 | Destination address (bulk/tensor ops) |
| 25 | Extra predicate (opcode 187 only) |
| 26 | Control word 0: `0x60000000 \| modifier_bits` |
| 27 | Control word 1: `0x60000000 \| ordering \| flags` |
| 28 | Control word 2: `0x60000000 \| scope \| 0x81000` |
| 29 | Write mask operand (conditional) |

## OCG Lookup Flow

```text
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

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_6C9EB0` | 13KB | OCG intrinsic table init (`__nv_ptx_builtin_ocg_*`) | 95% |
| `sub_6CC690` | 22KB | OCG router — vtable-dispatched operand assembly and SASS emission | 90% |
| `sub_6C9BC0` | — | OCG name parser — decomposes `__nv_ptx_builtin_ocg_X_Y_Z` into enum + sub-op array | 95% |
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
| `sub_6D9290` | — | OCG vtable entry point (calls `sub_6C9BC0` then `sub_6D8B20`) | 85% |
| `sub_6CB8A0` | — | SASS instruction emitter (template lookup via `sub_C3F490`) | 80% |
| `sub_6CAFD0` | — | Operand decoder (registers into v134[] slots) | 85% |
| `sub_6CAE80` | — | Uniform operand encoder | 85% |
| `sub_6CAF50` | — | Scope / memory-order encoder | 85% |
| `sub_6CBA50` | — | Barrier-level encoder | 85% |
| `sub_6CB4B0` | — | Operand validator (called by `sub_6CB8A0`) | 80% |
| `sub_6A97B0` | 26KB | LowerIntrinsicOp — SASS matching and unmatched-node GC | 90% |
| `sub_6B5F30` | — | Intrinsic lowering driver (calls `sub_6B40C0` then `sub_6A97B0`) | 90% |
| `sub_6A92E0` | — | RB-tree fixup (rotation/recolor after deletion) | 90% |
| `sub_6BC1D0` | — | Default opcode validator (vtable[2] of `off_202CF48`) | 90% |
| `sub_6BCE50` | — | Default scope validator (vtable[24]) | 90% |
| `sub_6BBEC0` | — | Default memory-order validator (vtable[25]) | 90% |

## Diagnostic Strings

| String | Location | Context |
|---|---|---|
| `"__nv_ptx_builtin_ocg_"` | `sub_6C9EB0` (0x6c9ecf) | OCG builtin name prefix |
| `"instrinsic"` (sic) | Multiple OCG handlers | Consistent NVIDIA typo for "intrinsic" |
| `".RELU not allowed with unsigned type"` | `sub_6BEC60` | OCG LDC/S2R handler |

## Cross-References

- [Intrinsic Table Architecture](index.md) — Classical 607-entry intrinsic system and body template tables
- [Math Intrinsics](math.md) — IEEE math software emulation (div, rcp, sqrt, rem)
- [Tensor Core Intrinsics](tensor.md) — WMMA, MMA, WGMMA, tcgen05 lowering
- [Sync & Warp Intrinsics](sync-warp.md) — Barriers, vote, shuffle, match, redux
- [SM Architecture Map](../targets/index.md) — Per-SM capability dispatch tables
- [TCGen05 — 5th Gen Tensor Cores](../targets/tcgen05.md) — Blackwell tensor core ISA detail
- [Mercury Encoder](../codegen/mercury.md) — Master SASS encoder `sub_6D9690` (94KB)
- [SASS Instruction Encoding](../codegen/encoding.md) — Instruction encoding infrastructure
- [ISel Pattern Matching](../codegen/isel.md) — Internal SASS routing values
- [Pipeline Overview](../pipeline/overview.md) — OCG-time measurement covers intrinsic lowering
