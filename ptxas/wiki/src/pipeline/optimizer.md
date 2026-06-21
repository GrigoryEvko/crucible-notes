# Optimization Pipeline (159 Registered / 157 Dispatched)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The ptxas optimizer is a fixed-order pipeline that transforms Ori IR from its initial post-lowering form into scheduled, register-allocated SASS machine code. The PhaseManager **registers 159 phase objects** (IDs 0–158), but the default driver **dispatches exactly 157 of them** (IDs 0–156) in a fixed identity order. Both numbers are real and describe different objects: 159 is the size of the phase-*name registry* and the inclusive ceiling for explicit phase selection, while 157 is the length of the default phase-*schedule* the dispatch loop actually walks. The two trailing registered phases — ID 157 `DebuggerBreak` and ID 158 `NOP` — are constructed but never scheduled on a default compile; they enter a schedule only through the recipe/named-phases override path (OCG knob 298). Unlike LLVM's PassManager — which uses dependency-driven scheduling and analysis preservation — ptxas dispatches every scheduled phase unconditionally and in a predetermined order; each phase decides internally (via opt-level, knob, and predicate checks inside its own `execute()` body) whether to do anything. Architecture-specific behavior is injected through "AdvancedPhase" hook points whose vtables are overridden per target.

Each phase is a polymorphic C++ object exactly 16 bytes in size, allocated from a memory pool by a 159-case factory switch. The PhaseManager constructs all 159 phase objects up front during initialization, stores them in a flat array, and iterates a separate 157-entry order array in a simple dispatch loop. Per-phase timing and memory consumption are optionally tracked for `--stat=phase-wise` output.

> **157 vs 159 — definitive.** The order accessor `sub_C60D20` returns a *pair*: the order-table pointer (`0x22BEEA0`) in `rax` and the count `0x9D = 157` in `rdx`. Hex-Rays drops `rdx` because the inferred prototype returns a single pointer, so the C view shows only `return &unk_22BEEA0;`. The driver passes `(ptr, count)` straight into the dispatch loop `sub_C64F70`, whose bound is `&schedule[count]` with `count = 157`. The order table at `0x22BEEA0` is the identity array `[0..156]` followed by unrelated rodata; `sub_C62720` sizes the *name registry* at 159 (`*((_DWORD*)PM+27) = 159`, copies `1272 = 159*8` bytes from `off_22BD0C0`). Registered = 159; dispatched by default = 157.

## Key Facts

| Field | Value |
|---|---|
| Registered phases | 159 (IDs 0–158; all named in `off_22BD0C0`) |
| Dispatched by default | 157 (IDs 0–156) |
| Debug-only (registered, not scheduled) | 2 — ID 157 `DebuggerBreak`, ID 158 `NOP` |
| AdvancedPhase hook points | 16 |
| Mercury sub-pipeline phases | 8 (phases 113–114, 117–122) |
| Phase object size | 16 bytes: `{vtable_ptr, allocator_ptr}` |
| Factory switch | `sub_C60D30` (3554 bytes, 159 cases) |
| PhaseManager constructor | `sub_C62720` (4734 bytes) |
| Order accessor | `sub_C60D20` — returns `(&0x22BEEA0, 157)` |
| Dispatch loop | `sub_C64F70` (1455 bytes) |
| Phase name table | `off_22BD0C0` (159 entries, 1272 bytes) |
| Default order table | `0x22BEEA0` — identity `[0..156]` (157 entries) |
| Vtable range | `off_22BD5C8`..`off_22BEE78` (40-byte stride) |
| Recipe/NamedPhases knob ID | 298 (OCG knob; `config+0x53D0`) |
| Pipeline orchestrator | `sub_7FB6C0` |

## Phase Object Layout

Every phase is a 16-byte polymorphic object created by the factory:

```c
struct Phase {
    void** vtable;       // +0: pointer to phase-specific vtable in .data.rel.ro
    void*  allocator;    // +8: memory pool used for allocation
};
```

The vtable provides three virtual methods common to all phases:

| Offset | Signature | Purpose |
|---|---|---|
| `+0` | `execute(Phase*, CompilationContext*)` | Run the phase on the IR — **always called** for every dispatched phase |
| `+8` | `getIndex(Phase*) -> int` | Return the factory/table index (0–158) |
| `+16` | `isNoOp(Phase*) -> bool` | Return 1 to suppress the Before/After timing banner — **does not gate `execute()`** |

Vtable slots `+24` and `+32` are NULL in all 159 vtable instances. Memory allocation uses standalone `pool_alloc` (`sub_424070`) / `pool_free` (`sub_4248B0`), not vtable dispatch.

## Dispatch Loop

The dispatch loop at `sub_C64F70` drives execution. The key fact: `execute()` runs **unconditionally** on every entry of the 157-element order array; `isNoOp()` gates only whether the `"Before "`/`"After "` diagnostic banner is printed, not whether the phase body runs. A phase that should do nothing returns early inside its own `execute()` body (an opt-level, knob, or predicate check), not by being skipped here.

