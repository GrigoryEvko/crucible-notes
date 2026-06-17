# Multi-Node Collectives: the libnccom NCCL Fork — Section Map

> **Binary:** `aws-neuronx-collectives_2.31.24.0-1a31ba186` → `opt/aws/neuron/lib/libnccom.so.2.31.24` (symlink chain `libnccom.so → .so.2 → .so.2.31.24`; SONAME `libnccom.so.2`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`** (1088 symtab / 281 dynsym). Build-id `9c00176c081788c9435d27d11bb40e92495463f0`. `.text` VMA == file offset for the cited band; every address pins to this build.
> **Fork base:** NVIDIA NCCL `2.31.24 + nrt2.0`, internal tree name **KaenaNCCL** (`/opt/workspace/KaenaNCCL/src`); `showVersion` banner `"NCCL version 2.31.24+nrt2.0"`. **Status:** Reimplementation-grade map · **Evidence grade:** Confirmed (byte-anchored: build-id, symbol counts, version constants, transport strings re-read directly from the binary) · **Part XII — Multi-Node Collectives** · [back to index](../index.md)

## Abstract

This page is the **map of the libnccom multi-node collective stack** as reconstructed from the (unstripped, DWARF-bearing) `libnccom.so`. If you already hold a mental model of upstream NVIDIA NCCL — a `ncclComm` per rank, a socket-based bootstrap that exchanges a 128-byte `ncclUniqueId`, a topology XML parsed into a node graph, a ring/tree search that produces per-channel rank orders, and a proxy thread that drives the network while CUDA warp kernels move and reduce the data — then you already know **most** of libnccom. It is a fork of that exact codebase: `init.cc`, `bootstrap.cc`, `graph/topo.cc`, `graph/search.cc`, `graph/paths.cc`, `channel.cc`, and `transport/p2p.cc` are all present as the same functions, parsing the same `ncclXml → ncclTopoSystem → ncclTopoGraph` representation so the inherited tuning and algorithm-selection code runs unchanged.

The **key structural fact**, and the single most important departure for a reimplementer, is what is *missing*: **on Trainium there are no CUDA warp kernels.** `nm -C` finds zero device-side primitive templates — no `genericOp`, no `recvReduceSend`, no `directSend`, no `__device_stub`, no `cudaLaunchKernel`. The whole GPU "primitives" layer that upstream NCCL compiles per arch simply does not exist here. In its place, **the "primitive" IS the host-side proxy state machine.** `libnccom` performs host-side orchestration only — communicator init / bootstrap, topology and graph build, ring (and the Neuron-original `kangaRing`) construction, and the proxy / progress engine — while the actual device data movement is done by **DMA plus the Trainium Q7 engine**, and the reduction is performed by the **Trainium device engine**, not by libnccom. `libnccom` only DMAs operands into the engine and posts/polls the network; it never touches a register file of compute lanes.

The page is organized as the orientation layer for the eleven sibling pages of Part XII. It documents (1) the host-orchestration pipeline from `neuronInitGlobalComm` down to the proxy progress loop; (2) the two transports — intra-node P2P versus inter-node `NET_NEURON` over EFA / libfabric; (3) the libnrt ↔ libnccom boundary that loads this library at runtime; and (4) the inherited-versus-forked code split that tells a reimplementer which half is reusable public NCCL and which half is AWS-Neuron-original. Every byte-level derivation — the bootstrap dual-path handshake, the `topology::getPaths` priority search, the proxy `ncclProxyArgs` FIFO, the EFA connection lifecycle, the ABI tables — lives on a sibling page that this map links rather than duplicates.

For reimplementation, the contract of the libnccom layer is:

- **Reproduce the host control plane, not a device kernel.** The deliverable of libnccom is a populated `ncclComm` (`calloc 0x4B28 = 19240` bytes) plus a running proxy thread; the data plane is the RT's DMA + Q7 engine, reached through 16 `nec_*` reverse-callbacks.
- **The init / bootstrap state machine** — the `neuron*` wrapper → `nccl*` core → `bootstrap.cc` chain, the 128-byte `ncclUniqueId` carried by value across 8 XMM registers, and the **Neuron-fork dual bootstrap** (flat vs 2-tier hierarchical) gated by `nrt_get_total_vnc_count()`.
- **The topology → graph → channel pipeline** — the inherited NCCL XML/system/search machinery plus the Neuron-original `topology`/`NeuronNode`/`NeuronEdge` priority search and the `kangaRing` / `mla-cycle` ring stitching.
- **The two-axis version/compat handshake** — the ELF `NRT_2.0.0` hard-link (reverse) and the numeric compatibility id `89` (`0x59`) checked by the RT at dlopen (forward); a reimplementer that misses either axis fails to load.

## At a glance

| | |
|---|---|
| **What it is** | Host-side multi-node collective orchestrator; NCCL `2.31.24+nrt2.0` fork (KaenaNCCL) |
| **Data plane** | **Not** in this library — DMA + Trainium **Q7 engine** + device reduction engine, via `nec_*` callbacks |
| **Communicator object** | `ncclComm` — `calloc(0x4B28)` @ `0x2c025` = **19240 bytes** |
| **Init entry points** | `neuronInitGlobalComm` @`0x46c40` · `neuronInitComm` @`0x46d90` · `neuronGetUniqueId` @`0x46660` |
| **Graph-build entry** | `neuronBuildGraphComm` @`0x46e90` → `ncclCommBuildGraphRank` @`0x26da0` → `buildGraphTransportsRank` @`0x24ef0` |
| **Topology synth/parse** | `ncclTopoGetSystem` @`0x58860` → `neuronTopoGetXml` @`0x623a0` → `ncclTopoGetSystemFromXml` @`0x58540` |
| **Proxy primitives** | `netNeuronSendProxy` @`0x4e510` · `netNeuronRecvProxy` @`0x4fe20` (the send/recv "primitives") |
| **Version constants** | `ncclGetVersion()` → `movl $0xbb7` = **2999** (fork int, not 23124) @`0x262d9`; compat id `movq $0x59` = **89** @`0x464a8` |
| **Boundary load** | libnrt `dlopen("libnccom.so", RTLD_NOW)` → dlsym 37 `neuron*` (gate #0 = `neuronGetVersionInfo`, compat must == 89) |
| **Boundary link (reverse)** | DT_NEEDED `libnrt.so.1`; VERNEED `NRT_2.0.0`; imports **16 `nec_*` + 4 `nrt_*`**, all @`NRT_2.0.0` |
| **Transports** | intra-node **P2P** (`transport/p2p.cc`) · inter-node **`NET_NEURON`** over EFA / libfabric (`net_neuron.cc`) |
| **Device kernels** | **Zero** — `nm -C` finds no `ncclKernel` / `__device_stub` / `cudaLaunchKernel` |

> **QUIRK — the proxy is the primitive.** Upstream NCCL's hot path is a CUDA kernel (`ncclKernel_*`) that runs `recvReduceCopySend`-class device functions; the host proxy thread only *feeds* the network. On Trainium there is **no such kernel** — `nm -C libnccom.so` returns none, and there is no `cudaLaunchKernel` call site. The send/recv "primitive" is literally the host function `netNeuronSendProxy` (`0x4e510`) / `netNeuronRecvProxy` (`0x4fe20`): they walk the per-op address/op FIFOs, DMA operands into the local Trainium engine, and (on TRN2/TRN3) issue an RDMA-READ of the peer. A reimplementer who ports NCCL by re-targeting its device kernels to Trainium has ported the wrong half of the library; the reusable half is the host orchestration + proxy loop, and the data movement must be rebuilt on the RT's DMA/Q7 path. This is the central design fact of Part XII.

---

## 1. The host-orchestration pipeline

libnccom is, end to end, a control-plane library. One distributed collective communicator is brought up by the flow below; the data plane it ultimately drives lives entirely behind the `nec_*` callbacks into libnrt. Stages 1–4 are init/bootstrap (sibling **[comm-init](comm-init.md)**), stage 5 is topology (sibling **[topology](topology.md)**), stage 6 is the algorithm wiring (siblings **[algorithm-ring](algorithm-ring.md)** / **[algorithm-tree](algorithm-tree.md)**), and stages 7–8 are the proxy/transport runtime (siblings **[proxy-engine](proxy-engine.md)** / **[send-recv-prims](send-recv-prims.md)** / **[transport-intra](transport-intra.md)** / **[transport-efa](transport-efa.md)**).

```text
libnrt dlopen("libnccom.so", RTLD_NOW) + dlsym 37 neuron*   [gate #0: neuronGetVersionInfo, compat==89]
        │
   [1]  UNIQUE-ID GEN (root rank)   neuronGetUniqueId @0x46660 → ncclGetUniqueId @0x262f0
        │      → initPlugin → bootstrapNetInit @0x3c2c0   (pick IF, bootstrapNetIfAddr)
        │      → bootstrapGetUniqueId @0x3cc20 → bootstrapCreateRoot @0x3ca00
        │            (createListenSocket + pthread_create root listener)  ── 128B ncclUniqueId out
        │
   [2]  COMM INIT      neuronInitGlobalComm @0x46c40 (global) | neuronInitComm @0x46d90 (per-rank)
        │      → ncclRtSetDevice → ncclCommInitRank @0x2cc20
        │            → showVersion "NCCL version 2.31.24+nrt2.0"
        │            → ncclCommInitRankSync @0x2beb0  ── comm = calloc(0x4B28) = 19240B
        │
   [3]  BOOTSTRAP      initTransportsRank @0x28ed0 → bootstrapInit @0x3cd90
        │      gate: nrt_get_total_vnc_count() == 0 required (single-vnc shortcuts bootstrap)
        ├── nranks==0 ─► bootstrapInitFlat @0x37b60        (extInfo sanity magic 0x61796c69 "ilya")
        └── nranks!=0 ─► bootstrapInitWithHierarchy @0x3b0f0  (2-tier intra/inter-node — FORK delta)
        │      → fillInfo (112B peer record) → bootstrapAllGather @0x3e280
        │
   [4]  PEER INFO      bootstrapAllGather → 112B/peer table (rank, cudaDev, hostHash, pidHash, mla_idx…)
        │
   [5]  TOPOLOGY       ncclTopoGetSystem @0x58860 → neuronTopoGetXml @0x623a0 (synth Neuron XML)
        │      → ncclTopoGetSystemFromXml @0x58540 → ncclTopoComputePaths @0x647a0 → verifyTopology
        │
   [6]  GRAPH/RING     neuronBuildGraphComm @0x46e90 → ncclCommBuildGraphRank @0x26da0
        │      → buildGraphTransportsRank @0x24ef0:
        │            ncclTopoCompute @0x6ab30  (OLD recursive DFS  |  NEW topology::getPaths @0x5e400)
        │            → ncclTopoPreset @0x6c1f0 → bootstrap AllGather(ncclTopoRanks)
        │            → ncclTopoPostset @0x6c490  ("built %d kangaring paths", "built %d mla cycles")
        │            → ncclTransportP2pSetup @0x3eb80 → ncclTopoComputeP2pChannels @0x654f0
        │            → per channel: initChannel @0x33c30 + ncclTransportP2pConnect @0x3e9c0
        │
   [7]  PROXY START    neuronStartNetworkProxy @0x483a0  (net_ops_info_t + net_src/dest_addr_t arrays)
        │      → ncclProxyArgs FIFO (sizeof 0x160 = 352)
        │
   [8]  PROGRESS LOOP  ncclProxyProgress @ (neuronNetworkProxyProgress @0x48930)
                 ├─ netNeuronSendProxy @0x4e510   ── DMA operand → Trainium engine; post net send
                 └─ netNeuronRecvProxy @0x4fe20   ── pre-post irecv; RDMA-READ peer (TRN2/TRN3)
                       │
                       ▼
                 DATA PLANE (NOT in libnccom): DMA + Q7 engine + device reduction engine, via nec_*
```

The split is clean: stages 1–6 build state (a populated `ncclComm`, a parsed topology, per-channel rings), stage 7 hands a batch of op descriptors to the proxy, and stage 8 is the steady-state loop that DMAs operands and posts/polls the network until completion. Reduction never appears in libnccom code — `netNeuron*Proxy` only moves bytes; the Trainium device engine sums them.

### Entry-point anchors

The orchestration entry points a reimplementer must reproduce, with their boundary role. All are byte-confirmed in the DWARF-bearing binary.

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `neuronGetUniqueId` | `0x46660` | root-rank id generation; emits 128B `ncclUniqueId` via 8 XMM stores | HIGH |
| `neuronInitGlobalComm` | `0x46c40` | global (multi-node) comm init; `isGlobalInit=1` | HIGH |
| `neuronInitComm` | `0x46d90` | per-rank comm init from a pre-shared id; `isGlobalInit=0` | HIGH |
| `ncclCommInitRankSync` | `0x2beb0` | `commAlloc` — `calloc(0x4B28)`, channel/proxy state init | HIGH |
| `initTransportsRank` | `0x28ed0` | bootstrap + peer-info AllGather + topology build | HIGH |
| `bootstrapInit` | `0x3cd90` | flat-vs-hierarchical dispatch (gated on `nrt_get_total_vnc_count`) | HIGH |
| `neuronBuildGraphComm` | `0x46e90` | graph/channel build entry (separate RT call) | HIGH |
| `buildGraphTransportsRank` | `0x24ef0` | master graph orchestrator: search → preset → postset → P2P setup | HIGH |
| `neuronStartNetworkProxy` | `0x483a0` | hands `net_ops_info_t` + addr arrays to the proxy | HIGH |
| `netNeuronSendProxy` / `netNeuronRecvProxy` | `0x4e510` / `0x4fe20` | the host-side send/recv primitives | HIGH |
| `neuronFreeComm` | `0x47f90` | teardown (`= ncclCommDestroy`) | HIGH |

---

## 2. The two transports

A collective ring crosses two kinds of edge: **intra-node** between NeuronCores/MLAs inside one server, and **inter-node** between servers over the EFA fabric. libnccom selects and connects them in `ncclTransportP2pSetup` @`0x3eb80` (`p2pCanConnect` vs `netNeuronCanConnect`), and the channel-emission strings name them verbatim: `Channel %02d : %d[%lx] -> %d[%lx] [send] via NET_NEURON/%s/%d%s` for the inter-node leg. They are documented in detail on **[transport-intra](transport-intra.md)** and **[transport-efa](transport-efa.md)**; this map fixes the axes.

| Dimension | Intra-node P2P | Inter-node `NET_NEURON` (EFA / libfabric) |
|---|---|---|
| Source file (fork) | `transport/p2p.cc` | `net_neuron.cc` + `libnccom-net.so` net plugin |
| Setup predicate | `p2pCanConnect` (intra-MLA / D2D / shared-memory edges) | `netNeuronCanConnect` (cross-node, EFA-reachable) |
| Connect / setup | `p2pSendSetup` / `p2pRecvSetup` → `ncclTransportP2pConnect` @`0x3e9c0` | `netNeuronSendSetup` / `netNeuronRecvSetup` → `neuronGetNetConnector` @`0x49070` |
| Send/recv primitive | proxy DMA between local engine buffers | `netNeuronSendProxy` @`0x4e510` / `netNeuronRecvProxy` @`0x4fe20` |
| Wire mechanism | local DMA into the Trainium engine; no network | OFI/RDMA: post `isend`/`irecv`; **RDMA-READ (iread, op `0xa8`)** of peer on TRN2/TRN3 |
| Net plugin handoff | n/a | `nrt_get_libnccl_net` → `initNetPlugin(&ncclNet)`; fallback `ncclNetSocket`; plugin `ncclNetPlugin_v4/v5/v6` |
| MR / addressing | none (local) | `neuronNetRegMr` / `neuronNetGetMrKey`; `net_src_addr_t` / `net_dest_addr_t` (`dst_rank`, `dst_addr`, `dst_mhandle`) |
| Edge locality (topo) | `EdgeSharedMemory` / `EdgeD2D` / `EdgeRMTV` / `EdgeLocal` | `EdgeRemoteMLA` + the inter-node **pod graph** (`nec_pod_type` P2P / SWITCH) |
| Sibling page | [transport-intra](transport-intra.md) | [transport-efa](transport-efa.md), [net-plugin](net-plugin.md) |

> **NOTE —** the inter-node leg is the one place where Trainium semantics surface in the proxy code: `netNeuronRecvProxy` issues an **RDMA READ** of the peer's data/semaphore (`iread`, op `0xa8`), not a write, so the receiver pulls rather than the sender pushing. The `nec_inc_semaphore` reverse-callback (libnrt `0x1bfd40`, inlined `mov %esi,(%rdi)`) is what closes the proxy↔device completion handshake. Full FIFO / slot / protocol detail is on **[send-recv-prims](send-recv-prims.md)** and **[proxy-engine](proxy-engine.md)**.

---

## 3. The libnrt boundary (how this library is loaded)

libnccom is never linked directly by an application; **libnrt `dlopen`s it at runtime** and resolves a fixed ABI. The boundary is asymmetric in linkage but symmetric in version policy, and it is the subject of the dedicated **[abi](abi.md)** page. The map needs only its shape:

- **Forward (libnrt → libnccom): runtime `dlopen`.** libnrt's embedded `ncclInit` (`0x1bff30`) calls `dlopen("libnccom.so", RTLD_NOW)`, then `dlsym`s **37 `neuron*`** entry points into a contiguous `.bss` fn-ptr table (`0xc967b8..0xc968d8`, 8-byte stride). All 37 are NULL-checked; the first failure zeroes every slot and degrades to **no collectives** — it is all-or-nothing.
- **Gate #0 is the version probe.** The *first* dlsym is `neuronGetVersionInfo`; libnrt calls it into a 56-byte `nec_version_info_t` and executes `cmpq $0x59,0x30(%rsp)` (`0x1bff9b`) — the `compatibility_version` field (offset 48) **must equal 89** or init aborts with `"Incompatible version of aws-neuronx-collectives"`. libnccom writes that 89 at `movq $0x59,0x30(%rax)` (`0x464a8`). The two halves of the compat check meet on the constant **89**.
- **Reverse (libnccom → libnrt): hard link.** DT_NEEDED `libnrt.so.1`, VERNEED requires `(libnrt.so.1, NRT_2.0.0)`. libnccom imports **16 `nec_*`** device/topology/proxy callbacks + **4 `nrt_*`** runtime queries, all `@NRT_2.0.0`. The data plane is reached entirely through these: `nec_get_virtual_core_size` (vcore sizing, the most-used callback), `nec_inc_semaphore` (proxy completion), `nec_get_dynamic_{send,recv}_*_bytes` / `nec_set_recv_size_bytes` (dynamic-size proxy ops), the MLA/pod topology callbacks (`nec_is_mla_available`, `nec_mla_idx_to_rid`, `nec_get_p2p_pod_peer_node`, …), and the 4 `nrt_*` (`nrt_get_total_vnc_count` — the bootstrap gate, `nrt_get_version`, `nrt_get_instance_info`, `nrt_get_libnccl_net`).

> **GOTCHA — two skew axes, two mechanisms.** Compatibility is enforced on **two independent dimensions**: the ELF symbol version (`NRT_2.0.0`, checked by the dynamic linker on the *reverse* hard link) and the numeric id **89** (checked by libnrt's own `cmpq` on the *forward* dlopen path). They are validated by different machinery and can fail independently — a libnrt that kept `NRT_2.0.0` but bumped the expected compat id past 89 would load the symbols yet reject the library at the numeric gate. A reimplementer must satisfy both. (libnccom binds *only* to `NRT_2.0.0`; it does not depend on the `NRT_3.0.0` `nrta_*` async family.)

---

## 4. What is inherited, what is forked

The library is a fork, and the reimplementer's leverage is knowing which code is stock NCCL (reusable as-is from public NCCL) and which is AWS-Neuron-original (must be rebuilt). The split is unambiguous in the binary — stock files appear as `_GLOBAL__sub_I_*` ctors and in assert/file strings; Neuron files are `graph/topo_neuron_{sunda,cayman,mariana}.cc` and the `topology`/`NeuronNode`/`NeuronEdge`/`NeuronPlatform` hierarchy.

| Layer | Provenance | Evidence |
|---|---|---|
| `init.cc` / `bootstrap.cc` core | Inherited NCCL, lightly wrapped | `ncclCommInitRank`/`Sync`, `bootstrapInit`/`AllGather` present; `showVersion` banner |
| Bootstrap **dual path** (flat + 2-tier hierarchical) | **Fork** | `bootstrapInitWithHierarchy` @`0x3b0f0`, `bootstrapRootWithHierarchy`, `bootstrapLocalRoot`; gated by `nrt_get_total_vnc_count` |
| `graph/{topo,xml,paths,search}.cc`, `channel.cc`, `transport/p2p.cc` | Inherited NCCL | all functions present with stock names; XML/system/graph structs match NCCL |
| Neuron XML synthesis (`topo_neuron_*.cc`) | **Fork** | `neuronTopoGetXml`, `{inf2,trn1,trn2,trn3}TopoGetXml`, `NeuronPlatformTRN{1,2,3}` vtables |
| `topology`/`NeuronNode`/`NeuronEdge` priority search ("new algorithm") | **Fork** | `topology::getPaths` @`0x5e400`; gated by env `NCCL_GRAPH_USE_NEW_ALGORITHM` |
| `kangaRing` / `mla-cycle` ring stitching | **Fork** | `ncclTopoPostset` strings `"built %d kangaring paths"` / `"built %d mla cycles"`; `JbogKangaringPath[4][4]` |
| `ncclComm` pod/kangaring/mla fields | **Fork** | `nKangaRingChannels` @+16384, `kangaRing[4][64]` @+16388, pod block @+19208 |
| Device-side primitive kernels | **Removed** | `nm -C` finds none; replaced by host proxy + DMA/Q7 |
| `net_neuron.cc` (EFA proxy primitives) | **Fork** (over aws-ofi-nccl) | `netNeuron{Send,Recv}Proxy`; `NET_NEURON/%s/%d` strings |
| `enqueue.cc` cost model / runtime cost | **Stub** | per the tuning cell, init builds topology + transports only |

The tuning / cost model is effectively dead in this build — covered on **[tuning](tuning.md)** — and the tree / CollNet algorithm path is present but unused (**[algorithm-tree](algorithm-tree.md)**). The live algorithm path is the ring / kangaRing construction (**[algorithm-ring](algorithm-ring.md)**). The reimplementation rule of thumb: anything named `nccl*` can be taken from upstream NCCL `2.31.24`; anything named `neuron*` / `Neuron*` / `kanga*` / `mla*`, plus the dual bootstrap and the `net_neuron` proxy, is Neuron-original and is where the genuine reverse-engineering work lives.

---

## 5. Verification notes

> Every anchor on this map was re-read directly from the binary, independent of the decompile sidecars:
> - **Build-id / SONAME / link surface** — `readelf -n` → `9c00176c081788c9435d27d11bb40e92495463f0`; `readelf -d` → SONAME `libnccom.so.2`, DT_NEEDED `libnrt.so.1`; `readelf -V` → VERNEED `NRT_2.0.0`. Exact.
> - **Symbol counts** — `nm -D | rg -c`: **37** `T neuron*`, **16** `U nec_*`, **4** `U nrt_*`. Exact.
> - **Version constants** — `objdump -d`: `262d9: movl $0xbb7` (`ncclGetVersion` → 2999); `464a8: movq $0x59,0x30(%rax)` (compat id 89). Exact.
> - **No device kernels** — `nm -C | rg -ci 'ncclKernel|__device_stub|cudaLaunchKernel'` → **0**. This is the central structural claim and it is byte-confirmed.
> - **Transport / proxy strings** — `strings -a`: `"NCCL version 2.31.24+nrt2.0"`, `"2.31.24.0"`, `/opt/workspace/KaenaNCCL/src`, `Channel … via NET_NEURON/%s/%d`, `ncclProxyProgress`, `kangaRing`, `built %d kangaring paths`. Present.
>
> **[MEDIUM]** The OLD-vs-NEW topology-search branch condition inside `ncclTopoCompute` @`0x6ab30` (recursive DFS vs `topology::getPaths`) is read from decompiled control flow plus the env knob `NCCL_GRAPH_USE_NEW_ALGORITHM`; the exact selection predicate is detailed (and partly inferred) on [topology](topology.md). **[MEDIUM]** `bootstrapAllGather`'s selection among ring / recursive-doubling / bruck (`bootstrap_message_type_t` values 1 / 7 / 8) is value-dependent and not byte-traced here. **[LOW]** The semantic meaning of `cluster_id` / `epoch` beyond opaque pass-through to the RT was not traced. The on-device reduction engine and Q7 data path are owned by the RT and documented in Parts VII–IX, not re-derived here.

---

## Cross-References

**Part XII — libnccom siblings (this map orients all of them)**
- [Communicator Init and Bootstrap](comm-init.md) — the `neuron*`→`nccl*`→`bootstrap.cc` chain, dual-path handshake, `ncclComm` layout
- [Topology Detection and Graph Build](topology.md) — `ncclTopoGetSystem` / `neuronTopoGetXml`, the inherited + Neuron-native graph search
- [Ring Algorithm Construction (Host-Side)](algorithm-ring.md) — `ncclTopoPreset`/`Postset`, ring and `kangaRing` stitching
- [Tree / CollNet (Dead Code)](algorithm-tree.md) — the present-but-unused tree / collnet path
- [Send / Recv Primitives and Protocols](send-recv-prims.md) — `netNeuron{Send,Recv}Proxy` as the host-side primitives, FIFO/slot/protocol detail
- [Proxy & Progress Engine](proxy-engine.md) — `ncclProxyArgs` (0x160), the progress loop, the `nec_inc_semaphore` bridge
- [Tuning Model (the Dead Cost Model)](tuning.md) — why `enqueue.cc`'s cost model is a stub in this build
- [Intra-Node Transport (P2P)](transport-intra.md) — `p2pCanConnect`, local-DMA edges
- [Inter-Node Transport: EFA / libfabric](transport-efa.md) — `netNeuronCanConnect`, OFI/RDMA, the `iread` peer-read path
- [The Net Plugin (ncclNet_v6)](net-plugin.md) — `nrt_get_libnccl_net` handoff, `ncclNetPlugin_v4/v5/v6`, socket fallback
- [The libnrt ↔ libnccom ABI](abi.md) — 37 `neuron*` dlsym targets, 16 `nec_*` + 4 `nrt_*` reverse imports, the two-axis compat gate

**The on-device half (the data plane libnccom drives)**
- [On-Device Collectives](../collectives/overview.md) — the enc/encd/cc_op compute architecture that performs the reduction libnccom only DMAs into

**Runtime spine that hosts this library**
- [Runtime Core](../runtime/overview.md) — libnrt, which `dlopen`s libnccom and exports the `nec_*`/`nrt_*` callbacks
- [The Hot Inference Path](../exec/overview.md) — execution context that the collective proxy runs alongside
- [DMA Descriptors](../dma/overview.md) — the descriptor layer the proxy ultimately drives for data movement
- [back to index](../index.md)
