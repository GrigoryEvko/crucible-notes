# NVIDIA Custom Passes

25+ proprietary optimization passes not found in upstream LLVM. Registered into the New PM pipeline at `sub_2342890` and into the pipeline assembler at `sub_12E54A0`.

| | |
|---|---|
| **Module-level custom** | 16 passes |
| **Function-level custom** | 9 passes |
| **Loop-level custom** | 1 pass |
| **Custom analyses** | 2 analyses |
| **Machine-level custom** | 13 passes |
| **Registration** | `sub_2342890` (New PM) + `sub_12E54A0` (pipeline builder) |
| **Dedicated deep-dive pages** | 22 |

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
| [`ipmsp`](./ipmsp.md) | `IPMSPPass` | — | Inter-procedural memory space propagation |
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