```c
// sub_C64F70 -- simplified
void dispatch(PhaseManager* pm, int* order, int count /* = 157 */) {
    MemorySnapshot baseline = take_snapshot();

    for (int i = 0; i < count; i++) {
        int idx = order[i];
        Phase* phase = pm->phase_list[idx];

        const char* name = pm->name_table[phase->getIndex()];   // vtable+8

        MemorySnapshot before = take_snapshot();                // always

        // isNoOp() (vtable+16) gates ONLY the diagnostic banner, checked
        // once before execute and once after. It never skips execute().
        if (!phase->isNoOp())
            diagnostic("Before " + name);

        phase->execute(pm->compilation_unit);                   // vtable+0, ALWAYS

        if (!phase->isNoOp())
            diagnostic("After " + name);

        if (pm->timing_enabled)
            report_phase_stats(pm, name, &before);              // unconditional
    }

    if (pm->timing_enabled) {
        report_summary(pm, "All Phases Summary", &baseline);
        report_pool_consumption(pm);
    }
}
```

Because `execute()` always runs and the timing record + pre-snapshot are written before the first `isNoOp()` check, `--ftime` output contains a row for every dispatched phase, including phases that immediately early-returned; gated-off phases simply show near-zero elapsed time rather than being omitted.

Timing output format (to stderr when `--stat=phase-wise`):
```text
  <phase_name>  ::  [Total 42 KB ]   [Freeable 8 KB ]   [Freeable Leaked 0 KB ] (0%)
```

## Complete Phase Table

### Group 1 — Initial Setup (phases 0–13)

Program validation, recipe application, FP16 promotion, control flow analysis, macro instruction creation.

| # | Phase Name | Category |
|---|---|---|
| 0 | `OriCheckInitialProgram` | Validation |
| 1 | `ApplyNvOptRecipes` | Recipe application |
| 2 | `PromoteFP16` | Type promotion |
| 3 | `AnalyzeControlFlow` | CFG analysis |
| 4 | `AdvancedPhaseBeforeConvUnSup` | Hook (no-op default) |
| 5 | `ConvertUnsupportedOps` | Legalization |
| 6 | `SetControlFlowOpLastInBB` | CFG fixup |
| 7 | `AdvancedPhaseAfterConvUnSup` | Hook (no-op default) |
| 8 | `OriCreateMacroInsts` | Macro expansion |
| 9 | `ReportInitialRepresentation` | Diagnostics |
| 10 | `EarlyOriSimpleLiveDead` | Early DCE |
| 11 | `ReplaceUniformsWithImm` | Immediate folding |
| 12 | `OriSanitize` | IR validation |
| 13 | `GeneralOptimizeEarly` | Bundled early opts |

Phase 0 validates the initial Ori IR for structural correctness. Phase 1 applies NvOptRecipe transformations (controlled by option 391, which allocates a 440-byte sub-manager at `PhaseManager+56`). Phase 2 promotes FP16 operations where profitable. Phases 4 and 7 are architecture hooks that bracket `ConvertUnsupportedOps` — backends override them to inject target-specific pre/post-legalization logic.

### Group 2 — Early Optimization (phases 14–32)

Branch optimization, loop canonicalization, strength reduction, software pipelining, SSA formation.

| # | Phase Name | Category |
|---|---|---|
| 14 | `DoSwitchOptFirst` | Switch optimization |
| 15 | `OriBranchOpt` | Branch optimization |
| 16 | `OriPerformLiveDeadFirst` | Liveness / DCE |
| 17 | `OptimizeBindlessHeaderLoads` | Texture header opt |
| 18 | `OriLoopSimplification` | Loop canonicalization |
| 19 | `OriSplitLiveRanges` | Live range splitting |
| 20 | `PerformPGO` | Profile-guided opt |
| 21 | `OriStrengthReduce` | Strength reduction |
| 22 | `OriLoopUnrolling` | Loop unrolling |
| 23 | `GenerateMovPhi` | SSA phi insertion |
| 24 | `OriPipelining` | Software pipelining |
| 25 | `StageAndFence` | Memory fence insertion |
| 26 | `OriRemoveRedundantBarriers` | Barrier elimination |
| 27 | `AnalyzeUniformsForSpeculation` | Constant bank speculation analysis |
| 28 | `SinkRemat` | Sink + rematerialization |
| 29 | `GeneralOptimize` | Bundled mid opts |
| 30 | `DoSwitchOptSecond` | Switch optimization (2nd) |
| 31 | `OriLinearReplacement` | Linear scan replacement |
| 32 | `CompactLocalMemory` | Local memory compaction |

