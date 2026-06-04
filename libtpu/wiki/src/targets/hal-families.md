# HAL Families

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The TPU Hardware Abstraction Layer (HAL) is built as a classic C++ abstract-factory framework. A pure-virtual `tpu::TpuHalFactory` interface is the registration key; a single concrete base `tpu::TpuHalHardwareFactoryBase` supplies the template-method `Create`/`CanCreate`; and three leaf factory classes each plug in one `CreateImpl` allocator. At library load, a set of `google_init_module_*` static initializers register one factory instance per `TpuVersion` into a process-wide registry keyed by `(TpuPlatformType, TpuVersion)`. At runtime `TpuHalFactory::Get(version)` looks the key up and the matching factory builds the per-family `TpuHal*HardwareImpl` object that abstracts one TPU generation's silicon.

The counter-intuitive fact this page exists to settle is that there are **three** factory classes but **five** init modules and **six** registered `TpuVersion` keys. The factory layer is deliberately thin: per-codename behavior does not live in distinct C++ subclasses but in a data-driven `TpuChipParts` proto loaded per version, and in the parallel codec/cost-model hierarchies that key on the same `TpuVersion`. The three families partition the six codenames as JXC (Jellyfish v0, Dragonfish v1), PXC (Pufferfish v2), and VXC (Viperfish v3, Ghostlite v4, 6acc60406 v5). The VXC class is registered three times — once each by the `vxc`, `glc`, and `gfc` init modules — under three different version keys, which is why the module count exceeds the class count.

The mapping is observable directly. Each init module's `TpuHalFactory::Register` call carries a `make_unique<tpu::TpuHal{Jxc,Pxc,Vxc}HardwareFactory>` source-string and a numeric `TpuVersion` argument, both of which survive in the binary as the `CHECK(... is OK)` failure message and the immediate operand. Reading those across the five modules reconstructs the registration table exactly.

For reimplementation, the contract is:

- The registry key space: `(TpuPlatformType, TpuVersion)` with the platform fixed at `kHardware` (value 0) and version running 0..5.
- The five init modules, their addresses, and which factory class and `TpuVersion` each `Register` call binds.
- Why a 3-class / 5-module / 6-key fan-out is correct, not a labeling error: the same `TpuHalVxcHardwareFactory` class is instantiated three times under different keys.

| | |
|---|---|
| **Factory interface** | `tpu::TpuHalFactory` (`_ZTI` @ 0x21d34410; no standalone vtable) |
| **Factory base** | `tpu::TpuHalHardwareFactoryBase` — vtable slots: 2 `Create` (0x1e80f560), 3 `CanCreate` (0x1e80f520), 4 `CreateImpl` (pure, per-family) |
| **Leaf factory classes** | 3 — Jxc (anon ns), Pxc (anon ns), Vxc (global ns) |
| **Factory object** | 16 B (`operator new(0x10)`): vtable ptr at +0, `TpuVersion` dword at +8 |
| **Init modules** | 5 — jxc, pxc, vxc, glc, gfc |
| **Registered TpuVersion keys** | 6 — 0..5 |
| **Registry** | `g_hal_factories_by_type` (platform-type-indexed), guarded by `g_hal_factories_mutex` |
| **Registry lookup** | `TpuHalFactory::Get(version, opt<platform>)` @ 0x1fbb19c0 (under mutex) |
| **Construction entry** | `TpuHal::Create(opt<platform>, version, profiler, wq)` @ 0x1e814180 |

---

## Why the Count Is 3, Not 2 (and Not 5)

A naive reading suggests two families — old PCIe-attached TPUs versus new fabric-attached ones. The binary disagrees on both ends. There are three distinct factory **classes**, distinguished by their vtables and C++ linkage:

```text
tpu::{anonymous}::TpuHalJxcHardwareFactory   _ZTV 0x215fe530   (anon ns)
tpu::{anonymous}::TpuHalPxcHardwareFactory   _ZTV 0x216085c8   (anon ns)
tpu::TpuHalVxcHardwareFactory                _ZTV 0x21cabf70   (global ns)
```

