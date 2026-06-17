# Switch-Platform Broadcast and Barrier Tables

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The TUs are `/opt/workspace/KaenaRuntime/enc/switch_platform/enc_switch_platform.cc` + `enc/switch_platform/events/{broadcast,handshake,reduce,sync}/*.cc` (the `_switch` composer) and `/opt/workspace/KaenaRuntime/enc/barrier.cc` (the barrier-group table builder). `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (decompile- and DWARF-anchored)** — the phase-builder set and the build↔compose event symmetry are read line-by-line from `initialize_trn3pds98 @0x1ff530` and the `build_*`/`compose_*` event TUs; `barrier_group::construct_table @0x2099a0` is read from disasm (Hex-Rays declined) but its dispatch is unambiguous via the tail-jumps; every struct offset is verbatim from `structures.json`, every enum value from `enums.json` (`barrier_type_t`, `enc_mesh_event_type`, `enc_op_type`), every address/signature from `functions.json`. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

The [mesh composer](mesh-composer.md) routes one specific topology — a NeuronSwitch-v1 (TRN3-PDS98) multi-rack fabric — to its own stateful builder, `alg_mesh_initializer_switch`, rather than the flat full-mesh path. This page owns that switch-platform branch and the host-side **barrier-group table** that drives the device barriers the switch schedule rides on. The switch composer is split into a **build side** (`alg_mesh_initializer_switch::build_*`, at comm-setup time) that allocates one `enc_alg_mesh_subtype` slot per algorithm and populates its `events[]` array by running an ordered sequence of **phase builders** — handshakes, broadcasts, reductions, dma-sync — and a **compose side** (`enc_mesh_primitive::compose_*`, at op-emit time) that mirrors that exact phase sequence to emit the device DMA descriptor stream. The build side additionally writes the per-op-type **size-limit table** (`op_max_limit` / `op_min_limit` and their `_sbuf` twins, indexed by `enc_op_type`) that the runtime later reads to *pick* this algorithm by message size.

The barrier-group table is a separate, lower subsystem built at NEFF load (`enc_load_operations @0x139d80`): per replica group it constructs a pair of `barrier_table`s — `table[0]` (send side) and `table[1]` (recv side) — where each entry is a `(enc_peer_info peer, barrier_type_t barr_type, int bit_to_set)` triple. `barrier_group::construct_table @0x2099a0` is a pure dispatcher that selects a topology strategy from device geometry: a switch-family fabric routes to `construct_table_switch_family @0x208470` (a two-level rack→rid map with a `glb_dev_id` modulo invariant and inter-rack counterpart matching), a multi-chip ring/pod geometry to `construct_table_ring_group @0x208130`, and a flat mesh to `construct_table_mesh_group @0x2090d0`. A shared `add_peer_to_tables @0x208300` dedups each peer against per-side `encountered_peers` scratch vectors before inserting, and a final `validate_and_populate_global_barrier_table @0x209ac0` merges every per-NEFF table into the per-device global barrier table, using a hashtable keyed on `{barr_type, bit_to_set}` to catch cross-NEFF bit collisions.

This page documents three things a reimplementer must reproduce: (1) the **barrier-table entry shape** and the `construct_table` topology-strategy dispatch — the switch / ring / mesh strategy pick, the dedup-then-insert discipline, and the global-merge collision check; (2) the **switch-platform phase table** — the ordered handshake/broadcast/reduce/dma-sync builders each `build_single_hop_*` algorithm runs, the `enc_mesh_event_type` each phase stamps, and the build↔compose mirror; and (3) a **representative event composer** — the broadcast and handshake emit logic, with the `num_fold` / `wait_val` metadata and the notify-mode discipline. The encd-level event emit (`encd_init_mesh_event`) and the generic primitive step-recorder API are owned elsewhere ([encd Overview](encd-overview.md), [enc Primitives](enc-primitives.md)); this page owns the switch-only schedule that calls them and the barrier table that gates them.

For reimplementation, the contract is:

- **A barrier-table entry is `(enc_peer_info, barrier_type_t, bit_to_set)`, and the strategy is dispatched on device geometry before any entry is built.** `barrier_group::construct_table @0x2099a0` picks switch-family vs ring/pod vs mesh from `nrt_is_neuron_switch_v1_family()` and the multi-chip geometry, then delegates to one strategy builder. Each builder dedups peers against the two `encountered_peers` vectors via `add_peer_to_tables @0x208300` and inserts into *both* the send (`table[0]`) and recv (`table[1]`) sides. A reimplementer dispatches first and reuses the dedup helper; it must not build the same `(peer, barr_type, bit)` twice, and it must run the global merge to detect two peers across different NEFFs that set the same bit.
- **The switch schedule is an ordered list of phase events per algorithm subtype, not a generated graph.** `initialize_trn3pds98 @0x1ff530` is the master build orchestrator; gated on family 15 and the logical-core size (`lnc`), it runs `build_single_hop_*` algorithm builders, each of which appends a fixed phase sequence of `build_<handshake|broadcast|chipr1w/dimr1w reduce|dma_sync>` events into one subtype slot. Every `build_X` phase has a mirror `compose_X` emitter; the build side stamps `evt_type` + `num_fold` + `wait_val` into the event, the compose side asserts the same `evt_type` and walks the planned neighbor groups. Reproduce the per-algorithm phase order and the build↔compose assertion pairing.
- **Each algorithm writes its message-size cutoffs into the subtype's per-op-type limit table, and that table is the runtime dispatch key.** `build_*_all_gather` writes index `[ENC_ALLGATHER=0]`; `build_*_all_reduce` writes index `[ENC_ALLREDUCE=1]` of `op_max_limit[13]` / `op_min_limit[13]` / their `_sbuf` twins (`enc_alg_mesh_subtype @+2584/+2688/+2792/+2896`). The values are rank-keyed (`ci->local_rack_rank_n` ∈ {4,8,16,32,64,128,256}) and capped at 4 MiB (`0x400000`) and/or `enc_get_mesh_channel_buf_max_size()`. A reimplementer reproduces the per-rank size table; getting the cap wrong mis-routes large messages to the wrong algorithm.

| | |
|---|---|
| **Barrier dispatcher** | `barrier_group::construct_table @0x2099a0` — switch / ring / mesh strategy pick |
| **Switch strategy** | `construct_table_switch_family @0x208470` — rack→rid map, glb_dev_id modulo invariant |
| **Ring strategy** | `construct_table_ring_group @0x208130` (lambda `@0x207e50`) |
| **Mesh strategy** | `construct_table_mesh_group @0x2090d0` |
| **Dedup-insert** | `add_peer_to_tables @0x208300` · `mark_peers_encountered @0x2081c0` (mark only) |
| **Global merge** | `validate_and_populate_global_barrier_table @0x209ac0` — `{barr_type,bit}` collision check |
| **Switch build master** | `alg_mesh_initializer_switch::initialize_trn3pds98 @0x1ff530` — family-15 / `lnc` gated |
| **Subtype slot** | `enc_alg_mesh_subtype` (3016 B) — `events[64]@+0`, `num_events@+2560`, `op_max_limit[13]@+2584` |
| **Barrier entry** | `barrier_table_entry` (32 B) — `peer@+0`, `barr_type@+24`, `bit_to_set@+28` |
| **Barrier group** | `barrier_group` (72 B) — `table[2]@+0` (send/recv), `group_id@+48`, `rg@+56`, `comm@+64` |
| **Event record** | `enc_mesh_event` (40 B) — `src_neighbor_grp@+0`, `dst_neighbor_grp@+16`, `valid@+32`, `evt_type@+36` |
| **Device emit** | `encd_init_mesh_event` (build) · `enc_primitive_mesh::send`/`notify` (compose) — see [encd Overview](encd-overview.md) |

> **NOTE —** "switch-platform" here means the NeuronSwitch-v1 instance family, internally `family == 15` (TRN3-PDS98). Only this family is supported by the event-based switch algorithms: `initialize @0x1ff7d0` and `compose_switch_platform @0x1fde40` both log *"Unsupported instance family for event-based algorithms"* and return `NRT_INVALID` for anything else. Every algorithm on this page is gated behind that family check.

---

## 1. The Barrier-Group Table

### Purpose

A device barrier is not a single CSR write; it is a fan-out of semaphore bits. Each participating rank must, on the **send** side, set a bit on every peer it is responsible for releasing, and on the **recv** side, wait on a bit from every peer that releases *it*. The barrier-group table is the static per-replica-group plan for that fan-out: two `barrier_table`s (`table[0]` send, `table[1]` recv), each a `std::vector<barrier_table_entry>` where an entry names *one* peer, the *kind* of link to it (`barrier_type_t`), and the semaphore *bit* to set or wait on (`bit_to_set`). It is built once per group at NEFF load (`enc_load_operations @0x139d80`) and then merged into the per-device global table that the runtime actually arms.

The structural problem is that "every peer I barrier with" is reached by different link kinds with different deadlock properties — an on-chip peer (`BARRIER_INTRA_CHIP`), a same-rack peer (`INTRA_RACK`), a cross-rack peer (`INTER_RACK`), a ring neighbor (`INTER_CHIP_RING_LOCAL` / `_REMOTE`), a pod peer (`INTRA_POD`). The `barrier_type_t` on each entry tells the device which path arms that bit. `construct_table` picks the *strategy* that knows how to enumerate those peers for the current geometry; the strategies share the dedup-insert helper and the entry shape.

### Entry Point

```text
enc_load_operations (0x139d80)                       ── NEFF load, per replica group
  └─ barrier_group::construct_table (0x2099a0)        ── DISPATCHER (read from disasm)
       ├─ [switch_v1] construct_table_switch_family (0x208470)   §1
       │     ├─ mark_peers_encountered (0x2081c0)     ── mark leader/same-rack, no entry
       │     └─ add_peer_to_tables   (0x208300)       ── dedup + insert send & recv
       ├─ [ring/pod] construct_table_ring_group (0x208130)
       │     ├─ {ring lambda} (0x207e50)              ── prev/next neighbor → INTRA_CHIP / RING_LOCAL/REMOTE
       │     └─ construct_table_pod_group (0x207be0)  ── [boundary] +pod entries if enable_pod
       └─ [mesh]     construct_table_mesh_group (0x2090d0)
             └─ (rev-)direct-link neighbors → INTRA_CHIP / INTER_CHIP
  └─ validate_and_populate_global_barrier_table (0x209ac0)   ── merge into device_barrier_table
       └─ barrier_table::add_table_entry (0x209ec0)  ── [boundary] + {barr_type,bit} collision hash
