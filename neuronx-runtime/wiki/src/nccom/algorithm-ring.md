# Ring Algorithm Construction (Host-Side)

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text` VMA == file offset for the cited band, so every `0x…` is both a file offset and an analysis VMA.*
> *Evidence grade: **Confirmed (byte-anchored)** — every symbol, struct offset, and the two ring-emission functions (`ncclBuildRings`, `ncclTopoGetNetDev`) were re-read from the binary's own DWARF and disassembly (`nm -C` / `objdump -d` / `gdb ptype /o`). The `ncclTopoPreset`/`Postset` per-rank assembly is read from decompiled control flow plus byte-confirmed struct offsets and is tagged MED where the exact arithmetic is inferred. Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

This page documents the **host-side ring wiring** — the back half of `buildGraphTransportsRank` (@`0x24ef0`) that turns a searched `ncclTopoGraph` into a per-rank ring ordering and connects only ring-adjacent peers over a transport. If you already hold a mental model of upstream NVIDIA NCCL's `graph/connect.cc` and the tail of `graph/topo.cc` — `ncclTopoPreset` packs each rank's local ring head/tail/prev/next into an `ncclTopoRanks`, a bootstrap AllGather broadcasts every rank's fragment to every other rank, `ncclTopoPostset` stitches the fragments into a global ordering, `ncclBuildRings` walks the `next[]` array to materialize each ring's `userRanks` traversal order, and `ncclTransportP2pSetup` connects each rank only to its ring neighbours — then you already know the inherited skeleton of this page. Those functions are present verbatim, operating on the same `ncclRing` (24 B), `ncclChannel` (512 B), `ncclTopoRanks` (960 B) and `ncclTopoGraph` (33080 B) structs the inherited topology code ([topology](topology.md)) produced.

The **single most important fact for a reimplementer** is the boundary: libnccom builds the ring *wiring* and nothing else. The only algorithm constructed is RING — `ncclTopoGraph.pattern == 4` (`NCCL_TOPO_PATTERN_RING`), a single `ncclTopoCompute` pass (`buildGraphTransportsRank+0x89` @`0x24f79`, exactly one call site), no tree builder reached (`ncclGetBtree`/`ncclGetDtree` are present but dead), and **no cost model anywhere** — `nm -C | rg 'TuneModel|getAlgoInfo|computeColl|EnqueueCheck'` is empty, and the `buildGraphTransportsRank` call list runs TopoCompute → Preset → AllGather → Postset → P2pSetup → ComputeP2pChannels → ProxyCreate with no `ncclTopoTuneModel` between them. The device-side **ring step loop** — the actual `prev → self → next` traversal, the reduce-op application, and the LL/LL128/Simple primitive selection — is **not in this binary**: a symbol sweep for `runRing*`/`reduceCopy*`/`ncclAllReduce`/`ncclAllGather`/`ncclReduceScatter`/`ncclBroadcast` returns zero. libnccom writes `comm->channels[].ring.{prev,next,userRanks,devUserRanks}` and the fork-added `comm->kangaRing[4][64]`; the NeuronCore device program consumes them and runs the schedule. That device step loop is owned by [collectives/ring-scheduling](../collectives/ring-scheduling.md) and is **not** re-derived here.

The fork delta is **`kangaRing`**: a TRN3-SwitchV1 multi-rail ring variant. The Neuron topology engine ([topology](topology.md) §3) produces a base set of Hamiltonian device rings via `topology::getPaths` (@`0x5e400`) → `findPathRec` (@`0x5c4e0`) and rotates one ring per NIC lane (≤ `MAX_KANGA_PATHS = 4`); `ncclTopoPreset`/`Postset` then **materialize** those rotated rings into the fork-added `comm->nKangaRingChannels` / `comm->kangaRing[4][64]` fields and the fork-added `ncclTopoRanks.JbogKangaringPath[4][4]`, via SIMD copy blocks gated on `neuronIsTrn3SwitchV1Family()`. The page is organized as the pipeline in execution order with the fixed H3 vocabulary (Purpose / Entry Point / Algorithm / Function Map / Considerations): (1) the **build driver** and the single-RING fact; (2) **per-rank ring assembly** (`ncclTopoPreset` head/tail/prev/next); (3) **global ring stitch and ordering** (`ncclTopoPostset` → `ncclBuildRings`); (4) the **`kangaRing[4][64]` / `crossracktorack` build**; (5) **ring connect** — only RING peers are P2P-connected, and the per-channel NIC binding.

For reimplementation, the contract of the ring layer is:

- **Build wiring, not a kernel.** The deliverable is `comm->channels[ch].ring` (`prev`/`next`/`userRanks`) per channel plus `comm->kangaRing[][]`; the step loop, reduce, and protocol are the device program's job. A reimplementer who ports a ring *kernel* into this library has ported the wrong half.
- **The per-rank → global pivot.** `ncclTopoPreset` writes *this* rank's local ring fragment (head/tail/prev/next) into `ncclTopoRanks`; a bootstrap AllGather makes every rank hold every rank's fragment; `ncclTopoPostset` reassembles the global ring and `ncclBuildRings` walks `next[]` to produce the device traversal order. Reproduce both the fragment packing and the stitch.
- **Single algorithm, hard channel bounds.** Only RING (`pattern=4`) is built; channel count is doubled (`×2`) then clamped to **32** (`ncclMaxNchannels()` @`0x6c480` returns `0x20`; `ncclMinNchannels()` @`0x6c470` returns 0). No tree, no cost model, no protocol choice here.
- **kangaRing is a copy, not a search.** The fork-added fields are *materialized* from already-computed `topology::getPaths` rotations by fixed `__m128i` copy unrolls (≤ 4 channels), gated on `neuronIsTrn3SwitchV1Family()`. The search that produced them is [topology](topology.md)'s; this page only stores them.

| | |
|---|---|
| **Build driver** | `buildGraphTransportsRank` @`0x24ef0` — single RING pass; `ncclTopoCompute` call @`0x24f79` (1×) |
| **Algorithm built** | **RING only** — `ncclTopoGraph.pattern = 4` (`NCCL_TOPO_PATTERN_RING`); tree/collnet dead |
| **Per-rank assembly** | `ncclTopoPreset` @`0x6c1f0` — head/tail/prev/next → `ncclTopoRanks` (960 B) |
| **Global stitch** | `ncclTopoPostset` @`0x6c490` → `ncclBuildRings` @`0x6d570` (call @`0x6c8cf`) |
| **Ring order generator** | `ncclBuildRings` @`0x6d570` — walk `next[]` for `nranks` steps; loop-back + membership validate |
| **Ring connect** | `ncclTransportP2pSetup` @`0x3eb80` / `ncclTransportP2pConnect` @`0x3e9c0` — only ring peers |
| **Per-channel NIC** | `ncclTopoGetNetDev` @`0x6bf30` — `inter[2*(ch%nCh)+(intra[(ch%nCh)*count]!=rank)]` |
| **Channel-count floor/ceiling** | `ncclMinNchannels()`→**0** @`0x6c470` · `ncclMaxNchannels()`→**32** @`0x6c480`; Postset `×2`, clamp 32 |
| **kangaRing fields (fork)** | `comm->nKangaRingChannels` @+`16384` · `comm->kangaRing[4][64]` @+`16388` (1024 B) |
| **kangaRing wiring (fork)** | `ncclTopoRanks.JbogKangaringPath[4][4]` @+`896`; `ncclTopoGraph.crossracktorack` @+`33076` |
| **kangaRing gate** | `neuronIsTrn3SwitchV1Family()`; `nKangaRingChannels ≤ MAX_KANGA_PATHS = 4` |
| **Device step loop** | **NOT here** — `runRing*`/`reduceCopy*`/`ncclAllReduce…` symbol sweep empty; owned by [ring-scheduling](../collectives/ring-scheduling.md) |

> **QUIRK — the ring step loop is in libnrt, not here.** A reimplementer porting NCCL's ring will look for `runRing` / the `prims` send-recv-reduce primitives / `reduceCopy` — the device functions that walk `userRanks` and sum the chunks. They do **not** exist in `libnccom.so`: `nm -C | rg 'runRing|reduceCopy|::prims|ncclAllReduce|ncclAllGather|ncclReduceScatter|ncclBroadcast'` returns nothing, and there is no `cudaLaunchKernel` call site (there is no GPU). This library builds only the **wiring** — `comm->channels[ch].ring.{prev,next,userRanks,devUserRanks}` and `comm->kangaRing[][]` — and `neuronDevCommSetup` (in libnrt's `rt_neuron`) consumes that already-decided ring to build the device communicator. The step traversal, the reduce-op application, and the LL/LL128/Simple protocol selection all execute in the NeuronCore device program and are documented on **[collectives/ring-scheduling](../collectives/ring-scheduling.md)**. Treat this page's output as the *input* to that one; do not look here for the loop body.

---

## 1. The Build Driver — One RING Pass, No Cost Model

### Purpose

`buildGraphTransportsRank` (@`0x24ef0`) is the master orchestrator of the graph/ring stage: it computes one topology graph, presets this rank's ring fragment, AllGathers fragments across ranks, postsets them into global rings, decides the P2P channel count, and connects the transports. It is the single point a reimplementer must reproduce to understand *which* algorithm is built and *what is not built* — the answer to both is decided by the linear call list, not by any runtime cost model.

### Entry Point

```text
buildGraphTransportsRank(comm, nNodes)   @0x24ef0   [master orchestrator]
  ├─ ncclTopoCompute(comm, &ringGraph, nNodes)     @0x24f79  ── ONE call; ringGraph.pattern = 4 (RING)
  │     [Neuron path] neuronTopoGetGraph → topology::getPaths → ncclXml → ncclTopoGetGraphFromXml
  │     [fallback]    ncclTopoSearchRec bandwidth search  ("…falling back to simple order" @0x92b78)
  ├─ ncclTopoPrintGraph(sys, &ringGraph)           @0x24fea  ── debug "Pattern %d, crossNic…" @0x92bc0
  ├─ ncclTopoPreset(comm, &ringGraph, &topoRanks)  @0x25102  ── per-rank head/tail/prev/next  (§2)
  ├─ bootstrapAllGather(… topoRanks …)             @0x251bb  ── exchange ncclTopoRanks, all ranks
  ├─ ncclTopoPostset(comm, nodesFirstRank, allTopoRanks,…) @0x25703  ── global stitch  (§3)
  │     └─ ncclBuildRings(nrings, userRanks, rank, nRanks, prev, next)  @0x6c8cf → 0x6d570  (§3)
  ├─ ncclTransportP2pConnect(comm, ch, …)          @0x25aec  ── connect ring neighbours  (§5)
  ├─ ncclTransportP2pSetup(comm, &ringGraph)       @0x25b26  ── p2pCanConnect / netNeuronCanConnect
  ├─ ncclTopoComputeP2pChannels(comm)              @0x25d95  ── p2p channel count (pow2 clamp ≤ 32)
  └─ ncclProxyCreate(comm)                         @0x25db5  ── hand the wired channels to the proxy
  *** NO ncclTopoTuneModel / getAlgoInfo / computeColl between any of these ***
