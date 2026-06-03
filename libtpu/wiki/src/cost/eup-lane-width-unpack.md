# EUP Lane-Width and Unpack Count

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA minus `0x200000` equals its file offset. Other libtpu builds will differ.*

## Abstract

A transcendental on the SparseCore is not one cost — it is `N` copies of one cost, where `N` is the **unpack fan-out**: how many lane-width pieces a packed operand vector splits into before each piece can be pushed into the Extended Unary Pipeline (EUP). This page is the cost-model half of the EUP datapath. The [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) page documents *how* a push is encoded; this page documents *how many* pushes a given operand costs, and *how* the push→pop latency is hidden by a software-pipelining split rather than charged inline. Both quantities are decided before the bundle scheduler ever runs, by two MLIR-level mechanisms a reimplementer's cost model must reproduce.

The first mechanism is the **lane-width / unpack-count model**. The SparseCore lowering registers two competing patterns for every elementwise float op: a 1:1 `UnaryFloatVector` pattern that emits a single fused EUP macro, and a 1:N `AluEp` pattern that unpacks a packed operand into lane-width sub-elements, runs one compute op per piece, and repacks. `IsDynamicallyLegal` (`@0x135ddd20`) decides which fires; when it fires the 1:N path, `UnpackOperand<UnpackFOp>` (`@0x1360fac0`) recursively halves the packed element down to the lane sub-element width and returns a `std::deque<Value>` whose **length is `N`**. The lane width itself is a single per-generation boolean: `SupportsBf16AluInstructions` (Target vtable `+0x780`) — FALSE means a 32-bit (F32) lane, TRUE means a native 16-bit (BF16) lane. The cost consequence is concrete and counter-intuitive: a generation with the native BF16 ALU (Ghostlite) processes a packed `bf16` transcendental at *half* the push count of a generation without it (Viperfish), because it never has to unpack to F32. The unpack count is the multiplier the per-piece transcendental cost is charged `N` times against.

The second mechanism is the **bare-push / `tpu_eup_pop` software-pipelining split**. MLIR emits a transcendental as a *fused* `kVector{fn}{F32,Bf16}AndPop` pseudo-op (opcodes `0x13b`..`0x14d`). On a v5+ generation the EUP push and its pop cannot co-issue (`HasEupRestrictions` is TRUE on Viperfish and Ghostlite), so `LloLateDecomposer` rewrites the fused op into a **bare hardware push** (`CreateVectorEup`, `0x128`..`0x13a`) and a **deferred pop** (`CreateVectorEupResult`, hardcoded `0x14e kVectorEupResultValue`) in a separate bundle. The cost-model implication is that a transcendental's latency is *not* a single bundle's stall — it is a push→pop window the scheduler fills with unrelated VALU work, and the EUP throughput is governed by an orthogonal issue-reservation rate (`VectorEupReservationCycles`), not by the latency. A cost model that multiplies latency by reservation, or that charges the push and pop as one indivisible op, gets the schedule wrong.

For reimplementation, the cost model's contract is:

- **The lane sub-element width.** `SupportsBf16AluInstructions` (vtable `+0x780`) as the 16-vs-32-bit selector, its per-gen values, and the `optional<int>` override that lets a caller force a non-default width.
- **The 1:1-vs-1:N decision.** The four-arm `IsDynamicallyLegal` truth table that picks the EUP-macro path (cost 1) versus the AluEp unpack path (cost `N`).
- **The unpack count `N`.** `UnpackOperand`'s recursive halving — the per-step piece count `result_bw / sub_bw`, the loop exit at lane width, and how the vector shape folds in.
- **The bare-push / deferred-pop split.** That the `V*Decomposed` builders emit a bare push + bare pop with *no* inline correction, that the split is mandated by `HasEupRestrictions`, and that the latency-hiding work is unrelated VALU instructions the scheduler interleaves — not part of the EUP op.
- **The latency / reservation orthogonality.** `VectorEupReservationCycles` (push→push spacing) as a separate quantity from the push→pop latency the correction window fills.