```

### The barrier-table entry struct

The entry is a flat 32-byte record: the full 24-byte `enc_peer_info` of the peer, then the link kind, then the bit index. The key used for the cross-NEFF collision check is a separate 8-byte `{barr_type, bit_to_set}` pair hashed as `barr_type ^ (bit_to_set << 7)`.

| Field | Offset | Type | Confidence |
|---|---|---|---|
| `peer.neuron_dev` | +0 | `int` | HIGH |
| `peer.rid` | +4 | `int` (ring-id) | HIGH |
| `peer.tpb_index` | +8 | `int` | HIGH |
| `peer.pod_node_id` | +12 | `int` | HIGH |
| `peer.reservation_id` | +16 | `uint64_t` | HIGH |
| `barr_type` | +24 | `barrier_type_t` (enum, below) | HIGH |
| `bit_to_set` | +28 | `int` (semaphore bit index) | HIGH |

`enc_peer_info` is the same 24-byte unit used throughout the collectives layer ([mesh composer](mesh-composer.md)); the barrier dedup compares the two low qwords (`[neuron_dev|rid]` and `[tpb_index|pod_node_id]`, loaded with `_mm_loadu_si128`) plus `reservation_id` to test peer identity.

The `barrier_type_t` enum (verbatim from `enums.json`) names the link kind the device uses to arm the bit:

| Value | Name | Meaning | Confidence |
|---|---|---|---|
| 0 | `BARRIER_INTRA_CHIP` | same `rid` — on-chip TPB-to-TPB | HIGH |
| 1 | `BARRIER_INTRA_POD` | within one pod | HIGH |
| 2 | `BARRIER_INTER_CHIP` | mesh peer across a direct link | HIGH |
| 3 | `BARRIER_INTER_CHIP_RING_LOCAL` | ring neighbor, local-direction leg | HIGH |
| 4 | `BARRIER_INTER_CHIP_RING_REMOTE` | ring neighbor, remote-direction leg | HIGH |
| 5 | `BARRIER_INTRA_RACK` | same rack, different chip (switch family) | HIGH |
| 6 | `BARRIER_INTER_RACK` | cross-rack counterpart (switch family) | HIGH |
| 7 | `BARRIER_MAX` | enum bound | HIGH |

> **GOTCHA —** the ring lambda computes its `barr_type` as `4 - is_sem_only_xactions_deadlock_safe(...)`, mapping a *deadlock-safe* neighbor to `INTER_CHIP_RING_LOCAL=3` and an unsafe one to `INTER_CHIP_RING_REMOTE=4`. The send and recv legs call `encd_is_sem_only_xactions_deadlock_safe` with the **peer arguments in opposite order**, so the same physical neighbor can be labeled `LOCAL` on one side and `REMOTE` on the other. A reimplementer who computes one `barr_type` per neighbor pair and reuses it for both table sides mis-labels the asymmetric leg and can reintroduce the very semaphore deadlock the local/remote split exists to avoid. (Direction asymmetry is MED confidence — inferred from the swapped argument order, not from an independent device trace.)

### Algorithm — construct_table dispatch

`construct_table @0x2099a0` is the only function in the barrier subsystem Hex-Rays declined; its logic is recovered from disasm and is unambiguous because the dispatch bottoms out in tail-jumps to the three strategy builders. It reads the instance family and the chip/rank geometry, then delegates.

```c
// barrier_group::construct_table @0x2099a0  (enc/barrier.cc) — dispatch only, read from disasm
function construct_table(this):                           // this = barrier_group*
    ci   = this->comm->ci;                                // barrier_group+64 -> comm -> ci

    if nrt_is_neuron_switch_v1_family():                  // family 15 fabric
        return construct_table_switch_family(this);       // 0x208470  §1

    // multi-chip ring / pod geometry: a ring exists or pod is enabled
    if has_ring_geometry(ci) || ci->enable_pod:
        construct_table_ring_group(this);                 // 0x208130 — ring lambda over comm.ring
        if ci->enable_pod:
            construct_table_pod_group(this);              // 0x207be0 [boundary]
        return NRT_SUCCESS;

    // else: flat mesh of direct-link peers
    chip_n        = count_distinct_rids(ci);
    ranks_per_chip = ci->rank_n / chip_n;
    if !valid_mesh_geometry(chip_n, ranks_per_chip):
        // "[nec_dev %u] Unsupported config for the barrier, chip count %d num ranks per chip %d" @0x7fd1b0
        return NRT_FAILURE;                               // (2)
    return construct_table_mesh_group(this);              // 0x2090d0
