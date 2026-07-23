# The Reservation-Table Disambiguation: Scheduler vs Allocator

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical). Every body lives in `neuronxcc/starfish/lib/libwalrus.so` (BuildID `92b4d331…`, 64,973,024 bytes). For `.text` (`0x62d660`+) and `.rodata` (`0x1c72000`+) the virtual address equals the file offset; `.data` carries a +0x400000 delta. The band `0x5e9020–0x62d650` is the `.plt` thunk table — a symbol there is a 6-byte `jmp` stub, not the body; every address below is a **real body** in the high-VA `.text`. Other wheels differ — treat every address as version-pinned.*

## Abstract

Three different passes in the walrus backend each carry something a casual reader will call a "reservation table," and conflating them produces a specific, recurring error: attributing the **partition-band reservation table** to *the scheduler*. It is not the scheduler's. It is the **SBUF coloring allocator's**, and it has nothing to do with instruction issue order, engines, or cycles. This page exists to draw the three boundaries cleanly and to *prove the ownership from the binary* — which pass's code constructs and reads each table — so that a reimplementer never wires the partition-band intersection test into a list scheduler where it does not belong.

The confusion is structural, not careless. SBUF is a **128-partition × byte** 2-D address space ([1.07](../arch/sbuf-psum-geometry.md)), so the allocator that places tensors into it runs a 2-D rectangle-packing intersection test over partition bands. A cycle-accurate scheduler is *also* 2-D — **engine × cycle** — and *also* list-schedules with an intersection-flavoured feasibility test. Two passes, two 2-D resource models, both phrased as "does this thing fit against the already-reserved set." The single word "partition" tips the reader: SBUF has partitions, so the allocator's *partition-band* table reads, at a glance, like a scheduler's *per-engine issue-slot* table. They are different axes in different passes that share no code, no data structure, and no axis.

The decisive evidence is a whole-binary cross-reference: the two assert strings that name the table —

- `"boundIter != partitionBandReservations.end()"` @ `.rodata 0x1cc2110`
- `"intersectionIter != reservations[otherPartitionBandIndex].end()"` @ `.rodata 0x1cc4128`

— have **exactly two** instruction references in the entire 65 MB binary, and *both* are inside `SB_Allocator::selectNode` @ `0xa05250`. No scheduler function references either string. That single fact settles ownership; the rest of the page maps the three resource models so the reader knows what each pass uses *instead*.

For reimplementation, the contract is:

- **Three disjoint resource axes**, one per pass: the allocator's *partition-band × byte* rectangle; pre_sched's *SBUF/PSUM live-byte* budget (no engine, no time); post_sched's *engine × cycle* timeline.
- **The ownership proof**: the partition-band reservation table belongs to `SB_Allocator::selectNode`, proven by the two-and-only-two xrefs to its assert strings and by the `std::array<std::vector<std::pair<int,int>>,4ul>` parameter type that *is* the table.
- **What the schedulers use in its place**: pre_sched has *no* table at all (`run_pre_sched` issues zero engine-binding calls); post_sched's "reservation" is a `PerfSim` `SimEvent{start, cost}` on a per-engine cycle timeline, searched 1-D, not a 2-D rectangle.
- **The negative invariants** a reimplementer must preserve: no partition coordinate in any scheduler body; no engine binding and no latency in pre_sched; no byte-interval `∩` in any scheduler.

| | |
|---|---|
| **The table (assert strings)** | `0x1cc2110` / `0x1cc4128` — **2 xrefs total**, both in `selectNode` @ `0xa06d4b` / `0xa06d6a` |
| **Owner = ALLOCATOR** | `SB_Allocator::selectNode` @ `0xa05250`; the table is its 3rd param `std::array<std::vector<std::pair<int,int>>,4ul>&` |
| **Axis A — allocator** | partition-band × byte (2-D rectangle ∩); placement-time SBUF address assignment |
| **Axis B — pre_sched** | SBUF/PSUM live-**byte** budget; `inst_use/def/kill/gain_size` @ `0xcaede0`…`0xcaf010`; no engine, no time ([8.8](reorder-nonssa-presched.md)) |
| **Axis C — post_sched** | engine × **cycle**; `PerfSim` `SimEvent{start,cost}`; `assign_engines` @ `0x156f700` binds engines |
| **Pre-sched engine calls** | `run_pre_sched` @ `0xca1490` → **0** calls to `assign_engines`/`getEngineType` |
| **Post-sched engine calls** | `post_scheduler::schedule(Function)` @ `0xc36010` → `call assign_engines@plt` @ `0xc36539` |

