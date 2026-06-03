# Tile-Index Expansion

> *All addresses, symbols, and offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`); `.data.rel.ro` carries a `0x200000` VMA→file delta. Other versions will differ — treat every VA as version-pinned.*

## Abstract

`ExpandTiledMemRefsPass` is the SparseCore stage-2 pass that finishes the DMA lowering the [LowerToMlo DMA bridge-cast](../compiler/lower-to-mlo-dma-bridge.md) deliberately deferred. Where stage 1 parks a `tpu.enqueue_dma` behind a `sc.unlowering`-tagged `builtin.unrealized_conversion_cast` — because the op's final SparseCore shape depends on a *tiled* memref layout that LowerToMlo cannot see — this pass runs once the tile layout is fixed, so the tile→address arithmetic is finally computable. The consumer of the parked op is `LowerMemrefToMlo::lowerEnqueueDma` (`0x135105a0`).

The pass owns one piece of compiler arithmetic that is invisible to every layer above and below it: the **de-tiling algebra** that turns a `tpu::Tile`-layout memref plus a logical `ValueRange` of indices into the row-major HBM / `TILE_SPMEM` address arithmetic, and the per-tile transfer issuers that bind that arithmetic onto the SparseCore DMA / Stream descriptor operands. A tiled dimension `d` with tile size `t` splits two ways that must agree: the *shape* splits into `(ceil(d/t), t)` (`getExpandedShape`), and the *index* `i` splits into `(i/t, i%t)` (`expandTiledIndices`). The outer half indexes the tile grid; the inner half indexes inside a tile; the row-major suffix-product strides (`getExpandedStrides`) turn the expanded index into a physical word offset.

This page owns three things and three things only:

- **The de-tiling primitives** — `expandTiledMemRef` (tiled `MemRefType` → flat strided `MemRefType`), `getExpandedShape` (`d → (ceil(d/t), t)`), `getExpandedStrides` (the row-major suffix-product over inner dims), and the index twin `expandTiledIndices` (`i → (i/t, i%t)` via `arith.divui` / `arith.remui`, with the `vector<i32>` broadcast variant).
- **The transfer issuers** — `issueContiguousTransfer` (the zero-index whole-memref bulk move that emits one `DmaSimpleStartOp` or one `LinearStreamStartOp`), `getDMATiling` (the retiling reshape), and `issueRetiledTransfer` (the per-tile descriptor loop whose signed `Div/Rem/Mul` is the retiled twin of the index algebra).
- **The deferred-DMA consumption** — `lowerEnqueueDma`, the stage-2 dispatch that ties `getTransferKind` to the retiled-vs-contiguous fork and erases the original op.

What this page does **not** own: the bridge-cast tagging mechanics and `ConversionTarget` of stage 1 ([LowerToMlo DMA Bridge-Cast](../compiler/lower-to-mlo-dma-bridge.md)); the descriptor field layout, `mem_id`/`core_id`, and slot opcode enums ([Intra-Chip DMA Descriptor](intra-chip-descriptor.md)); the `issueRolled*` / `issueGeneral*` transfer-body emitters ([Rolled / Strided / General Emitters](rolled-strided-general.md)); the Simple-vs-Strided selector ([DmaParameters Selector](dma-parameters-selector.md)).

| | |
|---|---|
| **Pass** | `xla::tpu::sparse_core::ExpandTiledMemRefsPass` (pattern install `…::populateTpuPatterns` @ `0x134e7700`) |
| **Source provenance** | `.rodata` string `platforms/xla/sparse_core/mlo/passes/expand_tiled_memrefs_pass.cc` (line 230 in `expandTiledMemRef`) |
| **Type de-tiling** | `xla::tpu::sparse_core::(anonymous namespace)::expandTiledMemRef(mlir::MemRefType)` @ `0x134e16e0` |
| **Shape de-tiling** | free `mlir::tpu::(anonymous namespace)::getExpandedShape(ArrayRef<long>, ArrayRef<xla::Tile>, bool)` @ `0x14aa7dc0`; wrapper `TiledLayoutAttr::getExpandedShape` @ `0x14aa7cc0` |
| **Stride de-tiling** | `mlir::tpu::TiledLayoutAttr::getExpandedStrides()` @ `0x14aa7400` |
| **Index de-tiling** | `xla::tpu::sparse_core::(anonymous namespace)::expandTiledIndices(ValueRange, ArrayRef<xla::Tile>, Location, OpBuilder&)` @ `0x134e1f20` |
| **Contiguous issuer** | `mlir::tpu::LowerMemrefToMlo::issueContiguousTransfer` @ `0x1350a3e0` |
| **Retiling reshape** | `mlir::tpu::LowerPassBase::getDMATiling(...) const` @ `0x13518660` |
| **Per-tile loop** | `mlir::tpu::LowerPassBase::issueRetiledTransfer(...)` @ `0x13519480` |
| **Stage-2 dispatch** | `mlir::tpu::LowerMemrefToMlo::lowerEnqueueDma(EnqueueDMAOp, EnqueueDMAOpAdaptor, ConversionPatternRewriter&)` @ `0x135105a0` |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against the IDA decompile (primitives, index algebra, dispatch all verified) |

---

## 1. The De-Tiling Algebra — Why a Tiled DMA Cannot Lower Early

### Purpose

A `tpu.enqueue_dma` whose operands are `tpu::Tile`-layout memrefs cannot be turned into a SparseCore descriptor until the tile layout is unfolded into explicit dimensions and row-major strides. The descriptor's address operands are plain flat offsets — the DMA engine walks a stride list, not a tile grid — so the compiler must first *de-tile* both the type (the memref's layout) and the indices (the logical access). The de-tiling is two halves of one identity: the shape split `(ceil(d/t), t)` and the index split `(i/t, i%t)` are mirror images, and the suffix-product strides are what make an expanded index resolve to a single word offset. This is the arithmetic that `ExpandTiledMemRefsPass` exists to perform, and it is why the bridge-cast had to wait for it.

### The de-tiling identity, at a glance

| logical object | de-tiled / expanded form | computed by (`@VA`) |
|---|---|---|
| tiled dim `d` with tile `t` | `(ceil(d/t)` outer, `t` inner`)` | `getExpandedShape` @ `0x14aa7dc0` (ceil-div) |
| tiled stride | stored outer strides ++ suffix-product(inner) | `getExpandedStrides` @ `0x14aa7400` (`imul` chain) |
| tiled `MemRefType` | flat strided `MemRefType` | `expandTiledMemRef` @ `0x134e16e0` |
| logical index `i` | `(i/t` outer, `i%t` inner`)` | `expandTiledIndices` @ `0x134e1f20` (`divui`/`remui`) |
| &nbsp;&nbsp;`vector<i32>` index | `broadcast(const i32 t)`, then `divui`/`remui` | `expandTiledIndices` vector branch |
| physical word offset | `Σ_d expIndex[d] * expStride[d]` | the `StridedLayoutAttr` `expandTiledMemRef` produces |

> **NOTE —** "tile rank" throughout this page is `tile.dims >> 1`. A `xla::Tile` entry stores `2·tile_rank` integers — the trailing `tile_rank` are the tile sizes — and the decompile reads them with a 24-byte (`0x18`) `ArrayRef` stride. The shape, stride, and index primitives all consume the same `ArrayRef<xla::Tile>`, which is exactly why their three splits stay consistent.

---

## 2. `expandTiledMemRef` — Tiled `MemRefType` → Flat Strided `MemRefType`

### Purpose

This is the *type-level* de-tiling: it converts a rank-`N` tiled memref into a rank-`(N + tile_rank)` flat strided memref whose address of expanded-index `e` is `Σ_d e[d]·expStride[d]`. Everything downstream addresses this flat memref; the tile structure is gone, unfolded into explicit outer/inner dims plus a `StridedLayoutAttr`.

### Algorithm

The body at `0x134e16e0` is byte-confirmed against the decompile, including the source filename and line the assertion carries.

```c
// expandTiledMemRef — @ 0x134e16e0
// (platforms/xla/sparse_core/mlo/passes/expand_tiled_memrefs_pass.cc:230)
MemRefType expandTiledMemRef(MemRefType orig):
    layout = orig.getLayout()
    // GATE: layout MUST be a TiledLayoutAttr; else absl LogFatal.
    // decompile: [layout_impl + 0x88] == TypeIDResolver<TiledLayoutAttr>::id ?
    if not isa<TiledLayoutAttr>(layout):
        LogFatal("isa<TiledLayoutAttr>(orig_type.getLayout())")   // @0x134e193c

    expShape = TiledLayoutAttr::getExpandedShape(layout, orig.getShape())  // §3
    expStr   = TiledLayoutAttr::getExpandedStrides(layout)                 // §4

    suffix = computeSuffixProduct(expShape)        // @0x1ca6fac0 — row-major strides of expShape
    if bcmp(expStr, suffix) == 0:                  // CONTIGUOUS: identity strided layout
        stridedLayout = <identity for expShape>
    else:
        stridedLayout = StridedLayoutAttr::get(ctx, /*offset=*/0, /*strides=*/expStr)  // @0x1d85be00

    return MemRefType::get(expShape, orig.getElementType(),
                           stridedLayout, orig.getMemorySpace())           // @0x1d897680
