# HBM DMA Alignment Contract

> *All addresses, struct offsets, line numbers, and magic constants on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`, clang/LLVM trunk). Other versions will differ.*

## Abstract

Every HBM DMA in libtpu is gated by a single alignment quantum: `jf_driver::kHbmMinimumDmaAlignment = 1024 B`. This is not advisory — it is enforced *twice*, at two independent layers, by two different mechanisms:

1. **At DMA-issue time** (`tpu::JfDmaIssuer::WritePremappedHbm` and its siblings), the byte offset, the buffer size, and the minimum transfer length are validated with **recoverable** `RetCheck`s. A misaligned request does not crash the process; it short-circuits into the caller's error callback with a non-OK `Status`.
2. **At descriptor-build time** (`asic_sw::driver::deepsea::jxc::HbmWriteDescriptor::SetHbmAddress`), the absolute HBM address is masked with `0x3FF` and validated with a **fatal** `LogMessageFatal` `CHECK`. By the time an address reaches the hardware descriptor it is too late to recover — a misaligned address here is a programming error and aborts.

The 1024 B floor is the reason the [HBM BestFit allocator](hbm-allocator.md) uses `1024 B` as its HBM `Config.alignment_in_bytes_` quantum: an allocator that handed out finer-grained HBM offsets would hand the DMA issuer addresses it rejects. This page owns the **DMA alignment floor, the `WritePremappedHbm` premapped path, and the alignment `CHECK`s**. It does *not* own the allocator's free-list math (that is [hbm-allocator.md](hbm-allocator.md)) or the intra-chip descriptor field layout (that is [../dma/intra-chip-descriptor.md](../dma/intra-chip-descriptor.md)); it links them.

For reimplementation, the contract is:

- **The floor.** `kHbmMinimumDmaAlignment = 1024 = 0x400`; the round-down mask is `~0x3FF = 0xFFFFFFFFFFFFFC00`; the modulo test is `x & 0x3FF == 0`. The same constant doubles as `kMinimumDmaLengthBytes = 1024` — the smallest legal transfer.
- **Three issue-time `RetCheck`s** (recoverable): `byte_offset % 1024 == 0`, `size % 1024 == 0`, `size >= 1024`. On failure each routes through `ScheduleCallbackOnError` — the user callback fires with the error, no abort.
- **Two descriptor-time `CHECK`s** (fatal): `address < kAddressOffsetMaxBytes` (`2^50`) and `(address & 1023) == 0`. These guard the 64-bit HBM-address field bit-packed into the JXC `HbmWriteDescriptor`.
- **The allocator quantum mirrors the floor.** HBM's `Config.alignment_in_bytes_` *is* `1024 B`, so every offset the allocator emits is already a multiple of the floor.
- **The 16 KiB compile-time program alignment** (`FLAGS_xla_jf_program_hbm_alignment_in_kib = 16`) is a far stricter, *upstream* quantum applied before MSA placement; it is unrelated to the DMA floor and does not relax it.

| | |
|---|---|
| **DMA floor constant** | `jf_driver::kHbmMinimumDmaAlignment = 1024 B` (`0x400`) |
| **Round-down mask** | `~0x3FF` = `0xFFFFFFFFFFFFFC00` |
| **Modulo test** | `x & 0x3FF == 0` |
| **Min transfer length** | `jf_driver::kMinimumDmaLengthBytes = 1024 B` (same constant) |
| **Issue-time guard** | `JfDmaIssuer::WritePremappedHbm` @ `0xe73db80` (3 `RetCheck`s, **recoverable**) |
| **Single-chunk variant** | `JfDmaIssuer::WritePremappedHbmSingleChunk` @ `0xe73eec0` |
| **Read variant** | `JfDmaIssuer::ReadPremappedHbm` @ `0xe73c880` |
| **Descriptor-time guard** | `HbmWriteDescriptor::SetHbmAddress` @ `0xe7ce7e0` (2 `CHECK`s, **fatal**) |
| **Max HBM address** | `kAddressOffsetMaxBytes = 0x4000000000000` (`2^50`) |
| **Issue source label** | `learning/45eac/tpu/runtime/hal/internal/jxc/jf_dma_issuer.cc` |
| **Descriptor source label** | `platforms/asic_sw/driver/deepsea/jxc/common/hbm_write_descriptor.h` |
| **Allocator quantum** | HBM `Config.alignment_in_bytes_` = `1024 B` (see [hbm-allocator.md](hbm-allocator.md)) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## Scope and Boundaries

This page owns the *HBM DMA alignment floor* and the issue-/descriptor-time checks that enforce it. Adjacent concerns live elsewhere; do not duplicate them here:

| Concern | Owner page |
|---|---|
| The `BestFitAllocator` free-list, best-fit search, coalescing, split policy, and the *allocator's* alignment quantum | [hbm-allocator.md](hbm-allocator.md) |
| The intra-chip DMA descriptor field layout (slice offset/size, stride, ring index) | [../dma/intra-chip-descriptor.md](../dma/intra-chip-descriptor.md) |
| The five-tier on-chip memory map and where HBM sits | [overview.md](overview.md) |
| How a device buffer's offset maps to the on-device tile layout | [tpu-buffer-layout.md](tpu-buffer-layout.md) |
| Host→device / device→host transfer paths and the premapped staging pool | [overview.md](overview.md), [../dma/host-device-dma.md](../dma/host-device-dma.md) |

What this page owns: the **1024 B floor constant**, the **`WritePremappedHbm` premapped path**, and the **alignment `CHECK`s** — i.e. the boundary conditions every HBM DMA must satisfy and the two code sites that verify them.

> **NOTE —** "premapped" here means the HBM-side endpoint of a DMA whose host buffer has already been registered (pinned and DMA-mapped) with the driver, so the issue path can skip the bounce-buffer copy and stream directly. The premapped *host pool* (the `PremappedMemoryManager` round-robin of `posix_memalign` partitions) is a separate object on the allocator side; see [overview.md](overview.md). The functions on this page are the *device-HBM-side* issue path that consumes those premapped buffers.

---

## The Floor Constant

`jf_driver::kHbmMinimumDmaAlignment` is `1024` (`0x400`). It never appears as a named symbol in the stripped binary — it is recovered from three independent, mutually-consistent encodings in the decompile:

| Encoding | Where it appears | Decompile evidence |
|---|---|---|
| Round-**down** mask | `WritePremappedHbm`, `ReadPremappedHbm`, single-chunk | `v8 = (a2 + 1023 if a2<0 else a2) & 0xFFFFFFFFFFFFFC00` |
| Modulo (round-**up** to zero) mask | size check at all issue sites | `v6 & 0x3FF` (`0x3FF = 1024 − 1`) |
| Literal | the min-length comparison | `*(_QWORD *)&v50 = 1024; if (size < 0x400) RetCheck` |
| `kAddressOffsetMaxBytes` literal | `SetHbmAddress` | `MakeCheckOpString(a2, 0x4000000000000, "address < kAddressOffsetMaxBytes")` |

The mask `0xFFFFFFFFFFFFFC00` is exactly `~0x3FF`, the canonical round-down-to-1024 operation. The `+ 1023` adjustment guarded by `if (a2 >= 0)` is the compiler's branchless implementation of a signed round-toward-zero before the mask — it only matters for negative offsets (which would themselves fail the subsequent `RetCheck`); for the non-negative offsets that actually occur, `v8 = a2 & ~0x3FF` is the floored value, and `a2 != v8` is true exactly when `a2` is misaligned.

`kMinimumDmaLengthBytes` is *also* `1024`. The decompile loads the literal `1024` into the comparison operand at every issue site, so the smallest legal HBM transfer is one full alignment quantum — there is no sub-1024-byte DMA.

> **NOTE —** the floor is a *byte* quantum, not a granule count. The DMA chunking arithmetic later in each issue routine divides by `a1[22]` (the per-issuer max-chunk size at issuer offset `+0x110`) to split a large transfer into ring-sized chunks; that chunk size is a separate, larger quantum and is documented with the descriptor on [../dma/intra-chip-descriptor.md](../dma/intra-chip-descriptor.md). The 1024 B floor is the *alignment* every chunk boundary inherits, not the chunk size itself.

---

## `WritePremappedHbm` — the Issue-Time Guard

`tpu::JfDmaIssuer::WritePremappedHbm` (`0xe73db80`) is the canonical premapped-HBM write. Its signature (demangled) is:

```text
void JfDmaIssuer::WritePremappedHbm(
        int64_t                          byte_offset,   // HBM offset to write at
        SlicedDmaBuffer                  buffer,        // premapped source view (ptr+size at a3[1])
        absl::AnyInvocable<void(const Status&)> on_done,
        const std::optional<asic_sw::deepsea::SyncFlag>& sync)
