# Schedule-Fusion & Fusion-to-Composite

> *All addresses on this page apply to `hlo2penguin` from `neuronx_cc 2.24.5133.0+58f8de22` (cp310). `.text` file-offset = VA − 0x201000; `.rodata` file-offset = VA − 0x200000. Other builds will differ.*

## Abstract

This page documents the two C-strand passes that close out the hlo2penguin fusion family. They are unrelated in mechanism but adjacent in the pipeline, and both write the same downstream artifact — a kind-tagged grouping op that the Penguin Python emitter later dispatches on.

`hilo::ScheduleFusion` (CLI `schedule-fusion`) is **not** a peephole rewriter. It is a collective co-scheduling pass: it finds an `mhlo.reduce_scatter` whose value-producer chain reaches back to an `mhlo.all_gather`, **physically reschedules** the AllGather and the intervening compute so the two collectives become adjacent (the comm-compute overlap idiom), then wraps exactly `{ReduceScatter, AllGather}` into one `mhlo.fusion` tagged `FusionKind="ScheduleFusion"`. The fusion op carries *no* backend config — it is a grouping marker; the optimization is the reschedule that already happened in the IR. This is the MLIR-level expression of the tensor-parallel / FSDP `all_gather → compute → reduce_scatter` overlap that the BIR back-half (Walrus scheduler / semaphore engine) later realizes.

`hilo::FusionToComposite` (CLI `fusion-to-composite`) is the post-hoc rewriter that lowers *every* `mhlo.fusion` (from any of the seven C-strand fusions, ScheduleFusion included) into a `stablehlo.composite` plus a private `func.func` decomposition. It copies the fusion's `FusionKind` into the Neuron-private `CompositeKind` inherent attr, fixes the composite's `name` to a constant `"composite.Fused"`, sets `version=0` and `composite.attributes={}`, clones the fusion body into the decomposition func (converting `stablehlo.return → func.return`), then erases the fusion. The fusion-name **registry** lives not in this pass but in its consumer: `StableHLOToPythonPrinter::print<stablehlo::CompositeOp>` reads `CompositeKind` and string-compares it against the seven kinds, dispatching to bespoke or arbitrary Python emitters.

For reimplementation, the contract is:

- **The ScheduleFusion match shape** — `mhlo.reduce_scatter` reaching back through movable compute to an `mhlo.all_gather`, with a four-collective dependence-walk boundary set.
- **The reschedule motion** — `Operation::moveAfter`/`moveBefore` gated by `isBeforeInBlock`, and *which* op moves where.
- **The FusionToComposite attribute schema** — the exact keys/values stamped on the composite and the decomposition func, and what is *not* stamped.
- **The CompositeKind dispatch registry** at the consumer, including the seven kinds and the fatal default.

| | |
|---|---|
| **ScheduleFusion entry** | `hilo::ScheduleFusion::runOnOperation` @ `0x210f760` (13 B) → tailcall worker |
| **ScheduleFusion worker** | `fuseAllGatherReduceScatter(func::FuncOp)` @ `0x210f370` (1005 B) |
| **ScheduleFusion matcher** | `{lambda(mhlo::ReduceScatterOp)#1}::operator()` @ `0x210d9f0` (6459 B) |
| **ScheduleFusion scope / arg** | `OperationPass<func::FuncOp>` (@`0x420760`); `"schedule-fusion"` (@`0x255d53`) |
| **FusionToComposite entry** | `hilo::FusionToComposite::runOnOperation` @ `0x2096d30` (216 B) |
| **FusionToComposite converter** | `convertFusionToComposite(func::FuncOp)::{lambda(FusionOp)#1}` @ `0x20957d0` (4977 B) |
| **FusionToComposite scope / arg** | `OperationPass<ModuleOp>` (vtable @`0x41dfe8`); `"fusion-to-composite"` (@`0x262839`) |
| **Composite kind dispatcher** | `StableHLOToPythonPrinter::print<stablehlo::CompositeOp>` @ `0x21947e0` |
| **IR level** | MHLO / StableHLO (hlo2penguin in-memory) |
| **The seven FusionKinds** | `{ScheduleFusion, MulRedSqrt, DotLogistic, Expm1, Log1p, Elementwise, DotSoftmax}` |

