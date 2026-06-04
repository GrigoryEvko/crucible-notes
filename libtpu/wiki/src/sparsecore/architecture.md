# SparseCore Hardware Architecture

> *Every address, offset, and value on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

This page is the **hardware model the SparseCore backend targets** — the geometry and memory layout a reimplementer must reconstruct before any SparseCore (SC) program can be scheduled or any on-chip buffer placed. Three things define that model, and the compiler reads all three from one runtime object: the **`SparseCoreTarget` sub-descriptor** parked at `Target+0x948`. It pins how many tile-execute cores a chip has, how wide each is, how big the on-chip SRAM tiers are, and where the hardware-reserved regions sit. The byte-exact field map of that struct is owned by [SparseCoreTarget (`Target+0x948`)](../targets/sparsecore-target-descriptor.md); this page synthesises it into the *architecture* — the engine→core layout, the four-tier memory model, and the bump/stack tile-allocation algorithm that turns geometry into placed buffers.

The mental model is closest to a **GPU's SM with shared memory**, but inverted. Where an SM streams contiguous tiles into a large register file, an SC streams *indirect* (index-driven) embedding rows into a tiny per-tile SRAM (TILE_SPMEM) and reduces them with a wide vector engine (the TEC). The compute fabric is partitioned into three VLIW sub-engines — SCS (scalar control), TAC (tile-fetch DMA issuer, present only on Viperfish/Ghostlite), and TEC (wide vector) — that coordinate through sync flags and a shared SRAM. The number of TEC cores per chip (`SparseCoreTiles`), each core's vector width (`SparseCoreLaneCount`), and the per-tier capacities are *not* code constants: `SparseCoreTarget::Init` reads them from the per-codename `TpuCoreParts` (ultimately the gzipped `chip_parts.binarypb`). The only code constants are the literals `HBM 4b-word = 4`, `SCS group count = 2`, and the SPMEM stripe granularity `= 32 bytes`.

The page is structured as four units: the **SparseCoreTarget map** (the geometry source-of-truth and the accessor gate), the **engine→core layout** (how SCS/TAC/TEC map onto physical cores and tiles), the **on-chip memory model** (the four address spaces, word sizing, and the SPMEM↔TILE_SPMEM split), and the **tile-allocation algorithm** (the per-address-space bump allocator with a scope stack, the tile sub-frame carve-out, embedding row sharding, the minibatch-fit inequality, and HBM spill). Each closes with the per-generation deltas, of which there is exactly one true code branch.

For reimplementation, the contract is:

- **Geometry is data-driven through `SparseCoreTarget` (`Target+0x948`).** A reimplementer must populate `SparseCoreTiles` (`[0x948]+0x90`), `SparseCoreLaneCount` (`[0x948]+0x94`), `stream_granule_size` (`[0x948]+0xA4`), and the per-tier capacity/word block (`[0x948]+0x10..+0x54`) from the chip's core-parts before any allocation runs. The four-tier memory sizes are read from there, not hard-coded.
- **The presence gate is a runtime topology test.** Every `Target::SparseCore*` accessor dispatches the virtual `SupportsSparseCore` (vtable slot `+0x260`, `0x1D48FD40`) and `LOG(FATAL)`s `"SparseCore is not supported by this target"` if the `TpuTopology[+0x98]` SC count is zero. There is no compile-time class test.
- **On-chip allocation is a bump allocator with a scope stack — never best-fit.** `AllocationAssignmentPass` walks `memref.alloca` ops and bump-reserves words per address space; tile-local buffers get a dedicated sub-frame carved out of the SPMEM bump pointer at a vector-lane-stripe boundary, reclaimed on pop. `Deallocate` is a no-op.
- **There is exactly one per-gen code branch in the allocator.** The circular-buffer-in-last-TILE_SPMEM-entry hardware-bug guard, which fires for DeepseaVersion 3 (Viperfish) and 4 (Ghostlite). Everything else — capacities, word widths, tile count, TAC presence — is data-driven.

| | |
|---|---|
| **Geometry source** | `std::unique_ptr<SparseCoreTarget>` at `Target+0x948`; built by `SparseCoreTarget::Init` (`0x1D612B20`) from the `SPARSE_CORE` `TpuCoreParts` |
| **Presence gate** | `Target::SupportsSparseCore` (`0x1D48FD40`, vtable slot `+0x260`) — reads `TpuTopology[+0x98] > 0` |
| **Per-chip tile count** | `SparseCoreTiles` = `[0x948]+0x90` = `TpuCoreParts::SequencerCount(seq-type 5 = TEC)` (v7x `6acc60406` = 16) |
| **Per-tile vector width** | `SparseCoreLaneCount` = `[0x948]+0x94` = `SequencerParts(5).vector_isa().lane_count` (v7x = 16) |
| **Engines** | SCS (scalar, 32 B bundle) · TAC (tile-fetch DMA, 64 B; VF/GL only) · TEC (vector, 64 B; one TEC = one tile) |
| **On-chip SRAM tiers** | HBM (GB) · SPMEM (MB, chip-wide) · TILE_SPMEM (KB, per-tile) · TIMEM (per-tile instr); SMEM/SFLAG per scope |
| **Allocator** | `AllocationAssignmentPass` (`0x134D8240`) — per-address-space bump + scope stack; `Deallocate` elided |
| **SPMEM stripe** | 32 bytes (`SparseCoreSpmemStripeGranularityBytes` = `0x1D499440` → `optional<32>`) |
| **Alloc alignment** | `SpmemAlignment` = `(SparseCoreTiles × lane_count) / 4` words (`0x13DC5500`) |