```

The `bcmp` against `computeSuffixProduct` is a contiguity short-circuit: when the stored expanded strides already equal the row-major suffix-product of the expanded shape, the flat memref needs no explicit strided layout (the identity layout suffices). Otherwise the explicit `StridedLayoutAttr` with offset `0` is stamped.

> **GOTCHA —** the `TiledLayoutAttr` gate is a hard `absl::log_internal::LogMessageFatal`, not a soft `emitError`. `expandTiledMemRef` is an internal primitive that is only ever reached with a tiled layout; a non-tiled memref here is a compiler invariant break, not a user error. A reimplementation must guarantee the gate upstream rather than expect graceful failure.

---

## 3. `getExpandedShape` — Each Tiled Dim Splits `(ceil(d/t), t)`

### Purpose

The shape half of the de-tiling. A tiled dim `d` with tile size `t` becomes an OUTER tile-count `ceil(d/t)` followed by an INNER tile-size `t`. This is the exact mirror of the index split in §5, and it is what makes the outer index range over the tile grid and the inner index range inside a tile.

### Algorithm

The free function `getExpandedShape(ArrayRef<long> shape, ArrayRef<xla::Tile> tiles, bool strict)` @ `0x14aa7dc0` returns `optional<SmallVector<long>>` (present-byte at result `+0x40`). The `TiledLayoutAttr::getExpandedShape` wrapper @ `0x14aa7cc0` reads the layout's `ArrayRef<Tile>` (`ptr @ +0x8`, `count @ +0x10`) and forwards.

```c
// getExpandedShape (free) — @ 0x14aa7dc0
optional<SmallVector<long>> getExpandedShape(ArrayRef<long> shape,
                                             ArrayRef<Tile> tiles, bool strict):
    result = copy(shape)
    for tile in tiles:                       // 24-byte (0x18) stride per Tile
        tile_rank = tile.dims >> 1
        // operate on the LAST tile_rank dims of the accumulating result
        for k in 0 .. tile_rank-1:
            d = result[end - tile_rank + k]
            t = tile.sizes[k]                // [tile.ptr + k*8]
            if d == kDynamic:                // 0x8000000000000000 passes through
                continue
            outer = ceil_div(d, t)           // (d>0) ? (d-1)/t + 1 : 0   — setne/sub/idiv/add
            if strict and (d % t != 0):
                return nullopt               // exact-tiling required; fail @0x14aa8190
            result[end - tile_rank + k] = outer
            result.append(t)                 // APPEND the inner tile size
    return result
