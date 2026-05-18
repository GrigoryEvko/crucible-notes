# Mode Pattern Verifiers

## Abstract

Mode-pattern verifiers sit between target-neutral layout algebra and architecture-specific atom lowering. They check LDSM/STSM modes, register fragment sizes, SMEM descriptor layouts, SM120 block-scaled mode parameters, swizzle legality, and TMA rank constraints. The checks are small individually but together they stop invalid atom shapes from reaching NVVM, where the original layout intent would be much harder to diagnose.

## LDSM and STSM Matrix

LDSM and STSM atoms accept only a finite set of shape, transpose, size-pattern,
and matrix-count combinations.

| Mode | Shape | `num_matrices` | Accepted size patterns | Transpose |
|---|---|---:|---|---|
| `.M88` | `8 x 8` | `1`, `2`, `4` | `u16` | no |
| `.MT88` | `8 x 8` | `1`, `2`, `4` | `u16` | yes |
| `.M816` | `8 x 16` | `1`, `2`, `4` | `u4to8`, `s4to8`, packed 4/6-bit to 8-bit modes | no |
| `.M832` | `8 x 32` | `1`, `2`, `4` | `u2to4`, `s2to4` | no |
| `.MT1616` | `16 x 16` | `1`, `2` | `u8`, packed 4/6-bit to 8-bit modes | yes |

```c
LogicalResult verify_ldsm_mode(LdsmMode mode,
                               LdsmSizePattern size,
                               int num_matrices,
                               bool transpose,
                               Shape result_shape) {
    require(num_matrices == 1 || num_matrices == 2 || num_matrices == 4);
    require(transpose == mode.requires_transpose);
    require(size in mode.accepted_sizes);
    require(result_shape.rank == 1);
    require(result_shape.dim(0) == expected_ldsm_extent(mode, num_matrices));
    return success();
}
```

For binary-compatible diagnostic tests, keep the exact legacy strings where the test suite expects them. For new user-facing documentation and errors, prefer clear corrected wording.

## Shared-Memory Matrix Movement

Load-side matrix atoms move shared memory into registers; store-side atoms move the other way. The verifier checks both memory spaces and the fragment shape.

```c
LogicalResult verify_matrix_space_copy(MatrixCopyOp op) {
    if (op.is_load) {
        require(op.src.memory_space == SHARED_MEMORY);
        require(op.dst.memory_space == REGISTER_MEMORY);
    } else {
        require(op.src.memory_space == REGISTER_MEMORY);
        require(op.dst.memory_space == SHARED_MEMORY);
    }

    require(fragment_shape_matches_mode(op.mode, op.result_shape));
    require(pointer_alignment_meets_atom_requirement(op.shared_operand));
    return success();
}
```

Register-space copy atoms additionally verify that the register count matches the layout cosize:

```c
LogicalResult verify_register_fragment(Layout layout, int register_count) {
    int expected_bits = 32 * cosize(layout);
    int actual_bits = 32 * register_count;
    require(actual_bits == expected_bits);
    return success();
}
```

## UMMA Canonical Layout Verifier

UMMA atoms require canonical `UMMA_MN` (matrix-major) or `UMMA_K` (k-major) layouts for their A and B operands. The UMMA layout verifier enforces those invariants on every `mma_atom` op before it can lower to PTX. Each gate emits a specific diagnostic, so a layout that survives this pass is structurally valid for the descriptor packer that runs immediately after.

The verifier takes four inputs: a `direction` that is either `UMMA_MN` or `UMMA_K`; an `elem_bits` width of 4, 8, 16, or 32; a `swz_triple` `(swz_mode, B, M)` read from the swizzled descriptor; and the `cute.Layout` being verified. Direction selects the canonical operand orientation, element width sets the expected K-extent, and the swizzle triple picks one of a small accepted set of bit-mask shapes. The layout may be a plain `Layout` or a `ComposedLayout` whose inner component is a swizzle — both forms walk uniformly once they pass the first gate.

