# TranslateNKIASTToBIR and the Loop-Unroll Passes

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical for the bodies cited). The pass bodies live in `neuronxcc/starfish/lib/libwalrus.so`; the `full_unroll` driver lives in `neuronxcc/starfish/bin/full_unroll` (a separate ELF). For `.text`/`.rodata` the virtual address equals the file offset; `.data` carries a fixed `+0x400000` delta and is called out where it matters. Treat every address as version-pinned.*

## Abstract

These are the first two stations of the libwalrus backend pipeline that turn an opaque NKI kernel into real, schedulable BIR. **`TranslateNKIASTToBIR`** (execution order 2 in the pre-codegen pipeline) is the *materializer*: it walks the module, finds every `InstNKIKLIRKernel` placeholder (instruction-type 56), and expands each one into a freshly built `bir::Function` plus a synthesized `InstNKIKernel` (type 55) call-site — either by deserializing a binary KLR file and running per-op codegen, or by parsing an embedded BIR-JSON blob with the two-pass loader. The **unroll passes** that follow it (`Unroll`, `FullUnroll`, `HeuristicUnroll`) are the *flatteners*: they clone loop bodies per iteration, fold every symbolic index expression to a concrete constant for that iteration, split each block tensor into per-iteration physical `MemoryLocation`s, and remap every access pattern onto the split storage.

The familiar reference frame is an LLVM loop unroller, but the divergence is total. There is no `LoopInfo` and no SSA: a `bir::InstLoop` (opcode `'i'` = 105) carries a `LoopAxis` (lb/ub/stride) and the unroller substitutes a concrete integer for that axis into a per-iteration `DenseMap<AffineIdx*,long>`, then re-evaluates the pelican `QuasiAffineExpr` algebra against the map to fold indices, orders, predicates, and dependency-carry conditions. Unlike LLVM, the unroller does not just duplicate instructions — it *duplicates tensors*, because each cloned iteration needs its own physical SBUF/PSUM address. Two passes do the same thing two ways: `Unroll` builds an `[iteration][block]` grid of new `MemoryLocationSet`s; `FullUnroll` splits each set's storage in place into `_sub<b>` memlocs. `HeuristicUnroll` does no cloning at all — it is purely the *eligibility decision* (innermost, fully-static, under a budget) that marks which loops the other two flatten.

Two facts dominate this page and both are byte-confirmed. First, the **25,000,000-instruction ceiling**: after `FullUnroll` finishes, a guard fails the whole compile if the post-unroll instruction count exceeds 24,999,999, with the diagnostic "*Compiler generated too many instructions, this may due to a failure in parallelism extraction by the tensorizer*". Second, `HeuristicUnroll`'s **threshold is not a trip-count number** — it fully unrolls a loop iff the loop is innermost and free of symbolic access patterns, capped only by the `MAXHU` environment variable; there is no "unroll loops with fewer than N iterations" heuristic in this build.

For reimplementation, the contract is:

- The `TranslateNKIASTToBIR` module walk: how it finds `InstNKIKLIRKernel(56)` nodes, the two lowering paths (binary KLR vs BIR-JSON), and the uniform `InstNKIKernel(55)` call-site replacement.
- The three unroll passes' division of labour: `Unroll` (production per-instruction cloner, `[iter][block]` grid), `FullUnroll` (level-gated 0/1/2 cloner, `split_map`), `HeuristicUnroll` (innermost+static+`MAXHU` eligibility gate).
- The per-iteration substitution model: the `DenseMap<AffineIdx*,long>` that drives `QuasiAffineExpr::eval`, tensor split, AP repointing, order rebuild, and dependency-carry filtering.
- The two budget ceilings and their exact literals: `Unroll`'s `instCountFitsLimit` (a configurable module max) and `FullUnroll`'s hard `25000000` cap.
- The `full_unroll` standalone binary: a self-contained driver onto the same `FullUnroll` source.

| | |
|---|---|
| **Translate entry** | `TranslateNKIASTToBIR::run(bir::Module&)` @ `0xf0dbc0` (~3.9 KB) |
| **Translate register name** | `"translate_nki_ast_to_bir"` @ `0x1c7db18`; execution order 2 |
| **Node found** | `InstNKIKLIRKernel`, instruction-type **56** (`cmp [rbx+50h], 38h` @ `0xf0e145`) |
| **Node emitted** | `InstNKIKernel`, instruction-type **55** (call-site into the built function) |
| **Unroll entry** | `Unroll::run(bir::Module&)` @ `0xb42f30` (thunk `0x612770`) |
| **FullUnroll entry** | `FullUnroll::run(bir::Module&)` @ `0xb78460`; level gated by `unrollLevel_` |
| **HeuristicUnroll entry** | `HeuristicUnroll::run(bir::Module&)` @ `0xb7d200`; gated by a global flag |
| **Hard ceiling** | `total_count < 25000000` — `FullUnroll::InstProfiler::instruction_limit_check` |
| **Standalone bin** | `neuronxcc/starfish/bin/full_unroll` — `main` @ `0x458f00`, `FullUnroll::run` @ `0x484490` |
| **IR level** | BIR (`bir::Module` / `Function` / `BasicBlock` / `Instruction`), pre-codegen |

