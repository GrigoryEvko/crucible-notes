# Segmented Add-Scan

> *Every opcode value, `Matches()` compare immediate, field shift/width, per-gen capability predicate, and intrinsic name on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`) — from each `SparseCoreTecVectorExtendedSegmentedAddScan<dtype>Opcode::Matches()` immediate and field accessor, the `mlir::sparse_core::SegmentedScanOp::build` `addOperands` order, and the five `xla::jellyfish::<Gen>Target::Supports{VectorPack,VectorUnpack,DynamicUnpack}Ops` / `SupportsBf16AluInstructions` bodies. Addresses apply to this build; other versions differ.*

## Abstract

`SegmentedAddScan` is the SparseCore's embedding-row reducer: a per-segment inclusive prefix sum whose running accumulator **resets at each segment boundary**. It is the reduce stage of the DLRM embedding lookup, where one `TILE_SPMEM` tile holds gathered rows for several samples and each output row must be the sum of one sample's contiguous run. The segment boundary is the compressed-sparse-row (CSR) offset vector that `XlaSparseDenseMatmulWithCsrInput` carries; the scan resets the partial sum at every new sample. The forward sum-lookup is a `SegmentedAddScan`; the result drains via XRF → [VectorResult](vector-opcode-enum.md) and scatter-adds back through the [VectorStore](vectorstore-slot.md) `…Add{dt}` mux — the stage-3 reduce of the gather → load → **reduce** → drain → scatter pipeline.

The decisive architectural fact is that `SegmentedAddScan` is the **SparseCore successor to a retired TensorCore primitive**, and it carries the segment boundary differently. The old TensorCore path (Jellyfish/Pufferfish only) used a side control register: a `SetSegmentPattern` (`Vsetspr`, LLO `0x8c`) op programmed a per-lane boundary register that the `kVector{Max,Min,Add}SegmentReduceF32` opcodes (`0xfa`/`0xfb`/`0xfc`) respected. **The SparseCore has no `SetSegmentPattern` analog.** Instead the segment boundary is folded into an ordinary vector operand — operand 1 of the `SegmentedScanOp`, register-allocated to a V read port like any data input. There is no prologue pattern instruction and no per-XLU pattern cache; the segment id rides the data path as a vector, not a control register. The newer generations (Viperfish, Ghostlite) dropped the TensorCore reduce entirely (`SupportsSegmentedReduce` false) and moved embedding-segment-reduce onto the dedicated SparseCore [VectorExtended](vectorextended-vex.md) scan unit.

This page owns three things: the **`SegmentedAddScan` op family** (the six dtype variants, the op-invariant operand frame proving the segment id must be an existing V operand, and the negative finding that no SparseCore `SetSegmentPattern` exists); the **per-gen `VpackFormat` capability matrix** (which pack/unpack/dynamic-unpack formats each of the five TensorCore targets supports — the supply side that the bf16↔f32 widen and the embedding-quant dequant depend on); and the **HLO → dialect → intrinsic → ISA emit chain** that wires `XlaSparseDenseMatmulWithCsrInput` to the `SparseCoreTecVectorExtended_SegmentedAddScan{dtype}` opcode. The op-invariant VEX frame and the opcode roster live in [VectorExtended](vectorextended-vex.md); the scan datapath and mask consumption in [Scan Datapath](scan-datapath.md); the `reduction_op` × element-type lowering switch in [Segmented Scan](segmented-scan.md). They are linked, not repeated.

For reimplementation, the contract is:

- **`SegmentedAddScan` is six dtype variants of one VEX opcode form**, present in *both* SparseCore engines (`gxc::gfc` general-fetch-core and `gxc::glc` general-load-core): `F32`, `S32`, `S16PartialSum{S16,S32}`, `Bf16PartialSum{Bf16,F32}`. The opcode is a flat 6-bit selector at `word0x28` bits 16..21 (gfc); `SegmentedAddScanS32` = 10 (`Matches` cmp `0xa0000`), `SegmentedAddScanF32` = 15 (cmp `0xf0000`).
- **The operand frame is byte-identical to the plain `AddScan` twin.** Every field accessor (`SourceOne`, `Vmask`, `VstSource`, `V0/V1/V2` `Y_VREG`+`X`) has the same word/shift/mask as plain `AddScanF32`; only the 6-bit opcode changes (plain `AddScanF32` = 5, segmented = 15). The Segmented variant adds **no** field — so the segment boundary must be one of the existing V operands.
- **The segment boundary is operand 1 of `SegmentedScanOp`, not a control register.** `build` adds two `Value` operands in order — `(data, segment-id)` — plus the `reduction_op` `StringAttr` (one of `"sum"`/`"min"`/`"max"` — the add path's attribute string is literally `"sum"`, not `"add"`). The segment id is allocated to a V read port (INFERRED V1 by build order). There is no SparseCore `SetSegmentPattern`.
- **Only `add` has a PartialSum-widening variant** (`tpu_add_half_seg_scan2xN`); `min`/`max` need no wider accumulator. The PartialSum variants (`Bf16→F32`, `S16→S32`) are the embedding-row sum primitive: many narrow rows accumulated into one wide partial without precision loss.
- **`VpackFormat` capability grows monotonically JF ⊂ PF ⊂ VF ⊂ GL.** A reimplementer gates every pack/unpack against the target's `Supports*Ops` predicate; the set of supported formats per generation is the byte-exact bitmask in [§The VpackFormat Capability Matrix](#the-vpackformat-capability-matrix).

| | |
|---|---|
| **ISA op** | `SparseCoreTecVectorExtended_SegmentedAddScan{F32,S32,S16PartialSum{S16,S32},Bf16PartialSum{Bf16,F32}}` |
| **Slot** | `VectorExtended` (VEX) slot of the 64-byte [TEC bundle](tec-engine.md#the-tec-bundle-64-bytes) |
| **Opcode field** | 6-bit @ `word0x28` bits 16..21 (gfc); `S32`=10 (`0xa0000`), `F32`=15 (`0xf0000`) |
| **Engines** | both `gxc::gfc` (fetch-core) and `gxc::glc` (load-core) — symbol-confirmed |
| **Dialect op** | `mlir::sparse_core::SegmentedScanOp::build` (`0x145fd4a0`) — 2 operands `(data, segment-id)` + `reduction_op` |
| **Lowering** | `SegmentedScanOpLowering::matchAndRewrite` (`0x13589d40`) — see [Segmented Scan](segmented-scan.md) |
| **HLO source** | `XlaSparseDenseMatmul[Grad]WithCsrInput` (`0xe650800` / `0xe65e920`) — CSR offsets = segment boundaries |
| **Proto oneof** | `segmented_add_scan_f32` (inst case `0x23`/35; `mutable_segmented_add_scan_f32` `0x13aaf600`) |
| **TC predecessor** | `kVector{Max,Min,Add}SegmentReduceF32` `0xfa`/`0xfb`/`0xfc` + `SetSegmentPattern` `0x8c` — JF/PF only, retired on VF/GL |
| **Confidence** | CONFIRMED (decompile / `Matches`-immediate & accessor-shift anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the `SegmentedAddScan` op family, the per-gen `VpackFormat` caps, and the pack/unpack supersession story.** The op-invariant VEX operand frame and the full opcode roster live in [VectorExtended](vectorextended-vex.md); the masked-scan datapath in [Scan Datapath](scan-datapath.md); the `reduction_op` × element-type lowering switch in [Segmented Scan](segmented-scan.md). They are linked, not repeated.

---

## The SegmentedAddScan Op Family

### Purpose

`SegmentedAddScan` computes a per-segment inclusive prefix scan: within each segment the running accumulator sums the lanes; at a segment boundary the accumulator resets to the reduction identity (0 for add). This is exactly the embedding-bag aggregation when one VREG holds rows for several samples — each sample is one segment, and the per-sample sum is the segment's final scan value. It is the SparseCore analog of a `tf.math.segment_sum`, executed in one VEX bundle slot rather than a software loop.

### The six dtype variants

The op is one VEX form parameterized by six accumulation dtypes, all present in both `gxc::gfc` and `gxc::glc`. The `PartialSum` suffix names the *output* (accumulator) width versus the *input* element width: a narrow input is accumulated into a wider partial so a long embedding run does not overflow or lose precision.

| Variant | Input → accumulator | Role | Confidence |
|---|---|---|---|
| `SegmentedAddScanF32` | f32 → f32 | float embedding sum (op 15) | CONFIRMED |
| `SegmentedAddScanS32` | s32 → s32 | integer embedding sum (op 10) | CONFIRMED |
| `SegmentedAddScanS16PartialSumS16` | s16 → s16 | narrow-int sum, no widen | CONFIRMED |
| `SegmentedAddScanS16PartialSumS32` | s16 → s32 | int8/int16 row sum widened to s32 | CONFIRMED |
| `SegmentedAddScanBf16PartialSumBf16` | bf16 → bf16 | bf16 sum kept in bf16 | CONFIRMED |
| `SegmentedAddScanBf16PartialSumF32` | bf16 → f32 | **the bf16 embedding-row sum** (many rows → f32 partial) | CONFIRMED |

Each variant is a distinct C++ type carrying the full accessor set — `…<dtype>Opcode::Matches`, `…<dtype>SourceOneField`, `…V0/V1/V2{X,YVreg}Field`, `…VmaskField`, `…VstSourceField` — confirmed present for `Bf16PartialSumBf16`, `Bf16PartialSumF32`, `S16PartialSumS16`, `S16PartialSumS32`, `F32`, and `S32` in the decompile. The six are the `SegmentedAddScan` rows of the 12-op `Segmented*` family (the segmented twins of the plain scans: `Add`, `Min`, `Max`, `MinIndex`, `MaxIndex` × {32-bit ops 10..19, 16-bit/bf16 ops 40..51}); the full roster lives in [VectorExtended](vectorextended-vex.md#the-embedding-reduce-op-roster).

### The op-invariant operand frame

The decisive structural finding: `SegmentedAddScanF32`'s field bit-layout is **byte-identical** to plain `AddScanF32`. Only the 6-bit opcode differs. The op *is* the function; the operands are positionally fixed.

```text
SegmentedAddScanF32 operand frame (gfc)  —  byte-identical to plain AddScanF32
 field        word       shift  width  accessor (Seg gfc)   role
 ───────────  ─────────  ─────  ─────  ───────────────────  ─────────────────────────────────
 Opcode       0x28       16     6      Matches & 0x3f0000   plain AddScanF32=5, Segmented=15
 SourceOne    0x28       13     3      0x1eca9aa0           scan seed-port / carry-in selector
 Vmask        0x28       5      5      0x1eca9ba0           lane predicate mask
 VstSource    0x30       27     6      0x1eca9ac0           fused store-source (== VectorStore Source)
 V0YVreg      0x40/0x38  shld4  6      0x1eca9ae0           operand-0 VREG (data)
 V0X          0x40       8      6      0x1eca9b00           operand-0 selector
 V1YVreg      0x38       23     6      0x1eca9b20           operand-1 VREG (segment-id, INFERRED)
 V1X          0x38       35     6      0x1eca9b40           operand-1 selector
 V2YVreg      0x30       50     6      0x1eca9b60           operand-2 VREG
 V2X          0x38/0x30  shld2  6      0x1eca9b80           operand-2 selector
