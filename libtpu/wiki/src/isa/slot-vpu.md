# VPU (Vector-ALU) Slot

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text` and `.rodata` VMAs equal their file offsets. Other libtpu builds will differ.*

## Abstract

The VPU is the TensorCore's per-lane vector ALU: the engine that runs element-wise arithmetic across the architectural **8 sublanes × 128 lanes = 1024-element** vector register. In the VLIW bundle it appears as one or more `VectorAlu*` sub-bundles — the slot a reimplementer must serialize to drive vector `add`/`mul`/`min`/`max`/`shift`/`select`/`compare`/`convert`/`pack`, and to push transcendentals into the extended-unary pipeline. Unlike an SSA back end where the scheduler tracks hazards at runtime, a TPU bundle *is* the issue packet: the encoder lays each present VALU slot into the bundle byte buffer at a generation-fixed bit offset, and an absent slot is filled with the never-execute predicate. There is no per-instruction header — the opcode immediate selects both the operation and (on older generations) the vector-mask register or transcendental function.

The VPU slot is not one wire format but a family of six. Two encoder lineages exist. Jellyfish and its byte-identical variant Dragonfish (the `EncoderJf` path) pack the VALU word with direct `and`/`shl`/`or` bit twiddling into a 64-bit value that is OR-merged into the bundle. Pufferfish and every v5+ generation (Viperfish, Ghostlite, 6acc60406) drive a uniform table-driven `BitCopy(dst, dst_bit, src, src_bit, nbits)` primitive (`@0x1fa0a900`) from per-opcode `Encode<gen>VectorAluN<Op>` helpers reached through an opcode-keyed jump table. Across the lineage the slot count grows **2 → 4**, the opcode field widens **6 → 7 → 8 bits**, the per-slot predicate field *shrinks* **5 → 4 → 2 bits**, and the single XLU (transcendental) becomes a pair — every change a deliberate response to a wider compute fabric, not padding.

This page documents the slot per generation as a reimplementation target: the opcode enum and its op-family grouping; the exact bit positions of opcode / destination / two sources / Y-operand selector / predicate, all anchored to verified `BitCopy` immediates; the lane geometry; the Y-operand (source-B) selector model; the EUP/XLU push-pop protocol; the predicate and vector-mask register files; and the JF→GF evolution.

For reimplementation, the contract is:

- The two encoder families and the universal `BitCopy` bit-packing primitive — when to direct-pack and when to table-dispatch.
- The per-generation field layout: opcode, destination vreg, two source vregs, the 5-bit Y-operand selector, and the predicate field, at their exact bit offsets within the bundle.
- The `VectorAluOpcode` space per generation (6/7/8-bit; 63 / 131-op enums) grouped by family, plus the `VectorAluYEncoding` (0..31) source-B model.
- The lane geometry (8×128 universal) and the EUP/XLU push-pop, including the v5+ restriction of the push to VALU slot 3.

| | |
|---|---|
| **Slot proto** | `VectorAluInstruction` (JF/DF/PF) / `TensorCoreVectorAlu[0..3]` (v5+) |
| **Universal bit-packer** | `BitCopy(void*, int dst_bit, const void*, int src_bit, int nbits)` @ `0x1fa0a900` (`_Z7BitCopyPviPKvii`) |
| **Opcode enum** | `VectorAluOpcode` dense `0..62` (`VectorAluOpcode_descriptor` @ `0x1fa1fca0`); v5+ proto `TensorCoreVectorAlu.<Op>H` (131 ops) |
| **Y-operand enum** | `VectorAluYEncoding` dense `0..31` (`@0x1fa1fc40`) — vreg / VS0-2 / IMM0-5 / hardwired constants |
| **Lane geometry** | 8 sublanes × 128 lanes = 1024 elements / vreg (all gens) |
| **Register file** | `v0..v1023` architectural; 6-bit slot window (PF/v5+) → 64 directly addressable per slot |
| **XLU push-pop** | VALU push → EUP pipeline; `VectorResult` `PopEupResult` pop one+ bundle later; single-issue (`"1 XLU Busy"`) |

---

## The Two Encoder Families

### Purpose

A reimplementer's first decision is which encoder lineage a target generation belongs to, because the two produce mutually incompatible wire formats from the same logical VALU instruction. The lineage determines whether fields are placed by hand-rolled shifts or by a generic copy primitive, and whether each opcode has its own emitter.

### Algorithm

The Jellyfish path packs the slot inline. `EncoderJf::EncodeVectorAluInstruction` (`@0x1e864f00`) masks each field to width and shifts it to position in a 64-bit accumulator, then ORs the accumulator into the bundle buffer:

```c
function EncoderJf_EncodeVectorAluInstruction(inst, slot, bundle):  // @0x1e864f00
    if slot > 1: fatal("slot < kMaxVectorAluSlotsPerBundle")  // only 2 VALU lanes
    pred = inst.predicate & 0x1f                  // 5-bit predicate  (and 0x1f)
    op   = inst.opcode    & 0x3f                   // 6-bit opcode     (and 0x3f)
    if op >= 0x3e: error                           // opcode range 0..62 (cmp 0x3e=62)
    if op == 0x18 or IsEupOpcode(op): reserve_xlu()  // ProtoUtils::IsEupOpcode @0x1e875900
    if slot == 0:                                  // lane 0 → struct 0x1D window (abs 136..167)
        bundle[0x1D] |= (Vx  & 0x1f) << 0          // Vx  @ abs 136
                      | (op  & 0x3f) << 5          // op  @ abs 141  (the "32 *" multiply)
                      | (pred & 0x1f) << 11         // pred@ abs 147
    else:                                          // lane 1 → struct 0x16 cross-word (abs 90..127)
        word  = (yenc & 0x1f) << 10                // Y-enc @ abs 90
              | (Vx   & 0x1f) << 25                // Vx    @ abs 105 (binary-op path)
              | (op   & 0x3f) << 30                // op    @ abs 110 (shl 0x1e)
              | (pred & 0x1f) << 36                // pred  @ abs 116
              | (dst  & 0x1f) << 41                // dst   @ abs 121
        bundle[0x16] |= word                        // OR-merge into 56-bit window
    EncodeVectorAluYEncoding(inst, slot, bundle)   // @0x1e864be0 — resolves Y-operand source
