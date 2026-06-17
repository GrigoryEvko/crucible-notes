# Comm Context and Bootstrap

> *All addresses on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`). The ELF is **not** stripped; full C++ symbols and DWARF are present, and the host-side composer source TU is `/opt/workspace/KaenaRuntime/enc/enc.cc`. `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA. Other versions will differ.*
> *Evidence grade: **Confirmed (decompile- and DWARF-anchored)** — the bootstrap field-by-field population is read line-by-line from `enc_init_global_comm @0xff430`; the two-level MD5 op-signature from `enc_calculate_signature @0x108220` + `md5_enc_op_args @0xf6870`; the per-NC node bootstrap (binary-tree sig validation + hypercube unique-id broadcast) from `get_nccl_comm @0x12b360`; every struct offset is verbatim from `structures.json` (DWARF). · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

Before a NeuronCore can compile a single collective into an on-device program, it must answer two questions that no per-op composer can: *who is in the world?* and *does my copy of the program match everyone else's?* This page owns both answers. The first is the **global communicator** — `enc_glb_comm`, a 564 408-byte per-`(nec_dev, ctx_id)` object that pins this rank's place in the collective world (global device id, world size, pod identity, the libnccom handle, and the intra-node bananaphone IPC rings) — bootstrapped once per process by `enc_init_global_comm @0xff430`. The second is the **per-rank op-signature**: a 16-byte MD5 over every replica group's posted ops, written to `enc_context+992` by `enc_calculate_signature @0x108220`, that ranks exchange and compare during communicator setup so that a recompile on any one rank that desynchronizes the collective schedule is caught at load time rather than corrupting a reduction at runtime.

