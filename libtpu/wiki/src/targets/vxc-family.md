# VXC Family (Viperfish)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

VXC is the broadest of the four HAL families. The factory class `tpu::TpuHalVxcHardwareFactory` — uniquely placed in the **global** namespace, where JXC and PXC are anonymous-namespace — serves three codenames: Viperfish (`TpuVersion::kViperfish` = 3), Ghostlite (`kGhostlite` = 4), and 6acc60406 (`k6acc60406` = 5). Viperfish is VXC's "home" codename and the family is named for it; Ghostlite and 6acc60406 are registered into this same factory by the [GXC family](gxc-family.md)'s `glc`/`gfc` init modules and sit operationally inside VXC.

VXC inherits the fetch/load-core split that [PXC](pxc-family.md) introduced: `asic_sw::driver::deepsea::vxc::vfc` (the vector fetch-core) and `vxc::vlc` (the vector load-core). Its defining new trait is **SparseCore** — Viperfish is the first generation to carry one, and the SparseCore introduction is matched by the retirement of BarnaCore. The VXC HAL impl is also the only one of the four that is **216 bytes** rather than 208; the extra 8 bytes are a single flag byte at offset `+0xD0`.

This page follows the same grammar as the [JXC](jxc-family.md), [PXC](pxc-family.md), and [GXC](gxc-family.md) pages. Because GXC registers into this factory, the codename-dispatch story is told here in full; the GXC page covers what is GXC-specific (its codecs and sub-core ISA). For the shared base chain, see [HAL Families](hal-families.md).

For reimplementation, the contract is:

- The multi-version registry-level dispatch: one global-ns class, one vtable, three 16-byte instances keyed by `TpuVersion`, registered by three separate init modules.
- The 5-slot factory vtable (identical shape to JXC/PXC) and the **216-byte** `TpuHalVxcHardwareImpl` with its `+0xD0` skip-slicebuilder flag.
- The construction chain HardwareImpl → CommonHelper (48 B) → Chip (416 B) → Core (800 B), with the genuine per-codename switch in `InitializeDrivers`.
- The `vxc::vfc` / `vxc::vlc` fetch/load split, the `vxc::isa` namespace, and the unified `TpuVxcDriver` (V2 descriptor, no DMA-issuer).

| | |
|---|---|
| **Factory class** | `tpu::TpuHalVxcHardwareFactory` (**global-ns**, the lone exception) |
| **TpuVersions served** | 3 — kViperfish (3), kGhostlite (4), k6acc60406 (5) |
| **Factory vtable / vptr** | `_ZTV` 0x21cabf70 / installed vptr 0x21cabf80 |
| **Factory typeinfo** | `_ZTI` 0x21cabfa8 (`__si` base → `TpuHalHardwareFactoryBase` 0x21d343f8) |
| **HAL impl class / size** | `TpuHalVxcHardwareImpl`, **216 B (0xD8)**, vtable 0x21cabfc0 |
| **Init modules** | vxc 0x213eed20 (v3), glc 0x213eb9e0 (v4), gfc 0x213e9f60 (v5) — 1× Register each |
| **Fetch/load split** | `vxc::vfc` + `vxc::vlc` (inherited from PXC) |
| **DMA engine** | unified `tpu::TpuVxcDriver` (no issuer); DmaDescriptorV2 |

---

## Factory Binding and Registration

### Purpose

VXC binds three codenames to one HAL implementation. Unlike JXC (one init module, two `Register` calls), VXC uses **three separate init modules**, each calling `Register` exactly once. The principle is identical — one class, one vtable, N registry entries keyed by `TpuVersion` at `+8` — but the registration sites are spread across the GXC family modules.

### Entry Point

```text
google_init_module_tpu_hal_vxc_hardware_impl (0x213eed20)  → Register(kHardware, 3, f)  // Viperfish
google_init_module_tpu_hal_glc_hardware_impl (0x213eb9e0)  → Register(kHardware, 4, f)  // Ghostlite
google_init_module_tpu_hal_gfc_hardware_impl (0x213e9f60)  → Register(kHardware, 5, f)  // 6acc60406
  (each: operator new(0x10); f[+8] = version; f[+0] = vptr 0x21cabf80; same vtable)
```

### Algorithm

