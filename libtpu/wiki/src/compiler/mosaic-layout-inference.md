# Mosaic Layout Inference

> *All addresses, symbols, op-name strings, and error strings on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ.*

## Abstract

Mosaic's layout phase is a **three-pass solver** that runs before the applier ([apply-vector-layout](mosaic-vectorlayout.md)). This page owns those three passes: `InferMemRefLayout` (stage 4), `TilingPropagation` (stage 9), and `InferVectorLayout` (stage 10) of the [16-stage pipeline](mosaic-overview.md#the-tpu-dialect-pass-pipeline-runmlirpasses). Together they compute the physical *memref* tiling and the per-value `in_layout`/`out_layout` `VectorLayout` attributes that the apply pass consumes. The `VectorLayout` value type itself — the `(sublane, lane)` tiling pair, offsets, `ImplicitDim`, `tilesPerVreg`/`tileArrayShape` — lives on [Mosaic VectorLayout](mosaic-vectorlayout.md); this page documents only how those layouts are *chosen*.

The model resembles a dataflow analysis with a semilattice, but with one design decision that drives everything: **`InferVectorLayout` never inserts a relayout.** Where an LLVM pass solving for register classes might split a value or insert a copy, this pass instead picks exactly one `VectorLayout` per value — greedily, operand-driven — and reconciles multiple operand demands with a `VectorLayout::join` meet over the layout lattice. Where reconciliation is genuinely impossible (a transpose feeding a matmul, say), it simply emits a producer `out_layout` that does not match the consumer's `in_layout` and lets the *separate* `RelayoutInsertion` pass (stage 11) bridge the gap. This is why the apply pass can assert `in == out` and blame a missing relayout-insertion run.

The page is structured as the three passes in pipeline order — `InferMemRefLayout` first (it produces the memref `TiledLayoutAttr` everything else reads), then `TilingPropagation` (which threads that tiling to the load/store/DMA sites), then `InferVectorLayout` (the op-TypeID dispatch and per-op rules) — followed by the `join`/`generalizes` lattice they share and an end-to-end worked matmul example.

For reimplementation, the contract is:

- **The memref tiling formula.** `getTilingFactor` — the sublane-tile chooser: `packing = 32/bw`, the `(32/bw)*sublanes` "large 2nd-minor" multiplier table, and the divisibility/power-of-two fallback — plus the `leading_tile_rows` arg-attr override that `inferLayout` applies one level up (bypassing `getTilingFactor` entirely). This is the producer of the tiling the load/store vector rules later match.
- **The propagation fixpoint.** `propagateTiling`'s worklist over a 26-entry op-name→rule `StringMap`, the see-through-`erase_layout` consumer rule, the `memref_slice` tile-stride folding, and the deferred `EraseLayoutOp` removal.
- **The vector op-TypeID dispatch.** `inferBlock`'s ≈35-case TypeID switch (the producer analogue of the apply `StringMap`), the bitwidth-cast pre-dispatch, and the elementwise/extension fallback.
- **The per-op rules.** The shared `bitwidth → native-tiling` skeleton, and the matmul / elementwise / load-store / broadcast / rotate rule bodies to reimplementation grade.
- **The lattice.** `VectorLayout::join` (the meet that picks the more-specific compatible layout) and `generalizes` (the ≤ relation), and why a failed join is the relayout-insertion signal rather than an error.

| | |
|---|---|
| **Pass 1 (stage 4)** | `mlir::tpu::InferMemRefLayoutPass::runOnOperation` @ `0x132c1820` (create: `0x132c0f00`) |
| **Pass 2 (stage 9)** | `mlir::tpu::TilingPropagationPass::runOnOperation` @ `0x132e0dc0` (create: `0x132e0900`) |
| **Pass 3 (stage 10)** | `mlir::tpu::InferVectorLayoutPass::runOnOperation` @ `0x132c3600` (create: `0x132c2c20`) |
| **Vector dispatch core** | `(anon)::VectorLayoutInferer::inferBlock` @ `0x132c3dc0` (~1400 lines decompiled; ≈35-case TypeID switch) |
| **Memref tiling core** | `inferLayout` @ `0x132bef00` → `getTilingFactor` @ `0x132bed80` |
| **Propagation fixpoint** | `propagateTiling` @ `0x132e10a0`; rule map `rules()` @ `0x132e15e0` (26 entries) |
| **Lattice** | `mlir::tpu::VectorLayout::join` @ `0x14a957c0`; `generalizes` (≤); source `layout.h:320` |
| **Inputs/outputs** | reads kernel-arg/alloca memrefs + `leading_tile_rows` arg-attr; writes memref `TiledLayoutAttr` and per-op `in_layout`/`out_layout` ArrayAttrs |
| **Key design rule** | inference picks **one** layout per value; it inserts **no** `tpu.relayout` — `RelayoutInsertion` (stage 11) does that |
| **Confidence** | HIGH (symbol/string-anchored) unless a row or callout says otherwise |

---

## Where the Three Passes Sit

The Mosaic layout phase is four passes; this page owns three of them, and [apply-vector-layout](mosaic-vectorlayout.md) is the fourth. The data flow is strictly producer→consumer:

```text
stage 4   infer-memref-layout    InferMemRefLayoutPass   ── memref args/allocas get a TiledLayoutAttr
              │   (sublane×lane physical tiling + packing tile; insert erase_layout / reinterpret_cast)
              ▼
stage 9   tiling-propagation     TilingPropagationPass   ── push the tiling through erase_layout / slice /
              │   casts to every load/store/DMA; drop the EraseLayoutOps once consumed
              ▼
stage 10  infer-vector-layout    InferVectorLayoutPass   ── every vector op gets in_layout / out_layout
              │   (reads the memref tiling at load/store sites; join-reconciles multi-operand demands)
              ▼
stage 11  relayout-insertion     RelayoutInsertionPass   ── where producer.out_layout != consumer.in_layout,
              │   insert tpu.relayout (so the applier never reconciles)   [not this page]
              ▼
stage 12  apply-vector-layout    ApplyVectorLayoutPass    ── materialize native vregs + shuffles  [not this page]
```

The architectural fact a reimplementer must internalize: stages 4/9/10 are *analysis* — they annotate, they never reshape data. The only IR they mutate are the memref types (stage 4 retypes args/allocas and inserts `erase_layout`/`reinterpret_cast`), the operand wiring around `erase_layout` (stage 9 rethreads and then erases it), and the `in_layout`/`out_layout` attributes (stage 10). All actual vreg materialization and relayout shuffles happen later, in stages 11–12.

