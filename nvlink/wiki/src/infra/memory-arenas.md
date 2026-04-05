# Memory Management (Arenas)

nvlink replaces libc `malloc`/`free` with a custom two-tier arena allocator that owns all dynamic memory for the linker's lifetime. The first tier (`arena_alloc` at `sub_4307C0`, 10,704 bytes, ~3,943 callers) is a thread-safe, per-arena allocator that manages memory through size-class free lists for small blocks and page-level doubly-linked lists for large blocks. The second tier (`ocg_memspace_alloc` at `sub_4882A0`, 2,516 bytes) is a segregated-freelist slab allocator with 128 size classes, 8-byte granularity up to 1,016 bytes, and 1 MB overflow pages -- used throughout the embedded ptxas/OCG subsystem. Arenas form a parent-child hierarchy: child arenas can merge their free lists back into a parent on destruction, enabling efficient scoped allocation for per-object or per-phase work.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_432020` | `arena_create_named` | 2,161 B | Create a new named arena with parent linkage |
| `sub_4307C0` | `arena_alloc` | 10,704 B | Core allocator: small-block free lists + large-block pages, mutex-protected |
| `sub_431000` | `arena_free` | 4,680 B | Deallocator: returns blocks to owning arena's free list |
| `sub_431C70` | `arena_destroy` | 3,564 B | Destroy arena; optionally merge free lists into parent |
| `sub_431770` | `arena_dump_stats` | 8,491 B | Print per-arena allocation statistics |
| `sub_426AA0` | `arena_strdup` | ~128 B | Allocate + copy string via arena (strdup equivalent) |
| `sub_45CAE0` | `arena_context_swap` | ~64 B | Swap current-arena pointer in TLS-like metadata |
| `sub_44F410` | `arena_get_metadata` | <2 KB | Retrieve per-thread arena metadata (contains current arena pointer) |
| `sub_45CAC0` | `arena_alloc_fail` | -- | OOM handler: aborts process on allocation failure |
| `sub_4882A0` | `ocg_memspace_alloc` | 2,516 B | Segregated-freelist slab allocator for OCG objects |
| `sub_489140` | `ocg_memspace_stats` | 4,368 B | Print OCG memspace allocation statistics |
| `sub_44ED60` | `mmap_alloc` | -- | Direct `mmap` fallback for null-arena (very large) allocations |

## Architecture Overview

### Two-Tier Design

```
                    +-----------------------+
                    |   mmap / OS pages     |   Fallback for arena_alloc(NULL, size)
                    +-----------+-----------+
                                |
          +---------------------+---------------------+
          |                                           |
  +-------+--------+                         +--------+--------+
  |  Arena Tier 1   |                         |  OCG Memspace   |
  |  (sub_4307C0)   |                         |  (sub_4882A0)   |
  |  Small+Large    |  <-- backing store -->  | Segregated      |
  |  block mgmt     |                         | free lists      |
  +-------+--------+                         +--------+--------+
          |                                           |
    +-----+------+-----+                    128 size-class buckets
    |            |      |                    + overflow list
  "nvlink    "nvlink  "elfw                  + 1MB page allocation
   option     memory   memory
   parser"    space"   space"
```

The arena tier provides the underlying page allocations. The OCG memspace tier sits on top of `arena_alloc` (calls it for 1 MB pages) and provides a finer-grained segregated-freelist allocator optimized for the many small, uniform-sized objects created by the compiler backend.

### Named Arena Hierarchy

`main()` creates two top-level arenas at startup via `arena_create_named` (`sub_432020`):

| Arena name | Variable | Purpose | Lifetime |
|---|---|---|---|
| `"nvlink option parser"` | `v338` in main | Holds all option-parsing allocations (strings, lists, paths) | Created at init, destroyed at cleanup |
| `"nvlink memory space"` | `v339` in main | Holds all linker working data (sections, symbols, relocations) | Created at init, destroyed at cleanup |

Additional arenas are created dynamically during execution:

| Arena name | Created by | Purpose |
|---|---|---|
| `"elfw memory space"` | `elfw_create` (`sub_4438F0`) | Per-ELF-wrapper allocations for section data, symbol tables |
| `"<anonymous>"` | Various | Unnamed child arenas for scoped temporary allocation |

Child arenas store a parent pointer at offset +16 of the arena control block. On destruction with `merge_flag=1`, the child's free lists are merged into the parent rather than released, allowing the parent to reuse the memory without returning it to the OS.

## Arena Control Block Layout

`arena_create_named` allocates a 7,136-byte control block via `arena_alloc` from the global arena. The block is zeroed on creation.