The `GeneralOptimize*` phases (13, 29, 37, 46, 58, 65) are compound passes that bundle multiple small optimizations (copy propagation, constant folding, algebraic simplification) into a single fixed-point iteration. They appear at multiple pipeline positions to re-clean the IR after major transformations. Liveness/DCE also runs repeatedly (`OriPerformLiveDead` at phases 16, 33, 61, 84) to remove dead code exposed by intervening passes.

### Group 3 — Mid-Level Optimization (phases 33–52)

GVN-CSE, reassociation, shader constant extraction, CTA expansion, argument enforcement.

| # | Phase Name | Category |
|---|---|---|
| 33 | `OriPerformLiveDeadSecond` | Liveness / DCE (2nd) |
| 34 | `ExtractShaderConstsFirst` | Shader constant extraction |
| 35 | `OriHoistInvariantsEarly` | LICM (early) |
| 36 | `EmitPSI` | PSI emission |
| 37 | `GeneralOptimizeMid` | Bundled mid opts |
| 38 | `OptimizeNestedCondBranches` | Nested branch opt |
| 39 | `ConvertVTGReadWrite` | VTG read/write conversion |
| 40 | `DoVirtualCTAExpansion` | Virtual CTA expansion |
| 41 | `MarkAdditionalColdBlocks` | Cold block marking |
| 42 | `ExpandMbarrier` | Mbarrier expansion |
| 43 | `ForwardProgress` | Forward progress guarantee |
| 44 | `OptimizeUniformAtomic` | Uniform atomic opt |
| 45 | `MidExpansion` | Mid-level legalization |
| 46 | `GeneralOptimizeMid2` | Bundled mid opts (2nd) |
| 47 | `AdvancedPhaseEarlyEnforceArgs` | Hook (no-op default) |
| 48 | `EnforceArgumentRestrictions` | ABI enforcement |
| 49 | `GvnCse` | GVN + CSE |
| 50 | `OriReassociateAndCommon` | Reassociation + commoning |
| 51 | `ExtractShaderConstsFinal` | Shader constants (final) |
| 52 | `OriReplaceEquivMultiDefMov` | Redundant move elimination |

Shader constant extraction (phases 34, 51) identifies uniform values that can be loaded from constant memory rather than recomputed per-thread. `GvnCse` (phase 49) combines global value numbering with common subexpression elimination in a single pass. The `MidExpansion` (phase 45) performs target-dependent lowering of operations that must be expanded before register allocation but after high-level optimizations have had their chance.

### Group 4 — Late Optimization (phases 53–77)

Predication, rematerialization, loop fusion, varying propagation, sync optimization, phi destruction, uniform register conversion.

| # | Phase Name | Category |
|---|---|---|
| 53 | `OriPropagateVaryingFirst` | Varying propagation |
| 54 | `OriDoRematEarly` | Early rematerialization |
| 55 | `LateExpansion` | Late legalization |
| 56 | `SpeculativeHoistComInsts` | Speculative hoisting |
| 57 | `RemoveASTToDefaultValues` | AST cleanup |
| 58 | `GeneralOptimizeLate` | Bundled late opts |
| 59 | `OriLoopFusion` | Loop fusion |
| 60 | `DoVTGMultiViewExpansion` | Multi-view expansion |
| 61 | `OriPerformLiveDeadThird` | Liveness / DCE (3rd) |
| 62 | `OriRemoveRedundantMultiDefMov` | Dead move elimination |
| 63 | `OriDoPredication` | If-conversion |
| 64 | `LateOriCommoning` | Late commoning |
| 65 | `GeneralOptimizeLate2` | Bundled late opts (2nd) |
| 66 | `OriHoistInvariantsLate` | LICM (late) |
| 67 | `DoKillMovement` | Kill movement |
| 68 | `DoTexMovement` | Texture movement |
| 69 | `OriDoRemat` | Rematerialization |
| 70 | `OriPropagateVaryingSecond` | Varying propagation (2nd) |
| 71 | `OptimizeSyncInstructions` | Sync optimization |
| 72 | `LateExpandSyncInstructions` | Late sync expansion |
| 73 | `ConvertAllMovPhiToMov` | Phi destruction |
| 74 | `ConvertToUniformReg` | Uniform reg conversion |
| 75 | `LateArchOptimizeFirst` | Arch-specific late opt |
| 76 | `UpdateAfterOptimize` | IR update pass |
| 77 | `AdvancedPhaseLateConvUnSup` | Hook (no-op default) |

Predication (phase 63) converts short conditional branches into predicated instruction sequences, eliminating branch divergence. Rematerialization runs twice (phases 54 and 69) — the early pass targets values that are cheap to recompute, while the late pass handles cases exposed by predication and loop fusion. Phase 73 (`ConvertAllMovPhiToMov`) destroys SSA form by converting phi nodes into move instructions, preparing the IR for register allocation. Phase 74 converts qualifying values to uniform registers (UR), reducing general register pressure.

### Group 5 — Legalization (phases 78–96)

Late unsupported-op expansion, backward copy propagation, GMMA fixup, register attribute setting, final inspection.