---

## Pass 1 — InferMemRefLayout (stage 4)

### Purpose

Assign each kernel-argument and `alloca` memref a `mlir::tpu::TiledLayoutAttr`: the physical sublane×lane tiling plus tile strides, including the sub-32-bit packing tile. This runs *before* the vector passes precisely so that the load/store vector rules in stage 10 have a concrete memref tiling to match their vreg layout against — they do not invent a tiling, they read this one.

### Entry Point

```text
InferMemRefLayoutPass::runOnOperation (0x132c1820)        ── requires hardware_generation (struct +114)
  └─ inferFunc (0x132c0560)                               ── per-func; single-block required
       ├─ inferMemref (0x132bfd60)   [per arg]            ── semaphore→contiguous; else inferLayout
       │    └─ inferLayout (0x132bef00)                   ── the core tiling math
       │         └─ getTilingFactor (0x132bed80)          ── the sublane-tile chooser
       └─ inferOp (0x132c01a0)       [per body op]        ── memref.alloca / tpu.alloca_semaphore results
```

### Algorithm

`inferFunc` retypes the function's memref arguments and any `alloca` results to tiled types, then aliases each through an `erase_layout` (or `reinterpret_cast`) so the rest of the body sees an untiled view until propagation rethreads it:

```c
function inferFunc(func, gen, target_shape, flags):          // 0x132c0560
    require func.body.hasOneBlock()                          // "Functions should only have a single block"
    core_type = TPUDialect::GetCoreTypeAttr(func)
    is_arg_in_smem = (core_type == 0x100000001)              // default MemorySpace for inferMemref
    for arg in func.arguments where isa<MemRefType>(arg):
        lead_rows = consume_arg_attr(arg, "leading_tile_rows")   // i32 override; removed after read
        tiled = inferMemref(arg.type, gen, target_shape, flags,
                            is_arg=1, lead_rows, default_space=is_arg_in_smem)
        set_arg_type(arg, tiled)
        if canReinterpretToUntiledMemref(tiled):            // 2-D, 32-bit, contiguous, ...
            alias = insert tpu.reinterpret_cast(arg)        // fold tile strides into linear strides
        else:
            alias = insert tpu.erase_layout(arg)            // typed-but-untiled body alias
        replaceAllUsesExcept(arg, alias, /*except=*/alias)
    rebuild FunctionType with the new arg types
    for op in func.body: inferOp(op, gen, target_shape, flags, default_space)   // 0x132c01a0
```

`inferOp` handles memref **results**: for `memref.alloca` and `tpu.alloca_semaphore` it calls `inferMemref` on the result memref and wraps it in an `EraseLayoutOp`, then recurses into nested regions. All other ops return success unchanged.

`inferMemref` resolves the memory space, then delegates the tiling decision:

```c
function inferMemref(memref, gen, target_shape, flags, is_arg, lead_rows, space):  // 0x132bfd60
    if memref has a non-tpu MemorySpace attr: return memref          // already placed; leave it
    if elem is semaphore / dma-semaphore:
        return TiledLayoutAttr::getContiguous(...) in space=4        // semaphore_mem, untiled
    layout = inferLayout(memref, ctx, target_shape, flags, is_arg, lead_rows)  // 0x132bef00
    checkTiles(layout)                                               // 0x132bfac0 — validates the tiling
    return MemRefType(elem, shape, layout, resolved_MemorySpaceAttr)
```

`inferLayout` is the core tiling math. The two regimes are 1-D (one tile along the minor axis) and 2-D+ (a sublane tile chosen by `getTilingFactor`, lane tile = `target_lane`), with a packing tile appended for sub-32-bit element types:

```c
function inferLayout(memref, ctx, target_shape, flags, is_arg, lead_rows):  // 0x132bef00
    require target_shape.size() == 1 or == 2                  // infer_memref_layout.cc:112
    bw = bitwidth(elem)
    if layout already isa<TiledLayoutAttr>:
        if lead_rows requested and mismatches existing sublane tiling:
            error "Trying to infer memref layout with sublane tiling <X>, but the memref"
                  " already has sublane tiling <Y>"
        return existing
    if layout isa<AffineMapAttr>: require identity else "Non-identity affine layout"
    elif layout isa<StridedLayoutAttr>: ok
    else: error "Unrecognized layout annotation"
    require isIntOrFloat(elem)                                // "Invalid element type for memref"
    if shape empty: return TiledLayoutAttr::get(ctx, 0,0,0,0)  // scalar
    lane = target_shape[-1]                                   // e.g. 128
    if rank == 1:
        tile = ((32/bw) << (gen < 4)) * lane                 // {2,...} packing descriptor 0x200000000
        if bw < 32: append packing tile (32/bw, 1)
        return single-tile layout along the minor axis
    else:                                                     // rank >= 2
        if lead_rows != 0:                                    // leading_tile_rows arg-attr overrides
            sublane = lead_rows
        else:
            sublane = getTilingFactor(shape[-2], gen, target_shape[0], flags, bw, is_arg, /*1d=*/0)
        L = TiledLayoutAttr::getContiguous(ctx, {sublane, lane}, shape)   // computes tile strides
        if bw < 32: append packing tile (32/bw, 1)
        return L
    // non-power-of-2 or bw > 32 → "Unsupported bitwidth: <bw>"
```

`getTilingFactor` chooses the sublane tile. Its third parameter is the **target sublane count** (`target_shape[0]`, normally 8) — `inferLayout` passes `*a3` here, *not* `leading_tile_rows`. The packing factor `32/bw` is the floor; a per-bitwidth "large 2nd-minor" multiplier (the packing factor times the target sublane, gated by three `TpuTilingFlags` bytes, `is_arg`, and `gen`) can raise it; and divisibility/size fallbacks keep it legal for the actual dimension. The `leading_tile_rows` arg-attr is handled one level up in `inferLayout` — when present it sets the sublane tile directly and `getTilingFactor` is never called for that arg:

```c
function getTilingFactor(dim, gen, sublanes, flags, bw, is_arg, is_1d):   // 0x132bed80
    require isPowerOf2_32(bw)                                 // infer_memref_layout.cc:53
    require 2 <= bw <= 32                                     // :54 / :55
    packing = 32 / bw
    base    = max(packing, sublanes)
    factor  = base
    if not is_1d:                                             // "large 2nd-minor" multiplier
        switch tzcnt(bw):                                     // bw2→1, bw4→2, bw8→3, bw16→4
            case bw==2 : candidate = 16 * sublanes            // unconditional
            case bw==4 : candidate =  8 * sublanes  if flags[2]
            case bw==8 : candidate =  4 * sublanes  if flags[1]
            case bw==16: candidate =  2 * sublanes  if (flags[0] or (not is_arg and gen >= 6))
        if candidate set: factor = candidate
    if dim % factor != 0: factor = base                       // divisibility fallback
    if dim < factor:                                          // walk powers of two up
        f = packing << (gen < 4)
        while f < min(dim, base): f *= 2
        factor = f
    return factor
```

The multiplier is `(32/bw) * sublanes` in every arm — i.e. it widens the sublane tile so the tile holds a full vreg's worth of packed sub-elements. For the common `sublanes == 8`: bw=2 → 128, bw=4 → 64, bw=8 → 32, bw=16 → 16; for f32 (bw=32) the switch is not entered and the tile stays at `base = max(1, 8) = 8`.

> **GOTCHA — the memref tiling is generation- and arg-dependent, not just bitwidth-dependent.** `getTilingFactor` reads `gen`, three `TpuTilingFlags` bytes, the `is_arg` flag, and the target sublane count. A reimplementation that derives the sublane tile from `32/bw` alone will produce the right answer for f32 (factor 8) but the wrong one for bf16/int8/int4, where the "large 2nd-minor" arm widens the tile (bf16 with `sublanes=8` → `2*8 = 16`) when the flags and divisibility permit. The gating differs per bitwidth: the bw=2 arm is unconditional, bw=4 needs `flags[2]`, bw=8 needs `flags[1]`, and bw=16 takes the wide tile when `flags[0]` is set *or* when the memref is a non-arg on `gen >= 6` (i.e. `flags[0] or (not is_arg and gen >= 6)`). The flags are recovered as `flags[0..2]`; their human names are inferred from use (MEDIUM).

> **QUIRK — `erase_layout` vs `reinterpret_cast` is decided by an un-decompiled predicate.** `inferFunc` inserts a `tpu.reinterpret_cast` (collapsing tiles into a linear-strided untiled view) when `canReinterpretToUntiledMemref` holds, else a `tpu.erase_layout` (a typed-but-untiled alias). The predicate's call sites are recovered; its body (the 2-D/32-bit/contiguous eligibility test) was not decompiled. Treat the choice as: reinterpretable contiguous 32-bit 2-D → `reinterpret_cast`; everything else → `erase_layout` (LOW on the exact predicate).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `InferMemRefLayoutPass::runOnOperation` | `0x132c1820` | pass entry; requires `hardware_generation` (+114); calls `inferFunc` | HIGH |
| `createInferMemRefLayoutPass` | `0x132c0f00` | factory (`gen`, target span, `TpuTilingFlags`); pass struct `0x328` B | HIGH |
| `inferFunc` | `0x132c0560` | per-func arg retype + `erase_layout`/`reinterpret_cast` insertion | HIGH |
| `inferOp` | `0x132c01a0` | memref-result inference for `alloca`/`alloca_semaphore` | HIGH |
| `inferMemref` | `0x132bfd60` | semaphore→contiguous; else `inferLayout` + `checkTiles` | HIGH |
| `inferLayout` | `0x132bef00` | the 1-D/2-D/packing tiling math | HIGH |
| `getTilingFactor` | `0x132bed80` | the sublane-tile chooser (the formula above) | HIGH |
| `checkTiles` | `0x132bfac0` | validates the resolved tiling | MEDIUM |

---

## Pass 2 — TilingPropagation (stage 9)

### Purpose

`InferMemRefLayout` placed each tiled memref behind an `erase_layout`/`reinterpret_cast` alias, so the body still references an *untiled* view. `TilingPropagation` is the fixpoint that pushes the physical tiling forward to every load / store / DMA / memref-shape op — rewriting their memref operand to reference the **tiled** memref directly (seeing *through* the `erase_layout`) — and then deletes each `EraseLayoutOp` once it has no remaining uses. After this pass, every memory op references a tiled memref, which is what the stage-10 load/store vector rules read.

### Entry Point

```text
TilingPropagationPass::runOnOperation (0x132e0dc0)         ── builds a PropagationContext from options
  └─ propagateTiling(ctx, entry_block) (0x132e10a0)        ── the worklist fixpoint
       ├─ rules() (0x132e15e0)                             ── 26-entry op-name → rule StringMap
       ├─ propagate_layout_to_consumer_rule (0x132e40a0)   ── 20 consumer ops: see-through erase_layout
       └─ tpu_memref_slice_rule (0x132e1b20)               ── slice: re-thread + fold tile strides
```

The `PropagationContext` carries `{target_sublane, target_lane}` (struct `+0x150`), a collected-`EraseLayoutOps` vector (`+16`), and the `sparse_core` `cl::opt` bool (struct `+472`). `createTilingPropagationPass({sublane,lane}, sparse_core)` (`0x132e0900`) builds a `0x228`-byte pass nested on `func.func`.

### Algorithm

```c
function propagateTiling(ctx, block):                        // 0x132e10a0
    for op in block, IN ORDER:
        if isa<EraseLayoutOp>(op):
            ctx.erase_list.push(op)                          // defer removal to the end
        for region in op.regions: for b in region:
            propagateTiling(ctx, b)                          // depth-first into nested regions
        rule = rules().lookup(op.name)                       // xxh3_64 + StringMapImpl::FindKey
                                                             // miss → StringMap default-bucket entry
        if rule(ctx, op) == failure: return failure
    for erase_op in ctx.erase_list:                          // cleanup
        if erase_op.use_empty() or all_uses_in_sc_tpu(erase_op):  // "sc_t"/"up" literal test
            erase_op.erase()
        else:
            return error "Failed to propagate the layout to all operations"
    return success
```

The "all uses in `sc_tpu`" predicate is the literal-string test `(*str ^ 0x745F6373) | (*(str+4) ^ 0x7570)` — i.e. the op-name prefix equals `"sc_t"` then `"up"` (`sc_tpu`). SparseCore ops legitimately retain the untiled alias, so they do not block erasure.

The central rule sees through `erase_layout`:

```c
function propagate_layout_to_consumer_rule(ctx, op):          // 0x132e40a0
    for operand in op.operands:
        def = operand.getDefiningOp()
        if isa<EraseLayoutOp>(def):
            op.setOperand(operand_index, def.getOperand(0))   // use the TILED memref directly
    return success                                            // always succeeds
```

