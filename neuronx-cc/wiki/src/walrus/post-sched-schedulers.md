# post_sched and the Three Schedulers — greedy, TimeAware, LncAware

`post_sched` (registered pass `PostSched`) is the post-register-allocation
instruction scheduler in the libwalrus backend. It runs *after* PSUM/SBUF
coloring, after the flow-dependence build, and after anti-dependence analysis,
so unlike the pre-allocation ordering pass it schedules over **physical access
patterns** with the full flow/anti/output edge set, and it is allowed to consume
real hardware latencies. The pass is not one scheduler but **three behind one
entry point**, selected by the compile policy that maps to an internal *level*:
levels 0/1/2 run a classic **greedy per-engine list scheduler**, level 3 runs a
cycle-accurate **TimeAware** simulation scheduler that drives a per-engine clock
off the PerfSim cost model, and — only when more than one Neuron core is in play
— an **LncAware** scheduler runs N TimeAware simulators in lock-step to align
cross-core barriers. This page documents all three, the per-engine ready-list
model they share, the TimeAware→PerfSim coupling, the LncAware lock-step loop,
and the `LNC=1` gate that disables the multi-core path. All addresses are real
function bodies in `libwalrus.so` (neuronx_cc 2.24.5133.0+58f8de22, cp310),
verified against the retained `.dynsym` (`nm -DC`) — the `0x5e…/0x5f…` band is a
`.plt` thunk region; the bodies live in the `0xc15…–0xc75…` `.text` range.

## Key facts

| Item | Value | Tag |
| --- | --- | --- |
| Pass class | `neuronxcc::backend::PostSched` (BackendPass subclass) | CERTAIN |
| Single-module entry (LNC=1) | `PostSched::run(bir::Module&)` @0xc3fc90 | CERTAIN |
| Multi-module entry (LNC dispatch) | `PostSched::run(vector<unique_ptr<Module>>&)` @0xc41c10 | CERTAIN |
| Worker (levels 0/1/2 + lvl-3 driver) | `post_scheduler` (ctor @0xc15740) | CERTAIN |
| Module driver | `post_scheduler::schedule(Module&,…)` @0xc39920 | CERTAIN |
| Per-function level dispatch | `post_scheduler::schedule(Function&,int,…)` @0xc36010 | CERTAIN |
| Greedy list scheduler | `post_scheduler::schedule_block(BasicBlock&)` @0xc2b3b0 | CERTAIN |
| Cycle-accurate scheduler | `TimeAwareScheduler` (ctor @0xc549b0) | CERTAIN |
| Multi-core scheduler | `LncAwareScheduler` (ctor @0xc6d7a0, `schedule` @0xc70b90) | CERTAIN |
| Multi-core gate flag | `cl::opt<bool> "lnc-aware-scheduler"`, **default false** | CERTAIN |
| LNC core-count field | `PassOptions+0x1A4` = `lnc_size` (default 1) | CERTAIN |
| Policy field | module options `+0xE0` (policy) / dispatch `+272` | CERTAIN |
| Output snapshot | `bir.scheduled.after-ooo.json` | CERTAIN |
| Autotuner reward (lvl 3) | `addMetric(42 = PostSchedEstLatency)` | CERTAIN |

The dynsym retains the fully-decorated type of the level-3 ready list and the
per-inst record, which removes any guesswork about the data structures:

```text
TimeAwareScheduler::ready_list_find(
  std::vector< std::map< unsigned long,
                         std::set<bir::Instruction*, ReadyInstCmp> > >&,
  bir::Instruction*)                                   @0xc51300
std::vector<TimeAwareScheduler::InstMetrics>::_M_default_append(...)   @0xc66d30
std::vector<TimeAwareScheduler::Candidate>::_M_realloc_insert(...)     @0xc73900
```

The ready list is therefore a `vector< map<readyCycle, set<Instruction*,
ReadyInstCmp> > >` **indexed per engine** — a per-engine timeline bucketed by the
simulated cycle at which each instruction becomes ready.

## Pipeline position and registration

`post_sched` is registered through `register_generator_post_sched__` @0x3e00759,
and a companion `MemoryAnalysisAfterPostSched`
(`register_generator_memory_analysis_after_post_sched__` @0x3e01f89) recomputes
SBUF/PSUM pressure immediately afterward. The pass appears **twice** in the
backend execution order — the main post-RA schedule, then a re-run after
`separate_load_and_compute` / `order_column_tiled_mms` / `arch_legalize` have
finalized the pipelined loop body. A second companion,
`address_rotation_psum_post_schedule`
(`register_generator_address_rotation_psum_post_schedule__` @0x3dff023), runs
*after* `post_sched` to realize PSUM multi-buffering of the loop the scheduler
just laid out.

