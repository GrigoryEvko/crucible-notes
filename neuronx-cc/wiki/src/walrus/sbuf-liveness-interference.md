# SBUF Allocator — Liveness, Interference & find-partners (Chaitin-Briggs, 2-D)

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). The code lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text`/`.rodata` the virtual address equals the file offset; `0x5e9020`–`0x62d650` is the `.plt` JMP-thunk band, so every symbol referenced there has a real high-VA body — the addresses below are the real bodies. The binary is stripped; symbol names come from the IDA sidecars and from embedded source-path / assert strings (`__PRETTY_FUNCTION__`). Other wheels differ — treat every address as version-pinned.*

## Abstract

The SBUF allocator (`ColoringAllocatorWithLoop::Rep::SB_Allocator`) is a Chaitin-Briggs graph colorer, but it colors a register file that no scalar allocator ever sees: SBUF is a 2-D memory — **128 partitions × per-partition bytes** ([1.05](../arch/sbuf-psum-geometry.md)) — and a "virtual register" is therefore a **partition×byte rectangle that persists over a time interval**, not a scalar that occupies one slot for one live range. This page documents the *front half* of that colorer: how a live range is built (`live_range`), how two ranges are tested for interference (`build` + `impact`), and how move-related ranges are coalesced before coloring (`find_partners`), plus the two setup phases that shape the node set — `renumber_locations` (dense node ids) and `memloc_split` (live-range splitting). The select/spill *back half* (geometric placement, the spill fixpoint) is documented separately in the planned SBUF spill/select page.

The bar is reimplementation: after this page a reader can build the liveness pass that emits the `firstDefs`/`lastUses` def-use maps and the per-tensor 2-D-plus-time interval list, the two-representation interference graph (triangular bit-matrix + adjacency lists) whose edges are time-overlaps and whose *weights* are geometric partition-band overlaps, and the conservative Briggs coalescer that merges accumulation groups only while the merged byte-pressure fits the SBUF band budget.

Four facts make this a *2-D-geometric* Chaitin-Briggs rather than the textbook scalar version, and they are the deliverable of this page:

1. A live range is a set of `eInterval{start,end,block}` records — one `[start,end]` time interval **per partition-block** the tensor occupies — keyed by the tensor node. The partition span is derived (a *shrink factor*), the byte span is `bytesPerBlock × liveN`, the time span is loop-extended.
2. An interference **edge** is a pure time-overlap (co-membership in the per-node co-live set). The partition×byte geometry is *not* folded into the boolean edge; it is carried separately as `impact()` — a 9×9 partition-band overlap factor times the combined byte extent.
3. Each node has its **own k** (`possible_placements`), because the number of available "colors" depends on the node's partition-band Height (4 bands, 2 half-bands, or 1 full band).
4. `find_partners` is conservative Briggs coalescing, but the "won't make the node uncolorable" test is expressed as a *byte-pressure* gate (`accumulation group is too large for SB`), the 2-D analogue of the classic significant-degree count.

| | |
|---|---|
| **Allocator class** | `neuronxcc::backend::ColoringAllocatorWithLoop::Rep::SB_Allocator` |
| **Source dir (embedded)** | `walrus_loop_flow/coloring_allocator_with_loop/src/sb_*_with_loop.cpp` |
| **Driver** | `SB_Allocator::allocate(LinearizedFunction*)` @ `0xa95310` ([8.16] allocator-drivers, planned) |
| **renumber_locations** | `0xaacc10` (`sb_renumber_with_loop.cpp`) — dense ids + Info[] alloc |
| **memloc_split** | `0xab9610` (`sb_memloc_split.cpp`) — live-range / web splitting |
| **live_range** | `0xa9dbb0` (`sb_live_range_with_loop.cpp`) — liveness → 2 DenseMaps |
| **create_eintervals** | `0xac14a0` — folds the maps into `DenseMap<MLSet*, vector<eInterval*>>` |
| **find_partners** | `0xaa9b60` (`sb_partners_with_loop.cpp`) — Briggs coalescing |
| **build** | `0xa99410` — interference graph (bit-matrix + adjacency) |
| **impact** | `0xab5000` — 2-D partition-band × byte overlap weight |
| **possible_placements** | `0xab5040` — per-node color count k by Height |
| **Info stride** | 128 bytes (`0x80`); array indexed `Info + (id << 7)` |
| **Colored node** | `bir::MemoryLocationSet*` (one per SBUF tensor) |

The select/spill half — `simplify` @ `0xab58c0`, `select`/`selectNode`, `insert_spill_code`, and the spill fixpoint that wraps this whole pipeline — is the planned SBUF spill/select page; this page references it but does not link it.

## Where the colorer sits — the driver order

The colorer runs once per `bir::Function`, inside `SB_Allocator::allocate` @ `0xa95310`. The phase order, recovered from the driver disassembly, is two setup phases followed by a spill fixpoint whose body re-runs the front-half passes on the (possibly spill-mutated) function:

```c
// SB_Allocator::allocate(LinearizedFunction* LF)            @0xa95310
renumber_locations(LF);          // dense node ids → MLSet+0x15c; alloc Info[128B]   (this page)
memloc_split(LF, info);          // live-range splitting: clone disjoint segments    (this page)
iter = 0;  heuristic = 0.0;
do {                                          // the spill FIXPOINT (not a randomized best-of-n)
    renumber_locations(LF);                   // re-number (lf may have grown via spill code)
    find_partners(LF, info);                  // Briggs COALESCE — pre-graph                 (this page)
    find_first_defs(LF, info);                // anchor: first writer → Info+32
    find_last_uses (LF, info);                // anchor: last reader  → Info+40
    find_loads(LF, info);                     // reload / remat seeding
    live_range(LF, info, firstDefs, lastUses);// LIVENESS → 2 DenseMaps                      (this page)
    build(LF, info, firstDefs, lastUses);     // INTERFERENCE GRAPH                           (this page)
    find_costs(LF, info, firstDefs, lastUses);// spill cost → Info+0
    simplify(LF, info, /*metric=*/0);         // Chaitin simplify → NodeStack          (spill/select page)
    Loc = select(LF, info, stack, &score);    // geometric placement                   (spill/select page)
    if (Loc->needSpill == 0) break;           // FIXPOINT reached — commit
    create_eintervals(LF, info, firstDefs, lastUses);  // rebuild interval list (spill path)
    insert_spill_code(LF, Loc->spillSet, info, homes); // MUTATES LF
    ++iter;
} while (1);
```

Two structural notes a reimplementer must not get wrong. First, the loop is a **spill fixpoint**, not a randomized restart — there is no RNG, no seed, no order shuffle, and no retained "best" coloring; `simplify`'s metric argument is hard-wired to 0 every pass, and the only cross-iteration change is the spill code `insert_spill_code` writes into the function — the driver contains no `rand`/`srand`/`mt19937`/`shuffle`/`seed` symbol at all, and the iteration counter is never compared to a bound. The string `"      best-of-n loop, heuristic = "` is a historical label for this fixpoint, and the value it prints is the integer iteration counter, not a score. Second, `find_partners` runs *before* `build`, so coalescing operates on the un-built graph and the merged super-nodes are what the interference graph then sees.

## Setup phase A — `renumber_locations`: dense ids and the Info array

`renumber_locations` @ `0xaacc10` (`sb_renumber_with_loop.cpp`) assigns each SBUF `MemoryLocationSet` a contiguous integer node-id and builds the parallel `Info[]` node array the rest of the pipeline indexes in O(1). The node count is the size of the function's `MemoryLocationSet*` vector, `n = (LF+0x18 − LF+0x10) >> 3`, stored at `LF+0x2f0` (the `numNodes` field). The `Info[]` array is `_Znam(n << 7)` — a 128-byte stride matching the Info layout below. Then a dense pass `id = 0 … n−1`:

```c
// per node id:
*(uint32*)(MLSet + 0x15c) = id;          // STAMP dense node-id into the tensor   (= MLSet+348)
Info[id].loc          = MLSet;           // Info+0x10
Info[id].coLive       = {};              // Info+0x30 — empty Rb-tree (co-live / partner set)
Info[id].alignedLen   = aligned_length(MLSet);   // Info+0x50  (call @0xa929c0)
Info[id].bandWidth    = (MLSet[0x100] <= 0x20) ? 32 : (… ? 64 : 128);  // Info+0x70
Info[id].height       = height_class;    // Info+0x74 — partition-band class 0..8
```

The stamped id is the same `MLSet+348` that `find_partners` reads as `getLocation()`'s node-id — the two sites anchor each other. `renumber_locations` re-runs at the top of every spill iteration because `insert_spill_code` adds and removes nodes; it is deterministic — the same walk order yields the same ids each pass, which is part of why the fixpoint has no perturbation source. A pre-placed or output `MemoryLocationSet` (`MLSet+0xa8` byte set) takes the precolored branch and is excluded from the colorable worklist.

The driver-visible metric `SbRenumberLocationsCount` (a `backend::BackendMetricType` enum member, published via `addMetric` @ `0x1742020`) tracks the renumbered node count; the PSUM and DRAM allocators carry the `Psum`/`Dram` twins.

## Setup phase B — `memloc_split`: live-range (web) splitting

`memloc_split` @ `0xab9610` (`sb_memloc_split.cpp`) is the SBUF analogue of SSA live-range splitting / web separation, run once in setup, before the coloring loop. A single named tensor whose writes and reads happen at disjoint program points has, as *one* `MemoryLocationSet`, a live range that conservatively spans the union of all segments — pinning the storage resident the whole time and manufacturing false interference. `memloc_split` clones such a tensor into one sub-location per disjoint live segment, so each clone is an independent, shorter-lived graph node, dropping false edges and letting the disjoint segments share physical SBUF.

For each Info node it gates the candidate:

```c
if (getPartitionDim(MLSet) == 0)        continue;   // skip scalar / 0-D locs
if (MLSet[0xf8] == 0)                   continue;   // no symbolic/tensor body
if (*(uint32*)(MLSet + 0x390) <= 1)     continue;   // writer/def-AP count must be > 1
if (*(uint32*)(MLSet + 0x368) <= 1)     continue;   // reader/use-AP count must be > 1
// candidate: multiple distinct defs AND multiple distinct uses
```

`MLSet+0x368` / `+0x390` are the count slots of the reader / writer access-pattern lists (the same lists at `MLSet+864`/`+904` whose extremes liveness uses; the `+N` here are the adjacent counts, an [INFERRED] field identity resting on a single anchor). For a candidate it walks the AP list, reads each instruction's `getLoopnest()`, computes interval relationships with `fmt::bigint` big-integer stride/period arithmetic, and **clones** the `MemoryLocationSet` per segment (`_Znwm` + `memmove` of the AP records; the clone is the `cl_mem_loc` of the assert `"cloned mem loc cannot be nullptr"`), splicing the clones back via `getMemlocByTensorId`. The clone+memmove+`getMemlocByTensorId` machinery is read directly; the bigint period predicate that decides *where* to cut is not — its mechanism is visible but the cut boundary was never byte-transcribed. The `TotalSplitSbNodesCount` metric counts the minted clones.

## The Info node and the colored tensor

The colorable node is `bir::MemoryLocationSet*` — the SBUF tensor. Each is materialized as one 128-byte `Info` record, indexed `Info + (id << 7)`; that stride reads byte-exact at every use site in `build`, `impact` and `simplify`. The fields this front-half touches:

| Field | Type | Meaning | Set by |
|---|---|---|---|
| `Info+0` | `double` | spill **cost** (`+INF` = uncolorable / precolored) | `find_costs` |
| `Info+16` | `MLSet*` | the tensor's `bir::MemoryLocationSet` (colored node) | `renumber` / `live_range` |
| `Info+32` | `Instruction*` | first writer (`def` anchor) | `find_first_defs` |
| `Info+40` | `Instruction*` | last reader (`use` anchor) | `find_last_uses` |
| `Info+48` | `set<uint>` | **co-live** node-id set (the edge source) | `live_range` / `create_eintervals` |
| `Info+56` / `+64` | `uint32*` / `uint32` | adjacency array / cursor (== degree) | `build` pass 2 |
| `Info+68` | `int32` | **degree** (interfering-neighbor count) | `build` pass 1 |
| `Info+72` | `{int hi, uint lo}` | packed pair: hi = significant-degree residual, lo = reverse impact sum | `simplify` |
| `Info+80` | `int32` | per-partition **bytesPerBlock** (one shrink-block's byte extent) | `live_range` |
| `Info+84` | `int32` | **liveBytes** = `bytesPerBlock × liveN` (the BYTE dimension) | `live_range` (L995) |
| `Info+88` | `uint32` | **liveN** (mirror of `MLSet::getLiveN()`) | `live_range` |
| `Info+96` / `+104` | `ptr` | per-partition-block live tracking arrays (`tc_new` × `liveN`) | `live_range` |
| `Info+116` | `int32` | **Height** class 0..8 (partition-band occupancy) | `mark_constraints` / `renumber` |
| `Info+122` | `bool` | precolored / pre-placed (skipped in simplify) | — |

The eligibility filter `candidate(MemoryLocationSet&)` @ `0xaacbe0` admits a set as a graph node iff it is SB-resident and not a function output: `firstMemLoc.type == 16 /*SB*/ && !isTensorKindOutput()`. Outputs are pre-pinned and never colored here.

## `live_range` — building the 2-D-plus-time interval

`live_range` @ `0xa9dbb0` is phase 1 of the colorer body. It does **not** re-scan the IR to find first/last; it trusts the anchors `find_first_defs`/`find_last_uses` parked at `Info+32`/`Info+40` and asserts they are non-null (`"no first def find"` @ `cpp:0x14`, `"no last use find"` @ `cpp:0x15` — both strings present verbatim in the binary). Its def-use *substrate* is the per-`MemoryLocationSet` writer/reader access-pattern lists the dependence builder populated:

- READERS at `MLSet+864/+888/+896` (begin / data / count)
- WRITERS at `MLSet+904/+928/+936`

Each accessor is classified by its access class at `AP+24`: `== 2` is a write (definition), `== 1` is a read (use). `live_range` walks both lists per tensor, collecting write-APs and read-APs, and sets a flag (`v74`) when a plain read is present that disables the partition-band shrink path.

### The time axis

The interval is `[def_idx, lastUse_idx]` in linear instruction-point index space. The flat program-order index is `bir::Instruction+0x4C` (the re-indexed program order; the same index `get_live_range_len` @ `0x994970` reads to compute the range length `max−min`). `create_eintervals` later translates `Instruction*` → integer time-coordinate through `LinearizedFunction+968`, a `DenseMap<Instruction*, uint>` of linear instruction-point indices built by walking the function's basic-block instruction list. This `LF+968` map is the bridge from `Instruction*` to the time coordinate. The point *sequence* is `linearize`'s product; that `LF+968` is itself built here is [INFERRED] from the structure rather than traced to a construction site.

### The partition footprint — the shrink factor

The partition span is not the tensor's full block count; it is the number of *distinct* partition-blocks the tensor is actually live in, derived by intersecting the partition-index sets across the access-pattern footprint:

```c
partDim      = loc->getPartitionDim();                       // MLSet+820
shrink_factor = Π over collapsible partition axes of TensorShape[axis];   // (or 1 if a plain read present)
shrinkDim     = collapsed_dim;                               // (or -1 if shrinking disabled)
assert(loc->getNBlocks() % shrink_factor == 0);   // "number of blocks is not a multiple of shrink factor" (cpp:0x88)
loc->setLiveN(loc->getNBlocks() / shrink_factor); // MLSet+? — LIVE partition-block count
loc->setShrinkDim(shrinkDim);                     // MLSet+828
```

`liveN ≤ NBlocks` is the partition-axis span of the rectangle. The shrink assert and `setShrinkDim` re-confirm what the ShrinkDN annotation pass pre-computed; `live_range` re-derives it for the colorer.

### The byte footprint and the SB capacity guard

```c
Info+84 = Info+80 /*bytesPerBlock*/ * liveN;          // total live byte footprint
if (Info+84 > LF+696 /*SBUF per-partition byte BUDGET*/)
    FATAL("<name> is too big for SB, requires <liveN> bytes with SB size <budget>");
