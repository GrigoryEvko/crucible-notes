# Tuning Model (the Dead Cost Model)

> *All addresses on this page apply to `libnccom.so.2.31.24` from `aws-neuronx-collectives 2.31.24.0-1a31ba186` (build-id `9c00176c081788c9435d27d11bb40e92495463f0`; SONAME `libnccom.so.2`; internal tree **KaenaNCCL**, `/opt/workspace/KaenaNCCL/src`). ELF64 x86-64, **DYN, NOT stripped, with `.debug_info`**; `.text`/`.rodata` VMA == file offset for the cited band, so every `0x…` there is both a file offset and an analysis VMA. **`.data` VMA = file offset + `0x1000`** (`.data` VMA `0xa86a0` == file off `0xa76a0`) — subtract `0x1000` from any `.data` VMA to read its bytes.*
> *Evidence grade: **Confirmed (byte-anchored, negative result)** — the absence of the upstream cost model is proven by empty `nm` over the full symbol table; the one live coefficient array (`speedArray[18]`) and the P2P divisor table are decoded from the binary's own bytes (`xxd`/`struct.unpack`); every cited function, every `ncclParam*` stub return, the comp-cap arms, and the dead-table zero-reference counts are read from `objdump -d`. Other versions will differ. · Part XII — Multi-Node Collectives · [back to index](../index.md)*

## Abstract

This page documents a **negative result**: the NCCL runtime cost model — the per-collective latency/bandwidth tables that upstream NVIDIA NCCL builds in `ncclTopoTuneModel` and consults at launch through `getAlgoInfo` / `ncclTopoGetAlgoTime` / `computeColl` to *choose* an algorithm (Ring vs Tree vs CollNet) and a protocol (Simple vs LL vs LL128) — **is not present in `libnccom.so`**. All four upstream symbols return nothing from `nm -C` over the full (1088-entry) symbol table, not just the dynamic table. There is no algorithm chooser and no protocol chooser in this library. The classic NCCL "tuning" stage has been compiled out; what is left is a single bandwidth-tier graph search that *sizes* one ring, plus a deterministic channel-count heuristic.

The reference frame is upstream NCCL's `enqueue.cc` + `graph/tuning.cc`. In stock NCCL, `ncclTopoTuneModel` populates `comm->latencies[5][2][3]` and `comm->bandwidths[5][2][3]` (5 functions × 2 algorithms × 3 protocols) and `comm->threadThresholds`; at every collective launch `getAlgoInfo` reads those tables, computes a model time per (algo, proto) via `ncclTopoGetAlgoTime`, and `computeColl` picks the minimum. **None of that machinery exists here.** The cost tables still *exist in the `ncclComm` struct* — they are DWARF-typed and ABI-retained at `comm+17904` (`latencies`), `comm+18024` (`bandwidths`), `comm+17856` (`threadThresholds`), `comm+18144` (`maxThreads`) — but they are **never written and never read**: a displacement sweep of `.text` finds **zero** references to each. They are dead ABI baggage. Algorithm is fixed at RING by construction ([algorithm-ring](algorithm-ring.md)); protocol is decided *outside* this library, in the Neuron compiler / NRT execution plan.

What *is* present, and what this page decodes byte-level, is a **single bandwidth-tier graph search** inside `ncclTopoCompute` (`0x6ab30`), driven by exactly one global coefficient array — `speedArray[18]` at `.data 0xae460` (file `0xad460`), the descending GB/s tier ladder `42,30,…,0.12`. The search seeds the highest tier `≤ topo->maxWidth`, asks `ncclTopoSearchRec` to realize a ring at that bandwidth, and walks *down* the table on failure; it sizes the one RING graph's `speedIntra`/`speedInter` and never compares algorithms or protocols. Alongside it, `ncclTopoComputeP2pChannels` (`0x654f0`) computes a channel *count* from a tiny per-arch link-width divisor table `{12.0, 21.0, 18.0}` (`.rodata 0x91dc4`), power-of-two rounding, and a hard clamp to 32. Finally, every NCCL tuning env-knob accessor (`ncclParam*`) is a **constant-return stub** that does not call `getenv`, so `NCCL_MIN/MAX_NCHANNELS`, `NCCL_NRINGS`, `NCCL_BUFFSIZE`, `NCCL_CROSS_NIC` and friends are inert; `NCCL_PROTO` / `NCCL_ALGO` / `NCCL_NTHREADS` have no string and no accessor at all. The page is organized as: (1) the **absence proof** — the empty-`nm` table plus the zero-reference cost-table evidence; (2) the **bandwidth-tier table** — `speedArray[18]` decoded from real bytes; (3) the **`ncclTopoCompute` search** as annotated pseudocode; (4) the **simplified algo / protocol / channel selection**, including the constant-stub env knobs.

For reimplementation, the contract of this page is **what NOT to build**:

- **Do not build a cost model.** There is no `ncclTopoTuneModel`, no `getAlgoInfo`, no `ncclTopoGetAlgoTime`, no `computeColl` (all four absent — §1). A reimplementer who ports NCCL's `graph/tuning.cc` and `enqueue.cc` cost path is adding a subsystem this library deliberately removed; nothing in `libnccom` would ever read its output.
- **Keep the cost-table struct slots, leave them dead.** `comm->latencies`(+17904)/`bandwidths`(+18024)/`threadThresholds`(+17856)/`maxThreads`(+18144) must exist for `ncclComm` ABI fidelity (the device receives a fixed 19240-byte comm), but they are never populated — zero `.text` references each. Do not fill them; nothing consults them.
- **Build one bandwidth search, not three.** The only live coefficient array is `speedArray[18]` (`0xae460`), read *only* by `ncclTopoCompute`. It sizes one RING graph's bandwidth; it does not select an algorithm. Reproduce the descending-tier descent (§3), seeded from `topo->maxWidth`, accepting when `nChannels × speedInter ≥ topo->totalWidth`.
- **The env knobs are inert; bake the sentinels.** All 18 `ncclParam*` accessors are constant-return stubs (`mov $-2` / `mov $0x20` / `xor eax,eax`). `ncclMinNchannels()`→0, `ncclMaxNchannels()`→32, `ncclParamMinP2pNChannels()`→1, `ncclParamMaxP2pNChannels()`→32 are the *only* live numeric clamps. The real `getenv` knobs are `NCCL_GRAPH_FILE`/`NCCL_GRAPH_USE_NEW_ALGORITHM`/`NCCL_TOPO_*`/`CCOM_*` plus the fork-new `NEURON_CCOM_SOCK_TIMEOUT` (default 120000).

| | |
|---|---|
| **Upstream cost model** | **ABSENT** — `ncclTopoTuneModel` / `getAlgoInfo` / `ncclTopoGetAlgoTime` / `computeColl`: 0 symbols (`nm -C`) |
| **Live bandwidth search** | `ncclTopoCompute` @`0x6ab30` — single RING pass; reads `speedArray[18]` only |
| **The one cost array** | `speedArray[18]` @ `.data 0xae460` (file `0xad460`) — 18 × float32, descending GB/s tiers |
| **`speedArray` readers** | **only** `ncclTopoCompute` — 5 xref sites `0x6ac66 / 0x6ae8c / 0x6b123 / 0x6b368 / 0x6b377` |
| **Channel-count heuristic** | `ncclTopoComputeP2pChannels` @`0x654f0` — divisor table `{12,21,18}`, pow2, clamp 32 |
| **P2P divisor table** | `{12.0, 21.0, 18.0}` @ `.rodata 0x91dc4` — comp-cap arms `==86 / [60,69] / else` |
| **Channel floor / ceiling** | `ncclMinNchannels()`→**0** @`0x6c470` · `ncclMaxNchannels()`→**32** (`mov $0x20`) @`0x6c480` |
| **Dead cost tables (ABI)** | `latencies`+17904 · `bandwidths`+18024 · `threadThresholds`+17856 · `maxThreads`+18144 — **0 refs each** |
| **Live buffer sizing** | `comm->buffSizes` @+17844 — **4** `.text` refs (proto buffers are sized; proto chooser absent) |
| **Env-knob accessors** | 18 × `ncclParam*` — **constant-return stubs**, never call `getenv` |
| **Algorithm decided** | **RING by construction** (not chosen) — [algorithm-ring](algorithm-ring.md) |
| **Protocol decided** | **outside this library** — Neuron compiler / NRT plan; `LL/LL128/Simple` here are debug labels |
| **Ring graph template** | `0x8bec0` — `pattern=4` (RING), `crossNic=2`, `collNet=0`, `minChannels=1`, `maxChannels=16` |

> **QUIRK — the tuning stage is removed three ways: no chooser, no model, no live knobs.** A reimplementer porting NCCL's tuning will expect (a) `ncclTopoTuneModel` filling per-(func,algo,proto) latency/bandwidth tables, (b) `getAlgoInfo`/`computeColl` reading them at launch to pick the cheapest (algo, proto), and (c) env knobs (`NCCL_ALGO`, `NCCL_PROTO`, `NCCL_MIN/MAX_NCHANNELS`) overriding the choice. **All three are gone.** (a) The four cost-model symbols are absent from the *entire* symbol table; the `latencies`/`bandwidths`/`threadThresholds`/`maxThreads` slots survive in `ncclComm` but have **zero** `.text` references. (b) There is no algorithm or protocol chooser — algorithm is fixed RING by the single `ncclTopoCompute` call (`0x24f79`), and protocol is encoded upstream in the NRT plan. (c) Every `ncclParam*` accessor is a constant-return stub that never reaches `getenv`, so the env knobs are inert (they return baked sentinels). The *only* surviving "cost" data is the single bandwidth-tier ladder `speedArray[18]`, which sizes one ring's bandwidth and nothing else. Document the search (§3) for completeness; build none of the chooser.

---

## 1. The Absence Proof — Empty `nm`, Zero-Reference Cost Tables

### Purpose

The central claim is that the NCCL runtime cost model — the subsystem that would *choose* an algorithm and protocol per collective — does not exist in this binary. "Removed" is a strong assertion that demands two independent proofs: that the *functions* are absent (no symbol the linker or a dispatcher could reach), and that the *data* those functions would fill is dead (present in the struct for ABI but never written or read). This part is both proofs, each anchored to a tool observation, contrasted against the data that *is* live (`speedArray`, `buffSizes`).

### Algorithm

