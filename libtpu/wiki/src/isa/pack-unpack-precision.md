# Pack/Unpack Precision

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA minus `0x200000` equals its file offset. Other libtpu builds will differ.*

## Abstract

This is the XLU's *precision-conversion datapath*: the family of LLO ops that move data between f32 and the narrow 16-bit format (bf16) that is *packed two-per-lane* inside a vector register, plus the per-segment cross-lane reduction that consumes a packed-and-widened embedding tensor. Two distinct hardware mechanisms live here, and the binary keeps them on separate factory chains a reimplementer must not conflate.

The first is **lane pack/unpack**. A `bf16` is bit-identical to the *top 16 bits* of an IEEE `f32`, so a 2-element bf16 vector fits in one 32-bit lane and the f32 widen is a pure position-and-zero — no arithmetic rounding. `VpackBf16` interleaves two bf16 vregs into one packed lane (`kVectorPack` `0x126`, `VpackFormat = 7 InterleavedBf16`); the inverse `VunpackUpperCF32` / `VunpackLowerCF32` select one bf16 half back out (`kVectorUnpack` `0x109`, `CompressedBf16`, with a `uint` upper/lower index); and the composite `VunpackF32` widens both halves to full f32 by *shifting the low lane left 16* and *masking the high lane to `0xffff0000`*. The format and the unpack sub-lane index are packed into one 64-bit instruction word at `+0x40`. This is the LLO level the EUP `AluEp` `UnpackFOp`/`PackFOp` ultimately lower to (see [EUP / Transcendental Slot](slot-eup-transcendental.md)).

The second is the **segmented reduction** (the per-segment cross-lane reduce). Three F32-only opcodes — `kVector{Max,Min,Add}SegmentReduceF32` `0xfa`/`0xfb`/`0xfc` — reduce within lane segments whose boundaries are programmed by a separate `kVectorSetSegmentPattern` (`0x8c`) op built by `Vsetspr`. It is the TensorCore analogue of an embedding segment-sum: the accumulator resets at each segment boundary the pattern register marks. The RPU re-emit pass fuses a pair of segment reduces by sharing one pattern setup. On the device codegen path this is a Jellyfish/Pufferfish primitive only — Viperfish and Ghostlite return `false` from `SupportsSegmentedReduce` and their TensorCore emitters are dead `LogFatal` stubs, the per-segment embedding reduce having moved to the SparseCore on those generations.

For reimplementation, the contract is:

- **The bf16 pack:** `VpackBf16` → `kVectorPack 0x126`, two operands, `VpackFormat = 7 InterleavedBf16`, gated by `SupportsVectorPackOps(7)`.
- **The bf16 unpack:** `Vunpack{Upper,Lower}CF32` → `kVectorUnpack 0x109`, one operand, `VpackFormat = 1 CompressedBf16`, `uint` index `1` (upper) / `0` (lower), gated by `SupportsVectorUnpackOps`.
- **The f32 widen:** `VunpackF32 = { CastTo(f32, lower<<16), CastTo(f32, upper&0xffff0000) }`.
- **The instruction-word format field** at `+0x40`: `VpackFormat` in bits 0-15, sub-lane/unpack index in bits 16-31, with the has-value / fan-in checks per opcode class.
- **The segment-reduce opcode set** `{0xfa,0xfb,0xfc}` (F32 only), the `kVectorSetSegmentPattern 0x8c` boundary program, the RPU shared-pattern dispatch, the per-gen VEX-opcode map, and the JF/PF-only support gate.

