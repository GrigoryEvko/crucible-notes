# VMEM Allocator

> *All addresses, vtable offsets, and field offsets on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other versions will differ.*

## Abstract

VMEM (vector memory) is the TensorCore's scarce on-chip scratchpad — the tier MSA rations, the staging area between HBM and the MXU/VPU, and the home of every spill the compiler can afford to keep on-chip. This page documents the **VMEM arena**: how a flat byte range is carved out per chip generation, the **tile-alignment quantum** every allocation rounds up to, the **per-generation VMEM size**, and how the compiler reserves a slice of that arena for scoped scratch and MXU overlay buffers before MSA is allowed to touch the rest.

There is one structural fact a reimplementer must internalize first: **there is no class named `VmemAllocator` in this binary.** VMEM is serviced by the same two-stack machinery as every other tier. At compile time, XLA places `kVmem` values through `MsaAlgorithm` on a `GlobalDecreasingSizeBestFitHeap<HloValue>`; at load time, the runtime rehydrates the frozen offsets into a single generic `tpu::BestFitAllocator` instantiated for the VMEM tier with a `MemoryAllocator::Config{base_offset=0, end=VmemSizeBytes, alignment, granule}`. The *only* VMEM-specific code in the allocator path is a handful of per-generation `xla::jellyfish::*Target::Vmem*` virtual overrides that supply the size, the alignment quantum, the bank count, and the reserved tail. Everything that page below documents is those virtuals plus the budget arithmetic that consumes them.

The arena contract this page owns:

- **The arena range.** `[0, VmemSizeBytes)` per TensorCore, `base_offset` always `0`, size read from a single `int32` field the chip-parts proto fills at boot.
- **The alignment quantum.** Generation-specific: the tile `ChunkBytes` on Jellyfish, `max(GranuleBytes, VmemWordSizeBytes)` on Pufferfish / Viperfish / Ghostlite. Never a fixed compile-time constant.
- **The per-generation sizing inputs.** `VmemSizeBytes`, `VmemWordSizeBytes`, `ChunkBytes`, the bank count, and the default scoped budget, all keyed off the active `Target` subclass.
- **The reservations carved before MSA runs.** `OverlayReservedVmemBytes` (MXU overlay tail) and `ChunkBytes * GetReservedVmemBufferSizeChunks` (collective staging), both subtracted from the arena to yield the usable scoped-VMEM limit.

This page does **not** cover: the HBM allocator and its coalescing rules — see [hbm-allocator.md](hbm-allocator.md); the MSA placement loop and the gflag knobs that gate VMEM residency — see [../compiler/msa-overview.md](../compiler/msa-overview.md) and [../compiler/msa-per-version-defaults.md](../compiler/msa-per-version-defaults.md); the on-chip tier map — see [overview.md](overview.md).

| | |
|---|---|
| **Arena range** | `[base_offset=0, end=VmemSizeBytes)` per TensorCore |
| **Size source** | `Target::VmemSizeBytes` @ `0x1d615e00` (int32 @ `Target +0x458`, sign-extended) |
| **Alignment (JF)** | `JellyfishTarget::VmemAlignmentBoundaryInBytes` @ `0x1d490d40` → `Target::ChunkBytes` |
| **Alignment (PF/VF/GL)** | `*Target::VmemAlignmentBoundaryInBytes` → `max(GranuleBytes, VmemWordSizeBytes)` |
| **Tile quantum** | `Target::ChunkBytes` @ `0x1d619f40` = `4 * topology.word_count` |
| **Granule** | `Target::VmemWordSizeBytes` @ `0x1d617300` (uint32 @ `Target +0x50C`) |
| **Overlay tail** | `Target::OverlayReservedVmemBytes` (vtable `+0x220`), base `0`, GL `16*ChunkSizeBytes` |
| **Scoped budget** | `scoped_memory_util::ScopedVmemLimitBytes` @ `0x1c864dc0` |
| **Compile-time placer** | `MsaAlgorithm` on `GlobalDecreasingSizeBestFitHeap<HloValue>` (MS = `kVmem` = 3) |
| **Runtime allocator** | generic `tpu::BestFitAllocator` (per-tier `Config`), shared with HBM |
| **`VmemAllocator` class** | **does not exist** — VMEM uses the generic allocator + per-gen `Target` virtuals |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The VMEM Arena

