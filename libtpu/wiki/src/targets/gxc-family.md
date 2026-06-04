# GXC Family (Ghostlite, 6acc60406)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

GXC is the family with no factory of its own. There is no `TpuHalGxcHardwareFactory`, no `TpuChipGxcDriverImpl`, and no `TpuGxcDriver` symbol anywhere in the binary. Instead, GXC's two codenames — Ghostlite (`TpuVersion::kGhostlite` = 4) and 6acc60406 (`k6acc60406` = 5) — are registered into the **shared** `tpu::TpuHalVxcHardwareFactory` by their own init modules (`glc` and `gfc`), and they reuse the entire VXC HAL product chain (`TpuHalVxcHardwareImpl`, `TpuVxcDriver`, the V2 descriptor). GXC is, at the HAL-object level, a VXC family member.

What makes GXC a *distinct* family is its **driver-layer sub-core ISA**. Where Viperfish's ISA lives under `vxc::vfc`/`vxc::vlc`, Ghostlite and 6acc60406 each get their own sub-core namespaces under `asic_sw::driver::deepsea::gxc::`: `gxc::gfc` (the general fetch-core) and `gxc::glc` (the general load-core). Each carries its own `isa` and `profiler` sub-namespace, the codecs bind exclusively to one of them, and the LLVM subtargets (`TPUGlcSubtarget`, `TPUGfcSubtarget`) are GXC-specific. GXC thus inherits the fetch/load split that [PXC](pxc-family.md) introduced but pairs it with its own ISA generation — the newest in the binary.

This page follows the same grammar as the [JXC](jxc-family.md), [PXC](pxc-family.md), and [VXC](vxc-family.md) pages, with two structural caveats: the "Factory Binding" section points at the VXC factory (covered in full on the [VXC page](vxc-family.md)), and the codename pairing of Ghostlite↔glc, 6acc60406↔gfc is itself the central finding.

For reimplementation, the contract is:

- GXC has **no own factory or HAL product** — `glc`/`gfc` init modules register v4/v5 into `TpuHalVxcHardwareFactory`; the products are the VXC products.
- The `gxc::gfc` (fetch-core) / `gxc::glc` (load-core) sub-namespaces, each with its own `isa` and `profiler` — the reason GXC is a family despite sharing the VXC HAL.
- The codename↔sub-core pairing: **Ghostlite (v4) = glc**, **6acc60406 (v5) = gfc** (a corrected mapping, see callout).
- The named-vs-anonymous codec asymmetry: `TpuCodecGhostlite` is fully symbol-named; the v5 codec is symbol-stripped.
- The **shared GlGf bundle encoder**: `ProgramProtoUtil::BundleCount` (`0x1e830e80`) routes both v4 and v5 to one `CreateEncoderGlGf` path, so 6acc60406 reuses Ghostlite's encoder — the reason the two `gxc` sub-core ISA namespaces are near-equal in size despite the named/anonymous split.

| | |
|---|---|
| **Own factory class** | **None** — uses `tpu::TpuHalVxcHardwareFactory` (vtable 0x21cabf70) |
| **TpuVersions** | kGhostlite (4), k6acc60406 (5) |
| **Init modules** | `..._glc_hardware_impl` 0x213eb9e0 (v4), `..._gfc_hardware_impl` 0x213e9f60 (v5) |
| **HAL impl class** | `TpuHalVxcHardwareImpl` (shared, 216 B) — no GXC-specific impl |
| **Sub-cores** | `gxc::gfc` (general fetch-core), `gxc::glc` (general load-core) |
| **Own isa/profiler** | per-sub-core: `gxc::{gfc,glc}::isa`, `gxc::{gfc,glc}::profiler` |
| **DMA engine** | `tpu::TpuVxcDriver` (shared with VXC); DmaDescriptorV2 |

---

## Factory Binding and Registration

### Purpose

