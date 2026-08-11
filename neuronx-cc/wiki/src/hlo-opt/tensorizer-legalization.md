# TensorizerLegalizationPass

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/bin/hlo2penguin` (cp310). `.rodata` = VA − 0x200000, `.text` = VA − 0x201000 — VA is **not** the file offset. Other wheels differ; treat every address as version-pinned.*

## Abstract

`TensorizerLegalizationPass` is the last MLIR pass that touches the **module-output side** of the graph before the Penguin tensorizer emits Python. Its job is narrow and contractual: make sure every value the `main` function returns satisfies the tensorizer's IO contract — a returned tensor must be a *distinct, writable, named* buffer, never an aliased view of an input or a constant. It does this with two mechanisms that share one walk: it inserts an identity copy (`mhlo.CopyOp`) whenever a returned value would otherwise alias an input or constant pool entry, and it stamps a `neuron.symName` symbol-reference attribute onto every output's producing op so the downstream `MhloToPythonPrinter` can label `getOutputTensors`. So this pass is simultaneously an **aliasing legalizer** (copy insertion) and the **output-naming stamper**.

The reframe versus a stock MLIR pass: there is no rewrite-pattern driver and no greedy fixpoint here. The pass walks the single `func::ReturnOp` of `main`, and for each returned operand classifies its *ultimate defining op* by chasing single-result chains, then applies one of three rules — copy-and-name (input/constant), name-in-place (compute op), or name-the-inner-region-value (fusion/composite). It is a one-shot, operand-indexed transform keyed off the return terminator, not the whole function body. The familiar analogue is LLVM's `PreISelIntrinsicLowering` or a `verifier`-adjacent normalization pass that enforces a backend ABI invariant rather than optimizing.

The pass exists in **two variants** sharing the naming machinery: the MHLO `TensorizerLegalizationPass` (`0x2197940`, CLI `tensorizer-legalization`) and the richer StableHLO `StableHLOTensorizerLegalizationPass` (`0x2147660`, CLI `stablehlo-tensorizer-legalization`). The StableHLO twin is **not** a mechanical clone: it bolts on two extra pre-stages (large-constant splat legalization and zero-sized-tensor legalization) that the MHLO pass lacks, and surfaces fusions as `stablehlo.composite` ops rather than `mhlo.fusion`. Both are global-namespace `mlir::PassWrapper<…, OperationPass<ModuleOp>>` classes.

For reimplementation, the contract is:

- **The IO contract** the tensorizer demands of `main`'s outputs — no direct input==output aliasing, no constant==output aliasing, every output named — and the three-case decision that enforces it.
- **The naming scheme** — `neuron.symName` as an `ArrayAttr<SymbolRefAttr>` on the *producing op*, one slot per op-result, accumulated as multiple outputs of a multi-result op are named.
- **The two StableHLO-only pre-stages** (splat-const → broadcast, zero-element → `AwsNeuronZeroSizedOp` custom call) and the `neuron.actual_shape` shape-recovery annotation.

| | |
|---|---|
| **MHLO entry** | `TensorizerLegalizationPass::runOnOperation` @ `0x2197940` (312 B) |
| **StableHLO entry** | `StableHLOTensorizerLegalizationPass::runOnOperation` @ `0x2147660` (1834 B) |
| **CLI args** | `tensorizer-legalization` (`0x262976`) · `stablehlo-tensorizer-legalization` (`0x30d950`) |
| **Per-output legalizer** | `duplicateLoadedOutputs` — MHLO `0x21971e0` (1661 B) / SHLO `0x21465f0` (951 B) |
| **Naming stamper** | `renameAsOutputImpl` @ `0x2196a00` / `0x2145680` (1551 B each) |
| **Output-name source** | `hilo::getOutputNames` @ `0x21c2760` (362 B) → `output_names` attr (`0x21a069`) or synthesized |
| **Naming attr** | `neuron.symName` (`0x27ae21`, len 14), `ArrayAttr<SymbolRefAttr>`, per op-result |
| **IR level** | MLIR — `mhlo` / `stablehlo` dialect, post-fusion, pre-Penguin emission |
| **Pass class** | `mlir::PassWrapper<…, OperationPass<ModuleOp>>`, global namespace |

## The IO Contract this pass enforces

The Penguin tensorizer treats a module output as a *backend output buffer*: a region of SBUF/DRAM the kernel writes and the runtime reads back. Three properties must hold for every operand of `main`'s `func.return`, and each maps to one legalization rule.

| Contract clause | Why the tensorizer needs it | Enforcement |
|---|---|---|
| An output may not be a function **block-argument** (a "loaded" input value) | The output buffer would alias the input buffer; the kernel would both read its input from and write its output to the same address | `duplicateLoadedOutputs` CASE 2 → insert `mhlo.CopyOp` |
| An output may not be a **constant** | The output buffer would alias the read-only constant pool | `duplicateLoadedOutputs` CASE 1 → insert `mhlo.CopyOp` |
| Every output **must carry its symbol name** on its producing op | The Python emitter reads `neuron.symName` to label `getOutputTensors`/`printDsts`; an unnamed result cannot be wired to a NEFF output slot | `renameAsOutputImpl` → stamp `neuron.symName` |
| (StableHLO only) An output may not be a **zero-element** tensor | The tensorizer has no buffer geometry for an empty tensor; it must be replaced by an explicit marker op | `legalizeZeroSizedTensors` → `AwsNeuronZeroSizedOp` custom call |

> **NOTE —** the term "loaded output" in the symbol name `duplicateLoadedOutputs` is the binary's own vocabulary for *an output that is directly a loaded (input/parameter) value*. The "duplicate" is the inserted identity copy that breaks the alias. CASE 1 (constant) rides the same copy-insert mechanism because a constant returned directly aliases the constant pool the same way.

The net model the pass leaves behind:

```text
output-is-input     →  mhlo.CopyOp + (SHLO) neuron.actual_shape + neuron.symName
output-is-constant  →  mhlo.CopyOp + (SHLO) neuron.actual_shape + neuron.symName
output-is-compute   →  neuron.symName in place (no copy)
output-is-fusion    →  neuron.symName on the INNER region-return value (ScheduleFusion / composite)
output-is-empty     →  (SHLO) AwsNeuronZeroSizedOp custom_call + neuron.actual_shape
```

---

## The walk — `runOnOperation`

### Purpose

Find `main`, read its declared output names, and walk to the single `func.return` terminator. The whole per-output transform is driven from a TypeID-filtered walk callback that fires only on `func::ReturnOp`.

### Entry Point

```text
TensorizerLegalizationPass::runOnOperation       0x2197940 (312 B)   ── MHLO driver
  ├─ hilo::getMainFunction(ModuleOp&)            0x21c1e70 (259 B)   ── find func "main"
  ├─ hilo::getOutputNames(func::FuncOp&)         0x21c2760 (362 B)   ── output_names or synth
  └─ mlir::detail::walk<Forward>(func, lambda)   ── legalizeReturnOp callback @0x2197860 (212 B)
       └─ duplicateLoadedOutputs(...)            0x21971e0 (1661 B)  ── per-output legalizer

