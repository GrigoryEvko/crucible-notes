# Cross-Core Shared Lifetimes and Address Sync

> *All addresses on this page apply to `libwalrus.so` from neuronx_cc 2.24.5133.0+58f8de22 (BuildID `92b4d331…`), ELF64 x86-64. The `.symtab` is stripped; member names below come from the retained `.dynsym` and are recoverable by address. Other versions will differ.*

## Abstract

When a model is split across `N` LNC cores, a subset of DRAM tensors must behave as **one physical buffer touched by several cores** — a value produced on core 0 and consumed on core 1 cannot be allocated, recolored, or overwritten as if it lived and died inside a single core. Two walrus passes, run on opposite sides of the shared-DRAM coloring allocator, make that work. `extend_shared_lifetimes` (pass order 81) runs *before* the allocator and **pins the cross-core live range**: it computes, per shared location, the union over all cores of `[first-producer-block, last-consumer-block]` and brackets that span with a pair of `CoreBarrier` (IT87) instructions, so the order-82 allocator sees the buffer as continuously live and never reuses its storage between core 0's write and core 1's read. `sync_shared_allocations` (pass order 83) runs *after* the allocator and **pins the physical address**: each core was colored independently (each core is a separate `bir::Module` after the LNC split), so a remote "view" of a shared buffer still carries no address. The pass follows each shared location's `RemoteLocalTarget` link back to the producing core's now-allocated location and copies its `(address, bankId, basePartition)` triple onto the local view, so every core resolves the same logical tensor to the same physical DRAM bytes.

The two passes are frequently conflated; they are not the same mechanism. **Extend** inserts barriers and touches no addresses. **Sync** copies addresses and inserts no barriers. The barrier brackets are a *lifetime* (and ordering) device that the static race checker later verifies; the address copy is an *identity* device that makes every core's name for a shared tensor resolve to one place. This page documents both, anchored to the disassembled worker bodies. It builds on [the DRAM allocator page](dram-allocator.md) (8.20), which already established `sync_shared_allocations @0x16b2da0` and the `setIsSharedPostDRAMAlloc` mark; here that pass is opened to the instruction level, and its upstream lifetime-extension sibling is documented for the first time.

The cross-core liveness join and the CoreBarrier bracketing are the headline. The address-consistency copy is the necessary post-allocation fixup that makes the bracket point at real memory.

For reimplementation, the contract is:

- The **two-overload dispatch shape** every cross-core walrus pass uses: a `run(bir::Module&)` single-module stub that is a near-no-op, and a `run(vector<unique_ptr<Module>>&)` real worker that owns all `N` per-core modules — because cross-core reasoning needs the whole module vector at once.
- The **lnc gate**: both passes early-exit when `PassOptions+0x1A4 == 1` (single core ⇒ nothing is genuinely shared).
- The **cross-core liveness join**: SPMD-identity validation, per-core fixpoint liveness over shared mlocs, the first-producer/last-consumer union across cores, and the realization of that union as a `cb_sg<n>_start … cb_sg<n>_end` CoreBarrier bracket.
- The **address-consistency copy**: the `RemoteLocalTarget` resolution and the exact `getAddress / getBankId / getBasePartition → allocate` copy chain.

| | |
|---|---|
| **Binary** | `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331a42d7e80bb839e03218d2b9b0c23c346`) |
| **Pass 1 — extend** | `ExtendSharedLifetimes::run(Module&)` `@0x162dbe0` (stub); real `run(vector<Module>&)` `@0x163f910` |
| **Pass 2 — sync** | `SyncSharedAllocations::run(Module&)` `@0x16b19b0` (stub); real `run(vector<Module>&)` `@0x16b2da0` |
| **lnc gate** | `cmpl $0x1,0x1a4(%rax)` at `0x163f92a` (extend) / `0x16b2dd9` (sync) |
| **Pass orders** | 81 (extend) and 83 (sync) — see [pass pipeline](pass-pipeline-optlevels.md) |
| **IR level** | post-LNC-split `bir` modules, post-`lower_local_collectives`; shared DRAM |
| **Barrier op** | `bir::InstCoreBarrier` = IT87 (all-cores rendezvous) |

