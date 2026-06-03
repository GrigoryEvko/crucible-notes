# SCTypeConverter

> *All addresses, symbol names, and table values on this page were read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The `.symtab` is **not stripped**; every claim is anchored to a demangled symbol, a relocation addend, or a decompiled body. Other versions will differ.*

## Abstract

When the SparseCore (SC) lowering descends a `mlir::sparse_core` (`ScDialect`) function into LLVM dialect, it needs to answer one structural question for every memref it touches: *what LLVM address-space integer goes on the `!llvm.ptr` inside the lowered memref descriptor struct?* The **SCTypeConverter** answers it. It is not a free-standing class but the `LLVMTypeConverter` that `LowerToSparseCoreLlvmPass::lowerFunc` (`0x13568280`) builds, augmented by exactly one `registerTypeAttributeConversion` lambda (`0x135763c0`) that converts a memref's `MemorySpaceAttr` into a 64-bit `IntegerAttr` carrying the raw SparseCore address-space ID. The headline result is that the rule is **one line**: the LLVM address space is `MemorySpaceToAddressSpace(MS)` — the same forward map the rest of the backend uses — so a `memref<…, #sc.memory_space<spmem>>` lowers to a descriptor whose pointers are `!llvm.ptr<202>`. There is **no renumbering**; the SC address-space ID *is* the LLVM `addrspace`.

This page owns three things, all of which the [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) rewrite bodies and the [DMA bridge-cast](lower-to-mlo-dma-bridge.md) sit on top of:

- **The address-space → `!llvm.ptr` type map** — the `MemorySpaceAttr → IntegerAttr(addrspace)` conversion lambda, the byte-dumped `MemorySpaceToAddressSpace` reverse table for all 21 named IDs, and the **sequencer-context flatten** override that collapses the per-tile and per-SCS spaces onto their generic base when the function is not in an execute-/core-sequencer context. That flatten *is* the elide-vs-emit decision the `MemorySpaceCast` lowering consults.
- **The `CheckAddressSpaces` legality matrix** — the single legality gate of the SC simple-DMA tier (`0x135b8e00`): the "simple DMA touching generic SMEM is only legal on the Scalar Core Sequencer" contract, with its `SupportsTileSmemDma` target-capability hatch and its `sc.sequencer=="scs"` escape decoded top-to-bottom.
- **The EUP instantiation roster** — the 42 type-converted elementwise lowerings in two families: 12 `UnaryFloatVectorOpLowering` (the 1:1 EUP push+pop macro) and 30 `AluEpOpLowering` (the 1:N unpack → compute → repack), each mapped from its source op to its EUP intrinsic or re-emitted compute op, with the `IsDynamicallyLegal` predicate that picks between the two paths.

The AS-id table itself — ID ↔ `MemorySpace` enum ↔ memory pool ↔ on/off-tile — is **not** re-derived here; it lives on [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md), and the per-cast `addrspacecast` ISel lives on [addrspacecast ISel](../sparsecore/addrspacecast-isel.md). This page documents how the type converter *consumes* that table to stamp pointer types, and how the legality and EUP layers ride on the converted types.

| | |
|---|---|
| **Type-converter site** | `LowerToSparseCoreLlvmPass::lowerFunc` @ `0x13568280` (`LLVMTypeConverter` ctor @ `0x13568369`) |
| **AS-attr conversion install** | `TypeConverter::registerTypeAttributeConversion` @ `0x135685e8` |
| **The conversion rule** | `!llvm.ptr<MemorySpaceToAddressSpace(MemorySpaceAttr::getValue())>` |
| **Conversion lambda (lowerFunc)** | `0x135763c0` — with the sequencer-context flatten override |
| **Conversion lambda (lowerAsserts)** | `0x135b6f80` — same map, **no** flatten override |
| **Forward map** | `mlir::sparse_core::MemorySpaceToAddressSpace(MemorySpace)` @ `0x14b78780` (table `0xaf36ce8`, mask `0x3fff7f`) |
| **Flatten gate booleans** | `ScDialect::HasExecuteSequencerTypeAttribute` @ `0x1459a020` · `HasCoreSequencerTypeAttribute` @ `0x14599ec0` |
| **Pointer constructor** | `LLVM::LLVMPointerType::get(ctx, ID)` @ `0x1746eb40` (ID = raw SC address-space) |
| **Legality gate** | `CheckAddressSpaces(SparseCoreTarget&, Operation*, int, int)` @ `0x135b8e00` |
| **Legality caller** | `DmaSimpleStartOpLowering::matchAndRewrite` @ `0x135a9100` (call @ `0x135a977a`) |
| **EUP roster** | 12 `UnaryFloatVectorOpLowering` (1:1 macro) + 30 `AluEpOpLowering` (1:N unpack/compute/pack) |
| **1:1-vs-1:N gate** | `IsDynamicallyLegal` @ `0x135ddd20` |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## The Address-Space → `!llvm.ptr` Type Map

