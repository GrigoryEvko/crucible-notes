# TDRV: Device-Memory Allocator (dmem)

> *All addresses, offsets, sizes, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; all four `PT_LOAD` segments are identity-mapped, so `.text`, `.rodata`, **and `.data` are VMA == file offset** (every `0x2…` is an analysis VMA; `.bss` is `NOBITS`). Provenance strings `/opt/workspace/KaenaRuntime/tdrv/dma_memory.c` and `/opt/workspace/KaenaRuntime/tdrv/dma_memory_logger.c` root every function. Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — struct layouts from the IDA `structures.json`, all four `dma_mem_*` enums from `enums.json`, alloc/free/copy logic from x86-64 disassembly, ioctl numbers read from the NDL leaf bodies, call edges from the IDA call graph. · Part IV — TDRV Runtime · [back to index](../index.md)*
>
> **CORRECTION —** an earlier revision of the line above claimed a `0x400000` `.data`/`.bss` VMA↔file-offset delta. That is **wrong** for `libnrt.so`. `readelf -lW` shows the RW `LOAD` segment as `LOAD 0xbeeaa0 ... 0xbeeaa0 ... RW` (Offset == VirtAddr, delta **zero**); `.data` sits at `Address 0xc07e00 / Off c07e00`. The `0x400000` delta belongs to a *different* binary (the libtpu / Kaena-profiler image), not libnrt — read `.data` globals at their VMA directly.

## Abstract

`tdrv/dma_memory.c` is the runtime's userspace device-memory allocator. It owns one object — the **`dmem_t` (192 B)** handle — and offers two services on it: *allocation* of a span of device DRAM (or host DRAM) through the kernel, and *synchronous host↔device copy* into and out of that span. Every weight, instruction block, tensor, scratchpad page, and DMA-ring buffer that a NeuronCore touches is backed by a `dmem_t`; the layers above (`tdrv/tensor.c`, the loader's `mem_ref` staging, the collectives encoder, the microcode platform-ops) never call the kernel directly — they call `dmem_alloc` / `dmem_free` / `dmem_buf_copyin` / `dmem_buf_copyout` and let this layer marshal the request.

The reference frame is a thin slab allocator sitting on top of an IOCTL boundary, with one twist a reimplementer must internalize: **this layer allocates no device bytes itself and copies no bytes itself.** It is a *bookkeeping and marshalling* layer. The actual allocation is a `MEM_ALLOC` ioctl issued by the **NDL** (Neuron Driver Layer) leaf `ndl_memory_alloc`; the actual DMA is a `MEM_COPY_ASYNC` ioctl issued by `ndl_memory_copy_as`. What `dma_memory.c` adds on top is: a `dmem_t` handle wrapping the opaque kernel handle plus its resolved physical address; a per-allocator intrusive doubly-linked list so a model or a TPB can free its allocations in bulk; a usage-type → sysfs-category projection; a refcount; a debug `hint` string; and — the second half of this page — the **DML (`dma_memory_logger`) subsystem**, a per-MLA running-counter and circular event log that every alloc and free updates so the runtime can render an OOM/utilization report when the kernel returns `ENOMEM`.

The page applies the recurring H3 vocabulary to five units: **(1)** the `dmem_t` object model and the allocator's intrusive list; **(2)** `dmem_alloc_internal` — the central allocation path and how it becomes a `MEM_ALLOC` ioctl through NDL; **(3)** `dmem_buf_copyin`/`copyout` and the chunked, double-buffered `dmem_device_copy` staging engine, and how it becomes a `MEM_COPY_ASYNC` ioctl; **(4)** `dmem_free` and the list/GC-list teardown; **(5)** the DML usage-tracking subsystem. The `dmem_t` is *referenced* by the tensor storage layer ([tdrv-tensor](tdrv-tensor.md)) and the staged `mem_ref` ([memory-planning](../neff/memory-planning.md)) — those pages own the structures that point *at* a `dmem_t`; this page owns the `dmem_t` itself and the allocator that produces it.

For reimplementation, the contract is:

- **The `dmem_t` handle (192 B)** — `{pcore, mem_handle, size, _pa, _va, align_offset, allocated_size, mem_type, mem_usage_type, tdram_channel, ref_count@128, hint@136, cpy_buf@144, next/prev@152/160, allocator@168}`. The opaque kernel `mem_handle` plus the resolved device physical address `_pa` are the two fields every consumer reads.
- **The allocator (`dmem_allocator_t`, 512 B)** — a mutex-guarded **intrusive doubly-linked list** with embedded `dummy_head`/`dummy_tail` sentinels; live `dmem_t` are spliced before `dummy_tail`. Two allocator kinds, `{GLOBAL=0, MODEL=1}`.
- **The NDL boundary** — `dmem_*` never issues a syscall; it calls `ndl_memory_alloc` (→ `MEM_ALLOC` ioctl `0x80384E66`), `ndl_memory_get_pa` (→ `0x80084E19`), `ndl_memory_copy_as` (→ `MEM_COPY_ASYNC` ioctl `0xC0384E6C`), `ndl_memory_buf_batch_copy` (→ `0xC0204E81`), `ndl_memory_free` (→ `0x80084E16`). The page pins each.
- **The copy chunking + double-buffer rule** — `dmem_device_copy` splits a transfer into ≤1 MiB chunks through a per-NeuronCore bounce buffer and overlaps each chunk's DMA with the previous chunk's host `memcpy`. The thresholds (`0x3FFF` / `0x100000` / half-page) are byte-exact.
- **The DML counter model** — every alloc/free updates `memory_usage[channel]` and `memory_usage_nc[nc][usage_type]` plus a per-NC grand-total column `[nc][22]`, and appends a 64-byte event to a fixed-size circular log (drop-counted when full).

| | |
|---|---|
| **Source** | `/opt/workspace/KaenaRuntime/tdrv/dma_memory.c`, `…/dma_memory_logger.c` |
| **Handle struct** | `dmem_t` — **192 B** (`calloc(0xC0, 1)`), DWARF ordinal 8568 |
| **Allocator struct** | `dmem_allocator_t` — **512 B** (`calloc(1, 0x200)`), ordinal 8569 |
| **Logger struct** | `dma_mem_log_t` — **1600 B** (`0x640`), ordinal 1313 |
| **Central alloc** | `dmem_alloc_internal` `@0x228640` (0x78e B) — resolve → ioctl → list-splice |
| **Free** | `dmem_free` `@0x2293a0` (0x3ab B) — refcount → unmap/free → unlink |
| **H2D / D2H chokepoint** | `dmem_buf_copyin` `@0x229820` · `dmem_buf_copyout` `@0x2299b0` |
| **Staging engine** | `dmem_device_copy` `@0x228090` (0x3da B) — chunked double-buffered ping-pong |
| **NDL alloc ioctl** | `ndl_memory_alloc` `@0xc2820` → `ioctl(fd, 0x80384E66, neuron_ioctl_mem_alloc_v2_mem_type64)` |
| **NDL copy ioctl** | `ndl_memory_copy_as` `@0xc2ee0` → `ioctl(fd, 0xC0384E6C, neuron_ioctl_mem_copy_async64)` |
| **Memory location** | `dma_mem_location_t` — `{INVALID=0, HOST_DRAM=1, TONGA_DRAM=2}` |
| **Usage type** | `dma_mem_usage_type_t` — 22 members, `GENERIC=0 … DMA_RING_COLLECTIVES=21`, `MAX=22` |

---

## 1. The `dmem_t` Object Model and the Allocator List

