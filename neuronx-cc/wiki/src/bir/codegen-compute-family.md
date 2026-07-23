# Codegen — Activation / Tensor-Scalar / Tensor-Tensor / Reduce Family

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310). The codegen leaf bodies live in `libwalrus.so`; `nki_klr_sim` statically links the same code at the same VAs. Other versions will differ.*

## Abstract

This page documents the compute-family leaves of `KlirToBirCodegen` — the KLR-AST → BIR lowerings for the element-wise and reduction engines. These are the workhorses of a Neuron kernel: every activation, every fused scalar/tensor ALU chain, every binary element-wise op, every scan, and every reduction passes through one of the eleven leaves catalogued here. Each leaf is a thin, straight-line translator: it resolves the KLR node's `TensorRef`s into BIR `MemoryLocation` + `AccessPattern` pairs (via the shared sub-encoders documented in [codegen sub-encoders](codegen-sub-encoders.md) and [codegen AccessPattern](codegen-accesspattern.md)), remaps the KLR ALU/func/accumulate enums into the BIR enum space, stamps a handful of mode fields, and emits exactly one BIR instruction. There is no peephole fusion, no multi-pass decomposition, and no scheduling here — those happen downstream.

The reimplementation-critical insight is that **the KLR node taxonomy is far wider than the BIR opcode set**. KLR distinguishes `TensorScalar`, `TensorScalarReduce`, `TensorScalarCumulative`, `NcScalarTensorTensor`, and `TensorTensorScan` as five distinct AST node kinds — but they collapse onto just **two** BIR opcodes, `InstTensorScalarPtr` (IT29) and `InstTensorScalarCache` (IT30), with the variant behaviour selected by *flag and mode fields* rather than by opcode. The same collapse happens on the reduce side: a free-axis `TensorReduce` and a cross-partition `TensorPartitionReduce` both lower to the single opcode `InstTensorReduce` (IT27), the entire difference being one field — the `AxisListType`, hard-coded to `C(5)` for the partition case. These two collapses are the page's central findings; both are reimplementation traps, because a reader who drives off the KLR enum will expect opcodes the BIR layer simply does not have.

For reimplementation, the contract is:

- The KLR→BIR enum remap tables: `codegenActivationFunc` (act-func), `codegenAluOp` (ALU op), `codegenAccumCmd` (accumulate command), `codegenEngine`, `codegenTensorSubDim` (reduce axis) — index bases, range checks, and defaults.
- The two-opcode collapse of the tensor-scalar family and which mode/flag field disambiguates each variant.
- The bias-defaulting machinery for activation (`getActBiasTensor`) and the dual remap tables that make `Activate2` a fused multi-stage op.
- The partition-reduce = `axis=C` lowering and where the cross-lane silicon op is actually synthesized (it is *not* here, and it is *not* a matmul-with-ones).

| | |
|---|---|
| **Binary** | `libwalrus.so` / `nki_klr_sim` (shared VA layout; `.text` VA == file-offset) |
| **Dispatch** | `codegenOperator` master switch → per-op `codegenOperator<Op>Wrapper` thin refcount shim → leaf ([dispatch core](klir-codegen-dispatch.md)) |
| **Activation leaves** | `codegenNcActivate` `0xf18ba0`, `codegenActivate2` `0xf2f3a0`, `codegenExponential` `0xf23fb0`, `codegenReciprocal` `0xf1a5a0` |
| **Tensor-scalar leaves** | `codegenTensorScalar` `0xf2bf80`, `codegenTensorScalarReduce` `0xf2b9f0`, `codegenTensorScalarCumulative` `0xf2aff0`, `codegenNcScalarTensorTensor` `0xf2c450` |
| **Tensor-tensor leaves** | `codegenTensorTensor` `0xf1b820`, `codegenTensorTensorScan` `0xf2b450` |
| **Reduce leaves** | `codegenTensorReduce` `0xf1a140`, `codegenTensorPartitionReduce` `0xf1b3d0` |
| **Enum remaps** | `codegenActivationFunc` `0xf14040`, `codegenAluOp` `0xf140c0`, `codegenAccumCmd` `0xf14110`, `codegenEngine` `0xf140f0`, `codegenTensorSubDim` `0xf140e0` |

> **NOTE — two VA frames.** The public `KlirToBirCodegen::codegen<Op>` symbols in the cp310 export table are 6-byte *thunks* (e.g. `codegenTensorScalar` at the public symbol `0x5eb100`) that tail-call the internal real body. The addresses on this page are the **real-body** VAs (the `0xf…` frame), confirmed against the disassembly. A reimplementer cross-checking by public symbol will land on the thunk; follow its single tail-call to reach the body.

