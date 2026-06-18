# SSA Exit and Pre-Scheduling

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; the geometry is byte-identical across cp310/11/12). The pass bodies live in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `.data` carries a +0x400000 delta. Other wheels differ — treat every address as version-pinned.*

## Abstract

Between the early cleanup passes and the real schedulers, the walrus backend runs five passes whose shared job is to **exit SSA and hand the schedulers and allocators a linearly-ordered, non-aliasing IR**. They are small, structural, and easy to under-rate: none of them is a cost-model search. `instruction_reorder` materializes the upstream modulo schedule into the linear instruction stream. `non_ssa_legalization` is the conceptual centre of the group — it leaves SSA by *renaming*, splitting a value's storage every time a later definition would alias an earlier live use. `pre_sched` is the **first** scheduler, a dependency-only list scheduler over a tensor DAG with a register-pressure tiebreak — explicitly *not* the cycle-accurate `post_sched`. `dead_code_elim_o1` is the -O1 DCE that precedes them, and `localize_shared_memory` reclaims per-core locality from buffers the LNC split left needlessly shared.

The grouping reflects a single contract with the allocators and schedulers downstream. SSA form lets the same logical value be redefined; a register or memory allocator that colors live ranges needs each definition to own distinct storage, so the false WAR/WAW that SSA papers over has to be broken before coloring. That is `non_ssa_legalization`'s entire purpose, and it is the reason the pass runs **twice** — once before allocation to make the IR allocatable, once after allocation to re-legalize the SSA reincarnations that inlining and tensor-copy acceleration reintroduce, this time preserving the physical placement already computed. `instruction_reorder` runs just before the first SSA-exit because the SSA-exit's conflict test is keyed on *linear program order*, which is only meaningful once the schedule has been linearized.

The reference frame is ordinary LLVM with one twist: there is no `LoopInfo`/`ScalarEvolution` here and no global liveness lattice. Liveness is approximated by a flat program-order index stamped into a scratch field of each `bir::Instruction`, plus a conservative "accessed in more than one basic block" escape set. The whole group reads and writes that index, so the order in which the five passes run is not cosmetic — it is the only reason their analyses agree.

For reimplementation, the contract is:

- The `bir::Instruction` **"order"** key (a `std::vector<QuasiAffineExpr>` modulo-schedule coordinate) and the two scratch `uint32` indices (`Inst+0x44`, `Inst+0x4C`) that the group co-opts, and which pass writes which.
- The `instruction_reorder` move-legality model: ordered runs delimited by unordered *barrier* instructions, stable-sorted by the order vector with a program-order tiebreak, spliced into the LLVM ilist.
- The `non_ssa_legalization` clone-rename algorithm: the write-after-read conflict test, the `_SsaClone<N>` storage clone, the access-pattern interval rebind, the matmul-accumulation exemption, and the `sb_allocated`/`allocated` dual-placement gate.
- The `pre_sched` two-stage model (MemoryLocation DAG → per-block instruction list scheduler), its **unit-weight** critical path, its SBUF-byte resource model, and the fact that it carries **no** latency or reservation-table machinery — the headline distinction from `post_sched`.
- The DCE `dont_touch_inst` preservation predicate (three vtable virtuals + an opcode switch) and the `localize_shared_memory` count-then-localize legality test.

| | |
|---|---|
| **`instruction_reorder`** | `InstructionReorder::run` @ `0xbb97a0` → `reorder` @ `0xbb9680` → `follow_modulo_sched_order` @ `0xbb92e0` (order 16, optlevel 1–3) |
| **`non_ssa_legalization`** | `NonSSALeg::run` @ `0x107c580`; clone worker `sub_107C070`; TU `non_ssa_legalization.cpp` (orders 17 **and** 65, optlevel 0–3) |
| **`pre_sched`** | `PreSched::run` @ `0xcad4c0` → `pre_sched::pre_schedule` @ `0xca1cc0` → `run_pre_sched` @ `0xca1490` (order 33); TU `pre_sched.cpp` |
| **`dead_code_elim_o1`** | `DeadCodeElim::run` @ `0xc0b050`; predicate `dont_touch_inst` @ `0xbfcb50` (order 11) |
| **`localize_shared_memory`** | `LocalizeSharedMemory::run(vector<unique_ptr<Module>>&)` @ `0xc00200` (order 12) |
| **Order key** | `bir::Instruction+0xD0` dep-block; "order" vector at `+0xA68`/`+0xA70`; scratch idx `+0x44` (reorder), `+0x4C` (NonSSALeg / pre-order) |
| **Schedule frame** | linear order materialized by `instruction_reorder`; SSA exited by `non_ssa_legalization`; dependency order set by `pre_sched` |

The five passes in -O2 pipeline order (from the per-optlevel builder `sub_80A6E0`):

```text
… 11 dead_code_elim_o1     ← DCE (this page)
   12 localize_shared_memory ← shared→local (this page)
   13–15 verifiers
   16 instruction_reorder   ← materialize modulo schedule (this page)
   17 non_ssa_legalization  ← SSA exit, PRE-allocation (this page)
   18–32 legalize / pre_opts / psum_legalization …
   33 pre_sched (+ memory_analysis_after_pre_sched)  ← FIRST scheduler (this page)
   40 linear_scan_allocator / 44 coloring_psum / 51 coloring_sb …
   58 tensorcopy_accel / 59 peephole_opts …
   65 non_ssa_legalization  ← SSA exit, POST-allocation (this page)
   66 dead_code_elim_o0 / 68 coloring_dram …
   92 post_sched            ← the real cost-model scheduler (Part 8.11, planned)
```

