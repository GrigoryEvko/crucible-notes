# Memory Pool Allocator

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas replaces `malloc`/`free` with a custom hierarchical pool allocator for the vast majority of allocations. The allocator (`sub_424070`, 3,809 callers) is the single most-used allocation function in the binary. Every IR node, hash map, linked list, phase object, and temporary buffer flows through pools. The design serves two goals: fast allocation via size-class free lists, and per-compilation-unit lifetime management via hierarchical pool ownership.

| | |
|---|---|
| **Allocator** | `sub_424070` (2,098 bytes, 3,809 callers) |
| **Deallocator** | `sub_4248B0` (923 bytes, 1,215 callers) |
| **Reallocator** | `sub_424C50` (488 bytes, 27 callers) |
| **OOM handler** | `sub_42BDB0` (14 bytes, 3,825 callers) |
| **TLS context** | `sub_4280C0` (597 bytes, 3,928 callers) |
| **Stats header** | `sub_423A10` (323 bytes) — prints "Memory space statistics for ..." banner |
| **Stats detail** | `sub_425020` (~1,500 bytes) — full per-pool metrics, recursive into children |
| **Stats entry** | `sub_425AB0` (80 bytes) — mutex-wrapped entry point for stats dump |
| **OCG stats** | `sub_6936B0` (120 bytes) — OCG mem space fixed-format stats to stderr |
| **Pool teardown** | `sub_4234D0` (258 bytes) |
| **Pool accounting** | `sub_423600` (922 bytes) |
| **Slab registration** | `sub_423E50` (544 bytes) |
| **Size-class index** | `sub_42BE50` (floor-log2, 64 bytes) |
| **Slab growth** | `sub_423B60` / `sub_423C70` |
| **Global fallback** | `sub_427A10` (raw malloc wrapper) |
| **System free** | `sub_427B30` (raw free wrapper) |
| **Pool reporter** | `sub_C62200` (888 bytes) |
| **Consumption query** | `sub_8DAE60` (32 bytes) |
| **Snapshot** | `sub_8DADE0` (48 bytes) |

## Pool Object Layout

The pool object is at least 7,136 bytes. It contains pool metadata at low offsets, large-block free lists indexed by power-of-2 order in the middle range, small-block free lists indexed by size class starting at offset +2128, and a mutex pointer at the end.

```text
Pool Object (~7136 bytes)
  +0        ptr      large_block_list     singly-linked list of large-block slab descriptors
  +32       u32      min_slab_size        minimum slab allocation (default from pool creator)
  +44       u32      slab_count           number of slabs allocated for this pool
  +48       ptr      large_free_list      free list head for large blocks
  +56       u32      fragmentation_count  decremented on block split
  +60       u32      max_order            highest power-of-2 order currently tracked
  +64..     ptr[]    order_free_lists     per-order free list: *(pool + 32*(order+2)) = head
  +2112     ptr      tracking_map         hash map for allocation metadata (when enabled)
  +2128..   ptr[]    small_free_lists     625 bins: *(pool + 8*(size>>3) + 2128) = head
  +7128     mutex*   pool_mutex           pthread_mutex_t* for thread safety
```

### Size-Class Bins (Small Path)

Small allocations (up to 4,999 bytes) are served from 625 free-list bins. Each bin holds blocks of exactly one size class. The bin index is computed from the 8-byte-aligned allocation size:

```c
aligned_size = max(16, (requested + 7) & ~7)
bin_index    = aligned_size >> 3
bin_head     = *(pool + 8 * bin_index + 2128)
```

This gives bins for sizes 16, 24, 32, 40, ... up to 4,992 bytes (the largest multiple of 8 that is <= 4,999). The minimum allocation is 16 bytes because each free block stores a next-pointer (8 bytes) and a slab-descriptor back-pointer (8 bytes).

### Order Free Lists (Large Path)