The proof is a two-axis sweep: a symbol sweep over the full `nm -C` table for the cost-model functions, and a displacement sweep over `.text` for the cost-model struct fields. Each axis is independently sufficient; together they are conclusive.

```c
// absence sweep over libnccom.so.2.31.24  [HIGH: each row is a direct tool observation]

(1) cost-model FUNCTIONS — full symbol table (1088 symtab entries, NOT stripped):
      nm -C libnccom.so | rg 'ncclTopoTuneModel'   -> 0 matches    // the model builder: ABSENT
      nm -C libnccom.so | rg 'getAlgoInfo'         -> 0 matches    // the launch-time chooser: ABSENT
      nm -C libnccom.so | rg 'ncclTopoGetAlgoTime' -> 0 matches    // the per-(algo,proto) timer: ABSENT
      nm -C libnccom.so | rg 'computeColl'         -> 0 matches    // the collective cost calc: ABSENT
      nm -D -C libnccom.so | rg '<same four>'      -> 0 matches    // also absent from dynsym
      // broader: rg 'TuneModel|getAlgoInfo|GetAlgoTime|computeColl|EnqueueCheck' -> 0

(2) cost-model DATA — displacement sweep of .text for the ncclComm cost fields:
      threadThresholds @comm+17856 (0x45c0):  refs in .text -> 0   // DEAD ABI slot
      latencies        @comm+17904 (0x45f0):  refs in .text -> 0   // float[5][2][3], DEAD
      bandwidths       @comm+18024 (0x4668):  refs in .text -> 0   // float[5][2][3], DEAD
      maxThreads       @comm+18144 (0x46e0):  refs in .text -> 0   // DEAD ABI slot

(3) CONTROLS that DO resolve (so the sweep is real, not a scanning artifact):
      buffSizes @comm+17844 (0x45b4):         refs in .text -> 4   // LIVE — proto buffers ARE sized
      speedArray @.data 0xae460:              refs in .text -> 5   // LIVE — the one cost array (§2)
                                                                   //   all 5 inside ncclTopoCompute
```

The symbol axis is decisive because the binary is **not stripped**: `nm -C` reads the full 1088-entry `.symtab`, so a present-but-static cost-model function would still appear by name (exactly as the dead tree builders `ncclGetBtree`/`ncclGetDtree` do — see [algorithm-tree](algorithm-tree.md)). The four cost-model names appear in *no* table. The data axis closes the loophole that the tables might be filled by inlined code with no named function: a displacement sweep for `0x45c0`/`0x45f0`/`0x4668`/`0x46e0` off any register finds zero instructions touching those offsets, while the immediately-adjacent `buffSizes` (`0x45b4`) resolves to 4 — the sweep is sound, the silence is real.

### Function Map

| Upstream symbol | Role in stock NCCL | `nm -C` result | Confidence |
|---|---|---|---|
| `ncclTopoTuneModel` | build `latencies`/`bandwidths`/`threadThresholds` per (func, algo, proto) | **0 matches — ABSENT** | HIGH |
| `getAlgoInfo` | per-launch chooser: read tables → pick (algo, proto) | **0 matches — ABSENT** | HIGH |
| `ncclTopoGetAlgoTime` | model time for one (algo, proto) candidate | **0 matches — ABSENT** | HIGH |
| `computeColl` | collective cost calc / min selection | **0 matches — ABSENT** | HIGH |
| `ncclTopoCompute` | the **one** surviving search (sizes a ring, no algo compare) | `0x6ab30` — LIVE | HIGH |
| `ncclTopoComputeP2pChannels` | channel-count heuristic (no cost model) | `0x654f0` — LIVE | HIGH |

| `ncclComm` cost field | Offset | Type | `.text` refs | Liveness |
|---|---|---|---|---|
| `threadThresholds` | +17856 (`0x45c0`) | (thread thresholds) | **0** | **DEAD** (ABI-retained) |
| `latencies` | +17904 (`0x45f0`) | `float[5][2][3]` (120 B) | **0** | **DEAD** (ABI-retained) |
| `bandwidths` | +18024 (`0x4668`) | `float[5][2][3]` (120 B) | **0** | **DEAD** (ABI-retained) |
| `maxThreads` | +18144 (`0x46e0`) | (max threads) | **0** | **DEAD** (ABI-retained) |
| `buffSizes` | +17844 (`0x45b4`) | (proto buffer sizes) | **4** | **LIVE** (proto buffers sized) |

> **CORRECTION (TUNE-1) — the `latencies`/`bandwidths` slots exist; the *model that fills them* does not.** It is tempting, seeing `float[5][2][3]` in the `ncclComm` DWARF, to conclude the cost model is present. It is not. The struct shape (5 functions × 2 algorithms × 3 protocols) is inherited verbatim from upstream NCCL — the same `5` matches the five op names `Broadcast/Reduce/AllGather/ReduceScatter/AllReduce` in the debug table at `0x8be73` — but the **writer** (`ncclTopoTuneModel`) and the **readers** (`getAlgoInfo`/`computeColl`) are absent (above), and a `.text` displacement sweep finds **zero** instructions touching `comm+17904`/`comm+18024`. The slots are dead ABI baggage: they exist so `ncclComm` is layout-compatible with stock NCCL (the device receives a fixed 19240-byte comm, `calloc(0x4B28)` @`0x2c025`), but no code path writes a latency or reads a bandwidth. A reimplementer must keep the slots for ABI and leave them zero.