---

## TranslateNKIASTToBIR — the NKI-kernel materializer

### Purpose

`TranslateNKIASTToBIR` is the second pass in the walrus pre-codegen pipeline (registered under `"translate_nki_ast_to_bir"` @ `0x1c7db18`, xxd-confirmed). Upstream, an NKI kernel arrives as a single placeholder instruction — `InstNKIKLIRKernel`, instruction-type 56 — that carries nothing but a file path (a serialized KLR binary or a BIR-JSON file), a format string, and the kernel's input/output argument lists. This pass is what turns that placeholder into a real, walkable BIR function body. The companion page [Codegen Helpers & the NKI-Kernel Provision Mechanisms](../bir/codegen-helpers-kernel-provision.md) covers Mechanism A — `lowerKernelInst`'s KLR-codegen entry that mutates an existing module; this section is the pass-level view of how `run` drives that mechanism across the whole module.

> **NOTE —** native BIR (libBIR) hard-asserts "Not Implemented" if it ever sees an `InstNKIKLIRKernel(56)` at opcode-level handling. That is consistent, not contradictory: the type-56 node never survives this pass. It is expanded here, in libwalrus, *before* any libBIR opcode handling runs.

### Entry Point

```text
TranslateNKIASTToBIR::run(bir::Module&)         @0xf0dbc0  ── module walk, node find, removal
  └─ lowerKernelInst(InstNKIKLIRKernel&, BB&)   @0xf0b610  ── per-node A/B dispatch (9643 B)
       ├─ (path B, fmt=="bir") lowerFromBirJson @0xf0a160  ── parse BIR-JSON → createFromJson
       ├─ (path A) checkVersionMatch            @0xf07570  ── KLR "M.m.p" vs "1.0.0" gate
       ├─ (path A) klr::KLRFile_des / KLRMetaData_des / Contents_des  ── binary deserialize
       ├─ (path A) Module::addNKIFunction → KlirToBirCodegen::codegenLncKernel @0xf34140
       └─ copyConstFileToArtifact               @0xf08980  ── stage TensorClass::Const(6) files
```

### Algorithm

`run` is a two-level intrusive-list walk (functions, then basic blocks) with a collect-then-remove inner loop so that expanding a placeholder does not invalidate the block iterator.

```c
function TranslateNKIASTToBIR_run(Module M):              // 0xf0dbc0
    log "Started running Lower KLIR Kernel into NKI kernel instruction"
    for func in M.functions():                            // ilist over M+0x8; FunctionHolder = node-160
        for BB in func.blocks():                          // BasicBlock = node-72
            log "Start of KLIR kernel lowering pass, number of insts: <n>, number of allocs: <m>"
            clock_start = clock()                          // "Scan BKs" timer
            collected = vector<InstNKIKLIRKernel*>{}
            for ins in BB.instructions():                  // ilist walk
                if *(u32*)(ins + 0x50) == 56:              // 0xf0e145  cmp [rbx+50h], 38h  ── IT-56
                    node = (InstNKIKLIRKernel*) ins
                    log "Found InstKLIRKernel: <node.name>"
                    // resolve the kernel file path against the artifact dir if the raw path is missing:
                    p = string(node + 232)                 // kernel filename (ptr,len pair)
                    if not exists(p):
                        p2 = M.getArtifactAbsPath() + "/" + basename(p)
                        assert exists(p2)                  // else NeuronAssertion translate_nki_ast_to_bir.cpp:377
                                                           //   "NKI Kernel file {filename} does not exist,
                                                           //    either in full path or in compiler
                                                           //    artifacts directory"
                        node.path = p2
                    copyFileToFolder(M.getArtifactAbsPath(), node + 232)   // stage the file up-front
                    lowerKernelInst(node, BB)              // expand the placeholder (run body line 548)
                    collected.push_back(node)
            for n in collected:  BB.removeInstruction(n)   // drop the now-expanded placeholders
            log "Scan BKs time (s): <clock() - clock_start>"
```

The node-find is an exact integer compare on the instruction-type field. The `InstructionType` lives at `Instruction+0x58`; `run` reads it at list-node `+0x50` because the pointer it walks is the intrusive-list header (`Instruction+0x8`), so `+0x50` from the node equals `+0x58` from the instruction base. Only type 56 triggers lowering (`cmp [rbx+50h], 38h` @ `0xf0e145`, where `0x38` = 56).

### The two lowering paths

`lowerKernelInst` (@ `0xf0b610`) reads the node's format string at `+0x110` and branches:

```c
function lowerKernelInst(InstNKIKLIRKernel node, BasicBlock BB):    // 0xf0b610
    fmt = string(node + 0x110)                             // disasm @0xf0b649: mov rsi,[rsi+110h]
    if fmt == "bir":                                       // path-B literal @0x1c879a8 ("/backend.bir"+9)
        return lowerFromBirJson(node)                      // 0xf0b689
    // ── path A: serialized binary KLR file ──
    checkVersionMatch(node)                                // gate "M.m.p" against "1.0.0"  (0xf07570)
    f = fopen(string(node + 240), "r")
    if not f:  throw runtime_error("fail to locate KLIR artifact")
    klr::KLRFile_des(&hdr, f)                              // binary deserialize: file header
    if hdr.versionWord != 12:  throw runtime_error("Wrong KLR version")
    klr::KLRMetaData_des(&meta, f)
    klr::Contents_des(&contents, f)
    if contents.tag != 4:  throw runtime_error("Wrong KLIR content type")   // 4 == "KLIR kernel"
    fclose(f)
    F = Module::addNKIFunction()                           // a SEPARATE new NKI function
    *(u32*)(F + 0xD0) = 2                                   // Function kind = NKI
    cg = KlirToBirCodegen(F)
    if node.debugInfo:  cg.setParentTensorizerId(...)
    cg.codegenLncKernel(contents.kernel)                   // KLR AST → BIR, per-op  (0xf34140)
    ... finalize: mark blocks, drop useless empties, set FunctionAttributes ...
    assert cg.getIsAllocated()                             // NeuronAssertion klir_to_bir_codegen.cpp:1670
    // then the InstNKIKernel(55) call-site insertion + arg cloning (shared with path B)
```

Path A is a custom **binary** serialization with three deserialized sections; the file-header version word must equal **12** and the content tag must equal **4** (the "KLIR kernel" content type) or the load throws. The KLR ("Kernel Language Repr") *is* the lowered NKI AST, and `KlirToBirCodegen::codegenLncKernel` (@ `0xf34140`) is the per-op walk that emits BIR from it.

```c
function lowerFromBirJson(InstNKIKLIRKernel node):         // 0xf0a160
    in = ifstream(node.path)
    if not in.is_open():  throw runtime_error(...)
    j = nlohmann::parse(in)                                // json v3.11.3
    log "BIR JSON file has been loaded from disk: <path>"
    require j.is_object()
    fns = j["functions"]                                   // one function per logical neuron core (LNC)
    coreId = M.getNeuronCoreId()
    if coreId set and size(fns) > 1 and coreId-unset-check:
        throw "NeuronCoreId must be set when the LNC count is greater than 1"
    if coreId >= size(fns):
        log "The kernel is compiled for LNC count lower than the current core_id <coreId> skip lowering"
        return false                                       // SKIP — node left in place for this core
    name = fns[coreId].at("name")
    log "Creating NKI function from BIR JSON: <name> and core id: <coreId>"
    F = bir::Function::createFromJson()                    // PASS1 (storages, blocks, insts, args)
    bir::Function::createFromJsonPass2()                   // PASS2 (queues, deps, block-arg phi)
    *(u32*)(F + 0xD0) = 2                                   // NKI kind
    ... scan F.allocs() for SBUF(16)/PSUM(32) to set isAllocated + sbMax ...
    // synthesize the call-site and wire args:
    call = parentFn.insertElement<InstNKIKernel>(insertPt, name)    // IT == 55
    *(u64*)(call + 0xF0) = F                                // call → built function
    FunctionArgumentMap::createMappingFromNames()          // inputs by name
    FunctionArgumentMap::createMappingFromNames()          // outputs by name
    for a in node.inputArgs:  AccessPattern::cloneToArgument(a, call)
    for o in node.outputArgs: Argument::cloneToOutput(o, call)
    if isAllocated:  createKernelScratchpadBuffers<InstNKIKernel>(call)   // size SBUF scratch
    copyConstFileToArtifact(node, M)                       // stage const/weight files
    return true
```

Path B is the BIR-JSON loader: it reuses the module-level two-pass JSON loader (`createFromJson` + `createFromJsonPass2`) on one embedded function. The `"functions"` array is **indexed by NeuronCore id** — LNC (logical-neuron-core) sharding means one function per core; if this core's id is out of range, the kernel is skipped and the placeholder is left in place (a no-op for this core). On the success path the type-56 node is replaced by an `InstNKIKernel(55)` whose `+0xF0` points at the loaded function and whose arguments are cloned by name from the original placeholder.

> **GOTCHA —** `lowerFromBirJson` returning `false` is *not* an error: it means "this core isn't in the JSON, so I left the placeholder alone." A reimplementer who treats every type-56 node as guaranteed-removed after this pass will mis-handle LNC-sharded kernels on cores not represented in the JSON. The removal in `run` only fires for nodes that `lowerKernelInst` actually expanded.