```

### Algorithm — dedup-then-insert

Every strategy builds entries through `add_peer_to_tables @0x208300`, which guards against double-insert using the two `encountered_peers` scratch vectors (one per table side, held on the owning `barrier_composer`), then inserts the same peer into both the send and recv tables with its computed `barr_type` and bit. `mark_peers_encountered @0x2081c0` is the sibling that records a peer as *covered* without emitting an entry — used by the switch strategy to mark the rack leader and same-rack peers that another rank is responsible for.

```c
// barrier_group::add_peer_to_tables @0x208300
function add_peer_to_tables(this, rank, barr_type, bit_to_set):
    peer = this->comm->ci->peers[rank];                   // enc_peer_info, 24 B
    enc  = this->composer->encountered_peers;             // std::array<vector<enc_peer_info>,2>

    // dedup: skip if this peer is already covered on either side
    if peer_in(enc[0], peer) && peer_in(enc[1], peer):
        return NRT_SUCCESS;                               // already inserted / marked

    // send side (table[0]) and recv side (table[1])
    ret = this->table[0].add_table_entry(peer, barr_type, bit_to_set);   // 0x209ec0
    assert(ret == NRT_SUCCESS);                           // "ret == NRT_SUCCESS" barrier.cc:0x174
    ret = this->table[1].add_table_entry(peer, barr_type, bit_to_set);
    assert(ret == NRT_SUCCESS);                           // barrier.cc:0x179

    enc[0].push_back(peer);  enc[1].push_back(peer);      // mark covered
    return NRT_SUCCESS;