Tuning globals (`.bss`) read by the schedulers:
`postsched_mm_accum_reorder` @0x3e00320, `postsched_cc_dma_priority`
@0x3e00a80, `postsched_dma_cc_contention_factor` @0x3e00900,
`postsched_cc_dma_max_reschedule_num` @0x3e009c0, `dump_postsched_trace`
@0x3e005e0, and the multi-core gate read-back `lncAwareScheduler` @0x3df6800.

Driver log markers in `.rodata` (all byte-present): `"Start PosT ScheD"`,
`"Done  PosT ScheD"`, `"Running LNC-aware post_sched"`, `"Running LNC-aware
scheduler on block "`, `"Time-aware hwm post-sched"`, `"Time-aware simulation
time: "`, and the five LNC-gate reason lines listed below.

## The module driver and per-function level dispatch

`PostSched::run(Module&)` @0xc3fc90 reads the scheduler config off the module
options block (`*((Module*)+12)`): `+272` is the **policy** (the level selector),
`+420` is `numcores`, `+224` is the arch/scheduler-name string, and
`+276/+277/+281/+500` are the balance / sld / lnc-aware / opt flags. If
`numcores > 1` it first canonicalizes cross-core barrier and collective order via
`orderCoreBarriersAndCollectives` @0x1089370, then calls
`post_scheduler::schedule(Module&,…)` @0xc39920.

The module driver legalizes the policy before dispatch: on `Inf1` with
`policy > 2` it forces `policy = 2` (logging `"policy N incompatible with
Inf1"`); the same clamp applies under LIW mode. A string compare on the
scheduler-name argument sets an internal mode field (`a1+80 ∈ {0,1,2}`) selecting
which sub-scheduler the per-function routine dispatches to. For each function it
calls `post_scheduler::schedule(Function&, policy,…)` @0xc36010, marks
`FunctionAttribute #12` (sched-done), and tears down the scheduling-scaffold flow
edges via `erase_flow_dependencies(F)` + `draw_trivial_deps(F)` so the
synchronizer can later rebuild only the surviving cross-engine dependencies.

`post_scheduler::schedule(Function&,…)` @0xc36010 is the ~2300-line dispatcher.
Stripped of logging:

```c
collect_all_blocks(F, this+624);
if (dump_postsched_trace) dump_schedule(F, ...);   // GraphViz/text, gated

// spill guard (mirrors the pre-allocation scheduler):
if (function_has_attribute("<spill>", key=5) == "too many spills") {
    log("Warning: too many spills, skip scheduling");
    draw_trivial_deps(F);
    level = 0;                                      // keep program order
}

// level-downgrade guards (level>1 and not LIW):
if (level > 1 && !is_LIW) {
    if (!F.hasAttribute(3))         { log("Warning 2 ... downgraded to 1"); level = 1; }
    else if (!deps_in_acc_groups(F)){ log("Warning 3 ... downgraded to 1"); level = 1; }
    // both first run sanitize_matmult_stc_bits + determine_mm_anti_deps
}

determine_mm_anti_deps(F, accGrpMap);              // §matmul-accum, @0xc22cc0
post_scheduler::init(F, level);                    // build 160-B/inst data, @0xc2f970
this+653 = using_memsets(F);                       // @0xc32a90
if (level != 3) assert_no_cycles(F);               // 3-color DFS validate
add_ordered_deps_for_matmult_accum_groups(F);      // @0xc26a00

for (BasicBlock B : collected_blocks) {
    assign_engines(B);                             // @0x156f700 — bind each inst→ENG
    switch (level) {
      case 2: if (count_insts(B) == 0) {
                  schedule_block(B);               // greedy list sched, §greedy
                  if (balance) { perform_balance(B); schedule_block(B); }
              } else { log("Warning 6 ... block downgraded to 1"); goto level1; }
              break;
      case 1: level1: sld(B);                       // separate-load + greedy, @0xc10dc0
              if (balance) { perform_balance(B); sld(B); }
              break;
      case 0: /* nothing — RA already linearized program order */ break;
      case 3: schedule_level3(B);                   // TimeAware path, §timeaware
              break;
      default: NeuronAssertion(850, "policy");      // inst_sch.cpp:2686
    }
    if (level != 3) assert_no_cycles(F);
}
```

So the dispatcher is: guards/downgrade → per-inst data → matmul-accum dependence
prep → per-block engine assignment → run the level-appropriate scheduler → (level
3 only) estimate latency and emit the autotuner reward.

## The per-engine ready-list model (levels 0/1/2 — greedy)

