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

UMMA atoms require canonical `UMMA_MN` (matrix-major) or `UMMA_K` (k-major) layouts for their A and B operands. `sub_167C690` (5046 B) enforces those invariants on every `mma_atom` op before it can lower to PTX. Each gate emits a specific diagnostic, so a layout that survives this pass is structurally valid for the descriptor packer that runs immediately after.

The verifier takes four inputs: a `direction` that is either `UMMA_MN` or `UMMA_K`; an `elem_bits` width of 4, 8, 16, or 32; a `swz_triple` `(B, M, S)` encoding the swizzle bit-mask pattern; and the `cute.Layout` being verified. Direction selects the canonical operand orientation, element width sets the expected K-extent, and the swizzle triple picks one of a small accepted set of bit-mask shapes. The layout may be a plain `Layout` or a `ComposedLayout` whose inner component is a swizzle — both forms walk uniformly once they pass the first gate.

The diagnostic ladder has seven failure messages, interleaved with the algorithm. Each fires at most once per verification, and a failure stops further checking:

- `"Not a canonical UMMA_MN/K Layout: layout must be a Layout or ComposedLayout"`
- `"Not a canonical UMMA_MN/K Layout: K-mode must be contiguous"`
- `"Not a canonical UMMA_MN/K Layout: too many modes (max 128)"`
- `"unsupported swizzle, got (B={B}, M={M}, S={S})"`
- `"Not a canonical UMMA_MN/K Layout: K-mode size mismatch (expected {k_size}, got {actual})"`
- `"Not a canonical UMMA_MN Layout: M-mode must follow K-mode contiguously"`
- `"Not a canonical UMMA_K Layout: K-mode must be the innermost mode"`

The eleven-step algorithm:

1. Check `layout.kind == ComposedLayout || layout.kind == Layout`. Else emit `"Not a canonical UMMA_MN/K Layout: layout must be a Layout or ComposedLayout"`.
2. Compute `k_size = direction == UMMA_K ? 512 / elem_bits : 256 / elem_bits`. This is the expected K-extent for the operand.
3. Check that the layout's innermost mode-K is contiguous (stride 1). Else emit `"Not a canonical UMMA_MN/K Layout: K-mode must be contiguous"`.
4. Check that the layout has at most 128 modes total. Else emit `"Not a canonical UMMA_MN/K Layout: too many modes (max 128)"`.
5. Verify the swizzle triple is in the accepted set: `(0, 2, 5)`, `(2, 5, 2)`, or `(n <= 3, 4, 3)` for `n` in `{1, 2, 3}`. Else emit `"unsupported swizzle, got (B={B}, M={M}, S={S})"`.
6. For sparse layouts, double `k_size` to account for the metadata stride.
7. Verify the K-mode size matches `k_size`. Else emit `"Not a canonical UMMA_MN/K Layout: K-mode size mismatch (expected {k_size}, got {actual})"`.
8. For `UMMA_MN`, verify M-mode is contiguous after the K-mode. Else emit `"Not a canonical UMMA_MN Layout: M-mode must follow K-mode contiguously"`.
9. For `UMMA_K`, verify K-mode comes first. Else emit `"Not a canonical UMMA_K Layout: K-mode must be the innermost mode"`.
10. Walk a 152-byte or 304-byte work-vector (stride depends on whether the layout has metadata) and verify each entry is well-formed.
11. If all gates pass, return success.

