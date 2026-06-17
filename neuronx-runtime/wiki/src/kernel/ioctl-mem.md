# Memory IOCTL Handlers

> *All `file:line` citations on this page are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The handler bodies are read from `neuron_cdev.c` (4043 lines), the arg structs from `neuron_ioctl.h` (876 lines), and the kernel bookkeeping objects (`mem_chunk`, the 2-level handle table, `mc_access_is_within_bounds`, `nmmap_offset`) from `neuron_mempool.{c,h}`, `neuron_mc_handle.{c,h}`, and `neuron_mmap.c`. The source is read directly, not reverse-engineered; every constant and offset is a `#define`, a struct field, or a literal line. Other driver versions renumber lines and add/remove commands. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

The memory `ioctl` family is the userspace allocator front-end for Neuron device HBM and host DMA-coherent memory: `MEM_ALLOC*`, `MEM_FREE`, `MEM_COPY*`, `MEM_BUF_COPY*`, `MEMSET*`, the `GET_INFO`/`GET_PA`/`GET_EXTENDED_INFO`/`MEM_MC_GET_INFO` queries, and the `MEM_BUF_ZEROCOPY64*` fast path. Every allocation produces one `struct mem_chunk` (`neuron_mempool.h:121`) — the kernel's bookkeeping object for a contiguous span of device or host memory — and that chunk is tracked in **two** structures at once: a per-device red-black tree keyed by physical address (`mpset.root`, walked by `mpset_search_mc`) for mmap and PA→chunk lookups, and a per-device **2-level handle table** (`nd->nmch`, `neuron_mc_handle.c`) that maps an opaque `u64` "mem handle" back to the `mem_chunk *`. The rbtree and the table's internals are owned in full by [mempool-handles](mempool-handles.md); this page documents how the `ioctl` surface *drives* both structures.

The pivot a reimplementer must internalize is the **opaque `u64` mem handle**. `MEM_ALLOC*` returns it; every later `COPY`/`MEMSET`/`FREE`/`GET_INFO` ioctl passes it back as the token that re-resolves the chunk. It is *not* a pointer and *not* an address — it is the chunk's index into the handle table, allocated lazily on first publication (`ncdev_mem_chunk_to_mem_handle`, `:65`) and validated on every consume (`ncdev_mem_handle_to_mem_chunk`, `:75`, which additionally checks `mc->magic == MEMCHUNK_MAGIC`). Distinct from the handle is the **mmap pgoff cookie**: the value a `GET_INFO`/`MEM_MC_GET_INFO` query returns in its `mmap_offset` field is the chunk's *physical address* verbatim (`nmmap_offset(mc)` returns `mc->pa`, `neuron_mmap.c:237-240`), so userspace `mmap(/dev/neuronN, pgoff = pa >> PAGE_SHIFT)` decodes straight back to the PA-keyed rbtree. Handle and cookie are two different keys into the same `mem_chunk`, derived two different ways — conflating them is the family's central trap.

This page documents, per handler: the arg struct it `copy_from_user`s, the validation it performs (handle resolution, `mc_access_is_within_bounds`), and the dispatch it issues. It does **not** re-derive the dispatch mechanism (the three-gate model, `_IOC_NR`/`_IOC_SIZE` overload resolution, the `*64` sub-gate slip — owned by [ioctl-dispatch](ioctl-dispatch.md)) nor the allocator/genpool/lifespan engine behind `mc_alloc_align`/`mc_free` (owned by [mempool-handles](mempool-handles.md)). The DMA primitives every copy path bottoms out in (`ndma_memcpy_*`) belong to [dma-op-layer](dma-op-layer.md).

For reimplementation, the contract is:

- **The two-key model** — a `mem_chunk` is reachable by `u64` handle (table index, lazy-allocated) and by physical address (rbtree, also the mmap cookie). Reproduce both lookups and the `MEMCHUNK_MAGIC` validation that guards the handle path.
- **The handle↔mc resolution** — `ncdev_mem_chunk_to_mem_handle` (`mc → handle`, lazy alloc) and `ncdev_mem_handle_to_mem_chunk` (`handle → mc`, validate), and the 2-level table they call into.
- **The per-handler arg/validate/dispatch shape** — each handler copies a fixed struct, resolves handles, bounds-checks copy ranges with `mc_access_is_within_bounds`, then calls the allocator or the DMA layer; the size-overloaded families demux the struct width first.
- **The lifecycle** — allocation stamps `MC_LIFESPAN_CUR_PROCESS`; explicit `MEM_FREE` or process-teardown auto-free reclaims it; transient bounce buffers are `MC_LIFESPAN_LOCAL` and freed before the handler returns.