```

> **NOTE —** the switch-family strategy (`@0x208470`) is the most elaborate: it builds a `map<rack, map<rid, vector<rank>>>`, sorts each rid's rank list by global device id (the two libstdc++ sort helpers `@0x206c80`/`@0x206e00` are specialized for its `participants[rank]` comparator), then emits `INTRA_CHIP` (same rid), `INTRA_RACK` (same rack, different rid), and `INTER_RACK` (the cross-rack counterpart) entries. It enforces three invariants, each with its own error string: every rid must carry the expected rank count (*"rid %d has %lu ranks but expecting %lu"*), the leader's global device id must share the local rank's `modulo` (*"my glb_dev_id … have different modulo %d values"*), and an inter-rack counterpart must exist for each rack-local rid (*"inter-rack counterpart not found for rack_local_rid %d in rack %d"*). The exact map-key derivation per branch was control-flow traced but not re-derived field-by-field (MED on the modulo arithmetic; HIGH on the entry-emission shape).

### Algorithm — global merge and collision detection

After every group's table is built, `validate_and_populate_global_barrier_table @0x209ac0` folds the per-NEFF **recv** tables (`table[1]`) into the per-device `enc_glb_comm::device_barrier_table`, using a hashtable keyed on `barrier_table_entry_key{barr_type, bit_to_set}` (hash = `barr_type ^ (bit_to_set << 7)`) to detect the case where two peers — possibly from *different* NEFFs — would arm the same semaphore bit.

```c
// validate_and_populate_global_barrier_table @0x209ac0
function validate_and_populate_global(composer):
    g_comm = encd_get_global_comm();
    assert(g_comm);                                       // "g_comm" barrier.cc:0x244
    seen = {};   // hashtable keyed {barr_type, bit_to_set}

    for grp in composer->groups:                          // barrier_composer+64
        for e in grp.table[1].entries:                    // recv side only
            key  = { e.barr_type, e.bit_to_set };
            prev = seen.find(key);
            if prev != end && prev.peer != e.peer:
                // "found multiple peers across diff neffs that are setting the same bit
                //  barr type %d bit pos %d peer0(...) peer1(...)"
                return NRT_FAILURE;                        // bit collision across NEFFs
            seen.insert(key, e.peer);
            g_comm->device_barrier_table.add_table_entry(e.peer, e.barr_type, e.bit_to_set);  // 0x209ec0
    return NRT_SUCCESS;
```

> **QUIRK —** the collision check keys only on `{barr_type, bit_to_set}` and merges only the **recv** side (`table[1]`). Two distinct entries may legitimately carry the same bit *if they name the same logical peer reached two ways* — the check fires only when `prev.peer != e.peer`. A reimplementer who keys the global table on the full peer triple instead of `{barr_type, bit}` never detects a genuine cross-NEFF collision (two unrelated peers racing for one semaphore bit), which surfaces as a silent lost-wakeup at run time, not a load-time failure.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `barrier_group::construct_table` | `0x2099a0` | strategy dispatcher (disasm-read) | HIGH |
| `barrier_group::construct_table_switch_family` | `0x208470` | rack→rid map, glb_dev_id modulo, inter-rack match | HIGH |
| `barrier_group::construct_table_ring_group` | `0x208130` | ring prev/next neighbor entries (+pod) | HIGH |
| `{ring lambda}` | `0x207e50` | per-neighbor `INTRA_CHIP` / `RING_LOCAL`/`REMOTE` emit | HIGH |
| `barrier_group::construct_table_mesh_group` | `0x2090d0` | direct-link neighbor entries, TRN2/TRN3 tpb-parity gate | HIGH |
| `barrier_group::add_peer_to_tables` | `0x208300` | dedup + insert into send & recv | HIGH |
| `mark_peers_encountered` | `0x2081c0` | mark covered, no entry (file-local `_ZL`) | HIGH |
| `validate_and_populate_global_barrier_table` | `0x209ac0` | global merge + `{barr_type,bit}` collision check | HIGH |
| `barrier_table::add_table_entry` | `0x209ec0` | append one entry (boundary, [encd Overview](encd-overview.md)) | HIGH |

---

## 2. The Switch-Platform Phase Schedule

### Purpose

`alg_mesh_initializer_switch` is the NeuronSwitch-v1 mesh composer. Like the [pod initializer](mesh-composer.md#4-per-platform-divergence-pod-and-switch-initializers) it is a stateful object — base `alg_mesh_initializer {vptr, mesh@8, ci@16, curr_subtype_id@24}` plus a cached `nrt_instance_info_t @+28` — but its object is only 64 B because per-rack state is read live from the instance info. Its master orchestrator `initialize_trn3pds98 @0x1ff530` selects an algorithm menu from the logical-core size (`lnc`) and the replica-group shape, and each algorithm it picks is a `build_single_hop_*` (or `build_one_rank_per_chip_*`) builder that appends a **fixed ordered phase sequence** into one freshly-allocated subtype slot.

The phase sequence is the heart of the schedule. A `build_single_hop_latency_opt_all_gather`, for example, is exactly: a PCIe-peer handshake, an intra-chip handshake, an intra-axis broadcast, an inter-axis broadcast, and a dma-sync — five events appended in order into `mesh_subtype[curr].events[0..4]`. Every phase builder has a mirror compose emitter that consumes the same event at op-emit time; the build side plans the neighbor groups and stamps `evt_type`, the compose side asserts that `evt_type` and walks the groups to emit DMA. This section documents the phase vocabulary, the per-algorithm phase order, and the build↔compose pairing; §3 walks a representative composer body.

### Entry Point

```text
alg_mesh_build_subtypes (other cell)                  ── mesh dispatcher (mesh-composer.md §1)
  └─ alg_mesh_initializer_switch::initialize (0x1ff7d0)        ── family==15 guard
       └─ ::initialize_trn3pds98 (0x1ff530)                    ── MASTER BUILD ORCHESTRATOR
            ├─ encd_get_virtual_core_size(&lnc)                ── lnc gates LNC1 vs LNC2
            ├─ build_single_hop_latency_opt_all_gather (0x2002e0)   one subtype slot
            ├─ build_single_hop_bandwidth_opt_all_gather (0x1ffac0) [lnc==2 only]
            ├─ build_single_hop_reduce_scatter             [boundary]
            ├─ build_single_step_all_reduce (0x202de0)     [local_rack_rank_n <= 64]
            ├─ build_single_hop_latency_opt_all_reduce (0x201c60)
            ├─ build_single_hop_bandwidth_opt_all_reduce (0x2013a0) [lnc==2 only]
            └─ build_single_hop_latency_opt_all_to_all     [lnc==2 only, boundary]
       each build_X ── runs its phase sequence (below) ── writes events[] + op_*_limit[op_type]

