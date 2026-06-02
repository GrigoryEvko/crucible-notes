# TpuHal Class Hierarchy

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

The TPU runtime exposes four conceptual "products" of a chip generation — a hardware abstraction, a per-core execution context, a cost model, and an ISA encoder. A reimplementer expecting four sibling `TpuHal*` classes under one factory will not find them. The symbol table is unambiguous: only **`TpuHalHardware*`** is actually a `tpu::TpuHal*`-named class. `TpuHalCore`, `TpuHalCostModel`, and `TpuHalEncoder` do not appear anywhere in the binary's symbol table — they are the *roles* filled by three independent class trees (`tpu::TpuCore`, `xla::jellyfish::CycleTable`, `tpu::TpuCodec`/`isa::Encoder`) that the HAL owns, pairs with, or feeds, but that do not share the `TpuHalFactory` base and are not produced by the HAL factory.

This page maps all four hierarchies with their real class names, vtable addresses, slot counts, and the single-inheritance edges between them. The four trees share exactly one axis: `tpu::TpuVersion`. The HAL factory dispatches on it via the registry; the chip/core impls are chosen by the family impl the HAL allocates; `TpuCodec::Create` switches on it; and `CycleTable::Create` looks it up in a `FunctionRegistry`. Knowing that the four "products" are coupled only through `TpuVersion` — and that only one of them is a `TpuHal` class — is the prerequisite for understanding why the override matrices differ so much from tree to tree.

The Hardware tree is the shallow one: `TpuHal` (abstract) → `TpuHalHardwareImpl` (intermediate) → `{Jxc,Pxc,Vxc}HardwareImpl` (leaves). The Core/Chip trees are one level deeper. The CostModel and Encoder trees are flat (each leaf is a direct subclass of an abstract root with no shared intermediate). All inheritance is single (`__si_class_type_info`); there is no multiple inheritance in any of the four.

For reimplementation, the contract is:

- The correction: only `TpuHalHardware*` is a `TpuHal` class; the other three roles are separate trees keyed on the same `TpuVersion`.
- The Hardware tree's base/intermediate/leaf structure, the 23-slot vtable, and the impl object layout (208 B Jxc/Pxc, 216 B Vxc; helper pointer at +200).
- The depth and slot counts of the paired Core (46/48-slot) and Chip (25/27-slot) trees, and the CostModel (5-slot) and Encoder (6/14/20-slot) trees.
- The ownership-versus-data-flow coupling between the four trees.

| | |
|---|---|
| **HAL object base** | `tpu::TpuHal` (`_ZTV` @ 0x21d34420, 23 slots, pure at 2 & 20) |
| **HAL intermediate** | `tpu::TpuHalHardwareImpl` (no own `_ZTV`; `_ZTI` @ 0x21cc52e0) |
| **HAL leaves** | `TpuHal{Jxc,Pxc,Vxc}HardwareImpl` (`_ZTV` 0x215fe580 / 0x21608618 / 0x21cabfc0) |
| **Impl object size** | 208 B (Jxc, Pxc) · 216 B (Vxc) · helper pointer at +200 (`obj[25]`) |
| **Core tree** | `tpu::TpuCore` (46-slot base) → `TpuCoreCommonImpl` → `TpuCoreDriverCommonImpl` → 3 leaves (48-slot) |
| **Chip tree** | `tpu::TpuChip` (25-slot base) → `TpuChipCommonImpl` → `TpuChipDriverCommonImpl` → 3 leaves (27-slot) |
| **CostModel tree** | `xla::jellyfish::CycleTable` (5-slot) → `{Jf,Pf,Vf,Glc,Gfc}CycleTable` (flat, 5 leaves) |
| **Encoder tree** | `tpu::TpuCodec` (6-slot) + `isa::Encoder` (14/20-slot) — not a `TpuHal` class |
| **Shared axis** | `tpu::TpuVersion` (0..5) |

---

## The Correction: Only Hardware Is a TpuHal Class

