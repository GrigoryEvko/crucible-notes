# Communicator Init and Bootstrap

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text` VMA == file offset for the cited band, so every `0x…` is both a file offset and an analysis VMA.*
> *Evidence grade: **Confirmed (byte-anchored)** — symbol counts, version constants, struct sizes, the bootstrap sanity magic, and the wrapper bodies were re-read directly from the binary (`readelf`/`nm`/`objdump`); decompiler-derived bodies are tagged MED/LOW. Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

Communicator init in libnccom is the **thinnest** kind of fork: a one-screen Neuron wrapper layer (`neuron*`, exported and dlsym'd by libnrt) sitting on top of an almost-unmodified NCCL `init.cc` / `bootstrap.cc` core (`nccl*`, internal). If you already hold a mental model of upstream `ncclCommInitRank` — set the CUDA device, generate or import a 128-byte `ncclUniqueId`, `calloc` the `ncclComm`, run a socket bootstrap that AllGathers a per-rank peer-info record, then build topology and transports — you already know the shape of this path. The wrappers add nothing algorithmic: they marshal the Neuron `enc_neuron_device_info` argument, set the device through the RT, and pass an `isGlobalInit` flag down. The reusable half is the `nccl*` core; the genuinely-forked half is **the bootstrap** (a dual flat/hierarchical handshake) and **the two-axis version handshake** that lets libnrt and libnccom verify each other at load time.

The link model is **resolve-and-trampoline**. libnccom never sees an application; libnrt `dlopen`s it `RTLD_NOW` and `dlsym`s **37 exported `neuron*` entry points** (verified: `nm -D … | rg ' T neuron' | wc -l` → 37) into a function-pointer table. Calls flow forward through those trampolines into the `nccl*` core. The core in turn calls **back** into libnrt through **16 `nec_*` device/topology callbacks + 4 `nrt_*` runtime queries**, all bound to ELF symbol version `NRT_2.0.0` (verified: 16 `U nec_*`, 4 `U nrt_*`). These three counts — 37 / 16 / 4 — are the ABI surface, and they agree exactly with the [overview](overview.md) map; this page anchors each symbol to its address and role.

The page is organized as the canonical NCCL-init grammar, one `##` unit per layer: the **wrapper layer** (the trampoline targets), the **`ncclCommInitRank` core** (the version banner, validation, and the `commAlloc` that produces the 19240-byte `ncclComm`), the **bootstrap** (`bootstrapInit`'s flat-vs-hierarchical dispatch and the flat handshake in full), and the **two version identities** (the runtime int `2999` versus the compat id `89`, and how `getNcclNrtGitHash` welds the libnccom and libnrt build hashes into one bootstrap sanity tag). The topology and graph-build stages that follow init are owned by their own siblings ([topology](topology.md)) and only referenced here.

For reimplementation, the contract of the init/bootstrap layer is:

- **The wrapper → core → bootstrap call tree** — `neuronInitGlobalComm`/`neuronInitComm` → `ncclCommInitRank` → `ncclCommInitRankSync` (`commAlloc`) → `initTransportsRank` → `bootstrapInit` → `bootstrapAllGather`, with the 128-byte `ncclUniqueId` passed **by value across 8 XMM registers** at every wrapper→core boundary.
- **The `ncclComm` allocation** — `calloc(0x4B28)` = **19240 bytes**, plus the satellite allocations (`asyncOps` `0x48000`, the per-rank `connectSend/Recv` and `p2pSends/Recvs`, the `abortFlag` host-alloc) and the channel/proxy-state init that `commAlloc` performs before handing the comm to `initTransportsRank`.
- **The bootstrap dual path** — the `nrt_get_total_vnc_count() == 0` gate that distinguishes single-vNC (no bootstrap) from distributed, and the flat (`bootstrapInitFlat`) vs 2-tier hierarchical (`bootstrapInitWithHierarchy`) split; the flat handshake's `extInfo` record carrying the `0x61796c69` (`"ilya"`) sanity magic and the `INFO_TO_ROOT` / `INFO_FROM_ROOT` exchange that returns the ring neighbor, `cluster_id`, and `epoch`.
- **The two-axis version handshake** — the forward numeric compat id **89** (`0x59`) that libnrt checks at dlopen, the reverse ELF `NRT_2.0.0` symbol-version hard link, and the 16-byte `nccl_nrt_git_hash` (libnccom hash ⊕ libnrt hash) that `getNcclNrtGitHash` embeds in the bootstrap `extInfo` for cross-process skew detection.

| | |
|---|---|
| **Wrapper entry (global)** | `neuronInitGlobalComm` @`0x46c40` — `isGlobalInit=1`, multi-node |
| **Wrapper entry (per-rank)** | `neuronInitComm` @`0x46d90` — `isGlobalInit=0`, id pre-shared |
| **Unique-id gen** | `neuronGetUniqueId` @`0x46660` → `ncclGetUniqueId` @`0x262f0` |
| **Core init** | `ncclCommInitRank` @`0x2cc20` → `ncclCommInitRankSync` @`0x2beb0` |
| **`ncclComm` size** | `calloc(0x4B28)` @`0x2c025/0x2c02a` = **19240 bytes** |
| **Transport/bootstrap entry** | `initTransportsRank` @`0x28ed0` → `bootstrapInit` @`0x3cd90` |
| **Bootstrap fork delta** | flat `bootstrapInitFlat` @`0x37b60` · hier `bootstrapInitWithHierarchy` @`0x3b0f0` |
| **Bootstrap gate** | `nrt_get_total_vnc_count()` (9 call sites; `@plt 0x1e430`) |
| **Sanity magic** | `extInfo.sanity = 0x61796c69` (`"ilya"`) — `movl $0x61796c69,(%rbp)` @`0x37c89` |
| **Version int** | `ncclGetVersion → 2999` — `movl $0xbb7,(%rdi)` @`0x262d9` |
| **Compat id** | `89` (`0x59`) — `movq $0x59,0x30(%rax)` @`0x464a8` (offset 48) |
| **Banner** | `"NCCL version 2.31.24+nrt2.0"` (`showVersion`) |
| **ABI surface** | **37** `neuron*` exports · **16** `nec_*` + **4** `nrt_*` imports `@NRT_2.0.0` |

