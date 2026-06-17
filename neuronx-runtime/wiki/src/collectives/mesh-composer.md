# Mesh Composer (alg_mesh_initializer)

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present. The composer source TU is `/opt/workspace/KaenaRuntime/enc/enc.cc` (group decomposition + subtype build) and `enc/replica_groups.cc`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (decompile- and DWARF-anchored)** — the peer-classification loop and the five subtype builders are read line-by-line from `alg_mesh_build_full_mesh @0x125fb0` (1752 decompiled lines); every enum value (`mesh_group_types`, `enc_alg_mesh_type_t`, `enc_alg_type`, `enc_mesh_event_type`, `enc_pattern_t`) is verbatim from `enums.json`; every struct offset from `structures.json`; every function address and signature from `functions.json`. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

A replica group arrives at the collectives layer as a flat list of ranks with a per-rank peer table (`enc_comm_info.peers[rank_n]`), and a mesh collective wants the opposite of a ring: instead of one forward neighbor per rank, **every** rank talks to **every** other in one round of fully-peered transfers. The mesh composer is the host-side pass that builds that all-to-all schedule once, at NEFF-load time, as a fixed array of *events* — handshake, reduce-copy, reduce-write, broadcast, sync, copy-from-host — bundled into up to five *mesh subtypes* (one schedule shape per topology size), and hands each event down to the device emitter `encd_init_mesh_event`. The entry is `alg_mesh_init @0x135990`; the dispatcher `alg_mesh_build_subtypes @0x133cd0` picks one of three composer flavors by silicon family; and the canonical full-mesh flavor is `alg_mesh_build_full_mesh @0x125fb0`.

The structural problem a full mesh must solve is that "every other rank" is not one kind of peer. On a multi-node job a peer is reached by one of three transports with three different costs: a **local** peer over the on-package NeuronLink mesh (a real `neuron_dev`), an **RDH-pod** peer one pod-hop away (sentinel `neuron_dev == −2`, `NEC_POD_MLA_DEV`), or a **network** peer across EFA (sentinel `neuron_dev == −1`). The classification loop in `alg_mesh_build_full_mesh` reads each peer's `neuron_dev`, sorts it into a `local` red-black tree, a `pod-root` red-black tree, or a `network-inter-node` list, computes a Hamming-distance hop metric (`popcount(rid_self ⊕ rid_peer)`) for local peers, and records the count of device-local peers (`src_devs`). A second pass bins ranks into a `std::unordered_map<mesh_group_types, std::vector<int>>` keyed by six roles — `SELF`, `INTRA_GROUP_ROOTS`, `INTER_GROUP_ROOTS`, `ALL_INTER_ROOTS`, `INTRA_GROUP_NEIGHBORS`, `INTRA_GROUP_MEMBERS` — and those six vectors are the source from which every event's source/destination neighbor group is sliced.

This page documents three things a reimplementer must reproduce: (1) the **peer classification + group binning** in `alg_mesh_build_full_mesh` — the three-way `neuron_dev` sentinel split, the two ordered trees, the `num_groups = num_net_inter_node_ranks + src_devs` accounting, and the `max_chbuf_space_per_group` budget; (2) the **five full-mesh subtypes** (`t0..t4`) as fixed event sequences and the `enc_mesh_event_type` each event carries; and (3) the **per-platform divergence** — the Trn2/Trn3 per-device initializer (`alg_mesh_initializer_pd`) and the NeuronSwitch-v1 initializer (`alg_mesh_initializer_switch`) that replace the full-mesh path when the topology is a pod or a switch fabric. The leaf event-emitters and the device emit (`encd_init_mesh_event`) are owned elsewhere ([enc Primitives](enc-primitives.md), [encd Overview](encd-overview.md)); this page owns the *schedule that calls them*.

For reimplementation, the contract is:

- **A peer's `neuron_dev` sentinel is the transport classifier, and the binning is asserted, not searched.** `neuron_dev == −1` ⇒ network/EFA peer (sets `mesh->use_net`); `neuron_dev == −2` (`NEC_POD_MLA_DEV`) ⇒ RDH-pod peer; any other value ⇒ a node-local peer whose ring-id and hop metric are read from the topology. The six `mesh_group_types` roles are filled in a fixed order, and the build asserts the cardinality invariants (`groups[ALL_INTER_ROOTS].size() > groups[INTER_GROUP_ROOTS].size()`, `num_net_inter_node_ranks > 0` on the net path, `(node_n == 1) || (use_net ⊕ enable_pod)`). A reimplementer reproduces the sentinel split and the bin invariants, not a cost model.
- **The schedule is up to five fixed subtypes, each an ordered event list, not a generated graph.** `t0` (degenerate 2-rank single-node) is `LOCAL_HNDSHK → REDUCE_COPY → SYNC`; `t1`/`t2`/`t4` are the `LOCAL_HNDSHK → GLOBAL_HNDSHK → BROADCAST → REDUCE_WRITE/REDUCE_COPY → SYNC` shapes; `t3` carries the `EVT_COPY_FROM_HOST` host-staging event for all-to-all-v. Each builder is an inlined `alg_full_mesh_build_subtype_tN(mesh, idx, groups&)` that fills `mesh_subtype[N].events[]`, sets `num_events`, and clamps `op_max_limit[ENC_ALLREDUCE]` against `enc_get_mesh_channel_buf_max_size()`. Reproduce the per-subtype event order and the limit clamp.
- **The composer flavor is selected by silicon family before any event is built, and the three flavors do not share schedule code.** `alg_mesh_build_subtypes` routes Trn2/Trn3 single-node-or-pod topologies to `alg_mesh_initializer_pd`, NeuronSwitch-v1 multi-rack to `alg_mesh_initializer_switch`, the simple `node_n != rank_n` case to `alg_mesh_build_full_mesh`, and the inf2-family `rank_n % 6 == 0` case to the grouped-mesh inline builder. A reimplementer must dispatch on family first; the full mesh is one flavor of four, not the universal path.