### One flat byte range per TensorCore

VMEM is a single contiguous byte arena per TensorCore. Both the compile-time placer and the runtime allocator treat it as `[0, VmemSizeBytes)`. The base offset is always zero — every chip starts VMEM at sub-tile address `0`, so the runtime `Config.base_offset_in_bytes_` for the VMEM tier is hard-`0`, unlike HBM where the runtime carves a user-reserved prefix.

`Target::VmemSizeBytes` (`0x1d615e00`) is a single field read, sign-extended from `int32`:

```c
// xla::jellyfish::Target::VmemSizeBytes  @ 0x1d615e00
__int64 Target::VmemSizeBytes(Target *this) {
  return *((int *)this + 278);   // Target +0x458, signed int32
}
```

CONFIRMED — `278 * 4 = 0x458`, and the IDA type is `int`, so the value is sign-extended to 64-bit (negative sentinels are therefore representable; the override flag below uses one). The field is populated at boot from `TpuChipParts` / `TpuMemoryParts` (decoded out of the embedded `chip_parts.binarypb`), not computed.

> **NOTE —** The compiler can replace the arena size at boot with the `xla_tpu_override_vmem_size_kib` flag (handle `0x223a0980`), read once and shifted left by 10 (KiB → bytes). Its sentinel is `-1`/unset, in which case the `Target`-supplied size is used verbatim. This is the only way the arena's `end` differs from the chip-parts value.

### What the arena holds, top to bottom

The usable arena seen by MSA is **smaller** than `VmemSizeBytes`. Two reservations are subtracted before MSA's heap is sized — see [Reservations](#reservations-carved-before-msa). Logically:

```text
 VMEM byte range  [0 ........................................ VmemSizeBytes)
 ┌──────────────────────────────────┬──────────────┬──────────────────────┐
 │  MSA-placed values + scoped       │ collective   │  OverlayReservedVmem  │
 │  scratch (the rationed region)    │ staging      │  (MXU operand overlay)│
 │  ← GlobalDecreasingSizeBestFit →  │ chunks       │  ← off-limits to MSA →│
 └──────────────────────────────────┴──────────────┴──────────────────────┘
   usable = ScopedVmemLimitBytes  =  VmemSizeBytes
                                     − OverlayReservedVmemBytes
                                     − ChunkBytes * GetReservedVmemBufferSizeChunks
```

The runtime allocator, by contrast, sees the *full* `[0, VmemSizeBytes)` range — it just replays the offsets MSA already chose inside the usable sub-region, so it never trips the reservations.

---

## The Alignment Quantum

Every VMEM allocation is rounded up to a generation-specific alignment boundary. This is the central tile-alignment rule, and it is **not a compile-time constant** — it is the `VmemAlignmentBoundaryInBytes` virtual, dispatched through the active `Target` subclass at vtable `+0x5C8`.

### Jellyfish: the tile chunk

`JellyfishTarget::VmemAlignmentBoundaryInBytes` (`0x1d490d40`) is a pure thunk to `Target::ChunkBytes`:

```c
// xla::jellyfish::JellyfishTarget::VmemAlignmentBoundaryInBytes  @ 0x1d490d40  (thunk)
__int64 JellyfishTarget::VmemAlignmentBoundaryInBytes(JellyfishTarget *this) {
  return Target::ChunkBytes(this);
}
```

`ChunkBytes` is the tile quantum — four bytes per topology word:

```c
// xla::jellyfish::Target::ChunkBytes  @ 0x1d619f40
__int64 Target::ChunkBytes(Target *this) {
  return 4LL * *(_QWORD *)(*((_QWORD *)this + 119) + 424LL);   // 4 * topology[+0x3B8].word_count
}
```

CONFIRMED — `this[119]` is the topology pointer (`+0x3B8`); `+424` (`+0x1A8` within the topology struct) is the sub-lane `word_count`; the result is `word_count * 4`. This is the smallest tile that fills one VPU bundle: the lane×sub-lane quantum scaled by the 4-byte word. The 32-bit form is `Target::ChunkSizeBytes` (`0x1d617100`), used where a `uint32` is wanted.

