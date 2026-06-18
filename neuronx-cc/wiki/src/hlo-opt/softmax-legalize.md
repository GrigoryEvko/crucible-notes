# Softmax Legalization

> *All addresses on this page apply to neuronx-cc `hlo-opt` (and `hlo2penguin`) from `neuronx_cc 2.24.5133.0+58f8de22` (cp310, x86-64). Other versions and other Python ABIs will differ. VA == file-offset holds for `.text`/`.rodata` in this binary.*

## Abstract

Softmax is not a primitive in the Neuron backend — it is carried across the pipeline as one of two HLO custom-call targets, `AwsNeuronSoftmax` (forward) and `AwsNeuronSoftmaxBackward` (the gradient), and three passes move it between forms. Two of them **raise**: they scan stock XLA HLO for the numerically-stable softmax idiom (`div(exp(sub(x, max)), sum)`) or its autograd graph and collapse the whole subgraph into a single custom-call. The third **lowers**: it consumes an existing softmax custom-call — whether one the raise passes just emitted, or one that arrived already-fused from the frontend — and rewrites it into the typed, iota-augmented form the downstream tensorizer/simulator consumes.

The shape is the inverse of how an LLVM peephole works. Instead of pattern-matching instructions down toward a target ISA, the raise passes pattern-match *up*: they walk the dataflow graph rooted at a `Divide`, chase a `Reduce` and an `Exp` through layout-only "no-compute" ops, prove the subgraph has no external readers, and replace the whole region with an opaque call. The matcher's robustness comes entirely from two shared helpers — `getNoComputeOpChain` and `getBinaryNoComputeOpChains` — that "see through" the reshapes, broadcasts, converts and transposes XLA scatters around every reduce/broadcast pair, so the match survives layout noise.

This page documents all three passes against the binary: the forward matcher (`NativeToCustomSoftmax::Run` @ `0x1f491b0`), the backward matcher (`NativeToCustomSoftmaxDx::Run` @ `0x1f4c530`), and the custom-call→custom-call lowering (`LegalizeSoftmax::Run` @ `0x1f00120`), plus the two rewrite builders, the shared no-compute-chain matchers, the backend-config wire contract (a bare decimal axis string), and the per-dtype kernel naming the lowering selects. A fourth consumer of the identical forward recognizer exists in `AddLogitOutput` and is called out so a reimplementer does not mistake it for a legalize pass.

For reimplementation, the contract is:

- **The recognized subgraphs** — the exact forward idiom (`div ← reduce-sum ← exp ← [sub ← reduce-max]`) and backward idiom (`dLdx = y ⊙ (dLdy − Σ(dLdy ⊙ y))`), including every guard (single reduction, reduced-axis collapses to 1, shape equality, no external readers).
- **The two custom-call targets and their operand/attribute schema** — `AwsNeuronSoftmax{,Backward}`, their operand spans in each direction, the `api_version=1` constant, and the backend-config = decimal-axis-string round-trip.
- **The lowering's dtype gate and kernel-name selection** — the `{F16, BF16, F8}` element-type acceptance set and the `Softmax_<dtype>` / `SoftmaxBackward_<dtype>` naming, plus the explicit S64 iota operand it materializes.

| | |
|---|---|
| **Forward raise** | `xla::NativeToCustomSoftmax::Run` @ `0x1f491b0` (≈10101 B, 371 bb) |
| **Backward raise** | `xla::NativeToCustomSoftmaxDx::Run` @ `0x1f4c530` (≈10349 B, 335 bb) |
| **Lowering** | `xla::LegalizeSoftmax::Run` @ `0x1f00120` (≈3677 B, 155 bb) |
| **Forward builder** | `xla::replaceNativeSoftmax(comp, logits, div, long dim)` @ `0x1f48a00` |
| **Backward builder** | `xla::replaceNativeSoftmaxDx(comp, y, dLdy, dLdx, long dim)` @ `0x1f4bc70` |
| **Shared matchers** | `getNoComputeOpChain` @ `0x1ebe960`, `getBinaryNoComputeOpChains` @ `0x1ebeb10` |
| **CC targets** | `"AwsNeuronSoftmax"` @ `0x20c9c2` (len 16), `"AwsNeuronSoftmaxBackward"` @ `0x2109e3` (len 24) |
| **CC constructor** | `xla::HloInstruction::CreateCustomCall(Shape, Span, string_view, string, CustomCallApiVersion)` @ `0x964eac0` |
| **itoa digit table** | `byte_40CDA1` (`"0001…9899"`, 200 B) @ `0x40cda0` |
| **Pass order (default pipeline)** | `legalize-softmax` = 7 (early); `native-to-custom-softmax-dx` = 41, `native-to-custom-softmax` = 42 (late) |
| **IR level** | XLA HLO (pre-tensorizer); plain `HloPass`, `Run` at vptr+0x18 |
| **Evidence** | symbols/strings/digit-table CERTAIN; opcode/dtype/api checks disasm-anchored (HIGH); branch nesting MED (no Hex-Rays for the raise `Run` bodies) |

