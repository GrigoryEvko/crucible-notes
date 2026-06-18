# The DRAM Allocator — Interval Tree and Shared-DRAM Partitioning

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds). Everything here lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; the `0x5e9020`–`0x62d650` range is `.plt` thunks. Each `DRAM_Allocator` body also appears at a second, lower address in the IDA snapshot frame (e.g. `build_interval_tree` at `0x5fe150`); the real `.text` body is the high `0x9x…`/`0xax…` frame cited throughout. The four `bir::MemoryLocation::*SharedPostDRAMAlloc*` and `birsim::NeuronCoresManager::getSharedDRAMforRank` symbols are imported (`U`) from `libbir`/`birsim`. Other wheels differ — treat every address as version-pinned.*

## Abstract

DRAM is the only memory space the walrus backend places with an **interval tree**, not graph coloring. Where the SBUF and PSUM colorers ([`allocator-drivers`](allocator-drivers.md)) build an explicit Chaitin–Briggs interference graph — adjacency bitsets, simplify, select, spill — `neuronxcc::backend::DRAM_Allocator` (`allocate` `@0xa6cb20`) encodes liveness as `[def, use]` intervals over a flat instruction index and answers "do these two tensors interfere?" with an `O(log n + k)` interval-tree overlap query instead of an `O(V²)` adjacency lookup. That is the right shape for DRAM: it is large and linear, so a first-fit placement into the lowest free gap is correct and cheap, and there is no spill — DRAM is effectively the bottom of the hierarchy. The page documents that mechanism end to end: `build_interval_tree` (`@0xa71a80`), the flat `IntervalTree` (`makeTree`/`makeMax`/`getAllOverlaps`), the `search` first-fit selector with 4 KiB page-straddle protection, the `simplify` elimination order, and `select_impl`'s assignment loop.

The second half is the multi-core headline. A real model is split across `N` LNC cores by `lnc_splitter`, and a subset of DRAM tensors must be **one physical buffer shared by all N cores** (collective-communication staging, cross-core handoff). The allocator marks those locations with `bir::MemoryLocation::setIsSharedPostDRAMAlloc(true)` during `allocate`; the flag is then the single source of truth read by eight downstream stages. `coloring_allocator_dram_shared` is the same `DRAM_Allocator` class run in shared mode (ctor `from_space = Shared`); `sync_shared_allocations` (`run(vector<Module>&)` `@0x16b2da0`) walks every shared location's `getRemoteLocalTarget()` link and writes the **same** address/partition/bank into the matching buffer in each sibling core's module, so all N cores resolve a shared tensor to one identical physical address. This is compile-time address agreement, not a runtime barrier; the ordering instructions are emitted separately.

The third strand is auto-sizing and gating. Collective-communication buffers are auto-sized by `cc_buffer_size` (`@0xa796c0`) when `--allreduce-buffer-size = -1`, page-aligned, and reserved as a region ceiling. The whole pass is gated by `--disable-dram-allocation`, checked in the **pipeline builder** (the `coloring_allocator_dram` step is conditionally skipped), not inside `allocate`.

For reimplementation, the contract is:

- The interval-tree liveness model — flat instruction numbering, `[def,use]` intervals, the augmented `IntervalTree`, and the overlap-query interference oracle that replaces the SBUF interference graph.
- The `search` first-fit placement: free-list construction from sorted occupied intervals, the gap-fit test, and the 4 KiB page-straddle-avoidance branch.
- Page / GCA alignment — `getAlignmentSize` returns a hard `0x1000` for `MemoryType == DRAM`, bypassing the access-pattern GCA computation used for SBUF.
- CC-buffer auto-sizing and the `--allreduce-buffer-size` knob; the `--disable-dram-allocation` gate and where it is checked.
- Cross-LNC shared-DRAM partitioning: the `setIsSharedPostDRAMAlloc` mark, the eight `isSharedPostDRAMAlloc` readers, and `sync_shared_allocations`' address-equalization algorithm.

| | |
|---|---|
| **Allocator class** | `neuronxcc::backend::DRAM_Allocator` (`Type=2` registration of `ColoringAllocator`) |
| **Driver** | `DRAM_Allocator::allocate(bir::Function*)` `@0xa6cb20` |
| **Interference model** | `build_interval_tree` `@0xa71a80` → `IntervalTree` (24-byte nodes), not an adjacency graph |
| **Tree ops** | `makeTree` `@0x9a7a00`, `makeMax` `@0x9a8000`, `getAllOverlaps` `@0x9a8a10`, `getAllOverlapsWithInterval` `@0x9a8740` |
| **Placement** | `search` `@0xa78740` — first-fit, 4 KiB page-straddle protection |
| **Order / assign** | `simplify` `@0xa814a0` (elim order) → `select` `@0xa7ef30` / `select_impl` `@0xa7a730` |
| **Alignment** | `getAlignmentSize` `@0x1091890` — DRAM (`memType==8`) → `0x1000` (4 KiB), fixed |
| **HWM floor** | `find_start_addr` `@0xa77710` — returns `this+123` (HWM, byte `0x3d8`), or `0` |
| **CC auto-size** | `cc_buffer_size` `@0xa796c0`; knob `--allreduce-buffer-size` (MB, **500**; `-1` = auto) |
| **Gate** | `--disable-dram-allocation` — checked in the pipeline builder, skips `coloring_allocator_dram` |
| **Shared mark** | `bir::MemoryLocation::setIsSharedPostDRAMAlloc(bool)` (imported), sole caller `allocate` `@0xa6e7b1` |
| **Cross-core sync** | `SyncSharedAllocations::run(vector<Module>&)` `@0x16b2da0` — address equalization |
| **Source paths** | `coloring_allocator/src/dram_allocator/{dram_build,dram_select,dram_simplify}.cpp`, `interval_tree.cpp`, `sync_shared_allocations/src/sync_shared_allocations.cpp` |

