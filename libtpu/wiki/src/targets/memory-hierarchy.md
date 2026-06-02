# Memory Hierarchy

> *Every size, offset, and enum value on this page was decoded byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

A TPU TensorCore sees a flat, software-managed scratchpad hierarchy, not a transparent cache. Five on-chip tiers sit between the off-chip HBM and the compute units: **HBM** (the device-global backing store), **VMEM** (the vector working set, the analogue of an L1/shared-memory scratchpad), **SMEM** (the scalar/sequencer scratchpad), **SFLAG** (the dedicated sync-flag tier the DMA engines and the barrier primitives poll), and **CMEM** (a Pufferfish-only second large scratchpad). There is no hardware coherence and no automatic eviction: a kernel explicitly DMAs tiles between tiers, and the compiler's allocators (Part X) place every buffer in exactly one space. This page is the orientation map for those tiers — what they are, how big each one is on each generation, and how the runtime represents them — and it hands off to Part X for the allocator internals and to Part VI for the ISA-level addressing.

Every tier's runtime size and word geometry is one field of the single per-device `xla::jellyfish::Target` object. `Target::Init` fills those fields at boot from the embedded `<codename>_chip_parts.binarypb` proto (see [Per-Codename Constants](per-codename-hw-constants.md)), and the rest of the compiler reads them through trivial accessors — `Target::HbmSizeBytes()` is literally `return *(int64_t*)(this + 0x450)`. The same `Target` carries the bank counts (a separate set of C++ `MemBanks` literals, not proto fields) and the per-tier word size used to convert byte offsets to hardware word addresses. So the hierarchy is not an abstraction layered over the silicon constants — it *is* those constants, indexed by tier.

The tier set is named twice in the binary, by two distinct enumerations that a reimplementer must keep separate. The compiler-side `xla::jellyfish::MemorySpace` enum (`MemorySpaceToString` table at `0x21CE6B08`, 18 entries) names every space a buffer can live in, including the BarnaCore and SparseCore sub-spaces and `host`/`pinned_hbm`/`absolute`. The LLVM-level **address-space IDs** ([Address-Space IDs](address-space-ids.md)) are the numbers the LLO/LLVM IR carries in `addrspace(N)`. The two are related but not identical, and the cost-model DMA dispatcher uses yet a third, narrower numbering (Hbm=1, Vmem=3, Cmem=4, Smem=5) for the same physical tiers. This page anchors the physical tiers to all three.

For orientation, the contract is:

- **The five physical tiers**, their role, and which generations have each.
- **The per-generation size / word / bank table** — kept consistent with [Per-Codename Constants](per-codename-hw-constants.md), which is the authoritative source.
- **The `Target` field map** — which struct offset and accessor backs each tier, so the runtime view is reconstructable.
- **The enum relationships** — `MemorySpace` int, the DMA-dispatcher int, and the address-space ID for each tier.

| | |
|---|---|
| **Runtime carrier** | `xla::jellyfish::Target` object (one per device); tiers at `Target+0x450..+0x510` |
| **Boot population** | `Target::Init` (region `0x1d610c20..`) from `TpuChipParts::FromProto` (`0x20b1b400`) |
| **Tier accessors** | `HbmSizeBytes 0x1d615320`, `VmemSizeBytes 0x1d615e00`, `SmemSizeBytes 0x1d615e40`, `CmemSizeBytes 0x1d615e20`, `SflagSizeBytes 0x1d615e60` |
| **Space enum** | `MemorySpaceToString` table `0x21CE6B08` (18 entries, indices 0..17) |
| **DMA bandwidth dispatcher** | `Target::LocalDmaBandwidth(MemorySpace,MemorySpace)` `0x1d6168e0` |
| **Tiers** | HBM · VMEM · SMEM · SFLAG · CMEM (v4-only) |
| **Generations** | jellyfish v2 / dragonfish v3 / pufferfish v4 / viperfish v5p·v5e / ghostlite v6e / `6acc60406` v7x |

---

## The Five Tiers

