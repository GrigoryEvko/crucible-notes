# VN-Splitter and Shrink-ML — Allocator-Prep Footprint Reshaping

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical Cython rebuilds). Everything here lives in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `0x5e9020`–`0x62d650` is the `.plt` thunk band, so every body address cited here is the real high body (`0xd4xxxx`–`0xd7xxxx` for VN-Splitter, `0x16bxxxx`–`0x182xxxx` for Shrink), never its thunk. `.data` carries a +0x400000 delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

Two consecutive walrus backend passes reshape the live-range graph *before* the coloring allocator runs, so that coloring can succeed without spilling. They allocate nothing themselves; they edit the set of `MemoryLocation` nodes the allocator will later color. Pass **24** (`vn_splitter`) makes every SBUF/PSUM node fit one SB tile and breaks access-disjoint clusters into independent nodes; pass **25** (`shrink_ml`, the `ShrinkDN` class) trims each node's physical extent down to the bytes actually touched. Together they hand the allocator a *disjoint, minimal, SB-fitting* node set. This is the seam after the pipeline ([`pass-pipeline-optlevels`](pass-pipeline-optlevels.md)) and before the colorers ([`allocator-drivers`](allocator-drivers.md)).

The single most important structural fact — and the one that corrects a naive reading — is that **`vn_splitter` is not a splitter; it is a split-then-fold body.** `VNSplitterPass::run` (`@0xd73890`) calls *two* transforms in sequence: `VNSplitter::runTransform` (the **SPLIT** phase, called first at `d73d6a`) and then `VerticalFusion::runTransform` (the **FOLD** phase, called second at `d74162`). Both call sites are directly visible in the dispatch, in straight-line order with no reorder branch between them. The splitter carves a tensor along a dimension into `N` disjoint `_VN_` sub-tensors; vertical fusion then folds producer→consumer op chains vertically to collapse intermediate-tensor liveness over the result. The two are inverse footprint moves — split *increases* node count by carving, fold *reduces* it by merging — and the pass runs split-first, so the fold operates on the chains the splitter produced (and any residual chains). The `foldIntoPredecessor` `PassOption` gates the fold stage.

Both drivers agree on that order: the PGA entry `runVNSplitterOnce` (`@0xd6f440`) also splits (`d6fad2`) before folding (`d6febf`). [INFERRED] The *reason* the fold runs after the split — to collapse the chains the split just produced, along with any residual chains — is not stated anywhere in the binary; only the order is.

The splitter's piece count `N` is not a constant: it is set by the `(vn_limit, ratio, perSplitLimit)` triple the pass is configured with — the float `ratio` bounds tolerated cross-piece duplication, the two integer caps bound the virtual-node count and pieces-per-node. The same triple is the argument list of `ProfileGuidedAutoTuning::runVNSplitterOnce(int, float, int)`, which drives this identical fold+split body over a 64-cell grid of those values. That sweep is **dead code in this build** — nothing calls it and it selects nothing, see [PGA](pga-feedback.md) — so in a real compile `N` is a function of the configured options only, not of a search. The `shrink_ml` half is unconditional: it minimizes the per-partition live byte-extent of every shrinkable node, leaving reinterpret-cast and shifted-partition nodes intact.

For reimplementation, the contract is:

- The **split↔fold dispatch**: `VNSplitterPass::run` → `VNSplitter::runTransform` then `VerticalFusion::runTransform`, the fold gated by `foldIntoPredecessor`.
- The **split decision**: how `analyze` clusters access-patterns into `ApGroup`s by overlapping partition sets, and the `isValidForSB` partition + byte gate that decides "splittable".
- The **split rewrite**: how `split` emits `<orig>_VN_<k>` sub-tensors, re-points cloned access-patterns, and rebases offsets (`normalizeOffsetToNewNode`).
- The **split-factor knobs**: `(vn_limit, ratio, perSplitLimit)` — also the `runVNSplitterOnce` argument triple — and the three reject-strings that pin each role.
- The **footprint shrink**: `ShrinkDN::analyze` probes writers for the true live window, `shrinkDN` re-packs partition stride and trims free dims; the set-level `shrinkTensor` annotates `(shrinkDim, liveN)`.
- The **invariants** handed to the colorer (SB-fittability, use-def locality, clean graph, minimal footprint).

