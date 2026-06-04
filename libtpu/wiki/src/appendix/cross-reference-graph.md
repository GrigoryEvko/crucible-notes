# Cross-Reference Graph

> *All counts on this page are derived from the wiki source tree, not from `libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`). They describe the **wiki's own navigation structure**, not the binary, and they drift as pages are added or renamed. Treat every number below as approximate and re-derive after any large editing pass.*

## Abstract

This appendix maps the wiki as a directed graph: every page is a node, every inline Markdown link `](path.md)` is an edge. Unlike the rest of the reference — which documents the TPU binary — this page documents the *book*, so a reader (or a future editor) can see which pages are central junctions, which pages fan out to many others, and which Parts are tightly coupled. It is the structural complement to [`front/how-to-read.md`](../front/how-to-read.md): that page owns the human reading-path narrative ("start here, read these in order"); this page owns the measured topology ("these 18 pages absorb a third of all inbound links").

The graph has **426 page nodes** — every distinct `.md` file linked from `SUMMARY.md`, including the `index.md` landing page that `SUMMARY.md` lists as its prefix chapter; `SUMMARY.md` itself is the mdBook table of contents and is excluded from the node set. Of the 425 nodes assigned to a numbered Part, plus `index.md`, the count is 426. Across those nodes there are roughly **eight thousand internal `.md` link instances**, nearly all of which resolve to a real page; a small number of stale relative paths are flagged for repair. The 18 Parts (0 through XVII) partition the nodes; inter-Part edges reveal the coupling backbone — the compiler→ISA→cost spine and the routing↔collectives mesh dominate.

Three roles emerge from the counts and structure the rest of the page:

- **Hubs** — pages with high *inbound* degree. A reader arrives here from many places; an editor who renames one breaks the most links. The overview pages and the shared ISA/cost reference pages are the hubs.
- **Connectors** — pages with high *outbound* degree. They fan the reader out into a subsystem. The cross-cutting reference pages (`glossary.md`, `subsystem-map.md`, the appendix tables) and the section `overview.md` pages are the connectors.
- **Spine** — the strongest directed inter-Part edges, which trace the canonical compile-and-run pipeline through the book.

| | |
|---|---|
| **Node set** | 426 pages (425 assigned to numbered Parts + `index.md` prefix chapter) |
| **Parts** | 18 (Part 0 — Reference Apparatus … Part XVII — Appendices) |
| **Internal link instances** | ~8200 (nearly all resolve; a handful of stale relative paths) |
| **Top hub** | [`isa/slot-mxu.md`](../isa/slot-mxu.md) — ~50 inbound |
| **Top connector** | [`glossary.md`](../glossary.md) — 73 distinct outbound |
| **Structural sink** | [`index.md`](../index.md) — ~146 inbound ("back to index") |
| **Densest Part (inbound)** | Part VI — TensorCore ISA |
| **Heaviest internal coupling** | Part IX — SparseCore (most intra-Part links) |

> **NOTE —** "inbound link count" here means *distinct source pages* that contain at least one link to the target (page-level in-degree), except where noted as *link instances* (raw edge count, counting repeats). The two differ when one page links the same target several times; the hub table uses distinct-source counts, which is the metric an editor cares about ("how many files do I touch if I move this page").

---

## Part Structure

The 18 Parts and their page counts, parsed directly from `SUMMARY.md` by assigning each linked page to the Part heading immediately above it. The total is 425; with `index.md` the node set is 426, and `SUMMARY.md` brings the file count to 427.