Each tier is a physically distinct on-chip (or, for HBM, on-package) memory with its own width, capacity, and access cost. The diagram sketches the data paths a TensorCore sees; every arrow is an explicit DMA priced by `LocalDmaBandwidth`, never an implicit cache fill. The table below is the orientation summary; the per-generation sizes follow in the next section, and the cross-tier bandwidth matrix lives in the [Memory Bandwidth & Latency Model](../cost/memory-bandwidth-latency-model.md).

```text
                       off-package / per-die
            +-------------------------------------------+
            |                  HBM                      |  Target+0x450 (int64)
            |   16..96 GiB · word 32..1024 B · MS=1     |  HbmSizeBytes 0x1d615320
            +----+--------------------+-----------------+
                 | DMA                | DMA (v4: staged via VMEM)
                 v                    v
   per-TensorCore scratchpads     +---------------------+
  +-----------------------+       |   CMEM  (v4 only)   |  Target+0x460 (int64)
  |   VMEM                |<----->|  128 MiB · word 512 |  CmemSizeBytes 0x1d615e20
  |  16..128 MiB          |  DMA  |  MS=4               |  (==0 ⇒ absent)
  |  word 512 B · MS=3    |       +---------------------+
  +-----------+-----------+
              | DMA
              v
  +-----------------------+   +-----------------------+
  |   SMEM                |   |   SFLAG               |  Target+0x468 (int32)
  |  16 KiB..1 MiB        |   |  1..16 KiB            |  SflagSizeBytes 0x1d615e60
  |  word 4 B · MS=5      |   |  word 4 B · MS=6/7    |  (sync-flag words;
  |  Target+0x470 (int32) |   |  polled by DMA/barrier)  BarnaCore SFLAG @+0x478)
  +-----------------------+   +-----------------------+
        scalar operands           completion / sync
```


| Tier | Role | Scope | Word | First gen | Confidence |
|---|---|---|---|---|---|
| **HBM** | Device-global backing store; all program inputs/outputs, spill, embeddings | whole chip / per-die | 32 B – 1024 B (per gen) | v2 (all) | CONFIRMED |
| **VMEM** | Vector working set — the tiled scratchpad MXU/VPU operands stage through | per TensorCore | 512 B (all gens) | v2 (all) | CONFIRMED |
| **SMEM** | Scalar / sequencer scratchpad — addresses, loop bounds, scalar operands | per TensorCore | 4 B (all gens) | v2 (all) | CONFIRMED |
| **SFLAG** | Sync-flag tier — the DMA/barrier completion words polled for cross-engine sync | per TensorCore | 4 B (all gens) | v2 (all) | CONFIRMED |
| **CMEM** | Second large scratchpad (`SharedMemory[CMEM]`) — staging buffer above VMEM | whole chip | 512 B | v4 only | CONFIRMED |

> **NOTE —** the tiers are software-managed scratchpads, not caches. A reimplementation that models VMEM/SMEM as a coherent cache backed by HBM is wrong: the only data path between tiers is an explicit DMA, priced by `LocalDmaBandwidth`. The compiler chooses each buffer's home tier; nothing migrates a buffer at runtime.

### HBM — device-global store

HBM is the only tier large enough to hold a whole program's tensors. Its size, word, and clock are proto fields (`SharedMemory[HBM]`), materialized into `Target+0x450` (`int64`, `HbmSizeBytes`), with the per-stack clock at `Target+0x910` and full-chip bandwidth at `Target+0x4f0` (`HbmFullChipBytesPerSecond`). The **HBM word grows narrower across generations** — 1024 B on v2/v3, 512 B on v4, then 32 B from v5p on — which is the same value the DMA path uses as its granule (`DmaRequirements.granule_bytes`); see [HBM DMA Alignment](../memory/hbm-dma-alignment.md). The allocator that carves HBM is [HBM Allocator](../memory/hbm-allocator.md); the embedded tcmalloc that sub-allocates host-visible regions is [Embedded tcmalloc](../memory/embedded-tcmalloc.md).

### VMEM — vector working set