GXC's codenames need to reach a HAL implementation, but GXC has no factory class to provide one. The `glc` and `gfc` init modules solve this by constructing a `TpuHalVxcHardwareFactory` instance and registering it under their `TpuVersion`. The HAL object that results is a `TpuHalVxcHardwareImpl` — a VXC product.

### Entry Point

```text
google_init_module_tpu_hal_glc_hardware_impl (0x213eb9e0)   // Ghostlite
  └─ operator new(0x10) ; f[+8] = 4 ; f[+0] = VXC vtable 0x21cabf80
     TpuHalFactory::Register(kHardware, 4, f)

google_init_module_tpu_hal_gfc_hardware_impl (0x213e9f60)   // 6acc60406
  └─ operator new(0x10) ; f[+8] = 5 ; f[+0] = VXC vtable 0x21cabf80  (SAME vtable)
     TpuHalFactory::Register(kHardware, 5, f)
```

### Algorithm

```c
function google_init_module_tpu_hal_glc_hardware_impl():   // 0x213eb9e0 (Ghostlite)
    f = operator_new(0x10)
    f[+8] = 4                               // kGhostlite (mov $0x4)
    f[+0] = &VxcFactory_vtable + 0x10        // VXC vtable 0x21cabf80 — NOT a GXC vtable
    TpuHalFactory::Register(kHardware, 4, unique_ptr(f))
    CHECK(s == OK)   // string 0x94A4A6F: make_unique<TpuHalVxcHardwareFactory>(kGhostlite)

// gfc module (0x213e9f60) is identical with version=5 (mov $0x5), CHECK 0x94A3EF5
```

> **QUIRK —** the CHECK strings prove the binding precisely: both pass `std::make_unique<tpu::TpuHalVxcHardwareFactory>(...)` — the **VXC** factory, not a GXC one. The init module is named for the GXC sub-core (`glc`/`gfc`), but the class it constructs is VXC. This is why the [codename matrix](tpu-version-codename-matrix.md) routes v4/v5 to the VXC family while the [sub-core taxonomy](sub-core-taxonomy.md) lists them under GXC: the HAL family and the driver sub-namespace are decoupled here.

### Function Map

| Function | Address | Role |
|---|---|---|
| `google_init_module_tpu_hal_glc_hardware_impl` | 0x213eb9e0 | Register v4 into VXC factory |
| `google_init_module_tpu_hal_gfc_hardware_impl` | 0x213e9f60 | Register v5 into VXC factory |
| `xla_target_ghostlite` | 0x213eeee0 | compiler-target registration (v4) |

---

## The Factory vtable