```c
function register_vxc_codename(version):    // one of three init modules
    f = operator_new(0x10)                  // 16-byte factory object
    f[+8] = version                         // 3, 4, or 5
    f[+0] = &VxcFactory_vtable + 0x10        // installed vptr 0x21cabf80 (SHARED)
    s = TpuHalFactory::Register(kHardware /*0*/, version, unique_ptr(f))
    CHECK(s == OK)
    // CHECK strings byte-confirm the codename → factory binding:
    //   0x94A3FA6 kViperfish, 0x94A4A6F kGhostlite, 0x94A3EF5 k6acc60406
    //   each: std::make_unique<tpu::TpuHalVxcHardwareFactory>(tpu::TpuVersion::kX)
```

> **QUIRK —** the VXC factory ctor *does* take a `TpuVersion` (the CHECK strings pass `kViperfish`/`kGhostlite`/`k6acc60406` to `make_unique`), exactly because it must serve three versions — like JXC, unlike PXC's argument-less ctor. There are **exactly three** references to the factory vtable 0x21cabf70 in the whole binary, all three the init-module `lea` sites. No other code references the factory class.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `google_init_module_tpu_hal_vxc_hardware_impl` | 0x213eed20 | Register v3 (Viperfish) | CERTAIN |
| `google_init_module_tpu_hal_glc_hardware_impl` | 0x213eb9e0 | Register v4 (Ghostlite) — GXC module | CERTAIN |
| `google_init_module_tpu_hal_gfc_hardware_impl` | 0x213e9f60 | Register v5 (6acc60406) — GXC module | CERTAIN |
| `TpuHalFactory::Register` | 0x1fbb16a0 | registry insert (shared) | CERTAIN |

---

## The Factory vtable

### Purpose

VXC's factory vtable has the same 5-slot shape as JXC and PXC; only the namespace placement (global vs anonymous) and the two overridden function addresses differ.

### Vtable Layout

| vaddr | slot | resolves to | base/override | Confidence |
|---|---|---|---|---|
| 0x21cabf80 | 0 — `~TpuHalFactory()` D2 | 0x0e723a80 (`ret`) | INHERITED | CERTAIN |
| 0x21cabf88 | 1 — `~TpuHalVxcHardwareFactory()` D0 | 0x1d110e80 | **OVERRIDE** | CERTAIN |
| 0x21cabf90 | 2 — `HardwareFactoryBase::Create(wq)` | 0x1e80f560 | INHERITED | CERTAIN |
| 0x21cabf98 | 3 — `HardwareFactoryBase::CanCreate()` | 0x1e80f520 | INHERITED | CERTAIN |
| 0x21cabfa0 | 4 — `TpuHalVxcHardwareFactory::CreateImpl(wq)` | 0x1d110e00 | **OVERRIDE** | CERTAIN |

Slot 1 encodes `operator delete(this, 0x10)` — the factory is 16 bytes, like all four families.

### Algorithm — CreateImpl

```c
function TpuHalVxcHardwareFactory::CreateImpl(out, this, wq):   // 0x1d110e00
    obj     = operator_new(0xD8)              // 216 B = TpuHalVxcHardwareImpl
    version = *(u32*)(wq + 8)                 // TpuVersion from work-queue+8
    TpuHal::TpuHal(obj, version, wq)          // base ctor 0x1e811c00: wq→+0x68, version→+0x78
    *(void**)(obj + 0)    = &VxcImpl_vtable + 0x10   // plant 0x21cabfc0 → 0x21cabfd0
    *(void**)(obj + 0xC8) = nullptr            // CommonHelper slot
    *(byte*) (obj + 0xD0) = 0                  // skip-slicebuilder flag (VXC-ONLY; the extra 8 B)
    out.value = obj ; out.status = OK
    return out
```

> **QUIRK —** the **extra 8 bytes** of the VXC impl are a single bool at offset `+0xD0` (padded to 8). It is the *skip-slicebuilder / use-default-configured-properties* toggle: `CreateAndInitializeChips` sets it from `FLAGS_deepsea_hal_test_skip_slicebuilder` (0x22398628); `GetConfiguredProperties` (slot 8) reads it to choose `GetDefaultConfiguredProperties(topology)` vs the helper's cached state; `PreTearDownChips` (slot 21) reads it to skip mesh teardown. The flag exists only on VXC because only VXC has a multi-chip slice-builder mesh that *can* be skipped. JXC/PXC have no slice-builder, hence no flag, hence 208 bytes. It is **not** a sparsity-state pointer.