| # | Phase Name | Category |
|---|---|---|
| 78 | `LateExpansionUnsupportedOps` | Late unsupported ops |
| 79 | `OriHoistInvariantsLate2` | LICM (late 2nd) |
| 80 | `ExpandJmxComputation` | JMX expansion |
| 81 | `LateArchOptimizeSecond` | Arch-specific late opt (2nd) |
| 82 | `AdvancedPhaseBackPropVReg` | Hook (no-op default) |
| 83 | `OriBackCopyPropagate` | Backward copy propagation |
| 84 | `OriPerformLiveDeadFourth` | Liveness / DCE (4th) |
| 85 | `OriPropagateGmma` | GMMA propagation |
| 86 | `InsertPseudoUseDefForConvUR` | UR pseudo use/def |
| 87 | `FixupGmmaSequence` | GMMA sequence fixup |
| 88 | `OriHoistInvariantsLate3` | LICM (late 3rd) |
| 89 | `AdvancedPhaseSetRegAttr` | Hook (no-op default) |
| 90 | `OriSetRegisterAttr` | Register attribute setting |
| 91 | `OriCalcDependantTex` | Texture dependency calc |
| 92 | `AdvancedPhaseAfterSetRegAttr` | Hook (no-op default) |
| 93 | `LateExpansionUnsupportedOps2` | Late unsupported ops (2nd) |
| 94 | `FinalInspectionPass` | Final IR validation |
| 95 | `SetAfterLegalization` | Post-legalization marker |
| 96 | `ReportBeforeScheduling` | Diagnostics |

GMMA (phases 85, 87) handles WGMMA (warp group matrix multiply-accumulate) instruction sequences that require specific register arrangements and ordering constraints. `OriSetRegisterAttr` (phase 90) annotates registers with scheduling attributes (latency class, bank assignment) consumed by the downstream scheduler. `FinalInspectionPass` (phase 94) is a validation gate that catches illegal IR patterns before the irreversible scheduling/RA phases.

### Group 6 — Pre-Scheduling and Register Allocation (phases 97–103)

Synchronization insertion, WAR fixup, register allocation, 64-bit register handling.

| # | Phase Name | Category |
|---|---|---|
| 97 | `AdvancedPhasePreSched` | Hook (no-op default) |
| 98 | `BackPropagateVEC2D` | Vector back-propagation |
| 99 | `OriDoSyncronization` | Synchronization insertion |
| 100 | `ApplyPostSyncronizationWars` | Post-sync WAR fixup |
| 101 | `AdvancedPhaseAllocReg` | Hook (no-op default) |
| 102 | `ReportAfterRegisterAllocation` | Diagnostics |
| 103 | `Get64bRegComponents` | 64-bit register splitting |

Phase 99 inserts the synchronization instructions (`BAR`, `DEPBAR`, `MEMBAR`) required by the GPU memory model. Phase 100 fixes write-after-read hazards exposed by sync insertion. Register allocation is driven through the hook at phase 101 — the actual allocator is architecture-specific and invoked from the AdvancedPhase override. Phase 103 splits 64-bit register pairs into their 32-bit components for architectures that require it.

### Group 7 — Post-RA and Post-Scheduling (phases 104–116)

Post-expansion, NOP removal, hot/cold optimization, block placement, scoreboards.

| # | Phase Name | Category |
|---|---|---|
| 104 | `AdvancedPhasePostExpansion` | Hook (no-op default) |
| 105 | `ApplyPostRegAllocWars` | Post-RA WAR fixup |
| 106 | `AdvancedPhasePostSched` | Hook (no-op default) |
| 107 | `OriRemoveNopCode` | NOP removal |
| 108 | `OptimizeHotColdInLoop` | Hot/cold in loops |
| 109 | `OptimizeHotColdFlow` | Hot/cold flow opt |
| 110 | `PostSchedule` | Post-scheduling |
| 111 | `AdvancedPhasePostFixUp` | Hook (no-op default) |
| 112 | `PlaceBlocksInSourceOrder` | Block layout |
| 113 | `PostFixForMercTargets` | Mercury target fixup |
| 114 | `FixUpTexDepBarAndSync` | Texture barrier fixup |
| 115 | `AdvancedScoreboardsAndOpexes` | Scoreboard generation |
| 116 | `ProcessO0WaitsAndSBs` | O0 wait/scoreboard |

Hot/cold partitioning (phases 108–109) separates frequently executed blocks from cold paths, improving instruction cache locality. `PlaceBlocksInSourceOrder` (phase 112) determines the final layout of basic blocks in the emitted binary. The scoreboard sub-system has two paths: at `-O1` and above, `AdvancedScoreboardsAndOpexes` (phase 115) performs full dependency analysis to compute the 23-bit control word per instruction (4-bit stall count, 1-bit yield, 3-bit write barrier, 6-bit read barrier mask, 6-bit wait barrier mask, plus reuse flags). At `-O0`, phase 115 is a no-op and `ProcessO0WaitsAndSBs` (phase 116) inserts conservative waits.