---

## The Shared Lowering Skeleton

Every leaf in this family follows the same shape, so it is documented once. The per-op sections below only describe the *deltas* from this skeleton.

### Algorithm

```c
function codegen<Op>(this, klr_node):          // each real-body leaf
    bb   = this->currentBasicBlock             // this+72
    inst = NamedObjectContainer::insertElement<bir::Inst…>(bb, name)
                                               // name = "<Op>-" + returnAndIncrementId()
    insertInstIntoBlock(this->cursor, inst)    // ilist splice + symtab insert; advances cursor (this+224)

    // operand binding — order is fixed and positional per op:
    apOut = codegenTensorRef(klr_node.out)     // → {MemoryLocation*, SmallVector<APPair,4>, dyn-id, opt<Dtype>}
    apIn  = codegenTensorRef(klr_node.in)
    inst.addArgument(apIn)                      // input AP  → operand-AP list (inst+192)
    inst.addOutput (apOut)                      // output AP → operand-AP list (inst+160)
    inst.addOperandToInst(klr_node.scalar?)     // scalar/extra operands: Imm → ImmediateValue, tensor → AP

    // enum remaps (see "Enum Crosswalks"):
    inst.op0   = codegenAluOp(klr_node.op0)             // or codegenActivationFunc for activation
    inst.engine= codegenEngine(klr_node.engine)         // inst+0x90
    inst.<mode/flag fields> = …                          // the per-op discriminators

    this->hook@+0xE0(inst)                      // post-build legality / registration hook
    return inst
```

The operand resolvers `codegenTensorRef` / `codegenTensorRefImpl` (kind-1 SBUF/PSUM `Access` → `codegenBirAccessPattern`; kind-4 HBM; dynamic-access throws) and the immediate/tensor operand binder `addOperandToInst` are detailed in [codegen sub-encoders](codegen-sub-encoders.md) and [codegen AccessPattern](codegen-accesspattern.md). Broadcast is carried *structurally*, as a stride-0 `APPair` minted upstream in KLR and passed through verbatim — there is no broadcast branch in any leaf (`codegenAP` `0xf13f50` is a pure pass-through copy of `{step, num}`).

### Enum Crosswalks

All five remap helpers are 1-based-input → BIR-ordinal lookups with a range check. They are shared by every leaf in this family (and ~13 other ops). The KLR enum orderings are **alphabetical by member name**; the tables relabel them into the BIR ordinal space.

| Helper | Address | Body | Default / Trap |
|---|---|---|---|
| `codegenAluOp` | `0xf140c0` | `i=klr-1; if(i>0x1C) throw; return dword_1DE9760[i]` | 29-entry table; out-of-range → range-throw |
| `codegenActivationFunc` | `0xf14040` | `i=klr-1; r=30; if(i<=0x18) r=dword_1DE9840[i]; return r` | 25-entry table; **default 30 = Unknown** |
| `codegenAccumCmd` | `0xf14110` | `switch: 1→0 2→1 3→4 4→3 5→throw` | `LoadAccumulate(5)` → "LoadAccumulate is not supported" |
| `codegenEngine` | `0xf140f0` | `i=klr-2; if(i<=5) return dword_1DE9740[i]; else 0` | `unassigned(1)` → 0 |
| `codegenTensorSubDim` | `0xf140e0` | `r=klr-1; if(r>3) throw; return r` | **≤3 clamp** — cannot produce `XYZWC(4)`/`C(5)` |

`codegenAluOp` byte-exact table `dword_1DE9760` (idx = `klrAluOp-1`), maps the 29-member KLR `AluOp` to `bir::AluOpType`:

```text
abs(1)→29   add(2)→4    ashl(3)→2   ashr(4)→3   average(5)→24
and(6)→10   bnot(7)→1   or(8)→11    xor(9)→12   bypass(10)→0
divide(11)→7 is_eq(12)→18 is_ge(13)→21 is_gt(14)→20 is_le(15)→23
is_lt(16)→22 land(17)→13 lor(18)→14  lshl(19)→16  lshr(20)→17
lxor(21)→15  max(22)→8   min(23)→9   mod(24)→27   mult(25)→6
neq(26)→19   pow(27)→26  rsqrt(28)→28 subtract(29)→5
```

> **GOTCHA — the enums are not identity-mapped.** The KLR ordinal and the BIR ordinal differ for almost every member (`is_ge` is KLR 13 but BIR 21; `not_equal` is KLR 26 but BIR 19). A reimplementer who skips the remap and forwards the KLR integer will silently emit the wrong operation — `subtract` would become whatever BIR op is numbered 29. The non-commutative ops (`subtract`, `divide`, shifts, `mod`) are exactly why the reverse/operand-swap flags below exist.