StableHLOTensorizerLegalizationPass::runOnOperation  0x2147660 (1834 B)  ── 3 ordered stages
  ├─ (A) legalizeLargeConstantTensors  [INLINED walk over stablehlo::ConstantOp]
  ├─ (B) legalizeZeroSizedTensors(func)          0x2146a90 (3019 B)
  └─ (C) getOutputNames + walk(legalizeReturnOp) ── callback @0x21469b0 → duplicateLoadedOutputs 0x21465f0
```

### Algorithm

```c
// TensorizerLegalizationPass::runOnOperation — 0x2197940 (MHLO, 312 B)
function runOnOperation():
    func  = hilo::getMainFunction(this->op)          // 0x21c1e70 ; op at [this+0x28]
    names = hilo::getOutputNames(func)               // 0x21c2760 ; std::vector<std::string>
    this->outputNames_ = names                       // stored at [this+0x150]=data, [this+0x158]=size
    walk<ForwardOrder>(func, legalizeReturnOp)        // mlir::detail::walk, callback @0x2197860
    free(names)                                       // vector destroyed on exit

// legalizeReturnOp callback — 0x2197860 (212 B)
function legalizeReturnOp(Operation* op) -> WalkResult:
    if TypeID(op) != TypeID<func::ReturnOp>:          // fires ONLY on the terminator
        return WalkResult::advance                    // = 1
    ctx     = op.getContext()                          // Attribute context, op+0x18
    operands = op.getODSOperandIndexAndLength(0)       // OperandRange = ALL returned values
    builder  = OpBuilder(ctx)                          // on-stack; inserts before the ReturnOp
    duplicateLoadedOutputs(builder, operands, op, ctx) // 0x21971e0
    return WalkResult::advance