```

The string `"is too big for SB"` appears verbatim in the binary. The 2-D footprint per tensor is therefore `(liveN partition-blocks) × (bytesPerBlock)`; the *base-partition offset* (`MLSet+560`) is fixed later by `select`, not here — liveness fixes the **span** (how many partitions, how many bytes, how long), placement fixes the **offset**.

### The loop-aware endpoint extension — the `_with_loop` delta

This is the whole reason the pass is named `*_with_loop`. A value written in iteration *i* and read in iteration *i+1* is loop-carried; if the colorer treated its interval as the inner def→use span it could alias the storage onto something reused within the loop. So the interval is **extended to the loop boundary**. `live_range` walks `def->getLoopnest()` and `use->getLoopnest()` in lockstep to find the common-nest prefix depth, then:

```c
commonDepth = longest common prefix of (def->getLoopnest(), use->getLoopnest());
// def-side: if def nests DEEPER than the shared prefix, record at the LOOP HEADER, not the inner def
defAnchor = (len(Ld) > commonDepth) ? loopHeader(Ld[commonDepth]) : def;
// use-side: if use nests deeper, push last-use to the loop TERMINATOR (the back-edge marker)
useAnchor = (len(Lu) > commonDepth) ? loopEnd(Lu[commonDepth]) : use;   // loop end = InstLoop opcode 105
firstDefs[defAnchor].push_back(defOrdinal);   // a4
lastUses [useAnchor].push_back(useOrdinal);   // a5
```

The loop terminator is found by scanning the loop body for opcode **105** — the `Loop` op, which the don't-touch pass hard-keeps for exactly this reason. The pre-pass `label_possible_loop_carried_dependency_node` @ `0x9945d0` (a TBB `parallel_for` over Info nodes) pre-marks which nodes are loop-carried candidates; `live_range` performs the actual extension. Net effect: a loop-carried tensor's interval is stretched `[loopHeader_idx … loopBackEdge_idx]`, so it is treated as live across the entire loop body.

### The outputs

`live_range` emits two `DenseMap<bir::Instruction*, std::vector<unsigned>>` — `firstDefs` (key = def-or-loop-header instruction, value = tensor ordinals first-defined there) and `lastUses` (key = use-or-loop-end instruction, value = tensor ordinals last-used there). `build` and `find_costs` consume these directly. `create_eintervals` @ `0xac14a0` folds them into the per-tensor 2-D-plus-time interval list:

```c
struct eInterval {        // tc_new(12)
    uint32 start;   // +0  linear-index of the DEF anchor (via LF+968)
    uint32 end;     // +4  linear-index of the USE anchor
    uint32 block;   // +8  the partition-BLOCK ordinal this interval belongs to
};
llvm::DenseMap<bir::MemoryLocationSet*, std::vector<eInterval*>>   // keyed by the tensor node
//   one vector of eIntervals per tensor; one eInterval per live partition-block.
```

This is the complete 2-D-plus-time live model: a tensor's liveness = a set of `[start,end]` time intervals (one per partition-block — the partition axis), each carrying the tensor's `bytesPerBlock` (the byte axis).

## `build` — the interference graph: edges are time-overlaps

`build` @ `0xa99410` ("build interference graph") is a classic Chaitin two-representation graph: a triangular **bit-matrix** for O(1) membership plus per-node **adjacency lists** for iteration. The bit-matrix is lower-triangular, sized `(n² + 15) >> 4` bytes (one bit per unordered pair), and an edge `(i,j)` with `lo=min`, `hi=max` maps to bit index `lo + ((hi*hi) >> 1)` — the standard half-matrix packing, which appears twice in the body.

The **edge source** is the per-node co-live set `Info+48` — an `std::set<uint>` of node-ids simultaneously live with this node, filled from the def/use maps. The decisive design choice: **two nodes interfere iff their live ranges overlap in TIME** — co-membership in `Info+48`. The 2-D partition×byte geometry is *not* folded into the boolean edge; it is carried separately as a weight (`impact`) and applied in `simplify`. So the edge is "ranges overlap in time"; the spatial dimension sharpens the *degree* into a byte-pressure, not the adjacency bit.

```c
// build PASS 1 — count degrees:
for each node v:  for each distinct co-live id w in Info[v].coLive:
    lo = min(v,w); hi = max(v,w); idx = lo + (hi*hi >> 1);
    if (!bittest(s, idx)) { setbit(s, idx); ++Info[lo].degree; ++Info[hi].degree; }   // undirected
