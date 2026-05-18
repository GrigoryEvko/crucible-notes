# Phase 110 -- PostSchedule

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Phase 110 (`PostSchedule`) is the post-register-allocation re-scheduling pass. Where `ScheduleInstructions` (`sub_8D0640`, bin 114 — a SKIP-numbered worker invoked by the wiki-97 gate `AdvancedPhasePreSched`) runs **pre-RA** on virtual registers and is responsible for ILP/pressure trade-offs, phase 110 runs **after** register allocation, after the post-RA WAR fixup (phase 105 `ApplyPostRegAllocWars`), and after the hot/cold layout passes (108--109), to re-order the now-physical instruction stream against the actual hardware latency/throughput model that requires concrete register numbers, banks, and reuse cache slots to be known.

The phase is implemented as a thin Type-A-shaped dispatcher whose body is 51 bytes (16 x86-64 instructions, 4 basic blocks) and which performs no scheduling work directly. Instead it routes through the **sub-target's** vtable at slot `+0x90` to the SM-family-specific post-scheduling backend -- either Backend A's `PostSchedulePass::runOnFunction` (`sub_A97600`, 42 KB) on the legacy path or, for Ampere and later (sm_80+), one of the modern backends that have a native post-scheduling entry point. PostSchedule is the **only** caller in the binary of the sentinel `nullsub_45` at `0x680190`; that sentinel is the marker the dispatcher uses to recognise a sub-target that does not install a post-scheduling override and therefore wants the phase to be a no-op.

| | |
|---|---|
| **Wiki number** | 110 |
| **Factory case** | 133 (in `sub_C60D30`) |
| **Vtable symbol** | `off_22BEA90` at `0x22BEA90` |
| **`execute()`** | `sub_C60640` (51 B, 16 insns, 4 blocks) |
| **`getIndex()`** | `sub_C5E3C0` -- returns `133` |
| **`isNoOp()`** | `sub_C5E3D0` -- returns `0` (active) |
| **O-level gate** | active at `-O1` and above; at `-O0` the entire `ctx+0x630 -> [+0x10] -> vtable[+0x90]` chain still indirects, but every release sub-target installs the no-op `nullsub_45` there for `-O0` |
| **SM gate (inside body)** | `sub_7DDB50(ctx) > 1` -- skipped on sm_50--sm_75 (Maxwell/Pascal/Volta/Turing); active on sm_80+ |
| **Sub-target dispatch slot** | sub-target `vtable[+0x90]` (slot 18) |
| **Sentinel** | `nullsub_45` at `0x680190` -- 2-byte `repz ret`, used only here |
| **Static name table entry** | `off_22BD0C0[133]` -- string `"PostSchedule"` at `.rodata:0x22BCD47` |
| **`AdvancedPhasePostSched` predecessor** | wiki phase 106 (binary case 129) -- Type-C thunk `sub_C5E830` writes `ctx+1552 = 14` before phase 110 enters |

## Position in the Pipeline

Phase 110 sits in the **post-RA cleanup band** between physical-register liveness fix-up and final block layout. The full band from the end of phase 97 (pre-RA scheduling) to phase 115 (scoreboard generation) is:

| # | Bin# | Phase | Role w.r.t. PostSchedule |
|---|---|---|---|
| 97 | 113 | `AdvancedPhasePreSched` | Type-A gate, dispatches to `ScheduleInstructions` (bin 114, SKIP-numbered worker); marks pre-RA scheduling boundary |
| -- | 114 | `ScheduleInstructions` (`sub_8D0640`) | Pre-RA scheduling -- 3-phase ReduceReg/ILP/DynBatch on virtual registers; SKIP-numbered in wiki |
| 101 | 121 | `AdvancedPhaseAllocReg` | Dispatches to `AllocateRegisters` (bin 122, SKIP-numbered worker; fatpoint allocator, `sub_957160`) |
| -- | 122 | `AllocateRegisters` | Physical register assignment; SKIP-numbered in wiki |
| 104 | 126 | `AdvancedPhasePostExpansion` | Type-A gate -> `PostExpansion` (bin 127, SKIP-numbered worker) |
| 105 | 128 | `ApplyPostRegAllocWars` | Fixes WAR hazards exposed by RA |
| 106 | 129 | `AdvancedPhasePostSched` | **Type-C** thunk `sub_C5E830`: writes `ctx+1552 = 14`. This is the timeline marker that downstream guards read to confirm "PostSchedule reached" |
| 107 | 130 | `OriRemoveNopCode` | Removes placeholder NOPs |
| 108 | 131 | `OptimizeHotColdInLoop` | Intra-loop hot/cold separation |
| 109 | 132 | `OptimizeHotColdFlow` | Function-level hot/cold separation |
| **110** | **133** | **`PostSchedule`** | **Sub-target post-RA re-scheduling -- this page** |
| 111 | 134 | `AdvancedPhasePostFixUp` | **Type-C** thunk `sub_C5E390` (11 B): `mov [rsi+0x610], 0x14; ret` -- writes `ctx+1552 = 20` |
| 112 | 135 | `PlaceBlocksInSourceOrder` (`sub_C60B70` -> `sub_788A30`) | Final BB linearisation. Active only when `sub_7DDB50(ctx) == 1` (sm_70/sm_75) **and** an options-bit guard passes |
| 113 | 136 | `PostFixForMercTargets` (`loc_C60700`) | Mercury-encoding-specific instruction fixup |
| 114 | 137 | `FixUpTexDepBarAndSync` (`loc_C5E310`) | Texture dependency barrier insertion; runs immediately before scoreboard generation |
| 115 | 138 | `AdvancedScoreboardsAndOpexes` | Full control-word generation (`sub_A36360` + `sub_A23CF0`) at `-O1+` |
| 116 | 139 | `ProcessO0WaitsAndSBs` | Conservative scoreboard insertion at `-O0` |

The five-phase chain 110--114 is the **finalisation window** for the scheduled instruction stream. Each phase has a narrow, target-dispatched responsibility:

1. **Phase 110** re-runs the schedule against physical resources (this page).
2. **Phase 111** is a pure pipeline-progress beacon -- it transforms no IR.
3. **Phase 112** flattens the basic-block graph into emission order.
4. **Phase 113** applies Mercury-encoding-specific peephole rewrites that depend on final instruction position.
5. **Phase 114** materialises explicit barriers for texture-dependent hazards that the scoreboard encoder cannot express.

Only after this chain completes does phase 115 walk the now-frozen instruction list to compute and pack the per-instruction 23-bit control words.

## Execute Body Disassembly

```
; sub_C60640 -- PostSchedule::execute(this, ctx)
;                a1 = `this` (16-byte phase object, unused)
;                a2 = compilation context (rsi/rbx)
;
0xc60640: 53                              push    rbx                       ; save rbx
0xc60641: 48 89 f7                        mov     rdi, rsi                  ; rdi = ctx (arg to sub_7DDB50)
0xc60644: 48 89 f3                        mov     rbx, rsi                  ; preserve ctx in rbx across the call
0xc60647: e8 04 d5 b7 ff                  call    sub_7DDB50                ; eax = GetSmVersionIndex(ctx)
0xc6064c: 83 f8 01                        cmp     eax, 1
0xc6064f: 7e 1d                           jle     short loc_C6066E          ; SM <= 1 (Maxwell..Turing) -> exit
0xc60651: 48 8b 83 30 06 00 00            mov     rax, [rbx+0x630]          ; rax = ctx.target  (target pointer at ctx+1584)
0xc60658: 48 8b 78 10                     mov     rdi, [rax+0x10]           ; rdi = target.subTarget  (sub-target / HW model)
0xc6065c: 48 8b 07                        mov     rax, [rdi]                ; rax = subTarget.vtable
0xc6065f: 48 8b 80 90 00 00 00            mov     rax, [rax+0x90]           ; rax = subTarget.vtable[18]
0xc60666: 48 3d 90 01 68 00               cmp     rax, offset nullsub_45    ; check against the per-dispatcher sentinel
0xc6066c: 75 02                           jnz     short loc_C60670          ; override installed -> tail-call it
0xc6066e: 5b                              pop     rbx
0xc6066f: c3                              retn                              ; no override -> phase is a no-op
0xc60670: 5b                              pop     rbx
0xc60671: ff e0                           jmp     rax                       ; tail-call into subtarget post-schedule hook
```

