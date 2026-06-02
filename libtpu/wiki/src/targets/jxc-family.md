# JXC Family (Jellyfish, Dragonfish)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

JXC is the oldest of the four TPU HAL families libtpu still carries. One C++ class — `tpu::(anonymous namespace)::TpuHalJxcHardwareFactory` — serves **two** silicon generations: Jellyfish (`TpuVersion::kJellyfish` = 0) and Dragonfish (`TpuVersion::kDragonfish` = 1). The factory is a thin 16-byte object whose only family-specific behaviour is allocating the right HAL implementation; everything that distinguishes Jellyfish from Dragonfish is data-driven through the embedded `TpuChipParts` proto, not the C++ type.

The defining architectural trait of JXC, and the reason it anchors the [sub-core taxonomy](sub-core-taxonomy.md), is that it has **no fetch/load-core split**. Where every later family (PXC, VXC, GXC) divides each core's instruction stream into a fetch-core and a load-core sub-namespace, JXC's driver layer is a single fused dataflow. Its driver sub-namespaces under `asic_sw::driver::deepsea::jxc::` are organized by *engine block* (`dfc`, `jfc`, `registers`, `snap`, the `*_trace_entry` set) rather than by fetch/load role. The compiler-side ISA for both generations lives in `xla::jellyfish::isa`, not in any `jxc::isa` namespace.

This page follows the same grammar as the [PXC](pxc-family.md), [VXC](vxc-family.md), and [GXC](gxc-family.md) family pages: factory binding, the construction path, the sub-namespace roster, and the per-codename differentiation. For the abstract base chain (`TpuHalFactory` → `TpuHalHardwareFactoryBase` → leaf) shared by all four families, see [HAL Families](hal-families.md).

For reimplementation, the contract is:

- The single-class / two-version registration model: one vtable, two 16-byte instances keyed by `TpuVersion` at object offset `+8`.
- The 5-slot factory vtable and the `CreateImpl` allocator that yields a 208-byte `TpuHalJxcHardwareImpl`.
- The construction chain HardwareImpl → CommonHelper → Chip → Core, and the `JfDmaIssuer` DMA engine threaded into each core.
- The driver sub-namespace roster — and the explicit fact that there is no fetch/load split and no `jxc::isa`.

| | |
|---|---|
| **Factory class** | `tpu::(anonymous namespace)::TpuHalJxcHardwareFactory` (anon-ns) |
| **TpuVersions served** | 2 — kJellyfish (0), kDragonfish (1) |
| **Factory vtable / vptr** | `_ZTV` 0x215fe530 / installed vptr 0x215fe540 |
| **Factory typeinfo** | `_ZTI` 0x215fe568 (`__si` base → `TpuHalHardwareFactoryBase` 0x21d343f8) |
| **HAL impl class / size** | `TpuHalJxcHardwareImpl`, 208 B (0xD0), vtable 0x215fe580 |
| **Init module** | `google_init_module_tpu_hal_jxc_hardware_impl` @ 0x213e9d80 (2× Register) |
| **Fetch/load split** | **None** — fused dataflow (defining JXC trait) |
| **DMA engine** | `JfDmaIssuer` (separate per-core object) |

---

## Factory Binding and Registration

### Purpose

