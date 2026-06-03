# How to Read This Book

> *This book reverse-engineers one file: `libtpu.so` v0.103 from the `libtpu-0.0.40-cp314` wheel — a 781,691,048-byte (745.5 MiB) ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d`. Every address on every page is an absolute virtual address in that one binary, recovered purely by static analysis. Another wheel will differ in every address.*

## Abstract

`libtpu.so` is Google's TPU **PJRT plugin** — the single shared object JAX, PyTorch/XLA, and TensorFlow load to drive Cloud TPU hardware. It is the TPU equivalent of NVIDIA's `libcuda.so` + `libnvrtc.so` + the device half of `ptxas`, except that instead of a thin user-mode driver it statically links the *entire* stack into one blob: the XLA compiler, every TPU MLIR dialect, the per-generation LLO backends, the cost and scheduling models, the SparseCore embedding engine, the on-chip memory allocators, the ICI/DCN interconnect fabric, and most of their third-party dependencies (libc++, Abseil, protobuf, Eigen, oneDNN, an embedded TCMalloc). The result is 745 MiB and roughly 884,832 functions in one file. This book reconstructs how that file works.

The reconstruction is *entirely* from static reverse engineering of the binary. There is no source code, no runtime trace, no debugger session behind any claim here — only the disassembled and decompiled bodies, the binary's own (unstripped) symbol table, and a family of structural sidecars indexed off them. That single-provenance discipline is what every page leans on, and it is why each page pins its version and anchors each fact to an address, an offset, a string, or a flag bit. This page is the map to the rest: what the book contains, who it is for, how the eighteen Parts are ordered, which pages to read for a given goal, what a typical page looks like, and where to find the conventions that make the anchors parse at a glance.

Read this page first, then keep two companions open beside it: [Evidence & Confidence Conventions](evidence-conventions.md) for *how much to trust* any given claim, and the [Codename Cheat-Sheet](codename-cheatsheet.md) for the silicon naming, because nearly every deep page indexes some fact by TPU generation and the generations wear at least seven different names.

For navigating this book, the contract is:

- **One binary, one provenance.** Every count, address, and offset traces to the static analysis of the single ELF in the version pin. Nothing is from running the library; nothing is from any source tree.
- **Read in dependency order, not importance order.** The eighteen Parts follow the data's own dependency chain — silicon → compiler → ISA → cost → scheduling → engines → memory → runtime → fabric → observability → config — so each Part assumes only the Parts before it.
- **Confidence is published, not assumed.** Tables carry a Confidence column; prose carries inline grades. A claim graded `Inferred` is scaffolding to orient you, not a foundation to build on.

| | |
|---|---|
| **Subject binary** | `libtpu/libtpu.so` (in the `cp314` manylinux wheel), 781,691,048 B (745.5 MiB) |
| **Build-id** | `89edbbe81c5b328a958fe628a9f2207d` (NT_GNU_BUILD_ID) |
| **Reported version** | `0.103` (package metadata; pin to the build-id, which is unambiguous) |
| **What it is** | The Google TPU PJRT plugin — compiler + runtime + driver + fabric in one object |
| **Recovered functions** | 884,832 (881,784 named / 3,048 anonymous; ~93 % demangled) |
| **ELF shape** | 52 sections, 11 program headers, 4 × `PT_LOAD`; entry point `0x0` (a library) |
| **Sibling object** | `sdk.so` — a separate, much smaller support library in the same wheel |
| **Provenance** | 100 % static reverse engineering; no source, no runtime, no debugger |
| **Structure** | 18 Parts (0–XVII); read in the order below |

---

## What This Book Is

The bar for every page is **reimplementation grade**: a competent systems or compiler engineer who has never opened this binary should be able to rebuild the documented component — the algorithm, the data layout, the bit encoding, the decision logic — from the page alone, and tell which parts are certain and which are inferred. That is a higher bar than "explain what it does," and it shapes everything: pages favor annotated pseudocode over decompiler transcripts, dispatch-dimension tables over thousand-row byte dumps, and rationale ("why this is shaped this way") over restatement.

What the book is **not**: it is not a user guide to JAX-on-TPU, not API documentation for application authors, and not a transcript of decompiler output. It does not tell you how to *use* a TPU; it tells you how the binary that drives the TPU is *built*, down to the bundle bits and cost-table integers.

> **NOTE —** the binary is **not stripped**. Roughly 99.66 % of its functions carry a real symbol and ~93 % carry a full demangled C++ name, read straight out of the object's own `.symtab`. So the C++ identifiers throughout this book — `xla::`, `asic_sw::driver::deepsea::`, `mlir::tpu::`, `TpuVersionToString` — are observed names, not guesses. Where a name is *not* in the binary (the marketing codenames "Trillium" and "Ironwood", for example), the book says so rather than inventing it.

### The intended reader

Write-for-one-reader: a **senior systems or compiler engineer**, comfortable with LLVM/MLIR, SSA, ELF internals, and VLIW ISA encoding, who wants reimplementation-grade detail and is willing to verify a claim against the binary. If you know what a `LoopInfo`, a `SelectionDAG`, a reservation table, a modulo schedule, and a relocation are, the book speaks your language; it teaches each unknown TPU mechanism as a *delta* from the nearest known one. A reader who only wants to call the PJRT API will find Parts II–III useful and the rest far deeper than they need.

### The two-binary fact

The wheel ships **two** ELF objects, and they are not the same thing. `libtpu.so` (this book's subject) is the 745 MiB PJRT plugin; it statically embeds its entire C++ runtime and links no external `libstdc++`. Its sibling `sdk.so` is a separate, much smaller CPython extension / support library that *does* dynamically need `libstdc++` and carries a protobuf/absl-heavy, `libtpu::sdk` / `tpu::monitoring` namespace surface. The analysis covers both, but the overwhelming bulk of the book is `libtpu.so`; treat any unqualified "the binary" as `libtpu.so`. The provenance of the split — why there are two objects and how symbols flow between them — is owned by [libtpu.so + sdk.so](../forensics/two-binary-split.md).

---

## The Eighteen Parts

The book is organized into eighteen Parts (0 through XVII), in [SUMMARY.md](../SUMMARY.md) order. The order is the **data's own dependency chain**: the silicon model parameterizes the compiler, the compiler emits an ISA, the ISA has a cost, the cost drives scheduling, and so on outward to the fabric and the configuration surface. Read top-to-bottom and each Part assumes only the Parts before it; jump around freely once oriented.

| Part | Theme | Start here |
|---|---|---|
| **0 — Reference Apparatus** | Orientation, evidence rules, codenames, glossary — the connective tissue. | [Compile-Flow Walkthrough](compile-flow-walkthrough.md) |
| **I — Binary Anatomy** | The 745 MiB ELF: sections, segments, embedded libraries, dispatch tables, RTTI. | [Forensics Overview](../forensics/overview.md) |
| **II — Plugin Lifecycle & PJRT API** | How the plugin loads, and the 140-slot `PJRT_Api` struct that JAX/PyTorch consume. | [Lifecycle Overview](../lifecycle/overview.md) |
| **III — Tpu C-Shim Layer** | The inner `Tpu*` C ABI between PJRT and the C++ compiler/runtime. | [Shim Overview](../shim/overview.md) |
| **IV — Silicon & Hardware Codename Model** | The six TPU generations the whole compiler is parameterized by. | [Targets Overview](../targets/overview.md) |
| **V — Compiler: Lowering & Optimization Passes** | The IR descent (HLO → MHLO → tpu → LLO) and the optimization passes. | [Compiler Overview](../compiler/overview.md) |
| **VI — TensorCore ISA & LLO Encoding** | The 462-opcode LLO IR and the per-generation VLIW bundle bit-layouts. | [ISA Overview](../isa/overview.md) |
| **VII — Cost & Latency Model** | What every instruction costs — the largest data surface in the binary. | [Cost Overview](../cost/overview.md) |
| **VIII — Instruction Scheduling & Bundle Packing** | The algorithms that consume the cost model and emit ordered, packed bundles. | [Scheduling Overview](../sched/overview.md) |
| **IX — SparseCore & BarnaCore** | The embedding/sparse engine (v5+) and its retired predecessor, kept whole. | [SparseCore Overview](../sparsecore/overview.md) |
| **X — On-Chip Memory & DMA** | The HBM/VMEM/SMEM/CMEM allocators and the intra-chip DMA descriptors. | [Memory Overview](../memory/overview.md) |
| **XI — Runtime & Execution** | Loading and executing a compiled program on a stream. | [Runtime Overview](../runtime/overview.md) |
| **XII — Interconnect & Routing** | The ICI fabric and the toroidal-torus routing solver. | [ICI Overview](../ici/overview.md) |
| **XIII — On-Pod Collectives & Barriers** | All-reduce / all-gather strategies and the SFLAG barrier machinery. | [Collectives Overview](../collectives/overview.md) |
| **XIV — Megascale (Multi-Host / DCN)** | Multi-host bootstrap, fleet metadata, and the cross-host fabric. | [Megascale Overview](../megascale/overview.md) |
| **XV — Profiling & Telemetry** | The XSpace/XPlane trace pipeline and the device telemetry schema. | [Profiling Overview](../profiling/overview.md) |
| **XVI — Configuration & Compile Knobs** | The `xla_*` flag atlas, environment variables, and the compile-env proto. | [Config Overview](../config/overview.md) |
| **XVII — Appendices** | The large enumerated catalogs (opcode tables, intrinsic tables, indices). | [LloOpcode Table](../appendix/llo-opcode-table.md) |

> **NOTE —** the compiler back-end is deliberately split across three Parts along the canonical *what / cost / how* seam: **VI** is which instructions exist, **VII** is what they cost, **VIII** is how to order and pack them. In this binary the cost-model *data* is several times the volume of the scheduling *algorithms*, so conflating them produced an unreadable monster. SparseCore (IX), by contrast, is kept whole rather than sliced across that seam — it is a self-contained engine a reader wants in one place.

---

## Reading Paths

You rarely want all 426 pages. Pick the path that matches your goal; each is a short ordered route through the Parts. Every path assumes you have read [Evidence & Confidence Conventions](evidence-conventions.md) and have the [Codename Cheat-Sheet](codename-cheatsheet.md) at hand.

### "I just want to get oriented"

Start in Part 0, then skim the two map pages. The fastest on-ramp is the [Compile-Flow Walkthrough](compile-flow-walkthrough.md) — it traces one `dot` op through every Part end-to-end and tells you which deep page owns each stage. Follow it with the [Subsystem Map](../subsystem-map.md) (the dependency web) and [Forensics Overview](../forensics/overview.md) (what the file *is*), then dip into Part IV for the silicon model.

### "I want to understand the compile pipeline"

Read the compiler stack in IR order: [HLO Ingestion](../compiler/hlo-ingestion.md) → [Compile Phases 0–3](../compiler/compile-phases.md) → [MHLO → XTile → tpu](../compiler/mhlo-xtile-tpu-lowering.md) → [tpu → LLO Lowering](../compiler/tpu-to-llo-ods.md), with [HLO Pass Registry](../compiler/hlo-pass-registry.md) for the pass framework. Then cross into Part VI for what the LLO it emits actually encodes to.

### "I want the ISA / bundle bytes"

Go straight to Part VI. Begin with the [ISA Overview](../isa/overview.md) and the [LloOpcode Enum (462)](../isa/llo-opcode-enum.md), learn the [Bundle Model](../isa/bundle-model-overview.md), then read the per-generation bundle page for your target (e.g. [Viperfish 64-Byte Bundle](../isa/bundle-vf-64b.md)) and the per-slot encoding pages. The opcode and intrinsic *catalogs* live in Part XVII: [LloOpcode Table (462)](../appendix/llo-opcode-table.md), [LlvmTpu Intrinsic Table (1356)](../appendix/llvmtpu-intrinsic-table.md).

### "I want to reimplement the cost model / scheduler"

Read in dependency order: Part IV for the per-codename silicon constants → Part VI for the ISA → Part VII for the cost data ([Cost Overview](../cost/overview.md) first) → Part VIII for the scheduling algorithms ([Scheduling Overview](../sched/overview.md)). The consolidated per-generation integers are in [Per-Gen Master Comparison Matrix](../appendix/per-gen-comparison-matrix.md).

### "I want TPU-to-TPU collectives / multi-host"

Part IV → Part XII (fabric + routing, starting at [ICI Overview](../ici/overview.md)) → Part XIII (the collective algorithms and barriers) → Part XIV (the multi-host/DCN story).

### "I want to write or debug a PJRT consumer"

Part II for the [PJRT_Api 140-Slot Reconstruction](../pjrt/api-vtable-reconstruction.md) and the [Extension Chain (17)](../pjrt/extension-chain.md), Part III for the [Tpu C-Shim](../shim/overview.md) it calls, Part XI for execution, and Part XV for profiling.

### "I want to navigate the binary itself"

Part I is the forensic map. [Forensics Overview](../forensics/overview.md) establishes the container shape; [ELF Anatomy](../forensics/elf-anatomy.md) walks every section and the VA==offset rule; [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) and [RTTI ↔ Vtable Cross-Validation](../forensics/rtti-vtable-census.md) explain how the 40,313 data tables and 160,566 RTTI records were read. Pair it with [Methodology](../methodology.md) for the extraction pipeline.

> **NOTE —** the book is heavily per-generation. To trace one silicon family end-to-end, use the per-generation cross-index on the [landing page](../index.md): it gives, for each `TpuVersion` 0–5, the family page (IV), the ISA bundle page (VI), the MXU-latency page, and the performance grid (VII) in a single row.

---

## Page Anatomy & Conventions

Every deep page is built the same way, so you learn the shape once and then navigate every page by reflex. The opening is a contract: by the end of the at-a-glance table you know what the component is, what version it pins to, and what you will be able to reimplement.

### The page skeleton

```text
# Title                     ← a plain noun phrase, one per page
> version-pin blockquote    ← fixes the binary so every address is unambiguous
## Abstract                 ← 2–3 paragraphs of orientation; relates to a known frame
   reimplementation contract ← bullets: what a reimplementer must reproduce
   at-a-glance table        ← the hard anchors: entry points, addresses, sizes, IR level