```

The Pufferfish and v5+ path instead routes through a per-slot `Encode` dispatcher that jump-tables on the opcode and tail-calls a per-op helper; every field is written by `BitCopy`:

```c
function VxcTensorCoreVectorAlu0Encoder_Encode(proto, out_span):  // VF @0x1eef8a80
    op = proto.opcode                              // read at proto +0x50
    BitCopy(out, 306, &proto.predicate, 0, 4)      // 4-bit predicate @ bit 306 (mov esi,0x132)
    BitCopy(out, 299, &op,             0, 7)       // 7-bit opcode    @ bit 299 (mov esi,0x12b)
    if op >= 0x80: error                           // opcode range 0..128 (cmp 0x80=128)
    helper = jump_table[op]                        // table @ rodata 0xb84600c
    helper(out, proto)                             // e.g. EncodeTensorCoreVectorAlu0VectorFloatAdd
```

`BitCopy` itself is the single primitive every v5+ field write funnels through: `rdi`=destination buffer, `esi`=destination bit offset within the bundle, `rdx`=pointer to the source value, `ecx`=source bit (almost always 0), `r8d`=bit count. It copies `nbits` from `src[src_bit..]` to `dst[dst_bit..]`, which is why every field offset on this page appears as a `mov esi, <bit>` / `mov r8d, <width>` pair immediately before a `call 0x1fa0a900`.

### Function Map

| Function | Address | Role |
|---|---|---|
| `BitCopy(void*,int,const void*,int,int)` | `0x1fa0a900` | Universal v5+/PF bit-packer |
| `EncoderJf::EncodeVectorAluInstruction` | `0x1e864f00` | JF/DF direct-pack VALU encoder |
| `EncoderJf::EncodeVectorAluYEncoding` | `0x1e864be0` | JF Y-operand selector encode |
| `EncoderJf::EncodePredication<VectorAluInstruction>` | `0x1e864000` | JF per-slot predicate encode |
| `EncoderJf::EncodeBundleInternal` | `0x1e86c7c0` | Calls VALU encoder per present slot |
| `ProtoUtils::IsEupOpcode(VectorAluOpcode)` | `0x1e875900` | JF EUP-push classifier |
| `pxc::isa::TensorCoreVectorAlu0Encoder::Encode` | `0x1ed45060` | PF VALU0 dispatcher (struct `…Alu0`) |
| `pxc::isa::TensorCoreVectorAlu1Encoder::Encode` | `0x1ed68d80` | PF VALU1 dispatcher (struct `…Alu1`) |
| `vxc::isa::TensorCoreVectorAlu{0..3}Encoder::Encode` | `0x1eef8a80` / `0x1ef1c500` / `0x1ef3f120` / `0x1ef62880` | VF 4 VALU dispatchers (shared struct) |
| `gxc::glc::isa::TensorCoreVectorAlu0Encoder::Encode` | `0x1f250160` | Ghostlite VALU0 dispatcher |
| `gxc::gfc::isa::TensorCoreVectorAlu0Encoder::Encode` | `0x1f8b53c0` | 6acc60406 (GF) VALU0 dispatcher |
| `gxc::{glc,gfc}::isa::SparseCoreTecVectorAlu{0..2}Encoder::Encode` | `0x1eaa4880…` / `0x1ec11100…` | SparseCore TEC 3 VALU slots |

> **QUIRK —** Pufferfish gives VALU0 and VALU1 *distinct struct types* (`TensorCoreVectorAlu0` vs `TensorCoreVectorAlu1`), and VALU1 accepts a wider opcode range — `cmp rcx,0x43` (67) versus VALU0's `cmp rcx,0x3e` (62). VALU1 carries a few ops VALU0 lacks. From Viperfish onward the four slots share one `TensorCoreVectorAlu` struct and one op range, so a reimplementation can use a single encoder template; on Pufferfish it cannot.

---

## Bit-Field Layout — Per Generation

### Purpose

This is the byte-level wire format: where each field sits inside the bundle byte buffer. **All bit positions on this page are LSB-first** — bit 0 is the least-significant bit of byte 0, matching the convention used throughout [Bundle Model](bundle-model-overview.md) and enforced by the `BitCopy` packer (which writes `nbits` upward from the LSB-numbered `dst_bit`). There is no MSB-first ordering anywhere in the encode path. All v5+ offsets below were read directly from the `BitCopy` `mov esi`/`mov r8d` immediates in the representative `VectorFloatAdd` / `VectorF32Add` helpers; the Jellyfish offsets from the `and`/`shl` immediates in `EncodeVectorAluInstruction`.

### Encoding

The encoder reads a uniform set of struct fields regardless of generation. The VALU instruction's opcode lives at proto `+0x50`; the operand descriptor (destination / sources / Y-encoding) hangs off `+0x48`; the per-slot predicate is its own field.

```c
// Field reads (proto offsets, consistent across the v5+ helpers):
proto +0x50 : VectorAluOpcode   (the op immediate)
proto +0x48 : operand descriptor → { dst vreg, src0 vreg, Y-encoding, src1 vreg }
            : per-slot predicate field source