---

## Why this page exists — the misattribution and its correction

### The claim being corrected

An early reading of this backend described a single **"scheduler partition-band reservation table shared by pre_sched and post_sched for resource-constrained list scheduling,"** with a per-engine issue-slot intersection test and a Hwm-latency-weighted critical path. That description fuses three real things that live in three different passes into one imaginary object. The correction, byte-grounded below, is:

> **The partition-band reservation table is the SBUF coloring allocator's, not any scheduler's.** No scheduler function in the binary owns, fills, or reads it. pre_sched has *no* resource table at all; post_sched's resource model is per-engine cycle timelines, not partition bands.

This is a **DISAMBIGUATION** page, not a re-derivation: the per-pass internals are documented by their own pages — [8.8](reorder-nonssa-presched.md) for pre_sched, the post_sched scheduler family (Part 8.11, planned) for post_sched, and the SBUF allocator (Part 8.17, planned) for `selectNode`. This page's deliverable is the **three-axis ownership table** and the **proof of who owns the partition-band table**.

### The three passes and where they sit

```text
   … 33 pre_sched          ← FIRST scheduler: logical tensors, NO engines, NO table   (Axis B)
      40 linear_scan_allocator
      44 coloring_psum
      51 coloring_sb        ← SB_Allocator::selectNode lives here: the partition-band table  (Axis A)
      …
      92 post_sched         ← cost-model scheduler: engine × cycle PerfSim timeline    (Axis C)
```

The ordering matters for the disambiguation. **pre_sched runs before allocation** (order 33), over logical `MemoryLocation`s that have no SBUF address yet — so it *cannot* reference a partition-band table; there is no partition assignment to reserve against. **`selectNode` runs inside coloring SB allocation** (order ~51), which *is* the act of assigning partition×byte addresses. **post_sched runs after allocation** (order 92), over physical access patterns, and reserves *engine-cycles*, not partitions. The three passes are temporally and structurally separated; the brief collapsed that separation.

---

## The ownership proof — whole-binary cross-reference

### Purpose

To prove a table belongs to pass *X* and not pass *Y*, find every instruction in the binary that touches the table's identifying data (here, the two assert strings the table's bounds-checks emit) and show they all live inside *X*. This is a stronger argument than reading any single function, because it rules out a *second* owner elsewhere.

### The two strings and their offsets

Both strings are present in `.rodata` at the offsets the table's bounds-asserts reference. VA == file offset, so the raw byte offset *is* the load address:

```text
0x1cc2110 : "boundIter != partitionBandReservations.end()"
0x1cc4128 : "intersectionIter != reservations[otherPartitionBandIndex].end()"
```

*Anchors: `rg -abo` over the binary returns these two strings at exactly `30155024 = 0x1cc2110` and `30163240 = 0x1cc4128`. The token `reservation` appears 12 times in total; none of them is a free-standing scheduler C-string — they are mangled `SB_Allocator` symbol fragments plus these two asserts.*

### The two — and only two — references

Disassembling the whole `.text` and counting every `lea …(%rip)` that materializes either VA yields **two** sites, both inside `selectNode`, both feeding `__assert_fail`:

```text
// inside SB_Allocator::selectNode (body 0xa05250..0xa06e20)
a06d4b:  48 8d 3d be b3 2b 01   lea  0x12bb3be(%rip),%rdi   # 1cc2110  (partitionBandReservations)
a06d52:  e8 89 68 be ff         call 5ed5e0 <__assert_fail@plt>
…
a06d6a:  48 8d 3d b7 d3 2b 01   lea  0x12bd3b7(%rip),%rdi   # 1cc4128  (reservations[otherPartitionBandIndex])
a06d71:  e8 6a 68 be ff         call 5ed5e0 <__assert_fail@plt>
```

*Anchors: a whole-`.text` `objdump -d | rg -c '# 1cc2110|# 1cc4128'` returns **2**, at `0xa06d4b` and `0xa06d6a`. No other site in the binary references either VA, and `selectNode`'s body spans `0xa05250..0xa06e20`, so both references sit inside it.*

### The owner symbols are all `SB_Allocator`, none scheduler

