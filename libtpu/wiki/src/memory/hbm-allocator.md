# HBM BestFit Allocator

> *All addresses, struct offsets, and magic constants on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`). Other versions will differ.*

## Abstract

`tpu::BestFitAllocator` is the single concrete `tpu::MemoryAllocator` subclass that services *every* runtime memory tier on a TPU core — HBM, VMEM, SMEM, CMEM, SFLAG — over a fixed `[base_offset, allocatable_range_end)` byte span established at boot from a `MemoryAllocator::Config` triple. This page documents the **runtime / driver-side** HBM allocator: the dual free-list data structure, the best-fit search, the eager bidirectional coalescing on free, the split / fragmentation policy, and the alignment quantum. It is *not* the compiler-side placement pass — that is XLA's [MSA](../compiler/msa-overview.md), which freezes static offsets into the program and merely hands them to this allocator to replay. The free-list arithmetic on this page runs only for the **dynamic** surface (user input/output buffers, transfer staging, async-copy scratch, the dynamic program stack); MSA-fixed static offsets are replayed, never re-derived.

There is no class literally named `HbmAllocator`. The HBM tier is one instance of `BestFitAllocator`; the same class, byte-for-byte, also backs VMEM and the other on-chip tiers — the only difference per tier (and per chip generation) is the `Config`. The allocator is structurally interesting because it is **not** bin-bucketed like `tsl::BFCAllocator` (which libtpu uses only host-side, see [embedded-tcmalloc.md](embedded-tcmalloc.md)). It is a classic **boundary-tag scheme**: an offset-keyed SwissTable holds every block (free *and* allocated) for O(1) neighbour lookup, layered with a size-ordered red-black tree of free blocks for an exact best-fit search.

A reader who knows `dlmalloc` will recognise the boundary tags and the eager coalesce; what is different here is that there are no size bins, the free tree gives true best-fit (smallest-fitting block) rather than a segregated approximation, allocations are placed at the **top** of the chosen free block, and the region never grows — capacity is fixed at construction.

For reimplementation, the contract is:

- **The dual structure.** `blocks_by_offset_` (Abseil `flat_hash_map<offset, HashTableEntry>`) is a boundary-tag map of *all* blocks; `free_tree_` (`std::set<FreeBlock, BlockOrder>`) holds *only* free blocks, ordered `(size, then begin)` ascending.
- **The best-fit search.** `FindAllocatableBlock` does `lower_bound({end − aligned, end})` on the size-ordered tree — the first node returned is the smallest block whose size ≥ request — with a reserved-bottom skip. A per-instance `Policy` switch selects an O(n) first-fit walk instead.
- **The coalescing.** Eager, immediate, bidirectional on every `Deallocate`. Next neighbour = `map.find(self.end)`; prev neighbour = free-tree `find_equal` keyed on `self.begin`. `MergeBlock` extends the lower entry and erases the upper.
- **The split / fragmentation policy.** Allocation placed top-down; `SplitBlock` has **no** minimum-remainder threshold — any non-zero leftover becomes a free block, an exact fit produces none. Internal fragmentation (round-up padding) is owned by the allocation, not a side ledger. External fragmentation is reported in the OOM diagnostic and reclaimed by [compaction](on-device-compaction.md).
- **The alignment quantum.** Requests round up to `alignment_in_bytes_` once at the request boundary; the region end is rounded down at construction, so every offset, size, and remainder is an alignment multiple. HBM's quantum is the 1024 B DMA minimum (see [hbm-dma-alignment.md](hbm-dma-alignment.md)).

| | |
|---|---|
| **Class** | `tpu::BestFitAllocator` : `tpu::MemoryAllocator` (200-byte instance, `operator new(0xC8)`) |
| **Constructor** | `0x1e817500` (`Config const&`, `Policy`); typeinfo `0x21d346e8`, vtable `0x21d34630` |
| **Allocate** | `0x1e817820` (vt+0x30) → `StatusOr<int64_t>` (returns `base + offset`) |
| **Deallocate** | `0x1e819dc0` (vt+0x38) |
| **Best-fit search** | `FindAllocatableBlock` @ `0x1e818540` |
| **Split** | `SplitBlock` @ `0x1e819060`; **no min-remainder** |
| **Coalesce** | `MergeBlock` @ `0x1e819700`; comparator impls `__lower_upper_bound` `0x1e824960` / `__find_equal` `0x1e824640` |
| **Defrag** | `Compact` @ `0x1e81c360` — owned by [on-device-compaction.md](on-device-compaction.md) |
| **Free list** | size-ordered RB-tree `std::set<FreeBlock, BlockOrder>` + offset-keyed SwissTable `blocks_by_offset_` |
| **HBM alignment quantum** | 1024 B (`jf_driver::kHbmMinimumDmaAlignment`) — per-tier via `Config`; see [hbm-dma-alignment.md](hbm-dma-alignment.md) |
| **Source label** | `learning/45eac/tpu/runtime/hal/internal/best_fit_allocator.cc` (from `LogMessageFatal` / error site-strings) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## Scope and Boundaries

This page is the runtime/driver HBM allocator. Three closely-related concerns live on their own pages; do not duplicate them here:

| Concern | Owner page |
|---|---|
| Compile-time HBM/VMEM placement (the `kDefault`/`kAlternate` decision, frozen offsets) | [../compiler/msa-overview.md](../compiler/msa-overview.md) |
| The 1024 B HBM DMA-issue alignment check and the 16 KiB compile-time program alignment | [hbm-dma-alignment.md](hbm-dma-alignment.md) |
| The OOM-triggered `Compact` defrag plan, `TpuMemmoves`, the DMA-program codegen, the OOM retry chain | [on-device-compaction.md](on-device-compaction.md) |
| The five-tier memory map and host-side allocators | [overview.md](overview.md), [embedded-tcmalloc.md](embedded-tcmalloc.md) |

What this page owns: the **free-list structure**, the **best-fit search**, the **coalescing**, the **split / fragmentation policy**, and the **alignment quantum** — i.e. the in-place free-list math of one `BestFitAllocator` instance.

> **NOTE —** the same `BestFitAllocator` class backs VMEM, SMEM, CMEM, and SFLAG. The algorithm is identical across tiers and across chip generations (Dragonfish through 6acc60406); there are no `switch (TpuVersion)` branches inside any allocator method. Per-tier and per-generation variation is carried entirely in the `Config` triple, sourced from the embedded `chip_parts.binarypb` resource at boot. See [vmem-allocator.md](vmem-allocator.md) and [cmem-pool.md](cmem-pool.md) for the sibling tiers.

---

## The Two Coupled Data Structures

The allocator keeps two structures, not one free list. This is the central design fact.

- **`blocks_by_offset_`** — Abseil `flat_hash_map<int64_t, HashTableEntry, OffsetHash>` at `this+0x08`. A **boundary-tag map** keyed by block offset that stores *every* block, free and allocated, with `{offset, state, size, metadata}`. This is what makes neighbour lookup O(1): the block physically above `self` is exactly `map.find(self.end)` (its offset equals `self`'s end).
- **`free_tree_`** — `std::set<FreeBlock, BlockOrder>` red-black tree rooted at `this+0x28`, holding *only* free blocks, ordered by `(size, then begin-offset)` ascending. `lower_bound` on this tree gives the smallest-fitting block — exact best-fit with a lowest-address tie-break.

It is therefore an **address-indexed boundary-tag scheme for coalescing** layered with a **size-ordered tree for the best-fit search**. This is *not* the size-bucketed bin scheme of `tsl::BFCAllocator` (host-only).

```c
// 200-byte instance (operator new(0xC8)); ctor 0x1e817500 sets these fields. Offsets byte-confirmed.
struct tpu::MemoryAllocator::Config {            // 32 B, passed by const&
    int64_t base_offset_in_bytes_;     // +0   >= 0  (validated; else fatal)
    int64_t allocatable_range_end_;    // +8   > 0   (capacity = end - base)
    int64_t alignment_in_bytes_;       // +16  > 0, power of two, divides granule
    int64_t granule_in_bytes_;         // +24  hardware granule (page/word)
};