```
Arena Control Block (7,136 bytes)
================================================================
Offset   Size   Field                 Description
----------------------------------------------------------------
   0       8    name                  Pointer to arena name string
   8       1    closed_flag           Set to 1 when arena is being destroyed
   9       7    (padding)
  16       8    parent_arena          Pointer to parent arena (NULL for root)
  24       8    child_list            LinkerHash of child arenas (ptr-keyed)
  32       4    page_size             Default page allocation size
  36       4    (reserved)
  40       4    stats_field_1         Allocation tracking field 1
  44       4    small_page_count      Count of small-block pages allocated
  48       8    large_page_list       Head of large-block page linked list
  56       4    large_block_high      Largest large-block page-list index seen
  60       4    max_page_class        Maximum page size class allocated

  64    2048    page_lists[64]        Doubly-linked lists of large-block pages
                                      (64 entries x 32 bytes each, indexed by
                                      ceil_log2 of page size)

2112       8    page_tree             B-tree or sorted structure for page lookup
2120       8    (reserved)

2128    5000    free_lists[625]       Small-block free-list heads
                                      (625 entries x 8 bytes, indexed by
                                      aligned_size / 8, sizes 16..4999)

7128       8    mutex                 pthread_mutex_t pointer for thread safety
7136            (end)
```

### Free-List Indexing (Small Blocks)

Small-block sizes are 8-byte aligned. The free-list array at offset 2128 is indexed by `(size & ~7)`, giving 625 buckets covering sizes from 16 bytes to 4,999 bytes. Each bucket is a singly-linked list of free blocks. Each free block stores:

```
Free Block (small, in free list)
================================
Offset  Size  Field
--------------------------------
  0       8   next           Pointer to next free block in same size class
  8       8   page_metadata  Pointer to owning page's metadata record
```

### Page Metadata Record

Each page allocation (whether for small blocks or large blocks) is tracked by a metadata record. Small-block pages use a 56-byte record; large-block pages use an 88-byte record.

```
Small-Block Page Metadata (56 bytes)
=====================================
Offset  Size  Field
-------------------------------------
  0       8   tree_link        Page tree linkage
  8       8   available_bytes  Bytes remaining available in this page
 16       8   total_bytes      Total bytes allocated for this page
 24       8   owning_arena     Pointer back to the arena
 32       8   data_pointer     Start of the data region
 40       1   is_small_block   Flag: 1 = small-block page
 41       3   (padding)
 44       4   sequence_number  Monotonically increasing page ID
 48       4   size_class       Size class this page serves (= aligned size)
 52       4   (padding)
```

```
Large-Block Page Header (in-band, 64 bytes before data)
========================================================
Offset  Size  Field
--------------------------------------------------------
  0       8   sentinel         -1 (0xFFFFFFFFFFFFFFFF) marks block start
  8       8   prev_block       Doubly-linked list backward pointer
 16       8   block_size       Size of this free region (including header)
 24       8   (reserved)
 32       8   next_page        Forward pointer in page-list
 40       8   page_list_head   Pointer back to arena page_lists[] entry
 48       8   total_page_size  Total size of the containing page
 56       8   offset_to_data   Offset from header start to usable data (=32)
```

## arena_alloc (sub_4307C0) -- Core Allocator

This is the central allocator, called by virtually every function in the binary. It takes `(arena_ptr, size)` and returns an aligned pointer. The function is 10,704 bytes / 433 decompiled lines -- it is recursive (allocates page metadata from itself) and thread-safe via per-arena mutex.

### Allocation Paths

**Path 1: Null arena (direct mmap)**

When `arena_ptr` is NULL, the allocator calls `sub_44ED60` (an `mmap` wrapper) to obtain memory directly from the OS. If the first attempt fails, it checks `dword_2A5F384` (an emergency page-release counter): if non-zero, it releases cached pages under a global mutex (`qword_2A5F398`) by calling `sub_44EE80(qword_2A5F390, 1)` to free reserved memory, then retries the `mmap`. If both attempts fail, it calls the OOM handler `sub_45CAC0` which aborts.

```c
void *arena_alloc(Arena *arena, size_t size) {
    if (!arena) {
        void *p = mmap_alloc(size);          // sub_44ED60
        if (!p) {
            if (emergency_page_count > 0) {
                lock(global_mmap_mutex);
                release_emergency_pages();   // sub_44EE80
                unlock(global_mmap_mutex);
                p = mmap_alloc(size);
            }
            if (!p) oom_abort(size);         // sub_45CAC0
        }
        return p;
    }
    // ... arena-based allocation below
}
```

