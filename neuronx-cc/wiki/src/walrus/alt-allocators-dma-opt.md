# Alternative Allocators and DMA Optimization

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 — the `neuronxcc/starfish/lib/libwalrus.so` body frame of the cp310 wheel, where `.text`/`.rodata` virtual address equals file offset. The `.symtab` is stripped but the **full** `.dynsym` export survives, so `nm -DC libwalrus.so` resolves every method named here. Other wheels differ; treat every address as version-pinned, and see the two-VA-frame NOTE below before cross-checking against a per-symbol IDA sidecar.*

## Abstract

The walrus backend has one *principal* tensor allocator — the Chaitin–Briggs graph colorer of [`allocator-drivers`](allocator-drivers.md) (`coloring_allocator_{sb,psum,dram}`, the "GCA" = **G**raph-**C**oloring **A**llocator) — and a small family of **alternatives** that an optlevel or a CLI flag can swap in for it. This page documents those alternatives and the DMA-level peephole passes that clean up whatever any allocator leaves behind. Three distinct subjects live here:

1. **`REG_Allocator`** — a *scalar register* colorer (`coloring_allocator_reg`), not a memory allocator. It colors `bir::Register*` (the SP/sequencer engine's scalar lanes) rather than `MemoryLocation` rectangles, so it is a 1-D Chaitin–Briggs colorer with a single scored color and a spill fixpoint, *contrasted* against the 2-D best-of-N SBUF colorer. It is **optlevel-independent** — it always runs late, after machine lowering materializes scalar registers.
2. **`LinearScanAllocator` + `BestFitMemoryManager`** — an *alternative memory allocator* selected by `--allocator=lsa` or forced at optlevel 8. It scans live intervals once in program order, expires dead intervals, and delegates concrete 2-D placement to a best-fit free-list manager — faster and lower-quality than the colorer, single-core only.
3. **`dma_optimization_{psum,sb}`** — two instances of one `DMAOptimization` pass that run *after* the allocator has bound tensors to physical addresses and inserted spill/reload DMAs. They perform three peephole families on real addresses: **coalescing** (merge abutting DMAs into one descriptor), **partial-DMA** (shrink an over-broad DMA to the used sub-range), and **redundant-removal** (drop a load whose data is already resident).

> **GOTCHA — "SMT allocation" involves no solver.** Despite the name, optlevel-7 allocation is not Z3 or any other SMT engine: `nm -D libwalrus.so` shows zero `Z3_*` / `CVC` / `Yices` symbols and zero `__gmp*` call sites, and although `libgmp` is a transitive NEEDED dependency, no allocation path reaches it. `--smt-allocation` is an `llvm::cl::opt<bool>` ("Doing smt allocation") that merely forces `optlevel = 7`, whose pipeline is built from the *same* family-(a) `coloring_allocator_{dram,dram_shared,sb}` passes — a reordered graph-coloring pipeline plus an in-place re-verification (`Unroll::check_in_place`). There is no objective function and no UNSAT path. See [§4](#optlevel-7-smt-allocation--not-a-solver).

For reimplementation, the contract is:

- The `REG_Allocator` algorithm: SSA-representative build, the live-range / coalesce / build / find-costs / simplify / select cycle, the single REG-score, the spill-to-`MemoryLocation` fixpoint, and the hard register-pressure FATAL.
- The `LinearScanAllocator` state machine: the `Live_Range_Manager` interval bookkeeping, the comparator-sorted active set, the expire-old-intervals rule, the spill-at-interval victim selection, and the `BestFitMemoryManager` 2-D corner-placement backend.
- The three `DMAOptimization` peepholes with their exact trigger predicates, and the data-driven PSUM-vs-SB split (one class, two names).
- The optlevel/flag selection map that decides *which* allocator is wired.

| | |
|---|---|
| **Driver dispatch** | `sub_80D9D0` (walrus_driver optlevel switch) · `run_backend_driver` `@0x8113b0` |
| **REG colorer** | `REG_Allocator::allocate(Function*)` `@0x9cbaf0`; pass `coloring_allocator_reg` (always-run, `sub_805870` L33) |
| **Linear scan** | `LinearScanAllocator::run(Module&)` `@0xb0eeb0`; `Live_Range_Manager` `@0xb32…`; `BestFitMemoryManager::allocate` `@0xb241a0` |
| **DMA opt class** | `DMAOptimization::run(bir::Module&)` `@0x966740` (0x8e0 B); `dma_optimization_psum`/`_sb` differ only by name |
| **REG colors** | `bir::Register*` (scalar SP lanes), `numPhysicalRegs` @ `Register+0x18C` |
| **LSA / opt8** | single-vNC-core ONLY (`vncNcCount==1`, `sub_80D9D0` assert L853) |
| **GCA dma knob** | `--gca-dma-optlevel` `cl::opt<int>` `@0x3dfb300`, default **1**, levels `{-1,0,1,2}` |

> **NOTE — the two-VA-frame artifact.** The K11/H18 evidence frame and this page pin the **libwalrus.so body** addresses (`REG_Allocator::allocate @0x9cbaf0`, `LinearScanAllocator::run @0xb0eeb0`, `Unroll::check_in_place @0xb361f0`). The per-symbol IDA export of an *adjacent* wheel (cp311) places these high-VA allocator functions ~0x20–0x30 lower (`0x9cbb00` / `0xb0ee80` / `0xb361c0`) — a layout delta, not a different function. The DMA-optimization block (`0x9*`) is byte-for-byte at the same VA across the frames (`internal_dma_coalescing @0x9654f0`, `partial_DMA_access @0x95a7f0`). Separately, [`sp-engine`](../arch/sp-engine.md) cites `REG_Allocator::simplify @0x5edb60` / `::live_range @0x5edd00`: those are the **standalone `coloring_allocator_with_loop` tool binary** frame, *not* libwalrus. When verifying, match the frame to the binary before declaring an address wrong.

---

## The Selection Map — which allocator is wired

### Purpose

Before any allocator runs, the walrus_driver dispatcher decides *which* family handles SBUF/PSUM. The decision is a normalize-then-`switch(optlevel)` in `sub_80D9D0`, fed by the `llvm::cl` globals that `run_backend_driver` (`@0x8113b0`) packs into a `PassOptions`. The detailed per-pass optlevel planes are owned by [`pass-pipeline-optlevels`](pass-pipeline-optlevels.md); the allocator-*family* selection (coloring vs with-loop vs linear-scan) is owned by [`allocator-drivers`](allocator-drivers.md#allocator-family-selection). This page records only the slice that picks an *alternative*.

### Algorithm

```c
// sub_80D9D0 — the walrus_driver optlevel DISPATCH (a switch, NOT an SMT builder)
function dispatch(PassOptions& po):
    assert(allocator=="" || allocator=="coloring" || allocator=="lsa");  // L731

    if (loopsInBackend)        optlevel = (optlevel==2) ? 6 : 8;         // L818
    if (SmtAllocation)         optlevel = 7;                             // L815 — forces opt7
    // LSA / opt0 / opt8 are SINGLE-vNC-core only; multi-core forbids them:
    assert(vncNcCount==1 || (allocator!="lsa" && optlevel!=0 && optlevel!=8)); // L853

    switch (optlevel):                                                   // L946
      case 0:        sub_806F80();   // minimal (~28 passes)
      case 1/2/3:    sub_80A6E0();   // primary pre-codegen cascade (coloring)
      case 6:        sub_807EF0();   // coloring_allocator_with_loop (allocator-drivers)
      case 7:        /* inline */    // reordered COLORING pipeline + in-place verify (§4)
      case 8:        sub_809580();   // linear_scan_allocator SOLE (§3)

    // AFTER any optlevel pipeline, the always-run low-level backend runs:
    sub_805870();   // add_lowlevel_backend_passes — contains coloring_allocator_reg (§2)
```

### Selector table

| CLI | optlevel | SBUF/PSUM allocator | DRAM | REG (always) | Confidence |
|---|---|---|---|---|---|
| `--optlevel 0` | 0 | minimal / reservation | `coloring_*` | `coloring_allocator_reg` | CERTAIN |
| `--optlevel 1/2/3` | 1/2/3 | `coloring_allocator_{psum,sb}` | `coloring_*` | `coloring_allocator_reg` | CERTAIN |
| `--optlevel 6` (or `--optlevel 2` + `loopsInBackend`) | 6 | `coloring_allocator_with_loop` | `coloring_*` | `coloring_allocator_reg` | CERTAIN |
| opt6 + `--allocator=lsa` | 6 | `linear_scan_allocator` (+`coloring_sb`) | `coloring_*` | `coloring_allocator_reg` | CERTAIN |
| `--smt-allocation` | 7 | `coloring_allocator_{dram,sb}` (reordered) | `coloring_*` | `coloring_allocator_reg` | CERTAIN |
| `--optlevel 8` | 8 | `linear_scan_allocator` (SOLE) | `coloring_dram_debug` | `coloring_allocator_reg` | CERTAIN |

> **QUIRK — `REG_Allocator` is optlevel-independent.** Every row spells the same final column. The register colorer cannot be swapped out by an optlevel because scalar SP-engine registers do not *exist* until `lower_branch`/`lower_sync`/`lower_ap` have expanded control flow into sequencer machine instructions. So `coloring_allocator_reg` lives in the *always-run* `sub_805870` pipeline (L33), after machine lowering and before `codegen`, regardless of which memory allocator ran upstream.

---

## REG_Allocator — the scalar register colorer

### Purpose

`REG_Allocator` (pass `coloring_allocator_reg`) colors `bir::Register*` — the SP/sequencer engine's named scalar registers, each spanning `numPhysicalRegs` physical lanes (`Register+0x18C`; 1 for a 32-bit value, 2 for a 64-bit value, per [`sp-engine`](../arch/sp-engine.md#51-the-register-struct)). It is the register-allocation analogue of the SBUF/PSUM memory colorer, but it operates on a *fundamentally different* object: a flat set of scalar registers, not a 2-D (partition × byte) rectangle space. Its `candidate(bir::MemoryLocation&)` `@0x9cda10` is a `return 0;` **stub** — proof that REG does not treat `MemoryLocation`s as candidates; it gathers registers through the SSA path instead.

### Entry Point

```text
ColoringAllocator<RepT<REG_Allocator,void>>::allocateImpl(Function*,int,int,bool)  @0x98b010
  ── demangles "REG_Allocator" + "LinearizedFunctionAllocator"; builds a stack REG_Allocator
  └─ REG_Allocator::allocate(Function*)                                            @0x9cbaf0
       ── banner "  allocating REG"; per-function
       ├─ build_ssa_representative(Function*)   @0x9ce0b0   ── SSA value-numbering of registers
       ├─ renumber_locations(ArrayRef<Register*>) @0x9c8580 ── location id := numPhysicalRegs (+396)
       ├─ recordFirstDefLastUse(Info&)          @0x9c8e10   ── per-register live-range endpoints
       └─ < spill fixpoint — see Algorithm >
  registered by register_generator_coloring_allocator_reg__  (BSS @0x3dff397)
```

### Algorithm

```c
// REG_Allocator::allocate(Function*)  @0x9cbaf0 — Chaitin–Briggs over scalar registers
function REG_allocate(Function* fn):                          // banner "  allocating REG"
    linearize(fn);                                            // @0x9ca0e0 — "linearize and check (REG)"
    build_ssa_representative(fn);                             // @0x9ce0b0
    renumber_locations(regs);                                 // @0x9c8580 — id = numPhysicalRegs (reg+396)
    recordFirstDefLastUse(info);                              // @0x9c8e10

    for (iter = 0; ; ++iter):                                 // "REG GCA interation nums = <iter>"
        live_range(info, regs);                               // @0x9b5930 — recompute live ranges
        coalesce_ssa_live_range(info);                        // @0x9cead0 — SSA-copy coalescing
        build(insts, info);                                   // @0x9af510 — interference graph
                                                              //   (llvm::SparseSet, Universe = #regs)
        find_costs(insts, info);                              // @0x9b2130 — spill cost
                                                              //   += numPhysicalRegs (reg+396) per def
        simplify(info);                                       // @0x9bc880 — Briggs simplify/optimistic push
        select(info, &colors, &score);                        // @0x9b9de0 — "REG score = … (lower is better)"

        if (select left nodes uncolored):
            if (no spill candidate):
                FATAL("cannot allocate registers since spilling "
                      "candidates are not enough");           // THE hard register-pressure limit
            insert_spill_code(insts, &spilled, info);         // @0x9c10d0 — reg → MemoryLocation
            continue;                                         // re-run the cycle with spills
        else:
            propagate_coalesced_ssa_allocation();             // @0x9cef00 — push color through copies
            break;
    // reports: "Spilling from REG cost about <N> cycles",
    //          "Number of registers spilled = <N>",
    //          "<pct>% REG utilization after allocation"
```

> **QUIRK — one scored color, not best-of-N.** The SBUF colorer runs a *heuristic sweep* and keeps the best of several scored colorings (the "best-of-N" reading corrected in [`allocator-drivers`](allocator-drivers.md#the-spill-fixpoint--best-of-n-is-a-correction)). REG does **not**: `select` `@0x9b9de0` produces a *single* `double` score ("REG score … lower is better"), and the outer counter ("REG GCA interation nums") is the spill-reinsert **fixpoint**, not an enumeration of alternative colorings. A reimplementer who copies the 2-D sweep into the scalar colorer will add iterations that this colorer never runs.

### The spill target — registers spill to memory

```c
// REG_Allocator::insert_spill_code(...)  @0x9c10d0 — banner "      collect spills"
//   builds a llvm::DenseMap<bir::Register*, bir::MemoryLocation*>  (decompile L1958–2075)
function insert_spill_code(insts, spilled, info):
    for each register r chosen to spill:
        MemoryLocation* slot = allocate_spill_slot(r);       // a MEMORY location, not a stack slot
        spillMap[r] = slot;
        rewrite each def of r  → store r → slot   (+ bir::sync::Wait)   // sequenced vs SP engine
        rewrite each use of r  → reload slot → r  (+ bir::sync::Wait)
```

> **GOTCHA — a spilled register becomes a `MemoryLocation`, not a stack frame.** The spill map is `DenseMap<bir::Register*, bir::MemoryLocation*>`: a scalar register that cannot be colored is materialized into SBUF/local store and reloaded around each use, with `bir::sync::Wait` instructions threaded in so the spill stores/loads are ordered against the SP engine's sequencer. There is no spill stack and no register-window. This is the inverse direction of an LSA memory spill, which goes the other way — memory to smaller-footprint memory (§3.4).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `RepT<REG_Allocator>::allocateImpl` | `0x98b010` | builds stack colorer, calls `allocate` | CERTAIN |
| `REG_Allocator::allocate(Function*)` | `0x9cbaf0` | the per-function spill fixpoint driver | CERTAIN |
| `build_ssa_representative` | `0x9ce0b0` | SSA value-numbering of registers | CERTAIN |
| `renumber_locations` | `0x9c8580` | assigns location id = `numPhysicalRegs` (+396) | CERTAIN |
| `recordFirstDefLastUse` | `0x9c8e10` | per-register live-range endpoints | CERTAIN |
| `live_range` | `0x9b5930` | recompute live ranges | CERTAIN |
| `coalesce_ssa_live_range` | `0x9cead0` | SSA-copy coalescing (`getRepresentative @0x9c73e0`) | CERTAIN |
| `build` | `0x9af510` | interference graph (`SparseSet`, Universe=#regs) | CERTAIN |
| `find_costs` | `0x9b2130` | spill cost (`+= numPhysicalRegs` per def) | CERTAIN |
| `simplify` | `0x9bc880` | Briggs simplify / optimistic stack push | CERTAIN |
| `select` | `0x9b9de0` | single-score color assignment | CERTAIN |
| `insert_spill_code` | `0x9c10d0` | reg → `MemoryLocation` spill (`DenseMap`) | CERTAIN |
| `propagate_coalesced_ssa_allocation` | `0x9cef00` | push color through coalesced copies | CERTAIN |
| `linearize(Function*)` | `0x9ca0e0` | flattens BBs to one interference scope | CERTAIN |
| `candidate(MemoryLocation&)` | `0x9cda10` | `return 0;` stub (no MemLoc candidates) | CERTAIN |
| `get_defs_reg` / `get_uses_reg` | `0x986710` / `0x988370` | register use-def edge harvest | CERTAIN |

### Considerations

`linearize` `@0x9ca0e0` ("linearize and check (REG)") flattens `bir::BasicBlock`s into one linear instruction order so register live ranges that cross basic blocks sit in a *single* interference scope — the same flattening idea the loop-aware memory colorer uses ([`allocator-drivers`](allocator-drivers.md#the-loop-flattening-linearize)), but applied to scalar registers. The hard register-file size that bounds `select` is `HwmCore->MaxRegNumPerEngine` — asserted, but the literal integer is not pinned in this slice. [`sp-engine`](../arch/sp-engine.md) carries a [SPECULATIVE] 64; do not hard-code it.

---

## LinearScanAllocator — the interval allocator

### Purpose

`LinearScanAllocator` (pass `linear_scan_allocator`) is an *alternative memory allocator*: a drop-in replacement for `coloring_allocator_{sb,psum}` that allocates the **same** `bir::MemoryLocationSet` symbolic tensors but with a classic linear-scan algorithm instead of a graph colorer. It is selected by `--allocator=lsa` on the opt6 path or forced as the *sole* SBUF/PSUM allocator at optlevel 8. It handles both spaces one pass each, keyed off the `MemoryType` at `this+88` (PSUM if `==32`, else SB — log channel switch in `allocate(Function*) @0xb0e670`). Concrete 2-D placement is delegated to a `BestFitMemoryManager` free-list backend.

> **QUIRK — the colorer vs the scan, in one sentence.** The colorer builds a full interference **graph** and runs Chaitin–Briggs simplify/select; LSA scans intervals **once** in program order, expiring dead ones, and hands each live interval to a best-fit free-list manager for concrete placement. LSA is faster and lower-quality, and is **single-vNC-core only** — the multi-core assert in `sub_80D9D0` L853 forbids it.

### Entry Point

```text
LinearScanAllocator(PassOptions&, function<unique_ptr<MemoryManager>(Module&)>)  @0xb15740
  ── ctor takes a MemoryManager FACTORY (2nd arg); default lambda @0xb15b70 builds BestFit
  ├─ run(Module&)                @0xb0eeb0   ── non-loop variant
  ├─ run_with_loops(Module&)     @0xb14fc0   ── "Linear Scan Allocator(LIW)"; init→alloc→exit ×2 (PSUM,SB)
  └─ allocate(Function*)         @0xb0e670   ── per-space; channel = (this+88==32 ? "PSUM" : "SB")
  registered by register_generator_linear_scan_allocator__  (BSS @0x3dff588)
```

### Algorithm

```c
// LinearScanAllocator::run(Module&)  @0xb0eeb0 — scan, expire-old, allocate, spill-at-interval
function LSA_run(Module& M):
    Live_Range_Manager lrm; lrm.init(M, memType, loopAware);   // @0xb323f0 → initLiveRanges → initLiveRange
    for each instruction I in linear program order:
        track nextUse against I's input/output AccessPatterns;

        // 1. EXPIRE-OLD-INTERVALS
        for each active interval iv:
            if (lrm.getRemUses(iv) == 0):                       // @0xb2f8c0 "has no more uses"
                lrm.kill(iv); lrm.erase(iv);                    // "erased"
                deAllocate(iv->loc);                            // @0xb0a1f0 — free the memory back

        // 2. ALLOCATE the instruction's output interval
        allocate(outSet);                                       // @0xb0d1d0 :
            assert(outSet->getType() == lrm.getMemType());
            if (!isAllocated(outSet)):
                if (!BestFit.allocate(outSet->loc)):            // §3.5 — 2-D placement
                    // 3. SPILL-AT-INTERVAL on placement failure
                    log("Failed to allocate <name>");
                    for each cand in active liveSet (sorted by liveSetComparator):
                        log("<idx>: <name> (<MemoryType>)");
                    victim = lrm.pickForSpilling();             // @0xb2b020 = HEAD of sorted set
                    spillMemLocSet(victim, I);                  // §3.4
                    retry BestFit.allocate(outSet->loc);
                markAllocated(outSet); lrm.makeLive(outSet);    // @0xb2fb11
                footprint += lrm.getLiveSize(outSet);
    addSpillInstsCanread();                                     // @0xb13ec0 — finalize deferred reloads
```

### The active set — `Live_Range_Manager` and its comparator

The active set is the linear-scan heart: an `std::_Rb_tree<info*, …, bool(*)(info*,info*)>` keyed by `liveSetComparator` `@0xb2ba60` — a **sorted active list**, with `info*` the per-interval node. The comparator orders by, in priority:

```c
// Live_Range_Manager::liveSetComparator(a, b)  @0xb2ba60  (layout exact; semantics read from use)
function liveSetComparator(info* a, info* b):
    if (*(int*)(a->loc+76) != *(int*)(b->loc+76))
        return *(int*)(a->loc+76) > *(int*)(b->loc+76);   // 1. interval-end weight DESC (head = furthest)
    if (name(a) != name(b)) return name(a) < name(b);     // 2. tie: tensor-name compare (SSO len tests)
    if (apMembership(a) != apMembership(b))                // 3. tie: AccessPattern def/use ownership
        return apMembership(a);
    int c = memcmp(a->name_buf, b->name_buf, …);           // 4. tie: name byte-buffer (loc+296/+304)
    return c ? c < 0 : a->loc->size < b->loc->size;        //    then size (loc+304)
```

`pickForSpilling()` `@0xb2b020` returns `**(QWORD**)(activeTree.begin.node+32)` — the **leftmost** (head) of this sorted tree, i.e. the highest interval-end-weight interval is chosen as the spill victim.

### Spill — copy-and-shrink + reload

```c
// LinearScanAllocator::spillMemLocSet(MemLocSet* loc, Inst* I)  @0xb0a870
function spillMemLocSet(loc, I):
    log("Spilling <name>");
    spillInst  = make_store(loc, I);     assert(spillInst.getPrivateIndex()  == 0);
    reloadInst = make_reload_at_next_use(loc);
    log("It will be reloaded at <inst>"); assert(reloadInst.getPrivateIndex() == 0);
    MemoryLocationSet* copied = copy_of(loc);   // copiedLocationSet == &loc
    copied->setShrinkDim(...);                   // spill to a REDUCED footprint (toward DRAM)
```

> **NOTE — LSA spill is memory→smaller-memory, the inverse of REG spill.** Where `REG_Allocator` spills a *register* into a *memory* slot (§2), `LinearScanAllocator` spills a *memory* interval into a *smaller-footprint* copy via `MemoryLocationSet::setShrinkDim` and reloads it — staying in the memory domain. Both insert store/reload pairs; the domains differ.

### Placement backend — `BestFitMemoryManager`

```c
// BestFitMemoryManager::allocate(MemoryLocation&)  @0xb241a0
//   holds a boost::multi_index_container<MemoryBlock> with THREE indices:
//     sequenced       — free blocks in ADDRESS order
//     hashed_non_unique by getMemoryLocation()
//     sorted_by_size  — ordered_unique with SizeCompare  ← the BEST-FIT search key
//   + two p_square_cumulative_distribution<float> accumulators for fragmentation stats
function BestFit_allocate(MemoryLocation& loc):
    cons = getAddressConstraints(loc);                 // @0xb22cd0 — per-tensor align / bank
    if (loc.type == PSUM):
        return tryAllocatePSUM(loc, cons);             // @0xb1b5f0 — bank-addressed, no 2-D corner
    // SB: 2-D rectangle packing in the (partition × byte) grid
    return tryAllocateLowerLeft(loc, window)           // @0xb20310
        || tryAllocateUpperLeft(loc, window)           // @0xb20ae0
        || tryAllocateRight(loc, window);              // @0xb212b0
```

> **QUIRK — SBUF is 2-D, PSUM is 1-D, even inside the "best-fit" manager.** The SB path tries three corner-placement strategies (lower-left / upper-left / right) against a `(partition × byte)` rectangle window; the PSUM path is a separate bank-addressed allocator with no corner search, because PSUM is a small array of fixed banks rather than a 2-D scratchpad ([`sbuf-psum-geometry`](../arch/sbuf-psum-geometry.md)). One manager class, two geometrically different placement routines selected by `MemoryType`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LinearScanAllocator` ctor | `0xb15740` | takes a `MemoryManager` factory (default → BestFit) | CERTAIN |
| `run(Module&)` / `run_with_loops` | `0xb0eeb0` / `0xb14fc0` | scan driver (non-loop / `(LIW)` twice) | CERTAIN |
| `allocate(Function*)` | `0xb0e670` | per-space entry; channel from `this+88` | CERTAIN |
| `allocate(MemLocSet*)` | `0xb0d1d0` | place one interval (asserts MemType match) | CERTAIN |
| `spillMemLocSet` | `0xb0a870` | copy+`setShrinkDim`+reload | CERTAIN |
| `addSpillInstsCanread` | `0xb13ec0` | finalize deferred spill-reads | CERTAIN |
| `deAllocate` | `0xb0a1f0` | free an expired interval's memory | CERTAIN |
| `Live_Range_Manager::init` / `initLiveRange` | `0xb323f0` / `0xb312c0` | build per-tensor `[firstDef,lastUse]` | CERTAIN |
| `getRemUses` | `0xb2f8c0` | remaining-uses count (drives expiry) | CERTAIN |
| `makeLive` / `kill` / `erase` | `0xb2fb11` / `0xb2f7f0` / `0xb2fad0` | active-set membership | CERTAIN |
| `pickForSpilling` | `0xb2b020` | head-of-sorted-set victim | CERTAIN |
| `liveSetComparator` | `0xb2ba60` | active-set ordering (4-key) | CERTAIN (layout) / HIGH (semantics) |
| `BestFitMemoryManager` ctor / `allocate` | `0xb21fa0` / `0xb241a0` | multi-index free-list + dispatch | CERTAIN |
| `tryAllocate{LowerLeft,UpperLeft,Right}` | `0xb20310` / `0xb20ae0` / `0xb212b0` | SB 2-D corner placement | CERTAIN |
| `tryAllocatePSUM` | `0xb1b5f0` | PSUM bank placement | CERTAIN |
| `getAddressConstraints` | `0xb22cd0` | per-tensor align/bank | CERTAIN |

### Considerations — the opt8 pipeline

At optlevel 8 (`sub_809580`, ~35 passes) `linear_scan_allocator` is the **sole** SBUF/PSUM allocator; DRAM still uses `coloring_allocator_dram_debug`. The pipeline is leaner than opt6/opt7 — no with-loop allocator, no PSUM coloring pass:

```text
… → pre_sched → linear_scan_allocator → lower_select → … → coloring_allocator_dram_debug
   → assign_hwdge_engine → alloc_queues → chain_dma_transposes → lnc_barriercheck
```

The interval `loc+76` field that drives `liveSetComparator` priority 1 is [INFERRED] to be the interval-end / spill-priority weight. It is printed alongside `MemoryType2string` at the failure site, but its exact semantic was not isolated.

---

## Optlevel-7 "SMT allocation" — NOT a solver

### Purpose

`--smt-allocation` advertises itself as an alternative allocator. It is not one. `SmtAllocation` is an `llvm::cl::opt<bool>` (`@0x3dfa180` BSS, arg `"smt-allocation"`, desc `"Doing smt allocation"`, set up in walrus_driver `main` `sub_7C2890`); `run_backend_driver` packs its bit into `PassOptions`, and the dispatcher (`sub_80D9D0` L815) does `if (SmtAllocation) optlevel = 7`. Optlevel 7 is a normal `BackendPassManager` pipeline of graph-**coloring** passes — the same family-(a) `coloring_allocator_{dram,dram_shared,sb}` as the default path, reordered, plus an in-place re-verification.

### The only "smt" runtime artifact — an in-place verifier

```c
// Unroll::check_in_place(Function&)  @0xb361f0 — the "smt mode" self-consistency check
function check_in_place(Function& fn):
    for each MemoryLocation mem in fn:
        bool ok = mem.allocate(mem.getType(), mem.getAddrSpace(), mem.getAddress(),
                               mem.getBankId(), mem.getBasePartition(), mem.isPinned());
        assert(ok && "smt allocation fails in-place write checker");
```

This re-materializes every `MemoryLocation` at its *exact* type/addrspace/address/bank/partition/pinned tuple and asserts the placement succeeds — a verifier that the (already-fixed) assignment is self-consistent, **not** constraint solving.

> **GOTCHA — there is no objective, no encoding, no UNSAT, and no timeout.** Building against a "Z3 finds an optimal allocation, with an UNSAT fallback" model produces a solver this binary does not contain. The reading that fits the code is that `smt-allocation` is a developer or experimental mode treating allocation as a **fixed, pre-determined assignment** to be reproduced and verified in place — plausibly fed from an upstream NKI or pre-solved source, though no external-allocation-file string survives in this build. Allocation failure surfaces as the ordinary coloring overflow assert (`"Allocation failed for v128/…"`), never as a solver UNSAT.

### The distinctive opt7 trait

The one thing that makes opt7 *structurally* different from the default coloring path is ordering: DMA optimization + address rotation run **before** the SB coloring, and there is **no** PSUM coloring pass (PSUM is handled via `dma_optimization`/`address_rotation` + the in-place checker). The tail of the opt7 pipeline:

```text
… → dma_optimization_psum → address_rotation_psum
  → dma_optimization_sb (+memory_analysis) → address_rotation_sb → address_rotation_psum
  → coloring_allocator_sb (+memory_analysis_after_coloring_allocator_sb) → address_rotation_sb
```

---

## DMAOptimization — the post-allocation DMA peepholes

### Purpose

`dma_optimization_psum` (pipeline order 45) and `dma_optimization_sb` (order 54) are **one** class, `neuronxcc::backend::DMAOptimization` (0x8e0 B, `run @0x966740`). They run *immediately after* the graph-coloring allocator has bound logical tensors to physical addresses **and** inserted spill/reload DMAs (`coloring_allocator_psum @44` → `_psum` opt @45; `coloring_allocator_sb @51` → `_sb` opt @54). The "GCA" in every report string is **G**raph-**C**oloring **A**llocator; these passes are the post-allocation cleanup that brackets the spill/reload-insertion phase (the `Pre/PostGcaDMAAccesses` counters, §6). They follow the DMA *legalization* of [`dma-legalization`](dma-legalization.md) — legalize makes each DMA valid; optimize makes the valid set cheaper.

> **NOTE — PSUM vs SB is data-driven, not two classes.** The two registration invokers build the **same** object and differ *only* in the stored name byte-suffix (`"…_psum"` len 21 vs `"…_sb"` len 19) and the `MemoryType` they work on (PSUM=0x20 / SB=0x10). `run()` reads the name + `has_uncovered_live_range(Module, MemoryType)` to gate which sub-optimizations fire and routes every rewrite through the space-correct address-rotation helper: `AddressRotation::update_psum_partition` for PSUM banks, `sb_rotation` for SB partitions.

### Entry Point

```text
DMAOptimization::run(bir::Module&)   @0x966740 (~9.4 KB)
  ├─ skip_pass(Module&)              @0x9188c0  ── SKIP iff module has ZERO DMA-bearing insts (cheap early-out)
  ├─ per-Function: CFG ctor → check_has_far_jump → obj+0x618 (cross-loop coalesce guard)
  └─ ordered worker dispatch (PSUM/SB select via name + MemoryType):
       DMA_analysis ("Pre GCA") → input_dma_coalescing → reload_for_save_optimization
       → load_merging (§ redundant-removal) → remat_load_shrink → DMA_analysis
       → spill_optimization → extra_save_removal → remove_memset_spill → dce
       → internal_dma_coalescing (§ coalescing) → partial_DMA_access (§ partial-DMA)
       → reload_for_save_optimization → DMA_analysis ("Post GCA") → re_enable_opt
       → PSUM_spill_optimization  (PSUM path only)
psum invoke @0x96ec40 / sb invoke @0x96edb0 — same ctor, differ only by name string
```

### The three peephole families

#### Coalescing — merge abutting DMAs (`internal_dma_coalescing @0x9654f0`)

This is the **post**-allocation analogue of the pre-allocation input coalescing of [`separate-load-prefetch`](separate-load-prefetch.md). Pre-alloc, no addresses exist, so it merges two *input tensors* into one wider tile; here, physical addresses are **known**, so it merges DMAs whose physical byte ranges *abut* within a partition into one wider descriptor.

```c
// internal_dma_coalescing(Module&)  @0x9654f0 — merge physically-contiguous DMAs
function internal_dma_coalescing(Module& M):
    for each Function F:
        opt_prepare(F);                                  // live / use-def indices
        for each inst I (getNextInstFlat):
            if   I.kind == 0x16 /*Save*/: save_path(I);
            elif I.kind == 0x13 /*Load*/: load_path(I);
            // addCandidate(I, buckets[129]) @0x5f0140 — bucket by (base-address, partition);
            //   129 = 128 partitions + 1 sentinel
        // a pair (A,B) merges iff ALL hold:                            // disasm 0x9659..0x965b
        //   • single producer→consumer chain (writers/readers AccessPattern locality)
        //   • bir::isFullAccess(AP)            — each AP reads its WHOLE memloc (no partial slice)
        //   • bir::AccessPattern::IsContiguous(AP)  @0x602580 — byte-contiguous on REAL addresses
        //   • element/byte count < 0xBB7 (2999) sanity limit
        //   • !tooLargeForCoalescing(A,B,…)    @0x918640 — per-partition byte budget
        //   • !cross_loop_boundary             — both endpoints share a loop nest
        //   • Instruction::canInstReadFromUninitMem — merged read is legal
        // merge via MemoryMergeTracker union-find:
        //   join(handleA,handleB); find_interval(idA,idB,MMT,lo,hi) places B right after A;
        //   MemoryLocation::allocate_no_check(...) mints the combined tile (addrs already legal);
        //   AddressRotation::update_psum_partition(merged,…) ×4  (PSUM)  or  sb_rotation;
        //   re-point consumers; the two source DMAs collapse to one wider DMA.
```

> **GOTCHA — the budget cap is an ArchModel field, not a JSON scalar.** `tooLargeForCoalescing` `@0x918640` vetoes a merge if the combined per-partition extent exceeds a byte cap fetched by a virtual call into the `ArchModel`/`EngineInfo` (`[obj+0x340]`/`[+0x2c0]`), with a hard `0x3E8 (1000)` element ceiling and an alignment `≤ 8` gate. The *numeric* cap is a constructor immediate on the per-arch model, not a recoverable JSON value — the same provenance gap as the DMA-legalization tile budget. The mechanism is pinned; the value is [INFERRED].

#### Partial-DMA — shrink an over-broad descriptor (`partial_DMA_access @0x95a7f0`)

```c
// partial_DMA_access(Module&)  @0x95a7f0 — descriptor-count minimization
function partial_DMA_access(Module& M):
    for each DMA reading allocated tensor T:
        get_memory_location_used_range(T, &lo, &hi, readers);   // tight sub-range actually touched
        if (used_range ⊊ allocated_range):
            new = create_new_reload_location(orig, …);          // @0x958350 — SMALLER memloc, used range only
            gather_instructions_to_ingore_and_rewire(T, …);     // @0x929480 [sic] — re-point consumers
            AddressRotation::update_psum_partition(new, …);     // re-rotate the trimmed tile
            DeadCodeElim::removeMemoryLocationHelper(T);         // erase the oversized memloc
            BasicBlock::removeInstruction(wide_dma);             // drop the redundant wide DMA
```

A DMA whose AP spans, e.g., `[W,Z,Y,X]` but is read only over a contiguous sub-block is rewritten to a lower-dimensional / shorter descriptor — fewer descriptor dims, smaller transfer. This *complements* coalescing: coalescing **merges** adjacent descriptors; partial-DMA **shrinks** an over-broad one.

#### Redundant-removal — drop a load already resident (`load_merging @0x9534e0`)

```c
// load_merging(Module&)  @0x9534e0 — drop a DMA whose result is already resident
function load_merging(Module& M):
    for each pair of loads (A,B) (or DMACopy-loads):
        if   same_load(A,B)        // @0x923c70 — same kind 0x13/0x16, copy-mode/MemType, queue, uninit flags
          && same_src(MemLoc,A,B)  // @0x934ae0 — identical SOURCE memloc + AP (dim==1, addr+0x4c)
          && same_sb_dst(MemLoc,A,B) // @0x934c20 — identical SB DESTINATION
          && !out_pinned(B)        // never drop a load feeding a pinned output
          && !cross_loop_boundary(A,B) && check_partition_match && check_constraint_use:
            if (B.AP == A.AP):
                merge_load(A,B);     // @0x94bce0 — re-point B's readers onto A; removeInstruction(B)
            else:
                transform_load(A,B); // @0x94dd60 — insert InstTensorCopy to re-apply B's AP onto A's tile
    removeRedundantWrites(...);      // dual: drop Save/write DMAs whose value is never re-read (dead store)
```

### PSUM-specific behavior — why order 45 ≠ order 54

PSUM (`MemoryType 0x20`) is a small banked space (a fixed window of 2 KiB banks, bank id at `MemoryLocation+528`), so every PSUM merge/shrink/spill must respect bank+partition geometry:

- `is_psum_dma(Inst*)` `@0x9234c0` — a DMA is PSUM iff its op has a `PhysicalAccessPattern` whose `MemoryLocation` type `[+0]==0x20`.
- `AddressRotation::update_psum_partition(MemLoc*, vector<vector<u32>>&, int, set<u32>)` `@0x922400` is **recursive** (self-calls ×5) and rebalances which bank/partition the (merged/trimmed) tile lands on; every PSUM rewrite routes through it.
- `PSUM_spill_optimization` `@0x946b70` ("[psum spill optimization]: removed N") is invoked *only* on the psum(45) path; the sb(54) path relies on `spill_optimization` + `AddressRotation::sb_rotation` instead.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `DMAOptimization::run(Module&)` | `0x966740` | the worker dispatch (~9.4 KB) | CERTAIN |
| `skip_pass(Module&)` | `0x9188c0` | "no DMA insts" early-out | CERTAIN |
| `internal_dma_coalescing` | `0x9654f0` | post-alloc contiguous merge | CERTAIN |
| `tooLargeForCoalescing` | `0x918640` | per-partition byte budget veto | CERTAIN (mechanism) / MEDIUM (cap) |
| `coalesc_per_partition_load_only` / `_save_only` | `0x9639a0` / `0x962720` | the merge mechanics | CERTAIN |
| `partial_DMA_access` | `0x95a7f0` | descriptor minimization | CERTAIN |
| `create_new_reload_location` | `0x958350` | mint the trimmed memloc | CERTAIN |
| `load_merging` | `0x9534e0` | redundant-load removal | CERTAIN |
| `same_load` / `same_src` / `same_sb_dst` | `0x923c70` / `0x934ae0` / `0x934c20` | redundancy predicates | CERTAIN |
| `merge_load` / `transform_load` | `0x94bce0` / `0x94dd60` | rewrite (drop / AP-reapply) | CERTAIN |
| `is_psum_dma` | `0x9234c0` | PSUM DMA classifier | CERTAIN |
| `AddressRotation::update_psum_partition` | `0x922400` | recursive PSUM bank rebalance | HIGH |
| `PSUM_spill_optimization` | `0x946b70` | PSUM-only spill cleanup | CERTAIN |
| `DMACopy_insertion` / `dmacopy_remat_optimization` | `0x94fff0` / `0x94e7c0` | IO→Internal tradeoff (§6) | CERTAIN |
| `DMAReport::DMA_analysis` | `0x9252d0` | Pre/Post GCA counters | CERTAIN |

---

## Related Knobs — the DMA-optimization tunables

`--gca-dma-optlevel` (`cl::opt<int>` `@0x3dfb300`, desc `"LIW GCA reload/spill insertion optimization level"`) is read by the **allocator** (`SB_Allocator` ctor `@0xa97750`), *not* by `DMAOptimization::run`. It governs how many spill/reload DMAs the colorer emits, so it *indirectly* scales the workload these passes clean up.

| Knob | Type | Default | Description | Confidence |
|---|---|---|---|---|
| `--gca-dma-optlevel` | INT | **1** | reload/spill insertion level: `{-1 auto, 0 off, 1 default, 2 aggressive}` (`SB_Allocator` branch `cmp eax,1/2/-1`) | CERTAIN |
| `--enableIOToInternalDMACopyInsertion` | BOOL | **false** | enable `DMACopy_insertion` (copy IO→Internal to cut runtime IO-descriptor overhead); the pass is gated behind it ("When on, …") | HIGH (flag pinned; default `false` is MEDIUM) |
| `--repeat-load-thres` | INT | **4** | insert a DMACopy for a LOAD when its reload count exceeds this | CERTAIN |
| `--save-len-thres` | INT | **512** | insert for a SAVE when max save DMA length ≤ this | CERTAIN |
| `--save-dma-cnt-thres` | INT | **32** | insert for a SAVE when #saves for the tensor exceeds this | CERTAIN |

The `DMACopy_insertion` tradeoff is **cost-modeled**: it weighs DMA-ring-slot pressure ("IO to Internal copy instruction increases DMA ring usage by N" / "leads to DMA ring decrease N") against scratch-pad cost ("leads to scratch pad increase") and inserts the copy only when the descriptor-generation saving outweighs the ring/scratch cost. `dmacopy_remat_optimization` `@0x94e7c0` is the inverse — it *re-creates* a value near its use (`MemoryLocation::copyInto` + a fresh `InstLoad`), trading recompute for a shorter live range.

`Pre/PostGcaDMAAccesses` (`backend::PreGcaDMAAccesses` `@0x1c89fde`, `PostGcaDMAAccesses` `@0x1c89ff9`) are formal `BackendMetricType` enum members, emitted by `DMAReport::DMA_analysis @0x9252d0` as before/after snapshots bracketing the pass — the autotuner-visible DMA-traffic delta.

---

## Side-by-Side Contrast

| Allocator | Colors | Algorithm | Placement backend | Spill target | Selected by |
|---|---|---|---|---|---|
| `ColoringAlloc{SB,PSUM,DRAM}` | `MemoryLocationSet` | Chaitin–Briggs + best-of-N | interference graph (self) | DRAM (spill/reload) | default (opt1/2/3) |
| `ColoringAllocWithLoop` | `MemoryLocationSet` | C–B + loop flattening | interference graph | DRAM | opt6 |
| **`REG_Allocator`** | `bir::Register*` (scalar SP) | C–B, **single** REG-score + spill fixpoint | `SparseSet` interference | **`MemoryLocation`** (reg→MemLoc map) | **always** (`sub_805870`) |
| **`LinearScanAllocator`** | `MemoryLocationSet` | linear scan: sorted intervals, expire-old, spill-at-interval | **`BestFitMemoryManager`** (2-D best-fit + PSUM bank) | DRAM (copy+`setShrinkDim`) + reload | **`--allocator=lsa` / opt8** |
| "SMT" (opt7) | `MemoryLocationSet` | = ColoringAlloc passes, reordered + in-place verify; **no solver** | (same as ColoringAlloc) | (same) + `check_in_place` | `--smt-allocation` |

Common to all: `bir::Register`/`MemoryLocation` use-def edges, the `MemoryType` discriminator (SB=16/PSUM=32/DRAM=8), and the always-run REG pass in `sub_805870`.

---

## Confidence and Gaps

**Pinned by dynsym names and disassembled bodies:** the optlevel→allocator selection switch in `sub_80D9D0`; the `REG_Allocator` spill-fixpoint cycle and its `candidate`-stub / reg→`MemoryLocation` spill; the `LinearScanAllocator` scan/expire/spill loop, the `Live_Range_Manager` comparator layout and `pickForSpilling`-head rule, and the `BestFitMemoryManager` three-index multi-index + corner-vs-bank dispatch; the `DMAOptimization` run() dispatch order; coalescing legality (`IsContiguous` + `isFullAccess` + `tooLargeForCoalescing`); `load_merging` redundancy predicates; `partial_DMA_access` shrink path; the IO→Internal thresholds 4/512/32; `--gca-dma-optlevel` default 1 / levels {-1,0,1,2} read by `SB_Allocator`; and the absence of any solver in the SMT path (no Z3 or GMP references; `check_in_place` is a verifier).

**Read from use rather than byte-walked:** the `liveSetComparator` *semantics*, whose layout is exact; the recursive PSUM bank-rotation in `update_psum_partition`; the 129-entry `addCandidate` bucket as 128 partitions plus a sentinel; and the reading of `smt-allocation` as a fixed upstream assignment.

**[INFERRED]:** the exact per-partition byte cap inside `tooLargeForCoalescing` (an ArchModel/EngineInfo ctor field, not a JSON scalar); the precise semantic of the `loc+76` interval-end weight that orders the LSA active set; the exact `FunctionAttribute` gates branched in `run()` (likely `ready_for_codegen` / `no_spill`).

**Gaps:** the colorer *bodies* of the SBUF/PSUM memory allocators are out of scope here (owned by [`allocator-drivers`](allocator-drivers.md) / [`sbuf-liveness-interference`](sbuf-liveness-interference.md)); `MaxRegNumPerEngine`, the literal SP register-file size bounding REG `select`, is asserted but not pinned to a numeric in this slice ([SPECULATIVE] 64, per [`sp-engine`](../arch/sp-engine.md)); and the arch-revision constants `0xA(10)` / `0x13(19)` that gate the DMA-opt constant tables are [SPECULATIVE] Trainium-core ISA versions.

---

## Related Components

| Name | Relationship |
|---|---|
| `ColoringAllocator` (K01–K10) | the principal allocator these alternatives replace; the GCA the DMA opts clean up after |
| `AddressRotation` | the per-space partition/bank re-rotation every DMA-opt rewrite routes through |
| `DMA legalization` | makes each DMA valid; `dma_optimization` then makes the valid set cheaper |
| `non_ssa_legalization` | re-legalizes SSA-clones to fixed slots after PSUM+SB coloring |

## Cross-References

- [Allocator Drivers](allocator-drivers.md) — the ColoringAllocator family (K01–K10), the optlevel→family selection, the loop-aware linearize
- [Pass Pipeline and Optlevels](pass-pipeline-optlevels.md) — the full per-optlevel pass planes; which optlevel selects which allocator
- [SP Engine](../arch/sp-engine.md) — the scalar register file `REG_Allocator` colors; the register struct and `MaxRegNumPerEngine`
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — why SBUF is 2-D and PSUM is 1-D-banked; the geometry `BestFitMemoryManager` places into
- [DMA Legalization](dma-legalization.md) — the DMA legalization these optimization passes follow
- [Address Rotation](address-rotation.md) — `update_psum_partition` / `sb_rotation`, the space-specific re-rotation the DMA opts invoke
- [Separate Load / Prefetch](separate-load-prefetch.md) — the *pre*-allocation input coalescing that `internal_dma_coalescing` mirrors post-allocation
