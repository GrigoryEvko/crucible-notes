# Hierarchical and RDH Composition

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The composer source TUs are `/opt/workspace/KaenaRuntime/enc/enc.cc` (group decomposition), `enc/enc_primitive.cc` (hier dispatch + per-stage page tables), `enc/inter_rdh.cc`/`inter_rdh.h` (inter-node recursive-halving scheduler), and `enc/rdh.cc`/`rdh.h` (intra-node RDH plan compiler). `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (decompile- and DWARF-anchored)** — the hier dispatch by op-type and slice count is read line-by-line from `enc_hier_primitive::compose_operation @0x1a8d20`; the recursive-halving peer math from `get_indices_rs_internal @0x1b4a50` and `get_indices_ag_internal @0x1b8fc0`; the RDH plan-compiler structs (`rdh_transfer` 120 B, `rdh_chunk` 72 B) verbatim from `structures.json`; the group split from `init_hierarchical_groups @0x12c820`. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

An all-reduce over a flat ring costs `2·(N−1)` link traversals; over a two-level topology where intra-node links (NeuronLink RMTV/D2D/PCIe) are an order of magnitude faster than inter-node links (the network proxy), that flat cost is wrong twice over — it ignores the bandwidth cliff at the node boundary and it serializes a reduction that decomposes cleanly. This page owns the two libnrt mechanisms that exploit the hierarchy. The first is **hierarchical composition** (`enc_hier_primitive`, `compose_operation @0x1a8d20`): a *fixed* two-level decomposition that rewrites one global collective into a sequence of intra-node and inter-node sub-collectives, handing each sub-stage to a mesh or ring/RDH sub-primitive and threading the reduce-scattered partials through a shared on-device scratch buffer (`comm.hier.devmem_res`, 128 MiB on TRN2/TRN3 else 32 MiB). The second is **RDH** — the recursive-doubling/halving scheduler family that *is* one of those stages: `inter_rdh_scheduler` (`enc/inter_rdh.cc`) for the inter-node tier and `rdh_scheduler` (`enc/rdh.cc`) for the intra-node tier, both host-side **plan compilers** that emit a static `vector<vector<rdh_transfer>>` of DMA transfers per device (plus `rdh_chunk` receive records), which the `enc-primitives` step recorders later lower into the device op stream.

The reference frame is two textbook results a reimplementer already owns. The first is the **hierarchical-collective identity**: an all-reduce equals a reduce-scatter followed by an all-gather, and on a two-level topology this composes as `intra-reduce-scatter → inter-all-reduce → intra-all-gather` — each rank first folds its node's contributions into one partial per node-peer, the per-node partials are all-reduced across the (now one-rank-per-node) inter group, and the result is scattered back inside each node. The second is **recursive halving/doubling**: for `N = 2^k` ranks, a reduce-scatter completes in `k` rounds where on round `b` rank `r` exchanges with the partner that differs in exactly one bit of its index (`r ⊕ 2^(k−1−b)`), each round halving the live data per rank; the all-gather is the same partner schedule run in reverse (recursive doubling). RDH is exactly this — `RDH = Reduce-Distribute-Half = ReduceScatter + AllGather = AllReduce` — but every "exchange" is a tagged `rdh_transfer` registered into a per-device schedule rather than an executed send/recv, and the reduce-scatter half is *tiled* (`schedule_rs_tiled @0x1b4f60`) so a large message is coalesced into chunks that pipeline through the bit-rounds.

This page documents what `comm-context.md` deferred (the `init_hierarchical_groups` rank→(intra,inter) decomposition and the `devmem_res` scratch reservation) and extends what `enc-primitives.md` owns (it documents the `rdh_transfer` **emit** — the lowering of one transfer into reduce/copy op-params; this page documents the **scheduling math that produces the transfer vector**). Concretely: (1) the `enc_alg_hier` two-level state and the fixed 2-/3-stage decomposition per op-type; (2) the RDH plan-compiler structs (`rdh_transfer`, `rdh_chunk`, `rdh_scheduler`, `inter_rdh_scheduler`) field-by-DWARF-field; (3) the recursive-halving peer-index generator as C pseudocode; and (4) the per-device-count intra-RDH dispatch (1/2/4/16-device, vNeuronCore 1/2) and the 2-step pod RDH.

For reimplementation, the contract is:

- **Hierarchical decomposition is fixed, not searched.** `enc_hier_primitive::compose_operation @0x1a8d20` routes by `op_type` to a hard-wired stage list — allreduce → 3 stages, reduce-scatter → 2 stages, all-gather → 2 stages (inter **first**) — selects one primitive per stage slot by a fixed priority cascade (`__select_algorithms @0x14c620`), builds a per-stage page table that addresses the shared scratch buffer, and instantiates a fresh mesh or metaring sub-primitive per stage. There is no cost model; reproduce the cascade, not a tuner.
- **The RDH scheduler is a plan compiler, not an executor.** `inter_rdh_scheduler` and `rdh_scheduler` run once at NEFF-load time and produce a static schedule — `vector<vector<rdh_transfer>>` keyed by `(device, step)` plus `rdh_chunk` receive registries — with no DMA issued. A reimplementer reproduces the index math and the `rdh_transfer`/`rdh_chunk` layouts; the lowering to DMA is `enc-primitives.md`.
- **The recursive-halving peer index is bit-exact and must match its mirror.** Reduce-scatter round `b` for rank `r` touches partitions `base + i·2^(b+1)` for `i ∈ [0, 2^(k−1−b))`, where `base = ((is_send << b) ⊕ (r & 2^b)) + (r & (2^b − 1))` (`get_indices_rs_internal @0x1b4a50`); the all-gather partner is `r ⊕ 2^(k−1−axis)` (`get_indices_ag_internal @0x1b8fc0`). The send-index set of one rank must equal the recv-index set of its partner; a reimplementer who flips the `is_send` polarity or the axis direction desynchronizes every reduction.

| | |
|---|---|
| **Hier composer** | `enc_hier_primitive` (248 B) — `compose_operation @0x1a8d20`; 5 alg slots @+224..+240 |
| **Hier state** | `enc_alg_hier` (386 368 B) — `intra@+0` / `inter@+136736` / `pipeline@+273472` / `devmem_res@+386344` |
| **Group split** | `init_hierarchical_groups @0x12c820`; pod variant `build_hierarchical_participants_for_pod @0x7e7a50` |
| **Stage selector** | `__select_algorithms @0x14c620` — fixed priority per slot |
| **Cross-stage scratch** | `comm.hier.devmem_res` — `0x8000000` (128 MiB, TRN2/3) \| `0x2000000` (32 MiB); checkout `__devmem_res_checkout @0x14cb50` |
| **Inter-node RDH** | `inter_rdh_scheduler` (512 B) — `schedule @0x1a91e0`; `schedule_rs_tiled @0x1b4f60`; `schedule_ag @0x1b9860` |
| **Intra-node RDH** | `rdh_scheduler` (160 B) — per-device-count `schedule_{1,2,4,16}_dev_*`; ctor `@0x1acb40` |
| **Transfer record** | `rdh_transfer` (120 B) — `src_devs@8`/`src_addrs@32`, `dst_devs@56`/`dst_addrs@80`, `size@104`, `dst_idx@112` |
| **Receive record** | `rdh_chunk` (72 B) — `idx@0`, `reduced_dev@8`, `addr@32`, `tag@40` |
| **RS peer math** | `get_indices_rs_internal @0x1b4a50` (recursive halving); thunks `get_{send,recv}_indices_rs @0x1b4b80/@0x1b4ba0` |
| **AG peer math** | `get_indices_ag_internal @0x1b8fc0` (recursive doubling); thunks `get_{send,recv}_indices_ag @0x1b9820/@0x1b9840` |
| **2-step pod RDH** | `__compose_{allg,allr,redsct}_with_two_step_pod_mesh_trn2_proxy @0x1c5600/@0x1c76c0/@0x1cab70` |

> **CORRECTION (HIER-1) — `enc_pattern_t` is `{RING=0, MESH=1, INVALID=2}`, not `{RING=1, MESH=2}`.** An early collectives sweep (SCAN-05) recorded the device pattern enum with RING at 1. `enums.json` for this image resolves it definitively: `ENC_PATTERN_RING=0`, `ENC_PATTERN_MESH=1`, `ENC_PATTERN_INVALID=2`. This matters because the hier stages emit `cc_op_entry.algo_type` from this enum; a reimplementer using the off-by-one values tags every RDH/mesh inter stage with the wrong device pattern and the firmware mis-dispatches. Confidence **HIGH** (`enums.json`, cross-checked against [Algorithm Taxonomy](algorithm-taxonomy.md)).

---

## 1. Hierarchical Composition (`enc_hier_primitive`)

### Purpose

`enc_hier_primitive` is the orchestrator for `ENC_ALG_HIER (1)`: it owns no leaf code (see [enc Primitives §1 QUIRK](enc-primitives.md)) but decomposes one global collective into a fixed list of intra-node and inter-node sub-collectives, builds a page table per sub-stage, and instantiates a fresh `enc_mesh_primitive` or `enc_metaring_primitive` per stage, calling *its* `compose_operation`. The decomposition is set at NEFF-load time by `init_hierarchical_groups @0x12c820` (which splits the replica group into an intra group of `local_rank_n` ranks and an inter group of `node_n` one-rank-per-node leaders) and consumed per op by `compose_operation @0x1a8d20`. The shared on-device scratch (`devmem_res`) is the cross-stage handoff: the reduce-scattered partials from stage 1 live there until stage 2 reads them.

### The two-level state — `enc_alg_hier` (386 368 B)

The per-comm hier state is embedded at `enc_comm->hier`. It carries three parallel sub-trees; the only structural difference between intra and inter is that **intra carries a kangaring metaring, inter carries an RDH metaring**.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `intra` | +0 | sub (136 736 B) | intra-node level: `{nccl_comm_node@0, ci@8, ring@80, kangaring@37624, mesh@75168}` | HIGH |
| `inter` | +136736 | sub (136 736 B) | inter-node level: `{nccl_comm_node@0, ci@8, ring@80, rdh@37624, mesh@75168}` | HIGH |
| `pipeline` | +273472 | sub (112 872 B) | `pipeline.stage[3]`, each `{nccl_comm_node, ci@8, ring@80}` — ring-only, used by the pipelined variant | HIGH |
| `devmem_res` | +386344 | `void*` | the shared HIER scratch reservation (cross-stage handoff) | HIGH |
| `comm` | +386352 | `enc_comm*` | owning communicator | HIGH |
| `drv_alg` | +386360 | `encd_alg_hier*` | device-resident mirror (`encd_alg_init_hier @0x24f250`) | HIGH |

Each level's `ci` is an `enc_comm_info` (72 B) — the per-level topology descriptor. The fields the decomposition writes and the selector reads:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `rank` / `rank_n` | +4 / +8 | `int` | this rank's position / size **within this level's group** | HIGH |
| `local_rank_n` | +12 | `int` | intra-node ranks (`= participants[INTRA].size()`) | HIGH |
| `local_rack_rank_n` | +16 | `int` | ranks within a rack (rack tier) | MED |
| `node` / `node_n` | +20 / +24 | `int` | this node index / node count (`= participants[INTER].size()`) | HIGH |
| `enc_topo_mode` | +28 | `enc_topology_mode_t` | `{NULL=0, 4_DEVS_IN_ROW=1, 4_DEVS_IN_COLUMN=2}` (intra-pod mesh shape) | HIGH |
| `enable_pod` / `use_net` | +32 / +33 | `bool` | pod hierarchy active / inter level uses network transport | HIGH |
| `pod` / `pod_n` / `pod_node` / `pod_node_n` | +36 / +40 / +44 / +48 | `int` | pod identity within the pod tier | HIGH |
| `peers` | +56 | `enc_peer_info*` | per-rank peer table (stride 24 B; `peer[i].neuron_dev@0`) | HIGH |

The composer object `enc_hier_primitive` (248 B) extends `enc_alg_primitive` (168 B) with five resolved-algorithm slots — one per stage role — that `__select_algorithms` fills:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `hier` | +168 | `enc_alg_hier*` | the per-comm state (`= &comm->hier`) | HIGH |
| `ranks_order` | +176 | `const vector<int>*` | this rank's traversal order (`ctx->alg_info.ranks_order[comm->id]`, `id ≤ 11`) | HIGH |
| `alg_intra_allg` | +224 | `enc_alg_type` | selected primitive for the intra all-gather stage | HIGH |
| `alg_intra_redsct` | +228 | `enc_alg_type` | selected primitive for the intra reduce-scatter stage | HIGH |
| `alg_inter_allr` | +232 | `enc_alg_type` | selected primitive for the inter all-reduce stage | HIGH |
| `alg_inter_allg` | +236 | `enc_alg_type` | selected primitive for the inter all-gather stage | HIGH |
| `alg_inter_redsct` | +240 | `enc_alg_type` | selected primitive for the inter reduce-scatter stage | HIGH |

### The group decomposition (deferred from `comm-context.md`)

`init_hierarchical_groups @0x12c820` is the setup-time rank→(intra,inter) split that `comm-context.md §6` deferred here. It runs once per replica group from `enc_init_replica_groups @0x138700`, gated by `enc_cc_algorithm_allowed(.., H_COMM_INTER_ID, ENC_ALG_HIER)`. It partitions the global `participants` set into two groups, sets each up as its own communicator (`set_my_group` with flag 0 = intra, flag 1 = inter), runs `nccl_init_info` once per level to populate `hier.{intra,inter}.{nccl_comm_node, ci, ...}`, and — for pod topologies — maps every rank to an `(intra_node_rank, inter_node_rank)` pair.

```c
// init_hierarchical_groups @0x12c820  (enc.cc; called from enc_init_replica_groups @0x138700)
function init_hierarchical_groups(ctx, nec_dev, group_id, comm, rg, pipeline):  // 0x12c820
    parts = {};  // enc_replica_group_info(&)[2] : [INTRA, INTER]
    if rg.enable_pod:
        // build_hierarchical_participants_for_pod @0x7e7a50
        // each rank -> (intra_node_rank < local_rank_n, inter_node_rank < node_n)
        for r in rg.participants:
            (in_rank, out_rank) = pod_rank_map(r);            // log @0x7e7cc8
            assert(in_rank  < local_rank_n);                  // enc.cc invariant
            assert(out_rank < node_n);
            parts[INTRA].add_if(in_rank);  parts[INTER].add_if(out_rank);
        assert(parts[INTRA].size() == local_rank_n);          // "...== local_rank_n"
    else:
        build_hierarchical_participants(rg, &parts);          // generic node split

    set_my_group(comm, parts[H_COMM_INTRA_ID], /*flag=*/0);   // INTRA group
    assert(node_n > 1);                                       // "node_n > 1"
    set_my_group(comm, parts[H_COMM_INTER_ID], /*flag=*/1);   // INTER group

    nccl_init_info(parts[INTRA]) -> hier.intra.{nccl_comm_node, ci, ...};
    nccl_init_info(parts[INTER]) -> hier.inter.{...};
    if pipeline:                                              // ring-only pipelined variant
        for s in 0..2: nccl_init_info(stage[s]) -> hier.pipeline.stage[s];

    ctx->alg_info.ranks_order[group_id] = copy(rg.ranks_order);
    // device mirror: encd_alg_init_hier @0x24f250 builds encd_alg_metaring per level
    //   intra.ring=RING, intra.kangaring=KANGARING, inter.ring=RING, inter.rdh=RDH