---

## The SparseCoreTarget Map (`Target+0x948`)

### Purpose

`xla::jellyfish::Target` is the per-generation hardware descriptor the XLA TPU backend queries for every codegen and cost decision. Its SparseCore half is not inline — it is a separately allocated `std::unique_ptr<SparseCoreTarget>` at `Target+0x948`, built once by `SparseCoreTarget::Init` (`0x1D612B20`) and installed by `Target::Init`. This sub-object is the single source of truth for SC geometry: tile count, lane width, per-tier SRAM capacities and word widths, the embedding param-region base, the reserved-region windows, and the per-gen capability bits. The compiler never hard-codes per-gen sizes; it reads them from here.

> **NOTE —** the complete byte-exact field map (every `+0xNN` offset, type, and Init store site) lives on [SparseCoreTarget (`Target+0x948`)](../targets/sparsecore-target-descriptor.md). This page documents only the *architecture-relevant* fields and how the allocator and engine layout consume them. Treat that page as the authority for any offset not reproduced here.

### Entry Point

```text
Target::Init                                       ── builds + installs the sub-object
  └─ SparseCoreTarget::Init (0x1D612B20)           ── populate geometry from SPARSE_CORE TpuCoreParts
       ├─ TpuCoreParts::SequencerCount(cp, 5=TEC)  ── SparseCoreTiles  → [0x948]+0x90
       ├─ SequencerParts(5).vector_isa().lane_count── SparseCoreLaneCount → [0x948]+0x94
       └─ MemoryParts(TILESPMEM/SPMEM/SMEM/SFLAG)  ── per-tier capacity + word-size block [0x948]+0x10..+0x54

Target::SparseCore*  (accessor surface)            ── each gates on vtable[+0x260] then loads [0x948]+field
  └─ Target::SupportsSparseCore (0x1D48FD40)       ── TpuTopology[+0x98] > 0  (the presence gate)
```

### Algorithm

