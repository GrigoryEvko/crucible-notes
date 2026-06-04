# SC ↔ MXU Handshake

> *Every op name, symbol, operand count, assertion string, and literal on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`, LLVM SHA `8918319853fbdf9e6f6cb69e96848f913a22bc31`). Other versions differ.*

## Abstract

This page is the **cross-engine boundary** between the SparseCore (SC) co-processor and the TensorCore (TC) systolic MXU — the handshake that lets a recommender / DLRM model feed gathered embedding rows out of SC and into a dense matmul. SC is good at exactly what the MXU cannot do (index-driven gather of single HBM rows, atomic FP-add scatter); the MXU is good at exactly what SC cannot do (128×128 weight-stationary dense matmul). A `XlaSparseDenseMatmul` is the fusion of those two: SC gathers the per-sample embedding rows, the MXU multiplies them against the dense MLP weights. The two engines run *asynchronously*, so the entire boundary reduces to three concerns: **how the data crosses** (SC writes a tile into TC's VMEM operand pool), **how the engines synchronize** (the SFLAG sync-flag protocol with TC-side `tpu.sem_wait`), and **how the MXU binds the result** (the contracting-depth contract — the SC tile lands as the MXU *moving* operand, reduced over the dense `MxuContractingSize`, not over any sparse contracting dimension).

This page documents only the *boundary*. The intra-SC gather datapath (the Stream slot, the `IndirectStream` form, the per-element address formula) is owned by [Stream Gather/Scatter](stream-gather-scatter.md); the TC MXU op family (`vlatch` / `vmatprep` / `vmatmul` / `vmatres`) is owned by [MXU Slot](../isa/slot-mxu.md); the SC on-chip geometry and the bump allocator are owned by [SparseCore Architecture](architecture.md). Here we connect them. The three handoff *channels* (HBM bulk, VMEM-targeted DMA, SFLAG control plane) and the lowering symbols that emit each are the recoverable surface; the byte-layout of the routing key (`DeviceAndCoreIds`) and the per-gen SFLAG-address bit-encoding are the open items.

For reimplementation, the contract is:

- **There is exactly ONE direct SC→VMEM data route and ONE direct VMEM→SC route.** Of the 40 `mlir::sparse_core::tpu_dma_<src>_to_<dst>_sc_<form>` ops, only `tpu_dma_hbm_to_vmem_sc_general` (the SC→MXU feed) and `tpu_dma_vmem_to_hbm_sc_general` (the MXU result writeback) cross the SC↔TC VMEM boundary, plus `tpu_dma_vmem_to_vmem_sc_general`. All three are **`general`-form only** (16 operands, full descriptor). Every other SC↔TC transfer transits HBM. A reimplementer must route the embedding-feed tile through this one op.
- **The sync is a writer-count SFLAG, waited on from the TC side.** SC raises a sync flag on tile completion; TC's `tpu.sem_wait` (`SemaphoreWaitOp`, 2 operands) advances when the flag's writer count reaches the threshold. The flag lives in the global `sflag` subspace for cross-CORE coordination; the cross-sub-engine flags (`sflag_scs`, `sflag_tile`) are SC-internal and never cross to TC directly.
- **The MXU consumes the SC tile as the dense *moving* operand.** The matmul reduces over `Target::MxuContractingSize` (128 base, 256 on the Ghostlite class). `MxuSparseContractingSize` is **0 on every generation** — the sparse-MXU contracting path is unused in this build, so the embedding-feed matmul runs the ordinary dense contracting datapath. The SC-vs-MXU split is *engine*-level, not a sparse contracting mode.
- **The pipeline is double-buffered.** `OffloadFactory::AllocateSflag(builder, /*set*/false, /*count*/2)` allocates one flag per buffer: SC fills buffer A while the MXU drains buffer B, then they flip. This is what hides the SC gather latency behind MXU compute.

| | |
|---|---|
| **HLO boundary marker** | `XlaSparseDenseMatmulOp` (+ Grad / CSR / optimizer-fused / minibatch / megachip variants) |
| **SC→MXU data route** | `mlir::sparse_core::tpu_dma_hbm_to_vmem_sc_general` (16 operands; `general` form only) |
| **MXU→SC writeback** | `mlir::sparse_core::tpu_dma_vmem_to_hbm_sc_general` (16 operands) |
| **DMA lowering** | `LowerMemrefToMlo::lowerEnqueueDma` (`0x135105a0`) → `sc_tpu.dma_general_start` + `sc_tpu.dma_wait` |
| **kStream vs kDma** | `xla::tpu::sparse_core::GetTransferKind` (`0x1351b140`; `(Target&, src, dst, 4×bool)`) |
| **TC-side sync ops** | `tpu.sem_wait` / `tpu.sem_signal` / `tpu.sem_read` / `tpu.fetch_and_add_sync` |
| **SFLAG subspaces** | `sflag` (cross-CORE) · `sflag_scs` (SCS scope) · `sflag_tile` (TEC/tile scope) |
| **Cross-sub-engine sync** | `OffloadFactory::SyncScsWithTec` (`0x133e9260`) / `SyncTecWithScs` (`0x133e8fe0`) |
| **MXU contracting depth** | `Target::MxuContractingSize` = 128 base / **256** Ghostlite class; `MxuSparseContractingSize` = 0 (unused) |
| **Double-buffer** | `OffloadFactory::AllocateSflag(b, false, 2)` — one flag per buffer |

---

## The Three Handoff Channels

### Purpose

SC and TC share three physical media: HBM (the global table store), TC's VMEM (the MXU operand feed), and the SFLAG register file (the control plane). The compiler picks a channel per transfer; the choice is what determines whether the handshake is a slow global round-trip or a fast direct operand feed.

### The Channels

```text
Channel 1 — HBM bulk (slow, global):
  SC stream engine  --STREAM_OPCODE_GATHER / _SCATTER_FLOAT_ADD-->  HBM
  TC                --tpu.enqueue_dma (DMA_MEMORY_ID_VMEM)------->  reads HBM into VMEM
  sync: SC stream op carries a completion sflag; TC tpu.sem_wait on the same flag.