`codegenEngine` table `dword_1DE9740` (idx = `klrEngine-2`): `act(2)→2, dma(3)→4, dve(4)→5, pe(5)→3, pool(6)→1, sp(7)→6`; `unassigned(1)→0`. See [op-family enums](op-family-enums.md) for the full `AluOpType` / `EngineAccumulationType` / `ActivationFunctionType` catalog and the L1/L2/L3 crosswalk.

---

## Activation Engine — NcActivate, Activate2, Exponential, Reciprocal

Four leaves target the activation/scalar datapath. Three of them carry an activation-function selector; the fourth (`Reciprocal`) hard-wires the function in the opcode. The beta-3 forward-builder counterpart of these lowerings is [NKI bircodegen compute](../nki/bircodegen-compute.md).

### codegenNcActivate — the canonical activation lowering

`codegenNcActivate` `0xf18ba0` emits `bir::InstActivation` (IT4) — the general activation. The act-func enum is read from the KLR node and remapped; the bias operand is *always* bound (a real bias if present, otherwise a lazily-created zero bias); the scale operand is a `variant<float|tensor>` that, when scalar, must be float32.

```c
function codegenNcActivate(this, klr):          // 0xf18ba0
    apOut = codegenTensorRef(klr.out)
    apIn  = codegenTensorRef(klr.in)
    func  = codegenActivationFunc(klr.actfunc)  // 0xf14040; *((DWORD*)*a2+9) → table dword_1DE9840, default Unknown(30)
    if klr.has_bias == 0:
        biasMemLoc = getActBiasTensor(this)      // 0xf16bc0 — COMPILER-GENERATED zero bias
        biasAP     = unit 1×1 AP
    else:
        bias = codegenTensorRef(klr.bias)
    switch klr.scale.tag:
        tensor: scaleArg = codegenTensorRef(klr.scale.tensor)
        imm:    scaleVal = codegenImmediate(klr.scale.imm)
                assert holds<float>(scaleVal)    // "scale for activation must be a float32"
        else:   throw "Unrecognized operand type … in activation scale"
    inst = InstBuilder::addActivation(func, biasMemLoc, inMemLoc,
                                      biasAP, inAP, dim, dyn,
                                      scaleArg, scaleVariant, 0.0f, SRC_LOC)   // @addActivation
    inst.acc(+0x124) = codegenAccumCmd(klr.acc)  // EngineAccumulationType
    if klr.has_activation_reduce:
        if klr.reduce_op != Accumulate: throw "Accumulation is the only supported op on activation_reduce"
        if klr.has_accumulator:
            inst.addInputAP(codegenTensorRef(klr.acc))    // bind HW-accumulator output AP
    return inst
```

The remapped `func` ordinal lands at `InstActivation+0x120`. The codegen **does not** load or resolve the PWP look-up-table set — it only stamps the `ActivationFunctionType` ordinal. Which LUT-set must be resident on the Activation engine is decided by a downstream LUT-residency pass that inserts a separate `InstLoadActFuncSet` (IT6) carrying an `act_func_set_id`.

The accumulate path here produces an *accumulating* activation (sets `acc` and binds the accumulator output AP) but does **not** itself emit the drain instruction. The `InstReadActivationAccumulator` (IT5) that drains the hardware accumulator is inserted later by the ActAcc peephole, which fuses a `TensorReduce(add)` over an `InstActivation` output into the accumulating activation. `InstActivation` default-constructs on the Activation engine (`EngineType=2`, external name `Scalar`); `codegenNcActivate` never overrides `inst+0x90`.

#### getActBiasTensor — the default-bias provider

`getActBiasTensor` `0xf16bc0` lazily builds and caches one zero-bias SBUF tensor per codegen instance, so `InstActivation` always has a bias operand even when the KLR node specifies none. On first call it allocates an SB tensor (`InstBuilder::addSBTensor`, name prefix `"COMPILER-GENERATED-zero_bias_act_"`), records it in the cached optional (present flag at `this+288`, pointer at `this+296`), reads its dtype, sizes a unit AP from the partition count, and zeroes it with an `InstBuilder::addMemset` (`"COMPILER-GENERATED-memset_zero_bias_"`). All bias-less activations share this one tensor; the cached optional is guarded by `boost::bad_optional_access` checks.

### codegenActivate2 — the fused multi-stage variant

`codegenActivate2` `0xf2f3a0` lowers to the **same** `bir::InstActivation` opcode but sets `is_activate2 = true` at `+0x128` — the discriminator the BIR/sim uses to take the dual-stage activation datapath. Unlike `NcActivate`, it constructs the inst directly (`insertElement<InstActivation>`, name `"Activate2-"`) and programs a *primary* act-func plus three additional enum slots through a **second, wider remap**:

```c
function codegenActivate2(this, klr):           // 0xf2f3a0
    inst = insertElement<InstActivation>(bb, "Activate2-"+id)
    inst.is_activate2(+0x128) = 1               // ← THE "2" MARKER
    inst.func     (+0x120) = codegenActivationFunc (klr.primaryFunc)   // 25-entry table
    inst.op0      (+0x150) = codegenActivationFuncWide(klr.op0)        // 29-entry WIDE table @0xf14... (dword_8AE500 frame)
    inst.op1      (+0x154) = codegenActivationFuncWide(klr.op1)
    inst.reduce_op(+0x1A8) = codegenActivationFuncWide(klr.reduceOp)
    inst.acc      (+0x124) = codegenAccumCmd(klr.acc)
    inst.reverse0 (+0x158) = klr.reverse0       // bool
    inst.reverse1 (+0x180) = klr.reverse1
    inst.addOutput(apOut);  inst.addInputAP(apIn)
    addOperandToInst(inst, klr.operand2/3/4)    // explicit bias/scale operands (tensor or imm)
    if klr.has_accumulator: inst.addInputAP(codegenTensorRef(klr.accTensor))
    return inst
```

The wide remap (a 29-member act-func enum that includes `Identity`, `Lrelu`, `Prelu`, `Is_finite`, `Abs_reciprocal_sqrt` that the 25-entry table omits) feeds the `op0`/`op1`/`reduce_op` slots. `Activate2` is **not** dual-output — it is a richer *single*-output fused activation that programs a primary `ActivationFunctionType` plus pre/post element-wise ALU ops and a reduce op in one instruction, with the two `reverse` flags controlling operand order on `op0`/`op1`. The offsets and the enum sources match exactly.

> **NOTE — [INFERRED] the semantic type of `op0`/`op1`/`reduce_op`.** Whether these are `AluOpType` or a second `ActivationFunctionType` is ambiguous on this build: the BIR JSON schema types the offsets as `AluOpType`, but the codegen sources them through the *act-func* wide remap, which returns `ActivationFunctionType` ordinals. The two enum spaces overlap at these integer values, and the wire records them at the `AluOp` offsets.

### codegenExponential — the dedicated exp + accumulate primitive

`codegenExponential` `0xf23fb0` emits `bir::InstExponential` (IT103). There is **no** `ActivationFunctionType` field — the function is implicit in the opcode. It runs on the **DVE / Vector engine** (`EngineType=5`), not the Activation engine, and its accumulator-drain is a distinct `InstDveReadAccumulator` (IT102).

```c
function codegenExponential(this, klr):         // 0xf23fb0
    inst = insertElement<InstExponential>(bb, "Exponential-"+id)   // confirmed @0xf24001
    insertInstIntoBlock(this->cursor, inst)
    inst.addOutput(codegenTensorRef(klr.out))
    addOperandToInst(inst, klr.operand2/3)      // exp-offset / extra operands
    inst.addInputAP(codegenTensorRef(klr.in))
    inst.reduce_cmd(+0xF0) = codegenAccumCmd(klr.reduceCmd)   // EngineAccumulationType
    inst.engine(+0x90) = 5                       // EngineType::DVE — a constant, not remapped
    if klr.has_accumulator:                      // fused softmax exp+sum
        dveAcc = insertElement<InstDveReadAccumulator>(bb, "DveReadAccumulator-"+id)
        insertInstIntoBlock(this->cursor, dveAcc)
        dveAcc.addInputAP(codegenTensorRef(klr.accTensor))
        dveAcc.accField(+56)=2; dveAcc.engine(+0x90)=5
    return inst
```

`InstExponential` is the hardware "exp + accumulate" fused softmax primitive: its only op-specific JSON key is `reduce_cmd` (`EngineAccumulationType` at `+0xF0`). On the simulator the modelled engine computes a numerically-stable `expf(x − rowmax)` inline; on real silicon `exp` is a PWP table-driven function. The accumulator drain is `InstDveReadAccumulator` (IT102) rather than the Activation engine's IT5 because Exponential lives on the DVE.

### codegenReciprocal — the pure unary opcode

`codegenReciprocal` `0xf1a5a0` is the simplest body: `bir::InstReciprocal` (IT21), a header-only record with **zero** op-specific JSON keys (the `1/x` is fully implied by the opcode). No func enum, no scale, no bias, no accumulator.