---

## ScheduleFusion — comm-compute overlap scheduling

### Purpose

`ScheduleFusion` is the seventh and last C-strand fusion. Unlike the dot/elementwise fusions (4.34/4.35), it does not fuse *compute* — it co-schedules two *collectives*. It recognizes the gather-then-(compute)-then-scatter idiom that arises in tensor-parallel and FSDP layers, where an `all_gather` result feeds (transitively, through movable compute) a `reduce_scatter`. By relocating the AllGather to sit adjacent to the ReduceScatter and legally re-ordering the data-dependent compute around them, the pass exposes the comm-compute overlap window that the backend scheduler exploits. The emitted `mhlo.fusion` is purely a grouping + kind marker; the win is the reschedule.

The complete C-strand kind set is enumerated verbatim by two diagnostic strings — the MHLO `FusionOp` validator (@`0x3a9bc0`) and the StableHLO `CompositeOp` validator (@`0x2e0258`):

```text
@0x3a9bc0: "Unexpected FusionOp type '%s'. Supported fusion types are:
            ScheduleFusion, MulRedSqrt, DotLogistic, Elementwise, Expm1, Log1p, DotSoftmax."
@0x2e0258: "Unexpected CompositeOp kind '%s'. Supported kinds are:
            ScheduleFusion, MulRedSqrt, DotLogistic, Expm1, Log1p, Elementwise, and DotSoftmax."
```

> **NOTE —** the two lists are the same seven names in a different order; `ScheduleFusion` heads both. Both strings are byte-confirmed at the cited `.rodata` offsets.

### Entry Point

```text
hilo::ScheduleFusion::runOnOperation  @0x210f760 (13 B)   ── thin: load FuncOp, tailcall
  └─ fuseAllGatherReduceScatter        @0x210f370 (1005 B) ── driver: walk → DominanceInfo → codeGen
       ├─ {lambda(mhlo::ReduceScatterOp)#1}::operator()  @0x210d9f0 (6459 B) ── match + reschedule + cluster
       └─ FusionCluster::codeGen(DomInfo, PostDom=NULL)  @0x2104640 ── shared region-builder (4.34)
```

`runOnOperation` (13 bytes) is a pure tail-call: `rsi = [pass+0x28] & ~7` (the `func::FuncOp` handle, masked to drop pass-state tag bits) → `jmp fuseAllGatherReduceScatter`. The demangled symbol name confirms the worker signature: `fuseAllGatherReduceScatter(mlir::func::FuncOp)`.

### Algorithm — the driver

```c
function fuseAllGatherReduceScatter(FuncOp func):     // 0x210f370
    SmallVector<unique_ptr<FusionCluster>, 100> clusters;  // inline cap = 100 (0x6400000000)
    mlir::walk(func.getOperation(),                        // 0x210f3ec, WalkOrder = PreOrder(1)
               matcher_lambda /*@0x210d9f0*/);             //   appends one cluster per matched RS-AG pair
    DominanceInfo dom = getAnalysis<mlir::DominanceInfo>();// 0x210f400; "mlir::DominanceInfo]" @0x318e46
    for (c : clusters)                                     // 0x210f4c8 forward loop
        c->codeGen(dom, /*PostDom=*/ NULL);                // 0x210f4d0 (xor edx,edx — PostDom NULL)
    for (c : reverse(clusters)) destroy(c);                // 0x210f500 dtor + buffer free
    // ~SmallVector @0x210f750
```

The driver is the same collect-then-execute spine as the other six fusions (4.34): the **matcher** does all the heavy lifting (match, reschedule, cluster build — 6459 bytes), the driver only collects clusters, runs Dominance analysis, and calls the shared `FusionCluster::codeGen` with `PostDom=NULL`.

> **QUIRK —** the SmallVector's inline capacity is **100** (`0x6400000000`), and the matcher self-bounds its dependence worklist against the same constant (`cmp rdx, 0x64` inside the lambda). With more than 100 candidate RS-AG pairs in one function the vector grows via `SmallVectorTemplateBase::grow`; the 100 is a heuristic inline budget, not a correctness limit.

