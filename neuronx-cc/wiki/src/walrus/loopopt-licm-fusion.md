# LoopOptimization — LICM and Greedy Profit-Based Fusion

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 verified identical in structure). The pass lives in `neuronxcc::backend::LoopOptimization`, compiled into `neuronxcc/starfish/lib/libwalrus.so`. That object retains its full C++ dynamic symbol table (`nm -DC`) and its `.text` is mapped at VA == file offset, so every body cited here is a demangled symbol at a hard address, disassembled with `objdump -d --start-address=0xVA`. The IDA `decompiled/…licm…_0x5ef5a0.c` sidecars are PLT-thunk stubs for the high-VA bodies and were **not** used; every claim is grounded in `objdump` of the real bodies at the `nm -D` addresses.*

## Abstract

`LoopOptimization` is the walrus-backend loop pass that runs after `full_unroll` and before `sb_size_legalization`. Two transforms dominate it: **loop-invariant code motion** (LICM, `licm` @`0xb90a70`) and **greedy profit-based loop fusion** (`checkLoopFusionGreedy` @`0xba9210`). This page documents both, plus the profit model that drives the fusion order.

The fusion engine is a textbook **greedy max-heap** scheduler with a quantified profit metric. It builds a *Fusion Profit Graph* — a `std::vector<std::pair<std::pair<InstLoop*,InstLoop*>,float>>` heaped by profit — then repeatedly pops the highest-profit fusable pair, runs a legality battery, fuses if legal, and re-scores the affected neighbourhood. The profit of fusing two loops is **`2 · Σ_sharedTensors (liveN · elemSize)`**: twice the byte traffic of every tensor that *both* loops touch. The factor 2 is read-plus-write traffic — fusing two loops over a shared intermediate lets that data stay resident instead of round-tripping to memory once on the producer's write and once on the consumer's read. That formula is the headline of this page, and it is confirmed at the instruction level (`getLiveN` → `imul` → `addss %xmm0,%xmm0`).

LICM is the simpler half: an instruction is loop-invariant iff none of its `SymbolicAccessPattern` operands is produced by a definition located inside the loop body. The test is a set-membership query against a `DenseSet` of in-loop-def storages; a passing instruction is **spliced** out of the loop body into the loop's preheader. The analogy to LLVM LICM holds at the level of intent (hoist invariants to the preheader) but diverges in representation: there is no `LoopInfo`/`MemorySSA`; invariance is decided over BIR `SymbolicAccessPattern` operands and the def-set is a hand-rolled `DenseSet` keyed by storage pointer.

For reimplementation, the contract is:

- **The LICM invariance test** — operand-kind discrimination (kind `== 2`), the per-loop in-loop-def `DenseSet`, the set-membership decision, and the `splice`-to-preheader hoist.
- **The greedy fusion driver** — Fusion Profit Graph construction, the `pop_heap`/`__adjust_heap` max-heap mechanism, stale-pair validation, the legality battery, and `updateFPG` re-scoring.
- **The `2 · Σ liveN · size` profit metric** — the shared-tensor enumeration, the `type == 1` placed-tensor filter, the `liveN × elemSize` product, and *why* the factor is 2.

| | |
|---|---|
| **Pass class** | `neuronxcc::backend::LoopOptimization` |
| **Object** | `neuronxcc/starfish/lib/libwalrus.so` (`.text` VA == file offset) |
| **Pass driver** | `run(Module&)` @`0xb9bb30` |
| **LICM** | `licm(Module&)` @`0xb90a70` (size `0x14f0`) |
| **Greedy fusion driver** | `checkLoopFusionGreedy(BasicBlock&, int)` @`0xba9210` (size `0x26f0`) |
| **Profit graph build** | `constructFusionProfitGraph(BasicBlock&, vector<InstLoop*>&)` @`0xba1080` (size `0x600`) |
| **Heap pop-best** | `findFusionPairGreedy(…)` @`0xba1ad0` (size `0x490`) |
| **Re-score** | `updateFPG(BasicBlock&)` @`0xba1f60` (size `0x8c0`) |
| **Apply fuse** | `loop_fusion(InstLoop*, InstLoop*)` @`0xb92920` (size `0x440`) |
| **Dependence legality** | `has_negative_distance(InstLoop*, InstLoop*)` @`0xb8f100` (size `0x510`) |
| **`InstLoop` opcode** | `105` (`0x69`) — `cmpl $0x69, 0x50(%reg)` |
| **Source paths (`.rodata`)** | `…/loop_optimization/src/loop_optimization.cpp`, `…/loop_profitable_fusion.cpp` |