## Pipeline Placement

### Purpose

The two passes only make sense as the bookends of the shared-DRAM allocation cluster. The order is the whole argument: a remote core's view of a shared buffer has to be made *live across the whole cross-core span* before addresses are assigned (or the allocator reuses the storage too early), and it has to be made *physically identical* after addresses are assigned (or the two cores' names point at different DRAM).

### Entry Point

```text
walrus shared-DRAM cluster (pass orders 79-84)
  79  anti_dependency_analyzer_post_shared_dram   ── WAR/WAW edges over shared DRAM
  80  lower_local_collectives                     ── mints RemoteLocalTarget links
  81  extend_shared_lifetimes        <── PASS 1   ── pins cross-core live range (barriers)
  82  coloring_allocator_dram_shared              ── assigns concrete DRAM addresses
  83  sync_shared_allocations        <── PASS 2   ── pins cross-core physical address
  84  anti_dependency_analyzer_post_shared_dram   ── re-run after addresses settle
```

The dependency on order 80 is direct: `lower_local_collectives` is what *creates* the cross-core sharing. For each shared DRAM location a remote core must reach, it sets a `RemoteLocalTarget` = `optional<pair<remote_mloc_name, remote_ncId>>` (`bir::MemoryLocation::setRemoteLocalTarget(optional<pair<string,uint>>)`). Extend (81) consumes that to find what is genuinely shared; sync (83) consumes it to find where the real address lives; `getRemoteLocalTarget` is the call at `0x16b330b` (see §Sync).

> **NOTE —** before this cluster, a separate pass (`localize_shared_memory`) has already converted Shared→Local for buffers that *only* one core touches. So everything reaching extend/sync with the shared flag still set is a *genuine* multi-core buffer. This is why both passes can treat `lnc == 1` as "nothing shared" and skip wholesale.

### Considerations

The two passes are physically separate `BackendPass` registrations (object sizes `0x80` for extend, `0x68` for sync) with their own pass-name strings (`"extend_shared_lifetimes"`, `"sync_shared_allocations"`, both length 23). They are not phases of one pass — order-82 allocation runs *between* them, which is exactly why two passes exist rather than one.

---

## The Two-Overload Dispatch Shape

### Purpose

Both passes inherit the same shim/real split that [`lnc_splitter`](dram-allocator.md) and the other post-split passes use: the `BackendPass` virtual is `run(bir::Module&)`, but the real work needs *all* per-core modules at once, so the single-module overload is a stub and the cross-core worker lives in the `run(vector<unique_ptr<bir::Module>>&)` overload.

### Algorithm

```c
// ExtendSharedLifetimes::run(bir::Module&)        // sub_0x162dbe0 (stub, ~107 bytes)
// SyncSharedAllocations::run(bir::Module&)         // sub_0x16b19b0 (stub, ~107 bytes)
function run_single_module_stub(this, module):
    this[0] = 2                      // status enum = "skipped/ok" — same const in both stubs
    seed_empty_string()
    call_internal_named_object_helper()
    return this                      // NO module mutation
```