| | |
|---|---|
| **MESH entry** | `alg_mesh_init @0x135990` — builds device neighbors + 20 subtype DMA channels, then composes |
| **Subtype dispatcher** | `alg_mesh_build_subtypes @0x133cd0` (7350 B) — family/topology → one composer flavor |
| **Full-mesh builder** | `alg_mesh_build_full_mesh @0x125fb0` (13649 B) → `mesh_type = ENC_ALG_FULL_MESH(0)` |
| **Per-device (pod)** | `alg_mesh_initializer_pd` (160 B, vtable `off_BF6A28`) → `ENC_ALG_MESH_TRN2(2)` |
| **Switch fabric** | `alg_mesh_initializer_switch` (64 B, vtable `off_BF6BA8`) → `ENC_ALG_MESH_SWITCH(3)` |
| **Composer state** | `enc_alg_mesh` (61568 B) — `mesh_type@+0`, `group_id@+1036`, `num_groups@+1040`, `channel@+1048`, `mesh_subtype[20]@+1136`, `use_net@+61498` |
| **Schedule slot** | `enc_alg_mesh_subtype` (3016 B) — `events[64]@+0`, `num_events@+2560`, `drv_mesh@+2568`, `op_max_limit[13]@+2584` |
| **Event record** | `enc_mesh_event` (40 B) — `src_neighbor_grp@+0`, `dst_neighbor_grp@+16`, `valid@+32`, `evt_type@+36` |
| **Group map** | `std::unordered_map<mesh_group_types, std::vector<int>>` — six roles binned in pass 2 |
| **Device emit** | `encd_init_mesh_event` (one call per event) — see [encd Overview](encd-overview.md) |

> **CORRECTION (MESH-1) — `enc_pattern_t` is `{RING=0, MESH=1, INVALID=2}`, not `{RING=1, MESH=2}`.** An early collectives sweep recorded the device channel pattern enum with RING at 1. `enums.json` resolves it definitively for this image: `ENC_PATTERN_RING=0`, `ENC_PATTERN_MESH=1`, `ENC_PATTERN_INVALID=2` (the `enc_channel.pattern` field at `+4`). This is the *device channel* pattern (a 3-value enum), and it is distinct from the *algorithm* selector `enc_alg_type` (a 12-value enum, `MESH=2`) that `enc_cc_algorithm_allowed` uses to choose mesh-vs-ring. Confidence **HIGH** (`enums.json`, cross-checked against [Hierarchical and RDH](hierarchical-rdh.md) and [Algorithm Taxonomy](algorithm-taxonomy.md)).

---

## 1. Entry and Flavor Dispatch

### Purpose

`alg_mesh_init @0x135990` is the MESH-algorithm constructor: it binds the `enc_alg_mesh` state to its `enc_comm_info` and `nccl_comm_node`, asks the device side to build the `N·(N−1)` neighbor link set, initializes the 20 mesh-subtype DMA channels, and then calls the dispatcher that composes the actual event schedule. It is reached from `enc_init_comm @0x135d60`, the per-comm algorithm selector that runs `enc_cc_algorithm_allowed @0x108d30` over the mesh family (`MESH`, `SINGLE_STEP_MESH`, `TWO_STEP_POD_MESH`, `LATENCY_OPT_MESH`, `BW_OPT_MESH`) and the ring family (`RING`, `KANGARING`, `SINGLE_CYCLE_RING`, `INTRA_RDH`, `INTER_RDH`, `HIER`); if any mesh variant is admitted the comm takes the mesh path. RDH depends on mesh — the string *"…RDH construction will be skipped because it depends on Mesh."* (in `enc_init_comm`) gates it.

### Entry Point

```text
enc_init_comm (0x135d60)                         ── per-comm mesh-vs-ring selector
  └─ enc_cc_algorithm_allowed (0x108d30)         ── per-(comm,alg) admission gate
  └─ alg_mesh_init (0x135990)                     ── MESH constructor
       ├─ encd_alg_init_mesh_resources (0x24cf90) ── [boundary] device builds N·(N−1) neighbors
       ├─ encd_alg_init_mesh_subtype × 20         ── [boundary] one DMA channel per subtype slot
       ├─ alg_mesh_build_subtypes (0x133cd0)      ── family/topology dispatch → compose events
       │    ├─ alg_mesh_build_full_mesh (0x125fb0)         ── ENC_ALG_FULL_MESH(0)   §2
       │    ├─ alg_mesh_initializer_pd::__build_cc_ops     ── ENC_ALG_MESH_TRN2(2)   §4
       │    ├─ alg_mesh_initializer_switch::initialize     ── ENC_ALG_MESH_SWITCH(3) §4
       │    └─ <grouped-mesh inline builder>               ── ENC_ALG_GROUPED_MESH(1)
       └─ (use_net && !enable_pod) ─ ncclExpandConnectivity(ENC_CONNECTIVITY_MESH=0) (0x1c1530)
                                     configure_net_connectors (0xfa1f0)  ── [boundary] channel wiring
```

### Algorithm — the family/topology dispatch

`alg_mesh_build_subtypes @0x133cd0` reads the instance family (`nrt_is_trn2_trn3_family` / `nrt_is_neuron_switch_v1_family`) and the topology shape (`node_n`, `rank_n`, `enable_pod`) and routes to exactly one composer. The routing is a cascade of asserted topology predicates, not a search.