Large allocations (above 4,999 bytes) use power-of-2 order free lists. The order is computed by `sub_42BE50`, which returns `floor(log2(size))` by clearing all bits except the highest set bit, then using `_BitScanForward64`. The free list for order `k` is at pool offset `32*(k+2) + 64`. The pool tracks `max_order` at +60 to avoid scanning empty higher-order lists.

## Allocation Algorithm — `sub_424070`

The allocator takes two arguments: a pool pointer (`a1`) and a size (`a2`). When `a1` is NULL, it falls through to the global allocator (`sub_427A10`) which wraps `malloc`. Otherwise, it acquires the pool mutex and dispatches to one of two paths based on the aligned size.

```c
// Pseudocode for sub_424070
void* pool_alloc(Pool* pool, size_t size) {
    if (!pool)
        return global_alloc(size);    // sub_427A10 -> malloc

    pthread_mutex_lock(pool->mutex);  // pool + 7128
    size_t aligned = (size + 7) & ~7;

    if (aligned <= 4999) {
        // --- Small path ---
        if (aligned < 16) aligned = 16;
        size_t bin = aligned >> 3;
        FreeNode** head = &pool->small_free_lists[bin];

        if (!*head) {
            // Bin empty: allocate a new slab from parent pool
            if (!can_grow(pool->min_slab_size))    // sub_423B60
                goto oom;

            // 1. Allocate 56-byte slab descriptor from parent pool
            Pool* parent = get_tls_context()->parent_pool;
            SlabDesc* desc = pool_alloc(parent, 56);

            // 2. Compute slab memory: aligned * ceil(min_slab_size / aligned)
            size_t slab_bytes = aligned * ((aligned + pool->min_slab_size - 1) / aligned);

            // 3. Allocate slab memory from parent
            void* slab_mem = pool_alloc(parent, slab_bytes);

            // 4. Initialize slab descriptor
            desc->total_size     = slab_bytes;   // +8
            desc->available_size = slab_bytes;   // +16
            desc->owning_pool    = pool;         // +24
            desc->memory_base    = slab_mem;     // +32
            desc->is_small_slab  = 1;            // +40
            desc->slab_id        = atomic_inc(&global_slab_counter);
            desc->bin_size       = aligned;      // +48

            // 5. Carve slab into free-list nodes
            char* cursor = slab_mem + slab_bytes;
            FreeNode* list = NULL;
            while (cursor > slab_mem) {
                cursor -= aligned;
                ((FreeNode*)cursor)->next = list;
                ((FreeNode*)cursor)->slab = desc;
                list = (FreeNode*)cursor;
            }
            *head = list;

            // 6. Register slab in tracking structures
            register_slab(desc);                   // sub_423E50
            pool->slab_count++;
        }

        // Pop from free list
        FreeNode* block = *head;
        *head = block->next;
        block->slab->available_size -= aligned;

        pthread_mutex_unlock(pool->mutex);
        return block;
    }

    // --- Large path ---
    size_t total = aligned + 32;  // 32 bytes for boundary tag header

    // Search order free lists starting from floor(log2(total))
    int order = floor_log2(total);       // sub_42BE50
    while (order <= pool->max_order) {
        BoundaryTag* block = pool->order_lists[order];
        while (block) {
            if (block->payload_size >= total) {
                // Found a fit: unlink from free list
                unlink_free_block(block);
                block->sentinel = -1;  // mark allocated

                // Split remainder if >= 40 bytes
                size_t remainder = block->payload_size - total;
                if (remainder > 39) {
                    split_block(block, total, remainder);
                    pool->fragmentation_count--;
                }

                // Update slab accounting
                slab_desc->available_size -= block->tag_offset;

                pthread_mutex_unlock(pool->mutex);
                return (char*)block + 32;  // skip header
            }
            block = block->next_free;
        }
        order++;
    }

    // No fit found: allocate new large slab from parent
    // (allocates 88-byte slab descriptor + slab memory + 64 bytes for
    //  header/footer boundary tags)
    allocate_large_slab(pool, total);
    // retry search...

    pthread_mutex_unlock(pool->mutex);
    return result;
}
```