| Part | Pages | Theme |
|---|---:|---|
| **0** — Reference Apparatus | 8 | How to read, glossary, methodology, bibliography, subsystem map |
| **I** — Binary Anatomy | 12 | ELF layout, sections, build metadata, forensics |
| **II** — Plugin Lifecycle & PJRT API | 23 | PJRT C API, vtable reconstruction, plugin init/teardown |
| **III** — Tpu C-Shim Layer | 10 | The `Tpu*` C entry points bridging PJRT to the runtime |
| **IV** — Silicon & Hardware Codename Model | 24 | Codename reconciliation, per-gen specs, PCI IDs, naming |
| **V** — Compiler: Lowering & Optimization Passes | 36 | XLA→MLIR→LLO pipeline, pass catalog, intrinsic lowering |
| **VI** — TensorCore ISA & LLO Encoding | 42 | Bundle model, VLIW slots, MC emitter, opcode encoding |
| **VII** — Cost & Latency Model | 41 | Resource enum, MXU/VPU latency tables, scheduling cost |
| **VIII** — Instruction Scheduling & Bundle Packing | 14 | List scheduler, bundle packer, hazard model |
| **IX** — SparseCore & BarnaCore | 45 | SC tiles, TEC/SCS sequencers, gather/scatter, BarnaCore |
| **X** — On-Chip Memory & DMA | 20 | VMEM/SMEM/HBM hierarchy, DMA descriptors, tiling |
| **XI** — Runtime & Execution | 11 | Program loading, internal pass names, execution loop |
| **XII** — Interconnect & Routing | 30 | ICI bring-up, topology, routing, TwistedTorus strategy |
| **XIII** — On-Pod Collectives & Barriers | 30 | All-reduce/all-gather, barrier engines, reduction trees |
| **XIV** — Megascale (Multi-Host / DCN) | 21 | DCN transport, multi-slice, host-side collective fanout |
| **XV** — Profiling & Telemetry | 22 | Trace format, counters, XProf integration |
| **XVI** — Configuration & Compile Knobs | 16 | Flag parsing, knob catalog, XLA flag plumbing |
| **XVII** — Appendices | 20 | Cross-cutting tables, glossaries, this graph, anchor index |
| **Total** | **425** | (+`index.md` = 426 nodes; +`SUMMARY.md` = 427 files) |

> **GOTCHA —** directory layout does **not** map one-to-one to Parts. `twist/` and `ici/` both live under Part XII (Interconnect & Routing); `barnacore/` lives under Part IX (SparseCore); `barrier/` and `collectives/` are both Part XIII; `memory/` and `dma/` are both Part X. Counting by top-level directory mis-files `twist/` (9 pages) into the silicon Part. The table above is parsed from `SUMMARY.md` ordering, which is authoritative.

---

## Top Hub Pages

Pages ranked by **distinct inbound source pages** (page-level in-degree), excluding `index.md`. These are the central junctions: rename one and you rewrite dozens of links; they are also the pages a cross-referencing editor should always remember to point new pages at.

| Page | Inbound | Role |
|---|---:|---|
| [`isa/slot-mxu.md`](../isa/slot-mxu.md) | ~50 | The MXU VLIW slot — every lowering/cost/sched page references it |
| [`sparsecore/overview.md`](../sparsecore/overview.md) | ~48 | SparseCore landing — most Part-IX pages link up to it |
| [`isa/bundle-model-overview.md`](../isa/bundle-model-overview.md) | ~45 | The VLIW bundle model — the ISA's central concept |
| [`compiler/overview.md`](../compiler/overview.md) | ~37 | Compiler pipeline landing |
| [`collectives/overview.md`](../collectives/overview.md) | ~35 | On-pod collectives landing |
| [`cost/resource-enum.md`](../cost/resource-enum.md) | ~34 | The resource enum shared by sched + cost + ISA |
| [`targets/tpu-version-codename-matrix.md`](../targets/tpu-version-codename-matrix.md) | ~33 | The authoritative codename reconciliation table |
| [`cost/mxu-latency-overview.md`](../cost/mxu-latency-overview.md) | ~31 | MXU latency reference, cited by lowering and sched |
| [`routing/overview.md`](../routing/overview.md) | ~28 | Routing landing |
| [`cost/overview.md`](../cost/overview.md) | ~28 | Cost-model landing |
| [`profiling/overview.md`](../profiling/overview.md) | ~27 | Profiling landing |
| [`compiler/compile-phases.md`](../compiler/compile-phases.md) | ~27 | The phase ordering, cited from runtime and ISA |
| [`isa/mc-emitter.md`](../isa/mc-emitter.md) | ~26 | The machine-code emitter |
| [`pjrt/overview.md`](../pjrt/overview.md) | ~25 | PJRT API landing |
| [`sparsecore/tec-engine.md`](../sparsecore/tec-engine.md) | ~24 | TEC sequencer engine |
| [`sparsecore/stream-gather-scatter.md`](../sparsecore/stream-gather-scatter.md) | ~24 | SC gather/scatter primitive |
| [`compiler/tpu-to-llo-ods.md`](../compiler/tpu-to-llo-ods.md) | ~23 | The TPU→LLO ODS dialect definition |
| [`sched/overview.md`](../sched/overview.md) | ~23 | Scheduling landing |
| [`index.md`](../index.md) | ~146 | The prefix landing — the universal "back to index" target |