---

## instruction_reorder — Materialize the Modulo Schedule

### Purpose

`instruction_reorder` is the cheapest of the five and the most easily mistaken for something it is not. It is **not** a latency-hiding or DMA-overlap scheduler. It performs one job per basic block: take the multi-dimensional affine **schedule coordinate** that the Penguin software-pipeliner / loop scheduler already attached to each instruction, and re-order the linear instruction stream so it agrees with that coordinate. The real latency-hiding and compute/DMA overlap happen later in `pre_sched`, `post_sched`, `separate_load_and_compute`, and `prefetch` — not here. The pass exists at optlevel 1–3 only; at -O0 there is no pipelining schedule to honor and the pass is omitted (the -O0 builder `sub_806F80` does not include it).

### Entry Point

```text
InstructionReorder::run(bir::Module&)            0xbb97a0
└─ for each Function, for each BasicBlock:
   └─ InstructionReorder::reorder(bir::BasicBlock&)   0xbb9680
      └─ InstructionReorder::follow_modulo_sched_order(bir::BasicBlock&)  0xbb92e0
```

`run` is a pure driver: it pushes a `boost::log` `module_name` attribute, walks the function intrusive list at `module+8` and the block list per function, and pops the attribute on exit.

### The order key

The schedule coordinate is the `bir::Instruction` **"order"** — a `std::vector<QuasiAffineExpr>`, one element per schedule dimension (stage / iteration / position). It does not live in the inline `Instruction` header; it hangs off a 0xA98-byte heap dependency/schedule block at `Instruction+0xD0` (`*((QWORD*)inst+26)`), at offsets:

| Field | Offset (within `+0xD0` block) | Type | Meaning |
|---|---|---|---|
| order begin | `+0xA68` | `QuasiAffineExpr*` | first schedule-dimension coefficient |
| order end | `+0xA70` | `QuasiAffineExpr*` | one past last |

`hasOrder()` is simply `begin != end` (the vector is non-empty). A `QuasiAffineExpr` is 0x20 bytes (the comparator strides it with `shl 5`). `getAxisCoef(LoopAxis*)` extracts the integer coefficient of a given loop axis; the comparator passes axis 0 (the leading/constant term). This is the modulo-schedule key emitted upstream and consumed verbatim here — `instruction_reorder` never recomputes it.

Two `uint32` scratch slots in the inline header are co-opted by this group (both inside the `+0x40..+0x4F` region):

| Slot | Writer | Role |
|---|---|---|
| `Inst+0x44` | `InstructionReorder::reorder` | BB-relative running counter (`mov [rbx+44h],eax`) |
| `Inst+0x4C` | `NonSSALeg::run` and the reorder comparator's tiebreak | persistent flat program-order index (`cmp [rdx+4Ch],[rax+4Ch]`) |

The *roles* of `+0x44` and `+0x4C` are CONFIRMED from the writers/readers; their canonical libBIR field names are not pinned (INFERRED — both sit in the region `bir::Instruction` maps to `optin_passes`/`unique_id`).

### Algorithm

```c
// InstructionReorder::reorder(BasicBlock& bb)  — 0xbb9680
function reorder(bb):
    this.worklist.clear()            // this+0x68 = vector<Instruction*>
    this.last_unordered = null       // this+0x80
    // (a) number instructions by BB position into Inst+0x44
    idx = 1
    for inst in bb.insts: inst[+0x44] = idx++   // bb head gets 0

    run_open = false
    for inst in bb.insts (forward):
        if inst.hasOrder():
            this.worklist.push_back(inst)        // grow a maximal ordered RUN
            run_open = true
        else:
            if run_open: follow_modulo_sched_order(bb)  // FLUSH the run
            this.last_unordered = inst                   // anchor / barrier
            run_open = false
    follow_modulo_sched_order(bb)                // flush trailing run
```

An instruction with an empty order vector (`hasOrder()==false`) is an **immovable barrier**: it splits the block into runs that are sorted independently, and it is never moved. Only spans of ordered instructions between two barriers are permuted.

```c
// InstructionReorder::follow_modulo_sched_order(BasicBlock&)  — 0xbb92e0
function follow_modulo_sched_order(bb):
    // (i) std::sort the current run (introsort → insertion below threshold 128)
    //     depth limit = 2*(63 - clz((end-begin)>>3))   // classic 2*log2 bound
    sort(this.worklist, less)
    // (iii) relink the sorted run into the BB ilist (ilist splice) and
    //       re-register each inst in the BB symbol table
    splice_into_ilist(bb, this.worklist)   // inst+8/+0x10 = next/prev surgery
    this.worklist.clear()

// strict-weak comparator  less(A, B)   — loc_BB9510..BB965e
function less(A, B):
    av = A[+0xD0].order      // begin..end at +0xA68/+0xA70, stride 0x20
    bv = B[+0xD0].order
    n  = min(|av|, |bv|)
    for i in 0..n-1:
        ca = av[i].getAxisCoef(axis=0)
        cb = bv[i].getAxisCoef(axis=0)
        if ca < cb: return true          // A precedes B
        if ca > cb: return false         // B precedes A
    if |av| != |bv|: return |av| < |bv|  // shorter schedule vector first
    return A[+0x4C] < B[+0x4C]            // STABLE tiebreak: original position
```

