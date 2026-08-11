# Control-Dep Tuple-Flatten (MLIR)

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/bin/hlo2penguin` (cp310). Other versions will differ. Addresses are VA; this binary was built without Hex-Rays decompilation, so structure-internal offsets are disasm-derived.*

## Abstract

`NeuronControlDepTupleSimplifier` is the MLIR-side counterpart to the HLO pass [`PreserveControlDeps`](control-dep-reification.md) (Part 4.15). Where the HLO pass *reifies* each HLO control edge `p→S` as a flat side-effecting `AwsNeuronControlDep(p0,…,p_{k-1}, D)` custom-call, this pass *undoes a tuple-wrapping artifact* that the MLIR importer introduces when that HLO module is re-imported as MHLO or StableHLO. The XLA→MLIR importer surfaces XLA tuple-shaped operands as real `mhlo.tuple` / `mhlo.get_tuple_element` SSA ops, so the flat predecessor bundle comes back as `custom_call{AwsNeuronControlDep}(mhlo.tuple(p0,…), …, D)` — a single nested-tuple operand instead of a flat predecessor list. This pass un-nests it back to a flat operand list so the Penguin printer can emit one `.add_dep_edge` per predecessor.

The component ships as a **matched MHLO/StableHLO pair**: `hilo::NeuronControlDepTupleSimplifier` (mnemonic `flatten-control-dep-tuple-operands`) and `hilo::StableHLONeuronControlDepTupleSimplifier` (`stablehlo-flatten-control-dep-tuple-operands`). The two are a structural clone differing only in dialect TypeIDs and op mnemonics; they share one recognizer, `hilo::isControlDep`, which itself dispatches on both `mhlo::CustomCallOp` and `stablehlo::CustomCallOp`. Both run as module-level `OperationPass<ModuleOp>` over the module's single main `func::FuncOp`.

For reimplementation, the contract is:

- The **flatten rewrite**: walk the main function for control-dep custom-calls that still carry a tuple/GTE operand, rebuild each with the tuple elements inlined and GTEs unwrapped, splice uses, erase the original, then garbage-collect now-dead tuple/GTE ops.
- The **selective, idempotent pre-filter**: only control-dep CCs that *still* have a tuple/GTE operand are collected — already-flat ops are skipped, so the pass is a no-op on a second run.
- The **MHLO ⇄ StableHLO twinning**: one algorithm, parameterized only by dialect TypeID and mnemonic.

| | |
|---|---|
| **MHLO pass class** | `hilo::NeuronControlDepTupleSimplifier` (`PassWrapper<…, OperationPass<ModuleOp>>`) |
| **MHLO entry** | `runOnOperation` `0x20f6a40` → `replaceTuples` `0x20f5d40` |
| **MHLO mnemonic** | `"flatten-control-dep-tuple-operands"` (`getArgument` `0x20f5620`) |
| **StableHLO entry** | `runOnOperation` `0x2132980` → `replaceTuples` `0x2131c80` |
| **StableHLO mnemonic** | `"stablehlo-flatten-control-dep-tuple-operands"` (`getArgument` `0x2131560`) |
| **Shared recognizer** | `hilo::isControlDep(Operation*)` `0x21c0370` (dual-dialect) |
| **Scope helper** | `hilo::getMainFunction(ModuleOp&)` `0x21c1e70` — first `func::FuncOp` |
| **Pass op-name** | `"builtin.module"` — module-level pass |
| **Custom-call target** | `"AwsNeuronControlDep"` (`hilo::kAwsNeuronControlDep`) |
| **Downstream consumer** | `MhloToPythonPrinter::printControlDeps` `0x20b8480` → `.add_dep_edge(` |

---

## Pipeline Position — the round-trip with 4.15

This pass is link #3 of a four-link chain. [`PreserveControlDeps`](control-dep-reification.md) (4.15) owns links #1–#2; this page owns #3; the Penguin printer owns #4.

```text
[#1 HLO]   PreserveControlDeps  (hlo-opt, 4.15)
            HLO control edge p→S  →  cc=AwsNeuronControlDep[side_effect](p0,…,p_{k-1}, D); S.operand(0)=cc
            (FLAT operands; module attr has_control_deps)
              │  HLO export → MHLO/StableHLO import
              ▼
[#2 IMPORT] importer wraps the variadic predecessor bundle in mhlo.tuple / mhlo.get_tuple_element
            mhlo.custom_call{AwsNeuronControlDep}( tuple(p0,…), …, D )           (NESTED)
              │
              ▼
[#3 THIS]   NeuronControlDepTupleSimplifier  (flatten-control-dep-tuple-operands)  0x20f6a40
            walk main-fn custom_call → isControlDep & has-tuple/GTE → replaceTuples 0x20f5d40
            mhlo.custom_call{AwsNeuronControlDep}( p0, …, p_{k-1}, D )           (FLAT again)
              │  (StableHLO twin 0x2132980 for the stablehlo path)
              ▼
[#4 EMIT]   MhloToPythonPrinter::printControlDeps  0x20b8480  (reads via isControlDep)
            → Penguin  .add_dep_edge(predecessor, successor)
            → neuronxcc.starfish.penguin.ir.Dependency → BIR ordering constraint
```

> **NOTE —** the word "tuple" in the pass name refers to the import-side `mhlo.tuple`, **not** an HLO `kTuple`. The HLO pass (4.15) emits flat operands; the tuple appears only after the MHLO/StableHLO round-trip. This pass exists purely to undo that import artifact, restoring the flatness the HLO pass produced.

The flatten in #3 is the prerequisite for #4: `printControlDeps` iterates the CC's operands to emit one `.add_dep_edge` per predecessor. If the predecessors stayed bundled inside one `mhlo.tuple` operand, the printer would see a single tuple operand instead of `k` predecessor values. The coupling between the two is the shared `isControlDep` recognizer; the printer's own body is documented in 4.15. See [Part 5 — Penguin dep edges](../penguin/dependency-model.md) for how `.add_dep_edge` becomes a BIR scheduling constraint.

---

## Pass Identity and Registration

### Purpose

Register a stateless module-level pass that finds tuple-wrapped control-dep custom-calls and rebuilds them with flat operand lists.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `hilo::NeuronControlDepTupleSimplifier::runOnOperation()` | `0x20f6a40` | getMainFunction → PreOrder walk `mhlo.custom_call` → replaceTuples | CERTAIN |
| `…::replaceTuples(SmallVectorImpl<Operation*>&)` | `0x20f5d40` | flatten tuple/GTE operands; rebuild; splice-use+erase; dead-tuple GC | CERTAIN |
| walk filter lambda (`function_ref::callback_fn<…mhlo::CustomCallOp…>`) | `0x20f59c0` | isControlDep + has-tuple/GTE pre-filter → worklist push | CERTAIN |
| `…::getArgument()` | `0x20f5620` | returns `"flatten-control-dep-tuple-operands"` | CERTAIN |
| `mlir::createNeuronControlDepTupleSimplifierPass()` | `0x20f68a0` | `new(0x150)`; OperationPass\<ModuleOp\> | CERTAIN |
| `~NeuronControlDepTupleSimplifier` D2/D0 | `0x20f5770` / `0x20f5890` | trivial dtors | CERTAIN |
| `_GLOBAL__sub_I_…createNeuronControlDepTupleSimplifierPass` | `0x20f6bc0` | static TypeID registrar | CERTAIN |
| `hilo::isControlDep(Operation*)` | `0x21c0370` | `getCallTargetName()=="AwsNeuronControlDep"` (dual-dialect) | CERTAIN |
| `hilo::getMainFunction(ModuleOp&)` | `0x21c1e70` | walk module → first `func::FuncOp` | CERTAIN |
| **StableHLO twin** | | | |
| `StableHLONeuronControlDepTupleSimplifier::runOnOperation()` | `0x2132980` | walks `stablehlo.custom_call` | CERTAIN |
| `…::replaceTuples(…)` | `0x2131c80` | structural clone; `stablehlo::{Tuple,GetTupleElement,CustomCall}Op` | CERTAIN |
| walk filter lambda (stablehlo) | `0x2131900` | isControlDep + has-tuple/GTE pre-filter | CERTAIN |
| `…::getArgument()` | `0x2131560` | `"stablehlo-flatten-control-dep-tuple-operands"` | CERTAIN |
| `mlir::createStableHLONeuronControlDepTupleSimplifierPass()` | `0x21327e0` | StableHLO factory | CERTAIN |
| **terminal consumers (NOT this pass)** | | | |
| `MhloToPythonPrinter::printControlDeps` | `0x20b8480` | reads via isControlDep → `.add_dep_edge(` | HIGH (4.15) |
| `StableHLOToPythonPrinter::printControlDeps` | `0x2153360` | StableHLO emitter twin | HIGH (4.15) |

> **NOTE —** the mangled name of the walk lambda is its own specification. `0x20f59c0` demangles to `…detail::walk<(WalkOrder)1, ForwardIterator, …runOnOperation()::lambda(mhlo::CustomCallOp)>`, which fixes **PreOrder** (`WalkOrder=1`), `ForwardIterator`, and the `mhlo::CustomCallOp` type filter without reading a byte of code; the StableHLO twin (`0x2131900`) substitutes `stablehlo::CustomCallOp`. The class is `PassWrapper<…, OperationPass<ModuleOp>>`, likewise readable from the `getName`/`clonePass` mangled symbols at `0x20f5630`/`0x20f5640`.

### Registration facts

- **Stateless.** The factory `new(0x150)` allocates a 336-byte pass object, writes the pass operation-name `"builtin.module"`, and zero-fills the rest plus the base `Pass`'s empty `SmallVector` descriptors. No `cl::opt`/flag is read. The object size and op-name are read off the disassembly of the factory, not from a header.
- `getArgument()` returns a `string_view` to the mnemonic literal — `"flatten-control-dep-tuple-operands"` (len `0x22`) for MHLO, `"stablehlo-flatten-control-dep-tuple-operands"` (len `0x2C`) for StableHLO. Both literals are present in the binary.
- The static registrar `_GLOBAL__sub_I_…` initializes the pass TypeID guard (`TypeIDResolver<hilo::NeuronControlDepTupleSimplifier>`) on first `createPass`.

---

## runOnOperation — walk + collect

### Algorithm

```c
void NeuronControlDepTupleSimplifier::runOnOperation():        // 0x20f6a40
    ModuleOp m   = this->getOperation();                       // [this+0x28] & ~7 (tag unmask)
    func::FuncOp fn = hilo::getMainFunction(m);                // 0x21c1e70 — first func op
    SmallVector<Operation*, 6> hits;                           // inline cap 6 (size-word seed 0x600000000)
    fn->walk< mhlo::CustomCallOp >( /*WalkOrder=PreOrder*/      // ForwardIterator
        lambda@0x20f59c0 );                                    // type-filtered callback
    replaceTuples(hits);                                       // 0x20f5d40
    // free(hits) iff spilled out of inline buffer
```

- **Scope is the main function only.** `getMainFunction` (`0x21c1e70`) walks the module body and returns the first `func::FuncOp` it finds — both simplifiers operate over *one* function, not every function in the module. *Anchors: `getMainFunction` @ `0x21c1e70`, whose walk lambda `0x21bf2c0` carries the `func::FuncOp` type filter.*
- The worklist is a `SmallVector<Operation*, 6>`; the walk is PreOrder over ops whose TypeID equals `mhlo::CustomCallOp` (StableHLO twin: `stablehlo::CustomCallOp`).

---

## Walk Filter Lambda — selective, idempotent pre-filter

### Purpose

Collect an op into the worklist **only** when it is a control-dep custom-call that still carries a tuple/GTE-shaped operand. A control-dep op whose operands are already flat is skipped, which is what makes the pass idempotent.

### Algorithm

```c
void walkFilter(Operation* op):                               // 0x20f59c0 (StableHLO twin 0x2131900)
    if (op.typeID != TypeID<mhlo::CustomCallOp>) return;       // stage 0: type filter
    if (!hilo::isControlDep(op))                  return;      // stage 1: control-dep gate (0x20f59fe)
    if ((signed char)op[+0x2E] >= 0)              return;      //   sign-bit = hasOperandStorage; no operands → skip
    n   = op[+0x44] >> 2;                          // numOperands
    ops = op[+0x48];                               // OpOperand[] base, stride 0x20, value at +0x18

    // stage 2: count tuple/GTE operands
    tupleish = 0;
    for (i = 0; i < n; i++):
        Operation* def = ops[i].value.getDefiningOp();
        if (def && def.typeID in { mhlo::TupleOp, mhlo::GetTupleElementOp })
            tupleish++;

    // FINAL: collect only if the op still has tuple/GTE structure to flatten
    if (op_has_tupleish_structure(op, tupleish))               // raw operand-count word vs walk state
        worklist.push_back(op);                                // 0x20f5c78
    // else: already flat → not collected (idempotent)
```

The gate is genuinely two-stage — `isControlDep` **and** "still has a tuple/GTE operand" — not a bare `if (isControlDep) push`. The precise arithmetic of the final comparison (raw `numOperands` field against the accumulated tuple/GTE count) is **[INFERRED]**; what is directly visible is the skip-when-nothing-to-flatten behaviour, which is also what `replaceTuples`'s own operand re-scan assumes.

> **GOTCHA —** because the worklist contains only ops that need flattening, the pass is safe to run twice and safe to run after the importer has *not* tuple-wrapped (e.g. `k=1` predecessor with no tuple). A reimplementation that collects every control-dep CC and unconditionally rebuilds would churn already-flat ops and could double-erase tuples in the GC phase.

---

## replaceTuples — the flatten rewrite

### Purpose

For each collected op, build an equivalent custom-call whose operands are the flattened predecessor values, splice the original's uses onto it, erase the original, then erase any tuple/GTE op that lost its last use.

### Algorithm

```c
void replaceTuples(SmallVectorImpl<Operation*>& worklist):    // 0x20f5d40
    SmallVector<Value, 8>       flat;        // flattened operands (inline cap 8)
    SmallVector<Operation*, 8>  deadTuples;  // tuple/GTE ops to GC (inline cap 8)

    for (Operation* S : worklist):
        MLIRContext* ctx = S->getContext();
        OpBuilder b(S);                       // insertion point = S
        flat.clear();

        // ---- 1. collect flattened operands ----
        if ((signed char)S[+0x2E] < 0):       // hasOperandStorage
            n = S[+0x44]; ops = S[+0x48];
            for (i = 0; i < n; i++):
                Value v        = ops[i].value;            // [ops + i*0x20 + 0x18]
                Operation* def = v.getDefiningOp();

                if (def is mhlo::TupleOp):                // 0x20f5e96 — INLINE tuple elements
                    if ((signed char)def[+0x2E] < 0):
                        for (j over def operands) flat.push_back(def.operand(j));
                    deadTuples.push_back(def);            // record for GC

                else if (def is mhlo::GetTupleElementOp): // 0x20f633a — UNWRAP GTE
                    Value src        = def.operand(0).value;
                    Operation* srcOp = src.getDefiningOp();
                    if (srcOp is mhlo::TupleOp):          // 0x20f639d — GTE(tuple(...))
                        for (j over srcOp operands) flat.push_back(srcOp.operand(j));
                    else:                                 // GTE(otherOp)
                        for (r in srcOp.results) flat.push_back(r);   // push ALL results

                else:                                     // plain operand
                    flat.push_back(v);

        // ---- 2. rebuild with FLAT operands ----
        Location  loc  = S->getLoc();
        TypeRange tys  = { S->getResultType(0) };          // single result type preserved
        StringAttr tgt = b.getStringAttr("AwsNeuronControlDep");
        NamedAttribute na = b.getNamedAttr("call_target_name", tgt);   // the ONLY attr re-stamped
        OperationName opn = RegisteredOperationName::lookup(TypeID<mhlo::CustomCallOp>, ctx);
        //  miss → report_fatal_error("Building op `mhlo.custom_call` but it isn't known…")
        OperationState st(loc, opn);
        mhlo::CustomCallOp::build(b, st, tys, ValueRange(flat), { na });
        Operation* newOp = b.create(st);

        // ---- 3. splice uses S→newOp, erase S ----
        replaceAllUsesWith(S, newOp);          // inline use-list relink (0x20f6130..)
        S->erase();
        flat.clear();

    // ---- 4. dead-tuple garbage-collect ----
    for (Operation* t : deadTuples):
        if (!any_result_has_use(t))            // use_list_head == null for all results
            t->erase();                        // erase ONLY when fully dead
```

### Key facts

- **Flatten direction (operand order preserved).** A `mhlo.tuple` operand is replaced in place by its element list; a `mhlo.get_tuple_element` operand is replaced by its source — by the source tuple's elements if the GTE feeds on a tuple, otherwise by *all* of the source op's results. A plain operand passes through unchanged. The loop appends in operand-index order, so `cc(tuple(p0,p1), D)` → `cc(p0, p1, D)`: predecessors first, data operand last, matching the HLO operand convention of 4.15. *Anchors: the three TypeID compares at `0x20f5e96` (tuple), `0x20f633a` (GTE), `0x20f639d` (GTE-of-tuple).*
- **GTE has two sub-cases.** `GTE(tuple(...))` inlines the tuple's elements; `GTE(otherOp)` pushes *every* result of the source op — richer than a naive "unwrap to operand(0)".
- **Rebuilt op carries exactly one attribute.** Only `call_target_name = "AwsNeuronControlDep"` is re-applied (NamedAttr count = 1). No `backend_config`, `api_version`, `opaque`, or `has_side_effect` is re-stamped — the builder default-constructs the rest.
- **Result type preserved.** The new op's `TypeRange` is the original's single result type — shape-transparent, matching the HLO `result = operand(0).shape()` convention.
- **Dead-tuple GC runs last.** Each recorded tuple/GTE op is erased only after all CCs are rebuilt, and only if all its results are now use-free — so a tuple still consumed elsewhere survives.
- **Robustness.** Both SmallVectors have `grow_pod` spill paths; the op-name registry lookup `report_fatal_error`s if `mhlo.custom_call` is unknown (i.e. the MHLO dialect must be loaded) — the `"Building op `"` prefix and the `"mhlo.custom_call"` mnemonic are both in the string pool.

> **GOTCHA —** the side-effect flag the HLO pass set on the original control-dep CC (4.15) is **not** explicitly re-asserted across this rebuild — only `call_target_name` is copied; S's attribute dictionary is not. Correctness still holds because the data operand still feeds the consumer and ordering is carried by the now-flat predecessor operand edges. Whether the flattened CC's `has_side_effect` after `mhlo::CustomCallOp::build` matters to the downstream Penguin scheduler is **[UNRESOLVED]** — the builder was not traced. See [Part 5](../penguin/dependency-model.md).

> **NOTE —** the disasm seeds the `getStringAttr` Twine with the literal `"AwsNeuronControlDep"` plus a length byte `3`. The `3` is the `Twine` kind tag (`CStringKind`/PtrAndLength packing), **not** a string length — the C-string is the full 19-char `"AwsNeuronControlDep"`, the same literal `isControlDep` compares against.

---

## isControlDep — the shared dual-dialect recognizer

### Purpose

The single function that recognizes a control-dep custom-call in either dialect. It is the one recognizer shared by both simplifiers' walk lambdas and all four Penguin printer call-sites.

### Algorithm

```c
bool isControlDep(Operation* op):                            // 0x21c0370
    if (!op) return false;
    TypeID t = op.typeID;
    StringRef tgt;
    if      (t == TypeID<mhlo::CustomCallOp>)      tgt = mhlo::CustomCallOp(op).getCallTargetName();
    else if (t == TypeID<stablehlo::CustomCallOp>) tgt = stablehlo::CustomCallOp(op).getCallTargetName();
    else return false;
    return std::string(tgt).compare(hilo::kAwsNeuronControlDep) == 0;   // "AwsNeuronControlDep"
```

- One function dispatches on **both** `mhlo::CustomCallOp` and `stablehlo::CustomCallOp`, each calling the dialect-correct `getCallTargetName()`. There is exactly one symbol, `_ZN4hilo12isControlDepEPN4mlir9OperationE` at `0x21c0370` — no per-dialect overload.
- `hilo::kAwsNeuronControlDep` is a file-static `std::string`; the comparison is `std::string::compare(const char*)`.
- Fan-out: this one recognizer is called by both walk lambdas (`0x20f59c0`, `0x2131900`) and by the `MhloToPythonPrinter` / `StableHLOToPythonPrinter` `printControlDeps` twins (`0x20b8480`, `0x2153360`).

---

## MHLO vs StableHLO — the divergence

The StableHLO twin is a structural clone of the MHLO pass. The algorithm, struct-access offsets, SmallVector capacities (6 for the worklist, 8/8 for `flat`/`deadTuples`), use-splice-then-erase, and dead-tuple GC are identical; the only differences are dialect TypeIDs, the op builder called, and three string literals.

| Element | MHLO `0x20f…` | StableHLO `0x213…` | Divergence |
|---|---|---|---|
| `runOnOperation` | `0x20f6a40` | `0x2132980` | walk filter TypeID: mhlo vs stablehlo CustomCallOp |
| walk filter lambda | `0x20f59c0` | `0x2131900` | mhlo vs stablehlo Tuple/GTE/CustomCall TypeIDs |
| `replaceTuples` | `0x20f5d40` | `0x2131c80` | dialect TypeIDs + builder below |
| → tuple-operand TypeID | `mhlo::TupleOp` | `stablehlo::TupleOp` | dialect TypeID |
| → GTE TypeID | `mhlo::GetTupleElementOp` | `stablehlo::GetTupleElementOp` | dialect TypeID |
| → rebuild builder | `mhlo::CustomCallOp::build` | `stablehlo::CustomCallOp::build` | dialect builder |
| → fatal op-name | `"mhlo.custom_call"` | `"stablehlo.custom_call"` | mnemonic |
| `getArgument` | `"flatten-control-dep-tuple-operands"` | `"stablehlo-flatten-control-dep-tuple-operands"` | pass mnemonic |
| factory | `0x20f68a0` | `0x21327e0` | symbol |

> **NOTE —** the `call_target_name` key and the `"AwsNeuronControlDep"` value are dialect-independent, and `isControlDep` (`0x21c0370`) is literally the same function for both. A module in either dialect form gets identical flatten semantics.

---

## Reconstructed Operation Fields

Offsets observed across the filter lambda and both `replaceTuples` (LLVM 17/18-era MLIR `Operation`). The access patterns are read straight off the disassembly; the ABI field *names* are reconstruction.

| Field | Offset | Use | Confidence |
|---|---|---|---|
| op result storage base | `+0x18` | builder/loc anchor; `[op-8]` = loc | CERTAIN (access) |
| result-count word | `+0x24` | read for result iteration | HIGH |
| flag byte | `+0x2E` | sign bit = `hasOperandStorage` (`jns ⇒ skip operand read`) | CERTAIN (access) / MEDIUM (name) |
| OperationName ptr | `+0x30` | `[+0x10]` = the op's TypeID | CERTAIN (access) |
| operand-count word | `+0x44` | filter reads `>>2` = numOperands; replaceTuples reads raw as loop bound | HIGH |
| `OpOperand[]` base | `+0x48` | stride `0x20`; `OpOperand.value` (`mlir::Value`) at `+0x18` | CERTAIN (access) |

> **GOTCHA —** `[op+0x44]` is read **shifted** (`>>2`, in the filter) in one place and **raw** as a loop bound in `replaceTuples`. Both iterate the operand array correctly given the `0x20` stride, but which low bits are trait flags is **[INFERRED]**. The flatten behaviour — inline tuples, unwrap GTEs, preserve order — does not depend on that packing detail.

---

## Verbatim String / Symbol Evidence

Every string below is present verbatim in `hlo2penguin`; every symbol address resolves in the function table.

```text
"flatten-control-dep-tuple-operands"            getArgument (MHLO)
"stablehlo-flatten-control-dep-tuple-operands"  getArgument (StableHLO)
"AwsNeuronControlDep"                            isControlDep + rebuild target
"call_target_name"                              NamedAttr key on rebuilt op
"mhlo.custom_call"                              fatal-error op-name (MHLO rebuild)
"stablehlo.custom_call"                         fatal-error op-name (StableHLO rebuild)
"Building op `"                                 report_fatal_error prefix
"builtin.module"                                OperationPass<ModuleOp> op-name
".add_dep_edge("                                Penguin emission (downstream, 4.15)
```

---

## Related Components

| Name | Relationship |
|---|---|
| [`PreserveControlDeps`](control-dep-reification.md) (4.15) | HLO-side pass that creates the flat `AwsNeuronControlDep` CC this pass re-flattens after import |
| `MhloToPythonPrinter::printControlDeps` (`0x20b8480`) | Consumer — reads the now-flat CC via `isControlDep`, emits `.add_dep_edge` |
| `StableHLOToPythonPrinter::printControlDeps` (`0x2153360`) | StableHLO consumer twin |
| MLIR XLA importer | Producer of the `mhlo.tuple`/GTE wrapping this pass undoes |

## Cross-References

- [Control-Dependency Reification](control-dep-reification.md) — Part 4.15, the HLO-side `PreserveControlDeps` pass that reifies control edges into the flat custom-call this pass restores after the MHLO/StableHLO round-trip.
- [hlo2penguin MLIR Pipeline Order & Entry Flow](hlo2penguin-mlir-pipeline.md) — where this flatten pass sits in the dialect pipeline.
- [Penguin Dependency Edges](../penguin/dependency-model.md) — Part 5, how the emitted `.add_dep_edge` becomes a BIR ordering constraint, and whether the side-effect flag still matters.