### Where the converter is built

The SparseCore type converter is not a distinct C++ class — there is no `SCTypeConverter` symbol. It is an ordinary `mlir::LLVMTypeConverter` constructed inside `LowerToSparseCoreLlvmPass::lowerFunc` (`0x13568280`): the ctor fires at `0x13568369`, and the *only* SC-specific behaviour added on top is a single `TypeConverter::registerTypeAttributeConversion` call at `0x135685e8` that installs the `BaseMemRefType × MemorySpaceAttr` lambda at `0x135763c0`. Everything else — the memref → `{alloc-ptr, align-ptr, offset, [sizes], [strides]}` descriptor struct, the function-signature conversion, the scalar/vector type passthrough — is stock upstream `LLVMTypeConverter`.

That separation is the reimplementation contract: **you do not subclass the type converter; you register one attribute-conversion lambda.** The lambda's job is narrow — turn a memref's source-dialect `MemorySpaceAttr` into the integer the base converter will bake into the descriptor's pointers — and the base converter does the rest.

### The conversion rule is one line

The lambda at `0x135763c0` is, stripped of MLIR bookkeeping:

```text
AttributeConversionResult convertSCMemorySpace(BaseMemRefType, MemorySpaceAttr msAttr):
    MemorySpace ms = msAttr.getValue()                    // 0x145929e0  (1-based enum)
    int id        = MemorySpaceToAddressSpace(ms)         // 0x14b78780  (the AS-id table)
    id            = applySequencerFlatten(id, ms, fn)     // override band — see below
    IntegerType i64 = IntegerType::get(ctx, 64, Signless) // 0x1d8c60c0
    return IntegerAttr::get(i64, id)                       // 0x1d859f00 — the new memory-space attr
```

The result is a 64-bit signless `IntegerAttr` carrying the raw address-space ID. The base `LLVMTypeConverter` then reads that integer attribute when it lowers the `MemRefType` and stamps it onto every `!llvm.ptr` field of the descriptor struct via `LLVM::LLVMPointerType::get(ctx, ID)` (`0x1746eb40`). So a `memref<…, #sc.memory_space<spmem>>` becomes `!llvm.struct<(ptr<202>, ptr<202>, i64, …)>`. The path is byte-confirmed end-to-end through `CircularBufferDescriptor::GetMemRefType` (`0x135c6020`): `MemRefType::get` with a `MemorySpaceAttr` → `convertType` (`0x1c956740`) → `mlir::StructBuilder` (`0x171c1640`, `extractPtr`/`setPtr`).

`MemorySpaceToAddressSpace` (`0x14b78780`) is the same forward map the [fat-pointers page](../sparsecore/fat-pointers-as789.md) documents; its decompiled body is:

```c
__int64 MemorySpaceToAddressSpace(unsigned int ms) {
  if (ms - 1 > 0x15 || ((0x3FFF7Fu >> (ms - 1)) & 1) == 0)
    LOG(FATAL) << "Unsupported memory space: " << ms;   // sc_enums.cc:110
  return dword_AF36CE8[ms - 1];                          // 22-entry reverse table
}
```

The range check `ms - 1 > 0x15` admits `MemorySpace` ∈ 1..22; the bitmask `0x3FFF7F` rejects the `MemorySpace 8` gap (and any unset bit), `LOG(FATAL)`-ing on an invalid space. The table `0xAF36CE8` is reproduced below — it is the **exact inverse** of the AS-id table on the fat-pointers page.

> **GOTCHA —** the result attribute is a plain 64-bit `IntegerAttr`, *not* a re-emitted `MemorySpaceAttr`. The SC `MemorySpaceAttr` does not survive into LLVM dialect; only its numeric address-space ID does, carried as an `i64` integer attribute that the base converter consumes when building `ptr<ID>`. A reimplementation that tries to thread the source-dialect attribute through unchanged will not match the descriptor the base converter actually builds.

### Table A — `MemorySpaceToAddressSpace` reverse table (`0xaf36ce8`)

The SCTypeConverter result for `!llvm.ptr<N>` is `N = MemorySpaceToAddressSpace(MS)`. Index = `MS − 1`; `MemorySpace 8` is a gap (invalid → `LOG(FATAL)`). The **flatten** column is the `lowerFunc` override (next section); the `lowerAsserts` lambda omits it.