```

The `Matches()` predicate isolates the opcode field and compares it to the op's signature. Byte-exact from the decompiled bodies:

```c
// SparseCoreTecVectorExtendedSegmentedAddScanF32Opcode::Matches  (gfc)
return (*((uint32_t *)this + 10) & 0x3F0000) == 0xF0000;   // word0x28 bits16..21 == 15
// SegmentedAddScanS32Opcode::Matches
return (*((uint32_t *)this + 10) & 0x3F0000) == 0xA0000;   // == 10
// plain AddScanF32Opcode::Matches (for contrast)
return (*((uint32_t *)this + 10) & 0x3F0000) == 0x50000;   // == 5
```

> **QUIRK — the Segmented variant adds no field, so the segment boundary cannot be a new operand.** A reimplementer who expects a segment-mask accessor (the obvious analog of a "boundary register") will not find one. The frame is identical to the plain scan; the segment id therefore *must* be one of the existing three V operand pairs. The dialect build (next section) pins it to operand 1. The opcode-keyed extraction rule — same bit region, op selects the interpretation — is the same one [VectorExtended](vectorextended-vex.md#the-slot-field-map) uses for `SourceTwo`/`VexDest`.

> **NOTE — the glc encoding places the opcode at a different bit position.** Some `Matches` bodies test `& 0x1F8000` rather than `& 0x3F0000` — the glc (load-core) VEX field sits at `word0x28` bit 15 (not 16) and is masked one bit narrower. The opcode *value* is recovered the same way (`cmp >> 15` for glc); the per-gen field delta is owned by [VectorExtended](vectorextended-vex.md#the-per-gen-delta).

---

## The Segment Boundary: No SetSegmentPattern

### Purpose

The single most important difference from the retired TensorCore reduce. On the TensorCore the segment boundary lived in a side XLU control register; on the SparseCore it is an ordinary vector operand. A reimplementer porting the TensorCore datapath will look for the boundary-register prologue op and must not emit one.

### The negative finding

`EmitVectorSetSegmentPattern` exists in the binary, but **only in the seven TensorCore / BarnaCore emitters** — `JellyfishEmitter` (`0x140b58e0`), `PufferfishTensorCoreEmitter` (`0x1411bfa0`), `PufferfishBarnaCoreChannel` (`0x140cf020`), `PufferfishBarnaCoreSequencer` (`0x140e4680`), `BarnaCoreAddressHandler` (`0x141654a0`), `ViperfishTensorCoreEmitter` (`0x141dd280`), and `GhostliteTensorCoreEmitter` (`0x1429ff20`, which `LogFatal`s "not supported"). There is **no** `gxc`/`gfc`/`glc` symbol. The LLO factory `LloInstruction::CreateVectorSetSegmentPattern` (`0x1d4d64a0`) is a pure-TensorCore op (LLO `0x8c`, one operand, no mode field). The SparseCore never builds it.

### The boundary as a build operand

```c
// mlir::sparse_core::SegmentedScanOp::build(OpBuilder&, OperationState&,    (0x145fd4a0)
//                                           Type result, Value data, Value segment, StringAttr reduction_op)
function SegmentedScanOp_build(state, result_ty, data, segment, reduction_op):
    addTypes(state, result_ty)
    addOperands(state, &data,    1)      // operand 0 = DATA              (0x145fd4ca)
    addOperands(state, &segment, 1)      // operand 1 = SEGMENT-ID        (0x145fd4db)
    setProperty(state, reduction_op)     // "add" / "min" / "max" StringAttr