```

The cross-stage scratch reservation `comm-context.md §6 NOTE` deferred is `enc_setup_global_comm_internal @0x10b050`: it calls `encd_global_devmem_reserve` with size `0x8000000` (128 MiB) on TRN2/TRN3 else `0x2000000` (32 MiB), writing the handle to `comm.hier.devmem_res`. The size comes from `get_hier_tmp_chunk_size` (which asserts `nrt_get_instance_info() == SUCCESS` to read the instance family). `__devmem_res_checkout @0x14cb50` checks this buffer out once per allreduce/reduce-scatter compose; all stages of one op share it.

### Algorithm — the hier dispatch and 3-stage all-reduce

`compose_operation @0x1a8d20` is the top dispatcher: it selects a primitive per stage, asserts the level shape, then routes by `op_type`. The all-reduce path is the canonical three-stage decomposition.

```c
// enc_hier_primitive::compose_operation @0x1a8d20  (enc_primitive.cc)
function compose_operation(mrc):                                  // 0x1a8d20
    __select_algorithms(this);                                    // 0x14c620 — fill the 5 slots
    assert(intra.ci.rank_n > 1);
    slice_n = base.hier_cc_pipeline_slice_n;                      // base+100; >1 => pipelined

    switch op_type:
      case ENC_ALLREDUCE (1):
        if slice_n <= 1:
            __devmem_res_checkout(this);                          // 0x14cb50 — share scratch
            return __compose_allreduce(mrc);                      // 0x1a34c0  (3 stages)
        else:
            return __compose_pipeline_allreduce(mrc, slice_n);    // 0x17b3f0
      case ENC_REDUCE_SCATTER (4):
        __devmem_res_checkout(this);
        return slice_n<=1 ? __compose_redsct(mrc)                 // 0x1a6d60  (2 stages)
                          : __compose_pipeline_redsct(mrc, slice_n);
      case ENC_ALLGATHER (0):
        // NO checkout: all-gather produces, does not pre-reduce into temp
        return slice_n<=1 ? __compose_allgather(mrc)              // 0x1a0f10  (2 stages, inter FIRST)
                          : __compose_pipeline_allgather(mrc, slice_n);
      default:
        return NRT_INVALID;   // "not supported operation (op_type %d)"