```

### Algorithm

The driver is a straight line: build one graph, wire it per-rank, exchange, stitch, connect. There is no tuning loop *at this level* — the relaxation ladder is inside `ncclTopoCompute` ([topology](topology.md) §2) and selects channel *quality*, not algorithm. Algorithm is fixed at RING by the single `ncclTopoCompute` call whose `ringGraph.pattern` is `4`.

```c
// buildGraphTransportsRank — 0x24ef0  [HIGH: call list read from disasm 0x24f79..0x25db5]
ncclResult_t buildGraphTransportsRank(ncclComm *comm, int nNodes):
    ncclTopoGraph ringGraph;
    ringGraph.id = 0;
    ringGraph.pattern = NCCL_TOPO_PATTERN_RING;      // 4 — the ONLY pattern built
    ringGraph.crossNic = 2;                           // template; resolved 0/1 in TopoCompute
    ringGraph.minChannels = 1; ringGraph.maxChannels = 16;   // template; comm cap 32 (ncclMaxNchannels)
    ncclTopoCompute(comm, &ringGraph, nNodes);        // 0x24f79 — ONE call (Neuron getPaths or DFS)
    ncclTopoPrintGraph(comm->topo, &ringGraph);       // 0x24fea — debug only

    ncclTopoRanks topoRanks[1];                        // this rank's fragment (960 B)
    ncclTopoPreset(comm, &ringGraph, topoRanks);       // 0x25102 — §2

    ncclTopoRanks *allTopoRanks[nRanks];
    bootstrapAllGather(comm->bootstrap, allTopoRanks, sizeof(ncclTopoRanks));  // 0x251bb
                                                       // every rank now holds every rank's fragment

    int nChannels, nNodesOut;
    ncclTopoPostset(comm, nodesFirstRank, allTopoRanks, &nChannels, &nNodesOut);  // 0x25703 — §3/§4
                                                       // → ncclBuildRings (0x6c8cf), kangaRing copy
    ncclTransportP2pSetup(comm, &ringGraph);           // 0x25b26 — connect ONLY ring peers (§5)
    ncclTopoComputeP2pChannels(comm);                  // 0x25d95 — p2pnChannels pow2 clamp ≤ 32
    ncclProxyCreate(comm);                             // 0x25db5 — hand channels to the proxy engine
    return ncclSuccess;
    //  *** no TuneModel / getAlgoInfo: protocol & cost live in the NRT plan, not here ***
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `buildGraphTransportsRank` | `0x24ef0` | master: compute → preset → AllGather → postset → connect → channels | HIGH |
| `ncclTopoCompute` | `0x6ab30` | graph search; single RING pass (Neuron `getPaths` or inherited DFS) | HIGH |
| `ncclTopoPrintGraph` | `0x6b9f0` | debug emit `"Pattern %d, crossNic…"` (@`0x92bc0`, lea @`0x6ba16`) | HIGH |
| `ncclTopoComputeP2pChannels` | `0x654f0` | p2p channel count: pow2 clamp ≤ 32, bit-reverse map | HIGH |
| `ncclMinNchannels` / `ncclMaxNchannels` | `0x6c470` / `0x6c480` | hard channel floor **0** / ceiling **32** (`mov $0x20`) | HIGH |
| `ncclGetBtree` / `ncclGetDtree` | `0x6d7d0` / `0x6d900` | binary/double-tree builders — **DEAD** (no call-site xref) | HIGH |