### The uniform node → call-site replacement

Both paths converge on the same shape: the type-56 placeholder becomes **{ a new `bir::Function` (NKI kind), full BIR body } + an `InstNKIKernel(55)` call-site** whose `+0xF0` references that function and whose access-pattern/output arguments are cloned from the placeholder. Path A reaches this via `lowerKLIRToNKI` (@ `0xf09c40`, reached only through its PLT stub `0x5f60f0` — a cross-TU indirect call, so no direct call appears in the dump); path B inlines the equivalent `insertElement<InstNKIKernel>` + clone sequence. The original type-56 node is then removed by `run` (`BasicBlock::removeInstruction`).

`copyConstFileToArtifact` (@ `0xf08980`) walks the lowered function's memory locations, and for any tensor whose `TensorClass` field equals **6** (`Const`) it copies the file-backed data into the NEFF artifact directory and rewrites the `MemoryLocation` path to be artifact-relative — so the packaged compiler output is self-contained.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `TranslateNKIASTToBIR::run` | `0xf0dbc0` | Module walk; IT-56 node find; collect-then-remove | CERTAIN |
| `TranslateNKIASTToBIR::lowerKernelInst` | `0xf0b610` | Per-node A/B dispatch; KLR binary path | CERTAIN |
| `TranslateNKIASTToBIR::lowerFromBirJson` | `0xf0a160` | BIR-JSON path; `createFromJson` + LNC gate | CERTAIN |
| `TranslateNKIASTToBIR::lowerKLIRToNKI` | `0xf09c40` | Shared call-site emit (PLT-only, disasm-only) | HIGH |
| `TranslateNKIASTToBIR::copyConstFileToArtifact` | `0xf08980` | Stage `TensorClass::Const(6)` files | CERTAIN |
| `backend::checkVersionMatch` | `0xf07570` | KLR `"M.m.p"` vs `"1.0.0"` `sscanf` gate | CERTAIN |
| `KlirToBirCodegen::codegenLncKernel` | `0xf34140` | KLR-AST walk → per-stmt BIR codegen | CERTAIN |

> **QUIRK —** `lowerKernelInst`'s `cpp:1670` assert `codegen.getIsAllocated()` means `TranslateNKIASTToBIR` consumes **already-allocated** kernels: the NKI front-end ran the allocator and the KLR/JSON already carries fixed SBUF/PSUM addresses. This pass materializes BIR; it does not allocate. A reimplementation that expects to assign addresses here is one phase too late.

---

## Unroll — the production per-instruction loop unroller

### Purpose

`Unroll` is the general-purpose loop flattener. For each function it (a) duplicates every block tensor into a per-iteration-per-block grid of physical `MemoryLocation`s, (b) clones each loop body once per static iteration, folding the loop axis to a concrete integer and re-evaluating every symbolic expression (index, offset, order, predicate, dependency carry) against that value, and (c) repoints every cloned access pattern onto the duplicated storage. It is the pass whose `LowerDynamicExprs` is the sole backend producer of `pelican::ModuloExpr` — see [pelican::ModuloExpr](../bir/pelican-moduleexpr.md), which establishes that `Unroll` writes the symbolic mod index while `AddressRotation` only chooses the physical slot.

### Entry Point

```text
Unroll::run(bir::Module&)                  @0xb42f30   (thunk 0x612770)
  ├─ legalizeGenericAccess(Module)         @0xb3ce80
  ├─ per-function: unroll_function         @0xb42880
  │    ├─ tensorUnroll(set, f)             @0xb3c520   ── split block tensors → "_set" memlocs
  │    │    └─ tensorSplit                 @0xb39e00   ── per-block new MemoryLocationSet
  │    ├─ unroll_ini_block(BB)             @0xb42760   ── snapshot, expand, delete originals
  │    │    └─ unrollInst(Instruction&)    @0xb3e2e0   ── switch IT 105/106/108 + clone tail
  │    │         └─ genPhyAP               @0xb4f330   ── repoint AP via this+312 grid
  │    └─ (drain deferred cross-iteration dependency fixup queue)
  ├─ InstProfiler::instCountFitsLimit      ── budget guard (unroll.cpp:1151)
  └─ check_in_place(f)                     @0xb361f0   ── SMT re-alloc legality
```

### Algorithm

The `Unroll` object is a substitution engine. Its central state is an `llvm::DenseMap<pelican::AffineIdx*, long>` (the "iteration-substitution map", at `this+27`/`this+216`) that maps each loop axis to its *current* concrete iteration value, plus an "unroll map" `DenseMap<MemoryLocationSet*, vector<vector<MemoryLocation*>>>` (at `this+312`) keyed `[iterationInstance][blockIndex]`.

