# Rolled / Strided / General Emitters

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`); `.rodata` VMA equals file offset; `.data.rel.ro` carries a `0x200000` VMA→file delta. Other versions will differ.*

## Abstract

Once the Mosaic TPU lowering pass has tiled an `EnqueueDMAOp`, picked a transfer kind, and decided how the tile grid is shaped, it has to *emit* the actual SparseCore DMA / Stream ops. That is the job of three functions in `lower_pass_base.cc` and `lower_memref_to_mlo.cc`: `issueRolledTransfer` rolls the residual tile grid into one `scf.for`; `issueStridedTransfer` is the per-tile callback that turns one `DmaParameters` bundle into one concrete `sc_tpu.*` op; and `issueGeneralDma` assembles the cross-core / multi-stride `GeneralDma` form. This page owns those three emitter bodies plus the ODS-builder slot-field maps for `DmaSimpleStart` / `DmaSingleStridedStart` / `DmaStridedStream` / `DmaGeneralStart` — the create-arg → `Properties`-offset → ODS-attr → slot-bit chains.

The reader who already knows MLIR's `OpBuilder` / ODS `create()` / `build()` pattern will recognize the shape: each emitter is a `*StartOp::create` call whose by-value bools, `IntegerAttr`s, `StringAttr`s, and `CoreTypeAttr`s are written into a `getOrAddProperties<>` `Properties` blob, and a fixed `+0x40` delta maps each raw `Properties` offset to the op-relative getter offset. The two surprises this page documents are (1) `issueRolledTransfer` does **not** emit one op per tile — it runs a dimension-coalescing optimizer that flattens contiguous tile dimensions *before* the loop, then rolls only the residual leading dimension into a single `scf.for` whose body is populated manually (the `ForOp::create` is called with a NULL body builder); and (2) the form selection is entirely driven by one integer — `DmaParameters.src_byte_strides.size()` (0 / 1 / >1) crossed with the `TransferKind` (kDma / kStream) — a 3×2 grid that maps to exactly six ops, with two cells trapping at `emitOpError`.

The selector that *decides* Simple-vs-Strided-vs-General and runs the up-front dim-coalescing is the subject of [DmaParameters Selector](dma-parameters-selector.md); the descriptor field layout and the `mem_id`/`core_id`/`dma_type` enums those ops ultimately stamp are on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md); the tile-index→flat-offset algebra that produces the address operands is on [Tile-Index Expansion](tile-index-expansion.md). This page is the *emitter* layer between them.

For reimplementation, the contract is:

- **The rolled SCF loop** — `issueRolledTransfer`'s coalesce-then-roll structure: the three parallel per-dimension `ArrayRef`s, the contiguity-merge predicate, the single `scf.for` with a NULL body builder + manual body population, the per-iteration `index × stride` offset, and the divisibility `emitOpError`.
- **The per-tile callback** — `issueStridedTransfer`'s 2-D dispatch (`TransferKind` × `src_byte_strides.size()`), the `DmaParameters` consistency invariant, the kStream gather/scatter striding gates, the granularity divisor, and the operand binding into each `*StartOp::create`.
- **The general path** — `issueGeneralDma`'s remote-vs-local target base, two-sided sync CHECKs, the `destination_id` topology arithmetic, the multi-level stride descriptor, and the `StringAttr` selectors.
- **The ODS builder slot maps** — the create-arg → `Properties`-offset → ODS-attr → slot-bit chains for `DmaSimpleStart`, `DmaSingleStridedStart`, `DmaStridedStream`, and `DmaGeneralStart`, including the invariant `+0x40` raw↔getter delta.

| | |
|---|---|
| **Rolled emitter** | `mlir::tpu::LowerPassBase::issueRolledTransfer` @ `0x13516ca0` (`0x1620` B) |
| **Strided emitter** | `mlir::tpu::LowerMemrefToMlo::issueStridedTransfer` @ `0x1350cb60` (`0x37e0` B) |
| **General emitter** | `mlir::tpu::(anonymous namespace)::issueGeneralDma` @ `0x1350b3a0` (`0x1700` B) |
| **Source files** | `lower_pass_base.cc` (rolled), `lower_memref_to_mlo.cc` (strided/general) |
| **Per-tile callback target** | `lowerEnqueueDma::$_0` policy-func `0x13516720` → `issueStridedTransfer` |
| **Six emitted ops** | `DmaSimpleStart` `0x6`, `DmaSingleStridedStart` `0x7`, `GeneralDma` `0x8`, `LinearStream` `0x3b`, `StridedStream` `0x3a`, + 2 error cells |
| **ODS raw↔getter delta** | `+0x40` (raw `Properties` offset + `0x40` == op-relative getter offset) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile of all three bodies + the ODS `create`/`build` pairs |

---

## 1. `issueRolledTransfer` — the SCF-loop rolled path

### Purpose

`issueRolledTransfer` is the multi-tile descriptor path: when a tiled DMA covers a grid of tiles that cannot be expressed as one contiguous transfer, this function emits the tile-grid traversal. It is **not** a recursion and **not** a fully-unrolled "one `DmaSimpleStart` per tile" emitter. Instead it (a) coalesces contiguous tile dimensions into one, then (b) rolls the single residual leading dimension into one `scf.for`, populating the loop body manually and issuing the per-tile transfer through a `LowerPassBase` virtual. `issueRetiledTransfer` (`0x13519480`) always routes the multi-tile case here (the two `call 0x13516ca0` sites at `0x13519f3d` / `0x1351a1ea`); the unrolled-vs-rolled choice lives *inside* this function, in the coalescing loop.

### Entry Point

```text
issueRetiledTransfer (0x13519480)             ── tile-grid driver
  └─ issueRolledTransfer (0x13516ca0, 0x1620 B) ── coalesce + roll
       ├─ $_0 (0x135182c0)                     ── per-axis tile-stride product table
       ├─ scf::ForOp::create (0x17866d60)      ── the rolled loop (NULL body builder)
       ├─ $_1 (0x13518480)                     ── arith.constant n : i32
       ├─ vtable+0x20 [LowerPassBase]          ── getConstantIntValue probe (coalescing)
       └─ vtable+0x18 [LowerPassBase]          ── per-tile sub-memref / offset generator
