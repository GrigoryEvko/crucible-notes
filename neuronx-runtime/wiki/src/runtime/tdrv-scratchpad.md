# TDRV: HBM Scratchpad and Sync-Point

> *All addresses, offsets, sizes, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (read `.data`/`.rodata` globals at their VMA directly); `.bss` is `NOBITS`. Provenance strings `/opt/workspace/KaenaRuntime/tdrv/scratchpad.c` and `…/tdrv_arch_type.c` root every function. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — the scratchpad struct layout is DWARF `structures.json`; the page-reserve / mmap / free logic is from x86-64 decompilation; the per-arch event-address arithmetic and the `add_ev_*` opcode bytes are read from the leaf bodies; call edges from the IDA call graph. · Part IV — TDRV Runtime · [back to index](../index.md)*

## Abstract

`tdrv/scratchpad.c` owns the runtime's **HBM scratchpad** — a per-TPB arena of device-DRAM pages that backs every *intermediate* tensor a model produces during execution. Where weights and I/O buffers each get their own dedicated `dmem_t` allocation, the transient feature maps a kernel writes and a later kernel reads (the `MR_VIRTUAL_TMP_BUF` variables in the static memory plan) are not allocated one-per-variable. The compiler instead packs them into a single linear arena whose size it computes at compile time, and the runtime reserves that arena once, as a small set of fixed-size **scratchpad pages**, at model load. A virtual-scratchpad variable then resolves to `page[offset / page_size]` plus an in-page byte offset — the same zero-allocation placement discipline that [memory-planning](../neff/memory-planning.md) describes for the plan as a whole, specialized to the one variable kind whose lifetime is bounded by a single inference.

The reference frame is a **slab of equal-size pages with a paged-arena address map**, sitting on top of the [dmem](tdrv-dmem.md) allocator. The arena has two flavors a reimplementer must keep distinct: the **paged** mode (the default — `num_pages` independent `dmem_alloc_aligned` allocations, each one `page_size` bytes, addressed bottom-up) and the **contiguous** mode (a single driver-backed contiguous region, opted in by `experimental_contiguous_scratchpad`, addressed top-down and `mmap`-able into one host VA). The arena is sized from the plan header (`kbin_mem_ref_set_t.local_scratchpad_size` / `.shared_scratchpad_size`), laid out by `update_scratchpad_offset`, reserved by the `tdrv_scratchpad_reserve` loop inside `tdrv_scratchpad_model_init`, and resolved per-variable by `tdrv_scratchpad_get_mem`. The staging bridge — the function that copies a `kbin_mem_ref` into the arena (or into SB/HBM for the non-scratchpad kinds) — is `mem_ref_copy_and_stage_mr`, owned by [memory-planning](../neff/memory-planning.md); this page documents the *placement* it performs into the scratchpad, not the planner.

The second half of the page is the **sync-event plane** — the hardware-semaphore layer that gates cross-engine ordering. A scratchpad page written by one engine and read by another is only safe if the reader waits on a semaphore the writer sets; the runtime emits those wait/set/clear events into engine instruction streams as it builds instruction blocks. Three pieces make this work: the `tdrv_arch_ops.sync_events` table (a `uint8[20]` of per-generation semaphore ids, read by the `tdrv_sync_get_*` accessor family — the *which-id* layer, owned by [tdrv-arch-ops](tdrv-arch-ops.md)); the `add_ev_wait`/`add_ev_set`/`add_ev_clr` emitters that encode an event op into an IB chunk (the *what-instruction* layer); and the per-arch `tdrv_arch_get_evt_addr` / `tdrv_arch_get_evt_accel_addr` accessors that resolve an event id to a **device MMIO address** (the *where* layer). This page documents the last two — the emitters and the address arithmetic — and pins the per-arch address formulas.

For reimplementation, the contract is:

- **The scratchpad object model** — `tdrv_scratchpad_t` (16440 B), embedded in `tpb_t` at `+22200`: a mutex, a `page_size`, a `num_pages` count, an `is_contiguous` flag, and a fixed `tdrv_scratchpad_page_t[1024]` array of `{dmem_t* mem, uint32 refs}`. Page count is hard-capped at 1024.
- **The two addressing modes** — paged (bottom-up: `pages[offset / page_size]`) vs contiguous (top-down: `pages[num_pages-1 - offset/page_size]`), selected by `is_contiguous`. The page-boundary rule and the per-mode bounds check differ; reproduce both.
- **The sizing + reserve chain** — `tdrv_scratchpad_init` sets `page_size`; `update_scratchpad_offset` packs variables into page-aligned offsets; `tdrv_scratchpad_model_init` sums per-TPB sizes from the plan header, runs the `tdrv_scratchpad_reserve` page-allocation loop (`dmem_alloc_aligned` per paged page, `dmem_alloc_contiguous_scratchpad_page` per contiguous page, 4K-alignment assert, page-`refs` accounting), and optionally `mmap`s the contiguous region.
- **The sync-event API** — the `add_ev_wait`/`_set`/`_clr` IB-opcode encoding (opcode `0x10A4`, sub-op bytes 7/17/18) and the per-arch event-MMIO-address arithmetic (`get_evt_addr`: SUNDA `axi_offset(tpb, 4·id + 0x2700000)`, CAYMAN/MARIANA `axi_offset(tpb, 4·(id + 0x2009C0000))`; `get_evt_accel_addr`: the per-arch `{clear, set}` carveout address pair).