> **NOTE —** the registration order is the counter-intuitive part. The lowering (`legalize-softmax`, order 7) runs *early* — it legalizes softmax custom-calls that already exist, e.g. ones the frontend fused — while the two *raise* passes run *late* (orders 41, 42), after `DecomposeAttention`/`InlineWeights`/`ReplaceRng`. And within the pair, **the backward pass (Dx, 41) is registered before the forward pass (42)**, so in the default pipeline backward softmax is raised first. A reimplementer who assumes "raise then legalize" in pipeline order has it exactly backwards.

---

## The Recognized Subgraph

All three softmax detectors anchor on the numerically-stable softmax. The canonical forward shape is the max-subtract-exp-sum-div idiom; XLA emits it with the standard subtract-the-row-max trick for FP stability:

```text
            x  (logits)
            │
   max = reduce(x, max, dim)          // reduce-max over the softmax axis (optional stability path)
            │
   sub = x − broadcast(max)           // "sub found" / "sub not found"  (the stability subtract)
            │
   exp = exp(sub)                     // the exp
            │
   sum = reduce(exp, add, dim)        // reduce-sum over the same axis; reduction_count == 1, reduced dim → 1
            │
   div = exp / broadcast(sum)         // the divide — the MATCHED ROOT
```

The matcher anchors on the `Divide` and chases its two operands: the numerator must reach an `Exp`, the denominator must reach a `Reduce`. The success diagnostic is verbatim `"div<-exp matches div<-reduce<-exp "` (@ `0x387a30`); the failure diagnostic is `"div<-exp does not match div<-reduce<-exp "` (@ `0x28a6c8` / `0x34bbe8`). The subtract is optional — a graph without the stability subtract still matches, logging `"sub not found"` (@ `0x25afcf`) and taking `exp.operand(0)` directly as the logits; with the subtract it logs `"sub found"` (@ `0x25f178`) and takes `sub.operand(0)`.

> **GOTCHA —** the reduce that feeds the divide is the **sum**, not the max. The detector matches `div ← reduce ← exp`, so the `Reduce` it first finds is the `add`-reduction (the normalizer Σ). The reduce-max is found later, *behind* the optional subtract, by chasing `exp.operand(0)` for a `kSubtract`. Matching the wrong reduce is the classic reimplementation bug here.

### Shared no-compute-chain matchers

The two helpers that make the match layout-robust are in `xla::hlo_utils`:

| Function | Addr | Signature | Role | Confidence |
|---|---|---|---|---|
| `getNoComputeOpChain` | `0x1ebe960` | `(HloInstruction*, HloOpcode)` | Walk a chain of layout-only ops (reshape / broadcast / convert / transpose / bitcast) from `inst` until the requested opcode is reached; return it or null | HIGH (symbol + 4 callers) |
| `getBinaryNoComputeOpChains` | `0x1ebeb10` | `(HloInstruction*, HloOpcode, HloOpcode, bool)` | For a binary op, run a no-compute chain on each of its two operands toward `op1` and `op2`; return `optional<pair<chain,chain>>` | HIGH (symbol + 4 callers) |

These are the see-through-layout primitive. `getBinaryNoComputeOpChains(div, kExp, kReduce, …)` is the single call that recognizes `div ← (exp-chain, reduce-chain)`; it returns a `std::optional<std::pair<NoComputeOpChain, NoComputeOpChain>>` (the optional payload destructor is `0x1f487a0`). Both helpers have exactly four callers — the forward matcher, the backward matcher, `LegalizeSoftmax` does *not* use them (it matches a custom-call, see §3), and the fourth caller is `DetectSoftmaxAndGetLogits` (see §4).

---

## NativeToCustomSoftmax (order 42) — forward raise

### Purpose

Scan every computation for the forward softmax idiom and collapse each match into a single `AwsNeuronSoftmax` custom-call carrying the reduction axis. A pure HLO→HLO rewrite that shrinks a 5–7 node region to one opaque node.

### Algorithm