```c
LogicalResult verify_umma_canonical_layout(UmmaDirection direction,
                                           uint32_t elem_bits,
                                           SwizzleTriple swz,
                                           LayoutLike layout) {
    if (layout.kind != LAYOUT && layout.kind != COMPOSED_LAYOUT) {
        return emit("Not a canonical UMMA_MN/K Layout: "
                    "layout must be a Layout or ComposedLayout");
    }

    uint32_t k_size = (direction == UMMA_K) ? 512 / elem_bits : 256 / elem_bits;

    if (!innermost_k_is_contiguous(layout)) {
        return emit("Not a canonical UMMA_MN/K Layout: K-mode must be contiguous");
    }
    if (mode_count(layout) > 128) {
        return emit("Not a canonical UMMA_MN/K Layout: too many modes (max 128)");
    }
    if (!is_accepted_swizzle(swz)) {
        return emit("unsupported swizzle, got (B=%u, M=%u, S=%u)", swz.B, swz.M, swz.S);
    }

    if (layout_is_sparse(layout)) {
        k_size *= 2;
    }

    if (k_mode_size(layout) != k_size) {
        return emit("Not a canonical UMMA_MN/K Layout: K-mode size mismatch "
                    "(expected %u, got %u)", k_size, k_mode_size(layout));
    }

    if (direction == UMMA_MN && !m_mode_follows_k_contiguously(layout)) {
        return emit("Not a canonical UMMA_MN Layout: "
                    "M-mode must follow K-mode contiguously");
    }
    if (direction == UMMA_K && !k_mode_is_innermost(layout)) {
        return emit("Not a canonical UMMA_K Layout: K-mode must be the innermost mode");
    }

    uint32_t stride = layout_is_sparse(layout) ? 304 : 152;
    return walk_work_vector(layout, stride);
}
```

Step 5's accepted swizzle set is the small closed enumeration the descriptor packer can express in shared-memory descriptors. `(0, 2, 5)` is the no-swizzle case; `(2, 5, 2)` is the 128-byte swizzle; the `(n, 4, 3)` family with `n` in `{1, 2, 3}` covers the 32-, 64-, and 128-byte interleaved variants whose choice depends on operand element width. Any other triple is rejected before any size check runs, keeping the diagnostic specific to the swizzle field rather than blaming a downstream size mismatch.

Step 10's work-vector walk picks one of two strides based on sparsity. A dense layout carries three slots per element — shape, stride, and a decoration word recording the per-mode flags consumed by later passes — giving a 152-byte stride. A sparse layout carries six slots: the dense triple plus a metadata-shape, metadata-stride, and metadata-decoration triple describing the sparsity-metadata operand parallel to the value operand, giving a 304-byte stride. The walk checks each entry against that schema; any malformed entry counts as a generic structural failure and gets reported through the K-mode size or contiguity gates rather than as a new diagnostic.

A sister verifier `sub_13F24D0` (11515 B) runs the same algorithm for arbitrary layout shapes and is invoked by ops taking non-MMA layouts. The two share most of their bodies, but `sub_167C690` is specialised for the MMA path with hard-coded `k_size` formulas keyed off `direction` and `elem_bits`. The split exists because callers that already know they have an MMA operand pay no dispatch cost, and the larger sibling only runs for layouts whose K-extent must be derived rather than computed.

## tcgen05.mma Kind-Word Verifier

The Blackwell `tcgen05.mma` op family packs several orthogonal attributes into a 7-bit kind word, and `sub_1AD26A0` (5154 B) checks that the bits are mutually consistent before any lowering pass sees the op. The kind word carries the warp-specialized flag, the CTA-group selector, the scale-vector size, the input-accumulator scale bit, the block-scale bit, and a one-bit selector that picks one of seven concrete `mma_kind` enum values. The verifier walks 13 mutual-exclusion rules over those fields and returns one of ten NVPTX opcode indices on success, so the lowering pass can branch directly on the result.

```c
typedef union Tcgen05MmaKind {
    uint8_t raw : 7;
    struct {
        uint8_t ws                : 1;   // bit 0: warp-specialized variant
        uint8_t cta_group         : 1;   // bit 1: 1 = single-CTA, 0 = cooperative pair
        uint8_t scale_vector_size : 2;   // bits 2-3: 0=16, 1=32, 2=64, 3=reserved
        uint8_t scale_input_acc   : 1;   // bit 4: 1 = scale applied to accumulator
        uint8_t block_scale       : 1;   // bit 5: 1 = block-scaled (FP4/FP8 microscale)
        uint8_t mma_kind          : 1;   // bit 6: 1 = one of the seven mma_kind enum values below
    };
} Tcgen05MmaKind;
```

