# PXC Family (Pufferfish)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

PXC is the single-codename family: `tpu::(anonymous namespace)::TpuHalPxcHardwareFactory` serves exactly one silicon generation, Pufferfish (`TpuVersion::kPufferfish` = 2). Because it serves one version, it is the simplest of the four families to reason about — one factory instance, one `Register` call, and a `CreateImpl` that hardcodes the version literal `2` rather than reading it at runtime.

PXC's place in the [sub-core taxonomy](sub-core-taxonomy.md) is pivotal: it is the **first family to split each core's instruction stream into a fetch-core and a load-core**. Where [JXC](jxc-family.md) had a single fused dataflow, PXC introduces `asic_sw::driver::deepsea::pxc::pfc` (the Pufferfish fetch-core) and `pxc::plc` (the Pufferfish load-core) as distinct sub-namespaces. This split persists through VXC and GXC, making PXC the architectural template for every later family. PXC also carries a family-level `pxc::isa` namespace and a `pxc::profiler` namespace — the first family with a HAL-driver ISA namespace under its own family tag.

This page follows the same grammar as the [JXC](jxc-family.md), [VXC](vxc-family.md), and [GXC](gxc-family.md) pages. The factory's vtable shape is byte-identical to the others; what differs is the version-handling, the DMA model, and the heavier helper. For the shared base chain, see [HAL Families](hal-families.md).

For reimplementation, the contract is:

- The single-version registration: one factory instance, one `Register` call, `CreateImpl` hardcoding `version = 2`.
- The 5-slot factory vtable (identical shape to JXC/VXC) and the 208-byte `TpuHalPxcHardwareImpl`.
- The construction chain HardwareImpl → CommonHelper (48 B) → Chip (408 B) → Core (800 B), with DMA folded into `tpu::TpuPxcDriver` — **no** separate DMA-issuer object.
- The `pxc::pfc` / `pxc::plc` fetch/load split and the `pxc::isa` / `pxc::profiler` namespaces.

| | |
|---|---|
| **Factory class** | `tpu::(anonymous namespace)::TpuHalPxcHardwareFactory` (anon-ns) |
| **TpuVersions served** | 1 — kPufferfish (2) |
| **Factory vtable / vptr** | `_ZTV` 0x216085c8 / installed vptr 0x216085d8 |
| **Factory typeinfo** | `_ZTI` 0x21608600 (`__si` base → `TpuHalHardwareFactoryBase` 0x21d343f8) |
| **HAL impl class / size** | `TpuHalPxcHardwareImpl`, 208 B (0xD0), vtable 0x21608618 |
| **Init module** | `google_init_module_tpu_hal_pxc_hardware_impl` @ 0x213e9ec0 (1× Register) |
| **Fetch/load split** | **First generation with the split** — `pxc::pfc` + `pxc::plc` |
| **DMA engine** | integrated into `tpu::TpuPxcDriver` (no issuer object); DmaDescriptorV2 |

---

## Factory Binding and Registration

### Purpose

The PXC factory binds `TpuVersion::kPufferfish` to its HAL implementation. Because Pufferfish is the only codename PXC handles, registration is a single call — the most economical of the four families.

### Entry Point

```text
google_init_module_tpu_hal_pxc_hardware_impl (0x213e9ec0)
  └─ operator new(0x10)                     ── single factory instance (16 B)
       f[+8] = 2 (kPufferfish) ; f[+0] = vptr 0x216085d8
       TpuHalFactory::Register(kHardware, 2, f)   (0x1fbb16a0)
       CHECK(s == OK)   // string 0x94acd7e
```

### Algorithm

```c
function google_init_module_tpu_hal_pxc_hardware_impl():   // 0x213e9ec0
    f = operator_new(0x10)                  // 16-byte factory object
    f[+8] = 2                               // kPufferfish (movl $0x2,0x8(%rax))
    f[+0] = &PxcFactory_vtable + 0x10        // installed vptr 0x216085d8
    s = TpuHalFactory::Register(kHardware /*0*/, 2, unique_ptr(f))
    if s != OK: LogMessageFatal(CHECK string @0x94acd7e)
```

> **QUIRK —** the CHECK string at 0x94acd7e reads `std::make_unique<tpu::TpuHalPxcHardwareFactory>()` with **no argument** — the PXC factory constructor takes no `TpuVersion`, because there is nothing to disambiguate. Contrast JXC and VXC, whose CHECK strings pass the codename to `make_unique`. The source path baked into the binary is `learning/45eac/tpu/runtime/hal/internal/pxc/tpu_hal_pxc_hardware_impl_registration.cc` (string 0x877b845).

### Function Map

