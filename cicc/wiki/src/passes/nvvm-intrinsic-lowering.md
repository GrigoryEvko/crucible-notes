# NVVM Intrinsic Lowering

The NVVMIntrinsicLowering pass is a pattern-matching rewrite engine that transforms NVVM intrinsic calls into equivalent sequences of standard LLVM IR operations. NVVM IR uses hundreds of target-specific intrinsics (`llvm.nvvm.*`) for GPU-specific operations -- texture/surface access, warp shuffles, type conversions, wide vector manipulations, barrier synchronization, and tensor core primitives. These intrinsics encode NVIDIA-specific semantics that have no direct LLVM IR equivalent. This pass bridges the gap: it matches each intrinsic call against a database of lowering rules and, when a match is found, replaces the call with a combination of standard LLVM instructions (shufflevector, extractelement, insertelement, bitcast, arithmetic) that express the same semantics in a form amenable to standard LLVM optimization passes.

The pass runs repeatedly throughout the pipeline -- up to 10 times in the "mid" compilation path -- because other optimization passes (NVVMReflect, InstCombine, inlining) can expose new intrinsic calls or simplify existing ones into forms that become lowerable. Two distinct invocation levels exist: level 0 for basic intrinsic lowering, and level 1 for barrier-related intrinsic lowering that must happen after barrier analysis infrastructure is in place.

| | |
|---|---|
| **Pass factory** | `sub_1CB4E40` (creates pass instance with level parameter) |
| **Core engine** | `sub_2C63FB0` (12 KB native; 2,460 lines decomp) |
| **Pass type** | FunctionPass (Legacy PM) |
| **Registration** | Legacy PM only (not separately registered in New PM); invoked from pipeline assembler |
| **Runtime positions** | Tier 1/2/3 #1, #3, #28, #50, #64 (level 1); "mid" path has 4 level-0 invocations (see [Pipeline](../llvm/pipeline.md)) |
| **NVVMPassOptions slot** | 99 (offset 2000, `BOOL_COMPACT`, default = 0 = enabled) |
| **Disable flag** | `opts[2000] = 1` disables all invocations |
| **Level parameter** | 0 = basic lowering, 1 = barrier-aware lowering |
| **Iteration limit** | 30 (global `qword_5010AC8`) |
| **Upstream equivalent** | None -- entirely NVIDIA-proprietary |
| **Address range** | `0x2C4D000`--`0x2C66000` (lowering engine cluster) |

## Algorithm

### Entry and Dispatch

The pass factory `sub_1CB4E40` takes a single integer parameter -- the lowering level. Level 0 performs basic intrinsic lowering (type conversions, vector decomposition, shuffle lowering). Level 1 adds barrier-related intrinsic lowering that depends on barrier analysis having already run. The factory allocates a pass object and stores the level in it; the pass entry point reads this level to filter which intrinsics are candidates for lowering.

At the core engine (`sub_2C63FB0`), the entry check validates that the instruction is an intrinsic call: the byte at `call->arg_chain->offset_8` must equal 17 (intrinsic call marker), and `call->offset_16` must be non-null (the callee exists). If either check fails, the function returns 0 (no lowering performed).

### Pattern-Matching Rewrite Loop

The algorithm operates as a worklist-driven rewrite system:

```
function lowerIntrinsic(ctx, call, level):
    if not isIntrinsicCall(call): return 0
    if not hasCallee(call): return 0

    operands = collectOperands(call)        // v285/v286 arrays
    worklist_direct = []                     // v288: direct operand replacements
    worklist_typed  = []                     // v294: type-changed operands
    worklist_shuf   = []                     // v300: shuffle/reorganized operands

    iterations = 0
    while iterations < qword_5010AC8:       // default 30
        iterations++

        // Phase 1: build candidate lowerings
        candidates = buildCandidates(operands)   // sub_2C4D470
        for each candidate in candidates:
            pattern = extractPattern(candidate)  // sub_2C4D5A0

            // Phase 2: type compatibility check
            if not checkTypeCompat(pattern, operands):  // sub_AD7630
                continue

            // Phase 3: operand matching
            if not matchOperands(pattern, operands):
                continue

            // Phase 4: additional pattern checks
            if not additionalChecks(pattern):    // sub_2C50020
                continue

            // Phase 5: core lowering -- create replacement
            replacement = buildReplacement(      // sub_2C515C0
                ctx, operands,
                worklist_direct, worklist_typed, worklist_shuf)

            // Phase 6: substitute
            replaceAllUses(call, replacement)    // sub_BD84D0
            transferMetadata(call, replacement)  // sub_BD6B90
            queueForDeletion(call)               // sub_F15FC0
            return 1

    return 0  // no lowering found within iteration limit
```