// enc_hier_primitive::__compose_allreduce @0x1a34c0  — the 3-stage all-reduce
function __compose_allreduce(mrc):                                // 0x1a34c0
    require(alg_intra_allg, alg_intra_redsct, alg_inter_allr != INVALID);  // else "Invalid allr alg types"

    // STAGE 1 — INTRA reduce-scatter: fold this node's contributions into per-peer partials.
    __build_allreduce_pgt_intra_rdsc(pgt, da, alg_intra_redsct, use_tmp, 0, 1);   // 0x1558f0
    compose_stage(intra, alg_intra_redsct);   // mesh -> enc_mesh_primitive(intra.mesh)::compose
                                               // ring -> enc_metaring_primitive(intra.ring|kangaring)::compose
                                               //   assert metaring in {RING, KANGARING}

    // STAGE 2 — INTER all-reduce: all-reduce the per-node partials across the inter group,
    //           reading the partials OUT of the shared scratch (use_hierarchical_tmp_buf).
    __build_allreduce_pgt_inter_allr(pgt, da, use_tmp, 0, 1);     // 0x14ff80
    //   assert(!use_tmp || data_array.size() == 1)               // enc_primitive.cc:0x3762
    //   __page_offset_with_dma_unalignment_and_temp_buff @0x14cc90 addresses devmem_res
    compose_stage(inter, alg_inter_allr);      // mesh, OR metaring in {RING, INTER_RDH, SINGLE_CYCLE_RING}

    // STAGE 3 — INTRA all-gather: scatter the reduced result back inside the node.
    __build_allreduce_pgt_intra_allg(pgt, da, alg_intra_allg, 0, 1);  // 0x155260
    compose_stage(intra, alg_intra_allg);      // metaring assert in {RING, KANGARING}