// plain twin: mlir::sparse_core::ScanOp::build  (0x145f9480) — (data, 2nd operand) instead
```

The `reduction_op` `StringAttr` is validated by the `sc_ops` attribute constraint (`0x145f8f80`) — a length-3 compare against `"add"`/`"min"`/`"max"`. At ISA emit, the read-port allocator (`FindAndEmitToUnusedPort`, gfc `0x13ab2aa0` / glc `0x13a4b680`) wires up to six read ports, writing `V0`@`struct+0x1c`, `V1`@`+0x24`, `V2`@`+0x28`, `Vmask`@`+0x2c`. `EmitVectorResultUnop<…SegmentedAddScanF32>` (gfc `0x13aaf560`) reads `operand[0x10]` → `Vmask` and `operand[0x20]` → the primary data Vregno; the remaining V ports carry the segment id.

> **GOTCHA — which V port the segment id occupies is INFERRED, not pinned.** `build` fixes the *SSA operand order* (operand 0 = data, operand 1 = segment), and the read-port allocator wires `V0`/`V1`/`V2`/`Vmask` structurally — but the exact operand-index → V-port-field binding was read from the allocator's structure, not from a single operand-index constant. The segment id landing in **V1** is the natural reading of "second operand," but a reimplementer must confirm the allocator's port-assignment policy rather than hard-code V1. The 6-read-port budget (`cmp 6`) is byte-confirmed.

---

## The HLO → Dialect → Intrinsic → ISA Emit Chain

### Purpose

The forward embedding sum-lookup and its gradient both lower through this chain. The CSR (compressed-sparse-row) row offsets that the DLRM op carries become the per-lane segment-id vector that the scan resets on.

### The chain

```text
1. HLO       XlaSparseDenseMatmulWithCsrInputOp::Compile        @0xe650800   (forward lookup)
             XlaSparseDenseMatmulGradWithCsrInputBase::Compile  @0xe65e920   (gradient)
             CSR input supplies per-sample row offsets = SEGMENT BOUNDARIES
             GetMaxIdsAndUniques @0xe651fa0  bounds the gather/dedup window
                  │
