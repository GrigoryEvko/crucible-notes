# bir_linker — Multi-LNC Re-Assembly (N Per-Core Modules → One Module)

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 share the `.text` logic). The pass lives in `neuronxcc/starfish/lib/libwalrus.so`; the BIR primitives it calls (`Module::addFunction`, `MemoryLocationSet::createFromJson`, `Instruction::fullClone`, `Module::addQueue`) are externs resolved through `.plt` thunks into `libBIR.so`. The ELF is stripped of `.symtab` but **retains the full `.dynsym`**, so every `BirLinker::` method name maps to an address via `nm -DC libwalrus.so`. For `.text` (base `0x62d660`) and `.rodata` (base `0x1c72000`) the virtual address equals the file offset; `.data` carries a `+0x400000` delta (not used here). Method bodies live high — `0x15c6xxx..0x15fbxxx` — and the call targets in their disasm resolve through `.plt` thunks at `0x5e9020..0x62d650`; cite the high body, never the thunk. Other wheels differ — treat every address as version-pinned.*

## Abstract

`lnc_splitter` ([8.30](lnc-splitter.md)) takes one symbolic program and produces `lnc_size` concrete per-core `bir::Module`s, one per `NeuronCoreId`. Each of those modules is then compiled **independently** through the entire main pre-codegen pipeline — allocated, scheduled, codegen-prepped — and the cross-core sharing passes (`extend_shared_lifetimes`, `coloring_allocator_dram_shared`, `sync_shared_allocations`) copy each shared buffer's physical address from the producing core onto the consuming core's *remote view*. By the time control reaches the low-level backend builder, the compiler is holding a `vector<unique_ptr<bir::Module>>` of `N` fully-finished, mutually-addressed programs. **`bir_linker` is the inverse of `lnc_splitter`: it merges those `N` modules back into one.**

The merge model is the headline, and it is not what the name "linker" suggests. `BirLinker` does **not** keep a vector and patch cross-references in place. It **constructs a fresh `bir::Module`** — the `"<name>/sgLnk"` linked module — and **copies every per-core module into it as a separate `bir::Function`**. It then builds one synthetic **call-dispatcher basic block** at the merged module's top level whose body is a sequence of `InstCall` instructions named `call_def_<coreId>_instance_<idx>` — exactly one call per core function. The linked program is therefore a *single* module shaped as a call graph: a caller block that dispatches to `N` callee functions, one per core, each callee being a clone of that core's finished body.

Everything else `bir_linker` does is the bookkeeping that makes that single module internally consistent. Cross-core symbol references collapse to one unified memory location per shared buffer (an `inpMap`/`outMap` name translation riding on the `remote_local_target` identity that `sync_shared_allocations` already established at the *address* level); producer-out↔consumer-in handoffs become BIR **alias** edges; the `N` cores' separate **DMA-queue and semaphore pools merge into one namespace**; **`CoreBarrier` IDs are globally renumbered** so two cores can never collide on a barrier ID; and the per-core on-disk artifacts (weight `.npy`, debug info, custom-op blobs) are symlinked into one `sgLnk` link directory with a unified tensor-map for the NEFF packager.

It runs as the **first** low-level pass (L1 of the low-level builder `sub_805870`), precisely because the per-core work above it is done and the whole-program work below it (post-link register allocation, `vnc_link`, codegen, the NEFF packager) must see exactly one merged module. `bir_linker` is the hinge of the **split ↔ link ↔ allocate ↔ NEFF** arc.

For reimplementation, the contract is:

- The **new-merged-Module model**: a freshly-allocated `bir::Module` of size `0x310` seeded from the def table, into which each core's module is *cloned* as a `bir::Function` — not an in-place edit of any input module.
- The **call-dispatcher BB**: a top-level block of `InstCall call_def_<core>_instance_<i>`, one per core, built by `addFnCall`, with each per-core function returning to it and emitting a completion barrier.
- **Barrier / queue / semaphore unification**: a single monotonic `CoreBarrier`-ID renumber over the merged module, and a folded DMA-queue + semaphore pool spanning all cores.
- The **`sgLnk` layout**: the `<name>/sgLnk` link directory of symlinked per-core artifacts plus the unified `tensor-map.json` — the I/O-binding surface the NEFF/kelf packager (Part 12, planned `formats/` — NEFF / `__kelf` / `sgLnk` packaging) consumes.