---

## Pass Driver — `run(Module&)`

### Purpose

`run` @`0xb9bb30` resolves the two memory budgets into the pass object, recomputes liveness, then drives fusion per-basic-block and LICM module-wide. Its phase order is fixed; the call sequence below is read off the body at `0xb9bbc0..0xb9c094`.

### Algorithm

```c
function run(Module &m):                         // 0xb9bb30
    // 1. Resolve budgets (cl::opt, value-cell at +0x78 of each global)
    sb = cl_opt(SBSize @0x3dc8b00 + 0x78);       // 0xb9bbd5
    this[0x104] = (sb == 0) ? target_sb_per_partition(m) : sb;   // 0 => target default
    psum = cl_opt(PSUMSize @0x3dc2d30 + 0x78);   // 0xb9bbf7
    this[0x108] = (psum == 0) ? target_psum_default() : psum;

    printParams();                               // 0xb9bc1?  (emits the four knobs)
    mmpk_lower_symbolic(m);                       // 0xb9bc29  matmul-pack symbolic lowering
    update_liveN(m);                              // 0xb9bc39  recompute MemoryLocation liveN

    // 2. Fusion per function, per basic block
    for fn in m.functions:                        // r14 = fn list
        for bb in fn.basic_blocks:                // r15 = bb list
            if *(bool*)(enableProfitableLoopFusion + 0x78):   // gate @0xb9bd36
                checkLoopFusionGreedy(bb, /*level=*/0);        // 0xb9bd18  DEFAULT
            else:
                check_loop_fusion(bb, level);                 // 0xb9bd3e  classic fallback

    // 3. LICM, module-wide, AFTER fusion
    licm(m);                                       // 0xb9c094

    // 4. Distribution / interchange / tiling reachable via the long if-chain
    //    (their check_* helpers are also invoked from inside the greedy driver)

    log("numLoopsFused = ", this[0x100]);          // 0xb9bf06  final total
```

> **NOTE —** `enableProfitableLoopFusion` defaults to **true**, so the *greedy* path is the default and the classic `check_loop_fusion` @`0xb95a70` is the fallback taken only when the flag is cleared. The classic path is not documented here beyond its existence.

> **QUIRK —** LICM runs *after* fusion, not before. Fusion changes which definitions sit inside which loop body (it merges two bodies onto one `LoopAxis`), so running LICM afterward lets it hoist invariants that only became invariant once the loops merged. The budgets `SBSize`/`PSUMSize` (`this+0x104`/`+0x108`) are resolved here but do **not** enter the fusion profit float — they act as later legality caps (`sb_size_legalization` runs after).

### Pipeline position

```text
full_unroll  →  loop_optimization  →  sb_size_legalization  →
  coloring_allocator_with_loop  →  (pre/post schedulers in libwalrus)
```

Cross-confirmed against the walrus pass topology. `full_unroll` runs first and uses a distinct mechanism (`InstLoop+0x158` unroll flag + full clone); `loop_optimization` uses `replaceLoopAxis`/`splice`, so the two do not overlap.

---

## LICM — `licm(Module&)`

### Purpose

