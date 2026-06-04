# Sub-Core Taxonomy (GFC/GLC/JXC/PXC/VFC/VLC)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

libtpu's driver layer is organized under one umbrella namespace, `asic_sw::driver::deepsea::`, with one sub-namespace per HAL family: `jxc`, `pxc`, `vxc`, `gxc`. Inside each family the code is partitioned into *sub-cores* — the per-engine instruction-stream handlers that the on-chip compiler targets. The single most important axis of this taxonomy is the **fetch/load-core split**: whether a family routes a core's instruction stream through one fused dataflow or through two cooperating cores (a fetch-core that reads/issues and a load-core that stages data).

The split has a clear chronological origin. [JXC](jxc-family.md) (Jellyfish, Dragonfish) has **no split** — its dataflow is fused, and its sub-namespaces are organized by engine block (`dfc`, `jfc`, `registers`, `snap`, trace-entry types). Starting with [PXC](pxc-family.md) (Pufferfish), every family adopts a **fetch+load split**: PXC has `pfc`+`plc`, [VXC](vxc-family.md) has `vfc`+`vlc`, and [GXC](gxc-family.md) has `gfc`+`glc`. The six tokens of this page's title are these sub-cores. They are not arbitrary labels: they appear in the symbol table as real C++ namespaces, each (for the split families) carrying its own `isa` and `profiler` sub-namespace.

This page unifies what the four family pages document individually. It establishes (1) the split-evolution timeline, (2) the verified per-family sub-namespace roster from the symbol table, and (3) the relationship between the sub-cores and the profiler `TraceEntry` classes that motivated grouping them. It is the canonical reference for "which sub-cores exist and what they mean"; the per-family pages carry the factory and construction detail.

For reimplementation, the contract is:

- The split timeline: JXC fused → PXC introduces fetch+load → VXC inherits it → GXC pushes ISA fully into the sub-cores. A reimplementation must model one pipeline per core for JXC and two cooperating cores for every later family.
- The verified namespace roster: which `asic_sw::driver::deepsea::<family>::<sub>` namespaces actually exist (not the prefixes-inside-type-names that look like namespaces).
- The codename ↔ sub-core map, including the GXC pairing (Ghostlite=glc, 6acc60406=gfc) that is easy to invert.
- The profiler `TraceEntry` set (five classes, not six) and how it diverges from the six sub-cores.

| | |
|---|---|
| **Umbrella namespace** | `asic_sw::driver::deepsea::` |
| **Families** | `jxc` (fused), `pxc`, `vxc`, `gxc` (all split) |
| **Fetch/load split origin** | PXC (Pufferfish, v2) — JXC is the lone fused family |
| **Sub-cores (split families)** | `pfc`/`plc`, `vfc`/`vlc`, `gfc`/`glc` |
| **`TraceEntry` classes** | 5 — `pxc` (family-level), `vxc::vfc`, `vxc::vlc`, `gxc::gfc`, `gxc::glc` |
| **Evidence** | `*_functions.json` symbol roster; mangled `asic_sw::driver::deepsea::*` namespaces |

---

## The Fetch/Load Split Evolution

### The fused era (JXC)

JXC carries a single fused dataflow. There is no fetch-core / load-core distinction in its namespace tree: the direct children of `asic_sw::driver::deepsea::jxc::` are engine blocks (`dfc` — dataflow controller, `jfc` — Jellyfish core, `registers`, `snap`), generation-specific performance counters (`jellyfish_performance_counters`, `dragonfish_performance_counters`), and a family of `*_trace_entry` event types. A reimplementation of JXC models one instruction pipeline per core; there is no second staging core to coordinate.

### The split era (PXC → VXC → GXC)

Beginning with Pufferfish, each core's work is divided between a **fetch-core** (instruction fetch and issue) and a **load-core** (data staging). The split is visible as two sibling sub-namespaces per family:

```text
asic_sw::driver::deepsea::
  ├─ jxc/                      (FUSED — no split)
  │    dfc, jfc, registers, snap, *_performance_counters, *_trace_entry
  ├─ pxc/                      (SPLIT introduced)
  │    ├─ pfc/                 ── Pufferfish fetch-core   (isa, profiler, b0)
  │    ├─ plc/                 ── Pufferfish load-core    (profiler)
  │    ├─ isa/                 ── family-level ISA
  │    └─ profiler/            ── family-level profiler (holds TraceEntry)
  ├─ vxc/                      (SPLIT inherited)
  │    ├─ vfc/                 ── vector fetch-core        (isa, profiler/TraceEntry)
  │    ├─ vlc/                 ── vector load-core         (profiler/TraceEntry)
  │    └─ isa/                 ── family-level ISA
  └─ gxc/                      (SPLIT; ISA only under sub-cores)
       ├─ gfc/                 ── general fetch-core (6acc60406/v5)  (isa, profiler/TraceEntry)
       └─ glc/                 ── general load-core  (Ghostlite/v4)  (isa, profiler/TraceEntry)
```

> **NOTE —** the split moved progressively *deeper*. PXC and VXC keep a family-level `isa` namespace *and* fetch/load sub-cores. GXC has **no** family-level `isa` (the symbol search `deepsea3gxc3isa` returns zero); its entire ISA lives under `gxc::gfc::isa` and `gxc::glc::isa`. So the architectural trend is fused (JXC) → split with shared family ISA (PXC, VXC) → split with per-sub-core ISA (GXC).

### Why the split exists

