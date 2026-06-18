# 6.5.10 BirCodeGenLoop: the beta3 Penguin→BIR Driver

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 twins present). Subject binary: `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` — an **UNSTRIPPED** Cython module compiled from `BirCodeGenLoop.py`. Its generated base class lives in `…/generated/BirCodeGenLoopGen.cpython-310.so`. All evidence below is from this binary's IDA sidecars (816 decompiled bodies + disasm + `_strings.json` + `_xrefs.json`); cross-checks into `libBIR.so` for the BIR-side targets.*

## Abstract

`BirCodeGenLoop` is the **beta3 driver** that lowers Penguin tensoriser-IR directly to Backend IR (BIR). Its self-description is a verbatim `.rodata` banner:

```text
Copyright (C) 2024, Amazon.com. All Rights Reserved
BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level.
```

That one line fixes the contract. The **input** is a Penguin tensoriser-IR tree — a compilation unit (`cu`) of subgraph functions, each a tree of axes (loops), scope regions, while loops, instructions, and tile tensors, all built upstream by the trace-time `NeuronCodegen` (the `KernelBuilder.so` forward builder; see [neuroncodegen-forward-builder](neuroncodegen-forward-builder.md) and [§5.9 ir-mlir-bir-mapping](../penguin/ir-mlir-bir-mapping.md)). The **output** is a freshly-constructed `bir::Module` of L1 BIR instructions at the **TongaISAInst granularity** — one BIR `Instruction` per Trainium "Tonga" hardware ISA instruction (PE matmul, Act, Pool/DVE, DMA, sync/branch), the same node model the walrus allocator and scheduler consume (Part 7 BIR, e.g. [axis-loop-model](../penguin/axis-loop-model.md) and [dependency-model](../penguin/dependency-model.md)).

This is one of **two parallel front-ends** onto the same BIR node model. `BirCodeGenLoop` (beta3) **builds** a `bir::Module` top-down from the Penguin IR tree. The other, `TranslateNKIASTToBIR` (beta2, the C++ `libwalrus` BackendPass, documented in the beta2/klr Part 7 BIR material), **mutates** an already-built `bir::Module` in place, patching serialized-KLR NKI-kernel nodes into it. They are not a pipeline — they are sibling drivers that converge on the identical L1 Inst data model. This page is the beta3 driver: its orchestration, its loop/scope/while control lowering, its tensor→`MemoryLocationSet` binding, and its per-instruction finalizer.

The driver is intentionally **thin at the top and fat in the leaves**. `runOnModule` is a fan-out; `runOnFunction` is a delegation shell. The real work is split across a per-function prologue/epilogue hook pair, a depth-first `transform(self)` recursion that opens BIR basic blocks on a `curBlockStack`, and a universal `transformInstruction` finalizer that stamps every emitted inst with its loopnest, engine, predicate, loop-mode and dependency edges.

| | |
|---|---|
| **Component** | `BirCodeGenLoop` Cython class (beta3 Penguin→BIR driver) |
| **Binary** | `BirCodeGenLoop.cpython-310-…so` (UNSTRIPPED, DWARF + Cython tracebacks) |
| **Base class** | `BirCodeGenLoopGen` (`…/generated/…so`) — the auto-generated visitor dispatch table |
| **Banner** | `.rodata`: *"Generate Backend IR from tensoriser IR at the TongaISAInst level."* |
| **Input** | Penguin tensoriser-IR (`cu.subgraph_functions` → Function/Axis/ScopeRegion/While/Inst/Tile-tensor tree) |
| **Output** | `bir::Module` of L1 BIR Insts at TongaISAInst granularity (built from scratch) |
| **Parallel driver** | `TranslateNKIASTToBIR` (beta2/klr; mutates an existing Module) — *not* a pipeline stage |
| **Top entry** | `runOnModule(self, cls, cu)` @ `0x975f0` (`BirCodeGenLoop.py:4010`) |
| **Debug artifact** | optional `<fn>.TensorizerBIR.json` (`curModule.toJson` → `json.dumps`) |
| **Source anchor** | each method body carries the literal `"neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.py"` + a line |

> **NOTE — Line-number convention.** Every method body embeds a `_Pyx_TraceSetupAndCall` traceback literal: the `.py` path and a line number. These are the Cython *def-line* anchors cited throughout (e.g. `runOnModule` → `py4010`). A sibling found that `addr2line` on the DWARF returns the *body-first-line*, which can sit a few lines below the def-line. Where both are available the DWARF line is preferred; this page cites the traceback def-lines because they are the stable per-method anchors present in `.rodata`.

