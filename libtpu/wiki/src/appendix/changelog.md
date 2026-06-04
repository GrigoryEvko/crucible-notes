# Changelog — Reconstruction Change Record

> *This wiki is a static reverse-engineering reconstruction of one artifact: `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (wheel version `0.0.40`, build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Every address, ordinal, and symbol on every page is pinned to that one binary by its build-id. Other wheels will differ.*

## Abstract

This is the wiki's own change record — not a software release log. `libtpu.so` ships as a single ~745 MB PJRT plugin with no public source and no versioned API surface the documentation could track release-over-release. (The binary is **not** stripped — it retains a full `.symtab` of ~1.23 M symbols alongside the 740-entry `.dynsym` — which is exactly what makes static reconstruction tractable; the runtime ABI surface is essentially the PJRT C entry point `GetPjrtApi` @ `0xe6a83a0`, alongside a second `GetLibtpuSdkApi` export.) There is exactly **one** binary under analysis, so a conventional dated changelog ("what changed in 0.0.40 vs 0.0.39") is not what this page can honestly provide. What it *can* provide is the record of how the **reconstruction itself** evolved: the version it pins to, the 18-Part / ~427-page structure it grew into, and — the part worth a reader's attention — the **corrections** the deep pages filed against themselves as they re-checked early beliefs against direct decompile evidence.

A correction here is a first-class artifact. When a page's analysis overturned a prior claim (its own scratch notes, an earlier pass, or a plausible-but-wrong inference), the old claim was **not** silently deleted. It was left visible with a tagged `> **CORRECTION (tag) —**` callout beside it, so a reader who absorbed the old claim is actively warned. The convention is defined on [evidence-conventions.md](../front/evidence-conventions.md); this page is the consolidated index of where it fired and on what. As that page puts it, an in-place correction is *evidence of trustworthiness* — a reverse-engineering reconstruction with zero corrections has either analyzed nothing hard or is hiding its mistakes.

What this page is:

- A **version pin** — the exact wheel, version string, and build-id every page is anchored to, re-confirmed against the binary here.
- A **structure record** — the 18 Parts and page count the reconstruction settled into.
- A **notable-corrections table** — the headline self-reversals, harvested from the real tagged callouts, with the owning page and a Confidence rating on the *corrected* finding.
- A **methodology-evolution note** — how the analysis loop changed shape as the corrections accumulated.

What this page is **not**: a per-release diff, a roadmap, or a list of TODOs. It documents a single-snapshot static-RE artifact and frames itself honestly as one.

---

## Release / Version Pin

There is one release under reconstruction. Every fact below was re-confirmed against the binary and the wheel's `.dist-info` while authoring this page.

| Field | Value | Source / Confirmation |
| --- | --- | --- |
| Wheel | `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` | `.dist-info/METADATA` |
| Package / Version | `libtpu` / `0.0.40` | `METADATA`: `Name: libtpu`, `Version: 0.0.40` |
| Runtime version | 0.103 | wiki version-pin convention only — **statically unverifiable** in the binary (no `0.103` string in `.rodata`; the ABI entry points `GetPjrtApi` + `GetLibtpuSdkApi` encode no version). Pin to the build-id, not this number. |
| Build label | `libtpu_lts_20260413_b_RC00` | house version-pin string (LTS build of 2026-04-13) |
| BuildID (md5) | `89edbbe81c5b328a958fe628a9f2207d` | `readelf -n` → `.note.gnu.build-id` |
| Binary | `libtpu/libtpu.so` | `extracted/libtpu-0.0.40-cp314-…/libtpu/libtpu.so` |
| Size | 781,691,048 bytes (~745 MB) | `stat` / `ls -l` |
| Python ABI | CPython 3.14 (`cp314`) | wheel tag |
| Platform | `manylinux_2_31_x86_64` | wheel tag |
| `soname` | absent | `readelf -n` (genuinely none — see `ELF-2` below) |

> **NOTE (PIN) —** the build-id is the canonical anchor. If `readelf -n` on your local `libtpu.so` reports a different value than `89edbbe81c5b328a958fe628a9f2207d`, you are not looking at the binary this wiki documents, and every address on every page should be treated as un-verifiable for your copy. Pin first; read second.

> **GOTCHA (VER) —** the literal string `0.103` is **not** a direct substring in the binary's `.rodata` (`strings | grep 0.103` returns nothing), and there is no exported `libtpu_version` symbol either — `.rodata` carries only the bare token `libtpu_version`, and the binary's dynamic ABI entry points (`GetPjrtApi` @ `0xe6a83a0`, `GetLibtpuSdkApi`) encode no version. So `0.103` is **not** statically verifiable from this binary: it is a wiki version-pin convention paired with the wheel's `0.0.40`, not a value the binary itself asserts. The canonical anchor is the build-id; do not present `0.103` as a binary fact.

---

## Structure

The reconstruction is organized as a single mdBook with **18 Parts** (Part 0 through Part XVII) and **427** Markdown pages under `src/` (count: `find src -name '*.md' | wc -l`). The Part roster is fixed in `SUMMARY.md`; this is the spine every deep page hangs from.

| Part | Title | Scope |
| --- | --- | --- |
| 0 | Reference Apparatus | how-to-read, evidence conventions, methodology, glossary |
| I | Binary Anatomy | ELF layout, custom sections, static-init, the trailing-blob forensics |
| II | Plugin Lifecycle & PJRT API | init/fini, plugin discovery, PJRT vtable reconstruction |
| III | Tpu C-Shim Layer | platform/topology shim, transfer manager |
| IV | Silicon & Hardware Codename Model | HAL families, codename matrix, PCI IDs, sub-core taxonomy |
| V | Compiler: Lowering & Optimization Passes | MHLO→MLO→LLO lowering, fusion, layout, sharding |
| VI | TensorCore ISA & LLO Encoding | bundle models, slot encoders, EmitX bit positions |
| VII | Cost & Latency Model | MXU/EUP/XLU latency, HLO cost analysis, per-gen integers |
| VIII | Instruction Scheduling & Bundle Packing | LHS core, MRB allocation, MXU bin-packing |
| IX | SparseCore & BarnaCore | SCS/TAC/TEC engines, minibatching, sequencer outlining |
| X | On-Chip Memory & DMA | memory-space enum, tcmalloc, SFLAG protocol, DMA descriptors |
| XI | Runtime & Execution | error templates, hint strings, internal pass names |
| XII | Interconnect & Routing | net-router pipeline, toroidal route cache, NF descriptors |
| XIII | On-Pod Collectives & Barriers | all-to-all tables, FP8 quantized collectives, barrier binding |
| XIV | Megascale (Multi-Host / DCN) | cross-host barrier, error aggregation |
| XV | Profiling & Telemetry | per-DeviceType structs, v7x perf-counters, trace coders |
| XVI | Configuration & Compile Knobs | DebugOptions proto, flag families, TCE field dictionary |
| XVII | Appendices | the consolidated catalogs (this page lives here) |

The appendices in Part XVII deliberately *aggregate* facts the deep pages established, then re-verify them — which is why the densest corrections (memory-space numbering, symbol-namespace counts, flag counts) cluster in the appendix tables: cross-checking one consolidated table against the binary is exactly the operation that surfaces an off-by-one in a name or an ambiguous count.

> **NOTE (STRUCT) —** the page count (427) and Part count (18) are themselves pinned facts, re-counted while writing this page. They will drift as pages are added; treat the numbers as a snapshot of the reconstruction state at the build-id above, not a permanent constant.

---

## Notable Corrections

These are the headline self-reversals — harvested from the real tagged `CORRECTION` callouts in the source tree, not invented. Across all 427 pages there are **231** distinct correction tags (in **249** `> **CORRECTION (…)` callouts; a handful of tags fire on more than one page); the table below is the curated set a reader should know about, each linking to its owning page where the full reasoning and addresses live. The **Confidence** column rates the *corrected* (current) finding, not the discarded claim.

| Tag | What was overturned | Corrected finding | Owning page | Confidence |
| --- | --- | --- | --- | --- |
| **ZSTD-01** | A "trailing zstd blob at `0x20F99BEF`, ~4.1 MB, dictionary-encoded, decoding to per-codename hardware constants." | No blob exists. The offset is inside `.text`, not past EOF; the bytes are a `mov` immediate, not a zstd frame; no dictionary, no payload. Every task gated on "decode the blob" is closed *no blob exists*. | [forensics/trailing-zstd-blob.md](../forensics/trailing-zstd-blob.md) | CERTAIN |
| **GLOSS-1** | `walrus` listed among the binary's IR/compiler terms. | `walrus` is **absent**. Case-insensitive search of both name and string tables returns nothing; retained only as a flagged absence so later pages do not treat it as binary-grounded. | [glossary.md](../glossary.md) | CERTAIN |
| **SYM-NS-1** | RE2 counted at ≈19,463 functions (a substring metric mistaken for ownership). | RE2's owned function surface is **226** functions (496 owner names); the ≈19k is `re2`-substring participation across all names — a different, non-comparable question. | [appendix/symbol-namespace-index.md](symbol-namespace-index.md) | HIGH |
| **SYM-NS-2** | absl ≈271,942 and Eigen ≈48,153 (figures that do not reproduce). | By the name sidecar: absl **owns 27,777** functions (participates in ~117k symbols); Eigen **owns 10,419**. Participation ≠ ownership; name the surface before quoting a number. | [appendix/symbol-namespace-index.md](symbol-namespace-index.md) | HIGH |
| **DISP-1** | "17 dispatch-taxonomy classes." | **19** classes after two mis-merges were split (dnnl vs Xbyak; the C-runtime/Rust handler tables mis-filed as trampoline false positives). | [forensics/dispatch-table-taxonomy.md](../forensics/dispatch-table-taxonomy.md) | HIGH |
| **PDT-1** | The per-DeviceType `+0x438`/`+0x440` tail read as a packed sub-field or hash. | Two `int32` slots: the **perf-counter-set enum bases** for the v7x ICR (`+0x438`) and CMNUR/HBM (`+0x440`) sets — nonzero only on `DT12`, not roofline doubles. | [profiling/per-devicetype-struct.md](../profiling/per-devicetype-struct.md) | HIGH |
| **MST-1** | A MEDIUM-confidence enum: `kSflag=7`, `kSparseCoreSequencerSmem=12`, `kPinnedHbm=2`, … | The binary fixes `sflag = 6`, `imem = 7`, `sparse_core_sequencer_smem = 14`, `sparse_core_sequencer_sflag = 12`, `host = 13`; `hib`(2) / `pinned_hbm`(16). The byte-exact 17-value table supersedes the prior numbering. | [appendix/memory-space-table.md](memory-space-table.md) | CERTAIN |
| **MST-2** | `ShapeSizeBytesRaw` branch annotated as `== kSparseCoreSequencerSmem (12)`. | The literal `12` is correct, but `12` is `sparse_core_sequencer_sflag`, not `…_smem` (which is `14`). The constant is right; the *name* attached was the off-by-one neighbour. | [appendix/memory-space-table.md](memory-space-table.md) | CERTAIN |
| **FLAG-CAT-01** | `xla_tpu_*` flag family tabulated at **968** "settable knobs." | **909** *registered* flags (from `AbslFlagHelpGenFor`); 968 is the count of distinct `xla_tpu_*` name *strings* in `.rodata` (909 registered + 59 rodata-only). 909 = "what you can pass"; 968 = "strings that exist." | [appendix/flag-catalog-full.md](flag-catalog-full.md) | HIGH |
| **CODEC-01** | The v5 codec named `tpu::TpuCodec6acc60406`. | The case-5 codec (`TpuCodec::Create` @ `0x1e835fa0` → `sub_1E838380`) constructs an **anonymous** class; the `6acc60406` token is a hardware codename, not a class name. | [targets/codename-superseded-labels.md](../targets/codename-superseded-labels.md) | HIGH |
| **PIN-01** | Ghostlite↔`0xd` / 6acc60406↔`0xc` device-type binding taken on cross-reference only. | `DeviceTypeFromDeviceIdentifiers` (`0xf6993a0`) **byte-pins** the `0xd`/`0xc` stores directly. | [targets/codename-superseded-labels.md](../targets/codename-superseded-labels.md) | HIGH |
| **ELF-1** | Object recorded as `sections=51`. | `readelf -h` reports **52** section headers; 51 counts meaningful sections, 52 includes the mandatory `NULL` section `[0]`. Both consistent once the `NULL` slot is accounted for. | [forensics/elf-anatomy.md](../forensics/elf-anatomy.md) | CERTAIN |
| **ELF-2** | Scratch note: `build_id=<none>`, `soname=<none>`. | `soname` is genuinely absent, but the build-id **is present** = `89edbbe81c5b328a958fe628a9f2207d`. The `<none>` was a tooling miss, not a binary fact — the pin every page depends on. | [forensics/elf-anatomy.md](../forensics/elf-anatomy.md) | CERTAIN |
| **SCS-ENUM** | A single SparseCore sequencer-type numbering. | Two enum spaces number the sequencers off by one; the wiki standardizes on the **codec-template** enum the encoder instantiations carry (`SCS=3`, `TAC=4`, `TEC=5`). | [sparsecore/scs-engine.md](../sparsecore/scs-engine.md) | HIGH |
| **EUP-1** | `V*Decomposed` transcendental builders interleave VALU correction (Newton refinement / `VfastTwoSum`) between push and pop. | Byte-exact disassembly shows **no** interleaved correction arithmetic in those builders; the push/pop pairing is plain. The Newton-refinement reading was an over-eager inference. | [isa/slot-eup-transcendental.md](../isa/slot-eup-transcendental.md) | HIGH |

> **GOTCHA (CORR-SHAPE) —** notice the recurring *shape* of these corrections: a single number that conflated two distinct surfaces (SYM-NS-1/2, FLAG-CAT-01), an enum constant whose attached *name* was the off-by-one neighbour (MST-1/2, SCS-ENUM), or an offset/feature read as one thing that the decompile pins as another (ZSTD-01, PDT-1). These are the three failure modes static RE produces most, and they are exactly what cross-checking a consolidated appendix table against the binary catches. A reimplementer should be most suspicious of *any single count* and *any name-to-ordinal binding* taken on cross-reference rather than a byte-pinned store.

---

## Methodology Evolution

The corrections above were not a single audit pass; they accumulated as the analysis loop changed shape. The arc, read off the tags:

1. **Early scratch-fingerprint phase.** The first pass recorded coarse fingerprints (`sections=51`, `build_id=<none>`, the substring-count namespace figures). Several of these were tooling artifacts — `ELF-2` is the central example: the build-id every later page pins to was initially missed entirely. Lesson: re-run the primitive tools (`readelf -n`, `readelf -h`) and trust their output over a hand-copied note.

2. **Inference-rich middle phase.** As pages reconstructed structure, plausible-but-unverified inferences crept in: the zstd "blob" (`ZSTD-01`), the Newton-refinement reading in the EUP builders (`EUP-1`), the `+0x438` "hash" (`PDT-1`). Each was overturned by going from *plausible interpretation* to *byte-exact disassembly of the actual instructions*. Lesson: an interpretation that has not been confirmed against the literal bytes is a hypothesis, and the callout convention exists to keep hypotheses visibly tagged until they are.

3. **Consolidation / appendix phase.** Building the master tables forced cross-checking independently-derived numbers against one source of truth, which surfaced the surface-conflation and name-vs-ordinal corrections (`SYM-NS-1/2`, `FLAG-CAT-01`, `MST-1/2`, `DISP-1`). Lesson: aggregation is itself a verification technique — two numbers that "should" match and don't are a correction waiting to be filed.

The correction convention (defined on [evidence-conventions.md](../front/evidence-conventions.md), detailed methodology on [methodology.md](../methodology.md) and [appendix/methodology-deep.md](methodology-deep.md)) is the through-line: a conclusion that changes is never silently edited. The old claim stays, the correction sits beside it with a tag, and pages like this one can audit the full set. That is what makes the reconstruction a *living, self-correcting* artifact rather than a frozen snapshot of first impressions.

> **NOTE (LIVING) —** 249 tagged correction callouts (231 distinct tags) across 427 pages is roughly one filed reversal every other page. That density is the point. It is the measurable signal that the hard claims were genuinely re-examined against the binary, not asserted once and left.

---

## Cross-References

- [forensics/trailing-zstd-blob.md](../forensics/trailing-zstd-blob.md) — the headline correction (`ZSTD-01`); the full resolution of the withdrawn "trailing blob" claim.
- [front/evidence-conventions.md](../front/evidence-conventions.md) — defines the `> **CORRECTION (tag) —**` callout convention this page indexes.
- [methodology.md](../methodology.md) — the analysis loop that produced (and overturned) these claims.
- [appendix/methodology-deep.md](methodology-deep.md) — the deep version of the methodology, including how byte-exact disassembly settles inferences.
- [glossary.md](../glossary.md) — owns `GLOSS-1`, the canonical flagged-absence correction (`walrus`).
- [appendix/symbol-namespace-index.md](symbol-namespace-index.md) — owns `SYM-NS-1/2`, the participation-vs-ownership count disambiguation.
- [appendix/memory-space-table.md](memory-space-table.md) — owns `MST-1/2`, the byte-exact memory-space enum numbering.
- [appendix/flag-catalog-full.md](flag-catalog-full.md) — owns `FLAG-CAT-01`, the registered-vs-string flag count split.
- [forensics/dispatch-table-taxonomy.md](../forensics/dispatch-table-taxonomy.md) — owns `DISP-1`, the 17→19 dispatch-class correction.
- [index.md](../index.md) — the full Part roster and page index this page summarizes.