The relink is genuine LLVM intrusive-list surgery — the assert string `llvm/ADT/ilist_base.h:58 &Next != &First && "Insertion point ..."` confirms an `llvm::ilist` splice — followed by `bir::NamedObjectContainer<BasicBlock,Instruction>::insertIntoSymboltable` so the block symbol table tracks the new sequence.

> **QUIRK — there is no dependency-graph walk here.** Move-legality is delegated entirely to the upstream scheduler that *assigned* the order vector, which already respected the D-E19 flow/anti/output edges. `instruction_reorder` only enforces two structural bounds: (1) unordered instructions are hard barriers, and (2) within a run the permutation is deterministic and stable, so two instructions sharing a schedule coordinate keep their relative order. A reimplementation that re-checks dependencies here is doing redundant work; one that *omits* the barrier rule will hoist a side-effecting unordered op past an ordered run and corrupt the schedule.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `InstructionReorder::run` | `0xbb97a0` | module/function/block driver | CONFIRMED |
| `InstructionReorder::reorder` | `0xbb9680` | per-block run partition | CONFIRMED |
| `InstructionReorder::follow_modulo_sched_order` | `0xbb92e0` | sort + ilist splice | CONFIRMED (disasm; HR failed) |
| comparator `less` | `0xbb9510`–`0xbb965e` | lex order-vector + `+0x4C` tiebreak | CONFIRMED |
| `bir::QuasiAffineExpr::getAxisCoef` | (libBIR extern) | per-dimension coefficient | CONFIRMED (call site) |

---

## non_ssa_legalization — The SSA Exit by Renaming

### Purpose

`non_ssa_legalization` is the conceptual heart of this group: it is the pass that **leaves SSA form**. SSA permits a logical value to be redefined; the backend's register and memory allocators color *live ranges* and require that each definition own distinct storage. Where a later write to a location crosses an earlier live read of the same location — an SSA "phi"/block-argument reincarnation that, on one piece of storage, would manifest as a false WAR/WAW — the pass splits the live range by **cloning the `MemoryLocation` and rebinding the conflicting accesses to the clone**. The downstream allocators then see correct, non-overlapping live ranges. The pass is mandatory at every optlevel including -O0 (it is in the -O0 set `sub_806F80`); it is not an optimization, it is a correctness prerequisite for allocation.

### Entry Point

```text
NonSSALeg::run(bir::Module&)                     0x107c580
├─ per-function gate: FunctionAttribute[10] == sb_allocated?
│    └─ if !sb_allocated: remove_redundant_loads(f, log)   0x159fcb0
├─ Phase A: re-index insts at +0x4C; scan memlocs for write-after-read conflicts
├─ Phase B: per candidate, build the conflict access list + interval set
└─ Phase C (TBB parallel_for): clone + rebind   sub_107C070
```

### The conflict test

```c
// NonSSALeg::run  Phase A  (lines 476-727)
function detect_conflicts(f):
    // (a) flat program-order index into Inst+0x4C  (includes loop nests)
    i = 0
    for inst in getFirstWithin(bb) .. getNextInstFlat .. getLastWithin(bb):
        inst[+0x4C] = i++

    for memloc in f.allocs():           // a MemoryLocationSet
        if not split_candidate(memloc): continue
        v84 = +inf   // min program index of a READ
        v85 = -1     // max program index of a WRITE
        for ap in joined(readers(memloc), writers(memloc)):   // sub_107BFA0
            if isDstPotentialPartialAccess(ap.owner): continue   // 0x1091800
            if is_matmul_accumulation_member(ap.owner): continue  // see exemption
            if ap is READ:  v84 = min(v84, ap.owner[+0x4C])
            if ap is WRITE: v85 = max(v85, ap.owner[+0x4C])
        if v84 < v85:                    // a WRITE after a READ on one storage
            worklist.push(memloc)        // = SSA reincarnation → must split

// split eligibility  (lines 521-545)
function split_candidate(memloc):
    return memloc[+224] == 7            // storage class = DRAM/internal kind
       and memloc[+240] == 0           // subkind
       and isSingleBBAccessed(memloc)  // touched in exactly one BB
       and not isWrittenByAnotherCore(memloc)
       and getRemoteLocalTarget(memloc).empty()
```

The `v84 < v85` test is exactly "a value is defined again across a live use of the same storage" — the condition an SSA→non-SSA pass must split. Candidate eligibility restricts the transform to single-BB internal scratch (`+224==7`), which is where false aliasing actually arises after the front-end's pipelining.

> **GOTCHA — matmul accumulation is deliberately *not* split.** When the writing instruction is a matmul (`InstructionType ∈ {7,8,9}` = `InstMatmult`/`MatmultSparse`/`MatmultMx`, or 95), the pass consults `InstMatmultBase::getCalcAuto()` / `getCalcStart()`. A member of a PSUM accumulation group (`getCalcAuto` true, or not the start of a fresh calc) is **excluded** from the conflict scan. Matmul accumulation is an intentional read-modify-write of one PSUM bank across a tiled-MM chain; cloning it would break the accumulator. A reimplementation that splits accumulation members produces numerically wrong tiled matmuls. The grouping itself is finalized later by `legalize_mm_accumulation_groups`.