| | |
|---|---|
| **Handler region** | `neuron_cdev.c:416-1426`, `:2299` (`RESOURCE_MMAP_INFO`), `:2390` (`DUMP_MEM_CHUNKS`) |
| **Core object** | `struct mem_chunk` (`neuron_mempool.h:121`); `magic = MEMCHUNK_MAGIC 0xE1C2D3F4` (`:114`), `0xDEAD` on free (`neuron_mempool.c:925`) |
| **Two trackers** | PA-keyed rbtree `mpset.root` (mmap / PA lookup) + 2-level handle table `nd->nmch` (`u64` handle → `mc *`) |
| **handle → mc** | `ncdev_mem_handle_to_mem_chunk` `:75` → `mc_handle_find` `:93` (+ `MEMCHUNK_MAGIC` check `:79`) |
| **mc → handle** | `ncdev_mem_chunk_to_mem_handle` `:65` → `nmch_handle_alloc` `:122` (lazy) |
| **mmap cookie** | `nmmap_offset(mc) == mc->pa` (`neuron_mmap.c:237-240`); `mmap` decodes `pgoff << PAGE_SHIFT → pa` |
| **Bounds gate** | `mc_access_is_within_bounds` (`neuron_mempool.h:225`, overflow-safe) — every copy/memset |
| **Alloc / free** | `mc_alloc_align` `:846` / `mc_free` `:860` (`neuron_mempool.c`) — [mempool-handles](mempool-handles.md) |
| **Lifespan** | `MC_LIFESPAN_CUR_PROCESS` for ioctl allocs; `MC_LIFESPAN_LOCAL` for bounce buffers |
| **Confidence** | HIGH — every handler body, arg struct, and bookkeeping object read verbatim from the shipped source |

---

## 1. The Two Keys to a `mem_chunk`