| | |
|---|---|
| **bf16 pack** | `VpackBf16` (`0x1d554680`) → `VpackBf16Inst` (`0x1d565940`) → `CreateVectorPack` (`0x1d4d3140`) → `kVectorPack 0x126`, `InterleavedBf16` (7) |
| **bf16 unpack** | `Vunpack{Upper,Lower}CF32` (`0x1d567f20`/`0x1d567e20`) → `CreateVectorUnpack` (`0x1d4d37c0`) → `kVectorUnpack 0x109`, `CompressedBf16` (1), index 1/0 |
| **f32 widen** | `VunpackF32` (`0x1d554620`); lower `<<16` (`SimplifyShllU32`), upper `&0xffff0000` (`SimplifyAndU32`) |
| **Format word** | `set_pack_format_sublane` (`0x1d4d3440`) → `WORD[instr+0x40]` bits 0-15 = `VpackFormat`, bits 16-31 = sub-lane index |
| **Segment reduce** | `kVector{Max,Min,Add}SegmentReduceF32` `0xfa`/`0xfb`/`0xfc` (F32 only); `LloOpcodeIsSegmentedReduction` (`0x1d60c340`) = `(op-0xfa)<3` |
| **Segment pattern** | `Vsetspr` (`0x1d52ba60`) → `CreateVectorSetSegmentPattern` (`0x1d4d64a0`) → `kVectorSetSegmentPattern 0x8c` (no mode field) |
| **Segment-reduce support** | JF / PF only; VF / GL `SupportsSegmentedReduce` = `false`, TC emitter is `LogFatal` |

---

## bf16 Pack: InterleavedBf16, Two Lanes into One

### Purpose

A reimplementer's entry point is "two bf16 vregs → one packed f32-width vreg". This is the format the EUP/XLU pipeline moves bf16 reductions through, because reducing a packed embedding row in f32 requires first widening it (next section), and storing the result as bf16 requires re-packing it.

### Algorithm

`VpackBf16(a, b)` (`0x1d554680`) is a peephole front then a tail-call to the real factory:

```c
function VpackBf16(a, b):                               // 0x1d554680
    folded = SimplifyPackF16(a, b, RegisterType=4)      // 0x1d599360, const-fold two bf16 constants
    if folded: return folded
    return VpackBf16Inst(a, b)                          // 0x1d565940 (tail)

function VpackBf16Inst(a, b):                           // 0x1d565940
    target = this->module->target                       // *(this+56)->+16
    if not target->vtable[+0x4c8](7):                   // SupportsVectorPackOps(format=7)
        UpdateStatus("target().SupportsVectorPackOps(format)")   // llo_region_builder.cc:13132
    inst = CreateVectorPack(0x126, 7, a, b, region)     // 0x1d4d3140
    return region->AppendInstruction(inst)
```

The decompile shows `VpackBf16Inst` calling `CreateVectorPack(294, 7u, …)` — `294` = `0x126` = `kVectorPack`, format `7` = `InterleavedBf16` — after the gate `(*(…+1224))(v7, 7)` where `1224` = `0x4c8` is the target sub-object vtable slot for `SupportsVectorPackOps`. `CreateVectorPack` (`0x1d4d3140`) builds a two-operand `New(0x126, {a,b})` and calls `set_pack_format_sublane(7, nullopt)`.

> **NOTE —** the bf16 pair-pack is **not** `kVectorWeird` (`0xae`). The `0xae` op is the *distinct one-operand* `VweirdBf16` factory (`0x1d5546e0` → `CreateVectorWeird` `0x1d4d4e20`), a single-input cross-lane op gated on `SupportsBf16AluInstructions` (vtable `+0x780`) that dispatches on the result `PrimitiveType` (`0xb` BF16 / `0x10` F16). An earlier reading that routed `VpackBf16` through `CreateVectorWeird`/`0xae` was wrong; the two share no factory.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LloRegionBuilder::VpackBf16` | `0x1d554680` | Peephole + tail-call entry | CERTAIN |
| `LloRegionBuilder::VpackBf16Inst` | `0x1d565940` | Gate + emit `0x126` | CERTAIN |
| `LloInstruction::CreateVectorPack` | `0x1d4d3140` | `New(0x126, {a,b})` + format | CERTAIN |
| `llo_simplifier::SimplifyPackF16` | `0x1d599360` | Constant-fold two bf16 | HIGH |
| `LloRegionBuilder::VweirdBf16` | `0x1d5546e0` | Distinct 1-op `0xae` factory | CERTAIN |

---

## bf16 Unpack: CompressedBf16 with an Upper/Lower Index

### Purpose

The inverse of the pack: pull one of the two bf16 halves back out of a packed lane. The CF32 form keeps the result in the compressed (16-bit-lane) domain; the F32 form (next section) widens it to a full 32-bit float.

### Algorithm

```c
function VunpackUpperCF32(in):                          // 0x1d567f20
    folded = SimplifyUnpack(index=1, format=1, in, 0)   // 0x1d593060
    if folded: return folded
    target = this->module->target
    if not target->vtable[+0x4d8](1, 0):                // SupportsVectorUnpackOps(format=1, TpuCoreType=0)
        UpdateStatus("target().SupportsVectorUnpackOps(format)")  // llo_region_builder.cc:13740
    inst = CreateVectorUnpack(0x109, index=1, format=1, in, region)   // 0x1d4d37c0
    return region->AppendInstruction(inst)