```

The selector `__select_algorithms @0x14c620` fills each slot with the **first eligible** primitive in a fixed priority order, queried per slot by the matching `enc_can_post_*` feasibility check against a per-stage slice size:

```c
// enc_hier_primitive::__select_algorithms @0x14c620  (priority cascade per slot)
function __select_algorithms():                                  // 0x14c620
    intra_slice = ceil(per_rank_bytes / intra.ci.rank_n);
    inter_slice = intra_slice * inter.ci.node_n;                 // all-gather feed
    // priority cascades (first that passes enc_can_post_* wins):
    alg_intra_allg   = first_ok({INTRA_RDH, MESH, KANGARING}, intra_slice);
    alg_intra_redsct = first_ok({INTRA_RDH, MESH, KANGARING}, intra_slice);
    alg_inter_allr   = first_ok({MESH, INTER_RDH, SINGLE_CYCLE_RING}, inter_slice);
    alg_inter_allg   = first_ok({MESH, INTER_RDH}, inter_slice);
    alg_inter_redsct = first_ok({MESH, INTER_RDH}, inter_slice);
    nlog("Hier algorithm selections - alg_intra_allg %d alg_intra_redsct %d "
         "alg_inter_allr %d alg_inter_allg %d alg_inter_redsct %d");   // @0x7ed4b0
    return enc_validate_operation(intra.ci, alg_intra_allg, op_type, data_array);
```

> **QUIRK —** all-gather decomposes with the **inter** stage first, the opposite of all-reduce. `__compose_allgather @0x1a0f10` runs `inter all-gather → intra all-gather` (stage 1 is `__build_allgather_pgt_inter_allg @0x14ee00`), whereas all-reduce sandwiches the inter stage between two intra stages. The reason is data direction: an all-reduce must *reduce-scatter inward* before the cross-node reduce and *all-gather outward* after, but an all-gather has nothing to reduce — it only fans the already-distinct per-node data out, so it gathers across nodes first (the expensive link, done once on the smallest data) then within each node. A reimplementer who runs intra-first for all-gather sends `local_rank_n×` the necessary bytes across the network.

> **GOTCHA —** stage *N*'s output pages are stage *N+1*'s input pages, but only because they share `devmem_res`, not because the page table is reused. Each `__build_*_pgt_*` builder recomputes offsets into the *same* checked-out scratch via `__page_offset_with_dma_unalignment_and_temp_buff @0x14cc90`. The inter-stage builder asserts `!use_hierarchical_tmp_buf || data_array.size() == 1` (`enc_primitive.cc:0x3762`) — the temp-buffer path is single-tensor only. A reimplementer who lets the inter stage read from the input tensor instead of the scratch reduces stale (un-reduce-scattered) data.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_hier_primitive::compose_operation` | `0x1a8d20` | top dispatch: select + route by op_type / slice_n | HIGH |
| `enc_hier_primitive::__select_algorithms` | `0x14c620` | fill the 5 alg slots by priority cascade | HIGH |
| `enc_hier_primitive::__compose_allreduce` | `0x1a34c0` | 3-stage: intra-RS → inter-AR → intra-AG | HIGH |
| `enc_hier_primitive::__compose_redsct` | `0x1a6d60` | 2-stage: intra-RS → inter-RS | HIGH |
| `enc_hier_primitive::__compose_allgather` | `0x1a0f10` | 2-stage: inter-AG → intra-AG (inter first) | HIGH |
| `enc_hier_primitive::__compose_pipeline_allreduce` | `0x17b3f0` | slice-interleaved 3-stage AR over `slice_n` chunks | HIGH |
| `enc_hier_primitive::__devmem_res_checkout` | `0x14cb50` | check out shared scratch (`"allocated dev mem … for hier intermediate buffer"`) | HIGH |
| `init_hierarchical_groups` | `0x12c820` | setup: split rg into intra/inter (+pipeline) groups | HIGH |
| `build_hierarchical_participants_for_pod` | `0x7e7a50` | pod variant: rank → (intra_node_rank, inter_node_rank) | HIGH |
| `should_use_hierarchical_cc_pipeline` | `0x109380` | gate the pipelined (ring-only, single-stream) variant | HIGH |
| `encd_alg_init_hier` | `0x24f250` | device-resident per-level `encd_alg_metaring` init | HIGH |
| `enc_free_alg_hier` | `0x10a2f0` | tear down all levels + `pipeline.stage[]` (stride 37624) | HIGH |

---

## 2. The Inter-Node RDH Scheduler (`inter_rdh_scheduler`)

### Purpose

`inter_rdh_scheduler` (`enc/inter_rdh.cc`, 512 B) is the recursive-doubling/halving plan compiler for the **inter-node** tier — the `INTER_RDH` metaring that `__compose_allreduce`/`__compose_redsct` can select for `alg_inter_allr`/`alg_inter_redsct`. It is constructed by `enc_metaring_primitive::__compose_inter_rdh` (other cell), runs `schedule @0x1a91e0` to build a tiled send/recv/reduce FIFO plan, and is drained by `actuate @0x1bb0c0` into the `enc-primitives` RDH leaves. It operates under hard constraints asserted in its constructor (`@0x1b0580`): `rank_n` is a power of two and `≥ 8`; `rank_n == node_n && local_rank_n == 1` (pure inter-node, one rank per node); `op ∈ {ALLGATHER, REDUCE_SCATTER, ALLREDUCE}`. `num_bits = log2(rank_n)` is `tzcnt(rank_n)` stored at `+128`.