> **QUIRK — the wrappers are almost empty.** A reimplementer who expects the `neuron*` entry points to *contain* init logic will be surprised: `neuronInitGlobalComm` (`0x46c40`) is `ncclRtSetDevice(dev->nec_dev_id)` → `ncclGetUniqueId(...)` → `ncclCommInitRank(..., isGlobalInit=1)` and nothing else; `neuronInitComm` (`0x46d90`) is shorter still. All the work is in the `nccl*` core. The wrapper layer exists only to (a) present a stable dlsym ABI to libnrt, (b) translate the Neuron `enc_neuron_device_info` arg, and (c) carry the `isGlobalInit` bit. Port the core; the wrappers are glue.

---

## 1. The Wrapper Layer (libnrt trampoline targets)

### Purpose

The 37 exported `neuron*` symbols are the entire forward ABI: libnrt `dlopen`s libnccom and `dlsym`s every one into a fn-ptr table (the load/gate mechanics live on [abi](abi.md)). Of the 37, **13 are init/bootstrap-cell** entry points; the remaining 24 are the net/proxy data-plane path (§7, out of scope here). Each init wrapper is a marshalling shim: it sets the Neuron device through the RT, optionally generates the id, and trampolines into a single `nccl*` core function. The wrappers carry the 128-byte `ncclUniqueId` **by value** — the System V ABI spills the 16-byte-aligned 128-byte aggregate across the 8 XMM argument registers, which is why every wrapper→core signature shows `ncclUniqueId id` passed inline rather than by pointer.

### Entry Point

```text
libnrt: dlopen("libnccom.so", RTLD_NOW) + dlsym 37 neuron*   [gate #0 = neuronGetVersionInfo, compat==89]
  │
  ├─ neuronGetUniqueId    @0x46660  ── root-rank id gen; 128B uid out via 8 XMM stores
  │     └─ ncclGetUniqueId @0x262f0
  │
  ├─ neuronInitGlobalComm @0x46c40  ── global (multi-node) init; isGlobalInit=1
  │     ├─ ncclRtSetDevice(dev->nec_dev_id)
  │     ├─ ncclGetUniqueId(root_comm_id, count, &uid, hint)
  │     └─ ncclCommInitRank(root_comm_id, &comm, count, gdev, dev, isGlobalInit=1, uid)  @0x2cc20
  │
  └─ neuronInitComm       @0x46d90  ── per-rank init; id pre-shared; isGlobalInit=0
        ├─ ncclRtSetDevice(dev->nec_dev_id)
        └─ ncclCommInitRank(0, &comm, rank_n, rank, dev, isGlobalInit=0, id-as-8-XMM)   @0x2cc20
```

### Algorithm

The two init wrappers differ only in whether they synthesize the id and in the `isGlobalInit` value they pass. Their bodies, recovered from the (small, fully-decompiled) functions:

```c
// neuronInitGlobalComm — 0x46c40  [HIGH: body fully decompiled]
NRT_STATUS neuronInitGlobalComm(int gdev, int gcount,
        const enc_neuron_device_info_t *dev, const char *root_comm_id,
        void **nccl_comm, const char *hint):
    ncclRtSetDevice(dev->nec_dev_id);                       // dev+0 -> RT device select
    ncclUniqueId uid;
    rc = ncclGetUniqueId(root_comm_id, gcount, &uid, hint); // 0x262f0: lazy bootstrapNetInit + create root
    if (rc) return rc;
    // uid is now a socketAddress (IP:port) of the root listener
    return ncclCommInitRank(root_comm_id, nccl_comm, gcount, gdev, dev,
                            /*isGlobalInit=*/1, uid);        // 0x2cc20, uid by value (8 XMM)

// neuronInitComm — 0x46d90  [HIGH]
NRT_STATUS neuronInitComm(void **nccl_comm, int rank_n, char *id, int rank,
        const enc_neuron_device_info_t *dev, bool bypass_graph):
    ncclRtSetDevice(dev->nec_dev_id);
    return ncclCommInitRank(0, nccl_comm, rank_n, rank, dev,
                            /*isGlobalInit=*/0, *(ncclUniqueId*)id);  // id already shared

// neuronGetUniqueId — 0x46660  [HIGH]
NRT_STATUS neuronGetUniqueId(char *id_out, int rank_n, const char *hint):
    ncclUniqueId uid;
    rc = ncclGetUniqueId(/*root_comm_id=*/0, rank_n, &uid, hint);     // 0x262f0
    if (rc) return rc;
    memcpy_8xmm(id_out, &uid, 128);    // 8 movdqu stores: the 128B id copied out by value
    return NRT_SUCCESS;
```

> **NOTE — `bypass_graph` is carried but deferred.** `neuronInitComm`'s `bypass_graph` flag is threaded down as the `bypass` byte through `ncclCommInitRank` → `ncclCommInitRankSync` → `initTransportsRank`, but the graph build it gates is a *separate* RT call (`neuronBuildGraphComm` @`0x46e90` → `ncclCommBuildGraphRank` @`0x26da0`). Init proper builds topology + transports only; the channel/ring graph is built later. This matches the runtime's two-phase comm bring-up and the finding that the cost-model `enqueue.cc` is a stub in this build (see [tuning](tuning.md)). `bypass_graph` therefore changes nothing on the init path itself.

### Function Map

