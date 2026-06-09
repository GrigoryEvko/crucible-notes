# nvvm-peephole-optimizer

The NVVM Peephole Optimizer is an NVIDIA-proprietary function-level IR pass that performs NVVM-specific pattern matching and instruction simplification. It is distinct from both LLVM's standard InstCombine pass (which handles general-purpose peephole optimization across ~600 functions in the `0x1700000`--`0x17B0000` range) and the machine-level `nvptx-peephole` pass (`sub_21DB090`) that operates on MachineInstrs after instruction selection.

This page documents all three peephole layers in cicc, their pipeline positions, their transformations, and the satellite machine-level peephole passes that complement them.

| | |
|---|---|
| **Pass name** | `nvvm-peephole-optimizer` |
| **Class** | `llvm::NVVMPeepholeOptimizerPass` |
| **Scope** | Function pass (IR level) |
| **Registration** | New PM slot 382 in `sub_2342890` |
| **Serializer** | `sub_2314DA0` |
| **Factory (legacy PM)** | `sub_1CEF8F0` |
| **Pipeline parser line** | 3534 in `sub_233C410` |
| **Enable knob** | `enable-nvvm-peephole` (bool, default = `true`) |
| **Knob address** | `ctor_358_0` @ `0x50E8D0` |
| **NVVMPassOptions slot** | `nvvm-peephole-optimizer` in 4512-byte options struct |
| **Pipeline position** | Function-level, runs after NVVMReflect + NVVMIntrinsicLowering |

## Purpose

CUDA programs produce IR patterns that standard LLVM optimizations do not recognize or cannot legally transform. The NVVM peephole pass fills this gap by matching NVVM-specific idioms — address space casts, intrinsic call sequences, convergent operation patterns, and GPU-specific type conversions — and rewriting them into simpler, cheaper forms. It operates at the LLVM IR level before code generation, complementing the machine-level `nvptx-peephole` pass that runs later in the pipeline.

The pass is always paired with `sub_215D9D0` (NVVMAnnotationsProcessor), which runs immediately after the peephole in every pipeline path. This companion pass processes NVVM annotations (e.g., `tcgen05` tensor annotation metadata) on the IR that the peephole has just simplified.

## Three Peephole Layers

CICC contains three distinct peephole optimization layers, each operating at a different abstraction level and targeting different pattern classes.

| Layer | Pass | Level | Address / Slot | Targets |
|---|---|---|---|---|
| LLVM InstCombine | `instcombine` | IR | `0x1700000`+ (~600 funcs) | General-purpose: algebraic simplification, constant folding, dead instruction removal |
| NVVM Peephole | `nvvm-peephole-optimizer` | IR | slot 382, factory `sub_1CEF8F0` | NVVM-specific: address space casts, intrinsic sequences, GPU type conversions |
| NVPTX Peephole | `nvptx-peephole` | MachineInstr | `sub_21DB090` | PTX-specific: redundant `cvta` folding, predicate optimization, move elimination |

The NVVM peephole pass handles transformations that require knowledge of NVVM's address space model, intrinsic semantics, or GPU-specific type system — patterns that InstCombine cannot match because they depend on NVPTX target information not available to target-independent passes. The machine-level NVPTX peephole then handles patterns that only emerge after instruction selection has lowered IR to MachineInstrs.

## Pipeline Positions

### IR-Level: nvvm-peephole-optimizer

The IR-level peephole (`sub_1CEF8F0`) is invoked from the legacy pipeline assembler (`sub_12E54A0`) in all three language-specific code paths. Its companion `sub_215D9D0` always follows immediately.

**Path A — "ptx" language** (lines 580–638 in `sub_12E54A0`):

```text
sub_1CEF8F0()    NVVMPeephole
sub_215D9D0()    NVVMAnnotationsProcessor
sub_1857160()    NVVMReflect (conditional)
sub_1A62BF0(1)   LLVM standard pipeline #1
sub_1B26330()    MemCpyOpt
sub_18DEFF0()    DCE
...
```