### Layout — `inter_rdh_scheduler` (512 B)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `mem_segs` | +0 | `memory_segment_manager` | segment + partition-size manager (`init` builds `bm` partitions) | HIGH |
| `coal_n` | +56 | `int` | tile coalescing factor | HIGH |
| `rank_n` / `rank` / `dev` | +60 / +64 / +68 | `int` | inter-group size / this rank / device id | HIGH |
| `dtype_size` | +96 | `size_t` | element byte size | HIGH |
| `type` | +104 | `enc_op_type` | the op being scheduled (∈ {AG, RS, AR}) | HIGH |
| `channel_buffer_size` | +112 | `size_t` | per-channel buffer | HIGH |
| `page_table` | +120 | `enc_pagetable*` | the op's page table | HIGH |
| `num_bits` | +128 | `int` | `= log2(rank_n)` (`tzcnt`) — the recursive-halving round count | HIGH |
| `debug_print` | +132 | `bool` | gates the FIFO-stringify debug path | HIGH |
| `recv_fifo` / `send_fifo` / `red_fifo` | +136 / +216 / +296 | `queue<inter_rdh_{recv,send,reduction}>` | the scheduled op records (drained by `actuate`) | HIGH |
| `all_tiles` | +376 | `unordered_map<string, inter_rdh_tile>` | tag → `{handle, size}` tile registry | HIGH |
| `bm` | +432 | `inter_rdh_buffer_manager` | channel buffer-partition manager | HIGH |
| `ag_sends_cache` / `ag_recvs_cache` | +456 / +480 | `vec<vec<vec<int>>>` | lazily-built `[bit][rank]` AG peer-index cache | HIGH |
| `get_indices_ag_internal_cached` | +504 | `bool` | one-shot flag gating the AG cache build | HIGH |

### Algorithm — the recursive-halving peer index

The core is `get_indices_rs_internal @0x1b4a50`: for a rank at recursive-halving round `bit`, it generates the set of partition indices that rank touches (sends or receives, by the `is_send` polarity). This is the bit-exact formula a reimplementer must reproduce — and it is its own inverse across the send/recv pair, which is what makes the exchange consistent.

```c
// inter_rdh_scheduler::get_indices_rs_internal @0x1b4a50  (NRVO: 'this' is the return vector<int>)
// reduce-scatter: which partition indices rank 'r' touches at recursive-halving round 'bit'
function get_indices_rs_internal(r, bit, is_send) -> vector<int>:        // 0x1b4a50
    assert(bit >= 0 && bit < num_bits && r >= 0 && r < rank_n);         // inter_rdh.cc:0x97
    span = 1 << (num_bits - (bit + 1));        // # of indices this round, halves each round
    // base partition: flip bit 'bit' of r toward the partner on a send; keep the low bits
    base = ((is_send << bit) ^ (r & (1 << bit))) + (r & ((1 << bit) - 1));
    out = {};
    for i in 0 .. span:
        out.push_back(base + (i << (bit + 1)));   // stride 2^(bit+1)
    return out;

// thin polarity thunks:
function get_send_indices_rs(r, bit): return get_indices_rs_internal(r, bit, /*is_send=*/1);  // 0x1b4b80
function get_recv_indices_rs(r, bit): return get_indices_rs_internal(r, bit, /*is_send=*/0);  // 0x1b4ba0
```

The all-gather side is recursive doubling: the partner at axis `axis` is `r ⊕ 2^(num_bits−1−axis)`, and the full `[bit][rank]` index sets are memoized once into `ag_sends_cache`/`ag_recvs_cache`.

```c
// inter_rdh_scheduler::get_indices_ag_internal @0x1b8fc0  (lazy, gated by +504)
function get_indices_ag_internal(r, bit, is_send) -> vector<int>:        // 0x1b8fc0
    assert(bit >= 0 && bit < num_bits && r >= 0 && r < rank_n);         // inter_rdh.cc:0xBB
    if !get_indices_ag_internal_cached:                                  // +504 — build once
        for b in 0 .. num_bits:
          for q in 0 .. rank_n:
            peer = get_peer_ag(q, /*axis=*/b);   // = q ^ (1 << (num_bits - 1 - b)); assert axis in range
            ag_sends_cache[b][q] = sends-to-peer index set;
            ag_recvs_cache[b][q] = recvs-from-peer index set;
        get_indices_ag_internal_cached = true;
    return (is_send ? ag_sends_cache : ag_recvs_cache)[bit][r];

function get_send_indices_ag(r, bit): return get_indices_ag_internal(r, bit, 1);  // 0x1b9820 (tail-thunk)
function get_recv_indices_ag(r, bit): return get_indices_ag_internal(r, bit, 0);  // 0x1b9840 (tail-thunk)
```

The top dispatcher `schedule @0x1a91e0` routes the op to its half: all-gather → `schedule_ag @0x1b9860` (per round `bit = 0..num_bits−1`, `peer = rank ⊕ 2^(num_bits−1−bit)`, push `inter_rdh_send`/`inter_rdh_recv`/`inter_rdh_reduction` records), reduce-scatter / all-reduce → `schedule_rs_tiled @0x1b4f60` (the tiled reduce phase). The drained records carry handles registered in `all_tiles` by tag.

> **QUIRK —** the reduce-scatter half is **tiled** but the all-gather half is **cached**, and they are not symmetric data structures. `schedule_rs_tiled @0x1b4f60` iterates `coal_n` coalesced tiles × `num_bits` rounds × ranks, building tags from a grammar (`coal_<i>` / `chunk_<i>` / `input_tile_<i>` / `_red_tile_<dim>_<i>` / `_round_<b>`) and registering tile handles in the `all_tiles` hashmap; `schedule_ag @0x1b9860` instead pre-builds the entire `[bit][rank]` peer-index cache and walks it. A reimplementer cannot share one index pipeline across the two halves: RS computes indices per-tile on the fly (memory pressure is the tile, not the index set), AG memoizes the whole `num_bits × rank_n` table because each round's peer set is reused across tiles.

