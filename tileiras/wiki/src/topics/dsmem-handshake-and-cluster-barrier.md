# DSMEM Handshake + Cluster Barrier

## Abstract

`ConvertTileASToLLVM` lowers `nv_tileaa.*` and `nv_tileas.*` barrier operations through two related paths. Plain barriers come from CTA barrier, cluster arrive, and cluster wait operations. DSMEM transaction barriers add a peer-CTA address translation and transactional mbarrier payload before joining the same cluster arrive/wait tail.

The split matches the CUTLASS distinction between `ClusterBarrier::wait()` and `ClusterTransactionBarrier::arrive_and_expect_tx()`. One path synchronizes control flow; the other also advertises how much distributed shared-memory traffic peer CTAs should expect before the cluster rendezvous completes.

[Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) is the producer side of what this page describes. The S2T copy lowering documented there issues a multicast `tcgen05.cp` whose payload is exactly the transaction byte count published by `nvvm.mbarrier.txn` here: producer and consumer must agree on a single byte count or the cluster rendezvous deadlocks. The transaction-byte field on the transaction barrier is therefore the public contract between the copy and the wait — the two paths cannot be reimplemented independently.

## DSMEM Transaction Handshake

For a single-CTA layout the transaction path collapses to the phase-bit update used by ordinary pipeline barriers: compute the next phase with `phase ^ 1`, load the current phase, store the flipped value. No DSMEM mapping or cluster fence is needed when there are no peer CTAs.

For a multi-CTA layout the lowering emits one handshake sequence per peer participant:

| Operation | Purpose |
| --- | --- |
| `nvvm.mapa` | Translate a shared-memory pointer into the peer CTA's DSMEM address. |
| `llvm.addrspacecast` | Convert the DSMEM pointer to the generic pointer type expected by the mbarrier op. |
| `llvm.inline_asm` | Emit `fence.release.cluster;` when the caller requested an explicit release fence. |
| `nvvm.mbarrier.txn` | Advertise the expected transaction byte count to the shared mbarrier. |
| `arith.cmpi` / `scf.if` | Restrict phase-bit mutation to the master lane. |
| `llvm.load` / `arith.xori` / `llvm.store` | Toggle the phase bit. |
| `nvvm.cluster.arrive.*` | Arrive at the cluster rendezvous. |
| `nvvm.cluster.wait` | Wait until all participating CTAs reach the same point. |

```mlir
%dsmem_ptr = nvvm.mapa %smem_ptr, %peer_ctarank : !llvm.ptr<3>
%gen_ptr   = llvm.addrspacecast %dsmem_ptr     : !llvm.ptr<3> to !llvm.ptr
llvm.inline_asm "fence.release.cluster;"          // when a4 != 0
nvvm.mbarrier.txn %gen_ptr, %tx_bytes          : !llvm.ptr, i32
%master   = arith.cmpi eq, %laneid, %zero      : i1
scf.if %master {
  %phase = llvm.load  %phase_ptr               : i1
  %flip  = arith.xori %phase, %one             : i1
  llvm.store %flip, %phase_ptr                 : i1
}
nvvm.cluster.arrive.relaxed { aligned }
nvvm.cluster.wait           { aligned }
```

Without a multi-CTA parent the DSMEM operations are skipped and the lowering emits only the arrive/wait tail. The release mode controls the arrive opcode: with an explicit upstream `fence.release.cluster;` the lowering uses `nvvm.cluster.arrive.relaxed`; otherwise it can use the aligned arrive form directly.

## Plain Cluster Barrier Lowering

Plain barrier lowering consumes a barrier scope and the target compute capability. The compute capability gate controls only the `nvvm.fence.mbarrier.init` prelude: Hopper and newer hardware get the prelude, older hardware skips it. The scope decides whether a CTA-local barrier is emitted before the cluster arrive/wait pair.

| Scope               | sm <= 89                        | sm >= 90                                        |
|---------------------|---------------------------------|-------------------------------------------------|
| `CTA` (0)           | `nvvm.barrier`                  | `fence.mbarrier.init` + `nvvm.barrier`          |
| `Cluster` (1)       | `cluster.arrive.relaxed` + `cluster.wait` | `fence.mbarrier.init` + arrive + wait |
| `ClusterAligned` (2)| `cluster.arrive.relaxed` + `cluster.wait` | `fence.mbarrier.init` + barrier + arrive + wait |

The CTA-only branch returns after `nvvm.barrier`. The cluster branches fall through into `nvvm.cluster.arrive.relaxed` and `nvvm.cluster.wait`. Unlike the DSMEM transaction path, plain barriers always use relaxed arrive: release ordering comes from the mbarrier init prelude on newer hardware and from the CTA-local barrier where that scope requires it.

## Reimplementation Notes

The lowering can be implemented as two explicit routines selected by whether the source barrier
has a DSMEM transaction payload:

```c
void lower_plain_barrier(Rewriter *rewriter, BarrierOp op, int sm) {
    if (sm >= 90) {
        emit_nvvm_fence_mbarrier_init(rewriter, op);
    }

    if (op.scope == BARRIER_SCOPE_CTA || op.scope == BARRIER_SCOPE_CLUSTER_ALIGNED) {
        emit_nvvm_barrier(rewriter);
        if (op.scope == BARRIER_SCOPE_CTA) {
            return;
        }
    }

    emit_cluster_arrive_relaxed(rewriter);
    emit_cluster_wait(rewriter);
}

void lower_dsmem_transaction_barrier(Rewriter *rewriter, TransactionBarrierOp op) {
    if (op.cluster_size == 1) {
        emit_phase_flip(rewriter, op.phase_ptr);
        return;
    }

    for (PeerCta peer : op.peers) {
        Value *dsmem = emit_nvvm_mapa(rewriter, op.smem_ptr, peer.rank);
        Value *generic = emit_addrspacecast_to_generic(rewriter, dsmem);

        if (op.requires_explicit_release) {
            emit_side_effect_inline_asm(rewriter, "fence.release.cluster;");
        }

        emit_nvvm_mbarrier_txn(rewriter, generic, op.transaction_bytes);
        emit_master_lane_phase_flip(rewriter, op.phase_ptr);
    }

    emit_cluster_arrive_for_release_mode(rewriter, op.requires_explicit_release);
    emit_cluster_wait(rewriter);
}
```

The shared invariant is ordering: publish the DSMEM transaction expectation before cluster arrive,
toggle the phase only on the master lane, and always pair cluster arrive with cluster wait for
multi-CTA rendezvous.