The fetch/load split decouples instruction issue from data movement, letting the load-core prefetch and stage operands while the fetch-core continues to issue — the standard rationale for separating an issue pipe from a load/store pipe. It arrives in PXC alongside two other PXC-era changes that point the same way: DMA moves out of a standalone issuer object (JXC's `JfDmaIssuer`) and into the driver itself, and the DMA descriptor advances from the V1 32-byte single-level form to the V2 ≥96-byte 4-level-strided form. The split is the instruction-side counterpart to the richer data-movement model.

---

## Per-Family Sub-Namespace Roster (Verified)

This roster is taken directly from the `*_functions.json` symbol table — the strongest available evidence for what actually exists. Counts are occurrence counts of the mangled namespace token; "—" means the namespace is absent.

| Sub-namespace | JXC | PXC | VXC | GXC |
|---|---|---|---|---|
| fetch-core | — (fused) | `pfc` | `vfc` | `gfc` |
| load-core | — (fused) | `plc` | `vlc` | `glc` |
| family-level `isa` | — | `pxc::isa` (137K) | `vxc::isa` (170K) | — (absent) |
| family-level `profiler` | — | `pxc::profiler` (8K) | — | — |
| sub-core `isa` | — | `pfc::isa` (46K) | `vfc::isa` (69K) | `gfc::isa` (270K), `glc::isa` (294K) |
| sub-core `profiler` | — | `pfc`, `plc` | `vfc`, `vlc` | `gfc`, `glc` |
| engine blocks | `dfc`, `jfc`, `registers`, `snap` | `internal`, `pfc::b0` | — | — |

> **NOTE —** the `bcs`/`brn`/`hbm`/`hib`/`ici` tokens are *not* JXC sub-namespaces — they are prefixes inside `*_trace_entry` type names (e.g. `bcs_internal_trace_entry`, `ici_packet_trace_entry`). JXC has **no `jxc::isa`** at all: the Jellyfish/Dragonfish compiler-side ISA lives in `platforms_deepsea::jellyfish::isa` (the shared compiler-base namespace, e.g. `platforms_deepsea::jellyfish::isa::BundleSlot`, `MiscOpcode`; the demangled-symbol search `xla::jellyfish::isa` returns zero, `platforms_deepsea::jellyfish::isa` returns 3122). `jellyfish`/`dragonfish` appear only as `jellyfish_performance_counters` / `dragonfish_performance_counters`, never as bare namespaces.

> **NOTE —** GXC's ISA and profiler live at the *sub-core* level: `gxc::gfc::isa`, `gxc::glc::isa`, `gxc::gfc::profiler`, and `gxc::glc::profiler` all exist and are large. What GXC lacks is a *family-level* `gxc::isa`/`gxc::profiler` (PXC and VXC have those; GXC pushes ISA down to the sub-cores). GXC sits inside the VXC family only at the HAL-object level (shared factory and impl — see [GXC Family](gxc-family.md)); its driver ISA is wholly its own.

---

## The Six Sub-Cores and the Codename Mapping

The six sub-cores map to silicon codenames as follows. JXC is included for completeness as the fused predecessor; it has no fetch/load sub-cores, so its row names the family rather than a sub-core.

| Sub-core | Family | Role | Codename(s) | TpuVersion |
|---|---|---|---|---|
| (fused) | JXC | single fused dataflow | Jellyfish, Dragonfish | 0, 1 |
| `pfc` | PXC | Pufferfish fetch-core | Pufferfish | 2 |
| `plc` | PXC | Pufferfish load-core | Pufferfish | 2 |
| `vfc` | VXC | vector fetch-core | Viperfish | 3 |
| `vlc` | VXC | vector load-core | Viperfish (Viperlite) | 3 |
| `glc` | GXC | general load-core | **Ghostlite** | **4** |
| `gfc` | GXC | general fetch-core | **6acc60406** | **5** |

> **GOTCHA —** the GXC codename pairing is the easiest thing on this page to get wrong. **Ghostlite (v4) = `glc`** (load-core); **6acc60406 (v5) = `gfc`** (fetch-core). The codec walks pin it at the symbol level: `TpuCodecGhostlite` dispatches only to `gxc::glc::isa` + `ghostlite::isa::EncoderGl*`; the anonymous v5 codec dispatches only to `gxc::gfc::isa`. The binary's external-name strings keep the two a generation apart — Ghostlite resolves to `TPU v6 lite` (the `TPU v6e`/`TPU v6 lite` band), 6acc60406 to `TPU7x` — so pairing `gfc` with a "v6" name is a generation off-by-one. The canonical version↔external-name reconciliation is the [Codename Matrix](tpu-version-codename-matrix.md).

---

## The Profiler Trace-Entry Classes

The sub-cores were originally grouped because the profiler emits a per-sub-core `profiler::TraceEntry` event class. The symbol table shows this class exists in **five** namespaces, not six — and not in the obvious one-per-sub-core pattern:

| Namespace holding `profiler::TraceEntry` | Token count | Granularity |
|---|---|---|
| `pxc::profiler::TraceEntry` | 3087 | **family-level** (not split into pfc/plc) |
| `vxc::vfc::profiler::TraceEntry` | 4338 | sub-core (fetch) |
| `vxc::vlc::profiler::TraceEntry` | 3326 | sub-core (load) |
| `gxc::gfc::profiler::TraceEntry` | 4781 | sub-core (fetch) |
| `gxc::glc::profiler::TraceEntry` | 4590 | sub-core (load) |

The `TraceEntry` class consumes a `TpuXPlaneBuilder` and produces `tsl::profiler::XEventBuilder` events (`ProcessTraceEntry`, `UpdateContext` methods), feeding the XLA profiler's XPlane. Each instance is keyed by a `ChipCoreId` and threads `JfTrace_RunDebugInfo` vectors and offload-context lookup maps.

> **GOTCHA —** the unified `profiler::TraceEntry` class is *not* one-per-sub-core. **JXC has no `profiler::TraceEntry` class** — its profiler support is realized through per-engine `*_trace_entry` *types* (e.g. `ici_packet_trace_entry`), not a unified `TraceEntry`. And **PXC's `TraceEntry` is at family level** (`pxc::profiler::TraceEntry`), not split into `pfc`/`plc`; the `pfc`/`plc` profilers instead hold control-interface and limits-factory classes (`TracemarkLimitsFactory`, `EveryoneTraceControlFactory`). The unified `TraceEntry` class therefore exists in exactly five places: PXC (family), VFC, VLC, GFC, GLC. The six *sub-cores* (the fetch/load namespaces) and the five *trace-entry classes* are distinct sets — they coincide cleanly only for VXC and GXC.

---

## The Deepsea Umbrella and the Compiler-Base Namespace

"deepsea" is the umbrella project; the per-silicon driver families (`jxc`/`pxc`/`vxc`/`gxc`) are children of `asic_sw::driver::deepsea::`. But there is a second, parallel use of "deepsea" and "jellyfish" that a reimplementer must not conflate with the driver tree: the **compiler base**. It is split across two top-level namespaces — `platforms_deepsea::jellyfish::isa` holds the shared ISA primitives, and `xla::jellyfish::` holds the codec, the per-codename compiler targets, and the cost models. There is no `xla::jellyfish::isa` (the `isa` sub-namespace lives only under `platforms_deepsea::`).

```text
deepsea (umbrella)
  ├─ COMPILER-BASE (generation-agnostic ISA + codec)
  │    ├─ platforms_deepsea::jellyfish::isa   ── shared ISA primitives (BundleSlot, MiscOpcode, …)
  │    ├─ ghostlite::isa                      ── named v4 worker encoders/decoders (EncoderGl*, DecoderGl*)
  │    ├─ viperfish::isa                      ── named v3 worker encoders/decoders (EncoderVf*, DecoderVf*)
  │    └─ xla::jellyfish::                     ── codec + targets + cost models
  │         ├─ CompactProgram<...>            ── templated over gxc::{gfc,glc}::isa bundle types
  │         ├─ JellyfishTarget / DragonfishTarget   ── per-codename compiler targets
  │         └─ JfCycleTable / GfcCycleTable / GlcCycleTable   ── per-gen cost models
  └─ asic_sw::driver::deepsea::                ── the DRIVER tree (this page's subject)
       jxc, pxc, vxc, gxc + their sub-cores
```

The two trees meet at the codec layer: a `TpuCodec*` object (compiler-side, under `xla::jellyfish::CompactProgram`) emits bundles whose types live under the driver tree's sub-core ISA — e.g. `xla::jellyfish::CompactProgram<asic_sw::deepsea::gxc::glc::isa::TensorCoreBundleCompact>`. So the compiler base is generation-agnostic and the per-generation specialization is the sub-core ISA bundle type plugged into it.

> **GOTCHA —** because the compiler base is named `jellyfish`, a search for "jellyfish ISA" lands in `platforms_deepsea::jellyfish::isa`, NOT in any `jxc::isa` (and not in `xla::jellyfish::isa`, which has zero symbols — `xla::jellyfish::` holds the codec, targets, and cost models, but the ISA *primitives* are under `platforms_deepsea::`). JXC's *driver* namespace has no `isa` at all. A reimplementer wiring up JXC must look for the ISA in the compiler-base namespace, not under the JXC driver family. This is the same reason `jxc::jellyfish` and `jxc::dragonfish` do not exist as namespaces — the codename-specific *driver* state is in `*_performance_counters` and `*_trace_entry`, while the codename-specific *compiler* state is `xla::jellyfish::JellyfishTarget` / `DragonfishTarget`.

---

## Sub-Cores and the Codec / Bundle ISA Layer

The sub-core that matters most for a compiler-backend reimplementation is the one that owns the on-chip bundle ISA. For the split families this is a per-sub-core `isa` namespace, and its central type is a `TensorCoreBundleCompact` (the packed instruction bundle the codec encodes and decodes):

| Sub-core ISA | Bundle-compact type present | Token count |
|---|---|---|
| `pxc::pfc::isa` | `BarnaCoreChannelBundle`, `VectorBase` | 46K |
| `vxc::vfc::isa` | SparseCore Scs/Tac bundle types | 69K |
| `gxc::glc::isa` | `TensorCoreBundleCompact` (Ghostlite/v4) | 294K |
| `gxc::gfc::isa` | `TensorCoreBundleCompact` (6acc60406/v5) | 270K |

The codec for each version binds *exclusively* to one sub-core ISA. The `TpuCodecGhostlite` codec dispatches only to `gxc::glc::isa` (+ the named `ghostlite::isa::EncoderGl*` workers); the anonymous v5 codec dispatches only to `gxc::gfc::isa`; the `TpuCodecViperfish` codec binds to `vxc::vfc`/`vlc` and `viperfish::isa`. This exclusive binding is the surest symbol-level evidence for the codename ↔ sub-core map, because the codec methods are decoded function bodies, not heuristics.

> **NOTE —** the presence of `TensorCoreBundleCompact` under both `gxc::gfc::isa` and `gxc::glc::isa` (and *not* under a shared `gxc::isa`) is what pins GXC's ISA to the sub-core level. The two GXC codecs differ in their bundle encoding even at the bit level — Ghostlite uses a 7-bit opcode with a 4-bit per-slot predicate, 6acc60406 widens the opcode to 8 bits and shrinks the per-slot predicate to a 2-bit dual form — so a single shared GXC ISA would be incorrect; the two sub-core ISAs are genuinely distinct generations. See [GXC Family](gxc-family.md) for the bit-level deltas.

---

## The Four Families at a Glance

A single grid relating each family to its split state, sub-cores, ISA placement, DMA model, and HAL product. This is the consolidated cross-family view that the four individual pages each present from their own perspective.

| Axis | JXC | PXC | VXC | GXC |
|---|---|---|---|---|
| Codenames | Jellyfish, Dragonfish | Pufferfish | Viperfish | Ghostlite, 6acc60406 |
| TpuVersions | 0, 1 | 2 | 3 | 4, 5 |
| Fetch/load split | none (fused) | `pfc`/`plc` | `vfc`/`vlc` | `gfc`/`glc` |
| Factory class | `TpuHalJxcHardwareFactory` (anon) | `TpuHalPxcHardwareFactory` (anon) | `TpuHalVxcHardwareFactory` (global) | none — uses VXC factory |
| Factory vtable | 0x215fe530 | 0x216085c8 | 0x21cabf70 | (VXC's 0x21cabf70) |
| HAL impl size | 208 B | 208 B | 216 B | 216 B (VXC's) |
| ISA placement | `platforms_deepsea::jellyfish::isa` (compiler-base) | family + sub-core | family + sub-core | sub-core only |
| DMA model | separate `JfDmaIssuer` | in `TpuPxcDriver` | in `TpuVxcDriver` | in `TpuVxcDriver` |
| DMA descriptor | V1 (32 B) | V2 (≥96 B) | V2 | V2 |
| TensorCore | yes | yes | yes | yes |
| BarnaCore | yes | yes (last gen) | no | no |
| SparseCore | no | no | yes (first gen) | yes |
| `profiler::TraceEntry` | none | family-level | per sub-core | per sub-core |

> **NOTE —** the table reads as a clean generational progression on every axis: the fetch/load split, V2 DMA descriptor, and DMA-in-driver all arrive together at PXC; SparseCore arrives and BarnaCore retires together at VXC; and the ISA placement migrates steadily inward (compiler-base only → family + sub-core → sub-core only). The HAL-impl size is the lone exception — it is 208 B for three families and 216 B only for VXC/GXC, purely because of the single `+0xD0` slice-builder flag those two need.

---

## Why the Families Are Named As They Are

The four family tags (`jxc`, `pxc`, `vxc`, `gxc`) follow a `_xc` suffix convention where the leading letter ties to the family's "home" codename or core class:

- **JXC** — **J**ellyfish; the family is named for its first codename, and the fused core engine is `jfc` (Jellyfish core).
- **PXC** — **P**ufferfish; the single codename it serves; cores `pfc`/`plc` are **P**ufferfish-fetch / **P**ufferfish-load.
- **VXC** — **V**iperfish; the home codename; cores `vfc`/`vlc` are **v**ector-fetch / **v**ector-load.
- **GXC** — **G**eneral; the only family whose tag is *not* a codename. Its cores `gfc`/`glc` are **g**eneral-fetch / **g**eneral-load, and it hosts two codenames (Ghostlite, 6acc60406) rather than being named for one. This abstraction is consistent with GXC having no factory of its own — it is the "general" extension family layered over VXC's HAL.

> **QUIRK —** the `g` in GXC stands for "general", not "Ghostlite". A reimplementer who reads `gfc` as "Ghostlite-fetch-core" will mis-pair the codenames: Ghostlite is the *load*-core (`glc`), and the fetch-core (`gfc`) is 6acc60406. The general-vs-codename naming is the structural tell that GXC is an extension family, not a standalone one.

---

## Evidence Method

The taxonomy is recovered from the IDA `*_functions.json` export — the symbol names plus their decompiled bodies — not from any single decompiled function. Each driver namespace appears in Itanium-mangled form as `asic_sw6driver7deepsea3<famlen><fam>3<sublen><sub>...` (e.g. `deepsea3gxc3glc3isa` for `asic_sw::driver::deepsea::gxc::glc::isa`). The counts in the tables above are raw occurrences of each length-prefixed token across that export (so they scale with how heavily a namespace is *referenced*, not with its distinct-symbol count — the binary's own symbol table is sparser; the demangled-symbol tally of `gxc::glc::isa`, for instance, is ~68K against the 294K token occurrences). Checking the *character that follows* a token distinguishes a real sub-namespace from a token that is merely the prefix of a longer type name. This is how the JXC `bcs`/`brn`/`hbm`/`hib`/`ici` "namespaces" were shown to be `*_trace_entry` type-name prefixes, and how the absence of a family-level `gxc::isa` (`deepsea3gxc3isa` → zero matches) was established.

---

## Reimplementation Notes

| Concern | Guidance |
|---|---|
| Modeling JXC | One fused pipeline per core; no load-core; DMA via a separate `JfDmaIssuer` object; ISA in `platforms_deepsea::jellyfish::isa` |
| Modeling PXC/VXC | Two sub-cores (fetch + load) per core; family-level `isa`; DMA folded into the driver; V2 descriptor |
| Modeling GXC | Two sub-cores with ISA *only* under the sub-cores; reuses the VXC HAL product chain; Ghostlite=glc, 6acc60406=gfc |
| Profiler | Expect a unified `TraceEntry` class for PXC (family), VFC, VLC, GFC, GLC; JXC uses per-engine `*_trace_entry` types |
| Codename ↔ sub-core | Use the verified table above; do not infer fetch vs load from the version number |

---

## Cross-References

- [Part IV Overview](overview.md) — the Silicon & Codename hub; where the fetch/load split sits in the `TpuVersion` dispatch model
- [JXC Family](jxc-family.md) — the fused-dataflow family; the no-split baseline and `platforms_deepsea::jellyfish::isa`
- [PXC Family](pxc-family.md) — origin of the fetch/load split; `pfc`/`plc`; family-level ISA and profiler
- [VXC Family](vxc-family.md) — `vfc`/`vlc`; first SparseCore family; the per-codename `InitializeDrivers` switch
- [GXC Family](gxc-family.md) — `gfc`/`glc`; per-sub-core ISA; Ghostlite/6acc60406 codename pairing
- [HAL Families](hal-families.md) — the shared `TpuHalFactory` base chain across all four families
- [Codename Matrix](tpu-version-codename-matrix.md) — the 6-value `TpuVersion` enum and HAL routing