### Considerations

The 216-byte impl size is double-proven: `operator new(0xD8)` in `CreateImpl` and `operator delete(this, 0xD8)` in the impl D0 dtor. VXC uniquely overrides impl slot 18 (`WaitForCoreDumpComplete`, 0x1d110f00) among the three families; its impl override count is 8 (slots 0, 1, 2, 8, 18, 19, 20, 21). See the [HAL Factory Override Matrix](hal-factory-override-matrix.md).

---

## Construction Chain Below the Factory

### Purpose

`TpuHalVxcHardwareImpl::CreateAndInitializeChips` (impl vtable slot 20, @ 0x1d110f20) drives the product graph and is the only one of the four families that sets the `+0xD0` flag and initializes a slice-builder mesh.

### Entry Point

```text
TpuHalVxcHardwareImpl::CreateAndInitializeChips (0x1d110f20)
  ├─ TpuChipParts::CoreCount / SharedMemoryCount   ── data-driven constraints
  ├─ this[+0xD0] = FLAGS_deepsea_hal_test_skip_slicebuilder & 1
  ├─ TpuHalVxcCommonHelper (48 B, 0x1d111a60)        ── stored at impl+0xC8
  ├─ helper->InitializeDrivers(options, skip)        ── per-codename switch (0x1d111a80)
  ├─ helper->MaybeInitializeSliceBuilder (0x1d113120)
  └─ helper->CreateChips (0x1d113440)
       └─ TpuChipVxcDriverImpl (416 B, 0x1d114120)    ── + SyncFlagResources (296 B)
            └─ core-factory lambda
                 └─ TpuCoreVxcDriverImpl (800 B, 0x1d118340)  ── takes a TpuVxcDriver*
```

### Algorithm — the per-codename switch

```c
function TpuHalVxcCommonHelper::InitializeDrivers(options, skip):   // 0x1d111a80
    chip_parts = *(options + 8)               // chip-parts proto pointer
    version    = *(u32*)chip_parts            // first u32 of chip-parts proto
    variant    = TpuChipParts::variant_name() // 0x20b1eb40 (sv: ptr in rax, len in rdx)
    switch (version):                          // inline cmp $5 / $4 / $3
        case 5 (6acc60406): if variant non-empty: InvalidArgument "6acc60406 unsupported variant " (0xa1d990c)
                            else if platform==0: scanner = sub_1FBA82A0 / CreateMultiVfScannerAdapter
        case 4 (Ghostlite): if variant non-empty: InvalidArgument "ghostlite unsupported variant " (0xa1d992b)
                            else if platform==0: scanner = asic_sw::deepsea::DeepseaDeviceScanner (0x1fba7a20)
        case 3 (Viperfish): scanner = vxc::vfc::VfDeviceScanner (0x1d1b0e20)
                            /* or vxc::vlc::VfDeviceScanner (0x1d1b0be0) when variant=="lite"
                               and FLAGS_vxc_virtual_function / qword_22398620 is set */
        default:            LogMessageFatal "TpuVersion <N> not supported." (line 501)
    // a nonzero platform type in any case → MakeError "TPU Platform Type `%s` is not supported."
```

> **NOTE —** this is the *only* genuine per-`TpuVersion` switch in the whole HAL tree, and it lives in the `CommonHelper`, not the factory or the impl. The factory layer is class-uniform; the impl methods carry no version switch. The dispatch is driver-init-level and data-fed from the chip-parts proto's first u32 — it selects the per-codename device scanner, it does not branch HAL-object construction.

### Considerations