| | |
|---|---|
| **Source** | `/opt/workspace/KaenaRuntime/tdrv/scratchpad.c`; `…/tdrv_arch_type.c` (sync-event accessors) |
| **Arena struct** | `tdrv_scratchpad_t` — **16440 B** (`structures.json` ord 8644), embedded in `tpb_t+22200` |
| **Page struct** | `tdrv_scratchpad_page_t` — **16 B** (`{dmem_t* mem@0, uint32 refs@8}`), `[1024]` array |
| **Plan-arg struct** | `tdrv_scratchpad_info_t` — **24 B** (`{pcore, size, offset}`); `tdrv_scratchpad_cleanup_info_t` — **48 B** |
| **Init** | `tdrv_scratchpad_init` `@0x302900` — set `page_size`, init lock; contiguous opt-in |
| **Plan driver** | `tdrv_scratchpad_model_init` `@0x302b70` (0xb19 B) — size → reserve → optional `mmap` |
| **Offset layout** | `update_scratchpad_offset` `@0x3024a0` — page-aligned packing into the arena |
| **Resolve** | `tdrv_scratchpad_get_mem` `@0x3026d0` — `(offset,size) → (dmem_t*, in-page offset)` |
| **Page cap** | **1024** pages (`scratchpad.c:tdrv_scratchpad_reserve`, `> 0x400` → error) |
| **Contiguous default** | `page_size = 0x4000000` (64 MiB), set when `experimental_contiguous_scratchpad` + driver feature `0x80` |
| **Sync-event table** | `tdrv_arch_ops.sync_events` (`+472`) → `uint8[20]` per-arch ids ([tdrv-arch-ops](tdrv-arch-ops.md) owns it) |
| **Event emitters** | `add_ev_wait @0x273c50` · `add_ev_set @0x273c00` · `add_ev_clr @0x273ca0` — opcode `0x10A4` |
| **Event-addr accessors** | `tdrv_arch_get_evt_addr @0x309f50` · `tdrv_arch_get_evt_accel_addr @0x309ef0` (arch-dispatched) |
| **Staging bridge** | `mem_ref_copy_and_stage_mr @0x2fb780` — owned by [memory-planning](../neff/memory-planning.md) |

---

## 1. The Scratchpad Object Model

### Purpose

A `tdrv_scratchpad_t` answers one question for the executor: *given a virtual-scratchpad variable's planned offset, which device DRAM page backs it, and at what in-page offset?* It is deliberately a flat slab of equal-size pages rather than a heap, because the compiler already decided every transient's placement — the runtime only reserves the bytes and exposes a paged-arena address map over them. The arena lives **inside** the TPB object (it is not separately allocated), one per physical TPB, so a scratchpad is implicitly scoped to a NeuronCore.

### `tdrv_scratchpad_t` — the arena (16440 B)

Embedded in `tpb_t` at `+22200` (`structures.json`); offsets below are relative to the arena base. All confirmed against the `tdrv_scratchpad_init` / `_reserve` / `_get_mem` field writes.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `lock` | +0 (0x00) | `pthread_mutex_t` (40 B) | guards `num_pages` and the `pages[]` array during reserve/free | HIGH |
| `page_size` | +40 (0x28) | `uint64_t` | bytes per page; `0` is asserted-against at resolve; set by `tdrv_scratchpad_init` | HIGH |
| `num_pages` | +48 (0x30) | `uint32_t` | live page count (`≤ 1024`); grown by reserve, shrunk by free | HIGH |
| `is_contiguous` | +52 (0x34) | `bool` | `1` ⇒ contiguous mode (single driver region, top-down map); `0` ⇒ paged | HIGH |
| `pages` | +56 (0x38) | `tdrv_scratchpad_page_t[1024]` | the page table; entry `i` backs arena bytes `[i·page_size, (i+1)·page_size)` | HIGH |

`tdrv_scratchpad_page_t` (16 B): `{dmem_t* mem @+0; uint32 refs @+8}`. `mem` is the backing [dmem](tdrv-dmem.md) handle (`NULL` until reserved); `refs` is a per-page reference count that lets several models on the same TPB share the lower pages of one arena — `tdrv_scratchpad_reserve` bumps `refs` on every page it touches, `tdrv_scratchpad_free` decrements and frees a page only when its `refs` hits zero.

> **NOTE —** the `pages[1024]` array is inline, not pointed-to, which is why `tdrv_scratchpad_t` is 16440 = `0x38 + 1024·16` bytes and why the whole arena ships *inside* every `tpb_t` (the largest single member of the TPB object). A reimplementer can heap-allocate the page table instead, but must then update the `tpb + 16·page_idx + 22256` addressing the decompiled reserve loop uses (the `.mem` of page *k* sits at `tpb + 22256 + 16k`; the `2782`-qword index in the raw decompile is `22256/8`).

### `tdrv_scratchpad_info_t` and `tdrv_scratchpad_cleanup_info_t`

The planner passes scratchpad sizing through two small records (not part of the arena):