| | |
|---|---|
| **Pass 24 entry** | `VNSplitterPass::run(bir::Module&)` `@0xd73890` |
| **Pass 24 registrar** | `register_generator_vn_splitter__` `@0x3e0135d`; lambda `_M_invoke` `@0xd75660` |
| **SPLIT phase (runs 1st, `d73d6a`)** | `VNSplitter::runTransform()` `@0xd5a3d0` |
| **FOLD phase (runs 2nd, `d74162`)** | `VerticalFusion::runTransform()` (`.plt` `@0x60ab60`); internals not traced here |
| **Split decision** | `VNSplitter::analyze(MemoryLocation*, float, int, int)` `@0xd53020` |
| **SB-fit gate** | `ApGroup::isValidForSB(SBModel, bool)` `@0xd501e0` |
| **Sub-tensor emit** | `VNSplitter::split(...)` `@0xd57c80` |
| **PGA driver** | `ProfileGuidedAutoTuning::runVNSplitterOnce(int, float, int)` → same body |
| **Pass 25 entry** | `ShrinkDN::run(bir::Module&)` `@0x16c4db0` (`shrink_dn.cpp`) |
| **Pass 25 registrar** | `register_generator_shrink_ml__` `@0x3e0578d`; lambda `_M_invoke` `@0x16c5be0` |
| **Shrink probe** | `ShrinkDN::analyze(MemoryLocation&)` `@0x16c07a0` |
| **Shrink rewrite** | `ShrinkDN::shrinkDN(MemoryLocation&)` `@0x16c38a0` |
| **Set-level analyzer** | `shrinkTensor(bir::Function&)` `@0x1826e00` |
| **Source TUs** | `walrus/vn_splitter/src/vn_splitting.cpp`, `walrus/shrink_ml/src/shrink_dn.cpp` |
| **Namespace** | `neuronxcc::backend::{VNSplitter, VerticalFusion, ShrinkDN}`; BIR is `neuronxcc::bir` |

---

## 1. Pass identity and the split-then-fold dispatch

Both passes are `BackendPass` subclasses registered into the walrus pass set by generator lambdas. The registrars come from the registry strings, and the pass-order positions (`vn_splitter=24`, `shrink_ml=25`) from the pass-order map.

Two independent things tie `shrink_ml` to `ShrinkDN`: the class-name string `"ShrinkDN"` is co-resident (×36 occurrences) with the pass-name `"shrink_ml"` (×13), and `shrink_dn.cpp` is the only source TU under `shrink_ml/`. The `run` body carries the mangled name `_ZN9neuronxcc7backend8ShrinkDN3runERN3bir6ModuleE`.

The `vn_splitter` body does not call one splitter — it calls *two* transforms, **split first then fold**, and the dispatch is directly readable in the disassembly:

```text
d73d6a:  call   5f4930 <…VNSplitter12runTransformEv@plt>        // SPLIT phase
d74162:  call   60ab60 <…VerticalFusion12runTransformEv@plt>    // FOLD phase
d74249:  call   5ee600 <…VerticalFusionD1Ev@plt>                // dtors
d74252:  call   5ed870 <…VNSplitterD1Ev@plt>
```

The `VNSplitter::runTransform` call at `d73d6a` runs first; the `VerticalFusion` vtable (`_ZTVN9neuronxcc7backend14VerticalFusionE`) and typeinfo (`_ZTIN…14VerticalFusionE`) are referenced at `d73e93`/`d73ea3`, after which `VNSplitterPass::run` constructs a real `VerticalFusion` object and runs its `runTransform` at `d74162`. The same two transforms in the same split→fold order are driven by `ProfileGuidedAutoTuning::runVNSplitterOnce` (`@0xd6f440`: split at `d6fad2`, fold at `d6febf`); the autotuner re-runs the whole split+fold body per trial.

> **QUIRK — `vn_splitter` is a two-stage body, not a monolithic splitter.** A reimplementer who treats `vn_splitter` as "split a tensor" will miss half the pass. The first stage is the virtual-node split (`VNSplitter::runTransform` at `d73d6a`); the second stage is `VerticalFusion` (the fold) at `d74162`, gated by the `foldIntoPredecessor` option. The split carves the over-sized nodes *first*; the fold then collapses the producer→consumer chains over the resulting (and any residual) node set. `VerticalFusion::runTransform`'s internals are a separate symbol and are **not traced on this page** [GAP] — only its position in the dispatch and its gating option are pinned.

### 1.1 `PassOptions` knobs

The following keys are parsed from `.rodata` (single occurrence each, so each is a distinct option): `vn_limit`, `vnLimit`, `perSplitLimit`, `noSplitDRAM`, `foldIntoPredecessor`, `splitSB`/`SplitSB`, `VNSplitterOnce`. The autotuner entry `runVNSplitterOnce(int vn_limit, float ratio, int perSplitLimit)` consumes the three tuning knobs as a positional triple; its mangled member-function signature `…ProfileGuidedAutoTuning…(f,i)…i,f,i…` fixes the `(int, float, int)` parameter shape. The per-knob roles come from the dedicated reject-strings below; the `runVNSplitterOnce` body itself was not separately traced.

