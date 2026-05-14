# cutlass Pipeline and Tile Scheduler

## Abstract

`cutlass.pipeline.*` and `cutlass.tile_scheduler.*` solve the two large-scale orchestration problems in CUTLASS-style GEMM kernels: how producer and consumer agents coordinate asynchronous work, and how CTAs receive tiles of the output problem. The pipeline family covers stage state, barriers, producer acquire/commit, consumer wait/release, and executor switching. The tile-scheduler family covers data-parallel, static-persistent, and StreamK work assignment, including workspace-based fixup for partial K splits.

The rest of this page documents the contracts and algorithms a lowering must preserve.

## Pipeline Model

A CUTLASS pipeline is a staged producer/consumer state machine. Each stage has a
barrier-like slot, a phase bit, an index, and a participant policy.

```c
typedef struct {
    int phase;
    int index;
    int count;
} PipelineState;

PipelineState pipeline_state_increment(PipelineState state, int stage_count) {
    state.index += 1;

    if (state.index == stage_count) {
        state.index = 0;
        state.phase ^= 1;
    }

    state.count += 1;
    return state;
}
```

The main handshake is:

1. Producer acquires an empty stage.
2. Producer issues async work for that stage.
3. Producer commits, optionally with expected transaction bytes.
4. Consumer waits for that stage.
5. Consumer reads the produced data.
6. Consumer releases the stage so it can be reused.

```c
void lower_pipeline_handshake(Pipeline pipeline, Stage stage) {
    Value slot_addr = pipeline.barrier_addr(stage.index);

    // Producer acquire — wait until the slot is empty.
    emit("nvvm.mbarrier.try_wait.parity.shared",
         /*addr=*/slot_addr,
         /*phase=*/stage.phase ^ 1,
         /*timeout=*/k_default_timeout);

    emit_producer_body(stage);

    // Producer commit — arrive with expect_tx for TMA-backed producers,
    // or plain arrive for non-TMA work.
    if (stage.transaction_bytes > 0)
        emit("nvvm.mbarrier.arrive.expect_tx.shared",
             slot_addr, /*tx_bytes=*/stage.transaction_bytes);
    else
        emit("nvvm.mbarrier.arrive.shared",
             slot_addr, /*count=*/pipeline.num_producers);

    // Consumer wait — wait until the slot is full.
    emit("nvvm.mbarrier.try_wait.parity.shared",
         slot_addr, /*phase=*/stage.phase,
         /*timeout=*/k_default_timeout);

    emit_consumer_body(stage);

    // Consumer release.
    emit("nvvm.mbarrier.arrive.shared",
         slot_addr, /*count=*/pipeline.num_consumers);
}
```

### Unified `pipeline_step` State Machine

The lowering above is per-stage. A compact way to specify the full state machine — what one iteration of one agent does given its current state and role — is the `pipeline_step` function below. A verifier or a model-checker can read this directly: every transition on every role is explicit, and the only side channel between roles is the barrier slot at `state.index % depth`.

```c
typedef enum { ROLE_PRODUCER, ROLE_CONSUMER } AgentRole;

PipelineState pipeline_step(Pipeline p, AgentRole role,
                            PipelineState state, StageBody body) {
    Value slot = p.barrier_addr(state.index);

    switch (role) {
    case ROLE_PRODUCER:
        // 1. acquire — empty-side parity is the inverse of full-side parity.
        emit("nvvm.mbarrier.try_wait.parity.shared", slot, state.phase ^ 1);
        // 2. issue async work (TMA / async copy / WGMMA / ...).
        run_producer_body(body);
        // 3. commit — arrive with expect_tx for TMA-backed producers.
        if (body.transaction_bytes > 0)
            emit("nvvm.mbarrier.arrive.expect_tx.shared",
                 slot, /*tx_bytes=*/body.transaction_bytes);
        else
            emit("nvvm.mbarrier.arrive.shared",
                 slot, /*count=*/p.num_producers);
        break;

    case ROLE_CONSUMER:
        // 1. wait — spin until the slot is full (parity matches state.phase).
        emit("nvvm.mbarrier.try_wait.parity.shared", slot, state.phase);
        // 2. read the stage's SMEM / TMEM / register fragments.
        run_consumer_body(body);
        // 3. release — arrive on the empty-side counter.
        emit("nvvm.mbarrier.arrive.shared", slot, /*count=*/p.num_consumers);
        break;
    }

    // 4. advance: increment index, flip phase on wrap.
    return pipeline_state_increment(state, p.stage_count);
}
```

Three invariants keep this state machine model-checkable: every transition is local to one (role, state) pair; the only inter-role communication runs through the barrier slot; and the parity-bit flip in `pipeline_state_increment` is what lets a single barrier slot be reused across stages without aliasing. Break any of the three and you lose the ability to prove progress and safety for the producer/consumer ring.

### If You Know CUTLASS (open source) — cross-walk

