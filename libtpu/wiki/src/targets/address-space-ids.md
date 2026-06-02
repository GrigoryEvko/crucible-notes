# Address-Space ID Table (AS0–AS225/501/502)

> *Every ID, MemorySpace number, and pool name on this page was decoded byte-exactly from the SparseCore LLVM-lowering functions in `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

A SparseCore pointer in the LLVM backend carries a numeric **address-space ID** — the integer `N` in `!llvm.ptr<N>`. Unlike a typical GPU backend that uses a compact `AS0..AS5` numbering, the SparseCore LLVM dialect uses a sparse, banded ID space: `0` for the inherited scalar memory, `201..225` (base `0xc9`) for the SparseCore-specific pools and their alias groups, and `501/502` (`0x1f5/0x1f6`) for the two circular-buffer windows. Each ID maps 1:1 onto a 1-based `mlir::sparse_core::MemorySpace` enum value, which in turn names a physical (or virtual/alias) memory pool — `smem`, `tile_spmem`, `spmem`, `hbm`, `sflag`, `vmem`, `dreg`, `timem`, `simem`, `iova`, `mar`, the per-tile/per-SCS variants, and the may-alias `*_any` supersets.

The ID is how the `LowerToSparseCoreLlvm` pass routes a `ScDialect` memref operand to a leaf `tpu_*` intrinsic: the DMA and stream lowerings dispatch on the `(srcMemSpaceID, dstMemSpaceID)` pair, and the `addrspacecast` lowering elides or emits an `llvm.addrspacecast` by comparing the LLVM pointer types the two IDs convert to. The table is recovered three independent ways that agree to the ID — the forward `AddressSpaceDescription(ID)→string` switch, the `AddressSpaceToMemorySpace(ID)→MemorySpace` jump table, and the inverse `MemorySpaceToAddressSpace(MemorySpace)→ID` reverse table.

The contract for a reimplementer is: tag every SparseCore pointer with the ID for its pool; classify on-tile vs off-tile with the one-line `IsOffTileMemory` mask; canonicalise to the `*Any` superset for alias analysis when the exact tile/core is statically unknown.

| | |
|---|---|
| **ID bands** | `0` (base Smem); `201..225` = `0xc9..0xe1` (SC pools + alias groups); `501/502` = `0x1f5/0x1f6` (CB windows) |
| **Named IDs** | 21 (18 in the 201-band + ID 0 + 501 + 502); 8 reserved/gap slots |
| **MemorySpace enum** | 22 values, 1-based (value 8 is an unused gap) |
| **Forward (ID→name)** | `mlir::sparse_core::LlvmTpuDialect::AddressSpaceDescription(int)` @ `0x135462c0` |
| **Forward (ID→MS)** | `mlir::sparse_core::AddressSpaceToMemorySpace(uint)` @ `0x14b78800` |
| **Inverse (MS→ID)** | `mlir::sparse_core::MemorySpaceToAddressSpace(MemorySpace)` @ `0x14b78780` |
| **Pool name (MS→str)** | `mlir::sparse_core::stringifyMemorySpace(MemorySpace)` @ `0x14b78240` |
| **On-/off-tile** | `IsOffTileMemory(MemorySpace)` @ `0x13d7ac00` = `(ms & ~0x10) != 2` |
| **Alias canonicalise** | `GetAnyTypeFromAddressSpace(int)` @ `0x1357b400` |
| **Confidence** | CONFIRMED unless a cell is annotated otherwise |

---

## The Master AS-ID Table

`AS#` is the LLVM address-space integer (the `N` in `!llvm.ptr<N>`). `Region/pool` is `stringifyMemorySpace(MS#)`. `MS#` is the `MemorySpace` enum value (1-based; `0` = no canonical pool). `Width` is the addressing scale the pool covers — KB per-tile, MB chip-shared, GB global. `tile?` is `IsOffTileMemory==false`, true only for MS 2 and MS 18. A ✓ in `notes` means the ID↔MS mapping is confirmed by the inverse `MemorySpaceToAddressSpace` reverse table.

