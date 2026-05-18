# NVIDIA Custom Passes

## Canonical Count

**35 NVIDIA custom passes** is the headline number used throughout this wiki.

> **Definition.** A *NVIDIA custom pass* is a pass *class* (a `PassInfoMixin` subclass or `MachineFunctionPass` subclass) registered by cicc that has **no upstream LLVM equivalent** -- i.e., its symbol is absent from a stock LLVM 20.0.0 build of `lib/Passes/PassRegistry.def`, `lib/Target/NVPTX`, and the public `llvm::*` namespace dumps. The count is over **pass classes**, not registration entries: a single class registered under multiple `StringMap` keys (e.g., a parameterized variant or a pipeline-shorthand wrapper) is counted once. Pure **analyses** (classes that produce results consumed by other passes but perform no IR/MIR mutation themselves) are counted **separately** and are not part of the 35.

| Category | Count | Notes |
|---|---|---|
| IR-level NVIDIA pass classes | 22 | 16 module + 9 function + 1 loop in the tables below, minus 4 parameterized/shorthand re-registrations |
| Machine-level NVIDIA pass classes | 13 | All 13 entries in the machine table are distinct classes |
| **Total NVIDIA custom passes** | **35** | Headline number |
| NVIDIA custom analyses | 2 | `rpa`, `merge-sets` — counted separately |
| Registration sites | -- | `sub_2342890` (New PM) + `sub_12E54A0` (pipeline assembler) |
| Dedicated deep-dive pages | 22 | One per major pass |

> **Why 33 also appears.** [`llvm/pipeline.md`](../llvm/pipeline.md) cites **33** when counting the `StringMap` registration entries inserted by `sub_2342890` -- the line that mentions "12 module + 20 function + 1 loop" reflects raw entries, including parameterized variants and short pipeline aliases registered under separate names. Both numbers describe the same code; 35 counts unique pass classes, 33 counts registration-table rows.

> **QUIRK.** The pipeline.md per-scope split ("12 module + 20 function + 1 loop") and this page's per-scope split ("16 module + 9 function + 1 loop") count *different things*. pipeline.md sums `StringMap` keys per scope (and many module-level passes also register a function-scope wrapper key, inflating the "function" bucket). The tables on this page count distinct pass classes by their *primary* scope -- the scope at which the pass's `run()` method actually mutates IR. Neither row-count is wrong; they answer different questions.

> **QUIRK.** Three IR-level pass *names* in the tables below (`nvvm-pretreat`, `check-kernel-functions`, `check-gep-index`) are verifier-shaped: they fail the build on invalid IR rather than transforming it. They are still counted in the canonical 35 because they are pass classes registered as transformations in `sub_2342890`, not as `AnalysisInfoMixin` analyses. The two true analyses (`rpa`, `merge-sets`) sit out.

## IR-Level Module Passes

| Pass Name | Class / Function | Size | Description |
|---|---|---|---|
| [`memory-space-opt`](./memory-space-opt.md) | `sub_1C70910` / `sub_1CA2920` | cluster | Resolves generic pointers to specific address spaces (global/shared/local/const). Warns on illegal ops: atomics on constant mem, wmma on wrong space. Parameterized: `first-time`, `second-time`, `no-warnings`, `warnings` |
| [`printf-lowering`](./printf-lowering.md) | `sub_1CB1E60` | 31KB | Lowers `printf` → `vprintf` + local buffer. Validates format string is a literal. `"vprintfBuffer.local"`, `"bufIndexed"` |
| [`nvvm-verify`](./nvvm-verify-deep.md) | `sub_2C80C90` | 230KB | Three-layer NVVM IR verifier (module + function + intrinsic). Validates triples, address spaces, atomic restrictions, pointer cast rules, architecture-gated intrinsic availability |
| `nvvm-pretreat` | `PretreatPass` | — | IR pre-treatment before optimization |
| `check-kernel-functions` | `NVPTXSetFunctionLinkagesPass` | — | Kernel function linkage validation |
| `check-gep-index` | — | — | GEP index validation |
| `cnp-launch-check` | `CNPLaunchCheckPass` | — | Cooperative launch validation |
| [`ipmsp`](./ipmsp.md) | `IPMSPPass` / `NVVMIPMemorySpacePropagationPass` | — | Inter-procedural memory space propagation. The full `getTypeName()` symbol is `llvm::NVVMIPMemorySpacePropagationPass`; see [IPMSP](./ipmsp.md) |
| `nv-early-inliner` | — | — | NVIDIA early inlining pass |
| `nv-inline-must` | `InlineMustPass` | — | Force-inline functions marked `__forceinline__` |
| `select-kernels` | `SelectKernelsPass` | — | Kernel selection for compilation |
| `set-global-array-alignment` | — | — | Parameterized: `modify-shared-mem`, `skip-shared-mem`, `modify-global-mem`, `skip-global-mem` |
| [`lower-aggr-copies`](./struct-splitting.md) | — | 72KB+58KB | Lower aggregate copies: [struct splitting](./struct-splitting.md), [memmove unrolling](./memmove-unroll.md). Param: `lower-aggr-func-args` |
| `lower-struct-args` | — | — | Lower structure arguments. Param: `opt-byval` |
| `process-restrict` | — | — | Process `__restrict__` annotations. Param: `propagate-only` |
| [`lower-ops`](./fp128-emulation.md) | `LowerOpsPass` | — | Lower special operations. Includes [FP128/I128 emulation](./fp128-emulation.md) via 48 `__nv_*` library calls |