2. SC HLO    EmbeddingDataFormattingDecomposer  @0x1095b6a0
   pass      builds the dialect SegmentedScanOp (reduction_op ∈ {add,min,max},
             operand-0 = data, operand-1 = segment-id)
                  │
3. dialect→  SegmentedScanOpLowering::matchAndRewrite  @0x13589d40           (see Segmented Scan)
   intrinsic  switch reduction_op × elementType {F32, I32, I16, BF16}
             → emit the matching tpu_*_seg_scan* intrinsic
                  │
4. ISA       SparseCoreTecVectorExtended_SegmentedAddScan{dtype}
             proto `inst` oneof case  (segmented_add_scan_f32 = 0x23)
```

The lowering reads `getReductionOp()` (the 3-char scan kind) and the result `VectorType`'s element type (via `Builder::getF32Type` / `getI32Type` / `getI16Type` / `getBF16Type`), builds an `i1` `VectorType` (the per-lane segment-boundary mask) and an `LLVMStructType` literal (the `{value, segment-id}` reduce pair), then creates the matching intrinsic and extracts the value back out with `LLVM::ExtractValueOp`. The full dispatch table is owned by [Segmented Scan](segmented-scan.md); the six segmented intrinsics it emits are:

| reduction | elem-type | segmented intrinsic | ISA dtype | Confidence |
|---|---|---|---|---|
| add | f32 | `tpu_add_seg_scan1xNf` (`0x146d5a80`) | `SegmentedAddScanF32` | CONFIRMED |
| add | i32 | `tpu_add_seg_scan1xNi` (`0x146d5c40`) | `SegmentedAddScanS32` | CONFIRMED |
| add | bf16 | `tpu_add_half_seg_scan2xN` (`0x146d45c0`) | `SegmentedAddScanBf16PartialSum*` | CONFIRMED |
| max | f32 | `tpu_max_seg_scan1xNf` (`0x14730e00`) | `SegmentedMaxScanF32` | CONFIRMED |
| max | i32 | `tpu_max_seg_scan1xNi` (`0x14730fc0`) | `SegmentedMaxScanU32` | CONFIRMED |
| min | f32 | `tpu_min_seg_scan1xNf` (`0x147316c0`) | `SegmentedMinScanF32` | CONFIRMED |
| min | i32 | `tpu_min_seg_scan1xNi` (`0x14731880`) | `SegmentedMinScanU32` | CONFIRMED |

The `1xNf`/`1xNi`/`2xN` suffix is the lane packing: `1xNf` = one f32 per lane, `1xNi` = one int32, `2xN` = two bf16 per lane (the packed pair). `half` is the PartialSum widen (bf16/s16 accumulated into f32/s32). Only `add` has the `half` widen — `min`/`max` need no wider accumulator.

> **GOTCHA — the `tpu_max_seg_scan2xN` / `tpu_min_seg_scan2xN` op *types* exist, but the lowering never emits them.** The decompile shows full op-definition symbol sets for `tpu_max_seg_scan2xN` and `tpu_min_seg_scan2xN` (and the ISA carries `SegmentedMin/MaxScanBf16` ops 48/49). But `SegmentedScanOpLowering` only creates the `1xN` form for `min`/`max` segmented scans — the packed-bf16-pair segmented min/max is *defined* in the dialect for completeness yet *unreachable* from this lowering. A reimplementer driving off the op-definition list will allocate handlers the lowering never invokes. Only `add` has a reachable segmented `2xN` path (`tpu_add_half_seg_scan2xN`).

---

## The VpackFormat Capability Matrix

### Purpose

The `SegmentedAddScan` PartialSum variants and the EUP's bf16↔f32 widen both depend on the XLU/EUP being able to pack and unpack the relevant lane format. That capability is **per-generation** and is the supply side this page documents: which of the 26 `VpackFormat` enum values each of the five TensorCore targets can pack (lanes → narrow slot), unpack (narrow slot → lanes), dynamically unpack, and compute on natively. The bf16 pack/unpack arithmetic itself (`kVectorPack` `0x126` / `kVectorUnpack` `0x109`, the `<<16` / `&0xffff0000` widen) is owned by the LLO XLU pages; this section is the capability gate.

### The four capability methods

Each generation's `Target` exposes four predicates. The first three live in a `+0x10` subobject vtable (slots `+0x4c8`/`+0x4d8`/`+0x4e0`); the fourth is the EUP lane sub-element-width selector in the primary vtable.

- `SupportsVectorPackOps(VpackFormat)` — which formats the XLU/EUP can **pack**.
- `SupportsVectorUnpackOps(VpackFormat, TpuCoreType)` — which it can **unpack**. The `TpuCoreType` arg is ignored on VF/GL (core-type-invariant — the decompiled VF/GL signatures take only the format).
- `SupportsDynamicUnpackOps(VpackFormat)` — the `0x10f` `kVectorDynamicUnpack` gate.
- `SupportsBf16AluInstructions()` — the EUP AluEp lane width: `false` → 32-bit/F32 lane (packed bf16 must be unpacked to F32, computed, re-packed); `true` → 16-bit/BF16 lane (native per-lane bf16 op, no unpack).

### Per-gen predicates (byte-exact)

```c
// JellyfishTarget                                     (Dragonfish ≡ Jellyfish: vtable slots identical, no override)
SupportsVectorPackOps(fmt)        = (fmt == 7);                       // 0x1d4907c0  → {7}
SupportsVectorUnpackOps(fmt,_)    = false;                            // 0x1d490840  → {}
SupportsDynamicUnpackOps(fmt)     = false;                            // 0x1d490860  → {}
SupportsBf16AluInstructions()     = false;                           // 0x1d4916e0  (32-bit F32 lane)