```

### Algorithm

The function takes three **parallel** per-dimension arrays — `bases` (`ArrayRef<Value>`, the tile-grid extents from [`getTiledDynamicShape`](tile-index-expansion.md)), `srcTileStrides` (`ArrayRef<long>`), and `dstTileStrides` (`ArrayRef<long>`) — one entry per tile-grid dimension. The prologue CHECK_EQs their lengths; the body coalesces, rolls, and issues.

```c
// mlir::tpu::LowerPassBase::issueRolledTransfer   sub_13516CA0
function issueRolledTransfer(b, op, bases, srcTileStrides, dstTileStrides,
                             v1, v2, L1, L2, v3, v4, devIds, prio, kind,
                             perTileCallback):
    // 0. three parallel arrays, one entry per tile-grid dim
    CHECK_EQ(bases.size(), srcTileStrides.size())   // "tiled_dynamic_shape.size() == src_tile_strides.size()" @0xa166591
    CHECK_EQ(bases.size(), dstTileStrides.size())   // "== dst_tile_strides.size()" @0xa16655b

    // 1. per-axis suffix-product multipliers (row-major), one per side
    srcMul = $_0(srcTileStrides)        // sub_135182C0: factor*shape[k] imul, SmallVector<unsigned>
    dstMul = $_0(dstTileStrides)        // same, dst side

    // 2. COALESCE contiguous striding dims (see §2 for the predicate)
    coalesceStridingDims(bases, srcTileStrides, dstTileStrides)   // backward scan @0x13517070..0x13517301

    // 3. emit ONE scf.for over the residual leading dim
    dim = residual_leading_dim
    forOp = scf::ForOp::create(b, op.getLoc(),
                lb   = arith.constant 0 : i32,          // $_1(0)
                ub   = bases[dim],                      // the tile-count Value
                step = arith.constant 1 : i32,          // $_1(1)
                inits = {},                             // empty ValueRange
                body = NULL,                            // function_ref left null
                false)                                  // sub_17866D60 @0x13517477
    b.setInsertionPointToStart(forOp.body[0])           // manual body population

    // 4. per-iteration base offset, per side
    for side in {src, dst}:
        idx = inductionVar-derived index
        off = arith.muli(idx, tileStride)               // MulIOp<V,V> 0xfaaae00 / <V,ConstOp> 0x1351ab00
        // 5. issue the per-tile transfer through the LowerPassBase virtual
        base = this->vtable[+0x18](b, op.getLoc(), tileStrideConst, idx, accum)  // per-tile sub-memref (HIGH)

    b.restoreInsertionPoint(savedIP)

    // 6. divisibility guard
    if (L1 % perIterStride) != 0:                       // idiv @0x13517726
        return op.emitOpError("Tile size (%d bytes) not divisible by %d bytes. …")  // @0xa01cd05
    return success
