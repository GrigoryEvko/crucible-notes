# MMA Atoms SM70-SM120

## Abstract

`cute_nvgpu` MMA atoms describe every NVIDIA matrix multiply-accumulate family from classic register MMA through Hopper WGMMA and Blackwell UMMA. Each atom records target tier, tile shape, operand element types, operand residency, sparsity, block scaling, and descriptor requirements. The compiler verifies layout legality against the atom and picks the correct NVGPU/NVVM lowering — all without losing the higher-level tile algebra.

## Cross-Tier Summary

| Tier | Instruction family | Operand residency | Main element families |
|---|---|---|---|
| SM70/SM75 | Legacy `mma.sync` forms | Register fragments | `f16`, `bf16`, `f32` accumulators. |
| SM80 | Dense and sparse `mma.sync` | Register fragments | `f16`, `bf16`, `tf32`, integer low-bit modes. |
| SM89 | FP8 register MMA | Register fragments | FP8 E4M3/E5M2 inputs with `f32` accumulators. |
| SM90 | WGMMA async | A in registers or SMEM descriptor; B in SMEM descriptor; D in registers | `f16`, `bf16`, `tf32`, FP8, integer modes. |
| SM100/SM103 | TCGEN/UMMA | A in SMEM descriptor or TMEM; B in SMEM descriptor; D in TMEM | FP8, FP6/FP4-like formats, `f16`, `tf32`, integer modes. |
| SM120/SM121 | Consumer block-scaled MMA | Register operands and per-input scale factors | MXFP8, MXFP4, NVFP4-style inputs with E8M0 scale factors. |

## Per-Arch MMA Shape Lattice

The table below summarises the `(M, N, K)` tile shapes and element-type tuples each tier accepts. Lowering reads this lattice as the first feasibility gate, before any descriptor or operand-layout check runs. Empty cells mean the shape is not exposed for that tier.

| Shape (M, N, K) | sm_70 | sm_75 | sm_80 | sm_89 | sm_90 (WGMMA) | sm_100 (UMMA) | sm_120 (block-scaled) |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| 8x8x4 (legacy)        | f16/f32 acc | — | — | — | — | — | — |
| 16x8x8                | — | f16/bf16 | f16/bf16/tf32 | — | — | — | — |
| 16x8x16               | — | — | f16/bf16, sparse | — | — | — | — |
| 16x8x32 (int/FP8)     | — | — | s8/u8, sparse | e4m3/e5m2 | — | — | f4/f6/f8 + E8M0 scales |
| 16x8x64 (int4)        | — | — | s4/u4 | — | — | — | f4 + E8M0 scales |
| 64x{8..256}x{8..32}   | — | — | — | — | f16/bf16/tf32/FP8/int (B in SMEM desc; A reg or SMEM desc) | — | — |
| 64x{8..256}x{16..64}  | — | — | — | — | — | f16/tf32/FP8/FP6/FP4 (A: SMEM desc or TMEM; B: SMEM desc; D: TMEM) | — |
| 128x{N}xK (2-CTA UMMA) | — | — | — | — | — | cluster-coop variant | — |

Notes on the lattice:

- `M` for SM90 WGMMA is fixed at 64 per warp-group instruction; `N` ranges over `{8, 16, 24, ..., 256}` in
  steps of 8; the canonical `K` per element type is `256 / elem_bits` (see the table below).
- `M` for SM100 UMMA is 64 (single-CTA) or 128 (2-CTA cooperative). `N` is a multiple of 8 up to 256, and
  `K` matches `512 / elem_bits` for the `UMMA_K` orientation or `256 / elem_bits` for `UMMA_MN`.
- SM120 block-scaled MMA accepts only `K = 32` (FP4/FP6/FP8 inputs with E8M0 scales, `vec_size = 32`) or
  `K = 64` (FP4 only, `vec_size in {16, 32}`).
- Sparse variants halve the structurally-sparse operand and add a metadata operand; the shape entry above
  applies to the dense operand.

```c
LogicalResult check_shape_in_lattice(SmTier tier, Shape mnk,
                                     ElementType a, ElementType b, ElementType c) {
    const ShapeLatticeRow *row = lookup_lattice_row(tier, mnk);
    require(row != NULL);
    require(in_set(a, row->legal_a_types));
    require(in_set(b, row->legal_b_types));
    require(in_set(c, row->legal_acc_types));
    return success();
}
```

