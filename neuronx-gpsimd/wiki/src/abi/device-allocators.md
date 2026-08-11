# Device Memory Allocators

A custom op running on the GPSIMD management core reaches for device DRAM and
on-core staging memory through two thin allocator façades: **`neuron_hbm_*`**
(HBM scratch heap, used to host the switched-out Q7 stack) and
**`neuron_dataram_*`** (on-core dataram / TCM heap, used for the TensorStream
bounce buffer). Both are recovered, symbol-for-symbol, from `allocator.o` inside
`libneuroncustomop.a` (`aws-neuronx-gpsimd-customop-lib` v0.21.2.0). Neither is a
custom allocator: each is a ~40-instruction wrapper around the Cadence/Tensilica
**xmem** heap manager — a first-fit, address-sorted, coalescing free-list
allocator whose source ships in the toolchain tree as `xmem_heap.c` /
`xmem_mgr.h` and whose object code is linked into the custom-op library through
the imported symbols `xmem_heap_init`, `xmem_heap_alloc`, `xmem_heap_free`, and
`xmem_heap_get_free_space`.

This page documents (1) the two wrapper APIs and their exact ABI, (2) the xmem
heap-block and heap-manager layouts from DWARF, (3) the first-fit /
split-on-alloc / coalesce-on-free algorithm as annotated pseudocode naming the
real xmem symbols, (4) which subsystem owns each heap, and (5) where each heap
region is established at init.

> **Provenance.** All offsets, sizes, manglings, and `.bss` placements below were
> read directly from `allocator.o` (ELF32-Xtensa, DWARF v4): `nm`/`c++filt` for
> symbols, `readelf --debug-dump=info` for struct members, and the native
> `xtensa-elf-objdump` (core `ncore2gp`) for the wrapper disassembly. The xmem
> algorithm is transcribed from the toolchain-shipped `xmem_heap.c`, whose
> object is what the wrappers call. The page default is **HIGH × OBSERVED**; claims
> that depart from it carry an explicit
> `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tag.

---

## 1. The two wrapper APIs

`allocator.o` exports four families of allocator entry points. The two that a
custom op uses for device memory are `neuron_hbm_*` and `neuron_dataram_*`;
`_libc_heap_*` is a third heap that backs the on-core libc allocator, and
`c10::GetNeuronAllocator()` (in `NeuronAllocator.o`) is the *host-side* ATen
tensor allocator — a different concern entirely, not one of the device heaps.

### Recovered manglings and signatures

The C++ manglings pin the ABI exactly (verified with `nm allocator.o | c++filt`):

| Symbol (mangled) | Demangled signature | Heap |
|---|---|---|
| `_Z19neuron_hbm_allocatejPyj` | `neuron_hbm_allocate(unsigned int size, unsigned long long *out_addr, unsigned int align)` | HBM |
| `_Z21neuron_hbm_deallocatey` | `neuron_hbm_deallocate(unsigned long long addr)` | HBM |
| `_Z25neuron_hbm_get_alloc_sizeyPj` | `neuron_hbm_get_alloc_size(unsigned long long, unsigned int *)` | HBM |
| `_Z25init_neuron_hbm_allocatorv` | `init_neuron_hbm_allocator()` | HBM |
| `_Z23neuron_dataram_allocatejPPvj` | `neuron_dataram_allocate(unsigned int size, void **out_ptr, unsigned int align)` | dataram |
| `_Z25neuron_dataram_deallocatePv` | `neuron_dataram_deallocate(void *p)` | dataram |
| `_Z29neuron_dataram_max_free_spacePjj` | `neuron_dataram_max_free_space(unsigned int *, unsigned int)` | dataram |
| `_Z29init_neuron_dataram_allocatorv` | `init_neuron_dataram_allocator()` | dataram |

Confidence: **HIGH × OBSERVED** (manglings are unambiguous; `j`=`unsigned int`,
`y`=`unsigned long long`, `Pv`=`void*`, `PPv`=`void**`, `Py`=`unsigned long long*`).

> **NOTE — return-by-out-param, not by value.** Both allocate entry points return
> their result through a pointer argument, *not* in `a2`. `neuron_hbm_allocate`
> writes a 64-bit HBM device address through `out_addr` (a `uint64_t*`, because
> HBM addresses exceed 32 bits even though the management core is a 32-bit
> Xtensa); `neuron_dataram_allocate` writes a 32-bit on-core pointer through
> `out_ptr` (a `void**`, since dataram is in the core's own address space). The
> function *return value* (`a2`) is a status word: `0` on success, `-1` on
> allocation failure.

### `neuron_hbm_allocate` — disassembly walkthrough

```
000000b0 <neuron_hbm_allocate>:
  b0: entry  a1, 48
  b3: { const16 a10,0; movi a12,64; addi.a a13,a1,12; mov.a a11,a2 }  ; a12=align default 64,
                                                                       ; a13=&status (sp+12), a11=size
  c3: { const16 a4,0; nop; movnez a12,a4,a4 }     ; if caller align(a4)!=0, a12=caller align
  cb: const16 a10,0                               ; a10 = &_hbm_heap_mgr  (.bss+0x14)
  ce: const16 a4,0                                ; a4  = &xmem_heap_alloc  (reloc)
  d1: callx8  a4                                  ; xmem_heap_alloc(mgr, size, align, &status)
  d4: l32i.n  a4, a1, 12                          ; a4 = status
  d6: movi.n  a2, -1                              ; default return = -1 (fail)
  d8: beqz.n  a4, dc                              ; if status==XMEM_OK fall through to address calc
  da: retw.n                                      ; else return -1
