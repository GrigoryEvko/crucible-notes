# The Dependence Graph

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 VAs drift, struct offsets and ordinals are ABI-stable). Both passes live in `neuronxcc/starfish/lib/libwalrus.so`; the edge container and the `addDependency` primitive live in `libBIR.so`. For `.text`/`.rodata` the virtual address equals the file offset; `.data` is offset by `+0x400000`. Treat every address as version-pinned.*

## Abstract

The walrus backend schedules a flat BIR program against a hardware machine with a fixed set of engines and a small, explicitly-managed on-chip memory. Before it can schedule, it must know which instructions order against which — and at this level that question has three answers, the classic Bernstein triple: **flow** (read-after-write, a true data dependence), **anti** (write-after-read, a name/storage dependence), and **output** (write-after-write, also a name dependence). Two passes build these three edge sets. `build_fdeps` (pass 74) builds the flow edges; `anti_dependency_analyzer` (pass 76) builds the anti and output edges. A reduction/optimization pass (75, [8.10](dep-opt-reduction.md)) sits between them. The result is the dependence graph the scheduler and the semaphore-lowering chain consume.

The split is not cosmetic. A flow dependence is intrinsic to the dataflow: a consumer reads the value a producer wrote, so it is visible in SSA form and survives any renaming. `build_fdeps` recovers it per logical tensor — it walks each `MemoryLocation`'s writer/reader use-def lists and links each read to its most recent prior write. Anti and output dependences do **not** exist in SSA: distinct values live in distinct virtual locations, and two writes to "the same place" only collide once an allocator has *colored* two logically-distinct tensors onto the same physical SBUF partition, PSUM bank, or DRAM bin. That reuse aliasing is invisible until PSUM coloring (pass 44) and SBUF coloring (pass 51) plus address rotation have run. This is the central timing fact of the page: **anti/output deps are computed post-allocation because they are an artifact of physical storage reuse, not of the dataflow.**

The two passes therefore use two different overlap primitives. `build_fdeps` groups accesses by `MemoryLocation` identity, so "do these conflict" is trivially "are they the same logical tensor"; only its SBUF/PSUM reduction refinements reach for the exact `bir::doAccessesOverlap` geometry ([8.x](dep-opt-reduction.md), shared with the frontend). `anti_dependency_analyzer` groups by *physical byte interval*: it buckets every physical access's byte footprint into a `boost::icl::split_interval_map` keyed by physical address, and **two accesses conflict iff they land in the same bucket**. The bucketing *is* the overlap test, turning the O(n²) pairwise question into an O(n log n) interval-tree insertion. Both edge sets are written into the same `bir::Instruction` edge containers as `llvm::PointerIntPair`-packed edges and merged by a MAX-of-kind rule.

For reimplementation, the contract is:

- The three TBB edge sets on each instruction and the `EdgePtr` `PointerIntPair` layout — how an edge encodes a target pointer plus a 3-bit `EdgeKind` and merges duplicates by max.
- `build_fdeps`: the per-`MemoryLocation` TBB `parallel_for`, the linear producer→consumer chain, and the SBUF/PSUM reduction/accumulation refinements that are the only callers of `doAccessesOverlap` in the dep passes.
- `anti_dependency_analyzer`: the `split_interval_map` byte-bucketing as the overlap test, the four filter sets, the reverse-program-order per-bucket walk, and the later-→-earlier edge-direction rule.
- Why anti/output run at pass 76, after allocation, and what each rebuild gives the scheduler that the pre-allocation flow graph did not.

| | |
|---|---|
| **Flow builder** | `build_flow_deps::constructBIRGraph(int optlevel, bool addRegDeps)` @ `0x1588990` |
| **Anti/Output builder** | `AntiDependencyAnalyzer::run(Module&)` @ `0x8d26e0` |
| **Edge primitive** | `bir::Instruction::addDependency(Inst*, EdgeKind, bool)` @ `0x2e7640` (libBIR) |
| **Edge encoding** | `llvm::PointerIntPair<Instruction*, 3>` = `(target \| EdgeKind in bits[2:0])` |
| **EdgeKind enum** | `{Invalid0, Ordered1, Anti2, Output3, Flow4}`; stored = MAX of all added |
| **Flow kind** | `Flow=4` (RAW) |
| **Anti/Output kinds** | `Anti=2` (WAR), `Output=3` (WAW) |
| **Overlap primitive (flow)** | `bir::doAccessesOverlap` — only via SBUF/PSUM reduction refinement |
| **Overlap primitive (anti)** | `boost::icl::split_interval_map<uint, vector<shared_ptr<PhysicalAccessPattern>>>` bucketing |
| **Pass order** | 74 `build_fdeps` → 75 `remove_redundancies` → 76 `anti_dependency_analyzer` |
| **Anti precondition** | post PSUM-coloring (44) + SBUF-coloring (51) + address rotation |
| **IR level** | flat BIR, materialized `PhysicalAccessPattern`s (post-unroll) |