```c
// alg_mesh_build_subtypes @0x133cd0  (enc.cc) — dispatch only; each branch composes events
function alg_mesh_build_subtypes(mesh):                            // 0x133cd0
    ci   = mesh->ci;                                               // enc_alg_mesh+61512
    info = nrt_get_instance_info();

    if nrt_is_trn2_trn3_family():
        if ci->node_n == ci->rank_n                               // pure intra-node …
           || (ci->node_n == 4 && ci->enable_pod)                 // … or a 4-node pod
           || multi_node:
            mesh->mesh_type = ENC_ALG_MESH_TRN2;                  // (2)
            pd = new alg_mesh_initializer_pd(mesh, ci);           // vtable off_BF6A28
            pd.__init_mesh_properties();                          // deviceid→rankid / hop maps
            pd.__build_cc_ops();   // run twice: increase_dma_for_larger_message_sizes 0 then 1
            pd.__append_fn_barr(EVT_FUNCTION_BARRIER_FIRST_COLL); // (4)
            pd.__append_fn_barr(EVT_FUNCTION_BARRIER_LAST_COLL);  // (5)
            if mesh->build_rdh: pd.__build_rdh_ops();             // RDH stage — see hierarchical-rdh.md
            return;
        else:                                                     // node_n != rank_n, not pod-4
            return alg_mesh_build_full_mesh(mesh);                // ENC_ALG_FULL_MESH(0)  §2

    if nrt_is_neuron_switch_v1_family()
       && ci->local_rack_rank_n > 1 && peers_differ_in_rid_or_pod_node:
        mesh->mesh_type = ENC_ALG_MESH_SWITCH;                    // (3)
        mesh->max_chbuf_space_per_rank = buf_max / ci->rank_n;
        sw = new alg_mesh_initializer_switch(mesh, ci);           // vtable off_BF6BA8
        return sw.initialize();                                   // §4

    switch instance_family:
      case 2, 3, 7: return alg_mesh_build_full_mesh(mesh);        // ENC_ALG_FULL_MESH
      case 4:                                                     // inf2 family
        if ci->rank_n in {2, 4, 8}: return alg_mesh_build_full_mesh(mesh);
        if ci->rank_n % MESH_INF2_MIN_GROUP_SIZE == 0:            // == 6
            assert(ci->node_n == 1);                              // "ci->node_n == 1"
            mesh->mesh_type = ENC_ALG_GROUPED_MESH;               // (1) — inline grouped builder
            return build_grouped_mesh(mesh);                      // union-find over peer rids
      default:
        return NRT_INVALID;   // "Mesh algo untested with %d ranks on this instance type"
```

> **GOTCHA —** the dispatcher decides `mesh_type` *before* `alg_mesh_build_full_mesh` runs, but `alg_mesh_build_full_mesh` **overwrites** `mesh->mesh_type = ENC_ALG_FULL_MESH(0)` itself at `@0x125fb0:660` once its classification confirms a flat full mesh. So `ENC_ALG_FULL_MESH` is set by the builder, not the dispatcher, while `ENC_ALG_MESH_TRN2`/`MESH_SWITCH`/`GROUPED_MESH` are set by the dispatcher before delegating. A reimplementer who sets `mesh_type` in the dispatcher for all four flavors gets the right value for three and a redundant (harmless) write for the fourth — but must not *read* `mesh_type` between dispatch and the full-mesh builder, because it is not yet `FULL_MESH` there.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_init_comm` | `0x135d60` | per-comm mesh-vs-ring algorithm selector (10644 B) | HIGH |
| `enc_cc_algorithm_allowed` | `0x108d30` | per-`(comm_type, alg)` admission gate (1605 B) | HIGH |
| `alg_mesh_init` | `0x135990` | MESH constructor: device neighbors + 20 subtype channels (966 B) | HIGH |
| `alg_mesh_build_subtypes` | `0x133cd0` | family/topology dispatch to the four composers (7350 B) | HIGH |
| `encd_alg_init_mesh_resources` | `0x24cf90` | device builds the `N·(N−1)` neighbor set ([encd Overview](encd-overview.md)) | HIGH |
| `ncclExpandConnectivity` | `0x1c1530` | enable network-mesh connectivity for net/EFA peers (`ENC_CONNECTIVITY_MESH=0`) | HIGH |
| `configure_net_connectors` | `0xfa1f0` | wire the channel descriptor's net send/recv connectors | HIGH |
| `enc_get_mesh_channel_buf_max_size` | `0xf9920` | per-channel buffer budget (subtype limit clamp source) | HIGH |

---

## 2. Full-Mesh Build: Peer Classification and Group Binning

### Purpose

`alg_mesh_build_full_mesh @0x125fb0` is the canonical composer: it turns the parsed peer table into the six-role group map and then emits the subtype event schedule from it. It is two passes plus a header-finalize plus five inlined subtype builders. The first pass classifies peers by transport and counts them; the second pass bins ranks into `mesh_group_types` roles; the finalize computes `num_groups` and the per-group channel-buffer budget; and the subtype builders (`alg_full_mesh_build_subtype_t0..t4`) slice events out of the group map. The whole function runs once per comm at NEFF-load time.

### Algorithm — pass 1: peer classification

The classification loop walks `ci->peers[0 .. rank_n−1]` and sorts each peer by its `neuron_dev` sentinel into one of three buckets: a network-inter-node list, a pod-root red-black tree (keyed by `pod_node_id`), or a local red-black tree (keyed by ring-id). For local peers it also computes a Hamming-distance hop metric and remembers the *immediate sibling* — the peer whose `neuron_dev` differs from self in exactly the low bit (`(nec_dev ⊕ 1) == peer_dev`), the directly-wired NeuronLink neighbor.

```c
// alg_mesh_build_full_mesh @0x125fb0  — pass 1 (lines 271-449)
// classify every peer by transport; build the local + pod-root ordered sets
function classify_peers(mesh, ci):                                // 0x125fb0
    nec_dev   = ci->neuron_dev;                                   // this rank's device
    rid_self  = encd_get_rid_from_nec_dev(nec_dev, nec_dev);      // 0x234e80
    mesh->use_net = 0;
    src_devs  = 0;                  // count of node-local peers
    imm_nbr_rank = -1;             // the (nec_dev ^ 1) direct sibling, if any
    local_tree = {};  pod_root_tree = {};  net_inter = [];        // two _Rb_tree + a list

    for i in 0 .. ci->rank_n:
        dev = ci->peers[i].neuron_dev;                            // enc_peer_info+0
        if dev == -1:                                             // network / EFA peer
            net_inter.push_back(i);
            mesh->use_net = 1;
            continue;
        if dev == -2:                                             // NEC_POD_MLA_DEV — RDH-pod peer
            pod_root_tree.insert(ci->peers[i].pod_node_id);       // enc_peer_info+12
            assert(i != ci->rank);                                // "i != ci->rank"
            ++src_devs;                                           // (pod peer still group-local)
            continue;
        // else: a node-local peer reached over the on-package NeuronLink mesh
        rid = encd_get_rid_from_nec_dev(nec_dev, dev);            // 0x234e80
        insert_ret = local_tree.insert(rid);
        assert(insert_ret.second);                               // "insert_ret.second" (no dup rid)
        hop = popcount(rid_self ^ rid);                          // Hamming distance to peer
        store_hop_metric(i, hop);
        if (nec_dev ^ 1) == dev: imm_nbr_rank = rank_of(i);      // direct NeuronLink sibling
        ++src_devs;
    assert(self_group_id != -1);                                 // "self_group_id != -1" (node_n>1)
    mesh->group_id = self_group_id;                              // enc_alg_mesh+1036