function VunpackLowerCF32(in):                          // 0x1d567e20
    // identical, index = 0; llo_region_builder.cc:13730
```

The decompile is unambiguous: `VunpackUpperCF32` calls `CreateVectorUnpack(0x109u, 1, 1u, …)` and `VunpackLowerCF32` calls `CreateVectorUnpack(0x109u, 0, 1u, …)` — opcode `0x109` (`kVectorUnpack`), index `1`/`0`, format `1` (`CompressedBf16`). The gate vtable slot `+0x4d8` (`1240`) is `SupportsVectorUnpackOps`, called with `(1, 0)`. **Upper = index 1, lower = index 0.**

`CreateVectorUnpack` (`0x1d4d37c0`) builds a one-operand `New(0x109, {in})` and calls `set_pack_format_sublane(format, index | engaged)` — the index is folded into the sub-lane field (next section).

> **CORRECTION —** the bf16 unpack opcode is `0x109 kVectorUnpack`, **not** `0x10f kVectorDynamicUnpack`. `0x10f` is the separate `CreateVectorDynamicUnpack` (`0x1d4d3d80`), gated by `SupportsDynamicUnpackOps` (vtable `+0x4e0`, `false` on Viperfish). The sibling sub-byte unpacks `kVectorUnpackAndJoinB2ToB4`/`B4ToB8` (`0x10a`-`0x10d`) and `kVectorUnpackEXMY` (`0x10e`) are distinct opcodes again.

### Function Map

| Function | Address | Emits | Confidence |
|---|---|---|---|
| `LloRegionBuilder::VunpackUpperCF32` | `0x1d567f20` | `0x109`, idx 1, fmt 1 | CERTAIN |
| `LloRegionBuilder::VunpackLowerCF32` | `0x1d567e20` | `0x109`, idx 0, fmt 1 | CERTAIN |
| `LloInstruction::CreateVectorUnpack` | `0x1d4d37c0` | `New(0x109, {in})` + format | CERTAIN |
| `LloInstruction::CreateVectorDynamicUnpack` | `0x1d4d3d80` | `0x10f` (separate path) | HIGH |
| `llo_simplifier::SimplifyUnpack` | `0x1d593060` | Const-fold peephole | HIGH |

---

## The f32 Widen: Shift the Low Lane, Mask the High Lane

### Purpose

`bf16` is the top 16 bits of an `f32`. Widening therefore needs no rounding — only repositioning. `VunpackF32` produces the *pair* of widened f32 lanes from one packed bf16 vreg.

### Algorithm

```c
function VunpackF32(in):                                // 0x1d554620
    lo = CastTo(f32 /*0x12*/, VunpackLowerF32(in))      // 0x1d528660
    hi = CastTo(f32 /*0x12*/, VunpackUpperF32(in))      // 0x1d528580
    return { lo, hi }                                   // composite pair

function VunpackLowerF32(in):                           // 0x1d528660
    u = CreateVectorUnpack(0x109, index=0, format=7 /*InterleavedBf16*/, in)
    return SimplifyShllU32(u, VectorU32Constant(16))    // 0x1d58f8a0 : low bf16 -> high 16 of f32, low 16 = 0

function VunpackUpperF32(in):                           // 0x1d528580
    u = CreateVectorUnpack(0x109, index=1, format=7, in)
    return SimplifyAndU32(VectorU32Constant(0xffff0000), u)  // 0x1d58dac0 : high bf16 already in place, mask low 16