> **GOTCHA —** the `base` formula's `is_send` term is `(is_send << bit)`, not a constant offset. On a send at round `bit`, rank `r`'s partition base flips bit `bit` of its index *toward* the partner (`r ⊕ 2^bit`); on a receive it keeps its own bit. Because XOR is its own inverse, `get_send_indices_rs(r, b)` for rank `r` equals `get_recv_indices_rs(r ⊕ 2^b, b)` for its partner — the two FIFOs line up. A reimplementer who hard-codes the partner as `r + span` or drops the `(r & ((1<<bit)−1))` low-bit carry produces index sets that overlap or gap, and the reduction double-counts or skips partitions.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `inter_rdh_scheduler::schedule` | `0x1a91e0` | top dispatch: AG → `schedule_ag`, RS/AR → `schedule_rs_tiled` | HIGH |
| `inter_rdh_scheduler::schedule_rs_tiled` | `0x1b4f60` | tiled reduce-scatter (+ AR reduce phase) plan builder (405 BBs) | HIGH |
| `inter_rdh_scheduler::schedule_ag` | `0x1b9860` | all-gather plan builder (XOR-peer recursive doubling) | HIGH |
| `inter_rdh_scheduler::get_indices_rs_internal` | `0x1b4a50` | recursive-halving RS partition-index generator | HIGH |
| `inter_rdh_scheduler::get_indices_ag_internal` | `0x1b8fc0` | recursive-doubling AG index cache builder | HIGH |
| `inter_rdh_scheduler::actuate` | `0x1bb0c0` | drain red/send/recv FIFOs → `enc_primitive::rdh_{reduce,copy,send,recv}` | HIGH |
| `inter_rdh_scheduler::add_tile` | `0x1bd740` | register `{handle,size}` in `all_tiles` (asserts no dup tag) | HIGH |
| `inter_rdh_scheduler::get_tag` / `get_tag_prefix` | `0x1bc970` / `0x1bc7b0` | tile-tag grammar (`coal_`/`chunk_`) | HIGH |
| `inter_rdh_scheduler` (ctor) | `0x1b0580` | invariants: `rank_n` pow2 & `≥8`, `rank_n==node_n`, `local_rank_n==1` | HIGH |

---

## 3. The Intra-Node RDH Plan Compiler (`rdh_scheduler`)

### Purpose

`rdh_scheduler` (`enc/rdh.cc`/`rdh.h`, 160 B) is the **intra-node** RDH plan compiler — the `INTRA_RDH` mesh primitive that `__select_algorithms` can pick for the intra reduce-scatter / all-gather slots. It is constructed by `enc_mesh_primitive::__compose_{rh_redsct, rd_allgather, rdh_allreduce}[_vnc1]` and produces, per device, a static `vector<vector<rdh_transfer>>` schedule (indexed by step) and a `rdh_chunk` receive registry. Unlike the inter scheduler's power-of-two/`≥8` constraint, the intra scheduler is keyed to the **physical device count of one node**: its constructor (`@0x1acb40`) asserts `node_n == 1`, `vnc ∈ {1, 2}`, and `rank_n ∈ {4, 8, 16, 64}` (the per-device-count RDH configs), then dispatches to a per-count schedule builder. The peer hops are physical-topology functions — RMTV (remote-via, the NeuronLink mesh), D2D (die-to-die), PCIe, and LOCAL — not abstract ring neighbors.

### Layout — `rdh_scheduler` (160 B) and the per-device entry

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `dummy_chunk` | +0 | `rdh_chunk` (72 B) | scratch chunk; index helpers write the sret `vector<int>` here | HIGH |
| `devices` | +72 | `vector<rdh_device>` | per-device schedule / received / all_chunks | HIGH |
| `local_dev` / `rank_n` | +96 / +100 | `int` | local device base / device count (∈ {4,8,16,64}) | HIGH |
| `ci` | +104 | `const enc_comm_info*` | the peer table | HIGH |
| `neighbor_params_array` | +112 | `neighbor_params*` | per-rank input/output/rdh_buf DMA addresses | HIGH |
| `element_n` / `dtype_size` | +120 / +128 | `size_t` | element count / element byte size | HIGH |
| `type` | +136 | `enc_op_type` | the op (∈ {AG, RS, AR}) | HIGH |
| `pgt` | +144 | `enc_pagetable*` | page table (chunk size/offset source) | HIGH |
| `chunk_n` | +152 | `int` | chunks per rank | HIGH |
| `use_2dev_proxy` | +156 | `bool` | 2-device proxy-RDH special case | HIGH |
| `output_shared_virtual_scratchpad` | +157 | `bool` | vnc1 shared-scratchpad layout | HIGH |
| `is_trn3` | +158 | `bool` | selects `rd_1_dev_step_0_trn3` (single, non-halved, transfer) | HIGH |

Each `rdh_device` (152 B) is one node-local NeuronCore's slice of the plan:

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `dev` | +0 | `int` | the `neuron_dev` id | HIGH |
| `schedule` | +8 | `vector<vector<rdh_transfer>>` | **the emitted plan**, indexed by step | HIGH |
| `received` | +32 | `vector<vector<rdh_chunk>>` | chunks delivered per step | HIGH |
| `all_chunks` | +56 | `map<int, vector<rdh_chunk>>` | chunk registry keyed by chunk idx | HIGH |
| `bm` | +104 | `buffer_manager` (24 B) | bump allocator `{capacity, base, offset}` | HIGH |
| `input` / `output` / `buf` | +128 / +136 / +144 | `dma_addr_t` | the device's input / output / scratch DMA bases | HIGH |

### The two plan-compiler records — `rdh_transfer` (120 B) and `rdh_chunk` (72 B)

These are the units the scheduler emits. `rdh_transfer` is the multi-source/multi-destination DMA descriptor pushed into `rdh_device.schedule[step]`; `rdh_chunk` is the receive/book-keeping record registered into `rdh_device.all_chunks`. The lowering of one `rdh_transfer` into `enc_reduce_op_params`/`enc_copy_op_params` is owned by [enc Primitives §2](enc-primitives.md); the layout below is what the scheduler *fills*.

| `rdh_transfer` field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `idx` | +0 | `int` | transfer index within the step | HIGH |
| `src_devs` | +8 | `vector<int>` | source `neuron_dev` ids (1:1 with `src_addrs`) | HIGH |
| `src_addrs` | +32 | `vector<u64>` | source DMA addresses | HIGH |
| `dst_devs` | +56 | `vector<int>` | destination dev ids (1 or 2: an RMTV peer and a D2D peer) | HIGH |
| `dst_addrs` | +80 | `vector<u64>` | destination DMA addresses | HIGH |
| `size` | +104 | `size_t` | transfer byte length | HIGH |
| `dst_idx` | +112 | `int` | destination ordinal (`−1` = leaf; `2` = half-chunk marker) | HIGH |

