# Minor NVIDIA Passes

This page indexes NVIDIA-proprietary passes that are too small or insufficiently decompiled for dedicated pages. For the ten passes that were previously documented here and now have full pages, see the links below.

## Passes with Dedicated Pages

| Pass | Page |
|---|---|
| NVVM IR Verifier | [nvvm-verify (Deep Dive)](./nvvm-verify-deep.md) |
| NVVM Intrinsic Lowering | [nvvm-intrinsic-lowering](./nvvm-intrinsic-lowering.md) |
| Dead Synchronization Elimination | [dead-sync-elimination](./dead-sync-elimination.md) |
| IV Demotion | [iv-demotion](./iv-demotion.md) |
| Struct/Aggregate Splitting | [struct-splitting](./struct-splitting.md) |
| Base Address Strength Reduction | [base-address-sr](./base-address-sr.md) |
| Common Base Elimination | [common-base-elim](./common-base-elim.md) |
| CSSA (Conventional SSA) | [cssa](./cssa.md) |
| FP128/I128 Emulation | [fp128-emulation](./fp128-emulation.md) |
| Memmove Unrolling | [memmove-unroll](./memmove-unroll.md) |

---

## alloca-hoisting -- Entry Block Alloca Consolidation

| Field | Value |
|---|---|
| **Pass ID** | `alloca-hoisting` |
| **Entry point** | `sub_21BC7D0` |
| **Scope** | Machine-level pass |

PTX requires all stack allocations to reside in the entry block. This pass moves `alloca` instructions inserted by inlining or loop transforms into the entry block, preserving order and alignment. Without it, non-entry-block allocas produce invalid PTX.

## image-optimizer -- Texture/Surface Access Optimization

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-image-optimizer` |
| **Entry point** | `sub_21BCF10` |
| **Scope** | Machine-level pass (pre-emission) |

Groups related texture loads for cache utilization and merges redundant surface operations. Works in coordination with Replace Image Handles (below). See also [Machine-Level Passes](../llvm/machine-passes.md).

## nvptx-peephole -- Machine-Level Peephole

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-peephole` |
| **Entry point** | `sub_21DB090` |
| **Scope** | Machine-level pass (pre-RA) |
| **Knob** | `enable-nvvm-peephole` (default: on) |

PTX-specific peephole that folds redundant `cvta` address space conversions, optimizes predicate patterns, and simplifies PTX-specific instruction sequences. Distinct from the IR-level [NVVM Peephole](./nvvm-peephole.md). See [Machine-Level Passes](../llvm/machine-passes.md) for pipeline position.

## proxy-reg-erasure -- Redundant cvta.to.local Removal

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-proxy-reg-erasure` |
| **Entry point** | `sub_21DA810` |
| **Scope** | Machine-level pass (late post-RA) |

Removes redundant `cvta.to.local` instructions left by address space lowering. Runs late in the pipeline after register allocation. See [Machine-Level Passes](../llvm/machine-passes.md).

## valid-global-names -- PTX Identifier Sanitization

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-assign-valid-global-names` |
| **Entry point** | `sub_21BCD80` |
| **Scope** | Machine-level pass (pre-emission) |

Rewrites global symbol names to comply with PTX naming rules, removing characters illegal in PTX identifiers (`@`, `$`, etc.). Runs immediately before PTX emission.

## replace-image-handles -- Texture/Surface Handle Substitution

| Field | Value |
|---|---|
| **Pass ID** | `nvptx-replace-image-handles` |
| **Entry point** | `sub_21DBEA0` |
| **Scope** | Machine-level pass (pre-emission) |

Replaces IR-level texture/surface handle references with PTX-level `.tex` / `.surf` declarations. Paired with image-optimizer above. See [Machine-Level Passes](../llvm/machine-passes.md).

## extra-mi-printer -- Register Pressure Diagnostics

| Field | Value |
|---|---|
| **Pass ID** | `extra-machineinstr-printer` |
| **Entry point** | `sub_21E9E80` |
| **Scope** | Diagnostic (debug-only) |

Prints per-function register pressure statistics. Used for tuning pressure heuristics during development. Not active in release builds.

## nvvm-intr-range -- Intrinsic Range Metadata

| Field | Value |
|---|---|
| **Pass ID** | `nvvm-intr-range` |
| **Entry point** | `sub_216F4B0` |
| **Scope** | Function pass (IR level) |
| **Knob** | `nvvm-intr-range-sm` (`ctor_359`) |

Attaches `!range` metadata to NVVM intrinsics that return hardware-bounded values (`threadIdx.x`, `blockIdx.x`, etc.), enabling downstream known-bits analysis and range-based dead code elimination. Tightens ranges when `__launch_bounds__` metadata is present. Documented in detail in [KnownBits & DemandedBits](../llvm/known-bits.md).

## GenericToNVVM -- Global Address Space Migration

| Field | Value |
|---|---|
| **Pass ID** | `generic-to-nvvm` |
| **Entry point** | `sub_215DC20` |
| **Size** | 36 KB |

Moves global variables from generic address space (AS 0) to global address space (AS 1), inserting `addrspacecast` at use sites. Required because PTX globals must reside in `.global` memory. Documented in detail in [PTX Emission](../pipeline/emission.md#generictonvvm--sub_215dc20).

---

## Other Passes Documented Elsewhere

These passes appear in the NVPTX backend but have primary documentation on other pages:

| Pass | Entry | Primary Page |
|---|---|---|
| nvvm-pretreat | `PretreatPass` (New PM slot 128) | [Optimizer Pipeline](../pipeline/optimizer.md) |
| NLO (Simplify Live Output) | `sub_1CE10B0`, `sub_1CDC1F0` | [Rematerialization](./rematerialization.md) |
| Prolog/Epilog | `sub_21DB5F0` | [Machine-Level Passes](../llvm/machine-passes.md), [PrologEpilogInserter](../llvm/prolog-epilog.md) |
| LDG Transform | `sub_21F2780` (`ldgxform`) | [Machine-Level Passes](../llvm/machine-passes.md), [Code Generation](../pipeline/codegen.md) |
| Machine Mem2Reg | `sub_21F9920` (`nvptx-mem2reg`) | [Machine-Level Passes](../llvm/machine-passes.md), [Code Generation](../pipeline/codegen.md) |
