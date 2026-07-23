# The PSUM Allocator — Bank Model

> *All addresses on this page apply to neuronx_cc 2.24.5133.0 (`libwalrus.so`, build `58f8de22`). VA == file offset for `.text` (base `0x62d660`) and `.rodata` (base `0x1c72000`); `0x5e9020`–`0x62d650` is the `.plt`-thunk band, so a low `0x5ex/0x6xx` address keyed onto a method name is a thunk — the real body lives high in `0xax/0xbx`. Other versions will differ. All findings derive from static binary analysis (IDA Hex-Rays sidecars, disassembly, and `.rodata` strings).*

## Abstract

PSUM (Partial-SUM, `MemoryType::PSUM = 32`) is the matmul accumulator space the PE systolic array drains into. The backend colors tensors into PSUM with the **same Chaitin–Briggs register allocator** it uses for SBUF — liveness, an interference graph, spill costs, simplify, select, and a best-of-n spill fixpoint — but with one decisive structural difference: **PSUM is not byte-addressable.** Where SBUF is a 2-D `(partition, byte)` rectangle space colored by a 2-D first/best-fit ([8.17](sbuf-liveness-interference.md), and the SBUF spill/select page 8.18), PSUM is a flat array of fixed **2 KiB banks**, and a tensor occupies a whole number of banks. That collapses the colorer one dimension: the footprint is a bank count, the interference degree test is **additive bank counting**, the placement is a **1-D first-fit over a bank bitmap**, and spill is **a `TensorCopy` DMA out of PSUM and back** — there is no PSUM-to-PSUM spill space. The result is a strictly leaner allocator than the SBUF one: no partition axis, no shrink-block rectangle, no per-partition-block sub-intervals, no Briggs coalescing, no geometric impact LUT.

This page recovers the PSUM-specific machine: the bank constants, the bank footprint and its capacity guard, the additive degree test that replaces SBUF's geometric `K`, the 1-D bitmap placement, and the `TensorCopy` spill. The bar is that a reimplementer can build the PSUM colorer from the SBUF one by substituting bank geometry for partition×byte geometry, and can predict the exact diagnostic strings and asserts it emits. The contrast with the SBUF colorer is the headline and is drawn at every step. The shared Chaitin–Briggs skeleton, the driver that runs PSUM **first** and freezes it, and the loop-flattening linearization are documented in [8.16](allocator-drivers.md); the bank geometry constants themselves are in [1.05](../arch/sbuf-psum-geometry.md).

For reimplementation, the contract is:

- **Bank model:** PSUM = an array of `NUM_PSUM_BANKS` banks, each `getPsumBankSize() = 2048` bytes. A tensor's footprint is `liveN = ceil(bytes / 2048)` banks (`getPsumBankNum`).
- **Capacity guard:** `NUM_PSUM_BANKS < liveN` ⇒ fatal `"is too big for PSUM, requires N banks with PSUM size M"`.
- **Additive degree test:** a node is trivially colorable iff `NUM_PSUM_BANKS − own_banks − Σ neighbor_banks ≥ 0`. No geometric impact LUT.
- **1-D placement:** build a free-bank bitmap by OR-ing colored neighbors' banks, first-fit the lowest base bank with `liveN` free banks, assign `address = base bank < NUM_PSUM_BANKS`.
- **`TensorCopy` spill:** save asserts source is PSUM, mints `<t>_SpillTensorCopy<id>` to SBUF/DRAM; reload asserts destination is PSUM, mints `<t>_ReloadTensorCopy<id>` back.