## IR-Level Function Passes

| Pass Name | Function | Size | Description |
|---|---|---|---|
| [`branch-dist`](./branch-distribution.md) | `sub_1C47810` cluster | — | Branch distribution optimization. Knobs: `branch-dist-block-limit`, `branch-dist-func-limit`, `branch-dist-norm` |
| [`nvvm-reflect`](./nvvm-reflect.md) | `sub_1857160` | — | Resolves `__nvvm_reflect()` calls to integer constants based on target SM and FTZ mode. Runs multiple times as inlining exposes new calls |
| `nvvm-reflect-pp` | — | — | NVVM reflect preprocessor |
| [`nvvm-intrinsic-lowering`](./nvvm-intrinsic-lowering.md) | `sub_2C63FB0` | 140KB | Lowers `llvm.nvvm.*` intrinsics to standard LLVM IR. Two levels: 0 = basic, 1 = barrier-aware. Runs up to 10 times in mid pipeline |
| [`nvvm-peephole-optimizer`](./nvvm-peephole.md) | — | — | NVVM-specific peephole optimizations |
| [`remat`](./rematerialization.md) | `sub_1CE7DD0` | 67KB | IR-level rematerialization. Analyzes live-in/live-out register pressure per BB. Contains [IV demotion](./iv-demotion.md) sub-pass (75KB) |
| `reuse-local-memory` | — | — | Local memory reuse optimization |
| `set-local-array-alignment` | — | — | Set alignment for local arrays |
| [`sinking2`](./sinking2.md) | — | — | NVIDIA-specific instruction sinking (distinct from LLVM's Sink pass) |

## IR-Level Loop Pass

| Pass Name | Function | Size | Description |
|---|---|---|---|
| [`loop-index-split`](./loop-index-split.md) | `sub_2CC5900` / `sub_1C7B2C0` | 69KB | Split loops on index conditions. NVIDIA-preserved pass (removed from upstream LLVM) |

## Custom Analyses

| Analysis Name | Purpose |
|---|---|
| `rpa` | Register Pressure Analysis — feeds into scheduling and rematerialization decisions |
| `merge-sets` | Merge set computation — used by coalescing and allocation |

## Machine-Level Passes

| Pass Name | Function | Pass ID | Size | Description |
|---|---|---|---|---|
| Block Remat | `sub_2186D90` | `nvptx-remat-block` | 47KB | Two-phase candidate selection + iterative "pull-in" for register pressure reduction. `"Max-Live-Function("`, `"Really Final Pull-in:"` |
| Machine Mem2Reg | `sub_21F9920` | `nvptx-mem2reg` | — | Promotes `__local_depot` stack objects back to registers post-regalloc |
| MRPA | `sub_2E5A4E0` | `machine-rpa` | 48KB | Machine Register Pressure Analysis — incremental tracking, not in upstream LLVM |
| LDG Transform | `sub_21F2780` | `ldgxform` | — | Transforms global loads to `ldg.*` (texture cache) for read-only data |
| GenericToNVVM | `sub_215DC20` | `generic-to-nvvm` | 36KB | Moves globals from generic to global address space |
| Alloca Hoisting | `sub_21BC7D0` | `alloca-hoisting` | — | Ensures all allocas are in entry block (PTX requirement) |
| Image Optimizer | `sub_21BCF10` | — | — | Optimizes texture/surface access patterns |
| NVPTX Peephole | `sub_21DB090` | `nvptx-peephole` | — | NVPTX-specific peephole optimization |
| Prolog/Epilog | `sub_21DB5F0` | — | — | Custom frame management (PTX has no traditional prolog/epilog) |
| Replace Image Handles | `sub_21DBEA0` | — | — | Replaces IR-level image handles with PTX texture/surface references |
| Extra MI Printer | `sub_21E9E80` | `extra-machineinstr-printer` | — | Register pressure statistics reporting |
| Valid Global Names | `sub_21BCD80` | `nvptx-assign-valid-global-names` | — | Sanitizes global names to valid PTX identifiers |
| NVVMIntrRange | `sub_216F4B0` | `nvvm-intr-range` | — | Adds `!range` metadata to NVVM intrinsics (e.g., tid.x bounds) |

## Major Proprietary Subsystems

### [Dead Synchronization Elimination](./dead-sync-elimination.md) — `sub_2C84BA0`

| Field | Value |
|---|---|
| Size | 96KB |
| Purpose | Removes redundant `__syncthreads()` barriers |

Bidirectional fixed-point dataflow analysis across the CFG, tracking four memory access categories per BB through eight red-black tree maps. Each deletion triggers full restart. Distinct from lightweight [`basic-dbe`](./dead-barrier-elim.md). See [dedicated page](./dead-sync-elimination.md) for full algorithm.

### [MemorySpaceOpt](./memory-space-opt.md) — Multi-Function Cluster

| Function | Size | Purpose |
|---|---|---|
| `sub_1C70910` | — | Pass entry point |
| `sub_1C6A6C0` | — | Pass variant |
| `sub_1CA2920` | 32KB | Address space resolution — `"Cannot tell what pointer points to, assuming global memory space"` |
| `sub_1CA9E90` | 28KB | Secondary resolver |
| `sub_1CA5350` | 45KB | Infrastructure |
| `sub_2CBBE90` | 71KB | Memory-space-specialized function cloning |

### [NV Rematerialization Cluster](./rematerialization.md)

| Function | Size | Role |
|---|---|---|
| `sub_1CE7DD0` | 67KB | Main driver — live-in/live-out analysis, skip decisions |
| `sub_1CE67D0` | 32KB | Block-level executor — `"remat_"`, `"uclone_"` prefixes |
| `sub_1CE3AF0` | 56KB | Pull-in cost analysis — `"Total pull-in cost = %d"` |

### NLO — Simplify Live Output

| Function | Size | Strings |
|---|---|---|
| `sub_1CE10B0` | 48KB | `"Simplify Live Output"`, `"nloNewBit"`, `"newBit"` |
| `sub_1CDC1F0` | 35KB | `"nloNewAdd"`, `"nloNewBit"` |

Creates new add/bit operations to simplify live-out values at block boundaries.

### [IV Demotion](./iv-demotion.md) — `sub_1CD74B0`

| Field | Value |
|---|---|
| Size | 75KB |
| Strings | `"phiNode"`, `"demoteIV"`, `"newInit"`, `"newInc"`, `"argBaseIV"`, `"newBaseIV"`, `"iv_base_clone_"`, `"substIV"` |

Demotes induction variables (e.g., 64-bit to 32-bit), creates new base IVs, clones IV chains for register pressure reduction. Sub-pass of [rematerialization](./rematerialization.md). See [dedicated page](./iv-demotion.md) for full algorithm.

### RLMCAST — `sub_2D13E90`

| Field | Value |
|---|---|
| Size | 67KB |
| Purpose | Register-level multicast instruction lowering |

Broadcasts a value to multiple register destinations. Uses 216-byte and 160-byte node structures.

### Texture Group Merge (.Tgm) — `sub_2DDE8C0`

Groups texture load operations to hide latency. Uses `.Tgm` suffix in scheduling and function pointer table (3 predicates) for grouping decisions.

### [NVVM Intrinsic Verifier](./nvvm-verify-deep.md) — `sub_2C7B6A0`

| Field | Value |
|---|---|
| Size | 143KB |
| Purpose | Validates ALL NVVM intrinsics against SM capabilities |

Architecture-gated validation for every intrinsic call. Part of the [three-layer NVVM verifier](./nvvm-verify-deep.md) (230KB total).

### [NVVM Intrinsic Lowering](./nvvm-intrinsic-lowering.md) — `sub_2C63FB0`

| Field | Value |
|---|---|
| Size | 140KB |
| Purpose | Lowers NVVM intrinsics to concrete operations |

Pattern-matching rewrite engine for `llvm.nvvm.*` intrinsics. Two levels (basic + barrier-aware), runs up to 10 times. See [dedicated page](./nvvm-intrinsic-lowering.md) for full dispatch table.

### [Base Address Strength Reduction](./base-address-sr.md) — `sub_2CA4A10`

| Field | Value |
|---|---|
| Size | 58KB |
| Knobs | `do-base-address-strength-reduce` (two levels: 1 = no conditions, 2 = with conditions) |

Scans loop bodies for memory ops sharing a common base pointer, hoists the anchor computation, rewrites remaining addresses as `(anchor + relative_offset)`. See [dedicated page](./base-address-sr.md) for the anchor selection algorithm.

### [Common Base Elimination](./common-base-elim.md) — `sub_2CA8B00`

| Field | Value |
|---|---|
| Size | 39KB |
| Purpose | Hoists shared base address expressions to dominating CFG points |

Operates at inter-block level (vs [BASR](./base-address-sr.md) intra-loop). The two passes form a complementary pair for comprehensive GPU address computation reduction. See [dedicated page](./common-base-elim.md).

### [CSSA Transformation](./cssa.md) — `sub_3720740`

| Field | Value |
|---|---|
| Size | 22KB |
| Purpose | Conventional-SSA for GPU divergent control flow |
| Knobs | `do-cssa`, `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa` |
| Debug | `"IR Module before CSSA"` |

Rewrites PHI nodes to be safe under warp-divergent execution by inserting explicit copy instructions at reconvergence points. See [dedicated page](./cssa.md) for the divergence model.

## NVIDIA Codegen Knobs — `sub_1C20170`

70+ knobs parsed from the NVVM container format:

### Graphics Pipeline
`VSIsVREnabled`, `VSIsLastVTGStage`, `EnableZeroCoverageKill`, `AllowComputeDerivatives`, `AllowDerivatives`, `EnableNonUniformQuadDerivatives`, `UsePIXBAR`, `ManageAPICallDepth`

### Compute / Memory
`DisableSAMRAM`, `DoMMACoalescing`, `DisablePartialHalfVectorWrites`, `AssumeConvertMemoryToRegProfitable`, `MSTSForceOneCTAPerSMForSmemEmu`, `AddDepFromGlobalMembarToCB`

### Register Allocation / Scheduling
`AdvancedRemat`, `CSSACoalescing`, `DisablePredication`, `DisableXBlockSched`, `ReorderCSE`, `ScheduleKils`, `NumNopsAtStart`, `DisableERRBARAfterMEMBAR`

### Type Promotion
`PromoteHalf`, `PromoteFixed`, `FP16Mode`, `IgnoreRndFtzOnF32F16Conv`, `DisableLegalizeIntegers`

### PGO
`PGOProfileKind`, `PGOEpoch`, `PGOBatchSize`, `PGOCounterMemBaseVAIndex`

### Knob Forwarding
`OCGKnobs`, `OCGKnobsFile`, `NVVMKnobsString`, `OmegaKnobs`, `FinalizerKnobs`

## Compile Modes — `sub_1C21CE0`

| Mode | Constant |
|---|---|
| Whole-program no-ABI | `NVVM_COMPILE_MODE_WHOLE_PROGRAM_NOABI` |
| Whole-program ABI | `NVVM_COMPILE_MODE_WHOLE_PROGRAM_ABI` |
| Separate ABI | `NVVM_COMPILE_MODE_SEPARATE_ABI` |
| Extensible WP ABI | `NVVM_COMPILE_MODE_EXTENSIBLE_WHOLE_PROGRAM_ABI` |

| Opt Level | Constant |
|---|---|
| None | `NVVM_OPT_LEVEL_NONE` |
| 1 | `NVVM_OPT_LEVEL_1` |
| 2 | `NVVM_OPT_LEVEL_2` |
| 3 | `NVVM_OPT_LEVEL_3` |

| Debug Info | Constant |
|---|---|
| None | `NVVM_DEBUG_INFO_NONE` |
| Line info | `NVVM_DEBUG_INFO_LINE_INFO` |
| Full DWARF | `NVVM_DEBUG_INFO_DWARF` |
