# Remaining NVIDIA Passes

This page covers NVIDIA-proprietary passes that do not have dedicated wiki pages. Each subsection documents a distinct pass with its registration identity, known implementation details, and decompiled evidence.

## nvvm-verify -- Module-Level IR Verifier

The NVVM verifier validates that the IR conforms to NVPTX constraints before optimization and code generation. It is one of the largest NVIDIA custom passes at 143 KB across four sub-verifiers.

| | |
|---|---|
| **Pass name** | `nvvm-verify` |
| **Class** | `llvm::NVVMIRVerifierPass` |
| **Scope** | Module pass |
| **Registration** | New PM slot 129 |
| **Module verifier** | `sub_2C80C90` (51 KB) |
| **Function verifier** | `sub_2C771D0` (36 KB) |
| **Intrinsic verifier** | `sub_2C7B6A0` (143 KB) |
| **Knob** | `nvvm-verify-show-info` (registered at `ctor_257`) |

The module verifier (`sub_2C80C90`) validates:

- **Data layout**: must not be empty; prints example valid layouts for 32-bit and 64-bit targets.
- **Target triple**: in UnifiedNVVMIR mode, must exactly match one of eight whitelisted triples: `nvptx-nvidia-cuda`, `nvptx64-nvidia-cuda`, `nvptx-nvidia-nvcl`, `nvptx64-nvidia-nvcl`, `nvsass-nvidia-cuda`, `nvsass-nvidia-nvcl`, `nvsass-nvidia-directx`, `nvsass-nvidia-spirv`. The `nvsass` triples confirm CICC can compile directly to native GPU assembly for DirectX and SPIR-V shader pipelines.
- **Per-instruction validation**: a switch on opcode (0x1E through 0x60) checks atomics, address space casts, fence ordering, alloca constraints, and tensor memory restrictions.

Notable error messages: `"Allocas are not supported on address spaces except Generic"`, `"Tensor Memory loads/stores are not supported"`, `"pragma unroll is not supported. Please use llvm.loop.unroll.count instead"`.

The function verifier (`sub_2C771D0`) checks cluster dimensions (sm_90+ only), parameter width (integers < 32 bits require `sext`/`zext`), and rejects 17 unsupported function attributes (`naked`, `ssp`, `uwtable`, sanitizers, etc.).

The intrinsic verifier (`sub_2C7B6A0`, 143 KB) validates every NVVM intrinsic call against SM capabilities using architecture-gated checks. SM version is stored internally as SM*10 (e.g., sm_90 = 900). It covers MMA dimension/type validation, rounding modes, address space constraints, constant argument requirements, and WMMA fragment sizes.

## nvvm-pretreat -- IR Pre-Treatment

| | |
|---|---|
| **Pass name** | `nvvm-pretreat` |
| **Class** | `llvm::PretreatPass` |
| **Scope** | Module pass |
| **Registration** | New PM slot 128 |

The pretreat pass runs early in the pipeline, before the main optimization sequence. Its exact transformations have not been deeply decompiled, but its position (immediately before `nvvm-verify`) suggests it normalizes IR into a form the verifier and subsequent passes expect -- canonicalizing metadata, resolving NVVM-specific annotations, and preparing address space attributes.

## GenericToNVVM -- Global Address Space Migration

| | |
|---|---|
| **Pass ID** | `generic-to-nvvm` |
| **Entry point** | `sub_215DC20` |
| **Size** | 36 KB |
| **Scope** | Machine-level pass |

This pass moves global variables from the generic address space (AS 0) to the global address space (AS 1). In PTX, globals must reside in `.global` memory to be correctly addressed. The pass iterates all `GlobalVariable` objects in the module and rewrites their address space, inserting `addrspacecast` instructions at use sites as needed. It runs at the machine level (post-ISel) as part of the NVPTX backend pipeline.

## alloca-hoisting -- Entry Block Alloca Consolidation

| | |
|---|---|
| **Pass ID** | `alloca-hoisting` |
| **Entry point** | `sub_21BC7D0` |
| **Scope** | Machine-level pass |

PTX requires all stack allocations to reside in the function's entry block. This pass scans every basic block for `alloca` instructions and moves them to the entry block, preserving their order and alignment. Without this pass, allocas created by inlining or loop transformations would appear in non-entry blocks, producing invalid PTX.

## Image Optimizer -- Texture/Surface Access Optimization

| | |
|---|---|
| **Entry point** | `sub_21BCF10` |
| **Scope** | Machine-level pass |

