# encd: the Device-Resident Descriptor Emitter

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The emitter source TU is `/opt/workspace/KaenaRuntime/tdrv/encd.c`; the composer layer above it is `enc/enc.cc` (`enc_*`). `.text`/`.rodata` VMA == file offset, so every `0x2…`/`0x3…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF- and symbol-anchored)** — all `encd_*` entry points resolve via `addr2line` to a single TU (`tdrv/encd.c`), are `local` (`t`) symbols, and were cross-checked against `function_addresses.json` + `structures.json` + `strings.json`. There are **zero** libnccom/`nccl*`/`neuron*`/`nec_*` edges from any function in this layer (callgraph out-edge sweep). · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

A NeuronCore does not call a collective; it *runs a program* that a collective was compiled into at NEFF load time (see [Overview §1](overview.md)). That compile is two layers stacked inside libnrt: a host-side **composer** (`enc/enc.cc`, the `enc_*` and `enc_primitive*` C++ classes) that turns a parsed replica group into a per-NeuronCore schedule of ring / mesh / hierarchical events, and a device-resident **emitter** (`tdrv/encd.c`, the `encd_*` driver) that lowers each composed event into the wire artifacts the silicon actually consumes. This page is the map of that emitter floor — the layer **below** the composers and **above** the al_udma/vring HAL, with no NCCL math fork in sight. It orients the four `encd` deep-dive pages and pins the boundary the composer crosses to reach them.

The emitter's job is to turn a composed schedule into two device-resident descriptor streams plus the topology state that binds them. The first stream is **TopSP scratch-pad (SPAD) control + slot descriptors**: an op-stream the on-device sync core (top-SP) walks, built by `encd_dma_mark_end @0x237200`, which packs `spad_ctrl` control words (via the inlined `create_spad_ctrl_entry @0x232cd0`) and `spad_slot` step entries (terminated by `mark_spad_slot_final_entry @0x22fc20`) into each engine's growable SPAD section. The second stream is **SDMA (al_udma) DMA packets**: 16-byte UDMA descriptors packed into per-channel **virtual rings** (`vring`) by the descriptor packers `add_dma_packet @0x22ff20` (copy / M2S-S2M) and `add_dma_packet_cce @0x2307d0` (collective-compute-engine reduce), which forward to the vring sinks `vring_add_dma_packet_v2 @0x312440` / `vring_add_dma_packet_cce @0x3134e0`. The binding state is the per-metaring channel-activation set and the per-NeuronCore `encd_context`.

The reference frame is a NIC's descriptor-emission floor. The composer is the protocol stack that decides *what* to send (ring order, mesh peering, reduce tree); `encd` is the descriptor-ring driver that turns those decisions into hardware submission entries — sizing packets to the engine's `max_desc_per_packet` and 64 KiB length cap, stamping address-routing bits for cross-device peers, padding short DMA engines so every participant emits an equal descriptor count, and marking the completion semaphore on the terminating descriptor. The composer never writes a wire byte; it calls `encd_*` entry points, and those entry points are the membrane this page documents. The four sub-pages then split the floor by concern: the DMA/devmem floor, the semaphore + TopSP bring-up, and the per-arch CSR dispatch.

For reimplementation, the map-level contract is:

- **The composer→emitter hand-off is the `encd_*` symbol family.** The composer's per-step leaves (`enc_primitive::mark_end`, `enc_primitive_mesh::sync`/`prefetch_metadata`, `enc_metaring_primitive::__set_active_channels`) call **down** into `encd_dma_mark_end`, `encd_mesh_add_wr_barrier @0x2386c0`, `encd_mesh_dma_send_net_proxy @0x238340`, `encd_ring_set_active_channels_cnt @0x237100`. The emitter never calls back **up** into `enc_*`; the edge is strictly one-directional.
- **Two descriptor streams, one context.** SPAD (TopSP op-stream) and SDMA (vring DMA packets) are distinct artifacts with distinct sinks; both are sized and routed by per-priority / per-arch knobs read from the `encd_context` (and its `enc_context` peer at `+288`). A reimplementer must reproduce both streams *and* the context that parameterizes them.
- **No libnccom edge at this layer.** The NCCL math fork (`neuron*` ABI over `dlsym`) is reached only by the composer/bootstrap layer. Every `encd_*` callee is intra-libnrt: `encd_arch_*` (per-arch CSR/topology), the `vring_*`/`dmem_*` DMA HAL, and libc. A reimplementer wiring an NCCL call into the emitter floor has mis-placed the boundary.

| | |
|---|---|
| **Emitter TU** | `tdrv/encd.c` (`/opt/workspace/KaenaRuntime`), GCC 14.2.1 — all `encd_*` `local` symbols |
| **Per-NC context** | `encd_context` — 624 B; `vcore @+0`, `ccop_owner @+16`, `enc_ctx @+288`, `curr_priority_class @+272`, `ctx_id @+561` |
| **SPAD op-stream emitter** | `encd_dma_mark_end @0x237200` (3983 B) → `create_spad_ctrl_entry @0x232cd0`, `mark_spad_slot_final_entry @0x22fc20` |
| **SDMA copy packer** | `add_dma_packet @0x22ff20` → `vring_add_dma_packet_v2 @0x312440` |
| **SDMA reduce packer** | `add_dma_packet_cce @0x2307d0` → `vring_add_dma_packet_cce @0x3134e0` |
| **TopSP channel config** | `prep_metaring_topsp_config @0x2331d0` (ring), the mesh analog (mesh) |
| **Packet sizing** | `encd_get_dma_copy_packet_size @0x238bc0` (copy) · `encd_get_cce_reduce_packet_size @0x2396f0` (reduce) — per-priority |
| **Channel-activation state** | `encd_ring_set_active_channels_cnt @0x237100` · `encd_ring_is_channel_active @0x2371c0` |
| **DMA channel object** | `encd_dma_channel` — 2 099 736 B; `tp_inc_steps[262144] @+2536`, `comm @+2099704`, `mesh @+2099728` |
| **Address routing** | `set_addr_routing_bits @0x231340` (neighbor sema bits) · `set_addr_forward_bit @0x231460` (net-proxy forward) |
| **Multi-node boundary** | **none at this layer** — composer/bootstrap own the libnccom `neuron*` ABI |

---

## 1. The emitter floor in the two-layer stack

The collective compile (mapped end-to-end on [Overview §1–2](overview.md)) bottoms out here. The composer produces an algorithm-family schedule object — `enc_alg_metaring` for ring/kangaring/RDH, `enc_alg_mesh` for mesh — and its per-step generators call `encd_*` entry points that author the device artifacts. `encd` is the lowering target; it does not decide ring order or mesh peering (the composer already did), it materializes those decisions as `spad_ctrl` op-stream entries and `vring` SDMA descriptors.

```text
        HOST COMPOSER  (enc/enc.cc — enc_* / enc_primitive*)
        ────────────────────────────────────────────────────
        enc_post_operation → family composer → per-step leaves
          enc_primitive::mark_end            enc_primitive_mesh::sync
          enc_metaring_primitive::__compose_*  ::prefetch_metadata
                          │  (calls DOWN — one-directional)
                          ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │           encd DEVICE-RESIDENT EMITTER  (tdrv/encd.c)             │
   │                                                                  │
   │   SPAD op-stream                  SDMA DMA packets                │
   │   ───────────────                 ───────────────                │
   │   encd_dma_mark_end (0x237200)    add_dma_packet     (0x22ff20)   │
   │     create_spad_ctrl_entry          (M2S/S2M copy)               │
   │       (0x232cd0)                  add_dma_packet_cce (0x2307d0)   │
   │     mark_spad_slot_final_entry      (reduce / FMA)               │
   │       (0x22fc20)                  ── sized by ──                  │
   │   ── per-engine SPAD section ──   encd_get_dma_copy_packet_size  │
   │   spad_section_t (grow/pad)         (0x238bc0)                   │
   │                                   encd_get_cce_reduce_packet_size │
   │   TopSP channel config              (0x2396f0)                   │
   │   prep_metaring_topsp_config      ── routed by ──                │
   │     (0x2331d0)                    set_addr_routing_bits (0x231340)│
   │   sema alloc/free                 set_addr_forward_bit  (0x231460)│
   │     alloc_sp_semaphores (0x234aa0)                               │
   │                                                                  │
   │            encd_context (624 B)  — per-NC, parameterizes both    │
   └──────────────────────────────────────────────────────────────────┘
                          │  (vring sinks — al_udma HAL)
                          ▼
        vring_add_dma_packet_v2 (0x312440)  ─┐
        vring_add_dma_packet_cce (0x3134e0) ─┤→  per-channel virtual ring
        vring_add_desc_transfer  (0x312290) ─┘   (tx/rx 16-byte UDMA descs)
                          │  (dump at load)
                          ▼
        vring_dump_to_pring  →  physical ring (pring)  →  device DRAM