---

## 2. VN-Splitter — the SPLIT phase

### 2.1 `runTransform` — the orchestrator (`@0xd5a3d0`)

`runTransform` first caches the SB geometry from the arch model, then runs a two-pass analyze/split sweep over each function's `MemoryLocation` set.

```c
// VNSplitter::runTransform()  @0xd5a3d0
void VNSplitter::runTransform() {
    Arch arch = Module::getArch();              // d5a532
    ArchModel *am = getArchModel(arch);         // d5a53a
    // SBModel cached from the ArchModel chain am[+8]->[+0x10]->[+0x28]:
    this->sbBytesPerPartition = am_sb[+0];      // this+0x30  (per-partition SBUF budget)
    this->sbPartitionCount    = am_sb[+8];      // this+0x34  (e.g. 128)
    // option bytes off the ArchModel (this+0xa8 = ArchModel*):
    bool noSplitDRAM        = am[+0xa8][+0x150]; // cmpb $0,0x150(%rax)  @d5a63f
    bool foldIntoPredecessor= am[+0xa8][+0x151]; // bit at 0x151        @d5a5ef

    // PASS A — decide which nodes split (per bir::Function via allocs()):
    for (MemoryLocation &ml : Function::allocs()) {     // Function::allocs @0x5f4a30, d5a66e
        if (excluded_by_user(ml)) continue;             // "VNSplitter: Skipping <name> by user request."
        if (skip_non_SB_or_DRAM(ml, noSplitDRAM)) continue;
        bool splittable = analyze(&ml, ratio, vn_limit, perSplitLimit);  // d5af9b
        log("INFO (VNSplitter)  Analyzing <ml>");
        if (splittable)
            log("<ml> is splittable, shape=(<P> partitions, <B> bytes)!");
    }

    // PASS B — split + dead-node sweep:
    collect_internal_vnodes();                          // "Collected all the internal vnodes: …"
    for (MemoryLocation &ml : splittable_set) {
        kind = ml.tensorKind;
        bool in  = isTensorKindInput(kind);             // d5bcc1
        bool out = isTensorKindOutput(kind);            // d5bcd7
        split(&ml, ratio, foldIntoPredecessor, deadSet, keepMemLocs, keepInsts);
    }
    // erase the originals the split made redundant:
    for (auto &d : deadSet)
        if (is_MemoryLocation(d) && !keepMemLocs.count(d)) {
            ml = Function::getMemoryLocationByName(name(d));
            Function::removeMemoryLocation(ml, /*recursive=*/true);
        }
    log("INFO (VNSplitter): Split <N> nodes … into <P> pieces.");
    log("(SplitSB) total_sb_nodes = …, can_sb_nodes = …, total dead nodes = …, "
        "vn_limit = …, foldIntoPredecessor = …");
    addMetric(BackendMetricType, "VNSplitterDeadNodesCount", <u32>, Aggregation);  // d5aa24/d5aa9c/d5ac3b
}
```

Both option-byte reads are visible in the disassembly: `cmpb $0x0,0x150(%rax)` at `d5a63f` is the `noSplitDRAM` test, and the `0x151` byte at `d5a5ef` is `foldIntoPredecessor`. The `VNSplitterDeadNodesCount` metric (string at VA `0x1d95cd9`, mangled `backend::VNSplitterDeadNodesCount`) is emitted from three `addMetric` sites; [INFERRED] its value is the count of `MemoryLocation`s removed this run, matching the `"total dead nodes"` figure in the `SplitSB` log.

### 2.2 `analyze` — the split-shape decision (`@0xd53020`)

`analyze(MemoryLocation* ml, float ratio, int vn_limit, int perSplitLimit)` is a four-stage funnel: pre-filter unsplittable tensors, sweep access-patterns for legality, cluster legal APs into `ApGroup`s, then test each group against the SB budget.