The 13 init/bootstrap-cell exports, anchored to address. (The 24 net/proxy exports are catalogued in §7.)

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `neuronGetVersionInfo` | `0x46150` | version probe; writes compat id **89** at out+48 — **dlsym gate #0** | HIGH |
| `neuronGetUniqueId` | `0x46660` | root-rank id gen; 128B uid out via 8 XMM stores | HIGH |
| `neuronInitGlobalComm` | `0x46c40` | global (multi-node) comm init; `isGlobalInit=1` | HIGH |
| `neuronInitComm` | `0x46d90` | per-rank comm init from a pre-shared id; `isGlobalInit=0` | HIGH |
| `neuronBuildGraphComm` | `0x46e90` | graph/channel build (separate RT call) → `ncclCommBuildGraphRank` | HIGH |
| `neuronGetCommInfo` | `0x46ec0` | export rank/peer/mla/pod info back to RT; calls `nec_get_virtual_core_size` | MED |
| `neuronFreeComm` | `0x47f90` | teardown (`= ncclCommDestroy`) | HIGH |
| `neuronExpandConnectivity` | `0x47fe0` | post-init P2P connectivity expansion | MED |
| `neuronSetAffinity` | `0x49020` | `sched_getaffinity` save + `ncclTopoSetAffinity(comm->topo, comm->rank, …)` | HIGH |
| `neuronInitNet` | `0x49220` | `= ncclInit(root_comm_id)` (net-plugin + bootstrapNetInit) | HIGH |
| `neuronBootstrapAllGather` | `0x48a00` | thunk → `bootstrapAllGather` @`0x3e280` | HIGH |
| `neuronBootstrapSend` | `0x489a0` | thunk → `bootstrapSend` @`0x3d060` | HIGH |
| `neuronBootstrapRecv` | `0x489d0` | thunk → `bootstrapRecv` @`0x3d500` | HIGH |

### Considerations

The wrapper layer enforces the **all-or-nothing** dlsym contract: libnrt resolves all 37 symbols, and the *first* one resolved is `neuronGetVersionInfo` — the version gate (§4). The init wrappers assume the device is selectable through `ncclRtSetDevice` and that `dev->nec_dev_id` (offset 0 of the 216-byte `enc_neuron_device_info`) is the RT device id. A reimplementer must keep the wrapper signatures byte-stable, because libnrt calls them by fixed table slot, not by name after the initial `dlsym`.

---

## 2. The `ncclCommInitRank` Core

### Purpose

`ncclCommInitRank` (`0x2cc20`) is the inherited NCCL entry point, lightly forked to take the Neuron `enc_neuron_device_info` and an `isGlobalInit` flag. It is the validation-and-version gate: it emits the version banner, probes RT readiness, range-checks the rank, and — if this rank owns the root id — creates the bootstrap root listener inline before delegating the heavy lifting to `ncclCommInitRankSync`. The split mirrors stock NCCL: `…InitRank` is the public, validating shell; `…InitRankSync` is `commAlloc` plus the synchronous bring-up.

### Entry Point

```text
ncclCommInitRank @0x2cc20
  ├─ ncclRtGetDevice(&device)
  ├─ if (root_comm_id && *root_comm_id && rank==0)
  │     bootstrapCreateRoot(&id, idFromEnv=1, isGlobalInit, nranks, "CommInitRankDev")   @0x3ca00
  │  else
  │     ncclInit(root_comm_id)            @0x26040  ── initPlugin + bootstrapNetInit + sock-timeout
  ├─ showVersion → "NCCL version 2.31.24+nrt2.0"
  ├─ ncclRtFree(0)                         ── RT-readiness probe (no-op free)
  ├─ PtrCheck(newcomm)
  ├─ rank-range check  (0 <= rank < nranks)  ── "Invalid rank requested : %d/%d"
  └─ ncclCommInitRankSync(&comm, nranks, rank, device, dev, isGlobalInit, id, bypass)  @0x2beb0
```

### Algorithm

```c
// ncclCommInitRank — 0x2cc20  [HIGH: named call sites + strings byte-confirmed]
ncclResult_t ncclCommInitRank(const char *root_comm_id, void **newcomm,
        int nranks, int rank, const enc_neuron_device_info *dev,
        bool isGlobalInit, ncclUniqueId id /* by value, 8 XMM */):
    nvtxRangePush("ncclCommInitRank");
    ncclRtGetDevice(&device);                                // current RT device
    if (root_comm_id && root_comm_id[0] && rank == 0):
        // this rank is root: stand up the listener now, id derives from env/IF
        bootstrapCreateRoot(&id, /*idFromEnv=*/1, isGlobalInit, nranks, "CommInitRankDev");
        // "Root comm ID set by environment to %s"  (init.cc:1012)
    else:
        ncclInit(root_comm_id);                              // 0x26040: lazy net/bootstrap init
    showVersion();                                           // -> "NCCL version 2.31.24+nrt2.0"
    ncclRtFree(0);                                           // probe: RT must be live
    if (newcomm == NULL) return ncclInvalidArgument;         // PtrCheck
    if (rank < 0 || rank >= nranks):
        WARN("Invalid rank requested : %d/%d", rank, nranks);   // init.cc:1024
        return ncclInvalidArgument;
    rc = ncclCommInitRankSync(newcomm, nranks, rank, device, dev, isGlobalInit, id, bypass);
    nvtxRangePop();
    return rc;
```

### Algorithm — `commAlloc` (`ncclCommInitRankSync`)

`ncclCommInitRankSync` (`0x2beb0`, mangled `_Z20ncclCommInitRankSync…`) is the allocator and synchronous initializer. It produces the 19240-byte `ncclComm` and its satellites, initializes the channel and proxy-state arrays, then runs `initTransportsRank` and `ncclRtDevCommSetup`. The `calloc(0x4B28)` is byte-confirmed (`mov $0x4b28,%edi; call calloc@plt` @`0x2c025/0x2c02a`), as is the `asyncOps` `calloc($0x48000)` @`0x2c38c`.