```

### Algorithm — pass 2: group binning and finalize

The second pass walks the same peers and pushes each rank into the `mesh_group_types` map under one of six roles, then the header is finalized: `num_groups` is the sum of the inter-node rank count and the device-local count, and the per-group channel budget is the total mesh channel buffer divided by `num_groups`.

```c
// alg_mesh_build_full_mesh @0x125fb0  — pass 2 + finalize (lines 451-665)
function bin_groups_and_finalize(mesh, ci):                       // 0x125fb0
    groups = {};   // unordered_map<mesh_group_types, vector<int>>
    for role in [SELF, INTRA_GROUP_ROOTS, INTER_GROUP_ROOTS,
                 ALL_INTER_ROOTS, INTRA_GROUP_NEIGHBORS]:         // v30 = 0..4
        for r in candidate_ranks(role):
            dev = ci->peers[r].neuron_dev;
            if dev == -2:                                         // RDH-pod membership
                assert(dev != NEC_POD_MLA_DEV || imm_nbr_rank == -1);
                groups[role].push_back(r);
            else if dev == -1 || encd_direct_link_neighbor(nec_dev, nec_dev, dev):
                groups[role].push_back(r);                        // direct-link or net peer

    use_net = mesh->use_net;
    assert((ci->node_n == 1) || (use_net ^ ci->enable_pod));     // exactly one of net / pod off-node

    if use_net:                                                  // round-robin inter-node ordering
        num_net = net_inter.size();
        assert(num_net > 0);                                     // "num_net_inter_node_ranks > 0"
        for k in 0 .. num_net:
            order = (ci->rank + k) % num_net;                    // rotate so every rank leads once
            groups[ALL_INTER_ROOTS].push_back(net_inter[order]);
        assert(groups[ALL_INTER_ROOTS].size()
               > groups[INTER_GROUP_ROOTS].size());

    mesh->mesh_type = ENC_ALG_FULL_MESH;                         // (0) — set HERE, not by dispatcher
    mesh->trn2.devid_to_rankid[0] = src_devs;                    // #device-local peers
    mesh->num_groups = num_net + src_devs;                       // enc_alg_mesh+1040
    mesh->max_chbuf_space_per_group =
        enc_get_mesh_channel_buf_max_size() / mesh->num_groups;  // enc_alg_mesh+61456