---

## The shared edge model

### Purpose

Both passes write into the same place: the three edge containers every `bir::Instruction` carries in its `+0xD0` scheduling block. Understanding that container and the `EdgePtr` packing is a prerequisite for reading either pass, because the only thing the two passes disagree on is *which `EdgeKind` they pack and how they decide the pair*. The container model is documented in full as the BIR sync/dependency model; this section gives the minimum a reimplementer needs to follow the edge-insertion calls below.

### The three sets and the EdgePtr

Each instruction's `+0xD0` scheduling block holds three `tbb::concurrent_unordered_set<EdgePtr>` containers — concurrent because both passes populate them under a TBB `parallel_for`:

| Set | Role | Block offset | Written by |
|---|---|---|---|
| `dependencies` | predecessors (the edges that gate this inst) | `BLK+0x08` | both passes |
| `descendents` | successors (inverse index) | `BLK+0x258` | mirror of `dependencies` |
| `loop_carried_dependencies` | cross-iteration predecessors | `BLK+0x4A8` | both, via the `loop_carried` flag |

An `EdgePtr` is an `llvm::PointerIntPair<bir::Instruction*, 3>`:

```text
bits[63:3]  target bir::Instruction*   (8-aligned pointer field)
bits[2:0]   EdgeKind                   (the 3-bit int field)
```

`EdgeKind` is the numeric enum `{Invalid0, Ordered1, Anti2, Output3, Flow4}`, ordered by `bir::operator<(EdgeKind,EdgeKind)` @ `0x26add0`. A *single* edge between an ordered pair `(A→B)` carries **one** `EdgeKind` — the MAX of every kind ever added between them — not one node per kind. `EdgePtr::addEdge(kind)` @ `0x26ae80` performs `if (kind > (cur & 7)) ptr = kind | (ptr & ~7)`, i.e. it keeps the stronger kind. So if a pair is both a flow and an anti dependence, the stored kind is `Flow` (4 > 2). The read-side duals partition the single dep-list back out: `getFlowDeps` @ `0x10e8e70` keeps targets where `hasEdge(Flow=4)`; `getAntiDeps` @ `0x10e8f30` keeps targets where `(EdgePtr & 7) == 2`.

> **QUIRK —** because `addEdge` merges by max and `Flow=4` is the maximum, a pair that the anti analyzer *also* finds to be a WAR conflict will keep its `Flow` tag, and `getAntiDeps`'s exact `==2` test will silently not return it. The anti edge is not lost — the ordering it implies is already enforced by the stronger flow edge to the *same* predecessor. A reimplementer who stores a separate node per kind will over-count edges and mis-drive the scheduler. *Anchors: `addEdge` @ `0x26ae80` (the max-merge); `getAntiDeps` @ `0x10e8f30` (the exact `==2` test).*

### The insertion primitive

Both passes funnel through one libBIR call:

```c
function addDependency(this, Inst* former, EdgeKind kind, bool loop_carried):   // 0x2e7640
    // PointerIntPair packing guards (verbatim LLVM PointerIntPair.h asserts):
    assert((former & 7) == 0)            // pointer is 8-aligned
    assert((kind & 0xF8) == 0)           // kind fits in 3 bits
    assert(this != former)               // no self-edge
    edge = former | kind                 // pack the EdgePtr
    set  = loop_carried ? this.block+0x4A8 : this.block+0x08
    insert_or_merge(set, edge)           // 0x2e6e90: hash on former's NAME string,
                                         //   addEdge-merge kind on a duplicate hit
```

The set is keyed by the *target's name string* (an `EdgeHash`/`EdgeEqual` over `former.name.data`/`.size`), so duplicate edges to the same-named producer collapse and merge their kind. The convention `A.addDependency(former=B)` means "A waits for B": the edge points from the later instruction to the earlier one. Both passes preserve this — every edge they insert goes from the *consumer/later* access to the *producer/earlier* access. *Anchors: `addDependency` @ `0x2e7640` → `0x2e6e90`.*

---

## `build_fdeps` — the flow (RAW) graph

### Purpose

`build_fdeps` builds the true-data-dependence graph: for every memory location, every read is linked to the write(s) that produced the value it reads, with `EdgeKind=Flow(4)`. It is the SSA-faithful skeleton of the dependence graph — it would be correct even with no allocation, because it works on logical `MemoryLocation` identity, not physical address. It runs at pass 74, after loop unrolling and address materialization, so its writers and readers are concrete `PhysicalAccessPattern`s with resolved `Pattern[]`/offset rather than symbolic forms. On top of the baseline RAW chain it also (re)builds the structural ordering chains the scheduler needs: loop-body barriers, RNG-state serialization, and accumulator-group ordering.

