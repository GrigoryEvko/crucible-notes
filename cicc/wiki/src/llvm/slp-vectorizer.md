# SLP Vectorizer

> **NVIDIA-modified pass.** See [Key Behavioral Differences from Upstream](#key-behavioral-differences-from-upstream) for GPU-specific changes.

The SLP (Superword-Level Parallelism) vectorizer packs independent scalar operations on adjacent data into vector operations. Unlike the loop vectorizer, SLP operates on straight-line code within a single basic block --- it does not require a loop. On NVPTX, the practical payoff is combining two or four scalar loads/stores into `ld.v2`/`ld.v4` (or `st.v2`/`st.v4`), and folding arithmetic on adjacent elements into a single wider instruction. CICC runs the SLP vectorizer as part of the combined `LoopVectorize / SLPVectorize` pass group at step 31 of the O2 pipeline (`sub_19B73C0`), after SCCP/GlobalOpt and before the post-vectorization GVN cleanup. The pass is registered under the name `slp-vectorizer` (pipeline slot 350, `llvm::SLPVectorizerPass`).

| Property | Value |
|---|---|
| Pass name | `slp-vectorizer` |
| Pipeline slot | 350 (`llvm::SLPVectorizerPass`) |
| Constructor registration | `ctor_517` at `0x560FD0` (12,410 bytes) |
| Option constructor | `ctor_248` at `0x4EEF30` (8,219 bytes) |
| Horizontal reduction entry | `sub_2BD1C50` (~85 KB, ~3,005 decompiled lines) |
| Straight-line SLP entry | `sub_2BCE070` |
| Store-SLP entry | `sub_2BCA110` |
| SLP tree code cluster | `0x1BC0000`--`0x1BFFFFF` (~1,353 KB across ~266 files) |
| Key diagnostic strings | `"slp-vectorizer"`, `"HorSLPNotBeneficial"`, `"VectorizedHorizontalReduction"`, `"const.rdx"`, `"SLP vectorized with cost"`, `"Cannot SLP vectorize list:"`, `"Stores SLP vectorized with cost"` |

## SLP vs Loop Vectorization on GPU

The loop vectorizer (see [LoopVectorize & VPlan](./loop-vectorize.md)) transforms counted loops by widening the loop body to process multiple iterations per step, driven by VPlan. SLP vectorization is fundamentally different: it searches a single basic block for groups of isomorphic scalar instructions that operate on adjacent memory or independent data, then replaces them with a single vector instruction. No loop structure is required.

On a GPU, SLP opportunities arise in three main patterns:

1. **Adjacent memory operations.** Two consecutive `f32` loads from addresses `p` and `p+4` become a single `ld.v2.f32`. Four consecutive `i32` stores become `st.v4.b32`. This is the highest-value SLP transformation on NVPTX because coalesced memory transactions are critical for throughput.

2. **Same-typed arithmetic on independent operands.** Two `fadd` instructions with no data dependency between them can become a single vector `fadd` on `<2 x float>`. The PTX backend later lowers this back to scalar instructions if the target has no native wide ALU, but the combined form enables better scheduling and may survive to the load/store vectorizer's benefit.

3. **Texture coordinate packing.** Texture/surface sampling requires coordinate tuples (u, v) or (u, v, w). When the scalar coordinates are computed independently, SLP can pack them into a `<2 x float>` or `<4 x float>` bundle that feeds directly into the sampling intrinsic, avoiding per-element extract/insert overhead.

## NVPTX TTI Hooks Affecting SLP

The SLP vectorizer consults TargetTransformInfo at several decision points. NVIDIA's proprietary TTI implementation differs significantly from the upstream open-source NVPTX backend.

### Upstream Open-Source NVPTX TTI (for reference)

| Hook | Return Value | Comment |
|------|-------------|---------|
| `getRegisterBitWidth(Vector)` | 32 bits | "Only `<2 x half>` should be vectorized" |
| `getMinVectorRegisterBitWidth()` | 32 bits | Matches 32-bit register file |
| `getNumberOfRegisters()` | 1 (all classes) | FIXME in source: "this is conservative" |
| `getArithmeticInstrCost(i64)` | 2x base for ADD/MUL/XOR/OR/AND | Reflects 32-bit ALU emulation |
| `supportsScalableVectors()` | `false` | No SVE/RVV equivalent in PTX |

With these returns, the standard LLVM VF formula (`registerBitWidth / elementBitWidth`) produces VF = 1 for `f32` and VF = 2 for `f16`. The open-source backend effectively limits SLP to `<2 x half>` bundles only.

### CICC v13.0 Proprietary TTI

CICC overrides the upstream returns at three levels: the TTI wrapper pass, the SLP tree's internal scheduling-width parameter, and several SLP-specific helper functions that query TTI indirectly.

**TTI hooks queried by SLP (directly or via the cost model):**

| Hook | Address | Return / Behavior | SLP Impact |
|------|---------|------------------|------------|
| `getRegisterBitWidth(Vector)` | `sub_DFE640` | `TypeSize::getFixed(32)` | Formal register width --- same as upstream. But see `a2+840` override below. |
| `getRegisterBitWidth(Scalar)` | `sub_DFB1B0` | 32 | Confirms 32-bit register file for scalar cost comparison. |
| `supportsScalableVectors()` | `sub_DFE610` | `false` | Scalable VF never attempted. |
| `getInstructionCost()` | `sub_20E14F0` (33KB) | Per-opcode latency from scheduling model | Called indirectly through `getTreeCost()` (`sub_2B94A80`) for each tree node. |
| `getInstructionCost()` (IR-level) | `sub_B91420` | Per-instruction cost estimate | Called 7 times per instruction during per-node SLP cost evaluation. |
| `hasAttribute(47)` | `sub_B2D610` | Checks `alwaysvectorize` | When set, SLP skips profitability check and vectorizes unconditionally. |
| `hasAttribute(18)` | `sub_B2D610` | Checks `optnone` | When set, SLP is entirely disabled. |

**The `a2+840` scheduling-width override:**

The SLP tree object (`BoUpSLP`, parameter `a2` in the horizontal reduction entry `sub_2BD1C50`) stores a **max register pressure / scheduling width** at offset +840. This value does NOT come from `getRegisterBitWidth(Vector)` directly. Instead, it is computed during SLP tree initialization from a combination of the target's scheduling model and available register budget. In the decompiled code, the VF derivation at lines 1354-1578 reads this value and clamps the resulting bit width to **[128, 512]**:

```c
// VF derivation from a2+840 (decompiled sub_2BD1C50)
uint64_t max_sched_width = *(a2 + 840);       // NOT from TTI.getRegisterBitWidth()
uint64_t scalar_width = sub_2B49BC0(a2, first_scalar);  // getScalarTypeWidth()

uint64_t vf;
if (scalar_width <= max_sched_width) {
    vf = 1 << bsr(max_sched_width / scalar_width);  // round-down power-of-2
    vf = clamp(vf, 128, 512);                        // clamp to [128, 512] BITS
} else {
    vf = 128;
}
// For f32 (32 bits) with max_sched_width=256: vf = 256/32 = 8 elements
// For f64 (64 bits) with max_sched_width=256: vf = 256/64 = 4 elements
```

This is the single most important NVIDIA divergence from upstream for SLP: the 32-bit `getRegisterBitWidth(Vector)` return would produce VF=1 for `f32` operations and kill SLP entirely for 32-bit types, but the `a2+840` scheduling width allows VF=4 or VF=8 for `f32`. The result is that CICC's SLP can produce `<4 x float>` bundles (later lowered to `ld.v4.f32` / `st.v4.f32`) that the open-source backend would never attempt.

**SLP-specific TTI helper functions:**

| Function | Address | Upstream Equivalent | Behavior |
|----------|---------|-------------------|----------|
| `getScalarTypeWidth()` | `sub_2B49BC0` | `DL.getTypeSizeInBits()` | Returns bit width of a scalar type for VF computation. |
| `getNextLegalVF()` | `sub_2B1E190` | No direct equivalent | Steps down through legal vector factors when current VF is unprofitable. Takes `(TTI, type, currentVF)`, returns next smaller legal VF >= minimum VF. Respects PTX v2/v4 legality constraints. |
| `adjustVF()` | `sub_2B1FA70` | Partial in `BoUpSLP::buildTree` | When `SLPMaxVF` (`qword_500F628`) is non-zero and `operand_count+1` is a power of 2, returns `operand_count` directly (non-power-of-2 VF). Otherwise computes a power-of-2 VF. |
| `isTreeNotBeneficialForArch()` | `sub_2B2DA40` | Not in upstream | NVIDIA-specific early rejection based on SM reduction type (`a1+1576`). Rejects trees whose structure is known to be unprofitable on the current GPU architecture. |

### Arithmetic Cost Impact on SLP Trees

The TTI cost model for i64 operations directly affects SLP profitability. Since NVPTX GPUs emulate all 64-bit integer arithmetic through pairs of 32-bit operations, the cost differential inflates the scalar cost baseline, making i64 SLP trees more profitable in relative terms:

| Operation | i32 Scalar Cost | i64 Scalar Cost | i64 Vector Cost (v2) | SLP Delta |
|-----------|----------------|----------------|---------------------|-----------|
| ADD/SUB | 1 | 2 (add.cc + addc) | 4 (two add.cc + addc pairs) | Neutral (2x scalar = 2x vector) |
| MUL | 1 | ~4 (mul.lo + mul.hi + add chain) | ~8 | Neutral |
| Loads | 1 | 1 (ld.b64) | 1 (ld.v2.b64) | **Profitable** --- single wide load |
| Stores | 1 | 1 (st.b64) | 1 (st.v2.b64) | **Profitable** --- single wide store |

The asymmetry is clear: SLP profit on NVPTX comes almost entirely from **memory coalescing** (loads and stores), not from arithmetic. The arithmetic cost for a v2 bundle is roughly 2x the scalar cost for all types, providing no ALU benefit. But a `ld.v2.f32` replaces two separate load instructions with one, reducing instruction count and improving coalescing. This is why Store-SLP (`sub_2BCA110`) and the load/store adjacency heuristics dominate profitable SLP on GPU.

## Maximum Vector Width on NVPTX

PTX supports vector types up to `.v4` for most data types, but the actual hardware constraint is tighter:

- **v2**: Supported for all types (`.b8` through `.b64`, `.f16`, `.f32`, `.f64`). This is the sweet spot for SLP.
- **v4**: Supported for `.b8`, `.b16`, `.b32`, `.f16`, `.f32`. NOT supported for `.b64`/`.f64`.
- **v8/v16**: Not supported in PTX at all. CPU-style AVX-width vectorization is never legal.

The SLP vectorizer's VF selection logic at `sub_2BD1C50` lines 1354–1578 computes:

```c
// VF selection pseudocode (from decompiled sub_2BD1C50)
uint64_t max_sched_width = *(a2 + 840);  // from TTI
uint64_t scalar_width = getScalarTypeWidth(a2, first_scalar);

uint64_t vf;
if (scalar_width <= max_sched_width) {
    vf = 1 << bsr(max_sched_width / scalar_width);  // round-down power-of-2
    vf = clamp(vf, 128, 512);                        // clamp to [128, 512] bits
} else {
    vf = 128;
}
```

For `f32` (32 bits) with a max scheduling width of 256 bits, this yields VF = 8 elements. However, PTX legalization later splits anything wider than v4 into multiple instructions, so the effective maximum is v4 for 32-bit types and v2 for 64-bit types. The SLP cost model accounts for this split cost.

## GPU-Specific Vectorization Constraints

### Legal Vector Types on NVPTX

The NVPTX target has exactly ONE vector register class --- `Int32HalfRegs` (`.b32`, prefix `%hh`) --- which holds 32 bits of packed data. The only legal vector types at the SelectionDAG level are:

| Type | Packing | Register | Legal Since |
|------|---------|----------|-------------|
| `v2f16` | Two `f16` in 32 bits | `%hh` | SM 53+ |
| `v2bf16` | Two `bf16` in 32 bits | `%hh` | SM 80+ |
| `v2i16` | Two `i16` in 32 bits | `%hh` | SM 53+ |
| `v4i8` | Four `i8` in 32 bits | `%hh` | SM 70+ |

**Every other vector type is illegal** and must be split or scalarized during type legalization (`sub_2029C10` / `sub_202E5A0`). This includes `<2 x float>`, `<4 x float>`, `<2 x i32>`, and `<2 x double>` --- the very types SLP produces for 32-bit and 64-bit operations.

### How SLP Vectors Survive to PTX

SLP-produced vector types such as `<4 x float>` are not killed by type legalization. Instead, the path is:

1. **SLP vectorizer** (IR level) produces `<4 x float>` loads, stores, and arithmetic in LLVM IR.
2. **SelectionDAG type legalization** splits `<4 x float>` into four scalar `f32` values for arithmetic operations. However, load and store nodes are intercepted by NVPTX's custom lowering (`NVPTXTargetLowering::LowerOperation`) which converts them to target-specific `NVPTX::LD_v4_f32` / `NVPTX::ST_v4_f32` pseudo-instructions.
3. **Instruction selection** maps these pseudo-instructions to PTX `ld.v4.f32` / `st.v4.f32`.
4. **Arithmetic** on the vector elements becomes four independent scalar instructions, which the scheduler can interleave with memory operations.

The net effect: SLP's primary benefit on NVPTX is vectorized memory access, while vectorized arithmetic is a wash. The cost model at `sub_2B94A80` (`getTreeCost`) accounts for this by assigning low cost to vector loads/stores and high scalarization overhead to vector arithmetic.

### PTX Vector Width Ceiling

PTX `.v2` and `.v4` load/store support imposes hard ceilings:

| Element Type | Max `.vN` | Max Bits | SLP VF Ceiling |
|-------------|----------|----------|---------------|
| `.b8` / `.u8` | `.v4` | 32 | 4 |
| `.b16` / `.f16` | `.v4` | 64 | 4 |
| `.b32` / `.f32` | `.v4` | 128 | 4 |
| `.b64` / `.f64` | `.v2` | 128 | 2 |
| `.b128` | `.v1` only | 128 | 1 (no vectorization) |

When the SLP VF exceeds the PTX ceiling (e.g., VF=8 for `f32` from the [128,512] bit-width clamping), the backend splits the single wide operation into multiple legal operations. The SLP cost model at `sub_2B889C0` factors this split cost into the tree evaluation, ensuring that overly wide VFs are rejected if the split overhead eliminates the coalescing benefit.

## Algorithm Overview

CICC's SLP vectorizer has three entry points that collectively implement the upstream `BoUpSLP` / `SLPVectorizerPass`:

### Straight-Line SLP (`sub_2BCE070`)

Scans each basic block for groups of isomorphic instructions (same opcode, adjacent or compatible operands). Builds a bottom-up SLP tree using `sub_2BAACB0` (`buildTree`), evaluates cost via `sub_2B94A80` (`getTreeCost`), and emits vector code via `sub_2BC6BE0` (`vectorizeTree`) when profitable. Diagnostic: `"SLP vectorized with cost N"` on success, `"Cannot SLP vectorize list:"` on failure.

### Store-SLP (`sub_2BCA110`)

Seeds the SLP tree from consecutive stores to adjacent memory addresses. This is the primary entry point for memory coalescing. Diagnostic: `"Stores SLP vectorized with cost N"`.

### Horizontal Reduction SLP (`sub_2BD1C50`)

The most complex path. Handles horizontal reductions (e.g., summing all elements of a vector). Proceeds in six phases:

**Phase 0 — Scalar chain scan.** Reads the reduction operand array at `a1+304` (pointer) and `a1+312` (count). Each bundle entry is 64 bytes. Classifies operands by opcode: values <= `0x1C` are simple scalars (add/sub/mul/etc.), values > `0x1C` are complex (fcmp, icmp variants). Calls `sub_2B0D8B0` (`isReductionOp`) to validate each operation as a legal reduction (add, fadd, mul, fmul, and, or, xor, smin/smax/umin/umax, fmin/fmax).

**Phase 1 — Hash table construction.** Builds two open-addressing hash tables. The "AllOps" table uses 32-byte entries with LLVM-layer sentinels (-4096 / -8192). See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function, probing strategy, and growth/compaction thresholds.

**Phase 2 — Bundle pair extraction.** Calls `sub_2B5F980` per bundle to classify reduction opcode pairs. When two consecutive bundles both contain `fadd` reductions (opcode 90), NVIDIA attempts a **paired fadd bundle merge** via `sub_2B3C030`/`sub_2B25EA0`/`sub_2B38BA0`. This is an NVIDIA-specific optimization for warp-level fadd reductions not present in upstream LLVM.

**Phase 3 — Main vectorization loop.** For each bundle, builds candidate operand lists, selects a VF, and tries vectorization with progressively smaller VFs on failure. The VF trial loop uses memoization (`sub_2B3C060`) to avoid re-trying the same (offset, VF) pair. Key substeps: `canVectorize` (legality), `buildTree`, `isTreeTinyAndNotFullyVectorizable` / `isTreeNotBeneficialForArch` (early rejection), `scheduleBlock`, `getTreeCost` + `getReductionCost` (profitability).

**Phase 4 — Final reduction codegen.** Produces the final horizontal reduction instruction via `sub_2B21C80` (`createFinalReduction`), chaining multiple entries with `sub_2B34820` when multiple sub-trees were vectorized.

**Phase 5 — Multi-tree scheduling and cleanup.** Builds a multi-tree reduction schedule, iteratively calling `sub_2B2F4A0` (`reduceTreeLevel`) until a single root value remains, then `replaceAllUsesWith` + `eraseFromParent`.

## Paired fadd Bundle Merging (NVIDIA-Specific)

This optimization is absent from upstream LLVM and targets warp-level floating-point reduction patterns common in CUDA kernels (e.g., block-level sum reductions, dot products, softmax denominators). When two consecutive reduction bundles both contain `fadd` operations, CICC attempts to merge them into a single wider bundle, doubling the effective vectorization width for the reduction.

### Trigger Condition

During Phase 2 of the horizontal reduction path (`sub_2BD1C50`, lines 921-1098), `sub_2B5F980` (`classifyReductionPair`) is called per bundle and returns a pair of reduction opcodes `(reductionOpcodeA, reductionOpcodeB)`. The merge path activates when:

1. Both opcodes in the current bundle equal **90** (0x5A), which is the internal opcode for `fadd` reduction.
2. The next consecutive bundle also has both opcodes equal to 90.
3. The two bundles are adjacent in the reduction operand array (no intervening non-fadd bundles).

```c
// Trigger check (decompiled from Phase 2, sub_2BD1C50)
if (v83 == v84 && v83 == 90) {        // both opcodes in bundle[i] are fadd
    if (v83_next == v84_next && v83_next == 90) {  // bundle[i+1] also all-fadd
        // Try paired merge
        sub_2B3C030(bundle_i, bundle_i_plus_1, ...);  // tryMergeFaddBundles
    }
}
```

### Three-Function Pipeline

The merge proceeds through three functions in sequence:

| Step | Function | Address | Role |
|------|----------|---------|------|
| 1. Try | `tryMergeFaddBundles()` | `sub_2B3C030` | Checks whether the two bundles' operand lists can be concatenated without violating data dependencies. Verifies that no operand in bundle B depends on the result of bundle A (or vice versa). Returns a candidate merged-bundle descriptor or null on failure. |
| 2. Validate | `validateMergedBundle()` | `sub_2B25EA0` | Confirms that the merged bundle satisfies SLP legality: all operands are isomorphic (same opcode), the combined operand count does not exceed `SLPMaxVF` limits, and the merged bundle's scheduling pressure stays within `a2+840`. Also checks that external uses of intermediate reduction values are compatible with the wider bundle. |
| 3. Rewrite | `rewriteMergedBundle()` | `sub_2B38BA0` | Physically merges the two bundle entries in the reduction operand array. The combined bundle gets double the operand count, and the second bundle slot is marked as consumed (skipped in Phase 3). Updates the AllOps hash table entries to point to the new merged bundle. |

### Why This Matters on GPU

Consider a warp-level sum reduction of 64 `f32` values, structured as two consecutive 32-element `fadd` reduction trees. Without merging, the SLP vectorizer processes each 32-element tree independently, producing two separate vectorized reduction chains. With merging, the combined 64-element tree exposes a wider VF window, allowing the vectorizer to produce wider `v4` bundles and reduce the total number of reduction shuffle steps.

The merged bundle also benefits the final reduction codegen (`sub_2B21C80`, `createFinalReduction`): instead of producing two separate reduction results and combining them with a scalar `fadd`, the merged tree produces a single reduction result directly.

### Commutativity Classification

The SM reduction type at `a1+1576` drives commutativity via bitmask `0x10804`:

```c
bool is_commutative;
if (reduction_type <= 0x10) {
    is_commutative = !((1 << reduction_type) & 0x10804);
    // Non-commutative types: 2, 14, 16 (likely fsub, signed cmp variants)
} else {
    is_commutative = true;
}
```

## SLP and the Load/Store Vectorizer

CICC runs two distinct passes that vectorize memory operations, and their scopes partially overlap:

| | SLP Vectorizer | OldLoadStoreVectorizerPass |
|---|---|---|
| Pass name | `slp-vectorizer` | `old-load-store-vectorizer` |
| Scope | Isomorphic ops in a BB | Adjacent loads/stores only |
| Seed | Any instruction group | Store/load chains |
| Handles arithmetic | Yes | No |
| Handles reductions | Yes (horizontal) | No |
| Pipeline position | Step 31 (with LoopVectorize) | Post-optimization (NVIDIA-specific) |
| Disable flag | `vectorize-slp` | `disable-nvptx-load-store-vectorizer` |

The NVIDIA-proprietary `old-load-store-vectorizer` (`llvm::OldLoadStoreVectorizerPass`) is a separate pass distinct from LLVM's `LoadStoreVectorizerPass`. It runs later in the pipeline and handles NVVM-specific intrinsic vectorization (`nvvm_load`/`nvvm_ld`, `nvvm_store`/`nvvm_st`) via the `vect-intrinsics` knob. SLP may vectorize the same load/store chains if they also contain arithmetic; the load/store vectorizer catches whatever SLP missed.

## Register Pressure Impact

SLP vectorization increases register pressure because vector values occupy wider registers. On NVPTX, a `<2 x float>` consumes two 32-bit registers (PTX has no native 64-bit float register file for packed types --- the backend lowers `<2 x f32>` to a pair of `.f32` registers). The benefit comes from reduced instruction count and improved memory coalescing, not from register savings.

The SLP cost model accounts for register pressure through `a2+840` (max scheduling width), and the profitability check rejects vectorization when the combined cost (tree cost + reduction cost) exceeds the threshold. When register pressure is already high, the TTI cost model inflates the scalarization overhead, making SLP less likely to fire.

## SLP Cost Model and TTI Callouts

The SLP profitability decision is the product of two cost functions that both delegate to TTI: `getTreeCost()` (`sub_2B94A80`, 71KB) and `getReductionCost()` (`sub_2B28940`). Understanding exactly how these call into TTI is essential for predicting when SLP will fire on a given kernel.

### getTreeCost() (`sub_2B94A80`)

This 71KB function walks every node in the SLP tree and accumulates the cost difference between the vectorized form and the original scalar form. For each tree node, it:

1. Calls `sub_2B889C0` (45KB, the inner cost computation) which dispatches to TTI via `sub_B91420` (`TTI::getInstructionCost()` at the IR level) --- called approximately 7 times per instruction to query costs for the scalar original, the vector alternative, and scalarization overhead (insert/extract elements).
2. For load/store nodes, queries the memory cost model which returns favorable costs for adjacent accesses (reflecting `ld.v2` / `ld.v4` coalescing benefit) and high costs for gather/scatter patterns.
3. For shuffle nodes (operand reordering), queries `TTI::getShuffleCost()` which on NVPTX returns high cost for any non-identity shuffle --- GPU has no native shuffle-within-register instruction for packed 32-bit values.
4. Returns a pair: `(vectorCost : i64, isExact : i32)`. When `isExact == 1`, the cost is a precise measurement from the scheduling model; the profitability check accepts it unconditionally regardless of the threshold.

### getReductionCost() (`sub_2B28940`)

Called with the TTI pointer (`a4`) as the second parameter, this function computes the cost of the horizontal reduction itself --- the shuffle-and-reduce tree that turns a vector into a scalar. Parameters:

```c
sub_2B28940(
    a1,     // HorizontalReduction object
    a4,     // TargetTransformInfo*
    v478,   // operand window start
    v479,   // operand window end
    v432,   // hasExternalUses flag
    v433,   // common opcode mask from Phase 1
    a2      // BoUpSLP tree
)
// Returns: (reductionCost : i64, costKind : i32)
```

The reduction cost on NVPTX is typically high because the GPU has no native horizontal reduction instruction for arbitrary vector widths. A `<4 x float>` fadd reduction requires 2 shuffle-and-add steps (log2(4) = 2), each involving an `extractelement` and a scalar `fadd`. The TTI cost model at `sub_20E14F0` (33KB) provides the per-step latency from the scheduling model.

### Combined Profitability Decision

```c
// Profitability check (decompiled from sub_2BD1C50, lines 2062-2163)
int64_t treeCost   = sub_2B94A80(tree, ...);   // vector tree cost
int64_t reducCost  = sub_2B28940(rd, TTI, ...); // reduction overhead
int64_t combined   = treeCost + reducCost;      // overflow-checked via __OFADD__

int64_t threshold  = -(int64_t)qword_5010428;  // SLPCostThreshold, default 0

if (costKind == 1) {
    // Exact cost from scheduling model: always accept
    goto vectorize;
}
if (combined > threshold) {
    // Not profitable: emit "HorSLPNotBeneficial" diagnostic
    // Try smaller VFs via getNextLegalVF() loop
    goto try_smaller_vf;
}
// Profitable: proceed to vectorizeTree()
```

The `costKind == 1` fast path is notable: when the cost model can determine the exact scheduling benefit (rather than a heuristic estimate), it bypasses the threshold entirely. This typically fires for small, fully-analyzable SLP trees where every instruction's latency is known from the TTI scheduling tables at `TTI+56`.

### VF Stepping on Failure

When vectorization at the current VF is unprofitable, the horizontal reduction path does not immediately give up. Instead, it calls `sub_2B1E190` (`getNextLegalVF`) to step down to the next smaller legal VF, then re-tries the entire build-tree / get-cost cycle:

```c
// VF step-down loop (decompiled from sub_2BD1C50, lines 2097-2163)
while (currentVF > minVF) {
    currentVF = sub_2B1E190(TTI, elementType, currentVF);
    if (sub_2B3C060(&memoSet, {offset, currentVF}))  // alreadyTried?
        continue;
    // Re-try vectorization at new VF
    sub_2BAACB0(tree, ops, currentVF, ...);  // buildTree
    treeCost  = sub_2B94A80(tree, ...);       // getTreeCost
    reducCost = sub_2B28940(rd, TTI, ...);    // getReductionCost
    combined  = treeCost + reducCost;
    if (combined <= threshold)
        goto vectorize;
}
// All VFs exhausted: emit "HorSLPNotBeneficial"
```

The memoization set (`sub_2B3C060`) prevents re-evaluating the same `(offset, VF)` pair, which is essential because the VF step-down loop can iterate many times for large operand counts.

## Configuration Knobs

### Upstream LLVM Knobs (present in CICC)

| Knob | Type | LLVM Default | CICC Default | Effect |
|---|---|---|---|---|
| `slp-threshold` | int | 0 | **0** | Profitability threshold. Vectorize when `cost <= -threshold`. Default 0 means any non-positive cost is profitable. |
| `slp-vectorize-hor` | bool | true | **true** | Enable horizontal reduction vectorization. |
| `slp-vectorize-hor-store` | bool | false | **false** | Seed horizontal reduction from stores. |
| `slp-max-reg-size` | int | 128 | **128** | Maximum vector register size in bits for SLP scheduling. |
| `slp-min-reg-size` | int | 128 | **128** | Minimum vector register size. |
| `slp-schedule-budget` | int | 100000 | **100000** | Maximum scheduling region size per block. |
| `slp-recursion-max-depth` | int | 12 | **12** | Maximum recursion depth for tree building. |
| `slp-min-tree-size` | int | 3 | **3** | Minimum tree size for full vectorization. |
| `vectorize-slp` | bool | true | **true** | Master switch for the SLP pass. |
| `view-slp-tree` | bool | false | **false** | Display SLP trees with Graphviz (debug). |
| `slp-max-vf` | int | 0 | **0** | Maximum vector factor override (0 = unlimited). |

### NVIDIA-Specific Globals

| Global | Address | Default | Effect |
|---|---|---|---|
| `SLPMaxVF` | `qword_500F628` | **0** | When zero: minimum VF = 4 elements. When non-zero: minimum VF = 3, and the value caps the maximum VF. Also bypasses power-of-2 VF requirement. |
| `SLPCostThreshold` | `qword_5010428` | **0** | Cost threshold for horizontal reduction profitability. Test is `cost > -(int)threshold`. Default 0: any non-positive cost is profitable. |
| Straight-line max VF | `qword_500FEE8` | unknown | Maximum VF override for straight-line SLP (`sub_2BCE070`), separate from horizontal reduction. |

### Key Behavioral Differences from Upstream

1. **Minimum VF default.** When `SLPMaxVF` is zero (default), CICC requires at least 4 scalar operands to attempt horizontal reduction vectorization. Upstream LLVM has no such global minimum; it relies on `slp-min-tree-size` (default 3) instead.

2. **VF clamping.** CICC clamps VF to [128, 512] bits based on the `a2+840` scheduling width, then steps down via `getNextLegalVF()` (`sub_2B1E190`). Upstream computes VF from `TTI::getMaximumVF()` or `slp-max-reg-size` without the explicit bit-width clamping. The [128, 512] range allows VF=4 through VF=16 for `f32` types, whereas upstream NVPTX (32-bit register width) would produce VF=1.

3. **Paired fadd merging.** CICC merges consecutive `fadd` reduction bundles into wider bundles via `sub_2B3C030` / `sub_2B25EA0` / `sub_2B38BA0`. This is absent from upstream and is targeted at GPU warp-level reduction patterns. See the [dedicated section above](#paired-fadd-bundle-merging-nvidia-specific).

4. **Scheduling-width-driven VF (not register-width-driven).** The upstream SLP vectorizer derives VF from `TTI::getRegisterBitWidth(Vector)`. CICC stores a separate scheduling width at `a2+840` that reflects available register budget after accounting for live-in pressure. This decouples SLP VF from the register file width, allowing profitable vectorization even though `getRegisterBitWidth(Vector)` returns 32.

5. **`isTreeNotBeneficialForArch()`.** CICC adds a GPU-architecture-specific early rejection filter (`sub_2B2DA40`) that takes the SM reduction type as a parameter. This rejects tree shapes known to be unprofitable on the target SM variant (e.g., trees that would produce reduction patterns not supported by the SM's warp-level primitives).

6. **O-level gating.** SLP vectorization is gated by `tier != 1` in the pipeline assembler: it is disabled at O1 and enabled at O2 and O3. At O2/O3, the LoopVectorize/SLP parameter `width` is set to `tier` (2 at O2, 3 at O3), affecting the scheduling width multiplier. SM-architecture-dependent thresholds are resolved at runtime via the `a2+840` value.

7. **Non-power-of-2 VF support.** When `SLPMaxVF` (`qword_500F628`) is non-zero and `operand_count + 1` is a power of 2, `adjustVF()` (`sub_2B1FA70`) returns `operand_count` directly, enabling VFs like 3, 5, 7. Upstream LLVM requires power-of-2 VFs except in specific recent patches for fixed-length non-power-of-2 vectorization.

## Diagnostic Strings

| String | Function | Meaning |
|---|---|---|
| `"SLP vectorized with cost N"` | `sub_2BCE070` | Straight-line SLP succeeded |
| `"Cannot SLP vectorize list:"` | `sub_2BCE070` | Straight-line SLP failed legality/cost |
| `"Stores SLP vectorized with cost N"` | `sub_2BCA110` | Store-seeded SLP succeeded |
| `"HorSLPNotBeneficial"` | `sub_2BD1C50` | Horizontal reduction not profitable |
| `"Vectorizing horizontal reduction is possible but not beneficial with cost C and threshold T"` | `sub_2BD1C50` | Full rejection diagnostic with cost details |
| `"VectorizedHorizontalReduction"` / `"Vectorized horizontal reduction with cost C and with tree size N"` | `sub_2BD1C50` | Horizontal reduction succeeded |
| `"const.rdx"` | `sub_2B21B90` | Intermediate reduction variable name |
| `"rdx.shuf.l"`, `"rdx.shuf.r"` | (cluster `0x1BDDB00`) | Left/right reduction shuffle names |
| `"op.rdx"`, `"op.extra"` | (cluster `0x1BDDB00`) | Reduction operation and extra operation names |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `HorizontalReduction::tryToReduce()` — main horizontal reduction entry | `sub_2BD1C50` | 85 KB | — |
| Straight-line SLP vectorizer entry | `sub_2BCE070` | — | — |
| Store-SLP vectorizer entry | `sub_2BCA110` | — | — |
| `BoUpSLP::buildTree()` | `sub_2BAACB0` | — | — |
| `BoUpSLP::getTreeCost()` | `sub_2B94A80` | 71 KB | — |
| `BoUpSLP::vectorizeTree()` (codegen) | `sub_2BC6BE0` | 71 KB | — |
| `BoUpSLP::computeScheduleData()` | `sub_2BBDBE0` | 40 KB | — |
| `BoUpSLP::scheduleBlock()` | `sub_2BBFB60` | 71 KB | — |
| `BoUpSLP::optimizeGatherSequence()` | `sub_2BB3590` | — | — |
| `BoUpSLP::reorderInputsIfNecessary()` | `sub_2BB0460` | — | — |
| `BoUpSLP::buildExternalUses()` | `sub_2B4F3D0` | — | — |
| `getReductionCost()` | `sub_2B28940` | — | — |
| `createFinalReduction()` | `sub_2B21C80` | — | — |
| `createReductionOp()` (`"const.rdx"`) | `sub_2B21B90` | — | — |
| `buildReductionResult()` | `sub_2B2FE10` | — | — |
| `reduceTreeLevel()` | `sub_2B2F4A0` | — | — |
| `isReductionOp()` | `sub_2B0D8B0` | — | — |
| `isHomogeneous()` (all ops satisfy predicate) | `sub_2B0D880` | — | — |
| `canVectorize()` (legality check) | `sub_2B4B450` | — | — |
| `isTreeTinyAndNotFullyVectorizable()` | `sub_2B2DB00` | — | — |
| `isTreeNotBeneficialForArch()` | `sub_2B2DA40` | — | — |
| `adjustVF()` (vectorization factor selection) | `sub_2B1FA70` | — | — |
| `getNextLegalVF()` | `sub_2B1E190` | — | — |
| `getScalarTypeWidth()` | `sub_2B49BC0` | — | — |
| `hasVectorizableReductions()` | `sub_2B6E610` | — | — |
| `tryMergeFaddBundles()` (NVIDIA-specific) | `sub_2B3C030` | — | — |
| `validateMergedBundle()` (NVIDIA-specific) | `sub_2B25EA0` | — | — |
| `rewriteMergedBundle()` (NVIDIA-specific) | `sub_2B38BA0` | — | — |
| `perBundleVectorize()` | `sub_2B77B90` | — | — |
| `emitVectorizedReductionDiagnostic()` | `sub_2B44ED0` | — | — |
| `reorderForCanonical()` | `sub_2B33D00` | — | — |
| SLP tree scheduling | `sub_2BD7F70` | 46 KB | — |
| SLP tree cost computation | `sub_2B889C0` | 45 KB | — |
| SLP value rewriting (scalar-to-vector) | `sub_2BCFB90` | 44 KB | — |
| SLP node creation (tree construction) | `sub_2BCAEC0` | 42 KB | — |
| `deleteTree()` (cleanup on failure) | `sub_2B5C350` | — | — |
| `alreadyTried()` (VF memoization) | `sub_2B3C060` | — | — |
| `tryNextVF()` (advance or fail) | `sub_2B399C0` | — | — |
| `classifyReductionPair()` (per-bundle opcode pair extraction) | `sub_2B5F980` | — | — |
| `hasExternalUses()` (external use check for bundles) | `sub_2B27F10` | — | — |
| `getTargetInfo()` (TTI accessor) | `sub_BD5C60` | — | — |
| `initDominatorContext()` | `sub_D5F1F0` | — | — |
| `hashOperandSlice()` (operand slice hash for scheduling cache) | `sub_27B0000` | — | — |
| Extended opcode classifier (opcodes > 0x1C) | `sub_2B15E10` | — | — |
| `buildOperandOrder()` (commutative reorder table) | `sub_2B3D4E0` | — | — |
| `isInScheduledSet()` (scheduling membership test) | `sub_2B3D560` | — | — |
| Reduction use counter (per-operand) | `sub_2B54920` | — | — |
| `TTI::getRegisterBitWidth(Vector)` — returns 32 | `sub_DFE640` | — | — |
| `TTI::supportsScalableVectors()` — returns false | `sub_DFE610` | — | — |
| `TTI::getRegisterBitWidth(Scalar)` — returns 32 | `sub_DFB1B0` | — | — |
| `TTI::getInstructionCost()` (scheduling cost model) | `sub_20E14F0` | 33 KB | — |
| `TTI::getInstructionCost()` (IR-level variant) | `sub_B91420` | — | — |
| `TTI::hasAttribute(N)` (function attribute query) | `sub_B2D610` | — | — |

## Data Structure: HorizontalReduction Object

| Offset | Type | Field |
|---|---|---|
| +0 | `ReductionBundle*` | Array of reduction bundle structs |
| +8 | `u32` | Bundle count |
| +304 | `Value**` | Pointer to operand arrays (each bundle = 64 bytes) |
| +312 | `u32` | Operand array count |
| +384 | `void*` | Auxiliary dependency table |
| +392 | `void*` | useDef map (bit 0 = inline/external flag) |
| +400 | `void*` | useDef map pointer |
| +408 | `u32` | useDef map capacity |
| +1568 | `Value*` | Root function / reduction entry value |
| +1576 | `u32` | SM reduction type (arch-specific opcode) |
| +1580 | `u8` | Commutative flag |
| +1584 | `char*` | Output result array |
| +1592 | `u32` | Output result count |
| +1596 | `u32` | Output result capacity |
| +1600 | `char[16]` | Inline result storage |

## Cross-References

- [LoopVectorize & VPlan](./loop-vectorize.md) — loop-based vectorization, runs alongside SLP in the same pipeline step
- [Loop Unrolling](./loop-unroll.md) — unrolling exposes more straight-line code for SLP
- [Pipeline & Ordering](./pipeline.md) — SLP placement at pipeline step 31
- [GVN](./gvn.md) — runs after SLP to clean up redundancies introduced by vectorization
- [Optimization Levels](../config/optimization-levels.md) — SLP enabled at tier 2+; width parameter varies by tier
- [NVPTX Target Infrastructure](../infra/nvptx-target.md) — TTI hook return values that drive SLP VF selection and cost model
- [Type Legalization](./type-legalization.md) — vector split/scalarize rules that constrain SLP output legality
- [SelectionDAG & NVPTX Lowering](./selectiondag.md) — custom lowering of SLP-produced vector loads/stores to `ld.vN`/`st.vN`
- [GPU Execution Model](../gpu-execution-model.md) — memory coalescing requirements that motivate SLP on GPU