| | |
|---|---|
| **Colorer class** | `ColoringAllocatorWithLoop::Rep::PSUM_Allocator` (loop-aware; `psum_*_with_loop.cpp`) |
| **Driver** | `Rep::allocate` `@0xa87030` → `PSUM_Allocator::allocate` `@0xad7140` (PSUM **first**, then SBUF) |
| **Constructor** | `@0xad9970` — caches `NUM_PSUM_BANKS` at allocator `this+0x0` |
| **Bank size** | `Hwm::getPsumBankSize()` = **2048** (literal, all arches) — `@0x1851900` / `@0x1857cf0` / `@0x185e570` |
| **Banks/tensor** | `getPsumBankNum` `@0x1088bd0` = `divideCeil(byteSize, 2048)` |
| **Candidate gate** | `candidate` `@0xae4740` — `MemoryType == 32` |
| **live_range** | `@0xadece0` — bank footprint + loop extension (§ live_range) |
| **build** | `@0xadad30` — interference graph + `"% PSUM demand before spilling"` |
| **find_costs** | `@0xae1ba0` — spill cost in bank·cycles |
| **simplify** | `@0xae7f90` — additive-bank degree test (§ simplify) |
| **select** | `@0xae4fc0` — 1-D bank-bitmap first-fit (§ select) |
| **insert_spill_code** | `@0xaef160` — `TensorCopy` save/reload (§ spill) |
| **Per-node `Info`** | 128-byte stride; `+0x54` = `liveN` (banks); `+0x5C` = `address` (base bank) |

## The PSUM bank model

The geometry difference is the whole story, so it comes first. PSUM is organized as a small array of fixed-size banks — *not* the 128-partition × byte rectangle the SBUF allocator searches. The bank size is a hard constant, identical across all three hardware-model classes:

```c
// TrainiumHwm::getPsumBankSize @0x1851900  (Gen3Hwm @0x1857cf0, CoreV4Hwm @0x185e570 identical)
__int64 TrainiumHwm::getPsumBankSize(TrainiumHwm *this) {
    return 2048;                              // three literal returns, one per arch class
}
```

These are virtual; the colorer reaches the live one through the `Hwm` cost-model singleton's vtable slot `+0x78` (`= +120`). `bir::Hwm::getSingleton()` returns the per-arch oracle selected from `bir::Module::getArch`. The banks-per-tensor accessor is `getPsumBankNum`, decompiled cleanly:

```c
// neuronxcc::backend::getPsumBankNum @0x1088bd0
__int64 getPsumBankNum(neuronxcc::backend *loc /*MemoryLocation* via 'this'*/) {
    // assert parent module is set, then:
    Singleton = bir::Hwm::getSingleton();
    bankSize  = (*(vtable[+120 /*0x78 = getPsumBankSize*/]))(Singleton);   // = 2048
    if (!bankSize) __assert_fail("Denominator && \"Division by zero\"", "llvm/Support/MathExtras.h", ...);
    // **((QWORD**)loc + 33)  =  MemoryLocation byteSize accessor  (MLoc + 0x108)
    bankNum = divideCeil( byteSize, bankSize );                            // = ceil(bytes / 2048)
    return bankNum;
}
```

So **banks-per-tensor = `ceil(tensorBytes / 2048)`** — confirmed verbatim: the `divideCeil` idiom (`(x − (x!=0))/d + (x!=0)`) appears literally in the body, and the divisor is the `+0x78` vtable slot. Two siblings round this out: `getPsumBankAlignment` `@0x1088ad0` (bank-stride alignment class, `1` when `size ≤ bankSize`, else a HW lookup via `Hwm` vtable `+0x80`) and `getAlignedPsumBankNum` `@0x1088cb0` (returns `bankNum` for arch field `+172 ≤ 39`, else rounds up to the alignment quantum).

The total bank count — the `PSUM_SIZE` the colorer compares against — is cached at allocator `this+0x0` by the constructor `@0xad9970`, read out of the per-arch `ArchModel`:

```c
// PSUM_Allocator ctor @0xad9970  (the cache write)
*(uint*)this = **(uint**)( *(uint64*)( *(uint64*)(ArchModel + 8) + 16 ) + 32 );  // = NUM_PSUM_BANKS
//             getArchModel(getArch) → Board+0x8 → Device+0x10 → Core → Psumbuf+0 (numBanks)
```

The per-arch integer is **table-resident, not a literal in the colorer** — it lives in the `Psumbuf` geometry record's `numBanks` field. [1.05](../arch/sbuf-psum-geometry.md) recovers those records directly: **4 banks** on gen1 Inferentia (64 partitions), **8 banks** on gen2/gen3/gen4 (128 partitions), each bank 2048 bytes — an 8 KiB / 16 KiB per-partition PSUM window. Throughout this page, `NUM_PSUM_BANKS = allocator this+0x0` denotes that cached count.

