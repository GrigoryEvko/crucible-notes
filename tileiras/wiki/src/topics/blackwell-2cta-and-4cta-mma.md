# Blackwell 2-CTA + 4-CTA MMA

## Abstract

The cluster tier — covered end-to-end in [GPU Execution Model](gpu-execution-model.md) — is what makes 2-CTA and 4-CTA cooperative MMA legal in the first place. The copy-side fan-out that distributes operand tiles across cluster CTAs before the MMA fires is the subject of this page; the cluster shape it depends on is established by the `.cluster_dim` directive at the kernel header.

Blackwell tensor-core lowering separates the cooperative copy from the matrix instruction that consumes the copied tile. The SMEM-to-TMEM staging copy can be single-CTA, 2-CTA, or 4-CTA. The matching `tcgen05.mma` instruction carries only the MMA-side group encodings it understands; the 4-CTA fan-out lives on the copy side, where the A operand is distributed across a CTA cluster before the MMA consumes each CTA's local slice.

Tileiras lowers the `cute_nvgpu.atom.make_s2t_copy` atom through one shared MLIR rewrite path. That path builds a `cute.tiled.copy`, optionally guards it with an `scf.if`, and later lowers the copy to the `tcgen05.cp` family. The sibling IMMA and WGMMA atom paths do not read the cluster CTA-rank special register; rank-aware partitioning is specific to S2T copy lowering.

The cluster fan-out lives on the copy side, not the MMA side. PTX gives `tcgen05.mma` only `cta_group::1` and `cta_group::2`; there is no `cta_group::4` MMA encoding. The 4-CTA shape must therefore be a copy-time partition that produces four already-sliced TMEM destinations, and the MMA that follows is a plain single-CTA matrix instruction over the per-CTA slice. A reimplementation that puts the fan-out on the MMA side will fail to encode anything in PTX. The DSMEM handshake described in [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) is the synchronisation companion of this copy lowering: the multicast S2T copy advertises its transaction bytes to peer CTAs through exactly that handshake.

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

The `make_warp_uniform` wrap is structural, not cosmetic. The `cluster.ctarank` SReg is per-CTA — every thread in a CTA reads the same value — but the rewrite emits the predicate at warp scope. Without the warp-uniform wrapper the verifier rejects the predicate as a control-flow operand that could diverge between lanes; with it, every lane in the producing warp agrees on the predicate value, and the downstream `tcgen05.cp` instruction (which requires warp-uniform predicates by ISA contract) accepts the operand. The wrapper is a no-op at runtime — it tells the verifier and downstream codegen that the SSA value is provably warp-uniform.

## Cluster Sibling Pairing

The 2-CTA cluster MMA pairs each CTA with its sibling through the `cluster.ctarank XOR 1` peer-selection idiom. The XOR maps rank 0 to peer 1, rank 1 to peer 0, rank 2 to peer 3, rank 3 to peer 2 — every even-ranked CTA pairs with the odd-ranked CTA one slot above it.

```c
int32_t peer_rank = nvvm_read_cluster_ctarank(rewriter) ^ 1;
```

The peer rank then feeds into the multicast destination address for the cooperative `tcgen05.cp` copy. The DSMEM handshake covered in [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) is what makes the cross-CTA address dereference legal — the multicast copy advertises its transaction bytes to the peer CTA's mbarrier through the cluster transaction protocol before the destination address becomes readable on the peer side.

The 4-CTA group-mapping partitions the cluster into 2-CTA groups by rank parity:

```c
int32_t group_id = nvvm_read_cluster_ctarank(rewriter) % 2;
```

Group 0 holds CTAs at even ranks (0, 2, ...), group 1 holds the odd-ranked CTAs. Inside each group the same `XOR 1` sibling rule applies. The two groups never share TMEM destinations — the `partition_D` step splits the destination into four quarter slices and gives each group two adjacent quarters to fill cooperatively.

## CTA Group Control Word

The `cta_group` field is a 2-bit bitfield inside the `tcgen05.mma` instruction's control word: encoding `01` selects single-CTA MMA, `10` selects the 2-CTA cooperative MMA. The encoding has no 4-CTA value — the hardware would have to interpret the remaining slot `11` as either reserved or as something it does not implement, and the PTX ISA assigns it neither. The structural consequence is what makes the 4-CTA shape a copy-side partition rather than an MMA-side encoding: the producer's `cta_group` bits select 1 or 2, the matrix instruction runs over its already-partitioned per-CTA slice, and the cluster fan-out lives entirely on the `tcgen05.cp` side that fed the slices.

The `cta_group` bits sit alongside the rest of the `Tcgen05MmaKind` enum in the instruction's kind-and-modifier control word; the corrected bitfield layout (after the 2-bit `cta_group` field was disambiguated from the surrounding kind bits) is the same control word the modifier-cascade canonicaliser threads through every `tcgen05.mma` emission.

## CTA-Group Mapping

Combining the enum selector and the numeric group gives the runtime mapping:

| Shape enum | Numeric `cta_group` | Copy lowering | MMA-side meaning |
| --- | --- | --- | --- |
| `1` | `1` | Single-CTA S2T copy; no real cluster partition. | Ordinary single-CTA MMA input slice. |
| `2` | `2` | 2-CTA cooperative S2T copy with uniform predicate. | Two CTAs co-own opposite halves. |
| `3` | `4` | 4-CTA S2T copy with rank-based issuing predicate. | MMA consumes already-partitioned slices. |

Destination partitioning is part of the copy layout. In the 4-CTA case, `partition_D` splits the TMEM destination into per-CTA quarter slices before the copy is emitted. The downstream MMA therefore needs no `cta_group::4` control word: by the time it runs, each participating CTA already sees the slice it owns.

