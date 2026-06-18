# NeuronCodegen Collective Forward Builders

> *All symbols, addresses, and source line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, subject binary `KernelBuilder.cpython-310-x86_64-linux-gnu.so` (Cython, **unstripped, DWARF**) under `neuronxcc/nki/compiler/backends/neuron/`. The cp311/cp312 wheels carry the same class but VAs drift; the `KernelBuilder.py` line numbers, the Op-class names, and the kwarg sets are build-invariant. Treat every VA as cp310-pinned.*

## Abstract

When NKI-Python code (or an `ncc.*` collective primitive) asks for a cross-rank operation, the request lands on a method of one Cython class: `neuronxcc.nki.compiler.backends.neuron.KernelBuilder.NeuronCodegen`. This class is the **NKI-side forward builder for collectives** — the layer that turns a high-level `ncc.all_gather(...)` or `ncc.collective_permute_implicit(...)` call into a Penguin-IR op object and threads it into the instruction stream. It does *not* write any BIR `InstCollectiveCompute` fields. The numeric `bir::CollectiveKind`, the `cc_dim`, and the materialized `replica_groups` are stamped **later**, by the KLR→BIR collective codegen ([6.5.13](./bircodegen-collective.md), and the `codegenCollectiveOp` / `codegenSendRecv` / `codegenCoreBarrier` leaves in libwalrus). NeuronCodegen's job is narrower and earlier: pick the right **Op class**, fill its routing fields, and insert.

The roster splits cleanly along two axes that a reimplementer must get right. The first axis is the `*_cc` vs `*_cc_raw` distinction, which is a **replica-group-source split**: a `*_cc_raw` method takes the routing data (`replica_groups`, dims, `src_tgt_pairs`) *from the caller* and is the escape hatch the fine-grained nkilib kernels use; the matching `*_cc` method takes none of that, synthesizes the full replica-group set from the distributed topology via `parallel_state.gen_all_worker_group(...)`, and then delegates to its `*_cc_raw` sibling. The second axis is the **insertion channel**: the group collectives and the raw send/recv use `insert_raw_cc` (which sets `has_collectives` and emits an abstract CC descriptor); the point-to-point `sendrecv`/`sendrecv_cce`, `core_barrier`, and `rank_id` use the ordinary tile-aware `self.insert`, because they weave a barrier or a register into the *local* instruction stream rather than emitting a pure routing descriptor.

This page enumerates every collective forward builder, reproduces the `*_cc_raw` construction pattern as annotated pseudocode, names the Op class each method mints, and maps that Op class to the downstream `CollectiveKind`. The Op→Kind binding is *by class* and resolved downstream, so this page draws the bridge and points at where the number is actually written.

| | |
|---|---|
| **Class** | `NeuronCodegen` in `KernelBuilder.cpython-310-…so` (`KernelBuilder.py`) |
| **Method-table prefix** | `__pyx_pw_…_13KernelBuilder_13NeuronCodegen_<idx><name>` |
| **Group emitters** | `all_reduce` · `all_gather` · `reduce_scatter` · `collective_permute` · `broadcast` · `all_to_all` (each `_cc` + `_cc_raw`) |
| **Tiled permutes** | `tiled_collective_permute_cc_raw` (139) · `…_implicit_cc_raw` (141) · `…_reduce_implicit_cc_raw` (143) |
| **Point-to-point** | `send_cc_raw` (153) · `recv_cc_raw` (155) · `sendrecv` (275) · `sendrecv_cce` (277) |
| **SPMD / sync** | `core_barrier` (373) · `rank_id` (377) · `sync_program` (43) · `post_process_spmd_grid` (32) |
| **CC sink** | `insert_raw_cc` (305) @ `0xff350` — sets `has_collectives`, calls `self.insert_raw` |
| **Op-class home** | `neuronxcc/starfish/penguin/ir/CollectiveOp.cpython-310-…so` |

## 1. The method roster

The Cython method table indexes each method as `NeuronCodegen_<idx><name>` (the `__pyx_mdef_…` data symbols), with a public-wrapper body at `__pyx_pw_…`. The table below is `nm`-verified against `KernelBuilder.cpython-310-…so`; the **KB.py line** column is from `addr2line` on the `pw` body's first instruction (DWARF), which is the authoritative per-build line.

