# EUP / Transcendental Slot

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA minus `0x200000` equals its file offset. Other libtpu builds will differ.*

## Abstract

The EUP — Extended Unary Pipeline, libtpu's name for the transcendental unit and equivalent to the "XLU" of the VPU page — is the TensorCore's hardware approximator for the nine vector functions a softmax/attention/activation workload actually needs: `tanh`, `pow2` (2^x), `log2`, `reciprocal`, `rsqrt`, `shifted-sigmoid`, `sinq`, `cosq`, and `erf`. It is **not** an ALU opcode that produces a result in the same bundle. It is a deep, FIFO-buffered pipeline driven by a *push* in one bundle and drained by a *pop* one or more bundles later, with the push restricted to **VALU slot 3** on every v5+ generation. The intervening bundles are where the compiler hides the EUP's latency with VALU correction arithmetic — the same software-pipelining trick a CPU back end uses for a long-latency divide, but exposed here in the instruction encoding because a TPU bundle *is* the issue packet and the scheduler has no runtime hazard interlock to lean on.

The slot has two halves a reimplementer must reproduce separately. The **encoding** half: the push is a `TensorCoreVectorAlu3` op whose 8-bit VALU-opcode field selects the EUP-push family (value `0`) and whose 5-bit *function selector* picks the transcendental; the pop is a `VectorResult`-slot op (`PopEupResult`) that names a destination vreg. The **numeric/timing** half: which functions the hardware computes directly versus which the compiler emits as a VALU polynomial fallback; the Payne-Hanek 2/π range reduction that the trig functions need before they can push; the Newton and rational-minimax *correction* coefficients that refine or replace the raw EUP result; and the per-generation push→pop latency that the FIFO imposes and the correction window must fill. Three generations route the push→pop latency through a per-instruction heap array (`Pufferfish` = 7, `Viperfish` = 6, `Ghostlite` = 13 for F32 / 14 for BF16); the legacy `Jellyfish`/`Dragonfish` path clamps it to a flat 4.

A counter-intuitive fact frames the rest: on an EUP-capable generation, the `V*Decomposed` builder that lowers a transcendental emits a **bare push + bare pop and nothing else** — the hardware transcendental is taken directly. Every polynomial coefficient on this page lives in one of two other places: the `*NoEupF32` *software fallbacks* used when a generation or datatype lacks the hardware EUP, and the shared Newton/range-reduction helpers (`recip`/`rsqrt` refinement, `tanh` rational, `sin`/`cos` Payne-Hanek) that a few accuracy-sensitive functions still wrap around the push. The page is organized to keep that split sharp.

For reimplementation, the contract is:

- **The push encoding.** The `Alu3` VALU-opcode field (8-bit on glc/gfc, 7-bit on vxc) selecting the EUP-push family, the 5-bit function selector and its full F32/BF16 value map, and the source-vreg field — at their exact bundle bit offsets.
- **The pop encoding.** The `VectorResult` `PopEupResult` op and its destination-vreg field, and the `kVectorEupResultValue` (0x14e) LLO opcode that is hardcoded into the pop builder.
- **The fused-vs-split duality.** The MLIR `tpu_*_macro` lowering to a fused `kVector*AndPop` pseudo-op (0x13b..0x14d), and the `LloLateDecomposer` rewrite into bare push (0x128..0x13a) + deferred pop (0x14e) + interleaved VALU correction, gated by `HasEupRestrictions`.
- **The lane-width / unpack model.** `SupportsBf16AluInstructions` as the 16-vs-32-bit lane sub-element selector, and the recursive `UnpackOperand` halving that determines the 1:N AluEp fan-out when a packed sub-lane vector must be staged before the per-piece EUP op.
- **The Payne-Hanek 2/π table** and the `VcomposeF32(π/2)` reconstruction for trig argument reduction.
- **The correction-polynomial coefficients** (`tanh` rational, `recip`/`rsqrt` Newton, the `*NoEupF32` fallbacks) and which path consumes each.
- **The per-generation push→pop latency** and its orthogonality to the `VectorEupReservationCycles` issue rate; the `EupResultFifoEntry` runtime-list FIFO model.

| | |
|---|---|
| **Push slot** | VALU slot 3 (`TensorCoreVectorAlu3`); single EUP-push helper per gen, no `Alu0/1/2` EUP |
| **Push LLO opcodes** | `0x128`..`0x13a` (bare) — 9 functions × {F32, BF16}; `0x131` push-form erf |
| **Pop LLO opcode** | `0x14e` `kVectorEupResultValue` (hardcoded in `CreateVectorEupResult` @ `0x1d4d9820`) |
| **Fused pseudo-op** | `0x13b`..`0x14d` `kVector*AndPop` (the MLIR-emitted form, split by the late decomposer) |
| **Function selector** | 5-bit field; gfc/glc @ bit 183, vxc @ bit 186; F32 + BF16 value map (verified) |
| **Pop result** | `VectorResult` slot, `PopEupResult` opcode (result-opcode 5 on gfc), dest vreg @ bit 11 (gfc) |
| **Push→pop latency** | JF/DF 4 (clamp); PF 7; VF 6; GL 13 (F32) / 14 (BF16); pop drains at latency 1 |
| **Issue rate** | `VectorEupReservationCycles`: JF/VF/GL 1, PF 2 (half-rate EUP) — orthogonal to latency |
| **Result FIFO** | `EupResultFifoEntry` — a runtime `repeated`-message list, not a fixed compile-time depth |

---

## The Push: VALU Slot 3, Function Selector

### Purpose

The transcendental enters the EUP through a VALU-slot-3 push. There is exactly one EUP-push encoder helper per generation, and it lives only in the `Alu3` op set — no `Alu0`/`Alu1`/`Alu2` helper exists, which is the encoding-level statement that the XLU/EUP is single-issue and sourced exclusively from slot 3. The push helper writes three fields: the VALU-opcode (selecting the EUP-push *family*), a 5-bit *function selector* (selecting *which* transcendental), and the source vreg.