```

The first thing the function does — before touching any queue, descriptor, or hardware state — is run three alignment guards on `byte_offset` (`a2`) and the buffer's `size` (`a3[1]`, captured as `v6`):

```c
// WritePremappedHbm @ 0xe73db80 — alignment guards, byte-confirmed
v6 = a3[1];                                   // buffer.size()
v7 = a2 + 1023; if (a2 >= 0) v7 = a2;         // signed round-toward-zero
v8 = v7 & 0xFFFFFFFFFFFFFC00LL;               // floor to 1024  (~0x3FF)
*(int64_t*)&v43 = a2 - v8;                    // remainder = byte_offset % 1024

// CHECK 1 (line 440): byte_offset is 1024-aligned
if (a2 != v8) {                               // remainder != 0
    v32 = MakeCheckOpString<long,int>(&v43, &v50,
              "byte_offset % jf_driver::kHbmMinimumDmaAlignment == 0");
    if (v32) { v33 = 440; goto fail; }        // RetCheckFailSlowPath -> error callback
}

// CHECK 2 (line 442): buffer size is a multiple of 1024
*(int64_t*)&v43 = v6 & 0x3FF;
if ((v6 & 0x3FF) != 0) {
    v32 = MakeCheckOpString<unsigned long,int>(&v43, &v50,
              "size % jf_driver::kHbmMinimumDmaAlignment == 0");
    if (v32) { v33 = 442; goto fail; }
}