### Critical Constants

| Constant | Value | Meaning |
|---|---|---|
| `0x1387` | 4,999 | Small/large allocation threshold |
| `16` | Minimum allocation | Free node: 8-byte next + 8-byte slab pointer |
| `32` | Boundary tag header size | Sentinel + prev + tag_offset + payload_size |
| `39` (`0x27`) | Minimum split remainder | Must hold a full boundary tag + at least 8 bytes |
| `56` | Slab descriptor size (small) | 7 fields |
| `88` | Slab descriptor size (large) | Extended with boundary-tag metadata |
| `64` | Overhead for large slab | Header (32) + footer (32) boundary tags |

## Deallocation Algorithm — `sub_4248B0`

The deallocator takes a single pointer argument. It locates the owning pool through the slab descriptor back-pointer (stored either inline for small blocks, or recoverable from boundary tags for large blocks), then returns the memory to the appropriate free list.

```c
// Pseudocode for sub_4248B0
void pool_free(void* ptr) {
    if (!ptr) { system_free(ptr); return; }   // sub_427B30

    // Locate slab descriptor via tracking map
    SlabDesc* desc = find_slab(ptr);
    if (!desc) { system_free(ptr); return; }

    Pool* pool = desc->owning_pool;
    pthread_mutex_lock(pool->mutex);

    if (desc->is_small_slab) {
        // Small block: push back onto size-class free list
        size_t bin_size = desc->bin_size;
        size_t bin = bin_size & ~7;
        FreeNode** head = &pool->small_free_lists[bin >> 3];
        ((FreeNode*)ptr)->slab = desc;
        ((FreeNode*)ptr)->next = *head;
        *head = (FreeNode*)ptr;
        desc->available_size += bin_size;
    } else {
        // Large block: coalesce with adjacent free blocks
        BoundaryTag* header = (BoundaryTag*)((char*)ptr - 32);
        size_t block_size = header->payload_size;

        // Validate sentinel (must be -1 = allocated)
        assert(header->sentinel == -1);
        desc->available_size += block_size;

        // Check next block's sentinel
        BoundaryTag* next = (BoundaryTag*)((char*)ptr - 32 + block_size);
        if (next->sentinel != -1) {
            // Next block is free: unlink and merge
            unlink_free_block(next);
            header->payload_size += next->payload_size;
            // Update footer
        }

        // Check prev block via footer tag
        BoundaryTag* prev_footer = (BoundaryTag*)((char*)header - header->prev_free);
        if (prev_footer->sentinel != -1) {
            // Prev block is free: merge into prev
            prev_footer->payload_size += header->payload_size;
            // Update footer
        } else {
            // Header becomes free: insert into order free list
            header->sentinel = 0;  // mark free
            int order = floor_log2(header->payload_size);
            insert_free_block(pool, order, header);
        }
    }

    pthread_mutex_unlock(pool->mutex);
}
```

### Small Block Free-List Node

Each free block in a small bin stores two pointers in the returned memory region itself (since the block is not in use):

```text
Small Free Node (aligned_size bytes, minimum 16)
  +0    ptr    next       next free node in this bin, or NULL
  +8    ptr    slab_desc  back-pointer to owning slab descriptor
```

On allocation, the node is popped from the head. On deallocation, the node is pushed back to the head. This is a classic LIFO (stack) free list with O(1) alloc and free.

## Boundary Tag Format (Large Blocks)

Large blocks use a classic Knuth-style boundary tag scheme. Every allocated or free block has a 32-byte header before the user payload and a 32-byte footer at the end. The sentinel field distinguishes allocated blocks (`-1`) from free blocks (pointer to next free block, or `0`).