```

> **QUIRK —** `scf::ForOp::create` is called with a **NULL** `function_ref` body builder (the `{callee=0, callable=0}` 16-byte struct, zeroed at `0x13517433`). That makes `ForOp::create` build only the loop *shell* (a body block carrying the induction variable). The body is then populated **manually**: the `OpBuilder` insertion point is redirected into the for-op's region-0 block (written to `OpBuilder+0x10`/`+0x18` at `0x135174ac`), the per-iteration ops are emitted there, and the insertion point is restored afterward (`SpecificNodeAccess::getNodePtr` @ `0x1d8ccbc0`, `0x1351763b`). A reimplementation that passes a real body builder lambda would emit the same IR, but the binary takes the manual-population route. *(Decompile @ `0x13516ca0` line 419: `mlir::scf::ForOp::create(v173, v118, v119)`; CONFIRMED.)*

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `issueRolledTransfer` | `0x13516ca0` | the rolled emitter | CONFIRMED |
| `issueRolledTransfer::$_0` | `0x135182c0` | per-axis tile-stride suffix-product table (`SmallVector<unsigned>`) | CONFIRMED |
| `issueRolledTransfer::$_1` | `0x13518480` | `arith.constant n : i32` builder | CONFIRMED |
| `mlir::scf::ForOp::create` | `0x17866d60` | the rolled loop shell | CONFIRMED |
| `MulIOp<Value,Value>::createOrFold` | `0xfaaae00` | per-iteration `index × stride` (both Values) | CONFIRMED |
| `MulIOp<Value,ConstantOp>::createOrFold` | `0x1351ab00` | per-iteration `index × stride` (folded const) | CONFIRMED |
| `OpState::emitOpError` | `0x1d8cefa0` | divisibility diagnostic | CONFIRMED |
| `LowerPassBase` vtable `+0x18` | (no symbol) | per-tile sub-memref / offset generator | HIGH |
| `LowerPassBase` vtable `+0x20` | (no symbol) | `getConstantIntValue`-style probe (coalescing) | HIGH |

> **NOTE —** the per-tile op-emission helper (vtable `+0x18`) and the constant-int probe (vtable `+0x20`) are abstract `LowerPassBase` virtuals with no standalone symbol; the concrete pass `LowerMemrefToMlo` is an anonymous-namespace class with no emitted `ZTV` table. The call arguments and the surrounding `index`/`offset` arithmetic are byte-pinned; the method *names* are inferred from those arguments — hence HIGH, not CONFIRMED.

---

## 2. The dimension-coalescing predicate (rolled prologue)

### Purpose

The coalescing pass is *why* the rolled form is efficient: contiguous or degenerate tile dimensions are flattened into one larger transfer (fewer descriptors, fewer loop iterations) **before** the `scf.for` is emitted, so the loop covers only the residual dimensions that cannot be flattened. The dim-coalescing optimizer as a *selector* concept is owned by [DmaParameters Selector](dma-parameters-selector.md); the byte-exact merge predicate that runs in `issueRolledTransfer`'s prologue is documented here because it gates the rolled loop.

### Algorithm

A backward adjacent-pair scan (`0x13517150`) walks the dimensions from the last toward the first. For each pair (outer `d`, inner `d+1`) it queries the `LowerPassBase` vtable `+0x20` `getConstantIntValue` probe on each base; the probe returns `optional<int64>` packed as `{bit32 = present | low32 = value}`, so the marker `0x100000001` means "present AND value == 1".

```c
// coalescing scan in issueRolledTransfer   @0x13517070..0x13517301
function coalesceStridingDims(bases, srcStride, dstStride):
    for d = N-2 downto 0:                              // backward adjacent-pair scan
        innerC = getConstantIntValue(bases[d+1])       // vtable+0x20 @0x13517176
        outerC = getConstantIntValue(bases[d])         // vtable+0x20 @0x13517160
        merge = false
        // COND (i): degenerate inner tile-count == 1
        if (innerC & 0x1ffffffff) == 0x100000001:      // @0x13517186
            merge = true
        // COND (ii): row-major contiguity, per side
        elif (outerC & 0x100000000) != 0:              // outer base is a constant int @0x1351721a
            if srcStride[d] == srcStride[d+1] * outerC  // imul; cmp @0x13517243
               && dstStride[d] == dstStride[d+1] * outerC:  // imul; cmp @0x1351726a
                merge = true
        if merge:
            bases[d] = arith.muli(bases[d], bases[d+1])         // MulIOp 0xfaaae00 @0x135171e5
            VLOG(1) << "Flattening striding dimension " << d    // lower_pass_base.cc:123, @0xa1f260b
            memmove(bases    + d+1, bases    + d+2, …)  // 8-byte entries  @0x135170b1
            memmove(srcStride+ d+1, srcStride+ d+2, …)  // 4-byte entries  @0x135170ea
            memmove(dstStride+ d+1, dstStride+ d+2, …)  // 4-byte entries  @0x1351711c
            N -= 1                                       // dec all three counts