Seven verbatim diagnostics fire from this verifier. Each is emitted at most once per verification; a failure stops further checking. The strings are part of the user-visible contract — reproducing them byte-for-byte is required for test suites that match diagnostics by string:

- `"unsupported swizzle, got "`
- `"Not a canonical UMMA_MN Layout: Expected K-size 256/sizeof_bits<T> or 512/sizeof_bits(T) in sparse gemm kernels."`
- `"Not a canonical UMMA_MN Layout: No flat offset mode"`
- `"Not a canonical UMMA_MN Layout: Expected stride failure."`
- `"Not a canonical UMMA_K Layout: Expected MN-size multiple of "`
- `"Not a canonical UMMA_K Layout: No flat offset mode"`
- `"Not a canonical UMMA_K Layout: Expected stride failure."`

The verifier walks the same shape extraction first, then forks on `direction` into a UMMA_MN branch and a UMMA_K branch. Each branch reads a per-element-size encoding table that maps `elem_bits` to two integers `(per_lane_count, stride_multiplier)` consumed by the rebuilt expected layout; the table also encodes the SM100 TMEM rule that element widths above 32 bits are rejected outright.

1. Entry: classify the swizzle triple. Accepted triples are `(0, 2, 5)` (no swizzle), `(2, 5, 2)` (128-byte swizzle), `(n, 4, 3)` for `n in {0..3}` (compact/canonical path), and `(2, 5, 2)` with `direction == UMMA_K`. Any other triple emits `"unsupported swizzle, got "` followed by the serialised swizzle.
2. Shape extraction: build a small vector of shape/stride pairs limited to 128 entries (the hard cap on tile dimensions UMMA_MN and UMMA_K accept).
3. Element-size decode: encode `elem_bits` through a 4-byte classification table into `per_lane_count` and `stride_multiplier`. The fp4 path produces `(4, 8)`; the default path produces `(8, computed)`; element widths outside the table land on an `undefined` stride and stop later steps from succeeding.
4. Direction split: `direction == 1` enters UMMA_MN; `direction == 0` enters UMMA_K; any other value is a bug.
5. UMMA_MN branch:
   a. Read the K-mode size; require `K_elements == 256/elem_bits` or `K_elements == 512/elem_bits` (the latter is the sparse-gemm path with doubled K). Failure emits `"Not a canonical UMMA_MN Layout: Expected K-size 256/sizeof_bits<T> or 512/sizeof_bits(T) in sparse gemm kernels."`.
   b. Synthesize the expected `(1-shape, stride_multiplier-stride) / (1-shape, per_lane_count-stride)` pair, build the flattened expected layout, and walk a 152-byte-per-slot work vector comparing it to the op's actual modes.
   c. Require every mode to have exactly 80 bytes of flat-mode storage. Failure emits `"Not a canonical UMMA_MN Layout: No flat offset mode"`.
   d. Verify each rebuilt mode's stride matches the `(stride_multiplier, per_lane_count)` pair from step 3. Failure emits `"Not a canonical UMMA_MN Layout: Expected stride failure."`.
6. UMMA_K branch:
   a. Read the MN-mode size; require `MN_size % per_lane_count == 0`. Failure emits `"Not a canonical UMMA_K Layout: Expected MN-size multiple of "` followed by the decimal value of `per_lane_count` and a terminating `"."`.
   b. Synthesize the expected `(1, per_lane_count) / (2, 1)` pair, walk the same 152-byte work vector, and require the 80-byte flat-mode condition. Failure emits `"Not a canonical UMMA_K Layout: No flat offset mode"`.
   c. Stride check on the rebuilt modes. Failure emits `"Not a canonical UMMA_K Layout: Expected stride failure."`.
7. On success, pack `(elem_class, k_size, mn_size)` as the verifier's result.