```

> **QUIRK —** `src_devs` counts the **pod-root peer too**, not just on-package locals. In pass 1 the `dev == −2` (RDH-pod) branch increments `src_devs` alongside the genuine on-package case — so `num_groups = num_net_inter_node_ranks + src_devs` folds RDH-pod peers into the "local" group count even though they are a pod-hop away. The classifier's three buckets (net / pod / local) are *not* the same partition as `num_groups`'s two terms (net-inter vs everything-else). A reimplementer who treats `src_devs` as "on-package device count" and computes `num_groups` from a literal local-peer count under-counts the groups whenever a pod peer is present, and over-shares the channel buffer (`max_chbuf_space_per_group` comes out too large, and the `op_max_limit[ENC_ALLREDUCE] <= enc_get_mesh_channel_buf_max_size()` assert can then trip).

> **GOTCHA —** the immediate-sibling rule and the pod-membership assert interact. `imm_nbr_rank` is set only when a peer's `neuron_dev` is `self_dev ⊕ 1` (the direct NeuronLink sibling); pass 2 then asserts `ci->peers[r].neuron_dev != NEC_POD_MLA_DEV || imm_nbr_rank == −1` (`enc.cc:0x1CCA`). The meaning: a rank cannot simultaneously have a direct on-package sibling **and** be reached as an RDH-pod peer — the two are mutually exclusive topology states. A reimplementer who sets `imm_nbr_rank` from any reduced hop distance (rather than the exact `⊕ 1` bit test) trips this assert on legitimate pod topologies, because a pod peer at Hamming distance 1 that is *not* the package sibling would falsely populate `imm_nbr_rank`.

### The `mesh_group_types` roles

The six roles are the partition of ranks the subtype builders read. The enum is verbatim from `enums.json` (the key type of the `unordered_map<mesh_group_types, vector<int>>` the builders thread); the *meaning* column is from how each role is sliced in `alg_full_mesh_build_subtype_t0..t4` and the leaf emitters.

| Value | Name | Meaning (how the subtype builders use it) | Confidence |
|---|---|---|---|
| 0 | `SELF` | the local rank alone — the source of a sync / reduce-into-self fold | HIGH |
| 1 | `INTRA_GROUP_ROOTS` | the per-group root ranks **within** this rank's group (intra-group reduce leaders) | HIGH |
| 2 | `INTER_GROUP_ROOTS` | the root ranks of the **other** groups this rank pairs with for inter-group transfer | HIGH |
| 3 | `ALL_INTER_ROOTS` | the full inter-group root set incl. self's group root; on the net path, round-robin ordered | HIGH |
| 4 | `INTRA_GROUP_NEIGHBORS` | the direct-link (NeuronLink-reachable) neighbors inside the group — handshake fan-out | HIGH |
| 5 | `INTRA_GROUP_MEMBERS` | the full intra-group membership — the broadcast / reduce-copy destination set | HIGH |

> **NOTE —** the build asserts `groups[ALL_INTER_ROOTS].size() > groups[INTER_GROUP_ROOTS].size()` (`enc.cc:0x1CEF`) precisely because `ALL_INTER_ROOTS` includes this rank's *own* group root while `INTER_GROUP_ROOTS` is the *other* groups only. The strict `>` (not `≥`) is the structural guarantee that self's group is always represented in the all-roots set — a reimplementer who builds `ALL_INTER_ROOTS` as the union of foreign roots (omitting self's root) makes the two sets equal and trips the assert.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `alg_mesh_build_full_mesh` | `0x125fb0` | classify peers, bin groups, emit `t0..t4` (13649 B) | HIGH |
| `alg_append_fn_barrier_event` | `0x125880` | append a `FUNCTION_BARRIER` event to every populated subtype (1449 B) | HIGH |
| `encd_get_rid_from_nec_dev` | `0x234e80` | resolve a peer's ring-id from its `neuron_dev` (hop-metric source) | HIGH |
| `encd_direct_link_neighbor` | `0x2365e0` | NeuronLink link-existence test (pass-2 membership gate) | HIGH |
| `populate_neighbor_group` | `0x10d3d0` | fill an event's `{src,dst}_neighbor_grp` (copy-from-host path) | HIGH |

---

## 3. The Five Full-Mesh Subtypes

### Purpose

A *subtype* is one schedule shape: a `mesh_subtype[N]` slot holding an ordered `enc_mesh_event[64]` array, a `num_events` count, a `drv_mesh` device handle, and a per-`enc_op_type` limit table (`op_max_limit[13]`) that gates which message sizes the subtype handles. `alg_mesh_build_full_mesh` composes up to five subtypes — each an inlined `alg_full_mesh_build_subtype_tN(mesh, idx, groups&)` that is selected by topology size (2-rank vs general, single-node vs `node_n == 2`). The events are emitted by the leaf composers ([enc Primitives](enc-primitives.md)); this section documents the *order* in which a subtype builder calls them and the `enc_mesh_event_type` each call stamps.

### The subtype event schedules

Each subtype is `LOCAL_HNDSHK`-first (every collective opens with an intra-group handshake) and `SYNC`-last (every collective closes with a fold-to-self). The middle is the per-shape reduce / broadcast body. The event types are verbatim from the `enc_mesh_event_type` enum (`enums.json`) and the call sites in `alg_mesh_build_full_mesh @0x125fb0`.

| Subtype | When chosen | Event sequence (in order) | `num_events` | Flags / limit | Confidence |
|---|---|---|---|---|---|
| `t0` | `node_n == 1 && rank_n == 2` | `LOCAL_HNDSHK(2)` → `REDUCE_COPY(8)` → `SYNC(0)` | 3 | `is_single_step_mesh=1`; `op_max_limit[ALLREDUCE]=0x10000000` | HIGH |
| `t1` | general single-node mesh | `LOCAL_HNDSHK(2)` → `GLOBAL_HNDSHK(1)` → `INTRA_GRP_BRDCST(7)` → `INTER_GRP_BRDCST(3)` → `REDUCE_WRITE(10)` → `SYNC(0)` | 6 | `is_single_step_mesh=1`; `op_max_limit[ALLREDUCE]` = `(rank_n==2) ? per_group : min(0x8000, per_group)` | HIGH |
| `t2` | multi-group mesh | `LOCAL_HNDSHK(2)` → `GLOBAL_HNDSHK(1)` → `INTER_GRP_BRDCST(3)` → `REDUCE_COPY(8)` → `REDUCE_WRITE(10)` → `[REDUCE_LOCAL_HNDSHK(6)]` → `INTER_GRP_BRDCST_2(11)` → `SYNC(0)` | 8–9 | `op_max_limit[ALLREDUCE]` clamped to `≤ enc_get_mesh_channel_buf_max_size()` | HIGH |
| `t3` | `node_n == 2` (all-to-all-v) | `LOCAL_HNDSHK(2)` → `GLOBAL_HNDSHK(1)` → `INTER_GRP_BRDCST(3)` → **`COPY_FROM_HOST(22)`** → `SYNC(0)` | 5 | host-staged via `populate_neighbor_group` + direct `encd_init_mesh_event` | HIGH |
| `t4` | `node_n == 2` (general) | `LOCAL_HNDSHK(2)` → `GLOBAL_HNDSHK(1)` → `INTER_GRP_BRDCST(3)` → `REDUCE_COPY(8)` → `REDUCE_WRITE(10)` → `SYNC(0)` | 5–6 | per-group limit clamp | HIGH |

> **NOTE —** the precise event *order* and the `num_events` counts above are read from a single decompiled pass of `alg_mesh_build_full_mesh @0x125fb0` (the `t0/t1/t2` call sites at lines 692/834/1019, `t3` at 1200, `t4` at 1576). The broad shape — handshake → broadcast → reduce → sync — is **HIGH**; the exact event-index assignment within `t2` (where an optional `REDUCE_LOCAL_HNDSHK` slot makes `num_events` 8 or 9) is **MEDIUM**, because the builder reuses the `groups` map `operator[]` heavily and the conditional event makes one index float.

### Algorithm — a representative subtype builder (t1)

`alg_full_mesh_build_subtype_t1` is the canonical six-event single-node mesh. It opens with a local handshake into the intra-group neighbors, a global handshake across group roots, broadcasts the data both intra- and inter-group, reduce-writes the result back, and closes with a sync. Each call slices its source/destination ranks from the `groups` map and stamps an event index; the builder then sets `num_events` and clamps the all-reduce limit.

```c
// alg_full_mesh_build_subtype_t1(mesh, idx, groups&)  inlined in 0x125fb0 (lines 834-921)
function build_subtype_t1(mesh, idx, groups):
    st = &mesh->mesh_subtype[idx];
    ci = mesh->ci;
    memset(st->op_max_limit, 0, 13*8); memset(st->op_max_limit_sbuf, 0, 13*8);

    // evt 0: intra-group handshake (src = SELF, dst = INTRA_GROUP_NEIGHBORS)
    alg_full_mesh_local_hndshk(st, ci, 0, EVT_LOCAL_HNDSHK, groups[SELF], groups[INTRA_GROUP_NEIGHBORS]);
    // evt 1: cross-group handshake (evt idx hard-coded to 1 in the .constprop clone)
    alg_full_mesh_global_hndshk(st, ci, groups[INTER_GROUP_ROOTS], groups[ALL_INTER_ROOTS]);
    // evt 2: broadcast within the group  (wait_val = 1, no proxy)
    ___alg_full_mesh_broadcast(st, ci, 2, EVT_INTRA_GRP_BRDCST, groups[SELF], groups[INTRA_GROUP_MEMBERS], 1, 0);
    // evt 3: broadcast across groups     (fanout = src_devs, proxy iff use_net)
    ___alg_full_mesh_broadcast(st, ci, 3, EVT_INTER_GRP_BRDCST, groups[INTER_GROUP_ROOTS], groups[ALL_INTER_ROOTS],
                               mesh->trn2.devid_to_rankid[0], mesh->use_net);
    // evt 4: write the reduced result back
    alg_full_mesh_reduce_write(st, ci, 4, groups[INTRA_GROUP_MEMBERS], groups[SELF]);
    // evt 5: fold-to-self sync (use_dma=1)
    _alg_full_mesh_sync(st, ci, 5, groups[SELF], /*use_dma=*/1, groups[INTRA_GROUP_MEMBERS], groups[SELF], 1);

    st->num_events = 6;
    st->is_single_step_mesh = 1;
    limit = (ci->rank_n == 2) ? mesh->max_chbuf_space_per_group
                              : min(0x8000, mesh->max_chbuf_space_per_group);
    st->op_max_limit[ENC_ALLREDUCE] = limit;                      // op_max_limit[1]
    assert(st->op_max_limit[ENC_ALLREDUCE] <= enc_get_mesh_channel_buf_max_size());
