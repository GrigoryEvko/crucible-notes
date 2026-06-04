# SparseCoreTarget (`Target+0x948`)

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

`xla::jellyfish::Target` is the per-generation hardware descriptor that the XLA TPU backend queries for every codegen and cost decision. The SparseCore half of that descriptor does not live inline in `Target`; it is a separately allocated `std::unique_ptr<xla::jellyfish::SparseCoreTarget>` parked at `Target+0x948`. This page is the full field map of that sub-object, the accessor surface that reaches into it, and — because it is the cleanest per-codename MXU differentiator recoverable from this wheel — the per-generation MXU-contracting-depth table that lives alongside it in the per-codename `Target` subclasses.

Three concrete facts drive the page, and a reimplementer needs all three:

- **SparseCore presence is gated, not assumed.** Every `Target::SparseCore*` accessor first calls a virtual predicate (`SupportsSparseCore`, vtable slot `+0x260`) and `LOG(FATAL)`s if it is false. The `Target+0x948` pointer is only populated on the SparseCore-bearing generations; on the BarnaCore generations (Jellyfish/Dragonfish/Pufferfish) and the TensorCore-only lite dies, the guard short-circuits before the pointer is ever dereferenced.
- **Three SparseCoreTarget classes ship.** A base `SparseCoreTarget` plus two concrete subclasses — `ViperfishSparseCoreTarget` (v5p) and `GhostLiteSparseCoreTarget` (v6e Ghostlite *and* v7x `6acc60406`). The per-gen capability bits, latencies, and FLOPS are C++ literals in those subclass virtuals.
- **The MXU contracting depth is a compiled-in literal.** Base `Target::MxuContractingSize` returns 128; only `GhostliteTarget` overrides it to 256. This 128-vs-256 split is the single hard per-codename MXU-geometry value in this build, and it is not a `chip_parts` proto field.

| | |
|---|---|
| **Sub-object** | `std::unique_ptr<xla::jellyfish::SparseCoreTarget>` at `Target+0x948` |
| **Built by** | `SparseCoreTarget::Init` @ `0x1D612B20`, installed by `Target::Init` |
| **Presence gate** | `Target::SupportsSparseCore` @ `0x1D48FD40` (vtable slot `+0x260`) |
| **Concrete classes** | base / `ViperfishSparseCoreTarget` / `GhostLiteSparseCoreTarget` |
| **Has SparseCore** | Viperfish (v5p), Ghostlite (v6e), `6acc60406` (v7x) |
| **No SparseCore** | Jellyfish / Dragonfish / Pufferfish (BarnaCore); lite dies (none) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Presence Gate

`Target+0x948` is meaningful only when the target has SparseCore silicon. The predicate that decides this is `Target::SupportsSparseCore` @ `0x1D48FD40`, and it does *not* test the `+0x948` pointer — it reads a count out of the `TpuTopology` sub-object at `Target+0x3B8`:

```c
// Target::SupportsSparseCore @ 0x1D48FD40
bool Target::SupportsSparseCore(Target *this) {
  // (this + 119) == Target+0x3B8 == TpuTopology*; field +0x98 is an SC count
  return *(uint32_t *)(*(uintptr_t *)(this + 0x3B8) + 0x98) > 0;
}
```

It is wired into vtable slot `+0x260`. Every scalar accessor in the next section dispatches through that slot before touching `Target+0x948`, so on a BarnaCore generation the accessor `LOG(FATAL)`s with `"SparseCore is not supported by this target"` rather than dereferencing a null pointer. This is the structural reason the older generations carry no `SparseCoreTarget`: their `TpuTopology[+0x98]` SparseCore count is zero, the gate returns false, and `Target::Init` never builds the sub-object.

> **NOTE —** the predicate is a *runtime topology* test, not a compile-time class test. A reimplementation must populate the SparseCore-count field of its topology descriptor before any SparseCore accessor is reachable; otherwise the entire SC accessor surface traps.

---

## The Scalar Accessor Surface

Six `Target::SparseCore*` accessors reach into `Target+0x948` and read one scalar each. They share an identical shape: dispatch the `+0x260` gate, `LOG(FATAL)` if false, then load `*(*(this+0x948) + field)`. The decompiled bodies confirm the base and every field offset (the decompiler renders `Target+0x948` as `*((_QWORD *)this + 297)`; `297 × 8 = 0x948`).

