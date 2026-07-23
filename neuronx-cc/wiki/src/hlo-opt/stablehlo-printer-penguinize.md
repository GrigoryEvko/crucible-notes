# StableHLO Printer & PenguinizeFunctions

> *All addresses on this page apply to the `hlo2penguin` binary inside `neuronx_cc 2.24.5133.0+58f8de22` (cp310 wheel, 282,927 functions). Offsets are RVA: `.rodata` file-offset = VA − 0x200000, `.text` file-offset = VA − 0x201000; VA ≠ file-offset for this binary. The companion `cp311`/`cp312` builds differ in addresses but not in algorithm.*

## Abstract

This is the closing page of the hlo2penguin chapter. Two components meet here. The first is `mlir::StableHLOToPythonPrinter` — the StableHLO-pipeline twin of the MHLO textual emitter reversed in [4.43](./mhlo-to-python-printer-driver.md)/[4.44](./mhlo-to-python-printer-heavy.md) — which serialises the legalized StableHLO module into a `neuronxcc.starfish.penguin` Python program. The second is `PenguinizeFunctions` (whose public factory is misleadingly called `createPenguinizeIOPass`), the **final** MLIR pass that runs *before* the printer fires: it packages the `main` function into the exact shape the printer's prologue assumes — named inputs, a weight-vs-activation partition, and a tuple-free I/O boundary.

The two are sequenced producer→consumer. `PenguinizeFunctions::runOnOperation` (@0x20881f0) stamps three attributes — `input_names`, `slowChangingIns`, `slowChangingOuts` — onto the `main` `func.func` and flattens its result tuple; the printer's `printStart` and `print<func::ReturnOp>` then read those attributes back to emit `m.Function(...)`, per-input `Tensor.markInput(..., is_parameter_tensor=…)`, and per-output destinations. Get the pass wrong and the printer emits a malformed prologue; get the printer wrong and Part 5's Penguin middle-end ingests the wrong Python.

The page is organised as: the StableHLO printer's *delta* against the MHLO printer (it shares the entire driver tier, so only the composite/MX/device-print emitters are new); the `compOpStk` nesting stack that the composite→private-func split forces; `PenguinizeFunctions`'s five sequential actions and the I/O-boundary contract it guarantees; and a closing note on the artifact that hands off to Part 5. For reimplementation the contract is:

- **The two StableHLO dispatch surfaces** — `print<CompositeOp>` keyed by the `CompositeKind` inherent attr, and `print<CustomCallOp>` keyed by call-target name — and which emitter each routes to.
- **The composite emission shape** — a summary `NeuronTensorOp` plus a *transparent* re-emission of the private decomposition-func body, with `compOpStk` recovering the parent context for the inner ops.
- **The five `PenguinizeFunctions` actions** — input naming, find-`main`, AWSNTWSR-strip + slow-changing-record, `slowChangingIns/Outs` stamping, result-tuple flatten + `setFunctionType`.
- **The I/O-boundary contract** — exactly which attributes survive onto `main` and which printer routine consumes each.

| | |
|---|---|
| **StableHLO printer class** | `mlir::StableHLOToPythonPrinter` |
| **Composite dispatch** | `print<stablehlo::CompositeOp>` @0x21947e0 — 7-way `CompositeKind` `std::string::compare` |
| **Custom-call dispatch** | `print<stablehlo::CustomCallOp>` @0x216d7a0 — ~19-entry `StringMap<function<void(Operation*)>>` |
| **Nesting stack** | `compOpStk` @0x9c707c0 — `std::stack<Operation*, std::deque<…>>` |
| **Final packaging pass** | `PenguinizeFunctions::runOnOperation` @0x20881f0 (factory `createPenguinizeIOPass` @0x2087080) |
| **StableHLO packaging twin** | `StableHLOPenguinizeFunctions::runOnOperation` @0x21273d0 (factory @0x2126c70) |
| **Boundary attrs produced** | `input_names`, `slowChangingIns` (@0x236853), `slowChangingOuts` (@0x27698e) |
| **Load-param marker stripped** | `AwsNeuronTransferWithStaticRing` (@0x2dfd28, len 31) |
| **Hands off to** | Part 5 — the emitted `neuronxcc.starfish.penguin` Python program |