| | |
|---|---|
| **Lane-width selector** | `Target::SupportsBf16AluInstructions` (vtable `+0x780`); VF `0x1d49c0e0` → FALSE (32-bit), GL `0x1d498ce0` → TRUE (16-bit) |
| **1:1-vs-1:N gate** | `IsDynamicallyLegal` (`@0x135ddd20`) — LEGAL ⇒ 1:1 macro (cost 1), ILLEGAL ⇒ AluEp unpack (cost `N`) |
| **Unpack-count engine** | `UnpackOperand<UnpackFOp>` (`@0x1360fac0`) — recursive halving; deque length = `N` |
| **Sub-element type** | `GetUnpackResultElementType` (`@0x1360ff20`) — 16→BF16, 32→F32, sub-8-bit → `int(2·src_bw)` |
| **Split trigger** | `HasEupRestrictions` — VF `0x1c458620`, GL `0x1c458d80` = TRUE; JF `0x1c457b80`, PF `0x1c4580c0` = FALSE |
| **Decomposer** | `LloLateDecomposer` (`@0x1269cb20`) → `DecomposeEupInstruction` (`@0x126a0340`) → 10 `V*Decomposed` builders across 19 cases |
| **Bare push / deferred pop** | `CreateVectorEup` (`@0x1d4d78a0`, `0x128`..`0x13a`) + `CreateVectorEupResult` (`@0x1d4d9820`, hardcoded `0x14e`) |
| **Issue reservation** | `VectorEupReservationCycles` (vtable `+0x480`): JF/VF/GL = 1, PF = 2 (half-rate EUP) |

---

## The Lane Sub-Element Width

### Purpose

The lane width is the floor the unpack model halves down to: a piece narrower than the lane is staged no further; a piece wider must be split. It is the single per-generation input that turns the abstract "unpack a packed operand" into a concrete count. A reimplementer's cost model reads it once per target and uses it everywhere the AluEp fan-out is computed.

### Algorithm

The width is one virtual call. `Target::SupportsBf16AluInstructions` is vtable slot `+0x780` (decimal `1920`, the immediate that appears in every consumer). The decompile of the two v5+ overrides is trivial and decisive:

```c
function ViperfishTarget::SupportsBf16AluInstructions():    // 0x1d49c0e0
    return 0                                                // FALSE → 32-bit (F32) lane

function GhostliteTarget::SupportsBf16AluInstructions():    // 0x1d498ce0
    return 1                                                // TRUE → 16-bit (BF16) lane
```

The base `Target::SupportsBf16AluInstructions` (`@0x1d61f580`) is a `LogFatal` pure-virtual sentinel — every concrete generation **must** override, so a reimplementer cannot leave it unset. The boolean is converted to a width by the same arithmetic in both unpack consumers: `width = 16 * (supports ^ 1) + 16`, i.e. TRUE → 16, FALSE → 32.

```c
// from GetUnpackResultElementType @0x1360ff20 and UnpackOperand @0x1360fac0:
lane_bw = 16 * (target.SupportsBf16AluInstructions() ^ 1) + 16   // *0x780 → {16, 32}
if (override & 0x100000000)   // optional<int> engaged-bit
    lane_bw = override        // caller-forced width (f8 / sub-byte staging)
```