```

The ceiling-division is the standard `(setne bl; sub d,bl; div t; add bl)` idiom — `(d-1)/t + 1` for `d > 0`. `kDynamic` (`0x8000000000000000`) dims pass through unchanged.

> **NOTE —** `strict` gates exact divisibility. `getExpandedStrides` (§4) calls this with `strict = 1` (`r9d = 1` @ `0x14aa74d8`) because computing inner strides from a ragged tile would be meaningless; `expandTiledMemRef`'s type path uses the wrapper. A nonzero `idiv` remainder under `strict` takes the divisibility-fail path that sets the present-byte to `0`, returning `nullopt`.

---

## 4. `getExpandedStrides` — Row-Major Suffix-Product Over Inner Dims

### Purpose

The stride half. The physical address of an expanded index `e` is `Σ_d e[d]·expStride[d]`; this function builds the `expStride` vector that `expandTiledMemRef` wraps into the `StridedLayoutAttr`.

### Algorithm

`TiledLayoutAttr::getExpandedStrides()` @ `0x14aa7400`:

```c
// getExpandedStrides — @ 0x14aa7400
SmallVector<long> getExpandedStrides():
    result = copy(layout.storedExpandedStrides)   // [impl+0x18] ptr, [impl+0x20] count
                                                  // the precomputed OUTER strides
    inner = getExpandedShape(/*strict=*/1)        // @0x14aa74de — the inner-tile shape
    // row-major SUFFIX PRODUCT over the inner shape (unrolled imul chain)
    for d in inner:                               // @0x14aa7540..0x14aa7589
        stride[d] = product(inner[d+1 ..])
    result.append(innerStrides)
    return result