enum class Policy     : int32_t { kBestFit = 0, kFirstFit = 1 };
enum class BlockState : int32_t { kFree = 0, kAllocated = 1, kReserved = 2 };

struct tpu::BestFitAllocator {                    // sizeof = 0xC8 (200)
    void*    vptr;                     // +0x00  -> vtable+0x10
    flat_hash_map<int64_t,HashTableEntry,OffsetHash>
             blocks_by_offset_;        // +0x08  boundary-tag map (ALL blocks)
    set<FreeBlock,BlockOrder>
             free_tree_;               // +0x28  RB-tree (FREE blocks only)
                                       //   size@+0x38, end_node@+0x48
    Policy   policy_a_;                // +0x40  copy 1
    /* free_tree_end_ */               // +0x48
    Policy   policy_b_;                // +0x50  copy 2 (read by Find/Split/Dealloc)
    int64_t  base_offset_in_bytes_;    // +0x58
    int64_t  alignment_in_bytes_;      // +0x60
    int64_t  max_aligned_size_;        // +0x68  INT64_MAX - (INT64_MAX % align)
    int64_t  allocatable_range_end_;   // +0x70  end - (end % align)
    int64_t  granule_in_bytes_;        // +0x78
    int64_t  bytes_in_use_;            // +0x80  } packed XMM pair, updated
    int64_t  num_allocations_;         // +0x88  } with vpaddq / vpsubq
    int64_t  peak_bytes_in_use_;       // +0x90
    int64_t  peak_block_bytes_in_use_; // +0x98
    int64_t  bytes_allocatable_init_;  // +0xA0  = end (init mirror)
    int64_t  reserved_bottom_limit_;   // +0xA8  ReserveBottomOfMemory current limit
    int64_t  peak_reserved_bottom_;    // +0xB0  max() watermark of reserved-bottom limit
    int64_t  capacity_in_bytes_;       // +0xB8  = end
    int64_t  reservation_count_;       // +0xC0
};