> **NOTE —** the `Target const&` the lowering passes is the base `xla::jellyfish::Target` (a per-gen `ViperfishTarget` / `GhostliteTarget`), **not** the derived `…SparseCoreTarget`. The `+0x780` slot on the gen-target vtable is `SupportsBf16AluInstructions`; on the `SparseCoreTarget` vtable the same offset is a different method (`NumVsSlots`), a different class layout that is not consulted here. A reimplementer dispatching `+0x780` on the wrong object reads the wrong field.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ViperfishTarget::SupportsBf16AluInstructions` | `0x1d49c0e0` | returns FALSE → 32-bit (F32) lane | CERTAIN |
| `GhostliteTarget::SupportsBf16AluInstructions` | `0x1d498ce0` | returns TRUE → 16-bit (BF16) lane | CERTAIN |
| `Target::SupportsBf16AluInstructions` (base) | `0x1d61f580` | `LogFatal` pure-virtual; must be overridden | CERTAIN |

### Considerations

The `optional<int>` override (the `bt $0x20` / `cmovb` engaged-bit test, byte-confirmed in both `UnpackOperand` and `GetUnpackResultElementType`) lets a caller force a non-default lane width for `f8` / sub-byte staging instead of the `SupportsBf16AluInstructions`-derived 16/32. The default path (no override) is byte-confirmed; *which* AluEp callers pass a non-default override was not enumerated, so the override-driven fan-out variants are **LOW** confidence. See [Pack/Unpack Precision](../isa/pack-unpack-precision.md) for the full `VpackFormat` model these widths interact with.

---

## The 1:1-vs-1:N Decision

### Purpose

Before any unpack count exists, the lowering decides whether to unpack at all. A transcendental source op (e.g. `sparse_core::RsqrtOp`) is registered with *both* a 1:1 `UnaryFloatVector` pattern (one fused EUP macro, cost 1) and a 1:N `AluEp` pattern (unpack → `N` compute ops → repack, cost `N`). `IsDynamicallyLegal` is the per-op dynamic-legality predicate that arbitrates: if the op is **LEGAL** it stays as the macro (1:1); if **ILLEGAL** the conversion runs the AluEp unpack (1:N). The cost model must reproduce this predicate exactly, because it is the difference between charging a transcendental once and charging it `N` times.

### Algorithm

`IsDynamicallyLegal` (`@0x135ddd20`) is a four-arm truth table, byte-confirmed in the decompile:

```c
function IsDynamicallyLegal(op, target, operand_idx):   // 0x135ddd20
    forced = ForceBF16ALUOperationsToUnpack(op, ty)     // @0x135dd6e0 — force-UNPACK (1:N) flag
    if ty.typeID != mlir::VectorType::id:  return LEGAL  // scalar operand → 1:1
    if !IsPackedVectorType(ty, op):        return LEGAL  // vector but not packed → 1:1
    fmt = GetVpackFormat(ty)                             // @0x13dad800: 0=no-pack,1=bf16,0xb=f16,0x7=sub-byte
    if fmt == 0 || forced:   return (fmt == 0)           // not packable → 1:1; forced+packable → !fmt = ILLEGAL (1:N)
    if !target.SupportsBf16AluInstructions():            // *0x780 FALSE (VF) → 1:N
        return ILLEGAL
    return (ty.element.bitwidth == 16) ? LEGAL : ILLEGAL // native bf16-16 → 1:1, else (f8/s8) → 1:N