### Considerations

The contrast with `buffSizes` (+17844, 4 refs) is the subtle but important nuance: **proto buffers are still sized, but the proto *chooser* is gone.** Upstream NCCL both (a) computes the staging-buffer sizes for the LL / LL128 / Simple protocols and (b) chooses, per message, which protocol to run. Here (a) survives — `buffSizes` is live, the four references being the `computeBuffSizes` path that allocates the proto staging buffers — but (b) is removed: nothing selects LL vs LL128 vs Simple. The protocol that *will* run is fixed by the Neuron compiler and carried in the NRT execution plan ([send-recv-prims](send-recv-prims.md)); this library only sizes the buffers that plan will use. So the `LL\0LL128\0Simple\0Tree\0Ring\0` literals at `0x8be53` are not a selector table — they are index→name strings consumed solely by `ncclTopoPrintGraph`'s debug format `"Pattern %d, crossNic %d, nChannels %d, speed %f/%f, type %s/%s, sameChannels %d"` (`0x92bc0`). No instruction uses them to *choose* anything.

---

## 2. The Bandwidth-Tier Table — `speedArray[18]` Decoded

### Purpose

There is exactly one live "cost" coefficient array in the library, and it is not a latency/bandwidth *matrix* — it is a one-dimensional descending ladder of bandwidth tiers, `speedArray[18]`. It is the only data the surviving search consults. A reimplementer must reproduce it byte-for-byte (the search descent in §3 indexes it linearly), so this part decodes it verbatim from the binary's own bytes and pins its sole reader.

### Data Tables

The array lives in `.data` at VMA `0xae460`; with the `.data` delta its bytes are at file offset `0xad460`. It is 18 contiguous little-endian float32 (72 bytes), decoded here directly from the binary:

```text
speedArray[18]  —  .data VMA 0xae460  (file off 0xad460, delta 0x1000)  —  18 × float32, 72 B
  file 0xad460:  0000 2842   0000 f041   0000 c041   0000 a841   =  42.0  30.0  24.0  21.0
  file 0xad470:  0000 9041   0000 7041   0000 4041   0000 2041   =  18.0  15.0  12.0  10.0
  file 0xad480:  0000 1041   0000 e040   0000 c040   0000 a040   =   9.0   7.0   6.0   5.0
  file 0xad490:  0000 8040   0000 4040   9a99 1940   9a99 993f   =   4.0   3.0   2.4   1.2
  file 0xad4a0:  8fc2 753e   8fc2 f53d                           =  0.24  0.12

  idx :  0    1    2    3    4    5    6    7    8    9   10   11   12   13    14    15    16     17
  GB/s: 42   30   24   21   18   15   12   10    9    7    6    5    4    3   2.4   1.2  0.24   0.12
```

The descent is strictly monotone, spanning intra-node fast-link speeds (42 GB/s top) down to near-zero (0.12 GB/s floor), so the search can always find *some* tier a ring fits at. This is the upstream NCCL "graph speed search" coefficient family (the `sm90SpeedArray`/`speedArrayIntra` lineage), carried over unchanged in values; what changed is the *consumer* — upstream uses it inside a search that also feeds the cost model, here it is the only cost data and feeds only the ring search.

The five reader sites are all inside `ncclTopoCompute` (`0x6ab30`, which ends where `ncclTopoPrintGraph` begins at `0x6b9f0`); no other function references the array:

| Reader site | Form | Role |
|---|---|---|
| `0x6ac66` | `movss 0x437f2(%rip),%xmm0  # ae460` | first if-ladder: pick tier `≤ topo->maxWidth` (start index) |
| `0x6ae8c` | `lea 0x435cd(%rip),%rax  # ae460` | array base for the step-down walk (pass 1) |
| `0x6b123` | `movss 0x43335(%rip),%xmm0  # ae460` | second if-ladder: re-seed from `graph->speedInter` (pass 2) |
| `0x6b368` | `lea 0x430f1(%rip),%rax  # ae460` | array base for the step-down walk (pass 2) |
| `0x6b377` | `lea 0x430e2(%rip),%rax  # ae460` | array base (fallback re-seed) |

> **NOTE — `speedArray` sizes a ring; it does not rank algorithms.** The array looks like a cost coefficient set, and in upstream NCCL it participates in one, but here its *only* use is to seed and step the bandwidth target of a single RING graph. The two if-ladders (each 18 `comiss` comparisons, `0x6ac66…0x6ad8f` and `0x6b123…0x6b26e`) compare every tier against a single `xmm1` operand (`topo->maxWidth` on pass 1, `graph->speedInter` on pass 2) and select the first tier `≤` it — there is no second array for a tree or a protocol to compare against. A reimplementer must not infer a multi-algorithm cost model from this array's presence; it is one-dimensional bandwidth descent, nothing more.

### Considerations