// build PASS 2 — fill adjacency:
for each node: Info.adj = tc_new(degree); Info.cnt = 0;   // then re-walk, append each fresh edge to BOTH endpoints
```

Interleaved with edge-building, `build` also accumulates a per-instruction **SB-pressure** profile: for each instruction's live-set it sums `Info+84` (bytes) bucketed by `Info+116` Height class, yielding the per-partition-band high-water demand (logged `"SB pressure N bytes"`, `"SB high-water mark = N bytes"`, `"N bytes in partitions [0,31]"`, …). A band that overflows the arch SBUF capacity raises `"bad height"`. This is the 2-D demand the colorer must satisfy.

## The 2-D overlap weight — `impact`, Height, `possible_placements`

The geometric interference is quantified by `impact` @ `0xab5000` — "how much of i's space does j deny":

```c
// impact(Info*, uint i, uint j)   @0xab5000
return vertical_impact_with_loop[ 9*Height(j) + Height(i) ]   // partition-band overlap factor
       * ( bytes(i) + bytes(j) - 1 );                          // combined byte pressure
// Height(x) = Info[x]+116;  bytes(x) = Info[x]+84
```

`vertical_impact_with_loop` (symbol confirmed in the binary) is a 9×9 `int` table indexed by both nodes' Height class: 0 when their partition bands are disjoint, `>0` scaled by how the bands overlap. The `9*Height(j)+Height(i)` index arithmetic fixes the 9×9 shape; the individual cell values do not appear, because the table lives in `.data` behind a relocated pointer. Shape and role are read, the integers are [INFERRED]. The second factor `bytes(i)+bytes(j)−1` is the byte extent the two tensors jointly consume in a shared band. The product is the SBUF space `j` removes from `i`'s options — the 2-D interference made numeric.

The **Height** enum (`Info+116`, 0..8) is decoded from two `.rodata` tables. Both were read byte-exact from the binary on this pass:

| Height | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|---|
| band-base (`int32` @ `0x1DDEDA0`) | 3 | 3 | 3 | 3 | 3 | 2 | 2 | 2 | 1 |
| divisor (`int16` @ `0x1DDEDD0`) | 32 | 32 | 32 | 32 | 32 | 64 | 64 | 64 | 128 |

The band-base table is 32-bit (`{3,3,3,3,3,2,2,2,1}`, dword elements). The divisor table is **16-bit** (`{32,32,32,32,32,64,64,64,128}`, `int16` elements). The semantics, on SBUF = 128 partitions:

- Height 0..4 → divisor 32 → occupies one of **four** 32-partition bands
- Height 5..7 → divisor 64 → occupies one of **two** 64-partition half-bands
- Height 8 → divisor 128 → spans **all 128** partitions (a tall tensor)

> **GOTCHA — the divisor table is `int16`, not `int32`.** Reading it a dword at a time silently interleaves adjacent pairs, so the first element decodes as `0x200020` — a single large value where the truth is the pair `(32, 32)`. The band-base table immediately above it *is* dword-wide, which makes the mixed widths easy to miss.

`possible_placements` @ `0xab5040` turns this into the per-node color count k:

```c
// possible_placements(Info*, uint)   @0xab5040
result = (SBUF_rows(this+696) + 1) - rows(Info+80);   // free rows the node fits in
switch (Height) {
    case 0:        return 4 * result;   // 4 bands available
    case 5:        return 2 * result;   // 2 half-bands
    case 6,7,8:    return     result;   // 1 band (tall / fixed-band)
    default:       FATAL("Unexpected value for info[xIndex].height");
}
```

So the Chaitin "k available colors" is **not a single scalar** — each node carries its own k determined by its partition-band Height. The colorer colors a non-uniform interference graph. This per-node k is what `simplify`'s significant-degree test (the spill/select page) compares against `Σ impact(node, neighbor)`.

## `find_partners` — conservative Briggs coalescing (2-D)

`find_partners` @ `0xaa9b60` (`sb_partners_with_loop.cpp`) runs *before* `build` and discovers groups of locations that should share storage, collapsing them to one graph node so a copy is elided or an accumulation chain is honored. It is gated by a "partners enabled" bool (`this+1040`), cleared on failure. Its logging trace (transcribed): `"find partners"` → `"found N accumulation groups"` → per group `"largest = <name>"` / `"tensors = N"` / `"requires N bytes/partition"` → on overflow `"accumulation group is too large for SB"` → `"expanding partners"`.

### What it coalesces

1. **Accumulation groups** — matmul/PSUM accumulation chains and `TensorCopy` src/dst whose live ranges are move-related. It walks each basic block, and for opcode-8 (`Matmult`) arguments collects the `AccessPattern → MemoryLocation` (node-id at `getLocation()+348`) into a hashtable `MemoryLocation* → std::set<uint>` — each entry is one co-allocation group.
2. **Base-partition alignment** — `isBasePartitionConstrained` marks members whose base partition is fixed, then stamps each member's Height (`Info+116`) from its base partition (`ml_base 0 → Height 6`; `32 → 2`; `64 → 7`; `96 → 4`; with the assert `"tall tensors must be accessed starting at partition 0"`), forcing coalesced members onto compatible partition bands so they *can* share one physical region.

### The (int, unsigned) comparator

The exact lambda demangle is in the binary: `find_partners(LinearizedFunction*, Info*)::<lambda(const auto:40&, const auto:41&)> [with auto:40 = int; auto:41 = unsigned int]`. Given a group base partition `base` and a member tensor-partition `tp`, it normalizes the member's partition index to the group base:

```c
assert(tp >= base);        // "tp >= base"  (physical AP) / "tps.first >= base"  (symbolic AP)
offset = tp - base;        // partition OFFSET of each member relative to the group base
```

The pair `(int, unsigned)` is `(signed tp, unsigned base)` and the comparator computes each member's partition offset, then memcmp's the offset-vectors to test whether two members occupy the *same relative partition layout* — i.e. whether they are coalescable. The physical-AP path (`AP+24 == 1`) uses the concrete base; the symbolic-AP path (`AP+24 == 2`) calls `SymbolicAccessPattern::getBasePartitionsInMemoryLocation`.

### Partner expansion (the transitive closure)

`"expanding partners"` is the final transitive merge: for every group it iterates members sorted by the base-offset comparator and, for each ordered pair of distinct members `(u, v)`, inserts `v` into `u`'s partner set (the per-node `std::set<uint>`). This forms the closure — every member of an accumulation group becomes a mutual partner of every other — so the downstream graph treats the group as one coalesced super-node sharing storage.

### The Briggs conservative gate

Classic Briggs coalesces a move only if the merged super-node remains colorable (`< k significant-degree neighbors`). Here the test is the **2-D analogue**: a group is coalesced only while its summed `"requires N bytes/partition"` stays under the per-band SB cap (`this+968`). The moment a group's combined byte demand exceeds the band capacity, the pass logs `"accumulation group is too large for SB"` and **disables partners** (`this+1040 = 0`), falling back to the un-coalesced graph. This is the Briggs guarantee — "don't coalesce if it would make the node uncolorable" — expressed in the SBUF byte-pressure metric that `impact`/`possible_placements` use. The mechanism is read directly from the pass; the mapping of the classic "< k significant-degree neighbors" test onto this per-band byte cap is [INFERRED], since no separate scalar degree count exists in this allocator.

The retry path `selectNodeWithPartnerRetry` (spill/select page) re-attempts a node honoring its partner set before declaring a spill, closing the coalesce↔color loop.

## End-to-end (front half)

```
renumber_locations          dense ids → MLSet+0x15c; alloc Info[128B]; cache band-width/Height
memloc_split                clone disjoint-segment memlocs → shorter live ranges, fewer false edges
   │