The hard rule is `tpu_memref_slice_rule`, which must re-tile the slice and, when it collapses the 2nd-minor dim of a 4-level-tiled 32-bit memref, fold the tile strides into the slice's base index with emitted arithmetic:

```c
function tpu_memref_slice_rule(ctx, slice):                   // 0x132e1b20
    src = slice.source
    if not isa<EraseLayoutOp>(src.getDefiningOp()): return success   // no-op
    tiled_src = src.getDefiningOp().getOperand(0)
    verifyOffsetAndSizeTileAlignment(slice)
    if collapses 2nd-minor of a 4-level-tiled 32-bit memref:
        // fold the tile strides into the physical base offset:
        new_idx = (idx / tile_size) * tile_stride + (idx % tile_size)
        // emitted via arith.constant + divui + remui + muli + addi (createOrFold)
        require isGuaranteedDivisible(off, stride, 128)
    elif contiguous & reinterpretable:
        require isGuaranteedDivisible(off, 8, 128)            // "Slice offset for 32-bit 1D
                                                             //  memrefs must be a multiple of 8"
        emit ReinterpretCastOp over an untiled TiledLayoutAttr
    new_slice = rebuild MemRefSliceOp on tiled_src
    result = wrap new_slice in a fresh EraseLayoutOp; ctx.erase_list.push(it)
    return success
```

The other five memref-shape rules (`squeeze`/`reshape`/`bitcast`/`reinterpret_cast`/`memref.cast`, `0x132e2a60`..`0x132e3ee0`) similarly re-thread the tiled memref through the cast and recompute the resulting tile strides; their per-rule stride math was sampled, not individually decompiled (HIGH).

### The 26-Entry Rule Map