```

**Jellyfish / Dragonfish** (41-byte TC bundle, `EncoderJf::EncodeVectorAluInstruction` @ `0x1e864f00`). Direct `and`/`shl`/`or`, *not* `BitCopy`. The two VALU lanes occupy two *separate* windows in the 328-bit bundle, not a single repeated stride: lane 0 (`slot == 0`) packs into the struct-`0x1D` window (absolute bits 136..167), lane 1 (`slot == 1`) into the struct-`0x16` cross-word window (absolute bits 90..127). Within each lane the fields are placed by the literal shift constants, and because the two windows have different origins the per-field absolute bits differ by lane (the raw shifts share a relative layout). The opcode is masked `and 0x3f` (6-bit, range 0..62 with a `cmp 0x3e` guard) and the predicate `and 0x1f` (5-bit); register and Y-encoding fields are 5-bit windows. Dragonfish shares `EncoderJf` and `JellyfishCodecMetadata`, so it is byte-identical. The cross-checked absolute positions (LSB-first, also tabulated in [Jellyfish 41-bit Bundle](bundle-jf-41b.md)):

| Field | Width | Raw shift | Lane 0 bit (struct `0x1D`) | Lane 1 bit (window `0x16`) |
|---|---|---|---|---|
| Y-encoding (src1 vreg) | 5-bit | `<< 10` | — (slot-3 path) | 90 |
| Vx (src0 vreg) | 5-bit | `0` / `<< 25` | 136 | 105 |
| opcode | 6-bit | `<< 5` / `<< 30` (`shl 0x1e`) | 141 | 110 |
| predicate | 5-bit | `<< 11` / `<< 36` | 147 | 116 |
| dst vreg | 5-bit | `<< 41` | (in `0x1D` tail) | 121 |

> **GOTCHA — JF VALU is two distinct windows, not a stride.** Lane 0 writes byte `0x1D`/qword2; lane 1 writes the 56-bit cross-word at byte `0x16` (assembled as `dword[0x16] | word[0x1A]<<32 | byte[0x1C]<<48`). A reimplementation that derives lane 1 by adding a fixed offset to lane 0 — as the v5+ slots permit — will mis-place every lane-1 field. The shift constants in `EncodeVectorAluInstruction` are relative to each lane's window origin (136 for lane 0, 80 for lane 1), which is why the same logical field lands at, e.g., opcode bit 141 in lane 0 and bit 110 in lane 1.

**Pufferfish** (51-byte TC bundle). VALU0 `Encode` @ `0x1ed45060`, VALU1 @ `0x1ed68d80`. `BitCopy`-driven, 6-bit register fields (64-window).

| Field | Width | VALU0 bit | VALU1 bit |
|---|---|---|---|
| predicate | 5-bit | 236 (`0xec`) | 193 (`0xc1`) |
| opcode range | 6-bit | `cmp 0x3e` (0..62) | `cmp 0x43` (0..67) |
| dst vreg | 6-bit | 230 (`0xe6`) | — |
| immediate (per-imm-op) | 16-bit each | 272 / 288 / 304 / 320 / 338 | — |
| NOP fill | — | predicate ← `0x1f` (`kNeverExecute`) | same |

**Viperfish** (64-byte TC bundle). Four slots, shared struct, **34-bit per-slot stride**. VALU0 `Encode` @ `0x1eef8a80`; representative `VectorFloatAdd` helper (op `0x0c`) @ `0x1eefa2c0`:

| Field | Width | VALU0 bit | Source |
|---|---|---|---|
| opcode | 7-bit | 299 (`0x12b`) | `mov esi,0x12b; r8d,7` |
| dst vreg | 6-bit | 276 (`0x114`) | `mov esi,0x114; r8d,6` |
| src vreg | 6-bit | 282 (`0x11a`) | `mov esi,0x11a; r8d,6` |
| src vreg | 6-bit | 293 (`0x125`) | `mov esi,0x125; r8d,6` |
| Y-encoding | 5-bit | 288 (`0x120`) | `mov esi,0x120; r8d,5` |
| predicate | 4-bit | 306 (`0x132`) | `mov esi,0x132; r8d,4` |
| opcode range | — | `cmp 0x80` (0..128) | dispatcher |

The four predicate fields sit at bits 306 / 272 / 238 / 204 (VALU0..3), a uniform −34-bit step, so the slots occupy `{opcode 7 + dst 6 + 2 src 6 + Y-enc 5 + pred 4} = 34` bits each in the upper third of the 512-bit bundle.

**Ghostlite** (64-byte TC bundle). VALU0 `Encode` @ `0x1f250160`: predicate `BitCopy(…,309,…,4)` (`0x135`), opcode `BitCopy(…,302,…,7)` (`0x12e`), dispatch `cmp 0x83` (0..131). 6-bit register fields, same template as Viperfish shifted +3 bits by the widened slot start.

**6acc60406 (GF)** (64-byte TC bundle). VALU0 `Encode` @ `0x1f8b53c0`; representative `VectorF32Add` helper @ `0x1f8b7860`:

| Field | Width | VALU0 bit | Source |
|---|---|---|---|
| opcode | **8-bit** | 293 (`0x125`) | `mov esi,0x125; r8d,8` |
| dst vreg | 6-bit | 276 (`0x114`) | `mov esi,0x114; r8d,6` |
| src0 vreg | 6-bit | 270 (`0x10e`) | `mov esi,0x10e; r8d,6` |
| src1 vreg | 6-bit | 287 (`0x11f`) | `mov esi,0x11f; r8d,6` |
| Y-encoding | 5-bit | 282 (`0x11a`) | `mov esi,0x11a; r8d,5` |
| predicate | **2-bit** | 301 (`0x12d`) | `mov esi,0x12d; r8d,2` |
| opcode range | — | `cmp 0x83` (0..131) | dispatcher |

> **GOTCHA —** 6acc60406 (GF)'s predicate field is only 2 bits, which is *not* enough to name one of 16 predicate registers. It selects among `{pred_0, pred_1, always, never}`: the bundle's two active dual predicates are written by the dedicated `TensorCorePredicates` slot, and the VALU slot merely picks which of the two applies. A reimplementation that treats the 2-bit field as a 4-register index will mis-predicate every GF VALU op. See [Predicate Slot](slot-predicate.md).

### Per-Generation Slot Position

| Gen | Bundle | #VALU | Lane-0 opcode bit | Lane-0 pred bit | Pred width | Per-slot stride |
|---|---|---|---|---|---|---|
| Jellyfish (v2) | 41 B | 2 | 141 (lane 1 op @110) | 147 (lane 1 @116) | 5-bit | two windows (136 / 80) |
| Dragonfish (v3 var) | 41 B | 2 | alias of Jellyfish | alias of Jellyfish | 5-bit | two windows (136 / 80) |
| Pufferfish (v4) | 51 B | 2 | (switch-dispatched) | 236; lane 1 @193 | 5-bit | distinct structs |
| Viperfish (v5p) | 64 B | 4 | 299 | 306 | 4-bit | 34 bits/slot |
| Ghostlite (v6e) | 64 B | 4 | 302 | 309 | 4-bit | ~34 bits/slot |
| 6acc60406 (TPU7x) | 64 B | 4 | 293 | 301 | 2-bit | ~34 bits/slot |
| SparseCore TEC | 64 B | 3 | `SparseCoreTecVectorAlu0..2` (same template, SC bundle) | — | 4/2-bit | not leaf-decoded |

The generation-to-codename mapping is fixed by the codec-metadata table (see [Bundle Model](bundle-model-overview.md)): `kJellyfish`=v2, `kDragonfish`=v3, `kPufferfish`=v4, `kViperfish`=v5p, `kGhostlite`=v6e, `k6acc60406`=TPU7x. The binary namespaces follow as `jellyfish` (JF/DF, shared proto), `pxc` (PF), `vxc` (VF), `gxc::glc` (GL), `gxc::gfc` (GF).

---

## VectorAlu Opcode Enum — by Family

### Purpose

The opcode immediate selects the operation. There are two naming generations of the enum: the dense Jellyfish `VectorAluOpcode` (`0..62`, 63 values) and the v5+ `TensorCoreVectorAlu.<Op>H` proto-message set (131 ops on Viperfish, 132 on Ghostlite/6acc60406). Both span the same logical repertoire — only the dtype split and the Vmsk handling differ. The list is grouped here by family rather than dumped flat; the SparseCore TEC 142-op enumeration (a finer-grained dtype split of the same families) is byte-exact in the source and summarized at the end of this section.

### Encoding

The proto enum descriptors and their dense ranges are recoverable from the mangled `NameOfDenseEnum` template instantiations:

```text
NameOfDenseEnum<VectorAluOpcode,      0, 62>  @ 0x22331a58   → 63 op values
NameOfDenseEnum<VectorAluYEncoding,   0, 31>  @ 0x223dff40   → 32 Y-selector values
NameOfDenseEnum<VectorExtendedOpcode, 0, 34>  @ 0x2239bce8   → 35 EUP/MXU staging ops
NameOfDenseEnum<VectorResultOpcode,   0,  2>  @ 0x2239bd00   →  3 pop ops
```

The v5+ op families, grouped (representative proto names; `H` suffix = the v5+ proto-message naming):

| Family | Ops (representative) | Notes |
|---|---|---|
| Float arithmetic | `VectorFloatAdd`, `Subtract`, `Multiply`, `Max`, `Min` | `f32`/`bf16` lanes |
| Float compare | `FloatEq`/`Neq`/`Gt`/`Gte`/`Lt`/`Lte`, `TotalLt`/`TotalLte`, `InfOrNan` | produce a vector mask |
| Integer arithmetic | `IntegerAdd`/`Subtract`/`Multiply`/`Carry` | `s16`/`s32`/`u16`/`u32` |
| Integer compare | `IntegerEq`/`Neq`/`Gt`/`Gte`/`Lt`/`Lte` | produce a vector mask |
| Bitwise | `BitwiseAnd`/`Or`/`Xor` | |
| Shift | `LogicalShiftLeft`/`Right`, `ArithmeticShiftRight` | |
| Move / misc | `Move`, `Clamp`, `Classify`, `Relux`, `Ceiling`, `Floor` | |
| Bit count | `CountLeadingZeros`, `PopulationCount`, `ByteNez` | |
| Convert | `ConvertF32To{Bf16,Bf8,Hf16,If8,Int32}[Stochastic]`, `ConvertInt32ToF32` | + FP8/FP4 narrow, stochastic round |
| Mask gen | `CreateMask`, `LaneId` | |
| Select | `VectorSelectVmsk0..15`, `VectorSelectNotVmsk0..15` (PF/VF) / `VectorSelect`+`VectorSelectNot` (GL/GF) | consume a Vmsk |
| Transcendental (EUP push) | `Reciprocal`, `ReciprocalSqrt`, `Tanh`, `ShiftedSigmoid`, `LogTwo`, `PowTwo`, `EupPush` | issued into the XLU |

> **QUIRK —** the 32 `VectorSelect[Not]Vmsk0..15` entries on Pufferfish and Viperfish are *not* 32 distinct operations — they are one select op whose mask-register index (`Vmsk0..15`) is baked into the opcode. Ghostlite and 6acc60406 consolidate them into a single `VectorSelect` / `VectorSelectNot` opcode with the `Vmsk` index moved to a separate field (width not measured; likely 4-bit for 16 masks — LOW). A reimplementation must know which scheme a generation uses or it will either explode the opcode space or fail to find the mask index.

The LLO IR mnemonics that lower onto these opcodes (geometry suffix `.8x128` = native vreg) confirm the repertoire in `.rodata`: `vadd.8x128.{f32,bf16,s32,s16}`, `vmul.8x128.{f32,bf16,u32,u16}` (+ the wide `vmul.u32.u64` slot-pair), `vand`/`vor`/`vxor`/`vandn.8x128.u32`, `vshll`/`vshra`/`vshrl`, `vcmp.{f32,f64}`, `vsel.8x128`, the `.xlane` tree-reduce family, the `vcvt.*` convert set (including `.sr` stochastic-round and FP8/FP4 narrow types), the `vpack`/`vunpack` sub-byte family, and the transcendental `vrsqrt`/`vrcp`/`vtanh`/`verf`/`vsinq`/`vcosq`/`vpow` with matching `.pop` forms.

> **NOTE —** the SparseCore TEC vector ALU runs the same family taxonomy at a much finer dtype granularity: the Ghostlite TEC consumer enumerates **142 ops** (92 integer/float core, 18 transcendental as 9 families × `{f32,bf16}`, 32 quant pack/unpack), versus the Viperfish TEC's 95 (dtype-merged ops, no `cosq`/`erf`/`sinq`). The split — `tanh` → `tanh_f32` + `tanh_bf16`, `unpack_*_sublanes_*` → `unpack_compressed_*_lanes_*_to_*` — is why the SC TEC table is larger than the TensorCore proto enum, and is documented per-opcode in the SparseCore router analysis rather than reproduced here.

---

## Lane Geometry and the Operand Register File

### Purpose

Every VALU op operates on a full vector register; the geometry is what a reimplementer must replicate to lay out vregs and reason about sub-byte packing.

### Encoding

The native vreg is **8 sublanes × 128 lanes = 1024 elements**, universal across all generations (the `.8x128` mnemonic suffix; HAL access is per-`(sublane, lane)`, e.g. `ReadVectorRegister(core, sublane, lane)` @ `0x0e755c20`). Sub-32-bit types pack within the lane:

```text
32-bit element :  8 × 128            = 1024 elements   (.8x128)
16-bit element :  8 × 128 × 2        (bf16/s16/hf16, two per 32-bit lane;  .8x128x2)
8-bit  element :  8 × 128 × 4        (s8/u8/e4m3/e5m2, four per lane;       .8x128x4)
4-bit / fp4    :  8 × 128 × 4 with sub-element packing
                 ("32x16x128 only supports fp4 element types")