The pass manager owns the `vector<Module>` for a post-split program and routes the lnc>1 work to the vector overload; the single-Module entry is the skip path. Both stub bodies are trivial and set the same `[this+0]=2` constant; the routing itself is **INFERRED** from the worker living in the vector overload.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ExtendSharedLifetimes::run(Module&)` | `0x162dbe0` | single-module stub | CERTAIN |
| `ExtendSharedLifetimes::run(vector<Module>&)` | `0x163f910` | real cross-core worker | CERTAIN |
| `SyncSharedAllocations::run(Module&)` | `0x16b19b0` | single-module stub | CERTAIN |
| `SyncSharedAllocations::run(vector<Module>&)` | `0x16b2da0` | real cross-core worker | CERTAIN |

---

## extend_shared_lifetimes — The Cross-Core Liveness Join

### Purpose

`extend_shared_lifetimes` makes a shared buffer's live range span **every core that touches it**. A buffer written on core 0 and read on core 1 must stay live on core 0 *until core 1's read* — otherwise the order-82 coloring allocator, reasoning per core, would see core 0's value die at its last local use and hand the storage to something else before core 1 reads it. The pass computes the cross-core union live range and brackets it with CoreBarriers so the allocator (and, later, the race checker) treat the buffer as live for the whole span.

### Entry Point

```text
ExtendSharedLifetimes::run(vector<Module>&)        0x163f910  (real worker, ~13 KB)
  ├─ lnc gate: cmpl $0x1,0x1a4(%rax); je 0x1640200            // single-core skip
  ├─ getAllCores()                                  0x162f910  // {0..lnc-1}
  ├─ getSharedLiveInLiveOut(fns, cfgs, ncId)        0x16367e0  // fixpoint liveness
  ├─ getSharedMultiBBAllocs(BB, fns, ncId)          0x1634ce0  // multi-BB shared allocs
  ├─ scanBasicBlocks(...)                           0x162fc70  // who accesses which mloc
  ├─ extendSharedLifetimesInBlock(ncId, names, blocks) 0x16396c0  // realize the union
  │    ├─ insertCoreBarrierAtStart(cbVec, ncId, blocks)  0x1631a80  // cb_sg<n>_start (IT87)
  │    └─ insertCoreBarrierAtEnd(instVec, ncId, blocks)  0x1632880  // cb_sg<n>_end   (IT87)
  └─ renumber core barriers                                    // dense per-engine indices
```

### Algorithm

```c
function ExtendSharedLifetimes_run(this, opts, modules):    // sub_0x163f910
    if opts[0x1A4] == 1:                       // 0x163f92a: cmpl $0x1,0x1a4(%rax)
        return                                 // 0x163f93b: je 0x1640200 — single core
    sgId  = getSubgraphId(*modules[0])         // read at 0x163f94a; names the cb_sg group
    cores = getAllCores()                      // {0..lnc-1}  (sub_0x162f910)

    // --- Phase A: SPMD-identity validation ---
    // LncSplitter clones the program per core, so all cores share one function keyspace.
    functionNames = [f.name for f in modules[0].functions]
    for ncId in cores:
        assert len(functionNames) == modules[ncId].num_functions()   // cpp:88
        for name in functionNames:
            assert modules[ncId].getFunctionByName(name) != null      // cpp:92 (sub_0x61ed40)

    // --- Phase B: per-core fixpoint liveness over SHARED mlocs ---
    for ncId in cores:
        cfgs = build_cfgs(modules[ncId].functions)
        sharedLiveInfo[ncId] = getSharedLiveInLiveOut(functions, cfgs, ncId)  // sub_0x16367e0
        // a producer must dominate every consumer: the entry block cannot have a
        // shared value already live-in.
        assert sharedLiveInfo[entry(cfgs[0])].liveIn.empty()          // cpp:115

        // --- Phase C: collect multi-BB shared allocs + per-core access map ---
        for bb in modules[ncId].blocks:
            multiBB[ncId] += getSharedMultiBBAllocs(bb, functions, ncId)  // sub_0x1634ce0
        scanBasicBlocks(...)   // sub_0x162fc70 — fill map<mloc-name, vector<MlocInfo>>
                               // MlocInfo = {which blocks/insts touch it, which are firstDefs}

    // --- Phase D: THE CROSS-CORE UNION (the "extend") ---
    for mloc in sharedMlocs:
        startBlk = min over cores of first-def-block(mloc)    // first producer, ALL cores
        endBlk   = max over cores of last-access-block(mloc)  // last consumer, ALL cores
        // the single live interval now spans first-producer..last-consumer across the
        // whole LNC group; the order-82 allocator will not reuse the storage inside it.

        // --- Phase E: realize the union as a CoreBarrier bracket on every core ---
        for ncId in cores:
            extendSharedLifetimesInBlock(ncId, spanningBlocks(mloc), blocks):  // sub_0x16396c0
                insertCoreBarrierAtStart(cbVec,   ncId, blocks)   // cb_sg<n>_start
                insertCoreBarrierAtEnd  (instVec, ncId, blocks)   // cb_sg<n>_end
        assert cfgs[ncId].blocks.size() == numBlocks             // cpp:162 (insertion adds
                                                                 // insts, not blocks)
    renumberCoreBarriers()   // "Renumbering core barriers." — dense per-engine index space
