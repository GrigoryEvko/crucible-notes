# Pass Inventory & Ordering

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The ptxas compilation pipeline consists of exactly 159 phases, executed in a fixed order determined by a static index table at `0x22BEEA0`. Every compilation traverses the same sequence -- phase skipping is handled per-phase via `isNoOp()` virtual method overrides, not by reordering the table. This page is the definitive inventory of all 159 phases: their index, name, category, one-line description, and cross-references to detailed documentation where available.

All 159 phases have names in the static name table at `off_22BD0C0` (159 entries, indexed 0--158). The factory switch at `sub_C60D30` allocates each phase as a 16-byte polymorphic object with a 5-slot vtable: `execute()` at +0, `getIndex()` at +8 (returns the factory/table index), and `isNoOp()` at +16 (returns 0 for active phases, 1 for phases skipped by default). Slots +24 and +32 are NULL.

| | |
|---|---|
| **Total phases** | 159 (indices 0--158) |
| **Named (static table)** | 159 (all have entries in `off_22BD0C0`) |
| **Late-pipeline phases** | 20 (indices 139--158, added after the original 0--138 design) |
| **Gate passes (AdvancedPhase)** | 17 conditional hooks |
| **Update passes** | 9 data-structure refresh passes (6 in main table + 3 in static name table, not yet positioned) |
| **Report passes** | 10 diagnostic/dump passes (9 in main table + 1 in static name table, not yet positioned) |
| **GeneralOptimize instances** | 6 compound optimization bundles |
| **Liveness/DCE instances** | 5 (including EarlyOriSimpleLiveDead) |
| **LICM instances** | 4 |
| **Pipeline infrastructure** | [Phase Manager](phase-manager.md), [Optimization Pipeline](../pipeline/optimizer.md) |

## Phase Categories

Each phase is tagged with one of 10 categories. These are not present in the binary -- they are an analytical classification applied during reverse engineering.

| Tag | Meaning | Count |
|---|---|---|
| **Validation** | Checks IR structural correctness, catches illegal patterns | 3 |
| **Lowering** | Converts unsupported ops, expands macros, legalizes IR | 14 |
| **Optimization** | Transforms IR to improve performance (DCE, CSE, LICM, etc.) | 68 |
| **Analysis** | Computes information consumed by later passes (liveness, CFG) | 6 |
| **Reporting** | Dumps IR, statistics, or memory usage for debugging | 9 |
| **Scheduling** | Instruction scheduling, sync insertion, WAR fixup | 8 |
| **RegAlloc** | Register allocation and related fixups | 6 |
| **Encoding** | Mercury SASS encoding, expansion, microcode generation | 9 |
| **Cleanup** | Post-transformation updates, NOP removal, block layout | 13 |
| **Gate** | Conditional hooks (`AdvancedPhase*`) -- no-op by default | 17 |

Phases 139--158 are late-pipeline phases covering Mercury encoding, scoreboards, register map computation, diagnostics, and a terminal NOP. They have the same vtable infrastructure as phases 0--138 and are fully named in the static table.

## Numbering Discrepancy -- Complete Wiki-to-Binary Mapping

> **Warning:** The wiki phase numbers 0--138 use a compressed scheme that omits 23 binary indices from the contiguous range 0--139. Of these 23, seven are displaced to wiki positions 132--138, and 16 have no wiki number at all. The divergence begins at binary index 8 (`UpdateAfterConvertUnsupportedOps`, skipped in the wiki) and accumulates to a delta of +23 by wiki phase 116. Phases 140--158 match their binary indices. Every cross-reference on this page and 40+ linked pages uses wiki numbers, NOT binary indices. Use the table below to convert.

### Complete Binary-to-Wiki Translation Table

**Reading guide:** `W#` = wiki phase number used on this page. Rows marked **SKIP** have no wiki number (16 phases). Rows marked **DISP** are displaced to wiki 132--138 (7 phases). Delta = binary index minus wiki number.