```c
// xla::NativeToCustomSoftmax::Run @ 0x1f491b0
function NativeToCustomSoftmax_Run(module):
  for comp in module.computations():
    count = 0
    for div in comp.MakeInstructionPostOrder():           // callee 0x9634ab0; candidate root = a Divide
      // ---- match  div ← (exp-chain, reduce-chain) ----
      chains = getBinaryNoComputeOpChains(div, kExp, kReduce, /*…*/)   // 0x1ebeb10
      if !chains:
        log "div<-exp does not match div<-reduce<-exp "    // 0x28a6c8 / 0x34bbe8
        continue
      log "exp and reduce found"                           // 0x214ba0
      (exp, reduce) = chains
      // ---- the reduce must be a single, axis-collapsing reduction ----
      if reduce.reduction_count() != 1:
        log "reduce has more than one reduction"           // 0x333f38
        continue
      reduce_dim = reduce.dimensions()[0]
      if reduce.shape().dim_size(reduce_dim) != 1:         // reduced axis must collapse to 1
        log "reduce dim size is not 1"                     // 0x218e79
        continue
      log "div<-exp matches div<-reduce<-exp "             // 0x387a30  → forward softmax confirmed
      log "reduce dim: " << reduce_dim                     // 0x22c78f
      // ---- optional numerical-stability subtract ----
      sub = getNoComputeOpChain(exp.operand(0), kSubtract) // 0x1ebe960
      if sub: { log "sub found"     /*0x25f178*/;  logits = sub.operand(0); }
      else:   { log "sub not found" /*0x25afcf*/;  logits = exp.operand(0); }
      // ---- escape / safety check: nothing inside the region may be read outside it ----
      blockOps = { div, reduce, exp, sub, max, broadcasts… } // hashtable insert 0x1f48810
      log "Number of Ops in Softmax block: " << |blockOps|   // 0x2a0e18
      for op in interior(blockOps):
        if not (op.users() ⊆ blockOps):
          log "Internal Softmax node (above) is used outside of Softmax "
              "Block. Skip Native to Custom Softmax replacement."   // 0x2fbae8
          goto next_div                                    // abort: a softmax-internal value escapes
      // ---- rewrite ----
      replaceNativeSoftmax(comp, /*logits=*/logits, /*div=*/div, /*dim=*/reduce_dim)  // 0x1f48a00
      count += 1
    log "Number of Native Softmax's detected and replaced: " << count  // 0x358158
```

The constant `0x3f800000` (= `1.0f`) is materialized in `Run` — the identity/broadcast-1 used while validating the reduction operand or canonicalizing the divide. `blockOps` is a hash set (`_M_insert_unique_node` @ `0x1f48810`) used purely for the escape check.

> **QUIRK —** the escape check is what makes this a *fusion*, not a peephole. If any node strictly inside the softmax region (e.g. the un-normalized `exp`, or the `reduce`) is read by an instruction *outside* the matched set, the rewrite is skipped entirely — replacing the divide would orphan a live value. A naive implementation that rewrites on pattern match alone will silently drop a real dataflow edge.

### Forward builder — `replaceNativeSoftmax` @ `0x1f48a00`

Disasm-confirmed argument setup, in order:

```c
// xla::replaceNativeSoftmax(comp, logits, div, long dim) @ 0x1f48a00
function replaceNativeSoftmax(comp, logits, div, dim):
  cfg = itoa(dim)                                          // digit-pair table byte_40CDA1 @ 0x40cda0 (0x1f48b5d)
  cc  = HloInstruction::CreateCustomCall(                  // 0x964eac0 (0x1f48bcd)
          /*shape   =*/ div->shape(),                      // result shape == softmax shape
          /*operands=*/ Span{ logits },                    // ONE operand: the logits tensor
          /*target  =*/ string_view{"AwsNeuronSoftmax", 16}, // r9d=ptr 0x20c9c2, r8d=0x10=16  (0x1f48bb9/0x1f48bac)
          /*opaque  =*/ cfg,                               // backend_config = decimal axis text
          /*api_ver =*/ 1 )                                // push 1 = API_VERSION_ORIGINAL  (0x1f48ba7)
  comp->AddInstruction(move(cc), /*name=*/"Softmax_…")     // 0x96370d0; name prefix "Softmax_" @ 0x40610c
  comp->ReplaceInstruction(div, cc)                        // 0x1f48c66 → 0x963fe50
        // assert string "computation->ReplaceInstruction(y, newInstPtr)" @ 0x387a00 (0x1f48c75)
  log "replaceNativeSoftmax Done!"                          // 0x25afb4
```