A symbol scan settles the naming question. The complete set of `tpu::TpuHal*` classes in the binary is the Hardware family plus its support classes; the three other "product" names are absent:

```text
present   tpu::TpuHal                     (abstract HAL object base)
present   tpu::TpuHalHardwareImpl         (intermediate)
present   tpu::TpuHal{Jxc,Pxc,Vxc}HardwareImpl
present   tpu::TpuHalFactory              (abstract factory interface)
present   tpu::TpuHalHardwareFactoryBase  (concrete Create/CanCreate)
present   tpu::TpuHal{Jxc,Pxc,Vxc}HardwareFactory
present   tpu::TpuHal{Jxc,Pxc,Vxc}CommonHelper
present   tpu::TpuHalCommonStates         (non-polymorphic data cache; no vtable/RTTI)

absent    tpu::TpuHalCore        — the role is filled by tpu::TpuCore
absent    tpu::TpuHalCostModel   — the role is filled by xla::jellyfish::CycleTable
absent    tpu::TpuHalEncoder     — the role is filled by tpu::TpuCodec / isa::Encoder
```

> **CORRECTION (HAL-A3) —** the framing of "four `TpuHal` sibling product types produced by one factory" is wrong. `TpuHalFactory` produces exactly one kind of object — a `TpuHal{Jxc,Pxc,Vxc}HardwareImpl`. "Core", "CostModel", and "Encoder" are real per-family/per-generation hierarchies, but they live in `tpu::` (Core/Chip), `xla::jellyfish::` (CostModel), and `tpu::`/`isa::` (Encoder) — none of them inherit from a `TpuHal` base or are built by the HAL factory. A reimplementation that derives all four from a common `TpuHalFactory` will not match the binary's class graph.

---

## Hierarchy 1 — TpuHalHardware (the HAL object)

This is the one tree the factory actually produces. Three levels, single inheritance throughout (verified by `__si_class_type_info` base pointers at typeinfo+0x10):

```text
                       (abstract HAL object)
                            tpu::TpuHal
                       _ZTV 0x21d34420   _ZTI 0x21d344e8
                       23 slots; pure-virtual at slot 2 (Type) and slot 20 (CreateAndInitializeChips)
                                |  __si base
                       (intermediate; no own _ZTV)
                       tpu::TpuHalHardwareImpl
                       _ZTI 0x21cc52e0
                       supplies Type (slot 2 → 0x1d3b5480) and the stricter
                       ValidateTopology (slot 19 → 0x1d3b54a0)
                                |  __si base (×3)
        +-----------------------+-----------------------+
        |                       |                       |
  TpuHalJxcHardwareImpl   TpuHalPxcHardwareImpl   TpuHalVxcHardwareImpl
  _ZTV 0x215fe580         _ZTV 0x21608618         _ZTV 0x21cabfc0
  _ZTI 0x215fe648         _ZTI 0x216086e0         _ZTI 0x21cac088
  208 B (0xD0)            208 B (0xD0)            216 B (0xD8)
  helper @ +200           helper @ +200           helper @ +200, flag byte @ +208
        |                       |                       |
        v                       v                       v
  TpuHalJxcCommonHelper   TpuHalPxcCommonHelper   TpuHalVxcCommonHelper
  (heap-owned; freed raw   (heap-owned)            (heap-owned; non-trivial dtor,
   in JxcImpl D2)                                   called then freed in VxcImpl D2)
```

### Vtable Layout — 23 Slots

The base `TpuHal` vtable holds 23 function-pointer slots. Slots 0/1 are the destructors; slots 2 and 20 are `__cxa_pure_virtual` in the abstract base; the remaining 20 carry concrete base implementations. The intermediate `TpuHalHardwareImpl` is what makes the class instantiable for all three families — it fills slot 2 (`Type`) and supplies a stricter slot 19 (`ValidateTopology`). The complete per-family override matrix (which slots each leaf overrides, with addresses) is on the [HAL Factory Override Matrix](hal-factory-override-matrix.md) page; this page documents the layout and the object.