### Encoding

Two layouts exist, one per opcode width. On 6acc60406 (`gfc`, the TPU7x TC bundle; external name TPU7x) and Ghostlite (`glc`) the VALU opcode is 8 bits at bit 194; on Viperfish (`vxc`) it is 7 bits at bit 197. The function selector is always 5 bits, at bit 183 on gfc/glc and bit 186 on vxc. The source vreg is 6 bits. Every field is written by the universal `BitCopy(dst, dst_bit, src, src_bit, nbits)` packer (`@0x1fa0a900`). **All bit positions on this page are LSB-first** — bit 0 is the least-significant bit of byte 0 (byte = `dst_bit >> 3`, bit-in-byte = `dst_bit & 7`), matching the `BitCopy` convention used throughout [Bundle Model](bundle-model-overview.md) and the [VPU Slot](slot-vpu.md); the packer writes `nbits` upward from the LSB-numbered `dst_bit`.

```c
function EncodeTensorCoreVectorAlu3F32Tanh(bundle, alu_proto):   // gfc @0x1f96ae40
    BitCopy(bundle, 194, &0,  0, 8)    // VALU opcode = 0  (the EUP-push family)
    BitCopy(bundle, 183, &19, 0, 5)    // function selector = 0x13 (Tanh, F32)
    if alu_proto.opcode == 211:        // present-bit gate (sub-message attached)
        if (alu_proto.submsg.flags & 1) == 0: return
    BitCopy(bundle, 188, &src_vreg, 0, 6)   // 6-bit source vreg
```

The push helper is per-function: `EncodeTensorCoreVectorAlu3F32Tanh` hardcodes selector `0x13`, `...F32Reciprocal` hardcodes `0x15`, and so on. Viperfish additionally ships a *generic* `EncodeTensorCoreVectorAlu3EupPush` (`@0x1ef6e400`) that writes selector `0x16` (the generic-push slot whose function is carried elsewhere) plus the named per-function vxc helpers.

```c
function EncodeTensorCoreVectorAlu3EupPush(bundle, alu_proto):    // vxc @0x1ef6e400
    BitCopy(bundle, 197, &0,  0, 7)    // VALU opcode = 0  (7-bit on Viperfish)
    BitCopy(bundle, 186, &22, 0, 5)    // generic-push selector = 0x16
    ... present gate ...
    BitCopy(bundle, 191, &src_vreg, 0, 6)
```

### Function Selector Map

The 5-bit function selector (gfc/glc @ bit 183, vxc @ bit 186) takes a distinct value per function *and per datatype*. Every value below is read directly from the `mov`-immediate the per-function `Encode...Alu3<Op>` helper writes before its `BitCopy(_, 183, _, _, 5)`. The F32 and BF16 columns are *different* selectors, not a datatype bit — the hardware decodes function and width from one 5-bit field.

| Function | F32 selector | BF16 selector | LLO push opcode (F32 / BF16) |
|---|---|---|---|
| Erf | `0x0e` (14) | `0x0f` (15) | `0x130` / `0x13a` |
| ReciprocalSqrt (`rsqrt`) | `0x10` (16) | `0x0c` (12) | `0x12c` / `0x136` |
| PowTwo (2^x) | `0x11` (17) | `0x19` (25) | `0x129` / `0x133` |
| LogTwo (`log2`) | `0x12` (18) | `0x1a` (26) | `0x12b` / `0x135` |
| Tanh | `0x13` (19) | `0x1b` (27) | `0x128` / `0x132` |
| ShiftedSigmoid | `0x14` (20) | `0x1c` (28) | `0x12d` / `0x137` |
| Reciprocal | `0x15` (21) | `0x1d` (29) | `0x12a` / `0x134` |
| Sinq (sin) | `0x17` (23) | `0x1e` (30) | `0x12e` / `0x138` |
| Cosq (cos) | `0x18` (24) | `0x1f` (31) | `0x12f` / `0x139` |
| (generic push, vxc) | `0x16` (22) | — | — |
| Erf, push-form | — | — | `0x131` (F32 only) |

> **QUIRK —** selector `0x16` is a hole between `rsqrt` (`0x15`) and `sin` (`0x17`) on the named gfc map, but it is the *generic EUP push* on Viperfish. A reimplementer driving a single hardware decode table must treat `0x16` as "function carried out-of-band", not as a tenth transcendental. The LLO opcode `0x131` `kVectorPushErf` is the F32-only push-form of erf and has no BF16 twin (BF16 erf uses `0x13a`).

### Function Map

| Function | Address | Role |
|---|---|---|
| `gfc::EncodeTensorCoreVectorAlu3F32Tanh` | `0x1f96ae40` | gfc Tanh push (sel `0x13` @183, op@194 w8, src@188) |
| `gfc::EncodeTensorCoreVectorAlu3F32Reciprocal` | `0x1f96afc0` | gfc Reciprocal push (sel `0x15`) |
| `gfc::EncodeTensorCoreVectorAlu3F32ReciprocalSqrt` | `0x1f96ac00` | gfc Rsqrt push (sel `0x10`) |
| `gfc::EncodeTensorCoreVectorAlu3F32Erf` | `0x1f96b200` | gfc Erf push (sel `0x0e`) |
| `gfc::EncodeTensorCoreVectorAlu3Bf16Reciprocal` | `0x1f96b680` | gfc BF16 Reciprocal push (sel `0x1d`) |
| `gfc::EncodeTensorCoreVectorAlu3Bf16Tanh` | `0x1f96b500` | gfc BF16 Tanh push (sel `0x1b`) |
| `vxc::EncodeTensorCoreVectorAlu3EupPush` | `0x1ef6e400` | vxc generic EUP push (sel `0x16` @186, op@197 w7, src@191) |
| `BitCopy` | `0x1fa0a900` | universal bit-granular packer (`_Z7BitCopyPviPKvii`) |