000000dc:
  dc: const16 a2,0 / df: const16 a2,0             ; a2 = &_hbm_heap_mgr_base (.bss+0x10)  -> heap host base
  e2: const16 a4,0 / e5: const16 a4,0             ; a4 = &_hbm_scratch_base  (.bss+0x8)   -> uint64 HBM base
  e8: l32i.n  a2, a2, 0                           ; heap_base (32-bit host ptr into _hbm_heap_mgr region)
  ea: { l32i a5,a4,0; nop; sub a2,a10,a2 }        ; a5 = lo(hbm_scratch_base); a2 = alloc_ptr - heap_base (offset)
  f2: l32i.n  a4, a4, 4                           ; a4 = hi(hbm_scratch_base)
  f4: { movi a2,0; nop; add a15,a5,a2 }           ; a15 = lo(hbm_scratch_base) + offset
  fc: { s32i a15,a3,0; nop; saltu a5,a15,a5 }     ; store lo word of HBM addr to *out_addr; a5 = carry
 104: add.n   a4, a4, a5                          ; hi word + carry
 106: s32i.n  a4, a3, 4                           ; store hi word of HBM addr to *out_addr+4
 108: retw.n
```

The mechanism is: `xmem_heap_alloc` returns a *host-side* pointer into the
`_hbm_heap_mgr`'s pool (which is a window the core maps onto HBM scratch). The
wrapper converts that host pointer into the **64-bit HBM device address** by
subtracting the heap's host base (`_hbm_heap_mgr_base`, `.bss+0x10`) and adding
the device base (`_hbm_scratch_base`, a 64-bit value at `.bss+0x8`), with proper
carry propagation (`saltu`/`add` on the high word).

> **QUIRK — default 64-byte alignment.** `movi a12, 64` followed by
> `movnez a12, align, align` means: if the caller passes `align == 0`, the
> wrapper substitutes **64**; any non-zero caller alignment is used verbatim.
> This is why the stack-switch path can request the switched HBM stack with no
> explicit alignment and still get the 64-byte alignment it documents. (See
> [stack-switch.md](stack-switch.md).)

### `neuron_dataram_allocate` — disassembly walkthrough

```
000001d0 <neuron_dataram_allocate>:
 1d0: entry  a1, 48
 1d3: { const16 a10,0; movi a12,64; addi.a a13,a1,12; mov.a a11,a2 }  ; same shape: a12=align 64 default
 1e3: { const16 a4,0; nop; movnez a12,a4,a4 }    ; caller align override
 1eb: { const16 a10,0; movi a2,-1 }              ; a10 = &_dataram_heap_mgr (.bss+0x70); default ret = -1
 1f3: const16 a4,0                               ; a4 = &xmem_heap_alloc (reloc)
 1f6: callx8 a4                                  ; xmem_heap_alloc(&_dataram_heap_mgr, size, align, &status)
 1f9: l32i.n a4, a1, 12                          ; a4 = status
 1fb: s32i.n a10, a3, 0                          ; *out_ptr = allocated host pointer (a10 = return of alloc)
 1fd: moveqz a2, a4, a4                          ; if status==0, a2 = status(0); else leave a2 = -1
 200: retw.n