find_partners               Briggs COALESCE (pre-graph): acc-groups + base-partition align,
   │                        (tp:int, base:uint) offset comparator, transitive partner expansion,
   │                        conservative gate = group byte-demand ≤ SB band cap
live_range                  per tensor: def/use anchors → loop-nest common prefix → EXTEND across
   │                        back-edge (header / opcode-105 terminator); 2-D footprint
   │                        (liveN partition-blocks × bytesPerBlock), SB-budget guard;
   │                        OUTPUT: firstDefs / lastUses DenseMaps
create_eintervals           fold maps → DenseMap<MLSet*, vector<eInterval{start,end,block}>>
   │
build                       interference graph: triangular bit-matrix (idx = min + max²/2) +
   │                        symmetric adjacency lists; EDGE = time-overlap (co-live set);
   │                        per-band SB-pressure high-water
impact / possible_placements  2-D weight = vertical_impact[9*Hj+Hi]×(bytes_i+bytes_j−1); per-node k by Height
   │
   └─→  find_costs → simplify → select   (spill/select half — planned page)
```

## Diagnostic strings (all present verbatim in `libwalrus.so`)

| String | Phase | Meaning |
|---|---|---|
| `coloring_allocator_with_loop/src/sb_live_range_with_loop.cpp` | `live_range` | source path (and `sb_partners_/sb_simplify_/sb_renumber_/sb_memloc_split.cpp`) |
| `no first def find` / `no last use find` | `live_range` | anchor assert (`cpp:0x14`/`0x15`) |
| `number of blocks is not a multiple of shrink factor` | `live_range` | shrink-factor divisibility assert (`cpp:0x88`) |
| `shrink factor` / `tenosrizer` (sic) | `live_range` | per-tensor shrink trace |
| `is too big for SB` | `live_range` | byte-budget fatal |
| `build interference graph` / `SB pressure` / `SB high-water` / `adjacency vectors require` | `build` | graph build + 2-D pressure |
| `Bad Height` / `Unexpected value for info[xIndex].height` | Height / `possible_placements` | enum guard |
| `tall tensors must be accessed starting at partition 0` | `find_partners` | base-partition align assert |
| `accumulation group is too large for SB` / `expanding partners` | `find_partners` | Briggs gate + transitive merge |
| `tp >= base` / `tps.first >= base` | `find_partners` | comparator normalization assert |
| `def cannot be nullptr` / `cloned mem loc cannot be nullptr` | `memloc_split` | split clone asserts |
| `best-of-n loop, heuristic = ` / `SB GCA interation` / `spilling from SB cost` | driver | spill-fixpoint labels (not a randomized restart) |

## Function map

| Symbol (real body) | Addr | Role |
|---|---|---|
| `SB_Allocator::allocate(LinearizedFunction*)` | `0xa95310` | per-Function driver / spill fixpoint ([8.16], planned) |
| `SB_Allocator::renumber_locations` | `0xaacc10` | dense node ids + `Info[]` alloc |
| `SB_Allocator::memloc_split` | `0xab9610` | live-range / web splitting |
| `SB_Allocator::find_partners` | `0xaa9b60` | Briggs coalescing |
| `SB_Allocator::find_first_defs` | `0xaa6fc0` | first-def anchor → `Info+32` |
| `SB_Allocator::find_last_uses` | `0xaa8e00` | last-use anchor → `Info+40` |
| `SB_Allocator::live_range` | `0xa9dbb0` | liveness → `firstDefs`/`lastUses` |
| `SB_Allocator::create_eintervals` | `0xac14a0` | fold maps → `eInterval` list |
| `SB_Allocator::build` | `0xa99410` | interference graph |
| `SB_Allocator::impact` | `0xab5000` | 2-D partition-band × byte weight |
| `SB_Allocator::possible_placements` | `0xab5040` | per-node color count k by Height |
| `SB_Allocator::candidate(MemoryLocationSet&)` | `0xaacbe0` | node eligibility (SB && !output) |
| `get_live_range_len(Info&)` | `0x994970` | interval length = `max−min` of `+0x4C` |
| `label_possible_loop_carried_dependency_node` | `0x9945d0` | TBB pre-mark loop-carried nodes |
| `vertical_impact_with_loop` | `0x3ded040` | 9×9 partition-band overlap table (`.data`) |

## Evidence summary

Read directly from `libwalrus.so`: the source paths (`sb_*_with_loop.cpp`); every assert and diagnostic string tabulated above; the Info stride of 128 and the field offsets; the triangular bit-matrix index `min + (max²>>1)` and its size `(n²+15)>>4`; the two-pass build; the `impact` formula and its 9×9 index arithmetic; the Height band-base table `{3,3,3,3,3,2,2,2,1}` (int32 @ `0x1DDEDA0`) and divisor table `{32,32,32,32,32,64,64,64,128}` (int16 @ `0x1DDEDD0`); `possible_placements`' k-by-Height switch; the `(int,unsigned)` lambda demangle and its `tp>=base` asserts; the `ml_base→Height` stamp; the opcode-105 loop terminator; the dense id at `MLSet+348`; and the spill-fixpoint structure, with no RNG, no fixed iteration count and no retained best coloring.

Well-supported but a step short of that: the interference edge as co-membership in `Info+48`; the band byte-cap serving as the Briggs-conservative coalesce gate; `memloc_split` as web-splitting, where the clone/memmove/`getMemlocByTensorId` machinery is proven but the segmentation predicate is not byte-transcribed; and `SbRenumberLocationsCount` / `TotalSplitSbNodesCount` as the driver-visible node and clone counters.

## Limits of this reading

Four things are reconstructed rather than read, each flagged where it appears above:

- the numeric cells of `vertical_impact_with_loop` — the table is `.data`-relocated, so only its 9×9 shape and its role are proven;
- the mapping of the classic "< k significant-degree neighbors" test onto the per-band byte-pressure residual;
- the identity of `MLSet+0x368` / `+0x390` as exactly the reader and writer AP-count slots (single-anchored, though consistent with the `+864` / `+904` lists);
- ownership of `LF+968`, i.e. that it is built here rather than upstream.

Three gaps remain outright. The numeric SBUF byte budget (`LF+696`) is not here at all — it lives in the per-arch Target/EngineInfo constructor rather than in JSON, and is tabulated in [1.05](../arch/sbuf-psum-geometry.md). The `memloc_split` bigint period cut-boundary is untraced. So is the `addMetric` increment site for the renumber and split counters.

## Cross-references

- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the 128-partition × per-partition-byte space these live-ranges occupy, and the per-arch SBUF byte budget (`LF+696`).
- [8.16] SBUF allocator drivers (planned, `walrus/allocator-drivers.md`) — the `allocate` driver and spill fixpoint that runs this colorer.
- The SBUF spill/select half (planned, `walrus/`) — `simplify` (significant-degree worklists, the `cost/degree²` spill metric), `select`/`selectNode` (geometric 2-D placement), and `insert_spill_code` — the *back half* of this colorer.
- [The Dependence Graph — build_fdeps + anti-dependency-analyzer](dependence-graph.md) — the writer/reader access-pattern lists (`MLSet+864`/`+904`) that supply liveness its def-use substrate.
- [matmul-ordering & accumulation groups](matmul-ordering-accgroups.md) — the accumulation chains `find_partners` coalesces.
