# cute_nvgpu Dialect Overview

## Abstract

`cute_nvgpu` is the NVIDIA architectural atom dialect sitting on top of `cute`. It hosts MMA atoms from SM70 through SM120 (WGMMA and UMMA included), TMA descriptor and transfer atoms, TMEM lifecycle operations, LDSM/STSM matrix-load atoms, and SMEM descriptor views. Every operation passes through an explicit SM-tier verifier, so an invalid `(shape, element type, target)` triple is rejected before NVVM emission. This page is the dialect-level map; per-family detail lives in the linked sub-pages.

Where `cute` describes target-neutral layout algebra, `cute_nvgpu` binds that algebra to real NVIDIA operations — MMA, WGMMA, TMA, TMEM allocation, ldmatrix, stmatrix, async bulk copies, SM-specific copy atoms. It is the seam where a layout stops being merely algebraic and starts requesting a specific GPU instruction family.

The dialect is organised by architecture tier. Older tiers describe classic tensor-core MMA and copy atoms. Hopper-era tiers add WGMMA and TMA descriptor movement. Blackwell-era tiers add tensor-memory lifecycle and block-scaled MMA forms. Tier names live in the operation spellings so verifiers and lowerings can reject invalid shape, element-type, or target combinations before NVVM conversion.

## Position in the Cascade

```text
cute
    |
    | select target-specific copy, MMA, TMA, and tensor-memory atoms
    v
cute_nvgpu
    |
    | normalize architecture atoms
    v
nvgpu
    |
    | emit NVVM intrinsics
    v
PTX
```

`cute_nvgpu` preserves the high-level atom boundary. It sits below pure layout algebra and above raw NVVM intrinsics — the natural place to enforce SM-tier constraints and descriptor compatibility while keeping the final intrinsic selection simple.

## Architecture Tiers

| Tier | Main operations | Meaning |
| --- | --- | --- |
| SM70 | universal FMA and copy fallbacks | Baseline tensor-core-era atom vocabulary. |
| SM80 | `sm80.mma`, sparse MMA | Ampere MMA and structured sparsity forms. |
| SM89 | FP8-oriented MMA variants | Ada-generation element-type extensions. |
| SM90 | WGMMA, TMA descriptor views | Hopper warpgroup MMA and tensor-memory async movement. |
| SM100 | UMMA, TMEM lifecycle, block-scaled MMA | Blackwell datacenter tensor-memory and tcgen-style operations. |
| SM120 | consumer Blackwell block-scaled forms | Consumer Blackwell microscaling and per-lane scale metadata. |

Tier spelling is part of the IR contract. Lowering must not silently reinterpret an operation as a different tier just because another instruction shape looks similar. If a target does not support the tier named by the operation, verification fails before NVVM lowering.

## Atom Families

The major atom families are:

- MMA atoms, including dense, sparse, FP8, WGMMA, UMMA, and block-scaled forms.
- TMA atoms for tensor-memory load, store, gather, scatter, prefetch, and descriptor use.
- Copy atoms for register, shared-memory, global-memory, and tiled partition movement.
- TMEM lifecycle operations for allocation, deallocation, permit transfer, and pointer retrieval.
- Descriptor view operations that connect `cute` layouts to hardware descriptor operands.
- Kernel-marker lowering that turns a `cute` kernel marker into the entry-point marker expected by NVVM.

Each family consumes `cute` layout values and emits lower-level operations whose shapes, element types, and memory spaces are visible to the target.

## Kernel Lowering

The kernel boundary stays deliberately simple. A function marked as a `cute` kernel becomes an NVVM kernel entry, and every architecture atom in the body lowers or normalises toward `nvgpu` and `nvvm`.

```c
void lower_cute_kernel_to_nvvm(Function func, Target target) {
    if (has_attr(func, "cute.kernel")) {
        remove_attr(func, "cute.kernel");
        set_attr(func, "nvvm.kernel");
    }

    for (Operation *op : func.walk()) {
        if (is_cute_nvgpu_mma(op)) {
            require(target_supports_mma_tier(target, op));
            lower_mma_atom(op, target);
        } else if (is_cute_nvgpu_tma(op)) {
            require(target_supports_tma(target, op));
            lower_tma_atom(op, target);
        } else if (is_cute_nvgpu_tmem(op)) {
            require(target_supports_tmem(target, op));
            lower_tmem_lifecycle_op(op, target);
        } else if (is_cute_layout_carrier(op)) {
            rewrite_descriptor_or_view(op, target);
        }
    }
}
```

The rewrite preserves the semantic shape of the atom. A WGMMA atom lowers through a warpgroup MMA op, not a scalarized loop that happens to compute the same value. A TMA atom lowers through descriptor construction and async tensor-memory ops, not through ordinary elementwise loads — unless an explicit fallback path exists.