```

### Algorithm — the copy-from-host event (t3) and the device handoff

`t3` is the one subtype that touches host memory: its `events[3]` is an `EVT_COPY_FROM_HOST` whose neighbor groups are filled directly by `populate_neighbor_group` (not the usual leaf emitter), then handed straight to `encd_init_mesh_event`. This is the all-to-all-v staging path, where variable-size sends are copied through a host buffer. The device emit is owned by [encd Overview](encd-overview.md); the composer's job ends at filling the event and calling the emitter.

```c
// alg_full_mesh_copy_from_host(st, ci, evt_id=3)  inlined in t3 (lines 1309-1385)
function build_t3_copy_from_host(mesh, st, ci):
    assert(st->mesh);                                            // "mesh_subtype->mesh"
    st->events[3].evt_type = EVT_COPY_FROM_HOST;                 // (22)
    populate_neighbor_group(&st->events[3].src_neighbor_grp, src_ranks, ci, &src_devs);   // 0x10d3d0
    populate_neighbor_group(&st->events[3].dst_neighbor_grp, dst_ranks, ci, &dst_devs);
    md = encd_mesh_evt_metadata{ valid=1, valid_direct_trigger_evt=1, wait_val=dst_ranks.size() };
    encd_init_mesh_event(st->drv_mesh, /*evt_id=*/3, EVT_COPY_FROM_HOST,
                         src_devs, n_src, dst_devs, n_dst, 0, 0, md, 0);   // device emit -> encd
```

After all subtypes are built, `alg_mesh_build_full_mesh` appends two function barriers across every populated subtype and tears down its scratch trees:

```c
// closing of alg_mesh_build_full_mesh (lines 1728-1752)
    alg_append_fn_barrier_event(mesh, &groups, EVT_FUNCTION_BARRIER_FIRST_COLL);  // (4)
    alg_append_fn_barrier_event(mesh, &groups, EVT_FUNCTION_BARRIER_LAST_COLL);   // (5)
    // cleanup: erase the local rid _Rb_tree, the pod-root _Rb_tree, clear the groups hashtable