### Considerations

The absence of a cost model is the central fact of this part: in upstream NCCL `buildGraphTransportsRank`'s sibling `ncclTopoTuneModel` builds per-algorithm/per-protocol latency tables that `getAlgoInfo` later reads to pick AllReduce-Ring-LL128 vs AllReduce-Tree-Simple at launch. Here `nm -C | rg 'TuneModel|getAlgoInfo|computeColl|EnqueueCheck'` is **empty**, and the driver's call list (byte-confirmed `0x24f79`…`0x25db5`) never reaches a tuning function. The `"LL"`/`"LL128"`/`"Simple"`/`"Tree"`/`"Ring"` literals at `0x8be50` are an **index→name debug table** consumed only by `ncclTopoPrintGraph`; they are *not* a protocol selector. Protocol (LL/LL128/Simple) is chosen by the Neuron compiler and encoded in the NRT execution plan, not decided in this library — the cell that owns that decision is [tuning](tuning.md), which confirms the stub on this exact image. The tree path is built into the binary (`ncclGetBtree` @`0x6d7d0`, `ncclGetDtree` @`0x6d900`) but has no call site; it is documented as dead code on [algorithm-tree](algorithm-tree.md).

---

## 2. Per-Rank Ring Assembly — `ncclTopoPreset`

### Purpose

`ncclTopoPreset` (@`0x6c1f0`) is the first half of the per-rank → global pivot: it reads *this* rank's position in each channel's local rank order (`ringGraph->intra`), and packs four facts per channel into a 960-byte `ncclTopoRanks` — the ring **head** (`ringRecv`, the rank that receives from the network), the ring **tail** (`ringSend`, the rank that sends to the network), and this rank's local **predecessor** and **successor** (`ringPrev`/`ringNext`). That fragment is what the AllGather broadcasts so every rank can reassemble the global ring.

### Entry Point