```c
// VNSplitter::analyze(ml, ratio, vn_limit, perSplitLimit)  @0xd53020
bool VNSplitter::analyze(MemoryLocation *ml, float ratio, int vn_limit, int perSplitLimit) {
    // (a) PRE-FILTER:
    if (isDRAMOnly(ml)) return false;   // DRAM-resident tensors are not SB-split (unless noSplitDRAM allows)
    if (isMx(ml)) special_handling();   // MX (block-scaled) tensors take a separate path

    // (b) AP-LEGALITY SWEEP over writers<AccessPattern>() and readers<AccessPattern>():
    for (AccessPattern *ap : ml.writers_and_readers()) {
        if (ap.isShiftedPartitionAccess())                  // reject: "due to non-contiguous AP." /
            return false;                                   //         "due to transpose (differing partiton step …)"
        if (PhysicalAccessPattern::hasCrossChannelAccess(ap))
            return false;                                   // reject: "due to access across channels."
        // other reject reasons share this string cluster:
        //   "due to reintepret_cast.", "due to partial copy (node size = …)",
        //   "due to CollectiveCompute access or BIRKernel access or indirect access.",
        //   "due to ISA pattern vector assignment needs non-TensorIndirect AP",
        //   "due to flow dependencies: …"
        // instr-type gates: copy(0x2e)/memset(0x2d)/transpose(0x2b)-class opcodes recognised;
        //   AP MemoryType and AP-kind@+0x18==1 select which APs count.
    }

    // (c) GROUPING — use-def clustering into ApGroups:
    collectAccesses(apMap, apaList);            // d52910: one APAccesses per AP (partition set + element range)
    for (APAccesses &a : apaList)
        if (a.is_read())
            findMatchingWrites(a);              // d4bb90: writes whose partition sets overlap this read
    for (APAccesses &a : apaList) {
        ApGroup *g = findMatchingGroup(...);    // d4de60 / findLastGroupWithOverlap d4d380
        if (!g) g = open_new_group();           // APs that never co-access a partition -> SEPARATE group
        g.addReadAPs / g.addWriteAPs(a);
    }

    // (d) PER-GROUP SHAPE + SB-FIT:
    bool any_splittable = false;
    for (ApGroup &g : groups) {
        g.collectStats();                        // memoised: highestElementInPartition, baseOffset, extents
        u32 bytesPP = g.getBytesPerPartition();  // = highestElementInPartition * dtypeBytes[ml.Dtype]
        auto dims   = g.getDimensions();         // [freeExtentBytes, highestElemPerPartition]
        bool valid  = g.isValidForSB({this->sbBytesPerPartition, this->sbPartitionCount}, strict);
        // SPLIT TRIGGER (synthesised): split when APs decompose into >1 disjoint-partition group,
        //   OR a single group overflows the SB per-partition budget, such that each piece fits SB.
        // ratio bounds tolerated cross-piece DUPLICATION; vn_limit caps running virtual-node count;
        //   perSplitLimit caps pieces per node.
        any_splittable |= ...;
    }
    return any_splittable;
}
```

**Each of the three PGA knobs has its own dedicated reject-string:**

- `ratio` — bounds duplication. Logged as `"elts/partition duplicated (<ratio> x)"`. A split that would replicate more than `ratio×` elements across pieces is rejected.
- `vn_limit` — caps the running virtual-node count. Reject-string `"; exceed vn limit = … v.s. limit=<vn_limit>"`.
- `perSplitLimit` — caps pieces per node. Reject-string `"Do not split the vn if this would result in any of the splits having to be placed in DRAM (due to perSplitLimit exceeded)"`.

These are exactly the three positional arguments of `runVNSplitterOnce(int vn_limit, float ratio, int perSplitLimit)`, and they are what makes the split factor tunable at all: tightening `ratio` or `perSplitLimit` reduces `N`, loosening them lets the splitter carve more pieces. In this build the tuning is done by whatever configures the pass — the PGA sweep over these three values is present but unreachable ([PGA](pga-feedback.md)).

### 2.3 The `ApGroup` accumulator and `APAccesses`

An `ApGroup` is the per-logical-sub-tensor accumulator — one group becomes one `_VN_` sub-tensor. The field map below is [INFERRED] from the offsets each accessor touches:

| Offset | Field | Meaning |
|---|---|---|
| `+0x00` | `bool statsValid` | `collectStats` memoisation flag (set 1 at end) |
| `+0x04` | `u32 highestElementInPartition` | max elements seen in one partition |
| `+0x08` | `u32 baseOffset` | min AP offset (= `AP[+0xD0]` of first AP) |
| `+0x18..+0x20` | `vector<PhysicalAP*> writeAPs` | writer access-patterns in this group |
| `+0x30..+0x38` | `vector<PhysicalAP*> readAPs` | reader access-patterns in this group |
| `+0x4c` | `u32 freeExtentBytes` | running max byte/element extent (via `pmaxsd`) |
| `+0x50` | `u32 partitionStride` | dtype partition stride (offset→partition-index divisor) |
| `+0x54` | `u32 rangeStat` | per-partition range stat (max of `getRange[0]`) |

