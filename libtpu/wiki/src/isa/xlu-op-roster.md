# XLU Op Roster

> *Every opcode, address, offset, bit position, and immediate on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — full C++ symbols, `nm -C` resolves every method). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA − 0x200000 = file offset. Other libtpu builds will differ.*

## Abstract

The **XLU** (Cross-Lane Unit) is the TensorCore engine that moves data *across* lanes — the one place in the otherwise per-lane vector fabric where lane *i* can read lane *j*. It is the hardware behind transpose, arbitrary lane permute, lane rotate, and cross-lane reductions (sum/max/min and their argmax/argmin index variants, plain and segmented). All of these share one bundle slot — the `VectorExtended` (VEX) slot also used by the [MXU](slot-mxu.md) and the [EUP transcendentals](slot-eup-transcendental.md) — so the compiler must pack them, balance them across the per-generation XLU count, and price their latency before it can lay them into the bundle.

This page is the authoritative XLU reference. It has three parts, each anchored to the binary:

1. **The op roster.** Two views of the same hardware. At the IR level the back end emits high-level `LloOpcode`s through the `LloRegionBuilder` cross-lane factory set (`Vsetperm`/`Vxpose`/`Vpermute`/`Vrotate`/`Vsetspr`/the reduce family/…); each factory is a thin wrapper that calls one `LloInstruction::CreateVector*` op-constructor, which calls `LloInstruction::New(LloOpcode, operand-Span, …)` with a fixed opcode immediate. At the wire level the per-generation encoder packs those ops into the bundle's VEX slot as a Jellyfish (v2) `VectorExtendedOpcode` — a dense 35-value protobuf enum `{0..34}` whose upper range `{13..34}` *is* the XLU/transpose/permute/cross-lane family. The roster tables both numbering spaces and the bridge between them.

2. **The combining pipeline.** `LloXluGraphOptimizer::Optimize` runs a five-stage XLU op-graph rewrite — `ComputeCombinablePairs` (fuse adjacent identical XLU ops) → `AssignXlu` (greedy least-loaded XLU-unit balance) → `ReorderToShortenCriticalPath` (latency-weighted list scheduler) → `ReemitReorderedCombinedXluOperations` (emit fused ops, share the permute/segment-pattern prologue) → `AssignSourceBus` (VEX source-bus pack). Every placement, order, and fuse decision keys on one cost function.

3. **The cost.** `CyclesAddedByXluOperation` — the single marginal-latency expression all five stages consume — and the `PreXluAssignmentLatencyTable` edge model (`ceil(base / xlu_count)` for XLU↔XLU edges). Plus the transpose-fusion slot-fit geometry: the `SupportsVectorXpose` / divisibility / `NumVexSlots` three-gate predicate that decides whether two transpose tiles can collapse into one VEX-slot-bounded fused transpose.

For reimplementation, the contract is:

- The factory → `CreateVector*` → `New(LloOpcode, operand-count)` table, and the JF `VectorExtendedOpcode {0..34}` roster the bundle encoder packs into.
- The `ProtoUtils::Is*` classifier ranges (proto-enum numbering) that gate matmul / push-gains / transpose / RPU dispatch.
- The combining pipeline order and the fusion predicate (equal metadata + tracker-ready + cost-bounded; control ops never fuse).
- The `CyclesAddedByXluOperation` closed form and the `ceil(base/xlu_count)` XLU edge weight.
- The transpose slot-fit predicate and the `VxposeMode`/`ElementCount` geometry.

| | |
|---|---|
| **Optimizer** | `xla::jellyfish::LloXluGraphOptimizer::Optimize` @ `0x126cdb80` |
| **Factory set** | `LloRegionBuilder::V{setperm,setspr,permute,permuteres,rotate,xpose,xposeres,packBf16,…}` |
| **Op-constructor** | `LloInstruction::CreateVector*` → `LloInstruction::New(LloOpcode, Span<LloValue*>, …)` @ `0x1d4cf560` |
| **Wire enum** | `platforms_deepsea::jellyfish::isa::VectorExtendedOpcode` — dense `{0..34}` (descriptor @ `0x1fa1fd00`) |
| **Classifiers** | `ProtoUtils::Is{MatrixMultiply,PushGains,Transpose,Rpu}` @ `0x1e875b{20,80,40,60}` |
| **Combine** | `ComputeCombinablePairs` @ `0x126d2480` |
| **Unit assign** | `AssignXlu` @ `0x126d3100` (greedy least-loaded; requires `XlusPerTensorCore() > 1`) |
| **Reorder** | `ReorderToShortenCriticalPath` @ `0x126d3460` (per-XLU max-heap list scheduler) |
| **Reemit** | `ReemitReorderedCombinedXluOperations` @ `0x126d5460` |
| **Source bus** | `AssignSourceBus` @ `0x126d70e0` (Pufferfish-only in this build) |
| **Cost** | `CyclesAddedByXluOperation` @ `0x126d22a0`; edge = `ceil(base / xlu_count)` |
| **XLU count** | `Target::XlusPerTensorCore()` = `VectorIsa.xlu_count` = `DWORD[Target+0x4b0]` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Two Numbering Spaces

A reimplementer's first source of confusion is that an XLU operation has two distinct identities, and the binary uses both.

The **IR identity** is the `LloOpcode` — a value in the back end's ~461-entry opcode space (`opcode_name` table @ `0x21ccfef0`). This is what the `LloRegionBuilder` factories emit and what every optimizer pass switches on. The XLU-relevant `LloOpcode`s are: `0x36` `kVectorPermute`, `0x3a` `kVectorRotate`, `0x3b` `kVectorBroadcastLane`, `0x8b` `kVectorSetPermutePattern`, `0x8c` `kVectorSetSegmentPattern`, `0xa6` `kVectorTranspose`, `0xa7` `kVectorTransposeBinary`, the reduce family `0xf5..0x101`, `0x150` `kVectorPermuteResult`, `0x154` `kVectorTransposeResult`, and `0x155` `kVectorTransposeClear`.