```

### The Union — Phase D

The cross-core union is the conceptual core. Per shared mloc, the pass takes the *earliest* producer block across all cores and the *latest* consumer block across all cores, and treats the buffer as live across that whole span on every core. The disassembled worker drives this off `getAllCores()` (the full `{0..lnc-1}` set) and the three log strings that name the merge inputs:

```text
"Block spanning mlocs (all cores)"       // 0x1d82d10 — the union of spanning blocks
"First def blocks for mlocs (per core)"  // 0x1d82d38 — per-core producer set
"All accesses for mlocs (per core)"      // 0x1d82d60 — per-core access set
```

The `(all cores)` vs `(per core)` pairing is the tell: per-core sets are merged into one `(all cores)` span. This is what prevents the order-82 DRAM coloring allocator from reusing the storage between, say, core-0's write and core-1's read. The union semantics rest on that string trio plus the `getAllCores`-driven loop; the set-merge code inside the ~25 KB `extendSharedLifetimesInBlock` body is not traced instruction-by-instruction.

### The CoreBarrier Bracket — Phase E

The extension is *materialized* as a pair of `bir::InstCoreBarrier` ops. `extendSharedLifetimesInBlock @0x16396c0`'s only `ExtendSharedLifetimes`-member calls are `insertCoreBarrierAtStart` and `insertCoreBarrierAtEnd` — confirmed as the only backend-member calls in that body's range. Each emits an IT87 CoreBarrier:

```c
function insertCoreBarrierAtStart(this, cbVec, ncId, blocks):   // sub_0x1631a80
    getAllCores()                          // 0x1631b72 — barrier applies to ALL cores
    name = "cb_sg" + str(sgId) + "_start"  // strings: "cb_sg" 0x1c88642, "_start"
    cb   = new bir::InstCoreBarrier(...)
    NamedObjectContainer<BasicBlock,Instruction>
        ::insertElement<InstCoreBarrier>(cb)  // at a checkInsertionPointValid() block START
    log("Inserted new CB %s", name)        // 0x1c88648
    cbVec.push_back(cb)

// insertCoreBarrierAtEnd  (sub_0x1632880) is symmetric: suffix "_end", block END.
```

A `CoreBarrier` is IT87 — an all-cores rendezvous: no core may pass `cb_sg<n>_end` until every core has reached it. Bracketing the cross-core live range with `_start … _end` therefore imposes a happens-before fence that both the simulator and the static checker honor, and guarantees the shared buffer is provably live (and untouchable by allocator reuse) for the whole span. After insertion the pass renumbers CoreBarrier indices so each engine's CoreBarriers own a dense sequential index space (string `"Renumbering core barriers."` `@0x1c8895c`), which the downstream race checker consumes.

> **QUIRK —** the barrier is named `cb_sg<id>` where `id` is the **subgraph id**, not a core id or a buffer name. `bir::getSubgraphId(Module const&)` is read at the run-prologue (`0x163f94a`) and the name keys on it to keep `cb_sg` names unique per shared group. The `_start`/`_end` suffix distinguishes the two ends of one bracket. A reimplementation that keys the barrier name on the buffer or core will collide names across subgraphs. *(The exact concatenation is **SPECULATIVE** — `getSubgraphId` is read at the prologue, but the name assembly is not traced past the `cb_sg`/`_start` string fetches.)*

### getAllCores — `{0..lnc-1}`

```c
function getAllCores(this, opts):              // sub_0x162f910
    n = *(uint*)(opts + 0x1A4)                 // lnc
    if n == 0: return empty
    for i in 0 .. n-1:                         // loop @0x162f9f0: store i, end += 4
        result.push_back(i)
    return result                              // {0, 1, ..., lnc-1}