| idx | method | `pw` VA | KB.py line | channel |
|----:|--------|---------|-----------:|---------|
| 121 | `all_reduce_cc_raw` | `0x109530` | 1478 | `insert_raw_cc` |
| 123 | `all_reduce_cc` | `0xf6b90` | 1516 | → `all_reduce_cc_raw` |
| 127 | `all_gather_cc_raw` | `0xf4ea0` | 1532 | `insert_raw_cc` |
| 129 | `all_gather_cc` | `0xc76d0` | — | → `all_gather_cc_raw` |
| 131 | `reduce_scatter_cc_raw` | `0xf31b0` | — | `insert_raw_cc` |
| 133 | `reduce_scatter_cc` | `0xc8a50` | — | → `reduce_scatter_cc_raw` |
| 135 | `collective_permute_cc_raw` | `0xfb210` | 1572 | `insert_raw_cc` |
| 137 | `collective_permute_cc` | `0xd08a0` | 1586 | → `collective_permute_cc_raw` |
| 139 | `tiled_collective_permute_cc_raw` | `0xec3c0` | — | `insert_raw_cc` |
| 141 | `tiled_collective_permute_implicit_cc_raw` | `0x1a2050` | — | `insert_raw_cc` |
| 143 | `tiled_collective_permute_reduce_implicit_cc_raw` | `0x1b8600` | — | `insert_raw_cc` |
| 145 | `broadcast_cc_raw` | `0x13a2e0` | — | `insert_raw_cc` |
| 147 | `broadcast_cc` | `0x94d40` | — | → `broadcast_cc_raw` |
| 149 | `all_to_all_cc_raw` | `0x19e800` | — | `insert_raw_cc` |
| 151 | `all_to_all_cc` | `0xb3690` | — | → `all_to_all_cc_raw` |
| 153 | `send_cc_raw` | `0x253c40` | — | `insert_raw_cc` |
| 155 | `recv_cc_raw` | `0x2553e0` | — | `insert_raw_cc` |
| 275 | `sendrecv` | `0x17a1f0` | 4271 | `self.insert` |
| 277 | `sendrecv_cce` | `0x2306c0` | 4308 | `self.insert` |
| 305 | `insert_raw_cc` | `0xff350` | 4764 | → `self.insert_raw` |
| 167 | `post_process_allreduce_result` | `0xde5c0` | — | `Barrier` fixup |
| 373 | `core_barrier` | `0x1e6d70` | 5848 | `self.insert` |
| 377 | `rank_id` | `0x11f6f0` | 5899 | `self.insert` |
| 43 | `sync_program` | `0x930f0` | — | program-axis sync |
| 32 | `post_process_spmd_grid` | `0x12e9e0` | — | SPMD grid setup |

