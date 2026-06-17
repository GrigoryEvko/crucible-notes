# Virtual Rings (vring) and Packet Builders

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba…`, SONAME `libnrt.so.1`, ELF64, **not stripped**, DWARF present; `.text` VMA == file offset, so every `0x31…` is an analysis VMA). All 24 vring functions resolve via DWARF (`addr2line`, `comp_dir = /opt/workspace/KaenaRuntime`) to a single TU, `tdrv/vring.c`; all are `local` (`t`) symbols, not exported NRT API.*
> *Evidence grade: **Confirmed (DWARF-anchored)** — function addresses, line numbers, struct offsets and packing constants are nm/structures-DB verified; the 16-byte element's bitfields are owned by [The 16-Byte Descriptor](descriptor-format.md) and are not re-derived here. Other versions will differ. · Part VIII — DMA & Descriptor Engine · [back to index](../index.md)*

## Abstract

A **vring** ("virtual ring") is the host-side staging structure the Neuron runtime uses to *build* a device DMA program before that program touches any device-visible memory. It is a **paged, growable, doubly-linked list of 16-byte `al_udma_desc` descriptors**, split into a **tx half** and an **rx half** — the exact mental model of building an `iovec` (or a NIC's scatter-gather list) in ordinary heap memory, accumulating entries with realloc-on-overflow, and only later *committing* the finished list into a hardware submission ring. The commit step here is called a **dump**: `vring_dump_to_pring_descriptors_padded` (`0x311f10`) copies a vring half, descriptor-for-descriptor, into a physical `dma_ring_info_t` ("pring") bound to device DRAM, padding to a fixed descriptor count so a *static execution plan* always occupies the same ring footprint.

The split between authoring and committing is deliberate and buys two things a hardware ring cannot offer. First, **unbounded growth**: a hardware TX/RX ring has a fixed descriptor count chosen at queue-init, but a model's descriptor program is not known until load time, so the runtime accumulates into vring pages (65536 descriptors each, malloc'd one at a time) and discovers the total before sizing the pring. Second, **post-hoc editing**: because the descriptors sit in plain host memory, the packing layer can walk back over them and rewrite control bits — coalesce adjacent CME descriptors into packets (`vring_pack_descs`, `0x312590`), set the DMB barrier on the last packet (`vring_set_dmb_on_last_packet`), graft an embedded semaphore increment onto the last RX descriptor, relocate every `buf_ptr` by a fixed delta (`vring_addr_rewrite`), or resolve a *template* vring (symbolic `var_id → tensor PA`) into a concrete one (`vring_build_vring_pages_from_template`). None of that is possible once the bytes are in a device ring being consumed by the UDMA engine.

This page documents four artifacts a reimplementer must reproduce: (1) the **vring / vring_pages / vring_desc_page geometry** and the page-allocation arithmetic that hands out the next descriptor; (2) the **append-a-copy path** — `vring_add_desc_transfer_internal` chunking a transfer into ≤64 KiB descriptors and delegating each to the Alpine `al_udma_m2m_build_copy_descriptor` builder, plus the 4-byte event/semaphore copy builders; (3) the **dump** — `vring_dump_to_pring_descriptors_padded` packing the accumulated descriptors into a pring and padding with no-op self-copies; and (4) **who authors into vrings** — the executor's pseudo-DMA translator and the collective `encd` devmem-load path, which are the producers that fill these rings before they are dumped and doorbelled.

For reimplementation, the contract is:

- **The paged-list geometry**: a `vring` = `{name[256], tx: vring_pages, rx: vring_pages, total_tx_size, total_rx_size}`; each half is a `vring_pages` = `{used_desc_count, head, tail, is_template, max_var_id}`; each page is `desc[65536] + next + prev` = exactly `0x100010` bytes. The descriptor index splits as `page = idx >> 16`, `in_page = idx & 0xFFFF`.
- **The next-descriptor allocator**: hand out `&page.desc[idx & 0xFFFF]`, malloc a fresh page when `(idx & 0xFFFF) == 0` wraps, enforce the hard cap **16,777,200** descriptors per half, doubly-link the page list.
- **The copy-append algorithm**: chunk `size` into ≤`0x10000`-byte runs, allocate a tx **and** an rx descriptor per chunk, call `al_udma_m2m_build_copy_descriptor`, set DMB on the final chunk (V2/V3), accumulate `total_tx_size`/`total_rx_size`.
- **The dump algorithm**: assert `pring->type ∈ {TX, RX}`, copy descriptors in page-stride (`<<20`) / ≤64 KiB-run (`<<16`) chunks via `dma_ring_copy_descriptors`, then pad to `padded_size` with 4-byte no-op self-copies.

| | |
|---|---|
| **TU** | `tdrv/vring.c` (`/opt/workspace/KaenaRuntime`), GCC 14.2.1, `-O2 -fPIC` |
| **`vring_t`** | 344 B (`0x158`) — `name@0x0`, `tx@0x100`, `rx@0x120`, `total_tx_size@0x140`, `total_rx_size@0x148` |
| **`vring_pages_t`** | 32 B — `used_desc_count@0x0`, `head@0x8`, `tail@0x10`, `is_template@0x18`, `max_var_id@0x1C` |
| **Page** | `vring_desc_page_t` = `0x100010` B = 65536 × 16 B descriptors + `next@0x100000` + `prev@0x100008` |
| **Descriptor cap** | **16,777,200** (`0xFFFFF0`) per half; reject at `0xFFFFEF` |
| **Allocator** | `vring_allocate_next_desc` `0x3111a0` (334 B, 79 insns) |
| **Copy append** | `vring_add_desc_transfer_internal` `0x3112f0` (513 B, 126 insns) → `al_udma_m2m_build_copy_descriptor` `0x45cca0` |
| **Dump** | `vring_dump_to_pring_descriptors_padded` `0x311f10` (728 B, 184 insns) → `dma_ring_copy_descriptors` `0x22eca0` |
| **Set ctor** | `vring_set_allocate` `0x311e00` — `calloc` of 16-vring `vring_set_t` (5520 B / `0x1590`) |
| **Dump target** | `dma_ring_info_t` ("pring", 32 B) — `type@0x0`, `ring_mem@0x8`, `ring_offset_bytes@0x10`, `used@0x18`, `alloc@0x1C` |

---

## 1. The vring Data Model

### Purpose

A vring exists so the runtime can author a complete DMA descriptor program in ordinary host memory, of arbitrary length, and edit it after the fact, before committing it to a fixed-size device ring. It is the layer directly *above* the 16-byte UDMA wire format ([descriptor-format](descriptor-format.md)): the vring owns *arrays* of that format; the Alpine HAL owns the bits inside one element. The whole structure is process-private host heap — no device visibility — until a `dump`.

### Layout

The geometry is three nested structs, all offset-verified against the decompiled field accesses and against exact-fit arithmetic (`0x100000 = 65536 × 16`, page malloc immediate `0x100010 = sizeof(vring_desc_page_t)`, `vring_t` stride 344, `vring_set_t` `0x1590`).

| Struct | Field | Offset | Type | Meaning |
|---|---|---|---|---|
| `vring_t` (344) | `name` | `0x000` | `char[256]` | `"<setname>_<u>"` via `snprintf`; printed in every error/trace string as `vring->name` |
| | `tx` | `0x100` | `vring_pages_t` | the m2s / source descriptor half (passed as `(int)vring + 256`) |
| | `rx` | `0x120` | `vring_pages_t` | the s2m / destination descriptor half (`+288`) |
| | `total_tx_size` | `0x140` | `size_t` | running sum of bytes across all tx descriptors |
| | `total_rx_size` | `0x148` | `size_t` | running sum across all rx descriptors |
| | `stochastic_rounding_en` | `0x150` | `bool` | per-vring SR flag, set at `vring_set_allocate` |
| `vring_pages_t` (32) | `used_desc_count` | `0x00` | `uint32_t` | descriptors handed out so far; low 16 bits index within current page |
| | `head` | `0x08` | `vring_desc_page_t*` | first page |
| | `tail` | `0x10` | `vring_desc_page_t*` | last page (where new descriptors land) |
| | `is_template` | `0x18` | `bool` | this half holds symbolic (template) `buf_ptr`s |
| | `max_var_id` | `0x1C` | `uint32_t` | upper bound on template `var_id` (sizes the found-tensors cache) |
| `vring_desc_page_t` (`0x100010`) | `desc` | `0x000000` | `al_udma_desc[65536]` | the 16-byte descriptors ([descriptor-format](descriptor-format.md)) |
| | `next` | `0x100000` | `vring_desc_page_t*` | forward link (walked by `vring_find_page_by_idx`) |
| | `prev` | `0x100008` | `vring_desc_page_t*` | back link |

> **QUIRK —** the page size is exactly **1 MiB + 16 bytes** (`0x100010`). The decompiler renders the `malloc` immediate `0x100010` as `malloc((size_t)enc_semaphore_check)` because `0x100010` happens to be the VA of an unrelated 43-byte function symbol (`enc_semaphore_check`). It is a literal page size, not a symbol reference. A reimplementer who treats it as anything but `sizeof(vring_desc_page_t)` is chasing a ghost. (HIGH — arithmetic verified `0x100010 == 1,048,592 == 65536 × 16 + 16`.)

### The set: 16 paired vrings

A `vring_set_t` is a flat array of 16 `vring_t` (stride 344) plus a `count` and a `tile_idx`, `calloc`'d whole (5520 B) by `vring_set_allocate` (`0x311e00`). Each of the `count` vrings is named `"%s_%u"` and stamped with the set's `stochastic_rounding_en`. The set is the unit the executor allocates per DMA-queue-set: `vring_set_allocate` has **16 distinct callers** across the load and exec layers (`init_dma_queue_set`, `encd_start_executable`, `io_create_queues`, `drs_create_data_refill_rings`, `dve_dynamic_config_init_{sunda,cayman,mariana}`, `act_local_storage_tbls_setup`, `imcpy_allocate_*`, …), and a symmetric set of callers into `vring_set_free` (`0x3121f0`).

### Function Map

| Function | Addr | Size / insns | Role | Confidence |
|---|---|---|---|---|
| `vring_set_allocate` | `0x311e00` | 198 / 58 | `calloc` 16-vring set, name + SR-stamp each | CERTAIN |
| `vring_set_free` | `0x3121f0` | 148 / 41 | free every vring's tx+rx pages, then the set | CERTAIN |
| `vring_free_pages` | `0x311ed0` | 57 / 17 | free one page list, zero head/tail/count | CERTAIN |
| `vring_find_page_by_idx` | `0x311d50` | 173 / 40 | walk `idx` hops via `next@+0x100000`; log "can't find page" | HIGH |
| `vring_deep_copy` | `0x313a10` | 128 / 36 | clone a whole vring (tx then rx halves) | HIGH |
| `vring_deep_copy_pages` | `0x3138d0` | 305 / 79 | per-page malloc + memcpy, relink prev/next | HIGH |

---

## 2. Allocating the Next Descriptor

### Purpose

Every descriptor-authoring path begins by claiming the next free 16-byte slot from a `vring_pages` half. This is the realloc-on-overflow primitive: it returns a pointer into the current tail page, and grows the list by one page the moment the in-page index wraps.

### Entry Point

```text
vring_add_desc_transfer_internal / _event / _semaphore_inc / dump-pad path
  └─ vring_allocate_next_desc (0x3111a0)        ── claim &page.desc[idx & 0xFFFF]
       └─ malloc(0x100010)                       ── only when (idx & 0xFFFF) == 0
