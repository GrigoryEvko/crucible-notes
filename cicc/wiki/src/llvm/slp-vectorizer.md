# SLP Vectorizer

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

The SLP vectorizer consults TargetTransformInfo at several decision points. NVIDIA's proprietary TTI implementation differs significantly from the upstream open-source NVPTX backend:

**Upstream open-source NVPTX TTI** (for reference):
- `getRegisterBitWidth()` returns **32 bits** --- deliberately small, with a comment: "Only `<2 x half>` should be vectorized."
- `getMinVectorRegisterBitWidth()` returns **32 bits**.
- `getNumberOfRegisters()` returns **1** for all classes, with a FIXME noting this is conservative.
- `getArithmeticInstrCost()` returns 2x base cost for `i64` operations (ADD, MUL, XOR, OR, AND), otherwise delegates to the base implementation.

**CICC v13.0 proprietary TTI** (from binary analysis):
- The SLP tree object at `a2+840` stores a **max register pressure / scheduling width** that drives VF selection. The decompiled code clamps VF to the range **[128, 512]** bits based on this value, far wider than the 32-bit upstream default.
- `sub_DFE640` implements `getRegisterBitWidth(vector)`, queried during pipeline setup.
- `sub_2B49BC0` implements `getScalarTypeWidth()`, returning the bit width of a scalar type for VF computation.
- `sub_2B1E190` implements `getNextLegalVF(TTI, type, vf)` --- steps down through legal vector factors when the current VF is unprofitable.

The critical implication: CICC's TTI reports much wider vector capability than upstream, enabling SLP to produce `v2` and `v4` bundles of 32-bit and 64-bit types. The upstream open-source backend's 32-bit register width essentially limits SLP to `<2 x half>` only.

## Maximum Vector Width on NVPTX

PTX supports vector types up to `.v4` for most data types, but the actual hardware constraint is tighter:

- **v2**: Supported for all types (`.b8` through `.b64`, `.f16`, `.f32`, `.f64`). This is the sweet spot for SLP.
- **v4**: Supported for `.b8`, `.b16`, `.b32`, `.f16`, `.f32`. NOT supported for `.b64`/`.f64`.
- **v8/v16**: Not supported in PTX at all. CPU-style AVX-width vectorization is never legal.

The SLP vectorizer's VF selection logic at `sub_2BD1C50` lines 1354--1578 computes:

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

## Algorithm Overview

CICC's SLP vectorizer has three entry points that collectively implement the upstream `BoUpSLP` / `SLPVectorizerPass`:

### Straight-Line SLP (`sub_2BCE070`)

Scans each basic block for groups of isomorphic instructions (same opcode, adjacent or compatible operands). Builds a bottom-up SLP tree using `sub_2BAACB0` (`buildTree`), evaluates cost via `sub_2B94A80` (`getTreeCost`), and emits vector code via `sub_2BC6BE0` (`vectorizeTree`) when profitable. Diagnostic: `"SLP vectorized with cost N"` on success, `"Cannot SLP vectorize list:"` on failure.

### Store-SLP (`sub_2BCA110`)

Seeds the SLP tree from consecutive stores to adjacent memory addresses. This is the primary entry point for memory coalescing. Diagnostic: `"Stores SLP vectorized with cost N"`.

### Horizontal Reduction SLP (`sub_2BD1C50`)

The most complex path. Handles horizontal reductions (e.g., summing all elements of a vector). Proceeds in six phases:

**Phase 0 -- Scalar chain scan.** Reads the reduction operand array at `a1+304` (pointer) and `a1+312` (count). Each bundle entry is 64 bytes. Classifies operands by opcode: values <= `0x1C` are simple scalars (add/sub/mul/etc.), values > `0x1C` are complex (fcmp, icmp variants). Calls `sub_2B0D8B0` (`isReductionOp`) to validate each operation as a legal reduction (add, fadd, mul, fmul, and, or, xor, smin/smax/umin/umax, fmin/fmax).

**Phase 1 -- Hash table construction.** Builds two open-addressing hash tables. The "AllOps" table uses 32-byte entries with the hash function `((ptr >> 9) ^ (ptr >> 4)) & (capacity - 1)`, matching LLVM's `DenseMap` pointer hash. Sentinel values: `-4096` (empty), `-8192` (tombstone). Load factor: grow at 75%, compact when free slots drop below 12.5%.

**Phase 2 -- Bundle pair extraction.** Calls `sub_2B5F980` per bundle to classify reduction opcode pairs. When two consecutive bundles both contain `fadd` reductions (opcode 90), NVIDIA attempts a **paired fadd bundle merge** via `sub_2B3C030`/`sub_2B25EA0`/`sub_2B38BA0`. This is an NVIDIA-specific optimization for warp-level fadd reductions not present in upstream LLVM.

**Phase 3 -- Main vectorization loop.** For each bundle, builds candidate operand lists, selects a VF, and tries vectorization with progressively smaller VFs on failure. The VF trial loop uses memoization (`sub_2B3C060`) to avoid re-trying the same (offset, VF) pair. Key substeps: `canVectorize` (legality), `buildTree`, `isTreeTinyAndNotFullyVectorizable` / `isTreeNotBeneficialForArch` (early rejection), `scheduleBlock`, `getTreeCost` + `getReductionCost` (profitability).

