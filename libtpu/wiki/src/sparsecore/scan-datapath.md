# Scan Datapath

> *Every address, oneof tag, register-band guard, reduction-string XOR constant, struct offset, and error string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`) — from the `ScanOpLowering`/`SegmentedScanOpLowering::matchAndRewrite` bodies, the `ConsumeOneTecVexBundleInstruction` per-arm emit, the `GetVectorMask`/`GetVMDestregno` band guards, the `ScanOp`/`SegmentedScanOp::build` `addOperands` order, and `mlir::tpu::ScanOp::verify`. Addresses apply to this build; other versions differ.*

## Abstract

The SparseCore scan datapath turns a vector prefix-reduction (`sum`/`min`/`max`) into a single masked [VectorExtended](vectorextended-vex.md) bundle slot. It is the reduce stage of the embedding pipeline: rows gathered into VREGs by the [VectorLoad](vectorload-slot.md) slot are scanned in place, with per-lane participation gated by an [M-register predicate](m-register-predicate.md). The reduction primitive a reimplementer must reproduce is a *masked, hardware-segmented prefix scan* — not a software loop and not a tree reduction. The hardware consumes the mask directly: the bundle carries a 5-bit M-register *selector*, and inactive INPUT lanes contribute the reduction identity in silicon, below the binary.

The decisive structural fact is that scan predication is a **two-part datapath that the lowering keeps strictly separate**. The *in-scan mask* is carried INTO the VEX bundle as a field of the scan op itself — `proto+0x38` (the M-register index) written by every scan-emit arm, unconditionally, from MLIR `operand[1]`. It is **not** pre-applied by a `VectorSelect` before the scan. The *inactive-output disposition* — what a masked-off lane reads after the scan — is a **separate** `VectorAlu` `VectorSelect` op (`select(M, scan_result, else)`), and it draws its mask from a *different, narrower* register file (M0..M15 vs the scan's M0..M31). A third, orthogonal field is the whole-op predicate (the `Predication` submessage, a P-register) that gates the entire instruction. Three predication fields coexist on one scan bundle; conflating them mis-models the datapath.

This page documents the three layers in order: the MLIR `ScanOp` lowering (the reduction × dtype × rank switch, the `i1→i32` count-active path, the segmented variant); the ISA mask consumption (the emit body that attaches `proto+0x38`, the encoder that copies it to bundle bit `0x104`, the two M-register bands); and the verify contract (`mlir::tpu::ScanOp::verify`, the constraints a front-end must satisfy). The VEX scan opcode roster and bit positions live in [VectorExtended](vectorextended-vex.md) and [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md); the M-register predicate word layout lives in [M-Register Predicate](m-register-predicate.md); the segment-boundary operand frame lives in [Segmented Scan](segmented-scan.md) and [Segmented Add-Scan](segmented-add-scan.md). This page owns the scan *datapath*: mask consumption, the `ScanOp` lowering, and the scan-mode roster.

For reimplementation, the contract is:

- **The scan is always masked, and the mask is carried to HW, not pre-applied.** Every scan-emit arm sets `proto+0x38 = GetVectorMask(operand[1])` and `proto+0x11 |= 1` (present) unconditionally. The encoder copies `proto+0x38` to bundle bit `0x104` as a 5-bit field. There is no pre-scan `VectorSelect` masking the input — the bundle hands the hardware the M-register index and the HW gates participating lanes.
- **The reduction is decoded from a 3-char string by XOR, not a switch on an enum (in the SC dialect).** `getReductionOp()` returns a `StringRef`; the lowering tests `len==3` then `(word0 ^ K0) | (byte2 ^ K1) == 0` with `sum`=`0x7573|0x6d`, `min`=`0x696d|0x6e`, `max`=`0x616d|0x78`. The Mosaic `tpu.scan` dialect instead carries a `ReductionKindAttr` enum (`sum=0`, `max=1`, `min=2`).
- **The intrinsic is a 3-axis function: reduction × element-type × rank.** `{sum,min,max}` × `{i32,f32,i16/bf16}` → one of `tpu_{add,min,max}_scan{1xNi,1xNf}` / `tpu_*_half_scan2xN` / `tpu_{min,max}_scan2xN`. The `1xN`/`2xN` suffix is rank (`1xN`=rank-1 lane vector, `2xN`=rank-2 packed sublanes); `i`/`f`=int/float.
- **`i1` (boolean) sum is the count-active path.** An `i1` input with `sum` lowers to `tpu_mprefix` (the population-count prefix) then convert-to-`i32` and `tpu_add_scan1xNi`. An `i1` input forbids a separate mask, forbids non-`sum` reductions, and requires an `i32` output (enforced in verify).
- **Segmented scans bind the boundary as operand[1], a V read-port operand — not the M-register mask.** `SegmentedScanOp::build` adds `(data, segment)` in order; the lowering emits `tpu_*_seg_scan*` (`NOperands<2>`). There is no `i1`/`mprefix` segmented path.
- **Inactive-OUTPUT lanes are a separate `VectorSelect` `VectorAlu` op** reading a mask from the M0..M15 write/select band, distinct from the scan's M0..M31 read band.

| | |
|---|---|
| **MLIR ops** | `sparse_core::ScanOp` (`sc_tpu.scan`), `sparse_core::SegmentedScanOp`, Mosaic `tpu::ScanOp` (`tpu.scan`) |
| **SC lowering** | `ScanOpLowering::matchAndRewrite` `0x1358ab00`; `SegmentedScanOpLowering::matchAndRewrite` `0x13589d40` |
| **Build (operand order)** | `ScanOp::build` `0x145f92e0` (data, mask); `SegmentedScanOp::build` `0x145fd4a0` (data, segment) |
| **ISA mask consume** | `ConsumeOneTecVexBundleInstruction` `0x13a15ba0` — every arm: `proto+0x38 = GetVectorMask(op[1])`, `proto+0x11 \|= 1` |
| **Mask read band** | `GetVectorMask` `0x13a33320` — `[0x5f,0x7e]` = M0..M31 (32-deep), value `regno−0x5f` |
| **Mask select/write band** | `GetVMDestregno` `0x13a65b20` — `[0x5f,0x6e]` = M0..M15 (16-deep) |
| **Encoder mask field** | `proto+0x38` → bundle bit `0x104`, 5-bit (`EncodeAddScanF32` `0x1eb32380`, gen-stable glc/vfc) |
| **Post-scan select** | `EmitVectorSelect` `0x13a1e000` → `SparseCoreTecVectorAlu_VectorSelect` |
| **Verify** | `mlir::tpu::ScanOp::verify` `0x14af7460` |
| **Reduction enum** | `ReductionKindAttr`: `sum=0`, `max=1`, `min=2` (Mosaic); 3-char string (SC) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

> **NOTE — this page owns the scan *datapath*: how the mask is consumed, the `ScanOp` lowering, and the scan-mode roster.** The VEX bundle bit positions live in [VectorExtended](vectorextended-vex.md) / [VEX Mask/Dest-Port](vex-mask-destport-subopcode.md); the M-register predicate word layout in [M-Register Predicate](m-register-predicate.md); the segment-boundary operand binding in [Segmented Scan](segmented-scan.md) / [Segmented Add-Scan](segmented-add-scan.md). They are linked, not repeated.

---

## The Two-Part Predication Datapath

### Purpose

Before the layer-by-layer detail, fix the model, because it is the part a naive reimplementation gets wrong. A masked scan has two questions, and the binary answers them in two different places with two different mechanisms:

1. **Which INPUT lanes participate in the reduction?** Answered by the *in-scan mask* — an M-register index carried in the scan op's own bundle field (`proto+0x38` → bit `0x104`). The hardware reads it and lets only active lanes contribute; inactive input lanes contribute the reduction identity (0 for `add`, ±inf for `min`/`max`).
2. **What does a masked-off OUTPUT lane read after the scan?** Answered by a *separate* `VectorAlu` `VectorSelect` op emitted alongside the scan, `select(M, scan_result, else)`, drawing its mask from the narrower M0..M15 write/select file.

```text
SparseCore masked scan — the predication is TWO ops, not one
  data (VREGs from VectorLoad) ─┐
                                ▼
   ┌──────────────────────────────────────────────────────┐
   │ VEX scan slot   (e.g. AddScanS32)                      │
   │   proto+0x38 = M-reg idx (in-scan mask)  → bit0x104 5b │  ◄── gates INPUT lanes in HW
   │   FindAndEmitToUnusedPort(data)          → V read port │      (M0..M31 read band)
   │   EmitPredicationToSlot(P-reg)           → Predication │  ◄── whole-op predicate (orthogonal)
   └──────────────────────────────────────────────────────┘
                                │ scan_result
                                ▼
   ┌──────────────────────────────────────────────────────┐
   │ VEX VectorAlu  VectorSelect  (SEPARATE op)             │
   │   select(M, then=scan_result, else)      M0..M15 band  │  ◄── disposes INACTIVE OUTPUT lanes
   └──────────────────────────────────────────────────────┘