The unrolled if-ladder (one `movss speedArray[idx]` + `comiss` per index, all 18 inlined rather than a loop) is why the array's reads are *contiguous* in the disassembly (`speedArray+0x0`, `+0x4`, … `+0x44`) and why exactly two such ladders appear: one seeded from `topo->maxWidth` (the initial tier), one from `graph->speedInter` (the second pass). The `lea` sites (`0x6ae8c`, `0x6b368`, `0x6b377`) take the array *base* for the runtime step-down walk (`++idx` until the ring fits or `idx==17`), which a fixed unroll cannot express. The two seeds are the whole "model": maxWidth bounds the fastest tier worth attempting; speedInter re-seeds for the inter-node leg. Neither is *measured* against a latency table — they are read from the topology's link widths ([topology](topology.md) §1, `ncclTopoComputePaths`). The values being inherited NCCL constants (not Neuron-tuned) is itself evidence the tuning stage was removed rather than re-tuned: a re-tuned fork would carry Trainium-specific tiers, not the stock GPU ladder.

---

## 3. The Bandwidth-Tier Graph Search — `ncclTopoCompute`

### Purpose

`ncclTopoCompute` (`0x6ab30`) is the one function that consumes `speedArray`, and it is the entire surviving "tuning" logic: it sizes the single RING graph's bandwidth by descending the tier ladder until a ring fits, then doubles channels up to the cap. It is called **exactly once** per comm (`0x24f79`, in `buildGraphTransportsRank`) with the RING template — not three times for ring/tree/collNet as upstream. There is no `ncclTopoTuneModel` after it and no `getAlgoInfo` before any launch; this search is the terminus of all bandwidth/channel decisions in the library.

### Entry Point

```text
buildGraphTransportsRank @0x24ef0
  └─ ncclTopoCompute(comm, &ringGraph, nNodes)   @0x24f79  ── ONE call; ringGraph.pattern = 4 (RING)
       ├─ if (graph->nChannels > 0) return       ── Neuron getPaths / NCCL_GRAPH_FILE XML already filled it
       ├─ getenv("NCCL_GRAPH_FILE")              @0x6abe7  (string @0x92e1b)
       │     └─ taken if set OR ncclRtName() != "Neuron Runtime"  (strcmp vs 0x8f441)
       │           → ncclTopoGetXmlGraphFromFile   (operator-supplied graph)
       │     else  → neuronTopoGetGraph             (synthesized Neuron paths — topology §3)
       ├─ ncclTopoSearchInit(sys)                @0x66110
       ├─ ncclTopoSearchParams(sys, pattern=4, &min, &max)  @0x669c0   ── ring channel bounds
       └─ tier descent over speedArray[18]:  ncclTopoSearchRec(...)  @0x66a20  (recursive ring search)
            *** NO ncclTopoTuneModel / getAlgoInfo / computeColl anywhere in or after this ***
```

### Algorithm

The search is a bandwidth descent, not an algorithm comparison. It seeds the highest tier the topology can sustain, tries to lay a ring at that bandwidth, and on failure steps *down* the tier ladder — toggling secondary knobs (`sameChannels`, `typeIntra/Inter`, `crossNic`, `pattern 2→3`) along the way — until the ring fits or the ladder is exhausted. A second pass re-seeds from the inter-node speed. The structure is read from the two if-ladders and the step-down loop; the relaxation order is read from the branch structure (**MED** on the exact knob order).

```c
// ncclTopoCompute — 0x6ab30  [HIGH on structure + speedArray seeds; relaxation order MED]
// the ENTIRE surviving "tuning model": sizes ONE ring's bandwidth, picks NOTHING else
ncclResult_t ncclTopoCompute(ncclComm *comm, ncclTopoGraph *graph, int nNodes):
    ncclTopoSystem *topo = comm->topo;

    // (0) fast exit: if a graph was already produced (Neuron getPaths / NCCL_GRAPH_FILE XML),
    //     its channels are filled and there is nothing to search.
    if (graph->nChannels > 0)
        return ncclSuccess;

    // (1) operator / runtime escape hatch — the only getenv in this function:
    const char *gf = getenv("NCCL_GRAPH_FILE");                 // 0x6abe7 — str @0x92e1b
    if (gf != NULL || strcmp(ncclRtName(), "Neuron Runtime") != 0):   // 0x6abef; vs @0x8f441
        return ncclTopoGetXmlGraphFromFile(gf, graph);          // load a hand-written / test graph
    // else the Neuron path (neuronTopoGetGraph) already filled graph; we only reach the
    // inherited search below in the bring-up / no-getPaths case.

    ncclTopoSearchInit(topo);                                    // 0x66110
    ncclTopoSearchParams(topo, graph->pattern /*=4 RING*/, &graph->minChannels, &graph->maxChannels);

    // (2) SEED the starting bandwidth tier: first speedArray idx whose value <= topo->maxWidth.
    //     The 18-way if-ladder 0x6ac66..0x6ad8f compares each tier (comiss) against maxWidth (xmm1).
    int v14 = first_idx_with(speedArray, /*<=*/ topo->maxWidth); // start tier (descending table)
    ncclTopoGraph tmp = *graph;
    tmp.speedIntra = speedArray[v14];                            // seed intra-node ring bandwidth

    // (3) DESCEND: try to realize a ring at the current tier; on failure step down the ladder.
    int budget = 0x180000;                                      // 0x6adb8 — outer search budget (1,572,864)
    for (int v16 = v14; v16 <= 17; v16++):                      // walk DOWN speedArray to idx 17
        int step = tmp.sameChannels ? 0x400 : 0x40000;          // 0x6aeb5 / 0x6add0 — per-pass step
        ncclTopoSearchRec(topo, graph, &tmp, &budget);          // 0x66a20 — recursive ring layout
        // accept when the ring's aggregate bandwidth meets the topology's demand:
        if (tmp.nChannels * tmp.speedInter >= topo->totalWidth):
            break;                                              // ring fits at this tier
        // else relax ONE secondary knob and step the tier down:
        tmp.speedIntra = speedArray[v16 + 1];                   // next-lower tier (while ratio > 0.49)
        toggle_one_of(tmp.sameChannels, tmp.typeIntra/*1..6*/, tmp.crossNic);
        if (tmp.pattern == 2) tmp.pattern = 3;                  // SPLIT_TREE -> TREE (degenerate only)
        budget -= step;

    // (4) SECOND PASS: re-seed from the inter-node speed if it dominates intra-node.
    if (tmp.speedInter + tmp.speedInter > tmp.speedIntra):       // 0x6b123 ladder re-seed
        int v_inter = first_idx_with(speedArray, /*<=*/ tmp.speedInter);
        repeat the descent (3) seeded from speedArray[v_inter];

    // (5) FINALIZE: if a ring was found, double channels up to maxChannels while speed stays high,
    //     rescaling speed by the channel multiplier; else fall back to a 1-channel identity ring.
    if (ring_found):
        while (tmp.nChannels < tmp.maxChannels && tmp.speedIntra >= 25.0):   // 0x6ac76 comiss vs 25.0
            tmp.nChannels *= 2;
            rescale(tmp.speedIntra, tmp.speedInter, ceil(maxCh / nCh));
    else:
        tmp.nChannels = 1; tmp.typeIntra = tmp.typeInter = 6;   // degenerate identity ring
    *graph = tmp;
    return ncclSuccess;
    //  *** none of this reads comm->latencies[]/bandwidths[]; it sizes the single RING graph ***
```

