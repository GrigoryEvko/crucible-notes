# Intra-Node Transport (P2P)

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**.*
> *Evidence grade: **Confirmed (byte-anchored)** — the `ncclTransports[2]` table, the `p2pTransport` vtable relocs, the no-op runtime stubs, and every struct offset below were re-read from the binary (`nm -C` / `objdump -R -d` / `readelf -S` / `gdb ptype`). · **`.text` VMA == file offset**; `.data` and **`.data.rel.ro` VMA == file offset + `0x1000`** (verified: `.data.rel.ro` sh_addr `0xa7bf8`, sh_offset `0xa6bf8`). Decompiler-derived control flow inside the connect/setup bodies is tagged MED. Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

The intra-node transport is the path a collective takes between two ranks that live in the **same server** — two NeuronCores / MLAs on one Trainium instance. If you already hold a mental model of upstream NVIDIA NCCL's transport layer — a global `ncclTransports[]` table of `{canConnect, send, recv}` vtables, a `selectTransport` loop that walks that table per peer pair and keeps the first transport whose `canConnect` returns true, a bootstrap exchange of a small `ncclConnect` blob, and a `connect()` that wires a FIFO descriptor (`ncclConnInfo`) into the channel — then you already know the *shape* of this page. The Neuron fork keeps that two-tier architecture verbatim: `transport/p2p.cc` is present as the same functions, and the framework drivers `ncclTransportP2pConnect` / `ncclTransportP2pSetup` run unchanged.

The **single most important structural fact** for a reimplementer is what the table contains. Upstream NCCL ships **three** transports — `p2pTransport`, `shmTransport` (a host-staged `/dev/shm` data plane), and `netTransport`. This fork ships **two**: `ncclTransports[2]` (`.bss` @`0xb13c0`) is populated by `_GLOBAL__sub_I_transport.cc` (@`0x202b0`) with `[0] = p2pTransport` ("P2P", intra-node, *this page*) and `[1] = netTransportNeuron` ("NET_NEURON", inter-node, owned by [transport-efa](transport-efa.md)). **There is no SHM data transport.** `nm -C` finds zero `shm*` transport symbols; the only `/dev/shm` string (@`0x8b873`) is the bootstrap rendezvous file, not a data plane. Intra-node movement is therefore P2P-only: a peer rank's device buffer is reused directly by the local rank.

The second fact a reimplementer must internalize is the **Trainium specialisation**: the runtime IPC / peer-access / device-memcpy primitives this transport calls are **no-op stubs** — `neuronIpcGetMemHandle` (@`0x441b0`), `neuronIpcOpenMemHandle` (@`0x441a0`), `neuronIpcCloseMemHandle` (@`0x441c0`), `neuronDeviceEnablePeerAccess` (@`0x44180`) and `neuronMemcpy` (@`0x44210`) each compile to `endbr64; xor eax,eax; ret` (byte-confirmed). That is consistent with a **unified / shared device address space**: there is no CUDA-style handle export/import to do, because a raw peer `devPtr` is already valid from the local rank. Real reachability is decided not by these stubs but by **topology** (`ncclTopoCheckP2p` @`0x63f00`, `ncclTopoCheckPdsP2p` @`0x64410`) and by `neuronDeviceCanAccessPeer` (@`0x454a0`), which is the one *real* function — it queries libnrt for MLA/pod locality. The transport is also **proxy-free**: its `proxy`/`allocRequest`/`freeRequest` vtable slots are NULL, so once connected, the device-side collective kernels post/poll the peer FIFO directly through the pointers wired into `ncclConnInfo`; there is no host proxy thread and no doorbell for P2P (that machinery belongs to NET_NEURON).

For reimplementation, the contract of the intra-node transport is:

- **The `ncclTransports[2]` table and the `selectTransport` fallthrough** — idx 0 (`p2pTransport`) is tried first; on `canConnect → 0` the loop falls through to idx 1 (`netTransportNeuron`). There is no third entry to fall through to.
- **The `p2pTransport` vtable** — `canConnect` + a `send`/`recv` pair of `{setup, connect, free, proxy=NULL, allocRequest=NULL, freeRequest=NULL}`; reproduce the proxy-free shape, not just the function pointers.
- **The three P2P wire modes** — direct-pointer (same process), IPC (cross-process), and indirect/relay (intermediate rank), selected by `pidHash` match and `intermediateRank`. On Trainium the IPC mode degenerates to the direct mode because the handle ops are stubs.
- **The connect → map → wire chain** — `p2pSendSetup`/`p2pRecvSetup` allocate the FIFO and exchange an 80-byte `p2pConnectInfo`; `p2pMap` resolves the peer pointer; `p2pSendConnect`/`p2pRecvConnect` wire `ncclConnInfo.buffs[]`/`tail`/`head`/`ptrExchange` from the remote `ncclRecvMem`/`ncclSendMem`. POST/POLL is then on-device, host-silent.