### Algorithm — the matcher and the reschedule

The matcher lambda receives one `mhlo::ReduceScatterOp` per walk visit. The demangled name is `…fuseAllGatherReduceScatter(func::FuncOp)::{lambda(mlir::mhlo::ReduceScatterOp)}::operator()`.

```c
function matcher(ReduceScatterOp rs):                  // 0x210d9f0
    // (a) skip-already-fused guard                              (0x210da2e)
    parent = rs.getBlock()->getParentOp();             // Block::getParentOp @0x9ae3f00
    if parent.TypeID == TypeID<mhlo::FusionOp>:        // 0x210da43
        return WalkResult::advance;                    //   RS already wrapped → never re-fuse

    // (b) find the source AllGather among RS's producers          (0x210dbf6)
    AllGather foundAG = null;
    for (i : rs.operands):                             // OpOperand stride 0x20, Value @ +0x18
        def = rs.operand[i].getDefiningOp();           // Value::getDefiningOp @0x9b75f40
        if def && def.TypeID == TypeID<mhlo::AllGatherOp> && foundAG == null:  // 0x210dc3d
            foundAG = def;                             //   first AllGather wins (var_1E0)
        worklist.push(rs.operand[i]);                  //   deque<mlir::Value> var_110
    if foundAG == null: return;                        // no AG reachable → no cluster (0x210dd58)

    // (c) data-dependence BFS + collective-boundary classification (0x210dd50)
    while (v = worklist.pop()):
        d = v.getDefiningOp();
        if d.TypeID in { AllReduceOp,                  // @0x210ddcc  ── BOUNDARY collectives:
                         ReduceScatterOp,              // @0x210de31     cannot be hoisted past
                         AllToAllOp,                   // @0x210de39
                         AllGatherOp }:                // @0x210de41
            continue;                                  //   scheduling boundary — stop here
        else:
            hook d into ordered op-list;               // _M_hook/_M_unhook, dedup via DenseMap var_160
            order by Operation::isBeforeInBlock;       // @0x210de63
        // bound: cmp rdx, 0x64 (=100) @0x210df6a

    // (d) THE RESCHEDULE — comm-compute overlap motion             (0x210e0f5)
    if foundAG.parentOp isa mhlo::FusionOp: descend into that fusion's region;  // 0x210e109
    foundAG.moveAfter(rs);                             // Operation::moveAfter @0x210e1f9  ── key motion
    for (op : ordered list):                           // SSA-dominance repair (0x210e220)
        if op.isBeforeInBlock(anchor): op.moveBefore(...);  // @0x210e246
    for (use : foundAG.result.getUses()):              // 0x210e332, skip mhlo::ReturnOp users
        if dependent.isBeforeInBlock(...): dependent.moveAfter(...);  // push into deque var_1F0

    // (e) build the cluster                                        (0x210e648)
    if rs.result.TypeID != TypeID<mlir::TupleType>:    // 0x210e683
        Operation* ops[2] = { rs /*var_1D0*/, foundAG /*var_1E0*/ };  // count=2 @0x210e6db
    else:                                              // tuple RS (0x210e85d):
        ops = collect GetTupleElementOp users of rs, then foundAG;    // @0x210e888
    clusters.push(new FusionCluster(ArrayRef{ops},     // 0x210e702
                                    StringRef{"ScheduleFusion", 0x0E}));  // tag @0x25e519
```

The disassembly of this lambda confirms the motion verbatim: `Operation::moveAfter` (×4), `Operation::moveBefore` (×2), `isBeforeInBlock` (×6), the four boundary collectives (`AllGather`/`AllReduce`/`AllToAll`/`ReduceScatter`), `FusionCluster`, and the `"ScheduleFusion"` tag are all present in the symbol/string references.

> **GOTCHA —** the cluster contains **only the two collectives** (`{ReduceScatter, AllGather}`, plus the `GetTupleElement`s in the tuple case). The intervening compute ops are *rescheduled in place* around the collectives but are **not** moved inside the fusion region. A reimplementation that drags the compute into the fusion body changes the dependence graph and breaks the overlap intent.