| MS | pool | → addrspace (= `ptr<N>`) | sequencer flatten | Confidence |
|---|---|---|---|---|
| 1 | `smem` | 0 (`0x00`) | — | CONFIRMED |
| 2 | `tile_spmem` | 201 (`0xC9`) | — | CONFIRMED |
| 3 | `spmem` | 202 (`0xCA`) | — | CONFIRMED |
| 4 | `hbm` | 203 (`0xCB`) | — | CONFIRMED |
| 5 | `sflag` | 204 (`0xCC`) | — | CONFIRMED |
| 6 | `vmem` | 205 (`0xCD`) | — | CONFIRMED |
| 7 | `dreg` | 208 (`0xD0`) | — | CONFIRMED |
| 8 | *(gap)* | invalid → `LOG(FATAL)` | — | CONFIRMED |
| 9 | `smem_any` | 212 (`0xD4`) | — | CONFIRMED |
| 10 | `hbm_any` | 213 (`0xD5`) | — | CONFIRMED |
| 11 | `timem` | 214 (`0xD6`) | — | CONFIRMED |
| 12 | `simem` | 215 (`0xD7`) | — | CONFIRMED |
| 13 | `iova` | 216 (`0xD8`) | — | CONFIRMED |
| 14 | `sflag_tile` | 217 (`0xD9`) | → **204** if `!execute-seq` | CONFIRMED |
| 15 | `spmem_any` | 218 (`0xDA`) | — | CONFIRMED |
| 16 | `smem_tile` | 219 (`0xDB`) | → **0** if `!execute-seq` | CONFIRMED |
| 17 | `mar` | 220 (`0xDC`) | — | CONFIRMED |
| 18 | `tile_spmem_cb` | 501 (`0x1F5`) | — | CONFIRMED |
| 19 | `smem_cb` | 502 (`0x1F6`) | — | CONFIRMED |
| 20 | `sflag_scs` | 223 (`0xDF`) | → **204** if `!core-seq` | CONFIRMED |
| 21 | `smem_scs` | 224 (`0xE0`) | → **0** if `!core-seq` | CONFIRMED |
| 22 | `sflag_tc` | 204 (`0xCC`) | **always 204** | CONFIRMED |

The address-space ID is used **directly** as the LLVM `addrspace` — there is no remap. Sweeping `LLVMPointerType::get` immediates across the SC lowering band `0x13530000..0x135c0000` recovers exactly these IDs as literals: `0xCA` (Spmem), `0xCB` (HBM), `0xCC` (Sflag, ×5), `0xD0` (Dreg), `0xD3` (SflagAny, ×4), `0xD4` (SmemAny), `0xD5` (HBMAny), `0xDB` (TileSmem), `0xE1` (SflagAnySynctile). The pool/`MemorySpace`/on-tile semantics of each ID belong to the [fat-pointers page](../sparsecore/fat-pointers-as789.md).

### The sequencer-context flatten — the elide-vs-emit collapse set

After `MemorySpaceToAddressSpace` produces the base ID, the `lowerFunc` lambda applies an override jump table (`0xae4633c`, indexed by `MS − 14`, 9 entries) gated by two closure booleans captured from the function being lowered:

```text
b0 = !ScDialect::HasExecuteSequencerTypeAttribute(fn)   // 0x1459a020  (TEC execute-lane context?)
b1 = !ScDialect::HasCoreSequencerTypeAttribute(fn)      // 0x14599ec0  (core-sequencer context?)
```

The per-`MemorySpace` override, byte-decoded from the jump-table targets:

| MS | space | base ID | override |
|---|---|---|---|
| 14 | `sflag_tile` | 217 | → 204 (Sflag) if `b0` (not execute-seq) |
| 16 | `smem_tile` | 219 | → 0 (Smem) if `b0` (not execute-seq) |
| 20 | `sflag_scs` | 223 | → 204 (Sflag) if `b1` (not core-seq) |
| 21 | `smem_scs` | 224 | → 0 (Smem) if `b1` (not core-seq) |
| 22 | `sflag_tc` | 204 | **always** 204 (TC sflag is generic sflag) |
| 15/17/18/19 | `spmem_any`/`mar`/`tile_spmem_cb`/`smem_cb` | — | **no override** (keep base) |

