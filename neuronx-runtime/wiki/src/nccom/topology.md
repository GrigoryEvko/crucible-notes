# Topology Detection and Graph Build

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text` VMA == file offset for the cited band, so every `0x…` is both a file offset and an analysis VMA.*
> *Evidence grade: **Confirmed (byte-anchored)** — every symbol, address, and struct offset below was re-read from the binary's own DWARF (`nm -C` / `objdump -d` / `readelf`); the `neuronTopoGetGraph` dispatch ladder is read directly from disassembly. Decompiler-derived control flow inside `topology::getPaths` / `findPathRec` is tagged MED. Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

Topology in libnccom is the stage that turns "a set of Trainium devices reachable from this rank" into "a per-channel ordering of ranks the proxy can drive." If you already hold a mental model of upstream NVIDIA NCCL's `graph/` subsystem — a hardware probe emits a **topology XML** DOM (`ncclXml`), the XML is parsed into a node/link graph (`ncclTopoSystem`), an all-pairs path matrix is computed over that graph (`graph/paths.cc`), a recursive search walks the graph to produce ring/tree channel orders (`ncclTopoGraph`), and `ncclTopoPreset`/`Postset` stitch per-rank orders into global rings — then you already know the inherited half of this page. Those files are present verbatim: `graph/topo.cc`, `graph/xml.cc`, `graph/paths.cc`, `graph/search.cc`, and `channel.cc` all appear as `_GLOBAL__sub_I_*` ctors and in assert strings, parsing the same `ncclXml → ncclTopoSystem → ncclTopoGraph` representation so the inherited tuning code consumes it unchanged.

The **fork delta** is the device-discovery substrate underneath that representation. Upstream NCCL discovers GPUs by walking sysfs PCI / NVLink. There are no GPUs and no NVLink here, so the discovery layer is **synthesized**: `neuronTopoGetXml` (@`0x623a0`) emits the topology XML *from* the RT's instance/MLA/pod queries rather than reading it off the bus, one synthesizer per platform (`inf2`/`trn1`/`trn2`/`trn3` `TopoGetXml`). On top of that, a second, **entirely Neuron-original graph search** lives beside the inherited DFS: a C++ `topology` class (`topo_neuron*.cc`, confirmed by ctors `_GLOBAL__sub_I_topo_neuron{,_sunda,_cayman,_mariana}.cc`) that runs a *priority* path search over a `NeuronNode`/`NeuronEdge` adjacency graph (`topology::getPaths` @`0x5e400`), keyed on edge **locality** (shared-memory < remote-MLA < D2D < RMTV < local) and the MLA/SEngine fabric geometry. The two searches co-exist and are selected at runtime; the inherited one emits ring/tree channels in `ncclTopoGraph`, the Neuron one emits device/core paths that are re-encoded as `<channel>` XML and parsed back into the *same* `ncclTopoGraph`. Either way the downstream consumers (preset/postset, channel assignment) see one representation.

The page is organized as the pipeline in execution order, one `##` unit per stage with the fixed H3 vocabulary (Purpose / Entry Point / Algorithm / Function Map / Considerations): (1) **system discovery** — synthesize XML, parse to `ncclTopoSystem`, compute paths, verify; (2) the **inherited recursive search** (`ncclTopoCompute` → `ncclTopoSearchRec*`); (3) the **Neuron `topology`-class priority search** and its platform dispatch (`neuronTopoGetGraph`); (4) **channel assignment** — `ncclTopoPreset`/`Postset` rank wiring, the `<channel>`-XML bridge, and `ncclTopoComputeP2pChannels` → `initChannel`. The ring/`kangaRing` stitching math that `ncclTopoPostset` performs is the subject of its own sibling ([algorithm-ring](algorithm-ring.md)); this page hands it the wired `ncclTopoRanks` and stops.

For reimplementation, the contract of the topology layer is:

- **The two-direction representation pivot** — `ncclXml` (the synthesized DOM) ⇄ `ncclTopoSystem` (the node/link graph) → `ncclTopoGraph` (the search output), and the second pivot the Neuron path uses: device/core paths → `<channel>` XML → `ncclTopoGraph`. A reimplementer must reproduce both DOM round-trips, not just the in-memory graph.
- **The synthesized discovery** — there is no bus walk; `neuronTopoGetXml` builds `<system>/<cpu>/<pci>/<gpu>/<nic>` tags out of `nrt_get_instance_info` + the `nec_*` MLA/pod callbacks, per platform. The `<gpu>` tags are Trainium NeuronCores reusing the NCCL GPU node-type bucket.
- **The dual graph search and its selection** — the inherited `ncclTopoSearchRec*` DFS over `ncclTopoSystem` versus the `topology::getPaths` priority DFS over `NeuronNode`/`NeuronEdge`, gated by `NCCL_GRAPH_USE_NEW_ALGORITHM` and per-platform family predicates, both reducing to one `ncclTopoGraph`.
- **The channel-assignment pipeline** — `ncclTopoPreset` (per-rank ring/tree wiring into `ncclTopoRanks`) → bootstrap AllGather → `ncclTopoPostset` (global stitch, kangaring/mla-cycle) → `ncclTopoComputeP2pChannels` → per-channel `initChannel`, and the Neuron-added fields (`ncclTopoGraph.crossracktorack`, `ncclTopoRanks.JbogKangaringPath[4][4]`) that have no stock-NCCL analogue.