enc_mesh_primitive::__compose_channel (other cell)    ── op emit, per CC instruction
  └─ ::compose_switch_platform (0x1fde40)             ── nrt_get_instance_info; compute_rg_coord_space
       └─ ::compose_trn3pds98 (0x1fd9c0)              ── reads subtype flags, TAIL-JMPs to one compose_*
            └─ compose_single_hop_*  ── mirrors the build phase sequence, emits DMA
```

### The switch-phase table

Each row is one phase builder a `build_single_hop_*` algorithm calls, in order. The `evt_type` is the `enc_mesh_event_type` the build side stamps and the compose side asserts. All addresses below are real `.text` functions in the switch composer.

| Phase | Builder | Compose mirror | Emits (`evt_type`) | Confidence |
|---|---|---|---|---|
| single-hop pcie peer handshake | `build_single_hop_pcie_peer_handshake @0x21c3b0` | `compose_single_hop_pcie_peer_handshake @0x21bfb0` | `EVT_GLOBAL_HNDSHK(1)` | HIGH |
| intra-chip handshake | `build_intra_chip_handshake @0x21bb40` | `compose_intra_chip_handshake @0x21b740` | `EVT_LOCAL_HNDSHK(2)` | HIGH |
| all-rank handshake | `build_all_rank_handshake @0x21b1c0` | `compose_all_rank_handshake @0x21adc0` | `EVT_GLOBAL_HNDSHK(1)` | HIGH |
| intra-axis broadcast | `build_intra_axis_broadcast @0x20ecf0` | `compose_intra_axis_broadcast @0x20f230` | `EVT_INTER_GRP_BRDCST(3)` | HIGH |
| inter-axis broadcast | `build_inter_axis_broadcast @0x20ac40` | `compose_inter_axis_broadcast @0x20c450` | `EVT_INTER_GRP_BRDCST(3)` / `_2(11)` | HIGH |
| intra-axis bw-opt broadcast | `build_intra_axis_bw_opt_broadcast @0x211900` | `compose_intra_axis_bw_opt_broadcast @0x2115d0` (thunk → `0x20f230`) | `EVT_INTER_GRP_BRDCST(3)` | HIGH |
| inter-axis bw-opt broadcast | `build_inter_axis_bw_opt_broadcast @0x20d410` | `compose_inter_axis_bw_opt_broadcast @0x20d940` | `EVT_INTER_GRP_BRDCST_2(11)` | HIGH |
| intra-chip broadcast | `build_intra_chip_broadcast @0x212220` | `compose_intra_chip_broadcast @0x214800` (d2d + rmv legs) | `EVT_INTRA_GRP_BRDCST(7)` | HIGH |
| intra-axis chipr1w reduce | `build_intra_axis_chipr1w @0x21e280` | `compose_intra_axis_chipr1w @0x21e710` | `EVT_REDUCE_COPY(8)` | HIGH |
| inter-axis dimr1w reduce | `build_inter_axis_dimr1w @0x21cb60` | `compose_inter_axis_dimr1w @0x21d040` | `EVT_REDUCE_COPY_2(9)` | HIGH |
| intra-rank dimr1w reduce | `build_intra_rank_dimr1w @0x21f9e0` | `compose_intra_rank_dimr1w @0x21fdb0` | `EVT_REDUCE_WRITE(10)` | HIGH |
| single-step reduce | `build_single_step_reduce @0x2237e0` | `compose_single_step_reduce @0x223bb0` | `EVT_REDUCE_WRITE(10)` | HIGH |
| dma-sync | `build_dma_sync @0x224720` | `compose_dma_sync @0x224640` | `EVT_SYNC(0)` | HIGH |
| first / last fn barrier | (build inlined into the `compose_trn3pds98` bracket) | `compose_first_function_barrier @0x21ac80` · `compose_last_function_barrier @0x21ad20` | `notify` only | HIGH |

> **NOTE —** `build_intra_chip_broadcast @0x212220` carries a copy/paste log bug in the binary: its DEBUG string reads *"Building intra axis broadcast event"* even though the function and its stamped `evt_type` are intra-**chip** (`EVT_INTRA_GRP_BRDCST=7`). This is a defect in the binary, not an analysis error — do not propagate the mislabel into a reimplementation's logging.

### The per-algorithm phase order

The algorithms `initialize_trn3pds98` builds each lay down a fixed phase order. The all-gather and all-reduce families differ in whether reduce phases precede the broadcast and in which broadcast variant (latency-opt vs bandwidth-opt) they pick; the bw-opt variants are LNC2-only.

```text
build_single_hop_latency_opt_all_gather (0x2002e0):
    pcie_peer_handshake → intra_chip_handshake → intra_axis_broadcast
      → inter_axis_broadcast(EVT_INTER_GRP_BRDCST_2) → dma_sync         [5 events]