VMEM is the per-TensorCore scratchpad the MXU and vector units read their operands from; it is the TPU's analogue of GPU shared memory, but explicitly allocated by the compiler rather than declared per-kernel. `VmemSizeBytes` (`0x1d615e00`) reads `Target+0x458` as an **`int32`** (a `movslq` sign-extends it), and the VMEM word is a fixed **512 B on every generation** (`Target+0x50c`, `VmemWordSizeBytes`). VMEM capacity climbed 16 → 64 → 128 MiB across generations. The placement/coloring engine is [VMEM Allocator](../memory/vmem-allocator.md), and the per-buffer tile shape it consumes is [TPU Buffer Layout](../memory/tpu-buffer-layout.md).

### SMEM — scalar scratchpad

SMEM holds scalars: loop induction variables, computed addresses, sequencer constants. It is tiny relative to VMEM (16 KiB on v2/v3, 1 MiB from v4) and addressed in **4-byte words** (`Target+0x508`, `SmemWordSizeBytes`). `SmemSizeBytes` is at `Target+0x470` (`int32`). The register-window view of SMEM and its allocator are [SMEM Register Window](../memory/smem-register-window.md) and [SMEM Scalar Memory](../memory/smem-scalar-memory.md).

### SFLAG — sync-flag tier

SFLAG is a dedicated small memory of 4-byte sync-flag words that the DMA engines increment on completion and that barrier/wait primitives poll. It is first-class — it has its own `Target` size field (`Target+0x468`, `int32`, `SflagSizeBytes 0x1d615e60`), its own word size (`Target+0x504`) and log2 (`Target+0x4c8`), and its own bank ladder — exactly so the sync protocol can address it without colliding with data tiers. SFLAG is 1 KiB on v2/v3, 2 KiB on v4–v6e, and jumps to **16 KiB on v7x**. The wait/clear protocol that consumes it is [SFLAG Protocol](../memory/sflag-protocol.md), and the barrier binding is [Barrier-to-SFLAG Binding](../barrier/barrier-to-sflag-binding.md).

> **QUIRK —** there are **two** physical SFLAG tiers in `Target`, not one. The main chip SFLAG (size `+0x468`, word `+0x504`) coexists with a separate **BarnaCore SFLAG** (size `Target+0x478`, accessor `BarnaCoreSflagSizeBytes 0x1d615f80`) that only exists when the `HasBarnaCore` vtable predicate (`vtable+0x258`) is true — i.e. on v2/v3/v4. BarnaCore also owns its own SMEM (`BarnaCoreSmemSizeBytes`, base `BarnaCoreSmemBaseBytes`). A reimplementer targeting the v2–v4 embedding path must allocate both SFLAG regions; targeting v5p+ there is only the one.

### CMEM — the Pufferfish second scratchpad

CMEM is a 128 MiB chip-level `SharedMemory[CMEM]` present **only on Pufferfish (v4)**. `CmemSizeBytes` (`0x1d615e20`) reads `Target+0x460` (`int64`); on every other generation that field is `0`, and the canonical CMEM-presence test in the binary is exactly `target().CmemSizeBytes() > 0`. The CMEM pool allocator and its VMEM-staged transfer model are [CMEM Pool](../memory/cmem-pool.md).

> **GOTCHA —** "CMEM is v4-only" is encoded *three* independent ways, and all three must agree in a reimplementation: (1) `CmemSizeBytes` is `0` except on v4; (2) only `PufferfishTarget::MemBanks` has a CMEM (space-4) entry, every other gen `LOG(FATAL)`s on it; (3) only the v4 `chip_parts` blob carries a `SharedMemory[CMEM]`. There is also no `LocalDmaBandwidthHbmToCmem` virtual — the cost-model dispatcher routes `(Hbm,Cmem)` into the `VmemToVmem` slot, because HBM→CMEM is modeled as HBM→VMEM→CMEM staging.

---

## Per-Generation Sizes

This is the orientation copy of the size/word/bank rows; the authoritative, fully-sourced master table (with bandwidth, clocks, MXU geometry, register files, SparseCore tiers) is [Per-Codename Constants](per-codename-hw-constants.md). The values here are the proto-decoded sizes (`bytes_per_word × word_count`) for the data tiers and the C++ `MemBanks` literals for the bank counts. All CONFIRMED unless flagged.