```text
                              Large Block Layout
  ┌──────────────────────────────────────────────────────────────────┐
  │ Header (32 bytes)                                                │
  │   +0   i64   sentinel        -1 = allocated, else next_free ptr │
  │   +8   ptr   prev_free       previous in order free list        │
  │   +16  u64   tag_offset      always 32 (header size)            │
  │   +24  u64   payload_size    user allocation size                │
  ├──────────────────────────────────────────────────────────────────┤
  │ User Payload (payload_size - 64 bytes)                           │
  │   ... returned to caller ...                                     │
  ├──────────────────────────────────────────────────────────────────┤
  │ Footer (32 bytes, at end of block)                               │
  │   +0   i64   sentinel        mirrors header sentinel             │
  │   +8   ptr   prev_free       (unused in footer)                  │
  │   +16  u64   footer_tag      always 32                           │
  │   +24  u64   block_size      total block size including headers  │
  └──────────────────────────────────────────────────────────────────┘
```

The footer allows the deallocator to coalesce with the preceding block by reading `block_size` from the footer of the previous block, then checking whether that block's header sentinel is `-1` (allocated) or a free-list pointer. This enables bidirectional coalescing in O(1) without maintaining a separate block-address data structure.

### Block Splitting

When a large free block is larger than needed, the allocator splits it if the remainder exceeds 39 bytes (enough for a header + footer + at least 8 bytes of payload). The split creates a new free block from the remainder and inserts it into the appropriate order free list. The pool's `fragmentation_count` is decremented on each split.

## Slab Descriptor

Every slab (contiguous memory region backing allocations) is tracked by a descriptor. Small slabs use 56-byte descriptors; large slabs use 88-byte descriptors with additional boundary-tag metadata.

### Small Slab Descriptor (56 bytes)

```text
SlabDesc (56 bytes)
  +0    ptr    chain_link       next descriptor in pool's slab chain
  +8    u64    total_size       total slab memory in bytes
  +16   u64    available_size   bytes currently free (decremented on alloc)
  +24   ptr    owning_pool      back-pointer to the pool that owns this slab
  +32   ptr    memory_base      base address of the contiguous slab memory
  +40   u8     is_small_slab    1 = small-alloc slab, 0 = large-alloc slab
  +44   u32    slab_id          global atomic sequence number
  +48   u32    bin_size         size class this slab serves
```

### Large Slab Descriptor (88 bytes)

Large slab descriptors extend the base 56 bytes with fields for boundary-tag free-list management. The memory base at +32 points to the raw allocation, which begins with a 32-byte header boundary tag. The descriptor at +48 points to the final footer boundary tag.

## Hierarchical Pool Model

Pools form a tree. The root is a global fallback that wraps `malloc`/`free`. Below it are named pools created by the compilation driver. Each named pool allocates its slab memory from its parent pool.

```text
    ┌─────────────────────────────────┐
    │  Global Fallback (a1 = NULL)    │
    │  sub_427A10 -> malloc           │
    │  sub_427B30 -> free             │
    └─────────┬───────────────────────┘
              │
    ┌─────────▼───────────────────────┐
    │  "Top level ptxas memory pool"  │
    │  Created in sub_446240 (driver) │
    │  Lifetime: entire compilation   │
    └─────┬───────────┬───────────────┘
          │           │
    ┌─────▼─────┐   ┌─▼──────────────────────────┐
    │ "Command  │   │  Per-compilation-unit pool  │
    │  option   │   │  (from compilation_ctx +16) │
    │  parser"  │   └──┬──────────┬───────────────┘
    └───────────┘      │          │
               ┌───────▼──┐   ┌──▼───────────────────┐
               │ "PTX     │   │ "Permanent OCG        │
               │  parsing │   │  memory pool"         │
               │  state"  │   │  per-kernel OCG state │
               └──────────┘   └───┬───────────────────┘
                                  │
                              ┌───▼───────────────┐
                              │ "elfw memory       │
                              │  space" (4096 init)│
                              │  ELF output buffer │
                              └───────────────────┘
```

### Known Named Pools