> **GOTCHA — this search sizes bandwidth, it does not choose an algorithm or a protocol.** A reader who knows upstream NCCL will recognize the speed-tier descent and assume it is one arm of a multi-graph search whose results a cost model then ranks. **It is not.** `ncclTopoCompute` is called *once* (`0x24f79`), always with `pattern=4` (RING); there is no tree pass, no collNet pass, and no `ncclTopoTuneModel` after it to compare the result against anything. The `pattern 2→3` toggle inside the loop is a *topology-search* normalization (SPLIT_TREE→TREE for the single-GPU degenerate case), not a collective-algorithm choice. The output is one RING `ncclTopoGraph` with a sized `speedIntra`/`speedInter`/`nChannels`; the *algorithm* was already RING before the search began, and the *protocol* is decided outside this library. A reimplementer who wires this search's output into an algorithm chooser has built a chooser nothing feeds.

### Function Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ncclTopoCompute` | `0x6ab30` | the one bandwidth-tier search; reads `speedArray`; sizes one RING graph | HIGH |
| `ncclTopoSearchInit` | `0x66110` | seed search/replay state | HIGH |
| `ncclTopoSearchParams` | `0x669c0` | per-pattern (RING) min/max channel bounds; no bandwidth cost | HIGH |
| `ncclTopoSearchRec` | `0x66a20` | recursive ring layout at the seeded bandwidth | HIGH |
| `ncclTopoTuneModel` / `getAlgoInfo` / `ncclTopoGetAlgoTime` / `computeColl` | — | **ABSENT** — no cost model after the search | HIGH |

### Considerations

The two byte-anchored constants pin the search shape. The double-up speed floor is **25.0** (`0x41c80000`), read at `comiss` site `0x6ac76`: channels are doubled only while `speedIntra ≥ 25.0`, so the channel multiplier never pushes the per-channel bandwidth below a quarter of the top tier. The search budgets are `0x180000` (1,572,864, the outer budget at `0x6adb8`), stepped by `0x400` when `sameChannels` is set (`0x6aeb5`) or `0x40000` otherwise (`0x6add0`/`0x6b678`) — the same dual-step the inherited `graph/search.cc` uses. The `getenv("NCCL_GRAPH_FILE")` gate (`0x6abe7`) plus the `ncclRtName() != "Neuron Runtime"` strcmp (`0x6abef`, vs `0x8f441`) is the operator/test escape: an explicitly supplied graph XML, or any runtime that is not the production Neuron Runtime, routes to `ncclTopoGetXmlGraphFromFile` and skips the search entirely. In production the Neuron `topology::getPaths` path ([topology](topology.md) §3) usually fills `graph->nChannels > 0` before `ncclTopoCompute` runs, so the `speedArray` descent is the *bring-up / fallback* path; either way no cost model follows it. The exact relaxation order (which of `sameChannels`/`typeIntra`/`crossNic` loosens first) is read from the branch structure and is **MED** — but the tier descent and the accept predicate (`nChannels × speedInter ≥ totalWidth`) are byte-anchored.

---

## 4. The Simplified Algo / Protocol / Channel Selection

### Purpose