```

A pair is collapsed iff **either** the inner dimension's tile-count is the constant `1` (COND i — always foldable, skips the stride check) **or** the outer base is a constant int and the outer stride equals the inner stride scaled by the outer extent, on *both* sides (COND ii — true row-major contiguity). If neither holds the pair is left intact and the dimension becomes a residual loop/stride dimension. When the VLOG path cannot fold a stride it logs `"Attempted to combine non-constant stride:"` (`@0xa27e800` family) and skips.

| Predicate step | Test (@VA) | Meaning | Confidence |
|---|---|---|---|
| outer-const probe | `getConstantIntValue(bases[d])` @ `0x13517160` | `vtable+0x20` constant-int probe | CONFIRMED |
| inner-const probe | `getConstantIntValue(bases[d+1])` @ `0x13517176` | same, inner dim | CONFIRMED |
| COND (i) | `(rax & 0x1ffffffff) == 0x100000001` @ `0x13517186` | inner tile-count is constant `1` | CONFIRMED |
| COND (ii) gate | `test r13, 0x100000000` @ `0x1351721a` | outer base is a constant int | CONFIRMED |
| COND (ii) src | `srcStride[d] == srcStride[d+1] × outerC` @ `0x13517243` | source row-major contiguity | CONFIRMED |
| COND (ii) dst | `dstStride[d] == dstStride[d+1] × outerC` @ `0x1351726a` | dest row-major contiguity | CONFIRMED |
| MERGE | `muli` + VLOG + 3× memmove + dec @ `0x13517070` | collapse `(d, d+1)` → one dim | CONFIRMED |

> **GOTCHA —** the two merge conditions are distinct branch targets in the binary. COND (i) (inner count == 1) skips the stride check entirely — it is a correctness-preserving superset of COND (ii) for count-1 dims, not a redundant special case. A reimplementation that folds only on the stride-contiguity test (COND ii) will fail to collapse degenerate count-1 dimensions and emit a wasteful 1-trip loop level. *(The two distinct jump targets are byte-pinned; whether (i) is logically subsumed by (ii) for zero-stride inner dims was not proven — the binary keeps them separate.)*

---

## 3. `issueStridedTransfer` — the per-tile callback

### Purpose

`issueStridedTransfer` is the unified per-tile DMA-op emitter — the `std::function` callback target the tiled-DMA dispatch invokes once per tile (via the `lowerEnqueueDma::$_0` policy-func `0x13516720`). It reads one `DmaParameters` bundle and emits exactly one concrete `sc_tpu.*` op. It is a 2-D selector: axis 1 is the `TransferKind` arg (kDma=0 / kStream=1 / else → `emitOpError`), axis 2 is `DmaParameters.src_byte_strides.size()` (0 / 1 / >1). The 3×2 grid maps to the six SparseCore off-tile ops.

### Entry Point

```text
lowerEnqueueDma::$_0  (policy-func 0x13516720)        ── std::function callback target
  └─ issueStridedTransfer (0x1350cb60, 0x37e0 B)      ── per-tile emitter
       ├─ DmaSimpleStartOp::create        (0x145b9740, opcode 0x6)
       ├─ DmaSingleStridedStartOp::create (0x145bcd20, opcode 0x7)
       ├─ issueGeneralDma                 (0x1350b3a0, → GeneralDma 0x8)   [§5]
       ├─ LinearStreamStartOp::create     (0x145e3440, opcode 0x3b)
       ├─ StridedStreamStartOp::create    (0x1460b8e0, opcode 0x3a)
       ├─ isGather                        (0x14afb1e0)  ── stream gather/scatter classifier
       └─ $_0 (0x13510340)                              ── per-side stride-level validator
```

### Algorithm

```c
// mlir::tpu::LowerMemrefToMlo::issueStridedTransfer   sub_1350CB60
function issueStridedTransfer(b, op, src, dst, p /*DmaParameters&*/,
                              devIds, priority, kind, force):
    CHECK_EQ(priority, 0)                              // "priority == 0" @0x9fc7181 (lower_memref_to_mlo.cc:1142)
    srcSpace = GetMemorySpace(p.src)                   // 0x1459c7e0 (from memref @p+0x00)
    dstSpace = GetMemorySpace(p.dst)                   // (from memref @p+0x08)
    trace = op.getInherentAttr("sc.inside_trace_region") != null   // @0x85fd7e8

    n = p.src_byte_strides.size()                      // p+0x18 — the form selector

    switch kind:
      case kDma (0):
        if n == 0 && !force:                           // contiguous
            len = ceil_div(p.length, Target::GranuleBytes())   // 0x1d617f80
            return DmaSimpleStartOp::create(b, loc, src, {0}, dst, {0}, len, sflagIdx, sflag)  // 0x6
        // Strided/General arm — consistency invariant first:
        CHECK_EQ(p.src_byte_strides.size(), p.tgt_byte_strides.size())   // @0xa166501 (:843)
        CHECK_EQ(p.src_byte_strides.size(), p.steps_per_stride.size() - 1)  // @0x9e79ab6 (:844)
        if n == 1:
            return DmaSingleStridedStartOp::create(…, srcStride, dstStride, innerVecLen, elemsPerStride)  // 0x7
        else: // n > 1
            return issueGeneralDma(b, locGen, target, op, src, dst, len, A, B, C, …)  // GeneralDma 0x8

      case kStream (1):
        isG = isGather(op)                             // 0x14afb1e0
        // gather/scatter striding gates (per side, via $_0 0x13510340 → vtable+0x20):
        if isG && $_0(p.tgt_byte_strides) > 0: return op.emitOpError("Gather streams do not support destination striding. …")  // @0xa06d0da
        if isScatter && $_0(p.src_byte_strides) > 0: return op.emitOpError("Scatter streams do not support source striding. …")  // @0xa06d17f
        gran = TransferGranularityInBytes(target, dstSpace, …)  // 0x14a89ea0 via target->[+0x948]
        if n == 0: return LinearStreamStartOp::create(b, loc, false, dstIsHbm, trace, 0, …)  // 0x3b
        if n == 1: return StridedStreamStartOp::create(b, loc, false, dstIsHbm, trace, …, stride0, stride1, …)  // 0x3a
        else:      return op.emitOpError("Streams support up to 1 level of striding. Got %d levels of …")  // @0xa092a49

      default:
        return op.emitOpError("Unsupported transfer kind: %d.")   // @0x872e1dd