```

> **GOTCHA — do not pre-apply the mask with a `VectorSelect` before the scan.** A reader who assumes "masked scan = select the inputs, then scan" builds the wrong datapath. The mask rides INTO the scan bundle (`proto+0x38`, set unconditionally in every emit arm), and the HW gates lanes during the reduction. The `VectorSelect` that does appear is *downstream* of the scan and disposes the *output* lanes — a different M-register file (M0..M15, not M0..M31). The two are independent ops with independent mask registers.

### The three orthogonal fields

Every VEX scan bundle carries three predication-related fields. They are decoded from three different MLIR sources and written to three different proto locations:

| Field | Emitter | proto / submessage | Scope | Source |
|---|---|---|---|---|
| per-lane **VECTOR MASK** (bit `0x104`, 5b) | scan-emit arm (`op[1]`) → `GetVectorMask` (M0..M31) | `proto+0x38`, present `proto+0x11 \|= 1` | which lanes participate in the reduction | `ConsumeOneTecVexBundleInstruction` `0x13a15ba0` |
| whole-op **PREDICATE** (`Predication` submessage) | `EmitPredicationToSlot` (last MCInst operand) → `GetPregno` (P0..P14) | `Predication` submessage `proto+0x20` | gates the ENTIRE instruction | `EmitPredicationToSlot` `0x13a4a160` |
| data **READ-PORT** (V port) | `FindAndEmitToUnusedPort` (`op[2]`) → `GetVregno` | a V read-port slot field | routes the DATA value VREG | `0x13a15ba0` per arm |

> **QUIRK — the mask SELECTS a predicate register; it is not the predicate itself.** `proto+0x38` holds a 5-bit M-register *index* (`regno − 0x5f`), not a lane bitmask. The 8-byte predicate WORD that the M-register holds — `{s_start, l_start, s_end, l_end}` sub-/lane bounds, or a synthesized iota-compare — lives in the [M-Register Predicate](m-register-predicate.md) page. This page only shows that the index is carried into the bundle; the word it points at is decoded there.

### The two M-register bands

The read and write sides of the mask file are *different widths*, and the band guards prove it. `GetVectorMask` (the scan's mask *read*) accepts M0..M31; `GetVMDestregno` (the `VectorSelect`'s mask *select/write*) accepts only M0..M15:

```c
// GetVectorMask<SparsecoreVectorMask>::                       (glc 0x13a33320)
//   the in-scan per-lane mask READ — 32-deep file
if (!operand.isReg())          LogFatal("operand.isReg()");
regno = operand.getReg();
if (regno <= 0x5E)             LogFatal("regno >= llvm::TPU::M0");    // M0  = 0x5f
if (regno >= 0x7F)             LogFatal("regno <= llvm::TPU::M31");   // M31 = 0x7e
return regno - 95;             // 0x5f → index 0   ⇒ band [0x5f,0x7e] = M0..M31