```

The expanded stride vector is therefore the concatenation of the layout's *stored* outer strides and the freshly-computed row-major *inner-tile* strides — `stride[inner_d] = prod(inner_shape[d+1 ..])`. Composed with the index split of §5, an expanded index resolves to a single physical word offset.

---

## 5. `expandTiledIndices` — Index `i` → `(i/t, i%t)`

### Purpose

The index twin of `getExpandedShape`. Each logical index `i` along a tiled dim with tile size `t` becomes the PAIR `(i/t [which tile], i%t [offset within tile])`. Composed over the whole index list, this addresses the de-tiled flat memref of §2: the outer (div) indices select the tile grid, the inner (rem) indices select within the tile, and the suffix-product strides of §4 turn the pair into the word offset.

### Algorithm

`expandTiledIndices(ValueRange logicalIndices, ArrayRef<xla::Tile> tiles, Location, OpBuilder&)` @ `0x134e1f20` returns `SmallVector<Value>`. Byte-confirmed against the decompile (the `divui`/`remui`/`ConstantIndexOp`/`Broadcast`/`isIndex`/`isSignlessInteger(32)`/`emitError` call census is all present).

```c
// expandTiledIndices — @ 0x134e1f20
SmallVector<Value> expandTiledIndices(ValueRange logical, ArrayRef<Tile> tiles,
                                      Location loc, OpBuilder& b):
    result = copy(logical)                        // dereference_iterator @0x1d8d9c60
    for tile in tiles:                            // 24-byte stride; r12 += 0x18 @0x134e23ea
        tile_rank = tile.dims >> 1
        tail = result[end - tile_rank .. end]     // the last tile_rank indices
        div_block = []; rem_block = []
        for k, idx in enumerate(tail):
            t = tile.sizes[k]                     // [tile.ptr + k*8]
            if idx.getType().isIndex():           // @0x1d8e86e0
                c = arith::ConstantIndexOp::create(b, loc, t)      // @0x1cacab00
            elif idx is vector<i32>:              // VectorType elem isSignlessInteger(32) @0x1d8e87e0
                k0 = arith::ConstantOp::create(b, loc, i32, i32Attr(t))  // @0x1cb002e0
                c  = vector::BroadcastOp::create(b, loc, vecTy, k0)      // @0x178d98c0
            else:
                return emitError("... must be index or i32 vector")     // @0x134e2400
            div_block.append( arith::DivUIOp::create(b, loc, idx, c, /*isExact=*/false) )  // @0x1cb06d00
            rem_block.append( arith::RemUIOp::create(b, loc, idx, c) )                     // @0x1cb20800
        // replace the last tile_rank indices with 2*tile_rank values:
        //   FIRST the div (outer) block, THEN the rem (inner) block
        result[end - tile_rank .. end] = div_block ++ rem_block         // memcpy @0x134e2360
    return result
