# Phase Pipeline — 159 Registered Phases (157 Dispatched)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The complete phase registry built by `sub_C62720` (factory `sub_C60D30`), indexed by **bin_index** = factory switch case = name-table index at `off_22BD0C0`. The **exec_order** column gives the position in the default 157-entry order table at `0x22BEEA0`; a dash (`-`) marks the two registered phases that are **not** in the default schedule (`DebuggerBreak`=157, `NOP`=158), reachable only via the recipe/named-phases path (OCG knob 298). The **opt_gate** column shows the opt-level predicate the phase's own `execute()` body applies (read via the OptBudget oracle `sub_7DDB50`); blank = runs at all levels. Every dispatched phase's `execute()` is called unconditionally — the gate is an early-return inside the body, not a dispatch-loop skip.

| exec_order | bin_index | phase_name | name_string_va | category | opt_gate | role |
|---|---|---|---|---|---|---|
| 0 | 0 | OriCheckInitialProgram | 0x22BC429 | Validation |  | Validate structural correctness of initial Ori IR after PTX lowering (validator sub_7EEB10) |
| 1 | 1 | ApplyNvOptRecipes | 0x22BC440 | Optimization |  | Apply NvOptRecipe transforms (opt 391, 440B sub-mgr); impl sub_796D60, dual-caller re-entry via sub_8F4D80 |
| 2 | 2 | PromoteFP16 | 0x22BC452 | Lowering |  | Promote FP16 ops to FP32 where HW lacks native support |
| 3 | 3 | AnalyzeControlFlow | 0x22BC45E | Analysis |  | Build CFG: loops/dominators/back-edges; impl sub_781F80 (131 callers, canonical CFG rebuild) |
| 4 | 4 | AdvancedPhaseBeforeConvUnSup | 0x22BC471 | Gate |  | AdvancedPhase Type C: sub_C5F620 writes ctx+1552=1 (pre-legalization boundary) |
| 5 | 5 | ConvertUnsupportedOps | 0x22BC48E | Lowering |  | Replace ops not native on target SM with equivalent sequences |
| 6 | 6 | SetControlFlowOpLastInBB | 0x22BC4A4 | Cleanup |  | Ensure control-flow op is last instruction in each basic block |
| 7 | 7 | AdvancedPhaseAfterConvUnSup | 0x22BC4BD | Gate |  | AdvancedPhase Type C: sub_C5F5A0 writes ctx+1552=2 (post-ConvUnSup boundary) |
| 8 | 8 | UpdateAfterConvertUnsupportedOps | 0x22BCF10 | Cleanup |  | Rebuild IR metadata after unsupported-op conversion |
| 9 | 9 | OriCreateMacroInsts | 0x22BC4D9 | Lowering |  | Expand PTX-level macro instructions into Ori sequences (impl sub_A112C0, 10 callers) |
| 10 | 10 | ReportInitialRepresentation | 0x22BC4ED | Reporting |  | Dump Ori IR after initial lowering (no-op unless DUMPIR) |
| 11 | 11 | EarlyOriSimpleLiveDead | 0x22BC509 | Optimization |  | Quick early dead-code elimination |
| 12 | 12 | ReplaceUniformsWithImm | 0x22BC520 | Optimization |  | Replace uniform register reads with immediates where value known |
| 13 | 13 | OriSanitize | 0x22BC537 | Validation |  | Validate IR consistency after initial setup |
| 14 | 14 | GeneralOptimizeEarly | 0x22BC543 | Optimization |  | GeneralOptimize bundle (early): copy-prop+const-fold+algebraic+DCE fixpoint |
| 15 | 15 | MergeEquivalentConditionalFlow | 0x22BCF38 | Optimization |  | Merge basic blocks with equivalent conditional flow (tail merge) |
| 16 | 16 | DoSwitchOptFirst | 0x22BC558 | Optimization | >1 | Switch-statement opt: jump-table gen, case clustering (1st); wrapper sub_C5F720 |
| 17 | 17 | OriBranchOpt | 0x22BC569 | Optimization | >1 | Branch folding, unreachable-block elim, conditional-branch simplification; sub_C5F950 |
| 18 | 18 | OriPerformLiveDeadFirst | 0x22BC576 | Analysis |  | Full liveness + DCE (1st of 4 major instances) |
| 19 | 19 | OptimizeBindlessHeaderLoads | 0x22BC58E | Optimization |  | Hoist/dedup bindless texture header loads |
| 20 | 20 | OriLoopSimplification | 0x22BC5AA | Optimization | >3 | Loop canonicalization: single entry/back-edge, preheader; aggressive peeling O4+ |
| 21 | 21 | OriSplitLiveRanges | 0x22BC5C0 | Optimization |  | Split live ranges at loop boundaries to reduce register pressure |
| 22 | 22 | OriCopyProp | 0x21E6CE1 | Optimization |  | Copy propagation (also sub-pass of all 6 GeneralOptimize bundles); injectable via recipe cpyN |
| 23 | 23 | PerformPGO | 0x22BC5D3 | Optimization |  | Apply PGO data (block weights, branch probabilities) |
| 24 | 24 | OriStrengthReduce | 0x22BC5DE | Optimization |  | Strength reduction: mul/div -> shift/add |
| 25 | 25 | OriLoopUnrolling | 0x22BC5F0 | Optimization | >1 | Loop unrolling by trip count + register-pressure heuristics |
| 26 | 26 | GenerateMovPhi | 0x22BC601 | Lowering |  | Insert SSA phi as MOV.PHI pseudo-insts; large wrapper sub_C60BD0 -> sub_790A40 |
| 27 | 27 | OriPipelining | 0x22BC610 | Optimization | >1 | Software pipelining: overlap loop iterations to hide latency |
| 28 | 28 | StageAndFence | 0x22BC61E | Lowering |  | Insert memory fence/staging instructions for coherence |
| 29 | 29 | OriRemoveRedundantBarriers | 0x22BC62C | Optimization | >1 | Eliminate barriers proven redundant by dataflow |
| 30 | 30 | AnalyzeUniformsForSpeculation | 0x22BC647 | Analysis |  | Analyze constant-bank accesses for speculation safety across control flow |
| 31 | 31 | SinkRemat | 0x22BC665 | Optimization | >1 | Sink instructions toward uses, mark remat candidates (O5: full cutlass) |
| 32 | 32 | OptimizeNaNOrZero | 0x21DD150 | Optimization |  | NaN/zero constant folding |
| 33 | 33 | GeneralOptimize | 0x22BC66F | Optimization |  | GeneralOptimize bundle (mid-early) |
| 34 | 34 | DoSwitchOptSecond | 0x22BC67F | Optimization | >1 | Switch opt 2nd pass after loop/branch xforms; wrapper sub_C5FC80 |
| 35 | 35 | OriLinearReplacement | 0x22BC691 | Optimization |  | Linear/affine-expr replacement: 11-case opcode dispatch -> IADD3/IMAD.WIDE; impl sub_7EC4B0; knobs 487+416 |
| 36 | 36 | CompactLocalMemory | 0x22BC6A6 | Optimization |  | Compact local-memory allocations (dead slot elim + reorder) |
| 37 | 37 | ConvertMemoryToRegisterOrUniform | 0x21DD228 | Optimization |  | Convert memory to register/uniform; knob 487 gated |
| 38 | 38 | OriPerformLiveDeadSecond | 0x22BC6B9 | Analysis |  | Full liveness + DCE (2nd instance) |
| 39 | 39 | ExtractShaderConstsFirst | 0x22BC6D2 | Optimization |  | Extract shader constants (1st); impl sub_1C72640 shared w/ bin59, is_final=0 |
| 40 | 40 | OriHoistInvariantsEarly | 0x22BC6EB | Optimization |  | LICM early: hoist loop-invariant computations |
| 41 | 41 | Vectorization | 0x21D3ABE | Optimization |  | Load/store vectorization |
| 42 | 42 | EmitPSI | 0x22BC703 | Lowering |  | Emit PSI (pixel-shader-input) interpolation setup (dispatch-only via arch vtable) |
| 43 | 43 | GeneralOptimizeMid | 0x22BC70B | Optimization |  | GeneralOptimize bundle (mid) |
| 44 | 44 | OptimizeNestedCondBranches | 0x22BC71E | Optimization | >1 | Flatten nested conditional branches; wrapper sub_C5FA70 |
| 45 | 45 | ConvertVTGReadWrite | 0x22BC739 | Lowering |  | Convert vertex/tess/geometry shader read/write (dispatch-only arch vtable) |
| 46 | 46 | DoVirtualCTAExpansion | 0x22BC74D | Lowering |  | Expand virtual-CTA ops into physical CTA primitives (dispatch-only) |
| 47 | 47 | MarkAdditionalColdBlocks | 0x22BC763 | Analysis |  | Mark cold basic blocks via heuristics + profile |
| 48 | 48 | ExpandMbarrier | 0x22BC77C | Lowering |  | Expand MBARRIER pseudo-insts into native barrier sequences |
| 49 | 49 | ForwardProgress | 0x22BC78B | Lowering |  | Insert forward-progress instructions (prevent infinite stalls; dispatch-only) |
| 50 | 50 | OptimizeUniformAtomic | 0x22BC79B | Optimization |  | Convert thread-uniform atomics into warp-level reductions |
| 51 | 51 | MidExpansion | 0x22BC7B1 | Lowering |  | Target-dependent mid-level expansion before regalloc |
| 52 | 52 | AdvancedPhaseAfterMidExpansion | 0x22BCF58 | Gate |  | AdvancedPhase Type C: sub_C5EF80 writes ctx+1552=3 (mid-expansion done) |
| 53 | 53 | GeneralOptimizeMid2 | 0x22BC7BE | Optimization |  | GeneralOptimize bundle (mid 2nd) |
| 54 | 54 | AdvancedPhaseEarlyEnforceArgs | 0x22BC7D2 | Gate |  | AdvancedPhase Type A: dispatches EnforceArgumentRestrictions (bin55) |
| 55 | 55 | EnforceArgumentRestrictions | 0x22BC7F0 | Lowering |  | Enforce ABI restrictions on args (register classes, alignment) |
| 56 | 56 | GvnCse | 0x22BC80C | Optimization | >1 | Global value numbering + common-subexpression elimination |
| 57 | 57 | OriCommoning | 0x22BC94D | Optimization |  | Commoning sub-pass (between GvnCse and reassociate) |
| 58 | 58 | OriReassociateAndCommon | 0x22BC813 | Optimization |  | Reassociate expressions for commoning, then eliminate commons |
| 59 | 59 | ExtractShaderConstsFinal | 0x22BC82B | Optimization |  | Extract shader constants (final); impl sub_1C72640, is_final=1, commits bank allocs |
| 60 | 60 | OriReplaceEquivMultiDefMov | 0x22BC844 | Optimization |  | Eliminate redundant multi-def MOVs with equivalent sources (sub_8FBCF0) |
| 61 | 61 | OriPropagateVaryingFirst | 0x22BC85F | Optimization |  | Propagate varying (non-uniform) annotations (1st pass) |
| 62 | 62 | OriDoRematEarly | 0x22BC878 | Optimization | >1 | Early rematerialization near uses to reduce register pressure |
| 63 | 63 | LateExpansion | 0x22BC888 | Lowering |  | Expand ops that must lower after high-level opt |
| 64 | 64 | SpeculativeHoistComInsts | 0x22BC896 | Optimization |  | Speculatively hoist common instructions above branches |
| 65 | 65 | RemoveASTToDefaultValues | 0x22BC8AF | Cleanup |  | Remove AST/address-space-type annotations lowered to defaults |
| 66 | 66 | GeneralOptimizeLate | 0x22BC8C8 | Optimization |  | GeneralOptimize bundle (late); O1 override via opt-31 value 199 |
| 67 | 67 | OriLoopFusion | 0x22BC8DC | Optimization |  | Fuse adjacent loops with compatible bounds, no inter-loop deps |
| 68 | 68 | DoVTGMultiViewExpansion | 0x22BC8EA | Lowering |  | Expand multi-view ops for VTG shaders (dispatch-only) |
| 69 | 69 | OriSimpleLiveDead | 0x22BC50E | Optimization |  | Quick DCE (OriSimpleLiveDead) |
| 70 | 70 | OriPerformLiveDeadThird | 0x22BC902 | Analysis |  | Full liveness + DCE (3rd instance) |
| 71 | 71 | OriRemoveRedundantMultiDefMov | 0x22BC91A | Optimization |  | Remove dead multi-def MOVs (impl sub_90A340) |
| 72 | 72 | OriDoPredication | 0x22BC938 | Optimization | >1 | If-conversion: short conditional branches -> predicated instructions |
| 73 | 73 | LateVectorization | 0x21D3ABA | Optimization |  | 2nd vectorization pass (late) |
| 74 | 74 | LateOriCommoning | 0x22BC949 | Optimization |  | Late commoning: CSE exposed by predication |
| 75 | 75 | GeneralOptimizeLate2 | 0x22BC95A | Optimization |  | GeneralOptimize bundle (late 2nd) |
| 76 | 76 | OriHoistInvariantsLate | 0x22BC96F | Optimization |  | LICM late after predication |
| 77 | 77 | SinkCodeIntoBlock | 0x21B5E6C | Optimization | >2 | Code sinking into block; Kill/Tex movement engine sub_8FFDE0 esi=0..3 |
| 78 | 78 | DoKillMovement | 0x22BC986 | Optimization | >2 | Sink kill markers toward last use (engine sub_8FFDE0 esi=0) |
| 79 | 79 | DoTexMovement | 0x22BC995 | Optimization | >2 | Hoist texture fetches upward to hide latency (engine esi=2) |
| 80 | 80 | OriDoRemat | 0x22BC9A3 | Optimization | >1 | Late rematerialization exposed by predication/fusion |
| 81 | 81 | OriPropagateVaryingSecond | 0x22BC9AE | Optimization |  | Propagate varying annotations (2nd pass) |
| 82 | 82 | OptimizeSyncInstructions | 0x22BC9C8 | Optimization | >1 | Eliminate/simplify synchronization instructions |
| 83 | 83 | AdvancedPhaseLateExpandSyncInstructions | 0x22BCF78 | Gate |  | AdvancedPhase Type B: 0xC5F110 -> ctx+0x630 vtable slot 360 (+0x168) |
| 84 | 84 | LateExpandSyncInstructions | 0x22BC9E1 | Lowering | >2 | Expand sync pseudo-insts into final HW sequences |
| 85 | 85 | ConvertAllMovPhiToMov | 0x22BC9FC | Lowering |  | Destroy SSA: convert MOV.PHI -> plain MOV |
| 86 | 86 | ConvertToUniformReg | 0x22BCA12 | Optimization |  | Convert qualifying values from R to UR (uniform regs) |
| 87 | 87 | LateArchOptimizeFirst | 0x22BCA26 | Optimization |  | Arch-specific late optimizations (1st pass) |
| 88 | 88 | UpdateAfterOptimize | 0x22BCA3C | Cleanup |  | Rebuild IR metadata after late optimization group |
| 89 | 89 | AdvancedPhaseLateConvUnSup | 0x22BCA50 | Gate |  | AdvancedPhase Type B: 0xC5EA50 -> ctx+0x630 vtable slot 376 (+0x178); drives LateExpUnSupOps |
| 90 | 90 | LateExpansionUnsupportedOps | 0x22BCA6B | Lowering |  | Expand remaining unsupported ops after all optimization |
| 91 | 91 | LateMergeEquivalentConditionalFlow | 0x22BCFA0 | Optimization |  | 2nd conditional-flow merge (cases exposed by late xforms) |
| 92 | 92 | OriHoistInvariantsLate2 | 0x22BCA87 | Optimization |  | LICM late 2nd pass |
| 93 | 93 | LateExpansionUnsupportedOpsMid | 0x22BCFC8 | Lowering |  | Mid-late unsupported-op expansion |
| 94 | 94 | ExpandJmxComputation | 0x22BCA9F | Lowering |  | Expand JMX (jump-with-index) pseudo-insts |
| 95 | 95 | LateArchOptimizeSecond | 0x22BCAB4 | Optimization |  | Arch-specific late optimizations (2nd pass) |
| 96 | 96 | AdvancedPhaseBackPropVReg | 0x22BCACB | Gate |  | AdvancedPhase Type B: arch-override vtable off_22BE298 (backprop vreg) |
| 97 | 97 | OriBackCopyPropagate | 0x22BCAE5 | Optimization |  | Backward copy propagation through move chains |
| 98 | 98 | OriSplitHighPressureLiveRanges | 0x22BCFE8 | RegAlloc |  | Last-resort live-range splitter when pressure exceeds HW limits |
| 99 | 99 | OriPerformLiveDeadFourth | 0x22BCAFA | Analysis |  | Full liveness + DCE (4th instance, pre-legalization) |
| 100 | 100 | OriPropagateGmma | 0x22BCB13 | Optimization |  | Propagate WGMMA accumulator values through IR |
| 101 | 101 | InsertPseudoUseDefForConvUR | 0x22BCB24 | Lowering |  | Insert pseudo use/def for UR-conversion bookkeeping |
| 102 | 102 | FixupGmmaSequence | 0x22BCB40 | Lowering |  | Fix WGMMA instruction sequences for HW ordering constraints |
| 103 | 103 | LateEnforceArgumentRestrictions | 0x22BD008 | Lowering |  | Late ABI argument enforcement (LateEnforceArgumentRestrictions) |
| 104 | 104 | OriHoistInvariantsLate3 | 0x22BCB52 | Optimization |  | LICM late 3rd pass after GMMA fixup |
| 105 | 105 | AdvancedPhaseSetRegAttr | 0x22BCB6A | Gate |  | AdvancedPhase Type B: ctx+0x630 SM backend vtable; precedes OriSetRegisterAttr |
| 106 | 106 | OriSetRegisterAttr | 0x22BCB82 | Analysis |  | Annotate registers with scheduling attrs (latency class, bank) |
| 107 | 107 | OriCalcDependantTex | 0x22BCB95 | Analysis |  | Compute texture-instruction dependencies for scheduling |
| 108 | 108 | AdvancedPhaseAfterSetRegAttr | 0x22BCBA9 | Gate |  | AdvancedPhase Type B: 0xC607A0 -> ctx+0x630 vtable +0x110 (guard nullsub_170) |
| 109 | 109 | LateExpansionUnsupportedOps2 | 0x22BCBC6 | Lowering |  | 2nd late unsupported-op expansion (ops exposed by GMMA/attr passes) |
| 110 | 110 | FinalInspectionPass | 0x22BCBE3 | Validation |  | Final IR validation before irreversible scheduling/RA (sub_788A30) |
| 111 | 111 | SetAfterLegalization | 0x22BCBF7 | Cleanup | >1 | Set post-legalization flag on compilation context |
| 112 | 112 | ReportBeforeScheduling | 0x22BCC0C | Reporting |  | Dump IR before scheduling (no-op unless DUMPIR) |
| 113 | 113 | AdvancedPhasePreSched | 0x22BCC23 | Gate |  | AdvancedPhase: pre-sched hook -> ScheduleInstructions (bin114, sub_8D0640 22KB) |
| 114 | 114 | ScheduleInstructions | 0x21DB4A8 | Scheduling |  | Instruction scheduler worker (sub_8D0640); DUMPIR target |
| 115 | 115 | UpdateAfterScheduleInstructions | 0x22BD028 | Cleanup |  | IR refresh after scheduling |
| 116 | 116 | BackPropagateVEC2D | 0x22BCC39 | Optimization |  | Backward-propagate 2D vector register assignments |
| 117 | 117 | OriDoSyncronization | 0x22BCC4C | Scheduling | >1 | Insert synchronization (BAR/DEPBAR/MEMBAR) per GPU memory model |
| 118 | 118 | UpdateAfterOriDoSyncronization | 0x22BD048 | Cleanup |  | IR refresh after sync insertion |
| 119 | 119 | ApplyPostSyncronizationWars | 0x22BCC60 | Scheduling | >1 | Fix WAR hazards exposed by sync insertion |
| 120 | 120 | ReportBeforeRegisterAllocation | 0x22BD068 | Reporting |  | Dump IR before register allocation (no-op unless DUMPIR) |
| 121 | 121 | AdvancedPhaseAllocReg | 0x22BCC7C | Gate |  | AdvancedPhase: regalloc driver hook -> AllocateRegisters (bin122); DUMPIR=AllocateRegisters |
| 122 | 122 | AllocateRegisters | 0x21F0229 | RegAlloc |  | Register allocation worker (canonical allocator entry) |
| 123 | 123 | ReportAfterRegisterAllocation | 0x22BCC92 | Reporting |  | Dump IR after register allocation (no-op unless DUMPIR) |
| 124 | 124 | UpdateAfterOriAllocateRegisters | 0x22BD088 | Cleanup |  | IR refresh after register allocation |
| 125 | 125 | Get64bRegComponents | 0x22BCCB0 | RegAlloc |  | Split 64-bit register pairs into 32-bit components |
| 126 | 126 | AdvancedPhasePostExpansion | 0x22BCCC4 | Gate |  | AdvancedPhase: pre post-RA-expansion hook -> PostExpansion (bin127) |
| 127 | 127 | PostExpansion | 0x22BCCD1 | Lowering |  | Post-RA expansion worker |
| 128 | 128 | ApplyPostRegAllocWars | 0x22BCCDF | RegAlloc |  | Fix WAR hazards exposed by register allocation (dispatch-only) |
| 129 | 129 | AdvancedPhasePostSched | 0x22BCCF5 | Gate |  | AdvancedPhase Type C: sub_C5E830 writes ctx+1552=14 (pre PostSchedule) |
| 130 | 130 | OriRemoveNopCode | 0x22BCD0C | Cleanup |  | Remove NOP/dead placeholder instructions |
| 131 | 131 | OptimizeHotColdInLoop | 0x22BCD1D | Optimization |  | Separate hot/cold paths within loops for cache locality |
| 132 | 132 | OptimizeHotColdFlow | 0x22BCD33 | Optimization |  | Separate hot/cold paths at function level |
| 133 | 133 | PostSchedule | 0x22BCD47 | Scheduling | >0 | Post-RA re-scheduling: sub_C60640 -> ctx+0x630 vtable +0x90 (sm_80+) |
| 134 | 134 | AdvancedPhasePostFixUp | 0x22BCD54 | Gate |  | AdvancedPhase: pre post-fixup hook -> PostFixUp (bin140, target vtable +0x148) |
| 135 | 135 | PlaceBlocksInSourceOrder | 0x22BCD6B | Cleanup |  | Determine final basic-block layout in emitted binary |
| 136 | 136 | PostFixForMercTargets | 0x22BCD84 | Encoding |  | Fix instructions for Mercury encoding requirements (dispatch-only) |
| 137 | 137 | FixUpTexDepBarAndSync | 0x22BCD9A | Scheduling |  | Fix texture dependency barriers + sync post-scheduling |
| 138 | 138 | AdvancedScoreboardsAndOpexes | 0x22BCDB0 | Gate | >0 | Full scoreboard gen: 23-bit control word/inst (sub_A36360 + DAG sched sub_A23CF0); O1+ only |
| 139 | 139 | ProcessO0WaitsAndSBs | 0x22BCDCD | Scheduling | ==0 | Conservative scoreboard insertion for -O0; sub_C5E2A0 on sm50+ -> target vtable +0x150 |
| 140 | 140 | PostFixUp | 0x22BCD61 | Cleanup |  | Target-specific post-fixup: sub_C5E270 -> target vtable +0x148 |
| 141 | 141 | MercConverter | 0x21E5C39 | Encoding |  | MercConverter 2nd pass: re-lower opcodes via sub_9F1A90/sub_9ED2D0; bit cu+1398&0x20 |
| 142 | 142 | MercEncodeAndDecode | 0x22BCDE2 | Encoding |  | MercEncodeAndDecode: encode to Mercury node + round-trip verify (sub_6D9690) |
| 143 | 143 | MercExpandInstructions | 0x22BCDF6 | Encoding |  | Expand compound Mercury pseudo-insts -> primitive SASS (sub_C3DFC0) |
| 144 | 144 | MercGenerateWARs1 | 0x22BCE0D | Encoding |  | Generate WAR hazard annotations (1st pass, pre-expansion; sub_6FC240) |
| 145 | 145 | MercGenerateOpex | 0x22BCE1F | Encoding |  | Generate opex (operation-extension) annotations per instruction |
| 146 | 146 | MercGenerateWARs2 | 0x22BCE30 | Encoding |  | Generate WAR annotations (2nd pass, post-expansion hazards) |
| 147 | 147 | MercGenerateSassUCode | 0x22BCE42 | Encoding |  | Produce final SASS microcode bytes (actual binary encoding) |
| 148 | 148 | ComputeVCallRegUse | 0x22BCE58 | RegAlloc |  | Compute register usage for virtual call sites |
| 149 | 149 | CalcRegisterMap | 0x22BCE6B | RegAlloc |  | Compute final physical-to-logical register map (EIATTR metadata) |
| 150 | 150 | UpdateAfterPostRegAlloc | 0x22BCE7B | Cleanup |  | IR refresh after post-RA processing (late dup, no-op by default) |
| 151 | 151 | ReportFinalMemoryUsage | 0x22BCE93 | Reporting |  | Print memory-pool consumption summary (late dup, no-op by default) |
| 152 | 152 | AdvancedPhaseOriPhaseEncoding | 0x22BCEAA | Gate |  | AdvancedPhase Type C: sub_C5E0B0 writes ctx+1552=21 (encoding boundary) |
| 153 | 153 | FormatCodeList | 0x22BCED3 | Encoding |  | Format final code list |
| 154 | 154 | UpdateAfterFormatCodeList | 0x22BCEC8 | Cleanup |  | Rebuild code list after Mercury reformats instructions |
| 155 | 155 | DumpNVuCodeText | 0x22BCEE2 | Reporting |  | Dump human-readable SASS text disassembly (--keep/DUMPIR) |
| 156 | 156 | DumpNVuCodeHex | 0x22BCEF2 | Reporting |  | Dump raw SASS binary as hex (--keep/DUMPIR) |
| - | 157 | DebuggerBreak | 0x22BCF01 | Cleanup |  | DebuggerBreak: dev hook, triggers breakpoint (empty in release) |
| - | 158 | NOP | 0x21E6C80 | Cleanup |  | Terminal NOP / dispatch sentinel (lookup-failure return value) |
