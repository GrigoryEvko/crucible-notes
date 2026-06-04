# SpecialPurposeSyncFlags — the chip-config SFLAG reservation message, its FromProto sink, and the `GetSpecialPurposeSyncFlags` accessor

> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset `0xe63c000`, `.rodata` VMA == file offset `0x84a0000`).
> **Status:** Reimplementation-grade · **Evidence grade:** Confirmed (byte-anchored) — the `GetSpecialPurposeSyncFlags` accessor, the `TpuChipConfig::FromProto` element build, the 0x40-byte runtime element layout, the `EnumMap::Clear` scalar-persistence, and the `Target::Init` / `SparseCoreTarget::Init` struct sinks are all byte-exact (`+core<<6`, `& 0x100000000` presence gates, `size − 5`). The literal per-gen SFLAG integers are an embedded-memfile dependency (LOW — see [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md)). · **Part XIII — On-Pod Collectives & Barriers** / SFLAG & barriers · [back to index](../index.md)

## Abstract

Every barrier on a TPU is a reserved **sync-flag (SFLAG)** number, and the numbers a barrier may take are not hard-coded — they are carried, one set per core type, in the chip config. The carrier is a proto submessage, `SpecialPurposeSyncFlags`, embedded as the repeated field 13 of `TpuChipConfigProto`. Each instance pairs a `compiler_reserved` repeated-int32 range (the per-id barrier window) with four named scalar SFLAG numbers (`sequencer_overlay`, `tile_overlay`, `global_barrier_sflag`, `local_barrier_sflag`). The general SFLAG number formulas that consume `compiler_reserved` live on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the literal per-`(codename, deployment)` integers live on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md). **This page owns the parsing and the access path**: the `TpuChipConfig::FromProto` runtime sink that lands the proto into an `EnumMap` element, the `GetSpecialPurposeSyncFlags(coreType)` accessor that retrieves it (`chip+0x2a0 + (core<<6)`, gated by the `chip+0x360` presence bitmask), the **overlay semantics** of the four named scalars (each a single SFLAG *index* — not a bitmask — packed with a presence bool, surviving `EnumMap::Clear`), and the `EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>` that keys the set by core type.

For reimplementation, the contract is:

- **The proto carrier:** `TpuChipConfigProto.special_purpose_sync_flags` (field 13, repeated `SpecialPurposeSyncFlags`), one element per `TpuCoreType`. Each element has `core_type` (f1), `compiler_reserved` (f3, repeated int32), and four scalar SFLAG numbers `sequencer_overlay`/`tile_overlay`/`global_barrier_sflag`/`local_barrier_sflag` (f4–f7).
- **The FromProto sink:** `TpuChipConfig::FromProto` @`0x20aea100` heap-copies `compiler_reserved` into a `vector<int32>` and stores the four scalars *verbatim* (low 32 bits = number, bit 32 = presence) into a 0x40-byte `EnumMap` element, then sets the EnumMap presence bit for that core. The scalars are not folded into `compiler_reserved` — they persist in the element padding.
- **The accessor:** `GetSpecialPurposeSyncFlags(core)` @`0x20afcf40` returns `NULL` if the per-core presence bit (`chip+0x360`) is clear, `ud1`-traps if `core >= 3`, else returns `chip + 0x2a0 + (core<<6)` (element stride `0x40`). The TensorCore entry is mandatory: `Target::Init` dies via `DieBecauseNull` if it is absent.
- **The overlay semantics:** each named scalar is a *single SFLAG number*, presence-gated, copied into a `Target`/`SparseCoreTarget` field on demand. The named-scalar path is **SparseCore-only**; on the TC only `sequencer_overlay` (→ `Target+0x534`) is consumed. The four scalars survive `EnumMap::Clear` (which frees only the `compiler_reserved` vector).