The body is small enough to read directly: there are exactly two exit paths and no inlined work. Every architectural decision PostSchedule makes is encoded in **what address the sub-target installs into `subTarget.vtable[+0x90]`**.

## Sub-Target Vtable Slot +0x90

The dispatch chain `ctx -> [+0x630] -> [+0x10] -> vtable[+0x90]` resolves through two indirections:

* `ctx[+0x630]` (`1584`) is the **target** object -- the SM-family backend (one of the per-SM-family classes allocated when ptxas selects an architecture profile).
* `target[+0x10]` is the **sub-target** -- the per-SM-variant specialisation of the target (e.g. sm_80 vs sm_86 vs sm_89 vs sm_90a). The sub-target embeds the hardware latency profile, the register-file partition layout, and the post-scheduling re-emit logic.
* Slot `+0x90` (byte offset 144, vtable slot 18) is the sub-target's **`postSchedule(this)`** virtual method.

The four observed installers for this slot are:

| Slot +0x90 install | Sub-target family | Post-schedule behaviour |
|---|---|---|
| `nullsub_45` at `0x680190` | sm_50, sm_52, sm_53, sm_60, sm_61, sm_62, sm_70, sm_72, sm_75 | No-op: phase 110 returns immediately. SM-version gate at the top of `sub_C60640` already excludes most of these, but the sentinel covers any remaining cases where `GetSmVersionIndex` returns 0--1 for a sub-target still constructed with the default vtable. (The pre-sm_80 legacy targets have **no** post-RA scheduling at all -- they also do not install a real `postSchedule` here. The function `sub_A97600` at the same six target classes' vtable+0x5F0 is the per-operand source-slot-count query, not a post-RA scheduler -- see [Legacy Backend A](./legacy-backend-a.md).) |
| Backend C entry (`sub_1908D90`, `RBTScheduleOrchestrator`, mode 0 = post-schedule) | sm_80, sm_86, sm_87, sm_89, sm_90, sm_90a, sm_100, sm_103, sm_120, sm_121 (default modern path) | RB-tree list scheduler in post-schedule mode. Walks the per-BB code list, builds a 3-key red-black priority tree (`sub_18FD370` / `sub_18FCDA0`), and emits a re-ordered instruction stream with full register-pressure cost-model awareness. Equivalent dispatch from outside the phase pipeline is `sub_C5FFF0` (`DispatchPostSchedule`), which is **not** what phase 110 calls -- the phase calls the same backend through the sub-target vtable, not through the side-channel dispatcher. |
| `sub_19D1AF0`-style sm_100/sm_120 specialisation | Blackwell, Blackwell-Ultra, sm_120 (consumer RTX 50**) | Sub-target-specific post-schedule that calls the Backend C orchestrator but with extra tensor-pipeline (TCGEN05) post-fixup before scoreboard generation. The +0x90 slot indirects through a further per-instruction vtable to apply Blackwell warpgroup re-grouping after the schedule is final. |

The relationship between phase 110 and the side-channel `DispatchPostSchedule` (`sub_C5FFF0`, documented in `scheduling/algorithm.md`) is **parallel, not equivalent**. `sub_C5FFF0` is called from *inside* the pre-RA scheduler `sub_8D0640` as a fallback when the legacy backend fails. Phase 110 is the *pipeline-driven* invocation -- the PhaseManager iterates to entry 133, calls `sub_C60640`, which routes through the sub-target. Both eventually land in `sub_1908D90` for sm_80+ but through different argument shapes (mode 0 in both cases) and different `this` pointers.

## Why PostSchedule Re-Runs Scheduling

Pre-RA scheduling (`ScheduleInstructions`, bin 114; dispatched by the wiki-97 gate `AdvancedPhasePreSched`) cannot model:

1. **Register-file bank conflicts.** Until RA has assigned physical registers, the scheduler does not know which operands sit in the same bank and therefore which instructions will stall on read-port contention. Backend C uses `RBTPressureCostModel` (`sub_18F3CB0`, 16 KB) which can score post-RA schedules accurately but only sees the right inputs after RA.
2. **Reuse cache slot assignment.** The 6-bit per-source reuse hint encoded in the control word requires knowing the exact source register numbers; pre-RA scheduling reasons about live ranges, not registers, so it cannot decide reuse cache occupancy.
3. **Post-RA spill/reload code.** Phases 102 (`AllocateRegisters`) and 105 (`ApplyPostRegAllocWars`) insert spill, reload, and WAR-break sequences that did not exist when the pre-RA schedule was computed. Those inserts can have very different latencies (a spill is ~30 cycles, a reload is ~20 cycles on sm_80+) and need to be co-scheduled with the rest of the block.
4. **Banked write-after-write hazards on the operand-collector pipeline.** On sm_90+ the operand collector imposes write-after-write timing constraints that depend on the **physical** destination register, not just the live range.

Pre-RA scheduling optimises for register pressure (Phase 1 ReduceReg, mode 0x39) and latency hiding (Phase 2 ILP, mode 0x49) against a *virtual* register model. Phase 110 re-runs the scheduler against the *physical* register model, accepting the constraints RA has imposed and tightening the ordering to minimise the actual bank conflicts and reuse-cache invalidations that the physical assignment introduced.

## What Phase 110 *Does Not* Do

A few clarifying negatives, because the post-RA window is densely packed and several adjacent passes have overlapping-sounding names:

* **Phase 110 does not generate the SASS control word.** That is phase 115 (`AdvancedScoreboardsAndOpexes`, `sub_A36360` 52 KB encoder). PostSchedule's output is still abstract Ori IR with re-ordered instruction nodes; the 23-bit control word is computed later.
* **Phase 110 does not pack the dual-issue pairs.** Dual-issue pairing on sm_70/sm_75 is decided during the pre-RA scheduling pass (`sub_8CF5D0`, `CheckDualIssueEligibility`, 3.5 KB, called by Phase 2 ILP) and materialised by the Mercury encoder; PostSchedule on sm_80+ does not pair (Ampere onwards is a single-issue warp scheduler with co-issue on independent functional units).
* **Phase 110 does not insert texture-dependency barriers.** Phase 114 (`FixUpTexDepBarAndSync`) does that. PostSchedule sees a texture load and a dependent consumer as ordinary RAW dependencies and orders them; the explicit `TEXDEPBAR` barrier instruction is inserted by phase 114 four phases later.
* **Phase 110 does not materialise stall counts.** Stall counts (the 4-bit `bits[3:0]` field of the SASS control word) are computed by phase 115's encoder from the **already-final** instruction ordering. PostSchedule's job is to produce that final ordering; the stall values come next.
* **Phase 110 does not relocate basic blocks.** That is phase 112 (`PlaceBlocksInSourceOrder` -> `sub_788A30`), which runs immediately after. PostSchedule operates within each basic block's existing position in the function CFG; it only re-orders instructions *within* blocks.

## QUIRK -- The `nullsub_45` Sentinel Is Unique to PostSchedule

`nullsub_45` at `0x680190` is a 2-byte `repz ret` referenced from **exactly one** location in the entire ptxas binary: the comparison at `0xc60666` inside `sub_C60640`. Other phases that use the same "is this slot overridden?" pattern compare against different sentinels:

* `AdvancedPhaseAfterSetRegAttr` (phase 92, `sub_C607A0`) compares slot `+0x110` against `nullsub_170` at `0x7D6C80`.
* `PostFixUp` (phase 140, `sub_C5E270`) tail-calls slot `+0x148` unconditionally without a sentinel check, relying on every Mercury target installing a target-specific method.

The choice of a unique sentinel per dispatcher gives ptxas a way to distinguish "target opts out of this phase" (slot still points to `nullsub_45`) from "target installs a no-op because it has nothing to do but the slot is logically populated" (slot points to a target-specific function that happens to return immediately). For PostSchedule the distinction matters because the SM-version gate at the top of the body (`SmVersion > 1`) and the sentinel gate are **both** needed: a target object can be constructed for a sub-target whose `GetSmVersionIndex` returns 2 (Ampere+) but whose vtable slot at `+0x90` is still the default `nullsub_45` -- this happens for early-prototype SM variants whose backend is not yet finished. In that case the SM-version gate passes (because the architecture index is 2+) but the sentinel gate aborts before the bogus call.

## QUIRK -- Reads Through the Sub-Target Pointer, Not the Target Vtable

Almost every other Type-A and Type-B gate in the 159-phase pipeline indirects through `ctx[+0x630] -> [+0x0]` -- i.e. directly through the **target's** vtable. PostSchedule is one of the very few that indirects through `ctx[+0x630] -> [+0x10]` -- the target's **sub-target** member -- and then through *that* object's vtable. The other gate that follows the same pattern is the pre-schedule analogue at `sub_C605C0`, with the corresponding sub-target slot.

This matters when interpreting the dispatch table at `off_22BEA90`: the table itself stores `sub_C60640` as the polymorphic execute, but the *actual* method that runs is a function pointer **stored on a different vtable entirely** -- the sub-target's vtable. Pipeline-level analysis that walks only the per-phase vtable (`off_22BCC78 .. off_22BEE50`) will see PostSchedule as a stub; finding what really runs requires resolving the sub-target vtable for the current SM and reading slot 18.

The motivation is **per-SM-variant specialisation without per-SM-family vtable duplication**. The target object for, e.g., "Ampere" is shared between sm_80, sm_86, sm_87, sm_89; only the sub-target differs. Putting the post-schedule hook on the sub-target lets each sub-target install a different `postSchedule` without forcing each SM-variant to clone the full ~50-slot target vtable.

## QUIRK -- Skipped Entirely on sm_50--sm_75

The SM-version gate `sub_7DDB50(ctx) > 1` means PostSchedule does **nothing** on Maxwell (sm_50/sm_52/sm_53), Pascal (sm_60/sm_61/sm_62), Volta (sm_70/sm_72), and Turing (sm_75). For those architectures the schedule from the pre-RA scheduler (bin 114 `ScheduleInstructions`) is the final schedule, modulo only the WAR-fixup in phase 105 and the texture-barrier work in phase 114.

This is consistent with the per-SM-backend dispatch documented in `scheduling/algorithm.md`: Backends B (SM89/90 codec) and C (RBT list) replace Backend A's three-phase scheduling on sm_80+, and only those backends have a meaningful post-scheduling step. On pre-sm_80 architectures the pre-RA scheduler is the only scheduler that runs, the dependency-graph re-build in PostSchedule would expose nothing the pre-RA pass missed, and the cost of re-scheduling 30--50% of the function would not pay for itself. The gate is hard-coded in the execute body rather than being a knob, and there is no `-knob SchedulePost*` option to override it.

The wiki number column "O-level" for phase 110 reads `> 0` (active at `-O1`+), but the SM gate is a separate orthogonal filter -- a `-O2 sm_50` compilation still skips PostSchedule, and a `-O0 sm_90` compilation still enters the body but every release sub-target's slot `+0x90` is `nullsub_45` at `-O0` (no override is installed).

## Interaction with Register Allocation

The pipeline order `RA (102) -> ApplyPostRegAllocWars (105) -> HotCold (108--109) -> PostSchedule (110)` is not accidental. Each step in that chain produces an invariant that PostSchedule needs:

1. **After phase 102 (`AllocateRegisters`)** every Ori IR instruction has its source and destination operands rewritten from virtual register IDs to physical register numbers (R-regs, UR-regs, and predicate registers). Spill/reload instructions have been materialised. The register-pressure tracker that pre-RA scheduling consulted (`sub_69A1A0`, 952 B context) is replaced by an exact register-occupancy map.

2. **After phase 105 (`ApplyPostRegAllocWars`)** the WAR (write-after-read) hazards introduced by RA's coalescing decisions have been resolved either by inserting extra NOPs or by reordering producer/consumer pairs. PostSchedule sees a stream in which physical-register WAR constraints are already legal -- it does not need to insert WAR stalls, only re-order independent instructions.

3. **After phases 108--109 (`OptimizeHotColdInLoop`, `OptimizeHotColdFlow`)** cold blocks have been hoisted out of the loop nest and the function-level layout has been split into hot and cold regions. PostSchedule sees a CFG where each region has uniform expected execution frequency; its priority function can apply the same `RBTPressureCostModel` weights uniformly within a region.

4. **Inside phase 110** Backend C (`sub_1908D90` -> `sub_1906090` -> `sub_1902B70`) builds a fresh per-block dependency DAG using `sub_19081F0` (17 KB), keyed on physical-register IDs rather than live ranges. It then runs the RB-tree list scheduler with `sub_18FDAF0` (double-precision weighted score) as the priority function, using `sub_18F3CB0` (16 KB pressure cost model) to track real bank pressure.

5. **After phase 110 returns** the per-BB instruction list has been re-ordered. The instruction pointers in `func+832` (register-liveness table) and `func+104` (SchedNode metadata chain) are stale and are rebuilt by `sub_1902100` (`RBTDependencyUpdate`, 15 KB) before the orchestrator exits.

If RA decides to spill a frequently-live register, PostSchedule will see a sequence `STL.W ..., spilled_reg; ... ; LDL.W spilled_reg, ...` where the spill/reload pair has high latency (~30/~20 cycles on sm_80+). The post-schedule re-ordering will hoist independent instructions into the gap between spill and reload to hide that latency, something pre-RA scheduling could not do because the spill/reload pair did not yet exist.

## Backend C Sub-Schedulers Reached Through Phase 110

When the sub-target vtable slot `+0x90` installs the Backend C entry, the call chain that runs inside PostSchedule is:

```
sub_C60640 (PostSchedule::execute)
  -> [tail-jmp via subtarget.vtable[+0x90]]
       sub_1908D90 (RBTScheduleOrchestrator, mode=0 post-schedule)
         -> sub_18FEE60 (RBTScheduleStateCreate, 528-byte state)
         -> sub_18FE320 (RBTScheduleDataPrepare)
         -> sub_1906090 (RBTScheduleDriver, per-block loop, 368-byte block stride)
              -> sub_19081F0 (RBTBlockDependencyGraphBuild, 17 KB)
              -> sub_1902B70 (RBTCoreListScheduler, 19 KB main loop)
                   -> sub_18FCDA0 (RBTreeExtractMax -- pop highest-priority)
                   -> sub_18FDAF0 (RBTScoreComputation -- weighted score)
                   -> sub_1904B70 (RBTSolutionEvaluator, 26 KB constraint check)
                   -> sub_19043F0 (RBTConstraintValidator, mode 5/6)
                   -> sub_18FD370 (RBTreeInsert -- 3-key balanced insertion)
              -> sub_1902100 (RBTDependencyUpdate, 15 KB DAG maintenance)
         -> sub_19072F0 (RBTInterBlockScheduling, cross-BB register dep, 14 KB)
         -> sub_18F94C0 (RBTCleanup -- state teardown)
```

The constraint-validator at `sub_19043F0` is what enforces post-RA-specific invariants the pre-RA scheduler could not check:
* Read-port bank conflict: at most 2 source operands of the issued instruction may come from the same bank.
* Reuse-cache occupancy: the 6-bit reuse hint can mark at most one source per bank as "reuse from cache" without a writeback-and-refill cost.
* Operand-collector write-after-write distance: on sm_90+ a back-to-back write to the same physical register in adjacent dual-issue slots stalls; the scheduler must separate them by at least one independent slot.

Pre-RA scheduling cannot enforce any of these because they all reference physical register numbers and physical bank IDs. PostSchedule's whole purpose is to be the pass where those constraints get applied.

## Cross-References

* [Scheduler Architecture](overview.md) -- the 3-phase pre-RA scheduling pipeline that phase 110 re-runs on physical registers
* [Scheduling Algorithm](algorithm.md) -- Backend A / B / C internals and the parallel `sub_C5FFF0` side-channel dispatcher
* [Latency Model](latency-model.md) -- per-opcode latency tables that PostSchedule re-evaluates against physical bank assignments
* [Scoreboards & Dependency Barriers](scoreboards.md) -- the phase-115 stage that consumes PostSchedule's final ordering
* [Phase Manager](../passes/phase-manager.md) -- how `sub_C60D30` allocates the 16-byte phase object for case 133
* [Pipeline Phase Table](../passes/index.md) -- complete 159-phase list with execute-function addresses and gates

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_C60640` | 51 B | `PostSchedule::execute(this, ctx)` -- the phase body | CERTAIN |
| `sub_C5E3C0` | 6 B | `PostSchedule::getIndex()` -- returns `133` | CERTAIN |
| `sub_C5E3D0` | 6 B | `PostSchedule::isNoOp()` -- returns `0` | CERTAIN |
| `sub_7DDB50` | -- | `GetSmVersionIndex(ctx)` -- reads `ctx+2104` | HIGH |
| `nullsub_45` | 2 B | The opt-out sentinel; checked only by `sub_C60640` | CERTAIN |
| `sub_A97600` | 7,780 B | **NOT** a post-RA scheduler. Per-operand source-slot-count query installed at the primary-target vtable slot **+0x5F0** (slot 190, byte 1520) on six legacy SM-target classes. See [Legacy Backend A](./legacy-backend-a.md) for the corrected classification. | CERTAIN |
| `sub_1908D90` | -- | Backend C `RBTScheduleOrchestrator` -- modern sm_80+ post-schedule, also installed at sub-target vtable +0x90 | HIGH |
| `sub_C5FFF0` | -- | `DispatchPostSchedule` -- parallel side-channel dispatcher (NOT phase 110's call path) | CERTAIN |
| `sub_C5E830` | 7 B | `AdvancedPhasePostSched` Type-C thunk -- writes `ctx+1552 = 14` immediately before phase 110 runs | CERTAIN |
| `sub_C5E390` | 11 B | `AdvancedPhasePostFixUp` Type-C thunk -- writes `ctx+1552 = 20` before the post-fixup worker (phase 140 `PostFixUp`) enters | CERTAIN |
| `off_22BEA90` | 24 B | Phase-110 vtable: `{ sub_C60640, sub_C5E3C0, sub_C5E3D0 }` | CERTAIN |
| `off_22BD0C0[133]` | -- | Static name table entry: `"PostSchedule"` at `0x22BCD47` | CERTAIN |