The **wire identity** is the per-generation bundle-slot opcode. On Jellyfish (v2) that is the `VectorExtendedOpcode` proto enum, a dense 35-value enum `{0..34}` carved into three op classes: matmul `{0..6}`, push-gains/latch `{7..12}`, and the XLU/transpose/permute/cross-lane family `{13..34}`. The matmul and latch bands belong to the [MXU](slot-mxu.md) and [matprep/latch](slot-matprep-iar-latch.md) slots; the `{13..34}` band is the XLU family this page documents.

The two spaces are bridged at bundle-encode time: the high-level `LloOpcode` the factory emitted is lowered onto its `VectorExtendedOpcode` ordinal as the bundle's VEX slot is packed. The roster below tables both.

> **NOTE — proto value vs encoded-bundle field.** The `VectorExtendedOpcode` *value* (the protobuf enum number, equivalently the canonical op name index) is what the `ProtoUtils::Is*` classifiers index. The encoded-bundle *field* the encoder ORs into the VEX slot bits differs from the proto value on the `{13..34}` range (the `SET_PERMUTE`/`SET_SEGMENT` pair shift the encode-side mapping). This page documents the proto-value numbering — the one the classifiers and the LLO bridge use. The encoded-field value is a separate concern of the [JF bundle layout](bundle-jf-41b.md).

---

## The XLU Op-Factory Set

### Purpose

Every XLU op the back end materializes goes through the `LloRegionBuilder` cross-lane factory set. A reimplementer needs the exact opcode + operand-span each factory emits, because that is the LLO word the whole optimizer then schedules and the encoder then packs.

### Algorithm

Each factory has the identical shape, byte-exact from `Vsetperm` @ `0x1d52ba20`:

```c
// LloRegionBuilder::Vsetperm(LloValue* in, SetPermuteMode mode, int xlu, optional<int> bus)
LloValue* r = LloInstruction::CreateVectorSetPermutePattern(in, mode, xlu, bus, this->region());
return this->region()->AppendInstruction(r);   // jmp AppendInstruction @0x1d50f9a0
```

The factory is a wrapper; the `CreateVector*` op-constructor holds the opcode. Each `CreateVector*` builds the operand `Span` on the stack and calls one primitive — confirmed in the decompile, e.g. `CreateVectorSetPermutePattern` issues `LloInstruction::New(139, span, 1, region, …)` (139 = `0x8b`), `CreateVectorPermute` issues `New(54, span, 2, …)` (54 = `0x36`), `CreateVectorTranspose` issues `New(166, span, 2, …)` (166 = `0xa6`):

```c
// LloInstruction::New(LloOpcode op, Span<LloValue* const> operands,
//                     LloRegion*, LloValue*, PredicationPolarity, LloValue*)  @0x1d4cf560
// op is the first immediate; the Span size is the operand count.
v = LloInstruction::New(/*op=*/166, /*operands=*/&span, /*count=*/2, region, 0, 0, ...);
```

`New` writes `WORD[value] = op` (the op word) and wires the `Span` as the source operands.

### Op → Factory → Opcode Table

All factories are in namespace `xla::jellyfish::LloRegionBuilder::`. The emitted opcode is the `New` first-immediate; the operand count is the `Span` size. Decimal/hex both shown because the decompile prints decimals.

| Factory @addr | `CreateVector*` @addr | Emitted op (dec / hex) | Ops | Confidence |
|---|---|---|---|---|
| `Vsetperm` @`1d52ba20` | `…SetPermutePattern` @`1d4d62a0` | 139 / `0x8b` `kVectorSetPermutePattern` | 1 | CONFIRMED |
| `Vsetspr` @`1d52ba60` | `…SetSegmentPattern` @`1d4d64a0` | 140 / `0x8c` `kVectorSetSegmentPattern` | 1 | CONFIRMED |
| `Vpermute` @`1d52c180` | `…Permute` @`1d4d55c0` | 54 / `0x36` `kVectorPermute` | 2 `{data,pat}` | CONFIRMED |
| `Vpermuteres` @`1d52bfa0` | `…PermuteResult` @`1d4d5e40` | 336 / `0x150` `kVectorPermuteResult` | 1 | CONFIRMED |
| `Vrotate` @`1d52c6c0` | `…Rotate` @`1d4d58a0` | 58 / `0x3a` `kVectorRotate` | 2 `{data,amt}` | CONFIRMED |
| `Vbroadcastlane` @`1d52c9a0` | `…LaneBroadcast` @`1d4d6080` | 59 / `0x3b` `kVectorBroadcastLane` | 2 (+`0x3d`,1) | CONFIRMED |
| `Vxpose` @`1d54f580` | `…Transpose` @`1d4dcfe0` | 166 / `0xa6` `kVectorTranspose` | 2 | CONFIRMED |
| `Vxposeres` @`1d5501e0` | `…TransposeResult` @`1d4d5b60` | 340 / `0x154` `kVectorTransposeResult` | 0 (FIFO pop) | CONFIRMED |
| `VxposeBinaryCompressedB16` @`1d550220` | `…TransposeBinaryCompressedB16` @`1d4dd7e0` | 167 / `0xa7` `kVectorTransposeBinary` | 3 (+scale) | CONFIRMED |
| `VpackBf16` @`1d554680` | `…Weird` @`1d4d4e20` | 174 / `0xae` `kVectorWeird` | 1 | CONFIRMED |
| `VunpackUpperCF32` @`1d567f20` | `…Unpack` @`1d4d37c0` | 271 / `0x10f` `kVectorDynamicUnpack` | … | LOW (arm not isolated) |
| `VunpackLowerCF32` @`1d567e20` | `…Unpack` @`1d4d37c0` | 271 / `0x10f` `kVectorDynamicUnpack` | … | LOW (arm not isolated) |
| `Vunpackf32` @`1d554620` | — composite — | `VunpackLowerF32`+`CastTo(0x12)`+`VunpackUpperF32`+`CastTo(0x12)` | — | CONFIRMED (composite) |
| `VpermuteSync` @`1d52baa0` | — composite — | `Vpermute(0x36)` then `Vpermuteres(0x150)` | — | CONFIRMED (composite) |
| `VpermuteSlane` @`1d52d220` | `CreateVectorBinop` @`1d4d27c0` | (opcode arg-driven) | — | LOW (op arg-driven) |
| `VpackiB16` @`1d553380` / `VpackcB16` @`1d562700` | `CreateVectorPack` @`1d4d3140` | (opcode arg-driven) | 2 | LOW (op arg-driven) |