### Entry Point

```text
build_flow_deps::constructBIRGraph(optlevel, addRegDeps)   @0x1588990   ── pass body
  ├─ tbb::parallel_for over MemoryLocationSet               (lambda#2)
  └─ tbb::parallel_for over DenseSet<MemoryLocation*>        sub_1587710  ── per-memloc RAW dispatch
       ├─ build_flow_deps::make_linear_dependencies(readers, writers)  @0x157ca80  ── baseline chain (ALL types)
       ├─ build_flow_deps::make_reduction_sequence(MemoryLocation*)    @0x1583db0  ── SBUF(16) refinement
       └─ build_flow_deps::make_accumulation_sequence(MemoryLocation*) @0x15876f0  ── PSUM(32) refinement
  ├─ (structural) loop-body / RNG / accumulator-group chains  (inline in constructBIRGraph)
  └─ build_flow_deps::add_register_dependencies(Function&)    @0x1579650  ── optional, gated on addRegDeps
```

### Algorithm — the pass body

```c
function constructBIRGraph(this, int optlevel, bool addRegDeps):        // 0x1588990
    log("Start build fdeps. Invocation: " + ctime())                    // string @ body
    for each Function F in this.module.functions:                       // module* at this+0x18
        // (a) SPILL GUARD — a per-function bool attribute (ToBoolVisitor)
        if F.too_many_spills:
            draw_trivial_deps(F)                                        // program-order chain only
            log("Warning: too many spills, skip build flow dependencies")
            continue
        get_inst_order(F)                                              // stamp program indices

        // (b) PER-MEMORY-LOCATION FLOW BUILD
        memlocs = DenseSet<MemoryLocation*>()
        for each Storage in F: for each AP in Storage.MemoryLocationSet:
            EvaluateAps(AP)                                            // force SymbolicAP → PhysicalAP
            memlocs.insert(AP.storage_base.owning_memloc)             // StorageBase+0x160 → Set
        tbb::parallel_for(memlocs, sub_1587710)                       // per-memloc RAW dispatch (below)

        // (c) STRUCTURAL DEPS over basic blocks
        for each BasicBlock BB in F:
            build_loop_body_ordering(BB)        // is_loop_inst markers → Flow chain to loop boundary
            build_rng_state_ordering(BB)        // SetRandState(op 59): serial chain per EngineType
            build_accumulator_groups(BB)        // Activation(4)/Exp(103)/TensorTensor(31): per-group chain
            addDependenciesAmongSchedulingUnits(BB)

        if addRegDeps:
            add_register_dependencies(F)        // 0x1579650 — Register RAW chain (optional)
    log("Build fdeps inserted N edges"); log("Done build fdeps " + ctime())
```

Every `addDependency` call in this pass packs `EdgeKind=Flow(4)`. The structural chains (loop / RNG / accumulator) are also Flow edges — they are real ordering constraints the scheduler must honor, modeled as data dependences so the single edge set carries them. *Anchors: `constructBIRGraph` @ `0x1588990` (exported), and the log strings `Start build fdeps. Invocation:`, `Done build fdeps`, `Build fdeps inserted`, `Warning: too many spills, skip build flow dependencies`.*

### Algorithm — per-memloc dispatch (`sub_1587710`)

The `parallel_for` runs this body once per touched `MemoryLocation`. It is the only non-thunk caller of all three `make_*` helpers.

```c
function per_memloc_flow(memloc):                       // sub_1587710 @0x1587710
    type    = *(int*)(memloc + 0x216_word)              // MemoryType: DRAM=8, SB=16, PSUM=32
    writers = StorageBase::writers<AccessPattern>(memloc)
    readers = StorageBase::readers<AccessPattern>(memloc)
    make_linear_dependencies(readers, writers)          // baseline RAW chain — ALL types
    if type == 16:  make_reduction_sequence(memloc)     // SBUF reduction refinement
    if type == 32:  make_accumulation_sequence(memloc)  // PSUM accumulation refinement
    // sanity: a non-DRAM memloc's writer count must equal its registered num_writers
```

> **NOTE —** the baseline RAW chain is built for *every* memory type. SBUF and PSUM memlocs get an *additional* refinement on top, because their accesses are tiled/strided sub-tensors and the per-`MemoryLocation` granularity is too coarse to catch the exact reduction/accumulation ordering. The refinements are where the byte-level geometry test enters `build_fdeps`. *Anchors: `make_linear_dependencies` @ `0x157ca80`, `make_reduction_sequence` @ `0x1583db0`, `make_accumulation_sequence` @ `0x15876f0`, all exported; `MemoryType` DRAM=8 / SB=16 / PSUM=32.*

### Algorithm — the baseline linear chain

