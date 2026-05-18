# LLVM Patch Catalog

> **Index page.** This is a one-row-per-component summary of every upstream LLVM file that cicc v13.0 carries, classified by patch class. For the underlying analysis, follow the **Wiki page** column to the per-pass deep dive. For the orchestration that schedules these components, see [Pipeline & Ordering](pipeline.md).

CICC v13.0 statically links a fork of LLVM 20.0.0. Some files are byte-for-byte upstream; some carry GPU-specific threshold overrides registered as new `cl::opt` knobs; some are recognizably upstream but have algorithmic additions tied to the [GPU execution model](../gpu-execution-model.md); and a handful are fully proprietary with no upstream counterpart. This page enumerates every component covered by the [LLVM Passes](pipeline.md) section of the wiki and labels each.

## Patch Classes

| Class | Meaning | Reimplementation strategy |
|---|---|---|
| **none** | Stock LLVM 20.0.0. No behavioral delta from upstream detected in the binary. Symbol layout may differ from a default LLVM build because of NVIDIA's LTO inlining choices, but the algorithm is unchanged. | Use the upstream `.cpp` directly. |
| **config-only** | Upstream algorithm preserved verbatim, but cicc registers extra `cl::opt` knobs (under the `NVVMPassOptions` umbrella), overrides upstream defaults, and re-runs the pass at additional pipeline positions. No source-level edits to the core algorithm. | Use upstream and re-apply the knob defaults from the cicc constructor. |
| **algorithmic** | Upstream algorithm clearly recognizable, but cicc adds GPU-specific code paths: extra DAG combines, intrinsic-aware fast paths, NVPTX-aware safety checks, expanded opcode coverage, structural reorganizations, or extra scoped tables. Reimplementing means stock LLVM **plus** patches. | Use upstream as the base, port the NVIDIA delta as a fork patch. |
| **proprietary fork** | Either a complete rewrite of an upstream pass (sharing only the registration name) or a pass that has no upstream equivalent at all. The binary may also ship the stock upstream copy alongside the proprietary one for unrelated linkage reasons. | Implement from scratch from the wiki deep-dive page. The upstream `.cpp` is **not** a viable starting point. |

Within the tables below, rows are grouped by upstream LLVM directory (the layout reimplementers will most naturally use when porting). Each row points at the dedicated wiki page where the delta is documented in detail. The "Upstream file" column refers to LLVM 20.0.0 unless otherwise noted.

## Provenance Signals

A row's patch class is assigned by combining four signals visible in the binary:

1. **Symbol-level fingerprints.** Function lengths, hash-table sizes, switch case counts, and constructor patterns line up against the same compilation of LLVM 20.0.0. A function whose disassembly walks the same case labels in the same order as the upstream `.cpp` (read alongside the wiki deep-dive) is `none` or `config-only`. A function that emits extra basic blocks before joining the upstream control flow is `algorithmic`. A function that shares only the entry-point name with upstream is `proprietary fork`.
2. **Knob inventory.** `cl::opt<...>` constructors (`ctor_NNN` symbols at fixed addresses) register the cicc-side tunables. Knobs whose names match upstream verbatim and whose defaults match upstream constants are `config-only` signals. Knobs whose names are NVIDIA-prefixed (`nv-*`, `nvvm-*`) or whose registrar uses NVIDIA-specific calling conventions (`sub_190BE40`'s `int**` default initializer) are `algorithmic` signals.
3. **Pipeline placement.** A pass that is scheduled multiple times, at non-upstream pipeline positions, or under cicc's tier system (Tier 0/1/2/3 in `sub_12E54A0`) carries scheduling-level customization even when the per-pass binary is stock.
4. **Companion NVIDIA-only symbols.** Where the cicc pipeline calls a stock pass and **immediately follows it with** an NVIDIA-only pass that compensates for what the stock pass cannot do on GPU (the second-pass coalescer after `RegisterCoalescer`, MRPA after `MachineScheduler`, the NVPTX-specific block placement after `MachineBlockPlacement`), the upstream row is labeled `none` but the companion row appears under the Target / NVPTX table as `proprietary fork`.

Rows where the binary contains both an unused stock upstream copy and an active NVIDIA copy (StructurizeCFG, LSR, the dual `RAGreedy` instances) are labeled by the **active** copy. See the quirks at the bottom of the page for why both copies coexist.

## Pipeline & Infrastructure

| Upstream file | Patch class | NVIDIA delta summary | Wiki page |
|---|---|---|---|
| `lib/Passes/PassBuilder.cpp` | algorithmic | 33 NVIDIA passes injected via the standard extension-callback table at `[PassBuilder+2208]`. Custom per-tier driver (`sub_12E54A0`) layered on top of the stock `PassBuilder`. | [Pipeline & Ordering](pipeline.md) |
| `lib/Passes/PassBuilderPipelines.cpp` | algorithmic | Tier 0/1/2/3 sub-pipelines, seven `nvopt<...>` shorthand expansions, GPU-aware ordering between SROA, LSR, and structurization. | [Pipeline & Ordering](pipeline.md) |
| `lib/Passes/PassRegistry.def` | config-only | Extended with NVIDIA pass names (`nvvm-reflect`, `process-restrict`, `lower-struct-args`, `lower-aggr-copies`, `nv-memory-space-opt`, etc.) registered through the same `StringMap<PassInfo>`. | [Pipeline & Ordering](pipeline.md) |

## Analysis

| Upstream file | Patch class | NVIDIA delta summary | Wiki page |
|---|---|---|---|
| `lib/Analysis/ConstantFolding.cpp` | algorithmic | Eligibility checker (`sub_14D90D0`) and evaluator (`sub_14D1BC0`) layered on top of `ConstantFoldCall`. Recognizes 110+ math name variants (C, glibc `__*_finite`, mangled C++) and 60+ NVVM intrinsic IDs. Exception-safe host FPU wrapper that aborts folding on domain errors. | [Constant Folding](constant-folding.md) |
| `lib/Analysis/ValueTracking.cpp` (KnownBits) | algorithmic | `computeKnownBits` and `SimplifyDemandedBits` fused into a single 127 KB function (`sub_11A7600`). GPU-specific oracle (`sub_F0C4B0`) returns ranges for `%tid`, `%ntid`, `%ctaid`, `%nctaid`, `%warpsize`, `%laneid`. | [KnownBits & DemandedBits](known-bits.md) |
| `lib/Analysis/ScalarEvolution.cpp` | algorithmic | Non-recursive worklist `createSCEV` (matches LLVM 16+ refactor). NVIDIA additions: `simple_mode` complexity gate, GPU-specific SCEV sources (thread-index bounds), CUDA loop-idiom recognition (warp-stride, grid-stride). Core SCEV algebra untouched. | [SCEV Overview](scev.md), [SCEV Ranges](scev-range-btc.md), [SCEV Invalidation](scev-invalidation.md) |

## Transforms / Scalar

| Upstream file | Patch class | NVIDIA delta summary | Wiki page |
|---|---|---|---|
| `lib/Transforms/Scalar/SROA.cpp` | none | LLVM 17+ two-pass analysis path (`qword_50055E8`); `preserve-cfg`/`modify-cfg` parser params unchanged. No detected modification to core splitting. NVIDIA's contribution is purely **scheduling** — SROA runs twice (early + late) in the cicc pipeline. | [SROA Deep Dive](sroa.md) |
| `lib/Transforms/Scalar/EarlyCSE.cpp` | algorithmic | Four CUDA extensions: barrier-aware memory versioning (`__syncthreads` invalidation), AS 7 store-fwd protection, NVVM intrinsic CSE handler (`sub_2780450`) with thread-invariant special-register fast path, PHI operand limit = 5. Adds a fourth scoped hash table (store-forwarding) absent upstream. | [EarlyCSE Deep Dive](early-cse.md), [Scalar Passes](scalar-passes.md) |
| `lib/Transforms/InstCombine/*.cpp` | algorithmic | ~15 upstream files inlined into a single 405 KB visitor (`sub_10EE7A0`). 80 unique opcode cases, three-level switch covering standard LLVM opcodes plus NVIDIA-extended FMA/vector opcodes (238–245) plus three high-opcode NVVM intrinsics (9549, 9553, 9567). Separate 87 KB intrinsic folder (`sub_1169C30`). | [InstCombine](instcombine.md) |
| `lib/Transforms/Scalar/GVN.cpp` | algorithmic | 11 NVIDIA tunables for PRE, store splitting, PHI removal, dominator caching, and recursion depth. Custom registrar `sub_190BE40` for `int**`-style defaults. | [GVN](gvn.md) |
| `lib/Transforms/Scalar/NewGVN.cpp` | algorithmic | Ships alongside classic GVN at `0x19F99A0` (68 KB). Same knob constructor (`ctor_201`). Used at NewGVN/GVNHoist pipeline slot. | [GVN](gvn.md) |
| `lib/Transforms/Scalar/JumpThreading.cpp` | config-only | Core algorithm unmodified. Adds loop-aware duplication threshold overrides (`qword_501D628`, `qword_501D548`), extra pipeline positions, an OCG disable flag, and `NVVMPassOptions` integration. | [JumpThreading Deep Dive](jump-threading.md) |
| `lib/Transforms/Scalar/LICM.cpp` | config-only | Stock IR-level LICM. No NVIDIA-specific knobs in the IR pass registration. `disable-machine-licm` and `disable-postra-machine-licm` are upstream knobs. | [LICM](licm-real.md) |
| `lib/Transforms/Scalar/DeadStoreElimination.cpp` | algorithmic | 91 KB across three functions. Partial store forwarding with type conversion, cross-store dependency tracking, store-chain decomposition for aggregates, native CUDA vector type (`float4`, etc.) overwrite recognition. | [DSE](dse.md) |
| `lib/CodeGen/CodeGenPrepare.cpp` | none | All 20+ upstream `cl::opt` knobs preserved unchanged. | [CodeGenPrepare](codegen-prepare.md) |
| `lib/Transforms/Scalar/LoopStrengthReduce.cpp` | proprietary fork | Stock LLVM LSR present at `0x284F650`–`0x287C150`, but a **separate** 160 KB NVIDIA formula solver (`sub_19A87A0`, 2688 lines, wrapped by `NVLoopStrengthReduce` at `sub_19CE990`) replaces formula generation and selection. 7-phase pipeline with 11 GPU-aware knobs. SCEV infrastructure and IV rewriting reused. | [LSR (NVIDIA)](lsr.md) |
| `lib/Transforms/Scalar/StructurizeCFG.cpp` | proprietary fork | Stock AMDGPU copy at `sub_1F0EBC0` shipped for linkage, but the pipeline uses NVPTX-customized copy at `sub_35CC920` (95 KB, 2,397 lines). Irreducibility detector, uniform-branch classifier, region structurizer core, edge-reroute, and domtree NCA walk are all separate symbols from the AMDGPU original. | [StructurizeCFG](structurizecfg.md) |
| `lib/Transforms/Scalar/SCEV-CGP.cpp` (NVIDIA-only) | proprietary fork | Has no upstream counterpart. Disabled by default (`nv-disable-scev-cgp = true`); when enabled, SCEV-driven GEP/IV restructuring runs before CodeGenPrepare. | [CodeGenPrepare](codegen-prepare.md) |

## Transforms / Loop

| Upstream file | Patch class | NVIDIA delta summary | Wiki page |
|---|---|---|---|
| `lib/Transforms/Utils/LoopSimplify.cpp` | none | Stock LLVM canonicalization (single preheader, single backedge, dedicated exits). | [Standard Loop Passes](loop-passes-standard.md), [Loop Passes Overview](loop-passes.md) |
| `lib/Transforms/Utils/LCSSA.cpp` | none | Stock LLVM. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/LoopRotation.cpp` | none | Stock LLVM. Appears in the pipeline multiple times (canonicalization prerequisite for LICM and unroll). | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/IndVarSimplify.cpp` | config-only | Three NVIDIA knobs that materially change LFTR / IV-widening behavior on GPU code. Algorithm itself unchanged. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/LoopIdiomRecognize.cpp` | none | Stock memcpy/memset/mismatch recognition. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/LoopInterchange.cpp` | config-only | Stock algorithm with threshold overrides. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/LoopDistribute.cpp` | config-only | Stock algorithm with threshold overrides. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/IRCE.cpp` | none | Stock LLVM Inductive Range Check Elimination. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/LoopDeletion.cpp` | none | Stock LLVM. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Scalar/LoopSink.cpp` | none | Stock LLVM. | [Standard Loop Passes](loop-passes-standard.md) |
| `lib/Transforms/Utils/LoopUnroll.cpp` (transformation engine) | config-only | Lightly modified upstream `llvm::UnrollLoop`. Bulk of the NVIDIA delta lives in the decision engine, not here. | [Loop Unrolling](loop-unroll.md) |
| `lib/Transforms/Scalar/LoopUnrollPass.cpp` (decision engine) | algorithmic | `computeUnrollCount` substantially reworked: priority-based decision cascade, local-array threshold multiplier, power-of-two factor enforcement, pragma threshold ~200x stock. Runs twice in the pipeline (`sub_197E720` / `sub_19C1680`, the latter gated by `opts[1360]`). | [Loop Unrolling](loop-unroll.md) |
| `lib/Transforms/Vectorize/LoopVectorize.cpp` | algorithmic | NVPTX's `TTI::getRegisterBitWidth()` returns 32, which would force VF=1 under stock LLVM; NVIDIA overrides the VF computation to produce v2/v4 vectorization that maps onto packed 32-bit register pairs and PTX `ld.v2`/`ld.v4`. VPlan kept. | [LoopVectorize & VPlan](loop-vectorize.md) |
| `lib/Transforms/Vectorize/SLPVectorizer.cpp` | algorithmic | Stock SLP algorithm; the divergence is in the proprietary NVPTX TTI implementation that this pass queries for cost. TTI differs significantly from upstream open-source NVPTX. | [SLP Vectorizer](slp-vectorizer.md) |

## CodeGen / Machine-Level (Target-Independent)

| Upstream file | Patch class | NVIDIA delta summary | Wiki page |
|---|---|---|---|
| `lib/CodeGen/SelectionDAG/SelectionDAG.cpp` | none | Target-independent infrastructure at `0xF05000`–`0xF70000` is stock LLVM 20 with no detected modifications. | [SelectionDAG](selectiondag.md) |
| `lib/CodeGen/SelectionDAG/DAGCombiner.cpp` | none | Generic combiner stock; NVPTX-specific combines hosted in `NVPTXISelLowering` (see target section). | [SelectionDAG](selectiondag.md) |
| `lib/CodeGen/SelectionDAG/LegalizeDAG.cpp` | config-only | Stock algorithm; legality tables driven by NVPTX target hooks. 137 KB action-dispatch function (`sub_1FFB890`) has 967 cases due to the NVPTX opcode count, not source modification. | [SelectionDAG](selectiondag.md) |
| `lib/CodeGen/SelectionDAG/LegalizeTypes.cpp` (+ Integer/Float/Vector) | algorithmic | Four upstream files collapsed into a single 348 KB monolithic function (`sub_20019C0`) — either LTO inlining artifact or deliberate I-cache locality. Functional behavior faithful to upstream `DAGTypeLegalizer`. | [Type Legalization](type-legalization.md) |
| `lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp` | algorithmic | Per-function cost table, priority-queue topological worklist, three-level dispatch (`sub_3090F90` → `sub_308FEE0` → `sub_347A8D0` / `sub_348D3E0`). Iteration budget guards pathological DAGs. | [ISel Pattern Matching](isel-patterns.md) |
| `lib/CodeGen/SelectionDAG/InstrEmitter.cpp` | algorithmic | Dedicated CopyToReg handler for the `.param` ABI, triple-vtable dispatch gating GPU pseudo-expansion, extended `MachineInstr` flag at bit 36 (`0x1000000000`) absent from stock LLVM. | [InstrEmitter](instr-emitter.md) |
| `lib/CodeGen/TwoAddressInstructionPass.cpp` | algorithmic | Structurally stock LLVM (the libNVVM build at `sub_F4EA80` is byte-for-byte identical). Four additions: extended `EXTRACT_SUBREG` handling for multi-register results, deeper `LiveVariables` maintenance, OptRemark integration, unconditional post-pass verifier. | [TwoAddressInstruction](two-address.md) |
| `lib/CodeGen/MachineScheduler.cpp` | config-only | Stock `ScheduleDAGMILive`. NVPTX provides MRPA incremental pressure tracker and Texture Group Merge as **separate** NVIDIA-only passes (see Targets table). | [Instruction Scheduling](scheduling.md) |
| `lib/CodeGen/MachinePipeliner.cpp` | none | Stock Swing Modulo Scheduler. | [Instruction Scheduling](scheduling.md) |
| `lib/CodeGen/LiveRangeCalc.cpp` | algorithmic | Same `updateSSA` algorithm as upstream, but a global fast-compile flag (`qword_5025F68`) bypasses the entire dataflow loop — no upstream equivalent. Likely wired to `-Ofast-compile` / `-O0`. | [LiveRangeCalc](live-range-calc.md) |
| `lib/CodeGen/RegisterCoalescer.cpp` | none | Stock worklist-driven coalescer at `sub_2F71140`. Handles the generic `COPY` pseudo. The NVPTX-specific coalescer is a **separate** NVIDIA pass (see Targets). | [Register Coalescing](register-coalescing.md) |
| `lib/CodeGen/RegAllocGreedy.cpp` (+ SplitKit, LiveRangeEdit) | algorithmic | Two complete copies of `RAGreedy` (legacy PM at `0x1EC0400`, new PM at `0x2F4C2E0`). Pressure-driven allocation, `-maxreg` ceiling, occupancy-aware rematerialization layered via TTI hooks and custom knobs. | [Register Allocation](register-allocation.md) |
| `lib/CodeGen/PrologEpilogInserter.cpp` | algorithmic | Ten-phase monolithic implementation. Significantly more sophisticated than upstream's linear scan; tied to PTX `.local` frame layout and the NVPTX-specific `nvptx-prolog-epilog` machine pass. | [PrologEpilogInserter](prolog-epilog.md) |
| `lib/CodeGen/BranchFolding.cpp` | algorithmic | Critical divergence: cicc **removes** the `requiresStructuredCFG()` gate that upstream uses to disable tail merging on GPU targets, and adds a reserved-register merge safety check absent from any upstream version. | [BranchFolding & TailMerge](branch-folding.md) |
| `lib/CodeGen/MachineBlockPlacement.cpp` | algorithmic | Two instances: stock LLVM copy for internal use plus an NVPTX-pipeline copy at `sub_3521FF0`. The NVPTX instance queries a divergence flag on the MachineFunction for tail-duplication profitability and adds an alternative layout proposal path (`sub_34BEDF0` / `sub_34C7080`) absent upstream. | [Block Placement](block-placement.md) |
| `lib/CodeGen/MachineOutliner.cpp` | none | Byte-for-byte upstream. NVPTX delta is limited to two target hooks (`NVPTXInstrInfo::getOutliningType` and the calling convention 95 assignment) and an activation policy in `NVPTXPassConfig::addMachineOutliner`. | [MachineOutliner](machine-outliner.md) |

## Target / NVPTX

| Upstream file | Patch class | NVIDIA delta summary | Wiki page |
|---|---|---|---|
| `lib/Target/NVPTX/NVPTXISelLowering.cpp` | proprietary fork | 111 KB `LowerOperation` dispatcher (`sub_32E3060`), 88 KB `LowerCall` (`sub_3040BF0`) implementing the PTX `.param`-space calling convention, 343 KB intrinsic-lowering mega-switch (`sub_33B0210`, 9518 lines) covering >200 CUDA intrinsic IDs up to 14196. NVPTX-side DAG combine (`sub_3425710`, 142 KB) and computeKnownBits (`sub_33D4EF0`, 114 KB). | [SelectionDAG](selectiondag.md) |
| `lib/Target/NVPTX/NVPTXISelLowering.h` (NVPTXISD enum) | proprietary fork | 460 distinct `NVPTXISD::*` opcodes — roughly 15x upstream's ~30. 372/460 are the texture/surface family; the rest cover SM90+ load/store variants, the four call-flavor matrix, funnel-shift-with-clamp, bitfield extract/insert, and the `.param` calling convention. | [NVPTXISD Opcodes](nvptxisd-opcodes.md) |
| `lib/Target/NVPTX/NVPTXISelDAGToDAG.cpp` | algorithmic | Three-level NVPTX-specific select dispatch (`sub_3090F90` driver, `sub_347A8D0` hand-written switch at 309 KB, `sub_348D3E0` TableGen-generated `SelectCode` at 256 KB), plus six sub-selectors for memory, texture/surface, complex addressing, vector patterns, atomics. Compressed per-SM legality table gates which opcodes exist per architecture. | [ISel Pattern Matching](isel-patterns.md) |
| `lib/Target/NVPTX/NVPTXInstrInfo.td` (TableGen) | proprietary fork | NVPTX patterns expanded to cover MMA/tensor-core families, surface/texture intrinsics, and SM-version-gated instructions. The TableGen-generated matcher feeds into `sub_348D3E0`. | [ISel Pattern Matching](isel-patterns.md) |
| `lib/Target/NVPTX/NVPTXTargetMachine.cpp` | algorithmic | `registerPassBuilderCallbacks` injects the 33 NVIDIA passes into the New PM extension table. `NVPTXPassConfig::addMachineOutliner` controls outliner activation. | [Pipeline & Ordering](pipeline.md) |
| MMA codegen (NVIDIA-only) | proprietary fork | No upstream MMA in `NVPTXISelLowering.cpp`. Full tensor-core pipeline spanning Volta through Blackwell (HMMA/IMMA/BMMA), SM90 WGMMA, SM100 tcgen05. Two parallel lowering paths (`sub_955A70` NVVM builtin + `sub_33B0210` SelectionDAG intrinsic) converging at a common PTX string builder driven by a packed 64-bit descriptor. | [Tensor / MMA Codegen](mma-codegen.md) |
| NVPTX-specific Register Coalescer (NVIDIA-only) | proprietary fork | Second coalescer at `sub_34AF4A0` runs after the stock `RegisterCoalescer`. Handles NVPTX pseudo-COPYs (typed register-class boundaries: `%r` ↔ `%rd`, `%f` ↔ `%fd`) that the generic algorithm cannot reason about. | [Register Coalescing](register-coalescing.md) |
| MRPA / Texture Group Merge (NVIDIA-only) | proprietary fork | Two scheduler-adjacent passes absent from upstream: MRPA tracks incremental register pressure for the scheduler's reorder decisions; Texture Group Merge clusters texture fetches to maximize TEX unit utilization. | [Instruction Scheduling](scheduling.md), [Machine-Level Passes](machine-passes.md) |
| Machine-pass umbrella | mixed | 51 of 64 registered MF passes are stock LLVM 20.0.0; 13 are NVIDIA-only. The NVIDIA additions cluster around four areas: PTX structurization (`nvptx-lower-aggr-copies`, `nvptx-lower-args`), pre-RA pressure shaping (MRPA, [IV demotion](../passes/iv-demotion.md), [rematerialization](../passes/rematerialization.md)), texture-group merging, and AsmPrinter glue (`nvptx-prolog-epilog`, `nvptx-proxy-reg-erasure`). | [Machine-Level Passes](machine-passes.md) |

## Summary Counts

| Class | Count |
|---|---|
| none (stock LLVM) | 18 |
| config-only | 9 |
| algorithmic | 19 |
| proprietary fork | 9 |
| mixed | 1 |
| **total rows** | **56** |

(Counts include both upstream files and the small set of NVIDIA-only entries documented under their nearest-upstream sibling.)

## Quirks of the Classification

> **QUIRK -- "Stock" sometimes means two copies side-by-side.** Several passes that classify as `none` or `config-only` (notably `StructurizeCFG`, `MachineBlockPlacement`, `RegisterCoalescer`, and `LoopStrengthReduce`) ship as **two distinct symbols** in the binary: the upstream copy that arrives from linking against `libLLVMCodeGen`/`libLLVMScalarOpts` and a separate NVIDIA copy that the cicc pipeline actually invokes. The unused upstream copy is dead code from the pipeline's perspective but cannot be stripped because other components in the binary still link the upstream symbol. Reimplementers should not be misled by sizeof-binary into thinking these are heavily modified — the modification is in **which copy gets scheduled**, not in the algorithm of either copy.

> **QUIRK -- LTO inlining inflates apparent patch class.** `LegalizeTypes` is labeled `algorithmic` because all four upstream files (`LegalizeTypes.cpp`, `LegalizeIntegerTypes.cpp`, `LegalizeFloatTypes.cpp`, `LegalizeVectorTypes.cpp`) collapse into a single 348 KB function. The functional behavior is a faithful reproduction of upstream `DAGTypeLegalizer`; the structural shape is the only delta. Likewise, `InstCombine`'s ~15 upstream files inline into one 405 KB visitor. A faithful reimplementation can keep the upstream file split — the monolithic shape is a build-system artifact, not a behavioral requirement.

> **QUIRK -- "config-only" hides materially different schedules.** A pass classified `config-only` (e.g. SROA, LICM) may still produce dramatically different output from a stock LLVM 20 invocation because cicc **runs it more than once** and at different pipeline positions. SROA runs twice (early after NVVMReflect, late after sinking); LICM runs early (hoist) plus late (sink); LoopRotate appears at several canonicalization points. The pass binary is upstream, but the scheduled effect is GPU-tuned. The full ordering lives in [Pipeline & Ordering](pipeline.md).

## How to Use This Catalog

For a reimplementer porting cicc to an open LLVM base, the patch class maps directly to the work effort:

* **none** rows: link the matching upstream `.cpp` unchanged. Re-apply scheduling from [Pipeline & Ordering](pipeline.md).
* **config-only** rows: link upstream and re-register the cicc knobs and their defaults. Wire into the same pipeline slots.
* **algorithmic** rows: start from upstream, then port the NVIDIA-specific extensions documented on the linked wiki page. Expect roughly one fork patch per row.
* **proprietary fork** rows: build from the wiki deep dive. Upstream provides only the registration interface and surrounding infrastructure; the algorithm itself comes from the binary analysis.

For the inverse view — pipeline slot to pass list — see [Pipeline & Ordering](pipeline.md). For NVIDIA-only passes that do not appear in this catalog at all (because they have no upstream counterpart even at the file level), see [NVIDIA Custom Passes](../passes/index.md).

### Suggested Porting Order

A reimplementation that follows the binary's optimization order rather than the upstream LLVM source order tends to surface fewer surprises:

1. Start with the **none** rows — link upstream, confirm the cicc pipeline assembler will accept them, and lock in the registration names.
2. Layer in the **config-only** rows. The knob defaults live in the cicc constructors documented on each deep-dive page; mismatched defaults are the most common cause of "the pass runs but the output is different" bugs during porting.
3. Port the **algorithmic** rows next, one wiki page at a time. The deep-dive pages enumerate the NVIDIA additions as discrete patches against the upstream `.cpp`; each can be applied and tested independently.
4. Tackle the **proprietary fork** rows last. These have the longest implementation tail (LSR alone is 2,688 decompiled lines; the SelectionDAG intrinsic switch is 9,518) and benefit from the surrounding infrastructure being already in place when their tests run.

### Where the Catalog is Deliberately Incomplete

This catalog covers the LLVM-derived components only. Three classes of cicc functionality are intentionally absent:

* **NVIDIA-only IR passes** ([Memory Space Opt](../passes/memory-space-opt.md), [NVVM Reflect](../passes/nvvm-reflect.md), [Printf Lowering](../passes/printf-lowering.md), the [process-restrict](../passes/other.md) family, etc.) live under [NVIDIA Custom Passes](../passes/index.md). They have no upstream file to point at.
* **The EDG frontend** that feeds the LLVM pipeline (NVVM IR generation, builtin lowering at `sub_955A70`) is documented under [Pipeline Overview](../pipeline/overview.md) and is not derived from LLVM at all.
* **PTX emission** (the AsmPrinter at `sub_21E74C0` and the surrounding text-writer infrastructure) is heavily NVIDIA-customized but classifying it row-by-row would require enumerating every PTX directive; the [Pipeline / Emission](../pipeline/emission.md) page covers it as one unit.

Together this catalog plus the [NVIDIA Custom Passes Overview](../passes/index.md) cover the full optimization-and-codegen surface of cicc v13.0.