The iteration limit of 30 (stored in `qword_5010AC8`) exists because lowering one intrinsic can produce new intrinsic calls that themselves need lowering. For example, lowering a wide vector intrinsic into narrower operations may produce calls to narrower intrinsics. Without the limit, pathological patterns could cause infinite expansion. In practice, most intrinsics lower in a single iteration; the limit is a safety net.

### Three Worklist Structures

The rewrite engine maintains three parallel worklist structures that categorize how operands are transformed:

| Worklist | Variable | Purpose |
|---|---|---|
| Direct | `v288` | Operands that pass through unchanged -- same value, same type |
| Type-changed | `v294` | Operands that need a type conversion (e.g., NVVM-specific type to standard LLVM type) |
| Shuffle/reorganized | `v300` | Operands that need positional rearrangement (vector lane reordering, element extraction) |

When `sub_2C515C0` builds the replacement instruction, it reads all three worklists to assemble the final operand list: direct operands are copied verbatim, type-changed operands go through a bitcast or type conversion, and shuffle operands are processed through a shufflevector or extractelement/insertelement sequence.

## Lowering Categories

### Vector Operation Decomposition

Wide vector NVVM intrinsics (operating on `v4f32`, `v2f64`, `v4i32`, etc.) are decomposed into sequences of narrower operations. The NVVM IR frontend emits vector intrinsics to express data-parallel GPU operations, but the NVPTX backend's instruction selector handles scalar or narrow-vector operations more efficiently.

The decomposition pattern:

```
// Before: single wide-vector intrinsic call
%result = call <4 x float> @llvm.nvvm.wide.op(<4 x float> %a, <4 x float> %b)

// After: four scalar operations + vector reconstruction
%a0 = extractelement <4 x float> %a, i32 0
%a1 = extractelement <4 x float> %a, i32 1
%a2 = extractelement <4 x float> %a, i32 2
%a3 = extractelement <4 x float> %a, i32 3
%b0 = extractelement <4 x float> %b, i32 0
...
%r0 = call float @llvm.nvvm.narrow.op(float %a0, float %b0)
%r1 = call float @llvm.nvvm.narrow.op(float %a1, float %b1)
...
%v0 = insertelement <4 x float> undef, float %r0, i32 0
%v1 = insertelement <4 x float> %v0,   float %r1, i32 1
...
```

This decomposition enables scalar optimizations (constant folding, CSE) to work on individual lanes, and the narrower intrinsics may themselves lower in subsequent iterations -- hence the iteration limit.

### Shuffle Vector Lowering

When an NVVM intrinsic performs pure data reorganization -- lane permutation, broadcast, or subvector extraction -- without any arithmetic, the pass replaces it with an LLVM `shufflevector` instruction. The core lowering for this path goes through `sub_DFBC30`, which takes:

```
sub_DFBC30(context, operation=6, type_info, shuffle_indices, count, flags)
```

The `operation=6` constant identifies this as a shufflevector creation. The `shuffle_indices` array encodes the lane mapping: for a warp shuffle that broadcasts lane 0 to all lanes, the mask would be `<0, 0, 0, 0, ...>`. For a rotation, it might be `<1, 2, 3, 0>`.

Shuffle lowering handles several NVVM intrinsic families:

- Warp-level shuffle operations (`__shfl_sync`, `__shfl_up_sync`, etc.) when the shuffle amount is a compile-time constant
- Subvector extraction from wider types (e.g., extracting the low `v2f16` from a `v4f16`)
- Lane broadcast patterns used in matrix fragment loading

### Type Conversion Lowering

NVVM defines intrinsic-based type conversions for types that LLVM's standard type system does not directly support, such as:

- BF16 (bfloat16) to/from FP32 -- intrinsic ID 0x2106, gated by sm_72+
- TF32 (tensorfloat32) conversions -- intrinsic ID 0x2106 with conversion type 3+, gated by sm_75+
- FP8 (E4M3/E5M2) conversions -- intrinsic ID 0x2107, gated by sm_89+ (Ada)
- Extended type conversions with saturate, rounding mode control

The lowering replaces these intrinsic calls with sequences of:
- `bitcast` for reinterpretation between same-width types
- `fptrunc` / `fpext` for standard floating-point width changes
- `trunc` / `zext` / `sext` for integer width changes
- Arithmetic sequences for rounding mode emulation when the hardware rounding mode is not directly expressible