> **METHOD — How the call sequences were recovered.** The Hex-Rays decompiled bodies suffer "local variable allocation has failed" mangling, so the ordered attribute/method-call sequence for each method is recovered from the **disasm**: each `mov rsi, …__pyx_n_s_<id>` / `__pyx_kp_[us]_<lit>` operand names a Python identifier or string literal, and reading those **in program order** reconstructs the actual call chain. Every claim below is tagged CONFIRMED (directly in this binary's disasm/strings) / STRONG (multi-evidence) / INFERRED / SPECULATIVE.

---

## 1. The call tree at a glance

The driver is a depth-first walk of the Penguin function tree. The spine is the `BirCodeGenLoopGen` base visitor firing `stmt.transform(self)` per node; this class supplies the per-node-type overrides. CONFIRMED for every named method (disasm name-traces, §§3–6).

```text
runOnModule(self, cls, cu)                         [py4010]  thin FAN-OUT
  └─ for f in cu.subgraph_functions:                          (only attr read off cu)
        results.append( f.runOnFunction(...) )                (drive each subgraph fn)

  runOnFunction(self, cls, cu)                     [py4015]  per-fn SHELL
    ├─ self.run_with_exception_handling(...)                  (UPSTREAM body-walk visitor:
    │                                                          fires the hooks + transforms)
    ├─ BirCodeGen.estimate_instances → num_dynamic_instances  (instance count metric)
    └─ [opt] curModule.toJson → json.dumps(NumpyJsonEncoder)  (→ "<fn>.TensorizerBIR.json")

  fired BY run_with_exception_handling, per function:
    beforeStmtTransform(self, f)                   [py420]   FUNCTION PROLOGUE
      ├─ curModule.addFunction(f.name, attributes=…)          (CREATE bir::Function)
      ├─ fn.addBasicBlock(...) → "Block1"  → push curBlockStack(entry BB)
      ├─ ensureDstOnFunction(...)                             (IO/output-tensor wiring)
      ├─ build IdentityWeightTensor "identity_%s"             (PE identity-matmul operand)
      └─ lowerAwsNeuronExit(insts)                            (function epilogue insts)

    < for each top-level stmt: stmt.transform(self) — DFS over the body >
      transformAxis / transformDynamicAxis  → codegenLoop     (open loop body BB "block0")
      transformScopeRegion                  → codegenScopeRegion (degenerate 1-trip InstLoop)
      transformWhile                        → codegenWhile      (InstDoWhile)
      transform{,NeuronSB,NeuronPSUM,NeuronWeight}Tensor → createMemLoc (MemoryLocationSet)
      codegen<Op> (matmul/act/dma/…)        → transformInstruction (finalize the Inst)

    afterStmtTransform(self, f)                    [py3898]  FUNCTION EPILOGUE
      ├─ f.ordered_all_tensors  (tensor-set finalize)
      ├─ self.curBlockStack.pop()                             (close entry "Block1" BB)
      └─ export_new_nki_fe_metrics / print_new_nki_frontend_cache_statistics
```

The **`curBlockStack` discipline** is the spine of the whole walk: it is a LIFO of open BIR `BasicBlock`s, and *every* emitted instruction lands in `curBlockStack[-1]`. `beforeStmtTransform` pushes the function entry block `"Block1"`; each interior control node (`transformAxis`/`ScopeRegion`/`While`) pushes its body block and pops it after recursing; `afterStmtTransform` pops `"Block1"`. CONFIRMED across §§3–6 (`curBlockStack` + `append`/`pop` name loads present in every relevant body).

> **CORRECTION — codegenLoop does not walk the loop body.** An early framing attributed the body walk and per-statement dispatch to `codegenLoop`. That is wrong. `codegenLoop` (§4) only **materializes** the BIR loop axis + loop wrapper and **opens** the body block `"block0"`. The body walk lives in `transformAxis` (§4): `codegenLoop(axis)` → `for stmt in axis.stmts: stmt.transform(self)` → `curBlockStack.pop()`. Disasm name-trace of `transformAxis@0x62260`: `{axis, codegenLoop, stmts, transform, curBlockStack, self, axis, pop}`. CONFIRMED.

> **CORRECTION — the Stmt hooks are PER-FUNCTION, not per-statement.** Despite the names, `beforeStmtTransform(self, f)` and `afterStmtTransform(self, f)` take a *function* `f`, not a statement. `beforeStmtTransform` is the function **prologue** (creates the `bir::Function`, opens the entry BB, wires IO, installs the identity weight, lowers the exit); `afterStmtTransform` is the **epilogue** (finalize tensors, pop the entry BB, flush NKI-frontend metrics). The genuinely per-statement bookkeeping (dep-edges, engine/predicate stamping) is in `transformInstruction` (§6), per emitted *instruction*. CONFIRMED (arg names + name-traces, §5).

---

## 2. beta3 (this driver) vs beta2 (the klr path)

The single most important orientation fact: there are two BIR front-ends, and this is the one that builds the Module. The distinction is crisp and CONFIRMED on the beta3 side from this binary; the beta2 column cross-references the `TranslateNKIASTToBIR` BackendPass (STRONG, multi-strand).

| Axis | **`BirCodeGenLoop` (beta3)** | `TranslateNKIASTToBIR` (beta2, klr) |
|---|---|---|
| Form | Python/Cython (birpy binding) | C++ `BackendPass` (`libwalrus`) |
| Input IR | Penguin tensoriser-IR **tree** (Function/Axis/Inst) | an **existing** `bir::Module` with `InstNKIKLIRKernel` nodes |
| Front-end era | beta3 / Penguin (default path) | beta2 / klr (KLIR) path |
| Top entry | `runOnModule(self, cls, cu)` | `BackendPass::run()` virtual |
| Module handling | **BUILDS** `bir::Module` from scratch (`curModule.addFunction` per fn) | **MUTATES** an existing Module in place (walks its BBs) |
| Per-fn drive | `runOnFunction` → `run_with_exception_handling` visitor | loop over Functions × BasicBlocks, find the kernel nodes |
| Per-unit lowering | `stmt.transform` → `codegen<Op>` + `transformInstruction` | `lowerKernelInst`: deserialize klr → `KlirToBirCodegen::codegenStmt` |
| Loop structure | `transformAxis`/`codegenLoop` → `InstLoop`/`InstDynamicForLoop` + `BirAxis`/`BirDynamicForAxis` | loops already lowered in the klr AST; this pass re-emits a sub-function |
| Output shape | one `bir::Function` per subgraph fn, body filled inline | a separate NKI `bir::Function` + a call-site node replacing the kernel node |
| Dep edges | `transformInstruction` wires Flow/Output/Anti/Ordered + affine predicates | `KlirToBirCodegen` wires edges in the generated NKI function |
| Debug artifact | `<fn>.TensorizerBIR.json` | bir-json (`createFromJson`) |

**Convergence.** Both drivers terminate at the *same* BIR L1 Instruction data model — `Matmult`/`Activation`/`DMACopy`/… at the TongaISAInst granularity. beta3 constructs the whole Module top-down from the Penguin tree; beta2 patches NKI-kernel nodes into an already-built Module from serialized klr. They are true parallel front-ends, not stages of one pipeline. (STRONG.)

> **CONFIRMED — beta3 builds, it does not mutate.** The `curModule.addFunction` call lives in `beforeStmtTransform@0xd1080` (name loads `curModule`, `addFunction`, §5), and there is **no** `addFunction` anywhere in `runOnModule@0x975f0` or `runOnFunction@0xadb40` (their only structural attr loads are `subgraph_functions`/`runOnFunction` and `run_with_exception_handling`/`toJson` respectively). The Module is therefore constructed function-by-function during the prologue hook, never patched into an existing one — the defining beta3 property.

---

## 3. `runOnModule` and `runOnFunction` — the thin top

### `runOnModule` (`_319` @ `0x975f0`, `py4010`) — the module fan-out

Signature CONFIRMED from disasm argparse: `runOnModule(self, cls, cu)`, with `pyargnames = [cls, cu]`. `cls` is a classmethod-style class pass-through (the same shape as the rest of the pass-entry convention); `cu` is the compilation unit / Penguin module being lowered.

```c
// _319 runOnModule(self, cls, cu)                  BirCodeGenLoop.py:4010
// CONFIRMED name loads (program order): {cls, cu, subgraph_functions, runOnFunction}
PyObject *runOnModule(self, cls, cu) {
    results = PyList_New(0);
    for (f in cu.subgraph_functions) {              // the ONLY attr read off `cu`
        r = f.runOnFunction(**kwargs);              // drive each subgraph fn
        results.append(r);
    }
    return results;                                 // list of per-function results
}
```

`runOnModule` is a pure fan-out: enumerate `cu.subgraph_functions`, call `runOnFunction` on each, accumulate the results. CONFIRMED — the disasm contains exactly four interesting name loads (`cls`, `cu`, `subgraph_functions`, `runOnFunction`) and nothing else.

> **GOTCHA — no module/registry setup lives here.** One might expect the `bir::Module` creation, the constant section, and the internal-kernel registry build to sit in `runOnModule`. They do not. The `bir::Module` (`curModule`) is **base-class instance state** (created in the upstream driver, not Cython-compiled into this class) and is populated per-function by `beforeStmtTransform`. The `_INTERNAL_KERNEL_REGISTRY` is a **module-level global** built lazily by `_build_internal_kernel_registry` (mangled `BirCodeGenLoop_31_build_internal_kernel_registry` — no class infix; CONFIRMED in `_strings.json` alongside `get_internal_kernel_registry`), consulted by the internal-kernel codegens, *not* per-`runOnModule`. Its docstring (verbatim, CONFIRMED): *"Build the registry of all internal NKI kernels that can be traced to new NKI frontend."*

### `runOnFunction` (`_321` @ `0xadb40`, `py4015`) — the per-function shell

```c
// _321 runOnFunction(self, cls, cu)                BirCodeGenLoop.py:4015
// CONFIRMED name loads (program order):
//   {cls, cu, run_with_exception_handling, num_dynamic_instances,
//    dump_tensorizer_bir_json, curModule, toJson, json, dumps, value,
//    NumpyJsonEncoder, indent, __enter__, __exit__, dump, print_info}
PyObject *runOnFunction(self, cls, cu) {
    self.run_with_exception_handling(...);          // (1) THE actual per-fn lowering
    n = "BirCodeGen.estimate_instances";            // (2) instance estimate
    self.num_dynamic_instances = estimate_instances(...);
    if (self.dump_tensorizer_bir_json) {            // (3) optional BIR-JSON dump
        v = self.curModule.toJson();
        s = json.dumps(v, cls=NumpyJsonEncoder, indent=...);
        with open("<nc>:<lnc_id>.TensorizerBIR.json", "w") as ctx:   // "%02d" nc/lnc_id
            ctx.dump(s);
    }
    self.print_info(...);                           // (4) diagnostic
    return ...;                                      // lowered fn result
}
```

`runOnFunction` is a delegation shell. It (1) hands the real lowering to `run_with_exception_handling`, (2) estimates the dynamic-instance count, (3) optionally serializes the just-built `bir::Module` to a `"<fn>.TensorizerBIR.json"` debug artifact (the beta3 analog of beta2's bir-json), and (4) prints info.

> **CORRECTION — runOnFunction does NOT build the bir::Function.** The `bir::Function` creation, IO/argument setup, loop-nest walk, and basic-block construction are **not** inlined here — there is no `addFunction`, `addBasicBlock`, or `Argument` vocabulary anywhere in this body (CONFIRMED by the name-trace, which contains only the 16 names above). Those responsibilities are split: `bir::Function` creation → `beforeStmtTransform` (§5); IO wiring → `ensureDstOnFunction` (§5); loop-nest/BB walk → `transformAxis`/`codegenLoop` (§4). STRONG.

> **STRONG — `run_with_exception_handling` is the upstream body-walk visitor.** The identifier is a string in this binary but has **no method body here**, so it is inherited from the upstream driver base. There it wraps the visitor that fires `beforeStmtTransform` → per-stmt `stmt.transform(self)` → `afterStmtTransform` over the function body, with the `run_with_exception_handling` name supplying the error-recovery posture.

---

## 4. Loops, scopes, while — control structure → BIR

The four interior control nodes (static axis, dynamic axis, scope region, while) share one shape — **enter / recurse / exit** — and each delegates the BIR-node construction to a `codegen*` factory that opens a body block on `curBlockStack`. CONFIRMED for all four.

### The enter/recurse/exit pattern

```c
// transformAxis (_25 @ 0x62260, py636)             — STATIC loop axis
// CONFIRMED trace: {axis, codegenLoop, stmts, transform, curBlockStack, self, axis, pop}
void transformAxis(self, axis) {
    self.codegenLoop(axis);                 // build BIR loop + open body BB "block0"
    for (stmt in axis.stmts)                // recurse into the loop body
        stmt.transform(self);               //   double-dispatch back into self.<visitor>
    self.curBlockStack.pop();               // close the loop body BB
}
```

`transformDynamicAxis` (`_29` @ `0x5d7e0`, `py669`), `transformScopeRegion` (`_21` @ `0x60d90`, `py601`), and `transformWhile` (`_17` @ `0x12e940`, `py561`) are byte-for-byte the same shape, differing only in their factory call (`codegenLoop` with `dynamic=`, `codegenScopeRegion`, `codegenWhile`) and the body-list attribute (`stmts` vs `children`). The static-vs-dynamic distinction is **not** in the visitor — both axis visitors funnel into `codegenLoop`; the dispatch table simply keys one visitor entry per Penguin axis subclass. CONFIRMED.

### `codegenLoop` (`_27` @ `0x1daca0`, `py645`) — the loop-axis factory

This is the single choke-point where a Penguin axis becomes a BIR loop node. CONFIRMED name loads: `{curModule, functions, function, name_2, checkAxisRepeat, it, dynamic, BirDynamicForAxis, BirAxis, addAxis, curBlockStack, InstDynamicForLoop, InstLoop, setParallel, parallel, addLoop, instructions, getName, get_attr_default, setBasicBlock, addBasicBlock, basicBlocks, block0, append}`.

```c
// codegenLoop(self, axis, dynamic=False)           BirCodeGenLoop.py:645
PyObject *codegenLoop(self, axis, dynamic) {
    bir_fn = self.curModule.functions[axis.function.name];   // owning bir::Function
    bir_fn.checkAxisRepeat(axis.it);                         // guard: no dup iter-var
    axis.it = uniquify(axis.it);                             // rename induction var

    if (dynamic) {                                          // ── runtime trip count ──
        ax = BirDynamicForAxis(axis);                       //   = bir::DynamicForLoopAxis (E16)
    } else {                                                // ── static trip count ──
        ax = BirAxis(axis);                                 //   = bir::LoopAxis (E16)
    }
    bir_fn.addAxis(ax);

    blk = self.curBlockStack[-1];
    if (dynamic) {
        loop = InstDynamicForLoop(axis, body);              //   E16 IT106, hasRuntimeValue=1
    } else {
        loop = InstLoop(axis, body);                        //   E16 IT105, hasRuntimeValue=0
        loop.setParallel(axis.parallel);                    //   ⭐ the affine/sequential attr
    }
    blk.addLoop(loop);  bir_fn.addLoop(loop);               // register on BB + Function
    loop.instructions[loop.getName()] = …;
    if (axis.get_attr_default(...)) loop.setBasicBlock(...);
    loop.addBasicBlock(...);                                // create body BB "block0"
    self.curBlockStack[-1].append( loop.basicBlocks["block0"] );  // PUSH body BB
    return None;                                            // body is filled by the CALLER
}
```

> **The E16 axis bridge (CONFIRMED end-to-end).** A static axis lowers to `BirAxis` (= `bir::LoopAxis`, `InstLoop`/IT105) and carries `setParallel(axis.parallel)`: `parallel=True` ⇔ `AxisType.Affine` (reorderable/vectorizable), `parallel=False` ⇔ `AxisType.Sequential` (ordered, no-reorder). A *single* `LoopAxis` node carries this attribute — there is no separate "parallel axis" class. A dynamic axis lowers to `BirDynamicForAxis` (= `bir::DynamicForLoopAxis`, `InstDynamicForLoop`/IT106) with `hasRuntimeValue=1`, `lb`/`ub`/`stride` as runtime `QuasiAffineExpr`, and **no** `setParallel` (a runtime loop is inherently ordered). This is the consuming-side confirmation of the upstream `create_affine_axis_block` (NeuronCodegen produces the Penguin axis; `codegenLoop` lowers it to the E16 node). The body BB is uniformly named `"block0"`; the function entry BB is `"Block1"` (§5). CONFIRMED.

### `codegenScopeRegion` (`_23` @ `0x173390`, `py615`) — scope as a degenerate loop

```c
// codegenScopeRegion(self, scope_region)           BirCodeGenLoop.py:615
// CONFIRMED loads: {checkAxisRepeat, BirAxis, addLoop, InstLoop, setBasicBlock, no_reorder}
void codegenScopeRegion(self, scope_region) {
    it.checkAxisRepeat();
    bir_axis = BirAxis(it);  fn.addAxis(bir_axis);
    loop = InstLoop(...);    block.addLoop(loop);            // a ONE-TRIP InstLoop
    bb   = loop.addBasicBlock(...);
    loop.setBasicBlock(bb, no_reorder = scope_region.no_reorder);   // barrier rides the BB
    self.curBlockStack.append(bb);
}
```

> **QUIRK — BIR has no ScopeRegion instruction.** A Penguin `ScopeRegion` (the upstream `allocation_scope`/`if_scope`/`stage_scope`/`no_reorder` container) is modelled as a **degenerate single-iteration `InstLoop`** carrying a `no_reorder` `BirAxis`. There is no dedicated BIR region instruction; a scope is a trivial loop block whose basic-block records the reordering barrier (`no_reorder` read at `setBasicBlock`). The walrus allocator/scheduler then treat the enclosed BB as a lifetime/ordering region. CONFIRMED (`no_reorder` + `InstLoop` + `setBasicBlock` name loads).

### `codegenWhile` (`_19` @ `0x122c40`, `py575`) — while as `InstDoWhile`

```c
// codegenWhile(self, while_loop)                   BirCodeGenLoop.py:575
// CONFIRMED loads: {AsTrivialExprOrNone, LoadTensorToRegister, IntRuntimeValue,
//                   InstDoWhile, is_do_while, continue_condition, addLoop}
void codegenWhile(self, while_loop) {
    block = self.curBlockStack[-1];
    is_do = while_loop.is_do_while;
    cond  = AsTrivialExprOrNone(while_loop.continue_condition);  // try compile-time fold
    if (cond is None) {                                          // runtime guard:
        reg  = LoadTensorToRegister(continue_condition);         //   load guard → register
        cond = IntRuntimeValue(reg);                             //   wrap as runtime int
    }
    loop = InstDoWhile(cond, is_do_while=is_do, ...);            // ⭐ the BIR do-while node
    block.addLoop(loop);
    bb = loop.addBasicBlock(...);  self.curBlockStack.append(bb);
}
```

The Penguin `While` node *is* the do-while abstraction (the body variable is literally `do_while`). Its guard — defaulting to `AlwaysTruePredicate` upstream, replaced by the real condition via `set_continue_condition` — becomes the `InstDoWhile.continue_condition`. A trivial/constant guard folds inline; a runtime guard is loaded into a sequencer register (`LoadTensorToRegister`) and wrapped as `IntRuntimeValue`. The `is_do_while` flag selects test-after (do-while) vs test-before (while) semantics on the **same** `InstDoWhile` node. CONFIRMED.

### `axisTrans` (`_131` @ `0xb3b10`) — SPMD grid axis → `AxisListType`

A small mapper, *not* a loop transform: it counts a Penguin SPMD grid axis's program dimensions and returns a BIR `AxisListType` enum tag `{X, XY, XYZ, XYZW}` (1/2/3/4 grid axes). This encodes the m-grid/SPMD launch axes (`grid{w,x,y,z}`) into the partition/replica-axis lowering, distinct from the per-loop machinery above. STRONG (name loads `{X, XY, XYZ, XYZW, AxisListType}`; integer ordinals not pinned).

---

## 5. The function hooks — prologue and epilogue

### `beforeStmtTransform` (`_13` @ `0xd1080`, `py420`) — function prologue

The largest hook in the class. It is a `super()` override (carries the "super(): empty `__class__` cell" literal — it calls the `Gen`/upstream base, then adds the work below). Argument `f` is the **Penguin function** being lowered. CONFIRMED name loads include `{need_unroll, need_dce, curModule, addFunction, addBasicBlock, Block1, ensureDstOnFunction, ordered_all_tensors, IdentityWeightTensor, identity__s, lowerAwsNeuronExit}`.

```c
// beforeStmtTransform(self, f)                      BirCodeGenLoop.py:420
void beforeStmtTransform(self, f) {
    super().beforeStmtTransform(f);
    nu = f.attributes["need_unroll"];                   // DCE/unroll flags
    nd = f.attributes["need_dce"];                      // ("processing_function … f.attrs: {…}")

    self.curModule.addFunction(f.name, attributes=…);   // ⭐ CREATE the bir::Function
    fn = self.curModule.functions[f.name];
    fn.addBasicBlock("Block1");                         // the function ENTRY BB
    self.curBlockStack.append(fn.basicBlocks["Block1"]);// open it

    self.ensureDstOnFunction(...);                      // ⭐ IO/output-tensor wiring (dst)
    for (t in f.ordered_all_tensors)                    // normalize tensor shapes/dtypes
        prep(t.dtype, t.split_dim, t.rank, ...);

    // PE identity-matmul stationary operand "identity_%s":
    iw = IdentityWeightTensor(...);                     // guarded by NeuronPSUM/WeightTensor
    //   is_x4_dtype, target.statebuf_num_partitions, allocateId; module str "identity_matrices"

    self.lowerAwsNeuronExit(insts);                     // materialize AwsNeuronExit epilogue
}
```

`beforeStmtTransform` is the function **prologue**: create the `bir::Function`, open the entry block `"Block1"`, wire the IO/destination tensors via `ensureDstOnFunction`, normalize the tensor set, install the per-function identity weight, and lower the function exit.

> **NOTE — why a per-function identity weight.** The `IdentityWeightTensor "identity_%s"` (module string `identity_matrices`, CONFIRMED in `_strings.json`) is a stationary operand for the PE array. Copies and transposes that have no native engine are lowered as identity matmuls (multiply by I) on the systolic array, so each function needs one identity operand available before its body emits any such inst. Guarded by `NeuronPSUMTensor`/`NeuronWeightTensor` type checks, `is_x4_dtype`, and `target.statebuf_num_partitions`. CONFIRMED.

### `afterStmtTransform` (`_313` @ `0x5e970`, `py3898`) — function epilogue

```c
// afterStmtTransform(self, f)                       BirCodeGenLoop.py:3898
// CONFIRMED loads: {ordered_all_tensors, transform, curBlockStack, pop,
//                   export_new_nki_fe_metrics, print_new_nki_frontend_cache_st}
void afterStmtTransform(self, f) {
    finalize(f.ordered_all_tensors);                    // finalize the fn tensor set
    self.curBlockStack.pop();                           // close the entry "Block1" BB
    self.export_new_nki_fe_metrics(...);                // flush NKI-frontend cache/metrics
    self.print_new_nki_frontend_cache_statistics(...);  //   (ties to the kernel-registry path)
}
```

The epilogue: finalize the function's tensors, pop the entry BB (balancing the `beforeStmtTransform` push), and flush the new-NKI-frontend cache statistics — the latter the diagnostic side of the internal-kernel-registry tracing path. CONFIRMED.

---

## 6. Tensors → `MemoryLocationSet`, and the instruction finalizer

### Tile tensor binding: four shims → `createMemLoc`

The four per-class tensor transforms are **byte-identical thin shims**. Each is literally one line:

```c
transformNeuronSBTensor(self, t)     { return self.codegenMemoryLoc(t); }  // py3874
transformNeuronPSUMTensor(self, t)   { return self.codegenMemoryLoc(t); }  // py3879
transformTensor(self, t)             { return self.codegenMemoryLoc(t); }  // py3883
transformNeuronWeightTensor(self, t) { return self.codegenMemoryLoc(t); }  // py3887

codegenMemoryLoc(self, t)            { return self.createMemLoc(t, tilename=tilename); } // py3870
```

CONFIRMED (`codegenMemoryLoc@0x73850` loads exactly `{t, createMemLoc, name_2(=tilename kw), self, tilename}`). The per-memory-class behavior is **not** in the transform methods — it is entirely inside `createMemLoc` (`_299` @ `0x175150`, `py3713`), which re-derives the class from the tile `t` at runtime. The transform shims exist only as visitor entry points (one dispatch key per Penguin tile subclass), all funneling to one factory.

### `createMemLoc` — the tile → BIR memory factory

`createMemLoc` is a giant if/elif chain keyed on the Penguin tile's Python class and its kind predicates, computing (a) the BIR `TensorClass`, (b) the `MemoryType`/`MemoryKind`, and (c) the addressing geometry, then calling `addMemoryLocationSet` on the owning BIR Function. CONFIRMED name loads include `{NeuronPSUMTensor, NeuronSBTensor, NeuronWeightTensor, MemoryType, PSUM, SB, DRAM, MemoryKind, ExternalInput, ExternalInputParameter, Const, Internal, is_const, isExternalInput, tensor_memkind, psum_par_size_in_bytes, tensor_scope_parent, addMemoryLocationSet}`.

```c
// createMemLoc(self, t, tilename=…)                BirCodeGenLoop.py:3713 — the factory
PyObject *createMemLoc(self, t, tilename) {
    shape = t.generateTensorShape();                 // → BIR tensor_shape
    // ── MemoryType from tile class (or explicit override) ──
    if (t.tensor_memkind present)  mtype = t.tensor_memkind;   // ⭐ explicit override
    else if (isinstance(t, NeuronPSUMTensor)) mtype = MemoryType.PSUM;
    else if (isinstance(t, NeuronSBTensor))   mtype = MemoryType.SB;
    else                                       mtype = MemoryType.DRAM;
    // ── MemoryKind (= wire "kind" / TensorKind) from boundary predicates ──
    if      (t.isParameter && primary_inputs)  kind = ExternalInputParameter;  // =1
    else if (t.isExternalInput)                kind = ExternalInput;           // =0
    else if (t.isParameter && primary_outputs) kind = ExternalOutputParameter; // =4
    else if (t.isExternalOutput)               kind = ExternalOutput;          // =3
    else if (t.is_const)                       kind = Const;                   // =6
    else                                       kind = Internal;               // =7
    // ── geometry by memory class ──
    //   PSUM: allocated_bank_expr/block_expr + psum_par_size_in_bytes + bankId → bank/base; pack_id_hint
    //   SBUF: npartitions × partition_size_in_bytes + address_expr → addr/base partition
    //   DRAM: sizeinbytes / n_elts + vnc_addr_space (Local/Shared/Debug)
    obj = resolve_owning_function(t.parent /* or tensor_scope_parent While */);
    obj.addMemoryLocationSet(set);                   // insert into the BIR Function
    return set;
}
```

> **QUIRK — `createMemLoc` is the *sole* TensorClass→MemoryType binder.** There is no `TensorClass`→`MemoryType` classifier inside `libBIR`. The binding is done **here**, by reading the explicit `tensor_memkind` attribute (when present, it *overrides* the inferred type) or the Penguin tile class. The class names map 1:1 onto the BIR `TensorClass` roster (`NeuronSBTensor=10`, `NeuronPSUMTensor=11`, `NeuronWeightTensor=3`, `NeuronBlockTensor=5`, `NeuronLocalTensor=9`). Weight tensors (`TensorClass 3`) additionally run `setShape`/`setFormat`/`setTensorName` and land in the DRAM constant section (kind `Const`/`ExternalInputParameter`). CONFIRMED.

> **GOTCHA — tensors scoped inside a while loop.** `createMemLoc` checks `tensor_scope_parent` and `While`: a tensor defined inside a while-loop body has its parent chain resolve to the `While` region, so its lifetime is the loop region, not the function. The error sentinel *"Tensor parent needs to be function, basicblock with function parent or loopaxis, tensor parent:"* (CONFIRMED verbatim in `_strings.json`) fires if the parent chain resolves to none of those. CONFIRMED.

### `transformInstruction` (`_295` @ `0xd5c40`, `py3645`) — the universal finalizer

This is **not** a dispatcher to `codegen<Op>` — it is the per-instruction **finalizer** run for *every* emitted BIR inst. The `codegen<Op>` bodies build the Inst payload; this method wires it into the current block and stamps its scheduling/dependency metadata. CONFIRMED name loads: `{curBlockStack, addInstToBir, id, instructions, setLoopnest, loopnest, engine, setEngine, engineTrans, lnc_id, setPredicate, enumerate_predicates_in_codegen, setLoopMode, function, dep_edges_for_inst, src, kind, EdgeKind, FLOW, OUTPUT, ANTI, Flow, Output, Anti, Ordered, addUnrollDependency, idx_map, BirQuasiAffineExpr, BirAffinePredicate, set_can_read_uninit, has_attr, BranchInst, OptBarrier, NeuronInst}`.

```c
// transformInstruction(self, inst)                 BirCodeGenLoop.py:3645
void transformInstruction(self, inst) {
    blk = self.curBlockStack[-1];
    self.addInstToBir(inst, blk);                    // append to current BB; routes via
                                                     //   dispatch_codegen → "codegen"+inst.dispatch_type
    inst.id = next_id();  blk.instructions[inst.id] = inst;

    inst.setLoopnest(self.loopnest);                 // (1) which BIR loops enclose it
    inst.setEngine( self.engineTrans(inst.engine) ); // (2) Penguin engine → BIR EngineType
    inst.setPredicate( enumerate_predicates_in_codegen_order(...) );  // (3) region/affine guards
    inst.setLoopMode(...);

    // (4) ── DEPENDENCY-EDGE WIRING ──
    for (src in self.function.dep_edges_for_inst(inst)) {
        kind = src.kind;                             // map → EdgeKind:
        //   FLOW/"Flow" (RAW), OUTPUT/"Output" (WAW), ANTI/"Anti" (WAR), "Ordered"
        inst.addUnrollDependency(... idx_map ...);   // (cross-iteration unroll dep)
        guard = BirAffinePredicate( BirQuasiAffineExpr(...) );   // per-edge affine predicate
        inst.set_can_read_uninit( src.has_attr(...) );           // uninit-read allowance
    }
    // special-cased classes: BranchInst, OptBarrier, NeuronInst — tailored edge handling
}
```

`transformInstruction` binds, per instruction, **four scheduling axes**: (1) **loopnest** placement — which `InstLoop`/`InstDoWhile` blocks (from §4) the inst nests under; (2) **engine** — `engineTrans` maps the Penguin engine via `engine.value` → `birEngineType[…]` to the BIR engine enum, with `lnc_id` = the logical-neuron-core id; (3) **predicate** — `setPredicate` over `enumerate_predicates_in_codegen` realizes the region-guard predicates as `BirAffinePredicate`/`BirQuasiAffineExpr` on the inst; (4) **dependencies** — `dep_edges_for_inst` builds the BIR dependency graph with `EdgeKind` and `addUnrollDependency` for cross-iteration deps. CONFIRMED.

> **NOTE — four edge kinds, not three.** The dependency edges are `EdgeKind.{Flow (RAW), Output (WAW), Anti (WAR), Ordered}`. The disasm carries both the enum-member name loads (`FLOW`/`OUTPUT`/`ANTI` as `n_s_`) **and** the wire-string literals (`Flow`/`Output`/`Anti`/`Ordered` as `n_u_`). The `Ordered` kind — a non-data ordering edge — is present alongside the three classic data-hazard kinds. CONFIRMED (an earlier transform-side note listed only Flow/Anti/Output; the driver-side trace adds `Ordered`).

> **The `addInstToBir` → `dispatch_codegen` routing.** `addInstToBir` (`_265` @ `0x63f30`) appends the inst to the BB and itself calls `dispatch_codegen` (`_263` @ `0x745d0`), which builds the method name `"codegen" + inst.dispatch_type` (CONFIRMED loads `n_u_codegen` + `n_s_dispatch_type`) and `getattr`-dispatches to `self.codegen<Op>`. This is the name-based op→codegen routing that the per-instruction codegens of §6.5.11–6.5.14 (the `codegenMatMulOp`/`codegenActivation`/`codegenNdDMAAP`/… roster) implement. CONFIRMED.

### The TongaISAInst granularity

The banner's "at the TongaISAInst level" fixes the emission granularity: **one BIR `Instruction` per Tonga ISA instruction** — the hardware-instruction granularity of the Trainium "Tonga" ISA (PE matmul, Act, Pool/DVE, DMA, sync/branch) — not a coarser op nor a finer micro-op. Each Penguin tensoriser-IR op (`MatMulOp`, `ActivationOp`, `DMACopy`, …) maps to a small fixed set of BIR L1 Insts (`Matmult`/`Activation`/`DMACopy`/…). The `codegen<Op>` bodies do the op→Inst mapping; `transformInstruction` does the per-ISA-inst finalization; `transformAxis`/`codegenLoop`/`codegenScopeRegion`/`codegenWhile` wrap them in the loop-nest/BB structure. CONFIRMED (banner + §6 structure).

---

## 7. Confidence, corrections, gaps

**CONFIRMED** (symbol / string / disasm of this binary): all driver methods at the cited pyx indices / VAs / def-lines (`runOnModule@0x975f0`, `runOnFunction@0xadb40`, `transformAxis@0x62260`, `codegenLoop@0x1daca0`, `beforeStmtTransform@0xd1080`, `afterStmtTransform@0x5e970`, `transformInstruction@0xd5c40`, `codegenScopeRegion@0x173390`, `codegenWhile@0x122c40`, `createMemLoc@0x175150`); the `runOnModule` `subgraph_functions`→`runOnFunction` fan-out; `runOnFunction`'s `run_with_exception_handling` + `estimate_instances` + `TensorizerBIR.json` dump; the prologue hook's `addFunction`/`Block1`/`ensureDstOnFunction`/`IdentityWeightTensor`/`lowerAwsNeuronExit`/`need_unroll`/`need_dce`; the epilogue's `pop` + NKI-fe metrics; `codegenLoop`'s `BirAxis`/`BirDynamicForAxis` + `InstLoop`/`InstDynamicForLoop` + `setParallel` + `block0`; `codegenScopeRegion`'s degenerate `InstLoop` + `no_reorder`; `codegenWhile`'s `InstDoWhile`/`AsTrivialExprOrNone`/`LoadTensorToRegister`/`IntRuntimeValue`; `transformInstruction`'s `addInstToBir` + loopnest/engine/predicate/loopmode + `dep_edges_for_inst`→`EdgeKind{Flow/Output/Anti/Ordered}` + `BirAffinePredicate`/`BirQuasiAffineExpr` + `set_can_read_uninit`; the banner; the `createMemLoc` class→`MemoryType`/`MemoryKind` dispatch; the registry symbols.

**STRONG:** `run_with_exception_handling` = the upstream body-walk visitor (string present, body inherited); `curModule` created in the base and populated by `beforeStmtTransform`; the §2 beta3-vs-beta2 table.

**INFERRED:** exact intra-body branch order where the mangled C obscured it (resolved via disasm name-order where possible); `createMemLoc`'s per-branch geometry arithmetic is read at attribute-name granularity, not arithmetic-pinned (the addr/bank/base *fields* it feeds are certain via the BIR struct, Part 7 BIR).

**Open gaps.** The C++ ctors behind `addMemoryLocationSet`/`addLoop`/`addAxis`/`addBasicBlock` are `PyObject` calls into `libBIR`, not byte-pinned here (semantics fixed by the BIR-side pages). The integer `AxisListType` ordinals for `{X,XY,XYZ,XYZW}` are read by name only (dim-count semantics STRONG). The exact `Inst`-subclass → `codegen<Op>` dispatch table is the `BirCodeGenLoopGen` base visitor; it is enumerated by the `codegen*` roster in this binary but the per-IT integer map is the subject of §6.5.11–6.5.14.

---

*Cross-references: [§5.9 ir-mlir-bir-mapping](../penguin/ir-mlir-bir-mapping.md) (where this lowering sits in the pipeline); §6.5.9 NKI codegen two-class architecture and [neuroncodegen-forward-builder](neuroncodegen-forward-builder.md) (the `NeuronCodegen` that builds the Penguin IR this driver consumes); §6.5.11–6.5.14 (the per-inst `codegen<Op>` bodies this driver dispatches to); Part 7 BIR — [axis-loop-model](../penguin/axis-loop-model.md), [dependency-model](../penguin/dependency-model.md), [dynamic-for-loop](../penguin/dynamic-for-loop.md) (the BIR target: `MemoryLocationSet`, `LoopAxis`, `InstDoWhile`, the L1 Inst model).*