The intent is a context-sensitive flatten: **outside** an execute-sequencer or core-sequencer function, the per-tile (`sflag_tile`, `smem_tile`) and per-SCS (`sflag_scs`, `smem_scs`) spaces collapse onto the generic `Sflag` (204) / `Smem` (0) pointer types — there is no per-tile or per-SCS bank to address when the code is not running on that sequencer. The `tc`-sflag (MS 22) is unconditionally generic `Sflag`.

This flatten *is* the [`MemorySpaceCast`](../sparsecore/addrspacecast-isel.md) elide-vs-emit decision. The cast lowering elides an `addrspacecast` exactly when its source and destination `MemorySpace`s map — **post-flatten** — to the *same* ID, which happens precisely for `{sflag_tile, sflag_scs, sflag_tc, sflag} → 204` and `{smem_tile, smem_scs, smem} → 0` in non-sequencer functions, and never otherwise. The companion lambda in `lowerAsserts` (`0x135b6f80`) is the identical map **without** the override band — assertion lowering always uses the un-flattened (per-tile/per-SCS) IDs, because the assert text wants to name the exact bank.

> **QUIRK —** the converter is stateful in the booleans `b0`/`b1` — the *same* `MemorySpace` lowers to a *different* `ptr<N>` depending on which sequencer's function it appears in. `smem_tile` is `ptr<219>` inside a TEC execute-lane function and `ptr<0>` everywhere else; `smem_scs` is `ptr<224>` inside an SCS function and `ptr<0>` everywhere else. A reimplementation must capture the enclosing function's sequencer-type attributes before converting any memref. The two booleans are stored at `-0xf0(rbp)` in `lowerFunc` (set @ `0x135685d3`). The sequencer-type attributes themselves are documented on [GetSequencerType](../sparsecore/getsequencertype.md).

---

## The `CheckAddressSpaces` Legality Matrix

### The one simple-DMA gate

`CheckAddressSpaces(SparseCoreTarget& tgt, Operation* op, int srcAS, int dstAS)` (`0x135b8e00`) is the *single* address-space legality gate the SC lowering runs, and it has exactly one caller. It enforces the SparseCore simple-DMA data-movement contract: **a simple-tier DMA whose source OR destination is the generic SMEM space (address-space 0) is only legal when issued from the Scalar Core Sequencer (SCS)** — unless the hardware advertises native tile/SMEM DMA. The decompiled body resolves to a short-circuit OR of three conditions; if all three fail it emits an error and returns failure:

```c
__int64 CheckAddressSpaces(SparseCoreTarget *tgt, Operation *op, int srcAS, int dstAS) {
  result = 1;                                          // assume legal
  if (!tgt->vtable[+0xd8]()) {                         // (1) SupportsTileSmemDma() ?
    fn = walkParentsTo<LLVM::LLVMFuncOp>(op);          //     enclosing llvm.func
    attr = fn.getInherentAttr("sc.sequencer", 12);     // (2) sc.sequencer attribute
    s    = StringAttr::getValue(attr);
    bool isScs = (s.size == 3) &&                       //     "scs": 0x6373='sc', 0x73='s'
                 ((s[0..1] ^ 0x6373) | (s[2] ^ 0x73)) == 0;
    if (!isScs && (srcAS == 0 || dstAS == 0)) {        // (3) neither endpoint is SMEM ?
      op->emitError("Simple DMAs on SMEM only supported on SCS");  // 41 chars
      result = failure;
    }
  }
  return result;
}
```

### Table D — the legality conditions (short-circuit OR, top to bottom)

| # | condition (any one ⇒ legal) | source | Confidence |
|---|---|---|---|
| 1 | `tgt.SupportsTileSmemDma()` | vtable `+0xd8`; VF = false, GL = false | CONFIRMED |
| 2 | enclosing `llvm.func`'s `sc.sequencer` inherent attr == `"scs"` | `getInherentAttr("sc.sequencer", 12)` → 3-char cmp | CONFIRMED |
| 3 | `srcAS != 0` **AND** `dstAS != 0` (neither endpoint is generic SMEM) | the two `int` params (post-cast Table-A IDs) | CONFIRMED |
| — | else → `emitError("Simple DMAs on SMEM only supported on SCS")` → failure | error string @ `0x91b1ca3` | CONFIRMED |

The decompile pins each arm: vtable slot `+216` (`= 0xd8`) is `SupportsTileSmemDma` (line gate at `*(this+216)(this)`); the parent walk stops at `mlir::detail::TypeIDResolver<mlir::LLVM::LLVMFuncOp>` (the enclosing `llvm.func`); the `"scs"` compare is the literal `(s[0..1] ^ 0x6373) | (s[2] ^ 0x73)` with `s.size == 3`; and the failure arm builds the 41-byte string `"Simple DMAs on SMEM only supported on SCS"`.