`SparseCoreTarget::Init` (`0x1D612B20`) — the geometry block, byte-confirmed against the decompile (decimal offsets are the decompiler's; hex in comments):

```c
function SparseCoreTarget_Init(sc, core_type, topology, target, core_parts):  // 0x1D612B20
    sc[88]  = 4;                                  // +0x58 HBM 4b-word size (literal)
    sc[116] = 2;                                  // +0x74 SCS sequencer/group count (literal)
    sc[144] = TpuCoreParts.SequencerCount(core_parts, 5);   // +0x90 SparseCoreTiles (seq type 5 = tile-exec)
    vi      = TpuSequencerParts.vector_isa(core_parts.SequencerParts(5));
    if vi.has_vector_isa == 0: FATAL;             // tile-exec sequencer must carry a VectorIsa
    sc[148] = vi.lane_count;                       // +0x94 SparseCoreLaneCount
    sc[152] = vi.lane_count << 2;                  // +0x98 SC lane bytes
    if core_parts[488 /*+0x1E8 SparseCore submsg present*/]:
        sc[160] = core_parts[480];                 // +0xA0 tile_hbm_bandwidth_bytes_per_cycle (+0x1E0)
        sc[164] = core_parts[484];                 // +0xA4 stream_granule_size (+0x1E4)
    // per-tier capacity/word block (+0x10..+0x54) populated from MemoryParts(type) above;
    // barrier sync-flag base/count (+0x1D0/+0x1D4) from a SpecialPurposeSyncFlags sub-object.
```

> **QUIRK — two `TpuSequencerType` numberings; `Init` uses the codec-template one.** The geometry is keyed on **`TpuSequencerType` value 5 = TEC** in the *codec-template* enum `{SCS=3, TAC=4, TEC=5}`. This is the index space that `TpuCoreParts::SequencerCount`/`SequencerParts` (`0x20B2AA20`/`0x20B2AA60`) actually take — both `bittest64` a 6-slot presence mask and `ud1`-trap on `index >= 6`, so the valid range is `0..5` and slot 5 is the tile-execute pool. `Init` reads `SequencerCount(5)`/`SequencerParts(5)` to size tiles and lanes, and the `chip_parts` proto labels that 16-instance pool as the tile-execute geometry. The C++ `TpuSequencerType` enum that `TpuSequencerTypeToString` renders uses this **same** codec-template numbering (`off_22010DE0[3]="SparseCoreSequencer"`, `[5]="…TileExecuteCoreSequencer"`). The *separate* numbering is the **protobuf** enum `TpuSequencerTypeProto`, which reserves `INVALID=0` and so numbers the same engines `{SCS=4, TAC=5, TEC=6}` — one *greater* than the C++ enum. The `chip_parts` proto stores its field as a `TpuSequencerTypeProto`, but `TpuSequencerParts::FromProto` (`0x20b30700`) runs it through `TpuSequencerTypeFromProto` (subtract one) **before** it ever indexes core-parts, so the in-memory `TpuCoreParts` is keyed on the codec-template `{3,4,5}` enum. A reimplementer must index `TpuCoreParts` with `{3,4,5}` (the one `Init` uses); feeding it a raw proto `{4,5,6}` ordinal reads the wrong pool. See [getSequencerType](getsequencertype.md) for the full off-by-one reconciliation.

### Accessor Surface and the Presence Gate

Every `Target::SparseCore*` accessor follows one shape: dispatch the `+0x260` virtual gate, `LOG(FATAL)` if false, then load one scalar from `[0x948]+field`. The decompiler renders `Target+0x948` as `*((_QWORD*)this + 297)` (`297 × 8 = 0x948`). The representative body for `SparseCoreTiles` (`0xFAAFA40`):

```c
function Target_SparseCoreTiles(this):              // 0xFAAFA40
    if !vtable[+0x260](this):                        // SupportsSparseCore
        LOG(FATAL) << "SparseCore is not supported by this target";  // target.h:1704
    return *(u32*)(this[297] + 0x90);                // [0x948] + 0x90  (v7x = 16)
```

| Accessor | Address | Reads | v7x value |
|---|---|---|---|
| `SparseCoreTiles` | `0xFAAFA40` | `[0x948]+0x90` | 16 |
| `SparseCoreLaneCount` | `0xF7906E0` | `[0x948]+0x94` | 16 |
| `SparseCoreHbm4bWordSizeBytes` | `0x1320C220` | `[0x948]+0x58` | 4 |
| `SparseCoreStreamGranuleSizeBytes` | `0x13886EE0` | `[0x948]+0xA4` | 4 |
| `GetSparseCoreBarrierSyncFlagCount` | `0x10972FA0` | `[0x948]+0x1D4` | — |
| `SupportsSparseCore` | `0x1D48FD40` | `TpuTopology[+0x98] > 0` | true |
| `SparseCoresPerLogicalDevice` | `0x135159C0` | `CoresPerChip / LogicalDevicesPerChip` | 2 |

> **GOTCHA —** the gate is a *runtime topology* test, not a class test. `SupportsSparseCore` reads `*(u32*)(TpuTopology* /*Target+0x3B8*/ + 0x98) > 0`, not the `[0x948]` pointer. A reimplementation must set the SparseCore-count field of its topology descriptor *before* any SC accessor is reachable; otherwise the whole accessor surface traps with the `target.h:1704` FATAL. On the BarnaCore generations (Jellyfish/Dragonfish/Pufferfish) and TC-only lite dies, that count is zero, so `Target::Init` never builds the sub-object.

---

## Engine → Core Layout

### Purpose

The SC compute fabric is three VLIW sub-engines, each a separate machine with its own bundle stream and codec. The architecture-relevant fact is how these map onto *physical cores and tiles*: one TEC sequencer is one tile, the tile count is `SparseCoreTiles`, and the SCS/TAC/TEC roster is per-generation.

### Layout

```text
                     ┌──────────────────────── SparseCore (one of SparseCoresPerLogicalDevice) ─────────────┐
                     │                                                                                      │
  codec-tmpl type 3  │  SCS  (1 control sequencer, 32 B bundle)                                             │
  (SPARSE_CORE_SEQ)  │   ├─ program counter, address arithmetic, circular buffers                          │
                     │   ├─ chip-register reads (GTC, tile id, sparse-core id, DMA credits)                 │
                     │   └─ atomic + sync-flag slot (coordinates tiles ↔ tiles, SC ↔ TC)                    │
                     │                                                                                      │
  codec-tmpl type 4  │  TAC  (tile-access core, 64 B bundle; VF/GL ONLY — absent on 6acc60406)              │
  (SPARSE_CORE_TILE_ │   └─ stream slot issues tile-fetch DMA  HBM[base + idx·stride] → TILE_SPMEM          │
   ACCESS_CORE_SEQ)  │      (no FPU, no vector ALU, no vector load/store — pure address + DMA)              │
                     │                                                                                      │
  codec-tmpl type 5  │  TEC  ×SparseCoreTiles  (tile-execute cores, 64 B bundle — one TEC = one tile)       │
  (SPARSE_CORE_TILE_ │   ├─ 3 vector ALU slots + vector load/store + vector-extended + vector-result        │
   EXECUTE_CORE_SEQ) │   ├─ vector_isa.lane_count lanes (= SparseCoreLaneCount, read at SequencerParts(5))  │
                     │   └─ on 6acc60406: TEC stream slot also issues tile-fetch DMA (TAC role absorbed)    │
                     └──────────────────────────────────────────────────────────────────────────────────── ┘
```

The chip carries `SparseCoreTiles` TEC cores; each owns one TILE_SPMEM window (the per-tile working set the vector ALU computes over) plus one TIMEM (per-tile instruction memory). A single 64-byte TEC bundle issues three vector ALU ops, a vector load, a vector store, a vector-extended op (scan/sort/uniquify), a vector-result pop, two scalar ALU slots, immediates, a DMA, and a stream slot — all in one cycle, which is why the engine is the widest compute surface in all of SparseCore.

### Per-Generation Roster

The engine roster is *not* constant across silicon. The discriminator is the per-codename codec family; the presence or absence of a `SparseCore<Engine>CodecBase` class is a direct binary readout.

| Gen | Codename | Family ns | SCS | TAC | TEC | Tile-fetch issuer | SCs/TC | SCs/chip |
|---|---|:---:|:---:|:---:|:---:|---|:---:|:---:|
| v5p | Viperfish | `vxc.vfc` | Y | Y | Y | TAC stream | 4 | 8 |
| v6e | Ghostlite | `gxc.glc` | Y | Y | Y | TAC stream | 4 | 8 |
| v7x | `6acc60406` | `gxc.gfc` | Y | **–** | Y | TEC stream (no TAC) | 4 | 4 |

> **QUIRK —** the `6acc60406` (gfc) generation collapses the SCS+TAC+TEC three-engine pipeline to SCS+TEC. The `gfc` namespace has **zero** `SparseCoreTac*` symbols; TEC absorbs the address-generation + DMA-issue duties through its own stream slot (`IndirectStream` / `IndirectVregStream` / `LinearStream` / `StridedStream`), and SCS computes gather addresses that TEC reads via `tile_wait_scs_smem`. For tile allocation this changes *who consumes* the tile, not *how it is allocated* — the bump allocator never knew about TAC. See [TAC Engine](tac-engine.md) for the absorbed-role detail and [SCS (Scalar) Engine](scs-engine.md) / [TEC (Vector) Engine](tec-engine.md) for the per-engine bundle surfaces.

### Considerations

The Jellyfish/Dragonfish/Pufferfish generations carry **no** SparseCore (they had the retired BarnaCore embedding accelerator). On those targets the presence gate returns false and the entire SC architecture described here is absent. The number of SCs per chip is `SparseCoreCountPerTensorCore × tensor_cores_per_chip`; `SparseCoreCountPerTensorCore` (`0x1C6CB760`) computes `sparse_core_count_per_chip / LogicalDevicesPerChip` and asserts both `sparse_core_count_per_chip >= tensor_core_count_per_chip` (lowering_util.cc:4488) and `… % … == 0` (line 4489) — the integer 4:1 SC:TC ratio.

---

## On-Chip Memory Model

### Purpose

SC owns four address-space tiers of decreasing scope and increasing speed. The compiler addresses every SC SRAM tier in **words**, where the word width is per-tier and per-gen, read from the `SparseCoreTarget` capacity/word block. The architecture-critical fact is that TILE_SPMEM and SPMEM share the same physical SRAM — TILE_SPMEM is a stripe carved out of SPMEM, not a separate bank.

### The Four Tiers

| Tier | Scope | Size class | LLVM addrspace | Capacity field | Word-size field | Use |
|---|---|---|---|---|---|---|
| HBM | Chip-wide (shared with TC) | GB | 203 | `Target::HbmSizeBytes` | `[0x948]+0x38` 4b-word count | Embedding tables, gradient buffers, spill |
| SPMEM | All SC cores on chip | MB | 202 | `[0x948]+0x2C` | `[0x948]+0x4C` | Cross-SC comms, large buffers, tile backing store |
| TILE_SPMEM | Per-tile (one TEC) | KB | 201 | `[0x948]+0x28` | `[0x948]+0x48` | Local working set the vector ALU computes over |
| TIMEM | Per-tile instruction memory | small | 214 | `[0x948]+0x10` | `[0x948]+0x50` | Tile-local kernel code |

SC also exposes per-scope **SMEM** (scalar memory, addrspace 0; per-tile `smem_tile` 219 in TEC scope, per-SCS `smem_scs` 224) and **SFLAG** (sync-flag pool, addrspace 204; per-tile `sflag_tile` 217, per-SCS `sflag_scs` 223). The `mlir::sparse_core::MemorySpace` enum (1-based) maps to an LLVM address space through a 22-entry table at `.rodata 0xAF36CE8`, decoded by `MemorySpaceToAddressSpace` (`0x14B78780`); the scope (SCS vs TEC/tile) selects which physical addrspace a logical `spmem`/`timem` request lands in.

### SPMEM ↔ TILE_SPMEM Split

```text
SPMEM (chip-wide SC SRAM, capacity [0x948]+0x2C)
│
├─ divided evenly into SparseCoreTiles regions:  total_spmem_bytes % num_tiles == 0
│                                                 num_tiles <= sequencer_count_
│                                                 num_tiles % num_groups == 0
│
└─ one tile's TILE_SPMEM window  = a stripe of SPMEM, aligned to a full vector-lane stripe
       private tile base        = SPMEM bump offset / SparseCoreTiles   (per-tile partition; 0x134DD340)
       stripe granularity        = 32 bytes   (SparseCoreSpmemStripeGranularityBytes, 0x1D499440)
       allocation alignment       = SpmemAlignment = (SparseCoreTiles × lane_count) / 4 words  (0x13DC5500)
```

`SpmemAlignment` (`0x13DC5500`) returns `*(u32*)(Target[0x948] + 0x90) × lane_count / 4` — the `+0x90` tile field times the vector lane count, divided by 4. The intent is that every SPMEM/TILE_SPMEM allocation is rounded to a full vector-lane stripe so the TEC's vector load/store always hits aligned addresses; the matching alignment-in-bits check rejects tile stores whose `numElements × elementBitWidth` is not a multiple of `SpmemAlignmentInBits`.

> **NOTE —** the byte-exact decompile (`0x13DC5500`) computes `*(u32*)(SparseCoreTarget[+0x90]) × lane_count / 4`, where the `+0x90` field is the tile count (the `SequencerCount(5)` slot) and `lane_count` is fetched through the `SparseCoreTarget` virtual at vtable slot `+0xD0`. A reimplementer should compute the numerator from the *actual two geometry fields* (`+0x90` and the vector lane count), not from a hard-coded constant. (The decompile gates the whole accessor on `SupportsSparseCore()` — a `target.h:1704` FATAL — and returns 1 when the lane-count optional is absent.)

The per-tile sync-flag budget is derived in `Init` as `tec_sflag_capacity_bytes / sflag_bytes_per_word` (byte-confirmed: `[a5+0x18] / [a5+0x40]`, where `[a5+0x18]` is `tec_sflag_parts.word_count × bytes_per_word` and `[a5+0x40]` is `scs_sflag_parts.bytes_per_word`); the four compiler-reserved sync-flag watermarks are that quotient `− {1,2,3,4}` (stored at `[a5]+0x1EC/0x1F0/0x1F4/0x1FC`) and reserve the TAC↔TEC (or, on `6acc60406`, SCS↔TEC) handshake flags out of the user pool. See [Memory Hierarchy](../targets/memory-hierarchy.md) for the cross-engine address-space catalog.

### Considerations

Embedding tables are **not** placed in SC SRAM. They live in HBM and are streamed into TILE_SPMEM per lookup. "Table allocation" is therefore HBM placement (handled by the runtime BestFit allocator + memory-space assignment, outside SC scope) plus the logical-replica row sharding documented below. The on-chip tiers hold only the transient per-window working set.

---

## Tile-Allocation Algorithm

### Purpose

Once geometry is known, the SC-MLO compiler must place every intermediate buffer into the four on-chip tiers. It does **not** use the runtime HBM/VMEM best-fit allocator. The SC on-chip allocator is a **per-address-space bump allocator with a scope stack**, implemented entirely in the `AllocationAssignmentPass` MLIR pass. There is no free list, no coalescing, no splitting; `Deallocate` is a no-op (`TileOverlayAllocationPass::ElideDeallocs` literally removes every `memref.dealloc`).

### Entry Point

```text
AllocationAssignmentPass::runOnOperation (0x134D8240)   ── SC tile/SMEM/SFLAG bump-allocate driver
  ├─ reset allocation_stack_ to frame 0 (root)
  ├─ pre-reserve HW-reserved regions in addrspaces {20 timem-SCS, 14 sflag-tile, 21 timem-other}
  ├─ param_table_size_ = GetParamTableSize(module)       ── 0x13DABDA0 (sc.param_table_size attr)
  ├─ IterateOverFunctions (twice): global+SCS, then per-func allocations
  │    └─ WalkOpsForAllocations → Allocate(memref, core, is_circular_buffer)  ── 0x134DB1E0
  │         ├─ GetMemRefSize (0x134DC1C0)                ── byte→word + alignment round-up
  │         └─ MemoryUsage::Reserve (0x134D9700)          ── the bump primitive
  ├─ for each sc_tpu.tile_task: PushToStackForTileAllocations (0x134DD340) → allocate → PopStack
  └─ emit sc.alloc_high_water_mark + sc.execute_alloc_high_water_mark

GlobalAllocationAssignmentPass::DoAllocations (0x1351BF80)  ── capacity check + llvm_tpu.spill_ranges
TileOverlayAllocationPass::runOnOperation (0x136025E0)      ── TIMEM overlay bump alloc
PrepareHbmSpillPass::runOnOperation (0x135F3B60)            ── per-SC HBM spill stack
```

### Algorithm — the bump primitive

`MemoryUsage::Reserve` (`0x134D9700`) is the core, byte-confirmed against the decompile: it looks up the current bump pointer for the address space in an `absl::flat_hash_map<int /*addrspace*/, long /*words*/>`, returns it as the new allocation's base, and advances it by the request size.

```c
function MemoryUsage_Reserve(usage, memory_space, size):   // 0x134D9700
    addrspace = MemorySpaceToAddressSpace(memory_space)    // enum → LLVM addrspace (0x14B78780)
    slot      = usage_map.find_or_insert(addrspace)         // flat_hash_map<int,long>
    base      = slot.value                                  // current bump pointer (words)
    slot.value = base + size                                // bump
    return base                                             // base offset of the new allocation
```

### Algorithm — `Allocate` and the circular-buffer (VF/GL) guard

```c
function Allocate(memref, opt_core, is_circular_buffer):    // 0x134DB1E0
    space = GetMemorySpace(memref.type)
    // remap (space, core_scope) → effective addrspace; core arrives as 0x100000000|core:
    //   0x100000000 = SCS scope, 0x100000002 = TEC/tile scope
    //   timem + SCS → 20 ; timem + TEC → 14 ; spmem + SCS → 21 ; spmem + TEC → 16
    //   reject smem/sflag outside the remap with FATAL:
    //     "memory_space != MemorySpace::smem && memory_space != MemorySpace::sflag"
    size  = GetMemRefSize(memref)                            // 0x134DC1C0 (words, alignment round-up)
    base  = top_frame.Reserve(eff_space, size)              // bump
    last  = base + size - 1
    hi    = GetUserAllocatableWordOffsets(target, eff_space) // 0x13DABC00 — per-space user window upper bound
    if last > hi:
        error("current allocation offset upper bound ({last} words) exceeds the legitimate "
              "user allocatable offset upper bound ({hi} words) in memory space {space} ...")
    // THE ONLY PER-GEN CODE BRANCH (byte-exact gate at 0x134DB1E0:195):
    //   guard fires iff (DeepseaVersion − 3) <= 1, i.e. version in {3,4} = Viperfish OR Ghostlite
    if (uint)(target[+0x398 /*DeepseaVersion*/] - 3) <= 1 && is_circular_buffer && last > hi - 8:
        emitOpError("Attempting to allocate circular buffer into last entry of TileSpmem. "
                    "This will result in an out-of-bounds tile-local stream on VFC due to a HW bug. ...")
    return base
```

`GetMemRefSize` (`0x134DC1C0`) computes `byte_size = numElements × elementBitWidth / 8`, checks the byte size is a multiple of the per-tier word size (else `"memref is not padded correctly"`), divides to words, and rounds up to `WordAlignmentInBytes / WordSizeInBytes`.

### Algorithm — the scope stack and tile sub-frame

The allocator keeps `allocation_stack_`, a `std::vector<MemoryUsage>` of 40-byte frames. `PushToStack` (`0x134DA8C0`) appends a frame that **copies the parent's bump-pointer map**, so a nested scope allocates above the parent's high-water marks; `PopStack` (`0x134DAB00`) drops the top frame (the nested allocations are reclaimed by stack discipline) and asserts `allocation_stack_.size() >= 2` — frame 0 is the persistent root.

`PushToStackForTileAllocations` (`0x134DD340`) pushes a *tile* sub-frame and carves TILE_SPMEM out of the SPMEM bump pointer, byte-confirmed:

```c
function PushToStackForTileAllocations(is_shared):          // 0x134DD340
    PushToStack()
    top.tile_spmem_snapshot = top[addrspace 0x10]           // snapshot spmem-TEC bump ptr
    top.tile_sflag_snapshot = top[addrspace 0x0E]           // snapshot sflag-tile bump ptr
    top.tile_unused         = 0
    spmem_base  = top[addrspace 3]                          // current SPMEM bump pointer
    spmem_align = SparseCoreTarget[+0x90] * lane_count / 4   // = SpmemAlignment (words)
    CHECK(spmem_base % spmem_align == 0)
        // "prev_frame[kSpmemAddressSpace] % LlvmTpuDialect::SpmemAlignment(target_) == 0"  (alloc pass:737)
    if is_shared:  tile_base = top[addrspace 2]             // shared TILE_SPMEM window
    else:          tile_base = top[addrspace 3] / SparseCoreTarget[+0x90]
                                                            // private tile window = SPMEM offset / SparseCoreTiles
                                                            // (the SPMEM bump ptr divided by the tile count — partitions SPMEM into per-tile windows)
    top[addrspace 2] = tile_base                            // set the tile bump pointer (MemoryUsage::operator[](…,2))
```

> **QUIRK —** "tile fetch" and "tile evict" are not RAM operations — they are bump-and-pop. Fetching the next tile is a bump-allocate into the next free TILE_SPMEM window; evicting is `PopStack`, which rewinds the bump pointer so tile N+1 reuses tile N's freed offsets. There is no separate tile-cache SRAM; TILE_SPMEM *is* the cache, and eviction is implicit in the stack discipline. A reimplementer who models a separate cache with explicit eviction will mis-size the working set.

### Embedding Row Sharding

Because tables live in HBM, the only "per-SC" placement is row sharding. `GetLogicalReplicaInfo` (`0x13CA1AE0`) solves for `{logical_replica_count, feature_dim_split_factor, sample_dim_split_factor}` under two invariants, both byte-confirmed as CHECK strings:

```text
logical_replica_count % physical_sparse_cores == 0          (logical_replica_util, line ~147)
RoundUpToPowerOf2(logical_replica_count) == logical_replica_count   (line ~186)
```

Each table is split into `logical_replica_count` row-shards; row `r` lands in shard `r mod logical_replica_count`; shards map onto the `physical_sparse_cores` round-robin. Because the count is a power of two that divides the SC count (`GetNumSparseCores`, `0x13C9EBA0` = `num_partitions × num_replicas × SCs_per_TC`), the per-SC row count is uniform.

### Minibatch Fit

A lookup window's variable-size IDs must fit in TILE_SPMEM. `CalculateVariableSizeWords` (`0x13CA3F20`) computes the fit inequality, byte-confirmed by its error string:

```c
// byte-exact at 0x13CA3F20: a3 = max_nz_per_row, a4 = logical_replicas
max_nz_per_row_partitioned = max(ceil(a3 / a4), SparseCoreLaneCount)   // clamp floor = [0x948]+0x94, the lane count (v7x = 16) — NOT sparse_cores_per_chip
variable_size_words        = num_variable_size_allocations × max_nz_per_row_partitioned  // num_variable_size_allocations = virtual method (*(estimator+0x38))()
require variable_size_words <= tile_spmem_words
    // "Variable size allocations (%d * %d = %d words) do not fit in TileSpmem (%d words)."
```

If the batch's IDs exceed this, mini-batching subdivides the sample batch into windows that each fit. `num_variable_size_allocations` is the pipeline buffer count (a double-buffer ⇒ 2); the six `allocation_estimator` subclasses each size their own pipeline-stage buffer.

### HBM Spill

When the on-chip working set exceeds SPMEM/TILE_SPMEM, `PrepareHbmSpillPass` (`0x135F3B60`) provisions a per-SC HBM-backed spill stack of `FLAGS_xla_sc_hbm_spill_stack` 4-byte words (a 1-D i32 memref in addrspace 4/HBM) plus an 11-word SMEM scratch pad, captured by `sc_tpu.hbm_spill_stack_capture`. `DoAllocations` (`0x1351BF80`) emits `llvm_tpu.spill_ranges` (`{smemStart/Limit, tilespmemStart/Limit}`) marking which offsets are resident vs spilled. With spill disabled (`flag == 0`), an over-large allocation is a hard compile error (`"TileSpmem high-water mark exceeds memory capacity"`).

### Function Map

| Function | Address | Role |
|---|---|---|
| `AllocationAssignmentPass::runOnOperation` | `0x134D8240` | bump-allocate driver (reserve HW regions, walk allocas, walk tile tasks) |
| `…::Allocate(MemRefType, opt<Core>, bool)` | `0x134DB1E0` | allocate one memref → base word offset; the VFC guard |
| `…::GetMemRefSize(MemRefType)` | `0x134DC1C0` | byte→word + padding/alignment round-up |
| `…::MemoryUsage::Reserve(MemorySpace, long)` | `0x134D9700` | the bump primitive (flat_hash_map) |
| `…::PushToStack` / `PopStack` | `0x134DA8C0` / `0x134DAB00` | scope-frame push (copy parent ptrs) / pop (`size >= 2`) |
| `…::PushToStackForTileAllocations(bool)` | `0x134DD340` | tile sub-frame; carve TILE_SPMEM from SPMEM at stripe boundary |
| `GlobalAllocationAssignmentPass::DoAllocations` | `0x1351BF80` | capacity check + `llvm_tpu.spill_ranges` emit |
| `TileOverlayAllocationPass::runOnOperation` | `0x136025E0` | TIMEM overlay bump alloc; elide deallocs |
| `PrepareHbmSpillPass::runOnOperation` | `0x135F3B60` | per-SC HBM spill stack |
| `lowering_util::GetUserAllocatableWordOffsets` | `0x13DABC00` | per-space user-window upper bound |
| `xla_mlo_util::{WordSizeInBytes,WordAlignmentInBytes,CapacityInBytes}` | `0x14A89D00` / `0x14A89E20` / `0x14A89EE0` | per-tier word width / alignment / capacity |
| `mlir::sparse_core::MemorySpaceToAddressSpace` | `0x14B78780` | enum → LLVM addrspace (table `0xAF36CE8`) |
| `LlvmTpuDialect::SpmemAlignment` | `0x13DC5500` | `(tiles × lane_count) / 4` words |
| `logical_replica_util::GetLogicalReplicaInfo` | `0x13CA1AE0` | row-shard / replica-count solver (pow-2, divides #SC) |
| `logical_replica_util::GetNumSparseCores` | `0x13C9EBA0` | total SC count across the mesh |
| `lowering_util::SparseCoreCountPerTensorCore` | `0x1C6CB760` | `SCs_per_chip / TCs_per_chip` (4:1) |
| `VariableWindowAllocationEstimator::CalculateVariableSizeWords` | `0x13CA3F20` | the TILE_SPMEM minibatch-fit inequality |

### Per-Generation Deltas

| Aspect | Viperfish (VF) | Ghostlite (GL) | `6acc60406` (GF) |
|---|---|---|---|
| Allocator algorithm | bump + stack | bump + stack | bump + stack (identical) |
| SCs per TC / per chip | 4 / 8 | 4 / 8 | 4 / 4 |
| Tile-fetch issuer | TAC stream | TAC stream | TEC stream (no TAC) |
| Access/Execute scope split | TAC + TEC | TAC + TEC | SCS + TEC (`tile_wait_scs_smem`) |
| TILE_SPMEM / SPMEM word + capacity | chip_parts (`[0x948]+0x48/+0x28`, `+0x4C/+0x2C`) | chip_parts | chip_parts |
| SPMEM stripe granularity | 32 B | 32 B | 32 B |
| `SpmemAlignment` (words) | `tiles·lane/4` | `tiles·lane/4` | `tiles·lane/4` |
| Circular-buffer-in-last-entry | **HW bug — guarded** | **HW bug — guarded** | OK |
| HBM spill | yes (flag) | yes (flag) | yes (flag) |

> **GOTCHA —** the *only* true per-generation code branch in the entire allocator is the circular-buffer-in-last-TILE_SPMEM-entry guard. Its gate (byte-exact, `0x134DB1E0:195`) is `(uint)(Target[+0x398 DeepseaVersion] − 3) <= 1`, i.e. it fires for DeepseaVersion **3 (Viperfish) and 4 (Ghostlite)** — *not* Viperfish alone — together with `is_circular_buffer` and `last > hi − 8`. The diagnostic text names "VFC" (the chip where the HW bug was first characterised), but the code path applies the guard on both VF and GL; only `6acc60406` (version 5) is exempt. Every other per-gen difference — capacities, word widths, tile count, TAC presence, SCs-per-chip — is data-driven through `SparseCoreTarget`/`TpuCoreParts`, with no code branch. A reimplementer should treat the allocator as generation-agnostic and push all per-gen variation into the geometry descriptor.

---

## Related Components

| Name | Relationship |
|---|---|
| `SparseCoreTarget::Init` (`0x1D612B20`) | populates the geometry struct this page's architecture is read from |
| `Target::SupportsSparseCore` (`0x1D48FD40`) | the presence gate every accessor and the allocator dispatch through |
| `AllocationAssignmentPass` (`0x134D8240`) | the bump/stack allocator that turns geometry into placed buffers |
| `SparseCoreHierarchicalSpmdPartitioner` | pads SC program I/O to the logical-replica shard boundary (SPMD plumbing) |

## Cross-References

- [SparseCore Overview](overview.md) — the navigational entry for Part IX; engine names, per-gen presence, the data path.
- [SparseCoreTarget (`Target+0x948`)](../targets/sparsecore-target-descriptor.md) — the byte-exact field map, the 24 virtual accessors, and the per-codename MXU table this page synthesises.
- [SC Backend Pipeline](sc-backend-pipeline.md) — the SC-MLO pass pipeline that runs `AllocationAssignmentPass` (and the MEGACORE barrier).
- [SC Core Selection](sc-core-selection.md) — how a computation is assigned to a physical SparseCore.
- [getSequencerType](getsequencertype.md) — the SCS/TAC/TEC engine-selection function and the sequencer-type enum.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — partitioning an SC computation into per-engine bundle streams.
- [SCS (Scalar) Engine](scs-engine.md) · [TAC Engine](tac-engine.md) · [TEC (Vector) Engine](tec-engine.md) — the three sub-engine bundle surfaces.
- [GetSparseCoreConfig](getsparsecoreconfig.md) — the offload op-type configuration the backend reads alongside this geometry.
- [Memory Hierarchy](../targets/memory-hierarchy.md) — the cross-engine address-space catalog the SC tiers slot into.
- [Per-Codename Constant Table](../targets/per-codename-hw-constants.md) — the `chip_parts`-sourced per-gen memory/core-count table the geometry is decoded against.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