```

> **NOTE —** the SPAD op-stream and the SDMA descriptor stream are **two different device artifacts with two different consumers**. The SPAD stream is walked by the on-device top-SP firmware (the sync core that sequences the collective); the SDMA stream is consumed by the al_udma DMA engines the firmware triggers. `encd` authors both, but they are not interchangeable — the op-stream carries *control* (which channels run, when to wait/post a semaphore, completion marks), the descriptor stream carries *data movement*. Conflating them mis-models the device. The op-stream's byte layout is owned by [The cc_op_entry On-Device ISA](cc-op-isa.md); the descriptor's by [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md).

### 1.1 What lives where — the symbol clusters

The `encd_*` floor partitions cleanly into functional clusters, each owned by one of the four sub-pages. This map names the cluster and its representative entry points; the sub-page derives the algorithm.

| Cluster | Representative entry points | Owning page |
|---|---|---|
| SPAD op-stream emit + completion mark | `encd_dma_mark_end @0x237200`, `create_spad_ctrl_entry @0x232cd0`, `mark_spad_slot_final_entry @0x22fc20`, `encd_spad_section_adjust_size_padding @0x236e60` | this page (§3) + [DMA & devmem Floor](encd-dma-devmem.md) |
| SDMA descriptor pack (copy + CCE) | `add_dma_packet @0x22ff20`, `add_dma_packet_cce @0x2307d0`, `add_dma_packet_profile_info @0x230420`, `__encd_dma_copy @0x238c80` | [DMA & devmem Floor](encd-dma-devmem.md) |
| Top-level DMA section staging (NEFF load) | `encd_dma_toplevel_section_descs_init @0x2304d0`, `…_push_back @0x22fdd0`, `…_desc_section_commit @0x2301f0`, `load_dma_queue_set @0x231dc0` | [DMA & devmem Floor](encd-dma-devmem.md) |
| devmem load-via-DMA | `encd_devmem_load_with_dma_add_descriptor @0x230a50`, `…_cleanup @0x230970` | [DMA & devmem Floor](encd-dma-devmem.md) |
| Semaphore pool + addressing | `alloc_sp_semaphores @0x234aa0`, `free_channel_semaphores @0x232870`, `free_mesh_resources @0x2326f0`, `encd_get_sema_addr_by_id @0x230710`, `alloc_sema_value @0x231580`, `encd_get_event_sema_iaddr @0x238590` | [Sema & TopSP Bring-Up](encd-sema-topsp.md) |
| TopSP / NCFW firmware bring-up | `encd_ncfw_configure_device_init @0x230c70`, `encd_get_coretype @0x230be0`, `prep_metaring_topsp_config @0x2331d0` | [Sema & TopSP Bring-Up](encd-sema-topsp.md) |
| Per-arch CSR / topology dispatch | `encd_arch_*` thunks (`encd_get_rid @0x235000`, `get_p2p_port_mesh_topo @0x22faa0`, `set_addr_routing_bits @0x231340`, `set_addr_forward_bit @0x231460`) | [Per-Arch Ops Dispatch](encd-arch-ops.md) |
| Metaring channel-activation state | `encd_ring_set_active_channels_cnt @0x237100`, `encd_ring_is_channel_active @0x2371c0`, `encd_set_kangaring_active_channel_n @0x2370c0`, `encd_hybrid_ring_is_dev_use_gateway @0x238190` | this page (§4) |

> **GOTCHA —** the floor is **not** a single source file's worth of contiguous logic — its `.text` spans several bands (`0x22f8e0…0x2397ab` and the load/lifecycle range `0x2431c0…0x251eb0`), and a handful of in-band functions are GCC cold-split `.part.0` `__noreturn` assert landing pads with no logic of their own (`get_sp_engine_idx.part.0 @0x2312b0`, `get_dma_queue_set.part.0 @0x2312e0`, `encd_mesh_find_dst_idx.part.0 @0x2313d0`). A reimplementer reading the disassembly band-by-band should treat the `.part.0` stubs as `assert()` tails of their hot parents, not as distinct functions.

---

## 2. The `encd_context` object

### Purpose

`encd_context` is the per-NeuronCore driver context every public-API caller actually holds, and the object that parameterizes both descriptor streams. It is the *small* driver-side companion to the heavyweight host-side `enc_context` (6.7 MB, the parse/exec state machine documented on [Engine Core](engine-core.md)), reached from it via `enc_ctx @+288`. Where `enc_context` holds the posted-op program being compiled, `encd_context` holds the device resources the emit consumes: the virtual core, the stream/SP-engine tables, the per-priority packet-size knobs, and the gating flags (`ccop_owner`, `enable_hw_barrier`, `ctx_id`).

### Layout

The members below are the ones the emitter floor reads, with offsets from `structures.json` cross-checked against the decompiled field accesses across the three `encd` cells (L-ENC-10/11/13). Confidence **HIGH** for every offset confirmed by a live access; **MED** where named from `structures.json` but not read by a function this map cites.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `vcore` | +0 | `const virtual_core_t*` | `->nec_dev_id` is the `"nec_dev %u"` in every emitter log line | HIGH |
| `model_allocator` | +8 | `dmem_allocator_t*` | HBM allocator for prings and devmem-load staging | HIGH |
| `ccop_owner` | +16 | `bool` | this context owns the collective resources — guard on most emit entry points | HIGH |
| `reduce_slice_sz_n` | +24 | `int` | CCE-reduce slice width; caps `encd_get_cce_reduce_packet_size` | HIGH |
| `max_desc_per_packet` | +32 | `size_t` | UDMA packetization bound read by `add_dma_packet` | HIGH |
| `dma_qsets` | +48 | `encd_dma_queue_set_t` | `qset[8]` of per-tpb DMA-queue sets | HIGH |
| `sp_engines` / `sp_engine_n` | +136 / +128 | `encd_sp_engine*` / `int` | the TopSP engines this NC drives | HIGH |
| `streams` / `streams_n` | +152 / +144 | `encd_stream*` / `int` | per-stream SP-engine + comm tables | HIGH |
| `config` | +160 | `encd_config` (112 B) | debug knobs (`dbg_multi_stream_mode @+60`, `dbg_cc_nop @+104`) | HIGH |
| `curr_priority_class` | +272 | `uint8_t` | per-op priority; indexes the gconf packet-size tables (`<= 4`) | HIGH |
| `permute_chain` / `unique_tensors` | +273 / +274 | `bool` | per-op composer flags set by `enc_post_operation` | HIGH |
| `enc_ctx` | +288 | `void*` (`enc_context*`) | back-edge to the host composer state | HIGH |
| `replica_groups_intialized` | +428 | `bool` | replica-group parse latch | MED |
| `topsp_init_dma` | +432 | `encd_devmem_load_with_dma_t` (48 B) | the devmem load-via-DMA scratch | HIGH |
| `basic_block_n` | +488 | `uint32_t` | function/basic-block count, gates static-ring placement | HIGH |
| `functions` | +496 | `encd_function_info_t*` | per-function padded/unrolled flags | HIGH |
| `enable_hw_barrier` | +560 | `bool` | gates barrier/sema reservation | HIGH |
| `ctx_id` | +561 | `uint8_t` | `== 1` ⇒ host-CC / standalone user comm; `0` ⇒ NEFF-loaded comm | HIGH |

> **QUIRK —** throughout the IDA decompile, the per-NC context is reached through the DMA channel via the nonsense expression `channel->comm[1].ring.channels[21].tp_inc_steps[81096].mark`, and the per-NC stream via `…tp_inc_steps[81095].mark`. Disassembly of `encd_get_cce_reduce_packet_size @0x2396f4` shows these are `mov r12, [comm + 0x3011F4C0]` — a *fixed byte offset* into `encd_comm` that holds the `ctx` (`encd_context*`) and `strm` (`encd_stream*`) pointers; the assert strings `"channel->comm->ctx->…"` and `"channel->comm && channel->comm->strm"` confirm it. A reimplementer must read every `comm[1]…mark` as `comm->ctx` and every `…[81095].mark` as `comm->strm`; the array-flatten is an IDA model artifact, not real indexing. *(field identity HIGH; the exact intermediate byte offset MED — Phase-2 D4.)*

### Setters / getters

A small block of per-context accessors lets the composer stamp op-scoped flags onto the context before the emit reads them. They are trivial guarded leaves (`assert(ctx)` then a single store/load), but they pin the offsets the emit consumes:

| Accessor | Address | Field | Offset | Confidence |
|---|---|---|---|---|
| `encd_set_ctx_curr_priority_class` | `0x238b00` | `curr_priority_class` | +272 | HIGH |
| `encd_set_ctx_permute_chain` | `0x238b30` | `permute_chain` | +273 | HIGH |
| `encd_set_ctx_unique_tensors` | `0x238b60` | `unique_tensors` | +274 | HIGH |
| `encd_get_ctx_curr_priority_class` | `0x238b90` | `curr_priority_class` | +272 | HIGH |

`encd_get_ctx_curr_priority_class` is the one consumed by both packet-size functions — they read `curr_priority_class` to index the per-priority gconf size tables (`cc_dma_copy_prio_pkt_size[5]` / `cc_cce_reduce_prio_pkt_size[5]`, `MAX_DMA_PKT_PRIO_NUM = 4`).

---

## 3. The SPAD op-stream emit (`encd_dma_mark_end`)

### Purpose

`encd_dma_mark_end @0x237200` is the funnel that closes out one collective DMA op into the TopSP scratch-pad op-stream. It is reached from the composer leaves `enc_primitive::mark_end()` (ring) and `enc_primitive_mesh::end_context()` (mesh), and it is the single largest function in the emitter floor (3983 B, 121 basic blocks) because it inlines seven `encd.c` SPAD helpers. This map pins its *shape* and its place in the flow; the byte-exact SPAD descriptor state machine and the `spad_ctrl_entry` bit-field are owned by the deep sibling — only the hand-off is documented here.

### Algorithm — the emit shape

```c
// encd_dma_mark_end @0x237200 — close out one op into the TopSP SPAD op-stream
function encd_dma_mark_end(channel, op_idx, func_rel_op_idx, compl_assert_addr[],
                           num_compl_assert_addr, mark_first, mark_continue,
                           sema_shift_offset, sema_mask, function_id, active_channel_n):
    ctx  = channel->comm->ctx;                          // encd_context* (comm+0x3011F4C0)
    strm = channel->comm->strm;                         // encd_stream*
    assert(ctx->sg_count == num_compl_assert_addr);     // "channel->comm->ctx->sg_count == …"
    spe  = channel->sp_engine;                          // the owning TopSP engine

    // (1) CTRL entry — the op-enable control word carrying the cc_op_entry ISA
    create_spad_ctrl_entry(...)                         // 0x232cd0 — packs spad_ctrl {header; cc_op_entry}
        // alg/subalg/trignext/chlist/reporter/sema_shift_offset/sema_mask/safe_mode bitfield
    encd_spad_add_ctrl_entry(spe, ...)                  // append; assert "SPAD control block overflow"

    // (2) SLOT entries — copy the channel's accumulated tp_inc_steps into the SPAD slot region
    for step in channel->tp_inc_steps[0 .. tp_inc_step_n]:   // +2536 .. +2099688
        encd_spad_add_slot_entries(spe, step)          // "TOP_SP #%d SPAD slot%d overflow" guard

    // (3) FINAL entry — CONTINUE mark or END mark with completion-assert semaphore bits
    mark_spad_slot_final_entry(channel, op_idx, mark_continue, compl_assert_addr, ...)  // 0x22fc20
        // CONTINUE -> SPAD_SLOT_CONTINUE_MARK (0xFFFEFFFE00000000)
        // END      -> completion addr + semaphore bits; assert sema bits equal across compl addrs

    // (4) pad TopSPs that did NOT participate with NOP CTRL entries (keep all engines in lockstep)
    encd_spad_add_nop_entries_to_unused_topsps(strm, spe, ...)   // "(strm->sp_engines[0]==spe) || …"

    spe->op_cnt++; spe->total_op_cnt++;                 // bump op/report counters
    return NRT_SUCCESS;