```c
LogicalResult verify_umma_canonical_layout(UmmaDirection direction,
                                           uint32_t elem_bits,
                                           SwizzleTriple swz,
                                           LayoutLike layout) {
    if (!is_accepted_swizzle(swz, direction)) {
        return emit("unsupported swizzle, got ") << serialize(swz);
    }

    ElementClass ec = decode_element_class(elem_bits);
    if (!ec.valid) {
        return failure();  // element width above 32 bits — caller diagnoses
    }

    if (direction == UMMA_MN) {
        uint64_t k_elements = product_of(shape_of_k_mode(layout));
        uint64_t expected_dense  = 256u / elem_bits;
        uint64_t expected_sparse = 512u / elem_bits;
        if (k_elements != expected_dense && k_elements != expected_sparse) {
            return emit("Not a canonical UMMA_MN Layout: Expected K-size "
                        "256/sizeof_bits<T> or 512/sizeof_bits(T) in sparse "
                        "gemm kernels.");
        }
        if (!has_flat_offset_mode(layout)) {
            return emit("Not a canonical UMMA_MN Layout: No flat offset mode");
        }
        if (!strides_match_expected(layout, ec)) {
            return emit("Not a canonical UMMA_MN Layout: Expected stride failure.");
        }
    } else /* UMMA_K */ {
        uint64_t mn_size = product_of(shape_of_mn_mode(layout));
        if (mn_size % ec.per_lane_count != 0) {
            return emit("Not a canonical UMMA_K Layout: Expected MN-size multiple of ")
                       << ec.per_lane_count << ".";
        }
        if (!has_flat_offset_mode(layout)) {
            return emit("Not a canonical UMMA_K Layout: No flat offset mode");
        }
        if (!strides_match_expected(layout, ec)) {
            return emit("Not a canonical UMMA_K Layout: Expected stride failure.");
        }
    }

    return success();
}
```

The accepted swizzle set is the small closed enumeration the descriptor packer can express in shared-memory descriptors. `(0, 2, 5)` is the no-swizzle case; `(2, 5, 2)` is the 128-byte swizzle; the `(n, 4, 3)` family with `n` in `{0, 1, 2, 3}` covers the 32-, 64-, and 128-byte interleaved variants whose choice depends on operand element width. Any other triple is rejected before any size check runs, keeping the diagnostic specific to the swizzle field rather than blaming a downstream size mismatch.

The 152-byte work-vector stride matches the dense per-mode record size used throughout this dialect: shape, stride, and a per-mode decoration word giving three slots per element. The sparse path doubles the K-extent budget (the 512-bit case in step 5a) but the verifier still walks the same 152-byte stride; the metadata operand is verified by a sibling pass once this layout walk succeeds.

A sister verifier runs the same algorithm for arbitrary layout shapes and is invoked by ops taking non-MMA layouts. The two verifiers share most of their bodies, but the MMA-side verifier is specialised for the MMA path with hard-coded `k_size` formulas keyed off `direction` and `elem_bits`. The split exists because callers that already know they have an MMA operand pay no dispatch cost, and the larger sibling only runs for layouts whose K-extent must be derived rather than computed.

## tcgen05.mma Kind-Word Verifier

The Blackwell `tcgen05.mma` op family packs several orthogonal attributes into a 9-bit kind word, and the verifier checks that the bits are mutually consistent before any lowering pass sees the op. The kind word carries the CTA-group selector, the scale-vector size, the scale-input-accumulator bit, the block-scale bit, and a 3-bit selector that picks one of seven concrete `mma_kind` enum values. A separate weight-stationary flag overlays bit 0 of the same word and is read as a 1-bit predicate (its `cta_group::1` requirement is enforced as a cross-field rule). The verifier walks the mutual-exclusion rules below and returns an NVPTX opcode index from the closed range 10521..10530 on success, so the lowering pass can branch directly on the result.

```c
typedef union Tcgen05MmaKind {
    uint32_t raw : 9;
    struct {
        uint32_t cta_group         : 2;   // bits 0-1: 0=reserved, 1=1-CTA, 2=2-CTA, 3=4-CTA
        uint32_t scale_vector_size : 2;   // bits 2-3: 0=1X (16), 1=2X (32), 2=4X (64), 3=reserved
        uint32_t scale_input_acc   : 1;   // bit 4: 1 = scale applied to accumulator
        uint32_t block_scale       : 1;   // bit 5: 1 = block-scaled (FP4/FP8 microscale)
        uint32_t mma_kind          : 3;   // bits 6-8: one of the seven enum values below
    };
} Tcgen05MmaKind;
```