| Bin | Phase Name | W# | D | | Bin | Phase Name | W# | D |
|--:|---|--:|--:|---|--:|---|--:|--:|
| 0 | 0 | `OriCheckInitialProgram` | 0 | 0 | | 80 | `OriDoRemat` | 69 | +11 |
| 1 | 1 | `ApplyNvOptRecipes` | 1 | 0 | | 81 | `OriPropagateVaryingSecond` | 70 | +11 |
| 2 | 2 | `PromoteFP16` | 2 | 0 | | 82 | `OptimizeSyncInstructions` | 71 | +11 |
| 3 | 3 | `AnalyzeControlFlow` | 3 | 0 | | **83** | **`AdvPhLateExpandSync`** | **135** | **DISP** |
| 4 | 4 | `AdvPhBeforeConvUnSup` | 4 | 0 | | 84 | `LateExpandSyncInstructions` | 72 | +12 |
| 5 | 5 | `ConvertUnsupportedOps` | 5 | 0 | | 85 | `ConvertAllMovPhiToMov` | 73 | +12 |
| 6 | 6 | `SetControlFlowOpLastInBB` | 6 | 0 | | 86 | `ConvertToUniformReg` | 74 | +12 |
| 7 | 7 | `AdvPhAfterConvUnSup` | 7 | 0 | | 87 | `LateArchOptimizeFirst` | 75 | +12 |
| **8** | **`UpdateAfterConvUnSupOps`** | **132** | **DISP** | | 88 | `UpdateAfterOptimize` | 76 | +12 |
| 9 | 10 | `OriCreateMacroInsts` | 8 | +1 | | 89 | `AdvPhLateConvUnSup` | 77 | +12 |
| 10 | 11 | `ReportInitialRepresentation` | 9 | +1 | | 90 | `LateExpUnSupportedOps` | 78 | +12 |
| 11 | 12 | `EarlyOriSimpleLiveDead` | 10 | +1 | | **91** | **`LateMergeEquivCondFlow`** | **136** | **DISP** |
| 12 | 13 | `ReplaceUniformsWithImm` | 11 | +1 | | 92 | `OriHoistInvariantsLate2` | 79 | +13 |
| 13 | 14 | `OriSanitize` | 12 | +1 | | **93** | **`LateExpUnSupOpsMid`** | **137** | **DISP** |
| 14 | 16 | `GeneralOptimizeEarly` | 13 | +1 | | 94 | `ExpandJmxComputation` | 80 | +14 |
| **15** | **`MergeEquivCondFlow`** | **133** | **DISP** | | 95 | `LateArchOptimizeSecond` | 81 | +14 |
| 16 | 18 | `DoSwitchOptFirst` | 14 | +2 | | 96 | `AdvPhBackPropVReg` | 82 | +14 |
| 17 | 19 | `OriBranchOpt` | 15 | +2 | | 97 | `OriBackCopyPropagate` | 83 | +14 |
| 18 | 20 | `OriPerformLiveDeadFirst` | 16 | +2 | | **98** | **`OriSplitHiPressLR`** | **138** | **DISP** |
| 19 | 21 | `OptimizeBindlessHeaderLoads` | 17 | +2 | | 99 | `OriPerformLiveDeadFourth` | 84 | +15 |
| 20 | 23 | `OriLoopSimplification` | 18 | +2 | | 100 | `OriPropagateGmma` | 85 | +15 |
| 21 | 24 | `OriSplitLiveRanges` | 19 | +2 | | 101 | `InsertPseudoUseDefConvUR` | 86 | +15 |
| **22** | **`OriCopyProp`** | -- | **SKIP** | | 102 | `FixupGmmaSequence` | 87 | +15 |
| 23 | 26 | `PerformPGO` | 20 | +3 | | **103** | **`LateEnforceArgRestr`** | -- | **SKIP** |
| 24 | 27 | `OriStrengthReduce` | 21 | +3 | | 104 | `OriHoistInvariantsLate3` | 88 | +16 |
| 25 | 28 | `OriLoopUnrolling` | 22 | +3 | | 105 | `AdvPhSetRegAttr` | 89 | +16 |
| 26 | 29 | `GenerateMovPhi` | 23 | +3 | | 106 | `OriSetRegisterAttr` | 90 | +16 |
| 27 | 30 | `OriPipelining` | 24 | +3 | | 107 | `OriCalcDependantTex` | 91 | +16 |
| 28 | 31 | `StageAndFence` | 25 | +3 | | 108 | `AdvPhAfterSetRegAttr` | 92 | +16 |
| 29 | 33 | `OriRemoveRedundantBarriers` | 26 | +3 | | 109 | `LateExpUnSupportedOps2` | 93 | +16 |
| 30 | 34 | `AnalyzeUniformsForSpec` | 27 | +3 | | 110 | `FinalInspectionPass` | 94 | +16 |
| 31 | 35 | `SinkRemat` | 28 | +3 | | 111 | `SetAfterLegalization` | 95 | +16 |
| **32** | **`OptimizeNaNOrZero`** | -- | **SKIP** | | 112 | `ReportBeforeScheduling` | 96 | +16 |
| 33 | 38 | `GeneralOptimize` | 29 | +4 | | 113 | `AdvPhPreSched` | 97 | +16 |
| 34 | 39 | `DoSwitchOptSecond` | 30 | +4 | | **114** | **`ScheduleInstructions`** | -- | **SKIP** |
| 35 | 40 | `OriLinearReplacement` | 31 | +4 | | **115** | **`UpdateAfterSchedInstr`** | -- | **SKIP** |
| 36 | 42 | `CompactLocalMemory` | 32 | +4 | | 116 | `BackPropagateVEC2D` | 98 | +18 |
| **37** | **`ConvMemToRegOrUniform`** | -- | **SKIP** | | 117 | `OriDoSyncronization` | 99 | +18 |
| 38 | 44 | `OriPerformLiveDeadSecond` | 33 | +5 | | **118** | **`UpdateAfterOriDoSync`** | -- | **SKIP** |
| 39 | 45 | `ExtractShaderConstsFirst` | 34 | +5 | | 119 | `ApplyPostSyncWars` | 100 | +19 |
| 40 | 46 | `OriHoistInvariantsEarly` | 35 | +5 | | **120** | **`ReportBeforeRegAlloc`** | -- | **SKIP** |
| **41** | **`Vectorization`** | -- | **SKIP** | | 121 | `AdvPhAllocReg` | 101 | +20 |
| 42 | 48 | `EmitPSI` | 36 | +6 | | **122** | **`AllocateRegisters`** | -- | **SKIP** |
| 43 | 49 | `GeneralOptimizeMid` | 37 | +6 | | 123 | `ReportAfterRegAlloc` | 102 | +21 |
| 44 | 50 | `OptimizeNestedCondBranches` | 38 | +6 | | **124** | **`UpdateAfterOriAllocReg`** | -- | **SKIP** |
| 45 | 51 | `ConvertVTGReadWrite` | 39 | +6 | | 125 | `Get64bRegComponents` | 103 | +22 |
| 46 | 53 | `DoVirtualCTAExpansion` | 40 | +6 | | 126 | `AdvPhPostExpansion` | 104 | +22 |
| 47 | 54 | `MarkAdditionalColdBlocks` | 41 | +6 | | **127** | **`PostExpansion`** | -- | **SKIP** |
| 48 | 55 | `ExpandMbarrier` | 42 | +6 | | 128 | `ApplyPostRegAllocWars` | 105 | +23 |
| 49 | 56 | `ForwardProgress` | 43 | +6 | | 129 | `AdvPhPostSched` | 106 | +23 |
| 50 | 58 | `OptimizeUniformAtomic` | 44 | +6 | | 130 | `OriRemoveNopCode` | 107 | +23 |
| 51 | 59 | `MidExpansion` | 45 | +6 | | 131 | `OptimizeHotColdInLoop` | 108 | +23 |
| **52** | **`AdvPhAfterMidExpansion`** | **134** | **DISP** | | 132 | `OptimizeHotColdFlow` | 109 | +23 |
| 53 | 61 | `GeneralOptimizeMid2` | 46 | +7 | | 133 | `PostSchedule` | 110 | +23 |
| 54 | 62 | `AdvPhEarlyEnforceArgs` | 47 | +7 | | 134 | `AdvPhPostFixUp` | 111 | +23 |
| 55 | 63 | `EnforceArgumentRestrictions` | 48 | +7 | | 135 | `PlaceBlocksInSourceOrder` | 112 | +23 |
| 56 | 64 | `GvnCse` | 49 | +7 | | 136 | `PostFixForMercTargets` | 113 | +23 |
| **57** | **`OriCommoning`** | -- | **SKIP** | | 137 | `FixUpTexDepBarAndSync` | 114 | +23 |
| 58 | 66 | `OriReassociateAndCommon` | 50 | +8 | | 138 | `AdvScoreboardsAndOpexes` | 115 | +23 |
| 59 | 67 | `ExtractShaderConstsFinal` | 51 | +8 | | 139 | `ProcessO0WaitsAndSBs` | 116 | +23 |
| 60 | 68 | `OriReplaceEquivMultiDefMov` | 52 | +8 | | 140--158 | *(19 late-pipeline phases)* | 140--158 | 0 |
| 61 | 70 | `OriPropagateVaryingFirst` | 53 | +8 | |
| 62 | 71 | `OriDoRematEarly` | 54 | +8 | |
| 63 | 72 | `LateExpansion` | 55 | +8 | |
| 64 | 74 | `SpeculativeHoistComInsts` | 56 | +8 | |
| 65 | 75 | `RemoveASTToDefaultValues` | 57 | +8 | |
| 66 | 76 | `GeneralOptimizeLate` | 58 | +8 | |
| 67 | 78 | `OriLoopFusion` | 59 | +8 | |
| 68 | 79 | `DoVTGMultiViewExpansion` | 60 | +8 | |
| **69** | **`OriSimpleLiveDead`** | -- | **SKIP** | |
| 70 | 81 | `OriPerformLiveDeadThird` | 61 | +9 | |
| 71 | 82 | `OriRemoveRedundantMultiDefMov` | 62 | +9 | |
| 72 | 84 | `OriDoPredication` | 63 | +9 | |
| **73** | **`LateVectorization`** | -- | **SKIP** | |
| 74 | 86 | `LateOriCommoning` | 64 | +10 | |
| 75 | 87 | `GeneralOptimizeLate2` | 65 | +10 | |
| 76 | 88 | `OriHoistInvariantsLate` | 66 | +10 | |
| **77** | **`SinkCodeIntoBlock`** | -- | **SKIP** | |
| 78 | 90 | `DoKillMovement` | 67 | +11 | |
| 79 | 92 | `DoTexMovement` | 68 | +11 | |