| Accessor | Address | Reads | v7x value |
|---|---|---|---|
| `SparseCoreTiles` | `0xFAAFA40` | `[0x948] + 0x90` | 16 |
| `SparseCoreLaneCount` | `0xF7906E0` | `[0x948] + 0x94` | 16 |
| `SparseCoreHbm4bWordSizeBytes` | `0x1320C220` | `[0x948] + 0x58` | 4 |
| `SparseCoreStreamGranuleSizeBytes` | `0x13886EE0` | `[0x948] + 0xA4` | 4 |
| `GetSparseCoreBarrierSyncFlagCount` | `0x10972FA0` | `[0x948] + 0x1D4` | — |
| `SupportsSparseCore` | `0x1D48FD40` | `[0x3B8 TpuTopology] + 0x98 > 0` | true |
| `SparseCoresPerLogicalDevice` | `0x135159C0` | `CoresPerChip / LogicalDevicesPerChip` | 2 |

A representative body (`SparseCoreTiles`, `0xFAAFA40`):

```c
__int64 Target::SparseCoreTiles(Target *this) {
  // vtable +0x260 == *(*this + 608)
  if (!(*(uint8_t (**)(Target *))(*(uintptr_t *)this + 608))(this))
    LOG(FATAL) << "SparseCore is not supported by this target";  // target.h:1704
  return *(uint32_t *)(*(uintptr_t *)(this + 0x948) + 0x90);     // SparseCoreTiles
}
```

`SparseCoresPerLogicalDevice` (`0x135159C0`) is the odd one out: it does not read `[0x948]` at all, computing `CoresPerChip(SPARSE_CORE) / LogicalDevicesPerChip` instead. It is included here because it is the entry point the runtime calls (via `tensorflow::GetAndSetSparseCoresPerLogicalDevice`) to size per-device SparseCore counts.

---

## Full Field Layout of `Target+0x948`

The sub-object is one struct with two consumer views: the SC-MLO allocator view (the `+0x10..+0x54` word/capacity block) and the HAL/cost-model view (`+0x58..+0x1D4`). Offsets are read from the `SparseCoreTarget::Init` (`0x1D612B20`) store sites or from the matching accessor deref. The struct spans at least `0x240` bytes; its constructor is inlined, so the destructor (`0x1D499060`) is a bare `ret`.

| Off | Type | Field / meaning |
|---|---|---|
| `+0x08` | ptr | back-pointer (`Target*` / core ptr) |
| `+0x10` | i32 | TIMEM/SCS-sflag capacity (group 0) |
| `+0x14` | i32 | TAC-sflag capacity (group 1) |
| `+0x18` | i32 | TEC-sflag / per-tile sflag pool (group 2) |
| `+0x1C` | i32 | SCS-SMEM capacity (group 0) |
| `+0x20` | i32 | TAC-SMEM capacity (group 1) |
| `+0x24` | i32 | TEC-SMEM capacity (group 2) |
| `+0x28` | i32 | **TILE_SPMEM capacity (bytes)** |
| `+0x2C` | i32 | **SPMEM capacity (bytes)** |
| `+0x30` | i32 | sparse-core SFLAG capacity |
| `+0x38` | i64 | HBM 4b-word count = `(Target[0x450]+3) >> 2` |
| `+0x40` | i32 | TIMEM/sflag word size (group 0) |
| `+0x44` | i32 | SMEM word size |
| `+0x48` | i32 | TILE_SPMEM word size |
| `+0x4C` | i32 | SPMEM word size |
| `+0x50` | i32 | TILE-instr (TIMEM) word size |
| `+0x54` | i32 | per-group word-size slot |
| `+0x58` | i32 | **`SparseCoreHbm4bWordSizeBytes` = 4** |
| `+0x5C..+0x70` | i32×N | group-mirrored sflag/smem word sizes + capacities |
| `+0x74` | i32 | **literal `2`** (SCS sequencer/group count) |
| `+0x78..+0x8C` | i32×N | per-coretype memory counts (TENSOR/SC group fanout) |
| `+0x90` | i32 | **`SparseCoreTiles` = `SequencerCount(5)`** |
| `+0x94` | i32 | **`SparseCoreLaneCount` = `vector_isa(5).lane_count`** |
| `+0x98` | i32 | SC lane bytes = `lane_count << 2` |
| `+0x9C` | i32 | SC freq / host-irq block (`TpuCoreParts[0x138]`) |
| `+0xA0` | i32 | `tile_hbm_bandwidth_bytes_per_cycle` (`cp[0x1E0]`, gated `cp[0x1E8]`) |
| `+0xA4` | i32 | **`stream_granule_size`** (`cp[0x1E4]`) |
| `+0xA8..+0x238` | ptr×N | per-tile reserved-region vectors (SMEM/SPMEM/SFLAG windows) |
| `+0x188` | u8 | bool flag |
| `+0x190` | i64 | **embedding param-region base word offset** |
| `+0x198` | u8 | bool flag |
| `+0x1D0` | i32 | SC barrier sync-flag base (`SpecialPurposeSyncFlags[0]`) |
| `+0x1D4` | i32 | **`GetSparseCoreBarrierSyncFlagCount`** (`SpecialPurposeSyncFlags[+0x10]`) |
| `+0x1E8` | u8 | SparseCore-proto-present flag |
| `+0x1EC..+0x22C` | i32×N | compiler-reserved SFLAG watermarks + per-tile reserved-SFLAG block |
| `+0x230/+0x238` | ptr×2 | tail region vectors |
| `~0x240` | — | `sizeof(SparseCoreTarget) >= 0x240` |