The warp-specialized variant reuses bit 0 of the same word and is materialized by the lowering pass as a boolean predicate `ws = (raw & 1) != 0`. The two views are mutually exclusive at the encoding layer: a kind word with `ws == 1` always has `cta_group == 1` (single-CTA), so rule 4 below rejects every other `cta_group` value the moment the WS bit is set.

The `mma_kind` field picks one of seven enum values. Each implies a different element type and a different valid range for the rest of the kind word; the verifier uses it as the primary dispatch key for type-specific rules.

| Value | mma_kind | Notes |
|---|---|---|
| 0 | `mxf4nvf4` | NVFP4 with block-scale |
| 1 | `i8` | Signed 8-bit integer matmul |
| 2 | `mxf8f6f4` | OCP MX-FP8/FP6/FP4 microscale |
| 3 | `f16` | Half-precision float |
| 4 | `tf32` | TensorFloat-32 (8-exp, 10-mantissa) |
| 5 | `f8f6f4` | (alias of mxf8f6f4 for backward compat) |
| 7 | `mxf4` | OCP MX-FP4 (no NVFP4 distinction) |

The 13 verbatim diagnostics below fire in the order shown. Each rule is independent; the verifier walks them in fixed sequence and reports the first failure rather than collecting all violations, so a kind word that clears one rule is not yet globally valid until the whole ladder completes. The `"colletor"` typo in rule 10 is preserved verbatim — reproducing it byte-for-byte is required for test suites that match diagnostics by string.

> ⚡ **QUIRK — `colletor` typo + fail-first walk masks later violations**
> Rule 10's diagnostic spells the noun `colletor` (missing `c`) instead of `collector`, and the ladder bails on the first failure rather than collecting every violation. Two surprises compose: a kind word that fails rule 3 may *also* trip rules 7 and 10, but the user sees only the rule-3 message; iteratively patching one symptom at a time is the only debugging path. Combined with the typo, log scrapers that search for the corrected spelling silently miss every rule-10 hit even when the verifier fires.

| # | Diagnostic | Trigger condition |
|---:|---|---|
| 1 | `"INT8 type is supported only on arch-conditional variants."` | `mma_kind == i8` outside an arch-conditional / family-conditional variant |
| 2 | `"MXF4 and MXF4NVF4 types with Sparsity are supported only on arch-conditional variants."` | `mma_kind in {mxf4nvf4, mxf4}` with sparsity bit set, non-arch-conditional |
| 3 | `"Explicit scale vector size is supported only on arch-conditional variants."` | `scale_vector_size != 0` outside an arch-conditional variant |
| 4 | `"Scale input accumulator is not supported on this architecture."` | `scale_input_acc == 1` on an ISA strictly below SM100a |
| 5 | `"Scale input accumulator can only be used with f16 and tf32 types"` | `scale_input_acc == 1 && mma_kind not in {f16, tf32}` |
| 6 | `"Block scale is not supported for f16, tf32, f8f6f4, and i8 types"` | `block_scale == 1 && mma_kind in {i8, f16, tf32, f8f6f4}` |
| 7 | `"ashift is not supported with tcgen05.mma.block_scale variants"` | ashift bit set on a block-scale opcode (10521 / 10526) |
| 8 | `"cta_group::2 is not supported with weight stationary"` | `(raw & 3) == 3` — i.e. `cta_group == 2` selector with WS set |
| 9 | `"Cannot use weight stationary with mxf8f6f4 and fp4 types"` | `ws == 1 && mma_kind in {mxf8f6f4, f8f6f4, mxf4}` |
| 10 | `"Cannot use collector::a::use or colletor::a::fill with ashift"` | collector-a use/fill combined with ashift |
| 11 | `"Cannot use 2X or 4X as scale vector size for mxf8f6f4 type"` | `mma_kind == mxf8f6f4 && scale_vector_size > 1` |
| 12 | `"Cannot use 1X as scale vector size for mxf4nvf4 type"` | `mma_kind == mxf4nvf4 && scale_vector_size == 0` (1X) |
| 13 | `"Cannot use 1X or 4X as scale vector size for mxf4 type"` | `mma_kind == mxf4 && scale_vector_size in {0, 2}` |