```c
function Unroll_run(Module M):                           // 0xb42f30
    log "INFO (Unroll) Start unrolling at <ctime>"
    legalizeGenericAccess(M)                              // 0xb3ce80
    reset subst_map (this+27)                             // clear iteration values
    for func in M.functions():
        unroll_function(func)                             // 0xb42880
        func.attr[16] = true                              // mark "unrolled"
        CFG::build(func); CFG::validateTopologicalOrdering(func)
    if M.config[109]:  for func: check_in_place(func)     // 0xb361f0  SMT re-alloc check
    log "INFO (Unroll) DONE unrolling <ctime>"
    renumberCoreBarriers(M)
    InstProfiler::print(this+96)
    if not InstProfiler::instCountFitsLimit(this+96):     // *** BUDGET GUARD ***
        // fmt "...total_count={} ... max_instruction_limit={}..."
        throw NeuronAssertion(...)                        // unroll.cpp:1151; max from M.config[112]
```

```c
function unroll_function(Function f):                     // 0xb42880
    this+66 = 1                                           // reset loop depth
    for set in f.memoryLocationSets():  tensorUnroll(set, f)   // (A) duplicate tensors FIRST
    for reg in f.foreign_registers():   registerUnroll(reg, f) // (B) loop-carried scalar regs
    blocks = snapshot(f.blocks())
    for b in blocks:  unroll_ini_block(b)                // (D) clone loop bodies
    for (from, to, kind) in this.fixupQueue:             // (E) deferred cross-iteration deps
        if to:  Instruction::addDependency(from, to, kind)
    for set in f.memoryLocationSets():  reset_use_def(set)     // (F) rebuild clean
```

`unroll_ini_block` (@ `0xb42760`) snapshots the block's original instructions, calls `unrollInst` on each (which appends clones to the same block), then deletes every original. `unrollInst` (@ `0xb3e2e0`, ~2170 lines) switches on `Instruction::InstructionType` (the 1-char opcode tag at `inst+22`):

```c
function unrollInst(Instruction inst):                   // 0xb3e2e0
    switch inst.opcode:
      case 105 (InstLoop):                               // static for-loop, LoopAxis lb/ub/stride
        axis = inst.loopAxis                              // inst+0xF8
        for idx in [axis.lb, axis.ub) step axis.stride:
            subst_map[axis] = idx                         // record this iteration's value
            unroll_block(loopBody)                        // recurse (clones keyed to idx)
            this+68 += 1                                  // iteration-instance counter
      case 106 (DynamicForLoop):                          // runtime-bound: register-lowered
        emit InstRegisterMove / InstRegisterAlu for the induction var
        LowerDynamicExprs(inst)                           // 0xb38a50 — pelican AffineExpr → reg expr
                                                           //   (sole backend producer of ModuloExpr)
      case 108 (DoWhile):                                 // post-test runtime loop — analogous
      default (every data-path inst):                     // THE CLONE PIPELINE
        if not inst.evalMask():  return                   // predicate statically false this iter → drop
        clone = inst.clone(this+35)                       // vtable+144 into current block
        for a in inst.inputs():   unroll_arg(clone, a, /*out*/0, &failed)
        for a in inst.outputs():  unroll_arg(clone, a, /*out*/1, &failed)
        if failed:  BB.removeInstruction(this+35, clone); return
        remap_dependencies(inst, clone)                   // filter carries by AffinePredicate::eval
        InstProfiler::instCount(this+96, clone)           // budget accounting
        eval_order(inst, clone)                           // 0xb364d0 — rebuild per-iter order keys
        rebuild_loopnest(clone)                           // fold axis terms to const AffineExpr
        Function::addUnrolledInst(f, inst, clone)         // 0xb47aa0 — register orig→clone
```

Each cloned argument is folded by `unroll_arg` (@ `0xb3def0`): an `ImmediateValue`/`SymbolicImmediateValue` is `eval`'d against the subst map to a concrete scalar; an `AccessPattern` (argument-kind 2) is repointed by `genPhyAP` (@ `0xb4f330`), which reads `SymbolicAccessPattern::evalIndex()` to pick `[iterationInstance][block]` out of the `this+312` grid built by `tensorUnroll`/`tensorSplit`. The dependency remap filters each loop-carried edge by `AffinePredicate::eval()` so a cross-iteration carry survives only when its predicate holds for the current iteration; unresolved forward carries are pushed onto a fixup queue drained in `unroll_function` step E.

### The tensor-split grid

```c
function tensorSplit(set_old, set_src, Function f):       // 0xb39e00
    if not set_src.getIsBlock():
        this+312[set_old] = 1x1 grid → set_src.getMemlocByTensorId(0)   // single tensor
        return
    NBlocks = set_src.getNBlocks()
    for b in [0, NBlocks):
        memloc_b = set_src.getMemlocByTensorId(b)
        newSet = f.addMemoryLocationSet("<dtype>_set")
        newLoc = newSet.addMemoryLocation()              // copy (bankId, basePartition, address)
        if TensorClass != 8 and M.config[109] and not memloc_b.isAllocated():
            throw NeuronAssertion "mem.isAllocated()"    // unroll.cpp:928
        this+312[set_old][b] = newSet                    // unroll-map row
```