### Group 8 — Mercury Backend (phases 117–122)

SASS instruction encoding, expansion, WAR generation, opex computation, microcode emission.

| # | Phase Name | Category |
|---|---|---|
| 117 | `MercEncodeAndDecode` | Mercury encode/decode |
| 118 | `MercExpandInstructions` | Instruction expansion |
| 119 | `MercGenerateWARs1` | WAR generation (1st pass) |
| 120 | `MercGenerateOpex` | Opex generation |
| 121 | `MercGenerateWARs2` | WAR generation (2nd pass) |
| 122 | `MercGenerateSassUCode` | SASS microcode generation |

"Mercury" is NVIDIA's internal name for the SASS encoding framework. Phase 117 converts Ori instructions into Mercury's intermediate encoding, then decodes them back to verify round-trip correctness. Phase 118 expands pseudo-instructions into their final SASS sequences. WAR generation runs in two passes (119, 121) because expansion in phase 118 can introduce new write-after-read hazards. Phase 120 generates "opex" (operation extension) annotations. Phase 122 produces the final SASS microcode bytes. The MercConverter infrastructure (`sub_9F1A90`, 35KB) drives the instruction-level legalization using a visitor pattern dispatched through a large opcode switch (`sub_9ED2D0`, 25KB).

### Group 9 — Post-Mercury (phases 123–131)

Register map, diagnostics, debug output.

| # | Phase Name | Category |
|---|---|---|
| 123 | `ComputeVCallRegUse` | Virtual call reg use |
| 124 | `CalcRegisterMap` | Register map computation |
| 125 | `UpdateAfterPostRegAlloc` | Post-RA update |
| 126 | `ReportFinalMemoryUsage` | Diagnostics |
| 127 | `AdvancedPhaseOriPhaseEncoding` | Hook (no-op default) |
| 128 | `UpdateAfterFormatCodeList` | Code list formatting |
| 129 | `DumpNVuCodeText` | SASS text dump |
| 130 | `DumpNVuCodeHex` | SASS hex dump |
| 131 | `DebuggerBreak` | Debugger breakpoint |

`CalcRegisterMap` (phase 124) computes the final physical-to-logical register mapping emitted as EIATTR metadata in the output ELF. `DumpNVuCodeText` and `DumpNVuCodeHex` (phases 129–130) produce the human-readable SASS text and raw hex dumps used by `cuobjdump` and debugging workflows. `DebuggerBreak` (phase 131) is a development-only hook that triggers a breakpoint when a specific phase is reached.

### Group 10 — Finalization (phases 132–158)

Late merge operations, late unsupported-op expansion, high-pressure live range splitting, architecture-specific fixups.

| # | Phase Name | Category |
|---|---|---|
| 132 | `UpdateAfterConvertUnsupportedOps` | Post-conversion update |
| 133 | `MergeEquivalentConditionalFlow` | Conditional flow merge |
| 134 | `AdvancedPhaseAfterMidExpansion` | Hook (no-op default) |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | Hook (no-op default) |
| 136 | `LateMergeEquivalentConditionalFlow` | Late conditional merge |
| 137 | `LateExpansionUnsupportedOpsMid` | Late unsupported mid |
| 138 | `OriSplitHighPressureLiveRanges` | High-pressure splitting |
| 139–158 | *(architecture-specific)* | Arch-specific fixups |

Phases 132–138 handle late-breaking transformations that must run after the Mercury backend but before finalization. `OriSplitHighPressureLiveRanges` (phase 138) is a last-resort live range splitter that fires when register pressure exceeds hardware limits after the main allocation pass.

Phases 139–158 are 20 additional registered slots — and they **are** named in the static name table at `off_22BD0C0`, which holds all 159 entries (indices 0–158). There is no "139 named + 20 unnamed" split; every phase ID resolves to a name through the table. Of these tail slots, IDs 139–156 (e.g. `ProcessO0WaitsAndSBs`=139 … `DumpNVuCodeHex`=156) are part of the default 157-entry schedule, while IDs 157 (`DebuggerBreak`) and 158 (`NOP`) are registered but dispatched only via the recipe path. Vtable addresses for the tail run `off_22BEB08`..`off_22BEE78`.

## Optimization Level Gating

### AdvancedPhase Hook Points

Sixteen phases serve as conditional extension points. Their `isNoOp()` returns `true` by default — which suppresses their Before/After banner but does **not** remove them from the schedule: their `execute()` still runs every compile and returns early until a backend installs a non-stub override. Architecture backends and optimization-level configurations override the vtable (or the per-architecture profile slot the stub dispatches to) to activate these hooks:

| Phase | Name | Gate Location |
|---|---|---|
| 4 | `AdvancedPhaseBeforeConvUnSup` | Before unsupported-op conversion |
| 7 | `AdvancedPhaseAfterConvUnSup` | After unsupported-op conversion |
| 47 | `AdvancedPhaseEarlyEnforceArgs` | Before argument enforcement |
| 77 | `AdvancedPhaseLateConvUnSup` | Late unsupported-op boundary |
| 82 | `AdvancedPhaseBackPropVReg` | Before backward copy prop |
| 89 | `AdvancedPhaseSetRegAttr` | Before register attr setting |
| 92 | `AdvancedPhaseAfterSetRegAttr` | After register attr setting |
| 97 | `AdvancedPhasePreSched` | Before scheduling |
| 101 | `AdvancedPhaseAllocReg` | Register allocation driver |
| 104 | `AdvancedPhasePostExpansion` | Before post-RA expansion (dispatches to worker phase 127 `PostExpansion`) |
| 106 | `AdvancedPhasePostSched` | Before post-scheduling (gates worker phase 110 `PostSchedule`; writes `ctx+1552=14`) |
| 111 | `AdvancedPhasePostFixUp` | Before post-fixup worker (phase 140 `PostFixUp`); writes `ctx+1552=20` |
| 115 | `AdvancedScoreboardsAndOpexes` | Full scoreboard analysis |
| 127 | `AdvancedPhaseOriPhaseEncoding` | Phase encoding hook |
| 134 | `AdvancedPhaseAfterMidExpansion` | After mid-expansion |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | Late sync expansion |

The pattern is consistent: AdvancedPhase hooks bracket major pipeline stages, allowing backends to insert target-specific transformations without altering the fixed phase ordering. Phase 101 (`AdvancedPhaseAllocReg`) is notable because register allocation itself is entirely driven through this hook — the base pipeline has no hardcoded allocator.

### O0 vs O1+ Behavior

At `-O0`, the pipeline skips most optimization phases via their individual `isNoOp()` checks. The critical difference is in scoreboard generation:

- **`-O1` and above**: Phase 115 (`AdvancedScoreboardsAndOpexes`) runs the full dependency analysis using `sub_A36360` (52KB control word encoder) and `sub_A23CF0` (54KB DAG list scheduler heuristic). Phase 116 is a no-op.
- **`-O0`**: Phase 115 is a no-op. Phase 116 (`ProcessO0WaitsAndSBs`) inserts conservative stall counts and wait barriers — every instruction gets the maximum stall, and barriers are placed at every potential hazard point. This produces correct but slow code.

Individual phases also check the optimization level internally via the compilation context. The scheduling infrastructure (`sub_8D0640`) reads the opt-level via `sub_7DDB50` and selects between forward-pass scheduling (opt-level <= 2, register-pressure-reducing) and reverse-pass scheduling (opt-level > 2, latency-hiding).

## The OCG-Knob Mechanism

Every control-flow decision the optimizer driver makes is gated by an **OCG knob**. An OCG knob is one 72-byte entry in a flat array at `config+0x48`, where `config` is the OCG config object reached as `ctx+0x680` (equivalently the options view `ctx+0x1664`). The config offset of knob `N` is exactly `N*72` (`0x48`-relative). Each 72-byte entry has a presence/type tag byte at `entry[0]` (`0` = unset, `5` = "set, value present") and a value pointer at `entry[8]` (read when the tag is `5`).

Two inlined leaf accessors recur, compared by address throughout the driver:

| Accessor | Address | Semantics |
|---|---|---|
| bool | `sub_6614A0` | `knobs = obj+0x48; e = knobs + idx*72; return e[0] != 0` |
| value | `sub_6614C0` | same indexing; `if (e[0] == 5) return e[8]; else 0` |

When the live accessor object's vtable slot does not match these fast leaves, the driver does a virtual call `(*slot)(obj, idx)` with the knob index. The knobs the driver and optimizer actually consult:

| Knob | config off | Read by | Role |
|---|---|---|---|
| 249 | `0x4608` | `sub_C62720` @0xc62786 | phase-wise compile-stats flag → sets PhaseManager+0x48 (Before/After/Summary timing report on/off) |
| 298 | `0x53D0` | `sub_7FB6C0` @0x7fb6ef | recipe/named-phases selector → takes the `sub_9F63D0` path instead of the default |
| 298 | `0x53D8` | `sub_9F4040` @0x9f4258 | recipe STRING value (tag==5, ptr at entry+8) → tokenizer `sub_798B60` |
| 391 | `0x6DF8` | `sub_C62720` @0xc6297d | NvOptRecipe present? → wires the `ApplyNvOptRecipes` (phase ID 1) object |
| 391 | `0x6E00` | `sub_C62720` @0xc62ac9 | NvOptRecipe value → recipe-apply object +312 |
| 499 | `0x8C58` | `sub_7DDB50` | "disable optimization" → forces opt level 1 past a budget counter |