`schedule_block(BasicBlock&)` @0xc2b3b0 is the greedy list scheduler. (Hex-Rays
failed on this body; the call sequence is transcribed from disasm @0xc2b3f8.) It
is a textbook critical-path list scheduler with **engine-grouped batching**: each
step commits one anchor instruction, then drains the load/store/matmul sub-lists
per engine so same-engine work issues back-to-back.

```c
init(B);                       // @0xc19fc0 — per-inst program index + flags
init_ready_list(B);            // @0xc285b0 — seed (§ready seed)
while (!ready_list.empty()) {
    I = ready_list.get_first();             // @0xc20490 — best by priority
    insertIntoSymboltable(I);               // EMIT: splice I to schedule tail
    update_ready_list(I);                    // @0xc28660 — release successors
    ready_list.erase(I);                     // @0xc21e70
    if (is_mm(I)) get_rest_of_acc_grp(I);   // @0xc29770 — keep accum chain tight
    pick_loads(I);                           // @0xc28db0
    ready_list.resort_ready_list();          // @0xc17ee0 — re-prioritize on new clock
    while (ready_list.has_loads())    pick_all_per_engine(I);  // @0xc2ab10
    while (ready_list.has_stores())   pick_stores(I);          // @0xc29150
    while (ready_list.has_matmults()) pick_matmults(I);        // @0xc295d0
}
```

**Ready seed.** `init_ready_list(B)` @0xc285b0 inserts every instruction whose
in-block producers are all already emitted (cross-block deps are pre-satisfied),
then `resort_ready_list()`. A node becomes ready when its
remaining-predecessor counter reaches 0.

**Priority = critical-path HEIGHT.** The ready-list comparators
`ready_list_s::cmpCands` @0xc15bc0 / `cmpDelayedCands` @0xc17600 sort primarily by
the instruction *height* computed by `post_scheduler::longest_path` @0xc183e0:

```c
// memoized critical-path height over the descendent dependence set.
int longest_path(Instruction *I, int depth) {
    int *slot = &perInst[160*privId(I) + 56];      // memo cell, sentinel -1
    if (*slot != -1) return *slot;
    int h = 0;
    for (succ in descendents(I)) {                 // I+0xD0+0x270, EdgePtr & ~7
        if (depth <= 999) h = max(h, longest_path(succ, depth + 1));
    }
    return *slot = 1 + h;
}
```

This is the height/critical-path machinery one would expect of a list scheduler;
in this toolchain it lives **here**, not in the pre-allocation ordering pass.
`requireLatencyHiding` @0xc17b60 (an `I→J` predicate) and `update_timings(ENG)`
@0xc17c60 feed the per-engine timing the comparator uses to keep producers ahead
of consumers.

**Successor release.** `update_ready_list(I)` @0xc28660 iterates `I`'s
descendents (a `tbb::concurrent_unordered_set<EdgePtr>` at `Instruction+0xD0+0x258`,
untagged `& ~7`); for each in-block successor it decrements the
remaining-predecessor counter at `perInst[160*privId + 48]`, and on reaching 0
inserts the successor into the ready list.

**Per-engine picking.** `pick_one_per_engine(I)` @0xc2af20 calls
`post_scheduler::pick_one(ENG)` @0xc29910 once per engine, advancing
`update_timings(ENG)` with the engine enum values `{1,2,5,3,4}` = PE, Pool/Vector,
Act, DVE, SP (the data-path engines), gated on `this+162` (engine count) and
`this+654` (LIW interleave). `pick_loads` / `pick_stores` / `pick_matmults` /
`pick_all_per_engine` / `pick_mm_slice` / `pick_delayed_insts` are the per-class
pickers; senior-load handling (`insert_senior_load` @0xc19f70 /
`remove_senior_loads` @0xc18990 / `update_senior_loads` @0xc25a10) issues a load
that feeds many consumers early (a software-prefetch heuristic). **The resource
model is per-engine ready sub-lists plus a per-engine timing word advanced by
`update_timings` — there is no partition-band reservation table** (see the
misattribution note below).

**The Hwm latency the picker consumes.** `get_latency(I)` @0xc18730 returns a
cycle estimate (all `/200`-normalized):

```c
double get_latency(Instruction *I) {
    if (all_aps_are_physical(I) && !psum_bias_flag)
        return (*(I->vptr + 104))(I) / 200.0;       // per-inst bir::Hwm oracle
    if (is_load_or_save(I))
        return (outMLoc(I).bytes * elemsPerPart / 2000.0 + base) / 200.0;
    if (op_type(I) == 32 /*matmul*/)
        return (outMLoc(I).bytes * elems / 300.0 + base) / 200.0;
    if (op_type(I) == 48 && kind(I) == 3 /*collective-compute*/)
        return (getTotalAccessedBytes(I) * sqrt(numAPs(I)) * this[165] / 400.0
                + 10000.0) / 200.0;
}
```