struct FreeBlock { int64_t lo_offset; int64_t hi_offset; };   // 16 B RB-tree value
// BlockOrder(a,b): a < b  <=>  (a.size < b.size) ||
//                              (a.size == b.size && a.lo < b.lo),
//                 where size := hi_offset - lo_offset.   (size-major, offset-minor)

struct HashTableEntry {                // 32 B SwissTable value
    int64_t offset;     // +0  = map key
    int32_t state;      // +8  kFree / kAllocated / kReserved
    int32_t _pad;       // +0xC
    int64_t size;       // +0x10  == aligned size (padding owned by allocation)
    int64_t metadata;   // +0x18  allocation id / owner
};

struct Block {                         // 48 B value materialised by GetBlockIf
    int64_t offset;     // +0
    int64_t end;        // +8   = offset + size
    int32_t state;      // +0x10
    int64_t metadata;   // +0x18
    void*   ctrl_ptr;   // +0x20  (Abseil ctrl byte, or nullptr if not found)
    void*   slot_ptr;   // +0x28  (HashTableEntry*)
};
```

**Initial state** (ctor): one `HashTableEntry {offset = 0, state = kFree, size = end}` and one `FreeBlock {0, end}` covering the whole region. The ctor seeds the tree with an `operator new(0x30)` node and a `__find_equal`-then-insert, then validates the `Config` (see [Alignment](#the-alignment-quantum)).

### Why two structures and not one

A single offset-ordered tree would give O(log n) neighbour lookup but a *linear* best-fit search (you would have to scan for the smallest fitting gap). A single size-ordered tree would give O(log n) best-fit but *no* way to find the physical neighbour of a freed block for coalescing. The boundary-tag map + size tree pairing gives **O(log n) best-fit and O(1) amortised neighbour lookup** simultaneously. The cost is keeping the two in sync on every split and merge, which is exactly what `SplitBlock`/`MergeBlock`/`ShrinkFreeListEntry`/`AddToFreeList` do.

| Field / structure | Address or offset | Role |
|---|---|---|
| `blocks_by_offset_` | `this+0x08` | boundary-tag map of all blocks |
| `free_tree_` | `this+0x28` (size `+0x38`, end `+0x48`) | size-ordered free RB-tree |
| `policy_b_` | `this+0x50` | read by `Find`/`Split`/`Dealloc` to pick best-fit vs first-fit |
| `reserved_bottom_limit_` | `this+0xA8` | bottom watermark; skipped by the search |
| `BlockOrder` `lower_bound` | `0x1e824960` | comparator `(size, then lo)` ascending |
| `BlockOrder` `find_equal` | `0x1e824640` | same comparator; prev/next coalesce lookups |

---

## The Best-Fit Search

`FindAllocatableBlock` (`0x1e818540`) is the placement search. The decompile confirms two branches gated on `policy_b_` (`this+0x50`, read as `*((_DWORD*)this + 20)`):

```c
// FindAllocatableBlock(int64_t aligned) const   (0x1e818540)
Iterator FindAllocatableBlock(int64_t aligned) const {
    if (policy_ != kBestFit) {                       // FIRST-FIT (policy@+0x50 != 0)
        for (node = free_tree_.begin(); node != end(); ++node)  // in-order RB walk
            if (node.hi - node.lo >= aligned)        // node[5]-node[4] >= aligned
                return node;                         // first block that fits
        return end();
    }
    // BEST-FIT: lower_bound on the size-ordered tree.
    int64_t end = allocatable_range_end_;            // this+0x70  (read as this+14)
    FreeBlock key{ end - aligned, end };             // {lo = end-aligned, hi = end}
    auto it = free_tree_.lower_bound(key);           // __lower_upper_bound_unique_impl
    if (it == free_tree_.end()) return end();        // no fit -> caller reports OOM

    // RESERVED-BOTTOM SKIP: if >= 2 free blocks and this candidate starts exactly
    // at the bottom watermark, step past it and rescan forward for the next fit.
    if (free_tree_size_ >= 2 && it->lo == reserved_bottom_limit_) {  // +0x38, +0xA8
        for (++it; it != end(); ++it)
            if (it->hi - it->lo >= aligned) return it;
        // none after the reserved block fits -> fall through to the candidate
    }
    return it;                                       // smallest fitting block
}
```

The key insight is in the `lower_bound` key. The tree is ordered `(size, then lo)`. Keying on `{end − aligned, end}` makes the key's derived size exactly `end − (end − aligned) = aligned`. `lower_bound` returns the first node whose `(size, lo) ≥ (aligned, end − aligned)` — i.e. the first node with `size ≥ aligned`. Because the tree is size-major, that first node is the **smallest block whose size ≥ the request**. This is true best-fit, not a bin approximation. The byte trace confirms `v13[0] = v7 - a2; v13[1] = v7` with `v7 = this[+0x70]` and `a2 = aligned`.

The first-fit branch walks the tree in *offset* order? No — it walks in *in-order* (size-then-offset) order via the std `__tree` iterator (`result[1]` left-child / `result[2]` parent successor logic) and returns the first block large enough. Because the tree is size-ordered, the first node it reaches that fits is, in fact, also the smallest-fitting one — so on this allocator first-fit and best-fit converge on the *same* block, and the only real difference is that first-fit always rescans from the smallest free block (O(n) worst case) while best-fit jumps straight there via `lower_bound` (O(log n)). All five `AllocatorFactoryByName` factory lambdas construct with `kBestFit`, so the first-fit path is dormant on the device in this build.

### The reserved-bottom watermark

`ReserveBottomOfMemory` (`0x1e81b0c0`) sets `reserved_bottom_limit_` (`+0xA8`). The search treats a free block whose `lo` equals that watermark as off-limits *when other free blocks exist* and steps to the next candidate. This is how the runtime carves out a protected region at the bottom of the tier (the XLA flag `xla_tpu_user_reserved_hbm_bytes` flows here at boot). The `free_tree_size_ >= 2` guard ensures that if the reserved block is the *only* free block, it is still returned rather than failing the allocation outright.

| Function | Address | Role |
|---|---|---|
| `FindAllocatableBlock` | `0x1e818540` | best-fit `lower_bound` + reserved-bottom skip; first-fit walk |
| `__lower_upper_bound_unique_impl<FreeBlock>` | `0x1e824960` | the `lower_bound` over the size-ordered tree |
| `ReserveBottomOfMemory` | `0x1e81b0c0` | sets the `+0xA8` watermark |
| `GetBlockIf` | `0x1e818f20` | SwissTable H2 probe → 48-B `Block` (or null-slot Block) |
| `BytesAllocatable` (vt+0x70) / `BytesAvailable` (vt+0x68) | `0x1e819b80` / `0x1e81dd60` | largest-free-run / total-free; supply the OOM diagnostic's $largest$ / $free$ values |

---

## Allocation and the Split / Fragmentation Policy

`Allocate` (`0x1e817820`) validates the size, rounds it up, finds a block, **splits at the top of that block**, shrinks the free entry, updates stats, and returns an absolute address.

```c
// Allocate(int64_t size) -> StatusOr<int64_t>   (0x1e817820), byte-confirmed
StatusOr<int64_t> Allocate(int64_t size) {
    if (size < 0 || size > max_aligned_size_)              // +0x68
        return BadSizeError(size);                         // 0x1e818240

    // round up to alignment (power of two). The (size!=0) term keeps 0 -> 0.
    int64_t aligned = (size + alignment_ - (size != 0)) & -alignment_;   // +0x60

    auto it = FindAllocatableBlock(aligned);               // 0x1e818540
    if (it == free_tree_.end())                            // == this+0x48
        // message streamed (not a Substitute template), pieces verbatim:
        //   "Attempting to allocate " << HR(size)
        //   << ". That was not possible. There are " << HR(BytesAvailable())  // vt+0x68
        //   << " free. The largest contiguous region of free memory is "
        //   << HR(BytesAllocatable())                                         // vt+0x70
        //   << " due to fragmentation.";
        // ResourceExhaustedErrorBuilder, line 129; if FLAGS_tpu_log_allocations_on_oom,
        // also dumps the map.
        return ResourceExhaustedError(...);

    Block found = GetBlockIf(it->lo, kFree);               // materialise the free block
    int64_t split_offset = found.end - aligned;            // PLACE AT TOP of the block
    SplitBlock(found, split_offset, /*left=*/kFree, /*right=*/kAllocated);  // 0x1e819060
    ShrinkFreeListEntry(it, found);                        // delete or re-key the free node

    bytes_in_use_ += aligned;  num_allocations_ += 1;      // packed XMM @ +0x80
    peak_block_bytes_in_use_ = max(peak_block_bytes_in_use_, aligned);
    peak_bytes_in_use_       = max(peak_bytes_in_use_, bytes_in_use_);
    return base_offset_in_bytes_ + split_offset;           // = found.end - aligned + base
}
```

The byte trace confirms the split call exactly: `SplitBlock(&out, this, &found, found.end - aligned, /*left*/0, /*right*/1)` and the return `base_offset_in_bytes_ + split_offset`.

### Top-down placement

The allocation is carved from the **high end** of the chosen free block: the allocated range is `[found.end − aligned, found.end]`, and the **low** remainder `[found.begin, found.end − aligned]` stays free. Packing allocations against the high end keeps the bottom of the region contiguous and below the reserved-bottom watermark, which improves the chance that small dynamic allocations can be satisfied without disturbing the protected bottom region.

### No minimum-split-remainder

`SplitBlock` (`0x1e819060`) has exactly one guard: if `split_offset == orig.end` (an exact fit), it writes only the left entry and returns — no remainder block. Otherwise it always emits a right-side entry, **no matter how small the leftover**. The decompile shows `if (a4 == orig.end) ...` (where `a4` is the split offset) as the sole branch; there is no comparison against a minimum-remainder constant.

```c
// SplitBlock(const Block& orig, int64_t split_off, BlockState left, BlockState right)
//   (0x1e819060) — re-states the existing slot in place, optionally adds a right entry
void SplitBlock(const Block& orig, int64_t split_off, BlockState left, BlockState right) {
    // LEFT  = [orig.begin, split_off)   gets `left`,  keeps orig.metadata
    // RIGHT = [split_off,  orig.end)    gets `right`
    if (orig.slot_ptr) {                               // re-state existing slot in place
        orig.slot_ptr->state = left;
        orig.slot_ptr->size  = split_off - orig.begin;
    } else {
        blocks_by_offset_.try_emplace(orig.begin,
            HashTableEntry{orig.begin, left, split_off - orig.begin, orig.metadata});
    }
    if (split_off == orig.end) return;                 // EXACT FIT -> no right block
    blocks_by_offset_.try_emplace(split_off,           // ANY leftover -> new entry
        HashTableEntry{split_off, right, orig.end - split_off, /*md*/0});
}
```

This matters for fragmentation analysis. A min-remainder threshold (like `dlmalloc`'s `MINSIZE`) trades a small amount of internal fragmentation to avoid creating unusably-small free blocks. This allocator makes the opposite choice: it never refuses to split, so it never inflates an allocation, but it can leave arbitrarily small free blocks in the tree. That is acceptable here because the **remainder is always an alignment multiple** (see below), so the smallest possible free block is one alignment quantum — 1024 B for HBM — never a sub-quantum sliver.

> **GOTCHA —** because there is no min-remainder, a long-lived workload that allocates and frees buffers of slightly varying sizes can accumulate many one-quantum free blocks scattered across the region. That is *external* fragmentation, and it is precisely what the OOM diagnostic's "The largest contiguous region of free memory is &lt;N&gt; due to fragmentation." message reports, and what [compaction](on-device-compaction.md) reclaims. The free *tree* never wastes the space — the bytes are available — but no single allocation larger than the largest gap can be satisfied without a defrag.

### Internal vs external fragmentation

- **Internal fragmentation** is the round-up padding: `aligned − size` bytes per allocation. It is owned by the allocation — `HashTableEntry.size` stores the *aligned* size, and there is no separate padding ledger. On free, the full aligned size is returned. For HBM with a 1024 B quantum this is at most 1023 B per buffer.
- **External fragmentation** is free space split across non-adjacent blocks. The allocator fights it two ways: (1) eager coalescing on every free (next section) immediately re-merges adjacent gaps, and (2) when even coalesced free space cannot satisfy a request, the OOM path triggers `Compact` (see [on-device-compaction.md](on-device-compaction.md)). `GetFragmentation` (`0x1e819c80`) reports `(total_free − largest_free_run) / total_free` — the fraction of free space not in the single largest contiguous run.

`ShrinkFreeListEntry` (`0x1e819520`) keeps the free tree consistent after the split: if the free block was fully consumed (`remaining == 0`) it erases the node and `operator delete`s it; otherwise it removes and re-inserts the node at its new sorted position (the block shrank, so its size key changed).

| Function | Address | Role |
|---|---|---|
| `Allocate` | `0x1e817820` | round-up → find → split-at-top → shrink → stats → `base+offset` |
| `SplitBlock` | `0x1e819060` | left/right states; exact-fit → no right entry; no min-remainder |
| `ShrinkFreeListEntry` | `0x1e819520` | delete-if-empty / remove+reinsert on size change |
| `BadSizeError` | `0x1e818240` | size `< 0` or `> max_aligned_size_` |
| `GetFragmentation` | `0x1e819c80` | `(free − largest_run) / free` |

---

## Coalescing on Free

`Deallocate` (`0x1e819dc0`) validates the address, decrements stats, **coalesces with both physical neighbours**, and re-inserts the merged free block. Coalescing is **eager, immediate, and bidirectional** — never deferred at the allocator level. (The `DeferredTpuAllocator` wrapper of the PJRT layer defers the *call* to `Deallocate`, not the coalescing inside it.)

```c
// Deallocate(int64_t address) -> Status   (0x1e819dc0), byte-confirmed
Status Deallocate(int64_t address) {
    int64_t offset = address - base_offset_in_bytes_;        // +0x58
    HashTableEntry* slot = blocks_by_offset_.find(offset);   // SwissTable H2 probe
    if (!slot || slot->state != kAllocated)                  // state != 1 -> error
        return FailedPreconditionError(                      // line 505
            "Attempted to deallocate address %d, which is not the address of "
            "an allocated buffer.", address);

    Block self = { offset, offset + slot->size, kAllocated, slot->metadata };
    bytes_in_use_ -= slot->size;  num_allocations_ -= 1;     // packed XMM (vpsubq) @ +0x80

    // (1) COALESCE WITH PREVIOUS free block (its END == self.begin).
    //     The prev block is found by free-tree find_equal keyed on self.begin; if
    //     present, its tree node is removed and MergeBlock extends DOWNWARD.
    Block prev = GetBlockIf(self.begin - prev_size, kFree);
    if (prev.slot_ptr) {                                     // prev exists and is free
        free_tree_.erase(find_equal(FreeBlock{prev.begin, ...}));
        MergeBlock(prev, self, /*new=*/kFree, &self);        // self.begin <- prev.begin
    }
    // (2) COALESCE WITH NEXT free block (its OFFSET == self.end).
    Block next = GetBlockIf(self.end, kFree);                // map.find(self.end)
    if (next.slot_ptr) {                                     // next exists and is free
        free_tree_.erase(find_equal(FreeBlock{next.begin, ...}));
        MergeBlock(self, next, kFree, &self);                // self.end <- next.end
    }
    // (3) (re)insert the coalesced free block into the size-ordered tree.
    AddToFreeList(self);                                     // find_equal-then-insert

    VLOG(2) << "Deallocated a block start at: " << address   // line 607
            << " of " << HR(self.size) << ". Available: " << HR(BytesAvailable());
    return OkStatus();
}
```

### Adjacency detection is by offset, O(1)

The whole reason `blocks_by_offset_` stores allocated *and* free blocks is this: the block physically above `self` is the one whose **offset equals `self`'s end**, so `map.find(self.end)` is the next neighbour. It is merged only if its state is `kFree`. The block below `self` is the free block whose **end equals `self`'s begin**; the free tree's `find_equal` (keyed so it matches a free node ending at `self.begin`) finds it, and it must be free by construction (the tree holds only free blocks). Both merges go through `MergeBlock`.

```c
// MergeBlock(const Block& a /*lower*/, const Block& b /*upper*/, BlockState new_state, Block* out)
//   (0x1e819700) — extend the lower entry to span both; erase the upper entry.
void MergeBlock(const Block& a, const Block& b, BlockState new_state, Block* out) {
    HashTableEntry* lo = blocks_by_offset_.find(a.begin);    // keep the lower entry
    lo->size  = (a.end - a.begin) + (b.end - b.begin);       // span both
    lo->state = new_state;
    blocks_by_offset_.erase(b.begin);                        // drop the upper entry
    *out = Block{ a.begin, b.end, new_state, a.metadata, lo->ctrl, lo };
}
```

After up to two merges, `self` is the maximal coalesced free run, and `AddToFreeList` (`0x1e81a700`) inserts a single `FreeBlock` node for it. The net effect: **the free tree never contains two adjacent free blocks** — adjacency is resolved on the spot. This is the invariant that keeps best-fit honest (a "best fit" of a fragmented pair would otherwise be wrong) and is what makes the `lo == reserved_bottom_limit_` skip in the search sufficient.

> **NOTE —** the byte trace shows the prev-merge, next-merge, and the free-node (re)insertion fused into one code path that reuses the merged `self` Block and re-keys an existing tree node in place where possible (rather than always erase-then-`operator new`). The pseudocode above is the semantic equivalent; the fused form avoids a node allocation when one of the neighbours already had a tree node that can be widened.

| Function | Address | Role |
|---|---|---|
| `Deallocate` | `0x1e819dc0` | validate → dec stats → coalesce prev+next → reinsert free |
| `MergeBlock` | `0x1e819700` | extend lower entry, erase upper entry |
| `AddToFreeList` | `0x1e81a700` | `find_equal`-then-insert `FreeBlock` RB node |
| `__find_equal<FreeBlock>` | `0x1e824640` | prev/next coalesce lookups (same comparator) |
| `GetBlockIf` | `0x1e818f20` | neighbour materialisation; null-slot Block if absent/wrong-state |

---

## The Alignment Quantum

Alignment is enforced **once, at the request boundary**, and tracked thereafter by block boundaries alone.

- **Round-up on allocate.** `aligned = (size + align − (size != 0)) & −align`, with `align = alignment_in_bytes_` (`+0x60`), a power of two. The `(size != 0)` term makes a zero-byte request stay zero rather than rounding up to one quantum.
- **Region end rounded down at construction.** The ctor sets `allocatable_range_end_` (`+0x70`) `= end − (end % align)`. Combined with `base_offset` being a multiple of the quantum, this guarantees **every offset, size, and remainder is an alignment multiple** — the free list can never produce a sub-quantum fragment.
- **Upper bound.** `max_aligned_size_` (`+0x68`) `= INT64_MAX − (INT64_MAX % align)`; any request above it is rejected by `BadSizeError`, which prevents the round-up arithmetic from overflowing.

The constructor validates the `Config` with fatal `LogMessageFatal` checks, all confirmed in the decompile:

```text
base_offset_in_bytes_ >= 0
alignment_in_bytes_ > 0
allocatable_range_end_ > 0
alignment_in_bytes_ % granule_in_bytes_ == 0
(alignment_in_bytes_ & (alignment_in_bytes_ - 1)) == 0   "alignment_in_bytes_ must be a power of two"
```

### What the quantum is per tier

The quantum is the `Config.alignment_in_bytes_` for that tier, and it must be a power of two and a multiple of the hardware granule. For **HBM** it is the **1024 B DMA minimum** (`jf_driver::kHbmMinimumDmaAlignment`). The DMA-issue sites enforce the same constant independently (`(address & (kHbmMinimumDmaAlignment − 1)) == 0`), so an allocator with a smaller HBM quantum would hand out addresses the DMA engine rejects. The full DMA alignment contract, the 16 KiB *compile-time* program alignment (`xla_jf_program_hbm_alignment_in_kib`, applied before MSA so static offsets arrive pre-aligned), and the per-tier table live on [hbm-dma-alignment.md](hbm-dma-alignment.md). This page only needs the fact that the allocator's quantum *is* that tier's `Config.alignment_in_bytes_` and that everything downstream of it is an alignment multiple.

> **NOTE —** because padding from round-up is owned by the allocation (it is included in `HashTableEntry.size`), there is no separate padding accounting. The bytes between `size` and `aligned` are simply part of the allocated block and are returned to the free list on `Deallocate`. Compaction's `Memmove` sizes are expressed in *granule* units (`size / granule_in_bytes_`), not byte units — see [on-device-compaction.md](on-device-compaction.md).

---

## Single Pool, Fixed Region

There is exactly **one** `BestFitAllocator` per `(core, memory-tier)`. No arenas, no slabs, no growth-by-doubling on the device side — the region is a single fixed `[base_offset_in_bytes_, allocatable_range_end_)` span established at boot from the `Config`. Capacity is fixed; `Expand` (`0x1e81a7a0`) and `Shrink` (`0x1e81ada0`) adjust the region bounds but are used by the program-stack managers, not by general allocation.

The only arena-like layer in the whole memory stack is the **host** `PremappedMemoryManager`, which round-robins N power-of-two partitions, each wrapping its own `BestFitAllocator` under an `absl::Mutex` — a host-DMA staging pool, not the device HBM allocator. It and the host-side `tsl::BFCAllocator` (used only for HBM buffers XLA spills to host RAM) are covered on [overview.md](overview.md) and [embedded-tcmalloc.md](embedded-tcmalloc.md).

> **WARNING —** a single `BestFitAllocator` instance is **not internally synchronized**. The HAL holds the relevant lock above it (the host `PremappedMemoryManager` wraps each partition's allocator in a per-partition `absl::Mutex`; on the device side the per-core HAL serializes access). A reimplementer must not assume `Allocate`/`Deallocate` are thread-safe on their own.

---

## OOM and Defragmentation (Pointer)

When `FindAllocatableBlock` returns `end()`, `Allocate` returns the `ResourceExhausted` "…That was not possible. There are &lt;N&gt; free. The largest contiguous region of free memory is &lt;M&gt; due to fragmentation." status. (The superficially-similar "…at the bottom of memory…" diagnostic belongs to `ReserveBottomOfMemory`, not this path.) The runtime's response — the bounded (≤ 2 attempt) retry, program eviction, and the `Compact` defrag that emits a `TpuMemmoves` relocation plan and rewrites the block map + free tree to the post-compaction layout — is **owned by [on-device-compaction.md](on-device-compaction.md)**. The one fact that belongs here: `Compact` (`0x1e81c360`) takes a `flat_hash_set<int64_t>` of **pinned** addresses (MSA-static and DMA/aliasing-held buffers) that it must never relocate, and rewrites *this allocator's* `blocks_by_offset_` and `free_tree_` to the new layout so live `tpu::TpuBuffer` handles resolve to the new offsets automatically. The pinned set is the precise mechanism enforcing "MSA owns the static layout; the runtime owns the dynamic layout in the remaining space."

---

## Relationship to MSA (Pointer)

[MSA](../compiler/msa-overview.md) is the *compile-time* planner: for every `HloValue` it picks a memory space and (via `xla::jellyfish::ProgramMemoryAllocator`) a frozen offset, serialized into the `ProgramMemoryMetadata` proto inside the compiled program. At load time `ProgramMemoryAllocator::CreateFromProto` replays those offsets and this `BestFitAllocator` is *told where* each static buffer goes — it does not run the best-fit search for them. The free-list math documented here runs only for the **dynamic** surface. This is why the runtime allocator and the compile-time placement are two distinct pages: same conceptual problem (where does each buffer sit in HBM), two completely different code paths, run at two different times.

---

## Cross-References

- [overview.md](overview.md) — the five on-chip tiers + host memory; where this allocator sits in the map
- [hbm-dma-alignment.md](hbm-dma-alignment.md) — the 1024 B DMA-issue alignment contract and the 16 KiB compile-time program alignment that feed this allocator's quantum
- [on-device-compaction.md](on-device-compaction.md) — `Compact`, the `TpuMemmoves` relocation plan, the OOM retry chain, and the pinned-set bridge to MSA
- [vmem-allocator.md](vmem-allocator.md) — the sibling VMEM tier, same `BestFitAllocator` class, different `Config`
- [cmem-pool.md](cmem-pool.md) — the CMEM tier (dormant where `CmemSize == 0`)
- [tpu-buffer-layout.md](tpu-buffer-layout.md) — how a device buffer's offset maps to the on-device tile layout
- [buffer-donation-aliasing.md](buffer-donation-aliasing.md) — aliased buffers that become pinned during compaction
- [embedded-tcmalloc.md](embedded-tcmalloc.md) — the host-side `tsl::BFCAllocator` / tcmalloc path (distinct from this device allocator)
- [../compiler/msa-overview.md](../compiler/msa-overview.md) — the compile-time HBM/VMEM placement pass that freezes the static offsets this allocator replays
- [../compiler/msa-reservation-hbm-policy.md](../compiler/msa-reservation-hbm-policy.md) — per-space capacity / reservation policy upstream of the runtime allocator
- [back to index](../index.md)