Rules 1, 2, 3, and 4 are architecture gates: the corresponding type/scale combinations only exist as arch-conditional or family-conditional variants of `tcgen05.mma`. Rule 5 narrows the scale-input-accumulator option to the two floating types that actually support it. Rule 6 expresses the inverse: the block-scale microscale path is defined for the FP4 / FP6 / FP8 narrow types, not for FP16, TF32, the legacy `f8f6f4`, or INT8. Rules 8 and 9 fence the warp-specialized variant: `cta_group::2` and the wider `mxf8f6f4`/`f8f6f4`/`mxf4` selectors are not part of the WS dispatch table. Rules 11, 12, and 13 each pin a single type's `scale_vector_size` to the one encoding the corresponding NVPTX instruction supports.

```c
LogicalResult verify_tcgen05_mma_kind(Tcgen05MmaKind k,
                                      uint32_t collector,
                                      uint32_t opcode,
                                      bool is_arch_cond,
                                      uint32_t isa_version) {
    bool ws = (k.raw & 1) != 0;

    if (k.mma_kind == I8 && !is_arch_cond) {
        return emit("INT8 type is supported only on arch-conditional variants.");
    }
    if ((k.mma_kind == MXF4NVF4 || k.mma_kind == MXF4)
        && sparsity_bit(k) && !is_arch_cond) {
        return emit("MXF4 and MXF4NVF4 types with Sparsity are "
                    "supported only on arch-conditional variants.");
    }
    if (k.scale_vector_size != 0 && !is_arch_cond) {
        return emit("Explicit scale vector size is supported only on "
                    "arch-conditional variants.");
    }
    if (k.scale_input_acc != 0 && isa_version < SM100A) {
        return emit("Scale input accumulator is not supported on this architecture.");
    }
    if (k.scale_input_acc != 0
        && k.mma_kind != F16 && k.mma_kind != TF32) {
        return emit("Scale input accumulator can only be used with f16 and tf32 types");
    }
    if (k.block_scale != 0
        && (k.mma_kind == I8 || k.mma_kind == F16
         || k.mma_kind == TF32 || k.mma_kind == F8F6F4)) {
        return emit("Block scale is not supported for f16, tf32, f8f6f4, and i8 types");
    }
    if (is_block_scale_opcode(opcode) && (collector & ASHIFT) != 0) {
        return emit("ashift is not supported with tcgen05.mma.block_scale variants");
    }
    if ((k.raw & 3) == 3) {
        return emit("cta_group::2 is not supported with weight stationary");
    }
    if (ws && (k.mma_kind == MXF8F6F4 || k.mma_kind == F8F6F4 || k.mma_kind == MXF4)) {
        return emit("Cannot use weight stationary with mxf8f6f4 and fp4 types");
    }
    if ((collector & COLLECTOR_A_USE_OR_FILL) != 0 && (collector & ASHIFT) != 0) {
        return emit("Cannot use collector::a::use or colletor::a::fill with ashift");
    }
    if (k.mma_kind == MXF8F6F4 && k.scale_vector_size > 1) {
        return emit("Cannot use 2X or 4X as scale vector size for mxf8f6f4 type");
    }
    if (k.mma_kind == MXF4NVF4 && k.scale_vector_size == 0) {
        return emit("Cannot use 1X as scale vector size for mxf4nvf4 type");
    }
    if (k.mma_kind == MXF4 && (k.scale_vector_size == 0 || k.scale_vector_size == 2)) {
        return emit("Cannot use 1X or 4X as scale vector size for mxf4 type");
    }

    return select_tcgen05_opcode(k);   // returns one of 10521..10530
}
```