> **NOTE — `this+0x0` is the bank count, not a byte budget.** The SBUF colorer caches `SB_SIZE` *bytes* at `allocator+0x2b8` (the field earlier reports called "a1+696"). The PSUM colorer caches a small *bank count* at `allocator+0x0`. The two budgets are different units in different slots; a reimplementer must not conflate them. A separate codegen-side accessor, `KlirToBirCodegen::getPsumMaxBankId` `@0xf14430`, returns `(psumTotalSize−1)/psumBankStride` (max bank ID = `#banks − 1`); the colorer does not use it — it has its own cached count.

## Where the PSUM colorer sits

PSUM is colored **first**, then frozen, then SBUF runs ([8.16](allocator-drivers.md)):

```c
// ColoringAllocatorWithLoop::Rep::allocate @0xa87030
lf = Rep::linearize(function);          // @0xa89e70 — loops INLINED (flattern_loop, op==105 recursion)
PSUM_Allocator ctor(opts, module, fn);  // @0xad9970 — caches NUM_PSUM_BANKS @ this+0
PSUM_Allocator::allocate(lf);           // @0xad7140 — *** PSUM FIRST, frozen ***
SB_Allocator::allocate(lf);             // @0xa95310 — consumes the PSUM-mutated lf
```

PSUM goes first because its RMW-pinned matmul-accumulator banks become immovable constraints the SBUF colorer must respect, and both share one `LinearizedFunction` (loop bodies flattened) so loop-crossing live ranges stay consistent. The driver `allocate@0xad7140` is the only PSUM method Hex-Rays failed on; its phase order is recovered from disassembly call targets and `.rodata` banners (`"  allocating PSUM"` `@0xad7148`):

```
renumber_locations  @0xad726c  → this+0x2b0 (688) = #candidate nodes
memloc_split        @0xad727a  (psum_memloc_split: split multi-range memlocs)
renumber_locations  @0xad7305  (re-run after split)
candidate scan      @0xad7421  (gate: memType==32; isa<AccessPattern>)
── best-of-n / spill fixpoint  ("best-of-n loop, heuristic = " @0xad75fd) ──
  DenseMap::clear ×2 @0xad760b/@0xad7617   (clear firstDefs@this+0x2d8, lastUses@this+0x2f0)
  live_range  @0xad762f   build  @0xad763d   find_costs  @0xad764b
  simplify    @0xad7ce6   select @0xad7d09 → Locations* + score←&double
  "PSUM score = " @0xad7e55 … "best PSUM heuristic = " @0xad83b3
  [if select left a non-empty spill DenseSet]:
     insert_spill_code @0xad8a25   "spilling from PSUM cost about " @0xad8ae6 " cycles"
     loop (with the spilled+reloaded lf)
  "number of tensors spilled from PSUM = " @0xad8bc4 ; "% PSUM utilization after allocation" @0xad8c7a
```

This is the same `{liveness → interference → cost → simplify → select → spill, wrapped in best-of-n}` skeleton as SBUF — confirming one shared Chaitin–Briggs algorithm differing only in geometry and special cases.

> **ROSTER CONTRAST.** The PSUM colorer's method roster is a strict *subset* of the SBUF one. It has **no `find_partners`** (no Briggs coalescing of accumulation groups at the colorer level), **no `find_first_defs` / `find_last_uses` / `find_loads`** (the def/use anchors are read straight off `Info+0x20` / `Info+0x28`), and **no `create_eintervals`** (no 2-D `eInterval{start,end,block}` list — PSUM has no per-partition block sub-axis). The leaner roster *is* the geometry simplification made structural.

## live_range `@0xadece0` — the bank footprint

`live_range(this, LinearizedFunction*, Info*)` runs the same liveness skeleton as SBUF over a 128-byte-stride `Info` node array (`a3 + (k<<7)`), but its product is bank counts, not partition-block rectangles. The `Info` layout it touches matches SBUF: `Info+0x10` = `MemoryLocationSet* loc`, `Info+0x20` = first writer (`def`), `Info+0x28` = last reader (`use`), `Info+0x54` = `liveN`.

**Def/use anchors are trusted, not re-scanned.** The body reads `def = Info+0x20`, `use = Info+0x28` and asserts both — verbatim from the decompiled body at lines 505/511:

```c
__assert_fail("def && \"no first def find\"", "psum_live_range_with_loop.cpp", ...);   // @0xadece0:505
__assert_fail("use && \"no last use find\"", "psum_live_range_with_loop.cpp", ...);    // @0xadece0:511
```