| | |
|---|---|
| **Transport table** | `ncclTransports[2]` — `.bss` @`0xb13c0`; populated by `_GLOBAL__sub_I_transport.cc` @`0x202b0` |
| **This transport** | `ncclTransports[0]` = `p2pTransport` — `.data.rel.ro` @`0xac000` (file `0xab000`); `name` = `"P2P"` |
| **Sibling (boundary)** | `ncclTransports[1]` = `netTransportNeuron` @`0xac520`; `name` = `"NET_NEURON"` → [transport-efa](transport-efa.md) |
| **SHM transport** | **Absent** — no `shm*` transport symbols; `/dev/shm` @`0x8b873` is bootstrap only |
| **canConnect** | `p2pCanConnect` @`0x4a1f0` |
| **send vtable** | setup `p2pSendSetup` @`0x4ab60` · connect `p2pSendConnect` @`0x4b4c0` · free `p2pSendFree` @`0x4a680` · proxy/alloc/free **NULL** |
| **recv vtable** | setup `p2pRecvSetup` @`0x4b0a0` · connect `p2pRecvConnect` @`0x4b7b0` · free `p2pRecvFree` @`0x4a7c0` · proxy/alloc/free **NULL** |
| **Map** | `p2pMap` @`0x4a900` (static) |
| **Framework drivers** | `ncclTransportP2pConnect` @`0x3e9c0` · `ncclTransportP2pSetup` @`0x3eb80` (inlines `selectTransport`) |
| **Reachability oracle** | `neuronDeviceCanAccessPeer` @`0x454a0` (the only **real** RT call); `ncclTopoCheckP2p` @`0x63f00` |
| **Exchange unit** | `p2pConnectInfo` — **80 B**; carried in `ncclConnect.data[128]` |
| **FIFO descriptor** | `ncclConnInfo` — `buffs[3]`/`tail`/`head`/`ptrExchange`/`sizesFifo`/`ptrsFifo` (the post/poll surface) |
| **RT stubs (no-op)** | `neuronIpc{Get,Open,Close}MemHandle`, `neuronDeviceEnablePeerAccess`, `neuronMemcpy` → all `xor eax,eax; ret` |

> **QUIRK — the third NCCL transport is gone.** A reimplementer porting NCCL's transport layer will reach for `shmTransport`, the `/dev/shm`-staged host bounce buffer that upstream uses for same-host, cross-process ranks that cannot do GPU P2P. It does not exist here: `nm -C libnccom.so | rg -i shm` returns no transport symbols, and the only `/dev/shm` reference (@`0x8b873`) is consumed by `initTransportsRank` (@`0x28ed0`) for the bootstrap rendezvous socket, not for data. On Trainium the device address space is unified, so the SHM bounce is unnecessary — every same-host edge that upstream would route through SHM is handled by `p2pTransport`'s direct-pointer or IPC mode. A port that keeps the three-entry table will index a NULL/garbage `ncclTransports[2]` whenever `p2pCanConnect` fails and the old code expected SHM next; here the fallthrough from idx 0 lands directly on idx 1 (NET_NEURON).

---

## 1. The Transport Table and `selectTransport`

### Purpose

A collective ring crosses two kinds of edge — intra-node (same server) and inter-node (across EFA). The framework decides which transport carries each edge by walking a global vtable table. This section pins the table itself: how many entries, which entries, and how the per-peer selection loop falls through them. Everything else on the page is the contents of entry 0.

### Entry Point

```text
_GLOBAL__sub_I_transport.cc (0x202b0)            ── C++ static init, runs at dlopen
  └─ ncclTransports[0] = &p2pTransport   (0xac000, "P2P")        [this page]
  └─ ncclTransports[1] = &netTransportNeuron (0xac520, "NET_NEURON")  [transport-efa]
     (table lives in .bss @0xb13c0; two 8-byte pointer slots)

ncclTransportP2pSetup (0x3eb80)                  ── per-peer connect driver
  └─ selectTransport<send|recv> (inlined)        ── the fallthrough loop
       └─ t = &ncclTransports[(graph==NULL) & (myRank==peerRank)]   ── idx 0 preferred
       └─ t->canConnect(&ok, topo, graph, myInfo, peerInfo)   ── p2pCanConnect (0x4a1f0)
       └─ if !ok: ++t   ── fall through to ncclTransports[1] (NET_NEURON)
       └─ t->{recv,send}.setup(...)              ── p2pRecvSetup / p2pSendSetup
```

The two template instantiations `selectTransport<0>` and `selectTransport<1>` are present as symbols (the send/recv specializations), confirming the table is walked by index, not by name.

### The `ncclTransports[2]` table