**Path 2: Small blocks (size <= 4,999 bytes)**

Sizes are rounded up to 8-byte alignment with a minimum of 16 bytes. The allocator locks the arena mutex, then checks the free-list bucket at `arena + 2128 + 8 * (size >> 3)`.

If a free block exists: pop from the list head, decrement the page metadata's available-bytes counter, unlock, and return.

If no free block: allocate a new page of `(page_size / size) * size` bytes, carve it into `size`-byte chunks, thread them into the free list, register the page metadata in the arena's page tree, then pop and return the first chunk. Page metadata itself is allocated recursively from the global arena.

```c
    size_t aligned = (size + 7) & ~7ULL;
    if (aligned < 16) aligned = 16;

    lock(arena->mutex);

    int idx = aligned >> 3;
    FreeBlock *block = arena->free_lists[idx];     // arena + 2128 + idx*8

    if (!block) {
        // Allocate new page, carve into chunks
        size_t page_bytes = aligned * (arena->page_size / aligned);
        PageMeta *meta = arena_alloc(global_arena, 56);   // recursive
        void *page = arena_alloc(global_arena, page_bytes);
        meta->total_bytes = page_bytes;
        meta->owning_arena = arena;
        meta->is_small_block = 1;
        meta->size_class = aligned;

        // Thread chunks into free list
        for (char *p = page; p + aligned <= page + page_bytes; p += aligned) {
            *(void **)p = block;
            *((void **)(p + 8)) = meta;
            block = (FreeBlock *)p;
        }
        arena->free_lists[idx] = block;
        register_page(arena, meta);                // sub_4305A0
    }

    arena->free_lists[idx] = block->next;
    block->page_metadata->available_bytes -= aligned;
    unlock(arena->mutex);
    return block;
```

**Path 3: Large blocks (size > 4,999 bytes)**

Large allocations add a 32-byte in-band header and search the arena's page lists. The page-list array at offset 64 holds 64 doubly-linked lists indexed by `ceil_log2(size)`. The allocator scans from the computed size class upward looking for a page with a free region large enough.

If a region is found: unlink it from the page list, split off any excess into a new free region (minimum 40 bytes for the split header), and return data at offset +32 from the region header.

If no region fits: allocate a new page (at least `arena->page_size` bytes, at least `size + 32`) via recursive `arena_alloc(global_arena, ...)`, set up sentinel headers at both ends, link into the appropriate page-list bucket, and retry.

```c
    size_t total = aligned + 32;              // 32-byte header

    int cls = ceil_log2(total);               // sub_45CB60
    for (int i = cls; i <= arena->max_page_class; i++) {
        PageEntry *entry = arena->page_lists[i];     // arena + 64 + 32*i
        while (entry) {
            if (entry->free_size >= total) {
                // Found fit: unlink, optionally split remainder
                unlink_from_page_list(entry);
                if (entry->free_size - total > 39) {
                    split_and_reinsert_remainder(entry, total);
                }
                return (char *)entry + 32;
            }
            entry = entry->next;
        }
    }
    // No fit: allocate new page from global arena
    allocate_new_large_page(arena, total);
    // retry...
```

### Thread Safety

Every arena operation acquires the per-arena mutex at offset 7128 before touching free lists or page lists. Mutex initialization is lazy: the first access checks `arena+7128`, and if NULL, acquires a global lock (`sub_44F9F0`), creates a new mutex via `sub_44F670`, stores it, and releases the global lock (`sub_44FAC0`).

```c
    pthread_mutex_t *mtx = arena->mutex;       // offset 7128
    if (!mtx) {
        global_lock();                          // sub_44F9F0
        if (!arena->mutex) {
            int64_t old = arena_context_swap(0);
            arena->mutex = create_mutex();      // sub_44F670
            arena_context_swap(old);
        }
        global_unlock();                        // sub_44FAC0
        mtx = arena->mutex;
    }
    pthread_mutex_lock(mtx);
```

The `arena_context_swap` (`sub_45CAE0`) call around mutex creation prevents the mutex allocation itself from accidentally using the arena being initialized.

### Debug Allocator Mode

When `byte_2A5BAD0` is non-zero, the allocator operates in a debug tracking mode. In this mode, `arena_alloc` registers every allocation in a hash table (`qword_2A5F370`) keyed by `address >> 3`, and `arena_free` looks up the allocation's metadata from this hash table rather than relying on the in-band metadata alone. This enables leak detection and double-free checking. The hash table is protected by its own global mutex (`qword_2A5F368`).

