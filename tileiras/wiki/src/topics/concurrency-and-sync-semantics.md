# Concurrency and Synchronization Semantics

## Abstract

NVIDIA GPUs expose a layered memory model: SIMT lockstep within a warp, explicit synchronization between warps in a CTA, cluster-scope synchronization between CTAs on SM90 and newer, and device- or system-scope ordering for atomics that cross those boundaries. Every memory operation tileiras emits carries an explicit `(semantic, scope)` pair, and that pair is what fixes the operation's position in the layered model. The pair survives every lowering stage from `cuda_tile` and `nv_tileaa` TileIR down to PTX: the `mem_semantic` and `mem_scope` attributes on the IR op map directly onto the `.sem` and `.scope` modifiers in the printed PTX, and the verifier rejects any combination that the printer cannot legally emit.

This page is the canonical reference for that pair: how the four scopes nest in the execution hierarchy, what the five semantics promise about ordering, which `(semantic, scope)` combinations each operation family accepts, and how the worked release-acquire pair fits into a producer/consumer pipeline that crosses the cluster boundary.

## The Four Scopes

A scope answers the question "which set of threads is required to observe the ordering this operation establishes?" The four scopes nest strictly: CTA is contained in cluster, cluster is contained in GPU, GPU is contained in system. A wider scope subsumes the visibility guarantee of every narrower scope but costs more cycles to enforce, because the hardware has to push or pull traffic across more of the on-chip network.

| Scope | PTX modifier | Visibility | Cost (~cycles) |
|---|---|---|---|
| CTA | `.cta` | threads in one CTA | 1-10 |
| Cluster | `.cluster` | CTAs in one cluster (SM90+) | 10-50 |
| GPU / device | `.gpu` | every SM on one device | 100-1000 |
| System | `.sys` | NVLink-coherent multi-GPU and host memory | 1000+ |

A few operations also accept the compound `.cta::cluster` scope. That form means the same as `.cluster` for visibility but lets the hardware take a shorter path when the operand address turns out to be local to the current CTA — the cluster tail is conditional on the address. Tileiras emits the compound only when the operand's address-space inference proves the access may target a peer CTA's distributed shared memory.

Below SM90 the cluster tier does not exist; the hierarchy collapses to CTA / GPU / system, and any IR op carrying `mem_scope = cluster` must be rewritten or rejected before printing.

## The Five Semantics

A semantic answers the question "what other memory operations are ordered relative to this one?" The five values form a lattice: `relaxed` is the weakest, `sc` is the strongest, and the three middle values (`acquire`, `release`, `acq_rel`) are partially ordered with respect to each other.

| Semantic | PTX modifier | Meaning | Typical use |
|---|---|---|---|
| relaxed | `.relaxed` | atomicity only; no inter-thread ordering | counters, statistics, lock-free queues with hand-rolled fences |
| acquire | `.acquire` | subsequent loads/stores see writes that happened before the matching release | consumer side of a producer/consumer handoff |
| release | `.release` | prior loads/stores are visible after this op to any thread that performs a matching acquire | producer side of a producer/consumer handoff |
| acq_rel | `.acq_rel` | both acquire and release; only legal on RMW operations | atomic counters that gate both publication and consumption |
| sc / seq_cst | `.sc` (on fences) | total order across every `sc`-ordered op of equal-or-wider scope | rarely emitted; tileiras prefers explicit `acq_rel` pairs |

A pure `ld` cannot carry `.release` and a pure `st` cannot carry `.acquire` — the verifier rejects either combination before printing. The `acq_rel` semantic is reserved for RMW (`atom.*`) instructions and the corresponding fence forms. Sequential consistency is supported by the PTX `fence.sc.{scope}` instruction; tileiras emits it only when an upstream TileIR op carries `mem_semantic = sc` and the optimizer cannot prove a weaker form suffices.

## Scope-Semantic Matrix per Operation

The TileIR opset partitions memory effects into five families. Each family takes a different subset of `(semantic, scope)` pairs, and the verifier knows which subsets each op family accepts.