### Both current gens lack native tile/SMEM DMA

`SupportsTileSmemDma` returns **false** on both shipping SparseCore targets — `ViperfishSparseCoreTarget` (`0x1d49c8e0`, `xor eax,eax`) and `GhostLiteSparseCoreTarget` (`0x1d499460`, `xor eax,eax`), reached through their vtables' `R_X86_64_RELATIVE` addends at `0x21cc9078` and `0x21cc86f8`. So on every current generation, condition (1) is dead, and the effective rule is condition (2) ∨ (3): **simple DMA touching SMEM is legal only from the SCS.** This is the IR-side twin of the SCS being the SparseCore's scalar control engine — see [SCS Engine](../sparsecore/scs-engine.md).

### The single call site and the address-space normalisation

The only caller is `DmaSimpleStartOpLowering::matchAndRewrite` (`0x135a9100`), with the call at `0x135a977a`. Critically, the `srcAS`/`dstAS` the gate sees are the **post-cast normalised** IDs: immediately before the call, `CastTileSmemPointerToSmem` (`0x135b86e0`) casts any tile-resident pointer down to generic SMEM (via `tpu_addrspacecast_smem` taking a `tpu_tileid` window operand — see [Tile-ID Cast](../sparsecore/tile-id-cast.md)) and writes the resulting address-space integers into the out-params the gate consumes. So a `tile_spmem`-resident DMA endpoint is first flattened to SMEM, *then* the "SMEM ⇒ SCS only" gate applies. The two-stage DMA lowering this gate sits inside is documented on the [LowerToMlo DMA bridge-cast](lower-to-mlo-dma-bridge.md).

> **GOTCHA —** the gate cares about the *generic SMEM* space (address-space 0), not about SMEM-family spaces in general. A DMA between two non-zero IDs (`spmem`/`hbm`/`tile_spmem`/…) is **unconditionally** legal regardless of sequencer — condition (3) passes the moment both endpoints are non-zero. The "SCS only" restriction is specifically the generic-SMEM (`ptr<0>`) case, which is why `CastTileSmemPointerToSmem` running first matters: it is what *creates* the `ptr<0>` endpoints the gate then guards.

---

## The EUP Instantiation Roster

### Two families, one source-op fan-in

The SparseCore EUP (Extended Unary Processor) lowering surface is **42 type-converted instantiations** in two templated pattern families, both registered by the same `LowerToSparseCoreLlvm` pass and both operating on the converted (LLVM-dialect) types:

- **`UnaryFloatVectorOpLowering<Src, tpu_*_macro>`** — 12 instantiations. The 1:1 path: the operand already fits a single EUP lane width, so the body emits one EUP **push+pop macro** intrinsic. Algebra: get operand + result type → `FilterLLVMAttributes` (drop `access_groups`, `0x135b7a20`) → `tpu_X_macro::create(b, loc, {resT}, {operand}, attrs)` → `replaceOp`. The `_macro` intrinsic is the EUP VALU3 push + result pop pair.
- **`AluEpOpLowering<Src, Compute, UnpackF, PackF>`** — 30 instantiations. The 1:N path: the operand is a packed sub-element vector (e.g. two `bf16` in a 32-bit lane), so the body **unpacks** the wide vreg into a deque of narrow sub-element values (`UnpackOperand<UnpackOp>`, `0x1360fac0`), re-emits the `ComputeOp` per piece, then **repacks** (`PackResults<PackOp>`, `0x13610940`).

The same source op can appear in **both** families: `sparse_core::RsqrtOp` has a `UnaryFloatVector` instantiation (`0x1357e540`) *and* an `AluEp` instantiation (`0x135e1c80`). The `IsDynamicallyLegal` predicate (below) decides which one fires for a given operand type. The AluEp family is therefore the general sub-element staging wrapper for *any* elementwise math/arith op — not only transcendentals — with the transcendentals being the subset that *also* has the fast 1:1 macro form.

### Table B — `UnaryFloatVectorOpLowering` roster (12; 1:1 EUP push+pop macro)

Each row's source op fans into the macro intrinsic at the body tail. All 12 `matchAndRewrite` symbols and their template parameters are confirmed in the decompile.