`nm -DC` confirms every symbol in the reservation cluster is `neuronxcc::backend::SB_Allocator::*` and that the table's *type* is literally the 4-quadrant partition-band structure:

```text
0xa05250  SB_Allocator::selectNode(Info&, vector<Info> const&,
            std::array<std::vector<std::pair<int,int>>, 4ul>&,   ← THE TABLE: 4 partition bands,
            std::vector<std::pair<int,int>>&,                       each a sorted vector of
            std::unordered_map<Statistics,float>&, bool)            occupied byte-intervals [lo,hi)
0xa06e20  SB_Allocator::selectNodeWithPartnerRetry(… same array<…,4>& …)
0xa021a0  SB_Allocator::collectReservations(array<vector<pair<int,int>>,4>&,
            array<vector<bool>,4>&, uint, Info&, vector<Info> const&)   ← fills the table
0xa02120  SB_Allocator::sortReservations(vector<pair<int,int>>&)
0xa041b0  SB_Allocator::compressReservations(vector<pair<int,int>>&, bool)
0xa04530  SB_Allocator::sortReservationsWithFlags(vector<pair<int,int>>&, vector<bool>&)
```

*Anchors: `nm -DC` demangles all six with the `SB_Allocator::` qualifier; `selectNode`'s 3rd parameter and `collectReservations`'s 1st parameter are both `std::array<std::vector<std::pair<int,int>>, 4ul>`. No scheduler-namespaced symbol (`pre_sched::*`, `post_scheduler::*`, `TimeAwareScheduler::*`, `ready_list_s::*`) appears anywhere in the reservation cluster.*

> **The conclusion is forced.** The only code in the binary that names the partition-band reservation table is `SB_Allocator`; the table's static type *is* the 4-band quadrant structure; and the allocator's TU (`coloring_allocator/.../sb_select.cpp`) is a disjoint subtree from every scheduler TU (`dep_based_optims/`). The brief's "scheduler reservation table" does not exist.

---

## What the table actually is — the allocator's partition-band model (Axis A)

For completeness — so the reader knows what the misattributed object *really* does — here is the allocator's model, the thing the brief mistook for the scheduler's. It is recovered in full on the SBUF-allocator page (Part 8.17, planned); only the shape relevant to the disambiguation is restated here.

A **partition band** is a 32-partition quadrant of the 128-partition SBUF: `[0,32) [32,64) [64,96) [96,128)`, hence `128 / 32 = 4` ([1.07](../arch/sbuf-psum-geometry.md)). For each band, the table holds a **sorted vector of occupied byte intervals** `[lower, upper)`. The two-level structure is exactly the demangled type:

```c
// SB_Allocator::selectNode 3rd parameter — the partition-band reservation table
std::array< std::vector< std::pair<int,int> >, 4 >  reservations;
//          ^one entry per 32-partition band         ^sorted [lower,upper) byte intervals
```

`collectReservations` @ `0xa021a0` walks the already-placed neighbour `Info` nodes and deposits their occupied `[lo,hi)` byte ranges into the band(s) they span — its body is a 4-iteration loop striding both `array<…,4>` parameters in lockstep (`add $0x18,%r14` for the `vector<pair>` slot, `add $0x28,%r13` for the companion `vector<bool>` slot, `cmp`/`jne` @ `0xa022d8` against the array-end sentinel) — and `sortReservations`/`compressReservations` keep each band's vector sorted and merged. `selectNode` then asks whether a candidate fits:

```c
// SB_Allocator::selectNode — the 2-D rectangle ∩ feasibility test (assert strings @0x1cc2110 / 0x1cc4128)
boundIter = lower_bound(reservations[band].begin(), reservations[band].end(), candidate.byteOff);
assert(boundIter != reservations[band].end());                 // 0x1cc2110
for (otherPartitionBandIndex in bands the candidate spans) {
    intersectionIter = walk(reservations[otherPartitionBandIndex]);
    assert(intersectionIter != reservations[otherPartitionBandIndex].end());   // 0x1cc4128
    if (interval_overlaps(candidate.[byteOff, byteOff+size), *intersectionIter))
        candidate_does_not_fit_here();
}
// candidate fits iff [byteOff, byteOff+size) ∩ every reserved interval in every spanned band == ∅
```