```c
function make_linear_dependencies(readers, writers, programIdx):   // 0x157ca80
    writer_set = DenseSet<Instruction*>()
    work = []
    for w in writers: writer_set.insert(w.parent_inst); work.append(w.parent_inst)  // AP+0x20 = parent
    for r in readers:
        if r.parent_inst.opcode == 41: continue        // op 41 is not a real flow consumer
        work.append(r.parent_inst)
    sort(work, key = programIdx[inst])                 // insertion sort ≤128 elems, else merge sort

    last_writer = null
    for inst in work (program order):                  // O(n) single pass
        if inst in writer_set:
            last_writer = inst                         // a write: becomes the new producer
        else if last_writer != null:
            inst.addDependency(former=last_writer, Flow, /*lc=*/0)   // read → most-recent write
```

This is textbook RAW: each read depends on the *most recent prior write* to the memloc, and writes serialize through the chain. It is O(n) per memloc, not all-pairs — and it needs no geometry test, because every AP in the call already belongs to the same `MemoryLocation` (the writer/reader use-def lists are per-memloc), so any reader of the memloc conflicts with the last writer by construction (`make_linear_dependencies` @ `0x157ca80`).

### The two refinements and the only `doAccessesOverlap` call

`make_reduction_sequence` (SBUF) and `make_accumulation_sequence` (PSUM) handle read-modify-write groups that the linear chain over-serializes or under-serializes. A reduction reads-modifies-writes the same SBUF tile across many ops whose APs are tiled and strided; a PSUM accumulation is a matmul/TensorTensor sequence that the hardware drains in-place into one bank, so each accumulate genuinely depends on the previous even though SSA sees distinct values.

`make_reduction_sequence` is the **only** caller of `bir::doAccessesOverlap` among the dependence passes (callgraph-confirmed). It gates on the first accessor being a reduction op (`opcode != 10 && != 23`), and for consecutive same-group `InstTensorTensor` (op 31) writes it chains a Flow edge when either `bir::supersetAP(producerAP, candidateAP)` holds (one AP wholly covers the other — the whole-tile case) or `bir::doAccessesOverlap(otherAP, producerAP, 0)` returns true (the exact strided-overlap case). The overlap test is what keeps two *interleaved* reduction streams that never share a byte from being spuriously chained.

```c
function make_reduction_sequence(memloc):                 // 0x1583db0 — SBUF only (type==16)
    accessors = sort(writers + readers, key = programIdx[inst])
    assert(accessors.size() > 0)                          // NeuronAssertion EC13
    if accessors[0].opcode == 10 or == 23: return         // not a reduction op
    for (producer, candidate) in consecutive same-group TensorTensor writes:   // op 31
        if supersetAP(producer.ap, candidate.ap)          // whole-tensor cover
           or doAccessesOverlap(candidate.ap, producer.ap, 0):   // exact strided overlap
            candidate.inst.addDependency(former=producer.inst, Flow, 0)
```

`make_accumulation_sequence` (PSUM, `type==32`) mirrors this skeleton — same writers/readers/sort/chain structure, specialized to the PSUM RMW group and gated on the accumulate/calc-start flags. It is the producer side of the matmul-accumulation group; the non-SSA exit logic deliberately exempts this group from value cloning, agreeing that the PSUM accumulate is an intentional RMW chain. That `doAccessesOverlap` has exactly one caller, `make_reduction_sequence`, is read directly. The `make_accumulation_sequence` body is reconstructed by analogy: its symbol and dispatch are pinned @ `0x15876f0`, but the PSUM-bank gate constants are not transcribed field by field.

### Function Map — `build_fdeps`

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `build_flow_deps::constructBIRGraph(int,bool)` | `0x1588990` | pass body; module loop, spill guard, parallel_for, structural chains | CERTAIN (exported) |
| `sub_1587710` | `0x1587710` | per-`MemoryLocation` RAW dispatch (parallel_for lambda) | CERTAIN (sole `make_*` caller) |
| `make_linear_dependencies(vec<AP&>,vec<AP&>)` | `0x157ca80` | baseline program-order producer→consumer chain | CERTAIN (exported) |
| `make_reduction_sequence(MemoryLocation*)` | `0x1583db0` | SBUF reduction refinement; only `doAccessesOverlap` caller | CERTAIN (exported) |
| `make_accumulation_sequence(MemoryLocation*)` | `0x15876f0` | PSUM accumulation refinement | HIGH (symbol + dispatch; body by analogy) |
| `add_register_dependencies(Function&)` | `0x1579650` | optional Register RAW chain (`addRegDeps`) | HIGH (exported, gated) |
| `getFlowDeps(Inst*, vec&)` | `0x10e8e70` | read-side: `hasEdge(Flow=4)` partition | CERTAIN (exported) |

---