## arena_free (sub_431000) -- Deallocator

Takes `(pointer, unused)` and returns the block to its owning arena's free list.

### Deallocation Paths

**Null pointer**: delegates to `sub_44EE80` (noop cleanup).

**Debug mode** (`byte_2A5BAD0` set): looks up the allocation in the per-arena debug hash table via `sub_457F60`. If not found there, checks the global hash table `qword_2A5F370` under mutex. The hash table entry at offset +24 gives the owning arena, and offset +40/+48 give block type and size class.

**Normal mode**: the block's inline metadata (at negative offsets for large blocks, or via the free-list backpointer for small blocks) identifies the owning arena.

**Small-block return**: if the size class is <= 4,999, the block is pushed onto the arena's free-list at `arena + 2128 + (size_class & ~7)`. The page metadata's available-bytes counter is incremented.

```c
    // Small block return
    arena = metadata->owning_arena;
    size_class = metadata->size_class;
    lock(arena->mutex);

    *(void **)ptr = arena->free_lists[size_class >> 3];     // push to head
    *((void **)(ptr + 8)) = metadata;
    arena->free_lists[size_class >> 3] = ptr;
    metadata->available_bytes += size_class;

    unlock(arena->mutex);
```

**Large-block return**: the block header at `ptr - 32` is examined. The allocator checks adjacent blocks (forward and backward) for coalescing: if the next block's sentinel is -1 (free), the two regions merge. If the previous block is also free (checked via the doubly-linked page-list), it merges backward. The coalesced region is reinserted into the appropriate page-list bucket.

## arena_destroy (sub_431C70) -- Arena Teardown

Takes `(arena, merge_flag)`. Sets the closed flag at offset +8 to 1, then proceeds based on `merge_flag`:

**merge_flag = 1 (merge into parent)**:
1. Iterates all 625 small-block free-list buckets (offsets 2128..7128) and concatenates each child list onto the parent's corresponding list via `sub_4649B0` (list-append).
2. Iterates all 64 large-block page lists (offsets 64..2112, stride 32) and appends each child's doubly-linked list to the parent's tail.
3. Propagates `max_page_class` upward (takes the maximum of parent and child).
4. Child arenas are recursively destroyed first via `sub_465390` (tree walk calling `arena_destroy`).

**merge_flag = 0 (release all)**:
1. Recursively destroys child arenas via `sub_465390`.
2. Walks the page tree at offset 2112 and releases each page via `sub_431EC0` (which calls `sub_44EE80` for mmap unmap).
3. Frees the arena name string and the control block itself via `arena_free`.
4. Destroys the mutex via `sub_44F950`.

## arena_create_named (sub_432020) -- Arena Creation

Takes `(name_string, parent_arena, page_size_hint)`.

```c
Arena *arena_create_named(const char *name, Arena *parent, uint32_t page_hint) {
    int64_t saved = arena_context_swap(0, parent);

    int page_size;
    if (!page_hint) {
        page_size = 0x10000;                    // 64 KB default
        if (parent)
            page_hint = parent->page_size;      // inherit from parent
    }
    page_size = (page_hint + 7) & ~7;           // 8-byte align

    // Allocate 7136-byte control block from global arena
    Arena *arena = arena_alloc(global_arena(), 7136);
    if (!arena) oom_abort();
    memset(arena, 0, 7136);

    arena->parent_arena = parent;               // offset 16
    arena->page_size = page_size;               // offset 32
    arena->child_list = LinkerHash_create(ptr_xor_shift_hash, ptr_equal, 8);
    arena->page_tree = PageTree_create(identity_hash, int_equal, 8);

    // Lazy mutex creation stored at offset 7128
    arena->mutex = create_mutex();

    // Register with parent
    if (parent) {
        lock(parent->mutex);
        LinkerHash_insert(parent->child_list, arena);
        unlock(parent->mutex);
    }

    // Copy name string
    const char *src = name ? name : "<anonymous>";
    size_t len = strlen(src) + 1;
    char *copy = arena_alloc(global_arena(), len);
    strcpy(copy, src);
    arena->name = copy;                         // offset 0

    arena_context_swap(saved);
    return arena;
}
```

The default page size is 64 KB (`0x10000`). Child arenas inherit the parent's page size unless an explicit hint is provided.

## arena_strdup (sub_426AA0)

A thin wrapper that allocates `strlen(s)+1` bytes from the current context's arena and copies the string:

```c
char *arena_strdup(size_t size) {
    int64_t meta = arena_get_metadata(size);     // sub_44F410
    Arena *arena = *(Arena **)(meta + 24);
    char *p = arena_alloc(arena, size);
    if (!p) oom_abort();
    return p;
}
```