```

The decompile confirms each step: `VunpackLowerF32` builds `VectorU32Constant(0x10)` and calls `SimplifyShllU32` (shift left 16); `VunpackUpperF32` builds `VectorU32Constant(0xFFFF0000)` and calls `SimplifyAndU32` (mask). The lower lane's 16 bf16 bits become the high half of an f32 with a zero mantissa-low; the upper lane is already in the high half, so it is masked clean. The result is exact — the round-trip `pack`/`unpack` is bit-preserving (`bf16` is the f32 high half).

> **NOTE —** the `VunpackF32` family widens with `VpackFormat = 7 InterleavedBf16` (the *interleaved* layout, fan-in 2), whereas the CF32 family uses `VpackFormat = 1 CompressedBf16`. The format selects how the two bf16 lanes are laid out in the source word; the widen arithmetic (shift/mask) is the same either way.

---

## The Instruction-Word Format Field (`+0x40`)

### Purpose

Both pack and unpack stash their format (and, for unpack, the sub-lane index) in one 64-bit field of the LLO instruction. A reimplementer encoding these ops must reproduce the bit layout and the per-opcode validity checks.

### Encoding

`set_pack_format_sublane(VpackFormat fmt, optional<u16> sublane)` (`0x1d4d3440`) writes `QWORD[instr+0x40]`. The decompile switches on the opcode:

```c
function set_pack_format_sublane(instr, fmt, sublane):  // 0x1d4d3440
    switch instr.opcode:
      case 0x109, 0x10E:                                // unpack / unpackEXMY -> sublane REQUIRED
          assert sublane.has_value()                    // llo_instruction.cc:3714
          assert *sublane < VpackFormatSublanesIndices(fmt)   // :3715  (index < lane fan-in)
          word = (word & ~0xFFFF0000) | (sublane << 16)
      case 0x10F, 0x126:                                // dynamic-unpack / pack -> sublane FORBIDDEN
          assert not sublane.has_value()                // llo_instruction.cc:3723
      default:
          LogFatal("unexpected instruction: <opcode>")  // :3726
    word = (word & ~0xFFFF) | fmt                       // VpackFormat in bits 0-15
    instr[+0x40] = word
```

So **bits 0-15 = `VpackFormat`**, **bits 16-31 = sub-lane/unpack index**. The unpack opcodes (`0x109`, `0x10e`) *require* a sub-lane index and check it is below the format's lane fan-in; the pack opcodes (`0x126`, `0x10f`) *forbid* one. `VpackFormatSublanesIndices(fmt)` (`0x1d629d60`) is a one-line `return dword_B53C790[fmt]` — a direct index into a `.rodata` `i32` table at `0xb53c790` giving the number of source lanes that fan into one packed slot (2 or 4 per format).

### VpackFormat (selected values)

The `VpackFormat` enum names come from `VpackFormatString` (`0x1d629960`). The values directly on the bf16↔f32 datapath:

| Value | Name | Lane fan-in | Role | Confidence |
|---|---|---|---|---|
| 0 | *(invalid)* | — | Sentinel / unset | HIGH |
| 1 | `CompressedBf16` | 2 | CF32 unpack source | CERTAIN |
| 7 | `InterleavedBf16` | 2 | `VpackBf16` pack format; `VunpackF32` widen | CERTAIN |
| 11 | `CompressedHf16` | 2 | f16 (half) compressed | HIGH |
| 19-22 | `Compressed{U8,S8,U4,S4}ToBf16` | 2 / 4 | int→bf16 dequant (embedding quant) | HIGH |

The full 26-entry enum extends through the fp8 (`F8E5M2`/`F8E4M3*`) and sub-byte dequant formats; those are the province of the quant pack/unpack path. See [Bias-Add & Quant/Dequant Helpers](bias-quantization-helpers.md).

### Per-Generation Pack/Unpack Capability Masks

The gate vtable slots (`+0x4c8` pack, `+0x4d8` unpack, `+0x4e0` dynamic-unpack) on the target's `+0x10` sub-object are byte-exact bitmask predicates:

| Target / method | Address | Predicate | Supported formats | Confidence |
|---|---|---|---|---|
| `ViperfishTarget::SupportsVectorPackOps` | `0x1d49b1a0` | `(fmt-1) < 0xA` | 1..10 | CERTAIN |
| `ViperfishTarget::SupportsVectorUnpackOps` | `0x1d49b1c0` | `fmt < 0xE && (0x39FE >> fmt)&1` | 1,2,3,4,5,6,7,8,11,12,13 | CERTAIN |
| `GhostliteTarget::SupportsVectorPackOps` | `0x1d498020` | `fmt < 0x17 && (0x7807FE >> fmt)&1` | 1..10, 19..22 | CERTAIN |
| `GhostliteTarget::SupportsVectorUnpackOps` | `0x1d498040` | `fmt < 0x17 && (0x7839FE >> fmt)&1` | 1..13, 19..22 | CERTAIN |

`InterleavedBf16` (7) is in both generations' pack set and `CompressedBf16` (1) in both unpack sets, so the bf16↔f32 datapath is universally available; Ghostlite additionally admits the `U8/S8/U4/S4→bf16` dequant pack formats (embedding quantization).

---

## Segmented Reduction: Per-Segment Cross-Lane Reduce

### Purpose

A segment reduce is an embedding-style aggregation: the lanes of a vreg are partitioned into segments, and the reduce produces one accumulator per segment, resetting at each boundary. The boundary pattern is supplied by a separate op. A reimplementer must model this as a *two-op* sequence — set the pattern, then reduce against it.

### Algorithm

The opcode set is exactly three, F32-only:

```c
bool LloOpcodeIsSegmentedReduction(op):                 // 0x1d60c340
    return (uint16)(op - 0xfa) < 3                       // {0xfa, 0xfb, 0xfc}