### Considerations

The push's VALU-opcode field is `0` on all three v5+ gens, which means the EUP-push family does not consume an opcode point in the dense `VectorAluOpcode` space the binary-ALU ops use; the function discriminator is entirely in the 5-bit selector. The push offset sits inside the slot-3 VALU window (gfc VALU0 opcode @ bit 293, ~33-bit/slot stride → VALU3 @ ~bit 194), consistent with the four-slot VALU layout of the [VPU Slot](slot-vpu.md). The push is placed into slot 3 by `FindFreeEupSlot<gen>` (`gfc`/`glc` @ `0x142def40`), which calls `FindFreeVectorSlot<gen>(..., 3, ...)` and sets the EUP-occupancy bit `Bundle[+0x10] |= 0x20`, subject to the `VregReadPort` hazard set.

---

## The Pop: VectorResult Slot

### Purpose

The EUP result is drained one or more bundles after the push, in the `VectorResult` slot — the same slot that drains the MXU matmul result (`PopMxuResult`). A single result-opcode value selects which: on 6acc60406 (`gfc`) the `TensorCoreVectorResult0Encoder::Encode` switch (`@0x1fa01820`) maps `5 → PopEupResult`, `6 → TransposeResult`, and `7 → PopMxuResult` — the `case 7` arm is the only one that writes the 8-bit accum-mode/format at bit 323. The pop names only a destination vreg; it carries no function (the function was decided at push time and travels with the FIFO entry).

### Encoding

`PopEupResult` writes a result-tag into the result-type discriminator and a 6-bit destination vreg through the common `VectorResult` tail. On 6acc60406 (`gfc`) the encoder first writes a 2-bit top-level result-tag at bit 20 (common to every result sub-message, value = proto field `+0x1c`), then — in the `case 5` (EUP) arm — a 3-bit EUP sub-tag literal `0` at bit 17, and finally the 6-bit dest vreg at bit 11. On Viperfish/Ghostlite the discriminator is 4 bits at bit 24 and the dest vreg 6 bits at bit 14.

```c
function PopEupResult_gfc(bundle, result_proto):     // gfc, result-opcode 5 (case 5 @0x1fa01820)
    BitCopy(bundle, 20, &result_proto.tag, 0, 2)  // common 2-bit result-tag (proto +0x1c)
    BitCopy(bundle, 17, &0, 0, 3)                 // EUP sub-tag literal 0 (case-5 arm)
    BitCopy(bundle, 11, &dest_vreg, 0, 6)         // common dest-vreg tail
    // (contrast case 7 / PopMxuResult, which writes tag literal 0x2 @ bit 18
    //  and the 8-bit accum-mode/format @ bit 323)
```

At the LLO level the pop is `kVectorEupResultValue`, opcode `0x14e`. The builder `CreateVectorEupResult` (`@0x1d4d9820`) hardcodes it — there is no width or function variant of the pop:

```c
function CreateVectorEupResult(eup_push, region):    // @0x1d4d9820
    assert (eup_push.opcode - 0x128) < 0x13          // push must be in [0x128, 0x13a]
                                                     // ("LloOpcodeIsVectorEup(eup->opcode())")
    return LloInstruction::New(0x14e, {eup_push}, /*n_operands=*/1)
```

### Function Map

| Function | Address | Role |
|---|---|---|
| `CreateVectorEupResult` | `0x1d4d9820` | builds the `0x14e` pop; asserts push ∈ [0x128,0x13a] |
| `gfc::TensorCoreVectorResult0Encoder::Encode` | `0x1fa01820` | result slot switch: op5=PopEup, op6=Transpose, op7=PopMxu |
| `vxc::TensorCoreVectorResult0Encoder::Encode` | `0x1f018f40` | vxc result slot (disc @bit24 w4, dest @bit14 w6) |
| `glc::TensorCoreVectorResult0Encoder::Encode` | `0x1f3bc160` | glc result slot; adds `PopAddMxu01Result` |

### Considerations

Per-gen the `VectorResult` slot carries different sub-messages: `vxc` adds `PopCcrfResult` (scalar/CRF pop), `glc` adds `PopAddMxu01Result` (the fused matres+accumulate of the K>128 matmul path). The EUP pop is the same `PopEupResult` op on all three. The result slot also carries its own predication field (`TensorCoreVectorResult1PredicationField::GetConcatenatedValue` @ `0x1fa02520`), so an EUP pop can be predicated independently of the push.

---

## Fused vs Split: the AndPop Duality

### Purpose

MLIR does not emit a bare push + bare pop. The `UnaryFloatVector` lowering of a transcendental (e.g. `math::TanhOp`) emits a *fused* `tpu_*_macro` IR op, which lowers to a single fused LLO pseudo-op `kVector{fn}{F32,Bf16}AndPop` (opcodes `0x13b`..`0x14d`). That fused op is what later passes split — or, on a generation that can co-locate push and pop, *keep* fused. The duality is explicit in the LLO opcode space: `0x12a kVectorReciprocalF32` (bare push), `0x13d kVectorReciprocalF32AndPop` (fused), `0x14e kVectorEupResultValue` (standalone pop).

### Algorithm

`LloLateDecomposer` (`@0x1269cb20`) walks fused pseudo-EUP ops and rewrites each into a bare push + a deferred pop, calling `DecomposeEupInstruction` (`@0x126a0340`), which dispatches on the pseudo-opcode to one of nine `V*Decomposed` builders. The classifier of a fused op is exact:

```c
function LloOpcodeIsPseudoEupInstruction(op):        // @0x1d60c880
    return (op - 0x13b) < 0x13          // op in [0x13b, 0x14d] (19 AndPop ops)
        && (0x7fdff >> (op - 0x13b)) & 1 // bitmask clears bit 9 (0x144 PushErfAndPop)
```