```

`vring_allocate_next_desc` is also called directly by `vring_allocate_next_desc_cb` (`0x311500`), a 2-instruction shim that sets `*ring_id = 1` and forwards — this is the alloc callback handed by pointer to the Alpine `al_sdma_*` packet builders so they can pull descriptors out of a vring instead of a flat buffer.

### Algorithm

```c
// vring_allocate_next_desc — vring.c:81  (0x3111a0, 79 insns, 24B frame)
function vring_allocate_next_desc(pages):           // pages = &vring.tx or &vring.rx
    idx = pages.used_desc_count
    if idx >= 16777200:                              // 0xFFFFF0 hard cap
        nlog("Descriptor limit reached. Descriptors in vring %u, Max allowed %u",
             idx, 16777200)                          // string @0x81a410
        return NULL                                  // QUEUE_FULL upstream
    in_page = idx & 0xFFFF                            // low 16 bits index the page
    if in_page == 0:                                 // page boundary -> grow
        page = malloc(0x100010)                       // sizeof(vring_desc_page_t)
        if page == NULL:
            nlog("Failed to expand vring page pool")  // @0x81a458
            return NULL
        page.next = NULL
        if pages.head == NULL:                        // first page
            assert(pages.tail == NULL)                // vring.c:97 (0x61)
            page.prev = NULL
            pages.head = page
        else:                                         // append
            assert(pages.tail != NULL)                // vring.c:101 (0x65)
            page.prev   = pages.tail
            pages.tail.next = page
        pages.tail = page
    pages.used_desc_count = idx + 1                    // commit the slot
    return &pages.tail.desc[in_page]                   // 16B al_udma_desc slot
