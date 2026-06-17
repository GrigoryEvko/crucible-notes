# Memory Pool and MC Handle Table

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The allocator and its three nested objects are read from `neuron_mempool.c` (953 lines) and `neuron_mempool.h` (239 lines); the opaque-handle table from `neuron_mc_handle.c` (260 lines) and `neuron_mc_handle.h` (86 lines). The source is read directly, not reverse-engineered — every constant is a `#define` or `module_param`, every field a struct member, every offset a declaration position. Struct byte offsets are **not** `pahole`-verified (kernel structs; layout depends on alignment and pointer width), so the layout tables give declaration order with a Confidence column, never a hard `+N`. Other driver versions renumber lines. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

This is the kernel-side physical-memory allocator for one Neuron device: it hands out spans of on-package HBM and host DMA-coherent DRAM, tracks every span in a per-device physical-address red-black tree, classifies each span by *lifespan* so the driver can bulk-reclaim on ioctl-exit / process-exit / device-detach, and publishes an opaque `u64` handle for any span that must cross to userspace. Three nested objects carry the model (header comment, `neuron_mempool.h:6-13`): a **`mem_chunk`/mc** is one allocated span; a **`neuron_mempool`/mp** is one backing arena — device HBM behind a Linux `gen_pool`, or host DRAM behind `dma_alloc_coherent` with a `gen_pool` fallback; a **`neuron_mempool_set`/mpset** is the per-device collection of all mp, embedded inline in `struct neuron_device`. The consumer of all this — the `MEM_ALLOC*`/`MEM_FREE`/`MEM_COPY*` ioctl handlers — is owned by [ioctl-mem](ioctl-mem.md); this page owns the engine those handlers call (`mc_alloc_align`, `mc_free`) and the struct layouts they drive.

The device side splits each HBM channel into a fixed `MAX_DRAM_CHANNELS(4) × MAX_DDR_REGIONS(4)` grid of independent `gen_pool` arenas (`neuron_mempool.h:70-71,85`), each optionally backed by a second *small-allocation* genpool (allocations `<= 512KB` prefer it, to keep small chunks from fragmenting the large arena, `neuron_mempool.c:65,718`). A genpool is a free-list over an integer address axis, and the Neuron driver maps device memory `va == pa` — but `lib/genalloc.c` cannot represent address `0`, and `pa == 0` is a legal HBM address. The driver works around this with a single bit: every device genpool's *virtual* axis is the physical address **OR'd with bit 63** (`GENPOOL_DEVMEM_BASE 0x1ull << 63`, `:32`), while the *physical* axis carries the true PA (`gen_pool_add_virt(.., start|bit63, start, size, ..)`, `:174`). The host side is different in kind: normal host allocations go straight through `dma_alloc_coherent`, and the four pre-reserved `mp_hrm` host genpools are only a fallback when that fails (`:688-705`).

The handle table (`neuron_mc_handle.c`) maps an opaque `u64` userspace handle to a `mem_chunk *` through a lazily-grown **2-level table** — 512 L1 slots × 8192 L2 entries = 4,194,304 handles per device (`:41-43`). The handle *is* the table index (the to/from-index functions are the identity, `:52-60`), and free slots form a LIFO free-list *threaded through the table cells themselves*: a cell holding a small integer is a free-list link to the next free index, a cell holding a value `>= NMCH_TBL_MAX_ENT` is a real `mem_chunk *`, and that threshold is the entire disambiguator (`nmch_l2_tbl_entry_valid`, `:62-66`). This page documents the alloc/free/carve call chains as annotated pseudocode, the three nested struct layouts, the bit-63 convention and *why* genalloc forces it, and the handle table's free-list/lazy-grow/poisoning internals.

For reimplementation, the contract is:

- **The mc/mp/mpset nesting** — one `mem_chunk` per span, joined to a PA-keyed rbtree *and* a per-lifespan list at birth; one `neuron_mempool` per arena (device-genpool or host); one `neuron_mempool_set` per device, holding the 4×4 device grid, the 4 host-reserve pools, the four lifespan lists, and the rbtree.
- **The device grid and small-pool strategy** — `MAX_DRAM_CHANNELS × MAX_DDR_REGIONS` genpools, each optionally split into a main arena plus a `<=512KB` small arena, with cross-pool retry on exhaustion.
- **The bit-63 devmem-base convention** — device genpool VA = `pa | (1<<63)`; reproduce it or genalloc rejects `pa == 0` and conflates the `va==pa` identity at address `0`.
- **The host path** — `dma_alloc_coherent(GFP_DMA32)` first, `mp_hrm` genpool fallback second, `pa |= pci_host_base` to tag the host-physical window.
- **The 2-level handle table** — identity handle↔index, LIFO free-list threaded through cells, lazy L2-table `kzalloc` on frontier advance, exhaustion/alloc-failure poisons the whole service (`free = 0` forever).
- **The lifespan GC** — `LOCAL → CUR_PROCESS[16] → ALL_PROCESS → DEVICE` contiguous enum, with refcount-survivor promotion to the next list and a `BUG_ON` leak assertion at device teardown.