```text
buildGraphTransportsRank @0x24ef0
  └─ ncclTopoPreset(comm, &ringGraph, topoRanks)   @0x25102 → 0x6c1f0
       for ch in [0, nChannels):
         intra = ringGraph->intra + ch*localRanks         // graph +52, int[8192]
         locate i with intra[i] == comm->rank
         write topoRanks->{ringRecv,ringSend,ringPrev,ringNext}[ch]
         comm->channels[ch].ring.{prev,next} = intra[i∓1]  // ncclRing +0/+4
       if neuronIsTrn3SwitchV1Family() && comm->nKangaRingChannels > 0:
         SIMD-copy comm->kangaRing[k] → topoRanks->JbogKangaringPath[k]   (k < nKangaRingChannels ≤ 4)
```

### Algorithm

The key index is `i`: the offset of `comm->rank` inside this channel's intra-node order. The head is always `intra[0]`, the tail always `intra[localRanks-1]`; prev/next are the neighbours of `i`, with `-1` at the ends (the boundary that the network leg crosses). The struct offsets below are **byte-confirmed** against the binary's DWARF; the index arithmetic is read from decompiled control flow (MED on the exact packing of `ringPrev[31]`/`ringNext` into the same array).

```c
// ncclTopoPreset — 0x6c1f0  [HIGH: ncclTopoRanks offsets via gdb ptype; index math MED]
void ncclTopoPreset(ncclComm *comm, ncclTopoGraph *graph, ncclTopoRanks *ranks):
    int localRanks = comm->localRanks;                 // comm +17648
    int nChannels  = graph->nChannels;                 // graph +24
    for (int ch = 0; ch < nChannels; ch++):
        int *intra = &graph->intra[ch * localRanks];   // graph +52 — this channel's local order
        int i = index_of(intra, localRanks, comm->rank);   // find self in the order

        ranks->ringRecv[ch] = intra[0];                 // +0   — ring HEAD (recv-from-net entry)
        ranks->ringSend[ch] = intra[localRanks - 1];    // +128 — ring TAIL (send-to-net exit)

        int prev = (i > 0)              ? intra[i - 1] : -1;
        int next = (i < localRanks - 1) ? intra[i + 1] : -1;
        ranks->ringPrev[ch] = prev;                     // +256 — local predecessor
        ranks->ringNext[ch] = next;                     // +384 — local successor

        comm->channels[ch].ring.prev = prev;            // ncclChannel +0 → ncclRing.prev +0
        comm->channels[ch].ring.next = next;            //                  ncclRing.next +4

    // duplicate the channels for the reverse direction (the nChannels..2*nChannels copy)
    memcpy(&comm->channels[nChannels], &comm->channels[0], nChannels * sizeof(ncclChannel));

    // *** FORK: kangaRing fragment copy (TRN3-SwitchV1 only) ***
    if (neuronIsTrn3SwitchV1Family() && comm->nKangaRingChannels > 0):
        for (int k = 0; k < comm->nKangaRingChannels; k++):   // k ≤ MAX_KANGA_PATHS = 4
            // copy comm->kangaRing[k] (already rotated by topology::getPaths) into the
            // per-rank fragment, so the AllGather carries it to every rank:
            ranks->JbogKangaringPath[k] = (m128_copy) comm->kangaRing[k];   // ranks +896, int[4][4]
```

> **NOTE — head and tail are the network endpoints.** `ringRecv[ch] = intra[0]` and `ringSend[ch] = intra[localRanks-1]` are not arbitrary: on a multi-node ring the channel enters a node at its head (which posts the `irecv` from the upstream node) and exits at its tail (which posts the `isend` to the downstream node). The intra-node ranks between them are pure local-DMA hops with no network leg. A reimplementer that wires the network connect on the wrong endpoint will form a ring that never closes across nodes. The `-1` sentinel on `prev`/`next` at the ring ends marks exactly where the local segment hands off to the network ([transport-efa](transport-efa.md)).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTopoPreset` | `0x6c1f0` | per-rank head/tail/prev/next → `ncclTopoRanks`; kangaRing fragment copy (**fork**) | HIGH addr / MED math |
| `bootstrapAllGather` | (PLT @`0x1e5c0`) | exchange `ncclTopoRanks` across ranks (every rank learns every fragment) | HIGH |
| `neuronIsTrn3SwitchV1Family` | — | gate for the kangaRing copy block (TRN3-SwitchV1) | HIGH |

### Considerations

`ncclTopoPreset` writes both the per-rank fragment (`ncclTopoRanks`) **and** the first-pass `comm->channels[ch].ring.{prev,next}` (`ncclRing` offsets +0/+4, DWARF-confirmed). The fragment is the only thing the AllGather moves; the channel ring is finalized in `Postset`. The `memcpy` that duplicates `[0..nChannels)` into `[nChannels..2*nChannels)` is the start of the channel-doubling that `Postset` completes (§3) — the duplicate set carries the reverse-direction ring. The kangaRing copy is **not a computation**: by the time `Preset` runs, `comm->kangaRing[4][64]` already holds the rotated paths produced by `topology::getPaths` ([topology](topology.md) §3); `Preset` only lifts them into the per-rank fragment so the AllGather distributes them. `comm->nKangaRingChannels` (≤ 4) bounds the copy; the `<=4` unroll is consistent with the `"paths.size() <= MAX_KANGA_PATHS"` assert string (@`0x96020`).

---

## 3. Global Ring Stitch and Ordering — `ncclTopoPostset` → `ncclBuildRings`

### Purpose

`ncclTopoPostset` (@`0x6c490`) is the second half of the pivot: with every rank's fragment in hand (post-AllGather), it reassembles the global ring neighbour table, writes the final `comm->channels[ch].ring.{prev,next}`, doubles the channel count and clamps it to 32, and calls `ncclBuildRings` (@`0x6d570`) to walk the `next[]` array into each ring's `userRanks` — the linear traversal order the device program steps through. `ncclBuildRings` is the ring **order generator**: it is the one function on this page whose body is fully byte-anchored, including its two failure asserts.

### Entry Point

```text
ncclTopoPostset(comm, nodesFirstRank, allTopoRanks, &nChannels, &nNodes)   @0x6c490
  ├─ alloc 4 scratch int[128*nRanks]:  recvHead, recvTail, sendTail, prev
  ├─ gather per rank r from allTopoRanks[r]:
  │     recvHead[r] = allTopoRanks[r]->ringRecv[0]      // +0
  │     recvTail[r] = allTopoRanks[r]->ringRecv[31]
  │     sendTail[r] = allTopoRanks[r]->ringSend[31]     // +128
  │     prev[r]     = allTopoRanks[r]->ringPrev[31]     // +256
  ├─ for ch in [0, nrings): stitch per-node ring neighbour table → comm->channels[ch].ring.{prev,next}
  │     if comm->enablePod && nNodes>1 && neuronIsUltraServer(podType):
  │        group by peerInfo[r].podNodeId (RB-tree) → ncclTopoGetP2pPodInterNodeGraph
  ├─ nrings *= 2; if (nrings > 32) nrings = 32;  comm->nChannels = nrings     // ncclMaxNchannels cap
  ├─ ncclBuildRings(nrings, userRanks, comm->rank, nRanks, src, prev)   @0x6c8cf → 0x6d570
  └─ if neuronIsTrn3SwitchV1Family(): SIMD-materialize comm->kangaRing[0..nKangaRingChannels)   (§4)