| Name | Creator | Lifetime | Purpose |
|---|---|---|---|
| `"Top level ptxas memory pool"` | `sub_446240` | Entire process | Root of all sub-pools |
| `"Command option parser"` | `sub_446240` | Entire process | CLI option storage |
| `"Permanent OCG memory pool"` | `0x1CE7B2B` ref | Per-kernel | OCG IR and pass state |
| `"PTX parsing state"` | `sub_451730` | Per-parse | Lexer/parser temporaries |
| `"elfw memory space"` | `sub_1CB53A0` / `sub_4258D0` | Per-ELF-output | ELF world (672-byte object, 4096 initial) |

### Parent Pool Resolution

When the allocator needs a new slab, it calls `sub_4280C0` to get the thread-local context, which holds a parent pool pointer at byte offset +192 (qword offset 24). This TLS context is a 280-byte (`0x118`) struct allocated via raw `malloc` on first access per thread, initialized with `pthread_cond_t` at +128, `pthread_mutex_t` at +176, and `sem_t` at +216.

```c
// TLS context layout (280 bytes = 0x118)
struct TLSContext {
    uint64_t error_flags;           // +0
    uint64_t has_error;             // +8
    // ... diagnostic fields ...
    void*    parent_pool;           // +192  (qword index 24)
    // ...
    pthread_cond_t  cond;           // +128  (48 bytes)
    pthread_mutex_t mutex;          // +176  (40 bytes)
    sem_t           sem;            // +216
    // ... diagnostic suppression ... // +384-416
};
```

The parent pool pointer determines where slab memory is allocated from. For the top-level pool, the parent is the global allocator (NULL pool, i.e., `malloc`). For sub-pools, the parent is the enclosing pool.

## Thread Safety

Every pool operation acquires a per-pool mutex at offset +7128. The mutex is lazily initialized: on first use, `sub_4286A0` (a once-init guard) creates the mutex via `sub_428320` (`pthread_mutex_init`). The initialization itself is serialized through a separate global once-init mechanism (`sub_42BDD0` saves/restores some state around the initialization).

There is also a global mutex at `qword_29FDC08` that protects the global slab counter (`dword_29FDBF4`) and the global emergency-reclaim state (`qword_29FDC00`). The allocator acquires this global mutex briefly after creating new slabs to decrement the outstanding-growth counter.

### Locking Sequence

```text
1. Lock pool->mutex  (per-pool, offset +7128)
2. Perform allocation or deallocation
3. If new slab was created:
   a. Lock global_mutex  (qword_29FDC08)
   b. Decrement dword_29FDBF4 (outstanding growth count)
   c. Unlock global_mutex
4. Unlock pool->mutex
```

The locking is strictly ordered (pool mutex first, then global mutex if needed), preventing deadlock between pool operations. There is no lock-free fast path — every allocation takes the pool mutex.

## OOM Handling

The OOM handler `sub_42BDB0` is a 14-byte stub that forwards to `sub_42F590` (the central diagnostic/fatal-error emitter) with the error descriptor at `unk_29FA530`. This triggers a `longjmp` to abort the current compilation.

```c
// sub_42BDB0 -- 14 bytes, 3825 callers
void alloc_fail_abort(void* pool, size_t size, ...) {
    fatal_error(&internal_error_descriptor, size, ...);
    // does not return -- longjmp
}
```

Every allocation site in ptxas follows the same pattern:

```c
void* p = pool_alloc(pool, size);
if (!p) alloc_fail_abort(pool, size);  // sub_42BDB0
```

The 3,825 call sites for `sub_42BDB0` exactly mirror the 3,809 callers of `sub_424070` (the difference being realloc and a few indirect call sites). This is an unconditional abort — there is no graceful degradation or fallback allocation strategy.

### Emergency Reclaim

Before aborting, the allocator at `a1 = NULL` (global path) checks for a reclaimable cache at `qword_29FDC00`. If present, it locks the global mutex, calls `sub_427B30` to free the cached block, zeroes the cache pointer, then retries the allocation. This provides a one-shot emergency reserve for the global allocator only.

## Per-Phase Memory Reporting

When `--stat=phase-wise` is enabled (option 17928), the phase manager takes memory snapshots before and after each phase, then reports deltas.