| AS# | hex | Region / pool | MS# | Width | tile? | Meaning · Confidence |
|----:|-----|---------------|----:|-------|:-----:|----------------------|
| 0 | 0x0 | smem | 1 | KB | off | inherited base TPU scalar memory ✓ · CONFIRMED |
| 201 | 0xc9 | tile_spmem | 2 | KB | **ON** | per-tile SparseCore SRAM ✓ · CONFIRMED |
| 202 | 0xca | spmem | 3 | MB | off | chip-shared SparseCore SRAM ✓ · CONFIRMED |
| 203 | 0xcb | hbm | 4 | GB | off | global HBM (embedding tables) ✓ · CONFIRMED |
| 204 | 0xcc | sflag | 5 | — | off | sync-flag memory ✓ (MS 22 `sflag_tc` also maps here) · CONFIRMED |
| 205 | 0xcd | vmem | 6 | MB | off | TensorCore vector memory (TC↔SC handoff) ✓ · CONFIRMED |
| 206 | 0xce | — | 0 | — | — | reserved / gap · CONFIRMED |
| 207 | 0xcf | — | 0 | — | — | reserved / gap · CONFIRMED |
| 208 | 0xd0 | dreg | 7 | — | off | data-register window ✓ · CONFIRMED |
| 209 | 0xd1 | — | 0 | — | — | reserved / gap · CONFIRMED |
| 210 | 0xd2 | — | 0 | — | — | reserved / gap · CONFIRMED |
| 211 | 0xd3 | — (alias) | 0 | — | off | `SflagAny` may-alias superset (no pool) · CONFIRMED |
| 212 | 0xd4 | smem_any | 9 | — | off | `SmemAny` may-alias superset ✓ · CONFIRMED |
| 213 | 0xd5 | hbm_any | 10 | — | off | `HBMAny` may-alias superset ✓ · CONFIRMED |
| 214 | 0xd6 | timem | 11 | — | off | per-tile instruction memory ✓ · CONFIRMED |
| 215 | 0xd7 | simem | 12 | — | off | SC instruction memory ✓ · CONFIRMED † |
| 216 | 0xd8 | iova | 13 | GB | off | I/O virtual address ✓ · CONFIRMED |
| 217 | 0xd9 | sflag_tile | 14 | — | off | per-tile sflag bank ✓ · CONFIRMED |
| 218 | 0xda | spmem_any | 15 | — | off | `SpmemAny` may-alias superset ✓ · CONFIRMED |
| 219 | 0xdb | smem_tile | 16 | KB | off | per-tile SMEM (`TileSmem`) ✓ · CONFIRMED |
| 220 | 0xdc | mar | 17 | — | off | memory-access-region ✓ · CONFIRMED † |
| 221 | 0xdd | — | 0 | — | — | reserved / gap · CONFIRMED |
| 222 | 0xde | — | 0 | — | — | reserved / gap · CONFIRMED |
| 223 | 0xdf | sflag_scs | 20 | — | off | per-SCS sflag bank (`SflagScs`) ✓ · CONFIRMED |
| 224 | 0xe0 | smem_scs | 21 | KB | off | per-SCS SMEM (`SmemScs`) ✓ · CONFIRMED |
| 225 | 0xe1 | — (alias) | 0 | — | off | `SflagAnySynctile` (no pool) · CONFIRMED |
| 501 | 0x1f5 | tile_spmem_cb | 18 | KB | **ON** | CBREG-windowed `TILE_SPMEM` ✓ · CONFIRMED |
| 502 | 0x1f6 | smem_cb | 19 | KB | off | CBREG-windowed `SMEM` ✓ · CONFIRMED |

The `desc` (`AddressSpaceDescription`) strings for the named IDs are, in `case` order: `TileSpmem`, `Spmem`, `HBM`, `Sflag`, `Vmem`, `Dreg`, `SflagAny`, `SmemAny`, `HBMAny`, `Timem`, `IOVA`, `SflagTile`, `SpmemAny`, `TileSmem`, `SflagScs`, `SmemScs`, `SflagAnySynctile`; ID 0 returns `Smem`; 501/502 return `"TileSpmem Circular Buffer"` / `"Smem Circular Buffer"`; everything else returns `"Unknown"`.

> **GOTCHA —** IDs **215** (`simem`) and **220** (`mar`) carry a real `MemorySpace` (12 and 17) but `AddressSpaceDescription` returns the empty default for them — they fall into the same `case 206/207/209/210/215/220/221/222: return result` arm as the true reserved gaps. The pool names `simem`/`mar` come from `stringifyMemorySpace`, not from the description switch. A reader that derives names only from `AddressSpaceDescription` will wrongly treat 215/220 as reserved.

---

## How the Backend Tags Pointers

The `LlvmTpuDialect` declares no separate "pointer type" per pool. Instead the address-space *integer* above is the `N` in the LLVM pointer type `!llvm.ptr<N>`, and a `ScDialect` memref carries its `MemorySpace` as a memref attribute. The lowering converts that to the LLVM AS number and uses it as the dispatch key:

```text
ScDialect op (memref with MemorySpace attr)
  → AddressSpaceToMemorySpace / MemorySpaceToAddressSpace  (ID ↔ MS, 1:1)
  → getStridedElementPtr → !llvm.ptr<AS#>                  (raw element pointer)
  → DMA/stream lowering dispatch on (srcAS, dstAS)         (selects tpu_* intrinsic)
```

`AddressSpaceToMemorySpace(uint)` is a jump table over IDs `201..224` plus explicit `501→18` / `502→19` arms; the low 32 bits of its `0x1_0000000N` return value are the `MemorySpace` enum. `MemorySpaceToAddressSpace(MemorySpace)` is the exact inverse, gated by a validity mask `0x3fff7f` (the bit set of the 22 valid MemorySpace values, with the value-8 gap clear). `stringifyMemorySpace` and `TpuVersionToString` are both pointer-table lookups (`off_219AF590[ms]` and `off_22011BF0[ver]`) whose string pointers live in `.data.rel.ro` and are filled by `R_X86_64_RELATIVE` relocations at load — they read as zero in the on-disk image.