// GetVMDestregno::                                            (glc 0x13a65b20)
//   the post-scan VectorSelect mask SELECT/WRITE — 16-deep file
if (regno <= 0x5E)             LogFatal("regno >= llvm::TPU::M0");
if (regno >= 0x6F)             LogFatal("regno <= llvm::TPU::M15");   // M15 = 0x6e
return regno - 95;             // band [0x5f,0x6e] = M0..M15
```

> **NOTE — the scan reads from M0..M31; the post-scan select reads from M0..M15.** A reimplementer allocating mask registers must respect both ceilings: a scan input mask may live in M16..M31, but a `VectorSelect` mask may not. The asymmetry is byte-confirmed by the two band guards above (`< 0x7F` vs `< 0x6F`).

---

## The MLIR Scan-Op Lowering

### Purpose

`ScanOpLowering` (a `ConvertToLLVM` conversion pattern) rewrites `sparse_core::ScanOp` into one SC scan intrinsic, chosen by the reduction string and the input element type. It is the single dispatch point where a generic prefix-scan becomes a concrete `tpu_*_scan*` op that the [VEX](vectorextended-vex.md) emitter later realizes as a bundle slot. The body is a flat string-XOR → element-type → rank cascade.

### Entry Point

```text
sparse_core::ScanOp  (op-name "sc_tpu.scan", AtLeastNOperands<1>, OneResult)
  └─ ScanOpLowering::matchAndRewrite              (0x1358ab00)   ── reduction × dtype × rank → intrinsic
       ├─ ScanOp::getReductionOp                  (StringRef, 3-char)
       ├─ VectorType::getElementType(operand[1])  ── the scanned element type
       ├─ tpu_mprefix::create / tpu_*_scan*::create
       └─ ReplaceWithScanIntrinsic<tpu_*_scan2xN> (0x1358c1c0 / 0x1358bd80 / 0x1358bfa0)
```

### Algorithm

The reduction string is decoded by a constant XOR over the 3 bytes — no `strcmp`, no enum. The element type is fetched from `operand[1]` (the data operand of the adaptor) and compared to the builder's canonical `i32`/`f32`/`i16`/`bf16` types. The `i1` element type is special-cased first:

```c
function ScanOpLowering_matchAndRewrite(op):       // 0x1358ab00
    elt   = getElementType(op.operand[1])           // scanned element type
    red   = op.getReductionOp()                     // StringRef, 3 chars

    // --- i1 (boolean) input: count-active path ---
    if elt == i1Type:                               // 0x1358abb1  cmp r13,r14
        if len(red) != 3 || (red ^ "sum") != 0:     // i1 allows ONLY sum
            return failure
        cnt = tpu_mprefix::create(builder, i1_input)        // 0x14731a40 — population-count prefix
        cnt = convert_to_i32_vector(cnt)
        replaceOp(op, tpu_add_scan1xNi(cnt))                // 0x146d57c0
        return success

    // --- sum: (word0 ^ 0x7573) | (byte2 ^ 0x6d) == 0 ---
    if len(red) == 3 && red == "sum":
        if   elt == i32:  emit tpu_add_scan1xNi      // 0x146d57c0
        elif elt == f32:  emit tpu_add_scan1xNf      // 0x146d4fc0
        elif elt in {i16, bf16}:
            if !HasGxcHalfScan(): emitError(
                "Currently scan add for i16 and bf16 is only supported for GXC")
            emit tpu_add_half_scan2xN                // 0x146d4400
        else: return failure

    // --- min: (word0 ^ 0x696d) | (byte2 ^ 0x6e) == 0 ---
    elif len(red) == 3 && red == "min":
        if   elt == i32:  emit tpu_min_scan1xNi      // 0x14731340
        elif elt == f32:  emit tpu_min_scan1xNf      // 0x14731180
        elif elt in {i16, bf16}:
            if !HasGxcHalfScan(): emitError(...GXC...)
            return ReplaceWithScanIntrinsic<tpu_min_scan2xN>   // 0x1358bd80
        else: return failure

    // --- max: (word0 ^ 0x616d) | (byte2 ^ 0x78) == 0 ---
    elif len(red) == 3 && red == "max":
        if   elt == i32:  emit tpu_max_scan1xNi      // 0x14730a80
        elif elt == f32:  return ReplaceWithScanIntrinsic<tpu_max_scan1xNf>   // 0x1358bfa0
        elif elt in {i16, bf16}:
            if !HasGxcHalfScan(): emitError(...GXC...)
            return ReplaceWithScanIntrinsic<tpu_max_scan2xN>   // 0x1358c1c0
        else: return failure
    else:
        return failure   // unknown reduction string