> **NOTE —** `VxposeBinaryCompressedB16` emits a single `0xa7` op (3 operands), not a multiply/pow chain. Its factory (`@0x1d550220`) and constructor (`@0x1d4dd7e0`) take the third operand as a `LloModule::ScalarU32ConstantImpl` scale value; the only extra action is a `target().SupportsVsupp()` gate (CHECK string at `llo_region_builder.cc:8617`). No `New(0x156/0x158/0x159)` call exists on the XLU path.

---

## The JF VectorExtendedOpcode Roster {0..34}

### Purpose

This is the wire-level enum the bundle encoder packs into the JF VEX slot. The `{13..34}` band is the XLU family; the `{0..12}` band is matmul + push-gains, documented in the [MXU](slot-mxu.md) and [matprep/latch](slot-matprep-iar-latch.md) slots. The `ProtoUtils::Is*` classifiers index the proto-enum value to route decode/encode dispatch.

### The Classifier Ranges (binary-exact)

Decompiled directly from the binary — these are the *real* dispatch ranges, in proto-enum numbering:

```c
IsMatrixMultiply(op) = (op < 7) & (0x77 >> op)        // {0,1,2,4,5,6}  (3 = DONE_WITH_GAINS excluded)
IsPushGains(op)      = (unsigned)(op - 7)  < 6         // {7..12}
IsTranspose(op)      = (unsigned)(op - 15) < 2         // {15,16}
IsRpu(op)            = (unsigned)(op - 17) < 0x12      // {17..34}
VectorExtendedUsesData(op) = (op != 3)                 // only op 3 reads no vector data operand
```

`ProtoUtils::IsRpu` (@ `0x1e875b60`) computes `(op - 17) < 0x12`, so the RPU band is `{17..34}` — `PERMUTE(17)` through `CROSS_LANE_SEGMENTED_MIN_INDEX_PERMUTE(34)`. `TRANSPOSE(15)`/`TRANSPOSE_START(16)` fall under `IsTranspose`, not `IsRpu`.

### Roster Table

`mnemonic` is the `ParserJf` cross-lane parse-pair string (assembler side); `LLO` is the high-level `LloOpcode` the `LloRegionBuilder` factory emits.

