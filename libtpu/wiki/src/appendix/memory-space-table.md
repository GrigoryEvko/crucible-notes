# Memory-Space Master Table

> *Every enum ordinal, address-space ID, allocator symbol, and per-gen geometry on this page was decoded byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous version anchor). Other builds will differ.*

## Abstract

This appendix is the single consolidated reference for **every memory space `libtpu.so` addresses** — the off-chip HBM main store, the on-chip TensorCore tiers (VMEM, SMEM, CMEM, SFLAG, IMEM), the BarnaCore sub-core tiers, the SparseCore sequencer pools, the host-interface and host-RAM pools, and — orthogonally — the **SparseCore LLVM address-space ID space** (`AS0` plus the `0xC9..0xE1` / `0x1F5`/`0x1F6` band) the SparseCore backend uses to tag pointers. It aggregates and cross-checks the facts that live on the per-tier memory deep pages and the two ISA/target enum pages into one master table, re-verified against the binary.

There are **three distinct, separately-numbered integer spaces** here, and the central job of this page is to keep them apart, because the same word (`smem`, `sflag`, `hbm`) appears in all three with different integers:

1. The **LLO `xla::jellyfish::MemorySpace` enum** — 17 values (`0`..`16`), the operand-space tag on every TensorCore LLO load/store, decoded by `MemorySpaceToString` @ `0x1d6ffae0` reading `off_21CE6B08[ms]`. This is what the allocator, the DMA emitter, and the bundle packer dispatch on. `smem` = **5**, `sflag` = **6**, `sparse_core_sequencer_smem` = **14**.
2. The **wire `MemorySpaceProto` field numbers** — the same 17 spaces, *remapped* integers (`hib`/`vmem`/`cmem` differ); a (de)serializer must remap at the boundary. Owned by [memory-space-enum.md](../isa/memory-space-enum.md).
3. The **SparseCore `mlir::sparse_core::MemorySpace` enum** (22 values, 1-based, value-8 gap) and its **LLVM address-space IDs** (`AS0`, `AS201..225`, `AS501/502`). Here `smem` = MS **1** (AS `0`). Owned by [address-space-ids.md](../targets/address-space-ids.md) and [fat-pointers-as789.md](../sparsecore/fat-pointers-as789.md).

The three are **not** convertible by arithmetic — only named pools correspond, and only by physical identity. This page gives the master table keyed on the LLO enum, then a focused section for the LLO ordinals, a section for the SparseCore AS space (including the dead AS7/8/9 fat-pointer reserve), and a section for the alignment/geometry rules. One deep-page label still disagrees with the binary (an off-by-one sequencer-SMEM name); it is corrected in place below.

This is a pure reference catalog — there is no algorithm to reimplement, only data to reproduce exactly. Every factual table carries a Confidence column.

| | |
|---|---|
| **LLO enum** | `xla::jellyfish::MemorySpace` — 17 values `0`..`16` |
| **LLO decoder** | `MemorySpaceToString(MemorySpace)` @ `0x1d6ffae0` → `off_21CE6B08[ms]` (no bounds check) |
| **LLO color remap** | `ColorToMemorySpace(color)` @ `0x1d6ffb00` → `byte_B5435CA[color]`, `color < 0xA` (10) |
| **Wire enum** | `xla.jellyfish.MemorySpaceProto` descriptor @ VA `0xbf8cc80` (17 values, remapped) |
| **SparseCore enum** | `mlir::sparse_core::MemorySpace` — 22 values, 1-based, value-8 gap |
| **SparseCore AS↔MS** | `AddressSpaceToMemorySpace` @ `0x14b78800` / `MemorySpaceToAddressSpace` @ `0x14b78780` (table `dword_AF36CE8`, mask `0x3FFF7F`) |
| **Universal allocator** | `tpu::BestFitAllocator` (208-B instance, ctor `0x1e817500`); one per tier, per-tier `Config` |
| **Compile-time placer** | `ProgramMemoryAllocator::AllocateBytes(MemorySpace, …)` @ `0x1c629e40` |
| **Confidence** | CONFIRMED (byte-anchored) unless a cell or callout says otherwise |

---

## The Master Memory-Space Table