This is the most common allocation pattern in nvlink -- virtually every string (symbol names, section names, file paths, error messages) flows through this function.

## arena_context_swap (sub_45CAE0)

Swaps the "current arena" pointer stored in per-thread metadata. This small function is called around operations that must temporarily change which arena receives allocations:

```c
int64_t arena_context_swap(int64_t new_arena, void *unused) {
    char *meta = arena_get_metadata(new_arena);  // sub_44F410
    int64_t old = *(int64_t *)(meta + 24);       // save current
    *(int64_t *)(meta + 24) = new_arena;         // install new
    return old;                                   // return previous
}
```

The metadata structure accessed via `sub_44F410` appears to be either TLS or a global per-thread array. The pointer at offset +24 is the "active arena" for the calling thread. Offset +96 holds the debug hash table pointer when `byte_2A5BAD0` is set.

## OCG Memspace Allocator (sub_4882A0)

The OCG (Optimizing Code Generator) memspace is a second-tier allocator built on top of `arena_alloc`. It implements a segregated-freelist slab allocator optimized for the many small, fixed-size objects created by the ptxas compiler backend embedded in nvlink. The design is bump-allocation-forward within 1 MB pages, with 128 size-class buckets providing O(1) allocation for objects up to 1,016 bytes.

### Memspace Control Block

Created by `ocg_memspace_create` (`sub_488470`), which allocates a 1,048-byte block (131 QWORDs) from the arena system and zeroes it. A 40-byte wrapper object holds a function pointer to `sub_4882A0` and a backpointer to the memspace array, enabling polymorphic dispatch from the OCG object system.

```
OCG Memspace (131 x 8 = 1,048 bytes)
=============================================
Index    Content                  Description
---------------------------------------------
 0..127  free_list[128]           Size-class free-list heads (one per class)
 128     overflow_list            Free list for chunks with > 1,016 bytes remaining
 129     total_pages_allocated    Cumulative bytes obtained from arena_alloc (pages only, excludes headers)
 130     total_available          Bytes currently available across all chunks (decremented on each alloc)
```

### Size-Class Table (128 Classes)

The class index is computed as `(requested + 7) >> 3` -- the requested size rounded up to the next 8-byte boundary, divided by 8. Each class `i` serves allocations whose 8-byte-aligned size equals `i * 8` bytes. The table spans classes 0 through 127:

| Class | Aligned Size (bytes) | Request Range (bytes) | Notes |
|------:|---------------------:|----------------------:|-------|
| 0 | 0 | 0 | Sink for exhausted chunks (0 remaining) |
| 1 | 8 | 1--8 | Minimum useful allocation |
| 2 | 16 | 9--16 | |
| 3 | 24 | 17--24 | Matches chunk header size |
| 4 | 32 | 25--32 | |
| 5 | 40 | 33--40 | |
| ... | ... | ... | Each class covers an 8-byte range |
| 16 | 128 | 121--128 | |
| 32 | 256 | 249--256 | |
| 64 | 512 | 505--512 | |
| 96 | 768 | 761--768 | |
| 126 | 1,008 | 1,001--1,008 | |
| 127 | 1,016 | 1,009--1,016 | Largest size class |

**Class index formula** (verified from decompiled code at `0x4882A0`):

```
class_index = (requested_size + 7) >> 3
aligned_size = (requested_size + 7) & ~7
```

Any allocation whose aligned size exceeds 1,016 bytes (class > 127) bypasses the size-class lookup and goes directly to the overflow list or new-page path.

### Chunk Header Format

Every chunk (whether in a size-class bucket or the overflow list) carries a 24-byte in-band header at its start. The header sits immediately before the usable data region:

```
Chunk Header (24 bytes, in-band)
======================================
Offset  Size  Field
--------------------------------------
  0       8   next             Singly-linked list pointer: next chunk in same bucket
  8       8   remaining_size   Bytes still available after data_pointer
 16       8   data_pointer     Next address to hand out (advances forward on each alloc)
```

The relationship between fields is: `data_pointer + remaining_size = end_of_usable_region`. After each allocation of `N` bytes, `data_pointer` advances by `N` and `remaining_size` decreases by `N`. When `remaining_size` drops below the current bucket's threshold, the chunk migrates to a smaller bucket.

### Allocation Algorithm (Detailed)

The allocator has three paths, tried in order. Path 1 is the fast path (single array lookup). Path 2 is the fallback (linear scan). Path 3 is the slow path (OS page allocation).