On success the verifier hands back an opcode index in the closed range 10521..10530. Each of the ten NVPTX MI opcodes — `tcgen05.mma`, `tcgen05.mma.sp`, `tcgen05.mma.block_scale`, `tcgen05.mma.sp.block_scale`, and their warp-specialized siblings — corresponds to exactly one combination of `cta_group`, weight-stationary, sparsity, and block-scale bits the lowering pass needs to pick a final instruction encoding. Returning the index from the verifier keeps the kind-word decode in one place and prevents the lowering pass from rederiving the dispatch table from raw bits.

### Worked Example: Kind Word `0x42`

A concrete kind word makes the bit packing and the ladder order easier to follow. Take `Tcgen05MmaKind.raw = 0x42`. In 9-bit binary, with bit 0 on the right, this is

```text
bit:   8 7 6   5 4   3 2   1 0
raw:   0 0 1   0 0   0 0   1 0   = 0x42
```

Reading the fields out of the bitfield declared above:

| Field | Bits | Value | Decoded |
|---|---|---|---|
| `cta_group` | 0-1 | `10` | 2 — `cta_group::2` (two-CTA dispatch) |
| `scale_vector_size` | 2-3 | `00` | 0 — 1X (16-element scale vector) |
| `scale_input_acc` | 4 | `0` | not set |
| `block_scale` | 5 | `0` | not set |
| `mma_kind` | 6-8 | `001` | 1 — `i8` |

The overlaid weight-stationary predicate is `ws = (raw & 1) != 0` — for `0x42` bit 0 is `0`, so `ws = false`. Sparsity bit `(raw & 0x20)` is also `0` — the sparsity bit overlays bit 5 of the encoding the way the bitfield's `block_scale` does, and reads zero here.

Walking the verifier ladder against this kind word, with `is_arch_cond = false` and `isa_version = SM100` (not the arch-conditional variant):

1. **Rule 1** — `k.mma_kind == I8 && !is_arch_cond`. Both predicates hold. The verifier fires `"INT8 type is supported only on arch-conditional variants."` and stops. No later rule runs.

Lifting the gate by setting `is_arch_cond = true` lets the kind word continue down the ladder. Rules 2 and 3 short-circuit (`mma_kind != mxf4nvf4/mxf4`, `scale_vector_size == 0`). Rule 4 short-circuits (`scale_input_acc == 0`). Rule 5 short-circuits for the same reason. Rule 6 short-circuits (`block_scale == 0`). Rule 7 short-circuits (no block-scale opcode in play). Rule 8 checks `(raw & 3) == 3` — for `0x42`, `raw & 3 = 2`, so the rule does not fire. Rule 9 reads the weight-stationary view, finds `ws = 0`, and short-circuits. Rules 10-13 all short-circuit on the same field-clear conditions. The ladder reaches `select_tcgen05_opcode`, which picks `tcgen05.mma` (opcode 10522, the dense, non-block-scale, non-WS path) on `cta_group::2`.

A symmetric example flips the gate the other direction. Take `raw = 0xE2` (`0b011100010`):

| Field | Bits | Value | Decoded |
|---|---|---|---|
| `cta_group` | 0-1 | `10` | 2 |
| `scale_vector_size` | 2-3 | `00` | 0 |
| `scale_input_acc` | 4 | `0` | not set |
| `block_scale` | 5 | `1` | set |
| `mma_kind` | 6-8 | `011` | 3 — `f16` |

The ladder walks rules 1-5 without firing (`mma_kind` is neither `i8` nor `mxf4*`, `scale_vector_size == 0`, `scale_input_acc == 0`). Rule 6 sees `block_scale == 1 && mma_kind in {i8, f16, tf32, f8f6f4}` — `mma_kind == f16` matches the set and the verifier fires `"Block scale is not supported for f16, tf32, f8f6f4, and i8 types"`.