| val | name | classifier | mnemonic / LLO bridge | Confidence |
|---|---|---|---|---|
| 0 | `MATRIX_MULTIPLY` | `IsMatrixMultiply` | matmul | CONFIRMED |
| 1 | `MATRIX_MULTIPLY_LOW` | `IsMatrixMultiply` | matmul.low | CONFIRMED |
| 2 | `MATRIX_MULTIPLY_HIGH` | `IsMatrixMultiply` | matmul.hi | CONFIRMED |
| 3 | `DONE_WITH_GAINS` | `UsesData=false` | done-with-gains (no vector operand) | CONFIRMED |
| 4 | `MATRIX_MULTIPLY_DONE_WITH_GAINS` | `IsMatrixMultiply` | matmul.dwg | CONFIRMED |
| 5 | `MATRIX_MULTIPLY_LOW_DONE_WITH_GAINS` | `IsMatrixMultiply` | matmul.low.dwg | CONFIRMED |
| 6 | `MATRIX_MULTIPLY_HIGH_DONE_WITH_GAINS` | `IsMatrixMultiply` | matmul.hi.dwg | CONFIRMED |
| 7 | `PUSH_GAINS` | `IsPushGains` | push-gains | CONFIRMED |
| 8 | `PUSH_GAINS_LOW` | `IsPushGains` | push-gains.low | CONFIRMED |
| 9 | `PUSH_GAINS_HIGH` | `IsPushGains` | push-gains.hi | CONFIRMED |
| 10 | `PUSH_GAINS_TRANSPOSED` | `IsPushGains` | push-gains.xpose | CONFIRMED |
| 11 | `PUSH_GAINS_LOW_TRANSPOSED` | `IsPushGains` | push-gains.low.xpose | CONFIRMED |
| 12 | `PUSH_GAINS_HIGH_TRANSPOSED` | `IsPushGains` | push-gains.hi.xpose | CONFIRMED |
| 13 | `SET_PERMUTE_CONTROL_REGISTER` | — | LLO `0x8b` `Vsetperm` | CONFIRMED |
| 14 | `SET_SEGMENT_PATTERN_REGISTER` | — | LLO `0x8c` `Vsetspr` | CONFIRMED |
| 15 | `TRANSPOSE` | `IsTranspose` | `vxpose` — LLO `0xa6` `Vxpose` | CONFIRMED |
| 16 | `TRANSPOSE_START` | `IsTranspose` | `vxpose.start` — LLO `0xa7` `VxposeBinaryCompressedB16` | CONFIRMED |
| 17 | `PERMUTE` | `IsRpu` | LLO `0x36` `Vpermute` | CONFIRMED |
| 18 | `LANE_ROTATE` | `IsRpu` | LLO `0x3a` `Vrotate` | CONFIRMED |
| 19 | `ROTATING_PERMUTE` | `IsRpu` | (rotating permute) | CONFIRMED |
| 20 | `CROSS_LANE_ADD` | `IsRpu` | `vadd.xlane` — LLO reduce-add | CONFIRMED |
| 21 | `CROSS_LANE_MAX` | `IsRpu` | `vmax.xlane` — LLO reduce-max | CONFIRMED |
| 22 | `CROSS_LANE_MIN` | `IsRpu` | `vmin.xlane` — LLO reduce-min | CONFIRMED |
| 23 | `CROSS_LANE_MAX_INDEX` | `IsRpu` | `vmax.index.xlane` | CONFIRMED |
| 24 | `CROSS_LANE_MIN_INDEX` | `IsRpu` | `vmin.index.xlane` | CONFIRMED |
| 25 | `CROSS_LANE_ADD_PERMUTE` | `IsRpu` | `vadd.xlane.perm` | CONFIRMED |
| 26 | `CROSS_LANE_MAX_PERMUTE` | `IsRpu` | `vmax.xlane.perm` | CONFIRMED |
| 27 | `CROSS_LANE_MIN_PERMUTE` | `IsRpu` | `vmin.xlane.perm` | CONFIRMED |
| 28 | `CROSS_LANE_MAX_INDEX_PERMUTE` | `IsRpu` | `vmax.index.xlane.perm` | CONFIRMED |
| 29 | `CROSS_LANE_MIN_INDEX_PERMUTE` | `IsRpu` | `vmin.index.xlane.perm` | CONFIRMED |
| 30 | `CROSS_LANE_SEGMENTED_ADD_PERMUTE` | `IsRpu` | `vadd.xlane.seg.perm` — LLO `0xfc` seg-reduce | CONFIRMED |
| 31 | `CROSS_LANE_SEGMENTED_MAX_PERMUTE` | `IsRpu` | `vmax.xlane.seg.perm` — LLO `0xfa` seg-reduce | CONFIRMED |
| 32 | `CROSS_LANE_SEGMENTED_MIN_PERMUTE` | `IsRpu` | `vmin.xlane.seg.perm` — LLO `0xfb` seg-reduce | CONFIRMED |
| 33 | `CROSS_LANE_SEGMENTED_MAX_INDEX_PERMUTE` | `IsRpu` | `vmax.index.xlane.seg.perm` | CONFIRMED |
| 34 | `CROSS_LANE_SEGMENTED_MIN_INDEX_PERMUTE` | `IsRpu` | `vmin.index.xlane.seg.perm` | CONFIRMED |

The dense range `{0..34}` is confirmed by the `NameOfDenseEnum<descriptor,0,34>` instantiation @ `0x2239bce8`; the names are the protobuf `EnumValueDescriptorProto` identifiers (descriptor @ `0x1fa1fd00`). The two `SET_*` names are independently visible as `.rodata` strings; the `Is*` classifier bodies are decompiled byte-exact (above).

> **NOTE — the LLO reduce-family → cross-lane bridge.** The high-level reduce LLO ops `0xf5..0x101` lower onto the `CROSS_LANE_*` band `{20..34}`. The split is decided by `LloOpcodeIsSegmentedReduction(op) = (op - 250) < 3 = {0xfa,0xfb,0xfc}` (binary-confirmed @ `0x1d60c340`): segment reduces take the `SET_SEGMENT_PATTERN_REGISTER` (`Vsetspr`) prologue and lower onto `CROSS_LANE_SEGMENTED_*`; all other reduces take the `SET_PERMUTE_CONTROL_REGISTER` (`Vsetperm`) prologue and lower onto the non-segmented `CROSS_LANE_*` ops.

---

## The XLU Op-Combining Pipeline

### Purpose

The XLU is a scarce, multi-cycle resource issued from a slot shared with the MXU. Two adjacent XLU ops that do the *same* cross-lane operation (e.g. two sum-reduces feeding the same permute pattern) can be fused into one cross-lane pass that pays for the pattern setup once. `LloXluGraphOptimizer::Optimize` is the rewrite that finds those fusions, balances the surviving ops across the per-generation XLU units, reorders them to shorten the critical path, and packs them onto the VEX source buses.

### The Pipeline Order

The five stages run in this exact order, byte-mapped from the `Optimize` body (`@0x126cdb80`):

```text
AdjustEdgesBeforeXluAssignment   @0x126d1de0   ; pre-adjust dependency-graph edges
build PreXluAssignmentLatencyTable              ; XLU↔XLU edge = ceil(base / xlu_count)
CrossXlu Create (tracker #1, reverse=0)         ; data-dependency tracker over the XLU ops
ComputeCombinablePairs           @0x126d2480   ; (1) fuse-candidate pairs
[gate optimizer+0x28==1] AssignXlu @0x126d3100  ; (2) greedy least-loaded XLU-unit assign
CrossXlu Create (tracker #2, reverse=1)         ; rebuilt on the unit-assigned graph
ReorderToShortenCriticalPath     @0x126d3460   ; (3) latency-weighted list scheduler
ReemitReorderedCombinedXluOperations @0x126d5460; (4) emit fused/reordered LLO ops
[gate optimizer+0x28==1] AssignSourceBus @0x126d70e0 ; (5) VEX source-bus pack (Pufferfish only)
```