> **GOTCHA —** the `+0xA8..+0x238` block is a run of libc++ vectors holding per-tile reserved-region descriptors. The store offsets are byte-confirmed, but the element layout of each vector (which reserved tile region each holds) was not individually walked — those rows are HIGH, not CONFIRMED.

---

## Init Geometry Block

`SparseCoreTarget::Init` (`0x1D612B20`) populates the geometry from the `SPARSE_CORE` `TpuCoreParts`. The block below is the decompiled store sequence, byte-confirmed (decompiler offsets in decimal; hex in comments):

```c
// SparseCoreTarget::Init @ 0x1D612B20 — geometry block
*(uint32_t *)(sc + 0x58) = 4;                              // SparseCoreHbm4bWordSizeBytes (literal)
*(uint32_t *)(sc + 0x74) = 2;                              // SCS sequencer/group count (literal)
*(uint32_t *)(sc + 0x90) = TpuCoreParts::SequencerCount(cp, 5);   // SparseCoreTiles (seq type 5)
vi = TpuSequencerParts::vector_isa(cp.SequencerParts(5));
*(uint32_t *)(sc + 0x94) = vi.lane_count;                  // SparseCoreLaneCount
*(uint32_t *)(sc + 0x98) = 4 * vi.lane_count;              // SC lane bytes
if (cp[0x1E8] /* SparseCore submsg present */) {
  *(uint32_t *)(sc + 0xA0) = cp[0x1E0];                    // tile_hbm_bandwidth_bytes_per_cycle
  *(uint32_t *)(sc + 0xA4) = cp[0x1E4];                    // stream_granule_size
}
// ... later: barrier sync-flag base + count from a SpecialPurposeSyncFlags sub-object
*(uint32_t *)(sc + 0x1D0) = SpecialPurposeSyncFlags[0];
*(uint32_t *)(sc + 0x1D4) = SpecialPurposeSyncFlags[+0x10];   // GetSparseCoreBarrierSyncFlagCount
```

The tile/lane geometry is sourced from `TpuCoreParts::SequencerCount(cp, 5)` and `SequencerParts(5).vector_isa()`. Both indices use the *internal* (codec-template) `TpuSequencerType` numbering `{SCS=3, TAC=4, TEC=5}`, so index `5` is `SC_TEC` — the SparseCore tile-execute pool, the 16-instance, `lane_count`-16 `VectorIsa` on v7x. So `SparseCoreTiles = 16`, `SparseCoreLaneCount = 16` for v7x, matching the `chip_parts` decode of the `6acc60406` SC sequencer geometry.

> **NOTE —** `Init`'s `SequencerCount(cp, 5)` / `SequencerParts(cp, 5)` indices are *internal* `TpuSequencerType` values, the same codec-template numbering the Part IX SparseCore pages use: `{TC=0, BARNA=1, BARNA_ADDR=2, SCS=3, TAC=4, TEC=5}`. In that scheme index `5 = SC_TEC` (the tile-execute pool), which is exactly what the geometry block reads — so `SparseCoreTiles` / `SparseCoreLaneCount` come from the TEC sequencer, not from TAC. The SparseCore block of this enum is `{SCS=3, TAC=4, TEC=5}`, the canonical wiki numbering; see [getSequencerType](../sparsecore/getsequencertype.md#the-off-by-one-runtime-enum-vs-codec-template-enum) and [SparseCore Overview](../sparsecore/overview.md).