Channel 2 — VMEM-targeted DMA (fast, programmed):       <-- THE SC->MXU FEED
  SC  --tpu_dma_hbm_to_vmem_sc_general (16 operands)----------->  TC VMEM
  the destination sflag (TC side) AND the source DMA-done sflag (SC side)
  are both carried in the operand vector.

Channel 3 — SFLAG-only control plane (no data):
  SCS  --SetSyncFlag / AddSyncFlag / AtomicRemoteWriteSetDone-->  shared SFLAG
  TC   --tpu.sem_wait / tpu.fetch_and_add_sync---------------->  keyed on same SFLAG addr
  remote address encoded per-gen by EncodeRemoteSyncFlagAddress{JfDf,Pufferfish,Viperfish}
  and xla::ghostlite::dma_utils::EncodeRemoteSyncFlagAddress.
```

### The Canonical Embedding-Feed Fastpath

There is exactly **one** direct SC→VMEM route and **one** TC→SC-via-VMEM route — verified by enumerating all 40 `sparse_core::tpu_dma_*` ops in the decompile, of which only three name `vmem`:

```text
tpu_dma_hbm_to_vmem_sc_general    16 operands   ← SC produces, MXU consumes
tpu_dma_vmem_to_hbm_sc_general    16 operands   ← MXU result writeback
tpu_dma_vmem_to_vmem_sc_general   16 operands   ← VMEM→VMEM intra-TC
```

So the canonical SC→MXU path is:

```text
SC TEC  ->  TILE_SPMEM  ->  SPMEM  ->  HBM  ->  VMEM  ->  MXU       (full aggregation)
SC TEC  ->  TILE_SPMEM  ->  HBM    ->  VMEM  ->  MXU                (direct, skips SPMEM)
```

The short form skips the SPMEM aggregation stage when a per-tile result is destined straight for MXU consumption; there is no `spmem_to_vmem` route, so SPMEM-resident tiles always transit HBM before reaching VMEM.

> **NOTE —** the absence of `simple` and `single_strided` forms for the VMEM routes is structural, not an omission: `*_to_vmem_sc_general` and `vmem_to_*_sc_general` exist **only** in the `general` (16-operand) form. SC↔TC VMEM transfers always carry a full multi-dim DMA descriptor. A reimplementer cannot emit a "simple" 8-operand DMA across the SC↔TC boundary — the geometry of an embedding tile (rows × embedding_dim, possibly strided per logical replica) always needs the full descriptor.

### Function Map

| Function | Address | Role |
|---|---|---|
| `LowerMemrefToMlo::lowerEnqueueDma` | `0x135105a0` | `tpu.enqueue_dma` → `sc_tpu.dma_{simple,general,single_strided}_start` + `sc_tpu.dma_wait` |
| `LowerMemrefToMlo::lowerEnqueueIndirectDma` | `0x13511da0` | `tpu.enqueue_indirect_dma` → indirect-stream ops |
| `LowerMemrefToMlo::lowerWaitDma` | `0x135135e0` | `tpu.wait_dma2` → `sc_tpu.dma_wait` |
| `LowerMemrefToMlo::getVerifiedDmaShapes` | `0x13509dc0` | extract + verify the DMA shapes |
| `LowerMemrefToMlo::checkShapeTileAlignment` | `0x135053a0` | verify shape-tile alignment for cross-engine DMA |
| `LowerMemrefToMlo::convertDeviceIdFromSubsliceToFullSlice` | `0x13505a40` | logical → physical core id for the routing key |

---

## kStream vs kDma — Choosing the Channel

### Purpose

Before lowering a transfer, the compiler classifies it as a *stream* (SC stream engine, high random-access bandwidth, the gather/scatter datapath) or a *DMA* (the regular DMA engine, high contiguous bandwidth, the cross-engine VMEM handoff). The SC→MXU feed is a **kDma**: it is a contiguous tile crossing the engine boundary, not an indexed gather.

### The Classifier

```cpp
// xla::tpu::sparse_core::GetTransferKind  @ 0x1351b140
//   demangled tail "...MemorySpaceES8_bbbb" == (Target&, MemorySpace, MemorySpace, b, b, b, b)
TransferKind GetTransferKind(
    const xla::jellyfish::Target& target,
    mlir::sparse_core::MemorySpace src_mem,
    mlir::sparse_core::MemorySpace dst_mem,
    bool is_indirect,
    bool is_atomic,
    bool is_remote,
    bool is_tile_scope);   //  -> TransferKind::kStream | TransferKind::kDma