The dependency tracker (`CrossXluOperationsDataDependencyTracker`) is built **twice**: once before combine (`reverse=0`) and once before the reorder (`reverse=1`, against the post-combine, post-unit-assign graph). Both stages query its `XluOperationIsReady` predicate (in-edge count == 0).

### Stage 1 — ComputeCombinablePairs

`ComputeCombinablePairs` (`@0x126d2480`) takes the XLU-op list (a `vector<variant<TransposeTile, RpuOperation, XluControlOperation>*>`), the cross-region `from`/`to` boundary `LloValue` pair, and the dependency tracker, and returns a `vector<pair<variant*,variant*>>` of fusable pairs.

It builds per-op cost / value / cumulative-max arrays (the critical-path DP), then groups ops by a metadata key into per-key `btree_set<long>` buckets and emits a combinable pair whenever a later op collides with an earlier one on the same key, is tracker-ready, and is cost-compatible.

Two metadata keys, one per fusable variant:

| variant | key struct (byte-exact) | extractor |
|---|---|---|
| `RpuOperation` (idx 1) | `RpuOperationMetadata {u16 opcode@0, LloValue* op0@8, LloValue* op1@0x10 (gated u8@0x18==1)}` | `GetRpuTransposeOperationKeyFrom` @ `0x126d8520` |
| `TransposeTile` (idx 0) | `TransposeTileMetadata {i32 height@0, i64@8, u16@0x10, u8 vxpose_mode@0x12, u8@0x13}` | inline in the `$_0` visitor |

For an RPU op, the key is `{opcode, source-operand-0, source-operand-1}` — except `opcode == 0x3a` (`Vrotate`), which uses a single from-end operand (`op1 = 0`, `has1 = 0`). For a transpose tile, the key is `{height, anchor, vxpose-mode, …}`.

The fusion predicate, end to end:

> Two adjacent XLU ops fuse into one cross-lane operation **iff** (a) same variant kind **and** same fusion metadata; (b) the second op is list-scheduling-ready in the dependency graph (`XluOperationIsReady`, all predecessor XLU ops scheduled); and (c) combining keeps the critical-path cost bounded (the `CyclesAddedByXluOperation` DP arrays). `XluControlOperation` ops never fuse — the `$_2` visitor arm (`@0x2139b1c0`) is a hard `LogFatal`.

### Stage 2 — AssignXlu

`AssignXlu` (`@0x126d3100`) is a greedy least-loaded bin-packer. It is meaningful only for `XlusPerTensorCore() > 1` — the decompile contains the exact CHECK string `"dep_graph_->target()->XlusPerTensorCore() > 1"` (line 2563), a `LogFatal` if the gen has fewer than 2 XLUs.

It allocates a per-XLU running-cost record array (`xlu_count` records of 0x20 B), then for each combinable pair scans all records, picks the XLU with the minimum accumulated cost, writes that unit index into every backing `LloInstruction`'s unit-selector field (the `$_0` lambda @ `0x126db0a0`), and adds the op's `CyclesAddedByXluOperation` to that XLU's running cost. The unit choice is committed into the instruction word immediately, so the subsequent reorder and the final emission see it.

The unit-selector write is byte-identical to the `ValidateAndSetXluAndSourceBus` emission writer (below) — `AssignXlu` is the scheduler-side producer of the same field.

### Stage 3 — ReorderToShortenCriticalPath

`ReorderToShortenCriticalPath` (`@0x126d3460`) is a textbook latency-weighted list scheduler over the (already unit-assigned) ops. It allocates a per-XLU `PerXluOperations` state struct (stride `0x60`):

| offset | field | role |
|---|---|---|
| `+0x00..0x38` | `absl::btree_set<long, less, alloc, 256>` | per-XLU pending op-index set |
| `+0x38` | `i64` | remaining running-cycle accumulator (Σ cost of not-yet-scheduled ops) |
| `+0x40` / `+0x48` | `LloValue*` pair | last-scheduled op's source operand 0 / 1 (the next-delta anchor) |
| `+0x50` | `variant*` | last-scheduled op on this XLU (the `prev` for the next `CyclesAdded`) |
| `+0x58` | `i64` | per-XLU completion-time clock (critical-path frontier) |

Phase A pre-computes `cost[i] = CyclesAddedByXluOperation(...)` per op, accumulates into `PerXlu[xlu][+0x38]`, and builds the pending sets. Phase B runs a per-XLU `priority_queue<pair<long,long>>` max-heap keyed `{marginal_cost, op_index}` (`less<>` ⇒ longest-marginal-cost ready op first, ties broken by higher op-index). It pops the highest-priority op; the `$_2` lambda tests `XluOperationIsReady` plus a completion-clock critical-path test (`[+0x58] + cost >= finish[idx]`); on ready, the `$_1` lambda commits (erase from the pending set, advance `[+0x58] = max([+0x58]+cost, finish[idx])`, write the pair into the output); on not-ready, the `$_3` lambda re-prices and the op is re-queued. A pre-test (`cmp heap_top_cost, [PerXlu+0x38]; jl skip`) only attempts a reorder when it can still shorten that XLU's remaining path — the critical-path-shortening gate the function name describes.

### Stage 4 — ReemitReorderedCombinedXluOperations

`ReemitReorderedCombinedXluOperations` (`@0x126d5460`) is the IR rewriter that turns the scheduler's decisions into actual LLO. It builds a fresh emission `LloRegionBuilder` and a per-XLU `PerXluState` array (stride `0x20`) — the per-XLU "currently-set pattern" cache:

| offset | field |
|---|---|
| `+0x00` | permute-pattern source value currently set on this XLU |
| `+0x08` | the emitted `Vsetperm` result (shared `SetPermutePattern` setup) |
| `+0x10` | segment-pattern source value currently set on this XLU |
| `+0x18` | the emitted `Vsetspr` result (shared `SetSegmentPattern` setup) |

For each combinable pair it emits **one** fused cross-lane op through the factory set:

- **RPU pair** (variant idx 1): for each source whose producer is a `SetPermutePattern` (`0x8b`) op, compare against `PerXluState[xlu][+0x00]`; if different, emit `Vsetperm` and cache the result, else reuse the cached pattern. (`SetSegmentPattern` `0x8c` → `Vsetspr`, cached at `+0x18`/`+0x10`.) Then emit the fused reduce body consuming the single shared pattern result — two combinable reduces collapse into one cross-lane reduce. `ReplaceUsesOfInstruction` redirects the second op's uses; `RemoveNode` deletes the originals.
- **Transpose pair** (variant idx 0): match the two tiles' geometry (see slot-fit below), gate on `SupportsVectorXpose`/`NumVexSlots`, emit one fused `Vxpose` / `VxposeBinaryCompressedB16`, re-home both tiles' instructions (`PopInstruction`/`AppendInstruction`), and emit `Vxposeres` per result chunk with `ReplaceUsesOfInstruction`.

The economy: the per-XLU `SetPermute`/`SetSegment` pattern op is emitted **once per XLU and reused** — N reduces sharing a pattern pay for the pattern setup once.

### Stage 5 — AssignSourceBus

`AssignSourceBus` (`@0x126d70e0`) routes each XLU op's operands onto the VEX source buses. It is gated on `Target::HasVexSourceBuses()` (vtable `+0x408`) — `true` only on **Pufferfish (v4)** in this build (`JF`/`DF`/`VF`/`GL` return `false`), so the source-bus pass is a no-op on every other generation here.

When active, it walks the dependency graph in topological order, and for each op satisfying `LloOpcodeUsesSourceBus` (the 29-opcode set below) binds the op to a bus. MXU ops (`LloOpcodeUsesMxu`) bind to an explicit MXU-indexed bus; pure XLU ops greedily take the next free bus. The bus pool comes from `SourceBusesForXlu(i)` — on Pufferfish, `{i, i+2}` (decompile: `[+8]=i, [+0xc]=i+2, count-tag=4`) — so XLU 0 owns buses `{0,2}` and XLU 1 owns `{1,3}`, i.e. the V0/V1/V2/V3 read ports paired `(V0,V2)`/`(V1,V3)`. Two ops landing on the same bus get a new latency-weighted serialization edge (`UpdateEdge`) — the shared-bus structural hazard.

`LloOpcodeUsesSourceBus` (binary-confirmed @ `0x10c0d420`) is `true` for exactly **29** opcodes:

```text
{0x36, 0x3a, 0x3b}        permute / rotate / broadcast-lane
{0x8b, 0x8c}              set-permute-pattern / set-segment-pattern
{0x8f .. 0x96}            8 matmul-push ops (MXU operand path, UsesMxu)
{0xa6, 0xa7}              transpose / transpose-binary
{0xf5 .. 0x101}           13 cross-lane reduce / index / segment-reduce ops
{0x155}                   transpose-clear
```

### The Scheduler-Side Bit-Field

`AssignXlu` (unit) and `AssignSourceBus` (bus) both write `WORD[LloInstruction + 0xb]`; the LLO-emission validators `ValidateAndSetXluAndSourceBus` / `ValidateAndSetMxuAndSourceBus` re-assert the same field. Byte-exact from the decompile:

```c
// unit selector (XLU or MXU instance):
WORD[instr+0xb] = ((xlu & 3) << 8) | (WORD[instr+0xb] & 0xF8FF) | 0x400;   // bits 8-9 + valid bit 10
// source bus (Pufferfish only):
WORD[instr+0xb] = ((bus & 3) << 11) | (WORD[instr+0xb] & 0xC7FF) | 0x2000;  // bits 11-12 + valid bit 13
```

The source-bus field holds a raw 2-bit index `{0..3}` — **not** the SparseCore `VexSourcePortEncoding` proto enum, which is a distinct 3-bit encoding on a different datapath (see [VPU Slot](slot-vpu.md)).

---

## The XLU Cost Model

### CyclesAddedByXluOperation

`CyclesAddedByXluOperation` (`@0x126d22a0`) is the single marginal-latency function the combine DP, the `AssignXlu` min-cost pick, and the reorder heap priority all consume. Decompiled byte-exact, the closed form is:

```c
long CyclesAddedByXluOperation(variant* prev, variant* cur,
                               LloValue* from, LloValue* to, LatencyTable& tbl) {
    if (cur == null) {
        // empty-XLU base case → only a transpose prev contributes
        if (prev == null || prev.discr != 0 /*TransposeTile*/) {
            if (prev == null) return 0;
            goto transpose_tail;       // prev.discr == 0
        }
        return 0;
    }
    // MAIN EDGE: prev's anchor op → cur's anchor op
    long cost = tbl.LatencyBetween( op_data(prev), op_data(cur) );

    if (prev != null && prev.discr != 0 /*not TransposeTile*/) {
        if (cur.discr == 0 /*cur is TransposeTile*/) goto transpose_tail;
        CHECK(prev.discr == 1 /*RpuOperation*/);     // XluControlOperation prev ⇒ LogFatal line 1012
        // RPU prev: two inline source-operand identities at [prev+0x00], [prev+0x08]
        if (prev.src0 && prev.src0->operands(0) != from)
            cost += tbl.LatencyBetween( GetAnchorInstruction(cur), prev.src0 );
        if (prev.src1 && prev.src1->operands(0) != to)
            cost += tbl.LatencyBetween( GetAnchorInstruction(cur), prev.src1 );
        return cost;
    }

transpose_tail:                                       // prev is TransposeTile (or cur==null path)
    long n = prev.read_set_size;                      // [prev+0x08]
    if (n >= 2)
        cost += (n - 1) * tbl.LatencyBetween( readset[0].op_data, readset[1].op_data );
    return cost;
}
```