> **NOTE —** `index.md` is in a class by itself at roughly 146 inbound. Almost every page closes with a `[back to index](../index.md)` link, so its in-degree tracks the page count, not topical importance. It is the structural sink of the graph and is listed last so it does not distort the topical ranking. The real topical hubs are the `overview.md` pages and the shared ISA/cost reference pages — exactly the pages [`front/how-to-read.md`](../front/how-to-read.md) recommends as section entry points.

> **QUIRK —** the hub list is dominated by Parts V (Compiler), VI (ISA), VII (Cost), and IX (SparseCore). That is not an artifact: those four Parts are the *referenced* core. A page about routing, collectives, or profiling routinely links *down* into the ISA and cost model to explain what an instruction costs or how a slot is encoded, but the ISA pages rarely link back up. The graph is asymmetric by design — the spine is referenced, not referencing.

---

## Top Connector Pages

Pages ranked by **distinct outbound internal links** (out-degree). These fan the reader out; they are the pages that knit the book together. Two species appear: the cross-cutting reference pages in Part 0 / Part XVII (glossaries, the subsystem map, the appendix tables) that point at everything, and the section `overview.md` pages that point at every page in their own Part.

| Page | Outbound | Species |
|---|---:|---|
| [`glossary.md`](../glossary.md) | 73 | Cross-cutting — every defined term links to its home page |
| [`subsystem-map.md`](../subsystem-map.md) | 63 | Cross-cutting — the visual index of the whole binary |
| [`appendix/glossary-extended.md`](glossary-extended.md) | 59 | Cross-cutting — long-form term definitions |
| [`isa/overview.md`](../isa/overview.md) | ~44 | Section overview — fans into the ISA pages |
| [`front/how-to-read.md`](../front/how-to-read.md) | ~42 | Cross-cutting — the reading-path companion to this page |
| [`sparsecore/overview.md`](../sparsecore/overview.md) | 40 | Section overview — both a top hub *and* a top connector |
| [`compiler/overview.md`](../compiler/overview.md) | 39 | Section overview — compiler pipeline fan-out |
| [`front/compile-flow-walkthrough.md`](../front/compile-flow-walkthrough.md) | 35 | Cross-cutting — end-to-end worked example threading many Parts |
| [`appendix/llvmtpu-intrinsic-table.md`](llvmtpu-intrinsic-table.md) | 33 | Cross-cutting — intrinsic→lowering-page index |
| [`isa/bundle-model-overview.md`](../isa/bundle-model-overview.md) | ~25 | Section overview — bundle/slot fan-out |
| [`runtime/internal-pass-names.md`](../runtime/internal-pass-names.md) | 22 | Cross-cutting — pass-name→pass-page index |
| [`profiling/overview.md`](../profiling/overview.md) | 22 | Section overview |
| [`compiler/compile-phases.md`](../compiler/compile-phases.md) | 21 | Section index — phase→page |
| [`appendix/llo-opcode-table.md`](llo-opcode-table.md) | 21 | Cross-cutting — opcode→encoding-page index |
| [`routing/overview.md`](../routing/overview.md) | 18 | Section overview |

> **GOTCHA —** [`sparsecore/overview.md`](../sparsecore/overview.md), [`compiler/overview.md`](../compiler/overview.md), [`isa/bundle-model-overview.md`](../isa/bundle-model-overview.md), and [`routing/overview.md`](../routing/overview.md) appear in *both* the hub and the connector tables. The section-overview pages are bidirectional anchors — every page in the Part links up to the overview (high inbound) and the overview links down to every page (high outbound). They are the natural per-Part entry points and the highest-value pages to keep accurate.