Each block of a block-tensor becomes its own `MemoryLocationSet`+`MemoryLocation`, indexed by block index; `genPhyAP` later reads this grid to give every cloned AP its own physical storage.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `Unroll::run` | `0xb42f30` | Driver; per-fn loop; `instCountFitsLimit` guard | CERTAIN |
| `Unroll::unroll_function` | `0xb42880` | Tensor dup, body clone, dep-fixup drain | CERTAIN |
| `Unroll::unroll_ini_block` | `0xb42760` | Snapshot → expand → delete originals | CERTAIN |
| `Unroll::unrollInst` | `0xb3e2e0` | Switch IT 105/106/108 + clone pipeline | CERTAIN |
| `Unroll::genPhyAP` | `0xb4f330` | Repoint AP via `this+312` `[iter][block]` grid | CERTAIN |
| `Unroll::tensorSplit` | `0xb39e00` | Per-block new `MemoryLocationSet` (`_set`) | CERTAIN |
| `Unroll::eval_order` | `0xb364d0` | Fold "order" `QuasiAffineExpr` vector per iter | CERTAIN |
| `Unroll::check_in_place` | `0xb361f0` | SMT re-allocation legality after unroll | STRONG |
| `Unroll::unroll_arg` | `0xb3def0` | `ImmediateValue::eval` / `unroll_AP` dispatch | INFERRED (decompile failed) |

> **NOTE —** `Unroll`'s budget guard (`instCountFitsLimit`, `unroll.cpp:1151`) reads its maximum from a module config field (`M.config[112]`), so it is a configurable per-compile limit — distinct from `FullUnroll`'s hard-coded `25000000`. Both can abort the compile, but only one is a literal in the binary.

---

## FullUnroll — the level-gated unroller with the hard ceiling

### Purpose

`FullUnroll` is a separate pass keyed by an explicit `unrollLevel_` (0, 1, or 2). It mirrors `Unroll`'s cloning machinery but repoints access patterns through a `split_map` of sets and splits each set's storage in place into `_sub<b>` memlocs. Level 0 does memloc generation only; level 1 unrolls the functions explicitly marked for unroll; level 2 unrolls everything and drops the dependency graph. After cloning, it enforces the headline **25-million-instruction ceiling**.

### Entry Point

```text
FullUnroll::run(bir::Module&)              @0xb78460
  ├─ (level 0)  memlocsGeneration          @0xb6c160
  │              └─ reset_and_generate_memlocs @0xb6af60  ── per-block "_sub<b>" memlocs in place
  ├─ (level 1/2) pre_process               @0xb6e900
  │              └─ unroll_function         @0xb781f0      ── body cloner (split_map AP repoint)
  ├─ InstProfiler::print → instruction_limit_check  ── *** 25M HARD CEILING ***
  └─ (level 2)  erase_all_dependencies      ── drop the dep graph entirely
```

### Algorithm

```c
function FullUnroll_run(Module M):                       // 0xb78460
    level = this.unrollLevel_                             // M-side +42*4 (ctor arg)
    assert level != -1                                   // "Need to set full_unroll level, 0: only
                                                          //  memloc generation; 1: unroll some;
                                                          //  2: unroll all"  (full_unroll.cpp:0x383)
    if level == 0:
        memlocsGeneration(M)                             // split block tensors only; then dce
        return
    pre_process(M)
    log "INFO (FullUnroll) Start unrolling at <ctime>"
    f0 = M.firstFunction()
    if f0.hasAttribute(2):                                // "is unrollable"
        if f0.hasAttribute(1):                            // "is marked to unroll"
            unroll_function(f0)                          // 0xb781f0
            reset every SymbolicAccessPattern's addr vector
            dce(M); log "INFO (FullUnroll) DONE unrolling <ctime>"
        else:
            dce(M); log "DONE DCE, no unrolling needed, running BirCodeGen with unroll pass"
    else:
        log "No unrolling needed, running hhir pass"
    InstProfiler::print(this+96)
    // *** HARD CEILING — instruction_limit_check, full_unroll.h:0x5C ***
    if *(int64*)(this+152) > 0x17D783F:                  // total_count > 24,999,999
        assert false:  "total_count < 25000000 && \"Compiler generated too many instructions,
                         this may due to a failure in parallelism extraction by the tensorizer !\""
    if level == 2:  erase_all_dependencies(M)            // everything statically scheduled
```

The level-0 path replaces each block tensor's single `MemoryLocation` with `NBlocks` per-block memlocs **inside the same set** (named `_sub<b>`), which is the cheap variant used when only storage needs to be regenerated:

```c
function reset_and_generate_memlocs(MemoryLocationSet set):   // 0xb6af60
    assert set.num_memorylocations() == 1                 // "MemlocSet cannot be empty"
    if not set.getIsBlock():  reset tensorId2MemLoc; orig.tensorId = 0; return
    NBlocks = set.getNBlocks()
    for b in [0, NBlocks):
        name = set.name + "_sub" + to_string(b)
        newLoc = set.addMemoryLocation()                  // copy (bankId, basePartition, address)
        newLoc.tensorId = b
    rebuild tensorId2MemLoc; set.removeMemoryLocation(orig)
```

> **GOTCHA —** the 25M check fails on the *post-unroll* total, and the diagnostic blames the tensorizer's parallelism extraction, not the unroller. The literal is `0x17D783F` = 24,999,999, and the compare is strictly greater (`total_count < 25000000` must hold). A reimplementation that caps at exactly 25,000,000 (inclusive) is off by one against the binary. This ceiling exists because full unroll of a deep, wide loop nest can explode the instruction count; hitting it almost always means an upstream pass failed to extract parallelism and the program tried to materialize a billion scalar ops.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `FullUnroll::run` | `0xb78460` | Level 0/1/2 gating; 25M ceiling | CERTAIN |
| `FullUnroll::memlocsGeneration` | `0xb6c160` | Level-0: split all sets, then `dce` | CERTAIN |
| `FullUnroll::reset_and_generate_memlocs` | `0xb6af60` | Per-block `_sub<b>` memlocs in place | CERTAIN |
| `FullUnroll::genPhyAP` | `0xb71400` | Repoint symbolic AP via `split_map` | CERTAIN |
| `FullUnroll::transfer_phyAP` | `0xb72170` | Repoint already-physical AP via `split_map` | CERTAIN |
| `FullUnroll::evalIndex` | `0xb6ade0` | Fold AP index → block/slab index or −1 | CERTAIN |

---

## HeuristicUnroll — the eligibility decision

### Purpose

`HeuristicUnroll` performs **no cloning**. It is a thin driver gated by a global on/off flag that, when enabled, walks each function's loop nest and decides which loops are *eligible* to be fully unrolled. The actual body cloning is left to the `Unroll`/`FullUnroll` machinery; this pass only marks and counts.

### Algorithm

```c
function HeuristicUnroll_run(Module M):                  // 0xb7d200
    if not global_flag:  log "Heuristic unroll is off.";  return
    total = 0
    for f in M.functions():  total += heuristic_unroll(f)     // 0xb7c620 — free fn, not a method
    log "Heuristically unrolled <total> loops."

function heuristic_unroll(BasicBlock BB, ofstream dbg):  // 0xb7bfe0
    if env MAXHU set:  cap = atoi(MAXHU)                  // global counter dword_3DFF678
    count = 0
    for inst in BB.instructions():
        if inst.opcode != 105 (InstLoop):  continue
        if not is_loop_innermost(inst):
            count += recurse(inst.childBlocks); continue // outer loop: descend
        if loop_contains_symbolic(inst):  continue       // symbolic AP → NOT eligible
        // ELIGIBLE: innermost AND fully static
        assert distance(inst.blocks().begin(), end()) == 1   // single body block (heuristic_unroll.cpp:0xB4)
        if MAXHU set and dword_3DFF678 >= cap:
            log "skip (cap <cap> reached)"               // do NOT unroll
        else:
            log "unroll"; dword_3DFF678 += 1             // mark for full unroll
        count += 1
    return count
```

> **QUIRK —** "heuristic" here is **not** a trip-count threshold. A loop is fully unrolled (factor = its trip count) iff it is (a) the innermost loop, (b) free of symbolic/runtime access patterns, and (c) under the `MAXHU` global cap. There is no "unroll loops with fewer than N iterations" rule and no fractional partial factor — the decision is innermost + static + budget. The only numeric tunable is the `MAXHU` environment variable, which caps *how many* loops are unrolled across the whole run, not the trip count of any one loop. A reimplementer expecting a classic cost-model threshold will not find one in this build.

### The split factor (≤ 16) is not a loop factor

The one ≤-16 cap that does exist — `getLargestFactorWithThreshold` (@ `0x506fa0` in the `full_unroll` bin, `utils.cpp`), which returns the largest divisor of a dimension whose paired cofactor is ≤ `MaxFactor` = **16** (hard literal `0x10`, with the trial-divisor loop capped at `0x11` = 17) — governs **storage/engine split granularity** (DGE/DMA descriptor fan-out: SWDGE 1020, HWDGE 65520), *not* loop trip slicing. `MaxFactor` is not a `cl::opt`; it appears only as the assert string `"MaxFactor > 0"`. Do not conflate the ≤16 storage-split factor with a per-loop unroll factor — there is no fractional partial-peel of a loop in `FullUnroll`.

---

## The `full_unroll` standalone binary