```

- `0xfa` `kVectorMaxSegmentReduceF32`
- `0xfb` `kVectorMinSegmentReduceF32`
- `0xfc` `kVectorAddSegmentReduceF32`

There is **no bf16 segment reduce**: the surrounding reduce family is `0xf5`-`0xf9` (plain F32 Min/Max/Add/MaxIndex/MinIndex), then `0xfa`-`0xfc` (F32 segment), then `0xfd`-`0x101` (plain bf16 reduces); `0x102` (`kVectorSublaneId`) is the next non-reduce op.

The boundary register is programmed by `Vsetspr`:

```c
function Vsetspr(pattern_src, xlu, source_bus):          // 0x1d52ba60
    inst = CreateVectorSetSegmentPattern(pattern_src, xlu, source_bus, region)   // 0x1d4d64a0
    return region->AppendInstruction(inst)

function CreateVectorSetSegmentPattern(pattern, xlu, bus, region):   // 0x1d4d64a0
    assert opcode_produced_register_type[pattern.opcode] == 4   // "pattern->ProducesVreg()", llo_instruction.cc:918
    inst = New(0x8c /*kVectorSetSegmentPattern*/, {pattern})     // one operand
    ValidateAndSetXluAndSourceBus(xlu, bus, inst)               // XLU index -> WORD[instr+0xb]
    set_annotation(inst)
    return inst
```

`CreateVectorSetSegmentPattern` (`0x1d4d64a0`) asserts the pattern source `ProducesVreg()` (`opcode_produced_register_type == 4`), builds the one-operand `New(0x8c, {pattern})`, and stamps the XLU/source-bus into `WORD[instr+0xb]` via `ValidateAndSetXluAndSourceBus` (`0x1d4d5180`: bits 8-9 = `xlu & 3`, bit 10 `0x400` = the XLU-op flag).

> **NOTE —** `kVectorSetSegmentPattern` (`0x8c`) carries **no** mode field, unlike its sibling `kVectorSetPermutePattern` (`0x8b`), which stores a `SetPermuteMode` (`{one_sublane=0, one_sublanes=1}`) at `DWORD[instr+0x40]`. The segment pattern is a pure per-lane segment-boundary program; the permute pattern additionally selects a permute granularity. See [XLU Op Roster](xlu-op-roster.md).

---

## The RPU Re-emit: Sharing One Pattern Setup

### Purpose

The reorder/pipeline (RPU) re-emit pass fuses adjacent XLU reduces so a *pair* of segment reduces shares one `Vsetspr` pattern setup — the per-segment cross-lane reduce economy. A reimplementer's scheduler must replicate this fusion to match the emitted bundle stream.

### Algorithm

`ReemitReorderedCombinedXluOperations` caches one pattern result per XLU in a per-XLU state record, then rewires the fused reduce operands onto it:

```c
// per source producer:
if producer is SetPermutePattern (0x8b):
    Vsetperm(mode = variant[+0x40], xlu = rpu[+0x20]); cache -> PerXluState[+0x08]