| Tier / field | v2 JF | v3 DF | v4 PF (std / lite) | v5p / v5e VF | v6e GL | v7x | Confidence |
|---|---|---|---|---|---|---|---|
| **HBM size** | 16 GiB | 32 GiB | 32 / 8 GiB | 96 / 16 GiB | 31.5 GiB | 95 / 190 GiB | CONFIRMED |
| HBM word | 1024 B | 1024 B | 512 B | 32 / 512 B | 32 B | 32 B | CONFIRMED |
| **VMEM / TensorCore** | 16 MiB | 16 MiB | 16 MiB | 64 / 128 MiB | 128 MiB | 64 MiB | CONFIRMED |
| VMEM word | 512 B | 512 B | 512 B | 512 B | 512 B | 512 B | CONFIRMED |
| **SMEM / TensorCore** | 16 KiB | 16 KiB | 1 MiB | 1 MiB | 1 MiB | 1 MiB | CONFIRMED |
| SMEM word | 4 B | 4 B | 4 B | 4 B | 4 B | 4 B | CONFIRMED |
| **SFLAG / TensorCore** | 1 KiB | 1 KiB | 2 KiB | 2 KiB | 2 KiB | 16 KiB | CONFIRMED |
| SFLAG word | 4 B | 4 B | 4 B | 4 B | 4 B | 4 B | CONFIRMED |
| **CMEM (chip)** | absent | absent | 128 MiB | absent | absent | absent | CONFIRMED |
| VMEM banks | 8 | 8 | 16 | 32 | 32 | 32 | CONFIRMED |
| SMEM banks | 2 | 2 | 8 | 8 | 8 | 8 | CONFIRMED |
| CMEM banks | FATAL | FATAL | 32 | FATAL | FATAL | FATAL | CONFIRMED |

Three discontinuities are worth memorizing because they break a "scale everything linearly" reimplementation:

- **SMEM jumps 64×** at v4 (16 KiB → 1 MiB) and then holds flat — the scalar scratchpad stopped being a bottleneck after Pufferfish.
- **SFLAG jumps 8×** at v7x (2 KiB → 16 KiB), reflecting the much larger sync-flag fan-out of the v7 collective fabric.
- **VMEM is non-monotonic** — it peaks at 128 MiB on the single-TensorCore lite/v6e dies (which pack more VMEM per core) and is *back down* to 64 MiB on v7x. VMEM-per-TensorCore is not a proxy for generation.

> **NOTE —** the bank counts are *not* proto fields. They are C++ literals in the per-codename `*Target::MemBanks` overrides: `JellyfishTarget` (`0x1d48fc80`) returns 8/–/2 for spaces 3/–/5; `PufferfishTarget` (`0x1d493900`) indexes the `.rodata` table `0xB5305C8 = {16, 32, 8}` for spaces 3/4/5; Dragonfish overrides none and inherits Jellyfish. A `MemBanks` call on a space the generation lacks is a hard `LOG(FATAL)`, not a zero — which is how CMEM-on-v2 is made unrepresentable rather than merely empty.

---

## The `Target` Field Map

The runtime view of the whole hierarchy is one contiguous block of `Target` struct fields filled by `Target::Init` from the chip-parts proto. Every tier accessor is a one-instruction getter, verified against the decompiled bodies: `HbmSizeBytes` is `return *((int64_t*)this + 138)` (= `+0x450`), `VmemSizeBytes` is `return *((int*)this + 278)` (= `+0x458`), `SflagSizeBytes` is `return *((uint*)this + 282)` (= `+0x468`). The map below is the field layout a reimplementation must reproduce so the same accessors resolve to the same offsets.

