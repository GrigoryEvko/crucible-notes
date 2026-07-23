# The BackendPass Hierarchy and the 150-Name → 121-Class Registry

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). Everything here lives in `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`). For `.text` and `.rodata` the virtual address equals the file offset. Other wheels differ — treat every address as version-pinned.*

## Abstract

The Walrus backend is a pass pipeline, and this page documents its spine: the abstract `neuronxcc::backend::BackendPass` base, the three `run` entry overloads it exposes, the fork/join granularity classes layered above it, and — the headline — the **pass registry** that turns a pass *name* into a constructed pass *object*. The registry is the seam the `walrus_driver` CLI ([3.7](../frontend/walrus-driver-cli.md)) drives: every `--pass <name>` resolves through it.

The single number that organises the whole backend is the collapse **150 → 121**. There are exactly **150** `register_generator_<name>__` free functions — one per legal pass name — but they construct only **121** distinct C++ pass classes. Ten classes are registered under more than one name, parameterized at construction time by a memory space, an optimization level, or a scheduling phase, so the same class serves several pipeline roles under several names. Above that, the pipeline quotes roughly **180** *steps*, because some names appear at several pipeline positions and because non-real check/dump/sim passes are interposed between named steps. This page recovers all three counts and shows exactly where each collapse arises.

The bar for reimplementation is that a reader can reconstruct: the `BackendPass` interface and which of its methods is the driver entry; how `GeneratorRegistration` populates a `name → factory` hashtable at library load via 150 static initializers; how a factory lambda's `_M_invoke` body is the ground-truth evidence for the `name → class` mapping; the three fork granularities and how they are assigned per call-site rather than per class; and the full 150-name table with its 10 multi-registered classes.

| | |
|---|---|
| **Abstract base** | `neuronxcc::backend::BackendPass` — vtable @ `0x3d98548` |
| **Driver entry** | `BackendPass::run(vector<unique_ptr<bir::Module>>&)` @ `0x1736d40` |
| **Per-unit hooks** | `<Class>::run(bir::Module&)` and/or `runSubgraph(...)` (e.g. `BirSim::runSubgraph` @ `0x1109440`) |
| **Registry singleton** | `GeneratorRegistration::getInstance()` @ `0x17356e0` |
| **Register / lookup** | `registerGenerator` @ `0x1735910` · `getGenerator` @ `0x1735740` · `hasGenerator` @ `0x1735630` |
| **Factory evidence** | each name's `_Function_handler<…,{lambda#1}>::_M_invoke` (e.g. `unroll` @ `0xb46400`) |
| **Granularity classes** | `ContainerPass` → `CoreForkPass` / `ModuleForkPass` / `SubgraphForkPass`; `DapPass : BackendPass` |
| **Counts** | **150** register names · **121** distinct classes · ~**180** pipeline steps |

---

## The `BackendPass` base interface

### Purpose

`BackendPass` is the abstract base every backend pass derives from. It is a *template-method* base in the classic sense: it owns one non-virtual entry method that does the bookkeeping common to all passes, and dispatches the actual per-unit work to a virtual hook the derived class overrides. A reimplementer needs three things from it — the entry overload the pipeline calls, the virtual hook it dispatches to, and the two predicate methods (`isRealPass`, `dry_run`) that the verifier/measurement front uses.

### The three `run` overloads

The base exports three methods, all present in `nm -DC libwalrus.so`:

```text
BackendPass::run(vector<unique_ptr<bir::Module>>&)         T  0x1736d40   ── DRIVER ENTRY
BackendPass::dry_run(vector<unique_ptr<bir::Module>>&, logging::Logger&) T 0x173af40   ── DRY-RUN
BackendPass::isRealPass() const                            W  0x87fd70    ── weak inline predicate
```

The vector overload at `0x1736d40` is **the** entry point the pipeline invokes for every pass. It is the *first* of the three `run` shapes a reader meets:

1. **`run(vector<unique_ptr<bir::Module>>&)`** — the driver entry. The vector holds one `bir::Module` per LNC core (the N per-core modules a Logical NeuronCore split produces). This overload is non-virtual on the base; it threads the module name into a Boost.Log scoped attribute, then **dispatches one virtual hook** through the object's vtable.
2. **`run(bir::Module&)`** — the per-module work hook the *leaf* passes override. Most transform passes implement only this (e.g. `Unroll::run(bir::Module&)` @ `0xb42f30`). It is called once per module element of the vector.
3. **`runSubgraph(vector<unique_ptr<bir::Module>>&)`** — the per-subgraph hook, overridden by the passes that fork over subgraphs (e.g. `BirSim::runSubgraph` @ `0x1109440`). `SubgraphForkPass` calls *this* hook on each subgraph rather than the per-module one.

The dispatch is visible in the disassembly of the vector overload — the entry loads the object's vptr and calls the slot at `vt+0x10`:

```c
// neuronxcc::backend::BackendPass::run(vector<unique_ptr<bir::Module>>&)  @0x1736d40  [CONFIRMED]
//   %r15 = `this` (the pass);  %rdx = &modules vector
//   1736d70:  mov    (%r15),%rax        // %rax = vptr (load the pass's vtable)
//   1736d73:  mov    (%rbx),%rdx        // %rdx = &modules[0] (first bir::Module)
//   1736d7c:  call   *0x10(%rax)        // INDIRECT call vtable slot at +0x10 == the per-unit hook
unique_ptr<BackendPass> /*ret in *%r13*/
BackendPass_run(BackendPass *this, vector<unique_ptr<bir::Module>> *modules) {
    // ... push module name into a boost.log scoped attribute ...
    (*(void(**)(BackendPass*, bir::Module*))(*(void***)this + 2))(this, modules->begin());
    // the +0x10 slot resolves, per dynamic type, to the derived
    //   run(bir::Module&)  or  runSubgraph(...)  override
}
```

The `call *0x10(%rax)` is the entire dispatch mechanism: the base's `run` is fixed code, the *behaviour* is whatever the derived class installs at vtable slot `+0x10`. (Slot index = `(0x10 − 0x10)/8 = 0`, measured from the loaded vptr — i.e. the first non-RTTI slot.)

*Anchors: `mov (%r15),%rax ; mov (%rbx),%rdx ; call *0x10(%rax)` at `0x1736d70`–`0x1736d7c`.*

### The two predicates

- **`isRealPass()`** @ `0x87fd70` (weak, inlined) distinguishes *real* transform passes from the synthetic check/dump/measurement passes (`Dumper`, `Hasher`, `perf_sim*`, the verifiers, `report_stats`) that the backend's `CheckPassAdder` interposes *between* named passes. `ContainerPass` overrides it at `0x1743320`. A `false` return is the marker that a pass is structural padding in the step count, not a pipeline transform.
- **`dry_run(vector<Module>&, Logger&)`** @ `0x173af40` is the legality/measurement traversal: run the pass *without committing emission*, used by the verifier and perf-sim "measure" front. The symbol is unambiguous; the measurement role is read from the codegen-driver side rather than from this body.

The vtable backing all this (`vtable for BackendPass` @ `0x3d98548`) has the standard Itanium layout — RTTI header (offset-to-top, `typeinfo*`) at `vt+0x00`, the D1/D0 destructor pair, then the per-unit hook at `+0x10`. The remaining slot ordering is **INFERRED**: the vtable function pointers are RELR/`.rela`-packed and read as zero in the raw `.data.rel.ro` image, so the `+0x10` slot — which the disassembly of `run()` shows is the dispatched hook — is the only one actually pinned.

### Construction signature

Every leaf pass shares a uniform construction signature: a base ctor `backend::<Class>::<Class>(PassOptions const&)`. Multi-registered classes additionally expose a *parameterized* overload that selects the variant. Two examples:

```text
ColoringAllocator::ColoringAllocator(PassOptions const&, Type, bool,
                                     bir::MemoryAddressSpace, bir::MemoryAddressSpace)  @0x9870c0
DapPass::DapPass(PassOptions const&, std::string const& name, bool)                    @0x87d930
```

