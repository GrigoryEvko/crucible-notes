# Memory Hierarchy Overview

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions will differ.*

## Abstract

A TPU program touches six addressable memory regions, and `libtpu.so` names every one of them with a single C++ enumeration (`xla::jellyfish::MemorySpace`) and services every one of them — except the host heap — with a single allocator class (`tpu::BestFitAllocator`). The hierarchy is, from abundant-and-far to scarce-and-near: **HBM** (off-chip DRAM, tens of GiB, the `kDefault` tier), then four on-chip SRAM tiers per TensorCore — **VMEM** (vector memory, the `kAlternate` fast-staging tier the MXU/VPU read operands from), **CMEM** (constant memory, a Pufferfish-only read-mostly operand pool), **SMEM** (scalar memory, the SPU's private spill/parameter store), and **SFLAG** (sync-flag memory, a word-granular atomic register file used for cross-engine handshakes) — and finally the **host tcmalloc-class heap**, which libtpu does *not* embed (no jemalloc/tcmalloc is linked); host allocations go through `posix_memalign` wrapped by either `tpu::PremappedMemoryManager` (DMA staging) or `tsl::BFCAllocator` (HBM-spill offload).

The reader who knows LLVM and GPU programming should hold one analogy and immediately complicate it. The HBM↔VMEM relationship is XLA's analogue of register allocation: [Memory-Space Assignment (MSA)](../compiler/msa-overview.md) is the compile-time pass that "colors" each `HloValue` `kDefault` (HBM) or `kAlternate` (VMEM), and the "spills" are HBM↔VMEM DMAs. But the analogy breaks four ways. First, the "registers" are tens of MiB, not 64-bit slots. Second, the allocator that realizes the coloring at runtime is the *same* best-fit class for every tier — there is no `HbmAllocator`, `VmemAllocator`, `SmemAllocator`, or `CmemAllocator` class; each tier is one `tpu::BestFitAllocator` instance distinguished only by a 32-byte `MemoryAllocator::Config{base_offset, end, alignment, granule}`. Third, only VMEM (and CMEM on Pufferfish) is MSA-managed; SMEM and SFLAG are *not* part of the `kAlternate`/`kDefault` tug-of-war — they are placed by opcode semantics and a fixed number-space partition respectively. Fourth, the runtime allocator almost never *decides* anything: MSA freezes every offset into the compiled program as a `ProgramMemoryMetadata_Allocation` proto, and the runtime allocator merely *replays* those offsets.

This page is the section map for the memory subsystem. It fixes the memory-space taxonomy, names the enum that labels them, gives the at-a-glance allocator/alignment/management facts for each tier, and points at the per-tier pages that own the detail. It does **not** reproduce the best-fit allocate/deallocate algorithm (that is [hbm-allocator.md](hbm-allocator.md)), the per-generation VMEM bank/bandwidth tables ([vmem-allocator.md](vmem-allocator.md)), the SFLAG atomic protocol ([sflag-protocol.md](sflag-protocol.md)), or the MSA placement cascade ([msa-overview.md](../compiler/msa-overview.md)).

For reimplementation, the orientation contract is:

- **The six-region taxonomy** — what each space physically is, who reads/writes it, and which engine owns it.
- **The `MemorySpace` enum** — the single label space (`kNone … kAlternate`) shared by the compile-time placer and the wire/profiler layer, plus the *second* numbering the DMA-driver-resource path uses (and why they disagree).
- **The per-space allocator/alignment matrix** — one `BestFitAllocator` per tier, the `Config` triple per tier, the 1024-B HBM DMA floor vs. the 16-KiB compile-time HBM alignment, and the word-granular on-chip alignments.
- **The compile-time → runtime hand-off** — MSA/`ProgramMemoryAllocator` freezes offsets into a proto; `CreateFromProto` rehydrates one `BestFitAllocator` per tier and replays.

| | |
|---|---|
| **Memory-space enum** | `xla::jellyfish::MemorySpace` (`kNone … kAlternate`); name table @ `0x21ce6b08` (`MemorySpaceToString`, 17 entries) |
| **Managed space-id array** | `ProgramMemoryAllocator::kAllocatedMemorySpaces` @ `0xb42ff10` (.rodata) |
| **Universal runtime allocator** | `tpu::BestFitAllocator` (208-byte instance, ctor `0x1e817500`); one per tier; typeinfo `0x21d346e8` |
| **Allocator base class** | `tpu::MemoryAllocator` (abstract; typeinfo `0x21d34700`, vtable `0x21d34700`) |
| **Compile-time placer** | `xla::jellyfish::ProgramMemoryAllocator::AllocateBytes` @ `0x1c629e40` (one entry, branches on `MemorySpace`) |
| **MSA (HBM↔VMEM coloring)** | `xla::memory_space_assignment::MsaAlgorithm::Finish` @ `0x1dc5b560` — see [msa-overview.md](../compiler/msa-overview.md) |
| **Hand-off proto** | `platforms_deepsea::jellyfish::xdb::ProgramMemoryMetadata_Allocation` |
| **Rehydrator** | `ProgramMemoryAllocator::CreateFromProto` @ `0x1c631f20` |
| **Endpoint render (DMA)** | `xla::jellyfish::MemorySpaceToDriverResource` @ `0x1d6223e0` (its own numbering — see [§2](#2-the-memoryspace-enum)) |
| **HBM DMA alignment floor** | `jf_driver::kHbmMinimumDmaAlignment` = **1024 B** (mask `& 0x3FF`, `WritePremappedHbm` @ `0xe73db80`) |
| **Compile-time HBM alignment** | `FLAGS_xla_jf_program_hbm_alignment_in_kib` = **16** ⇒ 16 KiB (@ `0x223b4888`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The Six-Region Taxonomy

### Purpose

There are six addressable regions a TPU program names. Five are on the chip (per TensorCore, plus per-BarnaCore / per-SparseCore variants); one is off-chip DRAM. The host heap is a seventh region that the *driver* (not the program) allocates from. The table below is the whole map at a glance; each row is owned by a dedicated page.

### At-a-glance

| Tier | Physical | Scope | Allocator | Alignment / granule | MSA-managed? | Owner page |
|---|---|---|---|---|---|---|
| **HBM** | Off-chip DRAM, tens of GiB | Per-chip, host-visible | `tpu::BestFitAllocator` (runtime); `ProgramMemoryAllocator` (compile) | **1024 B** DMA floor / **16 KiB** compile-time | yes — the `kDefault` tier | [hbm-allocator.md](hbm-allocator.md) · [hbm-dma-alignment.md](hbm-dma-alignment.md) |
| **VMEM** | On-chip SRAM, ~16–64 MiB/TensorCore | Per-TensorCore | `BestFitAllocator` (runtime); MSA + `ProgramMemoryAllocator` (compile) | `VmemAlignmentBoundaryInBytes()` — per-gen (`ChunkBytes` on JF; `max(Granule, VmemWord)` on PF/VF/GL) | **yes — the `kAlternate` fast tier** | [vmem-allocator.md](vmem-allocator.md) |
| **CMEM** | On-chip SRAM, read-mostly operand pool | Per-TensorCore (Pufferfish only) | `BestFitAllocator` (runtime); MSA (`xla_tpu_cmem_*`) | `CmemWordSizeBytes()` (~16 B on PF) | yes on **Pufferfish only**; `MemBanks(kCmem)` is `LogFatal` elsewhere | [cmem-pool.md](cmem-pool.md) |
| **SMEM** | On-chip SRAM, scalar/word-flat | Per-SPU (per-core scalar engine) | `BestFitAllocator` (runtime); `ProgramMemoryAllocator` (compile) | `SmemWordSizeBytes()` (word = alignment = granule) | **no** — placed by scalar-load/store opcode semantics | [smem-scalar-memory.md](smem-scalar-memory.md) · [smem-register-window.md](smem-register-window.md) |
| **SFLAG** | On-chip atomic register file, word-granular | Per-engine banks (TC / SCS / TEC / TAC); global sub-space cross-core | `BestFitAllocator` (size) + fixed *number-space partition* (compile) | `SflagWordSizeBytes()` (log2 cached @ `Target+0x4c8`) | **no** — placed by a reserved number-space partition | [sflag-protocol.md](sflag-protocol.md) |
| **Host heap** | Host DRAM | Process-wide | `PremappedMemoryManager` (DMA staging) / `tsl::BFCAllocator` (HBM offload) — both → `posix_memalign` | 4 KiB or 2 MiB page (`PickPageAlignment`); 16 B (BFC) | n/a (host-offload via custom calls) | [embedded-tcmalloc.md](embedded-tcmalloc.md) |

> **NOTE —** "register window" is a misnomer for every on-chip tier here. SMEM, CMEM, and SFLAG are all *flat byte/word arrays*; a search of the binary for `SmemRegisterWindow` / `SregWindow` / a CMEM register file returns zero hits. Scalar register windowing lives on the **SREG file** (allocated by LSRA-v2), and SMEM is merely its *spill backing store*. See [smem-register-window.md](smem-register-window.md) for why the window concept does not apply.

### Considerations

Three facts cut across all tiers and a reimplementer must internalize them before reading any per-tier page:

1. **There is no per-tier allocator class.** `tpu::BestFitAllocator` (208-byte instance, ctor `0x1e817500`) is the *single* concrete `tpu::MemoryAllocator` subclass in libtpu. The TpuHal binds one instance per tier through an `AllocatorFactory` (5 callbacks at `0x1e815600 … 0x1e8156c0`, all default to `Policy::kBestFit`). The only thing distinguishing the HBM allocator from the VMEM allocator is the 32-byte `Config` triple each is constructed with.

2. **There is no per-`TpuVersion` branch in the allocator.** Every per-codename divergence (HBM byte size, VMEM word size, alignment, granule) is data, carried in the embedded `*chip_parts.binarypb` resource and surfaced at boot as the `Config` triple. The allocator code is family-agnostic.

3. **The runtime allocator replays, it does not decide.** MSA and `ProgramMemoryAllocator` choose every offset at compile time and freeze them into `ProgramMemoryMetadata_Allocation` proto entries. At load time `CreateFromProto` (`0x1c631f20`) instantiates one `BestFitAllocator` per tier and replays the frozen offsets. The free-list / red-black-tree machinery is exercised at runtime only for *dynamic* allocations (scoped scratch, async-copy staging) that MSA marked run-time-allocated. See [§4](#4-the-compile-time--runtime-hand-off).

---

## 2. The `MemorySpace` Enum

### Purpose

One C++ enumeration, `xla::jellyfish::MemorySpace`, labels every region throughout the compiler and runtime. A reimplementer must reproduce *exactly this enum* because it is the operand-space tag on every LLO load/store, the `ProgramMemoryAllocator::AllocateBytes` selector, and the key the `kAllocatedMemorySpaces` array (`0xb42ff10`) iterates. The trap — and this page's central correction — is that a *second*, unrelated numbering governs how a memory space renders into a DMA descriptor's address word, and the two disagree on almost every value.

### Encoding — the compile-time `MemorySpace` enum

Recovered from `ProgramMemoryAllocator::kAllocatedMemorySpaces` (.rodata `0xb42ff10`) and the `MemorySpaceToString` name table (rodata `0x21ce6b08`, 17 entries). The two abstract MSA aliases (`kDefault`, `kAlternate`) sit at the tail; they are *not* distinct physical tiers but names the MSA algorithm uses for "HBM" and "the scarce on-chip tier."

| `MemorySpace` | Value | Physical tier | Owner | Confidence |
|---:|---:|---|---|---|
| `kNone` | 0 | — (no space) | — | CONFIRMED |
| `kHbm` | 1 | HBM (off-chip) | per-chip | CONFIRMED |
| `kPinnedHbm` | 2 | HBM, runtime-locked (peer-DMA inputs; repacker may not relocate) | per-chip | HIGH |
| `kVmem` | 3 | VMEM | per-TensorCore | CONFIRMED |
| `kSmem` | 5 | SMEM | per-SPU | CONFIRMED |
| `kCmem` | 4 | CMEM | per-TensorCore (PF) | CONFIRMED |
| `kSflag` | 7 | SFLAG (chip sync-flag tier) | per-engine banks | CONFIRMED |
| `kBarnaCoreBmem` | 8 | BarnaCore buffer memory | BarnaCore | HIGH |
| `kBarnaCoreSflag` | 11 | BarnaCore sync-flag tier | BarnaCore | CONFIRMED |
| `kBarnaCoreSmem` | 9/10 | BarnaCore scalar memory | BarnaCore | MEDIUM |
| `kSparseCoreSequencerSflag` | 13 | SC sequencer sync-flag region | SparseCore | CONFIRMED |
| `kSparseCoreSequencerSmem` | 12 | SC sequencer scalar memory | SparseCore | HIGH |
| `kHost` | — | Host RAM (offload spill target) | host | HIGH |
| `kDefault` | — | alias of HBM (MSA "abundant" tier) | MSA | CONFIRMED |
| `kAlternate` | — | alias of VMEM/CMEM (MSA "scarce" tier) | MSA | CONFIRMED |

> **QUIRK —** the enum ordering is *not* a clean physical-tier ordering, and the value-set the verifier accepts (`MemorySpaceToString`, 17 entries) is wider than the spaces any one generation uses. `kCmem`(=4) sits *before* `kSmem`(=5) numerically even though SMEM is the more universal tier; CMEM is alive only on Pufferfish. A reimplementer who drives a tier table off contiguous enum integers will misalign CMEM and SMEM. Use the named constants. The per-codename byte sizes that populate each tier's `Config` are absent from the C++ (they live in `chip_parts.binarypb`); the enum is the label, not the size.

### Correction — the DMA-driver-resource numbering is a *different* integer space

`xla::jellyfish::MemorySpaceToDriverResource(MemorySpace)` (`0x1d6223e0`) maps the LLO `MemorySpace` enum to a hardware *driver-resource id* stamped into a DMA descriptor's address word. Its switch (verified arm-by-arm in the decompile) does **not** return the enum value — it returns a permuted, non-monotone id, and it *traps* on `cmem` and the SparseCore spaces:

```c
// xla::jellyfish::MemorySpaceToDriverResource(MemorySpace ms)   sub_1D6223E0
function MemorySpaceToDriverResource(ms):
    switch ms:                       // ms = the 17-value LLO MemorySpace enum
        case 0 (<no space>): return 10
        case 1 (hbm):        return 2
        case 2 (hib):        return 3
        case 3 (vmem):       return 4
        case 4 (cmem):       FATAL("Unsupported memory space")   // not DMA-addressable here
        case 5 (smem):       return 6
        case 6 (sflag):      return 0
        case 7 (imem):       return 5
        case 8 (barna_core_bmem):  return 7
        case 9 (barna_core_smem):  return 9
        case 10 (barna_core_sflag): return 1
        case 11 (barna_core_imem):  return 8
        case 12..16 (sparse_core_*): FATAL("Unsupported memory space")
```

> **CORRECTION (MEM-1) —** the DMA-render numbering at `0x1d6223e0` is a *distinct* enumeration from the compile-time `MemorySpace` enum in [§2 table](#encoding--the-compile-time-memoryspace-enum). In the render path the operand-space ordering is `hbm=1, hib=2, vmem=3, cmem=4, smem=5, sflag=6, imem=7, …` and the *returned resource id* is `sflag→0, hbm→2, vmem→4, smem→6, …`. Neither the input ordering nor the output id matches the `kHbm=1, kCmem=4, kSmem=5, kSflag=7` constants the placer uses. A reimplementer must carry the `MemorySpace` enum end-to-end and convert to a driver-resource id *only* at the descriptor boundary via this explicit switch — deriving the id from the enum integer is wrong for every tier. The full resource-id table and the off-by-one trap that earlier confused the two live on [intra-chip-descriptor.md](../dma/intra-chip-descriptor.md#3-endpoint-rendering).

---

## 3. The Per-Space Allocator / Alignment Matrix

### Purpose

Every tier is one `tpu::BestFitAllocator` constructed from a 32-byte `MemoryAllocator::Config`. The only per-tier differences are the four `Config` fields and (for HBM) a stricter compile-time alignment than the hardware DMA floor. This section gives the `Config` triple and the alignment rule per tier; the allocate/deallocate algorithm itself is identical across tiers and is documented once on [hbm-allocator.md](hbm-allocator.md).

### The `Config` struct (one per tier)

```c
struct tpu::MemoryAllocator::Config {   // 32 B, passed by const&
    int64_t base_offset_in_bytes_;      // +0   ≥ 0   (0 for every on-chip tier)
    int64_t allocatable_range_end_;     // +8   > 0   (capacity = end − base)
    int64_t alignment_in_bytes_;        // +16  > 0, power of two, divides granule
    int64_t granule_in_bytes_;          // +24  hardware granule (page / word)
};
```

The ctor (`0x1e817500`) asserts, as `LogMessageFatal` checks: `base_offset_in_bytes_ >= 0`, `allocatable_range_end_ > 0`, `alignment_in_bytes_ > 0`, `alignment_in_bytes_ % granule_in_bytes_ == 0`, and `alignment_in_bytes_` is a power of two. These invariants hold for every tier — they are what let the allocator's round-up arithmetic (`(size + align − (size!=0)) & −align`, confirmed at the head of `Allocate` `0x1e817820`) be a single AND.

### Per-tier `Config` and alignment

| Tier | `base_offset` | `end` (capacity) | `alignment` | `granule` | Confidence |
|---|---|---|---|---|---|
| **HBM** | 0 | `chip_parts.binarypb` HBM bytes (`− xla_tpu_user_reserved_hbm_bytes`) | **16 KiB** compile-time (`xla_jf_program_hbm_alignment_in_kib`=16); **1024 B** runtime DMA floor | `chip_parts` HBM granule | CONFIRMED |
| **VMEM** | 0 | `Target::VmemSizeBytes()` (`Target+0x458`) or `xla_tpu_override_vmem_size_kib` | `VmemAlignmentBoundaryInBytes()` — `ChunkBytes` (JF) / `max(Granule, VmemWord)` (PF/VF/GL) | `VmemWordSizeBytes()` (`Target+0x50C`) | CONFIRMED |
| **CMEM** | 0 | `Target::CmemSizeBytes()` (`Target+0x460`) | `CmemWordSizeBytes()` | `CmemWordSizeBytes()` (`Target+0x510`, ~16 B PF) | CONFIRMED |
| **SMEM** | 0 | `Target::SmemSizeBytes()` (`Target+0x470`) | `SmemWordSizeBytes()` | `SmemWordSizeBytes()` (`Target+0x508`) | CONFIRMED |
| **SFLAG** | 0 | `Target::SflagSizeBytes()` (`Target+0x468`) | `SflagWordSizeBytes()` (`Target+0x504`) | `SflagWordSizeBytes()` | CONFIRMED |
| **Host (premapped)** | per-partition `partition_size * i` | `partition_size` | 4 KiB if size ≤ 2 MiB, else 2 MiB (`PickPageAlignment`) | = alignment | CONFIRMED |
| **Host (BFC offload)** | 0 | 256 GiB cap (`0x4000'0000'0000`) | ≥ 16 B (`posix_memalign`) | 2 MiB region growth | CONFIRMED |

> **GOTCHA —** HBM has **two** alignment numbers, and confusing them silently corrupts a DMA. `kHbmMinimumDmaAlignment` = 1024 B is the *hardware* floor: every DMA issue site masks size and address with `& 0x3FF` and `LogMessageFatal`s on a non-zero remainder (`byte_offset % jf_driver::kHbmMinimumDmaAlignment == 0`, `size % … == 0`, in `WritePremappedHbm` @ `0xe73db80`). The 16 KiB compile-time figure (`xla_jf_program_hbm_alignment_in_kib`) is *stricter* — it rounds every program-level HBM tensor up to 16 KiB before MSA places it, to accommodate XLA's stride/sub-tile addressing and slice-prefetch boundaries. A reimplementer who aligns HBM allocations to 1024 B at compile time will produce a layout MSA's slice machinery cannot address; one who enforces 16 KiB at DMA-issue time wastes nothing but is needlessly strict. The 1024-B floor is the wire contract; the 16-KiB rule is the placement contract. See [hbm-dma-alignment.md](hbm-dma-alignment.md).

> **NOTE —** the on-chip tiers (VMEM/CMEM/SMEM/SFLAG) all set `alignment == granule == <tier>WordSizeBytes()` and `base_offset == 0` — every on-chip tier starts at sub-tile address 0, and a single allocation is always one word-aligned run. Only HBM separates alignment from granule (16 KiB alignment over a smaller hardware granule), and only the host premapped manager uses a non-zero `base_offset` (the per-partition slot base). The numeric word sizes per codename live in `chip_parts.binarypb` and are not in the C++; the *formulas* above are exact.

### The host heap is not a tcmalloc

libtpu embeds **no jemalloc and no tcmalloc** (despite the page family name). The only OS-level allocation primitive reached is `posix_memalign`, wrapped two ways: `tpu::PremappedMemoryManager` partitions a single `posix_memalign` region into power-of-two partitions, each wrapping a per-partition `BestFitAllocator` under an `absl::Mutex`, round-robined for DMA staging; and `tsl::BFCAllocator` (the TF best-fit-with-coalescing allocator, ~1.2 KiB/instance, 21 size-class bins) backs only the `HostOffloadingTpuAllocator` (256 GiB cap) that receives HBM buffers MSA elected to spill to host RAM. Neither is the on-device allocator. See [embedded-tcmalloc.md](embedded-tcmalloc.md).

---

## 4. The Compile-Time → Runtime Hand-off

### Purpose

The same offset that MSA chooses at compile time is the offset the runtime allocator hands back at load time. This is the spine that ties the per-tier pages together: every tier flows through the same seven-stage hand-off, differing only in which compile-time placer chose the offset (MSA for VMEM/CMEM, opcode semantics for SMEM, number-space partition for SFLAG).

### Stages

```text
Compile time (XLA core):
  HeapSimulator::Run(GlobalDecreasingSizeBestFitHeap<HloValue>, …)   0x1e49dae0
      └─ produces per-buffer Chunk{offset, size}
Compile time (XLA TPU layer):
  MsaAlgorithm::Finish()                                             0x1dc5b560   ── HBM↔VMEM(↔CMEM) coloring only
      └─ Allocation objects {Pinned / Copy / Prefetch / Scoped / …}
Compile time (jellyfish):
  ProgramMemoryAllocator::AllocateBytes(MemorySpace, …)              0x1c629e40   ── one entry, branches on MS
      └─ emits ProgramMemoryMetadata_Allocation{memory_space, offset, size, block_type, name}
Codegen:
  the compiled XDB/LLO program embeds the proto (offsets symbolic until link)
────── load time ──────
  ProgramMemoryAllocator::CreateFromProto(LloModule*, …, proto)      0x1c631f20
      └─ per tier: TpuHal::GetAllocatorFactory() (this+0x48)         0x1e8139a0
            └─ tpu::BestFitAllocator(Config{base=0, end=<tier>Size, align, granule})
Execution:
  each compile-time Allocation → BestFitAllocator::Allocate(size) at the FROZEN offset
Deallocation:
  BestFitAllocator::Deallocate(offset) — eager coalescing on every free
```

> **QUIRK —** MSA only colors the HBM↔VMEM (and, on Pufferfish, HBM↔CMEM) axis. SMEM and SFLAG flow through the *same* `ProgramMemoryAllocator` → proto → `BestFitAllocator` spine but are **never** seen by MSA's `kAlternate`/`kDefault` decision: SMEM is committed wherever a scalar-load/store opcode declares `MemorySpace=kSmem`, and SFLAG is allocated out of a fixed *number-space partition* (`GetStartReservedSyncFlagNumber` `0x1d6178e0`, barriers at `Target+0x8c0/+0x8c4`), not the byte heap. A reimplementer who routes SMEM/SFLAG through the MSA cost model will mis-place them; MSA's tier-balancing is a VMEM/CMEM-only concern. See [smem-scalar-memory.md](smem-scalar-memory.md) and [sflag-protocol.md](sflag-protocol.md).

---

## Related Components

| Component | Relationship |
|---|---|
| [msa-overview.md](../compiler/msa-overview.md) | The compile-time pass that colors HBM (`kDefault`) vs. VMEM/CMEM (`kAlternate`) and inserts the async copies |
| [intra-chip-descriptor.md](../dma/intra-chip-descriptor.md) | The DMA descriptor whose `(mem_id, core_id)` endpoints render the `MemorySpace` enum via `MemorySpaceToDriverResource` |
| [memory-space-enum.md](../isa/memory-space-enum.md) | The 17-value LLO `MemorySpace` enum as it appears at the ISA / operand-tag level |

## Cross-References

- [hbm-allocator.md](hbm-allocator.md) — the universal `tpu::BestFitAllocator` algorithm (best-fit + eager coalescing); the HBM tier and the two-stack (compile-time `ProgramMemoryAllocator` + runtime `BestFitAllocator`) model
- [hbm-dma-alignment.md](hbm-dma-alignment.md) — the 1024-B `kHbmMinimumDmaAlignment` floor vs. the 16-KiB compile-time program alignment
- [vmem-allocator.md](vmem-allocator.md) — the `kAlternate` fast tier; per-generation VMEM size/word/bank/bandwidth `Config` and scoped-VMEM machinery
- [cmem-pool.md](cmem-pool.md) — the Pufferfish-only read-mostly operand pool; `xla_tpu_cmem_*` MSA knobs; `MemBanks(kCmem)` LogFatal elsewhere
- [smem-scalar-memory.md](smem-scalar-memory.md) — the SPU's scalar memory; opcode-driven placement (not MSA); reserved top/bottom blocks
- [smem-register-window.md](smem-register-window.md) — why no SMEM register window exists; SMEM as the SREG-file spill backing store (LSRA-v2)
- [sflag-protocol.md](sflag-protocol.md) — the sync-flag atomic tier; number-space partition, three SC sub-spaces, fence/ordering model
- [embedded-tcmalloc.md](embedded-tcmalloc.md) — the host heap: no tcmalloc/jemalloc; `PremappedMemoryManager` and `tsl::BFCAllocator` over `posix_memalign`
- [on-device-compaction.md](on-device-compaction.md) — `BestFitAllocator::Compact` relocation; the repacker that reduces fragmentation
- [buffer-donation-aliasing.md](buffer-donation-aliasing.md) — `kPinnedHbm` and input/output aliasing that the repacker may not relocate
- [tpu-buffer-layout.md](tpu-buffer-layout.md) — how a logical XLA buffer maps to physical offsets in these tiers
- [msa-overview.md](../compiler/msa-overview.md) — Phase 7; the consumer of this taxonomy on the compile side
- [intra-chip-descriptor.md](../dma/intra-chip-descriptor.md) — the wire view of the `MemorySpace` enum at the DMA boundary
- [back to index](../index.md) — Part X — On-Chip Memory & DMA