```

### Algorithm

`ncclBuildRings` is a textbook ring walk: from `rank`, follow `next[]` for `nranks` steps to enumerate the full ring, store it into `rings[r*nranks ...]` (== that channel's `userRanks`), then validate that the walk closed back to the start and visited the requested rank. Both error strings are byte-confirmed.

```c
// ncclBuildRings — 0x6d570  [HIGH: body + both asserts byte-anchored; disasm 0x6d570..0x6d6a0]
ncclResult_t ncclBuildRings(int nrings, int *rings, int rank, int nranks, int *prev, int *next):
    for (int r = 0; r < nrings; r++):
        int *ring = &rings[r * nranks];                 // this channel's userRanks output
        int cur = rank;
        for (int step = 0; step < nranks; step++):      // walk next[] nranks times
            ring[step] = cur;
            cur = next[r * nranks + cur];                // follow the successor edge
        // (1) loop-back validate: after nranks steps we must be back at the start
        if (cur != rank):
            snprintf(buf, "Error : ring %d does not loop back to start (%d != %d)",  // str @0x92fb0
                     r, cur, rank);                       //  __snprintf_chk @0x6d633
            return ncclInternalError;
        // (2) membership validate: rank must appear in the ring
        if (!ring_contains(ring, nranks, rank)):
            snprintf(buf, "Error : ring %d does not contain rank %d", r, rank);       // str @0x92fe8
            return ncclInternalError;
    return ncclSuccess;
    // output `rings` == per-channel userRanks (the device step-traversal order;
    //  copied into comm->channels[ch].ring.userRanks, mirrored to devUserRanks @ +16)

// ncclTopoPostset — 0x6c490  [HIGH on strings/struct; stitch arithmetic MED]
ncclResult_t ncclTopoPostset(ncclComm *comm, int *nodesFirstRank,
        ncclTopoRanks **allRanks, int *nChannelsOut, int *nNodesOut):
    int nrings = comm->nChannels;
    int *recvHead = scratch(); int *sendTail = scratch();
    int *src = scratch();      int *prev = scratch();    // 4 × int[128*nRanks]
    for (int r = 0; r < nRanks; r++):                    // collapse every rank's fragment
        recvHead[r] = allRanks[r]->ringRecv[0];          // +0
        sendTail[r] = allRanks[r]->ringSend[31];         // +128
        prev[r]     = allRanks[r]->ringPrev[31];         // +256
    for (int ch = 0; ch < nrings; ch++):
        build per-node ring neighbour table from recvHead/sendTail/prev;
        if (comm->enablePod && *nNodesOut > 1 && neuronIsUltraServer(podType)):   // comm +19208
            group_by_podNodeId(peerInfo);                // RB-tree (comm +17544 peerInfo)
            ncclTopoGetP2pPodInterNodeGraph(...);        // pod intra/inter ring ordering
        if (computed prev/next hits comm->rank):
            comm->channels[ch].ring.prev = computed_prev;            // ncclRing +0
            comm->channels[ch].ring.next = computed_next;            // ncclRing +4
            comm->channels[ch + nrings].ring = ...;                  // reverse-direction duplicate
    nrings *= 2;  if (nrings > 32) nrings = 32;          // channel DOUBLING, cap 32
    comm->nChannels = nrings;
    ncclBuildRings(nrings, userRanks, comm->rank, nRanks, src, prev);   // 0x6c8cf → 0x6d570
    if (neuronIsTrn3SwitchV1Family()):                   // §4
        materialize_kangaRing(comm, allRanks);
    *nChannelsOut = nrings;