// PufferfishTarget
SupportsVectorPackOps(fmt)        = (fmt == 7 || fmt == 1);           // 0x1d494e00  → {1,7}
SupportsVectorUnpackOps(fmt,_)    = (fmt == 1);                       // 0x1d494e20  → {1}
SupportsDynamicUnpackOps(fmt)     = false;                            // 0x1d494e40  → {}
SupportsBf16AluInstructions()     = false;                           // 0x1d495c20  (32-bit F32 lane)

// ViperfishTarget
SupportsVectorPackOps(fmt)        = ((uint16_t)(fmt - 1) < 0xA);      // 0x1d49b1a0  → {1..10}
SupportsVectorUnpackOps(fmt,_)    = (fmt < 0xE) & (0x39FE >> fmt);    // 0x1d49b1c0  → {1..8,11,12,13}
SupportsDynamicUnpackOps(fmt)     = false;                            // 0x1d49b1e0  → {}
SupportsBf16AluInstructions()     = false;                           // 0x1d49c0e0  (32-bit F32 lane)

// GhostliteTarget
SupportsVectorPackOps(fmt)        = (fmt < 0x17) & (0x7807FE >> fmt); // 0x1d498020  → {1..10,19..22}
SupportsVectorUnpackOps(fmt,_)    = (fmt < 0x17) & (0x7839FE >> fmt); // 0x1d498040  → {1..8,11..13,19..22}
SupportsDynamicUnpackOps(fmt)     = (fmt < 8)    & (0x8E   >> fmt);   // 0x1d498060  → {1,2,3,7}
SupportsBf16AluInstructions()     = true;                            // 0x1d498ce0  (16-bit BF16 lane, native)
```

The `Base` `Target` `Pack`/`Unpack`/`Dyn` slots (`0x1d61e200`/`40`/`80`) are pure-virtual `LogFatal` — every concrete generation must override.

> **NOTE — Dragonfish ≡ Jellyfish for pack/unpack.** `DragonfishTarget` has no own `Supports{VectorPack,VectorUnpack,DynamicUnpack}Ops`. Its `+0x10` subobject vtable slots `+0x4c8`/`+0x4d8`/`+0x4e0` resolve (via `R_X86_64_RELATIVE`) to the *exact same* addresses (`0x1d4907c0`/`0x1d490840`/`0x1d490860`) as `JellyfishTarget`. `SupportsBf16AluInstructions` is also `false` (no override). Treat DF as JF for all four capabilities.

### The format-support cross-grid

The `VpackFormat` enum is 26 entries (`0` invalid sentinel). The support sets grow monotonically: JF can only *pack* interleaved bf16; PF adds the minimum bf16↔f32 round-trip; VF opens the full sub-byte int pack and the f16/f8 unpack; GL adds the U8/S8/U4/S4→bf16 embedding-quant dequant, is the only gen with dynamic unpack, and the only one with a native bf16 ALU lane.

| fmt | name | JF/DF | PF | VF | GL |
|---:|---|---|---|---|---|
| 1 | CompressedBf16 | — | P U | P U | P U **D** |
| 2 | CompressedB16 | — | — | P U | P U **D** |
| 3 | CompressedB8 | — | — | P U | P U **D** |
| 4 | CompressedB4 | — | — | P U | P U |
| 5 | CompressedB2 | — | — | P U | P U |
| 6 | CompressedB1 | — | — | P U | P U |
| 7 | InterleavedBf16 | **P** | P | P U | P U **D** |
| 8 | InterleavedB16 | — | — | P U | P U |
| 9 | InterleavedB8 | — | — | P | P |
| 10 | InterleavedB4 | — | — | P | P |
| 11 | CompressedHf16 (f16) | — | — | U | U |
| 12 | CompressedF8E4M3B11 | — | — | U | U |
| 13 | CompressedF8E5M2 (bf8) | — | — | U | U |
| 14–18 | f8e4m3fn / EXMY→bf16 | — | — | — | — |
| 19 | CompressedU8ToBf16 | — | — | — | P U |
| 20 | CompressedS8ToBf16 | — | — | — | P U |
| 21 | CompressedU4ToBf16 | — | — | — | P U |
| 22 | CompressedS4ToBf16 | — | — | — | P U |
| 23–25 | Join / Interleaved-f8 | — | — | — | — |

(P = pack supported, U = unpack, **D** = dynamic-unpack. The f8e4m3fn / EXMY→bf16 (14..18) and Join/Interleaved-f8 (23..25) formats are not enabled on any of these five targets.)

> **QUIRK — `SupportsBf16AluInstructions` is the reason Ghostlite processes packed bf16 at half the op count.** On JF/DF/PF/VF (`false`), a packed bf16 vector entering the EUP AluEp must be *unpacked to a 32-bit F32 lane*, computed in F32, and re-packed — multiple LLO `kVectorUnpack`/`kVectorPack` steps per element. On GL (`true`), the BF16 ALU runs the per-lane bf16 op natively (the 1:1 EUP-macro path, no unpack). A `SegmentedAddScanBf16PartialSumF32` on GL therefore needs no widen prologue; on VF the same op unpacks to F32 first.

### VpackFormat fan-in

The lane fan-in (how many sub-lanes pack into one slot) is the `VpackFormatSublanesIndices` table (`0xb53c790`, 26 `int32`); `fmt0 = -1` is the invalid sentinel. The 4-fan-in formats are the sub-byte / f8 / u4/s4 dequant packs (`{3,9,12,13,14,15,21,22}`); the rest are 2.

```text
 fmt:   0   1   2   3   4   5   6   7   8   9  10  11  12  13  14  15  16  17  18  19  20  21  22  23  24  25
 fan:  -1   2   2   4   2   2   2   2   2   4   2   2   4   4   4   4   2   2   2   2   2   4   4   2   2   2