```

Because dataram is the core's own 32-bit address space, there is **no** HBM
address translation: the host pointer `xmem_heap_alloc` returns *is* the dataram
address, stored straight through `out_ptr` (`s32i.n a10, a3, 0`).

> **GOTCHA — `out_ptr` is written even on failure.** Note `s32i.n a10, a3, 0`
> executes *before* the status check. On `XMEM_ERR_ALLOC_FAILED`,
> `xmem_heap_alloc` returns `NULL`, so `*out_ptr` is set to `NULL` and the
> function returns `-1`. Callers must test the `-1` return, not the pointer value
> alone (it will be `NULL`, but relying on that is fragile if the heap ever yields
> a legitimately-zero host address).

### Failure semantics — no fallback

There is no secondary heap, no sbrk-style growth, and no retry. `xmem_heap_alloc`
either finds a free block big enough on the first-fit walk or hits the free-list
tail sentinel and returns `XMEM_ERR_ALLOC_FAILED` (`-100`). The wrapper turns
that into a `-1` status. The *caller* — `allocate_hbm_stack` in `stack_switch.o`,
or the TensorStream bounce-buffer setup — is what aborts: the custom-op runtime
treats a failed stack/bounce allocation as fatal. xmem itself also aborts
internally (via `XMEM_ABORT`) on contract violations such as a non-power-of-2
alignment (`XMEM_ERR_ILLEGAL_ALIGN`, `-99`) or `size == 0`
(`XMEM_ERR_INVALID_ARGS`, `-98`). Confidence: **HIGH × OBSERVED** for the status
codes (from `xmem.h`); **MED × INFERRED** for the "fatal in caller" framing.

---

## 2. xmem heap-block and heap-manager layout

xmem keeps **all bookkeeping out of band**: the allocatable pool holds only user
bytes, and the block descriptors live in a separate header array. This is why the
free-list is a clean address-sorted singly-linked list of fixed-size descriptors.

### `xmem_heap_block_struct` — the free/alloc list node

From `readelf --debug-dump=info allocator.o`, DIE `xmem_heap_block_struct`,
`DW_AT_byte_size : 16`:

| Offset | Field | Type | Meaning |
|---|---|---|---|
| `0x00` | `_next_block` | `xmem_heap_block_struct*` | next node in this list (free or alloc) |
| `0x04` | `_block_size` | `uint32_t` | size of the region in bytes |
| `0x08` | `_buffer` | `void*` | start of the region (unaligned) |
| `0x0c` | `_aligned_buffer` | `void*` | aligned pointer handed to the user |

Confidence: **HIGH × OBSERVED** (DWARF `data_member_location` 0/4/8/12,
byte_size 16). The block struct is **exactly 16 bytes**, a power of two, which
xmem exploits: `XMEM_HEAP_BLOCK_STRUCT_SIZE_LOG2 = 4` lets it convert a block
*address* back to a *bit-vector index* with a single right shift
`(block - &_blocks[0]) >> 4`.

> **NOTE — the block descriptor is not an inline header.** Unlike a classic
> Doug-Lea boundary-tag allocator, the `_buffer`/`_aligned_buffer`/`_block_size`
> live in the side `_blocks[]` array, **not** prepended to the user buffer. The
> user pointer returned is `_aligned_buffer`; `_buffer` is the (possibly smaller)
> true region start, and the gap `_aligned_buffer − _buffer` is alignment slack
> tracked in the manager's `_unused_bytes`. There is **no magic sentinel word at
> the head of each allocation** — the only sentinels are the list head/tail nodes
> described next.

### `xmem_heap_mgr_struct` — the manager (88 bytes)

From `xmem_mgr.h` (the header whose object code is linked here) and corroborated
by DWARF (`xmem_heap_mgr_struct`, `DW_AT_byte_size : 88`; `XMEM_HEAP_MGR_SIZE` is
defined as `88` in `xmem.h`):

| Offset | Field | Type | Meaning |
|---|---|---|---|
| `0x00` | `_lock` | `volatile void*` | user lock (set to `NULL` here — single management core) |
| `0x04` | `_buffer` | `void*` | base of the allocatable pool |
| `0x08` | `_buffer_size` | `uint32_t` | pool size in bytes |
| `0x0c` | `_free_bytes` | `uint32_t` | running free total |
| `0x10` | `_allocated_bytes` | `uint32_t` | running allocated total |
| `0x14` | `_unused_bytes` | `uint32_t` | alignment + header overhead |
| `0x18` | `_free_list_head` | `xmem_heap_block_t` (16B) | free-list head sentinel |
| `0x28` | `_free_list_tail` | `xmem_heap_block_t` (16B) | free-list tail sentinel |
| `0x38` | `_alloc_list_head` | `xmem_heap_block_t` (16B) | alloc-list head sentinel |
| `0x48` | `_blocks` | `xmem_heap_block_t*` | the side descriptor array |
| `0x4c` | `_num_blocks` | `uint32_t` | descriptor count |
| `0x50` | `_block_free_bitvec` | `uint32_t*` | bit per descriptor: 1=available, 0=in use |
| `0x54` | `_header_size` | `uint16_t` | bytes consumed by `_blocks[]` + bitvec |
| `0x56` | `_has_external_header` | `uint16_t` | 1 if bookkeeping is outside the pool |

Confidence: **HIGH × OBSERVED** for field names/order/sizes (from `xmem_mgr.h`
and the 88-byte DWARF size; the three embedded 16-byte sentinel blocks account
for the bulk of the struct).

The list sentinels are initialized so the walks never need a NULL check on the
common path:

* `_free_list_head._buffer = 0` and `_block_size = 0` (lowest possible address).
* `_free_list_tail._buffer = 0xffffffff`, `_block_size = 0xffffffff` (highest
  possible — every real block's address sorts before it, and every request size
  is `< 0xffffffff`, so the first-fit walk is guaranteed to terminate at the tail
  if nothing fits). Confidence: **HIGH × OBSERVED** (`xmem_heap_init`).

> **CORRECTION — `XMEM_HEAP_DEFAULT_NUM_BLOCKS` is 64, but these heaps use 32.**
> `xmem_mgr.h` defines `XMEM_HEAP_DEFAULT_NUM_BLOCKS (64)`, which applies *only*
> when no external header is supplied. Both Neuron heaps pass an **external
> header** and an explicit `num_blocks = 32` (the `movi a13, 32` in both init
> functions). So each heap supports at most **32 live descriptors**, i.e. ~16
> simultaneous splits before the splitter runs out of block headers (see the
> `avail_buf_idx >= _num_blocks` branch in §3). Do not assume 64. Confidence:
> **HIGH × OBSERVED**.

> **NOTE — the cache-line-padded alias.** `xmem.h` also defines a
> `xmem_heap_mgr_t` union whose first member is a `char _[...]` rounded up to a
> D-cache line. DWARF shows this alias (`xmem_heap_mgr_t`, member `_`, the
> manager's actual struct overlaid). For `ncore2gp` the manager objects in
> `.bss` are `0x58` (88) bytes each — i.e. the D-cache rounding is a no-op here
> (`_hbm_heap_mgr` and `_dataram_heap_mgr` are both exactly `0x58` long per
> `nm -S`), so the cache-coherency writebacks in `xmem_heap_*` are compiled out.
> Confidence: **HIGH × OBSERVED** (`nm -S allocator.o`).

---

## 3. The first-fit / split / coalesce algorithm

The xmem heap is a **first-fit, address-sorted, immediately-coalescing
free-list** allocator. The free list is kept sorted by `_buffer` address (lowest
first); allocation walks it front-to-back and takes the first block that fits;
free re-inserts in address order and merges with the physically-adjacent
neighbours. Below is the algorithm transcribed from `xmem_heap.c`, naming the
real symbols.

### `xmem_heap_alloc` (first-fit + split)

```c
/* xmem_heap.c : xmem_heap_alloc(mgr, size, align, *status) */
void *xmem_heap_alloc(xmem_heap_mgr_t *mgr, size_t size,
                      uint32_t align, xmem_status_t *status)
{
    /* contract: align is a power of two, size > 0; else XMEM_ERR_* via status */
    if (!(align && !(align & (align - 1)))) { *status = XMEM_ERR_ILLEGAL_ALIGN; return NULL; }
    if (size == 0)                          { *status = XMEM_ERR_INVALID_ARGS;  return NULL; }

    xmem_lock(mgr->_lock);                  /* NULL here -> no-op on single core */

    xmem_heap_block_t *prev = &mgr->_free_list_head;
    xmem_heap_block_t *curr =  mgr->_free_list_head._next_block;

    /* required size = requested size + alignment slack measured from THIS block */
    uint32_t new_size = xmem_heap_compute_new_size(curr, size, align);

    /* FIRST-FIT WALK: front-to-back over the address-sorted free list.
       new_size is recomputed per block because the alignment slack depends on
       each candidate's _buffer address. The tail sentinel (size 0xffffffff)
       guarantees termination. */
    while (curr->_block_size < new_size) {
        prev = curr;
        curr = curr->_next_block;
        new_size = xmem_heap_compute_new_size(curr, size, align);
    }
    if (curr == &mgr->_free_list_tail) {    /* walked off the end -> no fit */
        *status = XMEM_ERR_ALLOC_FAILED;    /* (-100) */
        xmem_unlock(mgr->_lock);
        return NULL;
    }

    /* place the user buffer at the top of the chosen region so the alignment
       slack lands at the FRONT (it stays attached to the allocated block). */
    curr->_aligned_buffer = curr->_buffer + new_size - size;
    void *r = curr->_aligned_buffer;
    mgr->_unused_bytes += (new_size - size);

    /* find a spare descriptor slot for the leftover (a free '1' bit) */
    uint32_t idx = xmem_find_leading_zero_one_count(mgr->_block_free_bitvec,
                                                    mgr->_num_blocks, 0, 0);

    /* SPLIT-ON-ALLOC: if the remainder is worth keeping (> XMEM_HEAP_MIN_ALLOC_SIZE
       == 4) AND a descriptor is free, carve a trailing free block. */
    if ((curr->_block_size - new_size) > XMEM_HEAP_MIN_ALLOC_SIZE
        && idx < mgr->_num_blocks) {
        xmem_toggle_bitvec(mgr->_block_free_bitvec, mgr->_num_blocks, idx, 1); /* mark in-use */
        xmem_heap_block_t *nb = &mgr->_blocks[idx];
        nb->_block_size     = curr->_block_size - new_size;
        nb->_buffer         = curr->_buffer + new_size;     /* leftover starts after the alloc */
        nb->_aligned_buffer = nb->_buffer;
        curr->_block_size   = new_size;                     /* shrink the chosen block */
        nb->_next_block     = curr->_next_block;            /* splice leftover into free list */
        prev->_next_block   = nb;                           /* in curr's old position (addr-sorted) */
    } else {
        /* remainder too small OR out of descriptors: hand over the whole block. */
        new_size = curr->_block_size;
        prev->_next_block = curr->_next_block;              /* unlink curr from free list */
    }

    /* push the now-allocated block onto the front of the alloc list (unsorted) */
    curr->_next_block = mgr->_alloc_list_head._next_block;
    mgr->_alloc_list_head._next_block = curr;
    mgr->_free_bytes      -= curr->_block_size;
    mgr->_allocated_bytes += curr->_block_size;

    xmem_unlock(mgr->_lock);
    *status = XMEM_OK;
    return r;                                               /* = _aligned_buffer */
}
```

Key helpers (from `xmem_misc.h`):

* `xmem_heap_compute_new_size(block, size, align)` aligns `block->_buffer`
  *upward* (`((p + align-1) >> log2(align)) << log2(align)` via
  `xmem_find_msbit(align)`) and returns `size + slack`. The slack is therefore
  computed **per candidate block**, which is why the first-fit walk recomputes
  `new_size` each iteration.
* `xmem_find_leading_zero_one_count(bitvec, n, start, 0)` scans the
  free-descriptor bit-vector for the first available slot (bit = 1).
* `xmem_toggle_bitvec(bitvec, n, idx, 1)` claims/releases a descriptor slot.

> **QUIRK — alignment slack stays with the allocation, not the free list.** The
> aligned user pointer is placed at `_buffer + new_size − size`, so the
> (`new_size − size`)-byte alignment gap sits at the *front* of the chosen block
> and is accounted as `_unused_bytes`, not returned to the free list. On free,
> that same gap is reclaimed (`_unused_bytes -= _buffer − _aligned_buffer`).

> **GOTCHA — descriptor exhaustion silently disables splitting.** When
> `xmem_find_leading_zero_one_count` returns `idx >= _num_blocks`, all 32
> descriptors are in use and xmem **cannot split**: it allocates the *entire*
> chosen block to the request, wasting the remainder until that block is freed.
> With only 32 descriptors this is a real ceiling. xmem logs but does not fail.

### `xmem_heap_free` → `xmem_heap_add_block_to_free_list` (coalesce)

Free first locates the allocation by matching `_aligned_buffer == p` on the
(unsorted) alloc list, unlinks it, then re-inserts it into the address-sorted
free list, merging with adjacent neighbours:

```c
/* xmem_heap.c : xmem_heap_free_internal(mgr, p, clear=0) */
xmem_status_t xmem_heap_free_internal(xmem_heap_mgr_t *mgr, void *p, int clear)
{
    if (p == NULL) return XMEM_ERR_PTR_OUT_OF_BOUNDS;       /* (-97) */
    xmem_lock(mgr->_lock);

    /* find the allocation: linear scan of the alloc list, match aligned ptr */
    xmem_heap_block_t *block, *prev;
    for (block = mgr->_alloc_list_head._next_block, prev = &mgr->_alloc_list_head;
         block != NULL; prev = block, block = block->_next_block)
        if (block->_aligned_buffer == p) break;
    if (!block) { xmem_unlock(mgr->_lock); return XMEM_ERR_PTR_OUT_OF_BOUNDS; }

    prev->_next_block = block->_next_block;                 /* unlink from alloc list */
    mgr->_free_bytes      += block->_block_size;
    mgr->_allocated_bytes -= block->_block_size;
    mgr->_unused_bytes    -= (block->_buffer - block->_aligned_buffer); /* reclaim slack */
    if (clear) memset(block->_buffer, 0, block->_block_size);

    xmem_heap_add_block_to_free_list(block, mgr);           /* sorted insert + coalesce */
    xmem_unlock(mgr->_lock);
    return XMEM_OK;
}