```

```c
// StableHLOTensorizerLegalizationPass::runOnOperation — 0x2147660 (1834 B)
function runOnOperation():
    func = hilo::getMainFunction(this->op)
    // (A) splat-const legalization — INLINED walk over stablehlo::ConstantOp
    for each cst in walk<stablehlo::ConstantOp>(func):
        attr = cst.getValue()                          // DenseElementsAttr
        if attr.isSplat():                             // single repeated value
            elt   = *attr.value_begin()                // AttributeElementIterator
            scalar = stablehlo::ConstantOp(DenseElementsAttr::get(scalarType, {elt}))
            full   = stablehlo::BroadcastInDimOp(scalar, getDenseI64ArrayAttr(dims))
            cst.replaceAllUsesWith(full); cst.erase()  // huge dense weight → scalar + broadcast
    // (B) zero-sized legalization
    legalizeZeroSizedTensors(func)                     // 0x2146a90
    // (C) identical to MHLO (C)
    this->outputNames_ = hilo::getOutputNames(func)
    walk<ForwardOrder>(func, legalizeReturnOp)         // callback @0x21469b0
```

> **QUIRK —** the walk targets the **terminator**, not the function body. There is exactly one `main` and exactly one `func.return`; the pass legalizes *its operand list* = the module outputs. A reimplementation that walks all ops looking for "outputs" will mis-fire — outputs are defined entirely by the return-operand positions, and slot index `i` (not the value's identity) is what selects the output name.

> **GOTCHA —** stage (A)'s emitter `report_fatal_error`s with *"Building op `stablehlo.broadcast_in_dim` but it isn't known in this MLIRContext…"* if the stablehlo dialect is not loaded. The StableHLO pass therefore declares its dependent dialects (`getDependentDialects` @ `0x2145490`, registers `mhlo` + `stablehlo` + `func`) — note it needs **mhlo** too, because the identity copy carrier is `mhlo.CopyOp` even in the StableHLO pipeline.

### Considerations

`getMainFunction` (`0x21c1e70`) walks the module body's `func::FuncOp` ops (`detail::walk` + callback `@0x21bf2c0`) and returns the one whose `SymbolTable::getSymbolName == "main"`. Its cold path is a no-main abort. `getOutputNames` is shared verbatim across both passes and both PyPenguin printers (callers: `MhloToPyPenguin::runOnOperation`, `StableHLOToPyPenguin::runOnOperation`, and both legalization passes), so the name list a legalized output carries is exactly the list the printer later reads back.

---

## `duplicateLoadedOutputs` — the per-output decision

### Purpose

For each returned operand `out_i` at slot `i`, walk the defining-op chain upward to classify the ultimate source, then apply exactly one of three legalization rules. This is the alias-breaking heart of the pass.

### Algorithm

```c
// duplicateLoadedOutputs(OpBuilder&, OperandRange returnOperands, Operation* returnOp, MLIRContext* ctx)
//   MHLO 0x21971e0 (1661 B, inlines copy + fusion descent)
//   SHLO 0x21465f0 (951 B, clean reference — delegates to helpers)
function duplicateLoadedOutputs(builder, returnOperands, returnOp, ctx):
    for i in 0 .. returnOperands.size()-1:
        out_i = returnOperands[i]
        op    = out_i.getDefiningOp()                  // 0x9b75f40 ; null ⇒ block-argument

        // ---- CASE 2 : block-argument (a "loaded" input value) ----
        if op == null:
            shp = ShapedType(out_i.getType())
            if shp.getNumElements() != 0:              // 0x9b218e0 ; empty falls through to zero-sized stage
                COPY_AND_NAME(out_i, i)                // duplicate the input → distinct output buffer
            continue

        // ---- CASE 1 : constant ----
        if TypeID(op) == TypeID<{mhlo,stablehlo}::ConstantOp>:
            COPY_AND_NAME(out_i, i)                    // break alias with the constant pool
            continue

        // ---- CASE 3' : ScheduleFusion / composite — name the INNER value ----
        if isScheduleFusion(op):                       // see fusion descent below
            ridx = find_if(op.getResults(), == out_i)  // which fusion result is this output
            term = Block::getTerminator(op.body.front())   // 0x9ae4360 ; mhlo.return inside fusion
            renameAsOutputImpl(i, term.operand(ridx), ctx) // name the value produced INSIDE the region
            continue

        // ---- CASE 3 : genuine compute-op result ----
        renameAsOutputImpl(i, out_i, ctx)              // MHLO 0x2197678 ; just stamp the name
        // (SHLO uses renameAsOutput 0x2145c90 here — composite-aware wrapper)