```

The XOR constants are the little-endian byte triples: `"sum"` = `s u`=`0x7573`, `m`=`0x6d`; `"min"` = `m i`=`0x696d`, `n`=`0x6e`; `"max"` = `m a`=`0x616d`, `x`=`0x78` — read directly off the `cmp`/`xor` immediates in the decompiled body (`0x1358ab00` lines around the three `getReductionOp` calls).

### The reduction × dtype × rank → intrinsic map

| reduction | input elt | → intrinsic | create / leaf @ | emit form | Confidence |
|---|---|---|---|---|---|
| `sum` | `i1` (count) | `tpu_mprefix` + `tpu_add_scan1xNi` | `0x14731a40` + `0x146d57c0` | direct | CONFIRMED |
| `sum` | `i32` | `tpu_add_scan1xNi` | `0x146d57c0` | direct | CONFIRMED |
| `sum` | `f32` | `tpu_add_scan1xNf` | `0x146d4fc0` | direct | CONFIRMED |
| `sum` | `i16`/`bf16` | `tpu_add_half_scan2xN` | `0x146d4400` | direct (GXC-gated) | CONFIRMED |
| `min` | `i32` | `tpu_min_scan1xNi` | `0x14731340` | direct | CONFIRMED |
| `min` | `f32` | `tpu_min_scan1xNf` | `0x14731180` | direct | CONFIRMED |
| `min` | `i16`/`bf16` | `tpu_min_scan2xN` | `0x1358bd80` | `ReplaceWithScanIntrinsic` (GXC-gated) | CONFIRMED |
| `max` | `i32` | `tpu_max_scan1xNi` | `0x14730a80` | direct | CONFIRMED |
| `max` | `f32` | `tpu_max_scan1xNf` | `0x1358bfa0` | `ReplaceWithScanIntrinsic` | CONFIRMED |
| `max` | `i16`/`bf16` | `tpu_max_scan2xN` | `0x1358c1c0` | `ReplaceWithScanIntrinsic` (GXC-gated) | CONFIRMED |

Naming: `1xN` = rank-1 lane vector; `2xN` / `half` = rank-2, two sublanes packed (`bf16`/16-bit with an `f32`-accumulate); `i`/`f` = int/float. `ReplaceWithScanIntrinsic<T>` is the templated path used for the `2xN` forms and `max`-`f32`; the others call `T::create` directly into the `LLVMStructType` literal. Whether the `2xN`/`half` forms map 1:1 onto the VEX `*PartialSum*` sub-opcodes or carry the accumulate dtype via the `VpackFormat` attribute is owned by [VEX](vectorextended-vex.md) / [Segmented Add-Scan](segmented-add-scan.md) — cross-linked, not re-decoded here (LOW for the exact rank-2 sub-opcode binding).

> **NOTE — i16/bf16 scan-add is gated on a target capability and emits a "GXC only" error otherwise.** The `i16`/`bf16` arms call a vtable predicate (`(**(ctx+104)+1920)(ctx+104)` in the decompile, a per-target `HasGxcHalfScan`-style query) and, when false, `emitError("Currently scan add for i16 and bf16 is only supported for GXC")` (`InFlightDiagnostic << "Currently scan add for i16 and bf16 is only supported for " << "GXC"`). A reimplementer targeting a non-GXC generation must reject half-precision scans rather than emit an intrinsic. (This gate is not in the prior `ScanOp` write-ups; CONFIRMED here from the lowering body.)

### The i1 count-active path

The `i1`-`sum` case is the SparseCore's population-count-prefix primitive — the count of active lanes up to each position, used for ragged-row offset computation in the embedding pipeline. It is the only place a scan op consumes a boolean vector:

```text
i1 vector  ──► tpu_mprefix (OneOperand)  ──► i32 vector  ──► tpu_add_scan1xNi
              (count-active prefix /                          (i32 prefix sum of the counts)
               DuplicateCount primitive)