`getDimensions()` (`@0xd4dde0`) returns the 2-entry shape `SmallVector<u32>{ group+0x4c, group+0x04 }` = `[freeExtentBytes, highestElementInPartition]`. `getBaseOffset()` (`@0xd4dcb0`) calls `collectStats()` then returns `this+0x08` (`mov 0x8(%rbx),%eax; ret`).

An `APAccesses` is the use-def grouping unit, one per `PhysicalAP`: it holds the `PhysicalAP*`, a `set<int> partitionIndices`, and an element range (the ctor `@0xd4be50` calls `getElementRange()`). The grouping predicate is `overlaps()` (`@0xd4a5a0`), defined as `!is_set_disjoint(a.partitions, b.partitions)` (`is_set_disjoint @0xd49120`) — **two APs overlap iff their accessed-partition sets intersect.** APs that never share a partition land in separate groups, so they become separate sub-tensors with non-conflicting live ranges. This is INV-2 (use-def locality).

### 2.4 `isValidForSB` — the SB-fit gate (`@0xd501e0`)

The gate packs the `SBModel` into a single 64-bit register: low 32 bits = `sbBytesPerPartition` budget, high 32 bits = `sbPartitionCount`. This is directly visible in the prologue — `mov %rsi,%rax; sar $0x20,%rsi` extracts the high half.

```c
// ApGroup::isValidForSB(SBModel sb, bool strict)  @0xd501e0
bool ApGroup::isValidForSB(SBModel sb /* esi: low32=byteBudget, high32=partCount */, bool strict) {
    u32 e = getHighestElementInPartition();              // this+0x04
    // walk write & read APs, count those with AP[+0x18]==1 (placed/real-AP kind),
    //   compute used bytesPerPartition.

    // PARTITION GATE  @d50532:
    //   if  partitionDim >= SBModel.partitionCount  ->  FAIL
    if (partitionDim >= sb.partitionCount /* 0x8(%rax) */) return false;   // cmp 0x8(%rax),%edx; jge -> fail
        // diagnostic: "ERR: channel count > … channels."

    // BYTE GATE:
    //   if  bytesPerPartition > SBModel.sbBytesPerPartition  ->  FAIL
    //   log "apGroup id <id> : isValidForSB = <bool>; exceed vn limit = <n> v.s. limit=<lim>"
    return (bytesPerPartition <= sb.sbBytesPerPartition);
}
```

The partition gate is plain in the disassembly: at `d50526` the group loads `mov 0x50(%rbx),%rax` (the SBModel record), and `d50536: cmp 0x8(%rax),%edx; jge` fails when the partition dim (`%edx` from `0x6c(%rsp)`) reaches the partition count at `0x8` of that record. This is the precondition that lets the colorer assume every node fits one SB tile (INV-1). The running per-AP byte sum is SIMD-inlined: the gate *semantics* — partition-count plus byte-budget — are unambiguous, but the exact per-AP summation form is [INFERRED].

### 2.5 `split` — sub-tensor creation (`@0xd57c80`)

For each valid `ApGroup`, `split` creates a new physical `MemoryLocation` named `<orig>_VN_<k>` (the literal `"_VN_"` is in `.rodata`), re-points every AP of the group at it, rebases offsets, and records the original for removal.

```c
// VNSplitter::split(ml, ratio, foldIntoPred, deadSet, keepMemLocs, keepInsts)  @0xd57c80
void VNSplitter::split(MemoryLocation *ml, float ratio, bool foldIntoPred,
                       set<variant<MemoryLocation*,Instruction*>> &deadSet,
                       DenseSet<MemoryLocation*> &keepMemLocs,
                       DenseSet<Instruction*> &keepInsts) {
    for (ApGroup &g : valid_groups) {     // or simple DRAM->SB case when groups.size()==1
        string newName = ml.name() + "_VN_" + to_string(k);
        MemoryLocation *vn = Function::addMemoryLocation(
            newName,
            partitionCount,            // from group / partition dim
            bytes,                     // group.bytesPerPartition * partitions
            ml.getDtype(),
            ml.getPartitionDim(),
            g.getDimensions(),         // tensorShape SmallVector<…,4>
            /*flags*/, /*optional*/, ml.getDebugInfo(),
            ml.getBankId(), ml.getAddress(), ml.getBasePartition(),
            ml.getMemoryType(),        // SB(16)/PSUM(32) per ml  (memory-type bit-flags)
            ml.getMemoryAddressSpace());

        for (PhysicalAP *ap : g.{writeAPs, readAPs}) {
            PhysicalAP *ap2 = _deepcopy(ap);                 // neuronxcc::_deepcopy clone
            // ap2 = cloneToOutput(inst) / cloneToArgument(inst) / PhysicalAP(inst) ctor
            ap2.setLocation(vn, b, b);                       // bind AP to the new sub-tensor
            int newOff = g.normalizeOffsetToNewNode(ap.offset);
            ap2.setOffset(newOff);                           // rebase into sub-tensor
            // patch Pattern dims via getPatternRef(); MemoryLocation::updateDimensions(jj,m)
        }
        deadSet.insert(variant(ml));                          // record original for removal
        // log "INFO (VNSPlitter): Moved <ml> from DRAM to SB…" /
        //     "WARNING: ML <name> has unused partitions! old: … ; new: …"
    }
}
```