| `rdh_chunk` field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `idx` | +0 | `int` | chunk index | HIGH |
| `reduced_dev` | +8 | `vector<int>` | the set of device ids already reduced **into** this chunk | HIGH |
| `addr` | +32 | `u64` | the chunk's DMA address | HIGH |
| `tag` | +40 | `std::string` | role tag (`"v_output"`, `"d_output"`, `"pcie_chunk_0"`, `"step0_chunk0"`, `"local_peer"`, …) | HIGH |

> **QUIRK —** `dst_idx == 2` is not a destination ordinal — it is a half-chunk marker. The 2-/1-device RD builders (`schedule_2_dev_rd_4s_step_3 @0x1d79b0`, `rd_1_dev_step_1 @0x1d9e10`) carry a `half_chunk_tracker` and assert `std::get<0>(half_chunk_tracker[idx]) == 2` when a chunk has been split and both halves reduced. A reimplementer reading `dst_idx` as a plain index on those transfers mis-routes the second half. The `reduced_dev` vector on `rdh_chunk` is how the scheduler proves correctness statically — `schedule_2_dev_reduce_scatter` asserts `chunk.reduced_dev.size() == rank_n` (every device contributed) before the chunk is final.

### The per-device-count dispatch

The intra scheduler has no single algorithm — it has a family selected by `(rank_n, vnc, op)`. The dispatcher resizes the per-device `schedule`/`received` vectors then calls the count-specific builder.

| Device count | All-gather entry | Reduce-scatter / all-reduce entry | Topology / notes | Confidence |
|---|---|---|---|---|
| 1 / node | `schedule_1_dev_all_gather @0x1df630` → `rd_1_dev_step_{0,1,2}` | `schedule_1_dev_reduce_scatter @0x1e90c0` / `schedule_1_dev_all_reduce @0x1e9ad0` | RMTV+D2D chunk-halving; `_trn3` does single non-halved transfer | HIGH |
| 2 / node | `schedule_2_dev_all_gather @0x1e0ac0` → `rd_4s_step_0..4` + `pci_peer_rd_step` | `schedule_2_dev_reduce_scatter @0x1e8930` → `schedule_2_dev_rh_step` ×4 | 4-step RD pipeline; PCIe-proxy `pcie_chunk_0/1`; `use_2dev_proxy` | HIGH |
| 4 / node | `schedule_4_dev_all_gather @0x1f3ca0` | `schedule_4_dev_reduce_scatter @0x1eac40` → `rh_4_dev_get_indices` | ring-hierarchy over `rh_4_dev_get_axis`; `pcie_closest`/d2d/rmtv | HIGH |
| 16 / node | `schedule_16_dev_all_gather @0x1dafd0` (+`_vnc1 @0x1dceb0`) | `schedule_16_dev_reduce_scatter @0x1eedd0` (+`_vnc1 @0x1e3710`) → `rh_get_indices` | 8-step ring; `sengine < SENGS_PER_MLA`; vnc1 adds `rdh_get_peer_local` | HIGH |

The peer-hop helpers all live out-of-cell (`rdh.cc` `@0x1d13f0..0x1d19a0`) and are the physical-topology layer: `rdh_get_peer_rmtv`, `_d2d`, `_d2d_rmtv`, `_local`, `_pcie`, `_pcie_closest`, plus the 2-dev proxy resolvers (`rdh_2_dev_get_peer_pcie_proxy`, `rdh_2_dev_is_proxy_dev`). The `rh_get_target_rids` free function (`@0x1d2ce0`) computes and sorts the ring-IDs touched at a 4-bit (16-device) ring axis — `rid ∈ [0,15]`, `bit ∈ [0,3]`.

```c
// rdh_scheduler::schedule_1_dev_all_gather @0x1df630  (dispatch shape; representative)
function schedule_1_dev_all_gather():                            // 0x1df630
    resize(devices[*].schedule, n_steps);  resize(devices[*].received, n_steps);
    if is_trn3:  rd_1_dev_step_0_trn3();    // 0x1d9740 — single transfer per chunk
    else:        rd_1_dev_step_0();         // 0x1d8980 — halve chunk to rmtv & d2d outputs
    rd_1_dev_step_1(step);                  // 0x1d9e10 — half_chunk_tracker; get_chunk()
    rd_1_dev_step_2(step, /*flush=*/...);   // 0x1d3650 — local-peer copies (vnc==1)

// the canonical single-transfer emit (schedule_single_src_dst_transfer @0x1d3030):
function schedule_single_src_dst_transfer(dev, step, src_dev, src_addr, dst_dev, dst_addr, dst_idx, tag):
    t = rdh_transfer{ idx, src_devs=[src_dev], src_addrs=[src_addr],
                      dst_devs=[dst_dev], dst_addrs=[dst_addr], size, dst_idx };
    devices[dev].schedule[step].push_back(t);
    c = rdh_chunk{ idx, reduced_dev, addr=dst_addr, tag };
    receive(dst_dev, step, c);              // 0x1f7c90 — append to received[step] + all_chunks[c.idx]
```