The rows are keyed on the **LLO `MemorySpace` enum** (the operand-space tag, the one number that flows end-to-end through the TensorCore compiler and runtime). `AS-id` is the SparseCore LLVM address-space integer for the *physically corresponding* SparseCore pool, where one exists — the two enums meet only at the SparseCore/SC-sequencer pools, and the AS-id column is blank for pure-TensorCore tiers that the SparseCore backend never names. `Allocator/owner` names the manager that owns the tier's bytes; `Owning page` is the deep page that documents it. Per-gen size/geometry literals live in `chip_parts.binarypb` (boot-filled) and are not in `.text`; the *formulas* and field offsets are exact, the literal byte counts are not asserted here (see [chip-parts-binarypb.md](../targets/chip-parts-binarypb.md)).

| LLO# | Space | AS-id | Purpose | Per-gen size / geometry | Allocator / owner | Owning page |
|-----:|-------|------:|---------|-------------------------|-------------------|-------------|
| 0 | `<no memory space>` | — | unset / invalid sentinel (default-constructed) | — | — | [memory-space-enum.md](../isa/memory-space-enum.md) |
| 1 | `hbm` | 203 (`0xCB`) | off-chip DRAM main store: program I/O, spill, embeddings | tens of GiB; `Config{base=0, end=chip_parts HBM −reserved, align=1024 B DMA / 16 KiB compile, granule=chip_parts}` | `BestFitAllocator` (runtime) / `ProgramMemoryAllocator` (compile) | [hbm-allocator.md](../memory/hbm-allocator.md), [hbm-dma-alignment.md](../memory/hbm-dma-alignment.md) |
| 2 | `hib` | — | Host-Interface Buffer: HBM↔host staging tier the HIB DMA engine drives | chip_parts | HIB DMA engine | [memory-space-enum.md](../isa/memory-space-enum.md) |
| 3 | `vmem` | 205 (`0xCD`) | vector memory: MXU/VPU operand staging, the MSA `kAlternate` fast tier | ~16–64 MiB/TensorCore; `align=VmemAlignmentBoundaryInBytes()`, `granule=VmemWordSizeBytes()` (`Target+0x50C`); banks JF8/PF16/VF32/GL32 | `BestFitAllocator` / MSA + `ProgramMemoryAllocator` | [vmem-allocator.md](../memory/vmem-allocator.md) |
| 4 | `cmem` | — | constant memory: Pufferfish-only read-mostly operand pool (dedicated co-issue load slot) | `CmemSizeBytes()` (`Target+0x460`); word=granule=`CmemWordSizeBytes()` (`Target+0x510`, ~16 B PF); banks PF=32 only | `BestFitAllocator` / MSA (`xla_tpu_cmem_*`) | [cmem-pool.md](../memory/cmem-pool.md) |
| 5 | `smem` | 0 (`0x0`) | scalar memory: SPU spill/parameter store, loop counters, completion descriptors | `SmemSizeBytes()` (`Target+0x470`); word=`SmemWordSizeBytes()`=4 B (`Target+0x508`); banks JF2/PF8/VF8/GL8 | `BestFitAllocator` / `ProgramMemoryAllocator` (opcode-driven, **not** MSA) | [smem-scalar-memory.md](../memory/smem-scalar-memory.md), [smem-register-window.md](../memory/smem-register-window.md) |
| 6 | `sflag` | 204 (`0xCC`) | sync-flag register file: DMA-completion/barrier handshake words, atomic counter/done-bit | word-granular S32; `SflagWordSizeBytes()` (`Target+0x504`), log2 cached `Target+0x4c8`; `byte_off = 4·n` | `BestFitAllocator` (size) + fixed number-space partition | [sflag-protocol.md](../memory/sflag-protocol.md) |
| 7 | `imem` | 214 (`0xD6`) † | instruction memory: bundles the sequencer fetches | chip_parts | sequencer | [memory-space-enum.md](../isa/memory-space-enum.md) |
| 8 | `barna_core_bmem` | — | BarnaCore (embedding-engine) bulk scratchpad | chip_parts; PXC family only | BarnaCore | [memory-space-enum.md](../isa/memory-space-enum.md) |
| 9 | `barna_core_smem` | — | BarnaCore scalar scratchpad | `Target+0x47C` size, `+0x480` base, `+0x51C` word | BarnaCore (`BarnaCoreSflagImmPtr`, scoped trampoline) | [smem-scalar-memory.md](../memory/smem-scalar-memory.md) |
| 10 | `barna_core_sflag` | — | BarnaCore sync-flag tier (distinct from TC SFLAG) | `Target+0x478` (`BarnaCoreSflagSizeBytes`) | BarnaCore | [sflag-protocol.md](../memory/sflag-protocol.md) |
| 11 | `barna_core_imem` | — | BarnaCore instruction memory | chip_parts | BarnaCore | [memory-space-enum.md](../isa/memory-space-enum.md) |
| 12 | `sparse_core_sequencer_sflag` | 223 (`0xDF`) ‡ | SC-sequencer sync-flag bank | chip_parts; SC sequencer | SparseCore sequencer | [sflag-protocol.md](../memory/sflag-protocol.md) |
| 13 | `host` | — | host-resident buffer (transfer source/sink; MSA offload spill target) | host DRAM | `PremappedMemoryManager` / `tsl::BFCAllocator` over `posix_memalign` | [overview.md](../memory/overview.md) |
| 14 | `sparse_core_sequencer_smem` | 224 (`0xE0`) ‡ | SC-sequencer scalar scratchpad (well-known constants: `chip_id`, `replica_id`, …) | SCS SMEM 64 KiB hard immediate on VF/GL/GF | SparseCore sequencer | [smem-scalar-memory.md](../memory/smem-scalar-memory.md) |
| 15 | `sparse_core_private_stack_hbm` | 203 (`0xCB`) | per-SC private stack carved from HBM | HBM-backed | HBM-backed | [memory-space-enum.md](../isa/memory-space-enum.md) |
| 16 | `pinned_hbm` | — | page-pinned HBM for host-visible DMA (repacker may not relocate) | 1024 B DMA floor + host pin | HBM (pinned) | [hbm-dma-alignment.md](../memory/hbm-dma-alignment.md) |