### The rename

```c
// sub_107C070  — per-interval clone worker (TBB parallel_for body, lambda#4)
function clone_and_rebind(memloc_ctx, first_idx, last_idx):
    n     = (*memloc_ctx.counter)++                       // per-memloc serial
    name  = memloc.name + "_SsaClone" + decimal(n)
    clone = bir::MemoryLocation::makeClone(memloc, name, memType)  // 0x61e330
    if memloc[+168] /* allocated, D-E13 */:               // PHYSICAL-AWARE branch
        clone.allocate(memloc.getBasePartition(),
                       memloc.getBankId(), memloc.getAddress())   // SAME slot
        clone.setUserAllocated(memloc[+208])
    for k in [first_idx .. last_idx]:                     // the conflicting interval
        accesspattern[k].setLocation(clone, …)            // 0x5e9cd0 — REBIND
```

Phase B prepares each candidate by gathering its `PhysicalAccessPattern`s, sorting them by owner program index (`*(*(ap+32)+76)` = owner`+0x4C`), and computing the address intervals to split on. It does this by inserting a throwaway sentinel instruction named `dummy_inst_for_non_ssa_legalization_pass`, building a `PhysicalAccessPattern` on it to materialize the value's `getAddressIntervals` as a `boost::icl::interval_set<u64>`, then removing the sentinel. A string-keyed hash (seed `0xC70F6907`) over `PhysicalAccessPattern::toString()` de-duplicates identical address ranges, and an assert `cur_end >= pair.second` at `non_ssa_legalization.cpp:337` guards interval monotonicity. After renaming, any memloc that becomes fully unused is dropped via `MemoryLocationSet::removeMemoryLocation`. The pass logs `[Non-SSA legalization]created <N> memorylocations` at severity 20.

### Why it runs twice — the dual placement

The only behavioral difference between order 17 and order 65 is driven by `FunctionAttribute[10] = sb_allocated` (found by a hash probe `find(key=10)`, hash `0xA % bucket_count`) and `MemoryLocation+168 = allocated`.

| | Order 17 — PRE-allocation | Order 65 — POST-allocation |
|---|---|---|
| `sb_allocated` | false | true |
| `remove_redundant_loads` | **runs first** (loads not yet pruned) | **skipped** (already done) |
| clone `+168`/allocated branch | not taken — clones are unplaced | taken — clone copies base-partition/bank/address, re-allocates to the **same** physical slot |
| Job | hand the PSUM(44)/SB(51) coloring allocators a non-SSA, non-aliasing module | re-legalize the SSA conflicts that inlining (48/60/61) and `tensorcopy_accel`(58)/`peephole_opts`(59) reintroduced, **preserving** the coloring just computed |
| Consumer | the allocation phase | the DRAM allocator(68), `post_sched`(92), and codegen — the **last** SSA exit before register allocation |

> **NOTE — the two placements bracket the entire allocation phase.** Order 17 makes the IR allocatable; order 65 re-makes it allocatable after inlining/tensor-copy churn *without* disturbing the physical allocation. At -O0 only the early placement exists, which is internally consistent: SB coloring never runs there, so `sb_allocated` is never set true and the late variant would have nothing different to do.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `NonSSALeg::run` | `0x107c580` | driver + Phase A/B/C | CONFIRMED (decompile + disasm) |
| `sub_107C070` | `0x107c070` | clone + rebind worker | CONFIRMED |
| `sub_107AF30` | `0x107af30` | insertion-sort APs by owner `+0x4C` | CONFIRMED |
| `sub_107B2E0` | `0x107b2e0` | introsort entry over APs | CONFIRMED |
| `bir::MemoryLocation::makeClone` | `0x61e330` (lib) | clone the storage object | CONFIRMED (call site) |
| `bir::PhysicalAccessPattern::setLocation` | `0x5e9cd0` (lib) | rebind an access to the clone | CONFIRMED (call site) |
| `remove_redundant_loads` | `0x159fcb0` | pre-alloc load pruning (758 ln) | CONFIRMED |
| `isDstPotentialPartialAccess` | `0x1091800` | partial-dst filter | CONFIRMED |

---

## pre_sched — The First, Dependency-Only Scheduler

### Purpose

`pre_sched` is the **first** of the backend's two schedulers. It orders instructions by dependency, with a register-pressure tiebreak, and nothing more. It is deliberately *not* the cycle-accurate scheduler — there is no per-engine issue-slot reservation, no partition-band reservation table, and no hardware latency weighting anywhere in its bodies. The latency-aware, reservation-driven scheduling is the job of `post_sched` (order 92) and the time-aware scheduler, both planned siblings of this page (Part 8.11 and 8.12). `pre_sched`'s purpose is to give those passes — and the allocators between them — a good, dependency-legal, pressure-aware *starting* order.