> **GOTCHA —** the chunk book-keeping (`receive @0x1f7c90`, `remove_chunk @0x1f7070`, `get_chunk @0x1f6f10`) is keyed by the *tag string*, not by index alone — `all_chunks[idx]` is a `vector<rdh_chunk>` and lookups linear-scan for a matching `.tag` via `std::operator==<char>`. The same chunk index can hold several tagged variants simultaneously (`"self_chunk_rmtv_output"` vs `"self_chunk_d2d_output"` from a halved 1-dev step-0). A reimplementer who collapses `all_chunks` to a single-chunk-per-index map drops the parallel rmtv/d2d outputs and the reduction loses one rail. `get_new_buffer @0x1f6880` bump-allocates 32-byte-aligned scratch from `rdh_device.bm` and asserts `(offset + size) < capacity`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `rdh_scheduler` (ctor) | `0x1acb40` | invariants: `node_n==1`, `vnc∈{1,2}`, `rank_n∈{4,8,16,64}`, page ratio; sets `is_trn3` | HIGH |
| `rdh_scheduler::schedule_single_src_dst_transfer` | `0x1d3030` | canonical 1-src/1-dst transfer + chunk tag + receive | HIGH |
| `rdh_scheduler::rh_1_dev_step_0` | `0x1e2170` | emit step-0 `(chunk0, chunk1)` transfer pair for the 1-dev RS/AR path | HIGH |
| `rdh_scheduler::rd_1_dev_step_1` | `0x1d9e10` | 1-dev RD step-1: `half_chunk_tracker`; rmtv/d2d/d2d_rmtv | HIGH |
| `rdh_scheduler::rh_4_dev_get_indices` | `0x1e1e40` | 4-dev ring-hierarchy peer-rank set | HIGH |
| `rdh_scheduler::rd_2_dev_get_peer` | `0x1d2020` | 2-dev step→peer dispatch (pcie/d2d/rmtv/local) | HIGH |
| `rdh_scheduler::rh_2_dev_get_indices` | `0x1d20c0` | 2-dev recursive-halving reduce-target set (`step < 4`) | HIGH |
| `rdh_scheduler::receive` | `0x1f7c90` | deliver a chunk: append to `received[step]` + `all_chunks[idx]` | HIGH |
| `rdh_scheduler::get_chunk_size` / `get_chunk_offset` | `0x1f6750` / `0x1f68f0` | per-op chunk byte size / page-table offset | HIGH |
| `rdh_scheduler::get_new_buffer` | `0x1f6880` | 32B-aligned bump-allocate from `rdh_device.bm` | HIGH |
| `rdh_scheduler::merge` | `0x1d2b80` | accumulate a chunk's `reduced_dev` into the scratch chunk | HIGH |

---

## 4. The 2-Step Pod RDH (TRN2 Proxy)

### Purpose

For a TRN2 pod (UltraServer / K-PELECT), the intra `enc_mesh_primitive` carries a third RDH flavor: the **two-step pod-mesh proxy** composers (`enc/pod_mesh_primitive.cc`). These are not `rdh_scheduler`/`inter_rdh_scheduler` — they are a separate two-level pod decomposer (`ENC_ALG_TWO_STEP_POD_MESH=8`) that walks the mesh op stream and emits `proxy_start_operation → {sync | notify | rdh_send | rdh_reduce} → end_context` over `enc_primitive_mesh`, building `enc_copy_op_params`/`enc_reduce_op_params` and `transaction_info` vectors. They are documented here because they share the RDH `rdh_send`/`rdh_reduce` emit ABI and the same hierarchical intent (a node-local "direct" neighbor plus a cross-node "proxy" neighbor), but a reimplementer must keep them distinct from the recursive-halving schedulers.

The three composers resolve a **direct neighbor** and a **proxy neighbor** via `enc_get_pod_peer` and assert neighbor-group cardinalities — these asserts are the topology contract:

| Composer | Address | Op | Neighbor-group asserts | Confidence |
|---|---|---|---|---|
| `__compose_allg_with_two_step_pod_mesh_trn2_proxy` | `0x1c5600` | all-gather | `dst_neighbor_grp->ranks_n == 3`; `proxy_neighbor != direct_neighbor`; `mesh_rmv_proxy_buf != 0` | HIGH |
| `__compose_allr_with_two_step_pod_mesh_trn2_proxy` | `0x1c76c0` | all-reduce | `dst_neighbor_grp->ranks_n == 2`; `ranks[0]==proxy`/`ranks[1]==direct`; in-place + physical-match check | HIGH |
| `__compose_redsct_with_two_step_pod_mesh_trn2_proxy` | `0x1cab70` | reduce-scatter | `pod_node_id[rank] > pod_node_id[opposite_peer]`; `pod_node_id[proxy]==0`; `dim0/dim1_neighbor != −1` | HIGH |

> **NOTE —** the reduce-scatter composer is the one that exposes the **rack tier**. Its assert `ci->peers[ci->rank].pod_node_id > ci->peers[opposite_peer].pod_node_id` (`pod_mesh_primitive.cc`) orders the two pod halves, and it resolves a `dim0_neighbor` and a `dim1_neighbor` — the two axes of the `enc_topology_mode {4_DEVS_IN_ROW, 4_DEVS_IN_COLUMN}` intra-pod mesh. This is where `enc_comm_info.local_rack_rank_n` (+16) feeds the third physical level: the pod→rack→node hierarchy is expressed as a recursive 2-level intra/inter split applied at the rack boundary, not as a distinct third scheduler. The exact `local_rack_rank_n` consumption path was surface-mapped, not byte-traced — confidence **MED** on the rack-tier wiring.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_metaring_primitive::__compose_inter_rdh` | constructs the `inter_rdh_scheduler` and calls `actuate` (the INTER_RDH metaring stage) |
| `enc_mesh_primitive::__compose_{rh_redsct,rd_allgather,rdh_allreduce}` | construct the intra `rdh_scheduler` (the INTRA_RDH mesh stage) |
| `inter_rdh_scheduler::actuate` (`@0x1bb0c0`) | drains the FIFOs into `enc_primitive::rdh_{reduce,copy,send,recv}` ([enc Primitives §4](enc-primitives.md)) |
| `submit_rdh_cce_transfers` / `submit_rdh_copy_transfers` (`@0x156cd0`/`@0x15e260`) | lower one `rdh_transfer` into reduce/copy op-params ([enc Primitives §2](enc-primitives.md)) |
| `enc_setup_global_comm_internal` (`@0x10b050`) | reserves `comm.hier.devmem_res` — the cross-stage scratch this page's stages share |

## Cross-References

- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — owns the `rdh_transfer` **emit** (FIFO drain → reduce/copy op-params + net credits); this page produces the transfer vector it consumes
- [Comm Context and Bootstrap](comm-context.md) — defers `init_hierarchical_groups` and the `devmem_res` reservation to this page (§1); keys the per-level comm node by `bootstrap_participants`
- [Ring Scheduling Math](ring-scheduling.md) — the metaring step loop; the RING/KANGARING/SINGLE_CYCLE_RING stages a hier decomposition can select per slot
- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — the mesh event schedule that hosts the INTRA_RDH `rdh_scheduler` and the 2-step pod-mesh proxy composers
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](algorithm-taxonomy.md) — the `enc_alg_type` enumeration and the gate that admits `HIER`/`INTRA_RDH`/`INTER_RDH`; the `enc_pattern_t` correction
- [back to index](../index.md)