`TpuHalHardwareImpl::Type` (slot 2, 0x1d3b5480) returns the constant `0` — the `kHardware` product-type tag, identical across all three families. `TpuHalHardwareImpl::ValidateTopology` (slot 19, 0x1d3b54a0) chains to `TpuHal::ValidateTopology`, then calls `ScanHardwareDevices`, compares the detected version and `TpuChipParts::variant_name` against the configured topology, and rejects a mismatch with "Detected hardware version ... does not match with topology ...".

### Object Layout

The impl object is allocated and stamped by the family `CreateImpl`, and torn down by its D2 destructor. Both confirm the layout:

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| vtable pointer | +0 | `void*` | per-family impl vtable (`off_215FE590` / `off_21608628` / `off_21CABFD0`) | CERTAIN |
| base state | +8 .. | (TpuHal base) | `TpuVersion`, work-queue, topology, etc. (set by `TpuHal::TpuHal`) | HIGH |
| `helper` | +200 (`obj[25]`) | `TpuHal*CommonHelper*` | heap-owned CommonHelper; `nullptr` until `CreateAndInitializeChips` | CERTAIN |
| `mesh_torn_down` flag | +208 | `uint8` | **VXC only** — guards `PreTearDownChips`; absent on the 208 B Jxc/Pxc objects | CERTAIN |

> **QUIRK —** the VXC impl is 216 bytes (`operator new(0xD8)`), eight bytes larger than the 208-byte (`0xD0`) Jxc and Pxc impls. The extra space is a flag byte at +208 that `VxcImpl::PreTearDownChips` (0x1d111720) reads to skip a double mesh-teardown. The CommonHelper pointer stays at +200 (`obj[25]`) in all three. A reimplementation that assumes a uniform 208-byte object across families will over-write past the Jxc/Pxc allocation or under-allocate the Vxc one.

The two D2 destructors also differ in how they release the helper: `JxcImpl::~JxcImpl` (0x0e724de0) re-plants the vtable, reads `obj[25]`, nulls it, and `free()`s it directly — the JXC helper is POD-trivial. `VxcImpl::~VxcImpl` (0x1d111740) calls `TpuHalVxcCommonHelper::~TpuHalVxcCommonHelper(helper)` *before* `free()` — the VXC helper has a non-trivial destructor.

---

## Hierarchy 2 — TpuCore and TpuChip (the per-core role)

The "Core" role is `tpu::TpuCore`: a TensorCore/BarnaCore execution context that lives on a chip. It is created by the chip (`TpuChipCommonImpl::CreateCores`), which is in turn created by the HAL's `CommonHelper::CreateChips`. The Core tree is four levels deep — one deeper than Hardware — and is paired with an equally deep `TpuChip` tree.

```text
  tpu::TpuChip  (ROOT)                          tpu::TpuCore  (ROOT)
  _ZTV 0x21cad4e8  (25 slots)                   _ZTV 0x21cad8e8  (46 slots)
     |  __si                                       |  __si
  tpu::TpuChipCommonImpl                        tpu::TpuCoreCommonImpl
  _ZTV 0x21cad3d8                               _ZTV 0x21cad730
     |  __si                                       |  __si
  tpu::TpuChipDriverCommonImpl                  tpu::TpuCoreDriverCommonImpl
  _ZTV 0x215fe828                               (_ZTI 0x21cacee0)
     |  __si (×3)                                   |  __si (×3)
  +--+---+----+                                 +--+----+----+
  Jxc  Pxc  Vxc                                 Jxc  Pxc  Vxc
  ChipJxcDriverImpl   .710                       CoreJxcDriverImpl  .ea30
  ChipPxcDriverImpl   .8758                      CorePxcDriverImpl  .8978
  ChipVxcDriverImpl   .c0b0   (27 slots)         CoreVxcDriverImpl  .c2e8   (48 slots)
     |  TpuChipCommonImpl::GetCore(TpuCoreOnChip) ─┘
```