`licm` @`0xb90a70` hoists loop-invariant compute out of loop bodies into the enclosing block (the loop's preheader / insertion point). The invariance decision is operand-driven: an instruction is invariant iff none of its access-pattern operands is produced by a def inside the loop.

### Entry Point

```text
licm(Module&)  @0xb90a70  (size 0x14f0)
  └─ collectInstructions(Module&)             @0xb90a94  ── flat instruction work-list
  └─ per-inst opcode filter (+0x58 loopnest discriminator)
  └─ per-arg invariance test (getArgument, kind==2, in-loop-def DenseSet)
  └─ NamedObjectContainer::splice(...)        @0xb91d00  ── hoist to preheader
  └─ backend::update_all_loopnest(Instruction*)  @0xb91d2a  ── refresh loopnest
```

### Algorithm

```c
function licm(Module &m):                        // 0xb90a70
    work = collectInstructions(m);               // 0xb90a94  flat list [0x70..0x78](rsp)

    for inst in work:                            // 0xb90ab8..b90b0c
        op = *(int*)(inst + 0x58);               // NB: +0x58 = loopnest-context discriminator,
                                                 //     distinct from the +0x50 BIR opcode
        if op == 0x1b: continue;                 // skip
        if op == 0x3 or op == 0x16: continue;    // boundary markers, advance
        if op > 0x69: not a hoist candidate;     // 105 = InstLoop nest
        if op in (0x2a .. 0x69]:
            // bitmask 0x4000000000000009 >> (op - 0x2b): set bits => skip/recurse
            //   opcodes 0x2b(43), 0x2e(46), 0x69(105) are recursed/skipped
            if mask_bit_set(op): recurse_or_skip;

        // ---- LOOP-INVARIANCE TEST (the core) ----
        in_loop_defs = DenseSet();               // storages defined INSIDE this loop
        // (built by walking the loop body; ptr-hash table, see below)

        invariant = true;
        for i in 0 .. inst.numArguments:
            arg = inst.getArgument(i);           // 0xb90b95
            if *(int*)(arg + 0x18) != 2:         // 0xb90b9f  operand-kind tag
                continue;                        //   kind != 2 => not an AP operand; ignore
            ap = inst.getArgument<SymbolicAccessPattern>(i);   // 0xb90bb6
            for def in [ ap[0xd8] .. ap[0xe0] ]:  // 0xb90bbb  AP def-list begin/end
                if in_loop_defs.contains(def.storage):
                    invariant = false;           // operand produced inside the loop
                    break;

        if invariant:
            // HOIST: splice inst from loop body into the loop's insertion point
            NamedObjectContainer<BasicBlock,Instruction>::splice(
                /*dest_iter=*/ loopHeader + 0x8,
                /*srcContainer=*/ inst's body container,
                /*src_iter=*/ inst + 0x50);                   // 0xb91d00
            backend::update_all_loopnest(inst);               // 0xb91d2a
            log("[LICM] Moving inst ", inst.getName(),
                " to a new insertion point\n");               // 0xb91c99..b91ce9
```

The in-loop-def set is a hand-rolled LLVM-style `DenseSet`: the inner block `0xb90c10..0xb90d40` computes the bucket index from the storage pointer (`shr 9` / `shr 4` / `xor` / `and(mask)`), rounds the table up to a power of two (`0xb90cc8`), and grows via `allocate_buffer` (`0xb90d22`). The sentinel keys `0xffffffffffffe000` (empty) and `0xfffffffffffff000` (tombstone) are skipped at `0xb90c7c`/`0xb90c88`.

### The invariance test, stated precisely

> **An instruction is loop-invariant iff, for every operand of kind `== 2` (a `SymbolicAccessPattern`), none of the storages in that AP's def-list is a member of the set of storages defined inside the loop body.** (`[S]`, built on the `[C]`-confirmed `kind==2` filter at `0xb90b9f`, the AP def-list walk at `0xb90bbb`, and the `DenseSet` membership at `0xb90c10..b90d40`.)

> **GOTCHA —** the side-effect / hazard gating is *implicit*, not an explicit predicate. Only operand-kind `== 2` (pure AP-typed) operands are tested; instructions whose operands carry other kinds, and all loop-structure opcodes, are excluded earlier by the opcode filter (`+0x58`). A reimplementation that tests *all* operands the same way will hoist instructions the binary leaves in place. The kind discriminator is the gate. `[I]`

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `licm` | `0xb90a70` | LICM driver | CERTAIN |
| `collectInstructions` | `0xb90a94` (callee) | Flat instruction work-list | CERTAIN |
| `Instruction::getArgument(j)` | `0x6119c0` (plt) | Operand fetch | CERTAIN |
| `Instruction::getArgument<SymbolicAccessPattern>(j)` | `0x5fa2e0` (plt) | Typed AP operand fetch | CERTAIN |
| `NamedObjectContainer::splice` | `0xb91d00` (call) | Hoist to preheader | CERTAIN |
| `backend::update_all_loopnest` | `0xb91d2a` (call) | Refresh loopnest membership | CERTAIN |

---

## Greedy Fusion — `checkLoopFusionGreedy(BasicBlock&, int level)`

### Purpose

`checkLoopFusionGreedy` @`0xba9210` fuses adjacent loops in a basic block in profit order. It builds the Fusion Profit Graph, then loops: pop the highest-profit pair off the heap, test legality, fuse if legal, re-score, repeat until the heap is empty. It recurses by nesting level (outer → inner).

### Entry Point

```text
checkLoopFusionGreedy(bb, level)  @0xba9210  (size 0x26f0)
  └─ constructFusionProfitGraph(bb, &loopVec)   @0xba9286  ── build FPG (see §Profit Model)
  └─ while findFusionPairGreedy(bb, FPG, &pair)  @loop      ── pop max-heap
       └─ traversePath / collect_illegal_common_ap / check_loop_{interchange,distribution}
       └─ has_negative_distance(l1,l2)            @0xbaab07  ── dependence legality
       └─ loop_fusion(l1,l2)                      @0xbaa854 / @0xbab542
       └─ ++this->numLoopsFused                   (addl $1, 0x100(%rbx))
       └─ updateFPG(bb)                           @0xbaa87c / @0xbab569  ── re-score
```

### Algorithm

```c
function checkLoopFusionGreedy(BasicBlock &bb, int level):    // 0xba9210
    if level > bb.maxNestLevel():                // 0xba9238  cmp level vs 0x190(bb)
        return;                                  // recursion floor

    loopVec = [];                                // InstLoops at this level
    constructFusionProfitGraph(bb, &loopVec);    // 0xba9286 -> FPG heap on the pass object

    pair<InstLoop*,InstLoop*> out;
    while findFusionPairGreedy(bb, FPG, &out):    // pop max-profit pair; false => heap empty
        l1 = out.first; l2 = out.second;
        log("[LOOP FUSION] , level = ... shrink factor ...");   // 0x1c76c0b

        // ---- LEGALITY BATTERY (any failure => skip this pair) ----
        traversePath(l1, l2, &insts);             // 0xba9c4f  instrs between the two loops
        collect_illegal_common_ap(l1, l2);        // 0xba9ee7  shared APs that block fusion
        check_loop_interchange(l1, l2, ...);      // 0xba9f61
        check_loop_distribution(l1, l2, ...);     // 0xbaa073

        if *(0xa8(l1)) != *(0xa8(l2)):            // 0xbaa312 trip/axis mismatch
            log("[STOP FUSION] unmatched trip count"); continue;     // 0x1ce6138
        if !accesses_partition_dim(l1, l2):
            log("[STOP FUSION] loop ... doesn't access partition dim"); continue;  // 0x1ce61b8
        if no_benefit:
            log("[STOP FUSION] no fusion benefit"); continue;        // 0x1ce6110
        if has_negative_distance(l1, l2):         // 0xbaab07 (see §Dependence Legality)
            log("[STOP FUSION] negative distance dep in def-use"); continue;  // 0x1ce6188

        // ---- APPLY ----
        loop_fusion(l1, l2);                      // 0xbaa854 OR 0xbab542 (two paths)
        ++this->numLoopsFused;                    // addl $1, 0x100
        updateFPG(bb);                            // 0xbaa87c OR 0xbab569  full rebuild
```

> **NOTE —** there are two `loop_fusion`+`updateFPG` call sites. The two paths are the interchange-then-fuse path (after `check_loop_interchange` may reorder the nest before fusing) and the direct-fuse path. `[I]`

### Termination

`findFusionPairGreedy` returns `false` (`al == 0`) when the heap is empty (`begin == end` @`0xba1b03`); the `while` exits (`je` @`0xba96b9`). Because each successful fuse destroys one loop and the heap is fully rebuilt by `updateFPG`, the loop strictly reduces the loop count and terminates.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `checkLoopFusionGreedy` | `0xba9210` | Greedy driver | CERTAIN |
| `findFusionPairGreedy` | `0xba1ad0` | Pop max-profit pair | CERTAIN |
| `constructFusionProfitGraph` | `0xba1080` | Build FPG / profit | CERTAIN |
| `updateFPG` | `0xba1f60` | Re-score (full rebuild) | CERTAIN |
| `loop_fusion` | `0xb92920` | Apply the fuse | CERTAIN |
| `has_negative_distance` | `0xb8f100` | Dependence legality | CERTAIN |
| `collect_illegal_common_ap` | `0xb8ea70` | Shared blocking APs | CERTAIN |
| `traversePath` | (callee) | Instrs between loops | HIGH |
| `{anon}::getLoopPairDistance` | inlined | Same-level index distance | HIGH |

---

## Profit Model — `constructFusionProfitGraph`

### Purpose

`constructFusionProfitGraph` @`0xba1080` collects every `InstLoop` at the basic block's current level, forms candidate pairs within a bounded fan-out window, scores each pair by shared-tensor byte traffic, and stores the result as a profit-keyed max-heap on the pass object.

### The FPG layout

The Fusion Profit Graph is a `std::vector<std::pair<std::pair<InstLoop*,InstLoop*>,float>>`. Each entry is **24 bytes** (`2 × 8` pointers + `4` float + `4` pad). The stride is proven two ways: the `/24` reciprocal magic `0xaaaaaaaaaaaaaaab` in `findFusionPairGreedy`, and the `× 24` allocation in the constructor (`lea (rbx,rbx,2); shl 3`). The `float` payload sits at offset `+0x10` of each entry — that is the value `findFusionPairGreedy` reads as the heap key.

> **CORRECTION —** "FPG" is **Fusion Profit Graph** (symbol `constructFusionProfitGraph`), not "Fusion-Pair Graph" as an earlier narration had it. The graph is keyed by the float profit weight, and the heap comparator is `LoopOptimization::CompFusionPriority`. Both are confirmed by the demangled `__adjust_heap<…, _Iter_comp_iter<CompFusionPriority>>` symbol whose element type is `pair<pair<InstLoop*,InstLoop*>,float>`.

### Algorithm

```c
function constructFusionProfitGraph(BasicBlock &bb, vector<InstLoop*> &loopVec):  // 0xba1080
    // 1. Collect all InstLoop(opcode 0x69) at this level
    for inst in bb:
        if *(int*)(inst + 0x50) == 0x69:          // 0xba10ec  InstLoop discriminator
            loopVec.push_back(cast<InstLoop>(inst));

    FPG = new pair<pair<InstLoop*,InstLoop*>,float>[ 24 * loopVec.size() ];   // 0xba1?? ×24

    // 2. For each ordered pair within the distance window
    for i in 0 .. N:
        l1 = loopVec[i];
        for j in i+1 .. N:
            l2 = loopVec[j];
            if *(0xa8(l1)) != *(0xa8(l2)):        // 0xba128f  same enclosing list required
                goto illegal_common_ap_path;

            // index distance between the two loops in their parent block
            d = abs( posOf(l2) - posOf(l1) );     // 0xba12ae..b12f9  (def/use ordinals)
            if d >= distanceThreshold:            // 0xba1302  default 8 -> jae, SKIP pair
                continue;                         //   no FPG entry beyond the window

            common = collect_illegal_common_ap(l1, l2);   // 0xba1360 shared MemoryLocations

            // ---- PROFIT WEIGHT (the headline) ----
            profit = 0.0f;
            for ap in common:
                mloc = ap.memoryLocation;
                if *(int*)(mloc + 0x110) != 1:    // 0xba13bf  type==1 => placed/internal tensor
                    continue;
                ml    = *(mloc + 0x188) - 0x118;  // 0xba13cc  MemoryLocation base
                n     = ml.getLiveN();            // 0xba13e4  live element count
                s     = *(int*)(*(ml + 0x108));   // 0xba13e9  element size / stride
                bytes = n * s;                    // 0xba13f2  imul
                profit += 2.0f * (float)bytes;    // 0xba140b cvtsi2ss; 0xba1410 addss xmm0,xmm0

            // ---- profit == 2 * Σ_sharedTensors (liveN * elemSize) ----
            FPG.push_back( { {l1, l2}, profit } );    // 0xba147d _M_realloc_insert
```

### The metric, verified

The profit is **CONFIRMED at the instruction level** in the window `0xba13a0..0xba1430`:

```asm
ba13bf:  cmpl   $0x1,0x110(%rax)                 ; MemoryLocation::type == 1 (placed tensor)
ba13e4:  call   getLiveN@plt                     ; n = ml->getLiveN()
ba13e9:  mov    0x108(%r13),%rdx                 ; &elemSize
ba13f2:  imul   (%rdx),%rax                      ; bytes = n * elemSize
ba140b:  cvtsi2ss %rdx,%xmm0                      ; (float)bytes
ba1410:  addss  %xmm0,%xmm0                       ; *= 2   <-- the factor 2
```

> **The factor 2 is read-plus-write traffic.** A tensor that both loops touch is, before fusion, written to memory at the end of the producer loop and read back at the start of the consumer loop — two passes over `liveN × elemSize` bytes of SBUF/DRAM traffic. Fusing the loops lets that intermediate stay resident across the merged body, eliminating *both* the write and the matching read. The savings are therefore `2 × bytes` per shared tensor, summed over all shared placed tensors. This is why `profit = 2 · Σ (liveN · elemSize)` and not `1 ×`. The `addss %xmm0,%xmm0` (self-add = double) is the binary's encoding of that `× 2`. `[C]` for the arithmetic; `[S]` for the read+write rationale.

> **GOTCHA —** only `MemoryLocation::type == 1` (placed / internal tensors) contributes to the profit (`cmpl $0x1,0x110` @`0xba13bf`). Tensors of other types are skipped — fusing does not change their memory residency in a way the model rewards. A reimplementation that sums over *all* shared APs will over-estimate profit and fuse in the wrong order. `[C]`

> **NOTE —** there is no budget term in the profit float. `SBSize`/`PSUMSize` are *not* part of the weight; they bound fusion only later, through `sb_size_legalization` and the `this+0x104`/`+0x108` caps. The profit is purely a traffic-reuse estimate. `[I]`

---

## Priority Queue — `findFusionPairGreedy`

### Purpose

`findFusionPairGreedy` @`0xba1ad0` pops the maximum-profit pair off the FPG, validates that the pair is still fusable (loops may have been consumed by an earlier fuse), and returns it through the out-parameter. It is `std::pop_heap` specialized for `pair<pair<InstLoop*,InstLoop*>,float>` under `CompFusionPriority`.

### Algorithm

```c
function findFusionPairGreedy(BasicBlock &bb, vector<FPGEntry> &fpg,
                              pair<InstLoop*,InstLoop*> &out) -> bool:   // 0xba1ad0
    while fpg.begin != fpg.end:                  // 0xba1b03  empty => return false
        front_profit = *(float*)(fpg.front() + 0x10);    // 0xba1b0c  heap top = max profit
        l1 = fpg.front().first;
        l2 = fpg.front().second;

        // pop_heap: move back to front, shrink by one 24-byte entry, sift down
        if fpg.size() > 24:                       // 0xba1e72  cmp $0x18 (one entry)
            move_back_to_front();
            fpg.end -= 24;                         // 0xba1b38  sub $0x18
            __adjust_heap<…, _Iter_comp_iter<CompFusionPriority>>(...);   // 0xba1ec6 sift-down

        // VALIDATE the popped pair is STILL fusable
        if *(int*)(l1 + 0x50) != 0x69: continue;  // 0xba1b76  l1 still an InstLoop?
        if *(int*)(l2 + 0x50) != 0x69: continue;  // 0xba1bb6  l2 still an InstLoop?
        if l1 == null or l2 == null: continue;
        if !still_adjacent(l1, l2): continue;     // 0xba1b81/ba1bc7  -0x8 / 0x58 offset checks

        out = { l1, l2 };
        return true;
    return false;                                 // heap drained -> termination signal
```

The heap key read at `0xba1b0c` (`movss 0x10(%rdi),%xmm1`) and the sift-down via `__adjust_heap<…CompFusionPriority>` at `0xba1ec6` are the two CONFIRMED anchors that pin the data structure as a binary max-heap over the float profit. The `cmpl $0x69, 0x50` checks at `0xba1b76`/`0xba1bb6` are the CONFIRMED stale-pair guards.

> **GOTCHA —** stale pairs are the central subtlety of the greedy loop. When loop `L` is fused away, *every* FPG entry that referenced `L` is now invalid, but the heap still holds them until `updateFPG` rebuilds. `findFusionPairGreedy` therefore re-validates each popped pair (both endpoints still opcode `0x69`, both non-null, still adjacent) and discards stale entries, trying the next heap top. A reimplementation that trusts the popped pair without re-validating will dereference a freed `InstLoop`. The assert `"doesLoopExist(bb,l1) && doesLoopExist(bb,l2)"` (string `0x1ce73f0`) guards this in debug builds. `[C]`

---

## Dependence Legality — `has_negative_distance`

### Purpose

`has_negative_distance` @`0xb8f100` is the final fusion-legality gate. Fusing two loops merges their iteration spaces; if loop `l2` writes a tensor that loop `l1` reads at a non-positive iteration distance, fusing them would reorder a true dependence. This function detects exactly that case.

### Algorithm

```c
function has_negative_distance(InstLoop *l1, InstLoop *l2) -> bool:   // 0xb8f100
    uses = collect_use_in_loop(l1);              // 0xb8f13a  vector<SymbolicAccessPattern*>
    defs = collect_def_in_loop(l2);              // 0xb8f159

    for use in uses:
        for def in defs:
            ml_use = *(use + 0x188) - 0x118;
            ml_def = *(def + 0x188) - 0x118;
            if ml_use != ml_def: continue;        // 0xb8f218  different tensor -> skip
            d = bir::getDefUseDistance(use, def, axis);   // 0xb8f35a  signed int
            if d <= 0:                             // 0xb8f388  test r14d; jle
                log(" : use -> def : (iteration groups:");   // 0x1c76b12
                return true;                       // backward/loop-carried dependence
    return false;
```

> **Semantics:** fusing `l1`, `l2` is **illegal** if any tensor written in `l2` is read in `l1` at a non-positive iteration distance (`d <= 0`) — that would reorder a true dependence. The distance primitive `bir::getDefUseDistance` lives in `libBIR.so`; the same-parent-level requirement is asserted (`"loop distance is ill defined if not at the same level."` @`0x1ce72d8`). The full polyhedral distance model is documented separately — see Cross-References. `[S]` for the legality semantics, `[C]` for the `d <= 0` branch at `0xb8f388`.

---

## The Fuse — `loop_fusion`

`loop_fusion` @`0xb92920` performs the structural transform once a pair clears legality:

```c
function loop_fusion(InstLoop *l1, InstLoop *l2):    // 0xb92920
    // 1. Unify l2's body onto l1's induction variable
    for inst in l2.body:
        inst.replaceLoopAxis(l2.axis, l1.axis);      // rewrite every loop-axis ref to l1's

    // 2. Move l2's body (plus an InstGenericCopy) into l1's body
    NamedObjectContainer<BasicBlock,Instruction>::insert/splice(l1.body, copy + l2.body);
    backend::update_all_loopnest(...);               // refresh loopnest membership

    // 3. Destroy l2
    BasicBlock::removeInstruction(l2.header);
    BasicBlockHolder::removeBasicBlock(l2.body_block);   // ×2: header block + body block
```

Net effect: `l2`'s body is moved under `l1`'s `LoopAxis`, `l2` is destroyed, and the result is one fused loop. `numLoopsFused` (`this+0x100`) is bumped by the *greedy driver*, not inside `loop_fusion`.

`updateFPG(bb)` @`0xba1f60` then re-invokes `constructFusionProfitGraph` to rebuild the FPG for the changed neighbourhood. It logs `"(LoopOpt::updateFPG) e -> : weight = "` / `" and loop , weight = , distance ="` (strings `0x1c76e2a`/`0x1c76e4f`).

> **QUIRK —** the "re-score affected neighbours" step is implemented as a **full rebuild**, not an incremental heap patch. After each fuse, `updateFPG` discards the FPG and reconstructs it from scratch over the current loop set. This is `O(N²)` per fuse in the worst case (every loop pair within the window re-scored), but `distanceThreshold` (default 8) bounds the fan-out so `N` is the loop count at one level, not the whole function. A reimplementation may keep an incremental heap, but must then handle the stale-pair invalidation that the full rebuild sidesteps. `[C]`

---

## Related Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `enable-profitable-loop-fusion` | `bool` | **true** | Selects the greedy path (`checkLoopFusionGreedy`); cleared → classic `check_loop_fusion`. Global @`0x3dc7078`; desc `"enable profit based loop fusion strategy"`. |
| `loop-opt-distance-threshold` | `uint` | **8** | Candidate-pair fan-out window and abs-index-distance cap in `constructFusionProfitGraph`. Global @`0x3dff780`; `movl $0x8` @`0x7cf85d`. |
| `enable-loop-distribution` | `bool` | **true** | Enables the distribution transform. Global @`0x3dc6180`; `movb $1` @`0x7cf703`. |
| `loop_opt_sb_size` | `uint` | **0** | SB size per partition; `0` → derive from target. Global @`0x3dc8b00`; `this+0x104`. |
| `loop_opt_psum_size` | `uint` | **0** | PSUM size per partition; `0` → target default. Global @`0x3dc2d30`; `this+0x108`. |

`printParams()` emits all five (`"distanceThreshold = … SBSize = … PSUMSize = … enableLoopDistribution = … enableProfitableLoopFusion = …"`, string @`0x1c76dc7`). The checkpoint dump `__loop_opt_chkpt.txt` is written by `loop_opt_doctor.cpp`.

---

## Stop-Fusion Reason Table

The greedy driver logs an exact reason on every rejected pair. The strings (verbatim `.rodata`):

| String | Cause |
|---|---|
| `[STOP FUSION] unmatched trip count` | `0xa8(l1) != 0xa8(l2)` — axis/trip-count structures differ (`0xbaa312`) |
| `[STOP FUSION] no fusion benefit` | profit is zero / non-beneficial (`0x1ce6110`) |
| `[STOP FUSION] negative distance dep in def-use` | `has_negative_distance` returned true (`0x1ce6188`) |
| `[STOP FUSION] loop to be fused doesn't access partition dim` | partition-dim gate (`0x1ce61b8`) |
| `loops to be fused are not at same nest level` | level mismatch (`l1->getParent() != l2->getParent()`) |
| `… has fusion prevention instructions in between them` | `traversePath` found blocking instrs |

---

## Adversarial Self-Verification

Five strongest claims re-checked against the binary:

1. **Profit = `2 · Σ liveN · elemSize`.** `[C]` — `getLiveN` call @`0xba13e4`, `imul (%rdx)` @`0xba13f2`, `addss %xmm0,%xmm0` @`0xba1410`. The factor 2 is the literal self-add. The read+write *rationale* is `[S]` (consistent with the `× 2` and with the placed-tensor `type==1` filter, but the binary does not annotate "read+write").
2. **FPG is a max-heap keyed by float profit under `CompFusionPriority`.** `[C]` — the demangled `__adjust_heap<…pair<pair<InstLoop*,InstLoop*>,float>…, _Iter_comp_iter<CompFusionPriority>>` symbol (`nm -DC`, body @`0xbaf890`) and the front-float read `movss 0x10(%rdi)` @`0xba1b0c` jointly pin the structure and key.
3. **LICM invariance = no AP operand (kind==2) defined in-loop.** `[C]` for the mechanism (`cmpl $0x2,0x18` @`0xb90b9f`, AP def-list `0xd8`/`0xe0` @`0xb90bbb`, `DenseSet` membership `0xb90c10..b90d40`); `[S]` for the precise "none defined inside loop ⇒ invariant" phrasing (the set is in-loop defs; membership flips invariance).
4. **`distanceThreshold` default 8.** `[C]` — `movl $0x8,0x78(%rbp)` @`0x7cf85d` at the cl::opt registration site, and `jae` against `+0x78` @`0xba1302` in the constructor.
5. **`has_negative_distance` rejects `d <= 0`.** `[C]` — `bir::getDefUseDistance` call @`0xb8f35a`, `test r14d; jle` @`0xb8f388`; semantics `[S]`.

**Honest re-verify ceiling.** The arithmetic of the profit metric, the heap mechanics, the LICM operand test, and the dependence branch are all CONFIRMED at the instruction level — these are bedrock. What remains `[S]`/`[I]`: (a) the read+write *interpretation* of the factor 2 (the binary does the doubling but does not label why); (b) the exact membership of the two `loop_fusion`/`updateFPG` call sites with the interchange path (`[I]`); (c) the implicit side-effect gating in LICM (`[I]` — inferred from the absence of an explicit hazard predicate). No address, default, or field meaning on this page is fabricated; every unverified inference carries its tag.

---

## Related Components

| Name | Relationship |
|---|---|
| `full_unroll` | Runs immediately before; uses `InstLoop+0x158` unroll flag + full clone (distinct mechanism) |
| `check_loop_fusion` @`0xb95a70` | Classic fusion fallback when `enable-profitable-loop-fusion` is cleared |
| `sb_size_legalization` | Runs after; enforces the `SBSize`/`PSUMSize` budgets that fusion's profit deliberately ignores |
| walrus loop distribution / interchange / tiling | Sibling transforms in the same pass; their `check_*` helpers are reachable from the greedy driver (planned page) |

---

## Cross-References

- [Backend Polyhedral Dependence-Distance](../penguin/backend-dependence-distance.md) — `bir::getDefUseDistance` and the distance model the fusion legality rests on
- [Penguin Loop-Transform Clients](../penguin/loop-transform-clients.md) — the Penguin-side loop-transform clients that feed this backend pass
- [The walrus Pass Pipeline & Optlevel Planes](pass-pipeline-optlevels.md) — where `loop_optimization` sits in the walrus pass order
- [translate-nki-ast-to-bir & Loop Unroll Passes](translate-nki-unroll.md) — `full_unroll`, the predecessor transform