This consumption of Hwm latencies and byte-size formulas is the defining contrast
with the pre-allocation scheduler, which has no `getLatency` call anywhere.

`sld(BasicBlock&)` @0xc10dc0 (levels 0/1) is the block-local "separate load and
compute" helper that splits the block's loads from its compute so the greedy
scheduler can overlap DMA loads with the previous tile's compute. It is the
block-local twin of the module-level `SeparateLoadAndCompute` pass.

## Matmul accumulation groups — the post-RA-specific concern

After PSUM coloring, multiple matmuls accumulating into the **same PSUM bank**
form an accumulation group that must stay strictly ordered (read-modify-write
into the bank). This is the chief reason a separate post-allocation scheduler
exists, because the groups only become visible once PSUM banks are physical:

- `determine_mm_anti_deps(F, accGrpMap)` @0xc22cc0 builds
  `DenseMap<MemoryLocation*, set<acc_grp>>` — per PSUM bank, the ordered set of
  matmuls — and adds the anti/output (WAR/WAW) edges that serialize accumulation.
- `add_ordered_deps_for_matmult_accum_groups(F)` @0xc26a00 stamps ORDERED edges
  (EdgeKind=1) so the scheduler cannot interleave foreign work into the middle of
  an accumulation chain.
- `get_rest_of_acc_grp(I)` @0xc29770 pulls the rest of a matmul's accumulation
  group when the greedy scheduler picks one, so the whole chain emits contiguously.
- `deps_in_acc_groups(F)` @0xc2e230 is the level≥2 gate (no accum-group deps →
  downgrade to level 1, "Warning 3").
- `set_last_matmults(F)` @0x1583540 + `sanitize_matmult_stc_bits` @0xc32e50 fix
  the matmul start/stop-accumulate (STC) bits for the chosen order.

## Level 3 — TimeAwareScheduler (cycle-accurate simulation)

The level-3 scheduler is an **event-driven list scheduler that maintains a
simulated clock and issues instructions at their earliest feasible cycle**, with
costs supplied by the PerfSim cost model (cross-referenced; not re-derived here).

**Object layout** (ctor @0xc549b0, args `Logger&, bool, bool`). The scheduler
embeds a `PerfSim` at `this+0x48` (built via `PerfSim::PerfSim(bool,float,bool,
bool,Logger&)` @0x5e9630), whose cost mode is selected by the global
`ml_model_directory` string (when set → `MLInstructionLatencyModel`; else the
TrainiumHwm analytical model). The per-inst record is a 160-byte `InstMetrics`
(type confirmed in dynsym):

| Offset | Field |
| --- | --- |
| `+0x28` | scheduled-start time |
| `+0x30` | secondary tiebreak (program/anchor index) |
| `+0x48` | `longest_path` / critical-path **height** (primary priority key) |
| `+0x4c` | engine / opcode class |

The ready list is the per-engine `vector< map<readyCycle, set<Instruction*,
ReadyInstCmp> > >` already established above, with helpers `ready_list_find`
@0xc51300, `ready_list_insert_by_eng_start` @0xc55dc0,
`ready_list_remove_by_eng_start` @0xc56c60, `update_ready_time_in_ready_list`
@0xc59560.

**Setup.** `init(B, InstCostMode, archname)` @0xc5e2a0:
`assign_private_ids(F)` @0xc51980 (dense id → `InstMetrics` index);
`khan_topo_sort(B)` @0xc585e0 (**Kahn's in-degree algorithm** is the production
seed path — `df_topo_sort` @0xc57d60 is the DFS debug/alt variant);
`replace_generic_with_lowering_choices(B)` @0xc5cf20 + `lower_hwdge_triggers(B)`
@0xc5c580 (emit each generic DMA as a set of trigger-engine lowering choices);
`mark_as_ready(I)` @0xc563e0 (seed the per-engine ready buckets by engine start
time); and prime the embedded PerfSim with the chosen `InstCostMode`.

**The simulation loop.** `schedule_basicblock(B)` @0xc63150, gated by
`"Searching for candidate: scheduled_insts=<N> =total_insts"`:

```c
while (scheduled_insts < total_insts) {
    cand = pick_candidate();                 // @0xc61920 — priority core
    if (!cand.has_value()) {                 // frontier fallback
        force one ready via mark_as_ready(...);          // @0xc563e0
        start = PerfSim::inst_start_time(cand);
        else NeuronAssertion(872, "cand.inst != nullptr"); // time_aware_sched.cpp:1379
    }
    if (is_cc_op(cand) && this+594)          // CC/DMA reschedule enabled
        reschedule_cc_related_inst(cand);    // @0xc577d0 — try/revert retiming
    dur = schedule_inst(cand, clock);        // @0xc5b6e0
        // transferBeforeImpl + insertIntoSymboltable → splice cand to schedule tail;
        // record cand engine end-time in InstMetrics; clock += dur;
}
delete_unselected_generic_insts();           // @0xc51880 — drop unused lowerings
// log "[Scheduling trigger engine] Schedule <inst> to <engine>" per inst
```

The thin public wrapper that runs this for one block and materializes the ilist
is `linearize_insts_by_simulation(BasicBlock&)` @0xc611b0.

**The priority core.** `pick_candidate()` @0xc61920, for each engine's ready
bucket: `cost = PerfSim::get_cost(I)` @0x165ba70; skip `I` if
`check_skip_due_to_register_pressure(engineOf(I))` @0xc56630 reports overflow;
keep the arg-best by `is_better_instruction(I, t_I, best, t_best)` @0xc61500. DMA
engines are additionally gated by `PerfSim::is_dma_queue_ready` @0x16590a0 (a DMA
cannot issue before its trigger slot is free). On commit,
`update_reg_pressure_for_candidate(eng)` @0xc56960.

The register-pressure throttle `check_skip_due_to_register_pressure(int eng)`
@0xc56630 uses a `DenseMap<bir::EngineType,int>` (a per-engine live-register
budget) plus `bir::isDataPathEngine`; if issuing `I` on its engine would exceed
the budget, `I` is deferred this step. This is the throttle the greedy levels
0/1/2 lack.

**The byte-true comparator order.** `is_better_instruction(a, t_a, b, t_b)`
@0xc61500 compares in this order (disasm-verified — *height first*):

```c
// disasm @0xc61533: mov 0x48(rbx),rcx ; mov 0x48(rax),rax ; cmp rax,rcx
// disasm @0xc6155a: cmp rdx,r15   (start-time tiebreak)
bool is_better_instruction(a, t_a, b, t_b) {
    if (a.height(+0x48) != b.height(+0x48))      // 1. CRITICAL-PATH HEIGHT
        return a.height > b.height;              //    larger height wins
    if (t_a != t_b)                              // 2. EARLIEST SIMULATED START
        return t_a < t_b;                        //    earlier cycle wins
    if (psum_aware_mode this+68)                 // 3. PSUM-BANK AFFINITY
        return banks(a) intersect held set       //    get_psum_reader_writer_banks
            better than banks(b);                //    @0x6099c0 — fewer switches
}
```

So the level-3 priority is **{height → earliest issue cycle → PSUM-bank
affinity}**. (A latency-hiding / busier-engine effect is emergent from the
per-engine ready buckets and PerfSim cost; it is not the first comparator key.)

The `set<>` ordering key `ReadyInstCmp::operator()` @0xc51270 reads the global
`preserve_incoming_order` (@0x3dc2590) at field `+0x78` (disasm @0xc5129d): if
`> 0` it orders by `Instruction+0x28` (scheduled-start, smaller first — preserve
incoming order); else by `Instruction+0x48` (height, larger first), then `+0x30`.

**HWDGE trigger-engine choice.** `is_better_trigger` @0xc51590 +
`delete_dma_triggers` @0xc51710 select, among a DMA's lowering choices, the
trigger engine that imposes the least wait, deleting the rest (gated by
`enable-hwdge-trigger-engine-scheduling`). `reschedule_cc_related_inst` @0xc577d0
is a try/revert retiming (`revert_schedule_inst` @0xc572d0 + remove/re-insert) of
collective-compute / DMA neighbours to relieve queue contention, tuned by
`postsched_dma_cc_contention_factor` / `postsched_cc_dma_max_reschedule_num` /
`postsched_cc_dma_priority` and active when `this+594` is set. This is the
single-core sync-aware retiming.

`TimeAwareScheduler` adds over the greedy scheduler: a real per-engine simulated
clock driven by PerfSim (Hwm or ML), the register-pressure DenseMap throttle,
DMA-queue / HWDGE trigger-engine modeling, the PSUM-bank-affinity tiebreak,
CC/DMA try-revert retiming, and the `PostSchedEstLatency=42` autotuner reward.

## Level 3 — the multi-core LncAware path and the LNC=1 gate

`PostSched::run(vector<unique_ptr<Module>>&)` @0xc41c10 is the multi-module entry.
It chooses between the cross-core `LncAwareScheduler` and the per-module LNC=1
fallback. The gate is **byte-verified** in the disasm of this body — it falls
back to the LNC=1 per-module scheduler when **any** of these hold, logging the
reason:

| Condition (disasm) | Meaning | Log string |
| --- | --- | --- |
| `cmpl $0x1, 0x1a4(%rdx)` @c41c2f | `lnc_size == 1` (`PassOptions+0x1A4`) | `Detected LNC=1; running LNC=1 post_sched` |
| `cmpl $0x3, 0xe0(%rdx)` @c41c4a | `policy != 3` | `Detected policy != 3; running LNC=1 post_sched` |
| `cmpq $0x2e/$0x3e, 0x88(%rbx)` | loop/terminator opcode present | `Detected loops; running LNC=1 post_sched` |
| `cmpb $0x0, 0x400(%rsp)` @c41d1d | `--lnc_aware_scheduler=false` | `Detected --lnc_aware_scheduler=false; running LNC=1 post_sched` |
| modules.size() == 1 | single module | `Detected modules.size() == 1; running LNC=1 post_sched` |

Only when **policy 3 AND loop-free AND lnc-aware ON AND `lnc_size > 1` AND more
than one module** does it log `"Running LNC-aware post_sched"`, collect one
`BasicBlock` per core, build `LncAwareScheduler(vector<BasicBlock*>, Logger)`
@0xc6d7a0, and call `.schedule()` @0xc70b90 (logging `"Running LNC-aware scheduler
on block "`).

*Anchors: all five reason strings and the four compare constants are byte-present in
`0xc41c10`.*

**The `lnc-aware-scheduler` flag defaults to false.** The option is a standard
LLVM `cl::opt<bool>` (`setArgStr("lnc-aware-scheduler")`, help `"Run LNC-aware
scheduler."`); its `Value` word at object `+0x98` is explicitly zeroed
(`pxor xmm0,xmm0 ; movups xmm0,0x98(r13)` in the registrar) and there is no
`init(true)` modifier, so the default is 0. Its read-back lives in BSS
`lncAwareScheduler` @0x3df6800. Multi-core LNC-aware scheduling is therefore
**opt-in**: absent the flag, every multi-LNC module is post-scheduled by the
LNC=1 path, and cross-core sync ordering is left to the barrier/semaphore
lowering passes rather than the scheduler.

**`LncAwareScheduler` construction.** The ctor @0xc6d7a0 builds N per-core
`TimeAwareScheduler`s (one per `BasicBlock`), each pushed into a
`vector<unique_ptr<TimeAwareScheduler>>` and `init()`-ed on its core's block, and
constructs the cross-reference maps that index `CoreBarrier`s by NeuronCore id
(`coreBarriers[ncId][index]`).

*Anchors: dynsym plus the `M_realloc_insert` of `unique_ptr<TimeAwareScheduler>` @0xc73aa0.*

**Sync-candidate identification.** Each global step asks each core for two kinds
of candidate:

- `get_non_lnc_sync_candidates()` @0xc6d700 — fully decoded — loops over the
  cores and calls each core's `pick_non_lnc_sync_candidate()` @0xc64b50 (a
  `pick_candidate` restricted to *non* cross-core-sync ops), collecting **one
  core-local candidate per core**.
- `get_lnc_sync_candidates()` @0xc6ec20 — identifies the cross-core
  synchronization candidates (`CoreBarrier` / `AllEngineBarrier`). For a barrier
  picked on one core it locates the **matching barrier on every other core by
  name** via `bir::BasicBlock::getInstructionByName`, populating
  `lnc_sync_candidates_per_core[ncid]`. An LNC-sync candidate is thus a barrier op
  that has a same-named instance on every core and must fire at the same logical
  step on all cores. *Anchors: the `cb_candidate` and `ncid` symbols plus the
  `coreBarriers[ncId][index]->getName()` assert strings.*

**The lock-step loop.** `LncAwareScheduler::schedule()` @0xc70b90:

```c
while (scheduled_insts < total_insts) {
    lnc = get_lnc_sync_candidates();         // @0xc6ec20 — cross-core barriers
    non = get_non_lnc_sync_candidates();     // @0xc6d700 — one local cand per core

    // FORWARD-PROGRESS / DEADLOCK guard (lnc_aware_sched.cpp:172):
    NeuronAssertion(!non.empty() || !lnc.empty() || got_terminator);

    // 1. schedule LNC-sync candidates FIRST, on ALL cores at the same step:
    for (core : cores) schedule_inst(core.TAS, barrier_on(core));
    //    → barrier issued simultaneously everywhere → minimal cross-core wait
    //    log "Scheduled LNC sync candidate <name> on all cores"

    // 2. then schedule the core-local candidates by per-core PerfSim time:
    for (core, cand : non) {
        schedule_inst(core.TAS, cand);
        update_reg_pressure_for_candidate(core, cand);
        // log "Scheduled non-LNC sync candidate <name> on core <ncid>"
    }
}
for (core : cores) delete_unselected_generic_insts(core);   // @0xc51880
```