---

## Inter-Part Link Density

Edge counts aggregated to the Part level, using the `SUMMARY.md`-authoritative page→Part assignment. `out` = link instances to *other* Parts; `in` = link instances arriving *from* other Parts; `self` = intra-Part link instances. The ASCII summary below ranks Parts by each axis and then traces the strongest directed edges.

```text
PER-PART LINK DEGREE  (link instances; self = intra-Part)

Part                                 in    out   self
  0   Reference Apparatus            16    347    44     <- pure connector (fans out, rarely targeted)
  I   Binary Anatomy               101     31   117
  II  Plugin Lifecycle & PJRT      124     84   357
  III Tpu C-Shim Layer              15     81    96
  IV  Silicon & Codename            99     47   195
  V   Compiler                     229    176   553     <- spine: most-referenced compiler stage
  VI  TensorCore ISA               363     75   542     <- HEAVIEST inbound: the referenced core
  VII Cost & Latency Model         161    113   579
  VIII Scheduling & Bundle Pack     92    129   130
  IX  SparseCore & BarnaCore       169    105   876     <- HEAVIEST self: most internally cohesive Part
  X   On-Chip Memory & DMA         166    125   282
  XI  Runtime & Execution           77     92   109
  XII Interconnect & Routing       123    110   480
  XIII On-Pod Collectives          102    143   477
  XIV Megascale (DCN)               39     15    97     <- most peripheral: low in, low out
  XV  Profiling & Telemetry         63     49   373
  XVI Configuration & Knobs         49     11   251     <- referenced (knobs), barely references out
  XVII Appendices                   22    433    35     <- HEAVIEST outbound: the index-of-indexes


STRONGEST DIRECTED INTER-PART EDGES  (from -> to, link instances)

  XVII -> VI    126   appendix tables index every ISA opcode/slot page
  VII  -> VI     76   cost model is computed per ISA instruction
  0    -> V      73   reference apparatus points readers at the compiler first
  VIII -> VII    63   scheduler queries the cost model on every decision
  XII  -> XIII   61   routing underpins collective communication
  XI   -> V      60   runtime dispatches into the compiler pipeline
  XIII -> XII    59   collectives ride the routing fabric  (mesh: XII<->XIII)
  XVII -> I      56   appendix anchors point back at binary-anatomy evidence
  X    -> V      48   memory/tiling decisions feed the compiler
  VIII -> VI     46   scheduling reasons over ISA bundle slots
  V    -> IX     45   compiler lowers a path into the SparseCore backend
  II   -> XI     45   PJRT API hands control to the runtime
  0    -> VI     44   subsystem map / glossary anchor ISA terms
  0    -> I      44   reference apparatus anchors binary-anatomy terms
  III  -> II     38   C-shim sits directly beneath the PJRT API
```

The picture in one paragraph: **Part VI (TensorCore ISA) is the gravitational center** — it absorbs 363 inbound link instances and the single strongest edge (`XVII → VI`, 126) comes from the appendix tables that catalog its opcodes. The **compile spine** is `0 → V → {VI, IX}` and `VII ↔ VI ↔ VIII`: the cost model (VII) and scheduler (VIII) both reference the ISA (VI), and the scheduler in turn queries the cost model (VIII → VII, 63). The **communication mesh** is the bidirectional `XII ↔ XIII` pair (routing ↔ collectives, 61 and 59) — the two Parts cite each other almost symmetrically because a collective *is* a routed communication pattern. **Part IX (SparseCore)** is the most internally cohesive (876 self-links) — it is a near-self-contained sub-book. **Part XVII (Appendices)** is the index-of-indexes: 433 outbound, almost none inbound. **Part XIV (Megascale)** is the most peripheral, sitting at the DCN edge with little inbound and the least outbound.