```

---

## The TensorCore → SparseCore Supersession

### Purpose

`SegmentedAddScan` did not arrive on a blank slate — it replaced a TensorCore XLU primitive. Understanding the delta both fixes the architecture and tells a reimplementer which path a given generation actually uses for embedding-segment-reduce.

### The retired TensorCore path

On Jellyfish and Pufferfish, the per-segment cross-lane reduce was three F32-only XLU opcodes — `kVectorMaxSegmentReduceF32` (`0xfa`), `kVectorMinSegmentReduceF32` (`0xfb`), `kVectorAddSegmentReduceF32` (`0xfc`) — gated by `LloOpcodeIsSegmentedReduction` (`0x1d60c340`, `(op - 0xfa) < 3`). The boundary came from a side register: `SetSegmentPattern` (`Vsetspr`, LLO `0x8c`) programmed a per-lane segment-id register the reduce respected, and a pair of fused reduces *shared one* `Vsetspr` setup (cached per-XLU). There was no bf16 segment reduce — the TensorCore path was F32-only.

The per-gen support gate:

| Target | `SupportsSegmentedReduce` | `SupportsSegmentedReducePartialResults` | TC emitter behaviour | Confidence |
|---|---|---|---|---|
| Jellyfish | true (`0x1d4909c0`) | true (`0x1d490a00`) | emits VEX `0xe`/`0x1e`/`0x1f`/`0x20` | CONFIRMED |
| Pufferfish | true (`0x1d494f80`) | false (`0x1d494fc0`) | emits (no partial-result drain) | CONFIRMED |
| Viperfish | false (`0x1d49b380`) | false (`0x1d49b3c0`) | uses SparseCore VectorExtended instead | CONFIRMED |
| Ghostlite | false (`0x1d4981c0`) | false (`0x1d498200`) | `LogFatal` "Operation not supported on Ghostlite" | CONFIRMED |

### The architectural delta

```text
TENSORCORE (JF/PF)                         SPARSECORE (VF/GL/gfc/glc — this page)
 ─────────────────                          ─────────────────────────────────────
 SetSegmentPattern (0x8c, Vsetspr)          NO SetSegmentPattern op exists
   → side control register (per-XLU)        segment id = operand-1 of SegmentedScanOp
 kVectorAddSegmentReduceF32 (0xfc)          SparseCoreTecVectorExtended_SegmentedAddScan{dtype}
   F32-only, 3 opcodes                        6 dtypes incl. bf16/s16 PartialSum widen
 two reduces share one Vsetspr (cached)     boundary rides the data path as a vector
 retired on VF/GL (SupportsSegmentedReduce  the embedding-segment-reduce engine on VF/GL
   false)
