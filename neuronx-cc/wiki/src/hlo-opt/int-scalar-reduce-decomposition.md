# Integer All-Reduce and Scalar-Reduce Decomposition

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (`neuronxcc/starfish/bin/hlo-opt`, cp310). VA == file offset holds for `.text` (0x1e6e960–0x9a1e4a2) and `.rodata` (0x20c940–0xd92a90). Other versions will differ.*

## Abstract

Two `hlo-opt` passes exist solely to route around a hardware limit: the Trillium-class collective and reduce hardware natively reduces **floating-point** types, and the engine reduce primitive operates on the 128-partition SBUF axis rather than on a scalar. When the incoming HLO graph asks for an *integer* cross-replica sum or a *scalar* non-`Add` reduction, neither maps onto a single native primitive. `DecomposeIntAllReduce` (registry order 88, `aws_neuron_decompose_int_all_reduce`) and `DecomposeScalarReduce` (registry order 74, `aws_neuron_decompose_scalar_reduce`) rewrite each into a subgraph the back-end *can* lower.

Both are `xla::OpExpanderPass` subclasses — the LLVM analogue is a peephole that supplies `InstructionMatchesPattern` (the gate) and `ExpandInstruction` (the rewrite) while the shared base `xla::OpExpanderPass::Run` @ `0x29f0bb0` walks every computation and instruction, fires the gate, and splices the rewrite's output in place of the matched op. Neither pass carries config or threshold state: both are default-constructed 48-byte objects whose only field is the vtable pointer. This is the inverse of the threshold-driven collective combiners, which read tuned byte limits out of `[rbx+0xDA0/...]`.

`DecomposeIntAllReduce` turns a scalar integer `all-reduce(Add)` into *gather every replica's contribution, then sum locally with an unrolled integer `kAdd` tree* — there is no integer collective reduce, so the cross-replica sum becomes an `all-gather` followed by explicit adds. `DecomposeScalarReduce` turns a scalar-output, non-`Add` reduction over a rank-2, 128-divisible operand into a **two-stage 128-partition reduce**: reshape to `[128, N/128]`, reduce the free axis, reduce the partition axis. The structure mirrors how the [Pool engine](../arch/pool-engine.md) lays a reduction across its 128 lanes. This page documents both gates byte-for-byte and reconstructs both rewrites as annotated pseudocode.

For reimplementation, the contract is:

- The two gates: opcode, reduction-computation-root, element-type, and shape predicates — including the `(et-4)&~4` narrowing that limits the integer all-reduce to `{S32, U32}`, and the `& 0x7F == 0` divisibility test that gates the scalar reduce.
- The two emitted subgraphs in build order, naming the real `Make*Hlo` / `CreateAllGather` builders and the loop that unrolls the `kAdd` tree.
- *Why* each subgraph is shaped as it is: the integer-collective gap, the 128-lane reduce geometry, and the `maximum(x,x)` anti-fusion barrier wedged between the two reduce stages.