## If You Know CUTLASS (open source) — what is different here

Coming from the open-source `cutlass/cute` C++ headers, the differences are representational rather than semantic.

| CUTLASS C++ concept | tileiras IR form |
|---|---|
| `cute::MMA_Atom<MMA_Traits<sm90_64x128x16_F16F16F32_SS>>` | `cute_nvgpu.sm90.mma` op with `shape_MNK`, `a_type`, `b_type`, `c_type` attributes plus operand-residency-typed values |
| `cute::Layout<Shape, Stride>` template | `!cute.layout` type with hierarchical `(shape, stride)` trees and a 7-kind discriminator (see `verifiers.md`) |
| `cute::TiledCopy` / `cute::TiledMMA` | `cute.make_tiled_copy` / `cute.make_tiled_mma` builders consuming atom values |
| `cutlass::PipelineTmaAsync<Stages>` class template | `cutlass.pipeline.create_pipeline` + `cutlass.pipeline.init` ops with explicit producer/consumer participant attributes |
| `cutlass::PersistentTileScheduler` class template | `cutlass.tile_scheduler.static_persistent` op returning a typed scheduler handle |
| WGMMA descriptor packed by `make_smem_desc` | `cute_nvgpu.smem_desc_view` type (see [WGMMA descriptor construction](#smem-descriptor-construction)) |
| Sparse metadata operand on `mma.sp` | Dedicated `sparse_metadata` value with its own layout, slot 3 of the synthesised layout result |
| Block-scaled `scale_factor_a`/`b` template arguments | `scale_a`/`scale_b` operands typed as `E8M0` fragments (SM120) or TMEM-resident scale vectors (SM100) |

Two practical consequences for porters: every template-time decision becomes an op attribute the verifier can re-check, and every operand residency (register / SMEM descriptor / TMEM) becomes a typed value the lowering routes through a dedicated atom path. The library's `make_smem_desc` is the per-atom call to `sub_17DD6A0`; the open-source `cute_tile_scheduler` is the `cutlass.tile_scheduler.*` family.

## Common Atom Contract

```c
LogicalResult verify_mma_atom(MmaAtom atom, Target target, MmaUse use) {
    require(target.supports(atom.min_tier));
    require(use.shape == atom.shape || shape_is_compatible(use.shape, atom.shape));
    require(use.a.element_type in atom.legal_a_types);
    require(use.b.element_type in atom.legal_b_types);
    require(use.acc.element_type in atom.legal_accumulator_types);
    require(use.a.residency in atom.legal_a_residency);
    require(use.b.residency in atom.legal_b_residency);
    require(use.result.residency in atom.legal_result_residency);

    if (atom.requires_sparse_metadata) {
        require(use.sparse_metadata.valid);
    }

    if (atom.requires_scale_factors) {
        require(use.scale_factors.valid);
        require(scale_factor_layout_is_legal(atom, use.scale_factors));
    }

    return success();
}
```

Check layout and residency in the verifier — not after lowering. Once an atom has become a raw NVVM intrinsic or an inline PTX fragment, diagnostics can no longer explain the original layout mismatch clearly.

## Operand Contract by Tier

Each tier pins its operands to a specific memory space and presents a specific kind of typed value to the lowering. The table below lays this out per tier so a reimplementation can carry one operand-type classifier per row.

| Tier / atom | A operand | B operand | D / accumulator | Predicate | Extra |
|---|---|---|---|---|---|
| SM70 universal FMA | register fragment | register fragment | register fragment | none | — |
| SM80 dense `sm80.mma` | register fragment | register fragment | register fragment | none | `f16`/`bf16`/`tf32`/`s8`/`s4` family |
| SM80 sparse `sm80.sparse_mma` | structurally-sparse register fragment | register fragment | register fragment | none | `u32` metadata fragment (slot 3) |
| SM89 FP8 `sm89.mma` | register fragment (e4m3 or e5m2) | register fragment | f32 register fragment | none | — |
| SM90 WGMMA `sm90.mma` | register fragment or SMEM descriptor (`!cute_nvgpu.smem_desc_view`) | SMEM descriptor | register fragment (async — not ready until wait) | none | mbarrier for completion; scale-D selector |
| SM100 UMMA `sm100.mma` | SMEM descriptor or TMEM pointer | SMEM descriptor | TMEM pointer | none | mbarrier; 2-CTA mask when clustered |
| SM100 block-scaled `sm100.mma_bs` | SMEM descriptor / TMEM | SMEM descriptor | TMEM pointer | none | scale-factor vectors in TMEM, E8M0 |
| SM100 sparse block-scaled `sm100.mma_bs_sp` | sparse SMEM/TMEM | SMEM descriptor | TMEM pointer | none | metadata vector + scale vectors |
| SM120 block-scaled `SM120.mma_bs` | register fragment | register fragment | register fragment | none | `scale_a` and `scale_b` register fragments (E8M0) |

Reading the table:

- **register fragment** means the operand is an SSA value typed as a `!cute.layout`-shaped register slice.
- **SMEM descriptor** means a packed 64-bit descriptor word built by the constructor at `sub_17DD6A0` and
  surfaced in IR as `!cute_nvgpu.smem_desc_view<src, layout>`.
- **TMEM pointer** means a Blackwell tensor-memory tile address, typed by the TMEM allocation lifecycle.
- **mbarrier** for SM90/SM100 means the atom's completion is observed by a separate `mbarrier.wait` or
  `wgmma.wait_group` op; no register-side operand carries the completion token.

The missing predicate column is deliberate. MMA atoms here do not carry per-lane predicates; masking is the job of the producer/consumer pipeline of the enclosing region — see the `cutlass.pipeline` family in [pipeline-and-tile-scheduler.md](../cutlass/pipeline-and-tile-scheduler.md).

## SM70 and SM75

Older tensor-core tiers travel through universal or backend intrinsic paths — no dedicated per-tier `cute_nvgpu` mnemonic. The public contract is:

- register-resident input and accumulator fragments;
- classic `mma.sync` shapes;
- `f16` and `bf16` style input families depending on tier;
- no WGMMA descriptor, TMA descriptor, TMEM, or block-scale operands.

These atoms remain useful as compatibility targets, but most modern layout-selection logic starts at SM80 or later.

## SM80 and SM89 Reference-Layout Synthesizer

`sub_1854CF0` (6 640 bytes) is the per-`mma_atom` builder that emits the canonical `Layout` for SM80 and SM89 register-MMA tile-fragment placement. It keys on a 5-tuple `(K, M, sparse, fp8, trans_a)` and routes to one of seven arms; each arm composes shape/stride triples that match the PTX form the lowering will eventually emit. The output Layouts feed straight into the operand-layout verifier, so the synthesiser and the verifier share one source of truth for fragment placement.

### Seven-Arm Dispatch

Each MMA atom carries its tile shape and element type in the 5-tuple key. The synthesiser reads the key out of the atom descriptor and routes to the arm whose tuple matches exactly. No fallthrough between arms — an unmatched key already failed verification earlier in the pipeline.

| Arm | K | M | sparse | fp8 | trans_a | PTX form |
|---|---:|---:|:---:|:---:|:---:|---|
| 0 | 16 | 16 | no | no | no | `mma.sync.aligned.m16n8k16.row.col.f16.f16.f16.f16` |
| 1 | 16 | 16 | no | no | yes | `mma.sync.aligned.m16n8k16.row.row.f16` |
| 2 | 16 | 16 | yes | no | no | `mma.sp.sync.aligned.m16n8k16.row.col.f16.f16.f16.f16` |
| 3 | 32 | 16 | no | no | no | `mma.sync.aligned.m16n8k32.row.col.s8.s8.s32` |
| 4 | 32 | 16 | no | yes | no | `mma.sync.aligned.m16n8k32.row.col.e4m3.e4m3.f32` (SM89) |
| 5 | 32 | 16 | yes | no | no | `mma.sp.sync.aligned.m16n8k32.row.col.s8.s8.s32` |
| 6 | 8 | 16 | no | no | no | `mma.sync.aligned.m16n8k8.row.col.f16.f16.f16.f16` |

Arm 4 is the SM89-only FP8 path. The remaining arms apply at SM80 and above. Arms 2 and 5 are the structured-sparse forms, and they select the four-slot return path described below.

### Stride Triples

Each arm assembles its output Layout from one of three stride triples. The triples land verbatim in the result Layouts and get matched against PTX-encoded offsets at lowering time.

| Triple | Stride values | Used by |
|---|---|---|
| dense.A | `{128, 256, 1024}` | dense-MMA A-operand |
| dense.B | `{2048, ...}` | dense-MMA B-operand |
| sparse.metadata | `{0x200000, 0x4000000, 0x8000000}` | metadata stride for sparse arms 2 and 5 |

The sparse-metadata triple encodes per-warp metadata-buffer offsets at the 21-, 26-, and 27-bit positions. Those bit positions match the `metadata-stride` field of the `mma.sp` PTX form, so the synthesised Layout surfaces the PTX wire format directly rather than as an abstract description awaiting translation.

### Result-Slot Encoding

Output Layouts are stored consecutively in a 152-byte stride array. Each entry holds the shape vector, the stride vector, and 24 bytes of decoration: per-element-type metadata, padding, and alignment information that the verifier compares against the declared operand layout. Slot zero through slot two always carry the A, B, and C Layouts. When the arm is sparse, the four-slot helper at `sub_1854130` writes the metadata Layout into slot three at offset `+456` of the result buffer.

```c
typedef struct {
    Layout slots[4];     /* 152 bytes each; slot[3] valid only on sparse arms. */
    uint32_t slot_count; /* 3 for dense arms, 4 for arms 2 and 5. */
} MmaLayoutResult;
```

The dispatcher picks between the three-slot and four-slot paths by inspecting the metadata-stride field of the input atom: a non-zero stride forces the sparse path. The caller-provided return buffer is fixed-size, so callers must read the slot count alongside the buffer rather than infer it from buffer width.

### Warp-Fragment Element Counts

Each arm also returns the per-thread fragment element count. The calling layout pass uses it to size the warp's register-file allocation. The counts come straight from dividing the tile size across the 32-thread warp tile:

| Arm class | Per-thread elements | Reasoning |
|---|---:|---|
| Dense `f16` | 8 | `16 * 8 * 16 / 256` over a four-warp warp-group footprint |
| Dense `s8` | 16 | wider K and narrower element width |
| Dense FP8 | 16 | same K and lane footprint as the `s8` dense path |
| Sparse | half of the dense count | the structured-sparse input layout is halved, metadata replaces the missing half |

### Atom Verifier Contract

The verifier consumes the synthesised Layouts directly. Residency, shape, and element-type tuples are checked together, and the sparse-metadata layout participates in the same equivalence check.

```c
LogicalResult verify_sm80_mma(MmaUse use, bool sparse) {
    require(use.a.residency == REGISTER_MEMORY);
    require(use.b.residency == REGISTER_MEMORY);
    require(use.result.residency == REGISTER_MEMORY);
    require(is_supported_sm80_mma_shape(use.shape));
    require(is_supported_sm80_element_tuple(use.a.type, use.b.type, use.acc.type));

    MmaLayoutResult expected = synthesize_sm80_layouts(use.atom);
    require(layouts_equivalent(use.a.layout, expected.slots[0]));
    require(layouts_equivalent(use.b.layout, expected.slots[1]));
    require(layouts_equivalent(use.acc.layout, expected.slots[2]));

    if (sparse) {
        require(use.sparse_metadata.valid);
        require(expected.slot_count == 4);
        require(layouts_equivalent(use.sparse_metadata.layout, expected.slots[3]));
    }

    return success();
}
```

SM80 sparse metadata is part of the atom contract. A lowering that drops it is not equivalent to dense MMA, and a verifier that skips the slot-three Layout comparison will miss a mis-sized metadata buffer entirely before lowering.

## SM89

SM89 extends the register-MMA model with FP8 E4M3 and E5M2 inputs and `f32` accumulators. Mixed FP8 input pairs are legal as long as both operands pick supported FP8 types.

```c
LogicalResult verify_sm89_fp8_mma(MmaUse use) {
    require(use.a.residency == REGISTER_MEMORY);
    require(use.b.residency == REGISTER_MEMORY);
    require(is_fp8_e4m3_or_e5m2(use.a.type));
    require(is_fp8_e4m3_or_e5m2(use.b.type));
    require(use.acc.type == f32_type());
    require(use.shape.k == 32);
    return success();
}
```

There is no sparse FP8 companion in this tier.

## SM90 WGMMA

SM90 WGMMA is a warp-group asynchronous operation. B always rides an SMEM descriptor; A is either a register fragment or another SMEM descriptor. The result lives in registers, but it is not ready until the WGMMA wait sequence completes.

```c
void lower_sm90_wgmma(WgmmaAtom atom, WgmmaUse use) {
    require(use.b.is_smem_descriptor);
    require(use.a.is_register_fragment || use.a.is_smem_descriptor);

    emit_wgmma_fence();

    for (MmaTile tile : split_into_wgmma_tiles(use)) {
        emit_wgmma_mma_async(atom, tile);
    }

    emit_wgmma_commit_group();
    emit_wgmma_wait_group();
}
```

A correct lowering preserves asynchronous ordering. Reading accumulators before the wait is a correctness bug even if the IR dependency graph looks fine.

The SMEM descriptor carries base address, leading byte offset, stride byte offset, base offset, and swizzle mode. Build it from the same layout algebra the operand verifier uses; otherwise descriptor construction and verification can drift apart.

### SMEM-Descriptor Construction

`sub_17DD6A0` (4 984 bytes) packs the 64-bit SMEM descriptor that each `wgmma.mma_async.sync.aligned` instruction consumes for its A and B operands. The descriptor is built once per operand before the WGMMA tile loop, then threaded through the inline-asm fragment as an `l`-constraint i64 input. The same bit layout serves every Hopper WGMMA shape, so the constructor is one routine fed by per-atom shape and swizzle metadata — not a family of per-shape variants.

The 64-bit packing layout is a bitfield over the canonical Hopper descriptor word:

```c
typedef union WgmmaDescriptor {
    uint64_t raw;
    struct {
        uint64_t start_addr   : 14;   /* bits 0-13  : low 14 bits of SMEM byte offset (>>4)        */
        uint64_t lbo          : 16;   /* bits 14-29 : leading byte offset (per-warp tile size)     */
        uint64_t sbo          : 16;   /* bits 30-45 : stride byte offset (between warp tiles)      */
        uint64_t base_offset  : 3;    /* bits 46-48 : base offset (per-CTA SMEM offset, divided 8) */
        uint64_t reserved     : 3;    /* bits 49-51 : reserved, always zero                        */
        uint64_t swizzle_mode : 2;    /* bits 52-53 : 0=none, 1=128-B, 2=64-B, 3=32-B              */
        uint64_t pad          : 10;   /* bits 54-63 : padding                                      */
    };
} WgmmaDescriptor;
```

The `start_addr` field stores the low 14 bits of `(smem_offset >> 4)`. WGMMA only accepts 16-byte-aligned SMEM addresses, so the constructor shifts and masks the raw SMEM byte offset rather than embedding it unshifted. `lbo` and `sbo` together encode the two-dimensional tile-stride layout for an A or B operand: `lbo` is the leading byte offset between rows of a single warp tile, and `sbo` is the stride byte offset between consecutive warp tiles along K. `base_offset` is a per-CTA offset scaled by eight. The reserved field must be zero per the Hopper ISA, and the constructor masks it explicitly.

The swizzle-mode field picks the SMEM bit-reversal pattern that lets two warps in the warp-group read the same SMEM region without bank conflicts:

| `swizzle_mode` | Bytes-per-row | Used for |
|---|---:|---|
| 0 | none | Plain row-major SMEM |
| 1 | 128 | Hopper canonical 128-B swizzle |
| 2 | 64  | 64-B swizzle (smaller TC operand) |
| 3 | 32  | 32-B swizzle (sub-tile WGMMA) |

The 128-B mode is the canonical Hopper choice for full-width A and B tiles. The 64-B and 32-B modes kick in when the operand element width or warp-tile footprint is smaller than a canonical 128-B row.

### GMMA_K and MN Constraints

Per element type, the canonical K-size one WGMMA instruction consumes is `256 / elem_bits`. The MN extent must be a multiple of 8 in every case — a WGMMA hardware constraint on the output-tile size, independent of input element type.

| Element type | K-size (canonical) | MN multiple |
|---|---:|---:|
| f32 (TF32) | 8 | 8 |
| f16 | 16 | 8 |
| bf16 | 16 | 8 |
| s8 | 32 | 8 |
| s4 | 64 | 8 |
| e4m3/e5m2 | 32 | 8 |

The constructor derives `lbo` and `sbo` byte counts from the abstract tile shape via this table. An `m64n128k16.f16` tile uses `K = 16` because `256 / 16 = 16`, and the leading byte offset is `K * sizeof(f16)` scaled by the swizzle mode.

### Inline-Asm Template

`sub_17DD6A0` ends by emitting an inline-asm fragment whose PTX body has the canonical WGMMA form. For `m64n128k16.f32.f16.f16` the emitted string is:

```
wgmma.mma_async.sync.aligned.m64n128k16.f32.f16.f16
    { %f0, %f1, ... },
    %r2, %r3, %p4
```

The accumulator register list expands to the per-thread fragment count for the chosen tile shape. The constraint string is `=f,=r,l,r,n` in argument order:

- `=f` marks each float output register in the accumulator fragment;
- `=r` marks the i32 output register used for the descriptor's scale-D return slot;
- `l` is the i64 descriptor input that the constructor produced;
- `r` is the i32 scale input that selects the accumulator-update mode;
- `n` is the immediate predicate input that conditions the MMA on a compile-time-known flag.

A correct lowering threads the same `WgmmaDescriptor.raw` value into the `l` slot for the operand-B descriptor and, when A is SMEM-resident rather than register-resident, into a second `l` slot for operand A. The constructor and the verifier must read the descriptor layout from the same table — if the verifier expects 128-B swizzle but the constructor emits 64-B, the inline-asm fragment runs against the wrong SMEM region and produces silently wrong results.

## SM100 and SM103 UMMA

SM100 introduces tensor memory and TCGEN-style MMA. The output accumulator lives in TMEM; A comes from an SMEM descriptor or from TMEM; B always comes from an SMEM descriptor. Sparse and block-scaled variants add metadata and scale-factor operands.

```c
LogicalResult verify_sm100_umma(MmaUse use, UmmaKind kind) {
    require(use.result.residency == TENSOR_MEMORY);
    require(use.b.is_smem_descriptor);
    require(use.a.is_smem_descriptor || use.a.residency == TENSOR_MEMORY);
    require(is_supported_umma_shape(use.shape));
    require(is_supported_umma_element_tuple(use, kind));

    if (kind.is_sparse) {
        require(use.sparse_metadata.valid);
    }

    if (kind.is_block_scaled) {
        require(use.scale_factors.valid);
        require(use.scale_factors.type == e8m0_type());
    }

    return success();
}
```

Two-CTA and cluster variants belong to the UMMA contract too — they affect TMEM allocation, write-disable behaviour, and barrier transaction counts.

### SM100 UMMA Block-Scaled `(atom_K, vecSize)` Atoms

SM100 UMMA's block-scaled MMA atom family covers FP4 and FP8 microscale matrix multiplication with per-block scale factors in tensor memory. The verifier `sub_14B71C0` enumerates exactly three legal `(atom_K, vecSize)` triples and returns a packed encoding `(atom_K << 32) | vecSize` (or zero on error). Callers mask the result with `~7` to extract a 3-bit tag from the low bits, and the atom builder records that tag to track which block-scaled variant the op carries.

| (atom_K, vecSize) | A type x B type | Scale type | PTX `kind` | Packed return |
|---|---|---|---|---|
| (32, 32) | `FP8` x `FP8` | `E8M0` | `kind::f8f6f4` | `0x2000000020` |
| (64, 16) | `FP4` x `FP4` | `E8M0` / `E4M3FN` | `kind::mxf4` (OCP MX-FP4) | `0x4000000010` |
| (64, 32) | `FP4` x `FP4` | `E8M0` | `kind::mxf4nvf4` (NVFP4 block-64) | `0x4000000020` |

The accumulator type is hard-locked to `Float32` across all three variants, regardless of input element type. Any other accumulator type triggers `"expects c type to be Float32"` and the op fails before lowering.

`cute_nvgpu` carries two 4-bit element-type TypeIDs sharing the same `.data.rel.ro` slot at `&unk_5BE6068`: `Float4E2M1FN` is the IEEE-style OCP MX-FP4 encoding (2 exponent, 1 mantissa, finite-only), and `FloatNV4E0M3F` is NVIDIA's NVFP4 fixed-point encoding (0 exponent, 3 mantissa). They share the slot because both are 4-bit packed types, but the dispatcher in `sub_14B71C0` distinguishes them by the `sf_a` and `sf_b` scale-factor element types. When `sf_a == sf_b == E8M0` the layout is NVFP4 and selects `kind::mxf4nvf4`. When the scale-factor element type is `E4M3FN` the layout is OCP MX-FP4 and selects `kind::mxf4`. A mismatch between `sf_a` and `sf_b` triggers `"sfa/sfb element type mismatch"`.

The verifier's accept set is the conjunction of four predicates:

- `c.elementType == Float32` always.
- `(a.elementType, b.elementType, atom_K)` matches one of `(FP8, FP8, 32)` or `(FP4, FP4, 64)`.
- `(sf.elementType, vecSize)` matches one of `(E8M0, 32)`, `(E8M0, 16)`, or `(E4M3FN, 16)`.
- `sf_a.elementType == sf_b.elementType`.

Every other combination emits `"Invalid (atom_K, vecSize) combination for block-scaled MMA"` and returns 0. See
[D22 verifier `sub_14B71C0`](../nv_tileas/verifiers.md) for the broader verifier context this table summarises, and
[the NVPTX subtarget feature matrix](../../codegen/nvptx-subtarget-and-feature-matrix.md) for the `tmem` feature
that gates SM100 atoms.

## SM120 and SM121 Block-Scaled MMA

SM120 keeps block-scaled MMA register-resident and uses two scale-factor operands — one for A, one for B. That sets it apart from SM100, where block-scaled forms are tied to the tensor-memory path.

```c
LogicalResult verify_sm120_block_scaled(MmaUse use) {
    require(use.a.residency == REGISTER_MEMORY);
    require(use.b.residency == REGISTER_MEMORY);
    require(use.result.residency == REGISTER_MEMORY);
    require(use.scale_a.valid);
    require(use.scale_b.valid);
    require(use.scale_a.type == e8m0_type());
    require(use.scale_b.type == e8m0_type());
    require(use.shape.k == 32 || use.shape.k == 64);
    require(is_supported_sm120_input_type(use.a.type));
    require(is_supported_sm120_input_type(use.b.type));
    return success();
}
```

For `K = 32`, FP4, FP6-like, and FP8-like input families are allowed with a fixed scale-vector shape. For `K = 64`, the accepted input family narrows to FP4-style operands, and the scale-fragment width must match the selected vector size.

## Operand Layout Grammar

MMA atoms use `cute` layout algebra to record which thread owns which fragment element. A verifier reconstructs the expected layout for the atom and compares it against the declared one:

```c
LogicalResult verify_operand_layout(MmaAtom atom, OperandRole role, Layout layout) {
    Layout expected = expected_mma_layout(atom, role);
    require(layouts_equivalent(normalize_layout(layout), normalize_layout(expected)));
    require(layout_is_static(layout));
    require(!layout_has_scaled_basis(layout));
    return success();
}
```

For WGMMA and UMMA the layout often lives in a descriptor rather than a lane-by-lane register layout. The verifier still derives the descriptor from layout algebra and rejects descriptors the declared layout cannot explain.

## Invariants

- The target supports the tier named by the atom.
- Operand residency matches the tier: registers, SMEM descriptor, or TMEM.
- MMA shape and element-type tuples are checked together.
- Sparse atoms carry valid metadata.
- Block-scaled atoms carry valid scale factors and scale-vector parameters.
- WGMMA lowering emits fence, async MMA, commit, and wait in order.
- UMMA lowering preserves TMEM allocation and CTA-group semantics.
- SM120 uses two scale-factor operands and preserves uppercase `SM120` spelling.

## Reimplementation Checklist

1. Model each tier as a data table plus verifier hooks.
2. Keep operand residency explicit in the atom type or use site.
3. Reconstruct expected operand layouts during verification.
4. Lower WGMMA through an async group sequence, not a synchronous MMA shortcut.
5. Lower SM100/SM103 through tensor-memory-aware UMMA paths.
6. Keep SM120 block-scaled register MMA separate from SM100 block-scaled UMMA.
