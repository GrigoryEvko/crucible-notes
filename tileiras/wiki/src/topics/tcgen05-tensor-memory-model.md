# tcgen05 and the Tensor Memory Model

## Abstract

Blackwell introduces tensor memory — TMEM — as a third on-chip memory class alongside registers and shared memory. TMEM is per-SM, addressed in a 128-row dense grid, and reachable only from a small family of asynchronous instructions. The `tcgen05` instruction family is that small family: matrix multiply, sparse multiply, weight-stationary multiply, and the block-scaled microscale variants all consume TMEM operands and write TMEM accumulators. This page documents the tensor memory model and the `tcgen05.mma` instruction family that consumes it. SM100 and SM103 only.

This page is the canonical reference for the model and the variant taxonomy. It supersedes the scattered tcgen05 paragraphs in [tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) (the validation snippet plus control-word table) and [Mode Pattern Verifiers](../dialects/cute_nvgpu/mode-pattern-verifiers.md) (the 13-diagnostic kind-word verifier). Those pages keep their backend-validation and verifier-diagnostic content; the structural model lives here.

## Tensor Memory

TMEM is per-SM, not per-CTA. A kernel that wants TMEM allocates from the SM's TMEM region through `nvvm.tcgen05.alloc.shared`, which returns a handle that subsequent `tcgen05` instructions consume as a 32-bit base address plus row/col descriptor. The allocator is shared across all warps on the SM — every warp in every resident CTA sees the same TMEM address space, but the allocation contract pins each region to one logical owner.

The grain is one 128-bit lane, organised into a 128-row grid where rows index along the M dimension of an MMA tile and columns index along K (or N for the accumulator). A WGMMA-style MMA tile of `m64n128k16.fp16` occupies a contiguous TMEM region spanning 64 rows and the K-derived column count; the allocator hands back the base row index, and the MMA operand encoding adds the column offset.

Only `tcgen05` instructions can read or write TMEM. There is no `ldg` to TMEM, no `cp.async` to TMEM directly, no register-to-TMEM move outside the `tcgen05` family. Staging into TMEM happens through `tcgen05.cp`, the copy variant that moves data from SMEM to TMEM. Staging out of TMEM happens through `tcgen05.st` and `tcgen05.ld`. The model is "TMEM is the accumulator and operand reservoir, and only the MMA family talks to it."

The instruction family also gates the 2-CTA cooperative MMA path. When two CTAs in a cluster cooperate on one MMA tile, they share TMEM rows: CTA 0 holds rows `[0..M/2)` and CTA 1 holds rows `[M/2..M)`. The cooperating MMA emits a `cta_group::2` opcode that pairs the two halves at execute time. The 4-CTA copy variant exists only on the copy side — the MMA encoding has no `cta_group::4` form, and Blackwell's 4-CTA semantics is a copy-time partition into already-sliced TMEM destinations that ordinary single-CTA MMAs then consume.

## The tcgen05 Variant Taxonomy

The `tcgen05.mma` family covers ten machine variants. Each combines an MMA kind (dense, sparse, block-scaled, sparse block-scaled) with optional weight-stationary mode and CTA-group selector. The lowering packs the choice into a 9-bit kind word; the backend verifier rejects illegal combinations before machine selection.

| Variant | CTA group | Sparsity | Block scale | Weight-stationary |
|---|---|---|---|---|
| dense MMA | 1 or 2 | no | no | no |
| sparse MMA | 1 or 2 | yes | no | no |
| weight-stationary dense | 1 | no | no | yes |
| weight-stationary sparse | 1 | yes | no | yes |
| block-scaled dense | 1 or 2 | no | yes | no |
| block-scaled sparse | 1 or 2 | yes | yes | no |
| warp-specialized dense | 1 | no | no | yes (alias) |
| warp-specialized sparse | 1 | yes | no | yes (alias) |
| warp-specialized block-scaled | 1 | no | yes | yes (alias) |
| warp-specialized sparse block-scaled | 1 | yes | yes | yes (alias) |

Weight-stationary mode reuses bit 0 of the kind word as a 1-bit predicate; the warp-specialized variants are weight-stationary at `cta_group::1`. The verifier rejects `cta_group::2` whenever the weight-stationary bit is set, and rejects weight-stationary mode for the wider `mxf8f6f4` and FP4 input families.

## Control Word Layout

The 9-bit kind word packs five orthogonal fields:

```c
typedef union Tcgen05MmaKind {
    uint32_t raw : 9;
    struct {
        uint32_t cta_group         : 2;   // bits 0-1: 1 = 1-CTA, 3 = 2-CTA
        uint32_t scale_vector_size : 2;   // bits 2-3: 0 = 1X (16), 1 = 2X (32), 2 = 4X (64)
        uint32_t scale_input_acc   : 1;   // bit 4: scale applied to accumulator
        uint32_t block_scale       : 1;   // bit 5: block-scaled (FP4/FP8 microscale)
        uint32_t mma_kind          : 3;   // bits 6-8: one of seven enum values
    };
} Tcgen05MmaKind;
```

The `mma_kind` field picks the element-type family and the variant of block scaling:

| Value | mma_kind | Operands |
|---|---|---|
| 0 | mxf4nvf4 | NVFP4 inputs with E8M0 block scales |
| 1 | i8 | signed 8-bit integer inputs (arch-conditional) |
| 2 | mxf8f6f4 | OCP MX-FP8 / FP6 / FP4 inputs with E8M0 scales |
| 3 | f16 | half-precision inputs |
| 4 | tf32 | TensorFloat-32 inputs |
| 5 | f8f6f4 | non-block-scaled FP8/FP6/FP4 (alias of mxf8f6f4 for backward compat) |
| 7 | mxf4 | OCP MX-FP4 inputs with E4M3FN scales |