### Purpose

A `dmem_t` answers two questions for every layer above it: *what is the kernel handle for this allocation* (`mem_handle`, fed to every NDL call) and *what is its device physical address* (`_pa`, the absolute address the silicon dereferences). Everything else on the handle is bookkeeping: a refcount so a shared allocation outlives its first owner, a `hint` for OOM diagnostics, a cached per-NeuronCore bounce buffer for non-zerocopy DMA, and the intrusive list links by which the owning allocator can walk or bulk-free its allocations. The allocator itself is deliberately the simplest possible container — a mutex plus a sentinel-bracketed doubly-linked list — because allocation is rare (load-time, not per-inference) and the kernel does the real placement.

### `dmem_t` — the handle (192 B)

All offsets are DWARF `data_member_location` (decimal), confirmed against `calloc(0xC0,1)` in three allocators and field-by-field against the disassembly.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `pcore` | +0 (0x00) | `const physical_core_t*` | owning core (`allocator->pcore`, or the passed `pcore` for scratchpad) | HIGH |
| `mem_handle` | +8 (0x08) | `uint64_t` | **opaque NDL/kernel allocation handle**; `0` = none; passed to every `ndl_memory_*` | HIGH |
| `size` | +16 (0x10) | `size_t` | logical (requested) size; in the aligned-fallback path this is the *pre-align* size | HIGH |
| `_pa` | +24 (0x18) | `uint64_t` | **device physical address** from `ndl_memory_get_pa`; the value consumers add offsets to | HIGH |
| `_va` | +32 (0x20) | `volatile void*` | mapped host VA; `0` until `dmem_mmap`; never deref raw — use `DMEM_GET_VA` | HIGH |
| `align_offset` | +40 (0x28) | `size_t` | bytes from `_pa`/`_va` to the aligned base; `0` unless the manual-align fallback ran | HIGH |
| `allocated_size` | +48 (0x30) | `size_t` | actual bytes the driver returned (`>= size`); the value DML counters sum | HIGH |
| `mem_type` | +56 (0x38) | `dma_mem_location_t` | `{INVALID=0, HOST_DRAM=1, TONGA_DRAM=2}` | HIGH |
| `mem_usage_type` | +60 (0x3C) | `dma_mem_usage_type_t` | `0..21` (§1 usage table) | HIGH |
| `tdram_channel` | +64 (0x40) | `uint32_t` | HBM channel (`< MAX_HBM=4`); also packed into the trace payload | HIGH |
| `ref_count` | +128 (0x80) | `volatile uint64_t` | atomic; init `1`; `dmem_acquire_reference` `++`, `dmem_free` `--`, frees at `0` | HIGH |
| `hint` | +136 (0x88) | `char*` | `strdup`'d debug tag; freed in `dmem_free` | HIGH |
| `cpy_buf` | +144 (0x90) | `ndl_copy_buf_t*` | cached per-NeuronCore DMA bounce buffer (from `ndl_get_copy_buf`) | HIGH |
| `next` | +152 (0x98) | `dmem*` | intrusive allocator-list forward link | HIGH |
| `prev` | +160 (0xA0) | `dmem*` | intrusive allocator-list back link | HIGH |
| `allocator` | +168 (0xA8) | `dmem_allocator_t*` | owning allocator; `NULL` after `dmem_allocator_destroy` unlinks it | HIGH |

> **QUIRK —** the gap `0x44..0x7F` is **not** read or written by any function in this layer. The struct is 192 bytes because `ref_count` is placed on its own 64-byte line at `+0x80` (so the atomic `lock`-prefixed RMW never shares a cache line with the hot `mem_handle`/`_pa` fields a concurrent copy reads). A reimplementer can pack the fields tighter, but losing that separation will false-share the refcount against every `dmem_buf_copyin`.

### `dmem_allocator_t` — the intrusive list (512 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `type` | +0 (0x00) | `dmem_allocator_type_t` | `{GLOBAL=0, MODEL=1}` | HIGH |
| `pcore` | +8 (0x08) | `const physical_core_t*` | owning core | HIGH |
| `lock` | +16 (0x10) | `pthread_mutex_t` (40 B) | guards the list (held in every splice/unlink) | HIGH |
| `dummy_head` | +64 (0x40) | `dmem` (sentinel) | `dummy_head.next` → first live node | HIGH |
| `dummy_tail` | +256 (0x100) | `dmem` (sentinel) | live nodes spliced before this; walk stops at `&dummy_tail` | HIGH |
| `largest_oversized_scratchpad_var` | +448 (0x1C0) | `size_t` | informational; not written by any in-scope function (MED) | MED |

> **NOTE —** `dmem_allocator_create` (`@0x2284c0`) stashes the `pcore` pointer using the embedded sentinels' own field layout rather than the named `pcore@+8`: it computes `p_dummy_tail = &allocator->dummy_tail` and writes `p_dummy_tail[-2].hint = pcore` — i.e. it stores the core pointer into the `hint` slot (`+0x88`) of the node *two `dmem`-strides before* `dummy_tail`, which lands exactly on `allocator->pcore@+8` (`0x100 − 2*0xC0 + 0x88 = 0x08`). The decompiler renders the named-field write as sentinel-array arithmetic; the destination is the real `pcore` field. The list is then seeded `dummy_head.next = &dummy_tail`, `dummy_tail.prev = &dummy_head` (an empty list bracketed by both sentinels), and the mutex is `pthread_mutex_init`'d.

### The intrusive list invariant

```c
// The splice used by dmem_alloc_internal / _scratchpad_page (under allocator->lock).
// Live nodes are appended at the tail: inserted *before* dummy_tail.
prev               = allocator->dummy_tail.prev;   // last live node (or dummy_head if empty)
prev->next         = mem;
mem->prev          = prev;
allocator->dummy_tail.prev = mem;
mem->next          = &allocator->dummy_tail;
mem->allocator     = allocator;                    // back-pointer for O(1) unlink in dmem_free
```

Because both ends are sentinels, neither insert nor unlink needs a NULL-check on the neighbours — the `dummy_head`/`dummy_tail` always exist. `dmem_free` exploits this for an O(1) unlink that asserts `mem->prev`/`mem->next` are non-NULL (`dma_memory.c:0x29C/0x29D`) precisely because a correctly-listed node *cannot* have NULL links; a NULL there means a double-free or a corrupted node.

### Usage type → sysfs category

`dma_mem_usage_type_t` has 22 members (`enums.json`-verified): `GENERIC=0, INSTR_PE=1, INSTR_ACT=2, INSTR_POOL=3, IO=4, DRAM_SPILL=5, WEIGHT=6, NOTIFICATION=7, ACT_TABLE=8, INSTR_DVE=9, INSTR_SP=10, SCRATCHPAD=11, TENSOR=12, UCODE_LIB=13, POOL_STDIO=14, CC=15, SCRATCHPAD_NOT_SHARED=16, XT_CC=17, DMA_RING=18, DMA_RING_SPILL=19, DMA_RING_IO=20, DMA_RING_COLLECTIVES=21`, `MAX=22`.

Allocation maps the usage type to a kernel **category byte** via one of two 22-byte `.rodata` tables — `dmem_usage_type_to_host_category` (`@0x9b9640`) for `HOST_DRAM`, `dmem_usage_type_to_device_category` (`@0x9b9660`) for `TONGA_DRAM`. The category sentinel **20** means *"this usage type is invalid for this location"*: `SCRATCHPAD`, `CC`, and `SCRATCHPAD_NOT_SHARED` map to host-category `20` (device-only usages), so requesting them on `HOST_DRAM` is rejected with `NRT_INVALID` and `"Invalid mem_usage_type %d for host"`. The category byte — not the usage type — is what is passed to the kernel as `mem_alloc_type`.