The factory selects, at `dlopen` time, which HAL implementation a given `TpuVersion` will instantiate. JXC is the only family whose factory class is registered for more than one version, because Jellyfish and Dragonfish are architecturally close enough to share one HAL implementation. (The `xla_*` flag prefixes confirm the kinship: there is no `xla_df_` prefix at all — Dragonfish reuses Jellyfish's `xla_jf_` flags entirely.)

### Entry Point

```text
google_init_module_tpu_hal_jxc_hardware_impl (0x213e9d80)
  ├─ operator new(0x10)                     ── factory instance #1 (16 B)
  │    f0[+8] = 0 (kJellyfish) ; f0[+0] = vptr 0x215fe540
  │    TpuHalFactory::Register(kHardware, 0, f0)   (0x1fbb16a0)
  └─ operator new(0x10)                     ── factory instance #2 (16 B)
       f1[+8] = 1 (kDragonfish) ; f1[+0] = vptr 0x215fe540  (SAME vtable)
       TpuHalFactory::Register(kHardware, 1, f1)
```

### Algorithm

```c
function google_init_module_tpu_hal_jxc_hardware_impl():   // 0x213e9d80
    // Two registry entries, one shared vtable. The instances differ
    // ONLY in the TpuVersion stored at +8.
    for version in {0 /*kJellyfish*/, 1 /*kDragonfish*/}:
        f = operator_new(0x10)                  // 16-byte factory object
        f[+8] = version                         // u32 TpuVersion key
        f[+0] = &JxcFactory_vtable + 0x10        // installed vptr 0x215fe540
        s = TpuHalFactory::Register(kHardware /*0*/, version, unique_ptr(f))
        CHECK(s == OK)   // fail strings 0x94a3e44 (kJellyfish), 0x94a4057 (kDragonfish)
```

> **QUIRK —** there is no class-level Jellyfish-vs-Dragonfish dispatch. Both 16-byte instances point at the *identical* vtable `0x215fe540` and differ only by the `TpuVersion` u32 at `+8`. The two CHECK strings byte-confirm `std::make_unique<TpuHalJxcHardwareFactory>(...kJellyfish)` and `(...kDragonfish)` — i.e. the JXC factory constructor *does* take a `TpuVersion` argument, unlike PXC's argument-less ctor. Per-codename behaviour (core counts, MXU shape, HBM cap) is resolved later, from `TpuChipParts`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `google_init_module_tpu_hal_jxc_hardware_impl` | 0x213e9d80 | 2× Register (v0, v1) | CERTAIN |
| `TpuHalFactory::Register` | 0x1fbb16a0 | registry insert `[platform][version]` | CERTAIN |
| `TpuHalFactory::Get` | 0x1fbb19c0 | runtime registry lookup under mutex | CERTAIN |
| `TpuHal::Create` | 0x1e814180 | public entry: Get → Create → bind profiler | CERTAIN |

---

## The Factory vtable

### Purpose

The factory exposes the abstract 5-slot `TpuHalFactory` interface. JXC overrides only two slots; the other three are *literally the same function addresses* PXC and VXC point at — the base `Create`/`CanCreate` are shared, not copied per family.

### Vtable Layout

| vaddr | slot | resolves to | base/override |
|---|---|---|---|
| 0x215fe540 | 0 — `~TpuHalFactory()` D2 | 0x0e723a80 (`ret`) | INHERITED |
| 0x215fe548 | 1 — `~TpuHalJxcHardwareFactory()` D0 | 0x0e723aa0 | **OVERRIDE** |
| 0x215fe550 | 2 — `HardwareFactoryBase::Create(wq)` | 0x1e80f560 | INHERITED |
| 0x215fe558 | 3 — `HardwareFactoryBase::CanCreate()` | 0x1e80f520 | INHERITED |
| 0x215fe560 | 4 — `TpuHalJxcHardwareFactory::CreateImpl(wq)` | 0x0e723ac0 | **OVERRIDE** |

Slot 1 (the deleting destructor) encodes `operator delete(this, 0x10)` — proof the factory object is 16 bytes. Slot 2 is the GoF template method: `Create` calls slot 3 (`CanCreate`) then slot 4 (`CreateImpl`), else builds a `NotFoundError`. Slot 3 reads the factory's stored `TpuVersion` at `+8` and matches it against `ScanHardwareDevices` (0x1fba53c0).

### Algorithm — CreateImpl

```c
function TpuHalJxcHardwareFactory::CreateImpl(out, this, wq):   // 0x0e723ac0
    version = *(u32*)(this + 8)              // factory's stored TpuVersion (0 or 1)
    obj     = operator_new(0xD0)              // 208 B = TpuHalJxcHardwareImpl
    TpuHal::TpuHal(obj, version, wq)          // base ctor 0x1e811c00: wq→+0x68, version→+0x78
    *(void**)(obj + 0)    = &JxcImpl_vtable + 0x10   // plant 0x215fe580 → 0x215fe590
    *(void**)(obj + 0xC8) = nullptr            // CommonHelper slot, null until CreateAndInitializeChips
    out.value = obj ; out.status = OK
    return out
```

> **NOTE —** the JXC `CreateImpl` reads the version from `this+8` (the factory's own stored key), because the class serves two versions. PXC, serving one, hardcodes the literal `2`; VXC reads it from the work-queue. The 208-byte impl and the helper slot at `+0xC8` are common to JXC and PXC; VXC's impl is 216 bytes (an extra flag byte at `+0xD0`). See the [HAL Factory Override Matrix](hal-factory-override-matrix.md) for the full impl override table.

---

## Construction Chain Below the Factory

### Purpose

The factory returns only the HAL object. The chip/core graph is built lazily in `TpuHalJxcHardwareImpl::CreateAndInitializeChips` (impl vtable slot 20, @ 0x0e723c20), invoked during `Initialize`.

### Entry Point

```text
TpuHalJxcHardwareImpl::CreateAndInitializeChips (0x0e723c20)
  ├─ TpuChipParts::CoreCount / SharedMemoryCount   ── data-driven constraints
  ├─ tpu::CreateFishTopology (0x1fc57c60)           ── Jellyfish-class BarnaCore mesh
  ├─ jxc::DriverFactory::Create (0x0e778a40)        ── per-device jxc::DriverInterface
  ├─ TpuHalJxcCommonHelper (24 B, 0x0e725820)       ── stored at impl+0xC8
  └─ helper->CreateChips (0x0e726a40)
       └─ TpuChipJxcDriverImpl (432 B, 0x0e727f80)
            └─ core-factory lambda
                 └─ TpuCoreJxcDriverImpl (824 B, 0x0e733760)  ── takes a JfDmaIssuer*
```

### Considerations

JXC's construction path is the heaviest of the four families (~5 KB of code in `CreateAndInitializeChips`). It is the only family that calls a `jxc::DriverFactory` and a `CreateFishTopology` builder; later families fold driver and topology construction into a leaner `InitializeDrivers`. JXC enforces two hardcoded constraints during this path: the shared core-count message at string 0xa01f614, and an HBM cap of 2 (`"TPU platform only supports up to two HBMs."`, string 0xa02a74d) — the latter is a JXC-specific hardcoded "two", whereas PXC/VXC runtime-format the cap.

> **GOTCHA —** the per-core constructor takes a `JfDmaIssuer*` (the Jellyfish-family DMA engine, ctor @ 0x0e73aea0), a *separate* object created per core. This is unique to JXC: PXC, VXC, and GXC have no standalone DMA-issuer class — they fold DMA into the per-family driver (`TpuPxcDriver` / `TpuVxcDriver`). A reimplementation that assumes a DMA-issuer object for the newer families will find no such symbol.

---

## Driver Sub-Namespace Roster

The `asic_sw::driver::deepsea::jxc::` namespace is the strongest evidence for the no-split nature of JXC. Its direct sub-namespaces, confirmed in the symbol table, are organized by engine block and trace-entry type, **not** by fetch/load role:

| Sub-namespace | Confidence | Role |
|---|---|---|
| `jxc::dfc` | CERTAIN | dataflow controller engine (1991 symbols) |
| `jxc::jfc` | CERTAIN | Jellyfish core engine (988 symbols) |
| `jxc::registers` | CERTAIN | register-block definitions (330 symbols) |
| `jxc::snap` | CERTAIN | snapshot / checkpoint support (241 symbols) |
| `jxc::jellyfish_performance_counters` | CERTAIN | gen-0 perf counters |
| `jxc::dragonfish_performance_counters` | CERTAIN | gen-1 perf counters |
| `jxc::*_trace_entry` | CERTAIN | profiler trace-entry types (see below) |

The `*_trace_entry` family includes `bcs_internal`, `brn_fabric_sync`, `brn_sync_wait`, `cs_internal`, `cs_external_sync_flag_update`, `hbm_mux_switch`, `hib_request`, `hib_interrupt`, `hib_hbm_write`, `hib_sync_update`, `ici_packet`, and the `nf_*` set — engine-block event records, not standalone namespaces.

> **CORRECTION (JXC-NS) —** earlier roster notes listed `jxc::bcs`, `jxc::brn`, `jxc::hbm`, `jxc::hib`, `jxc::ici`, and `jxc::isa` as JXC sub-namespaces. The symbol table refutes this: there is no standalone `jxc::bcs/brn/hbm/hib/ici/isa`. Those tokens are *prefixes inside trace-entry type names* (e.g. `bcs_internal_trace_entry`, `ici_packet_trace_entry`). The compiler-side ISA for both generations lives in `xla::jellyfish::isa` (e.g. `jellyfish::isa::BundleSlot`, `MiscOpcode`), reflecting the deepsea umbrella where `jellyfish::` is the shared compiler-base namespace for all generations.

> **QUIRK —** JXC has **no** `jxc::profiler::TraceEntry` class. The named `profiler::TraceEntry` event class that the [sub-core taxonomy](sub-core-taxonomy.md) groups exists only for the fetch/load-split families. JXC's profiler support is realized through the per-engine `*_trace_entry` types instead. JXC is therefore *not* one of the trace-entry sub-cores despite being a HAL family.

---

## Per-Codename Differentiation

Jellyfish and Dragonfish differ only in data, never in C++ type. Both produce the same `TpuHalJxcHardwareImpl`, the same `TpuChipJxcDriverImpl`/`TpuCoreJxcDriverImpl`, and share one DMA descriptor model (the V1 `jxc::DmaDescriptor`, 8×32-bit = 32 bytes). Each generation has its own named codec class — `TpuCodecJellyfish` and `TpuCodecDragonfish` (both fully named, RTTI-symbol-bearing) — selected by `TpuCodec::Create` case 0/1, but these are codec objects, not HAL types.

| Axis | Jellyfish (v0) | Dragonfish (v1) | Source |
|---|---|---|---|
| TpuVersion enum | kJellyfish = 0 | kDragonfish = 1 | `TpuVersionToString` 0x20b3a480 |
| ToString | "jellyfish" | "dragonfish" | rodata |
| Codec class | `TpuCodecJellyfish` (named) | `TpuCodecDragonfish` (named) | symtab |
| TensorCore / BarnaCore | yes / yes | yes / yes | TpuChipParts |
| SparseCore | no | no | TpuChipParts |
| Flag prefix | `xla_jf_` (417 flags) | `xla_jf_` (no `xla_df_`) | flag scan |

---

## Cross-References

- [PXC Family](pxc-family.md) — the next generation; first to split fetch/load cores, drops the `JfDmaIssuer`
- [VXC Family](vxc-family.md) — first SparseCore-bearing family; one factory serves three codenames
- [GXC Family](gxc-family.md) — Ghostlite + 6acc60406, registered into the shared VXC factory
- [Sub-Core Taxonomy](sub-core-taxonomy.md) — where JXC's fused dataflow sits in the fetch/load-split evolution
- [HAL Families](hal-families.md) — the shared `TpuHalFactory` base chain and template-method `Create`
- [Codename Matrix](tpu-version-codename-matrix.md) — the 6-value `TpuVersion` enum and HAL routing
- [HAL Factory Override Matrix](hal-factory-override-matrix.md) — the per-impl 23-slot override tables