The `sub_2C52B30` helper ("get canonical type") resolves NVVM-specific type encodings to their standard LLVM `Type*` equivalents during this process.

## Multi-Run Pattern

NVVMIntrinsicLowering appears more times in the compilation pipeline than any other NVIDIA custom pass. In the "mid" path (standard CUDA compilation), it runs approximately 10 times across the main path and the Tier 1/2/3 sub-pipelines. The pattern reveals a deliberate interleaving strategy.

### "mid" Path Invocations (Level 0)

All four invocations in the main "mid" path use level 0 and are guarded by `!opts[2000]`:

| Position | Context | Preceding Pass | Following Pass | Purpose |
|---|---|---|---|---|
| 1st | Early pipeline | ConstantMerge | MemCpyOpt | Lower intrinsics from the original IR before SROA/GVN operate |
| 2nd | After InstCombine + standard pipeline #5 | LLVM standard #5 | DeadArgElim | Re-lower intrinsics that InstCombine may have simplified or inlining may have exposed |
| 3rd | After NVVMReflect + standard pipeline #8 | LLVM standard #8 | IPConstProp | Lower intrinsics whose arguments became constant after NVVMReflect resolved `__nvvm_reflect()` calls |
| 4th | Late pipeline | LICM | NVVMBranchDist | Final cleanup of any remaining lowerable intrinsics before register-pressure-sensitive passes |

### Tier 1/2/3 Invocations (Level 1)

Within the `sub_12DE8F0` tier sub-pipeline, the pass runs with level 1 at five distinct points:

| Position | Context | Notes |
|---|---|---|
| 1st | Tier entry | Immediately at tier start -- lower barrier-related intrinsics before barrier analysis |
| 2nd | After 1st NVVMIRVerification | Re-lower after verification may have canonicalized IR |
| 3rd | After CVP + NVVMVerifier + NVVMIRVerification | Post-optimization cleanup of barrier intrinsics |
| 4th | After LoopUnswitch + standard pipeline #1 | Re-lower intrinsics exposed by loop transformations |
| 5th | After DSE + DCE + standard pipeline #1 | Final tier cleanup before MemorySpaceOpt |

Each tier (1, 2, and 3) runs this same sequence independently, so in a full compilation with all three tiers active, level-1 lowering executes up to 15 times total.

## Level Parameter Semantics

The level parameter partitions the intrinsic lowering rules into two sets:

**Level 0 -- Basic lowering.** Handles intrinsics whose lowering depends only on the intrinsic's operands and types. This includes vector decomposition, shuffle lowering, and standard type conversions. These are safe to run at any point in the pipeline because they have no dependencies on analysis results. The "mid" path runs level 0 exclusively.

**Level 1 -- Barrier-aware lowering.** Handles intrinsics related to synchronization barriers (`__syncthreads`, `__syncwarp`, barrier-guarded memory operations) whose lowering must coordinate with the barrier analysis infrastructure. In the tier sub-pipeline, level 1 runs at the entry point before `NVVMBarrierAnalysis` and `NVVMLowerBarriers`, and again after those passes have run. This two-phase pattern within the tier ensures that:

1. Barrier intrinsics are lowered to a canonical form that the barrier analysis can recognize
2. After barrier analysis and lowering, any residual barrier-related intrinsics are cleaned up

The reason level 1 is restricted to tiers rather than the main "mid" path: the tier sub-pipeline (`sub_12DE8F0`) sets up the barrier analysis state (via `sub_18E4A00` / `sub_1C98160`) that level 1 lowering depends on. Running level 1 in the main path before this state exists would produce incorrect results.

## Interaction with NVVMReflect

NVVMReflect resolves compile-time queries about the target GPU architecture:

```llvm
%arch = call i32 @llvm.nvvm.reflect(metadata !"__CUDA_ARCH__")
; After NVVMReflect: %arch = i32 900  (for sm_90)
```

This resolution has a cascading effect on intrinsic lowering. Many NVVM intrinsics are conditionally emitted by the frontend behind architecture checks:

```c
if (__CUDA_ARCH__ >= 900) {
    // Hopper-specific intrinsic
    __nvvm_tma_load_async(...);
} else {
    // Fallback path using standard loads
}
```

After NVVMReflect replaces the architecture query with a constant, and `nvvm-reflect-pp` (SimplifyConstantConditionalsPass) eliminates the dead branch, the surviving path may contain intrinsics that were previously unreachable. The pipeline runs NVVMIntrinsicLowering *after* NVVMReflect specifically to catch these newly-exposed intrinsics. This is why the 3rd invocation in the "mid" path immediately follows NVVMReflect + LLVM standard pipeline #8.