> **NOTE —** the `mov r8d, 10h` immediately before the `CreateCustomCall` (at `0x1f48bac`) is the target string *length* (16) — the constructor takes a `string_view`, so length is passed explicitly. The backward builder passes `0x18` (24) in the same slot. This is the cleanest disambiguator between the two targets in the disassembly.

---

## NativeToCustomSoftmaxDx (order 41) — backward raise

### Purpose

Recognize the autograd of softmax and collapse it into one `AwsNeuronSoftmaxBackward` custom-call. With `y = softmax(x)` and incoming gradient `dLdy`, the softmax Jacobian-vector product is

```text
   dLdx = y ⊙ ( dLdy − Σ_dim( dLdy ⊙ y ) )       // = y * (dLdy − sum(dLdy * y, dim))
```

### Algorithm

The backward matcher chases the named intermediate nodes of the reference autograd graph; the binary embeds the reference HLO's exact variable names (`div452 / div711 / div712`, `mul5574/5575/5576`, `reduce5 / reduce294`, `add1467`, `neg224`, `bcast16469`) as verbatim diagnostics, so the match progression is fully reconstructable from string order even without Hex-Rays.

```c
// xla::NativeToCustomSoftmaxDx::Run @ 0x1f4c530
function NativeToCustomSoftmaxDx_Run(module):
  for comp in module.computations():
    count = 0
    for root in comp.MakeInstructionPostOrder():
      // ---- locate the two divides + neg feeding the backward subtract/mul ----
      if !div711 || !neg224:  continue   else log "div711 and neg224 found"  // 0x24b7da
      if !add1467 || !exp0:   continue   else log "add1467 and exp0 found"   // 0x218e92
      // ---- the inner reduction Σ(dLdy ⊙ y) ----
      if !reduce5:  log "reduce5 not found" /*0x2308fd*/;  continue
      log "reduce5 found" /*0x228bc4*/;  log "reduce5Op3 found" /*0x224b46*/
      if reduce5.dim_size != 1:  log "reduce5 dim size is not 1" /*0x228bd2*/;  continue
      log "reduce5 dim: " << dim                                              // 0x214bb5
      if !reduce294:  log "reduce294 not found" /*0x262ff6*/;  continue
      log "reduce294 found"                                                   // 0x224b36
      // ---- the y ⊙ dLdy products ----
      mul5576 = getNoComputeOpChain(…, kMultiply, …)
      if !mul5576.chains:  log "mul5576OpChains not found" /*0x23bfaa*/
      log "mul5576 found" /*0x243dd6/; log "mul5575 found" /*0x26301c*/
      if mul5576.rhs != exp0:  log "mul5576 Rhs is not exp.0" /*0x20c9d3*/;  continue
      // ---- dL/dy identification ----
      assert one_of(mul5575.operands) == dLdy == div711.lhs
            // else "One of the mul5575O operands should be dL/dy (which is div711Lhs)"  // 0x3dac40
      log "bcast16469 found"                                                  // 0x218ea9
      if !div712:  log "div712 not found" /*0x25afdd*/;  continue   else log "div712 found" /*0x21ccfd*/
      if !mul5574: …  else log "mul5574 found" /*0x2825a5*/
      if mul5574.operands != reduce5:  log "mul5574 operands are not reduce.5" /*0x2abf00*/;  continue
      log "both mul5574 operands are reduce.5"                                 // 0x37bc60
      if !div452:  log "div452 not found" /*0x234881*/;  continue   else log "div452 found" /*0x21cd0a*/
      // ---- shape verification of (dLdy, y, dLdx) ----
      if !Shape::Equal(arg0.shape, arg1.shape):                                // 0x97d73c0
        log "SoftmaxDx was found but arg0 and arg1 shapes are different"       // 0x3c25b0 (INFO; skip)
        continue
      log "dLdyShape: " << shape /*0x25f182/; log "dLdy, y and dLdx Shape verification passed" /*0x2ce7b8*/
      // ---- rewrite ----
      replaceNativeSoftmaxDx(comp, /*y=*/y, /*dLdy=*/dLdy, /*dLdx=*/dLdx, /*dim=*/reduce5_dim)  // 0x1f4bc70
      count += 1
    log "Number of Native SoftmaxDx's detected and replaced: " << count        // 0x3283d0
```

> **GOTCHA —** the backward match has a hard shape gate the forward match lacks: `Shape::Equal(arg0.shape, arg1.shape)` (callee `0x97d73c0`) must hold for `dLdy` and `y`. When it fails the pass logs `"SoftmaxDx was found but arg0 and arg1 shapes are different"` at INFO and *skips* — it does not error. A correct backward subgraph with a mismatched-rank gradient will silently not be fused.