`PassOptions` is the one argument shared by *all* factory lambdas — the config bundle the CLI fills from the resolved flags. The extra `ColoringAllocator` arguments (a `Type` enum, two `MemoryAddressSpace` values) are exactly the discriminators that let one class be registered under eight names; see [the collapse](#the-150--121-collapse).

---

## The granularity classes (the fork/join model)

Granularity is layered **above** `BackendPass`, not inside it. A leaf pass does not know whether it runs serially or in parallel; that is decided by *how it is added* to a pipeline. The spine is one container class and three forks.

```text
BackendPass                                  (abstract base, §1)
  └─ ContainerPass : BackendPass             holds an ordered list of child pass factories
       ├─ CoreForkPass     : ContainerPass   PER-CORE parallel  (fork over per-core bir::Modules)
       ├─ ModuleForkPass   : ContainerPass   PER-MODULE parallel (one fork per module in the vector)
       └─ SubgraphForkPass : ContainerPass   PER-SUBGRAPH parallel (calls children's runSubgraph hook)
  └─ DapPass : BackendPass                    adapts an external "DAP" analyzer (NOT a ContainerPass)
```

### `ContainerPass` — the holder

A `ContainerPass` *is* a `BackendPass` (so it has the same `run` entry) that holds a list of child factories and runs them in turn:

```text
ContainerPass::addPass(function<unique_ptr<BackendPass>(PassOptions)>)  0x1739cc0   ── add by factory
ContainerPass::addPass(string const&)                                  0x1739d40   ── add by NAME (→ registry)
ContainerPass::runHelper(vector<Module>&, Logger&, string const&)      0x1739da0   ── run the children
ContainerPass::getKind() const                                        0x1737480   ── LLVM-style discriminator
ContainerPass::isRealPass() const                                     0x1743320   ── override
```

The by-name `addPass` is the bridge to the registry: handed a string, it asks `GeneratorRegistration` for the factory and stores it. `getKind()` plus a per-subclass `classof()` form an LLVM-style RTTI discriminator — every fork subclass is identified by `getKind()` and tested with `<Fork>::classof(ContainerPass const*)`. `getKind` sits at `0x1737480`, and all three `classof` symbols are present.

### The three forks

Each fork is a `ContainerPass` whose `run(vector<Module>&)` overload forks the children across one axis, runs them, and joins. Every address below is a symbol:

| Fork | Axis | `classof` | `run(bir::Module&)` | `run(vector&)` | `dry_run` |
|---|---|---|---|---|---|
| `CoreForkPass` | per-core | `0x17374d0` | `0x173c950` | `0x173e2c0` | `0x173ebf0` |
| `ModuleForkPass` | per-module | `0x1737490` | `0x173bca0` | `0x17404d0` | `0x173f5d0` |
| `SubgraphForkPass` | per-subgraph | `0x17374b0` | `0x173c2f0` | `0x173cfa0` | `0x173d8e0` |

The fork machinery is TBB-backed: the per-element child invocation runs under `tbb::detail::r1::execute_and_wait(parallel_for …)`, the same TBB arena that drives the per-`MemoryLocation` parallel-for elsewhere in the backend. The `parallel_for` calls are present in the pass bodies; the exact shape of the fork wrapping around them is reconstructed rather than byte-exact.

### `DapPass` — the odd one out

`DapPass` derives straight from `BackendPass`, **not** from `ContainerPass`. It adapts an external "DAP" analyzer/transform and carries its own name string in its ctor (`DapPass(PassOptions const&, string const& name, bool)` @ `0x87d930`). It exports its own `run(bir::Module&)` @ `0x87d7e0`, `run(vector&)` @ `0x87db10`, and `getAdapterInstance()` @ `0x87da40`. It is nonetheless eligible for subgraph-fork wrapping (see the template instance below). The non-`ContainerPass` parentage shows up as an absence: there is no `DapPass::classof(ContainerPass const*)`, and the only `run` overloads it carries are `BackendPass`-shaped.

### Granularity is assigned per call-site

The decisive fact: **the granularity of a pass is a property of its call-site, not of its class.** The `BackendPassManager` exposes an `add*` API that wraps a serially-added pass in a fork at *that* pipeline position:

```text
BackendPassManager::addPass(string)                        → SERIAL child of the current pipe
BackendPassManager::addModParallelPass[WithName]<C>        → wraps C in a ModuleForkPass
BackendPassManager::addCoreParallelPass[WithName]<C>       → wraps C in a CoreForkPass
BackendPassManager::addSubgraphParallelPass[WithName]<C>   → wraps C in a SubgraphForkPass
```

So the *same* leaf class can be added serially in one place and inside a fork in another. The compile-time fork bindings, read from the `add*ParallelPass<Class>` template instantiations in the symtab, are:

| Fork | Classes carrying a compile-time fork instance |
|---|---|
| `ModuleFork` | `AddressRotation`, `MemoryAnalysis`, `AntiDependencyAnalyzer`, `InlineNKIKernel`, `PreOpts` |
| `CoreFork` | `DMAReport`, `MemoryAnalysis`, `AntiDependencyAnalyzer`, `BirLinker` |
| `SubgraphFork` | `BirSim`, `BirSimWithKernelInline`, `DapPass` |

`MemoryAnalysis` and `AntiDependencyAnalyzer` appear under **both** module- and core-forks — they run at several pipeline points at different fork granularities — both `addModParallelPass<MemoryAnalysis,…>` and `addCoreParallelPass<MemoryAnalysis,…>` appear in `nm -DC`.

A handful of class names *end* in `Pass` but are ordinary leaf passes, **not** granularity bases: `LowerSelectPass`, `OrderConstraintsPass`, `PerfSimPass`, `PerfSimPackagePass`, `SynchronizerPass`, `VNSplitterPass`. Each has its own `register_generator_*__` factory in the table below. Do not mistake the suffix for membership in the fork spine; each has its own RTTI typeinfo and factory function.

---

## The registration mechanism: `GeneratorRegistration`

### Purpose

`GeneratorRegistration` is the backend's pass-factory registry — the analogue of LLVM's `PassRegistry`. It is a singleton holding a hashtable from pass *name* to a factory closure. Every legal `--pass <name>` is a key in this table; the value is a `std::function` that, when called with the `PassOptions`, constructs the pass object. This is the indirection that lets the CLI name passes as strings and lets one class answer to several names.

### The API

```text
GeneratorRegistration::getInstance()                       0x17356e0   ── the singleton
GeneratorRegistration::registerGenerator(string, function) 0x1735910   ── install a name → factory
GeneratorRegistration::getGenerator(string const&)         0x1735740   ── name → factory
GeneratorRegistration::hasGenerator(string const&)         0x1735630   ── name validity test (used by the CLI)
GeneratorRegistration::getPassNames[abi:cxx11]()           0x17357e0   ── dump all names (--list-passes)
```

The backing store is a `_Hashtable<string, pair<const string, function<unique_ptr<BackendPass>(PassOptions const&)>>>`. This exact value type is visible in the symbol the driver-entry `run` rehashes against — so the registry value really is *the factory closure type*. All five API symbols are present, and the value type `function<unique_ptr<BackendPass>(PassOptions const&)>` appears verbatim in the `_M_rehash` symbol called from `BackendPass::run`.

`getPassNames()` is what `walrus_driver --list-passes` dumps; `hasGenerator` is the validity gate that turns an unknown `--pass` into the `Backend pass: <X> is not registered!` error ([3.7](../frontend/walrus-driver-cli.md)).

### How a name gets into the table — the static initializers

Each `register_generator_<name>__` is a **static guard object** in `.bss` (nm class `B`), e.g. `neuronxcc::backend::register_generator_unroll__` @ `0x3dff58c`. A global static initializer, run at `dlopen`, calls:

```c
GeneratorRegistration::getInstance().registerGenerator("<name>",
    std::function<unique_ptr<BackendPass>(PassOptions const&)>( <lambda#1> ));
```

The lambda is type-erased through `std::_Function_handler<…>`, which gives **two weak functions per name** — `_M_invoke` (call the lambda) and `_M_manager` (copy/destroy) — plus the lambda's typeinfo. For `unroll` these are:

```text
_Function_handler<…, register_generator_unroll__::{lambda(PassOptions const&)#1}>::_M_invoke   @0xb46400   ── FACTORY BODY
_Function_handler<…, register_generator_unroll__::{lambda(PassOptions const&)#1}>::_M_manager   @0xb45540
typeinfo for register_generator_unroll__::{lambda(PassOptions const&)#1}                        @0x3d8dce8
```

> **Pitfall.** The IDA decompiled `*.c` sidecars for these `register_generator` lambdas are **PLT-thunk stubs**, not the real bodies. To read the constructed class you must `objdump -d --start-address=0xVA` the `_M_invoke` directly.

### `_M_invoke` is the ground-truth `name → class` evidence

The `_M_invoke` body is the canonical proof of which class a name builds: it allocates with `operator new` and calls the class ctor (or `make_unique<Class>`), then installs that class's vtable. For `unroll`, the body at `0xb46400` reads:

```c
// _M_invoke for register_generator_unroll__  @0xb46400  [CONFIRMED]
//   b46414:  call  5fb160 <_Znwm@plt>                                  // operator new(sizeof(Unroll))
//   b46422:  call  612430 <neuronxcc::backend::Unroll::Unroll(PassOptions const&)@plt>   // ctor
unique_ptr<BackendPass> unroll_factory(PassOptions const& opts) {
    void *p = operator new(sizeof(backend::Unroll));   // _Znwm
    backend::Unroll::Unroll((Unroll*)p, opts);         // construct → installs vtable for Unroll
    return unique_ptr<BackendPass>((BackendPass*)p);
}
```

Three evidence shapes recur across the 150 bodies, and the table below tags each row by which one was observed: `ctor` = a direct `backend::Class::Class(PassOptions)` PLT call; `mkuq` = a `make_unique<backend::Class>` call; `vt` = the `vtable for backend::Class` symbol the lambda installs. All three pin the same fact — *which class this name constructs*. The `unroll` body above is the exemplar; every row below was resolved the same way.

### Pipeline-build-time selection

At pipeline-build time, the optlevel pipeline builders call `addPass("<name>")`, which does `getGenerator("<name>")(passOptions)` → `unique_ptr<BackendPass>`. The same registry also serves the arch-`Generator` lookup: the `codegen` slot's `Codegen` pass resolves `CoreV{2,3,4}Gen` through `GeneratorRegistration::getGenerator`. The registry is thus the *single* name-to-object indirection for the entire backend: `getGenerator` @ `0x1735740` is the shared lookup for both pass and generator slots.

---

## The 150 → 121 collapse

This is the headline mechanism. **150** register names construct **121** distinct classes because **10** classes are registered under multiple names. The extra names are not aliases in the trivial sense — each one constructs the same class with a *different ctor argument*, so the binary genuinely needs all 150 names but only 121 vtables.

### Verifying the counts

```text
$ nm -DC libwalrus.so | rg -o 'register_generator_[a-z0-9_]+' | sort -u | wc -l
150                                          # ← 150 registered pass NAMES
```

121 is the count of distinct `_M_invoke` ctor targets across those 150 bodies (the table below). The two are reconciled exactly by the multi-registration groups.

### The 10 multi-registered classes

These are the classes that answer to more than one name; the "extra" column is `(names − 1)`, i.e. how many *surplus* names each contributes over its single class. The discriminator is the ctor flag named in the rightmost column.

| Class | Names | Extra | Discriminator (ctor argument / phase) |
|---|---:|---:|---|
| `MemoryAnalysis` | 10 | 9 | which prior pass the snapshot follows (post-rotation/coloring/dma/sched/unroll) |
| `ColoringAllocator` | 8 | 7 | `Type ∈ {SB, PSUM, REG, DRAM, DRAM-shared}` + `post_lnk` / `debug` flags |
| `AddressRotation` | 4 | 3 | space `dram` / `psum` / `sb` + `post_schedule` phase |
| `DMAOptimization` | 3 | 2 | `psum` / `sb` / `input-coalescing` |
| `FullUnroll` | 3 | 2 | `unrollLevel ∈ {all, some}` + `memloc-generation` |
| `SeparateLoadAndCompute` | 3 | 2 | base / `with_memset` / `post_ada` |
| `AntiDependencyAnalyzer` | 2 | 1 | base / `post_shared_dram` |
| `DeadCodeElim` | 2 | 1 | optimization level `o0` / `o1` |
| `PerfSimPass` | 2 | 1 | `perf_sim` / `perf_sim_at_end` |
| `PrefetchScheudling` | 2 | 1 | `before_sched` / `after_sched` *(class name misspelled in the binary — preserved verbatim)* |

Sum of the surplus column: `9 + 7 + 3 + 2 + 2 + 2 + 1 + 1 + 1 + 1 = 29`. Therefore `121 + 29 = 150`. The `MemoryAnalysis` group's 10 names and the `ColoringAllocator` / `AddressRotation` groups were enumerated directly from `nm -DC`.

A worked example: `MemoryAnalysis` is a single class registered under ten snapshot names (`memory_analysis_after_unroll`, `…_after_pre_sched`, `…_after_post_sched`, `…_after_address_rotation_{dram,psum,sb}`, `…_after_coloring_allocator_{sb,dram_post_lnk,dram_shared}`, `…_after_dma_optimization_sb`). Each name's `_M_invoke` constructs the same `MemoryAnalysis` with a `MemoryAnalysisRequest&` that says *which* prior pass it is profiling — matched by the `addModParallelPass<MemoryAnalysis, char const(&)[N], MemoryAnalysisRequest&>` template instances (lengths 29/32/33/42/44/53 — the differing `[N]` are the differing name-string lengths). Ten names, one vtable.

### Reconciling ~180 steps

The pipeline quotes roughly **180 steps**, above the 150 names, for two reasons:

1. **Repeats.** Some names occupy multiple pipeline positions (e.g. `non_ssa_legalization` early *and* late, `coalesce_multichannel_cc_ops` at two points, `perf_sim` twice). The same registered name is scheduled more than once.
2. **Interposed non-real passes.** The `CheckPassAdder` inserts `!isRealPass` check/dump/sim passes (`Dumper`, `Hasher`, `perf_sim*`, the verifiers, `report_stats`) *between* named steps, padding the step count above the name count.

So the three numbers are: **150** registered names (the registry size), **121** distinct classes (the vtable count), ~**180** pipeline steps (names × positions + interposed checkers).

---

## The full 150-name → class → granularity table

Evidence column (`ev`): `ctor` = direct `backend::Class::Class(PassOptions)` PLT call in the `_M_invoke` body; `mkuq` = `make_unique<Class>`; `vt` = `vtable for backend::Class` installed by the lambda. Granularity: `serial` = added by name (default serial child); a Fork tag is shown **only** where an explicit `add*ParallelPass<Class>` template instance exists, else `serial*` (the call-site may still fork — granularity is per-site, see above). Every row is grounded in factory-body evidence unless noted.

| register name | C++ pass class | granularity | ev |
|---|---|---|---|
| `address_rotation_dram` | `AddressRotation` | ModuleFork | vt |
| `address_rotation_psum` | `AddressRotation` | ModuleFork | vt |
| `address_rotation_psum_post_schedule` | `AddressRotation` | ModuleFork | vt |
| `address_rotation_sb` | `AddressRotation` | ModuleFork | vt |
| `address_tensor_content_dump` | `AddressTensorContentDump` | serial* | ctor |
| `address_tensor_insertion` | `AddressTensorInsertion` | serial* | ctor |
| `alloc_queues` | `AllocQueues` | serial* | vt |
| `alloc_semaphores` | `AllocSemaphores` | serial* | ctor |
| `anti_dependency_analyzer` | `AntiDependencyAnalyzer` | Core/ModuleFork | mkuq |
| `anti_dependency_analyzer_post_shared_dram` | `AntiDependencyAnalyzer` | Core/ModuleFork | mkuq |
| `arch_legalize` | `ArchLegalize` | serial* | vt |
| `assign_hwdge_engine` | `AssignHWDGEEngine` | serial* | vt |
| `assign_trigger_engine` | `AssignTriggerEngine` | serial* | vt |
| `bir_linker` | `BirLinker` | CoreFork (link) | mkuq |
| `bir_racecheck` | `BirRaceCheck` | serial* | ctor |
| `bir_sim` | `BirSim` | SubgraphFork | ctor |
| `bir_sim_with_kernel_inline` | `BirSimWithKernelInline` | SubgraphFork | ctor |
| `bir_splitter` | `BirSplitter` | serial* | ctor |
| `birverifier` | `Verifier` | serial* | vt |
| `branch_hint` | `BranchHint` | serial* | vt |
| `build_fdeps` | `BuildDeps` | serial* | vt |
| `chain_dma_transposes` | `ChainDMATransposes` | serial* | ctor |
| `coalesce_dma_blocks` | `CoalesceDmaBlocks` | serial* | vt |
| `coalesce_multichannel_cc_ops` | `CoalesceMultiChannelCCOps` | serial* | ctor |
| `codegen` | `Codegen` | serial* | ctor |
| `coloring_allocator_dram` | `ColoringAllocator` (DRAM) | serial* | ctor |
| `coloring_allocator_dram_debug` | `ColoringAllocator` (DRAM, debug) | serial* | ctor |
| `coloring_allocator_dram_post_lnk` | `ColoringAllocator` (DRAM, post-link) | serial* | ctor |
| `coloring_allocator_dram_shared` | `ColoringAllocator` (DRAM, shared) | serial* | ctor |
| `coloring_allocator_dram_shared_post_lnk` | `ColoringAllocator` (DRAM, shared, plnk) | serial* | ctor |
| `coloring_allocator_psum` | `ColoringAllocator` (PSUM) | serial* | ctor |
| `coloring_allocator_reg` | `ColoringAllocator` (REG) | serial* | ctor |
| `coloring_allocator_sb` | `ColoringAllocator` (SB) | serial* | ctor |
| `coloring_allocator_with_loop` | `ColoringAllocatorWithLoop` | serial* | ctor |
| `constant_propagate` | `ConstantPropagate` | serial* | ctor |
| `dead_code_elim_o0` | `DeadCodeElim` (o0) | serial* | vt |
| `dead_code_elim_o1` | `DeadCodeElim` (o1) | serial* | vt |
| `debug_dve_nan` | `DebugDveNan` | serial* | vt |
| `debug_init_mem` | `DebugInitMem` | serial* | vt |
| `debug_onchip` | `DebugOnchip` | serial* | vt |
| `dep_opt` | `DepOpt` | serial* | vt |
| `dep_reduction` | `DepReduction` | serial* | ctor |
| `dma_metrics` | `DMAMetrics` | serial* | vt |
| `dma_optimization_psum` | `DMAOptimization` (PSUM) | serial* | ctor |
| `dma_optimization_sb` | `DMAOptimization` (SB) | serial* | ctor |
| `do_nothing` | `DoNothing` (no-op) | serial* | ctor |
| `dumper` | `Dumper` | serial (!real) | ctor |
| `dynamic_dma_cleanup` | `DynamicDMACleanUp` | serial* | vt |
| `dynamic_dma_scan` | `DynamicDMAScan` | serial* | vt |
| `dynamic_dma_setup` | `DynamicDMASetup` | serial* | vt |
| `early_peephole_opts` | `EarlyPeepholeOpts` | serial* | vt |
| `error_injector` | `ErrorInjector` | serial* | ctor |
| `expand_all_engine_final_pre_codegen` | `ExpandAllEngineFinalPreCodegen` | serial* | vt |
| `expand_all_engine_pre_alloc_sema_and_reg` | `ExpandAllEnginePreAllocSemaAndReg` | serial* | vt |
| `expand_device_print` | `ExpandDevicePrint` | serial* | vt |
| `expand_inst_late` | `ExpandInstLate` | serial* | vt |
| `expand_replication` | `ExpandReplication` | serial* | vt |
| `expand_scheduling_units` | `ExpandSchedulingUnits` | serial* | ctor |
| `extend_shared_lifetimes` | `ExtendSharedLifetimes` | serial* | ctor |
| `flatten_small_loops` | `FlattenSmallLoops` | serial* | vt |
| `full_unroll_all` | `FullUnroll` (all) | serial* | ctor |
| `full_unroll_memloc_generation` | `FullUnroll` (memloc-gen) | serial* | ctor |
| `full_unroll_some` | `FullUnroll` (some) | serial* | ctor |
| `hasher` | `Hasher` | serial (!real) | ctor |
| `hbm_usage` | `HBMUsage` | serial* | vt |
| `heuristic_unroll` | `HeuristicUnroll` | serial* | vt |
| `infer_stream_ids` | `InferStreamIds` | serial* | ctor |
| `inline_bir_kernel` | `InlineBIRKernel` | serial* | ctor |
| `inline_nki_kernel` | `InlineNKIKernel` | ModuleFork | vt |
| `input_dma_coalescing` | `DMAOptimization` (input-coalesce) | serial* | ctor |
| `insert_dma_switch_queue_instance` | `InsertDMASwitchQueueInstance` | serial* | vt |
| `insert_ptcom_flat` | `InsertPTCOMFlat` | serial* | vt |
| `instruction_reorder` | `InstructionReorder` | serial* | vt |
| `label_dma_qos` | `LabelDMAQoS` | serial* | vt |
| `legalize_cce_dma` | `LegalizeCCEDMA` | serial* | vt |
| `legalize_mm_accumulation_groups` | `LegalizeMatmulAccumulationGroups` | serial* | ctor |
| `legalize_strided_dma` | `LegalizeStridedDMA` | serial* | vt |
| `linear_scan_allocator` | `LinearScanAllocator` | serial* | ctor |
| `lnc_barriercheck` | `LncBarrierCheck` | serial* | ctor |
| `lnc_splitter` | `LncSplitter` | serial* | ctor |
| `lnc_verifier` | `LncVerifier` | serial* | ctor |
| `localize_shared_memory` | `LocalizeSharedMemory` | serial* | ctor |
| `loop_optimization` | `LoopOptimization` | serial* | ctor |
| `lower_ac` | `LowerAC` | serial* | ctor |
| `lower_act` | `LowerAct` | serial* | vt |
| `lower_ap` | `LowerAP` | serial* | ctor |
| `lower_branch` | `LowerBranch` | serial* | vt |
| `lower_control` | `LowerControl` | serial* | vt |
| `lower_dma` | `LowerDMA` | serial* | vt |
| `lower_dve` | `LowerDVE` | serial* | ctor |
| `lower_dynamic_dma` | `LowerDynamicDMA` | serial* | vt |
| `lower_generic_indirect` | `LowerGenericIndirect` | serial* | vt |
| `lower_local_collectives` | `LowerLocalCollectives` | serial* | ctor |
| `lower_select` | `LowerSelectPass` | serial* | vt |
| `lower_symbolic_inst` | `LowerSymbolicInst` | serial* | ctor |
| `lower_sync` | `LowerSync` | serial* | vt |
| `mem2reg` | `Mem2Reg` | serial* | vt |
| `memory_analysis_after_address_rotation_dram` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_address_rotation_psum` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_address_rotation_sb` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_coloring_allocator_dram_post_lnk` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_coloring_allocator_dram_shared` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_coloring_allocator_sb` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_dma_optimization_sb` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_post_sched` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_pre_sched` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `memory_analysis_after_unroll` | `MemoryAnalysis` | Module/CoreFork | ctor |
| `neff_packager` | `NeffPackager` | serial* | vt |
| `non_ssa_legalization` | `NonSSALeg` | serial* | vt |
| `oom_checker` | `OOMChecker` | serial* | ctor |
| `optimize_act_control` | `OptimizeActControl` | serial* | vt |
| `optimize_prefetch_act_control` | `OptimizePrefetchActLoad` | serial* | vt |
| `optimize_queue_switch` | `OptimizeQueueSwitch` | serial* | vt |
| `order_column_tiled_mms` | `OrderColumnTiledMMs` | serial* | ctor |
| `order_constraints` | `OrderConstraintsPass` | serial* | ctor |
| `partitioner` | `Partitioner` | serial* | ctor |
| `peephole_opts` | `PeepholeOpts` | serial* | vt |
| `perf_sim` | `PerfSimPass` | serial (!real) | ctor |
| `perf_sim_at_end` | `PerfSimPass` | serial (!real) | ctor |
| `perf_sim_package_pass` | `PerfSimPackagePass` | serial (!real) | ctor |
| `post_sched` | `PostSched` | serial* | vt |
| `pre_opts` | `PreOpts` | ModuleFork | vt |
| `pre_sched` | `PreSched` | serial* | vt |
| `prefetch_scheduling_after_sched` | `PrefetchScheudling` (after) | serial* | vt |
| `prefetch_scheduling_before_sched` | `PrefetchScheudling` (before) | serial* | vt |
| `psum_legalization` | `PSUMLegalization` | serial* | ctor |
| `remat_optimization` | `RematOpt` | serial* | vt |
| `remove_redundancies` | `RemoveRedundancies` | serial* | vt |
| `report_stats` | `ReportStats` | serial (!real) | vt |
| `runtime_memory_reservation` | `RuntimeMemoryReservation` | serial* | vt |
| `sb_size_legalization` | `SBSizeLegalization` | serial* | ctor |
| `separate_load_and_compute` | `SeparateLoadAndCompute` | serial* | ctor |
| `separate_load_and_compute_post_ada` | `SeparateLoadAndCompute` (post-ada) | serial* | ctor |
| `separate_load_and_compute_with_memset` | `SeparateLoadAndCompute` (w/memset) | serial* | ctor |
| `seq_inst_opt` | `SeqInstOpt` | serial* | vt |
| `shared_mem_cb_insertion` | `SharedMemCBInsertion` | serial* | vt |
| `shrink_ml` | `ShrinkDN` | serial* | vt |
| `sync_before_global_cc` | `SyncBeforeGlobalCC` | serial* | ctor |
| `sync_shared_allocations` | `SyncSharedAllocations` | serial* | ctor |
| `synchronizer` | `SynchronizerPass` | serial* | vt |
| `tensor_copy_elim` | `TensorCopyElim` | serial* | vt |
| `tensorcopy_accel` | `TensorCopyAccel` | serial* | ctor |
| `translate_nki_ast_to_bir` | `TranslateNKIASTToBIR` | serial* | vt |
| `unroll` | `Unroll` | serial* | ctor |
| `value_numbering` | `ValueNumbering` | serial* | vt |
| `verify_bir_serdes` | `verifyBirSerDes` | serial (!real) | vt |
| `vn_splitter` | `VNSplitterPass` | serial* | vt |
| `vnc_link` | `VncLink` | serial* | ctor |
| `vnc_remote_addr_map` | `VncRemoteAddrMap` | serial* | ctor |

**Total: 150 register names · 121 distinct C++ pass classes.** The `serial*` tag means added via `addPass("<name>")` by default; the same class may be fork-wrapped at a different call-site. A fork tag appears only where an explicit `add*ParallelPass<Class>` template instance exists in the symtab.

---

## Evidence summary

**Read directly off the binary.** The name count is a one-liner: `nm -DC | rg -o 'register_generator_[a-z0-9_]+' | sort -u | wc -l` gives **150**. The three `run` overloads are all symbols — `run(vector<Module>&)` @ `0x1736d40`, the per-leaf `run(bir::Module&)` (e.g. `Unroll::run(bir::Module&)` @ `0xb42f30`), and `runSubgraph` (e.g. `BirSim::runSubgraph` @ `0x1109440`) — and the dispatch `mov (%r15),%rax ; call *0x10(%rax)` sits at `0x1736d70`. The pass-kind taxonomy rests on `ContainerPass::getKind` @ `0x1737480` plus the three `classof` symbols @ `0x17374d0` / `0x1737490` / `0x17374b0`; `DapPass` carries `BackendPass`-only `run` overloads and no `classof`, which is what places it outside the `ContainerPass` spine. For the registry, all five API symbols are present, the `unroll` factory body at `0xb46400` shows `_Znwm@plt` followed by `Unroll::Unroll(PassOptions const&)@plt`, and the value type `function<unique_ptr<BackendPass>(PassOptions const&)>` appears verbatim in the `_M_rehash` symbol called from `run`.

**Reconstructed rather than read.** The **121** distinct-class total is a count of distinct ctor targets over the table above — it is derived, not independently grep-able, unlike the 150. The multi-registration arithmetic that reconciles the two was enumerated group by group: `MemoryAnalysis` under 10 names, `ColoringAllocator` under 8 of the 9 `coloring_allocator_*` names (the ninth, `coloring_allocator_with_loop`, is the distinct `ColoringAllocatorWithLoop` class), `AddressRotation` under 4, `DeadCodeElim` under 2; the surplus sums to 29 and `121 + 29 = 150`. The TBB fork-wrapping shape and the `dry_run` measurement role are likewise reconstructed — the `parallel_for` calls and the `dry_run` symbol are real, the surrounding structure is not byte-exact. The RTTI class-set context (178 backend typeinfos, of which 121 are registry-reachable) comes from the RTTI table.

### Limits of this reading

The vtable slot ordering beyond `+0x10` is **INFERRED**. RELR-packed pointers read as zero in the static image, so only the dispatched-hook slot at `+0x10` — the one `BackendPass::run`'s disassembly exercises directly — is actually pinned.

---

## Cross-references

- [3.7 — `walrus_driver` Backend CLI & Pass Vocabulary](../frontend/walrus-driver-cli.md) — the `--pass` / `--skip-pass` / `--start-pass` vocabulary that draws from these 150 names, and `hasGenerator` as the validity gate.
- 8.2 — `walrus/pass-pipeline-optlevels.md` (planned) — the optlevel pipeline builders that call `addPass("<name>")` and the `add*ParallelPass` granularity assignment per optlevel.
- Part 14 pass catalog (`appendix/`, planned) — the per-pass H-strand documentation for each of the 121 classes.
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where the single `WalrusDriver` Job sits in the process pipeline.
