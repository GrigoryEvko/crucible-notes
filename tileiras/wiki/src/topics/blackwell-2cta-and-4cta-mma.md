# Blackwell 2-CTA + 4-CTA MMA

## Abstract

Blackwell tensor-core lowering separates the cooperative copy from the matrix instruction that consumes the copied tile. The SMEM-to-TMEM staging copy can be single-CTA, 2-CTA, or 4-CTA. The matching `tcgen05.mma` instruction carries only the MMA-side group encodings it understands; the 4-CTA fan-out lives on the copy side, where the A operand is distributed across a CTA cluster before the MMA consumes each CTA's local slice.

Tileiras lowers the `cute_nvgpu.atom.make_s2t_copy` atom through one shared MLIR rewrite path. That path builds a `cute.tiled.copy`, optionally guards it with an `scf.if`, and later lowers the copy to the `tcgen05.cp` family. The sibling IMMA and WGMMA atom paths do not read the cluster CTA-rank special register; rank-aware partitioning is specific to S2T copy lowering.

The cluster fan-out lives on the copy side, not the MMA side. PTX gives `tcgen05.mma` only `cta_group::1` and `cta_group::2`; there is no `cta_group::4` MMA encoding. The 4-CTA shape must therefore be a copy-time partition that produces four already-sliced TMEM destinations, and the MMA that follows is a plain single-CTA matrix instruction over the per-CTA slice. A reimplementation that puts the fan-out on the MMA side will fail to encode anything in PTX. The DSMEM handshake described in [DSMEM Handshake and Cluster Barrier](dsmem-handshake-and-cluster-barrier.md) is the synchronisation companion of this copy lowering: the multicast S2T copy advertises its transaction bytes to peer CTAs through exactly that handshake.

## Copy-Side Ownership

The S2T copy rewrite performs four jobs:

- Resolve the source and destination tile layouts.
- Initialize or find the mbarrier that protects the asynchronous copy.
- Partition the source and TMEM destination according to the CTA-group shape.
- Emit the `cute.tiled.copy` and return the async token expected by the surrounding pipeline.

The `AtomS2tCopyShape` properties carry the group width through two fields: a numeric `cta_group` value from `{1, 2, 4}` and a one-based enum selector used by the shape-dispatch table. They co-vary in observed inputs, but the lowering reads them independently. The numeric field controls mbarrier and predicate shortcuts; the enum controls the multicast width selected by the layout-composition branch.

## Rank Predicate

The multi-CTA gate reads `nvvm.read.ptx.sreg.cluster.ctarank`, computes the rank modulo the multicast width, masks the low bit, converts the result into a warp-uniform predicate, and uses that predicate to guard the copy body. In the 4-CTA case, ranks with odd low bits issue the multicast copy while peer CTAs receive their partition through the cluster copy semantics.

The 2-CTA case differs: it uses a direct uniform-true predicate and relies on the downstream `tcgen05.cp` 2-CTA handshake to handle the pair. The single-CTA case shares some lowering scaffolding with the 2-CTA case, but it is not a cluster partition — only one CTA participates.

```c
static Value *build_s2t_copy_predicate(Rewriter *rewriter, CtaGroup group) {
    if (group == CTA_GROUP_1 || group == CTA_GROUP_2) {
        return constant_true_i1(rewriter);
    }

    int32_t rank = nvvm_read_cluster_ctarank(rewriter);
    int32_t rem = arith_remsi(rank, (int32_t)group);
    int32_t low_bit = arith_andi(rem, 1);
    return make_warp_uniform_i1(rewriter, low_bit != 0);
}
```

## CTA-Group Mapping

Combining the enum selector and the numeric group gives the runtime mapping:

| Shape enum | Numeric `cta_group` | Copy lowering | MMA-side meaning |
| --- | --- | --- | --- |
| `1` | `1` | Single-CTA S2T copy; no real cluster partition. | Ordinary single-CTA MMA input slice. |
| `2` | `2` | 2-CTA cooperative S2T copy with uniform predicate. | Two CTAs co-own opposite halves. |
| `3` | `4` | 4-CTA S2T copy with rank-based issuing predicate. | MMA consumes already-partitioned slices. |

Destination partitioning is part of the copy layout. In the 4-CTA case, `partition_D` splits the TMEM destination into per-CTA quarter slices before the copy is emitted. The downstream MMA therefore needs no `cta_group::4` control word: by the time it runs, each participating CTA already sees the slice it owns.

## Reimplementation Notes

A reimplementation should model the fan-out as a copy property, not an MMA property:

- Parse the S2T copy shape into a numeric CTA group and a shape enum.
- Use the numeric group for mbarrier and predicate decisions.
- Use the enum to choose the layout composition and multicast width.
- Emit rank-based gating only for the 4-CTA copy path.
- Partition the TMEM destination before issuing the copy.
- Do not invent a `tcgen05.mma.cta_group::4` encoding.

```c
void lower_s2t_copy(Rewriter *rewriter, S2TCopyOp op) {
    AtomS2tCopyShape shape = parse_s2t_shape(op);
    CtaGroup group = shape.cta_group;

    Layout source_layout = build_source_copy_layout(op, shape);
    Layout dest_layout = partition_tmem_destination(op.tmem_layout, group);
    MBarrier barrier = get_or_create_s2t_mbarrier(op, group);
    Value *predicate = build_s2t_copy_predicate(rewriter, group);

    CopyAtom atom = make_s2t_copy_atom(source_layout, dest_layout, group, barrier);
    Coord coord = make_s2t_copy_coord(op, shape);

    scf_if(predicate) {
        emit_cute_tiled_copy(atom, coord, barrier);
    }

    replace_op_with_async_token(op, barrier);
}
```