| | |
|---|---|
| **Pass base** | `xla::OpExpanderPass` — shared `Run` @ `0x29f0bb0` |
| **IntAllReduce gate** | `xla::hilo::DecomposeIntAllReduce::InstructionMatchesPattern` @ `0x1f77eb0` (261 B) |
| **IntAllReduce rewrite** | `…::ExpandInstruction` @ `0x1f78230` (2433 B), `.cold` @ `0x1f780d4` |
| **IntAllReduce vtable / name()** | `_ZTV` @ `0x410c00` (vptr `0x410c10`) · `name()` @ `0x1f77ea0` → str @ `0x34bfa0` |
| **ScalarReduce gate** | `xla::hilo::DecomposeScalarReduce::InstructionMatchesPattern` @ `0x1f78bd0` (382 B) |
| **ScalarReduce rewrite** | `…::ExpandInstruction` @ `0x1f78e80` (1324 B), `.cold` @ `0x1f78dbe` |
| **ScalarReduce vtable / name()** | `_ZTV` @ `0x410d00` (vptr `0x410d10`) · `name()` @ `0x1f78bc0` → str @ `0x387ad8` |
| **Registrar** | `RegisterHiloHloPasses` @ `0x1e72270`; factories `_M_invoke` @ `0x1e719d0` (#88), `0x1e71b10` (#74) |
| **Config state** | none — both default-construct a 48-byte (0x30) object, vtable ptr only |

---

## Why the hardware forces these rewrites

The TPB collective datapath and the engine reduce primitive are both float-shaped. A native `all-reduce` lowers to a hardware collective that sums floating-point payloads across replicas; there is no integer-add collective. Likewise the engine reduce primitive sweeps a reduction down the 128-partition SBUF axis (see [Pool Engine — Windowed Pooling and the Reduce Leg](../arch/pool-engine.md)) — it reduces *along partitions*, not *to a bare scalar from an arbitrary rank-2 layout*. Two distinct gaps, two distinct decompositions:

- **Integer cross-replica sum.** Move the data, not the math. `all-gather` is type-agnostic — it just transports bytes — so the pass replaces the integer collective reduce with an `all-gather` that brings every replica's scalar onto every device, then sums the gathered values with an *ordinary integer `kAdd` tree* built from `MakeBinaryHlo(kAdd, …)`. The cross-replica reduction now runs on the same integer ALU path that any local `add` uses.
- **Scalar non-`Add` reduce.** Match the data to the engine geometry. A scalar output from a rank-2 operand is reshaped to `[128, N/128]` so the reduction tiles onto the 128-lane partition axis; the free axis is reduced first (`[128, N/128] → [128]`), then the partition axis (`[128] → scalar`). This is the same two-stage shape the [Pool engine reduce leg](../arch/pool-engine.md) wants.

The same "integer hardware gap" theme recurs in the matmul lowering — see [Precision & Upcast Passes (IntMatmulDowncast)](precision-upcast-passes.md) (4.23), which downcasts integer matmuls because the PE array's accumulate path is also float-native.

> **NOTE —** the split of labour between the two passes is deliberate and the gates encode it. `DecomposeIntAllReduce` fires *only* on `Add` reductions; `DecomposeScalarReduce` explicitly *excludes* `Add` reductions (`kAdd → return false`). Integer `Add` reductions that are *not* collectives are left for other lowerings; this pass takes `maximum/minimum/multiply/and/or/…`.

---

## DecomposeIntAllReduce (#88)

### Purpose

Rewrite an effectively-scalar `S32`/`U32` `all-reduce(Add)` into `reshape → broadcast → all-gather → strided slices → kAdd-tree → reshape`, replacing the (nonexistent) integer collective reduce with an `all-gather` plus a local integer sum.

### Entry Point

```text
xla::OpExpanderPass::Run                         @ 0x29f0bb0  ── shared driver, walks all instrs
  └─ DecomposeIntAllReduce::InstructionMatchesPattern @ 0x1f77eb0  ── gate (vptr+0x28)
  └─ DecomposeIntAllReduce::ExpandInstruction        @ 0x1f78230  ── rewrite, on match
       └─ .cold @ 0x1f780d4                       ── exception unwind only (no error string)
```

### Algorithm — gate

```c
// DecomposeIntAllReduce::InstructionMatchesPattern @ 0x1f77eb0
bool MatchesPattern(HloInstruction *I):
    if I->opcode() != kAllReduce(7):        return false   // cmp byte[rsi+14h],7   @0x1f77eb0
    comp = I->called_computations()[0]                     // 0x964c8a0; low-2-bit tagged ptr unpacked
                                                           //   and edx,3 / and rax,~3  @0x1f77edb
    root = comp->root()  // comp[+8]
    if root->opcode() != kAdd(1):           return false   // cmp byte[rax+14h],1   @0x1f77ef0
    et = I->shape().element_type()                         // 0x9650370
    if !IsIntegralType(et):                 return false   // CSWTCH classifier, see §classifier  @0x1f77f0d
    if ShapeUtil::TrueNumDimensions(I->shape()) != 0:      // 0x97dbf40 — every dim size-1
                                            return false   // @0x1f77f28
    et = I->shape().element_type()                         // re-read @0x1f77f35
    return ((et - 4) & ~4) == 0                            // sub eax,4 / and eax,0FFFFFFFBh / setz  @0x1f77f3c
```

The final mask is the subtle part. `IsIntegralType` passes *all* integer primitive types, but `(et-4) & ~4 == 0` is true only when `et-4 ∈ {0, 4}` — i.e. `et ∈ {S32 = 4, U32 = 8}`. So the broad integral gate is immediately narrowed: the pass fires only on **signed/unsigned 32-bit** scalar all-reduce-add. (CONFIRMED — `sub eax,4 / and eax,0FFFFFFFBh / setz al` @ `0x1f77f3c`.)

> **GOTCHA —** do not read the `IsIntegralType` call as the type gate. It is necessary but not sufficient; the `(et-4)&~4` mask is what actually decides. A reimplementation that accepts every integral type here will fire on `S8`/`S64`/`U8`/`U16`/`U64` all-reduces the real pass leaves untouched.

### Algorithm — rewrite

`AR = all-reduce(Add, operand0)`, `operand0` effectively-scalar `S32`/`U32`, `G = replica_groups[0].size()`.

```c
// DecomposeIntAllReduce::ExpandInstruction @ 0x1f78230
HloInstruction *Expand(HloInstruction *AR):
    s2   = ShapeUtil::MakeValidatedShape(et, {2})              // 0x97e13d0  @0x1f78290 — shape [2]
    op0  = AR->mutable_operand(0)                              // 0x965ea50  @0x1f782d7 — the scalar input
    rs   = MakeReshapeHlo(s2 /*[2]*/, op0)                     // 0x90f0950  @0x1f78342 — scalar → [2]   (*)
    bc   = MakeBroadcastHlo(rs, /*dims*/{}, s2 /*[2]*/)        // 0x90f0890  @0x1f7836f — duplicate to 2-vector

    arI  = Cast<HloAllReduceInstruction>(AR)                   // 0x1eeebf0  @0x1f7837e — read collective flags:
        constrain_layout      = arI[+0x250]                    //            @0x1f7841b
        use_global_device_ids = arI[+0x251]                    //            @0x1f78403
        channel_id (optional) = arI[+0x208 .. 0x217]           // movdqu     @0x1f78412
        replica_groups        = arI.collective_device_list().replica_groups()  // 0x96240f0  @0x1f78390

    count = 2 * replica_groups[0].size()                       // r12 = [grp+0x10] * 2   @0x1f783ae
    sAG   = ShapeUtil::MakeValidatedShape(et, {count})         //            @0x1f783bf — shape [2*G]
    AG    = HloInstruction::CreateAllGather(                   // 0x96680b0  @0x1f784a5
                sAG /*[2*G]*/, {bc}, /*all_gather_dim=*/0, replica_groups,
                constrain_layout, channel_id, use_global_device_ids)
    AG    = computation.AddInstruction(AG)                     // 0x96370d0  @0x1f784bf

    // --- unrolled cross-replica kAdd tree over the gathered [2*G] vector ---
    acc = MakeSliceHlo(AG, starts={0}, limits={2}, strides={1})   // 0x90f1770  @0x1f78551 — first [2] chunk
    for (b = 2; b < count; b += 2):                               //            @0x1f78571 .. 0x1f7863e
        sl  = MakeSliceHlo(AG, starts={b}, limits={b+2}, strides={1})  //         @0x1f785d0 — next [2] chunk
        acc = MakeBinaryHlo(kAdd(1), acc, sl)                    // 0x90f2760  @0x1f78605 — integer add
    tail = MakeSliceHlo(AG, starts={1}, limits={2}, strides={1})  //            @0x1f78696 — carve a scalar lane
    out  = MakeReshapeHlo(AR->shape(), /*acc-or-tail*/)          //            @0x1f786c4 — back to scalar
    return out                                                   // StatusOr<HloInstruction*>
```

The cross-replica reduction is an explicit **unrolled `kAdd` tree** — there is no `MakeReduceHlo` in this body. Each gathered replica contributes a `[2]` chunk; the loop slices chunk `b..b+2` and folds it into the accumulator with `MakeBinaryHlo(kAdd, …)`. (CONFIRMED — three `MakeSliceHlo` call sites @ `0x1f78551`/`0x1f785d0`/`0x1f78696` and a `MakeBinaryHlo` @ `0x1f78605` inside the loop body.)

> **QUIRK —** the `(*)` reshape from a 1-element scalar to `[2]` is not element-count-preserving in stock XLA. Combined with `MakeBroadcastHlo(rs, {} → [2])` and the trailing `slice[1:2]`, it reads as a **pad-to-even-payload idiom**: materialize the scalar as a 2-vector so the `all-gather` has an even per-replica payload, gather `2×G`, then keep one lane per replica. The graph topology (gather-`2G` → `G` strided `[2]`-slices → `kAdd` tree → reshape-to-scalar) is CONFIRMED.

> **GOTCHA —** the precise *intra-pair lane semantics* are INFERRED (MEDIUM). Which of the two lanes in each gathered `[2]` carries the live value, and whether the final `MakeReshapeHlo` consumes the `kAdd` accumulator (`acc`) or the trailing `slice[1:2]` (`tail`), is ambiguous in the disassembly: the overlapping `StatusOr` stack slots (`var_560`/`var_570`/`var_580`) alias the operand of the closing reshape. Resolving it needs a live HLO dump of the pass output. Everything *above* the final reshape's operand choice is binary-true.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `InstructionMatchesPattern` | `0x1f77eb0` | gate: opcode 7 + root kAdd + integral + scalar + `{S32,U32}` | CONFIRMED |
| `ExpandInstruction` | `0x1f78230` | rewrite: reshape→broadcast→all-gather→slice→kAdd-tree→reshape | CONFIRMED (topology); MEDIUM (final-reshape operand) |
| `ShapeUtil::MakeValidatedShape` | `0x97e13d0` | builds `[2]` and `[2*G]` shapes | CONFIRMED |
| `HloInstruction::CreateAllGather` | `0x96680b0` | the type-agnostic transport replacing the int collective | CONFIRMED |
| `MakeSliceHlo` | `0x90f1770` | carves per-replica `[2]` chunks | CONFIRMED |
| `MakeBinaryHlo` | `0x90f2760` | `kAdd` fold in the tree | CONFIRMED |
| `MakeReshapeHlo` | `0x90f0950` | scalar↔`[2]` reshapes | CONFIRMED |
| `MakeBroadcastHlo` | `0x90f0890` | duplicate scalar to `[2]` | CONFIRMED |

---

## DecomposeScalarReduce (#74)

### Purpose

Rewrite a scalar-output, **non-`Add`** integer `reduce` over a rank-2, 128-divisible operand into a two-stage 128-partition reduce: `reshape([128, N/128]) → reduce(free axis) → maximum(self,self) → reduce(partition axis) → reshape`, mapping the reduction onto the 128-lane engine layout.

### Entry Point

```text
xla::OpExpanderPass::Run                         @ 0x29f0bb0  ── shared driver
  └─ DecomposeScalarReduce::InstructionMatchesPattern @ 0x1f78bd0  ── gate (vptr+0x28)
  └─ DecomposeScalarReduce::ExpandInstruction        @ 0x1f78e80  ── rewrite, on match
       └─ .cold @ 0x1f78dbe                       ── exception unwind only (no error string)
```

### Algorithm — gate

```c
// DecomposeScalarReduce::InstructionMatchesPattern @ 0x1f78bd0
bool MatchesPattern(HloInstruction *I):
    if I->opcode() != kReduce(0x55):        return false   // cmp byte[rsi+14h],55h  @0x1f78bd0
    et = I->shape().element_type()                         // 0x9650370 — OUTPUT element type
    if !IsIntegralType(et):                 return false   // CSWTCH classifier  @0x1f78c0a
    if ShapeUtil::TrueNumDimensions(I->shape()) != 0:      // output must be scalar
                                            return false   // 0x97dbf40  @0x1f78c18
    comp = I->called_computations()[0]; root = comp->root()  // 0x964c8a0
    if root->opcode() == kAdd(1):           return false   // *** Add reductions EXCLUDED ***  @0x1f78c45
    in   = I->mutable_operand(0)                           // 0x965ea50  @0x1f78c50 — reduced tensor
    if (in->shape().array_state().rank) != 2:  return false  // 0x97d18e0; [state]>>1==2  cmp eax,2  @0x1f78c6e
    prod = product(in->shape().dims())                    // loop @0x1f78cf0 .. 0x1f78d1f
    return (prod & 0x7F) == 0                              // and r13d,7Fh / jz  @0x1f78d21 — prod % 128 == 0
```

Read against the IntAllReduce gate, every axis is inverted or extended. The opcode is `kReduce` (`0x55` = 85; case label "reduce" in the `HloOpcodeString` switch @ `0x96bb550`). The reduction computation must be **non-`Add`** — `kAdd → return false` @ `0x1f78c45` is the mirror of the IntAllReduce requirement. The element-type gate is *just* `IsIntegralType` with no `{S32,U32}` narrowing: any integral output dtype that classifies true passes. And there are two extra structural predicates the all-reduce gate lacks — operand rank exactly 2 (`cmp eax,2` @ `0x1f78c6e`) and total element count divisible by 128 (`and r13d,7Fh` @ `0x1f78d21`).

> **NOTE —** the `& 0x7F == 0` test is exact divisibility by 128, not a floor or a clamp. The rewrite relies on it: `N/128` is computed by `sar rax,7` (arithmetic shift) @ `0x1f78f80`, which is only the true quotient when `N % 128 == 0`. The gate guarantees the precondition the rewrite assumes.

### Algorithm — rewrite

`R = reduce(operand0, init, computation=Rc, dims=…)`, scalar integral output, `operand0` rank-2 with `N = product(dims)`, `N % 128 == 0`.

```c
// DecomposeScalarReduce::ExpandInstruction @ 0x1f78e80
HloInstruction *Expand(HloInstruction *R):
    op0  = R->mutable_operand(0)                              // 0x965ea50  @0x1f78eb2 — reduced tensor
    init = R->mutable_operand(1)                              //            @0x1f78ec6 — reduce init value
    Rc   = R->called_computations()[0]                        // 0x964c8a0  @0x1f78ed5 — the reduce comp (r15)
    meta = R[+0x200]                                          //            @0x1f78ef1 — OpMetadata*, threaded everywhere

    N    = product(op0->shape().dims())                       // loop @0x1f78f20 .. 0x1f78f4f
    s2d  = ShapeUtil::MakeValidatedShape(et, {128, N/128})    // 0x97e13d0
        // dims[0] = 0x80 = 128         (mov var_2D0, 80h  @0x1f78f75)
        // dims[1] = (N + 0x7F) sar 7   (sar rax,7         @0x1f78f80)  == N/128 since N%128==0

    rs   = MakeReshapeHlo(s2d /*[128, N/128]*/, op0)          // 0x90f0950  @0x1f78fe2 — flatten to engine tiling
    r1   = MakeReduceHlo(rs, init, dims={1}, comp=Rc, meta)   // 0x90f0f60  @0x1f7902f — reduce FREE axis → [128]
    b1   = MakeBinaryHlo(kMaximum(0x43), r1, r1, meta)        // 0x90f2760  @0x1f79066 — maximum(r1,r1) — barrier
    r2   = MakeReduceHlo(b1, init, dims={0}, comp=Rc, meta)   // 0x90f0f60  @0x1f790b3 — reduce PARTITION axis → scalar
    out  = MakeReshapeHlo(R->shape(), r2)                     // 0x90f0950  @0x1f790e6 — back to scalar output
    return out
```

The reshape target is exactly `[128, N/128]`: `0x80` is moved into the `dims[0]` slot and `(N+0x7F) sar 7` into `dims[1]`. This is the 128-partition SBUF tiling. The first `MakeReduceHlo` reduces **dim `{1}`** (the free axis) to `[128]`; the second reduces **dim `{0}`** (the partition axis) to a scalar. Both stages reuse the *same* reduction computation `Rc` (`r15`) and the *same* `init`, and both pass the original op's `OpMetadata*` (`R[+0x200]`). (CONFIRMED — two `MakeReduceHlo` call sites @ `0x1f7902f`/`0x1f790b3`; `mov var_2D0, 80h` @ `0x1f78f75`; `sar rax,7` @ `0x1f78f80`.)

> **QUIRK —** wedged between the two reduces is `MakeBinaryHlo(kMaximum, r1, r1)` — `mov esi, 0x43` @ `0x1f79057` (kMaximum, case "maximum" in `HloOpcodeString`) with both operands set to `r1` (`rcx = rdx`). `maximum(x, x) == x` is a pure identity on values; it changes nothing about the result. It is almost certainly a deliberate **anti-fusion sentinel** placed between the two stages so a later fusion / reduce-merge pass cannot collapse the two-stage partition reduce back into a single reduce — which would re-introduce the very shape the pass was built to avoid. (Opcode + self-operand CONFIRMED; barrier *intent* INFERRED, HIGH.)

> **GOTCHA —** the `init` value is reused verbatim on *both* stages. For an idempotent identity element (e.g. `-inf` for `maximum`, `+inf` for `minimum`, `1` for `multiply`) this is correct; the two-stage reduce produces the same scalar as the original single reduce. A reimplementation that injects a fresh or stage-specific init will silently corrupt non-`Add` reductions whose identity is not the operand-neutral default.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `InstructionMatchesPattern` | `0x1f78bd0` | gate: opcode 0x55 + integral + scalar + non-kAdd + rank-2 + `%128==0` | CONFIRMED |
| `ExpandInstruction` | `0x1f78e80` | rewrite: reshape[128,N/128]→reduce(1)→max(self)→reduce(0)→reshape | CONFIRMED |
| `ShapeUtil::MakeValidatedShape` | `0x97e13d0` | builds the `[128, N/128]` shape | CONFIRMED |
| `MakeReshapeHlo` | `0x90f0950` | flatten and re-scalarize | CONFIRMED |
| `MakeReduceHlo` | `0x90f0f60` | the two reduce stages (dims `{1}` then `{0}`) | CONFIRMED |
| `MakeBinaryHlo` | `0x90f2760` | `kMaximum(self,self)` anti-fusion barrier | CONFIRMED (op); INFERRED (intent) |
| `Shape::array_state` | `0x97d18e0` | rank check on operand(0) | CONFIRMED |

---

## Shared dtype classifier — `IsIntegralType`

Both gates inline the same `primitive_util::IsIntegralType(element_type)` predicate as a small jump over three byte tables in `.rodata`. The linker emitted two byte-identical copies, one per pass:

| Axis | DecomposeIntAllReduce | DecomposeScalarReduce | Source |
|---|---|---|---|
| range `elem-6` (≤ 0x19) | `CSWTCH_341` @ `0x410b70` | `CSWTCH_278` @ `0x410c60` | disasm range checks + table dump |
| range `elem-2` (≤ 0x1c) | `CSWTCH_339` @ `0x410b90` | `CSWTCH_276` @ `0x410c80` | same |
| range `elem-0x13` (≤ 0x0e) | `CSWTCH_335` @ `0x410bb0` | `CSWTCH_272` @ `0x410ca0` | same |
| high path (elem > 0x21) | bitmask `0xFFFFFFFCFFFB7FFF` | bitmask `0xFFFFFFFCFFFB7FFF` | disasm |

`CSWTCH_341`/`CSWTCH_278` are indexed `element_type − 6` and decode to the standard XLA "is this an integer primitive type" test (indices `0..3` = `U8/U16/U32/U64` = 1; floating types = 0). The `cmp eax,1` at the head of each classifier is the `PRED`-as-integral special case that jumps straight to the scalar-dimension check. (CONFIRMED — `cmp ds:CSWTCH_341[rdx],0` @ `0x1f77f1a`, `cmp ds:CSWTCH_278[rdx],0` @ `0x1f78c0a`.)

The IntAllReduce path then *additionally* narrows to `{S32, U32}` via the `(et-4)&~4` mask (above); ScalarReduce accepts the full integral set the classifier passes.

---

## Gate comparison (quick reference)

| Condition | DecomposeIntAllReduce (#88) | DecomposeScalarReduce (#74) |
|---|---|---|
| Opcode | `kAllReduce` (7) | `kReduce` (0x55) |
| Reduction-comp root | **must be `kAdd`** (1) | **must NOT be `kAdd`** (1) → excludes int-sum |
| Element type | integral **AND ∈ {S32, U32}** | any integral (`IsIntegralType`) |
| Output shape | `TrueNumDimensions == 0` (effectively scalar) | `TrueNumDimensions == 0` (scalar) |
| Operand rank | (scalar) | operand(0) rank **== 2** |
| Operand size | (1 element) | `product(dims) % 128 == 0` |
| Constructor config | none (stateless 0x30 object) | none (stateless 0x30 object) |

## Emitted-graph shapes (quick reference)

```text
DecomposeIntAllReduce  (G = replica_groups[0].size):
  scalar S32/U32 ─reshape→ [2] ─broadcast→ [2] ─all-gather(dim0, G)→ [2*G]
     ─{slice[0:2], slice[2:4], …}→ G×[2] ─kAdd-tree→ [2] ─slice[1:2]→ [1] ─reshape→ scalar
  (all-gather carries the original AllReduce's {constrain_layout, channel_id?,
   use_global_device_ids, replica_groups}; cross-replica reduce is an explicit integer kAdd tree)

DecomposeScalarReduce  (N = product(operand dims), N % 128 == 0):
  [d0,d1] (rank-2, N elems) ─reshape→ [128, N/128]
     ─reduce(dim=1, comp=Rc)→ [128] ─maximum(self,self)→ [128] ─reduce(dim=0, comp=Rc)→ scalar
     ─reshape→ scalar(orig)
  (same comp Rc and init on both stages; maximum(x,x) is an anti-fusion barrier)
```

---

## Adversarial self-verification

The five strongest claims, re-challenged against the binary:

1. **IntAllReduce gates `{S32, U32}`, not all integers.** Re-checked `0x1f77f3c`: `sub eax,4 / and eax,0FFFFFFFBh / setz al`. `et-4 ∈ {0,4} ⇒ et ∈ {4,8} = {S32,U32}`. **Holds — CONFIRMED.**
2. **ScalarReduce excludes `Add` reductions.** Re-checked `0x1f78c45`: `cmp byte[rax+14h],1` on the reduction-comp root, branching to the reject path. `kAdd == 1`. **Holds — CONFIRMED.**
3. **The 128-partition split is exactly `[128, N/128]`.** Re-checked `0x1f78f75`–`0x1f78f80`: `mov var_2D0, 80h` then `sar rax,7`. `0x80 = 128`, `sar …,7 = ÷128`. **Holds — CONFIRMED.**
4. **The cross-replica sum is an unrolled `kAdd` tree, not a `MakeReduceHlo`.** Re-checked the IntAllReduce rewrite call list: `MakeSliceHlo` ×3 + `MakeBinaryHlo` inside the loop @ `0x1f78605`; no `MakeReduceHlo` symbol anywhere in `0x1f78230`'s body. **Holds — CONFIRMED.**
5. **The `maximum(r1,r1)` is a true self-operand barrier.** Re-checked `0x1f79057`: `mov esi,0x43` (kMaximum) and the operand registers (`rcx = rdx = r1`) into `MakeBinaryHlo`. Opcode and self-operand **CONFIRMED**; the "anti-fusion sentinel" *purpose* is **INFERRED (HIGH)** — derived from topology, with no confirming string or comment in the binary.

Residual tags: IntAllReduce intra-pair lane semantics and the final-reshape operand (acc vs tail) — **INFERRED (MEDIUM)**, ambiguous due to aliased `StatusOr` stack slots. `R[+0x200]` as the `OpMetadata*` block — **STRONG**, consistent with its use as the metadata arg to all three builders but not cross-validated against an independent struct map. No diagnostic/error strings are emitted by either rewrite; both `.cold` paths are pure unwind, so there is no `NCC_*` code for these passes.

---

## Related Components

| Name | Relationship |
|---|---|
| `xla::OpExpanderPass::Run` @ `0x29f0bb0` | shared driver that fires both gates and splices both rewrites |
| `RegisterHiloHloPasses` @ `0x1e72270` | registers both via stateless factories (#88, #74) |
| Collective combiners (#77/#78/#79) | the *threshold-driven* counterparts — read tuned byte limits, unlike these stateless passes |
| `HloOpcodeString` switch @ `0x96bb550` | source of the opcode→name confirmations (reduce=0x55, maximum=0x43, add=1, all-reduce=7) |

## Cross-References

- [Pool Engine — Windowed Pooling and the Reduce Leg](../arch/pool-engine.md) — the 128-partition reduce geometry that `DecomposeScalarReduce` targets; *why* float-native reduce forces the rewrite
- [Precision & Upcast Passes (IntMatmulDowncast)](precision-upcast-passes.md) — 4.23; same integer-hardware-gap theme: the PE array's accumulate path is float-native, so integer matmuls are downcast
- [AllReduce/ReduceScatter/AllGather Combiners & Threshold Model](collective-combiners.md) — the threshold-driven collective passes these stateless decompositions sit beside
- [AllReduce→ReduceScatter & DynamicSlice Rewrites](allreduce-dynslice-rewrites.md) — sibling collective rewrites in the same `OpExpander` family
- [The hlo-opt Pass Registry (the `--passes` Table)](pass-registry.md) — registry orders 88 and 74, and the `RegisterHiloHloPasses` factory shape