---

## The allocate Driver

### Purpose

`DRAM_Allocator::allocate(bir::Function*)` is the single entry point. It scans the function's allocations, accounts the pre-placed DRAM footprint into a high-water mark, marks cross-core-shared locations, and — for the single-module core path — builds the interval tree and assigns addresses. It is reached only when DRAM allocation is enabled (the gate is upstream, §Gate) and is the **sole** caller of `setIsSharedPostDRAMAlloc`.

### Entry Point

```text
ColoringAllocator::run(bir::Module&)  0x985100      (Type=2 → DRAM_Allocator rep)
  └─ DRAM_Allocator::allocate(bir::Function*)        0xa6cb20   *** THE DRIVER ***
       ├─ collectCallReferencedSets(func)            // pin call-referenced allocs
       ├─ candidate(loc)  (vfn *(*this+32))          0xa6f0c0   // DRAM eligibility predicate
       ├─ setIsSharedPostDRAMAlloc(true)             0xa6e7b1   // mark cross-core locs (imported)
       ├─ renumber_locations(candidates)             0xa74360   // dense Info[] ids
       ├─ build_interval_tree(func, Info&)           0xa71a80   *** liveness intervals ***
       ├─ simplify(func, Info&)                      0xa814a0   // elimination order (stack)
       └─ select(func, Info&, order, inPlace)        0xa7ef30   // assign addresses
```

### Algorithm

```c
function DRAM_Allocator_allocate(func):                  // sub_0xa6cb20
    set_module_name_log_attr(func); this[152] = 1         // this+19: collectCallReferencedSets done
    collectCallReferencedSets(func)                       // call-referenced allocs not freed across calls

    // --- FIRST SCAN: is there anything to place? ---
    any = false
    for loc in func.allocs():                             // filter Storage→MemoryLocationSet→MemoryLocation
        if candidate(loc) && loc[168] == 0:               // candidate & not already addressed
            any = true
    if !any:
        log "No candidate, DRAM allocation successful"     // .rodata 0x1ccd328
        FunctionAttribute[key=11] = false                  // alloc result; benign no-op
        return                                             // *** EARLY EXIT ***

    linearized = linearize(func)                          // vfn *(*this+16)
    log "  allocating spills in DRAM <pre|post>_link mode for address space <ASname>"

    // --- SECOND SCAN: collect pre-placed shared locations ---
    for loc in allocs:
        if candidate(loc) && loc[168] != 0:               // already addressed (input/output/weight)
            push loc → this[126]   (post-DRAM-shared vector)

    // --- SHARED MARKING (multi-core): the sole setIsSharedPostDRAMAlloc site ---
    if module_name_compare(...):                          // gated; runs in dram_shared mode (from_space=1)
        for loc in func.allocs():
            if shared_predicate(loc):                     // vfn *(*r14+0x20): belongs to cross-core set
                loc.setIsSharedPostDRAMAlloc(true)         // 0xa6e7b1  (imported from libbir)

    // --- MULTI-CORE PRE-PASS: account pre-placed DRAM footprint into HWM ---
    if this[992]==0 || this[242]!=0:                      // pre-link, or dram_shared mode
        for sib_func in module.functions_named_like(func):
            for loc in candidate DRAM locs (memType==8, addrspace==this[243], usage in {7,9}):
                addr = MemoryLocation::getAddress(loc)
                size = elemCount(loc+264) * elemSize(loc+256)
                A    = getAlignmentSize(loc, this+360)     // = 4096 for DRAM
                size = roundUp(size, A)                     // size + A - size%A  if size%A != 0
                this[123] = max(this[123], addr+size-1)     // HWM update (byte 0x3d8)

    // --- COST ACCOUNTING + metrics ---
    for loc in allocs:                                     // spill / aligned-spill / dram totals
        if candidate(loc):
            push loc → this[172]  (candidate vector)
            if loc[168]==0: spill += sz; aligned_spill += roundUp(sz, A)
        elif memType(loc+216)==8 && this[243]==addrspace(loc+240): dram += sz
    addMetric(31, this[123]); addMetric(32, ...)           // "reserved space = N bytes", etc.

    // --- MULTI-CORE remote-marking OR single-module core ---
    if cores>1 && this[242]==1 && sharing-on-core-1:
        log "Skipping shared tensor allocations on core 1, marking as remoteLocalTarget instead"
        mark_remote(candidateVector)                       // 0xa76080
    else:                                                  // LABEL_208 — single-module allocation core
        CFG::build(func)
        renumber_locations(candidateVector)                // dense Info ids
        assign_extended_defuse(this, func, this+264)       // extend liveness across def/use
        build_interval_tree(this, func)                    // *** interval-tree liveness ***
        simplify(result, this, func, this+264)             // elimination order
        select(this)                                       // first-fit address assignment
        log "DRAM allocation successful"
        FunctionAttribute[key=11] = alloc-result
    return linearized
```

The three size primitives recur in every phase and are CONFIRMED by repeated offset use:

```c
elemCount = *(uint64*)(*(loc + 264))      // deref ptr-to-field
elemSize  = *(uint32*)(*(loc + 256))
A         = getAlignmentSize(loc, this+360)   // = 4096 for DRAM (memType==8)
byteSize  = elemCount * elemSize
alignedSize = roundUp(byteSize, A) = (byteSize + 4095) & ~4095
```