> **CORRECTION — backing-report line numbers were the `def` line, not the `pw` body.** The D-P04 report quoted `all_reduce_cc_raw` at KB.py:1472, `all_reduce_cc` at :1510, `insert_raw_cc` at :4758, `core_barrier` at :5842, `rank_id` at :5893, `sendrecv` at :4265, `sendrecv_cce` at :4302. `addr2line -e KernelBuilder.cpython-310-…so` on the `pw` entry points six lines later in each case (`:1478`, `:1516`, `:4764`, `:5848`, `:5899`, `:4271`, `:4308`). The ~6-line delta is the gap between the Python `def` line (where `_Pyx_TraceSetupAndCall` registers the name) and the first executable line of the inlined wrapper body (the DWARF line for `pw`'s entry). The table above uses the DWARF line. The methods, VAs, and indices are unchanged.

> **NOTE — `broadcast_partition` (idx 19/231) is *not* a collective.** The symbol table also carries `NeuronCodegen_231broadcast_partition` (`0xad480`) and its nested `build_broadcast_partition` (`0x1beaf0`). Despite the name, this is the SBUF partition-broadcast tile helper, not a cross-rank `BroadcastOp` emitter; it does not touch `insert_raw_cc`. The cross-rank broadcast is `broadcast_cc` / `broadcast_cc_raw` (idx 145/147).

## 2. The group collectives — `*_cc_raw` construction

Every `*_cc_raw` body has the same shape. The arg-parsing and refcount churn make the `pw` body byte-large, but the recovered call sequence (from the `__pyx_n_s_*` name-constant refs and the helper symbols) is small and uniform:

```c
// NeuronCodegen.<coll>_cc_raw — the low-level escape hatch.
// Caller supplies replica_groups + dims; this builds the Op and inserts it raw.
PyObject* <coll>_cc_raw(self, op, srcs, dsts, replica_groups,
                        /* <dim kwargs> */, dtype /*=None*/,
                        /* [tiled] */, dma_qos)
{
    validate_dma_qos(dma_qos);                       // __pyx_n_s_validate_dma_qos

    // TensorRef operands -> nd-access descriptors (HBM operands use the HBM variant)
    srcs = self._map_to_nd_access(srcs);             // _map_to_nd_access  (idx 124/125)
    dsts = self._map_to_nd_access(dsts);             // (permute uses _map_to_hbm_tensors, idx 118/119)

    op_obj = <Kind>Op(op            = op,            // reduce-op (AluOp) for the reducers
                      srcs          = srcs,
                      dsts          = dsts,
                      replica_groups= replica_groups,// vector<vector<u32>> rank groups
                      <dim kwarg>   = <dim>,         // cc_dim source, ∈ {0,1}
                      dtype         = dtype,
                      dma_qos       = dma_qos);

    return self.insert_raw_cc(op_obj);               // -> sets has_collectives, insert_raw
}
```

The only per-family differences are the **Op class** (which fixes the downstream `CollectiveKind`) and the **dim kwarg**. All Op-class names below are confirmed as strings inside both `KernelBuilder.cpython-310-…so` (the construction site, 3 refs each — the Cython `n_s`/`n_u`/`k` triple) and `CollectiveOp.cpython-310-…so` (the class home).

| `*_cc_raw` | Op class | dim / routing kwarg(s) | reduce-op? |
|------------|----------|------------------------|:----------:|
| `all_reduce_cc_raw` | `AllReduceOp` / `TiledAllReduceOp` | *(none — spans whole group)* | yes |
| `all_gather_cc_raw` | `AllGatherOp` | `all_gather_dim` | no |
| `reduce_scatter_cc_raw` | `ReduceScatterOp` | `reduce_scatter_dim` | yes |
| `collective_permute_cc_raw` | `CollectivePermuteOp` | *(routed by `replica_groups`)* | no |
| `broadcast_cc_raw` | `BroadcastOp` | `broadcast_sizes` | no |
| `all_to_all_cc_raw` | `AlltoAllOp` | `split_dimension` + `concat_dimension` | no |

A few details worth calling out for a reimplementer:

- **`all_reduce_cc_raw` has a `tiled` branch.** Both `AllReduceOp` and `TiledAllReduceOp` are present as name-constants in the binary, and the body runs a `PyObject_IsTrue(tiled)` test to pick between them. AllReduce takes **no** dim kwarg — it reduces across the entire group — which matches the downstream codegen storing only the reduce-op, no `cc_dim`. → `CollectiveKind = AllReduce(2)`.
- **`all_gather_cc_raw`'s `all_gather_dim`** becomes the BIR `cc_dim`, asserted downstream to be in `{0, 1}` (`Partition`/`Free`). No reduce-op is consumed. → `AllGather(4)`.
- **`reduce_scatter_cc_raw`** is the one group collective carrying *both* a reduce-op and a dim (`reduce_scatter_dim`). → `ReduceScatter(3)`.
- **`all_to_all_cc_raw`** carries **two** axes, `split_dimension` and `concat_dimension`. `CollectiveOp.so` asserts both are present — the verbatim strings `Illegal AlltoAll without split_dimension` and `Illegal AlltoAll without concat_dimension` are in that module. → `AllToAll(5)`.
- **`broadcast_cc_raw`** is the odd one: it uses `broadcast_sizes` (the per-rank broadcast extents) in place of a single dim. There is **no** `Broadcast` value in the downstream `CollectiveKind` enum; the broadcast is realized as a `Permute`/`AllGather`-class CC plus the `broadcast_sizes` geometry, resolved in `CollectiveOp.so` lowering. *(STRONG — `BroadcastOp` and `broadcast_sizes` are confirmed; the exact downstream kind is resolved in a module not byte-walked here.)*

> **QUIRK — permute operands map through `_map_to_hbm_tensors`, the group reducers through `_map_to_nd_access`.** Both helpers exist as their own methods (`_map_to_hbm_tensors` idx 118/119, `_map_to_nd_access` idx 124/125). The permute path treats its operands as whole HBM tensors; the reducer/gather paths map to nd-access descriptors. A reimplementer who routes permute operands through the nd-access mapper will mis-shape the permute.

## 3. The high-level `*_cc` — replica-group generation and delegation

The non-raw `*_cc` methods are thin. They never accept `replica_groups`; instead they synthesize the comm-group topology from the distributed `parallel_state` module and then call the matching `*_cc_raw`:

```c
// NeuronCodegen.<coll>_cc — the convenient TP/DP API.
// User names only op/dim semantics; group membership is derived from the topology.
PyObject* <coll>_cc(self, op, srcs, dsts, /* <dim kwargs> */, dtype, dma_qos)
{
    replica_groups = parallel_state.gen_all_worker_group(...);   // "all ranks, one group"
    return self.<coll>_cc_raw(op, srcs, dsts,
                              replica_groups = replica_groups,
                              /* <dim kwargs forwarded> */,
                              dtype, dma_qos);
}
```

The five group reducers/gathers (`all_reduce_cc`, `all_gather_cc`, `reduce_scatter_cc`, `broadcast_cc`, `all_to_all_cc`) all call `parallel_state.gen_all_worker_group`. The one exception is **`collective_permute_cc`** (`0xd08a0`, KB.py:1586), which instead calls `parallel_state.gen_all_collective_permute_config(...)` — a permute needs a routing config, not a flat worker group — and forwards that into `collective_permute_cc_raw`. Both `gen_all_worker_group` and `gen_all_collective_permute_config` are confirmed as string refs in the binary; their bodies live in `parallel_state` (a separate module, not walked here, so the exact default group geometry is **INFERRED**).

> **The raw-vs-high-level distinction, definitively.** Both tiers funnel into the *same* `<Kind>Op` + `insert_raw_cc`. The only difference is **who produced `replica_groups`**:
> - **`*_cc` (high-level):** the user names only the op/dim semantics; group membership is derived from the global parallel topology (`gen_all_worker_group` = "all ranks in one worker group"). This is the convenient TP/DP front door.
> - **`*_cc_raw` (raw):** the user supplies `replica_groups` (a `vector<vector<u32>>` of rank-id groups) and the exact dims/pairs. This is the escape hatch the fine-grained nkilib collective kernels (fgcc / fg_allgather / sb2sb) ride — they build their own replica groups and call the raw layer (or `ncc.*`, which bottoms out here).

The `replica_groups` `vector<vector<u32>>` minted here is exactly what the downstream collective codegen materializes into `cc_channel.replica_groups`; the dim kwargs are exactly what it asserts `∈ {0,1}` and stores as `cc_dim` (`CollectiveDimension` `Partition(0)` / `Free(1)`). End-to-end consistent. See [6.5.13](./bircodegen-collective.md).

## 4. The tiled permutes — the fine-grained / multi-channel ring

Three `tiled_*` raw emitters implement the chunked permute the nkilib ring kernels ride. All three build a `Tiled*` Op and `insert_raw_cc`; the `tiled` prefix means per-tile chunking via `combine_tiles` (the fine-grained granularity). The distinguishing kwargs are the whole story:

```c
// (A) explicit edges — routes by src_tgt_pairs, NOT replica_groups
tiled_collective_permute_cc_raw(0xec3c0):
    op_obj = TiledCollectivePermuteOp(srcs, dsts,
                                      src_tgt_pairs = ...,   // explicit src->tgt edges
                                      ... ) via self.combine_tiles(...)
    self.insert_raw_cc(op_obj)                               // -> Permute(7)

// (B) implicit ring — routes by replica_groups + carries channel_id / num_channels
tiled_collective_permute_implicit_cc_raw(0x1a2050):
    op_obj = TiledCollectivePermuteOp(srcs, dsts,
                                      replica_groups = ...,
                                      channel_id     = ...,  // multi-channel CC index
                                      num_channels   = ...)  // CHANNEL_N (1/2/4)
    self.insert_raw_cc(op_obj)                               // -> PermuteImplicit(9) + channel_id

// (C) fused permute-then-reduce
tiled_collective_permute_reduce_implicit_cc_raw(0x1b8600):
    op_obj = TiledCollectivePermuteReduceOp(op,             // reduce AluOp
                                            replica_groups = ...,
                                            channel_id     = ...,
                                            num_channels   = ...)
    // guards "err_instruction_unsupported_op" if op invalid
    self.insert_raw_cc(op_obj)                               // -> PermuteReduceImplicit(10)
```

| tiled emitter | Op class | routing | BIR kind |
|---------------|----------|---------|----------|
| `tiled_collective_permute_cc_raw` | `TiledCollectivePermuteOp` (pairs) | `src_tgt_pairs` | `Permute(7)` |
| `tiled_collective_permute_implicit_cc_raw` | `TiledCollectivePermuteOp` (implicit) | `replica_groups` + `channel_id`/`num_channels` | `PermuteImplicit(9)` |
| `tiled_collective_permute_reduce_implicit_cc_raw` | `TiledCollectivePermuteReduceOp` | 2-in/1-out + `op` + `channel_id` | `PermuteReduceImplicit(10)` |

The two routing styles mirror the two non-tiled permute forms: `collective_permute_cc_raw` (idx 135) routes by `replica_groups` (implicit), while the *explicit* `src_tgt_pairs` style first appears at the tiled layer. The `tiled_collective_permute_implicit_cc_raw` emitter is exactly what `ncc.collective_permute_implicit` lowers to — the multi-channel, double-buffered ring step the fine-grained nkilib kernels use, with `num_channels` carrying the `CHANNEL_N` (1/2/4) selection (by `tp_degree` and `nc_version`). The per-iteration rank resolver those kernels also call (`current_processing_rank_id`) is **not** a `NeuronCodegen` method — see [§6](#6-the-rank-resolver-is-ncc-side-not-here).

> **GOTCHA — `channel_id`/`num_channels` are tiled-permute-only.** The non-tiled `collective_permute_cc_raw` carries neither. If you target the multi-channel ring, you must go through the tiled-implicit emitter; the plain `collective_permute_cc` API gives you a single-channel implicit permute.

## 5. Point-to-point — `send`/`recv`, `sendrecv`, `sendrecv_cce`

There are two tiers of point-to-point, matching the BIR split.

**The raw descriptors.** `send_cc_raw` (`0x253c40`) and `recv_cc_raw` (`0x2553e0`) mint a bare `SendOp` / `RecvOp` and `insert_raw_cc`. These are the lowest-level "just emit a send/recv descriptor" escape hatch — they carry only a peer id, no `CollectiveKind`, and lower to the BIR `Send`/`Recv` instructions. `SendOp` and `RecvOp` are confirmed in `CollectiveOp.so`'s class roster.

**The fused, tile-aware `sendrecv`.** This is the high-level entry the GPSIMD SB2SB path uses. Unlike the group emitters, it does **not** call `insert_raw_cc` — it uses the ordinary tile-aware `self.insert`, because it weaves a cross-core barrier into the local instruction stream:

```c
// NeuronCodegen.sendrecv (0x17a1f0, KB.py:4271)
sendrecv(self, dst, src, send_to_rank, recv_from_rank, pipe_id,
         use_gpsimd_dma, ..., dma_qos, mask, deps, name)
{
    validate_dma_qos(dma_qos);
    // tile bookkeeping over canonical par/free indices:
    get_index_e(...); get_tile_with_local_tensor(...); combine_tiles(...);
    initiate_barrier(...);                         // cross-core fence

    op_obj = SendRecvOp(dst, src, send_to_rank, recv_from_rank,
                        pipe_id, use_gpsimd_dma, ...);
    self.insert(op_obj);                            // tile-aware, NOT insert_raw_cc
    // -> CollectiveKind = SendRecv(0), is_local = true
}
```

`send_to_rank`, `recv_from_rank`, `pipe_id`, and `use_gpsimd_dma` are all confirmed name-constants in the binary. This `SendRecvOp` is the op the on-chip lowering maps to a GPSIMD SB2SB transfer when it is compatible and stays within the per-partition byte budget, else a DMA copy.

> **NOTE — `use_gpsimd_dma` is captured here but dropped one layer down.** The KLR→BIR `codegenSendRecv` leaf hard-codes `useGpsimdDma = false` and ignores the klr field. Here, one level up at the NKI layer, `sendrecv` *does* take and pass `use_gpsimd_dma` into `SendRecvOp`. So the user-facing flag is genuinely captured into the Penguin op; it is only dropped at the klr→BIR boundary, with the GPSIMD-vs-DMA decision deferred to the on-chip `lowerSendRecv` (an `isCompatible` ∧ ≤1024 B/partition gate). The layering is consistent: NKI carries it, BIR-codegen drops it, on-chip lowering decides it.

**The collective-comm-engine multi-recv, `sendrecv_cce`.** This variant (`0x2306c0`, KB.py:4308) receives from **multiple** peers and reduces:

```c
// NeuronCodegen.sendrecv_cce (0x2306c0, KB.py:4308)
sendrecv_cce(self, dst, src, send_to_rank, recv_from_ranks, op,   // recv_from_ranks PLURAL
             pipe_id, ..., dma_qos, ...)
{
    ...
    op_obj = SendRecvCCEOp(dst, src, send_to_rank, recv_from_ranks, op, pipe_id, ...);
    self.insert(op_obj);                            // -> CollectiveKind = SendRecvCCE(1)
}
```

Both `recv_from_ranks` (plural — confirmed distinct from the singular `recv_from_rank` used by `sendrecv`) and the reduce `op` are present. `SendRecvCCEOp` is referenced as a name-constant in the binary.

> **CORRECTION-ASSIST — `sendrecv_cce` is a *second* origin of `CollectiveKind = SendRecvCCE(1)`.** The KLR→BIR analysis found no `codegenSendRecvCCE` leaf and attributed the kind-1 origin solely to the collectives-to-CC HLO path. This NKI emitter is a second, independent origin: `NeuronCodegen.sendrecv_cce` mints `SendRecvCCEOp` directly, so kind 1 can enter the IR from the NKI front-end without ever passing through a klr `SendRecv` leaf. Both paths can mint kind 1.

> **GOTCHA — `SendRecvOp` / `SendRecvCCEOp` are not in `CollectiveOp.so`'s class roster.** Unlike the group Op classes (which are all named strings in `CollectiveOp.cpython-310-…so`), the `SendRecvOp` and `SendRecvCCEOp` *type* names appear only as construction-site name-constants inside `KernelBuilder.so` itself (3 refs each), not as standalone class strings in the penguin `CollectiveOp`/`Barrier`/`ir` modules scanned here. The backing report placed them in `Barrier.so`/`ir.so`; that could not be confirmed against the binary. Their definitions live in a sibling module not pinned on this page (tagged **INFERRED** for the exact home; the construction in `KernelBuilder.so` is **CONFIRMED**).

## 6. SPMD and sync — `core_barrier`, `rank_id`, `sync_program`

These are control-plane and SPMD-grid primitives. Like `sendrecv`, they use `self.insert` (not `insert_raw_cc`) — they bind a register or a barrier into the local stream.

### `core_barrier` (`0x1e6d70`, KB.py:5848) — the LNC cross-core rendezvous

```c
core_barrier(self, wait_target, cores, engine, ...)
{
    // the wait target MUST be a full HBM tensor:
    nki_assert(is_hbm(wait_target),       "core_barrier can only wait for HBM Tensor.");
    nki_assert(is_full_tile_ap(wait_target),
        "core_barrier can only wait for full HBM Tensor, "
        "but tile of HBM Tensor is passed in.");

    sema   = LocalTensor(sema, shape);    // SBUF semaphore
    sema   = self.combine_tiles(sema);
    op_obj = CoreBarrierOp(data = sema, cores = cores, engine = engine);

    register program_axes / program_barriers;   // front-end bookkeeping
    self.insert(op_obj);                          // -> InstCoreBarrier
}
```

Both error strings are verbatim in the binary. The `data` SBUF semaphore is bound as arg+output (read-modify-write), `cores` is the active-core id list, and `engine` selects the engine. The `program_barriers`/`program_axes` registered here are the front-end bookkeeping a later barrier-renumbering pass turns into the module-wide barrier index. CoreBarrier is gen3+/Trn2 (LNC) — the downstream verifier gates it on an arch level ≥ 30. `CoreBarrierOp` is confirmed as a string in `CollectiveOp.so` (see correction below). See [6.5.13](./bircodegen-collective.md) and [Part 13](../collectives/).

> **CORRECTION — `CoreBarrierOp`'s class home is `CollectiveOp.so`, not `Barrier.so`.** The backing report listed `CoreBarrierOp` under `Barrier.so`. A content scan of the penguin `ir/` modules finds the `CoreBarrierOp` string only in `CollectiveOp.cpython-310-…so`; `Barrier.cpython-310-…so` carries the generic `Barrier` op, not `CoreBarrierOp`.

### `rank_id` (`0x11f6f0`, KB.py:5899) — materialize the global rank

```c
rank_id(self, dst, world_size, mask, ...)
{
    nki_assert(predicates.is_scalar(mask), "rank_id can only support scalar mask");

    dst_register = DynamicScalar(np.uint32);          // a uint32 register
    op_obj       = GetGlobalRankId(dst_register, world_size, ...);
    self.insert(op_obj);                               // -> InstGetGlobalRankId, writes uint32
}
```

The `rank_id can only support scalar mask` guard is verbatim. The emitter names `dst_register` (a `DynamicScalar` of `np.uint32`) and `world_size` (carried as an attr); the runtime rank value is resolved later (`rank = core / numCoresPerLNC`). `GetGlobalRankId` is confirmed as a string in `IRWriter.cpython-310-…so` (the class home that serializes it) and as a construction-site name-constant in `KernelBuilder.so`.

### `rank resolver is ncc-side, not here`

`NeuronCodegen` has **no** public in-group (TP) rank resolver in this method roster. The per-iteration rank resolver the ring kernels use — `collective_permute_implicit_current_processing_rank_id` — is exposed via the `ncc.*` API / a separate intrinsic and lowers to `InstGetCurProcessingRankID`, not through any `NeuronCodegen_<idx>` method here. *(STRONG — absence verified: no such symbol in the `NeuronCodegen_<idx>` table.)*

### `sync_program` (`0x930f0`) — program-axis synchronization

`sync_program` (idx 43) iterates `inactive_program_axes` through a generator (its closure scope is `__pyx_scope_struct_9_sync_program`) and emits barriers/fences to sync the SPMD program grid. It mints no Op class — it orchestrates barriers — and is the program-level (not per-CC) sync that brackets collective regions. *(STRONG — name refs and the closure scope confirmed; the exact barrier construction is the generator's and was not fully walked.)*

## 7. The CC sink and the post-processors

### `insert_raw_cc` (`0xff350`, KB.py:4764) — the single sink

Every `*_cc_raw` op funnels through one method:

```c
insert_raw_cc(self, inst)
{
    self.has_collectives = True;          // mark: this kernel contains collectives
    return self.insert_raw(inst);
}
```

Both `has_collectives` and `insert_raw` are confirmed name-constants. `insert_raw_cc` *is* `insert_raw` plus the `has_collectives` side-effect: that flag is the signal downstream passes read to know they must run the collective lowering and barrier insertion. The point-to-point and SPMD ops (§5–§6) bypass this sink precisely because they go through `self.insert` instead — they do not flip `has_collectives`, because they are local-stream ops, not abstract CC descriptors.

### `post_process_allreduce_result` (`0xde5c0`) — the AllReduce fixup

The post-AllReduce finalizer iterates `reduce_axes` / `parallel_axes` (with an `async` flag and a loop-reduce dst) and inserts `Barrier` / `program_barriers` plus `translate_axis`. It barriers the (possibly async) all-reduce result across the parallel axes so consumers see the reduced value. Its guards are the verbatim AllReduce-axis errors `program_axes of all_reduce must be program_id! Consider use loop_reduce or reduce instead.` and `reduce_axes of all_reduce cannot be program_id!`. *(STRONG — refs + error strings.)*

### `post_process_spmd_grid` (`0x12e9e0`, idx 32) — SPMD grid setup

Not a collective per se: it builds the kernel scope / launch grid via `CompositeSPMDDim`, `track_axes`, interchange, and `program_ids` over the loopnest. It is the SPMD launch-grid post-processing the rank/barrier ops are defined against. *(STRONG — refs only.)*

## 8. The Op-class → BIR-kind bridge

`NeuronCodegen` chooses the **Op class**; the numeric `bir::CollectiveKind` is stamped downstream by the KLR→BIR codegen ([6.5.13](./bircodegen-collective.md)). The group Op classes are defined in `neuronxcc/starfish/penguin/ir/CollectiveOp.cpython-310-…so`, whose class roster is confirmed verbatim: `AllGatherOp · AllReduceOp · AlltoAllOp · BroadcastOp · CollectiveComputeOp · CollectiveOp · CollectivePermuteOp · CollectivePermuteReduceOp · RecvOp · ReduceScatterOp · SendOp · ShardOp` (plus `CoreBarrierOp`). `CollectiveOp.so` also carries the `CollectiveKind` name pool — `AllGather · AllReduce · AllToAll · Permute · PermuteReduce · ReduceScatter` (all six confirmed) — and the asserts (`Illegal AlltoAll without {split,concat}_dimension`, `Collective PermuteReduce is not supported for now`). It is the layer that resolves the Op class to the numeric kind.

| NeuronCodegen Op class | BIR `CollectiveKind` | routing data |
|------------------------|----------------------|--------------|
| `AllReduceOp` / `TiledAllReduceOp` | `AllReduce(2)` | `replica_groups` + reduce-op |
| `ReduceScatterOp` | `ReduceScatter(3)` | `replica_groups` + op + dim |
| `AllGatherOp` | `AllGather(4)` | `replica_groups` + dim |
| `AlltoAllOp` | `AllToAll(5)` | `replica_groups` + split/concat dim |
| `CollectivePermuteOp` | `PermuteImplicit(9)` | `replica_groups` |
| `TiledCollectivePermuteOp` (pairs) | `Permute(7)` | `src_tgt_pairs` |
| `TiledCollectivePermuteOp` (implicit) | `PermuteImplicit(9)` | `replica_groups` + `channel_id` |
| `TiledCollectivePermuteReduceOp` | `PermuteReduceImplicit(10)` | 2-in/1-out + op + `channel_id` |
| `BroadcastOp` | *(Permute/AllGather-class + `broadcast_sizes`)* | `replica_groups` + sizes |
| `SendRecvOp` | `SendRecv(0)`, `is_local` | send/recv rank + `pipe_id` |
| `SendRecvCCEOp` | `SendRecvCCE(1)` | send rank + `recv_from_ranks` + op |
| `SendOp` / `RecvOp` | `Send` / `Recv` (peer_id) | peer rank |
| `CoreBarrierOp` | `InstCoreBarrier` | SBUF sema + cores + engine |
| `GetGlobalRankId` | `InstGetGlobalRankId` | dst_register + world_size |

> **NOTE — the numeric kind is resolved downstream, not here.** This table's kind column is the *downstream* binding (where the number is written). The per-Op numeric `kind` constant lives in `CollectiveOp.so`'s ctors and was not byte-walked on this page; the kind **name pool** is confirmed present, and the Op→kind correspondence is cross-checked against the KLR→BIR codegen. The `BroadcastOp` row is the weakest: there is no `Broadcast` enum value, and its realization (a Permute/AllGather-class CC carrying `broadcast_sizes`) is resolved in `CollectiveOp.so` lowering (**STRONG/INFERRED**).

## 9. Adversarial self-verification

The five strongest claims on this page, re-challenged against the binary:

1. **The 24-method roster + VAs (§1).** Re-ran `nm` on `KernelBuilder.cpython-310-…so`: every `__pyx_pw_…NeuronCodegen_<idx><name>` and its VA match the table, including the three tiled permutes (139/141/143) the report under-listed. **CONFIRMED.**
2. **The line numbers (§1).** `addr2line` on the `pw` entries returns lines six greater than the report's. Resolved in-place as a CORRECTION (def-line vs body-first-line); table uses the DWARF line. **CONFIRMED + corrected.**
3. **The `*_cc_raw` → `<Kind>Op` → `insert_raw_cc` pattern, and `*_cc` → `gen_all_worker_group` delegation (§2–§3).** All Op-class names, the `validate_dma_qos`/`_map_to_nd_access`/`_map_to_hbm_tensors`/`insert_raw_cc`/`insert_raw`/`has_collectives` helpers, the dim kwargs, and `gen_all_worker_group`/`gen_all_collective_permute_config` are confirmed strings. The `_map_to_*` helpers carry a leading underscore (corrected from the report's `map_to_*`). **CONFIRMED.**
4. **The tiled-permute three-way split (§4).** `src_tgt_pairs`, `channel_id`, `num_channels`, and the three Tiled Op classes are all confirmed strings; the three emitters are at the listed VAs. **CONFIRMED.**
5. **Op-class homes and the CollectiveOp roster (§8).** Re-scanned the penguin `ir/` modules: the group Op classes and the kind name pool are in `CollectiveOp.so`; `CoreBarrierOp` is also in `CollectiveOp.so` (**not** `Barrier.so` — corrected); `GetGlobalRankId` is in `IRWriter.so`. `SendRecvOp`/`SendRecvCCEOp` appear only as construction-site name-constants in `KernelBuilder.so`, so their class home is left **INFERRED** with a GOTCHA. **CONFIRMED with two corrections.**

**Anchors:** subject binary `KernelBuilder.cpython-310-x86_64-linux-gnu.so`; class home `CollectiveOp.cpython-310-…so`; rank-id serializer `IRWriter.cpython-310-…so`. VAs are cp310 image offsets of the `pw` bodies; KB.py lines are DWARF (`addr2line`). Op-class names, kwarg sets, and KB.py line numbers are build-invariant; the VAs are not.
