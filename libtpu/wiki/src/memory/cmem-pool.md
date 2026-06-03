# CMEM Constant-Memory Pool

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. CMEM byte sizes per codename live in the embedded `*chip_parts.binarypb` resource and are not in the C++ — every numeric size below is sourced as noted. Other versions will differ.*

## Abstract

CMEM ("constant memory", `MemorySpace::kCmem` = **4**) is an on-chip SRAM operand pool that exists as a first-class memory tier on exactly one production codename — **Pufferfish (PXC, TPU v4)**. It is the one tier the [memory-space taxonomy](overview.md#1-the-six-region-taxonomy) calls "a Pufferfish-only read-mostly operand pool", and it is the reason the Pufferfish TensorCore can fetch two vector operands per bundle cycle: a dedicated `TensorCoreCmemLoad` slot issues a CMEM read in the same 51-bit bundle as the regular VMEM `VectorLoad`, doubling MXU operand-fetch bandwidth.

This page owns three things and nothing else: **(1)** the CMEM *pool layout* — the allocator `Config`, the byte/word/bank addressing model, and where the per-program image and stack sit; **(2)** the *per-generation sizing* — which codenames have CMEM at all, and how the size, word, and bank count are surfaced; and **(3)** the *constant packing* — what the compiler elects to place in CMEM and how it gets there. The on-chip SMEM model is [smem-scalar-memory.md](smem-scalar-memory.md); the tier taxonomy and the shared `BestFitAllocator` are [overview.md](overview.md); the bit-exact `cmem_load` bundle slot is on the ISA side ([slot-cmem-load-pf.md](../isa/slot-cmem-load-pf.md), [bundle-pf-51b.md](../isa/bundle-pf-51b.md)). This page links those and does not duplicate them.

A reimplementer needs to internalize the following contract:

- **CMEM is not a separate allocator class.** There is no `CmemAllocator`. CMEM uses the same two-stack model as every other tier ([overview.md §1](overview.md#1-the-six-region-taxonomy)): a compile-time `ProgramMemoryAllocator::AllocateBytes(MemorySpace::kCmem, …)` and a runtime `tpu::BestFitAllocator` instance, distinguished only by a 32-byte `Config{base_offset, end, alignment, granule}`.
- **CMEM sizing is two scalar fields on `Target`.** `Target::CmemSizeBytes()` (`Target+0x460`, `uint64`) is total bytes; `Target::CmemWordSizeBytes()` (`Target+0x510`, `uint32`) is the word = the allocator's alignment *and* granule. Both are filled at boot from `chip_parts.binarypb`; neither is overridden by any codename's `Target` subclass.
- **CMEM is real only where `MemBanks(kCmem)` returns a value.** Only `PufferfishTarget::MemBanks(kCmem)` returns a number (**32**); on every other codename `MemBanks(kCmem)` is a `LogMessageFatal`. The whole per-gen CMEM difference reduces to data: the `MemBanks(kCmem)` return, the presence of the `PufferfishTarget::LocalDmaBandwidth*Cmem` overrides, the presence of a `pxc::isa::*Cmem*` opcode family, and the `chip_parts.binarypb`-supplied size triple.
- **Constants are packed by MSA and filled by program-prologue DMA.** The placement decision is the [MSA](../compiler/msa-overview.md) pass gated by the `xla_tpu_cmem_*` flag family; the bytes arrive via `kDmaHbmToCmem` / `kDmaVmemToCmem` issued in the compiled program's prologue, *not* at NEFF load time.

| | |
|---|---|
| **Memory space** | `xla::jellyfish::MemorySpace::kCmem` = **4** (name table @ `0x21ce6b08`) |
| **Total-size accessor** | `Target::CmemSizeBytes()` @ `0x1d615e20` → `*(uint64*)(this+0x460)` |
| **Word/granule accessor** | `Target::CmemWordSizeBytes()` @ `0x1d617320` → `*(uint32*)(this+0x510)` |
| **Banks (Pufferfish only)** | `PufferfishTarget::MemBanks(kCmem)` @ `0x1d493900` = **32** (`{16,32,8}[MS-3]` @ rodata `0xb5305c8`) |
| **Compile-time placer** | `ProgramMemoryAllocator::AllocateBytes(MemorySpace::kCmem, …)` @ `0x1c629e40` |
| **Runtime allocator** | `tpu::BestFitAllocator` (`Config{base=0, end=CmemSizeBytes, align=granule=CmemWordSizeBytes}`); rehydrated by `CreateFromProto` @ `0x1c631f20` |
| **Address scale** | `LloRegionBuilder::CmemAddrScaled` @ `0x1d539980` (byte→word by `CmemWordSizeBytes`; `kCmem` guard) |
| **Constant form** | `LloAddress::MakeCmemConstant(long)` @ `0x1d60ba20` → `LloAddress(MemorySpace=4, off)` |
| **Placement gate** | `parent->module()->target().CmemSizeBytes() > 0` (asserted in `CreateVectorCmemResult` @ `0x1d4d99a0`) |
| **Master MSA switch** | `FLAGS_xla_tpu_cmem_memory_space_assignment` @ `0x223c1750` |
| **Per-gen availability** | Pufferfish (PXC, **TPU v4**) only; `MemBanks(kCmem)` is `LogFatal` on DF/JF/VF/GLC/GFC |
| **Confidence** | CONFIRMED (byte-anchored, decompile-verified) unless a row or callout says otherwise |

---

## 1. The CMEM Pool Layout

### Purpose

CMEM is a flat per-TensorCore SRAM region. At the allocator level it is **byte-flat from offset 0**; at the LLO bundle level it is **word-indexed**; at issue time it is **indirect-state-driven**. A reimplementer must reproduce all three views because the byte→word conversion happens in the address builder, not at issue, and the bank coordinate is derived from the word index.

### The allocator `Config`

CMEM is one `tpu::BestFitAllocator` constructed from the universal 32-byte `MemoryAllocator::Config` ([overview.md §3](overview.md#3-the-per-space-allocator--alignment-matrix)). For the CMEM tier the four fields are:

```c
tpu::MemoryAllocator::Config {
    base_offset_in_bytes_  = 0,                       // CMEM starts at sub-tile address 0
    allocatable_range_end_ = Target::CmemSizeBytes(), // *(uint64*)(Target+0x460)
    alignment_in_bytes_    = Target::CmemWordSizeBytes(), // *(uint32*)(Target+0x510)
    granule_in_bytes_      = Target::CmemWordSizeBytes(), // alignment == granule
};
```

The ctor (`0x1e817500`) asserts `alignment % granule == 0` and `alignment` is a power of two — for CMEM both are the same value, so both invariants hold trivially. Capacity is `end − base = CmemSizeBytes()`. There is **no** `base_offset` reservation and **no** `ReserveBottomOfMemory` call for CMEM — unlike HBM there is no DMA-floor / compile-time-alignment split.

> **NOTE —** CMEM has **no scoped-limit machinery** analogous to scoped-VMEM. There is no `Target::OverlayReservedCmemBytes`, no `DefaultPlatformScopedCmemBytes`, no `RequiredPostModuleScopedCmemBytes`. `LloRegionBuilder::AllocateScopedCmem` (`0x1d5181a0`) is a thin trampoline into the shared `AllocateScopedMemory(shape, MemorySpace::kCmem, name)` (`0x1d517c20`). Scoped-CMEM allocations are bounded only by `CmemSizeBytes() − (used chunks × chunk_bytes)`.

### The addressing model

| Level | Unit | Mechanism | Confidence |
|---|---|---|---|
| Allocator | **byte** offset, base 0 | `BestFitAllocator` hands out byte offsets relative to `base_offset = 0` | CONFIRMED |
| Bundle / LLO | **word** index | `CmemAddrScaled` (`0x1d539980`) divides the byte address by `CmemWordSizeBytes` (the `Granule` arg to `AddrScaled` `0x1d538880`); the bundle `Offset` field carries the *word* index | CONFIRMED |
| Bank (PF) | `(word_index) mod 32` | low bits of the word index pick one of `MemBanks(kCmem)=32` banks | HIGH |
| Issue | **indirect-state slot** | `CmemIndirectState` pre-builds 16-byte slots; the bundle `base_address` field carries a slot index | HIGH |

`CmemAddrScaled` is the linkage between the byte allocator and the word-indexed bundle. Its decompiled body asserts the address operand is `kCmem` before scaling:

```c
// LloRegionBuilder::CmemAddrScaled  @0x1d539980  (verbatim-derived)
LloCheckForFailure<MemorySpace, MemorySpace, kCmem>(...);   // v13[0] = 4 == kCmem
// on failure: UpdateStatus("base->memory_space() == MemorySpace::kCmem",
//                          "platforms/xla/service/jellyfish/llo_region_builder.cc", 4965)
return AddrScaled(this, byte_addr, base, off, /*Granule=*/0 → CmemWordSizeBytes, ...);
```

The constant form `LloAddress::MakeCmemConstant(long off)` (`0x1d60ba20`) builds an `LloAddress(MemorySpace=4, off)` directly (constructor `0x1d60b980`, `esi=0x4`). There is **no CMEM register file**: the destination of a CMEM load is a Vreg in the VS / VEX register file; CMEM is purely an operand *source* pool plus a small mutable stack.

### The per-program image and stack

The CMEM contents are a **per-program image**, not a boot-time constant pool. Two regions sit inside it:

- The **indirect-state table** at the head of the image, anchored by `Target::CmemStackBaseTableWordOffset()` (`0x1d6185c0`). Each entry is a 16-byte indirect-state record built at program start by a `CmemIndirectStateFactory` (`pfc::b0::Cmq16BIndirectStateFactory` on full Pufferfish, `plc::Cmq16BIndirectStateFactory` on Pufferfish-Lite — the `Cmq16B` name is the source of the inferred 16-byte word).
- A small **per-program stack** at `Target::CmemStackStartWordOffset()` (`0x1d618660`) holding runtime-mutable scalars (loop counters consumed by `kVectorCmemLoadAndPop`, the load-and-post-increment streaming-read opcode). The rodata anchors are `"cmem stack start"` and `"cmem stack base table"`.

> **GOTCHA —** CMEM is **per-program scratch, overwritten on every program switch**, not a pinned chip-wide constant store. The CMEM allocator is instantiated per program by `CreateFromProto` (`0x1c631f20`); the image is filled from HBM in the program's prologue; the next program's prologue overwrites it. There is no dedicated "CMEM load engine" outside the compiled program — every CMEM byte is written by an LLO-emitted DMA descriptor or a VPU store opcode. This is why CMEM holds a *working set* of weights/LUTs for one program, not the whole model.

---

## 2. Per-Generation CMEM Sizing

### Purpose

Whether CMEM exists at all is a per-codename decision, and it is decided by *data plus three small `Target` overrides*, not by any `TpuVersion` branch in the allocator. A reimplementer who drives CMEM off the compile-time placer must replicate the gate exactly: CMEM placement is attempted only when `CmemSizeBytes() > 0` **and** `MemBanks(kCmem)` does not `LogFatal` — which, in the published 0.0.40 runtime, is Pufferfish alone.

### The sizing accessors (decompile-verified)

Both accessors are direct field reads on the base `Target`, with **no per-codename override**:

```c
// Target::CmemSizeBytes()      @0x1d615e20
return *((uint64_t*)this + 140);   // 140 * 8  = 0x460   — total CMEM bytes / core

// Target::CmemWordSizeBytes()  @0x1d617320
return *((uint32_t*)this + 324);   // 324 * 4  = 0x510   — word = alignment = granule
```

There is **no `Target::CmemWordSizeLog2`** field (SMEM has one at `+0x4CC` because LLO bundle packing emits a byte→word shift for SMEM; CMEM's byte→word conversion is done inside `CmemAddrScaled` / the indirect-state classes, which hard-wire the granule).

`PufferfishTarget::MemBanks(MemorySpace)` is the one override that gates everything (decompiled @ `0x1d493900`):

```c
// PufferfishTarget::MemBanks(MemorySpace ms)   @0x1d493900
unsigned v4 = ms - 3;                 // kVmem=3 → 0, kCmem=4 → 1, kSmem=5 → 2
if (v4 >= 3)                          // ms >= 6 (sflag, …)
    LogMessageFatal("target_pufferfish.h", 228,
                    "Unsupported memory space specified for MemBanks()");
return qword_B5305C8[v4];             // {16, 32, 8}  → kCmem ⇒ 32
```

The base `Target::MemBanks` and every non-Pufferfish override land `kCmem` in a `LogMessageFatal` branch — `MemBanks(kCmem)` simply has no valid return on JF/VF/GL/GF.

### Per-codename CMEM matrix

Sizes come from `chip_parts.binarypb` at boot; alignment = granule = `CmemWordSizeBytes()` for every codename. The Pufferfish bandwidth/latency immediates were decoded from the `PufferfishTarget::LocalDmaBandwidth*` / `InitialDmaLatencyInNs` bodies and re-verified here against the decompile.

| TpuVersion / family | `Target` class | CMEM size (B) | Word / granule (B) | `MemBanks(kCmem)` | CMEM ISA opcodes | BW VMEM→CMEM (GB/s) | InitialDmaLatency (kCmem, ns) | Conf. |
|---|---|---|---|---:|---|---:|---:|---|
| Dragonfish (DF) | `DragonfishTarget` | `chip_parts` (≈ 0) | `chip_parts` | n/a (`LogFatal`) | none | 0 (base, unmodelled) | 240 (inherited) | HIGH |
| Jellyfish (JF) | `JellyfishTarget` | `chip_parts` (≈ 0) | `chip_parts` | n/a (`LogFatal`) | none | 0 (base) | 240 (constant) | HIGH |
| **Pufferfish (PXC, TPU v4)** | `PufferfishTarget` | **`chip_parts` (non-zero)** | **~16** (`Cmq16B`) | **32** | **dedicated PXC slot + VS + Misc** | **1121** | **50** | CONFIRMED (size LOW) |
| Viperfish (VFC) | `ViperfishTarget` | `chip_parts` (= 0) | `chip_parts` | n/a (`LogFatal`) | emitters only, lower to VL/VS | 0 (base) | 1200 / 0 (lite) | HIGH |
| Ghostlite (GLC / glc, v6e) | `GhostliteTarget` | `chip_parts` (= 0) | `chip_parts` | n/a (`LogFatal`) | none (`no gxc::glc::isa::*Cmem*`) | 0 (base) | 1200 | HIGH |
| 6acc60406 (GFC / gfc, TPU7x) | `GhostliteTarget` | `chip_parts` (= 0) | `chip_parts` | n/a (`LogFatal`) | none (`no gxc::gfc::isa::*Cmem*`) | 0 (base) | 1200 | HIGH |

> **GOTCHA (literal byte size is LOW) —** the literal per-codename CMEM byte size lives in the embedded `chip_parts.binarypb` proto's `TpuMemoryParts` record and has *not* been extracted from the binary; the C++ reads it blindly from `Target+0x460`. The word/granule "16 B" for Pufferfish is **inferred** from the `Cmq16BIndirectStateFactory` template-parameter name, not read from the proto. Public Pufferfish materials describe CMEM at the per-TensorCore-MiB scale, but a reimplementer should treat the literal size and word as `chip_parts`-supplied data, not as binary-derived constants.

> **NOTE —** Pufferfish ships **two sub-variants** that both carry the full CMEM story behind the same `PufferfishTarget` C++ class: `pfc::b0` (canonical Pufferfish-A) and `plc` (Pufferfish-Lite), distinguished only by their `CmemIndirectStateFactory<…Cmq16BIndirectStateFactory>` specialization and (potentially) by a different `chip_parts.binarypb` CMEM size. The C++ Target class does not branch between them.

### Why Pufferfish alone

The dedicated CMEM bundle slot exists because Pufferfish alone *models* CMEM. The structural reason (decompile-verified): only `PufferfishTarget` overrides `MemBanks(kCmem)` to a real value, and only `PufferfishTarget` overrides the `LocalDmaBandwidth*Cmem` virtuals away from the base `0.0`. The dedicated `cmem_load` slot is disjoint from the `vector_load` slot in the bundle, so a CMEM read and a VMEM read co-issue in the same cycle — the v4 double-operand-fetch datapath. The bit-exact slot layout is on [slot-cmem-load-pf.md](../isa/slot-cmem-load-pf.md).

The Pufferfish CMEM cost-model immediates, re-decoded from the decompiled `movabs`/`return` bodies (IEEE-754 doubles):

| Pair (`PufferfishTarget::…`) | Addr | Raw immediate | Value (GB/s) | Conf. |
|---|---|---|---:|---|
| `LocalDmaBandwidthVmemToCmem` | `0x1d4943e0` | `0x4091840000000000` | **1121.0** | CONFIRMED |
| `LocalDmaBandwidthCmemToVmem` | `0x1d494440` | `0x40A2460000000000` | **2339.0** | CONFIRMED |
| `LocalDmaBandwidthCmemToSmem` | `0x1d494480` | `0x4041000000000000` | **34.0** | CONFIRMED |
| `LocalDmaBandwidthSmemToCmem` | `0x1d4944e0` | `0x4041000000000000` | **34.0** | CONFIRMED |
| `LocalDmaBandwidthCmemToHbm` | `0x1d494420` | `0x4090E00000000000` | **1080.0** | CONFIRMED |
| `LocalDmaBandwidthCmemToCmem` | `0x1d494460` | `0x4092A40000000000` | **1193.0** | CONFIRMED |
| `InitialDmaLatencyInNs(kCmem)` | `0x1d493d00` | table `[555.0, 50.0][ms==4]` | **50 ns** | CONFIRMED |

The asymmetry (read side 2339 GB/s vs. write side 1121 GB/s) reflects a CMEM physical bus whose read path has roughly twice the wire count; the 50 ns startup is an order of magnitude below the 555 ns VMEM/HBM startup, reflecting CMEM's per-tile-resident short bus. There is **no `LocalDmaBandwidthHbmToCmem` accessor** at all — HBM↔CMEM traffic is cost-modelled as the VMEM-bridged formula (HBM→VMEM latency + VMEM→CMEM bandwidth).

---

## 3. Constant Packing — What Goes in CMEM and How

### Purpose

CMEM is the "tile-resident operand store" for compile-time-known, read-mostly data. This section is the placement *criteria* (what MSA elects to pack) and the *fill path* (how the bytes get there). The bit-level encoding of the load that reads them is on the ISA pages.

### What gets packed

Recovered from the PXC ISA opcode family, the `xla_tpu_cmem_*` flag family, and the runtime error strings:

| Packed content | Why CMEM | Source evidence | Conf. |
|---|---|---|---|
| **Convolution-filter / matmul weights** for fixed-shape models | streamed in once at program start, then read many times to feed the MXU operand pipe | dominant `kDmaHbmToCmem` → `TensorCoreCmemLoad` flow | HIGH |
| **Look-up tables** (activation, saturation, quantisation) | materialise-once, read-many-per-element | LUT placement; high CMEM read BW | HIGH |
| **Indirect-state descriptor blocks** | drive indirect-address resolution for vectorised reads | `CmemIndirectState` (16-B `Cmq16B` records) | CONFIRMED (class) |
| **All-reduce staging buffers** | frees VMEM for operand staging during collectives | `FLAGS_xla_tpu_scoped_cmem_for_all_reduce` @ `0x223b8de8` | HIGH |
| **MXU result direct-to-CMEM** | bypasses the VMEM round-trip on matmul output | `kVectorCmemResult` (`CreateVectorCmemResult` @ `0x1d4d99a0`) | CONFIRMED |
| **HLO output buffers (experimental)** | a configured fraction of CMEM for top-level outputs | `FLAGS_xla_tpu_experimental_cmem_fraction_for_hlo_outputs` @ `0x223b8cc8` | HIGH |
| **CMEM stack scalars** (loop counters) | runtime-mutable; streamed by load-and-pop | `kCmemStackOffset`, `kVectorCmemLoadAndPop` | HIGH |
| **In-CMEM copy spans** (`CmemSpan`) | sliding-window sequential reads | `FLAGS_xla_tpu_allow_in_cmem_copy` @ `0x223b30d0` (default off) | HIGH |

### The MSA placement decision

Packing is decided by the [Memory-Space Assignment](../compiler/msa-overview.md) pass — the *same* `kAlternate`-tier coloring that places VMEM ([overview.md §4](overview.md#4-the-compile-time--runtime-hand-off)) — gated by a per-tier `xla_tpu_cmem_*` flag family that mirrors `xla_jf_vmem_*` one-to-one. The master switch is `FLAGS_xla_tpu_cmem_memory_space_assignment` (`0x223c1750`); the byte cap is `FLAGS_xla_tpu_max_cmem_used_by_memory_space_assignment` (`0x223ae230`); prefetch/eviction caps, repack/retry counts, and overlap ratios round out the family (`0x223c0250 … 0x223c1e78`).

The two-tier tug-of-war (decompile-gated by `CmemSizeBytes() > 0`, see below):

| HloValue characteristic | Wins |
|---|---|
| MXU primary operand (matmul A/B), needs VS-slot ports | **the codename's `FastMemorySpace()`** — VMEM on VF/GL, HBM on JF, CMEM on PF (see per-codename breakdown below) |
| Weight tile for fixed-shape conv; LUT; quant table (read-mostly, small) | **CMEM** (high read BW, dedicated bundle slot) — *Pufferfish only* |
| Activation tile (changes per batch) | VMEM |
| All-reduce staging | CMEM if `xla_tpu_scoped_cmem_for_all_reduce` else VMEM |
| HLO top-level output | HBM default; CMEM if `…cmem_fraction_for_hlo_outputs > 0` |
| No live-range headroom anywhere | HBM (spill, fetch on demand) |

`FastMemorySpace()` is **per-codename and decompile-verified**: `JellyfishTarget::FastMemorySpace()` (`0x1d491a20`) returns **`kHbm`(=1)** (Jellyfish has no on-chip fast operand tier); `ViperfishTarget` (`0x1d49c3c0`) and `GhostliteTarget` (`0x1d499000`) both return **`kVmem`(=3)**; and — the one that makes the whole CMEM datapath exist — `PufferfishTarget::FastMemorySpace()` (`0x1d495f00`) returns **`kCmem`(=4)**. On Pufferfish, CMEM *is* the fast tier: a single `mov $0x4,%al; ret`. MSA therefore biases read-mostly tiles toward CMEM specifically on Pufferfish, where the active Target advertises both the `kCmem` fast space *and* non-zero `LocalDmaBandwidthCmemToVmem`.

> **NOTE (CONFIRMED gate) —** the entire CMEM placement path is short-circuited on any codename that advertises zero CMEM. The decompiled `LloInstruction::CreateVectorCmemResult` (`0x1d4d99a0`) asserts `parent->module()->target().CmemSizeBytes() > 0` and `LogMessageFatal`s otherwise — so a CMEM-result write is unreachable unless the active Target has non-zero CMEM. The same invariant guards MSA, alongside the rodata diagnostic `"CMEM is not supported."`. A reimplementer that omits the `CmemSizeBytes() > 0` precondition will mis-route CMEM placement onto a codename that cannot back it.

### The fill path

CMEM is **populated by program-prologue DMA**, not at NEFF / image load. The compiled XDB program embeds the CMEM image declarations as `ProgramMemoryMetadata_Allocation` entries with `memory_space = kCmem`, exactly like VMEM/HBM/SMEM; `CreateFromProto` (`0x1c631f20`) turns them into runtime allocator offsets. The bytes then arrive via the compiled prologue:

| Fill mechanism | Emitter | Cost-model BW (PF) |
|---|---|---|
| HBM→CMEM bulk fill (weights, LUTs) | `LloRegionBuilder::DmaHbmToCmemInBytes` @ `0x1d576e60` (`kDmaHbmToCmem`) | VMEM-bridged (no direct HbmToCmem BW) |
| VMEM→CMEM staged fill | `LloRegionBuilder::DmaVmemToCmemInBytes` @ `0x1d576c80` | 1121 GB/s |
| In-program VPU store | `TensorCoreVectorStore_CmemStore` / `…NoOffset` (folded into the VS slot) | (VS write port) |
| MXU result direct write | `kVectorCmemResult` (`CreateVectorCmemResult` @ `0x1d4d99a0`) | (result-buffer drain) |
| SMEM→CMEM scalar staging | `DmaSmemToCmemInBytes` @ `0x1d577220` (only this direction has an emitter; no `DmaCmemToSmemInBytes`) | 34 GB/s |

After the bulk fill, the `CmemIndirectState` ctor builds the indirect-state vector at the head of the image so the VPU bundle slot can issue `TensorCoreCmemLoad` against the resident tiles. Eviction back to HBM (the MSA spill path) uses `DmaCmemToHbmInBytes` (`0x1d577040`).

> **GOTCHA — read-only is a convention, not a hardware write-protect.** CMEM is "read-mostly" by *compiler classification* only: the ISA has explicit CMEM write opcodes (`VectorCmemStore`, `VectorCmemStoreNoOffset`), seven DMA write paths target CMEM, and the MXU result drain writes it. LSRA-v2 treats compile-time-constant CMEM loads as rematerialisable, and MSA biases read-mostly tiles to CMEM, but a buggy program that stores over a constant region will silently corrupt subsequent reads — there is no hardware fault. The protection is purely SSA / dataflow-level.

### Exhaustion handling

Compile-time CMEM exhaustion modes (from runtime error strings):

- **Requested size exceeds capacity** — `result.memory_space_assignment_size_in_bytes <= target.UserAllocationSharedMemoryLimitBytes(MemorySpace::kCmem)` (`0x1d616680`); violation is `LogMessageFatal` (`"Requested Cmem size for memory space assignment (…)"`).
- **Target advertises zero CMEM** — the `CmemSizeBytes() > 0` gate above; emits `"CMEM is not supported."` and skips CMEM (or fails the compile when a custom-call required CMEM).
- **Out-of-range byte address** — `byte_address < target().CmemSizeBytes()` asserted at every CMEM operand emission; violation is `LogMessageFatal`.
- **MSA cannot place** — retries up to `xla_tpu_cmem_max_retries`, then falls back to HBM and logs the soft diagnostics `"Ignoring cmem allocation: …"` / `"Ignoring cmem allocation storage"`.
- **CmemSpan disabled** — `use_cmem_span_ == true` / `!use_cmem_span_` invariants in `cmem_indirect_state_factory.h`; a span requested while `xla_tpu_allow_in_cmem_copy` is off `LogFatal`s with `"Disabled cmem span"`.

Runtime exhaustion goes through the standard `BestFitAllocator::Allocate` `ResourceExhaustedError` path ([hbm-allocator.md](hbm-allocator.md)) — rare, because the CMEM image is fully materialised at compile time and replayed at load.

---

## Cross-References

- [overview.md](overview.md) — the six-region memory-space taxonomy, the `MemorySpace` enum (`kCmem`=4), the shared `BestFitAllocator` and the compile-time→runtime hand-off this page specializes for CMEM
- [smem-scalar-memory.md](smem-scalar-memory.md) — the sibling on-chip scalar tier (`kSmem`=5); the SMEM↔CMEM 34 GB/s staging pair and the `CmemWordSizeLog2`-absent contrast
- [vmem-allocator.md](vmem-allocator.md) — the `kAlternate` fast tier CMEM competes with in MSA; the per-gen VMEM word/bank `Config`
- [hbm-allocator.md](hbm-allocator.md) — the universal best-fit allocate/deallocate algorithm CMEM shares; the runtime `ResourceExhaustedError` path
- [tpu-buffer-layout.md](tpu-buffer-layout.md) — how a logical XLA buffer maps to physical offsets across these tiers
- [slot-cmem-load-pf.md](../isa/slot-cmem-load-pf.md) — the bit-exact Pufferfish `cmem_load` bundle slot (the read access path this page references)
- [bundle-pf-51b.md](../isa/bundle-pf-51b.md) — the 51-byte Pufferfish TensorCore bundle the dedicated CMEM-load slot lives in
- [memory-space-enum.md](../isa/memory-space-enum.md) — the LLO `MemorySpace` enum at the ISA / operand-tag level
- [msa-overview.md](../compiler/msa-overview.md) — the placement pass that colors CMEM via the `xla_tpu_cmem_*` flag family
- [back to index](../index.md) — Part X — On-Chip Memory & DMA