---

## On-Tile vs Off-Tile (the access-semantics gate)

`IsOffTileMemory(MemorySpace)` is a single masked compare:

```c
bool IsOffTileMemory(int ms) { return (ms & 0xFFFFFFEF) != 2; }   // (ms & ~0x10) != 2
```

Clearing bit 4 (`0x10`) folds MS 2 (`tile_spmem`) and MS 18 = `0x12` (`tile_spmem_cb`) together, so **only** those two are on-tile. Every other pool — `hbm`, `spmem`, `smem`, `sflag`, `vmem`, `dreg`, `timem`, `simem`, `iova`, `mar`, all the `*_tile`/`*_scs`/`*_any` variants — is off-tile and requires a DMA, stream, or sync to reach. This is the predicate the DMA and stream lowerings consult before selecting a data-movement intrinsic.

---

## The `*Any` May-Alias Canonicalisation

When a pointer's exact tile or core is statically unknown, the SparseCore LLVM backend widens it to a wildcard `*Any` space for alias analysis. `GetAnyTypeFromAddressSpace(int)` is the canonicaliser:

| concrete ID (name) | → canonical ID (name) |
|---|---|
| 201 `TileSpmem`, 202 `Spmem` | 218 `SpmemAny` |
| 203 `HBM` | 213 `HBMAny` |
| 204 `Sflag` | 211 `SflagAny` |
| 205 `Vmem` | 205 `Vmem` (self — no separate wildcard) |
| 219 `TileSmem`, 0 `Smem` | 212 `SmemAny` |

The `*Any` IDs (211/212/213/218) carry a description but **no** `MemorySpace` pool — they are alias-analysis groupings, not physical pools. Calling `GetAnyTypeFromAddressSpace` on an already-wildcard or leaf space (`Dreg`, `Timem`, `IOVA`, `SflagTile`, the `*Any` IDs themselves) hits the `LogFatal("Unsupported address space: ")` arm (`llvm_tpu_dialect_only.h:100`), so the canonicaliser is total only over the concrete spaces above.

> **NOTE —** the `*Any` widening is the SparseCore answer to the fat-pointer problem: a pointer into HBM/SPMEM whose owning tile is a runtime value cannot be proven disjoint from another such pointer, so the backend assigns both the `HBMAny`/`SpmemAny` superset and lets alias analysis treat them as may-alias. The concrete-vs-Any distinction is what keeps statically-resolved tile-local accesses from being pessimised.

---

## Cross-Validation and `CheckAddressSpaces`

The four accessors form a closed, self-checking system:

```text
AddressSpaceDescription(ID)   : ID → human string      @0x135462c0
AddressSpaceToMemorySpace(ID) : ID → MemorySpace        @0x14b78800
MemorySpaceToAddressSpace(MS) : MemorySpace → ID (inv)  @0x14b78780
stringifyMemorySpace(MS)      : MemorySpace → pool name  @0x14b78240
```

The forward and inverse ID↔MS maps are exact inverses for all 21 named IDs (verified arm-by-arm against the decompiled switches). `CheckAddressSpaces(SparseCoreTarget&, Operation*, int, int)` @ `0x135b8e00` is the verifier the lowering calls to validate a `(src, dst)` ID pair against the target before emitting a data-movement intrinsic; its full legality matrix (which pairs are valid per primitive) is not enumerated here — only its existence and signature are confirmed.

---

## Related Components

| Name | Relationship |
|---|---|
| `LowerToSparseCoreLlvm` pass | reads these IDs to route memref operands to `tpu_*` intrinsics |
| `MemorySpaceCastOpLowering` @ `0x135a5c20` | elides/emits `llvm.addrspacecast` by comparing converted pointer types |
| `DmaSimpleStartOpLowering` / `LinearStreamStartOpLowering` | dispatch on `(srcAS, dstAS)` / `(dtype, off-tile MS, verb)` |
| `getStridedElementPtr` | turns a memref+index into a raw `!llvm.ptr<AS#>` |

## Cross-References

- [Memory Hierarchy](memory-hierarchy.md) — the HBM/VMEM/SMEM/SFLAG/CMEM tier model these pools populate
- [Memory-Space Enum](../isa/memory-space-enum.md) — the `MemorySpace` enum and its 22 values
- [Fat Pointers (AS 7/8/9)](../sparsecore/fat-pointers-as789.md) — the SparseCore fat-pointer encoding and the `*Any` superset relation
- [addrspacecast ISel](../sparsecore/addrspacecast-isel.md) — the elide-or-emit rule for `llvm.addrspacecast` over these IDs