> **GOTCHA —** the usage-type index space and the category-byte index space overlap numerically but are **distinct**. `dml_add_entry` indexes its per-NC counter matrix by the *usage type* (`0..21`); `ndl_memory_alloc` receives the *category byte*. A reimplementer who passes the usage type to the kernel, or who indexes the DML matrix by the category, will mis-account every allocation. The projection is one-directional and lossy (22 usages → ~17 categories).

### Function Map — object model

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `dmem_allocator_create` | `0x2284c0` | `calloc(0x200)`; seed sentinels; init mutex; stash `pcore` | HIGH |
| `dmem_allocator_destroy` | `0x228550` | walk live list clearing `allocator` back-ptr; destroy mutex; free | HIGH |
| `dmem_acquire_reference` | `0x229390` | atomic `++ref_count`; return `mem` | HIGH |
| `dmem_mmap` | `0x228dd0` | lazy `ndl_memory_map`; cache `_va` (idempotent) | HIGH |
| `DMEM_GET_VA` | `0x228e50` | `dmem_mmap` then return `_va + align_offset` (NULL on failure) | HIGH |
| `dma_mem_usage_to_name` | `0x228480` | `&names[32*usage]`; asserts `usage < MAX` | HIGH |
| `dma_memory_init` | `0x228470` | set global `enable_metrics`; return `NRT_SUCCESS` | HIGH |

> **GOTCHA —** `dmem_allocator_destroy` **unlinks but does not free** the live `dmem_t` nodes — it only clears each node's `allocator` back-pointer (`*(_OWORD*)&node->next = 0` zeroes `next`/`prev` too) and frees the *allocator*, not the handles. Every owning subsystem (kbl, ioqs, pool_stdio, ucode) must `dmem_free` its allocations *before* tearing down the allocator, or the device DRAM leaks. This is by design (the allocator does not own the lifetime of what it lists), but it is a teardown-order trap.

---

## 2. Allocation: `dmem_alloc_internal` and the `MEM_ALLOC` ioctl

### Purpose

`dmem_alloc_internal` is the one path that turns a `(size, mem_type, channel, usage)` request into a live `dmem_t`. The two thin wrappers (`dmem_alloc` with `align=0`, `dmem_alloc_aligned` with a driver-capability fallback) both funnel here. Its job is sequencing, not allocation: resolve the owning MLA/TPB, project the usage type to a category, `calloc` the handle, ask NDL to allocate device bytes (the ioctl), resolve the physical address, cache the bounce buffer, account the allocation in DML/NDS/trace, and splice the handle into the allocator list.

### Entry Point

```text
caller (tensor_allocate / hw_exec_queue_add_exec_request / dma_ring_info_alloc / 14 more)
  └─ dmem_alloc (0x228ed0)                       ── align=0 wrapper; asserts *mem!=NULL on success
       └─ dmem_alloc_internal (0x228640)         ── CORE
            ├─ db_physical_core_get_mla_and_tpb (0x2272a0)   resolve mla + tpb
            ├─ dmem_usage_type_to_{host|device}_category[usage]   project; 20 ⇒ reject
            ├─ calloc(0xC0, 1)                                the dmem_t
            ├─ ndl_memory_alloc (0xc2820)         ── ioctl 0x80384E66  [NDL → KERNEL]
            ├─ ndl_memory_get_pa (0xc2a00)        ── ioctl 0x80084E19  [NDL → KERNEL]
            ├─ ndl_get_copy_buf (0xc44d0)         ── pointer into device->cpy_bufs[nc] (no syscall)
            ├─ dml_add_entry (0x22ab80) / tdrv_nds_register_mem_alloc / ntrace_record_event
            └─ splice before allocator->dummy_tail; optional dmem_list_add(ref_list)

dma_ring_info_alloc / pool_stdio / ucode (aligned)
  └─ dmem_alloc_aligned (0x228f20)               ── driver-align or over-alloc + manual align_offset
```

### Algorithm — `dmem_alloc_internal`

```c
// dmem_alloc_internal @0x228640 — the central allocation path.
// On entry pcore = allocator->pcore. Trace event type 34 brackets the whole op.
function dmem_alloc_internal(allocator, mem, size, align, mem_type,
                             tdram_channel, ref_list, mem_usage_type, hint):
    if db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb) != 0:        // 0x2272a0
        nlog("Failed to get a valid Neuron Core"); return <err>
    tid = nrt_sys_trace_new_event(begin, 34, …, vtpb_idx, …)            // alloc-begin marker
    nc_id = tpb->idx
    device = mla->device                                                // +314664 (loc_4CD28)

    // (1) usage -> sysfs category byte; sentinel 20 = invalid-for-location
    if mem_type != HOST_DRAM:
        cat = dmem_usage_type_to_device_category[mem_usage_type]        // @0x9b9660
        if cat == 20: nlog("Invalid mem_usage_type %d for device"); return NRT_INVALID
    else:
        cat = dmem_usage_type_to_host_category[mem_usage_type]          // @0x9b9640
        if cat == 20: nlog("Invalid mem_usage_type %d for host"); return NRT_INVALID

    // (2) the handle
    *mem = calloc(0xC0, 1)
    if !*mem: nlog("Failed to allocate dmem_t"); return NRT_RESOURCE

    // (3) THE KERNEL ALLOCATION — ndl_memory_alloc issues MEM_ALLOC ioctl 0x80384E66
    rc = ndl_memory_alloc(device, size, align, /*host_memory=*/(mem_type==HOST_DRAM),
                          tdram_channel, /*dram_region=*/0, nc_id, cat, &mem_handle)  // 0xc2820
    err = errno
    if rc != 0:
        // alloc-stop trace; on ENOMEM, dump the DML utilization report (§5)
        nrt_sys_trace_new_event(stop, 34, tid, …)
        free(*mem); *mem = NULL
        if err == 12 /*ENOMEM*/:
            dml_dump(&mla->dmem_logger)
            if mem_type == HOST_DRAM:
                dml_log_host_mem(size, mem_usage_type);  return NRT_FAIL_HOST_MEM_ALLOC
            else:
                dml_driver_dump(device, tdram_channel)
                if align: nlog("Aligned allocations may fail even if HBM has space "
                               "for the actual size requested, due to running out of "
                               "alignment boundaries")
                dml_log_dev_mem(&mla->dmem_logger, pcore, tdram_channel, 1,
                                size, align, mem_usage_type)              // §5 OOM report
                return NRT_RESOURCE
        return NRT_FAILURE

    // (4) resolve physical address — ndl_memory_get_pa issues ioctl 0x80084E19
    mem->mem_handle = mem_handle; mem->size = size
    if ndl_memory_get_pa(mem_handle, &mem->_pa) != 0:                    // 0xc2a00
        nlog("Failed to get the phys address"); /* free + ENOMEM-style return */

    // (5) populate the handle
    mem->_va = 0                                                          // mapped lazily
    mem->mem_usage_type = mem_usage_type; mem->mem_type = mem_type
    mem->tdram_channel  = tdram_channel;  mem->pcore = pcore
    mem->ref_count      = 1
    mem->hint           = hint ? strdup(hint) : NULL

    // (6) cache the per-NeuronCore bounce buffer (pointer into device->cpy_bufs[nc], no syscall)
    if ndl_get_copy_buf(device, nc_id, &mem->cpy_buf) != 0:              // 0xc44d0
        nlog("Failed to get copy buffer for nc %u", nc_id); return NRT_FAILURE

    // (7) accounting: DML counters + circular log, NDS metric, ntrace, sys-trace
    if dml_add_entry(&mla->dmem_logger, *mem, DMEM_ACTION_ALLOC):        // 0x22ab80 (§5)
        nlog("Failed to add entry to dmem log")
    if enable_metrics && tdrv_nds_register_mem_alloc(device_id, nc_id, mem_usage_type,
                                                     mem_type, mem->allocated_size):
        nlog("Unable to log memory allocation to NDS for device %d, type %d, location %d, amount %lu")
    ntrace_record_event(NTRACE_DATA_TYPE_MEM_ALLOC, mem->allocated_size, mla->mla_idx, 0, mem->hint)
    nlog_TRACE("MEM_USAGE: %lu bytes, channel: %u, total usage: (%lu/%lu), hint:%s", …)
    nrt_sys_trace_new_event(stop, 34, tid, …)

    // (8) splice into the allocator list (under lock), and optional GC list
    lock(allocator->lock)
        splice *mem before allocator->dummy_tail; (*mem)->allocator = allocator
    unlock(allocator->lock)
    if ref_list: dmem_list_add(ref_list, *mem)                          // §4
    return NRT_SUCCESS
```