The SBUF colorer *populates* these via `find_first_defs` / `find_last_uses` helpers; PSUM has no such helpers — the anchors are set by `memloc_split` / `renumber` upstream and the colorer trusts them. (Same asserts, same `+0x20`/`+0x28` offsets; the producer differs.)

**Loop extension is byte-identical to SBUF** — the `_with_loop` delta. The body takes `Ld = def->getLoopnest()`, `Lu = use->getLoopnest()`, computes their longest common prefix (`commonDepth`), records the shared `LoopAxis` set into a `DenseMap<LoopAxis*, …>`, and stretches the interval: if the def is deeper than the common nest it anchors at the loop **header** (`*(Ld[commonDepth]->block + 104) + 88`, the `InstLoop` region head); the use side scans the loop body's basic blocks for `opcode == 105` (`InstLoop`, the back-edge marker) and anchors last-use at the loop **terminator**. Net: a loop-carried PSUM value is live across the whole loop body, so the interference graph sees the conflict. This is the same mechanism as the SBUF loop extension.

**The bank footprint (the geometry).** `live_range` derives a `shrink_factor` by intersecting partition-axis index sets over the access-pattern footprint (writers at `loc+0x388`, readers at `loc+0x360` — the same `ConvertReaderWriter<AccessPattern>` substrate as SBUF), then sets the live bank count:

```c
// @0xadece0  (decompiled body, lines as noted)
assert( loc->getNBlocks() % shrink_factor == 0 );          // :1304 "getNBlocks() % shrink_factor == 0"
loc->setLiveN( loc->getNBlocks() / shrink_factor );        // :1310 — #LIVE banks
loc->setShrinkDim( shrinkDim );                            // :1311
Info[node]+0x54 = loc->getLiveN();                         // :1378 — bank count cached on the node

// ⭐ THE BANK CAPACITY GUARD  (replaces SBUF's byte-budget "too big for SB"):
if ( *(uint*)this  <  loc->getLiveN() ) {                  // :1344 — NUM_PSUM_BANKS < banks-needed
    // FATAL (severity 50 → abort):
    //   "<name> is too big for PSUM, requires <liveN> banks with PSUM size <NUM_BANKS>"
    log(" is too big for PSUM, requires ");                // :1361
    log(" banks with PSUM size ");                         // :1364
}
```

A PSUM tensor's footprint is therefore **`liveN` banks (1-D)**, not `(liveN partition-blocks × bytesPerBlock)`. The guard at line 1344 is the verbatim contrast with SBUF's byte-budget guard: `*(uint*)this` is `NUM_PSUM_BANKS` (allocator `this+0`), compared `<` against the tensor's `getLiveN()` bank demand — both target strings and the `this+0 < liveN` compare read directly from the body.

**Output: two member DenseMaps.** `live_range` writes its interval product into two *allocator-member* maps — `firstDefs` at `this+0x2d8` and `lastUses` at `this+0x2f0` — each `DenseMap<Instruction*, vector<unsigned>>` keyed by the def/use anchor instruction, valued by the list of node ordinals defined/last-used there. The SBUF colorer takes `firstDefs`/`lastUses` as *caller arguments* and additionally folds them into a per-partition-block `eInterval` list via `create_eintervals`. PSUM's are members (cleared each best-of-n iteration at `@0xad760b`/`@0xad7617`), and there is **no** `create_eintervals` and **no** `eInterval` — because PSUM has no partition-block sub-axis.

> **GOTCHA —** do not mirror SBUF's `eInterval` machinery into PSUM. There is no partition-block sub-axis for it to index.

> **Accumulation residency is implicit.** A matmul accumulation group writes the *same* `MemoryLocationSet` across all its matmuls (the chaining is done upstream — see [Matmul Tile Ordering and Accumulation-Group Legalization](matmul-ordering-accgroups.md)). So its writer-list spans every accumulating matmul, and its `def = first-matmul`, `use = last-reader` live range naturally covers the whole accumulation, pinning the banks RMW-live. The colorer needs no special accumulation code: disassembly of `build` / `find_costs` / `live_range` shows **zero** calls to any `MatmulAccGrp` / zero-region helper (`is_zero_region_bank_overlap` `@0x16da920`, `set_psum_zero_region` `@0x16daff0`, …) — that machinery is upstream (`psum_legalization` / `build_flow_deps`), never colorer-called. The absence is read directly; the upstream binding is read off the caller side.