// COPY_AND_NAME — the alias-break primitive
//   MHLO: inline OpBuilder::create<mhlo::CopyOp> (0x209c570) + renameAsOutputImpl + ilist relink
//   SHLO: replaceOperandWithCopy(builder, returnOp, out_i, i, ctx)  // 0x21462d0
function COPY_AND_NAME(out_i, i):
    copy = builder.create<mhlo::CopyOp>(loc, out_i)    // identity copy — fresh op-result
    renameAsOutputImpl(i, copy.result, ctx)            // 0x2196a00 ; name the copy as output i
    returnOp.setOperand(i, copy)                       // rewire ; MHLO ilist splice via returnOp+0x48
```

### The fusion-region descent (CASE 3′)

The two variants diverge on *how* a fusion's output is named, because MHLO carries fusions as `mhlo.fusion` and StableHLO as `stablehlo.composite`.

```c
// MHLO isScheduleFusion + descent — 0x21975f0..0x219780f
function isScheduleFusion_MHLO(op) -> bool:
    if TypeID(op) != TypeID<mhlo::FusionOp>: return false
    kind = op.getInherentAttr("FusionKind")            // key 0x266931, read at 0x2197624 / 0x219764e
    return kind.getValue() == "ScheduleFusion"         // 0x25e519, byte-checked, len 0x0E

// StableHLO equivalent lives in renameAsOutput — 0x2145c90
function renameAsOutput_SHLO(i, v, ctx):
    op = v.getDefiningOp()
    if TypeID(op) == TypeID<stablehlo::CompositeOp> and op.getInherentAttr("CompositeKind"):  // 0x21a28c
        inner = hilo::getCompositeReturnOp(op)         // 0x21bffe0 — resolve decomposition return
        renameAsOutputImpl(i, inner, ctx)
    else:
        renameAsOutputImpl(i, v, ctx)

// hilo::getCompositeReturnOp — 0x21bffe0 (276 B)
function getCompositeReturnOp(composite):
    decompName = composite.getDecomposition()          // 0x.. CompositeOp::getDecomposition
    fn = SymbolTable::lookupSymbolIn(module, decompName) // 0x.. resolve the decomposition func
    return fn.getReturnValue()                          // the inner value to be named
