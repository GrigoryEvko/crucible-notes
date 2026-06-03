# HAL Factory Override Matrix

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The TPU HAL factory framework dispatches in two stages, both keyed on `TpuVersion`. The first stage selects a **factory** object from the registry; the second stage runs that factory's `CreateImpl` to build the per-family **impl** object, which then carries its own virtual-method specialization. This page tabulates the override matrix at both layers: a five-slot factory vtable (uniform shape, two family-specific slots) and a twenty-three-slot impl vtable (mostly inherited, three to eight per-family overrides). Both matrices are small enough to show in full; the interesting axes are *factory class* (Jxc / Pxc / Vxc) and *vtable slot*.

The framework is a textbook abstract factory with a template-method twist. `TpuHalFactory` is the pure-virtual interface; `TpuHalHardwareFactoryBase` is the concrete intermediate that implements `Create` and `CanCreate` once and leaves `CreateImpl` as the single per-family hook. Because the base does all the orchestration, the leaf factories override only their destructor and `CreateImpl` — three of their five vtable slots point at inherited base code. The real per-generation logic lives one level down, in the impl object the factory allocates, and even there the override set is dominated by destructors, the two pure-virtual hooks every concrete subclass must fill (`Type`, `CreateAndInitializeChips`), and a small tail of teardown/configuration slots.

The dispatch dimension that matters for a reimplementer is therefore not "which method does family X override" in isolation, but the chain: `TpuVersion` → registry → factory instance → `CreateImpl` → impl class → impl vtable. The factory selection is data (a registry lookup); the impl specialization is C++ virtual dispatch. There is no third internal switch on `TpuVersion` inside any impl method — VXC serves three codenames through one identical code path.

For reimplementation, the contract is:

- The factory vtable shape (5 slots) and which two are family-specific.
- The `TpuHal` base vtable (23 slots), including the two `__cxa_pure_virtual` slots that force every concrete impl to override them.
- The per-family impl override matrix (factory × slot), with override addresses and override counts.
- The two-stage dispatch mechanism and the absence of any per-codename internal switch.

| | |
|---|---|
| **Factory vtable slots** | 5 (D2, D0, `Create`, `CanCreate`, `CreateImpl`) |
| **Family-specific factory slots** | 2 — slot 1 (`~Factory` D0), slot 4 (`CreateImpl`) |
| **Impl vtable slots** | 23 (`TpuHal` base; pure at slots 2 and 20) |
| **Override counts** | JXC = 7, PXC = 7, VXC = 8 |
| **Stage-1 dispatch** | registry lookup `TpuHalFactory::Get(v)` @ 0x1fbb19c0 |
| **Stage-2 dispatch** | C++ vtable on the allocated `TpuHal*HardwareImpl` |
| **Per-codename internal switch** | none (VXC = single class for v3/v4/v5) |

---

## Dispatch Mechanism

Dispatch is two stages, each keyed on `TpuVersion`, but realized by two different mechanisms.

### Stage 1 — Factory Selection (Data-Driven)

`TpuHalFactory::Get(version)` (0x1fbb19c0) reads the process-wide registry under a mutex and returns the factory instance registered for `(kHardware, version)`. The registry was populated at load time by the five init modules (see [HAL Families](hal-families.md)). This stage is *not* a `switch` and *not* a vtable cast — it is a table lookup. Six version keys map onto three factory classes:

| Axis | Values | Source |
|---|---|---|
| `TpuVersion` key | 0, 1, 2, 3, 4, 5 | `Register` immediate in each init module |
| Factory class | Jxc (0,1), Pxc (2), Vxc (3,4,5) | `make_unique<TpuHal*HardwareFactory>` in each `Register` |
| Factory vtable | 0x215fe530, 0x216085c8, 0x21cabf70 | vtable planted at object+0 in each init module |

### Stage 2 — Impl Specialization (Virtual Dispatch)

The selected factory's `Create` (inherited from `TpuHalHardwareFactoryBase` @ 0x1e80f560) calls the factory's own `CanCreate` (slot 3) to probe device availability, then — on success — calls the factory's own `CreateImpl` (slot 4). `CreateImpl` allocates the impl object, runs `TpuHal::TpuHal()` with the `TpuVersion` taken from the factory at +8 (the dword stamped at `Register` time), and plants the per-family impl vtable into the new object's slot 0. From that point on, all per-generation behavior is ordinary C++ virtual dispatch on the impl vtable.

