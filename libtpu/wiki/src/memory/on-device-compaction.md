# On-Device Compaction

> *All addresses, struct offsets, vtable slots, and magic constants on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id md5 `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`, clang trunk). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. Other versions will differ.*

## Abstract

This page answers one question precisely, with binary evidence: **does the libtpu runtime perform HBM defragmentation by relocating live buffers, and how?** The answer is **yes** — and that is the non-obvious result, because the device allocator (`tpu::BestFitAllocator`, see [hbm-allocator.md](hbm-allocator.md)) is a classic non-moving boundary-tag allocator whose `Allocate`/`Deallocate` paths *only* coalesce adjacent free blocks. Live-buffer relocation lives in a single, separate, OOM-triggered method: **`tpu::BestFitAllocator::Compact`** (`0x1e81c360`, vtable slot `vt+0x90`).

The clean way to state the architecture is a three-way split of "where does each buffer sit in HBM":

1. **Compile-time placement** — XLA's MSA + jellyfish `ProgramMemoryAllocator` freeze static offsets into the program ([../compiler/msa-overview.md](../compiler/msa-overview.md)). The bulk of HBM occupancy is laid out once, at compile time, which is *why* the runtime so rarely needs to defragment — packing is solved before the program ever runs.
2. **Runtime free-list math (non-moving)** — `Allocate`/`Deallocate` service the *dynamic* surface (user input/output PJRT buffers, transfer staging, async-copy scratch, the dynamic program stack) with best-fit search + **eager bidirectional coalescing on free**. This is documented on [hbm-allocator.md](hbm-allocator.md); it never moves a live buffer.
3. **Runtime compaction (moving)** — when even fully-coalesced free space cannot satisfy a request, `Compact` *relocates* movable live buffers downward to consolidate free space, **emitting a relocation plan** (`std::vector<tpu::TpuMemmoves::Memmove>`) that a separate codegen path turns into on-device DMA programs.

So the page is a present/absent inventory. **PRESENT:** a full live-buffer relocation path (`Compact` → `TpuMemmoves` → DMA codegen → enqueue), an OOM-driven retry chain, a pinned-set that keeps MSA-static and aliased buffers immovable, and donation-driven *reuse* (a zero-copy alternative to allocation). **ABSENT:** any relocation inside `Allocate`/`Deallocate` (those coalesce only); any periodic/background defragmenter; any device-side arena/slab compaction; any growth-by-doubling. The distinction this page owns is precisely **coalescing (in-place merge of free neighbours, every free) vs. compaction (relocation of live buffers, only on OOM)**.

| | |
|---|---|
| **Compaction performed?** | **YES** — live movable buffers are relocated to consolidate free space |
| **Trigger** | OOM only (allocation failure → retry); never periodic/background |
| **Relocation engine** | `tpu::BestFitAllocator::Compact(flat_hash_set<long> pinned)` @ `0x1e81c360` (vt+0x90) |
| **Output** | `std::vector<tpu::TpuMemmoves::Memmove>` — a relocation *plan*; bytes moved by a later codegen step |
| **Byte movement** | `TpuCompactionIsaEmitterCodegen::Generate` @ `0x1090ece0` (VMEM-staged DMA) → `EnqueueCompactionImpl` @ `0x1d12ed00` → `CompactionRunner` |
| **Device driver entry** | `tpu::System::CompactMemory` @ `0x1d0b6000` (shrink program stacks → `EnqueueCompaction`) |
| **PJRT entry** | `TpuClient::DefragmentMemory` @ `0xf7fd660` → `EnqueueDefragmentMemory` @ `0xf7fd180` |
| **Retry policy** | `TpuClient::ShouldRetryOnOom` @ `0xf8141a0` — ≤ 2 attempts; evict programs + defragment between tries |
| **Immovability bridge** | the `pinned` `absl::flat_hash_set<long>` — MSA-static and DMA/aliasing-held addresses never move |
| **Relocation in `Allocate`/`Deallocate`?** | **NO** — those paths only coalesce free blocks (verified absent) |
| **Algorithm** | one-pass, reverse (high→low) greedy bin-pack against a `gtl::IntervalSet<long>` occupancy oracle |
| **Source label** | `learning/45eac/tpu/runtime/hal/internal/best_fit_allocator.cc` (from `ResourceExhaustedErrorBuilder` site-strings) |
| **Confidence** | CONFIRMED (byte-anchored, full `Compact` body decompiled) unless a row or callout says otherwise |