```

The four arms that matter for cost, as a truth table:

| operand type | `IsPackedVectorType` | `GetVpackFormat` | `SupportsBf16Alu` | elem bw | result | EUP cost |
|---|---|---|---|---|---|---|
| not a vector / not packed | — / false | — | — | — | LEGAL | 1 (macro) |
| packed, `fmt == 0` | true | 0 (no-pack) | — | — | LEGAL | 1 (macro) |
| packed, `fmt != 0` | true | != 0 | FALSE (VF) | any | ILLEGAL | `N` (unpack) |
| packed, `fmt != 0` | true | != 0 | TRUE (GL) | 16 (bf16) | LEGAL | 1 (macro) |
| packed, `fmt != 0` | true | != 0 | TRUE (GL) | != 16 (f8/s8) | ILLEGAL | `N` (unpack) |

> **QUIRK —** the cheapest path is the *newest* generation on its *native* datatype. A packed `bf16` op is LEGAL (cost 1) only when the generation has the BF16 ALU **and** the element is exactly 16-bit. Viperfish — which lacks the BF16 ALU — marks the same op ILLEGAL and pays the `N`-way unpack to F32. A reimplementer who assumes "packed always unpacks" over-charges Ghostlite by a factor of `N`; one who assumes "transcendentals are always 1:1" under-charges Viperfish by the same factor.

> **CORRECTION (EUP-2) —** `ForceBF16ALUOperationsToUnpack` (`@0x135dd6e0`) forces the *ILLEGAL / 1:N unpack* path, not the 1:1 macro. Its name is literal: when the op carries the `sc.emit_vectorized_alu_operation_in_f32_precision` BoolAttr **true** and its element `isBF16`, the function returns 1, and the `(fmt == 0) | forced` short-circuit in the decompile (`if ((VpackFormat == 0) | v4) return !VpackFormat`) then returns `!fmt`. For a packed bf16 op `fmt == 1`, so `!fmt == 0 == ILLEGAL` — the op is forced to unpack to F32 and run the vectorized ALU in F32 precision, exactly as the attribute name describes. The flag therefore only takes effect on a generation that would otherwise mark the bf16 op LEGAL (Ghostlite, native BF16 ALU): it overrides that to a 1:N F32 unpack. An earlier draft of this page described it as a force-1:1 flag, which is backwards.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `IsDynamicallyLegal` | `0x135ddd20` | 1:1-vs-1:N selector (4-arm truth table) | CERTAIN |
| `ForceBF16ALUOperationsToUnpack` | `0x135dd6e0` | force-1:N flag; on the `f32-precision` attr drives a packed bf16 op to ILLEGAL (unpack to F32) | CERTAIN |
| `IsPackedVectorType` | `0x13611720` | sub-element-packed vector test | CERTAIN |
| `GetVpackFormat` | `0x13dad800` | pack-format enum (0=none,1=bf16,0xb=f16,0x7=sub-byte) | HIGH |

### Considerations

Both pattern families are registered for the *same* source ops (`sc::RsqrtOp` has a 1:1 `UnaryFloatVector` at `0x1357e540` and a 1:N `AluEp` at `0x135e1c80`); `IsDynamicallyLegal` is the dynamic-legality predicate that decides which applies, and the conversion framework rewrites the ILLEGAL op through the AluEp pattern. The `PatternBenefit` ordering between the two co-registered patterns was read structurally, not byte-decoded, so the claim that dynamic legality is the *sole* arbiter (with no benefit tie) is **HIGH**, not CERTAIN.

---

## The Unpack Count `N`

### Purpose

This is the cost multiplier. When `IsDynamicallyLegal` marks an op ILLEGAL, the AluEp body calls `UnpackOperand<UnpackFOp>` to split the packed operand into lane-width pieces, emits exactly one compute op per piece, and repacks. The number of pieces — the returned `std::deque<Value>` length — is `N`, and the AluEp body's per-piece `ComputeOp::create` loop runs `N` times. A reimplementer's elementwise / transcendental cost is `N × (per-piece op cost)`.

### Algorithm

`UnpackOperand<UnpackFOp>` (`@0x1360fac0`) seeds a deque with the wide operand, then halves repeatedly until each piece fits the lane width:

```c
function UnpackOperand(loc, packBits, operand, vecTy, builder, target, override):  // 0x1360fac0
    lane_bw = 16 * (target.SupportsBf16AluInstructions() ^ 1) + 16   // *0x780 → {16, 32}
    if (override & 0x100000000):  lane_bw = override                 // engaged optional<int>
    elem = vecTy.getElementType()
    deque = [operand]                                                // seed with the wide value
    do:                                                              // outer loop @0x1360fb70
        sub_ty = GetUnpackResultElementType(elem, builder, target, override)  // @0x1360ff20
        sub_vec = GetVectorType(sub_ty, target, ...)                 // CHECK: status OK
        count = bitwidth(elem) / bitwidth(sub_ty)                    // pieces this step (idiv)
        tuple = UnpackFOp::create(...)                               // one unpack → a tuple
        for i in 0 .. count-1:                                       // inner loop @0x1360fb80
            deque.push( ExtractTupleElementOp::create(tuple, i) )    // one piece per tuple slot
        elem = sub_ty                                                // narrow for next iteration
    while (lane_bw > bitwidth(elem))                                 // exit when piece fits lane
    return deque                                                     // length == N
```

The sub-element type per step is `GetUnpackResultElementType` (`@0x1360ff20`):

```c
function GetUnpackResultElementType(elem, builder, target, override):  // 0x1360ff20
    lane_bw = 16 * (target.SupportsBf16AluInstructions() ^ 1) + 16      // *0x780
    if (override & 0x100000000):  lane_bw = override
    src_bw = elem.getIntOrFloatBitWidth()
    out_bw = (src_bw >= 8) ? lane_bw : 2 * src_bw      // ≥8-bit jumps to lane; sub-8-bit doubles
    if elem is a FloatType:
        if out_bw == 16:  return builder.getBF16Type()
        if out_bw == 32:  return builder.getF32Type()
    return builder.getIntegerType(out_bw)