This is **placement-time SBUF address assignment**: it decides *where in the 128×SB_SIZE scratchpad a tensor lives*. It is **engine-agnostic** and **time-agnostic** — it never asks *when* or *on which engine* an instruction issues. The axis is **partition-band × byte**; the operation is a **2-D rectangle intersection**.

*Anchors: the type, owner, and cross-references are read directly, as is the 4-band fill loop — `collectReservations` strides the `array<…,4>` in lockstep, `cmp`/`jne` @ `0xa022d8`. The interior `lower_bound`/overlap shape in `selectNode` is reconstructed from the two asserts' control flow rather than transcribed from its 6 KB body, which page 8.17 documents.*

---

## What pre_sched uses instead — the live-byte budget (Axis B)

pre_sched is the **first** scheduler (order 33), and it runs **before** any SBUF address exists. Its full internals are on [8.8](reorder-nonssa-presched.md); the disambiguation-relevant facts are that it has **neither a partition-band table nor any engine model**.

### No engine binding

`run_pre_sched` @ `0xca1490` (body `0xca1490..0xca1cc0`) issues **zero** calls to either engine-binding routine:

```text
// objdump -d run_pre_sched, grep for assign_engines / getEngineType / their PLT VAs:
(no matches)
```

*Anchors: disassembling `0xca1490..0xca1cc0` and grepping for `assign_engines`, `getEngineType`, `0x156f700`, `0x917400` returns nothing. pre_sched produces one logical instruction order per basic block; engine binding is deferred to post_sched (next section).*

### No latency, no reservation — only live bytes

Its critical-path priority is **unit-weight longest path**. `compute_depths_forward` @ `0xc8f610` calls only LLVM `DenseMap` helpers, `__assert_fail`, and **itself** (recursion @ `0xc8f89a`) — no Hwm/latency oracle, no float-cost arithmetic:

```text
// compute_depths_forward body (0xc8f610..0xc8f8f0) — every call target:
c8f630  DenseMapBase<…MemoryLocation…>::at@plt        (adjacency lookup)
c8f760  DenseMap<…>::grow@plt                          (depth map grow)
c8f79f  __assert_fail@plt
c8f89a  compute_depths_forward  (SELF — the recursive longest-path step)
// NO getLatency / Hwm / cvtsi2ss / divss anywhere → depth = +1 per hop, unit weight.
```

Its resource model is the **SBUF/PSUM live-byte budget** — the bytes a basic block holds live, not engine slots and not cycles:

```text
inst_use_size   @0xcaede0   Σ input  memloc bytes (read)
inst_def_size   @0xcaefb0   output memloc bytes (only on first def; else 0)
inst_kill_size  @0xcaeb60   Σ bytes of inputs whose LAST use is this inst (freed)
inst_gain_size  @0xcaf010   = kill − def  (net SBUF bytes freed by scheduling now)
```

*Anchors: the four sizing symbols resolve at the listed addresses, with `inst_gain_size` as the Sethi-Ullman-flavoured pressure heuristic. There is no per-engine slot, no cycle, and no partition coordinate anywhere in the pre_sched/pre_scheduler bodies.*

> **pre_sched is the doubly-empty case.** The brief's premise — a partition-band reservation table *and* an engine-resource model in pre_sched — is absent on both counts. pre_sched has neither.

---

## What post_sched uses instead — the engine × cycle timeline (Axis C)

post_sched is the real cost-model scheduler (order 92), running **after** coloring over physical access patterns. Its internals are on the post_sched scheduler page (Part 8.11, planned); the disambiguation-relevant facts are that it *does* bind engines and that its "reservation" is a per-engine **cycle** timeline, not a partition band.

### It binds engines — pre_sched does not

`post_scheduler::schedule(Function)` @ `0xc36010` calls `assign_engines` per block:

```text
c36539:  e8 b2 aa 9d ff   call 610ff0 <…backend::assign_engines(bir::BasicBlock&)@plt>
```

*Anchors: the call at `0xc36539` targets the `assign_engines` PLT thunk `0x610ff0` → body `0x156f700`, which binds each instruction to a `bir::EngineType` via `getEngineType` @ `0x917400`. This is where the engine-resource model materializes, and it belongs to post_sched — pre_sched makes no such call.*

### Its resource axis is engine, never partition

The scheduler counts engines three congruent ways, none of which is a partition band:

- `backend::ENG` — the greedy ready-list engine selector: `ready_list_s::update_timings(ENG)` @ `0xc17c60` is a **6-case** jump table (`cmp $0x5` → table `0x1de2c3c`); `pick_one_per_engine` @ `0xc2af20` drains the data-path engines `{1 Pool, 2 Act, 5 DVE, 3 PE, 4 DMA}` per round.
- `PerfSim::SimEngineId` — the level-3 simulated roster: `simEngineId2string` @ `0x1657410` dispatches **13 cases** (`cmpl $0xc,(%rsi)` → `ja`) = 6 base engines (Pool/Act/PE/DMA/DVE/SP) + 4 CollectiveCompute streams + Num.
- per-engine **register-pressure** cap: `check_skip_due_to_register_pressure(int eng)` @ `0xc56630` — a per-engine integer budget over the 8 EngineType ordinals.

*Anchors: the `update_timings` ENG jump table is 6-case, `simEngineId2string` is 13-case (`cmpl $0xc`), and both symbols plus `check_skip_due_to_register_pressure` resolve at the listed addresses. Every one of these is an *engine* index; none carries a partition coordinate.*

### Its "multi-cycle reservation" is a SimEvent on a cycle timeline

The closest scheduler analog to "an instruction occupies a resource for N units" is a **`PerfSim` `SimEvent{start, cost}`** on a per-engine **cycle** timeline — and the search for a free slot is **1-D over time**, not a 2-D rectangle:

- ready list type (level 3): `TimeAwareScheduler::ready_list_insert_by_eng_start` @ `0xc55dc0` takes `std::vector<std::map<unsigned long /*ready-cycle*/, std::set<bir::Instruction*, ReadyInstCmp>>>` — a **per-engine** (outer vector) timeline **bucketed by ready cycle** (map key).
- placement: `schedule_inst` @ `0xc5b6e0` → `PerfSim::add_instruction(I, start, SimEngineId, I)` @ `0x165fa30` places the instruction on its `SimEngineId` timeline at `start`.
- free-slot search: `PerfSim::determine_start_time(clock, SimEngineId, …)` @ `0x165ad90` indexes a per-engine timeline (24-byte stride), takes `max(clock, engine-free-time)` — a **1-D earliest-feasible-cycle** scan, single-dimensional, per engine.
- latency: `post_scheduler::get_latency` @ `0xc18730` makes the Hwm virtual call `call *0x68(%rax)` @ `0xc18796` — the per-cycle cost pre_sched never consults.

*Anchors: the `ready_list_insert_by_eng_start` parameter type demangles to `vector<map<ulong, set<Instruction*, ReadyInstCmp>>>`, and `get_latency`'s indirect `call *0x68(%rax)` sits at `0xc18796`. The `{start,cost}` semantics come from `add_instruction(…, start, …)` plus `get_cost` = duration; the `SimEvent` field layout `{start, cost, dispatch…}` itself is reconstructed from the shape of the `get_timeline_*` accessors.*

---

## The three-axis ownership table (core deliverable)

| Dimension | **ALLOCATOR** `selectNode` (Axis A) | **pre_sched** [8.8](reorder-nonssa-presched.md) (Axis B) | **post_sched** 8.11 *(planned)* (Axis C) |
|---|---|---|---|
| TU / subtree | `coloring_allocator/…/sb_select.cpp` | `dep_based_optims/pre_sched.cpp` | `dep_based_optims/inst_sch + time_aware_sched.cpp` |
| Runs at (order) | ~51 (coloring SB) | 33 (first scheduler) | 92 (cost-model scheduler) |
| Operates over | physical SBUF addresses | logical `MemoryLocation`s | physical access patterns |
| **Resource axis** | **partition-band × byte** (2-D rectangle) | **SBUF/PSUM live-byte** budget (no engine, no time) | **engine × cycle** (per-engine timeline) |
| "Reservation" object | `std::array<vector<pair<int,int>>,4>` (4 quadrants) | **NONE** | `vector<map<cycle,set<I>>>` + `PerfSim` `vector<SimEvent>` |
| Feasibility test | byte ∩ ∧ partition ∩ (`boundIter`/`intersectionIter`, `0x1cc2110`/`0x1cc4128`) | **NONE** | earliest-free **cycle** scan (`determine_start_time` @ `0x165ad90`; 1-D) |
| Multi-cycle occupancy | n/a (placement) | n/a | `SimEvent{start,cost}` on a `SimEngineId` timeline |
| Engine binding | n/a (engine-agnostic) | **NONE** (`run_pre_sched` issues 0 `assign_engines`) | `assign_engines` @ `0x156f700` (`call` @ `0xc36539`) |
| Priority | Chaitin geometric coloring + spill cost | structural **unit-weight** longest path + reg-pressure | `longest_path` **height** (Hwm-weighted) + PerfSim time |
| Latency (Hwm, [G14]) | n/a | **UNUSED** (`compute_depths_forward` has 0 latency calls) | **USED** (`get_latency` `call *0x68(%rax)` @ `0xc18796`) |
| Owns `partitionBandReservations`? | **YES** — `selectNode` (the §3.4 table) | **ABSENT** | **ABSENT** |