> **QUIRK —** `AllGather.moveAfter(ReduceScatter)` looks backwards — it relocates the *producer* collective to sit after its (transitive) *consumer* collective. This is deliberate: it positions the two comm ops adjacent so the backend can co-issue and pipeline them. The `moveBefore` fix-up loop then repairs any SSA dominance the AG hop broke, gated by `isBeforeInBlock`. The shared `FusionCluster::codeGen` afterward runs `recursivelyMoveDependentOps` with `PostDom=NULL` for final dominance legalization.

### The four-collective boundary set

The dependence walk stops at any of `{AllGatherOp, ReduceScatterOp, AllReduceOp, AllToAllOp}`. The rationale: overlap may only be formed within *one* comm-compute window. A second collective on the producer chain is itself a scheduling barrier — hoisting compute across it would reorder communication that the program ordered intentionally. So the pass forms overlap for exactly one `all_gather → compute → reduce_scatter` span and never reaches across an intervening `all_reduce` or `all_to_all`.

### The emitted ScheduleFusion op (MHLO form)

```text
%r0, %r1, … = mhlo.fusion(%liveIn0, %liveIn1, …) ({
   ^bb0(%a0, %a1, …):
       %g  = mhlo.all_gather    %a_k { all_gather_dimension, channel_handle, replica_groups, … }
       %rs = mhlo.reduce_scatter %…  { scatter_dimension, channel_handle, replica_groups, computation }
       mhlo.return %rs (, %g if live-out)
}) { FusionKind = "ScheduleFusion" }
```

- **operands** = live-ins (cluster operands with an external producer, after the shared `updateInputs` inter-fusion remap; see 4.34).
- **results** = live-out value types (cluster results with an external consumer).
- **body** = the **moved** ops (MHLO moves, does not clone). Because the matcher already did `AG.moveAfter(RS)`, the body order reflects the reschedule.
- **attribute** = exactly one inherent `FusionKind` StringAttr `"ScheduleFusion"` (key `"FusionKind"` @`0x266931`). There is **no** `composite.attributes` / `backend_config` — the matcher passes the `FusionCluster` ctor only `(ArrayRef<Operation*>, StringRef)`, never an attribute dict (contrast DotLogistic/MulRedSqrt, which thread per-fusion params).

### Backend handoff

The only signal exported to the backend is the `FusionKind="ScheduleFusion"` grouping plus the now-adjacent body order. `MhloToPythonPrinter::print<mhlo::FusionOp>` reads `FusionKind` and routes `"ScheduleFusion"` to `printScheduleFusionOp` @`0x20f4ce0` (xref @`0x20f5261`), which walks the fusion body (skipping `mhlo.return`, `TypeID<mhlo::ReturnOp>` @`0x20f4da0`) and emits each inner op as an **ordinary Penguin op** in its rescheduled order via the shared `printOperation` @`0x20ee320`, then `defScalar`s the fusion results. Diagnostic `NCC_PYP052` (@`0x262919`) fires on a malformed body. The ScheduleFusion composite therefore prints **transparently** — there is no special Penguin op; the overlap is carried by collective adjacency, which the Penguin/Walrus scheduler later turns into actual semaphore-pipelined comm-compute overlap.

> **GOTCHA —** `hilo::AnalyzeSchedule` (CLI `live-range-analysis`, `runOnOperation` @`0x2071a80`, diagnostic `" Liverange ="` @`0x2830c1`) is a separate live-range analysis, not a second name for `ScheduleFusion`. It produces no comm-overlap fusion.

### MHLO vs StableHLO divergence

A StableHLO twin, `hilo::StableHLOScheduleFusion` (CLI `stablehlo-schedule-fusion` @`0x27af08`), implements the same idiom on `stablehlo` ops. Its `getTypeName` class string @`0x3a9a78` reads `hilo::StableHLOScheduleFusion`.

