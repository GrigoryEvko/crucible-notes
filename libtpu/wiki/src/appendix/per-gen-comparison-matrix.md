# Per-Gen Comparison Matrix

> *Every per-generation constant on this page was decoded byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every accessor is a demangled C++ name; `.text` / `.rodata` / `.lrodata` map VMA == file offset and `.data.rel.ro` maps VMA − `0x200000` == file offset. Other builds will differ.*

## Abstract

This appendix is the single master table that lines up every TPU generation `libtpu.so` knows about — `jellyfish` (v2), `dragonfish` (v3), `pufferfish` (v4, + `v4 lite`), `viperfish` (v5p, + `v5e` lite), `ghostlite` (v6e), `6acc60406` (v7) — side by side across every recovered hardware constant a reimplementer needs to model a generation: bundle byte size, the lane/sublane/chunk geometry, the MXU / XLU / IAR execution-unit counts and dimensions, the per-tier memory capacities (VMEM, SMEM, SFLAG, CMEM, HBM), the cost-model class trio (LatencyTable / CycleTable / Performance subclass), local DMA bandwidth, ICI/PCIe bandwidth, the accelerator-core type (BarnaCore vs SparseCore, with the SCS/TAC/TEC sequencer split), and the three independent version ordinals. Each column's deep derivation lives on a dedicated cost / isa / targets page; this page is the cross-gen index those pages point back to.

Three facts make the consolidation worth doing on one page. First, almost every per-die geometry constant flows from one source — the embedded `<codename>_chip_parts.binarypb` proto, selected by `TpuChipParts::DefaultsForVersion` (`0x20b1b040`) and materialized into the runtime `Target` / `TpuTopology` objects at boot — so the whole matrix is anchored to one load path rather than to scattered C++ literals. Second, two constants that *look* per-gen are actually gen-stable (`lane_count` = 128, `sublane_count` = 8, `iar_count` = 2 for every generation); the genuinely per-gen ISA-geometry knobs are `mxu_count` (1→2→4→4→2→2) and `xlu_count` (1→1→2→3→2→2). Third, the cost model collapses 6 versions onto 5 class boundaries at the silicon-architecture seams: v2 and v3 share one `LatencyTable` class (split only by a `DeviceIdentifiers` compare into `PerformanceJf`/`PerformanceDf`), and v6e and v7 share `GhostlitePerformance` behind distinct wrapper classes. The matrix below makes those merges explicit.

For navigation, the contract is:

- **The master matrix** is one row per generation, the headline constants as columns, each row carrying a Confidence. It is the canonical lookup; the rest of the wiki links here.
- **Grouped detail tables** then split the matrix by subsystem — geometry, compute units, memory tiers, cost-model classes, interconnect, and the BarnaCore↔SparseCore pivot — because no single 30-column table is readable.
- **Callouts** flag the traps: the Viperfish std/lite variant split decided at runtime by a string compare, the v7 `6acc60406` dropping the TAC sequencer, the `v4`-only CMEM tier, and the cells this build genuinely cannot confirm.

| | |
|---|---|
| **Version axis (internal)** | `tpu::TpuVersion` `0..5` — `TpuVersionToString` @ `0x20b3a480`, table `off_22011BF0` (6 ptrs) |
| **Per-die geometry source** | `<codename>_chip_parts.binarypb` → `TpuChipParts::DefaultsForVersion` @ `0x20b1b040` → `FromProto` |
| **Geometry cache** | `tpu::TpuTopology` ctor @ `0x20acee60` (lane→`+0x198`, sublane→`+0x1a0`); read via `Target[+0x3b8]` |
| **ISA-geometry source** | `VectorIsa` sub-message (`mxu`/`xlu`/`iar`) → `Target+0x4ac`/`+0x4b0`/`+0x4a8` |
| **Cost-model factories** | `LatencyTable::Create` @ `0x1c89fba0` (version-indexed); `CycleTable::Create` @ `0x1c89cc00` (Target-keyed) |
| **Confidence** | CONFIRMED (byte-anchored) unless a cell or callout says otherwise |

---

## The Master Per-Gen Matrix

One row per generation, in `tpu::TpuVersion` order. The cells are the headline per-die (single-tensornode) values; lite-variant and full-chip deltas live in the grouped tables below. The **Confidence** column applies to the whole row's binary-anchored cells.