| idx | Symbol | Addr | `name` string | Scope | Confidence |
|---|---|---|---|---|---|
| 0 | `p2pTransport` | `0xac000` (file `0xab000`) | `"P2P"` (bytes `50 32 50 00`) | **intra-node** — same server, NeuronCore↔NeuronCore | HIGH |
| 1 | `netTransportNeuron` | `0xac520` (file `0xab520`) | `"NET_NEURON"` | inter-node — across EFA/libfabric (→ [transport-efa](transport-efa.md)) | HIGH |
| — | ~~`shmTransport`~~ | — | — | **absent** in this fork (no `shm*` transport symbols) | HIGH |

> **GOTCHA — the `.data.rel.ro` address is `file_offset + 0x1000`.** The transport vtables live in `.data.rel.ro`, whose VMA is **not** equal to its file offset: `readelf -S` gives sh_addr `0xa7bf8` / sh_offset `0xa6bf8`, a constant `+0x1000` delta. So `p2pTransport` at VMA `0xac000` is read from file offset `0xab000` (`xxd -s 0xab000` shows `50 32 50 00` = "P2P"). Only `.text` (and `.rodata`) have VMA == file offset in this binary. A reimplementer cross-checking a vtable reloc or a name string with `xxd`/`objdump -s` on the raw file **must subtract `0x1000`** for any `.data`/`.data.rel.ro`-resident structure, or they will read 4 KB of the wrong section and conclude the layout is wrong.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `_GLOBAL__sub_I_transport.cc` | `0x202b0` | static ctor; populates `ncclTransports[2]` | HIGH |
| `ncclTransportP2pConnect` | `0x3e9c0` | mark per-channel connect intent (bitmasks) | HIGH |
| `ncclTransportP2pSetup` | `0x3eb80` | the `selectTransport` + exchange + connect driver | HIGH |
| `ncclTopoComputeP2pChannels` | `0x654f0` | P2P channel-count policy (clamps to Min/Max) | HIGH |
| `ncclParamMinP2pNChannels` | `0x654d0` | min P2P channels knob accessor | HIGH |
| `ncclParamMaxP2pNChannels` | `0x654e0` | max P2P channels knob accessor | HIGH |

### Considerations

The selection index `(graph == NULL) & (myRank == peerRank)` is the upstream NCCL trick that prefers P2P (`ncclTransports[0]`) for a connect-to-self or non-graph edge and otherwise starts at the same index 0 — the `& 1` only ever yields 0 or 1, and both branches land inside the 2-entry table. The fallthrough `++t` after a failed `canConnect` is bounded by the table size: with only two entries, P2P-fail means NET_NEURON, and NET_NEURON-fail is the terminal `"No transport found between devices %d and %d. Possible replica group misconfiguration"` error (@ `ncclTransportP2pSetup`). Upstream's three-entry table gave two fallthroughs (P2P → SHM → NET); this fork gives exactly one (P2P → NET).

---

## 2. The `p2pTransport` vtable

### Purpose

`p2pTransport` is a 120-byte `ncclTransport`: a 16-byte name, a `canConnect` function pointer, and two embedded 48-byte `ncclTransportComm` blocks (`send` and `recv`), each `{setup, connect, free, proxy, allocRequest, freeRequest}`. The defining trait of the **intra-node** transport is that the last three slots of each `ncclTransportComm` are NULL — it is proxy-free.

### Layout

The vtable relocations were read with `objdump -R` at the `0xac000` band (`R_X86_64_RELATIVE` entries; each names the absolute target):

| Field offset | Slot | Target symbol | Addr | Confidence |
|---|---|---|---|---|
| `+0x00` | `name[16]` | inline `"P2P\0"` | bytes `50 32 50 00` | HIGH |
| `+0x10` | `canConnect` | `p2pCanConnect` | `0x4a1f0` | HIGH |
| `+0x18` | `send.setup` | `p2pSendSetup` | `0x4ab60` | HIGH |
| `+0x20` | `send.connect` | `p2pSendConnect` | `0x4b4c0` | HIGH |
| `+0x28` | `send.free` | `p2pSendFree` | `0x4a680` | HIGH |
| `+0x30` | `send.proxy` | **NULL** (no reloc) | — | HIGH |
| `+0x38` | `send.allocRequest` | **NULL** (no reloc) | — | HIGH |
| `+0x40` | `send.freeRequest` | **NULL** (no reloc) | — | HIGH |
| `+0x48` | `recv.setup` | `p2pRecvSetup` | `0x4b0a0` | HIGH |
| `+0x50` | `recv.connect` | `p2pRecvConnect` | `0x4b7b0` | HIGH |
| `+0x58` | `recv.free` | `p2pRecvFree` | `0x4a7c0` | HIGH |
| `+0x60` | `recv.proxy` | **NULL** (no reloc) | — | HIGH |
| `+0x68` | `recv.allocRequest` | **NULL** (no reloc) | — | HIGH |
| `+0x70` | `recv.freeRequest` | **NULL** (no reloc) | — | HIGH |