```

The four-stage structure is the whole contract: a control word per op, a run of slot steps, a terminating mark (CONTINUE chains to the next op's program; END carries the completion semaphore), and a NOP-pad so every top-SP advances the same number of control entries even when only a subset of channels participate. The SPAD section that holds these entries is a growable array — `encd_spad_section_adjust_size_padding @0x236e60` doubles its capacity and zero-fills the tail when the op-stream outgrows it.

> **GOTCHA —** the CONTINUE / END distinction is the op-stream's chaining protocol, not a data field. A non-terminal op writes `SPAD_SLOT_CONTINUE_MARK` (`0xFFFEFFFE00000000`) as its final slot entry, and the *next* op's `encd_dma_mark_end` asserts `prev_final_entry->slot_entry.mark == SPAD_SLOT_CONTINUE_MARK` before overwriting it with its own steps; the terminal op writes the END mark with the completion-assert semaphore bits. A reimplementer who emits an END mark mid-chain breaks the top-SP's walk; one who never emits END leaves the collective without a completion signal. The full mark protocol and `spad_ctrl_entry` bitfield are [The cc_op_entry On-Device ISA](cc-op-isa.md).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_dma_mark_end` | `0x237200` | the op-stream funnel; CTRL + SLOT + FINAL + NOP-pad (inlines 7 helpers) | HIGH |
| `create_spad_ctrl_entry` | `0x232cd0` | pack one `spad_ctrl` control word (`{header; cc_op_entry}`) | HIGH |
| `mark_spad_slot_final_entry` | `0x22fc20` | write CONTINUE / END terminating slot entry | HIGH |
| `encd_spad_section_adjust_size_padding` | `0x236e60` | grow + zero-pad the per-engine SPAD section | HIGH |
| `encd_function_execution_queue_update_all` | `0x236f90` | push a function-id onto every stream/SP-engine exec queue (load-time) | HIGH |
| `encd_function_execution_queue_push_back` | `0x236f00` | one-engine exec-queue append (realloc-on-full) | HIGH |