| Struct | Size | Fields |
|---|---|---|
| `tdrv_scratchpad_info_t` | 24 B | `+0 const physical_core_t* pcore`; `+8 uint64 size`; `+16 uint64 offset` (the planned base of this TPB's arena slice) |
| `tdrv_scratchpad_cleanup_info_t` | 48 B | `+0 const physical_core_t* pcores[2]`; `+16 uint64 sizes[2]`; `+32 uint32 num_entries`; `+40 void* contiguous_scratchpad_va` |

`tdrv_scratchpad_info_t` is the per-TPB sizing/placement descriptor `tdrv_scratchpad_model_init` fills and `update_scratchpad_offset` consumes; `tdrv_scratchpad_cleanup_info_t` is the rollback ledger — it records which `(pcore, size)` pairs were reserved so `tdrv_scratchpad_cleanup` can free exactly them (and `munmap` the contiguous VA) on a partial-init failure.

### The two addressing modes

The single fact that governs every scratchpad address is `is_contiguous`. In **paged** mode the arena is `num_pages` independent allocations addressed bottom-up; in **contiguous** mode it is one driver region addressed top-down (page `num_pages-1` holds the lowest arena bytes). This is the most counter-intuitive part of the layer.

> **QUIRK —** the two modes index the page table in **opposite directions**. Paged: `page = pages[offset / page_size]`, and a request may not straddle a page boundary (`offset % page_size + size ≤ page_size` is required, else the resolve returns "not found"). Contiguous: `page = pages[(num_pages-1) - offset / page_size]`, with a single arena-wide bound (`offset + size ≤ page_size · num_pages`) and no per-page straddle restriction, because the region is physically contiguous. A reimplementer who uses the paged formula on a contiguous arena (or vice versa) addresses the wrong page for every variable past the first. The in-page offset is `offset % page_size` in **both** modes.

### Function Map — object model

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tdrv_scratchpad_init` | `0x302900` | zero the arena, set `page_size`, init lock; contiguous opt-in (§2) | HIGH |
| `tdrv_scratchpad_destroy` | `0x302ac0` | 12-byte stub → `tdrv_scratchpad_cleanup` (teardown) | HIGH |
| `tdrv_scratchpad_get_mem` | `0x3026d0` | resolve `(offset,size)` → `(dmem_t* mem, in-page offset)` (§1 modes) | HIGH |
| `tdrv_scratchpad_get_max_size` | `0x302830` | max oversized-scratchpad size across a vcore's TPBs | HIGH |
| `tdrv_scratchpad_get_max_size_from_pcore` | `0x3027c0` | `ht_for_each(model_db)` → largest oversized var (`get_largest_oversized_scratchpad_size`) | HIGH |
| `tdrv_scratchpad_recommend_page_size` | `0x3028b0` | round `max_size` up to 512 MiB; emit `NEURON_SCRATCHPAD_PAGE_SIZE` advice | HIGH |

---

## 2. Initialization: `tdrv_scratchpad_init` and the Contiguous Opt-In

### Purpose

`tdrv_scratchpad_init` is run once per TPB from `tdrv_init` (`@0x26c6dd`). It establishes the arena's `page_size` and lock but reserves **no** device bytes — pages are allocated lazily at model load (§3). Its one branch of interest is the *contiguous-scratchpad* feature gate: an experimental mode that, when the driver supports it, replaces the many-small-pages arena with one large driver-backed contiguous region.

### Algorithm

```c
// tdrv_scratchpad_init @0x302900 — per-TPB arena setup. scratchpad.c.
function tdrv_scratchpad_init(pcore, page_size):
    if page_size - 1 > 0xFFFFFFFE:                       // page_size == 0 or > UINT32_MAX
        nlog("Invalid scratchpad page size %lu bytes… must be less than %u bytes")
        return NRT_INVALID
    if db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb) != 0: return <err>

    memset(&tpb->scratchpad, 0, sizeof(tdrv_scratchpad_t))   // 16440 B cleared
    tpb->scratchpad.page_size = page_size                    // +0x28

    if !nrt_gconf()->experimental_contiguous_scratchpad:
        goto init_lock                                       // default: paged mode, caller's page_size

    // --- contiguous-scratchpad opt-in ---
    if al_hal_tpb_get_arch_type() > 2 && pcore->vcore->num_tpbs == 1:
        nlog("Contiguous Scratchpad feature is disabled on LNC 1")     // CAYMAN/MARIANA, 1-TPB vnc
        // (falls through to init_lock in paged mode)
    else if ndl_feature_supported(mla->device->ctx_fd, 0x80):          // driver capability bit 0x80
        tpb->scratchpad.page_size    = 0x4000000                       // 64 MiB single page
        tpb->scratchpad.is_contiguous = 1                              // +0x34
    else:
        nlog("Contiguous Scratchpad feature is disabled due to incompatible driver")

init_lock:
    if pthread_mutex_init(&tpb->scratchpad.lock, 0) != 0:
        nlog("Failed to init lock"); return NRT_FAILURE
    return NRT_SUCCESS
```

> **GOTCHA —** in contiguous mode `tdrv_scratchpad_init` **overrides** the caller's `page_size` with the hard-wired `0x4000000` (64 MiB) and sets `is_contiguous = 1`. A reimplementer who honors the requested page size in contiguous mode will mis-size every page; the contiguous region is reserved as `num_pages × 64 MiB` and `mmap`'d as one span (§3). The feature is gated three ways — `experimental_contiguous_scratchpad` global, *not* a 1-TPB vnc on CAYMAN/MARIANA (`arch > 2`), and driver feature bit `0x80` (the same `ndl_feature_supported` bit `dmem_alloc_contiguous_scratchpad_page` and `ndl_memory_map_contiguous_scratchpad` check) — and silently falls back to paged mode if any gate fails.

> **NOTE —** the page-size advisory path (`tdrv_scratchpad_recommend_page_size @0x3028b0`) rounds the largest seen oversized variable up to a 512 MiB (`0x20000000`) multiple and prints the `NEURON_CC_FLAGS="--hbm-scratchpad-page-size=%lu"` / `NEURON_SCRATCHPAD_PAGE_SIZE=%lu` recommendation in MiB. It is emitted when a variable does not fit a single page (§3), so an operator can grow the page size to avoid the per-variable independent-allocation fallback. The largest oversized size is harvested from the per-TPB `model_db` hashtable, reading `mem_ref_t+448` (`largest_oversized_scratchpad_var`) per entry.

---

## 3. The Plan: `tdrv_scratchpad_model_init` and the Reserve Loop

### Purpose

`tdrv_scratchpad_model_init` is the heart of the layer: at model load (driven by `vtpb_info_shared_init_vtpb` `@0x314cc6`/`@0x314ed8`), it turns the compiler's per-TPB scratchpad *sizes* into reserved device pages. It runs three phases — **size** (sum each TPB's local + shared arena bytes from the plan header), **lay out** (`update_scratchpad_offset` packs variables into page-aligned offsets), and **reserve** (the inlined `tdrv_scratchpad_reserve` loop allocates pages and bumps `refs`) — then, for a contiguous arena, `mmap`s the whole region into one host VA. On any failure it rolls back via `tdrv_scratchpad_cleanup`.

### Entry Point

```text
vtpb_info_shared_init_vtpb (0x3148e0)            ── per-vnc model-load driver [memory-planning]
  └─ tdrv_scratchpad_model_init (0x302b70)        ── THIS §
       ├─ db_get_hbm_group_pcores                 ── the ≤2 pcores sharing one HBM group
       ├─ (size)  kbin->mem_ref_set->{local,shared}_scratchpad_size   ── plan header
       ├─ update_scratchpad_offset (0x3024a0)     ── page-aligned packing, per TPB
       ├─ tdrv_arch_get_num_tpb_per_hbm           ── ownership assertion
       ├─ [reserve loop — inlined tdrv_scratchpad_reserve]
       │    ├─ dmem_alloc_aligned (0x228f20)               ── PAGED: one 1-page DRAM alloc, 4K-aligned
       │    ├─ dmem_alloc_contiguous_scratchpad_page (0x229080)  ── CONTIGUOUS: one driver region
       │    └─ (page->refs++; assert _pa+align_offset 4K-aligned)
       └─ [contiguous]  ndl_memory_map_contiguous_scratchpad (0xc54d0)  ── mmap whole region → one VA
  └─ (on failure) tdrv_scratchpad_cleanup (0x302ad0) → tdrv_scratchpad_free (0x302510)