**Path 1 -- Size-class direct lookup** (class 0--127, aligned <= 1,016):

```c
void *ocg_memspace_alloc(uint64_t *ms, size_t requested) {
    size_t aligned = (requested + 7) & ~7ULL;
    uint32_t cls = (uint32_t)(aligned >> 3);

    if (cls <= 127) {
        Chunk *chunk = (Chunk *)ms[cls];           // free_list[cls]
        if (chunk != NULL && aligned < chunk->remaining_size) {
            //
            // Fast path: bump-allocate from the head chunk.
            // Note: strict less-than (not <=). The chunk must have MORE
            // remaining than requested. When remaining == aligned exactly,
            // this path is skipped and the request falls through to the
            // overflow search. This guarantees data_pointer never advances
            // past the chunk boundary within this path.
            //
            void *result = chunk->data_pointer;
            chunk->remaining_size -= aligned;
            chunk->data_pointer = (char *)result + aligned;
            ms[130] -= aligned;                    // total_available

            uint32_t new_cls = (uint32_t)(chunk->remaining_size >> 3);
            if (new_cls > 127) {
                //
                // Remainder still > 1016 bytes: chunk stays in THIS
                // bucket (not unlinked). The caller simply got their
                // pointer; the chunk remains at the head of free_list[cls].
                // This is critical: the chunk was NOT popped from the list.
                // On the next allocation to this class, it will be tried
                // again. Only when remainder drops to a smaller class does
                // migration occur.
                //
                return result;
            }
            // Remainder shrank below this class: pop from current list
            ms[cls] = (uint64_t)chunk->next;       // unlink from old class
            // Fall through to reinsert at new_cls
            goto reinsert;
        }
    }
```

**Path 2 -- Overflow list search** (aligned <= 1 MB):

```c
    if (aligned <= 0x100000) {                     // 1 MB limit
        Chunk *node = (Chunk *)ms[128];            // overflow_list head
        if (node != NULL) {
            //
            // Check first node separately (different from inner loop
            // because unlinking the head updates ms[128] directly).
            //
            if (aligned <= node->remaining_size) {
                // Serve from first overflow node
                void *result = node->data_pointer;
                node->remaining_size -= aligned;
                node->data_pointer = (char *)result + aligned;
                ms[130] -= aligned;

                uint32_t new_cls = (uint32_t)(node->remaining_size >> 3);
                if (new_cls <= 127) {
                    ms[128] = (uint64_t)node->next;  // unlink from overflow
                    goto reinsert;                     // migrate to size-class
                }
                return result;                         // stays in overflow
            }

            // Walk remaining nodes (prev->next linkage for unlinking)
            while (node->next != NULL) {
                Chunk *candidate = (Chunk *)node->next;
                if (aligned <= candidate->remaining_size) {
                    void *result = candidate->data_pointer;
                    candidate->remaining_size -= aligned;
                    candidate->data_pointer = (char *)result + aligned;
                    ms[130] -= aligned;

                    uint32_t new_cls = (uint32_t)(candidate->remaining_size >> 3);
                    if (new_cls <= 127) {
                        node->next = candidate->next;  // unlink from overflow
                        // chunk = candidate for reinsert
                        goto reinsert;
                    }
                    return result;                     // stays in overflow
                }
                node = (Chunk *)node->next;
            }
        }
    }
```

**Path 3 -- New page allocation**:

```c
    //
    // No existing chunk can serve this request.
    // Allocate a fresh page via arena_alloc(NULL, ...) which goes through
    // the mmap fallback path (arena pointer is NULL).
    //
    size_t page_size;
    size_t remainder;

    if (aligned > 0x100000) {
        // Oversized: allocate exact amount, no remainder
        page_size = aligned;
        remainder = 0;
    } else {
        // Standard: 1 MB page
        page_size = 0x100000;                      // 1,048,576 bytes
        remainder = 0x100000 - aligned;
    }

    Chunk *new_chunk = (Chunk *)arena_alloc(NULL, page_size + 24);  // +24 for header
    void *result = (char *)new_chunk + 24;         // data starts after header

    new_chunk->next = NULL;
    new_chunk->remaining_size = remainder;
    new_chunk->data_pointer = (char *)result + aligned;

    ms[129] += page_size;                          // total_pages_allocated
    ms[130] += page_size - aligned;                // total_available (net gain)

    uint32_t new_cls = (uint32_t)(remainder >> 3);
    if (new_cls > 127) {
        // Remainder > 1016: insert into overflow list
        new_chunk->next = (Chunk *)ms[128];
        ms[128] = (uint64_t)new_chunk;
        return result;
    }
    // Remainder fits a size class (or is 0 for oversized allocs)
    // Fall through to reinsert

reinsert:
    // Push chunk onto the head of free_list[new_cls]
    chunk->next = (Chunk *)ms[new_cls];
    ms[new_cls] = (uint64_t)chunk;
    return result;
}
```

