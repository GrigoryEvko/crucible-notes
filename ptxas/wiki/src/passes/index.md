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

All 17 gate passes fall into three categories when activated by a backend override: (A) dispatch to a named worker phase from the static name table, (B) dispatch through an SM backend vtable slot at `ctx+0x630`, or (C) execute a pipeline progress counter thunk that writes `ctx+1552 = N`. Category A gates have a named worker visible to `DUMPIR`. Category B dispatches through architecture-specific code in the SM backend object. Category C gates' only effect is advancing the pipeline progress counter, which downstream passes read via `*(ctx+1552) > N` guards.

| Gate (Wiki #) | Bin | Cat | Execute Fn | Worker / Dispatch Target | Evidence |
|---|--:|---|---|---|---|
| `AdvPhBeforeConvUnSup` (4) | 4 | C | `sub_C5F620` (7B) | `ctx+1552 = 1`; marks pre-legalization | P0_03 thunk table; early pipeline boundary |
| `AdvPhAfterConvUnSup` (7) | 7 | C | `sub_C5F5A0` (7B) | `ctx+1552 = 2`; marks post-ConvUnSup | P0_03 thunk table; `sub_752CF0` checks `<= 3` |
| `AdvPhEarlyEnforceArgs` (47) | 54 | A | vtable dispatch | `EnforceArgumentRestrictions` [48] | P5_02 correspondence table; W020 "Before EnforceArgumentRestrictions" |
| `AdvPhAfterMidExpansion` (134) | 52 | C | `sub_C5EF80` (7B) | `ctx+1552 = 3`; marks mid-expansion done | P0_03 thunk table; `sub_752CF0` checks `<= 3` |
| `AdvPhLateExpandSync` (135) | 83 | B | `0xC5F110` (6B) | `jmp *(*(ctx+0x630))+0x168`; SM backend vtable slot 360 | W029 disasm; brackets `LateExpandSyncInstructions` [84] |
| `AdvPhLateConvUnSup` (77) | 89 | B | `0xC5EA50` (13B) | `jmp *(*(ctx+0x630))+0x178`; SM backend vtable slot 376 | W033 disasm lines 108--111; drives `LateExpUnSupportedOps` [90] |
| `AdvPhBackPropVReg` (82) | 96 | B | off_22BE298 | Arch-override vtable dispatch; next phase [83] writes `ctx+1552 = 9` | P1_08 vtable layout; `isNoOp` returns 0 (runtime-overridden to 1) |
| `AdvPhSetRegAttr` (89) | 105 | B | vtable dispatch | `ctx+0x630` SM backend vtable; precedes `OriSetRegisterAttr` [90] | W020 line 407 "Before OriSetRegisterAttr" |
| `AdvPhAfterSetRegAttr` (92) | 108 | B | `0xC607A0` (51B) | `*(*(ctx+0x630))+0x110`; guarded by `nullsub_170@0x7D6C80` | W029 disasm line 53; returns NOP when default impl |
| `AdvPhPreSched` (97) | 113 | A | vtable dispatch | `ScheduleInstructions` [114]; `sub_8D0640` (22 KB) | P5_02 table; string `"ScheduleInstructions"` |
| `AdvPhAllocReg` (101) | 121 | A | vtable dispatch | `AllocateRegisters` [122] | String `"Please use -knob DUMPIR=AllocateRegisters"` at `sub_9714E0` |
| `AdvPhPostExpansion` (104) | 126 | A | vtable dispatch | `PostExpansion` [127]; post-RA expansion dispatch | P5_02 table |
| `AdvPhPostSched` (106) | 129 | C | `sub_C5E830` (7B) | `ctx+1552 = 14`; marks post-scheduling | P0_03 thunk table; adjacent to `PostSchedule` [110] |
| `AdvPhPostFixUp` (111) | 134 | A | vtable dispatch | `PostFixUp` [140]; `ctx+0x630` vtable+0x148 | P2_14 line 85; target-specific post-fixup |
| `AdvScoreboardsAndOpexes` (115) | 138 | B | vtable dispatch | `sub_A36360` (52 KB) + `sub_A23CF0` (54 KB); O1+ only | Control word gen + DAG scheduler; -O0 uses phase 139 instead |
| `AdvPhOriPhaseEncoding` (127) | 152 | C | `sub_C5E0B0` (7B) | `ctx+1552 = 21`; marks encoding boundary | P2_15 disasm; `sub_8C0270` checks `== 19` |
| *(total: 5 type A, 5 type B, 6 type C = 16 gates)* | | | | | |

**Type A** gates (5) dispatch to a named worker phase in the static name table -- valid `DUMPIR`/`NamedPhases`/`DisablePhases` targets. `AdvPhEarlyEnforceArgs` was reclassified from C to A based on P5_02 evidence: it dispatches to `EnforceArgumentRestrictions` [48], with `LateEnforceArgumentRestrictions` [103] as its late counterpart. **Type B** gates (5) dispatch through an SM backend vtable slot at `ctx+0x630`; the worker code lives in the per-SM backend object. Specific vtable offsets: +0x168 (late sync expansion), +0x178 (late unsupported ops), +0x110 (post-reg-attr, guarded by default-impl check against `nullsub_170@0x7D6C80`). **Type C** gates (6) write `ctx+1552` (pipeline_progress) to values 1--21, forming a monotonically increasing timeline that 20+ downstream guards check. `AdvPhPostSched` was reclassified from B to C based on P0_03 evidence: `sub_C5E830` is a 7-byte thunk writing `ctx+1552 = 14`, identical in structure to the other progress thunks.

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

### Mechanism

The 159-phase pipeline does **not** carry any opt-level metadata on the phase objects themselves. Three binary facts establish this:

1. **Uniform phase construction.** `sub_C60D30` (the 159-case phase factory at `0xC60D30`, 1132 lines) allocates every phase object via the same 5-line body: request 16 bytes from the pool, store a per-case vtable pointer at offset `+0`, store the allocator pointer at `+8`, return. There is no `*(char*)(obj+N) = LEVEL` write anywhere in the switch; every case is byte-identical except for the vtable symbol (cases 0--158 at `sub_C60D30_0xc60d30.c:172--1125`, tail default at 1126--1129). The 16-byte phase object therefore has room for exactly `{vtable, allocator}` and no inline "minimum opt level" field.

2. **The dispatch loop does not consult opt-level.** `sub_C64F70` (the phase iterator at `0xC64F70`, 276 lines) calls each phase's `isNoOp()` virtual (vtable slot `+0x10`) twice per iteration, and then unconditionally calls the phase's `execute()` virtual (vtable slot `+0x00`) via `LABEL_4`:

   ```c
   // sub_C64F70:86 (first isNoOp check)
   if ( (*(unsigned __int8 (**)(__int64))(*(_QWORD *)v6 + 16LL))(v6) )
     goto LABEL_4;             // skips "Before <phase>" diagnostic print
   /* ... allocate & emit "Before <name>" diagnostic string ... */
   LABEL_4:
     (**(void (***)(__int64, __int64))v6)(v6, *a1);   // execute(ctx)
   // sub_C64F70:162 (second isNoOp check)
   if ( !(*(unsigned __int8 (**)(__int64))(*(_QWORD *)v6 + 16LL))(v6) )
     /* ... allocate & emit "After <name>" diagnostic string ... */
   ```

   The `goto LABEL_4` branch bypasses *only* the diagnostic-string formatting block (lines 88--159); control still falls through to `execute()` at line 161. `isNoOp()` is therefore a **diagnostic-suppression flag**, not an execution gate. The pre-`execute` call at line 86 hides the "Before" string; the post-`execute` call at line 162 hides the "After" string; the `execute()` body itself runs every iteration regardless. See [Phase Manager -- Phase Dispatch Loop](phase-manager.md#phase-dispatch-loop----sub_c64f70) for the full annotated dispatch pseudocode and the `isNoOp` timing discussion.

3. **The gate lives inside each `execute` body.** Phases that honour the `-O` level do so via an early-return prologue in their `execute()` thunk. The canonical pattern (instantiated ~82 times in the `0xC5F7xx`--`0xC60Bxx` range) is:

   ```c
   // sub_C60140 (representative execute thunk, 8 bytes + prologue)
   void PhaseN::execute(ocg_ctx* ctx) {
     if ( (int)sub_7DDB50(ctx) > 1 )    // opt_level > 1 (i.e. O2+)
       sub_XXXXXX(ctx);                  // tail-call real implementation
     // else: fall through -- phase was a no-op at this O-level
   }
   ```

   `sub_7DDB50` (the opt-level accessor at `0x7DDB50`, 232 bytes) reads the cached 32-bit opt_level field from `ocg_ctx + 2104` (i.e. `ctx + 0x838`), but only when knob 499 is active; otherwise it returns `1`, capping effective behaviour at O1. The knob-499 kill-switch and the iteration-budget counter at `kv->state[35940]` are documented in [Optimization Levels -- Gate Accessor](../config/opt-levels.md#gate-accessor-sub_7ddb50).

**Important corollary.** Because `execute()` is always invoked, every phase's timing record and pre-snapshot (written at `sub_C64F70:72--85`, before the first `isNoOp()` call) are also recorded. `--ftime` output therefore contains a row for all 159 phases in every compilation, including phases that immediately early-returned because the opt-level guard failed. Gated-off phases show near-zero elapsed time rather than being omitted.

### Pseudocode for the full gate mechanism

```c
// OCG context fields referenced by the gate
struct ocg_ctx {
    // ...
    void*      options_mgr;       // +0x680 (1664)  -- knob query vtable
    int32_t    opt_level_cached;  // +0x838 (2104)  -- parsed -O level, 0..5
    // ...
};

// sub_7DDB50 (0x7DDB50) -- opt-level accessor, called by each phase execute
int getOptLevel(ocg_ctx* ctx) {
    OptionsMgr* om  = ctx->options_mgr;           // [ctx+1664]
    auto        set = om->vtable->setOption;      // [vtbl+152]
    if (set == sub_67EB60) {
        auto isSet = om->vtable->isOptionSet;     // [vtbl+72]
        bool knob_499 = (isSet == sub_6614A0)
            ? (om->state[35928] != 0)             // direct bss read
            : isSet(om, 499);                     // virtual query
        if (!knob_499)
            return ctx->opt_level_cached;         // honour -O level
        int used = om->state[35940];              // iteration counter
        if (om->state[35936] > used) {            // budget not exhausted?
            om->state[35940] = used + 1;
            return ctx->opt_level_cached;         // honour -O level
        }
    } else if (set(om, 499, 1)) {
        return ctx->opt_level_cached;             // honour -O level
    }
    return 1;                                     // fallback: clamp to O1
}

// Per-phase execute prologue (replicated in ~82 wrapper thunks)
void Phase_execute(phase* self, ocg_ctx* ctx) {
    if ((int)getOptLevel(ctx) > 1) {              // the gate: O2+ only
        do_the_actual_work(ctx);                  // tail-call real pass
    }
    // else: phase is a runtime no-op for this compilation
}

// sub_C64F70 dispatch loop (pseudocode; isNoOp() only gates diagnostics)
void PhaseManager::dispatch(int* idx, int n) {
    for (int i = 0; i < n; i++) {
        phase* p = phases[idx[i]];
        append_timing_record(p);                  // unconditional
        take_pre_snapshot();                      // unconditional
        if (!p->isNoOp()) print("Before " + p->name);
        p->execute(ctx);                          // ALWAYS called
        if (!p->isNoOp()) print("After "  + p->name);
    }
}
```

### The matrix is regular (two-bucket structure at the wrapper layer)

Scanning all phase wrappers in `0xC5F7xx`--`0xC60Bxx` (the per-phase `execute` thunks) for calls to `sub_7DDB50`:

| Gate predicate                                            | Wrappers | Meaning |
|-----------------------------------------------------------|----------|---------|
| (none -- wrapper unconditionally calls implementation)    | ~50      | Phase runs at every `-O` level |
| `(int)sub_7DDB50(ctx) > 1`                                | ~78      | Phase runs at O2, O3, O4, O5 |
| `(unsigned int)sub_7DDB50(ctx) == 1 && knob_235` (or similar guarded O1 path) | 3--4 | Phase runs at O1 only when an auxiliary knob is set |
| `> 1 \|\| (ctx+1424 == 199 && == 1)`                       | 1        | Phase 58 `GeneralOptimizeLate` -- O2+ **or** O1 with option-31 extended value 199 (see [General Optimize](general-optimize.md)) |

**Zero layer-1 wrappers** use the thresholds `> 0` (would mean "O1+"), `> 2` ("O3+"), `> 3` ("O4+"), or `> 4` ("O5 only"). Fine-grained opt-level branching (e.g. `opt_level <= 2` in `sub_78DB70`, `<= 3` in `sub_914B40`, `> 2` in `sub_8FB5D0` / `sub_9FC860` / `sub_9F8C00`, `> 3` in `sub_137EE50`) happens **inside** the implementation bodies, *after* the wrapper has already let control through. Those internal decisions toggle sub-algorithms (e.g. forward vs. reverse scheduler pass, loop-peeling depth, remat strategy) rather than enabling or disabling the phase as a whole.

**The phase-to-O-level activity matrix is therefore regular**: the layer-1 wrapper either runs the phase at every level, or gates it at exactly one threshold (`opt_level > 1`). Per-phase irregularity exists only at layer 2 -- inside the implementations that the wrappers call. This collapses the "159 phases × 6 opt-levels" table to a **two-column classification** at the outer dispatch layer:

```
+------------------------------+-------------------------------------+
|  Category A: always-run       |  Category B: O2+ only               |
|  (no sub_7DDB50 in wrapper)   |  (wrapper: sub_7DDB50(ctx) > 1)     |
+------------------------------+-------------------------------------+
|  * All reporting/dump phases  |  * GVN-CSE, LICM, rematerialization |
|  * All validation phases      |  * Loop unrolling, software pipe    |
|  * All legalization phases    |  * Predication / if-conversion      |
|  * All Mercury/SASS encoding  |  * Switch/branch optimization       |
|  * All register-allocation    |  * Sync-instruction optimization    |
|  * All AdvancedPhase gates    |  * Barrier removal                  |
|  * All pseudo/expansion phases|  * Backward copy propagation        |
|  * Initial setup & cleanup    |  * Speculative hoisting, peephole   |
+------------------------------+-------------------------------------+
```

### Concrete -O0 vs -O3 phase lists

Resolving the gate with `opt_level = 0` (i.e. `sub_7DDB50` returns 0) against the 159-phase pipeline and the Category-B wrappers identified above:

**At -O0, the following phases early-return (runtime no-ops):**
Phase 14 `DoSwitchOptFirst` (gate `sub_C5F720`), 15 `OriBranchOpt` (`sub_C5F950`), 22 `OriLoopUnrolling`, 24 `OriPipelining`, 26 `OriRemoveRedundantBarriers`, 28 `SinkRemat`, 30 `DoSwitchOptSecond` (`sub_C5FC80`), 38 `OptimizeNestedCondBranches` (`sub_C5FA70`), 49 `GvnCse`, 54 `OriDoRematEarly`, 58 `GeneralOptimizeLate` (`sub_C603E0`, unless option-31 override), 63 `OriDoPredication`, 69 `OriDoRemat`, 71 `OptimizeSyncInstructions`, 72 `LateExpandSyncInstructions`, 95 `SetAfterLegalization`, 99 `OriDoSyncronization`, 100 `ApplyPostSyncronizationWars`, 110 `PostSchedule`, 115 `AdvancedScoreboardsAndOpexes`, and ~60 other Category-B phases. At `-O0` the scheduling subsystem *does* still run phase 116 `ProcessO0WaitsAndSBs`, which performs the conservative-scoreboard insertion that makes O0 code actually executable -- phase 116 is itself a Category-A wrapper that dispatches to `sub_C5E2A0` only when the target architecture has `sm_version > 0x3FFF`.

**At -O3 (the default), every Category-A wrapper runs**, and every Category-B wrapper also runs because `sub_7DDB50` returns `3` which satisfies `> 1`. The difference between -O2 and -O3 at the wrapper level is therefore **zero phases** -- both levels activate the same 159 wrappers. The -O2/-O3 distinction happens entirely inside the implementation bodies (e.g. scheduling direction in `sub_8D0640`, which branches on `opt_level > 2`). The same is true for -O3 vs. -O4 vs. -O5: identical layer-1 wrapper activation, different internal algorithm selection. Only the `-O0` and `-O1` thresholds produce layer-1 visible skips.

This two-tier design explains why the wiki's "O-Level" column in the 159-phase table below is sparse: most phases have no entry because they always run (Category A) or because the visible O-level branching is buried inside a layer-2 implementation and does not show up at the phase wrapper at all.

See [Optimization Levels](../config/opt-levels.md) for the confirmed per-phase threshold list, the detailed `sub_7DDB50` accessor breakdown, knob 499 kill-switch semantics, the NvOpt recipe system, and the scheduler/RA-specific opt-level interactions.

---

## Complete 159-Phase Table

> **Coverage status.** 62 of the 159 phases (39 %) currently lack a dedicated detail page — the **Detail Page** column for those rows is blank. Phases without detail pages are documented at the row granularity of the table below plus, for the late-pipeline range 139--158, the [phase-by-phase deep dive](#phase-by-phase-deep-dive-139158). Phases targeted by future hunter-resolver waves: `OriCheckInitialProgram`, `ApplyNvOptRecipes`, `PromoteFP16`, `AnalyzeControlFlow`, `OriCreateMacroInsts`, `OriSanitize`, `OptimizeBindlessHeaderLoads`, `PerformPGO`, `GenerateMovPhi`, `StageAndFence`, `AnalyzeUniformsForSpeculation`, `OriLinearReplacement`, `CompactLocalMemory`, `ExtractShaderConsts{First,Final}`, `EmitPSI`, `ConvertVTGReadWrite`, `DoVirtualCTAExpansion`, `ForwardProgress`, `OptimizeUniformAtomic`, `EnforceArgumentRestrictions`, `OriReplaceEquivMultiDefMov`, `SpeculativeHoistComInsts`, `RemoveASTToDefaultValues`, `DoVTGMultiViewExpansion`, `OriRemoveRedundantMultiDefMov`, `DoKillMovement`, `DoTexMovement`, `LateArchOptimize{First,Second}`, `ExpandJmxComputation`, `OriBackCopyPropagate`, `InsertPseudoUseDefForConvUR`, `OriCalcDependantTex`, `FinalInspectionPass`, `SetAfterLegalization`, `BackPropagateVEC2D`, `OriRemoveNopCode`, `PlaceBlocksInSourceOrder`, `PostFixForMercTargets`, `FixUpTexDepBarAndSync`, `ComputeVCallRegUse`, `CalcRegisterMap`, `UpdateAfter{Optimize,PostRegAlloc,FormatCodeList,ConvertUnsupportedOps}`, `ReportBeforeScheduling`, `MergeEquivalentConditionalFlow`, `LateMergeEquivalentConditionalFlow`. Pages should follow the structure of [Strength Reduction](strength-reduction.md) or [Varying Propagation](varying-propagation.md) (front-matter table, algorithm pseudocode, data flow, function map, cross-refs).

### Stage 1 -- Initial Setup (Phases 0--13)

Program validation, recipe application, FP16 promotion, control flow analysis, unsupported-op conversion, macro creation, initial diagnostics.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 0 | 0 | `OriCheckInitialProgram` | Validation |  | Validates structural correctness of the initial Ori IR after PTX lowering |  |
| 1 | 1 | `ApplyNvOptRecipes` | Optimization |  | Applies NvOptRecipe transformations (option 391, 440-byte sub-manager) |  |
| 2 | 2 | `PromoteFP16` | Lowering |  | Promotes FP16 operations to FP32 where hardware lacks native support |  |
| 3 | 3 | `AnalyzeControlFlow` | Analysis |  | Builds the CFG: identifies loops, dominators, back edges |  |
| 4 | 4 | `AdvancedPhaseBeforeConvUnSup` | Gate |  | Type C: `sub_C5F620` writes `ctx+1552 = 1`; pre-legalization boundary |  |
| 5 | 5 | `ConvertUnsupportedOps` | Lowering |  | Replaces operations not natively supported on the target SM with equivalent sequences | [Late Legalization](late-legalization.md) |
| 6 | 6 | `SetControlFlowOpLastInBB` | Cleanup |  | Ensures control flow instructions are the final instruction in each basic block |  |
| 7 | 7 | `AdvancedPhaseAfterConvUnSup` | Gate |  | Type C: `sub_C5F5A0` writes `ctx+1552 = 2`; post-ConvUnSup boundary |  |
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
| 14 | 16 | `DoSwitchOptFirst` | Optimization | **> 1** | Optimizes switch statements: jump table generation, case clustering (1st pass); wrapper `sub_C5F720` | [Branch & Switch](branch-switch.md) |
| 15 | 17 | `OriBranchOpt` | Optimization | **> 1** | Branch folding, unreachable block elimination, conditional branch simplification; wrapper `sub_C5F950` | [Branch & Switch](branch-switch.md) |
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
| 27 | 30 | `AnalyzeUniformsForSpeculation` | Analysis |  | Analyzes constant bank accesses for speculation safety across control flow | [Uniform Regs](uniform-regs.md) |
| 28 | 31 | `SinkRemat` | Optimization | **> 1 / > 4** | Sinks instructions closer to uses and marks remat candidates; O2+: basic; O5: full cutlass | [Rematerialization](rematerialization.md) |
| 29 | 33 | `GeneralOptimize` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (mid-early) | [GeneralOptimize](general-optimize.md) |
| 30 | 34 | `DoSwitchOptSecond` | Optimization | **> 1** | Second switch optimization pass after loop/branch transformations; wrapper `sub_C5FC80` | [Branch & Switch](branch-switch.md) |
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
| 38 | 44 | `OptimizeNestedCondBranches` | Optimization | **> 1** | Simplifies nested conditional branches into flatter control flow; wrapper `sub_C5FA70` | [Branch & Switch](branch-switch.md) |
| 39 | 45 | `ConvertVTGReadWrite` | Lowering |  | Converts vertex/tessellation/geometry shader read/write operations |  |
| 40 | 46 | `DoVirtualCTAExpansion` | Lowering |  | Expands virtual CTA operations into physical CTA primitives |  |
| 41 | 47 | `MarkAdditionalColdBlocks` | Analysis |  | Marks basic blocks as cold based on heuristics and profile data | [Hot/Cold](hot-cold.md) |
| 42 | 48 | `ExpandMbarrier` | Lowering |  | Expands `MBARRIER` pseudo-instructions into native barrier sequences | [Sync & Barriers](sync-barriers.md) |
| 43 | 49 | `ForwardProgress` | Lowering |  | Inserts instructions guaranteeing forward progress (prevents infinite stalls) |  |
| 44 | 50 | `OptimizeUniformAtomic` | Optimization |  | Converts thread-uniform atomic operations into warp-level reductions |  |
| 45 | 51 | `MidExpansion` | Lowering |  | Target-dependent mid-level expansion of operations before register allocation | [Late Legalization](late-legalization.md) |
| 46 | 53 | `GeneralOptimizeMid2` | Optimization |  | Compound pass: copy prop + const fold + algebraic simplify + DCE (mid 2nd) | [GeneralOptimize](general-optimize.md) |
| 47 | 54 | `AdvancedPhaseEarlyEnforceArgs` | Gate |  | Type A: dispatches to `EnforceArgumentRestrictions` [48]; late counterpart `LateEnforceArgumentRestrictions` [103] |  |
| 48 | 55 | `EnforceArgumentRestrictions` | Lowering |  | Enforces ABI restrictions on function arguments (register classes, alignment) |  |
| 49 | 56 | `GvnCse` | Optimization | **> 1** | Global value numbering combined with common subexpression elimination | [Copy Prop & CSE](copy-prop-cse.md) |
| 50 | 58 | `OriReassociateAndCommon` | Optimization |  | Reassociates expressions for better commoning opportunities, then eliminates commons | [Copy Prop & CSE](copy-prop-cse.md) |
| 51 | 59 | `ExtractShaderConstsFinal` | Optimization |  | Final shader constant extraction pass (after GVN may expose new constants) |  |
| 52 | 60 | `OriReplaceEquivMultiDefMov` | Optimization |  | Eliminates redundant multi-definition move instructions with equivalent sources |  |

### Stage 4 -- Late Optimization (Phases 53--77)

Predication, rematerialization, loop fusion, varying propagation, sync optimization, phi destruction, uniform register conversion.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 53 | 61 | `OriPropagateVaryingFirst` | Optimization |  | Propagates varying (non-uniform) annotations to identify divergent values (1st pass) | [Varying Propagation](varying-propagation.md) |
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
| 70 | 81 | `OriPropagateVaryingSecond` | Optimization |  | Propagates varying annotations (2nd pass, after predication changes control flow) | [Varying Propagation](varying-propagation.md) |
| 71 | 82 | `OptimizeSyncInstructions` | Optimization | **> 1** | Eliminates and simplifies synchronization instructions | [Sync & Barriers](sync-barriers.md) |
| 72 | 84 | `LateExpandSyncInstructions` | Lowering | **> 2** | Expands sync pseudo-instructions into final hardware sequences | [Sync & Barriers](sync-barriers.md) |
| 73 | 85 | `ConvertAllMovPhiToMov` | Lowering |  | Destroys SSA form: converts `MOV.PHI` instructions into plain `MOV` |  |
| 74 | 86 | `ConvertToUniformReg` | Optimization |  | Converts qualifying values from general registers (R) to uniform registers (UR) | [Uniform Regs](uniform-regs.md) |
| 75 | 87 | `LateArchOptimizeFirst` | Optimization |  | Architecture-specific late optimizations (1st pass) |  |
| 76 | 88 | `UpdateAfterOptimize` | Cleanup |  | Rebuilds IR metadata invalidated by the late optimization group |  |
| 77 | 89 | `AdvancedPhaseLateConvUnSup` | Gate |  | Type B: `0xC5EA50` dispatches `ctx+0x630` vtable+0x178 (slot 376); drives `LateExpUnSupportedOps` [90] | [Late Legalization](late-legalization.md) |

### Stage 5 -- Legalization (Phases 78--96)

Late unsupported-op expansion, backward copy propagation, GMMA fixup, register attributes, final validation.

| # | Bin# | Phase Name | Category | O-Level | Description | Detail Page |
|---|---|---|---|---|---|---|
| 78 | 90 | `LateExpansionUnsupportedOps` | Lowering |  | Expands remaining unsupported operations after all optimizations | [Late Legalization](late-legalization.md) |
| 79 | 92 | `OriHoistInvariantsLate2` | Optimization |  | LICM (late 2nd pass) after unsupported-op expansion | [Loop Passes](loop-passes.md) |
| 80 | 94 | `ExpandJmxComputation` | Lowering |  | Expands JMX (jump with index computation) pseudo-instructions |  |
| 81 | 95 | `LateArchOptimizeSecond` | Optimization |  | Architecture-specific late optimizations (2nd pass) |  |
| 82 | 96 | `AdvancedPhaseBackPropVReg` | Gate |  | Type B: arch-override vtable dispatch (off_22BE298); next phase [83] writes `ctx+1552 = 9` | [Copy Prop & CSE](copy-prop-cse.md) |
| 83 | 97 | `OriBackCopyPropagate` | Optimization |  | Backward copy propagation: propagates values backward through move chains | [Copy Prop & CSE](copy-prop-cse.md) |
| 84 | 99 | `OriPerformLiveDeadFourth` | Analysis |  | Full liveness analysis + DCE (4th instance, pre-legalization cleanup) | [Liveness](liveness.md) |
| 85 | 100 | `OriPropagateGmma` | Optimization |  | Propagates WGMMA accumulator values through the IR | [GMMA Pipeline](gmma-pipeline.md) |
| 86 | 101 | `InsertPseudoUseDefForConvUR` | Lowering |  | Inserts pseudo use/def instructions for uniform register conversion bookkeeping | [Uniform Regs](uniform-regs.md) |
| 87 | 102 | `FixupGmmaSequence` | Lowering |  | Fixes WGMMA instruction sequences for hardware ordering constraints | [GMMA Pipeline](gmma-pipeline.md) |
| 88 | 104 | `OriHoistInvariantsLate3` | Optimization |  | LICM (late 3rd pass) after GMMA fixup | [Loop Passes](loop-passes.md) |
| 89 | 105 | `AdvancedPhaseSetRegAttr` | Gate |  | Type B: `ctx+0x630` SM backend vtable dispatch; precedes `OriSetRegisterAttr` [90] |  |
| 90 | 106 | `OriSetRegisterAttr` | Analysis |  | Annotates registers with scheduling attributes (latency class, bank assignment) | [Scheduling](../scheduling/overview.md) |
| 91 | 107 | `OriCalcDependantTex` | Analysis |  | Computes texture instruction dependencies for scheduling |  |
| 92 | 108 | `AdvancedPhaseAfterSetRegAttr` | Gate |  | Type B: `0xC607A0` dispatches `ctx+0x630` vtable+0x110; guarded by `nullsub_170@0x7D6C80` |  |
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
| 106 | 129 | `AdvancedPhasePostSched` | Gate |  | Type C: `sub_C5E830` writes `ctx+1552 = 14`; post-scheduling boundary |  |
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
| 127 | 152 | `AdvancedPhaseOriPhaseEncoding` | Gate |  | Type C: `sub_C5E0B0` writes `ctx+1552 = 21`; marks encoding boundary |  |
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
| 134 | 52 | `AdvancedPhaseAfterMidExpansion` | Gate |  | Type C: `sub_C5EF80` writes `ctx+1552 = 3`; marks mid-expansion done |  |
| 135 | 83 | `AdvancedPhaseLateExpandSyncInstructions` | Gate |  | Type B: `0xC5F110` dispatches `ctx+0x630` vtable+0x168 (slot 360) | [Sync & Barriers](sync-barriers.md) |
| 136 | 91 | `LateMergeEquivalentConditionalFlow` | Optimization |  | Second conditional flow merge pass (catches cases exposed by late transforms) |  |
| 137 | 93 | `LateExpansionUnsupportedOpsMid` | Lowering |  | Mid-late unsupported-op expansion (between the two merge passes) | [Late Legalization](late-legalization.md) |
| 138 | 98 | `OriSplitHighPressureLiveRanges` | RegAlloc |  | Last-resort live range splitter when register pressure exceeds hardware limits | [RegAlloc Architecture](../regalloc/overview.md) |
Columns: `#` (wiki number) = `Bin#` (binary factory index) for all late-pipeline phases (no renumbering gap from phase 139 onward). `Execute` = address of the vtable-slot-0 `execute(ctx)` function allocated by `sub_C60D30` (factory cases 139--158 at lines 1006--1125); worker / tail-call target addresses are listed in the Description column. `Gate` = the runtime predicate checked inside execute (if any) before the body runs; "unconditional" means the execute body has no skip branch. SM activation is `all` unless marked otherwise -- the only phase with an SM-version check in its execute body is 139.

| # = Bin# | Phase Name | Category | Execute | Gate | Description (algorithm / nullsub status / cross-ref) |
|---|---|---|---|---|---|
| 139 | `ProcessO0WaitsAndSBs` | Scheduling | `sub_C5E2A0` (41 B) | `target[+0x174] > 0x3FFF` (sm50+) | On sm50+, tail-calls `target.vtable[+0x150]` (`ApplyConservativeScoreboards`) with `edx=1` (O0-mode flag) to insert maximum-wait scoreboards on every instruction; pre-sm50 targets fall through to `ret`. `isNoOp=0`. See [Scoreboards](../scheduling/scoreboards.md) and deep-dive below. |
| 140 | `PostFixUp` | Cleanup | `sub_C5E270` (13 B) | unconditional | Tail-calls `target.vtable[+0x148]`; each Mercury target installs a target-specific post-fixup method (texture-barrier placement on Volta, scoreboard packing on Turing+, etc.), non-Mercury targets install a nullptr-safe stub. `isNoOp=0`. |
| 141 | `MercConverter` | Encoding | `sub_C60300` (8 B thunk) -> `sub_9F3760` | `cu[+1398] & 0x20` (bit 5 inside body) | Second MercConverter pass re-lowering PTX-derived opcodes introduced by optimization (rematerialization, peephole, loop xforms); dispatches on `target.sm_code` to the per-generation path, then routes through `sub_9F1A90` / `sub_9ED2D0` (the 35 KB opcode dispatcher reused from phase 5 `ConvertUnsupportedOps`). After completion every IR instruction carries a valid SASS opcode. See [Mercury: MercConverter](../codegen/mercury.md#mercconverter-operand-reorganization-for-encoding). |
| 142 | `MercEncodeAndDecode` | Encoding | `sub_C60310` (8 B thunk) -> `sub_18F21F0` | `ctx[+0x571] & 0x02` (bit 1) AND `ctx[+0x788] != NULL` (Mercury ctx present) | Encodes every Ori IR node to a Mercury node via the master encoder `sub_6D9690`, then round-trip-decodes each node to verify the binary encoding is reversible. After this phase all subsequent pipeline stages operate on Mercury nodes exclusively. See [Mercury: Stage 1 MercEncodeAndDecode](../codegen/mercury.md#stage-1-mercencodeanddecode-roundtrip-verification). |
| 143 | `MercExpandInstructions` | Encoding | `sub_C60320` (16 B) -> `sub_C3DFC0` (102 B orchestrator) | `ctx[+0x570] & 0x20` (bit 5) | Expands compound Mercury pseudo-instructions -- multi-word branches, multi-step LDG/STG sequences, sm_120 TCGEN05 macros -- into their primitive SASS sequences. `sub_C3DFC0` walks the Mercury list via `sub_C3CC60` and invokes each node's `vtable[+0x40]` Expand hook. See [Mercury: Stage 2 MercExpandInstructions](../codegen/mercury.md#stage-2-mercexpandinstructions-pseudo-instruction-expansion). |
| 144 | `MercGenerateWARs1` | Encoding | `sub_C60340` (16 B) -> `sub_6FC240` (47 B) | `ctx[+0x570] & 0x80` (bit 7, `js` opcode test) | First WAR (write-after-read) hazard annotation pass -- walks the Mercury node list and tags each consumer with the stall counts the target's hazard model requires. Runs before `MercGenerateOpex` (145). See [Mercury: Stage 3 MercGenerateWARs](../codegen/mercury.md#stage-3-mercgeneratewars-war-hazard-annotation). |
| 145 | `MercGenerateOpex` | Encoding | `sub_C60380` (16 B) -> `sub_7032A0` (472 B) | `ctx[+0x570] & 0x40` (bit 6) | Generates Opex (operand-exchange) annotations -- the per-instruction control bits that tell the hardware which physical register bank to read each operand from, required by the sm_90+ banked-register-file microarchitecture to avoid read-port conflicts. See [Mercury: Stage 4 MercGenerateOpex](../codegen/mercury.md#stage-4-mercgenerateopex-opex-generation). |
| 146 | `MercGenerateWARs2` | Encoding | `sub_C60360` (16 B) -> `sub_6FC240` | `ctx[+0x570] & 0x80` (bit 7) | Second WAR pass, byte-for-byte identical to phase 144 except for the vtable store. Two WAR passes bracket Opex (145) because Opex can rewrite operand banks and thereby introduce new write-to-read distances that must be re-annotated. See [Mercury: Stage 3 MercGenerateWARs](../codegen/mercury.md#stage-3-mercgeneratewars-war-hazard-annotation). |
| 147 | `MercGenerateSassUCode` | Encoding | `sub_C603A0` (16 B) -> `sub_6EEE90` (1472 B) -> `sub_6E4110` (24 KB) | `ctx[+0x571] & 0x01` (bit 0) | Terminal Mercury stage: walks the fully-annotated Mercury node list and emits the final SASS binary microcode bytes that end up in the ELF `.text` section. `sub_6EEE90` is a 0x110-byte stack-scratch wrapper that calls `sub_6E8EB0` for per-function setup, then hands off to the 24 KB emitter `sub_6E4110`. See [Mercury: Stage 5 MercGenerateSassUCode](../codegen/mercury.md#stage-5-mercgeneratesassucode-final-sass-emission) and [SASS Printing](../codegen/sass-printing.md). |
| 148 | `ComputeVCallRegUse` | RegAlloc | `sub_C5E160` (13 B) | unconditional | Tail-calls `target.vtable[+0x2B8]` to compute register usage at virtual call sites (indirect calls, function pointers). The result is written into the target-side register-use tracker and later emitted as `EIATTR_EXTERNS` / `EIATTR_INDIRECT_BRANCH_TARGETS` metadata so the CUDA runtime can honour conservative register budgets for callees whose footprint is unknown at compile time. |
| 149 | `CalcRegisterMap` | RegAlloc | `sub_C603C0` (32 B) -> `sub_95A350` (6456 B) | `ctx[+0x590] & 0x02` (bit 1) | Computes the final physical-to-logical register mapping that gets emitted as `EIATTR_REGCOUNT` / `EIATTR_MIN_STACK_SIZE` metadata. The execute thunk indirects through `ctx.target[+0x18]` (the SM-specific sub-target) before tail-calling `sub_95A350` (the actual mapping builder). The map is needed by the CUDA driver to inflate saved contexts during preemption and by NVRTC for relocation. See [RegAlloc Architecture](../regalloc/overview.md). |
| 150 | `UpdateAfterPostRegAlloc` | Cleanup | **`nullsub_630` at `0xC5E110` (2 B)** | -- | **True no-op in release ptxas.** Empty `repz ret` body; `isNoOp()` = 1 (`sub_C5E130`, 6 B) suppresses the "Before/After" diagnostic frame. Slot retained for ABI compatibility with debug builds where the body would be `PhaseManager::RebuildAfterPostRegAlloc`. |
| 151 | `ReportFinalMemoryUsage` | Reporting | **`nullsub_629` at `0xC5E0E0` (2 B)** | -- | **True no-op.** `isNoOp()` = 1 (`sub_C5E100`). Debug builds would dump the memory-arena high-water mark to stderr here; release strips the body entirely. |
| 152 | `AdvancedPhaseOriPhaseEncoding` | Gate | `sub_C5E0B0` (11 B) | unconditional | Type-C gate: `movl dword [rsi+0x610], 0x15; ret` -- writes `pipeline_progress = 21` (the final value of the monotonic `ctx[+0x610]` counter). Downstream consumers: `sub_8C0270` checks `*(ctx+0x610) == 19`; scoreboard guards check values 16--19. `isNoOp()` = 1 (`sub_C5E0D0`) because this is state-tracking, not an IR transform. |
| 153 | `FormatCodeList` | Encoding | `sub_C5E080` (13 B) | unconditional | The one late-pipeline phase that indirects through `ctx[+0x648]` (the code-list / ELF-section emitter) rather than `ctx[+0x630]` (target) or `ctx[+0x788]` (Mercury). Tail-calls `emitter.vtable[+0x10]` -- the "format" entry point that serialises the fully-encoded instructions into the final ELF text-section layout (addresses, relocations, alignment). `isNoOp=0`. See [Mercury](../codegen/mercury.md). |
| 154 | `UpdateAfterFormatCodeList` | Cleanup | **`nullsub_628` at `0xC5E050` (2 B)** | -- | **True no-op.** `isNoOp()` = 1 (`sub_C5E070`). Hook point kept in case a backend needs to re-sync IR metadata after FormatCodeList reorders instructions, but no release target uses it. |
| 155 | `DumpNVuCodeText` | Reporting | `sub_C60420` (54 B) | `ctx[+0x598] > 0 && ctx[+0x740] != NULL && *ctx[+0x740] != NULL` | Guarded by `-dump_nvu_code_text=1` knob; the full gate cascade is retained, but the tail-call target `0x67FF60` resolves to **`nullsub_31` (2 B)** -- the actual text dumper has been stripped from release ptxas, leaving the gate as an orphan that falls through to a stub. Effective no-op. See [SASS Printing](../codegen/sass-printing.md). |
| 156 | `DumpNVuCodeHex` | Reporting | `sub_C60460` (~48 B) | `ctx[+0x598] > 0 && ctx[+0x740] != NULL` | Mirror image of phase 155 with a simpler gate (no extra pointer indirection) and tail-call target `0x67FF50` = **`nullsub_30` (2 B)**. Same conclusion: stripped from release, orphan gate only. See [SASS Printing](../codegen/sass-printing.md). |
| 157 | `DebuggerBreak` | Cleanup | **`nullsub_627` at `0xC5DFE0` (2 B)** | -- | Debug-build breakpoint marker; release builds emit a bare `ret`. `isNoOp()` = 0 (`sub_C5E000`), so the "Before/After" diagnostic frame still fires -- useful when running ptxas under `gdb` with `b *0xC5DFE0` because the dispatch loop will print `"Before DebuggerBreak"` / `"After DebuggerBreak"` on either side of the breakpoint. |
| 158 | `NOP` | Cleanup | **`nullsub_626` at `0xC5DFB0` (2 B)** | -- | Terminal sentinel. The 159-phase dispatch loop (`sub_C64F70`) iterates `a1[0] .. a1[158]` and needs a final slot to anchor the loop end; `NOP` is that anchor. `isNoOp()` = 0 (`sub_C5DFD0`), so the final `"Before NOP"` / `"After NOP"` prints appear in verbose dumps as the explicit terminator for `"All Phases Summary"`. |

Phases 139--158 are 20 late-pipeline phases whose vtable pointers range from `off_22BEB80` to `off_22BEE78` (40-byte stride, 20 * 0x28 bytes). All 20 have names in the static table at `off_22BD0C0` (159 entries -- the earlier wiki note claiming "139 entries" was based on a compressed model that excluded these phases). Name resolution for the dispatch-loop diagnostic (`sub_C64F70`) goes through the static table indexed by `getIndex()` at vtable+8.

**Vtable layout (3 slots, 24 bytes per object, `off_22BExxx`).** Every late-pipeline phase object has exactly three virtual methods:

| Slot | Offset | Purpose | Behaviour |
|---|---|---|---|
| 0 | vtbl+0 | `execute(ctx)` | Entry point called by `sub_C64F70` dispatch loop (`LABEL_4`). See per-phase details below. |
| 1 | vtbl+8 | `getIndex()` | Returns the constant 139--158 (`mov eax, 0x8b..0x9e; ret`). Index into `off_22BD0C0` for the name string. Always 6 bytes. |
| 2 | vtbl+16 | `isNoOp()` | Either `xor eax,eax; ret` (3 bytes, always false) or `mov eax,1; ret` (6 bytes, always true). **Does not skip execute** -- it suppresses the `"Before <phase>"` / `"After <phase>"` diagnostic print around the call (see `sub_C64F70:86`, the `goto LABEL_4` branch falls into `execute` either way). |

The phase object itself is 16 bytes: `[0]=vtable*`, `[8]=ctx*`. No per-phase instance state -- all state lives in the shared OCG context passed to every `execute()` call.

**`isNoOp` statistics.** 16 of 20 phases return 0 from `isNoOp()` (diagnostics printed). Exactly **4 phases** return 1 (diagnostics suppressed): 150, 151, 152, 154. Of those, three (150, 151, 154) also have `nullsub` execute bodies and are truly vestigial; phase 152 has an 11-byte body that writes `pipeline_progress = 21` but is hidden from dumps because it is a state-tracking marker, not an IR transform.

### Phase-by-phase deep dive (139--158)

Each entry gives: execute function address, body size in bytes, vtable address, gate condition (if any), and pseudocode. All addresses are verified via (a) the factory switch in `sub_C60D30` (cases 139--158 at lines 1006--1125), (b) the raw pointer-table dump at `0x22BEB80`--`0x22BEE78` read from `.rodata`, and (c) direct `objdump` of the `.text` segment.

**Phase 139 `ProcessO0WaitsAndSBs`** -- `vtable=0x22BEB80` -- execute `sub_C5E2A0` (41 bytes, IDA missed it, recovered via objdump).
Runs on sm50+ only. Tail-dispatches to the target's `ApplyConservativeScoreboards` hook (vtable slot `0x150`) with flag `edx=1` (O0 mode). On sm30 / sm_3x and pre-sm50 architectures the phase returns immediately because the legacy shader-processor scoreboard model does not apply.
```
mov  rdi, [rsi+0x630]          ; rdi = ocg_ctx->target
cmp  dword [rdi+0x174], 0x3FFF ; if sm_version_encoded <= 16383 (pre-sm50)
jle  .return                   ;   skip
mov  rax, [rdi]                ; target.vtable
mov  edx, 1                    ; mode = O0
jmp  [rax+0x150]                ; target->ApplyConservativeScoreboards(ctx,1)
.return:
ret
```
`isNoOp` returns 0 (`sub_C5E2E0`, 3 bytes). No pipeline_progress write.

**Phase 140 `PostFixUp`** -- `vtable=0x22BEBA8` -- execute `sub_C5E270` (13 bytes).
Unconditional target-hook dispatch. Every Mercury target registers a post-fixup method at vtable slot `0x148`; non-Mercury targets install a nullptr-safe stub. The method performs target-specific cleanup after schedule and register allocation are final (examples: texture barrier placement on Volta, scoreboard packing on Turing+).
```
mov  rdi, [rsi+0x630]          ; target
mov  rax, [rdi]                ; target.vtable
jmp  [rax+0x148]                ; target->PostFixUp(target)
```
`isNoOp` = 0 (`sub_C5E290`).

**Phase 141 `MercConverter`** -- `vtable=0x22BEBD0` -- execute `sub_C60300` (8 bytes, thunk) -> body `sub_9F3760`.
Second MercConverter invocation, re-running the 35 KB opcode-dispatch machinery from phase 5 (`ConvertUnsupportedOps`) on instructions introduced by optimization passes (rematerialization, peephole, loop transforms) that may carry unlegalized PTX-derived opcodes. Internal gate `testb $0x10, [rdi+0x570]` (bit 4) inside `sub_9F3760` makes the body an immediate return on non-Mercury targets. When enabled the body dispatches on `target.sm_code` at `[rdi+0x174]` with the arch constants `0x9000`/`0x7005`/`0x7001`/`0x6001` to pick a per-generation conversion path. After completion every IR instruction carries a valid SASS opcode ready for encoding. See [Mercury](../codegen/mercury.md) `sub_9F1A90` / `sub_9ED2D0` for the full opcode dispatch.
```
; execute thunk
mov  rdi, rsi                  ; rdi = ocg_ctx
jmp  0x9F3760                  ; MercConverter::Run

; sub_9F3760 prologue
testb [rdi+0x570], 0x10        ; Mercury-active bit
jz   .return
...
```

**Phase 142 `MercEncodeAndDecode`** -- `vtable=0x22BEBF8` -- execute `sub_C60310` (8 bytes, thunk) -> body `sub_18F21F0`.
Encodes each Ori IR node to its Mercury-node form via `sub_6D9690` (the master encoder), then round-trip-decodes to verify the binary encoding is reversible. After this phase all subsequent pipeline stages operate on Mercury nodes exclusively. Internal gate `testb $0x2, [rdi+0x571]` (bit 1 of the high byte of `ctx+0x570`) makes it a no-op when Mercury is not the active backend.
```
mov  rdi, rsi
jmp  0x18F21F0                 ; MercEncodeAndDecode::Run

; body prologue
testb [rdi+0x571], 0x2
jz   .return
mov  r15, [rdi+0x788]          ; Mercury context
test r15, r15
jz   .return
...
```

**Phase 143 `MercExpandInstructions`** -- `vtable=0x22BEC20` -- execute `sub_C60320` (16 bytes).
Expands compound Mercury pseudo-instructions (e.g. multi-word branches, multi-step LDG/STG sequences, sm_120 TCGEN05 macros) into their SASS primitives. Gated by `ctx+0x570` bit 5; the Mercury backend sets this bit during its init recipe. Tail-calls `sub_C3DFC0` (102 bytes, an orchestrator that calls `sub_C3CC60` to iterate the Mercury list and invokes per-instruction `vtable+0x40` `Expand` hooks). `sub_C3DFC0` also emits the `"After MercExpand"` diagnostic on completion.
```
testb [rsi+0x570], 0x20        ; bit 5: MercExpandEnable
jnz  .active
repz ret                        ; skip -- non-Mercury target
.active:
mov  rdi, [rsi+0x788]          ; rdi = Mercury context
jmp  0xC3DFC0                  ; RunMercExpandPass
```

**Phase 144 `MercGenerateWARs1`** -- `vtable=0x22BEC48` -- execute `sub_C60340` (16 bytes).
First WAR-hazard annotation pass. Walks the Mercury node list and tags each consumer with the write-after-read stall counts needed to satisfy the target's hazard model. Runs after `MercExpandInstructions` (143) but before `MercGenerateOpex` (145); the "pass-1" naming reflects that two WAR passes are needed because Opex (145) can rewrite operand banks and introduce new write-to-read distances that pass 146 then re-annotates. Gated by the sign bit (`cmpb $0, [rsi+0x570]; js` i.e. bit 7) of `ctx+0x570`.
```
cmpb [rsi+0x570], 0             ; js = "if signed" = bit 7 set
js   .active
repz ret
.active:
mov  rdi, [rsi+0x788]
jmp  0x6FC240                  ; RunMercWARsPass (47 bytes)
```

**Phase 145 `MercGenerateOpex`** -- `vtable=0x22BEC70` -- execute `sub_C60380` (16 bytes).
Generates Opex (operand-exchange) annotations per instruction -- extra control bits that tell the hardware which physical register bank to read each operand from, required by the sm_90+ banked-register file to avoid bank conflicts. Gated by `ctx+0x570` bit 6. Tail-calls `sub_7032A0` (472 bytes, `RunMercOpexPass`). See [Mercury](../codegen/mercury.md) Stage 4.
```
testb [rsi+0x570], 0x40
jnz  .active
repz ret
.active:
mov  rdi, [rsi+0x788]
jmp  0x7032A0
```

**Phase 146 `MercGenerateWARs2`** -- `vtable=0x22BEC98` -- execute `sub_C60360` (16 bytes).
Second WAR-hazard pass. Identical instruction body to phase 144 (same `sub_6FC240` tail-call, same bit-7 gate); the two invocations bracket phase 145 (Opex) which may rewrite operand banks and thereby introduce new write-to-read distances that need re-annotation. Opcode bytes are byte-for-byte identical to phase 144 modulo the vtable store before it.
```
cmpb [rsi+0x570], 0
js   .active
repz ret
.active:
mov  rdi, [rsi+0x788]
jmp  0x6FC240                  ; same entry as phase 144
```

**Phase 147 `MercGenerateSassUCode`** -- `vtable=0x22BECC0` -- execute `sub_C603A0` (16 bytes).
The terminal Mercury stage: walks the fully-annotated Mercury node list and emits the final SASS binary microcode bytes that will end up in the ELF `.text` section. Gated by `ctx+0x571` bit 0 (the lowest bit of the second flag byte). Tail-calls `sub_6EEE90` (1472 bytes), which is a thin wrapper that allocates a 0x110-byte stack scratch area, invokes `sub_6E8EB0` for per-function setup, then calls into `sub_6E4110` (24 KB, the real emitter documented in [Mercury](../codegen/mercury.md) Stage 5).
```
testb [rsi+0x571], 0x1
jnz  .active
repz ret
.active:
mov  rdi, [rsi+0x788]
jmp  0x6EEE90                  ; MercGenerateSassUCode::Run
```

**Phase 148 `ComputeVCallRegUse`** -- `vtable=0x22BECE8` -- execute `sub_C5E160` (13 bytes).
Computes register usage at virtual call sites (indirect calls, function pointers) and stores the result in the target-side register-use tracker. The data is consumed during ELF emission as `EIATTR_EXTERNS`/`EIATTR_INDIRECT_BRANCH_TARGETS` metadata so that the CUDA runtime can honour conservative register budgets for callees whose register footprint is unknown at compile time. Unconditional; all architectures route through the target vtable slot `0x2B8`.
```
mov  rdi, [rsi+0x630]           ; target
mov  rax, [rdi]                 ; vtable
jmp  [rax+0x2B8]                ; target->ComputeVCallRegUse(target)
```

**Phase 149 `CalcRegisterMap`** -- `vtable=0x22BED10` -- execute `sub_C603C0` (32 bytes).
Computes the final physical-to-logical register mapping that gets emitted as `EIATTR_REGCOUNT` / `EIATTR_MIN_STACK_SIZE` metadata. The mapping is needed by the CUDA driver to inflate saved contexts during preemption and by NVRTC for relocation. Gated by `ctx+0x590` bit 1 (register-map-export knob). Indirects through `ctx.target->tex_or_fat_target` at `ctx+0x630 ; [rax+0x18]` then tail-calls `sub_95A350` (6456 bytes, the actual mapping builder).
```
testb [rsi+0x590], 0x2
jnz  .active
repz ret
.active:
mov  rax, [rsi+0x630]
mov  rdi, [rax+0x18]            ; target.sub_target (sm-specific)
jmp  0x95A350                   ; CalcRegisterMap body
```

**Phase 150 `UpdateAfterPostRegAlloc`** -- `vtable=0x22BED38` -- execute `nullsub_630` at `0xC5E110` (**2 bytes, `repz ret`**).
**True no-op in release ptxas.** `isNoOp()` returns 1 (`sub_C5E130`, 6 bytes) to suppress the diagnostic frame around the call. The phase slot is kept for ABI compatibility with debug builds where the body is `PhaseManager::RebuildAfterPostRegAlloc`, but the release build strips it.

**Phase 151 `ReportFinalMemoryUsage`** -- `vtable=0x22BED60` -- execute `nullsub_629` at `0xC5E0E0` (**2 bytes, `repz ret`**).
**True no-op.** `isNoOp()` = 1 (`sub_C5E100`). Debug builds would dump the memory-arena high-water mark to stderr here; release strips the body entirely.

**Phase 152 `AdvancedPhaseOriPhaseEncoding`** -- `vtable=0x22BED88` -- execute `sub_C5E0B0` (**11 bytes**). The single surviving late-pipeline gate hook.
```
movl dword [rsi+0x610], 0x15   ; pipeline_progress = 21
ret
```
Writes `pipeline_progress = 21` (the final value of the monotonic `ctx+0x610` counter; see [Targets](../targets/index.md#runtime-state-layout) offset +1552). Downstream consumers: `sub_8C0270` checks `*(ctx+0x610) == 19`; scoreboard guards check values 16--19. `isNoOp()` = 1 (`sub_C5E0D0`) because the write is state-tracking, not IR transformation.

**Phase 153 `FormatCodeList`** -- `vtable=0x22BEDB0` -- execute `sub_C5E080` (13 bytes).
Indirects through a different context object than the other late phases: `ctx+0x648` is the code-list / ELF-section emitter rather than `ctx+0x630` (target) or `ctx+0x788` (Mercury context). Tail-calls `vtable+0x10` on that object -- the "format" entry point that serialises the fully-encoded instructions into the final ELF text-section layout (addresses, relocations, alignment).
```
mov  rdi, [rsi+0x648]           ; code-list emitter
mov  rax, [rdi]                 ; its vtable
jmp  [rax+0x10]                 ; emitter->FormatCodeList()
```

**Phase 154 `UpdateAfterFormatCodeList`** -- `vtable=0x22BEDD8` -- execute `nullsub_628` at `0xC5E050` (**2 bytes, `repz ret`**).
**True no-op.** `isNoOp()` = 1 (`sub_C5E070`). Kept as a hook point in case a target backend needs to re-sync IR metadata after FormatCodeList reordered instructions, but no release target uses it.

**Phase 155 `DumpNVuCodeText`** -- `vtable=0x22BEE00` -- execute `sub_C60420` (54 bytes).
The gate cascade `ctx+0x598 > 0 && ctx+0x740 != NULL && *(ctx+0x740) != NULL` is fully retained, so the code path is reachable when the hidden `-dump_nvu_code_text=1` knob is set, but the tail-call target `0x67FF60` resolves to **`nullsub_31` (2 bytes)** -- the actual text dumper has been stripped from release ptxas, leaving an orphan gate that falls through to a stub.
```
mov  eax, [rsi+0x598]           ; verbosity level
test eax, eax
jle  .skip
mov  rax, [rsi+0x740]           ; dump sink
test rax, rax
je   .skip
mov  rdi, [rax]
test rdi, rdi
je   .skip
xor  edx, edx
xor  esi, esi
jmp  0x67FF60                   ; nullsub_31 -- stub
.skip:
repz ret
```

**Phase 156 `DumpNVuCodeHex`** -- `vtable=0x22BEE28` -- execute `sub_C60460` (~48 bytes).
Mirror image of phase 155 with a simpler gate (no extra pointer indirection) and tail-call target `0x67FF50` = **`nullsub_30`**. Same conclusion: stripped from release, orphan gate only.

**Phase 157 `DebuggerBreak`** -- `vtable=0x22BEE50` -- execute `nullsub_627` at `0xC5DFE0` (**2 bytes, `repz ret`**).
Debug-build breakpoint marker; release builds emit a bare `ret`. `isNoOp()` = 0 (`sub_C5E000`), so the diagnostic frame still fires -- useful when running ptxas under `gdb` with `b *0xC5DFE0` because the dispatch loop will print `"Before DebuggerBreak"` / `"After DebuggerBreak"` on either side of the breakpoint.

**Phase 158 `NOP`** -- `vtable=0x22BEE78` -- execute `nullsub_626` at `0xC5DFB0` (**2 bytes, `repz ret`**).
Terminal sentinel. The 159-phase dispatch loop (`sub_C64F70`) iterates `a1[0] .. a1[158]` and needs a final slot to anchor the loop end; `NOP` is that anchor. `isNoOp()` = 0 (`sub_C5DFD0`), so the final "Before NOP" / "After NOP" prints appear in verbose dumps as the explicit terminator for `"All Phases Summary"`.

**Summary of nullsubs (release build).** Five of the 20 phases have bodies that are pure `ret` stubs: **150, 151, 154, 157, 158**. Two more (**155, 156**) have non-trivial gate cascades but their tail-call targets resolve to nullsubs, making them effectively no-ops too. That leaves **13 phases (139--149, 152, 153)** that actually transform IR or pipeline state in a release build. Of the 13 active phases, seven are Mercury encoder stages (141--147) gated by `ctx+0x570`/`ctx+0x571` bits -- so on a non-Mercury backend the active count drops to six (139, 140, 148, 149, 152, 153).

**No per-SM arch split across these phases.** None of the 20 execute bodies contain an `sm_version` switch on `ctx.target[+0x174]` at the phase level; the only such check is in phase 139's gate (`> 0x3FFF` i.e. "sm50-and-up"). All per-generation specialisation happens one level down, inside the target vtable methods each phase tail-calls (Mercury backend for 141--147, target vtable slots `0x148`/`0x150`/`0x2B8` for 140/139/148). The pipeline itself is arch-uniform; backends differ only in the methods they plug into the vtables.

The Mercury phases (141--147) are gated by flag bits at `ctx+0x570`/`ctx+0x571`, allowing non-Mercury backends to selectively disable encoding stages. WAR generation runs in two passes (144, 146) bracketing Opex (145) because Opex can rewrite operand banks and thereby introduce new write-to-read distances that need re-annotation -- phase 143 (MercExpandInstructions) also runs before the pair but has its own bit-5 gate.

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
