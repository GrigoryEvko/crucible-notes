# IsaEmitter Registry

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

The TensorCore and BarnaCore code generators do not hard-code which `IsaEmitter` subclass lowers an [LLO bundle](bundle-model-overview.md) for a given chip. They consult a process-wide **pair-key registry**: a `util_registration::FunctionRegistry` keyed by a `std::pair<tpu::TpuVersion, tpu::TpuSequencerType>` whose stored value is a factory that builds a `unique_ptr<xla::jellyfish::IsaEmitter>`. Each `(generation, sequencer)` cell is registered once at static-init time by a `google_init_module_*_emitter` translation unit; at compile time `IsaEmitterFactory::Create` packs the two enums into a single 64-bit key, looks the cell up, and invokes the cell's factory to construct the leaf emitter that lowers each LLO op to its [proto bundle](mc-emitter.md).

The registry is the high-level *selector* — it picks the leaf emitter class per `(gen, seq)`. The leaf then routes each LLO opcode through its own per-op `EmitX` template family (the proto-population layer), and the [per-slot encoders](v5plus-emitx-bit-positions.md) finally pack the bits. The registry's two axes are therefore the spine of the TensorCore/BarnaCore encode path: the first axis (`TpuVersion`) selects the silicon generation; the second (`TpuSequencerType`) selects which of a chip's sequencers (TensorCore vs the two BarnaCore variants) the bundle targets. Exactly **12 cells** are populated, across **8 init modules**, resolving to **6 distinct leaf emitter classes**. The SparseCore sequencers (`SparseCoreSequencer` / TAC / TEC) appear in the `TpuSequencerType` enum but register **no** cell — they reach their `EmitX` templates through a separate variant-keyed dispatcher (see the [QUIRK below](#why-only-tensorcorebarnacore-cells-exist)).

For reimplementation, the contract is:

- The key is a 64-bit pack: **low dword = `TpuVersion`, high dword = `TpuSequencerType`** — confirmed at both the register side (each module builds the constant) and the lookup side (`IsaEmitterFactory::Create` reads `Target+0x398` for the version and shifts the sequencer into the high dword).
- `Target+0x398` is the *sole* version source for the lookup — the same field the rest of the target/cost-model layer keys on.
- The registry stores `shared_ptr<MapValue>`; a `MapValue` carries the factory closure (`__call_func`) and a one-byte "absent" flag at `+0x10`. A lookup MISS is a **hard `LogMessageFatal`** ("couldn't create ISA emitter for target: …"), not a soft fallback.
- The 12-cell census: each `(gen, seq)` cell installs a specific `__call_func` wrapper whose lambda constructs one concrete `IsaEmitter` leaf. One wrapper may serve several generations (Jellyfish/Dragonfish share one leaf; v6e/v7 share another).
- The v4+ `Target` classes are **direct** subclasses of `xla::jellyfish::Target` (single inheritance); only `DragonfishTarget : JellyfishTarget` is a two-level chain.

| | |
|---|---|
| **Registry type** | `util_registration::FunctionRegistry<pair<tpu::TpuVersion, tpu::TpuSequencerType>, unique_ptr<IsaEmitter>(Target const*, CompilerMetadata*, TpuSequencerType, bool, bool, IsaEmitter*, optional<bool>)>` |
| **Singleton** | `xla::jellyfish::GetIsaEmitterRegistry` @ `0x143f6480` (`__cxa_guard`-protected function-local static; 40-B object zero-init) |
| **Register** | `FunctionRegistry::Register` @ `0x140c2360` (mutex-locked `flat_hash_map` insert) |
| **Lookup** | `FunctionRegistry::Get` @ `0x140af4e0`; key-compare in the `raw_hash_set` find @ `0x140af5e0` |
| **Factory entry** | `xla::jellyfish::IsaEmitterFactory::Create` @ `0x140af220` |
| **Key layout** | `uint64 = (uint32 TpuSequencerType << 32) \| (uint32 TpuVersion)` — `[+0]`=version, `[+4]`=seqtype |
| **Version source** | `Target+0x398` (read at `Create+0x6f`) |
| **Populated cells** | 12 (8 init modules → 6 leaf classes) |
| **Map policy** | `FlatHashMapPolicy<pair<TpuVersion,TpuSequencerType>, shared_ptr<MapValue>>` |
| **MISS** | `LogMessageFatal` "couldn't create ISA emitter for target:" (`Create+0x259`) |
| **Confidence** | CONFIRMED (decompile-verified) unless a row says otherwise |

---

## The Two Axes

The registry is a two-dimensional table: `(TpuVersion) × (TpuSequencerType) → IsaEmitter leaf`. Neither axis is a free index — both are silicon-defined enums, and a cell exists only where that combination is a real engine on real hardware.

**Axis 1 — `TpuVersion`** is the silicon generation, the same six-value enum that keys the codec metadata and the cost model. `tpu::TpuVersionToString` (`0x20b3a480`) indexes a 6-entry pointer table and traps for any ordinal `≥ 6`:

| `TpuVersion` | Codename | Public name | Confidence |
|---|---|---|---|
| 0 | jellyfish | TPU v2 | CONFIRMED |
| 1 | dragonfish | TPU v3 | CONFIRMED |
| 2 | pufferfish | TPU v4 | CONFIRMED |
| 3 | viperfish | TPU v5p (+ v5e lite) | CONFIRMED |
| 4 | ghostlite | TPU v6e | CONFIRMED |
| 5 | 6acc60406 | TPU v7 | CONFIRMED |

The codename strings are read straight from the `tpu::TpuVersionToString` table (`off_22011BF0`): ordinals 0..5 resolve to `jellyfish`, `dragonfish`, `pufferfish`, `viperfish`, `ghostlite`, `6acc60406`. The public-name column follows the canonical codename → marketing-name mapping in the [per-gen comparison matrix](../appendix/per-gen-comparison-matrix.md); `6acc60406` is the only generation whose binary carries no public-name string (the literal `Trillium`/`Ironwood` appears **nowhere** in `libtpu.so` — `6acc60406` is the sole codename for that generation).

**Axis 2 — `TpuSequencerType`** is which sequencer in the chip the bundle targets. `tpu::TpuSequencerTypeToString` (`0x20b362e0`) is a single instruction — `return off_22010DE0[ordinal]` — an ordinal-indexed pointer table with eight entries:

| `TpuSequencerType` | Name | Registers a cell? | Confidence |
|---|---|---|---|
| 0 | `TensorCoreSequencer` | yes (every gen) | CONFIRMED |
| 1 | `BarnaCoreSequencer` | yes (v0/v1/v2) | CONFIRMED |
| 2 | `BarnaCoreAddressHandler` | yes (v0/v1/v2) | CONFIRMED |
| 3 | `SparseCoreSequencer` | no (separate path) | CONFIRMED |
| 4 | `SparseCoreTileAccessCoreSequencer` (TAC) | no | CONFIRMED |
| 5 | `SparseCoreTileExecuteCoreSequencer` (TEC) | no | CONFIRMED |
| 6 | `IMEM` | no | CONFIRMED |
| 7 | `VIMEM` | no | CONFIRMED |

The cross-product is sparse. Only `(gen, seq)` pairs that name a sequencer actually present on that generation are registered: v0/v1/v2 carry a TensorCore plus BarnaCore engines; v3/v4/v5 carry only a TensorCore on this path (their SparseCore lowering is a separate dispatcher). That gives 12 live cells out of the 6 × 8 = 48 grid positions.

> **NOTE —** the two enums are independent in the binary but coupled in the key word. The version comes from `Target+0x398` (a runtime property of the target object); the sequencer type is an explicit argument passed by the caller for the engine being lowered. A reimplementation that derives the sequencer from the version, or vice versa, will mis-key the lookup.

---

## The Key Layout and the Lookup Path

`IsaEmitterFactory::Create` (`0x140af220`) is the single entry point. It builds the key, looks it up, and either invokes the cell's factory or dies. The decompiled body shows the pack and the MISS check exactly:

```c
// xla::jellyfish::IsaEmitterFactory::Create  @ 0x140af220  (decompiled, trimmed)
__int64 Create(const Target *target, CompilerMetadata *md,
               TpuSequencerType seq, bool compact, ...) {
    if (seq == 2 /* BarnaCoreAddressHandler */) {
        // BarnaCoreAddressHandler rejects parallel codegen and compact emit (Fatal)
        ...
    }
    Registry *reg = GetIsaEmitterRegistry();           // the singleton

    // KEY PACK: low dword = Target+0x398 (version); high dword = seq
    uint64_t key = *(uint32_t*)((char*)target + 0x398)
                 | ((uint64_t)seq << 32);

    MapValue *v = FunctionRegistry::Get(reg, &key);    // raw_hash_set find @ 0x140af5e0
    if (v->absent /* byte @ MapValue+0x10 == 1 */)
        LogMessageFatal("couldn't create ISA emitter for target: ", target->name);

    // INVOKE the cell's factory __call_func (slot v[2]) -> constructs the leaf
    return v->call_func(v, target, md, seq, compact, ..., key_lo16);
}
```

Three facts from this body are load-anchored:

- **`Target+0x398` is the version source.** The decompiler renders it `*((unsigned int *)v10 + 230)` — `230 × 4 = 0x398`. This is the same `Target+0x398` the [target/cost-model layer](../targets/tpuhal-class-hierarchy.md) reads as its generation selector, so the IsaEmitter registry and the cost model are keyed off the *same* field.
- **The MISS is fatal.** The `MapValue` returned by `Get` carries an "absent" sentinel byte at `+0x10`; when set, `Create` raises `LogMessageFatal`. There is no default emitter — an unregistered `(gen, seq)` aborts the compile.
- **Sequencer 2 is special-cased early.** Before the lookup, `seq == BarnaCoreAddressHandler` rejects parallel codegen and compact emit with their own fatals — a per-engine constraint the registry value alone could not express.

The key-compare in the `raw_hash_set` find (`0x140af5e0`) compares the low dword (version) and the `+4` dword (sequencer) separately — direct confirmation that the 64-bit key is two packed int32s, version in the low half.

---

## The Registration Side

Each cell is installed once, at static-init time, by a `google_init_module_*_emitter` function calling `FunctionRegistry::Register` (`0x140c2360`). `Register` takes a `mutex` lock, heap-allocates a 0x48-byte `MapValue` (carrying the factory closure), and inserts it into the `flat_hash_map` keyed by the `pair`; a duplicate key raises `LogMessageFatal` "Registration failed; key already exists in registry". The key constant the module builds is the same packed `uint64` the lookup reconstructs.

The `jellyfish_emitter` module is the clearest example: it registers four cells back-to-back, all installing the **same** `JellyfishEmitter` factory closure (`$_0`), differing only in the key constant:

```c
// google_init_module_jellyfish_emitter  @ 0x213ecdc0  (decompiled, trimmed)
key = 0;            Register(GetIsaEmitterRegistry(), &key, JellyfishEmitter_$_0, ...); // (v0, seq0)
key = 0x100000000;  Register(GetIsaEmitterRegistry(), &key, JellyfishEmitter_$_0, ...); // (v0, seq1)
key = 1;            Register(GetIsaEmitterRegistry(), &key, JellyfishEmitter_$_0, ...); // (v1, seq0)
key = 0x100000001;  Register(GetIsaEmitterRegistry(), &key, JellyfishEmitter_$_0, ...); // (v1, seq1)
```

`key = 0x100000000` is `(version 0, seqtype 1)`; `key = 1` is `(version 1, seqtype 0)`. The high dword is the sequencer, the low dword the version — exactly the layout `Create` packs. The same idiom appears in every module; the per-gen modules each load a single key constant (`mov [rbp-8], 3` for Viperfish, `4` for Ghostlite, `5` for 6acc60406) before their `Register` call.

> **NOTE —** the registered closure is a *factory*, not the leaf itself. The `MapValue` stores a `__call_func` wrapper around a lambda; the lambda constructs the concrete leaf on demand inside `Create`. One closure can therefore serve several keys — the four Jellyfish/Dragonfish cells above all share one `JellyfishEmitter` closure, so a single leaf class lowers both v0 and v1 across both their TensorCore and BarnaCore sequencers.

---

## The 12-Cell Census

The whole-section scan finds exactly twelve `Register` call sites feeding this registry, across eight init modules. Each cell's leaf is the `IsaEmitter` subclass the cell's `__call_func` lambda constructs.

| # | key (u64) | (`TpuVersion`, `TpuSequencerType`) | Init module | Leaf emitter | Confidence |
|---|---|---|---|---|---|
| 1 | `0x000000000` | (0 jellyfish, 0 TensorCore) | `jellyfish_emitter` | `JellyfishEmitter` | CONFIRMED |
| 2 | `0x100000000` | (0 jellyfish, 1 BarnaCoreSequencer) | `jellyfish_emitter` | `JellyfishEmitter` | CONFIRMED |
| 3 | `0x200000000` | (0 jellyfish, 2 BarnaCoreAddressHandler) | `barna_core_address_handler_emitter` | `BarnaCoreAddressHandlerEmitter` | CONFIRMED |
| 4 | `0x000000001` | (1 dragonfish, 0 TensorCore) | `jellyfish_emitter` | `JellyfishEmitter` | CONFIRMED |
| 5 | `0x100000001` | (1 dragonfish, 1 BarnaCoreSequencer) | `jellyfish_emitter` | `JellyfishEmitter` | CONFIRMED |
| 6 | `0x200000001` | (1 dragonfish, 2 BarnaCoreAddressHandler) | `barna_core_address_handler_emitter` | `BarnaCoreAddressHandlerEmitter` | CONFIRMED |
| 7 | `0x000000002` | (2 pufferfish, 0 TensorCore) | `pufferfish_tensorcore_emitter` | `PufferfishTensorCoreEmitter` | CONFIRMED |
| 8 | `0x100000002` | (2 pufferfish, 1 BarnaCoreSequencer) | `pufferfish_barnacore_sequencer_emitter` | `PufferfishBarnaCoreSequencerEmitter` | CONFIRMED |
| 9 | `0x200000002` | (2 pufferfish, 2 BarnaCoreAddressHandler) | `pufferfish_barnacore_channel_emitter` | `PufferfishBarnaCoreChannelEmitter` | CONFIRMED |
| 10 | `0x000000003` | (3 viperfish, 0 TensorCore) | `viperfish_tensorcore_emitter` | `ViperfishTensorCoreEmitter` | CONFIRMED |
| 11 | `0x000000004` | (4 ghostlite, 0 TensorCore) | `ghostlite_tensorcore_emitter` | `GhostliteTensorCoreEmitter` | CONFIRMED |
| 12 | `0x000000005` | (5 6acc60406, 0 TensorCore) | `6acc60406_tensorcore_emitter` (`sub_213ED1C0`) | `GhostliteTensorCoreEmitter` (reused) | CONFIRMED |

The shape of the table is the silicon story:

- **v0/v1 (Jellyfish/Dragonfish)** each get three cells — a TensorCore plus the chip's two BarnaCore sequencer roles — all served by two leaf classes (`JellyfishEmitter` for TC + BarnaCoreSequencer; `BarnaCoreAddressHandlerEmitter` for the address-handler). The `barna_core_address_handler_emitter` module (`0x213ed040`) installs cells 3 and 6 as two explicit key constants in one body — `0x200000000` then `0x200000001`, both with the same `$_0` closure — so both address-handler cells are byte-confirmed, not inferred.
- **v2 (Pufferfish)** also gets three cells, but its BarnaCore is split into a *sequencer* emitter and a *channel* emitter (cells 8 and 9), each a distinct leaf.
- **v3/v4/v5 (Viperfish/Ghostlite/6acc60406)** get only a TensorCore cell. There is no BarnaCore on v5p+; their SparseCore goes through a separate path.
- **Cell 12 reuses cell 11's leaf.** The 6acc60406 (TPU v7) TensorCore cell installs the *same* `GhostliteTensorCoreEmitter` factory as Ghostlite (TPU v6e) — mirroring the runtime fact that the v5-ordinal generation reuses `GhostliteTarget`. The generation merge happens at the leaf-class layer; the gfc-vs-glc encoder split happens downstream inside the codec.

> **CORRECTION (REG-1) —** cells 11 and 12 are not two registrations inside one translation unit. The decompile shows the named `ghostlite_tensorcore_emitter` init function (`0x213ed160`) registers only cell 11 (key 4, from `.../target/ghostlite/ghostlite_tensorcore_emitter.cc:3598`) and returns; cell 12 (key 5) is registered by an adjacent static-init function (`sub_213ED1C0`) compiled from a *separate* source file (`.../target/6acc60406/6acc60406_tensorcore_emitter.cc:3649`) that installs the same `GhostliteTensorCoreEmitter` factory (its `__call_func` `sub_14398B60` and cell 11's `__call_func` at `0x142a04c0` both `operator new(0x2A0u)` then call the one `GhostliteTensorCoreEmitter::GhostliteTensorCoreEmitter` ctor). The two cells share a leaf but come from two TUs.

---

## Why Only TensorCore/BarnaCore Cells Exist

The `TpuSequencerType` enum exposes the three SparseCore sequencers (`SparseCoreSequencer` = 3, TAC = 4, TEC = 5) and the two memory sequencers (`IMEM` = 6, `VIMEM` = 7), but **no module registers a cell for any of them**. The SparseCore engines reach their `EmitX` templates through a completely separate dispatcher.

> **QUIRK — the SparseCore engines bypass the pair-key registry entirely.** SparseCore (SCS/TAC/TEC) lowering is driven by `xla::tpu::sparse_core::code_generator`, a two-tier path that keys on the chip-parts *variant* (not on `Target+0x398`): `RunCodeGen` → a gen-switch `MakeTpuCoreProgram` → a per-gen template instantiation (`MakeTpuCoreProgram<{Viperfish,Ghostlite}Emitter, …>`) → `Emitter::ConsumeProgram` → a per-bundle-type `Consume*Instruction` jump-table on the MCInst opcode → `EmitX<Bundle, Op>`. The per-instruction engine (SCS/TAC/TEC) is chosen by a section-name classifier, not a registry `Get`. A reimplementation that expects the pair-key registry to own all sequencer types will find the SparseCore half missing and must model the variant-keyed dispatcher separately.

The practical split is: the pair-key registry owns the TensorCore + BarnaCore halves of the encode path (the 12 cells above); the variant-keyed `code_generator` owns the SparseCore half. Both halves end at the same [per-slot encoder → `BitCopy`](v5plus-emitx-bit-positions.md) bit-packing stage.

---

## The Target Base-Class Chain

The version axis is backed by a `Target` class hierarchy: `IsaEmitterFactory::Create` reads `Target+0x398` to key the registry, and the per-gen `Target` subclass is what carries that field and the chip-parts profile the emitter consults. The hierarchy is single-inheritance throughout (every typeinfo is `__si_class_type_info`), rooted at the abstract `xla::jellyfish::Target`:

```text
  xla::jellyfish::Target  (abstract root, __class_type_info)
    ├── JellyfishTarget (ordinal 0)
    │     └── DragonfishTarget (ordinal 1) ← the ONLY two-level chain
    ├── PufferfishTarget (ordinal 2)       ← direct
    ├── ViperfishTarget  (ordinal 3)       ← direct
    └── GhostliteTarget  (ordinal 4; reused by ordinal 5 / 6acc60406, no separate class)

  xla::jellyfish::SparseCoreTarget  (parallel abstract root)
    ├── ViperfishSparseCoreTarget
    └── GhostLiteSparseCoreTarget
```

| Class | ZTI @ | base class | object size | ordinal / public | Confidence |
|---|---|---|---|---|---|
| `Target` (root) | `0x21ccef00` | — | — | — | CONFIRMED |
| `JellyfishTarget` | `0x21cc7420` | `Target` | `0x958` (2392 B) | 0 / v2 | CONFIRMED |
| `DragonfishTarget` | `0x21cc6ba8` | `JellyfishTarget` | `0x958` | 1 / v3 | CONFIRMED |
| `PufferfishTarget` | `0x21cc7d38` | `Target` | `0x950` (2384 B) | 2 / v4 | CONFIRMED |
| `ViperfishTarget` | `0x21cc8f78` | `Target` | `0x950` | 3 / v5p | CONFIRMED |
| `GhostliteTarget` | `0x21cc85f8` | `Target` | `0x950` | 4 / v6e (+5/v7 reuse) | CONFIRMED |
| `SparseCoreTarget` (root) | `0x21ccef10` | — | — | — | CONFIRMED |
| `ViperfishSparseCoreTarget` | `0x21cc9080` | `SparseCoreTarget` | — | v5p SC | CONFIRMED |
| `GhostLiteSparseCoreTarget` | `0x21cc8700` | `SparseCoreTarget` | — | v6e SC | CONFIRMED |

The 8-byte object-size delta (`0x958` for v0/v1 vs `0x950` for v2+) is `JellyfishTarget`'s extra `+0x950` `Performance*` field (the Jf cost-model object built in its ctor); v4+ build their performance object through a different path and omit that field.

Each per-gen ctor contributes only a handful of `this`-stores beyond the base `Target::Target` call and the vtable patch. `PufferfishTarget::PufferfishTarget` (`0x1d493840`) is representative and decompile-confirmed:

```c
// PufferfishTarget::PufferfishTarget  @ 0x1d493840  (decompiled)
Target::Target(this, version, variant_name, ..., CreateDefaultTargetEnv(chip_parts));
*(void**)this              = off_21CC74E8;          // vtable patch -> PufferfishTarget
*((uint32_t*)this + 0x14F) = 5;                     // +0x53c : chip-generation code = 5
*((uint32_t*)this + 0x245) = 1;                     // +0x914 : config word = 1
new_divisor = ConstantDivisor(16);                  // +0x938 : lane/tile divisor = 16
*((void**)this + 0x127)    = new_divisor;
```

The single `Target::Target` base call confirms `PufferfishTarget` derives **directly** from `Target` — there is no per-arch intermediate. Only the v0→v1 pair chains (Dragonfish reuses Jellyfish's ctor and patches the vtable). The per-gen ctor values are the silicon profile: `+0x53c` is the chip-generation code (Pufferfish 5, Viperfish/Ghostlite 7), `+0x914` a config word, and `+0x938` a `util::math::ConstantDivisor` whose divisor is the per-gen lane/tile count (8 / 16 / 32 for Jellyfish / Pufferfish / Viperfish+).

> **NOTE —** the per-gen ctor is *not* where the bulk of the target fields are set. The shared `Target::Init` (`0x1d60fc20`, common to all generations) writes 173 distinct target fields across `+0x3cc..+0x948`; the per-generation *values* come from chip-parts variant-visit lambdas dispatched inside `Init`, not from a different `Init` body. The ctor above only stamps the handful of fields that are structurally per-class.

---

## How the Registry Fits the Encode Pipeline

The registry is the first of three layers between an LLO bundle and its packed bytes:

```text
                Target+0x398 (TpuVersion)   +   TpuSequencerType (arg)
                                  │
                                  ▼
   1.  IsaEmitterFactory::Create  ──pack key──▶  FunctionRegistry::Get  ──▶  IsaEmitter leaf
                                  │                                              (per (gen,seq))
                                  ▼
   2.  leaf::Emit<op>            ──▶  EmitX<Bundle,Op> family  (proto submessage population)
                                  │
                                  ▼
   3.  <Slot>Encoder::Encode     ──▶  BitCopy(buf, abs_bit, &field, 0, width)  (bit packing)
```

Layer 1 (this page) selects *which* leaf emitter handles a `(gen, seq)`. Layer 2 is the leaf's per-op `EmitX` template family, which populates a proto bundle submessage per op — the [proto-population layer](v5plus-emitx-bit-positions.md). Layer 3 is the per-slot encoder that finally writes absolute bit positions into the [fixed-width bundle word](bundle-model-overview.md) via `BitCopy`. The registry contributes no bits itself; it is pure dispatch. Its role is exactly analogous to the LLVM-MC side's table select — but where the [MC emitter](mc-emitter.md) keys on the opcode through a jump table, the IsaEmitter registry keys on `(gen, seq)` through a hash map, and the two paths are complementary: the MC emitter returns all-zero for every TensorCore/V5+ opcode precisely because their real bytes come from this proto-bundle path the registry selects into.

---

## Cross-References

- [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) — the per-op `EmitX` → `<Slot>Encoder::Encode` → `BitCopy` bit-packing stage the leaf emitter feeds.
- [MC-Emitter](mc-emitter.md) — `getBinaryCodeForInstr`; the complementary LLVM-MC path that returns all-zero for every opcode this registry's leaves encode.
- [Bundle Model](bundle-model-overview.md) — the per-generation fixed-width bundle word the encode path lays slots into.
- [TpuHal Class Hierarchy](../targets/tpuhal-class-hierarchy.md) — the `tpu::TpuVersion` axis all the target/codec/cost-model trees dispatch on, including the `Target+0x398` field this registry keys on.
- [Per-Gen Comparison Matrix](../appendix/per-gen-comparison-matrix.md) — the canonical codename ↔ `TpuVersion` ordinal ↔ public-name mapping (jellyfish v2 … 6acc60406 v7) the version axis on this page resolves against.