Two takeaways follow from the worked examples. First, the bit packing is order-sensitive: `cta_group` sits in the low two bits, `mma_kind` in the high three, with single-bit predicates between them — a writer that confuses bit order silently changes the dispatched opcode. Second, the ladder is fail-first: once any rule fires the verifier stops, so a kind word that passes rule 6 has *not* been proven globally valid until every later rule clears too. The 13-rule sequence is the complete witness.

## SM120 Block-Scaled Lattice

SM120 block-scaled MMA verifies shape, input type, scale-factor type, scale-vector size, and scale-fragment width as one combined gate.

```c
LogicalResult verify_sm120_scale_lattice(Sm120ScaleParams p) {
    require(p.scale_vector_size == 16 || p.scale_vector_size == 32);
    require(p.k == 32 || p.k == 64);

    if (p.k == 32) {
        require(is_fp4_fp6_or_fp8(p.a_type));
        require(is_fp4_fp6_or_fp8(p.b_type));
        require(p.sf_type == e8m0_type());
        require(p.scale_vector_size == 32);
        require(p.scale_fragment_bits == 8);
        return success();
    }

    require(p.a_type == fp4_e2m1_type());
    require(p.b_type == fp4_e2m1_type());
    require(p.scale_fragment_bits * p.scale_vector_size == 512);
    return success();
}
```

The `K = 64` row deliberately narrows the accepted input set. Do not reuse the `K = 32` FP6/FP8 allow-list there.

## Swizzle Legality

`apply_swizzle` and `add_offset` do not commute freely. The verifier rejects rewrites that assume:

```text
add_offset(apply_swizzle(x), k) == apply_swizzle(add_offset(x, k))
```

unless the selected swizzle is identity for the affected address bits.

```c
LogicalResult verify_swizzle_offset_commutation(Swizzle swizzle, Offset offset) {
    if (swizzle.is_identity()) {
        return success();
    }

    require(offset_preserves_swizzle_partition(swizzle, offset));
    return success();
}
```

Accepted swizzle modes are a closed target-aware enum. Unknown modes must not silently fold to identity after parsing.

## TMA Rank and Mode Gates

TMA bulk tensor operations support ranks one through five. Im2col and scatter variants tighten the rank requirements, and some modes are Blackwell-only.

```c
LogicalResult verify_tma_rank_and_mode(TmaMode mode, int rank, Target target) {
    require(1 <= rank && rank <= 5);

    if (mode == IM2COL || mode == IM2COL_W || mode == IM2COL_W128) {
        require(rank >= 3);
    }

    if (mode == SCATTER4) {
        require(rank == 2);
    }

    if (mode == IM2COL_W || mode == IM2COL_W128) {
        require(target.supports_blackwell_tma_modes);
    }

    return success();
}
```

## Invariants

- LDSM/STSM mode, transpose, size pattern, and matrix count are verified as one
  tuple.
- Shared-memory matrix movement checks memory-space direction and alignment.
- Register fragment size is derived from layout cosize.
- UMMA canonical layouts emit one of seven verbatim diagnostics on failure, keyed on direction and on flat-mode / stride structure.
- `tcgen05.mma` kind words are gated by 13 mutual-exclusion diagnostics over a 9-bit packed encoding plus a separate weight-stationary predicate.
- SM120 block-scaled validation distinguishes `K = 32` from `K = 64`.
- Swizzle and offset rewrites must prove commutation.
- TMA ranks and special modes are target-gated before PTX emission.

## Cross-References

[TMA Atoms — Eleven-Step Partition Verifier](tma-atoms.md#eleven-step-partition-verifier) documents the partition verifier whose eleven-step ladder these mode verifiers compose with. [SM Tier Roster and Copy Atom Registry — MMA Atom Verifier Diagnostics](sm-tier-roster-and-copy-atom-registry.md#mma-atom-verifier-diagnostics) lists the MMA atom verifier diagnostics that the layout walker emits before the canonical-layout check runs.