```

> **GOTCHA — channel doubling caps hard at 32, and the doubled half is the reverse ring.** `Postset` doubles `nrings` (`nrings *= 2`) and clamps the result to **32** — the same ceiling `ncclMaxNchannels()` (@`0x6c480`) returns as the literal `mov $0x20,%eax`. The doubled half is *not* a fresh search: it is the **reverse-direction** ring, written into `comm->channels[ch + nrings]` from the same neighbour table (the `+nrings` duplicate-channel writes). A reimplementer who treats the doubled channels as independent rings, or who omits the 32-clamp, will either over-allocate channel state (each `ncclChannel` is 512 B; 32 channels = 16384 B = `comm->channels[]`) or build a ring set the device program cannot index. The exact mapping of the doubled set to forward/reverse directions is inferred from the `+nrings` write pattern (**MED**); the `×2` and the 32-clamp are confirmed.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTopoPostset` | `0x6c490` | global ring stitch; channel doubling; calls `ncclBuildRings` + kangaRing materialize | HIGH addr / MED math |
| `ncclTopoPostset [.cold]` | `0x1f18c` | error/cold paths | HIGH |
| `ncclBuildRings` | `0x6d570` | walk `next[]` → per-channel `userRanks`; loop-back + membership validate | HIGH |
| `ncclTopoGetP2pPodInterNodeGraph` | `0x6c030` | UltraServer pod intra/inter ring ordering (gated `enablePod`) | MED |

### Considerations

`ncclBuildRings`'s output is the **device step-traversal order**, copied into `comm->channels[ch].ring.userRanks` (`ncclRing +8`) and mirrored to `devUserRanks` (`ncclRing +16`) for the device side to read — these are the two pointers `neuronDevCommSetup` consumes. The two validate-asserts are not decoration: a malformed `next[]` (e.g. a fragment lost in the AllGather) produces a ring that either fails to close (`"does not loop back to start"`, @`0x92fb0`) or omits a rank (`"does not contain rank %d"`, @`0x92fe8`), and `ncclBuildRings` is the only gate that catches it before the device program walks a broken schedule. The `enablePod` branch (comm +19208) is the UltraServer pod path: when more than one node is present and the pod type is an UltraServer, ranks are grouped by `podNodeId` (an RB-tree keyed on `peerInfo[r].podNodeId`) and the pod-level ring ordering is delegated to `ncclTopoGetP2pPodInterNodeGraph`; that function's body was not transcribed here (**MED**) and the pod-graph construction is owned by [transport-efa](transport-efa.md).

---

## 4. The `kangaRing[4][64]` and `crossracktorack` Build

### Purpose

`kangaRing` is the fork's TRN3-SwitchV1 multi-rail ring variant. The base rings come from the Neuron topology search ([topology](topology.md) §3) — `topology::getPaths` builds Hamiltonian device rings and rotates one ring per NIC lane (the `duplicatePaths`/`rotatePath` inline in `getPaths`, ≤ `MAX_KANGA_PATHS = 4`). This part of `Postset` **materializes** those rotated rings into the fork-added `comm->kangaRing[4][64]` (1024 B) from the per-rank `JbogKangaringPath[4][4]` fragments the AllGather distributed. It is a **copy**, not a search — the structural work happened upstream.

### Entry Point

```text
ncclTopoPostset @0x6c490   (kangaRing tail block)
  if neuronIsTrn3SwitchV1Family():
    assert nec_get_virtual_core_size() == 0           // vcore-size precondition
    assert die/jbog count ∈ {1,2,3}                   // neuronTrnSwitchV1JbogId (@0x778b0) math
    for k in [0, comm->nKangaRingChannels):            // ≤ MAX_KANGA_PATHS = 4
      SIMD-copy allTopoRanks[*]->JbogKangaringPath  →  comm->kangaRing[k]    // 16 × __m128i loop
    // "built %d kangaring paths" @0x8f11e ;  "kangaring path[%d]=%s" @0x8f14d
```

### Algorithm

The materialize is a fixed `__m128i` copy unroll: each `kangaRing[k]` is 64 ints = 256 B = 16 × `__m128i`, and the loop runs `comm->nKangaRingChannels` times (≤ 4). The `JbogKangaringPath[4][4]` fragments (16 ints = 64 B, `ncclTopoRanks +896`) supply the per-Jbog path that the copy assembles into the full `kangaRing[k]`.

```c
// ncclTopoPostset kangaRing materialize — 0x6c490 tail  [HIGH: fields/strings; copy shape MED]
void materialize_kangaRing(ncclComm *comm, ncclTopoRanks **allRanks):
    if (!neuronIsTrn3SwitchV1Family()):
        return;
    // preconditions asserted in the body:
    assert(nec_get_virtual_core_size() == 0);          // RT callback — vcore precondition
    int jbog = neuronTrnSwitchV1JbogId(comm->rank);    // 0x778b0 — die/jbog id ∈ {1,2,3}

    for (int k = 0; k < comm->nKangaRingChannels; k++):   // comm +16384, ≤ 4
        // comm->kangaRing[k] is 64 ints = 16 __m128i; copied from the AllGathered
        // JbogKangaringPath fragments (the rotated per-NIC-lane ring):
        __m128i *dst = (__m128i*) comm->kangaRing[k];      // comm +16388, int[4][64]
        for (int v = 0; v < 16; v++):                       // 16 × 128-bit stores
            dst[v] = load_jbog_path_fragment(allRanks, k, v);
    // ringGraph->crossracktorack (graph +33076) was set during the search; the kangaRing
    // copy reads it to decide cross-rack vs intra-rack lane assignment.

    log("built %d kangaring paths", comm->nKangaRingChannels);    // @0x8f11e
```