```

This is the *full* core set, as opposed to `LowerLocalCollectives::getRemoteCores` (= `{0..lnc-1}\{self}`). The barrier and the union both span all cores, so `extend` uses the full set. Both readers take `lnc` from `PassOptions+0x1A4`, the same offset as the lnc gate.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `run(vector<Module>&)` | `0x163f910` | the real extend worker | CERTAIN |
| `getAllCores()` | `0x162f910` | `{0..lnc-1}` core set | CERTAIN |
| `getSharedLiveInLiveOut(...)` | `0x16367e0` | per-core fixpoint liveness over shared mlocs | CERTAIN (symbol + strings) |
| `getSharedMultiBBAllocs(BB,fns,i)` | `0x1634ce0` | collect allocs spanning >1 BB on a core | CERTAIN (symbol) |
| `scanBasicBlocks(...)` | `0x162fc70` | per-block access map (`map<name,vector<MlocInfo>>`) | CERTAIN (symbol) |
| `extendSharedLifetimesInBlock(i,names,blocks)` | `0x16396c0` | orchestrate barrier insertion | CERTAIN (symbol) |
| `insertCoreBarrierAtStart(cbVec,i,blocks)` | `0x1631a80` | emit `cb_sg<n>_start` (IT87) | CERTAIN |
| `insertCoreBarrierAtEnd(insts,i,blocks)` | `0x1632880` | emit `cb_sg<n>_end` (IT87) | CERTAIN |
| `populateAPFromMemLoc(PAP&,Mloc*,bool)` | `0x16394b0` | build PhysicalAccessPattern for an mloc | CERTAIN (symbol) |
| `bir::getSubgraphId(Module const&)` | imported | subgraph id for `cb_sg` naming | CERTAIN (import) |

### Considerations

The fixpoint liveness (`getSharedLiveInLiveOut`) is a classic iterative dataflow restricted to *shared* mlocs; its log strings enumerate the steps (`"Build Shared live-in/live-out with <N> iterations"`, `"Propagating <m> to newLiveIn for <bb>"`, `"Removing <m> from newLiveOut, because it is not defined here"`). The `cpp:115` assert — entry block has no shared live-in — is the dominance precondition: a cross-core buffer cannot already be live at function entry, because some block must produce it. A reimplementation that allows shared live-in will mis-bracket — the `_start` barrier would land after a value that is already live. (The `cpp:115` assert string sits at `0x1d82cd0`.)

---

## sync_shared_allocations — The Address-Consistency Fixup

### Purpose

After the order-82 coloring allocator gives the *producing* core's location a concrete DRAM address, every *other* core's view of that same buffer (a `RemoteLocalTarget` link, not a real allocation) still has no address. `sync_shared_allocations` walks each core's shared locations, follows the `RemoteLocalTarget` back to the producing core's now-allocated location, and copies its `(address, bankId, basePartition)` onto the local view — so every core's name for the shared tensor resolves to the same physical DRAM. This re-confirms and details the finding in [the DRAM allocator page](dram-allocator.md): it inserts **no** barriers and **no** semaphores; it is purely an allocation fixup.

### Entry Point

```text
SyncSharedAllocations::run(vector<Module>&)        0x16b2da0  (real worker, ~8.6 KB)
  └─ lnc gate: cmpl $0x1,0x1a4(%rax); je 0x16b414b           // single-core skip
     per Module: filter allocs to shared (Function::allocs() shared predicate)
       per shared mloc m on core C:  (0x16b32f8 .. 0x16b340c)
         isSharedPostDRAMAlloc → getRemoteLocalTarget → getFunctionByName
           → getMemoryLocationByName → isAllocated → copy (addr,bank,part) → allocate