### Pufferfish / Viperfish / Ghostlite: the larger of granule and word

The three newer Targets share an identical body — the alignment is the larger of the dispatched granule and the VMEM word size:

```c
// xla::jellyfish::{Pufferfish,Viperfish,Ghostlite}Target::VmemAlignmentBoundaryInBytes
//   @ 0x1d4952e0 / 0x1d49b8e0 / 0x1d4985c0   (byte-identical bodies)
__int64 PufferfishTarget::VmemAlignmentBoundaryInBytes(PufferfishTarget *this) {
  __int64 v1 = Target::GranuleBytes(this);                 // vtable[+0x5C0] dispatch
  __int64 result = (int)Target::VmemWordSizeBytes(this);   // uint32 @ Target +0x50C
  return v1 > (int)result ? v1 : result;
}
```

CONFIRMED for Pufferfish (`0x1d4952e0`) and Ghostlite (`0x1d4985c0`) by decompile; Viperfish (`0x1d49b8e0`) shares the same mangled body per the symbol table (HIGH — not separately re-read, but the source-identical bodies and matching address are conclusive). `GranuleBytes` (`0x1d617f80`) is itself a per-generation virtual (`vtable[+0x5C0]`); `VmemWordSizeBytes` (`0x1d617300`) is a direct field read at `Target +0x50C`.

### The granule and the word

`Target::VmemWordSizeBytes` is the per-lane sub-word — the allocation granule the runtime `Config` uses:

```c
// xla::jellyfish::Target::VmemWordSizeBytes  @ 0x1d617300
__int64 Target::VmemWordSizeBytes(Target *this) {
  return *((unsigned int *)this + 323);   // Target +0x50C, uint32
}
```