The NULL slots are not inferred — they are the *absence* of a relocation. `objdump -R` lists relocs at `0xac010`, `…18`, `…20`, `…28`, `…48`, `…50`, `…58` and **nothing** at `…30/…38/…40/…60/…68/…70`. A `.bss`/`.data.rel.ro` qword with no reloc is a literal zero pointer.

> **QUIRK — proxy-free is the whole point.** Every NULL in the table above is a deliberate "no host work." Upstream NCCL's P2P transport is also largely proxy-free, but the structural contrast here is with the sibling: `netTransportNeuron` (idx 1) populates all six slots — its `proxy` is `netNeuronSendProxy` (@`0x4e510`) / `netNeuronRecvProxy` (@`0x4fe20`), and its `allocRequest`/`freeRequest` are `0x4bf60`/`0x4c6b0`. For NET, the host proxy thread DMAs operands into the engine and rings a doorbell (`nec_inc_semaphore`). For P2P, the same-host peer FIFO is reachable directly from the device, so there is nothing for the host to do after connect — the proxy slots are NULL by design. A reimplementer who wires a proxy callback into the P2P transport has misread the architecture: the intra-node data plane is on-device and host-silent.

### vtable struct shapes

```c
// ncclTransport — 120 B; p2pTransport @0xac000  [HIGH: DWARF + objdump -R]
struct ncclTransport {
    char              name[16];   // +0x00  "P2P\0"
    ncclResult_t    (*canConnect)(int*, ncclTopoSystem*, ncclTopoGraph*,
                                  ncclPeerInfo*, ncclPeerInfo*);   // +0x10
    ncclTransportComm send;       // +0x18  (48 B)
    ncclTransportComm recv;       // +0x48  (48 B)
};

// ncclTransportComm — 48 B  [HIGH]
struct ncclTransportComm {
    ncclResult_t (*setup)(...);        // +0x00
    ncclResult_t (*connect)(...);      // +0x08
    ncclResult_t (*free)(void*);       // +0x10
    ncclResult_t (*proxy)(...);        // +0x18  NULL for P2P
    ncclResult_t (*allocRequest)(...); // +0x20  NULL for P2P
    ncclResult_t (*freeRequest)(...);  // +0x28  NULL for P2P
};
```

---

## 3. The Three Wire Modes and the Connect Path

### Purpose

Given two same-host ranks selected onto P2P, the transport must choose *how* the local rank reaches the peer's FIFO buffer, set it up, exchange a connect blob, and wire the `ncclConnInfo` descriptor. There are three modes, chosen per peer pair; the connect path is the same control spine for all three, branching on `pidHash` equality and on whether topology returned an intermediate (relay) rank.

### The three modes

The three mode strings are verbatim in `.rodata` (the channel-emission print), and the branch that picks them is in `p2pSendSetup`/`p2pMap`:

| Mode | Selected when | Reach mechanism | `.rodata` string | Confidence |
|---|---|---|---|---|
| **direct pointer** | same process (`pidHash` match) | reuse the peer's raw `directPtr`; `conn.direct |= 1` if same device | `"… via P2P/direct pointer%s"` | HIGH |
| **IPC** | different process (`pidHash` mismatch) | `IpcGetMemHandle` / `IpcOpenMemHandle` — **no-op stubs on Trn**, so degenerates to direct | `"… via P2P/IPC%s"` | HIGH |
| **indirect / relay** | `intermediateRank != -1` (topology inserts a relay) | route through an intermediate rank's buffer; `info.rank = intermediateRank` | `"… via P2P/indirect/%d[%lx]%s"` | HIGH |

> **QUIRK — IPC mode is a formality on Trainium.** The cross-process branch exports an IPC handle (`ncclRtIpcGetMemHandle` → `neuronIpcGetMemHandle` @`0x441b0`) into `p2pConnectInfo.devIpc` and imports it on the far side (`ncclRtIpcOpenMemHandle` → `neuronIpcOpenMemHandle` @`0x441a0`). Both RT implementations are `xor eax,eax; ret` (byte-confirmed) — they touch neither the handle nor the pointer. The 64-byte `devIpc` field is still *packed and exchanged* (the connect blob is fixed-size), but `p2pMap`'s cross-process branch sets `*devMem` from the no-op `ncclRtIpcOpenMemHandle` out-param, which on Trainium leaves the import resolving to the same shared address space. The mode distinction survives in the code and the log string, but the actual reach is the unified device address space in every case. A reimplementer targeting a *non-unified* memory model must put real IPC logic behind these stubs; on Trainium they are intentionally empty.

### Algorithm — connect → map → wire