> **NOTE —** Part 0 and Part XVII are structurally opposite poles of the same function. Part 0 (`glossary`, `subsystem-map`, `how-to-read`) is the *front-door* connector: it fans readers *into* the content Parts (347 out, 16 in). Part XVII (the appendix tables and extended glossary) is the *back-door* connector: it indexes the content Parts from behind (433 out, 22 in). Neither is a destination; both are pure routers. This page lives in Part XVII and is one of the few exceptions — it is meant to be *read*, not just traversed.

---

## Recommended Traversal Paths

The measured topology suggests four traversal orders. For the prose rationale and the per-persona reading plan, defer to [`front/how-to-read.md`](../front/how-to-read.md); the paths below are the *graph-shortest* routes through the hubs.

### Path 1 — The compile spine (most-traveled)

The canonical end-to-end route, following the strongest inbound edges into the ISA core:

```text
front/how-to-read.md
  -> compiler/overview.md            (hub: ~37 in)
  -> compiler/compile-phases.md      (hub: ~27 in)
  -> compiler/tpu-to-llo-ods.md      (hub: ~23 in)
  -> isa/bundle-model-overview.md    (hub: ~45 in)
  -> isa/slot-mxu.md                 (TOP hub: ~50 in)
  -> cost/resource-enum.md           (hub: ~34 in)
  -> sched/overview.md               (hub: ~23 in)
```

This is the spine Parts V → VI → VII → VIII trace. A reader who walks only these seven pages sees the whole lowering-to-scheduling pipeline through its highest-degree nodes.

### Path 2 — The accelerator-engine deep dives

The two engine sub-books, each entered through its dual hub/connector overview:

```text
sparsecore/overview.md  (~48 in / 40 out)  -> tec-engine.md, stream-gather-scatter.md, architecture.md
barnacore/overview.md                     -> bcs-scalar-isa.md, retirement.md
```

Part IX is the most internally cohesive Part (876 self-links); once inside `sparsecore/overview.md` the reader can stay local and follow intra-Part links exclusively.

### Path 3 — The communication mesh

The bidirectional routing↔collectives pair, plus the megascale extension:

```text
routing/overview.md      (hub: ~28 in)  <-> collectives/overview.md   (hub: ~35 in)
  twist/overview.md      (hub: ~22 in)     barrier/...
  ici/...                                 megascale/...   (Part XIV, peripheral)
```

The `XII ↔ XIII` symmetry (61 / 59) means either overview is a valid entry; the graph lets the reader oscillate between routing mechanism and collective semantics.

### Path 4 — The reference-apparatus shortcut

For a reader who knows what they want, the connector pages are the fastest fan-out:

```text
glossary.md          (73 out)  — term -> home page
subsystem-map.md     (63 out)  — subsystem -> Part
appendix/llvmtpu-intrinsic-table.md (33 out) — intrinsic -> lowering page
appendix/llo-opcode-table.md        (21 out) — opcode -> encoding page
```

These four pages collectively point at most of the book; they are the index layer.

> **QUIRK —** there is no single Hamiltonian reading order — the graph is not a line, it is a hub-and-spoke with a routing↔collectives cycle. The four paths above are the natural *arcs*; `front/how-to-read.md` sequences them for a first read, this page measures them. An editor adding a new page should wire it into at least one Part-overview hub (inbound) and the relevant appendix index (so it is discoverable from the back door), or it becomes an orphan that the next run of this analysis will flag.

---

## Cross-References

- [`front/how-to-read.md`](../front/how-to-read.md) — the reading-path companion; owns the human narrative this page measures structurally. Read it first.
- [`subsystem-map.md`](../subsystem-map.md) — the top connector (63 outbound); the visual index whose edges this page counts.
- [`glossary.md`](../glossary.md) — the single highest-outbound page (73); the term-to-page router.
- [`appendix/evidence-anchor-index.md`](evidence-anchor-index.md) — sibling appendix; indexes binary evidence anchors as this page indexes navigation edges. Parallel structure, orthogonal axis.
- [`appendix/glossary-extended.md`](glossary-extended.md) — long-form glossary, the third-highest connector (59 outbound).
- [`index.md`](../index.md) — the prefix landing and structural sink (~146 inbound); the `SUMMARY.md` table of contents is its source of truth.