| Generation | `TpuVer` | Codec | Bundle | Lane×Sub | Chunks/Tile | MXU (count×geom) | XLU | IAR | VMEM | SMEM | SFLAG | CMEM | Accel core | Cost trio | Confidence |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **Jellyfish** v2 | 0 | `jxc` | 41 B | 128×8 | 16 | 1 × 128² | 1 | 2 | 16 MiB | 16 KiB | 1 KiB | — | BarnaCore ×2 | JF / Jf / `PerformanceJf` | CERTAIN |
| **Dragonfish** v3 | 1 | `jxc` | 41 B | 128×8 | 16 | 2 × 128² | 1 | 2 | 16 MiB | 16 KiB | 1 KiB | — | BarnaCore ×2 | JF / Jf / `PerformanceDf` | CERTAIN |
| **Pufferfish** v4 | 2 | `pxc` / `pfc` | 51 B | 128×8 | 16 | 4 × 128² | 2 | 2 | 16 MiB | 1 MiB | 2 KiB | **128 MiB** | BarnaCore ×4 | PF / Pf / `PufferfishPerformance` | CERTAIN |
| **Viperfish** v5p | 3 | `vxc` / `vfc` | 64 B | 128×8 | 16 | 4 × 128² | 3 | 2 | 64 MiB | 1 MiB | 2 KiB | — | SparseCore ×4 | VF / Vf / `ViperfishPerformance` | CERTAIN |
| **Ghostlite** v6e | 4 | `gxc` / `glc` | 64 B | 128×8 | 16 | 2 × 256² | 2 | 2 | 128 MiB | 1 MiB | 2 KiB | — | SparseCore ×2 | GL / Glc / `GhostlitePerformance` | CERTAIN |
| **6acc60406** v7 | 5 | `gxc` / `gfc` | 64 B | 128×8 | 16 | 2 × 256² | 2 | 2 | 64 MiB | 1 MiB | 16 KiB | — | SparseCore ×2 | Gf / Gfc / `GhostlitePerformance` | CERTAIN |

> **NOTE —** the v7 `6acc60406` blob ships in this build (both the single-die `tensornode` and the 2-die full-chip variant), so its memory/ISA constants are decoded straight from the proto wire bytes. The v2–v6e blobs are *also* embedded in this build (nine `chip_parts.binarypb` blobs total, contiguous in `.rodata` at `0xbdf29a0..`), so the older-gen lane/sublane/MXU/memory values are likewise proto-sourced, not inferred — the `chip_parts` HBM/VMEM/SMEM/SFLAG bytes, the MXU `VectorIsa`, and the per-gen frequencies are all materializable. Cost-model class selection (`LatencyTable`/`CycleTable`/`Performance`) is decided purely by the `TpuVersion` ordinal via the two factories named above.

> **GOTCHA —** `lane_count` (128), `sublane_count` (8), and `iar_count` (2) do **not** grow with the generation. A reimplementer who expects the lane width to widen at v5/v7 (as the HBM clock and VMEM do) will mis-model every tile loop. The lane/sublane growth is a *constant fallback* the `TpuTopology` ctor (`0x20acee60`) installs as `128`/`8` when the proto omits the field; every blob also carries `128`/`8` explicitly, so both paths agree. The only per-gen `VectorIsa` deltas are `mxu_count` and `xlu_count`.

---

## Geometry — Lane / Sublane / Chunk

The transpose / tiling geometry is a `chip_parts` property cached on the `TpuTopology`, read through the `Target` accessors. The whole matrix is numerically identical across generations because every blob's TensorCore `VectorIsa` reports `lane_count` = 128, `sublane_count` = 8.

| Constant | Accessor | Value (all gens) | Source | Confidence |
|---|---|---|---|---|
| `LaneCount` | `Target::LaneCount` @ `0x1d60f400` | 128 | `*(Target[+0x3b8] + 0x198)` ← `VectorIsa.lane_count` | CERTAIN |
| `SublaneCount` | `Target::SublaneCount` @ `0x1d60f300` | 8 | `*(Target[+0x3b8] + 0x1a0)` ← `VectorIsa.sublane_count` | CERTAIN |
| `ChunksPerTile` | `Target::ChunksPerTile` @ `0x1d60f2c0` | 16 | `LaneCount / SublaneCount` (idiv, 32-bit fast path) | CERTAIN |
| Tile element count | `TpuTopology[+0x1a8]` | 1024 | `LaneCount × SublaneCount` (ctor `@0x20acf2e8`) | CERTAIN |