```

> **GOTCHA —** the replacement layout is `[outer_0..outer_{r-1}][inner_0..inner_{r-1}]` — the entire div block first, then the entire rem block — **not** interleaved `(outer, inner)` pairs. The decompile's temp buffer is laid out `[div...][rem...]` and the final memcpy writes `2·tile_rank` values over the `tile_rank` it replaces. A reimplementation that interleaves the pairs will mis-order the operands against the expanded stride table of §4 (which expects outer dims then inner dims) and silently corrupt every tiled address.

> **NOTE —** the `vector<i32>` branch is the lane-broadcast form: the divisor `t` is materialized as an `i32` `arith.constant` then `vector.broadcast`-splatted to the index vector type so the `divui`/`remui` operate lane-wise. The scalar and vector forms produce identical `(i/t, i%t)` algebra; only the divisor materialization differs.

> **NOTE —** the consumers of `expandTiledIndices` live on adjacent pages. `expandSCStreamStart<…StreamStartOp>` (`0x134ea400`) runs each operand group (`{Src,Dst,Offset,Sflag}Indices`) through `expandTiledIndices` then `MutableOperandRange::assign`. The dynamic-shape twin `expandTiledShape` (`0x134e2aa0`) expands a possibly-dynamic shape into the same `(outer, inner)` form, emitting `arith` ops for the runtime dims — its full body is not decoded here (LOW).

---

## 6. `issueContiguousTransfer` — The Zero-Index Bulk Move

### Purpose

The terminal transfer issuer: a single whole-memref bulk move that emits exactly one descriptor — one `DmaSimpleStartOp` (for `kDma`) or one `LinearStreamStartOp` (for `kStream`). It addresses the WHOLE memref from offset `0` (all index operands are `ConstantIndexOp(0)`); the actual base address is the memref's own base. `issueRetiledTransfer` (§8) calls this once per tile via the per-tile callback, so this is the per-tile leaf of the tiled path as well as the direct emitter for the contiguous path.

### Algorithm

`issueContiguousTransfer(OpBuilder&, EnqueueDMAOp, Value srcBase, Value dstBase, Value length, Value sflag, …, optional<DeviceAndCoreIds>, TransferKind kind, bool)` @ `0x1350a3e0`. The `GetMemorySpace`×2, zero-index `ConstantIndexOp(0)`, `sc.inside_trace_region` probe, `DivUIOp` stream-length divide, `TraceLocalDma`, and both `…StartOp::create` calls are all confirmed in the decompile.

```c
// issueContiguousTransfer — @ 0x1350a3e0
LogicalResult issueContiguousTransfer(b, op, srcBase, dstBase, length, sflag,
                                      ..., kind, ...):
    srcSpace = sparse_core::GetMemorySpace(getMemRefType(srcBase))   // @0x1459c7e0
    dstSpace = sparse_core::GetMemorySpace(getMemRefType(dstBase))
    // MemorySpace enum: HBM=4, TILE_SPMEM=2, SPMEM=3  (Intra-Chip Descriptor)

    srcZeroIdx = [ ConstantIndexOp(0) ] * rank(src)   // zero offsets — whole memref
    dstZeroIdx = [ ConstantIndexOp(0) ] * rank(dst)   // @0x1350a4f2 / @0x1350a633

    // trace-region flag: getInherentAttr("sc.inside_trace_region", 22) / DictionaryAttr fallback
    insideTrace = probe sc.inside_trace_region        // matches the stage-1 bridge tag

    dstIsHbm = (dstSpace == 4)                        // memspace XOR-4 test @0x1350a870

    switch kind:
      case kDma:                                      // @0x1350abc4
        sparse_core::DmaSimpleStartOp::create(b, loc,
            srcBase, srcZeroIdx, dstBase, dstZeroIdx,
            length, sflagIdx, sflag)                  // @0x145b9740  → SimpleDma slot 0x6
      case kStream:                                   // @0x1350a9c0
        stripe    = target()->[+0x948]->[+0xa4]       // SPMEM stripe / DMA-word granularity (bytes)
        streamLen = DivUIOp(length, ConstantIndexOp(stripe), /*isExact=*/false)  // @0x1cb06d00
        trace     = Target::TraceLocalDma()           // @0x1d6186c0
        sparse_core::LinearStreamStartOp::create(b, loc,
            /*b1=*/false, /*b2=*/dstIsHbm, /*b3=*/trace, /*opcode=*/0,
            srcBase, srcZeroIdx, dstBase, dstZeroIdx,
            /*off=*/streamLen, /*IntegerAttr=*/null, sflag, sflagIdx)  // @0x145e3440 → Stream Linear slot 0x3b
      default:
        return emitOpError("unsupported transfer ...")  // @0x1350aecb