> **NOTE —** the `host_memory` argument to `ndl_memory_alloc` is computed as `(mem_type == HOST_DRAM)`, i.e. it is `1` for `HOST_DRAM` and `0` for `TONGA_DRAM`. The kernel's `MEM_ALLOC` then either pins host pages or carves device DRAM on the named `tdram_channel`. The `dram_region` argument is hard-wired `0` from this layer — region selection is not exposed above NDL.

### How `ndl_memory_alloc` becomes a `MEM_ALLOC` ioctl

This layer's entire kernel interaction is mediated by NDL. The allocation leaf is byte-anchored:

```c
// ndl_memory_alloc @0xc2820 — the MEM_ALLOC ioctl. (Compat-mux elided.)
function ndl_memory_alloc(device, size, align, host_memory, dram_channel,
                          dram_region, nc_id, mem_alloc_type, out_handle):
    *out_handle = 0
    h = calloc(0x40, 1)                       // the kernel-side handle record (64 B)
    if !h: errno = 12; return -1
    h.device = device; h.nc_id = nc_id; h.size = size; h.align = align
    h.host_memory = host_memory
    fd = *(int*)device->context               // the /dev/neuron* fd
    arg = (neuron_ioctl_mem_alloc_v2_mem_type64){
              .mem_handle = &h.<handle_word>, .mem_type = mem_alloc_type,
              .host_memory = host_memory, .dram_region = dram_region,
              .size = size, .align = align, .dram_channel = dram_channel, .nc_id = nc_id }
    rc = ioctl(fd, 0x80384E66, &arg)          // <-- MEM_ALLOC_V2 (mem_type64) ioctl
    if rc:  free(h);  /* maybe fall back to a 32-bit-size compat path */
    else:   *out_handle = (uint64_t)h         // opaque handle = pointer to the 64 B record
    return rc
```

The `dmem_t.mem_handle` the runtime carries is therefore a **pointer to a 64-byte NDL handle record**, not a kernel cookie directly; the kernel cookie lives inside it, and `ndl_memory_get_pa`/`_free`/`_map` re-enter through the same `device->context` fd stored at offset `+632` of the record (`ioctl(*(int*)(*(uint64_t*)mem_handle + 632), …)`). The four ioctl numbers this layer ultimately drives:

| NDL leaf | Addr | ioctl number | ioctl arg struct | Driven by |
|---|---|---|---|---|
| `ndl_memory_alloc` | `0xc2820` | `0x80384E66` | `neuron_ioctl_mem_alloc_v2_mem_type64` | `dmem_alloc_internal` |
| `ndl_alloc_contiguous_scratchpad` | `0xc53c0` | `0x80384E66` | (same alloc arg) | `dmem_alloc_contiguous_scratchpad_page` |
| `ndl_memory_get_pa` | `0xc2a00` | `0x80084E19` | (get-pa arg) | `dmem_alloc_internal`, `_scratchpad_page` |
| `ndl_memory_free` | `0xc2ba0` | `0x80084E16` | (free arg) | `dmem_free`, scratchpad error path |
| `ndl_memory_copy_as` | `0xc2ee0` | `0xC0384E6C` | `neuron_ioctl_mem_copy_async64` | `dmem_device_copy` |
| `ndl_memory_buf_batch_copy` | `0xc5a30` | `0xC0204E81` | (batch-copy arg) | `dmem_batch_transfer_common` |

> **GOTCHA —** `ndl_get_copy_buf` (`@0xc44d0`) is **not** a kernel call and allocates nothing — it returns `&device->cpy_bufs[nc_id]`, a pointer into an inline `ndl_copy_buf_t[8]` array embedded in `ndl_device_t` at `+104`. The DMA bounce buffer for a NeuronCore is allocated once, at device open, and *shared* by every `dmem_t` on that core (serialised by `ndl_copy_buf_t.lock@+16`). A reimplementer who allocates a fresh staging buffer per `dmem_t` will waste device-mapped host memory and break the per-NC serialisation `dmem_device_copy` relies on.

### Algorithm — `dmem_alloc_aligned` (driver-capability fallback)

```c
// dmem_alloc_aligned @0x228f20 — alignment with a graceful driver fallback.
// Global ndl_supports_align_12 latches the driver's alignment capability.
function dmem_alloc_aligned(allocator, mem, size, mem_type, chan,
                            ref_list, usage, hint, alignment):
    if ndl_supports_align_12:
        rc = dmem_alloc_internal(..., align=alignment, ...)
        if rc == NRT_SUCCESS: assert(*mem != NULL); return NRT_SUCCESS
        if rc != NRT_FAILURE: return rc
        // driver lacks the aligned-alloc API → warn once, latch off, fall through
        nlog("(Non-FATAL) aligned memory allocation API not available in driver. "
             "Please consider upgrading driver for more efficient memory allocation.")
        ndl_supports_align_12 = 0
    // over-allocate by `alignment` and align by hand
    rc = dmem_alloc_internal(..., size + alignment, align=0, ...)
    if rc: return rc
    if alignment:
        pa = (*mem)->_pa
        (*mem)->size         = size                                     // restore logical size
        (*mem)->align_offset = ((pa + alignment) & -alignment) - pa     // bytes to aligned base
    else assert(*mem != NULL)
    return NRT_SUCCESS
```

The manual `align_offset` is the bridge that lets every consumer treat an unaligned kernel allocation as aligned: `DMEM_GET_VA` returns `_va + align_offset`, `dmem_buf_copyin` adds `align_offset` into every device offset, and `tensor_get_pa` adds `_pa + align_offset`. The over-allocated prefix is dead space the runtime never addresses.

### Algorithm — `dmem_alloc_contiguous_scratchpad_page` (direct NDL bypass)