```

### Algorithm — sizing and layout

```c
// tdrv_scratchpad_model_init @0x302b70 — phases: size → layout → reserve → mmap.
// kbins[0..num_kbins) is the per-TPB plan; an HBM group has at most 2 TPBs.
function tdrv_scratchpad_model_init(kbins, num_kbins, vcore, cleanup_infos,
                                    local_scratchpads, shared_scratchpad, out contiguous_va):
    assert num_kbins <= vcore->num_tpbs        // scratchpad.c:0x1A2
    assert num_kbins <= MAX_TPB_PER_HBM (=2)   // scratchpad.c:0x1A4
    db_get_hbm_group_pcores(vcore->tpbs, &owned_pcores, &num_owned_tpbs)   // the ≤2 sharing pcores
    sharing_pcore = owned_pcores[0]
    db_physical_core_get_mla_and_tpb(sharing_pcore, &mla, &sharing_tpb)
    // "single core" placement: gconf.scratchpad_on_single_core OR a contiguous sharing TPB
    single_core = sharing_tpb->scratchpad.is_contiguous || !gconf->scratchpad_on_single_core

    // (1) SIZE — sum per-TPB arena bytes from the plan header (kbin_mem_ref_set_t)
    //     local_scratchpad is per-TPB; shared_scratchpad is the MAX across the group.
    for k in 0..num_kbins:
        ms = kbins[k].mem_ref_set
        local_scratchpads[k].pcore  = <owning pcore>
        local_scratchpads[k].offset = 0
        local_scratchpads[k].size   = ms->local_scratchpad_size          // +0  (plan header)
        shared_scratchpad->size     = max(shared_scratchpad->size, ms->shared_scratchpad_size)  // +8
    shared_scratchpad->pcore = sharing_pcore
    // shared arena is charged to the sharing TPB's running offset:
    scratchpad_sizes_by_tpb_idx[sharing_tpb_idx] += shared_scratchpad->size

    // (2) LAYOUT — pack each TPB's local arena after the shared one, page-aligned
    for info in local_scratchpads[0..num_kbins]:
        if update_scratchpad_offset(&scratchpad_sizes_by_tpb_idx[info->pcore->device_tpb_idx], info):
            goto rollback                                                // "Failed to calculate scratchpad info"

    // ownership invariant: if optimizations assume the whole HBM group is owned, enforce it
    if num_owned_tpbs != tdrv_arch_get_num_tpb_per_hbm():
        nlog("current process does not own all physical cores on HBM…"); return NRT_INVALID
```

`update_scratchpad_offset` is the packer — it advances a per-TPB running offset, inserting page-alignment padding only when a variable would otherwise straddle a page boundary (and only in paged mode):

```c
// update_scratchpad_offset @0x3024a0 — page-aligned bump allocator into one TPB's arena.
function update_scratchpad_offset(current_offset /* in/out */, info):
    db_physical_core_get_mla_and_tpb(info->pcore, &mla, &tpb)
    ps    = tpb->scratchpad.page_size
    rem   = *current_offset % ps                        // bytes into the current page
    slack = ps - rem                                    // bytes left in the current page
    base  = *current_offset
    if rem != 0 && slack < info->size && !tpb->scratchpad.is_contiguous:
        base += slack                                   // pad to next page so the var fits in one page
    info->offset    = base                              // the var's planned arena offset
    *current_offset = base + info->size                 // advance the running offset
    return NRT_SUCCESS