The 0x7fdff mask excludes exactly one of the 19 AndPop opcodes — bit 9, opcode `0x144 kVectorPushErfAndPop` — so 18 of the 19 are treated as pseudo-EUP. Each `V*Decomposed` builder is a **bare push + bare pop**, selecting the F32 or BF16 push opcode from the `PrimitiveType` argument:

```c
function VtanhDecomposed(builder, prim_type, value):  // @0x1d555040
    push_opcode = (prim_type == 0xb) ? 0x128 : 0x132  // 0xb = BF16 → F32 push 0x128;
                                                      // else BF16 push 0x132
    push = CreateVectorEup(push_opcode, value, region)   // @0x1d4d78a0
    AppendInstruction(region, push)
    pop  = CreateVectorEupResult(push, region)           // hardcodes 0x14e
    AppendInstruction(region, pop)
    // NO inline correction polynomial, NO refinement helper
```

> **NOTE —** the `V*Decomposed` builders carry no inline correction. `VrsqrtDecomposed` and `VtanhDecomposed` emit a bare push + bare pop and nothing else; neither issues a second push+pop pair for a Newton refinement nor interleaves a correction step between push and pop. `VfastTwoSum` (`@0x1d5550a0`) is a stand-alone Dekker two-sum helper (three ops, no constants) that merely sits physically adjacent to `VtanhDecomposed` in `.text` — it is not invoked by it. The correction *math* (with coefficients) lives in the `*NoEupF32` fallbacks and the shared Newton/rational helpers, not inside the `V*Decomposed` builders.

The split is mandatory on the v5+ generations because `HasEupRestrictions` is TRUE on Viperfish (`@0x1c458620`) and Ghostlite (`@0x1c458d80`): the EUP push and its pop **cannot co-issue**, so the decomposer must emit them into separate bundles with VALU correction (or unrelated work) filling the gap. On Jellyfish (`@0x1c457b80`) and Pufferfish (`@0x1c4580c0`) the restriction is FALSE, so the fused form can survive — the inverse simplifiers `SimplifyTanhAndPop` (`@0x1d593c60`), `SimplifyReciprocalAndPop` (`@0x1d595d40`), `SimplifySinqAndPop` (`@0x1d596de0`), `SimplifyCosqAndPop` (`@0x1d597680`) re-fuse a matching push+pop when the schedule allows co-location.

### LLO Opcode Taxonomy

The opcode space is three contiguous bands plus the pop:

| Band | Range | Meaning |
|---|---|---|
| Bare push | `0x128`..`0x13a` | 9 functions × {F32, BF16}; `CreateVectorEup` emits one of these |
| Push-form erf | `0x131` | F32-only push variant of erf |
| Fused AndPop | `0x13b`..`0x14d` | MLIR-emitted fused push+pop; `0x144` (PushErfAndPop) excluded from pseudo-EUP |
| Deferred pop | `0x14e` | `kVectorEupResultValue`; `CreateVectorEupResult` hardcodes it |

### Function Map

| Function | Address | Role |
|---|---|---|
| `DecomposeEupInstruction` | `0x126a0340` | dispatch to 9 `V*Decomposed` builders |
| `LloLateDecomposer` | `0x1269cb20` | splits fused AndPop → bare push + deferred pop |
| `LloOpcodeIsPseudoEupInstruction` | `0x1d60c880` | classifies fused AndPop (bitmask `0x7fdff`) |
| `CreateVectorEup` | `0x1d4d78a0` | emits a bare hardware EUP push (1 operand) |
| `VtanhDecomposed` / `VrsqrtDecomposed` | `0x1d555040` / `0x1d557b60` | bare push+pop builders |
| `DecomposeEupOperationsForBarnacore` | `0x1269c5c0` | BarnaCore EUP split (separate result-drain path) |

---

## Lane Width and the AluEp Unpack Fan-Out

### Purpose

Before a transcendental can push, its operand must fit the EUP lane width. A packed sub-lane vector (two BF16 in a 32-bit lane, or sub-byte `f8`/`s8`) on a generation whose ALU lane is wider than the element must be *unpacked* into lane-width pieces, each piece pushed separately, then the results repacked. This is the `AluEpOpLowering` 1:N path; whether it fires at all, and what N is, is the lane-width model.

### Algorithm

The selector is `SupportsBf16AluInstructions` (Target vtable slot `+0x780`): FALSE → the lane sub-element width is 32 bits (an F32 lane), TRUE → 16 bits (a native BF16 lane). Viperfish returns FALSE (`@0x1d49c0e0`); Ghostlite returns TRUE (`@0x1d498ce0`); the base `Target` is a `LogFatal` pure-virtual, so every concrete gen overrides. `IsDynamicallyLegal` (`@0x135ddd20`) decides 1:1 (stay as the EUP-macro path) vs 1:N (unpack):

```c
function IsDynamicallyLegal(op, target, operand_idx):   // @0x135ddd20
    forced = ForceBF16ALUOperationsToUnpack(op, ty)     // force-1:1 flag
    if !isVectorType(ty):            return LEGAL        // scalar → 1:1
    if !IsPackedVectorType(ty):      return LEGAL        // not packed → 1:1
    fmt = GetVpackFormat(ty)                             // 0=no-pack,1=bf16,0xb=f16,0x7=sub-byte
    if fmt == 0:                     return LEGAL        // not packable → 1:1
    if !target.SupportsBf16AluInstructions():            // *0x780 false (VF) → 1:N
        return ILLEGAL
    return (ty.element.bitwidth == 16) ? LEGAL : ILLEGAL // bf16-16 → 1:1, else (f8/s8) → 1:N
```

When the op is marked ILLEGAL, `UnpackOperand<UnpackFOp>` (`@0x1360fac0`) recursively halves the packed element down to the lane width and returns a `std::deque<Value>` whose length is the 1:N fan-out N:

```c
function UnpackOperand(loc, packBits, operand, vecTy, target, override):  // @0x1360fac0
    lane_bw = target.SupportsBf16AluInstructions() ? 16 : 32   // *0x780; override via optional<int>
    deque = [operand]
    while lane_bw > bitwidth(current_element):                 // outer loop @0x1360fb70
        sub_ty = GetUnpackResultElementType(current, ...)      // @0x1360ff20: 16→BF16, 32→F32,
                                                              //   else getIntegerType(2*src_bw)
        count  = bitwidth(result_elem) / bitwidth(sub_elem)    // pieces this step (typ. 2)
        tuple  = UnpackFOp::create(...)                        // one unpack → a tuple
        for i in 0..count:  deque.push(ExtractTupleElementOp(tuple, i))
    return deque    // length == N
```

The AluEp body then emits exactly N per-piece `ComputeOp::create` calls (each its own EUP push+pop) between one unpack and one `PackResults<PackFOp>`.

### Worked Example — packed `bf16 rsqrt`, Viperfish vs Ghostlite

A `sc.rsqrt` on `vector<…×bf16>` (16-bit packed sub-lane). Both gens register both a 1:1 `UnaryFloatVector` and a 1:N `AluEp` pattern; `IsDynamicallyLegal` picks:

```text
VIPERFISH (SupportsBf16AluInstructions = false):
  fmt(bf16) = 1 (!=0), SupportsBf16Alu = false → ILLEGAL → AluEp 1:N fires.
  UnpackOperand: lane_bw = 32 (F32) > 16 (bf16) → one halving step:
    result_bw = 32, sub_bw = 16 → count = 2 pieces; loop exits (32 == 32).
  ⇒ N ≈ 2 × (shape / lane-tiling); each F32 piece → its own 0x12c rsqrt push + 0x14e pop.

GHOSTLITE (SupportsBf16AluInstructions = true, elem bw == 16):
  fmt != 0, SupportsBf16Alu = true, elem == 16 → LEGAL → NO unpack; the 1:1 path fires:
    tpu_rsqrt_macro → fused 0x149 kVectorRsqrtBf16AndPop, split into a 0x136 BF16 push +
    0x14e pop. ONE push/pop, the BF16 ALU runs the per-lane op natively.
⇒ The newer gen (GL) processes packed bf16 at half the push count of VF, because the native
   BF16 lane lets it skip the unpack to F32 entirely.
```

### Function Map

| Function | Address | Role |
|---|---|---|
| `UnpackOperand<UnpackFOp>` | `0x1360fac0` | recursive sub-element halving; returns the N-deque |
| `GetUnpackResultElementType` | `0x1360ff20` | next-narrower sub-element type per lane width |
| `IsDynamicallyLegal` | `0x135ddd20` | 1:1 vs 1:N selector (4-arm truth table) |
| `Target::SupportsBf16AluInstructions` | `0x1d498ce0` (GL) / `0x1d49c0e0` (VF) | lane sub-element width (vtable +0x780) |
| `GetVpackFormat` | `0x13dad800` | pack-format enum (0=none,1=bf16,0xb=f16,0x7=sub-byte) |

### Considerations

`SupportsSparseCore` (vtable `+0x260`, `@0x1d48fd40`) is the AluEp *entry guard* (the lowering bails if the target has no SparseCore), not a lane-width input — easy to conflate with `+0x780` because both gate the same lowering. The `optional<int>` lane-width override that `UnpackOperand` honors (the `bt $0x20`/`cmovb` path) lets a caller force a non-default lane width for `f8`/sub-byte staging; which callers pass it is not enumerated here. See [Pack/Unpack Precision](pack-unpack-precision.md) for the full vpack-format model.

---

## Payne-Hanek Range Reduction (Trig)

### Purpose

`sin`/`cos` cannot push raw — a large argument |x| ≫ 1 would lose all its significant fraction bits when reduced mod π/2 in float. `PayneHanekRangeReduction` (`@0x1d5819c0`) performs the classic exact-fraction reduction: it multiplies x by a high-precision fixed-point expansion of 1/(2π), keeps the windowed fraction selected by x's exponent, and reconstructs the reduced argument r = (x mod π/2) and the quadrant index k. The function is the integer half of the trig path that pairs with the `sin`/`cos` EUP push.

### The 1/(2π) Table

Six contiguous 32-bit words form the fixed-point expansion of **1/(2π)** (the quadrant-count form: multiply by 1/(2π) to get the number of full turns), loaded MSB-first via `LloModule::VectorU32Constant` (`@0x1d506400`):

| word | u32 | role |
|---|---|---|
| w0 | `0x28be60db` | bits[191..160] of 1/(2π), the MSB limb ( = floor(2³²/(2π)) = 683565275 ) |
| w1 | `0x9391054a` | bits[159..128] |
| w2 | `0x7f09d5f4` | bits[127..96] |
| w3 | `0x7d4d3770` | bits[95..64] |
| w4 | `0x36d8a566` | bits[63..32] |
| w5 | `0x4f10e410` | bits[31..0], the LSB limb |

Fractional reconstruction Σ wᵢ·2^(−32(i+1)) = 0.15915494309189535 = 1/(2π), exact to fp64. The windowed multiply (`VshllU64High` @ `0x1d583ac0` + `VmulU64`) extracts the bit window of the product selected by the argument's exponent, so the relevant fraction bits survive for arbitrarily large |x|.

The reconstruction uses these additional `VectorU32Constant` immediates:

| u32 | value | role |
|---|---|---|
| `0x00000020` | 32 | per-word shift width for the windowed multiply |
| `0x00000fff` | 4095 | 12-bit window / round mask |
| `0x0000001e` | 30 | window-bit count for fractional-product extraction |
| `0xffffffe2` | −30 | binary-point exponent offset (feeds `SubS32` → `CreateVectorBinop 0x121`) |
| `0x20000000` | 2^29 | rounding / half-ULP bias |
| `0x00c90fdb` | — | 24-bit significand of π/2 (float π/2 = `0x3fc90fdb`) → `VcomposeF32` |