| | |
|---|---|
| **Proto carrier** | `TpuChipConfigProto.special_purpose_sync_flags` (field 13, repeated `SpecialPurposeSyncFlags`) |
| **Proto element fields** | `core_type` f1 `@msg+0x2c`; `compiler_reserved` f3 (count `@+0x1c`, data `@+0x18`/`+0x20`); `sequencer_overlay` f4 `@+0x30`; `tile_overlay` f5 `@+0x34`; `global_barrier_sflag` f6 `@+0x38`; `local_barrier_sflag` f7 `@+0x3c` |
| **FromProto sink** | `TpuChipConfig::FromProto` @`0x20aea100` (special_purpose_sync_flags loop) |
| **Runtime container** | `EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>` @`TpuChipConfig+0x2a0`, stride `0x40`, presence bitmask `@+0x360` |
| **Accessor** | `GetSpecialPurposeSyncFlags(core)` @`0x20afcf40` → `chip + 0x2a0 + (core<<6)` |
| **EnumMap teardown** | `EnumMap<…SpecialPurposeSyncFlags,3>::Clear` @`0x20b08200` (frees `compiler_reserved` vector only) |
| **TC struct sink** | `Target::Init` @`0x1d60fc20` → `Target+0x8c0`/`+0x8c4` (cr base/count−5), `+0x534` (sequencer_overlay) |
| **SC struct sink** | `SparseCoreTarget::Init` @`0x1d612b20` → `+0x1d0`/`+0x1d4` (cr base/count, no −5), `+0x1e8`/`+0x200`/`+0x204`/`+0x208` (the four scalars) |
| **Scalar semantics** | single SFLAG *index*, not a bitmask; `value@bits[31:0]`, `present@bit[32]` |

---

## 1. The proto carrier — `SpecialPurposeSyncFlags`

`SpecialPurposeSyncFlags` is a submessage of `TpuChipConfigProto`, carried as the **repeated field 13** `special_purpose_sync_flags`. The chip config carries one instance per `TpuCoreType` (kTensorCore=0, kSparseCore=1, kBarnaCore=2). Each instance bundles a *range* of SFLAG numbers reserved for the compiler with four *individual* named SFLAG numbers used by the overlay / barrier lowering.

The generated-C++ message struct offsets (recovered from the generated `_InternalSerialize` @`0x20b0f040`, which is the authoritative layout witness — `WriteInt32<ILi4>(+0x30)` … `<ILi7>(+0x3c)`) are:

| Proto field | Name | Type | Message struct offset | Role |
|---|---|---|---|---|
| f1 | `core_type` | enum `TpuCoreTypeProto` | `@+0x2c` | selects which core type this element describes |
| f3 | `compiler_reserved` | repeated int32 | count `@+0x1c`, data `@+0x18` (inline) / `@+0x20` (heap) | the per-id barrier SFLAG window (ascending contiguous) |
| f4 | `sequencer_overlay` | int32 | `@+0x30` | overlay-reserved SFLAG number (single index) |
| f5 | `tile_overlay` | int32 | `@+0x34` | tile-overlay SFLAG number (SC; single index) |
| f6 | `global_barrier_sflag` | int32 | `@+0x38` | SC global-barrier SFLAG number (single index) |
| f7 | `local_barrier_sflag` | int32 | `@+0x3c` | SC local-barrier SFLAG number (single index) |

> **NOTE —** the hasbits word sits at `@+0x10`. The `core_type` slot is `+0x2c` in the *runtime message struct* — distinct from `+0x1c`, which is the proto field-NUMBER / descriptor list offset, not the runtime struct offset. The `compiler_reserved` field-3 range, and the general SFLAG number formulas built from its `base`/`count` (`base+count+4` global, `base+count` megacore, `base+id` per-key), are documented on [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md); the per-`(codename, deployment)` literal integers are on [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md).

---

## 2. The FromProto sink — `TpuChipConfig::FromProto`

`TpuChipConfig::FromProto` @`0x20aea100` carries a dedicated `special_purpose_sync_flags` loop that, per `SpecialPurposeSyncFlags` proto element, decodes the core type, heap-copies the `compiler_reserved` range, captures the four scalars with their presence bits, and writes a 0x40-byte `EnumMap` element. The loop iterates the repeated field 13 (begin `@proto+0x98`, count `@proto+0xa0`). The byte-exact body:

```c
// TpuChipConfig::FromProto @0x20aea100 — special_purpose_sync_flags loop (one element)
for (each SpecialPurposeSyncFlags msg in proto.special_purpose_sync_flags) {  // field 13
    core      = TpuCoreTypeFromProto(*(int*)(msg + 0x2c));        // 0=TC, 1=SC, 2=BC
    cr_count  = *(int*)(msg + 0x1c);                              // compiler_reserved size
    cr_data   = (hasbit on msg+0x18) ? *(int**)(msg + 0x20)       // heap repeated field
                                     : (int*)(msg + 0x18);        // inline
    // heap copy of the compiler_reserved range
    v.data = (int*)_Znwm(cr_count * 4);
    memcpy(v.data, cr_data, cr_count * 4);
    v.size = v.cap = cr_count;

    // the four scalars, each packed (value | present<<32)
    seq_ov   = (long)*(int*)(msg + 0x30) | (seq_ov   > 0 ? 0x100000000 : 0);   // f4
    tile_ov  = (long)*(int*)(msg + 0x34) | (tile_ov  > 0 ? 0x100000000 : 0);   // f5
    glob_bar = (long)*(int*)(msg + 0x38) | (glob_bar > 0 ? 0x100000000 : 0);   // f6
    local_bar= (long)*(int*)(msg + 0x3c) | (local_bar> 0 ? 0x100000000 : 0);   // f7

    elem = chip + 0x2a0 + (core << 6);          // EnumMap element, 0x40-byte stride
    *(int*)  (elem + 0x00) = core;              // element key / runtime core marker
    *(int**) (elem + 0x08) = v.data;            // compiler_reserved vector data ptr
    *(long*) (elem + 0x10) = v.size;            // compiler_reserved size
    *(long*) (elem + 0x18) = v.cap;             // compiler_reserved cap (== size)
    *(int*)  (elem + 0x20) = tile_ov_value;     *(bool*)(elem + 0x24) = tile_ov_present;
    *(int*)  (elem + 0x28) = seq_ov_value;      *(bool*)(elem + 0x2c) = seq_ov_present;
    *(int*)  (elem + 0x30) = glob_bar_value;    *(bool*)(elem + 0x34) = glob_bar_present;
    *(int*)  (elem + 0x38) = local_bar_value;   *(bool*)(elem + 0x3c) = local_bar_present;
    set_enummap_presence_bit(chip + 0x360, core);   // bts core, *(chip+0x360)
}
```

Two properties matter for a reimplementer:

- **The scalars are stored verbatim.** The writer's `>>8` / `<<8` split + low-byte `movzbl` (visible in the raw disassembly) is a register-allocation artifact: it reconstructs the exact proto value while carrying the presence bool separately in bit 32 of the packed qword. The presence bool is computed `setg` (`value > 0`), so a zero or negative value reads as absent.
- **The element ordering is not the proto order.** Note the element stores `tile_overlay` first (`+0x20`) then `sequencer_overlay` (`+0x28`), inverting the proto field order (f4 `sequencer_overlay`, f5 `tile_overlay`). The struct sinks (§5) read the element offsets, not the proto offsets — `Target::Init` reads element `+0x28` for `sequencer_overlay`.

> **CONFIRMED (decompile) —** in the `special_purpose_sync_flags` loop of `FromProto`, the element base is `v1047 << 6` (= `core * 0x40`), and per element the four scalars are reconstructed verbatim (`>>8` / `<<8 | low-byte` register split) with the presence bool stored separately: `sequencer_overlay` presence is carried as `v706 = v705 + 0x100000000` (= `*(int*)(msg+0x30)`, bit 32 = `value > 0`); `tile_overlay`/`global_barrier`/`local_barrier` presence are the byte `setg` flags `v710`/`v711`/`v714` (= `*(int*)(msg+0x34/0x38/0x3c) > 0`) written at element `+0x24`/`+0x34`/`+0x3c`. The readers (`Target::Init` / `SparseCoreTarget::Init`) test `& 0x100000000` on the packed qword. The repeated-field copy is `operator new(4*count)` + `memcpy`. This matches the proto→element→struct chain exactly.

---

## 3. The runtime container — `EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>`

The three per-core elements live in a fixed-capacity `EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>` embedded directly inside `TpuChipConfig` at offset `+0x2a0`. The map is keyed by `TpuCoreType` (the enum value *is* the element index), with a separate per-core presence bitmask at `+0x360`. It is a fixed array, not a hash map: three 0x40-byte slots at `+0x2a0`, `+0x2e0`, `+0x320`.

### 3.1 The element layout (0x40 bytes)