```c
// ncclTransportP2pSetup — 0x3eb80  [HIGH structure; relay branch MED]
// The driver: for each (recvPeer, sendPeer) round-robin, for each channel bit set,
// select the transport, setup, exchange, connect, then mirror the connector to device.
ncclResult_t ncclTransportP2pSetup(ncclComm *comm, ncclTopoGraph *graph):
    for each peer round-robin:
        // --- select (see §1) ---
        t = &ncclTransports[(graph==NULL) & (myRank==peerRank)];   // idx 0 = P2P
        t->canConnect(&ok, comm->topo, graph, myInfo, peerInfo);   // p2pCanConnect 0x4a1f0
        if (!ok) ++t;                                              // fall through to NET_NEURON
        // --- setup: allocate FIFO + pack connect blob ---
        t->recv.setup(comm, graph, myInfo, peerInfo, &recvData, recvConn, chId);  // p2pRecvSetup
        t->send.setup(comm, graph, myInfo, peerInfo, &sendData, sendConn, chId);  // p2pSendSetup
        // --- exchange the 128-byte ncclConnect blob over bootstrap ---
        bootstrapSend(comm->bootstrap, peer, /*type*/TRANSPORT_INIT,
                      data, n << 7, "p2pconnect");   // n<<7 = n*128 (stride = sizeof ncclConnect.data)
        bootstrapRecv(comm->bootstrap, peer, ... , "p2pconnect");
        // --- connect: wire ncclConnInfo from the remote send/recv mem ---
        t->send.connect(comm, sendData, 1, rank, sendConn);   // p2pSendConnect 0x4b4c0
        t->recv.connect(comm, recvData, 1, rank, recvConn);   // p2pRecvConnect 0x4b7b0
        // --- mirror the connector into device-visible memory ---
        if (connector->connected == 1)
            ncclRtMemcpy(channel->devPeers + off, connector, 0x80, /*kind*/1);  // neuronMemcpy = NO-OP
    // (string "No transport found between devices %d and %d…" on the terminal !ok)

// p2pSendSetup — 0x4ab60  (p2pRecvSetup 0x4b0a0 is symmetric)  [HIGH; arithmetic byte-anchored]
ncclResult_t p2pSendSetup(comm, graph, myInfo, peerInfo, connectInfo, connector, chId):
    resources = calloc(32);                       // -> connector->transportResources (p2pResources, 32B)
    ncclTopoCheckP2p(comm->topo, myInfo->busId, peerInfo->busId,
                     &p2p, &useRead, &intermediateRank);     // 0x63f00 — reachability + relay
    size = roundUpTo2MB(buffSizes);               // 'mov $0x200000' ; 'and $0xffe00000' @0x4abe6/0x4adcf
    if (intermediateRank != -1):
        info.rank = intermediateRank;             // RELAY: share the intermediate's buffer
    else:
        ncclCudaCalloc(&info.directPtr, size);    // 0x4aa80 -> ncclRtMalloc + memset: allocate local FIFO
    if (myInfo->pidHash == peerInfo->pidHash):    // SAME PROCESS
        if (!useRead) conn.direct |= 1;           // direct-pointer mode; bit0 of ncclConnInfo.direct
    else:                                          // CROSS PROCESS
        ncclRtIpcGetMemHandle(&info.devIpc, info.directPtr);   // -> neuronIpcGetMemHandle: NO-OP
    resources[+0x10] = -1;                         // intermediate sentinel (p2pSendFree asserts ==-1)
    resources[+0x14] = info.rank;
    resources[+0x18] = comm->bootstrap;
    p2pMap(myInfo, &comm->peerInfo[info.rank], &info, &resources[0], &resources[1]);  // 0x4a900
    pack(connectInfo->data, &info, /*80 bytes via 5× 16B loads*/);   // p2pConnectInfo -> ncclConnect.data

// p2pMap — 0x4a900  [HIGH]  resolve the peer device pointer for this mode
void p2pMap(myInfo, peerInfo, p2pInfo, void **devMem, void **ipcPtr):
    if (myInfo->pidHash == peerInfo->pidHash):    // same process
        if (myDev != peerDev)
            ncclRtDeviceEnablePeerAccess(peerDev, 0);   // -> neuronDeviceEnablePeerAccess: NO-OP
                                                        // (rc 704 "already enabled" ignored upstream)
        *devMem = p2pInfo->directPtr;             // RAW peer pointer reuse — the unified-AS shortcut
        *ipcPtr = NULL;
    else:                                          // cross process
        ncclRtIpcOpenMemHandle(devMem, p2pInfo->devIpc, 1);  // -> neuronIpcOpenMemHandle: NO-OP
        *ipcPtr = *devMem;

// p2pSendConnect — 0x4b4c0  (p2pRecvConnect 0x4b7b0 symmetric)  [HIGH addr; field wiring MED]
ncclResult_t p2pSendConnect(comm, connectInfo, n, rank, connector):
    assert(connector->connected == 0);            // p2p.cc guard
    if (cross_process) reopen IPC handle from connectInfo->data[+16];   // (no-op on Trn)
    // wire the FIFO descriptor from the REMOTE ncclRecvMem (send side reads peer recvMem):
    conn.buffs[0] = recvMem->buff;                              // lo-proto buffer
    conn.buffs[1] = recvMem->buff + buffSizes[0];              // mid buffer
    conn.buffs[2] = useRead ? localFifo + 4096                  // simple/LL buffer (read-vs-write branch)
                            : recvMem->buff + sz01;
    conn.tail        = &recvMem->tail;            // producer cursor (peer's recvMem header +0x00)
    conn.head        = &localFifo->head;          // consumer cursor (local sendMem header +0x00)
    conn.ptrExchange = (void**)(localFifo + 128); // ncclSendMem.ptrExchange (+0x80)
    connector->connected = 1;
```