The offset rebase is exact at `normalizeOffsetToNewNode` (`@0xd4dc80`): `sub 0x8(%rdi),%esi` then `idivl 0x50(%rdi)` — i.e. `(ap.offset − group.baseOffset[+0x08]) / group.partitionStride[+0x50]`. The group stats accumulate via `addAPtoStats` (`@0xd49200`): partition index `(ap.offset[+0xD0] − base) / stride`, range stat `group[+0x54] = max(·, getRange[0])` (via `pmaxsd`), free extent `group[+0x4c] = max(·, ap.basePtr[+8] + adj)`, and `group[+0x04] = max(·) + 1` (highest element index + 1).

`getBytesPerPartition` (`@0xd4dc40`) = `getHighestElementInPartition() × dtypeBytes[ml.Dtype]`, indexed into the dtype byte-size table at `0x1de6c40` (20×int64: u8=1, u16=2, fp32/i32=4, i64/fp64=8, …, `Dtype` field ≤ `0x13`, matching the BIR `AccessPattern` Dtype range).

### 2.6 Dead-node elimination and `VNSplitterDeadNodesCount`

A node is **dead** after a split when its every reader/writer AP has been re-pointed to the new `_VN_` sub-tensors, leaving it with no real use — that is the original over-sized `MemoryLocation` and any wholly-subsumed copy/memset instruction. `split` collects these into `deadSet` (`std::set<variant<MemoryLocation*, Instruction*>>`); the `DenseSet<MemoryLocation*>` / `DenseSet<Instruction*>` arguments are the **keep-alive** sets (referenced elsewhere, must survive). `runTransform`'s tail resolves each dead entry by name (`getMemoryLocationByName`) and erases it via `removeMemoryLocation(ml, /*recursive=*/true)`. The `VNSplitterDeadNodesCount` metric equals the count removed this run, which is the `"total dead nodes"` figure in the `SplitSB` log. This is INV-3 (clean graph).

---

## 3. Shrink-ML / `ShrinkDN` — the footprint shrink

Where the splitter carves nodes to *fit*, `shrink_ml` trims each node to its *true* size. A `MemoryLocation` was sized for its declared shape; `ShrinkDN` discovers the live window — the largest byte-range any access actually touches in a partition — and physically rewrites the node down to that window. The pass runs at order 25, *after* the splitter, so each `_VN_` piece is shrunk independently.

### 3.1 `ShrinkDN::run` — the driver (`@0x16c4db0`)

```c
// ShrinkDN::run(bir::Module&)  @0x16c4db0
void ShrinkDN::run(bir::Module &m) {
    // iterate every MemoryLocation: filter Storage -> MemoryLocationSet -> MemoryLocation
    for (MemoryLocation &ml : module_memory_locations(m)) {
        if (ml.hasReinterpretCast()) continue;   // reinterpret nodes left untouched
        analyze(ml);                             // compute live extents
        shrinkDN(ml);                            // rewrite extents
    }
}
```

### 3.2 `ShrinkDN::analyze` — the liveness probe (`@0x16c07a0`)

```c
// ShrinkDN::analyze(bir::MemoryLocation &ml)  @0x16c07a0
void ShrinkDN::analyze(bir::MemoryLocation &ml) {
    if (ml.isMx()) { /* special / skip */ }
    u32 maxUsed = 0;
    for (AccessPattern *ap : StorageBase::writers<AccessPattern>(ml)) {
        if (!AccessPattern::classof(ap)) continue;
        if (ap.isShiftedPartitionAccess()) bail();    // cannot shrink a shifted layout
        if (!isShrinkable(ap)) bail();
        u32 used = AccessPatternUtils::getElementRangeInPartition(ap);   // 108a130
                 // = getRange(ap.Pattern) * dtypeBytes[ml.Dtype]   (table @0x1df0e60, idx Dtype@+0x30 ≤0x13)
        maxUsed = max(maxUsed, used);
    }
    // record maxUsed as the shrink dimension/extent (consumed later via getShrinkDim)
    // assert shrink_dn.cpp:216
}
```