### Freelist Management Algorithm

The core insight is that chunks are **not permanently bound to a size class**. A fresh 1 MB page starts in the overflow list (class > 127). As allocations consume bytes from it, the chunk's `remaining_size` shrinks. When remaining drops to 1,016 bytes or below, the chunk migrates from the overflow list to `free_list[remaining >> 3]`. Further allocations continue to shrink it, potentially migrating it to progressively smaller classes.

**Lifecycle of a typical 1 MB page**:

```
1. Page allocated: 1,048,576 bytes usable
   -> Inserted into overflow_list (remaining >> 3 = 131072, > 127)

2. After ~100 allocations of 24 bytes each (2,400 bytes consumed):
   remaining = 1,046,176 -> still in overflow (class 130772, > 127)

3. After ~43,690 allocations of 24 bytes (1,048,560 consumed):
   remaining = 16 -> migrates to free_list[2] (class 2)

4. After one more 16-byte allocation:
   remaining = 0 -> migrates to free_list[0] (exhausted sink)

5. Class 0 chunks are inert: any future lookup finds remaining = 0,
   fails the "aligned < remaining" check, and falls through.
```

**Class migration trigger** -- The migration check uses truncating integer division: `new_cls = (uint32_t)(remaining >> 3)`. This means the chunk migrates when `remaining` drops to 1,016 or below. The threshold for each class boundary is exact: a chunk with 1,017 bytes remaining has `new_cls = 127` and lands in `free_list[127]`; a chunk with 1,024 bytes remaining has `new_cls = 128` (> 127) and stays in the overflow list.

**Asymmetric comparison** -- The size-class path uses strict less-than (`aligned < remaining`), while the overflow path uses less-than-or-equal (`aligned <= remaining`). This means a chunk in class `K` with exactly `K * 8` bytes remaining will **not** serve a request for `K * 8` bytes via the size-class path -- the request falls through to overflow or new-page. This is a deliberate choice: it ensures the size-class fast path always leaves some remainder, guaranteeing that `data_pointer` never overruns the chunk. The overflow path's `<=` comparison handles the exact-fit case, allowing a chunk to be fully consumed there.

### 1 MB Page Layout

Each page obtained from the OS has a 24-byte chunk header prepended by the allocator. The usable region follows immediately:

```
+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+
| Chunk Header (24 bytes)       | Usable Data (page_size bytes)   |
| next | remaining | data_ptr   |                                 |
+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+
^                               ^                                 ^
|                               |                                 |
new_chunk                       new_chunk + 24                    new_chunk + 24 + page_size
                                = initial data_pointer
```

The call to `arena_alloc(NULL, page_size + 24)` obtains `page_size + 24` bytes via `mmap`. Since the arena pointer is NULL, this bypasses the arena system entirely and goes straight to OS virtual memory. The 24-byte overhead per page is minimal relative to the 1 MB (0.002%) page size.

**Memory accounting**:
- `ms[129]` (total_pages_allocated) tracks only the `page_size` portion, excluding the 24-byte header
- `ms[130]` (total_available) tracks the delta: `page_size - aligned` is added when a page is allocated, and `aligned` is subtracted on each allocation from any path

### Key Design Properties

**No individual free**: the OCG memspace does not support freeing individual allocations. Objects are allocated forward from 1 MB pages and never returned individually. The entire memspace is released when the containing arena is destroyed. This is appropriate for compiler objects that live for the duration of a compilation unit.

**Bump allocation within chunks**: each chunk tracks a `data_pointer` that advances forward. This gives near-zero allocation overhead -- the fast path (size-class hit with sufficient remaining) is a single array index, a comparison, a subtraction, and a pointer advance. No locking is needed because the OCG subsystem runs single-threaded per compilation unit.

**Automatic class migration**: when a chunk's remainder shrinks below the current size class, the chunk is moved to the appropriate smaller bucket. This is a form of **adaptive binning** -- a fresh page starts in overflow, then gradually settles into smaller and smaller class buckets as it fills up. The result is that any size class can scavenge leftover space from pages originally allocated for larger objects.