> **NOTE — the connector mirror is a no-op copy on Trainium.** After connect, `ncclTransportP2pSetup` calls `ncclRtMemcpy(channel->devPeers, connector, 0x80, 1)` to push the connector into device-visible memory. That call dispatches through the runtime vtable to `neuronMemcpy` (@`0x44210`), which is `xor eax,eax; ret`. The mirror is therefore a structural relic from upstream (where `devPeers` is a separate CUDA allocation needing an explicit `cudaMemcpy`); on Trainium the device already sees host memory in the unified space, so the copy is elided to a no-op. A reimplementer on a non-unified target must restore a real device copy here, or the device kernel will read a stale/zero `devPeers` and poll the wrong FIFO.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `p2pCanConnect` | `0x4a1f0` | reachability gate (calls `ncclTopoCheckP2p`/`Pds`/`CanAccessPeer`) | HIGH |
| `p2pSendSetup` | `0x4ab60` | alloc FIFO, run topo check, pack `p2pConnectInfo` (send) | HIGH |
| `p2pRecvSetup` | `0x4b0a0` | symmetric setup (recv) | HIGH |
| `p2pSendConnect` | `0x4b4c0` | wire `ncclConnInfo` from peer `ncclRecvMem` (send) | HIGH addr / MED wiring |
| `p2pRecvConnect` | `0x4b7b0` | wire `ncclConnInfo` from peer `ncclSendMem` (recv) | HIGH addr / MED wiring |
| `p2pMap` | `0x4a900` | resolve peer `devMem` per mode (direct vs IPC) | HIGH |
| `p2pSendFree` | `0x4a680` | teardown: close IPC (no-op), free FIFO, assert sentinel | HIGH |
| `p2pRecvFree` | `0x4a7c0` | symmetric teardown | HIGH |
| `ncclCudaCalloc<char>` | `0x4aa80` | `ncclRtMalloc` + memset; allocates the FIFO | HIGH |

---

## 4. The Connect Blob and the FIFO Descriptor

### Purpose

Two structs cross the wire and pin the steady-state surface. `p2pConnectInfo` is the 80-byte payload bootstrap-exchanged at connect time; `ncclConnInfo` is the FIFO descriptor the device kernels read/write forever after. A reimplementer must reproduce both layouts exactly — the connect blob because it is serialized between ranks, and the descriptor because it is the contract between this host transport and the on-device collective kernels.

### `p2pConnectInfo` — the exchange unit (80 B)

DWARF-confirmed (`gdb ptype` + offset probes): size 80, `directPtr` @`+0x08`, `devIpc` @`+0x10`.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `rank` | `+0x00` | `int` | peer or intermediate (relay) rank | HIGH |
| `read` | `+0x04` | `int` | use the CE-read protocol (`useRead`) | HIGH |
| `directPtr` | `+0x08` | `void*` | raw device FIFO pointer — cross-rank-usable on Trn | HIGH |
| `devIpc` | `+0x10` | `cudaIpcMemHandle_t` | 64-byte IPC handle; packed but no-op on Trn | HIGH |

It is copied verbatim into `ncclConnect.data[128]` (the bootstrap exchange unit) with five 16-byte loads; the `n << 7` stride in `ncclTransportP2pSetup` is `n * 128` — one `ncclConnect.data` slot per connection.

### `ncclConnInfo` — the post/poll surface

DWARF-confirmed field order and the key offsets (`tail` @`+0x18`, `head` @`+0x20`, `direct` @`+0x28`, `ptrExchange` @`+0x30`):