The metric `ShrinkDN` minimises is the **per-partition live byte-extent**: the largest range any access touches in a partition. `isShrinkable(ap)` (`@0x16bef10`) is `AccessPattern::isPartitionContiguous()` — only contiguous-partition APs may shrink; shifted/non-contiguous nodes bail and stay at their declared size. Where exactly `analyze` stores its result for `shrinkDN` to pick up is [INFERRED].

### 3.3 `ShrinkDN::shrinkDN` — the extent rewrite (`@0x16c38a0`)

```c
// ShrinkDN::shrinkDN(bir::MemoryLocation &ml)  @0x16c38a0
void ShrinkDN::shrinkDN(bir::MemoryLocation &ml) {
    u32 newExtent = analyze_result;                     // live elements-per-partition
    for (AccessPattern *ap : writers_and_matching_readers(ml)) {
        updateChannelStep(ap, newStep, partitionIdx);   // re-pack partition stride
        shrinkAP(ap);                                    // trim free-dim extents to newExtent
    }
    MemoryLocation::updateDimensions(jj, m);             // ×2: write reduced stride + free size back
    // propagate ml.getDebugInfo(); per-set rewrite via indirect callback fn(MemoryLocation*,…)
    // assert shrink_dn.cpp:444
}

// updateChannelStep(ap, newStep, idx)  @0x16bdd20  (exact)
void updateChannelStep(PhysicalAP &ap, u32 newStep, u32 idx) {
    ap.getPatternRef().Pattern[0].step = newStep;       // channel (partition) step
    ap.setOffset(newStep * idx + oldBase);              // re-pack partitions packed-contiguous
    // assert shrink_dn.cpp:292
}

// shrinkPAP(ap, n)  @0x16c2f00  (exact) — invoked by shrinkAP @0x16c3020
void shrinkPAP(PhysicalAP *ap, u32 n) {
    Pattern v = copy(ap.Pattern[0]);                    // keep the partition dim
    for (dim = 1; dim < numDims; ++dim)
        v.Pattern[dim] = canonical{step, num} from const@0x1dd1fe0, free-count = n;
    ap.setPattern(v);                                   // PhysicalAccessPattern::setPattern
    // -> the AP now addresses only `n` live elements
}
```

`updateChannelStep` re-packs the partition stride so partitions stay contiguous after the free dim shrinks (`setOffset = newStep * idx + oldBase`); `shrinkPAP` keeps the partition dim and rewrites the free dims to the canonical `{step, num}` with the free count set to the shrunk extent `n`. `shrinkAP` (`@0x16c3020`) computes the live count via `getElementRangeInPartition + getMemoryLocationSet`, guarded by `isPartitionContiguous` (×5), then calls `shrinkPAP(ap, liveCount)`. Together these write the reduced extents back through `MemoryLocation::updateDimensions`, so the tensor occupies fewer bytes per partition. This is INV-4 (minimal footprint).

### 3.4 `shrinkTensor` — the set-level analyzer

`shrinkTensor(bir::Function&)` (`@0x1826e00`) is the *logical* counterpart that operates at the `MemoryLocationSet` level rather than on physical extents. For each `MemoryLocationSet`, it inspects `SymbolicAccessPattern::getBlockAddrs()` and `Instruction::getLoopnest()` over the set's APs (a `hashtable<u64, Instruction*>` of block→writer), derives the shrink dimension and the count of *live* blocks, then records:

```c
MemoryLocationSet::setShrinkDim(dim);   // producer
MemoryLocationSet::setLiveN(n);         // live block count
```

`shrinkTensor(bir::Module&)` (`@0x1829e90`) just loops functions. The cross-pass channel is `getShrinkDim()` — only a thunk lives in libwalrus (`0x5fb650` for `MemoryLocation`, `0x5efe20` for `MemoryLocationSet`); the real body and the shrink-dim *selection heuristic* live in libBIR and are **not traced here** [GAP]. So `shrinkTensor` is the analysis that annotates each set with `(shrinkDim, liveN)`, and `ShrinkDN` is the transform that reads `getShrinkDim()` and physically rewrites the per-`MemoryLocation` APs. The producer/consumer split (`setShrinkDim` versus `getShrinkDim`) is what ties them together.