> **NOTE — pre_sched vs post_sched is the distinction to get right.** Both produce an instruction order, but their cost models are categorically different. `pre_sched`'s critical path is **unit-weight** longest-path over a tensor DAG (`compute_depths_forward` @ `0xc8f610`, `compute_depths_backward` @ `0xc8f270`) — every edge counts 1, the only non-unit contribution being a loop trip-count "hack". Its resource model is the **SBUF/PSUM live-byte budget**, not pipeline cycles. There is **no** `TrainiumHwm::getLatency*` / `bir::Hwm` reference in any `pre_sched`/`pre_scheduler` body, and **no** reservation table. A reimplementation that fuses the two into one latency-weighted scheduler will mis-place the allocators (which expect to run *between* a dependency order and a cycle-accurate order) and will lose the cheap, pressure-reducing first pass entirely.

### Entry Point

```text
PreSched::run(bir::Module&)                  0xcad4c0   (thin shim: demangle + ctor)
└─ pre_sched::pre_schedule(Module&, PassOptions&, Logger&)   0xca1cc0   (driver §below)
   └─ for each Function:
      pre_sched::run_pre_sched(Function&, Logger&, level, flag)   0xca1490
      ├─ pre_sched::init(F)              0xc9a870   ← STAGE A: build memloc DAG
      ├─ pre_sched::sort_mem_locs(F, level, log)   0xc9df40   ← STAGE A: order memlocs
      ├─ pre_sched::order_insts_by_mlocs(F)        0xc9b990
      ├─ [conditional] LoadCloning::run(F, log)    (duplicate loads, cut false anti-edges)
      └─ pre_scheduler::schedule(F)      0xcb2dd0   ← STAGE B: per-block list scheduler
```

A paired `MemoryAnalysisAfterPreSched` pass runs right after to recompute SB/PSUM pressure (registered as `memory_analysis_after_pre_sched`).

### Module driver

`pre_sched::pre_schedule` (`0xca1cc0`) reads `numcores` and a tuning flag from `PassOptions`, selects a scheduling **level**, and runs:

```c
function pre_schedule(module, opts, log):
    // 1. NO-REORDER GUARD: scan for an InstLoop (IT==105) carrying no_reorder (Inst+249)
    if found:  log "Skipping pre-scheduling as we detected no_reorder block"; return
    // 2. SPILL ANALYSIS: LayerSpiller + split_far_live_ranges(module)  0xc9e660
    if too_many_spills:  log "Too many spills, skip pre scheduling"; return
    // 4. assert std::distance(functions.begin, end) == 1   (pre_sched.cpp:1014)
    // 5. CLEANUP/DEP-PREP (gated by level): remove_redundant_memsets,
    //    remove_redundant_loads (skipped at level 4), DCE,
    //    "build flow dependencies"  ← materializes the FLOW edges Stage A reads
    // 6. SCRATCHPAD OPT: early_sched_io(f) per function; reduce_tensor_size(module)
    // 7. for each function: run_pre_sched(f, log, level, flag)     ← STAGE A+B
    //    for each function: assert_no_cycles(f)   ← validate acyclic
    // 8. erase_flow_dependencies(module)  ← tear down the scheduling scaffolding
    // 9. log "sb_hwm=<n>" + "DONE PRE scheduling"
```

The FLOW edges built in step 5 are scaffolding for scheduling only; step 8 tears them down so the real synchronizer rebuilds the surviving cross-engine dependencies. Levels 1 and 2 are the valid scheduler levels (level 1 = trust the tensorizer order and refine; level 2 = full height/depth sort); any other level raises `NeuronAssertion(828) "level"` at `pre_sched.cpp:644`.

### Stage A — the MemoryLocation DAG

The nodes scheduled in Stage A are **tensors**, not instructions: a `bir::MemoryLocation` is produced by one instruction and consumed by others, and ordering the memlocs producer-before-consumer induces the instruction order. `init` (`0xc9a870`) builds two LLVM `DenseMap`s on the pass object:

| Slot | Type | Meaning |
|---|---|---|
| `this+48` | `DenseMap<MemoryLocation*, DenseSet<MemoryLocation*>>` | adjacency (the DAG edges) |
| `this+120` | `DenseMap<MemoryLocation*, mloc_extra_data>` | per-node depth/fringe annotations |

Edges come from the `bir::Instruction` dependency block: a FLOW edge `A→B` becomes an edge from A's output memloc to B's output memloc. ANTI/OUTPUT (from `anti_dependency_analyzer`) and ORDERED (from `order_constraints`) edges feed in too, because they were materialized as Instruction dependency-set members before `pre_sched` runs. The `EdgePtr` low-3-bit kind tag is *untagged* (`&~7`) — the memloc DAG keeps reachability, not edge kind. Loop bodies are folded in via `count_inst_with_hack` (`0xc902c0`), which walks a loop body and counts its instructions so a loop's effective DAG weight scales with its instruction count.

The priority is a structural critical path written into `mloc_extra_data`:

```c
// compute_depths(F)  0xc8f8f0
function compute_depths(f):
    for node: node.depth(+40) = -1
    for node:
        compute_depths_forward(node, depth=1)    // 0xc8f610: longest path FROM sources
        compute_depths_backward(node)            // 0xc8f270: longest path TO sinks
    // edge weight is UNIT (depth+1 per hop); NO getLatency/Hwm anywhere
// compute_fringe(F)  0xc8e980  — TBB parallel_for; sets schedulability-frontier flags
```