The `mma_kind` field picks one of seven enum values. Each implies a different element type and a different valid range for the rest of the kind word; the verifier uses it as the primary dispatch key for type-specific rules.

| Value | mma_kind | Notes |
|---|---|---|
| 0 | `mxf4nvf4` | NVFP4 with block-scale |
| 1 | `i8` | Signed 8-bit integer matmul |
| 2 | `mxf8f6f4` | OCP MX-FP8/FP6/FP4 microscale |
| 3 | `f16` | Half-precision float |
| 4 | `tf32` | TensorFloat-32 (8-exp, 10-mantissa) |
| 5 | `f8f6f4` | (alias of mxf8f6f4 for backward compat) |
| 6 | `mxf4` | OCP MX-FP4 (no NVFP4 distinction) |

The 13 verbatim diagnostics below fire in the order shown. Each rule is independent; the verifier walks them in fixed sequence and reports the first failure rather than collecting all violations, so a kind word that clears one rule is not yet globally valid until the whole ladder completes. The `"colletor"` typo in rule 7 is preserved verbatim from the binary — reproducing it byte-for-byte is required for test suites that match diagnostics by string.

| # | Diagnostic | Trigger condition |
|---:|---|---|
| 1 | `"INT8 mma cannot use block-scale"` | `mma_kind == i8 && block_scale != 0` |
| 2 | `"MXF4 mma scale_vector_size must be 16"` | `mma_kind == mxf4 && scale_vector_size != 0` |
| 3 | `"NVFP4 mma scale_vector_size must be 32"` | `mma_kind == mxf4nvf4 && scale_vector_size != 1` |
| 4 | `"WS variant requires cta_group::1"` | `ws == 1 && cta_group != 1` |
| 5 | `"WS variant cannot use mxf8f6f4"` | `ws == 1 && mma_kind == mxf8f6f4` |
| 6 | `"cta_group::2 + WS conflict"` | `cta_group == 0 && ws == 1` |
| 7 | `"colletor::a::use requires scale_input_acc=0"` | accumulator-collector use with scale-input-acc set |
| 8 | `"FP16 mma cannot have block_scale=1"` | `mma_kind == f16 && block_scale != 0` |
| 9 | `"TF32 mma cannot have block_scale=1"` | `mma_kind == tf32 && block_scale != 0` |
| 10 | `"scale_vector_size==3 is reserved"` | `scale_vector_size == 3` |
| 11 | `"i8 mma cannot have scale_input_acc=1"` | `mma_kind == i8 && scale_input_acc != 0` |
| 12 | `"WS variant requires accumulator type Float32"` | `ws == 1 && c.type != Float32` |
| 13 | `"tcgen05.mma supported only on arch-conditional or family-conditional variants from SM100 onwards."` | SM gate; `cc < 0xA0` |

Rules 4, 5, and 6 form an interlocked set the warp-specialized variant must clear together: WS demands `cta_group::1` (single-CTA mode), refuses `mxf8f6f4` because the OCP microscale path is not wired into the WS dispatch tables, and rejects the cooperative `cta_group::2` selector outright. Rules 1, 2, 3, 8, and 9 enforce per-type constraints on the scale fields: INT8 has no microscale path; `mxf4` and `mxf4nvf4` each pin `scale_vector_size` to a specific encoded value because the underlying NVPTX instruction has only one legal scale-vector layout per variant; FP16 and TF32 do not participate in block-scale at all. Rule 10 reserves `scale_vector_size == 3` for future encodings. Rules 7, 11, and 12 are operand-level: accumulator-collector use is incompatible with scaling the input accumulator, INT8 cannot scale into the accumulator, and the WS variant requires an FP32 accumulator. Rule 13 is the architecture gate — pre-SM100 compute capabilities reject the entire op before any field check runs.