```c
// ncclCommInitRankSync — 0x2beb0  [HIGH: calloc sizes byte-confirmed]
ncclResult_t ncclCommInitRankSync(ncclComm **out, int nranks, int rank, int cudaDev,
        const enc_neuron_device_info *dev, bool isGlobalInit, ncclUniqueId id, char bypass):
    ncclRtSetDevice(cudaDev);
    ncclRtEventCreateWithFlags(&doneEvent, /*flags=*/2);     // comm+17672

    comm = calloc(0x4B28);                                   // 19240B  @0x2c025/0x2c02a
    comm->rank = rank; comm->nRanks = nranks;                // +17616 / +17620
    ncclRtGetDevice(&comm->cudaDev);
    comm->net_conn = ENC_CONNECTIVITY_DEFAULT;               // +17628 = 2  [FORK]
    getBusId(comm->cudaDev, &comm->busId);                   // +17632

    ncclRtHostAlloc(&comm->abortFlag, 4, 2);                 // +18192, 4B host page
    comm->hostDevComm.abortFlag = comm->abortFlag;

    comm->asyncOps    = calloc(0x48000);                     // +19160, 4096 * sizeof(ncclInfo)=72  @0x2c38c
    comm->connectSend = calloc(4 * nRanks);                  // +17576
    comm->connectRecv = calloc(4 * nRanks);                  // +17584
    comm->p2pSends    = calloc(16 * nRanks);                 // +19184
    comm->p2pRecvs    = calloc(16 * nRanks);                 // +19192

    for (i = 0; i < 32; i++) comm->channels[i].id = -1;      // stride 128 dwords (512B/channel)
    if (dev->histogram_config.enable)                        // dev+48
        comm->send_histograms = new multi_timer_histograms(0x78);   // +19232 [FORK telemetry]
    for (s = 0; s < 3; s++) init_proxy_state(&comm->proxyStateStream[s]);  // +18384, stride 192

    *out = comm;
    rc = initTransportsRank(comm, &id, dev, isGlobalInit, bypass);   // 0x28ed0  — §3
    if (rc) goto fail;
    ncclRtDevCommSetup(comm);                                 // dev-side comm mirror
    INFO("comm %p rank %d nranks %d cudaDev %d busId %lx - Init COMPLETE", …);  // init.cc:998
    return ncclSuccess;
  fail:
    bootstrapAbort(comm->bootstrap);                          // +17560
    *out = 0;
    return rc;
```

### Function Map

| Function | Addr | Size/Sig source | Role | Confidence |
|---|---|---|---|---|
| `ncclCommInitRank` | `0x2cc20` | DWARF | banner, validation, root-create, delegate | HIGH |
| `ncclCommInitRankSync` | `0x2beb0` | `_Z20ncclCommInitRankSync…` | `commAlloc` + synchronous bring-up | HIGH |
| `initTransportsRank` | `0x28ed0` | `_ZL18initTransportsRank…` (12247 B) | bootstrap + peer AllGather + topology | HIGH |
| `ncclInit` | `0x26040` | DWARF | lazy `initPlugin` + `bootstrapNetInit` + sock-timeout | HIGH |
| `ncclGetVersion` | `0x262d0` | DWARF | `*version = 2999` (`movl $0xbb7` @`0x262d9`) | HIGH |
| `ncclGetUniqueId` | `0x262f0` | DWARF | net-init then `bootstrapGetUniqueId` | HIGH |
| `initPlugin` | `0x41a80` | DWARF | mutex; `initNetPlugin(&ncclNet)`; fallback `ncclNetSocket` | HIGH |
| `ncclRtDevCommSetup` | — | (RT-side) | device-comm mirror construction | MED |

### Considerations