```c
function codegenReciprocal(this, klr):          // 0xf1a5a0
    apOut = codegenTensorRef(klr.out);  apIn = codegenTensorRef(klr.in)
    inst  = InstBuilder::addReciprocal("Reciprocal-"+id,
                outMemLoc, inMemLoc, outAPVec, inAPVec, dim, dyn, SRC_LOC)
    return inst
```

`Reciprocal(25)` *also* exists as a value of the `InstActivation` `func` enum; the compiler may lower a reciprocal either as `InstActivation(func=Reciprocal)` on the Activation engine **or** as the dedicated `InstReciprocal` fast opcode. This leaf always emits the dedicated opcode.

---

## Tensor-Scalar Family — the Two-Opcode Collapse

Four KLR node kinds — `TensorScalar`, `TensorScalarReduce`, `TensorScalarCumulative`, `NcScalarTensorTensor` — collapse onto only **two** BIR opcodes. There is no `InstScalarTensorTensor`, no `InstTensorScalarReduce`, no `InstTensorScalarCumulative` opcode. The variant behaviour is a mode/flag field.

> **QUIRK — the central collapse.** `TensorScalar`, `TensorScalarReduce`, and `NcScalarTensorTensor` all emit `bir::InstTensorScalarPtr` (IT29); only `TensorScalarCumulative` emits `bir::InstTensorScalarCache` (IT30). The "Ptr" suffix is literal: the scalar is bound via `addOperandToInst`, so it can be either an immediate (→ `ImmediateValue`) *or* a runtime pointer/tensor — `InstTensorScalarPtr` exists precisely to carry the pointer form. This collapse is independently corroborated by `birverifier::checkScalarTensorTensor(InstTensorScalarPtr const&)` and `CoreV2GenImpl::generateScalarTensorTensor(InstTensorScalarPtr&)` — both take `InstTensorScalarPtr`. A reimplementer who looks for four opcodes will not find them.

The shared two-stage compute model these ops realize is `out = op1(op0(in, s0), s1)`, with `op0 @+0xF0` and `op1 @+0x120` set from `codegenAluOp`, and operand-order swaps from `setTensorScalarReverseOps` (KLR `TensorScalarReverseOps`: `none/first/second/both` → `reverse0 @+0xF8` / `reverse1 @+0x128`). The four discriminator fields that distinguish the variants:

| KLR node | BIR opcode | Discriminator set by the leaf | Confidence |
|---|---|---|---|
| `TensorScalar` | `InstTensorScalarPtr` (IT29) | `is_tensor_scalar_addr @+0x178` (from `bir::isTensorScalarAddr`) | CERTAIN |
| `TensorScalarReduce` | `InstTensorScalarPtr` (IT29) | `acc @+0x1F0 = 3` (ZeroAccumulate) **+ a second `addOutput`** | CERTAIN |
| `TensorScalarCumulative` | `InstTensorScalarCache` (IT30) | `TSCMode @+0x150 = 1` (Cumulative); `acc @+0x154`; engine pinned `=5` | CERTAIN |
| `NcScalarTensorTensor` | `InstTensorScalarPtr` (IT29) | `is_scalar_tensor_tensor @+0x1A0 = 1`; 3 operands | CERTAIN |

### codegenTensorScalar — plain two-stage scalar op

`codegenTensorScalar` `0xf2bf80` binds one input AP, one output AP, one scalar operand, sets `op0`, and conditionally a second scalar + `op1` (single-scalar when the second is absent: `op1 = bypass(0)`). It then queries `bir::isTensorScalarAddr(inst)` and stamps `is_tensor_scalar_addr @+0x178` — TRUE when the scalar operand is itself a per-partition addressable tensor (the "addr" datapath). This leaf does **not** emit the predicated `InstTensorScalarAffineSelect` (IT62); that is a separate select leaf. The engine is `codegenEngine(klr.engine)` → `inst+0x90`.

### codegenTensorScalarReduce — fused reduce via acc + dual output

`codegenTensorScalarReduce` `0xf2b9f0` is the reduce variant. It realizes a fused reduction on `InstTensorScalarPtr` by two means: `acc @+0x1F0 = 3` (`EngineAccumulationType::ZeroAccumulate` — zero-then-write, the start-of-accumulation fold) **and** a second `addOutput` whose target is the reduce-result tensor. `op0` is the per-element tensor-scalar op; `op1` is the reduce/fold op. The reverse flag is read directly from a plain KLR bool. There is *no* `ReduceCmdType` field — the reduce command is the `EngineAccumulationType` in `+0x1F0`, and the reduce axis comes from the access-pattern extents of the two output APs. *Anchors: `mov dword [r12+1F0h], 3` @ `0xf2bca1`; `addOutput` at `0xf2bc30` and again at `0xf2bd3d`.*