The table is the page. Read across the bottom row: the partition-band reservation table is owned by exactly one pass, the allocator, and is **absent** from both schedulers. Read across the "resource axis" row: three passes, three disjoint axes — partition×byte, live-byte, engine×cycle — that share no data structure.

---

## Evidence anchors and limits

The disambiguation rests on five instruction-level reads, each of which rules out a specific way of getting this wrong:

- **The partition-band table's strings have exactly two cross-references, both in `selectNode`.** `objdump -d <bin> | rg -c '# 1cc2110|# 1cc4128'` over the whole 65 MB `.text` returns **2**, at `0xa06d4b` and `0xa06d6a`, both inside `selectNode`'s body `0xa05250..0xa06e20`. There is no second owner.
- **The table really is the 4-band quadrant structure.** `nm -DC` shows `selectNode`'s 3rd param and `collectReservations`'s 1st param are both literally `std::array<std::vector<std::pair<int,int>>, 4ul>` — not a generic container misread as 4-band.
- **pre_sched binds no engine; post_sched does.** `run_pre_sched` @ `0xca1490..0xca1cc0` contains zero `assign_engines`/`getEngineType` calls, while `post_scheduler::schedule(Function)` @ `0xc36010` reaches `call assign_engines@plt` @ `0xc36539`. pre_sched carries no hidden engine model.
- **pre_sched's depth is latency-free; post_sched's is not.** `compute_depths_forward` @ `0xc8f610` calls only DenseMap helpers, `__assert_fail`, and itself — no Hwm, no float cost. `post_scheduler::get_latency` @ `0xc18730` makes `call *0x68(%rax)` (Hwm vptr+0x68) @ `0xc18796`. The two schedulers do not share a latency-weighted priority.
- **post_sched's resource axis is engine × cycle, not partition.** `update_timings(ENG)` is a 6-case ENG jump table, `simEngineId2string` a 13-case (`cmpl $0xc`) SimEngineId roster, and `ready_list_insert_by_eng_start`'s type is `vector<map<cycle,set<I>>>`. No partition coordinate appears anywhere — there is no partition-band table hiding inside post_sched.

Three things stop short of an instruction-level read. `selectNode`'s interior control flow: the `lower_bound`/overlap pseudocode reconstructs the two asserts' guards rather than transcribing its 6 KB body, which belongs to page 8.17. `collectReservations`'s 4-iteration fill loop, established from the `array<…,4ul>` signature rather than by stepping the loop. And the `SimEvent` field offsets, deduced from the accessor shapes — the `{start,cost}` semantics are read, the byte layout is not.

## Cross-references

- [8.8 — SSA Exit and Pre-Scheduling](reorder-nonssa-presched.md): pre_sched's two-stage internals (MemoryLocation DAG → per-block list scheduler), the unit-weight critical path, and the live-byte resource model summarized here as Axis B.
- **Part 8.11 — post_sched schedulers** *(planned)*: the engine × cycle cost-model scheduler, the `PerfSim` cycle timeline, the `backend::ENG`/`SimEngineId` engine rosters, and the Hwm-weighted height priority summarized here as Axis C.
- **Part 8.17 — SBUF coloring allocator** *(planned)*: `SB_Allocator::selectNode` in full — the partition-band reservation table whose *ownership* this page proves, the `collectReservations`/`compress`/`sort` fill path, and the Chaitin coloring it serves (Axis A).
- [1.07 — SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md): why SBUF is a 2-D (partition × byte) address space — the geometry the allocator's partition bands quantize, and the reason the partition axis is *physically* the allocator's, not a scheduler's.