```

> **NOTE —** the cap is **16,777,200** (`0xFFFFF0`), not `0xFFFFFF`. It is `256 × 65535` rounded — i.e. a few descriptors short of 256 full pages — and is checked *before* the wrap, so the reject boundary is `used_desc_count == 0xFFFFEF`. A reimplementation that allows the index to reach `0x1000000` will allocate a 257th page and silently corrupt the `page = idx >> 16` arithmetic that consumers (`vring_find_page_by_idx`) rely on. (HIGH — constants `16777199`/`16777200`/`65535` present in the function's `constants_used`.)

> **QUIRK —** the in-page index is the **low 16 bits of `used_desc_count`**, so the allocator does not store a per-page free count — it derives "is this page full" purely from `(used_desc_count & 0xFFFF) == 0`. This is why a page is exactly `65536` descriptors: any other count breaks the `>> 16` / `& 0xFFFF` decode that the rest of the file uses to map a flat descriptor index back to `(page, slot)`.

---

## 3. Appending a Copy — the tx/rx Split

### Purpose

The core authoring operation is "copy `size` bytes from `src` to `dst`". A vring expresses this as the standard UDMA **pair**: one tx (m2s) descriptor that reads the source and one rx (s2m) descriptor that writes the destination, on the two halves of the *same* vring ([descriptor-format §1](descriptor-format.md#the-paired-descriptor-rule)). Because the 16-bit length field maxes at 64 KiB, a larger transfer is chunked into a `FIRST … LAST` run, one pair per chunk.

### Entry Point

```text
translate_one_pseudo_dma_memcpy_instr_v2 / encd_devmem_load_with_dma_add_descriptor /
setup_desc_for_act_tbl_ring_v2 / dve_dynamic_config_init_* / act_local_storage_tbls_setup
  └─ vring_add_desc_transfer        (0x312290, set_sow=arch)  ── thin forwarder (34 callers)
  └─ vring_add_desc_transfer_ex     (0x3122a0, set_sow=0)     ── thin forwarder
       └─ vring_add_desc_transfer_internal (0x3112f0)         ── the chunk loop
            ├─ vring_allocate_next_desc  (tx slot)
            ├─ vring_allocate_next_desc  (rx slot)
            └─ al_udma_m2m_build_copy_descriptor (0x45cca0)   ── [BOUNDARY -> Alpine HAL]