Phases 140--158 are identity-mapped (wiki number = binary index). The full list appears in [Stage 10](#stage-10----late-cleanup--late-pipeline-phases-132--158) below. Note that binary 139 (`ProcessO0WaitsAndSBs`) appears at BOTH wiki 116 (in Stage 7) and wiki 139 (in Stage 10).

### 16 Phases Missing from Wiki Numbering

These binary phases have no wiki number. All are valid `DUMPIR` and `DisablePhases` targets.

| Bin | Name | Cat | Pipeline Position |
|--:|---|---|---|
| 22 | 25 | `OriCopyProp` | Opt | Between `OriSplitLiveRanges` [21] and `PerformPGO` [23]; sub-pass of all 6 GeneralOptimize bundles |
| 32 | 36 | `OptimizeNaNOrZero` | Opt | Between `SinkRemat` [31] and `GeneralOptimize` [33]; NaN/zero constant folding |
| 37 | 43 | `ConvertMemoryToRegisterOrUniform` | Opt | Between `CompactLocalMemory` [36] and `OriPerformLiveDeadSecond` [38]; knob 487 gated |
| 41 | 47 | `Vectorization` | Opt | Between `OriHoistInvariantsEarly` [40] and `EmitPSI` [42]; load/store vectorization |
| 57 | 65 | `OriCommoning` | Opt | Between `GvnCse` [56] and `OriReassociateAndCommon` [58]; commoning sub-pass |
| 69 | 80 | `OriSimpleLiveDead` | Opt | Between `DoVTGMultiViewExpansion` [68] and `OriPerformLiveDeadThird` [70]; quick DCE |
| 73 | 85 | `LateVectorization` | Opt | Between `OriDoPredication` [72] and `LateOriCommoning` [74]; 2nd vectorization pass |
| 77 | 89 | `SinkCodeIntoBlock` | Opt | Between `OriHoistInvariantsLate` [76] and `DoKillMovement` [78]; code sinking |
| 103 | 125 | `LateEnforceArgumentRestrictions` | Lower | Between `FixupGmmaSequence` [102] and `OriHoistInvariantsLate3` [104]; late ABI enforcement |
| 114 | 137 | `ScheduleInstructions` | Sched | Worker for `AdvancedPhasePreSched` [113]; `sub_8D0640` (22 KB) |
| 115 | 138 | `UpdateAfterScheduleInstructions` | Clean | IR refresh after scheduling; between [113] and `BackPropagateVEC2D` [116] |
| 118 | 143 | `UpdateAfterOriDoSyncronization` | Clean | IR refresh after sync insertion [117]; between [117] and `ApplyPostSyncWars` [119] |
| 120 | 145 | `ReportBeforeRegisterAllocation` | Report | Diagnostic dump; between `ApplyPostSyncWars` [119] and `AdvPhAllocReg` [121] |
| 122 | 147 | `AllocateRegisters` | RegAlloc | Worker for `AdvancedPhaseAllocReg` [121]; canonical allocator entry |
| 124 | 149 | `UpdateAfterOriAllocateRegisters` | Clean | IR refresh after regalloc; between `ReportAfterRegAlloc` [123] and `Get64bRegComponents` [125] |
| 127 | 152 | `PostExpansion` | Lower | Worker for `AdvancedPhasePostExpansion` [126]; post-RA expansion |

### 7 Displaced Phases (Wiki 132--138)

These phases exist in the binary at early/mid positions but were assigned wiki numbers 132--138 when discovered after the initial compressed numbering was established. Their true execution order follows their binary index, not their wiki number.

| Wiki # | True Binary Index | Name | Executes Between |
|--:|--:|---|---|
| 132 | 8 | `UpdateAfterConvertUnsupportedOps` | `AdvPhAfterConvUnSup` [7] and `OriCreateMacroInsts` [9] |
| 133 | 15 | `MergeEquivalentConditionalFlow` | `GeneralOptimizeEarly` [14] and `DoSwitchOptFirst` [16] |
| 134 | 52 | `AdvancedPhaseAfterMidExpansion` | `MidExpansion` [51] and `GeneralOptimizeMid2` [53] |
| 135 | 83 | `AdvancedPhaseLateExpandSyncInstructions` | `OptimizeSyncInstructions` [82] and `LateExpandSyncInstructions` [84] |
| 136 | 91 | `LateMergeEquivalentConditionalFlow` | `LateExpansionUnsupportedOps` [90] and `OriHoistInvariantsLate2` [92] |
| 137 | 93 | `LateExpansionUnsupportedOpsMid` | `OriHoistInvariantsLate2` [92] and `ExpandJmxComputation` [94] |
| 138 | 98 | `OriSplitHighPressureLiveRanges` | `OriBackCopyPropagate` [97] and `OriPerformLiveDeadFourth` [99] |

## Gate Passes (AdvancedPhase)

Seventeen phase instances (16 unique gates, plus `AdvancedPhaseOriPhaseEncoding` appearing at both wiki 127 and 152) are conditional extension points whose `isNoOp()` returns `true` in the default vtable. They exist as insertion points for architecture backends and optimization-level overrides. When a specific SM target or `-O` level requires additional processing at a given pipeline position, the backend overrides the phase's vtable to provide a real `execute()` implementation.

Gate passes bracket major pipeline transitions. For example, phases 4 and 7 bracket `ConvertUnsupportedOps` (phase 5), allowing a backend to inject pre- and post-legalization logic without modifying the fixed phase table. Phase 101 (`AdvancedPhaseAllocReg`) is the most critical gate -- the entire register allocation subsystem is driven through this hook; the base pipeline contains no hardcoded allocator.

The naming convention is consistent: `AdvancedPhase` prefix followed by the pipeline position or action name. One exception is `AdvancedScoreboardsAndOpexes` (phase 115), which uses `Advanced` without `Phase`.

### Gate Pass Worker Correspondence

All 17 gate passes fall into three categories when activated by a backend override: (A) dispatch to a named worker phase from the static name table, (B) dispatch through an SM backend vtable slot at `ctx+0x630` or `ctx+0x640`, or (C) execute a pipeline progress counter thunk that writes `ctx+1552 = N`. Categories B and C have no separately named worker -- the gate IS the execute body.

| Gate (Wiki #) | Bin | Category | Worker / Dispatch Target | Evidence |
|---|--:|---|---|---|
| `AdvPhBeforeConvUnSup` (4) | 4 | C | `sub_C5F620`: `ctx+1552 = 1` | Pipeline progress thunk (7 bytes) |
| `AdvPhAfterConvUnSup` (7) | 7 | C | `sub_C5F5A0`: `ctx+1552 = 2` | Pipeline progress thunk (7 bytes) |
| `AdvPhAfterMidExpansion` (134) | 52 | C | `sub_C5EF80`: `ctx+1552 = 3` | Pipeline progress thunk; marks mid-expansion complete |
| `AdvPhEarlyEnforceArgs` (54) | 54 | C | `sub_C5EF30`: `ctx+1552 = 4` | Remat mode flag; `sub_A11060` checks `> 4` |
| `AdvPhLateExpandSync` (135) | 83 | B | `ctx+0x630` vtable dispatch | Brackets `LateExpandSyncInstructions` [84] |
| `AdvPhLateConvUnSup` (77) | 89 | B | `ctx+0x630` vtable+0x178 | Drives `LateExpansionUnsupportedOps` [90]; see [Late Legalization](late-legalization.md) |
| `AdvPhBackPropVReg` (82) | 96 | B | Arch-specific backward copy propagation | Next phase [97] writes `ctx+1552 = 9`; see [Copy Prop](copy-prop-cse.md) |
| `AdvPhPreSched` (97) | 113 | A | `ScheduleInstructions` [114] | `sub_8D0640` (22 KB), string `"ScheduleInstructions"` |
| `AdvPhAllocReg` (101) | 121 | A | `AllocateRegisters` [122] | String `"Please use -knob DUMPIR=AllocateRegisters"` at `sub_9714E0` |
| `AdvPhPostExpansion` (104) | 126 | A | `PostExpansion` [127] | Post-RA expansion dispatch |
| `AdvPhPostSched` (106) | 129 | B | Arch-specific post-scheduling cleanup | Adjacent to `PostSchedule` [133] (W110); `sub_C5E830` writes `ctx+1552 = 14` |
| `AdvPhPostFixUp` (111) | 134 | A | `PostFixUp` [140] | Target vtable+0x148 dispatch |
| `AdvScoreboardsAndOpexes` (115) | 138 | B | `sub_A36360` (52 KB) + `sub_A23CF0` (54 KB) | Control word gen + DAG scheduler; O1+ only |
| `AdvPhSetRegAttr` (89) | 105 | B | Arch-specific register attribute config | Precedes `OriSetRegisterAttr` [106] |
| `AdvPhAfterSetRegAttr` (92) | 108 | B | Arch-specific post-reg-attr processing | Follows `OriSetRegisterAttr` [106], `OriCalcDependantTex` [107] |
| `AdvPhOriPhaseEncoding` (127) | 152 | C | `sub_C5E0B0`: `ctx+1552 = 21` | Pipeline progress thunk; marks encoding boundary |
| *(total: 4 type A, 7 type B, 5 type C = 16 gates)* | | | | |

**Type A** gates have a named worker phase in the static name table (valid `DUMPIR`/`NamedPhases` target). **Type B** gates dispatch through an architecture vtable slot; the worker code lives in the SM backend object, not in a separate named phase. **Type C** gates are degenerate -- their only effect is advancing the pipeline progress counter at `ctx+1552`, which downstream passes read via `*(ctx+1552) > N` guards.

See [Optimization Levels](../config/opt-levels.md) for per-gate activation rules.

## Update Passes

Nine phases refresh data structures invalidated by preceding transformations. Six are documented at specific wiki phase numbers; three additional update phases exist in the static name table but are not yet mapped to wiki phase numbers (see Numbering Discrepancy above):

| Phase | Name | Refreshes |
|---|---|---|
| 76 | 88 | `UpdateAfterOptimize` | Rebuilds IR metadata after the late optimization group |
| 125 | 150 | `UpdateAfterPostRegAlloc` | Rebuilds IR metadata after register allocation and post-RA fixups |
| 128 | 154 | `UpdateAfterFormatCodeList` | Rebuilds the code list after Mercury encoding reformats instructions |
| 132 | 8 | `UpdateAfterConvertUnsupportedOps` | Rebuilds IR metadata after late unsupported-op expansion |
| 150 | 150 | `UpdateAfterPostRegAlloc` | Late-pipeline duplicate: rebuilds IR metadata after post-RA processing (no-op by default) |
| 154 | 154 | `UpdateAfterFormatCodeList` | Late-pipeline duplicate: rebuilds IR data structures after FormatCodeList (no-op by default) |
| *(true 115)* | `UpdateAfterScheduleInstructions` | Refreshes IR after scheduling completes (omitted from compressed numbering) |
| *(true 118)* | `UpdateAfterOriDoSyncronization` | Refreshes IR after sync insertion (omitted from compressed numbering) |
| *(true 124)* | `UpdateAfterOriAllocateRegisters` | Refreshes IR after register allocation (omitted from compressed numbering) |

These are lightweight passes that call into the IR's internal consistency maintenance routines. They do not transform the IR -- they only update auxiliary data structures (liveness bitmaps, instruction lists, block layout caches) so that downstream passes see a coherent view. Phases 150 and 154 are late-pipeline duplicates whose `isNoOp()` returns 1 by default; they only activate when a backend requires a second update cycle. The three `*(true N)*` entries are in the static name table at the indicated indices but are not yet assigned wiki phase numbers.

## Report Passes

Ten phases produce diagnostic output. They are no-ops unless specific debug options are enabled (e.g., `--stat=phase-wise`, `DUMPIR`, `--keep`):

| Phase | Name | Output |
|---|---|---|
| 9 | 10 | `ReportInitialRepresentation` | Dumps the Ori IR immediately after initial lowering |
| 96 | 112 | `ReportBeforeScheduling` | Dumps the IR as it enters the scheduling/RA stage |
| 102 | 123 | `ReportAfterRegisterAllocation` | Dumps the IR after register allocation completes |
| *(true 120)* | `ReportBeforeRegisterAllocation` | Dumps IR before register allocation; omitted from compressed numbering (name at `0x22BD068`) |
| 126 | 151 | `ReportFinalMemoryUsage` | Prints memory pool consumption summary |
| 129 | 155 | `DumpNVuCodeText` | SASS text disassembly (`cuobjdump`-style) |
| 130 | 156 | `DumpNVuCodeHex` | Raw SASS hex dump |
| 151 | 151 | `ReportFinalMemoryUsage` | Late-pipeline duplicate: memory pool summary (no-op by default, `isNoOp=1`) |
| 155 | 155 | `DumpNVuCodeText` | Late-pipeline duplicate: SASS text disassembly; guarded by `ctx+0x598` and `ctx+0x740` |
| 156 | 156 | `DumpNVuCodeHex` | Late-pipeline duplicate: raw SASS hex dump; same guard as phase 155 |

Phase 131 (`DebuggerBreak`) is a development-only hook that triggers a breakpoint -- it is not a report pass per se, but serves a similar diagnostic purpose. Phase 157 is its late-pipeline counterpart (empty body in release builds).

## GeneralOptimize Bundles

The `GeneralOptimize*` passes are compound optimization bundles that run multiple small transformations (copy propagation, constant folding, algebraic simplification, dead code elimination) in a fixed-point iteration until no further changes occur. They appear at 6 positions throughout the pipeline to re-clean the IR after major transformations:

| Phase | Name | Position |
|---|---|---|
| 13 | 14 | `GeneralOptimizeEarly` | After initial setup, before loop passes |
| 29 | 33 | `GeneralOptimize` | After early loop/branch optimizations |
| 37 | 43 | `GeneralOptimizeMid` | After mid-level transformations |
| 46 | 53 | `GeneralOptimizeMid2` | After VTA/CTA/mbarrier expansion |
| 58 | 66 | `GeneralOptimizeLate` | After late expansion |
| 65 | 75 | `GeneralOptimizeLate2` | After predication and late commoning |

See [GeneralOptimize Bundles](general-optimize.md) for the sub-pass decomposition.

---

## O-Level Gating

Twenty-two phases have confirmed optimization-level gates. The **O-Level** column in the table below annotates every phase where the activation threshold has been verified from decompiled `isNoOp()` methods or execute-function guards. Phases without an O-Level annotation run at all optimization levels (O0--O5). Threshold notation: `> N` means the phase requires `opt_level > N`; `== 0` means the phase is active only at O0.

See [Optimization Levels](../config/opt-levels.md) for the complete per-phase activation table, the O-level accessor (`sub_7DDB50`), and the NvOpt recipe system.

---

## Complete 159-Phase Table

### Stage 1 -- Initial Setup (Phases 0--13)

Program validation, recipe application, FP16 promotion, control flow analysis, unsupported-op conversion, macro creation, initial diagnostics.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 0 | 0 | `OriCheckInitialProgram` | Validation |  | Validates structural correctness of the initial Ori IR after PTX lowering |  |
| 1 | 1 | `ApplyNvOptRecipes` | Optimization |  | Applies NvOptRecipe transformations (option 391, 440-byte sub-manager) |  |
| 2 | 2 | `PromoteFP16` | Lowering |  | Promotes FP16 operations to FP32 where hardware lacks native support |  |
| 3 | 3 | `AnalyzeControlFlow` | Analysis |  | Builds the CFG: identifies loops, dominators, back edges |  |
| 4 | 4 | `AdvancedPhaseBeforeConvUnSup` | Gate |  | Hook before unsupported-op conversion; no-op by default |  |
| 5 | 5 | `ConvertUnsupportedOps` | Lowering |  | Replaces operations not natively supported on the target SM with equivalent sequences | [Late Legalization](late-legalization.md) |
| 6 | 6 | `SetControlFlowOpLastInBB` | Cleanup |  | Ensures control flow instructions are the final instruction in each basic block |  |
| 7 | 7 | `AdvancedPhaseAfterConvUnSup` | Gate |  | Hook after unsupported-op conversion; no-op by default |  |
| 8 | 9 | `OriCreateMacroInsts` | Lowering |  | Expands PTX-level macro instructions into Ori instruction sequences |  |
| 9 | 10 | `ReportInitialRepresentation` | Reporting |  | Dumps the Ori IR for debugging (no-op unless DUMPIR enabled) |  |
| 10 | 11 | `EarlyOriSimpleLiveDead` | Optimization |  | Quick early dead code elimination pass | [Liveness](liveness.md) |
| 11 | 12 | `ReplaceUniformsWithImm` | Optimization |  | Replaces uniform register reads with immediate constants where value is known | [Uniform Regs](uniform-regs.md) |
| 12 | 13 | `OriSanitize` | Validation |  | Validates IR consistency after initial setup transformations |  |
| 13 | 14 | `GeneralOptimizeEarly` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (early) | [GeneralOptimize](general-optimize.md) |

### Stage 2 -- Early Optimization (Phases 14--32)

Branch/switch optimization, loop canonicalization, strength reduction, software pipelining, SSA phi insertion, barrier optimization.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 14 | 16 | `DoSwitchOptFirst` | Optimization | **> 0** | Optimizes switch statements: jump table generation, case clustering (1st pass) | [Branch & Switch](branch-switch.md) |
| 15 | 17 | `OriBranchOpt` | Optimization | **> 0** | Branch folding, unreachable block elimination, conditional branch simplification | [Branch & Switch](branch-switch.md) |
| 16 | 18 | `OriPerformLiveDeadFirst` | Analysis |  | Full liveness analysis + dead code elimination (1st of 4 major instances) | [Liveness](liveness.md) |
| 17 | 19 | `OptimizeBindlessHeaderLoads` | Optimization |  | Hoists and deduplicates bindless texture header loads |  |
| 18 | 20 | `OriLoopSimplification` | Optimization | **4--5** | Canonicalizes loops: single entry, single back-edge, preheader insertion; aggressive loop peeling at O4+ | [Loop Passes](loop-passes.md) |
| 19 | 21 | `OriSplitLiveRanges` | Optimization |  | Splits live ranges at loop boundaries to reduce register pressure | [Liveness](liveness.md) |
| 20 | 23 | `PerformPGO` | Optimization |  | Applies profile-guided optimization data (block weights, branch probabilities) |  |
| 21 | 24 | `OriStrengthReduce` | Optimization |  | Replaces expensive operations (multiply, divide) with cheaper equivalents (shift, add) | [Strength Reduction](strength-reduction.md) |
| 22 | 25 | `OriLoopUnrolling` | Optimization | **> 1** | Unrolls loops based on trip count and register pressure heuristics | [Loop Passes](loop-passes.md) |
| 23 | 26 | `GenerateMovPhi` | Lowering |  | Inserts SSA phi nodes as `MOV.PHI` pseudo-instructions |  |
| 24 | 27 | `OriPipelining` | Optimization | **> 1** | Software pipelining: overlaps loop iterations to hide latency | [Loop Passes](loop-passes.md) |
| 25 | 28 | `StageAndFence` | Lowering |  | Inserts memory fence and staging instructions for coherence | [Sync & Barriers](sync-barriers.md) |
| 26 | 29 | `OriRemoveRedundantBarriers` | Optimization | **> 1** | Eliminates barrier instructions proven redundant by data-flow analysis | [Sync & Barriers](sync-barriers.md) |
| 27 | 30 | `AnalyzeUniformsForSpeculation` | Analysis |  | Identifies uniform values safe for speculative execution | [Uniform Regs](uniform-regs.md) |
| 28 | 31 | `SinkRemat` | Optimization | **> 1 / > 4** | Sinks instructions closer to uses and marks remat candidates; O2+: basic; O5: full cutlass | [Rematerialization](rematerialization.md) |
| 29 | 33 | `GeneralOptimize` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (mid-early) | [GeneralOptimize](general-optimize.md) |
| 30 | 34 | `DoSwitchOptSecond` | Optimization | **> 0** | Second switch optimization pass after loop/branch transformations | [Branch & Switch](branch-switch.md) |
| 31 | 35 | `OriLinearReplacement` | Optimization |  | Replaces branch-heavy patterns with linear (branchless) sequences |  |
| 32 | 36 | `CompactLocalMemory` | Optimization |  | Compacts local memory allocations by eliminating dead slots and reordering |  |

### Stage 3 -- Mid-Level Optimization (Phases 33--52)

GVN-CSE, reassociation, shader constant extraction, CTA/VTG expansion, argument enforcement.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 33 | 38 | `OriPerformLiveDeadSecond` | Analysis |  | Full liveness analysis + DCE (2nd instance, post-early-optimization cleanup) | [Liveness](liveness.md) |
| 34 | 39 | `ExtractShaderConstsFirst` | Optimization |  | Identifies uniform values loadable from constant memory instead of per-thread computation (1st pass) |  |
| 35 | 40 | `OriHoistInvariantsEarly` | Optimization |  | Loop-invariant code motion: hoists invariant computations out of loops (early) | [Loop Passes](loop-passes.md) |
| 36 | 42 | `EmitPSI` | Lowering |  | Emits PSI (Pixel Shader Input) interpolation setup for graphics shaders |  |
| 37 | 43 | `GeneralOptimizeMid` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (mid) | [GeneralOptimize](general-optimize.md) |
| 38 | 44 | `OptimizeNestedCondBranches` | Optimization | **> 0** | Simplifies nested conditional branches into flatter control flow | [Branch & Switch](branch-switch.md) |
| 39 | 45 | `ConvertVTGReadWrite` | Lowering |  | Converts vertex/tessellation/geometry shader read/write operations |  |
| 40 | 46 | `DoVirtualCTAExpansion` | Lowering |  | Expands virtual CTA operations into physical CTA primitives |  |
| 41 | 47 | `MarkAdditionalColdBlocks` | Analysis |  | Marks basic blocks as cold based on heuristics and profile data | [Hot/Cold](hot-cold.md) |
| 42 | 48 | `ExpandMbarrier` | Lowering |  | Expands `MBARRIER` pseudo-instructions into native barrier sequences | [Sync & Barriers](sync-barriers.md) |
| 43 | 49 | `ForwardProgress` | Lowering |  | Inserts instructions guaranteeing forward progress (prevents infinite stalls) |  |
| 44 | 50 | `OptimizeUniformAtomic` | Optimization |  | Converts thread-uniform atomic operations into warp-level reductions |  |
| 45 | 51 | `MidExpansion` | Lowering |  | Target-dependent mid-level expansion of operations before register allocation | [Late Legalization](late-legalization.md) |
| 46 | 53 | `GeneralOptimizeMid2` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (mid 2nd) | [GeneralOptimize](general-optimize.md) |
| 47 | 54 | `AdvancedPhaseEarlyEnforceArgs` | Gate |  | Hook before argument enforcement; no-op by default |  |
| 48 | 55 | `EnforceArgumentRestrictions` | Lowering |  | Enforces ABI restrictions on function arguments (register classes, alignment) |  |
| 49 | 56 | `GvnCse` | Optimization | **> 1** | Global value numbering combined with common subexpression elimination | [Copy Prop & CSE](copy-prop-cse.md) |
| 50 | 58 | `OriReassociateAndCommon` | Optimization |  | Reassociates expressions for better commoning opportunities, then eliminates commons | [Copy Prop & CSE](copy-prop-cse.md) |
| 51 | 59 | `ExtractShaderConstsFinal` | Optimization |  | Final shader constant extraction pass (after GVN may expose new constants) |  |
| 52 | 60 | `OriReplaceEquivMultiDefMov` | Optimization |  | Eliminates redundant multi-definition move instructions with equivalent sources |  |

### Stage 4 -- Late Optimization (Phases 53--77)

Predication, rematerialization, loop fusion, varying propagation, sync optimization, phi destruction, uniform register conversion.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 53 | 61 | `OriPropagateVaryingFirst` | Optimization |  | Propagates varying (non-uniform) annotations to identify divergent values (1st pass) |  |
| 54 | 62 | `OriDoRematEarly` | Optimization | **> 1** | Early rematerialization: recomputes cheap values near uses to reduce register pressure | [Rematerialization](rematerialization.md) |
| 55 | 63 | `LateExpansion` | Lowering |  | Expands operations that must be lowered after high-level optimizations | [Late Legalization](late-legalization.md) |
| 56 | 64 | `SpeculativeHoistComInsts` | Optimization |  | Speculatively hoists common instructions above branches |  |
| 57 | 65 | `RemoveASTToDefaultValues` | Cleanup |  | Removes AST (address space type) annotations that have been lowered to defaults |  |
| 58 | 66 | `GeneralOptimizeLate` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (late) | [GeneralOptimize](general-optimize.md) |
| 59 | 67 | `OriLoopFusion` | Optimization |  | Fuses adjacent loops with compatible bounds and no inter-loop dependencies | [Loop Passes](loop-passes.md) |
| 60 | 68 | `DoVTGMultiViewExpansion` | Lowering |  | Expands multi-view operations for vertex/tessellation/geometry shaders |  |
| 61 | 70 | `OriPerformLiveDeadThird` | Analysis |  | Full liveness analysis + DCE (3rd instance, post-late-optimization) | [Liveness](liveness.md) |
| 62 | 71 | `OriRemoveRedundantMultiDefMov` | Optimization |  | Removes dead multi-definition move instructions |  |
| 63 | 72 | `OriDoPredication` | Optimization | **> 1** | If-conversion: converts short conditional branches into predicated instructions | [Predication](predication.md) |
| 64 | 74 | `LateOriCommoning` | Optimization |  | Late commoning pass: eliminates common subexpressions exposed by predication | [Copy Prop & CSE](copy-prop-cse.md) |
| 65 | 75 | `GeneralOptimizeLate2` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (late 2nd) | [GeneralOptimize](general-optimize.md) |
| 66 | 76 | `OriHoistInvariantsLate` | Optimization |  | LICM: hoists loop-invariant code (late, after predication may expose new invariants) | [Loop Passes](loop-passes.md) |
| 67 | 78 | `DoKillMovement` | Optimization |  | Moves kill annotations closer to last use to improve register pressure |  |
| 68 | 79 | `DoTexMovement` | Optimization |  | Moves texture fetch instructions to minimize latency exposure |  |
| 69 | 80 | `OriDoRemat` | Optimization | **> 1** | Late rematerialization: recomputes values exposed by predication and fusion | [Rematerialization](rematerialization.md) |
| 70 | 81 | `OriPropagateVaryingSecond` | Optimization |  | Propagates varying annotations (2nd pass, after predication changes control flow) |  |
| 71 | 82 | `OptimizeSyncInstructions` | Optimization | **> 1** | Eliminates and simplifies synchronization instructions | [Sync & Barriers](sync-barriers.md) |
| 72 | 84 | `LateExpandSyncInstructions` | Lowering | **> 2** | Expands sync pseudo-instructions into final hardware sequences | [Sync & Barriers](sync-barriers.md) |
| 73 | 85 | `ConvertAllMovPhiToMov` | Lowering |  | Destroys SSA form: converts `MOV.PHI` instructions into plain `MOV` |  |
| 74 | 86 | `ConvertToUniformReg` | Optimization |  | Converts qualifying values from general registers (R) to uniform registers (UR) | [Uniform Regs](uniform-regs.md) |
| 75 | 87 | `LateArchOptimizeFirst` | Optimization |  | Architecture-specific late optimizations (1st pass) |  |
| 76 | 88 | `UpdateAfterOptimize` | Cleanup |  | Rebuilds IR metadata invalidated by the late optimization group |  |
| 77 | 89 | `AdvancedPhaseLateConvUnSup` | Gate |  | Hook at the late unsupported-op boundary; no-op by default |  |

### Stage 5 -- Legalization (Phases 78--96)

Late unsupported-op expansion, backward copy propagation, GMMA fixup, register attributes, final validation.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 78 | 90 | `LateExpansionUnsupportedOps` | Lowering |  | Expands remaining unsupported operations after all optimizations | [Late Legalization](late-legalization.md) |
| 79 | 92 | `OriHoistInvariantsLate2` | Optimization |  | LICM (late 2nd pass) after unsupported-op expansion | [Loop Passes](loop-passes.md) |
| 80 | 94 | `ExpandJmxComputation` | Lowering |  | Expands JMX (jump with index computation) pseudo-instructions |  |
| 81 | 95 | `LateArchOptimizeSecond` | Optimization |  | Architecture-specific late optimizations (2nd pass) |  |
| 82 | 96 | `AdvancedPhaseBackPropVReg` | Gate |  | Hook before backward copy propagation; no-op by default |  |
| 83 | 97 | `OriBackCopyPropagate` | Optimization |  | Backward copy propagation: propagates values backward through move chains | [Copy Prop & CSE](copy-prop-cse.md) |
| 84 | 99 | `OriPerformLiveDeadFourth` | Analysis |  | Full liveness analysis + DCE (4th instance, pre-legalization cleanup) | [Liveness](liveness.md) |
| 85 | 100 | `OriPropagateGmma` | Optimization |  | Propagates WGMMA accumulator values through the IR | [GMMA Pipeline](gmma-pipeline.md) |
| 86 | 101 | `InsertPseudoUseDefForConvUR` | Lowering |  | Inserts pseudo use/def instructions for uniform register conversion bookkeeping | [Uniform Regs](uniform-regs.md) |
| 87 | 102 | `FixupGmmaSequence` | Lowering |  | Fixes WGMMA instruction sequences for hardware ordering constraints | [GMMA Pipeline](gmma-pipeline.md) |
| 88 | 104 | `OriHoistInvariantsLate3` | Optimization |  | LICM (late 3rd pass) after GMMA fixup | [Loop Passes](loop-passes.md) |
| 89 | 105 | `AdvancedPhaseSetRegAttr` | Gate |  | Hook before register attribute setting; no-op by default |  |
| 90 | 106 | `OriSetRegisterAttr` | Analysis |  | Annotates registers with scheduling attributes (latency class, bank assignment) | [Scheduling](../scheduling/overview.md) |
| 91 | 107 | `OriCalcDependantTex` | Analysis |  | Computes texture instruction dependencies for scheduling |  |
| 92 | 108 | `AdvancedPhaseAfterSetRegAttr` | Gate |  | Hook after register attribute setting; no-op by default |  |
| 93 | 109 | `LateExpansionUnsupportedOps2` | Lowering |  | Second late unsupported-op expansion (catches ops exposed by GMMA/attr passes) | [Late Legalization](late-legalization.md) |
| 94 | 110 | `FinalInspectionPass` | Validation |  | Final IR validation gate: catches illegal patterns before irreversible scheduling/RA |  |
| 95 | 111 | `SetAfterLegalization` | Cleanup | **> 1** | Sets post-legalization flag on the compilation context |  |
| 96 | 112 | `ReportBeforeScheduling` | Reporting |  | Dumps IR before scheduling (no-op unless diagnostic options enabled) |  |

### Stage 6 -- Scheduling & Register Allocation (Phases 97--103)

Synchronization insertion, WAR fixup, register allocation, 64-bit register handling.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 97 | 113 | `AdvancedPhasePreSched` | Gate |  | Hook before scheduling; when active, dispatches to `ScheduleInstructions` (`sub_8D0640`, true table index 114) | [Scheduling](../scheduling/overview.md) |
| 98 | 116 | `BackPropagateVEC2D` | Optimization |  | Backward-propagates 2D vector register assignments |  |
| 99 | 117 | `OriDoSyncronization` | Scheduling | **> 1** | Inserts synchronization instructions (`BAR`, `DEPBAR`, `MEMBAR`) per GPU memory model | [Sync & Barriers](sync-barriers.md) |
| 100 | 119 | `ApplyPostSyncronizationWars` | Scheduling | **> 1** | Fixes write-after-read hazards exposed by sync insertion | [Sync & Barriers](sync-barriers.md) |
| 101 | 121 | `AdvancedPhaseAllocReg` | Gate |  | Register allocation driver hook; when active, dispatches to `AllocateRegisters` (true table index 122); `DUMPIR=AllocateRegisters` targets this | [RegAlloc Architecture](../regalloc/overview.md) |
| 102 | 123 | `ReportAfterRegisterAllocation` | Reporting |  | Dumps IR after register allocation (no-op unless diagnostic options enabled) |  |
| 103 | 125 | `Get64bRegComponents` | RegAlloc |  | Splits 64-bit register pairs into 32-bit components for architectures that require it | [RegAlloc Architecture](../regalloc/overview.md) |

### Stage 7 -- Post-RA & Post-Scheduling (Phases 104--116)

Post-expansion, NOP removal, hot/cold optimization, block placement, scoreboard generation.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 104 | 126 | `AdvancedPhasePostExpansion` | Gate |  | Hook after post-RA expansion; when active, dispatches to `PostExpansion` (true table index 127) |  |
| 105 | 128 | `ApplyPostRegAllocWars` | RegAlloc |  | Fixes write-after-read hazards exposed by register allocation |  |
| 106 | 129 | `AdvancedPhasePostSched` | Gate |  | Hook after post-scheduling; no-op by default |  |
| 107 | 130 | `OriRemoveNopCode` | Cleanup |  | Removes NOP instructions and dead code inserted as placeholders |  |
| 108 | 131 | `OptimizeHotColdInLoop` | Optimization |  | Separates hot and cold paths within loops for cache locality | [Hot/Cold](hot-cold.md) |
| 109 | 132 | `OptimizeHotColdFlow` | Optimization |  | Separates hot and cold paths at the function level | [Hot/Cold](hot-cold.md) |
| 110 | 133 | `PostSchedule` | Scheduling | **> 0** | Post-scheduling pass: finalizes instruction ordering | [Scheduling](../scheduling/overview.md) |
| 111 | 134 | `AdvancedPhasePostFixUp` | Gate |  | Hook after post-fixup; when active, dispatches to `PostFixUp` (phase 140, target vtable+0x148) |  |
| 112 | 135 | `PlaceBlocksInSourceOrder` | Cleanup |  | Determines final basic block layout in the emitted binary |  |
| 113 | 136 | `PostFixForMercTargets` | Encoding |  | Fixes up instructions for Mercury encoding requirements | [Mercury](../codegen/mercury.md) |
| 114 | 137 | `FixUpTexDepBarAndSync` | Scheduling |  | Fixes texture dependency barriers and sync instructions post-scheduling | [Scoreboards](../scheduling/scoreboards.md) |
| 115 | 138 | `AdvancedScoreboardsAndOpexes` | Gate | **> 0** | Full scoreboard generation: computes 23-bit control word per instruction (-O1+); no-op at -O0 | [Scoreboards](../scheduling/scoreboards.md) |
| 116 | 139 | `ProcessO0WaitsAndSBs` | Scheduling | **== 0** | Conservative scoreboard insertion for -O0: maximum stalls, barriers at every hazard | [Scoreboards](../scheduling/scoreboards.md) |

Scoreboard generation has two mutually exclusive paths. At `-O1` and above, phase 115 (`AdvancedScoreboardsAndOpexes`) runs the full dependency analysis using `sub_A36360` (52 KB) and `sub_A23CF0` (54 KB DAG list scheduler), while phase 116 is a no-op. At `-O0`, phase 115 is a no-op and phase 116 inserts conservative stall counts.

### Stage 8 -- Mercury Backend (Phases 117--122)

SASS instruction encoding, expansion, WAR generation, opex computation, microcode emission.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 117 | 142 | `MercEncodeAndDecode` | Encoding |  | Converts Ori instructions to Mercury encoding, then round-trip decodes for verification | [Mercury](../codegen/mercury.md) |
| 118 | 143 | `MercExpandInstructions` | Encoding |  | Expands pseudo-instructions into final SASS instruction sequences | [Mercury](../codegen/mercury.md) |
| 119 | 144 | `MercGenerateWARs1` | Encoding |  | Generates write-after-read hazard annotations (1st pass, pre-expansion) | [Mercury](../codegen/mercury.md) |
| 120 | 145 | `MercGenerateOpex` | Encoding |  | Generates "opex" (operation extension) annotations for each instruction | [Mercury](../codegen/mercury.md) |
| 121 | 146 | `MercGenerateWARs2` | Encoding |  | Generates WAR annotations (2nd pass, covers hazards introduced by expansion) | [Mercury](../codegen/mercury.md) |
| 122 | 147 | `MercGenerateSassUCode` | Encoding |  | Produces the final SASS microcode bytes (the actual binary encoding) | [Mercury](../codegen/mercury.md) |

"Mercury" is NVIDIA's internal name for the SASS encoding framework. WAR generation runs in two passes (119, 121) because instruction expansion in phase 118 can introduce new write-after-read hazards. The MercConverter infrastructure (`sub_9F1A90`, 35 KB) drives instruction-level legalization via a visitor pattern dispatched through `sub_9ED2D0` (25 KB opcode switch).

### Stage 9 -- Post-Mercury (Phases 123--131)

Register map computation, diagnostics, debug output.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 123 | 148 | `ComputeVCallRegUse` | RegAlloc |  | Computes register usage for virtual call sites |  |
| 124 | 149 | `CalcRegisterMap` | RegAlloc |  | Computes the final physical-to-logical register mapping emitted as EIATTR metadata | [RegAlloc Architecture](../regalloc/overview.md) |
| 125 | 150 | `UpdateAfterPostRegAlloc` | Cleanup |  | Rebuilds IR metadata after post-RA processing |  |
| 126 | 151 | `ReportFinalMemoryUsage` | Reporting |  | Prints memory pool consumption summary to stderr |  |
| 127 | 152 | `AdvancedPhaseOriPhaseEncoding` | Gate |  | Phase encoding hook; no-op by default |  |
| 128 | 154 | `UpdateAfterFormatCodeList` | Cleanup |  | Rebuilds the code list after Mercury encoding reformats instructions |  |
| 129 | 155 | `DumpNVuCodeText` | Reporting |  | Dumps human-readable SASS text disassembly |  |
| 130 | 156 | `DumpNVuCodeHex` | Reporting |  | Dumps raw SASS binary as hex |  |
| 131 | 157 | `DebuggerBreak` | Cleanup |  | Development hook: triggers a debugger breakpoint at this pipeline position |  |

### Stage 10 -- Late Cleanup & Late Pipeline (Phases 132--158)

Late merge operations, late unsupported-op expansion, high-pressure live range splitting, Mercury encoding pipeline, register map computation, diagnostics, and debug hooks.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 132 | 8 | `UpdateAfterConvertUnsupportedOps` | Cleanup |  | Rebuilds IR metadata after late unsupported-op conversion |  |
| 133 | 15 | `MergeEquivalentConditionalFlow` | Optimization |  | Merges basic blocks with equivalent conditional flow (tail merging) |  |
| 134 | 52 | `AdvancedPhaseAfterMidExpansion` | Gate |  | Hook after mid-level expansion; no-op by default |  |
| 135 | 83 | `AdvancedPhaseLateExpandSyncInstructions` | Gate |  | Hook for late sync instruction expansion; no-op by default |  |
| 136 | 91 | `LateMergeEquivalentConditionalFlow` | Optimization |  | Second conditional flow merge pass (catches cases exposed by late transforms) |  |
| 137 | 93 | `LateExpansionUnsupportedOpsMid` | Lowering |  | Mid-late unsupported-op expansion (between the two merge passes) | [Late Legalization](late-legalization.md) |
| 138 | 98 | `OriSplitHighPressureLiveRanges` | RegAlloc |  | Last-resort live range splitter when register pressure exceeds hardware limits | [RegAlloc Architecture](../regalloc/overview.md) |
| 139 | 139 | `ProcessO0WaitsAndSBs` | Scheduling | **== 0** | Conservative scoreboard insertion for `-O0`; inserts maximum wait counts at every hazard | [Scoreboards](../scheduling/scoreboards.md) |
| 140 | 140 | `PostFixUp` | Cleanup |  | Target-specific post-fixup dispatch (calls target vtable+0x148) |  |
| 141 | 141 | `MercConverter` | Encoding |  | Initial Mercury conversion: translates Ori instructions to Mercury format (`sub_9F3760`) | [Mercury](../codegen/mercury.md) |
| 142 | 142 | `MercEncodeAndDecode` | Encoding |  | Encode/decode round-trip verification of SASS binary encoding (`sub_18F21F0`) | [Mercury](../codegen/mercury.md) |
| 143 | 143 | `MercExpandInstructions` | Encoding |  | Expands Mercury pseudo-instructions into final SASS sequences; gated by `ctx+0x570` bit 5 | [Mercury](../codegen/mercury.md) |
| 144 | 144 | `MercGenerateWARs1` | Encoding |  | WAR hazard annotation (1st pass, pre-expansion); gated by `ctx+0x570` sign bit | [Mercury](../codegen/mercury.md) |
| 145 | 145 | `MercGenerateOpex` | Encoding |  | Generates operation extension annotations per instruction; gated by `ctx+0x570` bit 6 | [Mercury](../codegen/mercury.md) |
| 146 | 146 | `MercGenerateWARs2` | Encoding |  | WAR hazard annotation (2nd pass, covers hazards from expansion in phase 143) | [Mercury](../codegen/mercury.md) |
| 147 | 147 | `MercGenerateSassUCode` | Encoding |  | Final SASS microcode emission: produces the binary bytes for the ELF; gated by `ctx+0x571` bit 0 | [Mercury](../codegen/mercury.md) |
| 148 | 148 | `ComputeVCallRegUse` | RegAlloc |  | Computes register usage for virtual call sites (EIATTR metadata for indirect calls) |  |
| 149 | 149 | `CalcRegisterMap` | RegAlloc |  | Computes the final physical-to-logical register mapping; gated by `ctx+0x590` bit 1 | [RegAlloc Architecture](../regalloc/overview.md) |
| 150 | 150 | `UpdateAfterPostRegAlloc` | Cleanup |  | Rebuilds IR metadata after post-RA processing (no-op by default, `isNoOp=1`) |  |
| 151 | 151 | `ReportFinalMemoryUsage` | Reporting |  | Prints memory pool consumption summary (no-op by default, `isNoOp=1`) |  |
| 152 | 152 | `AdvancedPhaseOriPhaseEncoding` | Gate |  | Phase encoding gate; when active, sets `ctx+0x610` (`pipeline_progress`) `= 0x15` (21) to mark encoding boundary |  |
| 153 | 153 | `FormatCodeList` | Encoding |  | Formats the instruction list for ELF output; dispatches through `ctx+0x648` vtable+0x10 | [Mercury](../codegen/mercury.md) |
| 154 | 154 | `UpdateAfterFormatCodeList` | Cleanup |  | Rebuilds IR data structures after FormatCodeList reformats instructions (no-op by default, `isNoOp=1`) |  |
| 155 | 155 | `DumpNVuCodeText` | Reporting |  | Dumps human-readable SASS text disassembly; guarded by `ctx+0x598 > 0` and `ctx+0x740` non-null |  |
| 156 | 156 | `DumpNVuCodeHex` | Reporting |  | Dumps raw SASS binary as hex; same guard as phase 155 |  |
| 157 | 157 | `DebuggerBreak` | Cleanup |  | Development hook: convenient breakpoint location for pipeline debugging (empty body in release) |  |
| 158 | 158 | `NOP` | Cleanup |  | Terminal no-op sentinel; final phase in the 159-phase pipeline |  |

Phases 139--158 are 20 late-pipeline phases whose vtable pointers range from `off_22BEB80` to `off_22BEE78` (40-byte stride). All 20 have names in the static table at `off_22BD0C0` (159 entries, not 139). The vtable slot at +16 is `isNoOp()` (returns 0 for active phases, 1 for phases skipped by default); name resolution goes through the static table indexed by `getIndex()` at +8.

The Mercury phases (141--147) are gated by flag bits at `ctx+0x570`/`ctx+0x571`, allowing backends to selectively enable/disable encoding passes. WAR generation runs in two passes (144, 146) bracketing instruction expansion (143) because expansion can introduce new write-after-read hazards.

---

## Pipeline Ordering Notes

**Stage numbering.** The 10 stages on this page (Stage 1--10) subdivide the 159-phase OCG pipeline. They are distinct from the 6 timed phases in [Pipeline Overview](../pipeline/overview.md) (Parse, CompileUnitSetup, DAGgen, OCG, ELF, DebugInfo), which cover the entire program lifecycle. All 10 stages here fall within the single OCG timed phase.

**Identity ordering.** The default ordering table at `0x22BEEA0` (159 x `uint32`) is an identity mapping for indices 0--156: `exec[N] = factory[N]`. The last two entries are zero: `exec[157] = 0` and `exec[158] = 0`, mapping both slots back to factory index 0 instead of the expected 157 and 158. This is benign -- phase 157 (`DebuggerBreak`, empty body in release builds) and phase 158 (`NOP`, terminal sentinel) both have trivial `execute()` bodies, so the factory index they resolve through is irrelevant to pipeline behavior. For all practical purposes the factory index IS the execution order: phases execute in strict index order 0--158, and the two trailing zeros are don't-care slots. The original wiki analysis that placed phases 132--138 as "out-of-order slots" was based on a compressed 139-phase model that excluded 20 phases (see note below).

**Repeated passes.** Several transformations run at multiple pipeline positions because intervening passes expose new opportunities:

| Pass Family | Instances | Phases |
|---|---|---|
| `GeneralOptimize*` | 6 | 13, 29, 37, 46, 58, 65 |
| `OriPerformLiveDead*` | 4 | 16, 33, 61, 84 |
| `OriHoistInvariants*` | 4 | 35, 66, 79, 88 |
| `LateExpansionUnsupportedOps*` | 3 | 78, 93, 137 |
| `ExtractShaderConsts*` | 2 | 34, 51 |
| `OriPropagateVarying*` | 2 | 53, 70 |
| `OriDoRemat*` | 2 | 54, 69 |
| `DoSwitchOpt*` | 2 | 14, 30 |
| `LateArchOptimize*` | 2 | 75, 81 |
| `MergeEquivalentConditionalFlow` | 2 | 133, 136 |
| `MercGenerateWARs*` | 2 | 144, 146 |
| `UpdateAfterPostRegAlloc` | 2 | 125, 150 |
| `UpdateAfterFormatCodeList` | 2 | 128, 154 |
| `ReportFinalMemoryUsage` | 2 | 126, 151 |
| `DumpNVuCodeText` | 2 | 129, 155 |
| `DumpNVuCodeHex` | 2 | 130, 156 |
| `ComputeVCallRegUse` | 2 | 123, 148 |
| `CalcRegisterMap` | 2 | 124, 149 |
| `DebuggerBreak` | 2 | 131, 157 |
| `Vectorization`/`LateVectorization` | 2 | *(true 41, 73)* -- omitted from compressed numbering |
| `EnforceArgumentRestrictions`/`Late...` | 2 | 48 (wiki), *(true 103)* -- late variant omitted |

## Cross-References

- [Optimization Pipeline](../pipeline/optimizer.md) -- pipeline infrastructure, PhaseManager data structures, dispatch loop
- [Phase Manager Infrastructure](phase-manager.md) -- PhaseManager object layout, constructor, destructor, factory switch
- [GeneralOptimize Bundles](general-optimize.md) -- sub-pass decomposition of compound optimization passes
- [Branch & Switch Optimization](branch-switch.md) -- phases 14, 15, 30, 38
- [Loop Passes](loop-passes.md) -- phases 18, 22, 24, 35, 59, 66, 79, 88
- [Strength Reduction](strength-reduction.md) -- phase 21
- [Copy Propagation & CSE](copy-prop-cse.md) -- phases 49, 50, 64, 83
- [Predication](predication.md) -- phase 63
- [Rematerialization](rematerialization.md) -- phases 28, 54, 69
- [Liveness Analysis](liveness.md) -- phases 10, 16, 19, 33, 61, 84
- [Synchronization & Barriers](sync-barriers.md) -- phases 25, 26, 42, 71, 72, 99, 100, 114
- [Hot/Cold Partitioning](hot-cold.md) -- phases 41, 108, 109
- [GMMA/WGMMA Pipeline](gmma-pipeline.md) -- phases 85, 87
- [Uniform Register Optimization](uniform-regs.md) -- phases 11, 27, 74, 86
- [Late Expansion & Legalization](late-legalization.md) -- phases 5, 45, 55, 78, 93, 137
- [Register Allocator Architecture](../regalloc/overview.md) -- phases 101, 103, 105, 123, 124, 138, 148, 149
- [Scheduler Architecture](../scheduling/overview.md) -- phases 90, 97--100, 110
- [Scoreboards & Dependency Barriers](../scheduling/scoreboards.md) -- phases 114, 115, 116
- [Mercury Encoder](../codegen/mercury.md) -- phases 113, 117--122, 141--147, 153
- [Optimization Levels](../config/opt-levels.md) -- O-level gating of gate passes
- [DUMPIR & NamedPhases](../config/dumpir.md) -- user-specified phase targeting and reordering

## Key Functions

| Address | Size | Role | Confidence |
|---------|------|------|------------|
| `sub_C60D30` | -- | Phase factory switch; allocates each of the 159 phases as a 16-byte polymorphic object with a 5-slot vtable (`execute`, `getIndex`, `isNoOp`, NULL, NULL) | 0.92 |
| `sub_7DDB50` | 232B | Opt-level accessor; runtime gate called by 20+ pass execute functions to check opt-level threshold | 0.95 |
| `sub_A36360` | 52KB | Master scoreboard control word generator; per-opcode dispatch for phase 115 (`AdvancedScoreboardsAndOpexes`) | 0.90 |
| `sub_A23CF0` | 54KB | DAG list scheduler heuristic; barrier assignment for phase 115 scoreboard generation | 0.90 |
| `sub_9F1A90` | 35KB | MercConverter infrastructure; drives instruction-level legalization for Mercury phases 117--122 via visitor pattern | 0.92 |
| `sub_9ED2D0` | 25KB | Opcode switch inside MercConverter; dispatches per-opcode legalization/conversion | 0.90 |
| `sub_9F3760` | -- | Phase 141 (`MercConverter`) execute function; initial Mercury conversion of Ori instructions | 0.85 |
| `sub_18F21F0` | -- | Phase 142 (`MercEncodeAndDecode`) execute function; encode/decode round-trip verification | 0.85 |