```

Decision rule (from the assertion-string anchors `transfer_kind == TransferKind::kStream` and `== TransferKind::kDma`, both present in the lowering decompile):

| Transfer | Kind | Why |
|---|---|---|
| Indirect (gather / scatter), id-list driven | `kStream` | stream engine has higher random-access bandwidth |
| Atomic (scatter-add into HBM) | `kStream` | the in-HBM FP-add the MXU cannot do |
| Embedding-style HBM traffic | `kStream` | single-row random access |
| Contiguous bulk transfer | `kDma` | sequential, the regular DMA engine |
| **Cross-engine SC↔TC via VMEM** | **`kDma`** | the operand-feed tile is contiguous |
| Host (IOVA) transfer | `kDma` | host staging |

> **GOTCHA —** the `getTransferKind<EnqueueDMAOp>` (`0x135114a0`) and `getTransferKind<WaitDMA2Op>` (`0x135145e0`) member-templates are *per-op-kind* classifiers that delegate to the free `GetTransferKind`; the lowering then **CHECK**s that the chosen kind matches the producing source path (the two `transfer_kind == TransferKind::k*` asserts). A reimplementer who emits a VMEM-targeted transfer as a `kStream` will trip that CHECK — the SC↔TC VMEM feed must be classified `kDma`. The two channels also use distinct HBM controller policies (random-access prefetch for SC stream, sequential-stream prefetch for TC DMA), so the classification is not cosmetic.

---

## The SFLAG Sync Handshake

### Purpose

The data channel moves bytes; the SFLAG channel moves *permission*. Because SC and TC run asynchronously, the MXU must not latch a VMEM tile until SC has finished writing it, and SC must not overwrite a buffer until the MXU has finished reading it. Both directions are sync flags — a small counter the producer increments and the consumer waits on.

### The SFLAG Memory Model

`SFLAG` is not one register file; it is segmented at the sub-engine level into three subspaces, confirmed by the verifier assertion that a flag's memory space is one of exactly these three:

```text
sflag_memory_space == mlir::sparse_core::MemorySpace::sflag        // global, cross-CORE (SC<->TC)
                   || mlir::sparse_core::MemorySpace::sflag_tile    // per-tile, TEC scope
                   || mlir::sparse_core::MemorySpace::sflag_scs     // per-SCS scope