JXC and PXC sit in an anonymous namespace (mangled `_ZN3tpu12_GLOBAL__N_1...`); VXC is a global-namespace class (`_ZN3tpu24TpuHalVxcHardwareFactory...`). That is the only C++-visible structural difference at the factory layer — behaviorally all three are identical template-method factories.

> **QUIRK —** the family count is three because the *class* count is three, not because the *codename* count (six) or the *init-module* count (five) line up with it. GXC has no factory class of its own: the `gfc` and `glc` init modules construct `TpuHalVxcHardwareFactory` instances. "GXC family" in the codename taxonomy is a registry-level grouping of versions 4 and 5 onto the VXC class, not a fourth C++ factory. A reimplementation that creates a `GxcHardwareFactory` class will diverge from the binary, which never declares one.

The split is not arbitrary. JXC handles the two oldest generations (Jellyfish/Dragonfish), which share a driver path and BarnaCore-mesh topology. PXC is Pufferfish-only. VXC covers every fabric-attached generation from Viperfish forward and overrides the most chip-management slots (throttle, core-dump, host-sync-flag) because that silicon needs its own machinery. The three classes therefore correspond to three driver eras, with the VXC class absorbing all post-Pufferfish codenames rather than forking per generation.

---

## The Five Init Modules

Each module is an internal-linkage (`_ZL44...`) static initializer that allocates a 16-byte factory object (`operator new(0x10)`), plants the factory vtable into slot 0, writes the embedded `TpuVersion` into the object at +8, and calls `TpuHalFactory::Register`. The JXC module does this **twice** (versions 0 and 1); the other four do it once. Every `Register` result is `CHECK`-ed against `OK` (value 1), and the failure message is the `make_unique<...>` call expression — which is how the factory class and intended version are recoverable as plaintext.

### Registration Table

The platform argument is `TpuPlatformType::kHardware` (0) in every call. The version is the second `Register` argument (an immediate). The codenames below are the exact `tpu::TpuVersion::k*` enumerators the source strings spell; the parenthetical external name in the last column is the canonical `TpuVersionToExternalName` (`0x20b3a500`) output, reproduced here only as a reader aid and verified against the [codename matrix](tpu-version-codename-matrix.md). The non-mnemonic codenames (`6acc60406`) and the external names that are *not* in the binary (`Trillium`, `Ironwood`) are kept off this page deliberately — every token here is a literal in `libtpu.so`.

| Init module | Addr | Factory class | TpuVersion | Enum name | Codename (external) |
|---|---|---|---|---|---|
| `google_init_module_tpu_hal_jxc_hardware_impl` (1st `Register`) | 0x213e9d80 | `TpuHalJxcHardwareFactory` | 0 | `kJellyfish` | jellyfish (`TPU v2`) |
| `google_init_module_tpu_hal_jxc_hardware_impl` (2nd `Register`) | 0x213e9d80 | `TpuHalJxcHardwareFactory` | 1 | `kDragonfish` | dragonfish (`TPU v3`) |
| `google_init_module_tpu_hal_pxc_hardware_impl` | 0x213e9ec0 | `TpuHalPxcHardwareFactory` | 2 | `kPufferfish` | pufferfish (`TPU v4`) |
| `google_init_module_tpu_hal_vxc_hardware_impl` | 0x213eed20 | `TpuHalVxcHardwareFactory` | 3 | `kViperfish` | viperfish (`TPU v5`) |
| `google_init_module_tpu_hal_glc_hardware_impl` | 0x213eb9e0 | `TpuHalVxcHardwareFactory` | 4 | `kGhostlite` | ghostlite (`TPU v6 lite`) |
| `google_init_module_tpu_hal_gfc_hardware_impl` | 0x213e9f60 | `TpuHalVxcHardwareFactory` | 5 | `k6acc60406` | 6acc60406 (`TPU7x`) |

