# Cluster Sync and DSMEM Handshake

## Abstract

A Hopper or Blackwell cluster is a group of cooperating CTAs that share work through a single cluster-level rendezvous. Tileiras lowers cluster-aware barrier operations through two related paths. The plain cluster barrier is a control-flow rendezvous: every participating CTA arrives, then waits, and execution resumes once every participant has reached the same point. The DSMEM transaction handshake is a data-flow rendezvous: a peer CTA publishes its expected transaction byte count on a remote mbarrier before the cluster arrive/wait pair, and the rendezvous completes only when both the arrival count and the transaction-byte count clear.

Both paths share one mechanism. Plain cluster sync is the general primitive every multi-CTA cluster needs; the DSMEM transaction handshake is a specific case where the rendezvous carries a transaction-byte payload because peers are exchanging distributed shared memory through an asynchronous copy. The split matches the CUTLASS distinction between `ClusterBarrier::wait()` and `ClusterTransactionBarrier::arrive_and_expect_tx()`.

The transaction-byte field is the contract between the producer-side copy and the consumer-side wait. [Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) is the producer of the multicast S2T copy whose `tcgen05.cp` payload is exactly the byte count published by `nvvm.mbarrier.txn` below: producer and consumer must agree on a single byte count or the cluster rendezvous deadlocks. The mbarrier object that carries the count is documented separately in [mbarrier State Machine](mbarrier-state-machine.md); this page covers the cluster-side rendezvous that consumes it.

## Plain Cluster Barrier

The plain cluster-barrier lowering consumes a barrier scope and the target compute capability. The compute-capability gate controls only the `nvvm.fence.mbarrier.init` prelude: Hopper and newer hardware get the prelude, older hardware skips it. The scope decides whether a CTA-local barrier is emitted before the cluster arrive/wait pair.

| Scope               | sm <= 89                        | sm >= 90                                        |
|---------------------|---------------------------------|-------------------------------------------------|
| `CTA` (0)           | `nvvm.barrier`                  | `fence.mbarrier.init` + `nvvm.barrier`          |
| `Cluster` (1)       | `cluster.arrive.relaxed` + `cluster.wait` | `fence.mbarrier.init` + arrive + wait |
| `ClusterAligned` (2)| `cluster.arrive.relaxed` + `cluster.wait` | `fence.mbarrier.init` + barrier + arrive + wait |

The CTA-only branch returns after `nvvm.barrier`. The cluster branches fall through into `nvvm.cluster.arrive.relaxed` and `nvvm.cluster.wait`. Plain barriers always use the relaxed arrive form: release ordering comes from the mbarrier-init prelude on newer hardware and from the CTA-local barrier where that scope requires it.

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
```

## DSMEM Transaction Handshake

The DSMEM transaction handshake is the cluster-sync variant that carries a transaction-byte payload. It extends the plain arrive/wait pair with a peer-CTA address translation, an mbarrier `expect_tx` publication, and a master-lane phase flip — all before the cluster arrive.

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
| `nvvm.cluster.wait` | Wait until every participating CTA reaches the same point. |

```mlir
%dsmem_ptr = nvvm.mapa %smem_ptr, %peer_ctarank : !llvm.ptr<3>
%gen_ptr   = llvm.addrspacecast %dsmem_ptr     : !llvm.ptr<3> to !llvm.ptr
llvm.inline_asm "fence.release.cluster;"          // when the upstream release flag is set
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

Without a multi-CTA parent the DSMEM operations are skipped and the lowering emits only the arrive/wait tail. The release mode controls the arrive opcode: when an upstream `fence.release.cluster;` is already in place the lowering uses `nvvm.cluster.arrive.relaxed`; otherwise it can use the aligned arrive form directly.

```c
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

The ordering invariant is: publish the DSMEM transaction expectation before cluster arrive, toggle the phase only on the master lane, and pair every cluster arrive with a cluster wait for multi-CTA rendezvous. Reversing the order — arriving before publishing the transaction count — races peer CTAs that read the mbarrier as part of the arrive completion.

## Cross-References

[mbarrier State Machine](mbarrier-state-machine.md) covers the barrier object itself: arrival semantics, phase parity, and the transaction-byte field this page consumes.
[Blackwell 2-CTA and 4-CTA MMA](blackwell-2cta-and-4cta-mma.md) is the producer of the multicast S2T copy whose transaction-byte count drives the consumer-side handshake here.
[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) describes the TMEM allocator and instructions whose 2-CTA cooperative MMA variant rides on top of this rendezvous.
[Atomic, Warp, Sreg, Fence Emission](../codegen/atomic-warp-sreg-fence.md) documents the PTX printer for `cluster.arrive`, `cluster.wait`, and `fence.mbarrier_init.release.cluster`.