| | |
|---|---|
| **Link entry (multi-core)** | `BirLinker::run(vector<unique_ptr<bir::Module>>&)` @ `0x15f5070` |
| **Single-module wrapper** | `BirLinker::run(bir::Module&)` @ `0x15c6ed0` (lnc==1 path, reuses merge machinery) |
| **Feasibility probe** | `BirLinker::dry_run(vector<…>&, logging::Logger&)` @ `0x15edd50` (no mutate) |
| **Structural merge driver** | `BirLinker::runCallGraph(vector<…>&)` @ `0x15f21c0` |
| **Seed merged Module** | `BirLinker::initCallGraphModule(vector<…>&)` @ `0x15d3cd0` |
| **Clone core → Function** | `BirLinker::copyModulesAsFunctions(vector<…>&)` @ `0x15f1da0` |
| **Per-BB instr clone+remap** | `BirLinker::copyInstrs2BB(Function,Function&,…)` @ `0x15f07a0` |
| **Per-func alloc + remote resolve** | `BirLinker::copyAllocs2Func(Function,DefInfo&,…)` @ `0x15e5500` |
| **Per-inst alloc resolve (inp/outMap)** | `BirLinker::copyAllocs2Inst(Module,name,…)` @ `0x15ce1a0` |
| **Insert dispatcher call** | `BirLinker::addFnCall(BasicBlock&,int,int)` @ `0x15d95e0` |
| **Unify DMA queue + semaphore pool** | `BirLinker::initQueues(Function*,vector<…>&)` @ `0x15cadc0` |
| **Unify memlocs / aliases** | `buildMainMemlocs` @ `0x15d0ad0` · `buildMainAliases` @ `0x15cd370` |
| **Demote cross-core outputs** | `BirLinker::fixOutputKind()` @ `0x15eb680` |
| **Global barrier renumber** | `backend::renumberCoreBarriers(Module&)` @ `0x1089250` (free fn) |
| **Link-dir orchestration** | `BirLinker::setupLinkDir(vector<…>&)` @ `0x15d8590` |
| **Derive `"<name>/sgLnk"`** | `BirLinker::getNcLnkModuleName(string)` @ `0x15ed9e0` |
| **Pass factory / registrar** | `make_unique<BirLinker>(PassOptions const&)` @ `0x15f89e0`; registrar block @ `0x3e04d40` |
| **Merged Module size** | `0x310` bytes (784) — `_Znwm(0x310)` in `run`, matches `lnc_splitter`'s `tc_new(784)` |

The class exports **45 `T` methods** plus the weak destructor pair `~BirLinker()` @ `0x15faed0` / `0x15fb280`; all names are recovered from `nm -DC libwalrus.so | rg 'BirLinker::'`. **[CONFIRMED — `.dynsym` name map + per-body disasm.]**

---

## 1. Where it sits — L1 of the low-level builder

`add_lowlevel_backend_passes` (`sub_805870`) registers `bir_linker` as its **first** entry (`L1`). The main pre-codegen pipeline that precedes it (`sub_80A6E0`) operates strictly **per core**: `lnc_splitter` (order 8) clones one module per `NeuronCoreId`, and every later main-pipeline pass — allocators, scheduler, `lower_local_collectives` (order 80), `extend_shared_lifetimes` (81), `coloring_allocator_dram_shared` (82), `sync_shared_allocations` (83) — runs once *per element* of the per-core `vector`. The split↔link boundary is a clean phase change:

```text
  ── main pipeline (sub_80A6E0) — PER CORE, on vector<Module> ──
  lnc_splitter (8)  clone → concretize → prune          (one Module per NeuronCoreId)
  …per-core alloc / schedule / codegen-prep…
  lower_local_collectives (80)  mint RemoteLocalTargets, insert CoreBarriers
  extend_shared_lifetimes (81)  cross-core barriers preserve shared lifetimes
  coloring_allocator_dram_shared (82)  color shared DRAM
  sync_shared_allocations (83)  copy producer's physical addr onto remote view  ← ADDRESS identity
  ── low-level builder (sub_805870) — WHOLE PROGRAM, on one merged Module ──
  L1   bir_linker            ← THIS PASS: merge N → 1                          ← NAME/ALIAS unification
  L12-17 infer_stream_ids / label_dma_qos / lower_dynamic_dma / lower_dma / …
  L19  expand_all_engine_pre_alloc_sema_and_reg
  L21-22 synchronizer / alloc_semaphores      (assigns IDs over §6's unified pool)
  L33  coloring_allocator_reg  (post-link REG allocator, runs on the MERGED module)
  L34-35 vnc_remote_addr_map / vnc_link
  L40  codegen      (per-engine emit → 64 B TPB bundles)
  L42  neff_packager (NeffPackager → NEFF; J-strand)
```

**Why L1.** The linker can do its job *cheaply* only because everything below it is already resolved. The per-core modules arrive fully scheduled with local addresses colored; crucially, `sync_shared_allocations` (83) has already followed each `RemoteLocalTarget` back to the producing core's now-allocated mloc and **copied the physical address** onto the remote view. So `bir_linker` never re-allocates — it needs only *name/alias* unification (§4–§5), *queue/semaphore/barrier* unification (§6–§7), and *call-graph* wiring (§3). Conversely the passes below it (DMA lowering, semaphore alloc, the post-link register allocator at L33, `vnc_link`, codegen) must run **once** on the single merged module: a register allocator that saw only one core's stream, or a codegen that emitted one NEFF per core, would be wrong. The merge has to happen exactly here. **[CONFIRMED via the `sub_805870` pass-order disasm and the §9 cross-refs to the allocator wiring.]**

`bir_linker` is registered as a **core-parallel** pass through `BackendPassManager::addCoreParallelPassWithName<BirLinker, bool&, string&>` (factory `_M_invoke` @ `0x858a70`, manager @ `0x81b340`); the instance is built by `make_unique<BirLinker>(PassOptions const&)` @ `0x15f89e0`, and the global registrar block at `0x3e04d40` (lambda `_M_invoke` @ `0x15f8f10`) sets the pass-name string. **[STRONG — registrar block + factory thunks present in `.dynsym`/disasm.]**

---

## 2. `run(vector<unique_ptr<Module>>&)` — the link entry (`0x15f5070`)

The disasm `0x15f5070..0x15f6770` performs the link as an ordered sequence (logging and string formatting elided):