`rules()` (`0x132e15e0`) is an op-name→rule `StringMap` — the propagation-typed sibling of the [apply pass's 49-entry rewrite map](mosaic-vectorlayout.md). Rather than dump 26 near-identical rows, the shape is: **6 memref-shape ops carry bespoke rules; the other 20 "consumer" ops share `propagate_layout_to_consumer_rule`.**

| Group | Op-names | Rule fn | Address |
|---|---|---|---|
| memref slice | `tpu.memref_slice` | `tpu_memref_slice_rule` | `0x132e1b20` |
| memref squeeze | `tpu.memref_squeeze` | `tpu_memref_squeeze_rule` | `0x132e2a60` |
| memref reshape | `tpu.memref_reshape` | `tpu_memref_reshape_rule` | `0x132e33a0` |
| memref bitcast | `tpu.memref_bitcast` | `tpu_memref_bitcast_rule` | `0x132e37a0` |
| reinterpret cast | `tpu.reinterpret_cast` | `tpu_reinterpret_cast_rule` | `0x132e3e60` |
| memref cast | `memref.cast` | `memref_cast_rule` | `0x132e3ee0` |
| **consumers (×20)** | `tpu.{load,store,strided_load,strided_store,vector_load,vector_store,vector_load_idx,vector_store_idx,enqueue_dma,enqueue_indirect_dma,wait_dma2,wait_indirect_dma,log_buffer,fetch_and_add_sync,sem_signal,sem_wait,sem_read}`, `memref.{store,load,reinterpret_cast}` | `propagate_layout_to_consumer_rule` | `0x132e40a0` |

The four op-name strings `tpu.vector_store`, `tpu.enqueue_indirect_dma`, `tpu.wait_indirect_dma`, `tpu.fetch_and_add_sync` were resolved from `.rodata` at `0x869EF8B` / `0x879F7AE` / `0x879F798` / `0x8730A53`.

> **NOTE — the dispatch machinery is shared with the apply pass.** The lookup is `xxh3_64bits` + `StringMapImpl::FindKey` with a default-bucket fallback on miss — identical to the apply pass's `applyLayoutOp` `StringMap`. A reimplementer can use one `StringMap` implementation for both producer-side maps.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TilingPropagationPass::runOnOperation` | `0x132e0dc0` | pass entry; builds `PropagationContext` | HIGH |
| `createTilingPropagationPass` | `0x132e0900` | factory (`{sublane,lane}`, `sparse_core`); struct `0x228` B | HIGH |
| `propagateTiling` | `0x132e10a0` | worklist fixpoint + deferred `EraseLayoutOp` removal | HIGH |
| `rules()` | `0x132e15e0` | 26-entry op-name→rule `StringMap` | HIGH |
| `propagate_layout_to_consumer_rule` | `0x132e40a0` | see-through `erase_layout` for 20 consumer ops | HIGH |
| `tpu_memref_slice_rule` | `0x132e1b20` | slice re-tile + tile-stride folding | HIGH |
| `tpu_memref_{squeeze,reshape,bitcast}_rule` | `0x132e2a60`/`0x132e33a0`/`0x132e37a0` | re-thread cast + recompute strides | HIGH |
| `tpu_reinterpret_cast_rule` / `memref_cast_rule` | `0x132e3e60` / `0x132e3ee0` | re-thread cast + recompute strides | HIGH |

---

## Pass 3 — InferVectorLayout (stage 10)

### Purpose

Annotate every op with an `in_layout` ArrayAttr (one `VectorLayout` per operand, `kNoLayout` for scalars) and an `out_layout` ArrayAttr (one per result). This is the producer of the exact attributes the apply pass reads. It is implemented as an op-TypeID switch — the producer-side analogue of the apply pass's op-name `StringMap`, but using direct `TypeIDResolver<>` pointer equality because the inferer is a single translation unit.

### Entry Point

```text
InferVectorLayoutPass::runOnOperation (0x132c3600)        ── requires hardware_generation (pass +114)
  │   constructs a VectorLayoutInferer on the stack:
  │     +0  hardware_generation     +24 target_sublane (8)   +32 target_lane (128)
  │     +TpuTilingFlags (+214/+164/+568), large-2nd-minor bool (+1056), reduce bool (+1138)
  │   requires single-block func ── "Only one block functions supported"
  └─ VectorLayoutInferer::inferBlock(block, fn::return_handler) (0x132c3dc0)   ── per-op driver
```

### Algorithm

`inferBlock` is the per-op driver. For each op until the terminator it runs guards, collects operand layouts, decides scalar-vs-vector, runs the bitwidth-cast pre-dispatch, then the TypeID switch, then a fallback:

```c
function inferBlock(block, terminator_handler):              // 0x132c3dc0
    for op in block until terminator:
        // 1. pre-attached guard
        if (op has in_layout or out_layout) and not isa<AssumeLayoutOp>(op):
            error "layout attributes already attached"
        if isa<AssumeLayoutOp>(op):
            require op has both layouts                       // "expect layout attributes in tpu::AssumeLayoutOp"
            continue                                          // pass through

        // 2. collect operand layouts (skipped for broadcast / extract_strided_slice)
        if not isa<vector::BroadcastOp, ExtractStridedSliceOp>(op):
            operand_layouts = getLayoutFromOperands(op)       // 0x132c59a0; kNoLayout for scalars
        else:
            set inferer+44 lane-replicate flag if an operand is replicated along an axis

        // 3. scalar test — no VectorType among operands OR results
        if no operand and no result is a VectorType:
            setInLayout(op, kNoLayout per operand)            // 373/374 CHECK path
            if op.getNumResults() == 0: continue

        // 4. bitwidth-cast pre-dispatch  (extsi/extf/sitofp/uitofp/trunci/truncf/fptosi/fptoui/extui)
        if isConversionOp(op):
            bin  = operand_bitwidth(op)                       // Float8EXMY counts as 8; i1 special
            bout = result_bitwidth(op)
            if   bout > bin:  return inferExt(op)              // 0x132c5be0  widening
            elif bout < bin:  return inferTrunc(op)            // 0x132c6600  narrowing
            else:             return inferElementwise(op)      // extui i1 (bout == bin)

        // 5. select / cmp guards
        if isa<arith.select, arith.cmpi, arith.cmpf>(op):
            return inferElementwise(op)                        // "Only one side of arith/cmp is a vector?"

        // 6. op-TypeID switch (≈35 cases) → infer(<Op>)        [table below]
        switch TypeID(op): ...

        // 7. fallback
        else if hasElementwiseMappableTraits(op): inferElementwise(op)
        else if extensions::canInferVectorLayout(op):          // 0x13246280
                 extensions::inferVectorLayout(op)             // 0x132462a0  out-of-tree ops
        else: error "Not implemented: Unsupported operation: <op> in infer-vector-layout pass"

        // 8. post-checks
        re-assert 373/374 CHECKs; clear inferer+44 lane-replicate flag
```

The two invariants re-asserted per op (`infer_vector_layout.cc:373/374`) are: every op with results carries an `out_layout`; every op with operands carries an `in_layout`. `setInLayout` (`0x14b75c60`), `setOutLayout` (`0x14b75e40`), and `setLayout` (`0x14b75fa0`/`0x14b75fe0`/`0x14b76020`) are the free functions that write the ArrayAttrs; `kNoLayout` is a module-global sentinel `VectorLayout` marking a scalar/non-vector operand slot.

> **QUIRK — `broadcast` and `extract_strided_slice` skip operand-layout collection on purpose.** For these two ops, `inferBlock` does *not* call `getLayoutFromOperands`; instead it sets the inferer's `+44` lane-replicate flag when an operand is replicated/singleton along an axis. That flag makes the subsequent `getLayout` read force a lane-replicated layout for the source. A reimplementation that runs the uniform operand-collection path for these ops will read the wrong source layout.

> **QUIRK — extension layout is chosen by packing direction, not op name.** `arith.extui` of an `i1` has `bout == bin` and is routed to `inferElementwise`, while `arith.extsi`/`tpu.extf` widen (`inferExt`) and `arith.trunci`/`tpu.truncf` narrow (`inferTrunc`). The dispatcher compares the operand and result bitwidths and ignores whether the op is named ext or trunc — so sign-extension and fp-cast layout follow the change in sub-32-bit packing, not the spelling.

### The Op-TypeID Dispatch Table

The switch has ≈35 cases. The producer-side parallel of the apply pass's 49 rewrite rules, it maps each op TypeID to an `infer(<Op>)` method. Several ops share a method (noted), and four ops are handled inline:

| Op | infer method | Address | Note |
|---|---|---|---|
| `arith.constant` | `infer(arith::ConstantOp)` | `0x132c78c0` | |
| `cf.assert` | inline | — | `setInLayout = kNoLayout` |
| `memref.load` | `infer(memref::LoadOp)` | `0x132c7de0` | distinct from `tpu.load` |
| `tpu.load` | `infer(LoadOp)` | `0x132cb6c0` | |
| `tpu.store` | `infer(StoreOp)` | `0x132cbb20` | |
| `tpu.strided_load` / `tpu.strided_store` | `infer(Strided{Load,Store}Op)` | `0x132cbd80` / `0x132cc240` | |
| `tpu.matmul` (+ `push_rhs`/`acc_lhs`/`pop`) | `infer(Matmul*Op)` | `0x132cc740` / `0x132cccc0` / `0x132cce00` / `0x132ccf40` | |
| `tpu.rotate` / `tpu.dynamic_rotate` | `infer({Rotate,DynamicRotate}Op)` | `0x132ca600` / `0x132ca840` | |
| `tpu.concatenate` | `infer(ConcatenateOp)` | `0x132cacc0` | |
| `tpu.erase_layout` | inline | — | `setLayout = kNoLayout` (in+out) |
| `tpu.iota` | `infer(IotaOp)` | `0x132cd0a0` | |
| `tpu.gather` | `infer(GatherOp)` | `0x132cd380` | distinct from dynamic |
| `tpu.dynamic_gather` | `infer(DynamicGatherOp)` | `0x132cd3e0` | |
| `tpu.reduce_index` | `infer(ReduceIndexOp)` | `0x132cd6e0` | |
| `tpu.bitcast` | `infer(BitcastOp)` | `0x132cdb40` | |
| `tpu.trace` | `infer(TraceOp)` | `0x132ce020` | |
| `tpu.prng_random_bits` | `infer(PRNGRandomBitsOp)` | `0x132ce160` | |
| `tpu.region` | `infer(RegionOp)` | `0x132ce300` | region-carrying |
| `scf.if` / `scf.for` / `scf.while` | `infer(scf::{If,For,While}Op)` | `0x132c8140` / `0x132c8c40` / `0x132c9760` | region-carrying |
| `vector.broadcast` | `infer(vector::BroadcastOp)` | `0x132ce520` | |
| `vector.extract` | `infer(vector::ExtractOp)` | `0x132cece0` | |
| `vector.multi_reduction` | `infer(MultiDimReductionOp)` | `0x132cf660` | |
| `vector.shape_cast` / `tpu.reshape` | `inferReshape` | `0x132d09a0` | shared |
| `vector.extract_strided_slice` | `infer(ExtractStridedSliceOp)` | `0x132d2d80` | skips operand collect |
| `tpu.vector_load` / `tpu.vector_store` | `infer(Vector{Load,Store}Op)` | `0x132cf2c0` / `0x132d2340` | → `inferLoadStoreVectorLayout` |
| `tpu.transpose` | `infer(TransposeOp)` | `0x132d26c0` | |

Only the two *vector* memory ops — `tpu.vector_load` and `tpu.vector_store` — funnel through the shared `inferLoadStoreVectorLayout` (`0x132d48c0`); the scalar `memref.load` / `tpu.load` / `tpu.store` and the `strided_*` ops each have their own `infer(...)` body and do not call it.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `InferVectorLayoutPass::runOnOperation` | `0x132c3600` | pass entry; constructs the `VectorLayoutInferer` | HIGH |
| `createInferVectorLayoutPass` | `0x132c2c20` | factory (`gen`, `{sublane,lane}`, `TpuTilingFlags`, bool) | HIGH |
| `VectorLayoutInferer::inferBlock` | `0x132c3dc0` | per-op TypeID dispatch (~1400 lines decompiled) | HIGH |
| `getLayoutFromOperands` | `0x132c59a0` | collects each operand's producer `out_layout` | HIGH |
| `getLayout` | `0x132d3260` | reads a value's `out_layout` at the result index | HIGH |
| `inferExt` / `inferTrunc` | `0x132c5be0` / `0x132c6600` | widening / narrowing cast layout | HIGH |
| `inferElementwise` | `0x132c70e0` | layout-preserving elementwise rule (uses `join`) | HIGH |
| `inferLoadStoreVectorLayout` | `0x132d48c0` | reads the memref tiling, matches the vreg layout | HIGH |
| `verifyMemoryTiling` | `0x132d3580` | enforces the legal memory-op tiling | HIGH |
| `infer(MatmulOp)` | `0x132cc740` | the MXU-packed operand/acc/result layouts | HIGH |
| `infer(vector::BroadcastOp)` | `0x132ce520` | replicated-axis selection | HIGH |
| `infer(RotateOp)` / `infer(IotaOp)` / `infer(TransposeOp)` / `inferReshape` | `0x132ca600` / `0x132cd0a0` / `0x132d26c0` / `0x132d09a0` | per-op rules | HIGH |
| remaining ≈25 `infer(...)` bodies | `0x132c78c0`..`0x132d2d80` | addresses + signatures recovered; bodies sampled | HIGH |
| `setInLayout` / `setOutLayout` / `setLayout` | `0x14b75c60` / `0x14b75e40` / `0x14b75fa0` | write the `in_layout`/`out_layout` ArrayAttrs | HIGH |
| `extensions::{can,}inferVectorLayout` | `0x13246280` / `0x132462a0` | out-of-tree op fallback | HIGH |

---

## The Per-Op Rule Skeleton

Every per-op rule shares one skeleton. A reimplementer who internalizes this skeleton can reconstruct the ≈25 rules whose bodies were sampled rather than fully decompiled:

- **Bitwidth.** Compute the element bitwidth `bw` of each vector operand/result. `Float8EXMYType` counts as 8; `i1` is special-cased.
- **Native sublane tiling** = `target_sublane * 32 / bw` — the packed tile holding `32/bw` sub-elements per sublane: **8** for f32, **16** for bf16, **32** for int8/fp8, **64** for int4.
- **Native lane tiling** = `target_lane` (128).
- **Offset** = `{0,0}` for a freshly produced value. An axis becomes *replicated* (offset absent) only when the op broadcasts or reduces over it.
- **`implicit_dim`** by rank: rank-2+ → `NONE`(0); 1-D → `MINOR`(1) or `SECOND_MINOR`(2) depending on whether the vector lives on the lane or sublane axis; scalar-promoted → `MINOR_AND_SECOND_MINOR`(3).

The four reimplementation-grade rules and the secondary ones follow.

### infer(MatmulOp) — `0x132cc740`

The matmul rule forces the operands to the packed native tiling the MXU latch expects, and the accumulator/result to the f32 `(8,128)` tiling. It CHECKs `"Expected 32-bit acc in tpu::MatmulOp"` and `"Expected 32-bit result in tpu::MatmulOp"`, then emits three `in_layout`s and one `out_layout`:

```c
function infer(MatmulOp op):                                 // 0x132cc740
    bw_lhs = bitwidth(op.lhs);  bw_rhs = bitwidth(op.rhs)
    require bitwidth(op.acc) == 32   // "Expected 32-bit acc in tpu::MatmulOp"
    require bitwidth(op.res) == 32   // "Expected 32-bit result in tpu::MatmulOp"
    in_layout[lhs] = { bw_lhs, {0,0}, (32*target_sublane/bw_lhs, target_lane), NONE }
    in_layout[rhs] = { bw_rhs, {0,0}, (32*target_sublane/bw_rhs, target_lane), NONE }
    in_layout[acc] = { 32,     {0,0}, (target_sublane,          target_lane), NONE }
    out_layout     = { 32,     {0,0}, (target_sublane,          target_lane), NONE }
    // 32*sublane/bw computed as 32 * inferer[+24] / bw
```

For bf16 operands this yields the packed `16,{0,0},(16,128)` and the f32 result `32,{0,0},(8,128)` — exactly the attributes the [apply pass](mosaic-vectorlayout.md) consumes for its worked bf16 matmul. The MXU tile-cost reasoning for *why* operands must be packed lives on the dot/conv cost page.

### inferElementwise — `0x132c70e0`

Layout-preserving: all non-`i1` vector/scalar operands plus the result must share one bitwidth; the vector operand layouts are folded with `VectorLayout::join` into one common layout that becomes both every vector operand's `in_layout` and the `out_layout`:

```c
function inferElementwise(op):                               // 0x132c70e0
    require op.getNumResults() == 1            // "only one result supported"
    require op.getNumOperands() > 0            // "elementwise ops with no operands unsupported"
    bw = -1
    for v in operands + [result] where v is non-i1 vector/scalar:
        if bw == -1: bw = bitwidth(v)
        else require bitwidth(v) == bw         // "Mismatched bitwidth in elementwise for non-i1 ..."
        require isa<VectorType, scalar>(v)     // "expected only vector and scalar operands"
    L = none
    for vop in vector operands:
        l = getLayout(vop)                     // "missing vector layout"
        L = (L is none) ? l : VectorLayout::join(L, l, shape)
        if join failed: L = native_layout(bw)  // re-derive a fresh native layout
    if no vector operand but result is vector:
        L = { bw, {0,0}, (32*target_sublane/bw, target_lane) }   // synthesize native
    set every vector operand's in_layout = L; out_layout = L; scalar operands → kNoLayout
```

A failed `join` is not an error here — it falls back to a fresh native layout for that operand, and the eventual layout mismatch is bridged later by `RelayoutInsertion`.

### inferLoadStoreVectorLayout — `0x132d48c0`

The load/store rule does **not** invent a tiling — it reads the memref's `TiledLayoutAttr` (set by `InferMemRefLayout`, propagated by `TilingPropagation`) and matches the vreg layout to it:

```c
function inferLoadStoreVectorLayout(memref, vec, indices, ...):  // 0x132d48c0
    require rank(memref) == rank(vec)          // "memref and vector rank mismatch"
    require rank(vec) > 0                       // "rank 0 vectors unsupported"
    bw = bitwidth(elem)                         // "Unsupported bitwidth"
    if rank == 1:
        require 1D tiling                       // "Expected 1D tiling in 1D loads/stores"
                                                // else "Unsupported tiling for 1D load/store"
        layout = { offset {0, base_offset mod (lane*packing)},
                   tiling (1, lane*packing), implicit MINOR(2) }
    else:                                       // rank >= 2
        require 2D tiling                       // "Expected 2D tiling in 2D+ loads/stores"
        if memref 2nd-minor dim <= one sublane-tile OR vec minor dim == 1:
            layout.offset = {0,0}               // sublane-replicated fast path
        else:
            layout.offset = { base_idx mod tile (sublane), base_idx mod tile (lane) }
        if bw==32 and vec minor dim==1 and canReinterpretToUntiledMemref:
            layout = sublane=1 broadcast layout
    verifyMemoryTiling(...)                      // 0x132d3580 (CHECKs below)
    if canReinterpretToUntiledMemref(memref):
        fold tile strides into offset using leading_tile_rows
```

`verifyMemoryTiling` (`0x132d3580`) enforces: `"Loads of types wider than 32-bit unsupported"`, `"Only three-level tiling supported for 1D memory ops narrower than 32-bit"`, `"Invalid first-level tile in 1D memory op"`.

### infer(vector::BroadcastOp) — `0x132ce520`

A scalar source produces a result native layout (rank-1 → `MINOR`(2), else `NONE`) with `in_layout = kNoLayout`. A vector source reads the source layout via `getLayout`, tests equivalence with `VectorLayout::generalizes` both ways, and marks the broadcast axis replicated on the output — keeping each offset only where the source and destination dim sizes match:

```c
function infer(vector::BroadcastOp op):                      // 0x132ce520
    require rank(result) > 0                    // "rank 0 vectors unsupported"
    if scalar source:
        out = native_layout(bw, implicit = rank1 ? MINOR(2) : NONE)
        in_layout = kNoLayout
    else:
        src = getLayout(op.source)              // "missing vector layout"
                                                // "unsupported broadcast source type"
        require generalizes(src, dst) or generalizes(dst, src)   // equivalence
        for axis i: keep offset[i] iff src_dim[i] == dst_dim[i]  // else replicated (absent)
        out = src with broadcast axes replicated
```

### Secondary Rules (rotate, iota, transpose, multi_reduction, reshape)

These follow the same `bitwidth → native-tiling` skeleton; their exact offset/implicit-dim choice was sampled, not each fully decompiled (HIGH):

| Op | Address | Behavior |
|---|---|---|
| `infer(RotateOp)` | `0x132ca600` | 32-bit only (`"not implemented: Rotate with non-32-bit data"`); native `(sublane,lane)`, `in == out` |
| `infer(IotaOp)` | `0x132cd0a0` | native tiling; the iota dim is set replicated (offset absent) on that axis |
| `infer(MultiDimReductionOp)` | `0x132cf660` | native tiling; the reduced dim set replicated on that axis |
| `infer(TransposeOp)` | `0x132d26c0` | swaps the `(sublane,lane)` roles → an `out_layout` that differs from the operand (so `RelayoutInsertion` bridges it) |
| `inferReshape` | `0x132d09a0` | recomputes the implicit-dim; may force a `{0,0}` re-layout |

> **NOTE — the region-carrying ops (`scf.for`/`scf.while`/`scf.if`/`tpu.region`) are not fully traced.** Their entry points are `0x132c8140`/`0x132c8c40`/`0x132c9760`/`0x132ce300`. The cross-iteration fixpoint that unifies iter-arg and yield layouts across a loop body was not decompiled (LOW on those bodies). A reimplementation must add a loop-body layout-unification step that the per-op table above does not capture.

---

## The Lattice: join and generalizes

Because `InferVectorLayout` never inserts a relayout, it reconciles multiple operand layouts with a semilattice on `VectorLayout` (source `layout.h`). The `VectorLayout` struct itself is on [Mosaic VectorLayout](mosaic-vectorlayout.md); here is only the meet and the ≤ relation.

```c
function VectorLayout::join(a, b, shape):                    // 0x14a957c0
    require shape.size() >= layout_rank(implicit_dim)        // layout.h:320
    if a more_general_than b:                                // a replicated where b concrete,
        return b                                             //   same bw/tiling/implicit, singleton dim
    if b more_general_than a:
        return a
    require bitwidth(a) == bitwidth(b)                        // 16-byte tiling pair XOR + ptest
    require implicit_dim(a) == implicit_dim(b)
    out = a
    for each dim:                                            // merge offsets
        if one offset replicated, other concrete: take the concrete
        elif both concrete and equal:                        take it
        else (concrete and different):                       return invalid()   // no join
    return out
```

`generalizes(this, other, shape)` is the ≤ relation (same `layout.h:320` CHECK): true iff `this` is at least as general as `other` — every concrete axis of `this` is matched by `other`, and every replicated axis of `this` admits `other`'s concrete value. `inferElementwise` uses `join`; `infer(BroadcastOp)` uses `generalizes` both ways to test equivalence before marking the broadcast axis replicated.

`getLayout(Value)` (`0x132d3260`) reads a value's layout: it finds the defining op and reads its `out_layout` ArrayAttr at the result index (CHECKs `infer_vector_layout.cc:2152/2154/2157` — `"op"`/`"op_result"`/`"out_attrs.size() > result_index"`). When the inferer's `+44` lane-replicate flag is set, it forces the lane offset to replicated so a broadcast/extract source is read as lane-broadcast.

> **GOTCHA — a failed `join` is a feature, not an error.** When two operands genuinely need different layouts, `join` returns an *invalid* layout (the `_RDI[56] = 0` "no value" flag). `inferElementwise` treats this as the signal to re-derive a fresh native layout, and the resulting producer/consumer mismatch is later bridged by `RelayoutInsertion` (stage 11). A reimplementation that treats a failed join as a hard error will reject kernels that the production compiler compiles fine.

---

## Worked Example: a bf16 Matmul Kernel

The kernel (`{sublane=8, lane=128}`):

```mlir
%a : memref<512x256xbf16, #tpu.memory_space<vmem>>
%b : memref<256x128xbf16, #tpu.memory_space<vmem>>
%o : memref<512x128xf32,  #tpu.memory_space<vmem>>
%va  = tpu.vector_load %a   : vector<512x256xbf16>
%vb  = tpu.vector_load %b   : vector<256x128xbf16>
%acc = tpu.matmul %va, %vb  : vector<512x128xf32>
       tpu.vector_store %acc, %o
```

**Stage 4 — infer-memref-layout.** Each arg memref gets a `TiledLayoutAttr`. With no `leading_tile_rows` arg-attr, for bf16 (`bw=16`) `getTilingFactor(512, gen, sublanes=8, …, is_arg=1, 16)`: `packing=2`, and (assuming `flags[0]` is set, since these are kernel args) the `bw==16` "large 2nd-minor" arm yields `2*sublanes = 16` (divisible into 512), plus the packing tile `(2,1)`:

```text
%a  →  tiles [(16,128),(2,1)], contiguous strides          (512x256 bf16)
%b  →  tiles [(16,128),(2,1)]                               (256x128 bf16)
%o  →  f32 (bw=32): sublane tile 8 → tiles [(8,128)]        (512x128 f32)
```

Each arg's tiled type is aliased through a `tpu.erase_layout` for the body.

**Stage 9 — tiling-propagation.** The two `tpu.vector_load`s and the `tpu.vector_store` match `propagate_layout_to_consumer_rule`; their memref operand is rethreaded to the tiled memref (seeing through `erase_layout`). The three `EraseLayoutOp`s now have no uses and are erased.

**Stage 10 — infer-vector-layout.**

```text
tpu.vector_load %a  → inferLoadStoreVectorLayout reads %a's tiling (16,128)+(2,1)
                      out_layout = 16,{0,0},(16,128)   (bf16 native, packed sublane 16)
                      in_layout  = kNoLayout           (memref operand)
tpu.vector_load %b  → out_layout = 16,{0,0},(16,128)
tpu.matmul %va,%vb  → infer(MatmulOp):
                        in_layout[lhs] = 16,{0,0},(16,128)
                        in_layout[rhs] = 16,{0,0},(16,128)
                        in_layout[acc] = 32,{0,0},(8,128)
                        out_layout     = 32,{0,0},(8,128)
                      ── lhs/rhs MATCH the loads' out_layouts ⇒ no relayout
tpu.vector_store    → in_layout[%acc] = 32,{0,0},(8,128)  (matches matmul out) ⇒ no relayout
```

**Stage 11 — relayout-insertion.** Every `producer.out_layout == consumer.in_layout` ⇒ **no** `tpu.relayout` inserted; the apply-pass `in == out` invariant holds.

**Stage 12 — apply-vector-layout** consumes exactly these attrs: bf16 → `vector<8x128x2xbf16>` vregs; f32 → `vector<8x128xf32>`; matmul → MXU latch/matpush/matres.

> **NOTE — change one op and a relayout appears.** If a `tpu.transpose` fed the matmul lhs, `infer(TransposeOp)` would emit an `out_layout` that does not `join` with the matmul's required lhs `in_layout`; stage 11 would then insert a `tpu.relayout` between them. This is the entire reason the solver and the applier are separate passes — the solver records the disagreement, the relayout-insertion pass resolves it.

---

## What Was Not Traced

- The ≈25 per-op `infer(...)` bodies beyond matmul / elementwise / load-store / broadcast / rotate / iota: addresses + signatures recovered, the skeleton verified on a representative subset; exact offset/implicit-dim choice of `reduce_index` / `dynamic_gather` / `prng_random_bits` / `concatenate` / `reshape` / `transpose` / `extract_strided_slice` sampled, not fully decompiled (HIGH).
- The 5 bespoke memref-propagation rule bodies other than `tpu_memref_slice_rule` — verified to re-thread the tiled memref and recompute tile strides; per-rule stride math not individually decompiled (HIGH).
- The `TpuTilingFlags` byte semantics: the 3 flag bytes gating `getTilingFactor`'s "large 2nd-minor" multiplier are recovered as `flags[0..2]`; their field names are inferred from use (MEDIUM).
- `canReinterpretToUntiledMemref` / `canReinterpretToUntiledContiguousMemref` — the predicate selecting `erase_layout` vs `reinterpret_cast`: call sites recovered, body not decompiled (LOW on the exact predicate).
- The `scf.for`/`while`/`if` + `tpu.region` region-layout inference (iter-arg/yield unification fixpoint): entry points recovered, the cross-iteration fixpoint not decompiled (LOW on those bodies).
- The SparseCore mirror `mosaic_sc::InferVectorLayoutPass` (`0x132ed380` + per-op infer methods `0x132ed7a0`..`0x132ee1e0`): confirmed present and parallel to the TensorCore inferer, bodies not decompiled here.

---

## Cross-References

- [Mosaic Overview](mosaic-overview.md) — the 16-stage pipeline; these three passes are stages 4, 9, 10 there.
- [Mosaic VectorLayout](mosaic-vectorlayout.md) — the `(sublane, lane)` `VectorLayout` struct, `ImplicitDim`, `tilesPerVreg`/`tileArrayShape`, the `relayout` driver, and the 49-entry `applyLayoutOp` table that consumes the attributes this page produces.
- [The tpu MLIR Dialect](tpu-dialect-and-ops.md) — the ops (`tpu.matmul`, `tpu.vector_load`/`store`, `tpu.erase_layout`, `tpu.memref_slice`, …) whose layouts these passes infer.
- [MHLO → XTile → tpu Lowering](mhlo-xtile-tpu-lowering.md) — the general HLO path, and the proof that no MHLO→`tpu` legalizer exists (Mosaic is the only `tpu` producer).
- [Layout Assignment](layout-assignment.md) — the host-side HLO layout assignment, distinct from this in-kernel Mosaic tiling solver.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part V — Compiler: Lowering & Optimization Passes / MLIR lowering chain — [back to index](../index.md)