### Memory Snapshot

`sub_8DADE0` captures a 48-byte snapshot from the pool state:

```c
// sub_8DADE0 -- take_snapshot(snapshot, pool_state)
void take_snapshot(Snapshot* snap, PoolState* ps) {
    snap->pool_state    = ps;           // +0
    snap->total_alloc   = ps[80];       // +8   (qword offset 80 = byte +640)
    snap->freeable      = ps[78];       // +16  (qword offset 78 = byte +624)
    snap->freeable_leak = ps[79];       // +24  (qword offset 79 = byte +632)
    snap->metric4       = ps[76];       // +32  (qword offset 76 = byte +608)
    snap->current_usage = ps->vtable->get_usage(ps); // +40
}
```

### Memory Delta Queries

Three helper functions compute deltas between the current pool state and a saved snapshot:

| Function | Computation | Metric |
|---|---|---|
| `sub_8DAE20` | `pool[632] - snap[3]` | Total memory delta |
| `sub_8DAE30` | `pool[624] - snap[2]` | Freeable memory delta |
| `sub_8DAE40` | `snap[1] + pool[624] - snap[2] - pool[640]` | Freeable leaked delta |

### Pool Consumption Query

`sub_8DAE60` returns the current pool consumption as a single integer:

```c
// sub_8DAE60 -- pool_consumption(pool_state)
int64_t pool_consumption(PoolState* ps) {
    return *(ps->vtable->field_at_32) - ps[5];
    // i.e., total allocated from parent minus some baseline
}
```

### Reporter Output

The pool reporter (`sub_C62200`) prints to stderr:

```text
[Pool Consumption = 45.678 MB]
```

Size formatting follows the same thresholds used throughout ptxas:
- 0--1023 bytes: raw integer with `B` suffix
- 1,024--10,485,760 bytes: `%.3lf KB`
- Above 10 MB: `%.3lf MB`

The per-phase reporter (`sub_C64310`) prints one line per phase:

```text
  <phase_name>  ::  [Total 1234 KB]  [Freeable 567 KB]  [Freeable Leaked 12 KB] (2%)
```

The leak percentage is computed only when both freeable and freeable-leaked are positive.

## Memory Space Statistics Dump

ptxas contains a detailed memory-space statistics subsystem for debugging the pool allocator. The output is gated by a byte flag at context+404 (initialized to 0 in `sub_434320`; not exposed as a user-facing knob). When the flag is non-zero, the compilation driver calls into the statistics printers at two points: after each per-kernel compilation (`sub_436DF0`, `sub_4428E0`) and on error-path exit from the main driver (`sub_446240`).

### Generic Pool Statistics — `sub_425020`

The entry point is `sub_425AB0`, which acquires the pool mutex, builds a stack-local stats-context struct, and calls `sub_425020`. The stats context is 28 bytes:

```text
StatsContext (28 bytes, on stack)
  +0    ptr    output_stream     FILE* for sub_42BB30 (formatted output)
  +8    u8     verbosity_flag    enables/disables output
  +12   u32    detail_level      0 = compact, 1 = standard, 2 = per-page
  +16   u8     recurse_flag      walk child pools if set
  +20   u32    indent_level      current tab depth
  +24   u32    indent_step       tabs added per recursion level
```

`sub_425020` first calls `sub_423A10` to print the banner, then walks two structures to compute totals:

1. **Large-block slab chain** (pool+48 linked list): for each slab descriptor, accumulates `total_size` and `available_size`, and counts free blocks within each slab.

2. **Small-block bin scan** (pool+2112 hash map, via `sub_426D60`): iterates all 625 size classes (0..4992 in steps of 8), summing per-bucket `total_size` and `available_size`.

The three output metrics are `in_use = total_available - total_allocated`, all formatted as hex strings via `sprintf("0x%llx", ...)`.

**Detail level 1 (standard) output:**