Every row was read directly from the decompiled init module: the immediate `Register(0, N, ...)` operand and the `tpu::TpuVersion::k*` token inside the `make_unique<...>` `CHECK` string.

> **QUIRK —** the PXC `make_unique` is the lone version-less constructor. The JXC, VXC, GLC, and GFC source strings spell `make_unique<...HardwareFactory>(tpu::TpuVersion::k<Codename>)`, but PXC's is bare — `std::make_unique<tpu::TpuHalPxcHardwareFactory>()` — because Pufferfish is the only generation that factory services, so the version need not be passed to the constructor. The version dword is still stamped into the factory object at +8 (`*(int*)(obj+8) = 2`) and still supplied as the `Register` key; only the *constructor argument* is omitted. The matrix and overview both record this asymmetry; it is the one binary-visible difference in the otherwise-uniform five registrations.

> **NOTE —** the JXC init module's source strings name version 0 `tpu::TpuVersion::kJellyfish` and version 1 `tpu::TpuVersion::kDragonfish`: the JXC family is Jellyfish (0) + Dragonfish (1), with no separate pre-Jellyfish key. The v5 enum is literally `k6acc60406` — the "Ghostfish" label never appears in the binary.

### JXC: The Double Registration

`google_init_module_tpu_hal_jxc_hardware_impl` is the only module that registers twice. Its body, lightly cleaned from the decompile at 0x213e9d80:

```c
function google_init_module_tpu_hal_jxc_hardware_impl():   // 0x213e9d80
    f0 = operator new(0x10)                 // 16-byte factory
    f0[1]      = 0                           // TpuVersion = 0 (kJellyfish) at +8
    *(void**)f0 = &JxcFactory_vtable[+0x10]  // off_215FE540
    st = TpuHalFactory::Register(0, 0, f0)   // platform=kHardware, version=kJellyfish
    CHECK(st == OK)                          // "...make_unique<TpuHalJxcHardwareFactory>(kJellyfish)) is OK"

    f1 = operator new(0x10)
    f1[1]      = 1                           // TpuVersion = 1 (kDragonfish) at +8
    *(void**)f1 = &JxcFactory_vtable[+0x10]  // off_215FE540 (same vtable)
    st = TpuHalFactory::Register(0, 1, f1)   // version=kDragonfish
    CHECK(st == OK)                          // "...make_unique<TpuHalJxcHardwareFactory>(kDragonfish)) is OK"
```

Both registrations use the same factory vtable (`off_215FE540`). The only thing distinguishing the two registered instances is the `TpuVersion` byte at +8 and the key under which the registry stores them. The same code shape recurs in the other four modules with one `Register` each.

### VXC: Three Modules, One Class

`vxc`, `glc`, and `gfc` are three separate init modules that each construct a `TpuHalVxcHardwareFactory` (`make_unique<tpu::TpuHalVxcHardwareFactory>`) and register it under a different version:

```text
0x213eed20  vxc  →  Register(0, 3, make_unique<TpuHalVxcHardwareFactory>)  // kViperfish
0x213eb9e0  glc  →  Register(0, 4, make_unique<TpuHalVxcHardwareFactory>)  // kGhostlite
0x213e9f60  gfc  →  Register(0, 5, make_unique<TpuHalVxcHardwareFactory>)  // k6acc60406
```

All three reference the same factory vtable at 0x21cabf70 and produce the same `TpuHalVxcHardwareImpl` object (`CreateImpl` @ 0x1d110e00). The codename a particular instance serves is determined solely by the registry key, never by class identity.

> **NOTE —** the per-codename hardware constants for Viperfish, Ghostlite, and 6acc60406 are not encoded in three subclasses. They come from the `TpuChipParts` proto each version loads (`embed://tpu_chip_parts/<version>_chip_parts.binarypb`) and from the parallel `TpuCodec` / `CycleTable` hierarchies that switch on `TpuVersion`. The HAL factory deliberately does no per-codename branching. See [chip_parts.binarypb Decode](chip-parts-binarypb.md) and [Per-Codename Constant Table](per-codename-hw-constants.md).