The image optimizer analyzes texture and surface access patterns and applies hardware-specific optimizations. It groups related texture loads to improve cache utilization and merges redundant surface operations. It works in coordination with the Replace Image Handles pass (`sub_21DBEA0`), which substitutes IR-level image handles with concrete PTX texture/surface references.

## CSSA -- Conventional SSA for Divergent Control Flow

| | |
|---|---|
| **Entry point** | `sub_3720740` |
| **Scope** | Machine-level pass |
| **Knobs** | `cssa-coalesce`, `cssa-verbosity`, `dump-before-cssa` |
| **Debug string** | `"IR Module before CSSA"` |

GPU programs exhibit thread divergence: different threads in a warp may take different control flow paths. Standard SSA form does not account for this divergence, leading to incorrect phi-node resolution when threads reconverge. The CSSA transformation converts the IR into a form where phi nodes respect warp divergence semantics, inserting additional copies at reconvergence points.

The `cssa-coalesce` knob controls whether the pass attempts to coalesce copies introduced during the transformation. `cssa-verbosity` enables detailed debug output, and `dump-before-cssa` dumps the IR module before the transformation begins.

## NLO -- Simplify Live Output

| | |
|---|---|
| **Entry points** | `sub_1CE10B0` (48 KB), `sub_1CDC1F0` (35 KB) |
| **Strings** | `"Simplify Live Output"`, `"nloNewBit"`, `"newBit"`, `"nloNewAdd"` |
| **Scope** | Function pass (IR level) |

NLO creates new add and bitwise operations to simplify live-out values at basic block boundaries. When a value crossing a block boundary is a complex expression, NLO decomposes it into simpler operations that can be recomputed cheaply, reducing register pressure. The `"nloNewBit"` and `"nloNewAdd"` strings name the synthesized instructions.

## IV Demotion -- Induction Variable Narrowing

| | |
|---|---|
| **Entry point** | `sub_1CD74B0` |
| **Size** | 75 KB |
| **Strings** | `"phiNode"`, `"demoteIV"`, `"newInit"`, `"newInc"`, `"argBaseIV"`, `"newBaseIV"`, `"iv_base_clone_"`, `"substIV"` |

IV Demotion narrows induction variables from wider types (typically 64-bit) to narrower types (typically 32-bit) when the loop trip count fits in the smaller type. This is critical for GPU performance because 32-bit integer operations are significantly faster than 64-bit operations on most NVIDIA architectures.

The pass creates new base IVs (`"newBaseIV"`), clones IV chains (`"iv_base_clone_"`), and substitutes the narrowed versions (`"substIV"`) throughout loop bodies. The `"demoteIV"` string names the demoted phi node, and `"newInit"` / `"newInc"` name the replacement initial value and increment.

## Memmove Unrolling

| | |
|---|---|
| **Entry point** | `sub_1C82A50` |
| **Size** | 39 KB (~1,200 lines) |
| **Threshold knob** | `dword_4FBD560` (compile-time unroll size threshold) |

This pass replaces `memmove` and `memcpy` calls with unrolled element-wise copy loops. It generates both forward and reverse copy paths to handle overlapping memory correctly.

The pass creates four basic blocks: `"split"` (direction comparison), `"forward.for"` (forward copy loop), `"reverse.for"` (reverse copy loop), and `"nonzerotrip"` (exit). The split block compares source and destination addresses to decide direction.

For sizes below the threshold `dword_4FBD560`, the pass generates fully unrolled element-by-element copies with GEP names `"src.memmove.gep.unroll"` and `"dst.memmove.gep,unroll"` (the comma in the dst name is a naming inconsistency in the binary). For larger or dynamic sizes, it generates a loop with a PHI induction variable.

## Struct/Aggregate Splitting

| | |
|---|---|
| **Entry point** | `sub_1C86CA0` |
| **Size** | 72 KB (~1,200+ lines, 500+ locals) |

This pass decomposes struct and aggregate operations into element-wise scalar operations. GPUs cannot natively operate on aggregate types, so this decomposition is essential for register allocation.

The pass creates NVVM-specific `splitStruct` instructions (opcode 32) that decompose aggregates, then replaces all uses of the original aggregate with the individual elements via `sub_164D160`. Alignment is carefully preserved using the formula `1 << (alignment_field >> 1) >> 1`.

## FP128/I128 Emulation

| | |
|---|---|
| **Entry point** | `sub_1C8C170` |
| **Size** | 25 KB (~960 lines) |
| **Runtime functions** | 48 distinct `__nv_*` library calls |