| Function | Address | Role |
|---|---|---|
| `google_init_module_tpu_hal_pxc_hardware_impl` | 0x213e9ec0 | single Register (v2) |
| `TpuHalFactory::Register` | 0x1fbb16a0 | registry insert (shared) |
| `TpuHalFactory::Get` | 0x1fbb19c0 | runtime lookup (shared) |
| `TpuHal::Create` | 0x1e814180 | public entry (shared) |

---

## The Factory vtable

### Purpose

PXC's factory vtable is byte-identical in shape to JXC's: two family-specific overrides (D0 dtor, `CreateImpl`) and three inherited slots whose addresses are *literally the same functions* JXC and VXC point at.

### Vtable Layout

| vaddr | slot | resolves to | base/override |
|---|---|---|---|
| 0x216085d8 | 0 — `~TpuHalFactory()` D2 | 0x0e723a80 (`ret`) | INHERITED |
| 0x216085e0 | 1 — `~TpuHalPxcHardwareFactory()` D0 | 0x0e7f8260 | **OVERRIDE** |
| 0x216085e8 | 2 — `HardwareFactoryBase::Create(wq)` | 0x1e80f560 | INHERITED |
| 0x216085f0 | 3 — `HardwareFactoryBase::CanCreate()` | 0x1e80f520 | INHERITED |
| 0x216085f8 | 4 — `TpuHalPxcHardwareFactory::CreateImpl(wq)` | 0x0e7f8280 | **OVERRIDE** |

Slot 1 encodes `operator delete(this, 0x10)` — the factory is 16 bytes, like all four families.

### Algorithm — CreateImpl

```c
function TpuHalPxcHardwareFactory::CreateImpl(out, this, wq):   // 0x0e7f8280
    version = 2                              // HARDCODED kPufferfish (mov $0x2,%esi)
    obj     = operator_new(0xD0)              // 208 B = TpuHalPxcHardwareImpl
    TpuHal::TpuHal(obj, 2, wq)                // base ctor 0x1e811c00: wq→+0x68, version→+0x78
    *(void**)(obj + 0)    = &PxcImpl_vtable + 0x10   // plant 0x21608618 → 0x21608628
    *(void**)(obj + 0xC8) = nullptr            // CommonHelper slot, null until init
    out.value = obj ; out.status = OK
    return out
```

> **NOTE —** PXC's `CreateImpl` is the only one that hardcodes its version (`mov $0x2`). It reads neither the factory's `+8` nor the work-queue. The result is functionally identical to JXC reading `factory+8` (the registered key is the same `2`), but the source operand differs: PXC bakes in the literal.

### Considerations

The `TpuHalPxcHardwareImpl` overrides one slot JXC does not: slot 8, `GetConfiguredProperties` (@ 0x0e7f82e0), which tail-calls into the helper at `this+0xC8`. The PXC impl override count is 7 (slots 0, 1, 2, 8, 19, 20, 21). See the [HAL Factory Override Matrix](hal-factory-override-matrix.md) for the full 23-slot impl table.

---

## Construction Chain Below the Factory

### Purpose

PXC's `CreateAndInitializeChips` (impl vtable slot 20, @ 0x0e7f8300) is markedly leaner than JXC's (~1.8 KB vs ~5 KB). It calls no `DriverFactory` and no topology builder — driver and topology construction are folded into a no-argument `InitializeDrivers`.

### Entry Point

```text
TpuHalPxcHardwareImpl::CreateAndInitializeChips (0x0e7f8300)
  ├─ TpuChipParts::CoreCount / SharedMemoryCount   ── data-driven constraints
  ├─ TpuHalPxcCommonHelper (48 B, 0x0e7f8b60)        ── stored at impl+0xC8
  ├─ helper->InitializeDrivers() (0x0e7f8b80)        ── NO ARGS; builds TpuPxcDriver vector
  └─ helper->CreateChips (0x0e7f9a00)
       └─ TpuChipPxcDriverImpl (408 B, 0x0e7fa5a0)    ── single int + unique_ptr<TpuPxcDriver>
            └─ core-factory lambda (0x0e7fd3c0)
                 └─ TpuCorePxcDriverImpl (800 B, 0x0e7ffd40)  ── 6-arg ctor, NO DMA-issuer
```

### Considerations

