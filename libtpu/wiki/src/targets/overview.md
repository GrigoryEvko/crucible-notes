# Part IV Overview — Silicon and Hardware Codename Model

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`libtpu.so` is the PJRT plugin XLA loads to target Google TPU hardware, and it must service six distinct silicon generations from one binary. Part IV documents how the library represents that fact: a single 6-value enum (`tpu::TpuVersion`) that names the generation, three HAL factory families that drive it, and the per-generation codec, bundle encoder, and chip-constant tables that hang off the enum integer. This page is the map of that subsystem — what the six generations are, how they group, and how the version integer threads through HAL routing, ISA selection, and hardware-constant lookup.

The model is best understood by analogy to an LLVM backend serving multiple sub-targets from one `TargetMachine`. The `TpuVersion` enum plays the role of `Triple::ArchType` — a single integer that gates every target-specific decision — and the HAL factory families play the role of `Subtarget` selection. The wrinkle unique to TPU is the dual-enum trap: the integer the runtime dispatches on (`TpuVersion`, 0-based) differs by one from the integer that travels in serialized protos (`TpuVersionProto`, 1-based). Half the pages in this part exist to keep those two numberings straight.

The part is organized in three layers. The **identity** layer (this overview, the [codename matrix](tpu-version-codename-matrix.md), the [dual-enum](dual-enum-proto-vs-internal.md) page) defines the enum and its cross-walk to codenames, wire values, and external names. The **routing** layer ([HAL families](hal-families.md), [sub-core taxonomy](sub-core-taxonomy.md), the per-family pages) defines how a version selects a factory and a fetch/load core split. The **constants** layer ([per-codename HW constants](per-codename-hw-constants.md), [PCI device IDs](pci-device-ids.md), [chip parts](chip-parts-binarypb.md)) defines the hardware parameters each generation carries.

| | |
|---|---|
| **Generations** | 6 — `kJellyfish=0` … `k6acc60406=5` |
| **Identity enum** | `tpu::TpuVersion` (internal, 0-based) ↔ `TpuVersionProto` (wire, 1-based) |
| **Canonical map** | `tpu::TpuVersionToString` @ `0x20b3a480` → `off_22011BF0` rel.ro pointer table |
| **HAL families** | 3 factory classes — JXC, PXC, VXC |
| **Sub-core model** | fetch/load core split from Pufferfish onward (`pfc`/`plc`, `vfc`/`vlc`) |
| **ISA sub-families** | `gxc::gfc` (v5, fetch), `gxc::glc` (v4, load) under the shared VXC family |

---

## The Six Generations

The six `TpuVersion` values, in enum order, with the one-line identity that the [codename matrix](tpu-version-codename-matrix.md) establishes in full. The codename is the `TpuVersionToString` output; the external name is the `TpuVersionToExternalName` output.

| Int | Codename | External name | HAL family | Defining trait |
|---:|---|---|---|---|
| 0 | `jellyfish` | `TPU v2` | JXC | first gen; BarnaCore embedding engine, no SparseCore |
| 1 | `dragonfish` | `TPU v3` | JXC | Jellyfish derivative — shares encoder, flags, bundle restrictions |
| 2 | `pufferfish` | `TPU v4` | PXC | introduces the fetch/load core split (`pfc`/`plc`) |
| 3 | `viperfish` | `TPU v5` | VXC | first SparseCore; BarnaCore retired |
| 4 | `ghostlite` | `TPU v6 lite` | VXC | `gxc::glc` ISA sub-family; named codec |
| 5 | `6acc60406` | `TPU7x` | VXC | obfuscated codename; anonymous codec; `gxc::gfc` ISA sub-family |

Two structural facts shape everything downstream. First, the generations are **not independent** — Dragonfish reuses Jellyfish's encoder (the shared `CreateEncoderJfDf` path), and 6acc60406 reuses Ghostlite's (`CreateEncoderGlGf`), so the six generations resolve to four encoder families. Second, the newest generation (`6acc60406`) is the least exposed: it alone has an obfuscated, non-mnemonic codename, no named `TpuCodec` C++ class, and a bundle-restrictions registration that exists only as a string. That asymmetry is the binary's own marker of which silicon is freshest in this build.

---

## Three HAL Families

The six codenames are serviced by exactly three HAL factory classes, confirmed in the symbol table: `TpuHalJxcHardwareFactory`, `TpuHalPxcHardwareFactory`, and `TpuHalVxcHardwareFactory`. The mapping is many-to-one:

```text
JXC  TpuHalJxcHardwareFactory   <- kJellyfish (0), kDragonfish (1)
PXC  TpuHalPxcHardwareFactory   <- kPufferfish (2)              (constructed with no version arg)
VXC  TpuHalVxcHardwareFactory   <- kViperfish (3), kGhostlite (4), k6acc60406 (5)
```

JXC carries the two oldest generations because Dragonfish is a Jellyfish refresh sharing its dataflow. PXC is dedicated to Pufferfish and is constructed with no version argument, since it services exactly one generation. VXC is the modern family: it handles Viperfish, Ghostlite, and 6acc60406, differentiated only by the `TpuVersion` integer the factory is constructed with — one factory class parameterized three ways. The init-module-to-factory wiring (the `google_init_module_tpu_hal_*` translation units, including the separately-named `glc` and `gfc` init modules that both register the VXC factory) is detailed in [HAL Families](hal-families.md).

> **GOTCHA —** the `glc` and `gfc` init-module names do not imply `Glc`/`Gfc` factory classes. Generations 4 and 5 register through init modules named for their ISA sub-family (`glc`, `gfc`), but both construct the shared `TpuHalVxcHardwareFactory`. There is no `TpuHalGxcHardwareFactory`. The `glc`/`gfc` tokens name the *ISA sub-core namespace*, not a fourth HAL family.