---

## 1. The Printer Delta: What StableHLO Adds Over MHLO

`StableHLOToPythonPrinter` shares the **entire driver tier** with the MHLO printer of [4.43](./mhlo-to-python-printer-driver.md)/[4.44](./mhlo-to-python-printer-heavy.md): the same `printOperation` TypeID cascade, the same `printOperandsAndAttributes` statement assembler, the same `printSrcs`/`printDsts`/`printMeta`/`printType`/`getImport`/`defScalar`/`getScalar` helpers, the same `m<N>`/`input_<i>`/`v<N>` name families, and the same `perOpCounter` static `StringMap`. The two classes funnel every ordinary op through identical code. **What differs is confined to three emitter families**, because StableHLO carries fusions and a few custom-calls differently than MHLO does.

### Two dispatch surfaces

The StableHLO printer has two *separate* dispatch points that the MHLO printer does not exercise in the same way:

| Surface | Address | Keyed on | Routes to |
|---|---|---|---|
| `print<stablehlo::CompositeOp>` | 0x21947e0 | `CompositeKind` **inherent** attr (string `@0x21a28c`) | the four composite emitters (§2) |
| `print<stablehlo::CustomCallOp>` | 0x216d7a0 | `getCallTargetName()` via a per-call `StringMap` (~19 entries) | MX + device-print emitters (§3) and the generic native-kernel body |

The crucial asymmetry: **composites are routed by an inherent attribute, custom-calls by their target-name string.** A composite never reaches the `StringMap`; a custom-call never consults `CompositeKind`. The custom-call `StringMap` was observed in the binary as a contiguous run of `std::_Function_handler` thunks (`…printINS0_9stablehlo12CustomCallOpEEE…UlS2_E0_E` … `E17_E`), confirming the ~19-lambda count cited for the per-call dispatch table.

> **NOTE —** The MHLO printer has the analogous `print<CustomCallOp>` `StringMap` (the `AwsNeuronDevicePrint` handler is the same shape there). What it lacks entirely is the `CompositeOp` surface — MHLO fusions are `mhlo.fusion` ops, not `stablehlo.composite` ops. See §2 for why that one representational difference forces an entire nesting stack into existence.

### Why the printers diverge at all

The divergence is a downstream consequence of *how each pipeline represents a fusion*, decided long before the printer:

| Aspect | MHLO printer ([4.43](./mhlo-to-python-printer-driver.md)) | StableHLO printer (this page) |
|---|---|---|
| Fusion carrier | `mhlo.fusion` — body **inline** in the op's region | `stablehlo.composite` + a **private `func.func` decomposition** |
| Fusion dispatch key | `FusionKind` on the `FusionOp` | `CompositeKind` inherent attr on the `CompositeOp` |
| Decomp-func indirection | none (region is inline) | `getDecomposition()` + `SymbolTable::lookupSymbolIn` + operand→arg aliasing |
| Nesting stack | **none** | `compOpStk` @0x9c707c0 (push pre-dispatch, pop post) |
| Emitted Python shape | `NeuronTensorOp(xla_op='mhlo.fusion', op=…, hilo_fusion_op=…)` + body | **same shape** |
| MX `QuantizeMX` / scaled-dot | absent | present (§3) |

> **GOTCHA —** The emitted *Python* is the same shape in both printers. The StableHLO emitters do strictly **more work** (resolve the private decomposition func, alias the composite's operands onto the func's block-args, push/pop `compOpStk`) to reconstruct the same `NeuronTensorOp` summary that the MHLO emitters get for free from an inline region. The genuinely StableHLO-exclusive *output* is the MX path (§3) and the `hilo_fusion_op` provenance attribute that keeps `Expm1`/`Log1p` distinct under a shared Python op name.

---

## 2. Composite Emission and the `compOpStk` Nesting Stack

### The 7-way CompositeKind dispatch