Interpretation:

- The dominant term is the **per-(op,op) edge latency** from the XLU's last op to the new op on the optimizer's table.
- For an RPU op, **each of its two source operands that is not the cross-region boundary value** (`from`/`to`) adds one more `LatencyBetween(cur_anchor, prev_source)` — the fan-in penalty for materializing a non-boundary source on the XLU. Boundary operands are free.
- For a transpose chain, the cost is `(read-set − 1)` copies of the latency between the first two tile elements — the per-extra-chunk transpose-sequence latency.
- An `XluControlOperation` as `prev` is a hard `LogFatal` — control ops are never priced, consistent with their never being combinable.

`GetAnchorInstruction` (`@0x126cda00`) resolves the variant to its anchor `LloValue`: idx 1 (RPU) → `[v+0x10]`; idx 2 (control) → `[v]`; idx 0 (transpose) → last read-set element; `op_data(v) = [resolve(v) + 0x10]`.

### The PreXluAssignmentLatencyTable Edge

`LatencyBetween` runs on the optimizer's own `PreXluAssignmentLatencyTable`, a wrapper around the per-generation base `LatencyTable` (selected by `LatencyTable::Create(TpuVersion)` registry dispatch). Its `LatencyBetweenInternal` (`@0x126e0e40`) is, byte-exact from the decompile:

```c
long LatencyBetweenInternal(LloValue* from, LloValue* to) {
    if (IsXluOp(from.op) && IsXluOp(to.op)) {
        int raw = delegate.LatencyBetween(from, to);   // [this+0x18] per-gen base table
        int div = xlu_count;                            // [this+0x20]
        return ceil(raw / div);                         // div>0: quotient + (raw > quotient*div ? 1 : 0)
    }
    return delegate.LatencyBetween(from, to);           // pass-through for non-XLU edges
}
```

`IsXluOp(op)` is the union of the 21 XLU opcodes `{0x8b, 0x8c, 0xa6, 0xa7, 0xf5..0x101, 0x14f, 0x150, 0x154, 0x155}` (the `case` labels in the decompile) and the low-band bit-mask `op <= 0x3b && bt(0xc40000000000000, op) = {0x36, 0x3a, 0x3b}` — `Vpermute` / `Vrotate` / `Vbroadcastlane`.

> The XLU↔XLU edge is the per-generation base latency **divided across the available cross-lane units**: the more XLUs, the cheaper a single XLU↔XLU edge — the parallelism discount the whole optimizer prices against. `xlu_count = Target::XlusPerTensorCore() = VectorIsa.xlu_count = DWORD[Target+0x4b0]`.

---

## Transpose Slot-Fit Geometry

### Purpose

A fused transpose must fit the per-generation VEX-slot budget. The reemit transpose path gates a two-tile fusion on a three-condition predicate before it can collapse the tiles into one `Vxpose`.

### VxposeMode and ElementCount

`VxposeMode` is a 5-value enum; `ElementCount(mode)` is the elements-per-chunk for the mode, read from the table at `0xb53c830`. Confirmed byte-exact (`xxd` of `.rodata` gives `01 00 00 00  02 00 00 00  04 00 00 00  01 00 00 00  02 00 00 00`):

| mode | name | ElementCount | meaning | Confidence |
|---|---|---|---|---|
| 0 | `B32` | 1 | full-width 32-bit transpose (default) | CONFIRMED |
| 1 | `Compressed B16` | 2 | bf16-compressed, 2 elements/chunk | CONFIRMED |
| 2 | `Compressed B8` | 4 | b8-compressed, 4 elements/chunk | CONFIRMED |
| 3 | `Segmented B32` | 1 | segmented 32-bit transpose | CONFIRMED |
| 4 | `Segmented B16` | 2 | segmented bf16 transpose | CONFIRMED |

### The Three-Gate Predicate

Byte-exact from the reemit transpose block (`@0x126d5b1b..0x126d5f5a`):

| gate | condition | accept / reject | Confidence |
|---|---|---|---|
| G1 | `target->SupportsVectorXpose(vxpose_mode) == true` (vtable `+0x100`) | reject (no fusion) if the gen does not support the mode | CONFIRMED |
| G2 | `[tile+0x30] % (SublaneCount() * ElementCount(mode)) == 0` (imul + idiv + test-remainder) | reject if the chunk dimension is not a whole number of slot-sized element chunks | CONFIRMED |
| G3 | `NumVexSlots() != 0` (vtable `+0x690`) | accept → emit; pack into `NumVexSlots()` VEX slots | CONFIRMED |
| G3′ | if `NumVexSlots() == 0`: `ChunksPerTile() == (chunks >> mode_shift)` | reject if not equal (no-VEX-slot path) | CONFIRMED |

A gen *with* VEX slots packs the fused transpose into `NumVexSlots()` vector_extended slots; a gen *without* can only fuse a transpose occupying exactly one tile's worth of chunks. (Before the gates, the two tiles must already match on `GetNumberOfChunksInTransposeSequence`, `vxpose_mode`, `TransposeResultChunkCount`, and `GetTransposeWidth`.)