For readers fluent in the open-source `cutlass::PipelineTmaAsync<Stages>` and friends:

| CUTLASS C++ | tileiras IR |
|---|---|
| `PipelineTmaAsync<Stages>::producer_acquire(state)` | `cutlass.pipeline.producer.acquire %pipe, %state` |
| `PipelineTmaAsync<Stages>::producer_commit(state, bytes)` | `cutlass.pipeline.producer.commit %pipe, %state {transaction_bytes = N}` |
| `PipelineTmaAsync<Stages>::consumer_wait(state)` | `cutlass.pipeline.consumer.wait %pipe, %state` |
| `PipelineTmaAsync<Stages>::consumer_release(state)` | `cutlass.pipeline.consumer.release %pipe, %state` |
| `PipelineState<Stages>` member object | `!cutlass.pipeline_state` typed value with phase/index/count |
| `cutlass::arch::NamedBarrier::sync(id, threads)` | `cutlass.bar` op + warp-cooperative diagnostic |
| Cluster-wide barrier on Hopper / Blackwell | `cute_nvgpu.arch.cluster.barrier` op |
| Template parameter `Stages` | `numStages` attribute on `cutlass.pipeline.init` |
| Template parameter `ClusterShape` | `cluster_shape_x/y/z` fields on `CutlassTileSchedulerParams` |

Two differences are worth flagging. The IR carries `num_producers` and `num_consumers` as explicit attributes the verifier cross-checks against the participants list, where the C++ template collapses them into a single `ThreadCategory` enum. And the `executor` axis (warp-specialized vs cooperative) is an op-level attribute selected by `cutlass.pipeline.switch_by_executor` rather than a compile-time template specialisation.

## Pipeline Operations

| Operation area | Contract |
|---|---|
| `pipeline.create` | Allocate or bind the shared barrier storage for a staged pipeline. |
| `pipeline.init` | Initialize all stage barriers with the correct participant count. |
| `pipeline.state.create` | Construct the phase/index/count tuple for an agent. |
| `pipeline.state.increment` | Advance index and flip phase on wraparound. |
| `producer_acquire` / `producer_try_acquire` | Wait or probe for an empty producer stage. |
| `producer_commit` | Signal that produced data is ready, with transaction byte count when needed. |
| `producer_tail` | Drain outstanding WGMMA or async groups before leaving the mainloop. |
| `consumer_wait` / `consumer_try_wait` | Wait or probe for a ready consumer stage. |
| `consumer_release` | Signal that the consumer has released the stage. |
| `switch_by_executor` | Split a region by executor role or warp-specialized agent mask. |
| `async.exec` | Contain executor-specific regions that will become scheduled pipeline roles. |

`switch_by_executor` verifies that masks are contiguous, cover the enclosing executor set, and match the operand groups they select. It is a semantic partition, not just a branch.

## ConvertPipelineToNVVM

`ConvertPipelineToNVVM` rewrites every `cutlass.pipeline.*` op into a sequence of `nvvm.*` intrinsics. It runs after the MaterializeAsync pass (the D07 stage of the cutlass→nvvm pipeline) and is the single point where the abstract producer/consumer state machine becomes a concrete mbarrier program on shared memory. Every later pass sees only NVVM intrinsics for synchronisation.

The driver is `sub_15EC600`. Its `runOnOperation` body builds the conversion target — marking eight dialects fully legal (`builtin`, `arith`, `nvvm`, `llvm`, `scf`, `cute`, `cute_nvgpu`, `cutlass`) — then calls `sub_15E9940` to populate the pattern set and invokes `applyPartialConversion`. The target legality is partial on purpose: `cute` and `cutlass` stay legal so any op outside the pipeline family (TMA descriptors, WGMMA tiles, copy atoms) passes through untouched. One specific cute op, `cute_nvgpu.arch.make_warp_uniform`, is reserved as legal even within `cute` because the warp-uniformity anchor must survive past this pass — the downstream codegen relies on it to broadcast scheduler state across the warp.

The pattern set has 22 `OpLowering` subclasses, splitting into four functional clusters. Initialization: `PipelineInitOpLowering`, `BarrierInitOpLowering`, `PipelineSwitchByExecutorOpLowering`. Producer/consumer handshake: `PipelineProducerAcquireOpLowering`, `PipelineProducerCommitOpLowering`, `PipelineProducerTailOpLowering`, `PipelineConsumerWaitOpLowering`, `PipelineConsumerReleaseOpLowering`. State arithmetic: `PipelineStateOpLowering`, `PipelineStateIncrementOpLowering`, `PipelineStateBumpOpLowering`, `BarOpLowering`. Async-future plumbing: seven `AsyncWait*` and `AsyncFutureWait*` variants plus the cast bridges and the block-striped load/store helpers.

The table below lists each pattern with its `matchAndRewrite` slab address (where known), the vtable bank base under which the RTTI and method table live, the slab size in bytes, and the NVVM emit set the match produces.