> **GOTCHA —** in the decompile of `Create` (0x1e80f560) both indirect calls read their vtable from the **second** argument, which the decompiler types `TpuHostWorkQueue*`. The disassembly resolves the aliasing: the caller `TpuHal::Create` (0x1e814180) invokes `factory_vtable[2](ret, factory, wq)`, so inside `Create` that second argument is the **factory pointer**, not the work-queue, and the work-queue is the third argument. Both probe (`call *0x18(rax)` = slot 3) and build (`call *0x20(rax)` = slot 4) therefore dispatch on the factory's own vtable. A reimplementation that routes `CanCreate`/`CreateImpl` through a work-queue method will not match the binary.

```c
function HardwareFactoryBase::Create(this, factory, wq):  // 0x1e80f560
    if factory->vtable[3](factory):                       // CanCreate — slot 3 (0x1e80f520)
        return factory->vtable[4](this, factory, wq)      // CreateImpl — per-family slot 4
    else:
        return NotFound("No " + device_name + " device found.")   // tpu_hal_hardware_factory_base.cc:22

function JxcFactory::CreateImpl(ret, factory, wq):      // 0x0e723ac0
    v   = factory[2]                                     // TpuVersion at factory+8 (stamped at Register)
    obj = operator new(0xD0)                             // 208 B JxcImpl
    TpuHal::TpuHal(obj, v, wq)                           // base ctor — wq is the genuine work-queue arg
    obj[0]  = &JxcImpl_vtable[+0x10]                     // off_215FE590 — plant impl vtable
    obj[25] = 0                                          // helper @ +200 not yet attached
    ret[1] = obj; ret[0] = OK                            // write StatusOr<unique_ptr> result
    return ret
```

> **GOTCHA —** the `TpuVersion` the impl ctor receives is read from `factory+8` (`*((_DWORD*)factory + 2)` in the JXC/VXC stubs), *not* from the work-queue — the decompiler again mistypes the factory pointer as `TpuHostWorkQueue*`. `factory+8` is the dword each init module stamps at `Register` time (see [HAL Families](hal-families.md)). The work-queue is a separate argument and is forwarded only to the base ctor. PXC does not even read its factory pointer for the version: it hardcodes the literal `2`, because the Pxc factory services Pufferfish alone.

PXC and VXC `CreateImpl` are byte-for-byte the same shape; only the allocation size, the planted vtable, and (for PXC) a hardcoded `TpuVersion` literal differ. PXC allocates 0xD0 (208 B) and plants `off_21608628` with version `2`; VXC allocates **0xD8 (216 B)** and plants `off_21CABFD0`, additionally zeroing an extra flag byte at +208. (See [TpuHal Class Hierarchy](tpuhal-class-hierarchy.md) for the object layout.)

> **QUIRK —** there is no third dispatch. VXC serves Viperfish, Ghostlite, and 6acc60406 (versions 3/4/5) through one identical `TpuHalVxcHardwareImpl` and one identical `CreateImpl`. No VXC method contains a `switch (TpuVersion)`. Per-codename differentiation is pushed entirely out of the HAL into the `TpuChipParts` proto loader and the `TpuCodec` / `CycleTable` factories, all of which key on the same `TpuVersion`.

---

## Factory Vtable Override Matrix

All three leaf factories share one vtable shape: five function-pointer slots. Slots 0, 2, and 3 point at inherited base code (the complete-object destructor, `Create`, `CanCreate`); only slot 1 (the deleting destructor) and slot 4 (`CreateImpl`) carry family-specific code. The deleting destructors are trivially `free(this)` in every family — the 16-byte factory owns no members.

| Slot | Method | JXC | PXC | VXC | Confidence |
|---|---|---|---|---|---|
| 0 | `~Factory()` D2 (complete-obj dtor) | inherited 0x0e723a80 | inherited 0x0e723a80 | inherited 0x0e723a80 | HIGH |
| 1 | `~Factory()` D0 (deleting dtor) | **override 0x0e723aa0** | **override 0x0e7f8260** | **override 0x1d110e80** | CERTAIN |
| 2 | `Create(TpuHostWorkQueue*) const` | inherited 0x1e80f560 | inherited 0x1e80f560 | inherited 0x1e80f560 | CERTAIN |
| 3 | `CanCreate() const` | inherited 0x1e80f520 | inherited 0x1e80f520 | inherited 0x1e80f520 | CERTAIN |
| 4 | `CreateImpl(TpuHostWorkQueue*) const` | **override 0x0e723ac0** | **override 0x0e7f8280** | **override 0x1d110e00** | CERTAIN |