```

> **QUIRK —** the page-alignment pad in `update_scratchpad_offset` is applied **only when the variable would cross a page boundary and only in paged mode** (`rem != 0 && slack < size && !is_contiguous`). A variable that already fits in the current page's remaining slack is packed without padding; one that crosses is bumped to the next page so `tdrv_scratchpad_get_mem`'s single-page straddle rule (§1) holds. In contiguous mode no padding is ever inserted (the region is physically contiguous, so a variable may span page boundaries freely). A reimplementer who always page-aligns will waste arena bytes; one who never aligns in paged mode will produce variables that straddle two `dmem_t` pages and fail to resolve.

### Algorithm — the reserve loop (inlined `tdrv_scratchpad_reserve`)

The reserve loop is where pages become real device DRAM. It is invoked under `cleanup_info`-guarded iteration over the group's pcores; for each TPB it computes the page count from the summed size, allocates any missing pages (mode-dependent), and bumps every touched page's `refs`.

```c
// tdrv_scratchpad_reserve — inlined into tdrv_scratchpad_model_init, scratchpad.c.
// `size` is this TPB's total arena bytes from the layout phase.
function tdrv_scratchpad_reserve(pcore, size):
    db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)
    num_pages_needed = ceil_div(size, tpb->scratchpad.page_size)        // = size/ps - (size%ps==0 - 1)
    if num_pages_needed > 0x400:                                        // HARD CAP = 1024 pages
        nlog("Number of scratchpad pages requested exceeded the max… (requested: %u, max: %u)", …, 1024)
        return <err>
    lock(tpb->scratchpad.lock)
    have = tpb->scratchpad.num_pages
    while have < num_pages_needed:                                      // grow the arena
        ps = tpb->scratchpad.page_size
        page_slot = &tpb->scratchpad.pages[have].mem                   // tpb + 22256 + 16*have
        if tpb->scratchpad.is_contiguous:
            rc = dmem_alloc_contiguous_scratchpad_page(pcore, ps, page_slot)   // 0xc53c0 NDL leaf
        else:
            nlog_DEBUG("Allocating scratchpad page=(%u/%u), page_size=%lu", have, num_pages_needed, ps)
            hbm = get_default_hbm_index(tpb->idx)
            rc  = dmem_alloc_aligned(tpb->tpb_allocator, page_slot, ps, TONGA_DRAM, hbm,
                                     0, DMA_MEM_USAGE_TYPE_SCRATCHPAD, "scratchpad", 0x400000)  // 4 MiB align
            mem = tpb->scratchpad.pages[have].mem
            if ((mem->_pa + mem->align_offset) & 0xFFF) != 0:          // 4K page alignment is mandatory
                nlog("Page needs to be 4K aligned")
                __assert_fail(…, "scratchpad.c", 0xDA, "tdrv_scratchpad_reserve")
        if rc:                                                          // alloc failed → unwind
            nlog("Failed to allocate virtual scratchpad!")
            // free every page allocated THIS call (paged mode), then unlock
            while tpb->scratchpad.num_pages < have: dmem_free(pages[--have].mem); pages[have].mem = 0
            unlock(tpb->scratchpad.lock)
            return rc                                                   // NRT_RESOURCE may be a contiguous ceiling (below)
        ++have
    tpb->scratchpad.num_pages = num_pages_needed
    for p in 0..num_pages_needed:                                       // bump refs on every backing page
        assert tpb->scratchpad.pages[p].mem != NULL                    // scratchpad.c:0xED
        ++tpb->scratchpad.pages[p].refs
    unlock(tpb->scratchpad.lock)
    return NRT_SUCCESS
```

> **GOTCHA —** the 4K-alignment check on `_pa + align_offset` is a hard `__assert_fail`, not a recoverable error. Scratchpad pages are addressed by hardware DMA that requires 4K-aligned page bases, so the runtime over-aligns the `dmem_alloc_aligned` request to **4 MiB** (`0x400000`) to guarantee it. A reimplementer who requests only natural alignment will trip the assert on a driver that returns a sub-4K-aligned base. Note the alignment is checked against the *resolved* address `_pa + align_offset`, not the raw `_pa`.

> **QUIRK —** when paged allocation returns `NRT_RESOURCE` (out of HBM) on the sharing TPB and the arena is contiguous, the model_init driver logs `"Could not grow contiguous scratchpad beyond %ld bytes"` (= `page_size · num_pages`) and treats it as a non-fatal ceiling rather than a hard failure — the contiguous arena simply cannot grow past what the driver pre-reserved. For a paged arena, the same `NRT_RESOURCE` triggers the per-call page rollback above and propagates. The two failure dispositions of the *same* `NRT_RESOURCE` code are mode-dependent; a reimplementer must branch on `is_contiguous` in the OOM path.

### Algorithm — contiguous mmap and rollback

```c
// tail of tdrv_scratchpad_model_init — map a contiguous arena into one host VA.
if sharing_tpb->scratchpad.is_contiguous && total_arena_bytes != 0:
    ps = sharing_tpb->scratchpad.page_size
    n  = min(ceil_div(total_arena_bytes, ps), sharing_tpb->scratchpad.num_pages)
    rc = ndl_memory_map_contiguous_scratchpad(                          // 0xc54d0 → ioctl 0x80084E1A
             sharing_tpb->scratchpad.pages[n-1].mem->mem_handle,        // the LOWEST arena page (top-down)
             contiguous_va, ps * n)
    if rc: nlog("Failed to mmap scratchpad, return code %d", rc); goto rollback
    cleanup_infos[idx]->contiguous_scratchpad_va = *contiguous_va       // recorded for munmap on teardown
    return NRT_SUCCESS

rollback:                                                               // partial-init unwind
    for ci in cleanup_infos[0..num_kbins]:
        if ci: tdrv_scratchpad_cleanup(ci)                              // free reserved pages + munmap
    return <error>