So `LncAwareScheduler` is N concurrent single-core TimeAware simulators sharing a
global step counter: every `CoreBarrier` / `AllEngineBarrier` fires on all cores
at the same step (matched by name/ID), while within each core the ordinary
TimeAware priority (height → earliest cycle → PSUM affinity) still governs the
non-sync instructions. The `:172` assert is a deadlock check — if no core can
advance and no terminator was reached, the cross-core sync graph deadlocked.

For `LNC > 1` the backend additionally inserts PTCOM ops after `CoreBarrier`s and
renumbers core barriers (`renumberCoreBarriers` region, `"Done renumbering core
barriers."`); those are sibling passes, not part of `schedule()` itself.

## Outputs — materialized schedule, snapshot, autotuner reward

Every scheduler ends by **splicing the block's ilist into emit order**: levels
0/1/2 via `insertIntoSymboltable` in `schedule_block`; level 3 via
`transferBeforeImpl` + `insertIntoSymboltable` in `schedule_basicblock`. After
`post_sched`, the ilist *is* the per-engine schedule.

The post-OOO snapshot is dumped as `bir.scheduled.after-ooo.json` (@0x1c81cbe);
`dump_schedule(F)` writes per-core text traces under
`Module::getArtifactAbsPath/<nc??>.txt` when `dump_postsched_trace` is set.

At level 3 the scheduler also produces the autotuner reward: `est =
get_modular_flow_runs(Module) * PerfSim::get_overall_end()` (the loop-trip
multiplier times the simulated makespan), logged as `"Time-aware simulation
time: <est>"` and recorded via `addMetric(42 = PostSchedEstLatency)`
(`POracle::addMetric` @0xd13070). `post_sched` is the producer of that scalar.

## No partition-band reservation table

The resource model of all three schedulers is the per-engine ready-list (greedy)
or per-engine time-keyed ready map plus PerfSim clock (TimeAware), the per-engine
register-pressure DenseMap, the PSUM-bank-affinity sets, and the DMA-queue
dispatch slots — **none of them owns a partition-band reservation table**.

> **GOTCHA —** the `partitionBandReservations` / `reservations[otherPartitionBandIndex]` /
> `"Bad Height"` strings look like a scheduler reservation table. They xref to the
> **graph-coloring SB allocator** (`SB_Allocator::selectNode`, `heightAttributes`) and
> to a prefetch peephole (`OptimizePrefetchActLoadImpl::visitInstruction`), not to any
> scheduler body. The height priority a scheduler is expected to carry *is* here — via
> `longest_path` — but the reservation table belongs to the allocator.

## pre-allocation vs post-allocation scheduler contrast

| Dimension | Pre-allocation scheduler | `post_sched` (this page) |
| --- | --- | --- |
| Pipeline position | before allocation (logical tensors) | after coloring + flow/anti-dep build (physical APs) |
| Edges used | kind-agnostic reachability (flow only) | full flow/anti/output/ordered on physical APs (incl. PSUM-accum WAR/WAW) |
| Priority key | structural unit-weight longest path | latency-weighted height (`longest_path` @0xc183e0) + PerfSim time |
| Latency model | none (no `getLatency`) | yes — `get_latency` @0xc18730 + `PerfSim::get_cost` |
| Resource model | SBUF/PSUM live-byte budget | per-engine ready sub-lists + per-engine PerfSim clock (lvl 3) + reg-pressure DenseMap |
| Engine awareness | none | yes — `assign_engines` + per-engine pick |
| Cross-core / sync | none | `LncAwareScheduler` (coordinated sync candidates) + HWDGE trigger choice |
| Matmul accum groups | not handled (pre-alloc) | `determine_mm_anti_deps` + accum-group ordering |
| Output / reward | log only | `bir.scheduled.after-ooo.json`; `addMetric(42)=PostSchedEstLatency` |

## Function map