if producer is SetSegmentPattern (0x8c):
    Vsetspr(xlu = rpu[+0x20]);                          cache -> PerXluState[+0x18]

// reduce body:
if LloOpcodeIsSegmentedReduction(op):                   // segment path
    ReplaceOperandIn(op_a, shared_segment_result)       // DOUBLE rewire ...
    ReplaceOperandIn(op_b, shared_segment_result)       // ... both fused operands
else:                                                    // plain reduce path
    ReplaceOperandIn(op, shared_permute_result)         // single rewire
PopInstruction(); AppendInstruction()                   // re-home both ops
```

The segment path does a **double** `ReplaceOperandIn` (both fused-reduce source operands rewire onto the shared `Vsetspr` segment-pattern result); the plain reduce path rewires a single operand onto the shared `Vsetperm` permute result. This is how two segment reduces on the same XLU collapse to one pattern program plus two reduces.

---

## Per-Generation TensorCore Emit and Support

### Purpose

The LLO segment ops map to VEX-control opcodes the TensorCore bundle understands, but only on Jellyfish and Pufferfish. A reimplementer targeting Viperfish or later must route per-segment embedding reduction to the SparseCore instead.

### Encoding

`JellyfishEmitter::EmitVectorSegmentedReduce` (`0x140b6c80`) maps the LLO opcode to a VEX opcode and emits it through `EmitVectorExtendedInstruction` (one `Vregno` data operand scheduled into a VEX bundle slot):

```c
function EmitVectorSegmentedReduce(op, src, trf_id):     // 0x140b6c80
    CHECK(trf_id == 0)                                   // jellyfish_emitter.cc:1248
    switch op:
      case 0xfa: vex = 0x1f                              // kVectorMaxSegmentReduceF32
      case 0xfb: vex = 0x20                              // kVectorMinSegmentReduceF32
      case 0xfc: vex = 0x1e                              // kVectorAddSegmentReduceF32
      default:   NoteError("unhandled opcode for vector segmented reduce: %s")
    EmitVectorExtendedInstruction(this, vex, src, 0)

function EmitVectorSetSegmentPattern(src, spr_id):       // 0x140b58e0
    CHECK(spr_id == 0)                                   // jellyfish_emitter.cc:1027
    EmitVectorExtendedInstruction(this, 0xe, src, 0)     // VEX opcode 0xe
