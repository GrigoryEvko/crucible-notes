# IndirectVregStream — The VREG-Driven Gather Loop

> *Every function address, operand index, struct offset, shift/mask, and opcode value on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`). `.text` VA equals file offset at `0xe63c000`, `.rodata` at `0x84a0000` — both identity-mapped. Addresses are from the **gfc** (6acc60406) instances unless tagged otherwise; the **glc** (Ghostlite) and **vfc** (Viperfish) siblings carry the same schema at different addresses. Other versions differ.*

## Abstract

`IndirectVregStream` is the SparseCore Stream form that reads its per-element offset stream **from a vector register** instead of from a memory id-list. It is the fourth `oneof` form of `SparseCoreStream` (slot opcode `0x38`), structurally identical to [`IndirectStream`](stream-gather-scatter.md) (`0x39`) except in its leading four proto fields — the two SREG operands (`indirect_offset`, `indirect_size_and_hbm4b_offset`) are replaced by a VREG selector (`indirect_offsets`), a lane mask (`valid_offset_mask`), and an explicit base (`off_tile_start_offset`). Everything from proto field `#5` down — the entire stream-control tail — is byte-for-byte the same as `IndirectStream`.

This page owns **the VREG read loop and the per-index DMA issue**: how the offset stream is sourced from a vector register and a lane mask, how the SC-side MLIR op carries the VREG operands as plain SSA `Value`s (not memref index groups), how the index expansion flattens exactly three index groups (and conspicuously *not* an offset group), and how the LLVM lowering selects one of nine per-route DMA-issue intrinsics keyed on the `(hbm4b, src-space, dst-space)` triple. The slot **bit encoding** and the **indirect-DMA descriptor** proper live on [Stream Gather/Scatter](stream-gather-scatter.md) — this page links to them rather than re-deriving them. The engine-assignment policy that pins this form to TEC lives on [getSequencerType](getsequencertype.md).

The reimplementation contract:

- **The offset source is a register, not a memory list.** `IndirectStream` reads its offset/size from two 5-bit SREG selectors; `IndirectVregStream` reads its offset *stream* from a 6-bit VREG selector (`indirect_offsets`) plus a separate 6-bit VREG selector for the per-element access length (`indirect_access_lengths`), gated by `valid_offset_mask`. The id stream lives in the TEC vector register file — which is exactly why this form is **TEC-only**.
- **The loop body is identical; only the *index supply* differs.** The per-element address arithmetic, the row-stride multiply, the accumulate `StreamOpcode`, the filter, the sliding-window post-update — all the [common control tail](stream-gather-scatter.md#the-common-control-tail) fields — are the same proto layout as `IndirectStream`. A reimplementer encodes both forms from one struct; the form discriminator only picks how each lane's offset is fetched.
- **The SC op carries three index groups, not four.** `IndirectVectorStreamStartOp` expands `{Src, Dst, Sflag}Indices` through `expandTiledIndices`; there is **no** `OffsetIndices` group (the offsets are a VREG operand) and **no** `getIndirectListType` attribute. This is the structural delta versus `IndirectStreamStartOp`, confirmed in both the index-expansion and the LLVM-lowering bodies.
- **Per-index issue is a 9-way intrinsic dispatch.** The LLVM lowering builds nine candidate emitter lambdas and selects one by matching the runtime `(hbm4b-flag, src-memory-space, dst-memory-space)` triple. An unmatched triple emits `"IndirectVectorStreamStartOp doesn't support transfer from source memory space … to destination memory space …"`.

| | |
|---|---|
| **Proto form** | `IndirectVregStream` — `SparseCoreStream` `oneof` member, slot opcode `0x38` |
| **Sibling** | `IndirectStream` (`0x39`); shares proto fields `#5…#22` byte-for-byte |
| **VREG offset selector** | `indirect_offsets` — 6-bit VREG selector, slot `+0x28 >> 27 & 0x3F` |
| **VREG length selector** | `indirect_access_lengths` — 6-bit VREG selector, slot `+0x30 >> 2 & 0x3F` |
| **Lane gate** | `valid_offset_mask` — which VREG lanes carry a valid offset this pass |
| **Gather base** | `off_tile_start_offset[_valid]` — explicit base (replaces SREG `indirect_offset`) |
| **SC-side op** | `sc_tpu.indirect_vector_stream_start` / `.indirect_vector_stream_add_start` |
| **Op traits** | `ZeroResults`, `AtLeastNOperands<6>`, `AttrSizedOperandSegments` |
| **Index groups** | `{Src, Dst, Sflag}Indices` — **no** `OffsetIndices` |
| **Index expansion** | `expandSCStreamStart<IndirectVectorStreamStartOp>` @ `0x134eb880` (Add @ `0x134ebd20`) |
| **Builder** | `lowering_util::InitiateIndirectVectorStreamOperation` @ `0x13d870e0` / `0x13d866c0` |
| **Per-index issue** | `IndirectVectorStreamStartOpLowering::rewriteSparseCoreStreamOpToLLVM` @ `0x13560ce0` |
| **Engine** | TEC only (the VREG operand requires the TEC vector register file) |
| **Confidence** | CONFIRMED (decompile/accessor/symbol anchored) unless a row says otherwise |

---

## Where This Form Fits

`SparseCoreStream` is a shared header plus a four-way `oneof` of addressing forms. The full message, its shared control tail, and the per-form opcodes are documented on [Stream Gather/Scatter](stream-gather-scatter.md). The four forms and their offset sources:

| Form | Opcode | Per-element offset comes from |
|---|---|---|
| `LinearStream` | `0x3b` | a single linear base — no per-element offset |
| `StridedStream` | `0x3a` | one stride dimension — no per-element offset |
| `IndirectStream` | `0x39` | a **memory** offset id-list (two 5-bit SREG operands) |
| `IndirectVregStream` | `0x38` | a **vector register** offset stream (a 6-bit VREG selector + lane mask) |

`IndirectStream` and `IndirectVregStream` are the two *indirect* (gather/scatter) forms: both walk an offset stream element by element and issue one off-tile transfer per element. They differ only in *where the offset stream is read from*. `IndirectStream` round-trips the ids through an SREG-addressed memory list; `IndirectVregStream` consumes them straight out of a TEC vector register. This page is the second case.

> **NOTE — this is the variable-length / minibatch inner-loop gather.** The VREG form is the natural fit when the embedding-id stream is *produced in a register* — for example the output of a TEC scan/uniquify over a minibatch — and must be gathered lane-parallel without first materializing it as a memory id-list. The `valid_offset_mask` gates which lanes fire, so a ragged minibatch maps directly onto a fixed-width VREG sweep. This ties to the per-physical-core minibatch model (see [Embedding Minibatching](embedding-minibatching.md)); it is the only Stream form whose offset operand is a register file, which is exactly why it cannot live in the SCS or TAC bundle. (Use-case inference: HIGH.)

---

## The Proto Delta — Fields `#1…#4`

`IndirectVregStream` and `IndirectStream` share proto fields `#5…#22` identically (`sync_flag_core_type`, `sync_flag_count_type`, `set_done_bit`, `tile_local_stride`, `post_update_circular_buffer`, `indirect_list_type`, `indirect_list_stride`, `indirect_filter_en`, `trace_en`, `stream_opcode`, `tile_local_memory_type`, `tile_local_stream_type`, `off_tile_memory_type`, `gather_scatter_add_is_b16`, `s0_x`, `s0_y`, `s1_x`, `s1_y`). Those fields and their meaning are documented in the [common control tail](stream-gather-scatter.md#the-common-control-tail). The forms differ **only** in their leading four fields:

```text
IndirectStream (#1…#4) — offset stream from MEMORY (an SREG-addressed id-list):
  #1 indirect_offset_valid                 : bool
  #2 indirect_offset                        : uint32 (5-bit SREG)   ← base/list address
  #3 indirect_size_and_hbm4b_offset_valid  : bool
  #4 indirect_size_and_hbm4b_offset         : uint32 (5-bit SREG)   ← lookup size / HBM_4B suboffset

IndirectVregStream (#1…#4) — offset stream from a VECTOR REGISTER:
  #1 indirect_offsets                       : uint32   ← a VREG selector (the offset stream)
  #2 valid_offset_mask                      : uint32   ← which VREG lanes carry a valid offset
  #3 off_tile_start_offset_valid            : bool
  #4 off_tile_start_offset                  : uint32   ← explicit gather BASE (like LinearStream)
```

So the swap is precise: the two SREG operands that *name registers holding the list address and the size* are replaced by (a) one VREG selector that *names the register holding the offset stream itself*, (b) a lane mask, and (c) an explicit base offset that replaces the SREG-sourced base. The hardware then reads `indirect_offsets[lane]` per lane instead of walking a memory id-list.

> **GOTCHA — the base moves from SREG to an immediate-style field.** In `IndirectStream` the gather base is one of the two SREG operands; in `IndirectVregStream` the base is `off_tile_start_offset`, the same field name `LinearStream` uses. A reimplementer porting an `IndirectStream` descriptor to the VREG form must relocate the base out of the SREG operand and into `off_tile_start_offset`, and move the id stream from memory into a VREG. The size/length, which `IndirectStream` packs into `indirect_size_and_hbm4b_offset`, becomes the separate per-lane `indirect_access_lengths` VREG selector in the slot (below).

---

## The VREG Read Loop — Slot Selectors

At the bundle level, the two VREG operands are 6-bit *selectors* — small indices that name which vector register the hardware reads the offset stream and the per-lane access length from. They are decoded by the gfc TEC-stream `…Field::GetConcatenatedValue()` accessors, read directly:

```c
// asic_sw::deepsea::gxc::gfc::isa::
//   SparseCoreTecStreamIndirectVregStreamIndirectOffsetsField::GetConcatenatedValue  @0x1ebe30a0
return (*((u64*)this + 5) >> 27) & 0x3F;          // struct +0x28, shift 27, width 6

//   …IndirectAccessLengthsField::GetConcatenatedValue  @0x1ebe30c0
return (*((u32*)this + 12) >> 2) & 0x3F;           // struct +0x30, shift 2, width 6
```

| Slot field | Struct word | Shift | Width | Accessor (gfc) | Role | Confidence |
|---|---|:---:|:---:|---|---|---|
| `IndirectOffsets` | `+0x28` | 27 | 6 | `0x1ebe30a0` | VREG selector — the offset stream | CONFIRMED |
| `IndirectAccessLengths` | `+0x30` | 2 | 6 | `0x1ebe30c0` | VREG selector — per-lane access length | CONFIRMED |
| `OffTileStartOffset` | `+0x10` | 41 | 5 | `0x1ebe3100` | gather base | CONFIRMED |
| `OffTileStartOffsetValid` | `+0x10` | 46 | 1 | `0x1ebe30e0` | base-present bit | CONFIRMED |
| `OffTileMemoryType` | `+0x10` | 47 | 3 | `0x1ebe3340` | HBM / HBM_4B / SPMEM pool | CONFIRMED |
| `StreamOpcode` | `+0x18` | 9 | 3 | `0x1ebe32c0` | accumulate mode (shared tail) | CONFIRMED |
| `TileLocalStride` | `+0x18` | 29 | 3 | `0x1ebe3160` | dst write granule (shared tail) | CONFIRMED |

The two VREG selectors are the entire delta of the *read* side. Everything else (`StreamOpcode`, `TileLocalStride`, `OffTileMemoryType`, the sync flag, the filter, the list stride) is the shared `IndirectStream` decode map — see [Stream Gather/Scatter § Decode Side](stream-gather-scatter.md#decode-side-decoded-struct-relative); only the leading operand words `+0x28`/`+0x30` carry VREG selectors rather than the SREG operands.

> **NOTE — a 6-bit selector names the VREG, not the lane count.** `indirect_offsets` and `indirect_access_lengths` are 6-bit *register selectors*; they index the TEC vector register file (≤64 addressable selectors). The number of lanes swept — the per-pass id count — is the VREG width, which is chip geometry, not a slot field. The mask `valid_offset_mask` then gates which of those lanes carry a live offset this pass. The physical VREG-file size behind the 6-bit selector was not enumerated (LOW — see Limits).

### Encode side

The gfc TEC-stream encoder writes opcode `0x38` and places the two VREG selector operands in the bundle's high region:

```text
SparseCoreTecStreamEncoder::Encode  @0x1ebe33e0   (oneof switch on *(u32*)(stream+88))
  case 8   → opcode 0x3b (59)  LinearStream
  case 9   → opcode 0x3a (58)  StridedStream
  case 10  → opcode 0x39 (57)  IndirectStream
  case 0xB → opcode 0x38 (56)  IndirectVregStream   ← case-11 branch
  opcode            @ bundle bit 181, width 6  = 0x38 (56)   BitCopy(a3,181,…,6)
  vreg sel #1 (msg member +6) @ bundle bit 283, width 6      BitCopy(a3,283,…,6)
  vreg sel #2 (msg member +7) @ bundle bit 322, width 6      BitCopy(a3,322,…,6)
```

(The TEC bundle is 64 B / 512 bits; both VREG selectors are width-6 and land in the high region. Member +6 is serialized first, member +7 second.)

> **GOTCHA — the TEC encoder's `oneof` bound is `0xb`; the SCS encoder's is `0xa`.** `SparseCoreStreamEncoder::Encode` (SCS, `@0x1eb9b4c0`) bounds its `oneof` switch at `cmp rax, 0xa` (max case 10) — it has only three forms and **no** `IndirectVreg` branch. The TEC encoder bounds at `cmp rax, 0xb` (case 11). The extra case is the entire structural reason `IndirectVregStream` is TEC-only: the SCS and TAC stream encoders cannot reach a 4th form. There are zero `SparseCoreStreamIndirectVregStream*` and `SparseCoreTacStreamIndirectVregStream*` field accessors in any generation; the form's field accessors exist only under the `SparseCoreTecStream` prefix (vfc `0x1e936240`, glc `0x1ea7be20`, gfc `0x1ebe30a0`). (This corrects an earlier per-gen table that listed `IndirectVreg` in all three bundle slots; see [Stream Gather/Scatter](stream-gather-scatter.md) for the form roster.)

---

## The SC-Side Op and Its Operand Groups

The VREG gather is expressed by the MLIR op pair `sc_tpu.indirect_vector_stream_start` (gather) and `sc_tpu.indirect_vector_stream_add_start` (gather/scatter with accumulate). The op carries the same traits as `IndirectStreamStartOp` — `ZeroResults`, `AtLeastNOperands<6>`, `AttrSizedOperandSegments` — but a different operand and attribute set:

```text
sc_tpu.indirect_vector_stream_start / .indirect_vector_stream_add_start
  Traits: ZeroResults, AtLeastNOperands<6>, AttrSizedOperandSegments
  Memref index groups (MutableOperandRange getters):
    getSrcIndicesMutable()   @ 0x145d7860   — off-tile (HBM table) source memref + indices
    getDstIndicesMutable()   @ 0x145d7920   — TILE_SPMEM destination memref + indices
    getSflagIndicesMutable() @ 0x145d7a00   — completion sync-flag operand(s)
    (NO getOffsetIndicesMutable — the offsets are a VREG operand, not a memref index group)
  Attributes (getters):
    getHbm4b()                    @ 0x145d9120   — 4-byte-granule HBM addressing
    getTileLocalLengthPerStride() @ 0x145d9160   — destination granule per stride
    getUpd()                      @ 0x145d9100   — post-update (advance circular buffer)
    getEnableTrace()              @ 0x145d9140   — hardware trace enable
    (NO getIndirectListType — the VREG form omits it)
  Operand-segment table:  getODSOperandIndexAndLength @ 0x145d7720 (op)
                                                       @ 0x145d7620 (adaptor base)
```

The structural delta versus [`IndirectStreamStartOp`](stream-gather-scatter.md#the-sc-side-op-and-its-operand-groups) is two omissions, both confirmed by the *absence* of the corresponding symbols in the decompile: there is no `getOffsetIndicesMutable` (no per-element id-list memref) and no `getIndirectListType` attribute (the word-vs-row interpretation that the memory id-list needs). The `_add_start` variant selects an accumulate `StreamOpcode` (`*_FLOAT_ADD` / `*_INTEGER_ADD`).

`AttrSizedOperandSegments` means the operand list is a concatenation of variadic segments whose sizes live in a per-op array; `getODSOperandIndexAndLength(i)` is the standard MLIR prefix-sum over that array (`@0x145d7620` sums `this[16..16+i]` — vectorized with `vpaddd` for `i ≥ 4` — and returns `(start << 0) | (len << 32)`). The VREG offset/length/mask operands and the memref groups all live in this segmented list; the getters above name the slices.

---

## Index Expansion — Three Groups, No Offset Group

Before LLVM lowering, `ExpandTiledMemRefsPass` flattens every tiled-layout memref index into a flat word offset. For `IndirectVectorStreamStartOp` this is `expandSCStreamStart<IndirectVectorStreamStartOp>` (`@0x134eb880`; the `…AddStartOp` instantiation is `@0x134ebd20`). The decompiled body clones the op, then runs **exactly three** index groups through `expandTiledIndices`:

```c
// expandSCStreamStart<IndirectVectorStreamStartOp>  @0x134eb880  (reconstructed)
started = cloneWithSubstOperands<IndirectVectorStreamStartOp>(op, ...);

// group 1 — Src  (ODS operand index 1, adaptor segment 2)
if (layoutIsTiled(getMemRefType(operand[1]))) {
    range  = getSrcIndicesMutable();
    seg    = adaptor.getODSOperandIndexAndLength(2);
    flat   = expandTiledIndices(seg, getTiles(layout), builder);  // 0x134e1f20
    range.assign(ValueRange(flat));                                // MutableOperandRange::assign
}
// group 2 — Dst  (ODS operand index 3, adaptor segment 4)
if (layoutIsTiled(getMemRefType(operand[3]))) {
    getDstIndicesMutable().assign(expandTiledIndices(adaptor.getODSOperandIndexAndLength(4), ...));
}
// group 3 — Sflag (ODS operand index 6, adaptor segment 7)
if (layoutIsTiled(getMemRefType(operand[6]))) {
    getSflagIndicesMutable().assign(expandTiledIndices(adaptor.getODSOperandIndexAndLength(7), ...));
}
rewriter.replaceOp(op, started);
return success();
```

Three groups, three `expandTiledIndices` + `MutableOperandRange::assign` calls — and **no fourth pass for an offset list**. Compare `expandSCStreamStart<IndirectStreamStartOp>` (`@0x134ead00`), which runs `{Src, Dst, Offset, Sflag}Indices` (four groups). The missing fourth pass is the index-expansion-level signature of the VREG form: the per-element offsets never enter the memref index machinery because they are a vector-register operand. `ExpandTiledMemRefsPass::addPattern<IndirectVectorStreamStartOp>` (`@0x134fc680`) registers this expansion.

> **NOTE — operand indices `1 / 3 / 6` are the memref groups; the VREG operands sit at the scalar segments.** The expansion only touches the *memref* segments (Src/Dst/Sflag, ODS operands 1/3/6). The VREG offset/length/mask operands are plain SSA `Value`s in other segments (5/8/9 — see the LLVM lowering below) and are not tiled-memref-expanded; they pass through untouched. A reimplementer must not route the VREG operands through the tiled-index expander.

---

## The Per-Index DMA Issue — `rewriteSparseCoreStreamOpToLLVM`

The final lowering, `IndirectVectorStreamStartOpLowering::rewriteSparseCoreStreamOpToLLVM` (`@0x13560ce0`), turns the op into the actual SparseCore stream-start intrinsic. This is where the VREG operands are read as values and the per-index issue intrinsic is chosen. The recovered flow:

```c
// rewriteSparseCoreStreamOpToLLVM  @0x13560ce0  (reconstructed)

// 1. Dereference the VREG operands as scalar SSA Values (NOT memref index groups):
v_off  = adaptor.getODSOperandIndexAndLength(5); offVal  = ValueRange(...).dereference_iterator(0);
v_len  = adaptor.getODSOperandIndexAndLength(8); lenVal  = ValueRange(...).dereference_iterator(0);
v_mask = adaptor.getODSOperandIndexAndLength(9); maskVal = ValueRange(...).dereference_iterator(0);
hbm4b  = op.getHbm4b();

// 2. Read src/dst element types + shapes from the Src (operand 1) and Dst (operand 3) memrefs
srcShape = getShape(getMemRefType(operand[1])); srcElem = getElementType(...);
dstShape = getShape(getMemRefType(operand[3])); dstElem = getElementType(...);

// 3. HBM_4B address adjustment (when hbm4b set): scale the Src/Dst offset operands
//    via getHbm4bOffset(...) + adjustOffsetForHbm4b(...) — adaptor operands 2 (src) / 4 (dst)
if (hbm4b) { srcOff = adjustOffsetForHbm4b(getHbm4bOffset(operand[2], srcElem)); ... }

// 4. Build NINE candidate issue lambdas (#3…#11), one per (hbm4b, srcSpace, dstSpace) route.
//    Each lambda closes over {offVal, lenVal, maskVal, base, sync, ...} and emits the
//    sc-stream-start intrinsic for that specific memory-space route.
cand[0..8] = { lambda#3, …, lambda#11 };   // each: {flag, srcSpace, dstSpace} + emitter

// 5. Select the lambda whose (flag,srcSpace,dstSpace) triple matches the runtime triple:
for k in 0..8:
    if cand[k].flag == hbm4b && cand[k].src == srcSpace && cand[k].dst == dstSpace:
        // optional: if a filter value operand (operand 0) is present, emit
        //   sc_tpu.set_indirect_filter_value first
        if (operand[0] present) builder.create<SetIndirectFilterValueOp>(filterVal);
        cand[k].emit();          // ISSUE the per-route stream-start
        return success();
// 6. No match → diagnostic:
return op.emitError(
    "IndirectVectorStreamStartOp doesn't support transfer from source memory space: %d"
    " to destination memory space: %d", srcSpace, dstSpace);
```

The nine candidate emitters are the per-route specializations of the SparseCore stream-start. The dispatch key is the `(hbm4b-flag, src-memory-space, dst-memory-space)` triple — the same `(srcSpace, dstSpace)` pair that [`GetTransferKind`](getsequencertype.md#layer-1-gettransferkind--stream-vs-dma) used upstream to classify this as a Stream rather than a DMA. So the kStream decision (made during `getSequencerType` lowering) is here turned into the concrete per-route intrinsic. An unsupported pair falls through to the `emitError`, which builds the message via the LLVM `SmallVector<u32>` interpolation seen in the decompile.

| Lowering element | Evidence (gfc) | Confidence |
|---|---|---|
| VREG offset operand at adaptor segment 5, dereferenced as a `Value` | `getODSOperandIndexAndLength(5)` + `dereference_iterator(0)` | CONFIRMED |
| VREG length operand at adaptor segment 8 | `getODSOperandIndexAndLength(8)` + `dereference_iterator(0)` | CONFIRMED |
| VREG mask operand at adaptor segment 9 | `getODSOperandIndexAndLength(9)` + `dereference_iterator(0)` | CONFIRMED |
| `getHbm4b()` gates HBM_4B offset scaling | `getHbm4b` @ `0x145d9120`; `getHbm4bOffset` / `adjustOffsetForHbm4b` calls | CONFIRMED |
| HBM_4B offset operands at adaptor segments 2 (src) / 4 (dst) | `getODSOperandIndexAndLength(2)` / `(4)` in the hbm4b branch | CONFIRMED |
| 9-way `(hbm4b, src, dst)` triple dispatch over emitter lambdas | nine `operator new(0x50/0x58)` emitter closures + match chain on `(flag, *(+1), *(+2))` | CONFIRMED |
| Optional `SetIndirectFilterValueOp` when operand 0 present | `OpBuilder::create<SetIndirectFilterValueOp>` on the matched branch | CONFIRMED |
| Unsupported-route diagnostic | `"IndirectVectorStreamStartOp doesn't support transfer from source memory space: … to destination memory space: …"` | CONFIRMED |
| Lane-parallel loop body / hardware per-lane address arithmetic | not a software loop — issued as one intrinsic; HW walks the VREG | HIGH |

> **GOTCHA — there is no software per-element loop; the "loop" is the hardware stream engine.** The lowering does **not** emit a `scf.for` over the offset lanes and a per-lane `imul`/DMA. It emits a single stream-start intrinsic that hands the VREG selector, the length selector, the lane mask, and the base to the hardware Stream engine, which then walks the lanes itself — applying the same per-element row-stride multiply (`addr_i = base + offsets[lane] * indirect_list_stride`) documented for [`IndirectStream`](stream-gather-scatter.md#the-per-element-address-formula). The compiler's job is operand assignment and route selection, not loop emission. A reimplementer modelling the lowering must emit one intrinsic per stream-start, not an unrolled loop.

---

## The Builder — `InitiateIndirectVectorStreamOperation`

The higher-level builder that constructs the op from a target + a set of `ValueRange` operands is `lowering_util::InitiateIndirectVectorStreamOperation` (`@0x13d870e0`; a sibling overload at `@0x13d866c0`). The recovered body threads the VREG operands as a `ValueRange` and validates the transfer geometry before building the op:

```c
// InitiateIndirectVectorStreamOperation(Target&, OpBuilder, LocationGenerator,
//     Value, ValueRange, ValueRange, …, StreamOptions)  @0x13d870e0  (reconstructed)
srcSpace = GetMemorySpace(srcMemref);
srcGran  = xla_mlo_util::TransferGranularityInBytes(target.chip_parts, srcSpace, ...);
dstSpace = GetMemorySpace(dstMemref);
dstGran  = xla_mlo_util::TransferGranularityInBytes(target.chip_parts, dstSpace, ...);

// transfer byte-count must be a multiple of each address space's granularity
if (bytes % srcGran) return InvalidArgument(
    "Bytes transferred by IndirectStreamOperation must be a multiple of "
    "src address space %d granularity %d. Got %d", srcSpace, srcGran, bytes);
if (bytes % dstGran) return InvalidArgument( /* … dest address space … */ );

if (!target.SupportsSparseCore()) LOG(FATAL) << "SparseCore is not supported by this target";

minGran = target.chip_parts->sc_min_granularity;          // chip_parts +164
if (bytes % minGran) return InvalidArgument(
    "Bytes transferred by IndirectStreamOperation must be a multiple of %d. Got %d", minGran, bytes);

// then build sc_tpu.indirect_vector_stream_start with the VREG operands as a ValueRange
…
```

Two facts worth preserving: (1) the builder validates that the per-stream byte count is a multiple of **both** endpoints' transfer granularity *and* the chip's SparseCore minimum granularity — the granularity model that bounds a single stream pass; (2) its diagnostic strings say `"IndirectStreamOperation"` (the generic name), shared with the SREG form — the builder is the common entry the VREG form reuses, parameterized by which operand carries the offsets.

> **NOTE — `SupportsSparseCore()` here is a hard `LOG(FATAL)`, distinct from `SupportsScVar`.** This builder asserts the target supports SparseCore at all (`target.h:1757`); it is *not* the `SupportsScVar` capability bit that [`GetTransferKind`](getsequencertype.md#layer-1-gettransferkind--stream-vs-dma) gates the kStream routes on. A reimplementer must keep the two target predicates separate: `SupportsSparseCore()` is a build-time invariant (true for every SC gen), `SupportsScVar` is the per-gen capability that is 0 in this wheel.

---

## Per-Generation Presence

| Mechanism | VF (vfc, Viperfish) | GL (glc, Ghostlite) | GF (gfc, 6acc60406) |
|---|:---:|:---:|:---:|
| `IndirectVregStream` form (opcode `0x38`) | yes | yes | yes |
| In the **TEC** Stream slot | yes | yes | yes |
| In the SCS Stream slot | **no** | **no** | **no** |
| In the TAC Stream slot | **no** | **no** | — (no TAC) |
| `IndirectOffsets` field accessor | `0x1e936240` | `0x1ea7be20` | `0x1ebe30a0` |
| TEC IndirectVreg field-accessor count | 21 | 22 | 26 |
| SCS / TAC IndirectVreg field-accessor count | 0 | 0 | 0 |
| `sc_tpu.indirect_vector_stream_start` op + 3 index groups | yes | yes | yes |

The discriminator is the field-accessor namespace prefix: every `IndirectVregStream` field accessor is under `SparseCoreTecStream…`; there are **zero** under `SparseCoreStream…` (SCS) or `SparseCoreTacStream…` (TAC) in any generation. The per-gen TEC accessor count (21/22/26) grows as more shared-tail fields gain explicit accessors, not because the form changes. No `SparseCoreStream` schema appears under the `jxc`/`pxc` namespaces (those generations have no SparseCore).

---

## Limits and Open Items

| Item | Status | Confidence |
|---|---|---|
| Proto delta `#1…#4` (VREG offsets + mask + base replace 2 SREG operands) | decoded from descriptor; shared `#5…#22` identical to `IndirectStream` | CONFIRMED |
| `IndirectOffsets` / `IndirectAccessLengths` slot selectors (6-bit, `+0x28>>27` / `+0x30>>2`) | accessor bodies read (`0x1ebe30a0` / `0x1ebe30c0`) | CONFIRMED |
| Opcode `0x38` @ bundle bit 181; two width-6 VREG selectors @ bits 283 / 322 | `case 0xB` of TEC encoder `@0x1ebe33e0`: `BitCopy(…,181,…,6)`, `(…,283,…,6)`, `(…,322,…,6)` | CONFIRMED |
| TEC-only (SCS bound `0xa`, no SCS/TAC field accessors) | encoder bounds + zero-accessor counts | CONFIRMED |
| Op: 3 index groups, no `OffsetIndices`, no `getIndirectListType` | getters located; offset/list-type getters absent | CONFIRMED |
| Index expansion runs 3 groups (Src/Dst/Sflag), no 4th | `expandSCStreamStart<IndirectVectorStreamStartOp>` `@0x134eb880` body | CONFIRMED |
| VREG operands at adaptor segments 5/8/9, dereferenced as `Value`s | `rewriteSparseCoreStreamOpToLLVM` `@0x13560ce0` body | CONFIRMED |
| 9-way `(hbm4b, src, dst)` per-route intrinsic dispatch + diagnostic | nine emitter closures + match chain; `emitError` string | CONFIRMED |
| Builder validates transfer byte-count vs src/dst/min granularity | `InitiateIndirectVectorStreamOperation` `@0x13d870e0` body | CONFIRMED |
| Per-lane HW address arithmetic (`base + offsets[lane] * list_stride`) | no software loop in lowering; HW intrinsic | HIGH |
| Minibatch / variable-length use case | inferred from `valid_offset_mask` + register-sourced ids | HIGH |
| Physical VREG-file size behind the 6-bit selector | selector width known; file size is chip_parts geometry, not enumerated | LOW |
| Mapping of the 9 emitter lambdas to specific `(src,dst)` memory-space pairs | dispatch structure confirmed; per-lambda space triples not exhaustively decoded | LOW |
| Absolute bit base of the TEC Stream slot in the 512-bit bundle | opcode @ 181 / `indirect_offsets` @ 322 known; full slot-base partition not mapped | LOW |

---

## Cross-References

- [Stream Gather/Scatter](stream-gather-scatter.md) — the `SparseCoreStream` proto, the shared control tail, the slot bit encoding, and the `IndirectStream` indirect-DMA descriptor this form mirrors; the per-element address formula.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-assignment policy (`GetTransferKind` kStream-vs-kDma) that pins this form to TEC and supplies the `(src,dst)` triple the per-index dispatch keys on.
- [VectorLoad Slot](vectorload-slot.md) — the TILE_SPMEM gather-load counterpart that consumes the streamed rows.
- [SparseCore Architecture](architecture.md) — engine roles and the embedding datapath in full depth.
- [SparseCore Overview](overview.md) — the host-table → HBM → SC gather path this form executes from a register.
- [Embedding Minibatching](embedding-minibatching.md) — the per-physical-core minibatch model the register-sourced id stream serves.
- [CBREG Circular-Buffer Register](cbreg.md) — the sliding-window register the shared-tail offset source can advance.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore pointers & DMA — [back to index](../index.md)