```

### Algorithm

```c
// vring_add_desc_transfer_internal — vring.c:131  (0x3112f0, 126 insns, 152B frame)
function vring_add_desc_transfer_internal(vring, src, dst, size, ..., set_sow):
    remaining = size
    s = src; d = dst
    while remaining > 0:
        chunk = min(remaining, 65536)                 // 0x10000 — len field maxes at 64K
        tx = vring_allocate_next_desc(&vring.tx)      // source descriptor
        rx = vring_allocate_next_desc(&vring.rx)      // destination descriptor
        if tx == NULL or rx == NULL:
            nlog("Failed to add descriptor %u/%u to vring %s", i, n, vring.name)  // @0x81a4b0
            return QUEUE_FULL
        is_last = (remaining - chunk == 0)
        barrier = (is_last and not set_sow) ? DMB : NONE         // DMB on final chunk only
        // BOUNDARY: Alpine HAL packs the 16-byte wire format (FIRST/LAST/len0/ring-id)
        al_udma_m2m_build_copy_descriptor(tx, rx, s, d, chunk, ring_id=?, barrier, set_sow)
        vring.total_tx_size += chunk                  // vring_t+0x140
        vring.total_rx_size += chunk                  // vring_t+0x148
        s += chunk; d += chunk
        remaining -= chunk
    nlog_trace("Added %u/%u desc to vring %s", n, n, vring.name)  // @0x847116
    return SUCCESS
```

> **GOTCHA —** the **DMB / strong-order decision is in the chunk loop, on the *last* chunk only**, and is mutually exclusive with `set_sow`. `vring_add_desc_transfer` (`0x312290`) forwards with `set_sow` = the arch's SOW support, while `vring_add_desc_transfer_ex` (`0x3122a0`) forces `set_sow = 0` (DMB path). A reimplementation that sets the barrier on every chunk, or on the first, serializes mid-transfer and changes ordering semantics. The actual bit positions (DMB = `len_ctrl` bit 30, SOW = `len_ctrl` bit 29 of the **Rx** descriptor, V3+ only) live in [descriptor-format §6](descriptor-format.md#6-barrier-modes); the vring layer only chooses *when*.

### The 4-byte event / semaphore copies

Three sibling builders author **4-byte** self-copies that poke a single device CSR — the descriptor *is* the side effect, the copy is a vehicle. All three emit one `vring_add_desc_transfer` of 4 bytes from an arch-resolved source to an arch-resolved destination, and propagate `is_sow_supported()` as `set_sow`:

```c
// vring_add_desc_event — vring.c:204 (0x3122b0)
function vring_add_desc_event(vring, tpb_idx, event_id, set/clear):
    src = tdrv_arch_get_evt_accel_addr(...)            // the "accelerator" constant feed
    dst = tdrv_arch_get_evt_addr(tpb_idx, event_id)    // the event CSR
    return vring_add_desc_transfer(vring, src, dst, 4, ..., is_sow_supported())

// vring_add_desc_semaphore_inc — vring.c:216 (0x312310): dst = tdrv_arch_get_sem_inc_addr(...)
// vring_add_desc_semaphore_inc_from_sb — vring.c:228 (0x312370): CAYMAN-only;
//   src = aws_hal_stpb_get_axi_offset base + (dma_eng_to_sb_partition_0[eng & 0xF] << 18) + 229372
```

> **NOTE —** `vring_add_desc_semaphore_inc_from_sb` asserts `al_hal_tpb_get_arch_type() == CAYMAN` (`vring.c:229`, offset `0xE5`) and computes its source from the State-Buffer AXI window: partition = `dma_eng_to_sb_partition_0[eng & 0xF]`, shifted `<< 18` (the 256 KiB partition stride), plus the literal `229372` byte offset of the semaphore-increment register within a partition. The constants `15`, `18`, `229372` are present in the function's `constants_used`. (HIGH for the arithmetic shape; the `229372` offset's meaning is MEDIUM — grounded only by this one call site.)

### Function Map

| Function | Addr | Size / insns | Role | Confidence |
|---|---|---|---|---|
| `vring_add_desc_transfer` | `0x312290` | 13 / 3 | forwarder, `set_sow = arch` (34 callers) | CERTAIN |
| `vring_add_desc_transfer_ex` | `0x3122a0` | 12 / 3 | forwarder, `set_sow = 0` (DMB path) | CERTAIN |
| `vring_add_desc_transfer_internal` | `0x3112f0` | 513 / 126 | chunk loop, tx+rx pair, total accumulation | HIGH |
| `vring_add_desc_event` | `0x3122b0` | 86 / 28 | 4-byte event set/clear copy | HIGH |
| `vring_add_desc_semaphore_inc` | `0x312310` | 90 / 30 | 4-byte semaphore-increment copy | HIGH |
| `vring_add_desc_semaphore_inc_from_sb` | `0x312370` | 193 / 54 | CAYMAN SB-sourced sem inc | HIGH |
| `al_udma_m2m_build_copy_descriptor` | `0x45cca0` | — | [BOUNDARY] packs the 16-byte pair | CERTAIN |

---

## 4. Packet Builders — Generic, CCE, Transpose

### Purpose

Above the single-copy primitive sit *packet* builders, which emit a multi-descriptor unit that the engine treats as one operation. A "packet" is a `FIRST … (CONCAT) … LAST` run of descriptors. Three families exist: a plain copy packet, a **CCE** (collective-compute-engine) reduction packet (ADD/MIN/MAX/FMA), and a **transpose** packet. The vring layer owns the orchestration and the size accounting; the Alpine `al_sdma_*` / `aws_cayman_sdma_*` builders own the per-descriptor encoding.

### Algorithm — the generic copy packet

```c
// vring_add_dma_packet_v2 — vring.c:278 (0x312440, 83 insns)
function vring_add_dma_packet_v2(vring, src, dst, size, ..., out_tx_n, out_rx_n):
    // Alpine builder pulls descriptors via the vring alloc callback (ring_id forced to 1)
    ret = al_udma_m2m_build_copy_packet(src, dst, size, ...,
                                        alloc_cb = vring_allocate_next_desc_cb,  // 0x311500
                                        ctx = vring)
    if ret != 0:
        nlog("Failed to build packet for vring %s err %d", vring.name, ret)      // @0x81a6d0
        return ret
    *out_tx_n = tx descriptors produced
    *out_rx_n = rx descriptors produced
    return SUCCESS