CONFIRMED — `323 * 4 = 0x50C`. Both the word size and `GranuleBytes` are filled from `TpuChipParts` at boot; numeric per-codename values await the `chip_parts.binarypb` decode (see [Caveats](#caveats-and-open-items)).

| Generation | Alignment formula | Tile quantum (`ChunkBytes`) | Granule (`VmemWordSizeBytes`) |
|---|---|---|---|
| Jellyfish (v2) | `ChunkBytes` | `4 * topology.word_count` | chip-parts |
| Pufferfish (v3) | `max(GranuleBytes, VmemWordSizeBytes)` | `4 * topology.word_count` | chip-parts |
| Viperfish (v4) | `max(GranuleBytes, VmemWordSizeBytes)` | `4 * topology.word_count` | chip-parts |
| Ghostlite (v5 / Trillium) | `max(GranuleBytes, VmemWordSizeBytes)` | `4 * topology.word_count` | chip-parts |

> **NOTE —** The arena's **granule** (the `Config.granule_in_bytes_` the runtime allocator quantizes to) is `VmemWordSizeBytes`, whereas the **alignment** is the larger formula above. On the newer generations these can differ: a value's start offset is rounded to the alignment boundary, but its size is rounded to the granule. The base `Target::VmemAlignmentBoundaryInBytes` (`0x1d61e940`) is a pure-virtual error path that never returns — a `Target` with no codename subclass is a bug.

---

## Per-Generation Sizing Inputs

The arena is parameterized entirely by the active `Target` subclass. The numeric byte size of VMEM is *not* in the `.text` — it is the `chip_parts.binarypb` field surfaced through `VmemSizeBytes`. What *is* baked into the code is everything else: the bank count, the default scoped budget, and the overlay reservation formula.

### Bank count

`MemBanks(MemorySpace)` (vtable `+0xC0`) returns the bank count per tier; `MemorySpace::kVmem == 3`. The bodies are decompile-confirmed:

```c
// xla::jellyfish::JellyfishTarget::MemBanks  @ 0x1d48fc80
// MS==3 (kVmem) → 8 ;  MS==5 (kSmem) → 2 ;  else LogFatal
// xla::jellyfish::GhostliteTarget::MemBanks @ 0x1d4969c0
// MS==3 (kVmem) → 32 ;  MS==5 → 8 ;  else LogFatal
// xla::jellyfish::PufferfishTarget::MemBanks @ 0x1d493900
// qword_B5305C8[MS-3]  for MS ∈ {3,4,5}  → {16, 32, 8} ;  else LogFatal
```

CONFIRMED for Jellyfish, Pufferfish, Ghostlite. Pufferfish indexes a 3-entry rodata table at `0xb5305c8` (`{16, 32, 8}`) for the contiguous range `MS ∈ {3..5}`. Viperfish (`0x1d4999c0`) returns VMEM=32, MS=5→8 per the symbol-table body (HIGH — same shape as Ghostlite, not separately re-read).

| Target | VMEM banks (MS=3) | `kSmem` (MS=5) | Cross-slot bank conflicts |
|---|---:|---:|:---:|
| JellyfishTarget | 8 | 2 | false |
| PufferfishTarget | 16 | 8 | false |
| ViperfishTarget | 32 | 8 | **true** |
| GhostliteTarget | 32 | 8 | **true** |

Banking is an *access-scheduling* property, not an allocation property — the allocator hands out byte offsets and the LLO bundle packer derives `(bank, sub-bank) = (offset / VmemWordSizeBytes) mod MemBanks(kVmem)` at issue time. The cross-slot-conflict bit (Viperfish/Ghostlite `true`) is what drives the `xla_jf_avoid_cross_slot_vmem_bank_conflicts` swizzle insertion; it does not change the arena layout.

### Default scoped-VMEM budget

`DefaultPlatformScopedMemoryBytes` (vtable `+0x228`) is the per-program high-water mark for scoped scratch, before the limit arithmetic clamps it:

```c
// JellyfishTarget::DefaultPlatformScopedMemoryBytes  @ 0x1d48fc40  →  0x1000000  (16 MiB)
// GhostliteTarget::DefaultPlatformScopedMemoryBytes  @ 0x1d497540  →  0x2000000  (32 MiB)
```

CONFIRMED. Pufferfish (`0x1d494520`) and Viperfish (`0x1d49a720`) both return `0x1000000` (16 MiB) per the symbol table (HIGH). The base `Target` version is a `LogMessageFatal` — every concrete generation must override it.

| Target | DefaultPlatformScopedMemoryBytes |
|---|---:|
| JellyfishTarget | 16 MiB (`0x1000000`) |
| PufferfishTarget | 16 MiB |
| ViperfishTarget | 16 MiB |
| GhostliteTarget | 32 MiB (`0x2000000`) |
| `Target::` base | `LogMessageFatal` — never reached |

The compile flag `xla_tpu_scoped_vmem_limit_kib` (handle `0x223b8770`) overrides this default when present (read from the `TpuCompilationEnvironment` proto at offset `0x10F0` inside `DefaultScopedVmemBytes` @ `0x1c864e40`); a `-1` sentinel selects the per-Target default, otherwise the proto value is `<< 10` (KiB → bytes).

---

## Reservations Carved Before MSA

Two slices of the arena are removed before MSA sees a heap. The usable scoped-VMEM limit is computed by `scoped_memory_util::ScopedVmemLimitBytes` (`0x1c864dc0`), which is decompile-confirmed to be exactly:

```c
// xla::jellyfish::scoped_memory_util::ScopedVmemLimitBytes  @ 0x1c864dc0
__int64 ScopedVmemLimitBytes(/*Target*/ this, const Target *a2, const HloModule *a3) {
  __int64 v3 = Target::VmemSizeBytes(this);
  __int64 v4 = (*(vtable[+0x220]))(this);                 // OverlayReservedVmemBytes
  __int64 v7 = 0;
  if (a2) {
    __int64 v5 = Target::ChunkBytes(this);
    __int64 env = GetTpuCompEnv(a3);
    v7 = v5 * ring_sum_emitter_utils::GetReservedVmemBufferSizeChunks(this, env);
  }
  return v3 - (v7 + v4);
}
```

CONFIRMED — the body computes `VmemSizeBytes − OverlayReservedVmemBytes − ChunkBytes * GetReservedVmemBufferSizeChunks(comp_env)`. (Note the vtable slot called is `+0x220`; the raw `+0x5D0` annotation refers to the *declared* `OverlayReservedVmemBytes` virtual — both name the same hook reached at runtime through the Target vtable.)

### Overlay reservation (MXU operand staging tail)

`OverlayReservedVmemBytes` carves a fixed tail off the top of VMEM for MXU-resident "overlay" operand buffers (ping-pong staging plus MSA temporary scratch). The base returns `0`; Ghostlite reserves 16 tile chunks:

```c
// xla::jellyfish::GhostliteTarget::OverlayReservedVmemBytes  @ 0x1d497520
__int64 GhostliteTarget::OverlayReservedVmemBytes(GhostliteTarget *this) {
  return 16LL * (int)Target::ChunkSizeBytes(this);   // 16 tile chunks
}
```

CONFIRMED for Ghostlite. The base `Target::OverlayReservedVmemBytes` is `xor eax,eax; ret` (`0`); Jellyfish and Pufferfish inherit it. Viperfish (`0x1d49a6c0`) gates on the codename: production Viperfish reserves `16 * ChunkSizeBytes`, but **viperfish-lite disables the overlay** (returns `0`) — selected by a `cmpl $0x6574696c` ("lite", little-endian) test on the first four bytes of `TpuChipParts::variant_name()` (HIGH — the lite branch is reported by the symbol-level analysis; the production formula matches Ghostlite's confirmed body).

| Target | OverlayReservedVmemBytes |
|---|---|
| `Target::` base / Jellyfish / Pufferfish | `0` |
| ViperfishTarget | `16 * ChunkSizeBytes` (production); `0` if codename `*lite*` |
| GhostliteTarget | `16 * ChunkSizeBytes` |

### Collective staging chunks

The second reservation is `ChunkBytes * GetReservedVmemBufferSizeChunks(comp_env)` (`ring_sum_emitter_utils::GetReservedVmemBufferSizeChunks` @ `0x1c86a820`) — a comp-env-driven count of tile chunks set aside for ring-sum / all-reduce staging buffers, so collective lowering always has a guaranteed bounce buffer regardless of how MSA packs the rest of the arena.

---

## Two-Stack Allocation Path

VMEM is allocated by the same two-stack pattern as every other tier. Neither stack contains VMEM-specific allocator *code* — both are the generic engines, parameterized by the Target virtuals above.

### Compile time (XLA)

Per-HLO placement runs through MSA. The decision "does this `HloValue` live in VMEM (`kAlternate`) or HBM (`kDefault`)" is made by `MsaAlgorithm` on a `GlobalDecreasingSizeBestFitHeap<HloValue>` — the same heap MSA uses for any alternate-memory tier; VMEM is simply the `kAlternate` realization on a TensorCore. The greedy heap never sees "VMEM"; it sees a `[0, ScopedVmemLimitBytes)` byte range. The full placement loop, prefetch headroom logic, and gflag knobs belong to [../compiler/msa-overview.md](../compiler/msa-overview.md) and [../compiler/msa-per-version-defaults.md](../compiler/msa-per-version-defaults.md) — this page owns only the arena those passes draw from.

Scoped (per-instruction) scratch is allocated by `LloRegionBuilder::AllocateScopedVmem` (`0x1d5182c0`), a five-instruction trampoline:

```c
// xla::jellyfish::LloRegionBuilder::AllocateScopedVmem  @ 0x1d5182c0
LloValue *AllocateScopedVmem(LloRegionBuilder *a1, Shape *a2, ...) {
  return AllocateScopedMemory(a1, a2, 3u, ...);   // MemorySpace::kVmem == 3
}
```

CONFIRMED — it forwards to the generic `AllocateScopedMemory` with the literal `kVmem` (3) memory-space tag. The chosen offsets are frozen into `ProgramMemoryMetadata_Allocation{memory_space=kVmem, offset, size, …}` proto entries embedded in the compiled program.

### Load time (runtime HAL)

`ProgramMemoryAllocator::CreateFromProto` rehydrates one `tpu::BestFitAllocator` per memory tier. The VMEM tier receives the generic 32-byte `MemoryAllocator::Config`:

```text
MemoryAllocator::Config {
  base_offset = 0,                               // VMEM always starts at sub-tile 0
  end         = Target::VmemSizeBytes(),         // or xla_tpu_override_vmem_size_kib << 10
  alignment   = Target::VmemAlignmentBoundaryInBytes(),
  granule     = Target::VmemWordSizeBytes(),
}
```

The allocator class, free-list, coalescing, and best-fit algorithm are **identical to the HBM tier** — there is no VMEM subclass. See [hbm-allocator.md](hbm-allocator.md) for the allocator internals. Because MSA pre-computes every offset, the runtime VMEM allocator is almost never asked to *search*: it replays the frozen offsets and only exercises the free-list path for dynamic scoped scratch and a few DMA staging buffers.

> **NOTE —** The runtime VMEM tier uses **no deferred-free path**. `tpu::DeferredTpuAllocator` wraps only user-visible HBM buffers; VMEM deallocations are coalesced inline.

---

## Exhaustion

VMEM exhaustion at compile time is a **hard error, not a spill** — there is nowhere on-chip to spill *to*. Three failure modes touch the arena:

- **Requested scoped VMEM exceeds the limit.** `IsRequestedScopedVmemValid` (`0x12fcbec0`) returns a `Status` built from rodata at `0xa215530`: *"\<N\> bytes of scoped Vmem requested (via \<op\>), but the max valid bytes is \<ScopedVmemLimitBytes()\>. See go/scoped-vmem for more details."* — i.e. the request exceeded the usable arena computed above.
- **MSA cannot place a value.** MSA retries up to `xla_jf_vmem_max_retries`, then falls the value back to `kDefault` (HBM-resident). If the value *must* stay in alternate memory, the compile fails hard via `xla::error::CompileTimeScopedVmemOom` (`0x1c62e5a0`).
- **Runtime OOM.** Matches HBM: `BestFitAllocator::Allocate` returns `absl::ResourceExhaustedError` with the fragmentation dump (see [hbm-allocator.md](hbm-allocator.md)). Rarely exercised for VMEM because offsets are static.

Fusion-overflow rejection (`CostModel::FusionWouldExceedVmemCapacity` @ `0x130c4a80`) is a *silent* fusion-formation veto, not an allocation error — it prevents a fusion that would exceed the arena from forming at all, and is documented with the fusion cost model rather than here.

---

## Caveats and Open Items

- **Numeric per-codename VMEM byte sizes are not in `.text`.** `VmemSizeBytes`, `VmemWordSizeBytes`, and `GranuleBytes` all read fields the boot-time `chip_parts.binarypb` decode fills. The *formulas* on this page are byte-exact; the *literal byte counts* per codename (Jellyfish, Pufferfish, Viperfish, viperfish-lite, Ghostlite, Trillium-class) await the proto decode and are not asserted here.
- **The "v1–v5" labels are `TpuVersion` enum ints** observed in switch tables (`kJellyfish == 2`, …). Trillium-class chips are serviced by `GhostliteTarget` with a different `variant_name()` codename string; there is no separate `GhostfishTarget`/`TrilliumTarget` class in 0.0.40 — the `xla_gf_vmem_*` flag family reconfigures the same `MsaAlgorithm`.
- **Viperfish entries marked HIGH** (alignment body, MemBanks, default scoped, overlay lite-branch) were confirmed via the symbol table and source-identical sibling bodies (Pufferfish/Ghostlite), not by separately re-reading the Viperfish decompile — graded HIGH rather than CONFIRMED.

---

## Cross-References

- [overview.md](overview.md) — the five on-chip tiers + host memory; where VMEM sits in the hierarchy.
- [hbm-allocator.md](hbm-allocator.md) — the generic `tpu::BestFitAllocator` internals (coalescing, free-list, OOM message) shared by the VMEM tier.
- [smem-scalar-memory.md](smem-scalar-memory.md) — the scalar-memory tier (`MemorySpace::kSmem == 5`), allocator and addressing.
- [tpu-buffer-layout.md](tpu-buffer-layout.md) — on-device buffer structure that the chosen VMEM offsets describe.
- [../compiler/msa-overview.md](../compiler/msa-overview.md) — the compile-time placer (`MsaAlgorithm`) that rations the VMEM arena via `kAlternate`.
- [../compiler/msa-per-version-defaults.md](../compiler/msa-per-version-defaults.md) — per-generation MSA numeric defaults and the `xla_jf_vmem_*` flag family.
- [../compiler/layout-assignment.md](../compiler/layout-assignment.md) — the pass that runs before MSA and fixes the tile layouts VMEM offsets must respect.