```c
// BirLinker::run(vector<unique_ptr<bir::Module>>& modules) @ 0x15f5070  [CONFIRMED]
// returns the single merged Module.

// (1) name the linked module
string base = modules[0]->getName();                 // bir::Module::getName
string lnkName = getNcLnkModuleName(base);            // @0x15ed9e0 → appends "/sgLnk"
                                                       //   lea "/sgLnk" @0x15edbf1 → rodata @0x1c87d3d

// (2) diagnostics: "bir_linker cwd: …" (@0x1c87f45),
//     "DMA Descriptor ReUse Enabled" gated on global enableDmaDescReuse (@0x3dc57c8+0x78),
//     "Num Module Definitions" (@0x1c87f56), "Module Instance … DefID" (@0x1c87f74)

// (3) per-module-def prep
runModuleDefPasses(modules);                          // @0x15c9e80 — "runModuleDefPasses numModules .."

// (4) load Modular-flow metadata
json netlist = bir::loadJsonFile("hlo_netlist.json"); // rodata @0x1c820d5 "Modular flow metadata file"

// (5) build the module-definition table
size_t numDefs = initDefInfo();                       // @0x15dd850 → ModuleDefInfo, returns def count

// (6) *** NEW MERGED MODULE — fresh allocation, NOT an in-place edit ***
if (numDefs != modules.size()) { /* mismatch diagnostic */ }
bir::Module* main = (bir::Module*)_Znwm(0x310);       // mov edi,310h (bf 10 03 00 00) @0x15f5495;
                                                       //   _tc_new (tcmalloc operator-new alias)
bir::Module::Module(main, lnkName);                   // call @0x15f54a8 — construct in place
this->mainModule /* [this]+0x138 */ = main;           // mov [rax+138h] @0x15f54ad — store merged module

// (7) *** STRUCTURAL MERGE — clone each core in as a Function, build dispatcher ***
runCallGraph(modules);                                // @0x15f21c0  (§3)

// (8) *** unify DMA queue + semaphore pool into the linked Function ***
initQueues(mainFunc, modules);                        // @0x15cadc0  (§6)

// (9) per-core artifact assembly
for (def : defs) getSgDirNameForDef(def);             // @0x61bbb0 — "nc<id>/sg<id>" dir names
setupLinkDir(modules);                                // @0x15d8590  (§8)

// (10) reclassify cross-core intermediate outputs (don't export them as NEFF outs)
fixOutputKind();                                      // @0x15eb680  (§5d)

// (11) *** global CoreBarrier-ID renumber on the MERGED module ***
backend::renumberCoreBarriers(*main);                 // @0x1089250  (§7)

// (12) matmul utilization report
PostLinkMmtReport(*main, logger);                     // @plt 0x60c800

// (13) destroy the per-core modules; return `main`.
```