```

> **QUIRK —** for a `ScheduleFusion` (the AG/RS schedule-fusion produced by the [Schedule-Fusion pass](schedule-fusion-composite.md)) the output name is applied to the value produced **inside** the fusion region — the matching operand of the inner `mhlo.return` — *not* to the fusion op itself. Naming the fusion op would label the wrong SSA value when the Penguin emitter unrolls the region. Non-`ScheduleFusion` fusions take CASE 3 (name the fusion op result directly). The `find_if` over `op.getResults()` (`0x2196470`, an `_Iter_equals_val` over `ResultRange`) maps this output's `Value` to the result index that selects which inner-return operand to name.

> **NOTE —** CASE 2's empty-tensor fallthrough is deliberate: a zero-element block-argument output is *not* copied here. In the StableHLO pass it was already replaced by an `AwsNeuronZeroSizedOp` in stage (B); the MHLO pass has no zero-sized stage and simply leaves an empty returned input un-copied (an edge case the MHLO front end is not expected to produce).

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `TensorizerLegalizationPass::duplicateLoadedOutputs` | `0x21971e0` | 1661 | MHLO per-output legalizer (3 cases inline) | CERTAIN |
| `StableHLOTensorizerLegalizationPass::duplicateLoadedOutputs` | `0x21465f0` | 951 | SHLO per-output legalizer (delegates) | CERTAIN |
| `replaceOperandWithCopy` | `0x21462d0` | 792 | SHLO copy + `neuron.actual_shape` + rename + rewire | CERTAIN |
| `renameAsOutput` (SHLO wrapper) | `0x2145c90` | 478 | composite-aware: descends `stablehlo.composite` | CERTAIN |
| `getCompositeReturnOp` | `0x21bffe0` | 276 | resolve composite decomposition → inner return | HIGH |

> **GOTCHA —** the copy op is `mhlo.CopyOp` in **both** passes — the StableHLO legalizer depends on the `mhlo` dialect (declared in `getDependentDialects`) purely to materialize the identity carrier. A reimplementation that emits a `stablehlo.*` copy in the StableHLO path diverges from the binary, which routes through the single `OpBuilder::create<mhlo::CopyOp>` instantiation at `0x209c570`.

---

## `replaceOperandWithCopy` + `neuron.actual_shape`

### Purpose

The StableHLO copy-insert helper. It creates the identity `mhlo.CopyOp`, records the logical tensor shape on the copy via `neuron.actual_shape` (so the backend can recover the pre-legalization geometry), names the copy, and rewires the return operand.

### Algorithm

```c
// replaceOperandWithCopy(OpBuilder&, Operation* returnOp, Value& v, unsigned i, MLIRContext* ctx)
//   StableHLO 0x21462d0 (792 B)
function replaceOperandWithCopy(builder, returnOp, v, i, ctx):
    copy = builder.create<mhlo::CopyOp>(loc, v)        // identity copy of the aliasing value
    if not v.getDefiningOp().hasAttr("neuron.actual_shape"):   // key 0x24a5f0, len 0x13 = 19
        copy.setAttr("neuron.actual_shape", StringAttr(logicalShape))  // record shape on the copy
    renameAsOutputImpl(i, copy.result, ctx)            // 0x2145680 ; name the copy as output i
    returnOp.setOperand(i, copy)                       // ilist splice via returnOp+0x48
