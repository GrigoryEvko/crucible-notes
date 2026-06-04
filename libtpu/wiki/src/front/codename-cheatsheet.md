# Codename Cheat-Sheet

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary). The binary is **not** stripped — every symbol is a demangled C++ name; `.text`, `.rodata`, and `.lrodata` map VMA == file offset. Other builds will differ.*

## Abstract

A single TPU generation wears at least seven different names inside `libtpu.so`, and they live on **three independent integer axes** that do not share a numbering. A reader chasing one fact — "which chip is `gfc`?", "what does `DeviceType` 12 mean?", "is `TpuVersion` 4 Trillium or v7x?" — keeps hitting a different axis than the one they hold, and the off-by-ones between those axes are exactly where prior analysis went wrong. This page is the one card to come back to: it pins every name of every generation to the binary site that defines it, side by side, so the rest of the wiki can point here instead of re-deriving the mapping.

The three axes are the **internal `tpu::TpuVersion` enum** (a dense `0..5`, the compiler's notion of "which silicon", defined by `TpuVersionToString` @ `0x20b3a480`); the **profiler's `xprof::DeviceType` enum** (a sparse `1..13`, the trace pipeline's notion of "which device", defined by `DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0` and `DeviceTypeString` @ `0xf69c7c0`); and the **protobuf `TpuVersionProto`** (a `1..6` wire enum, `internal = proto − 1`, `TpuVersionFromProto` @ `0x20b3a8c0`). Orthogonal to all three are the codec/ISA codenames (`jxc`, `pxc`/`plc`, `vfc`/`vlc`, `glc`, `gfc`), the fish marketing codenames (Jellyfish … Trillium), the PCI device-ids, and the public Cloud-API strings (`v2`…`tpu7x`). Every binary-anchored cell below carries its evidence address; where a name is *not* in the binary (Trillium, Ironwood, "Ghostfish"), the page says so rather than inventing it.

For navigation, the contract is:

- **The master table** binds every axis for every generation in one row, each cell carrying its own confidence — the canonical lookup the rest of the wiki links to.
- **The two-axes warning** explains *why* `TpuVersion` and `DeviceType` disagree numerically, so a reimplementer never indexes one table with the other's ordinal.
- **The gotchas** collect the traps: the two off-by-one SparseCore sequencer enums, the nested codec namespaces (`pxc::plc`, `vxc::vlc`, `gxc::gfc`), and the v7x `6acc60406`/`gfc` shipping SparseCore SCS+TEC but **not** TAC.

| | |
|---|---|
| **Internal enum** | `tpu::TpuVersion` `0..5` — `TpuVersionToString` @ `0x20b3a480`, table `off_22011BF0` (6 ptrs) |
| **Profiler enum** | `xprof::DeviceType` `1..13` (sparse) — `DeviceTypeFromDeviceIdentifiers` @ `0xf6993a0`, names `off_21772F00` |
| **Proto enum** | `TpuVersionProto` `1..6` — `TpuVersionFromProto` @ `0x20b3a8c0` (`internal = proto − 1`) |
| **Codec factory** | `tpu::TpuCodec::Create(TpuVersion)` @ `0x1e835fa0` (6-case switch, order = `TpuVersion`) |
| **Trace codec** | `xprof::tpu::GetTraceCodec` @ `0xf5a2900` (keyed on PCI identity, not on either ordinal) |
| **PCI vendor** | `0x1ae0` (Google) for every TPU device |

---

## The Master Cheat-Sheet

One row per generation, in `TpuVersion` order. Read left-to-right to translate any one name into all the others. The **Confidence** column applies to the whole row's binary-anchored cells; the marketing column is called out separately because it is the one column not sourced from the binary.

| Codec codename | Fish codename | `TpuVersion` (internal) | `DeviceType` (profiler) | Marketing display | PCI chip DID | HAL family | Confidence |
|---|---|---|---|---|---|---|---|
| `jxc` (jellyfish) | Jellyfish | **0** | **3** | `TPU v2` | `0x004e` | `TpuHalJxc` | CERTAIN |
| `jxc` (dragonfish) | Dragonfish | **1** | **5** | `TPU v3` | `0x004f` | `TpuHalJxc` | CERTAIN |
| `pxc` / `pfc` | Pufferfish | **2** | **7** | `TPU v4` (`v4 lite`) | `0x0050`/`51`/`52` | `TpuHalPxc` | CERTAIN |
| `pxc` / `plc` | Puffylite | — *(no own `TpuVersion`)* | **8** | *(v4-class lite)* | *(chip-parts variant)* | `TpuHalPxc` | HIGH |
| `vxc` / `vfc` | Viperfish | **3** | **10** | `TPU v5` (`v5 lite`) | `0x00ac`/`0x00ad` | `TpuHalVxc` | CERTAIN |
| `vxc` / `vlc` | Viperlite | — *(folds into v3)* | **11** | *(v5-class lite)* | `0x00ae`/`0x00af` | `TpuHalVxc` | HIGH |
| `gxc` / `glc` | Ghostlite | **4** | **13** | `TPU v6 lite` | `0x00d1` | `TpuHalVxc` | CERTAIN |
| `gxc` / `gfc` | *(none — `6acc60406`)* | **5** | **12** | `TPU7x` | `0x00f2` | `TpuHalVxc` | CERTAIN |

> **NOTE —** the `TpuVersion`→codename binding is the single most-anchored fact in the binary. `TpuVersionToString` (`0x20b3a480`) indexes the 6-pointer `.data.rel.ro` table at `off_22011BF0`, whose `R_X86_64_RELATIVE` relocations target the literals `jellyfish` (`0x863f064`), `dragonfish` (`0x863f392`), `pufferfish` (`0x863f1c4`), `viperfish` (`0x863f172`), `ghostlite` (`0x86864e0`), `6acc60406` (`0x863f0cf`). This compiled array is the root every other axis hangs off.

### The marketing / Cloud-API column (separate confidence)

The fish and codec codenames are *internal* names baked into symbols and `.rodata`. The customer-facing Cloud-TPU names are a parallel string set reached through `TpuVersionToExternalName` (`0x20b3a500`) and the `AcceleratorType…` parser (`0x204cf620` / `0x20b3a740`). The public **product** codenames are layered on externally and are **not all in the binary**:

| `TpuVersion` | Fish | `TpuVersionToExternalName` string | Cloud-API string(s) | Public marketing | Marketing confidence |
|---|---|---|---|---|---|
| 0 | Jellyfish | `TPU v2` | `v2` | TPU v2 | HIGH (string in binary) |
| 1 | Dragonfish | `TPU v3` | `v3` | TPU v3 | HIGH (string in binary) |
| 2 | Pufferfish | `TPU v4` / `TPU v4 lite` | `v4`, `v4lite` | TPU v4 | HIGH (string in binary) |
| 3 | Viperfish | `TPU v5` / `TPU v5 lite` | `v5`, `v5e`, `v5p` | TPU v5p / v5e | HIGH (string in binary) |
| 4 | Ghostlite | `TPU v6 lite` | `v6e` | **Trillium** | LOW — *"Trillium" is NOT in the binary* |
| 5 | `6acc60406` | `TPU7x` | `tpu7x`, `tpu7` | **Ironwood** | LOW — *"Ironwood" is NOT in the binary (external name)* |

> **GOTCHA —** the string `Trillium` has **zero** occurrences in `libtpu.so`; so does `Ironwood`; so does `Ghostfish`. Trillium = Cloud `v6e` = `Ghostlite`/`glc`/`TpuVersion` 4 and Ironwood = Cloud `tpu7x` = `6acc60406`/`gfc`/`TpuVersion` 5 are both *external* facts (Cloud-TPU documentation), correct but un-sourceable from the binary. The newest generation's only internal name is the obfuscated tag `6acc60406`; its `gxc::gfc` directory abbreviation plausibly stands for a "Ghostfish"-style fish name, but that name is **not present** — do not assert it. Cite `6acc60406` (or `TPU7x` for the display string) as the canonical internal name for `TpuVersion` 5, and treat **Ironwood** as the external-only marketing label.

---

## Two Axes, Two Numberings — Why `TpuVersion` ≠ `DeviceType`

The most common error is to index one table with the other's ordinal. `TpuVersion` 4 is **Ghostlite**; `DeviceType` 4 is **not** any generation in this table at all. They are different enums maintained by different subsystems, and they were never meant to align.

### `TpuVersion` — the dense compiler axis (`0..5`)

`tpu::TpuVersion` is the *compiler / codec / HAL* axis. It is a contiguous `0..5`, one value per silicon family that the compiler emits code for. `TpuVersionToString` bounds it at `< 6`; `TpuCodec::Create` (`0x1e835fa0`) is a clean 6-case switch (`case 0→`Jellyfish … `case 5→` the anonymous `gfc` codec via `sub_1E838380`); `TpuVersionFromProto` maps proto `1..6` onto it as `internal = proto − 1`. Lite variants (Puffylite, Viperlite) **do not get their own `TpuVersion`** — they multiplex inside the parent family's HAL through the embedded `TpuChipParts` proto, so this axis has exactly six values.

### `DeviceType` — the sparse profiler axis (`1..13`)

`xprof::DeviceType` is the *profiler / trace* axis. `DeviceTypeFromDeviceIdentifiers` (`0xf6993a0`) reads a captured device's 12-byte PCI `DeviceIdentifiers` tuple and assigns a `DeviceType` ordinal directly — and that ordinal set is **sparse**: the eight real TPU silicon generations land on ordinals `{3, 5, 7, 8, 10, 11, 12, 13}`, skipping `1, 2, 4, 6, 9`. The skipped values are not gaps: in the 13-entry name table `off_21772F00`, ordinal `1` is `"GPU"` and ordinals `2, 4, 6, 9` all read the generic `"Cloud TPU"` placeholder string, so the TPU silicon slots are interleaved with a GPU slot and several unassigned/fallback slots. `DeviceTypeString` (`0xf69c7c0`) computes `index = ordinal − 1` and indexes that table, bounding at `ordinal − 1 > 0xC` (i.e. ordinal `1..13`); out-of-range returns `"Cloud TPU"`. The ordinal store is a literal `mov` in each branch of `DeviceTypeFromDeviceIdentifiers`:

```c
// xprof::tpu::DeviceTypeFromDeviceIdentifiers(DeviceIdentifiers)  // 0xf6993a0
// each branch matches the PCI tuple against a kXxxChipIdentifiers constant,
// then stores the DeviceType ordinal at result+8 (decompiler: result[2]):
if matches kJellyfishIdentifiers:            DeviceType = 3
else if matches kDragonfishIdentifiers:      DeviceType = 5
else if matches kPuffyliteChipIdentifiers:   DeviceType = 8     // pxc::plc — its own ordinal
else if matches kPufferfishChipB0{Mfg,Water,Air}Identifiers:
                                             DeviceType = 7     // pxc::pfc
else if matches kViperliteChip{A0,A1}{PF,VF}Identifiers:
                                             DeviceType = 11    // vxc::vlc
else if matches kViperfishChip{PF,VF}Identifiers:
                                             DeviceType = 10    // vxc::vfc
else if IsGlc(ids):                          DeviceType = 13    // gxc::glc, Ghostlite / v6e
else if IsGfc(ids):                          DeviceType = 12    // gxc::gfc, 6acc60406 / v7x
else: error("Unsupported device identifiers")                  // device_identifiers_utils.cc:152
```

> **QUIRK —** on the `DeviceType` axis the two newest generations are **numerically inverted** relative to chronology: Ghostlite (older, v6e) is `13`, while `6acc60406` (newer, v7x) is `12`. The name table itself confirms it directly — `off_21772F00` slot 11 (ordinal 12) is `"TPU v7x"` and slot 12 (ordinal 13) is `"TPU v6 Lite"`. This is the single fact that trips every analyst — `DeviceType` 12 < 13 does **not** imply v7x is older than v6e. The profiler's perf-counter layer gates on `DeviceType == 12` precisely because v7x is the only generation in this build that exposes named on-device counters (see [v7x Perf-Counters](../profiling/v7x-perf-counters.md)); a reimplementer who gates on `== 13` expecting "the latest chip" silences the entire v7x counter pipeline.

> **GOTCHA —** `Puffylite` (`pxc::plc`) and `Viperlite` (`vxc::vlc`) exist as **first-class `DeviceType` ordinals** (8 and 11) but have **no `TpuVersion`** of their own — they fold into Pufferfish (`TpuVersion` 2) and Viperfish (`TpuVersion` 3) respectively. So `DeviceType → TpuVersion` is many-to-one. Translating a captured `DeviceType` to a compiler `TpuVersion` must collapse `8→2` and `11→3`; the reverse direction loses the lite/non-lite distinction (which is recovered from the `TpuChipParts` variant, not from `TpuVersion`).

### The proto axis (`1..6`) and the public `TpuType`

A third enum, `TpuVersionProto`, is the protobuf wire form: `TPU_V2=1` … `TPU_V6_LITE=5`, `TPU_V7X=6`. `TpuVersionFromProto` (`0x20b3a8c0`) is the literal `internal = proto − 1` translation; this is why the embedded `6acc60406_chip_parts.binarypb` blob carries `version = 6` (proto) for the chip whose internal `TpuVersion` is `5`. A fourth, coarser public enum — `superpod::routing::TpuType` (`GetTpuType` @ `0x1ff94340`) — spreads the lite/standard split back out (`type=2`…`type=10`). Four enums, fully reconcilable through the master table above; see [Dual-Enum: Proto vs Internal](../targets/dual-enum-proto-vs-internal.md).

---

## Gotchas and Namespace Nesting

### Codec namespace nesting — family vs sub-core

The codec/ISA namespaces are **two levels deep**: a family tag, then a sub-core — for the split families (`pxc`, `vxc`, `gxc`) that sub-core is a fetch/load pair; `jxc` is fused and instead nests engine blocks. A symbol search for the family tag alone (`pxc`, `vxc`, `gxc`) lands in the wrong sub-namespace half the time. The nesting, under `asic_sw::driver::deepsea::`:

| Family | Sub-cores (nested namespaces) | Serves | Confidence |
|---|---|---|---|
| `jxc` | `jxc::jfc` (Jellyfish core), `jxc::dfc` (dataflow), `jxc::registers`, `jxc::snap` | Jellyfish, Dragonfish (fused, no fetch/load split) | HIGH |
| `pxc` | `pxc::pfc` (fetch), `pxc::plc` (load) | Pufferfish, Puffylite | HIGH |
| `vxc` | `vxc::vfc` (fetch), `vxc::vlc` (load) | Viperfish, Viperlite | HIGH |
| `gxc` | `gxc::glc` (load), `gxc::gfc` (fetch) | Ghostlite (`glc`), `6acc60406` (`gfc`) | HIGH |

> **GOTCHA —** `jxc::jellyfish`, `jxc::dragonfish`, `jxc::bcs`, and `jxc::brn` are **not** namespaces. The only real nested namespaces under `jxc` are the engine blocks (`jfc`, `dfc`, `registers`, `snap`); the `bcs`/`brn` tokens are prefixes inside `*_trace_entry` type names (`bcs_internal_trace_entry`, `brn_perf1_trace_entry`), and `jellyfish`/`dragonfish` appear only as `*_performance_counters` identifiers. JXC's compiler-side ISA lives in `platforms_deepsea::jellyfish::isa`, not in any `jxc::isa`. See [Sub-Core Taxonomy](../targets/sub-core-taxonomy.md).

> **QUIRK —** the `gxc` family registers its HAL into the **shared `TpuHalVxcHardwareFactory`** (vtable `0x21cabf70`), the same factory class Viperfish uses — there is no `TpuHalGxc` factory. Three internal codenames (Viperfish, Ghostlite, `6acc60406`) share one factory class and one vtable, differing only by the stored `TpuVersion` at `+8`. The per-generation HAL registration is what `google_init_module_tpu_hal_{vxc,glc,gfc}_hardware_impl` injects; the family tag in the *codec* namespace (`gxc`) and the HAL *factory* class (`Vxc`) are deliberately different — don't expect them to match.

> **NOTE —** only `TpuVersion` 4 (`glc`) ships a **named** codec class, `tpu::TpuCodecGhostlite` (`_ZTV` @ `0x21d35c00`). `TpuVersion` 5 (`gfc`) ships an **anonymous** codec (vtable `0x21d35898`, built by `sub_1E838380`, reached as `case 5` of `TpuCodec::Create`); there is no `TpuCodec6acc60406` symbol. The codec's `Encode` guard re-proves the proto axis independently: the `glc` codec checks `proto_tag == 5` at `+0x58`, the `gfc` codec checks `proto_tag == 6`.

### The two off-by-one SparseCore sequencer enums

`TpuSequencerType` (the sub-core a bundle targets) has **two numberings one apart**, and mixing them silently encodes for the wrong engine. The codec template instantiates SCS/TAC/TEC at internal values `{3, 4, 5}`; the proto/runtime form is one higher, `{4, 5, 6}`:

| Sequencer | Codec-template (internal) | Proto / runtime | Confidence |
|---|---|---|---|
| TensorCore (TC) | 0 | 1 | CONFIRMED |
| BarnaCore (BCS) | 1 | 2 | CONFIRMED |
| *(reserved)* | 2 | 3 | CONFIRMED |
| SparseCore Scalar (SCS) | **3** | **4** | CONFIRMED |
| SparseCore Tile-Access (TAC) | **4** | **5** | CONFIRMED |
| SparseCore Tile-Execute (TEC) | **5** | **6** | CONFIRMED |

`TpuSequencerTypeFromProto` (`0x20b36300`) is the literal `internal = proto − 1` switch that joins them; the SCS codec is instantiated at `(TpuSequencerType)3`, the TAC codec at `(TpuSequencerType)4`. Full op rosters per generation are on [Sequencer Ops Per Gen](../isa/sequencer-ops-per-gen.md).

> **GOTCHA — 6acc60406 (v7x) ships SCS + TEC only, no TAC.** `gxc::gfc::isa::SparseCoreScs{Bundle,CodecBase,Program}` and `gfc::isa::SparseCoreTec{Bundle,Program}` are present in the symbol table; `gfc::isa::SparseCoreTac{Bundle,CodecBase,Program}` is **absent**. Viperfish (`vfc`) and Ghostlite (`glc`) carry all three SparseCore sequencers; `6acc60406`/`gfc` (v7x) drops the tile-access engine. A reimplementation that assumes the SparseCore triad is uniform across the SparseCore-bearing generations (Viperfish onward) will emit a TAC codec for v7x that the hardware has no sequencer for.

### Trace-codec selection is keyed on PCI identity, not on either ordinal

`GetTraceCodec` (`0xf5a2900`) does **not** take a `TpuVersion` or a `DeviceType`. It re-classifies the raw `DeviceIdentifiers` tuple with the `Is{Jfc,Dfc,Pfc,Plc,Vfc,Vlc,Glc,Gfc}` predicates and selects one of six `std::variant` codec alternatives. The variant index is a *fourth, independent* small enum (jxc=6, glc=1, gfc=2, vlc=3, vfc=4, pxc=5 in the decompiled `__assign<N>` calls) that matches none of the other three axes. When wiring the profiler, drive codec selection from the PCI tuple through these predicates — not from a `DeviceType` or `TpuVersion` you computed elsewhere. See [Riegeli Trace Container](../profiling/riegeli-trace-container.md) for how the selected codec is then framed.

---

## Cross-References

- [TpuVersion Codename Matrix](../targets/tpu-version-codename-matrix.md) — the deep page that owns the `TpuVersion`→codename derivation and its eighteen cross-validation sites
- [PCI Device IDs](../targets/pci-device-ids.md) — the full `DeviceIdentifiers` table, header DIDs, rev-masks, and the `IsGlc`/`IsGfc` chip-DID compares
- [Dual-Enum: Proto vs Internal](../targets/dual-enum-proto-vs-internal.md) — `TpuVersionProto` `1..6` vs internal `TpuVersion` `0..5`, the `proto − 1` reconciliation
- [Marketing / Cloud Naming](../targets/marketing-cloud-naming.md) — external `TPU vN`, Cloud-API strings, and the Trillium/Ironwood "not in binary" note
- [Codename Superseded Labels](../targets/codename-superseded-labels.md) — the `Ghostlite=v5p` / `Trillium=6acc60406` / "Ghostfish" mislabels to avoid, with the binary site that disproves each
- [GXC Family](../targets/gxc-family.md) · [VXC Family](../targets/vxc-family.md) · [PXC Family](../targets/pxc-family.md) · [JXC Family](../targets/jxc-family.md) — per-family codec, ISA, and HAL detail
- [Sub-Core Taxonomy](../targets/sub-core-taxonomy.md) — the `fetch`/`load` sub-core split and the `xc`/`fc`/`lc` naming pattern
- [HAL Families](../targets/hal-families.md) — the three `TpuHal{Jxc,Pxc,Vxc}HardwareFactory` classes and which `TpuVersion` each registers
- [Per-DeviceType Profiler Struct](../profiling/per-devicetype-struct.md) — the `kDeviceTypeInfo` array indexed by the sparse `DeviceType` ordinal
- [v7x Perf-Counters](../profiling/v7x-perf-counters.md) — the `DeviceType == 12` gate and why v7x is the only counter-naming generation
- [Sequencer Ops Per Gen](../isa/sequencer-ops-per-gen.md) — the `(TpuVersion, TpuSequencerType)` op rosters and the v7x `6acc60406` TAC drop
- [Riegeli Trace Container](../profiling/riegeli-trace-container.md) — how the PCI-identity-selected trace codec frames its records
- [Per-Gen Function Dispatcher](../forensics/per-gen-function-dispatcher.md) — the binary's per-`TpuVersion` dispatch pattern across the codebase