The per-core ctor takes a `TpuVxcDriver*` — the unified VXC driver, not a separate DMA-issuer (contrast JXC's `JfDmaIssuer*`). `TpuVxcDriver` builds the V2 DMA descriptor (`asic_sw::deepsea::dma::Descriptor`, 14-bit sync flag) and configures ICI routing via `EnableNHopRouting` / `SetRoutingStrategy`. The chip object additionally allocates a 296-byte `SyncFlagResources` block.

---

## Driver Sub-Namespace Roster

`asic_sw::driver::deepsea::vxc::` carries the fetch/load split and a family-level ISA. Direct sub-namespaces, confirmed in the symbol table:

| Sub-namespace | Confidence | Role |
|---|---|---|
| `vxc::vfc` | CERTAIN | **vector fetch-core** — fetch-side instruction stream |
| `vxc::vlc` | CERTAIN | **vector load-core** — load-side instruction stream |
| `vxc::isa` | CERTAIN | family-level ISA (170K symbols) |

Below the fetch-core sit `vxc::vfc::isa` (67K symbols), `vxc::vfc::profiler` (40K symbols, with the named `TraceEntry` class, 4015 symbols), and on the load side `vxc::vlc::profiler` (12K symbols, `TraceEntry`, 3001 symbols). The SparseCore ISA bundles (`SparseCoreScsBundle`, `SparseCoreTacBundle`) live under `vxc::vfc::isa` — in the ISA/codec layer, not the 216-byte HAL impl. Each `vxc::vfc` / `vxc::vlc` carries its own `profiler::TraceEntry` class, making both sub-cores trace-entry sub-cores in the [taxonomy](sub-core-taxonomy.md).

---

## Per-Codename Differentiation

The three codenames behind VXC differ in data (chip-parts) and in their *codec* classes, but share one HAL impl, one `TpuVxcDriver`, one V2 descriptor. Viperfish has its own named workers under `viperfish::isa` (`EncoderVfTensorCore`, `DecoderVfTensorCore`); Ghostlite and 6acc60406 are detailed on the [GXC page](gxc-family.md).

| Axis | Viperfish (v3) | Ghostlite (v4) | 6acc60406 (v5) | Source | Confidence |
|---|---|---|---|---|---|
| TpuVersion enum | kViperfish = 3 | kGhostlite = 4 | k6acc60406 = 5 | `TpuVersionToString` 0x20b3a480 | CERTAIN |
| ToString | "viperfish" | "ghostlite" | "6acc60406" | rel.ro table 0x22011BF0 | CERTAIN |
| External name | "TPU v5" (v5p / v5e) | "TPU v6 lite" (v6e) | "TPU7x" (tpu7x) | `TpuVersionToExternalName` 0x20b3a500 | CERTAIN |
| Init module | vxc 0x213eed20 | glc 0x213eb9e0 | gfc 0x213e9f60 | symtab | CERTAIN |
| Codec (case in `TpuCodec::Create` 0x1e835fa0) | `CreateTpuCodecViperfish` (case 3) | `CreateTpuCodecGhostlite` (case 4) | anonymous `sub_1E838380` (case 5) | symtab | CERTAIN (v3/v4); MEDIUM (v5 anon) |
| TensorCore / BarnaCore | yes / no | yes / no | yes / no | TpuChipParts | CERTAIN |
| SparseCore | yes (first gen) | yes | yes | TpuChipParts | CERTAIN |
| Driver sub-core ISA | `vxc::vfc/vlc::isa` | `gxc::glc::isa` | `gxc::gfc::isa` | symtab | CERTAIN |
| Flag prefixes | `xla_vf_` (50), `xla_sc_` (164) | `xla_gf_` (44), `xla_sc_` | `xla_gf_`, `xla_sc_` | flag scan | HIGH |

> **GOTCHA —** Ghostlite and 6acc60406 register into *this* (VXC) factory, but their driver-layer ISA lives under the **`gxc`** namespace (`gxc::glc::isa`, `gxc::gfc::isa`), not `vxc`. Only Viperfish's ISA is under `vxc`. The HAL family and the driver sub-namespace are decoupled for v4/v5 — the reason both pages cross-link. See [GXC Family](gxc-family.md).

---

## Cross-References

- [JXC Family](jxc-family.md) — the fused-dataflow ancestor with a separate `JfDmaIssuer`
- [PXC Family](pxc-family.md) — introduced the fetch/load split VXC inherits; the V2 descriptor's first user
- [GXC Family](gxc-family.md) — Ghostlite + 6acc60406, registered into this VXC factory via glc/gfc init modules
- [Sub-Core Taxonomy](sub-core-taxonomy.md) — `vfc`/`vlc` in the fetch/load-split evolution; SparseCore introduction
- [HAL Families](hal-families.md) — the shared `TpuHalFactory` base chain and template-method `Create`
- [Codename Matrix](tpu-version-codename-matrix.md) — the 6-value `TpuVersion` enum and HAL routing
- [HAL Factory Override Matrix](hal-factory-override-matrix.md) — the per-impl 23-slot override tables