```c
// dmem_alloc_contiguous_scratchpad_page @0x229080 — used by tdrv_scratchpad_model_init.
// Bypasses dmem_alloc_internal: a *contiguous* scratchpad page via a distinct NDL leaf.
function dmem_alloc_contiguous_scratchpad_page(pcore, size, mem):
    db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)
    hbm = get_default_hbm_index(pcore->device_tpb_idx)
    *mem = calloc(0xC0, 1)
    rc = ndl_alloc_contiguous_scratchpad(mla->device, size, hbm,
                                         pcore->device_tpb_idx, &mem_handle)   // 0xc53c0
    if rc: { if mem_handle: ndl_memory_free(mem_handle); free(*mem); *mem=0;
             return (rc == -12) ? NRT_RESOURCE : NRT_FAILURE }
    (*mem)->size = size
    ndl_memory_get_pa(mem_handle, &(*mem)->_pa)
    (*mem)->_va = 0
    *(uint64_t*)&(*mem)->mem_type = 0xB00000002          // mem_type=TONGA_DRAM(2) | usage=SCRATCHPAD(0xB)<<32
    (*mem)->tdram_channel = hbm; (*mem)->pcore = pcore; (*mem)->ref_count = 1
    dml_add_entry(&mla->dmem_logger, *mem, DMEM_ACTION_ALLOC)
    /* enable_metrics → tdrv_nds_register_mem_alloc */
    // splice into tpb->tpb_allocator (NOT a caller-passed allocator)
    lock(tpb->tpb_allocator->lock); splice; unlock
    return NRT_SUCCESS
```

> **QUIRK —** the `0xB00000002` store sets `mem_type` (`+0x38`) and `mem_usage_type` (`+0x3C`) in a single 8-byte write: `0x2` is `TONGA_DRAM`, `0xB << 32` is `SCRATCHPAD=11` in the adjacent dword. And the page is spliced into `tpb->tpb_allocator`, **not** any allocator the caller named — scratchpad pages are owned by the TPB, not the model. A reimplementer who routes them to the model allocator will free them at model-unload while the TPB still references them.

### Function Map — allocation

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `dmem_alloc_internal` | `0x228640` | 0x78e | Central path: resolve → ioctl → get_pa → account → splice | HIGH |
| `dmem_alloc` | `0x228ed0` | 0x04f | `align=0` wrapper; asserts `*mem!=NULL` on success | HIGH |
| `dmem_alloc_aligned` | `0x228f20` | 0x15f | Driver-aligned or over-alloc + manual `align_offset` | HIGH |
| `dmem_alloc_contiguous_scratchpad_page` | `0x229080` | 0x310 | Direct `ndl_alloc_contiguous_scratchpad`; → `tpb_allocator` | HIGH |
| `ndl_memory_alloc` | `0xc2820` | — | `MEM_ALLOC` ioctl `0x80384E66` (boundary) | HIGH |
| `ndl_memory_get_pa` | `0xc2a00` | — | get-PA ioctl `0x80084E19` (boundary) | HIGH |
| `ndl_get_copy_buf` | `0xc44d0` | — | accessor → `device->cpy_bufs[nc]` (no syscall) | HIGH |

---

## 3. Transfer: the Copy Chokepoints and `dmem_device_copy`

### Purpose

Two public chokepoints move bytes between a host buffer and a `dmem_t`: `dmem_buf_copyin` (H2D) and `dmem_buf_copyout` (D2H). Each is a three-way dispatch on `mem_type` and a global zerocopy flag; for the common `TONGA_DRAM`-without-zerocopy case both fall through to `dmem_device_copy`, the staging engine that owns the chunking and the double-buffered overlap. A batch variant (`dmem_batch_transfer_common`) coalesces many tensors' ops into a single driver batch ioctl. Every transfer applies `mem->align_offset` and is bracketed by a sys-trace event (type 30 copyin, 31 copyout).

### Entry Point

```text
tensor_write / mem_ref_copy_and_stage_mr / ucode write / pool_stdio … (16+ callers)
  └─ dmem_buf_copyin (0x229820)                  ── trace evt 30
       ├─ [HOST_DRAM]        ndl_memory_buf_copyin       (0xc2ce0)   [NDL → KERNEL]
       ├─ [TONGA+zerocopy]   ndl_memory_buf_zerocopyin   (0xc48d0)   [NDL → KERNEL]
       └─ [TONGA else]       dmem_device_copy (..., copyin=1) (0x228090)
              └─ ndl_memory_copy_as (0xc2ee0)  ── MEM_COPY_ASYNC ioctl 0xC0384E6C  [NDL → KERNEL]

tensor_read / exec debug-tensor read / pool_stdio consume … 
  └─ dmem_buf_copyout (0x2299b0)                 ── trace evt 31
       ├─ [zerocopy]         ndl_memory_buf_zerocopyout  (0xc48a0)
       ├─ [HOST_DRAM]        ndl_memory_buf_copyout      (0xc2cf0)
       └─ [TONGA else]       dmem_device_copy (..., copyin=0) (0x228090)

tensor_write_batch → dmem_buf_batch_copyin (0x228eb0) ─┐
tensor_read_batch  → dmem_buf_batch_copyout(0x228ec0) ─┴→ dmem_batch_transfer_common (0x227d80)
       └─ ndl_memory_buf_batch_copy (0xc5a30) ── batch ioctl 0xC0204E81
```

### Algorithm — `dmem_buf_copyin` / `dmem_buf_copyout` (the dispatch)

```c
// dmem_buf_copyin @0x229820 — H2D chokepoint. Trace event 30 brackets it.
function dmem_buf_copyin(mem, buffer, offset, size):
    tid = nrt_sys_trace_new_event(begin, 30, …, vtpb_idx, …)
    if mem->mem_type == HOST_DRAM:                                       // host buffer: direct
        rc = ndl_memory_buf_copyin(mem->mem_handle, buffer, offset + mem->align_offset, size)
    else if nrt_gconf()->test_zerocopy:                                  // BAR4 direct path
        rc = ndl_memory_buf_zerocopyin(mem->mem_handle, buffer, offset + mem->align_offset,
                                       size, -1, gconf->direct_bar4_write_size)
    else:                                                                // staged DMA
        rc = dmem_device_copy(mem, buffer, offset, size, /*copyin=*/1)
    nrt_sys_trace_new_event(stop, 30, tid, …)
    if rc: nlog("Copy from buffer to memory failed"); return NRT_FAILURE
    return NRT_SUCCESS

// dmem_buf_copyout @0x2299b0 — D2H chokepoint. Trace event 31.
function dmem_buf_copyout(mem, buffer, offset, size):
    tid = nrt_sys_trace_new_event(begin, 31, …)
    if nrt_gconf()->test_zerocopy:                                       // zerocopy checked FIRST
        rc = ndl_memory_buf_zerocopyout(mem->mem_handle, buffer, offset + mem->align_offset, size, -1)
    else if mem->mem_type == HOST_DRAM:
        rc = ndl_memory_buf_copyout(mem->mem_handle, buffer, offset + mem->align_offset, size)
    else:
        rc = dmem_device_copy(mem, buffer, offset, size, /*copyin=*/0)
    nrt_sys_trace_new_event(stop, 31, tid, …)
    if rc: nlog("Copy from memory  to buffer failed"); return NRT_FAILURE   // sic: double space
    return NRT_SUCCESS
```

> **QUIRK —** the dispatch order is **asymmetric**: `copyin` tests `mem_type == HOST_DRAM` first, then zerocopy; `copyout` tests `test_zerocopy` first, then `HOST_DRAM`. This is real (both disassemblies confirm it), not a transcription error. The practical consequence: with `test_zerocopy` set, a `HOST_DRAM` allocation still takes the *direct* `buf_copyin` path on the way in (host memory needs no zerocopy DMA) but the *zerocopyout* path on the way out. A reimplementer who symmetrises the two branches will route host-buffer reads through a device DMA path they should never touch.