```

So a `bf16` (16-bit) element on a 32-bit lane unpacks in one step (`count = 32/16 = 2` pieces, then `lane_bw == elem_bw` exits); a sub-8-bit element doubles its width each step (`out_bw = 2·src_bw`) until it reaches the lane width, multiplying the piece count by 2 per step. The total `N` is the product over halving steps of `result_bw / sub_bw`, folded with the vector shape's lane tiling.

> **NOTE —** the per-step piece count `result_bw / sub_bw` is byte-exact (the `idiv` at `0x1360fbfc`). The *total* `N` for a multi-element vector folds in the `getShape` product divided by the lane-tiling capacity (the same `LaneCount = 128` sub-lane geometry the per-piece cost uses); that shape-folding factor was applied structurally, not re-dumped — so the per-step count is CERTAIN and the absolute `N` for a given tensor shape is **INFERRED** from the lane-tiling geometry.

### The AluEp Body Charges `N` Compute Ops

The representative AluEp body, `AluEp<math::ExpOp>` (`@0x135df200`), makes the cost concrete:

```text
AluEp<math::ExpOp>::matchAndRewrite:                 // 0x135df200
  if !target.SupportsSparseCore():  bail              // *0x260 — ENTRY GUARD, not a width input
  if IsDynamicallyLegal(op, target):
        ExpOp::create(...)                            // 1:1 — a single fresh op (cost 1)
        replaceOp; return
  deque = UnpackOperand<UnpackFOp>(...)               // N narrow pieces  @0x135df55b
  for piece in deque:                                 // walk the deque   @0x135df8f0
        results.push( ExpOp::create(piece) )          // ONE compute op per piece (N total)
  PackResults<PackFOp>(results)                       // repack the N results @0x135df7ba
  replaceOp
```

The compute-op count equals the deque length `N`. For a transcendental this is `N` independent EUP push/pop pairs (each piece is its own push); for a plain arithmetic op it is `N` VALU ops. This is the `N` the per-piece transcendental / elementwise cost is charged against.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `UnpackOperand<UnpackFOp>` | `0x1360fac0` | recursive halving; returns the `N`-deque | CERTAIN |
| `GetUnpackResultElementType` | `0x1360ff20` | next-narrower sub-element type per lane width | CERTAIN |
| `AluEp<math::ExpOp>` (representative) | `0x135df200` | `N` `ComputeOp::create` between one unpack and one pack | CERTAIN |
| `PackResults<PackFOp>` | `0x13610940` | reassemble the `N` results into the wide output | CERTAIN |
| `Target::SupportsSparseCore` | `0x1d48fd40` | AluEp entry guard (vtable `+0x260`), **not** a width input | CERTAIN |

### Considerations

`SupportsSparseCore` (vtable `+0x260`, `topology[+0x3b8][+0x98] > 0`) is the AluEp `matchAndRewrite` entry guard — the lowering bails if the target has no SparseCore — and is easy to conflate with the `+0x780` width selector because both gate the same lowering. It is **not** an input to the unpack count; the cost model must read width from `+0x780` only. The unpack helpers come in float / signed-int / unsigned-int flavors (`UnpackOperand<UnpackFOp/SIOp/UIOp>`); only the `UnpackFOp` (float) path is decoded here, and the int paths are assumed to follow the same halving by structural symmetry.

---

## The Bare-Push / Deferred-Pop Split

### Purpose

The unpack count fixes *how many* EUP pushes a transcendental costs; the split fixes *how the latency of each push is paid*. MLIR emits a transcendental as a single fused `kVector{fn}{F32,Bf16}AndPop` pseudo-op. On a v5+ generation the push and pop cannot live in one bundle, so the late decomposer splits the fused op into a bare hardware push and a deferred pop placed one or more bundles later. The cost-model consequence: a transcendental's latency is a *window* the scheduler fills with unrelated work, not a stall — and the EUP throughput is the orthogonal push→push reservation, not the latency. This is the classic software-pipelining of a long-latency divide, exposed in the instruction stream because a TPU bundle is the issue packet and there is no runtime hazard interlock.

### Algorithm

The fused pseudo-ops occupy opcodes `0x13b`..`0x14d` and are identified by `LloOpcodeIsPseudoEupInstruction` (`@0x1d60c880`):

```c
function LloOpcodeIsPseudoEupInstruction(op):        // 0x1d60c880
    return (op - 0x13b) < 0x13            // op in [0x13b, 0x14d] (19 AndPop ops)
        && (0x7fdff >> (op - 0x13b)) & 1  // bitmask clears bit 9 → excludes 0x144 PushErfAndPop