```

The decompile confirms the literal VEX values: `0xfa→0x1f` (`31`), `0xfb→0x20` (`32`), `0xfc→0x1e` (`30`), `SetSegmentPattern→0xe` (`14`). The plain reduce family `0xf5`-`0xf9` maps to VEX `{0x16,0x15,0x14,0x17,0x18}` (a separate cross-lane-reduce emitter). The `.seg.perm` cross-lane mnemonics (`vadd.xlane.seg.perm`, `vmax.xlane.seg.perm`, `vmin.xlane.seg.perm`) in `.rodata` confirm the segment-pattern-driven reduce semantics.

| LLO opcode | Name | JF VEX opcode | Confidence |
|---|---|---|---|
| `0x8c` | `kVectorSetSegmentPattern` | `0xe` | CERTAIN |
| `0xfc` | `kVectorAddSegmentReduceF32` | `0x1e` | CERTAIN |
| `0xfa` | `kVectorMaxSegmentReduceF32` | `0x1f` | CERTAIN |
| `0xfb` | `kVectorMinSegmentReduceF32` | `0x20` | CERTAIN |

### The JF/PF-Only Support Gate

`SupportsSegmentedReduce` is a one-line per-target predicate:

| Target | Address | Returns | TC emitter behaviour | Confidence |
|---|---|---|---|---|
| `JellyfishTarget` | `0x1d4909c0` | `1` (true) | Emits VEX `0xe`/`0x1e`/`0x1f`/`0x20` | CERTAIN |
| `PufferfishTarget` | `0x1d494f80` | `1` (true) | Emits (no partial-result drain — `SupportsSegmentedReducePartialResults` `false`) | CERTAIN |
| `ViperfishTarget` | `0x1d49b380` | `0` (false) | `LogFatal "Operation not supported."` | CERTAIN |
| `GhostliteTarget` | `0x1d4981c0` | `0` (false) | `LogFatal "Operation not supported on Ghostlite."` | CERTAIN |

Both the Viperfish (`ViperfishTensorCoreEmitter::EmitVectorSegmentedReduce` `0x141dd2c0`) and Ghostlite (`GhostliteTensorCoreEmitter::EmitVectorSegmentedReduce` `0x1429ff60`) TensorCore segment-reduce emitters are `__noreturn` `LogMessageFatal` stubs. On those generations the per-segment embedding aggregation runs on the SparseCore VectorExtended unit, not the TensorCore XLU — the segment-reduce became a JF/PF legacy primitive.

---

## Worked Example: Reduce a Packed bf16 Embedding Row in f32

```text
A bf16 embedding tensor lives two-bf16-per-32-bit-lane (InterleavedBf16). To reduce it in f32:

  1. WIDEN   VunpackF32(packed) -> { CastTo(f32, VunpackLowerF32), CastTo(f32, VunpackUpperF32) }
               lower: kVectorUnpack(0x109, idx0, fmt7) -> <<16   (low bf16 -> high 16 of f32)
               upper: kVectorUnpack(0x109, idx1, fmt7) -> &0xffff0000

  2. REDUCE  (JF/PF) Vsetspr -> kVectorSetSegmentPattern(0x8c) programs the per-lane boundary;
               kVectorAddSegmentReduceF32(0xfc -> VEX 0x1e) sums each segment, resetting at boundaries.
               A second reduce on the same XLU SHARES the one Vsetspr (RPU double ReplaceOperandIn).

  3. RE-PACK VpackBf16(lo, hi) -> kVectorPack(0x126, InterleavedBf16) interleaves the f32 halves
               back to one packed bf16 lane for storage.

  On Viperfish/Ghostlite step 2 has no TensorCore path: the segment reduce runs on the SparseCore.
```

---

## What Is Not Decoded

- The exact middle `VpackFormat` enum indices (the `CompressedB8/B4/B2/B1` arms and the fp8/sub-byte dequant arms 12-25) are name-confirmed from `VpackFormatString` but their per-arm byte-overlap offsets were read structurally, not byte-exactly.
- The `VpackFormatSublanesIndices` fan-in semantics for the dequant formats (12-25): the `2`/`4` table values are dumped, but whether `4` means "4 sub-byte elements per slot" vs "4-bit element width" was not separated per format.
- The per-gen VEX *bundle slot* the segment-reduce / SetSegmentPattern op lands in (`EmitVectorExtendedInstruction` schedules via `CurrentBundle`/`GetPopulatedSlots`/`FindFreeSlot`; the VEX-slot mask was read structurally).
- The `VectorExtendedOpcode` proto enum *symbolic names* per value (descriptor `0x1fa1fd00`): the VEX *values* are byte-exact but the proto member names were not pulled from the serialized FileDescriptor.
- The host-side embedding lowering that emits the `SetSegmentPattern` + `SegmentReduce` pair (how an HLO segment-sum's offsets become the segment-id pattern source).

---

## Cross-References

- [VPU (Vector-ALU) Slot](slot-vpu.md) — the VALU slot that carries the `vpack`/`vunpack` opcode immediates and the convert family
- [EUP / Transcendental Slot](slot-eup-transcendental.md) — the `AluEp` `UnpackFOp`/`PackFOp` and `SupportsBf16AluInstructions` lane-width model these LLO ops lower from
- [XLU Op Roster](xlu-op-roster.md) — the `Vsetperm`/`Vsetspr` set-pattern factories and the permute-vs-segment distinction
- [Bias-Add & Quant/Dequant Helpers](bias-quantization-helpers.md) — the fp8/int8 pack formats (12-25) and the quant pack/unpack helpers that share `set_pack_format_sublane`
- [Bundle Model](bundle-model-overview.md) — the per-generation VLIW bundle the VEX-control segment ops schedule into