GPUs lack native 128-bit arithmetic support. This pass replaces FP128 and I128 operations with calls to NVIDIA runtime library functions. The dispatch is based on the instruction opcode byte at offset +16.

Key function families:

| Category | Functions |
|---|---|
| FP128 arithmetic | `__nv_add_fp128`, `__nv_sub_fp128`, `__nv_mul_fp128`, `__nv_div_fp128`, `__nv_rem_fp128` |
| I128 division | `__nv_udiv128`, `__nv_idiv128`, `__nv_urem128`, `__nv_irem128` |
| FP128 conversions | `__nv_fp128_to_uint{8,16,32,64,128}`, `__nv_fp128_to_int{8,16,32,64,128}`, reverse |
| FP128 to/from float | `__nv_fp128_to_float`, `__nv_fp128_to_double`, `__nv_float_to_fp128`, `__nv_double_to_fp128` |
| I128 to/from float | `__nv_cvt_f32_u128_rz`, `__nv_cvt_f64_i128_rz`, etc. (`_rz` = round-toward-zero, `_rn` = round-to-nearest) |
| FP128 comparisons | `__nv_fcmp_oeq`, `__nv_fcmp_olt`, `__nv_fcmp_uno`, etc. (14 predicates, ordered/unordered) |

## Base Address Strength Reduction

| | |
|---|---|
| **Entry point** | `sub_1C67780` |
| **Size** | 58 KB (~1,400 lines) |
| **Confirmed name** | String `"BaseAddressStrengthReduce"` at line 457 |
| **Knob** | `do-base-address-strength-reduce` |
| **Negative offset knob** | `dword_4FBCAE0` (aggressiveness control) |

Optimizes repeated address computations in loop bodies by factoring out common base address expressions. The pass collects address patterns into hash maps, finds the minimum constant offset among all uses of the same base pointer (the "anchor"), and rewrites other addresses as `(anchor + relative_offset)`.

## Common Base Elimination

| | |
|---|---|
| **Entry point** | `sub_1C5DFC0` |
| **Size** | 38 KB (~850 lines) |

Hoists common base address expressions to dominating points in the control flow graph. For each group of memory operations sharing the same base pointer, the pass creates a new base computation at the common dominator and rewrites all uses as `(hoisted_base + relative_offset)`.

This pass complements Base Address Strength Reduction: BASR focuses on intra-loop induction-variable-based addresses, while Common Base Elimination handles inter-block dominator-based hoisting. Together they reduce address computation overhead, which is critical since GPUs have limited integer ALU units relative to FP throughput.

## NVVM Intrinsic Lowering

| | |
|---|---|
| **Entry point** | `sub_2C63FB0` |
| **Size** | 140 KB |
| **Iteration limit** | `qword_5010AC8` (default = 30) |

A pattern-matching rewrite system that lowers NVVM intrinsic calls into equivalent standard IR operations. It handles vector operation decomposition (breaking wide vector intrinsics into narrower operations), shuffle vector lowering, and type conversion lowering. The iteration limit of 30 prevents infinite expansion when lowering produces new intrinsic calls that themselves need lowering.

## NVVMIntrRange -- Intrinsic Range Metadata

| | |
|---|---|
| **Pass ID** | `nvvm-intr-range` |
| **Entry point** | `sub_216F4B0` |

Adds `!range` metadata to NVVM intrinsics that return bounded values, such as `threadIdx.x` (bounded by block dimensions) and `blockIdx.x` (bounded by grid dimensions). This metadata enables downstream optimizations like known-bits analysis and range-based dead code elimination.

## Other Machine-Level Passes

Several additional machine-level passes handle PTX-specific requirements:

| Pass | Entry | Purpose |
|---|---|---|
| NVPTX Peephole | `sub_21DB090` | Machine-level peephole optimization after instruction selection |
| Prolog/Epilog | `sub_21DB5F0` | Custom frame management (PTX has no traditional prolog/epilog) |
| Replace Image Handles | `sub_21DBEA0` | Substitutes IR-level image handles with PTX texture/surface references |
| Valid Global Names | `sub_21BCD80` | Sanitizes global names to valid PTX identifiers (`nvptx-assign-valid-global-names`) |
| Extra MI Printer | `sub_21E9E80` | Register pressure statistics reporting (`extra-machineinstr-printer`) |
| LDG Transform | `sub_21F2780` | Transforms global loads to `ldg.*` (texture cache) for read-only data |
| Machine Mem2Reg | `sub_21F9920` | Promotes `__local_depot` stack objects back to registers post-regalloc (`nvptx-mem2reg`) |