```

The `0x7fdff` mask (binary `0111 1111 1101 1111 1111`, bit 9 clear) excludes exactly one of the 19 — `0x144 kVectorPushErfAndPop` — leaving 18 fused ops treated as pseudo-EUP. `LloLateDecomposer` (`@0x1269cb20`) walks these and calls `DecomposeEupInstruction` (`@0x126a0340`), a `switch` on the pseudo-opcode (19 cases, `0x13b`..`0x14d`) that dispatches to one of ten `V*Decomposed` builders — `Vpow2`, `Vrecp`, `Vlog2`, `Vrsqrt`, `Vsigshft`, `Vsinq`, `Vcosq`, `Vtanh`, `VpushErf`, `Verf` — each chosen by the opcode and handed a `PrimitiveType` of `11` (the BF16-source F32 form) or `16` (the BF16 form). Each builder is a **bare push + bare pop**, selecting the F32 or BF16 push opcode from the `PrimitiveType` argument:

```c
function VtanhDecomposed(builder, prim_type, value):  // 0x1d555040 (23 lines, byte-exact)
    push_opcode = (prim_type == 0xb) ? 0x128 : 0x132  // 0xb = BF16 → F32 push 0x128; else BF16 push 0x132
    push = CreateVectorEup(push_opcode, value, region)   // @0x1d4d78a0 — bare push, 1 operand
    AppendInstruction(region, push)
    pop  = CreateVectorEupResult(push, region)           // @0x1d4d9820 — deferred pop, hardcodes 0x14e
    AppendInstruction(region, pop)
    // NO inline correction polynomial, NO refinement, NO second push/pop pair
```

`VrsqrtDecomposed` (`@0x1d557b60`) is byte-identical in structure (`push_opcode = (prim_type == 0xb) ? 0x12c : 0x136`), also 23 lines, also bare push + bare pop. `CreateVectorEup` asserts the push opcode is a vector-EUP opcode (`(opcode - 0x128) < 0x13`, i.e. inclusive `0x128`..`0x13a`, the `LloOpcodeIsVectorEup` check) and that the operand `ProducesVreg` (its `opcode_produced_register_type[]` entry is the EUP class `4`); `CreateVectorEupResult` asserts the same push range and emits `New(0x14e, {push}, 1)` — the pop carries no function or width, only the push handle.

> **CORRECTION (EUP-1) —** the raw EUP-datapath trace reported that the `V*Decomposed` builders interleave VALU correction arithmetic (`CreateVectorBinop` / `VfastTwoSum` / Newton refinement) between the push and the pop, and that `VrsqrtDecomposed` issues a second push/pop pair for an rsqrt Newton step. Byte-exact decompilation of both builders (`0x1d555040`, `0x1d557b60`) shows they are bare push + bare pop *only* — 23 lines each, with no `CreateVectorBinop`, no `VfastTwoSum`, and no second push. The `VfastTwoSum` helper (`@0x1d5550a0`) that the earlier trace placed "between" them is a stand-alone Dekker two-sum that merely sits physically adjacent to `VtanhDecomposed` in `.text`; the merged trace conflated adjacent functions. The latency-hiding work in the push→pop window is therefore **unrelated VALU instructions the scheduler interleaves**, not correction emitted by the decomposer. The transcendental's own correction math (where it exists) lives in the `*NoEupF32` software fallbacks and the shared Newton/rational helpers — see [EUP Correction Coefficients](eup-correction-coeffs.md) — not inside the split.

The split is **mandatory** on v5+ because `HasEupRestrictions` is TRUE on Viperfish (`@0x1c458620`) and Ghostlite (`@0x1c458d80`): push and pop cannot co-issue, so the decomposer must place them in separate bundles. On Jellyfish (`@0x1c457b80`) and Pufferfish (`@0x1c4580c0`) the restriction is FALSE, so the fused `AndPop` form can survive — the inverse `Simplify{Tanh,Reciprocal,Sinq,Cosq}AndPop` simplifiers (`0x1d593c60`..) re-fuse a matching push+pop when the schedule allows co-location.

### LLO Opcode Bands (cost-relevant)

| Band | Range | Cost role |
|---|---|---|
| Bare push | `0x128`..`0x13a` | one EUP push (the unit of the `N`-way fan-out); `CreateVectorEup` emits one |
| Fused AndPop | `0x13b`..`0x14d` | MLIR-emitted; the late decomposer splits it on v5+ (`0x144 PushErfAndPop` excluded from pseudo-EUP) |
| Deferred pop | `0x14e` | `kVectorEupResultValue`; drains the push one+ bundle later (`CreateVectorEupResult` hardcodes it) |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LloLateDecomposer` | `0x1269cb20` | splits fused AndPop → bare push + deferred pop | CERTAIN |
| `DecomposeEupInstruction` | `0x126a0340` | `switch` dispatch (19 cases) to 10 `V*Decomposed` builders | CERTAIN |
| `LloOpcodeIsPseudoEupInstruction` | `0x1d60c880` | classifies fused AndPop (range `[0x13b,0x14d]`, mask `0x7fdff`) | CERTAIN |
| `VtanhDecomposed` / `VrsqrtDecomposed` | `0x1d555040` / `0x1d557b60` | bare push + bare pop builders (23 lines each) | CERTAIN |
| `CreateVectorEup` | `0x1d4d78a0` | bare push (`0x128`..`0x13a`, 1 operand, asserts `ProducesVreg`) | CERTAIN |
| `CreateVectorEupResult` | `0x1d4d9820` | deferred pop, hardcodes `0x14e`, asserts push ∈ `0x128`..`0x13a` | CERTAIN |
| `HasEupRestrictions` (VF/GL/JF/PF) | `0x1c458620`/`0x1c458d80`/`0x1c457b80`/`0x1c4580c0` | v5+ separate-bundle constraint (TRUE/TRUE/FALSE/FALSE) | CERTAIN |
| `DecomposeEupOperationsForBarnacore` | `0x1269c5c0` | BarnaCore EUP split (separate result-drain path) | HIGH |