```

### The form-selection grid

| `src_byte_strides.size()` | kDma (0) | kStream (1) |
|---|---|---|
| 0 (contiguous) | `DmaSimpleStart` `0x6` | `LinearStream` `0x3b` |
| 1 (one stride level) | `DmaSingleStridedStart` `0x7` | `StridedStream` `0x3a` |
| >1 (multi-stride) | `issueGeneralDma` → `GeneralDma` `0x8` | `emitOpError` (≤1 level only) |
| (else `TransferKind`) | `emitOpError "Unsupported transfer kind: %d."` | (same) |

> **GOTCHA —** kStream tops out at one stride level. The decompile carries **two** distinct "Streams support up to 1 level of striding" diagnostics — one keyed on *source* striding (`src_byte_strides.size() <= 1`, `lower_memref_to_mlo.cc:1189`) and one on *steps per stride* (`:1193`), plus the separate gather/scatter gates. A reimplementation that emits a `StridedStream` for any `n >= 1` will mis-encode any 2+-level stride that a stream path receives; only kDma routes `n > 1` to the `GeneralDma` form.

### The `DmaParameters` consistency invariant

The Strided/General arm CHECK_EQs three size fields of the bundle (whose offset map is owned by [Tile-Index Expansion](tile-index-expansion.md)): `|src_byte_strides| == |tgt_byte_strides| == |steps_per_stride| − 1`. A contiguous (0-stride) descriptor therefore has `steps_per_stride.size() == 1`: the `steps_per_stride` vector carries N+1 cumulative steps for N stride levels.

| Bundle field | Offset | Role in `issueStridedTransfer` | Confidence |
|---|---:|---|---|
| `src` (memref `Value`) | `+0x00` | → `GetMemorySpace` → src endpoint | CONFIRMED |
| `dst` (memref `Value`) | `+0x08` | → `GetMemorySpace` → dst endpoint | CONFIRMED |
| `src_byte_strides` `{data,size}` | `+0x10` / `+0x18` | **the form selector** (`+0x18`) | CONFIRMED |
| `tgt_byte_strides` `{data,size}` | `+0x50` / `+0x58` | CHECK_EQ vs `+0x18` | CONFIRMED |
| `length`/`sflag` `Value` | `+0x90` | create `len`/`sflag` arg | CONFIRMED (offset/type), INFERRED (which) |
| `steps_per_stride` `{data,size}` | `+0x98` / `+0xa0` | CHECK `size == +0x18 + 1` | CONFIRMED |

> **NOTE —** the granularity divisor differs by kind: kDma uses `Target::GranuleBytes()` (`0x1d617f80`) with a ceil-div idiom; kStream uses `xla_mlo_util::TransferGranularityInBytes(SparseCoreTarget const&, MemorySpace, bool)` (`0x14a89ea0`) reached via `target()->[+0x948]` — the per-memory-space SPMEM-stripe / DMA-word unit. The chosen granularity becomes an `arith.ConstantIndexOp` divisor folded into a `DivUIOp`/`idiv` before the op is built.

---

## 4. The ODS builder slot-field maps (contiguous + single-stride forms)

### Purpose

Each `*StartOp::create` is a thin wrapper that forwards arg-order-identity into a `build()` which adds the operand groups (`AttrSizedOperandSegments`) and writes the inherent attributes into a `getOrAddProperties<…Properties>()` blob. The invariant across the whole family is the **`+0x40` delta**: the op getters add a constant `0x40` to the raw `Properties` offset (op-relative getter offset = `raw + 0x40`). This section maps the create args of the contiguous (`DmaSimpleStart` `0x6`) and single-stride (`DmaSingleStridedStart` `0x7`, `DmaStridedStream` `0x3a`) forms; the `GeneralDma` map is in [§6](#6-dmageneralstart-ods-slot-map).

### `DmaSimpleStart` (`0x6`)

`DmaSimpleStartOp::create` (`0x145b9740`) is a wrapper supplying `enable_trace=false`, two null `IntegerAttr`s, and `StringRef=""`; it forwards into `build` (`0x1459ac00`), which adds 7 operand segments and writes a `0x40`-byte `Properties` (TypeID `@0x224e95c0`). The 7 create operand args are `(srcBase, srcIdx, dstBase, dstIdx, length, sflagIdx, sflag)`.

| create arg / wrapper default | ODS getter (op-rel) | SparseCoreDma `SimpleDma` slot field | Confidence |
|---|---|---|---|
| `enable_trace = false` (default) | `getEnableTrace` (`+0x50`) | `TraceEn` (slot `+0x18` bit 41) | CONFIRMED |
| `srcMemIndex IntegerAttr = null` | `getSrcMemIndex` (`+0x58`) | `dma_sreg_source_offset` | CONFIRMED |
| `dstMemIndex`/opcode `IntegerAttr = 0` | `getDstMemIndex` (`+0x40`) / `getDstOpcode` (`+0x48`) | `dst_opcode` (slot `+0x18` `>>39 &0x3`) | CONFIRMED |
| `srcBase` `Value` | `getSrcBufferIndicesMutable` | `src_mem_{core,mem}_id` (via `GetMemorySpace`) | CONFIRMED |
| `dstBase` `Value` | `getDstBufferIndicesMutable` | `dst_mem_{core,mem}_id` (via `GetMemorySpace`) | CONFIRMED |
| `length` `Value` | (operand seg) | `dma_length` (slot `+0x18` `>>42 &0x3f`) | CONFIRMED |
| `sflagIdx`, `sflag` | `getDstSflagIndicesMutable` | `dest_sync_flags` (slot `+0x18` `>>15 &0x3f`) | CONFIRMED |

The `dst_opcode` string-attr → 2-bit code mapping (`write_4b` / `read_and_add` / `atomic_add`) and its memory-space gates are owned by [Intra-Chip DMA Descriptor §2](intra-chip-descriptor.md#2-the-opcode-enums-srcopcode--dstopcode); the `GetDstOpcode<DmaSimpleStartOp>` accessor (`0x135aaa60`) reads the optional `dst_opcode` attribute at `this+0x48`.

### `DmaSingleStridedStart` (`0x7`)

`DmaSingleStridedStartOp::create` (`0x145bcd20`) — demangled signature `(b, loc, V, VR, V, VR, V, VR, V, V, V, V, V)` — is the `DmaSimpleStart` operand set plus **4 trailing `Value` stride args**, built from `src_byte_strides[0]`, `tgt_byte_strides[0]`, the `steps_per_stride` span, and the inner-vector-length (materialised via `arith.ConstantIndexOp`/`IndexCastOp`). By position those 4 strides bind to the `SingleStridedDma` `{source_stride, destination_stride, inner_vector_length, elements_per_stride}` slot fields (`P-3-295 §B`: slot `+0x10 >>41/35/—/29` + `+0x18 >>10`). The create arity (4 strides) and the `SmallVector` reads are CONFIRMED; the exact 1-to-1 stride-arg → slot-field order is by position (HIGH — its `build` body was not independently decoded).

### `DmaStridedStream` (`0x3a`)

`StridedStreamStartOp::create` (`0x1460b8e0`) — demangled signature `(b, loc, b, b, b, V, VR, V, VR, V, V, V, IntegerAttr, V, VR)` — is the `LinearStream` operand set plus **2 trailing stride `Value`s** (`stride0`, `stride1`) and the `IntegerAttr` `tile_local` length-per-stride. The three leading bools mirror `LinearStream`'s `{upd, hbm4b/dstIsHbm, enable_trace}`. The 2 strides map to the `StridedStream` `{stride0, stride1}` operands (`P-3-287`); the create-arg shape is CONFIRMED, the slot-bit join is structural (HIGH).

> **NOTE —** the `LinearStream` (`0x3b`) contiguous-stream form's full create→`Properties`→slot map (the `upd` / `hbm4b` / `enable_trace` / `opcode` / `tile_local_length_per_stride` bindings) is owned by the SparseCore stream slot-encoding page; on this page it is the `n==0`/kStream cell of [§3](#3-issuestridedtransfer--the-per-tile-callback)'s grid. `issueContiguousTransfer`'s stream call is `(false, dstIsHbm, TraceLocalDma, 0, …)` ⇒ `upd=false, hbm4b=dstIsHbm, enable_trace=TraceLocalDma, opcode=0`.

---

## 5. `issueGeneralDma` — the general-DMA path

### Purpose

`issueGeneralDma` is the assembler for the `GeneralDma` (`0x8`) form — the only DMA form that carries >1 stride level, and the cross-core / cross-device form. `issueStridedTransfer` routes the kDma `n > 1` case here. The free function (anonymous namespace, `lower_memref_to_mlo.cc`) computes a two-sided sync, an explicit `destination_id` from the device topology, and a multi-level stride descriptor, then emits the richest of the four `DmaGeneralStartOp::create` overloads.

### Entry Point

```text
issueStridedTransfer (0x1350cb60)             ── kDma, src_byte_strides.size() > 1
  └─ issueGeneralDma (0x1350b3a0, 0x1700 B)   ── GeneralDma assembler
       ├─ lowering_util::GetRemoteMemBase (0x13d88660)   ── remote target base
       ├─ AllocateAtOffsetOp::create      (0x145a5aa0)   ── local sflag memref
       ├─ TpuChipConfig::Megacore         (0x20afca00)   ── topology gate
       ├─ {Tensor,Sparse}CoresPerLogicalDevice (0x111f6020 / 0x135159c0)
       ├─ CoreIndexOp::create             (0x145aba00)   ── runtime core index
       └─ DmaGeneralStartOp::create       (0x145b1b80)   ── GeneralDma 0x8 (28-arg overload)
