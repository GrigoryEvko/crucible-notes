# TpuTopology Struct (Target+0x3b8)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

`tpu::TpuTopology` is the chip-geometry and device-mesh descriptor that every `xla::jellyfish::Target` holds at `Target+0x3b8`. It is the single object that answers "how big is one TPU chip, and how are the chips arranged into a slice": the lane/sublane MXU-tile geometry, the per-core-type core counts, the X/Y/Z chip torus extents, and the host/chip mesh products. The XLA-for-TPU backend reads it for every tiling, cost-model, and memory-space decision; the C-API runtime reads the same fields through a parallel set of `TpuTopology_*` wrappers.

The struct is built once, in its constructor at `0x20acee60` (~309 decompiled lines), from a `shared_ptr<TpuChipParts>` plus two `TpuDimensions` triples (host bounds and chips-per-host). The constructor multiplies the two dimension triples into the combined chip bounds, queries `chip_parts` for the per-core-type counts, and then walks `chip_parts->CoreParts(TENSOR_CORE)->SequencerParts(...)->vector_isa()` to fill the geometry block at `+0x198..+0x1b0`. If that VectorIsa chain is absent, the constructor falls back to a hard-coded **128 lanes × 8 sublanes** — so 128×8 is the build-in default for any generation. The object is exactly `0x3c8` bytes (`operator new(0x3C8u)` at every construction site) and ends with its own HalLocations vector at `+0x3b8..+0x3c8` (a numeric coincidence: `TpuTopology+0x3b8` is the object's own HalLocations count word, unrelated to the `Target+0x3b8` pointer that names this struct).

`Target::Init` (`0x1d60fc20`) installs the pointer: `*((_QWORD *)target + 119) = topology` puts the `TpuTopology*` at `Target+0x3b8`, and `*((_QWORD *)target + 297) = sparsecore` puts the SparseCore sub-descriptor at `Target+0x948`. The adjacent `Target+0x3c0` slot (`*((_QWORD *)target + 120)`) is **not** a second topology — `Target::Init` stores the incoming `unique_ptr<CpuTopology>` there and the destructor frees it through a `TargetMachineOptions` member, so it is the host-CPU topology, unrelated to the chip geometry. The rest of this page is the byte-exact field layout, the accessors that read each field, and the constructor's geometry-population chain — all read directly from the decompiled constructor and accessor bodies.

For reimplementation, the contract is:

- The field layout `+0x00..+0x3c8`: scalar mesh dims, per-core-type counts, the `+0x198..+0x1b0` MXU-tile geometry block, and the location-vector tails.
- The geometry-population chain: how lane/sublane/granules are derived from `chip_parts` VectorIsa, and the 128×8 fallback.
- The accessor surface: which `Target::` and `TpuTopology_*` methods dereference which offset, so a reimplementation exposes the same scalar contract.

| | |
|---|---|
| **Struct** | `tpu::TpuTopology` |
| **Constructor** | `0x20acee60` (~309 lines), `_ZN3tpu11TpuTopologyC1...` |
| **Destructor** | `0xe6b0080` |
| **`sizeof`** | `0x3c8` bytes (exact — `operator new(0x3C8u)` at every construction site, e.g. `TpuTopologySerdes::Construct` `0x20805ee0`, `TpuTopology::Subslice` `0x20ad20a0`) |
| **Held by** | `xla::jellyfish::Target+0x3b8` (the only `TpuTopology*`; `Target+0x3c0` holds `CpuTopology`, not a topology) |
| **Installed in** | `Target::Init` `0x1d60fc20` (`*((_QWORD*)target+119) = topology`) |
| **Geometry block** | `+0x198` lane, `+0x1a0` sublane, `+0x1a8` lane·sublane, `+0x1b0` chunk-granules |
| **Geometry source** | `chip_parts→CoreParts(TENSOR_CORE)→SequencerParts→vector_isa()`; fallback 128×8 |

---

## Field Layout

The table is the complete `tpu::TpuTopology` layout. Every offset was read byte-exact from the constructor store site (decimal offsets in the decompile converted to hex here) or the matching accessor body. Types: `i32`/`i64` scalar, `u8` bool, `ptr` pointer / `shared_ptr` control word, `vec`/`loc` libc++ inline-vector or location-array (a begin/end/cap triple or a count+pointer pair).

| Field | Offset | Type | Meaning |
|---|---:|---|---|
| `platform_type` | `+0x00` | i32 | `TpuPlatformType` (ctor `*(_DWORD*)a1 = a2`) |
| `chip_parts` (ctrl) | `+0x08` | ptr | `shared_ptr<const TpuChipParts>` — control word; geometry + `Version` read through this |
| `chip_parts` (refcnt) | `+0x10` | ptr | `shared_ptr<const TpuChipParts>` refcount block |
| `chip_config` (ctrl) | `+0x18` | ptr | `shared_ptr<const TpuChipConfig>` — Megacore / logical-device gating |
| `chip_config` (refcnt) | `+0x20` | ptr | `shared_ptr<const TpuChipConfig>` refcount block |
| `flags` | `+0x28` | i64 | `long` flags arg (ctor `*(_QWORD*)(a1+40) = a7`) |
| `chips_per_host.x/.y/.z` | `+0x30/+0x34/+0x38` | i32×3 | per-host chip-mesh dims (ctor `vmovups` of first `TpuDimensions` arg; ctor's own assert names it `chips_per_host_bounds()`) |
| `chips_per_host.w` | `+0x40` | i32 | 4th chip-mesh dim |
| `host_bounds.x/.y/.z` | `+0x44/+0x48/+0x4c` | i32×3 | host-mesh dims (ctor `vmovups` of second `TpuDimensions` arg; ctor's own assert names it `host_bounds()`) |
| `host_bounds.w` / `using_tensornode` | `+0x54` | i32/u8 | 4th host dim; low byte read by `UsingTensorNode` (`[+0x54]`) |
| **`ChipBounds_X`** | `+0x58` | i32 | `chips_per_host.x · host_bounds.x` (ctor `v22*v24`) |
| **`ChipBounds_Y`** | `+0x5c` | i32 | `chips_per_host.y · host_bounds.y` |
| **`ChipBounds_Z`** | `+0x60` | i32 | `chips_per_host.z · host_bounds.z` |
| `wrap.x` / `wrap.y` | `+0x64/+0x68` | u8×2 | torus-wrap bytes, ctor-init 0 |
| **`HostCount`** | `+0x6c` | i32 | ∏ host-bound dims (ctor `v29*v24*v25`) |
| `chips_product` | `+0x70` | i32 | ∏ combined chip-bound dims = `ChipBounds_X·Y·Z` (ctor `v31 = v30*v26*v27`); the `chips_product` multiplier for all per-core-type counts |
| **`ChipsPerHost`** | `+0x74` | i32 | ∏ chips-per-host dims (ctor `v28*v22*v23`) |
| `total_cores` | `+0x78` | i32 | `chips_product · CoreCount()` (all core types) |
| **`TENSOR_CORE` count/chip** | `+0x7c` | i32 | `CoreCount(chip_parts, 0)`; base of `CoresPerChip(t)` |
| `TENSOR_CORE · chips` | `+0x80` | i64 | `[+0x7c] · chips_product` (8-byte store) |
| **`SPARSE_CORE` count/chip** | `+0x88` | i32 | `CoreCount(chip_parts, 1)`; `CoresPerChip(1)` |
| `SPARSE_CORE · chips` | `+0x8c` | i32 | `[+0x88] · chips_product` |
| `TENSOR_CORE · chips` (dup) | `+0x90` | i32 | duplicate of the TC·chips product |
| **`BARNA_CORE` count/chip** | `+0x94` | i32 | `CoreCount(chip_parts, 2)`; `CoresPerChip(2)` |
| `core[2] · chips` | `+0x98` | i32 | `CoreCount(...,2) · chips_product`; `SupportsSparseCore` tests `>0` (see note) |
| `(TC+SC) · chips` | `+0x9c` | i32 | sum of the TC·chips and SC·chips products |
| `wrap_proto_lo16` / `wrap_proto_b16` | `+0xa0/+0xa2` | u16/u8 | low bits of the `TpuWrapProto`/`TpuWrapTag` arg (ctor `*(_WORD*)(_RBX+160)=a14`, `*(_BYTE*)(+162)=BYTE2(a14)`); `+0xa0 & 0x101` feeds the `Topology` wrap ctor — distinct from the `flags` long at `+0x28` |
| `twisted_bool` | `+0xa3` | u8 | trailing `bool` ctor arg (`a15`); selects `TwistedTorusTopology` (`new 0x138`) vs `Topology` (`new 0x58`) below |
| `topology_kind` | `+0xa4` | i32 | TwistedTorus-vs-Topology validity selector |
| `topology` | `+0xa8` | ptr | `slice_builder::Topology*` (`new 0x58`) or `TwistedTorusTopology*` (`new 0x138`) |
| `HostLocations` | `+0xb0` | loc | `MakeHostLocations` (`0x20acf5c0`) |
| `ChipLocations` | `+0xc8` | loc | `MakeChipLocations` (`0x20acf800`) |
| `CoreLocations` (primary) | `+0xe0` | ptr | `MakeCoreLocations`; `cores()` base, element stride `0x38` |
| `CoreLocations` (megacore) | `+0xf8` | loc | second `MakeCoreLocations`; `logical_devices` (`0x20ad38c0`) returns `[+0xf8]` when `TpuChipConfig::Megacore`, else the primary `[+0xe0]`+stride |
| `SharedMemoryLocations` | `+0x110` | loc | `MakeSharedMemoryLocations` (`0x20ad02c0`) |
| `MemoryLocations` | `+0x128` | loc | `MakeMemoryLocations` (`0x20ad08e0`) |
| `StandardFactoryInfo` | `+0x140` | blob | optional 0x14-byte block; ctor-init 0 |
| `subslice` dims | `+0x158/+0x15c/+0x160` + `+0x16c/+0x170/+0x174` | i32×6 | subslice chip-bound / extent fields; `GetFullSliceDeviceCount` multiplies exactly these six |
| `subslice_valid` | `+0x184` | u8 | subslice-valid flag (`GetFullSliceDeviceCount` `cmpb`) |
| `has_subslice` | `+0x190` | u8 | ctor `movb $0/$1`; gates the subslice path |
| **`lane_count`** | `+0x198` | i64 | VectorIsa lane count; `Target::LaneCount` = `[0x3b8]->[0x198]` |
| **`sublane_count`** | `+0x1a0` | i64 | VectorIsa sublane count; `Target::SublaneCount` = `[0x3b8]->[0x1a0]` |
| **`lane·sublane`** | `+0x1a8` | i64 | MXU-tile element count (ctor `imul`); feeds `ChunkSizeBytes` |
| **`chunk_granules`** | `+0x1b0` | i64 | derived (see [Geometry Population](#geometry-population)); `version<2 ? computed : 32` |
| `HalLocations` | `+0x1b8` | loc | `MakeHalLocations` (`0x20ad0de0`), gated `popcount(granules)<2` |
| `ChipViewLocations` | `+0x2b8/+0x2c0` | vec | count `[+0x2b8]` / heap ptr `[+0x2c0]`, element stride `0x20` (dtor `<9` gate then `×32`); built by `MakeChipViewLocations` (`0x20ad1080`, writing from `+0x2c8`) |
| `HalLocations` tail | `+0x3b8/+0x3c0` | vec | object's OWN HalLocations: count `[+0x3b8]` / heap ptr `[+0x3c0]` (dtor `cmp $6,[0x3b8]` then `free([+0x3c0])`); the last 16 bytes of the `0x3c8`-byte object |

> **GOTCHA —** the numeric offset `0x3b8` appears in both objects with unrelated meanings. In `xla::jellyfish::Target`, `+0x3b8` is the `tpu::TpuTopology*` member. Inside `tpu::TpuTopology` itself (a `0x3c8`-byte object), `+0x3b8` is the object's own HalLocations count word (its last vector's tail). Every `[0x3b8]->[X]` on this page means "dereference the `Target`'s `TpuTopology*`, then read field X". A reimplementer who conflates the two reads garbage.

`Target::CoresPerChip(t)` (`0x1d615b40`) returns `[0x3b8]->[0x7c + 12·t]`, i.e. the per-chip count at `+0x7c` for `t=0` (TENSOR_CORE), `+0x88` for `t=1` (SPARSE_CORE), `+0x94` for `t=2` (BARNA_CORE) — and `BUG()`s for `t≥3`. The `+0x80..+0x9c` neighbours are the corresponding *·chips* products. `Target::SupportsSparseCore` (`0x1d48fd40`) reads `[0x3b8]->[0x98] > 0`, but the constructor stores `[+0x98] = CoreCount(chip_parts, 2) · chips_product` — the type-index-2 (BARNA-slot) product, not the SPARSE_CORE one.

> **NOTE —** whether index 2 is the SparseCore slot in the runtime `TpuCoreType` enum (distinct from the proto `BARNA_CORE` ordering) is not independently confirmed: the literal store at `+0x98` is `CoreCount(chip_parts, 2) · chips`, so the SparseCore-vs-Barna label on that field is MEDIUM confidence.

---

## Geometry Population

The lane/sublane/granule block at `+0x198..+0x1b0` is filled near the end of the constructor (decompile lines 230–268). The path is: gate on the TENSOR_CORE·chips count being non-zero, fetch the TensorCore's `CoreParts → SequencerParts(0) → vector_isa()`, check the VectorIsa `has_vector_isa` byte at `+0x18`, and copy `vector_isa[0]` (lane) and `vector_isa[+0x04]` (sublane). If any link in that chain is missing, the constructor uses the hard-coded **128 × 8** fallback.

### Algorithm

```c
function PopulateGeometry(this, chip_parts):           // ctor 0x20acee60, lines 230-268
    if this->[0x80] /*TENSOR_CORE · chips*/ == 0:       // line 230
        goto fallback
    parts = chip_parts->CoreParts(0 /*TENSOR_CORE*/)    // line 233, sub_20b1e840
    if parts == NULL: goto fallback
    seq = parts->SequencerParts(0 /*TC_SEQ*/)           // line 236, sub_20b2aa60
    if seq == NULL: goto fallback
    vi = seq->vector_isa()                              // line 237, = seq+0x1c (sub_20b31840)
    if vi->[0x18] /*has_vector_isa*/ != 1: goto fallback
    if vi->[0x18] == 0: BUG()                           // line 240-241, FATAL double-check
    lane    = (i64)(i32)vi->[0x00]                       // line 242
    sublane = (i64)(i32)vi->[0x04]                       // line 244
    goto store

fallback:                                                // lines 248-251 (LABEL_40)
    lane    = 128                                        // movq $0x80
    sublane = 8

store:
    this->[0x198] = lane                                 // line 243/249
    this->[0x1a0] = sublane                              // line 253
    this->[0x1a8] = lane * sublane                       // line 254-255  (MXU-tile elems)

    // chunk_granules (tc_max_packing_factor):
    numer   = 4 * (lane * sublane)                       // line 256, bytes
    divisor = 4 * lane                                   // line 258
    cpc     = chip_parts->[0xc8]                          // line 259
    if cpc > divisor: divisor = cpc                       // line 259-260  (MAX, not min)
    q       = numer / divisor                             // line 261-264
    this->[0x1b0] = (chip_parts->[0] < 2 /*version<2*/) ? q : 32   // lines 265-268
    CHECK(this->[0x1b0] > 0 && IsPowerOfTwo(this->[0x1b0]))         // lines 269-274
```

> **NOTE —** the `chunk_granules` divisor is `max(4·lane, chip_parts[+0xc8])` — the *larger* of `4·lane` and the chip-parts field — with the dividend `4·(lane·sublane)`. The decompile (`if (*(_QWORD*)(v59+200) > v60) v60 = *(_QWORD*)(v59+200)`) is a `max` over `lane·4`, not a `min` over `sublane·4`. The result is force-checked to be a positive power of two (`IsPowerOfTwo(result.tc_max_packing_factor)`, FATAL at source line 129); for any generation reporting `chip_parts.version >= 2` the stored value is simply `0x20` (32).

> **NOTE —** the fallback writes `lane=0x80, sublane=8`, so a `TpuTopology` built from a chip-parts blob that lacks a populated VectorIsa still presents 128×8 geometry. For the v7 (`6acc60406`) chip-parts embedded in this wheel the VectorIsa is present and also reports `lane=128, sublane=8`, so the populated and fallback paths agree on this build. The `lane·sublane = 1024` product and `chunk_granules = 32` follow.

### Source Chain

```text
TpuTopology ctor (0x20acee60)
  └─ chip_parts->CoreParts(TENSOR_CORE)          0x20b1e840
       └─ TpuCoreParts::SequencerParts(TC_SEQ)   0x20b2aa60
            └─ TpuSequencerParts::vector_isa()    0x20b31840  (= this+0x1c)
                 ├─ [+0x00] lane_count    → TpuTopology+0x198
                 ├─ [+0x04] sublane_count → TpuTopology+0x1a0
                 └─ [+0x18] has_vector_isa (gate; FATAL if 0 after the outer test)
```

---

## Accessors

Two parallel surfaces read these fields: the `xla::jellyfish::Target` methods (used by the compiler), which dereference `Target+0x3b8` first, and the `TpuTopology_*` C-API wrappers (used by the runtime), which take a `TpuTopology*` directly. Both were read byte-exact; the offsets match.

### Target accessors (read `[0x3b8]->[X]`)

`*((_QWORD*)target + 119)` is `target + 0x3b8`, the `TpuTopology*`. Each accessor dereferences it and reads field X.

| Accessor | VA | Reads | Returns |
|---|---|---|---|
| `Target::LaneCount` | `0x1d60f400` | `[0x3b8]->[0x198]` | i64 lane count |
| `Target::SublaneCount` | `0x1d60f300` | `[0x3b8]->[0x1a0]` | i64 sublane count |
| `Target::ChunksPerTile` | `0x1d60f2c0` | `[0x198] / [0x1a0]` | lane/sublane (16 for 128/8) |
| `Target::TileBytes` | `0x1d615bc0` | `4 · [0x198] · [0x198]` | lane²·4 bytes (65,536 for lane 128) |
| `Target::ChunkSizeBytes` | `0x1d617100` | `4 · (i32)[0x1a8]` | lane·sublane·4 bytes (4096 for 1024) |
| `Target::ChunkGranules` | `0x1d61a440` | `(4·[0x1a8]) / vtable->GranuleBytes()` | tile chunks per granule |
| `Target::LaneCountLog2` | `0x1d615be0` | `bsr (i32)[0x198]` | log2(lane) = 7 for 128 |
| `Target::SublaneCountLog2` | `0x1d615c40` | `bsr (i32)[0x1a0]` | log2(sublane) = 3 for 8 |
| `Target::CoresPerChip(t)` | `0x1d615b40` | `[0x3b8]->[0x7c + 12·t]` | per-coretype count; `BUG()` if `t≥3` |
| `Target::SupportsSparseCore` | `0x1d48fd40` | `[0x3b8]->[0x98] > 0` | bool (`+0x98` = `CoreCount(chip_parts,2)·chips`; SparseCore-vs-Barna label MEDIUM) |
| `Target::HbmCountPerChip` | `0x1d616080` | `chip_parts->SharedMemoryCount([0x3b8]+8, 0)` | HBM stacks; FATAL if `[0x3b8]` null |

> **NOTE —** `ChunkSizeBytes` reads `[0x1a8]` as a 32-bit value (`4 * *(_DWORD*)(... + 424)`), whereas the field is stored as a 64-bit `lane·sublane` product. For any realistic geometry the product fits in 32 bits, so this is harmless, but a reimplementation must store the product full-width (the `imul` is 64-bit) even though one consumer narrows it. `TileBytes`, by contrast, reads the lane field as the full 64-bit `_QWORD` and squares it.

### TpuTopology C-API wrappers (read `[X]` directly)

These take the `TpuTopology*` as their argument, so the offsets are the raw struct offsets (no `+0x3b8` indirection).

| Wrapper | VA | Reads | Meaning |
|---|---|---|---|
| `TpuTopology_ChipBounds_X` | `0xeabc040` | `[+0x58]` | combined chip-torus X extent |
| `TpuTopology_ChipBounds_Y` | `0xeabc060` | `[+0x5c]` | chip-torus Y extent |
| `TpuTopology_ChipBounds_Z` | `0xeabc080` | `[+0x60]` | chip-torus Z extent |
| `TpuTopology_HostCount` | `0xeabc000` | `[+0x6c]` | ∏ host-bound dims |
| `TpuTopology_ChipsPerHost` | `0xeabc020` | `[+0x74]` | ∏ chips-per-host dims |
| `TpuTopology_Version` | `0xeabc2a0` | `**(i32**)[+0x08]` | `chip_parts.version < 4 ? version+1 : 0` |
| `TpuTopology::UsingTensorNode` | `0x20ad7700` | `[+0x54]` (u8) | tensornode-vs-full-chip blob selector |
| `TpuTopology::cores(t)` | `0x20ad3880` | base `[+0xe0]` + `0x38·[+0x84+12·t]` | location span for core type t; `BUG()` if `t≥3` |

> **QUIRK —** `TpuTopology_Version` does not read a stored version field. It loads the first `i32` of the `chip_parts` blob (`**(i32**)(this+8)`) and returns `version+1` for `version<4`, else `0`. So the C-API "version" is `chip_parts.version + 1` clamped, a different numbering than the internal `tpu_version` the `Target` keeps at `Target+0x398`. A reimplementation that reports the raw internal version through this wrapper is off by one and silently zeroes anything `≥4`.

---

## Per-Codename Geometry

The geometry fields are DEFINITIVE for every generation, because this wheel embeds all nine `<name>_chip_parts.binarypb` blobs with data (each a name→data→length→md5 TOC entry in `.data.rel.ro`, blob bytes in `.rodata` `0xBDF2BA0..0xBDF38C0`; e.g. `jellyfish_chip_parts.binarypb` is 435 B at `0xBDF3700` and leads with field-1 `version=1`). Each blob carries its own absolute lane/sublane VectorIsa; the decoded values all report 128×8, which is also what the constructor's 128×8 fallback would yield, so the populated path and the fallback agree on this build. Internal `TpuVersion` is 0-based and chronological (`kJellyfish=0`, `kDragonfish=1`, `kPufferfish=2`, `kViperfish=3`, `kGhostlite=4`, `k6acc60406=5`); the external "TPU vN" axis is separate — see the [version→codename matrix](tpu-version-codename-matrix.md).

The one hard per-codename MXU differentiator recoverable from this wheel is not a `TpuTopology` field at all — it is a C++ literal in the per-codename `Target` subclass: the base `Target::MxuContractingSize` (`0x1d490060`) returns 128, while `GhostliteTarget::MxuContractingSize` (`0x1d497840`) and `MxuNoncontractingSize` (`0x1d497860`) return 256. So the systolic MXU is 128×128 on the Jellyfish-through-Viperfish classes and 256×256 on the Ghostlite-and-`6acc60406` class (external TPU v6e / TPU7x). This 256 is the systolic depth, distinct from the 128-lane width the VectorIsa reports.

| Geometry constant | Field / source | Jellyfish…Viperfish (v0–v3) | Ghostlite / `6acc60406` (v4–v5) |
|---|---|---|---|
| `lane_count` | `[0x3b8]+0x198` | 128 (fallback / chip-parts) | **128** (`6acc60406` chip-parts) |
| `sublane_count` | `[0x3b8]+0x1a0` | 8 (all gens) | **8** (`6acc60406` chip-parts) |
| `lane·sublane` | `[0x3b8]+0x1a8` | 1024 | **1024** |
| `chunk_granules` | `[0x3b8]+0x1b0` | computed (`version<2`) / 32 (`version≥2`) | **32** (version ≥ 2 → 0x20) |
| `ChunksPerTile` | `[0x198]/[0x1a0]` | 16 | **16** |
| `TileBytes` | `4·lane²` | 65,536 | **65,536** |
| `ChunkSizeBytes` | `4·lane·sublane` | 4096 | **4096** |
| MXU contracting / noncontracting | `*Target::Mxu*Size` (CODE) | 128 / 128 | **256 / 256** (Ghostlite override) |
| `TENSOR_CORE` / chip | `[0x3b8]+0x7c` | gen-dep (2 on v0–v3 std) | **1** (die) / **2** (full chip) |
| `SPARSE_CORE` / chip | `[0x3b8]+0x88` | gen-dep (BarnaCore engine on v0–v2; SparseCore from v3 onward) | **2** (die) / **4** (full chip) |
| `BARNA_CORE` / chip | `[0x3b8]+0x94` | gen-dep (2 on v0/v1, 4 on v2; 0 from v3) | **0** (none in `6acc60406` chip-parts) |

> **NOTE —** the per-chip counts at `+0x7c/+0x88/+0x94` reflect whichever chip-parts blob the runtime selected — the half-die tensornode blob or the full two-die chip blob — gated by `UsingTensorNode` (`[+0x54]`). For `6acc60406` the tensornode blob reports TC=1, SC=2, HBM=1, and the full-chip blob doubles each (TC=2, SC=4, HBM=2). The `TpuTopology` cells are not a fixed per-codename constant; they track the chosen blob.

---

## SparseCore Geometry

`TpuTopology` tracks SparseCore *counts* (`+0x88` per chip, `+0x8c` ·chips), but the SparseCore *geometry* lives in a separate sub-descriptor at `Target+0x948`, installed by `Target::Init` (`*((_QWORD*)target + 297) = sparsecore`) and built by `SparseCoreTarget::Init` (`0x1d612b20`). Its accessors dereference `*((_QWORD*)target + 297) = target + 0x948` and are guarded by the `SupportsSparseCore` vtable predicate at `vtable[+0x260]`, which FATALs ("SparseCore is not supported by this target") if the target has no SparseCore.

| Accessor | VA | Reads | `6acc60406` value |
|---|---|---|---|
| `Target::SparseCoreLaneCount` | `0xf7906e0` | `[0x948]->[0x94]` | 16 |
| `Target::SparseCoreTiles` | `0xfaafa40` | `[0x948]->[0x90]` | 16 TEC/SC |
| `Target::SparseCoreHbm4bWordSizeBytes` | `0x1320c220` | `[0x948]->[0x58]` | 4 |
| `Target::SparseCoreStreamGranuleSizeBytes` | `0x13886ee0` | `[0x948]->[0xa4]` | 4 |

The full `SparseCoreTarget` field map is a separate object documented in the [SparseCore target descriptor](sparsecore-target-descriptor.md) page; only the four fields above were walked here.

---

## Not Resolved

- **Absolute lane/sublane for v0–v4.** This wheel embeds all nine `<name>_chip_parts.binarypb` blobs (jellyfish through `6acc60406`, plus the `pufferfish_lite`/`viperfish_lite`/`6acc60406_tensornode` variants), so each generation's VectorIsa is decodable directly rather than inferred from the constructor fallback. The decoded `VectorIsa.sublane_count` is 8 on every generation in this build; the proto carries `sublane_count = 8` uniformly across jellyfish through `6acc60406` (see [Per-Codename HW Constants](per-codename-hw-constants.md)). The 128×8 fallback the constructor would supply when a VectorIsa chain is absent coincides with what every embedded blob reports.
- **The `+0x158..+0x190` subslice field semantics.** `GetFullSliceDeviceCount` multiplies `+0x158/+0x15c/+0x160/+0x16c/+0x170/+0x174` and gates on `+0x184/+0x190`, but which axis is the subslice base vs extent was not individually pinned. Marked MEDIUM in the layout table.
- **The location-element structs.** The base offsets and strides of the `+0xb0..+0x2c8` vectors are recovered (Core stride `0x38`, ChipView `0x20`, Hal `0x30`), but the per-element `TpuCoreLocation` / `TpuChipLocation` field packing was not decoded.
- **`+0x98` SparseCore-vs-Barna label.** The constructor stores `CoreCount(chip_parts, 2) · chips` there and `SupportsSparseCore` reads it; the runtime `TpuCoreType` index-2 → SparseCore-or-Barna mapping is not separately confirmed.

---

## Cross-References

- [TpuChipConfig](tpu-chip-config.md) — the `shared_ptr<TpuChipConfig>` at `TpuTopology+0x18`; gates Megacore / logical-device geometry.
- [Per-Codename HW Constants](per-codename-hw-constants.md) — the wider per-gen constant surface (MemBanks, memory sizes, frequencies) that sits alongside this geometry on the `Target` object.
- [TPU Version → Codename Matrix](tpu-version-codename-matrix.md) — the `tpu_version` → codename map referenced by the per-codename table.
- [SparseCore Target Descriptor](sparsecore-target-descriptor.md) — the `Target+0x948` sub-descriptor whose lane/tile geometry this page links to.
- [ICI Topology Discovery](../ici/topology-discovery.md) — how the mesh dims at `+0x58..+0x74` are consumed when bringing up the inter-chip-interconnect torus.