All offsets `id*72` are byte-exact (`298*72 = 0x53D0`, `391*72 = 0x6DF8`, `249*72 = 0x4608`, `499*72 = 0x8C58`, remainder 0).

## Recipe / NamedPhases Override (OCG Knob 298)

The recipe mechanism allows replacement of the default 157-phase schedule with a custom sequence, used for debugging and engineering investigation. It is gated by an internal knob, not a documented user-facing ptxas flag; default compiles never reach it.

### Activation

The pipeline orchestrator (`sub_7FB6C0`) reads OCG knob 298 (presence flag at `config+0x53D0`). When clear (the default), it builds the PhaseManager and dispatches the default 157-entry schedule. When set, it delegates to `sub_9F63D0`, which builds a custom schedule and feeds it to the **same** dispatch loop:

```c
// sub_7FB6C0 -- simplified
void orchestrate(Context* ctx) {
    ocg = *(ctx + 0x680);                       // OCG knob/config object
    if (knob_active(ocg, 298)) {                // config+0x53D0 != 0
        recipe_run(ctx);                        // sub_9F63D0 (custom schedule)
    } else {
        PhaseManager pm;
        PhaseManager_construct(&pm, ctx);       // sub_C62720 -> registers 159
        auto [order, count] = get_default_order(); // sub_C60D20 -> (&0x22BEEA0, 157)
        dispatch(&pm, order, count);            // sub_C64F70 -> runs IDs 0..156
        PhaseManager_destroy(&pm);              // sub_C61B20
    }
    // ... teardown ctx+1880 / +1872 / +1864 scratch objects ...
}
```

### Recipe path and DSL

`sub_9F63D0` builds the PhaseManager (same `sub_C62720`), seeds a 256-int buffer with `[0] = 158` (NOP), then calls `sub_9F4040` to compute a custom schedule and length. `sub_9F4040` starts from the default 157-identity schedule (`sub_C60D20()`), tokenizes the recipe string (`sub_798B60`) into parallel name/value arrays, and applies a small DSL. Explicit phase IDs are **clamped to `[0, 159]`** (`if (v121 > 159) v121 = 159;`), and unknown phase names map to `158`/NOP via `sub_C641D0`. This is the **only** path that can schedule IDs 157/158.

| Token | Kind | Effect |
|---|---|---|
| `NamedPhases` | phase-list | replace the schedule with the explicit name list; each name → ID via `sub_C641D0` (miss/`-` → 158) |
| `p%d` | index-select | select phases by numeric index (`sprintf "p%d", i`); clamped to `[0, 159]` |
| `shuffle` | permute | seeded pairwise rotation pass over the schedule slots (reps-controlled) |
| `reps` | repeat-count | number of shuffle iterations (≤ 256) |
| `swap1`..`swap6` | swap-slot | swap a pair of schedule slots; each variant uses an independent base offset |
| `dce1`..`dce3` | inject-phase | insert `OriPerformLiveDead` (DCE; phase IDs 18/38/70/99 family) at the counter position |
| `cpy1`..`cpy3` | inject-phase | insert `OriCopyProp` (phase ID 22) at the counter position |

OCG knob 297 is pulsed (write-style virtual call) once per applied recipe element — a diagnostic "recipe-applied" event/counter. Every value is parsed with `strtol` and bounds-clamped to `[0, 256]` or `[0, 159]`.

### NvOptRecipe (Knob 391) — the user-facing counterpart

When OCG knob 391 is present, the PhaseManager ctor reads its value (`391*72+8 = +0x6E00`) and constructs a 440-byte recipe-apply object, stashing the recipe pointer at its `+312`. That object is consumed by phase ID 1, `ApplyNvOptRecipes` — the second phase in the default schedule. This is the supported path for applying recipe transforms without diverting the whole pipeline.

### Pass-Disable Integration

Individual passes can be disabled without reordering the pipeline. The check function `sub_799250` (IsPassDisabled, 68 bytes) performs a case-insensitive substring match against the `PTXAS_DISABLED_PASSES` string at context offset 13328:

```c
// sub_799250 -- simplified
bool is_pass_disabled(Context* ctx, const char* pass_name) {
    if (ctx->pass_disable_flag == 0) return false;  // offset 13320
    if (ctx->pass_disable_flag == 5) {
        return strcasestr(ctx->pass_disable_string, pass_name);  // offset 13328
    }
    return false;
}
```

This check is called from 16+ sites across the codebase, guarding passes like `LoopMakeSingleEntry` and `SinkCodeIntoBlock`. A more thorough variant (`sub_7992A0`, IsPassDisabledFull) uses FNV-1a hashing for function-specific override tables.

## PhaseManager Data Structures

### PhaseManager Object (~112 bytes)