/* xmem_heap.c : xmem_heap_add_block_to_free_list(new_block, mgr) */
static void xmem_heap_add_block_to_free_list(xmem_heap_block_t *nb, xmem_heap_mgr_t *mgr)
{
    /* walk to the insertion point: first free block whose successor's buffer
       address is >= nb->_buffer (keeps the list address-sorted). */
    xmem_heap_block_t *block;
    for (block = &mgr->_free_list_head;
         (uintptr_t)block->_next_block->_buffer < (uintptr_t)nb->_buffer;
         block = block->_next_block)
        ;

    int merged_prev = 0, merged_next = 0;

    /* COALESCE LEFT: previous block physically abuts nb? */
    if (block != &mgr->_free_list_head &&
        (uintptr_t)block->_buffer + block->_block_size == (uintptr_t)nb->_buffer) {
        block->_block_size += nb->_block_size;
        xmem_toggle_bitvec(mgr->_block_free_bitvec, mgr->_num_blocks,
                           (nb - &mgr->_blocks[0]) >> XMEM_HEAP_BLOCK_STRUCT_SIZE_LOG2, 1);
        nb = block;                                         /* nb now subsumes prev */
        merged_prev = 1;
    }

    /* COALESCE RIGHT: nb (possibly already merged-left) abuts the next block? */
    if (block->_next_block != &mgr->_free_list_tail &&
        (uintptr_t)nb->_buffer + nb->_block_size == (uintptr_t)block->_next_block->_buffer) {
        xmem_toggle_bitvec(mgr->_block_free_bitvec, mgr->_num_blocks,
                           (block->_next_block - &mgr->_blocks[0]) >> XMEM_HEAP_BLOCK_STRUCT_SIZE_LOG2, 1);
        nb->_block_size += block->_next_block->_block_size;
        nb->_next_block  = block->_next_block->_next_block;
        if (!merged_prev) block->_next_block = nb;
        merged_next = 1;
    }

    if (!merged_prev && !merged_next) {                     /* isolated: plain sorted insert */
        nb->_next_block   = block->_next_block;
        block->_next_block = nb;
    }
}
```

So coalescing is **immediate and bidirectional**: a freed block merges with its
physical predecessor and/or successor in the same operation, reclaiming the
descriptor slots of any blocks it absorbs (via `xmem_toggle_bitvec(..., 1)`).
Address-sorted ordering is the invariant that makes neighbour coalescing cheap.
The walk itself is O(blocks) — fine for ≤32 descriptors.

> **NOTE — free matches on the *aligned* pointer.** Because alloc returns
> `_aligned_buffer`, free must compare against `_aligned_buffer`, not `_buffer`.
> Passing the raw region start to `neuron_*_deallocate` would miss the alloc-list
> entry and return `XMEM_ERR_PTR_OUT_OF_BOUNDS`.

---

## 4. Which heap each wrapper targets, and who consumes it

Both heaps are statically reserved as anonymous-namespace globals in
`allocator.o`'s `.bss`. The `nm -S allocator.o` map nails the offsets:

| `.bss` off | Size | Symbol | Role |
|---|---|---|---|
| `0x00` | 4 | `(anon)::_hbm_heap_header` | external-header pointer for the HBM heap |
| `0x08` | 8 | `(anon)::_hbm_scratch_base` | **64-bit** HBM device base address |
| `0x10` | 4 | `(anon)::_hbm_heap_mgr_base` | host base the HBM pool is mapped at |
| `0x14` | 0x58 | `(anon)::_hbm_heap_mgr` | the 88-byte HBM `xmem_heap_mgr` |
| `0x6c` | 4 | `(anon)::_dataram_heap_header` | external-header pointer for the dataram heap |
| `0x70` | 0x58 | `(anon)::_dataram_heap_mgr` | the 88-byte dataram `xmem_heap_mgr` |
| `0xc8` | 4 | `(anon)::_dataram_libc_heap_header` | header for the libc backing heap |
| `0xcc` | 0x58 | `(anon)::_dataram_libc_heap_mgr` | the 88-byte libc `xmem_heap_mgr` |

### HBM heap ← the switched-out Q7 stack

`neuron_hbm_allocate` drives `_hbm_heap_mgr` (`.bss+0x14`) over the HBM scratch
region exported as `extended_isa::sdk::hbm_scratch` /
`extended_isa::sdk::hbm_scratch_size` (both imported by `allocator.o`). The HBM
allocator's consumer is the **stack-switch** machinery: `stack_switch.o` imports
`neuron_hbm_allocate`, `neuron_hbm_deallocate`, and `init_neuron_hbm_allocator`,
and its `allocate_hbm_stack(uint, uint, uint)` calls `neuron_hbm_allocate` to
carve the HBM-resident stack that the Q7 switches to. The switched stack is
requested with default **64-byte** alignment, is capped at
**`MAX_STACK_SIZE = 0x400000`** (4 MiB), and defaults to **`STACK_SIZE = 4196`**
bytes. Confidence: **HIGH × OBSERVED** that `stack_switch.o` calls
`neuron_hbm_allocate` (import + `callx8` in `allocate_hbm_stack`); **CARRIED**
for the `MAX_STACK_SIZE`/`STACK_SIZE` numerics (owned by
[stack-switch.md](stack-switch.md) and the shared Part-7 pack).

### dataram / TCM heap ← the TensorStream bounce buffer

`neuron_dataram_allocate` drives `_dataram_heap_mgr` (`.bss+0x70`) over an on-core
dataram region. Its consumer is the **TensorStream / TCM** data-transfer path,
which needs a fixed **4 KiB** staging "bounce" buffer
(`BUFFER_SIZE_BYTES = 4096`) inside the on-core dataram window. The dataram
staging window is asserted as **`[0x80000, 0x90000)`** (64 KiB) — read directly
from `data_transfer.o`'s embedded assertion string
`data_transfer.cpp:160 dram_addr >= 0x80000 && dram_addr < 0x90000`. The bounce
buffer is allocated from the dataram heap and used as the C-memcpy / vector-memcpy
landing zone before DMA. Confidence: **HIGH × OBSERVED** for the `[0x80000,
0x90000)` window (assertion string); **CARRIED** for the 4 KiB
`BUFFER_SIZE_BYTES` and the bounce-buffer ownership (owned by
[tensorstream-tcm.md](tensorstream-tcm.md) /
[data-transfer-backends.md](data-transfer-backends.md)). The data-transfer
backend selector lives at `data_transfer_method_table` `@.data 0x200`
`{C_MEMCPY=0, VEC_MEMCPY=1, DMA=2}`.

### A third heap: the dataram-resident libc heap

`_init_libc_heap_allocator` builds a *separate* `xmem` heap
(`_dataram_libc_heap_mgr`, `.bss+0xcc`) that backs the on-core libc allocator
(`_libc_heap_allocate`/`_deallocate`/`_get_alloc_size`/`_max_free_space`). It is
also carved from dataram via `data_scratch_map`, but it is not one of the two
device-memory APIs a custom op calls directly — it is the C-runtime malloc
arena. Confidence: **HIGH × OBSERVED** (symbols + relocations).

> **NOTE — `data_scratch_map` is external.** Both `init_neuron_dataram_allocator`
> and `_init_libc_heap_allocator` reference an imported `data_scratch_map` symbol
> to obtain their dataram base; the HBM init instead uses
> `extended_isa::sdk::hbm_scratch`. `data_scratch_map` is *undefined* in
> `allocator.o` (resolved elsewhere in the firmware image), so this page does not
> claim its concrete address — only that it is the dataram region descriptor the
> inits consume. Confidence: **HIGH × OBSERVED** (the `U data_scratch_map`).

---

## 5. Heap base / extent / init

Each heap is established once, before any custom op runs, by its `init_*`
function. Both call `xmem_heap_init(mgr, pool, size, num_blocks=32, header)` with
an **external header** — meaning the 32-descriptor `_blocks[]` array plus its
1-word bit-vector live *outside* the allocatable pool (the
`_hbm_heap_header`/`_dataram_heap_header` pointers), so the entire pool is
available to callers.

### `init_neuron_hbm_allocator` (`allocator.o` @0x40)

```
  40: entry a1, 32
  43: { ... movi a15,0x228; movi a11,-1; movi a13,32 }   ; a13 = num_blocks = 32
  53: { ... slli a11,a11,31 }                            ; a11 = -1<<31 = 0x80000000 (sentinel/flag)
  67: l32i a2,a2,0                                        ; a2 = data_scratch_map[...] (dataram base for header)
  72: { l32i a3,a3,0; l32i a7,a3,4 }                      ; a3:a7 = hbm_scratch (64-bit base) and size
  7a: add a14, a2, a15                                    ; a14 = scratch + 0x228  -> HBM heap POOL base
  82: s32i.n a7,a4,0 / 84: s32i.n a3,a4,0                 ; store hbm_scratch_base (lo/hi) to .bss+0x8
  86: l32i a12,a6,0                                       ; a12 = pool size argument
  a0: s32i.n a11,a5,0                                     ; store host base -> _hbm_heap_mgr_base (.bss+0x10)
  a2: s32i a14,a4,0                                       ; store pool base into header slot
  a5: callx8 a2                                           ; xmem_heap_init(&_hbm_heap_mgr, pool, size, 32, header)
  a8: movi.n a2,-1 / aa: moveqz a2,a10,a10                ; return status (0 or -1)