## `anti_dependency_analyzer` — the anti (WAR) + output (WAW) graph

### Purpose

`anti_dependency_analyzer` builds the name/storage dependences: a later write must wait for an earlier *read* of the same physical location (anti, WAR, `Anti=2`), and two writes to the same physical location must be ordered (output, WAW, `Output=3`). These edges do not exist in SSA — they are created by the allocator coloring distinct tensors onto shared storage. The pass therefore runs at 76, *after* PSUM coloring (44) and SBUF coloring (51) and address rotation, when every access has a concrete physical address. It does **not** call `doAccessesOverlap`; instead it buckets every physical access's byte footprint into `boost::icl` interval maps and treats "same bucket" as "overlap."

The class is registered twice — `register_generator_anti_dependency_analyzer__` and `…_post_shared_dram__` — and constructed with five filter arguments (ctor @ `0x8cac40`): two `AdaAnalysisType` sets, a `bir::TensorKind` set, a `bir::MemoryAddressSpace` set, and a string set. The two registrations differ in which sets they pass: the base run handles SBUF/PSUM/local DRAM; the `_post_shared_dram` variant re-runs after shared-DRAM allocation (the cross-core/collective DRAM that only aliases once shared addresses are assigned). The five-set ctor signature and both `addCoreParallelPassWithName` / `addModParallelPassWithName<AntiDependencyAnalyzer, unordered_set<AdaAnalysisType>, …, unordered_set<TensorKind>, unordered_set<MemoryAddressSpace>>` template instantiations appear verbatim in the exported symbol table. *Anchors: ctor @ `0x8cac40`; registration templates @ `0x82cc30` / `0x82df60`; RTTI @ `0x3d897d0` / `0x3d89880`.*

### Entry Point

```text
AntiDependencyAnalyzer::run(Module&)                       @0x8d26e0   ── WAR/WAW driver
  ├─ getArchModel(Module) → DRAM_size, num_bins, bin_size              ── DRAM binning constants
  ├─ (per Function, per BasicBlock):
  │    ├─ collectPartitionRanges(BB)                        @0x8cb9f0   ── size per-partition interval sets
  │    ├─ populateAliasUseMap(Function&)                    @0x8c7c00   ── DRAM alias-keyed batches
  │    ├─ populatePsumUseMaps(...)                          @0x8c73c0   ── per-PSUM-bank interval maps
  │    ├─ populateSbUseMap(...)                             @0x8c6d10   ── SBUF partition×byte interval map
  │    ├─ analyzeDependencies(BB)                           @0x8c5d50   ── per-AP → enqueueUseBatch
  │    │    └─ enqueueUseBatch(AP / PAP, batches, maps)     @0x8c5be0 / @0x8c5260
  │    └─ run::Task per non-empty batch  (DRAM:/ALIAS:/PSUM:/SB:)
  └─ tbb parallel_for_in_arenas over Tasks → AddressMapFunctor::apply  @0x8cf900
       └─ processDependenciesForAccessPatterns                @0x8cec30 ── per-bucket reverse walk
            └─ AddressMapFunctor::addDependency               @0x8ce660 → @0x8cc560 ── edge insert
```

### The byte-interval bucketing — the overlap test

The core container is a Boost.Interval-Container-Library map. The exported signatures of `enqueueUseBatch`, `analyzeDependencies`, `populateSbUseMap`, and `populatePsumUseMaps` carry it verbatim:

```text
boost::icl::split_interval_map<
    uint,                                              // key: physical byte address
    std::vector<std::shared_ptr<PhysicalAccessPattern>>,   // value: APs touching that byte range
    partial_enricher, std::less, inplace_plus, inter_section,
    right_open_interval<uint> >
```

A `split_interval_map` auto-splits at every interval boundary, so after all accesses are inserted each maximal sub-interval carries the *exact* set of APs that touch it. Inserting an AP appends its `shared_ptr` to every sub-interval its `[addr, addr+len)` footprint covers. This is the whole trick: **two APs that conflict are exactly the two APs that share a sub-interval**, so the O(n²) "does A overlap B" pairwise question becomes an O(n log n) series of interval-tree insertions, and the per-bucket pairwise pass (below) only ever compares genuinely-conflicting accesses.

The address space is sliced three ways, each gated by the `MemoryAddressSpace` filter set:

| Space | Key map | Built by | Bucketing |
|---|---|---|---|
| DRAM (`type==8`) | fixed-size bins | `populateAliasUseMap` @ `0x8c7c00` | `addr/bin_size .. (addr+len-1)/bin_size`; `bin_size = DRAM_size/num_bins` |
| DRAM alias | alias-set keyed | `populateAliasUseMap` | `aliasBatchMap[ aliasMemLocMap[memloc] ]` |
| SBUF (`type==16`) | partition × byte interval | `populateSbUseMap` @ `0x8c6d10` | `getSBPartitionRange(ap)` → walk overlapping intervals, append `ap` |
| PSUM (`type==32`) | per-bank partition × byte | `populatePsumUseMaps` @ `0x8c73c0` | `getPSUMPartitionRange(ap)` + `getBankId`/vectorized banks |