```

`tpu_mprefix` (`0x14731a40`, trait `OneOperand`, op-name `"tpu.mprefix"`) takes the `i1` input *alone* — it has no separate mask, which is exactly why verify forbids a mask operand on `i1` inputs. The count is converted to an `i32` vector and chained into a normal `i32` add-scan. The VEX sub-opcode `tpu_mprefix` lowers to (whether it reuses `DuplicateCountInteger` or a dedicated mask-prefix encoder) is owned by [VEX](vectorextended-vex.md); LOW here.

---

## The Segmented Variant

### Purpose

`SegmentedScanOpLowering` is the embedding-`sum` lowering: a prefix scan that *resets* the running accumulator at per-sample segment boundaries, so a single scan over a packed ragged batch produces per-row sums. It reuses the identical reduction-string XOR switch but binds a second operand — the segment-boundary vector — and has **no** `i1`/`mprefix` path (a segment scan reduces data, it does not count predicate bits).

### Algorithm

```c
function SegmentedScanOpLowering_matchAndRewrite(op):   // 0x13589d40
    red = op.getReductionOp()                            // same 3-char StringRef
    elt = getElementType(op.operand[1])
    // same XOR switch: "sum"=0x7573|0x6d, "min"=0x696d|0x6e, "max"=0x616d|0x78
    switch (red, elt):
        sum / i32      -> tpu_add_seg_scan1xNi       // 0x146d5c40
        sum / f32      -> tpu_add_seg_scan1xNf       // 0x146d5a80
        sum / i16,bf16 -> tpu_add_half_seg_scan2xN   // 0x146d45c0   (GXC-gated, "...GXC" error)
        min / i32      -> tpu_min_seg_scan1xNi       // 0x14731880
        min / f32      -> tpu_min_seg_scan1xNf       // 0x147316c0
        max / i32      -> tpu_max_seg_scan1xNi       // 0x14730fc0
        max / f32      -> tpu_max_seg_scan1xNf       // 0x14730e00
        // min/max i16,bf16 → tpu_{min,max}_seg_scan2xN (roster)
        default        -> emitError / failure
```

### The boundary operand is operand[1], not the mask

The segment boundary is bound as the **second SSA operand**, and `SegmentedScanOp::build` proves the order — it issues two `addOperands` calls, data first then segment, both unconditional:

```c
// SegmentedScanOp::build(OpBuilder, OperationState, Type, Value data, Value segment, StringAttr red)
//   (0x145fd4a0)
addOperands(state, &data,    1);     // operand[0] = data
addOperands(state, &segment, 1);     // operand[1] = segment boundary
state.getOrAddProperties().reduction_op = red;   // StringAttr property
```

Contrast `ScanOp::build` (`0x145f92e0`), which guards the data operand (`if (data) addOperands(data)`) and then adds the *mask* as operand[1] — so for a *plain* scan operand[1] is the per-lane VECTOR MASK (the one that becomes `proto+0x38`), whereas for a *segmented* scan operand[1] is the SEGMENT BOUNDARY, a V-read-port value. Both are operand[1] at the SSA level, but they are routed to different bundle fields by the [VEX](vectorextended-vex.md) emitter.

> **GOTCHA — operand[1] means two different things for `ScanOp` vs `SegmentedScanOp`.** In a plain `ScanOp`, operand[1] is the M-register vector mask (→ `proto+0x38`, the in-scan predicate). In a `SegmentedScanOp`, operand[1] is the segment-id boundary (→ a V read port, register-allocated by `FindAndEmitToUnusedPort`). A reimplementer wiring the operand frame must branch on the op identity, not assume operand[1] is always the mask. The boundary operand frame and `VpackFormat` capability matrix are owned by [Segmented Scan](segmented-scan.md) / [Segmented Add-Scan](segmented-add-scan.md).

---

## The ISA Mask Consumption

### Purpose

`ConsumeOneTecVexBundleInstruction` (the glc TEC-VEX bundle emitter) converts an `MCInst` scan op into the bundle proto. Every scan arm does the same three things: construct the per-op proto submessage (a oneof tag into `proto+0x50`), attach the per-lane mask from MCInst `operand[1]`, and route the data VREG from `operand[2]` to a free V read port. The mask attach is the heart of this page — it is what makes the scan masked in hardware.

### Algorithm

The `AddScanS32` arm, byte-traced (`0x13a16ce6`..; oneof tag 6):

```c
// ConsumeOneTecVexBundleInstruction — AddScanS32 arm        (0x13a15ba0)
clear_inst(vex_proto);                                        // SparseCoreTecVectorExtended::clear_inst
vex_proto.oneof_tag = 6;                                      // [proto+0x50] = 6
sub = Arena::DefaultConstruct<...AddScanS32>(arena);          // proto+0x50 submessage
proto.scan = sub;

// --- the in-scan mask: operand[1] → M-register index, ALWAYS attached ---
sub[0x38] = GetVectorMask<SparsecoreVectorMask>(mcinst.operand[1]);   // proto+0x38 = regno - 0x5f
sub[0x11] |= 1;                                               // present flag (or [proto+0x11], 1)