| | |
|---|---|
| **Allocator entry** | `mc_alloc_align` (`neuron_mempool.c:846`) → `mc_alloc_internal` (`:631`) |
| **Free entry** | `mc_free` (`:860`) — refcount-- then full teardown at zero |
| **Pool construct / destroy** | `mpset_constructor` (`:451`) / `mpset_destructor` (`:499`) — called at PCI probe/remove |
| **Core object** | `struct mem_chunk` (`neuron_mempool.h:121`); `magic = MEMCHUNK_MAGIC 0xE1C2D3F4` (`:114`), `0xDEAD` on free (`:925`) |
| **Device grid** | `mp_device[MAX_DRAM_CHANNELS 4][MAX_DDR_REGIONS 4]` (`neuron_mempool.h:85`, `:70-71`) |
| **Host reserve** | `mp_hrm[MP_HOST_RESERVE_MEMORY_POOL_COUNT 4]` (`:87,76`); page sizes 2MB/1MB/512KB/256KB |
| **Devmem base** | `GENPOOL_DEVMEM_BASE 0x1ull<<63` (`neuron_mempool.c:32`) — OR'd onto device genpool VA (`:174,187`) |
| **Small-pool threshold** | `mempool_small_alloc_max 512*1024` (`:65`); small pool `mempool_small_pool_size 1<<30` (`:64`) |
| **Handle table** | `nd->nmch`, 2-level `512 × 8192 = 4,194,304` (`neuron_mc_handle.c:41-43`); handle == index (`:52-60`) |
| **PA rbtree** | `mpset.root` (`neuron_mempool.h:100`), keyed by `pa`, walked by `mpset_search_mc` (`neuron_mempool.c:532`) |
| **Confidence** | HIGH — all four files read in full; constants, fields, and call chains verbatim. Struct byte offsets MED (declaration order only) |

---

## 1. The Three Nested Objects

The allocator's data model is a strict three-level nest, declared in `neuron_mempool.h` and summarized in its own header comment (`:6-13`). A reimplementer must reproduce all three and their cross-pointers, because every operation walks between them: an `mc` knows its `mp` and `mpset`; the `mpset` owns the grid of `mp`s and the rbtree of every `mc`.

```text
neuron_device  (device.h)
└─ neuron_mempool_set  mpset            ── per-device; one mutex, one PA rbtree, four lifespan lists
   ├─ mp_device[4][4]   neuron_mempool  ── HBM grid: channel × region, each a gen_pool (+optional small)
   ├─ mp_hrm[4]         neuron_mempool  ── host reserve genpools (2MB/1MB/512KB/256KB pages), fallback only
   ├─ root              rb_root         ── every mem_chunk, keyed by pa
   └─ mc_lifespan_*_head                ── LOCAL / CUR_PROCESS[16] / ALL_PROCESS / DEVICE
        └─ mem_chunk  mc  (one per span) ── joined to root AND one lifespan list at alloc
```

### `struct mem_chunk` — the span handle

One `mem_chunk` is `kmalloc`'d per allocation (`:668`), stamped at `:800-813`, and `kfree`'d at `:928`. It is the object every other layer holds a pointer to. Fields, in declaration order (`neuron_mempool.h:121-152`):

| Field | Type | Meaning | Confidence |
|---|---|---|---|
| `magic` | `u32` | `MEMCHUNK_MAGIC 0xE1C2D3F4` at alloc (`:800`); `0xDEAD` poison at free (`:925`). The ioctl layer's use-after-free guard | HIGH |
| `node` | `struct rb_node` | link in `mpset->root`, the pa-keyed tree (inserted `:817`) | HIGH |
| `pa` | `phys_addr_t` | physical address. Device: true PA from `gen_pool_virt_to_phys`. Host: PA `| pci_host_base` (`:707`) | HIGH |
| `va` | `void *` | kernel VA. Device: genpool VA = `pa | bit63`. Host: `dma_alloc_coherent` VA | HIGH |
| `size` | `u64` | span size, rounded up to `PAGE_SIZE` at alloc (`:650`) | HIGH |
| `mp` | `struct neuron_mempool *` | owning pool. **NULL** for the host `dma_alloc_coherent` fast path (no genpool involved) | HIGH |
| `mpset` | `struct neuron_mempool_set *` | back-pointer to the device set | HIGH |
| `gen_pool` | `struct gen_pool *` | the exact genpool the VA came from (main / small / hrm); the free target | HIGH |
| `dram_channel` | `u32` | HBM channel `0..3` | HIGH |
| `dram_region` | `u32` | DRAM region `0..3` | HIGH |
| `nc_id` | `u32` | NeuronCore index, validated `< MAX_NC_PER_DEVICE(8)` (`:657`) | HIGH |
| `mc_handle` | `neuron_mc_handle_t` (`u64`) | opaque handle; `NMCH_INVALID_HANDLE(0)` until the ioctl layer publishes one | HIGH |
| `alloc_type` | `mem_alloc_category_t` | sysfs counter category; also selects the scratchpad bounds rule (`mc_access_is_within_bounds`) | HIGH |
| `mem_location` | `enum mem_location` | `MEM_LOC_HOST(1)` / `MEM_LOC_DEVICE(2)` | HIGH |
| `pid` | `pid_t` | `task_tgid_nr(current)` at alloc (`:809`) — owning process for per-pid accounting | HIGH |
| `ref_count` | `int` | init `1` (`:810`); `mc_free` decrements, releases at `0` | HIGH |
| `lifespan` | `enum mc_lifespan` | which reclaim list this chunk lives on | HIGH |
| `lifespan_list` | `struct list_head` | link in that per-lifespan list | HIGH |
| `caller_pc` | `void *` | `__builtin_return_address(0)` (`:812`) — leak diagnostics name the allocating caller | HIGH |
| `model_start_tracker` | `struct {bool; u32;}` | model-start detection (`:116-119`); set by higher layers, not the allocator | MED |

### `struct neuron_mempool` — one arena

A single arena, used two ways. For device memory it is a `gen_pool` (plus optional `gen_pool_small`); for host reserve it is a `gen_pool` backed by an array of `dma_alloc_coherent` pages. Fields (`neuron_mempool.h:42-67`):