### Algorithm — `dmem_device_copy` (chunked double-buffered staging)

This is the subtle one. Because a `dmem_t`'s device DRAM is not directly host-addressable (without zerocopy), every byte is staged through the per-NeuronCore bounce buffer (`cpy_buf->mmap_va`). To hide DMA latency, the engine **overlaps** chunk *i*'s in-flight DMA with chunk *i−1*'s host `memcpy` — a software pipeline of depth 2.

```c
// dmem_device_copy @0x228090 — staged H2D/D2H copy through mem->cpy_buf, double-buffered.
// chunk-size and wait-handle are chosen by total size; see the threshold table below.
function dmem_device_copy(mem, buffer, offset, size, copyin):
    cpy_buf = mem->cpy_buf
    // (1) pick chunk size and async wait-handle mode from total size
    if size <= 0x3FFF:                  chunk = 0x4000;  wait_handle = 0     // whole, single pass
    elif size > 0x100000:               chunk = 0x100000; wait_handle = -1   // 1 MiB, double-buffered
    else:                               chunk = ((size>>1)+0xFFF) & ~0xFFF   // ~half, page-rounded
                                        wait_handle = (chunk >= size) ? 0 : -1
    assert(cpy_buf != NULL)                       // dma_memory.c:0x2E6
    lock(cpy_buf->lock)                           // per-NC bounce buffer is shared → serialise

    // (2) pipeline. `i` = cpy_buf write offset of the IN-FLIGHT chunk;
    //               prev_* = the staged chunk whose host memcpy is still owed.
    prev_bytes_copied = 0; prev_cpy_buf_offset = 0; prev_cpy_size = 0
    remaining = size; src_off = 0; cpy_off = 0
    for (;;):
        n = min(chunk, remaining)
        host = buffer + cpy_off
        if copyin:
            memcpy(cpy_buf->mmap_va + cpy_off_in_buf, host, n)     // host -> staging
            // launch async DMA staging -> device; ping-pong: next chunk uses the other half
            if ndl_memory_copy_as(cpy_buf->mem_handle, mem->mem_handle,
                                  cpy_off_in_buf, cpy_off + mem->align_offset + offset,
                                  n, 0, wait_handle):              // 0xC0384E6C MEM_COPY_ASYNC
                nlog("Failed to copy in buffer to dmem"); goto drain_fail
        else: // copyout
            prev_cpy_size = <bytes staged by previous DMA>; prev_cpy_buf_offset = <its buf offset>
            // launch async DMA device -> staging for THIS chunk
            if ndl_memory_copy_as(mem->mem_handle, cpy_buf->mem_handle,
                                  cpy_off + mem->align_offset + offset, cpy_off_in_buf,
                                  n, host, wait_handle):
                nlog("Failed to copy out dmem to buffer")
                if ndl_memory_copy_as_wait(mem->mem_handle, wait_handle) == 0:
                    // recover the PREVIOUS chunk's staged bytes before bailing
                    memcpy(buffer + prev_bytes_copied,
                           cpy_buf->mmap_va + prev_cpy_buf_offset, prev_cpy_size)
                goto fail
            // OVERLAP: while THIS DMA is in flight, drain the PREVIOUS chunk to host
            if prev_cpy_size:
                memcpy(buffer + prev_bytes_copied,
                       cpy_buf->mmap_va + prev_cpy_buf_offset, prev_cpy_size)

        prev_bytes_copied = cpy_off
        remaining -= n
        if remaining == 0:
            if ndl_memory_copy_as_wait(mem->mem_handle, wait_handle) != 0: goto fail
            if !copyin:
                memcpy(buffer + prev_bytes_copied, cpy_buf->mmap_va + last_buf_off, n)  // final drain
            break
        cpy_off += n; toggle cpy_off_in_buf between the two halves
    unlock(cpy_buf->lock); return NRT_SUCCESS
fail: drain_fail:
    unlock(cpy_buf->lock); return NRT_FAILURE
```

| Total `size` | Chunk | `wait_handle` | Behaviour |
|---|---|---|---|
| `≤ 0x3FFF` (16 KiB−1) | `0x4000` | `0` | single pass, no overlap |
| `0x4000 .. 0x100000` | `((size>>1)+0xFFF) & ~0xFFF` (≈half, page-rounded) | `0` if chunk≥size else `-1` | double-buffered if it does not fit in one chunk |
| `> 0x100000` (1 MiB) | `0x100000` | `-1` | always double-buffered, 1 MiB chunks |

> **GOTCHA —** on the `copyout` path the overlap means at any instant *two* chunks are alive in the bounce buffer: the one whose DMA is in flight and the one being `memcpy`'d to the host. The error-recovery branch deliberately `memcpy`s **`prev_cpy_size` from `prev_cpy_buf_offset`** — the *previously staged* chunk, not the one whose DMA just failed — so a mid-transfer failure does not drop the last successfully-staged chunk. A reimplementer who, on error, copies the *current* chunk's offset/size will either drop or duplicate a chunk's worth of output. (This is the off-by-one flagged for re-derivation in the source notes; the disassembly resolves it as above — HIGH.)

### Algorithm — `dmem_batch_transfer_common` (one ioctl for many tensors)

```c
// dmem_batch_transfer_common @0x227d80 — coalesce N tensors' ops into ONE driver batch ioctl.
function dmem_batch_transfer_common(tensor_batches, num_batches, is_copy_to_device, trace_type):
    if !tensor_batches || !num_batches:
        nlog("Invalid arguments: tensor_batches is NULL or num_batches is 0"); return NRT_INVALID
    driver_batches = calloc(num_batches, 0x28)         // neuron_memcpy_batch_t[]
    if !driver_batches: nlog("Failed to allocate memory for NDL batches"); return NRT_RESOURCE
    for b in tensor_batches:
        dmem = b.tensor->sto->dmem
        out.mem_handle        = dmem->mem_handle
        out.mem_handle_offset = dmem->align_offset + b.tensor->_offset
        out.ops_ptr           = b.ops; out.num_ops = b.num_ops
        out.bar4_wr_threshold = nrt_gconf()->direct_bar4_write_size
        out.flags = 0; out.context = 0
    tid = nrt_sys_trace_new_event(begin, trace_type, …)
    rc = ndl_memory_buf_batch_copy(driver_batches, num_batches,
                                   /*dir=*/is_copy_to_device, -1)    // 0xC0204E81 batch ioctl
    if rc: nlog("NDL batch copy (%s) failed: %d", is_copy_to_device?"copyin":"copyout", rc)
    nrt_sys_trace_new_event(stop, trace_type, tid, …)
    free(driver_batches)
    return rc ? NRT_FAILURE : NRT_SUCCESS
```

The batch path reads the backing `dmem_t` *through the tensor storage* (`b.tensor->sto->dmem`) — it is the one place this layer reaches into the tensor object ([tdrv-tensor](tdrv-tensor.md) owns `nrt_tensor_storage_t`). It builds one `neuron_memcpy_batch_t` (40 B: `mem_handle@0`, `mem_handle_offset@8`, `ops_ptr@16`, `num_ops@24`, `bar4_wr_threshold@28`, `flags@30`, `context@32`) per tensor and submits the whole array in a single ioctl, amortising the syscall over many small per-op transfers.