**Path B — "mid" language** (Ofcmid, lines 814–1075):

```text
sub_184CD60()    ConstantMerge / GlobalDCE
sub_1CB4E40(0)   NVVMIntrinsicLowering
sub_1B26330()    MemCpyOpt
sub_198E2A0()    SROA / CorrelatedValuePropagation
sub_1CEF8F0()    NVVMPeephole                   <<<
sub_215D9D0()    NVVMAnnotationsProcessor
sub_17060B0(1,0) PrintModulePass
sub_198DF00(-1)  JumpThreading / CVP
sub_1C6E800()    GVN / LICM
...
```

**Path C — default/general** (O2/O3, lines 1077–1371):

```text
sub_1A62BF0(4)   LLVM standard pipeline #4
sub_1857160()    NVVMReflect
sub_1CB4E40(0)   NVVMIntrinsicLowering
sub_1857160()    NVVMReflect (second pass)
sub_1CEF8F0()    NVVMPeephole                   <<<
sub_215D9D0()    NVVMAnnotationsProcessor
sub_1A7A9F0()    InstructionSimplify
sub_1A62BF0(5)   LLVM standard pipeline #5
...
```

**Late position** (O3 tier finalization):

```text
sub_1B7FDF0(n)   BranchFolding / CFGSimplify
sub_1CEF8F0()    NVVMPeephole                   <<<
sub_215D9D0()    NVVMAnnotationsProcessor
sub_18B3080(f)   Sinking2Pass (fast mode)
sub_1CC60B0()    NVVMSinking
sub_18A3430()    AggressiveInstCombine
...
```

In every path, the peephole runs **after** `NVVMIntrinsicLowering` (`sub_1CB4E40`) and `NVVMReflect` (`sub_1857160`) have resolved intrinsics and reflect calls. This ensures the peephole sees simplified IR where previously-opaque intrinsic call patterns have been reduced to simpler forms amenable to pattern matching.

### Machine-Level: nvptx-peephole

The machine-level peephole (`sub_21DB090`) runs in `addPreRegAlloc()` (`sub_2166ED0`):

```text
EarlyTailDuplicate
codegen DCE
Machine LICM + CSE + Sinking        (conditional on enable-mlicm, enable-mcse)
PeepholeOptimizerPass                (stock LLVM, slot 492, disable-peephole)
NVPTXPeephole (sub_21DB090)         <<<
DeadMachineInstrElim
MachineCopyPropagation
```

The string `"After codegen peephole optimization pass"` in `sub_2166ED0` marks the checkpoint after both the stock LLVM peephole and the NVPTX peephole have completed.

### New PM Registration

The pass is registered as a function-level pass in the New Pass Manager at registration line 2242 in `sub_2342890`. It sits in the mid-optimization phase alongside other NVIDIA function passes:

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

## IR-Level Transformation Categories

Based on pipeline position (after NVVMReflect + NVVMIntrinsicLowering, before sinking and rematerialization) and the patterns visible in NVVM IR, the peephole optimizer targets several categories.

### Address Space Cast Simplification

After `memory-space-opt` and `ipmsp` resolve generic pointers to specific address spaces, redundant `addrspacecast` chains remain in the IR. The peephole rewrites these:

```llvm
; Before:
%p1 = addrspacecast ptr addrspace(3) %src to ptr        ; shared -> generic
%p2 = addrspacecast ptr %p1 to ptr addrspace(3)         ; generic -> shared
store i32 %val, ptr addrspace(3) %p2

; After:
store i32 %val, ptr addrspace(3) %src                   ; chain eliminated
```

```llvm
; Before:
%p = addrspacecast ptr addrspace(1) %src to ptr addrspace(1)  ; identity cast

; After:
; (use %src directly — identity addrspacecast removed)
```

The validation function `sub_21BEE70` (`"Bad address space in addrspacecast"`, 4.1KB) ensures the peephole does not create illegal address space transitions. NVPTX address spaces are:

| AS | Name | Legal cast targets |
|---|---|---|
| 0 | Generic | All |
| 1 | Global | Generic |
| 3 | Shared | Generic |
| 4 | Constant | Generic |
| 5 | Local | Generic |

### Intrinsic Call Folding

After NVVMIntrinsicLowering has expanded NVVM intrinsics, some expansion sequences can be further simplified:

```llvm
; Before (after intrinsic lowering, launch_bounds known):
%tid = call i32 @llvm.nvvm.read.ptx.sreg.tid.x()
%cmp = icmp ult i32 %tid, 256       ; blockDim.x known = 256

; After (when nvvm-intr-range has set !range {0, 256}):
%cmp = i1 true                      ; always true for valid threads
```

```llvm
; Before:
call void @llvm.nvvm.barrier0()
; (no shared memory operations between barriers)
call void @llvm.nvvm.barrier0()

; After:
call void @llvm.nvvm.barrier0()     ; redundant barrier removed
```

### Type Conversion Cleanup

GPU-specific type representations (`bf16`, `tf32`, `fp8`) produce conversion chains not present in standard LLVM IR:

```llvm
; Before (roundtrip through wider type):
%wide = fpext half %x to float
%back = fptrunc float %wide to half

; After:
; (use %x directly — roundtrip eliminated when no precision loss)
```

```llvm
; Before (bf16 roundtrip):
%f32 = call float @llvm.nvvm.bf16.to.f32(i16 %bf)
%bf2 = call i16 @llvm.nvvm.f32.to.bf16(float %f32)

; After:
; (use %bf directly)
```

### Post-Reflect Dead Code Cleanup

The companion pass `nvvm-reflect-pp` (`SimplifyConstantConditionalsPass`) runs immediately before the peephole in the pipeline. It resolves `__nvvm_reflect()` calls and simplifies constant conditionals:

```llvm
; Before (after nvvm-reflect-pp resolves __nvvm_reflect("__CUDA_FTZ") = 1):
%ftz = call i32 @__nvvm_reflect(ptr @"__CUDA_FTZ")     ; resolved to 1
%cmp = icmp ne i32 %ftz, 0                              ; always true
br i1 %cmp, label %ftz_path, label %no_ftz_path

; After nvvm-reflect-pp:
br label %ftz_path                   ; unconditional

; The peephole then cleans up dead instructions in %no_ftz_path
; and simplifies any resulting phi nodes at merge points
```

### Convergent Operation Canonicalization

CUDA's convergent operations (`__syncwarp`, `__ballot_sync`, etc.) have specific semantic constraints that standard InstCombine cannot reason about because it must treat `convergent` calls as opaque. The peephole, with knowledge of NVVM semantics, can simplify convergent call sequences when the mask or participating threads can be determined at compile time.

## Machine-Level NVPTXPeephole (sub_21DB090)

The machine-level peephole operates on `MachineInstr` objects after instruction selection has converted LLVM IR to PTX pseudo-instructions. It targets patterns specific to the PTX instruction set.

### Redundant cvta Folding

The `cvta` (convert address) instruction converts between generic and specific address spaces. Address space lowering often inserts redundant conversions:

```ptx
// Before:
cvta.to.global %rd1, %rd2        ; convert global -> generic
cvta.global %rd3, %rd1           ; convert generic -> global (redundant pair)

// After:
mov.b64 %rd3, %rd2               ; direct copy, cvta pair eliminated
```

The companion pass `sub_21DA810` (`"NVPTX optimize redundant cvta.to.local instruction"`) handles the remaining `cvta.to.local` instructions that survive to late post-RA:

```ptx
// Before (late pipeline):
cvta.to.local %rd1, %rd2         ; redundant when %rd2 is already local-space

// After (sub_21DA810 removes it):
; (use %rd2 directly)
```

### Predicate Pattern Optimization

PTX uses predicate registers for conditional execution. The peephole simplifies predicate sequences:

```ptx
// Before:
setp.ne.s32 %p1, %r1, 0;
@%p1 bra target;

// After (folds setp into branch when pattern is recognized):
// Combined compare-and-branch
```

### PTX Move Elimination

`sub_2204E60` (`"Remove redundant moves"`) eliminates identity moves:

```ptx
// Before:
mov.b32 %r5, %r5;               ; identity move

// After:
; (deleted)
```

## Satellite Machine Peephole Passes

Three additional machine-level passes perform specialized peephole transformations adjacent to the main NVPTXPeephole:

### param-opt (sub_2203290)

| | |
|---|---|
| **Pass name** | `param-opt` |
| **Entry point** | `sub_2203290` |
| **Description** | `"Optimize NVPTX ld.param"` |

Optimizes parameter load patterns. In PTX, kernel parameters are loaded via `ld.param` instructions into registers. When the same parameter is loaded multiple times (e.g., after inlining or loop unrolling), `param-opt` consolidates them:

```ptx
// Before:
ld.param.u32 %r1, [_param_0];
...
ld.param.u32 %r7, [_param_0];    ; redundant reload of same parameter

// After:
ld.param.u32 %r1, [_param_0];
...
mov.b32 %r7, %r1;                ; reuse previous load
```

### nvptx-trunc-opts (sub_22058E0)

| | |
|---|---|
| **Pass name** | `nvptx-trunc-opts` |
| **Entry point** | `sub_22058E0` |
| **Description** | `"Optimize redundant ANDb16ri instrunctions"` [sic] |

Eliminates redundant `AND` operations on b16 (16-bit) registers. When type legalization widens a sub-16-bit value to 16 bits, it inserts an AND with a mask to preserve the original width. If the value is already correctly masked (e.g., from a load that zero-extends), the AND is redundant:

```ptx
// Before:
ld.u8 %rs1, [%rd1];              ; loads 8-bit, zero-extended to 16
and.b16 %rs2, %rs1, 0xFF;        ; redundant mask — already 8-bit clean

// After:
ld.u8 %rs1, [%rd1];
// (AND deleted, use %rs1 directly)
```

The binary contains the string with a typo: `"instrunctions"` instead of `"instructions"`.

### Remove Redundant Moves (sub_2204E60)

| | |
|---|---|
| **Entry point** | `sub_2204E60` |
| **Description** | `"Remove redundant moves"` |

Eliminates move instructions where source and destination are the same register, or where the move is immediately dead. This complements the stock LLVM `MachineCopyPropagation` pass with PTX-specific move patterns.

## Knobs

| Knob | Type | Default | Scope | Effect |
|---|---|---|---|---|
| `enable-nvvm-peephole` | bool | true | IR + Machine | Master switch for both the IR-level `nvvm-peephole-optimizer` and the machine-level `nvptx-peephole`. Registered at `ctor_358_0` (`0x50E8D0`). |
| `disable-peephole` | bool | false | Machine only | Disables the stock LLVM `PeepholeOptimizerPass` (slot 492). Does **not** affect the NVIDIA-specific passes. Registered at `ctor_314` (`0x502360`). |
| `aggressive-ext-opt` | bool | (varies) | Machine only | Controls aggressive extension optimization in stock LLVM peephole. |
| `disable-adv-copy-opt` | bool | false | Machine only | Disables advanced copy optimization in stock LLVM peephole. |
| `rewrite-phi-limit` | int | (varies) | Machine only | Limits PHI rewriting in stock LLVM peephole. |
| `recurrence-chain-limit` | int | (varies) | Machine only | Limits recurrence chain analysis in stock LLVM peephole. |

The `enable-nvvm-peephole` description string recovered from the binary is: `"Enable NVVM Peephole Optimizer"`. Its default-on status indicates the pass is mature and does not require opt-in behavior.

## Optimization Level Behavior

The IR-level peephole runs in all optimization paths except `-O0`:

| Level | Path | NVVMPeephole invocations |
|---|---|---|
| Ofcmin | "ptx" path | 1 (early) |
| Ofcmid | "mid" path | 1 (after SROA/CVP) |
| O2/O3 | "default" path | 1 (after NVVMReflect + IntrinsicLowering) |
| O3 (late) | Tier finalization | 1 (after BranchFolding/CFGSimplify) |

At `-O0`, the peephole is likely skipped along with most optimization passes. The factory function `sub_1CEF8F0` appears only in code paths that are active at O1 and above.

## End-to-End Peephole Pipeline

The complete peephole optimization flow through cicc, from IR to PTX:

```text
Source CUDA
    |
    v
[LLVM IR after clang/EDG frontend]
    |
    v
InstCombine (0x1700000+)           General algebraic simplification
    |                               ~600 functions, target-independent
    v
NVVMReflect (sub_1857160)          Resolve __nvvm_reflect() calls
    |
    v
nvvm-reflect-pp                    Simplify constant conditionals from reflect
    |
    v
NVVMIntrinsicLowering (sub_1CB4E40) Expand NVVM intrinsics
    |
    v
nvvm-peephole-optimizer             NVVM-specific IR patterns:
  (sub_1CEF8F0 factory)              - addrspacecast chain folding
    |                                - intrinsic sequence simplification
    v                                - type conversion roundtrip elimination
NVVMAnnotationsProcessor             - post-reflect dead code cleanup
  (sub_215D9D0 companion)
    |
    v
[Further IR optimization: GVN, LICM, Sinking2, etc.]
    |
    v
[Instruction Selection: DAGToDAG (sub_2200150, 78KB)]
    |     Hash-table pattern matching: hash = (37*idx) & (tableSize-1)
    v
PeepholeOptimizerPass (slot 492)    Stock LLVM machine peephole:
    |                                - redundant copy folding
    v                                - compare-and-branch simplification
NVPTXPeephole (sub_21DB090)         PTX-specific machine peephole:
    |                                - cvta pair elimination
    v                                - predicate folding
param-opt (sub_2203290)              - ld.param consolidation
    |
    v
nvptx-trunc-opts (sub_22058E0)      - ANDb16ri elimination
    |
    v
Remove Redundant Moves (sub_2204E60) - identity move deletion
    |
    v
[Register Allocation]
    |
    v
ProxyRegErasure (sub_21DA810)       Late cvta.to.local removal
    |
    v
[PTX Emission]
```

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| — | `sub_1CEF8F0` | small | NVVMPeephole factory (legacy PM) |
| — | `sub_215D9D0` | — | NVVMAnnotationsProcessor (companion, always paired) |
| — | `sub_2314DA0` | small | NVVMPeepholeOptimizerPass serializer (New PM) |
| — | `sub_2342890` | — | New PM registration function (slot 382) |
| — | `sub_233C410` | — | Pipeline text parser (line 3534) |
| — | `sub_21DB090` | small | NVPTXPeephole machine pass registration |
| — | `sub_2166ED0` | 1.6KB | `addPreRegAlloc()` — hosts NVPTXPeephole |
| — | `sub_21DA810` | — | ProxyRegErasure (`cvta.to.local` removal) |
| — | `sub_2203290` | small | `param-opt` (`ld.param` optimization) |
| — | `sub_2204E60` | small | Remove Redundant Moves |
| — | `sub_22058E0` | small | `nvptx-trunc-opts` (ANDb16ri elimination) |
| — | `sub_21BEE70` | 4.1KB | `"Bad address space in addrspacecast"` validation |
| — | `sub_20DA7F0` | 30KB | DAG combine / peephole on MachineInstrs |
| — | `sub_37E1AE0` | 18KB | Late-stage machine optimization (peephole or copy prop) |

## Differences from Upstream LLVM

Upstream LLVM (as of LLVM 17/18) contains `NVPTXPeephole.cpp` in `llvm/lib/Target/NVPTX/`, which implements a small machine-level pass that:

1. Folds `cvta` address-space-conversion pseudo-instructions
2. Removes `NVPTX::PROXY_REG` pseudo-instructions (now split into a separate `NVPTXProxyRegErasure` pass in cicc)

CICC v13.0 extends this significantly:

- **The IR-level pass (`nvvm-peephole-optimizer`) has no upstream counterpart.** It is entirely NVIDIA-proprietary, filling a gap between target-independent InstCombine and target-specific machine peephole.
- **Three satellite machine passes** (`param-opt`, `nvptx-trunc-opts`, `Remove Redundant Moves`) have no upstream equivalents.
- **The machine-level `nvptx-peephole`** is larger than upstream, likely incorporating additional pattern rules for newer PTX features (tensor core operations, cluster operations, etc.).
- **ProxyRegErasure** is separated from NVPTXPeephole into its own pass (`sub_21DA810`) and runs late post-RA rather than inline with the peephole.

## Evidence Summary

The pass's existence and classification are confirmed through multiple independent sources:

| Source | Address / Location | Evidence |
|---|---|---|
| Pipeline parser | `sub_233C410` line 3534 | Registers `"nvvm-peephole-optimizer"` as function-level NVIDIA custom pass |
| New PM registration | `sub_2342890` slot 382 | Maps string to `llvm::NVVMPeepholeOptimizerPass` |
| Serializer | `sub_2314DA0` | Produces `"nvvm-peephole-optimizer"` text for pipeline printing |
| Legacy PM factory | `sub_1CEF8F0` | Called 2x from `sub_12E54A0` (pipeline assembler) |
| Companion pairing | `sub_215D9D0` | Always immediately follows `sub_1CEF8F0` in all paths |
| Knob sweep | `0x50E8D0` (ctor_358_0) | `enable-nvvm-peephole` = `"Enable NVVM Peephole Optimizer"`, default true |
| Knob duplicate | `0x560000` sweep line 292 | Confirmed with identical description |
| NVVMPassOptions | `p2a.3-03-passoptions.txt` | Listed as `nvvm-peephole-optimizer` in option table |
| Machine pass | `sub_21DB090` | `"NVPTX Peephole"` / `"nvptx-peephole"` registration string |
| Machine pipeline | `sub_2166ED0` | `"After codegen peephole optimization pass"` checkpoint string |

**Confidence note.** The pass registration, knobs, pipeline position, and factory function are confirmed at HIGH confidence from binary evidence. The specific transformation patterns described above are at MEDIUM confidence — inferred from pipeline position (runs after NVVMReflect + NVVMIntrinsicLowering), NVVM IR semantics, and address space validation code, but the actual `NVVMPeepholeOptimizerPass::run()` body has not been individually decompiled. The factory `sub_1CEF8F0` creates the pass object; the run method is dispatched through the object's vtable.

## Cross-References

- [Scalar Passes (InstCombine)](../llvm/scalar-passes.md) — stock LLVM InstCombine that handles general-purpose peephole
- [NVVM Intrinsic Lowering](./nvvm-intrinsic-lowering.md) — runs before the peephole, expands intrinsics
- [NVVMReflect](./nvvm-reflect.md) — resolves `__nvvm_reflect()` before the peephole cleans up
- [Machine-Level Passes](../llvm/machine-passes.md) — documents the full pre-RA / post-RA machine pass pipeline
- [Minor NVIDIA Passes](./other.md) — brief entries for `nvptx-peephole`, `proxy-reg-erasure`, and other small passes
- [Address Spaces](../reference/address-spaces.md) — NVPTX address space numbering and cast rules
- [NVVMPassOptions](../config/nvvm-pass-options.md) — the 4512-byte options struct that gates this pass
- [Optimization Levels](../config/optimization-levels.md) — which paths invoke the peephole at each -O level
- [Pipeline Assembler](../pipeline/optimizer.md) — the master `sub_12E54A0` function that builds the pass pipeline