The write path is unambiguous: the `TpuTopology` ctor (`0x20acee60`) gates on the platform-has-TensorCore byte (`topo[+0x80]`), reads `CoreParts(TENSOR_CORE).SequencerParts(0).vector_isa()`, checks the matrix-unit has-bit, then stores `LaneCount` ← `vector_isa[+0x0]` (`@0x20acf2bc`) and `SublaneCount` ← `vector_isa[+0x4]` (`@0x20acf2c6`), with the hard-coded `128`/`8` fallback at `@0x20acf2cc`/`@0x20acf2dc`. `Target::Init` (`0x1d60fc20`) then installs the `TpuTopology*` at `Target[+0x3b8]` (`mov [rcx+0x3b8],rdi`).

> **NOTE —** the lane/sublane geometry is **not** written by `tpu::TpuChipConfig::Create` (`0x20ae98e0`). `TpuChipConfig::Create` resolves a config alias via the `kChipConfigAliases` flat-map (`0x2200b8b0`) and builds the *driver-side* memory/queue layout; it does not touch `LaneCount`/`SublaneCount`. The geometry is a `TpuChipParts`/`TpuTopology` property, written by the `TpuTopology` ctor from the MXU `VectorIsa`. The `cfg` in `MaximumNumberOfChunks` is the `TpuTopology` object (`Target[+0x3b8]`), not a separate config struct.