`print<stablehlo::CompositeOp>` @0x21947e0 reads the `CompositeKind` StringAttr (the inherent-attr slot at `op+0x2F`, falling back to the dictionary attr), resolves the named `decomposition` func, **aliases the composite's operands onto that func's block-arguments** (so inner ops reference the composite's real SSA names), pushes the composite onto `compOpStk`, runs a 7-way `std::string::compare` switch, then pops. The seven kinds collapse onto four emitters:

```c
// print<stablehlo::CompositeOp>(CompositeOp op)  @0x21947e0
void print_CompositeOp(CompositeOp op) {
    StringAttr kind = op.getInherentAttr("CompositeKind");   // key @0x21a28c

    // resolve the private decomposition func (emitters re-resolve it too)
    StringRef decompName = op.getDecomposition();
    ModuleOp  mod = walkUpToModule(op);
    func::FuncOp fn = SymbolTable::lookupSymbolIn(mod, decompName);

    // alias composite operand i -> decomposition func block-arg i
    for (unsigned i = 0; i < op.getNumOperands(); ++i)
        defScalar(fn.getArgument(i), getScalar(op.getOperand(i)));

    compOpStk.push(op);                       // nesting marker — see §2.3

    StringRef s = kind.getValue();
    if      (s == "ScheduleFusion") printScheduleCompositeOp(op);            // @0x2194490
    else if (s == "MulRedSqrt")     printMulRedSqrtCompositeOp(op);          // @0x2191450
    else if (s == "DotLogistic")    printDotLogisticCompositeOp(op);         // @0x2192620
    else if (s == "Expm1")          printArbitraryCompositeOp(op,"Elementwise","Expm1");
    else if (s == "Log1p")          printArbitraryCompositeOp(op,"Elementwise","Log1p");
    else if (s == "Elementwise")    printArbitraryCompositeOp(op,"Elementwise","Elementwise");
    else if (s == "DotSoftmax")     printArbitraryCompositeOp(op,"DotSoftmax","DotSoftmax");
    else  /* incl. default "FusedComposite" */
        NEURON_LOG(ERROR, "StableHLOToPythonPrinter.cc", 4049)              // NCC_ISPP054 @0x256145
            << "unknown CompositeKind";       // fatal-style diagnostic; no emitter runs

    compOpStk.pop();
}
```

`printArbitraryCompositeOp` (@0x2193630) takes **two** `StringRef`s: `(pyName, kind)`. `pyName` is the Python op name; `kind` is the literal `CompositeKind` value, emitted verbatim as `hilo_fusion_op='<kind>'`. So `Expm1` and `Log1p` both print as Python op `'Elementwise'` but keep their distinct `CompositeKind` in `hilo_fusion_op` — the provenance survives into the Penguin metadata even though the op name is collapsed.

### The four composite emitters

All four share one shape: each emitter walks the decomposition body and re-emits it via `printOperation` after the summary node.

| Emitter | Addr | Python op | Extra attr | Notes |
|---|---|---|---|---|
| `printArbitraryCompositeOp` | 0x2193630 | `'Elementwise'` / `'DotSoftmax'` | — | `Expm1`/`Log1p`/`Elementwise`→`Elementwise`; `DotSoftmax`→`DotSoftmax` |
| `printDotLogisticCompositeOp` | 0x2192620 | **`'DotFusion'`** | — | finds a `DotGeneralOp` *or* `DotOp` in the body (advisory; no fatal) |
| `printMulRedSqrtCompositeOp` | 0x2191450 | `'MulRedSqrt'` | `reduce_dims=[…]` | the only emitter with a kind-specific attr |
| `printScheduleCompositeOp` | 0x2194490 | *(none)* | — | purely transparent — **no summary node** |

The general shape is a single summary statement followed by the inner graph:

```c
// COMMON(op) — shape of the three non-Schedule emitters
v   = defScalar(void);                          // fresh `v<N>`
ret = hilo::getCompositeReturnOp(op);           // @0x21bffe0 — decomp func's func.return
fn  = SymbolTable::lookupSymbolIn(walkUpToModule(op), op.getDecomposition());

// summary node — srcs remapped from decomp-return operands back to composite operand names
emit("%s = m<XlaFE>.NeuronTensorOp(srcs=[…], dsts=%s,",  v, getOutputTensors(op));
emit("    xla_op='mhlo.fusion', op='%s', hilo_fusion_op='%s'", pyName, kind);   // single-quoted
if (kind == "MulRedSqrt")
    emit(", reduce_dims=%s", formatList(reduceOp.getDimensions()));   // key @0x22a652
emit(", dl=m<S>.DebugLocation(…))\n");

// TRANSPARENT decomposition body — re-emit the actual inner graph
for (Operation &inner : fn.body())
    if (!isa<func::ReturnOp>(inner)) printOperation(&inner);
```

> **QUIRK —** `DotLogistic`'s Python op name is **`'DotFusion'`** (`@0x25a294`), not `'DotLogistic'`. The `CompositeKind` `DotLogistic` rides only in `hilo_fusion_op`. A reimplementer who maps the dispatch key directly to the op name will mislabel exactly this one case.

> **NOTE —** `printScheduleCompositeOp` emits **no** `NeuronTensorOp` summary at all — it is purely transparent. It re-emits the composite's two inner collectives (`ReduceScatter`, `AllGather`, plus their GTEs) directly, in the rescheduled adjacency order, then binds each composite result to the corresponding decomp-return operand. The rescheduling decided upstream by Schedule-Fusion ([4.x](./schedule-fusion-composite.md)) survives into the Penguin Python as plain emission order; Part 5/Walrus later turns that adjacency into semaphore-pipelined overlap.

The `xla_op='mhlo.fusion'` tag is stamped even by the StableHLO emitter — it carries the MHLO-era tag string regardless of which dialect produced the composite.

### `compOpStk` — why the composite split needs a stack

`compOpStk` @0x9c707c0 is a process-global `std::stack<mlir::Operation*, std::deque<mlir::Operation*>>` (symbol `_ZN4mlir24StableHLOToPythonPrinter9compOpStkE`, present in `names.json`; deque node size 0x200 = 64 `Operation*` per node). The dispatch **pushes** the composite before delegating and **pops** after the emitter returns. Two readers consume `compOpStk.top()` while the decomposition body emits:

- **`printMeta`** (@0x2154390) — if the stack is non-empty and the current op's parent is a `stablehlo::CompositeOp`, it uses that composite as the `parent=` context for the inner op's `DebugLocation`. Each decomposed inner op's `dl=…DebugLocation(parent=…)` therefore points at the enclosing composite, not at the private `func.func`.
- **`printGlobalOp`** (@0x215c190) — same non-empty read, so globals/constants produced inside a composite are registered under the composite's ownership.

The stack exists **only** because a `stablehlo.composite` decomposes into a *separate* private `func.func`, so while that func's body emits, the printer has lost the enclosing-composite context and must recover it from somewhere. The MHLO printer has no `compOpStk`: a `mhlo.fusion` body is inline, so the parent is reachable by ordinary region-walking. Push/pop nests correctly if composites contain composites.

> **GOTCHA —** `compOpStk` is a *process-global static* (like `perOpCounter`). It is balanced (push-then-pop per composite) and benign for the single-module compile path, but a reimplementer building a re-entrant or threaded printer must make it per-invocation. It is not state the IR carries — it is printer-side scratch.

---

## 3. The MX and Device-Print Custom-Call Emitters

These three are reached from `print<stablehlo::CustomCallOp>` @0x216d7a0 by **call-target name**, not by `CompositeKind`. They consume the un-suffixed custom-call names that the HLO MX passes ([4.x](./mx-fp8-legalization.md)) emit, reading `block_size`/`dim`/`scale_method` straight out of the `backend_config`.

| Target name | Lambda | Emitter | Addr |
|---|---|---|---|
| `"AwsNeuronDevicePrint"` | #14 | `printDevicePrintCustomCall` | 0x216d0f0 |
| `"QuantizeMX"` (`@0x215dea`) | #17 | `printQuantizeMX` | 0x218b0c0 |
| `"__op$block_scaled_dot"` (`@0x28307c`) | #19 | `printScaleMatmult` | 0x218a280 |

### `printQuantizeMX` → `QuantizeMXTensorOp`

```c
// printQuantizeMX(Operation* op)  @0x218b0c0
void printQuantizeMX(Operation *op) {
    StringAttr cfg = cast<CustomCallOp>(op).getBackendConfig();
    if (!cfg)                                                  // @0x218b188
        NEURON_LOG(ERROR, "StableHLOToPythonPrinter.cc", 3276) // NCC_EVRF036: "backend_config is missing"
            ;
    json j = nlohmann::json::parse(cfg);                       // v3.11.3 lexer+parser
    if (j.is_discarded())
        NEURON_LOG(ERROR, "StableHLOToPythonPrinter.cc", 3286);// NCC_EVRF036: "malformed JSON"

    string dtype      = j.value("dtype", "");        // scale FP8 dtype   key @0x2326c3
    int    dim        = j.value("dim", 0);           //                   key @0x227e91
    int    block_size = j.value("block_size", 32);   // preset 32 (0x20)  key @0x222293
    string scale_meth = j.value("scale_method", "EMAX");  //              key @0x23a66e

    dim = normalizeAxis(dim, op->getOperand(0));     // negative-index fold

    // x4-pack: if result element type is u32, override dst dtype with packed FP8 container
    if (op->getResult(0).getElementType().isUnsignedInteger(32)) {
        if (dtype == "float8_e5m2")   dstDtype = "<support>.dtype.float8_e5m2_x4";
        if (dtype == "float8_e4m3fn") dstDtype = "<support>.dtype.float8_e4m3fn_x4";
    }

    emit("%s = m<Intrinsics>.QuantizeMXTensorOp(",  printDsts(op));    // ".QuantizeMXTensorOp(" @0x232801
    emit("    %s, %s,", printSrcs(op), printDsts(op));
    emit("    mx_dtype='%s', dim=%d, block_size=%d, scale_method='%s',", dtype, dim, block_size, scale_meth);
    emit("    target_name=%s, %s)\n", quote(op.getCallTargetName()), printMeta(op));
}
```

`Intrinsics` resolves to `neuronxcc.starfish.penguin.ir.Intrinsics`; `<support>` to `neuronxcc.starfish.support`. The result is one Penguin Intrinsics call carrying the full MX config; `dsts` = {x4-packed quantized data (U32), E8M0 per-block scale}. The `block_size=32` / E8M0 geometry is inherited from the HLO custom-call — the printer only marshals the config into attrs and packs the data tensor x4.

### `printScaleMatmult` → a regular `DotOp`

`printScaleMatmult` (@0x218a280) does **not** emit a bespoke op. It reads the `backend_config` via `getBackendConfigWithEmptyHandling` (@0x214cc80, `StatusOr<string>`, `""` if empty), parses the nested `scaled_dot_backend_config` object via `parseScaleMatmulConfig` (@0x2189590) into four dim-number lists (`lhs/rhs_batch_dimensions`, `lhs/rhs_contracting_dimensions`) plus `element_dtype`, then **delegates to the general `printDotOp`** with op-tag `"'mhlo.scaled_dot'"` (`@0x2523da`):

```c
printDotOp(op, lhs_batch_dims, rhs_batch_dims,
               lhs_contracting_dims, rhs_contracting_dims,
               std::string("'mhlo.scaled_dot'"));   // call @0x218a58e
```

So an MX scaled-matmul appears in the emitted Python as an ordinary `DotOp`: `srcs=[lhs, rhs, lhs_scale, rhs_scale]` (operands 2/3 are the E8M0 scales, riding as extra positional srcs), `op="'mhlo.scaled_dot'"`, with the contracting/batch dims and `element_dtype` carried as `printDotOp`'s attrs. There is **no** MHLO twin for this path — the JAX `__op$block_scaled_dot` composite is a StableHLO-only construct.

### `printDevicePrintCustomCall` — a thin spine handler

`printDevicePrintCustomCall` (@0x216d0f0) is a thin handler that funnels through the **shared** `printOperandsAndAttributes` spine — not a `NeuronTensorOp`/`Intrinsics` summary:

```c
v = defScalar(void);
attrs = [ ("xla_op", quote("mhlo.device_print")),     // val @0x25e5d6
          ("prefix", cc.getBackendConfig() ? quote(cc.getBackendConfig().getValue()) : "") ];  // key @0x22e44c
printOperandsAndAttributes(op, …, attrs);
// emits:  <v> = m<N>.<OpName>(srcs=[…], dsts=[…], xla_op='mhlo.device_print', prefix='<text>', dl=…)
```

A device-print becomes a normal side-effecting Penguin op statement with two frontend attrs: the universal `xla_op` tag and the user-supplied `prefix` lifted from `backend_config`. The data tensors to print are the op's operands.

---

## 4. PenguinizeFunctions — the Final Packaging Pass

### One pass, two names

"`PenguinizeFunctions`" and "`PenguinizeIO`" name **one** pass, not two stages. `createPenguinizeIOPass` @0x2087080 does `operator new(0x180)` and constructs a **`PenguinizeFunctions`** object (guard string `"PenguinizeFunctions]"`, vtable `off_41D770`); `createStableHLOPenguinizeIOPass` @0x2126c70 is byte-identical but constructs a **`StableHLOPenguinizeFunctions`**. `names.json` carries **no** `PenguinizeIO` class symbol — no `runOnOperation`, no vtable, no typeinfo. "PenguinizeIO" is purely the public factory / pass-argument name; both the function packaging and the I/O-boundary contract live inside the single `PenguinizeFunctions::runOnOperation`.

> **GOTCHA —** the factory name and the class name disagree. Searching the binary for a `PenguinizeIO` pass class finds nothing — the implementation is `PenguinizeFunctions`, reachable only through that factory.

Both pass classes are GLOBAL-namespace (`_ZN19PenguinizeFunctions…` / `_ZN28StableHLOPenguinizeFunctions…`), `mlir::PassWrapper<…, OperationPass<ModuleOp>>`, op-filter `"builtin.module"`.

### Five sequential actions

`runOnOperation` (@0x20881f0) performs five actions in order on the module:

```c
// PenguinizeFunctions::runOnOperation()  @0x20881f0   (ModuleOp = this->op)
void runOnOperation() {
    // (1) INPUT NAMING                                                       @0x2088231
    this->inputNames = hilo::getInputNames(module);   // @0x21c2e30 -> reads "input_names" ArrayAttr
                                                      //   else hilo::generateInputNames (default synthesis)

    // (2) FIND MAIN                                                          @0x208830f
    func::FuncOp main = nullptr;
    module.walk([&](func::FuncOp f){                  // lambda#1 @0x2087cc0
        if (f.getSymbolName() == "main") main = f;    // len==4 && "main" (0x6E69616D)
    });

    // (3) STRIP AWSNTWSR + RECORD SLOW-CHANGING                              lambda#2 @0x2087ee0
    SmallVector<unsigned> slowIns, slowOuts;
    main.walk([&](mhlo::CustomCallOp cc){
        if (cc.getCallTargetName() != "AwsNeuronTransferWithStaticRing") return;  // len 31, @0x2dfd28
        cc->moveBefore(definingOpOf(cc.getOperand(0)));   // relocate near first use
        Value v = cc.getOperand(0);
        if (isBlockArgument(v))                            // operand kind &7 == 7
            slowIns.push_back(blockArgNumber(v));          // direct param input
        else
            for (OpOperand &use : v.getUses())             // use-walk path
                if (isa<mhlo::TupleOp>(use.getOwner()))
                    slowOuts.push_back(use.getOperandNumber());
        cc.erase();                                        // marker removed; load remains
    });

    // (4) STAMP COUNTS                                                       @0x208862c / @0x208878e
    for (auto [vec, key] : {{slowIns,"slowChangingIns"}, {slowOuts,"slowChangingOuts"}}) {
        auto shTy = RankedTensorType::get({vec.size()}, i32);
        auto attr = DenseElementsAttr::getRawIntOrFloat(shTy, vec.bytes(), /*elt*/4, /*isInt*/true);
        main->setInherentAttr(key, attr);              // "slowChangingIns" @0x236853 / "…Outs" @0x27698e
    }

    // (5) FLATTEN func.return + REWRITE FunctionType                         @0x2088917
    func::ReturnOp term = main.body().front().getTerminator();
    SmallVector<Value> flatV; SmallVector<Type> flatT;
    for (Value v : term.getOperands()) {
        if (auto tt = dyn_cast<TupleType>(v.getType()))
            for (unsigned j = 0; j < tt.size(); ++j) {     // explode tuple via GTE
                auto gte = builder.create<mhlo::GetTupleElementOp>(loc, tt.getType(j), v, j);
                flatV.push_back(gte); flatT.push_back(tt.getType(j));
            }
        else { flatV.push_back(v); flatT.push_back(v.getType()); }
    }
    auto newTy = FunctionType::get(ctx, funcType.getInputs(), flatT);  // inputs verbatim, results flat
    function_interface_impl::setFunctionType(main, newTy);             // @0x998e170
    builder.create<func::ReturnOp>(loc, flatV);                        // rebuild flat
    term.erase();
}
```

> **NOTE —** Action (5) flattens only the **result** side. `getInputs()` is passed through verbatim — input-side flattening, if any, happens upstream. This pass re-types the return so the printer's `getOutputTensors`/`print<func::ReturnOp>` never has to descend a `TupleType` at the boundary. The `GetTupleElementOp` and `func::ReturnOp` builders both go through `RegisteredOperationName::lookup` with a `report_fatal_error` "Building op `mhlo.get_tuple_element` … isn't known in this MLIRContext" guard if the dialect is unloaded (strings `@0x22a5a3`, `@0x226311`).

### Where the weight/parameter distinction comes from

The `AwsNeuronTransferWithStaticRing` (AWSNTWSR) marker is the compiler's "this input is a statically loaded **parameter/weight**, transferred via a static ring buffer" tag — the same target recognised HLO-side by `xla::partition::isCustomCallLoadingParam` and unpacked by `xla::UnpackNestedAWSNTWSR`. `PenguinizeFunctions` is where that marker is **consumed** at the MLIR boundary: the marker is stripped, and the parameter's arg/operand index is recorded into `slowChangingIns`. The weight-vs-activation partition is therefore an **index set**, not a dialect op or a `backend_config` field.

> **GOTCHA —** The marker op is *erased*, but the load itself **remains** (repositioned adjacent to its first use). Only the wrapping AWSNTWSR custom-call disappears. A reimplementer who deletes the loaded value along with the marker drops the weight entirely.

---

## 5. The I/O-Boundary Contract

After `PenguinizeFunctions`, the `main` `func.func` carries exactly these attributes. Each printer consumer below was located by `referenced_by_functions` on the attribute's literal string, so the producer→consumer seam is direct:

| Attr | Type | Meaning | Printer consumer |
|---|---|---|---|
| `input_names` | `ArrayAttr<StringAttr>` | per-entry-parameter Penguin input name | `printStart` / `printInput` |
| `output_names` | `ArrayAttr<StringAttr>` | per-result Penguin output name (stamped by TensorizerLegalization, [4.x](./tensorizer-legalization.md)) | `getOutputTensors` / `printDsts` |
| `neuron.symName` | `ArrayAttr<SymbolRefAttr>` | per-op-result output symbol | `getOutputTensors` / `printDsts` |
| `slowChangingIns` | `DenseI32` index array | which **input** positions are loaded weights | `printStart` @0x20e6b10 (`hasAttr` gate) |
| `slowChangingOuts` | `DenseI32` index array | which **output** positions are slow-changing | `print<func::ReturnOp>` |

Structurally, the boundary the printer walks is: inputs = entry parameters verbatim; results = **flattened** (no `TupleType` leaves); all AWSNTWSR markers removed. The printer's prologue then emits:

```python
v = m.Function(id_=0, batch_ids=[], attrs=("model-type=…", "mac-count=…", …))
# gated on hasAttr("slowChangingIns"):
input_0 = m.Tensor(...).markInput(..., is_parameter_tensor=True)   # + weight_files / np_file  — weight
input_1 = m.Tensor(...).markInput(..., is_parameter_tensor=False)  #                            — activation
```

The `slowChangingIns` index set drives `is_parameter_tensor=True` for exactly the weight inputs; `slowChangingOuts` is read symmetrically by `print<func::ReturnOp>` to classify slow- vs fast-changing outputs. This is the **end-to-end** weight distinction: HLO marks loaded params with AWSNTWSR → `PenguinizeFunctions` records the index into `slowChangingIns` → `printStart` emits `is_parameter_tensor=True` + `weight_files` for those indices.

### MHLO vs StableHLO packaging twin

> **NOTE —** Unlike the TensorizerLegalization twin ([4.x](./tensorizer-legalization.md)), where the StableHLO variant is strictly *richer* (adds large-constant-splat + zero-sized-tensor stages), the `StableHLOPenguinizeFunctions` twin (@0x21273d0) is a **mechanical dialect clone**. It differs from the MHLO pass ONLY in the `mhlo::` ↔ `stablehlo::` TypeIDs/op-names for `CustomCallOp`, `TupleOp`, and `GetTupleElementOp`; `func.return` and the `slowChangingIns/Outs` stamping are dialect-neutral and identical. No behavioural divergence, no extra stage. "Every pass has a StableHLO twin" holds, but the twins are *not* uniformly richer.

---

## 6. What Hands Off to Part 5

This is the seam where the hlo2penguin chapter ends. `PenguinizeFunctions` runs last among the MLIR passes — after TensorizerLegalization ([4.x](./tensorizer-legalization.md)) has already stamped `output_names`/`neuron.symName`, and after Fusion-to-Composite ([4.x](./schedule-fusion-composite.md)) has produced the `stablehlo.composite` ops. It adds `input_names`/`slowChanging*` and flattens the return, so the module is now in the exact form the printer's prologue assumes. `StableHLOToPythonPrinter` (or its MHLO twin) then serialises that module into a **`neuronxcc.starfish.penguin` Python program** — a textual sequence of `m.Function(...)`, `m.Tensor(...).markInput(...)`, `NeuronTensorOp(...)`, `QuantizeMXTensorOp(...)`, `DotOp(...)`, and `DebugLocation(...)` statements.

That Python text **is** the artifact Part 5 ingests. It is not MLIR — it is Penguin-IR source that, when executed, materialises `neuronxcc.starfish.penguin.ir` objects in the Penguin middle-end's process. From there the Penguin/Strand-U lowering turns each `QuantizeMXTensorOp`/scaled-dot `DotOp` into NKI `quantize_mx`/`nc_matmul_mx` and ultimately BIR MX instructions, and turns each `ScheduleFusion`'s transparently-emitted collective adjacency into semaphore-pipelined overlap. The composite *summary* `NeuronTensorOp` and its transparently re-emitted decomposition body both appear in the Python; the middle-end consumes both. **The hlo2penguin chapter ends at this emitted Python; Part 5 — Penguin IR & Middle-End — begins by parsing it.**

> **Cross-references:** [4.43 — MHLO→Py-Penguin printer driver](./mhlo-to-python-printer-driver.md), [4.44 — the Python printer helpers](./mhlo-to-python-printer-heavy.md) (shared driver tier this page builds on), [Schedule-Fusion & Fusion-to-Composite](./schedule-fusion-composite.md) (produces the composites), [TensorizerLegalizationPass](./tensorizer-legalization.md) (stamps `output_names`/`neuron.symName` before this pass), [MX-FP8 Microscaling Legalization](./mx-fp8-legalization.md) (emits the `QuantizeMX`/`__op$block_scaled_dot` custom-calls), and Part 5 — Penguin IR & Middle-End (ingests the emitted Python).