### Per-Gen Target Overrides

Byte-exact from the per-`Target` vtable slots. `NumVexSlots()` is the per-gen `vector_extended` slot count — the same slot the [MXU](slot-mxu.md) and [EUP](slot-eup-transcendental.md) use; on JF it is the single VEX slot of the [41-byte bundle](bundle-jf-41b.md).

| Target (gen) | `NumVexSlots()` | `SupportsVectorXpose(mode)` | Confidence |
|---|---|---|---|
| `JellyfishTarget` (v2) | 1 (`return 1`) | `mode == 0` (B32 only) | CONFIRMED |
| `PufferfishTarget` (v4) | 2 | `mode != 2` (all except Compressed B8) | CONFIRMED |
| `GhostliteTarget` (v6e) | 2 | `mode < 3` (B32 / Compressed B16 / B8) | CONFIRMED |
| `ViperfishTarget` (v5p) | 2 | `mode != 2` (all except Compressed B8) | CONFIRMED |
| `Target` (base) | `LogFatal` | abstract | CONFIRMED |

> **NOTE —** the PF/VF `SupportsVectorXpose` bodies (`0x1d4940a0` / `0x1d49a000`) are `return a2 != 2;`: they accept every `VxposeMode` **except** mode 2 (Compressed B8). The `cmp esi, 2; ret` form is an inequality test, not `mode == 2`. See [Transpose Reservation Latency](../cost/xpose-reservation-latency.md) for the `VxposeMode` ordinal roster.

---

## Worked Example — Two `kVectorAddReduceF32` Fused (Pufferfish v4, xlu_count = 2)

1. `ComputeCombinablePairs` emits the pair `{&R_a, &R_b}` — equal `RpuOperationMetadata{0xf7, src0, src1}` (`0xf7 ≠ 0x3a`, so two source operands), `R_b` tracker-ready, cost-compatible.
2. `AssignXlu`: `xlu_count == 2` (≥ 2 OK). Both fit XLU 0 (min cost, tie → index 0). The `$_0` lambda writes `unit = 0 | valid` into both instruction words. Cost `= CyclesAddedByXluOperation(...) = ceil(B / 2)` where `B` is the per-gen base reduce-edge latency — half the serial latency because the two XLUs run in parallel.
3. The tracker is rebuilt (`reverse = 1`) on the unit-assigned graph. `ReorderToShortenCriticalPath` initializes `PerXluOperations[0]`, queues `R_a`/`R_b` keyed by `$_3` marginal cost, and schedules the longest-cost ready op first.
4. `ReemitReorderedCombinedXluOperations`: pair `{R_a, R_b}`, both RPU, `xlu = 0`. Emit `Vsetperm` **once** (cache in `PerXluState[0][+0x08]`); the two reduces collapse into one fused cross-lane reduce consuming that single pattern setup. `ReplaceUsesOfInstruction` redirects `R_b`'s uses; `RemoveNode` deletes the originals.
5. `AssignSourceBus` (`HasVexSourceBuses = true`): packs the fused op onto a V-port from `SourceBusesForXlu(0) = {0,2}`; the 2-bit bus index is written into `WORD[+0xb]` bits 11-13.

Contrast a **segment** reduce pair (`0xfc`): `LloOpcodeIsSegmentedReduction(0xfc) = true`, so the shared prologue is `Vsetspr` (cached at `PerXluState[+0x18]`) instead of `Vsetperm`. Contrast a **transpose** pair (`0xa6`): variant idx 0; reemit matches vxpose-mode/height/chunk-count, gates on the three-gate slot-fit predicate, emits one fused `Vxpose`, re-homes both tiles' instructions, and emits `Vxposeres` per result chunk.

---

## What Is Not Pinned

- The `SetPermuteMode` enumerator names (the 2nd `Vsetperm` arg): the value is threaded through as `DWORD[variant+0x40]` but no `SetPermuteModeToString` was located. LOW.
- The `VunpackUpperCF32` / `VunpackLowerCF32` exact `CreateVectorUnpack` arm + its `VpackFormat`: multiple `New` arms (`0x10f` and a dynamic-opcode arm), the CF32-specific half not isolated. LOW.
- The `VpermuteSlane` / `VpackiB16` / `VpackcB16` emitted opcodes are passed as a parameter to `CreateVectorBinop` / `CreateVectorPack`; the concrete value per call is not isolated. LOW.
- The PF/VF `SupportsVectorXpose` exact mode mask (ICF-folded `cmp esi,2; ret` thunk). LOW.
- The `LatencyBetween` stochastic perturbation (`UniformDistribution(0,0x65)` added when `[table+0x10] != 0`): present in the cost path, its enable condition and scheduling effect not isolated.

---

## Cross-References

- [VPU Slot](slot-vpu.md) — the per-lane vector ALU; the EUP/XLU push-pop protocol and the distinct SparseCore `VexSourcePortEncoding`.
- [MatPrep/IAR/Latch Slot](slot-matprep-iar-latch.md) — the matmul `{0..6}` / push-gains `{7..12}` bands that share the `VectorExtended` slot with the XLU.
- [vcreate_mask & M-Register](slot-vcreate-mask-mregister.md) — the SparseCore vector-mask file the masked-scan family selects.
- [ResultFifo & ArchRegister](resultfifo-archregister.md) — the result FIFOs the `Vxposeres` / `Vpermuteres` pops drain.
- [Bundle Model](bundle-model-overview.md) — the VLIW bundle the encoder packs the XLU op into; the per-gen `NumVexSlots()` budget.