// vring_add_dma_packet (0x312570) is a thin wrapper -> _v2 with vring_idx = 0.
```

### Algorithm — the CCE reduction packet

`vring_add_dma_packet_cce_int` (`0x311510`, **2050 bytes, 425 insns** — the largest function in the file) dispatches an `SDMA_CCETYPE` to the matching Alpine builder. This is the on-device collective reduction path: `vring_add_dma_packet_cce` (`0x3134e0`) wraps it for ordinary reductions, and `vring_add_set_seed_packet` (`0x313510`) wraps it with `op = ADD, set_seed = 1` to inject an RNG-seed-set operation.

```c
// vring_add_dma_packet_cce_int — vring.c:828 (0x311510)
function vring_add_dma_packet_cce_int(vring, op, srcs, dst, num_dests, dtype, scale, ...):
    assert(cce_info != NULL)                              // vring.c:952 (0x3B8)
    assert(scale_dtype == fp32)                            // vring.c:954 (0x3BA) for FMA scale
    if num_dests > 1:                                      // replication
        if al_hal_tpb_get_arch_type() <= 2:               // SUNDA / v2
            nlog("CCE replication not supported for v1/v2 archs"); return -1
        ret = aws_cayman_sdma_m2m_build_combo_op(...)      // CAYMAN combo-op
    else:
        switch op:                                         // SDMA_CCETYPE
            case ADD:        ret = al_sdma_m2m_build_add_packet(...)
            case MIN, MAX:   ret = al_sdma_m2m_build_min_max_packet(...)   // op-2 <= 1
            case FMA:        ret = al_sdma_m2m_build_fma_packet(...)
            default:         nlog("unsupported op! (%u)", op); return -1
    if ret != 0:
        nlog("Failed to add CCE %u packet, ret=%u", op, ret); return ret
    vring.total_tx_size += sum-of-tx-descriptor-sizes      // accounted post-build
    return SUCCESS
```

> **GOTCHA —** CCE **replication** (`num_dests > 1`, one source fanned to many destinations) is gated to CAYMAN (arch > 2) and routes to a *different* builder (`aws_cayman_sdma_m2m_build_combo_op`) than the single-destination reductions. On v2 it is a hard `-1` with the log `"CCE replication not supported for v1/v2 archs"`, not a silent fallback to N separate copies. The inner SSE-packed operand marshalling into the `al_sdma_*` builders (FMA `a_sel`/`b_sel`/`c_sel` arrays sized by `num_blocks`, the `min_max` `use_constant` path) is the Alpine packet ABI and is **out of this TU** — role HIGH, micro-encoding MEDIUM.

### Algorithm — the transpose packet

`vring_add_dma_packet_transpose` (`0x312ea0`, 249 insns) maps a 4-character permutation op (one of 12: `"WZXY"`, `"WXZY"`, … `"XYWZ"`) to a dimension/order index via an `strncmp` table, maps `element_size ∈ 1..8` to an `al_sdma_m2m_meta_ctrl_data_type` via a switch table, then selects the v2 builder (`al_sdma_m2m_build_transpose_packet`) or the CAYMAN builder (`aws_cayman_sdma_m2m_build_transpose_packet`) by arch. `vring_add_dma_packet_transpose_flush` (`0x3133a0`) emits the matching flush packet and rejects v2 (`"v2 arch does not support transpose flush"`).

### Function Map

| Function | Addr | Size / insns | Role | Confidence |
|---|---|---|---|---|
| `vring_add_dma_packet` | `0x312570` | 25 / 8 | wrapper → `_v2`, `vring_idx = 0` | CERTAIN |
| `vring_add_dma_packet_v2` | `0x312440` | 294 / 83 | generic copy packet via `al_udma_m2m_build_copy_packet` | HIGH |
| `vring_add_dma_packet_cce_int` | `0x311510` | 2050 / 425 | CCE ADD/MIN/MAX/FMA + replication dispatch | HIGH (encoding MED) |
| `vring_add_dma_packet_cce` | `0x3134e0` | 42 / 13 | wrapper → `_cce_int` | CERTAIN |
| `vring_add_set_seed_packet` | `0x313510` | 449 / 108 | seed-set op via `_cce_int(ADD, set_seed=1)` | HIGH |
| `vring_add_dma_packet_transpose` | `0x312ea0` | 1270 / 249 | 12-perm transpose packet | HIGH |
| `vring_add_dma_packet_transpose_flush` | `0x3133a0` | 316 / 82 | transpose flush (CAYMAN only) | HIGH |
| `dma_util_vring_append_descs` | `0x316d10` | 2507 / 579 | executor entry that fans out to the three packet builders | HIGH |

---

## 5. Post-Authoring Edits — Pack, Barrier, Embedded Sem, Rewrite

Because the descriptors are in host memory, the runtime walks back over them and rewrites control bits before dumping. These are the editing operations a hardware ring cannot support.

### vring_pack_descs — coalesce into packets

`vring_pack_descs` (`0x312590`, 276 insns, 61 basic blocks — the most complex function in the TU) walks a `[start_idx, end_idx)` window of tx descriptors, groups CME descriptors into packets bounded by `max_packet_size` **and** `tdrv_arch_get_max_desc_per_packet()`, and (re)writes the FIRST/LAST/CONCAT bits via the Alpine `al_udma_m2m_set_*` / `clear_*` helpers (`0x45cf00`–`0x45cf50`). It returns `packet_delta = (packets after) − (packets before)` so the caller can adjust its packet count.

```c
// vring_pack_descs — vring.c:381 (0x312590)  — sketch of the FIRST/LAST/CONCAT state machine
function vring_pack_descs(vring, start_idx, end_idx, max_packet_size, cur_num_packets):
    max_per_packet = tdrv_arch_get_max_desc_per_packet()   // per-arch (128 v2, 65 v3/v4)
    cur = vring_find_page_by_idx(&vring.tx, start_idx >> 16)
    if cur == NULL: nlog("Couldn't find cur_page"); return error    // @0x847177
    packet_descs = 0; packet_bytes = 0; packets = 0
    for idx in [start_idx, end_idx):
        desc = &cur.desc[idx & 0xFFFF]
        sz   = al_udma_m2m_get_tx_desc_size(desc)            // len0 -> 64K aware (0x45cf60)
        if would_overflow(packet_descs+1, max_per_packet) or
           would_overflow(packet_bytes+sz, max_packet_size):
            al_udma_m2m_set_last(prev)                        // close current packet
            al_udma_m2m_clear_concat(prev)
            packets += 1; packet_descs = 0; packet_bytes = 0
        if packet_descs == 0: al_udma_m2m_set_first(desc)     // open new packet
        else:                 al_udma_m2m_set_concat(prev)    // chain (>64K runs)
        packet_descs += 1; packet_bytes += sz; prev = desc
        advance cur across page boundary via next@+0x100000
    al_udma_m2m_set_last(prev); packets += 1
    return packets - cur_num_packets                          // packet_delta