> **NOTE — which generation reads which `kChipConfigAliases` entry.** `kChipConfigAliases` (`0x2200b8b0`) is not keyed by a runtime-probed device string; it is a static `gtl::flat_map<tpu::TpuVersionAndVariant, MapView<string_view, string_view>>` with exactly **four** inline entries, keyed by `TpuVersion` ordinal **{2, 3, 4, 5}** — each with variant `"default"` (the version field is the literal byte at each 48-byte entry's `+0x0`: `02`/`03`/`04`/`05`, byte-confirmed in `.data.rel.ro`). `TpuChipConfig::Create(TpuVersion, string_view variant)` (`0x20ae98e0`) looks the pair up with `gtl::flat_map::find` (`0x20afd7c0`) and returns that version's alias sub-map. The alias-name vocabulary across the four sub-maps is `default`, `legacy`, `megacore`, `megachip`, `megachip_tccontrol` (`.rodata` string_views at `0x84f7d8c`/`0x84be65c`/`0x86a44a9`/`0x85c5f87`/`0x861b61d`). The v4 and v5 entries (`TpuVersion` 4 and 5) **share one MapView backing** (both entry-`+0x28` relocate to `0x2200b9b0`), so those two generations resolve aliases through an identical sub-map. The consumer split is therefore fully static and per-`TpuVersion`; the exact key→value pairing inside each type-erased MapView is the only residual. [Confidence: HIGH for the 4-entry version-keyed structure, the variant=`"default"` keys, the alias vocabulary, and the v4/v5 shared sub-map — all byte-read from `.data.rel.ro`/`.rodata` + the `find` call site; MEDIUM for the internal pair direction. See the [TpuVersion ↔ codename matrix](../targets/tpu-version-codename-matrix.md) for the ordinal→codename binding.]

`MaximumNumberOfChunks(VxposeMode, n)` (`0x1d60f200`) is therefore fully numeric and identical across generations (`ElementCount` table `0xb53c830` = `{1,2,4,1,2}`): `B32` → 16, `CompressedB16` → 8, `CompressedB8` → 4, `SegmentedB32` → `n/8`, `SegmentedB16` → `n/16`.

---

## Compute Units — MXU / XLU / IAR

These three counts come from the TensorCore `VectorIsa` sub-message (`TpuSequencerPartsProto` field 5), rendered field-by-field from each blob's wire bytes. Only `mxu_count` and `xlu_count` vary per gen; `iar_count` is 2 everywhere.

| `TpuVer` | Gen | `mxu_count` (f5) | `xlu_count` (f6) | `iar_count` (f7) | MXU systolic dim | FLOPS-derivation MXU | Confidence |
|---|---|---|---|---|---|---|---|
| 0 | Jellyfish v2 | 1 | 1 | 2 | 128×128 | 1 × 128² | CERTAIN |
| 1 | Dragonfish v3 | 2 | 1 | 2 | 128×128 | 2 × 128² | CERTAIN |
| 2 | Pufferfish v4 | 4 | 2 | 2 | 128×128 | 4 × 128² | CERTAIN |
| 3 | Viperfish v5p | 4 | 3 | 2 | 128×128 | 4 × 128² | CERTAIN |
| 4 | Ghostlite v6e | 2 | 2 | 2 | **256×256** | 2 × 256² (override) | HIGH |
| 5 | 6acc60406 v7 | 2 | 2 | 2 | **256×256** | 2 × 256² (override) | HIGH |

The `VectorIsa` wire stream per gen is an 11-byte varint block `10 80 01` (lane=128) · `18 08` (sublane=8) · `28 <m>` (mxu) · `30 <x>` (xlu) · `38 02` (iar); tag `0x20` (`issue_latency_cycle_count`, field 4) is **never** present (see GOTCHA below). All three counts reach the runtime `Target`: `Target::Init` (`0x1d60fc20`) reads `vector_isa()+0xc` as one QWORD packing `{mxu_count, xlu_count}` → `Target+0x4ac`/`+0x4b0`, and `vector_isa()+0x14` = `iar_count` → `Target+0x4a8`. `Target+0x4ac` (`mxu_count`) is consumed by the SDC-checker MXU-sequence injector (`mov eax,[rax+0x4ac]` @ `0x144fca07`); it is not a dead field.

The v6e/v7 drop to `mxu_count` = 2 (from v4/v5's 4) is compensated by the `GhostliteTarget` C++ override of the contracting/non-contracting matrix dimension to **256** (vs the base 128): v6e/v7 use 2 × 256×256 systolic arrays where v4/v5 use 4 × 128×128, yielding comparable peak per-cycle MACs. The 256×256 override is a C++ accessor literal, hence HIGH rather than CERTAIN — the proto `lane_count` stays 128.

> **NOTE —** the FLOPS cross-validation pins both `mxu_count` and the TensorCore clock simultaneously for v2/v3/v4: peak BF16 = `2 × mxu_count × 128² × freq_MHz·1e6` reproduces the C++ `FlopsPerSecond` literal within 1 % (v2 1·700 MHz → 22.9 T vs 22.8 T; v3 2·940 MHz → 61.6 T vs 61.4 T; v4 4·1050 MHz → 137.6 T vs 137 T). The v5p/v6e/v7 peak uses the 256² override plus a per-dtype reduced-precision ladder, so the simple `128²` formula no longer applies.

> **GOTCHA —** `issue_latency_cycle_count` (`VectorIsa` field 4) is **absent in every embedded blob** (proto default 0). A reimplementer must **not** read MXU/VPU issue latency from `chip_parts`; the real per-gen issue latency lives in the cost-model `Performance` grids, queried through `CycleTable::GetCyclesForThroughput` (per-gen vtable slot `+0x10`). The `chip_parts` field exists in the schema but is never populated in this build.

---

## Memory Tiers

Per-die (single-tensornode) capacities. VMEM, SMEM, SFLAG, and HBM are `chip_parts` `Memory`/`SharedMemory` sub-messages; CMEM is a `SharedMemory[CMEM]` present on exactly one generation. `MemBanks` columns are C++ ladder accessors (the bank count per memory space).

| Tier | v2 JF | v3 DF | v4 PF (std / lite) | v5p VF (std / lite) | v6e GL | v7 6acc (die / full) | Confidence |
|---|---|---|---|---|---|---|---|
| **HBM (total)** | 16 GiB | 32 GiB | 32 GiB / 8 GiB | 96 GiB / 16 GiB | 31.5 GiB | 95 GiB / 190 GiB | CERTAIN |
| HBM word | 1024 B | 1024 B | 512 B | 32 B / 512 B | 32 B | 32 B | CERTAIN |
| HBM clock | 1400 MHz | 1800 MHz | 2400 MHz | 3600 / 3200 MHz | 6400 MHz | 7200 MHz | CERTAIN |
| HBM bw / stack | 0.317 TB/s | 0.430 TB/s | 0.982 / 0.492 TB/s | 2.350 / 0.738 TB/s | 1.638 TB/s | 3.686 TB/s | CERTAIN |
| **VMEM / TC** | 16 MiB | 16 MiB | 16 MiB | 64 / 128 MiB | 128 MiB | 64 MiB | CERTAIN |
| VMEM word | 512 B | 512 B | 512 B | 512 B | 512 B | 512 B | CERTAIN |
| **SMEM (TC)** | 16 KiB | 16 KiB | 1 MiB | 1 MiB | 1 MiB | 1 MiB | CERTAIN |
| **SFLAG (TC)** | 1 KiB | 1 KiB | 2 KiB | 2 KiB | 2 KiB | 16 KiB | CERTAIN |
| **CMEM** | — | — | **128 MiB** | — | — | — | CERTAIN |
| IMEM bundle_count | 65536 | 65536 | 65536 | 65536 | 65536 | 65536 | CERTAIN |
| Reg SREG/VREG/PREG/VMREG | 32/32/15/8 | 32/32/15/8 | 32/32/15/8 | 32/64/14/16 | 32/64/14/16 | 32/64/14/16 | CERTAIN |
| DMA granule | 1024 B | 1024 B | 512 B | 32 B / 512 B | 32 B | 32 B | CERTAIN |
| `max_single_host_dma` | 8 MiB | 16 MiB | 2 GiB | 128 GiB | 64 GiB | 32 GiB | CERTAIN |

`MemBanks` ladders (C++ accessors; the bank count per `MemorySpace`, decoded from the FATAL-vs-return structure):

| `MemBanks(space)` | v2/v3 (`JellyfishTarget` @ `0x1d48fc80`) | v4 (`PufferfishTarget` @ `0x1d493900`) | v5p (`ViperfishTarget` @ `0x1d4999c0`) | v6e/v7 (`GhostliteTarget` @ `0x1d4969c0`) | Confidence |
|---|---|---|---|---|---|
| VMEM (space 3) | 8 | 16 | 32 | 32 | CERTAIN |
| CMEM (space 4) | FATAL | 32 | FATAL | FATAL | CERTAIN |
| SMEM (space 5) | 2 | 8 | 8 | 8 | CERTAIN |

> **QUIRK —** CMEM is first-class on **Pufferfish (v4) only**. `PufferfishTarget::MemBanks` is the single ladder that accepts memory space 4 — it computes `idx = space − 3` and indexes a 3-entry table `{16, 32, 8}` (`0xb5305c8`) for spaces 3/4/5, so CMEM (4) returns 32 banks. Every other generation's `MemBanks` either tests `space == 3 || space == 5` and FATALs otherwise (Jellyfish/Dragonfish, `@0x1d48fc80`) or excludes space 4 from its valid range. This is double-encoded: v4 is also the only generation whose `chip_parts` carries a `SharedMemory[CMEM]` (128 MiB, 1050 MHz, 2.151 TB/s) and the only one with a CMEM local-DMA bandwidth row. A reimplementer must FATAL on CMEM for every gen except v4.

> **NOTE —** the register file widens at v5p, not gradually: v2/v3/v4 TensorCores carry SREG 32 / VREG 32 / PREG 15 / VMREG 8; v5p/v6e/v7 carry SREG 32 / VREG **64** / PREG **14** / VMREG **16** (VREG doubled, VMREG doubled, PREG dropped by one). VMEM also does not grow monotonically — the single-TensorCore lite/v6e dies pack *more* VMEM per core (128 MiB on v5e/v6e) than the dual-core v7 (64 MiB), because VMEM is per-TensorCore and the lite dies have fewer cores to share the die area.

---

## Cost-Model Class Trio

Each generation selects a `LatencyTable` subclass (edge/latency model), a `CycleTable` subclass (throughput model), and a `Performance` backend (the numeric grids). The selection is by `TpuVersion` ordinal through two distinct factory mechanisms; 6 versions collapse onto 5 class boundaries.

| `TpuVer` | Gen | `LatencyTable` subclass | LT size | LT vtable | `CycleTable` subclass | `Performance` backend | Confidence |
|---|---|---|---|---|---|---|---|
| 0 | Jellyfish v2 | `LatencyTableJellyfish` | 0x58 | `0x21c202d0` | `JfCycleTable` | `PerformanceJf` (isa) | CERTAIN |
| 1 | Dragonfish v3 | `LatencyTableJellyfish` | 0x58 | `0x21c202d0` | `JfCycleTable` | `PerformanceDf` (isa) | CERTAIN |
| 2 | Pufferfish v4 | `LatencyTablePufferfish` | 0x1e0 | `0x21c20320` | `PfCycleTable` | `PufferfishPerformance` | CERTAIN |
| 3 | Viperfish v5p | `LatencyTableViperfish` | 0x1e0 | `0x21c203f0` | `VfCycleTable` | `ViperfishPerformance` | CERTAIN |
| 4 | Ghostlite v6e | `LatencyTableGhostlite` | 0x1e0 | `0x21c20698` | `GlcCycleTable` | `GhostlitePerformance` | CERTAIN |
| 5 | 6acc60406 v7 | (anon-ns `gf` LatencyTable) | 0x1e0 | `0x21c20920` | `GfcCycleTable` | `GhostlitePerformance` (shared) | HIGH |

The two factories:

- **`LatencyTable::Create(TpuVersion)`** (`0x1c89fba0`) is a direct version-**indexed** `InlinedVector` registry (`registry` @ `0x225799f8`): sign-check `version >= 0`, bounds-check `version < registry->size()`, SOO buffer select, entry stride `0x20`, factory pointer at `[entry+0x18]`, then `call`. The registry is populated by 6 `Register()` calls at static-init from 5 TUs (`latency_table_{jf,pf,vf,gl,gf}.cc`) — `jf.cc` registers both v0 and v1 onto `LatencyTableJellyfish`.
- **`CycleTable::Create(Target const&)`** (`0x1c89cc00`) is the parallel-but-distinct mechanism: a `FunctionRegistry` (Mutex-guarded `FlatHashMap`, `0x225799e8`) keyed on `Target[+0x398]` = `TpuVersion`. Six registrations, `JfCycleTable` serving both v2 and v3.

> **QUIRK —** the v2-vs-v3 split is **not** in the `LatencyTable` or `CycleTable` class — both versions instantiate `LatencyTableJellyfish` (slots 0 and 1) and `JfCycleTable`. The actual JF→DF cost delta comes from `Performance::CreateTensorCore` (`0x1d4927e0`), which compares the `DeviceIdentifiers` against `kJellyfishIdentifiers` (`0xbdf3c0c`) → `PerformanceJf`, else `kDragonfishIdentifiers` (`0xbdf3c18`) → `PerformanceDf`. Likewise v6e and v7 share `GhostlitePerformance` behind distinct `LatencyTable`/`CycleTable` wrapper classes — which is exactly why their `VectorIsa` is byte-identical (`mxu`=2/`xlu`=2/`iar`=2) and their trig estimates match. A reimplementer modeling per-gen cost must split on the silicon-architecture seams, not on the 6-way version enum.

> **NOTE —** the v7 (`Gfc`) `LatencyTable` is an anonymous-namespace type in `latency_table_gf.cc` — no named typeinfo symbol, so its precise C++ class name is unrecoverable (hence HIGH, not CERTAIN). Its vtable (`0x21c20920`), ctor (`0x1c8b9520`), `VectorRawHazardCycles` = 7 (`0x1c8b9c80`), and `GhostlitePerformance` delegation are all confirmed; only the source class name is unknown.

---

## Interconnect — ICI / PCIe / Local DMA Bandwidth

ICI and PCIe bandwidths are C++ `Target` accessor literals (each returns an immediate IEEE-754 double). The local on-chip DMA bandwidth matrix is per-codename `LocalDmaBandwidth*` overrides; the base `Target` accessors return 0 (use the default cost model).

| Metric | v2 JF | v3 DF | v4 PF | v5p VF | v6e GL | Confidence |
|---|---|---|---|---|---|---|
| `IciGigabytesPerSecond` | 123.8 | 164.0 | 89.6 | 186.7 | 186.7 | CERTAIN |
| `PcieUnidirectionalBytesPerSecond` | 16 GB/s | 16 GB/s | 16 GB/s | 16 GB/s | 32 GB/s | CERTAIN |
| FLOPS BF16 (per chip) | 22.8 T | 61.4 T | 137 T | 197 T | 918 T | CERTAIN |
| Transcendentals/s (per TC) | 717 G | 963 G | 537.75 G | 1.536 T | 1.792 T | HIGH |

Local DMA bandwidth (GB/s, C++ literals; selected rows — the full matrix lives on the deep page). The base `Target::LocalDmaBandwidth*` returns 0; each codename subclass overrides the live cells with immediate doubles.

| src → dst | v3 DF | v4 PF | v5p VF (std) | v5e VF-lite | v6e GL | Confidence |
|---|---|---|---|---|---|---|
| HBM → VMEM | 423 | 481 | 1198 | 822 | 1285 | CERTAIN |
| VMEM → HBM | 423 | 1111 | 1224 | 828 | 1432 | CERTAIN |
| HBM → SMEM | — | 34 | 55 | 56 | 55 | HIGH |
| VMEM → CMEM | — | 1121 | — | — | — | CERTAIN |
| CMEM → VMEM | — | 2339 | — | — | — | CERTAIN |
| SPMEM → HBM | — | — | 587.4 | (587.4) | 588 | HIGH |

> **GOTCHA —** Viperfish has a **runtime** std/lite branch inside the bandwidth accessors. `ViperfishTarget::LocalDmaBandwidthHbmToVmem` (`0x1d49a380`) loads the variant byte, checks `variant != 4` → returns the std value `0x4092B80000000000` (1198.0), then compares the variant string dword against `0x65746C6C` (`"ltil"` little-endian = the tail of `"lite"`) and returns `0x4089B00000000000` (822.0) for the lite die. So one C++ method serves both v5p (std, returns 1198) and v5e (`viperfish_lite`, returns 822); a reimplementer cannot treat v5p and v5e as separate `Target` subclasses — they are one class with a string-compare fork. Jellyfish (v2) overrides none of these, so all its `LocalDmaBandwidth*` return base 0.

---

## Accelerator Cores — BarnaCore ↔ SparseCore Pivot

The embedding/dedup accelerator changes type at v5p. v2/v3/v4 carry `BARNA_CORE` cores (the pre-SparseCore engine); v5p/v6e/v7 carry `SPARSE_CORE` cores. The lite dies (`pufferfish_lite`, `viperfish_lite`) ship neither — they are TensorCore-only single-core dies.

| `TpuVer` | Gen | Accel core type | Count / chip (std) | Sequencers | TEC `VectorIsa` lane×sub | Confidence |
|---|---|---|---|---|---|---|
| 0 | Jellyfish v2 | `BARNA_CORE` | 2 | BC_SEQ + 16× BC_ADDR | 1×8 (BC_ADDR) | CERTAIN |
| 1 | Dragonfish v3 | `BARNA_CORE` | 2 | BC_SEQ + 16× BC_ADDR | 1×8 (BC_ADDR) | CERTAIN |
| 2 | Pufferfish v4 | `BARNA_CORE` | 4 | BC_SEQ + 16× BC_ADDR | 1×8 (BC_ADDR) | CERTAIN |
| 3 | Viperfish v5p | `SPARSE_CORE` | 4 | SC_SEQ + 16× SC_TAC + 16× SC_TEC | 8×1 (SC_TEC) | CERTAIN |
| 4 | Ghostlite v6e | `SPARSE_CORE` | 2 | SC_SEQ + 16× SC_TAC + 16× SC_TEC | 8×1 (SC_TEC) | CERTAIN |
| 5 | 6acc60406 v7 | `SPARSE_CORE` | 2 (die) / 4 (full) | SC_SEQ + 16× SC_TEC (**no TAC**) | 16×1 (SC_TEC) | CERTAIN |

The **runtime** `tpu::TpuSequencerType` enum is `SC_SEQ` = 4, SC_TILE_ACCESS_CORE (TAC) = 5, SC_TILE_EXECUTE_CORE (TEC) = 6 (and on the BarnaCore side, BC_SEQ = 2, BC_ADDR = 3) — the `TpuSequencerTypeToString`-table numbering used to size per-engine resource arrays. The **codec template-parameter** numbering used by the codec/ISA pages is off by one (the codec form omits the `INVALID` slot): `{SCS=3, TAC=4, TEC=5}` (and `BARNA=1`/`BARNA_ADDR=2`). Cross the two numberings only with the `+1`; see [getSequencerType](../sparsecore/getsequencertype.md#the-off-by-one-runtime-enum-vs-codec-template-enum). The non-TensorCore sequencers carry no matrix unit, so their `VectorIsa` is lane/sublane only — the SC_TEC vector width is the transposed analogue of the BC_ADDR address-walker geometry.

> **QUIRK —** Trillium (v6e / `ghostlite`) ships SCS + TAC + TEC, but the newest generation **drops the TAC sequencer**: the v7 (`6acc60406`) `chip_parts` carries SC_SEQ + 16× SC_TEC and **no** SC_TILE_ACCESS_CORE_SEQ. The SC_TEC vector width also doubles from 8 lanes (v5p/v6e) to 16 lanes (v7) — the tile-execute width grew while the tile-access sequencer was folded away. A reimplementer enumerating SparseCore sequencer types must not assume the v5p/v6e {SEQ, TAC, TEC} triple holds for v7.

> **NOTE — (missing-data cells)** the BarnaCore detailed sub-message geometry (`BarnaCoreFsm` freg/smem offsets, the dedup/address-map sizes) is **not** walked here; the table reports the per-BarnaCore sequencer/memory presence and the TEC/BC vector geometry, but the BarnaCore `Core` sub-message internals remain undecoded. Likewise the exact `MatmulDataFormat` enum → dtype-name binding for the v5p/v6e/v7 reduced-precision FLOPS ladder (1×/2×/4×) is inferred from the doubling pattern, not byte-pinned per index. Cells marked "—" are genuinely absent for that generation (e.g. SparseCore rows for v2–v4, CMEM rows for every gen but v4), not unknown.

---

## Version Ordinals — Three Independent Axes

A generation wears three integer ordinals on three axes that do **not** share a numbering. The matrix above is keyed on the internal `tpu::TpuVersion`; this table binds it to the other two so the page never indexes one axis with another's ordinal.

| Generation | `tpu::TpuVersion` (internal) | `TpuVersionProto` (wire) | `xprof::DeviceType` (profiler) | Confidence |
|---|---|---|---|---|
| Jellyfish v2 | 0 | 1 | 3 | CERTAIN |
| Dragonfish v3 | 1 | 2 | 5 | CERTAIN |
| Pufferfish v4 | 2 | 3 | 7 | CERTAIN |
| Viperfish v5p | 3 | 4 | 10 | CERTAIN |
| Ghostlite v6e | 4 | 5 | 13 | CERTAIN |
| 6acc60406 v7 | 5 | 6 | 12 | CERTAIN |

`TpuVersionToString` (`0x20b3a480`) FATALs on `version >= 6` and indexes the 6-pointer table `off_22011BF0`, whose relocations target the codename literals `jellyfish` … `6acc60406`. The proto ordinal is always `internal + 1` (the blob's field-1 version). `DeviceType` is a **sparse** profiler axis (`DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0`) that includes lite variants as their own ordinals (Puffylite = 8, Viperlite = 11) — it is not arithmetically derivable from `TpuVersion`.

> **GOTCHA —** the `DeviceType` ordinals are out of `TpuVersion` order at the top end: Ghostlite (v6e) is `DeviceType` **13** but `6acc60406` (v7) is `DeviceType` **12**. A reimplementer who assumes `DeviceType` increases with generation will swap v6e and v7. Always translate through this table, never by adding a constant to one ordinal.

---

## Cross-References

Each column of the master matrix has a deep page that owns its derivation:

- [Codename Cheat-Sheet](../front/codename-cheatsheet.md) — the full three-axis version map (codec / fish / `TpuVersion` / `DeviceType` / proto / PCI DID), the source of the ordinal table here
- [Chip-Parts `.binarypb`](../targets/chip-parts-binarypb.md) — the embedded blob catalog, the `DefaultsForVersion` `embed://` load path, the `TpuChipPartsProto` schema
- [Per-Codename HW Constants](../targets/per-codename-hw-constants.md) — the full per-gen `chip_parts` decode (HBM/VMEM/SMEM/SFLAG/MXU/register file) the memory columns summarize
- [TPU-Topology Struct](../targets/tpu-topology-struct.md) — the lane/sublane cache and `Target[+0x3b8]` geometry chain
- [TPU-Version / Codename Matrix](../targets/tpu-version-codename-matrix.md) — the `TpuVersionToString` table and ordinal axes
- [Bundle Model Overview](../isa/bundle-model-overview.md) — the per-gen bundle byte sizes ([JF 41 B](../isa/bundle-jf-41b.md), [PF 51 B](../isa/bundle-pf-51b.md), [VF 64 B](../isa/bundle-vf-64b.md), [GL](../isa/bundle-gl.md), [GF](../isa/bundle-gf.md))
- [IARs per TensorCore](../cost/iars-per-tensorcore.md) — the `iar_count` = 2 derivation and the `VectorIsa` → `Target+0x4a8` store
- [CycleTable Family](../cost/cycletable-family.md) and [Performance Overview](../cost/performance-overview.md) — the per-gen `CycleTable` / `Performance` subclass dispatch
- [Local DMA Bandwidth](../cost/local-dma-bandwidth.md) — the full per-codename `LocalDmaBandwidth*` matrix and the Viperfish variant split
- [Matmul-Mode Modifiers](../cost/matmul-mode-modifiers.md) — the `MatmulDataFormat` set the reduced-precision FLOPS ladder prices
- [LLO Opcode Table](llo-opcode-table.md) — the per-gen opcode roster the bundle/sequencer geometry encodes
- [Memory-Space Master Table](memory-space-table.md) — the `MemorySpace` enum the `MemBanks` ladders and per-tier capacities key on