```

`neuron.actual_shape` is a StableHLO-path annotation. The string (`0x24a5f0`) is referenced by exactly three functions — `replaceOperandWithCopy`, `legalizeZeroSizedTensors`, and the printer's `StableHLOToPythonPrinter::collectTensorAttributes` — all on the StableHLO side, and `collectTensorAttributes` is its only consumer.

> **GOTCHA — the MHLO inline copies carry no `neuron.actual_shape`.** MHLO's `duplicateLoadedOutputs` references no shape string anywhere in its constant or parameter copy paths; it relies on `renameAsOutputImpl` alone. Assuming the annotation is present on both paths will mislead any backend that tries to recover pre-legalization geometry from an MHLO-produced copy.

---

## `renameAsOutputImpl` — the naming scheme

### Purpose

Stamp the output's symbol name onto its producing op. The naming attribute is `neuron.symName`, an `ArrayAttr<SymbolRefAttr>` with **one slot per op-result**; multi-result ops accumulate names result-by-result across successive calls.

### Algorithm

```c
// renameAsOutputImpl(unsigned outputIdx, mlir::Value v, MLIRContext* ctx)
//   MHLO 0x2196a00 / StableHLO 0x2145680 (1551 B each, identical machinery)
function renameAsOutputImpl(outputIdx, v, ctx):
    name = this->outputNames_[outputIdx]               // [this+0x150] + outputIdx*0x20 → {ptr,len}
    op   = v.getDefiningOp()
    ridx = find_if(op.getResults(), == v)              // which op-result is THIS value

    existing = op.getAttr("neuron.symName")            // key 0x27ae21 ; getInherentAttr OR DictionaryAttr
    if existing == null:                               // first output named on this op
        seed = SymbolRefAttr::get(ctx, "")             // empty leaf-ref placeholder (NUL @0x23656d)
        arr  = ArrayAttr::get(ctx, [seed, seed, ...])  // one slot per result, all empty
    else:
        arr  = existing.getValue()                     // reuse + grow (SmallVector grow_pod + memcpy)

    arr[ridx] = SymbolRefAttr::get(ctx, name)          // write THIS result's name
    op.setAttr("neuron.symName", arr)                  // NamedAttrList(op.attrs).set + getDictionary
                                                       // (setInherentAttr if op has an inherent slot)
```

### The scheme, in detail

`neuron.symName` is an **`ArrayAttr` of `SymbolRefAttr`** on the *producing op*, indexed by op-result number. The i-th entry is a flat `SymbolRefAttr` whose root reference is the module output name for that result. The empty `SymbolRefAttr("")` (root = single NUL `@0x23656d`) is the initial fill for not-yet-named results; `find_if` over `op.getResults()` maps each named `Value` back to its result slot.

> **QUIRK —** output names are wrapped as `SymbolRefAttr`, **not** a bare `StringAttr`. This lets the Penguin emitter treat output names as symbol references in `getOutputTensors`/`printDsts` — uniform with how it handles other symbol-named tensors. A reimplementation that stores names as plain strings would not round-trip through the printer's symbol-ref reader.

> **GOTCHA —** the attribute lives on the *op*, accumulated across results, not on the `func.return` and not one-attr-per-output. A two-result op that feeds two module outputs is visited twice by `duplicateLoadedOutputs`; the first call seeds the full-width array with empties and writes slot 0, the second reuses-and-grows and writes slot 1. Reset the array per op and you lose the first output's name.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `TensorizerLegalizationPass::renameAsOutputImpl` | `0x2196a00` | 1551 | stamp `neuron.symName` ArrayAttr (MHLO) | CERTAIN |
| `StableHLOTensorizerLegalizationPass::renameAsOutputImpl` | `0x2145680` | 1551 | same, StableHLO | CERTAIN |
| `hilo::getOutputNames` | `0x21c2760` | 362 | read `output_names` attr or fall back | CERTAIN |
| `hilo::parseNamesAttr` | `0x21c1fb0` | 645 | decode `output_names` ArrayAttr → vec<string> | HIGH |
| `hilo::generateOutputNames` | `0x21c2270` | 1239 | synthesize `output0`, `output1`, … defaults | HIGH |

---

## Output names — `getOutputNames`

`hilo::getOutputNames` (`0x21c2760`) supplies the name list `renameAsOutputImpl` indexes. It reads the func's `output_names` inherent attribute (key `output_names` @ `0x21a069`, len 12) via `Operation::getInherentAttr(StringRef)` or `DictionaryAttr::get`; if present and an `ArrayAttr`, it decodes through `hilo::parseNamesAttr`; otherwise it synthesizes defaults through `hilo::generateOutputNames` using the literal `output` (`0x21dfc1`) — building `output0`, `output1`, … by appending the terminator-operand index.

```c
// hilo::getOutputNames — 0x21c2760 (362 B)
function getOutputNames(func) -> vector<string>:
    attr = func.getInherentAttr("output_names")        // 0x21a069 ; or DictionaryAttr::contains/get
    if attr != null and TypeID(attr) == TypeID<ArrayAttr>:
        return hilo::parseNamesAttr(attr)              // 0x21c1fb0
    return hilo::generateOutputNames(func)             // 0x21c2270 ; "output" + index