## <Unit 1>                 ← one structural unit (a phase, engine, slot, pass)
   ### Purpose / Entry Point / Algorithm / Function Map / Considerations
---                         ← horizontal rule between top-level units
## <Unit 2> …
## Cross-References          ← link list, each with a one-line why, closes the page
```

Sibling units inside a page share the same `###` vocabulary in the same order — `Purpose`, `Entry Point`, `Algorithm` (annotated pseudocode, not raw decompiler output), `Function Map` (a table with a Confidence column), `Considerations`. A reader scanning for "how is the decision made" always finds it under `### Algorithm`.

### Confidence and callouts

Because this is reverse engineering, every page is honest about *how directly the binary supports each claim*. Reverse-engineered tables carry a **Confidence** column and prose carries inline grades. The scale grades evidence directness, not plausibility:

| Grade | Meaning |
|---|---|
| **High** (or **CERTAIN** for byte-checked counts) | Read directly from a decompiled body or a byte-exact table. Trust verbatim. |
| **Medium** | Inferred from several agreeing indirect indicators (callers, strings, table position). |
| **Low** | A single weak indicator with no corroboration. A lead, not a fact. |
| **Inferred** | Reasoned from structure or analogy with no direct byte. A hypothesis for orientation only. |

Callouts are blockquotes with a bold text marker — never an emoji. `> **QUIRK —**` flags a counter-intuitive fact; `> **GOTCHA —**` flags a trap where the naive implementation is silently wrong; `> **NOTE —**` flags a clarification; `> **CORRECTION (tag) —**` records an overturned earlier claim in place rather than editing it away. The full definitions — the four grades, the callout vocabulary, the citation grammar, and the known extraction limits — are the subject of [Evidence & Confidence Conventions](evidence-conventions.md). Read that page once and every label elsewhere carries a precise, pre-agreed meaning.