| Target off | Accessor (VA) | Type | Tier datum | Confidence |
|---|---|---|---|---|
| `+0x438` / `+0x448` | user-alloc shared-mem limit clamp | int64 | HBM / scoped (CMEM) user-alloc cap | HIGH |
| `+0x450` | `HbmSizeBytes` (`0x1d615320`) | int64 | HBM size | CONFIRMED |
| `+0x458` | `VmemSizeBytes` (`0x1d615e00`, `movslq`) | int32 | VMEM size | CONFIRMED |
| `+0x460` | `CmemSizeBytes` (`0x1d615e20`) | int64 | CMEM size (0 ⇒ absent) | CONFIRMED |
| `+0x468` | `SflagSizeBytes` (`0x1d615e60`) | int32 | SFLAG size | CONFIRMED |
| `+0x470` | `SmemSizeBytes` (`0x1d615e40`) | int32 | SMEM size | CONFIRMED |
| `+0x478` | `BarnaCoreSflagSizeBytes` (`0x1d615f80`) | int32 | BarnaCore SFLAG (v2–v4, gated by `HasBarnaCore`) | CONFIRMED |
| `+0x4c8` / `+0x4cc` / `+0x4d0` | Sflag/Smem/Vmem `WordSizeLog2` | int32 | per-tier word log2 (byte→word shift) | CONFIRMED |
| `+0x4f0` | `HbmFullChipBytesPerSecond` (`0x1d6172a0`) | int64 | HBM bandwidth | CONFIRMED |
| `+0x4f8` | `CmemFullChipBytesPerSecond` (`0x1d6172c0`) | int64 | CMEM bandwidth (0 off-v4) | CONFIRMED |
| `+0x504` / `+0x508` / `+0x50c` / `+0x510` | Sflag/Smem/Vmem/Cmem `WordSizeBytes` | int32 | per-tier word size | CONFIRMED |
| `+0x90c` / `+0x910` | TC freq / HBM freq MHz | int32 | clocks (boot-filled, `0xFFFFFFFF` sentinel pre-init) | CONFIRMED |

The word-size pairs (`WordSizeBytes` + `WordSizeLog2`) exist because every tier address the ISA produces is a *word* address, not a byte address: a buffer's byte size is converted to a hardware word count by `>> WordSizeLog2`, and the allocator asserts each buffer is a multiple of the tier word. The bounds-check assertions that gate addressing are visible verbatim in the binary as `byte_address < target().HbmSizeBytes()`, `< target().VmemSizeBytes()`, `< target().SmemSizeBytes()`, `< target().SflagSizeBytes()` — one per data tier — which is the cleanest evidence that each tier's `Target` size field is the addressing ceiling, not merely a capacity hint.

---

## Tiers, `MemorySpace`, and Address Spaces

A reimplementer hits three distinct numberings for the same five physical tiers. Keeping them straight is the single most error-prone part of the memory model, so they are tabulated together here; the full enum and the LLVM address-space band live on their own pages.

### The three numberings

The compiler-side `xla::jellyfish::MemorySpace` enum is the string table at `0x21CE6B08`, indexed directly by the enum value (`MemorySpaceToString(e)` is literally `off_21CE6B08[e]`). The cost-model DMA dispatcher `Target::LocalDmaBandwidth` uses a narrower set of the *same* integers — its decompiled body XORs the argument against `1` (Hbm), `3` (Vmem), `4` (Cmem), `5` (Smem) to pick a vtable offset, confirming those four enum values for the data tiers. The LLVM-level address-space IDs are a separate numbering carried in `addrspace(N)` and detailed on [Address-Space IDs](address-space-ids.md).

| Physical tier | `MemorySpace` name | enum int | DMA-dispatcher int | Confidence |
|---|---|---|---|---|
| HBM | `hbm` (also `kDefault`) | 1 | 1 | CONFIRMED |
| VMEM | `vmem` | 3 | 3 | CONFIRMED |
| CMEM | `cmem` | 4 | 4 | CONFIRMED |
| SMEM | `smem` | 5 | 5 | CONFIRMED |
| SFLAG | `sflag` | 6 (string table) / 7 (dispatcher refs) | — | HIGH |
| IMEM (instr.) | `imem` | 7 (string table) | — | HIGH |
| BarnaCore SMEM | `barna_core_smem` | 9 | — | CONFIRMED |
| BarnaCore SFLAG | `barna_core_sflag` | 10 | — | CONFIRMED |
| SC sequencer SFLAG | `sparse_core_sequencer_sflag` | 12 | — | CONFIRMED |
| host / pinned / absolute | `host` / `pinned_hbm` / `absolute` | 13 / 16 / 17 | — | CONFIRMED |