```

| MemorySpace | Scope | Crosses to TC? |
|---|---|---|
| `MemorySpace::sflag` | global SFLAG pool (cross-engine, cross-core) | **yes** — this is the SC↔TC handshake flag |
| `MemorySpace::sflag_tile` | per-tile (TEC sub-engine scope) | no — SC-internal |
| `MemorySpace::sflag_scs` | per-SCS (SCS sub-engine scope) | no — SC-internal |

The region scoping is verifier-enforced, byte-confirmed by two assertion strings present in `mlir::sparse_core::MloModuleVerifier::Verify(mlir::memref::StoreOp)` (`0x146a9da0`):

```text
"on smem_tile/sflag_tile is only allowed inside a tile_task or an execute sequencer function"
"on smem_scs/sflag_scs is not allowed inside a tile_task or an execute sequencer function"
```

So a flag that crosses to the TC *must* live in the global `sflag` subspace. The tile-scope and SCS-scope flags are confined to their sub-engine and need an explicit address-space cast (`tpu_addrspacecast_sflag_tile_sflag_scs`, etc.) even to be shared between SC sub-engines, let alone to reach TC.

### TC-Side Ops That Target SC Flags

The TC sequencer (`TpuSequencer`) issues four `mlir::tpu` ops that participate in the handshake. All four symbols (`SemaphoreWaitOp`, `SemaphoreSignalOp`, `SemaphoreReadOp`, `FetchAndAddSyncOp`) are present in the decompile:

| TC op | MLIR op | Operands | Role |
|---|---|---|---|
| `tpu.sem_wait` | `SemaphoreWaitOp` | 2 | wait on `(sflag_ptr, threshold)` — the MXU-feed gate |
| `tpu.sem_signal` | `SemaphoreSignalOp` | ≥2 (variadic, `AttrSizedOperandSegments`) | increment a flag, optionally targeting a remote SC partition |
| `tpu.sem_read` | `SemaphoreReadOp` | 1 → 1 result | read a flag value without blocking |
| `tpu.fetch_and_add_sync` | `FetchAndAddSyncOp` | 3 (`NOperands<3>`) → 1 result | atomic add, returns old value (tile counters) |

Both `tpu.enqueue_dma` (`EnqueueDMAOp`) and `tpu.sem_signal` (`SemaphoreSignalOp`) carry a `getRemoteDeviceAndSparseCoreIds<T>()` specialization — confirming that *both DMA and semaphore can address a remote SparseCore by partition id* via the `DeviceAndCoreIds` routing key.

The `expandTPUFetchAndAddSync` helper (`0x134e60c0`, in `ExpandTiledMemRefsPass`) lowers a TC-side `tpu.fetch_and_add_sync` into an `sc_tpu.fetch_and_add` when its destination flag is in SC memory — i.e. the SC side is a passive recipient of TC's atomic-add primitive.

### The Handshake Sequence

```text
forward (SC produces, MXU consumes):
  1. SC TEC vector-store completes the tile in VMEM (or HBM->VMEM DMA done).
  2. SC sync_set / set_done_bit raises the completion flag in the global `sflag` subspace.
  3. TC tpu.sem_wait(flag, threshold) advances; the MXU latches the tile (matprep.subr).
  4. MXU runs; on result writeback, TC sem_signal the read-done flag.
  5. SC sem_read / sync_wait sees the read-done flag and is free to reuse the buffer.
```

When the pipeline is balanced, the wait is non-blocking: SC tile N's `sync_set` completes at TC cycle T+1, TC's `sem_wait` at T+1 unblocks immediately, the MXU latch for tile N runs at T+2, and SC tile N+1's gather has already started at T+1 — so tile N+1 is resident by the time the MXU asks for it.

### Cross-Sub-Engine Sync (SC-Internal, Feeding the Boundary)

Before SC can raise the cross-CORE flag, its own sub-engines must agree the tile is done. The collective-offload emitter does this through `OffloadFactory`:

| Helper | Address | Role |
|---|---|---|
| `OffloadFactory::SyncScsWithTec(builder, value, CoreKind)` | `0x133e9260` | SCS waits on a flag set by TEC (body issues a `SyncWait`) |
| `OffloadFactory::SyncTecWithScs(builder, v1, v2, CoreKind)` | `0x133e8fe0` | TEC waits on a flag set by SCS |
| `OffloadFactory::AllocateSflag(builder, bool set, long count)` | `0x133e8320` | allocate an SFLAG region (count + initial state) |
| `OffloadFactory::StartLocalDma(...)` | `0x133eb3a0` | same-chip DMA with src/completion sflags |
| `OffloadFactory::DmaWait(builder, sflag_idx, wait_size, ...)` | `0x133e8180` | wait for a previously issued DMA |

`CoreKind` parametrizes which sub-engine the helper operates on:

| `CoreKind` | Meaning |
|---|---|
| `CoreKind::kScs` | SparseCore Scalar sub-engine |
| `CoreKind::kTec` | Tile-Execute sub-engine |
| `CoreKind::kTac` | Tile-Access sub-engine (Viperfish / Ghostlite only) |

> **NOTE —** on TPU7x (6acc60406, the gfc namespace) there is no TAC, so the SCS↔TEC sync collapses to a single primitive: `sc_tpu.tile_wait_scs_smem` (`TileWaitScsSmemOp`, 3 operands `= {sflag_ptr, expected_value, smem_buf}` — `NOperands<3u>` confirmed). Viperfish and Ghostlite both still carry the full TAC ISA. The TEC waits until SCS has written a designated value to SMEM, then reads SMEM and continues — SCS does the address computation that TAC used to do. This is the TAC-replacement primitive; it does not change the *cross-CORE* (SC↔TC) handshake, only the SC-internal access→execute synchronization. See [TAC Engine](tac-engine.md) and the per-gen section below.

---

## The Contracting-Depth Binding

### Purpose

Once the SC tile is resident in VMEM and the sync flag has cleared, the MXU consumes it. The binding question is: *what dimension does the matmul reduce over, and is the SC's sparsity visible to the MXU at all?* The answer is the cleanest part of the boundary: the SC tile enters the MXU as the ordinary dense **moving operand**, reduced over the dense `MxuContractingSize`. The MXU never sees "sparse" — the sparsity was fully resolved by the SC gather upstream.

### What the MXU Latches

The MXU side runs the standard op family ([MXU Slot](../isa/slot-mxu.md)): a `vlatch` loads the stationary weight tile, a `vmatprep.subr` / `.mubr` stages the SC-produced VMEM tile as the moving operand into `MATPUSH_TARGET_MSRA` / `MSRB`, `vmatmul` clocks the systolic array, and `vmatres` drains the accumulator. The activation tile is read from VMEM by the TC's `LoadActivationsChunk` path; the embedded-lookup result *is* that activation tile.

```text
SC-produced VMEM tile  --(matprep.subr/.mubr)-->  MSRA/MSRB  --(vmatmul, K steps)-->  accumulator
                                                                    ^
                                                            dense MLP weights (vlatch, stationary)