```

| issuer (kind) | `sparse_core::…::create` (`@VA`) | slot | operand binding |
|---|---|---|---|
| `kDma` | `DmaSimpleStartOp::create` `0x145b9740` | SimpleDma `0x6` | `(srcBase, srcZeroIdx, dstBase, dstZeroIdx, length, sflagIdx, sflag)` → `{src,dst}mem_{core,mem}_id`, `dma_length`, `dest_sync_flags` |
| `kStream` | `LinearStreamStartOp::create` `0x145e3440` | Stream Linear `0x3b` | `(false, dstIsHbm, traceLocalDma, opcode=0, srcBase, srcZeroIdx, dstBase, dstZeroIdx, off=length/stripe, IntegerAttr=null, sflag, sflagIdx)` → `off_tile_start_offset`, stream verb, trace bit |

> **NOTE —** the slot opcodes (`SimpleDma 0x6`, `Stream Linear 0x3b`, `GeneralDma 0x8`, `SingleStrided 0x7`) and the descriptor field semantics those create-args feed are documented on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md). This page binds the *compiler-side operand origin*; the field layout is cross-referenced, not re-derived.

> **HIGH —** the stream divisor is `target()->[+0x948]->[+0xa4]`, a runtime chip-parts geometry DWORD that the analysis matches to the SPMEM-stripe / DMA-word granularity (`SparseCoreSpmemStripeGranularityBytes` const accessor `0x1d499440` returns `32`). The DIVISION of `length` into stripe/word units is byte-confirmed; the exact runtime-field identity beyond the offset chain is inferred. The `{b1=false, b2=dstIsHbm, b3=TraceLocalDma, opcode=0, IntegerAttr=null}` argument VALUES are byte-confirmed; their precise mapping onto the `LinearStreamStartOp` ODS slot fields (`hbm4b` / `enable_trace` / stream verb) is structural — see [Intra-Chip DMA Descriptor](intra-chip-descriptor.md).

---

## 7. `getDMATiling` — The Retiling Reshape

### Purpose

When one DMA endpoint is tiled and the other is contiguous-but-reinterpretable, the two layouts must be reconciled before a per-tile transfer can be issued. `getDMATiling` computes the per-tile STRIDE structure (the tile strides in element/word units) and a retiled `TiledLayoutAttr`, so the tiled side's tile grid maps onto the contiguous side's flat layout.

### Algorithm

`getDMATiling(OpBuilder&, Operation*, TypedValue<MemRefType> src, TypedValue<MemRefType> dst, TiledLayoutAttr& srcLayout, TiledLayoutAttr& dstLayout, SmallVectorImpl<long>& outStrides, long& outElemsPerStride) const` @ `0x13518660` (a `const` method, mangled `ZNK…`).

```c
// getDMATiling — @ 0x13518660
LogicalResult getDMATiling(b, op, src, dst, &srcLayout, &dstLayout,
                           &outStrides, &outElemsPerStride):
    // GATE 1: both layouts must be tiled
    if src.getTiles().empty() or dst.getTiles().empty():        // @0x14a9dac0
        return emitOpError("... layout must be tiled")          // @0x135186db

    // GATE 2: tile ranks match AND rank in {1,2}
    tile_rank = src.tile_dims >> 1
    if tile_rank != dst.tile_rank or tile_rank not in {1,2}:    // lea ecx,[rbx-3]; cmp ecx,0xfffffffd
        return emitOpError("... only supports 1-D and 2-D tiling")

    if leading tile dims MATCH:                                  // contiguous reshape
        getElementTypeBitwidth() gate                            // @0x11233400
        outStrides = existing tile strides / element stride
    else:                                                        // 2-D reshape @0x13518ad9
        require Tile::operator==(inner(src), inner(dst))         // @0x13519340 — inner tiles match
        require canReinterpretToUntiledMemref(contiguousSide,
                  {SublaneCount, LaneCount}, false)              // @0x14b74480 — flatten to sublane×lane
        require elementBitwidth == 32 and leadingDim == 1
        outStrides = contiguousSide.getTileStrides() / untiledElemStride   // idiv, divisibility-checked
            // nonzero remainder -> emitOpError "... stride not divisible"  @0x13518f95
        retiled = TiledLayoutAttr::get(ctx, tiles, dividedStrides)         // @0x14a9d980
        *outLayoutRef = retiled                                  // write *[rbp+0x10]
        outElemsPerStride = 1                                    // write *[rbp+0x20]
    return success