The PXC `CommonHelper` is 48 bytes (vs JXC's 24) because it owns a `std::vector<unique_ptr<tpu::TpuPxcDriver>>` (proven by the D0 dtor freeing a vector at helper+0x18/+0x28 before `free(helper, 0x30)`). The chip ctor takes a single `int` (JXC takes two) and one `unique_ptr<TpuPxcDriver>` (JXC takes a driver-interface plus a register-interface). The core ctor takes **6 arguments and no DMA-issuer** — contrast JXC's 8-argument core ctor with its `JfDmaIssuer*`.

> **GOTCHA —** PXC has **no** `PfDmaIssuer` symbol anywhere in the binary. DMA for Pufferfish is built directly into `tpu::TpuPxcDriver` (`ReadFromMemoryHelper` 0x0e80f220, `WriteToMemoryHelper` 0x0e80bd80, `OnChipTransfer` 0x0e8139a0, `InjectFishDescriptors` 0x0e80a3a0). The on-chip queue model is a `std::variant` of `asic_sw::deepsea::pxc::{DirectWrite,Debug,Infeed,Magic,Outfeed}QueueDescriptor`, and the hardware descriptor is the V2 `DmaDescriptorV2` (≥96 bytes, 4-level strided) — the first V2 user. The compiler-side builder is `xla::pufferfish::PufferfishDmaDescriptorState`, whose `CreateForViperfish` method shows the V2 state is shared forward into Viperfish.

---

## Driver Sub-Namespace Roster

`asic_sw::driver::deepsea::pxc::` is the first family namespace to carry the fetch/load split. Its direct sub-namespaces, confirmed in the symbol table:

| Sub-namespace | Role |
|---|---|
| `pxc::pfc` | **Pufferfish fetch-core** — fetch-side instruction stream |
| `pxc::plc` | **Pufferfish load-core** — load-side instruction stream |
| `pxc::isa` | family-level ISA (137K symbols) |
| `pxc::profiler` | family-level profiler (8125 symbols), holds the `TraceEntry` class |
| `pxc::internal` | internal driver helpers |

Below the fetch-core sit further namespaces: `pxc::pfc::isa` (46K symbols — includes `pxc::pfc::isa::BarnaCoreChannelBundle`, `VectorBase`), `pxc::pfc::profiler`, and `pxc::pfc::b0` (a register-block namespace). The load-core carries `pxc::plc::profiler`. BarnaCore bundle types living under the fetch-core's ISA confirm Pufferfish is the last family to ship BarnaCore — it is retired in VXC.

> **QUIRK —** the named `profiler::TraceEntry` event class lives at the **family** level (`pxc::profiler::TraceEntry`, 3087 token occurrences), **not** under `pfc` or `plc`. The `pxc::pfc::profiler` / `pxc::plc::profiler` sub-namespaces instead hold control-interface and limits-factory classes (`TracemarkLimitsFactory`, `EveryoneTraceControlFactory`). This is unlike VXC/GXC, whose `TraceEntry` classes are per-sub-core. PXC is therefore one of the [sub-core taxonomy](sub-core-taxonomy.md)'s trace-entry families, but with the trace entry at family granularity.

---

## Per-Codename Differentiation

Pufferfish has chip variants (B0 Mfg / B0 Water / B0 Air) multiplexed inside the same impl via `TpuChipParts::variant_name()` (0x20b1eb40). They are not separate `TpuVersion` values and do not change the factory or vtable — the variant name is read only for census reporting and human-readable naming (`TpuVersionAndVariantToHumanReadableName`, 0x20b3b040), with no per-variant code branch observed in `CreateAndInitializeChips`.

| Axis | Pufferfish (v2) | Source |
|---|---|---|
| TpuVersion enum | kPufferfish = 2 | `TpuVersionToString` 0x20b3a480 |
| ToString | "pufferfish" | rodata |
| External name | "TPU v4" (lite variant: "TPU v4 lite") | naming path |
| Codec class | `TpuCodecPufferfish` (named) | `TpuCodec::Create` 0x1e835fa0 case 2 |
| TensorCore / BarnaCore | yes / yes (last BarnaCore gen) | TpuChipParts |
| SparseCore | no | TpuChipParts |
| Flag prefix | `xla_pf_` (only 3 flags; mostly shares `xla_jf_`) | flag scan |

> **NOTE —** the tiny `xla_pf_` count (3 flags) shows Pufferfish is still close to the Jellyfish flag base; it shares most of `xla_jf_`. The architectural break (fetch/load split, DMA-in-driver, V2 descriptor) is structural, not flag-surfaced.

---

## Cross-References

- [JXC Family](jxc-family.md) — the predecessor with fused dataflow and a separate `JfDmaIssuer`
- [VXC Family](vxc-family.md) — inherits the fetch/load split; first SparseCore family; shares the V2 descriptor
- [GXC Family](gxc-family.md) — Ghostlite + 6acc60406, registered into the shared VXC factory
- [Sub-Core Taxonomy](sub-core-taxonomy.md) — PXC as the origin of the fetch/load-core split
- [HAL Families](hal-families.md) — the shared `TpuHalFactory` base chain and template-method `Create`
- [Codename Matrix](tpu-version-codename-matrix.md) — the 6-value `TpuVersion` enum and HAL routing
- [HAL Factory Override Matrix](hal-factory-override-matrix.md) — the per-impl 23-slot override tables