// --- the data value: operand[2] → a free V read port ---
vregno = GetVregno(mcinst.operand[2]);
FindAndEmitToUnusedPort<SparsecoreVregReadPort, ...AddScanS32>(status, slot, vregno, sub);
```

The decompile shows the assignment as a plain `mov [sub+0x38], eax` followed by `or [sub+0x11], 1` — present-bit set unconditionally, in *every* scan arm. The MCInst operand fetch is `*((_QWORD*)inst+2) + 0x10` for `operand[1]` and `+0x20` for `operand[2]` (the per-operand stride is 0x10).

> **QUIRK — the mask is attached on EVERY scan arm, not just masked scans.** There is no "is this scan masked" branch in the emit body — `proto+0x38 = GetVectorMask(op[1])` and `proto+0x11 |= 1` run unconditionally for `AddScanS32`, `AddScanBf16PartialSumBf16`, `AddScanS16PartialSumS16`, the `SegmentedAddScan*` family, `DuplicateCount{Integer,Float}`, `MaxIndexScan{F32,U32}`, `MaxScanF32`, and every other scan/reduce arm. An "unmasked" scan is simply one whose mask M-register selects all lanes; the bundle field is always populated. A reimplementer who makes the mask optional at the encoder level will mis-encode every scan.

### From proto field to bundle bit

The encoder closes the loop: `proto+0x38` (the M-register index) is copied into bundle bit `0x104` as a 5-bit field. The mask arm is the *last* field the scan encoder writes, after the V read-port array:

```asm
; EncodeSparseCoreTecVectorExtendedAddScanF32          (glc 0x1eb32380), mask arm @0x1eb32470
test  byte ptr [rax+0x11], 1        ; f6 40 11 01  — mask present?
; if present:
movsxd rax, dword ptr [rax+0x38]    ; 48 63 40 38  — sign-extend the M-register index
mov   esi, 0x104                    ; be 04 01 00 00  — bundle bit 0x104
xor   ecx, ecx
mov   r8d, 5                        ; 41 b8 05      — 5-bit field
call  BitCopy
```

This arm is **byte-identical** in the vfc `EncodeSparseCoreTecVectorExtendedFloatAddScan` encoder (`0x1e9b14a0`, mask arm `0x1e9b1590`: same `be 04 01 00 00` / `41 b8 05`), so the M-register-selector-to-bundle field is gen-stable glc↔vfc. The exact bit position and the surrounding V-port array are owned by [VEX Mask/Dest-Port/Sub-Opcode](vex-mask-destport-subopcode.md); this page anchors only that `proto+0x38` is the source and bit `0x104` (5b) is the destination.

### The post-scan VectorSelect

The inactive-OUTPUT-lane disposition is a *separate* `VectorAlu` op, `SparseCoreTecVectorAlu_VectorSelect`, emitted by `EmitVectorSelect` (`0x13a1e000`). It reads three things: the `then` value (the scan result, via the X port), an `else` value, and a mask register via `GetVMDestregno` (the M0..M15 select band):

```c
// EmitVectorSelect<...VectorSelect>                          (0x13a1e000)
then_x = GetOperandAndVsEncoding(op);          // X-port = then / scan_result
GetVregno -> proto+0x18
mask   = GetVMDestregno(op);                   // M0..M15 select band → proto+0x1c
else_v = GetVregno(op);                        // → proto+0x20
proto[0x10] |= 3; proto[0x10] |= 4;            // present flags
// result lane = mask[lane] ? then[lane] : else[lane]
```

The `else` operand is the zero-vs-preserve choice: a masked-off output lane reads `else`, which is identity/zero for a fresh result or the prior value for a preserve-old reduction. The exact `else` wiring per scan family (whether index-scan writes a sentinel, whether duplicate-count zeros) is per-emitter and not exhaustively enumerated here (LOW); the `VectorSelect` op, its M0..M15 mask, and its two-VREG operand frame are CONFIRMED.

---

## The Verify Contract

### Purpose

`mlir::tpu::ScanOp::verify` (`0x14af7460`) is the front-end contract: the constraints a `tpu.scan` op must satisfy before lowering. It is the cleanest single source for the scan's typing rules — core placement, rank, element-type, the `i1` special cases, the mask shape, and the reduction enum. Verify failures emit `opError`s with the exact strings below.

### The constraints (byte-exact strings)

| # | Check | String | Decompile anchor |
|---|---|---|---|
| 1 | parent core type == 2 (SC vector subcore) | `Scan is supported only on the SC vector subcore` | `GetCoreTypeOfParentOp != 2` (line 51) |
| 2 | `i1` input → output is `i32` | `Output element type must be i32 vector for i1 vector inputs.` | `isInteger(1)` then `!isInteger(0x20)` (60/63) |
| 3 | non-`i1` input/output element types match | `Input and output element type mismatch.` | `getElementType` cmp (71/72) |
| 4 | input/output shapes match | `Input and output shape mismatch. Input shape: (` | `getShape` + `bcmp` (78–84, 163) |
| 5 | input rank 1 or 2 (reject ≥3) | `Input must be a rank 1 or 2 vector.` | rank `>= 3` (87) |
| 6 | `i1` input → reduction is `sum` (enum 0) | `Only sum reduction is supported for i1 vector inputs.` | `getValue() != 0` (101–103) |
| 7 | reduction ∈ {sum=0, max=1, min=2} | `Only sum, max and min reductions are supported.` | `getValue()` 0/1/2 chain (108–110, 153) |
| 8 | `i1` input → no mask operand | `Mask is not supported for i1 vector inputs.` | mask present + `isInteger(1)` (116–118) |
| 9 | mask is rank 1 | `Mask must be a rank 1 vector.` | mask `getShape` rank `!= 1` (124, 148) |
| 10 | mask length == input lane count | `Mask and input mismatch. Expected mask of length: …, but got …` | mask shape cmp (131–141) |

```c
function ScanOp_verify(op):                       // 0x14af7460
    if GetCoreTypeOfParentOp(op) != 2:            // SC vector subcore
        return opError("Scan is supported only on the SC vector subcore")
    in_elt  = getElementType(op.operand[0])
    out_elt = getElementType(op.result[0])
    if in_elt.isInteger(1):                        // i1 input
        if !out_elt.isInteger(32):
            return opError("Output element type must be i32 vector for i1 vector inputs.")
    else if in_elt != out_elt:
        return opError("Input and output element type mismatch.")
    if shape(in) != shape(out):                    // bcmp
        return opError("Input and output shape mismatch. Input shape: (")
    if rank(in) >= 3:
        return opError("Input must be a rank 1 or 2 vector.")
    red = op.reduction_kind                         // ReductionKindAttr enum
    if in_elt.isInteger(1) && red != 0:             // i1 ⇒ sum only
        return opError("Only sum reduction is supported for i1 vector inputs.")
    if red not in {0,1,2}:                          // sum=0, max=1, min=2
        return opError("Only sum, max and min reductions are supported.")
    if op.numOperands == 1 || op.mask == null:      // no mask → done
        return success
    if in_elt.isInteger(1):
        return opError("Mask is not supported for i1 vector inputs.")
    if rank(op.mask) != 1:
        return opError("Mask must be a rank 1 vector.")
    if shape(op.mask)[0] != shape(in)[lane_dim]:
        return opError("Mask and input mismatch. Expected mask of length: <N>, but got <M>.")
    return success
```

The reduction enum (`sum=0`, `max=1`, `min=2`) is read from the `ReductionKindAttr::getValue()` `je`/`jne` chain (lines 101/108–110) and cross-confirmed by the parse error roster (`expected ::mlir::tpu::ReductionKind to be one of: …`). The mask-presence test `numOperands == 1 || mask == null` (decompiled as `*((_DWORD*)op+17) == 1 || !*(...op+9)+56)`) is the exact gate: only when a real mask operand is present do constraints 8–10 run.

> **NOTE — verify enforces TWO mask-shape constraints the lowering does not re-check.** A mask must be rank-1 and its length must equal the input's lane count. These (`"Mask must be a rank 1 vector."`, `"Mask and input mismatch. Expected mask of length: …, but got …"`) are CONFIRMED in `verify` here and are additions to the prior mask write-ups, which listed only the `i1`-no-mask rule. A reimplementer's verifier must reject a rank-2 or mis-sized mask before lowering, because the lowering assumes a well-formed mask.

> **QUIRK — there are two scan dialects with two reduction encodings.** Mosaic `tpu.scan` (`tpu::ScanOp`, this verify) carries a `ReductionKindAttr` ENUM (`sum=0`/`max=1`/`min=2`). The SC `sc_tpu.scan` (`sparse_core::ScanOp`, the lowering above) carries a 3-char `reduction_op` STRING decoded by XOR. The enum→string bridge (the `tpu.scan → sc_tpu.scan` conversion) is a separate pattern not on this page. A reimplementer must not assume one encoding; the front-end op uses the enum, the SC lowering uses the string.

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `ScanOpLowering::matchAndRewrite` | `0x1358ab00` | reduction × dtype × rank → intrinsic; the `i1` count path | CONFIRMED |
| `SegmentedScanOpLowering::matchAndRewrite` | `0x13589d40` | segmented variant; same XOR switch, no `i1` path | CONFIRMED |
| `ScanOp::build` (StringAttr) | `0x145f92e0` | `addOperands(data)` then `addOperands(mask)` — operand[0]=data, [1]=mask | CONFIRMED |
| `ScanOp::create` | `0x145f93e0` | op-name `"sc_tpu.scan"`; calls `build` | CONFIRMED |
| `SegmentedScanOp::build` | `0x145fd4a0` | `addOperands(data)` then `addOperands(segment)` — operand[1]=boundary | CONFIRMED |
| `SegmentedScanOp::create` | `0x145fd5a0` | builds `(data, segment, reductionStr)` | CONFIRMED |
| `ConsumeOneTecVexBundleInstruction` | `0x13a15ba0` | per-arm emit; `proto+0x38 = GetVectorMask(op[1])`, `proto+0x11 \|= 1` | CONFIRMED |
| `GetVectorMask<SparsecoreVectorMask>` | `0x13a33320` | in-scan mask read; band `[0x5f,0x7e]` = M0..M31, value `regno−0x5f` | CONFIRMED |
| `GetVMDestregno` | `0x13a65b20` | `VectorSelect` mask select/write; band `[0x5f,0x6e]` = M0..M15 | CONFIRMED |
| `EmitPredicationToSlot<…VectorExtended>` | `0x13a4a160` | whole-op predicate (last MCInst operand → `Predication` submessage) | CONFIRMED |
| `EmitVectorSelect<…VectorSelect>` | `0x13a1e000` | post-scan `select(M, then, else)`; mask via `GetVMDestregno` | CONFIRMED |
| `EncodeSparseCoreTecVectorExtendedAddScanF32` | `0x1eb32380` | encoder; `proto+0x38` → bundle bit `0x104` (5b); glc | CONFIRMED |
| `EncodeSparseCoreTecVectorExtendedFloatAddScan` | `0x1e9b14a0` | vfc encoder; mask arm byte-identical to glc | CONFIRMED |
| `BroadcastBoolToVector` | `0x13d9bfa0` | `getBoolAttr(value)` → `BroadcastScalarToVector` — the scan-mask producer | CONFIRMED |
| `tpu_mprefix::create` | `0x14731a40` | `i1` population-count prefix (`OneOperand`, `"tpu.mprefix"`) | CONFIRMED |
| `mlir::tpu::ScanOp::verify` | `0x14af7460` | the typing/mask/reduction contract (10 constraints) | CONFIRMED |

> **NOTE — the prior write-ups cited `tpu::ScanOp::verify` at `0x14af7460` and that is correct; the SC-side `verifyInvariantsImpl` (`0x145f9640`) does NOT carry the constraint strings.** The byte-exact constraint roster (incl. the two NEW mask-shape rules) lives in the Mosaic `tpu::ScanOp::verify`. A reimplementer searching the SC `sparse_core::ScanOp::verifyInvariantsImpl` for the messages will not find them.

---

## Considerations

- **The scan is always masked at the ISA layer.** Encode `proto+0x38` (the M-register selector) and set `proto+0x11 |= 1` on every scan op, not conditionally. An unmasked scan selects an all-lanes M-register; it is not a different encoding.
- **Mask consumption is in-HW, not pre-select.** Do not lower a masked scan as `VectorSelect(input) → scan`. Lower it as `scan(input, mask)` with the mask carried in the bundle; the HW gates lanes and inactive inputs contribute the reduction identity. The `VectorSelect` that appears is a *separate downstream* op for the output lanes.
- **Two M-register files, two ceilings.** Scan input masks may use M0..M31 (`GetVectorMask`); post-scan `VectorSelect` masks may use only M0..M15 (`GetVMDestregno`). Allocate accordingly.
- **operand[1] is mask for `ScanOp`, boundary for `SegmentedScanOp`.** Branch on op identity; both are SSA operand[1] but route to different bundle fields.
- **`i1`-`sum` is the only boolean scan, and it is `mprefix` + `add_scan`.** It forbids a mask, forbids non-`sum`, and requires an `i32` output (verify constraints 2/6/8).
- **i16/bf16 scan-add is GXC-only.** Gate half-precision scans on the target-capability predicate and emit the "GXC only" error otherwise; do not synthesize a `*_half_scan2xN` intrinsic on a non-GXC generation.
- **Two scan dialects, two reduction encodings.** Mosaic `tpu.scan` uses `ReductionKindAttr` (`sum=0`/`max=1`/`min=2`); SC `sc_tpu.scan` uses a 3-char string (XOR-decoded). Verify operates on the enum; the SC lowering operates on the string.
- **Unmapped / LOW.** The lowest per-lane HW write-enable of the in-scan mask (physical suppress-on-masked-output vs always-materialized `VectorSelect`); the exact `else` operand per scan family in the post-scan select; the rank-2 `2xN`/`half` → VEX `*PartialSum*` sub-opcode binding; the `tpu_mprefix` → VEX-bundle sub-opcode.

---

## Related Components

| Name | Relationship |
|---|---|
| `ScanOpLowering` / `SegmentedScanOpLowering` (`0x1358ab00` / `0x13589d40`) | the reduction × dtype × rank → intrinsic switch; the segmented variant |
| `ConsumeOneTecVexBundleInstruction` (`0x13a15ba0`) | the per-arm emit that attaches the in-scan mask (`proto+0x38`) and routes the data port |
| `GetVectorMask` / `GetVMDestregno` (`0x13a33320` / `0x13a65b20`) | the M0..M31 read band vs the M0..M15 select/write band |
| `EmitVectorSelect` (`0x13a1e000`) | the separate `VectorAlu` op that disposes inactive OUTPUT lanes |
| `mlir::tpu::ScanOp::verify` (`0x14af7460`) | the 10-constraint typing/mask/reduction contract |
| `BroadcastBoolToVector` (`0x13d9bfa0`) | the all-true/all-false scan-mask producer at the MLIR layer |

## Cross-References

- [VectorExtended (VEX)](vectorextended-vex.md) — the scan/sort/reduce slot this datapath emits into; the VEX opcode roster, V read ports, and the `SourceOne` seed the scans realize.
- [VEX Mask / Dest-Port / Sub-Opcode](vex-mask-destport-subopcode.md) — the bundle bit `0x104` (5-bit mask selector), the dest read-port, and the 48-encoder sub-opcode map this page's `proto+0x38` feeds.
- [M-Register Predicate Word (M0–M31)](m-register-predicate.md) — the 8-byte predicate WORD the M-register holds (`{s_start,l_start,s_end,l_end}` / iota-compare) and the masked-scan inactive-lane output model.
- [Segmented Scan](segmented-scan.md) — the per-segment-reset scan; the `SegmentedScanOpLowering` reduction switch and the boundary-operand binding summarized here.
- [Segmented Add-Scan](segmented-add-scan.md) — the `SegmentedAddScan` operand frame and the `VpackFormat` dtype-attribute capability matrix the `2xN`/`half` forms ride.
- [VectorLoad Slot](vectorload-slot.md) — the read-side slot that fills the VREGs this datapath scans; the `SourceOne` seed selector and the segment-id operand binding documented there.
- [TEC Vector Opcode Enumeration](vector-opcode-enum.md) — the `VectorAlu` opcode roster (incl. `VectorSelect`) and the opcode-recovery model.
- [SparseCore Overview](overview.md) — the three SC engine classes and where the TEC vector scan datapath sits.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore datapath (embeddings) — [back to index](../index.md)
