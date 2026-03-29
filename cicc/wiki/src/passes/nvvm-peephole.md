# nvvm-peephole-optimizer

The NVVM Peephole Optimizer is an NVIDIA-proprietary function-level IR pass that performs NVVM-specific pattern matching and instruction simplification. It is distinct from both LLVM's standard InstCombine pass (which handles general-purpose peephole optimization across ~600 functions in the `0x1700000`--`0x17B0000` range) and the machine-level `nvptx-peephole` pass (`sub_21DB090`) that operates on MachineInstrs after instruction selection.

| | |
|---|---|
| **Pass name** | `nvvm-peephole-optimizer` |
| **Class** | `llvm::NVVMPeepholeOptimizerPass` |
| **Scope** | Function pass (IR level) |
| **Registration** | New PM slot 382 in `sub_2342890` |
| **Enable knob** | `enable-nvvm-peephole` (bool, default = `true`) |
| **Pipeline position** | Function-level, runs alongside `branch-dist`, `nvvm-reflect-pp`, `remat`, `sinking2` |

## Purpose

CUDA programs produce IR patterns that standard LLVM optimizations do not recognize or cannot legally transform. The NVVM peephole pass fills this gap by matching NVVM-specific idioms -- address space casts, intrinsic call sequences, convergent operation patterns, and GPU-specific type conversions -- and rewriting them into simpler, cheaper forms. It operates at the LLVM IR level before code generation, complementing the machine-level `nvptx-peephole` pass that runs later in the pipeline.

## Position in the Pipeline

The pass is registered as a function-level pass in the New Pass Manager at registration line 2212+ in `sub_2342890`. It sits in the mid-optimization phase alongside other NVIDIA function passes:

```
... -> nvvm-reflect-pp -> nvvm-peephole-optimizer -> remat -> ...
```

The knob `enable-nvvm-peephole` (registered at `ctor_358_0`, address `0x50E8D0`) provides a global on/off switch. It defaults to enabled, suggesting the pass is considered safe and beneficial across all SM targets.

## Relationship to Other Peephole Passes

CICC contains three distinct peephole optimization layers:

| Layer | Pass | Level | Scope |
|---|---|---|---|
| LLVM InstCombine | `instcombine` | IR | General-purpose, ~600 functions at `0x1700000`+ |
| NVVM Peephole | `nvvm-peephole-optimizer` | IR | NVVM-specific patterns only |
| NVPTX Peephole | `nvptx-peephole` (MachineFunction) | Machine | Post-ISel, `sub_21DB090` |

The NVVM peephole pass handles transformations that require knowledge of NVVM's address space model, intrinsic semantics, or GPU-specific type system -- patterns that InstCombine cannot match because they depend on NVPTX target information not available to target-independent passes.

## Knob Registration

The enable knob is registered in two constructor functions:

| Constructor | Address | Context |
|---|---|---|
| `ctor_358_0` | `0x50E8D0` | NVPTX pass enable/disable switches (43 strings) |
| Sweep confirmation | `0x560000`--`0x5CFFFF` | Alias registration alongside `nvptx-exit-on-unreachable`, `no-reg-target-nvptxremat` |

The knob description string recovered from the binary is: `"Enable NVVM Peephole Optimizer"`. Its default-on status indicates the pass is mature and does not require opt-in behavior from users.

## Interaction with nvvm-reflect-pp

The pass named `nvvm-reflect-pp` (`llvm::SimplifyConstantConditionalsPass`) runs immediately before the NVVM peephole optimizer in the pipeline. `nvvm-reflect-pp` resolves `__nvvm_reflect()` calls and simplifies the resulting constant conditionals, which exposes dead code and simplified control flow. The peephole pass then operates on this cleaned-up IR, where many previously-opaque intrinsic call patterns have been reduced to simpler forms amenable to pattern matching.

## Expected Transformation Categories

Based on the pass's position in the pipeline (after `nvvm-reflect-pp` but before `remat`) and the patterns visible in NVVM IR, the peephole optimizer likely targets several categories of NVVM-specific patterns:

**Address space cast simplification**. After `memory-space-opt` and `ipmsp` resolve generic pointers, redundant `addrspacecast` chains remain in the IR. For example, `addrspacecast(addrspacecast(ptr, generic), shared)` can be simplified to a single cast or eliminated entirely when the source and destination spaces match.

**Intrinsic call folding**. Certain NVVM intrinsic sequences produce constant results or simplify to cheaper operations. For example, `llvm.nvvm.read.ptx.sreg.tid.x` followed by a comparison against the block dimension can sometimes be folded when the dimension is known at compile time via `__launch_bounds__`.

**Convergent operation canonicalization**. CUDA's convergent operations (`__syncwarp`, `__ballot_sync`, etc.) have specific semantic constraints that standard InstCombine cannot reason about. The peephole pass can simplify convergent call sequences that the general-purpose optimizer must treat as opaque.

**Type conversion cleanup**. NVVM uses GPU-specific type representations (e.g., `bf16`, `tf32`) that produce conversion chains not present in standard LLVM IR. The peephole pass can fold redundant conversion sequences that arise from type promotion and demotion patterns.

## Neighboring Passes in Registration Order

The function-level NVIDIA passes are registered in this order:

| Slot | Pass | Class |
|---|---|---|
| 376 | `basic-dbe` | `BasicDeadBarrierEliminationPass` |
| 377 | `branch-dist` | `BranchDistPass` |
| 378 | `byval-mem2reg` | `ByValMem2RegPass` |
| 379 | `bypass-slow-division` | `BypassSlowDivisionPass` |
| 380 | `normalize-gep` | `NormalizeGepPass` |
| 381 | `nvvm-reflect-pp` | `SimplifyConstantConditionalsPass` |
| **382** | **`nvvm-peephole-optimizer`** | **`NVVMPeepholeOptimizerPass`** |
| 383 | `old-load-store-vectorizer` | `OldLoadStoreVectorizerPass` |
| 384 | `print<merge-sets>` | `MergeSetsAnalysisPrinterPass` |
| 385 | `remat` | `RematerializationPass` |

## Evidence Summary

The pass's existence and classification are confirmed through multiple independent sources:

- **Pipeline parser** (`sub_233C410`): line 3534 registers `"nvvm-peephole-optimizer"` as a function-level NVIDIA custom pass
- **New PM registration** (`sub_2342890`): slot 382 maps the string to `llvm::NVVMPeepholeOptimizerPass`
- **Knob survey** (sweep `0x4F0000`--`0x51FFFF`): `enable-nvvm-peephole` is a boolean knob with description `"Enable NVVM Peephole Optimizer"` and default `true`
- **Knob duplicate** (sweep `0x560000`--`0x5CFFFF`): confirmed at line 292 with identical description

The pass implementation function has not yet been deeply analyzed at the decompilation level; the above evidence comes from registration infrastructure, knob discovery, and pipeline ordering analysis.