| Element offset | Content | Source |
|---|---|---|
| `+0x00` | element key / runtime core (0/1/2) | `core` |
| `+0x08` | `compiler_reserved` vector data ptr | `_Znwm` result |
| `+0x10` | `compiler_reserved` size | `cr_count` |
| `+0x18` | `compiler_reserved` cap (== size) | `cr_count` |
| `+0x20` | `tile_overlay` (int32) | proto f5 `@msg+0x34` |
| `+0x24` | `tile_overlay` present (bool) | `setg (value > 0)` |
| `+0x28` | `sequencer_overlay` (int32) | proto f4 `@msg+0x30` |
| `+0x2c` | `sequencer_overlay` present (bool) | bit-32 packing |
| `+0x30` | `global_barrier_sflag` (int32) | proto f6 `@msg+0x38` |
| `+0x34` | `global_barrier_sflag` present (bool) | `setg` |
| `+0x38` | `local_barrier_sflag` (int32) | proto f7 `@msg+0x3c` |
| `+0x3c` | `local_barrier_sflag` present (bool) | `setg` |

### 3.2 Teardown — `EnumMap::Clear` frees only the vector

`EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>::Clear` @`0x20b08200` walks the three elements (`+0x0`/`+0x40`/`+0x80`), and for each whose presence bit (`+0xc0` in the map) is set, frees the `compiler_reserved` vector (`begin@+0x8`, `cap@+0x18 << 2` bytes) and zeros the size at `+0x10`. **It never touches `+0x20..+0x3c`.** Consequence: the four named scalars persist in the element padding — they are stored at `FromProto` time and read on demand by `Target::Init` / `SparseCoreTarget::Init`, not consumed and discarded.

> **NOTE —** this is why the four scalars are described as living "in the element" rather than "in a parsed result struct". The `compiler_reserved` vector is the only heap allocation the element owns; everything else is plain-old-data in the 0x40-byte slot.

---

## 4. The accessor — `GetSpecialPurposeSyncFlags(coreType)`

`GetSpecialPurposeSyncFlags(core)` @`0x20afcf40` is the single retrieval path for a `SpecialPurposeSyncFlags` element. It is a presence-gated, bounds-checked address computation over the `EnumMap` at `chip+0x2a0`:

```c
// TpuChipConfig::GetSpecialPurposeSyncFlags(TpuCoreType core) @0x20afcf40
SpecialPurposeSyncFlags* GetSpecialPurposeSyncFlags(TpuChipConfig* chip, TpuCoreType core) {
    uint64_t mask = *(uint64_t*)(chip + 0x360);     // per-core presence bitmask
    if (!_bittest64(&mask, core)) return NULL;       // bt core, mask; jae → NULL
    if ((unsigned)core >= 3) ud1();                  // CHECK core ∈ {0,1,2}
    return chip + 0x2a0 + ((uint64_t)core << 6);     // element `core`, 0x40-byte stride
}
```

The contract a reimplementer must honour:

- **Presence is checked first, then bounds.** If the chip config carried no `SpecialPurposeSyncFlags` for `core`, the accessor returns `NULL` *before* the `core < 3` check. A caller that does not check the return value for `NULL` will dereference null garbage.
- **The index is `core << 6`, not `+core`.** The element stride is `0x40` (the EnumMap element size); a reimplementation that indexes `+0x2a0 + core` reads into the TensorCore element's `compiler_reserved` pointer for `core=1` and into its scalar padding for `core=2` — garbage for `kSparseCore` / `kBarnaCore`.
- **The TensorCore entry is mandatory.** `Target::Init` calls the accessor for `kTensorCore` and, on `NULL`, dies via `DieBecauseNull` with the string `"chip_config.GetSpecialPurposeSyncFlags( ::tpu::TpuCoreType::kTensorCore)"`. Every valid chip config must carry a TC special-purpose entry.

> **CONFIRMED (decompile) —** the accessor body is exactly `v2 = *(chip+864); if (!_bittest64(&v2, core)) return 0; if ((unsigned)core >= 3) ud1; return chip + 672 + (core << 6)` — i.e. `+0x360` bitmask, `+0x2a0` base (`672 = 0x2a0`), `core << 6` stride. The six callers of this accessor in the build are `Target::Init` (×2), `SparseCoreTarget::Init` (×2), `TpuPxcDriver::InitializeCores`, and `TpuProfilerControlListener::CanStartProfiler`.