The reduced argument is r = `VcomposeF32(0x00c90fdb, ...)` with the −30 binary-point shift — i.e. (x mod π/2) recombined with the π/2 mantissa. The quadrant index k = floor(x/(π/2)) mod 4 selects the sin/cos sign and the sin↔cos swap.

### Considerations

The k-mod-4 → {+sin, +cos, −sin, −cos} sign/swap truth table is computed by the function's 16 `SimplifySelect` arms and is not reproduced here. The table is **1/(2π), not 2/π**, despite the conventional "2/π reduction" name: the leading word is floor(2³²/(2π)), and the reconstruction sums to 1/(2π) exactly. See [Payne-Hanek Range Reduction](../cost/eup-paynehanek.md) for the cost-model framing.

---

## Correction Coefficients

### Purpose

On an EUP-capable generation most transcendentals push raw and that is the answer. Three places hold polynomial coefficients: the **Newton refinements** that a few functions wrap around the raw push for extra accuracy (`recip`, `rsqrt`); the **rational minimax** approximations (`tanh`, `atan2`) used as the no-EUP software path; and the **`*NoEupF32` software fallbacks** (`exp`, `expm1`, `ln`, `ln1p`, `log2`) that implement a transcendental entirely in VALU when a gen/datatype lacks the hardware EUP. All constants are fp32 `.rodata` literals (VMA == file offset).

### Newton-Raphson Refinements

```c
function EmitRecpNrIteration(x, y):       // @0x1d5a9ec0 — recip Newton step
    return y * (2 - x*y)                  // one constant: 1.0 = 0x3f800000 @0x84a2444

function EmitRsqrtNrIteration(x, y):      // @0x1d5a9e20 — rsqrt Newton step
    return y * (1.5 - 0.5*x*y*y)          // 0.5 = 0x3f000000 @0x84a27e8;
                                          // 1.5 = 0x3fc00000 @0x84a2680
```

### Tanh Rational (`tanh(x) ≈ x·P(x²)/Q(x²)`)

`EmitTanhPolyApproximation` (`@0x1d5a9f40`) is the no-EUP software tanh and the rational the cost model sizes against. The SSA structure, traced through `VdivF32`: clamp x to [−9, +9]; small-|x| guard (|x| < 4e-4 → return x); x² = x·x; **numerator** = x·P(x²) where P is a 7-coefficient Horner in x² (seed c0 + 6 FMAs c1..c6, then ×x); **denominator** = Q(x²), a 4-coefficient Horner (seed d0 + 3 FMAs d1..d3); `VdivF32(NUM, DEN)`; `Vselect(small ? x : quotient)`; final clamp to [−1, +1].

The numerator P has **7** coefficients (c0..c6): the seed `VimmF32(c0)` plus six `VmulAddF32` FMA steps (c1..c6). The `VdivF32` operand trace disambiguates the two Horner accumulators — the one multiplied by the clamped x (forming the odd function x·P) is the numerator; the bare even Horner is the denominator.

Numerator P(x²) — 7 coeffs, Horner low→high in x², then ×x:

| role | u32 | f32 | .rodata off |
|---|---|---|---|
| c0 | `0xa59f25c0` | -2.7607683663038313e-16 | `0x84a2654` |
| c1 | `0x2a61337e` | 2.0001879384549948e-13 | `0x84a2428` |
| c2 | `0xaebd37ff` | -8.604671836165423e-11 | `0x84a2ef4` |
| c3 | `0x335c0041` | 5.122297253024044e-08 | `0x84a27d8` |
| c4 | `0x3779434a` | 1.4857223504805006e-05 | `0x84a24f0` |
| c5 | `0x3a270ded` | 0.0006372619536705315 | `0x84a242c` |
| c6 | `0x3ba059dc` | 0.004893524572253227 | `0x84a24a0` |

Denominator Q(x²) — 4 coeffs, Horner low→high in x²:

| role | u32 | f32 | .rodata off |
|---|---|---|---|
| d0 | `0x35a0d3d8` | 1.1982583600911312e-06 | `0x84a2e8c` |
| d1 | `0x38f895d6` | 0.00011853470641653985 | `0x84a2658` |
| d2 | `0x3b14aa05` | 0.0022684347350150347 | `0x84a2564` |
| d3 | `0x3ba059dd` | 0.0048935250379145145 | `0x84a27dc` |

Brackets: clamp lo −9.0 (`0xc1100000` @ `0x84a283c`), clamp hi +9.0 (`0x41100000` @ `0x84a2a84`), small thresh 4e-4 (`0x39d1b717` @ `0x84a2758`), saturate −1.0 (`0xbf800000` @ `0x84a26cc`), +1.0 (`0x3f800000` @ `0x84a2444`).

> **NOTE —** c6 (`0x3ba059dc`) and d3 (`0x3ba059dd`) differ in only the LSB. The leading numerator and denominator coefficients are intentionally near-equal so x·P/Q → ±1 as |x| → 9 (the saturation limit); this is the rational's high-order asymptote. They are byte-confirmed *distinct* constants, not a transcription error.

### No-EUP Software Fallbacks

When a gen/datatype has no hardware EUP, the `*NoEupF32` family implements the transcendental in pure VALU. These are the coefficient sources for `exp`/`log` (which have no hardware EUP push — only `pow2`/`log2` do, and `exp`/`ln` are built from them by scaling).