```

> **QUIRK —** `t0` is single-node 2-rank only and skips the broadcast entirely. The 2-rank single-node mesh has no inter-group transfer and no fan-out — both ranks see each other directly — so its schedule collapses to `LOCAL_HNDSHK → REDUCE_COPY → SYNC` (3 events) with `op_max_limit[ALLREDUCE] = 0x10000000` (256 MiB, effectively unclamped) and `is_single_step_mesh = 1`. A reimplementer who routes 2-rank single-node through the general `t1` path builds a six-event schedule whose `GLOBAL_HNDSHK` and `INTER_GRP_BRDCST` events have empty inter-group neighbor sets (`valid = 0`), wasting two event slots and two semaphore allocations per collective.

> **GOTCHA —** `wait_val` is the destination-group size, and it is the per-peer semaphore count a peer waits on — but only the *count* is set here; the actual device semaphore threshold lives behind `encd_init_mesh_event`. Each leaf sets `encd_mesh_evt_metadata.wait_val = dst_group.size()` (and `proxy_wait_val = 2` when `use_net`), asserting `wait_val > 0` on the net path. A reimplementer who reads `wait_val` as a literal byte count or a fold count (it is neither — it is the number of distinct destination ranks) mis-sizes every mesh semaphore. The metadata fields `valid_direct_trigger_evt` vs `valid_dma_trigger_evt` further select whether the device arms a direct trigger or a DMA-completion trigger; the composer sets these per event-type, the device acts on them.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `alg_full_mesh_local_hndshk` | `0x10d5e0` | `EVT_LOCAL_HNDSHK` event: src=self, dst=intra neighbors | HIGH |
| `alg_full_mesh_global_hndshk` | `0x10edf0` | `EVT_GLOBAL_HNDSHK` event (evt idx 1, `.constprop` clone) | HIGH |
| `___alg_full_mesh_broadcast` | `0x10df30` | broadcast event with caller-supplied evt_type + fanout / proxy counts | HIGH |
| `alg_full_mesh_reduce_cpy` | `0x10d930` | `EVT_REDUCE_COPY` event (asserts `src_group.size()==2`) | HIGH |
| `alg_full_mesh_reduce_write` | `0x10e1a0` | `EVT_REDUCE_WRITE` event (`wait_val=src_group.size()`) | HIGH |
| `_alg_full_mesh_sync` | `0x10dbd0` | `EVT_SYNC` event; picks DMA-trigger vs proxy by peer transport | HIGH |
| `populate_neighbor_group` | `0x10d3d0` | fill a neighbor group for the copy-from-host event | HIGH |

---

## 4. Per-Platform Divergence: Pod and Switch Initializers

### Purpose

The full-mesh builder of §2–§3 is the flat-topology composer. When the topology is a Trn2/Trn3 pod or a NeuronSwitch-v1 fabric, `alg_mesh_build_subtypes` instead instantiates a stateful builder object — `alg_mesh_initializer_pd` (160 B) or `alg_mesh_initializer_switch` (64 B) — and calls its `__build_cc_ops` / `initialize`. These do not reuse the full-mesh subtype builders; they compose their own event sequences keyed to physical link topology (RMTV / D2D / PCIe hops for the pod; axis broadcast for the switch). The two share the same `enc_mesh_event[]` / `enc_alg_mesh_subtype[20]` output structures and the same `encd_init_mesh_event` sink, so a reimplementer reuses the schedule *containers* but writes three distinct schedule *producers*.

### The pod initializer — `alg_mesh_initializer_pd`

`alg_mesh_initializer_pd` is a builder bound to one `enc_alg_mesh` + `enc_comm_info`, carrying a `deviceid_to_rankid_map`, a `deviceid_to_hop_count_map` (the broadcast-tree hop levels), an `intra_chip_neighbor_rank_ids` vector, and a `virtual_core_size` (1 = full vNeuronCore, 2 = half). Its top entry `__build_cc_ops @0x114ad0` builds the per-collective subtypes — allreduce, reduce-scatter, allgather, alltoall, single-step-mesh, and the 2-step-pod-mesh proxies. Each per-op builder scans `mesh_subtype[0..19]` for the first free slot (`num_events == 0`, cap `ENC_MAX_MESH_SUBTYPES=20`), then chains handshake / broadcast / reduce / dma_sync leaves; the deeper RDH/RH/RD step builders are owned by [Hierarchical and RDH](hierarchical-rdh.md).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| *(base `alg_mesh_initializer`)* | +0 | `{vptr, mesh@8, ci@16, curr_subtype_id@24}` | the bound state | HIGH |
| `deviceid_to_rankid_map` | +32 | `std::map<int,int>` | device-id → comm rank | HIGH |
| `deviceid_to_hop_count_map` | +80 | `std::map<int,int>` | broadcast-tree hop levels (1..4) | HIGH |
| `intra_chip_neighbor_rank_ids` | +128 | `std::vector<int>` | the local handshake / broadcast fan-out set | HIGH |
| `virtual_core_size` | +152 | `uint32_t` | 1 = DMA-trigger path, 2 = proxy / 2-dev path | HIGH |
| `self_relative_worker_id` | +156 | `uint8_t` | sengine-relative worker id (reduce filter) | HIGH |
| `increase_dma_for_larger_message_sizes` | +157 | `bool` | second `__build_cc_ops` pass flag | HIGH |
| `is_trn3` | +158 | `bool` | selects Trn3 chunk counts in RDH steps | HIGH |

### The switch initializer — `alg_mesh_initializer_switch`

`alg_mesh_initializer_switch::initialize @0x1ff7d0` is the NeuronSwitch-v1 multi-rack composer (`ENC_ALG_MESH_SWITCH(3)`). It decomposes the fabric into inter-axis and intra-axis broadcast trees and a one-rank-per-chip layout, and offers latency-optimized vs bandwidth-optimized variants (`build_single_hop_latency_opt_*` / `build_single_hop_bandwidth_opt_*`) that correspond to `ENC_ALG_LATENCY_OPT_MESH(9)` / `ENC_ALG_BW_OPT_MESH(10)`. Its object is only 64 B — `{base alg_mesh_initializer @0, nrt_instance_info_t @28}` — because all per-rack state is read live from the instance info rather than cached.

> **QUIRK —** the pod and switch initializers select the `enc_mesh_event_type` from a *much wider* slice of the 53-value enum than the full mesh does. The full-mesh subtypes use only the common events (`EVT_SYNC=0` … `EVT_COPY_FROM_HOST=22`); the pod RDH/RH/RD step builders reach into `EVT_RH_STEP_0=23` … `EVT_RDH_LOCAL_PEER_HANDSHAKE=52`, and the switch path uses the 2-dev (`EVT_2DEV_BRDCST=20` / `EVT_2DEV_HNDSHK=21`) and axis-broadcast events. A reimplementer who hard-codes the full-mesh event vocabulary and assumes the pod/switch flavors reuse it will be missing 30 event types — the entire RDH step family lives only in the pod path. The event *record* (`enc_mesh_event`, 40 B) and the device emit (`encd_init_mesh_event`) are identical across all three flavors; only the event *type* vocabulary and the *source of the neighbor groups* (group map vs hop-count map vs axis decomposition) differ.

> **GOTCHA —** `__build_cc_ops` runs **twice** — once with `increase_dma_for_larger_message_sizes = 0` and once with `= 1` — and each pass allocates *new* subtype slots. This is why the pod path can consume far more than five of the 20 subtype slots: two passes × several collectives × multiple device-count variants. The `op_max_limit[ENC_ALLREDUCE]` and `op_min_limit` tables on each subtype are the size cutoffs that select, at enqueue time, which of the two DMA-tuning variants handles a given message. A reimplementer who builds a single pass mis-handles large messages (the `increase_dma` variant never gets built), and one who lets the two passes share subtype slots overwrites the small-message schedule with the large-message one.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `alg_mesh_initializer_pd::initialize` | `0x145940` | pod builder entry → `__build_cc_ops` + `__build_rdh_ops` (196 B) | HIGH |
| `alg_mesh_initializer_pd::__build_cc_ops` | `0x114ad0` | per-collective subtype orchestrator (runs twice) (344 B) | HIGH |
| `alg_mesh_initializer_pd::__init_mesh_properties` | `0x108780` | build `deviceid_to_rankid` / hop-count maps | HIGH |
| `alg_mesh_initializer_pd::__append_fn_barr` | `0x114c30` | append FIRST/LAST_COLL barrier across populated subtypes | HIGH |
| `alg_mesh_initializer_pd::__get_direct_link_neighbors` | `0x109460` | direct-link (NeuronLink peer) neighbor enumeration | HIGH |
| `alg_mesh_initializer_switch::initialize` | `0x1ff7d0` | NeuronSwitch-v1 multi-rack composer entry (77 B) | HIGH |
| `alg_mesh_initializer_switch::build_inter_axis_broadcast` | `0x20ac40` | inter-axis broadcast tree event builder | HIGH |
| `alg_mesh_initializer_switch::build_one_rank_per_chip_all_gather` | `0x200b20` | one-rank-per-chip all-gather schedule | HIGH |
| `~alg_mesh_initializer_pd` (D1 / D0) | `0x13ae10` / `0x13aea0` | free hop / rank maps + neighbor vector; `operator delete(this, 160)` | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `alg_mesh_init` (`@0x135990`) | the MESH constructor that drives this composer; builds device neighbors + 20 channels |
| `enc_cc_algorithm_allowed` (`@0x108d30`) | admits the mesh family in `enc_init_comm`, selecting the mesh path over ring |
| `enc_mesh_primitive` (`@0x1a9da0` ctor) | consumes the `mesh_subtype[].events[]` this composer builds and emits `cc_op` streams |
| `disjoint_set` / `connected_components` (`@0x141dc0` / `@0x142fb0`) | union-find over peer rids for the `ENC_ALG_GROUPED_MESH` inline builder ([Topology Partitioning](topology-partition.md)) |
| `encd_init_mesh_event` | the device-side DMA + semaphore emit each event bottoms out into ([encd Overview](encd-overview.md)) |

## Cross-References

- [enc Primitives (Send/Recv Leaves)](enc-primitives.md) — the leaf event emitters (`alg_full_mesh_*`, `enc_primitive_mesh`) whose calls this page sequences into subtypes
- [Hierarchical and RDH Composition](hierarchical-rdh.md) — shares the `alg_mesh_initializer_pd`; owns the pod RDH/RH/RD step builders and the 2-step pod-mesh proxy this page only names
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — owns `encd_init_mesh_event` (the per-event DMA + semaphore wiring) and the `encd_alg_init_mesh_resources` neighbor build; this page stops at the call shape
- [The 148-Byte Ring Channel Descriptor](channel-descriptor.md) — the `enc_channel` (`enc_alg_mesh.channel @+1048`) the mesh path configures via `configure_net_connectors`; `enc_pattern_t = {RING=0, MESH=1, INVALID=2}`
- [Topology Partitioning (Union-Find)](topology-partition.md) — the `disjoint_set` / `connected_components` grouping the `ENC_ALG_GROUPED_MESH` path uses to coalesce ranks into groups of 6
- [back to index](../index.md)