```c
LogicalResult verify_tcgen05_mma_kind(Tcgen05MmaKind k,
                                      Type accumulator_type,
                                      uint32_t cc) {
    if (cc < 0xA0) {
        return emit("tcgen05.mma supported only on arch-conditional or "
                    "family-conditional variants from SM100 onwards.");
    }

    if (k.mma_kind == I8 && k.block_scale != 0) {
        return emit("INT8 mma cannot use block-scale");
    }
    if (k.mma_kind == MXF4 && k.scale_vector_size != 0) {
        return emit("MXF4 mma scale_vector_size must be 16");
    }
    if (k.mma_kind == MXF4NVF4 && k.scale_vector_size != 1) {
        return emit("NVFP4 mma scale_vector_size must be 32");
    }
    if (k.ws == 1 && k.cta_group != 1) {
        return emit("WS variant requires cta_group::1");
    }
    if (k.ws == 1 && k.mma_kind == MXF8F6F4) {
        return emit("WS variant cannot use mxf8f6f4");
    }
    if (k.cta_group == 0 && k.ws == 1) {
        return emit("cta_group::2 + WS conflict");
    }
    if (uses_accumulator_collector(k) && k.scale_input_acc != 0) {
        return emit("colletor::a::use requires scale_input_acc=0");
    }
    if (k.mma_kind == F16 && k.block_scale != 0) {
        return emit("FP16 mma cannot have block_scale=1");
    }
    if (k.mma_kind == TF32 && k.block_scale != 0) {
        return emit("TF32 mma cannot have block_scale=1");
    }
    if (k.scale_vector_size == 3) {
        return emit("scale_vector_size==3 is reserved");
    }
    if (k.mma_kind == I8 && k.scale_input_acc != 0) {
        return emit("i8 mma cannot have scale_input_acc=1");
    }
    if (k.ws == 1 && accumulator_type != Float32) {
        return emit("WS variant requires accumulator type Float32");
    }

    return select_tcgen05_opcode(k);   // returns one of 10521..10530
}
```

On success the verifier hands back an opcode index in the closed range 10521..10530. Each of the ten NVPTX MI opcodes — `MMA_TCGEN05_SHARED_DENSE`, `MMA_TCGEN05_SHARED_SPARSE`, and the eight sibling variants switching on the dense/sparse and operand-source axes — corresponds to exactly one combination of `cta_group`, `ws`, and operand-source bits the lowering pass needs to pick a final instruction encoding. Returning the index from the verifier keeps the kind-word decode in one place and prevents the lowering pass from rederiving the dispatch table from raw bits.

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
- UMMA canonical layouts are gated by `sub_167C690` with seven diagnostics over an eleven-step algorithm.
- `tcgen05.mma` kind words are gated by `sub_1AD26A0` with 13 mutual-exclusion diagnostics over a 7-bit packed encoding.
- SM120 block-scaled validation distinguishes `K = 32` from `K = 64`.
- Swizzle and offset rewrites must prove commutation.
- TMA ranks and special modes are target-gated before PTX emission.

## Reimplementation Checklist

1. Encode the LDSM/STSM acceptance matrix as data and test every row.
2. Share load/store matrix-copy memory-space checks with reversed direction.
3. Reuse the UMMA canonical-layout verifier across `UMMA_MN` and `UMMA_K` with a single `direction`-keyed `k_size` formula.
4. Decode the `tcgen05.mma` kind word as a packed 7-bit union and walk the 13-rule ladder in fixed order, returning the NVPTX opcode index from the verifier rather than rederiving it downstream.
5. Keep SM120 scale validation table-driven and shape-aware.
6. Reject unknown swizzle modes at parse or verify time.
7. Verify TMA rank and mode before cp.async bulk lowering.