`get_initial_order` (`0xc97bb0`, comparator `sub_C8E380`) sorts all memlocs by a class-flag → depth(descending) → program-index(`MemoryLocation+16`, ascending) priority, and `dfs_topo_sort` (`0xc98c00` / helper `0xc98720`) runs a priority-seeded, 3-color DFS topological linearization: children are visited in priority order, the node is emitted post-order, so ties inside the DAG break toward the critical path. There is no per-cycle ready queue; "ready" is implicit in the seed order plus the visited `DenseSet`. `copy_mloc_order_to_f` (`0xc99560`) writes the order back onto the Function's storage list.

### Stage B — the instruction list scheduler

```c
// pre_scheduler::schedule(BasicBlock&)  0xcb1900
function schedule_block(bb):
    insts = snapshot(bb)
    sort(insts, priority_cmp)          // comparator sub_CAF530:
        // 1. is_loop_inst → register-pressure path
        // 2. if both WRITE a memloc and the memlocs differ:
        //      order by per_memloc_data+8  (the Stage-A memloc order)  ← the coupling
        // 3. tie: inst_use_size(a) < inst_use_size(b)   (Sethi-Ullman flavor)
        // 4. final tie: per_inst_data[0] program-order index (stable)
    for inst in insts not yet scheduled:
        inst_order_recurse(inst, scheduledSet, outVec)   // 0xcb1600
        // walks Inst+0xD0 dep list (EdgePtr &~7), emits in-block predecessors first
    splice_ilist(bb, outVec)           // add_linear_order 0xcb1550 (insertIntoSymboltable)
```

The Stage-B coupling at step 2 is what makes the instruction order honor the tensor schedule: the instruction writing the earlier-scheduled tensor goes first. The resource model is register pressure measured in **bytes**:

| Quantity | Address | Definition |
|---|---|---|
| `inst_use_size` | `0xcaede0` | Σ input-memloc byte sizes (`memloc+264`) read by the inst |
| `inst_def_size` | `0xcaefb0` | output memloc bytes, but only if first def of that memloc; else 0 |
| `inst_kill_size` | `0xcaeb60` | Σ bytes of input memlocs whose **last** use is this inst (freed) |
| `inst_gain_size` | `0xcaf010` | `kill − def` = net SBUF bytes freed by scheduling this inst now |
| `is_psum_reader_b` | `0xcaefa0` | does the inst read PSUM (must follow its matmul producer) |

### Cycle detection

`assert_no_cycles` (`0xc941a0`) drives `topSortDfs` (`0xc93ba0`) — a recursive 3-color DFS (unvisited / on-stack / done) over the `this+48` adjacency map. A successor reached while "on-stack" (gray) is a back-edge = a cycle, and the build fails immediately rather than emitting a deadlocking schedule. It is invoked twice per the driver (after each scheduling pass).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `PreSched::run` | `0xcad4c0` | pass entry (thin shim) | CONFIRMED |
| `pre_sched::pre_schedule` | `0xca1cc0` | module driver | CONFIRMED |
| `pre_sched::run_pre_sched` | `0xca1490` | per-function core | CONFIRMED (disasm; HR failed) |
| `pre_sched::init` | `0xc9a870` | Stage A DAG build | CONFIRMED |
| `compute_depths_forward` / `_backward` | `0xc8f610` / `0xc8f270` | unit-weight critical path | CONFIRMED |
| `pre_sched::dfs_topo_sort` | `0xc98c00` | priority-seeded topo sort | CONFIRMED |
| `pre_scheduler::schedule(BasicBlock)` | `0xcb1900` | per-block list scheduler | CONFIRMED |
| `inst_use_size` / `inst_gain_size` | `0xcaede0` / `0xcaf010` | SBUF-byte resource model | CONFIRMED |
| `topSortDfs` / `assert_no_cycles` | `0xc93ba0` / `0xc941a0` | 3-color cycle check | CONFIRMED |

---

## dead_code_elim_o1 — Bottom-Up DCE with a Side-Effect Predicate

### Purpose

`dead_code_elim_o1` is the -O1 dead-code eliminator that runs just before the SSA-exit group, at order 11. `DeadCodeElim` is a **single class** with an opt-level int and a LIW-spill bool in its ctor (`0xc0cbb0`): the `dead_code_elim_o0` and `dead_code_elim_o1` registry entries build the same class, differing only in the opt-level int (`this+0x6C`: O0=0, O1=1). At O1 the pass runs dead-store elimination, LIW spill cleanup, the use/def worklist, four per-BB DMA/loop peepholes, and a dead-memloc prune; at O0 only the use/def worklist + tensor-map prune run.

### Algorithm

```c
// DeadCodeElim::run(bir::Module&)  0xc0b050
function dce_run(module):
    load tensor_map.json (from Module artifact path or embedded map)   // pruned in place
    if opt_level > 0:                                  // this+0x6C
        for f in module:  eliminateDeadStore(f)        // 0xc02950
    if liw_spill_bool /* this+104 */:
        remove_extra_spill_for_liw_flow(module)        // 0xbfdb80
    remove_inst_with_no_user(module, &tensor_map)      // 0xc06a60 — core use/def sweep
    for f in module:
        if not hasAttribute(f,10) and not hasAttribute(f,11):   // no-DCE / boundary markers
            for bb in f:
                remove_empty_loops(bb)                          // 0xbff3c0
                remove_must_alias_dmacopy(bb)                   // 0xbff540
                remove_redundant_alias_dmacopy(bb)              // 0xc01af0
                remove_redundant_internal2internal_dmacopy(bb)  // 0xc09430
        remove_dead_memlocs(f, tensor_map)             // 0xc05ec0
    save tensor_map.json
```