```

The HBM heap's pool base is `hbm_scratch_base + 0x228` (the first `0x228` bytes
of HBM scratch are reserved ahead of the pool); `num_blocks = 32`. Confidence:
**HIGH × OBSERVED** for `num_blocks=32` and the `+0x228` pool offset; **MED ×
INFERRED** for the exact register-to-argument mapping of `pool`/`size` (the
constants are read straight from the encoding).

> **CORRECTION — `0x228` is a *pool offset*, not a header size.** It is tempting
> to read `movi a15, 0x228` as the xmem header size, but for `num_blocks = 32`
> with an external header the bookkeeping is only `16·32 + 4 = 0x204` bytes
> (`XMEM_HEAP_BLOCK_ARRAY_SIZE(32)`). `0x228` (552) is added to the *HBM scratch
> base* (`add a14, a2, a15`) to form the heap **pool base** — i.e. the heap
> region begins `0x228` bytes into HBM scratch, reserving that prefix for other
> firmware metadata. Do not conflate the two. Confidence: **HIGH × OBSERVED**
> (the `add` targets `hbm_scratch`, not the `size` arg).

### `init_neuron_dataram_allocator` (`allocator.o` @0x18c)

```
 18c: entry a1, 32
 18f: { ... movi a3,0x42c; movi a12,1; movi a13,32 }      ; a13 = num_blocks = 32
 1ab: l32i.n a2,a2,0                                       ; a2 = data_scratch_map base (dataram)
 1b0: { ... add a14,a2,a3; slli a12,a12,14; addmi a11,a2,0x3200 }
                                                          ; a14 = dataram_base + 0x42c -> pool base
                                                          ; a12 = 1<<14 = 0x4000 (16 KiB) -> pool size
                                                          ; a11 = dataram_base + 0x3200 -> header region
 1c3: s32i.n a14,a4,0                                      ; store pool base into header slot
 1c5: callx8 a2                                            ; xmem_heap_init(&_dataram_heap_mgr, pool, 0x4000, 32, hdr)
 1c8: movi.n a2,-1 / 1ca: moveqz a2,a10,a10                ; return status