With the cost model gone, "selection" in `libnccom` collapses to three deterministic facts: algorithm is RING by construction, protocol is decided outside the library, and the only real *numeric* decision is the channel count. This part documents each, plus the constant-stub env knobs — so a reimplementer knows exactly which decisions are made here (channel count) and which are made elsewhere (algorithm: construction; protocol: NRT plan).

### Algorithm

**Algorithm — fixed RING.** `buildGraphTransportsRank` builds one graph from the RING template (`0x8bec0`, `pattern=4`) and calls `ncclTopoCompute` once. No tree or collNet graph is constructed; `ncclGetBtree`/`ncclGetDtree` have zero callers ([algorithm-tree](algorithm-tree.md)). The in-search `pattern` toggles are topology normalizations, not collective choices.

**Protocol — decided outside.** No `getAlgoInfo`; no protocol chooser. `LL`/`LL128`/`Simple` are debug labels (`0x8be53`). `buffSizes` (+17844, 4 refs) sizes the proto staging buffers, but the per-message protocol is encoded by the Neuron compiler in the NRT execution plan.

**Channel count — the one live numeric decision**, in `ncclTopoComputeP2pChannels` (`0x654f0`), driven by the divisor table `{12.0, 21.0, 18.0}` (`.rodata 0x91dc4`):

```c
// ncclTopoComputeP2pChannels — 0x654f0  [HIGH: divisor table + comp-cap arms + clamp byte-anchored]
ncclResult_t ncclTopoComputeP2pChannels(ncclComm *comm):
    int nCh = comm->nChannels;                              // from the ring search (§3)
    if (nCh > 32):                                          // 0x6550f cmp $0x20
        comm->p2pnChannels = 32;                            // 0x656b5 movl $0x20,0x452c(%rdi)
        return ncclSuccess;
    int v5 = (nCh > 0) ? nCh : 1;

    int v8 = INT_MAX;
    for each local NeuronCore, for each peer with a type-1 path:
        int compCap = peer.cudaCompCap;                    // inherited NCCL field (Trainium gen tag)
        float div;
        if (compCap == 86):              div = 12.0;        // 0x6567c cmp $0x56 -> divisor[0] @0x91dc4
        else if ((compCap - 60) <= 9):   div = 18.0;        // 0x65681 sub $0x3c; 0x6568c cmp $0x9 -> divisor[2] @0x91dcc
        else:                            div = 21.0;        //                                       -> divisor[1] @0x91dc8
        int cand = 2 * max(1, (int)(path.width / div));     // channels for this path, ×2
        v8 = min(v8, cand);
    v8 = min(v8, v5);

    comm->p2pnChannelsPerPeer = nextPow2(v8);              // >= 1, round UP to power of two
    comm->p2pnChannels        = nextPow2(v5);              // >= 1, "Numbers of channels set to %d"
    // butterfly order: p2pChannels[i] = bit-reversal permutation of i over p2pnChannels
    for (int i = 0; i < comm->p2pnChannels; i++):
        comm->p2pChannels[i] = bit_reverse(i, comm->p2pnChannels);
    // hard outer clamps: ncclMinNchannels()=0, ncclMaxNchannels()=32 (constant stubs)
    return ncclSuccess;
```

### Function Map — the env-knob stubs (all constant-return, never call `getenv`)

The 18 `ncclParam*` accessors that upstream NCCL implements as `getenv`-backed tunables are here **constant-return stubs** — each disassembles to a single `mov imm; ret`, so the env knobs they would read are inert. The sentinel `-2` (`0xfffffffffffffffe`) is upstream NCCL's "unset" value; a stub returning it means "no override, use the built-in default."

| Accessor | Addr | Returns | Disasm | Env knob — status |
|---|---|---|---|---|
| `ncclMinNchannels` | `0x6c470` | **0** | `xor %eax,%eax` | hard floor (live clamp) |
| `ncclMaxNchannels` | `0x6c480` | **32** | `mov $0x20,%eax` | hard ceiling (live clamp) |
| `ncclParamMinP2pNChannels` | `0x654d0` | **1** | `mov $0x1,%eax` | live clamp |
| `ncclParamMaxP2pNChannels` | `0x654e0` | **32** | `mov $0x20,%eax` | live clamp |
| `ncclParamMinNrings` / `MaxNrings` | `0x6c430` / `0x6c440` | **−2** | `mov $-2` | `NCCL_MIN/MAX_NRINGS` — **inert** |
| `ncclParamMinNchannels` / `MaxNchannels` | `0x6c450` / `0x6c460` | **−2** | `mov $-2` | `NCCL_MIN/MAX_NCHANNELS` — **inert** |
| `ncclParamBuffSize` / `LlBuffSize` / `Ll128BuffSize` | `0x26d50` / `0x26d60` / `0x26d70` | **−2** | `mov $-2` | `NCCL_*BUFFSIZE` — **inert** |
| `ncclParamCrossNic` | `0x26d80` | **2** | `mov $0x2` | `NCCL_CROSS_NIC` — **inert** (baked 2) |
| `ncclParamP2pReadEnable` / `NetGdrRead` | `0x4ba90` / `0x641f0` | **−2** | `mov $-2` | inert |
| `ncclParamCheckPointers` / `IgnoreCpuAffinity` | `0x25ee0` / `0x59c00` | **0** | `xor %eax,%eax` | inert |
| `ncclParamGraphDumpFileRank` / `TopoDumpFileRank` | `0x26d90` / `0x58850` | **0** | `xor` | inert |
| `ncclParamGroupCudaStream` | `0x25ed0` | **1** | `mov $0x1` | inert |
| `ncclParamReportConnectProgress` | `0x3eb70` | **0** | `xor` | inert |