`neuronxcc/starfish/bin/full_unroll` is a separate ELF (2.9 MB, BuildID `88d5ca05…`) that drives the same `FullUnroll` source as a command-line tool. It is **not** a thin shim over libwalrus: `readelf -d` shows it links `libBIR.so` + `liblogging.so` and has **no `libwalrus.so` dependency**; it statically embeds its own copies of `FullUnroll::run`, `unroll_function`, `memlocsGeneration`, `tensorSplit`, `genPhyAP`, and `getLargestFactorWithThreshold`. The *code* is the same source (so every algorithm above applies); it is statically linked here rather than delegated.

```c
function main(argc, argv):                               // 0x458f00
    llvm::cl::ParseCommandLineOptions(argc, argv, "")
    logging::setConsoleLogLevel(verbose)                 // -verbose, default 30
    M = bir::Module()
    fu = FullUnroll(opts, unrollLevel)                   // 0x475110; -unrollLevel default 0, stored this+0xA8
    M.load()                                             // positional <input file>
    status = fu.run(M)                                   // 0x484490 — same level gating + 25M ceiling
    M.save()                                             // -o <output file name>
    return status != 0
```

The bin registers three backend passes via static-init lambdas, each constructing `new FullUnroll(opts, LEVEL)`:

| Registrar | Generator | Level | Effect |
|---|---|---|---|
| `full_unroll_memloc_generation` | `sub_4868F0` | 0 | Split block tensors into `_sub<b>` memlocs only |
| `full_unroll_some` | `sub_4867E0` | 1 | Unroll the functions marked for unroll, then DCE |
| `full_unroll_all` | `sub_4869D0` | 2 | Unroll everything, then `erase_all_dependencies` |

With no `-unrollLevel`, the default is **0** — the bin run bare does only memloc generation. The Python `WalrusDriver` passes `-unrollLevel` explicitly per compile phase. The 25M ceiling (`total_count < 25000000`, `instruction_limit_check`, `full_unroll.h:0x5C`) is present and byte-identical in this binary, confirming the same literal seen in `libwalrus.so`.

> **CORRECTION (D-K12) —** an earlier note described the `full_unroll` bin as "a thin CLI shim dynamically linking libwalrus." That is wrong: `readelf -d` shows no `libwalrus.so` `NEEDED` entry, and the FullUnroll bodies, the three registrars, and the 25M check are all directly present in this ELF. It is a self-contained executable that links only `libBIR` + `liblogging`. The algorithmic description still applies because the source is shared.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| Translate walk finds IT-56 nodes | `cmp [rbx+50h], 38h` @ `0xf0e145` in `run` disasm; logs present | CONFIRMED |
| Three distinct unroll passes | `Unroll::run`/`FullUnroll::run`/`HeuristicUnroll::run` @ `0xb42f30`/`0xb78460`/`0xb7d200`; distinct logs | CONFIRMED |
| 25M instruction ceiling | `total_count < 25000000` string in both libwalrus.so and full_unroll bin; `0x17D783F` compare | CONFIRMED |
| `full_unroll` standalone | three registrar strings, level assert, 25M all present in the bin's own `.text` | CONFIRMED |
| `Unroll::LowerDynamicExprs` = sole `ModuloExpr` producer | xref table per [pelican::ModuloExpr](../bir/pelican-moduleexpr.md) | CONFIRMED |
| HeuristicUnroll = innermost + static + `MAXHU` | `"Heuristic unroll is off."`, `MAXHU` env, single-body-block assert | CONFIRMED |
| `lowerKLIRToNKI` call-site wiring | disasm-only (no Hex-Rays), inferred from callee set + path-B mirror | HIGH |
| `unroll_arg` dispatch detail | decompilation failed; reconstructed from callee tables | INFERRED |
| Module config byte semantics (`config[106/107/109/112]`) | named by effect only | SPECULATIVE |

---

## Related Components

| Name | Relationship |
|---|---|
| `TranslateNKIASTToBIR` (order 2) | Materializes type-56 placeholders before unroll runs |
| `Unroll` | Production loop flattener; producer of `ModuloExpr` |
| `FullUnroll` | Level-gated flattener with the 25M ceiling |
| `HeuristicUnroll` | Eligibility decision; cloning done by Unroll/FullUnroll |
| `full_unroll` bin | Standalone driver onto the same `FullUnroll` source |

## Cross-References

- [Codegen Helpers & the NKI-Kernel Provision Mechanisms](../bir/codegen-helpers-kernel-provision.md) — Mechanism A's `lowerKernelInst` KLR-codegen entry; this page is the pass-level view
- [pelican::ModuloExpr](../bir/pelican-moduleexpr.md) — `Unroll::LowerDynamicExprs` is the sole backend producer of the ring-index `ModuloExpr`
- [Penguin Axis / Loop-Axis Model](../penguin/axis-loop-model.md) — the `LoopAxis` lb/ub/stride and `AffineIdx` substitution the unroller folds against