build_single_hop_bandwidth_opt_all_gather (0x1ffac0):  [LNC2 only]
    pcie_peer_handshake → intra_chip_handshake → intra_axis_bw_opt_broadcast
      → inter_axis_bw_opt_broadcast → intra_chip_broadcast → dma_sync

build_single_hop_latency_opt_all_reduce (0x201c60):
    intra_chip_handshake → intra_axis_chipr1w → inter_axis_dimr1w → intra_rank_dimr1w
      → intra_axis_broadcast → inter_axis_broadcast(EVT_INTER_GRP_BRDCST_2) → dma_sync

build_single_step_all_reduce (0x202de0):  [local_rack_rank_n <= 64]
    intra_axis_broadcast → inter_axis_broadcast(EVT_INTER_GRP_BRDCST_2)
      → single_step_reduce → dma_sync
```

The reduce family's `evt_type` / reduce-mode pairing is exact and cross-checked build↔compose: `intra_axis_chipr1w` stamps `EVT_REDUCE_COPY(8)` and composes `reduce(...,copy_mode=4)`; `inter_axis_dimr1w` stamps `EVT_REDUCE_COPY_2(9)` and also composes mode 4; the self-only write phases (`intra_rank_dimr1w`, `single_step_reduce`) stamp `EVT_REDUCE_WRITE(10)` and compose `reduce(...,copy_mode=0)`. The `num_fold` metadata distinguishes them: `chipr1w`/`inter_dimr1w` set `num_fold=4`, the self-write reduces set `num_fold=8`, the proxy remote reduce sets `num_fold=1`.

### The per-op-type size-limit table

Each algorithm subtype writes a row of message-size cutoffs the runtime later reads to pick *which* subtype handles a given collective at a given size. The table is four parallel `size_t[13]` arrays indexed by `enc_op_type`:

| Array | Offset | Indexed by | Confidence |
|---|---|---|---|
| `op_max_limit[13]` | +2584 | `enc_op_type` | HIGH |
| `op_min_limit[13]` | +2688 | `enc_op_type` | HIGH |
| `op_max_limit_sbuf[13]` | +2792 | `enc_op_type` | HIGH |
| `op_min_limit_sbuf[13]` | +2896 | `enc_op_type` | HIGH |

All-gather algorithms write index `[ENC_ALLGATHER=0]`; all-reduce algorithms write index `[ENC_ALLREDUCE=1]`. The values are rank-keyed off `ci->local_rack_rank_n` and capped. For example, `build_single_hop_bandwidth_opt_all_gather` (LNC2) writes a rank table of `{4,8 → 0x400000, 16 → 0x1000000, 32/64/128 → 0x1800000}` into `op_max_limit_sbuf[0]`, and `build_single_step_all_reduce` writes a small table of `{4 → 0x20000, 8 → 0x8000, 16 → 0x4000, 32/64 → 0x2000}`. The latency-opt all-reduce clamps `op_max_limit_sbuf[1] = min(2 * chan_buf_max, 0x400000)`.

```c
// the limit write, common shape across build_* (read from build_single_hop_bandwidth_opt_all_gather @0x1ffac0)
function write_size_limits(st, ci, op_idx, lnc):          // st = &mesh_subtype[curr]
    if lnc != 2:
        // "Bandwidth Opt All Gather does not support LNC1"
        return NRT_INVALID;
    switch ci->local_rack_rank_n:                          // enc_comm_info+16
        case 4: case 8:  size = 0x400000;                 // 4 MiB
        case 16:         size = 0x1000000;
        case 32: case 64: case 128: size = 0x1800000;     // 24 MiB
        default:
            // "Unsupported rank (%d) for building Bandwidth Opt All Gather"
            return NRT_INVALID;
    st->op_max_limit[op_idx]      = 0;                     // table semantics: 0 = unset row
    st->op_min_limit[op_idx]      = 0;
    st->op_max_limit_sbuf[op_idx] = size;                 // the runtime dispatch cutoff
    st->op_min_limit_sbuf[op_idx] = 0;