The version probe `ncclRtFree(0)` is a deliberate liveness check, not a leak: passing `0` to the RT free path forces the RT to report whether the device runtime is initialized before the comm allocates 19 KB. The rank-range check is the only input validation on this path; `nranks` itself is trusted (the wrappers do not bound it). `comm->net_conn = ENC_CONNECTIVITY_DEFAULT` (=2) is a Neuron-fork field at +17628 — stock NCCL has no per-comm connectivity enum here. The per-comm `multi_timer_histograms` at +19232 is allocated only when `dev->histogram_config.enable` is set, making proxy-latency telemetry an opt-in, per-communicator object (its `output_path` and 16-bucket `bucket_usecs` come straight from the RT's `enc_neuron_device_info`).

---

## 3. Bootstrap — `initTransportsRank` and the Dual Handshake

### Purpose

`initTransportsRank` (`0x28ed0`) is where init becomes distributed. It stamps `isGlobalComm` into the comm, hashes the 128-byte id, runs `bootstrapInit` to stand up the bootstrap ring, fills a 112-byte per-rank peer record, AllGathers it across the ring, and then enters the topology stage. The bootstrap itself (`bootstrapInit`, `0x3cd90`) is the **single largest fork delta** on the init path: it chooses between a **flat** ring (stock-NCCL-shaped) and a **2-tier hierarchical** intra/inter-node ring, gated by the RT's vNC count.

### Entry Point

```text
initTransportsRank @0x28ed0
  ├─ comm->isGlobalComm = isGlobalInit                        ── +17592
  ├─ Hash = getHash(id->internal, 128)
  ├─ bootstrapInit(&id, rank, nRanks, &comm->bootstrap,       ── 0x3cd90
  │                &comm->cluster_id, &comm->epoch, isGlobalInit)
  │     gate: nrt_get_total_vnc_count() == 0   (single-vNC ⇒ no bootstrap)
  │     ├─ nranks==0 ─► bootstrapInitFlat            @0x37b60   (flat ring — §3 handshake)
  │     └─ nranks!=0 ─► bootstrapInitWithHierarchy   @0x3b0f0   (2-tier — FORK)
  ├─ allData = calloc(112 * nRanks)                            ── peer-info table
  ├─ fillInfo(allData[rank])                                   ── 112B record (rank, cudaDev, hashes…)
  ├─ bootstrapAllGather(comm->bootstrap, allData, 112, "initTransportRank AllGather1")  @0x3e280
  ├─ if isGlobalInit: build mla_indexes / host_device_ids maps; neuronIsUltraServer(pod_type)
  └─ ncclTopoGetSystem → ComputePaths → TrimSystem → ComputePaths → SearchInit → Print   ── [topology]
```

### Algorithm — `initTransportsRank`

```c
// initTransportsRank — 0x28ed0  [HIGH for named call sites; fillInfo fields HIGH]
ncclResult_t initTransportsRank(ncclComm *comm, ncclUniqueId *id,
        const enc_neuron_device_info *dev, bool isGlobalInit, char bypass):
    comm->isGlobalComm = isGlobalInit;                       // +17592
    uint64_t Hash = getHash(id->internal, 128);              // mix of the 128B id

    bootstrapInit(id, comm->rank, comm->nRanks, &comm->bootstrap,
                  &comm->cluster_id, &comm->epoch, isGlobalInit);   // 0x3cd90, fills +17560/+17600/+17608

    char *allData = calloc(112 * comm->nRanks);              // one 112B peer record per rank
    peer = &allData[comm->rank];
    peer->rank        = comm->rank;                          // +0
    ncclRtGetDevice(&peer->cudaDev);                         // +4
    peer->compCap     = ncclCudaCompCap();                   // +96 (capcc | host_device_id)
    peer->host_dev_id = dev->host_device_id;                 // dev+12
    peer->mla_idx     = dev->mla_idx;                        // +104, dev+4
    peer->hostHash    = getHostHash() + Hash + dev->virtual_server_id;  // +16, dev+40
    peer->pidHash     = getPidHash() + Hash;                 // +24
    stat("/dev/shm", &peer->shmDev);                         // +? shm device id
    peer->comm        = comm;                                // +88

    bootstrapAllGather(comm->bootstrap, allData, 112, "initTransportRank AllGather1");   // 0x3e280

    if (isGlobalInit):
        build_map(mla_indexes from allData[*].mla_idx);
        build_map(host_device_ids from allData[*].host_dev_id);
        neuronIsUltraServer(dev->pod_type);                  // dev+32

    ncclTopoGetSystem(comm); ncclTopoComputePaths(comm);     // [topology] — see topology.md
    ncclTopoTrimSystem(comm); ncclTopoComputePaths(comm);
    ncclTopoSearchInit(comm); ncclTopoPrint(comm);
    return ncclSuccess;
```

### Algorithm — `bootstrapInit` dispatch and the flat handshake

The dispatch is gated twice: first on `nrt_get_total_vnc_count()` (single-vNC short-circuits the whole bootstrap to a no-op — there is no peer to reach), then on `nranks` to pick flat vs hierarchical. The flat handshake is the stock-NCCL-shaped path, recovered in full; the hierarchical path is the Neuron extension (MED — its body is large and only partly traced here; see the deep-dive backlog in [overview](overview.md#5-verification-notes)).

```c
// bootstrapInit — 0x3cd90  [HIGH on the gate + dispatch]
ncclResult_t bootstrapInit(ncclUniqueId *id, int rank, int nranks, void **commState,
        uint64_t *cluster_id, time_t *epoch, bool isGlobalInit):
    if (nrt_get_total_vnc_count() != 0):                     // @plt 0x1e430 — single-vNC gate
        return ncclSuccess;                                  // no bootstrap: nothing to connect
    if (isGlobalInit):
        setFilesLimit();                                     // getrlimit/setrlimit RLIMIT_NOFILE=rlim_max
                                                             // "Call to {get,set}rlimit failed : %s"
    if (nranks != 0):
        return bootstrapInitWithHierarchy(id, rank, nranks, commState, cluster_id, epoch);  // 0x3b0f0 [FORK]
    else:
        return bootstrapInitFlat(id, rank, nranks, commState, cluster_id, epoch, isGlobalInit); // 0x37b60

// bootstrapInitFlat — 0x37b60  [HIGH: sanity magic + INFO_TO/FROM_ROOT exchange byte-confirmed]
ncclResult_t bootstrapInitFlat(socketAddress *rootAddr, int rank, int nranks,
        extState **commState, uint64_t *cluster_id, time_t *epoch, bool wantExt):
    assert(nrt_get_total_vnc_count() == 0);                  // re-guarded

    extState *st = calloc(64);                               // 64B extState
    st->rank = rank; st->nranks = nranks;                    // +40 / +44
    *commState = st;

    extInfo info;                                            // 40B + appended git hash
    info.sanity = 0x61796c69;                                // "ilya" LE — movl $0x61796c69,(%rbp) @0x37c89
    info.rank   = rank; info.nranks = nranks;
    info.extAddressListen = bootstrapNetIfAddr;              // my listen socketAddress
    if (wantExt):                                            // isGlobalInit path
        getNcclNrtGitHash(&info.nccl_nrt_git_hash, 16);      // 0x465a0 — 16B cross-process sanity (§4)

    createListenSocket(&st->extListenFd, &myAddr);           // st+0
    int fd; connectAddress(&fd, rootAddr);                   // dial the root listener
    bootstrapNetSend(fd, &info, hdr=INFO_TO_ROOT);           // hand my info to root
    bootstrapNetRecv(fd, &reply, hdr=INFO_FROM_ROOT);        // root replies: neighbor + cluster_id + epoch
    close(fd);

    // wire the bootstrap ring: connect to ring-successor, accept from ring-predecessor
    connectAddress(&st->extRingSendFd, reply.neighborAddr);  // st+8
    bootstrapNetAccept(st->extListenFd, &st->extRingRecvFd);  // st+4

    st->peerCommAddresses = calloc(28 * nranks);             // st+16; put myAddr at [rank]
    bootstrapRingAllGather(st, st->peerCommAddresses, 28, hint);   // 0x35ba0 — fill the full ring
    st->peerCommSendFd = ncclCalloc<int>(nranks);            // st+24, init -1
    st->peerCommRecvFd = ncclCalloc<int>(nranks);            // st+32, init -1

    *cluster_id = reply.cluster_id;                          // reply[0..7]   -> comm->cluster_id (+17600)
    *epoch      = reply.epoch;                               // reply[8..15]  -> comm->epoch     (+17608)
    return ncclSuccess;
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `initTransportsRank` | `0x28ed0` | bootstrap + 112B peer AllGather + topology entry | HIGH |
| `bootstrapInit` | `0x3cd90` | vNC gate + flat/hier dispatch | HIGH |
| `bootstrapInitFlat` | `0x37b60` | flat handshake; `extInfo`/`INFO_TO_ROOT`/`INFO_FROM_ROOT` | HIGH |
| `bootstrapInitWithHierarchy` | `0x3b0f0` | 2-tier intra/inter-node handshake (calls `getNcclNrtGitHash`) | MED |
| `bootstrapCreateRoot` | `0x3ca00` | `createListenSocket` + `malloc(0x8C=140)` root-args + `pthread_create` | HIGH |
| `bootstrapRootFlat` | `0x386d0` | root listener thread (flat) | MED |
| `bootstrapRootWithHierarchy` | `0x393a0` | root listener thread (hierarchical) | MED |
| `bootstrapLocalRoot` | `0x3a360` | intra-node local root | MED |
| `bootstrapAllGather` | `0x3e280` | ring / recursive-doubling / bruck AllGather (size-1367) | HIGH addr / MED body |
| `bootstrapRingAllGather` | `0x35ba0` | ring AllGather over the bootstrap ring | MED |
| `bootstrapNetInit` | `0x3c2c0` | pick IF; fill `bootstrapNetIfName/Addr`; `bootstrapPort` | HIGH |
| `bootstrapGetUniqueId` | `0x3cc20` | env-id vs create-root; emit 128B id | HIGH |
| `getNcclNrtGitHash` | `0x465a0` | weld libnccom+libnrt git hashes into 16B sanity tag (§4) | HIGH |

### Considerations

The `bootstrapCreateRoot` root-args block is `malloc(0x8C)` = **140 bytes** (byte-confirmed: `mov $0x8c,%edi; call malloc@plt` @`0x3ca90/0x3ca95`), and the dispatch to flat-vs-hierarchical root thread happens *inside* `bootstrapCreateRoot` after a second `nrt_get_total_vnc_count()` check (`call …@plt` @`0x3cafc`): `count != 0` errors out, `isGlobalInit && nranks > vncPerInstance` spawns `bootstrapRootWithHierarchy`, else `bootstrapRootFlat`. The `extInfo.sanity = 0x61796c69` is a developer fingerprint (`"ilya"`) and serves as the first byte-pattern the root validates on an inbound `INFO_TO_ROOT` — a mismatched magic means a foreign or skewed peer. The reply's `cluster_id` and `epoch` are opaque on the libnccom side: they pass straight into `comm->cluster_id` (+17600) and `comm->epoch` (+17608) and are handed to the RT; their semantic meaning beyond pass-through was not traced (LOW).

> **GOTCHA — the single-vNC short-circuit makes bootstrap a no-op.** A reimplementer testing on one NeuronCore will see `bootstrapInit` return `ncclSuccess` *without opening any socket*, because `nrt_get_total_vnc_count() != 0` skips the entire flat/hierarchical body. There is no ring, no `extState`, no peer AllGather — `comm->bootstrap` (+17560) stays NULL and the later `bootstrapAbort` on the failure path is a safe no-op against NULL. Distributed behavior only engages when the RT reports vNC count == 0 (i.e. the multi-vNC distributed regime). Do not conclude the bootstrap is broken from a single-core run; it is gated off by design.

---

## 4. The Two Version Identities and the libnrt↔libnccom Handshake

### Purpose

libnccom and libnrt verify each other on **two independent axes**, by **two different mechanisms**, and a reimplementer that satisfies only one fails to load. Axis one is the **reverse ELF hard link**: libnccom's `nec_*`/`nrt_*` imports are all bound to symbol version `NRT_2.0.0`, so the dynamic linker rejects an libnrt that does not export that version. Axis two is the **forward numeric compat id 89**: at dlopen, libnrt's *first* dlsym is `neuronGetVersionInfo`, which writes `89` into the `compatibility_version` field; libnrt then `cmp`s that field against its own expected constant. The two checks can fail independently. A third, lower-stakes identity — the 16-byte `nccl_nrt_git_hash` — is not a load gate but a per-connection bootstrap sanity tag.

### Algorithm — the version probe (`neuronGetVersionInfo`)

```c
// neuronGetVersionInfo — 0x46150  [HIGH on the 89 store]
uint64_t neuronGetVersionInfo(nec_version_info_t *out, size_t n):
    // parse "2.31.24.0" into major/minor/patch/maintenance via std::string::find
    parse_version("2.31.24.0", &out->major, &out->minor, &out->patch, &out->maintenance);  // out[0..3]
    if (n > 0x2F) memset(&out->git_hash, 0, 16);            // out+32, clear git_hash start
    if (n > 0x37)
        out->compatibility_version = 89;                   // out+48 — movq $0x59,0x30(%rax) @0x464a8
    return …;
```

The store is byte-confirmed (`48 c7 40 30 59 00 00 00 → movq $0x59,0x30(%rax)` @`0x464a8`; `0x30 = 48` is the `compatibility_version` offset in the 56-byte `nec_version_info_t`). This `89` is the constant libnrt compares against at its dlopen gate; the two halves of the check meet on it. Note the numeric runtime version is a *different* identity entirely: `ncclGetVersion()` returns **2999** (`movl $0xbb7,(%rdi)` @`0x262d9`) — a fork constant, **not** `23124`.

### Algorithm — `getNcclNrtGitHash` (the cross-process sanity tag)

```c
// getNcclNrtGitHash — 0x465a0  [HIGH: callees byte-confirmed in disasm]
void getNcclNrtGitHash(char git_hash_out[16]):
    nec_version_info_t nccl_vi;                              // 56B
    nrt_version_t      nrt_vi;                               // 224B
    neuronGetVersionInfo(&nccl_vi, 56, …);                  // 0x465d4 call — sets compat=89, fills nccl git_hash
    memcpy(&git_hash_out[0], nccl_vi.git_hash, 8);          // 0x465ee — libnccom's own 8 bytes
    nrt_get_version(&nrt_vi);                                // 0x465f9 — boundary import @NRT_2.0.0
    memcpy(&git_hash_out[8], nrt_vi.git_hash, 8);           // 0x46614 — libnrt's 8 bytes
    // -> 16B = [libnccom hash | libnrt hash], embedded in extInfo (§3) for skew detection
```

This welds **both** libraries' build hashes into one 16-byte tag that travels in the bootstrap `extInfo` (the `wantExt` branch of `bootstrapInitFlat`). Two ranks whose libnccom *or* libnrt build differs will carry different tags and can be flagged at the root — a finer-grained skew check than the numeric `89`/`2999` constants, which only catch ABI-version drift, not build drift.

### Function Map — the reverse ABI (16 `nec_*` + 4 `nrt_*`, all `@NRT_2.0.0`)

libnccom's callbacks into libnrt. INIT-relevant rows in bold; the rest belong to the topology/proxy cells but are listed for the complete ABI count. All are `U … @NRT_2.0.0` (no addresses — they resolve into libnrt at load; libnrt-side addresses are on [abi](abi.md)).

| Symbol | Group | INIT-path role | Confidence |
|---|---|---|---|
| **`nrt_get_total_vnc_count`** | nrt query | **bootstrap gate** — `bootstrapInit`/`Flat`/`CreateRoot` (9 call sites) | HIGH |
| **`nrt_get_version`** | nrt query | **`getNcclNrtGitHash`** — fills `nrt_version_t` for the 16B sanity tag | HIGH |
| `nrt_get_instance_info` | nrt query | instance/topology metadata (topology cell) | MED |
| `nrt_get_libnccl_net` | nrt query | net-plugin handoff (`initNetPlugin`, net cell) | MED |
| `nec_get_virtual_core_size` | device sizing | pervasive — `neuronGetCommInfo`, `getDeviceInfo`, topo sizing | HIGH |
| `nec_get_device_count` | device | `getDeviceInfo` | HIGH |
| `nec_get_device_pci_bdf` | device | `getDeviceInfo` (busId/topology) | HIGH |
| `nec_build_port_and_rid_map` | topology | `neuronBuildPortAndRidMap` | HIGH |
| `nec_get_peer_mla_idx` | topology | `neuronDeviceCanAccessPeer` (connectivity during init) | HIGH |
| `nec_is_mla_available` | topology | MLA availability (topo/pod) | MED |
| `nec_mla_idx_to_rid` | topology | MLA↔RID map (topo/pod) | MED |
| `nec_rid_to_mla_idx` | topology | RID↔MLA map (topo/pod) | MED |
| `nec_get_p2p_pod_peer_node` | pod topology | inter-node pod graph | MED |
| `nec_pod_node_can_access_peer_node` | pod topology | inter-node pod reachability | MED |
| `nec_inc_semaphore` | proxy | proxy↔device completion (proxy cell) | HIGH |
| `nec_set_recv_size_bytes` | proxy | dynamic-size recv op (proxy cell) | MED |
| `nec_get_dynamic_recv_offset_bytes` | proxy | dynamic recv offset (proxy cell) | MED |
| `nec_get_dynamic_send_offset_bytes` | proxy | dynamic send offset (proxy cell) | MED |
| `nec_get_dynamic_send_size_bytes` | proxy | dynamic send size (proxy cell) | MED |
| `nec_ndl_printk` | logging | → `ncclDebugLog` sink | MED |

> **CORRECTION (NCCOM-INIT-1) —** an earlier inventory of this cell listed a `nec_get_dynamic_recv_size_bytes` callback. The binary's dynamic-proxy callbacks, re-read from `nm -D … | rg ' U nec_'`, are in fact `nec_get_dynamic_recv_offset_bytes`, `nec_get_dynamic_send_offset_bytes`, and `nec_get_dynamic_send_size_bytes` — there is **no** `recv_size_bytes` symbol; the recv side exposes only an *offset* getter, while the send side exposes both offset and size. The 16-symbol `nec_*` count is unaffected; the row name is corrected here.

### Considerations

The two load axes are validated by different machinery and can skew independently: a libnrt that kept `NRT_2.0.0` but bumped its expected compat id past `89` would resolve every symbol yet reject libnccom at the numeric `cmp`. Conversely, a libnrt that dropped `NRT_2.0.0` would fail at the dynamic linker before the numeric gate ever runs. A reimplementer must satisfy **both**. libnccom binds *only* to `NRT_2.0.0` — it does not depend on any `NRT_3.0.0` async (`nrta_*`) family — so the reverse-link surface is exactly these 20 symbols.

---

## 5. Unique-ID Generation and Network Init

### Purpose

Before any comm exists, the root rank must publish a reachable address. `neuronGetUniqueId` → `ncclGetUniqueId` does this: it lazily initializes the bootstrap network (picks an interface, records `bootstrapNetIfAddr`), then either reads a root address from the environment or creates a listening root and returns its socket address as the 128-byte `ncclUniqueId`. Every other rank receives this id out-of-band and passes it to `neuronInitComm`.

### Entry Point

```text
neuronGetUniqueId @0x46660
  └─ ncclGetUniqueId @0x262f0
       ├─ initPlugin → bootstrapNetInit(root_comm_id) @0x3c2c0   ── pick IF, set bootstrapNetIfAddr
       │     env: CCOM_SOCKET_IFNAME / CCOM_SOCKET_FAMILY ; "Bootstrap : Using%s"
       ├─ env NEURON_CCOM_SOCK_TIMEOUT → "Use socket timeout: %d"
       ├─ PtrCheck(out)
       └─ bootstrapGetUniqueId(root_comm_id, nranks, &uid, hint) @0x3cc20
            ├─ if env COMM_ID set: GetSocketAddrFromString → uid     ("Root comm Id set by environment to %s")
            └─ else: uid := bootstrapNetIfAddr ;
                     bootstrapCreateRoot(uid, idFromEnv=0, isGlobalInit=0, nranks, hint) @0x3ca00
                       └─ createListenSocket + malloc(140) + pthread_create(root listener)
  └─ copy 128B uid out via 8 XMM stores
```

### Algorithm

```c
// ncclGetUniqueId — 0x262f0  [HIGH: strings + call sites byte-confirmed]
ncclResult_t ncclGetUniqueId(const char *root_comm_id, int nranks, ncclUniqueId *out, const char *hint):
    initPlugin();                                            // 0x41a80: net plugin, once
    bootstrapNetInit(root_comm_id);                         // 0x3c2c0: pick IF, fill bootstrapNetIfAddr
    sock_timeout = getenv("NEURON_CCOM_SOCK_TIMEOUT");      // "Use socket timeout: %d"
    if (out == NULL) return ncclInvalidArgument;            // PtrCheck
    return bootstrapGetUniqueId(root_comm_id, nranks, out, hint);   // 0x3cc20

// bootstrapGetUniqueId — 0x3cc20  [HIGH]
ncclResult_t bootstrapGetUniqueId(const char *root_comm_id, int nranks,
        ncclUniqueId *id, const char *hint):
    memset(id, 0, 128);
    if (root_comm_id && root_comm_id[0]):                   // env-provided root address
        if (!GetSocketAddrFromString(&id->addr, root_comm_id)):
            WARN("Invalid Comm Id [%s], please use format: <ipv4>:<port> | [<ipv6>]:<port> | <hostname>:<port>");
            return ncclInvalidArgument;
        // id now carries the env root socketAddress; no local root spawned
    else:
        memcpy(&id->addr, &bootstrapNetIfAddr, sizeof(socketAddress));   // my IF address
        bootstrapCreateRoot(id, /*idFromEnv=*/0, /*isGlobalInit=*/0, nranks, hint);  // spawn local root
    return ncclSuccess;
```

### Considerations

The id is **a socket address by value**: the 128-byte `ncclUniqueId.internal` holds the root listener's `socketAddress` (IP:port), which is why the wrapper layer can carry it across 8 XMM registers and why a peer that receives it can `connectAddress` directly. `bootstrapNetInit` honors `CCOM_SOCKET_IFNAME` / `CCOM_SOCKET_FAMILY` for interface selection (strings `"NCCL_SOCKET_IFNAME set by environment to %s"`, `"NCCL_SOCKET_FAMILY set by environment to %s"`) and fails loudly with `"NET/Socket : No usable listening interface found"` when no interface matches. The socket timeout comes from `NEURON_CCOM_SOCK_TIMEOUT` and bounds every bootstrap socket op — a reimplementer must thread it through `bootstrapNetSend/Recv/Accept`.

---

## 6. Verbatim Strings → Function

The init/bootstrap strings, each pinned to its emitting function. They are the cheapest cross-check a reimplementer has: grep the binary, land on the call site.

| String | Function | Confidence |
|---|---|---|
| `"NCCL version 2.31.24+nrt2.0"` | `showVersion` (in `ncclCommInitRank`) | HIGH |
| `"comm %p rank %d nranks %d cudaDev %d busId %lx - Init COMPLETE"` | `ncclCommInitRankSync` (init.cc:998) | HIGH |
| `"Invalid rank requested : %d/%d"` | `ncclCommInitRank` (init.cc:1024) | HIGH |
| `"Root comm ID set by environment to %s"` | `ncclCommInitRank` (init.cc:1012) | HIGH |
| `"Root comm Id set by environment to %s"` | `bootstrapGetUniqueId` (bootstrap.cc) | HIGH |
| `"Invalid Comm Id [%s], please use format: …"` | `bootstrapGetUniqueId` | HIGH |
| `"COMM_ID must be specified"` | `bootstrapNetInit` | HIGH |
| `"Bootstrap : Using%s"` / `"NET/Socket : No usable listening interface found"` | `bootstrapNetInit` | HIGH |
| `"NCCL_SOCKET_IFNAME set by environment to %s"` | `findInterfaces` | HIGH |
| `"Use socket timeout: %d"` | `ncclInit` / `ncclGetUniqueId` (env `NEURON_CCOM_SOCK_TIMEOUT`) | HIGH |
| `"Call to {get,set}rlimit failed : %s"` | `setFilesLimit` (in `bootstrapInit`) | HIGH |
| `"Neuron core ID of calling thread is uninitialized"` | `initTransportsRank` | HIGH |
| magic `0x61796c69` (`"ilya"`) | `extInfo.sanity` in `bootstrapInitFlat` @`0x37c89` | HIGH |

---

## 7. The Non-Init Exports (out of scope, for ABI completeness)

The 37 `neuron*` exports total includes 24 net/proxy data-plane entry points that are **not** on the init path. They are listed once here so the 37-count reconciles against the [overview](overview.md) and [abi](abi.md) pages; their bodies are documented on the net/proxy siblings.

| Group | Exports | Sibling |
|---|---|---|
| Network proxy lifecycle | `neuronStartNetworkProxy` @`0x483a0`, `neuronStopNetworkProxy` @`0x487d0`, `neuronNetworkProxyProgress` @`0x48930` | [proxy-engine](proxy-engine.md) |
| Net transport | `neuronNetListen` @`0x49280`, `neuronNetConnect` @`0x49320`, `neuronNetAccept` @`0x493c0`, `neuronNetIsend` @`0x49640`, `neuronNetIrecv` @`0x496f0`, `neuronNetIflush` @`0x497f0`, `neuronNetTest` @`0x498e0` | [transport-efa](transport-efa.md) |
| MR / close | `neuronNetRegMr` @`0x48a30`, `neuronNetDeregMr` @`0x48d50`, `neuronNetRegMrOG` @`0x49460`, `neuronNetDeregMrOG` @`0x495a0`, `neuronNetGetMrKey` @`0x49500`, `neuronNetCloseSend/Recv/Listen` @`0x49980/0x49a20/0x49ac0` | [transport-efa](transport-efa.md) |
| Net connector / device id | `neuronGetNetConnector` @`0x49070`, `neuronGetOFIHandle` @`0x49260`, `neuronGetPeerDeviceId` @`0x48ff0`, `neuronPreferredNcRidNic` @`0x46810`, `neuronPreferredNcRidNicBdf` @`0x46710` | [net-plugin](net-plugin.md) |
| Plugin teardown | `neuronFiniPlugin` @`0x49240` | [net-plugin](net-plugin.md) |

13 (init, §1) + 24 (above) = **37**, matching the byte-verified export count.

---

## Cross-References

- [Overview: the NCCL Fork (2.31.24+nrt2.0)](overview.md) — the Part-XII map; the 8-stage host-orchestration pipeline this page details stages 1–4 of
- [The libnrt ↔ libnccom ABI](abi.md) — the dlopen/dlsym mechanics, the libnrt-side compat `cmp`, and the libnrt addresses of the 16 `nec_*` + 4 `nrt_*` callbacks named here
- [Topology Detection and Graph Build](topology.md) — `ncclTopoGetSystem` onward, the stage `initTransportsRank` enters after the peer AllGather
- [The libnrt ↔ libnccom Boundary (nec_ / nccl)](../collectives/nccl-boundary.md) — the on-device collectives view of the same boundary, from the libnrt/`enc` side
- [Proxy & Progress Engine](proxy-engine.md) — the data plane that init's `neuronStartNetworkProxy` feeds; `nec_inc_semaphore` completion bridge
- [back to index](../index.md)