> **QUIRK — `VncLink` is not part of this chain.** `VncLink` (`run(Module&) @0x16477c0`, `run(vector<unique_ptr<Module>>&) @0x1648de0`) is the per-Neuron-Core *link* pass that joins per-LNC module shards. It shares no callgraph edge with `VNSplitter`/`ShrinkDN`; the only overlap is the `"VN"` prefix, which here means *virtual-neuron-core*, not *virtual-node*. A reimplementer must not conflate the two.

---

## 4. Allocator-prep role — invariants handed to the colorer

The coloring allocator ([`allocator-drivers`](allocator-drivers.md)) and `SBSizeLegalization` color `MemoryLocation`s onto a fixed SB grid of `sbPartitionCount × sbBytesPerPartition`. The two prep passes establish the preconditions that make coloring tractable:

| Invariant | Owner | Guarantee | Confidence |
|---|---|---|---|
| **INV-1 SB-fittability** | VN-Splitter | every SB/PSUM node, post-split, has `partitionDim < sbPartitionCount` **and** `bytesPerPartition ≤ sbBytesPerPartition` (`isValidForSB`). An over-sized node is decomposed into disjoint-partition `_VN_` pieces that each fit. | CERTAIN |
| **INV-2 Use-def locality** | VN-Splitter | sub-tensors carved along `ApGroup` boundaries (overlapping-partition-set clusters). Disjoint access clusters become independent nodes whose live ranges no longer conflict and can share colors; duplication bounded by `ratio`. | CERTAIN |
| **INV-3 Clean graph** | VN-Splitter | the original over-sized node and now-redundant copy/memset instructions are removed (`deadSet → removeMemoryLocation`); `VNSplitterDeadNodesCount` records the cleanup. | CERTAIN |
| **INV-4 Minimal footprint** | Shrink-ML | each node's physical extent reduced to the live element-range-in-partition, with partition strides re-packed (`updateChannelStep`) and free-dim counts trimmed (`shrinkPAP`). Reinterpret-cast / shifted-partition nodes left intact. | CERTAIN |

**Ordering rationale.** `vn_splitter`(24) runs before `shrink_ml`(25): the splitter carves each over-sized node so each piece is independently shrinkable; `shrink_ml` then trims each piece to its live extent; then the disjoint, minimal, SB-fitting node set is handed to the colorer. *Within* `vn_splitter` the order is split-then-fold: the virtual-node split runs first, then `VerticalFusion` folds the producer→consumer chains over the splitter's output. [INFERRED] The precise rationale for folding *after* splitting is not stated in the binary.

**The PGA sweep would close over this — if anything ran it.** `runVNSplitterOnce` re-runs the whole split+fold body with a different `(vn_limit, ratio, perSplitLimit)` per grid cell, which is what a search over the split factor `N` against downstream cost would look like: tighter knobs give fewer, larger pieces, looser ones give more, smaller pieces. In this build the sweep has no caller and no selection step ([PGA](pga-feedback.md) §5), so `vn_splitter` is a **fixed transform under its configured knobs**, not a self-adapting one. The split↔fold duality is real; the adaptation is not present.

---

## 5. Limits of this reading

Read straight off the disassembly: the split-then-fold dispatch (`VNSplitter::runTransform` at `d73d6a`, then `VerticalFusion::runTransform` at `d74162`); the `isValidForSB` partition gate (`cmp 0x8(%rax),%edx; jge`); the packed-`SBModel` register split (`sar $0x20,%rsi`); `normalizeOffsetToNewNode` as `(off − base)/stride` (`sub`/`idivl`); `getBaseOffset` returning `this+0x08`; the two option-byte reads at `+0x150`/`+0x151`; the `perSplitLimit` reject-string; the `_VN_`, `SplitSB`, `is splittable` and `VNSplitterDeadNodesCount` literals; and the `ShrinkDN::run` symbol and namespace.

Softer, pinned by strings or signatures but with SIMD-inlined or partly-reconstructed arithmetic: the `(int, float, int)` PGA signature and its per-knob role mapping; the `isValidForSB` running byte-sum; the `ApGroup` field map at `+0x4c`/`+0x54`; the `shrinkTensor` ↔ `ShrinkDN` producer/consumer linkage.

Two heuristics are named and positioned but **not reconstructed**, because their bodies are not in `libwalrus.so`: the vertical-fold condition inside `VerticalFusion::runTransform`, and the shrink-dim selection behind `getShrinkDim` (libwalrus carries only thunks; the real body is in libBIR). The `runVNSplitterOnce` body and the autotuner's search policy are likewise out of scope here. A reimplementer would need those bodies to fill in the decision logic; everything structural on this page — which functions run, what they gate, and the invariants they establish — holds without them.