| Function | Address | Method | Key constants |
|---|---|---|---|
| `VexpNoEupF32` | `0x1d533820` | exp(x)=2^(x·log2e), Cody-Waite | log2e 1.4426950 (`0x3fb8aa3b`); ln2-hi −0.693359375 (`0xbf318000`), ln2-lo 2.1219e-4 (`0x395e8083`); clamp [−87.337, +88.723]; 5-term Taylor {0.49999988, 0.16666518, 0.041669652, 0.0083689447, 0.0013744964} |
| `Vexpm1NoEupF32` | `0x1d533ec0` | expm1, own 5-coeff Taylor | clamp lo −17.32868 (`0xc18aa123`); 5-term {1.428607e-6, 0.0013910766, 0.008363097, 0.041666709, 0.16666578} |
| `Vlog2NoEup` | `0x1d556a60` | mantissa-extract + 9-coeff Horner | masks `0x7fffff` / `0x3f800000`; sqrt2 1.4142135 (`0x3fb504f3`); 9 coeffs c0..c8 ending 1.44269502 |
| `VlnNoEupF32` | `0x1d534740` | ln = log2 · ln2 | ln2 = 0.6931472 (`0x3f317218`) |
| `Vln1pNoEupF32` | `0x1d534a20` | log1p, 8-coeff Horner | ln2-hi 0.693145752 (`0x3f317200`), ln2-lo 1.428607e-6 (`0x35bfbe8e`); 8 coeffs |
| `VtanhNoEupF32` | `0x1d535b20` | 32-byte thunk → `EmitTanhPolyApproximation` | (rational above) |
| `EmitAtan2Approximation` | `0x1d5aa1c0` | r=min/max ratio; 7-term odd Horner | quadrant consts π/2 1.5707964, π 3.1415927, π/4 0.78539819, 3π/4 2.3561945 |

The EUP-using exp/ln variants apply a small fp32 correction over the hardware pow2/log2 push: `VexpEup` (`@0x1d556080`) multiplies by log2e then uses the fused `0x146 kVectorPow2Bf16AndPop`; `Vln1pEup` (`@0x1d557340`) uses the log2 EUP plus a 4.4273e-4 correction (`0x39e81ecb`).

### Considerations

The exact gen×datatype gating of "no-EUP fallback vs bare push" is owned by the per-gen `SupportsXxxInstruction` Target accessors, not re-derived here; the binding of a fallback to a specific gen lives in those accessors. See [EUP Correction Coefficients](../cost/eup-correction-coeffs.md) for the complete coefficient catalog.

---

## Push→Pop Latency and the Result FIFO

### Purpose

The EUP push→pop pair is bounded by two independent quantities read from two different arrays: the **data latency** (minimum bundles from push to its drain) and the **issue reservation** (minimum bundles from one push to the next). The data latency is the depth the VALU correction window must hide; the reservation is the EUP unit's throughput. A reimplementer who multiplies them gets the wrong schedule.

### The Latency Edge

Two mechanisms set the push→pop data-latency edge. The legacy `Jellyfish`/`Dragonfish` path clamps it: `LatencyTableJellyfish::LatencyBetweenInternal` (`@0x1c8a0d60`), when the first opcode is a bare push (∈ [0x128, 0x13a]) and the second is the `0x14e` pop, clamps the latency to `this+0x1c`, which the ctor copies from `Performance[+0x30]` = 4. The newer gens route through a per-instruction heap array instead:

```c
function LatencyBetweenInternal_newer(from, to):   // PF/VF/GL
    inst = Get<Gen>Instruction(from)               // LLO-opcode → per-gen Instruction enum
    base = <Gen>Performance::GetLatency(inst)       // latencies[inst]: int32 heap array
                                                    // ptr@Perf[0], count@Perf[+0x8]
    // max-combine with XLU/MXU floors — the EUP push is NOT in the floored set —
    // so `base` passes through unmodified.
    return base
```

`GetLatency` is the identical 4-instruction lookup on every newer gen (VF `@0x1c8cbc20`, PF-TC `@0x1c8c3860`, GL `@0x1c8d36e0`): `cmp [rdi+8],idx; jbe ud2; mov rcx,[rdi]; mov eax,[rcx+idx*4]; ret`. The per-gen `Performance` ctor fills the array element-wise; the EUP push value is the edge weight.

| gen | EUP push latency | pop latency | mechanism | byte anchor |
|---|---|---|---|---|
| Jellyfish | 4 | (clamp) | `Performance[+0x30]` = 4 clamp | `PerformanceJf` ctor `@0x1d4930c0` |
| Dragonfish | 4 (inherits Jf) | (clamp) | `PerformanceDf` keeps `+0x30` = 4 | `@0x1d493060` |
| Pufferfish | 7 | 1 | `PufferfishPerformance` array | `[rax+0x19c]=7` (idx 0x67) |
| Viperfish | 6 | 1 | `ViperfishPerformance` array | `[rax+0x330..0x348]=6` (idx 0xcc..0xd2) |
| Ghostlite | 13 (F32) / 14 (BF16) | 1 | `GhostlitePerformance` array | `[rax+0x418..0x460]=0xd/0xe`; pop `[rax+0x710]=1` |

The push value is **uniform across all classified EUP functions** on each gen — `tanh = pow2 = recip = log2 = rsqrt = sigshft = erf` — so the EUP unit has a single transcendental latency per datatype. Ghostlite is the only gen that splits F32 (13) from BF16 (14): it has the native BF16 ALU (`SupportsBf16AluInstructions` TRUE) so its classifier maps both the F32 pushes (Instr 0x106..0x10f → 13) and the BF16 pushes (Instr 0x110..0x118 → 14) to distinct latency slots; the BF16 transcendental costs one extra cycle. Viperfish and Pufferfish only classify the F32 pushes (the late decomposer widens BF16 EUP to the F32 push on those gens), so they have a single EUP latency.