## Verifier Invariants

A correct verifier should reject invalid target combinations early:

- the selected target supports the SM tier named by the operation,
- MMA tile shapes are supported by that tier,
- operand element types match the tier and the chosen MMA mode,
- sparse MMA forms include valid metadata and selector attributes,
- block-scaled MMA forms include valid scale-vector layout and per-lane scale ids,
- TMA descriptor operands agree with the source or destination layout,
- tensor-memory operations respect allocation, deallocation, and permit-transfer order,
- descriptor views preserve address space, element type, shape, and swizzle requirements,
- kernel entry markers are rewritten before NVVM emission.

These invariants are easiest to enforce while the atom name is still present. Once the op has become an NVVM intrinsic the diagnostic context shrinks, and the original layout intent may already be gone.

## Reimplementation Checklist

Start with `cute` layout consumption and target-tier verification. Add MMA atoms next — they exercise the core shape and element-type tables. Then add TMA and descriptor lowering. Save TMEM lifecycle for last, because tensor-memory allocation interacts with pipeline stages and agent roles, and gets messy without solid pipeline scheduling and memory ordering already in place.

Required pieces:

- SM-tier-aware operation names and verifier tables,
- MMA shape and element-type validation,
- sparse and block-scaled MMA metadata validation,
- TMA descriptor view construction,
- async tensor-memory load/store/gather/scatter lowering,
- TMEM allocation and permit-lifecycle operations for Blackwell targets,
- kernel-marker rewrite to the NVVM entry-point marker,
- lowering from `cute_nvgpu` atoms into `nvgpu` and `nvvm`.

## If You Know CUTLASS (open source) — cross-walk

For readers fluent in `cutlass/arch/*.hpp` and the per-SM atom traits in open-source CUTLASS:

| CUTLASS C++ | tileiras IR (`cute_nvgpu`) |
|---|---|
| `cutlass::arch::Mma<...>` SM70/SM80/SM89 specialisations | `atom.universal_fma`, `sm80.mma`, `sm89.mma` (plus `sm80.sparse_mma`) |
| `cutlass::arch::Wmma<...>` traits | accessed through `atom.universal_fma` and tier-generic paths |
| Hopper `GMMA::ss/rs/sr` descriptor builders | `cute_nvgpu.smem_desc_view` + the descriptor packer at `sub_17DD6A0` |
| Hopper WGMMA atom + `make_smem_desc` | `cute_nvgpu.sm90.mma` op consuming a `!smem_desc_view` typed operand |
| Hopper TMA `cp.async.bulk.tensor` family | `atom.tma_load`, `atom.tma_store`, `atom.tma_reduce` plus the non-exec variants |
| Hopper `cuTensorMapEncodeTiled` | `tma_descriptor_tiled` type + the descriptor builder in [tma-atoms.md](tma-atoms.md) |
| Blackwell TCGEN / UMMA atoms | `sm100.mma`, `sm100.mma_sp`, `sm100.mma_bs`, `sm100.mma_bs_sp` |
| Blackwell TMEM allocation / lifecycle | `atom.tmem_load`, `atom.tmem_store`, `atom.s2t_copy`, the TMEM lifecycle ops |
| `cutlass::arch::Sm120BlockScaledMma<...>` | `SM120.mma_bs` (uppercase `SM` is required) |
| Shared-memory matrix loads (`ldmatrix`) | `atom.ldsm`, `atom.stsm` with the mode/size pattern matrix in [mode-pattern-verifiers.md](mode-pattern-verifiers.md) |

Two departures from the open-source surface matter. First, `SM120.mma_bs` is the only SM120 entry — no `SM120.mma`, no sparse variant — matching the consumer-Blackwell FP4 surface where sparse MMA is not exposed. Second, the SMEM descriptor is a first-class IR type (`!smem_desc_view`) rather than an i64 immediate, so the verifier can re-check the descriptor's swizzle and tile-stride encoding against the same layout that produced it.

## Cross-links

- [SM Tier Roster and Copy Atom Registry](sm-tier-roster-and-copy-atom-registry.md) lists the atom families by target tier.
- [MMA Atoms SM70-120](mma-atoms-sm70-120.md) covers matrix-multiply atom semantics.
- [TMA Atoms](tma-atoms.md) covers tensor-memory descriptor and transfer atoms.
- [Mode Pattern Verifiers](mode-pattern-verifiers.md) covers shape, element-type, and mode checks.
- [Asm Printer and Mnemonic Hash](asm-printer-and-mnemonic-hash.md) covers textual spelling and parser dispatch.