**Phase 4 -- Final reduction codegen.** Produces the final horizontal reduction instruction via `sub_2B21C80` (`createFinalReduction`), chaining multiple entries with `sub_2B34820` when multiple sub-trees were vectorized.

**Phase 5 -- Multi-tree scheduling and cleanup.** Builds a multi-tree reduction schedule, iteratively calling `sub_2B2F4A0` (`reduceTreeLevel`) until a single root value remains, then `replaceAllUsesWith` + `eraseFromParent`.

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

2. **VF clamping.** CICC clamps VF to [128, 512] bits based on register pressure, then steps down via `getNextLegalVF()`. Upstream computes VF from `TTI::getMaximumVF()` or `slp-max-reg-size` without the explicit bit-width clamping.

3. **Paired fadd merging.** CICC merges consecutive `fadd` reduction bundles into wider bundles. This is absent from upstream and is targeted at GPU warp-level reduction patterns.

4. **Interaction with NVIDIA TTI.** CICC's proprietary TTI reports wider vector register capabilities than the open-source NVPTX backend (which reports 32 bits). This enables SLP to produce `v2` and `v4` bundles that the open-source backend would never attempt.

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

| Address | Size | Identity |
|---|---|---|
| `sub_2BD1C50` | 85 KB | `HorizontalReduction::tryToReduce()` -- main horizontal reduction entry |
| `sub_2BCE070` | -- | Straight-line SLP vectorizer entry |
| `sub_2BCA110` | -- | Store-SLP vectorizer entry |
| `sub_2BAACB0` | -- | `BoUpSLP::buildTree()` |
| `sub_2B94A80` | 71 KB | `BoUpSLP::getTreeCost()` |
| `sub_2BC6BE0` | 71 KB | `BoUpSLP::vectorizeTree()` (codegen) |
| `sub_2BBDBE0` | 40 KB | `BoUpSLP::computeScheduleData()` |
| `sub_2BBFB60` | 71 KB | `BoUpSLP::scheduleBlock()` |
| `sub_2BB3590` | -- | `BoUpSLP::optimizeGatherSequence()` |
| `sub_2BB0460` | -- | `BoUpSLP::reorderInputsIfNecessary()` |
| `sub_2B4F3D0` | -- | `BoUpSLP::buildExternalUses()` |
| `sub_2B28940` | -- | `getReductionCost()` |
| `sub_2B21C80` | -- | `createFinalReduction()` |
| `sub_2B21B90` | -- | `createReductionOp()` (`"const.rdx"`) |
| `sub_2B2FE10` | -- | `buildReductionResult()` |
| `sub_2B2F4A0` | -- | `reduceTreeLevel()` |
| `sub_2B0D8B0` | -- | `isReductionOp()` |
| `sub_2B0D880` | -- | `isHomogeneous()` (all ops satisfy predicate) |
| `sub_2B4B450` | -- | `canVectorize()` (legality check) |
| `sub_2B2DB00` | -- | `isTreeTinyAndNotFullyVectorizable()` |
| `sub_2B2DA40` | -- | `isTreeNotBeneficialForArch()` |
| `sub_2B1FA70` | -- | `adjustVF()` (vectorization factor selection) |
| `sub_2B1E190` | -- | `getNextLegalVF()` |
| `sub_2B49BC0` | -- | `getScalarTypeWidth()` |
| `sub_2B6E610` | -- | `hasVectorizableReductions()` |
| `sub_2B3C030` | -- | `tryMergeFaddBundles()` (NVIDIA-specific) |
| `sub_2B25EA0` | -- | `validateMergedBundle()` (NVIDIA-specific) |
| `sub_2B38BA0` | -- | `rewriteMergedBundle()` (NVIDIA-specific) |
| `sub_2B77B90` | -- | `perBundleVectorize()` |
| `sub_2B44ED0` | -- | `emitVectorizedReductionDiagnostic()` |
| `sub_2B33D00` | -- | `reorderForCanonical()` |
| `sub_2BD7F70` | 46 KB | SLP tree scheduling |
| `sub_2B889C0` | 45 KB | SLP tree cost computation |
| `sub_2BCFB90` | 44 KB | SLP value rewriting (scalar-to-vector) |
| `sub_2BCAEC0` | 42 KB | SLP node creation (tree construction) |
| `sub_2B5C350` | -- | `deleteTree()` (cleanup on failure) |
| `sub_2B3C060` | -- | `alreadyTried()` (VF memoization) |
| `sub_2B399C0` | -- | `tryNextVF()` (advance or fail) |

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

- [LoopVectorize & VPlan](./loop-vectorize.md) -- loop-based vectorization, runs alongside SLP in the same pipeline step
- [Loop Unrolling](./loop-unroll.md) -- unrolling exposes more straight-line code for SLP
- [Pipeline & Ordering](./pipeline.md) -- SLP placement at pipeline step 31
- [GVN](./gvn.md) -- runs after SLP to clean up redundancies introduced by vectorization
- [Optimization Levels](../config/optimization-levels.md) -- SLP enabled at tier 2+
