# mbarrier State Machine

## Abstract

An `mbarrier` is a 64-bit transactional barrier object that lives in shared memory and synchronises a fixed set of arrival participants with an arbitrary count of in-flight transactions. It is the SM80-and-later primitive that decouples asynchronous data movement from compute: TMA loads, Hopper WGMMA, Blackwell tcgen05, and the entire `cutlass.pipeline` producer/consumer scaffold all observe completion through one of these objects. This page is the canonical reference for the mbarrier state machine — initialization, arrival, transaction tracking, phase parity, and invalidation — and for the 21-op NVVM family that touches it.

This page supersedes the scattered mbarrier paragraphs in [Atomic, Warp, Sreg, Fence Emission](../codegen/atomic-warp-sreg-fence.md) (the 21-op printer table), [tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) (the finalize-phase fragment), [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) (the transactional handshake), [Pipeline and Tile Scheduler](../dialects/cutlass/pipeline-and-tile-scheduler.md) (the producer/consumer step function), and [Seq-Bar and Block-Striped](../dialects/cutlass/seq-bar-and-block-striped.md) (the ring-of-slots view). Those pages now defer here for the mechanism itself.

## NamedBarrier Is a Different Thing

Tileiras code paths and CUTLASS-style kernels routinely reach for two synchronization primitives whose surface vocabulary overlaps. They are structurally distinct and must not be conflated.

A NamedBarrier is one of the 16 hardware `bar.sync` slots per CTA. Allocation is static: [Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) reserves a 32-bit slot vector in Phase 2 and hands each producer/consumer pair one slot. The synchronization model is warp-cooperative — all participating warps `bar.sync N, count` against the same slot id, and the barrier releases when `count` arrivals have accumulated. There is no transaction tracking, no shared-memory storage, no phase bit. Slots can be reused across disjoint lifetimes but not at one program point. The `cutlass.bar` op and the `nvvm.bar.cta.sync` family print into NamedBarrier slots.

An mbarrier is a shared-memory object — a 64-bit word at an aligned SMEM address — that carries explicit state: an `arrive_count`, an `expected_txn` byte count, a phase bit, and a current count. Synchronization is by polling: a consumer issues `mbarrier.try_wait.parity` against an expected phase, and the hardware reports completion when both arrivals and transactions have reached their targets. There is no shared hardware slot, no warp-cooperative constraint, and no static allocation table — every kernel instantiates as many mbarriers as it wants, subject only to SMEM capacity. The `nvvm.mbarrier.*` family operates on these objects.

The two primitives often appear in the same kernel: a `cutlass.pipeline` producer typically arrives on an mbarrier (to publish a TMA-completed tile) and on a NamedBarrier (to synchronise its warp group), and the buffer-assignment pass binds both kinds of slot for the same pipeline value. They remain distinct mechanisms.

> ⚡ **QUIRK — NamedBarrier and mbarrier share vocabulary but no mechanism**
> "Barrier" appears on both sides — both objects live in SMEM-adjacent storage, both gate producer/consumer regions, and both end up bound by the same pass for the same pipeline value. They are otherwise unrelated: NamedBarrier is one of 16 statically allocated CTA-wide `bar.sync` slots with a warp-cooperative count gate, mbarrier is a 64-bit transactional object with arrive/expect-tx/parity polling. Reusing one's idioms on the other (a polling wait on a NamedBarrier, a `bar.sync` arrival on an mbarrier) does not type-check and produces nothing resembling synchronisation if it slips past the front end.

## State Machine

An mbarrier carries four fields packed into one shared-memory 64-bit word:

```c
typedef struct MBarrier {
    uint32_t arrive_count;     /* remaining producer arrivals before this phase completes */
    uint32_t expected_txn;     /* expected transaction-byte count, 0 for ordinary barriers */
    uint32_t txn_count;        /* current transaction-byte total */
    uint32_t phase  : 1;       /* parity bit, flips on completion                          */
    uint32_t pending: 31;      /* current arrivals remaining                               */
} MBarrier;
```

Hardware-visible state advances through five operations: init, arrive, arrive-with-expect-tx, try-wait-parity, and inval. The producer side decrements `pending` (and optionally publishes a transaction byte count); the consumer side polls until completion; the phase bit flips on each completion and re-arms the barrier for the next round.

```c
void mbarrier_init(MBarrier *b, uint32_t count) {
    b->arrive_count = count;
    b->pending      = count;
    b->expected_txn = 0;
    b->txn_count    = 0;
    b->phase        = 0;
}

void mbarrier_arrive(MBarrier *b) {
    if (atomic_fetch_sub(&b->pending, 1) == 1) {
        atomic_store(&b->pending, b->arrive_count);
        b->phase ^= 1;
    }
}

void mbarrier_arrive_expect_tx(MBarrier *b, uint32_t tx_bytes) {
    b->expected_txn = tx_bytes;
    mbarrier_arrive(b);
}

bool mbarrier_try_wait_parity(MBarrier *b, uint32_t want_phase) {
    return b->phase == want_phase
        && b->pending == b->arrive_count
        && b->txn_count >= b->expected_txn;
}
```