```

### Algorithm

```c
function SyncSharedAllocations_run(this, opts, modules):   // sub_0x16b2da0
    if opts[0x1A4] == 1:                       // 0x16b2dd9: cmpl $0x1,0x1a4(%rax)
        return                                 // single core — nothing shared
    for ncId, mod in enumerate(modules):
        for fn in mod.functions:
            for m in fn.allocs():
                // 0x16b32fb: skip non-shared
                if !m.isSharedPostDRAMAlloc(): continue        // @plt 0x6138e0
                rlt = m.getRemoteLocalTarget()                 // @plt 0x5fab60 (0x16b330b)
                assert rlt.has_value()                         // cpp:29 (cmpb $0,(%rax))
                (remote_name, remote_ncId) = rlt.value()       // rlt+0x8/+0x10 name; rlt+0x28 ncId
                rfn    = modules[remote_ncId].getFunctionByName(fn.name)  // @plt 0x61ed40
                remote = rfn.getMemoryLocationByName(remote_name)         // @plt 0x5f9a90
                assert remote != null                          // cpp:33 (test %rax,%rax)
                assert remote.isAllocated()                    // cpp:34 (cmpb $0,0xa8(%rax))

                // --- copy remote's PHYSICAL placement onto the local view m ---
                m.setAllocated(true)                           // @plt 0x60fe50
                bp   = remote.getBasePartition()  -> r9d       // @plt 0x6046b0
                bank = remote.getBankId()         -> r8        // @plt 0x629620
                addr = remote.getAddress()        -> rcx       // @plt 0x600830
                m.allocate(                                    // @plt 0x5f92f0
                    /* MemoryType         */ *(uint*)(m + 0xD8),  // m keeps its OWN type (DRAM=8)
                    /* MemoryAddressSpace */ *(uint*)(m + 0xF0),  // and its OWN addr-space (Shared=1)
                    /* address (ulong)    */ addr,                // COPIES remote address
                    /* (long)             */ 0,
                    /* bankId (uint)      */ bank,
                    /* basePartition      */ bp,
                    /* (bool)             */ false,
                    /* APs                */ null)
                log("Assigned memloc %s to address %#x based on its remote local "
                    "target %s", m.name, addr, remote_name)    // 0x1c8940d / 0x1d88040