`run::run` logs `DRAM size: <s> num-bins: <n> bin-size: <b>` and `Number of batches: N (DRAM: a, ALIAS: b, PSUM: c, SB: d)` — both strings verified in the binary. The DRAM binning exists to *partition and parallelize* the WAR/WAW search: each bin becomes an independent `run::Task` so the per-bucket pass runs under a TBB `parallel_for_in_arenas`. *Anchors: `split_interval_map<uint,…,right_open_interval<uint>>` is the exported template of `populateSbUseMap` @ `0x8c6d10` and its siblings.*

### Algorithm — bucketing one access (`enqueueUseBatch`)

```c
function enqueueUseBatch(ap, batches, aliasMap, aliasBatch, sbMap, psumMaps):   // 0x8c5be0 / 0x8c5260
    sb   = ap.storage_base                              // assert sb.kind == MemoryLocation
    type = sb.memloc.memory_type                        // +0x216 word
    if type == 8:                                       // DRAM
        if TensorKind not in filter_set: return         // this+0x66 set
        addrspace = sb.addr_space                        // forced Shared(1) if isSharedPostDRAMAlloc
        if addrspace not in filter_set: return           // this+0x6D set
        if AdaAnalysisType has "2" and memloc.allocated:  // DRAM bin path
            addr = memloc.address;  len = aligned_length(sb)   // round to bin granularity
            for bin in [addr/bin_size .. (addr+len-1)/bin_size]:
                batches[bin].push_back(ap)
        else if AdaAnalysisType has "3":                  // DRAM alias path
            aliasBatch[ aliasMap[sb] ].push_back(ap)
        collectAccessPatternIntervals(ap, 0)              // materialize byte intervals
    else if type == 16:                                 // SBUF
        for interval in sbMap.overlapping(getSBPartitionRange(ap)):
            interval.value.push_back(ap)                  // append to every covered sub-interval
    else:                                               // PSUM (type == 32)
        banks = getBankId(sb) >= 0 ? [getBankId(sb)] : get_vectorized_bank_ids(ap)
        for bank in banks:
            for interval in psumMaps[bank].overlapping(getPSUMPartitionRange(ap)):
                interval.value.push_back(ap)
```

The SBUF/PSUM "overlapping interval" walk is the `boost::icl right_open_interval` RB-tree search — it appends `ap` to *every* maximal sub-interval its partition×byte footprint covers, auto-splitting the map. The signatures are exported. The `AdaAnalysisType` integers `2` and `3` are read at the role level — DRAM-direct versus alias — rather than off a `2string` table, so the specific ordinals are the softest part of this section.

### Algorithm — the per-bucket edge insertion

Each `run::Task` feeds one batch to an `AddressMapFunctor` (ctor @ `0x8c1510`). `apply()` @ `0x8cf900` iterates the batch's interval map and, for each current AP and the set of other AP indices in the same bucket, calls `processDependenciesForAccessPatterns` @ `0x8cec30`:

```c
function processDependenciesForAccessPatterns(idxVec, interval, curAP, sets):   // 0x8cec30
    for otherAP in idxVec (REVERSE program order):     // latest → earliest
        if otherAP.inst == curAP.inst: continue
        kind = *(byte*)(otherAP + 0x2C)                // precomputed EdgeKind hint: Anti(2)/Output(3)
        if otherAP.inst.opcode in {7, 9, 95}:          // Matmult-base family: stop at first same-kind dep
            addDependency(interval, otherAP, curAP, sets)
            break                                      // transitive reduction — nearest prior only
        addDependency(interval, otherAP, curAP, sets)  // 0x8ce660
```

The reverse walk plus "stop at first blocking same-kind dep" gives each access an Anti/Output edge to its *nearest prior conflicting access only* — a built-in transitive reduction so the graph never goes quadratic. The `sets` argument is a `std::array<llvm::DenseSet<const Instruction*>, 2>` (one DenseSet per kind), and `addDependency` @ `0x8ce660` indexes it by `3 * (*(byte*)(otherAP+0x2C))` to dedup duplicate WAR (or WAW) edges to the same producer per kind. *Anchors: `processDependenciesForAccessPatterns` @ `0x8cec30`, and the per-kind-dedup `addDependency` whose `std::array<llvm::DenseSet<…>,2>` template is exported verbatim.*