> **NOTE —** † The AS-id column maps each TensorCore tier to the *physically corresponding* SparseCore pool, not to an identity. `imem`(LLO 7) ↔ SC `timem` (AS 214) and `sparse_core_sequencer_sflag`/`_smem` (LLO 12/14) ↔ SC `sflag_scs`/`smem_scs` (AS 223/224) are physical-identity correspondences, not arithmetic conversions. ‡ The SparseCore sequencer banks are the per-SCS variants; the AS-id band also carries per-tile (`AS217 sflag_tile`, `AS219 smem_tile`) and chip-shared (`AS202 spmem`) SC pools that have **no** LLO `MemorySpace` equivalent (they live only inside the SparseCore LLVM lowering). See the SparseCore AS section below.

> **GOTCHA —** the `MemorySpaceToString` table at `0x21ce6b08` does **not** stop at index 16. Indices 17/18/19 resolve to `absolute`, `heap_relative` (`0x8678cad`), and `stack_relative` (`0x8678cbb`) — pointer-*relativity* tags appended to the same string array, **not** memory pools. They belong to the `LloAddress` relocation model. A reimplementation that sizes the enum by the string-table length, or treats `absolute`/`heap_relative`/`stack_relative` as tiers, is wrong: the canonical region enum is exactly 17 values.

---

## The LLO MemorySpace Enum (TensorCore Operand Tag)

### Decoder and ground truth

`MemorySpaceToString` is the ground truth for the integer→region mapping: it is a single indexed load with no bounds check, so the enum value is a direct array index and the over-long table is shared with the relativity tags.

```c
// xla::jellyfish::MemorySpaceToString(MemorySpace ms)   sub_1D6FFAE0, 14 bytes
const char *MemorySpaceToString(int ms):
    return (&off_21CE6B08)[ms];        // off_21CE6B08[ms] — no bounds check
```

The ordinal assignment is re-verified four independent ways, all byte-exact and mutually consistent:

| Probe | Function | What it pins |
|-------|----------|--------------|
| String-table index | `MemorySpaceToString` @ `0x1d6ffae0` | `off_21CE6B08[ms]` flat lookup |
| DMA-render switch | `MemorySpaceToDriverResource` @ `0x1d6223e0` | input ordinals: `1=hbm,2=hib,3=vmem,4=cmem,5=smem,6=sflag,7=imem,8..11=barna_core_*,12..16=sparse_core_*` (FATAL on cmem + SC) |
| CMEM constant ctor | `LloAddress::MakeCmemConstant` @ `0x1d60ba20` | `LloAddress(MemorySpace=4, off)` → `cmem = 4` |
| SC-seq SMEM ctor | `LloAddress::MakeSparseCoreSequencerSmemConstant` @ `0x1d60bc60` | `LloAddress(MemorySpace=14, off)` → `sparse_core_sequencer_smem = 14` |

The `MemBanks(MemorySpace)` overrides independently confirm the mid-range ordinals: `GhostliteTarget::MemBanks` (`0x1d4969c0`) returns `32` for `ms==3` and `8` for `ms==5`, FATAL otherwise — i.e. `kVmem=3`, `kSmem=5`. `PufferfishTarget::MemBanks` (`0x1d493900`) indexes `qword_B5305C8[ms-3] = {16,32,8}` over `ms ∈ {3,4,5}` — i.e. vmem/cmem/smem = banks 16/32/8.

### The DMA-render numbering is a third, distinct integer space

`MemorySpaceToDriverResource` (`0x1d6223e0`) maps the LLO enum to a hardware driver-resource id stamped into a DMA descriptor's address word. It is *not* the enum value and it *traps* on `cmem` and the SparseCore spaces:

```c
// xla::jellyfish::MemorySpaceToDriverResource(MemorySpace ms)   sub_1D6223E0
function MemorySpaceToDriverResource(ms):
    switch ms:                       // ms = the 17-value LLO MemorySpace enum
        case 0 (<no space>): return 10
        case 1 (hbm):        return 2
        case 2 (hib):        return 3
        case 3 (vmem):       return 4
        case 4 (cmem):       FATAL("Unsupported memory space")   // memory_space.cc:31 — not DMA-addressable here
        case 5 (smem):       return 6
        case 6 (sflag):      return 0
        case 7 (imem):       return 5
        case 8  (barna_core_bmem):  return 7
        case 9  (barna_core_smem):  return 9
        case 10 (barna_core_sflag): return 1
        case 11 (barna_core_imem):  return 8
        case 12..16 (sparse_core_*): FATAL("Unsupported memory space")  // memory_space.cc:49
```

The `sflag → render id 6` ordering this switch implies is the same one `SflagImmPtr` (`0x1d5185a0`) bakes into its pointer: it passes render-space `6` to `ImmPtr` while the resulting operand still carries the `kSflag`(6) tag. A reimplementer must carry the `MemorySpace` enum end-to-end and convert to a driver-resource id *only* at the descriptor boundary via this explicit switch.

### Wire-format remap

LLO serializes through `MemorySpaceProto` (descriptor @ VA `0xbf8cc80`). The proto and the C++ enum name the same 17 spaces with **different** integers across `2`..`11` (`hib` is C++ 2 / proto 10; `vmem` is C++ 3 / proto 2; `cmem` is C++ 4 / proto 11); they agree at `0`, `1`, and `12`..`16`. The full remap table and the masked DMA-validity gates live on [memory-space-enum.md](../isa/memory-space-enum.md); a (de)serializer that conflates proto field numbers with the runtime enum silently relabels every `vmem`/`cmem`/`hib` buffer.

### The canonical assignment, four ways anchored