```

### Algorithm

```c
// mlir::tpu::(anonymous namespace)::issueGeneralDma   sub_1350B3A0
function issueGeneralDma(b, locGen, target, op, src, dst, len,
                         A, B, C /*ArrayRef<Value> stride lists*/,
                         isRemote, srcSpace, dstSpace, devIds, v1, v2, syncBool):
    // 1. remote-vs-local target base (the SOURCE side of two-sided sync)
    if isRemote && devIds.present:
        base = GetRemoteMemBase(b, locGen, …)            // 0x13d88660 → remote base
    elif isRemote:                                       // remote but no source semaphore
        return MakeError("Source semaphore must be provided for a remote DMA.")  // @0xa0c8904 (:915)
    else:
        base = AllocateAtOffsetOp::create(ChipIdOp, MemorySpaceAttr(sflag=5), MemRefType, ConstantIndex)  // local sflag

    // 2. target-semaphore core-type CHECK (the SET side)
    ct = op.operand(3).getType().getMemorySpace().getCoreType()   // getCoreType 0x14a9e320
    if !ct.present:
        return MakeError("Target semaphore is missing a core type in its memory space")  // @0x86e65f5 (:948)

    // 3. destination_id from topology
    if Megacore(target) && target.coresGate >= 2:        // [target+0x3b8]+0x7c >= 2
        if srcSpace == vmem || dstSpace == vmem:
            return MakeError("Targeting VMEM on MegaCore targets is ambiguous.")  // @0x9ffb522 (:968)
    divisor = (dstCoreType == TENSOR_CORE) ? SparseCoresPerLogicalDevice * chipMeta[+0x90]
            : (dstCoreType == 1)           ? SparseCoresPerLogicalDevice
            :                                TensorCoresPerLogicalDevice
    destId = arith.addi(arith.{rem,div}ui(index_cast(CoreIndexOp), ConstantIndex(divisor)),
                        ConstantIndex(offset))            // RemUI 0x1cb20800 / DivUI 0x1cb06d00 / AddI 0x1caf0b00

    // 4. multi-level (>1) stride descriptor
    shape = SmallVector<unsigned>(inline cap 6)          // header 0x600000000
    fill shape per stride-dim (cmp r15,N je ladder N=1..6; grow_pod for >=7)
    nondefault_stride_dimensions = strideDimCount        // ≤8, 3-bit

    // 5. attribute selectors
    dst_opcode   = (srcSpace == smem || dstSpace == smem) ? "write_4b" : null   // @0x879a767
    enable_trace = (same smem condition) ? UnitAttr : null
    sync_mode    = syncBool ? "count_dones" : "count_words"   // cmovne @0x8562648 / @0x8570908
    dma_ordering = "relaxed"                              // @0x86ff2e0

    // 6. emit
    return DmaGeneralStartOp::create(b, loc, enable_trace, src, srcIdx, base, …,
                                     A, B, C, ct1..ct4, dst_opcode, sync_mode, dma_ordering)  // 0x145b1b80
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `issueGeneralDma` | `0x1350b3a0` | GeneralDma assembler | CONFIRMED |
| `lowering_util::GetRemoteMemBase` | `0x13d88660` | cross-device remote target base | CONFIRMED |
| `AllocateAtOffsetOp::create` | `0x145a5aa0` | local sflag memref | CONFIRMED |
| `MemorySpaceAttr::getCoreType` | `0x14a9e320` | target-semaphore core type | CONFIRMED |
| `TpuChipConfig::Megacore` | `0x20afca00` | megacore gate | CONFIRMED |
| `Target::SparseCoresPerLogicalDevice` | `0x135159c0` | dest_id divisor (SC) | CONFIRMED |
| `Target::TensorCoresPerLogicalDevice` | `0x111f6020` | dest_id divisor (TC) | CONFIRMED |
| `CoreIndexOp::create` | `0x145aba00` | runtime core index | CONFIRMED |
| `DmaGeneralStartOp::create` | `0x145b1b80` | the emit (28-arg overload) | CONFIRMED |