> **QUIRK —** Pufferfish's latency table is a `std::variant<PufferfishPerformance, PufferfishBarnaCorePerformance>` dispatched by a 2-arm `__fmatrix` visitor (`@0x21c203d0`); the high 16 bits of `GetPufferfishInstruction`'s return select the variant. **Every** EUP opcode emits variant 0 (TensorCore), so the EUP edge is the TensorCore array = 7, not the BarnaCore variant (which has its own 6-cycle EUP block in a separate 134-entry array). Pufferfish ships both because BarnaCore retires only after Pufferfish.

### Latency vs Reservation Orthogonality

`LatencyTable::LatencyBetween` (`@0x1c89f820`) calls the per-gen `LatencyBetweenInternal`, optionally adds random jitter, special-cases only the matres/transpose opcodes (`0x82`/`0x84`) with an MXU floor, and returns the edge **unchanged** for the EUP push. There is no multiply by any reservation field anywhere on the path. `VectorEupReservationCycles` (Target vtable `+0x480`: JF/VF/GL = 1, PF = 2) is the *orthogonal* issue-occupancy — how many bundles the EUP resource stays reserved after a push — applied by the per-instruction resource model (`GetResourceUsage` matrix + `SlotTracker`), not by the latency edge.

| quantity | bounds | source | PF | VF | GL (F32/BF16) | JF |
|---|---|---|---|---|---|---|
| push→pop data latency | min bundles push → drain | `latencies[Get<Gen>Instr(push)]` | 7 | 6 | 13 / 14 | 4 (clamp) |
| pop latency | latency the drained value carries | `latencies[pop Instr]` | 1 | 1 | 1 | — |
| `VectorEupReservationCycles` | min bundles push → next push | Target accessor (`+0x480`) | 2 | 1 | 1 | 1 |

The composition is `max(latency-deadline, resource-availability)`, not a product: a pop is placed no earlier than `push_bundle + latency`; consecutive pushes are no closer than `reservation` bundles. The VALU-correction software-pipeline depth the decomposer must fill = the **latency** (PF 7), independent of the reservation. Pufferfish's half-rate EUP (reservation 2) does *not* double the 7-cycle push→pop window — it halves the issue rate. For a chain of N independent transcendentals on PF the EUP-bound schedule length ≈ 2·(N−1) + 7 bundles, not 7·2.

### The Result FIFO

The EUP result FIFO is **not a fixed compile-time depth**. The HW-state simulator proto holds it as `repeated EupResultFifoEntry eup_result_fifo_entries = 3` (descriptor bytes `18 05 20 03 28 0b`), where `message EupResultFifoEntry { repeated Lane lanes = 1; }` — a runtime snapshot list of in-flight pushed results, each entry the per-lane result vector. The number of in-flight pushes is bounded at *schedule* time by the latency edge + the `BaseFifoTracker<LloValue*>` push/pop ordering (`FindBlockingPushesAndPops` @ `0x14442f60`, which calls `LatencyTable::LatencyBetween` for the FIFO-ordering edges), and at the v5+ bundle level by `HasEupRestrictions` forcing push/pop into separate bundles. The silicon FIFO's physical depth is a chip parameter, not a libtpu literal — it is not recoverable from this binary.

### Function Map

| Function | Address | Role |
|---|---|---|
| `LatencyTable::LatencyBetween` | `0x1c89f820` | dispatcher; returns EUP edge unmodified |
| `LatencyTableJellyfish::LatencyBetweenInternal` | `0x1c8a0d60` | JF/DF EUP clamp to `Performance[+0x30]`=4 |
| `GhostlitePerformance` ctor | `0x1c8cbc80` | fills F32 EUP=13, BF16=14, pop=1 |
| `ViperfishPerformance` ctor | `0x1c8c4840` | fills EUP=6, pop=1 |
| `PufferfishPerformance` ctor | `0x1c8be080` | fills EUP=7 (TensorCore variant 0) |
| `<Gen>Performance::GetLatency` | `0x1c8cbc20` (VF) etc. | `latencies[Instruction]` heap lookup |
| `EupResultFifoEntry` (proto) | `0x0e7a6cc0` (ctor) | runtime `repeated`-message FIFO list |
| `BaseFifoTracker<LloValue*>::FindBlockingPushesAndPops` | `0x14442f60` | FIFO push/pop ordering edges |

### Considerations

The Pufferfish `kVectorSigShftF32` (0x12d) Instruction ordinal falls to the classifier default (the other six F32 EUP pushes are all 7), so the shifted-sigmoid edge on PF follows the uniform-EUP-latency pattern of 7, though that specific edge is not pinned here. Which of the 31 `GhostlitePerformance` resources is the EUP unit (and whether its `GetResourceUsage` cycle count equals `VectorEupReservationCycles`) is not isolated here from the ctor's template fill. See [EUP Latency Overview](../cost/eup-latency-overview.md) for the cost-model integration.

---

## Cross-References

- [VPU Slot](slot-vpu.md) — the VALU slot family; the EUP push is `Alu3`-only (slot-3 restriction, op@197/194 sel@186/183 src@191/188)
- [Viperfish 64-bit Bundle](bundle-vf-64b.md) — the Alu3 EUP push in the full 64-byte bundle context
- [Ghostlite Bundle](bundle-gl.md) — the glc bundle layout and its 8-bit VALU opcode
- [6acc60406 Bundle](bundle-gf.md) — the gfc result-slot map (`5`=PopEup, `6`=Transpose, `7`=PopMxu) and the full 64-byte slot layout
- [Pack/Unpack Precision](pack-unpack-precision.md) — the vpack-format model behind `IsDynamicallyLegal` and the AluEp unpack
- [EUP Latency Overview](../cost/eup-latency-overview.md) — the cost-model framing of the push→pop latency edge and reservation
- [EUP Correction Coefficients](../cost/eup-correction-coeffs.md) — the full coefficient catalog for the no-EUP fallbacks and Newton/rational refinements
- [Payne-Hanek Range Reduction](../cost/eup-paynehanek.md) — the trig 1/(2π) reduction table and quadrant reconstruction