```

> **QUIRK —** the size cutoff lives in the `_sbuf` (static-buffer) twin of the table, not the plain `op_max_limit`, for these switch all-gather/all-reduce algorithms — the plain `op_max_limit[op_idx]` is written `0`. A reimplementer who populates only `op_max_limit` and leaves `op_max_limit_sbuf` at zero makes the runtime see a zero-width window for every static-buffer message and never selects the algorithm. The `_sbuf` distinction tracks whether the collective stages through the per-channel static buffer or the device-memory channel buffer; the limit must be written into the matching twin.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `alg_mesh_initializer_switch::initialize` | `0x1ff7d0` | family-15 guard → `initialize_trn3pds98` (77 B) | HIGH |
| `alg_mesh_initializer_switch::initialize_trn3pds98` | `0x1ff530` | master build orchestrator; `lnc`/rank gating | HIGH |
| `build_single_hop_latency_opt_all_gather` | `0x2002e0` | latency-opt all-gather phase chain; writes `[0]` | HIGH |
| `build_single_hop_bandwidth_opt_all_gather` | `0x1ffac0` | bw-opt all-gather (LNC2); rank size table → `[0]_sbuf` | HIGH |
| `build_single_hop_latency_opt_all_reduce` | `0x201c60` | latency-opt all-reduce chain; writes `[1]` | HIGH |
| `build_single_hop_bandwidth_opt_all_reduce` | `0x2013a0` | bw-opt all-reduce (LNC2); writes `[1]` | HIGH |
| `build_single_step_all_reduce` | `0x202de0` | single-step all-reduce (rank ≤ 64); small size table | HIGH |
| `build_dma_sync` | `0x224720` | append `EVT_SYNC(0)` self-only event (~11 callers) | HIGH |
| `compose_switch_platform` | `0x1fde40` | compose entry: family-15 guard → `compose_trn3pds98` | HIGH |
| `compose_trn3pds98` | `0x1fd9c0` | reads subtype flags, tail-jmp dispatch to one `compose_*` | HIGH |
| `compute_rg_coord_space` | `0x1fdcb0` | replica-group MLA bounding box (`rows`/`cols`) | HIGH |

---

## 3. A Representative Event Composer

### Purpose

The build side plans an event; the compose side emits it. Every `compose_*` shares a fixed discipline: assert the event's `evt_type` matches the expected phase, resolve DMA source/destination addresses from the page table via `__get_pgt_offset`, pack one or more `enc_copy_op_params` (or `enc_reduce_op_params` for reduce phases), and hand them to `enc_primitive_mesh::send` followed by `enc_primitive_mesh::notify`. The `notify` *mode* encodes intent: mode 4 is a semaphore/handshake update, mode 0 is a plain completion. This section walks two representative composers — the inter-axis broadcast (a data-moving phase) and the all-rank handshake (a notify-only phase) — to ground the build↔compose mirror.

The whole compose pass is bracketed. `compose_trn3pds98` builds a 128-byte stack `enc_primitive_mesh prmtv` from the primitive's fields (op idx, data type, nccl mhandles, sema shift, function id), deep-copies the completion-assert-address vector, calls `proxy_start_operation(EVT_LOCAL_HNDSHK=2, EVT_INTER_GRP_BRDCST=3, 0, 0)`, then runs `compose_first_function_barrier`, the per-algorithm phase emitters, `compose_last_function_barrier`, `compose_dma_sync`, and finally `end_context`. The phase emitters between the barriers are the mirror of the build phase sequence.

### Algorithm — a broadcast composer

`compose_inter_axis_broadcast @0x20c450` consumes the inter-axis broadcast event. It asserts the planned `evt_type`, then for each source rank and each output block it computes the output page-table offset and builds an `enc_copy_op_params` DMA descriptor — selecting, for a single-step all-reduce, the double-buffered mesh buffer rather than the per-rank output address — and submits it.

```c
// enc_mesh_primitive::compose_inter_axis_broadcast @0x20c450
//   (enc/switch_platform/events/broadcast/inter_axis_broadcast_event.cc)
function compose_inter_axis_broadcast(this, prmtv, evt_id):
    event = &this->mesh_subtype->events[*evt_id];         // enc_mesh_event, 40 B
    assert(event->evt_type == evt_type);                  // inter_axis_broadcast_event.cc:0x80

    op_sz     = get_operation_size(this);
    max_block = get_max_block_sz(this);
    partial   = use_partial_data_element_entry(this);     // 0x1fdc80

    for src_rank in event->src_neighbor_grp.ranks[0 .. ranks_n]:
        copy_ops = vector<enc_copy_op_params>{};
        for k in 0 .. num_output_blocks:
            block_sz  = get_block_sz(k, max_block, op_sz, partial);   // 0x1fdc50, floor 0
            block_off = get_block_offset(k, max_block);              // 0x1fdc70 = max_block*k
            ret = this->__get_pgt_offset(PAGETABLE_TYPE_OUTPUT, k, &out_off);  // 0x149a70
            if ret != NRT_SUCCESS:
                // "Inter axis broadcast failed to get output offset, ret = %d"
                return ret;

            // single-step ALLREDUCE writes the double-buffered mesh buffer; else the peer's output addr
            if this->op_type == ENC_ALLREDUCE && this->mesh_subtype->is_single_step_mesh:
                d_addr = neighbor_params_array[dst].mesh_buf + mesh_double_buf_offset;   // +64 / +256
            else:
                d_addr = neighbor_params_array[dst].output_addr[k];                       // +48

            copy_ops.push_back(enc_copy_op_params{          // 184 B
                src_idx    = src_rank,
                s_offsets  = [block_off],
                d_addrs    = [d_addr + out_off],
                copy_sizes = [block_sz],
                d_ranks    = [dst_rank] });
        enc_primitive_mesh::send(prmtv, *evt_id, &copy_ops);
    ++*evt_id;
    enc_primitive_mesh::notify(prmtv, *evt_id, /*mode=*/0, 0);   // plain completion