```

`outStrides` feeds the `SingleStrided` / `GeneralDma` stride operands; `outElemsPerStride` feeds `elements_per_stride`. The `canReinterpretToUntiledMemref` gate is what restricts retiling to a contiguous side that flattens to the physical MXU sublane×lane tile geometry (`SublaneCount` @ `0x1d60f300`, `LaneCount` @ `0x1d60f400`).

---

## 8. `issueRetiledTransfer` — The Per-Tile Descriptor Loop

### Purpose

Turns a tiled DMA into a SEQUENCE of per-tile contiguous transfers. Each tile's outer index becomes a base offset (`tile_index × tile_stride × elemBytes`); the inner offset is handled by the per-tile single transfer. The signed `Div/Rem/Mul` here is the retiled twin of `expandTiledIndices`' unsigned `Div/Rem` — the *same* `(i/t, i%t)` algebra, expressed as the descriptor's base + stride operands rather than as an index `ValueRange`.

### Algorithm

`issueRetiledTransfer(OpBuilder&, EnqueueDMAOp, Value src, Value dst, Value length, Value sflag, ValueRange dynShape, long, unsigned numTiles, optional<DeviceAndCoreIds>, int, function<LogicalResult(OpBuilder&, EnqueueDMAOp, Value, Value, DmaParameters const&, optional<DeviceAndCoreIds>, int)> const& perTileCallback)` @ `0x13519480`.

```c
// issueRetiledTransfer — @ 0x13519480
LogicalResult issueRetiledTransfer(b, op, src, dst, length, sflag, dynShape,
                                   ..., numTiles, deviceIds, ..., perTileCallback):
    getDMATiling(b, op, src, dst, srcLayout, dstLayout, outStrides, elemsPerStride)  // §7 @0x135195fa
    getTiledDynamicShape(staticShape, dynShape, ...)            // @0x13516840 — resolve dynamic extents
    totalTiles = product(outStrides) * elementBitwidth          // vpmulld @0x13519780 ; imul @0x135197fd

    for each tile dim with tile-stride s:                       // @0x135199bb..0x13519ae0
        c0    = $_0(s)                                          // i32 ConstantOp @0x1351a7c0
        inner = createOrFold<arith::RemSIOp>(idx, c0)           // idx % s  @0x1351ad20
        c1    = $_0(...)
        outer = createOrFold<arith::DivSIOp>(idx, c1)           // idx / s  @0x1351af20
    scale strides by element size:  imul stride, elemBits       // @0x13519b53 + vpmuludq @0x13519bb0
    perTileBase = createOrFold<arith::MulIOp>(tileIndex, tileStride)  // @0x1351ab00

    for each tile:
        // invoked via std::function::operator()  call [rax+0x18]  @0x1351a067 / @0x1351a106
        perTileCallback(b, op, perTileSrcBase, perTileDstBase,
                        DmaParameters, deviceIds, tileIndex)
        // DmaParameters predicate queried via call [rax+0x10]  @0x13519990

    // MULTI-TILE FALLBACK: issueRolledTransfer for the rolled (loop) form
    // @0x13516ca0 / @0x1351a1ea
    return success
```

The per-tile callback is, in the `lowerEnqueueDma` path (§9), a `std::function` wrapping `lowerEnqueueDma::$_0` (policy func `0x13516720`) — a closure that calls `issueContiguousTransfer` (§6) once per tile. When the tile grid is large enough that unrolling would explode code size, the path falls back to `issueRolledTransfer` (`0x13516ca0`) which emits a loop or rolled multi-stride descriptor; that emitter is owned by [Rolled / Strided / General Emitters](rolled-strided-general.md).

> **NOTE —** `getTiledDynamicShape` (`0x13516840`) and the `DmaParameters` struct (the 5th callback arg, queried via the `[rax+0x10]` predicate at `0x13519990`) are named in the signatures but their bodies/field layouts are **not** decoded here (LOW). They are the runtime-shaped half of the per-tile parameter bundle.

---

## 9. `lowerEnqueueDma` — The Stage-2 Dispatch

### Purpose

This is the consumer of the deferred DMA: the conversion pattern `ExpandTiledMemRefsPass` registers for the bridged `tpu.enqueue_dma`, which ties the whole datapath together and erases the original op. It is the closure of the loop the [LowerToMlo bridge-cast](../compiler/lower-to-mlo-dma-bridge.md) opened — stage 1 parked the op behind a `sc.unlowering` cast precisely because *this* function needs the tile layout (`expandTiledIndices` / `getDMATiling`) that only `ExpandTiledMemRefsPass` owns.

### Algorithm

`lowerEnqueueDma(EnqueueDMAOp, EnqueueDMAOpAdaptor, ConversionPatternRewriter&)` @ `0x135105a0`. The dispatch order — `getVerifiedDmaShapes`, `getTransferKind<EnqueueDMAOp>`, `getRemoteDeviceAndSparseCoreIds<EnqueueDMAOp>`, the `issueRetiledTransfer` vs `get_transfer_size_bytes`+`issueContiguousTransfer` fork, then `eraseOp` — is byte-confirmed in the decompile.

```c
// lowerEnqueueDma — @ 0x135105a0  (the ExpandTiledMemRefs stage-2 consumer)
LogicalResult lowerEnqueueDma(EnqueueDMAOp op, adaptor, ConversionPatternRewriter& rw):
    getVerifiedDmaShapes(b, locGen, op, srcMemref, dstMemref, ...)   // @0x13509dc0 — transfer-compatible?
    kind     = getTransferKind<EnqueueDMAOp>(target, srcSpace, dstSpace)  // @0x135114a0 → kDma/kStream
    deviceIds= getRemoteDeviceAndSparseCoreIds<EnqueueDMAOp>(b, locGen, op, src, dst)  // @0x13511660

    if memref is TILED:                                  // needs per-tile expansion
        issueRetiledTransfer(b, op, src, dst, len, sflag, dynShape,
                             ..., deviceIds, ..., /*callback=*/$_0)  // @0x13510e97
                             // $_0 = lowerEnqueueDma::$_0 (0x13516720) wrapping issueContiguousTransfer
    else:                                                // CONTIGUOUS
        sizeBytes = get_transfer_size_bytes(b, locGen, mixedShape, memref)  // @0x13511be0
        issueContiguousTransfer(b, op, src, dst, sizeBytes, sflag,
                                ..., deviceIds, kind, ...)              // @0x13510fbb

    rw.eraseOp(op)                                       // @0x1c951760 — original tpu.enqueue_dma erased
    return success