> **NOTE —** the byte-exact ordinal assignment is the 17-value table at the top of this page, anchored four independent ways above (`MemorySpaceToString`, `MemorySpaceToDriverResource`, `MakeCmemConstant`, `MakeSparseCoreSequencerSmemConstant`). The boundary cases a reimplementer most often gets wrong: `sflag = 6` (not 7) with `imem = 7`; `sparse_core_sequencer_sflag = 12` and `host = 13` and `sparse_core_sequencer_smem = 14` (the sequencer SFLAG/SMEM ordinals are *not* adjacent — `host` sits between them); `hib = 2` and `pinned_hbm = 16` (there is no `kPinnedHbm` at slot 2). [overview.md §2](../memory/overview.md#2-the-memoryspace-enum) carries the same `kNone=0 … kPinnedHbm=16` assignment.

---

## SparseCore Address Spaces

### Two number spaces, disjoint by construction

The SparseCore LLVM backend tags every pointer with a numeric **address-space ID** — the `N` in `!llvm.ptr<N>` — drawn from a sparse, banded range: `0` (inherited scalar memory), `201..225` (`0xC9..0xE1`, the SC-specific pools and `*Any` alias supersets), and `501`/`502` (`0x1F5`/`0x1F6`, the two CBREG circular-buffer windows). Each ID maps 1:1 onto a 1-based `mlir::sparse_core::MemorySpace` enum value (22 values, value-8 gap). The conversion is byte-exact and self-inverse:

```c
// AddressSpaceToMemorySpace(uint id)   sub_14B78800   (low 32 bits of 0x1_0000000N = MS)
// MemorySpaceToAddressSpace(MemorySpace ms)   sub_14B78780
//   guard: (ms-1) > 0x15 || ((0x3FFF7F >> (ms-1)) & 1) == 0  ->  FATAL("Unsupported memory space")
//   return dword_AF36CE8[ms-1]
```

The validity mask `0x3FFF7F` is the bit-set of the 22 valid `MemorySpace` values with the value-8 gap clear; `ms-1 > 0x15` bounds the table.

### The AS-ID master table

`MS#` is the 1-based `mlir::sparse_core::MemorySpace`; `tile?` is `IsOffTileMemory == false`, true only for MS 2 and MS 18. A blank MS# means the ID is an alias-analysis grouping or a reserved gap with no physical pool.

| AS# | hex | Pool (`stringifyMemorySpace`) | MS# | tile? | Notes |
|----:|-----|-------------------------------|----:|:-----:|-------|
| 0 | `0x00` | `smem` | 1 | off | inherited base TPU scalar memory |
| 201 | `0xC9` | `tile_spmem` | 2 | **ON** | per-tile SC SRAM (KB) |
| 202 | `0xCA` | `spmem` | 3 | off | chip-shared SC SRAM (MB) |
| 203 | `0xCB` | `hbm` | 4 | off | global (GB) embedding tables |
| 204 | `0xCC` | `sflag` | 5 | off | sync-flag memory (MS 22 `sflag_tc` also maps here) |
| 205 | `0xCD` | `vmem` | 6 | off | TC vector memory (TC↔SC handoff) |
| 206/207 | `0xCE`/`0xCF` | — | — | — | reserved gap |
| 208 | `0xD0` | `dreg` | 7 | off | data-register window |
| 209/210 | `0xD1`/`0xD2` | — | — | — | reserved gap |
| 211 | `0xD3` | — (`SflagAny`) | — | off | sflag may-alias superset (no pool) |
| 212 | `0xD4` | `smem_any` | 9 | off | smem may-alias superset |
| 213 | `0xD5` | `hbm_any` | 10 | off | hbm may-alias superset |
| 214 | `0xD6` | `timem` | 11 | off | per-tile instruction memory |
| 215 | `0xD7` | `simem` | 12 | off | SC instruction memory (empty `desc`) |
| 216 | `0xD8` | `iova` | 13 | off | I/O virtual address (GB) |
| 217 | `0xD9` | `sflag_tile` | 14 | off | per-tile sflag bank |
| 218 | `0xDA` | `spmem_any` | 15 | off | spmem may-alias superset |
| 219 | `0xDB` | `smem_tile` (`TileSmem`) | 16 | off | per-tile SMEM (KB) |
| 220 | `0xDC` | `mar` | 17 | off | memory-access-region (empty `desc`) |
| 221/222 | `0xDD`/`0xDE` | — | — | — | reserved gap |
| 223 | `0xDF` | `sflag_scs` | 20 | off | per-SCS sflag bank |
| 224 | `0xE0` | `smem_scs` | 21 | off | per-SCS SMEM (KB) |
| 225 | `0xE1` | — (`SflagAnySynctile`) | — | off | sflag-any-synctile (no pool) |
| 501 | `0x1F5` | `tile_spmem_cb` | 18 | **ON** | CBREG-windowed TILE_SPMEM |
| 502 | `0x1F6` | `smem_cb` | 19 | off | CBREG-windowed SMEM |

> **NOTE —** the on-tile gate is a single masked compare: `IsOffTileMemory(ms) = (ms & ~0x10) != 2` (`0x13d7ac00`). Clearing bit 4 folds MS 2 (`tile_spmem`) and MS 18 = `0x12` (`tile_spmem_cb`) together, so **only** those two are on-tile; every other pool requires a DMA/stream/sync to reach. This is the predicate the DMA and stream lowerings consult before selecting a data-movement intrinsic, and it is why a TEC needs the tile-id cast to turn an on-tile `TileSpmem`(201) pointer into an off-tile-addressable `Spmem`(202) pointer.

### The `*Any` may-alias canonicalisation

Four IDs (`211 SflagAny`, `212 SmemAny`, `213 HBMAny`, `218 SpmemAny`, plus the synthetic `225 SflagAnySynctile`) carry a description but **no** `MemorySpace` pool — they are alias-analysis groupings the backend widens a pointer to when its exact tile or core is statically unknown. `GetAnyTypeFromAddressSpace(int)` (`0x1357b400`) canonicalises a concrete ID to its wildcard; calling it on a leaf or already-wildcard space `LOG(FATAL)`s, so it is total only over the concrete spaces below:

| concrete ID (pool) | → canonical ID (superset) |
|---|---|
| 201 `tile_spmem`, 202 `spmem` | 218 `SpmemAny` |
| 203 `hbm` | 213 `HBMAny` |
| 204 `sflag` | 211 `SflagAny` |
| 205 `vmem` | 205 `vmem` (self — no separate wildcard) |
| 219 `smem_tile`, 0 `smem` | 212 `SmemAny` |

This is the SparseCore answer to the fat-pointer problem: a pointer into HBM/SPMEM whose owning tile is a runtime value cannot be proven disjoint from another, so the backend assigns both the `*Any` superset and lets alias analysis treat them as may-alias. The concrete-vs-`Any` distinction is what keeps statically-resolved tile-local accesses from being pessimised.

### The AS7/8/9 fat-pointer reserve is dead

The TPU `DataLayout` (@ `0x973de15`) carries a `p7:160:256:256:32-p8:128:128:128:48-p9:192:256:256:32 … ni:7:8:9` fragment — the 160/128/192-bit AMDGPU **buffer-fat-pointer** family — inherited verbatim because the TPU `TargetMachine` shares LLVM's AMDGPU ABI fragment. **No TPU or SparseCore op ever constructs an AS7/8/9 pointer.** A SparseCore pointer is at most a 64-bit LLVM ptr (default `p:64:64`) carrying a 32-bit word offset; the routing a fat pointer would pack into bits rides as separate SSA operands instead (`tpu_tileid` for on-tile TEC casts, destination-id for remote). The full negative result, the operand-arity split, and the value-preserving `addrspacecast` lowering are owned by [fat-pointers-as789.md](../sparsecore/fat-pointers-as789.md).

> **GOTCHA —** do **not** allocate SparseCore address-space numbers from `{7,8,9}`. Allocate from `{0, 201..225, 501, 502}`. The two ranges are disjoint, and a reimplementation that drives off the `p7/p8/p9` `DataLayout` entries will look for a constructor that does not exist.

---

## Alignment, Geometry, and the Allocator Model

### One allocator class, per-tier Config

Every runtime tier — HBM, VMEM, CMEM, SMEM, SFLAG — is a single `tpu::BestFitAllocator` instance (208 B, ctor `0x1e817500`), distinguished only by a 32-byte `MemoryAllocator::Config{base_offset, allocatable_range_end, alignment, granule}`. There is no `HbmAllocator`/`VmemAllocator`/`SmemAllocator` class and no per-`TpuVersion` branch inside the allocator: every per-codename divergence is *data* carried in `chip_parts.binarypb` and surfaced as the `Config` triple. The allocate/deallocate algorithm (boundary-tag SwissTable + size-ordered free RB-tree, best-fit `lower_bound`, eager bidirectional coalescing, no min-split-remainder) is documented once on [hbm-allocator.md](../memory/hbm-allocator.md).

### Per-tier alignment / geometry

| Tier | `base_offset` | `alignment` | `granule` | Geometry source |
|------|---------------|-------------|-----------|-----------------|
| HBM | 0 | **1024 B** DMA floor (`kHbmMinimumDmaAlignment`); **16 KiB** compile-time (`xla_jf_program_hbm_alignment_in_kib`) | chip_parts HBM granule | dual-quantum; DMA floor enforced at issue (`WritePremappedHbm`) + descriptor (`SetHbmAddress`, fatal) |
| VMEM | 0 | `VmemAlignmentBoundaryInBytes()` — `ChunkBytes` (JF) / `max(GranuleBytes, VmemWordSizeBytes)` (PF/VF/GL) | `VmemWordSizeBytes()` (`Target+0x50C`) | `ChunkBytes = 4·topology.word_count` (`0x1d619f40`) |
| CMEM | 0 | `CmemWordSizeBytes()` (`Target+0x510`, ~16 B PF) | `CmemWordSizeBytes()` | alignment == granule; Pufferfish only |
| SMEM | 0 | `SmemWordSizeBytes()` (4 B; `Target+0x508`) | `SmemWordSizeBytes()` | word-flat; `SmemWordImmPtr` asserts word == 4 B |
| SFLAG | 0 | `SflagWordSizeBytes()` (`Target+0x504`) | `SflagWordSizeBytes()` | `byte_off = 4·n` per flag; log2 cached `Target+0x4c8` |
| Host (premapped) | per-partition `partition_size·i` | 4 KiB if ≤ 2 MiB, else 2 MiB (`PickPageAlignment`) | = alignment | `PremappedMemoryManager` over `posix_memalign` |
| Host (BFC offload) | 0 | ≥ 16 B (`posix_memalign`) | 2 MiB region growth | `tsl::BFCAllocator` (256 GiB cap) |

> **GOTCHA —** HBM has **two** alignment numbers and confusing them silently corrupts a DMA. `kHbmMinimumDmaAlignment = 1024 B` is the *hardware* floor: every DMA site masks with `& 0x3FF` and rejects a non-zero remainder (recoverable `RetCheck` at issue, fatal `CHECK` at descriptor). The 16 KiB compile-time figure rounds every program-level HBM tensor up *before* MSA places it. The 1024-B floor is the wire contract; the 16-KiB rule is the placement contract. See [hbm-dma-alignment.md](../memory/hbm-dma-alignment.md).

> **NOTE —** the on-chip tiers (VMEM/CMEM/SMEM/SFLAG) all set `alignment == granule == <tier>WordSizeBytes()` and `base_offset == 0`. Only HBM separates alignment from granule, and only the host premapped manager uses a non-zero `base_offset`. The numeric per-codename word/byte sizes live in `chip_parts.binarypb` and are **not** in `.text`; the *formulas* and field offsets above are exact, the literals are not asserted. The `(sublane, lane)` on-chip tile geometry that buffers pad to — `(8, 128)` on Trillium/v5+, `(16, 128)` on v4 — is owned by [tpu-buffer-layout.md](../memory/tpu-buffer-layout.md).

### Per-generation on-chip bank counts

The one piece of on-chip geometry that *is* baked into `.text` (not `chip_parts`) is the bank count, returned by the per-`Target` `MemBanks(MemorySpace)` virtual. The bank index for a byte offset `B` is `(B / <tier>WordSizeBytes) mod MemBanks(tier)`. Banking is an access-scheduling property, not an allocation property — the allocator hands out byte offsets and the LLO bundle packer derives the `(bank, sub-bank)` coordinate at issue time. Decompile-confirmed:

| Target (gen) | VMEM (MS 3) | CMEM (MS 4) | SMEM (MS 5) | `MemBanks` accessor |
|---|---:|---:|---:|---|
| JellyfishTarget (v2) | 8 | — (`LogFatal`) | 2 | `0x1d48fc80` |
| PufferfishTarget (v4) | 16 | **32** | 8 | `0x1d493900` (`qword_B5305C8[ms-3]={16,32,8}`) |
| ViperfishTarget (v5p) | 32 | — (`LogFatal`) | 8 | `0x1d4999c0` |
| GhostliteTarget (v6e) | 32 | — (`LogFatal`) | 8 | `0x1d4969c0` |

Pufferfish is the only generation where `MemBanks(kCmem)` returns a value rather than `LogFatal` — the structural marker that CMEM is a real tier only on Pufferfish (PXC, TPU v4). Viperfish (`0x1d4999c0`) is graded HIGH (symbol-table body, source-identical to the confirmed Ghostlite shape, not separately re-read).

### MSA management is VMEM/CMEM-only

Only VMEM (and CMEM on Pufferfish) is MSA-managed — the `kAlternate`/`kDefault` tug-of-war that colors `HloValue`s. SMEM is placed by scalar load/store **opcode semantics** (the operand declares `MemorySpace=kSmem`); SFLAG is placed out of a fixed **number-space partition**, never the byte heap. All tiers nonetheless flow through the same `ProgramMemoryAllocator` → `ProgramMemoryMetadata_Allocation` proto → `CreateFromProto` → `BestFitAllocator` hand-off. A reimplementer who routes SMEM/SFLAG through the MSA cost model will mis-place them.

### The buffer-layout sequencer-SMEM label

> **NOTE —** the `ShapeSizeBytesRaw` (`0x1d6add40`) untiled-dense branch tests `ColorToMemorySpace(layout.memory_space) == 12`. The constant `12` is `sparse_core_sequencer_sflag` in the canonical LLO enum, **not** `sparse_core_sequencer_smem` (which is `14`, byte-confirmed by `MakeSparseCoreSequencerSmemConstant` @ `0x1d60bc60`) — the two are off-by-one neighbours and easy to mislabel. The branch routes a `sparse_core_sequencer_sflag`-colored buffer to the dense, untiled byte-size path. `ColorToMemorySpace` (`0x1d6ffb00`) is a `byte_B5435CA[color]` remap with `color < 0xA`, so its *output* is the canonical `MemorySpace` enum — the `12` is an enum value, not a raw layout color.

---

## Cross-References

- [memory-space-enum.md](../isa/memory-space-enum.md) — the 17-value LLO `MemorySpace` enum, the `MemorySpaceToString` decoder, the proto↔enum remap, and the masked DMA-validity gates; the authority for the ordinals on this page
- [overview.md](../memory/overview.md) — the six-region taxonomy, the universal `BestFitAllocator`, and the compile-time→runtime hand-off; its §2 carries the same canonical `kNone=0 … kPinnedHbm=16` ordinal assignment used here
- [hbm-allocator.md](../memory/hbm-allocator.md) — the universal best-fit allocate/deallocate algorithm shared by every tier
- [hbm-dma-alignment.md](../memory/hbm-dma-alignment.md) — the 1024-B DMA floor vs. the 16-KiB compile-time program alignment
- [vmem-allocator.md](../memory/vmem-allocator.md) — the `kAlternate` fast tier; per-gen VMEM size/word/bank/alignment formulas
- [cmem-pool.md](../memory/cmem-pool.md) — the Pufferfish-only constant-memory operand pool; `MemBanks(kCmem)=32`, the `xla_tpu_cmem_*` family
- [smem-scalar-memory.md](../memory/smem-scalar-memory.md) — the SPU scalar tier (`kSmem`=5); `SmemWordImmPtr`, opcode-driven placement, the BarnaCore SMEM sibling
- [smem-register-window.md](../memory/smem-register-window.md) — why no SMEM register window exists; the flat 32-entry SREG file and CBREG/OperandWindow disambiguation
- [sflag-protocol.md](../memory/sflag-protocol.md) — the sync-flag atomic tier (`kSflag`=6); the `4·n` stride, counter/done-bit semantics, the `Vsync*`/`Vwait*` primitives
- [tpu-buffer-layout.md](../memory/tpu-buffer-layout.md) — how a logical XLA buffer maps to padded, tiled physical offsets in these tiers (its §4 sequencer-SMEM `12` label is the `sparse_core_sequencer_sflag` enum value, not SMEM — see the note above)
- [address-space-ids.md](../targets/address-space-ids.md) — the full SparseCore AS-ID table, the `*Any` may-alias canonicalisation, and `CheckAddressSpaces`
- [fat-pointers-as789.md](../sparsecore/fat-pointers-as789.md) — the dead AS7/8/9 fat-pointer reserve and the actual 64-bit/32-bit-word SparseCore pointer representation
- [chip-parts-binarypb.md](../targets/chip-parts-binarypb.md) — the boot-time resource that supplies the per-codename size/word/granule literals absent from `.text`
- [per-gen-comparison-matrix.md](per-gen-comparison-matrix.md) — the per-generation feature/geometry comparison this table feeds
- [back to index](../index.md) — Part XVII — Appendices