### Function Map — transfer

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `dmem_buf_copyin` | `0x229820` | — | H2D dispatch; HOST/zerocopy/staged; trace 30 | HIGH |
| `dmem_buf_copyout` | `0x2299b0` | — | D2H dispatch; zerocopy-first; trace 31 | HIGH |
| `dmem_device_copy` | `0x228090` | 0x3da | Chunked double-buffered staging through `cpy_buf` | HIGH |
| `dmem_batch_transfer_common` | `0x227d80` | 0x30b | Build driver-batch array; one `buf_batch_copy` ioctl | HIGH |
| `dmem_buf_batch_copyin` | `0x228eb0` | 0x00f | wrapper → `…common(…, copyin)` | HIGH |
| `dmem_buf_batch_copyout` | `0x228ec0` | 0x00c | wrapper → `…common(…, copyout)` | HIGH |
| `ndl_memory_copy_as` | `0xc2ee0` | — | `MEM_COPY_ASYNC` ioctl `0xC0384E6C` (boundary) | HIGH |
| `ndl_memory_buf_batch_copy` | `0xc5a30` | — | batch-copy ioctl `0xC0204E81` (boundary) | HIGH |

---

## 4. Free and Bulk Teardown

### Purpose

`dmem_free` is the inverse of `dmem_alloc_internal`: it drops the refcount, and only on the final reference does it unmap, free the kernel allocation, account the free, and unlink the handle from its allocator list. Two helpers walk *GC lists* (`dmem_list_t`, a separate singly-linked structure a caller passes as `ref_list` to alloc) so a subsystem can free a batch of allocations with one call.

### Algorithm — `dmem_free`

```c
// dmem_free @0x2293a0 — atomic refcount drop; full teardown on the last reference.
function dmem_free(mem):
    if !mem: return
    if _InterlockedSub64(&mem->ref_count, 1) != 0:   // other holders remain
        return
    if db_physical_core_get_mla_and_tpb(mem->pcore, &mla, &tpb) != 0:
        assert("ret == NRT_SUCCESS", dma_memory.c:0x276)
    tid = nrt_sys_trace_new_event(begin, 35, …)      // free-begin marker (trace type 35)
    if mem->_va && ndl_memory_unmap(mem->mem_handle):                    // only if mapped
        nlog("Failed to unmap allocation! %s", mem->hint)
    ndl_memory_free(mem->mem_handle)                 // ioctl 0x80084E16  [NDL → KERNEL]
    ntrace_record_event(NTRACE_DATA_TYPE_MEM_FREE, mem->allocated_size, mem->pcore->device_id, 0, mem->hint)
    if mem->hint: free(mem->hint)
    if dml_add_entry(&mla->dmem_logger, mem, DMEM_ACTION_FREE):          // §5 — decrement counters
        nlog("Failed to add entry to dmem logger")
    if enable_metrics && tdrv_nds_register_mem_free(device_id, tpb->idx, mem->mem_usage_type,
                                                    mem->mem_type, mem->allocated_size):
        nlog("Unable to log memory deallocation to NDS for device %d, type %d, location %d, amount %lu")
    nlog_TRACE("MEM_USAGE: %lu bytes, channel: %u, total usage: (%lu/%lu)", …)
    // unlink from the allocator list (O(1) via the back-pointer)
    if mem->allocator:
        assert(mem->prev, dma_memory.c:0x29C); assert(mem->next, dma_memory.c:0x29D)
        lock(mem->allocator->lock)
            mem->prev->next = mem->next; mem->next->prev = mem->prev    // splice out
        unlock(mem->allocator->lock)
    nrt_sys_trace_new_event(stop, 35, tid, …)
    free(mem)
```

> **NOTE —** the unmap is conditional on `mem->_va` — a `dmem_t` that was never `dmem_mmap`'d has no host mapping to release, so `ndl_memory_unmap` is skipped. `ndl_memory_free`, by contrast, always runs: every `dmem_t` holds a kernel allocation even if it was never mapped. The order matters — unmap (release the host VA) before free (release the device bytes) — and a reimplementer who frees before unmapping risks the driver reclaiming pages still mapped into the process.

### Algorithm — the GC list (`dmem_list_t`)

```c
// dmem_list_add @0x2285c0 — append a node to a caller-owned GC list (calloc 0x10 per node).
function dmem_list_add(list, mem):
    n = calloc(1, 0x10)                              // {dmem_t* dmem; node* next;}
    if !n: nlog("Failed to allocate memory for dmem_ref_node_t"); return NRT_RESOURCE
    n->dmem = mem
    if list->tail: list->tail->next = n  else  list->head = n
    list->tail = n
    return NRT_SUCCESS

// dmem_list_free @0x229750 — walk the GC list, dmem_free each, free each node.
function dmem_list_free(list):
    if !list: return
    for (n = list->head; n; ):
        next = n->next
        if n->dmem: dmem_free(n->dmem)               // drops a ref; frees if last
        free(n)
        list->head = next; n = next
    list->tail = 0
```

The GC list is a *second* sharing axis, orthogonal to the allocator list: a `dmem_t` can be on its allocator's intrusive list *and* on one or more GC lists. `dmem_list_free` calls `dmem_free`, which only frees when the refcount hits zero — so a GC list holding a borrowed reference (via `dmem_acquire_reference`) releases its hold without destroying an allocation another owner still uses. `dma_ring_info_alloc_from_memchunk` is the one caller that `dmem_acquire_reference`s before listing.

### Function Map — free

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `dmem_free` | `0x2293a0` | 0x3ab | atomic `--ref_count`; unmap/free/account/unlink on 0 | HIGH |
| `dmem_list_add` | `0x2285c0` | 0x073 | append `dmem_ref_node_t` (calloc 0x10) to GC list | HIGH |
| `dmem_list_free` | `0x229750` | 0x059 | walk GC list, `dmem_free` each, free nodes | HIGH |
| `dmem_acquire_reference` | `0x229390` | 0x00d | atomic `++ref_count`; return `mem` | HIGH |
| `ndl_memory_free` | `0xc2ba0` | — | free ioctl `0x80084E16` (boundary) | HIGH |

---

## 5. DML — the `dma_memory_logger` Usage Subsystem

### Purpose

DML is the allocator's accounting plane: a per-MLA running counter set plus a fixed-size circular event log that every `dmem_alloc_internal` and `dmem_free` updates. Its purpose is not to manage memory — it manages *visibility*. When the kernel returns `ENOMEM`, `dmem_alloc_internal` calls into DML's reporting half (`dml_dump`/`dml_log_dev_mem`) to render a human-readable per-NeuronCore / per-HBM / per-usage-type utilization table so an operator can see *what* exhausted HBM. The counters also feed the `MEM_USAGE: … total usage: (%lu/%lu)` trace line on every alloc/free, and `dmem_get_memory_stats` reads them for `nrt_get_vnc_memory_stats`.

### `dma_mem_log_t` — the counters (1600 B)

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `lock` | +0 (0x00) | `pthread_mutex_t` (40 B) | guards all counters + the ring | HIGH |
| `mla_idx` | +40 (0x28) | `uint32_t` | owning MLA index | HIGH |
| `max_entries` | +48 (0x30) | `size_t` | ring capacity (from `dml_init`) | HIGH |
| `entry_count` | +56 (0x38) | `size_t` | live entries (saturates at `max_entries`) | HIGH |
| `next_entry_idx` | +64 (0x40) | `size_t` | write cursor `= (idx+1) % max_entries` | HIGH |
| `entries_dropped` | +72 (0x48) | `size_t` | `++` when the ring is full | HIGH |
| `memory_usage` | +80 (0x50) | `size_t[4]` | per-HBM-channel running total (`channel < 4`) | HIGH |
| `memory_usage_nc` | +112 (0x70) | `size_t[8][23]` | per-NC: cols `[0..21]` per usage type, col `[22]` per-NC grand total | HIGH |
| `entries` | +1584 (0x630) | `dma_mem_log_entry_t*` | the `calloc`'d ring (`64 B × max_entries`) | HIGH |
| `verbose_log_dev_mem` | +1592 (0x638) | `bool` | render the 22-column verbose table vs the 12-column aggregate | HIGH |