| `matchAndRewrite` @VA | source op | → EUP macro intrinsic | EUP selector | Confidence |
|---|---|---|---|---|
| `0x1357e2c0` | `math::TanhOp` | `tpu_tanh_macro` (`0x14988180`) | Tanh `0x13`/`0x1b` | CONFIRMED |
| `0x1357e540` | `sparse_core::RsqrtOp` | `tpu_rsqrt_macro` (`0x14735840`) | ReciprocalSqrt `0x10`/`0x0c` | CONFIRMED |
| `0x1357e880` | `math::Log2Op` | `tpu_log2_macro` (`0x14730640`) | LogTwo `0x12`/`0x1a` | CONFIRMED |
| `0x1357eb00` | `sparse_core::ReciprocalOp` | `tpu_rcp_macro` (`0x147346c0`) | Reciprocal `0x15`/`0x1d` | CONFIRMED |
| `0x1357ef40` | `sparse_core::Log2Op` | `tpu_log2_macro` (`0x14730640`) | LogTwo `0x12`/`0x1a` | CONFIRMED |
| `0x1357f380` | `sparse_core::Pow2Op` | `tpu_pow2_macro` (`0x147339c0`) | PowTwo `0x11`/`0x19` | CONFIRMED |
| `0x1357f6c0` | `math::SinOp` | `tpu_sin_macro` (`0x14736880`) | Sinq `0x17`/`0x1e` | CONFIRMED |
| `0x1357f840` | `math::CosOp` | `tpu_cos_macro` (`0x146d8540`) | Cosq `0x18`/`0x1f` | CONFIRMED |
| `0x1357fac0` | `sparse_core::VsinqOp` | `tpu_sin_macro` (`0x14736880`) | Sinq `0x17`/`0x1e` | CONFIRMED |
| `0x1357ff00` | `sparse_core::VcosqOp` | `tpu_cos_macro` (`0x146d8540`) | Cosq `0x18`/`0x1f` | CONFIRMED |
| `0x13580240` | `math::ErfOp` | `tpu_erf_macro` (`0x1472efa0`) | Erf `0x0e`/`0x0f` | CONFIRMED |
| `0x135804c0` | `sparse_core::VsigshftOp` | `tpu_sigshft` (`0x147365e0`) | ShiftedSigmoid `0x14`/`0x1c` | CONFIRMED |

Eight distinct macros — `rsqrt`, `rcp`, `tanh`, `sin`, `cos`, `erf`, `log2`, `pow2` — plus the bare `tpu_sigshft`. The `math::` and `sparse_core::` source ops fan into the *same* macro: `math.tanh` and a `sc.tanh` both reach `tpu_tanh_macro`; the `sc::Vsinq`/`Vcosq`/`Vsigshft` ops are the EUP-native dialect forms.

### Table C — `AluEpOpLowering` roster (30; 1:N unpack → compute → pack)

Each `AluEpOpLowering<Src, Compute, Unpack, Pack>` unpacks the operand, re-emits the `Compute` op per sub-element piece, and repacks. Column 3 is the re-emitted `Compute` op; the pack family (`F`=float, `SI`=signed-int, `UI`=unsigned-int) is the `Unpack`/`Pack` template pair. All 30 template signatures are confirmed in the decompile.