### Considerations

`DecomposeEupOperationsForBarnacore` (`@0x1269c5c0`) also calls `DecomposeEupInstruction`, but drains the EUP result through a BarnaCore `EupResultRead` address-handler rather than the TensorCore `VectorResult` pop — whether its push→pop distance differs from the late decomposer's was not separated (**HIGH**). The pseudo-opcode `0x144 kVectorPushErfAndPop`, excluded from the pseudo-EUP set by the `0x7fdff` mask, is not split by the late decomposer; its handling was not traced and is out of scope for the cost model here.

---

## Latency vs Reservation Orthogonality

### Purpose

The cost model bounds an EUP push by two independent quantities, read from two different mechanisms, and a reimplementer who conflates them gets the schedule wrong. The **push→pop latency** is the depth of the window the deferred pop sits behind — the work the scheduler interleaves to hide it. The **issue reservation** is how many bundles the EUP unit stays busy after a push, bounding back-to-back pushes. They compose as a `max`, not a product.

### Algorithm

`VectorEupReservationCycles` (Target vtable `+0x480`) is the issue-occupancy — the minimum bundles from one push to the next:

```c
function JellyfishTarget::VectorEupReservationCycles():  return 1   // 0x1d490660
function PufferfishTarget::VectorEupReservationCycles():  return 2  // 0x1d494cc0 — half-rate EUP
function ViperfishTarget::VectorEupReservationCycles():  return 1   // 0x1d49b060
function GhostliteTarget::VectorEupReservationCycles():  return 1   // 0x1d497ee0
```

This reservation is applied by the per-instruction resource model (the `GetResourceUsage` matrix + slot tracker), **not** by the push→pop latency edge. The latency edge — the minimum bundles from a push to its drain — is a separate dependency-graph weight; the two are orthogonal.

| quantity | meaning | source | JF | PF | VF | GL |
|---|---|---|---|---|---|---|
| push→pop latency | min bundles push → drain (the correction window) | dependency-graph edge weight | (clamp) | (heap array) | (heap array) | (heap array) |
| `VectorEupReservationCycles` | min bundles push → next push | Target accessor (`+0x480`) | 1 | 2 | 1 | 1 |