The **new-Module construction** is the structural pin of the whole pass: step (6) allocates a `0x310`-byte block (`mov edi,310h` / `bf 10 03 00 00` @ `0x15f5495`, then `_tc_new`, tcmalloc's `operator new` alias), runs the `Module(name)` constructor over it (`call @0x15f54a8`), and stores the pointer on the pass object at `[rax+138h]` (`@0x15f54ad`). Nothing in `run` re-uses an input module as the merged module — the inputs are *copied into* `main` (§3) and then destroyed in step (13). `0x310 == 784` is the same `sizeof(bir::Module)` the `lnc_splitter` page pins from its `tc_new(784)` / `operator delete[](…, 0x310u)` ([8.30](lnc-splitter.md)). **[CONFIRMED — `_Znwm(0x310)` + `Module::Module` call in the `run` body; size cross-checks the splitter.]**

`run(Module&)` @ `0x15c6ed0` is the **single-module thunk**: it wraps the one module in a 1-element sequence and tail-calls the same internal helpers (`0x15c6f50` / `0x15c7260`), so the **lnc==1** path reuses the merge machinery with no special-casing — a one-core program still gets a `sgLnk` module, a dispatcher with a single `call_def_0_instance_0`, and a (trivial) unified namespace. **[CONFIRMED — wrapper body tail-calls the shared helpers.]**

---

## 3. `runCallGraph` — the structural merge (`0x15f21c0`)

`runCallGraph` is the merge engine. Its disasm `0x15f21c0..0x15f30d0` issues this ordered chain:

```c
// BirLinker::runCallGraph(vector<unique_ptr<bir::Module>>& modules) @ 0x15f21c0  [CONFIRMED]

setupLinkDir(modules);                       // (a) artifact dir (§8)
initCallGraphModule(modules);                // (b) @0x15d3cd0 — seed merged Module +
                                             //     its top-level dispatcher Function & BasicBlock

copyModulesAsFunctions(modules);             // (c) @0x15f1da0 — for each per-core Module:
//   Module::addFunction(coreFuncName)                     @plt 0x624030
//   Function::cloneAttributesFrom(coreFunc)               @plt 0x5fdd10
//   copyAllocs2Func(coreFunc, ModuleDefInfo&, newFunc,    @0x15e5500  (§4a)
//                   mlocDM&, regDM&)                       //   resolves remote_local_target
//   copyInstrs2BB(coreFunc, newFunc, mlocDM, regDM,       @0x15f07a0  (§4b)
//                 instDM, i)                               //   fullClone every Instruction, remap
//   copyDeps(coreFunc, instDM)                            @0x15c95c0  // clone dep edges
//   copyTensorMapForFunc(funcName, mlocDM)                @0x15ca740

runFunctionPasses();                         // (d) @0x15d95c0 = fpassInsertReturns
                                             //                  then fpassInsertSwitchQueueInstances (§5a)
buildMainMemlocs();                          // (e) @0x15d0ad0 — unify MemoryLocationSets (§5b)
buildMainAliases();                          // (f) @0x15cd370 — producer-out ↔ consumer-in (§5c)
buildMainTensorMap();                        // (g) @0x15ee350 — unify tensor-map (NEFF I/O)

for (BasicBlock& bb : blocks()) {            // (h) walk every BB of the merged module
    Function* f = bb.getFunction();
    int coreId = f->getModule()->getNeuronCoreId();   // @plt 0x62b1e0
                                                       //   reads ModuleAttribute key 2 (NeuronCoreId,
                                                       //   set by LncSplitter::concretizeModule)
    addFnCall(dispatcherBB, coreId, idx);    //     @0x15d95e0  (§3.1) — one InstCall per core
}

moveModulesAsFunctions(modules);             // (i) @0x15e8cc0 — finalize funcArgs / uniqueId
attachWholeTensorInputAP(inst, mloc);        // (j) @plt 0x601ec0 — whole-tensor AP on linked inputs
backend::renumberCoreBarriers(*main);        // (k) @0x1089250  (§7)
PostLinkMmtReport(...);                      // (l)
```

**Merge model (CONFIRMED).** The linked program is **one `bir::Module`** =
`{ dispatcher Function (a top-level BB of `InstCall`) }` ∪ `{ one Function per NeuronCoreId, each = a clone of that core's module body }`.
There is **no flat instruction interleave** — the cores stay as separately-callable functions, and the dispatcher block calls them in order. This is the exact inverse of `lnc_splitter`'s clone-and-concretize: the splitter took one body and produced `N` per-core bodies by binding the shard symbol; the linker takes the `N` per-core bodies and folds them back under one caller, with the cross-core names re-unified.

### 3.1 `addFnCall(BasicBlock& dispatcher, int coreId, int idx)` — the dispatch (`0x15d95e0`)

The dispatcher block is built one `InstCall` at a time:

```c
// BirLinker::addFnCall(BasicBlock& dispatcher, int coreId, int idx) @ 0x15d95e0  [CONFIRMED]

// 1. bounds-check the per-core function table at 0x120/0x128(container) and load the callee
//    Function* at table[idx]: offset (idx + 0xf0f0f0f0f0f0f0f1)*8 + 0x50
//    — the NamedObjectContainer element arithmetic (the constant is -0xf0f0f0f0f0f0f0f * ... )
Function* callee = funcTable[idx];

// 2. build the instruction name from the two ints
string name = "call_def_" + to_string(coreId) + "_instance_" + to_string(idx);
//    rodata: "call_def_" @0x1c87a3a (lea @0x15d9677), "_instance_" @0x1c87a44 (lea @0x15d968b);
//    "TMC Case#1..#4b" @0x1c87a4f..0x1c87a91 — same naming family

// 3. insert a CALL into the dispatcher block and bind its callee
auto* call = NamedObjectContainer<BasicBlock,Instruction>
                 ::insertElement<InstCall>(iter, name);   // @plt 0x605d50
call->callee /* +0xf0 */ = callee;                         // write the target Function*
```

So the dispatcher block reads, in core order:

```text
dispatcher:
    call_def_0_instance_0     →  Function nc0   (core 0's body)
    call_def_1_instance_0     →  Function nc1   (core 1's body)
    …
    call_def_<N-1>_instance_0 →  Function nc<N-1>
```

Each `InstCall` invokes the corresponding core's function. The `instance_<idx>` token allows more than one instance of a module definition; the `"TMC Case#1"` / `sgDir` tokens are adjacent `.rodata` of the same naming family. **[CONFIRMED — `insertElement<InstCall>` call + the `call_def_`/`instance_` rodata; callee written at `+0xf0`.]**

> **The dispatch is static, not data-dependent.** The dispatcher block lists *all* cores' calls; runtime core selection is not a branch on a core-id register inside this block — each `InstCall` already carries the concrete callee, and the per-core function it targets was already concretized to that `NeuronCoreId` by `lnc_splitter`. The "dispatch to the right core" happens because the NEFF packager (J-strand) tags each callee function's instruction stream into the per-core NEFF section keyed by the `NeuronCoreId` the function carries, and the runtime fires each core's section on its own core. The dispatcher block is the *structural* manifest of "which functions exist and in what order," not a runtime switch. **[STRONG for the structural reading; the per-core NEFF section tagging is J33–J36 scope, NAMED only — INFERRED here.]**

---

## 4. Cross-core symbol / address resolution

This is the structural counterpart to `sync_shared_allocations` (D-H24). Two cooperating mechanisms turn `N` cores' *separate names* for a shared buffer into *one* merged memory location.

### 4a. Alloc resolution via `inpMap` / `outMap` (`copyAllocs2Inst` @ `0x15ce1a0`)

The `copyAllocs2Inst` body's `.rodata` (`0x1c8776a..0x1c8782c`) spells the algorithm verbatim:

```text
  "copyAllocs2Inst"  "!inpMap.empty()"  "!outMap.empty()"
  "Replacing from inpMap"  "Replacing from outMap"
  "Name collision for Input . in subgraph"
  "Name collision for ExtInput . in Module Instance subgraph"
  "Added new mloc . memMap[.] = ."  "CopyAllocs2Inst added . locs"
```

```c
// copyAllocs2Inst(Module, name, inpMap, outMap, memMap, mlocDM, …) @ 0x15ce1a0  [CONFIRMED]
// inpMap, outMap : unordered_map<string,string>  (subgraph-local name → unified main name)
// memMap         : unordered_map<string,string>  (record of newly-minted unified names)
// mlocDM         : llvm::DenseMap<MemoryLocation const*, MemoryLocation*>  (pointer remap)

for (string mlocName : inst.referencedMlocNames()) {
    if (isInput(mlocName)  && inpMap.count(mlocName))  mlocName = inpMap[mlocName];   // producer's out
    else if (isOutput(mlocName) && outMap.count(mlocName)) mlocName = outMap[mlocName];
    else {
        // unmapped → fresh main-Module mloc
        auto* set = MemoryLocationSet::createFromJson(...);          // @plt 0x623690
        auto* m   = mainFunc.getMemoryLocationByName(mlocName);      // @plt 0x5f9a90
        memMap[origName] = mlocName;                                  // "Added new mloc memMap[.] = ."
        mlocDM[oldMloc]  = m;                                         // for copyInstrs2BB pointer remap
    }
    // name collisions across subgraphs are detected and logged/diverted
}
```

So an **input** name on the consumer core that matches the producer core's **output** (carried in `inpMap`) is rewritten to the producer's unified name; symmetrically for outputs via `outMap`. Unmapped names get a fresh main-module mloc recorded in `memMap` and the parallel `DenseMap` (consumed by `copyInstrs2BB`, §4b, to remap the actual pointers in cloned instructions). **[CONFIRMED — full string set + the `createFromJson`/`getMemoryLocationByName` calls.]**

### The remote_local_target identity (`copyAllocs2Func` @ `0x15e5500`)

`copyAllocs2Func` / `copyAllocs` carry the *remote* resolution strings (`0x1c87bdf..0x1c87c17`):

```text
  "remote_local_target"
  "CopyAllocs resolved <core> collisions for subgraph"
  "copyAllocs2 . with mlocset name . maps to memset"  "Failed to tensormap"
  (@0x1c80618) "Output . has incorrect AllocedFunc: . instead of . SB Input .isLocal"
```

An mloc carrying a `RemoteLocalTarget` — an `optional<pair<remote_mloc_name, remote_ncId>>` minted by `lnc_splitter`'s bracketing collective-lowering passes (`LowerLocalCollectives::createRemoteAP`, D-H03/D-H23) and set via `bir::MemoryLocation::setRemoteLocalTarget` — is followed back to the producing core's allocation. The crucial fact: **`sync_shared_allocations` (order 83, *before* `bir_linker`) already copied the producing core's physical address/bank/partition onto the remote view**, so the "remote" name and the "local" name now denote the *same physical DRAM cell*. `copyAllocs2Inst` therefore safely collapses them to one main-module mloc (the `"maps to memset"` detection handles the no-real-backing case).

> **The division of labor (STRONG).** `bir_linker` does the **name-level** unification; `sync_shared_allocations` did the **address-level** identity. Together: every core's name for a shared buffer resolves to one mloc with one physical address. The `copyAllocs` vs `copyAllocs2` vs `copyAllocs2Func`/`copyAllocs2Inst` split is module-set passes vs per-function/per-instruction resolvers reached from `copyModulesAsFunctions` — the `inpMap/outMap → memMap` collapse is fully evidenced only in `copyAllocs2Inst` rodata. **[STRONG for the H24-pairing interpretation; the `RemoteLocalTarget` struct shape is cross-ref, not re-disassembled here — INFERRED.]**

### 4b. Pointer remap on instruction clone (`copyInstrs2BB` @ `0x15f07a0`)

`copyInstrs2BB` clones each instruction with `Instruction::fullClone(name, BB, b)` (`@plt 0x6293f0`), then rewrites all of its references through the DenseMaps:

```c
// copyInstrs2BB(Function src, Function& dst, mlocDM, regDM, instDM, i) @ 0x15f07a0  [CONFIRMED]
for (Instruction& old : src.instructions()) {
    Instruction* nw = old.fullClone(name, dstBB, b);                  // @plt 0x6293f0
    updatePhysicalAccessPattern(old.pap, nw->pap, mlocDM, regDM, b);  // @0x15f0610  (below)
    nw->setQueue(mainModule.getQueueByName(old.queueName));          // @plt 0x5fbad0 — unified pool (§6)
    nw->copyOpDebugInfo(old); nw->setTensorizerId(...);             // @plt 0x60f3b0
    io2Spill(nw, name, i);                                            // @0x15d3390 — dangling IO → spill
    instDM[&old] = nw;                                                // for copyDeps
}

// updatePhysicalAccessPattern(oldPAP, newPAP&, mlocDM, regDM, b) @ 0x15f0610  [CONFIRMED]
newPAP.setLocation(mlocDM[oldPAP.storageBase()], b, b);  // @plt 0x5e9cd0 — point at remapped StorageBase
newPAP.forEachDynamicExpr([&](QuasiAffineExpr& e){       // @plt 0x5f8aa0
    e.rewriteRegisterOperands(regDM);                     //   dynamic affine reg operands → regDM
});
```

The cloned instruction in the linked module therefore references the **unified** mlocs/regs/queues and the already-resolved physical addresses — so the downstream post-link allocator and codegen see one self-consistent program. `io2Spill` converts dangling IO arguments (a per-core input that is now an internal handoff) into spill slots on copy. **[CONFIRMED — `fullClone` + `setLocation` + `forEachDynamicExpr` + `getQueueByName` in the bodies.]**

---

## 5. Memloc / alias / output-kind unification

### 5a. `runFunctionPasses` (`0x15d95c0`) — make the call graph well-formed

`runFunctionPasses` calls two fixups, in order:

- **`fpassInsertReturns`** (`0x15d89a0`) appends at the end of each copied core function an `InstReturn` (`NamedObjectContainer::insertElement<InstReturn>` @plt `0x607560`, name `"return_…"`) **and** a completion `InstCoreBarrier` (`insertElement<InstCoreBarrier>` @plt `0x61bf40`, name `"cb_return_…"`; rodata @`0x1c87a28` `"cb_return_.I-SQI-.call_def_._instance_"`). Each per-core function thus **returns to the dispatcher** and emits a barrier that makes the merged call graph synchronizable.
- **`fpassInsertSwitchQueueInstances`** (`0x15d9080`) inserts `InstSwitchQueueInstance` (name `"I-SQI-…"`, rodata @`0x1c87a33`) at validated points (`BasicBlock::checkInsertionPointValid` @plt `0x603af0`) — switching the active DMA-queue instance per call site so the unified queue pool (§6) is driven correctly.

**[CONFIRMED — both `insertElement` specializations + the `cb_return_`/`I-SQI-` rodata.]**

### 5b. `buildMainMemlocs` (`0x15d0ad0`)

Builds the linked function's `MemoryLocationSet`s: `MemoryLocationSet::createFromJson` (@plt `0x623690`, from the netlist json), `FunctionArgumentMap::setMapping(set*, set*)` (@plt `0x5f83f0`, binding linked-function args to the per-core sets), `renameDebugTensor` (@plt `0x5fa8d0`), and `Function::getMemoryLocationSetByName` (@plt `0x613f00`). Rodata @`0x1c87876` (`"buildMainMemlocs . out[.ModuleDef[. instId . instOutName .insItr"`) shows it iterating `ModuleDef`s and per-instance outputs, creating the canonical main-module mloc per cross-core tensor. **[CONFIRMED.]**

### 5c. `buildMainAliases` (`0x15cd370`) — the cross-core symbol alias step

Rodata @`0x1c87720`: `"Aliases.No aliases defined.Alias: out . kin . outLocSet . inpLocSet .must.copyAllocs2Inst .!inpMap.empty().!outMap.empty()"`.

```c
// buildMainAliases() @ 0x15cd370  [CONFIRMED]
for (alias : definedAliases) {
    MemoryLocationSet* outSet = alias.producerOut;   // producer core's output set
    MemoryLocationSet* inpSet = alias.consumerIn;    // consumer core's input set
    AliasPtr a = bir::AliasPtr(outMloc, aliasKind);  // @plt 0x5f3660
    inpMloc->addAlias(a);                            // @plt 0x609620
}
```

For each defined alias it pairs the producer core's output `MemoryLocationSet` with the consumer core's input set, builds an `AliasPtr(MemoryLocation*, AliasKind)`, and calls `MemoryLocation::addAlias` — so the consumer's input name **aliases** the producer's allocated output. This is the BIR-level realization of `lnc_splitter`'s `RemoteLocalTarget`s: the cross-core data hand-off is expressed as an alias on one unified mloc. **[CONFIRMED — `AliasPtr`/`addAlias` calls + the alias rodata.]**

### 5d. `fixOutputKind` (`0x15eb680`) — don't export internal handoffs as NEFF outputs

Rodata @`0x1c87cf2`: `"Intermediate Output .Num remaining outs2Erase .sg(\d+).(nc[0-9]+)/sg[0-9]+./sgLnk"`. An output that was *external* on a per-core subgraph but is actually only consumed by another core is **demoted** from `ExternalOutput` to an internal/intermediate kind; outputs with no real reader are **erased**. `hasRealReaders` (`0x15eb560`) drives this via `ConvertReaderWriter<AccessPattern>` (@plt `0x601690`) + `AccessPattern::classof` to find non-DMA-copy readers. This stops cross-core intermediates from leaking out as NEFF outputs. The path tokens `(nc[0-9]+)/sg[0-9]+` confirm the per-core artifact layout `nc<coreId>/sg<subgraphId>`, linked into `sgLnk`. **[CONFIRMED.]**

### 5e. Tensor-map unification

`buildMainTensorMap` (`0x15ee350`) / `copyTensorMap` (`0x15d9b30`) / `verifyTensorMap` (`0x15dbf90`) / `tryUpdateTensorMapFile` (`0x15c91f0`) merge each per-core tensor-map (fields `layer_name` / `sim_format` / `sim_shape` / `tf_file` / `sbuf_compatible`, rodata @`0x1c801ee`) into one unified tensor-map, validate it, and write `tensor-map.json` into the link dir (rodata @`0x1c77380` `"Tensor map file.tensor-map"`). This is the I/O-binding table the NEFF packager (J-strand) and the runtime use to map host tensors to the linked program's inputs/outputs. **[CONFIRMED.]**

---

## 6. DMA queue + semaphore unification (`initQueues` @ `0x15cadc0`)

The `initQueues` rodata (`0x1c87696..0x1c876fa`) spells the merge:

```text
  "copying queue . from module ._defId_"
  "initQueues: adding new qInst . to existing"
  " to new dmaQ . getNumSemaphores"
  "initQueues copied . All …"
```

```c
// initQueues(Function* mainFunc, vector<unique_ptr<Module>>& modules) @ 0x15cadc0  [CONFIRMED]
for (Module& core : modules) {
    for (DMAQueue& q : core.queues()) {
        Queue* uq = mainModule.addQueue(q.name, q.type);   // call @0x15cb2f0 — "copying queue from module <defId>"
        for (QueueInstance& qi : q.instances()) {
            if (uq->has(qi))  uq->fold(qi);                 // "initQueues: adding new qInst . to existing"
            else              uq->addNew(qi);               // "to new dmaQ"
        }
        // semaphore accounting logged via " getNumSemaphores " (@0x1c876fa)
    }
}
// addBasicBlock @plt 0x600a40 materializes the queue-driver block(s)
```

Each per-core module's DMA queues merge into the linked module via `bir::Module::addQueue(name, DMAQueue::Type)` (the single `addQueue` call in the body, `@0x15cb2f0`); matching queue-instances fold into the existing unified queue when present, or create a new `dmaQ`; semaphore counts are accounted across the merge so the linked program has **one consistent queue/semaphore namespace** across cores. The downstream `alloc_semaphores` pass (L21–22) then assigns concrete semaphore IDs over this unified pool — which is exactly why the merge must precede it. The `"copying queue "` (@`0x1c87696`), `"initQueues: adding new qInst "` (@`0x1c876bb`), `" getNumSemaphores "` (@`0x1c876fa`), and `"initQueues copied "` (@`0x1c8770d`) strings are the in-body log tokens for these steps — `getNumSemaphores` appears as a log token, consistent with semaphore-count accounting rather than a standalone method call. **[CONFIRMED — single `addQueue` call @ `0x15cb2f0` + the four `initQueues` rodata strings, all referenced in-body.]**

---

## 7. Core-barrier ID renumbering (`renumberCoreBarriers(Module&)` @ `0x1089250`)

After merging `N` cores — each of which numbered its own barriers `0..k` *locally* — two cores would collide on barrier IDs. A single monotonic pass over the merged module fixes that. The function is a leaf loop with **no backend callees**:

```c
// neuronxcc::backend::renumberCoreBarriers(bir::Module& m) @ 0x1089250  [CONFIRMED]
uint32_t ctr = 0;                                  // running global ID (in %ebp)
for (Function* f = m.begin /*+0x0*/; f != m.end /*+0x8*/; f = f->next) {
    for (Instruction* in = f->bbHead /*-0x90*/; in; in = in->next /*0x8(node)*/) {
        if (*(uint8_t*)(in + 0x50) != 0x57) continue;     // cmp dword [rbx+50h], 57h @0x10892d7
                                                           //   opcode 0x57 == 87 == bir::InstCoreBarrier
        if (*(uint8_t*)(in + 0x130) != 0xff) {            // movzx [rbx+130h] @0x10892dd; 0xff = "not flagged"
            *(uint32_t*)(in + 0x110) = ctr++;             // mov [rbx+110h], ebp @0x10892ff — new global ID
            *(uint8_t*)(in + 0x130) = 0;                  // mov byte [rbx+130h], 0 @0x1089308 — clear flag
        }
    }
}
```

Opcode **`0x57` (= 87) is `bir::InstCoreBarrier`** — confirmed by the `dyn_cast<bir::InstCoreBarrier>` assert string @`0x1d35f58` (`"… [with To = bir::InstCoreBarrier; From = bir::Instruction]"`) referenced inside this body. For each barrier whose `+0x130` flag is set (sentinel `0xff` = unset), the running counter is written into the ID field at `+0x110` and the flag cleared. The result: globally-unique `CoreBarrier` IDs across the whole linked program. **[CONFIRMED for the opcode test + loop structure; the precise `+0x110`/`+0x130` field offsets are read from this loop only, not cross-checked against an independent `InstCoreBarrier` layout dump (`klr::CoreBarrier_ser` @`0xf475b0` would confirm) — INFERRED.]**

Two companions live in the same translation unit:

- `orderCoreBarriersAndCollectives(BasicBlock&)` (`0x1089370`) — orders `InstCoreBarrier` and collective instructions within a block (the `GPSIMDSB2SB`/`CoreBarrier` sequencing introduced by `lower_local_collectives`), keeping the merged stream's barrier/collective order legal.
- `renumberCoreBarriers(vector<unique_ptr<Module>>&, ulong)` (`0x10906e0`) — a **pre-merge** vector overload (renumber across the still-separate per-core modules, with a base-offset arg).

`addBarrier(vec<Instruction*>, vec<Instruction*>)` (`0x15ca280`) is a distinct wiring helper: for two instruction sets it calls `bir::Instruction::addDependency(Instruction*, EdgeKind, bool)` (@plt `0x5eff00`), adding dependency edges from the first set to the second — the link-time cross-core fence wiring, separate from the ID renumber. **[CONFIRMED — `addDependency` call.]**

---

## 8. The `sgLnk` layout — per-core artifact assembly (`setupLinkDir` @ `0x15d8590`)

`bir_linker` does **not** emit machine code (codegen is L40, downstream). What it assembles is the **link directory** of per-core on-disk artifacts that codegen + the NEFF packager will consume. `setupLinkDir` orchestrates:

```c
// BirLinker::setupLinkDir(vector<unique_ptr<Module>>& modules) @ 0x15d8590  [CONFIRMED]
createLinkDir();                 // @0x15c7910  mkdir "<name>/sgLnk"
symlinkFiles(src, dst, pred);    // @0x15d5630  generic predicate-filtered symlink
symlinkNpyFiles(names);          // @0x15d5a70  weight/const .npy tensors
symLinkDebugInfoFiles(path);     // @0x15d5f70  debug-info artifacts
symLinkCustomOpFiles(path);      // @0x15d6460  custom-op binary blobs
copyCpuNodeFiles(path);          // @0x15d6950  host/CPU-node files (COPIED, not symlinked)
```

The link directory model:

```text
  <name>/
    nc0/sg0/   ← core 0's subgraph artifacts (.npy weights, debug info, custom-op blobs)
    nc1/sg0/   ← core 1's subgraph artifacts
    …
    sgLnk/     ← THE LINKED DIR: symlinks to every core's .npy / debug / custom-op,
               ← plus the unified tensor-map.json (§5e)
```

Each per-core subgraph lives under `nc<coreId>/sg<subgraphId>` (the regex token from §5d); its constant tensors, debug info, and custom-op binaries are **symlinked** into the single `sgLnk` dir (CPU-node host files are **copied**), so the merged module's functions resolve their external file references in one place. The per-core "code section" identity is preserved *structurally*: each core remains its own `bir::Function` tagged with its `NeuronCoreId`, and codegen later emits per-engine 64 B TPB bundles per function. The NEFF packager (J-strand) then tags each per-core function's instruction stream into the NEFF's per-core sections using the `NeuronCoreId` carried on the function. The exact NEFF/kelf section tagging is **Part 12** (planned `formats/`, NEFF / `__kelf` / `sgLnk` packaging) and J33–J36 scope — **NAMED only here, INFERRED**. **[CONFIRMED for the symlink/copy orchestration + the `sgLnk` dir; NEFF section tagging INFERRED.]**

---

## 9. The split ↔ link ↔ NEFF arc

```text
  lnc_splitter (8.30)  concretizeModule @0x16d3bf0
    clone ONE Module per core; set ModuleAttribute key 2 = NeuronCoreId; per-core renames;
    LowerLocalCollectives mints RemoteLocalTargets (vnc_remote_addr_map / remote_target_name).   [CONFIRMED]
        │  per-core compilation: alloc, schedule, codegen-prep (main pipeline)
        ▼
  extend_shared_lifetimes (81) cross-core barriers preserve shared lifetimes;
  coloring_allocator_dram_shared (82) colors shared DRAM;
  sync_shared_allocations (83) follows each RemoteLocalTarget back to the producing core's
    NOW-allocated mloc and COPIES the physical address  →  ADDRESS identity across cores.        [CONFIRMED]
        │
        ▼
  bir_linker (L1, THIS PAGE)  merge the N finished per-core Modules into ONE Module:
    each core → a bir::Function (copyModulesAsFunctions §3);
    dispatcher block of InstCall call_def_<core>_instance_<i> invokes them (addFnCall §3.1);
    cross-core names collapse to unified mlocs via inp/outMap + remote_local_target
      (copyAllocs2Inst §4) with addresses inherited from sync_shared_allocations;
    producer-out ↔ consumer-in ALIAS edges (buildMainAliases §5c);
    DMA queues + semaphores merged (initQueues §6);
    CoreBarrier IDs globally renumbered (renumberCoreBarriers §7);
    per-core npy/debug/custom-op artifacts symlinked into <name>/sgLnk (setupLinkDir §8);
    unified tensor-map written.                                                                  [CONFIRMED]
        │  post-link REG alloc (L33), vnc_link (L34-35), codegen (L40) — whole-program
        ▼
  NeffPackager (L42): packages codegen output + linked tensor-map + per-core sections
    (tagged by NeuronCoreId) into the NEFF / kelf (Part 12, planned formats/).                   [NAMED here]
```

The **inverse relationship** is the cleanest way to hold both passes in mind: `lnc_splitter` is `1 → N` by binding a shard symbol and pruning off-core ops; `bir_linker` is `N → 1` by cloning each core into a callable function and re-unifying the names, queues, barriers, and artifacts the split scattered. The split happens at the *top* of the main pipeline so per-core work can proceed independently; the link happens at the *bottom* (L1 of the low-level builder) so whole-program work — register allocation, codegen, packaging — runs exactly once. ([8.30](lnc-splitter.md) is the split; [the multi-core (LNC) memory model](../arch/lnc-memory-model.md) describes the `N`-core symbolic-shard address model both passes rest on.)

---

## 10. Caveats & confidence ledger

- **`dry_run` (`0x15edd50`)** shares the `"missing input '…'"` diagnostic (@`0x1c87d44`) with the real `run`; it is the no-mutate feasibility/cost probe (loop-fusion / dep-edge accounting strings present) used before committing the link. It does not construct the merged module. **[STRONG.]**
- **`InstCoreBarrier` field offsets (`+0x110` ID, `+0x130` flag)** are read from the renumber loop only; not cross-checked against an independent `InstCoreBarrier` layout dump (`klr::CoreBarrier_ser` @`0xf475b0` would confirm). **[INFERRED.]**
- **`copyAllocs` vs `copyAllocs2` vs `copyAllocs2Func`/`copyAllocs2Inst` division of labor** — module-set passes vs per-function/per-instruction resolvers — is inferred from call structure + naming; the `inpMap/outMap → memMap` collapse is fully evidenced only in `copyAllocs2Inst` rodata. **[INFERRED for the split; the collapse itself is CONFIRMED.]**
- **The runtime dispatch semantics** of the dispatcher block (static manifest vs per-core NEFF-section firing) rest on the J-strand NeffPackager, NAMED only here. **[INFERRED.]**
- The `RemoteLocalTarget` struct shape (`optional<pair<name, ncId>>`) is cross-ref to D-H03/D-H24, not re-disassembled on this page. **[INFERRED.]**

**Re-verify ceiling.** The new-merged-Module model (§2.6, `_Znwm(0x310)` + `Module::Module`), the call-dispatcher BB (§3.1, `insertElement<InstCall>` + `call_def_` rodata), the queue unification (§6, `addQueue` + rodata), and the barrier renumber's opcode test (§7, `cmp … 0x57` + `InstCoreBarrier` assert string) are all CONFIRMED at the disasm+rodata level. The internal field offsets inside `InstCoreBarrier`/`InstCall`, and the runtime NEFF-section dispatch, are the honest INFERRED ceiling.