```

### End-to-end trace

```text
  STAGE 1 — LowerToMlo (ModuleOp pass)   [../compiler/lower-to-mlo-dma-bridge.md]
    tpu.enqueue_dma %src, %dst, %sflag    (tiled MemRef operands)
      └─ parked behind builtin.unrealized_conversion_cast {sc.unlowering}
         + op tagged sc.unlowered
  ── applyFullConversion succeeds, bridge intact ──
  STAGE 2 — ExpandTiledMemRefsPass (tile layout now fixed)   [THIS PAGE]
    lowerEnqueueDma  0x135105a0
      ├─ getVerifiedDmaShapes / getTransferKind / getRemoteDeviceAndSparseCoreIds
      ├─ TILED  → issueRetiledTransfer 0x13519480
      │            ├─ getDMATiling 0x13518660           (retile reshape)
      │            └─ per tile: $_0 → issueContiguousTransfer 0x1350a3e0
      └─ CONTIG → issueContiguousTransfer 0x1350a3e0
                    ├─ kDma    → DmaSimpleStartOp::create   0x145b9740  (slot 0x6)
                    └─ kStream → LinearStreamStartOp::create 0x145e3440 (slot 0x3b)
      └─ eraseOp(op)  0x1c951760
    (the underlying memref type is de-tiled by expandTiledMemRef 0x134e16e0;
     index operands by expandTiledIndices 0x134e1f20)
```

---

## What Is Not On This Page

- **The stage-1 bridge mechanics** — the `sc.unlowering` / `sc.unlowered` tagging, the per-operand selective cast, the `ConversionTarget` legality predicates, and `substituteUnloweringConversionCastOp` — are owned by [LowerToMlo DMA Bridge-Cast](../compiler/lower-to-mlo-dma-bridge.md). This page begins where that page ends: at `lowerEnqueueDma`.
- **The descriptor field layout** — the `(mem_id, core_id)` polymorphic memory-space enums, the `Src`/`Dst` opcode enums, the `(length, length_granule)` size pair, and the `+0x18`..`+0x5c` field span the `…StartOp::create` calls populate — is owned by [Intra-Chip DMA Descriptor](intra-chip-descriptor.md).
- **The rolled / strided / general transfer bodies** — `issueRolledTransfer` (`0x13516ca0`), the `DmaGeneralStartOp` / `DmaSingleStridedStartOp` emitters, and the multi-stride loop-nest — are owned by [Rolled / Strided / General Emitters](rolled-strided-general.md). This page identifies `issueRolledTransfer` as the multi-tile fallback but does not decode it.
- **The Simple-vs-Strided selection** — the `DmaParameters` predicate that chooses which descriptor variant a transfer uses — is owned by [DmaParameters Selector](dma-parameters-selector.md).
- **`getTiledDynamicShape` and the `DmaParameters` struct layout** — the runtime-shaped tiling half — are located (`0x13516840`; predicate at `0x13519990`) but not decoded; marked LOW above.

---

## Cross-References

- [LowerToMlo DMA Bridge-Cast](../compiler/lower-to-mlo-dma-bridge.md) — the stage-1 producer: how `tpu.enqueue_dma` is parked behind the `sc.unlowering` bridge-cast that *this* pass consumes via `lowerEnqueueDma`
- [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) — the descriptor record the `DmaSimpleStartOp` / `LinearStreamStartOp` this page emits ultimately fill: `mem_id`/`core_id` enums, opcode enums, field layout, slot opcodes
- [Rolled / Strided / General Emitters](rolled-strided-general.md) — `issueRolledTransfer` and the `DmaGeneralStartOp` / `DmaSingleStridedStartOp` transfer bodies the retiled path falls back to
- [DmaParameters Selector](dma-parameters-selector.md) — the Simple-vs-Strided `DmaParameters` selection that classifies each transfer the per-tile callback issues
