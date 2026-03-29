# NVIDIA Custom Passes

25+ proprietary optimization passes not found in upstream LLVM. Registered into the New PM pipeline at `sub_2342890` and into the pipeline assembler at `sub_12E54A0`.

| | |
|---|---|
| **Module-level custom** | 16 passes |
| **Function-level custom** | 7 passes |
| **Loop-level custom** | 1 pass |
| **Custom analyses** | 2 analyses |
| **Machine-level custom** | 13 passes |
| **Registration** | `sub_2342890` (New PM) + `sub_12E54A0` (pipeline builder) |

## IR-Level Module Passes

| Pass Name | Class / Function | Size | Description |
|---|---|---|---|
| `memory-space-opt` | `sub_1C70910` / `sub_1CA2920` | cluster | Resolves generic pointers to specific address spaces (global/shared/local/const). Warns on illegal ops: atomics on constant mem, wmma on wrong space. Parameterized: `first-time`, `second-time`, `no-warnings`, `warnings` |
| `printf-lowering` | `sub_1CB1E60` | 31KB | Lowers `printf` → `vprintf` + local buffer. Validates format string is a literal. `"vprintfBuffer.local"`, `"bufIndexed"` |
| `nvvm-verify` | `sub_2C80C90` | 51KB | Module-level NVVM IR verifier. Validates triples, address spaces, atomic restrictions, pointer cast rules |
| `nvvm-pretreat` | `PretreatPass` | — | IR pre-treatment before optimization |
| `check-kernel-functions` | `NVPTXSetFunctionLinkagesPass` | — | Kernel function linkage validation |
| `check-gep-index` | — | — | GEP index validation |
| `cnp-launch-check` | `CNPLaunchCheckPass` | — | Cooperative launch validation |
| `ipmsp` | `IPMSPPass` | — | Inter-procedural memory space propagation |
| `nv-early-inliner` | — | — | NVIDIA early inlining pass |
| `nv-inline-must` | `InlineMustPass` | — | Force-inline functions marked `__forceinline__` |
| `select-kernels` | `SelectKernelsPass` | — | Kernel selection for compilation |
| `set-global-array-alignment` | — | — | Parameterized: `modify-shared-mem`, `skip-shared-mem`, `modify-global-mem`, `skip-global-mem` |
| `lower-aggr-copies` | — | — | Lower aggregate copies. Param: `lower-aggr-func-args` |
| `lower-struct-args` | — | — | Lower structure arguments. Param: `opt-byval` |
| `process-restrict` | — | — | Process `__restrict__` annotations. Param: `propagate-only` |
| `lower-ops` | `LowerOpsPass` | — | Lower special operations |

## IR-Level Function Passes

| Pass Name | Function | Size | Description |
|---|---|---|---|
| `branch-dist` | `sub_1C47810` cluster | — | Branch distribution optimization. Knobs: `branch-dist-block-limit`, `branch-dist-func-limit`, `branch-dist-norm` |
| `nvvm-reflect-pp` | — | — | NVVM reflect preprocessor |
| `nvvm-peephole-optimizer` | — | — | NVVM-specific peephole optimizations |
| `remat` | `sub_1CE7DD0` | 67KB | IR-level rematerialization. Analyzes live-in/live-out register pressure per BB. Strings: `"Skip rematerialization on "`, `"Reducing %d live-ins"` |
| `reuse-local-memory` | — | — | Local memory reuse optimization |
| `set-local-array-alignment` | — | — | Set alignment for local arrays |
| `sinking2` | — | — | NVIDIA-specific instruction sinking (distinct from LLVM's Sink pass) |

## IR-Level Loop Pass

| Pass Name | Function | Size | Description |
|---|---|---|---|
| `loop-index-split` | `sub_2CC5900` / `sub_1C7B2C0` | 69KB | Split loops on index conditions. NVIDIA-preserved pass (removed from upstream LLVM) |

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

### Dead Synchronization Elimination — `sub_2C84BA0`

| Field | Value |
|---|---|
| Size | 96KB |
| Purpose | Removes redundant `__syncthreads()` barriers |

Analyzes memory access patterns between barriers to determine which can be safely removed without affecting program correctness.

### MemorySpaceOpt — Multi-Function Cluster

| Function | Size | Purpose |
|---|---|---|
| `sub_1C70910` | — | Pass entry point |
| `sub_1C6A6C0` | — | Pass variant |
| `sub_1CA2920` | 32KB | Address space resolution — `"Cannot tell what pointer points to, assuming global memory space"` |
| `sub_1CA9E90` | 28KB | Secondary resolver |
| `sub_1CA5350` | 45KB | Infrastructure |
| `sub_2CBBE90` | 71KB | Memory-space-specialized function cloning |

### NV Rematerialization Cluster

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

### IV Demotion — `sub_1CD74B0`

| Field | Value |
|---|---|
| Size | 75KB |
| Strings | `"phiNode"`, `"demoteIV"`, `"newInit"`, `"newInc"`, `"argBaseIV"`, `"newBaseIV"`, `"iv_base_clone_"`, `"substIV"` |

Demotes induction variables (e.g., 64-bit → 32-bit), creates new base IVs, clones IV chains for register pressure reduction.

### RLMCAST — `sub_2D13E90`

| Field | Value |
|---|---|
| Size | 67KB |
| Purpose | Register-level multicast instruction lowering |

Broadcasts a value to multiple register destinations. Uses 216-byte and 160-byte node structures.

### Texture Group Merge (.Tgm) — `sub_2DDE8C0`

Groups texture load operations to hide latency. Uses `.Tgm` suffix in scheduling and function pointer table (3 predicates) for grouping decisions.

### NVVM Intrinsic Verifier — `sub_2C7B6A0`

| Field | Value |
|---|---|
| Size | 143KB |
| Purpose | Validates ALL NVVM intrinsics against SM capabilities |

Architecture-gated validation for every intrinsic call.

### NVVM Intrinsic Lowering — `sub_2C63FB0`

| Field | Value |
|---|---|
| Size | 140KB |
| Purpose | Lowers NVVM intrinsics to concrete operations |

Handles tex, surf, syncwarp, ISBE, and other NVVM-specific operations.

### Base Address Strength Reduction — `sub_2CA4A10`

| Field | Value |
|---|---|
| Size | 62KB |
| Knobs | `do-base-address-strength-reduce` |

GPU-specific address strength reduction for memory access patterns.

### Common Base Elimination — `sub_2CA8B00`

| Field | Value |
|---|---|
| Size | 39KB |
| Purpose | Hoists common base expressions to reduce redundant computation |

### CSSA Transformation — `sub_3720740`

| Field | Value |
|---|---|
| Purpose | Conventional-SSA for GPU divergent control flow |
| Knobs | `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa` |
| Debug | `"IR Module before CSSA"` |

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