### codegenTensorScalarCumulative — the scan on the cache op

`codegenTensorScalarCumulative` `0xf2aff0` is the only one of the four that emits `InstTensorScalarCache` (IT30) — the CACHE op carries the per-partition running accumulator. It hard-codes `TSCMode @+0x150 = 1` (Cumulative; the enum is `Reduce=0, Cumulative=1, TensorScan=2`), sets `acc @+0x154 = codegenAccumCmd(klr.accum)` to select the scan accumulator init, and **pins the engine to 5** (the cache-bearing vector engine — not `codegenEngine`'d, because the running recurrence is engine-specific). The running recurrence writes `out[i] = acc` at *every* element along the scan dim (vs Reduce, which writes only the final fold); `reverse0`/`reverse1` select forward vs reverse scan direction. *Anchor: `mov dword [r12+150h], 1` @ `0xf2b2ab`.*

### codegenNcScalarTensorTensor — the 3-operand variant

`codegenNcScalarTensorTensor` `0xf2c450` binds **three** operands in order — `[tensor0 (AP arg), scalar (operand), tensor1 (operand)]` — sets two ALU ops, and forces `is_scalar_tensor_tensor @+0x1A0 = 1`. This flag is the bit that turns a generic `InstTensorScalarPtr` into the 3-operand datapath; the canonical compute is `out = (scalar op0 tensor0) op1 tensor1`. The op gate is `birverifier::checkScalarTensorTensor(InstTensorScalarPtr const&)`. No `acc` and no `TSCMode` (IT29 has no mode field).

---

## Tensor-Tensor — the Element-Wise Binary Workhorse

`codegenTensorTensor` `0xf1b820` is the plain binary element-wise op: it binds three `TensorRef`s (`out`, `in0`, `in1` — positional order fixed at KLR slots 0/1/2), maps the single `klr::AluOp` through `codegenAluOp`, sets the engine, and calls `InstBuilder::addTensorTensor` to emit `bir::InstTensorTensor` (IT31) with the ALU op at `+0xF0`. The builder names the inst `"<out>=TensorTensor_<AluOpType2string>(<in0>, <in1>)"`. All 29 `AluOp` values are legal at codegen — there is no per-op filtering; executability on the chosen engine is a downstream verifier concern.

Mixed-input dtypes are illegal at the BIR level: `birverifier::checkTensorTensorInputType` requires both SBUF-resident inputs to share a dtype. The only "promotion" knob is the per-access dtype override applied upstream in KLR (carried by the AP); the codegen forwards whatever each AP declares — there is no automatic max-of-inputs promotion in the leaf. Plain `TensorTensor` has no operand-negate field and no accumulation (the verifier `checkTensorTensor` asserts `acc == Idle` and `engine != Unassigned`).

### codegenTensorTensorScan — scan lowers to the tensor-scalar opcode

`codegenTensorTensorScan` `0xf2b450` does **not** emit `InstTensorTensor`. After the identical 3-`TensorRef` bind it emits `bir::InstTensorScalarPtr` (IT29) — the same opcode as the tensor-scalar family — in "tensor-tensor-scan" guise:

```c
function codegenTensorTensorScan(this, klr):    // 0xf2b450
    op0 = codegenAluOp(klr.op0); op1 = codegenAluOp(klr.op1)
    acc = codegenAccumCmd(klr.accumCmd)
    inst = insertElement<InstTensorScalarPtr>(bb, "TensorTensorScan-"+id)   // IT29, NOT IT31
    inst.op0(+0xF0)=op0;  inst.op1(+0x120)=op1
    setTensorScalarReverseOps(klr.reverseOps, inst)        // reverse0 @+0xF8 / reverse1 @+0x128
    inst.acc(+0x1F0) = acc
    inst.addInputAP(in0);  addOperandToInst(inst, klr.scanInit)  // 4th Operand = scan seed (imm or tensor)
    inst.addInputAP(in1);  inst.addOutput(out)
    inst.is_tensor_tensor_scan(+0x1C8) = 1                 // ★ discriminator
    inst.is_scalar_tensor_tensor(+0x1A0) = 1
    return inst
```

So a scan is an `InstTensorScalarPtr` carrying two AluOps, an `EngineAccumulationType` accumulate command, a scan-init operand (immediate float/int or tensor, via `addOperandToInst`), and `is_tensor_tensor_scan @+0x1C8 = 1` — the same silicon path as the two-stage tensor-scalar op, but with both stage operands being full tensors and the second stage folding a running accumulator along the scan axis (which rides the AP kept-axis order; no explicit axis enum is written). The reverse flags are `MaybeAffine<bool>` (a bool *or* a `QuasiAffineExpr`), hence the variant-reset machinery and the `getIsTensorTensorScanEvalIfNeeded` accessor that resolves the affine predicate at query time. The `op0`/`op1`/`acc`/scan-init wiring is read from the body; that the scan axis is the AP kept-axis follows from the absence of any axis enum on the instruction.

---

## Reduce — the Partition-Reduce = Axis-C Finding

Two leaves target the reduce datapath, and **both lower to the single opcode `bir::InstTensorReduce` (IT27)** via the same builder `InstBuilder::addTensorReduce(name, AxisListType&, AluOpType&, negate&, dst, ifmap, dstAP, ifmapAP, …)`. The entire difference between a free-axis reduce and a cross-partition reduce is the value of **one field**: the `AxisListType`.

### codegenTensorReduce — free-axis reduce

`codegenTensorReduce` `0xf1a140` derives the axis from the KLR op's `opDim` field through `codegenTensorSubDim` (`klr::TensorSubDim {1,2,3,4} → AxisListType {0=X, 1=XY, 2=XYZ, 3=XYZW}`), reads `negate` from the KLR node, and maps the reduce op through `codegenAluOp`. The `≤3` clamp in `codegenTensorSubDim` **structurally forbids** this path from ever selecting `XYZWC(4)` or `C(5)` — the free path is the ordinary engine datapath reduce, where each of the 128 partition lanes folds its own column independently with no cross-lane traffic. The axis value (`AxisListType+1` = count of innermost AP dims collapsed, walking the canonical `[W,Z,Y,X]` order from the innermost free entry) is stored at `InstTensorReduce+0xF4`; it tells the downstream consumer how many trailing AP dims to collapse. There is no multi-pass decomposition — one `addTensorReduce` call, ≤4 contiguous free dims in one shot.

### codegenTensorPartitionReduce — axis hard-coded to C

`codegenTensorPartitionReduce` `0xf1b3d0` is the cross-partition reduce. Its KLR node has **no axis field and no negate field** (the serializer has exactly four fields: `dst`, `op`, `data`, `dtype`). The axis and negate are supplied as codegen *constants*:

```c
function codegenTensorPartitionReduce(this, klr):   // 0xf1b3d0
    dst   = codegenTensorRef(klr.dst)               // @+0x00
    ifmap = codegenTensorRef(klr.data)              // @+0x18  (plain reduce reads +0x10)
    aluop = codegenAluOp(klr.op)                    // @+0x10  (plain reduce reads +0x20)
    axis  = bir::AxisListType::C  (= 5)             // ★ CONSTANT, not derived
    negate= false                                   // ★ CONSTANT — cross-lane reduce cannot negate
    return InstBuilder::addTensorReduce("TensorPartitionReduce-"+id,
                                        axis, aluop, negate, dst, ifmap, …)
```

At the BIR-node layer a partition reduce is indistinguishable from a free-axis reduce except that `axis == C(5)` and `negate == false`.

*Anchors: `mov [rbp+var_214], 5` @ `0xf1b581` (axis slot) and `mov [rbp+var_219], 0` @ `0xf1b570` (negate), marshalled into `addTensorReduce` via `lea rdx,[var_214]` @ `0xf1b618` and `lea r8,[var_219]` @ `0xf1b600`.*

> **QUIRK — partition reduce is NOT a matmul-with-ones.** A reader expecting the systolic-array trick (multiply by a vector of ones to sum across the 128 partition rows) will be wrong. The codegen emits a *single* `InstTensorReduce` with `axis=C`; the cross-partition mechanism is supplied **downstream** by `CoreV2GenImpl::visitInstTensorReduce`, which reads `axis @+0xF4` and, for `axis ∈ {XYZWC(4), C(5)}`, emits a single `NEURON_ISA_TPB_TENSOR4D` descriptor with the cross-lane flag (descriptor byte `+36`) set — the ISA `CROSS_LANE_REDUCE` mode (string verbatim in the encoder). The verifier `birverifier::checkTensorReduce` names the same `{4,5}` family "Cross-lane-reduce", **forbids negate on it** (exactly why codegen pins `negate=0`), and demands 32-element / 32-partition multiples (the array's 32-partition band geometry). Three independent layers — codegen, verifier, encoder — agree: `axis=C` is a dedicated hardware cross-lane datapath, not a synthesized macro-op. The "average" op folds a `1.0/N` divisor at descriptor `+40`.

A mixed free-and-partition reduction that a single op cannot express is split **upstream** in KLR legalization into a `TensorReduce` (free axes) + a `TensorPartitionReduce` (partition axis) pair; the two leaves are strictly 1:1, single-op, zero loops. [INFERRED] — the split point is placed upstream because the leaves themselves contain no decomposition.

---

## Function Map

| Function | Address | KLR node | BIR opcode | Confidence |
|---|---|---|---|---|
| `codegenNcActivate` | `0xf18ba0` | `NcActivate` | `InstActivation` (IT4) | CERTAIN |
| `codegenActivate2` | `0xf2f3a0` | `Activate2` | `InstActivation` (IT4, `is_activate2`) | CERTAIN |
| `codegenExponential` | `0xf23fb0` | `Exponential` | `InstExponential` (IT103) | CERTAIN |
| `codegenReciprocal` | `0xf1a5a0` | `Reciprocal` | `InstReciprocal` (IT21) | CERTAIN |
| `getActBiasTensor` | `0xf16bc0` | — | (lazy zero-bias SB tensor) | CERTAIN |
| `codegenTensorScalar` | `0xf2bf80` | `TensorScalar` | `InstTensorScalarPtr` (IT29) | CERTAIN |
| `codegenTensorScalarReduce` | `0xf2b9f0` | `TensorScalarReduce` | `InstTensorScalarPtr` (IT29) | CERTAIN |
| `codegenTensorScalarCumulative` | `0xf2aff0` | `TensorScalarCumulative` | `InstTensorScalarCache` (IT30) | CERTAIN |
| `codegenNcScalarTensorTensor` | `0xf2c450` | `NcScalarTensorTensor` | `InstTensorScalarPtr` (IT29) | CERTAIN |
| `codegenTensorTensor` | `0xf1b820` | `TensorTensor` | `InstTensorTensor` (IT31) | CERTAIN |
| `codegenTensorTensorScan` | `0xf2b450` | `TensorTensorScan` | `InstTensorScalarPtr` (IT29, scan) | CERTAIN |
| `codegenTensorReduce` | `0xf1a140` | `TensorReduce` | `InstTensorReduce` (IT27) | CERTAIN |
| `codegenTensorPartitionReduce` | `0xf1b3d0` | `TensorPartitionReduce` | `InstTensorReduce` (IT27, `axis=C`) | CERTAIN |
| `codegenActivationFunc` | `0xf14040` | — | act-func enum remap (default 30) | CERTAIN |
| `codegenAluOp` | `0xf140c0` | — | `AluOp` → `AluOpType` remap | CERTAIN |
| `codegenAccumCmd` | `0xf14110` | — | `AccumCmd` → `EngineAccumulationType` | CERTAIN |
| `codegenEngine` | `0xf140f0` | — | `Engine` → `EngineType` remap | CERTAIN |
| `codegenTensorSubDim` | `0xf140e0` | — | `TensorSubDim` → `AxisListType` (≤3) | CERTAIN |

> **NOTE — where the recovery stops.** KLR node *field names* are reconstructed from offsets and behaviour, since the KLR side ships no debug symbols; the offset→role mapping is pinned by the BIR target offsets the setters write. [INFERRED] The `Activate2` `op0`/`op1`/`reduce_op` enum *type* (`AluOpType` vs a second `ActivationFunctionType`). [SPECULATIVE] The exact silicon micro-sequence of the cross-lane datapath, which sits below the ISA-descriptor abstraction these binaries expose.

## Cross-References

- [KlirToBirCodegen Dispatch Core](klir-codegen-dispatch.md) — the master `codegenOperator` switch and the per-op wrapper shims that reach these leaves
- [codegen AccessPattern Primitive](codegen-accesspattern.md) — `codegenAccess` / `codegenBirAccessPattern` / `codegenAP`, the TensorRef → AP resolver every leaf calls
- [codegen Sub-Encoders](codegen-sub-encoders.md) — `codegenTensorRef`, `addOperandToInst`, `insertInstIntoBlock`, the name counter
- [Op-Family Enums & the L1/L2/L3 Crosswalk](op-family-enums.md) — `AluOpType`, `EngineAccumulationType`, `ActivationFunctionType` (31), `AxisListType`, `PoolFunctionType`
- [NKI BirCodegen Compute](../nki/bircodegen-compute.md) — the beta-3 forward-builder counterpart of these activation/TS/TT/reduce lowerings
- [Activation Encoding](../isa/activation-encoding.md) — the L3 `InstActivation` encoding and the PWP id space behind the act-func ordinal
- [Pool/Reduce Encoding](../isa/pool-reduce-encoding.md) — the L3 reduce descriptor and the `CROSS_LANE_REDUCE` cross-lane bit that `axis=C` becomes
- [TensorScalar Encoding](../isa/tensorscalar-encoding.md) — the L3 encoding of `InstTensorScalarPtr` / `InstTensorScalarCache`