---

## 4. Metaring channel-activation state

### Purpose

A ring/kangaring collective runs over a subset of the up-to-32 channels a metaring owns; the composer decides how many channels are *active* for a given op, and the emitter records that decision as per-channel `is_active` flags the op-stream reads. These accessors are the binding between the composer's channel count and the device's per-channel gating — small guarded leaves, but the authoritative witness for the `encd_alg_metaring` channel-state layout.

### Algorithm — the active-channel set

```c
// encd_ring_set_active_channels_cnt @0x237100 — set the active-channel count + per-channel flag
function encd_ring_set_active_channels_cnt(metaring, active_channels_n):
    assert(active_channels_n <= metaring->channel_n);                  // +84 vs +80
    assert(metaring->is_hybrid_ring ||                                 // hybrid may run a subset
           metaring->channel_n == active_channels_n);                  // non-hybrid: all-or-nothing
    metaring->active_channel_n = active_channels_n;                    // +84
    for ch in metaring->channels[0 .. channel_n]:                      // channels[32] @+96, stride 2099736
        ch.is_active = (ch.id < active_channels_n);                    // channel +9
```

> **QUIRK —** for a **non-hybrid** ring the active count is all-or-nothing — the assert `is_hybrid_ring || channel_n == active_channel_n` rejects any partial activation. Only a *hybrid* ring (`is_hybrid_ring` set) may run a strict subset of its channels, which is why the gateway-routing predicates (`encd_hybrid_ring_is_dev_use_gateway @0x238190`, `…_is_next_neigh_use_gateway @0x238250`) exist only on the hybrid path. A reimplementer who allows partial activation on a flat ring will desynchronize the ring walk.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_ring_set_active_channels_cnt` | `0x237100` | set `active_channel_n` (+84) and per-channel `is_active` | HIGH |
| `encd_ring_is_channel_active` | `0x2371c0` | read `channels[cid].is_active` (channel +9) | HIGH |
| `encd_set_kangaring_active_channel_n` | `0x2370c0` | set `kangaring_active_channel_n` (+88) | HIGH |
| `encd_get_kangaring_active_channel_n` | `0x2370f0` | read `kangaring_active_channel_n` (+88) | HIGH |
| `encd_hybrid_ring_is_dev_use_gateway` | `0x238190` | hybrid-ring: is dev a direct port to its PREV ring neighbor? | HIGH |
| `encd_hybrid_ring_is_next_neigh_use_gateway` | `0x238250` | hybrid-ring: does the NEXT neighbor reach via the gateway? | HIGH |

---

## 5. The four encd sub-pages

The emitter floor is deep enough that its three substantive concerns — DMA descriptor authoring, semaphore + TopSP bring-up, and the per-arch CSR dispatch underneath both — each get a dedicated page. This map and the three deep pages together cover the floor; the table below routes a reimplementer to the page that owns each artifact.

| Page | Owns | Key entry points |
|---|---|---|
| **This page** — [encd: Device-Resident Descriptor Emitter](encd-overview.md) | the map, the `encd_context`, the SPAD op-stream hand-off, channel-activation state | `encd_dma_mark_end @0x237200`, `encd_ring_set_active_channels_cnt @0x237100` |
| [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md) | the SDMA packers (copy + CCE), top-level DMA section staging, devmem load-via-DMA, packet sizing, `__encd_dma_copy` folding/slicing | `add_dma_packet @0x22ff20`, `add_dma_packet_cce @0x2307d0`, `__encd_dma_copy @0x238c80`, `load_dma_queue_set @0x231dc0`, `encd_get_dma_copy_packet_size @0x238bc0` |
| [encd: Semaphore and TopSP Bring-Up](encd-sema-topsp.md) | TopSP semaphore pool alloc/free, sema SOC-address resolution, NCFW firmware bring-up, the `prep_metaring_topsp_config` channel populator | `alloc_sp_semaphores @0x234aa0`, `encd_get_sema_addr_by_id @0x230710`, `encd_ncfw_configure_device_init @0x230c70`, `prep_metaring_topsp_config @0x2331d0` |
| [encd: Per-Arch Ops Dispatch](encd-arch-ops.md) | the `encd_arch_*` per-generation CSR/topology dispatch underneath the floor — RID/MLA mapping, port resolution, address-routing-bit encoding | `get_p2p_port_mesh_topo @0x22faa0`, `set_addr_routing_bits @0x231340`, `set_addr_forward_bit @0x231460`, `encd_get_rid @0x235000` |

### The vring sink boundary

The SDMA half of the floor terminates at the al_udma/vring HAL — a separate TU (`tdrv/vring.c`) owned by [Virtual Rings (vring)](../dma/virtual-rings.md). The `encd` packers hand a built descriptor list to one of three sinks, and the boundary is one-directional (the vring layer never calls back into `encd`):

| `encd` packer | vring sink | Carries |
|---|---|---|
| `add_dma_packet @0x22ff20` | `vring_add_dma_packet_v2 @0x312440` | M2S/S2M copy packet (FIRST…LAST run) |
| `add_dma_packet_cce @0x2307d0` | `vring_add_dma_packet_cce @0x3134e0` | CCE reduce packet (ADD/MIN/MAX/FMA) |
| `encd_devmem_load_with_dma_add_descriptor @0x230a50` | `vring_add_desc_transfer @0x312290` | a single tensor-stage tx/rx copy pair |

> **NOTE —** the descriptor stream is authored into a vring (editable host memory), packed/barriered, and only *dumped* into a physical ring (`pring`) at load time — `encd` is one of the two producer families that fill vrings (the other is the executor's pseudo-DMA translator). The devmem-load path emits *template* `buf_ptr`s (symbolic `var_id` tags) that the vring template pass resolves to physical addresses per execution; that encoding is owned by [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md) and [Virtual Rings §5](../dma/virtual-rings.md). The vring → pring dump and the descriptor cap are [Virtual Rings §6](../dma/virtual-rings.md).

---

## 6. Verification notes

> The emitter-floor map, the `encd_context` layout, the SPAD op-stream hand-off, and the channel-activation accessors were cross-checked against the IDA artifacts of `libnrt.so` 2.31.24.0:
> - Every `encd_*` address on this page resolves in `function_addresses.json` (`encd_dma_mark_end @0x237200`, `create_spad_ctrl_entry @0x232cd0`, `mark_spad_slot_final_entry @0x22fc20`, `add_dma_packet @0x22ff20`, `add_dma_packet_cce @0x2307d0`, `prep_metaring_topsp_config @0x2331d0`, `alloc_sp_semaphores @0x234aa0`, `encd_get_sema_addr_by_id @0x230710`, `encd_ncfw_configure_device_init @0x230c70`, `encd_ring_set_active_channels_cnt @0x237100`, `encd_get_dma_copy_packet_size @0x238bc0`, `encd_get_cce_reduce_packet_size @0x2396f0`, `set_addr_routing_bits @0x231340`, `set_addr_forward_bit @0x231460`) — all `local` (`t`) symbols, all `addr2line`'d to `tdrv/encd.c`.
> - The vring sinks (`vring_add_dma_packet_v2 @0x312440`, `vring_add_dma_packet_cce @0x3134e0`, `vring_add_desc_transfer @0x312290`) resolve to `tdrv/vring.c` — a distinct TU, the al_udma HAL boundary.
> - The **zero libnccom/`nccl*`/`neuron*`/`nec_*` edges** claim is from a callgraph out-edge sweep over all `encd_*` functions in the three cells (L-ENC-10/11/13); every callee is `encd_arch_*`, a `vring_*`/`dmem_*` DMA helper, or libc. The NCCL boundary is reached only by the composer/bootstrap layer (`enc_setup_global_comm_internal` → `ncclInitGlobalComm`), documented on [The libnrt ↔ libnccom Boundary](nccl-boundary.md).
> - Struct offsets (`encd_context` 624 B, `encd_dma_channel` 2 099 736 B, `encd_alg_metaring` channel block, `encd_sp_engine`) are from `structures.json`, consistent with the decompiled field accesses.
>
> **[MED]** The `comm->ctx` / `comm->strm` byte offset (`0x3011F4C0`) inside `encd_comm` is disasm-confirmed at the `encd_get_cce_reduce_packet_size` call site but the exact `encd_comm` field layout (the IDA `comm[1]…mark` artifact) is model-derived elsewhere; field *identity* is HIGH, the intermediate path is a Phase-2 item.
> **[MED]** `encd_dma_mark_end`'s inlined-helper boundaries (`prep_spad_ctrl_entry` / `encd_spad_add_*` / `create_spad_ctrl_entry`) are attributed by DWARF line ranges (0xAB4–0xCD7); the exact `spad_ctrl_entry` bitfield and the SPAD slot state machine are owned by [The cc_op_entry On-Device ISA](cc-op-isa.md), not pinned bit-by-bit on this map.

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_post_operation` (`@0x11f790`, `enc.cc`) | the composer dispatcher whose family composers call down into the `encd` floor |
| `enc_primitive::mark_end` / `enc_primitive_mesh::end_context` | the composer leaves that drive `encd_dma_mark_end` |
| `encd_dma_mark_end` (`@0x237200`) | the SPAD op-stream funnel this page maps |
| `add_dma_packet` (`@0x22ff20`) / `add_dma_packet_cce` (`@0x2307d0`) | the SDMA descriptor packers (deep page: encd-dma-devmem) |
| `prep_metaring_topsp_config` (`@0x2331d0`) | the ring channel-config populator (deep page: channel-descriptor / encd-sema-topsp) |
| `vring_add_dma_packet_v2` (`@0x312440`) | the al_udma vring sink the copy packer hands off to |

## Cross-References

- [Overview: the Collective-Compute Architecture](overview.md) — the two-layer composer→emitter split and the end-to-end flow this floor terminates
- [Engine Core (enc_context and Accessors)](engine-core.md) — the host composer state (`enc_context`) this `encd_context` back-references at `+288`
- [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md) — the SDMA copy/CCE packers, top-level section staging, devmem load-via-DMA, and packet sizing
- [encd: Semaphore and TopSP Bring-Up](encd-sema-topsp.md) — TopSP semaphore alloc/free, sema SOC-address resolution, and NCFW firmware bring-up
- [encd: Per-Arch Ops Dispatch](encd-arch-ops.md) — the `encd_arch_*` per-generation CSR/topology dispatch underneath the floor
- [Virtual Rings (vring) and Packet Builders](../dma/virtual-rings.md) — the al_udma vring the SDMA packers author into, and the vring → pring dump
- [back to index](../index.md)