> **GOTCHA —** a demangled symbol name describes what the original author *named* a function, not necessarily what the function does in this build. When a name and its decompiled body disagree, the book follows the **body** and flags the discrepancy. A claim resting on a name alone never rises above `Low`; do not read a friendly symbol as proof of behavior.

### Citation grammar

Anchors follow one small, fixed grammar so they parse instantly. Addresses are absolute virtual addresses in hex (`0xfe21da0`, `sub_E635524`); a bare `sub_ADDR` is the placeholder for an anonymous function. Struct fields are cited as base+offset (`ctx+0x10`, `Target+0x628`) because the binary has no field names — the offset *is* the field's identity. Tables, switches, and strings are cited by anchor address and, where relevant, an index or literal text. The rule behind all of it: every claim points at something a reader with the same binary can independently find.

---

## First Stops

Before any deep page, three Part-0 pages repay reading, and two more are constant companions you will return to.

- **[Codename Cheat-Sheet](codename-cheatsheet.md)** — the one card to come back to. A single TPU generation wears at least seven names across three independent integer axes (`TpuVersion` 0–5, `DeviceType` 1–13, `TpuVersionProto` 1–6) that do not share a numbering. This page pins every name of every generation to the binary site that defines it, side by side, and warns about the off-by-ones — including that `DeviceType` 12 is *newer* than 13.
- **[Evidence & Confidence Conventions](evidence-conventions.md)** — the calibration page. The four Confidence levels, the callout markers, the citation style, and — most importantly — what static analysis structurally *could not* recover, so you know where the floor is.
- **[Methodology](../methodology.md)** — how the analysis was performed: the extraction pipeline, the IDA sidecars (`functions`, `names`, `strings`, `rtti`, `switches`, `xrefs`, …), and the naming conventions every page inherits.
- **[Subsystem Map](../subsystem-map.md)** — the dependency web that explains *why* the eighteen Parts sit in the order they do, and which domain each covers.
- **[Glossary](../glossary.md)** — the TPU vocabulary the book assumes: LLO, MXU, XLU, EUP, SCS/TAC/TEC, MRB, SFLAG, ICI/DCN, HBM/VMEM/SMEM/CMEM, and the rest. Keep it open on your first read of any deep page.