---

## Registry and Construction Flow

The registry is a `(TpuPlatformType, TpuVersion)`-keyed table populated at static-init time and read at runtime. The end-to-end path from a caller wanting a HAL to the object existing:

```text
caller wants HAL for TpuVersion v
   |
   v
TpuHal::Create(opt<platform>, v, profiler, wq)          0x1e814180
   |
   v
TpuHalFactory::Get(v, opt<platform>)                    0x1fbb19c0   (registry lookup under mutex)
   |
   v  &factory  (one of Jxc / Pxc / Vxc factory instance)
   |
factory->Create(wq) via factory-vtable slot 2           0x1e80f560 = HardwareFactoryBase::Create
   |- this->CanCreate()                                  factory-vtable slot 3 (0x1e80f520) — hardware probe
   |    └─ ScanHardwareDevices(); ok iff scanned version == factory's registered TpuVersion (factory+8)
   |- if !CanCreate: build NotFound "No <device> device found."
   |- if  CanCreate: this->CreateImpl(wq)                factory-vtable slot 4 (per-family)
   v
new TpuHal{Jxc,Pxc,Vxc}HardwareImpl                      (208 B Jxc/Pxc, 216 B Vxc)
```

`HardwareFactoryBase::Create` (0x1e80f560) is the shared template method, reached through factory-vtable slot 2. It dispatches twice on the **factory's own** vtable — not the work-queue's: slot 3 is the `CanCreate` probe and, on success, slot 4 is the per-family `CreateImpl`.

> **GOTCHA —** the decompiler types `Create`'s second register argument as `TpuHostWorkQueue*`, which makes the two indirect calls look like work-queue vtable dispatches. They are not. The caller (`TpuHal::Create`, 0x1e814180) invokes `factory_vtable[2](ret_slot, factory, wq)`; inside `Create` that `factory` pointer is what slots 3 and 4 dispatch through, with the work-queue passed on as the trailing argument. A reimplementation that routes availability through a work-queue method instead of the factory's own `CanCreate` will not match the binary.

`CanCreate` (0x1e80f520) is the inherited concrete predicate the base advertises: it calls `tpu::ScanHardwareDevices`, then returns true only when the scanned hardware version equals the factory's registered `TpuVersion` (the dword at factory+8 stamped by the init module) and at least one device was found. On a failed probe `Create` constructs a `NotFound` status carrying the message `"No <device> device found."` built via `util::NotFoundErrorBuilder` from `tpu_hal_hardware_factory_base.cc`.

The per-family `CreateImpl` is a small allocator stub — its layout is documented on the [TpuHal Class Hierarchy](tpuhal-class-hierarchy.md) page. The factory-vtable slot map and the impl-vtable override matrix live on the [HAL Factory Override Matrix](hal-factory-override-matrix.md) page.

---

## Cross-References

- [Part IV Overview](overview.md) — the Silicon & Codename hub; where HAL routing sits in the `TpuVersion` dispatch pipeline
- [6-Codename Authoritative Reconciliation](tpu-version-codename-matrix.md) — the canonical `TpuVersion` → codename → external-name cross-walk this page's registration keys index into
- [HAL Factory Override Matrix](hal-factory-override-matrix.md) — per-factory virtual-method override matrix and the dispatch mechanism
- [TpuHal Class Hierarchy](tpuhal-class-hierarchy.md) — the `TpuHal` → `TpuHalHardwareImpl` → per-family object tree the factories construct
- [JXC Family](jxc-family.md) — Jellyfish (v0) + Dragonfish (v1) silicon detail
- [PXC Family](pxc-family.md) — Pufferfish (v2) silicon detail
- [VXC Family](vxc-family.md) — Viperfish (v3) silicon detail
- [GXC Family](gxc-family.md) — Ghostlite (v4) + 6acc60406 (v5), registered onto the VXC factory class
- [chip_parts.binarypb Decode](chip-parts-binarypb.md) — the data-driven per-codename constant source the factory layer defers to