// CHECK 3 (line 444): transfer is at least one quantum long
*(int64_t*)&v43 = v6;
*(int64_t*)&v50 = 1024;
if ((unsigned long)v6 < 0x400) {              // size < kMinimumDmaLengthBytes
    v32 = MakeCheckOpString<unsigned long,unsigned long>(&v43, &v50,
              "size >= jf_driver::kMinimumDmaLengthBytes");
    if (v32) { v33 = 444; goto fail; }
}
```

The `fail` label is `RetCheckFailSlowPath` against source `learning/45eac/tpu/runtime/hal/internal/jxc/jf_dma_issuer.cc` at the recorded line, followed by `tpu::ScheduleCallbackOnError`, which invokes the caller's `on_done` (`a4`) with the failed `Status` and then destroys the two `StatusBuilder`s. **This is recoverable**: a misaligned premapped write does not abort the process — it completes asynchronously with a non-OK status, exactly like any other DMA error.

### Why three checks, in this order

The three checks are a complete precondition for the chunking arithmetic that follows. Once `byte_offset` and `size` are both 1024-multiples and `size >= 1024`:

- the **fast path** (`size <= a1[22]`, the max chunk size at issuer offset `+0x110`) enqueues a single `{offset, MaybeOwningDmaBuffer, optional<SyncFlag>}` tuple onto the issuer's `BufferedQueue` (`a1[5] + 0x100`);
- the **slow path** splits the transfer into `ceil(size / max_chunk)` chunks under an `AsyncTaskGroup`, each chunk inheriting alignment from the aligned `byte_offset` and aligned per-chunk size, with a `slice_offset + slice_size <= base.size()` bounds `CHECK` (a fatal `LogMessageFatal` at `dma_buffer_utils.h:40`) per chunk.

Because the inputs are pre-floored, every chunk boundary is automatically 1024-aligned and the per-chunk descriptor address that eventually reaches `SetHbmAddress` is guaranteed to pass the fatal mask check below. The issue-time `RetCheck`s are the *recoverable* front line; the descriptor-time `CHECK` is the *fatal* backstop.

| Function | Address | Role | Confidence |
|---|---|---|---|
| `WritePremappedHbm` | `0xe73db80` | premapped HBM write; 3 issue-time alignment `RetCheck`s + chunk split | CONFIRMED |
| `WritePremappedHbmSingleChunk` | `0xe73eec0` | single-chunk variant; same checks, lines 551/553/555 | CONFIRMED |
| `ReadPremappedHbm` | `0xe73c880` | premapped HBM read; mirror checks at lines 344/346/348 | CONFIRMED |
| `RetCheckFailSlowPath` | (issuer-local) | builds the error `Status`, fed to `ScheduleCallbackOnError` | CONFIRMED |
| `ScheduleCallbackOnError` | (issuer-local) | fires `on_done` with the error; no abort | CONFIRMED |

---

## The Single-Chunk and Read Variants

The check sequence is identical across the premapped family; only the recorded source line numbers and the field that holds `size` differ.

`WritePremappedHbmSingleChunk` (`0xe73eec0`) reads `size` from `*(buffer + 8)` rather than `a3[1]`, and its checks land at lines **551 / 553 / 555** with messages keyed on `buffer.size()`:

```c
// WritePremappedHbmSingleChunk @ 0xe73eec0 — same floor, different lines
v7 = (a2 + 1023 if a2<0 else a2) & 0xFFFFFFFFFFFFFC00;
if (a2 != v7)  RetCheck("byte_offset % jf_driver::kHbmMinimumDmaAlignment == 0");  // 551
v8 = *(uint64_t*)(a3 + 8);                                                          // buffer.size()
if (v8 & 0x3FF)  RetCheck("buffer.size() % jf_driver::kHbmMinimumDmaAlignment == 0");// 553
if (v8 < 0x400)  RetCheck("buffer.size() >= jf_driver::kMinimumDmaLengthBytes");     // 555
// then a fatal CHECK: tensor_node_ != nullptr (line 559) before enqueue
```

`ReadPremappedHbm` (`0xe73c880`) mirrors the write path exactly — `byte_offset % 1024 == 0` (line 344), `size % 1024 == 0` (line 346), `size >= 1024` (line 348) — confirming the floor is symmetric across read and write. The read path then chunks against the issuer's per-read max size at offset `+0xB0` (`*(_QWORD*)(v50 + 176)`) and dispatches each chunk to `NodeFabricTransferHbmToHostInternal`.

> **NOTE —** the single-chunk variant adds one extra *fatal* `CHECK` that the multi-chunk path does not surface at the top: `tensor_node_ != nullptr` (line 559). That is a structural precondition (the issuer must be bound to a tensor node), not an alignment check, but it shows the single-chunk path expects to enqueue directly without the `AsyncTaskGroup` fan-out.

---

## `SetHbmAddress` — the Fatal Descriptor Backstop

When a chunk's HBM address is written into the JXC hardware descriptor, `asic_sw::driver::deepsea::jxc::HbmWriteDescriptor::SetHbmAddress` (`0xe7ce7e0`) re-validates it — this time **fatally**:

```c
// HbmWriteDescriptor::SetHbmAddress @ 0xe7ce7e0 — byte-confirmed
__int64 SetHbmAddress(HbmWriteDescriptor *this, uint64_t address) {
    uint64_t v7 = address;
    // CHECK A (line 37): address fits the 50-bit HBM address field
    if (address >> 50) {                                  // address >= 2^50
        v4 = MakeCheckOpString(address, 0x4000000000000LL,
                 "address < kAddressOffsetMaxBytes");
        v5 = 37;  goto fatal;                             // LogMessageFatal -> Flush -> ~ -> abort
    }
    // CHECK B (line 38): address is 1024-aligned
    if (address & 0x3FF) {                                // low 10 bits set
        v4 = MakeCheckOpString(address & 0x3FF, 0,
                 "(address & (kHbmMinimumDmaAlignment - 1)) == 0");
        v5 = 38;
fatal:
        LogMessageFatal("platforms/asic_sw/driver/deepsea/jxc/common/hbm_write_descriptor.h", v5, v4);
        LogMessage::Flush(...); ~LogMessageFatal(...);    // process abort
    }
    return BitCopy(this, 64, &v7, 0, 64);                 // pack the 64-bit address field
}
```

Two facts make this the *backstop*, not the *front line*:

- **It is fatal.** Unlike the issue-time `RetCheck`s, a failure here is `LogMessageFatal` → `Flush` → destructor → `abort`. There is no error callback, no recovery. A misaligned address reaching the descriptor is treated as a libtpu/compiler bug, because the issue path should already have rejected it recoverably.
- **It expresses the floor as a bitmask, not a modulo.** `(address & (kHbmMinimumDmaAlignment − 1)) == 0` with `kHbmMinimumDmaAlignment − 1 = 0x3FF` is the algebraic identity of the issuer's `byte_offset & 0x3FF` test, confirming both layers share the same `1024` constant. The address is finally bit-packed into a 64-bit descriptor field via `BitCopy(this, 64, &addr, 0, 64)`.

`CHECK A` additionally bounds the address to `kAddressOffsetMaxBytes = 0x4000000000000 = 2^50`, i.e. the JXC HBM-address field is 50 bits wide (a 1 PiB addressable span). The descriptor field layout that this address feeds into is documented on [../dma/intra-chip-descriptor.md](../dma/intra-chip-descriptor.md).

| Constant / check | Value / message | Severity | Confidence |
|---|---|---|---|
| `kHbmMinimumDmaAlignment` | `1024` (`0x400`); mask `0x3FF` | — | CONFIRMED |
| `kMinimumDmaLengthBytes` | `1024` (same constant) | — | CONFIRMED |
| `kAddressOffsetMaxBytes` | `0x4000000000000` (`2^50`) | — | CONFIRMED |
| `byte_offset % 1024 == 0` | issue-time, `WritePremappedHbm:440` | recoverable `RetCheck` | CONFIRMED |
| `size % 1024 == 0` | issue-time, `WritePremappedHbm:442` | recoverable `RetCheck` | CONFIRMED |
| `size >= 1024` | issue-time, `WritePremappedHbm:444` | recoverable `RetCheck` | CONFIRMED |
| `(address & 1023) == 0` | descriptor-time, `SetHbmAddress:38` | **fatal** `CHECK` | CONFIRMED |
| `address < 2^50` | descriptor-time, `SetHbmAddress:37` | **fatal** `CHECK` | CONFIRMED |

---

## How the Allocator Quantum Relates to the Floor

The [HBM BestFit allocator](hbm-allocator.md) constructs its HBM instance with `Config.alignment_in_bytes_ = 1024 B` — *exactly* the DMA floor — and that single fact ties the two subsystems together:

- **Every offset the allocator emits is already a 1024-multiple.** `Allocate` rounds each request up to `alignment_in_bytes_` via `(size + align − (size != 0)) & −align`, and the ctor rounds the region end *down* to the same quantum. So `base_offset + offset` — the address `Allocate` returns — is always a multiple of 1024. When that address later becomes a DMA `byte_offset`, the issue-time `byte_offset % 1024 == 0` `RetCheck` is satisfied **by construction**.
- **Allocation sizes are 1024-multiples too**, so a DMA covering a whole buffer satisfies `size % 1024 == 0` automatically. The `size >= 1024` floor means a buffer must be at least one quantum, which the allocator's round-up also guarantees for any non-zero request.
- **The relationship is a contract, not a coincidence.** If the allocator used a *smaller* HBM quantum (say 256 B), it could hand out a `byte_offset` like `0x300` that the issuer's `RetCheck` would reject and — worse — that `SetHbmAddress` would *fatally* reject if it slipped through. The allocator quantum is therefore pinned to the DMA floor as the tightest value that keeps every emitted address DMA-legal. The allocator's quantum is the *minimum*; the [compile-time program alignment](#the-16-kib-compile-time-program-alignment) is a much larger, separate quantum layered on top.

```text
            compile time                         load / run time
   ┌──────────────────────────┐        ┌────────────────────────────────────┐
   │ MSA / ProgramMemory-     │        │ BestFitAllocator (HBM)               │
   │ Allocator                │ proto  │   Config.alignment_in_bytes_ = 1024  │
   │   round up to 16 KiB ────┼───────▶│   round up to 1024  (no-op on        │
   │   (program HBM align)    │offsets │     already-16KiB static offsets)    │
   └──────────────────────────┘        └───────────────┬──────────────────────┘
                                                        │ base+offset (×1024)
                                                        ▼
                                        ┌────────────────────────────────────┐
                                        │ JfDmaIssuer::WritePremappedHbm       │
                                        │   RetCheck byte_offset % 1024 == 0   │ recoverable
                                        │   RetCheck size       % 1024 == 0    │
                                        │   RetCheck size       >= 1024        │
                                        └───────────────┬──────────────────────┘
                                                        │ per-chunk address
                                                        ▼
                                        ┌────────────────────────────────────┐
                                        │ HbmWriteDescriptor::SetHbmAddress    │
                                        │   CHECK address < 2^50               │ FATAL
                                        │   CHECK (address & 1023) == 0        │
                                        └──────────────────────────────────────┘