The per-family override set in the Core leaves is almost entirely the BarnaCore (SparseCore sequencer / address-handler) programming surface — vtable slots 25..38 — plus host-interrupt plumbing (slots 46, 47). The TensorCore run/load/memory surface is fully generic, implemented once in `TpuCoreCommonImpl` / `TpuCoreDriverCommonImpl`. PXC uniquely overrides slot 11 (`FlushOutfeedQueues`).

In the Chip tree, VXC overrides far more slots than JXC or PXC (throttle, core-dump, host-sync-flag, init, teardown, continuation-queue) — the fabric-attached v3/v4/v5 silicon needs its own machinery where the older PCIe-attached jxc/pxc inherit the generic `TpuChip` base / `TpuChipCommonImpl`. This mirrors VXC's lone slot-18 override in the Hardware tree. Two auxiliary per-family trees hang off this layer with the same Jxc/Pxc/Vxc pattern: `TpuChipProfiler*Impl` (`_ZTV` 0x215fff68 / 0x21609060 / 0x21cb1ce0) and `TpuCoreDebugInterface*DriverImpl` (`_ZTV` 0x215fee10 / 0x21608c10 / 0x21cac550). (Slot-by-slot Core/Chip override detail: [VXC Family](vxc-family.md) and the per-family pages.)

---

## Hierarchy 3 — CycleTable (the cost-model role)

The "CostModel" role is `xla::jellyfish::CycleTable` — and it is not in the `tpu::` HAL runtime at all. It is a compiler-side class in the XLA backend, attached to the `xla::jellyfish::Target` (one per `TpuVersion`), not owned by any HAL object. The tree is flat: an abstract root with no shared intermediate, and five leaves each fully overriding all five slots.

```text
  xla::jellyfish::CycleTable  (abstract ROOT; no own _ZTV)   _ZTI 0x21c20008
     |  __si base of all 5 (each is a direct leaf — no intermediate)
  +-----------+-----------+-----------+-----------+-----------+
  JfCycleTable PfCycleTable VfCycleTable GlcCycleTable GfcCycleTable
  v=0,1        v=2          v=3          v=4           v=5
  _ZTV 0x21c1ffb8 0x21c20048 0x21c200c8  0x21c20148    0x21c201c8

  5 slots, all overridden in every leaf:
    slot 0  ~CycleTable D2
    slot 1  ~CycleTable D0
    slot 2  GetCyclesForThroughput(Instruction) const
    slot 3  EstimateSinCosCost() const
    slot 4  EstimateTanCost() const
```

Dispatch is neither a `switch` nor a vtable cast but a `util_registration::FunctionRegistry<TpuVersion, unique_ptr<CycleTable>(const Target&)>`. `CycleTable::Create(Target const&)` (0x1c89cc00) reads `target.tpu_version()` (Target+920) and invokes the registered lambda; the `cycle_table.cc` static initializer (0x21353460) registers all six keys 0..5. The data layer is per-generation `*Performance` latency/resource arrays plus the embedded `chip_parts.binarypb` for clock frequency — the cost model is fully data-table-driven, with no learned-model client.

---

## Hierarchy 4 — TpuCodec and isa::Encoder (the encoder role)

The "Encoder" role is two coupled levels, neither of which inherits from any `TpuHal` class: the public per-codename `tpu::TpuCodec` (the API the runtime calls) and the internal per-family/per-core `isa::Encoder` (the bit-level worker the codec drives).