**1 MB pages**: new pages are always 1 MB (`0x100000` = 1,048,576 bytes), allocated via `arena_alloc(NULL, ...)` which goes through the mmap fallback path. For allocations exceeding 1 MB, the page is sized to the exact request with zero remainder. The 1 MB granularity means the allocator obtains at most one page per ~1 million bytes of total allocation, keeping `mmap` syscall overhead negligible.

**Oversized allocations**: requests larger than 1 MB bypass both the size-class and overflow lookups entirely (`v4 > 0x100000` branches directly to page allocation). The resulting chunk has `remaining_size = 0` and is inserted into `free_list[0]` -- the exhausted-chunk sink. This handles the rare case of very large OCG objects (e.g., large constant data blocks) without disrupting the small-object fast path.

## Diagnostic Output

### Arena Statistics (sub_431770)

When invoked (typically at cleanup or via debug flags), `arena_dump_stats` prints per-arena memory accounting:

```
Page size: 0x10000 bytes
Nrof small block pages: 47
Nrof large block pages: 12
Total allocated:          3,276,800 bytes
Total available:          1,048,576 bytes
Total in use:             2,228,224 bytes
Longest free list size: 128
Average free list size: 4

@@ small block size  16: 0x4000/0x8000 (512/1024 blocks) 2 pages
@@ small block size  24: 0x6000/0xC000 (341/682 blocks) 2 pages
@@ large block page    0: 0x10000/0x10000, #=1 max=0x8000
```

The format includes per-size-class breakdowns showing allocated/total bytes, block counts, and page counts for small blocks, plus per-page usage for large blocks.

### OCG Memspace Statistics (sub_489140)

```
Memory space statistics for 'OCG mem space'
===========================================
Page size : 0x100000 bytes
Total allocated : 0x3200000 bytes
Total available : 0x1A0000 bytes
```

## Arena Lifecycle in main()

```
main()
  |
  +-- arena_create_named("nvlink option parser", NULL, 0)    // v338
  +-- arena_create_named("nvlink memory space", NULL, 0)     // v339
  +-- arena_context_swap(v339)          // set working arena
  |
  +-- [option parsing phase]            // allocations go to v338
  +-- [input reading phase]             // allocations go to v339
  |     +-- elfw_create()
  |           +-- arena_create_named("elfw memory space", v339, 0)
  |
  +-- [merge phase]                     // heavy allocation from v339
  +-- [layout/relocate/finalize]
  +-- [write output]
  |
  +-- arena_destroy(v339, 0)            // release all linker data
  +-- arena_destroy(v338, 0)            // release option data
```

The error state is tracked in the arena metadata byte at offset +1 (accessible via `sub_44F410`). This is checked throughout the pipeline to detect if any prior phase failed, enabling early exit without exceptions.

## Global Variables

| Address | Name | Purpose |
|---|---|---|
| `byte_2A5BAD0` | `debug_alloc_flag` | Non-zero enables debug tracking hash table for all allocations |
| `dword_2A5F384` | `emergency_page_count` | Number of reserved emergency pages available for OOM recovery |
| `qword_2A5F370` | `global_debug_hashtable` | Debug-mode allocation tracking hash table |
| `qword_2A5F368` | `global_debug_mutex` | Mutex protecting `global_debug_hashtable` |
| `dword_2A5F378` | `page_sequence_counter` | Monotonically increasing page ID (atomically incremented) |
| `qword_2A5F390` | `emergency_reserve_ptr` | Pointer to reserved emergency memory region |
| `qword_2A5F398` | `emergency_reserve_mutex` | Mutex protecting emergency reserve release |

## OOM Handling

When `arena_alloc` cannot satisfy a request, it calls `sub_45CAC0` (`arena_alloc_fail`), which produces:

```
An allocation failure occurred; heap memory may be exhausted.
```

This handler terminates the process. There is no graceful recovery path -- the linker assumes sufficient virtual memory is available for the linking workload. The emergency page reserve (`dword_2A5F384` / `qword_2A5F390`) provides a single retry opportunity for the null-arena mmap path only.

A separate handler at `sub_4B9E70` handles allocation failures in the OCG/ptxas subsystem with the same message, plus optional compound error reporting (`"Multiple errors:"`).

## Cross-References

- [Pipeline Overview](../pipeline/overview.md) -- arena creation/destruction in main flow
- [Entry Point](../pipeline/entry.md) -- arena lifecycle details in main()
- [Hash Tables](../linker/hash-tables.md) -- LinkerHash used for child-arena tracking and page trees
- [ELF Parsing](../input/elf-parsing.md) -- per-ELF arenas ("elfw memory space")
- [Serialization](../elf/serialization.md) -- arena-backed growable buffers