| `matchAndRewrite` @VA | source op | re-emitted Compute | pack | class | Confidence |
|---|---|---|---|---|---|
| `0x135de780` | `math::RsqrtOp` | `math::RsqrtOp` | F | transcendental | CONFIRMED |
| `0x135df200` | `math::ExpOp` | `math::ExpOp` | F | transcendental | CONFIRMED |
| `0x135dfca0` | `math::Log2Op` | `math::Log2Op` | F | transcendental | CONFIRMED |
| `0x135e0740` | `math::TanhOp` | `math::TanhOp` | F | transcendental | CONFIRMED |
| `0x135e11e0` | `math::FloorOp` | `math::FloorOp` | F | rounding | CONFIRMED |
| `0x135e1c80` | `sparse_core::RsqrtOp` | `sparse_core::RsqrtOp` | F | transcendental | CONFIRMED |
| `0x135e2720` | `sparse_core::ReciprocalOp` | `sparse_core::ReciprocalOp` | F | transcendental | CONFIRMED |
| `0x135e31c0` | `math::AbsFOp` | `math::AbsFOp` | F | abs | CONFIRMED |
| `0x135e3c60` | `arith::FPToSIOp` | `arith::FPToSIOp` | F→SI | convert | CONFIRMED |
| `0x135e4700` | `math::CeilOp` | `math::CeilOp` | F | rounding | CONFIRMED |
| `0x135e51a0` | `sparse_core::Pow2Op` | `sparse_core::Pow2Op` | F | transcendental | CONFIRMED |
| `0x135e5c40` | `sparse_core::Log2Op` | `sparse_core::Log2Op` | F | transcendental | CONFIRMED |
| `0x135e66e0` | `arith::AddFOp` | `arith::AddFOp` | F | binary float | CONFIRMED |
| `0x135e7160` | `arith::DivFOp` | `arith::DivFOp` | F | binary float | CONFIRMED |
| `0x135e7c00` | `math::CopySignOp` | `math::CopySignOp` | F | binary float | CONFIRMED |
| `0x135e86a0` | `arith::MaximumFOp` | `arith::MaximumFOp` | F | binary float | CONFIRMED |
| `0x135e9140` | `arith::MinimumFOp` | `arith::MinimumFOp` | F | binary float | CONFIRMED |
| `0x135e9be0` | `arith::MulFOp` | `arith::MulFOp` | F | binary float | CONFIRMED |
| `0x135ea680` | `arith::NegFOp` | `arith::NegFOp` | F | unary float | CONFIRMED |
| `0x135eb120` | `arith::SubFOp` | `arith::SubFOp` | F | binary float | CONFIRMED |
| `0x135ebbc0` | `sparse_core::ClampFOp` | `sparse_core::ClampFOp` | F | clamp | CONFIRMED |
| `0x135ec660` | `arith::MaxSIOp` | `arith::MaxSIOp` | SI | binary int | CONFIRMED |
| `0x135ed100` | `arith::MinSIOp` | `arith::MinSIOp` | SI | binary int | CONFIRMED |
| `0x135edba0` | `arith::MaxUIOp` | `arith::MaxUIOp` | UI | binary uint | CONFIRMED |
| `0x135ee640` | `arith::MinUIOp` | `arith::MinUIOp` | UI | binary uint | CONFIRMED |
| `0x135ef0e0` | `arith::MulIOp` | `arith::MulIOp` | SI | binary int | CONFIRMED |
| `0x135efb80` | `arith::AddIOp` | `arith::AddIOp` | SI | binary int | CONFIRMED |
| `0x135f0620` | `arith::SubIOp` | `arith::SubIOp` | SI | binary int | CONFIRMED |
| `0x135f10c0` | `sparse_core::AddSIOp` | `arith::AddIOp` | SI | int alias | CONFIRMED |
| `0x135f1b20` | `sparse_core::AddUIOp` | `arith::AddIOp` | UI | uint alias | CONFIRMED |

Two structural facts the template signatures pin down:

- **The two `sc::` int aliases re-emit `arith::AddIOp`.** `sparse_core::AddSIOp` (`UnpackSIOp`/`PackSIOp`) and `sparse_core::AddUIOp` (`UnpackUIOp`/`PackUIOp`) both lower their Compute op to `arith::AddIOp` — they are SI/UI-flavoured aliases of the integer add, not distinct compute kernels.
- **`Exp` has no EUP macro.** `math::ExpOp` appears *only* in AluEp (`0x135df200`, re-emitting `math::ExpOp`) — there is no `tpu_exp_macro`. Exp is always the polynomial/`pow2`-built form, never a single EUP push, consistent with Exp being absent from the EUP-native function set.

The shared unpack/pack layer:

```text
UnpackOperand<UnpackFOp>  0x1360fac0      PackResults<PackFOp>   0x13610940
UnpackOperand<UnpackSIOp> 0x13610080      PackResults<PackSIOp>  0x13611280
UnpackOperand<UnpackUIOp> 0x136104e0      PackResults<PackUIOp>  0x13610de0
GetUnpackResultElementType                0x1360ff20
```

The 1:N body was decoded on `AluEp<math::ExpOp>` (`0x135df200`): `math::ExpOp::create` (`0x1782be40`) is invoked twice — once as a type probe and once as the per-piece loop body — bracketed by `UnpackOperand` and `PackResults`.

### The 1:1-vs-1:N decision — `IsDynamicallyLegal`

The selector between the two families is the `addDynamicallyLegalOp` predicate `IsDynamicallyLegal(Operation*, SparseCoreTarget&, int)` (`0x135ddd20`), installed per-op by `PackedOperandsLowering::AddDynamicallyLegalAluEpOps<Op, UnpackF, PackF>`. It marks the AluEp pattern *legal-as-is* (so AluEp does **not** fire; the 1:1 `UnaryFloatVector` macro path runs) when the operand is **not** a packed sub-element vector, and *illegal* (AluEp **does** fire) when the operand needs sub-element staging. The predicate calls:

```text
ForceBF16ALUOperationsToUnpack(op, type)   0x135dd6e0   (force-unpack BF16 ALU ops)
IsPackedVectorType(type, target, bool)     0x13611720   (packed sub-element layout, e.g. 2×bf16 / 32-bit lane?)
lowering_util::GetVpackFormat(type)        0x13dad800   (VPACK format enum)
element bit-width == 0x10                    @0x135dddc9  (16-bit → bf16/fp16 packed-pair detect)
target vtable *0x780                         @0x135ddda5  (lane-width / pack capability)
target vtable *0x260                         @0x135dde75  (EUP / format support)
```

> **NOTE (HIGH) —** the *structure* of the predicate — "fire AluEp iff the operand is a packed sub-element vector" — is decompile-confirmed via the `IsPackedVectorType` / `ForceBF16ALUOperationsToUnpack` / `GetVpackFormat` calls and the `element-width == 16` test. What is **not** name-resolved is the precise per-gen return of the two target vtable accessors at slots `*0x780` (lane width) and `*0x260` (EUP-format support); they are confirmed *called* but their per-generation values — which would make the unpack-iteration count numeric — are not byte-decoded. This is the one place the page drops below CONFIRMED.

> **NOTE (INFERRED) —** because a transcendental source op is registered in *both* families (e.g. `sc::RsqrtOp` at `0x1357e540` UnaryFloatVector and `0x135e1c80` AluEp), some arbitration must pick one when both could match. The dynamic-legality predicate decides *applicability* (packed operand → AluEp; lane-fitting operand → the 1:1 macro), but the explicit `PatternBenefit` integers and the conversion-target legality-marking order were traced structurally, not byte-decoded; the page treats the dynamic-legality predicate as the effective selector.

### EUP availability is universal on current gens

`SupportsScEupOps` returns **true** on both `ViperfishSparseCoreTarget` (`0x1d49c8c0`, `mov $1, al`) and `GhostLiteSparseCoreTarget` (`0x1d499420`, `mov $1, al`) — both ship the EUP transcendental unit, so the `UnaryFloatVector` macro lowerings are universally available at the IR level. (Whether a *cost-model-cheap* single-µop `sinq`/`cosq` path exists is a separate per-generation question charged downstream of this lowering, not by these patterns.)

---

## Related Components

| Name | Relationship |
|---|---|
| `LowerToSparseCoreLlvmPass::lowerFunc` (`0x13568280`) | builds the converter and installs the AS-attr lambda |
| `MemorySpaceToAddressSpace` (`0x14b78780`) | the forward map the conversion lambda calls (Table A) |
| `LLVM::LLVMPointerType::get` (`0x1746eb40`) | stamps the raw ID onto the descriptor's `!llvm.ptr<ID>` |
| `CheckAddressSpaces` (`0x135b8e00`) | the simple-DMA SMEM-on-SCS legality gate (Table D) |
| `CastTileSmemPointerToSmem` (`0x135b86e0`) | normalises endpoints to SMEM before the gate sees them |
| `IsDynamicallyLegal` (`0x135ddd20`) | the 1:1-vs-1:N selector between the two EUP families |

## Cross-References

- [LowerToSparseCoreLlvm](lower-to-sparsecore-llvm.md) — the per-class rewrite bodies that sit on top of the converted types this page produces.
- [LowerToMlo DMA Bridge-Cast](lower-to-mlo-dma-bridge.md) — the two-stage DMA lowering inside which `CheckAddressSpaces` runs.
- [The `tpu` MLIR Dialect](tpu-dialect-and-ops.md) — the op-registration ABI for the `tpu`/`sparse_core` ops these patterns rewrite into.
- [Compiler Overview](overview.md) — Part V orientation; where SC lowering sits in the five-phase descent.
- [Fat Pointers (AS7/8/9)](../sparsecore/fat-pointers-as789.md) — the AS-id ↔ `MemorySpace` ↔ pool table this converter consumes (do not duplicate).
- [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) — the `MemorySpaceCast` → `llvm.addrspacecast` conversion the flatten collapse-set drives.
- [Tile-ID Cast](../sparsecore/tile-id-cast.md) — the 2-operand `{base, tileId}` cast `CastTileSmemPointerToSmem` emits to normalise tile pointers.
- [SCS Engine](../sparsecore/scs-engine.md) — the Scalar Core Sequencer the `sc.sequencer=="scs"` legality escape names.
- [TEC Engine](../sparsecore/tec-engine.md) — the per-tile execute lane whose sequencer context unlocks the per-tile flatten.
- [GetSequencerType](../sparsecore/getsequencertype.md) — the sequencer-type attributes the flatten booleans (`b0`/`b1`) read.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