This accessor is also summarised on [overview](overview.md) §5.1; this page is the authoritative derivation.

---

## 5. The overlay semantics — the four named scalars

The four named scalars (`sequencer_overlay`, `tile_overlay`, `global_barrier_sflag`, `local_barrier_sflag`) are each a **single SFLAG number** (an index into the SFLAG MemorySpace), not a bitmask. They overlay the general SFLAG window — i.e. they reserve specific SFLAG indices outside the `compiler_reserved` per-id range, pinned to the top of the per-gen SFLAG address space. After `FromProto` stores them into the EnumMap element, `Target::Init` / `SparseCoreTarget::Init` copy the present ones into named `Target`/`SparseCoreTarget` fields, each guarded by the bit-32 presence test.

### 5.1 The presence-gated qword

Each scalar occupies one 8-byte element qword: `bits[31:0]` = the SFLAG number, `bit[32]` (mask `0x100000000`) = the proto-presence bool. The reader tests bit 32 (`test $0x100000000, qword` or `bt $0x20, qword`) and stores the low 32 bits only if present:

```c
// SparseCoreTarget::Init @0x1d612b20 — scalar store (representative; one of four)
v74 = *(uint64_t*)(elem + 0x30);            // global_barrier_sflag (packed)
if (v74 & 0x100000000)                       // bit-32 presence gate
    *(int*)(sparse_core_target + 0x204) = (int)v74;   // store low 32 bits = the SFLAG number
```

### 5.2 The struct sinks and consumers

`Target::Init` copies only `sequencer_overlay` into a named TC field; `SparseCoreTarget::Init` copies all four into SC fields. The TC `tile_overlay`/`global_barrier_sflag`/`local_barrier_sflag` scalars are **never read** for the TC — the named-scalar barrier/overlay path is SparseCore-only. (The TC global/megacore/all-reduce barriers come from `compiler_reserved` via `base+count+{0,2,3,4}` — see [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md).)

| Scalar (proto field) | Element off | Presence gate | TC sink | SC sink | Consumer |
|---|---|---|---|---|---|
| `sequencer_overlay` (f4) | `+0x28` | bit 32 | `Target+0x534` | `SparseCoreTarget+0x200` | TC: `GetOverlayReservedSyncFlagNumber` → overlay-reserved SFLAG (single index). SC: **none** (stored, never read in this build) |
| `tile_overlay` (f5) | `+0x20` | bit 32 | (not copied) | `SparseCoreTarget+0x1e8` | SC: `overlayer::OverlayProgram` encodes it as a `SyImm32` MC immediate; also read by `EmitFinishDescriptorDma`/`EmitEmulatedContinuation` |
| `global_barrier_sflag` (f6) | `+0x30` | bit 32 | (not copied) | `SparseCoreTarget+0x204` | SC: `LoweringEmitter::Emit` / `CustomKernelEmitter::MaybeInsertGlobalBarrier` (`mlir::sparse_core::MemorySpaceAttr::get(ctx, 14)` global-barrier SFLAG MemRef — SC MLIR enum sflag-band value 14, **not** the jellyfish `MemorySpace` enum) |
| `local_barrier_sflag` (f7) | `+0x38` | bit 32 | (not copied) | `SparseCoreTarget+0x208` | SC: `lowering_util::ReservedLocalBarrierSflag` (1-elt i32 MemRef, `mlir::sparse_core::MemorySpaceAttr::get(ctx, 5)` — SC MLIR enum sflag-band value 5, **not** jellyfish `MemorySpace`) |
| `compiler_reserved` (f3) | `+0x08`/`+0x10` | (vector, always) | `Target+0x8c0`/`+0x8c4` (count = size **−5**) | `SparseCoreTarget+0x1d0`/`+0x1d4` (count = size, **no −5**) | per-id barrier window — see [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) |

### 5.3 Why "overlay" is an index, not a bitmask

`sequencer_overlay` lands at `Target+0x534` and is returned by `GetOverlayReservedSyncFlagNumber()` @`0x1d617900` (`mov 0x534(rdi), eax; ret`). All four of its consumers treat it as one int SFLAG number, never AND/test it:

- `EmitContinuationTailcall` @`0x12718ca0`: number → `SflagImmPtr(int, "overlay reserved sync flag")` → a single SFLAG pointer used as a DMA completion sflag.
- `LinkAndFinishProgram` @`0x10a25a20`: inserts `{number → "overlay"}` into a `std::map<int, string>` (next to `{GetGlobalBarrierSyncFlagNumber → "global barrier"}`) — a single map key.
- `CodeGenerationHelper::emit_routine<1>` / `<3>` @`0x1409a5a0` / `0x14062da0`: number → `linked_hash_map<int, BundleImmediatesMetadata>` key.

The per-gen values follow a `2^n − 1` progression — `254` (0xFE, JF/DF), `511` (0x1FF, PF/VF/GL), `4095` (0xFFF, GF) — **not** because it is a bitmask, but because the overlay-reserved SFLAG is pinned to the topmost addressable SFLAG index, and the SFLAG-number encoding width grows per generation (8→9→12 bits; gen ISA `SyncFlagCountType`). The SC `tile_overlay`/`sequencer_overlay` (7167/7157) likewise land as single SFLAG indices, materialised as 1-element i32 MemRefs in MemorySpace::sflag.

> **LOW —** the per-gen SFLAG-number bit-width (8/9/12) is attributed from the gen ISA `SyncFlagCountType` plus the `2^n − 1` overlay-number progression; it is not read as a single binary literal in one accessor. The *index-not-bitmask* conclusion is CONFIRMED (no consumer masks the value); the *width* is inferred. The literal scalar values per gen are an embedded-memfile dependency (see [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md)).

> **QUIRK —** the SC `sequencer_overlay` (`SparseCoreTarget+0x200`, value 7157) is *stored* by `SparseCoreTarget::Init` but has **no reader** anywhere in `.text` in this build — it is ingested-but-unused. The SC overlayer reads `tile_overlay` (`+0x1e8`), not `sequencer_overlay`. Whether this is a retired field, a forward-declared field for a future generation, or a flag-disabled path is not determinable from the binary. A reimplementer should still ingest it (the proto carries it) but need not wire a consumer.

---

## 6. The full chip-config → element → struct → consumer chain

The complete data path from proto to consumer, byte-anchored end-to-end:

```text
TpuChipConfigProto.special_purpose_sync_flags[core]   (field 13, repeated SpecialPurposeSyncFlags)
        │   core_type f1 @+0x2c ; compiler_reserved f3 ; seq_ov f4 ; tile_ov f5 ; glob f6 ; local f7
        │
  FromProto @0x20aea100  — per element:
        │   _Znwm + memcpy(compiler_reserved) ; pack 4 scalars (value | present<<32)
        ▼
EnumMap<TpuCoreType, SpecialPurposeSyncFlags, 3>  @TpuChipConfig+0x2a0  (stride 0x40, bitmask +0x360)
        │   element: +0x08/+0x10/+0x18 cr vector ; +0x20/+0x28/+0x30/+0x38 scalars (+presence)
        │   Clear @0x20b08200 frees only the cr vector → scalars persist
        │
  GetSpecialPurposeSyncFlags(core) @0x20afcf40  — presence-gated, bounds-checked, +0x2a0+(core<<6)
        │
        ├── Target::Init @0x1d60fc20  (core=TC, NULL → DieBecauseNull)
        │     element +0x10 size / +0x08 data → contiguity-check → Target+0x8c0 base, +0x8c4 count−5
        │     element +0x28 sequencer_overlay (if present) → Target+0x534 = GetOverlayReservedSyncFlagNumber
        │       → EmitContinuationTailcall / LinkAndFinishProgram / emit_routine<1,3>  (single index)
        │
        ├── SparseCoreTarget::Init @0x1d612b20  (core=SC)
        │     element +0x10 size / +0x08 data → SparseCoreTarget+0x1d0 base, +0x1d4 count (no −5)
        │     +0x20 tile_overlay → +0x1e8   (→ overlayer::OverlayProgram SyImm32)
        │     +0x28 sequencer_overlay → +0x200   (stored, NO reader)
        │     +0x30 global_barrier_sflag → +0x204 (→ MaybeInsertGlobalBarrier, SC MLIR MemSpace 14)
        │     +0x38 local_barrier_sflag → +0x208  (→ ReservedLocalBarrierSflag, SC MLIR MemSpace 5)
        │
        ├── TpuPxcDriver::InitializeCores @0xe806500  (reads only the cr vector +0x08/+0x10)
        └── TpuProfilerControlListener::CanStartProfiler @0xf3328c0  (profiler gate)
```