### Backward builder — `replaceNativeSoftmaxDx` @ `0x1f4bc70`

```c
// xla::replaceNativeSoftmaxDx(comp, y, dLdy, dLdx, long dim) @ 0x1f4bc70
function replaceNativeSoftmaxDx(comp, y, dLdy, dLdx, dim):
  cfg = itoa(dim)                                          // digit-pair table @ 0x40cda0
  cc  = HloInstruction::CreateCustomCall(                  // 0x964eac0
          /*shape   =*/ dLdx->shape(),                     // result == grad-input shape
          /*operands=*/ Span{ y, dLdy },                   // TWO operands: forward output y, then upstream grad dLdy
          /*target  =*/ string_view{"AwsNeuronSoftmaxBackward", 24}, // r9=ptr 0x2109e3, r8d=0x18=24
          /*opaque  =*/ cfg,                               // backend_config = decimal axis text
          /*api_ver =*/ 1 )                                // push 1 = API_VERSION_ORIGINAL
  comp->AddInstruction(move(cc), /*name=*/"SoftmaxBackward_…")  // 0x96370d0; prefix "SoftmaxBackward_" @ 0x406120
  comp->ReplaceInstruction(dLdx, cc)                       // 0x963fe50
        // assert "computation->ReplaceInstruction(dLdx, newInstPtr)" @ 0x3c2578
  log "replaceNativeSoftmaxDx Done!"                        // 0x22c79c
```

> **QUIRK —** operand order matters and is **`{y, dLdy}`** — the *forward output first*, then the gradient. The backward kernel needs `y` to form `y ⊙ (…)` without recomputing softmax, so the raise passes the already-computed forward result rather than `x`. Swapping the span is a wrong-answer bug the type system will not catch (both are same-shaped float tensors).

---

## LegalizeSoftmax (order 7) — lower the softmax custom-call

### Purpose

Unlike the two raise passes, `LegalizeSoftmax` matches an **existing** custom-call, not a native subgraph. It takes an abstract `AwsNeuronSoftmax{,Backward}` call and rewrites it into the concrete form the tensorizer/simulator consumes: it materializes an explicit S64 iota index/axis constant operand, re-validates the result shape, re-parses the reduction axis from the backend-config, and selects a per-dtype kernel name. It is a pure custom-call→custom-call rewrite.

### Algorithm

Every step here is disasm-anchored (the `Run` body *was* exported for this pass):

```c
// xla::LegalizeSoftmax::Run @ 0x1f00120
function LegalizeSoftmax_Run(module):
  for comp in module.computations():
    for inst in comp.MakeInstructionPostOrder():           // 0x9634ab0
      if inst->opcode() != kCustomCall:  continue          // 0x1f001d6: cmp byte[r15+0x14], 0x2B
      cc  = dynamic_cast<HloCustomCallInstruction*>(inst)  // 0x1f001ec: ___dynamic_cast
      tgt = cc->custom_call_target()                       // (field at cc+0x220)
      isFwd = (tgt == "AwsNeuronSoftmax")                  // 0x1f001f1 (target str 0x20c9c2)
      if !isFwd && tgt != "AwsNeuronSoftmaxBackward":      // 0x1f00210/0x1f00224 (str 0x2109e3)
        continue
      isBwd = (tgt == "AwsNeuronSoftmaxBackward")          // selects fwd/bwd path
      // ---- dtype gate: accept the floating softmax-kernel element types ----
      et = inst->shape().element_type()                    // 0x1f0023c
      if !((et - 0x0A) <= 1 || et == 0x10):  continue      // 0x1f00244 sub 0Ah; 0x1f0024c cmp 10h
            //   accepts F16 (0x0A=10), BF16 (0x0B=11), and F8-family (0x10=16)
      // ---- build the S64 iota / axis index literal ----
      axis = inst->shape().rank() >> 1                      // 0x1f00264..
      ax0  = LiteralUtil::CreateR0<long>(axis)              // 0x1ed73c0
      dims = inst->operand(0)->shape().dims()               // 0x1f0028c.. (memcpy of dim array)
      idxShape = ShapeUtil::MakeValidatedShape(/*S64=*/5, Span<long>{len})  // 0x97e13d0 (esi=5)  (0x1f00329)
      idxLit   = Literal(idxShape).PopulateR1<long>(Span{indices})          // 0x1eec6d0 (the iota vector)
      idxConst = HloInstruction::CreateConstant(move(idxLit))               // 0x9668610
      comp->AddInstruction(idxConst)                        // 0x96370d0
      // ---- re-parse reduce dim from backend_config ----
      cfg = cc->backend_config_wrapper().GetRawStringWithoutMutex()         // 0x96cc310
      dim = strtol(cfg, &end, 10)                           // base 10 (0x1f003da: mov edx,0Ah)
            //   on malformed input → throw std::invalid_argument tagged "stoi"  (str 0x22c74e)
      // ---- pick the per-dtype kernel name via the static softmaxNames map ----
      name = softmaxNames[et]                               // static local, __cxa_guard-inited
            //   "Softmax_<dtype>" (fwd) / "SoftmaxBackward_<dtype>" (bwd)
            //   dtype-suffix DenseMap<PrimitiveType,string> ctor @ 0x1eec220
      // ---- emit the legalized custom-call ----
      newInst = HloInstruction::CreateCustomCall(           // 0x964eac0
            /*shape   =*/ inst->shape(),
            /*operands=*/ Span{ operand0, idxConst },       // original input + the iota index const
            /*target  =*/ string_view{ tgt },               // REUSES "AwsNeuronSoftmax"/"…Backward"
            /*opaque  =*/ itoa(dim),                         // re-rendered via digit table 0x40cda0
            /*api_ver =*/ 1 )
      comp->ReplaceInstruction(inst, newInst)               // 0x963fe50
            // assert "computation->ReplaceInstruction(inst, newInst)" @ 0x2b7ff8
  // guard near exit: assert entry_computation_ != nullptr (str 0x2108c9, hlo_module.h @ 0x25f076)
```