```

The newer generations moved the embedding segment-reduce off the TensorCore XLU — with its side pattern register and shared-pattern fusion — onto the SparseCore's dedicated VectorExtended scan unit, folding the segment boundary into a normal vector operand. The TensorCore reduce is now a JF/PF legacy primitive.

> **NOTE — the TensorCore reduce is documented from the TC LLO/emit side, not here.** This page documents the *SparseCore successor*. The TensorCore `0xfa`/`0xfb`/`0xfc` reduce, the `Vsetspr` `SetSegmentPattern`, the Reemit shared-pattern fusion, and the per-gen VEX-opcode map belong to the LLO XLU pages; only the supersession boundary (which gen uses which path) is owned here.

---

## Worked Example — a bf16 multi-sample embedding lookup-sum

A minibatch holds several samples; each sample looks up a contiguous run of bf16 embedding rows gathered into one `TILE_SPMEM` tile. The forward output row is the per-sample sum of that run.

```text
SparseCore (VF/GL, this page) — NO SetSegmentPattern:
  data       = bf16 embedding rows (gathered, one tile)
  segment_id = CSR-offset vector (per-sample boundaries from XlaSparseDenseMatmulWithCsrInput)
  mlir::sparse_core::SegmentedScanOp(data, segment_id, reduction_op="add")
    → tpu_add_half_seg_scan2xN
    → SparseCoreTecVectorExtended_SegmentedAddScanBf16PartialSumF32  (op 47)
  one ISA op: per-segment prefix sum of the bf16 rows into an f32 partial (the PartialSum widen),
  the segment_id riding the V1 operand as a normal vector (INFERRED).
  GL: SupportsBf16AluInstructions=true  → native bf16 lane, no unpack.
  VF: SupportsBf16AluInstructions=false → EUP AluEp unpacks to F32 first.
  result drains via XRF → VectorResult, scatter-adds via VectorStore TileSpmemStoreAddBf16.