And two structural maps for going wide:

- **[Forensics Overview](../forensics/overview.md)** — the verified top-level shape of the 745 MiB file: the section/segment census, the function population, and the capsule atlas of major embedded regions. The source of the headline numbers reused on every page.
- **[Compile-Flow Walkthrough](compile-flow-walkthrough.md)** — the single best on-ramp: one matmul traced from HLO to bundle bytes to execution, cross-referencing every Part as it goes.

> **NOTE —** not every page in the book is written yet. The index tracks status: pages marked *(written)* have content; the rest are scaffolded stubs awaiting authoring, and unrecovered topics are tracked in the [Open-Frontier Register](../appendix/open-frontier-register.md). A stub link is still a valid destination — it just means the deep content is still in the pipeline.

---

## Cross-References

- [Evidence & Confidence Conventions](evidence-conventions.md) — the trust contract: the four Confidence grades, callout markers, citation grammar, and extraction limits. Its companion on *how much to believe*.
- [Codename Cheat-Sheet](codename-cheatsheet.md) — the silicon naming card; the three-axis enum reconciliation the whole book indexes by.
- [Methodology](../methodology.md) — the static-analysis pipeline and the sidecar family every page cites.
- [Subsystem Map](../subsystem-map.md) — the 13-domain dependency web behind the Part ordering.
- [Glossary](../glossary.md) — the TPU/LLO/fabric vocabulary the deep pages assume.
- [Forensics Overview](../forensics/overview.md) — the canonical headline structural counts, confirmed against the raw binary.
- [Compile-Flow Walkthrough](compile-flow-walkthrough.md) — one op traced through every Part; the recommended first deep read.
- [libtpu Internals (landing)](../index.md) — the master index, the per-generation cross-index, and the full page-status table.