The `softmaxNames` population is visible inline in the disassembly: the dtype map is built with keys `0Bh` (BF16, at `0x1f00aa2`), `0Ah` (F16, at `0x1f00b26`), and `10h` (F8, at `0x1f00ba0`), and a suffix value constant `0x36316662` (= `"bf16"` little-endian) is stored at `0x1f00bea` — confirming both the three-dtype gate and the suffix-string construction at the same site.

> **NOTE —** the dtype gate `(et − 0x0A) ≤ 1 || et == 0x10` is the **only** acceptance filter. PrimitiveType IDs `0x0A`/`0x0B` are the F16/BF16 region of this XLA vintage's enum and `0x10` is the FP8 family; F32 (and anything else) falls through and is left as the abstract softmax call. The precise enum-to-name binding for `0x10` is not cross-checked against this binary's `PrimitiveType` table (MED); the *numeric* gate is disasm-certain.

> **QUIRK —** `LegalizeSoftmax` *reuses the same target string* it matched (`r8`/`rsi` is reloaded from the saved target), so the lowered call still reads `AwsNeuronSoftmax{,Backward}`. The legalization is **not** in the target name — it is in the added S64 iota operand, the re-validated shape, and the dtype-keyed instruction name. A reader expecting a renamed target (`Softmax_bf16` as the *custom_call_target*) is wrong: that string is the *instruction name*, set via `AddInstruction`, not the call target.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `LegalizeSoftmax::Run` | `0x1f00120` | 3677 B / 155 bb | CC→CC lowering: opcode-0x2B + target match, dtype gate, S64 iota, dtype name | HIGH (disasm-anchored) |
| `NativeToCustomSoftmax::Run` | `0x1f491b0` | 10101 B / 371 bb | Forward raise: `div ← reduce-sum ← exp ← [sub ← reduce-max]` → `AwsNeuronSoftmax` | HIGH (strings/callees) |
| `NativeToCustomSoftmaxDx::Run` | `0x1f4c530` | 10349 B / 335 bb | Backward raise: `y⊙(dLdy−Σ(dLdy⊙y))` → `AwsNeuronSoftmaxBackward` | HIGH (strings/callees) |
| `replaceNativeSoftmax` | `0x1f48a00` | 1668 B / 68 bb | Forward CC builder + `ReplaceInstruction(div)` | HIGH |
| `replaceNativeSoftmaxDx` | `0x1f4bc70` | 1917 B / 84 bb | Backward CC builder + `ReplaceInstruction(dLdx)` | HIGH |
| `getNoComputeOpChain` | `0x1ebe960` | 394 B / 31 bb | See-through-layout single chain to a target opcode | HIGH |
| `getBinaryNoComputeOpChains` | `0x1ebeb10` | 543 B / 25 bb | See-through-layout pair of chains for a binary op | HIGH |

---

## Custom-Call Attribute / Backend-Config Schema

`xla::HloInstruction::CreateCustomCall(Shape, Span<HloInstruction* const>, string_view, std::string, CustomCallApiVersion)` @ `0x964eac0` is the single constructor all three rewrites use. The reconstructed schema:

| Field | Forward (`AwsNeuronSoftmax`) | Backward (`AwsNeuronSoftmaxBackward`) | Source |
|---|---|---|---|
| custom_call_target | `"AwsNeuronSoftmax"` (len 16) | `"AwsNeuronSoftmaxBackward"` (len 24) | str `0x20c9c2`/`0x2109e3`; `r8d=0x10`/`0x18` |
| operands (raise 41/42) | `{ logits }` (1) | `{ y, dLdy }` (2) | §2 / §3 span setup |
| operands (legalize 7) | `{ operand0, iota_index_const }` (2) | `{ operand0, iota_index_const }` (2) | §3 `CreateConstant`+span |
| result shape | `div->shape()` (softmax shape) | `dLdx->shape()` | `shape()` callee `0x9650370` |
| backend_config / opaque | decimal ASCII of the **reduce/softmax axis** | decimal ASCII of the `reduce5` axis | digit table `0x40cda0`; `strtol`/"stoi" |
| api_version | `1` = `API_VERSION_ORIGINAL` | `1` | `push 1` before `CreateCustomCall` |
| instruction name | `"Softmax_<dtype>"` | `"SoftmaxBackward_<dtype>"` | prefix str `0x40610c`/`0x406120`; `AddInstruction` name arg |

**Backend-config contract.** The on-wire `backend_config`/opaque is the **softmax reduction axis as a bare decimal string** — nothing more. It is *written* by `replaceNativeSoftmax(Dx)` via the 200-byte two-digit itoa table `byte_40CDA1` @ `0x40cda0` (the classic `"00010203…9899"` pair table), and *re-parsed* by `LegalizeSoftmax` via `strtol` (base 10), which throws `std::invalid_argument` tagged `"stoi"` (str `0x22c74e`) on malformed input. No structured protobuf was observed for these two targets in any of the three passes — it is a plain integer string that round-trips cleanly through the raise→legalize boundary.

> **GOTCHA —** because the axis travels as decimal text, a reimplementer must keep the write and read sides numerically consistent: `replaceNativeSoftmax` emits e.g. `"2"` for axis 2; `LegalizeSoftmax`'s `strtol` reads it back. There is no length prefix, no key=value, no JSON — feeding anything other than a bare base-10 integer triggers the `"stoi"` throw. No `frontend_attributes` are attached by any of these three passes (negative finding, HIGH for this pass).

**Per-dtype kernel naming.** Only `LegalizeSoftmax` consults the static `softmaxNames` map (lazily initialized via `__cxa_guard`) plus a `DenseMap<PrimitiveType, std::string>` (ctor @ `0x1eec220`) whose values are dtype suffixes (e.g. `"bf16"`, the `0x36316662` constant). It forms the instruction name `Softmax_<dtype>` / `SoftmaxBackward_<dtype>` (prefix `"Softmax_"` @ `0x40610c`, `"SoftmaxBackward_"` @ `0x406120`) to select the typed simulator/tensorizer kernel that ultimately implements the call. The full PrimitiveType→suffix key list is not enumerated beyond the three gated dtypes; the `"bf16"` suffix is confirmed (MED on the complete map).

---

## Related Passes & Components

| Order | Name | Relationship |
|---|---|---|
| 7 | `legalize-softmax` | This page — the lowering; runs early to legalize pre-existing softmax custom-calls |
| 41 | `native-to-custom-softmax-dx` | This page — backward raise; registered **before** the forward raise |
| 42 | `native-to-custom-softmax` | This page — forward raise |
| 43 | `add-logit-output` | Reuses the identical forward recognizer but for a different purpose (see CORRECTION below) |
| 38 | `decompose-attention` | Runs before the raise passes; the raise passes see post-decomposition HLO |

> **CORRECTION (D-B02-N1) —** a *fourth* softmax recognizer, `(anonymous)::DetectSoftmaxAndGetLogits` @ `0x1e82980` (in `AddLogitOutput`), reuses the exact same `getNoComputeOpChain`/`getBinaryNoComputeOpChains` `div ← exp ← reduce` matcher and even the same success string (`"Verified softmax pattern: div<-exp matches div<-reduce<-exp"` @ `0x3b7168`). It is **not** a legalize pass — it only extracts the logits tensor (`"Found logits tensor: "` @ `0x243cf8`) to add a logit output, and is called solely by `AddLogitOutput::Run` (order 43). Do not fold it into the softmax-legalize family; it shares the recognizer, not the purpose.