Each leaf factory's D0 destructor (slot 1) decompiles to `free(this)` — verified for all three at the addresses above. The override addresses for slot 4 are the `CreateImpl` stubs whose bodies are shown in the dispatch section.

---

## Impl Vtable Override Matrix

The `TpuHal` abstract base declares 23 virtual slots. Two are `__cxa_pure_virtual` and **must** be overridden in any instantiable subclass: slot 2 (`Type`) and slot 20 (`CreateAndInitializeChips`). The intermediate `TpuHalHardwareImpl` fills the two pure-base methods it can (`Type` at slot 2, a stricter `ValidateTopology` at slot 19); the three per-family impls inherit those and add their destructors, the mandatory `CreateAndInitializeChips`, and a small set of teardown/configuration slots.

The matrix below shows every slot. Cells reading "inherited" point at the `TpuHal` base implementation in the second column; bold cells are family overrides with their addresses.

| Slot | Method | Base (`TpuHal`) | JXC | PXC | VXC | Confidence |
|---|---|---|---|---|---|---|
| 0 | `~Impl()` D2 | base dtor | **0x0e724de0** | **0x0e7f8a40** | **0x1d111740** | CERTAIN |
| 1 | `~Impl()` D0 | base dtor | **0x0e724e40** | **0x0e7f8ac0** | **0x1d1117a0** | CERTAIN |
| 2 | `Type() const` | `__cxa_pure_virtual` | mid-base 0x1d3b5480 | mid-base 0x1d3b5480 | mid-base 0x1d3b5480 | CERTAIN |
| 3 | `Initialize(TpuHalOptions const&)` | 0x1e8132a0 | inherited | inherited | inherited | HIGH |
| 4 | `TearDown()` | 0x1e813440 | inherited | inherited | inherited | HIGH |
| 5 | `topology() const` | 0x1e8140a0 | inherited | inherited | inherited | HIGH |
| 6 | `host_location() const` | 0x1e814100 | inherited | inherited | inherited | HIGH |
| 7 | `hal_location() const` | 0x1e814160 | inherited | inherited | inherited | HIGH |
| 8 | `GetConfiguredProperties() const` | 0x0e724ea0 | inherited | **0x0e7f82e0** | **0x1d110ea0** | CERTAIN |
| 9 | `GetChip(int)` | 0x1e811e80 | inherited | inherited | inherited | HIGH |
| 10 | `GetChip(TpuChipLocation const&)` | 0x1e811e40 | inherited | inherited | inherited | HIGH |
| 11 | `AllocatePremapped(unsigned long)` | 0x1e8143a0 | inherited | inherited | inherited | HIGH |
| 12 | `DeallocatePremapped(void*)` | 0x1e8143c0 | inherited | inherited | inherited | HIGH |
| 13 | `PremappedAllocatorStats() const` | 0x1e8143e0 | inherited | inherited | inherited | HIGH |
| 14 | `GetPremappedAlignment() const` | 0x1e814420 | inherited | inherited | inherited | HIGH |
| 15 | `Throttle(TpuChipLocation const&)` | 0x1e814440 | inherited | inherited | inherited | HIGH |
| 16 | `Unthrottle(TpuChipLocation const&)` | 0x1e814460 | inherited | inherited | inherited | HIGH |
| 17 | `GetThrottleState(TpuChipLocation const&)` | 0x1e814480 | inherited | inherited | inherited | HIGH |
| 18 | `WaitForCoreDumpComplete()` | 0x213d7760 | inherited | inherited | **0x1d110f00** | CERTAIN |
| 19 | `ValidateTopology()` | 0x1e8139c0 | mid-base 0x1d3b54a0 | mid-base 0x1d3b54a0 | mid-base 0x1d3b54a0 | CERTAIN |
| 20 | `CreateAndInitializeChips(TpuHalOptions const&)` | `__cxa_pure_virtual` | **0x0e723c20** | **0x0e7f8300** | **0x1d110f20** | CERTAIN |
| 21 | `PreTearDownChips()` | 0x1d3b5a20 (no-op) | **0x0e724da0** | **0x0e7f8a20** | **0x1d111720** | CERTAIN |
| 22 | `PostTearDownChips()` | 0x0e7f8b40 (no-op) | **0x0e724dc0** | inherited | inherited | CERTAIN |