| Pattern | matchAndRewrite | vtable bank base | Slab size | Emit set |
|---|---|---|---|---|
| PipelineInitOpLowering | (varies) | `0x59E4520` | 0x70 | `nvvm.mbarrier.init` + `nvvm.bar.cta.arrive.expect_tx` |
| PipelineSwitchByExecutorOpLowering | — | `0x59E4520` | 0x70 | conditional branch on executor mode |
| PipelineProducerAcquireOpLowering | `sub_15EFAB0` (17 KB) | `0x59E42F0` | 0x70 | 12-op emit set (see task #576) |
| PipelineProducerCommitOpLowering | — | `0x59E4340` | 0x78 | `nvvm.mbarrier.arrive.expect_tx` + arrives |
| PipelineProducerTailOpLowering | — | `0x59E4340` | 0x78 | `nvvm.cp.async.bulk.wait_group { count = 0 }` (fast path) or scf.for (slow path) |
| PipelineConsumerWaitOpLowering | — | `0x59E4570` | 0x70 | `nvvm.mbarrier.try_wait.parity.shared` spin loop |
| PipelineConsumerReleaseOpLowering | — | `0x59E4570` | 0x70 | `nvvm.mbarrier.arrive` |
| PipelineStateOpLowering | — | (varies) | 0x70 | builds the per-stage state |
| PipelineStateIncrementOpLowering | — | `0x59E45C0` | 0x70 | `arith.addi` plus modulo wrap |
| PipelineStateBumpOpLowering | — | `0x59E45C0` | 0x70 | sibling of above |
| BarOpLowering | `sub_15FC250` (~5.5 KB) | `0x59E4610` | 0x78 | named-barrier emission |
| BarrierInitOpLowering | — | `0x59E4660` | 0x70 | `nvvm.barrier0.init` |
| AsyncWaitOpConversionMbarrier | — | `off_59D5DD8` | 0x70 | `nvvm.mbarrier.try_wait.parity.shared` |
| AsyncWaitOpConversionTMASTGAndTMAREDG | — | `off_59D5E28` | 0x70 | TMA store-and-reduce wait |
| AsyncWaitOpConversionGMMA | — | `off_59D5E78` | 0x70 | `nvvm.wgmma.wait.group.sync.aligned` |
| AsyncToAsyncOpConversion | — | `off_59D5EC8` | 0x70 | `builtin.unrealized_conversion_cast` |
| CreateNoneOpConversion | — | `off_59D5F18` | 0x70 | `llvm.mlir.poison` |
| AsyncFutureWaitMbarrier | — | `off_59D5F68` | 0x70 | `nvvm.mbarrier.try_wait.parity.shared` (different spin form) |
| AsyncFutureWaitGroup | — | `off_59D5FB8` | 0x70 | `nvvm.cp.async.bulk.wait_group` |
| TokenToAsyncOpConversion | — | `off_59D6008` | 0x70 | `builtin.unrealized_conversion_cast` |
| BlockStripedLoadOpLowering | — | (varies) | 0x70 | `cute.block_striped_load` |
| BlockStripedStoreOpLowering | — | (varies) | 0x70 | `cute.block_striped_store` |

A few details in the table are worth unpacking. The vtable banks cluster patterns that share a base class: the three handshake-side acquire/wait patterns occupy the `0x59E42F0`/`0x59E4570` banks; the commit and tail patterns share `0x59E4340` with a slightly larger 0x78 slab to hold extra emit-set state; and the seven `AsyncWait`/`AsyncFutureWait` patterns occupy the contiguous `off_59D5DD8`–`off_59D6008` range. The 0x70 default slab is the standard `OpRewritePattern` footprint plus one type-converter pointer; the 0x78 patterns carry one extra field — usually a precomputed attribute (transaction byte count for commit, fast/slow-path flag for tail).

The producer tail emits `nvvm.cp.async.bulk.wait_group { count = 0 }` on the fast path — when the pipeline depth is known statically and all outstanding TMA stores can be drained with a single group wait. The slow path falls back to an `scf.for` loop that iterates over remaining stages and arrives on each barrier in turn, and gets taken when the analysis cannot prove a single-group drain is safe. The producer commit is the canonical `nvvm.mbarrier.arrive.expect_tx` site — the only place in the pass that emits an `expect_tx` attribute — and the byte count comes verbatim from the `producer_commit` op's transaction-bytes operand.

The two `try_wait.parity.shared` emit sets are not identical. `PipelineConsumerWaitOpLowering` emits the canonical spin form with a single phase operand and a fixed timeout. `AsyncFutureWaitMbarrier` emits a variant that pulls the phase from the future handle and uses a different timeout constant. The two paths cannot be unified because the future-handle form must survive a type round-trip through `builtin.unrealized_conversion_cast` — the same mechanism `AsyncToAsyncOpConversion` and `TokenToAsyncOpConversion` use to bridge the async-token type system before the LLVM lowering finally collapses the casts.

The pass must keep the state tuple coherent across the rewrite. If `PipelineStateIncrementOpLowering` lowers a wrap that does not flip phase, the resulting `try_wait.parity` observes stale barrier state on the very next iteration. The increment pattern emits `arith.addi` plus a modulo compare-and-select that XORs the phase bit on wrap; `PipelineStateBumpOpLowering` is a sibling pattern used when the pipeline carries a side-channel counter that must advance in lockstep without a phase flip.

## Producer Acquire 4-Phase Lowering

`PipelineProducerAcquireOpLowering` at `sub_15EFAB0` is the largest `OpLowering` in `ConvertPipelineToNVVM` — roughly 17 KB of compiled code. It splits cleanly into four phases: operand unpack, mask-matrix construction dispatched on the pipeline mode, per-stage arrive emission, and a NamedBarriers tail. All four phases share one rewriter and one running operand vector, so phase ordering is part of the contract; each phase reads state produced by the previous one.

### Phase A — Operand Unpack

The acquire op carries three operand bundles. The pipeline state record `%pipe` unpacks via `sub_15E0190(adaptor, k)` into `{phase, index, count, base_ptr}`. The per-stage record `%state` unpacks into `{phase, index}`. The last operand is an i1 `%unacquired_state` flag separating a first-time acquire from a re-entry — first-time entries skip the parity-flip path the steady-state phase masks use.

After the unpack, the lowering materialises two constants — `i32 0` and the parity-flip mask `i32 -2` — via `sub_170B220`, and reads the six pipeline shape fields `(P, C, num_producers, num_consumers, participants, mode)` through `sub_172E920`, `sub_172E930`, `sub_172E940`, `sub_172E950`, and `sub_172E980`. The participants field gives the matrix size; the mode field picks between five mask-construction strategies in Phase B.

```c
struct AcquireOperands {
    Value phase;
    Value index;
    Value count;
    Value base_ptr;
    Value state_phase;
    Value state_index;
    Value unacquired_state;
};

struct PipelineShape {
    uint32_t P;
    uint32_t C;
    uint32_t num_producers;
    uint32_t num_consumers;
    uint32_t participants;
    uint32_t mode;
};
```

### Phase B — 16×16 Mask Matrix Dispatch

Phase B builds four 16-element mask buffers — one per parity/role combination (producer-even, producer-odd, consumer-even, consumer-odd) — and dispatches on the pipeline mode read from `sub_172E7A0`. The 16-row buffer is sized for the worst-case participant matrix; smaller pipelines leave trailing rows zero.

```c
void phase_b_build_masks(Rewriter *r, PipelineShape s, MaskMatrix *out) {
    uint32_t mode = read_mode(s);

    switch (mode) {
    case MODE_COOPERATIVE_ARRIVAL:
        emit_cooperative_mask_reduction(r, s, out);
        break;
    case MODE_WARP_SPECIALIZED_1x1:
        emit_unrolled_select_or_chain(r, s, out);
        break;
    case MODE_STRIDED_UREM:
    case MODE_STRIDED_UDIV:
        emit_scf_for_strided_mask(r, s, mode, out);
        break;
    case MODE_CLUSTER:
        emit_cluster_arrive(r, s);
        break;
    }
}
```

Mode 0 is **CooperativeArrival**. The emitter walks the `P × C` participants matrix and, for each cell, emits a `llvm.extractvalue` to pull the participant slot, a `llvm.and` to apply the per-stage mask, an `llvm.icmp ne` against zero, a `llvm.select` to pick between the parity-flipped and unflipped mask, and a `llvm.or` reduction into the running mask. This collapses the per-cell predicates into a single per-mask u32 without any control flow — the entire matrix is straight-line code.

Mode 1 is **WarpSpecialized P=C=1**. Both dimensions are 1, so the participants matrix degenerates to a single column. The emitter unrolls a 16-stage select-and-or chain through `sub_15E0F00` and `sub_15E69E0` rather than building a matrix walk: fewer ops, fewer values, no per-cell extractvalues.

Modes 2 and 3 are **strided urem** and **strided udiv**. Both build an `scf.for` whose body emits one `llvm.urem` (mod-2 striding) or `llvm.udiv` (mod-3 striding) per stage. The loop carries the running mask as its iter-arg. The strided modes kick in when the participant count outgrows the unrolled chain but the access pattern stays regular.

Mode 4 is **cluster**. The mask matrix collapses to a cluster-wide barrier — `sub_611A10` emits a single `nvvm.cluster.arrive`. No 16-element buffer is materialised; Phase C below adapts to the cluster case by substituting the cluster-arrive intrinsic for the per-stage arrives.

### Phase C — Per-Stage Arrive

With the masks built, Phase C iterates `g ∈ [0, P)` and emits four arrives per stage through `sub_15E28B0(builder, %pipe, V_arr, ..., mask, 16, 1)`. This is the 9-argument TMA-aware arrive helper; the ninth argument toggles `expect_tx`, picking between a plain `mbarrier.arrive` and an `mbarrier.arrive.expect_tx` carrying the transaction-bytes hint for TMA-backed producers.

The four arrives per stage cover producer-side and consumer-side masks under both phase parities. Producer-even and consumer-even masks match the current phase; the odd variants are pre-staged for the next phase flip, so a subsequent acquire on the same stage finds its mask already on the barrier.

```c
void phase_c_emit_arrives(Builder *b, Value pipe, MaskMatrix *m, uint32_t P) {
    for (uint32_t g = 0; g < P; ++g) {
        sub_15E28B0(b, pipe, V_arr, /*role*/ PRODUCER, /*parity*/ EVEN, m->prod_even[g], 16, 1);
        sub_15E28B0(b, pipe, V_arr, /*role*/ PRODUCER, /*parity*/ ODD,  m->prod_odd[g],  16, 1);
        sub_15E28B0(b, pipe, V_arr, /*role*/ CONSUMER, /*parity*/ EVEN, m->cons_even[g], 16, 1);
        sub_15E28B0(b, pipe, V_arr, /*role*/ CONSUMER, /*parity*/ ODD,  m->cons_odd[g],  16, 1);
    }
}
```

### Phase D — NamedBarriers Tail

Phase D emits one trailing `arith.addi` per NamedBarrier barrier-id base. The op type is `&unk_5BE5898` and the builder is `sub_42D92B0`. The barrier-id offset comes from `sub_17346A0(op, 3)`, where the literal 3 is the offset constant identifying NamedBarrier slots in the operand bundle. NamedBarriers piggyback on the acquire so warp-specialized named regions stay synchronised with the staged pipeline without a separate lowering pass.

Once all four phases complete, the lowering finalises with `sub_36C67C0(rewriter, op, results, 1u)` — the single-result commit. The `1u` is the result count, not a flag: producer acquire returns the updated pipeline state record and nothing else.

### TypeIDs Used

The emit set spans twelve op types across the four phases. The first nine cover mask-matrix and arrive emission; the last three cover the `scf.for` strided modes and the NamedBarriers tail.

| TypeID | Op |
|---|---|
| `&unk_5BA8EB0` | `llvm.extractvalue` |
| `&unk_5BA8E20` | `llvm.icmp ne` |
| `&unk_5BA8D60` | `llvm.select` |
| `&unk_5BA8DA8` | `llvm.or` |
| `&unk_5BA8F28` | `llvm.and` |
| `&unk_5BA8D18` | `llvm.urem` |
| `&unk_5BA8D28` | `llvm.udiv` |
| `&unk_5BA8F50` | `llvm.add` |
| `&unk_5BA8E00` | `llvm.insertvalue` |
| `&unk_5BE4008` | `scf.for` |
| `&unk_5BE3FC0` | `scf.yield` |
| `&unk_5BE5898` | `arith.addi` |

## `nv_tileas.async.*` Populate Roster

The `nv_tileas.async.*` family is the alias-aware twin of the `cutlass.pipeline` family. It offers similar producer/consumer, wait, and future-wait operations, but typed at the `nv_tileaa` layer where buffer aliasing lives in the type system rather than as a side fact recovered by analysis. `ConvertPipelineToNVVM` rewrites this family through eight populator-registered `OpConversionPattern`s. The pattern class vtables occupy a contiguous range at `off_59D5DD8..off_59D6008`; each pattern is a 120-byte (`0x78`) record allocated through `sub_44A8C20(0x78)` and pushed onto the `RewritePatternSet` via `sub_367D330`. The full populate roster lives in `sub_1189A50` — about 10.5 KB of pattern registration plus the surrounding constructor wiring.

| Pattern | Op | vtable | Lowering |
|---|---|---|---|
| AsyncWaitOpConversionMbarrier | `nv_tileas.async.wait` (mbarrier flavor) | `off_59D5DD8` | `nvvm.mbarrier.try_wait.parity.shared` spin loop |
| AsyncWaitOpConversionTMASTGAndTMAREDG | `nv_tileas.async.wait` (TMA flavor) | `off_59D5E28` | `nvvm.cp.async.bulk.commit.group` + `nvvm.cp.async.bulk.wait_group` |
| AsyncWaitOpConversionGMMA | `nv_tileas.async.wait` (GMMA flavor) | `off_59D5E78` | `nvvm.wgmma.commit.group.sync.aligned` + `nvvm.wgmma.wait.group.sync.aligned` |
| AsyncToAsyncOpConversion | `nv_tileas.async.to_async` | `off_59D5EC8` | `builtin.unrealized_conversion_cast` |
| CreateNoneOpConversion | `nv_tileas.create_none` | `off_59D5F18` | `llvm.mlir.poison` |
| AsyncFutureWaitMbarrier | `nv_tileas.async.future_wait` (mbarrier) | `off_59D5F68` | `nvvm.mbarrier.try_wait.parity.shared` (spin form) |
| AsyncFutureWaitGroup | `nv_tileas.async.future_wait` (group) | `off_59D5FB8` | `nvvm.cp.async.bulk.wait_group` |
| TokenToAsyncOpConversion | `nv_tileas.async.token_to_async` | `off_59D6008` | `builtin.unrealized_conversion_cast` |

The mbarrier, TMA, and GMMA wait patterns share an operation name but discriminate at match time on the source of the token they wait on. Mbarrier waits resolve to a parity spin against a shared-memory barrier; TMA waits commit and drain a bulk async group; GMMA waits commit and drain a WGMMA group. The two `builtin.unrealized_conversion_cast` patterns let `nv_tileaa`-typed values flow through later lowering passes without losing their alias typing — the cast disappears in subsequent type-conversion folds. `CreateNoneOpConversion` lowers `nv_tileas.create_none` to `llvm.mlir.poison` because the only legal use of a none value is to be consumed by an op that will itself be erased once its data dependence is materialised.

## Wait-Group Deduplication Walker

A separate pass-body emitter, `sub_1181940` (about 30 KB), walks each function's regions after the eight patterns have run. The walker dedupes `nv_tileas.async.wait` ops by group-id and emits exactly one wait at each region's tail, separately per flavor. It uses 184-byte per-region records keyed by `Operation *` in open-addressed hash maps with sentinels `-4096` (empty) and `-8192` (tombstone), and a key hash of `(op >> 9) ^ (op >> 4)`. Three identical re-hash-on-grow blocks cover the three co-allocated tables: the region map at offset `+0`, the group-id map at offset `+16`, and the per-flavor cohort map at offset `+32`.

```c
typedef struct GroupWaitState {
    /*+0x00*/ uint32_t  flavor;      // 0 = wgmma, 1 = cp.async.bulk
    /*+0x04*/ uint32_t  base;        // group-id base for this scope
    /*+0x08*/ uint32_t  count;       // current count of outstanding ops
    /*+0x0C*/ uint32_t  cursor;      // next group-id to claim
} GroupWaitState;
```

At each region exit the walker emits one wait per active flavor. For bulk async it builds `nvvm.cp.async.bulk.wait_group` (TypeID `&unk_5B8DAC8`, builder `sub_2E6CFB0`, `flavor == 1`). For WGMMA it builds `nvvm.wgmma.wait.group.sync.aligned` (TypeID `&unk_5B8D610`, builder `sub_2E78330`, `flavor == 0`). The mbarrier handshake never appears in this walker — mbarrier waits come entirely from `AsyncWaitOpConversionMbarrier` in the eight-pattern roster above, because each mbarrier wait ties to its own barrier slot rather than a group-id cohort that needs a region-tail drain.

The walker is what makes the eight wait patterns safe to fire eagerly. A pattern can emit `commit.group` and `wait_group` in isolation, and the walker then collapses redundant waits across a scope without the patterns having to coordinate among themselves.

## Tile Scheduler Kinds

| Scheduler | Work assignment | Runtime state |
|---|---|---|
| Data-parallel | One output tile per CTA or CGA. | Linear tile id and raster unpacking. |
| Static persistent | Resident CTAs walk a closed-form tile iterator. | Current tile id, validity bit, pipe increment bit. |
| StreamK | Split K work across CTAs, then fix up partial accumulators. | K range, split state, workspace pointer, barrier counters. |

Scheduler handles carry the selected kind. Work-tile-info values carry the fields downstream mainloop and epilogue code needs.

## Scheduler Bodies

The runtime work-distribution layer in Tileiras is not one routine. Six cooperating subs decide which CTA handles which tile: four scheduler bodies — one per `cutlass.tile_scheduler.*` op variant, with two specialisations of StreamK for SM100 vs generic — plus two helpers (workspace sizing and a `Params` struct factory). Every kernel using CUTLASS-style work distribution picks one of the four body subs based on the dialect op in its module and the target SM, and links in both helpers unconditionally for setup.

| Sub | Scheduler variant | Workspace | Notes |
|---|---|---|---|
| `sub_R01` | SM100 StreamK | needs `workspace` global | Blackwell-specific StreamK with cluster-level coordination |
| `sub_R02` | StaticPersistent | small workspace | 1-CTA-per-SM persistent kernel; works on all SMs |
| `sub_R03` | StreamK (generic) | needs `workspace` | Hopper-style StreamK; the SM100 variant supersedes when targetSM >= 100 |
| `sub_R04` | DataParallel | no workspace | Pure data-parallel — no work-stealing, simplest case |
| `sub_R05` | (helper) `getWorkspaceSize` | — | Computes the per-scheduler workspace requirement |
| `sub_R06` | (helper) `Params` struct factory | — | Builds the `Params` struct each scheduler reads |

The symbols `sub_R01` .. `sub_R06` are the canonical names used throughout this wiki for the six bodies. Each body exposes the same external entry shape — `(Params *params, int linear_id) -> WorkTileInfo` — so the dialect lowering can fix on one indirect call site and dispatch by kind at op-selection time.

Any scheduler that needs cross-CTA coordination state allocates a global buffer in the kernel's parameter space. `sub_R05` computes the workspace size from `(num_ctas, num_stages, tile_count)` and the result lives in the kernel's `nv_tileas.workspace_global_offset` attribute; DataParallel returns zero and the kernel skips the allocation. StaticPersistent needs only a small counter region for the persistent-advance bookkeeping. Both StreamK variants need partial-accumulator plus barrier regions, whose layout is described under StreamK Workspace below.

The shared `Params` struct, built by `sub_R06` and passed to every body, is a 48-byte record:

```c
typedef struct CutlassTileSchedulerParams {
    /*+0x00*/ uint32_t  num_tiles_m;
    /*+0x04*/ uint32_t  num_tiles_n;
    /*+0x08*/ uint32_t  num_tiles_k;
    /*+0x0C*/ uint32_t  cluster_shape_x;
    /*+0x10*/ uint32_t  cluster_shape_y;
    /*+0x14*/ uint32_t  cluster_shape_z;
    /*+0x18*/ uint32_t  num_ctas_per_cluster;
    /*+0x1C*/ uint32_t  total_ctas;
    /*+0x20*/ uint64_t  workspace_ptr;          // 0 if scheduler is workspace-free
    /*+0x28*/ uint32_t  k_split_count;          // StreamK-only; 0 for others
    /*+0x2C*/ uint32_t  reserved;
} CutlassTileSchedulerParams;
```

The struct is passed as an `nv_tileas.grid_constant` argument, which lets the compiler hoist all field loads into scalar registers at kernel entry. `workspace_ptr` is the only 64-bit field — it carries a global address; everything else is a count or shape index and fits in 32 bits. `k_split_count` is zero for DataParallel and StaticPersistent; both StreamK bodies are the only consumers, reading it to decide how many K partials to expect at fixup time. The trailing `reserved` word keeps the struct 8-byte aligned so `workspace_ptr` lands on its natural alignment without a hidden pad.

Scheduler op selection in the `cutlass` dialect happens at lowering time. Each `cutlass.tile_scheduler.*` op declares which scheduler variant it backs. `cutlass.tile_scheduler.streamk` resolves to `sub_R01` on SM100 and `sub_R03` otherwise; `cutlass.tile_scheduler.static_persistent` always resolves to `sub_R02`; `cutlass.tile_scheduler.data_parallel` always resolves to `sub_R04`. The dialect verifier enforces the inverse direction too: SM100 streamk is illegal on `sm_90` (R01 uses Blackwell cluster barriers that do not exist on Hopper), and the generic streamk op is illegal on `sm_100` because R01 supersedes it and a kernel must not link both.

The SM100 streamk body (`sub_R01`) is the only one using cluster-level coordination. It emits `cute_nvgpu.arch.cluster.barrier` ops — the Blackwell 2-CTA/4-CTA cooperative MMA protocol described under `topics/blackwell-2cta-and-4cta-mma.md` — so each cluster can claim a contiguous range of `(M, N, K)` tiles, distribute them across the cluster's CTAs, and coordinate K-split partial-reductions through an inter-CTA barrier. The generic streamk body (`sub_R03`) reaches the same logical result with per-CTA atomics on the barrier workspace. The SM100 variant exists because the cluster-barrier path is far cheaper on Blackwell — at high cluster counts the atomic path's runtime cost would dominate.

## Data-Parallel Scheduler

Data-parallel assignment is the simplest mapping: linear CTA id maps to an
output tile by raster order and swizzle policy.

```c
WorkTileInfo data_parallel_tile(SchedulerParams p, int linear_id) {
    int total = p.tiles_m * p.tiles_n * p.tiles_l;
    require(0 <= linear_id && linear_id < total);

    RasterCoord r = raster_unpack(linear_id,
                                  p.tiles_m,
                                  p.tiles_n,
                                  p.tiles_l,
                                  p.raster_order,
                                  p.swizzle_size);

    WorkTileInfo info;
    info.m = r.m * p.cga_shape_m;
    info.n = r.n * p.cga_shape_n;
    info.l = r.l;
    info.linearized_id = linear_id;
    return info;
}
```

Data-parallel schedulers need a WorkID response pointer when running the runtime pull model.

## Static Persistent Scheduler

Static persistent scheduling keeps CTAs resident. Each CTA advances by the grid width every iteration until no work remains.

```c
AdvanceResult persistent_advance(StaticPersistentParams p, WorkTileInfo t) {
    WorkTileInfo next = t;
    next.linearized_id += p.grid_width;

    bool valid = next.linearized_id < p.total_cga_tiles;
    bool increment_pipe = (t.tile_idx + 1) == p.tiles_per_pipeline_round;

    next.tile_idx = increment_pipe ? 0 : t.tile_idx + 1;
    return (AdvanceResult){
        .tile = next,
        .is_valid_tile = valid,
        .increment_pipe = increment_pipe,
    };
}
```

`advance_to_next_work` belongs inside an async execution region because it advances the scheduler and may clock the enclosing pipeline.

## StreamK Scheduler

StreamK splits work along the K dimension. Some CTAs compute full output tiles; others compute partial K ranges and stash partial accumulators in a workspace. A reducer CTA then waits for all partials, accumulates them, and runs the final epilogue.

```c
StreamKParams compute_streamk_params(Problem problem, TileShape tile, int target_units) {
    int cga_tiles_m = ceil_div(problem.m, tile.m);
    int cga_tiles_n = ceil_div(problem.n, tile.n);
    int output_tiles = cga_tiles_m * cga_tiles_n * problem.l;
    int total_k_work = output_tiles * problem.k_tiles_per_output;

    int units = choose_streamk_units(target_units, output_tiles, total_k_work);
    int dp_tiles = choose_data_parallel_tail(output_tiles, units);
    int sk_tiles = output_tiles - dp_tiles;
    int sk_units = units - dp_tiles;

    int total_sk_k = sk_tiles * problem.k_tiles_per_output;
    int small = total_sk_k / sk_units;
    int big = total_sk_k % sk_units;

    return (StreamKParams){
        .sk_tiles = sk_tiles,
        .sk_units = sk_units,
        .k_tiles_per_small_unit = small,
        .big_units = big,
    };
}
```

Per-CTA dispatch splits a linear worker id into either a StreamK slice or a data-parallel tail tile:

```c
WorkTileInfo streamk_tile(StreamKParams p, int worker_id) {
    if (worker_id >= p.sk_units) {
        int tail = worker_id - p.sk_units;
        return data_parallel_tail_tile(p, p.sk_tiles + tail);
    }

    int k_start = streamk_flat_k_start(p, worker_id);
    int k_count = streamk_k_count(p, worker_id);

    WorkTileInfo info;
    info.tile_idx = k_start / p.k_tiles_per_output;
    info.k_tile_start = k_start % p.k_tiles_per_output;
    info.k_tile_count = k_count;
    info.is_separate_reduction = k_count != p.k_tiles_per_output;
    return info;
}
```

## StreamK Workspace

StreamK uses two workspace regions:

- reduction workspace: partial accumulator tiles;
- barrier workspace: per-output-tile counters or synchronization words.

```c
WorkspaceSizes streamk_workspace_sizes(StreamKParams p, int accumulator_tile_bytes) {
    int splits = ceil_div(p.k_tiles_per_output, p.k_tiles_per_small_unit);
    int reduction = p.sk_tiles * (splits - 1) * accumulator_tile_bytes;
    int barriers = p.sk_tiles * sizeof(uint32_t);
    int total = align_to(reduction + barriers, 128);

    return (WorkspaceSizes){
        .total = total,
        .reduction = reduction,
        .barriers = barriers,
    };
}
```

The workspace pointer must be aligned for vectorised global-memory access. Data-parallel and static-persistent schedulers return zero workspace sizes.

## Fixup Protocol

```c
void streamk_fixup(StreamKWorkspace ws, WorkTileInfo info, Accumulator partial) {
    if (!info.is_final_split) {
        store_partial(ws.reduction, info.tile_idx, info.split_idx, partial);
        atomic_add_release(ws.barrier, info.tile_idx, 1);
        return;
    }

    wait_until(ws.barrier[info.tile_idx] == info.expected_splits - 1);

    Accumulator total = partial;
    for (int split = 0; split < info.expected_splits - 1; ++split) {
        total += load_partial(ws.reduction, info.tile_idx, split);
    }

    run_epilogue(total);
    reset_barrier(ws.barrier, info.tile_idx);
}
```

Fixup ops are epilogue ops. They do not need to live inside the pipeline async region, but they must preserve memory ordering on the workspace.

## Invariants

- Pipeline stage count, producer count, consumer count, and participant masks
  are mutually consistent.
- Pipeline state increment flips phase on index wraparound.
- Executor masks are contiguous and cover the enclosing async execution mask.
- Data-parallel schedulers have a WorkID response source when the runtime pull
  model is selected.
- Static-persistent and StreamK schedulers do not use the data-parallel WorkID
  pointer path.
- `advance_to_next_work` appears inside async execution.
- StreamK workspace sizes and scheduler kind agree.
- StreamK fixup uses release/acquire-style ordering around partials.