| Aspect | MHLO `ScheduleFusion` | StableHLO `StableHLOScheduleFusion` |
|---|---|---|
| Pass scope | `OperationPass<func::FuncOp>` (@`0x420760`) | `OperationPass<ModuleOp>` (@`0x422400`) |
| CLI arg | `"schedule-fusion"` (@`0x255d53`) | `"stablehlo-schedule-fusion"` (@`0x27af08`) |
| `runOnOperation` | @`0x210f760` (13 B) — tailcall | @`0x213e270` (62 B) — zeros `scheduleFusionCount`, walks `func::FuncOp` |
| Worker / matcher | @`0x210f370` / @`0x210d9f0` | @`0x213fea0` / @`0x213e2b0` |
| Matched op | `mhlo::ReduceScatterOp` | `stablehlo::ReduceScatterOp` |
| Emit | `mhlo.fusion` + `FusionKind`; body MOVED | `stablehlo.composite` (`CompositeKind`) + private `func.func @genComposite<N>`; body CLONED |
| Module state | none (stateless) | `scheduleFusionCount` @`0x9c70658` (version counter); `compParentMap` @`0x9c70620` (decompName map) |
| Terminal emitter | `printScheduleFusionOp` @`0x20f4ce0` | `printScheduleCompositeOp` @`0x2194490` |

The StableHLO worker resets `scheduleFusionCount = 0` per module run (`0x213e286`) — the counter is per-module composite numbering, monotone across all funcs. This is the same *MHLO-moves vs StableHLO-clones-into-`func.func`* seam documented for the rest of the family (4.34), specialized to the ScheduleFusion kind.

---

## FusionToComposite — wrapping a fusion as an MLIR composite

### Purpose