A `mem_chunk` is born in `mc_alloc_align` and immediately joins the PA-keyed rbtree (`mc_insert_node(&mpset->root)`), but it does **not** yet have a handle: `mc_alloc_internal` stamps `mc->mc_handle = NMCH_INVALID_HANDLE` (the allocator does not touch the handle table — that is the `ioctl` layer's job, [mempool-handles](mempool-handles.md)). The chunk is therefore reachable by physical address the instant it exists, but reachable by handle only once an `ioctl` publishes it. The two keys exist for two different consumers.

**Key 1 — the `u64` handle (table index).** The handle is the userspace token. It is an index into a per-device 2-level table (`nd->nmch`, geometry `NMCH_L1_TBL_SZ 512 × NMCH_L2_TBL_SZ 8192 = 4,194,304` entries, `neuron_mc_handle.c:41-43`); `nmch_handle_to_idx`/`idx_to_handle` are the *identity* function (`:52-60`), so the handle *is* the index. Index `0` is reserved invalid (`NMCH_INVALID_IDX`, `:47`; `NMCH_INVALID_HANDLE 0`, `neuron_mc_handle.h:14`). A handle resolves to a `mem_chunk *` through one L1 deref and one L2 deref. Crucially, the L2 cell is a `union { struct mem_chunk *mc; uint64_t value; }` (`neuron_mc_handle.h:23-26`): a cell holding a *small* integer is a free-list link, a cell holding a value `>= NMCH_TBL_MAX_ENT` is a real pointer — the disambiguator is `nmch_l2_tbl_entry_valid` (`value >= NMCH_TBL_MAX_ENT`, `:62-66`). This is why a forged or stale handle that lands on a free cell is rejected: a free-list link is never a valid pointer.

**Key 2 — the physical address (rbtree + mmap cookie).** `mpset.root` is a red-black tree of *every* `mem_chunk` keyed by `pa`; `mpset_search_mc(mpset, pa)` returns the chunk whose `[pa, pa+size)` span contains the query. The `ioctl` family exposes this key through the `mmap_offset` out-field of the info queries, and the value it returns is the PA itself: `nmmap_offset(mc)` is a one-line `return mc->pa` (`neuron_mmap.c:237-240`). Userspace then calls `mmap(/dev/neuronN, …, pgoff = mmap_offset >> PAGE_SHIFT)`, and `ncdev_mmap` reverses the shift to recover `pa` and finds the chunk via the same rbtree. So the cookie round-trips through the page-offset argument of `mmap(2)`.

> **QUIRK —** the handle and the cookie are *both* keys to the same `mem_chunk`, but they are not interchangeable and they are produced by different calls. `MEM_ALLOC*` hands back the **handle** (an opaque index); `MEM_GET_INFO`/`MEM_MC_GET_INFO` hand back the **cookie** (`mc->pa`, for `mmap`). A reimplementer who feeds a handle to `mmap`'s `pgoff`, or a `pa>>PAGE_SHIFT` cookie to `MEM_FREE`, gets garbage: `MEM_FREE` resolves through the handle table (`mc_handle_find`), `mmap` resolves through the rbtree (`mpset_search_mc`). The only handler that bridges them is `MEM_MC_GET_INFO_V2`, which takes a `pa` (`[in]`) and returns *both* the size/offset *and* a freshly-allocated handle (`:664`) — the explicit PA→handle bridge.

### Handle → mc resolution

Every consuming handler (`COPY`, `MEMSET`, `FREE`, `GET_*`) re-resolves the caller's handle through one helper, which layers a `MEMCHUNK_MAGIC` sanity check on top of the table lookup:

```c
// neuron_cdev.c:75 — handle → mem_chunk, with magic validation
function ncdev_mem_handle_to_mem_chunk(nd, mh):
    mc = mc_handle_find(nd, mh)                     // :77  2-level table lookup (lockless read)
    if mc == NULL || mc->magic != MEMCHUNK_MAGIC:   // :79  reject free/forged/freed handles
        pr_err("invalid memory handle %llx", mh)    // :80
        return NULL
    return mc                                       // :83

// neuron_mc_handle.c:93 — the table walk (no lock taken: insert/free are locked, search is not)
function mc_handle_find(nd, mc_handle):
    idx = mc_handle                                 // :97  handle == index (identity)
    if nmch_service_is_down(nd): return NULL        // :99  nd->nmch.free == 0 → table poisoned
    if !nmch_idx_valid(idx): return NULL            // :103 0 < idx < 4,194,304
    l2_tbl = nd->nmch.l1_tbl[idx / 8192]            // :107 L1 deref (NMCH_IDX_2L1_IDX)
    if l2_tbl == NULL: return NULL                  // :108 L2 table never allocated
    ent = l2_tbl[idx % 8192]                         // :113 L2 deref (NMCH_IDX_2L2_IDX)
    if !nmch_l2_tbl_entry_valid(ent):               // :115 ent.value >= NMCH_TBL_MAX_ENT → real ptr
        return NULL                                  // :116 else it is a free-list link
    return ent.mc                                   // :119
```

The mirror direction publishes a handle lazily — the table slot is consumed only when a chunk first needs to cross to userspace, not at allocation:

```c
// neuron_cdev.c:65 — mc → handle, allocate on first publication
function ncdev_mem_chunk_to_mem_handle(nd, mc, *mh):
    if mc->mc_handle == NMCH_INVALID_HANDLE:        // :68  not yet published
        nmch_handle_alloc(nd, mc, &mc->mc_handle)   // :69  pop free-list head, lazily kzalloc next L2 tbl, mutex-guarded (:122)
    *mh = (u64)mc->mc_handle                          // :71
```

> **GOTCHA —** `mc_handle_find` takes **no lock** (`neuron_mc_handle.c:93`; only `nmch_handle_alloc`/`_free` hold `nd->nmch.lock`). A concurrent `nmch_handle_alloc` that advances the free-list frontier and `kzalloc`s a fresh L2 table races this lockless reader; the `mpset.root` rbtree walk in `mpset_search_mc` is likewise lock-relaxed against concurrent `rb_insert_color`/`rb_erase`. The race is bounded in practice because per-process mem ioctls serialize through the owning `nd` and the attach model, but a reimplementer must not assume the search side is safe to call from an arbitrary context. Full analysis (flagged B1) is on [mempool-handles](mempool-handles.md).

---

## 2. Allocation — `MEM_ALLOC` and the `V2` Family

### Purpose

`MEM_ALLOC*` carves a `mem_chunk` from host or device memory and returns its `u64` handle. There are two handlers: the legacy `ncdev_mem_alloc` (`:416`, the fixed `mem_alloc` struct, `nr 21`) and `ncdev_mem_alloc_libnrt` (`:456`, the size-overloaded `V2`/`V2MT`/`V2MT64` family, all `nr 102`). Both reduce to one `mc_alloc_align` call plus handle publication.

### Algorithm

The two handlers share their tail; the only differences are the struct width and whether `mem_type` and `align` come from the caller. `ncdev_mem_alloc` (legacy) hard-codes `align = 0` and derives `mem_type` from `host_memory`; `ncdev_mem_alloc_libnrt` demuxes three struct widths by exact `cmd ==`, taking `align` and (for the `MT` variants) a user-supplied `mem_type`:

```c
// neuron_cdev.c:456 — the V2 family; legacy :416 is the same tail with align=0
function ncdev_mem_alloc_libnrt(nd, cmd, param):
    static_assert(MEM_ALLOC_V2 != V2MT != V2MT64)            // :458-460 constants must differ
    if cmd == MEM_ALLOC_V2:                                  // :476  8-byte ptr-form macro
        copy_from_user(&arg, param, sizeof(mem_alloc_v2))    // :478
        mem_type = host_memory ? UNKNOWN_HOST : UNKNOWN_DEVICE // :490-494  driver picks category
    else if cmd == MEM_ALLOC_V2MT:                           // :495  +mem_type field
        copy_from_user(&arg, param, sizeof(mem_alloc_v2_mem_type)) // :497
        mem_type = arg.mem_type                               // :508  caller picks category
    else if cmd == MEM_ALLOC_V2MT64:                         // :510  +trailing pad
        copy_from_user(&arg, param, sizeof(..._mem_type64))   // :512
        mem_type = arg.mem_type                               // :523  pad never read
    else: return -EINVAL                                     // :525-526

    location = host_memory ? MEM_LOC_HOST : MEM_LOC_DEVICE   // :529-532
    mc_alloc_align(nd, MC_LIFESPAN_CUR_PROCESS, size, align, // :533  → mempool.c:846 (genpool / dma_alloc engine)
                   location, dram_channel, dram_region, nc_id, mem_type, &mc)
    trace_ioctl_mem_alloc(nd, mc)                            // :537

    ncdev_mem_chunk_to_mem_handle(nd, mc, &mh)               // :539  lazily mint the handle
    copy_to_user(arg.mem_handle, &mh, sizeof(mc))            // :541  ← see CORRECTION below
    if (copy failed):
        mc_free(&mc)                                          // :545  roll back alloc + handle
        return ret
```

> **CORRECTION (MEM-alloc-copyout) —** the `copy_to_user` that returns the handle uses `sizeof(mc)`, **not** `sizeof(mh)` (`:446` in the legacy handler, `:541` in `libnrt`). `mc` is a `struct mem_chunk *`, so `sizeof(mc) == 8` on LP64, which *happens* to equal `sizeof(u64)` — the eight bytes written are correct on every supported target. It is a latent footgun, not a live bug: the intent is "write the 8-byte handle," and it does, but the expression names the pointer's size by coincidence rather than the value's. A reimplementer should write `sizeof(mh)`; an auditor porting to ILP32 or CHERI, where `sizeof(void*) != sizeof(u64)`, must flag it.

> **QUIRK —** `MEM_ALLOC_V2MT64` is byte-identical to `V2MT` plus a trailing `__u32 pad` (`neuron_ioctl.h:52-62`) that the handler **never reads** — `mem_type` and `mem_handle` are taken, `pad` is ignored (`:523-524`). The pad exists only to make `sizeof` (hence `_IOC_SIZE`, hence the full `cmd` constant) differ, so `libnrt` can feature-probe whether the driver accepts the wider command — i.e. whether it has `>=4GB` allocation/copy support (header comment `neuron_ioctl.h:46-51`). The `_IOC` overload mechanics are owned by [ioctl-dispatch §3](ioctl-dispatch.md#scheme-b--struct-version-overloads-size-resolved-inside-the-handler).

### Allocation rollback

On any `copy_to_user` failure after the chunk exists, the handler calls `mc_free(&mc)` (`:450`/`:545`), which both returns the chunk to its pool *and* releases the handle-table slot (`mc_free` → `nmch_handle_free`, `neuron_mempool.c:877`). So a failed handle-return never leaks either the chunk or the table entry — the alloc is atomic from userspace's view.

---

## 3. Free, Copy, Memset, and Buffer Copy

These are the consume handlers: each `copy_from_user`s its arg, resolves one or two handles through `ncdev_mem_handle_to_mem_chunk`, bounds-checks any copy range, then dispatches to `mc_free` or the DMA layer.

### `MEM_FREE` (`:691`)

```c
// neuron_cdev.c:691 — the only handler that releases a chunk on demand
function ncdev_mem_free(nd, param):
    copy_from_user(&arg, param, sizeof(mem_free))            // :697  arg = { u64 mem_handle }
    mc = ncdev_mem_handle_to_mem_chunk(nd, arg.mem_handle)   // :701  resolve + magic-check
    if mc == NULL: return -EINVAL                            // :702
    trace_ioctl_mem_alloc(nd, mc)                            // :704  (alloc trace event reused)
    mc_free(&mc)                                             // :705  refcount--, at 0: free handle, rbtree, pool; magic→0xDEAD
```

`mc_free` (`neuron_mempool.c:860`) decrements `ref_count` and, at zero, releases the handle slot (`nmch_handle_free`, `:877`), removes the rbtree node, returns the memory to its genpool or `dma_free_coherent`, unlinks the lifespan list, poisons `magic = 0xDEAD` (`:925`), and `kfree`s. The magic poison is exactly what makes a *second* `MEM_FREE` on a stale handle fail cleanly: the slot is back on the free list (so `mc_handle_find` rejects it) and even a re-aliased pointer would fail the `MEMCHUNK_MAGIC` check.

### `MEM_BUF_COPY` (`:1045`) — the representative consume handler

`ncdev_mem_buf_copy` moves a host userspace buffer to/from a chunk. It is the cleanest illustration of the family's arg-demux → resolve → bounds-check → dispatch shape, and it branches on the chunk's *location* for the actual transfer:

```c
// neuron_cdev.c:1045 — host buffer ⇄ mem_chunk
function ncdev_mem_buf_copy(nd, cmd, param):
    static_assert(MEM_BUF_COPY != MEM_BUF_COPY64)           // :1047
    if cmd == MEM_BUF_COPY:                                  // :1057  32-bit size/offset
        copy_from_user(&arg, param, sizeof(mem_buf_copy))    // :1059
    else if cmd == MEM_BUF_COPY64:                          // :1067  64-bit size/offset
        copy_from_user(&arg, param, sizeof(mem_buf_copy64))  // :1069
    else: return -EINVAL                                    // :1077-1078
    // both widen mem_handle/buffer/offset/size/copy_to_mem_handle to locals

    mc = ncdev_mem_handle_to_mem_chunk(nd, mem_handle)      // :1081  resolve + magic-check
    if mc == NULL: return -EINVAL                           // :1082
    if !mc_access_is_within_bounds(mc, offset, size):       // :1085  THE security gate (overflow-safe)
        return -EINVAL                                      // :1087

    if mc->mem_location == MEM_LOC_HOST:                    // :1095  host chunk → direct copy
        if copy_to_mem_handle: copy_from_user(mc->va + offset, buffer, size)  // :1097
        else:                  copy_to_user(buffer, mc->va + offset, size)    // :1099
        return ret
    else:                                                  // :1102  device chunk → bounce via DMA
        mc_alloc_align(.., MC_LIFESPAN_LOCAL, MAX_DMA_DESC_SIZE, .., NCDEV_HOST, &src_mc)  // :1108  scratch bounce buffer
        while remaining:                                   // :1114  chunked transfer
            copy_size = min(remaining, MAX_DMA_DESC_SIZE)
            if copy_to_mem_handle:
                copy_from_user(src_mc->va, buffer + off, copy_size)          // :1117
                ndma_memcpy_buf_to_mc(nd, src_mc->va, 0, mc, offset+off, copy_size)  // :1121  → dma-op-layer
            else:
                ndma_memcpy_buf_from_mc(nd, src_mc->va, 0, mc, offset+off, copy_size) // :1127
                copy_to_user(buffer + off, src_mc->va, copy_size)            // :1132
        mc_free(&src_mc)                                   // :1140  release the LOCAL bounce buffer
```

> **GOTCHA —** the bounds check `mc_access_is_within_bounds(mc, offset, size)` (`:1085`) is the *only* thing standing between an attacker-controlled `offset`/`size` and an OOB device-memory access, and it is deliberately written overflow-safe: `(size <= allowed && offset <= allowed - size)`, never `offset + size <= allowed` (the addition could wrap, `neuron_mempool.h:234-236`). For a `CONTIGUOUS_SCRATCHPAD_DEVICE` chunk `allowed` spans the *whole* contiguous scratchpad (`mp->main_pool_end_addr - mc->pa`, `:229`), not just `mc->size` — a deliberate cross-chunk window. A reimplementer who "simplifies" this to `offset + size <= mc->size` reintroduces both the overflow bug and the scratchpad-span regression. The same check guards `MEMSET` (`:749`), `MEM_COPY`, and the zerocopy paths.

### `MEMSET` (`:709`)

`ncdev_memset` demuxes `MEMSET`/`MEMSET64` by exact `cmd ==` (`:721`/`:730`), resolves the handle (`:743`), applies the identical `mc_access_is_within_bounds` gate (`:749`), then calls `ndma_memset(nd, mc, offset, value, size)` (`:754`) — the DMA engine fills the span; there is no host-direct fast path even for host chunks.

### Info queries — handle in, PA/cookie out

The query handlers take a handle (or a PA) and return location metadata. They are where the **mmap cookie** is minted: `MEM_GET_INFO` returns `mc->pa` *and* `nmmap_offset(mc)` (= `mc->pa`) in its `mmap_offset` out-pointer (`:582`,`:587-588`); `MEM_GET_EXTENDED_INFO` returns the same inline (not via user pointers) plus `pid`/`size`, and — a second footgun — sets `local.mem_handle = (u64)mc`, the raw kernel pointer (`:617`), which is *not* the table handle. `MEM_MC_GET_INFO` (`:628`) is the reverse bridge: it takes a `pa` `[in]`, finds the chunk via `nmmap_get_mc_from_pa` (`:642`), and returns `mmap_offset = mc->pa`/`size` (`:647-648`); the `_V2` variant additionally mints a real table handle through `ncdev_mem_chunk_to_mem_handle` (`:664`).

> **NOTE —** `MEM_GET_EXTENDED_INFO`'s `local.mem_handle = (u64)mc` (`:617`) leaks a kernel heap pointer to userspace and is *not* a usable handle — it is not a table index, so feeding it to a later ioctl would fail `nmch_idx_valid`. It is informational only (and a KASLR-relevant infoleak). The usable PA→handle bridge is `MEM_MC_GET_INFO_V2` (`:664`), which returns a genuine table index. Do not confuse the two.

---

## 4. Per-Handler Reference

### Arg structs

The `[in]`/`[out]` shape per handler. Field offsets are nominal LP64 (declaration order; not `pahole`-verified, hence MED on exact padding — the field *set* is HIGH).

| Handler | Arg struct (`neuron_ioctl.h`) | Key `[in]` fields | Key `[out]` fields |
|---|---|---|---|
| `ncdev_mem_alloc` | `mem_alloc` `:16` | `size`, `host_memory`, `dram_channel`, `dram_region`, `nc_id` | `*mem_handle` |
| `ncdev_mem_alloc_libnrt` | `mem_alloc_v2` `:25` / `_mem_type` `:35` / `_mem_type64` `:52` | + `align`; `_mem_type*` add `mem_type`; `64` adds ignored `pad` | `*mem_handle` |
| `ncdev_mem_free` | `mem_free` `:110` | `mem_handle` | — |
| `ncdev_mem_get_pa_deprecated` | `mem_get_pa` `:95` | `mem_handle` | `*pa` |
| `ncdev_mem_get_info_deprecated` | `mem_get_info` `:82` | `mem_handle` | `*mmap_offset` (=`pa`), `*pa` |
| `ncdev_mem_get_extended_info` | `mem_get_extended_info` `:100` | `mem_handle`, `version` | inline `host_memory`, `mmap_offset`, `pa`, `pid`, `size`, `mem_handle`(=raw ptr) |
| `ncdev_mem_get_mc_mmap_info` | `mem_get_mc_mmap_info` `:506` / `_v2` `:512` | `pa` | `mmap_offset` (=`pa`), `size`; `_v2` adds `mem_handle` |
| `ncdev_memset` | `memset` `:159` / `memset64` `:166` | `mem_handle`, `offset`, `value`, `size` | — |
| `ncdev_mem_copy` | `mem_copy` `:114` / `mem_copy64` `:122` | `src/dst_mem_handle`, `size`, `src/dst_offset` | — |
| `ncdev_mem_copy_async` | `mem_copy_async` `:130` / `64` `:141` | + `host_prefetch_addr`, `pwait_handle` | `wait_handle` |
| `ncdev_mem_buf_copy` | `mem_buf_copy` `:173` / `64` `:181` | `mem_handle`, `buffer`, `size`, `offset`, `copy_to_mem_handle` | — |
| `ncdev_mem_buf_zerocopy64` | `mem_buf_copy64zc` `:189` | + `is_copy_to_device`, `bar4_wr_threshold`, `h2t_qid` | — |

### Function Map

| Handler | file:line | Arg width demux | Bounds / validate gate | Confidence |
|---|---|---|---|---|
| `ncdev_mem_alloc` | `:416` | single (`nr 21`) | `mc_alloc_align` rc | HIGH |
| `ncdev_mem_alloc_libnrt` | `:456` | exact `cmd ==` (V2/MT/MT64) `:476-510` | `mc_alloc_align` rc | HIGH |
| `ncdev_mem_free` | `:691` | single | `ncdev_mem_handle_to_mem_chunk` `:701` | HIGH |
| `ncdev_mem_get_pa_deprecated` | `:551` | single | handle resolve `:562` | HIGH |
| `ncdev_mem_get_info_deprecated` | `:568` | single | handle resolve `:579` | HIGH |
| `ncdev_mem_get_extended_info` | `:594` | single | handle resolve `:604` | HIGH |
| `ncdev_mem_get_mc_mmap_info` | `:628` | `_IOC_SIZE ==` (v1/v2) `:637-651` | `nmmap_get_mc_from_pa` NULL-check `:643` | HIGH |
| `ncdev_memset` | `:709` | exact `cmd ==` `:721-730` | `mc_access_is_within_bounds` `:749` | HIGH |
| `ncdev_mem_copy` | `:761` | exact `cmd ==` `:774-785` | `mc_access_is_within_bounds` (src+dst) | HIGH |
| `ncdev_mem_copy_async` | `:823` | `union` + `_IOC_SIZE` `:837-858` | bounds + `pwait_handle` range | HIGH |
| `ncdev_mem_buf_copy` | `:1045` | exact `cmd ==` `:1057-1067` | `mc_access_is_within_bounds` `:1085` | HIGH |
| `ncdev_mem_buf_zerocopy64` | `:1150` | `_IOC_SIZE` guard | bounds + `access_ok(buffer)` + BAR4 alignment | HIGH |
| `ncdev_mem_buf_zerocopy64_batch` | `:1242` | single (`nr 129`) | per-op bounds + `SIZE_MAX` overflow guard | HIGH |
| `ncdev_dump_mem_chunks` | `:2390` | single (`nr 116`) | `mc_dump_all_chunks` rb-walk | HIGH |
| `ncdev_resource_mmap_info` | `:2299` | single (`nr 112`) | delegates `nmap_dm_special_resource_get` | MED (boundary) |

> **NOTE —** `MEM_COPY64` and `MEM_COPY_ASYNC64` reach `ncdev_mem_copy`/`_async` via `_IOC_NR` dispatch (`:3294`/`:3296`) but **slip the attach gate** — the Gate-2 allow-list (`:3213-3216`) tests the exact 32-bit `cmd`, which the `*64` width fails. They still require a non-free-access fd, and the blast radius is bounded by handle ownership (`mc_handle_find` resolves only *this* `nd`'s handles) and `mc_access_is_within_bounds`. The full slip is finding S3 on [ioctl-attack-surface](../security/ioctl-attack-surface.md), derived on [ioctl-dispatch](ioctl-dispatch.md#the-64-sub-gate-slip).

---

## 5. Lifecycle and Auto-Free

An ioctl-allocated chunk is `MC_LIFESPAN_CUR_PROCESS` (`:437`/`:533`), so it survives across ioctls within the owning process and is reclaimed when that process tears the device down — userspace need not pair every `MEM_ALLOC` with a `MEM_FREE`. Two reclaim points back-stop a leak:

- **Per-process flush** — on the last attached close (`attach_cnt == 1`), `ncdev_flush` calls `mpset_free_expired_mc(&nd->mpset, MC_LIFESPAN_CUR_PROCESS)` and `nmmap_delete_all_nodes(nd)` (`:3478-3479`), freeing every chunk the process allocated and dropping its mmap nodes.
- **Last-fd release** — on `open_count == 0`, `ncdev_release` frees the `MC_LIFESPAN_ALL_PROCESS` chunks and again `nmmap_delete_all_nodes` (`:3514-3515`).

Transient scratch chunks — the `MEM_BUF_COPY` device bounce buffer (`:1108`), `PROGRAM_ENGINE` staging — are `MC_LIFESPAN_LOCAL` and explicitly `mc_free`d before the handler returns (`:1140`), so they never reach either teardown path. The lifespan promotion machinery (`LOCAL → CUR_PROCESS → ALL_PROCESS → DEVICE`) and the leak `BUG_ON` at device detach are owned by [mempool-handles](mempool-handles.md).

> **QUIRK —** the same `trace_ioctl_mem_alloc` event is emitted from `MEM_FREE` (`:704`) as from `MEM_ALLOC` (`:442`/`:537`). A reimplementer wiring tracepoint consumers must not assume one `ioctl_mem_alloc` record equals one allocation — the free path reuses the event to log the chunk being released. Distinguish by the surrounding `mc->ref_count`/`magic` transition, not by the event name.

---

## Cross-References

- [IOCTL Catalog](ioctl-catalog.md) — the per-command `nr`/direction/arg-struct/`ndl_*`-caller/gate table for the mem family; this page is the handler-body deep-dive the catalog's mem rows cross-reference
- [Memory Pool and MC Handle Table](mempool-handles.md) — the allocator engine behind `mc_alloc_align`/`mc_free`, the genpool/lifespan model, the PA-keyed rbtree, and the 2-level handle table's free-list/poisoning internals (owns the layout this page drives)
- [Char Device, fops and mmap](cdev-mmap.md) — the `mmap` pgoff-cookie decode (`pgoff << PAGE_SHIFT → pa → mpset_search_mc`) that consumes the `nmmap_offset` cookie minted here, and full-chunk-only mapping
- [DMA Op Layer and the Completion-Marker Model](dma-op-layer.md) — `ndma_memcpy_mc`/`ndma_memcpy_buf_to_mc`/`ndma_memset`/zerocopy submit, the DMA primitives every copy/memset path bottoms out in
- [IOCTL Dispatch and the Privilege-Gate Model](ioctl-dispatch.md) — the `_IOC_NR`/`_IOC_SIZE` overload resolution this page's size-overloaded handlers demux, and the `*64` attach-gate slip
- [The IOCTL Attack Surface (14 Findings)](../security/ioctl-attack-surface.md) — the security projection: the `*64` slip (S3), `mc_access_is_within_bounds` as the OOB boundary, and the `MEM_GET_EXTENDED_INFO` kernel-pointer infoleak