```

The contiguous mmap maps from `pages[n-1]` — the page holding the *lowest* arena bytes — because contiguous mode is top-down (§1). `tdrv_scratchpad_cleanup` (`@0x302ad0`) walks the `cleanup_info`'s recorded `(pcore, size)` entries, `tdrv_scratchpad_free`s each, then `munmap`s `contiguous_scratchpad_va` if set; `tdrv_scratchpad_free` (`@0x302510`) decrements each page's `refs` and `dmem_free`s only the pages whose count reaches zero (asserting `pages_freed == pages_marked_for_freeing`, `scratchpad.c:0x11B`).

### Function Map — plan

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `tdrv_scratchpad_model_init` | `0x302b70` | 0xb19 | size → layout → reserve → mmap; per-vnc plan driver | HIGH |
| `update_scratchpad_offset` | `0x3024a0` | 0x6f | page-aligned arena packing per TPB | HIGH |
| `tdrv_scratchpad_cleanup` | `0x302ad0` | 0x96 | free recorded pages + `munmap` contiguous VA (rollback/teardown) | HIGH |
| `tdrv_scratchpad_free` | `0x302510` | 0x1bb | `refs`-counted page free; `dmem_free` at zero-ref | HIGH |
| `dmem_alloc_aligned` | `0x228f20` | — | paged page alloc (4 MiB-aligned, `SCRATCHPAD` usage) — [dmem](tdrv-dmem.md) | HIGH |
| `dmem_alloc_contiguous_scratchpad_page` | `0x229080` | — | contiguous page alloc via `ndl_alloc_contiguous_scratchpad` — [dmem](tdrv-dmem.md) | HIGH |
| `ndl_memory_map_contiguous_scratchpad` | `0xc54d0` | — | `mmap` whole region (ioctl `0x80084E1A`, feature bit `0x80`) | HIGH |
| `get_largest_oversized_scratchpad_size` | `0x302480` | — | `ht_for_each` callback reading `mem_ref_t+448` | HIGH |

> **NOTE —** `mem_ref_copy_and_stage_mr` (`@0x2fb780`, [memory-planning](../neff/memory-planning.md)) is the *consumer* that places a `MR_VIRTUAL_TMP_BUF` variable into the arena: it calls `tdrv_scratchpad_get_mem` (§1) to resolve the variable's planned offset to a `(dmem_t*, in-page offset)` and writes the resulting `physical_address` into the runtime `mem_ref_t`. This page documents the arena and its reserve; the staging walk and the SB / HBM placement of the *other* `mem_ref` kinds (`MR_SB`, `MR_BUFFER`, `MR_TMP_BUF`, …) belong to the planner cell — it is the `mem_ref` owner. A reimplementer wires the two together at `tdrv_scratchpad_get_mem`.

---

## 4. The Sync-Event API

### Purpose

A scratchpad page is shared mutable state across engines: one engine writes a feature map, another reads it. Correctness depends on the reader waiting for a hardware semaphore the writer sets — a cross-engine fence the runtime inserts as it builds instruction blocks. The sync-event API has three layers, and this page owns the lower two. The **which-id** layer is the `tdrv_arch_ops.sync_events` table read by the `tdrv_sync_get_*` accessors ([tdrv-arch-ops](tdrv-arch-ops.md) owns the table + accessor family); the **what-instruction** layer is `add_ev_wait`/`_set`/`_clr`, which encode a wait/set/clear of a given semaphore id into an IB chunk; the **where** layer is `tdrv_arch_get_evt_addr` / `tdrv_arch_get_evt_accel_addr`, which resolve an event to a device MMIO address per generation.

### Entry Point

```text
ib_create_one_block / ib_add_inference_wait_v2 / hw_exec_queue_add_exec_request_impl  (IB builders)
  ├─ tdrv_sync_get_<EVENT> (0x30a5c0..0x30acf0)   ── read uint8 id from sync_events  [tdrv-arch-ops]
  ├─ tdrv_add_ev_wait/set/clr (0x30a470/0x30a4e0/0x30a550)  ── generic guarded dispatch
  │    └─ add_ev_wait/set/clr (0x273c50/0x273c00/0x273ca0)  ── ARCH-AGNOSTIC IB-opcode emitter
  │         └─ add_ins                                       ── append the 0x10A4-opcode instr
  └─ tdrv_arch_get_evt_addr (0x309f50)            ── id → device MMIO addr (arch-dispatched)
       └─ tdrv_arch_get_evt_addr_{sunda|cayman|mariana} (0x30b1b0/0x30c190/0x30d2b0)
            └─ aws_hal_stpb_get_axi_offset                   ── per-TPB AXI → absolute device addr
  └─ tdrv_arch_get_evt_accel_addr (0x309ef0)      ── event-accel carveout {clear,set} addr pair
       └─ tdrv_arch_get_evt_accel_addr_{sunda|cayman|mariana} (0x30b0b0/0x30bbd0/0x30ccf0)
```

### Algorithm — the IB-opcode emitters

`add_ev_wait`/`_set`/`_clr` are three near-identical leaves that build one instruction — opcode `0x10A4` (4260) — into a chunk via `add_ins`. They are **arch-agnostic** (the same leaf is installed in `tdrv_arch_ops.add_ev_wait/set/clr` on all three generations; see [tdrv-arch-ops §3](tdrv-arch-ops.md)), because the wait/set/clear encoding does not vary by silicon. The three differ only in two payload bytes: a sub-op selector and which byte carries the semaphore id.

```c
// add_ev_wait @0x273c50 / add_ev_set @0x273c00 / add_ev_clr @0x273ca0 — IB sync-event emitter.
// Builds a 0x10A4-opcode instruction; emits it via add_ins(chunk, offset, &instr).
function add_ev_<op>(chunk, offset, event_id):
    instr = zeroed 48-byte buffer
    instr.opcode16 = 0x10A4            // 4260 — a 16-bit value at bytes 0-1 (mov %ax,(%rsp));
                                       // bytes 2-3 are zeroed (movups), NOT part of the opcode
    instr.field@12 = 1                 // dword at byte 12 (0xc) = 1 (movl $1,0xc; common to all three)
    switch <op>:
      wait:  instr.byte@4 = 7;  instr.byte@5 = event_id     // sub-op 7,  id at payload byte 5
      set:   instr.byte@6 = 17; instr.byte@7 = event_id     // sub-op 17, id at payload byte 7
      clr:   instr.byte@6 = 18; instr.byte@7 = event_id     // sub-op 18, id at payload byte 7
    return add_ins(chunk, offset, &instr)                   // append; returns new chunk offset