```

The dataram heap is a **16 KiB** (`0x4000`) pool, based at
`data_scratch_map_base + 0x42c`, with its 32-descriptor external header placed at
`data_scratch_map_base + 0x3200`; `num_blocks = 32`. This 16 KiB pool comfortably
holds the 4 KiB TensorStream bounce buffer with room for other dataram-resident
staging. Confidence: **HIGH × OBSERVED** for `num_blocks=32`, pool size
`0x4000`, and the `+0x42c`/`+0x3200` offsets (all encoded as immediates);
**MED × INFERRED** that `0x4000` is exactly the `size` argument to
`xmem_heap_init` (register tracing).

> **GOTCHA — dataram heap pool vs. the `[0x80000,0x90000)` staging window.** The
> 16 KiB xmem dataram *pool* (`data_scratch_map + 0x42c`) is the allocator's
> region; the **`[0x80000, 0x90000)`** assertion in `data_transfer.cpp` is the
> hardware dataram *window* the DMA engine is allowed to address. They are
> related (the bounce buffer must land inside that window) but established by
> different code: `data_scratch_map` is resolved in firmware, the window bound is
> a static assertion in the transfer path. This page does not assert a numeric
> identity between `data_scratch_map`'s value and `0x80000`. Confidence: **MED ×
> INFERRED**.

---

## Cross-references

* [stack-switch.md](stack-switch.md) — `allocate_hbm_stack` / `switchStack`, the
  primary consumer of `neuron_hbm_allocate`; `MAX_STACK_SIZE` (4 MiB) and default
  `STACK_SIZE` (4196).
* [tensorstream-tcm.md](tensorstream-tcm.md) — the TCM/dataram staging window
  `[0x80000, 0x90000)` and the 4 KiB bounce buffer fed by
  `neuron_dataram_allocate`.
* [data-transfer-backends.md](data-transfer-backends.md) — the
  `data_transfer_method_table` (`C_MEMCPY=0, VEC_MEMCPY=1, DMA=2`) that copies
  through the dataram bounce buffer.
* [q7ptrtype.md](q7ptrtype.md) — how Q7 pointer types distinguish HBM-device vs.
  on-core dataram addresses, the two address spaces these heaps serve.

## Symbol & anchor index

* `allocator.o` exports: `neuron_hbm_allocate` (`_Z19neuron_hbm_allocatejPyj`,
  @0xb0), `neuron_hbm_deallocate` (@0x10c), `neuron_dataram_allocate`
  (`_Z23neuron_dataram_allocatejPPvj`, @0x1d0), `neuron_dataram_deallocate`
  (@0x204), `init_neuron_hbm_allocator` (@0x40),
  `init_neuron_dataram_allocator` (@0x18c).
* `.bss`: `_hbm_scratch_base` @0x8 (u64), `_hbm_heap_mgr_base` @0x10,
  `_hbm_heap_mgr` @0x14 (0x58), `_dataram_heap_mgr` @0x70 (0x58),
  `_dataram_libc_heap_mgr` @0xcc.
* xmem imports: `xmem_heap_init`, `xmem_heap_alloc`, `xmem_heap_free`,
  `xmem_heap_get_free_space`; helpers `xmem_heap_compute_new_size`,
  `xmem_heap_add_block_to_free_list`, `xmem_find_msbit`,
  `xmem_find_leading_zero_one_count`, `xmem_toggle_bitvec`.
* Struct DWARF: `xmem_heap_block_struct` (16B; `_next_block`@0, `_block_size`@4,
  `_buffer`@8, `_aligned_buffer`@12); `xmem_heap_mgr_struct` (88B,
  `XMEM_HEAP_MGR_SIZE`).
* Strings: `data_transfer.cpp:160 dram_addr >= 0x80000 && dram_addr < 0x90000`;
  source path `/opt/workspace/SundaCustomOpLibrary/custom_op/library/`.
* Constants: `XMEM_HEAP_DEFAULT_NUM_BLOCKS=64` (unused; heaps use 32),
  `XMEM_HEAP_MIN_ALLOC_SIZE=4`, `XMEM_HEAP_BLOCK_STRUCT_SIZE=16`,
  `XMEM_HEAP_BLOCK_STRUCT_SIZE_LOG2=4`; HBM pool offset `+0x228`; dataram pool
  `0x4000` @`+0x42c`, header @`+0x3200`.