| Field | Offset | Type | Wired from | Meaning | Confidence |
|---|---|---|---|---|---|
| `buffs[3]` | `+0x00` | `char*[3]` | remote `ncclRecvMem`/`SendMem` buff | proto buffers (lo / mid / ll-simple) | HIGH |
| `tail` | `+0x18` | `uint64_t*` | peer `ncclRecvMem.tail` | producer cursor | HIGH |
| `head` | `+0x20` | `uint64_t*` | local/peer `ncclSendMem.head` | consumer cursor | HIGH |
| `direct` | `+0x28` | `int` | bit0 set in same-device direct mode | direct-pointer flag | HIGH |
| `shared` | `+0x2c` | `int` | — | shared-buffer flag | MED |
| `ptrExchange` | `+0x30` | `void**` | `ncclSendMem.ptrExchange` (+0x80) | pointer-exchange slot | HIGH |
| `sizesFifo` | `+0x38` | `int*` | `ncclRecvMem.sizesFifo[8]` (+0x80) | per-step size FIFO | HIGH |
| `ptrsFifo` | `+0x40` | `void**` | `ncclRecvMem.ptrsFifo[8]` (+0xA0) | per-step ptr FIFO | HIGH |
| `step` | `+0x48` | `uint64_t` | — | producer/consumer step counter | HIGH |
| `llLastCleaning` | `+0x50` | `uint64_t` | — | LL-protocol cleanup watermark | HIGH |

The `ncclSendMem` / `ncclRecvMem` are page-aligned, 4096-byte header then buffer: `ncclSendMem` = `{head @+0x00, ptrExchange @+0x80, buff @+0x1000}`; `ncclRecvMem` = `{tail @+0x00, sizesFifo[8] @+0x80, ptrsFifo[8] @+0xA0, buff @+0x1000}`.

### POST / POLL — host-silent

```text
Once connected, the steady state is entirely on-device:

  producer kernel:  write conn.buffs[proto] in peer (mapped) device memory
                    advance *conn.tail        ── peer ncclRecvMem.tail
  consumer kernel:  spin on *conn.tail vs local step
                    read conn.buffs[proto]
                    advance *conn.head        ── local/peer ncclSendMem.head

  HOST: nothing.  p2pTransport.{send,recv}.proxy == NULL.
        No proxy thread, no doorbell, no nec_inc_semaphore for P2P.

  (Contrast NET_NEURON: netNeuronSendProxy 0x4e510 + nec_inc_semaphore doorbell,
   polled by pollPosted 0x4d300 / netNeuronRecvProxy 0x4fe20 — see transport-efa.)
```

> **GOTCHA — the device kernel mechanics are inferred, not in this `.so`.** The `buffs[]`/`tail`/`head` wiring above is byte-confirmed from `p2pSendConnect`/`p2pRecvConnect`, but the *consumer* of that wiring — the device-side collective kernel that spins on `tail`, reads `buffs[]`, and advances `head` — is **not code in `libnccom.so`** (this library has zero device primitives; see [overview](overview.md)). The simple/LL protocol semantics over `buffs[0..2]`, `sizesFifo[8]`, `ptrsFifo[8]`, and `step`/`llLastCleaning` are mapped from upstream NCCL's protocol and the field names, **MED confidence**, pending the separate device-kernel cell. A reimplementer should treat §4's descriptor as the *interface* and reconstruct the protocol from the upstream simple/LL kernel, not invent one.

### Teardown

```c
// p2pSendFree — 0x4a680  (p2pRecvFree 0x4a7c0 symmetric)  [HIGH]
ncclResult_t p2pSendFree(void *resources):
    if (resources[+0x08] /* ipcPtr */)
        ncclRtIpcCloseMemHandle(resources[+0x08]);   // -> neuronIpcCloseMemHandle: NO-OP
    assert(resources[+0x10] == -1);                  // intermediate sentinel guard
    ncclRtFree(resources[+0x00]);                    // free the FIFO (neuronFree 0x444a0)
    free(resources);
```

---

## 5. Reachability — Who Can P2P

### Purpose

`p2pCanConnect` (@`0x4a1f0`) is the gate that decides whether an edge stays on P2P or falls through to NET_NEURON. It is the one part of the transport that does real work on Trainium, because the IPC/peer-access stubs are empty: the *decision* lives in topology and in `neuronDeviceCanAccessPeer`, not in the connect path. This section pins the gate; the topology graph it reads is owned by [topology](topology.md).

### Algorithm

```c
// p2pCanConnect — 0x4a1f0  [HIGH addr; predicate composition MED]
ncclResult_t p2pCanConnect(int *ok, ncclTopoSystem *topo, ncclTopoGraph *graph,
                           ncclPeerInfo *info1, ncclPeerInfo *info2):
    *ok = 0;
    if (ncclParamP2pDisable()) return ncclSuccess;          // NCCL_P2P_DISABLE
    ncclTopoCheckP2p(topo, info1->busId, info2->busId,
                     &p2p, &useRead, &intermediateRank);     // 0x63f00 (NCCL_P2P_LEVEL gate)
    if (!p2p) return ncclSuccess;                            // topology says no -> fall to NET
    // pod / same-CC / MLA-locality refinements:
    ncclTopoCheckPdsP2p(info1, info2, &pds);                 // 0x64410 (Trn3SwitchV1 pod path)
    ncclTopoCheckSameIntraCC(info1, info2, &sameCC);         // 0x64270
    ncclRtDeviceCanAccessPeer(&canAccess, dev1, dev2);       // -> neuronDeviceCanAccessPeer 0x454a0 (REAL)
    *ok = p2p && canAccess;                                  // (string "Could not enable P2P …" on fail)
    return ncclSuccess;
```