```

> **CORRECTION —** an earlier revision placed these bytes two too low: it described the opcode `0x10A4` as a 4-byte field and put `wait` at bytes 2/3, `set`/`clr` at bytes 4/5, with the common dword at `payload+10`. Re-verified against `objdump -d` of `add_ev_wait@0x273c50` / `add_ev_set@0x273c00` / `add_ev_clr@0x273ca0` (build-id `8bb57aba…`): the opcode is a **16-bit** store (`mov $0x10a4,%eax; mov %ax,(%rsp)`) occupying bytes 0-1 only — bytes 2-3 are zeroed by a `movups`, so the sub-op/id never land there. The true offsets are `wait{sub-op@4, id@5}` (`movb $0x7,0x4`; `mov %dl,0x5`), `set{sub-op@6, id@7}` (`movb $0x11,0x6`; `mov %dl,0x7`), `clr{sub-op@6, id@7}` (`movb $0x12,0x6`; `mov %dl,0x7`), and the common dword is `movl $0x1,0xc` → byte **12 (0xc)**. The opcode (`0x10A4`), the sub-op values (7/17/18), and the field value (1) were correct; only the byte offsets were wrong.

> **QUIRK —** the three ops do **not** share a byte layout: `wait` places its sub-op (7) and id at payload bytes 4/5, while `set` (17) and `clr` (18) place theirs at bytes 6/7. The `event_id` argument is the resolved semaphore id from a `tdrv_sync_get_<EVENT>` accessor (a `uint8`, never `0xFF` — that value is `TDRV_SYNC_EVENT_UNSUPPORTED` and is asserted against at the accessor). A reimplementer who uses one byte layout for all three ops mis-encodes two of them; the opcode (`0x10A4`) and the `byte@12 = 1` field are the only parts in common.

### Algorithm — per-arch event-address resolution

`tdrv_arch_get_evt_addr` resolves a `(tpb_idx, event_id)` pair to the absolute device MMIO address of that semaphore's event register; it dispatches through `tdrv_arch_ops.get_evt_addr` (slot 34) into one of three leaves whose arithmetic differs by generation. The base register address is fed to `aws_hal_stpb_get_axi_offset(tpb_idx, …)`, which rebases it into the addressed TPB's AXI window.

```c
// tdrv_arch_get_evt_addr @0x309f50 — guarded dispatch (slot 34 @+272).
function tdrv_arch_get_evt_addr(tpb_idx, event_id):
    ENSURE_ARCH_OPS()                                       // lazy install [tdrv-arch-ops §2]
    fn = tdrv_arch_ops.get_evt_addr; assert fn != NULL      // tdrv_arch_type.c:0x189
    return fn(tpb_idx, event_id)

// the three leaves — the per-arch event-register base + 4-byte stride:
function get_evt_addr_sunda(tpb, id):    // 0x30b1b0
    return aws_hal_stpb_get_axi_offset(tpb, 4*id + 0x2700000)            // EVT region base 0x2700000
function get_evt_addr_cayman(tpb, id):   // 0x30c190  (mariana 0x30d2b0 identical)
    return aws_hal_stpb_get_axi_offset(tpb, 4*(id + 0x2009C0000))        // = 4*id + 0x802700000
```

```c
// tdrv_arch_get_evt_accel_addr @0x309ef0 — event-accel carveout {clear, set} address pair.
function get_evt_accel_addr(set):                          // slot 33 @+264; arg picks clear vs set
    ENSURE_ARCH_OPS(); return tdrv_arch_ops.get_evt_accel_addr(set)