```

The build side (`build_inter_axis_broadcast @0x20ac40`) is the planning mirror: it computes the src/dst/recv rank groups via the `add_*_peer_ranks` family (`add_rmv_peer_ranks @0x1fe8c0`, `add_single_hop_inter_chip_peer_ranks_recv @0x1fe5d0`) plus `shuffle_peers_for_port_spraying`, `str_join`s them into the *"src-%s dst-%s recv-%s"* debug log, allocates the neighbor group via `__alloc_neighbor_group @0x10f140`, fills `encd_mesh_evt_metadata{ valid=1, valid_dma_trigger_evt=1, valid_wait_evt=1, num_fold=2, wait_val=#recv_ranks }`, and commits via `encd_init_mesh_event @0x24b830`. The compose side never recomputes the groups — it reads the planned `event->src_neighbor_grp` / `dst_neighbor_grp` directly.

### Algorithm — a handshake composer

The handshake phases carry no data; they are a pure semaphore notify. `compose_all_rank_handshake @0x21adc0` asserts the global-handshake event type and, unless skipped, issues a mode-4 notify (sem update) and advances the event index.

```c
// enc_mesh_primitive::compose_all_rank_handshake @0x21adc0
//   (enc/switch_platform/events/handshake/all_rank_handshake_event.cc)
function compose_all_rank_handshake(this, prmtv, evt_id, skip):
    event = &this->mesh_subtype->events[*evt_id];
    assert(event->evt_type == EVT_GLOBAL_HNDSHK);         // (1)
    if !skip:
        // "[nec_dev %u] Composing all rank handshake"
        enc_primitive_mesh::notify(prmtv, *evt_id, /*mode=*/4, 0);   // 4 = sem / handshake
    else:
        // "[nec_dev %u] Skipping composition of all rank handshake"
        ;
    ++*evt_id;
```

The two other handshake composers follow the same shape with different event types and skip gates: `compose_intra_chip_handshake @0x21b740` asserts `EVT_LOCAL_HNDSHK(2)`; `compose_single_hop_pcie_peer_handshake @0x21bfb0` asserts `EVT_GLOBAL_HNDSHK(1)` and is additionally gated on `this->mesh_skip_global_handshake @+208`. The build side (`build_all_rank_handshake @0x21b1c0`) stamps `EVT_GLOBAL_HNDSHK(1)` with `valid_wait_evt=1` and a destination group of all ranks except self (grown via a realloc loop).

> **GOTCHA —** `wait_val` in the broadcast metadata is the **count of recv ranks**, not a byte size or a fold factor. The build side sets `encd_mesh_evt_metadata.wait_val = event->dst_neighbor_grp.ranks_n` (or the recv-group size); the device arms a semaphore threshold of exactly that many incoming notifies. `num_fold` is a *separate* field (2 for broadcasts, 4/8/1 for the reduce variants) that controls reduction-tree unrolling, not the wait threshold. A reimplementer who conflates `wait_val` and `num_fold`, or reads `wait_val` as a transfer size, mis-sizes every switch-platform semaphore and either hangs (threshold too high) or releases early (threshold too low).

> **QUIRK —** `compose_first_function_barrier @0x21ac80` and `compose_last_function_barrier @0x21ad20` are notify-only and *self-gated* on `this->first_cc_fn_barr @+272` / `last_cc_fn_barr @+273`. When the gate is false they emit *nothing* (logging *"Skipping composition of … function barrier"*) — but the build side still reserved the event slot. The barrier event ids are cached separately (`first_fn_barr_evt_id @+276`, `last_fn_barr_evt_id @+280`) and are *not* the running `evt_id` the phase emitters advance. A reimplementer who threads the running `evt_id` through the barrier notifies (instead of the cached barrier ids) notifies the wrong semaphore and corrupts the per-function barrier accounting.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `build_inter_axis_broadcast` | `0x20ac40` | plan inter-axis broadcast event (groups + metadata) | HIGH |
| `compose_inter_axis_broadcast` | `0x20c450` | emit inter-axis broadcast DMA (`send` + `notify 0`) | HIGH |
| `build_all_rank_handshake` | `0x21b1c0` | plan `EVT_GLOBAL_HNDSHK` to all-but-self | HIGH |
| `compose_all_rank_handshake` | `0x21adc0` | emit handshake `notify 4` (or skip) | HIGH |
| `compose_intra_chip_handshake` | `0x21b740` | emit `EVT_LOCAL_HNDSHK` notify | HIGH |
| `compose_single_hop_pcie_peer_handshake` | `0x21bfb0` | emit handshake; gated on `mesh_skip_global_handshake` | HIGH |
| `compose_first_function_barrier` | `0x21ac80` | notify on `first_cc_fn_barr`, cached barrier evt id | HIGH |
| `compose_last_function_barrier` | `0x21ad20` | notify on `last_cc_fn_barr` | HIGH |
| `enc_mesh_primitive::__get_pgt_offset` | `0x149a70` | page-table chunk/offset resolver ([enc Primitives](enc-primitives.md)) | HIGH |
| `should_write_to_proxy_buff` | `0x1fdf20` | RMV vs direct routing gate for proxy composers | HIGH |
| `encd_init_mesh_event` | `0x24b830` | device-side event installer (build sink, [encd Overview](encd-overview.md)) | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `alg_mesh_initializer_switch::initialize` (`@0x1ff7d0`) | the switch composer entry that this page's phase builders implement |
| `compose_trn3pds98` (`@0x1fd9c0`) | reads the subtype flag bytes (`is_bw_opt`/`is_latency_opt`/`is_one_rank_per_chip`/`is_single_step_mesh`) to tail-jump to one `compose_*` |
| `enc_load_operations` (`@0x139d80`) | the NEFF-load driver that calls `construct_table` and `validate_and_populate_global_barrier_table` |
| `enc_primitive_mesh::send` / `::notify` | the compose-side device sink each event emitter bottoms out into ([enc Primitives](enc-primitives.md)) |
| `encd_init_mesh_event` (`@0x24b830`) | the build-side device sink each phase builder commits its planned event through ([encd Overview](encd-overview.md)) |

## Cross-References

- [Mesh Composer (alg_mesh_initializer)](mesh-composer.md) — owns the full-mesh / pod composer and the dispatcher that routes the switch family to `alg_mesh_initializer_switch`; this page owns that switch branch and the barrier table
- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — owns `enc_primitive_mesh::send`/`notify`, `__get_pgt_offset`, and the `enc_copy_op_params` / `enc_reduce_op_params` records the compose side fills
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — owns `encd_init_mesh_event` (the build sink), `barrier_table::add_table_entry`, and the metaring/TopSP device tables the switch reduce phases credit against
- [The 148-Byte Ring Channel Descriptor](channel-descriptor.md) — the ring-algorithm sibling of the switch event schedule; the barrier ring strategy (`construct_table_ring_group`) walks the same ring neighbor set
- [TopSP Notification Path](../kernel/topsp.md) — the on-device sync core that arms the barrier semaphore bits this page's `bit_to_set` entries name, and walks the spad-control op-stream the compose side feeds
- [back to index](../index.md)
