# enc Primitives (Send/Recv Leaves)

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The composer source TU is `/opt/workspace/KaenaRuntime/enc/enc_primitive.cc` (bodies) and `enc/enc_primitive.h` (inlined accessors). `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (decompile- and DWARF-anchored)** — every struct offset is verbatim from `structures.json`; every op-params field is cross-checked against the step-recorder body that fills it (`prepare_reduce_params @0x1560d0`, `submit_rdh_cce_transfers @0x156cd0`, `proxy_end_operation @0x157990`); symbols/addresses resolve in `functions.json`; enums verbatim from `enums.json`. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

Above the device emitter (`encd_*`, [encd Overview](encd-overview.md)) and below the per-algorithm channel composers ([Ring Scheduling](ring-scheduling.md), [Mesh Composer](mesh-composer.md), [Hierarchical and RDH](hierarchical-rdh.md)) sits a single shared **primitive compose layer**: four C++ classes — `enc_mesh_primitive`, `enc_metaring_primitive`, `enc_hier_primitive`, `enc_1rank_primitive` — that all subclass `enc_alg_primitive` (168 B base) and all drive the *same* step-recorder API exposed by two leaf classes, `enc_primitive` (ring / point-to-point / RDH) and `enc_primitive_mesh` (mesh-topology flavor). The channel composers decide the *schedule* — which chunk moves on which step over which ring/mesh link; this page owns the **leaf layer they call**: the step recorders that turn one scheduled step into a concrete op-param stream (`enc_reduce_op_params`, `enc_copy_op_params`, `rdh_transfer`) plus the net-proxy FIFO entries (`net_ops_info`, `net_src_addr`, `net_dest_addr`) that bind a step to a network send/recv credit.

The reference frame is NCCL's `ncclPrimitives` — the `send`/`recvReduceSend`/`directRecvCopySend` leaf set a ring/tree algorithm calls per step. Neuron's leaves play the same role with one structural difference: they do not move bytes, they **record** them. A composer constructs a stack `enc_primitive` (or `enc_primitive_mesh`), brackets the op with `proxy_start_operation` … `mark_end`/`end_context`, and in between calls leaves like `send`, `direct_recv`, `direct_reduce_send_kangaring`, or `rdh_reduce`. Each leaf computes its DMA source/destination addresses from the page table (`__get_pgt_offset`), packs them into an op-params record, hands that record down to the device packer (`encd_dma_copy` / `encd_dma_reduce_copy` / `encd_mesh_memcopy`), and pushes one net-FIFO entry that the on-device sync core later credits against the network proxy. The whole sequence runs once, at NEFF load time, to *compile* the collective; nothing here executes at run time.

This page documents three things a reimplementer must reproduce: (1) the **four primitive classes** and which algorithm family each one serves; (2) the **op-params struct layouts** — `enc_reduce_op_params` (104 B), `enc_copy_op_params` (184 B), `rdh_transfer` (120 B) — the records the step recorders fill, field by DWARF-confirmed field; and (3) the **net-proxy FIFO discipline** — the `net_ops_info` / `net_src_addr` / `net_dest_addr` triple, the per-op terminator entry `proxy_end_operation` appends, and the credit accounting (`tx_entry_cnt`/`rx_entry_cnt`, `initial_send_credits`, `ending_recv_credits`) that a naive reimplementation gets subtly wrong.

For reimplementation, the contract is:

- **One step-recorder API, four callers.** All four primitive classes share the `enc_primitive`/`enc_primitive_mesh` leaf set. A reimplementer builds the leaves once; the difference between ring, mesh, hier, and 1-rank is *which* leaves the composer calls and *in what order*, not different leaf code.
- **The op-params records are the wire-shape of a step.** A reduce step is an `enc_reduce_op_params{s_addrs[], src_idx[], d_addrs[], dst_idx[], reduce_size}`; a copy step is an `enc_copy_op_params{s_addr, s_offsets[], d_addrs[], d_skip_copy[], d_ranks[], copy_sizes[]}`; an RDH transfer is an `rdh_transfer{src_devs[]/src_addrs[], dst_devs[]/dst_addrs[], size}`. Reproduce these layouts and the address arithmetic that fills them.
- **The net-proxy FIFO bracket is mandatory and asymmetric.** Every network-bearing op pushes one `net_ops_info` at `proxy_start_operation`, accumulates `net_src_addr`/`net_dest_addr` entries per transfer (bumping `tx_entry_cnt`/`rx_entry_cnt`), and *must* be closed by `proxy_end_operation`, which appends a sentinel terminator entry (`net_op_idx = count−1`, `size = 0`, `mark = NET_OP_COMPLETE`). Skipping the terminator leaves the device-side FIFO walker without a stop mark.

| | |
|---|---|
| **Base class** | `enc_alg_primitive` (168 B) — `ci@+0`, `op_type@+8`, `data_type@+12`, `chunk_size_n@+24`, `data_array@+80`, `pgt@+88` |
| **Ring/p2p/RDH leaf** | `enc_primitive` — net-FIFO step recorders; `self`/`next`/`peers` node graph + `ch_ctx` |
| **Mesh leaf** | `enc_primitive_mesh` (128 B) — mesh-flavor recorders; `mesh_subtype@+32`, event-driven sema resolution |
| **Reduce step record** | `enc_reduce_op_params` (104 B) — `s_addrs@0`, `src_idx@24`, `d_addrs@48`, `dst_idx@72`, `reduce_size@96` |
| **Copy step record** | `enc_copy_op_params` (184 B) — `src_idx@0`, `s_addr@8`, `s_offsets@32`, `d_addrs@56`, `copy_sizes@144` |
| **RDH transfer record** | `rdh_transfer` (120 B) — `src_devs@8`/`src_addrs@32`, `dst_devs@56`/`dst_addrs@80`, `size@104`, `dst_idx@112` |
| **Net op record** | `net_ops_info` (96 B) — one per op; credits, sema pointers, `tx/rx_entry_cnt` |
| **Net send entry** | `net_src_addr` (72 B) — one per transmit; `net_op_idx@0`, `mark@36`, `dst_rank@48` |
| **Net recv entry** | `net_dest_addr` (48 B) — one per receive; `net_op_idx@0`, `mark@36`, `src_rank@40` |
| **Op bracket** | `proxy_start_operation @0x158d40` (mesh) / `@0x15a4b0` (ring) … `mark_end @0x157b80` / `proxy_end_operation @0x157990` |
| **Reduce recorder** | `prepare_reduce_params @0x1560d0` · `submit_rdh_cce_transfers @0x156cd0` (RDH CCE) |
| **Address source** | `enc_mesh_primitive::__get_pgt_offset @0x149a70` (PGT walk) — see [Hierarchical and RDH](hierarchical-rdh.md) |

> **CORRECTION (ENC-PRIM-1) — `addr_neigh_tuple_t` is a named DWARF struct, not an inferred layout.** An earlier sweep marked the `(addr, neigh)` pair built inside `__direct_fetch_send` as MED-confidence, "inferred from `_M_realloc_append` usage". `structures.json` carries it as a real type: `addr_neigh_tuple_t` (16 B) = `{dma_addr_t addr @0, encd_neigh_t neigh @8}`. The reduce-send leaves (`direct_reduce_send_kangaring @0x158120`, `direct_recv @0x158540`) build a `std::vector<addr_neigh_tuple_t>` of `{self LOCAL + each peer's RMTV/RMTV2/LOCAL}` and pass it to `encd_dma_reduce_copy`. Confidence upgraded to **HIGH**.

> **CORRECTION (ENC-PRIM-2) — `net_addr_mark_t` is enumerated.** The same sweep left the `mark` field (`net_src_addr+36` / `net_dest_addr+36`) as LOW, "not enumerated". `enums.json` resolves it: `net_addr_mark_t` = `{ NET_TRANSFER=0, NET_OP_COMPLETE=1, EXEC_COMPLETE=2 }`. A per-transfer FIFO entry carries `NET_TRANSFER`; the `proxy_end_operation` terminator carries `NET_OP_COMPLETE`. Confidence upgraded to **HIGH** — and this is exactly the stop-mark a reimplementer must emit (see §3).

---

## 1. The Four Primitive Classes

### Purpose

Every collective a NeuronCore compiles is built by exactly one of four `enc_alg_primitive` subclasses, selected by the resolved `enc_alg_type` ([Engine Core §2](engine-core.md) owns the resolution). The four classes do not share schedule logic — that is per-class — but they *do* share the `enc_primitive`/`enc_primitive_mesh` leaf API documented in §3. The class hierarchy is the reuse axis: a reimplementer writes the leaves once and the four composers reuse them.

### The class table

| Class | Size | Role | Algorithms it serves | Step-recorder leaf | Confidence |
|---|---|---|---|---|---|
| `enc_mesh_primitive` | 328 B | single-level mesh collectives; full/grouped/TRN2/switch mesh, RDH mesh, all-to-all | `MESH(2)`, `INTRA_RDH(5)`, `INTER_RDH(7)`, `SINGLE_STEP_MESH(6)`, `TWO_STEP_POD_MESH(8)`, `LATENCY/BW_OPT_MESH(9/10)` | `enc_primitive_mesh` | HIGH |
| `enc_metaring_primitive` | 3840 B | ring-family per-channel collectives | `RING(0)`, `KANGARING`, `SINGLE_CYCLE_RING`, `RDH` metaring | `enc_primitive` | HIGH |
| `enc_hier_primitive` | 248 B | two-level (inter-node × intra-node) hierarchical collectives | `HIER(1)` — recursively composes a mesh **or** metaring sub-primitive per level | *(delegates)* | HIGH |
| `enc_1rank_primitive` | 184 B | degenerate single-rank "ring" (self collective) | `RING(0)` with `rank_n == 1` | `enc_primitive` | HIGH |

> **QUIRK —** `enc_hier_primitive` (248 B) owns no leaf code of its own. Its composers (`__compose_allgather @0x1a0f10`, `__compose_allreduce @0x1a34c0`, `__compose_redsct @0x1a6d60`) decompose the op into an **inter** stage and an **intra** stage, build a page table per stage (`__build_*_pgt_inter_*` / `__build_*_pgt_intra_*`), then *construct a fresh `enc_mesh_primitive` or `enc_metaring_primitive`* per stage and call its `compose_operation`. Hier allreduce = intra reduce-scatter + inter allreduce + intra allgather, three sub-primitives. A reimplementer must treat hier as an orchestrator over the other two classes, not a peer leaf-emitter.

### Base layout (`enc_alg_primitive`, 168 B)

All four classes share this header; the leaves read `pgt`, `data_array`, `chunk_size_n`, and `data_type` from it on every step.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `ci` | +0 | `const enc_comm_info*` | parsed topology (`rank`, `rank_n`, `node_n`, `peers[]`) | HIGH |
| `op_type` | +8 | `const enc_op_type` | the collective being composed (dispatch key) | HIGH |
| `data_type` | +12 | `SDMA_DTYPE` | element type → `sdma_data_type_size[dt]` byte size | HIGH |
| `data_op_type` | +16 | `SDMA_CCETYPE` | in-DMA reduce opcode (`ADD/FMA/MAX/MIN/EXT/GCE`) | HIGH |
| `chunk_size_n` | +24 | `size_t` | per-channel chunk element count (set by `__set_dynamic_chunk_size`) | HIGH |
| `nr_channel_chunks` | +32 | `int` | chunk-groups per channel | HIGH |
| `channel_n` | +36 | `const int` | active channel count | HIGH |
| `completion_assert_addr` | +56 | `vector<u64>` | per-op completion semaphore addresses (copied into each leaf) | HIGH |
| `data_array` | +80 | `const data_array_t*` | input/output/scratch tensor descriptors | HIGH |
| `pgt` | +88 | `enc_pagetable*` | page table the leaves walk via `__get_pgt_offset` | HIGH |
| `instr_chaining` | +128 | `enc_op_instr_chaining` | `NONE/INITIAL/BODY/FINAL` — op-fusion chain position | HIGH |
| `function_id` | +136 | `u32` | NEFF function id, stamped into `net_ops_info.basic_block_id` | HIGH |
| `inter_rdh_ch_buf_size` | +152 | `const size_t` | RDH channel buffer size | HIGH |
| `inter_rdh_recv_window_n` | +160 | `u32` | RDH recv credit window (see [Hierarchical and RDH](hierarchical-rdh.md)) | HIGH |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_mesh_primitive::compose_operation` | `0x1a0d00` | mesh build pipeline → `__compose_channel` dispatch | HIGH |
| `enc_metaring_primitive::compose_operation` | `0x178170` | ring master emit loop ([Ring Scheduling](ring-scheduling.md)) | HIGH |
| `enc_hier_primitive::compose_operation` | `0x1a8d20` | hier orchestrator → mesh/metaring sub-primitives | HIGH |
| `enc_1rank_primitive::compose_operation` | `0x15d150` | single-rank self collective | HIGH |
| `enc_1rank_primitive::__handle_semaphore_init` | `0x14cd10` | 1-rank metaring semaphore bring-up | HIGH |
| `enc_1rank_primitive::__devmem_res_checkout` | `0x14ce10` | 1-rank channel-buffer device-memory checkout | HIGH |

---

## 2. The Op-Params Records

### Purpose

A scheduled step is materialized as one op-params record before it is handed to the device packer. There are exactly three record types, each owned by a different transfer flavor: `enc_reduce_op_params` for an N-source in-DMA reduce, `enc_copy_op_params` for a 1-source / M-destination scatter copy, and `rdh_transfer` for the RDH (Ring-Distributed-Hierarchy) src→dst tuples that the RDH scheduler emits and the CCE/copy submitters consume. These are the *only* data a reimplementer must produce to drive the device packer; everything else (semaphores, credits) is FIFO bookkeeping (§3).

### `enc_reduce_op_params` (104 B)

The reduce record is N source addresses reduced into M destination addresses, with parallel rank-index vectors so the device knows which neighbor each address belongs to. It is built by three recorders: `prepare_reduce_params @0x1560d0` (mesh, from `transaction_info` + PGT offsets), `submit_rdh_cce_transfers @0x156cd0` (RDH, from `rdh_transfer`), and inline in `__compose_allreduce_full_mesh_t0 @0x1595f0` (the 2-reader/1-writer mesh step).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `s_addrs` | +0 | `vector<u64>` | source DMA addresses (one per reader) | HIGH |
| `src_idx` | +24 | `vector<int>` | source rank/neighbor index per `s_addrs[i]` | HIGH |
| `d_addrs` | +48 | `vector<u64>` | destination DMA addresses (one per writer) | HIGH |
| `dst_idx` | +72 | `vector<int>` | destination rank/neighbor index per `d_addrs[i]` | HIGH |
| `reduce_size` | +96 | `size_t` | byte length of the reduce (`dtype_size × element count`) | HIGH |

> **GOTCHA —** `src_idx`/`dst_idx` are **neighbor-group indices**, not absolute ranks. `submit_rdh_cce_transfers` resolves the source index with `get_idx_in_group(ci, grp, src_dev)` and the destination index with `get_idx_in_group(ci, grp+1, dst_dev)` — *two different neighbor groups* (`grp` for src, `grp+1` for dst). A reimplementer who indexes both into the same group, or who stores raw `neuron_dev` ids here, mis-routes every RDH reduce. The `grp` vs `grp+1` split is the RDH src/dst rank-to-group resolution.

### `enc_copy_op_params` (184 B)

The copy record is one source fanned out to many destinations with per-destination skip flags — the shape a mesh broadcast or RDH all-gather copy needs. Built by `submit_rdh_copy_transfers @0x15e260` (RDH copy) and `a2a_switch_send @0x154b80` (mesh switch all-to-all); consumed by `enc_primitive_mesh::send @0x181900`.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `src_idx` | +0 | `int` | source rank/neighbor index | HIGH |
| `s_addr` | +8 | `mapped_dma_addr_t` | source `{dev_addr, host_addr}` (16 B) | HIGH |
| `nccl_mhandle` | +24 | `void*` | source net (NCCL) memory-registration handle | HIGH |
| `s_offsets` | +32 | `vector<u32>` | per-destination source-offset list | HIGH |
| `d_addrs` | +56 | `vector<u64>` | destination DMA addresses | HIGH |
| `d_skip_copy` | +80 | `vector<bool>` | per-destination skip flag (40 B `vector<bool>` header) | HIGH |
| `d_ranks` | +120 | `vector<int>` | destination rank per `d_addrs[i]` | HIGH |
| `copy_sizes` | +144 | `vector<u64>` | per-destination byte length | HIGH |
| `host_d_addr` | +168 | `void*` | destination host address (net path) | HIGH |
| `dst_nccl_mhandle` | +176 | `void*` | destination net memory-registration handle | HIGH |

> **NOTE —** the parallel-vector discipline is strict and asserted. `enc_primitive_mesh::send @0x181900` checks `dst < d_addrs.size()`, `copy_size_idx < copy_sizes.size()`, `copy_size < UINT32_MAX`, and `nccl_mhandle != nullptr` per destination (`enc_primitive.cc:0x75F/0x764/0x77C/0x77D`). `d_addrs`, `d_skip_copy`, `d_ranks`, and `copy_sizes` must all carry the same length; a reimplementer keeping them out of step trips a load-time assert, not a silent miscopy.

### `rdh_transfer` (120 B)

The RDH transfer is the scheduler's output unit: a set of source `(dev, addr)` pairs and a set of destination `(dev, addr)` pairs that the CCE/copy submitters lower into reduce/copy op-params. The RDH scheduler (`enc/inter_rdh.cc`, [Hierarchical and RDH](hierarchical-rdh.md)) produces a `vector<rdh_transfer>`; `submit_rdh_cce_transfers`/`submit_rdh_copy_transfers` consume it.

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `idx` | +0 | `int` | transfer index within the schedule | HIGH |
| `src_devs` | +8 | `vector<int>` | source `neuron_dev` ids | HIGH |
| `src_addrs` | +32 | `vector<u64>` | source DMA addresses (1:1 with `src_devs`) | HIGH |
| `dst_devs` | +56 | `vector<int>` | destination `neuron_dev` ids | HIGH |
| `dst_addrs` | +80 | `vector<u64>` | destination DMA addresses (1:1 with `dst_devs`) | HIGH |
| `size` | +104 | `size_t` | transfer byte length | HIGH |
| `dst_idx` | +112 | `int` | destination ordinal | HIGH |

> **QUIRK —** `can_use_cme_sdma @0x14b330` decides the device path *from the transfer shape*: it returns true only if **every** `rdh_transfer` in the vector has exactly one `src_addr` and one `dst_addr` (a 1:1 copy). CME-SDMA handles 1:1 copies; any transfer with a fan-out forces the general path. `submit_rdh_cce_transfers` separately asserts `src_devs.size() == src_addrs.size()` (`enc_primitive.cc:0x1B1E`). A reimplementer must keep the dev/addr vectors length-locked and route fan-out transfers off the CME-SDMA fast path.

---

## 3. The Step Recorders and the Net-Proxy FIFO

### Purpose

A step recorder is the function that turns one scheduled step into (a) an op-params record handed to the device packer and (b) one or more net-proxy FIFO entries. The FIFO discipline is the part that bites reimplementers: every network-bearing op is a bracket — `proxy_start_operation` pushes one `net_ops_info`, the per-step recorders push `net_src_addr`/`net_dest_addr` entries and bump `tx_entry_cnt`/`rx_entry_cnt`, and `proxy_end_operation` closes the bracket with a sentinel terminator. The on-device sync core later walks these FIFOs to drive the network proxy; the terminator is its stop mark.

### Entry Point

```text
<channel composer>                                    ── e.g. __compose_allreduce_channel (0x171600)
  └─ enc_primitive::proxy_start_operation (0x15a4b0)   ── push net_ops_info{credits, sema ptrs}
       │  (mesh: enc_primitive_mesh:: variant 0x158d40 ── resolve event sema iaddr)
       ├─ <per-step leaf> × N                          ── send / recv_reduce_send / direct_recv / …
       │    ├─ __get_pgt_offset (0x149a70)             ── [boundary] DMA src/dst from page table
       │    ├─ encd_dma_copy / encd_dma_reduce_copy    ── [boundary] tdrv/encd.c device packer
       │    └─ __record_net_src_addr / __record_net_dest_addr ── push FIFO entry, ++tx/rx_entry_cnt
       └─ enc_primitive::mark_end (0x157b80)           ── encd_dma_mark_end + proxy_end_operation
            └─ proxy_end_operation (0x157990)          ── push terminator {idx=N-1, size=0, NET_OP_COMPLETE}
```

### Algorithm — a reduce step recorder

`prepare_reduce_params @0x1560d0` is the canonical reduce recorder: it walks the op's remaining size in chunks, resolves each input/output transaction's virtual offset and DMA size from the page table, finds the destination neighbor, and appends one `enc_reduce_op_params` per reduce. The address arithmetic is the reimplementation-grade core.

```c
// enc_mesh_primitive::prepare_reduce_params @0x1560d0
// builds a vector<enc_reduce_op_params> for one reduce event
function prepare_reduce_params(event_id, in_txns, out_txn, total_size):   // 0x1560d0
    assert(events[event_id].valid);                          // enc_primitive.cc:0x21ED
    out = {};                                                 // vector<enc_reduce_op_params>
    remaining = total_size;
    offset    = 0;
    while remaining > 0:
        op = enc_reduce_op_params{};                         // 104 B, all vectors empty
        // --- sources: each input transaction contributes one reader ---
        for txn in in_txns:
            // PGT walk: virtual offset + DMA size for this chunk
            (voff, dma_sz) = __get_pgt_offset(PAGETABLE_TYPE_INPUT, txn, offset);   // 0x149a70
            assert(dma_sz == remaining || dma_sz == txn.input_dma_size);            // :0x2202/0x2212
            op.s_addrs.push_back(txn.base + voff);
            op.src_idx.push_back(txn.src_neighbor_idx);
        // --- destination: resolve the writer neighbor ---
        (voff_o, _) = __get_pgt_offset(PAGETABLE_TYPE_OUTPUT, out_txn, offset);     // 0x149a70
        dst_nbr = find_dst_neighbor_idx(out_txn);
        assert(dst_nbr != -1);                               // :0x2220
        op.d_addrs.push_back(out_txn.base + voff_o);
        op.dst_idx.push_back(dst_nbr);
        op.reduce_size = min(dma_sz, remaining);             // bytes this step reduces
        out.push_back(op);
        remaining -= op.reduce_size;
        offset    += op.reduce_size;
    return out;                                              // -> enc_primitive_mesh::reduce (0x14d900)
```

The RDH variant `submit_rdh_cce_transfers @0x156cd0` is the same record built from a different source — an `rdh_transfer` instead of a `transaction_info` — with the two-group index resolution from §2's GOTCHA:

```c
// submit_rdh_cce_transfers @0x156cd0  (file-local static)
function submit_rdh_cce_transfers(prim, transfers, event, evt_id, ci):  // 0x156cd0
    for t in transfers:
        assert(t.src_devs.size() == t.src_addrs.size());     // enc_primitive.cc:0x1B1E
        op = enc_reduce_op_params{};
        for i in 0 .. t.src_devs.size():
            op.s_addrs.push_back(t.src_addrs[i]);
            op.src_idx.push_back(get_idx_in_group(ci, grp,   t.src_devs[i]));   // group grp
        for j in 0 .. t.dst_devs.size():
            op.d_addrs.push_back(t.dst_addrs[j]);
            op.dst_idx.push_back(get_idx_in_group(ci, grp+1, t.dst_devs[j]));   // group grp+1
        op.reduce_size = t.size;
        *evt_id = (*evt_id) + 1;                             // advance event cursor
        enc_primitive_mesh::rdh_reduce(prim, *evt_id, &op, /*cce=ADD..GCE*/4);   // 0x149240
```

### Algorithm — the net-proxy FIFO bracket

`proxy_start_operation @0x15a4b0` (ring) opens the bracket by pushing one `net_ops_info` carrying the credit window and the four semaphore pointers; the per-step recorders append `net_src_addr`/`net_dest_addr` entries and increment the op's `tx_entry_cnt`/`rx_entry_cnt`; `proxy_end_operation @0x157990` closes it with a sentinel terminator on *both* FIFOs.

```c
// enc_primitive::proxy_start_operation @0x15a4b0  (ring / p2p flavor)
function proxy_start_operation():                            // 0x15a4b0
    info = net_ops_info{};                                   // 96 B
    info.inc_recv_handshake = encd_get_net_recv_event_sema_iaddr_from_vaddr_ring(dma_channel);  // 0x238610
    info.inc_send_handshake = encd_get_net_send_event_sema_iaddr_from_vaddr_ring(dma_channel);  // 0x238660
    info.net_idx_loop_size    = NET_INDEX_LOOP_SIZE;         // 512
    info.initial_send_credits = net_initial_send_credits;    // assert in (0, 0x1FF]  :0xA2A/0xA2C
    info.ending_recv_credits  = net_ending_recv_credits;     // assert in (0, 0x1FF]  :0xA2B/0xA2D
    info.data_type_sz   = sdma_data_type_size[data_type];
    info.variable_peer  = variable_peer;
    info.basic_block_id = function_id;                       // base+136
    info.func_rel_op_idx = func_rel_op_idx;
    info.enc_channel    = channel;
    push(ch_ctx->net_ops_info_fifo, info);                   // one per op

// __record_net_dest_addr @0x157c50  (per-receive entry)
function __record_net_dest_addr(dev_addr, host, mhandle, size, complete, src_rank):  // 0x157c50
    assert(self.buf_sz > 0);                                 // :0x92
    assert(ch_ctx->net_ops_info_fifo && !empty);             // :0x93/0x94
    n = net_ops_info_fifo.size();
    push(self.dest_addr_fifo, net_dest_addr{
        net_op_idx = n - 1, complete, dev_addr, host, mhandle,
        size, mark = NET_TRANSFER, src_rank });
    ++net_ops_info_fifo.back().rx_entry_cnt;                 // count this receive

// enc_primitive::proxy_end_operation @0x157990  (the mandatory terminator)
function proxy_end_operation():                              // 0x157990
    assert(ch_ctx->net_ops_info_fifo && !empty);             // :0xA4F/0xA50
    n = net_ops_info_fifo.size();                            // = byte_len / 96  (magic-div)
    if next.src_addr_fifo:                                   // a transmitting op
        push(next.src_addr_fifo, net_src_addr{
            net_op_idx = n - 1, dst_rank = -1,
            size = 0, mark = NET_OP_COMPLETE, /* rest zero */ });
    if self.dest_addr_fifo:                                  // a receiving op
        push(self.dest_addr_fifo, net_dest_addr{
            net_op_idx = n - 1,
            size = 0, mark = NET_OP_COMPLETE, src_rank = -1 });
```

> **GOTCHA —** the terminator is encoded by a single 16-byte store of the constant `0x100000000` at `net_*_addr.size` (decompile `0x157990:44/76`): the low dword is `size = 0` and the next dword is `mark = 1 = NET_OP_COMPLETE`. The `net_op_idx = n − 1` is computed by magic-division of the `net_ops_info_fifo` byte length by `sizeof(net_ops_info) = 96` (`-1431655765 × (delta >> 5)` = `delta / 96`), then minus one — i.e. "the index of the op this terminator closes". A reimplementer who appends a terminator with `size != 0`, the wrong `mark`, or a stale `net_op_idx` desynchronizes the device FIFO walker, which reads `mark == NET_OP_COMPLETE` as the per-op stop boundary.

> **QUIRK —** the mesh recorders are a parallel set, not a wrapper. `enc_primitive_mesh::proxy_start_operation @0x158d40` resolves its handshake/complete semaphore *addresses* through the mesh driver handle (`encd_get_event_sema_iaddr_from_vaddr_mesh`) keyed by `enc_mesh_event_type`, and `end_context @0x158060` (the mesh analog of `mark_end`) re-fetches the channel via `encd_mesh_get_channel`. The ring leaves resolve theirs through the ring channel (`encd_get_net_{recv,send}_event_sema_iaddr_from_vaddr_ring`). Same `net_ops_info` record, two different sema-resolution backends — a reimplementer cannot share the resolution code across the two topologies even though the FIFO record is identical.

### `net_ops_info` (96 B) — the per-op credit record

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `sema_shift_offset` | +0 | `u16` | per-op semaphore shift | HIGH |
| `early_send_completion` / `early_recv_posting` | +2 / +3 | `bool` | early-completion / early-posting flags | HIGH |
| `inc_send_handshake` … `inc_recv_complete` | +8 / +16 / +24 / +32 | `volatile u32*` | the four device semaphore pointers | HIGH |
| `tx_entry_cnt` / `rx_entry_cnt` | +40 / +44 | `u32` | count of `net_src_addr` / `net_dest_addr` entries for this op | HIGH |
| `net_idx_loop_size` | +48 | `u32` | net index ring size (`NET_INDEX_LOOP_SIZE = 512`) | HIGH |
| `initial_send_credits` / `ending_recv_credits` | +52 / +56 | `u32` | credit window (asserted in `(0, 0x1FF]`) | HIGH |
| `data_type_sz` | +64 | `size_t` | element byte size | HIGH |
| `variable_peer` | +73 | `bool` | dynamic-peer op (mesh) | HIGH |
| `basic_block_id` | +76 | `u32` | NEFF `function_id` | HIGH |
| `func_rel_op_idx` / `unrolled_count` | +80 / +84 | `u32` | function-relative op index / unroll count | HIGH |
| `enc_channel` | +88 | `void*` | owning channel back-pointer | HIGH |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_primitive::proxy_start_operation` | `0x15a4b0` | ring/p2p: push `net_ops_info` (credits, ring sema iaddr) | HIGH |
| `enc_primitive_mesh::proxy_start_operation` | `0x158d40` | mesh: push `net_ops_info` (event-keyed mesh sema iaddr) | HIGH |
| `enc_primitive_mesh::proxy_advance_operation` | `0x158f50` | tail-call thunk → mesh `proxy_start_operation` | HIGH |
| `enc_primitive::proxy_end_operation` | `0x157990` | append `NET_OP_COMPLETE` terminator to src+dest FIFOs | HIGH |
| `enc_primitive_mesh::proxy_end_operation` | `0x157eb0` | mesh flavor; guarded by `!wait_sync_only` | HIGH |
| `enc_primitive::mark_end` | `0x157b80` | `encd_dma_mark_end` then `proxy_end_operation` | HIGH |
| `enc_primitive_mesh::end_context` | `0x158060` | mesh op close: `encd_dma_mark_end` + `proxy_end_operation` | HIGH |
| `enc_primitive::__record_net_dest_addr` | `0x157c50` | push `net_dest_addr`, ++`rx_entry_cnt` (6-arg) | HIGH |
| `enc_primitive::__record_net_dest_addr` | `0x157da0` | 4-arg thunk → 6-arg with `size=self.buf_sz`, `src_rank=-1` | HIGH |
| `prepare_reduce_params` | `0x1560d0` | mesh reduce recorder → `enc_reduce_op_params[]` | HIGH |
| `submit_rdh_cce_transfers` | `0x156cd0` | RDH reduce recorder (`rdh_transfer` → reduce params) | HIGH |
| `submit_rdh_copy_transfers` | `0x15e260` | RDH copy recorder (`rdh_transfer` → `enc_copy_op_params`) | HIGH |

---

## 4. The Send/Recv Leaves

### Purpose

The per-step data-movement leaves are the functions a channel composer calls in its step loop. Each resolves its DMA addresses from the page table, emits the device packet (`encd_dma_copy` / `encd_dma_reduce_copy` / `encd_dma_copy_sb2sb` / `encd_mesh_memcopy`), and records the net entry. They split by transport: ring leaves over `next`/`self`/`peers` nodes, mesh leaves over the event schedule. The central kernel is `__direct_fetch_send @0x16e650`, an 8-flag fan-out that every ring copy/recv leaf wraps.

### The node graph the ring leaves traverse

Ring leaves read three node descriptors off the `enc_primitive`: `self` (the local rank), `next` (the ring-forward target), and the `peers[]` (kangaring RMTV/RMTV2/LOCAL multi-rail neighbors). Each carries its own net FIFO.

| Struct | Size | Key fields | Role |
|---|---|---|---|
| `node` (self) | 144 B | `input@0`/`input1@24`/`output@48` (mapped handles), `buffers@72`, `gw_ch_buffer@96`, `send_buf@104`, `dest_addr_fifo@128`, `buf_sz@136` | local rank's buffers + receive FIFO |
| `node_next` | 48 B | `output@0` (validated non-null), `buffers@24` (!=0), `gw_ch_buffer@32`, `src_addr_fifo@40` | ring-forward target + transmit FIFO |
| `node_peer` | 56 B | `input@0`, `output@24`, `neigh@48` (`ENCD_NEIGH_PEER_RMTV/RMTV2/LOCAL`), `output_shared_virtual_scratchpad@52` | kangaring multi-rail peer |

> **NOTE —** `node_next.src_addr_fifo` (a `vector<net_src_addr>*`) being non-null is exactly the selector between the **net** path and the **local** path: a leaf records a `net_src_addr` and posts a send credit only when the forward target is across the network. `__direct_fetch_send` asserts `!send_to_peers || !is_next_net` (`enc_primitive.cc:0x3FB`) — peer fan-out and a net next-hop are mutually exclusive on one step.

### Algorithm — the central fetch/send kernel

`__direct_fetch_send @0x16e650` is the densest leaf: an eight-boolean kernel that emits up to three `encd_dma_reduce_copy` packets (one per RMTV/LOCAL peer, `SDMA_CCETYPE_ADD`) or a single `encd_dma_copy`, then posts the send/recv and marks the step.

```c
// enc_primitive::__direct_fetch_send @0x16e650
// flags: send_to_peers, reverse_self_and_peer, read_from_gw, write_to_gw,
//        skip_data_transfer, use_channel_buffer  (+ hc, neigh)
function __direct_fetch_send(hc, neigh, flags):              // 0x16e650
    src = pick_source(self, peers, flags.read_from_gw, flags.use_channel_buffer);
    dst = pick_dest(next, self.gw_ch_buffer, flags.write_to_gw);
    if flags.send_to_peers:                                  // reduce fan-in from up to 3 peers
        assert(!peers[0].output_shared_virtual_scratchpad);  // :0x404
        assert(peers[1].neigh in {PEER_RMTV2, PEER_LOCAL});  // :0x421
        assert(peers[2].neigh in {PEER_RMTV2, PEER_LOCAL});  // :0x429
        tuples = [ {self_addr, ENCD_NEIGH_LOCAL} ];          // vector<addr_neigh_tuple_t>
        for p in peers: tuples.push_back({p.addr, p.neigh});
        encd_dma_reduce_copy(dst, tuples, SDMA_CCETYPE_ADD); // 0x23e070
    else if !flags.skip_data_transfer:
        encd_dma_copy(dst, src, size);                       // 0x23cf80
    if next.src_addr_fifo:  __record_net_src_direct_addr(...); __post_send(neigh);
    else:                   __record_net_dest_addr(...);     __post_recv(...);
    __mark_step(0);
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_primitive::__direct_fetch_send` | `0x16e650` | central 8-flag fan-out kernel (up to 3-peer reduce) | HIGH |
| `enc_primitive::direct_copy_send` | `0x16f710` | PGT-validated copy: `__get_pgt_offset(OUTPUT)` → fetch_send (`NEIGH_LOCAL`) | HIGH |
| `enc_primitive::direct_recv_send` | `0x16f820` | `NEIGH_PREV` + reverse + channel-buffer fetch_send | HIGH |
| `enc_primitive::direct_recv` | `0x158540` | per-chunk OUTPUT recv; reduce path builds `addr_neigh_tuple_t` peers | HIGH |
| `enc_primitive::direct_reduce_send_kangaring` | `0x158120` | kangaring fused reduce+send to `next.buffers + dst_buf_idx·half_buffer_size` | HIGH |
| `enc_primitive::direct_reduce_send_permute` | `0x16c110` | fused reduce+send for permute | HIGH |
| `enc_primitive::send` | `0x16c960` | half-chunk-aware ring send leaf; net src addr + `__post_send` | HIGH |
| `enc_primitive::direct_send` | `0x174830` | INPUT/OUTPUT PGT walk → `encd_dma_copy`/`_sb2sb` → `__post_send` | HIGH |
| `enc_primitive::rdh_send` | `0x16e580` | RDH leaf: net src direct addr → `__post_send(NEIGH_NEXT)` | HIGH |
| `enc_primitive::rdh_recv` | `0x157dc0` | RDH leaf: `__record_net_dest_addr` → `__mark_step` → `__post_recv` | HIGH |
| `enc_primitive_mesh::send` | `0x181900` | mesh copy leaf (22 callers); `encd_mesh_memcopy`/`_sb2sb` over `enc_copy_op_params` | HIGH |
| `enc_primitive_mesh::a2a_switch_send` | `0x154b80` | mesh switch all-to-all → `encd_mesh_memcopy` | HIGH |

### Considerations

- **Half-chunk pipelining is per-leaf.** `send`, `direct_recv`, and the reduce-send leaves take an `enc_half_chunk_index` (`CHUNK_H0=0`, `CHUNK_H1=1`, `ENC_CHUNK_SPLIT_N=2`); the step loop issues each step twice so the two halves of a chunk overlap on adjacent links. The half-chunk math is owned by [Ring Scheduling](ring-scheduling.md); the leaves only assert `!(channel_chunk_sz_bytes & 1)` (`enc_primitive.h:0x1EE`) so a chunk always splits evenly.
- **Device packing is the boundary.** The leaves stop at `encd_dma_copy @0x23cf80` / `encd_dma_reduce_copy @0x23e070` / `encd_mesh_memcopy @0x23a790`; the actual 16-byte UDMA descriptor packing and SPAD op-stream emission belong to [encd Overview](encd-overview.md). This page's leaves decide *what* to copy and record the net credit; `encd` decides *how* the descriptor is laid out.
- **`enc_primitive_mesh::send` is the mesh hot leaf.** With 22 callers it is the convergence point of every mesh broadcast/collective composer; it chooses `encd_mesh_memcopy` vs `encd_mesh_memcopy_sb2sb` per destination and updates the net index (`encd_mesh_dma_update_net_index`). Its per-destination assert battery (§2 NOTE) is the contract the `enc_copy_op_params` vectors must satisfy.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_metaring_primitive::compose_operation` (`@0x178170`) | the ring composer whose step loop calls these leaves |
| `enc_mesh_primitive::__compose_channel` (`@0x1a0910`) | the mesh dispatch that selects which leaf-driving composer runs |
| `enc_primitive_mesh::reduce` / `rdh_reduce` (`@0x14d900` / `@0x149240`) | the reduce sinks `prepare_reduce_params` / `submit_rdh_cce_transfers` feed |
| `__get_pgt_offset` (`@0x149a70`) | the page-table walk every leaf uses to resolve DMA addresses |
| `encd_dma_copy` / `encd_dma_reduce_copy` (`@0x23cf80` / `@0x23e070`) | the device packers the leaves bottom out into |

## Cross-References

- [Ring Scheduling Math](ring-scheduling.md) — the per-channel step schedule that calls these leaves; half-chunk and chunk-group math
- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — the mesh event schedule whose leaf interpreters drive `enc_primitive_mesh::send`/`reduce`
- [Hierarchical and RDH Composition](hierarchical-rdh.md) — the hier orchestrator and the RDH scheduler that produces the `rdh_transfer` vectors these recorders consume
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — the device packer floor (`encd_dma_copy`/`_reduce_copy`/`mesh_memcopy`) the leaves hand off to
- [Engine Core (enc_context and Accessors)](engine-core.md) — the `enc_alg_type` resolution that selects which of the four primitive classes builds the op
- [back to index](../index.md)