`neuronDeviceCanAccessPeer` (@`0x454a0`) is the only function in this chain that is not a stub: it calls `nrt_get_instance_info`, `nec_get_virtual_core_size`, and `nec_get_peer_mla_idx` (all `@NRT_2.0.0`), and branches on instance family (TRN1/2/3/Trn3SwitchV1) to compute MLA/pod locality. That is the true intra-node reachability oracle.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `p2pCanConnect` | `0x4a1f0` | the P2P reachability gate | HIGH |
| `ncclTopoCheckP2p` | `0x63f00` | topology P2P check; `NCCL_P2P_LEVEL`, relay detection | HIGH |
| `ncclTopoCheckPdsP2p` | `0x64410` | Trn3SwitchV1 pod reachability | HIGH |
| `ncclTopoCheckSameIntraCC` | `0x64270` | same intra-CC predicate | HIGH |
| `neuronDeviceCanAccessPeer` | `0x454a0` | **real** RT oracle: MLA/pod locality via libnrt | HIGH |
| `neuronGetP2pPodPeerNode` | `0x49bc0` | pod peer-node resolution (`nec_get_p2p_pod_peer_node`) | HIGH |
| `ncclParamP2pReadEnable` | `0x4ba90` | `useRead` (CE-read) protocol knob | HIGH |
| `ncclRtFree` | `0x434e0` | FIFO free (→ `neuronFree`) | HIGH |
| `ncclRtMemcpy` | `0x438a0` | connector→`devPeers` mirror (→ `neuronMemcpy`, no-op) | HIGH |

### Considerations

The runtime-wrapper layer (`ncclRt*`) is a thin vtable dispatch into `ncclRtNeuron` (`.data` @`0xaba20`, installed by `ncclInitRuntime` @`0x20460`): `ncclRtIpcGetMemHandle` (@`0x43300`) → slot `+0x88`, `ncclRtIpcOpenMemHandle` (@`0x43230`) → `+0x80`, `ncclRtDeviceEnablePeerAccess` (@`0x43050`) → `+0x68`, `ncclRtMalloc` (@`0x43440`) → `+0x98`, `ncclRtFree` (@`0x434e0`) → `+0xa0`, `ncclRtMemcpy` (@`0x438a0`) → `+0xd0`. The concrete targets behind those slots are the no-op stubs catalogued in the [Abstract](#abstract), except `deviceCanAccessPeer` (`+0x60` → `neuronDeviceCanAccessPeer`) and `malloc`/`free` (which back the FIFO). The reimplementation tell: every P2P operation that *moves bytes or exports a handle* is a stub, and every operation that *decides reachability or allocates* is real. The transport is, on Trainium, almost entirely a topology-driven pointer-sharing exercise.

---

## Related Components

| Name | Relationship |
|---|---|
| `netTransportNeuron` (`ncclTransports[1]`, `0xac520`) | the sibling transport; P2P falls through to it on `canConnect → 0`; proxy/doorbell path |
| `_GLOBAL__sub_I_transport.cc` (`0x202b0`) | static ctor that builds the 2-entry table both transports live in |
| `ncclTopoCheckP2p` / `Pds` / `SameIntraCC` | the topology gates `p2pCanConnect` composes (owned by [topology](topology.md)) |
| `neuronDeviceCanAccessPeer` (`0x454a0`) | the one real RT call; MLA/pod reachability via `@NRT_2.0.0` callbacks |
| `ncclRtNeuron` vtable (`0xaba20`) | the runtime dispatch whose P2P-relevant slots are the no-op stubs |

## Cross-References

- [Inter-Node Transport: EFA / libfabric](transport-efa.md) — `ncclTransports[1]` = `netTransportNeuron`; the proxy/doorbell path P2P falls through to
- [The Net Plugin (ncclNet_v6)](net-plugin.md) — the libfabric/OFI plugin behind the NET_NEURON transport
- [Topology Detection and Graph Build](topology.md) — `ncclTopoCheckP2p` and the edge-locality classes (`EdgeSharedMemory`/`EdgeD2D`/`EdgeRMTV`/`EdgeLocal`) that `p2pCanConnect` reads
- [Send / Recv Primitives and Protocols](send-recv-prims.md) — the FIFO/slot/protocol detail that the on-device side of §4's `ncclConnInfo` implements
- [Overview: the NCCL Fork (2.31.24+nrt2.0)](overview.md) — the Part-XII map; the two-transport table and the no-device-kernel fact
- [back to index](../index.md)