The `dma_mem_log_entry_t` (64 B) is the ring element: `{action@0, mem_loc@4, mem_usage@8, tdram_channel@12, hint@16, nc_idx@24, size@32, pa@40, align_offset@48, total_mem_used@56}` — a snapshot of the `dmem_t` at the instant of the event plus the post-update channel total.

### Algorithm — `dml_add_entry`

```c
// dml_add_entry @0x22ab80 — update running counters + append a ring event. TONGA_DRAM only.
function dml_add_entry(log, mem, action):           // action ∈ {ALLOC=1, FREE=2}
    lock(log->lock)
    if mem->mem_type == TONGA_DRAM:                  // host allocs do not move device counters
        assert(mem->tdram_channel < 4, dma_memory_logger.c:0x58)
        nc = mem->pcore->device_tpb_idx
        assert(nc < tdrv_arch_get_num_tpb(), …:0x59)
        delta = (action == ALLOC) ? +mem->allocated_size : -mem->allocated_size
        log->memory_usage[mem->tdram_channel]              += delta      // per-channel
        log->memory_usage_nc[nc][mem->mem_usage_type]      += delta      // per-NC per-usage
        log->memory_usage_nc[nc][22]                       += delta      // per-NC grand total
    // append to the circular log (independent of mem_type)
    if log->max_entries && log->entries:
        e = &log->entries[log->next_entry_idx]
        log->next_entry_idx = (log->next_entry_idx + 1) % log->max_entries
        if log->entry_count < log->max_entries: log->entry_count++
        else: log->entries_dropped++                // overwrite oldest, count the drop
        *e = { action, mem->mem_type, mem->mem_usage_type, mem->tdram_channel,
               mem->hint, mem->pcore->device_tpb_idx, mem->size, mem->_pa, mem->align_offset }
        if mem->mem_type == TONGA_DRAM:
            assert(e->tdram_channel < 4, …:0x73)
            e->total_mem_used = log->memory_usage[e->tdram_channel]      // post-update snapshot
    unlock(log->lock)
    return NRT_SUCCESS
```

> **QUIRK —** the per-NC matrix is declared `[8][23]` but indexed in the decompile as `memory_usage_nc[0][usage + 24*nc - 24*… ]` collapsing to a **stride of 23 elements (184 bytes)** per NC row, with column `[22]` reserved as the per-NC grand total. So the row width is 23 (`0..21` usages + 1 total), not 24, and the second dimension is genuinely 23. A reimplementer who allocates `[8][22]` will overwrite the next NC's first usage counter on every grand-total update.

### Algorithm — `dml_init`

```c
// dml_init @0x22aa50 — zero the 1600-byte counter block, init mutex, calloc the ring.
function dml_init(log, mla_idx, max_entries, verbose):
    memset(log, 0, 0x640)                            // clear all counters + cursors
    if pthread_mutex_init(&log->lock, 0):
        nlog("Failed to initialize dma memory log lock"); return NRT_FAILURE
    ring = calloc(0x40, max_entries)                 // 64 B × capacity
    if !ring: nlog("Failed to allocate memory for dma memory log entries"); return NRT_RESOURCE
    log->mla_idx = mla_idx; log->max_entries = max_entries
    log->verbose_log_dev_mem = verbose; log->entries = ring
    return NRT_SUCCESS
```

`dml_init` is called once per device from `tdrv_init`; `dml_free` (`@0x22ab50`) destroys the mutex and frees the ring at `tdrv_destroy`. The reporting half — `dml_log_dev_mem`, `log_dev_mem_usage_table`, `human_readable_byte_size`, the per-NEFF breakdown, and the 22↔12 column aggregation tables — is a self-contained rendering subsystem (`dma_memory_logger.c`) invoked only on the OOM/utilization paths; it formats the counters above into the `"Failed to allocate %s (alignment: %s, usage: %s) on ND%2u:NC%2u"` report and, on the verbose path, a per-HBM ASCII table. Its internals (the `dmem_usage_type_to_aggr_index[22]` projection onto 12 aggregate categories, the table-builder helpers) are accounting presentation, not allocation logic, and are not re-derived here.

### Function Map — DML

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `dml_init` | `0x22aa50` | zero counters, init mutex, `calloc` the ring | HIGH |
| `dml_free` | `0x22ab50` | destroy mutex, free ring, null the pointer | HIGH |
| `dml_add_entry` | `0x22ab80` | update per-channel/per-NC counters; append ring event | HIGH |
| `dml_get_device_memory_usage` | (boundary) | read back `(total, per-type)` for the trace payload | HIGH |
| `dml_dump` / `dml_driver_dump` / `dml_log_dev_mem` / `dml_log_host_mem` | (other cell) | OOM/utilization rendering on `ENOMEM` | HIGH |

---

## Related Components

| Name | Relationship |
|---|---|
| `ndl_memory_*` family | the NDL leaves this layer drives; the sole kernel (ioctl/mmap) boundary |
| `nrt_tensor_storage_t` (`DMA` kind) | holds a `dmem_t*`; `dmem_batch_transfer_common` reads `tensor->sto->dmem` |
| staged `mem_ref_t` | `mem_ref_copy_and_stage_mr` `dmem_alloc_aligned`s + `dmem_buf_copyin`s, sets `physical_address` from the `dmem_t` |
| `tpb->tpb_allocator` | the per-TPB `dmem_allocator_t` that `tdrv_init` creates and scratchpad pages splice into |
| nrtucode platform-ops | `platform_memhandle_device_malloc` wraps `dmem_alloc_aligned`; the opaque memhandle is a `dmem_t*` |

## Cross-References

- [TDRV: Tensor Object Layer](tdrv-tensor.md) — the `nrt_tensor_storage_t` (`DMA` kind) that points at a `dmem_t`; `_pa`/`align_offset`/`tdram_channel` consumers; the batch tensors `dmem_batch_transfer_common` walks
- [TDRV: Device Bring-Up and Lifecycle](tdrv-lifecycle.md) — `tdrv_init` creates the per-TPB `dmem_allocator_t` and calls `dma_memory_init`/`dml_init`; `tdrv_destroy` frees them; the platform-ops callbacks that wrap this allocator
- [NDL: Neuron Driver Layer (IOCTL/mmap Wrappers)](ndl.md) — `ndl_memory_alloc`/`_get_pa`/`_copy_as`/`_buf_batch_copy`/`_free` and the `/dev/neuron*` fd they ioctl
- [Static Memory Planning (mem_ref)](../neff/memory-planning.md) — assigns the device `physical_address` that a staged `mem_ref_t` reads from its backing `dmem_t`; `mem_ref_copy_and_stage_mr` is the bridge from a load-time plan to a live `dmem_t`
- [Memory IOCTL Handlers](../kernel/ioctl-mem.md) — the kernel side of `MEM_ALLOC` (`0x80384E66`), `MEM_COPY_ASYNC` (`0xC0384E6C`), get-PA (`0x80084E19`), and free (`0x80084E16`) that this layer drives through NDL