The hardware implementation is atomic and lock-free, but a reimplementation only needs to preserve three invariants: `pending` decrements toward zero, the phase bit flips on the decrement that reaches zero (re-arming `pending`), and `try_wait.parity` releases only when both the arrival side and the transaction side have caught up.

## Phase Parity

A consumer that waits on the same mbarrier across loop iterations cannot ask "is the barrier complete?" — the answer is yes between every two iterations. It asks "has the barrier flipped to phase `p`?". The producer flips phase on arrival, the consumer reads the phase it expects to see, and the wait succeeds exactly when the producer's flip and the consumer's expectation agree.

In practice each pipeline agent carries a one-bit phase counter that toggles on every wraparound of its stage index. For a depth-`D` pipeline the phase of stage `s` at iteration `i` is `(i / D) & 1`; the producer pre-arms phase `((i / D) ^ 1) & 1` (the slot's "next-empty" parity) and the consumer waits for phase `(i / D) & 1` (the slot's "now-full" parity). This is why `cutlass.pipeline.state` carries both `index` and `phase` — the phase is what makes a single barrier slot reusable across iterations without ABA hazards.

## Kinds: Ordinary, Transaction, Cluster

Three kinds of mbarrier appear in TileIR:

- **Ordinary**. `expected_txn` is implicitly 1 (or zero, with the count-only path), and only `arrive_count` participants need to arrive. The `cutlass.bar` and `seq_bar` paths use this kind. Lowering emits `nvvm.mbarrier.arrive` or `nvvm.mbarrier.arrive.nocomplete`.

- **TMA transaction**. `expected_txn` is the byte count the TMA copy will deliver — `32 * size_minor` for a tiled TMA load of `size_minor` elements per minor dimension. The producer announces the expectation with `nvvm.mbarrier.arrive.expect_tx` before issuing the TMA instruction; the TMA copy then updates `txn_count` asynchronously, and the consumer's `try_wait.parity` releases only once both the arrival side and the transaction-byte side complete. This is the kind that ties `cp.async.bulk.tensor` to the consumer's WGMMA or tcgen05 instruction.

- **Cluster transaction**. The cross-CTA variant. The producer maps the barrier into a peer CTA's distributed shared memory through `nvvm.mapa`, publishes `expected_txn` via `nvvm.mbarrier.txn` (the cluster-scope expect-tx op), then participates in `cluster.arrive` / `cluster.wait`. The DSMEM handshake on [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) documents the rendezvous; the mbarrier state-machine view is just that the transaction byte count is published on a peer-CTA mbarrier rather than a local one.

## The 21-Op NVVM Family

The `nvvm` dialect exposes 21 ops that touch mbarrier state. They cover initialization, three arrive variants by transaction kind, two wait variants by blocking semantics, plus invalidation and the shared-memory specialisations of each. Lowering picks the `.shared` form when the barrier address space is 3 and the generic form otherwise.

| State-machine role | NVVM op | PTX |
|---|---|---|
| init / inval | `nvvm.mbarrier.init` / `.shared` | `mbarrier.init[.shared].b64 [%p], %n;` |
| init / inval | `nvvm.mbarrier.inval` / `.shared` | `mbarrier.inval[.shared].b64 [%p];` |
| init / inval | `nvvm.fence.mbarrier.init` | `fence.mbarrier_init.release.cluster;` |
| arrive (count only) | `nvvm.mbarrier.arrive` / `.shared` | `mbarrier.arrive[.shared].b64 %r, [%p];` |
| arrive (count, no-complete) | `nvvm.mbarrier.arrive.nocomplete` / `.shared` | `mbarrier.arrive.noComplete[.shared].b64 %r, [%p], %cnt;` |
| arrive (expect-tx, local) | `nvvm.mbarrier.arrive.expect_tx` / `.shared` | `mbarrier.arrive.expect_tx[.shared].b64 %r, [%p], %tx;` |
| arrive (expect-tx, cluster) | `nvvm.mbarrier.txn` | `mbarrier.expect_tx{.relaxed}.{cta|cluster}.b64 [%p], %tx;` |
| wait (parity, blocking) | `nvvm.mbarrier.wait` | `mbarrier.wait[.parity].b64 %r, [%p][, %par];` |
| wait (parity, polling) | `nvvm.mbarrier.try_wait.parity.shared` | `mbarrier.try_wait.parity.shared.b64 %p, [%mbar], %ph, %ns;` |
| wait (test) | `nvvm.mbarrier.test.wait` / `.shared` | `mbarrier.test_wait[.shared].b64 %r, [%p], %token;` |

The same 21 ops cover the address-space split: each ordinary form has a `.shared` variant chosen by the rewriter when the barrier lives in address space 3. The `nvvm.mapa` op is not in this family — it translates a shared pointer into a peer CTA's DSMEM address — but always appears upstream of a cluster `mbarrier.txn` because no other instruction reaches a remote mbarrier object.