| Field | Type | Meaning | Confidence |
|---|---|---|---|
| `name[32]` | `char[]` | `"device mempool [%d:%d]"` (channel:region, `:197`) or `"host mempool [%d]"` (page_size, `:280`) | HIGH |
| `initialized` | `bool` | gate for `mp_destroy_gen_pool` (`:305`) | HIGH |
| `mpset` | `struct neuron_mempool_set *` | parent set | HIGH |
| `mem_location` | `enum mem_location` | `MEM_LOC_DEVICE` (`:164`) or `MEM_LOC_HOST` (`:250`) | HIGH |
| `dram_channel` / `dram_region` | `u32` | grid coordinates (device pools only) | HIGH |
| `gen_pool` | `struct gen_pool *` | main genpool | HIGH |
| `gen_pool_small` | `struct gen_pool *` | small-alloc genpool; **may be NULL** (`:53`, host pools always NULL `:252`) | HIGH |
| `main_pool_end_addr` | `u64` | `start + main_pool_size` (`:167`); also the small genpool's start, and the contiguous-scratchpad ceiling | HIGH |
| `small_pool_size` | `size_t` | bytes carved for the small genpool (`0` if disabled) | HIGH |
| `region_size` | `size_t` | total bytes of the initial region (`:198,281`) | HIGH |
| `allocated_size` | `size_t` | running total handed out from this arena (`:782`, `:893/903`) | HIGH |
| `page_size` / `page_requested_count` / `page_count` | `u32` | host-pool page bookkeeping (`:261,277-278`) | HIGH |
| `page_va_array` / `page_pa_array` | `void**` / `dma_addr_t*` | host-pool backing-page KVAs / PAs (freed in `mp_destroy_hrm_pool`) | HIGH |
| `scratchpad_size` | `u64` | only for `CONTIGUOUS_SCRATCHPAD_DEVICE`; tracks the backwards-growing scratchpad frontier | HIGH |

### `struct neuron_mempool_set` — the per-device set

Embedded inline in `struct neuron_device`, constructed at PCI probe. It owns the grid, the host pools, the four lifespan lists, the PA rbtree, and the per-process mmap trees (the mmap trees are *declared* here but managed by `neuron_mmap.c` — boundary). Fields (`neuron_mempool.h:78-105`):

| Field | Type | Meaning | Confidence |
|---|---|---|---|
| `lock` | `struct mutex` | serializes alloc / free / counter updates (`:675,855,869`) | HIGH |
| `nd` | `struct neuron_device *` | back-pointer | HIGH |
| `mp_device_num_regions` / `num_channels` | `u32` | grid extents set by DHAL; `num_regions==1` ⇒ shared-DRAM mode forces region 0 (`:661`) | HIGH |
| `mp_device[4][4]` | `neuron_mempool[][]` | the HBM channel×region grid | HIGH |
| `mp_hrm[4]` | `neuron_mempool[]` | host reserve pools, descending page size | HIGH |
| `mc_lifespan_local_head` | `struct list_head` | `MC_LIFESPAN_LOCAL` chunks | HIGH |
| `mc_lifespan_cur_process_head[16]` | `struct list_head[]` | per-process-slot lists, `NEURON_MAX_PROCESS_PER_DEVICE(16)` | HIGH |
| `mc_lifespan_all_process_head` | `struct list_head` | `MC_LIFESPAN_ALL_PROCESS` chunks | HIGH |
| `mc_lifespan_device_head` | `struct list_head` | `MC_LIFESPAN_DEVICE` chunks | HIGH |
| `host_mem_size` / `device_mem_size` | `u64` | running usage stats (`:821,824`) | HIGH |
| `pdev` | `void *` | `pci_dev->dev`, the `dma_*_coherent` device argument | HIGH |
| `root` | `struct rb_root` | every `mem_chunk`, keyed by `pa` | HIGH |
| `rblock` | `rwlock_t` | guards `root` (write on insert/erase, read on dump) | HIGH |
| `mmap_root[16]` | `struct rb_root[]` | per-process mmap'd-VA trees — declared here, **owned by `neuron_mmap.c`** | HIGH |
| `rbmmaplock` | `rwlock_t` | guards `mmap_root` | HIGH |

> **NOTE —** the four lifespan lists are not a refinement of the rbtree — they are an orthogonal index. The rbtree answers "which chunk owns physical address `pa`?" (used by mmap and DMA-target validation); the lifespan lists answer "which chunks must I free when *this* scope ends?" Every `mem_chunk` is on exactly one of each at all times between alloc and free.

---

## 2. The Device Pool — Grid, Small Pool, and the Bit-63 Convention

### Grid construction

`mpset_init_device_pools` (`:378`) asks the DHAL for each channel's DRAM base and size (`mpset_set_dram_and_mpset_info`, `:386` — boundary), divides each channel's span into `mp_device_num_regions` equal regions, and builds one `neuron_mempool` per `(channel, region)` cell via `mp_init_device_mem` (`:392`). After the grid is built it carves a 16MB firmware-reserved block off the top of every region (§5).

```c
// neuron_mempool.c:136 — build one device-HBM arena over [start_addr, start_addr+pool_size)
function mp_init_device_mem(mp, mpset, start_addr, pool_size, channel, region):
    small = mempool_small_pool_size                       // :140  default 1GB
    if small >= pool_size:               small = 0        // :143  pool too small to split
    if small % mempool_min_alloc_size:   small = 0        // :147  must be a clean multiple
    if !ndhal.small_pool_supported:      small = 0        // :151  arch gates it
    main_size  = pool_size - small                        // :154
    small_start = start_addr + main_size                  // :155  small pool sits ABOVE main

    BUG_ON(mempool_min_alloc_size < PAGE_SIZE)            // :158  refuse to load if misconfigured
    BUG_ON(mempool_min_alloc_size % PAGE_SIZE != 0)      // :159

    mp.main_pool_end_addr = start_addr + main_size        // :167  end of main = start of small
    mp.gen_pool = gen_pool_create(ilog2(mp_min_alloc_size), -1)         // :170  min-order genpool
    gen_pool_add_virt(mp.gen_pool,
                      start_addr | GENPOOL_DEVMEM_BASE,   // :174  VA axis = PA | bit63
                      start_addr,                          //        PA axis = true PA
                      main_size, -1)
    if small > 0:                                          // :180
        mp.gen_pool_small = gen_pool_create(ilog2(mp_min_alloc_size), -1)
        gen_pool_add_virt(mp.gen_pool_small,
                          small_start | GENPOOL_DEVMEM_BASE, small_start, small, -1)   // :187
    mp.region_size = pool_size                            // :198
    mp.initialized = 1
```