GXC has no factory vtable. v4 and v5 use the VXC factory vtable at 0x21cabf70 / vptr 0x21cabf80 in its entirety — slots, typeinfo, `CreateImpl` and all. See the [VXC Family](vxc-family.md#the-factory-vtable) page for the full 5-slot walk and the 216-byte impl. The only GXC-visible state at the HAL-object level is the `TpuVersion` (4 or 5) stored at factory `+8` and threaded into the impl at `+0x78`.

> **NOTE —** because GXC reuses the VXC `CreateImpl` (which reads the version from the work-queue), the only difference between a Ghostlite HAL object and a Viperfish one, at construction time, is the `TpuVersion` value. The per-codename behaviour is resolved further down, in `TpuHalVxcCommonHelper::InitializeDrivers` (the `cmp $5 / $4 / $3` switch documented on the VXC page) and in the codecs.

---

## Sub-Core Sub-Namespace Roster

The GXC story is the driver layer. `asic_sw::driver::deepsea::gxc::` has exactly two direct sub-namespaces — the fetch/load-core pair — and, unlike PXC and VXC, **no family-level `isa` or `profiler`**: GXC carries its ISA and profiler entirely under the sub-cores.

| Sub-namespace | Role |
|---|---|
| `gxc::gfc` | **general fetch-core** — the 6acc60406 (v5) sub-core |
| `gxc::glc` | **general load-core** — the Ghostlite (v4) sub-core |
| `gxc::gfc::isa` | v5 ISA (270K symbols) — e.g. `TensorCoreBundleCompact`, SparseCore Tec codecs |
| `gxc::glc::isa` | v4 ISA (294K symbols) — e.g. `TensorCoreBundleCompact`, SparseCore codecs |
| `gxc::gfc::profiler` | v5 profiler (48K symbols), with named `TraceEntry` class (4781 token occ.) |
| `gxc::glc::profiler` | v4 profiler (29K symbols), with named `TraceEntry` class (4590 token occ.) |

> **NOTE —** there is no *family-level* `gxc::isa` (the search for `deepsea3gxc3isa` returns zero); the ISA lives one level deeper than PXC's/VXC's, under the sub-cores (`gxc::gfc::isa`, `gxc::glc::isa`, with matching `profiler` namespaces). GXC shares the VXC family only at the HAL-object layer (the common factory/impl); the ISA layer is wholly GXC-specific.

> **QUIRK —** the naming pairing is counter-intuitive and was a documented off-by-one source. **Ghostlite (v4) binds to `gxc::glc`** (load-core) and **6acc60406 (v5) binds to `gxc::gfc`** (fetch-core). The codec walks confirm this at the symbol level: `TpuCodecGhostlite` dispatches exclusively to `gxc::glc::isa` + `ghostlite::isa::EncoderGl*`; the anonymous v5 codec dispatches exclusively to `gxc::gfc::isa`. By the binary's own naming axes (`TpuVersionToExternalName` @ `0x20b3a500`), Ghostlite displays as `"TPU v6 lite"` (Cloud `accelerator_type` `v6e`) and 6acc60406 as `"TPU7x"` (Cloud `tpu7x`) — a full generation apart. A reimplementation that reads `gfc` as "the v6e fetch core" is one generation off. (Public marketing names for these generations are not embedded in the binary and are deliberately not cited here — see [Marketing / Cloud Naming](marketing-cloud-naming.md).)

---

## Construction Chain Below the Factory

### Purpose

GXC has no construction chain of its own. A Ghostlite or 6acc60406 HAL object is a `TpuHalVxcHardwareImpl`, and `CreateAndInitializeChips` is the VXC method at 0x1d110f20. The GXC-specific work happens inside the helper's per-codename switch, where the chip-parts version (4 or 5) selects the device scanner and validates the variant.

### Entry Point

```text
TpuHalVxcHardwareImpl::CreateAndInitializeChips (0x1d110f20)   ── shared VXC method
  └─ TpuHalVxcCommonHelper::InitializeDrivers (0x1d111a80)      ── per-codename switch
       ├─ case 4 (Ghostlite): validate variant; else "ghostlite unsupported variant " (0xa1d992b)
       ├─ case 5 (6acc60406): validate variant; else "6acc60406 unsupported variant " (0xa1d990c)
       └─ generic: asic_sw::deepsea::DeepseaDeviceScanner (0x1fba7a20)
```

### Considerations

The Ghostlite (v4) and 6acc60406 (v5) branches of the switch are reject-validators: they confirm the chip-parts `variant_name()` against a per-codename allow-list and error out on an unsupported variant. Unlike the Viperfish (v3) branch, neither v4 nor v5 constructs a codename-specific device scanner — they fall through to the shared `DeepseaDeviceScanner`. The product objects are the VXC ones in full: `TpuChipVxcDriverImpl` (416 B), the 296-byte `SyncFlagResources`, and `TpuCoreVxcDriverImpl` (800 B), each taking a `TpuVxcDriver*`. See [VXC Family](vxc-family.md#construction-chain-below-the-factory) for the chain detail.

---

## Compiler-Target and Cost-Model Binding

### Purpose

While the HAL/driver layer is shared with VXC, the *compiler* layer is GXC-specific. Each GXC codename registers its own LLVM-style compiler target, its own cost-model cycle table, and (for Ghostlite) its own LLO-to-instruction mapping table.

### Function Map

| Artefact | Address / symbol | Codename |
|---|---|---|
| `xla_target_ghostlite` | 0x213eeee0 (`mov edi,4`) | Ghostlite (v4) |
| `GlcCycleTable` vtable | `_ZTV` 0x21c20148 | Ghostlite (v4) |
| `GfcCycleTable` vtable | `_ZTV` 0x21c201c8 | 6acc60406 (v5) |
| `TPUGlcSubtarget` | predicate-regs @ 0x13c615c0 (16) | Ghostlite (v4) |
| `TPUGfcSubtarget` | predicate-regs @ 0x13c630e0 (16) | 6acc60406 (v5) |
| `GhostliteCodecMetadata` vtable | `_ZTV` 0x21d647a8 | Ghostlite (v4) |
| `xla::ghostlite::kLloOpcodeToGlcInstruction` | 0x4067dc8 (258 entries) | Ghostlite (v4) |

### Considerations

The asymmetry between v4 and v5 is sharpest here. Ghostlite has a *named* compiler-target init (`xla_target_ghostlite`), a *named* `GhostliteCodecMetadata`, and a named LLO-mapping table; 6acc60406 has the cost-model `GfcCycleTable` (named) but no `xla_target_6acc60406` symbol and no named codec metadata — its codec metadata is an anonymous entry in the `CodecMetadataRegistry` (singleton guard 0x22582fa0). Both subtargets report 16 predicate registers, so the predicate-register count is *not* the v4/v5 distinguishing axis; the bundle opcode/predicate field widths are (see Per-Codename Differentiation below).

> **NOTE —** the GXC cost model lives under the compiler-base namespace (`xla::jellyfish::GlcCycleTable` / `GfcCycleTable`), exactly like JXC's `JfCycleTable` — another instance of generation-specific compiler state hanging off the shared `xla::jellyfish::` base while the driver state lives under `asic_sw::driver::deepsea::gxc::`. The two trees are documented in the [Sub-Core Taxonomy](sub-core-taxonomy.md#the-deepsea-umbrella-and-the-compiler-base-namespace).

---

## Per-Codename Differentiation

Ghostlite and 6acc60406 share the VXC HAL product chain (impl 216 B, `TpuVxcDriver`, V2 descriptor) and one bundle encoder (the shared `CreateEncoderGlGf` path that `ProgramProtoUtil::BundleCount` at `0x1e830e80` selects for both v4 and v5), but diverge sharply at the codec/ISA layer. The clearest divergence is symbol visibility and the TensorCore bundle encoding.

> **NOTE —** the shared GlGf encoder is why the two `gxc` sub-core ISA namespaces are near-equal in token weight (`gxc::glc::isa` 294K, `gxc::gfc::isa` 270K) despite the named-vs-anonymous codec split: 6acc60406 inherits Ghostlite's encoder machinery rather than carrying a wholly independent one. The same pairing shows on the version-dispatch side — `BundleCount`'s switch collapses the six generations to four encoder families (`JfDf`, `Pf`, `Vf`, `GlGf`), with v4+v5 sharing `GlGf` exactly as v0+v1 share `JfDf` (see the [Codename Matrix](tpu-version-codename-matrix.md#codec-selection-confirms-the-ordering)).

| Axis | Ghostlite (v4 / glc) | 6acc60406 (v5 / gfc) | Source |
|---|---|---|---|
| TpuVersion enum | kGhostlite = 4 | k6acc60406 = 5 | `TpuVersionToString` 0x20b3a480 |
| ToString | "ghostlite" (0x86864e0) | "6acc60406" (0x863f0cf) | rodata |
| External / Cloud name | "TPU v6 lite" / `v6e` | "TPU7x" / `tpu7x` | naming path |
| Init module | glc 0x213eb9e0 | gfc 0x213e9f60 | symtab |
| Sub-core | `gxc::glc` (load-core) | `gxc::gfc` (fetch-core) | symtab |
| Codec class | `TpuCodecGhostlite` (NAMED, `_ZTV` 0x21d35c00) | anonymous (no codec `_ZTV`/`_ZTI`) | codec walks |
| Codec creator | `CreateTpuCodecGhostlite` 0x1e83bce0 (named) | `sub_1E838380` (inline, anon) | disasm |
| Named workers | `ghostlite::isa::EncoderGl{TC,Scs,Tac,Tec}` | gfc `EncoderBase` templates (no `EncoderGf*`) | symtab |
| LLVM subtarget | `TPUGlcSubtarget` | `TPUGfcSubtarget` | symtab |
| Cost-model table | `GlcCycleTable` (`_ZTV` 0x21c20148) | `GfcCycleTable` (`_ZTV` 0x21c201c8) | symtab |
| Codec metadata | `GhostliteCodecMetadata` (NAMED, `_ZTV` 0x21d647a8) | anonymous registry entry | disasm |
| Codec platform tag (cmp) | `$0x5` (proto value for Ghostlite) | `$0x6` (proto value for 6acc60406) | Encode body |
| Entry-point opcodes | 0x11/0x12/0x13/0x14 (TC/Scs/Tac/Tec) | 0x15/0x16/0x17 (+4 shift) | Encode body |
| TensorCore bundle | 64 B; 4 VALU; 7-bit opcode @302; 4-bit pred @309 | 64 B; 4 VALU; 8-bit opcode @293; 2-bit dual pred @301 | VALU0 encoder bytes |
| TC CodecBase object | 240 B (1 `Predication` param) | 248 B (4 predication params) | EncodeBundle `new()` |
| PCI chip DID | 0x00d1 (named `kGhostliteChip*`) | 0x00f2 (anonymous gfc) | DeviceIdentifiers |
| Device-type byte | 0xd (`IsGlc`) | 0xc (`IsGfc`) | `DeviceTypeFromDeviceIdentifiers` |
| Flag prefix | `xla_gf_` (44), `xla_sc_` | `xla_gf_`, `xla_sc_` | flag scan |

> **GOTCHA —** v5 (6acc60406 / gfc) is the **only fully symbol-stripped codename**. Its codec class, vtable, typeinfo, and creator carry no demangled names; v0..v4 all retain `_ZTV`/`_ZTI`/`_ZTS` symbols (only the RTTI name-string *content* is obfuscated to `s_NNNNN` tags for all six codecs). A reimplementer cross-checking by symbol will find Ghostlite by name but will recover the v5 codec only structurally (8-byte object, 6-slot vtable, `gxc::gfc::isa` dispatch). The v4→v5 codec delta — opcode widens 7→8 bits, per-slot predicate shrinks 4→2 bits into a dedicated dual-predicate slot, entry-point opcodes shift +4, TC CodecBase grows 240→248 B — is the structural fingerprint to match.

---

## Cross-References

- [VXC Family](vxc-family.md) — the family GXC registers into; the shared factory, impl, driver, and `InitializeDrivers` switch
- [JXC Family](jxc-family.md) — the fused-dataflow ancestor with no fetch/load split
- [PXC Family](pxc-family.md) — introduced the fetch/load split GXC's sub-cores inherit
- [Sub-Core Taxonomy](sub-core-taxonomy.md) — `gfc`/`glc` as the newest fetch/load sub-cores; the trace-entry classes
- [HAL Families](hal-families.md) — the shared `TpuHalFactory` base chain
- [Codename Matrix](tpu-version-codename-matrix.md) — v4/v5 routed to VXC family; the Ghostlite/6acc60406 enum entries
- [HAL Factory Override Matrix](hal-factory-override-matrix.md) — the VXC impl override table v4/v5 share
- [Per-Codename HW Constants](per-codename-hw-constants.md) — the v6e/v7x chip-parts constants (256×256 MXU, SparseCore sequencer deltas) behind the bundle/ISA divergence
- [PCI Device IDs](pci-device-ids.md) — the source for the chip-DID (`0x00d1`/`0x00f2`) and device-type (`0xd`/`0xc`) rows in the differentiation table
- [Marketing / Cloud Naming](marketing-cloud-naming.md) — the `v6e`/`tpu7x` Cloud names and why public marketing names are external-only, not binary facts