> **NOTE —** the per-engine emitters `processInput` @ `0x8c4190`, `processOutput` @ `0x8cf7a0`, `processOutputMatmul` @ `0x8c39d0`, `processOutputGeneric` @ `0x8c32c0`, and `processOutputGenericCoreV4` @ `0x8ced30` are sibling members of `AddressMapFunctor` that specialize the read-side (input) vs write-side (output) handling, and the matmul vs generic vs gen4 PE-array cases. The `{7,9,95}` early-stop in the reverse walk corresponds to the Matmult-base family these `processOutputMatmul`/`Generic` variants split on. All five are exported `AddressMapFunctor` members; the opcode→family mapping behind `{7,9,95}` is reconstructed rather than read from a table.

### Algorithm — edge direction and the loop-carried branch

The final `addDependency` @ `0x8cc560` (the bulk of its body is an optional `"Explaining why X and Y interfere"` debug trace that dumps `EdgeKind2string` and both `MemoryLocation`s' JSON; the live logic is ~25 lines) decides direction by comparing program indices:

```c
function addDependency(interval, otherAP, curAP):       // 0x8cc560 — final edge + direction
    cur_idx   = curAP.inst.program_index                // inst+0x4C
    other_idx = otherAP.inst.program_index
    if other_idx <= cur_idx:                            // otherAP is EARLIER (or equal)
        curAP.inst.addDependency(former=otherAP.inst, kind=*(byte)(otherAP+0x2C), lc=0)
        ++edge_count                                    // this+0x298
    else:                                               // out of program order
        if curAP.memloc_set != otherAP.memloc_set
           and inCommonLoopNest(curAP.inst, otherAP.inst):
            curAP.inst.addDependency(former=otherAP.inst, kind, lc=1)   // loop-carried WAR/WAW
            ++edge_count
```

The edge always points from the *later* instruction to the *earlier* one — matching the `dependencies = predecessors` semantics: `A.addDependency(former=B)` means A waits for B. The `EdgeKind` is the precomputed per-AP byte at `+0x2C`: `Anti(2)` when the pair is (later WRITE, earlier READ) = WAR, `Output(3)` when (later WRITE, earlier WRITE) = WAW. The out-of-program-order branch is what populates the `loop_carried_dependencies` set (`BLK+0x4A8`) for cross-iteration WAR/WAW — only when the two accesses are different memloc sets *and* share a loop nest. *Anchors: `addDependency` @ `0x8cc560` — the `+0x4C` program-index compare, the `inCommonLoopNest` loop-carried branch, and the `addDependency(former,kind,lc)` target are all explicit in the body.*

### Function Map — `anti_dependency_analyzer`

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `AntiDependencyAnalyzer::run(Module&)` | `0x8d26e0` | WAR/WAW driver; setup, binning, batch build, dispatch | CERTAIN (exported) |
| `AntiDependencyAnalyzer(PassOptions, 5 sets)` | `0x8cac40` | ctor with two `AdaAnalysisType`, `TensorKind`, `MemoryAddressSpace`, string filter sets | CERTAIN (exported) |
| `collectPartitionRanges(BasicBlock&,…)` | `0x8cb9f0` | size the per-partition interval-set vector | CERTAIN (exported) |
| `collectAccessPatternIntervals(PAP*, bool)` | `0x8c4a90` | materialize an AP's byte intervals (partition-rel vs absolute) | CERTAIN (exported) |
| `populateAliasUseMap(Function&,…)` | `0x8c7c00` | DRAM alias-keyed batches | CERTAIN (exported) |
| `populatePsumUseMaps(vec<set>,…)` | `0x8c73c0` | per-PSUM-bank interval maps | CERTAIN (exported) |
| `populateSbUseMap(set,…)` | `0x8c6d10` | SBUF partition×byte interval map | CERTAIN (exported) |
| `enqueueUseBatch(AP&/PAP&,…)` | `0x8c5be0` / `0x8c5260` | classify one access, append to its batch/bucket | CERTAIN (two overloads exported) |
| `analyzeDependencies(BasicBlock&,…)` | `0x8c5d50` | fill the use-maps from the block's APs | CERTAIN (exported) |
| `getSBPartitionRange` / `getPSUMPartitionRange` / `aligned_length` | `0x8c1860` / `0x8c1700` / `0x8c1920` | partition-range + bin-granularity helpers | CERTAIN (exported) |
| `AddressMapFunctor::apply()` | `0x8cf900` | iterate one batch's interval map | CERTAIN (exported) |
| `processDependenciesForAccessPatterns(SmallVec<uint,6>,…)` | `0x8cec30` | reverse-order per-bucket pairwise walk | CERTAIN (exported) |
| `AddressMapFunctor::addDependency(…, array<DenseSet,2>)` | `0x8ce660` | per-kind dedup guard | CERTAIN (exported) |
| `AddressMapFunctor::addDependency(interval,PAP,PAP)` | `0x8cc560` | final edge insert + direction + loop-carried | CERTAIN (exported) |
| `getAntiDeps(Inst*, vec&)` | `0x10e8f30` | read-side: `(EdgePtr&7)==2` partition | CERTAIN (exported) |

---

## Why anti/output run post-allocation

### The argument

A flow (RAW) edge is a *value* dependence: the consumer reads what the producer wrote. It is intrinsic to the dataflow, present in SSA, and survives any renaming or reallocation. `build_fdeps` recovers it from `MemoryLocation` identity — two accesses to the same logical tensor are *by construction* the same `MemoryLocation`, so no physical-address reasoning is needed for the baseline; only SBUF/PSUM sub-tile reductions need `doAccessesOverlap` for precision.

Anti (WAR) and output (WAW) edges are *name/storage* dependences: "a later write must wait for an earlier read of the **same physical location**", "two writes to the **same physical location** must be ordered." In SSA these do not exist — distinct values live in distinct virtual locations. They are *created* when the allocator colors two logically-distinct tensors onto the same physical SBUF partition, PSUM bank, or DRAM bin (storage reuse). That reuse aliasing is invisible until PSUM coloring (pass 44) and SBUF coloring (pass 51) plus address rotation have assigned concrete addresses. **Therefore `anti_dependency_analyzer` must run after allocation** — at pass 76, after `build_fdeps` (74) and `remove_redundancies` (75).

This explains the two different overlap primitives. `build_fdeps` groups by `MemoryLocation` identity, so overlap is "same logical tensor" — trivial. `anti_dependency_analyzer` groups by physical byte interval, because it must catch conflicts between tensors that were *not* the same `MemoryLocation` before allocation but now share storage. The byte-interval bucketing *is* the overlap test; the per-bucket pairwise pass only decides edge direction and kind.

### Considerations

The pre-scheduling pass (33) runs before `build_fdeps` and consumes whatever dependency-set members existed then, *kind-agnostically* — its `MemoryLocation` DAG keeps only topological reachability (`EdgePtr & ~7`), discarding the `Flow`/`Anti`/`Output`/`Ordered` tag. It therefore does not — and cannot — depend on the post-allocation anti/output edges, which do not yet exist. `build_fdeps` then rebuilds the flow graph on the now-materialized `PhysicalAccessPattern`s, and `anti_dependency_analyzer` adds the WAR/WAW edges the post-allocation aliasing introduced. The full three-kind edge set is what the post-scheduling pass and the synchronizer / `alloc_semaphores` / `lower_sync` chain consume to emit the actual hardware semaphores. The pass order and the post-allocation precondition are directly established; that the pre-scheduling pass consumes the edge set kind-agnostically is inferred from its `EdgePtr & ~7` masking rather than stated anywhere.

> **GOTCHA —** a reimplementation that builds anti/output edges *before* allocation will find none (every tensor is its own virtual location) and will then under-synchronize the colored program — two reused buffers will race. The edges only exist as a function of the coloring, so the analysis must be re-run, or run for the first time, *after* every allocator that can introduce storage reuse. The `_post_shared_dram` second registration exists for exactly this reason: shared/collective DRAM aliasing is not known until shared-DRAM allocation, so a second anti pass runs after it.

---

## Related Passes

| Pass | Name | Relationship |
|---|---|---|
| 74 | `build_fdeps` | builds the Flow(4) graph this page's anti pass complements |
| 75 | `remove_redundancies` | prunes redundant edges between the two builders ([8.10](dep-opt-reduction.md)) |
| 76 | `anti_dependency_analyzer` (+`_post_shared_dram`) | builds Anti(2)/Output(3) post-allocation |
| 33 | pre-scheduler | kind-agnostic consumer of the *pre*-allocation flow reachability |
| 44 / 51 | PSUM / SBUF coloring | the allocators whose storage reuse *creates* the anti/output edges |
| — | synchronizer / `alloc_semaphores` / `lower_sync` | lower the surviving cross-engine edges into hardware semaphores |

---

## Cross-References

- [5.5 — Penguin Dependency Model](../penguin/dependency-model.md) — the frontend `DependencyEdge`/`EdgeKind` twin; the same `{FLOW, ANTI, OUTPUT, ORDERED}` taxonomy that lowers into the `bir::EdgeKind` this page's backend builders pack
- [Backend Dependence-Distance](../penguin/backend-dependence-distance.md) — the polyhedral name-level dependence model that frames these edges
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the partition×byte and bank address spaces the anti analyzer buckets into; the `MemoryType` SB=16 / PSUM=32 split
- [8.10 — Dependence Optimization & Reduction](dep-opt-reduction.md) — `remove_redundancies` (pass 75), the reduction that prunes the edge set between the two builders (planned)
- 8.11 — Backend Scheduler — the consumer of the three-kind edge set that the synchronizer then lowers to semaphores (planned)