The reference frame is an NCCL communicator's lifecycle, split across the two-binary Neuron boundary. In stock NCCL one `ncclCommInitRank` does identity assignment, unique-id exchange, and transport setup in one call. In the Neuron stack that work is staged across libnrt's `enc_glb_comm` (identity + pod topology + the once-per-process libnccom comm allocation) and a **per-NeuronCore `enc_nccl_comm_node`** that `get_nccl_comm @0x12b360` allocates and bootstraps lazily — the first time a replica group with that exact `bootstrap_participants` set is initialized. The unique-id distribution and the signature reconciliation both run over the libnccom bootstrap socket (`ncclBootstrapSend`/`ncclBootstrapRecv`, reached over the `dlsym`'d `neuron*` ABI), but the *schedule* of those exchanges — a binary-tree AND-reduce for the signature, a hypercube fan-out for the unique id — lives in libnrt, not the NCCL fork.

This page documents (1) the `enc_glb_comm` and `enc_nccl_comm_node` structures a reimplementer must reproduce, (2) the `enc_init_global_comm` bootstrap sequence field-by-field, (3) how a NEFF-embedded replica group is parsed and its `bootstrap_participants` become the comm-pool key, (4) the two-level MD5 op-signature computation, and (5) the per-NC node bootstrap (unique-id generation → binary-tree signature validation → hypercube unique-id broadcast → `ncclInitComm` → local barrier). The global-comm *object* and its bootstrap are owned here; the per-op device step loop is [Ring Scheduling](ring-scheduling.md), the libnrt↔libnccom ABI is [The libnrt ↔ libnccom Boundary](nccl-boundary.md), and the host-side NCCL topology / ring-order construction is [Communicator Init and Bootstrap](../nccom/comm-init.md).

For reimplementation, the contract is:

- **The global communicator** — `enc_glb_comm` (564 408 B), keyed by `(nec_dev, ctx_id)` in the `encd` driver (`encd_get/set_global_comm`, `ENCD_MAX_CONTEXTS=2`), is the per-process identity record. It holds the world size (`g_device_cnt`), this rank's global id (`g_device_id`), pod topology (`pod_type`/`pod_node_id`/`pod_sz`), the `mmap`'d semaphore-increment value buffer, the bananaphone `local_rings`, and the embedded `enc_comm comm` whose `nccl_comm_node->nccl_comm` is the libnccom handle.
- **The per-NC comm node** — `enc_nccl_comm_node` (88 B) is the per-NeuronCore comm state, keyed in `nccl_comm_pool` by the byte image of the replica group's `bootstrap_participants` vector. One node is shared by every op of every replica group with identical bootstrap participants; it carries the libnccom comm pointer, a refcount, the stream id, and the `bp_barrier* local_barrier`.
- **The op-signature** — a two-level MD5: an inner MD5 per replica group / per source-target-pair set over `{participants, per-op (enc_op, size_n, peer/channel)}`, written into each group's own `signature[16]`; then an outer MD5 over the concatenation of all inner digests, written to `enc_context+992`. The send/recv canonicalization (RECV folds to SEND + global device id) is what makes a point-to-point pair's two endpoints hash identically across ranks.

| | |
|---|---|
| **Global comm object** | `enc_glb_comm` — 564 408 B; keyed `(nec_dev, ctx_id)`, `ENCD_MAX_CONTEXTS=2` |
| **Per-NC comm node** | `enc_nccl_comm_node` — 88 B; keyed by `bootstrap_participants` byte image in `nccl_comm_pool` |
| **Bootstrap entry** | `enc_init_global_comm @0xff430` (`calloc 0x89CB8`; under `gcomm_init_mtx[nec_dev]`) |
| **Validate / re-enter** | `enc_validate_global_comm @0xffa50` — neff world ≤ comm world; else → `enc_init_global_comm` |
| **Comm allocation** | `enc_setup_global_comm @0x10c5d0` → `enc_setup_global_comm_internal @0x10b050` |
| **One-time init** | `enc_init @0xffbb0` — `arch_init` + `ncclInit` + `pthread_mutex_init` × (256 init + 256 setup) |
| **Lock teardown** | `enc_destroy_gcomm_locks @0xffc90` — `pthread_mutex_destroy` over both 256-entry arrays |
| **Op-signature** | `enc_calculate_signature @0x108220` → `enc_context+992`; per-op hash `md5_enc_op_args @0xf6870` |
| **Per-NC node bootstrap** | `get_nccl_comm @0x12b360`; driver `nccl_init_comm @0x12c360` |
| **Driver registry** | `encd_get_global_comm @0x24e9d0` · `encd_set_global_comm @0x24e920` (`tdrv/encd.c`) |
| **libnccom boundary** | `ncclGetUniqueId @0x1c0a50`, `ncclBootstrapSend @0x1c1af0`, `ncclBootstrapRecv @0x1c1cc0`, `ncclInitComm @0x1c0e10` |

---

## 1. The Global Communicator (`enc_glb_comm`)

### Purpose

`enc_glb_comm` is the per-process, per-`(nec_dev, ctx_id)` identity record for the collective world. It answers "who am I and how big is the world" once, at `nrt_build_global_comm` time, so every later replica-group init and every barrier reads a fixed identity instead of re-deriving it. It is **not** the per-op compile state (`enc_context`, [Engine Core](engine-core.md)) and **not** the per-NC comm node (`enc_nccl_comm_node`, §2); it sits above both. It is owned by the `encd` driver, which keys it in a 2-slot table (`ENCD_MAX_CONTEXTS=2`: ctx 0 = NEFF-loaded comm, ctx 1 = standalone user comm) — `encd_get_global_comm @0x24e9d0` / `encd_set_global_comm @0x24e920` are its only accessors, and every libnrt consumer reaches the libnccom handle through the chain `glb_comm->comm.nccl_comm_node->nccl_comm`.

### Layout — `enc_glb_comm` (564 408 B, `structures.json`)

The header (the first 564 352 bytes are mostly the embedded `enc_comm comm` at +208); the fields below are those `enc_init_global_comm` populates and the lifecycle / barrier / cluster-id consumers read. Confidence **HIGH** for every field written by `enc_init_global_comm` (the store offsets are decompile-confirmed against the calloc'd object); **MED** for the per-RG devmem reservation arrays (named from `structures.json`, populated by `enc_setup_global_comm_internal`, not re-derived here).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `g_device_id` | +0 | `uint32_t` | this rank's global device id (its rank in the world) | HIGH |
| `g_device_cnt` | +4 | `uint32_t` | collective world size | HIGH |
| `vtpb_idx` | +8 | `uint32_t` | virtual-TPB index of the owning core | HIGH |
| `nec_dev_id` | +12 | `int` | physical NeuronCore device id | HIGH |
| `mla_idx` | +16 | `int` | MLA (NeuronLink) index this core sits on | HIGH |
| `host_device_id` | +20 | `int` | host-relative device id (`db_get_host_device_id_from_mla`) | HIGH |
| `routing_id` | +24 | `int` | NeuronLink routing id (`loc_4CD20[mla]`) | HIGH |
| `virtual_server_id` | +28 | `uint32_t` | `g_device_id / virtual_server_size` (if configured) | HIGH |
| `pod_type` | +32 | `nec_pod_type_t` | `NONE=0 / P2P=1 / SWITCH=2 / INVALID=3` | HIGH |
| `pod_node_id` | +36 | `uint32_t` | this node's index within the pod | HIGH |
| `pod_sz` | +40 | `uint32_t` | nodes per pod | HIGH |
| `reservation_id` | +48 | `uint64_t` | EC2 capacity-reservation id (`tdrv_ctx`) | HIGH |
| `root_comm_id` | +56 | `const char*` | the user-supplied root comm id string | HIGH |
| `check_sigs` | +64 | `bool` | enable cross-rank signature reconciliation | HIGH |
| `rank_nodes` | +72 | `uint32_t*` | rank → node map | MED |
| `local_ranks` | +80 | `uint32_t*` | intra-node rank list | MED |
| `nccl_comm_node` | +88 | `enc_nccl_comm_node_t` | the global comm's own node (88 B inline) | HIGH |
| `local_rings` | +176 | `bananaphone*` | intra-node IPC rings (asserted non-NULL post-setup) | HIGH |
| `local_peer_handles` | +184 | `bp_handle*` | intra-node peer handles (asserted non-NULL post-setup) | HIGH |
| `inc_recv_sem_values_buffer` | +192 | `uint32_t*` | `mmap`'d semaphore-increment value buffer | HIGH |
| `inc_recv_sem_values_buffer_size` | +200 | `size_t` | = 516 (`0x204` bytes / 4 = 129 entries) | HIGH |
| `comm` | +208 | `enc_comm` | the embedded communicator (`comm.nccl_comm_node->nccl_comm` = libnccom handle) | HIGH |
| `inter_rdh_devmem_res` | +562112 | `void*[4]` | inter-pod RDH scratch reservation | MED |
| `intra_rdh_devmem_res` | +562144 | `void*[4]` | intra-pod RDH scratch reservation | MED |
| `mesh_devmem_res_per_rg` | +562176 | `void*[96]` | per-replica-group mesh scratch | MED |
| `gcomm_setup_mtx` | +564352 | `pthread_mutex_t` | the per-comm setup lock | HIGH |
| `proxy_queue` | +564392 | `void*` | the net-edge proxy task queue | MED |
| `device_barrier_table` | +564400 | `void*` | `operator new(0x18)` device barrier sub-object | HIGH |

> **QUIRK —** the `calloc` size in `enc_init_global_comm` reads as `calloc(1u, (size_t)&loc_89CB8)` in the decompile — IDA models the immediate `0x89CB8` as a fake symbol address. `0x89CB8 = 564 408 = sizeof(enc_glb_comm)`, so the allocation is the whole object, not a sub-buffer. Likewise the late store `*((_QWORD*)g_comm + 70550) = operator new(0x18)` is `device_barrier_table` (word index 70550 = byte 564 400) and `*((_QWORD*)g_comm + 25) = 516` is `inc_recv_sem_values_buffer_size` (word 25 = byte 200). A reimplementer reading the raw decompile must back out every flat word index against the 564 408-byte struct layout; none of these are real array indices.

### The driver registry

The two-slot per-NC registry is the only handle into the global comm; every lifecycle entry point looks the comm up before deciding whether to allocate, validate, or reject.

| Function | Address | Role | Confidence |
|---|---|---|---|
| `encd_get_global_comm` | `0x24e9d0` | `(nec_dev, ctx_id) → enc_glb_comm*` or NULL | HIGH |
| `encd_set_global_comm` | `0x24e920` | register a freshly-allocated `enc_glb_comm` | HIGH |
| `enc_get_glb_device_id_and_cnt` | `0xffb80` | `→ {g_device_id, g_device_cnt}`; `NRT_FAILURE` if none | HIGH |
| `enc_get_cluster_id` | `0xffd90` | `ncclGetCommInfo(comm).cluster_id`; 0 on failure | HIGH |
| `enc_get_epoch` | `0xffdf0` | `ncclGetCommInfo(comm).epoch`; `NRT_FAILURE` on failure | HIGH |

---

## 2. The Per-NeuronCore Comm Node (`enc_nccl_comm_node`)

### Purpose

Where `enc_glb_comm` is one-per-process, `enc_nccl_comm_node` is one-per-distinct-replica-group-topology. A NEFF can bind several replica groups (an all-reduce group, a separate send/recv group, the intra/inter split a hierarchical op decomposes into); each distinct set of bootstrap participants gets its own libnccom comm, allocated lazily on first use and cached. The node carries the libnccom comm pointer, a refcount (so multiple ops sharing the topology share the comm), the stream id the comm runs on, and the `bp_barrier* local_barrier` that synchronizes the intra-node participants. It is keyed in `nccl_comm_pool` (a `string → enc_nccl_comm_node*` hash) by the **byte image of the `bootstrap_participants` vector** — so two replica groups with identical participant ordering collapse onto one comm.

### Layout — `enc_nccl_comm_node` (88 B, `structures.json`)

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `nccl_comm` | +0 | `void*` | the libnccom comm handle (`ncclInitComm` fills it) | HIGH |
| `key` | +8 | `char*` | copy of the `bootstrap_participants` byte image | HIGH |
| `key_sz` | +16 | `size_t` | = `4 * rank_n` (one `u32` per participant) | HIGH |
| `disable_graph` | +24 | `bool` | suppress the `ncclBuildGraphComm` graph-capture path | HIGH |
| `global_nccl_comm_node` | +25 | `bool` | this is the global comm's own node (vs. a per-RG node) | HIGH |
| `refcnt` | +28 | `int` | shared-use refcount (init 1) | HIGH |
| `stream_id` | +32 | `uint32_t` | the stream this comm runs on (`< ENC_MAX_STREAM_N=4`) | HIGH |
| `context_id` | +36 | `uint32_t` | the `(nec_dev, ctx_id)` context this node belongs to | HIGH |
| `num_local_participants` | +40 | `uint32_t` | intra-node participant count | MED |
| `num_local_leaders` | +44 | `uint32_t` | inter-node leader count | MED |
| `my_local_leader` | +48 | `uint32_t` | this rank's local leader | MED |
| `local_participants` | +56 | `uint32_t*` | intra-node participant list | MED |
| `local_leaders` | +64 | `uint32_t*` | inter-node leader list | MED |
| `local_barrier` | +72 | `bp_barrier*` | the intra-node bananaphone barrier | HIGH |
| `intra_pod_interface` | +80 | `bool` | this node uses the intra-pod transport | MED |

> **NOTE —** the key is the *byte image* of `bootstrap_participants`, not of `participants`. A replica group carries two participant vectors: `participants` (@+40) is the logical collective membership, `bootstrap_participants` (@+64) is the set of ranks that exchange the unique id and signature during bootstrap. They differ for hierarchical and pod topologies, where the bootstrap set is the per-level subgroup. Keying the comm pool on `bootstrap_participants` is what lets two ops that share a bootstrap topology but differ in logical membership reuse one libnccom comm. (`get_nccl_comm` computes `key_sz = 4 * rg->rank_n` and `memcpy`s `bootstrap_participants._M_start`.)

---

## 3. The replica-group input

### How a NEFF replica group reaches the comm node

The collective world a NEFF declares is a set of `enc_replica_group_info` records, parsed from the NEFF's `kbin_replica_group_set_t` by `enc_parse_replica_groups @0x11e850` into `enc_context.replica_groups` (@+840, stride 96). Each record is the input to one `get_nccl_comm` call; the fields the bootstrap reads are below (`structures.json`, 96 B).

| Field | Offset | Type | Role in bootstrap | Confidence |
|---|---|---|---|---|
| `rank` | +0 | `int` | this rank's position in the group (`-1` for sendrecv groups) | HIGH |
| `rank_n` | +4 | `int` | group size; asserted `> 0` | HIGH |
| `group_id` | +8 | `int` | the group index (`< ENC_MAX_COMM_N=12`) | HIGH |
| `src_target_pairs_id` | +12 | `int` | back-link to the source-target-pair set | HIGH |
| `num_ops` | +16 | `uint32_t` | ops posted to this group (gates `comm->id` write) | HIGH |
| `sendrecv` | +20 | `bool` | this is a point-to-point group | HIGH |
| `nccl_mesh_supported` | +21 | `bool` | mesh algorithm eligible | MED |
| `must_use_ring_alg` | +22 | `bool` | force ring family | MED |
| `signature` | +23 | `uint8_t[16]` | the per-RG MD5 (§4) compared during validation | HIGH |
| `participants` | +40 | `vector<u32>` | logical collective membership | HIGH |
| `bootstrap_participants` | +64 | `vector<u32>` | unique-id/signature exchange set; the comm-pool **key** | HIGH |
| `bootstrap_rank` | +88 | `int` | this rank's position in the bootstrap tree; `< rank_n` | HIGH |

The companion `enc_src_target_pairs_info` (@+816 in `enc_context`, stride 80) is the point-to-point analog — it carries its own `pairs` (vector of source/target vectors), `participants`, and a 16-byte `signature` (@+48), and is parsed by `enc_parse_src_target_pairs @0x130620`. Both contribute to the op-signature in §4.

---

## 4. The Op-Signature

### Purpose

Collective correctness depends on every rank executing the *same* schedule: the same op types, the same sizes, the same peers, in the same order. A recompile on one rank that reorders or resizes a collective produces a schedule mismatch that, executed, silently corrupts a reduction. `enc_calculate_signature @0x108220` computes a 16-byte MD5 fingerprint of this rank's entire collective program (per replica group and per source-target-pair set), stores it in each group's own `signature[16]` *and* a global digest at `enc_context+992`, and the per-NC bootstrap (§5) exchanges the per-group digest across ranks and AND-reduces a match flag. It runs only in the PARSE state (`enc_model_state_t::ENC_MODEL_PARSE=0`); calling it later logs `"cannot calculate CC signature: not in parse state"` and returns.

### Algorithm — the two-level MD5

The computation is a digest-of-digests. An inner MD5 fingerprints each replica group and each source-target-pair set; the inner digests are concatenated into a flat scratch buffer; an outer MD5 over that buffer is the rank's global signature. The per-op hash (`md5_enc_op_args @0xf6870`) is the heart of it, and its send/recv canonicalization is what makes a point-to-point pair's two endpoints agree.

```c
// enc_calculate_signature @0x108220 — two-level op-signature
function enc_calculate_signature(drv_ctx):                       // 0x108220
    enc_ctx = drv_ctx->enc_ctx;                                  // encd_context+288
    if (!enc_ctx || !drv_ctx->ccop_owner) return NRT_SUCCESS;
    if (enc_ctx->state != ENC_MODEL_PARSE):                      // +300
        nlog("cannot calculate CC signature: not in parse state (%d)");
        return NRT_INVALID;

    // scratch sized = 16 * (n_replica_groups + n_src_target_pairs)
    n_rg  = (enc_ctx->replica_groups.end  - .begin) / 96;        // +840
    n_stp = (enc_ctx->src_target_pairs.end - .begin) / 80;       // +816
    scratch = alloca(16 * (n_rg + n_stp));  memset(scratch, 0);
    offset = 0;
    g_device_id = encd_get_global_comm(nec_dev, ctx_id)->g_device_id;

    // --- LEVEL 1a: per replica group (only sendrecv groups, rank == -1) ---
    for rg in enc_ctx->replica_groups:                           // stride 96
        if (rg.rank != -1): continue;                            // skip non-sendrecv groups here
        // build a shared per-rank op view across the function's op_queue (vtbl+8)
        ops = collect_op_args(enc_ctx->op_queue, rg.group_id);   // +864
        MD5_Init(&md);
        MD5_Update(&md, rg.participants.begin, 4 * rg.participants_n);
        for op in ops: md5_enc_op_args(&md, op, g_device_id);    // 0xf6870
        MD5_Final(rg.signature, &md);                            // write rg.signature[16] @+23
        scratch[offset .. +16] = rg.signature;  offset += 16;

    // --- LEVEL 1b: per source-target-pair set ---
    for stp in enc_ctx->src_target_pairs:                        // stride 80
        ops = collect_op_args(enc_ctx->op_queue, stp...);        // vtbl+16 (src-tgt variant)
        MD5_Init(&md);
        for vec in stp.pairs:                                    // hash each pair vector's bytes
            MD5_Update(&md, vec.begin, vec.end - vec.begin);
        for op in ops: md5_enc_op_args(&md, op, g_device_id);
        MD5_Final(stp.signature, &md);                           // stp.signature[16] @+48
        scratch[offset .. +16] = stp.signature;  offset += 16;

    assert(offset <= scratch_sz);  assert(offset > 0);           // enc.cc:0x39FC / 0x39FD

    // --- LEVEL 2: digest-of-digests into enc_context+992 ---
    MD5_Init(&md);  MD5_Update(&md, scratch, offset);
    MD5_Final(enc_ctx + 992, &md);                               // the global op-signature
    return NRT_SUCCESS;

// md5_enc_op_args @0xf6870 — per-op contribution (the canonicalization core)
function md5_enc_op_args(md, op, g_device_id):                   // 0xf6870
    if (op.enc_op == ENC_SEND):
        MD5_Update(md, {ENC_SEND, op.size_n, op.permute_ring_ch_id}, 16);
    else if (op.enc_op == ENC_RECV):
        // RECV is canonicalized to SEND + the GLOBAL device id, so a
        // send/recv pair on two ranks hashes IDENTICALLY.
        MD5_Update(md, {ENC_SEND, op.size_n, g_device_id}, 16);
    else:
        // collective op: 20-byte view {enc_op, size_n, peer, dtype/alg word}
        MD5_Update(md, {op.enc_op, op.size_n, *(&op.peer + 1)}, 20);
```

> **QUIRK —** RECV does not hash as RECV. `md5_enc_op_args` rewrites every `ENC_RECV` op to `{ENC_SEND, size_n, g_device_id}` before hashing (`0xf6870` lines 22-28). The send side of the same point-to-point edge hashes `{ENC_SEND, size_n, permute_ring_ch_id}`. For the two endpoints of a `send(peer=B)` / `recv(peer=A)` pair to reconcile, the sender's channel id and the receiver's substituted device id must align — which they do because both are derived from the *global* device numbering. A reimplementer who hashes RECV literally (as a distinct op-type with the local peer) will compute a digest that never matches its sender's, and every point-to-point group will fail signature validation.

> **GOTCHA —** the two LEVEL-1 loops feed the op view differently: the replica-group loop calls the op-collector through vtable slot **+8** (`update_param_for_signature`), the source-target-pair loop through slot **+16** (`update_param_for_signature_src_tgt_pairs`). They produce *different* op-arg projections for the same underlying ops. Conflating them — hashing both group kinds with the same projection — yields a signature that diverges from the canonical one even though every byte of the program is identical. The two collectors are `enc_op_list::update_param_for_signature @0x10c870` and `…_src_tgt_pairs @0x10cae0` (and the `enc_fnc` variants at `0xf57c0`/`0xf5830`).

---

## 5. The Per-NC Node Bootstrap

### Purpose

`get_nccl_comm @0x12b360` is where a replica group acquires (or reuses) its libnccom comm. On a cache hit it returns the pooled node (optionally re-running `ncclBuildGraphComm`). On a miss it allocates a fresh `enc_nccl_comm_node`, generates the NCCL unique id on the bootstrap root, validates that every rank's per-group MD5 signature matches via a binary-tree AND-reduce, broadcasts the unique id over a hypercube, then calls `ncclInitComm` and `setup_local_barrier`. The driver wrapper `nccl_init_comm @0x12c360` resolves the stream id from `stream_id_map[comm_id]`, calls `get_nccl_comm`, runs `nccl_setup_comm` (chunk sizing), and on success writes `comm->{id, stream_id}`.

### Entry Point

```text
enc_init_replica_groups (0x138700)            ── per-RG comm init at nrt_load_collectives
  └─ enc_init_comm (0x135d60)                  ── fix the algorithm family (ring / mesh / hier)
       └─ nccl_init_comm (0x12c360)            ── resolve stream_id; write comm->{id,stream_id}
            └─ get_nccl_comm (0x12b360)         ── *** the bootstrap ***
                 ├─ find_nccl_comm_node          ── pool lookup by bootstrap_participants
                 ├─ get_nccl_comm_pool           ── (miss) per-(dev,ctx,stream) pool
                 ├─ ncclGetUniqueId   (0x1c0a50) ── [libnccom] root only (bootstrap_rank==0)
                 ├─ <validate_rg_signature>      ── binary-tree AND-reduce of rg.signature
                 │    ├─ ncclBootstrapRecv (0x1c1cc0)  ── 0x11-byte sig from children 2r+1 / 2r+2
                 │    └─ ncclBootstrapSend (0x1c1af0)  ── 0x11-byte result to parent (r-1)>>1
                 ├─ <broadcast_unique_id>        ── hypercube fan-out
                 │    ├─ ncclBootstrapSend (0x1c1af0)  ── 0x81-byte unique id to rank+mask
                 │    └─ ncclBootstrapRecv (0x1c1cc0)  ── 0x81-byte unique id from rank&~mask
                 ├─ ncclInitComm      (0x1c0e10) ── [libnccom] build the comm from the unique id
                 └─ setup_local_barrier (0x10ab90)── allocate the bp_barrier (node+72)
```

### Algorithm — allocate, validate, broadcast, init

```c
// get_nccl_comm @0x12b360 — per-NC comm node bootstrap
function get_nccl_comm(rg, nec_dev, ctx_id, stream_id, disable_graph):     // 0x12b360
    g = encd_get_global_comm(nec_dev, ctx_id);
    key = rg->bootstrap_participants;  key_sz = 4 * rg->rank_n;

    node = find_nccl_comm_node(nec_dev, ctx_id, stream_id, key, key_sz);   // pool lookup
    if (node):
        if (!disable_graph && node.graph_pending):
            ncclBuildGraphComm(node);                                      // re-capture graph
        return node;

    // --- (1) ALLOCATE: 88-byte node, keyed by bootstrap_participants ---
    assert(stream_id < ENC_MAX_STREAM_N);                                  // enc.cc:0x29D (==4)
    pool = get_nccl_comm_pool(nec_dev, ctx_id, stream_id);
    node = calloc(1, 0x58);                                                // sizeof(enc_nccl_comm_node)
    node->key = copy(key, key_sz);  node->key_sz = key_sz;  node->refcnt = 1;
    node->context_id = ctx_id;  node->stream_id = stream_id;
    pool_insert(pool, string(key, key_sz), node);

    // --- (2) UNIQUE ID: root generates the 129-byte NCCL unique id ---
    if (rg->bootstrap_rank == 0):
        if (ncclGetUniqueId(&unique_id, rg->rank_n, "nccl init comm")):    // 0x1c0a50
            goto fail;                                                     // "failed to get unique ID"
    bcomm = g->comm.nccl_comm_node->nccl_comm;                             // the bootstrap socket
    bp    = copy(rg->bootstrap_participants);                              // rank -> peer device-id map

    // --- (3) VALIDATE RG SIGNATURE: binary tree, AND-reduce of match flag ---
    //     validate_rg_signature, enc.cc:0x114/0x115
    rank = rg->bootstrap_rank;  rank_n = rg->rank_n;
    assert(rank_n > 0 && rank < rank_n);
    match = true;
    for child in {2*rank + 1, 2*rank + 2}:                                 // binary-tree children
        if (child < rank_n):
            recvd = {sig:[16], ok:bool};
            if (ncclBootstrapRecv(bcomm, bp[child], &recvd, 0x11)):        // 17 = 16-byte sig + status
                goto fail;  // "failed to receive signature validation result from %d ..."
            if (match):
                match = recvd.ok && (recvd.sig == rg->signature);          // compare vs rg.signature@+23
    if (rank > 0):                                                         // non-root: report up
        parent = (rank - 1) >> 1;
        if (ncclBootstrapSend(bcomm, bp[parent], &{2r+1,2r+2}, 0x11)):     // send {children,match}
            goto fail;  // "failed to send signature validation result to %d ..."

    // --- (4) BROADCAST UNIQUE ID: hypercube fan-out from root ---
    //     broadcast_unique_id, enc.cc:0x15E/0x15F
    if (rank == 0): valid = match;                                         // only root holds the AND
    mask = 1;
    while ((mask & rank) == 0):                                            // ascend to my hypercube level
        mask <<= 1;
        if (rank_n <= mask): break;
    if ((mask & rank) != 0):                                              // I have a parent in the cube
        parent = rank & ~mask;
        ncclBootstrapRecv(bcomm, bp[parent], &unique_id, 0x81);           // 129-byte unique id
    for step = mask >> 1; step != 0; step >>= 1:                          // fan out down the cube
        if (rank + step < rank_n):
            ncclBootstrapSend(bcomm, bp[rank + step], &unique_id, 0x81);

    if (!valid):
        nlog("[gid: %u] failed signature validation: ... mismatched collectives between the peers");
        goto fail;

    // --- (5) INIT + LOCAL BARRIER ---
    if (ncclInitComm(&node->nccl_comm)):                                   // 0x1c0e10
        nlog("failed to init NCCL comm stream_id:%d group_id:%d");  goto fail;
    if (setup_local_barrier(g, node, rg)):                                 // 0x10ab90 -> node->local_barrier
        nlog("failed to setup local barrier");  goto fail;
    return node;

fail:
    free_nccl_comm_node(nec_dev, node);  return NULL;
```

> **QUIRK —** the signature reconciliation and the unique-id distribution use **two different tree shapes** over the *same* `bootstrap_participants` array. The signature is AND-reduced up a **binary tree** (children `2r+1`/`2r+2`, parent `(r-1)>>1`) so the root learns whether *all* ranks agree; the unique id is then fanned **down a hypercube** (mask-stepping `rank ± step`) so the root's id (and its single accumulated validity bit) reaches every rank in `log2(rank_n)` rounds. A reimplementer who uses one topology for both will either fail to AND every leaf's verdict into the root, or distribute the unique id with the wrong fan-out and deadlock on a missing send/recv partner. The message sizes also differ and are the cheapest sanity check: `0x11` (17 B) for the signature exchange, `0x81` (129 B) for the unique id.

> **GOTCHA —** the validity bit only *means* anything at the root. Each non-root rank forwards a match flag up the binary tree but never sees the global verdict during validation; it learns it only when the root's hypercube broadcast carries the accumulated `valid` bit down with the unique id. So a rank that locally matches but whose sibling subtree mismatched will still abort — correctly — but only after the broadcast. A reimplementer must not let a locally-matching rank proceed to `ncclInitComm` before the broadcast confirms the *global* AND; gating on the local compare alone admits a desynchronized world.

> **NOTE —** `nccl_init_comm @0x12c360` resolves the stream id from `enc_ctx->stream_id_map[comm_id]` (the `+248` dword block, asserting `!= ENC_STREAM_ID_UNINITIALIZED` at `enc.cc:0x68C`) and, only when `rg->num_ops > 0`, writes `comm->{id, stream_id}` as a single packed 8-byte store (`id`@+80, `stream_id`@+84). A replica group with zero posted ops gets its comm bootstrapped but its `comm->id` is left untouched — the comm exists for barrier participation without being bound as an op target.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `get_nccl_comm` | `0x12b360` | per-NC node alloc/lookup + the full bootstrap (sig-validate, bcast, init) | HIGH |
| `nccl_init_comm` | `0x12c360` | driver wrapper: stream resolve → `get_nccl_comm` → `nccl_setup_comm` → `comm->{id,stream_id}` | HIGH |
| `nccl_setup_comm` | `0x107c50` | chunk/chunk-size config of the bound comm | HIGH |
| `setup_local_barrier` | `0x10ab90` | allocate the intra-node `bp_barrier` into `node->local_barrier` (+72) | HIGH |
| `find_nccl_comm_node` | (pool) | lookup by `bootstrap_participants` byte image | MED |
| `get_nccl_comm_pool` | `0x109930` | per-`(dev, ctx, stream)` comm pool | HIGH |
| `enc_init_replica_groups` | `0x138700` | per-RG init at load (drives `nccl_init_comm`) | HIGH |
| `enc_init_comm` | `0x135d60` | fix the algorithm family before bootstrap | HIGH |

---

## 6. The Lifecycle Spine

### Bootstrap sequence — `enc_init_global_comm`

`enc_init_global_comm @0xff430` is the once-per-process global-comm constructor, called from `nrt_build_global_comm` / `nrt_cc_global_comm_init`. It gathers this core's identity from the device broker, allocates and populates the `enc_glb_comm`, initializes the semaphore-increment value buffer, validates the pod topology, and registers the comm — all under `gcomm_init_mtx[nec_dev]`.

```c
// enc_init_global_comm @0xff430  (DWARF name: enc_init_global_comm_internal)
function enc_init_global_comm(vnc, g_device_id, g_device_count, ctx_id, root_comm_id, check_sigs):  // 0xff430
    vcore = vtpb_get_virtual_core(vnc);             if (!vcore) FAIL "Failed to find core %u";
    if (db_physical_core_get_mla_and_tpb(vcore->tpbs, &mla, &tpb)):  FAIL "Failed to get MLA";
    tdrv = db_tdrv_ctx_get();                       if (!tdrv) FAIL "TDRV not initialized!";
    nec_dev      = vcore->nec_dev_id;
    mla_idx      = vcore->tpbs[0].device_id;
    host_dev_id  = db_get_host_device_id_from_mla(mla);
    routing_id   = mla_routing_table[mla];          // loc_4CD20[mla]
    pod_type     = tdrv->pod_type;  pod_node_id = tdrv->pod_node_id;
    pod_sz       = tdrv->pod_sz;    reservation_id = tdrv->reservation_id;

    // idempotent fast path: already set up with the SAME world -> return success
    if ((g = encd_get_global_comm(nec_dev, ctx_id)) != NULL):
        if ({g->g_device_id, g->g_device_cnt} != {g_device_id, g_device_count}):
            FAIL "NRT has already been setup with a collectives world size of %d ...";
        return NRT_SUCCESS;

    pthread_mutex_lock(&gcomm_init_mtx[nec_dev]);
    vss = nrt_config_0.virtual_server_size;
    vtpb_idx = vtpb_get_virtual_core_from_nec_dev_id(nec_dev)?->vtpb_idx ?? -1;

    // double-checked: another thread may have set it up under the lock
    if ((g = encd_get_global_comm(nec_dev, ctx_id)) != NULL):
        if (... mismatched world ...): FAIL "... already been setup ...";
        unlock; return NRT_SUCCESS;

    g = calloc(1, 0x89CB8);                         // sizeof(enc_glb_comm) = 564408
    if (!g): { unlock; return NRT_FAIL_HOST_MEM_ALLOC; }

    if (vss):                                       // virtual-server partitioning
        if (g_device_count % vss != 0): FAIL "virtual server size %u is not a divisor of world size %u";
        g->virtual_server_id = g_device_id / vss;   // +28
    g->g_device_id   = g_device_id;                 // +0
    g->g_device_cnt  = g_device_count;              // +4
    g->vtpb_idx      = vtpb_idx;                     // +8
    g->reservation_id= reservation_id;              // +48
    g->pod_sz        = pod_sz;                       // +40
    g->{nec_dev_id, mla_idx, host_device_id, routing_id} = {...};   // +12/+16/+20/+24 (one SSE store)
    g->root_comm_id  = root_comm_id;                // +56
    g->{pod_type, pod_node_id} = {pod_type, pod_node_id};           // +32/+36
    g->check_sigs    = check_sigs;                  // +64
    g->device_barrier_table = operator new(0x18);   // +564400 (word 70550), zeroed
    g->inc_recv_sem_values_buffer_size = 516;       // +200 (word 25)

    // semaphore-increment value buffer: 0x204 bytes mmap'd, init to a 128-entry stride pattern
    buf = mmap(NULL, 0x204, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    g->inc_recv_sem_values_buffer = buf;            // +192
    if (buf == MAP_FAILED): FAIL "global comm failed to mmap memory for the semaphore increment value buffer";
    buf[0] = 0x10000;                               // header
    seed = xmmword_850990;                          // 4-lane SSE seed
    for i in 0..127: store seed at buf[1 + 4*i]; seed += {4,4,4,4};   // 128 striding entries

    // pod-topology validation
    if (pod_type > 2): FAIL "Invalid pod_type: %u";
    if (pod_type != NONE):
        assert(pod_type == P2P || pod_type == SWITCH);              // enc.cc:0x3EC6
        limit = (pod_type == P2P) ? pod_sz : 4;
        if (pod_node_id >= limit): FAIL "Invalid pod_node_id: %u (pod_size: %u) pod_type:%d";

    rc = encd_set_global_comm(nec_dev, ctx_id, g);  // register in the 2-slot driver table
    if (rc != NRT_SUCCESS): { nlog "failed to set global comm"; free(g); }
    pthread_mutex_unlock(&gcomm_init_mtx[nec_dev]);
    return rc;
```

> **GOTCHA —** `enc_init_global_comm` is **double-checked-locked**: it queries `encd_get_global_comm` once before taking `gcomm_init_mtx[nec_dev]` and again after. The pre-lock check is the idempotent fast path (a second `nrt_build_global_comm` with the same world returns `NRT_SUCCESS` without re-allocating); the post-lock check closes the race where two threads on the same NeuronCore both miss the pre-lock query. A reimplementer who drops the second check will leak an `enc_glb_comm` and register the loser, leaving `comm.nccl_comm_node` bootstrapped against the wrong object. The mismatch path (different world size on re-entry) is a hard `NRT_INVALID` — the world size is immutable once set.

> **QUIRK —** the pod-node validity limit is **asymmetric** by pod type. For `NEC_POD_TYPE_P2P` the bound is the dynamic `pod_sz`; for `NEC_POD_TYPE_SWITCH` it is a hard-coded `4`. A `pod_node_id` that is valid for a P2P pod of 8 nodes (e.g. 5) is rejected for a switch pod, and the assert `pod_type == P2P || pod_type == SWITCH` (`enc.cc:0x3EC6`) fires before either bound is checked if a fourth pod-type value ever reaches here. `NEC_POD_TYPE_NONE` skips validation entirely.

### Validate / re-enter — `enc_validate_global_comm`

`enc_validate_global_comm @0xffa50` is the load-time guard (called from `nrt_load_collectives` / `nrt_cc_prepare`). If a global comm already exists it checks the NEFF's world size against it; otherwise it delegates straight to `enc_init_global_comm`.

```c
// enc_validate_global_comm @0xffa50
function enc_validate_global_comm(vnc, dev_id, dev_count, ctx_id, root_comm_id, check_sigs):  // 0xffa50
    vcore = vtpb_get_virtual_core(vnc);  if (!vcore) return NRT_INVALID;   // "Failed to find core"
    g = encd_get_global_comm(vcore->nec_dev_id, ctx_id);
    if (!g):
        return enc_init_global_comm(vnc, dev_id, dev_count, ctx_id, root_comm_id, check_sigs);
    if (g->g_device_cnt < dev_count):
        return NRT_INVALID;  // "World size of neff %d is greater than world size of global communicator %d"
    if (g->g_device_cnt == dev_count || g->comm.nccl_comm_node):
        return NRT_SUCCESS;  // equal world, OR comm already bootstrapped
    return NRT_INVALID;      // "World size of neff != global communicator. Recommend nrt_build_global_comm first."
```

> **NOTE —** a NEFF may declare a world **smaller** than the global communicator (a model that uses a subset of the cluster), and that is permitted — `g_device_cnt < dev_count` (neff bigger than comm) is the only hard reject. The middle case (neff smaller, comm not yet bootstrapped) is also rejected with the "call `nrt_build_global_comm` on all ranks first" hint, because a sub-world NEFF cannot itself bootstrap the comm — the full-world build must precede it. Equal world size, or an already-bootstrapped comm, passes.

### One-time init / teardown — `enc_init` / `enc_destroy_gcomm_locks`

`enc_init @0xffbb0` (from `nrt_init`) is the process-level setup: it runs `arch_init`, hands off to libnccom via `ncclInit` (the `dlopen`/`dlsym` of the `neuron*` ABI), and constructs the two 256-entry mutex arrays — one init lock and one setup lock per possible NeuronCore (`NEC_MAX_DEVICES = 256`). `enc_destroy_gcomm_locks @0xffc90` (from `nrt_close`) destroys both arrays.

```c
// enc_init @0xffbb0
function enc_init():                                            // 0xffbb0
    if (arch_init()) return error;                              // 0x2563f0
    if (ncclInit()) return error;                              // 0x1bff30 — dlopen libnccom, dlsym neuron*
    for dev in 0..255:                                          // NEC_MAX_DEVICES
        if (pthread_mutex_init(&gcomm_init_mtx[dev], NULL)):
            FAIL "Failed to create global comm init lock (ret=%d)";
        if (pthread_mutex_init(&gcomm_setup_mtx[dev], NULL)):
            FAIL "Failed to create global comm setup lock (ret=%d)";
    return NRT_SUCCESS;

// enc_destroy_gcomm_locks @0xffc90
function enc_destroy_gcomm_locks():                             // 0xffc90
    for dev in 0..255:
        pthread_mutex_destroy(&gcomm_setup_mtx[dev]);
        pthread_mutex_destroy(&gcomm_init_mtx[dev]);
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `enc_init` | `0xffbb0` | one-time: `arch_init` + `ncclInit` + 256×2 mutex init | HIGH |
| `enc_init_global_comm` | `0xff430` | allocate + populate + register the `enc_glb_comm` (double-checked lock) | HIGH |
| `enc_validate_global_comm` | `0xffa50` | load-time world-size guard; delegates to `enc_init_global_comm` | HIGH |
| `enc_setup_global_comm` | `0x10c5d0` | post-bootstrap invariant check; delegates to `…_internal` for comm alloc | HIGH |
| `enc_setup_global_comm_internal` | `0x10b050` | allocate the libnccom comm (`get_nccl_comm` for the global RG) | MED |
| `enc_calculate_signature` | `0x108220` | the two-level MD5 op-signature → `enc_context+992` | HIGH |
| `enc_destroy_gcomm_locks` | `0xffc90` | destroy both 256-entry mutex arrays | HIGH |
| `enc_init_global_comm` (cold) | `0x46de1` | the `.cold` assert/error landing pad of `…_internal` | HIGH |

> **NOTE —** `enc_setup_global_comm @0x10c5d0` is the second gate, run after a comm exists. It asserts the post-bootstrap invariants — `g_device_id == comm.ci.rank`, `g_device_cnt == comm.ci.rank_n`, `local_rings != NULL`, `local_peer_handles != NULL` (`enc.cc:0x3E5F..0x3E62`) — so that the global identity (§1) and the libnccom-derived `enc_comm_info` view agree. If `comm.nccl_comm_node` is not yet set it calls `enc_setup_global_comm_internal @0x10b050`, which performs the actual `get_nccl_comm` for the global replica group and reserves the hierarchical/RDH/mesh devmem scratch the global comm owns. That internal allocation is surface-mapped here; its devmem-reservation math is owned by [Hierarchical and RDH Composition](hierarchical-rdh.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `enc_init_global_comm` (`@0xff430`) | the global-comm constructor this page documents |
| `enc_calculate_signature` (`@0x108220`) | the two-level MD5 op-signature writer (`enc_context+992`) |
| `get_nccl_comm` (`@0x12b360`) | the per-NC node bootstrap (validate + broadcast + init) |
| `encd_get_global_comm` / `_set_` (`@0x24e9d0`/`@0x24e920`) | the `(nec_dev, ctx_id)` driver registry |
| `ncclBootstrapSend`/`Recv` (`@0x1c1af0`/`@0x1c1cc0`) | the libnccom bootstrap socket the exchanges run over |
| `setup_local_barrier` (`@0x10ab90`) | allocates the per-node `bp_barrier` (`enc_nccl_comm_node+72`) |

## Cross-References

- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](nccl-boundary.md) — the `dlsym`'d `neuron*` ABI behind `ncclInit`/`ncclGetUniqueId`/`ncclBootstrapSend`/`ncclInitComm`
- [Ring Scheduling Math](ring-scheduling.md) — the per-op device step loop that runs once the comm node is bootstrapped
- [Engine Core (enc_context and Accessors)](engine-core.md) — the per-NC compile state whose `signature` (@+992) this page writes and whose `comms[12]` the comm node binds
- [Communicator Init and Bootstrap](../nccom/comm-init.md) — the host-side NCCL topology and ring-order construction on the libnccom side of the boundary
- [back to index](../index.md)