```

The full allocator quantum mechanics (the round-up formula, the region-end round-down, `max_aligned_size_`, and why padding is internal fragmentation owned by the allocation) live on [hbm-allocator.md § The Alignment Quantum](hbm-allocator.md#the-alignment-quantum) — this page does not repeat them.

---

## The 16 KiB Compile-Time Program Alignment

There is a *second*, much coarser HBM quantum that operates entirely at compile time and must not be confused with the 1024 B DMA floor.

`FLAGS_xla_jf_program_hbm_alignment_in_kib` (default `16`, the `.data` global at `0x223b4888`) rounds every program-level HBM tensor up to **16 KiB** *before* MSA placement. This is XLA-side, runs on the host during compilation, and exists to accommodate XLA's stride / sub-tile addressing schemes and slice-prefetch boundaries — concerns that have nothing to do with the DMA engine's alignment requirement.

The relationship between the two quanta:

- **16 KiB is a multiple of 1024 B** (`16 × 1024 = 16384 = 16 × 1024`), so any 16-KiB-aligned static offset is *automatically* 1024-aligned. When MSA's frozen offsets are replayed into the runtime allocator, the allocator's own 1024 B round-up is a no-op on them.
- **The two are independent.** The 16 KiB program alignment is far stricter than the DMA floor and is applied only to the static (MSA-placed) surface. Dynamic runtime allocations (transfer staging, async-copy scratch) go through the allocator's 1024 B quantum directly and are *not* 16-KiB-aligned. Both surfaces satisfy the DMA floor; only the static surface satisfies the program alignment.
- **Bumping the flag does not change the DMA floor.** Raising `xla_jf_program_hbm_alignment_in_kib` makes static tensors coarser-aligned (wasting more HBM to internal fragmentation) but cannot relax the 1024 B issue-/descriptor-time checks, which are hard-coded constants in the binary.

| Memory space | Minimum DMA / access alignment | Compile-time program alignment (default) | Source of quantum |
|---|---|---|---|
| HBM / `kHbm` | **1024 B** (`jf_driver::kHbmMinimumDmaAlignment`) | **16 KiB** (`xla_jf_program_hbm_alignment_in_kib = 16`) | DMA floor (binary const); program flag (XLA) |
| `kPinnedHbm` | 1024 B (same floor, plus the host-side pinning lock) | 16 KiB (inherits from HBM) | same |
| VMEM / `kVmem` | per-generation (32 / 64 / 128 B; from `chip_parts.binarypb`) | per-codec bundle width | `Config.alignment_in_bytes_` — see [vmem-allocator.md](vmem-allocator.md) |
| SMEM / SFLAG / CMEM | per-generation (typically 32 B; SFLAG 4 B) | per tier | `Config.alignment_in_bytes_` |

> **NOTE —** the per-generation VMEM / SMEM / CMEM rows above are carried for context only; their alignment is governed by the tier's `Config.alignment_in_bytes_`, sourced from the embedded `chip_parts.binarypb` resource, and is owned by the sibling tier pages ([vmem-allocator.md](vmem-allocator.md), [cmem-pool.md](cmem-pool.md)). Only the HBM and `kPinnedHbm` rows — the 1024 B DMA floor and the 16 KiB program alignment — are owned here.

---

## Reimplementer's Checklist

To reproduce the HBM DMA alignment contract:

1. **Define `kHbmMinimumDmaAlignment = 1024` and `kMinimumDmaLengthBytes = 1024`.** They are the same value but expressed for different intents (alignment vs. minimum length). The derived mask is `0x3FF`; the round-down mask is `~0x3FF`.
2. **At DMA issue, validate recoverably.** Before any chunking, check `byte_offset % 1024 == 0`, `buffer.size() % 1024 == 0`, and `buffer.size() >= 1024`. On failure, route to the completion callback with a non-OK status — do *not* abort. These are `RetCheck`s, returning `Status`.
3. **At descriptor build, validate fatally.** When packing the absolute HBM address into the hardware descriptor, `CHECK address < 2^50` and `CHECK (address & 1023) == 0`. These are `LogMessageFatal` — by this layer a misaligned address is a logic error, because step 2 should have caught it.
4. **Pin the allocator quantum to the floor.** Construct the HBM allocator with `alignment_in_bytes_ = 1024`. Never finer; finer offsets would fail step 2/3. Coarser (e.g. matching the 16 KiB program alignment) is legal but wastes HBM.
5. **Keep the program alignment separate.** Apply the 16 KiB compile-time round-up only to static MSA-placed tensors, upstream of placement; never let it relax the runtime 1024 B floor.

---

## Cross-References

- [hbm-allocator.md](hbm-allocator.md) — the runtime `BestFitAllocator`; its `Config.alignment_in_bytes_ = 1024 B` HBM quantum is the value this page's floor pins
- [../dma/intra-chip-descriptor.md](../dma/intra-chip-descriptor.md) — the JXC DMA descriptor field layout that `SetHbmAddress` packs the validated address into
- [overview.md](overview.md) — the five on-chip tiers + host memory; where HBM and the premapped staging pool sit
- [vmem-allocator.md](vmem-allocator.md) — the sibling VMEM tier, same allocator class, different (smaller) `Config` quantum
- [tpu-buffer-layout.md](tpu-buffer-layout.md) — how a device buffer's offset maps to the on-device tile layout
- [../dma/host-device-dma.md](../dma/host-device-dma.md) — host↔device transfer paths that consume premapped buffers
- [../compiler/msa-overview.md](../compiler/msa-overview.md) — the compile-time placement pass that applies the 16 KiB program alignment before freezing static offsets
- [back to index](../index.md)