---

## Scope and Boundaries

This page owns the **present/absent compaction inventory**, the **`Compact` relocation method**, and the **coalescing-vs-compaction distinction**. Three adjacent concerns live on their own pages; this page links them rather than duplicating:

| Concern | Owner page |
|---|---|
| The non-moving free-list: best-fit search, the dual data structure, **coalescing on free**, split policy, alignment quantum | [hbm-allocator.md](hbm-allocator.md) |
| Compile-time HBM/VMEM placement (the static layout that obviates most runtime compaction) | [../compiler/msa-overview.md](../compiler/msa-overview.md) |
| Donation/aliasing as a *reuse* path (a donated input buffer becomes the output, avoiding an allocation entirely) | [buffer-donation-aliasing.md](buffer-donation-aliasing.md) |

> **NOTE — the coalescing/compaction split is the whole point.** *Coalescing* (on [hbm-allocator.md](hbm-allocator.md)) is in-place: on every `Deallocate`, the freed block is merged with its adjacent free neighbours by extending one map entry and erasing the other — **no bytes move, no live buffer is touched**. *Compaction* (this page) is the only path that moves a live buffer's bytes to a new HBM offset. They are complementary: coalescing keeps free space maximally contiguous cheaply; compaction is the heavyweight last resort when coalesced contiguity is still insufficient.

---

## What Is Present vs. Absent