## Configuration

### NVVMPassOptions

| Slot | Offset | Type | Default | Semantics |
|---|---|---|---|---|
| 98 | 1976 | STRING | (empty) | Paired string parameter for the pass (unused or reserved) |
| 99 | 2000 | BOOL_COMPACT | 0 | Disable flag: 0 = enabled, 1 = disabled |

Setting slot 99 to 1 disables all invocations of NVVMIntrinsicLowering across the entire pipeline -- both level 0 and level 1. There is no mechanism to disable one level independently.

### Global Variables

| Variable | Default | Purpose |
|---|---|---|
| `qword_5010AC8` | 30 | Maximum iterations per invocation of the rewrite loop |

This global is not exposed as a user-facing knob. It is initialized at program startup and is constant for the lifetime of the process.

## Key Helper Functions

### Pattern Matching

| Function | Role |
|---|---|
| `sub_2C4D470` | Build candidate lowering list from intrinsic operands |
| `sub_2C4D5A0` | Extract pattern from candidate -- returns the lowering rule |
| `sub_2C50020` | Additional pattern compatibility checks beyond type matching |
| `sub_2C52B30` | Get canonical LLVM type for an NVVM-specific type encoding |
| `sub_AD7630` | Type-lowering query -- checks if source type can lower to target type |

### Instruction Construction

| Function | Role |
|---|---|
| `sub_2C515C0` | Build replacement instruction from three worklist structures |
| `sub_2C4FB60` | Opcode dispatch -- selects the LLVM opcode for the lowered operation |
| `sub_DFBC30` | Create shufflevector or similar vector IR construct (operation=6) |

### IR Mutation

| Function | Role |
|---|---|
| `sub_BD84D0` | Replace all uses of old instruction with new value |
| `sub_BD6B90` | Transfer metadata from old instruction to replacement |
| `sub_F15FC0` | Queue old instruction for deletion |

### Pass Infrastructure

| Function | Address | Role |
|---|---|---|
| `sub_1CB4E40` | `0x1CB4E40` | Pass factory -- creates pass with level parameter |
| `sub_2C63FB0` | `0x2C63FB0` | Core lowering engine (12 KB native; 2,460 lines decomp) |

## Diagnostic Strings

The core engine at `sub_2C63FB0` contains no user-visible diagnostic strings. This is unusual for a 12 KB native function and reflects the fact that intrinsic lowering is a mechanical pattern-matching operation: either a lowering rule matches (silently applied) or it does not (silently skipped). Failures are not reported because an unlowered intrinsic is not necessarily an error -- it may be handled by a later pass (NVVMLowerBarriers, GenericToNVVM) or by the NVPTX instruction selector directly.

The pass factory `sub_1CB4E40` similarly contains no diagnostic strings.

## Pipeline Position Summary

```
sub_12E54A0 (Master Pipeline Assembly)
  │
  ├─ "mid" path (level 0, 4 invocations):
  │    ├─ #1: After ConstantMerge, before MemCpyOpt/SROA
  │    ├─ #2: After InstCombine + LLVM standard #5
  │    ├─ #3: After NVVMReflect + LLVM standard #8
  │    └─ #4: After LICM, before NVVMBranchDist/Remat
  │
  ├─ "ptx" path (level 0, 0 invocations):
  │    └─ (not present -- PTX input already has intrinsics lowered)
  │
  ├─ default path (level 0, 1 invocation):
  │    └─ #1: After NVVMReflect, before NVVMPeephole
  │
  └─ Tier 1/2/3 sub-pipeline (level 1, 5 invocations per tier):
       ├─ #1: Tier entry
       ├─ #2: After NVVMIRVerification
       ├─ #3: After CVP + NVVMVerifier
       ├─ #4: After LoopUnswitch + LLVM standard #1
       └─ #5: After DSE + DCE + LLVM standard #1
```

## Cross-References

- [LLVM Optimizer](../pipeline/optimizer.md) -- master pipeline assembly and tier system
- [NVIDIA Custom Passes -- Inventory](./index.md) -- pass registry and classification
- [Rematerialization](./rematerialization.md) -- runs after intrinsic lowering in "mid" path
- [NVVM Peephole](./nvvm-peephole.md) -- peephole patterns that may expose new lowerable intrinsics
- [MemorySpaceOpt](./memory-space-opt.md) -- runs after level-1 lowering in tier sub-pipeline