```

### The Copy Chain

The copy chain is the byte-confirmed core of the pass (disassembled `0x16b32f8..0x16b340c`). The argument sourcing in the final `allocate` call is exact: `getAddress`'s return is moved into `rcx`, `m+0xF0` (the local view's own MemoryAddressSpace) into `edx`, `m+0xD8` (its own MemoryType) into `esi`, `rbx` (bank) into `r8`, `r14d` (base partition) into `r9d`. So `m` **keeps its own type and address-space** and **copies only the placement triple** — address, bank, partition — from the remote. The `getRemoteLocalTarget @plt 0x5fab60` is the type `optional<pair<string remote_name, uint remote_ncId>>` minted by `lower_local_collectives`; the remote core id is read from `rlt+0x28` and indexes `modules[]` to reach the producing module. The `allocate` signature is `bir::MemoryLocation::allocate(MemoryType, MemoryAddressSpace, unsigned long, long, unsigned int, bool, vector<AccessPattern const*>*)`, from the imported symbol.

> **NOTE —** the local view is resolved in the *remote* module **by name**, not by index: `getFunctionByName(fn.name)` then `getMemoryLocationByName(remote_name)`. This is sound only because Phase A of `extend` (the SPMD-identity asserts at `cpp:88`/`cpp:92`) guaranteed all cores share one function keyspace and `lower_local_collectives` recorded the exact remote location name. A reimplementation that resolves by allocation index will break the moment two cores order their allocations differently. The call pair sits at `0x16b337e`/`0x16b3389`.

One call in this loop is not sync-private: `0x16b3357` reaches `bir::backend::CFG::getFirstCommonPostdominator<...>`, a shared CFG utility, sitting between the `getRemoteLocalTarget` deref and the `getFunctionByName` resolution. It does not participate in the address copy.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `run(vector<Module>&)` | `0x16b2da0` | the real sync worker | CERTAIN |
| `bir::MemoryLocation::isSharedPostDRAMAlloc()` | imported `@plt 0x6138e0` | shared predicate | CERTAIN |
| `bir::MemoryLocation::getRemoteLocalTarget()` | imported `@plt 0x5fab60` | `optional<pair<name,ncId>>` link | CERTAIN |
| `bir::Module::getFunctionByName(...)` | imported `@plt 0x61ed40` | resolve remote function by name | CERTAIN |
| `bir::Function::getMemoryLocationByName(...)` | imported `@plt 0x5f9a90` | resolve remote mloc by name | CERTAIN |
| `bir::MemoryLocation::getAddress/getBankId/getBasePartition` | `0x600830/0x629620/0x6046b0` | read remote placement | CERTAIN |
| `bir::MemoryLocation::allocate(...)` | imported `@plt 0x5f92f0` | write placement onto local view | CERTAIN |

### Considerations

`isAllocated()` is the byte at `remote+0xA8` (`cmpb $0,0xa8(%rax)` at `0x16b339e`); the `cpp:34` assert that it is set is the explicit precondition that order-82 *did* place the producer side. This is the formal coupling between passes 82 and 83: sync cannot run before the producing allocation exists. The `MemoryType`/`MemoryAddressSpace` retention (`DRAM=8`, `Shared=1`) means the *kind* of memory is intrinsic to each view; only the *placement* is shared. The `0xA8` `isAllocated` byte and the assert string at `0x1d87fa8` are read directly; the `DRAM=8`/`Shared=1` constants are cross-checked against the `MemoryLocation::toString` placement encoding.

---

## Insert-then-Verify — Relationship with the Race Checker

The two passes split work with the dependency machinery: **extend inserts the fence; the race checker verifies it suffices.**

- `extend_shared_lifetimes` (81) inserts the `cb_sg<n>_start`/`_end` `InstCoreBarrier` (IT87) brackets around every cross-core live range, then renumbers core barriers so each CoreBarrier on an engine owns a dense sequential index.
- The dependency passes — `build_fdeps` (RAW) and `anti_dependency_analyzer_post_shared_dram` (WAR/WAW), documented on [the dependence-graph page](dependence-graph.md) — build the cross-core conflict edges over shared DRAM.
- The race checker assigns every instruction an `[earliest, latest]` CoreBarrier-index band per engine and reports a race for a cross-core conflicting pair iff their bands *intersect on every engine* (i.e. no CoreBarrier index separates them). The `barrierIdx → InstCoreBarrier*` map it consumes is exactly the `cb_sg` ops `extend` inserted.

So `extend_shared_lifetimes` is precisely the pass that makes those bands *disjoint*: bracketing producer..consumer with `cb_sg*_start`/`_end` guarantees a CoreBarrier index separates the cross-core producer from the cross-core consumer, and the checker then proves it sufficient. If `extend` under-inserted, the checker would flag a residual cross-core RAW/WAR/WAW; in a correct compile it does not, because the barriers cover exactly the live-range union of Phase D. The checker's band-intersection logic itself lives in a separate barriercheck component.

A `CoreBarrier` is an all-engine/all-core fence, so one bracket conservatively orders *every* cross-core RAW/WAR/WAW that crosses its boundary at once — which is why the checker's test is "some CoreBarrier index lies between" rather than a per-edge semaphore. Fine-grained per-edge ordering inside a single engine is handled separately downstream (`synchronizer`/`lower_sync` event semaphores), orthogonal to this cross-core CoreBarrier path.

---

## Evidence Summary

The five strongest claims and their anchors in `libwalrus.so`:

1. **lnc gate (both passes).** `objdump` at `0x163f92a` and `0x16b2dd9` both hold `cmpl $0x1,0x1a4(%rax)` followed by `je`.
2. **Worker symbol identities.** `.dynsym` resolves `0x163f910` to `ExtendSharedLifetimes::run(vector<unique_ptr<bir::Module>>&)` and `0x16b2da0` to `SyncSharedAllocations::run(vector<unique_ptr<bir::Module>>&)`, with the single-module stubs at `0x162dbe0`/`0x16b19b0`.
3. **sync copy chain.** The disassembly at `0x16b32fb..0x16b3407` walks exactly `isSharedPostDRAMAlloc → getRemoteLocalTarget → getFunctionByName → getMemoryLocationByName → isAllocated(0xA8) → setAllocated → getBasePartition → getBankId → getAddress → allocate`, with `m+0xD8`/`m+0xF0` sourcing the local type/space and `getAddress` in `rcx`.
4. **CoreBarrier insertion.** `insertCoreBarrierAtStart @0x1631a80` calls `getAllCores@plt` (at `0x1631b72`) and the binary carries the `cb_sg`, `Inserted new CB`, and `Renumbering core barriers` strings.
5. **BuildID.** The ELF note carries `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, matching the version pin.