```

> **CORRECTION (VRING-PACK) —** the exact ordering of the `set_last`/`clear_concat`/`set_first` writes and the precise overflow predicate are reconstructed from the call set and the per-arch caps, **not** traced instruction-by-instruction through this function's 61-block tangled goto/label flow. Treat the *shape* (open-on-first, concat-on-continue, close-on-overflow-or-end, return delta) as HIGH and the bit-write sequencing as MEDIUM; a reimplementer should verify packet-boundary correctness against the `al_udma_m2m_*` bit semantics in [descriptor-format §2](descriptor-format.md#5-the-control-bit-table).

### The other edits

```c
// vring_set_dmb_on_last_packet — vring.c:513 (0x312b30)
//   NO-OP (returns SUCCESS) if is_sow_supported(); else walks the tx tail backward to the
//   last packet's first/meta descriptor and sets its DMB bit (al_udma_m2m_set_dmb, 0x45ced0).
//   Asserts the target is NOT meta and NOT already first (vring.c:0x232/0x233).

// vring_should_emb_sem_update — vring.c:575 (0x312d30)  -> bool
//   arch-supports-embedded-sem AND gconf.embedded_sem_en AND rx has descriptors AND
//   bit 31 of the last rx descriptor's len_ctrl is clear ((HIBYTE(rx.len_ctrl) >> 7) == 0).

// vring_set_embedded_semaphore_inc_on_last_desc — vring.c:601 (0x312da0)
//   resolve embedded-sem CSR index (aws_hal_sdma_embedded_sem_csr_index) for sem_pcore,
//   build a 6-byte field blob ([0]=set_sow,[1..2]=0x0100,[3]=csr_idx,[4..5]=semaphore),
//   write via aws_hal_sdma_s2m_set_embedded_sem_fields on the last rx descriptor.