| | |
|---|---|
| **Graph-build entry** | `neuronBuildGraphComm` @`0x46e90` → `ncclCommBuildGraphRank` @`0x26da0` → `buildGraphTransportsRank` @`0x24ef0` |
| **System discovery** | `ncclTopoGetSystem` @`0x58860` → `neuronTopoGetXml` @`0x623a0` → `ncclTopoGetSystemFromXml` @`0x58540` |
| **Path matrix** | `ncclTopoComputePaths` @`0x647a0` → `ncclTopoSetPaths` @`0x63660` (per-node BFS over links) |
| **Verify** | `verifyTopology` @`0x24a20` — asserts legal device count 1/4/8/16; gated by `CCOM_DBG_DISABLE_VERIFY_TOPOLOGY` |
| **Inherited search** | `ncclTopoCompute` @`0x6ab30` → `ncclTopoSearchRec` @`0x66a20` → `ncclTopoSearchRecGpu` @`0x66e10` (DFS core) |
| **Neuron search** | `neuronTopoGetGraph` @`0x625c0` (platform dispatch) → `topology::getPaths` @`0x5e400` → `findPaths`/`findPath`/`findPathRec` |
| **Algorithm gate** | `neuronTopoUseNewAlgorithm` @`0x70850` reads env `NCCL_GRAPH_USE_NEW_ALGORITHM` (default OFF) |
| **Channel assign** | `ncclTopoPreset` @`0x6c1f0` → `ncclTopoPostset` @`0x6c490` → `ncclTopoComputeP2pChannels` @`0x654f0` → `initChannel` @`0x33c30` |
| **XML ⇄ graph bridge** | `getGraphTable` @`0x73f20` → `xmlAddChannel<int const*>` @`0x6ef50` / `channelAddNic` @`0x6e0b0` → `ncclTopoGetGraphFromXml` @`0x693e0` |
| **Search output struct** | `ncclTopoGraph` — **33080 B**; Neuron-added `crossracktorack` @+`0x812C` |
| **Rank-wiring struct** | `ncclTopoRanks` — **960 B**; Neuron-added `JbogKangaringPath[4][4]` @+`0x380` |
| **Source files** | inherited `graph/{topo,xml,paths,search}.cc`, `channel.cc`; **fork** `graph/topo_neuron{,_sunda,_cayman,_mariana}.cc` |

> **QUIRK — the device graph is synthesized, not probed.** A reimplementer porting NCCL's topology stage will look for the sysfs walk (`/sys/bus/pci`, NVLink discovery) that produces the XML. It is not on the Neuron path: `neuronTopoGetXml` (@`0x623a0`) *manufactures* the entire `<system>` DOM from RT queries (`nrt_get_instance_info`, the `nec_*` MLA/pod callbacks), and the only sysfs read that survives is a NUMA-node probe (`"/sys/.../node%03d"`, @`0x97390`). NeuronCores are emitted as `<gpu>` nodes reusing NCCL's GPU node-type bucket, so the *inherited* graph code (paths, search, preset) runs unmodified over them. Port the synthesizers (`{inf2,trn1,trn2,trn3}TopoGetXml`), not a bus walk; the rest of the topology subsystem is stock NCCL that consumes whatever XML you hand it.

---

## 1. System Discovery — Synthesize XML, Parse to System, Compute Paths

### Purpose