> **GOTCHA —** Pufferfish's reservation of 2 (half-rate EUP) does **not** double the push→pop latency. The latency is the window depth; the reservation is the issue rate. For a chain of `M` independent transcendentals on Pufferfish, the EUP-bound schedule length is roughly `2·(M−1) + latency` bundles — the reservation spaces consecutive pushes two bundles apart, and the final pop drains `latency` bundles after the last push. A cost model that multiplies the two (`latency × reservation`) over-counts the chain badly.

The absolute per-generation push→pop latency integers are documented on the [EUP Latency Overview](eup-latency-overview.md) and [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) pages (PF 7, VF 6, GL 13/14, JF clamp 4); they are read from per-instruction heap arrays, not from a single immediate. What this page fixes is the *shape* of the composition — `max(latency-deadline, reservation-availability)` — and that the unpack count `N` multiplies the *number* of push/pop pairs but not the per-pair latency.

### Considerations

The minimum push→pop bundle gap (the latency-edge weight) was confirmed structurally — the decomposer builds the region and the scheduler places the pop after the deferred work — but the per-gen latency integer is not a single `mov` immediate in the split path; it derives from the EUP result-FIFO depth plus the dependency-graph latency model. The numeric latency per generation is therefore documented on the latency-overview / slot pages from the `<Gen>Performance` arrays, and is referenced here, not re-derived. The orthogonality of latency and reservation, and the `N`-fold push-count multiplier, are CERTAIN; the absolute push→pop distance is **HIGH**.

---

## Worked Example — Packed `bf16` rsqrt, Viperfish vs Ghostlite

A `sc.rsqrt` on `vector<…×bf16>` (16-bit packed sub-lane). Both generations register both the 1:1 `UnaryFloatVector` and the 1:N `AluEp` pattern; `IsDynamicallyLegal` picks, and the unpack count follows:

```text
VIPERFISH (SupportsBf16AluInstructions = FALSE → 32-bit lane):
  IsDynamicallyLegal: fmt(bf16)=1 (!=0), SupportsBf16Alu=FALSE → ILLEGAL → AluEp 1:N fires.
  UnpackOperand: lane_bw = 32 > 16 (bf16) → one halving step:
      count = bitwidth(result=32) / bitwidth(sub=16) = 2 pieces; loop exits (32 == 32).
  ⇒ N ≈ 2 × (shape / lane-tiling). Each F32 piece → its own bare 0x12c rsqrt push + 0x14e pop.
  COST: N × (one EUP push/pop). The push count is DOUBLED by the unpack to F32.

GHOSTLITE (SupportsBf16AluInstructions = TRUE → 16-bit lane, elem bw == 16):
  IsDynamicallyLegal: fmt!=0, SupportsBf16Alu=TRUE, elem==16 → LEGAL → NO unpack, 1:1 macro fires.
  tpu_rsqrt_macro → fused 0x149 kVectorRsqrtBf16AndPop; LloLateDecomposer (HasEupRestrictions=TRUE)
  splits it into a bare 0x136 BF16 rsqrt push + 0x14e pop. ONE push/pop; the BF16 ALU runs the
  per-lane op natively.
  COST: 1 × (one EUP push/pop). No unpack.

⇒ The newer generation (GL) processes the packed bf16 rsqrt at HALF the EUP push count of VF,
  because the native BF16 lane lets it skip the unpack to F32 entirely. The unpack count N is the
  cost-model multiplier; SupportsBf16AluInstructions is the single boolean that sets it.
```

---

## Cross-References

- [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) — the push/pop *encoding* (VALU slot 3, 5-bit function selector, `0x14e` pop); this page is the *cost* (unpack count + split) half
- [EUP Latency Overview](eup-latency-overview.md) — the per-generation push→pop latency integers this page treats as orthogonal to the reservation
- [EUP Correction Coefficients](eup-correction-coeffs.md) — where the transcendental correction math actually lives (`*NoEupF32` fallbacks, Newton/rational helpers), since the `V*Decomposed` split carries none
- [Pack/Unpack Precision](../isa/pack-unpack-precision.md) — the `VpackFormat` model and the `kVectorUnpack`/`kVectorPack` LLO ops the AluEp `UnpackFOp`/`PackFOp` lower to
- [VPU (Vector-ALU) Slot](../isa/slot-vpu.md) — the VALU slot family; the EUP push is `Alu3`-only and the interleaved correction work runs on these VALU slots