| Op family | Takes semantic | Takes scope | Notes |
|---|---|---|---|
| `atom.*` (RMW, CAS) | yes — all five | yes — `cta / cluster / gpu / sys` | scope required when semantic > `relaxed` |
| `ld` (load) | yes — `relaxed / acquire` | yes — `cta / cluster / gpu / sys` | scope is required when semantic is `acquire` |
| `st` (store) | yes — `relaxed / release` | yes — `cta / cluster / gpu / sys` | scope is required when semantic is `release` |
| `fence.*` | yes — `acquire / release / acq_rel / sc` | yes — `cta / cluster / gpu / sys` | scope is always required |
| `mbarrier.*` | implicit | implicit | scope dictated by the mbarrier's address space; cluster forms use `mbarrier.expect_tx.cluster` |
| `cp.async.bulk.*` | implicit | implicit | ordering flows through the completion mbarrier paired with the copy |

The TileAA verifier hard-codes one structural rule that applies across families: a non-`weak` semantic requires a scope, and a `weak` semantic must not carry a scope. This rule is what the `verify_memory_op_common` predicate in [nv_tileaa Operation Roster](../dialects/nv_tileaa/op-roster.md#memory-effects) enforces.

```c
LogicalResult verify_memory_ordering(MemoryOp op) {
    MemSemantic sem = op.mem_semantic();
    Optional<MemScope> scope = op.mem_scope();

    if (sem == WEAK) {
        require(!scope.has_value(),
                "weak memory ordering must not carry a scope");
        return success();
    }

    require(scope.has_value(),
            "non-weak memory ordering requires explicit scope");

    if (op.is_load_only()) {
        require(sem == RELAXED || sem == ACQUIRE,
                "loads accept only relaxed or acquire");
    } else if (op.is_store_only()) {
        require(sem == RELAXED || sem == RELEASE,
                "stores accept only relaxed or release");
    }

    return success();
}
```

The matrix is asymmetric on purpose: only RMW operations support `acq_rel`, because only a single atomic instruction can plausibly establish a happens-before edge in both directions at once.

## How Tileiras Chooses

The frontend produces tile-IR ops with `memSemantic` and `memScope` attributes attached at construction. The defaults are conservative — `sc` and `sys` — and the contract is that lowering may strengthen but never weaken the pair. In practice the frontend overrides the defaults at every site where the user-supplied source carries a weaker promise: a `tl.atomic_add` with `sem="relaxed", scope="gpu"` lowers to a `cuda_tile.atomic_rmw` with the same pair, which lowers to `nv_tileaa.atomic_rmw` with the same pair, which lowers to `nv_tileas.atomic_rmw` with the same pair, which finally prints as the matching PTX modifier.

```text
tl.atomic_add(p, v, sem="relaxed", scope="gpu")
        │
        ▼ tile-IR construction
cuda_tile.atomic_rmw add, ... { mem_semantic = relaxed, mem_scope = gpu }
        │
        ▼ cuda_tile to tileaa lowering
nv_tileaa.atomic_rmw add, ... { mem_semantic = relaxed, mem_scope = gpu }
        │
        ▼ tileaa to tileas lowering
nv_tileas.atomic_rmw add, ... { mem_semantic = relaxed, mem_scope = gpu }
        │
        ▼ tileas to NVVM/LLVM lowering, PTX emission
atom.relaxed.gpu.add.u32.global [%rd0], %r1;
```

Each stage's converter copies the attribute pair through `addStoreAttribute` / `addLoadAttribute` helpers without re-deriving them. The only stage that strengthens the pair is the safety pass that detects an `mbarrier.expect_tx` without a matching upstream release, which inserts an explicit `fence.release.cluster` instead of demoting the cluster transaction to a weaker form.

## CTA-Scope Sync Primitives

Within a CTA, threads synchronize through three mechanisms. The choice depends on whether the rendezvous is warp-cooperative, count-only, or transactional.

- `bar.sync 0` — the implicit barrier; every thread in the CTA arrives, every thread waits. This is the cheapest CTA-wide rendezvous and the only one safe to emit when the warp count is unknown.
- `bar.sync N` for `N = 1..15` — a named barrier slot with an explicit participant count. The 16-slot pool is allocated at compile time by the buffer-assignment pass and bound to specific producer/consumer pairs. See [Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) for the slot pool's allocation discipline.
- `mbarrier.*` — a 64-bit transactional barrier object in shared memory. Unlike `bar.sync`, an mbarrier carries explicit state (arrival count, expected transaction byte count, phase parity) and is polled instead of blocked on. The full state machine is documented in [mbarrier State Machine](mbarrier-state-machine.md).

A NamedBarrier and an mbarrier are structurally distinct primitives that often coexist in the same kernel — a `cutlass.pipeline` producer typically arrives on an mbarrier to publish a TMA-completed tile and on a NamedBarrier to synchronize its warp group. The two never substitute for each other.

## Cluster-Scope Sync Primitives

Above the CTA, the only hardware-supported rendezvous is the cluster arrive/wait pair. The producer side issues `cluster.arrive.relaxed`, the consumer side issues `cluster.wait`, and the rendezvous completes when every participating CTA has arrived. The DSMEM transaction variant additionally publishes a transaction byte count on a peer CTA's mbarrier through `mbarrier.expect_tx.cluster` before arriving, so the rendezvous completes only when both the arrival count and the transaction byte count clear.

The full cluster-side protocol — peer-CTA address translation through `nvvm.mapa`, the `fence.release.cluster` ordering prelude, master-lane phase-bit handoff, and the arrive/wait tail — is documented in [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md). The scope-semantic view of that protocol is straightforward: the producer-side `mbarrier.expect_tx.cluster` carries an implicit release scoped to `cluster`, and the consumer-side `mbarrier.try_wait.parity` (after the cluster wait completes) carries an implicit acquire of equal scope.

## Race Patterns and the Verifier

Race-freedom is not decidable from IR alone — the verifier cannot enumerate every interleaving of threads and ranks. What it can do is reject structural patterns that are racy by construction, where the IR provably lacks the synchronization edge the operation needs. Three such patterns are checked at TileAA verification time.

The first is a scope/address-space mismatch: an atomic with `mem_scope = cta` on an `addrspace(1)` (global) pointer is suspicious, because CTA-scope atomicity cannot enforce visibility across the device-wide L2 path that a global access takes. The verifier emits a diagnostic; the optimizer either widens the scope to `gpu` or rejects the program.

The second is an mbarrier with `expect_tx` but no matching upstream `arrive.expect_tx`: the consumer is waiting for a transaction byte count that no producer will ever publish, and the rendezvous deadlocks. The verifier walks the local dataflow graph backwards from the wait site to confirm that an arrive-with-tx producer dominates it.

The third is a WGMMA without the preceding `wgmma.fence.sync.aligned`: the warp group reads SMEM through the descriptor before the producer's SMEM writes are guaranteed visible, which races. The [WGMMA Emission Protocol](wgmma-emission-protocol.md) documents the four-op sequence that the verifier enforces.

None of these checks proves the program race-free; they only reject the structural patterns where racing is the only possible outcome. Programs that race through more subtle channels — false sharing, ABA on a reused mbarrier slot, an atomic counter with wrong scope across a launch-cooperative boundary — pass the verifier and fail at runtime.

## Worked Example: Producer-Consumer Pipeline Ordering

Consider a three-stage software-pipelined GEMM loop: a TMA load fetches an A-tile and a B-tile from global memory into shared memory, a barrier publishes the SMEM stage, and a WGMMA consumer reads through the SMEM descriptor and accumulates into TMEM. The pipeline crosses three sync tiers (per-stage TMA completion, per-CTA SMEM publication, per-warp-group WGMMA fence) and forms one valid release-acquire pair at the SMEM boundary.

The four steps in one iteration of the steady-state loop:

1. The producer warp issues `cp.async.bulk.tensor.shared::cluster.global.mbarrier::complete_tx::bytes`. The asynchronous bulk copy reads global memory and writes the destination SMEM stage. Ordering flows through the mbarrier passed in `mbarrier::complete_tx::bytes`: the copy will increment the mbarrier's transaction byte count when its writes are visible to every thread that polls the mbarrier on the consumer side.

2. The producer warp issues `mbarrier.arrive.expect_tx.shared.b64 %tok, [%mbar], %tx`. This publishes the expected transaction byte count and arrives at the same time. The semantic-scope view: this op carries an implicit `release` semantic at `cta` scope (or `cluster` if `%mbar` lives in a peer CTA's DSMEM, in which case the printer emits `mbarrier.expect_tx.cluster.b64`). Any prior writes from this warp — including the TMA payload itself, asynchronously — are guaranteed visible to a consumer that subsequently acquires the same mbarrier.

3. The consumer warp issues `mbarrier.try_wait.parity.shared.b64 %p, [%mbar], %ph, %ns`. The op carries an implicit `acquire` semantic at `cta` (or `cluster`) scope. The wait succeeds only when both the arrival count and the transaction byte count have cleared, at which point every write that the producer ordered through this mbarrier is visible to the consumer.

4. The consumer warp issues `wgmma.fence.sync.aligned` followed by the WGMMA instruction(s) and `wgmma.commit_group.sync.aligned`. The fence is required because the WGMMA reads SMEM through the descriptor outside the normal load/store path, so the producer-to-consumer `release`/`acquire` edge through the mbarrier needs an extra fence to be visible to the WGMMA pipeline specifically. See [WGMMA Emission Protocol — The Four-Op Sequence](wgmma-emission-protocol.md#the-four-op-sequence) for the full sequence.

The release-acquire pair at the SMEM boundary is `mbarrier.arrive.expect_tx` (release) paired with `mbarrier.try_wait.parity` (acquire), both implicitly scoped to the mbarrier's address space. The pair satisfies the layered memory model: the consumer's reads see the producer's writes, including the asynchronous TMA payload, and the optimizer is allowed to reorder the producer's tile-compute and the consumer's tile-compute across the boundary as long as it never sinks operations past their release or hoists them past their acquire.

```text
        producer warp                                consumer warp
           │                                              │
           │  cp.async.bulk.tensor  → SMEM stage          │
           │  mbarrier.arrive.expect_tx  (release, cta)   │
           │                                              │
           │                                              ▼
           │                            mbarrier.try_wait.parity  (acquire, cta)
           │                            wgmma.fence.sync.aligned
           │                            wgmma.mma_async  ← reads SMEM via descriptor
           │                            wgmma.commit_group.sync.aligned
```

If `%mbar` is mapped into a peer CTA's DSMEM through `nvvm.mapa`, the same diagram describes a cluster-scope handshake: the `release` widens to `cluster`, the `acquire` widens to `cluster`, and the rendezvous spans every participating CTA in the cluster. The mechanism is unchanged; only the scope modifier on the printed PTX changes.

## Cross-References

[GPU Execution Model](gpu-execution-model.md) establishes the five-tier hierarchy (thread / warp / CTA / cluster / grid) whose tiers this page's scopes nest into.
[mbarrier State Machine](mbarrier-state-machine.md) covers the barrier object whose `arrive.expect_tx` and `try_wait.parity` ops carry the implicit release/acquire pair documented above.
[Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) is the cluster-scope counterpart: peer-CTA address translation, the `fence.release.cluster` prelude, and the cluster arrive/wait tail.
[WGMMA Emission Protocol](wgmma-emission-protocol.md) is the consumer side of the worked example: the warp-group fence, the MMA op, the commit, and the matching wait-group.
[nv_tileaa Operation Roster — Memory Effects](../dialects/nv_tileaa/op-roster.md#memory-effects) catalogues the `mem_semantic` and `mem_scope` attribute slots on every memory-effect op.
[Atomic, Warp, Sreg, Fence Emission](../codegen/atomic-warp-sreg-fence.md) prints the final PTX form: the modifier ordering on `atom.*`, the `fence.*` family, and the cluster-scope mbarrier variants.
[Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) allocates the 16-slot CTA-scope NamedBarrier pool that complements the mbarrier-based rendezvous discussed here.