### Why bit 63 is forced

`lib/genalloc.c` is an address allocator over an unsigned integer axis. The Neuron driver makes a deliberate simplification for device memory: the genpool's *virtual* address equals the *physical* address (`va == pa`), so a chunk's PA can be recovered with a cheap `gen_pool_virt_to_phys` and no separate mapping table. Two facts collide with that:

1. `genalloc` treats a returned address of `0` as the allocation-failure sentinel — it cannot hand out address `0`.
2. `pa == 0` is a *legal* HBM physical address (it is the base of channel 0, region 0 before the carveout).

So a `va == pa == 0` chunk would be indistinguishable from "out of memory." The fix (comment `:27-31`) is to offset the entire *virtual* axis by `GENPOOL_DEVMEM_BASE = 0x1ull << 63`: the genpool is told its VA range starts at `start_addr | bit63` while its PA range starts at the true `start_addr` (`gen_pool_add_virt`'s separate virt/phys arguments, `:174`). Every device VA therefore has bit 63 set and can never be `0`, while `gen_pool_virt_to_phys` strips back to the true PA. The chunk records the bit-63 VA in `mc->va` and the clean PA in `mc->pa` (`:737,760`).

> **QUIRK —** the bit-63 base lives **only** on the genpool VA axis and on `mc->va`. `mc->pa` is always the clean physical address, and the rbtree is keyed on `mc->pa`, so a reimplementer must not OR bit 63 into anything the rbtree or DMA layer sees. Conversely, `gen_pool_free` (`:902`) and `gen_pool_virt_to_phys` (`:737`) take the *VA* — i.e. the bit-63 value in `mc->va` — so freeing with the clean PA would corrupt the genpool. The two axes are not interchangeable: `mc->va` is the genpool key, `mc->pa` is everyone else's key. Host pools never set the bit (their VA is a real kernel VA from `dma_alloc_coherent`, `:268`).

### Small pool vs. main pool

Each device cell may carry a second genpool sized `mempool_small_pool_size` (default 1GB, `:64`) carved off the top of the region. Allocations `<= mempool_small_alloc_max` (default 512KB, `:65`) prefer the small pool; larger ones prefer the main pool; and either path retries on the *other* pool when its first choice is exhausted (`:718-724`, retry at `:750/771`). The intent is fragmentation control: many small, short-lived chunks churn the small arena without punching holes in the large contiguous region that big tensor allocations need. The split is disabled (`gen_pool_small = NULL`) when the small size is invalid, not a clean multiple of `mempool_min_alloc_size`, or unsupported by the arch (`:143-153`).

---

## 3. The Allocator — `mc_alloc_internal`

`mc_alloc_align` (`:846`) is a one-line wrapper over `mc_alloc_internal` (`:631`), the single allocation path for both host and device memory. The shape is: validate → `kmalloc` the `mem_chunk` → carve from host or device under the mpset mutex → stamp → join rbtree + lifespan list → bump counters. Three device sub-paths (contiguous-scratchpad, aligned, plain) differ only in which genpool algorithm they call.

```c
// neuron_mempool.c:631 — THE allocator. ioctl-mem calls this via mc_alloc_align (:846)
function mc_alloc_internal(nd, lifespan, size, align, location, channel, region, nc_id, mem_type, *result):
    *result = NULL
    if size > INT64_MAX:                       return -EINVAL    // :647  overflow guard
    size = roundup(size, PAGE_SIZE)                              // :650  mmap-friendly granularity
    if channel >= ndhal.dram_channels:         return -EINVAL    // :651
    if nc_id  >= MAX_NC_PER_DEVICE:            return -EINVAL    // :657  (8)
    if mpset.mp_device_num_regions == 1: region = 0             // :661  shared-DRAM mode
    if region >= mpset.mp_device_num_regions:  return -EINVAL    // :664

    mc = kmalloc(sizeof(mem_chunk)); if !mc: return -ENOMEM      // :668
    *result = mc; memset(mc, 0)                                 // :672  published early; reset to NULL on error (:841)

    mutex_lock(&mpset.lock)                                      // :675
    if location == MEM_LOC_HOST:
        if align: { ret = -EINVAL; goto exit }                  // :677  aligned host alloc unsupported
        mc.va = dma_alloc_coherent(mpset.pdev, size, &mc.pa, GFP_KERNEL|GFP_DMA32)   // :684  FAST PATH
        if mc.va == NULL:                                        // :688  fallback to reserve pools
            for i in 0..3:                                       // :690
                if (MP_HOST_PAGE_SIZE_MIN << i) < size: continue // :692  pool page too small
                mp = &mpset.mp_hrm[i]
                mc.va = gen_pool_dma_alloc(mp.gen_pool, size, &mc.pa)   // :695
                mc.gen_pool = mp.gen_pool                        // :696  (set before va-check — see CORRECTION)
                if mc.va: break                                  // :697
        if mc.va: mc.pa |= ndhal.pci_host_base                  // :707  tag host-physical window
    else:                                                        // MEM_LOC_DEVICE
        mp = &mpset.mp_device[channel][region]                  // :711
        if !mp.gen_pool: { ret = -ENOMEM; goto exit }           // :712
        // pick pool: small alloc -> small genpool, with the other as the retry alt
        if mp.gen_pool_small && size <= mempool_small_alloc_max:  // :718
            pool = mp.gen_pool_small;  alt = mp.gen_pool
        else:
            pool = mp.gen_pool;        alt = mp.gen_pool_small

        if mem_type == CONTIGUOUS_SCRATCHPAD_DEVICE:            // :726  sub-path A
            pool = mp.gen_pool; alt = NULL                       //       force main, no retry
            off = get_offset_for_scratchpad_alloc(mp, size)     // :729  grows BACKWARDS from main end
            mc.va = gen_pool_alloc_algo(pool, size, gen_pool_fixed_alloc, {off})   // :730
            mc.pa = gen_pool_virt_to_phys(pool, mc.va)          // :737  strip bit63 -> true PA
            mc.gen_pool = pool; mp.scratchpad_size += size      // :738
        else if align > PAGE_SIZE:                              // :740  sub-path B (aligned)
            mc.va = gen_pool_alloc_algo(pool, size, gen_pool_first_fit_align, {align})   // :748
            if mc.va == NULL && alt: mc.va = gen_pool_alloc_algo(alt, ..); mc.gen_pool = alt   // :750-756
            else: mc.gen_pool = pool
            mc.pa = gen_pool_virt_to_phys(mc.gen_pool, mc.va)   // :760
            if (align-1) & mc.pa != 0 || mc.va == NULL:         // :761  verify HW alignment
                if mc.va: gen_pool_free(mc.gen_pool, mc.va, size)   // :764  give it back
                ret = -ENOMEM; goto exit
        else:                                                   // :769  sub-path C (plain)
            mc.va = gen_pool_dma_alloc(pool, size, &mc.pa)      // :770
            if mc.va == NULL && alt: mc.va = gen_pool_dma_alloc(alt, ..); mc.gen_pool = alt   // :771-776
            else: mc.gen_pool = pool
        if mc.va: mp.allocated_size += size                     // :782

    if mc.va == NULL: { ret = -ENOMEM; goto exit }             // :795

    // ---- stamp the chunk (:800-813) ----
    mc.magic = MEMCHUNK_MAGIC; mc.mpset = mpset; mc.mp = mp; mc.size = size
    mc.mc_handle = NMCH_INVALID_HANDLE                          // :804  handle minted LATER, by ioctl-mem
    mc.mem_location = location; mc.dram_channel = channel; mc.dram_region = region
    mc.nc_id = nc_id; mc.pid = task_tgid_nr(current); mc.ref_count = 1
    mc.lifespan = lifespan; mc.caller_pc = __builtin_return_address(0); mc.alloc_type = mem_type
    mc_add_to_lifespan_list(mc)                                 // :814  join the reclaim list

    write_lock(&mpset.rblock); mc_insert_node(&mpset.root, mc); write_unlock   // :816  join PA tree

    mpset.{host,device}_mem_size += size                       // :821/824
    nsysfsmetric_inc_counter(HOST_MEM | DEVICE_MEM, ..)        // :822/825  [boundary: sysfs]
    counter = mem_alloc_type_to_sysfs_counter[mem_type]        // :832  per-category counter
    nsysfsmetric_inc_counter(counter, ..)                      // :833
    npid_add_allocated_memory(nd, location, size)              // :835  [boundary: per-pid]
exit:
    mutex_unlock(&mpset.lock)
    if ret: kfree(mc); *result = NULL                          // :839-842
    return ret
```

> **NOTE —** the allocator deliberately does **not** mint a handle. `mc->mc_handle` is left `NMCH_INVALID_HANDLE` (`:804`); the 2-level table is touched only when the ioctl layer first needs to hand the chunk to userspace (`ncdev_mem_chunk_to_mem_handle` → `nmch_handle_alloc`, owned by [ioctl-mem §1](ioctl-mem.md#handle--mc-resolution)). A reimplementer should keep allocation and publication separate: most kernel-internal chunks (DMA rings, NQ buffers, the 16MB carveout) never get a handle at all.

> **CORRECTION (MP-host-genpool) —** in the host fallback loop, `mc->gen_pool = mp->gen_pool` is assigned (`:696`) *before* the `if (mc->va)` success check (`:697`). If the last reserve pool also fails, `mc->gen_pool` is left pointing at a pool the chunk was never taken from. This is harmless in practice: a fully-failed host alloc leaves `mc->mp == NULL`, and `mc_free`'s host branch keys on `mc->mp` (`:891`) — `NULL` routes to `dma_free_coherent`, never to `gen_pool_free` on the stale pointer. The stale `gen_pool` is dead state on an object that is about to be `kfree`'d on the error path. A reimplementation should still assign `mc->gen_pool` only on the success branch.

### The three device sub-paths

| Sub-path | Trigger | Genpool algorithm | PA source | Retry on `alt` |
|---|---|---|---|---|
| **Contiguous scratchpad** | `mem_type == CONTIGUOUS_SCRATCHPAD_DEVICE` (`:726`) | `gen_pool_fixed_alloc` at a *backwards-growing* offset (`get_offset_for_scratchpad_alloc`, `:617`) | `gen_pool_virt_to_phys` (`:737`) | **No** (main pool only, `alt = NULL`) |
| **Aligned** | `align > PAGE_SIZE` (`:740`) | `gen_pool_first_fit_align` (`:748`) | `gen_pool_virt_to_phys` (`:760`) | Yes (`:750`), then re-verify `(align-1) & pa == 0` (`:761`) |
| **Plain** | otherwise (`:769`) | `gen_pool_dma_alloc` (`:770`) | returned by `gen_pool_dma_alloc` | Yes (`:771`) |

The contiguous-scratchpad sub-path is the sharp one: it grows a single contiguous region *downward* from `main_pool_end_addr`, so each new scratchpad alloc sits immediately below the previous one (`region_size - small_pool_size - scratchpad_size - alloc_size`, `:628`). This makes the scratchpad a LIFO stack — `mc_free` checks that the freed chunk is the topmost page (`pa + scratchpad_size == main_pool_end_addr`, `:911`) and only logs (cannot return an error) if a middle page is freed out of order. The bounds-check special case in `mc_access_is_within_bounds` (`neuron_mempool.h:228-229`) lets a scratchpad chunk's access window span the *whole* contiguous scratchpad, not just `mc->size` — see [ioctl-mem §3](ioctl-mem.md#mem_buf_copy-1045--the-representative-consume-handler).

---

## 4. Free, Refcount, and the Lifespan GC

`mc_free` (`:860`) is refcount-gated: it decrements and returns early if references remain, otherwise it tears the chunk fully down. The teardown order matters — handle slot first (so a racing lookup fails cleanly), then rbtree, then counters, then the actual memory return, then lifespan unlink and magic poison.

```c
// neuron_mempool.c:860 — release one reference; full teardown at zero
function mc_free(*mcp):
    mc = *mcp
    BUG_ON(mc == NULL); BUG_ON(mc->magic != MEMCHUNK_MAGIC)     // :865-866  catch double-free / corruption
    mutex_lock(&mpset.lock)
    if --mc->ref_count > 0: { unlock; return }                 // :870  still referenced

    if mc->mc_handle != NMCH_INVALID_HANDLE:
        nmch_handle_free(mpset->nd, mc->mc_handle)              // :877  release table slot FIRST
    write_lock(&mpset.rblock); mc_remove_node(&mpset.root, mc); write_unlock   // :880  leave PA tree
    nsysfsmetric_dec_counter(category, ..)                      // :889  [boundary]

    if mc->mem_location == MEM_LOC_HOST:
        if mc->mp: gen_pool_free(mc->gen_pool, mc->va, mc->size); mc->mp->allocated_size -= size  // :891  reserve pool
        else:      dma_free_coherent(pdev, size, mc->va, mc->pa & ~pci_host_base)                 // :895  fast path
        mpset.host_mem_size -= size
    else if mc->mem_location == MEM_LOC_DEVICE:
        gen_pool_free(mc->gen_pool, mc->va, mc->size)          // :902  bit63 VA -> genpool
        mp->allocated_size -= size; mpset.device_mem_size -= size
    else: BUG()                                                // :907

    if mc->alloc_type == CONTIGUOUS_SCRATCHPAD_DEVICE:         // :910  LIFO discipline
        if mc->pa + mp->scratchpad_size != mp->main_pool_end_addr:
            pr_err("freeing page ... expected ... to be freed first")   // :915  out-of-order free, log only
        mp->scratchpad_size -= size                            // :919
    npid_dec_allocated_memory(..)                              // :922  [boundary]
    *mcp = NULL                                                // :923  poison the caller's pointer
    mc_remove_from_lifespan_list(mc)                           // :924
    mc->magic = 0xDEAD                                         // :925  UAF tripwire
    mutex_unlock(&mpset.lock); kfree(mc)                       // :928
```

### The lifespan ladder

The four lifespans form a *contiguous* enum on purpose (`MC_LIFESPAN_LOCAL=1 → CUR_PROCESS=2 → ALL_PROCESS=3 → DEVICE=4`, `neuron_mempool.h:107-112`). `mpset_free_expired_mc(mpset, lifespan)` frees a list and *promotes survivors* to `lifespan+1` (`:609-614`) — a chunk whose refcount is still nonzero when its scope ends is moved one rung up the ladder rather than leaked, by indexing the next head with arithmetic on the enum. The promotion target for `DEVICE` would be `lifespan+1 == 5`, which `mpset_get_lifespan_head` returns as `NULL`; `mpset_free_lifespan_list` reads that NULL as "force-free" — it sets `ref_count = 1` and frees regardless (`:599-604`), logging a leak. So device teardown is the terminal rung that cannot promote.

`mpset_destructor` (`:499`) runs the ladder at device detach: it reparents any still-attached process's `CUR_PROCESS` list onto the `DEVICE` list (`:511`), frees `ALL_PROCESS` then `DEVICE` (`:514-515`), and asserts the whole device is empty with `mpset_verify_all_mc_freed` → `BUG_ON(count != 0)` (`:448`). Only then does it destroy the host and device genpools.

> **GOTCHA —** `mpset_search_mc` (`:532`) walks the rbtree and `mc_handle_find` (`neuron_mc_handle.c:93`) walks the handle table **without taking `rblock` / `nmch.lock`** — the header note admits the table is "mutex protected for insert/remove but not for search" (`:21`). A concurrent `nmch_handle_alloc` that advances the frontier and `kzalloc`s a fresh L2 table, or a concurrent `rb_insert_color`/`rb_erase` rotation, races a lockless reader; this is a genuine lock-relaxed tree walk, not RCU. The exposure is bounded because per-process mem ioctls serialize through the owning `nd` and the attach model, but a reimplementer must not assume the search side is safe from an arbitrary context. Flagged B1; the consumer-side framing is on [ioctl-mem §1](ioctl-mem.md#handle--mc-resolution).

---

## 5. The 16MB Firmware Carveout

`mpset_block_carveout_regions` (`:329`) runs immediately after the device grid is built. For each `(channel, region)` it allocates a `MEMPOOL_CARVEOUT_SIZE = 0x1000000 (16MB)` chunk (`:319`) with `MC_LIFESPAN_DEVICE`, then asserts the chunk landed at offset 0 of the region (`mc->pa == start_addr`, `:358`). Because the device genpool is first-fit, the first allocation from a fresh region *is* its base — so this reserves the top of the region in a roundabout way, then bumps `device_dram_effective_base_addr[channel] += 16MB` (`:364`) so every later allocation reports addresses above the firmware block.

The comment (`:336-346`) explains *why* the driver reserves by allocation instead of simply shrinking the region's start address: shrinking broke aligned allocation on 4.x kernels (a `lib/genalloc.c` bug fixed in 5.x by commit `52fbf113`). Allocating-to-reserve sidesteps the kernel-version dependency. The carveout chunk is a permanent `MC_LIFESPAN_DEVICE` resident — it is freed only at device teardown.

> **QUIRK —** the carveout is *physically* the top 16MB of HBM (the firmware-owned region, `:318`) but is reserved by allocating the *first* chunk of each region, which first-fit places at the region base. The reconciliation is `device_dram_effective_base_addr[channel] += 16MB` (`:364`): the driver shifts the *reported* base up by 16MB so the effective usable window excludes the firmware block, while the reserved `mem_chunk` itself occupies the genpool's offset 0. A reimplementer copying the "carve out by allocation" idiom must replicate the effective-base bump, or every later address will be 16MB low.

---

## 6. The 2-Level MC Handle Table

The handle table (`neuron_mc_handle.c`) is the bridge between userspace's opaque `u64` and the kernel's `mem_chunk *`. It is per-device (`nd->nmch`), 2-level, lazily grown, and threads its free-list through the table cells themselves to avoid a parallel free-list array.

### Geometry and the identity handle

```text
handle == index  (nmch_handle_to_idx / idx_to_handle are identity, :52-60)

idx ──► L1 idx = idx / NMCH_L2_TBL_SZ      (idx / 8192,  NMCH_IDX_2L1_IDX :49)
        L2 idx = idx % NMCH_L2_TBL_SZ      (idx % 8192,  NMCH_IDX_2L2_IDX :50)

l1_tbl[512] ──► l2_tbl[8192] ──► nmch_map_ent_t { mc | value }

NMCH_TBL_MAX_ENT = 512 * 8192 = 4,194,304          (:43)  "512K per core on a Trn2" (:19)
idx 0 = NMCH_INVALID_IDX (reserved)                (:47)  free == 0 ⇒ service down
```

The disambiguator is the entire trick: a cell is a `union { struct mem_chunk *mc; uint64_t value; }` (`neuron_mc_handle.h:23-26`). A real `mem_chunk *` is a kernel heap pointer, which on every supported target is numerically `>= 4,194,304`; a free-list link is an index `< 4,194,304`. So `nmch_l2_tbl_entry_valid` is just `value >= NMCH_TBL_MAX_ENT` (`:62-66`) — no tag bit, no separate metadata. A forged or stale handle that lands on a free cell is rejected because a free-list link can never be a valid pointer.

### Lookup — lockless, three guards

```c
// neuron_mc_handle.c:93 — handle -> mem_chunk*, NO lock (insert/free are locked, search is not)
function mc_handle_find(nd, mc_handle):
    idx = mc_handle                                            // :97  identity
    if nmch_service_is_down(nd): return NULL                   // :99  nd->nmch.free == 0 (poisoned)
    if !nmch_idx_valid(idx):     return NULL                   // :103 0 < idx < 4,194,304
    l2 = nd->nmch.l1_tbl[idx / 8192]                           // :107 L1 deref
    if l2 == NULL:               return NULL                   // :108 L2 table never allocated
    ent = l2[idx % 8192]                                        // :113 L2 deref
    if !nmch_l2_tbl_entry_valid(ent): return NULL              // :115 ent.value < MAX_ENT ⇒ free-list link
    return ent.mc                                              // :119
```

### Mint — pop the LIFO free-list, lazily grow

```c
// neuron_mc_handle.c:122 — publish mc, return its index as handle (mutex-guarded)
function nmch_handle_alloc(nd, mc, *mc_handle):
    mutex_lock(&nd->nmch.lock)
    if nmch_service_is_down(nd): { ret = -ENOENT; goto done }  // :132
    idx = nd->nmch.free                                        // :137  free-list head
    pent = &l1_tbl[idx/8192][idx%8192]                         // :141
    nextfree = pent->value                                     // :143

    if nextfree == NMCH_INVALID_IDX:                           // :146  this cell is a FRESH frontier cell
        nextfree = idx + 1                                     // :149  next free is one past
        if nextfree >= NMCH_TBL_MAX_ENT:                       // :151  EXHAUSTED -> poison service
            nmch_service_set_down(nd); ret = -ENOENT; goto done   // :153  free = 0 forever
        if l1_tbl[nextfree/8192] == NULL:                      // :161  next cell needs a new L2 table
            if nmch_l2tbl_alloc(&l1_tbl[nextfree/8192]):       // :162  kzalloc 8192-entry table
                nmch_service_set_down(nd); ret = -ENOENT; goto done   // :163  alloc fail -> poison

    nd->nmch.free = nextfree                                   // :171  advance free-list head
    pent->mc = mc                                              // :174  store the pointer (overwrites the link)
    *mc_handle = idx; ret = 0                                  // :175
done:
    mutex_unlock(&nd->nmch.lock); return ret
```

The free-list is **threaded through the cells**: a free cell's `value` is the index of the *next* free cell, in LIFO order. Two cases on pop. A cell that was previously freed holds a real link (its old `value`), and `nextfree` is simply that link. A *fresh* frontier cell — one never used before — holds `0` (`kzalloc`), which signals "the next free index is `idx+1`, and you must lazily allocate its L2 table if it crosses into a new L1 bucket" (`:146-167`). This is why the table grows on demand: L2 tables are `kzalloc`'d one-ahead of the frontier, never all 512 up front (only `l2_tbl[0]` is allocated at init, `:235`).

### Free — push back onto the LIFO list

```c
// neuron_mc_handle.c:183 — return idx to the free-list (mutex-guarded)
function nmch_handle_free(nd, mc_handle):
    mutex_lock(&nd->nmch.lock)
    if nmch_service_is_down(nd):  { ret = -ENOENT; goto done } // :192
    idx = mc_handle
    if !nmch_idx_valid(idx):      { ret = -ENOENT; goto done } // :200
    l2 = nd->nmch.l1_tbl[idx/8192]
    if l2 == NULL:                { ret = -ENOENT; goto done } // :207
    pent = &l2[idx%8192]
    if !nmch_l2_tbl_entry_valid(*pent):  { ret = -ENOENT; goto done }  // :216  double-free guard
    pent->value = nd->nmch.free                               // :222  link old head into this cell
    nd->nmch.free = idx                                       // :223  this cell becomes the new head
    ret = 0
done:
    mutex_unlock(&nd->nmch.lock); return ret
```

### Init, cleanup, and poisoning

`nmch_handle_init` (`:231`) `kzalloc`s the 512-pointer L1 table plus L2 table `[0]`, inits the mutex, and sets `free = 1` (index 0 is reserved invalid). `nmch_handle_cleanup` (`:246`) frees every non-NULL L2 table then the L1 table, and sets the service down. The **poisoning** model is the table's most important property for a reimplementer: any unrecoverable error — exhaustion (`:151`), an L2 `kzalloc` failure during grow (`:162`), or init failure (`:237`) — calls `nmch_service_set_down`, which sets `free = 0`. Since `0` is `NMCH_INVALID_IDX`, *every* subsequent `find`/`alloc`/`free` short-circuits to failure (`nmch_service_is_down`, `:73`). The table is one-shot-fatal by design: once any internal invariant is at risk, the whole handle service is dead for the device's lifetime rather than risk corrupting the free-list.

> **QUIRK —** the table never shrinks and never reuses index 0. `free` is both the free-list head *and* the poison sentinel — its `0` value means "down," not "first slot is free." This dual use is why index 0 is permanently reserved (`:47`) and why init sets `free = 1`. A reimplementer who lets a real allocation use index 0, or who clears `free` to 0 as an "empty" marker, silently poisons the service. The security note (`:28-29`) is the other constraint: error paths log the *handle* but never the table *value*, because a leaked free-list link or pointer would let a caller probe table internals — keep that discipline.

---

## Function Map

| Function | file:line | Role | Confidence |
|---|---|---|---|
| `mc_alloc_align` | `neuron_mempool.c:846` | public alloc entry; thin wrapper → `mc_alloc_internal` | HIGH |
| `mc_alloc_internal` | `:631` | the allocator: validate, carve host/device, stamp, join rbtree+lifespan, bump counters | HIGH |
| `mc_free` | `:860` | refcount--; at 0: release handle, rbtree, counters, memory, lifespan; poison magic | HIGH |
| `mc_inc_refcount` | `:852` | mutex-guarded `++ref_count` | HIGH |
| `mpset_search_mc` | `:532` | PA→`mem_chunk` rbtree lookup (lockless) | HIGH |
| `mc_dump_all_chunks` | `:931` | read-locked rbtree walk; copies `{pa,size,mem_type}` per channel | HIGH |
| `mpset_constructor` | `:451` | build mpset: mutex, lists, mmap trees, 4 host pools, device grid | HIGH |
| `mpset_destructor` | `:499` | reparent, free ALL_PROCESS+DEVICE, `BUG_ON` leak, destroy pools | HIGH |
| `mpset_free_expired_mc` | `:609` | free a lifespan list; promote survivors to `lifespan+1` | HIGH |
| `mp_init_device_mem` | `:136` | build one device cell (main genpool + optional small, bit-63 VA) | HIGH |
| `mp_init_hrm_pool` | `:241` | build one host reserve genpool over `dma_alloc_coherent` pages | HIGH |
| `mpset_init_device_pools` | `:378` | DHAL DRAM query → grid build → carveout | HIGH |
| `mpset_block_carveout_regions` | `:329` | reserve 16MB firmware block per region; bump effective base | HIGH |
| `get_offset_for_scratchpad_alloc` | `:617` | backwards-growing fixed offset for contiguous scratchpad | HIGH |
| `mc_insert_node` / `mc_remove_node` | `:89` / `:117` | pa-keyed rbtree insert (no dedup) / `rb_erase` | HIGH |
| `mc_handle_find` | `neuron_mc_handle.c:93` | handle→`mem_chunk*` 2-level lookup (lockless) | HIGH |
| `nmch_handle_alloc` | `:122` | pop LIFO free-list, lazily grow L2, store mc (mutex) | HIGH |
| `nmch_handle_free` | `:183` | push idx back onto LIFO free-list (mutex) | HIGH |
| `nmch_handle_init` / `_cleanup` | `:231` / `:246` | alloc/free L1+L2 tables; `free=1` init, poison on teardown | HIGH |
| `nmch_l2_tbl_entry_valid` | `:62` | `value >= NMCH_TBL_MAX_ENT` ⇒ real pointer vs. free-list link | HIGH |

---

## Cross-References

- [Memory IOCTL Handlers](ioctl-mem.md) — the userspace front-end this page's engine serves: `MEM_ALLOC*`/`MEM_FREE`/`MEM_COPY*` call `mc_alloc_align`/`mc_free`, and own the handle *publication* (`ncdev_mem_chunk_to_mem_handle` → `nmch_handle_alloc`) that this allocator deliberately skips
- [Char Device, fops and mmap](cdev-mmap.md) — the `mmap` pgoff-cookie decode (`pgoff << PAGE_SHIFT → pa → mpset_search_mc`) that consumes this page's PA-keyed rbtree as its lookup index
- [DMA Op Layer and the Completion-Marker Model](dma-op-layer.md) — the DMA consumer that allocates rings/buffers via `mc_alloc_align` and validates copy targets through `mpset_search_mc` + `mc_access_is_within_bounds`
- [Kernel Datastore](datastore.md) — another `mc_alloc_align` consumer: per-pid datastore slabs are host `mem_chunk`s on the lifespan ladder
- [Memory Hierarchy, BAR Layout and State Buffer](../arch/memory-hierarchy.md) — the hardware view of the HBM channel/region geometry this allocator carves into the 4×4 genpool grid, and the firmware-reserved top region the 16MB carveout protects