> **QUIRK — kangaRing is materialized, not searched, and only on TRN3-SwitchV1.** The `comm->kangaRing[4][64]` table looks like a ring-construction output, but no search runs in `Postset` — by the time this block executes, `topology::getPaths` ([topology](topology.md) §3) has already produced the rotated per-NIC-lane rings, and this is a fixed `16 × __m128i` copy gated on `neuronIsTrn3SwitchV1Family()`. On every other platform the block is skipped entirely and `comm->nKangaRingChannels` stays 0. A reimplementer must (a) supply the upstream rotation in the topology layer, not here, and (b) gate the materialize on the SwitchV1 family predicate — wiring `kangaRing` on TRN1/TRN2 produces a table the device program will not read. The `≤ 4` bound is `MAX_KANGA_PATHS` (`"paths.size() <= MAX_KANGA_PATHS"` @`0x96020`); the `{1,2,3}` jbog-count assert and the `nec_get_virtual_core_size() == 0` precondition are read from decompiled control flow (**MED**).

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTopoPostset` (kangaRing tail) | `0x6c490` | `__m128i` copy of rotated paths → `comm->kangaRing[4][64]`; gated TRN3-SwitchV1 | HIGH addr / MED copy |
| `topology::getPaths` | `0x5e400` | **upstream** — builds + rotates the base rings ([topology](topology.md)) | HIGH |
| `topology::findPathRec` | `0x5c4e0` | recursive backtracking core that produces each Hamiltonian ring | HIGH |
| `neuronTrnSwitchV1JbogId` | `0x778b0` | die/jbog id math (TRN3-SwitchV1); asserts count ∈ {1,2,3} | HIGH addr / MED math |

### Considerations

The two fork-added struct fields are the reimplementer's tell. `comm->kangaRing[4][64]` (@+`16388`, 1024 B) and `comm->nKangaRingChannels` (@+`16384`) live in `ncclComm` between `channels[32]` (ending at +16384) and `nMlaInCycle` (+17412); `ncclTopoRanks.JbogKangaringPath[4][4]` (@+`896`, 64 B) sits in the slack past the inherited `ringNext[32]` array (which ends at +512) and the dead `treeTo*` region; and `ncclTopoGraph.crossracktorack` (@+`33076`) is appended immediately past the stock struct's `inter[64]` (which ends at +33076 in stock NCCL). All four offsets are **DWARF-confirmed** by `gdb ptype /o`. A reimplementer that uses the stock NCCL struct layouts will mis-place every field after `ncclTopoGraph.inter[64]` and silently corrupt the kangaRing copy. `MAX_KANGA_PATHS = 4` is corroborated here by the `<=4` copy unrolls plus the assert string; it was originally byte-pinned in the static-archive analysis (the cmp is not re-pinned in this `.so`, **MED**).

---

## 5. Ring Connect — Only RING Peers Are P2P-Connected

### Purpose

The final stage connects the transports. The central rule is **selectivity**: of the `nRanks²` possible peer pairs, libnccom connects only those that are **ring-adjacent** in some channel — each rank's `ring.prev` and `ring.next` per channel. `ncclTransportP2pSetup` (@`0x3eb80`) and `ncclTransportP2pConnect` (@`0x3e9c0`) walk the channels and connect exactly those edges; `ncclTopoGetNetDev` (@`0x6bf30`) binds each channel to its EFA NIC. A rank never connects to a non-neighbour, which is what makes a ring an O(N)-edge topology instead of O(N²).

### Entry Point

```text
buildGraphTransportsRank @0x24ef0
  ├─ ncclTransportP2pConnect(comm, ch, …)   @0x25aec → 0x3e9c0   ── per-channel connect mask
  ├─ ncclTransportP2pSetup(comm, &ringGraph) @0x25b26 → 0x3eb80
  │     for ch in [0, nChannels):
  │       connectPeer(comm, ch, channels[ch].ring.prev)   // upstream neighbour only
  │       connectPeer(comm, ch, channels[ch].ring.next)   // downstream neighbour only
  │       predicate: p2pCanConnect (intra-node) | netNeuronCanConnect (inter-node)
  │       per-channel NIC: ncclTopoGetNetDev(sys, rank, &ringGraph, ch, &dev)   @0x6bf30
  └─ ncclTopoComputeP2pChannels(comm)        @0x25d95 → 0x654f0   ── p2pnChannels pow2 clamp ≤ 32
```

### Algorithm

The connect set is the ring adjacency. `ncclTopoGetNetDev` is the one byte-exact function here: it picks the EFA NIC for a channel by rotating across rails (`channel % nChannels`) and choosing the forward or reverse NIC of that rail-pair by a direction bit (whether the channel's intra-head is *this* rank).

```c
// ncclTransportP2pSetup — 0x3eb80  [HIGH: only-neighbour connect from connect.cc semantics]
ncclResult_t ncclTransportP2pSetup(ncclComm *comm, ncclTopoGraph *graph):
    for (int ch = 0; ch < comm->nChannels; ch++):
        ncclRing *ring = &comm->channels[ch].ring;      // ncclChannel +0
        // connect ONLY the two ring neighbours of this rank in this channel:
        if (ring->prev != -1) connectTo(comm, ch, ring->prev, RECV);   // accept from upstream
        if (ring->next != -1) connectTo(comm, ch, ring->next, SEND);   // connect to downstream
        // predicate selects transport: p2pCanConnect (intra) vs netNeuronCanConnect (inter-node)
        // inter-node legs bind a NIC:
        int dev;
        ncclTopoGetNetDev(comm->topo, comm->rank, graph, ch, &dev);    // 0x6bf30
    return ncclSuccess;