---

## The Fetch/Load Sub-Core Split

Starting with Pufferfish, the per-generation ISA namespace splits into a **fetch core** and a **load core** — a decoupled-access/execute organization where one core streams operands and the other executes. The split shows up directly in the `asic_sw::driver::deepsea` namespace populations:

```text
deepsea (driver umbrella)
  jxc   jellyfish/dragonfish   -- no fetch/load split (fused dataflow)
  pxc   pfc (fetch) / plc (load)      -- Pufferfish; split introduced here
  vxc   vfc (fetch) / vlc (load)      -- Viperfish family
  gxc   gfc (fetch) / glc (load)      -- gfc -> 6acc60406 (v5), glc -> Ghostlite (v4)
```

The pattern is clear for `pxc` and `vxc`: the `f`/`l` letter marks fetch versus load, and the symbol counts are asymmetric (the fetch core is far larger — `pfc` ~8.0K vs `plc` ~0.9K, `vfc` ~17.4K vs `vlc` ~1.8K mangled-token occurrences), consistent with a decoupled fetch/execute pair within one chip. JXC has no such split: `jxc::jellyfish` is a thin namespace, matching a first-generation fused dataflow design with no separate fetch core.

> **GOTCHA — under `gxc` the `f`/`l` letters mark fetch/load *and* each sub-core is a different generation.** `gfc` is the general fetch-core, `glc` the general load-core — the same f/l convention as `pxc`/`vxc`. The twist is that the two `gxc` sub-cores do not split one chip: `glc` carries Ghostlite (v4) and `gfc` carries 6acc60406 (v5), each a full, near-equal-sized ISA namespace (`gfc` ~63.8K, `glc` ~62.9K token occurrences — not the lopsided fetch/load ratio of `pxc`/`vxc`). The codec walk pins the pairing — `TpuCodecGhostlite` binds `gxc::glc::isa`, the anonymous v5 codec binds `gxc::gfc::isa`. Reading `gfc` as "Ghostlite fetch-core" inverts both facts; the [sub-core taxonomy](sub-core-taxonomy.md) page works the pairing out in detail.

---

## How `TpuVersion` Threads Through the Pipeline

The version integer is read at three decision points, in this order, as a program moves from target identification to code emission:

```text
accelerator_type string ("v5p", "v6e", "tpu7x", ...)
        |  AcceleratorTypeToTpuVersionEnum (parser)         @ 0x204cf620
        v
   TpuVersion  (internal enum, 0..5)
        |
        +--> HAL routing:    TpuHalFactory::Register / Create  -> JXC | PXC | VXC factory
        |
        +--> chip constants: per-version constant tables, chip_parts.binarypb (proto version = internal+1)
        |
        +--> ISA selection:  TpuCodec::Create  @ 0x1e835fa0     -> per-codename codec
                             ProgramProtoUtil::BundleCount @ 0x1e830e80 -> encoder family (JfDf | Pf | Vf | GlGf)
```

1. **Identification.** A user-supplied `accelerator_type` string is parsed to a `TpuVersion` by `AcceleratorTypeToTpuVersionEnum` (`0x204cf620`). From this point the string is gone and everything is integer dispatch. The reverse direction — enum back to display string — is `TpuVersionToExternalName` (`0x20b3a500`).

2. **HAL routing.** The version selects the HAL factory family. `TpuHalFactory::Register` records `(PlatformType, TpuVersion) → factory`, and lookup at runtime constructs the JXC, PXC, or VXC factory parameterized by the version.

3. **Chip constants.** Per-version hardware parameters (core counts, MXU shape, memory hierarchy) are read from per-version constant tables and from the embedded `chip_parts.binarypb` resources. This is the one stage where the dual-enum trap bites: the chip-parts `version` field is a *proto* value, so the newest generation's blob reads `6` for the silicon the runtime dispatches as `5`. See [Dual Enum](dual-enum-proto-vs-internal.md).

4. **ISA selection.** The version picks the codec (`TpuCodec::Create`, `0x1e835fa0` — one `CreateTpuCodec<Codename>` per generation, with the v5 codec anonymous) and the bundle encoder family (`ProgramProtoUtil::BundleCount`, `0x1e830e80` — which collapses the six generations to four encoder families: `JfDf`, `Pf`, `Vf`, `GlGf`).

A reimplementation must perform these reads in the same order and on the same integer. The most common failure mode is mixing the two enum spaces between stages — parsing a string to an internal enum, serializing it as a proto without the `+1`, then deserializing it back without the `−1`, ending up one generation off.

---

## Cross-References

- [TPU Version Codename Matrix](tpu-version-codename-matrix.md) — the authoritative enum-to-codename mapping, the five-axis cross-walk, and the feature matrix
- [Dual Enum (Proto vs Internal)](dual-enum-proto-vs-internal.md) — the `internal = proto − 1` off-by-one and the full wire-value table
- [HAL Families](hal-families.md) — JXC / PXC / VXC factory routing and the per-codename init modules
- [Sub-Core Taxonomy](sub-core-taxonomy.md) — the fetch/load core split and the `gxc::glc` / `gxc::gfc` ISA sub-families
- [Per-Codename HW Constants](per-codename-hw-constants.md) — hardware parameters gated by `TpuVersion`
- [PCI Device IDs](pci-device-ids.md) — DeviceIdentifiers records that bind silicon to a version at discovery
- [ISA Overview](../isa/overview.md) — the codec and bundle-encoder families a version selects