// leaves return a fixed (clear_addr, set_addr) pair per arch:
//   sunda   (0x30b0b0): clear=0x2000000000  set=0x2000000004
//   cayman  (0x30bbd0): clear=0x9000000000  set=0x9000000004
//   mariana (0x30ccf0): clear=0x9000000000  set=0x9400000000
```

> **NOTE —** the SUNDA event-register base `0x2700000` is the on-chip **EVT aperture** — the same region the V2 UDMA address validator (`mla_udma_addr_check_v2`) recognizes as "checking EVT address." The CAYMAN/MARIANA base `0x802700000` is the SUNDA base with the mesh high word (`0x800000000`) set, consistent with the [arch-csr-offsets](arch-csr-offsets.md) sequencer-aperture rebase: the per-engine `0x2B0xxxx`/`0x2700000`-class low words are shared across generations, and the Trn2/Trn3 mesh prefix distinguishes the second mesh half. The 4-byte stride (`4·id`) means each event register is one dword; `id` is the semaphore index from the `sync_events` table.

> **QUIRK —** the event-accel "set" address is **not** uniformly `clear + 4`. SUNDA and CAYMAN both encode set as `clear + 4` (`…0000` / `…0004`), but MARIANA encodes set as a *different base* entirely (`clear = 0x9000000000`, `set = 0x9400000000` — a `+0x400000000` delta, not `+4`). A reimplementer who computes the set address as `clear_addr + 4` on MARIANA writes to the wrong carveout. These are the absolute device addresses the event-accel carveout descriptor (the IB completion path) targets; `get_evt_accel_addr` is the only event accessor that returns a *pair* keyed by a boolean.

### Per-Arch Event-Address Matrix

| Accessor (slot) | Arg | SUNDA (2) | CAYMAN (3) | MARIANA (4) | Confidence |
|---|---|---|---|---|---|
| `get_evt_addr` (34) | `(tpb, id)` | `axi(tpb, 4·id + 0x2700000)` | `axi(tpb, 4·id + 0x802700000)` | `axi(tpb, 4·id + 0x802700000)` | HIGH |
| `get_evt_accel_addr` (33) | `clear` | `0x2000000000` | `0x9000000000` | `0x9000000000` | HIGH |
| `get_evt_accel_addr` (33) | `set` | `0x2000000004` | `0x9000000004` | `0x9400000000` | HIGH |

### Function Map — sync-event API

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `tdrv_add_ev_wait` | `0x30a470` | guarded dispatch → `tdrv_arch_ops.add_ev_wait` (slot 56) | HIGH |
| `tdrv_add_ev_set` | `0x30a4e0` | guarded dispatch → slot 57 | HIGH |
| `tdrv_add_ev_clr` | `0x30a550` | guarded dispatch → slot 58 | HIGH |
| `add_ev_wait` | `0x273c50` | emit opcode `0x10A4`, sub-op 7 @byte4, id@byte5 (arch-agnostic) | HIGH |
| `add_ev_set` | `0x273c00` | emit opcode `0x10A4`, sub-op 17 @byte6, id@byte7 | HIGH |
| `add_ev_clr` | `0x273ca0` | emit opcode `0x10A4`, sub-op 18 @byte6, id@byte7 | HIGH |
| `tdrv_arch_get_evt_addr` | `0x309f50` | guarded dispatch → per-arch `get_evt_addr` (slot 34) | HIGH |
| `tdrv_arch_get_evt_addr_sunda` | `0x30b1b0` | `axi(tpb, 4·id + 0x2700000)` | HIGH |
| `tdrv_arch_get_evt_addr_cayman` | `0x30c190` | `axi(tpb, 4·id + 0x802700000)` | HIGH |
| `tdrv_arch_get_evt_addr_mariana` | `0x30d2b0` | byte-identical to cayman | HIGH |
| `tdrv_arch_get_evt_accel_addr` | `0x309ef0` | guarded dispatch → per-arch carveout `{clear,set}` (slot 33) | HIGH |
| `tdrv_arch_get_evt_accel_addr_{sunda,cayman,mariana}` | `0x30b0b0`/`0x30bbd0`/`0x30ccf0` | per-arch `{clear,set}` address pair | HIGH |
| `tdrv_sync_get_<EVENT>` (family) | `0x30a5c0`–`0x30acf0` | read `uint8` id from `sync_events`; assert ≠ `0xFF` — [tdrv-arch-ops](tdrv-arch-ops.md) | HIGH |

> **NOTE —** the `sync_events` table itself (`uint8[20]`, per-arch `sunda_sync_events @0x9de8a0` / `cayman @0x9def80` / `mariana @0x9df660`) and the full `tdrv_sync_get_*` accessor roster are owned by [tdrv-arch-ops §4.3](tdrv-arch-ops.md); the concrete per-generation semaphore *values* in those tables are that page's deep-dive candidate and are not re-derived here. This page documents how an id, once obtained, is encoded (`add_ev_*`) and addressed (`get_evt_addr`).

---

## Related Components

| Name | Relationship |
|---|---|
| `mem_ref_copy_and_stage_mr` (`0x2fb780`) | the staging bridge that places `MR_VIRTUAL_TMP_BUF` vars into the arena via `tdrv_scratchpad_get_mem`; owned by [memory-planning](../neff/memory-planning.md) |
| `dmem_alloc_aligned` / `dmem_alloc_contiguous_scratchpad_page` | the [dmem](tdrv-dmem.md) allocations a reserved page wraps (`SCRATCHPAD` usage, 4 MiB-aligned) |
| `vtpb_info_shared_init_vtpb` (`0x3148e0`) | the per-vnc load driver that calls `tdrv_scratchpad_model_init` then `mem_ref_copy_and_stage_mr` |
| `tdrv_arch_ops.sync_events` / `tdrv_sync_get_*` | the per-arch semaphore-id table + accessors the `add_ev_*` emitters consume ([tdrv-arch-ops](tdrv-arch-ops.md)) |
| `aws_hal_stpb_get_axi_offset` | rebases the per-arch event-register base into the addressed TPB's AXI window ([arch-csr-offsets](arch-csr-offsets.md)) |
| `kbin_mem_ref_set_t` | the plan header whose `local_scratchpad_size` / `shared_scratchpad_size` size the arena |

## Cross-References

- [TDRV: Tensor Object Layer](tdrv-tensor.md) — the `nrt_tensor_t` / `nrt_tensor_storage_t` that staged scratchpad-backed intermediates surface as at execute time, and the async-exec fence the sync-events complement
- [Static Memory Planning (mem_ref)](../neff/memory-planning.md) — the `mem_ref` owner: `mem_ref_copy_and_stage_mr`, the `MR_VIRTUAL_TMP_BUF` placement that drives `tdrv_scratchpad_get_mem`, and the plan header that sizes the arena
- [Per-Arch Device Layer: CSR and Register-Offset Accessors](arch-csr-offsets.md) — the per-arch CSR/aperture bases (EVT region `0x2700000` and the `+0x800000000` mesh high bit) the event-address arithmetic builds on, and `aws_hal_stpb_get_axi_offset`
- [TDRV: Device-Memory Allocator (dmem)](tdrv-dmem.md) — the `dmem_t` handle, `dmem_alloc_aligned`, and `dmem_alloc_contiguous_scratchpad_page` that back every scratchpad page
- [TDRV: arch-ops Dispatch and Sync-Event Accessors](tdrv-arch-ops.md) — the `tdrv_arch_ops` vtable that holds `get_evt_addr` / `get_evt_accel_addr` / `add_ev_*` / `sync_events`, and the `tdrv_sync_get_*` accessor family
- [back to index](../index.md)