> **CORRECTION (MEM-HIER-1) —** the SFLAG / IMEM enum *index* is not perfectly pinned across the sources. The `MemorySpaceToString` string table at `0x21CE6B08` (consumed by `MemorySpaceToString(e) = off_21CE6B08[e]`) places `sflag` at index 6 and `imem` at index 7, while the DMA-dispatcher cross-references and the SFLAG-protocol analysis read `sflag` as enum int 7. The two differ by the presence of an extra slot near `hib` (index 2). The *data-tier* values (Hbm 1, Vmem 3, Cmem 4, Smem 5) are unambiguous — they are confirmed by the `LocalDmaBandwidth` XOR constants — so a reimplementation that hardcodes those four is safe; the exact SFLAG/IMEM index should be re-pinned against the `0x21CE6B08` table before relying on it (marked HIGH, not CONFIRMED).

### SFLAG's sub-spaces

SFLAG is the one tier that splits into scoped sub-spaces at the MLIR level, because synchronization happens at three granularities: the global cross-engine `sflag` (the only one reachable cross-CORE — TC↔SC, SC↔SC, TC↔TC, and remote), the per-tile `sflag_tile` (TEC sub-engine scope), and the per-SCS `sflag_scs`. These are distinct `mlir::sparse_core::MemorySpace` values, asserted by the verifier; they are detailed in [SFLAG Protocol](../memory/sflag-protocol.md), not duplicated here. The point for the hierarchy map is that the single physical SFLAG tier backs all three scopes — the sub-spaces are addressing scopes, not separate memories.

### SparseCore tiers

From v5p the SparseCore brings its own memory family — SPMEM, TILESPMEM, and per-SCS SMEM/SFLAG — that is parallel to, not part of, the TensorCore hierarchy described here. Those sizes are tabulated in [Per-Codename Constants](per-codename-hw-constants.md) (the SC rows) and the BarnaCore↔SparseCore pivot is [BarnaCore overview](../barnacore/overview.md). They are out of scope for this TensorCore-tier orientation page beyond noting that `SPMEM→HBM` is a *separate* DMA-bandwidth virtual not reachable through the TensorCore `LocalDmaBandwidth` dispatcher.

---

## Related Components

| Name | Relationship |
|---|---|
| `Target::Init` | fills `Target+0x450..+0x510` from `chip_parts`; the boot source of every tier size |
| `TpuChipParts::FromProto` (`0x20b1b400`) | decodes the `chip_parts` proto whose fields become the tier sizes |
| `*Target::MemBanks` | C++ source of the per-tier bank counts (the only non-proto integers here) |
| `Target::LocalDmaBandwidth` (`0x1d6168e0`) | the (src,dst) tier → bandwidth dispatcher; the consumer of the tier enum ints |
| `MemorySpaceToString` (`0x1d6ffae0`) | the enum→name table that names every tier and sub-space |

## Cross-References

- [Per-Codename Constants](per-codename-hw-constants.md) — the authoritative, fully-sourced master table these sizes are drawn from (bandwidth, clocks, MXU geometry, SparseCore tiers)
- [Address-Space IDs](address-space-ids.md) — the LLVM `addrspace(N)` numbering for these tiers, incl. the SparseCore fat-pointer bands
- [MemorySpace Enum](../isa/memory-space-enum.md) — the full 18-entry `MemorySpace` enum and its string table
- [MemorySpace Table](../appendix/memory-space-table.md) — the same enum as a reference appendix
- [Memory Subsystem Overview](../memory/overview.md) — Part X entry point: how the tiers are allocated and managed
- [HBM Allocator](../memory/hbm-allocator.md) — the device-global store allocator
- [VMEM Allocator](../memory/vmem-allocator.md) — the vector-scratchpad placement/coloring engine
- [SMEM Scalar Memory](../memory/smem-scalar-memory.md) — the scalar-tier allocator and register window
- [SFLAG Protocol](../memory/sflag-protocol.md) — the sync-flag wait/clear protocol that drives SFLAG
- [CMEM Pool](../memory/cmem-pool.md) — the Pufferfish-only second-scratchpad pool
- [Memory Bandwidth & Latency Model](../cost/memory-bandwidth-latency-model.md) — the cross-tier `LocalDmaBandwidth` matrix and DMA latencies
