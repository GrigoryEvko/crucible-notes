# Overview: the Collective-Compute Architecture

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present, and the host-side composer source TU is `/opt/workspace/KaenaRuntime/enc/enc.cc`. The device-resident emitter TU is `tdrv/encd.c`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Firmware-consumer addresses apply to `libncfw.so` (build-id `a98f8e1c…`). Other versions will differ.*
> *Evidence grade: **Confirmed (symbol- and switch-anchored)** — the 13-case dispatch and the algorithm roster are read from the IDA-recovered jump tables; every enum is verbatim from `enums.json`; the two-layer substrate split is reconciled against `enc.cc` and `encd.c` symbols. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

A NeuronCore runs a collective — `all-reduce`, `all-gather`, `reduce-scatter`, `all-to-all`, `collective-permute`, point-to-point `send`/`recv` — not by calling into a monolithic NCCL, but by **compiling the collective into an on-device program at model-load time** and letting the NeuronCore's sync core execute that program with no host in the loop. This page is the map of how that compilation works. The architecture is two layers stacked on the familiar NCCL decomposition: a host-side **composer** layer (`enc/enc.cc`) that turns a parsed NEFF replica group into a per-NeuronCore schedule of ring / mesh / hierarchical / RDH "events", and a device-resident **emitter** layer (`tdrv/encd.c`, the `encd` driver) that lowers each event into the wire artifacts the silicon actually consumes: TopSP scratchpad-control (`spad_ctrl`) entries carrying the `cc_op_entry` ISA, plus SDMA descriptor streams packed into per-channel virtual rings (`vring`).