```text
  LEVEL A — public codec (6-slot vtable)
  tpu::TpuCodec  (abstract ROOT; no own _ZTV)   _ZTI 0x21d35858
     |  __si base of 5 named + 1 anonymous (v=5)
  +----------+-----------+-----------+-----------+-----------+----------+
  Jellyfish  Dragonfish  Pufferfish  Viperfish   Ghostlite   (anon v=5)
  v=0        v=1         v=2         v=3         v=4         6acc60406
  _ZTV.360f0 .35800      .36148      .361a0      .35c00      sub_1E838380 (no _ZTV)

  6 slots: D2 (shared base @ 0x1e836100), D0, Encode, Decode, EncodeBundle, DecodeBundle
  Factory: TpuCodec::Create(TpuVersion) @ 0x1e835fa0 — jump-table switch on v=0..5

  LEVEL B — bit-level worker (14-slot per-family / 20-slot legacy)
  platforms_deepsea::jellyfish::isa::Encoder  (abstract ROOT; no own _ZTV)  _ZTI 0x21cb6a20
     |  __si
  +-- legacy (20 slots): EncoderJf / EncoderDf / EncoderBcsDf
  +-- per-family (14 slots): EncoderPf*/EncoderVf*/EncoderGl* (TensorCore / BarnaCore / SparseCore)
        each COMPOSES (template, non-virtual) a *CodecBase<...> mixin carrying the
        per-codename bundle bit layout (the 41/51/64-byte wire formats)
```

`TpuCodec::Create` (0x1e835fa0) is a six-case jump-table switch; cases 0..4 call named `CreateTpuCodec<X>` constructors and case 5 calls an anonymous-namespace creator (`sub_1E838380`) — the v=5 / 6acc60406 codec has no named C++ class and no recovered vtable. The codec drives the matching `isa::Encoder` leaf via `tpu::internal::CreateEncoder{JfDf,Pf,Vf,GlGf}`, which dispatch on `TpuSequencerType` (not `TpuVersion`).

> **NOTE —** the Encoder is not owned by the Core. The Codec/Encoder (a compiler and serialization layer) *produces* the bundle bytes that become a `TpuCoreProgram`, which a `TpuCore*DriverImpl::LoadProgram` later loads and runs. The coupling is data-flow (program bytes), not object ownership.

---

## How the Four Trees Relate

There is no single linear ownership chain. The four trees couple through one shared key (`TpuVersion`) and two distinct mechanisms (composition versus data-flow):

```text
  TpuHal (HardwareImpl)                         <- the only TpuHal class; built by TpuHalFactory
    └─owns→ N × TpuChip*DriverImpl              (CommonHelper::CreateChips)        [composition]
              └─owns→ M × TpuCore*DriverImpl    (TpuChipCommonImpl::CreateCores)   [composition]
                        └─runs→ TpuCoreProgram  (LoadProgram → TpuCoreProgramHandleImpl)

  TpuCodec (per TpuVersion; static factory, NOT owned by the HAL)
    └─drives→ isa::Encoder leaf  (per TpuSequencerType)
                └─emits→ bundle bytes → TpuCoreProgram                              [data-flow]

  CycleTable (per TpuVersion; attached to xla::jellyfish::Target, NOT owned by the HAL)
    └─reads→ per-gen *Performance arrays; consumed during XLA scheduling           [compiler-side]
```

`Hardware → Chip → Core` is a true composition chain, all in `tpu::` runtime. The Encoder reaches the Core only through the program bytes it serializes. The CostModel touches neither — it is a compiler-side object keyed by the same `TpuVersion`. The one axis every tree dispatches on is `tpu::TpuVersion`, which is exactly why the codename taxonomy ([6-Codename Authoritative Reconciliation](tpu-version-codename-matrix.md)) is the spine of the whole target model.

---

## Cross-References

- [6-Codename Authoritative Reconciliation](tpu-version-codename-matrix.md) — `TpuVersion`, the single axis all four hierarchies dispatch on
- [HAL Families](hal-families.md) — the three factory classes and five init modules that build the Hardware tree's leaves
- [HAL Factory Override Matrix](hal-factory-override-matrix.md) — the full 23-slot per-family override matrix for the Hardware tree
- [JXC Family](jxc-family.md) — Jellyfish (v0) + Dragonfish (v1): the Jxc impl, chip, core, and codec specializations
- [PXC Family](pxc-family.md) — Pufferfish (v2) specializations
- [VXC Family](vxc-family.md) — Viperfish (v3): the impl that overrides the most chip/core slots
- [GXC Family](gxc-family.md) — Ghostlite (v4) + 6acc60406 (v5), sharing the VXC impl vtable