```text
Memory space statistics for 'Top level ptxas memory pool'            
==========================================================
Page size                 : 0x10000 bytes
Total allocated           :     0x1a2b3c4 bytes
Total available           :     0x1ffffff bytes
Total in use              :     0x05d4c3b bytes
Nrof small block pages    : 42
Nrof large block pages    : 7
Longest free list size    : 3
Average free list size    : 0
```

**Detail level 2** adds per-page breakdowns:

```text
@@ large block page    0 : 0x1234/0x10000, #=2  max=0x5000
@@ small block size  24: 0x600/0x1800 (64/128 blocks) 3 pages
```

**Detail level 0** (compact) prints a single line:

```text
	 available= 	     0x1ffffff, allocated= 	     0x1a2b3c4, used= 	     0x05d4c3b
```

When `recurse_flag` is set, `sub_425020` calls `sub_42D4C0(child_chain, sub_425020, stats_context)` to recursively walk and print statistics for all child pools, incrementing the indentation at each level.

### OCG Memory Space Statistics — `sub_6936B0`

The OCG (Optimizing Code Generator) uses a separate fixed-page allocator tracked in a 1048-byte hash-table object with 128 buckets. `sub_6936B0` prints its statistics to stderr via `sub_427540`:

```text
Memory space statistics for 'OCG mem space'
===========================================
Page size                 : 0x100000 bytes
Total allocated           : 0x340000 bytes
Total available           : 0x400000 bytes
```

The page size is hardcoded at 0x100000 (1 MB). The counters are read from the OCG state object at offsets +1032 (`total_allocated`) and +1040 (`total_available`) of the hash-table structure at OCG-context+24.

After printing, `sub_693630` tears down the OCG allocator: it walks all 128 hash buckets freeing every linked-list entry, frees the overflow list at +1024, then frees the hash table object and the parent allocation via `sub_4248B0`.

### Trigger

Both statistics paths are gated by the same flag: `*(uint8_t*)(context + 404)`. This flag defaults to 0 and is not registered as a CLI knob. It is an internal debug mechanism, likely set only by NVIDIA-internal debug builds or environment variables not present in the release binary.

## Pool Reset and Reuse

The pool system does not expose an explicit "reset" operation that returns all allocations without freeing slabs. Instead, pool lifetime is managed through the hierarchical ownership model:

1. **Per-parse pool** (`"PTX parsing state"`): created before parsing, destroyed after parsing is complete. All lexer/parser temporaries are freed in bulk when the pool is torn down.

2. **Per-kernel pool** (`"Permanent OCG memory pool"`): created before the 159-phase pipeline runs on a kernel, destroyed afterward. All IR nodes, analysis results, and phase-local data die with this pool.

3. **ELF output pool** (`"elfw memory space"`): scoped to the ELF emission phase.

The teardown helper `sub_4234D0` walks the pool's slab chain and returns each slab's memory to the parent pool via `sub_4248B0` (free), then frees the slab descriptors themselves. Because slabs are allocated from the parent pool, this cascades upward — destroying a child pool returns memory to the parent without touching the system heap.

## Allocation Pattern: The 50KB Buffer

A pervasive allocation pattern across ptxas is the "alloc-format-shrink" idiom, observed in all PTX text formatters:

```c
// ~100+ call sites follow this exact pattern
Pool* pool = get_arena_pool(ctx, table);           // sub_4280C0 -> offset 24
char* buf = pool_alloc(pool, 50000);               // 50KB temp buffer
if (!buf) alloc_fail_abort(pool, 50000);
int len = snprintf(buf, 50000, format, ...);
char* result = pool_alloc(pool2, len + 1);          // exact-size copy
memcpy(result, buf, len + 1);
pool_free(buf);                                     // return 50KB to pool
return result;
```

The 50,000-byte temporary buffer is a "one size fits all" strategy. Because it exceeds the 4,999-byte small-path threshold, every format operation takes the large-block path. However, because the buffer is freed immediately after use, it is typically coalesced back and reused by the next formatter call, making this effectively a per-thread scratch buffer recycled through the pool.

## Global State