> **NOTE —** the early-exit "No candidate, DRAM allocation successful" path is not failure: it covers the degenerate case where nothing needs DRAM placement (all spills already addressed). It writes the same success `FunctionAttribute` (key 11) the full path does, so the pass reports OK with zero work. **[CONFIRMED — string + `FunctionAttribute` write in `allocate` decompile.]**

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `DRAM_Allocator::allocate(Function*)` | `0xa6cb20` | driver; scans, marks shared, builds tree, assigns | CONFIRMED |
| `DRAM_Allocator::candidate(MemoryLocation&)` | `0xa6f0c0` | DRAM eligibility predicate (vfn slot `*this+32`) | CONFIRMED |
| `DRAM_Allocator::renumber_locations(ArrayRef<MemoryLocation*>)` | `0xa74360` | allocate `Info[]`, dense ids, `alignedSize` | CONFIRMED |
| `DRAM_Allocator::mark_remote(...)` | `0xa76080` | mark core-1 shared as `remoteLocalTarget` | CONFIRMED |
| `DRAM_Allocator::find_start_addr()` | `0xa77710` | HWM floor (`this+123`, byte `0x3d8`) | CONFIRMED |

---

## The Interval-Tree Liveness Model

### Purpose

`build_interval_tree` is the DRAM analogue of the SBUF interference-graph build. It does **not** materialize adjacency. It assigns each instruction a flat integer "time", emits one `[def_flat, use_flat]` interval per def→use edge of each candidate location, and packs all intervals into a flat array that becomes a statically balanced, subtree-max-augmented interval tree. Two locations interfere iff any of their intervals overlap — answered by an interval-tree query, not a bitset lookup.

### Entry Point

```text
DRAM_Allocator::build_interval_tree(Function*, vector<Info>&)   0xa71a80
  ├─ flat instruction numbering  (inst[+76] = monotone index)
  ├─ per-location interval lists (vector<u32> of node indices, one per Info)
  ├─ def→use interval emission   (get_defs / get_uses, DenseMap-cached)
  ├─ IntervalTree::makeTree(it, 0, N)        0x9a7a00   // balanced BST by array index
  └─ IntervalTree::makeMax(it, root)         0x9a8000   // subtree-max augmentation
```

Source: `coloring_allocator/src/dram_allocator/dram_build.cpp` (asserts cite `:61`, `:81`, `:93`).

### Algorithm

```c
function build_interval_tree(func, info):                // sub_0xa71a80
    // STEP A — flat instruction numbering (dram_build.cpp ~293-315)
    idx = 0
    for inst in walk_flat_backward(func):                // getFirstWithin / getPrevInstFlat / getLastWithin
        inst[+76] = idx++                                // unique flat "time" coordinate

    // STEP B — per-location interval lists
    n = this[122]                                        // #Info records
    alloc n × vector<u32>                                // each Info's IntervalTreeNode indices
    assert all info[i].def_use_vec.size() > 0 || info[i].pinned   // dram_build.cpp:61

    // STEP C — single-BB bitset
    for i in 0..n: bit[i] = !isSingleBBAccessed(info[i].memloc)   // cross-BB → multi-interval

    // STEP D — def→use interval emission (~565-1346)
    for inst in walk_flat_backward(func):
        defs = ColoringAllocator::get_defs(inst)         // DenseMap<Inst*, vector<MemoryLocation*>>
        uses = ColoringAllocator::get_uses(inst)
        for m in defs ∪ uses:
            if !candidate_inline(m): continue            // memType==8, addrspace==this[242], usage∈{7,9}
            if shouldSkipNKITensorWithNoDefUse(m): continue
            useIdx  = m[+348]                            // m.firstDefInfoIndex (Info index)
            defInst = info[useIdx].def_use_vec.get_def_from_use(&inst)
            assert defInst != null                       // dram_build.cpp:81
            lo = defInst[+76];  hi = inst[+76]           // def flat#, use flat#
            node = IntervalTreeNode{ hi, lo, lo, useIdx, -1, -1 }   // see layout
            info[useIdx].intervals.push_back(nodeIndex)

    // STEP E — build the tree (~1392-1402)
    assert intervals.size() >= locations                 // dram_build.cpp:93
    root = IntervalTree::makeTree(it, 0, nodes.size())   // 0x9a7a00
    IntervalTree::makeMax(it, root)                      // 0x9a8000
    log "Num intervals N  Num locations M";  log "IntervalTree Build Done"
```

### The IntervalTree

`IntervalTree` is a `llvm::MutableArrayRef<IntervalTreeNode>` — `{ IntervalTreeNode* base; size_t count }`. Each node is 24 bytes:

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `lo` | `+0` | u32 | interval start = def's flat instruction index |
| `hi` | `+4` | u32 | interval end = use's flat instruction index |
| `max` | `+8` | u32 | augmentation: max `hi` in this node's subtree |
| `payload` | `+12` | u32 | the `Info`/location index this interval belongs to |
| `left` | `+16` | i32 | left child array index (`-1` = none) |
| `right` | `+20` | i32 | right child array index (`-1` = none) |

`makeTree(lo, hi)` `@0x9a7a00` is a classic recursive perfect-balance builder: `mid = (lo+hi)/2`, `node[mid].left = makeTree(lo, mid)`, `node[mid].right = makeTree(mid+1, hi)`, returns `mid`; asserts `lo <= hi` (`interval_tree.cpp:5`). The tree is keyed by **array index**, not by `lo` — the in-order index sequence *is* the tree, so rebalancing is free. `makeMax(idx)` `@0x9a8000` is the CLRS post-order subtree-max augmentation: `node.max = max(node.hi, makeMax(left), makeMax(right))`.