| Address | Identity |
| --- | --- |
| 0xc3fc90 | `PostSched::run(Module&)` — LNC=1 entry |
| 0xc41c10 | `PostSched::run(vector<Module>&)` — multi-LNC entry / LNC gate |
| 0xc39920 | `post_scheduler::schedule(Module&,…)` — module driver |
| 0xc36010 | `post_scheduler::schedule(Function&,int,…)` — level dispatcher |
| 0xc2f970 | `post_scheduler::init(Function&,int)` — per-inst data build |
| 0xc2b3b0 | `post_scheduler::schedule_block(BasicBlock&)` — greedy list sched |
| 0xc285b0 | `post_scheduler::init_ready_list(BasicBlock&)` — ready seed |
| 0xc28660 | `post_scheduler::update_ready_list(I)` — release successors |
| 0xc183e0 | `post_scheduler::longest_path(I,depth)` — critical-path height |
| 0xc18730 | `post_scheduler::get_latency(I)` — Hwm/byte latency |
| 0xc29910 | `post_scheduler::pick_one(ENG)` — per-engine pick |
| 0xc15bc0 | `ready_list_s::cmpCands` — greedy priority comparator |
| 0xc22cc0 | `post_scheduler::determine_mm_anti_deps` — PSUM-accum WAR/WAW |
| 0xc26a00 | `post_scheduler::add_ordered_deps_for_matmult_accum_groups` |
| 0xc29770 | `post_scheduler::get_rest_of_acc_grp(I)` — accum-group pull |
| 0xc10dc0 | `sld(BasicBlock&)` — separate-load/compute |
| 0xc549b0 | `TimeAwareScheduler` ctor (embeds PerfSim @+0x48) |
| 0xc5e2a0 | `TimeAwareScheduler::init(BB, InstCostMode, str)` |
| 0xc63150 | `TimeAwareScheduler::schedule_basicblock(BB)` — simulation loop |
| 0xc611b0 | `TimeAwareScheduler::linearize_insts_by_simulation(BB)` |
| 0xc61920 | `TimeAwareScheduler::pick_candidate()` — priority core |
| 0xc61500 | `TimeAwareScheduler::is_better_instruction` — height→start→PSUM |
| 0xc51270 | `TimeAwareScheduler::ReadyInstCmp::operator()` — set ordering |
| 0xc5b6e0 | `TimeAwareScheduler::schedule_inst(Cand,t)` — commit + clock |
| 0xc56630 | `TimeAwareScheduler::check_skip_due_to_register_pressure` |
| 0xc577d0 | `TimeAwareScheduler::reschedule_cc_related_inst` — CC/DMA retime |
| 0xc585e0 | `TimeAwareScheduler::khan_topo_sort` — production seed |
| 0xc6d7a0 | `LncAwareScheduler` ctor — N per-core TimeAware |
| 0xc70b90 | `LncAwareScheduler::schedule()` — lock-step loop |
| 0xc6ec20 | `LncAwareScheduler::get_lnc_sync_candidates()` — cross-core barriers |
| 0xc6d700 | `LncAwareScheduler::get_non_lnc_sync_candidates()` — core-local |
| 0xc64b50 | `TimeAwareScheduler::pick_non_lnc_sync_candidate()` |
| 0x165ba70 | `PerfSim::get_cost(I)` — Hwm/ML latency |
| 0x16590a0 | `PerfSim::is_dma_queue_ready` — DMA dispatch slot |
| 0xd13070 | `POracle::addMetric(PMetric&)` — metric 42 = PostSchedEstLatency |

## Cross-references

- [MinReg scheduler (penguin)](../penguin/scheduling-minreg.md) — the front-end
  ISL/MinReg scheduling pass, a separate scheduling stage upstream of the backend.
- `walrus/dependence-graph` (planned, 8.9) — the dependence graph (flow/anti/
  output edges) this scheduler consumes.
- `walrus/dep-opt-reduction` (planned, 8.10) — the reduced dependence graph that
  feeds the ready-list seed.
- `walrus/` PerfSim cost model (planned, 8.45) — the cycle-cost oracle the
  TimeAware scheduler calls via `PerfSim::get_cost` / `inst_start_time`; the
  source of the `InstCostMode {Hwm | ML-regression}` selection and the
  `PostSchedEstLatency` makespan.

## Evidence Anchors and Limits

Read byte-exact from the cp310 `libwalrus.so`: every scheduler symbol address
(`nm -DC` dynsym); the ready-list, `InstMetrics`, and `Candidate` types, which
survive decorated in the dynsym; the five LNC-gate reason strings and the four
gate compare constants (`cmpl $0x1,0x1a4`, `cmpl $0x3,0xe0`,
`cmpb $0x0,0x400(%rsp)`, and the loop-opcode `cmpq`); the `is_better_instruction`
height-first ordering, with `+0x48` compared before `+0x28`; the `ReadyInstCmp`
`+0x78`/`+0x48`/`+0x28` field reads; and the `cl::opt` default-false markers.

**Limits.** The `schedule_block` body is transcribed from disassembly because
Hex-Rays failed on it, so its exact inner control flow is reconstructed rather than
read statement-by-statement. The `InstCostMode` enum integer values and the precise
opcode identities behind the `+0x88` loop-opcode constants are [INFERRED] from
naming and adjacency.