The first topology stage runs during init (inside `initTransportsRank`, see [comm-init](comm-init.md#3-bootstrap--inittransportsrank-and-the-dual-handshake)) and produces the `ncclTopoSystem` node graph plus its all-pairs path matrix. Upstream NCCL fills this by probing the bus; libnccom fills it by **synthesis** — `neuronTopoGetXml` emits a topology XML DOM from RT queries, the inherited parser turns the DOM into a node/link graph, and the inherited `paths.cc` computes the per-node distance matrix the search later reads. The stage closes with `verifyTopology`, the one input-validation gate, which asserts the discovered device count is a legal Trainium fabric size.

### Entry Point

```text
initTransportsRank @0x28ed0   [comm-init cell]
  └─ ncclTopoGetSystem(comm, &topo, …)            @0x58860
       ├─ getenv NCCL_TOPO_FILE → ncclTopoGetXmlFromFile  (if provided; else synthesize)
       ├─ neuronTopoGetXml(xml, nRanks, nNodes)    @0x623a0   ── synthesize Neuron XML in-memory
       │     └─ {inf2,trn1,trn1NoNic,trn2,trn3}TopoGetXml / trnTopoGetXmlHelper  @0x6fb50…0x824c0
       │           └─ buildHeader · addSystem · addGpusToSystem · addNeuronCore
       │              · addPciSwitchesToCpu · addNumaNodesToSystem · addP2pLink(s)
       ├─ ncclTopoFillGpu / ncclTopoFillNet        ── augment with sysfs PCI/NIC detail
       ├─ ncclTopoTrimXml                          ── drop nodes not on any rank's path
       ├─ (env NCCL_TOPO_DUMP_FILE) ncclTopoDumpXmlToFile
       └─ ncclTopoGetSystemFromXml(xml, &topo)     @0x58540   ── XML DOM → node/link graph
             ├─ ncclTopoAddCpu/AddPci/AddGpu/AddNic/AddNet/AddNvLinks   @0x55b10…0x57d40
             │     └─ ncclTopoCreateNode · ncclTopoConnectNodes · ncclTopoConnectCpus
             └─ ncclTopoSortSystem                 @0x55980
  └─ ncclTopoComputePaths(topo, peerInfo)          @0x647a0   ── per-node BFS path matrix
       └─ ncclTopoSetPaths(node, topo) per node    @0x63660   · addCpuStep @0x634e0 · followPath @0x65a00
  └─ ncclTopoTrimSystem(topo, comm)                @0x64fd0   ── prune unreachable; re-ComputePaths
  └─ verifyTopology(deviceIds)                     @0x24a20   ── assert count ∈ {1,4,8,16}
```

### Algorithm

The synthesis-then-parse shape means the XML DOM is an intermediate that never touches disk (unless `NCCL_TOPO_DUMP_FILE` is set). The synthesizer's job is to emit, for the detected platform, the same tag schema the inherited parser expects.

```c
// ncclTopoGetSystem — 0x58860  [HIGH: call sites + env strings byte-confirmed]
ncclResult_t ncclTopoGetSystem(ncclComm *comm, ncclTopoSystem **out, int flags):
    ncclXml *xml = calloc(sizeof(ncclXml));        // 115,085,832 B DOM (nodes[10432])
    const char *file = getenv("NCCL_TOPO_FILE");
    if (file)
        ncclTopoGetXmlFromFile(file, xml);         // operator override: load a hand-written topology
    else
        neuronTopoGetXml(xml, comm->nRanks, comm->nNodes);   // 0x623a0 — SYNTHESIZE [FORK]
    ncclTopoFillGpu(xml, …); ncclTopoFillNet(xml, …);        // augment from sysfs where present
    ncclTopoTrimXml(xml);                                    // 0x89a80 — drop off-path nodes
    if (getenv("NCCL_TOPO_DUMP_FILE")) ncclTopoDumpXmlToFile(…);
    return ncclTopoGetSystemFromXml(xml, out);     // 0x58540 — parse DOM → ncclTopoSystem

// neuronTopoGetXml — 0x623a0  [HIGH: per-platform synthesizers byte-confirmed]
ncclResult_t neuronTopoGetXml(ncclXml *xml, int nRanks, int nNodes):
    // platform select by family predicate (see §3 dispatch); each emits the SAME tag schema
    if (neuronIsTRN3Family())        return trn3TopoGetXml(xml, nRanks, nNodes);   // 0x782e0
    if (neuronIsTRN2Family())        return trn2TopoGetXml(xml, nRanks, nNodes, instInfo);  // 0x75880
    if (is_inf2)                     return inf2TopoGetXml(xml);                    // 0x6fb50
    if (has_nic)                     return trn1TopoGetXml(xml, nRanks, nNodes);    // 0x708a0
    else                             return trn1NoNicTopoGetXml(xml);              // 0x73110
    //   each → buildHeader (0x7e950) → addSystem → addGpusToSystem (NeuronCores as <gpu>)
    //        → addNeuronCore (0x7f3a0) → addPciSwitchesToCpu → addNumaNodesToSystem
    //        → addP2pLink(s) (0x7e0e0/0x7e580) wiring intra-MLA / D2D edges

// ncclTopoGetSystemFromXml — 0x58540  [HIGH]
ncclResult_t ncclTopoGetSystemFromXml(ncclXml *xml, ncclTopoSystem **out):
    sys = calloc(sizeof(ncclTopoSystem));          // 7,755,840 B; nodes[7] type-buckets
    for each <cpu> node:  ncclTopoAddCpu(xmlNode, sys);          // 0x57230
    for each <gpu> node:  ncclTopoAddGpu(xmlNode, sys, parent);  // 0x563c0 (NeuronCore)
    for each <nic>/<net>: ncclTopoAddNic/AddNet(…);              // 0x56270/0x55b10
    ncclTopoAddNvLinks(…);                          // 0x57d40 (intra-MLA links via <nvlink> tags)
    ncclTopoConnectCpus(sys);                        // 0x552f0 — fully connect CPU sockets
    ncclTopoSortSystem(sys);                         // 0x55980 — canonical node order
    *out = sys;

// ncclTopoComputePaths — 0x647a0  [HIGH]
ncclResult_t ncclTopoComputePaths(ncclTopoSystem *sys, ncclPeerInfo *peers):
    for each node in every type-bucket:
        ncclTopoSetPaths(node, sys);                 // 0x63660 — BFS from node over links[],
                                                     // fill node.paths[targetType] (ncclTopoLinkList*[7])
                                                     // each path: hop list + count + min width
    // addCpuStep (0x634e0) inserts a CPU relay hop where two nodes share no direct link
    // followPath (0x65a00) accumulates min-width / hop-count along a candidate path
```

> **NOTE — `<gpu>` is a NeuronCore.** The XML schema is upstream NCCL's, so a NeuronCore is emitted under the `<gpu>` tag and lands in the `ncclTopoSystem` GPU type-bucket (`ncclTopoAddGpu` @`0x563c0`). The `ncclTopoNode` GPU union (`+0 dev`, `+4 rank`, `+8 cudaCompCap`, `+12 gdrSupport`) is reused with Trainium meanings; `ncclTopoGetCompCap` @`0x5a5e0` still reads a "compute capability" that is a Trainium generation tag, not an SM version. Inter-MLA links are emitted as `<nvlink>` tags and added by `ncclTopoAddNvLinks` @`0x57d40` — there is no NVLink hardware; the tag is the transport-agnostic "fast intra-node link" the inherited code understands.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTopoGetSystem` | `0x58860` | synthesize-or-load XML, fill, trim, parse | HIGH |
| `neuronTopoGetXml` | `0x623a0` | **fork** — platform-dispatched XML synthesis | HIGH |
| `ncclTopoGetSystemFromXml` | `0x58540` | XML DOM → `ncclTopoSystem` node/link graph | HIGH |
| `ncclTopoAddGpu` | `0x563c0` | add a NeuronCore as a `<gpu>` node | HIGH |
| `ncclTopoAddNvLinks` | `0x57d40` | add intra-MLA fast links (`<nvlink>` tags) | HIGH |
| `ncclTopoConnectNodes` | `0x55250` | create a typed, width-weighted link between two nodes | HIGH |
| `ncclTopoSortSystem` | `0x55980` | canonical node ordering (`ncclTopoSort` @`0x53f90`) | HIGH |
| `ncclTopoComputePaths` | `0x647a0` | all-pairs per-node path matrix | HIGH |
| `ncclTopoSetPaths` | `0x63660` | BFS from one node; fill `node.paths[7]` | HIGH |
| `ncclTopoTrimSystem` | `0x64fd0` | prune nodes off every rank's path; re-compute | HIGH |
| `verifyTopology` | `0x24a20` | assert device count ∈ {1,4,8,16}; gated by env | HIGH |
| `trn1TopoGetXml` | `0x708a0` | canonical platform synthesizer (largest, 0x2868) | HIGH |
| `addNeuronCore` | `0x7f3a0` | emit one `<gpu>`/NeuronCore tag into the DOM | HIGH |

### Considerations

`verifyTopology` (@`0x24a20`) is the single guard on discovery integrity. It asserts every device id in the rank set is actually present (`"Unsupported topology. Devices with IDs %d and %d are part of topology but devices with those IDs are missing"`, @`0x8b060`) and that the local device count is a legal fabric size — `"… expects 1, 4, 8, or 16 local devices but got %d"` (@`0x8b0d0`). A reimplementer can disable it with `CCOM_DBG_DISABLE_VERIFY_TOPOLOGY` (accessor @`0x25ef0`), which is exactly how the developers bring up illegal partial topologies; in production it is the assertion that catches a miswired pod before the search runs on a degenerate graph. The double `ncclTopoComputePaths` — once before `ncclTopoTrimSystem` and once after — is deliberate: trimming removes nodes that no rank routes through, which invalidates the first path matrix, so the second recompute is over the pruned graph the search will actually walk.

---

## 2. The Inherited Recursive Search

### Purpose

`ncclTopoCompute` (@`0x6ab30`) is the inherited NCCL search driver: for a requested channel pattern (Ring, Tree, …) it runs a recursive depth-first search over `ncclTopoSystem`, looking for a channel — an ordering of GPU nodes whose inter-node links satisfy the pattern's bandwidth and crossing constraints — then a tuning loop relaxes the constraints (speed downgrade, `typeIntra` relaxation, `sameChannels`) and re-searches until it has the requested number of channels or exhausts the relaxation ladder. The output is an `ncclTopoGraph` (33080 B): per-channel intra-node rank orders in `intra[8192]` and inter-node endpoints in `inter[64]`. This is the path that runs when the Neuron "new algorithm" is **not** selected.

### Entry Point

```text
ncclTopoCompute(comm, &graph, nNodes)   @0x6ab30
  ├─ (fast path) "Search %d : %d channels loaded from XML graph"  ── if a graph XML was provided
  ├─ ncclTopoSearchInit(sys)                       @0x66110   ── seed search state / replay table
  ├─ ncclTopoSearchParams(sys, pattern, &min, &max)@0x669c0   ── per-pattern channel bounds
  └─ tuning loop {  ncclTopoSearchRec(sys, &graph, &tmp, &time)  @0x66a20
        └─ ncclTopoSearchRecGpu(sys, g, tmp, node, …)   @0x66e10   ── DFS recursion core
              ├─ ncclTopoSearchNextGpuSort(…)    @0x66260   ── order candidate next-GPUs
              ├─ ncclTopoSearchTryGpu(…)         @0x67c30   ── try extending the channel
              ├─ ncclTopoFollowPath / followPath @0x65e10/0x65a00 ── min-width along the hop list
              ├─ ncclTopoSearchRecNet(…)         @0x68050   ── extend across a NIC/net node
              └─ ncclTopoReplayGetGpu(…)         @0x668f0   ── replay a known-good prefix
        ncclTopoCompareGraphs(&graph, &tmp, &better)  @0x66960   ── keep best by speed/nChannels
     }  // relax speedInter/typeIntra/sameChannels and repeat until min channels found
```

### Algorithm

The tuning loop is the part that distinguishes "found a legal channel" from "found *enough fast* channels." It is a descent: start at the highest speed/type constraint, search, and on failure loosen exactly one knob and retry.

```c
// ncclTopoCompute — 0x6ab30  [HIGH on structure; relaxation ladder MED]
ncclResult_t ncclTopoCompute(ncclComm *comm, ncclTopoGraph *graph, int nNodes):
    ncclTopoSearchInit(comm->topo);                     // 0x66110
    ncclTopoSearchParams(comm->topo, graph->pattern, &graph->minChannels, &graph->maxChannels);
    // initial constraints: highest speedIntra/Inter, tightest typeIntra/Inter, sameChannels=1
    while (graph->nChannels < graph->minChannels && constraints_can_relax):
        ncclTopoGraph tmp = *graph;                     // candidate
        ncclTopoSearchRec(comm->topo, graph, &tmp, &timeBudget);   // 0x66a20 — DFS
        int better;
        ncclTopoCompareGraphs(graph, &tmp, &better);    // 0x66960
        if (better) *graph = tmp;                        // keep the stronger result
        relax_one_of(speedInter↓, typeIntra↑, sameChannels→0, crossNic→1);  // loosen & retry
    // graph->intra[8192] now holds per-channel intra-node rank orders; graph->inter[64] endpoints

// ncclTopoSearchRecGpu — 0x66e10  [MED: recursion shape from decomp]
void ncclTopoSearchRecGpu(sys, ncclTopoGraph *g, ncclTopoGraph *saved,
        ncclTopoNode *gpu, int step, int backToFirstRank, int forcedOrder, int *time):
    if (step == nGpusInChannel):                        // channel complete
        if (closes_into_a_legal_ring) ncclTopoCompareGraphs(saved, g, …);  // candidate accept
        return;
    int next[NCCL_TOPO_MAX_NODES], count;
    ncclTopoSearchNextGpuSort(sys, g, gpu, next, &count, forcedOrder);  // 0x66260 — order successors
    for (i = 0; i < count; i++):
        ncclTopoSearchTryGpu(sys, g, saved, …, next[i], …, &time);      // 0x67c30 — extend + recurse
        // followPath accumulates min link width; a too-narrow hop prunes this branch
```

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTopoCompute` | `0x6ab30` | search driver + relaxation tuning loop | HIGH |
| `ncclTopoSearchInit` | `0x66110` | seed search/replay state | HIGH |
| `ncclTopoSearchParams` | `0x669c0` | per-pattern min/max channel bounds | HIGH |
| `ncclTopoSearchRec` | `0x66a20` | top-level recursion entry | HIGH |
| `ncclTopoSearchRecGpu` | `0x66e10` | DFS recursion core (0xe12 — largest) | HIGH |
| `ncclTopoSearchNextGpuSort` | `0x66260` | order candidate next-GPUs by path quality | HIGH |
| `ncclTopoSearchTryGpu` | `0x67c30` | try extending the channel by one node | HIGH |
| `ncclTopoSearchRecNet` | `0x68050` | extend a channel across a net/NIC node | HIGH |
| `ncclTopoFollowPath` / `followPath` | `0x65e10` / `0x65a00` | min-width / hop-count along a path | HIGH |
| `ncclTopoCompareGraphs` | `0x66960` | keep the better of two candidate graphs | HIGH |
| `ncclTopoReplayGetGpu` | `0x668f0` | replay a known-good channel prefix | HIGH |

### Considerations

The "channels loaded from XML graph" fast path (`"Search %d : %d channels loaded from XML graph"`, @`0x92b08`) is the operator escape hatch: if a graph was supplied via `NCCL_GRAPH_FILE` it is parsed by `ncclTopoGetGraphFromXml` (§4) and the whole DFS is skipped. The relaxation ladder inside the tuning loop is read from decompiled control flow rather than a symbol-anchored table (**MED**) — the precise order in which `speedInter`, `typeIntra`, `sameChannels`, and `crossNic` are loosened is inferred from the branch structure and the XML graph attribute strings (`"crossnic"`, `"speedintra"`, `"speedinter"`, `"typeintra"`, @`0x92d32`…). The inherited search treats NeuronCores as GPUs throughout; it has no knowledge of MLA/SEngine structure. That structural knowledge is exactly what the Neuron search in §3 adds.

---

## 3. The Neuron `topology`-Class Priority Search

### Purpose

The "new algorithm" is a from-scratch graph search that understands the Trainium fabric directly. Where the inherited DFS sees an undifferentiated GPU graph, the `topology` class (`graph/topo_neuron*.cc`) sees a `NeuronNode`/`NeuronEdge` adjacency graph in which every edge carries a **locality** (`EdgeSharedMemory` < `EdgeRemoteMLA` < `EdgeD2D` < `EdgeRMTV` < `EdgeLocal`) and a mutable **priority**. The search (`topology::getPaths` @`0x5e400`) is a priority-ordered DFS that prefers cheap-locality edges, raises priority on edges it has committed to, and refuses to close a cycle prematurely — producing device-level paths that are then expanded to per-vcore core paths and re-encoded as `<channel>` XML. The result is parsed back into the *same* `ncclTopoGraph` the inherited search would have produced, so everything downstream is identical.

### Entry Point

```text
neuronTopoGetGraph(xml, nRanks, nNodes, comm)   @0x625c0   ── PLATFORM DISPATCH
  ├─ neuronGetInstanceInfo(&instInfo)            @0x46100   ── nrt_get_instance_info
  ├─ neuronIsTrn3SwitchV1Family() ─yes→ neuronTopoCalculateGraphTRN3SwitchV1(xml, comm)  @0x797b0
  ├─ neuronIsTRN3Family()         ─yes→ neuronTopoCalculateGraphTRN3(xml, …, comm)       @0x78370
  ├─ neuronIsTRN2Family()         ─yes→ neuronTopoCalculateGraphTRN2(xml, …, comm)       @0x75910
  └─ else                              → neuronTopoGetGraphTrn1(xml, …, sys)             @0x74530
        each calculator:
          getGraphTable(table[*][128], sys, …)   @0x73f20   ── BRIDGE: ncclTopoSystem → bitset → request
            └─ topology::getPaths(request)       @0x5e400   ── NEW priority search
                 ├─ constructNodes(avail, nodes, nodesPerMLA)   @0x5cf90 (build NeuronNode graph)
                 ├─ topology::findPaths(avail, request, …)      @0x5d580
                 │     └─ findPath → findPathRec (priority DFS)  @0x5cc00 / 0x5c4e0
                 │           · NeuronEdge::isIntraMLA / tryIncreasePriority / decreasePriority
                 │           · topology::isClosingCycle(…)       @0x5b2a0
                 ├─ getSingleVCorePerDevicePath(request)         @0x5dd40
                 └─ convertDevicePathToCorePath(request, paths)  @0x5d310
          → xmlAddChannel<…> / channelAddNic emit <channel> XML   (see §4)
          → ncclTopoGetGraphFromXml parses XML back into ncclTopoGraph
```

The dispatch ladder is read directly from `neuronTopoGetGraph`'s disassembly: at `0x625f8` it calls `neuronGetInstanceInfo`, then `0x62650` `call neuronIsTrn3SwitchV1Family` → `0x6265f` `call neuronTopoCalculateGraphTRN3SwitchV1`, `0x62670` `call neuronIsTRN3Family` → TRN3, `0x62679` `call neuronIsTRN2Family` → TRN2, falling through to the Trn1 path.

### Algorithm

The search is a priority DFS with a locality-ordered frontier. `paths_request` carries the available vcore bitset and the fabric geometry (`ncPerSengine`, `senginePerMLA`, `ncPerVcore`); `constructNodes` materializes the `NeuronNode` graph whose edges are grouped by locality in `NeuronNode::EdgeSet` (shared-memory / d2d / rmtv / remote). `findPathRec` walks it, raising the priority of edges it commits to so subsequent passes reuse them, and rejecting an edge that would close the cycle before every required node is visited.

```c
// topology::getPaths — 0x5e400  [HIGH entry; MED on inner ordering]
PathContainer topology::getPaths(const paths_request &req):
    vector<NeuronNode> nodes;
    constructNodes(req.available_vcores, nodes, req.senginePerMLA);   // 0x5cf90
    topology topo(nodes, req.available_vcores, req.senginePerMLA);    // 0x5b2d0 ctor
    bitset<128> visited;
    bool ok;
    auto devicePaths = topo.findPaths(visited, req, /*start*/0, req.max, ok);  // 0x5d580
    // expand device-level paths to per-vcore core paths:
    if (single_vcore_per_device)
        return topo.getSingleVCorePerDevicePath(req);                 // 0x5dd40
    else
        topo.convertDevicePathToCorePath(req, devicePaths);           // 0x5d310
    return devicePaths;

// topology::findPathRec — 0x5c4e0  [MED: locality ordering from decomp + enum]
void topology::findPathRec(vector<vector<int>> &out, bitset<128> &visited,
        vector<int> &current, int node, int target, int &found,
        bool backward, int depth, int maxDepth, int priorityFloor, bool closeAllowed):
    if (node.localsFullyVisited(current, depth)):     // 0x5c3e0 — all intra-MLA peers used
        return;
    for each NeuronEdge e in node.edges  // edges pre-sorted by (locality, priority)
                                         // EdgeSharedMemory(0) < EdgeRemoteMLA(1)
                                         // < EdgeD2D(2) < EdgeRMTV(3) < EdgeLocal(4):
        if (visited[e.target] && !(closeAllowed && e.target == start)):
            continue;
        if (isClosingCycle(e.target, current, e)):    // 0x5b2a0 — would close ring early?
            if (depth < maxDepth) continue;           //   reject premature closure
        e.tryIncreasePriority(e.isIntraMLA(), priorityFloor);   // 0x5c460 — commit cost up
        visited.set(e.target); current.push_back(e.target);
        findPathRec(out, visited, current, e.target, target, found, …);  // recurse
        visited.reset(e.target); current.pop_back();
        e.decreasePriority(e.isIntraMLA());           // 0x5c4a0 — backtrack: cost down

// getGraphTable — 0x73f20  [HIGH: the sys→request bridge]
int getGraphTable(int table[*][128], const ncclTopoSystem *sys, int trnVer, int *n, int a, int b):
    paths_request req;
    req.available_vcores = systemDeviceBitset(sys, …);   // 0x6df30 — which vcores present
    req.trn_version = trnVer;
    req.senginePerMLA = platform->get_sengine_num_per_mla();  // NeuronPlatform vtable
    req.ncPerVcore    = nec_get_virtual_core_size();          // RT callback
    PathContainer paths = topology::getPaths(req);            // 0x5e400
    encode_paths_into(table);                                  // device/core path rows
    return n_paths;
```

> **QUIRK — edges are re-priced as the search runs.** `NeuronEdge::tryIncreasePriority` (@`0x5c460`) and `decreasePriority` (@`0x5c4a0`) mutate an edge's `priority` field (offset +8 of the 24-byte `NeuronEdge`) *during* the DFS: committing to an edge raises its priority so a later pass over the same node revisits it preferentially, and backtracking lowers it. This is not Dijkstra — there is no fixed edge weight. The frontier order is `(locality, current priority)`, and `topology::maxPriority` (offset +0x44 of the 72-byte `topology`) tracks the ceiling. A reimplementer who models edges with static weights will not reproduce the path set; the priority is state, evolved by the traversal, and the locality enum order (`EdgeSharedMemory=0 … EdgeLocal=4`) is the tiebreak. (The exact `isClosingCycle` predicate and the priority-floor arithmetic are read from decompiled control flow — **MED**.)

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `neuronTopoGetGraph` | `0x625c0` | **fork** — platform dispatch (family-predicate ladder) | HIGH |
| `neuronTopoCalculateGraphTRN3SwitchV1` | `0x797b0` | switch-pod fabric graph (jbog/rack math; 0x2454, largest) | HIGH |
| `neuronTopoCalculateGraphTRN3` | `0x78370` | TRN3 P2P-pod graph | HIGH |
| `neuronTopoCalculateGraphTRN2` | `0x75910` | TRN2 graph | HIGH |
| `neuronTopoGetGraphTrn1` | `0x74530` | TRN1 / INF2 graph | HIGH |
| `getGraphTable` | `0x73f20` | bridge `ncclTopoSystem` → `paths_request` → `getPaths` | HIGH |
| `systemDeviceBitset` | `0x6df30` | available-vcore `bitset<128>` from the system | HIGH |
| `topology::getPaths` | `0x5e400` | **fork** — new priority search entry (0x1851) | HIGH |
| `topology::findPaths` | `0x5d580` | per-target path enumeration | HIGH |
| `topology::findPath` / `findPathRec` | `0x5cc00` / `0x5c4e0` | priority DFS core | HIGH addr / MED body |
| `topology::isClosingCycle` | `0x5b2a0` | premature-ring-closure predicate | HIGH addr / MED body |
| `constructNodes` | `0x5cf90` | build the `NeuronNode` adjacency graph | HIGH |
| `getSingleVCorePerDevicePath` | `0x5dd40` | one-vcore-per-device path expansion | HIGH |
| `convertDevicePathToCorePath` | `0x5d310` | device path → per-vcore core path | HIGH |
| `neuronTopoUseNewAlgorithm` | `0x70850` | reads `NCCL_GRAPH_USE_NEW_ALGORITHM` (default OFF) | HIGH |
| `NeuronPlatform::get_sengine_num_per_mla` | `0x89c80` | virtual fabric-geometry accessor (TRN1/2/3 overrides) | HIGH |

### Considerations

Selection between the inherited DFS and this search is the one **MED** claim on the page. `ncclTopoCompute` (@`0x6ab30`) holds both call targets — `ncclTopoSearchRec` and the path that reaches `neuronTopoGetGraph` — and the predicate that chooses is read from decompiled control flow plus two anchors: the env gate `neuronTopoUseNewAlgorithm` (@`0x70850`, `"NCCL_GRAPH_USE_NEW_ALGORITHM set by environment"`, default OFF) and the per-platform family predicates. The switch-pod platform (`neuronIsTrn3SwitchV1Family` @`0x45da0`) routes to `neuronTopoCalculateGraphTRN3SwitchV1` (@`0x797b0`) regardless of the env knob, because its jbog/rack fabric has no inherited-DFS expression; the `topology` class is the only path that can describe it. The fabric geometry the search reads (`get_sengine_num_per_mla` and the `sengine_get_{remote,d2d,rmtv}_peer_nums` family, vtables `NeuronPlatform` @`0xa7bf8`, TRN3 `0xa7c38`, TRN2 `0xa7c78`, TRN1 `0xa7cb8`) is the per-platform virtual layer; a reimplementer must supply one `NeuronPlatform` subclass per Trainium generation, each returning that generation's SEngine-per-MLA count and per-locality peer counts. The `PathContainer duplicatePaths(…)` / `rotatePath(…)` helpers (signatures @`0x921a0`/`0x92210`) replicate and rotate a base path across NICs using `trn1_nic_assignment_t` records — the NIC-assignment math that §4's channel emission consumes.

---

## 4. Channel Assignment — Rank Wiring and the XML Bridge

### Purpose

Whichever search ran, channel assignment is the final stage: turn an `ncclTopoGraph` into per-rank ring/tree orders, AllGather them across ranks, stitch the per-rank fragments into global rings, decide how many P2P channels to materialize, and allocate one runtime `ncclChannel` per channel. This is `buildGraphTransportsRank`'s back half. The Neuron search reaches it through a detour the inherited search does not need: its device/core paths are first emitted as `<channel>` XML and parsed *back* into `ncclTopoGraph`, so `ncclTopoPreset`/`Postset` see one representation regardless of which search produced it.

### Entry Point

```text
buildGraphTransportsRank(comm, nNodes)   @0x24ef0   [master orchestrator]
  ├─ for each graph (ring/tree/…): ncclTopoCompute(comm, &graph, nNodes)   @0x6ab30   (§2 / §3)
  │     [NEW path] → getGraphTable → topology::getPaths → device/core paths
  │        └─ xmlAddChannel<int const*>(xml, sys, ch, node, begin, end, …)  @0x6ef50   ── emit <channel>
  │           · channelAddNic(xml, sys, node, instInfo, …)                  @0x6e0b0   ── "Channel %d uses NICs …"
  │           · getGraphMax(xml, sys, instInfo, …)                          @0x6f4a0
  │        └─ ncclTopoGetGraphFromXml(xmlNode, sys, &graph, &n)             @0x693e0   ── <channel> → ncclTopoGraph
  │              └─ ncclTopoGetChannelFromXml(xmlNode, ch, sys, &graph)     @0x686c0
  ├─ ncclTopoPreset(comm, graphs, &allTopoRanks[rank])     @0x6c1f0   ── per-rank ring/tree → ncclTopoRanks
  ├─ bootstrap AllGather(allTopoRanks)                                ── exchange all ranks' wiring
  ├─ ncclTopoPostset(comm, nodesFirstRank, allTopoRanks, …) @0x6c490  ── global stitch; kangaring/mla-cycle
  ├─ ncclTransportP2pSetup(comm, &graph)                   @0x3eb80   ── p2pCanConnect / netNeuronCanConnect
  ├─ ncclTopoComputeP2pChannels(comm)                      @0x654f0   ── "Numbers of channels set to %d"
  └─ per channel: initChannel(comm, ch)   @0x33c30  +  ncclTransportP2pConnect(comm, ch, …)  @0x3e9c0
```

### Algorithm

`ncclTopoPreset` writes this rank's ring/tree neighbors into a 960-byte `ncclTopoRanks`; after the AllGather, `ncclTopoPostset` has every rank's fragment and stitches them into a global ordering, writing the final `ncclChannel.ring`/`tree`. The Neuron-added kangaring/mla-cycle stitching lives in `Postset` and is owned by [algorithm-ring](algorithm-ring.md); this page anchors the structure and the channel-count decision.

```c
// ncclTopoPreset — 0x6c1f0  [HIGH: ncclTopoRanks offsets byte-confirmed]
void ncclTopoPreset(ncclComm *comm, ncclTopoGraph *graphs, ncclTopoRanks *ranks):
    for each channel ch built by the search:
        // from graph->intra[ch] (this node's rank order) fill the per-rank wiring:
        ranks->ringPrev[ch] = prev_rank_in_intra_order;   // +0x100
        ranks->ringNext[ch] = next_rank_in_intra_order;   // +0x180
        ranks->ringRecv[ch] = recv_endpoint;              // +0x000
        ranks->ringSend[ch] = send_endpoint;              // +0x080
        ranks->treeToParent[ch] = …;                      // +0x200
        ranks->treeToChild0[ch] = …; treeToChild1[ch] = …;// +0x280 / +0x300
    // JbogKangaringPath[4][4] @+0x380 is filled later, in Postset [FORK]

// ncclTopoPostset — 0x6c490  [HIGH on strings; ring math MED → algorithm-ring]
ncclResult_t ncclTopoPostset(ncclComm *comm, int *nodesFirstRank,
        ncclTopoRanks **allRanks, int *nChannelsOut, int *nNodesOut):
    // allRanks[r] holds every rank's preset wiring after the AllGather
    for each channel ch:
        stitch ringPrev/ringNext fragments across ranks → global ring → comm->channels[ch].ring;
        stitch tree fragments → comm->channels[ch].tree;
    build_kangaring_paths(comm);   // "built %d kangaring paths" @0x8f11e  [FORK]
    build_mla_cycles(comm);        // "built %d mla cycles"      @0x8f137  [FORK]
                                   // → ncclTopoRanks.JbogKangaringPath[4][4]

// ncclTopoComputeP2pChannels — 0x654f0  [HIGH addr; param read MED]
ncclResult_t ncclTopoComputeP2pChannels(ncclComm *comm):
    n = clamp(graph.nChannels, ncclParamMinP2pNChannels(), ncclParamMaxP2pNChannels());
    comm->p2pnChannels = n;        // "Numbers of channels set to %d" @0x8f0ee
    for (ch = 0; ch < n; ch++) initChannel(comm, ch);   // 0x33c30 — allocate the 512B ncclChannel

// initChannel — 0x33c30  [HIGH]
ncclResult_t initChannel(ncclComm *comm, int channelId):
    ncclChannel *c = &comm->channels[channelId];        // 512B per channel
    c->id = channelId;                                  // +0x40
    c->peers    = calloc(nRanks * sizeof(ncclPeer));    // +0x48
    c->devPeers = …;                                    // +0x50 device mirror
    c->workFifo = alloc_work_fifo();                    // +0x58
    // ring (+0x00) / tree (+0x18) were written by Postset; here the runtime peer/fifo state is built
```

> **NOTE — the `<channel>`-XML round-trip is the Neuron path's pivot.** The inherited search writes `ncclTopoGraph` directly. The Neuron search produces `topology::getPaths` device/core paths, which are *serialized* into `<channel>` XML by `xmlAddChannel<int const*>` (@`0x6ef50`, plus a `reverse_iterator` overload @`0x6e9e0` for reversed rings) and `channelAddNic` (@`0x6e0b0`), then *parsed back* into `ncclTopoGraph` by `ncclTopoGetGraphFromXml` (@`0x693e0`) → `ncclTopoGetChannelFromXml` (@`0x686c0`). The round-trip exists so the Neuron search can reuse the inherited XML schema (`"crossnic"`, `"nchannels"`, `"speedintra"`, …) and feed `ncclTopoPreset`/`Postset` unchanged. The XML version is guarded: `"XML Graph has wrong version %d, %d needed"` (@`0x96e50`). `channelAddNic` is where NIC assignment lands — each channel records which NICs it uses, derived from the `trn1_nic_assignment_t` records the path layer (`duplicatePaths`/`rotatePath`) produced.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `buildGraphTransportsRank` | `0x24ef0` | master: search → preset → AllGather → postset → P2P → channels | HIGH |
| `ncclTopoPreset` | `0x6c1f0` | per-rank ring/tree wiring into `ncclTopoRanks` | HIGH |
| `ncclTopoPostset` | `0x6c490` | global ring/tree stitch; kangaring/mla-cycle (**fork**) | HIGH addr / MED math |
| `ncclTopoComputeP2pChannels` | `0x654f0` | clamp channel count; loop `initChannel` | HIGH |
| `initChannel` | `0x33c30` | allocate the 512 B runtime `ncclChannel` | HIGH |
| `freeChannel` | `0x34180` | channel teardown | HIGH |
| `getGraphTable` | `0x73f20` | bridge into `topology::getPaths` (also §3) | HIGH |
| `xmlAddChannel<int const*>` | `0x6ef50` | serialize a path as a `<channel>` XML node | HIGH |
| `xmlAddChannel<reverse_iterator>` | `0x6e9e0` | reversed-ring `<channel>` emit | HIGH |
| `channelAddNic` | `0x6e0b0` | attach NIC assignment to a `<channel>` | HIGH |
| `getGraphMax` | `0x6f4a0` | per-graph max-channel computation | HIGH |
| `ncclTopoGetGraphFromXml` | `0x693e0` | parse `<channel>` XML back into `ncclTopoGraph` | HIGH |
| `ncclTopoGetChannelFromXml` | `0x686c0` | parse one `<channel>` node | HIGH |
| `ncclTopoGetXmlFromChannel` | `0x69610` | inverse: `ncclTopoGraph` channel → XML | HIGH |
| `neuronTopoGetP2pPodGraph` | `0x62740` | inter-node pod graph (P2P/SWITCH pod) | HIGH |

### Considerations

`ncclTopoComputeP2pChannels` clamps the channel count to `[ncclParamMinP2pNChannels, ncclParamMaxP2pNChannels]`; the `"Numbers of channels set to %d"` string (@`0x8f0ee`) is attributed to it by address proximity and decomp, not a symbol-anchored xref (**MED**). The inter-node leg adds a second graph: `neuronTopoGetP2pPodGraph` (@`0x62740`, thin wrapper `ncclTopoGetP2pPodInterNodeGraph` @`0x6c030`) builds the pod-level ordering for multi-instance workloads, keyed on `nec_pod_type` (`NONE`/`P2P`/`SWITCH`), and is where `"Discovered a new Pod …"` (@`0x8ba90`) and `"POD is enabled …"` (@`0x8bba8`) are logged; it reaches into the RT via `nec_get_p2p_pod_peer_node` / `nec_pod_node_can_access_peer_node`. The two Neuron-added struct fields are the reimplementer's tell that this is forked: `ncclTopoGraph.crossracktorack` at +`0x812C` (stock NCCL has no such field — the struct ends at `inter[64]`) and `ncclTopoRanks.JbogKangaringPath[4][4]` at +`0x380`. Both are written by the Neuron stitching in `Postset` and consumed by the ring algorithm; a reimplementer that uses the stock struct layouts will mis-place every field after the graph's `inter[64]`.

---

## Related Components

| Name | Relationship |
|---|---|
| inherited `graph/{topo,xml,paths,search}.cc`, `channel.cc` | reusable as-is from public NCCL `2.31.24`; consume `ncclXml`/`ncclTopoSystem`/`ncclTopoGraph` unchanged |
| **fork** `graph/topo_neuron{,_sunda,_cayman,_mariana}.cc` | the synthesizers + `topology`/`NeuronNode`/`NeuronEdge` + `NeuronPlatform` hierarchy — where the RE work lives |
| `initTransportsRank` (comm-init) | calls §1 (discovery) during init, before the graph build of §2–§4 |
| RT (`libnrt`, `@NRT_2.0.0`) | the discovery data source: `nrt_get_instance_info`, `nec_*` MLA/pod/device callbacks |

## Cross-References

- [Overview: the NCCL Fork (2.31.24+nrt2.0)](overview.md) — the Part-XII map; this page details its stages 5–6 (topology + graph/ring)
- [Communicator Init and Bootstrap](comm-init.md) — `initTransportsRank`, which enters §1 (system discovery) after the peer AllGather
- [Ring Algorithm Construction (Host-Side)](algorithm-ring.md) — `ncclTopoPostset`'s ring / `kangaRing` / `mla-cycle` stitching that §4 hands off to
- [Intra-Node Transport (P2P)](transport-intra.md) — the `p2pCanConnect` edges (`EdgeSharedMemory`/`EdgeD2D`/`EdgeRMTV`/`EdgeLocal`) the graph's intra-node legs become
- [back to index](../index.md)