> **CORRECTION (D-B02-C1) —** an earlier survey attributed "recognizing the native max-subtract-exp-sum-div subgraph" to `LegalizeSoftmax`. Binary truth: native-subgraph recognition lives entirely in `NativeToCustomSoftmax(Dx)` (orders 41/42). `LegalizeSoftmax` (order 7) is a pure custom-call→custom-call rewrite — it matches `kCustomCall` (`cmp byte[r15+0x14], 0x2B` @ `0x1f001d6`) + `dynamic_cast<HloCustomCallInstruction>` (@ `0x1f001ec`) and never inspects a native exp/reduce/div subgraph.

---

## Adversarial Self-Verification

The five strongest claims on this page, re-challenged against the binary:

1. **The three passes are at the cited addresses with the cited symbols.** VERIFIED — `_names.json` maps `0x1f00120 → xla::LegalizeSoftmax::Run`, `0x1f491b0 → xla::NativeToCustomSoftmax::Run`, `0x1f4c530 → xla::NativeToCustomSoftmaxDx::Run`, and the two builders `0x1f48a00 → replaceNativeSoftmax(comp, HloInstruction*, HloInstruction*, long)` and `0x1f4bc70 → replaceNativeSoftmaxDx(comp, …, …, …, long)`. The demangled builder signatures independently confirm the operand roles `(comp, logits, div, dim)` and `(comp, y, dLdy, dLdx, dim)`. **CONFIRMED.**
2. **The two custom-call targets and their lengths.** VERIFIED — `"AwsNeuronSoftmax"` @ `0x20c9c2` (length 16) and `"AwsNeuronSoftmaxBackward"` @ `0x2109e3` (length 24) in `_strings.json`; the lengths match the `mov r8d, 10h` / `r8d, 18h` immediates fed to `CreateCustomCall`. **CONFIRMED.**
3. **`LegalizeSoftmax` matches a custom-call, not a native subgraph.** VERIFIED in disasm: `cmp byte ptr [r15+14h], 2Bh` (`0x1f001d6`), `call ___dynamic_cast` (`0x1f001ec`), then the two target compares against the offsets of `"AwsNeuronSoftmax"`/`"…Backward"`. It calls neither `getNoComputeOpChain` nor `getBinaryNoComputeOpChains`. **CONFIRMED.**
4. **The dtype gate accepts F16/BF16/F8 and the map is populated with 0x0A/0x0B/0x10.** VERIFIED in disasm: `sub eax, 0Ah` (`0x1f00244`) + `cmp …, 10h` (`0x1f0024c`) for the gate; `softmaxNames` build stores keys `0Bh`/`0Ah`/`10h` (`0x1f00aa2`/`0x1f00b26`/`0x1f00ba0`) and the `"bf16"` suffix constant `0x36316662` (`0x1f00bea`). The *numeric* gate is certain; the enum→name binding for `0x10` (F8 family) is INFERRED (not cross-checked against this binary's `PrimitiveType` enum) and tagged MED in §3.
5. **The backend-config is a bare decimal axis string round-tripped via the digit table + `strtol`.** VERIFIED — `replaceNativeSoftmax` reads `byte_40CDA1` (`movzx … ds:byte_40CDA1[rax]` @ `0x1f48b5d`) and pushes `api_version=1` (`push 1` @ `0x1f48ba7`); `LegalizeSoftmax` re-parses with base-10 `strtol` (`mov edx, 0Ah` @ `0x1f003da`) and carries the `"stoi"` throw string `0x22c74e`. No structured protobuf observed. **CONFIRMED** (absence of richer schema is HIGH for these three passes; other passes upstream are out of scope).

Residual MED items, explicitly: the exact branch nesting of the two raise `Run` bodies (no Hex-Rays; `NVOPEN_IDA_SKIP_DECOMPILE` was set for those — reconstruction is from disasm + diagnostic-string order, ~49 try-blocks in Dx); the complete `softmaxNames` PrimitiveType key list beyond the three gated dtypes; and the `0x10`→FP8-name binding.

---

## Cross-References

- [HLO → Native / NKI Kernel Lowering](hlo-to-native-kernel-lowering.md) — §4.33; the attention-fusion path that consumes the same custom-call raising machinery and shares the `getNoComputeOpChain` matchers
- [Attention Kernels with Online Softmax](../nki/attention-online-softmax.md) — §6.7.6; the NKI kernels that ultimately implement `Softmax_<dtype>` / fused attention, where softmax is computed online rather than as a discrete custom-call
- [The hlo-opt Pass Registry](pass-registry.md) — the `--passes` table; resolves orders 7/41/42/43 to these passes and their factories
- [CC-Op Decompose & Legalize Family](ccops-decompose-legalize.md) — sibling custom-call legalization passes that share the CC→CC rewrite shape