`FusionToComposite` is a standalone `ModuleOp` pass (`PassWrapper<hilo::FusionToComposite, OperationPass<ModuleOp>>`, vtable @`0x41dfe8`, typeinfo @`0x41dfd0`, class string @`0x2d4dc8`). It is distinct from the StableHLO-direct fusion path (4.34's `FusionCluster::stableHLOCodeGen`): rather than building composites *during* fusion, it post-processes a module that already contains `mhlo.fusion` ops (from any of the seven C-strand fusions) and lowers each to a `stablehlo.composite` + a private `func.func` decomposition. The two paths converge only on the shared `CompositeKind` dispatch attr and the same naming/cloning machinery.

### Entry Point

```text
hilo::FusionToComposite::runOnOperation  @0x2096d30 (216 B)  ── ModuleOp pass
  └─ {lambda(func::FuncOp)#1}            @0x2096ba0 (400 B)   ── per-func: skip genComposite, walk FusionOps
       └─ {lambda(mhlo::FusionOp)#2}     @0x2096b50 (73 B)    ── convert + erase
            └─ convertFusionToComposite::{lambda(FusionOp)#1}  @0x20957d0 (4977 B)  ── THE CONVERTER
```

### Algorithm — the driver

```c
function FusionToComposite_runOnOperation():           // 0x2096d30
    ModuleOp op = [this+0x28] & ~7;
    getOrLoadDialect<stablehlo::StablehloDialect>("stablehlo");  // 0x2096d82 — composite dialect must load
    this->[0x150] = 0;                                 // 0x2096da0 — decomposition-name counter reset
    mlir::walk(op, perFunc_lambda /*@0x2096ba0*/);     // walk all func::FuncOp

function perFunc(FuncOp func):                         // 0x2096ba0
    if func.attrs.contains("genComposite"):            // 0x2096be6 (key @0x256067)
        return WalkResult::skip;                        //   do NOT re-scan a decomposition func
    for (op : func.body):
        mlir::walk<FusionOp>(op, perFusion_lambda);    // 0x2096cab
    return WalkResult::advance;

function perFusion(FusionOp fusion):                   // 0x2096b50
    convertFusionToComposite(fusion);                  // 0x20957d0
    fusion.erase();                                    //   source op consumed
```

`[this+0x150]` is the **only** mutable pass state — a `uint` counter that names the decomposition funcs `hilo.FusedComposite.<N>`, reset to 0 per pass run.

> **GOTCHA —** the `genComposite` guard is the idempotency / recursion barrier. The decomposition funcs this pass emits carry `genComposite=true`; without the skip, a second sweep (or the walk re-entering them) would try to re-process their cloned bodies for nested fusions. The guard is a correctness requirement, not an optimization.

### Algorithm — the converter

```c
function convertFusionToComposite(FusionOp fusion):    // 0x20957d0
    ctx = fusion.loc.getContext();                                   // 0x2095804

    // (a) locate the enclosing ModuleOp by walking up the region tree  (0x2095817)
    blk = fusion.block;
    while (op = blk.getParentOp()): if op isa ModuleOp: module = op; break; blk = op.block;

    // (b) capture boundary types                                       (0x2095860)
    operandTypes = fusion.getOperands().getTypes();    // var_340
    resultTypes  = fusion.getResults().getTypes();     // var_320

    // (c) allocate decomposition name from the per-pass counter        (0x209592c)
    n = pass->[0x150]; pass->[0x150] = n + 1;          // post-increment
    name = "hilo.FusedComposite." + itoa10(n);         // prefix @0x211a6d (len 0x14); digits @0x40a580

    // (d) composite.attributes = EMPTY dict                            (0x2095b00)
    attrs = Builder(ctx).getDictionaryAttr( ArrayRef<NamedAttribute>{} );

    // (e) build the stablehlo.composite at the fusion's insertion point (0x2095b53)
    composite = OpBuilder(before fusion).create<stablehlo::CompositeOp>(
        /*resultTypes*/ resultTypes,
        /*operands*/    fusion.getOperands(),          // VERBATIM — no live-in remap
        /*name*/        "composite.Fused",             // str @0x24a537 (len 0x0F) — constant for ALL kinds
        /*composite_attributes*/ attrs,                // {} empty
        /*decomposition*/ "hilo.FusedComposite.<N>",   // symbol ref to func in (f)
        /*version*/     0u);                           // literal 0
        // CompositeOp::build @0x9146130 maps name→composite.name, attrs→composite.attributes, version→composite.version
        // build guard: report_fatal_error("Building op `stablehlo.composite` but it isn't known…" @0x2df9e8)

    // (f) build the private func.func decomposition                    (0x2095d96)
    funcTy = FunctionType::get(ctx, operandTypes, resultTypes);        // I/O matches the fusion
    fn = OpBuilder.create<func::FuncOp>("hilo.FusedComposite.<N>", funcTy, {}, {});  // build @0x997c780

    // (g) mark the decomposition func                                  (0x2095ed1)
    SymbolTable::setSymbolVisibility(fn, Private);     // visibility = 1
    fn.setInherentAttr("genComposite", BoolAttr(ctx,true));  // key @0x256067 — idempotency tag (driver §perFunc)
    fn.setInherentAttr("noDelete",     BoolAttr(ctx,true));  // key @0x27eef7 — symbol-DCE shield

    // (h) clone the fusion BODY into the func region                   (0x20960b3)
    IRMapping map;
    fusion.region.cloneInto(fn.region, map);           // CLONE (not move)
    term = fn.region.front().getTerminator();
    if term isa stablehlo::ReturnOp:
        OpBuilder.create<func::ReturnOp>(term.loc, term.getOperands());   // stablehlo.return → func.return
        term.erase();

    // (i) stamp CompositeKind — the Neuron dispatch tag                (0x2096383)
    fk = fusion.getInherentAttr("FusionKind");         // key @0x266931 (len 0x0A)
    if fk != null:
        composite.setInherentAttr("CompositeKind", fk);     // key @0x21a28c (len 0x0D) — COPY the kind
    else:
        composite.setInherentAttr("CompositeKind", StringAttr(ctx,"FusedComposite"));  // value @0x2830f0

    // (j) live-out rewire                                              (0x2096262)
    for (i : composite.numResults):
        fusion.result(i).replaceUsesWithIf(composite.result(i), pred);
        // pred @0x20950a0: keep a use IFF use-owner.block.getParent() != fusion.region (setnz)
        //                  — external consumers move to composite result; internal uses stay
    // fusion op itself is erased by the CALLER (driver §perFusion)
```

The converter's disassembly confirms every structural callee: `CompositeOp` (×6 references), `cloneInto` (×2), `FunctionType` (×4), `replaceUsesWithIf` (×2), `setInherentAttr` (×8 — covering `CompositeKind`/`genComposite`/`noDelete`), `setSymbolVisibility` (×2), and the `"composite.Fused"`/`"FusedComposite"` string constants.

> **QUIRK —** the composite's `composite.name` is the literal constant `"composite.Fused"` for **every** kind — it is *not* the fusion kind. The discriminating fusion-kind string lives in a separate inherent attr, `CompositeKind` (=`DotLogistic`/`MulRedSqrt`/`ScheduleFusion`/…). A reimplementation that reads the kind from `composite.name` will read `"composite.Fused"` for all seven kinds and dispatch nothing.

### Composite attribute schema

| Attribute | Key (addr) | Value | Source | Conf |
|---|---|---|---|---|
| `composite.name` | dialect-internal (key @`0x27713d`) | **`"composite.Fused"`** (@`0x24a537`, len 15) — constant for every kind | `CompositeOp::build` name arg | CERTAIN |
| `composite.attributes` | dialect-internal (key @`0x2122ce`) | **`{}`** empty DictionaryAttr | `Builder::getDictionaryAttr({})` (0x2095b4b) | CERTAIN |
| `composite.version` | dialect-internal (key @`0x20e096`) | **`0`** (uint) | `CompositeOp::build` version arg | CERTAIN |
| `decomposition` (symbol-ref) | — | **`@hilo.FusedComposite.<N>`** | counter `[pass+0x150]++` + prefix @`0x211a6d` | CERTAIN |
| `CompositeKind` (Neuron inherent) | @`0x21a28c` (len 13) | **`FusionOp.FusionKind`** value, else `"FusedComposite"` (@`0x2830f0`) | `setInherentAttr` (0x2096994 / 0x20969b8) | CERTAIN |

The decomposition `func.func @hilo.FusedComposite.<N>` carries: `sym_visibility = Private`; inherent `genComposite = true` (@`0x256067`); inherent `noDelete = true` (@`0x27eef7`); signature `(operand-types) -> (result-types)`; body = cloned fusion ops + `func.return`.

> **NOTE —** `composite.name`/`composite.attributes`/`composite.version` are the upstream StableHLO dialect's own attribute keys, written internally by `CompositeOp::build` from the positional `StringRef`/`DictionaryAttr`/`uint` args (the StableHLO ODS for `stablehlo.composite` is `name` / `composite_attributes` / `decomposition` / `version`). The converter never spells those keys itself — confirmed by xref direction: those three key strings are read by `xla::HloFunctionImporter` / `CallInliner` / dialect code, never written by `convertFusionToComposite`. The only strings the converter *touches* are `"composite.Fused"`, `"CompositeKind"`, `"genComposite"`, `"noDelete"`.

> **GOTCHA —** unlike the StableHLO-direct path (4.34), this MHLO→composite path marshals **no** per-fusion parameters: `composite.attributes` is empty and `version` is 0. The per-fusion attribute payload is a property of the direct `stableHLOCodeGen` path, where each `fuse*` lambda populates it before codegen. Those lambdas do not run here — FusionToComposite copies only the `FusionKind`→`CompositeKind` discriminator.

### The fusion-name registry (consumer dispatch)

There is **no name registry inside FusionToComposite** — it blindly copies `FusionKind`→`CompositeKind`. The actual registry is the consumer, `StableHLOToPythonPrinter::print<stablehlo::CompositeOp>` @`0x21947e0`, which reads `CompositeKind` (`getInherentAttr` @`0x2194823`, `.getValue()` @`0x2194a6d`) and `std::string::compare`s it against the seven names:

| `CompositeKind` value | addr / len | Python emitter | Notes |
|---|---|---|---|
| `ScheduleFusion` | `0x25e519` / 14 | `printScheduleCompositeOp` @`0x2194b5a` | from `fuseAllGatherReduceScatter` |
| `MulRedSqrt` | `0x2560e7` / 10 | `printMulRedSqrtCompositeOp` @`0x2194acf` | RMSNorm denominator |
| `DotLogistic` | `0x215ec2` / 11 | `printDotLogisticCompositeOp` @`0x2194c5a` | bespoke |
| `Expm1` | `0x2327b3` / 5 | `printArbitraryCompositeOp(op,"Elementwise")` | folds into Elementwise |
| `Log1p` | `0x21a2eb` / 5 | `printArbitraryCompositeOp(op,"Elementwise")` | folds into Elementwise |
| `Elementwise` | `0x2669e3` / 11 | `printArbitraryCompositeOp(op,"Elementwise")` | |
| `DotSoftmax` | `0x25e528` / 10 | `printArbitraryCompositeOp(op,"DotSoftmax")` | |
| *(default)* `"FusedComposite"` | `0x2830f0` / 14 | **unmatched → fatal** | see below |
| *(any unknown)* | — | `NCC_ISPP054` @ `StableHLOToPythonPrinter.cc:4049` | error path @`0x2194ed0` |

So the seven FusionKinds — `{ScheduleFusion, MulRedSqrt, DotLogistic, Expm1, Log1p, Elementwise, DotSoftmax}` — are re-confirmed at the consumer. `Expm1`/`Log1p`/`Elementwise` collapse onto one arbitrary emitter (kind `"Elementwise"`); `DotSoftmax` uses arbitrary with its own kind; `Schedule`/`MulRedSqrt`/`DotLogistic` have bespoke emitters.

> **GOTCHA —** a composite reaching the printer with `CompositeKind="FusedComposite"` (the FusionToComposite default, i.e. a fusion that lacked a `FusionKind`) matches none of the seven and **FATALs** with `NCC_ISPP054` (string @`0x256145`; file `hilo/MLIRPasses/Transforms/StableHLOToPythonPrinter.cc` @`0x3daaa8`). The `else` branch in converter step (i) is a defensive default that, on a well-formed fusion, never fires — every C-strand fusion stamps a `FusionKind`.

### One-way conversion

There is **no** `CompositeToFusion` inverse pass in hlo2penguin. The only other composite-consuming walker besides the printer is the upstream `stablehlo_ext::ChloRecomposeOpsPass` (`{lambda(CompositeOp)#2}` @`0x2eeed30`) — unrelated CHLO recomposition. `xla::StringToFusionKind` @`0x92f3590` parses XLA HLO fusion-kind enums (`kLoop`/`kInput`/…), not the Neuron name set. The FusionToComposite conversion is strictly forward.

---

## Related Components

| Name | Relationship |
|---|---|
| `FusionCluster::codeGen` / `stableHLOCodeGen` (4.34) | shared region-builder; ScheduleFusion calls `codeGen(dom, NULL)` identically to DotLogistic |
| Dot/elementwise fusions (4.35) | the other six C-strand `FusionKind`s that FusionToComposite also lowers |
| `MhloToPythonPrinter::print<mhlo::FusionOp>` | MHLO-side dispatcher routing `FusionKind` to per-kind Penguin emitters |
| `StableHLOToPythonPrinter::print<CompositeOp>` @`0x21947e0` | the actual fusion-name registry (`CompositeKind` 7-way dispatch) |
| `hilo::AnalyzeSchedule` (`live-range-analysis`) | a *separate* live-range analysis — not the ScheduleFusion producer |

## Cross-References

- [MulRedSqrt / RMSNorm Fusion & FusionCluster CodeGen](rmsnorm-fusion-cluster-codegen.md) — the shared `codeGen`/`stableHLOCodeGen` machinery and the MHLO-moves vs StableHLO-clones seam
- [Neuron Op-Fusion — Dot, Elementwise, Transcendental Families](op-fusion-dot-elementwise.md) — the other six C-strand `FusionKind`s and their per-fusion attribute payloads
- [Collective Combiners & Threshold Model](collective-combiners.md) — Part 4.5, the AllGather/ReduceScatter/AllReduce ops that ScheduleFusion co-schedules
- [Penguin IR & Middle-End](../penguin/) — Part 5, the Penguin/Walrus scheduler that realizes the comm-compute overlap from the ScheduleFusion grouping into BIR semaphore pipelining