> **QUIRK —** `GeneralDma`'s descriptor attributes are emitted as **`StringAttr`s**, not the integer enums the slot decode shows: `dst_opcode = "write_4b"`, `sync_mode = "count_dones"/"count_words"`, `dma_ordering = "relaxed"`. The codec maps these symbolic names to bit fields at encode time. A reimplementation that writes the integer enum directly into the op skips the string layer the binary uses; the *descriptor* the hardware sees is the same, but the MLIR op carries strings. `dst_opcode` is `null` unless `src` or `dst` is `smem` (then `"write_4b"` ⇒ `WRITE_4B=1`, the 4-byte scalar write). *(All four StringAttr selectors CONFIRMED in decompile @ `0x1350b3a0` lines 762/1025/1027/1033.)*

> **GOTCHA —** the `destination_id` is computed from the device topology, **not** a constant: `dest_id = base + (core_index op divisor)`, where the divisor is `TensorCoresPerLogicalDevice` vs `SparseCoresPerLogicalDevice` selected by the *destination* core type, and `op` is `RemUI` on the megacore path vs an `idiv`-folded constant on the non-megacore path. A reimplementation that hard-codes a routing id will mis-route on any non-default chip geometry. The exact algebraic formula is reconstructed from the op order (HIGH); the per-core-type branch selection is CONFIRMED; the chip-metadata field names behind `[target+0x3b8]+0x7c` (the megacore core-count gate, `119*8+124`) and `[target+0x948]+0x90` (the SparseCore multiplier, `297*8+144`) were not resolved to named `TpuChipConfig` accessors.

---

## 6. `DmaGeneralStart` ODS slot map

`DmaGeneralStartOp::create` (`0x145b1b80`, the 28-arg overload) forwards into `build` (`0x1459b2c0`), which adds the operand groups (`AttrSizedOperandSegments`, segment-size array at `op+0x80`) and writes 8 inherent attributes into a `0x88`-byte `Properties` (`operator new(0x88)`, TypeID `@0x224e95b0`). The same `+0x40` raw↔getter delta as the simpler forms holds. The build's attr-pointer writes were data-flow traced; the 6 typed getters are byte-confirmed.