The reference frame is standard collective theory. The ring family is the textbook NCCL ring: an all-reduce is `(nranks-1)` reduce-scatter steps followed by `(nranks-1)` all-gather steps, half-chunk pipelined; the mesh family is a fully-peered `N·(N-1)` exchange that the ring cannot express; the hierarchical and RDH families layer intra-node and inter-pod phases on top. What is Neuron-specific is *where* the schedule lives. libnccom (the NCCL math fork, reached over a `dlsym`'d `neuron*` ABI) decides only the host-side ring **order** (prev/next/userRanks); libnrt **re-derives** the device neighbor order from the physical MLA topology and emits the step program, so the device-side ring loop, the mesh event graph, and the `cc_op_entry` byte stream all live in libnrt, not in the NCCL fork.

The central dispatch is `enc_post_operation @0x11f790` (`enc.cc`, 21 KB): per posted op it runs a six-way feasibility battery, resolves one `enc_alg_type` by a fixed precedence, and switches the 13-value `enc_op_type` into a per-op-type handler that builds an `enc_operation` and posts it. Each algorithm family then has its own composer — and those composers, the cost-free op leaves, the channel descriptor, and the `cc_op_entry` byte layout are all **sibling pages**. This page documents (1) the two-layer substrate split, (2) the end-to-end flow from `enc_post_operation` through the algorithm dispatch to the `encd` emit, and (3) the collective op family and its algorithm roster — and it links the siblings rather than re-deriving their byte layouts.

For reimplementation, the map-level contract is:

- **The two layers and their hand-off** — the host composer produces an algorithm-family schedule object (`enc_alg_metaring` for ring, `enc_alg_mesh` for mesh, …); the `encd` driver lowers that into `spad_ctrl` / `cc_op_entry` + `vring` SDMA descriptors. The hand-off is the `encd_*` boundary.
- **The two algorithm axes, never conflated** — the host `enc_alg_type` (12 values; picks the composer) is *not* the 4-bit device `enc_pattern_t` (RING=0 / MESH=1 / INVALID=2) that ends up in the wire descriptor. The composer collapses 12 host algorithms onto a binary device pattern.
- **The dispatch shape** — `enc_post_operation`'s `enc_can_post_*` battery → a fixed alg precedence → the 13-case `enc_op_type` jump table → the family composer → the `encd` emit.

| | |
|---|---|
| **Central dispatcher** | `enc_post_operation @0x11f790` (`enc.cc`, 21252 B) — 13-case `enc_op_type` switch |
| **Op-dispatch jump table** | switch @`0x12038a`, 13 cases, default `0x1213c0` (`switches.json`) |
| **Build-time family selector** | `enc_init_comm @0x135d60` → `enc_cc_algorithm_allowed @0x108d30` (per-`enc_alg_type` gate) |
| **Algorithm roster** | `enc_get_algorithm_name @0xfef30` — 11-case switch (`enc_alg_type` 0..10) |
| **Host alg axis** | `enc_alg_type` — 12 values (`RING=0 … INVALID=11`) |
| **Device pattern axis** | `enc_pattern_t` — `RING=0`, `MESH=1`, `INVALID=2` (the `cc_op_entry.algo_type` field) |
| **Op family** | `enc_op_type` — 13 valid (`ALLGATHER=0 … ALLTOALL_V=12`), `OP_N=13` |
| **Device emitter** | `encd` (`tdrv/encd.c`) → `create_spad_ctrl_entry @0x232cd0`, `vring_pack_descs @0x312590` |
| **Multi-node boundary** | libnccom (NCCL fork) via `dlsym`'d `neuron*` ABI (`ncclInitGlobalComm @0x1c0c30`, …) |

---

## 1. The two execution layers

The collective stack is realized in two layers that share the parsed replica-group topology but differ in who builds the schedule and what artifact each produces. Both run inside libnrt; the boundary between them is the `encd_*` symbol family.

### 1.1 The composer layer (host, `enc/enc.cc`)

The composer turns a parsed NEFF replica group into an algorithm-specific, per-NeuronCore schedule object. It is reached two ways:

- **At communicator-init time**, `enc_init_comm @0x135d60` fixes the *family* for each comm (INTRA / INTER) by polling `enc_cc_algorithm_allowed @0x108d30` for each candidate `enc_alg_type`. If any MESH-family algorithm is allowed it takes the mesh path (`alg_mesh_init @0x135990`); otherwise it falls to RING / KANGARING (`alg_ring_init`/`alg_kangaring_init`). RDH is gated *on* mesh — the string `"Detected usage of RDH while mesh is not allowed. RDH construction will be skipped because it depends on Mesh."` records the dependency.
- **At op-post time**, `enc_post_operation @0x11f790` re-resolves a concrete `enc_alg_type` per op via the `enc_can_post_*` battery (§3) and builds the schedule for that op into the family object.

The family schedule objects are distinct types, one per composer:

| Family | Schedule object | Composer entry | Owning page |
|---|---|---|---|
| Ring / Kangaring / Single-cycle / RDH | `enc_alg_metaring` (37544 B; `ring_ranks[32]` @+2824) | `enc_metaring_primitive::compose_operation @0x178170` | [Ring Scheduling Math](ring-scheduling.md) |
| Mesh (full / grouped / Trn2-pd / Switch) | `enc_alg_mesh` (61568 B; `mesh_subtype[20]` @+1136) | `alg_mesh_build_subtypes @0x133cd0` | [Mesh Composer](mesh-composer.md) |
| Hierarchical / RDH | (metaring + per-phase) | `enc_can_post_hierarchical_operation` / RDH builders | [Hierarchical and RDH](hierarchical-rdh.md) |

Each family composer drives the per-op step generators — the ring step loop (`send` / `recv_reduce_send` / `direct_recv_send` / …) or the mesh event composers (handshake / reduce-copy / reduce-write / broadcast / sync) — and those generators call the `enc_primitive` leaves that *produce* the device artifacts. The leaves are their own page: [enc Primitives](enc-primitives.md).

### 1.2 The emitter layer (device-resident, `tdrv/encd.c`)

The `encd` driver is the lowering target. The composer's step/event generators do not write wire bytes directly; they call `encd_*` entry points that build two device-resident artifact streams:

- **TopSP scratchpad-control (`spad_ctrl`) entries** carrying the `cc_op_entry` collective-op ISA. `create_spad_ctrl_entry @0x232cd0` packs the 8-byte `spad_ctrl_entry` (a 1-byte op-enable header + the 7-byte `cc_op_entry`); the op-stream funnel `encd_dma_mark_end @0x237200` stages each op's slot. The byte-exact layout of that descriptor is [The cc_op_entry On-Device ISA](cc-op-isa.md).
- **SDMA descriptor streams in per-channel virtual rings (`vring`)**. `vring_pack_descs @0x312590` appends 16-byte UDMA descriptors to a virtual ring; `vring_dump_to_pring_descriptors_padded @0x311f10` and `dma_ring_create_prings_from_vring @0x22e310` flush the vring into the physical ring (`pring`) the hardware reads. The descriptor itself is [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md).

A third `encd` output is the per-channel TopSP configuration (`prep_metaring_topsp_config @0x2331d0` for ring channels, the mesh analog for mesh), which binds the channel's SP engine, semaphores, and ring state before the op stream runs. These three encd outputs are detailed across [encd: Device-Side Descriptor Emitter](encd-overview.md), [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md), and [encd: Semaphore and TopSP Bring-Up](encd-sema-topsp.md).

> **GOTCHA —** the two layers carry **two different algorithm enumerations**, and conflating them mis-sizes the wire descriptor. The composer dispatches on the 12-value host `enc_alg_type` (Ring / Hier / Mesh / Kangaring / RDH / …); the emitter packs only the **4-bit** device `enc_pattern_t` (`RING=0`, `MESH=1`, `INVALID=2`) into `cc_op_entry.algo_type`. A reimplementation that tries to pack `enc_alg_type` (which reaches 11) into the 4-bit field will overflow it. The composer is where 12 host algorithms collapse to the binary device pattern. See [The cc_op_entry On-Device ISA §4](cc-op-isa.md) for the two-axis split.

### 1.3 Shared substrate: the replica-group topology

Both layers read the same parsed topology. `enc_parse_replica_groups @0x11e850` walks the NEFF `kbin_replica_group_set_t` into `enc_context.replica_groups` (@+840) and `src_target_pairs` (@+816), validating device-id range/uniqueness and that the local rank is present (`"replica groups (%u/%u) does not have myself %u"`). The per-rank `enc_comm_info` (rank, rank_n, node_n, `peers[]`) is the view every composer reads, and the per-op `enc_op_args` (248-byte stride) is what each op carries. For multi-node, the global communicator is bootstrapped through libnccom (`enc_setup_global_comm_internal @0x10b050` → `ncclInitGlobalComm`); the topology parse and the libnrt↔libnccom boundary are [Comm Context and Bootstrap](comm-context.md) and [The libnrt ↔ libnccom Boundary](nccl-boundary.md).

A per-rank op **signature** (16-byte MD5 over the group's `enc_op_args`, written to `enc_context+992` by `enc_calculate_signature @0x108220`) is computed in the PARSE state for cross-rank reconciliation; it is a consistency check, not part of the emitted program.

---

## 2. End-to-end flow

The lowering of one collective op, from a posted `enc_op_list` to the device-resident artifact streams, proceeds through the stages below. Stage [3] is the heart of `enc_post_operation`; stages [5]–[7] fork by the resolved algorithm family.

```text
posted op  (enc_fnc::post_instr -> enc_post_function -> enc_op_list::post)
        |
   [1]  enc_post_operation @0x11f790
        |   validate (enc_validate_operation); SB2SB / 2D / multi-node guards
        |   read group_id, stream_id, priority_class, data_type, total_size_n
        |
   [2]  FEASIBILITY BATTERY  (enc_can_post_* — six gates, all run)
        |   kangaring | mesh | intra_rdh | inter_rdh | single_cycle_ring | hierarchical
        |
   [3]  ALGORITHM PRECEDENCE  (resolve one enc_alg_type v91; enc.cc ~0xE4F)
        |   HIER > INTRA_RDH > MESH > KANGARING > SINGLE_CYCLE_RING > INTER_RDH > RING
        |
   [4]  13-CASE enc_op_type DISPATCH  (jump table @0x12038a, default 0x1213c0)
        |   ALLGATHER(0) ALLREDUCE(1) REDUCE_SCATTER(4) SEND(5) RECV(6)
        |   ALLTOALL(7) PERMUTE(8/10) PERMUTE_REDUCE(9/11) ALLTOALL_V(12)
        |   BROADCAST(2)/REDUCE(3) -> unsupported branch (0x1213c0)
        |   ALLTOALL(7)/ALLTOALL_V(12) assert alg == ENC_ALG_MESH
        |
   [5]  BUILD enc_operation  (248 B: {alg, dtype, channel_n, priority_class,
        |   copy/reduce_slice_sz, pipeline slice_n}); op_slot_idx from curr_op_slot_idx[stream]
        |   -> enc_context::post_op @0x10c750  (posted_ops[stream] / all_posted_ops)
        |
   [6]  FAMILY COMPOSE  (per resolved enc_alg_type)
        |     RING/KANGARING/SINGLE_CYCLE/RDH -> enc_metaring_primitive::compose_operation
        |       -> __compose_channel_ring @0x177340 (op_type jump table @0x857e8c)
        |          -> enc_primitive::{send,recv_reduce_send,direct_recv_send,...}
        |     MESH                            -> alg_mesh_build_subtypes @0x133cd0
        |       -> alg_full_mesh_* event composers -> encd_init_mesh_event
        |
   [7]  encd EMIT  (tdrv/encd.c)
        |     create_spad_ctrl_entry @0x232cd0 -> spad_ctrl / cc_op_entry stream
        |     vring_pack_descs @0x312590 -> 16-byte UDMA descriptors
        |     vring_dump_to_pring_descriptors_padded @0x311f10 -> physical ring (pring)
        |     prep_metaring_topsp_config @0x2331d0 -> per-channel TopSP config
        |
   [8]  proxy / barrier enqueue  (enc_enq_proxy_tasks @0x105560; net + barrier tasks)
```

The build-time family decision (`enc_init_comm`, §1.1) and the per-op resolution (stage [3]) are two passes over the *same* `enc_cc_algorithm_allowed` gate: init fixes which family objects are constructed; post picks the concrete `enc_alg_type` for each op from the families that survived. The proxy/barrier enqueue (stage [8]) drives the worker thread that progresses the network edges of multi-node rings — see [Bananaphone IPC and the Proxy Driver](proxy-driver.md).

### 2.1 The algorithm precedence

When all six `enc_can_post_*` gates have run, `enc_post_operation` resolves exactly one `enc_alg_type` into `v91` by a fixed if-else cascade (decompile lines 762–791; the mesh path is hard-asserted for all-to-all at `enc.cc:0xE4F`, `"alg == ENC_ALG_MESH"`):

| Priority | Guard (`enc_can_post_*` result) | Resolved `enc_alg_type` |
|---|---|---|
| 1 | `hierarchical_operation` (with `v86` = no kangaring/mesh/single-cycle and no ring drv_alg) | `ENC_ALG_HIER` (1) |
| 2 | `intra_rdh_operation` | `ENC_ALG_INTRA_RDH` (5) |
| 3 | `mesh_operation` | `ENC_ALG_MESH` (2) |
| 4 | `kangaring_operation` | `ENC_ALG_KANGARING` (3) |
| 5 | `single_cycle_ring` | `ENC_ALG_SINGLE_CYCLE_RING` (4) |
| 6 | `inter_rdh_operation` | `ENC_ALG_INTER_RDH` (7) |
| 7 | (fallthrough) | `ENC_ALG_RING` (0) |

> **QUIRK —** every `enc_can_post_*` gate runs unconditionally before the precedence cascade; the cascade is a priority resolver over their boolean results, **not** a short-circuit. So the battery is six function calls regardless of which family wins, and hierarchical wins *only* when no flat-ring family is feasible (the `v86` extra condition folds the ring drv_alg presence into the hierarchical gate). A reimplementer who short-circuits at the first feasible gate will mis-rank when two families are simultaneously allowed.

---

## 3. The collective op family

The 13 `enc_op_type` values are dispatched by the jump table at `0x12038a` (13 cases, default `0x1213c0`). The opcode integers are verbatim from `enums.json`. Each data-carrying opcode routes to a per-op-type handler that emits its INFO line, builds the `enc_operation`, and hands off to the resolved family composer; `BROADCAST`/`REDUCE` land on the unsupported default, and `*-permute-implicit` share the explicit-permute handlers.

| Op (`enc_op_type`) | Value | Dispatch target | Dense composer / note | Owning page |
|---|---|---|---|---|
| `ALLGATHER` | 0 | `0x122572` | ring all-gather / mesh / hier | [Ring Scheduling](ring-scheduling.md), [Mesh Composer](mesh-composer.md) |
| `ALLREDUCE` | 1 | `0x122bb6` | ring AR (RS+AG) / mesh / hier / RDH | [Ring Scheduling](ring-scheduling.md), [Hierarchical and RDH](hierarchical-rdh.md) |
| `BROADCAST` | 2 | `0x1213c0` (default) | unsupported on this path | — |
| `REDUCE` | 3 | `0x1213c0` (default) | unsupported on this path | — |
| `REDUCE_SCATTER` | 4 | `0x121ebd` | ring RS phase / mesh | [Ring Scheduling](ring-scheduling.md) |
| `SEND` | 5 | `0x123224` | point-to-point | [Async Send / Recv](send-recv.md) |
| `RECV` | 6 | `0x121958` | point-to-point | [Async Send / Recv](send-recv.md) |
| `ALLTOALL` | 7 | `0x120d97` | mesh only (`assert alg==MESH`) | [Mesh Composer](mesh-composer.md) |
| `PERMUTE` | 8 | `0x1207a6` | permute composer | [Ring Scheduling](ring-scheduling.md) |
| `PERMUTE_REDUCE` | 9 | `0x1213c0` (default) | permute-reduce composer | [Ring Scheduling](ring-scheduling.md) |
| `PERMUTE_IMPLICIT` | 10 | `0x1207a6` (shares `PERMUTE`) | permute composer | [Ring Scheduling](ring-scheduling.md) |
| `PERMUTE_REDUCE_IMPLICIT` | 11 | `0x121435` | permute-reduce composer | [Ring Scheduling](ring-scheduling.md) |
| `ALLTOALL_V` | 12 | `0x120d97` (shares `ALLTOALL`) | mesh only | [Mesh Composer](mesh-composer.md) |

Notes on the dispatch shape (full per-op-type field population lives in the family pages):

- **`ALLTOALL` / `ALLTOALL_V` are mesh-exclusive.** Both share target `0x120d97`, and the ring composer rejects all-to-all outright (`"alltoall cannot be supported without Mesh algorithm … rank_n(%d)"`); `enc_post_operation` hard-asserts `alg == ENC_ALG_MESH` for these op-types. A ring-only topology cannot serve all-to-all.
- **`BROADCAST` / `REDUCE` fall to the default branch** (`0x1213c0`) on this dispatcher — they are not emitted as standalone collective programs here; the ring per-channel dispatcher (`__compose_channel_ring @0x177340`) likewise routes opcodes 2/3 to its error path.
- **The implicit-permute variants alias the explicit handlers** (`PERMUTE_IMPLICIT`→`PERMUTE`, `PERMUTE_REDUCE_IMPLICIT`→its own slot), so a reimplementer maps four permute opcodes onto two composer bodies.

A second 13-case switch at `0x1242ef` (default `0x124943`) is the **per-op-type teardown / final INFO** dispatch in the same function — it shares the `enc_op_type` index but its cases are the op-end paths, not the build paths. Both switches confirm the 13-value family.

### 3.1 The algorithm roster

The host `enc_alg_type` is named by `enc_get_algorithm_name @0xfef30`, an 11-case switch (values 0..10, default `0xfefd0` = `"Invalid"`). The roster is the catalog of families the composer layer can build; the table below pins each to its name string and the device pattern it collapses to. Names are verbatim from `.rodata @0x840d11`.

| `enc_alg_type` | Value | Switch target | Name | Device `enc_pattern_t` | Composer page |
|---|---|---|---|---|---|
| `ENC_ALG_RING` | 0 | `0xfefc0` | `Ring` | RING (0) | [Ring Scheduling](ring-scheduling.md) |
| `ENC_ALG_HIER` | 1 | `0xfefb0` | `Hier` | (mixed) | [Hierarchical and RDH](hierarchical-rdh.md) |
| `ENC_ALG_MESH` | 2 | `0xfef60` | `Mesh` | MESH (1) | [Mesh Composer](mesh-composer.md) |
| `ENC_ALG_KANGARING` | 3 | `0xfef70` | `Kangaring` | RING (0) | [Ring Scheduling](ring-scheduling.md) |
| `ENC_ALG_SINGLE_CYCLE_RING` | 4 | `0xfefd0` | (→ `Invalid` name) | RING (0) | [Ring Scheduling](ring-scheduling.md) |
| `ENC_ALG_INTRA_RDH` | 5 | `0xfef50` | `RDH` | MESH-dependent | [Hierarchical and RDH](hierarchical-rdh.md) |
| `ENC_ALG_SINGLE_STEP_MESH` | 6 | `0xfefa0` | `Single Step Mesh` | MESH (1) | [Mesh Composer](mesh-composer.md) |
| `ENC_ALG_INTER_RDH` | 7 | `0xfef50` | `RDH` | MESH-dependent | [Hierarchical and RDH](hierarchical-rdh.md) |
| `ENC_ALG_TWO_STEP_POD_MESH` | 8 | `0xfef80` | `UltraServer Mesh` | MESH (1) | [Mesh Composer](mesh-composer.md) |
| `ENC_ALG_LATENCY_OPT_MESH` | 9 | `0xfef60` | `Mesh` | MESH (1) | [Switch Broadcast / Barrier](switch-broadcast-barrier.md) |
| `ENC_ALG_BW_OPT_MESH` | 10 | `0xfef90` | `Bw Optimal Mesh` | MESH (1) | [Switch Broadcast / Barrier](switch-broadcast-barrier.md) |
| `ENC_ALG_INVALID` | 11 | (>10 → default) | `Invalid` | INVALID (2) | — |

> **NOTE —** the roster has **three** redundancies worth flagging: `SINGLE_CYCLE_RING (4)` shares the `"Invalid"` string slot (its name is unused — it is a ring variant, not an invalid alg); `LATENCY_OPT_MESH (9)` reuses the plain `"Mesh"` string; and `INTRA_RDH (5)` / `INTER_RDH (7)` both print `"RDH"`. The *name* table therefore has fewer distinct labels than the enum has values. A third axis, `metaring_type` (`enc_get_metaring_type @0xfc860`: alg 0→RING, 3→KANGARING, 4→SINGLE_CYCLE_RING, 7→RDH), is the ring-family topology label — recorded for context; it is not a `cc_op_entry` field.

---

## 4. The cc_op_entry hand-off

The terminal artifact of every collective compile is the `cc_op_entry` stream the firmware executes. The composer's per-step leaves (ring `send`/`recv_reduce_send`/`direct_recv_send`, mesh handshake/reduce/broadcast events) all funnel into `create_spad_ctrl_entry @0x232cd0`, which packs an 8-byte `spad_ctrl_entry` = `{u8 header; cc_op_entry}`. The `cc_op_entry` is a 7-byte tagged descriptor: a 4-bit `algo_type` (the device `enc_pattern_t`), a 3-bit `algo_sub_type`, ring/mesh completion-handshake flags, and a 4-byte union that is *either* a ring `channel_list` bitmap *or* a mesh `{sema_shift_offset, sema_mask}` pair. The firmware (whose host-side reflection is libncfw's JSON serializer) reads `algo_type` and dereferences one union view.

This is the membrane between the host compile documented here and the device execution documented elsewhere. The byte-exact `cc_op_entry` bit-field, the union overlay, and the emit↔consume agreement are fully derived in [The cc_op_entry On-Device ISA](cc-op-isa.md); the `cc_op` stream merge/validate step (`enc_validate_and_merge_ccops @0x132b80`, run before scheduling) and the channel descriptor whose `abs_id` drives the `channel_list` bitmap are [encd: Device-Side Descriptor Emitter](encd-overview.md) and [The 148-Byte Ring Channel Descriptor](channel-descriptor.md).

> **NOTE —** the `cc_op_entry.algo_type` is **binary** (ring vs mesh) even though the host resolved one of 12 `enc_alg_type` values. Hierarchical and RDH do not get their own device pattern — they decompose into ring and mesh phases, each phase emitting `cc_op_entry`s with `algo_type ∈ {RING, MESH}`. The phase structure is the composer's; the device only ever sees the binary pattern per op.

---

## 5. Verification notes

> Substrate split, end-to-end flow, the 13-case op dispatch, and the algorithm roster were cross-checked against the IDA artifacts of `libnrt.so` 2.31.24.0:
> - `enc_post_operation @0x11f790`: the op-dispatch switch @`0x12038a` (13 cases, default `0x1213c0`) and the teardown switch @`0x1242ef` (13 cases, default `0x124943`) are both present in `switches.json`; the alg-precedence cascade (HIER > INTRA_RDH > MESH > KANGARING > SINGLE_CYCLE_RING > INTER_RDH > RING) is read from the decompile (lines 762–791) with the `"alg == ENC_ALG_MESH"` assert at `enc.cc:0xE4F`.
> - `enc_get_algorithm_name @0xfef30`: 11-case switch (values 0..10, default `0xfefd0`), targets and `.rodata` name strings confirmed.
> - Enums `enc_op_type` (13), `enc_alg_type` (12), `enc_pattern_t` (RING=0/MESH=1/INVALID=2), `metaring_type` (5) are verbatim from `enums.json`.
> - Emit symbols `create_spad_ctrl_entry @0x232cd0`, `encd_dma_mark_end @0x237200`, `vring_pack_descs @0x312590`, `vring_dump_to_pring_descriptors_padded @0x311f10`, `dma_ring_create_prings_from_vring @0x22e310`, `prep_metaring_topsp_config @0x2331d0` resolved in `function_addresses.json`; vring/spad strings (`"failed to write vring descriptors to pring"`, `"vrings[%d] is null"`) confirmed in `strings.json`.
>
> **[MED]** The mapping of each `enc_alg_type` to a device `enc_pattern_t` in §3.1 is inferred from the composer-to-pattern correspondence (ring families → RING, mesh families → MESH, RDH/HIER decompose into both phases), not from a single switch. The binary-ness of `cc_op_entry.algo_type` and `enc_pattern_t = {RING=0, MESH=1, INVALID=2}` are HIGH (enum + both binaries); which numeric pattern a *given* phase emits is owned by the composer-phase pages, not pinned op-by-op here.
> **[LOW]** The exact `enc_operation` field population per op-type (channel_n, slice_n, copy/reduce_slice_sz, permute chaining) is gated here but emitted in the family composers; only the dispatch targets and the alg precedence are pinned on this map.

---

## Cross-References

**Composer layer (host `enc/enc.cc`)**
- [Engine Core (enc_context and Accessors)](engine-core.md) — the `enc_context` parse/exec state, op_queue, and signature
- [Comm Context and Bootstrap](comm-context.md) — global-comm bootstrap, replica-group parse, local barriers
- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](nccl-boundary.md) — the `dlsym`'d `neuron*` ABI for multi-node
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](algorithm-taxonomy.md) — the `enc_alg_type` family catalog and `enc_cc_algorithm_allowed` gate
- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — `enc_alg_mesh`, the 20 subtypes, full/grouped/pd/switch builders
- [Ring Scheduling Math](ring-scheduling.md) — `enc_alg_metaring`, the ring step loop, chunk sizing
- [Hierarchical and RDH Composition](hierarchical-rdh.md) — the multi-phase intra/inter-pod composers
- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — the per-step `enc_primitive` leaves that produce cc_op + DMA
- [Switch-Platform Broadcast and Barrier Tables](switch-broadcast-barrier.md) — NeuronSwitch axis broadcast / latency-opt / bw-opt
- [Topology Partitioning (Union-Find)](topology-partition.md) — the grouped-mesh disjoint-set grouping
- [Async Send / Recv (Point-to-Point)](send-recv.md) — the `SEND`/`RECV` p2p path
- [Bananaphone IPC and the Proxy Driver](proxy-driver.md) — net-edge progression for multi-node rings

**Emitter layer (device-resident `tdrv/encd.c`)**
- [The cc_op_entry On-Device ISA](cc-op-isa.md) — the byte-exact collective-op descriptor the composer emits
- [The 148-Byte Ring Channel Descriptor](channel-descriptor.md) — the channel whose `abs_id`/`reporter` drive `channel_list`
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — the `encd_dma_mark_end` op-stream emitter and ccop merge
- [encd: DMA Descriptor and devmem Floor](encd-dma-devmem.md) — the vring → pring SDMA descriptor lowering
- [encd: Semaphore and TopSP Bring-Up](encd-sema-topsp.md) — per-channel TopSP config and semaphore wiring
- [encd: Per-Arch Ops Dispatch](encd-arch-ops.md) — the per-generation `encd` op-class dispatch

**Sibling subsystems**
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the SDMA descriptor the vrings carry
- [Static Memory Planning (mem_ref)](../neff/memory-planning.md) — the placement plan whose `MR_REMOTE` vars are collective shadow buffers
- [back to index](../index.md)