The cross-field consistency rules — for example, "scale-input-accumulator only applies to f16 and tf32", "block-scale rejects f16/tf32/i8" — are enforced by the verifier and listed in detail on the [Mode Pattern Verifiers](../dialects/cute_nvgpu/mode-pattern-verifiers.md) page.

Beside the kind word, a separate collector word controls how operand A is staged into the MMA:

| Collector::a mode | Meaning |
|---|---|
| use | reuse the existing collector state from a previous MMA in the chain |
| fill | refill the collector with the new A operand before MMA |
| discard | drop collector state after MMA (no reuse downstream) |

Collector mode interacts with the `ashift` modifier — collector use or fill cannot combine with ashift, because both want exclusive control of the A operand's staging slot. The verifier emits "Cannot use collector::a::use or colletor::a::fill with ashift" (preserving the verbatim typo in `colletor`) for that combination.

## Sparsity Metadata

Sparse `tcgen05.mma` variants halve the structurally-sparse operand and add a metadata operand that encodes which lanes are non-zero. The metadata is a 2-bit-per-element selector packed into a u32 stream: each four-element group of the structured-sparse operand carries one byte of metadata that names the two non-zero positions within the group.

The metadata operand rides a separate TMEM region from the value operand. Allocation pairs the two: the dense-value region holds the halved operand at one base row, and the metadata region holds the selector stream at a fixed offset from that base. The pairing is part of the atom contract — the lowering allocates both regions atomically, and the verifier rejects operands where the metadata layout does not match the value layout at the corresponding stride.

For block-scaled sparse variants, the metadata operand applies to the structurally-sparse input (typically operand A), and the scale-factor operands apply independently. The kind word's block-scale bit and sparsity bit are independent — the verifier's ladder checks them as orthogonal modifiers and rejects only specific illegal combinations (MXF4 and MXF4NVF4 with sparsity require arch-conditional targets).

## Block-Scale Operands

Block-scale microscale MMA is the Blackwell answer to MXFP4, MXFP6, MXFP8, and NVFP4. Inputs ride narrow-precision element types (4-bit, 6-bit, or 8-bit); a separate scale-factor vector multiplies each contiguous block of `vecSize` elements by a per-block scale factor. The accumulator stays FP32.

The legal `(atom_K, vecSize)` triples are exactly three:

| (atom_K, vecSize) | A × B types | Scale type | Variant |
|---|---|---|---|
| (32, 32) | FP8 × FP8 | E8M0 | `kind::f8f6f4` |
| (64, 16) | FP4 × FP4 | E4M3FN | `kind::mxf4` (OCP MX-FP4) |
| (64, 32) | FP4 × FP4 | E8M0 | `kind::mxf4nvf4` (NVFP4 block-64) |

Other combinations fail verification with "Invalid (atom_K, vecSize) combination for block-scaled MMA". `atom_K` is the K extent per MMA instruction; `vecSize` is the number of contiguous K-axis elements that share one scale factor.

NVFP4 and OCP MX-FP4 share a 4-bit element type encoding but differ in their scale-factor format: NVFP4 uses E8M0 (8-bit exponent-only) and OCP MX-FP4 uses E4M3FN (4-bit exponent, 3-bit mantissa, finite-only). The dispatcher distinguishes them by inspecting `sf_a` / `sf_b` element types — if both scale-factor operands are E8M0 the layout is NVFP4 and the opcode is `kind::mxf4nvf4`; if both are E4M3FN the layout is OCP MX-FP4 and the opcode is `kind::mxf4`. A mismatch between `sf_a` and `sf_b` rejects with "sfa/sfb element type mismatch".

The scale-factor operands ride dedicated TMEM regions that the atom builder allocates alongside the value operands. The scale-factor layout is one E8M0 (or E4M3FN) value per `(M / vecSize)` tile element — sparse compared to the value operands, but parallel in tile addressing.

## Weight-Stationary Mode

Weight-stationary mode pins operand A to its TMEM region across the K loop, letting subsequent MMA tiles reuse the staged operand without re-loading. The op encoding sets bit 0 of the kind word; the variant is `cta_group::1` only (the verifier rejects `cta_group::2` with weight-stationary), and the operand-A element type is restricted — `mxf8f6f4`, `f8f6f4`, and `mxf4` are all rejected because their wider operand layouts cannot stay stationary across the K loop.

The practical effect is throughput: weight-stationary mainloops amortise A-side TMEM bandwidth across many K iterations. The cost is operand flexibility — the A operand stays in its TMEM region for the whole loop, so the kernel cannot use that region for any other purpose between MMAs.

## Cross-References

[mbarrier State Machine](mbarrier-state-machine.md) is the consumer-side synchronisation that pipelines staging copies into TMEM against the MMA that reads them.
[WGMMA Emission Protocol](wgmma-emission-protocol.md) is the Hopper predecessor; comparing the two shows why the accumulator moved from registers to TMEM.
[Matmul Progression by SM](matmul-progression-by-sm.md) places tcgen05 in the broader SM70-to-SM121 lineage and explains the operand-residency change at SM100.
[MMA Atoms SM70-SM120](../dialects/cute_nvgpu/mma-atoms-sm70-120.md) carries the `(atom_K, vecSize)` block-scaled triple table and the SM100 UMMA layout grammar.
[Mode Pattern Verifiers](../dialects/cute_nvgpu/mode-pattern-verifiers.md) documents the 13-diagnostic ladder that enforces the inter-field consistency rules summarised above.
[Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) covers the cluster-side copy patterns that stage operands into the cooperating CTAs' TMEM regions.
[tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) covers the backend-side machine-form validation.