TensorCore equivalent (JF/PF, the retired path):
  SetSegmentPattern (0x8c, Vsetspr) programs the per-lane boundary register;
  kVectorAddSegmentReduceF32 (0xfc) sums each segment (accumulator resets at boundaries),
  F32-only — the bf16 rows widened by VunpackF32 (<<16 / &0xffff0000) first.
```

The SparseCore folds the boundary into the data path and keeps the bf16 partial sum in one op; the TensorCore needed a separate boundary-register prologue and a pre-widen to F32.

---

## What Is Not Pinned

- **The exact V-port index of the segment id.** `build` fixes operand order (0 = data, 1 = segment-id) and the read-port allocator wires `V0`@`+0x1c`/`V1`@`+0x24`/`V2`@`+0x28`/`Vmask`@`+0x2c` (6 ports); the operand-index → V-port binding was read structurally, not pinned to a constant. INFERRED segment-id = V1. (INFERRED)
- **The `SourceOne` 3-bit scan-seed enum** (the reduction identity / carry-in: 0 for add, ±inf for min/max, or a prev-accumulator for multi-tile chaining). Same field as the plain scan; its 8 values were not enumerated. (LOW)
- **The DLRM front-end provenance** — how `XlaSparseDenseMatmulWithCsrInput`'s CSR row-offsets materialise into the segment-id *vector* operand. The HLO → `SegmentedScanOp` link is confirmed; the decomposer that turns offsets into per-lane segment ids was not traced end-to-end. (Chain HIGH; the materialiser untraced.)
- **The precise primary-vtable offset of `SupportsBf16AluInstructions`.** The per-gen *return values* (JF/DF/PF/VF false, GL true) are byte-exact from the method bodies, but a neighbouring vtable slot returns a different value, so the exact slot index is open. (Return values CONFIRMED; slot index LOW.)
- **Whether the f8e4m3fn / EXMY→bf16 (14..18) and Join/Interleaved-f8 (23..25) formats are enabled on a later TC target** not present as a distinct vtable here. VF/GL never enable them. (CONFIRMED for VF/GL; later targets untraced.)

---

## Cross-References

- [VectorExtended (VEX)](vectorextended-vex.md) — the op-invariant VEX operand frame and the full 0..52 opcode roster the `SegmentedAddScan` family lives in
- [Scan Datapath](scan-datapath.md) — the masked-scan datapath, mask consumption, and the `ScanOp` lowering this segmented variant parallels
- [Segmented Scan](segmented-scan.md) — the `SegmentedScanOpLowering` `reduction_op` × element-type dispatch switch that selects the intrinsic
- [VectorStore](vectorstore-slot.md) — the `VstSource` fused-store mux the scan result drains into (`…Add{dt}` scatter-add)
- [Dedup / Multiplicity](dedup-multiplicity.md) — the gather-side dedup/uniquify that bounds the per-sample run before the segment-reduce
- [SparseCore Overview](overview.md) — the gather → load → reduce → drain → scatter embedding pipeline this op is the reduce stage of