> **CORRECTION (TUNE-2) — `NCCL_PROTO` / `NCCL_ALGO` / `NCCL_NTHREADS` are not even *stubbed*; they are entirely absent.** A reimplementer auditing env knobs might expect the protocol/algorithm overrides to be present-but-inert like `NCCL_MIN_NCHANNELS`. They are not: there is **no string and no accessor** for `NCCL_PROTO`, `NCCL_ALGO`, `NCCL_NTHREADS`, `NCCL_LL128_NTHREADS`, `NCCL_THREAD_THRESHOLDS`, or `NCCL_TREE_THRESHOLD` anywhere in the binary — `strings`/`nm` return nothing. This is the cleanest evidence the chooser was removed at *source* level, not merely disabled: the knobs that would tune a chooser have no compiled trace. The knobs that *do* read `getenv` are the topology/transport ones (`NCCL_GRAPH_FILE`, `NCCL_GRAPH_USE_NEW_ALGORITHM`, `NCCL_TOPO_FILE`/`DUMP_FILE`, `NCCL_P2P_LEVEL`, the `CCOM_*` family) plus the fork-new `NEURON_CCOM_SOCK_TIMEOUT` (`0x8b4b7`, default **120000** ms → `g_sock_timeout` `.data 0xa86a8`, written at `0x26228`/`0x265c0`). Tuning knobs: gone. Topology/transport knobs: live.

### Considerations

The comp-cap arms (`==86`, `[60,69]`, else → divisors 12 / 18 / 21) are byte-anchored (`0x6567c cmp $0x56`; `0x65681 sub $0x3c` then `0x6568c cmp $0x9`) but are **vestigial GPU heuristics** carried over from upstream NCCL: on Trainium, `cudaCompCap` is the inherited NCCL node field repurposed as a generation tag, and the operative branch is the *presence* of a type-1 path on a NeuronCore link, not the SM-version arm taken (**MED** — the arms are structurally clear but not exercised against a live Trainium topology here). The two live clamps that matter are `ncclMinNchannels()`→0 and `ncclMaxNchannels()`→32 (`mov $0x20` at `0x6c484`): combined with the `×2` doubling in `ncclTopoPostset` ([algorithm-ring](algorithm-ring.md) §3) and the `cmp $0x20` at `0x6550f`, the channel count is hard-bounded to 32 regardless of any (inert) env override. `comm->p2pnChannels` is written at `comm+0x452c` (offset 17708); the `"Numbers of channels set to %d"` log (`0x8f0ee`) is emitted here. The `nextPow2` rounding plus the bit-reversal (butterfly) permutation of `p2pChannels[]` is the upstream channel-ordering carried over; the exact bit-reversal width for very large `p2pnChannels` was not enumerated (**LOW**), but the loop shape and the 32-clamp bound it.

---

## Related Components

| Name | Relationship |
|---|---|
| inherited `graph/tuning.cc` / `enqueue.cc` cost model | **removed** — `ncclTopoTuneModel`/`getAlgoInfo`/`ncclTopoGetAlgoTime`/`computeColl` absent from the symbol table |
| inherited `graph/search.cc` speed-tier descent | **retained, simplified** — `speedArray[18]` (`0xae460`) drives the one RING-sizing search in `ncclTopoCompute` |
| `ncclComm.latencies`/`bandwidths`/`threadThresholds`/`maxThreads` | ABI-retained struct slots, **0 `.text` refs** — dead cost matrices the absent model would fill |
| `ncclComm.buffSizes` (+17844) | **live** (4 refs) — proto staging buffers sized; the proto *chooser* is gone |
| `ncclTopoComputeP2pChannels` (`0x654f0`) | the one live numeric "tuning" decision — channel count from `{12,21,18}`, pow2, clamp 32 |
| NRT execution plan (libnrt / Neuron compiler) | owns the protocol (Simple/LL/LL128) decision this library does not make ([send-recv-prims](send-recv-prims.md)) |

## Cross-References

- [Ring Algorithm Construction (Host-Side)](algorithm-ring.md) — the **live** algorithm path: RING is built, not chosen; its §1 names the same empty cost-model sweep, §3 the `×2`/clamp-32 channel doubling this page's `ncclTopoComputeP2pChannels` finalizes
- [Tree / CollNet (Dead Code)](algorithm-tree.md) — the matching negative result for the algorithm *builders*: with no cost model here, nothing could ever *select* Tree/CollNet even if their dead builders ran
- [Topology Detection and Graph Build](topology.md) — produces the `ncclTopoGraph` (`pattern=4`) and the link widths (`topo->maxWidth`/`totalWidth`) that seed this page's `speedArray` descent; §2 documents the inherited search `ncclTopoCompute` drives
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](../collectives/algorithm-taxonomy.md) — the device-side view of the algorithms this (absent) cost model would have ranked; the reduction the host never tunes
- [back to index](../index.md)
