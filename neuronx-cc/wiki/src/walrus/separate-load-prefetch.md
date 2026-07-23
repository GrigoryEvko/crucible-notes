# Separate-Load-and-Compute and Prefetch Scheduling

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). Both passes live in `neuronxcc/starfish/lib/libwalrus.so`. For `.text` (0x62d660…) and `.rodata` (0x1c72000…) the virtual address equals the file offset; `.data` carries a +0x400000 delta. The `0x5e…/0x5f…/0x6…` band is `.plt` thunks — every body cited here is a real `.text` frame, cross-checked against the retained `.dynsym` (`nm -DC`). Other wheels differ — treat every address as version-pinned.*

## Abstract

Two libwalrus backend passes turn a straight-line loop body into a software-pipelined one: `separate_load_and_compute` cuts the loop body into a **load phase** and a **compute phase** and drops a hard schedule fence between them, and `prefetch_scheduling` decides **which** loads are eligible to be hoisted across that fence and **in what order**. Neither pass is a monolithic software pipeliner. The backend has no single `SoftwarePipelineCodeGen`; pipelining is *emergent* — these two passes set up the dependency graph so the downstream list scheduler ([8.11, `post_sched`](post-sched-schedulers.md)) and the address-rotation multi-buffering can overlap iteration *i*'s compute with iteration *i+1*'s loads. This page documents the cut, the barrier, the before/after boolean, and the one fact a reimplementer is most likely to get wrong.

The cut is mechanical and visible in the dependency graph. `separate_load_and_compute` classifies every loop-body instruction by its opcode field: `Load`/`DMACopy` are the **load class** (stage 0), everything else is **compute** (stage 1). A load whose source has no incoming flow dependency *qualifies* — its data comes from outside the iteration, so it can be issued ahead. The pass then severs each qualified load's predecessor edges (`removeDependency` — the WAR/anti severance that pinned `load(i)` behind `compute(i-1)`) and splices an `InstCoreBarrier` (opcode 87) named `separation_barrier` immediately before the first compute instruction, wiring `every load → barrier → every compute` with Flow edges. The result is a hard stage divider expressed entirely in the dep graph; the list scheduler then slides `load(i+1)` up next to `compute(i)` because nothing forbids it anymore.

The headline gotcha lives in the prefetch pass. **The prefetch distance is structural, not latency-driven.** A reimplementer who expects `prefetch_scheduling` to compute "issue the load *N* cycles ahead" from DMA latency is wrong: the entire prefetch translation unit contains **no** `getLatency`/`Hwm`/cost-model call (verified by sweeping every body in `0xb64810`–`0xb67ca0` — clean). The lookahead window is the dependency live-range inside a basic block; the one-iteration-ahead intent is carried as a *constraint attribute* (`prefetch_one_iteration_ahead`) set upstream and read by the loop machinery, never computed here. `prefetch_scheduling` only picks eligible large tensors and Flow-pins their load order; the cycle-level overlap is delegated.

For reimplementation, the contract is:

- The opcode-based load/compute classification and the qualification test (no incoming flow dependency on the load's source).
- The dependency CUT (`removeDependency` on each qualified load's predecessor edges) and the `InstCoreBarrier` fence insertion (`load* → barrier → compute*`, all Flow edges).
- The `(postAda, withMemset)` ctor-boolean matrix that selects the three `separate_load_and_compute` variants, and the `after_sched` boolean that selects prefetch before/after.
- The negative result: prefetch distance is the BB live-window plus a constraint attribute, **not** a function of DMA latency — there is no cost-model call to reproduce.

| | |
|---|---|
| **Separate-load class** | `neuronxcc::backend::SeparateLoadAndCompute` (BackendPass subclass) |
| **Separate-load `run`** | `SeparateLoadAndCompute::run(bir::Module&)` @0x15bf170 (~0x44e0 bytes) |
| **Separate-load ctor** | `…::SeparateLoadAndCompute(PassOptions const&, bool postAda, bool withMemset)` @0x15c3e30 |
| **Variant bools** | `obj+0x2c0` = postAda, `obj+0x2c1` = withMemset (class size 0x2c8) |
| **CoreBarrier insert** | `insertElement<InstCoreBarrier>` @0x15c2d68; barrier name `separation_barrier` @0x1c875a5 |
| **The CUT** | `Instruction::removeDependency` @0x5f5ae0 (plt), call sites 0x15c002b / 0x15c17c4 / 0x15c2a04 |
| **Prefetch class** | `neuronxcc::backend::PrefetchScheudling` (TU typo "Scheudling" is real) |
| **Prefetch `run`** | `PrefetchScheudling::run(bir::Module&)` @0xb66d90 (464 bytes) |
| **Before/after bool** | `obj+104` (`*((u32*)this+26)`) = after_sched flag {0=before, 1=after} |
| **Prefetch-distance basis** | BB live-window + `prefetch_one_iteration_ahead` attr — **no latency/cost call** |

---

## Separate-Load-and-Compute

### Purpose

`separate_load_and_compute` partitions a loop body into a load stage and a compute stage and inserts a hard schedule fence between them, so the downstream scheduler is free to overlap one iteration's loads with the previous iteration's compute. One C++ class backs three registered pass names; the variant is chosen by two constructor booleans (`postAda`, `withMemset`). The base variant is an analysis/barrier pass — its own banner @0x1d7a1c8 reads *"This pass will break your program! Use for analysis purposes only."* — that marks the split in the dep graph; the `_post_ada` variant performs the real physical code motion once the schedule is final.

### Entry Point

```text
SeparateLoadAndCompute::run(Module&)        @0x15bf170  (~0x44e0 B)
  ├─ get_inst_in_order(Function&, vec)       @0x62a640 (plt)  ── capture program order (×2)
  ├─ [postAda==0] per-function classify/cut/barrier loop
  │    ├─ removeDependency(load, edge, 0)    @0x5f5ae0 (plt)  ── the CUT
  │    ├─ insertElement<InstCoreBarrier>     @0x61bf40 (plt)  ── the stage divider
  │    └─ addDependency(…, Flow=4, …)        @0x5eff00 (plt)  ── fence wiring
  └─ [postAda==1] post_ada physical-hoist worker  @0x15be280  ── "Removing deps for …"
```

The three registration lambdas each `new` a 0x2c8 object then call the ctor with a distinct `(postAda, withMemset)` pair (the `xor` / `mov edx` / `ecx` immediately precede the ctor call):

| Pass name | postAda (edx) | withMemset (ecx) | Reg invoke | Role |
|---|---|---|---|---|
| `separate_load_and_compute` | 0 | 0 | @0x15c4760 | base — analysis/barrier only |
| `separate_load_and_compute_post_ada` | 1 | 0 | @0x15c49b0 | physical hoist (core_v4/gen4), post-schedule |
| `separate_load_and_compute_with_memset` | 0 | 1 | @0x15c4880 | base + gather-index zero-init |

> **NOTE —** the ctor stores both bools as a single 16-bit write: `mov WORD PTR [rbx+0x2c0],ax` @0x15c44a2 places postAda at `obj+0x2c0` and withMemset at `obj+0x2c1`. `run()` reads postAda back with `cmp BYTE PTR [rax+0x2c0],0x0` @0x15bf37b — the very first behaviour branch.

### Algorithm

```c
function SeparateLoadAndCompute_run(Module& M):              // 0x15bf170
    push_log_attr("module_name", M.getName())                // diagnostics only
    if obj[0x2c0] != 0:                                       // postAda?  cmp @0x15bf37b
        return post_ada_hoist(M)                              // §post_ada — jumps to 0x15c15de

    for F in M.functions:                                     // fn list @module+0x8
        vec = []
        get_inst_in_order(F, vec)                             // 0x15bf437 — capture order ONCE
        loads = []; first_compute = NULL; last_load = NULL

        // ---- PHASE A: classify + qualify (loops @0x15bf500 / 6ec / b40) ----
        for I in vec:
            op = I[0x58]                                       // InstructionType (u32)
            if op == 0x13 /*Load*/ or op == 0x20 /*DMACopy*/:  // LOAD CLASS = stage 0
                if source_has_incoming_flow_dep(I):            // walk dep set @I+0xD0
                    log("Disqualified Load/DMACopy due to flow dependencies")
                    continue                                   // stays in COMPUTE (stage 1)
                log("Found Load/DMACopy without flow dependencies")
                loads.append(I); last_load = I                 // QUALIFIED → stage 0
            else if first_compute == NULL:
                first_compute = I                              // "First non-load/copy" anchor

        if loads.empty():    log("No qualifying loads found, skipping function");      continue
        if first_compute==NULL: log("No non-load instructions found, skipping function"); continue

        // ---- PHASE B: the dependency CUT (loop @0x15c0020) ----
        for L in loads:                                        // sever predecessor edges
            preds = snapshot(L.dep_block.predecessors)         // @L+0xD0 -> +0x08
            for e in preds:
                L.removeDependency(e, /*recompute=*/0)         // 0x15c002b — WAR/anti severance
            if obj[0x2c1] /*withMemset*/ and L.op==0x2B /*IndirectLoad*/:
                wire_memset_before(L)                          // §with_memset — re-attach zero-init

        // ---- PHASE C: insert the InstCoreBarrier stage divider (@0x15c2d28..30cb) ----
        bb   = first_compute.parentBB                          // @inst+0x50
        iter = ilist_iterator(first_compute)
        checkInsertionPointValid(bb, iter)                     // 0x15c2d5a
        bar  = bb.insertElement<InstCoreBarrier>(iter, "separation_barrier")  // 0x15c2d68, op 87
        bar[0x140] = engine_ids(F[0x204])                      // fence ALL engines of the fn
        for L in loads:        bar.addDependency(L,  Flow=4, …)   // 0x15c2eae — barrier waits for loads
        for U in first_compute.. : 
            cloneToArgument(U, …)                              // 0x15c2f78 — re-bind compute-side AP
            U.addDependency(bar, Flow=4, …)                    // 0x15c2fdc — compute waits for barrier
        log("Inserted CoreBarrier before <first_compute>")     // 0x15c3027

        // ---- PHASE D: verify (@0x15c3134) ----
        assert(status && "Unexpected edge found. Disable separation pass.")
```

The classification is opcode-driven, read straight off `Instruction+0x58`. The qualification test is the substantive part: a qualified load is one whose *source* carries **no incoming flow dependency**, meaning its data is loaded from outside the loop rather than produced this iteration. Those are exactly the loads that can be issued one iteration early. Disqualified loads (their data was computed this iteration) stay in the compute stage. The boundary between stages is the **first non-load/copy** instruction in program order — the anchor logged @0x15bf5f6 and used as the barrier insertion point.

> **GOTCHA —** the base variant deliberately does *not* move any instruction. It only edits the dependency graph (cut + barrier). The "will break your program" banner is honest: in base form it is a legality/analysis gate, and the final `assert "Unexpected edge found"` @0x15c3134 aborts if any edge the split should have removed survives. The actual code motion is `_post_ada`'s job, run later. A reimplementer who makes the base pass relocate instructions has merged two passes that the binary keeps separate.

### The Dependency Cut

The cut is three `removeDependency` call sites (0x15c002b base, 0x15c17c4 with_memset, 0x15c2a04 post_ada), each driven by a snapshot of the qualified load's predecessor edges. Snapshotting first and removing second avoids invalidating the dep-set iterator mid-walk. The edges removed are the WAR/anti edges that pinned `load(i)` behind `compute(i-1)` — once gone, the scheduler may hoist `load(i+1)` past `compute(i)`. The cut alone does not multi-buffer; if `load(i+1)` and `load(i)` still target the same physical buffer, the severed hazard would reappear. The matching physical multi-buffering is supplied by address-rotation (8.14 multi-buffering, planned), which rings each recurring tensor across *N* buffers and rewrites the per-iteration address as a `(iv mod N)·stride` modulo expression, so the two iterations land in distinct buffers and the cut edges never need to come back.

### The CoreBarrier Stage Divider

`insertElement<InstCoreBarrier>` @0x15c2d68 splices a barrier (opcode 87, "gen3+") into the instruction list immediately before the first compute instruction. The barrier's engine vector (`bar+0x140`, built from the function's engine count `F+0x204`) makes it fence **all** engines of the function — it is not a single-engine fence. Two `addDependency` loops, both with `mov edx,0x4` (Flow) immediately preceding the call (@0x15c2ea3, @0x15c2fd4), wire the fence:

- `barrier.addDependency(L, Flow)` for every stage-0 load — the barrier waits for all loads.
- `compute_use.addDependency(barrier, Flow)` for every stage-1 operand, after re-binding its access pattern via `PhysicalAccessPattern::cloneToArgument` @0x15c2f78 — every compute consumer waits for the barrier.

> **QUIRK —** every edge this pass adds is Flow (`EdgeKind=4`), the strongest kind (the model is `Invalid0 < Ordered1 < Anti2 < Output3 < Flow4`, and a doubled edge keeps the max). A reimplementer tempted to use Ordered or Anti edges for the fence will produce a barrier the scheduler is allowed to reorder around. The fence must be Flow to be a hard cut.

### The with_memset Variant

When `withMemset` (`obj+0x2c1`) is set, the target is **indirect loads** (gather, opcode 0x2B). A gather reads an index tensor in SBUF to compute its source address; hoisting it into stage 0 (one iteration early) would read stale indices if the index producer was a compute-stage instruction. The fix synthesises a zero-init: build an `InstMemset` (opcode 10) named `memset_<tensor>`, covering the whole index tile, and Flow-dep it before the gather (re-attach @0x15c1686, after the predecessor cut). Selection requires the index `MemoryLocation` be **SB-resident** (`memloc+0xd8 == 0x10`, check @0x15c11ba) — DRAM/PSUM index tensors are skipped ("Skipping index writer"). Because inserting the memset perturbs program order, `run()` re-derives the order with a second `get_inst_in_order` @0x15c1187 (the only second call site, reached solely on this path) and re-enters the classify loop. This mirrors the PSUM→SBUF drain + zero-init that bootstraps a software-pipelined accumulation in the NKI blockwise-MoE pattern.

### The post_ada Variant

When `postAda` (`obj+0x2c0`) is set, `run()` branches at @0x15bf37b straight to @0x15c15de, skipping the entire base body, and calls a per-function worker @0x15be280 ("Removing deps for …"). "post_ada" = the arch level past Ada (core_v4/gen4, where the gen3+ CoreBarrier is legal). This variant runs at pipeline order #105 — *after* `post_sched` and `dep_opt`, once the schedule and the final stage count are fixed — and again post-link. Where base inserts a barrier, post_ada performs the **physical hoist**: an ilist splice (@0x15c1b10–0x15c1b32) unlinks each qualified load and re-links it before the first non-load anchor, sets the new parent BB at `L+0x50`, and re-keys the symbol table via `insertIntoSymboltable` @0x15c1ccf. It logs "Moving <L> before the first non-load instruction" and "Removed <N>". The dependency surgery (cut + Flow re-wire) is the same as base. The splice, the symbol-table re-key, and the move logs are all read directly; how the first-non-load anchor is chosen inside @`0x15be280` is inferred from the shared string set rather than walked instruction by instruction.

> **NOTE —** the base/`with_memset` passes (#89–90, pre-schedule) only *mark* the split so the scheduler can overlap; `_post_ada` (#105, post-schedule) finalises it physically. This two-phase placement mirrors address-rotation: the earlier appearance prepares, the later appearance materialises. The prologue fill comes from the hoist, steady-state overlap is the scheduler's, and the epilogue drains naturally (the last iteration's loads have no consumer compute and are dead-code-eliminated).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `SeparateLoadAndCompute::run(Module&)` | 0x15bf170 | dispatcher + base classify/cut/barrier | CERTAIN |
| `SeparateLoadAndCompute::SeparateLoadAndCompute(PassOptions&,bool,bool)` | 0x15c3e30 | ctor; stores bools @0x2c0/0x2c1 | CERTAIN |
| post_ada per-fn worker ("Removing deps for") | 0x15be280 | physical hoist dep clearing | HIGH |
| memset factory (with_memset) | 0x15bf020 | builds `memset_<idx>` InstMemset | CERTAIN |
| reg lambdas base / post_ada / with_memset | 0x15c4760 / 0x15c49b0 / 0x15c4880 | variant registration | CERTAIN |
| `Instruction::removeDependency` (plt) | 0x5f5ae0 | the CUT | CERTAIN |
| `Instruction::addDependency` (plt) | 0x5eff00 | Flow=4 re-wire (×7) | CERTAIN |
| `insertElement<InstCoreBarrier>` (plt) | 0x61bf40 | stage divider insert | CERTAIN |
| `get_inst_in_order` (plt) | 0x62a640 | program-order capture | CERTAIN |

### Key BIR Constants

| Constant | Value | Meaning | Confidence |
|---|---|---|---|
| `Instruction+0x58` | u32 | opcode (InstructionType) | CERTAIN |
| Load / DMACopy | 0x13 / 0x20 | LOAD class → stage 0 | CERTAIN |
| IndirectLoad | 0x2B | gather; with_memset target | CERTAIN |
| Memset | 0x0A | inserted by with_memset | CERTAIN |
| CoreBarrier | 87 | inserted stage divider (gen3+) | CERTAIN |
| EdgeKind Flow | 4 | every edge this pass adds | CERTAIN |
| `memloc+0xd8` == 0x10 | SB | SBUF-resident gate (with_memset) | CERTAIN |
| bools @ `obj+0x2c0/0x2c1` | postAda / withMemset | variant select | CERTAIN |

---

## Prefetch Scheduling

### Purpose

`prefetch_scheduling` decides which loads are eligible to be prefetched across the load/compute boundary and pins their order, so the separate-load split has well-formed candidates to overlap. One C++ class (`PrefetchScheudling` — the TU typo `Scheudling` is the real symbol name) backs both registered passes (`_before_sched`, `_after_sched`); they differ in exactly one boolean. The pass governs two things: collective/DMA prefetch alignment (`cc_dma_alignment`, always runs) and forced incoming-order enforcement for large tensors (`force_incoming_order`, gated). Critically, it sets prefetch *intent and legality* — which loads, what order, which engine boundary — and delegates the cycle-level overlap entirely.

### Entry Point

```text
PrefetchScheudling::run(Module&)            @0xb66d90  (464 B)
  ├─ cc_dma_alignment(Module&)              @0xb64810  ── ALWAYS runs (gate opts+172 mode)
  └─ [opts+176 >= 0] force_incoming_order(Module&)  @0xb651e0  ── gated by force-prefetch knob
       └─ sub_B64CC0                        @0xb64cc0  ── AP-argument-set dedup (×4)
```

The two register-generator lambdas build a 112-byte object with an identical vtable, differing in exactly two writes:

| Generator | `obj+104` | name string | Address |
|---|---|---|---|
| before-sched | 0 | `prefetch_scheduling_before_sched` | @0xb67940 |
| after-sched | 1 | `prefetch_scheduling_after_sched` | @0xb675e0 |

`obj+104` (= `*((u32*)this+26)`) is the `after_sched` flag, read *inside* the workers — there is no second algorithm, the flag toggles which dependency-map path and which alignment sub-mode the otherwise-identical walk takes.

### Algorithm

```c
function PrefetchScheudling_run(Module& M):                  // 0xb66d90
    push_log_attr("module_name", M.getName())                // diagnostics only
    cc_dma_alignment(M)                                       // 0xb66e8f — ALWAYS
    opts = this[96]                                           // stored PassOptions*
    if (int)opts[0xB0] >= 0:                                  // mov eax,[rax+0xb0]; js @0xb66ea0
        force_incoming_order(M)                               // 0xb66ea8 — gated
    return ""                                                 // BackendPass status
```

The gate is `opts+0xB0` (176), the `force-prefetch-follow-incoming-order` size threshold (CLI string @0x1dc25c8, help: *"Force prefetch of tensor larger than set <size> byte to following incoming order, avoid out of order prefetch. Default value -1, off."*). The default −1 (`< 0`) skips `force_incoming_order`; `cc_dma_alignment` still runs. The disassembly shows the gate exactly: `call cc_dma_alignment` (0xb66e8f), `mov eax,[rax+0xb0]` (0xb66e98), `js` to skip (0xb66ea0), `call force_incoming_order` (0xb66ea8).

`cc_dma_alignment` (@0xb64810, gate `opts+0xAC` = `cc-dma-alignment-mode`) is the collective-side prefetch governor. Mode 0 ("prefetch as much as possible") returns immediately — no edges. Mode 1 renumbers program order, then for each `CollectiveCompute` (opcode 48, discriminator `+240 == 2`) finds its `Save`-anchor and Flow-pins the DMA loads that feed it. Mode 2 (only in the after-sched run, gated `this+104 == 1`) does the inverse — keeps the prefetch DMAs from crossing the collective. Every edge is Flow (`mov edx,4` @0xb64c52/0xb64c97).

`force_incoming_order` (@0xb651e0, 374 blocks) re-stamps a fresh sequential program index (the "incoming order"), builds a producer map for each large SBUF tensor (class `loc+224 <= 1`, footprint `loc+264 × loc+256 >= threshold` = rows × bytes-per-row), then forces Flow edges so each big DMA load follows incoming order. The before path just records loads in a forward `DenseMap` (intent/reservation); the after path resolves consumer→producer and emits the cross-engine Flow edge — but only under a legality guard:

```c
// force_incoming_order Phase 3 legality (line 750/1136):
if producer[0x90] != consumer[0x88]:        // DIFFERENT engines
    load.addDependency(producer, Flow=4, …)  // 0xb659b6 / 0xb65c21
```

The guard adds the edge only when producer and consumer live on different engines (`Inst+0x90` = engine field) — precisely the load→compute boundary that prefetch needs to cross. Same-engine pairs are already serialized.

### The Prefetch Distance is Structural

> **GOTCHA —** the prefetch distance is **not** computed from DMA latency. A reimplementer who expects `prefetch_scheduling` to derive "issue the load *N* cycles ahead" from a cost model will look for a calculation that does not exist. The entire prefetch translation unit — `run`, `cc_dma_alignment`, `force_incoming_order`, `sub_B64CC0` — contains **no** `getLatency`/`Hwm`/cost-model call. Sweeping every instruction in `0xb64810`–`0xb67ca0` turns up no such call. The distance is expressed structurally, three ways:

1. **The lookahead window is the BB live-range.** Phase 3 of `force_incoming_order` walks within a basic block until a `Save`/`Collective` closes the live window. There is no numeric "N cycles ahead" — the window is the dependency live-range between a tensor's producing DMA and its consuming compute.

2. **The iteration distance is "one iteration ahead", carried as a constraint attribute, not computed here.** The string `prefetch_one_iteration_ahead` (.rodata @0x1c89a14) is consumed by a BIR-JSON deserializer (`from_json(…, ConstraintOp&)`, region @0x16efc61), **not** by this pass. The loop-pipelining hint is set upstream (Penguin/NKI) and read by the loop/dependency machinery — that is what turns the prefetch into a 1-iteration-ahead software pipeline, rather than any arithmetic in this translation unit.

3. **The size knob decides eligibility, not distance.** `opts+0xB0` (default −1 = off) selects *which* tensors get forced incoming order (footprint ≥ threshold) — only large tiles worth multi-buffering. Small tensors are left to the scheduler's discretion. The knob gates membership, never a cycle count.

So `prefetch_scheduling` sets prefetch intent and legality and delegates the actual cycle-level overlap to the list scheduler ([8.11](post-sched-schedulers.md)) and address-rotation. It does not allocate buffers either: it selects large SBUF-resident tensors and Flow-pins their load order; the multi-buffers are created later by address-rotation, which rings each such tensor and rewrites its address as a `(iv mod N)·stride` modulo expression. The prefetch pass marks the candidates; rotation rings them.

### before vs after, in one line

`before_sched` marks intent and reserves order — it picks large tensors, stamps incoming order, and records their loads in a forward `DenseMap`. `after_sched` *places* the prefetched DMAs into the now-finalized schedule by resolving consumer→producer in that map and emitting the cross-engine Flow edges that lock each load ahead of its compute consumer, plus the `cc_dma_alignment` mode-2 pass that keeps prefetch from drifting across collectives. Same class, one flag (`obj+104`).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `PrefetchScheudling::run(Module&)` | 0xb66d90 | dispatcher; gate `opts+0xB0 >= 0` | CERTAIN |
| `PrefetchScheudling::cc_dma_alignment(Module&)` | 0xb64810 | DMA↔Collective Flow-pin; mode {0,1,2} | CERTAIN |
| `PrefetchScheudling::force_incoming_order(Module&)` | 0xb651e0 | incoming-order enforcer; large-tensor select | CERTAIN |
| `sub_B64CC0` | 0xb64cc0 | AP-argument-set dedup/compaction (×4) | CERTAIN |
| before / after reg invoke | 0xb67940 / 0xb675e0 | obj+104 = 0 / 1 | CERTAIN |
| `prefetch_one_iteration_ahead` (.rodata) | 0x1c89a14 | upstream constraint attr (not read here) | HIGH |
| `Instruction::addDependency` (plt) | 0x5eff00 | Flow=4 edges @0xb659b6/0xb65c21 | CERTAIN |

### Related Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `force-prefetch-follow-incoming-order` | INT (bytes) | **−1 (off)** | Force prefetch of tensors larger than `<size>` to follow incoming order; gate `opts+0xB0` |
| `cc-dma-alignment-mode` | INT {0,1,2} | (per policy) | 0 = prefetch as much as possible; 1 = prefer DMA/CC alignment; 2 = avoid (after-sched only) |

---

## How They Compose

Backend software pipelining is emergent — these two passes plus the scheduler and address-rotation realise it together:

```text
(a) Unroll / loop reshaping                 ── expose cross-iteration parallelism (upstream)
(b) prefetch_scheduling_before_sched        ── mark intent: pick large SBUF tensors,
                                                stamp incoming order, reserve forward map
(c) separate_load_and_compute[_with_memset] ── CUT load↔compute edges + insert CoreBarrier;
                                                with_memset zero-inits gather indices
(d) post_sched (the list scheduler)         ── actually overlap load(i+1) with compute(i)
(e) coloring_allocator_sb → address_rotation_sb ── ring SBUF tiles, (iv mod N) ModuloExpr
(f) prefetch_scheduling_after_sched         ── place prefetched DMAs: resolve consumer→producer,
                                                emit cross-engine Flow edges; cc mode-2
(g) separate_load_and_compute_post_ada      ── physically hoist loads (core_v4, post-schedule)
```

`prefetch_scheduling` decides *which* loads cross the boundary and *in what order*; `separate_load_and_compute` builds *the boundary itself* (cut + barrier); the list scheduler overlaps; address-rotation gives the overlapping iterations distinct physical buffers so the severed edges never reappear. None of the four computes a latency-based distance — the distance is the structural one-iteration-ahead window throughout.

## Related Components

| Name | Relationship |
|---|---|
| `post_sched` (three schedulers) | Consumes the cut + barrier; actually overlaps load(i+1) with compute(i) |
| address-rotation (multi-buffering) | Rings the stage buffers `(iv mod N)·stride` so cut edges stay severed |
| `order_column_tiled_mms` | Runs between separate-load (#89-90) and post_sched in the phase-4 pipeline |
| `dead_code_elim_o0` | DCEs the epilogue's consumer-less last-iteration loads |

## Cross-References

- [post_sched and the Three Schedulers](post-sched-schedulers.md) — the list scheduler that materialises the load/compute overlap once this pass cuts and fences
- [NeuronCodegen Tiling Internals](../nki/neuroncodegen-tiling.md) — the NKI tiling layer that sets the upstream one-iteration-ahead loop-pipelining intent
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the SBUF tile geometry whose tensors are the prefetch and multi-buffering candidates