The allocator uses several global variables for cross-pool coordination:

| Address | Type | Purpose |
|---|---|---|
| `dword_29FDBF4` | `u32` | Outstanding slab growth count (decremented after slab creation) |
| `dword_29FDBF8` | `u32` | Emergency cache flag (zeroed when cache is reclaimed) |
| `qword_29FDC00` | `ptr` | Emergency reclaimable cache block pointer |
| `qword_29FDC08` | `mutex*` | Global mutex protecting the above three fields |
| `dword_29FDBE8` | `u32` | Global slab sequence number (atomic increment) |
| `qword_29FDBE0` | `ptr` | Global slab tracking map (for cross-pool slab lookup) |
| `qword_29FDBD8` | `mutex*` | Mutex protecting `qword_29FDBE0` |
| `byte_29FA4C0` | `u8` | Flag enabling per-pool slab tracking maps |

The slab tracking map (`qword_29FDBE0`) is a hash map keyed by `address >> 3` that maps any allocated pointer to its owning slab descriptor. The deallocator (`sub_4248B0`) consults this map when the per-pool tracking flag (`byte_29FA4C0`) is enabled. When per-pool tracking is disabled, it falls back to the global map.

## Key Functions Reference

| Address | Size | Callers | Identity |
|---|---|---|---|
| `sub_424070` | 2,098 | 3,809 | `pool_alloc(pool, size)` — main allocator |
| `sub_4248B0` | 923 | 1,215 | `pool_free(ptr)` — main deallocator |
| `sub_424C50` | 488 | 27 | `pool_realloc(ptr, new_size)` — alloc+copy+free |
| `sub_42BDB0` | 14 | 3,825 | `alloc_fail_abort()` — fatal OOM via longjmp |
| `sub_4280C0` | 597 | 3,928 | `get_tls_context()` — per-thread state accessor |
| `sub_427A10` | — | — | `global_alloc(size)` — malloc wrapper for NULL pool |
| `sub_427B30` | — | — | `global_free(ptr)` — free wrapper for non-pool memory |
| `sub_423A10` | 323 | 1 | `pool_stats_header()` — prints "Memory space statistics for ..." banner |
| `sub_425020` | ~1,500 | 1 | `pool_stats_detail()` — full metrics dump, recursive child walk |
| `sub_425AB0` | 80 | 2 | `pool_stats_entry()` — mutex-wrapped entry point |
| `sub_6936B0` | 120 | 2 | `ocg_memspace_stats()` — OCG allocator stats to stderr |
| `sub_693630` | 166 | 2 | `ocg_memspace_teardown()` — free OCG hash-table allocator |
| `sub_4234D0` | 258 | 1 | `pool_teardown()` — recursive slab deallocation |
| `sub_423600` | 922 | 3 | `pool_accounting_init()` — accounting/hash-set setup |
| `sub_423E50` | 544 | 2 | `register_slab()` — slab tracking insertion |
| `sub_423B60` | — | — | `can_grow()` — checks whether slab expansion is permitted |
| `sub_423C70` | 480 | 2 | `pool_grow()` — slab expansion handler |
| `sub_42BE50` | 64 | — | `floor_log2(size)` — clear-to-highest-bit + BSF |
| `sub_42B990` | — | — | `slab_lookup(map, addr>>3)` — find slab for address |
| `sub_4258D0` | — | — | `create_named_pool(name, flags, init_size)` |
| `sub_8DADE0` | 48 | — | `take_snapshot(snap, pool_state)` |
| `sub_8DAE20` | 16 | — | `delta_total(snap)` |
| `sub_8DAE30` | 16 | — | `delta_freeable(snap)` |
| `sub_8DAE40` | 32 | — | `delta_freeable_leaked(snap)` |
| `sub_8DAE60` | 32 | — | `pool_consumption(pool_state)` |
| `sub_C62200` | 888 | 1 | Pool consumption reporter (stderr) |
| `sub_C64310` | 3,168 | — | Per-phase timing/memory reporter |