```text
Offset  Type       Field
------  ----       -----
+0      int64      compilation_unit pointer
+8      int64*     allocator
+16     void*      sorted_name_table (for binary search)
+24     int32      sorted_name_count
+28     int32      sorted_name_capacity
+32     int64*     allocator_copy
+40     void*      phase_list (array of 16-byte Phase entries)
+48     int32      phase_list_count
+52     int32      phase_list_capacity
+56     int64      nvopt_recipe_ptr (NvOptRecipe sub-manager, or NULL)
+64     int64      (reserved)
+72     bool       timing_enabled (from options[17928])
+76     int32      (flags)
+80     bool       flag_byte
+88     int64*     timing_allocator
+96     void*      phase_name_raw_table
+104    int32      phase_name_raw_count
+108    int32      phase_name_raw_capacity
```

### Timing Record (32 bytes)

```text
Offset  Type       Field
------  ----       -----
+0      int32      phase_index (-1 = sentinel)
+8      int64      phase_name_or_magic (0x2030007 = sentinel)
+16     int64      timing_value
+24     int32      memory_flags
```

### NvOptRecipe Sub-Manager (440 bytes, at `PhaseManager+56`)

Created when option 391 is set. Contains timing records with 584-byte stride, a hash table for recipe lookup, sorted arrays, and ref-counted shared lists. The sub-manager inherits the phase chain from the previous execution context, enabling recipe-based pipeline modification across compilation units.

## Function Map

| Address | Size | Identity |
|---|---|---|
| `sub_C60D20` | 11 B | Default order accessor — returns `(&0x22BEEA0, 157)` in `(rax, rdx)` |
| `sub_C60D30` | 3554 B | Phase factory (159-case switch) |
| `sub_C60BD0` | 334 B | Multi-function phase invoker |
| `sub_C61B20` | 1753 B | PhaseManager destructor |
| `sub_C62200` | 888 B | Pool consumption reporter |
| `sub_C62580` | 253 B | Timing record array resizer (1.5x growth) |
| `sub_C62640` | 223 B | Phase list resizer (1.5x growth) |
| `sub_C62720` | 4734 B | PhaseManager constructor |
| `sub_C639A0` | 1535 B | Case-insensitive quicksort (median-of-3) |
| `sub_C63FA0` | 556 B | Phase name table sort/rebuild |
| `sub_C641D0` | 305 B | Phase name-to-index binary search |
| `sub_C64310` | 3168 B | Per-phase timing reporter |
| `sub_C64F70` | 1455 B | Phase dispatch loop |
| `sub_7FB6C0` | 1193 B | Pipeline orchestrator (option 298 gate) |
| `sub_798B60` | 1776 B | NamedPhases::ParsePhaseList |
| `sub_799250` | 68 B | IsPassDisabled (substring check) |
| `sub_7992A0` | 894 B | IsPassDisabledFull (FNV-1a hash) |
| `sub_9F4040` | 9093 B | NamedPhases::parseAndBuild |
| `sub_9F63D0` | 342 B | NamedPhases::run |
| `sub_9F1A90` | 6310 B | MercConverter main pass |
| `sub_9F3340` | ~7 KB | MercConverter orchestrator |
| `sub_9ED2D0` | ~25 KB | MercConverter opcode dispatch |

## Diagnostic Strings

| String | Location | Trigger |
|---|---|---|
| `"All Phases Summary"` | `sub_C64F70` | End of dispatch loop (timing enabled) |
| `"[Pool Consumption = "` | `sub_C62200` | End of dispatch loop (timing enabled) |
| `"  ::  "` | `sub_C64310` | Per-phase timing line |
| `"[Total "`, `"[Freeable "`, `"[Freeable Leaked "` | `sub_C64310` | Memory delta columns |
| `"Before "`, `"After "` | `sub_C64F70` | Phase execution markers |
| `"NamedPhases"` | `sub_9F4040` | NamedPhases config parsing |
| `"shuffle"`, `"swap1"`..`"swap6"` | `sub_9F4040` | NamedPhases manipulation keywords |
| `"After MercConverter"` | near `sub_9F3340` | Post-MercConverter diagnostic |
| `"CONVERTING"` | `sub_9EF5E0` | During MercConverter lowering |
| `"Internal compiler error."` | `sub_9EB990` | ICE assertion (3 sites) |

## Cross-References

- [Phase Manager Infrastructure](../passes/phase-manager.md) — detailed PhaseManager internals
- [Pass Inventory & Ordering](../passes/index.md) — per-pass documentation index
- [GeneralOptimize Bundles](../passes/general-optimize.md) — compound optimization passes
- [Scheduler Architecture](../scheduling/overview.md) — scheduling phases 97–116
- [Mercury Encoder](../codegen/mercury.md) — Mercury backend phases 117–122
- [Scoreboards & Dependency Barriers](../scheduling/scoreboards.md) — control word generation
- [Optimization Levels](../config/opt-levels.md) — O-level gating details
- [DUMPIR & NamedPhases](../config/dumpir.md) — NamedPhases configuration reference
- [Memory Pool Allocator](../infra/memory-pools.md) — pool allocator used by phase objects