// vring_addr_rewrite — vring.c:1253 (0x313810)
//   for every descriptor whose tx.buf_ptr in [base, base+size), add (new_base - base).
//   Page-chunked at 0x10000 descs/page. Logs "vring is modified".
```

> **QUIRK —** `vring_set_dmb_on_last_packet` is a **no-op when strong-ordered writes are supported** — on V3+ the ordering is already carried by the SOW bit on the rx descriptors (set at append time), so a DMB on the last tx packet would be redundant. On V2, where SOW does not exist, the runtime instead retro-fits a DMB onto the last packet's leading descriptor. The two mechanisms are never both active; this is the same NONE/DMB/SOW three-way split documented per-bit in [descriptor-format §6](descriptor-format.md#6-barrier-modes), here chosen at the *packet* level.

### Templates → concrete

A *template* vring carries symbolic `buf_ptr`s and is resolved per execution. `vring_build_vring_pages_from_template` (`0x313a90`) walks each descriptor and, for any whose `buf_ptr` has the template bit set, looks up the tensor and rewrites the pointer to a physical address:

```c
// buf_ptr template encoding (constants 61, 36, 524287, 68719476735 verified)
is_template = (buf_ptr >> 61) & 1            // bit 61 = 0x2000000000000000
var_id      = (buf_ptr >> 36) & 0x7FFFF      // bits 54:36  (524287 = 0x7FFFF, 19-bit)
offset      = buf_ptr & 0xFFFFFFFFF          // bits 35:0   (68719476735, 36-bit)
// resolve: tensor = found_tensors[var_id] ?? ht_name_find(ht_find(mr_id=var_id));
//          new buf_ptr = tensor_get_pa(tensor) + offset
```

This is the symbolic-relocation pass; `var_id` is asserted `< max_var_id + 1` (`vring.c:0x576`), and a missing tensor logs `"Failed to find tensor %s in tensor set"`. The full encoding is owned by the encd/devmem layer — see [encd-dma-devmem](../collectives/encd-dma-devmem.md).

---

## 6. The Dump — vring → pring

### Purpose

The terminal operation. `vring_dump_to_pring_descriptors_padded` copies a finished vring *half* into a physical `dma_ring_info_t` ("pring") that is bound to device DRAM, then pads to a fixed descriptor count. The padding makes the device ring footprint identical across loads of the same model, which is what a *static execution plan* requires: the doorbell, completion offsets, and ring-wrap arithmetic are all precomputed against `padded_size`, not against the variable real descriptor count.

### Entry Point

```text
encd_start_executable / load_dma_queue_set / imcpy_load_vring / dve_* / encd_*
  └─ vring_dump_to_pring (0x3137e0)                    ── pad = used_desc_count (no extra pad)
       └─ vring_dump_to_pring_padded (0x3136f0)        ── BOTH halves: tx then rx
            └─ vring_dump_to_pring_descriptors_padded (0x311f10)   ── one half
                 ├─ dma_ring_copy_descriptors (0x22eca0)   ── chunked copy into pring
                 ├─ vring_allocate_next_desc / al_udma_m2m_build_copy_descriptor  ── 4B no-op pad
                 └─ (optional trap descriptor)
// vring_dump_to_pring_descriptors (0x3136e0) is a wrapper -> _padded with pad = used_desc_count.
```

### Algorithm

```c
// vring_dump_to_pring_descriptors_padded — vring.c:1145 (0x311f10, 184 insns)
function vring_dump_to_pring_descriptors_padded(pring, pages, padded_size, free_on_dump):
    assert(pring.type == DMA_RING_TYPE_TX ||
           pring.type == DMA_RING_TYPE_RX)              // vring.c:1146 (0x47A) @0x81a620
    n = pages.used_desc_count
    if n > padded_size:
        nlog("Failed to pad vring %s from %lu descriptors to %lu descriptors",
             ..., n, padded_size)                       // @0x81a668 — pad must be >= real count
        return error
    // (1) copy the real descriptors, page-by-page, ≤64KiB per run
    dst_off = 0
    for page in pages (head -> next via +0x100000):
        descs_in_page = min(remaining, 65536)
        for run in chunks of <= 65536 descriptors:      // page stride <<20, run <<16
            dma_ring_copy_descriptors(pring, dst_off,
                                      &page.desc[run_start], run_count)   // 0x22eca0
            dst_off += run_count
    // (2) pad the remainder with 4-byte no-op self-copies
    while dst_off < padded_size:
        slot = vring_allocate_next_desc(...) // or write directly into pring tail
        al_udma_m2m_build_copy_descriptor(slot, slot, scratch, scratch, 4, ...)  // no-op
        dst_off += 1
    pring.used_desc_count = padded_size
    nlog_trace("vring is copied to pring. ndesc=%u", padded_size)   // @0x81a6a8
    if free_on_dump: vring_free_pages(pages)
    return SUCCESS