The interference oracle is `getAllOverlaps(qIdx)` `@0x9a8a10` (and `getAllOverlapsWithInterval(lo, hi)` `@0x9a8740`): an iterative DFS over an explicit `std::deque<int>` stack that prunes a subtree when `q.lo > node.max` (the augmentation's whole purpose), collects `node.payload` whenever `node.lo <= q.hi && q.lo <= node.hi`, and asserts `0 <= qIdx < intervals.size()` (`interval_tree.cpp:63`).

> **QUIRK —** the tree is **static and perfectly balanced by construction**, never rebalanced. `makeTree` partitions the node array by midpoint, so the BST shape is fixed the instant the array is laid out. The interval semantics live entirely in the `max` augmentation, not in any ordering of `lo`. A reimplementer who builds a self-balancing red-black interval tree (the textbook structure) will get the right answers but a different, more expensive structure — the binary deliberately trades insert/delete flexibility (which it never needs, the interval set is built once) for a free, cache-friendly array layout.

> **GOTCHA —** non-interfering DRAM tensors **freely share/alias** the same address range. `search` places a node only into gaps *between its interval-overlapping neighbors*; two tensors whose `[def,use]` intervals never overlap may be assigned identical or overlapping addresses. This is liveness-based reuse, exactly the SBUF colorer's "non-adjacent nodes share a color" — but here the overlap test is `getAllOverlaps`, not a colored adjacency graph. A reimplementer who treats every DRAM tensor as needing a private byte range will over-allocate by a large factor.

### Contrast with the SBUF/PSUM colorer

| | SBUF / PSUM (Chaitin–Briggs) | DRAM (this page) |
|---|---|---|
| Interference | explicit graph (adjacency bitsets, move sets) | implicit: `IntervalTree` overlap query over `[def,use]` |
| Build | `build` graph, `O(V²)` adjacency | `build_interval_tree`, `O(n log n)` |
| Order | Chaitin simplify + coalesce | `simplify` `chooseOne` over a `SparseSet` (no coalesce) |
| Spill | spill-cost loop when `K` colors exhausted | **no spill** — DRAM is the bottom; the only ceiling is the CC-buffer partition |
| Assign | color = bank / partition+offset (bounded 2-D geometry) | first-fit aligned byte address in the lowest free gap (linear scan) |
| Alignment | GCA over access patterns (variable) | flat 4 KiB page (`getAlignmentSize` `memType==8` → `0x1000`) |

The geometry the SBUF/PSUM colorers place into is [`sbuf-psum-geometry`](../arch/sbuf-psum-geometry.md); the wiring that runs this allocator after them is [`allocator-drivers`](allocator-drivers.md).

---

## Page / GCA Alignment

### Purpose

Every DRAM allocation is rounded up to, and placed on, a **fixed 4 KiB page**. `getAlignmentSize` is the shared alignment function for all memory spaces, but for DRAM it short-circuits to a constant — the access-pattern "greatest-common-alignment" (GCA) computation is only exercised for SBUF/PSUM.

### Algorithm

```c
function getAlignmentSize(loc, opts, b):              // sub_0x1091890
    switch (loc->memType):                            // read via [rdi+0xD8]->[0]
        case 8:    return 0x1000                       // DRAM → 4096, FIXED  (0x1091fe5: mov $0x1000,%r12d)
        case 0x10: ... GCA over access patterns ...    // SBUF/PSUM only
        default:   return 1
```

The DRAM path at `0x1091fe5` is an unconditional `mov $0x1000, %r12d ; ret` — confirmed directly by disassembly (`cmp $0x8` at `0x10918b3`, `cmp $0x10` at `0x10918bc`, the constant at `0x1091fe5`). The SBUF/PSUM (`0x10`) path walks the location's reader/writer `AccessPattern` arguments and reduces an alignment value across partition strides, thresholded by a `PassOptions` field (`[opts+0xAC] > 29`); it is not on the DRAM path.

> **QUIRK —** `--dram-page-size` (cl::opt<int>, default **512**; flag name at `.rodata` `0x1dc19a3`, description "DRAM page size for GCA allocation aligment" at `0x1dc1678`) does **not** size the DRAM page. It feeds the *SBUF* GCA path (`memType==0x10`); a true DRAM tensor (`memType==8`) always aligns to a hard 4096 regardless of `--dram-page-size`. The knob's name is misleading: it is the SBUF allocator's GCA granule. **[CONFIRMED — the DRAM path ignores `opts`; the constant `0x1000` is unconditional.]**

`roundUp` everywhere is therefore `alignedSize = (byteSize + 4095) & ~4095`, computed at `renumber_locations` time into `Info[+0]` and consumed unchanged by `search`.

---

## First-Fit Placement — search

### Purpose

`search` is the address selector: given a node's list of **occupied** intervals (the address ranges of all its interval-overlapping, already-placed neighbors), find the lowest free gap that fits `info[node].alignedSize`, with 4 KiB page-boundary protection. This is linear-scan first-fit — the interval tree decides *who* interferes; `search` decides *where* in the free address space the survivor lands.

### Entry Point

```text
DRAM_Allocator::select_impl(Function*, vector<Info>&, vector<int> order, ulong start)   0xa7a730
  └─ DRAM_Allocator::search(vector<Info>&, vector<Interval>&, int node, ulong start, ulong align)  0xa78740
```

`DRAM_Allocator::Interval` is `{ u64 lo, u64 hi }` (a byte-address range). Source: `coloring_allocator/src/dram_allocator/dram_select.cpp` (asserts cite `:70`, `:251`).

### Algorithm

```c
function search(info, occupied, node, start, align):     // sub_0xa78740  (align = 4096)
    sort occupied by .lo                                  // introsort + insertion-sort, in place
    free = []                                             // build the free-list of gaps
    prevEnd = start
    for [lo,hi] in occupied:                              // sweep sorted occupied ranges
        if lo > prevEnd: free.push([prevEnd, lo])
        prevEnd = max(prevEnd, hi)
    free.push([prevEnd, this[296]])                       // trailing gap up to region ceiling (this+296)

    size = info[node].alignedSize                         // Info[+0], u64 (multiple of 4096)
    for [lo,hi] in free:                                  // FIRST-FIT (dram_select.cpp ~263-285)
        if hi - lo >= size:                               // gap big enough
            endPage = (lo + size - 1) / align             // page index of the last byte
            if lo/align == endPage || size > align:       // fits in one page, or big alloc
                info[node].address = lo                   // place at raw gap start
                info[node].allocated = 1; return
            else if hi - align*endPage >= size:           // sub-page alloc straddles a page
                info[node].address = align*endPage        // bump up to next page boundary
                info[node].allocated = 1; return
        // else advance to next gap
    assert info[node].allocated                           // dram_select.cpp:70
```

> **QUIRK —** the alignment is **page-straddle avoidance, not unconditional round-up**. A sub-page (< 4 KiB) allocation is placed at the *raw gap start* as long as it stays inside one 4 KiB page; only if it would cross a page boundary is it bumped up. Any allocation with `size > align` — i.e. every real DRAM tensor, since `alignedSize` is itself a multiple of 4096 — always takes the raw gap start. Because the gaps lie between page-aligned occupants, those starts are page-aligned anyway. A reimplementer who unconditionally rounds the chosen address up to a page boundary will waste up to 4095 bytes per sub-page object and diverge from the binary's tighter packing. **[CONFIRMED — the two-arm placement branch in `dram_select.cpp`; `Interval` `{u64,u64}` from the signature.]**

### The simplify elimination order

`simplify` (`@0xa814a0`, `dram_simplify.cpp`) produces the order `select_impl` pops nodes in. Despite the interval-tree interference, the ordering heuristic is the classic Briggs worklist: it logs `"simplify interference graph"`, computes a per-node lo/hi liveness span and a "hi ratio", then repeatedly `chooseOne` (asserting the `SparseSet` worklist is non-empty) — removing the least-constrained node, pushing it onto an elimination stack, and re-scanning its interval-tree neighbors for nodes that drop below the simplify threshold (`"new candidates = N"`). The helper `simplify_step1` `@0xa80e40` carries the `SparseSet<unsigned>` include/exclude worklists and the neighbor `unordered_map`. `select_impl` pops the stack back-to-front: least-constrained popped last, hardest first — Briggs optimistic order. There is **no coloring and no spill**; the stack is purely an assignment order.

### select and select_impl

`select` `@0xa7ef30` chooses the path: if `inPlaceFlag` is set or `dram_shared mode (this+968) != 1`, it calls `find_start_addr` then `select_impl` directly (the common path). Otherwise (multi-core dram_shared mode 1) it computes `coreCap = this[488] << 20` (MB-per-core × 1 MiB) and runs per-core slab partitioning before `select_impl`. `select_impl` `@0xa7a730` resets `allocated=0` on every non-pinned node, sets the CC-buffer ceiling, then for each popped node builds its occupied-interval list by querying the tree for already-allocated overlapping neighbors, projects each to `[address, address+alignedSize)`, and calls `search`. If a placement would exceed the CC-buffer ceiling it retries `search` in the CC-buffer region. It writes the chosen address back via `MemoryLocation::setAddress`, logs `"allreduce_dram_hwm"` and `"Real CC buffer size"`, and writes `FunctionAttribute` key 13 (real CC buffer size).

### find_start_addr — the HWM floor

```c
function find_start_addr():                              // sub_0xa77710
    hwm = this[123]                                       // byte 0x3d8 (qword)  — cmpq $0,0x3d8(%rdi)
    if hwm == 0: return 0                                 // 0xa7771a: xor %eax,%eax; ret
    log "Already used DRAM hwm: <hwm>"
    return hwm + 1                                        // start one byte past existing DRAM footprint
```

Confirmed directly: at `0xa77710` the body opens `cmpq $0x0, 0x3d8(%rdi)` (`this+123` = byte offset 984 = `0x3d8`) and returns 0 when the HWM is zero. Fresh spill/intermediate DRAM tensors are placed **above** the pre-existing addressed tensors (inputs, outputs, weights at fixed addresses). The HWM is both the reported `"reserved space"` metric (`addMetric(31)`) and the allocation floor. The per-core HBM window that caps this is in [`dram-hbm-geometry`](../arch/dram-hbm-geometry.md).

---

## CC-Buffer Auto-Sizing

### Purpose

Collective-communication (allreduce / collective-op) pipelines stage their data in a reserved DRAM region of bounded size. The size comes from `--allreduce-buffer-size` (MB, default 500); when set to `-1`, `cc_buffer_size` computes it from the actual CC operands in the function and page-aligns the result.

### Algorithm

```c
function cc_buffer_size(info):                           // sub_0xa796c0
    total = 0
    for inst in func:
        if is_collective_op(inst):                       // allreduce / CC op
            for op in inst.operands:
                loc = op.getLocation()                   // PhysicalAccessPattern
                total += info[loc[+348]].size            // Info size at the loc's node index
                total += apSize(op)                      // access-pattern footprint
    return total                                         // bytes

// in select_impl:
ccLimit = this[516] << 20                                // --allreduce-buffer-size (MB) → bytes
if this[516] == -1:                                      // auto
    ccLimit = roundUpPage( cc_buffer_size(info) )        // 4 KiB round
    log "CC buffer size limit N"
FunctionAttribute[key=13] = real_cc_buffer_size
```

The CC-buffer region is reserved separately from the rotatable / general region because collective buffers must sit at a known, stable address range that the DMA/collective layer targets. When `select_impl` cannot place a node below the CC ceiling, it retries in the CC-buffer region (the two `search` calls). The same `--allreduce-buffer-size` knob and `"Real CC buffer size"` accounting reappear in DRAM rotation ([`address-rotation`](address-rotation.md)), which must preserve the CC region across re-placement.

### Related Knobs

| Knob | Type | Default | Description |
|---|---|---|---|
| `--allreduce-buffer-size` | int (MB) | **500** | CC-op pipeline buffer size (`0x1F4` initializer at `0x7c441c`); `-1` → `cc_buffer_size` auto-computes (`this+516`) |
| `--dram-page-size` | int | **512** | GCA granule for the *SBUF* path; DRAM ignores it (flat 4 KiB) |
| `--print-GCA-debug` | bool | **false** | `this+485` → `Module::save("GCA_debug.bir")` |

---

## The disable_dram_allocation Gate

### Purpose

`--disable-dram-allocation` (cl::opt<bool>, strings `"disable_dram_allocation"` / `"disable dram allocation"` / `"disable-dram-allocation"` at `.rodata` `0x1dc...`) turns off DRAM placement entirely. When set, the `coloring_allocator_dram` step is **not emitted/run** by the pipeline builder; spills are left unaddressed in DRAM and a downstream stage (or the runtime) must assign them, or an error is raised if a DRAM spill survives.

### Where the gate is checked

The gate is in the **pipeline builder**, not inside `allocate`. `allocate` (`@0xa6cb20`) has **zero** references to the `disable_dram_allocation` global (the `cl::opt<bool>` storage at `0x3dfb840`, accessed via the option-object pointer at `0x3dc3f80`) — it assumes it is reached only when allocation is enabled. The builder `sub_80A6E0` (the optlevel 1/2/3 cascade, documented in [`allocator-drivers`](allocator-drivers.md)) guards the `coloring_allocator_dram` emission with the flag — at `0x80b4dc` it loads the option pointer, tests `cmp byte [rax+0x78], 0` and `jnz` past the `lea "coloring_allocator_dram"` at `0x80b4e9` — exactly as `address_rotation_dram` is guarded by `--disable-dram-rotation`. This mirrors the gate placement of the HBM-budget knob in [`dram-hbm-geometry`](../arch/dram-hbm-geometry.md).

```asm
80b4dc:  mov  rax, [rip+disable_dram_allocation_ptr]   ; 0x3dc3f80
         cmp  byte [rax+0x78], 0                        ; the cl::opt<bool> value
         jnz  loc_80b523                                ; skip if disabled
80b4e9:  lea  rsi, "coloring_allocator_dram"            ; else emit the DRAM pass
```

> **GOTCHA —** a reimplementer who puts the `--disable-dram-allocation` check inside the allocator (an early `if (disabled) return`) will diverge: the binary skips the entire pass *registration* in the builder, so none of `coloring_allocator_dram`'s paired re-tuning passes (`dynamic_dma_cleanup`, `build_fdeps`, the post-DRAM anti-dependency analyzer) run either. The gate is structural, at pipeline-construction time, not a runtime early-out. **[CONFIRMED — builder gate; `allocate` carries no reference to the flag global.]**

---

## Cross-LNC Shared-DRAM Partitioning

### Purpose

A model is cloned across `N` LNC cores by `lnc_splitter`. Most DRAM tensors are per-core private, but collective-communication staging buffers and cross-core handoff tensors must be **one physical buffer all N cores address identically**. The allocator marks those locations, places them, and a dedicated pass equalizes their addresses across the per-core modules. The on-chip side (SBUF/PSUM replicated, DRAM partitioned) is the model in [`lnc-memory-model`](../arch/lnc-memory-model.md); this section is the allocator-level mechanism that realizes the one shared region.

### The isSharedPostDRAMAlloc marker

`bir::MemoryLocation::setIsSharedPostDRAMAlloc(bool)` is **imported** from `libbir`; its sole caller is `DRAM_Allocator::allocate` at `0xa6e7b1`. The marking loop iterates `func.allocs()`, applies an allocator-held predicate (a `std::function` wrapper, vfn slot `+0x20`, returning whether the location belongs to the cross-core shared set), and tags matching locations `setIsSharedPostDRAMAlloc(true)`. The whole block is gated by a module-name compare so it runs only in dram_shared mode (the `from_space = Shared` registration). Marking happens *during* allocation: once a shared location is placed, the flag tells every later stage to treat it as a single cross-core object rather than a per-core private spill.

`bir::MemoryLocation::isSharedPostDRAMAlloc() const` (imported) is read at **eight** confirmed sites — the flag is the single source of truth for "this DRAM object is one buffer shared by all N cores":

| Reader | Address | What it does with the flag |
|---|---|---|
| `DRAM_Allocator::allocate` | `0xa6e…` | the marker; skips re-placement of shared locs |
| `AntiDependencyAnalyzer::enqueueUseBatch` | `0x8c5260` | shared locs get cross-core WAR ordering |
| `LiveMemSize::operator-(MemoryLocation)` | `0x10e1f50` | exclude shared bytes from per-core pressure |
| `getMemPressure(Function&, …)` (2 sites) | `0x10e4000` | shared not double-counted per core |
| `MemoryAnalysis::filter_mem(Set&, MemoryType, MemoryAddressSpace)` | `0x10f7d60` | filter shared out of per-core analysis |
| `LncVerifier::checkSubgraphsWithConcretizedShardIds` | `0x16772a0` | verify final shared placement matches |
| `SyncSharedAllocations::run(vector<Module>&)` | `0x16b2da0` | drive cross-core address equalization |

> **QUIRK —** "shared" DRAM is counted **once** across all N cores, not N times, for memory-pressure purposes. `getMemPressure` / `LiveMemSize::operator-` / `MemoryAnalysis::filter_mem` all exclude `isSharedPostDRAMAlloc` bytes from per-core accounting. A reimplementer that sums every core's DRAM footprint including its copy of the shared buffers will over-report HBM usage by `(N-1) ×` the shared region and may spuriously fail the HBM budget check. **[CONFIRMED — the three accounting readers above.]**

### sync_shared_allocations — address equalization

`SyncSharedAllocations` is a multi-module pass: `run(bir::Module&)` `@0x16b19b0` is a near-no-op single-module stub; the real work is `run(vector<unique_ptr<Module>>&)` `@0x16b2da0`, which receives all N per-core modules and reconciles the shared buffers across them. It does **not** insert a runtime barrier — it makes every core's copy of a shared tensor resolve to one identical physical address at compile time.

```c
function SyncSharedAllocations_run(modules):             // sub_0x16b2da0
    if numCores == 1: return                             // cmp DWORD[module+0x1a4],1 — trivial single-core path
    for module in modules:
        for loc in module.function.memlocs():            // r15
            if loc[168]!=0 || *(*(loc+224))!=7: continue  // already-addressed, or usage class != 7
            if !loc.isSharedPostDRAMAlloc(): continue     // 0x16b32fb
            opt = loc.getRemoteLocalTarget()              // imported; { remote_name, coreIdx }
            assert opt.has_value()                        // "memloc.getRemoteLocalTarget().has_value()"
            remote = *opt
            rfunc  = modules[remote.coreIdx].getFunctionByName(<derived>)
            rmloc  = rfunc.getMemoryLocationByName(remote.remote_name)
            assert rmloc != null                          // "remote_mloc != nullptr"
            loc.setAllocated(true)
            addr = loc.getAddress(); part = loc.getBasePartition(); bank = loc.getBankId()
            rmloc.allocate(memType, addrSpace, addr, len, part, bank, /*b*/, &APs)   // SAME addr/part/bank
            assert rmloc.isAllocated()                    // "remote_mloc->isAllocated()"
            log "Assigned memloc <name> to address <addr> based on its remote local target"
```

The cross-core link is the bir `RemoteLocalTarget` (`getRemoteLocalTarget()` → `{ string remote_name, coreIdx }`, imported). The pass resolves the sibling core's matching `MemoryLocation` **by name** and writes the identical `(address, partition, bank)` triple into it. The result: every core's copy of a cross-core-shared DRAM tensor resolves to one physical address, so all cores read/write the same bytes. Mismatches raise `logging::NeuronAssertion<ErrorCode>` with `RESOLUTION_CONTACT_SUPPORT` / aws-neuron-sdk issue links.

> **NOTE —** this is compile-time address agreement, not synchronization in the runtime sense. The actual happens-before — a write on one core observed by another — is provided at runtime by `birsim::NeuronCoresManager` (`getSharedDRAMforRank(rank)` hands each rank the shared region; `doSyncPhysicalCores` is the cross-core barrier) and by the ordering instructions emitted downstream. `sync_shared_allocations` only guarantees those ordering primitives all target the same address. **[CONFIRMED — the `allocate`-equalization call; `NeuronCoresManager` symbols imported.]**

### Why anti-deps re-run after shared placement

Once shared-DRAM tensors have real addresses, the *same physical bytes* may be touched by accesses that were independent before placement (distinct logical tensors coalesced into the shared region). `anti_dependency_analyzer_post_shared_dram` — the same `AntiDependencyAnalyzer` class parameterized with the shared `MemoryAddressSpace` set — rebuilds WAR/anti dependences over the freshly-placed shared region (its `enqueueUseBatch` at `0x8c5260` routes `isSharedPostDRAMAlloc` locations through a distinct cross-core use-map so a use on one core is ordered against a def on another). The VNC cross-core variant of this flow is documented separately (planned page, `walrus/` VNC cross-core).

### The shared-DRAM pass cluster

| Pass | Registration / entry | Role |
|---|---|---|
| `coloring_allocator_dram_shared` | `_M_invoke` `0x989e80` (`Type=2`, `from_space=Shared`) | `DRAM_Allocator` in shared mode; marks + places shared locs |
| `coloring_allocator_dram_shared_post_lnk` | `_M_invoke` `0x98a220` (`post_lnk=1`) | shared region re-laid after `bir_linker` merges N modules |
| `sync_shared_allocations` | `run(vector<Module>&)` `0x16b2da0` | cross-core address equalization |
| `anti_dependency_analyzer_post_shared_dram` | `_M_invoke` `0x8fe030` → builder `0x8fd5f0` | re-derive WAR/anti-deps over the shared region |
| `memory_analysis_after_coloring_allocator_dram_shared` | `_M_invoke` `0x11070c0` | recompute live-mem after the shared allocator stage |

---

## Infrastructure Functions

| Function | Address | Role | Confidence |
|---|---|---|---|
| `DRAM_Allocator::allocate(Function*)` | `0xa6cb20` | driver; scan / mark-shared / build-tree / assign | CONFIRMED |
| `DRAM_Allocator::candidate(MemoryLocation&)` | `0xa6f0c0` | DRAM eligibility predicate | CONFIRMED |
| `DRAM_Allocator::build_interval_tree(Function*, vector<Info>&)` | `0xa71a80` | flat numbering + interval emission + tree build | CONFIRMED |
| `DRAM_Allocator::renumber_locations(ArrayRef<MemoryLocation*>)` | `0xa74360` | dense `Info[]` ids, `alignedSize` round-up | CONFIRMED |
| `DRAM_Allocator::find_start_addr()` | `0xa77710` | HWM floor (`this+123` / byte `0x3d8`) | CONFIRMED |
| `DRAM_Allocator::search(vector<Info>&, vector<Interval>&, int, ulong, ulong)` | `0xa78740` | first-fit, page-straddle protection | CONFIRMED |
| `DRAM_Allocator::cc_buffer_size(vector<Info>&)` | `0xa796c0` | auto-size CC buffer region | CONFIRMED |
| `DRAM_Allocator::select_impl(Function*, vector<Info>&, vector<int>, ulong)` | `0xa7a730` | per-node assignment loop | CONFIRMED |
| `DRAM_Allocator::select(Function*, vector<Info>&, vector<int>, bool)` | `0xa7ef30` | path select (in-place vs shared slab) | CONFIRMED |
| `DRAM_Allocator::simplify(Function*, vector<Info>&)` | `0xa814a0` | Briggs elimination order | CONFIRMED |
| `DRAM_Allocator::simplify_step1(...)` | `0xa80e40` | `SparseSet` worklist helper | CONFIRMED |
| `IntervalTree::makeTree(int, int)` | `0x9a7a00` | balanced BST by array index | CONFIRMED |
| `IntervalTree::makeMax(int)` | `0x9a8000` | post-order subtree-max augmentation | CONFIRMED |
| `IntervalTree::getAllOverlaps(int) const` | `0x9a8a10` | interference oracle (query by node) | CONFIRMED |
| `IntervalTree::getAllOverlapsWithInterval(int, int) const` | `0x9a8740` | interference oracle (query by `[lo,hi]`) | CONFIRMED |
| `getAlignmentSize(MemoryLocation*, PassOptions&, bool)` | `0x1091890` | alignment; DRAM → `0x1000` (4 KiB) | CONFIRMED |
| `SyncSharedAllocations::run(vector<Module>&)` | `0x16b2da0` | cross-core shared-DRAM address equalization | CONFIRMED |
| `SyncSharedAllocations::run(Module&)` | `0x16b19b0` | single-module stub (near no-op) | CONFIRMED |
| `bir::MemoryLocation::setIsSharedPostDRAMAlloc(bool)` | imported | mark a loc cross-core (sole caller `allocate`) | CONFIRMED |
| `bir::MemoryLocation::isSharedPostDRAMAlloc() const` | imported | the cross-core flag (8 readers) | CONFIRMED |

> **Gaps.** The `Info` struct (280-byte stride: `alignedSize@+0`, `allocated@+45`, `pinned@+46`, `def_use_vec@+56`, `memloc@+176`, `address@+256`) is derived from offset use across `renumber_locations`/`search`/`select_impl`, not from RTTI (STRONG, not CONFIRMED). The exact integer values of the `usageClass ∈ {7,9}` candidate filter and the `shared_predicate` vfn body are read from call shape, not fully decompiled (STRONG). The runtime-side `NeuronCoresManager` barrier sequence (`doSyncPhysicalCores` / `unsafeNotifyNext`) is named from imports, not traced here.

---

## Related Components

| Name | Relationship |
|---|---|
| `ColoringAllocator` (`Type=2`) | the registration that constructs `DRAM_Allocator`; eight-name → one-class collapse |
| `SB_Allocator` / `PSUM_Allocator` | the Chaitin–Briggs colorers run *before* DRAM in the cascade |
| `AddressRotation::dram_rotation` | relocates allocated DRAM nodes within a rotation window after this pass |
| `AntiDependencyAnalyzer` (`post_shared_dram`) | re-derives WAR/anti-deps over the placed shared region |
| `birsim::NeuronCoresManager` | runtime realization of the shared region + cross-core barrier |

## Cross-References

- [Allocator Drivers — ColoringAllocator Wiring](allocator-drivers.md) — the `Type=2` registration and `PSUM→SB→DRAM→DRAM-shared` cascade that runs this allocator
- [The multi-core (LNC) memory model](../arch/lnc-memory-model.md) — `N` cores, DRAM partitioned, the one shared region this allocator equalizes
- [DRAM / HBM Geometry and the DRAM Split](../arch/dram-hbm-geometry.md) — the per-core HBM window that caps the HWM, and the HBM budget gate
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — the bank/partition geometry the colorers (run before DRAM) place into
- [Address Rotation and the Emergent Software Pipeline](address-rotation.md) — relocates the DRAM layout after allocation; the 8 GiB rotation cap and CC-buffer preservation