The `compiler_reserved` carve (the `−5` reservation on TC, the SC full-range, and the per-id / global / megacore / all-reduce SFLAG formulas built from `base`/`count`) is the subject of [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md). This page stops at the field stores; it does not re-derive the number formulas.

---

## 7. Verification notes

> Byte-exact in `libtpu.so` v0.0.40:
> - `GetSpecialPurposeSyncFlags` @`0x20afcf40`: `v2 = *(chip+864)`; `if (!_bittest64(&v2, core)) return 0`; `if ((unsigned)core >= 3) ud1`; `return chip + 672 + (core<<6)` — i.e. `+0x360` bitmask, `+0x2a0` base, `core<<6` stride — byte-exact.
> - `TpuChipConfig::FromProto` @`0x20aea100`: in the `special_purpose_sync_flags` loop the element base is `core << 6`; the four scalars are stored verbatim (`>>8`/`<<8|low-byte` reconstruction), `sequencer_overlay` presence carried as `v705 + 0x100000000` and the other three as `setg (value > 0)` bytes at element `+0x24`/`+0x34`/`+0x3c`; `compiler_reserved` via `operator new(4*count)` + `memcpy` — confirms the element build and the (value, present) packing.
> - `Target::Init` @`0x1d60fc20`: `*((_DWORD*)target + 560) = *base` (= `Target+0x8c0` base); `*((_DWORD*)target + 561) = size − 5` (= `Target+0x8c4` count); sequencer_overlay gated by `& 0x100000000` at element `+0x28`; `DieBecauseNull("…GetSpecialPurposeSyncFlags( ::tpu::TpuCoreType::kTensorCore)")` on `NULL` — exact.
> - `SparseCoreTarget::Init` @`0x1d612b20`: `+464`/`+468` = `+0x1d0`/`+0x1d4` (base / count, no −5); the four scalar stores `+488`/`+512`/`+516`/`+520` (= `+0x1e8`/`+0x200`/`+0x204`/`+0x208`), each gated by `& 0x100000000` — exact.
>
> **[LOW]** The literal per-gen `compiler_reserved` integers and the four scalar values (e.g. `sequencer_overlay = 254/511/4095`, SC `global_barrier_sflag = 7156`, `local_barrier_sflag = 7155`) are runtime-resolved from embedded chip-config memfile binarypb blobs and were not statically extracted here. The per-gen SFLAG-number bit-width (8/9/12) is inferred from the `2^n − 1` overlay progression + the gen ISA `SyncFlagCountType`, not read as a single literal. The *index-not-bitmask* overlay semantics, the FromProto sink, the accessor, the EnumMap layout, and the struct sinks are CONFIRMED byte-anchored.

---

## Cross-References

**Barrier algorithms (this section)**
- [overview](overview.md) — the barrier subsystem map; §5.1 summarises the `GetSpecialPurposeSyncFlags` accessor (this page is the authoritative derivation)
- [Barrier-to-SFLAG Binding](barrier-to-sflag-binding.md) — the general SFLAG number formulas built from `compiler_reserved` `base`/`count` (`base+count+4` global, `base+count` megacore, `base+id` per-key)
- [Per-Codename Compiler-Reserved](per-codename-compiler-reserved.md) — the literal per-`(codename, deployment)` `compiler_reserved` integers and scalar values (memfile-resolved)
- [Infer Barrier Config](infer-barrier-config.md) — the pincer-fusion `CUSTOM → GLOBAL/REPLICA` normaliser that consumes the carved TC `count`
- [Global-Barrier Window](global-barrier-window.md) — the `base+count+4` global SFLAG slot and the per-core barrier window

**Sibling subsystems**
- [SFLAG Sync-Flag Tier](../memory/sflag-protocol.md) — the SFLAG atomic-counter substrate every barrier (and overlay reservation) is built on
- [back to index](../index.md)