## Cluster Init Fence

When the barrier object crosses a CTA boundary, the producer must publish the initialisation before any peer can observe it. Hopper and Blackwell expose `fence.mbarrier_init.release.cluster` for exactly this purpose. The prelude pattern is:

```mlir
%bar = memref.get_global @__shared_mbarrier : memref<...>
nvvm.mbarrier.init.shared %bar, %count : i32
nvvm.fence.mbarrier.init                          // cluster-visible publish
nvvm.cluster.arrive.relaxed { aligned }
nvvm.cluster.wait           { aligned }
```

Older targets (sm_70 / sm_80) skip the fence — there is no cross-CTA visibility to guarantee. The fence is also unnecessary for purely intra-CTA mbarriers; only the cross-CTA path needs it.

## Diagnostic Strings

The mbarrier verifier and lowerings emit these verbatim binary messages:

- `" must be mbarrier barrier type, but got "` — the typed-operand trait reports a non-mbarrier SSA type; the printed type name follows the trailing space.
- `"Only acquire/relaxed ordering supported for MBarrierWaitOp."` (and the parallel `MBarrierWaitParityOp.` / `MBarrierTryWaitTimeLimitOp.` / `MBarrierTryWaitParityTimeLimitOp.` variants) — the memory-ordering attribute is outside the acquire / relaxed set.
- `"using transaction mbarrier is not supported"` — a transaction-mbarrier was used on a code path that has not been wired up to the txn family.
- `"mbarrier has wait-like users, cannot share pipeline buffer."` — the alias pass refuses to fold a buffer shared with a wait-side user.
- `"Invalid TxnKind in MBarrierTransactionCTASpaceOp."` — the transaction kind enum carries an unsupported value.
- Lowering-time failures: `"failed to find smem buffer address for mbarrier"`, `"failed to find address of omitted mbarrier"` (note: the binary also carries the misspelled twin `"failed to find address of ommited mbarrier"`), `"failed to init mbarrier"` / `"Failed to init mbarrier"`, `"failed to setup mbarrier"`, `"failed to get MBarrier object"`.

The lowering rejects mismatched-address-space combinations before the printer fires, so the final PTX template never has to recover from a malformed modifier word.

## Cross-References

[Buffer Assignment and Named-Barrier Binding](../scheduler/buffer-assignment-and-mbarriers.md) documents the 32-slot NamedBarrier pool that this page disambiguates from mbarrier; both kinds end up in the same value record but are different mechanisms.
[Pipeline and Tile Scheduler](../dialects/cutlass/pipeline-and-tile-scheduler.md) builds its producer/consumer step function on top of the state machine above; its `try_wait.parity` calls and `arrive.expect_tx` calls land verbatim in the NVVM ops listed here.
[Seq-Bar and Block-Striped](../dialects/cutlass/seq-bar-and-block-striped.md) wraps the same primitives into an ordered ring with a phase cursor and uses arrives plus parity waits exactly as documented above.
[WGMMA Emission Protocol — The Four-Op Sequence](wgmma-emission-protocol.md#the-four-op-sequence) is the consumer side of the TMA-transaction kind: the WGMMA wait-group sequence runs after `try_wait.parity` succeeds on the producer's mbarrier.
[tcgen05 Tensor Memory Model — Tensor Memory](tcgen05-tensor-memory-model.md#tensor-memory) uses cluster-transaction mbarriers to coordinate the 2-CTA and 4-CTA TMEM staging copies that precede each MMA.
[Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) extends the transaction kind across CTA boundaries with peer-CTA address translation and the cluster arrive/wait rendezvous.
[Concurrency and Sync Semantics](concurrency-and-sync-semantics.md) frames `mbarrier.arrive.expect_tx` and `mbarrier.try_wait.parity` as the implicit `release`/`acquire` pair at the heart of the producer/consumer pipeline ordering story.
[Atomic, Warp, Sreg, Fence Emission](../codegen/atomic-warp-sreg-fence.md) lists the PTX-print form of every mbarrier op alongside the fence and warp-collective families.
[tcgen05 / WGMMA / mbarrier / Cluster Emission](../codegen/tcgen05-wgmma-mbarrier-cluster.md) covers the backend-side validation that runs once the NVVM op has been selected.
[DSL to PTX End-to-End](dsl-to-ptx-end-to-end.md) shows the transaction-kind mbarrier in flight — Stage 3 carries the producer/consumer rendezvous as `async.pipeline.producer_commit` / `consumer_wait` tokens, Stage 4 lowers them to `nvvm.mbarrier.init.shared` plus a parity-encoded `try_wait.parity.shared`, Stage 5 surfaces `MBARRIER_TRY_WAIT_PARITY_SHARED` in MIR, and Stage 6 emits the `mbarrier.try_wait.parity.shared.b64` retry loop with explicit `@!%p bra` fallback.