The core sweep `remove_inst_with_no_user` (`0xc06a60`) is a classic bottom-up dead-instruction elimination to fixpoint: it builds `getAllMultiBBAccessedMemLocs(f)` (locations whose liveness escapes a single BB — not prunable locally), iterates the flat instruction array **in reverse** (bottom-up liveness), and for each instruction not preserved by `dont_touch_inst` checks whether any result is observable (a write to a host-visible Input/Output `TensorKind`, or a `RegisterAccess` whose location escapes the block). If nothing observable survives, `removeInstructionAndUpdateDeps` deletes it, sets the `changed` flag, and prunes any memloc whose read/write counts both drop to zero. The whole module is re-swept while `changed` (the outer `do…while(v302)`), so removing a producer transitively kills its now-dead operands.

### The side-effect / sync preservation predicate

```c
// dont_touch_inst(Instruction* I)  0xbfcb50  — returns 1 = NEVER remove
function dont_touch_inst(I):
    // three bir::Instruction vtable virtuals — the observable-effect trio
    if (*I->vtable[+0x18])(I): return 1   // *(*I+24): has-side-effect / observable
    if (*I->vtable[+0x20])(I): return 1   // *(*I+32): is-sync / barrier / semaphore
    if (*I->vtable[+0x10])(I): return 1   // *(*I+40): must-keep / not-safely-removable
    switch (I.InstructionType /* I+0x58 */):
        case 105: return 1                         // Loop — control construct, never DCE'd
        case 4:   keep = I[+0x124] != 0; …         // Activation accumulate
        case 29:  keep = I[+0x1F0] != 0            // TensorScalarPtr indirect-scalar
        case 30:  keep = I[+0x154] != 0            // TensorScalarCache PSUM-cache write
        case 31:  keep = I[+0x0F4] != 0            // TensorTensor accumulate/reverse
        case 63:  keep = I[+0x124] != 0            // RangeSelect side flag
        case 103: keep = I[+0x0F0] != 0            // Exponential accumulate
        case 52:  keep = I[+0x144] != 0            // CopyPredicated predicate-side flag
        default:  return <slot-derived bool>
```

> **GOTCHA — sync/semaphore/barrier/collective preservation is in the vtable, not a name list.** No sync opcode (EventSemaphore, AllEngineBarrier, Drain, Halt, CoreBarrier, side-effecting DMA, CustomOp/InlineASM, collectives) appears by number in the switch. They are caught by the three base virtuals, which every `Inst` subclass overrides. A reimplementation that enumerates a name allow/deny list instead of asking the instruction "do you have observable effects / do you synchronize" will silently delete a semaphore or a collective. The opcode switch only adds the seven compute opcodes whose accumulate/cache/command field, when set, makes the op a non-idempotent writer into a PSUM accumulator or cache — preserved even when its nominal SSA result looks unused. The three vtable slot *roles* are INFERRED from position (the base `getValidEngines` anchors vtable+96); the three-virtual gate and the opcode switch are CONFIRMED.

The memloc analogue `dont_touch_memloc` (`0xc059c0`) keeps any location that is Shared address space, written-by-another-core, has a remote target, is host-visible I/O, or is touched by an indirect DMA (`InstructionType ∈ {43,45,46}`). DCE therefore **refuses to delete shared/cross-core allocations** — converting those to local is the next pass's job.

---

## localize_shared_memory — Reclaim Per-Core Locality

### Purpose

`localize_shared_memory` (order 12) runs immediately after DCE and recovers SBUF locality that the LNC split gave up conservatively. After `lnc_splitter` (order 8), each Module holds one `bir::Function` per logical neuron core, and some DRAM scratch buffers are still marked Shared even though, post-split, only one core actually touches them. This pass counts per-core touchers of each shared internal buffer and converts the provably single-core ones from Shared DRAM back to per-core Local storage — a single enum store, no data movement.

### Algorithm

```c
// LocalizeSharedMemory::run(vector<unique_ptr<Module>>&)  0xc00200
function localize(module):
    accessCount: unordered_map<string,int>            // keyed by MemoryLocation name (loc+296)
    numFns = functions_end - functions_begin

    // PASS 1 — COUNT toucher cores
    for f in module:
        for loc in f.allocs() with addr_space(+0xF0/+240)==1     // Shared
                              and TensorKind(+0xE0/+224)==7:       // Internal
            if hasReader(loc) or hasWriter(loc):                   // refcount nonzero
                ++accessCount[loc.name]

    // PASS 2 — LOCALIZE / PRUNE
    for f in module:
        deadLocs = {}
        for loc in f.allocs() with addr_space==Shared and kind==Internal:
            if hasReader(loc) or hasWriter(loc):                  // touched by THIS core
                if accessCount[loc.name] < numFns                 // not touched by EVERY core
                   and not loc.allocated(+168)                    // not pinned
                   and accessCount[loc.name] == 1:                // exactly ONE core
                    *(loc+0xF0) = 0                                // ★ Shared(1) → Local(0)
            else:
                deadLocs.push(loc)                                // untouched here ⇒ removable
        for loc in deadLocs: removeMemoryLocation(f, loc, 1)
```