```

> **NOTE —** the string `output_names` at `0x21a069` shows only `InferMissingMLIRInfo` in its textual `referenced_by`; `getOutputNames` reaches it through a runtime `StringRef` argument to `getInherentAttr`, so it does not appear as a literal x-ref in `getOutputNames`. This is consistent with `InferMissingMLIRInfo` being the pass that *writes* `output_names` upstream and `getOutputNames` *reading* it here. Do not conflate this with `neff_output_names` (`0x22e17d`), a separate NEFF-layer string this pass never touches.

---

## MHLO vs StableHLO divergence

| Facet | MHLO `TensorizerLegalizationPass` | StableHLO `StableHLOTensorizerLegalizationPass` |
|---|---|---|
| `runOnOperation` | `0x2197940` (312 B): names + walk only | `0x2147660` (1834 B): 3 ordered stages |
| Large-constant splat legalization | ABSENT | INLINED (splat const → scalar + `broadcast_in_dim`) |
| Zero-sized-tensor legalization | ABSENT | `legalizeZeroSizedTensors` `0x2146a90` → `AwsNeuronZeroSizedOp` custom call |
| Copy insertion | INLINE `OpBuilder::create<mhlo::CopyOp>` | `replaceOperandWithCopy` `0x21462d0` |
| `neuron.actual_shape` on copy | omitted on inline copies | stamped (string x-ref present) |
| Fusion-region output naming | `mhlo::FusionOp` + `FusionKind=="ScheduleFusion"` → `Block::getTerminator` (`0x21975f0`) | `stablehlo::CompositeOp` + `CompositeKind` → `getCompositeReturnOp` (decomposition func return) |
| Public `renameAsOutput` wrapper | NONE (callers use `…Impl` directly) | `renameAsOutput` `0x2145c90` (composite-aware) |
| `getDependentDialects` | not specialized | `0x2145490` (registers `mhlo` + `stablehlo` + `func`) |
| Naming attr / scheme | identical — `neuron.symName` `ArrayAttr<SymbolRefAttr>`, per-result | identical |
| Output-name source | identical — `hilo::getOutputNames` | identical |

The StableHLO twin is a strictly **richer** pass: it adds whole legalization stages the MHLO pass lacks (splat-const, zero-sized), consistent with the broader two-pipeline split where every pass has a StableHLO sibling but the siblings are not mechanical clones.

## Related Components

| Name | Relationship |
|---|---|
| `MhloToPythonPrinter` / `StableHLOToPythonPrinter` | Consume `neuron.symName` in `getUniqueName`/`getOutputTensors` and `neuron.actual_shape` in `collectTensorAttributes` — the downstream readers of everything this pass stamps |
| `hilo::ScheduleFusion` / `StableHLOScheduleFusion` | Produce the `ScheduleFusion` fusions whose inner region-return value this pass names (CASE 3′) |
| `InferMissingMLIRInfo` | Writes the `output_names` attr this pass reads |
| `AwsNeuronZeroSizedOp` (`0x21a33f`) | The custom-call target the StableHLO zero-sized stage emits |

## Cross-References

- [CanonicalizeForTensorizer Rewriters](canonicalize-for-tensorizer.md) — 4.41, the immediately-prior tensorizer-targeted normalization that runs before this output-side legalization
- [Schedule-Fusion & Fusion-to-Composite](schedule-fusion-composite.md) — produces the `ScheduleFusion` fusions/composites whose inner region-return value this pass names in CASE 3′
- [Input/Output Alias Family](io-alias-family.md) — 4.17, the broader alias-legalization family; this pass is the *output-side* member that breaks input/constant==output aliasing
- [StaticIOTranspose](../frontend/static-io-transpose.md) — 3.15, the IO-transpose job downstream that consumes the named, de-aliased outputs this pass produces
- [hlo2penguin MLIR Pipeline Order & Entry Flow](hlo2penguin-mlir-pipeline.md) — where `tensorizer-legalization` sits in the pass schedule relative to fusion and Penguin emission