**Ceiling.** The cross-core set-merge that forms the Phase-D union — the code that folds per-core spans into one `(all cores)` span — is not traced through the ~25 KB `extendSharedLifetimesInBlock` body; it rests on the `(all cores)`/`(per core)` string trio plus the `getAllCores`-driven loop. The `cb_sg<id>` name's exact subgraph-id concatenation is **SPECULATIVE** past the string fetches. The `allocate` 4th (`long`) and 7th (`bool`) arguments are passed as `0`/`false` and were not semantically decoded; they do not affect the address-identity result.

## Related Components

| Pass / Component | Relationship |
|---|---|
| `lower_local_collectives` (80) | mints the `RemoteLocalTarget` links both passes consume; `lnc_splitter` upstream creates the per-core modules |
| `coloring_allocator_dram_shared` (82) | assigns the producer-side address that `sync` (83) copies; runs *between* the two passes |
| `anti_dependency_analyzer_post_shared_dram` (79/84) | builds the cross-core WAR/WAW edges the race checker tests against the `cb_sg` barriers |
| barriercheck (verifier) | proves the `cb_sg` brackets `extend` inserted separate every cross-core conflicting pair |

## Cross-References

- [The DRAM Allocator — Interval Tree and Shared-DRAM Partitioning](dram-allocator.md) — 8.20; establishes `setIsSharedPostDRAMAlloc`, the order-82 allocation, and `sync_shared_allocations @0x16b2da0` at the summary level this page details
- [The Dependence Graph — build_fdeps + anti-dependency-analyzer](dependence-graph.md) — builds the cross-core RAW/WAR/WAW edges the `cb_sg` barriers must separate
- [The walrus Pass Pipeline & Optlevel Planes](pass-pipeline-optlevels.md) — the order-79..84 shared-DRAM cluster placement
- `lnc_splitter` (8.30, walrus/) — the per-core module split that creates the cross-core sharing (page in flight; the SPMD-clone it produces is what makes the by-name remote resolution in `sync` sound)
- VNC handling (8.34, walrus/, planned) — the broader vnc_remote_addr_map / cross-core addressing model