```

`vring_dump_to_pring_padded` (`0x3136f0`) calls the above twice — `pring_tx ← vring.tx` then `pring_rx ← vring.rx` — each with its own `padded_size`, and TRACE-logs `"vring is copied to pring. ndesc=%u/%u"`. The plain `vring_dump_to_pring` (`0x3137e0`) supplies `pad = used_desc_count`, i.e. no extra padding.

> **GOTCHA —** the copy is **chunked at two granularities**: the page stride is `1 << 20` (1 MiB = 65536 descriptors × 16 B) so a copy run never spans two malloc'd pages, and each `dma_ring_copy_descriptors` run is capped at `1 << 16` = 65536 descriptors. These are not arbitrary — they fall straight out of the page geometry (§1). A reimplementation that copies the logically-flat descriptor array in one `memcpy` is wrong: the source is a **linked list of pages**, not contiguous memory, so the copy *must* respect the `next@+0x100000` boundary. (Constants `16`, `20`, `65536` present in the function's `constants_used`; page-stride `<<20` and run `<<16` inferred from the geometry — HIGH.)

> **NOTE —** the pad descriptors are 4-byte **self-copies** (`src == dst`, size 4) — they advance the ring's descriptor index without moving meaningful data, so the engine retires them as no-ops. The optional trap descriptor (if requested) terminates the padded region. The `dma_ring_info_t` target is described in [ring-cycle](ring-cycle.md); its `used_desc_count@0x18` ends equal to `padded_size`.

### Function Map

| Function | Addr | Size / insns | Role | Confidence |
|---|---|---|---|---|
| `vring_dump_to_pring` | `0x3137e0` | 35 / 9 | wrapper, `pad = used_desc_count` | CERTAIN |
| `vring_dump_to_pring_padded` | `0x3136f0` | 235 / 72 | dump tx then rx, per-half pad | HIGH |
| `vring_dump_to_pring_descriptors` | `0x3136e0` | 15 / 4 | wrapper → `_padded` | CERTAIN |
| `vring_dump_to_pring_descriptors_padded` | `0x311f10` | 728 / 184 | one half: page-chunked copy + no-op pad | HIGH |
| `dma_ring_copy_descriptors` | `0x22eca0` | — | [BOUNDARY] device-visible descriptor copy | HIGH |

---

## 7. Who Authors Into vrings

A vring is empty until a *producer* fills it. Two layers do almost all the authoring, and both bottom out in `vring_add_desc_transfer` / the packet builders before a dump.

### The executor's pseudo-DMA translator

The hot load path translates a model's compiled DMA program (pseudo-DMA-memcpy pseudo-instructions and activation-table rings) into vring descriptors:

- `translate_one_pseudo_dma_memcpy_instr_v2` → `vring_add_desc_transfer` — one source/dest copy per pseudo-instruction.
- `setup_desc_for_act_tbl_ring_v2`, `act_local_storage_tbls_setup`, `create_act_tbl_queue_and_memref_v2` → `vring_set_allocate` then `vring_add_desc_transfer` — activation-table rings.
- `drs_create_data_refill_rings`, `drs_expand_data_desc_model`, `expand_io_descs` → `vring_set_allocate`, `dma_util_vring_append_descs`, `vring_pack_descs` — IO and data-refill expansion.
- `fill_io_dma_desc_template` → `vring_add_desc_event` / `_semaphore_inc` — synchronization descriptors interleaved into the data stream.

`dma_util_vring_append_descs` (`0x316d10`, 579 insns) is the executor's single fan-out: it dispatches to `vring_add_dma_packet`, `vring_add_dma_packet_transpose`, or `vring_add_dma_packet_cce` based on the pseudo-op kind, and is reached from `drs_expand_data_desc_model` and `expand_io_descs`.

### The collective encd / devmem layer

The on-device collectives emitter (`encd`) authors its descriptor stream into vrings too:

- `encd_devmem_load_with_dma_add_descriptor` → `vring_add_desc_transfer` — the devmem-load floor: each tensor staged into device DRAM becomes a tx/rx copy pair in a vring. This is the producer that the **template** mechanism (§5) serves — `encd` emits symbolic `buf_ptr`s (`var_id`-tagged) and `vring_build_vring_from_template` resolves them to physical addresses per execution.
- `encd_start_executable` → `vring_set_allocate` + `vring_add_desc_transfer` — allocates the per-collective vring set and authors its bring-up copies.
- `dma_ring_set_seed_set_descriptors` → `vring_add_set_seed_packet` — injects the RNG-seed-set CCE op.
- `dve_dynamic_config_init_{sunda,cayman,mariana}` → `vring_set_allocate` + `vring_add_desc_transfer` — per-arch dynamic-config rings.

The flow is uniform: **producer authors copies/packets → packing layer coalesces and barriers → template pass resolves addresses → dump commits to pring → ring-cycle doorbells it**. The vring is the editable intermediate the whole pipeline pivots on. See [encd-overview](../collectives/encd-overview.md) and [encd-dma-devmem](../collectives/encd-dma-devmem.md) for the producer side.

> **NOTE —** the entire vring TU is internal (`local` symbols) — no `vring_*` name is an exported NRT API. A reimplementer is free to choose any host-side representation; what must be reproduced is the *contract* with the consumers: a flat descriptor index that decodes as `(idx>>16, idx&0xFFFF)`, a `tx`/`rx` split, the `total_*_size` accounting the executor reads back, and a dump that lands `padded_size` descriptors in the pring.

---

## Related Components

| Name | Relationship |
|---|---|
| `al_udma_m2m_build_copy_descriptor` (`0x45cca0`) | [BOUNDARY] packs each tx/rx pair the vring allocates |
| `al_udma_m2m_build_copy_packet` (`0x45d550`) | [BOUNDARY] builds a CONCAT-chained copy packet via the vring alloc callback |
| `al_udma_m2m_set_*` / `clear_*` (`0x45cf00`–`0x45cf50`) | [BOUNDARY] the FIRST/LAST/CONCAT/DMB bit-setters `vring_pack_descs` drives |
| `dma_ring_copy_descriptors` (`0x22eca0`) | [BOUNDARY] the device-visible copy primitive the dump uses |
| `dma_ring_info_t` ("pring", 32 B) | the dump target — a device-bound physical ring |
| `dma_queue_info_t` (`vring@0x140`) | a queue carries a `vring_t*` until it is dumped to its static rings |
| `tdrv_arch_get_max_desc_per_packet` (`0x30adf0`) | [BOUNDARY] the per-arch packet cap `vring_pack_descs` honors |

## Cross-References

- [The 16-Byte Descriptor](descriptor-format.md) — the `al_udma_desc` element this page builds lists of; owns every bit position (len_ctrl, meta_ctrl, barrier modes, len0 trick)
- [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md) — what happens to a pring after the dump: doorbell, trigger, completion harvest
- [encd: DMA Descriptor and devmem Floor](../collectives/encd-dma-devmem.md) — the `buf_ptr` template encoding and the collective devmem-load producer that authors into these vrings
- [encd: Device-Side Descriptor Emitter](../collectives/encd-overview.md) — the collective emitter that allocates vring sets and emits the symbolic descriptor stream
- [SDMA / CCE / TDG Meta-Control Overlays](meta-ctrl-overlays.md) — the op-class detail behind the CCE/transpose packet builders
- [back to index](../index.md)