The headline inventory, each line byte-verified in the decompile (see [Evidence](#evidence-table)):

| Mechanism | Present? | Evidence |
|---|---|---|
| Live-buffer **relocation** (`Compact`) | **PRESENT** | `BestFitAllocator::Compact` @ `0x1e81c360` emits `vector<TpuMemmoves::Memmove>` and rewrites the block map + free tree |
| **Relocation plan** decoupled from byte movement | **PRESENT** | `Compact` returns the move list; bytes moved later by `Generate` codegen |
| **OOM-triggered** defrag-and-retry | **PRESENT** | `System::CompactMemory` `0x1d0b6000`, `TpuClient::DefragmentMemory` `0xf7fd660`, `ShouldRetryOnOom` `0xf8141a0` |
| **Pinned set** keeping static/aliased buffers immovable | **PRESENT** | `Compact` takes `flat_hash_set<long> pinned`; CRC32 SwissTable probe per block |
| Program-stack **shrink** before compaction | **PRESENT** | `System::CompactMemory` calls `TpuProgramStack::MaybeShrink` `0x1db0c100` |
| Program **eviction** to free HBM between retries | **PRESENT** | `EvictLoadedPrograms` `0xf80d2c0`, `UnloadAllProgramsForCore` (in retry lambda) |
| Donation-driven **reuse** (allocation avoidance) | **PRESENT** | `AllocateOutputBuffersWithInputReuse` `0xf7ba9a0` (see [buffer-donation-aliasing.md](buffer-donation-aliasing.md)) |
| Relocation inside `Allocate` | **ABSENT** | `Allocate` `0x1e817820` has no `Compact`/`Memmove` reference; only `SplitBlock` |
| Relocation inside `Deallocate` (free-time defrag) | **ABSENT** | `Deallocate` `0x1e819dc0` only calls `MergeBlock`/free-tree ops — coalescing, not moving |
| Periodic / background defragmenter | **ABSENT** | no timer/thread driving `Compact`; sole driver is the OOM retry path |
| Multi-pass fixpoint compaction within one `Compact` call | **ABSENT** | single reverse sweep; a block failing `IsDisjoint` is left in place |
| Device-side arena/slab compaction | **ABSENT** | one fixed-region `BestFitAllocator` per (core, tier); no arenas — see [hbm-allocator.md](hbm-allocator.md) |
| Growth-by-doubling on the device | **ABSENT** | region is a fixed `[base, end)`; `Expand`/`Shrink` only adjust stack bounds |

> **WARNING — this is not the negative result one might expect.** A naive read of "`BestFitAllocator` is a non-moving boundary-tag allocator" would conclude libtpu cannot defragment. That read is wrong. The *allocator's free-list methods* never relocate, but the allocator class also exposes a separate `Compact` method that does — and the runtime calls it on OOM. The honest summary is: **non-moving for the common case, moving-as-last-resort on exhaustion.**

---

## The Relocation Method: `Compact`

`BestFitAllocator::Compact` (`0x1e81c360`, vt+0x90) is the only method in the allocator that produces buffer relocations. Its signature (demangled from the binary symbol) is:

```cpp
// 0x1e81c360 — returns the relocation plan by value (NRVO into a1).
std::vector<tpu::TpuMemmoves::Memmove>
tpu::BestFitAllocator::Compact(const absl::flat_hash_set<long>& pinned);
```

It is a **one-pass, reverse (high-address-to-low) greedy bin-pack** with a pinned exclusion set. The decompiled body (`~3.9 kB`) confirms five phases:

```c
// Reconstructed from the 0x1e81c360 decompile (byte-confirmed structure).
std::vector<TpuMemmoves::Memmove>
Compact(const flat_hash_set<int64_t>& pinned) {
    std::vector<TpuMemmoves::Memmove> moves;          // RETURNED plan (begins empty)

    // (1) COLLECT every allocated block from the boundary-tag map.
    std::vector<LiveBlock> live;                      // LiveBlock = {begin,end,state} (48 B)
    for (HashTableEntry& e : blocks_by_offset_)       // walk SwissTable slots
        if (e.state != kFree)                         // *((_DWORD*)slot+2) != 0
            live.push_back({e.offset, e.offset + e.size, e.state});

    // (2) SORT the live blocks (introsort, by offset).
    __introsort(live.begin(), live.end());            // 0x1e81e260

    // (3) REVERSE SWEEP: pack movable blocks against the top, against an occupancy oracle.
    gtl::IntervalSet<int64_t> occupied;               // absl btree of intervals
    int64_t top = allocatable_range_end_;             // this+0x70
    std::vector<LiveBlock> kept;
    for (LiveBlock& b : reverse(live)) {              // high -> low
        if (b.state == kReserved) { kept.push_back(b); continue; }   // reserved -> immovable
        int64_t addr = b.begin + base_offset_in_bytes_;              // this+0x58
        if (pinned.contains(addr)) {                  // CRC32 H2 SwissTable probe
            kept.push_back(b);
            occupied.Add({b.begin, b.end});           // 0x1e824ae0 (AddImpl)
            continue;                                 // PINNED -> never relocated
        }
        int64_t size = b.end - b.begin;
        int64_t cand_lo = top - size, cand_hi = top;  // candidate placement against the top
        if (occupied.IsDisjoint({cand_lo, cand_hi})) { // 0x1cc99740 — placement is free
            moves.push_back(Memmove{ b.begin  / granule_,  // +0 src, +8 dst, +0x10 size (GRANULE units)
                                     cand_lo  / granule_,
                                     size     / granule_ });
            kept.push_back({cand_lo, cand_hi, b.state});
            occupied.Add({cand_lo, cand_hi});
            top = cand_lo;
        } else {
            kept.push_back(b);                        // overlap -> leave in place (single pass)
            occupied.Add({b.begin, b.end});
        }
    }

    // (4) REWRITE bookkeeping to the post-compaction layout, atomically in-call.
    ClearBackingArray(blocks_by_offset_);             // wipe the SwissTable
    free_tree_.clear();                               // __tree_deleter over the RB-tree
    __introsort(kept.begin(), kept.end());
    int64_t cursor = 0;
    for (LiveBlock& b : kept) {
        if (b.begin > cursor) {                       // gap below -> synthesize a free block
            blocks_by_offset_.try_emplace(cursor, {cursor, kFree, b.begin - cursor});
            free_tree_.insert(FreeBlock{cursor, b.begin});   // find_equal + balance_after_insert
        }
        blocks_by_offset_.try_emplace(b.begin, {b.begin, b.state, b.end - b.begin});
        cursor = b.end;
    }
    if (cursor < allocatable_range_end_) {            // trailing free block
        blocks_by_offset_.try_emplace(cursor, {cursor, kFree, allocatable_range_end_ - cursor});
        free_tree_.insert(FreeBlock{cursor, allocatable_range_end_});
    }

    // (5) RECOMPUTE the derived watermarks via a GetBlockIf probe at the reserved offset.
    capacity_in_bytes_ = ...;                         // this+0xB8 updated from GetBlockIf result

    return moves;                                     // caller turns the plan into DMA programs
}
```

### Why the plan is returned, not executed

`Compact` does **not** move a single byte. It returns a `std::vector<TpuMemmoves::Memmove>` — a list of `{dst, src, size}` triples, **all three fields in granule units** (the byte values divided by `granule_in_bytes_`, `this+0x78`). The byte movement is performed by a separate pipeline (next section). This decoupling matters: the allocator's bookkeeping is updated synchronously inside `Compact` (so live `tpu::TpuBuffer` handles resolve to the new offsets *immediately*, because they resolve their device address *through* the allocator's current map), while the physical DMA is enqueued asynchronously on the device.

```c
struct tpu::TpuMemmoves::Memmove {     // 24 B, GRANULE units
    int64_t src;     // +0   source offset / granule  (the block's current begin)
    int64_t dst;     // +8   destination offset / granule  (the new top placement)
    int64_t size;    // +0x10  size / granule
};
struct Compact::LiveBlock {            // 48 B local helper
    int64_t begin;   // +0
    int64_t end;     // +8
    int32_t state;   // +0x10
    // (trailing bytes used as a small-string/scratch slot in the decompile)
};
```

### The pinned set is the MSA / aliasing immovability bridge

The single argument to `Compact` is `const absl::flat_hash_set<long>& pinned` — buffer **addresses** that must not move. In the decompiled reverse sweep, each candidate block's absolute address (`b.begin + base_offset_in_bytes_`) is looked up in this set via a CRC32-keyed Abseil SwissTable probe (`_mm_crc32_u64` + `vpcmpeqb` group scan, lines confirming the H2 metadata match). A hit means the block is appended to the kept list and its interval marked occupied — it is **never** assigned a `Memmove`.

This is the precise mechanism that enforces *"MSA owns the static layout; the runtime owns the dynamic layout in the remaining space"*:

- **MSA-static buffers** (placed at compile time, replayed at load by `ProgramMemoryAllocator::CreateFromProto` — see [../compiler/msa-overview.md](../compiler/msa-overview.md)) are passed in `pinned` and stay put.
- **Buffers with an outstanding DMA or aliasing hold** (donated inputs reused as outputs, buffers mid-transfer — see [buffer-donation-aliasing.md](buffer-donation-aliasing.md)) are also pinned, because relocating bytes underneath an in-flight DMA would corrupt them.
- Only the remaining **dynamic, movable** PJRT buffers receive `Memmove` records.

> **NOTE — `kReserved` is a second, state-level immovability gate.** Independently of the `pinned` set, any block whose `state == kReserved` (e.g. the bottom-of-memory reservation set by `ReserveBottomOfMemory`, see [hbm-allocator.md](hbm-allocator.md#the-reserved-bottom-watermark)) is skipped by the sweep entirely. So a buffer is immovable if it is either reserved-state *or* address-pinned.

### The reverse-sweep packing, in words

Allocations are placed at the **top** of free blocks by `Allocate` (top-down placement, see [hbm-allocator.md](hbm-allocator.md#top-down-placement)), so live buffers naturally cluster against the high end. `Compact` mirrors that: it walks live blocks from high address to low, and for each movable block tries to place it as high as possible (`top - size`) against the running `IntervalSet` of already-placed/pinned intervals. If that candidate window is disjoint from everything occupied, the block relocates there and `top` drops; otherwise the block is left where it is. The net effect is that movable buffers slide downward to fill gaps left by pinned buffers, consolidating free space into a single contiguous run at the bottom — which is exactly the run a subsequent best-fit `Allocate` can then satisfy.

> **GOTCHA — single pass, not a fixpoint.** Each `Compact` call is one reverse sweep. A block that fails the `IsDisjoint` test (because a pinned block sits in its candidate window) is left in place and **not** retried within the same call. Multi-pass behaviour, if it occurs, comes from the OOM retry loop invoking the whole defrag chain again (bounded at 2 attempts). This was observed but not exhaustively traced — marked HIGH.

---

## Byte Movement: From Plan to On-Device DMA

`Compact` produces the plan; three downstream functions turn it into executed DMA:

1. **`TpuSharedMemoryCommonImpl::GenerateAndValidateCompactionPrograms(const TpuMemmoves&)`** (`0x1d130ec0`) validates the move set (its `CHECK` strings confirm `compaction_buffer_base_ != nullptr`, `compaction_codegen() != nullptr`, and `memmoves.type() == compaction_buffer().location().type()`) and drives codegen.
2. **`TpuCompactionIsaEmitterCodegen::Generate(TpuSharedMemoryType, Span<TpuMemmoves::Memmove const>)`** (`0x1090ece0`) codegens the actual DMA. The decompile shows it reading `Target::VmemSizeBytes` / `Target::VmemWordSizeBytes` and building a `vector<Transaction>` — i.e. it stages moves **through VMEM** (HBM→VMEM→HBM) in chunks bounded by VMEM capacity, merging adjacent moves into batched transactions. This is why compaction is genuinely a *physical* relocation, not a remapping trick: there is no HBM address-translation layer to retarget, so the bytes are copied.
3. **`TpuSharedMemoryDriverCommonImpl::EnqueueCompactionImpl(const TpuMemmoves&, AnyInvocable<void(Status const&)>)`** (`0x1d12ed00`) constructs a `CompactionRunner` (`CompactionRunner::Create`) and enqueues the generated programs on the core's command stream, invoking the completion callback when the DMA finishes.

The stream-executor surface exposes the same capability via `TpuExecutor::EnqueueCompactionOnStreamForHbm` (`0xe997400` / interface `0x1d0eff00`), and `TpuNodeContext::CompactionSupported` (`0xeaca440`) gates whether a given chip supports it at all.

```text
BestFitAllocator::Compact(pinned)                 // build the plan + rewrite map/tree
        │  std::vector<TpuMemmoves::Memmove>
        ▼
TpuSharedMemory::EnqueueCompaction (0x1d4bcde0)    // per-core entry
        ▼
GenerateAndValidateCompactionPrograms (0x1d130ec0) // validate the move set
        ▼
TpuCompactionIsaEmitterCodegen::Generate (0x1090ece0)  // codegen VMEM-staged DMA transactions
        ▼
EnqueueCompactionImpl (0x1d12ed00) -> CompactionRunner // enqueue on the device command stream
```

| Function | Address | Role |
|---|---|---|
| `BestFitAllocator::Compact` | `0x1e81c360` | collect → sort → IntervalSet pack → emit `TpuMemmoves` → rewrite map/tree |
| `TpuSharedMemory::EnqueueCompaction` | `0x1d4bcde0` | per-core compaction entry (`TpuCompactionConfig` + callback) |
| `GenerateAndValidateCompactionPrograms` | `0x1d130ec0` | validate the move set, drive codegen |
| `TpuCompactionIsaEmitterCodegen::Generate` | `0x1090ece0` | codegen merged VMEM↔HBM staged DMA transactions |
| `EnqueueCompactionImpl` | `0x1d12ed00` | build `CompactionRunner`, enqueue on the command stream |
| `gtl::IntervalSet<long>::AddImpl` | `0x1e824ae0` | mark an interval occupied during the pack |
| `gtl::IntervalSet<long>::IsDisjoint` | `0x1cc99740` | test a candidate placement window |
| `__introsort` (Compact `LiveBlock`) | `0x1e81e260` | sort live blocks by offset |
| `TpuExecutor::EnqueueCompactionOnStreamForHbm` | `0xe997400` | stream-executor compaction surface |
| `TpuNodeContext::CompactionSupported` | `0xeaca440` | per-chip capability gate |

---

## What Triggers Compaction: the OOM Retry Chain

Nothing runs `Compact` proactively. The sole driver is an allocation failure. When `BestFitAllocator::Allocate` (`0x1e817820`) cannot find a fitting free block, it returns the `ResourceExhausted` diagnostic *"Attempting to allocate \<size\>. That was not possible. There are \<free\> free. The largest contiguous region of free memory is \<largest\> due to fragmentation."* (site string at `best_fit_allocator.cc:129`). (The superficially similar "…at the bottom of memory…" wording is a *different* string emitted by `ReserveBottomOfMemory` @ `0x1e81b0c0`, not by this `Allocate` OOM path — see [hbm-allocator.md](hbm-allocator.md).) That error propagates up the four-layer allocator stack and lands in the retry chain:

```c
// tpu::System::CompactMemory (0x1d0b6000): the device-side defrag driver.
TpuEvent CompactMemory(const TpuSharedMemoryLocation& loc) {
    auto* core = ResolveCore(loc);
    if (!core)                                        // best_fit_allocator path needs a core
        return MakeError<ResourceExhausted>(          // MakeErrorImpl<13>, system.cc:2410
            "No attached TPU to compact.");
    int seg = TpuSharedMemoryTypeToTpuSegmentMemoryType(loc.type());  // 0..2
    for (TpuProgramStack* stack : core->program_stacks())
        stack->MaybeShrink(stack->segment_limit(seg));  // 0x1db0c100 — reclaim dynamic-stack HBM first
    return shm(loc)->EnqueueCompaction(/* -> BestFitAllocator::Compact(pinned) */);
}

// xla::TpuClient::ShouldRetryOnOom (0xf8141a0): the bound + recovery actions.
bool ShouldRetryOnOom(int attempt, PjRtDevice* dev, PjRtLoadedExecutable* exe, Status s) {
    if (attempt > 1) return attempt < 2;              // <= 2 total attempts
    // tpu_pjrt_client.cc:4441 — verbatim LOG prefix
    LOG(INFO) << "TpuLoadedExecutable::ExecutePrepareWithOomRetries "
                 "attempting to defragment and retry after seeing error: " << s;
    if (this->evict_programs_on_oom_ /*+0x67*/)
        for (auto& [id, ls] : loaded_executables_)
            if (ls != exe) ls->EvictLoadedPrograms(); // 0xf80d2c0 — free program HBM
    if (this->defragment_on_oom_ /*+0x69*/)
        DefragmentMemory(dev->core()->LocalSharedMemory(kHbm));  // 0xf7fd660 -> CompactMemory
    return attempt < 2;
}
```

The async leaf, `tfrt::tpu::AllocateTpuBufferWithRetry` (`0xf7ec6a0`), is dependency-gated rather than spin-looping: if the System `AsyncValueRef` is still pending it enqueues a waiter carrying a retry continuation (`$_0` @ `0xf7ed620`) that also calls `TpuCompilationCache::UnloadAllProgramsForCore` to free program HBM, then re-runs `System::Allocate` when the dependency resolves. So the recovery sequence on OOM is, in order: **(a)** shrink dynamic program stacks; **(b)** evict/unload loaded programs to reclaim their static HBM; **(c)** run `Compact` to relocate movable buffers and consolidate the remainder; **(d)** retry the allocation — at most twice.

> **NOTE — two config bools gate recovery.** `TpuClient+0x67` toggles program eviction and `TpuClient+0x69` toggles defragmentation on OOM; either can be disabled independently. Their exact `PjRtTpuClientConfig` key names were not back-traced (marked in the open items). The megascale-aware variant `CommonPjRtClient::ShouldRetryOnOom` (`0xe6edc80`) additionally consults the `DeviceAssignment` so a pod slice can coordinate retry across devices.

| Function | Address | Role |
|---|---|---|
| `BestFitAllocator::Allocate` (OOM site) | `0x1e817820` | emits the `ResourceExhausted` "…due to fragmentation" leaf error |
| `tpu::System::CompactMemory` | `0x1d0b6000` | shrink program stacks → `EnqueueCompaction`; `MakeErrorImpl<13>` on bad core |
| `TpuProgramStack::MaybeShrink` | `0x1db0c100` | reclaim dynamic-stack HBM before compacting |
| `TpuClient::DefragmentMemory` | `0xf7fd660` | PJRT defrag entry → `EnqueueDefragmentMemory` → `System::CompactMemory` |
| `TpuClient::EnqueueDefragmentMemory` | `0xf7fd180` | enqueue the defrag work item |
| `TpuClient::ShouldRetryOnOom` | `0xf8141a0` | ≤ 2 attempts; evict programs + defragment |
| `CommonPjRtClient::ShouldRetryOnOom` | `0xe6edc80` | megascale/pod-aware retry coordination |
| `tfrt::tpu::AllocateTpuBufferWithRetry` | `0xf7ec6a0` | async dependency-gated retry; `UnloadAllProgramsForCore` |
| `TpuExecutableLoadState::EvictLoadedPrograms` | `0xf80d2c0` | free program HBM between retries |

---

## Why Runtime Compaction Is Rarely Needed: the MSA Story

The reason a TPU program can run a 745 MB-binary's worth of kernels and almost never hit `Compact` is that the hard packing problem is solved **before** the program runs. XLA's MemorySpaceAssignment (`MsaAlgorithm`) plus jellyfish `ProgramMemoryAllocator` assign every static intra-program buffer a memory space and a frozen byte offset at *compile time*, serialized into `ProgramMemoryMetadata` inside the compiled program. At load time, `ProgramMemoryAllocator::CreateFromProto` (`0x1c631f20`) replays those offsets, and the runtime `BestFitAllocator` is *told where* each static buffer goes — it does not run a best-fit search for them, and it never needs to relocate them (they are pinned). See [../compiler/msa-overview.md](../compiler/msa-overview.md).

This is the key architectural insight for a reimplementer: **compile-time packing is the primary anti-fragmentation strategy; runtime compaction is the safety net.** The runtime free-list math (and any compaction) is exercised only against the *dynamic* surface in the HBM left over after MSA's static layout — user input/output PJRT buffers, transfer staging, async-copy scratch, the dynamic program stack. Because those are typically large, few, and short-lived relative to the static program footprint, the runtime mostly gets by on best-fit + coalescing alone, and `Compact` fires only under genuine pressure.

> **NOTE — donation is the other allocation-avoidance mechanism.** Before any dynamic allocation, `AllocateOutputBuffersWithInputReuse` (`0xf7ba9a0`) consults the compiler-emitted `HloInputOutputAliasConfig` and, for aliased outputs, *reuses the donated input buffer in place* — no new HBM, no relocation. This is reuse, not compaction, but it has the same effect of reducing the dynamic allocation pressure that would otherwise drive fragmentation. See [buffer-donation-aliasing.md](buffer-donation-aliasing.md).

---

## Coalescing vs. Compaction: the Precise Distinction

To close the inventory, the two free-space-recovery mechanisms side by side. This is the central reason these are two pages, not one.

| Property | Coalescing ([hbm-allocator.md](hbm-allocator.md)) | Compaction (this page) |
|---|---|---|
| Method | inside `Deallocate` `0x1e819dc0` (via `MergeBlock` `0x1e819700`) | `Compact` `0x1e81c360` |
| When | **every free**, eager, immediate | **only on OOM**, after coalescing already failed to help |
| Moves live bytes? | **No** — merges adjacent *free* blocks only | **Yes** — relocates movable *live* buffers via DMA |
| Mechanism | extend one map entry, erase the neighbour entry | emit `TpuMemmoves` plan → VMEM-staged DMA |
| Touches a live `TpuBuffer`? | never | yes (movable, unpinned ones) |
| Cost | O(1) amortised neighbour lookup + O(log n) tree edit | O(n log n) collect+sort + interval-set pack + DMA + full map/tree rebuild |
| Recovers | adjacency-induced free fragments | *non-adjacent* free fragments (external fragmentation) |
| Failure mode it addresses | small free gaps next to a freed block | "largest contiguous region … due to fragmentation" OOM |

Coalescing guarantees the free tree never holds two physically adjacent free blocks (see [hbm-allocator.md](hbm-allocator.md#coalescing-on-free)); that keeps best-fit honest and recovers all *adjacency*-recoverable space for free. What coalescing **cannot** do is merge free space separated by a live buffer — that is precisely the gap compaction closes, by moving the intervening live buffer out of the way. The OOM diagnostic's "largest contiguous region of free memory is \<X\> due to fragmentation" is the symptom of exactly this situation: total free bytes are sufficient, but no single contiguous run is, because live buffers sit between the gaps. `Compact` slides the movable ones aside to coalesce those gaps into one run.

---

## Cross-References

- [hbm-allocator.md](hbm-allocator.md) — the non-moving free-list: best-fit search, the dual data structure, **coalescing on free** (the in-place counterpart to this page's relocation), split policy, alignment quantum
- [../compiler/msa-overview.md](../compiler/msa-overview.md) — the compile-time placement pass that freezes static offsets and is the primary reason runtime compaction is rarely needed
- [buffer-donation-aliasing.md](buffer-donation-aliasing.md) — donation/aliasing as a zero-copy *reuse* path, and the source of the DMA/aliasing holds that make a buffer pinned during compaction
- [tpu-buffer-layout.md](tpu-buffer-layout.md) — how a device buffer's HBM offset maps to its on-device tile layout (the bytes that `Compact` actually relocates)
- [overview.md](overview.md) — the five on-chip memory tiers and where the HBM allocator sits in the map
- [hbm-dma-alignment.md](hbm-dma-alignment.md) — the 1024 B DMA quantum that keeps every relocated offset DMA-legal
- [vmem-allocator.md](vmem-allocator.md), [cmem-pool.md](cmem-pool.md) — sibling tiers using the same `BestFitAllocator` class (and therefore the same `Compact`)
- [back to index](../index.md)