**Override counts:** JXC = 7 (slots 0,1,2,19,20,21,22), PXC = 7 (slots 0,1,2,8,19,20,21), VXC = 8 (slots 0,1,2,8,18,19,20,21).

Slots 2 and 19 are shown as "mid-base" because all three families share the *same* implementation, inherited from the intermediate `TpuHalHardwareImpl` rather than from a per-family body: `TpuHalHardwareImpl::Type` (0x1d3b5480) and `TpuHalHardwareImpl::ValidateTopology` (0x1d3b54a0). `Type` returns the constant `0` (the `kHardware` product-type tag); `ValidateTopology` is the stricter version that scans hardware devices, compares the detected `TpuVersion` and `TpuChipParts::variant_name` against the topology, and emits a "Detected hardware version ... does not match" diagnostic. These two are the entire raison d'être of the intermediate class.

### Where the Three Families Actually Differ

Reading the matrix by override delta isolates each family's specialization:

- **All three** override destructors (slots 0,1), the mandatory `CreateAndInitializeChips` (slot 20), and `PreTearDownChips` (slot 21 → `{Jxc,Pxc,Vxc}CommonHelper::TearDownMesh`). These are the per-family core: the chip-creation constraint checks and the mesh-teardown path.
- **PXC and VXC** override `GetConfiguredProperties` (slot 8) — both delegate to their family `CommonHelper`. JXC inherits the base default (`GetDefaultConfiguredProperties(topology)`).
- **VXC alone** overrides `WaitForCoreDumpComplete` (slot 18 → `TpuHalVxcCommonHelper::WaitForCoreDumpComplete`). JXC and PXC inherit the generic per-chip wait loop. This matches VXC's broader fabric-attached core-dump machinery (visible also in its `TpuChip*` override set).
- **JXC alone** overrides `PostTearDownChips` (slot 22), even though its body returns `1` identically to the base no-op — the source file places the override explicitly.

> **NOTE —** the override bodies for slots 20/21 carry the family's hardcoded core-count and HBM constraints and its driver wiring. `JxcImpl::CreateAndInitializeChips` (0x0e723c20) caps the core counts with three run-time-assembled diagnostics — the TensorCore limit (prefix `"Jellyfish Hardware only supports at most "` + count + `" TensorCore."`), the BarnaCore limit (same prefix + count + `" Barnacore."`), and the HBM limit (the standalone literal `"TPU platform only supports up to two HBMs."`) — then drives the `jxc` deepsea `DriverFactory`. `PxcImpl` and `VxcImpl` carry the analogous Pufferfish / Viperfish messages and their `CommonHelper::CreateChips` paths. The constraint detail belongs to the per-family pages; the slot ownership is what this matrix fixes.

`VxcImpl::PreTearDownChips` (0x1d111720) is also where the 216-byte VXC object's extra +208 flag byte is read: if set it returns `1` (mesh already torn down), otherwise it calls `TpuHalVxcCommonHelper::TearDownMesh(this[25])`. JXC and PXC have no such guard byte.

---

## Cross-References

- [HAL Families](hal-families.md) — the registry that drives stage-1 factory selection and the five init modules that populate it
- [TpuHal Class Hierarchy](tpuhal-class-hierarchy.md) — the vtable layouts, slot counts, and object sizes this matrix indexes into
- [6-Codename Authoritative Reconciliation](tpu-version-codename-matrix.md) — the `TpuVersion` keys both dispatch stages switch on
- [JXC Family](jxc-family.md) — the slot-20/21/22 override bodies for Jellyfish and Dragonfish
- [PXC Family](pxc-family.md) — the slot-8/20/21 override bodies for Pufferfish
- [VXC Family](vxc-family.md) — the slot-8/18/20/21 override bodies for Viperfish
- [GXC Family](gxc-family.md) — Ghostlite and 6acc60406, dispatched onto the same VXC impl vtable