```

### The Contracting Constants

The contracting depth is a per-codename C++ literal in the `Target` subclass — not a `chip_parts` field. The byte-exact values (owned by [SparseCoreTarget (`Target+0x948`)](../targets/sparsecore-target-descriptor.md), reproduced here as the *consumer*):

| MXU constant | v3 Jelly | v4 Puffer | v5p Viperfish | v6e Ghostlite | TPU7x (6acc60406) | Source |
|---|:--:|:--:|:--:|:--:|:--:|---|
| `MxuContractingSize` | 128 | 128 | 128 | **256** | **256** | base `0x1D490060`; Ghostlite `0x1D497840` |
| `MxuNoncontractingSize` | 128 | 128 | 128 | **256** | **256** | base `0x1D490080`; Ghostlite `0x1D497860` |
| `MxuSparseContractingSize` | 0 | 0 | 0 | 0 | 0 | base `0x1D4900C0`; no override |
| `MxuContractingSizeIsDoubled(mode)` | false | false | predicate | predicate | predicate | base `0x1D4900A0`; VF `0x1D49AA60`; GL `0x1D497880` |

Three facts a reimplementer must encode:

1. **The embedding-feed matmul reduces over the dense contracting dimension** (`MxuContractingSize` — 128 on Jellyfish through Viperfish, 256 on the Ghostlite class, which TPU7x/6acc60406 reuses — there is no separate `Tpu7xTarget`; only `GhostliteTarget` overrides `MxuContractingSize` to 256). The reduction depth is the embedding-dim (or a tile thereof), packed exactly like any other dense activation.
2. **`MxuSparseContractingSize` is 0 on every generation** — no `Target` subclass overrides it. The sparse-MXU contracting path is *unused* in this build. The SC/MXU division of labor is at the *engine* level (SC gathers, MXU does dense matmul), not at the level of a sparse contracting mode inside the MXU. A reimplementer should not look for a sparse-MXU latch on the embedding feed.
3. **The doubling predicate is orthogonal to sparsity.** Both `ViperfishTarget` and `GhostliteTarget` override `MxuContractingSizeIsDoubled` with the identical predicate `(mode - 22) < 4u` — true for raw `GainLatchMode ∈ {22,23,24,25}`, the 4-bit packed-nibble (S4/U4) matmul modes that pack two nibbles into one physical systolic row (doubling effective contracting depth). This is a dtype-packing feature of the dense MXU and applies to embedding-feed matmuls only insofar as the activation dtype is 4-bit — it is not an SC-specific path.

> **GOTCHA —** the SC `SparseCoreLaneCount` (`Target::SparseCoreLaneCount`, `0x0F7906E0` reading the per-gen target descriptor; the runtime `tensorflow::GetSparseCoreLaneCount` is flag-overridable, so the exact per-gen integer is not a fixed literal) is the vector width SC reduces over, and is **not** the MXU contracting depth. They are independent: SC's lane count is the TEC vector engine's SIMD width for the *gather/reduce* upstream; the MXU contracting size (128 / 256) is the systolic *row depth* the dense matmul reduces over downstream. The handoff is a reshape boundary — SC produces a `[rows × embedding_dim]` tile in its lane geometry; the MXU re-tiles it into `[contracting × noncontracting]` for the systolic array. Conflating the two will mis-size the VMEM staging buffer. See [SparseCore Architecture](architecture.md) for the SC lane geometry and [MXU Slot](../isa/slot-mxu.md) for the systolic tiling.

---

## HLO-Level Boundary Markers

### Purpose

The handshake is created at the HLO level: an `XlaSparseDenseMatmul` custom-call is the single op that, after decomposition, becomes an SC-side gather computation plus a TC-side dense matmul plus the SFLAG sync between them. These are the op names a reimplementer's front-end must emit to trigger the boundary.

### The Custom Calls

| HLO op | What it means |
|---|---|
| `XlaSparseDenseMatmulOp` (TF op-kernel) / `tf.XlaSparseDenseMatmulWithCsrInput` | single-step embedding lookup + dense matmul (the base boundary marker) |
| `XlaSparseDenseMatmulGradOp` (TF op-kernel) | backward: TC computes the gradient, SC scatter-adds into the table |
| `tf.XlaSparseDenseMatmulWithCsrInput` / `…WithStaticBufferSize` | base, with CSR (or static-buffer-size) sparse-input format |
| `tf.XlaSparseDenseMatmulGradWith{Sgd,Adam,Ftrl,Adagrad,AdagradMomentum}AndCsrInput[Op]` | backward + optimizer fused |
| `SparseDenseMatmulWithMinibatchingOp` (internal HLO) | mini-batch variant (subdivides a batch into TILE_SPMEM-fitting windows); decomposed by `sparse-dense-matmul-with-minibatching-op-decomposer` |
| `tf.XlaSparseDenseMatmulCustomCombinerOnTc[Grad]With…CsrInput` / `SparseDenseMatmulCustomCombinerTcCombiner{,Megachip}Op` | TC-side custom combiner (and mega-chip cross-chip form) |
| `GatherMulScatterSparseDenseMatmulOp` (internal HLO) | gather-mul-scatter fused form |
| `XlaLocalSparseDenseMatmulOp` (TF op-kernel) / `tf.XlaLocalSparseDenseMatmul` | single-device variant (no SPMD partitioning) |

Each is rewritten by its decomposer (the `sparse_dense_matmul_*` family) into a tuple of `{SC-side gather computation, TC-side dense matmul, cross-engine SFLAG sync}`. The `EmbeddingsPass` / `EmbeddingBackwardPass` recognize the embedding-lookup HLO pattern and produce this custom-call; the `EmbeddingDataFormattingDecomposer` sets `sc.core_type = "sparse"` on the SC computation. After decomposition the SC half is partitioned by `SparseCoreHierarchicalSpmdPartitioner` (with `PadSparseCoreProgramInputs` / `UnPadSparseCoreProgramOutputs` aligning the shard boundary) and handed to the SC-MLO pipeline. See [SC Backend Pipeline](sc-backend-pipeline.md) for the pass sequence and [SparseCore vs Neuron MatmultSparse](sparsecore-vs-neuron-matmultsparse.md) for the cross-vendor comparison.

> **NOTE —** the SC↔TC boundary marker is the HLO custom-call, not an MLIR op. By the time the program reaches `sc_tpu.*` (the SC mid-level IR) the two halves are already separate computations; the only thing tying them together is the SFLAG-keyed sync inserted at the boundaries. A reimplementer working at the MLIR level sees two independent programs — the *coupling* is the sync-flag use-def chain, which `SyncFlagVerifierPass` validates.

---

## Partition Assignment — Which SC Feeds Which MXU

### Purpose

A chip has several SCs and fewer TCs; the routing key on each TC-side DMA / semaphore selects which SC partition it addresses. The ratio is fixed per generation.

### The Ratio

The central constant is `xla::jellyfish::lowering_util::SparseCoreCountPerTensorCore(tpu::TpuTopology const*)` (`0x1C6CB760`) = `sparse_core_count_per_chip / tensor_core_count_per_chip`, guarded by two `CHECK`s — `"sparse_core_count_per_chip >= tensor_core_count_per_chip"` and `"sparse_core_count_per_chip % tensor_core_count_per_chip == 0"` (the integer 4:1 ratio):

| Gen | TCs/chip | SCs/chip | SCs per TC |
|---|:--:|:--:|:--:|
| Viperfish | 2 | 8 | 4 |
| Ghostlite | 2 | 8 | 4 |
| TPU7x (6acc60406) | 1 | 4 | 4 |

The 4-SCs-per-TC ratio is preserved across all SC-bearing gens. The four SCs jointly produce the embedding-tile rows consumed by one MXU sequence; each SC's `read_register_sparse_core_id` (SCS scalar opcode) returns its per-chip id, and `IfLocalSparseCore` (`0x1C6CD000`) emits a conditional that runs only when the current SC id matches the designated target (the per-partition gate in SPMD-replicated SC kernels).

The TC-side `DeviceAndCoreIds` struct (the `(logical_device_id, logical_core_id)` pair) on each `tpu.enqueue_dma` / `tpu.sem_signal` selects the SC; `convertDeviceIdFromSubsliceToFullSlice` (`0x13505a40`) maps the logical id to the physical id, and the dialect-level `LogicalToPhysicalDeviceIdPass` performs the conversion before lowering to Mlo.

> **GOTCHA —** the canonical assignment (SC 0→MXU lane 0, SC 1→lane 1, …) is a *convention*, not a hard wiring — it is the `DeviceAndCoreIds` value the compiler fills in. A reimplementer must populate the routing key explicitly; there is no implicit SC↔MXU affinity in the hardware. The Jellyfish / Dragonfish / Pufferfish generations have **no** SparseCore (the presence gate `Target::SupportsSparseCore` returns false), so this entire boundary is absent on them.

---

## Per-Generation Variations

| Aspect | Viperfish (vfc) | Ghostlite (glc) | TPU7x / 6acc60406 (gfc) |
|---|---|---|---|
| TAC sub-engine | yes | yes | **no** |
| SC-internal access→execute sync | SCS→TAC→TEC three-way | SCS→TAC→TEC three-way | SCS→TEC two-way via `tile_wait_scs_smem` |
| SCs per chip / per TC | 8 / 4 | 8 / 4 | 4 / 4 |
| DMA channels | 8 SC + 12 TC | 8 SC + 12 TC | 4 SC + 12 TC |
| `MxuContractingSize` | 128 | **256** | **256** (reuses `GhostliteTarget`) |
| Remote-sflag encoder | `EncodeRemoteSyncFlagAddressViperfish` | `xla::ghostlite::dma_utils::EncodeRemoteSyncFlagAddress` | same Ghostlite helper |
| Dual-channel sync (`AddBothSyncFlag` …) | **yes** | **yes** | **no** |
| Yieldable sync family | **12 ops** | **12 ops** | **no** |

The cross-CORE handshake *protocol* is identical across the three SC gens — global `sflag`, `tpu.sem_wait`, double-buffer. The deltas are: (1) the gfc generation (TPU7x / 6acc60406) drops TAC and folds its address/DMA-issue role into TEC, replacing the three-way SC-internal sync with `tile_wait_scs_smem`; (2) both Viperfish and Ghostlite carry the dual-channel sync family (`Add/SetBothSyncFlag`, `Add/SetOtherSyncFlag`) and the 12 yieldable-sync ops (`SparseCoreScalarMisc_YieldableSync{Done,NotDone,Equal,NotEqual,Greater,Less,…}`), both of which gfc drops; (3) the remote-SFLAG-address bit-encoding is per-gen, dispatched through a `RemoteSyncFlagEncoderRegistry` keyed on `TpuVersion`.

> **NOTE —** `CoreKind::kTac` is still a registered enum value on TPU7x for binary compatibility, but the gfc emitter never produces it — confirmed by zero `gfc::isa::SparseCoreTac*` symbols (against the full TAC ISA surface in both `vfc::isa::SparseCoreTac*` and `glc::isa::SparseCoreTac*`). A reimplementer targeting TPU7x must route the gather-issue and access→execute sync through TEC + `tile_wait_scs_smem`, not through a TAC path.

---

## Bandwidth and Back-Pressure at the Boundary

SC↔TC and SC↔HBM traffic share the on-chip HBM controller, arbitrated on a credit basis. The relevant primitives:

| Mechanism | Symbol / op | Role |
|---|---|---|
| Per-channel DMA credit | `sc_tpu.set_dma_credit` | TC has 12 DMA channels, SC has 4; credits prevent monopolizing the HBM bus |
| Throttle on sflag range | `sc_tpu.set_dma_throttle_sflag_range` | back-pressure the SC stream engine when HBM is saturated |
| Cost-model bandwidth share | `backend_config_util::GetSparseCoreHbmBandwidthAdjustmentFactor` | per-HLO-op fractional HBM-bandwidth budget |
| Concurrent-offload limit | `FLAGS_xla_tpu_sparse_core_offload_queuing_overlap_limit` | max SC-offload tasks in flight (per-gen tuned) |
| Channel policy | chip-config protos (`*_chip_configs_legacy_sparse_core.binarypb`) | random-access prefetch for SC, sequential-stream for TC |

The two engines use distinct HBM MMU page-prefetcher policies — random-access for SC (single-row gather), sequential-stream for TC (contiguous tile fetch) — over the same DRAM. This is why the `kStream` / `kDma` classification matters beyond the bundle slot: it also selects the HBM channel policy.

---

## Error Handling at the Boundary

| Failure | Mechanism | Detail |
|---|---|---|
| MXU starvation (TC waits for SC) | `tpu.sem_wait` finite timeout | `FLAGS_xla_tpu_debug_sc_sflag_wait_timeout_ms`; on timeout TC issues a controlled halt + writes a diagnostic record naming the source HLO instruction |
| SC stall | SCS `Halt` opcode | controlled halt with a status code; no interrupt — the host polls the sequencer status register; the SDC checker keeps running |
| Sync-flag use-def error | `SyncFlagVerifierPass` (compile-time) | every wait must have a matching producer; every producer must count a threshold for ≥1 consumer |
| Wait-stat telemetry | `SflagWaitInstrumentationPass` | inserts wait-statistics counters at SC↔TC boundaries (`xla_tpu_collect_sflag_wait_stats_filter`) |
| SDC mismatch | `TPU_MEMORY_RESERVATION_TYPE_SPARSE_CORE_SDC_CHECKER_REPORT_SYNC_FLAG` | dedicated SFLAG range the SDC checker sets on a row-checksum mismatch |

The verifier can be opted out per-kernel via the `sc.disable_before_use_sync_flag_verification` / `sc.disable_after_use_sync_flag_verification` HLO attributes (for hand-written kernels). `xla_sc_conservative_sflag_aliasing` forces the verifier to disallow any SFLAG-range aliasing — a debugging mode for stuck pipelines.

---

## Limits and Open Items

| Item | Status |
|---|---|
| 40 `tpu_dma_*_sc_*` ops; only 3 name `vmem`; `general`-form only | enumerated in decompile |
| `GetTransferKind` signature `(Target&, MemorySpace, MemorySpace, 4×bool)` | demangled symbol + `kStream`/`kDma` asserts |
| SFLAG 3-subspace model + region-scoping assertions | assertion strings read |
| TC-side `Semaphore{Wait,Signal,Read}Op` + `FetchAndAddSyncOp` | symbols present |
| `SyncScsWithTec` / `SyncTecWithScs` / `AllocateSflag(.,false,2)` double-buffer | function bodies located; `SyncWait` in body |
| `MxuContractingSize` 128/256; `MxuSparseContractingSize` = 0 | per-codename literals (see target descriptor) |
| `DeviceAndCoreIds` byte layout (routing key) | recovered as `optional<>`; field widths not decomposed |
| Per-gen SFLAG remote-address bit-encoding | encoders located; bit composition not decoded |
| `EmbeddingsPassType` enum value list | parameter of `GetLogicalReplicaInfo`; values not exposed as strings |
| `TileTaskOp` access-vs-execute region content rules | op present (`MloModuleVerifier::Verify(TileTaskOp)` `0x146aca00`); exact region count and per-region legality predicate not cleanly decompiled |
| Exact `EnqueueDMAOp::getPriority()` value space | getter present; arbitration rules not traced |

---

## Cross-References

- [SparseCore Architecture](architecture.md) — the SC geometry, the four-tier memory model, and the `SparseCoreTarget` (`Target+0x948`) the contracting binding is read from.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the intra-SC indirect-DMA datapath (the `IndirectStream` slot, the per-element address formula) that produces the tile this page hands to the MXU.
- [MXU Slot](../isa/slot-mxu.md) — the TC MXU op family (`vlatch` / `vmatprep` / `vmatmul` / `vmatres`), the 128×128 systolic array, and the `GainLatchMode` doubling modes this page's contracting binding feeds.
- [MATPREP / IAR Latch Slot](../isa/slot-matprep-iar-latch.md) — the moving-operand staging slot the SC-produced VMEM tile enters through.
- [SparseCoreTarget (`Target+0x948`)](../targets/sparsecore-target-descriptor.md) — the byte-exact `MxuContractingSize` / `MxuSparseContractingSize` / doubling-predicate table this page consumes.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the SC-MLO pass pipeline (and the HLO `sparse_dense_matmul_*` decomposers) that builds the two halves of the boundary.
- [SC Core Selection](sc-core-selection.md) — how a computation is assigned to a physical SparseCore partition (the `DeviceAndCoreIds` routing).
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC selection that picks the engine issuing each transfer.
- [TAC Engine](tac-engine.md) — the Tile-Access sub-engine absent on TPU7x (6acc60406); the `tile_wait_scs_smem` replacement.
- [SparseCore vs Neuron MatmultSparse](sparsecore-vs-neuron-matmultsparse.md) — cross-vendor comparison of the gather-then-matmul boundary.
- [SparseCore Overview](overview.md) — the navigational entry for Part IX.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore cross-cutting — [back to index](../index.md)