```

V5+ additionally exposes wider physical layouts (`16x128`, `8x256`, `4x8x128`, `8x8x128`, twisted/untwisted) for the layout-inference pass; these are multi-vreg *tile aggregations*, not a change to the per-instruction 1024-lane count.

The architectural register file is `v0..v1023` (1024 names in `TPURegStrings`). A VALU slot encodes a destination plus up to three source vregs, each a **6-bit field on Pufferfish and v5+** (5-bit register-class window on Jellyfish). Six bits address 64 registers directly, so each slot sees a *window* into the 1024-name file; the per-subtarget `getVyEncodings(unsigned)` map translates an architectural vreg into the slot encoding (returning `0xffffffff` when a vreg is not encodable in that slot's window):

| Subtarget | `getVyEncodings` |
|---|---|
| `TPUBcSubtarget` (PF BarnaCore) | `0x13c58de0` |
| `TPUVfcSubtarget` (Viperfish) | `0x13c5ec20` |
| `TPUGlcSubtarget` (Ghostlite) | `0x13c60a20` |
| `TPUGfcSubtarget` (6acc60406) | `0x13c625a0` |

> **NOTE —** the window-map contents (which architectural vreg maps to which 6-bit slot code) were not dumped; the lookup shape and the `-1` sentinel are confirmed, the per-entry table is not. A reimplementer must recover the window assignment per generation before encoding real programs.

---

## The Y-Operand (Source-B) Selector

### Purpose

A binary VALU op's second source is not a plain vreg field — it is chosen by a 5-bit `VectorAluYEncoding` value that can name a vreg, a shared vector-source read port, an immediate slot, or a hardwired constant. This is the mechanism that lets common scale/bias arithmetic avoid burning an immediate slot.

### Encoding

The field is 5-bit (`VectorAluYEncoding` dense `0..31`, `@0x1fa1fc40`), `BitCopy`'d from operand-descriptor `+0x20`. The 32 values (confirmed verbatim from `.rodata` `VECTOR_ALU_Y_*` strings):

| Group | Values | Meaning |
|---|---|---|
| Vreg | `VREG` | explicit vector register (uses the src1 vreg field) |
| VS ports | `VS0`, `VS1`, `VS2` | bundle-shared vector-source read port 0/1/2 (4 ports on v5+) |
| Float constants | `FLOAT_ONE`, `FLOAT_TWO`, `FLOAT_NEGATIVE_ONE`, `FLOAT_ZERO_POINT_FIVE` | hardwired `1.0`/`2.0`/`-1.0`/`0.5` |
| Integer constants | `INTEGER_ONE`, `INTEGER_NEGATIVE_ONE`, `ZERO` | hardwired |
| Immediate slots | `IMM0_ZERO`..`IMM5_ZERO`, `ZERO_IMM0`..`ZERO_IMM5`, `ONES_IMM0`..`ONES_IMM5` | reference bundle imm slots 0..5 with zero/ones extension |
| Paired-slot wide | `IMM1_IMM0`, `IMM3_IMM2`, `IMM5_IMM4` | two imm slots fused into a wide immediate |

The VS ports are a scarce bundle resource: multiple VALU slots in one bundle share a small number of read ports, which the packer's `SlotTracker` counts as a bundling constraint. The hardwired float constants are why scaling and bias ops appear with no immediate slot consumed.

---

## XLU (Transcendental) Push-Pop

### Purpose

Transcendentals — `rsqrt`, `rcp`, `tanh`, `sigmoid`, `log2`, `pow2`, and the mnemonic-pool `erf`/`sin`/`cos`/`pow` — do not complete inside the VALU slot. They are *pushed* into the Extended-Unary Pipeline (XLU) from a VALU slot and *popped* one or more bundles later from a `VectorResult` slot. A reimplementer must model this as a two-instruction protocol with a structural single-issue hazard, not as a single-cycle op.

### Algorithm

```c
// STAGE 1 — PUSH (issued from a VALU slot)
//   VALU op = the XLU push.  Proto names: ReciprocalH / ReciprocalSqrtH /
//   TanhH / ShiftedSigmoidH / LogTwoH / PowTwoH / EupPushH (generic push).
//   IsEupOpcode(op) @0x1e875900 classifies which opcodes are pushes so the
//   packer reserves the XLU resource for this bundle.
//
// STAGE 2 — POP (issued one+ bundle later from a VectorResult slot)
//   VectorResult op PopEupResultH reads the XLU result into a dest vreg.
//   The .pop mnemonic suffix (vrsqrt.f32.pop, vrcp.bf16.pop, …) is this pop.
```

On Viperfish the push is restricted to **VALU slot 3**: the only EUP-push helper is `EncodeTensorCoreVectorAlu3EupPush` (`@0x1ef6e400`, `vxc` anonymous namespace), which places a 7-bit opcode at bit 197 (`0xc5`), a 5-bit function selector at bit 186 (`0xba`), and a 6-bit source at bit 191 (`0xbf`). Because the XLU is single-issue, only `Alu3` (not `Alu0/1/2`) sources a push; the transcendental helpers exist only in the `Alu3` set.

The XLU is single-issue hardware — the diagnostic string `"1 XLU Busy"` is present in `.rodata`, and the bundle cost model assigns the XLU to overlap-blended resources so the pipeline drain is charged as ~50% residual overlap. Jellyfish has 1 XLU; the v5+ generations have 2 (the packer's `AddXluRequirements` reserves accordingly).

> **NOTE —** the separate `VectorExtendedOpcode` enum (dense `0..34`, 35 ops, `@0x1fa1fd00`) is the *MXU / matmul staging* path — `MatrixMultiply<fmt>`, `LoadMatrixRegister{Gmr,Lmr}`, `LaneBroadcast`, `LaneRotate`, `LoadStagingUpperBlock`. It is distinct from the VALU transcendental push but shares the same EUP pipeline and the same `PopEupResult` pop. See [MXU Slot](slot-mxu.md).

### Function Map

| Function | Address | Role |
|---|---|---|
| `vxc::isa::EncodeTensorCoreVectorAlu3EupPush` | `0x1ef6e400` | VF EUP push (Alu3 only) |
| `jellyfish::isa::ProtoUtils::IsEupOpcode` | `0x1e875900` | classifies EUP-push opcodes |
| `proto::Arena::DefaultConstruct<gxc::glc::isa::TensorCoreVectorAlu_EupPush>` | `0x1fb49c00` | Ghostlite EUP push proto |
| `proto::Arena::DefaultConstruct<…TensorCoreVectorResult_PopEupResult>` | `0x1fb55b40` (glc) / `0x1fb9e660` (gfc) | result pop proto |
| `proto::Arena::DefaultConstruct<pxc::isa::TensorCoreVectorResult{0,1}_PopEupResult>` | `0x1fa86240` / `0x1fa86ac0` | PF dual result pop |

---

## Predicate and Vector-Mask Register Files

### Purpose

Two independent register files touch the VALU slot and are easy to conflate. The **predicate** field gates whether the slot's op executes; the **vector mask** (`Vmsk`) is the data operand of conditional select. They are separate files.

### Encoding

Per-slot predicate field width and the predicate register count:

| Gen | Pred width | VALU0 pred bit | Semantics | Pred regs |
|---|---|---|---|---|
| Jellyfish | 5-bit | (packed word) | 0..14 reg, 15 = always, 31 = never | 15 |
| Pufferfish | 5-bit | 236 (V0) / 193 (V1) | same | 15 (16 on BarnaCore) |
| Viperfish | 4-bit | 306 | 1 of 16 pred regs | 16 |
| Ghostlite | 4-bit | 309 | 1 of 16 pred regs | 16 |
| 6acc60406 | 2-bit | 301 | `{pred_0, pred_1, always, never}` | 16 |

The 16-register count for the v5+ subtargets is a confirmed inline constant — `getNumPredicateRegisters` returns `mov eax,0x10; ret` for `TPUVfcSubtarget` (`@0x13c5f6e0`), `TPUGlcSubtarget` (`@0x13c615c0`), `TPUGfcSubtarget` (`@0x13c630e0`), and `TPUBcSubtarget` (`@0x13c59780`). Jellyfish/Pufferfish-TC use the base `TPUSubtarget` count of 15.

The vector-mask file is **16 registers** (`Vmsk0..15`), distinct from the predicate file. Compare ops produce a `Vmsk`; `VectorSelect` consumes one. The select opcode/field split per generation is described in the opcode-enum section above.

> **NOTE —** 6acc60406 (GF)'s narrow 2-bit predicate works only because the full per-bundle predicate-register write was moved out of the VALU slot into the dedicated `TensorCorePredicates` slot. The VALU slot picks which of the two pre-written dual predicates applies. The exact 2-bit value-to-meaning mapping (`0=pred_0`? `3=never`?) was not decoded — LOW.

---

## JF → GF Evolution

The lineage is a coherent story of a widening compute fabric, not arbitrary per-generation churn:

| Axis | JF (v2) | PF (v4) | VF (v5p) | GL (v6e) | GF (TPU7x) |
|---|---|---|---|---|---|
| VALU slots | 2 | 2 (distinct structs) | 4 | 4 | 4 |
| Encoder | direct `and`/`shl`/`or` | `BitCopy` | `BitCopy` | `BitCopy` | `BitCopy` |
| Slot struct | shared proto, two windows (136 / 80) | `Alu0` ≠ `Alu1` | shared | shared | shared |
| Opcode bits | 6 (0..62) | 6 (V0 0..62 / V1 0..67) | 7 (0..128) | 7 (0..131) | **8** (0..131) |
| Register field | 5-bit window | 6-bit | 6-bit | 6-bit | 6-bit |
| Y-encoding | 5-bit | 5-bit | 5-bit | 5-bit | 5-bit |
| Predicate field | 5-bit | 5-bit | 4-bit | 4-bit | **2-bit (dual)** |
| Predicate regs | 15 | 15 (16 BC) | 16 | 16 | 16 |
| Vmsk select | per-Vmsk opcode | per-Vmsk opcode | per-Vmsk opcode | single op + field | single op + field |
| XLUs | 1 | 1 | 2 | 2 | 2 |

The narrative: Pufferfish keeps two slots but switches to the table-driven encoder and 64-window registers, splits the two slots into distinct structs (VALU1 wider), and adds the `vmul.u32.u64` slot-pair wide multiply. Viperfish doubles to four slots, widens the opcode to 7 bits to admit the FP8/FP4 convert + stochastic-round + sublane pack/unpack set, narrows the predicate to 4 bits, and adds a second XLU. Ghostlite folds the 32 per-`Vmsk` select opcodes into one op plus a mask field and splits reciprocal by dtype. 6acc60406 (GF) widens the opcode to 8 bits (headroom; current max 131) and shrinks the per-slot predicate to a 2-bit dual-predicate selector, having moved the predicate-register write into a dedicated slot.

> **GOTCHA —** the EUP push is slot-3-specific on v5+, not slot-agnostic. The Viperfish encoder exposes the push helper only as `EncodeTensorCoreVectorAlu3EupPush` (slot 3); the single-issue XLU is sourced exclusively from `Alu3`.

---

## What Is Not Decoded

- The leaf per-opcode sub-field offsets for *every* PF/VF/GL/GF VALU op. The binary-ALU / select / convert / pack-unpack / EUP-push templates were decoded to exact offsets; the remaining ops reuse the same template with only the opcode immediate changing, but the special forms (sublane-masked pack/unpack, the `vmul.u32.u64` pair, `CreateMask`, `LaneId`) carry extra sub-fields not enumerated op-by-op.
- The index-ordered `VectorAluOpcode` value→name array (the descriptor and dense `0..62` range are located; per-index names were not walked).
- The v5+ 131-op enum value→name mapping (the op names are a confirmed set; only a few opcode immediates, e.g. `VectorFloatAdd = 0x0c`, are index-confirmed).
- The SparseCore TEC `VectorAlu0..2` leaf bit layout (encoders exist; per-slot bit offsets within the SC TEC bundle not individually decoded).
- The per-generation `getVyEncodings` window-map contents.
- The Vmsk-index field width on Ghostlite/6acc60406 (inferred ~4-bit).
- 6acc60406 (GF)'s 2-bit dual-predicate value semantics.

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW bundle the VALU slots pack into; per-gen byte widths and codec metadata
- [Viperfish 64-bit Bundle](bundle-vf-64b.md) — the absolute VALU bit positions in context, plus the Alu3 EUP push
- [Jellyfish 41-bit Bundle](bundle-jf-41b.md) — the direct-pack VALU encoder in the full JF bundle
- [Pufferfish 51-bit Bundle](bundle-pf-51b.md) — the distinct `Alu0`/`Alu1` slot structs in context
- [Ghostlite Bundle](bundle-gl.md) — the v6e VALU slot and the consolidated select op
- [EUP / Transcendental Slot](slot-eup-transcendental.md) — the XLU push-pop pipeline and result pop
- [MXU Slot](slot-mxu.md) — the `VectorExtended` matmul-staging path that shares the EUP and `PopEupResult`
- [Predicate Slot](slot-predicate.md) — the predicate register file and 6acc60406 (GF)'s dual-predicate slot
- [VCreate / Mask / Mregister](slot-vcreate-mask-mregister.md) — the 16 `Vmsk` vector-mask registers consumed by select
- [Pack/Unpack Precision](pack-unpack-precision.md) — the sub-byte convert and pack/unpack family semantics
- [LLO Opcode Enum](llo-opcode-enum.md) — the LLO IR opcodes that lower onto the VALU slot
- [MC Emitter](mc-emitter.md) — the LLVM MC `VADD*`/`VMUL*`/`VSEL*` masked mnemonics that lower to this encoding
- [Performance / Cost Model](../cost/performance-overview.md) — VALU op throughput and the single-issue XLU overlap blend