// ncclTopoGetNetDev — 0x6bf30  [HIGH: byte-exact, disasm 0x6bf5b..0x6bf80]
ncclResult_t ncclTopoGetNetDev(ncclTopoSystem *sys, int rank,
                               ncclTopoGraph *graph, int channelId, int *dev):
    if (graph == NULL):
        return ncclTopoGetLocalNet(sys, rank, dev);     // fallback: local-net pick
    int c        = channelId % graph->nChannels;        // 0x6bf5c idivl 0x18(graph) — nChannels @+24
    int count    = sys->nodes[0].count;                 // 0x6bf5f mov (sys)
    int headRank = graph->intra[c * count];             // 0x6bf66 — graph->intra @+52 (0x34)
    int dir      = (headRank != rank) ? 1 : 0;          // 0x6bf6b setne — direction bit
    *dev = graph->inter[2 * c + dir];                   // 0x6bf74 lea; 0x6bf79 inter @+32820 (0x8034)
    return ncclSuccess;
```

> **NOTE — the NIC binding is byte-exact and is "channel N → rail N".** The formula `*dev = graph->inter[2*(channelId % graph->nChannels) + (graph->intra[(ch%nCh)*count] != rank)]` is read directly from disassembly: `idivl 0x18(%rsi)` is `% graph->nChannels` (nChannels @+24), the `cmp …, 0x34(%rsi,…)` reads `graph->intra` (+52), the `setne` produces the direction bit, and `mov 0x8034(%rsi,…),%eax` reads `graph->inter` (+32820). Channel index mod `nChannels` rotates across rails; the direction bit (intra-head ≠ rank) picks the forward or reverse NIC of that rail-pair. The producer of the `inter[2*c]` NIC ids is `ncclTopoGetChannelFromXml` (@`0x686c0`), which fills `graph->inter[2*c]` while parsing the `<channel>` XML ([topology](topology.md) §4). A reimplementer wiring EFA must reproduce this exact index, or channels land on the wrong NIC and the ring's network legs cross rails incorrectly.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTransportP2pSetup` | `0x3eb80` | connect only ring-neighbour peers; select transport predicate | HIGH |
| `ncclTransportP2pConnect` | `0x3e9c0` | per-channel connect mask (prev/next) | HIGH |
| `ncclTopoGetNetDev` | `0x6bf30` | per-channel EFA NIC: `inter[2*(ch%nCh)+dir]` (byte-exact) | HIGH |
| `ncclTopoGetChannelFromXml` | `0x686c0` | producer: fills `graph->intra[count*c+g]` and `graph->inter[2*c]` NIC ids | HIGH |
| `ncclTopoComputeP2pChannels` | `0x654f0` | `p2pnChannels` pow2 clamp ≤ 32; bit-reverse channel map | HIGH |

### Considerations

The selectivity is the whole point of building a ring on the host: by connecting only `ring.prev`/`ring.next` per channel, the transport setup is O(nChannels) edges per rank instead of O(nRanks²), and the proxy ([proxy-engine](proxy-engine.md)) only ever posts to those neighbours. The transport split — `p2pCanConnect` for intra-node edges versus `netNeuronCanConnect` for cross-node legs — is decided per edge inside `ncclTransportP2pSetup` and detailed on [transport-intra](transport-intra.md) and [transport-efa](transport-efa.md); this page fixes only that the *connect set* is the ring adjacency. `ncclTopoGetNetDev`'s `graph == NULL` branch falls back to `ncclTopoGetLocalNet` (a local-net pick when no searched graph is present); in production the Neuron path always supplies the graph, so the fallback is the bring-up/degenerate case. The downstream consumer of everything this part builds is `neuronDevCommSetup` in libnrt's `rt_neuron`, which reads `comm->channels[].ring.{prev,next,userRanks,devUserRanks}` plus `comm->kangaRing[][]` to build the device communicator — and from there the device program runs the step loop owned by [collectives/ring-scheduling](../collectives/ring-scheduling.md).

---

## Related Components

| Name | Relationship |
|---|---|
| inherited `graph/connect.cc`, tail of `graph/topo.cc` | reusable from public NCCL `2.31.24`; `ncclTopoPreset`/`Postset`/`BuildRings` operate on stock structs |
| **fork** kangaRing materialize (`Postset` tail) | TRN3-SwitchV1 `__m128i` copy of `topology::getPaths` rotations into `comm->kangaRing[4][64]` — where the RE work lives |
| [topology](topology.md) §3–4 | upstream of this page: produces the searched `ncclTopoGraph` + the rotated kangaRing paths this page stitches |
| [collectives/ring-scheduling](../collectives/ring-scheduling.md) | downstream: the **device** step loop that walks `userRanks` and applies the reduce — NOT built here |
| RT (`libnrt`, `@NRT_2.0.0`) | `neuronDevCommSetup` consumes `comm->channels[].ring` + `comm->kangaRing[][]` to build the device communicator |

## Cross-References

- [Topology Detection and Graph Build](topology.md) — `ncclTopoPreset`/`Postset` entry, the `<channel>`-XML bridge, and the `topology::getPaths` rotation that produces the base + kangaRing rings this page stitches
- [Overview: the NCCL Fork (2.31.24+nrt2.0)](overview.md) — the Part-XII map; this page details its stage 6 (graph/ring wiring), the live algorithm path
- [On-Device Ring Scheduling Math](../collectives/ring-scheduling.md) — the **device-side** ring step loop, reduce-op application, and LL/LL128/Simple protocol that consume this page's `userRanks`/`kangaRing` wiring
- [Intra-Node Transport (P2P)](transport-intra.md) — `p2pCanConnect`, the intra-node ring edges that `ncclTransportP2pSetup` connects between ring neighbours
- [back to index](../index.md)