| raw `Properties` | op-rel getter | getter | attr name | type | Confidence |
|---:|---:|---|---|---|---|
| `+0x00` | `+0x40` | (getInherentAttr only) | `dma_ordering` | `StringAttr` | HIGH |
| `+0x08` | `+0x48` | `getDstMemCoreType` | `dst_mem_core_type` | `CoreTypeAttr` | CONFIRMED |
| `+0x10` | `+0x50` | `getDstOpcode` | `dst_opcode` | `StringAttr` | CONFIRMED |
| `+0x18` | `+0x58` | `getDstSyncFlagCoreType` | `dst_sync_flag_core_type` | `CoreTypeAttr` | CONFIRMED |
| `+0x20` | `+0x60` | `getEnableTrace` | `enable_trace` | `UnitAttr` | CONFIRMED |
| `+0x28` | `+0x68` | `getSrcMemCoreType` | `src_mem_core_type` | `CoreTypeAttr` | CONFIRMED |
| `+0x30` | `+0x70` | `getSrcSyncFlagCoreType` | `src_sync_flag_core_type` | `CoreTypeAttr` | CONFIRMED |
| `+0x38` | `+0x78` | (getInherentAttr only) | `sync_mode` | `StringAttr` | HIGH |

The create-arg → `GeneralDma` slot-field join (`P-3-295 §C`):

- src/dst data buffers → `{src,dst}_mem_{core,mem}_id` (slot `+0x18 >>26/28/34/36`, via the buffer memrefs' `MemorySpace`s).
- `dst_opcode` `"write_4b"` → `DstOpcode` (slot `+0x18 >>39 &0x3` = `WRITE_4B=1`).
- `enable_trace` `UnitAttr` → `TraceEn` (slot `+0x18` bit 41).
- `dst_sync_flag_core_type` `CoreTypeAttr` → the sync-flag core type feeding `dest_sync_flags_vs1`.
- the `destination_id` `Value` (the topology `AddIOp`) → `DestinationId` (slot `+0x10 >>29 &0x1f`) + `DestinationIdValid`.
- the source semaphore → `SourceSyncFlagNumber` WAIT (slot `+0x10 >>41 &0x1f`) + valid bit.
- the dst semaphore base → `DestSyncFlagsVs1` SET (slot `+0x10 >>35 &0x1f`) + valid bit.
- the stride-dim count → `NondefaultStrideDimensions` (slot `+0x18 >>10 &0x7`); the three `ArrayRef<Value>` → the per-level stride operands.

> **NOTE —** the two `StringAttr` slots without a typed getter — `+0x00` (`dma_ordering`) and `+0x38` (`sync_mode`) — are paired to those names by elimination plus the caller's `StringAttr` construction order (the 6 typed getters fix the other slots exactly). The slot *offsets* are byte-pinned; the name↔offset pairing for these two is the one inferred join (HIGH). A `getInherentAttr` name→offset bit-trace would confirm it.

The `GeneralDma` opcode is `0x8`, encoded at bundle bit `0xb5=181` (the `gfc` `SparseCoreDmaEncoder::Encode` `0x1eb5a3a0`, oneof bound `cmp rax,0xa`, case 10).

---

## Related Components

| Component | Relationship |
|---|---|
| `issueRetiledTransfer` (`0x13519480`) | tile-grid driver; always routes the multi-tile case to `issueRolledTransfer` |
| `issueContiguousTransfer` (`0x1350a3e0`) | the single-contiguous-transfer peer (`DmaSimpleStart`/`LinearStream` directly) |
| `lowerEnqueueDma::$_0` (`0x13516720`) | the `std::function` policy-func that invokes `issueStridedTransfer` per tile |
| `getTiledDynamicShape` (`0x13516840`) | produces the `bases` `ArrayRef<Value>` the rolled loop iterates |

## Cross-References

- [DmaParameters Selector](dma-parameters-selector.md) — the Simple-vs-Strided-vs-General selector and the up-front dim-coalescing that decides which form these emitters produce
- [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) — the descriptor field layout and the `mem_id`/`core_id`/`dma_type`/`dst_opcode` enums the emitted ops ultimately stamp
- [Tile-Index Expansion](tile-index-expansion.md) — `getTiledDynamicShape` and the `DmaParameters` bundle (`src_byte_strides`/`tgt_byte_strides`/`steps_per_stride`) whose `size()` drives the form selection
- [Host↔Device DMA](host-device-dma.md) — the host infeed/outfeed leg; the `DMA_TYPE_CHIP_TO_HOST`/`DMA_TYPE_LOCAL_OR_HOST` path
- [OCI Command DMA-ID](oci-command-dma-id.md) — the `trace_id_header` DMA-id that pairs the issued descriptor's begin/end trace points
- [The net_router Emitter Pipeline](../routing/net-router-pipeline.md) — the collective router whose local leg issues these intra-chip transfers; the cross-chip remote-endpoint encoding `issueGeneralDma`'s `GetRemoteMemBase` path counterparts