## build `@0xadad30` — interference and the demand metric

`build(this, LinearizedFunction*, Info*)` constructs the interference graph identically to SBUF: a lower-triangular bit-matrix sized `n = ((this+0x2b0)² + 15) >> 4` bytes (where `this+0x2b0` = #candidate nodes), plus per-node symmetric adjacency lists (`Info+0x38`/`+0x40`) and a degree counter (`Info+0x44`). The edge rule is live-range time **overlap**: sweep the linear instruction points using an `llvm::SparseSet<unsigned>` as the currently-live set; at each point insert `firstDefs[inst]` (`this+0x2d8`) and erase `lastUses[inst]` (`this+0x2f0`); any two co-resident nodes get an edge. Same time-overlap interference as SBUF; only the source maps differ (members vs args).

The PSUM bank-pressure metric is computed here and reported before any eviction decision:

```c
// build @0xadad30   (string read at :1415)
this+0x2c8 = (double)(NUM_PSUM_BANKS /*this+0*/) / total_bank_demand;   // total_demand = Σ liveN over candidates
log( <ratio>, "% PSUM demand before spilling" );                       // :1415 — verbatim target string
```

This is the PSUM analogue of SBUF's `"% SB demand before allocation"`, sited in `build` — the string `"% PSUM demand before spilling"` was found in the decompiled body at line 1415 (and is x-referenced by both this `build` and the family-a `build_no_bitmap` `@0xa437d0`).

## find_costs `@0xae1ba0` — spill cost in bank·cycles

`find_costs` walks the linear points building `needSave` / `needLoad` / `load2copy` sets and accumulates a `double` cost per node at `Info+0x0` (zeroed at line 382). The decisive contrast with SBUF: **cost scales with bank count, not bytes**:

```c
// find_costs @0xae1ba0   (banner "      find costs")
// STORE (spill-save):  cost += getLiveN(loc) * Latency      (:618 "add store cost, remove from needSave ")
// LOAD  (reload):      cost += getLiveN(loc) * Latency      (:820 " need load, add load cost, add to needSave ")
//  …also "need load2Copy, add to needSave " (:1058) — the load-via-TensorCopy variant.
// Un-spillable (RMW-pinned accumulator) nodes → cost = +inf (> 1.79e308)
```

Un-spillable nodes (RMW-pinned accumulators) get `cost = +inf`, classified by `simplify` via `fabs(cost) > 1.797693e308`. SBUF's `find_costs` weights by `CFG::isLoopHeader` and **bytes**; PSUM weights by **bank count × Latency**. Both stamp `+inf` for un-spillable.

## simplify `@0xae7f90` — the additive-bank degree test

This is the headline. The Chaitin simplify partitions nodes into three `llvm::SparseSet` worklists over the `this+0x2b0` node universe — `lo` (SAFE), `hi` (UNSAFE), `inf` (INFINITE) — and the degree-`<K` test that decides which is **pure additive bank counting**, read directly from the decompiled body:

```c
// PSUM_Allocator::simplify @0xae7f90
setUniverse(&lo, #nodes); setUniverse(&hi, #nodes); setUniverse(&inf, #nodes);   // :390-392

for (each node) {
    v11 = *(int*)a1            - *(int*)(node_info + 0x54);   // :402  NUM_PSUM_BANKS − own.liveN
    for (each neighbor)
        v11 -= *(int*)(a3 + (neighbor << 7) + 0x54);          // :412  − neighbor.liveN  (additive)
    // v11 = free banks remaining after this node + all neighbors take their banks

    if      ( v11 >= 0 )                            insert(&lo,  node);  // :420 SAFE (trivially colorable)
    else if ( fabs(*(double*)node_info) > 1.79e308) insert(&inf, node);  // :441 INFINITE (un-spillable)
    else                                            insert(&hi,  node);  //      UNSAFE
}
// trace banners: "            lo = " :483, "            hi = " :499, "            inf = " :515, "            total = " :531
```

The test reads literally: at line 402, `v11 = (NUM_PSUM_BANKS at this+0) − (own banks at Info+0x54)`; the neighbor loop at line 412 subtracts each neighbor's bank count (`Info+0x54`); line 420 classifies `v11 ≥ 0` as SAFE. A node is trivially colorable **iff `NUM_PSUM_BANKS − own_banks − Σ neighbor_banks ≥ 0`**. There is **no 9×9 `vertical_impact` LUT, no `possible_placements`, no partition-band Height class** — all of which the SBUF simplify uses (its `K = possible_placements − Σ impact[9·Hj+Hi]·(span_i+span_j−1)`). PSUM's 1-D bank model collapses "impact" to simply "the neighbor's bank count." This is the cleanest expression of the geometry difference, and it reads directly off the K-computation loop.

The worklist drain is the standard Chaitin contract: SAFE nodes are pushed onto the `NodeStack` (a `vector<int>`; banners `"push back low "` :637, `"neighbor push to low "` :1098), each removal decrements neighbors' remaining-bank budget and promotes newly-SAFE nodes; un-spillable nodes go to `inf`. The push order (= removal order) is the reverse of select's LIFO pop.

## select `@0xae4fc0` — 1-D bank-bitmap first-fit

`select(this, LinearizedFunction*, Info*, NodeStack*, double* score) → Locations*` pops nodes off the `NodeStack` and places each into the lowest free bank window. The `Info` fields used: `+0x38`/`+0x40` neighbor list/count, `+0x54` `liveN`, `+0x5C` `address` (the assigned **base bank index**), `+0x60` per-node live-bank index array, `+0x79` `allocated` bool.

```c
// PSUM_Allocator::select @0xae4fc0
// pop node (assert !allocated when popped from stack), then build the FREE-BANK bitmap:
for (each neighbor /*count Info+0x40, list Info+0x38*/) {
    if ( neighbor_allocated /*Info+0x79*/ ) {
        assert( neighbor.address /*Info+0x5C*/ < PSUM_SIZE /*= *this*/ );          // :749
        //   "info[neighbor_index].address < PSUM_SIZE && \"bad PSUM address during select\""
        for (j = 0; j < neighbor.liveN /*Info+0x54*/; j++) {
            bank = neighbor.live_bank_array /*Info+0x60*/ [j];
            bitmap[(bank >> 3) & 0x1FFFFFF8] |= 1LL << bank;                        // :764  set occupied bit
        }
    }
}
// first-fit: scan the 1-D bitmap for the lowest base bank where this node's liveN banks are free  :772-817
//   on failure → " cannot allocate " (:1058) → node joins the spill DenseSet allocate consumes
// else set address = base bank (Info+0x5C), allocated = 1 (Info+0x79)

// COMMIT (:1519-1542): for each allocated node, set MemoryLocation+0xA8 = 1 (PSUM-placed flag),
//   then for each accessor in the MemoryLocation list call bir::MemoryLocation::allocate()
//   cycling the bank index round-robin across the tensor's liveN banks:
for (k = mloc+0x180; k != end; v169 = (v169 + 1) % *(int*)(a3 + (node << 7) + 0x54) /* % liveN */)  // :1534
    bir::MemoryLocation::allocate();                                                                // :1536
```

Every claim here is verbatim from the decompiled body: the assert string at line 749, the bitmap-set at line 764, the `" cannot allocate "` give-up at line 1058, and the round-robin commit `v169 = (v169 + 1) % liveN` at line 1534 followed by `bir::MemoryLocation::allocate()` at 1536. The placement is a **1-D bank bitmap first-fit**: the `address` field is a **bank index** `< NUM_PSUM_BANKS`, asserted on every neighbor read, with no partition coordinate — PSUM banks span all partitions. SBUF's select instead does a 2-D first/best-fit over partition×byte rectangles (`search_intervals`, `possible_placements` by Height class, partition-band reservations). `select` returns a `Locations*` and writes the score to `*score` (`"PSUM score = … (lower is better)"`); the `DenseSet<MemoryLocationSet*>` of un-placeable nodes flows to the spill phase.

## insert_spill_code `@0xaef160` — spill via TensorCopy

When `select` cannot place a node, it joins a spill set, and `insert_spill_code(LinearizedFunction*, DenseSet<MemoryLocationSet*>*, Info*)` (psum_spill_with_loop.cpp) inserts the spill/reload code, then `allocate` loops with the rewritten function. The decisive contrast: **PSUM has no PSUM-side spill space** — a spilled value is copied *out* of PSUM by a `TensorCopy` DMA and copied *back* into PSUM right before its consumer (a matmul accumulate or activation read can only source PSUM).

**Save** asserts the source is PSUM and mints a `_SpillTensorCopy` memloc:

```c
// insert_spill_code @0xaef160 — SAVE   (verbatim strings)
assert( *(int*)(loc+0xD8) == 32 );   // :2540  "memType == bir::MemoryType::PSUM && \"Expected save from PSUM\""
assert( next_inst != nullptr );      //         "tried to insert PSUM spill at end of block"
mint MemoryLocationSet  "<tensor>_SpillTensorCopy<id>";   // :2569/:2592 (copyInto / sub_96C850)
insert TensorCopy DMA at find_insertion_pos(..., 0 /*SAVE*/, ...);
this+0xAE /*"insert spill " counter*/ ++;
```

**Reload** asserts the *destination* is PSUM and mints a `_ReloadTensorCopy` memloc:

```c
// insert_spill_code @0xaef160 — RELOAD   (verbatim strings)
assert( reload_target_memType == 32 );    // :3088/:3384  "Expected reload to PSUM"  (severity-50 → abort)
assert( spill_location[loc] != nullptr ); //              original→spilled-copy map must be populated
mint MemoryLocationSet  "<tensor>_ReloadTensorCopy<id>";  // :3129/:3155
insert reload TensorCopy at find_insertion_pos(..., 1 /*RELOAD*/, ...);
this+0xAF /*"insert reload " counter*/ ++;
// "too many reloads required"  :3524  — give-up diagnostic when a node can't be relieved by spilling
```

Both endpoint asserts, both `TensorCopy` memloc names, and the `"too many reloads required"` give-up were read directly from the decompiled body (lines 2540, 3088, 2569, 3129, 3524). The reload **destination is PSUM** (asserted — the consuming matmul/activation reads PSUM); the **saved** copy lives in the spill space (SBUF or DRAM, the only larger spaces), with its `MemoryType` set when `copyInto` rehomes it — the colorer asserts only the PSUM endpoints. The single-basic-block invariant `"shouldn't need initial loads with a single basic block"` (`needLoad.size() == 0`, :2162) is asserted. A companion routine `correct_spill_reload` `@0xaed4b0` (Hex-Rays failed; recovered from disasm string loads — `"reference inst has no matching memlocs"`, `"original spill/reload output AP is not physical AP"`) patches the access patterns of instructions that referenced the spilled tensor.

> **Spill-target contrast.** SBUF spills target the DRAM spill space (`_Reload` / `_ReloadStore`). PSUM spills target a `TensorCopy`-reachable copy: the saved value *leaves* PSUM and is reloaded *back* into PSUM before its consumer. The `_SpillTensorCopy` / `_ReloadTensorCopy` naming feeds the downstream `address_rotation_psum` / `dma_optimization_psum` passes (`.rodata`: `"address_rotation_psum"`, `"dma_optimization_psum"`, `"Cannot Rotate PSUM Bank for "`, `"Re-allocate PSUM Bank for "`).

## The twin: family-a PSUM_Allocator

There is a second, non-loop top-level `neuronxcc::backend::PSUM_Allocator` (driven by `ColoringAllocator` `@0x9870c0`, used at opt0–3/7/8): `allocate` `@0xa3ad90`, `build_no_bitmap` `@0xa437d0`, `find_costs` `@0xa4a2e0`, `select` `@0xa53270`, `simplify` `@0xa55fc0`, `spill` `@0xa5f2e0`. It runs the **same** algorithm — it uses the verbatim `"info[neighbor_index].address < PSUM_SIZE && \"bad PSUM address during select\""` assert (found in its `select@0xa53270`) and the same `"% PSUM demand before spilling"` string (x-referenced by both `build@0xadad30` and `build_no_bitmap@0xa437d0`). The two families differ only in source layout: `coloring_allocator_with_loop/src/psum_*_with_loop.cpp` (loop-aware, this page) vs `coloring_allocator/src/psum_allocator/psum_*.cpp` (family-a). This is the same twin structure as the two SBUF colorer families.

## SBUF vs PSUM — consolidated contrast

| Axis | SBUF (`SB_Allocator`) | PSUM (`PSUM_Allocator`, this page) |
|---|---|---|
| Memory model | 128 partitions × byte (2-D rectangle) | array of 2-KiB **banks** (1-D) |
| Candidate gate | `memType==16 && !output` | `memType==32` (`candidate@0xae4740`) |
| Footprint unit | `liveN` partition-blocks × bytesPerBlock | `liveN` **banks** = `ceil(bytes/2048)` |
| Capacity guard | `Info+0x54 > SB_SIZE` "too big for SB" (byte budget) | `liveN > NUM_PSUM_BANKS` "too big for PSUM, requires N banks with PSUM size M" |
| live_range maps | `firstDefs`/`lastUses` = caller args | **members** `this+0x2d8`/`+0x2f0` (cleared/iter) |
| eIntervals | `create_eintervals → {start,end,block}` | **none** (no partition sub-axis) |
| Briggs coalescing | `find_partners` (accum groups) | **none** (accum residency implicit via shared MLSet writer list) |
| def/use anchors | `find_first_defs`/`find_last_uses` helpers | read straight from `Info+0x20`/`+0x28` |
| Interference | bit-matrix + lists, time-overlap | **same** (bit-matrix `n²>>4`, SparseSet) |
| simplify `K` test | `possible_placements − Σ geometric impact` (9×9 LUT) | `NUM_PSUM_BANKS − own − Σ neighbor banks` (pure additive) |
| select placement | 2-D first/best-fit, partition-band reservations | **1-D bank-bitmap first-fit**; `address` = base bank `< NUM_PSUM_BANKS` |
| Spill target | DRAM spill space (`_Reload`/`_ReloadStore`) | **`TensorCopy` to SBUF/DRAM, reload BACK to PSUM** (`_Spill`/`_ReloadTensorCopy`) |
| Spill cost | bytes × loop weight | `liveN`(banks) × Latency |
| Demand metric | "% SB demand before allocation" | "% PSUM demand before spilling" |
| Order | colored **second** (consumes PSUM result) | colored **first**, frozen |

## Gaps and caveats

- **G1 — `NUM_PSUM_BANKS` value.** The colorer reads the bank count from `getArchModel(getArch) → Psumbuf+0` at runtime; it is not a literal in the colorer body. The per-arch integers — 4 banks on gen1, 8 on gen2/3/4 — are pinned independently in [1.05](../arch/sbuf-psum-geometry.md) from the `Psumbuf` ctors. The bank *size* `2048` is a hard literal, one of three returns.
- **G2 — Hex-Rays failures.** `allocate@0xad7140`, `correct_spill_reload@0xaed4b0`, and the family-a `select@0xa53270` failed to decompile; their phase order and strings were recovered from disassembly call targets and `.rodata`. The exact best-of-n perturbation source — what changes between `"main loop"` iterations — is **not pinned**; most likely a tie-break seed as in SBUF. [INFERRED]
- **G3 — Save spill space.** Whether the saved copy lands in SBUF vs DRAM is set by `copyInto`'s rehoming, downstream of the colorer (`coloring_allocator_sb` / `dma_optimization_psum`); the colorer asserts only the PSUM source/dest endpoints. Reload-dest-is-PSUM is asserted in the body; save-dest SBUF-preferred-else-DRAM is [INFERRED].
- **G4 — Accumulation machinery is upstream.** `is_zero_region_bank_overlap` / `set_psum_zero_region` / `get_psum_zero_region_bank_range` are present in the binary but are **not called** from any `PSUM_Allocator` colorer method (disasm of `build`/`find_costs`/`live_range` = zero xrefs) — they are `psum_legalization` / `build_flow_deps` territory ([Matmul Tile Ordering and Accumulation-Group Legalization](matmul-ordering-accgroups.md)). Accumulation residency reaches the colorer only via the shared-MLSet writer list.
- **G5 — Reporting semantics.** The `"%(psum first iter)"` / `"PSUM GCA interation nums = "` reporting strings sit in `allocate`'s best-of-n block but their exact numeric semantics (iteration count, first-iteration utilization snapshot) were **not transcribed** — named only.