> **QUIRK —** there are two legitimate numbering schemes, off by one, and `Init` uses the internal one. The *proto* enum `TpuSequencerTypeProto` prepends an `INVALID=0` slot and renumbers the SparseCore block one higher — `{INVALID=0, TC=1, BARNA=2, BARNA_ADDR=3, SCS=4, TAC=5, TEC=6}` — so a reader who applies proto numbering to `Init`'s index would mis-read `5` as TAC. `tpu::TpuSequencerTypeToProto` (`0x20B36460`) returns `internal + 1` and `tpu::TpuSequencerTypeFromProto` (`0x20B36300`) maps proto case *N* to `internal = N − 1`, so `internal = proto − 1` exactly. Code paths that index `TpuCoreParts` (`Init` here) take the internal value; only serialized proto fields and the `TpuSequencerTypeToString` label table use the proto value. (`6acc60406` / `gfc` carries internal codec params 3 and 5 only — SCS + TEC, no TAC — so on v7x there is no TAC sequencer to confuse with TEC in the first place.)

---

## Non-Virtual Helpers

The base `SparseCoreTarget` defines three non-virtual helpers the SC-MLO allocator reads. All byte-confirmed:

| Helper | Address | Body |
|---|---|---|
| `SparseCoreSpmemStripeGranularityBytes` | `0x1D499440` | `return 0x100000020LL;` → `optional<32>` (value `0x20`=32, engaged-flag in high dword) |
| `SparseCoreParamPtrLocationWordOffset(i)` | `0x1D618080` | `return [0x190] - i;` with `CHECK(result >= 0)` |
| `SparseCoreStartReservedSmemWordOffset(i)` | `0x1D618060` | tail-jumps to the above |

These pin field `+0x190` as the embedding param-region base word offset: the allocator places parameter pointers at descending offsets `[0x190] - i` and fatals on underflow (`"Parameter number N causes underflow"`).

---

## Per-Codename MXU Contracting-Depth Table

The MXU contracting dimension is a per-codename C++ literal in the `Target` subclass, not a `chip_parts` field. The base `Target` returns 128; only `GhostliteTarget` overrides to 256. "inherit" means the subclass does not override and uses the base value. Every numeric cell is a byte-exact literal read from the named method.

| MXU constant (CODE) | v2 Jelly | v3 Dragon | v4 Puffer | v5p Viperfish | v6e Ghostlite | v7x 6acc60406 |
|---|---|---|---|---|---|---|
| `MxuContractingSize` | 128 | 128 | 128 | 128 | **256** | **256** |
| `MxuNoncontractingSize` | 128 | 128 | 128 | 128 | **256** | **256** |
| `MxuSparseContractingSize` | 0 | 0 | 0 | 0 | 0 (inherit) | 0 (inherit) |
| `MxuContractingSizeIsDoubled(mode)` | false | false | false | **predicate** | **predicate** | **predicate** |
| └ doubled-mode set (raw GainLatchMode) | — | — | — | {22,23,24,25} | {22,23,24,25} | {22,23,24,25} |
| `MinLmrWidthInColumns` | FATAL | FATAL | FATAL | **8** | **16** | **16** |
| `MaxLmrWidthInColumns` | FATAL | FATAL | FATAL | **128** | **128** | **128** |

> **NOTE —** v7x `6acc60406` reuses the `GhostliteTarget` TensorCore subclass; no separate `Tpu7xTarget`/`TpuV7xTarget` class exists in this build. The v6e/v7 distinction is data-driven via `chip_parts`, so both get `MxuContractingSize` = 256.

How a reimplementer should read this:

- **Contracting depth.** The systolic MXU reduction depth is 128 on Jellyfish through Viperfish and 256 on the Ghostlite class (v6e/v7). This is orthogonal to the `chip_parts` `lane_count`=128: the proto carries the 128-lane *width* and `mxu_count`=2 for v7x; the 256 is the systolic *row depth* code constant. Cross-validated by the FLOPS relation on the 128×128 generations (peak BF16 = `2 × mxu_count × 128² × frequency_mhz` reproduces v2/v3/v4 to within 1%).
- **The doubling predicate.** Both `ViperfishTarget` and `GhostliteTarget` override `MxuContractingSizeIsDoubled` with the *identical* predicate `(mode - 22) < 4u`, i.e. true for raw `GainLatchMode` ∈ {22,23,24,25}. Those indices are the 4-bit packed-nibble matmul modes (S4/U4): two nibbles pack into one physical systolic row, doubling effective contracting depth (256 on the 128-row gens, 512 on Ghostlite's 256-row). The base returns always-false — the older gens have no 4-bit MXU mode.
- **LMR width.** `Min/MaxLmrWidthInColumns` is the latch-matrix-register column window (the staging-register granularity), *distinct* from the contracting depth. It is defined only on the SparseCore-bearing gens; the base `LOG(FATAL)`s `"Unimplemented"`. Jellyfish/Pufferfish instead expose the older `MxuResultEntries{Pushed,Popped}` result-FIFO model. `MxuSparseContractingSize` is 0 on every gen — no subclass overrides it; sparse-MXU contracting is unused in this build.

---

## SparseCore Presence and Geometry Per Generation

| Generation | TpuVersionProto | SparseCore? | SparseCoreTarget class | Tiles × Lane | MXU contracting |
|---|---|---|---|---|---|
| v2 Jellyfish | 1 | no (BarnaCore) | — | — | 128 |
| v3 Dragonfish | 2 | no (BarnaCore) | — | — | 128 |
| v4 Pufferfish | 3 | no (BarnaCore) | — | — | 128 |
| v5p Viperfish | 4 | yes | `ViperfishSparseCoreTarget` | — × 8 (SC_TEC lane) | 128 |
| v6e Ghostlite | 5 | yes | `GhostLiteSparseCoreTarget` | — × 8 (SC_TEC lane) | 256 |
| v7x `6acc60406` | 6 | yes | `GhostLiteSparseCoreTarget` | 16 × 16 | 256 |
| v4 lite / v5e lite | — | no (TC-only) | — | — | 128 |

The `Tiles × Lane` column shows the `chip_parts` `SC_TEC VectorIsa` lane width; v7x widens that to 16 (and `SparseCoreTiles`/`SparseCoreLaneCount` both report 16 after `Init`). v5p/v6e carry the narrower 8-lane TEC. The lite dies (`pufferfish_lite`, `viperfish_lite`) carry neither BarnaCore nor SparseCore — their `TpuTopology[+0x98]` SC count is zero, so `SupportsSparseCore` is false and no `SparseCoreTarget` is built.

---

## Per-Gen SparseCore Capability / Geometry Table

The 24 `SparseCoreTarget` virtual accessors are present in both concrete subclasses (`ViperfishSparseCoreTarget` for v5p, `GhostLiteSparseCoreTarget` for v6e Ghostlite + v7x `6acc60406`). Values are byte-exact, matched by method name (the Viperfish vtable order shifts by one vs GhostLite).

| `SparseCoreTarget` vfn | Viperfish (v5p) | Ghostlite / 6acc60406 (v6e/v7x) | Source (VF / GL) · Confidence |
|---|---|---|---|
| `FlopsPerSparseCore(fmt∈{1,2})` | 1.0e12 (1 TFLOP/s) | 3.595e13 (35.95 TFLOP/s) | `0x1D49C540` / `0x1D4990A0` · CONFIRMED |
| └ other fmt | → `Target` vtable `+0x718` | → `Target` vtable `+0x718` | tail-call · CONFIRMED |
| `SparseCoreTileCrossbarBandwidthRandomAccess` | 29 B/cyc | 29 B/cyc | vtable · CONFIRMED |
| `SparseCoreTileVectorAluSlotCount(bool)` | 3 | 3 | vtable · CONFIRMED |
| `SparseCoreHbmAccessLatency` | 418 | 418 | `0x1D49C5C0` / `0x1D499120` · CONFIRMED |
| `SparseCoreSpmemAccessLatency` | 30 | 30 | `0x1D49C5E0` / `0x1D499140` · CONFIRMED |
| `TaskRequestStartAccessFunctionArgWordOffset` | 1 | 1 | vtable · CONFIRMED |
| `TaskRequestStartExecuteFunctionArgWordOffset` | 1 | 1 | vtable · CONFIRMED |
| `TaskRequestEndExecuteFunctionArgWordOffset` | 0 | 0 | vtable · CONFIRMED |
| `TraceEnBitOffsetInStreamControl` | 15 | 15 | vtable · CONFIRMED |
| `SetDoneBitOffsetInStreamControl` | 2 | 2 | vtable · CONFIRMED |
| `TileLocalStrideOffsetInStreamControl` | 3 | 3 | vtable · CONFIRMED |
| `IndirectListTypeBitOffsetInStreamControl` | 7 | 7 | vtable · CONFIRMED |
| `IndirectFilterEnBitOffsetInStreamControl` | 14 | 14 | vtable · CONFIRMED |
| `SupportsScVdupcntVuniqueWithLaneIds` | **0** | **1** | `0x1D49C7A0` / `0x1D499300` · CONFIRMED |
| `SupportsScVldVstIdxAdd` | **0** | **1** | `0x1D49C7C0` / `0x1D499320` · CONFIRMED |
| `SupportsScVar` | 0 | 0 | `0x1D49C7E0` / `0x1D499340` · CONFIRMED |
| `SupportsScFp8VectorCmp` | 0 | 0 | vtable · CONFIRMED |
| `SupportsScVmemStream` | 0 | 0 | vtable · CONFIRMED |
| `SupportsScHbm4bStream` | 1 | 1 | `0x1D49C840` / `0x1D4993A0` · CONFIRMED |
| `SupportsScLocalSpmemDma` | 1 | 1 | `0x1D49C860` / `0x1D4993C0` · CONFIRMED |
| `SupportsScBundleCompression` | 0 | 0 | vtable · CONFIRMED |
| `SupportsScB8VectorMaskPopulationCount` | 0 | 0 | vtable · CONFIRMED |
| `SupportsScEupOps` | **1** | **1** | `0x1D49C8C0` / `0x1D499420` · CONFIRMED |
| `SupportsTileSmemDma` | 0 | 0 | vtable · CONFIRMED |

The genuine Viperfish → Ghostlite/6acc60406 capability gains are exactly two bits plus the FLOPS jump:

- `SupportsScVdupcntVuniqueWithLaneIds` (0 → 1) — vdupcnt / vunique-with-lane-ids ops.
- `SupportsScVldVstIdxAdd` (0 → 1) — indexed vector load/store with add.
- `FlopsPerSparseCore` 1 TFLOP/s → 35.95 TFLOP/s — the widened Ghostlite/6acc60406 TEC vector engine.

> **GOTCHA —** `SupportsScEupOps` returns **1 on both** Viperfish and Ghostlite/6acc60406 (verified at `0x1D49C8C0` and `0x1D499420` — both `return 1`). It is *not* a per-generation delta: the Extended-Unit-Pipeline ops are present on Viperfish too, so do not count it among the Ghostlite gains. The only capability-bit deltas are the two listed above.

The `FlopsPerSparseCore` body is a small dispatcher, identical in shape across both subclasses:

```c
// GhostLiteSparseCoreTarget::FlopsPerSparseCore @ 0x1D4990A0
__int64 FlopsPerSparseCore(this, int fmt) {
  if ((uint8_t)(fmt - 1) >= 2u)                       // fmt not in {1,2}
    return (*Target_vtable[+0x718])(this->target, fmt);  // delegate to Target flops
  return /* xmm0 = */ qword_A2DE920;                  // 3.595e13 for fmt in {1,2}
}
```

For matmul data formats 1 and 2 it returns the SparseCore-local FLOPS constant; for everything else it tail-delegates to the `Target` per-format FLOPS at vtable slot `+0x718`. Viperfish has the identical structure with the constant at `qword_A2DFD18` (1.0e12).

---

## Related Components

| Name | Relationship |
|---|---|
| `Target::Init` | builds `unique_ptr<SparseCoreTarget>` via `SparseCoreTarget::Init`, installs it at `Target+0x948` |
| `SparseCoreTarget::Init` (`0x1D612B20`) | populates every field from the `SPARSE_CORE` `TpuCoreParts` |
| `xla_mlo_util::CapacityInBytes` / `WordSizeInBytes` | SC-MLO allocator consumers of the `+0x10..+0x54` capacity/word block |
| `GhostliteTarget` / `ViperfishTarget` | the TensorCore `Target` subclasses carrying the MXU-256 / LMR-width literals |

## Cross-References

- [Per-Codename Constants](per-codename-hw-constants.md) — the `chip_parts`-sourced per-generation memory/MXU/core-count table this descriptor's geometry is decoded against
- [TpuChipConfig](tpu-chip-config.md) — how the per-codename constants assemble into the runtime `Target` configuration
- [SparseCore Overview](../sparsecore/overview.md) — the SCS/TAC/TEC engine model these capability bits and stream-control offsets feed
- [SC ↔ MXU Handshake](../sparsecore/sc-mxu-handshake.md) — the consumer of `MxuContractingSize`, the doubling predicate, and the LMR-width window