### Legality

A shared-DRAM internal scratch buffer becomes per-core Local iff **all** hold:

| Test | Field | Meaning |
|---|---|---|
| L1 | `addr_space(+240)==1 && kind(+224)==7` | internal cross-core scratch, not host I/O / Const / Pointer |
| L2 | `accessCount[name]==1` | exactly one of the post-split cores reads-or-writes it ⇒ provably single-core |
| L3 | `accessCount[name] < numFns` | guards the `numFns==1` corner (no churn on single-core modules) |
| L4 | `!allocated(+168)` | not already pinned to a fixed physical address |

> **NOTE — collective safety is structural, from counting *after* the LNC split.** Because the toucher count is taken over the per-core Functions, any buffer a collective or a second core touches has `accessCount ≥ 2` and fails L2. There is no explicit alias analysis for cross-core safety; the post-split counting domain *is* the safety argument. The rewrite is a single store `*(loc+0xF0)=0`; the downstream shared-DRAM vs SB/PSUM allocators and `sync_shared_allocations` then place the now-Local buffer in per-core space and drop the cross-core synchronization a shared buffer would have required. L1–L4 are CONFIRMED; the collective-safety attribution is INFERRED from the counting domain.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `LocalizeSharedMemory::run(vector<unique_ptr<Module>>&)` | `0xc00200` | 2-pass count-then-localize | CONFIRMED |
| `LocalizeSharedMemory::run(Module&)` | `0xbfc770` | 14-line stub returning `Status(2)` | CONFIRMED |
| `DeadCodeElim::run` | `0xc0b050` | -O1 DCE driver | CONFIRMED |
| `dont_touch_inst` | `0xbfcb50` | side-effect/sync preservation | CONFIRMED (gate); INFERRED (slot roles) |
| `remove_inst_with_no_user` | `0xc06a60` | reverse use/def sweep to fixpoint | CONFIRMED |
| `eliminateDeadStore` | `0xc02950` | single-BB dead-store elim | CONFIRMED |
| `dont_touch_memloc` | `0xc059c0` | memloc preservation | CONFIRMED |

---

## Confidence and Gaps

- **CONFIRMED** by reading the pass bodies: both `instruction_reorder` bodies (the modulo-sort comparator, the `+0x4C` tiebreak, the ilist splice); the `NonSSALeg` clone-rename worker `sub_107C070`, the `_SsaClone` naming, the `sb_allocated` gate, the `allocated(+168)` physical-clone branch, the `dummy_inst_for_non_ssa_legalization_pass` sentinel, the matmul-accumulation exemption, and the dual-placement orders; `pre_sched`'s two-stage structure, the unit-weight critical path, and the absence of any `getLatency`/`Hwm`/reservation-table reference; the DCE `dont_touch_inst` three-virtual gate plus opcode switch; the `localize_shared_memory` ==1/==7 gate and `*(+0xF0)=0` rewrite.
- **STRONG**: `hasOrder`/`setOrder` semantics (libBIR externs read at the def site + call sites); the `+224==7` storage-class candidate filter (numeric, name from the class-field convention); the downstream allocator effects of the `localize` rewrite (from the pass-order table).
- **INFERRED**: the canonical libBIR field names of the `+0x44` vs `+0x4C` scratch `uint32` slots (their *roles* are CONFIRMED, the names are not pinned); the three `dont_touch_inst` vtable slot roles (positions confirmed, semantics inferred from role + the `getValidEngines`-anchored table); the collective-safety argument for `localize_shared_memory` (inferred from the post-split counting domain, not an explicit alias check).
- **Gap**: `bir::QuasiAffineExpr::getAxisCoef` internals (which `LoopAxis` the `axis=0` call selects per dimension) live in libBIR and were not read here — the comparator treats the result as an opaque per-dimension integer, which is sufficient for the sort semantics.

## Cross-References

- [The walrus Pass Pipeline & Optlevel Planes](pass-pipeline-optlevels.md) — the per-optlevel builders that place these five passes (orders 11, 12, 16, 17, 33, 65)
- [BackendPass Hierarchy & the 150-Name→121-Class Registry](backendpass-registry.md) — how `non_ssa_legalization` registers under one name at two pipeline positions, and the `MemoryAnalysis` snapshot names
- [translate-nki-ast-to-bir & Loop Unroll Passes](translate-nki-unroll.md) — the unroll pass at order 9 that precedes this group; the upstream that emits the `bir::Instruction` order vector
- [DMA Legalization — Strided / CCE / Generic-Indirect](dma-legalization.md) — the legalize passes (orders 18–19) that follow the early SSA-exit
- *Planned (Part 8):* `peephole-constprop-remat` (8.7) — the prior cleanup that, with `tensorcopy_accel`, reintroduces the SSA reincarnations the late `non_ssa_legalization` re-legalizes; `post_sched` (8.11) — the cost-model scheduler `pre_sched` feeds; the reservation-table disambiguation (8.12); and the dependency model (8.9) whose FLOW/ANTI/OUTPUT/ORDERED edges `pre_sched` lifts into its tensor DAG.
