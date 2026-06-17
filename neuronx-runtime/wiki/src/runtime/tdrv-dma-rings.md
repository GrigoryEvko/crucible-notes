# TDRV: DMA Descriptor Rings and Swap-Queue

> *All addresses, offsets, sizes, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF; `.text` is VMA == file offset (every `0x3…`/`0x4…`/`0x2…` is an analysis VMA). Provenance strings `/opt/workspace/KaenaRuntime/tdrv/dma_ring.c`, `…/dma_dynamic_ring_set.c`, `…/dma_queue_reg_offset.c`, and `…/sw_dma_queue.c` root every function. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF/byte-anchored)** — function addresses from `nm`/sidecar filenames, struct offsets from decompiled field accesses + the IDA structures DB, the ring-id bit positions and wrap arithmetic from x86-64 disassembly, call edges from the IDA call graph. · Part IV — TDRV Runtime · [back to index](../index.md)*

## Abstract

This page owns the **userspace DMA descriptor-ring construction core** of the Tonga device driver inside `libnrt.so`: the layer that turns a model's compiled, template descriptor program into *physical* rings (**prings**) bound to device DRAM, and the lightweight software shadow (**`sw_dma_queue`**) that bookkeeps producer indices for the one ring the runtime fills incrementally at execution time. It sits directly above two layers it does not re-derive: the [16-byte descriptor](../dma/descriptor-format.md) wire format that goes *into* a ring, and the [vring](../dma/virtual-rings.md) host-side authoring/dump machinery that fills a ring's descriptor memory. It sits directly below the kernel ring container ([dma-rings](../kernel/dma-rings.md)) — the kernel owns the hardware `udma_q` and the doorbell; this layer owns the host-side `dma_ring_info_t` ("pring") and the `sw_dma_queue` index shadow.

There are three ring families a reimplementer must distinguish, and the runtime's own vocabulary keeps them apart. A **vring** (virtual ring) is a paged, growable host buffer of descriptors that producers author into — owned by [virtual-rings](../dma/virtual-rings.md). A **pring** (physical ring) is a `dma_ring_info_t` (32 B) that binds a `dmem_t` device-DRAM chunk to a descriptor count and a tx/rx role; a vring is *dumped* into a pring to commit it. A **dynamic ring set** (`ddrs_`) is the per-queue collection of prings that `ddrs_build_dma_rings` materializes at NEFF load: it walks a model's `dma_queue_set_t`, resolves each instance's *template* vrings against the load-time tensor placements, allocates a pring per template via `dma_ring_info_alloc`, dumps the resolved descriptors in, and caches the whole set keyed by the tensors' physical addresses so identical placements reuse the same rings. The second half of the page is the **`sw_dma_queue`** — a 12-byte `{next_desc_idx, desc_ring_id, desc_count}` shadow with a **2-bit ring-id generation counter** — that the hardware-exec queue uses to reserve and commit TX/RX descriptor *pairs* into its static prings without re-reading device memory, stamping each descriptor's phase bits as it goes. A short third unit pins the **`dma_queue_reg_offset`** resolvers — six thin BAR-offset accessors that compute where a queue's tail/head/base/size registers and a DMA engine's abort-cause register live, the seam between this layer and the Annapurna UDMA HAL.

The recurring H3 vocabulary applies to four units: **(1)** pring allocation — `dma_ring_info_alloc` sizing a descriptor buffer and binding a `dmem_t`; **(2)** `ddrs_build_dma_rings` — the per-TPB load-time ring-set builder and its tensor-PA-keyed LRU cache; **(3)** the `sw_dma_queue` shadow — reserve/commit of TX/RX descriptor pairs and the 2-bit generation counter; **(4)** the `dma_queue` register-offset resolvers. The `dma_queue_bundle`/`dma_queue_set` allocation model (engine/queue reservation bitmaps, H2D queues, the `dma_queue_init` HW-init path) is the *queue* side and is owned by the [TDRV device-lifecycle layer](tdrv-lifecycle.md); this page is the *ring* side.

For reimplementation, the contract is:

- **The pring (`dma_ring_info_t`, 32 B)** — `{type@0, ring_mem@8, ring_offset_bytes@0x10, used_desc_count@0x18, allocated_desc_count@0x1C}`. The `type ∈ {INVALID=0, TX=1, RX=2}` and the `dmem_t* ring_mem` (the device-DRAM descriptor buffer) are the two fields every consumer reads.
- **The pring sizing rule** — `dma_ring_info_alloc` rounds the requested descriptor count to a power of two (wraparound rings) or up-to-16 + 16 (linear rings), with a floor of **64 descriptors / 1024 bytes**, then `dmem_alloc`s `16 × allocated_desc_count` bytes tagged by a usage class from `category_map_28`.
- **The `ddrs_build_dma_rings` algorithm** — per bundle instance with a template `vring_set`: build a tensor-PA lookup key, probe the per-instance LRU `ring_cache`; on hit bump a refcount and reuse, on miss deep-copy each template vring, resolve it (`vring_build_vring_pages_from_template`), `dma_ring_info_alloc` a pring, dump the resolved descriptors, and insert the set into the cache.
- **The `sw_dma_queue` shadow (12 B)** — `{next_desc_idx@0, desc_ring_id@4, desc_count@8}`. *Reserve* returns the current `next_desc_idx` of TX and RX and advances both by `ndescs` modulo `desc_count`; *commit* stamps each descriptor's `len_ctrl` bits 25:24 with the per-index ring-id and copies them into the pring, splitting the copy at the ring wrap.
- **The 2-bit generation counter** — `desc_ring_id` initializes to **1**, advances `(id+1) & 3` on every producer wrap (`next_desc_idx` returns to/past 0), and is written into descriptor `len_ctrl` bits 25:24 so the hardware can tell a freshly-written descriptor from a stale one a lap behind.

| | |
|---|---|
| **Source** | `tdrv/dma_ring.c`, `tdrv/dma_dynamic_ring_set.c`, `tdrv/sw_dma_queue.c`, `tdrv/dma_queue_reg_offset.c` |
| **pring struct** | `dma_ring_info_t` — **32 B** (`calloc(1, 0x20)`) |
| **pring alloc** | `dma_ring_info_alloc` `@0x22d680` (280 B) — size → `dmem_alloc` → bind |
| **Load-time builder** | `ddrs_build_dma_rings` `@0x3179a0` (3293 B, 107 BBs) — per-TPB ring-set materialization |
| **Builder caller** | `kbl_compute_build_compute_resources` `@0x306790` (the sole inbound edge) |
| **Shadow struct** | `sw_dma_queue_t` — **12 B** `{next_desc_idx@0, desc_ring_id@4, desc_count@8}` |
| **Shadow init** | `sw_dma_queue_init` `@0x448f30` — `next_desc_idx=0, desc_ring_id=1` |
| **Reserve** | `sw_dma_queue_reserve_descriptors` `@0x448ce0` — TX+RX index pair, advance both |
| **Commit** | `sw_dma_queue_set_descriptors` `@0x448d30` — stamp ring-id, wrap-split copy |
| **Generation counter** | `desc_ring_id` — 2-bit, init **1**, `(id+1) & 3` on wrap; `len_ctrl[25:24]` |
| **Copy primitive** | `dma_ring_copy_descriptors` `@0x22eca0` → `dmem_dma_copy_descriptors` |
| **Reg resolvers** | `get_dma_queue_{tail,head,base_low,base_high,size}_offset` `@0x318a00..0x318b80` |
| **Ring types** | `dma_ring_type_t` — `{INVALID=0, TX=1, RX=2}` |

---

## 1. The pring — `dma_ring_info_alloc`

### Purpose

A **pring** (`dma_ring_info_t`, 32 B) is the terminal home of a DMA descriptor program: a chunk of device DRAM, sized to a descriptor count, tagged TX or RX. It is the bridge from the editable host-side vring to the device-visible ring the [ring-cycle](../dma/ring-cycle.md) doorbells. `dma_ring_info_alloc` is the one path that creates one. Its job is purely sizing and binding — it does not author a single descriptor; it computes how many 16-byte slots the ring needs, asks the [dmem allocator](tdrv-dmem.md) for `16 × count` device bytes, and stamps the resulting `dmem_t` and count into the caller's `dma_ring_info_t`. The descriptor bytes are written later, by a vring dump ([virtual-rings §6](../dma/virtual-rings.md#6-the-dump--vring--pring)) or by the `sw_dma_queue` commit (§3).

### The pring layout

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `type` | +0 (0x00) | `dma_ring_type_t` | `{INVALID=0, TX=1, RX=2}` — which half of the queue this ring drives | HIGH |
| `ring_mem` | +8 (0x08) | `dmem_t*` | device-DRAM descriptor buffer; `NULL` until alloc, zeroed at free | HIGH |
| `ring_offset_bytes` | +16 (0x10) | `uint64_t` | byte offset of descriptor 0 within `ring_mem`; `0` from `dma_ring_info_alloc`, must be 256-B aligned when set elsewhere | HIGH |
| `used_desc_count` | +24 (0x18) | `uint32_t` | descriptors actually written (set by the dump = `padded_size`) | HIGH |
| `allocated_desc_count` | +28 (0x1C) | `uint32_t` | descriptor capacity the buffer was sized for | HIGH |

### Algorithm — `dma_ring_info_alloc`

```c
// dma_ring_info_alloc @0x22d680 — size a descriptor buffer and bind a dmem_t to a pring.
// Two sizing regimes; a 64-descriptor / 1024-byte floor; one device allocation.
function dma_ring_info_alloc(allocator, mem_loc, tdram_channel, type, name,
                             ndesc, wraparound, category, /*out*/ ring):
    assert(category < KBIN_DMA_RING_TYPE_GENERIC)        // dma_ring.c:0x42A

    if ndesc <= 31:                                       // both regimes share the floor
        alloc_count = 64;  bytes = 1024                   // min ring: 64 desc, 1 KiB
    elif wraparound:
        alloc_count = next_pow2(ndesc + 15)               // a HW wrap ring must be pow2
        bytes = 16 * alloc_count                          //   (bit-smear on ndesc+15)
    else:                                                 // linear ring
        alloc_count = round_up_16(ndesc + 16)             // +16 guard, then 16-align
        bytes = 16 * alloc_count

    // THE DEVICE ALLOCATION — one dmem_alloc, usage class projected from the ring type
    usage = category_map_28[category]                     // @0x9ba320 — ring type -> dma_mem_usage_type
    rc = dmem_alloc(allocator, &ring_mem, bytes, mem_loc, tdram_channel,
                    /*align=*/0, usage, name)             // 0x228ed0  [-> dmem layer]
    if rc != NRT_SUCCESS: return rc

    ring.type                 = type                       // TX or RX
    ring.allocated_desc_count = alloc_count
    ring.ring_mem             = ring_mem
    ring.ring_offset_bytes    = 0                          // descriptor 0 at the chunk base
    return NRT_SUCCESS
```

> **QUIRK —** the **`wraparound` flag picks the sizing law**. A `wraparound` ring is rounded to a power of two because the hardware UDMA queue masks its producer index with `size - 1` ([ring-cycle §1](../dma/ring-cycle.md#1-the-ring-cursors-and-the-wrap-rule)) and a non-power-of-two ring would alias. A *linear* ring (the dynamic/static descriptor rings this page builds) is instead rounded up to a multiple of 16 plus a 16-descriptor pad — the same `+16` guard the kernel ring math reserves, surfaced here at allocation time. A reimplementer who sizes a linear ring to an exact `ndesc` under-allocates by the guard; one who pads a wraparound ring to non-pow2 corrupts the hardware wrap. The `category_map_28` projection (ring type → `dma_mem_usage_type`) is owned by the [vring](../dma/virtual-rings.md) and dmem usage tables — `COLLECTIVES→21`, `DATA→19` (spill), `IN`/`OUT→20` (io), everything else `→18` (`DMA_RING`).

### Function Map — pring allocation

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `dma_ring_info_alloc` | `0x22d680` | size (pow2 / +16) → `dmem_alloc(16×count)` → bind `dma_ring_info_t` | HIGH |
| `dma_ring_free` | `0x22d570` | `dmem_free(ring->ring_mem)`; zero type / mem / offset / counts | HIGH |
| `dma_ring_copy_descriptors` | `0x22eca0` | copy `ndescs` descriptors into `ring_mem` at `ring_offset_bytes + offset` | HIGH |
| `dma_ring_info_alloc_from_memchunk` | `0x22d5f0` | bind a *pre-existing* `dmem_t` to a ring (256-B-aligned offset); the static-ring variant | HIGH |

The five callers of `dma_ring_info_alloc` span every ring family: `ddrs_build_dma_rings` (dynamic rings, §2), `hw_exec_queue_init` (the static exec ring the §3 shadow drives), `dma_ring_alloc`/`dma_alloc_refill_ring` (named tx/rx data rings), and `dma_ring_allocate_seed_set_ring` (the RNG seed ring). `dma_ring_copy_descriptors` is the single device-write primitive shared by both the vring dump and the §3 commit — a one-line forwarder to `dmem_dma_copy_descriptors` that adds the ring's `ring_offset_bytes` to the caller's offset.

---

## 2. `ddrs_build_dma_rings` — Per-TPB Load-Time Ring Construction

### Purpose

`ddrs_build_dma_rings` is the load-time driver that materializes a model's *dynamic* descriptor rings on one TPB. A NEFF carries its DMA program as **template** vrings — descriptor arrays whose `buf_ptr`s are symbolic (`var_id`-tagged), not yet bound to physical addresses ([virtual-rings §5](../dma/virtual-rings.md#5-post-authoring-edits--pack-barrier-embedded-sem-rewrite)). At load, the runtime knows where each tensor landed in HBM, so it walks the model's queue set, resolves every template against the actual tensor placements, and dumps the resolved descriptors into freshly-allocated prings. The decisive optimization is the **LRU ring cache**: the resolved rings depend only on the tensors' physical addresses, so a set is keyed by the array of resolved PAs and reused whenever an identical placement recurs — across queue instances, model reloads, and shape-compatible variants.

### Entry Point

```text
kbl_compute_build_compute_resources (0x306790)          ── the compute-resource builder (KBL boundary)
  └─ ddrs_build_dma_rings (0x3179a0)                     ── per qset: build dynamic ring sets
       ├─ ht_find / ht_name_find / tensor_get_pa         ── resolve var_id -> tensor -> PA (lookup key)
       ├─ lru_cache_buf_find / _buf_insert (0x446d00/0x446de0)  ── the PA-keyed ring cache
       ├─ vring_deep_copy_pages (0x3138d0)               ── clone the template vring
       ├─ vring_build_vring_pages_from_template (0x313a90) ── symbolic buf_ptr -> physical PA
       ├─ dma_ring_info_alloc (0x22d680)                 ── one pring per template (§1)
       └─ vring_dump_to_pring_descriptors (0x3136e0)     ── commit resolved descriptors to the pring
```

### Structures

`ddrs_build_dma_rings` writes into a `tdrv_compute_ioqs_resource_t` (the `compute_resource` out-parameter) and builds `dma_dynamic_ring_set_t` objects cached per bundle-instance. The fields it touches, offset-verified against the decompiled accesses:

| Struct | Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|---|
| `dma_dynamic_ring_set_t` | `ref_count` | +0 (0x00) | `int64_t` | atomic; init `1`; `+1` on cache hit, `-1` in `ddrs_free` (frees at 0) | HIGH |
| | `max_num_rings` | +8 (0x08) | `uint32_t` | capacity = template ring count of the instance | HIGH |
| | `num_rings` | +12 (0x0C) | `uint32_t` | rings filled so far (`< max_num_rings`) | HIGH |
| | `rings[]` | +16 (0x10) | `{dma_ring_info_t*, mr_id, type/chan}` | stride 24; entry = pring ptr + var-id + packed type/channel | HIGH |
| `ddrs_cache_node_t` | `lookup_key` | +0x00 | `uint64_t*` | array of resolved tensor PAs (`8 × tensor_id_count` bytes) | HIGH |
| | `key_size` | +0x08 | `size_t` | `8 × tensor_id_count` | HIGH |
| | `ring_set` | +? | `dma_dynamic_ring_set_t*` | the cached set this key maps to | MED |
| | `node` | +0x10 | `lru_node_t` | LRU intrusive node (node `calloc(1, 0x58)`) | HIGH |
| `dma_queue_bundle_instance_t` | `tensor_var_ids` | +288 | `uint64_t*` | the symbolic var-ids whose PAs form the key | HIGH |
| | `tensor_id_count` | +296 | `size_t` | key length (asserted `> 0`) | HIGH |
| | `vring_set` | +312 | `vring_set_t*` | template ring set (NULL ⇒ instance is not dynamic) | HIGH |
| | `ring_cache` | +320 | `ddrs_cache_t*` | the per-instance LRU `{lru_cache_t* @0, mutex @8}` | HIGH |

### Algorithm — `ddrs_build_dma_rings`

```c
// ddrs_build_dma_rings @0x3179a0 — materialize/reuse the dynamic ring sets for one queue set.
// One ring set per bundle-instance that owns a template vring_set; cache-keyed by tensor PAs.
function ddrs_build_dma_rings(allocator, qset, mr_to_name_map, input_set, output_set,
                              ring_mem_location, /*out*/ compute_resource):
    rset_idx = 0
    for i in 0 .. qset.nsets:                              // each queue bundle
        bundle   = qset.set_info[i]
        assert(bundle.instances != NULL && bundle.num_instances > 0)   // ...c:0x17A
        instance = bundle.instances[0]
        if instance.vring_set == NULL:                    // +312 — not a dynamic instance
            continue                                       // (static rings handled elsewhere)
        assert(bundle.num_instances == 1)                  // template rings: exactly one instance
        assert(instance.tensor_var_ids != NULL && instance.tensor_id_count > 0)

        // (1) BUILD THE CACHE KEY — resolve each var_id to its tensor's physical address
        key = malloc(8 * instance.tensor_id_count)
        for j in 0 .. instance.tensor_id_count:
            var_id = instance.tensor_var_ids[j]
            name   = ht_find(mr_to_name_map, var_id)        // var_id -> mem-ref name
            set    = (mem_ref.is_input) ? input_set : output_set
            tensor = ht_name_find(set, name)                // name -> nrt_tensor_t
            key[j] = tensor_get_pa(tensor)                  // 0x30FAE0
        key_size = 8 * instance.tensor_id_count

        // (2) PROBE THE LRU CACHE (under instance.ring_cache->mutex)
        cache = instance.ring_cache
        lock(cache.mutex)
        if lru_cache_buf_find(cache.lru, key, key_size, &hit) == NRT_SUCCESS:
            ring_set = hit.ring_set
            atomic_add(&ring_set.ref_count, 1)              // share the cached rings
            unlock(cache.mutex)
            free(key)
            compute_resource.dynamic_ring_sets[rset_idx++] = ring_set
            continue
        unlock(cache.mutex)

        // (3) CACHE MISS — build a fresh ring set
        rc_node            = calloc(1, 0x58)                // ddrs_cache_node_t
        rc_node.lookup_key = key;  rc_node.key_size = key_size
        ring_set = malloc(24 * instance.num_rings + 128)    // header (128) + 24B/ring
        ring_set.max_num_rings = instance.num_rings
        ring_set.num_rings     = 0

        for r in 0 .. instance.num_rings:                   // each template ring of the instance
            template = ring.template_vring                  // assert(template != NULL) ...c:0x113
            type     = template.is_tx ? TX : RX             // ring half
            // (3a) clone + resolve symbolic buf_ptrs to physical addresses
            vring_deep_copy_pages(template, &resolved)
            vring_build_vring_pages_from_template(&resolved, mr_to_name_map,
                                                  input_set, output_set, is_tx)
            // (3b) allocate the pring and commit the resolved descriptors (§1)
            pring = calloc(1, 0x20)                          // dma_ring_info_t
            dma_ring_info_alloc(allocator, ring_mem_location, tdram_channel, type,
                                name, resolved.used_desc_count, /*wrap=*/0, category, pring)
            vring_dump_to_pring_descriptors(&resolved, pring, name, /*pad*/1, /*free*/1)
            vring_free_pages(&resolved)
            assert(ring_set.num_rings < ring_set.max_num_rings)   // ddrs_add_ring_to_set
            ring_set.rings[ring_set.num_rings++] = { pring, mr_id, type|channel }

        // (4) INSERT into the LRU cache (re-probe under lock; loser frees its set)
        ring_set.ref_count = 1
        rc_node.ring_set   = ring_set
        lock(cache.mutex)
        if lru_cache_buf_find(cache.lru, key, key_size, NULL) != HIT:   // still absent
            lru_cache_buf_insert(cache.lru, key, key_size, &rc_node.node, &evicted)
            if evicted: free(evicted.lookup_key); ddrs_free(evicted.ring_set); free(evicted)
            atomic_add(&ring_set.ref_count, 1)              // cache holds one reference
        unlock(cache.mutex)
        compute_resource.dynamic_ring_sets[rset_idx++] = ring_set

    assert(rset_idx == compute_resource.num_ring_sets)      // ...c:0x1E8 — exact count match
    return NRT_SUCCESS
```

> **GOTCHA —** the cache is **refcounted, not copied**. A cache hit hands back the *same* `dma_dynamic_ring_set_t` and bumps `ref_count`; both the cache entry and every `compute_resource` slot that points at it hold a reference. `ddrs_free` (`0x317750`) decrements with `_InterlockedExchangeAdd64(-1)` and only frees the rings (and each pring's `dmem_t`) when the count was 1. A reimplementer who frees a ring set at the first `ddrs_free` tears device descriptor memory out from under every other instance still sharing it; one who never refcounts the cache entry leaks the set when the LRU evicts it. The miss path also **re-probes under the lock before inserting** — two threads building the same placement concurrently both build a set, and the loser's set is freed (the cache holds exactly one), which is why the build is idempotent but not allocation-free under a race.

> **QUIRK —** the key is the array of tensor **physical addresses**, not the var-ids. Two instances with different symbolic var-ids but the same resolved HBM placement produce the same key and share rings; the same var-ids resolved to a *different* placement (a reload that moved tensors) produce a different key and rebuild. This is what makes the resolved-ring reuse survive model reloads as long as the loader reproduces the placement — the rings are a pure function of where the tensors physically are, which is the whole point of resolving templates at load rather than baking absolute addresses into the NEFF.

### Function Map — dynamic ring construction

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `ddrs_build_dma_rings` | `0x3179a0` | per-qset: resolve templates → prings, PA-keyed LRU cache | HIGH |
| `ddrs_free` | `0x317750` | refcount `-1`; free each ring's pring + `dmem_t` at 0 | HIGH |
| `dma_ring_info_alloc` | `0x22d680` | one pring per resolved template (§1) | HIGH |
| `vring_deep_copy_pages` | `0x3138d0` | [BOUNDARY] clone a template vring before resolving | HIGH |
| `vring_build_vring_pages_from_template` | `0x313a90` | [BOUNDARY] symbolic `buf_ptr` → physical PA | HIGH |
| `vring_dump_to_pring_descriptors` | `0x3136e0` | [BOUNDARY] commit resolved descriptors to the pring | HIGH |
| `lru_cache_buf_find` / `_buf_insert` | `0x446d00` / `0x446de0` | [BOUNDARY] the PA-keyed ring cache | HIGH |
| `tensor_get_pa` | `0x30FAE0` | [BOUNDARY] resolve a tensor's device PA for the key | HIGH |

---

## 3. The `sw_dma_queue` Shadow — Reserve / Commit and the 2-Bit Generation Counter

### Purpose

The dynamic rings of §2 are built whole at load time. One ring is different: the **hardware execution queue** (`hw_exec_queue`) on each TPB accumulates descriptors *incrementally* at execution time as the runtime submits work, and must do so without re-reading the device-resident pring it is writing into. The `sw_dma_queue` is the host-side shadow that makes that possible: a 12-byte `{next_desc_idx, desc_ring_id, desc_count}` that mirrors, for a TX and an RX pring, the producer index and the **phase** the hardware will see. It is the same producer-cursor model as the kernel `udma_q` ([ring-cycle §1](../dma/ring-cycle.md#1-the-ring-cursors-and-the-wrap-rule)), reduced to exactly the three fields a userspace producer needs to (a) hand out the next free descriptor index for a TX/RX *pair* and (b) stamp each descriptor with the correct ring-id generation so the hardware does not execute a stale descriptor from the previous lap.

### The shadow layout

The `sw_dma_queue_t` is 12 bytes; `sw_dma_queue_init` zeros `next_desc_idx`+`desc_ring_id` in one 8-byte store, then sets `desc_count` and `desc_ring_id`. Two of these sit inline in the `hw_exec_queue` (the TX shadow precedes the RX shadow by one 12-byte stride).

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `next_desc_idx` | +0 (0x00) | `uint32_t` | **PRODUCER** index — next free descriptor slot, `mod desc_count` | HIGH |
| `desc_ring_id` | +4 (0x04) | `uint32_t` | **PHASE / generation** — 2-bit (`& 3`); init `1`; `+1` on wrap | HIGH |
| `desc_count` | +8 (0x08) | `uint32_t` | ring capacity = the pring's `allocated_desc_count` | HIGH |

### Entry Point

```text
hw_exec_queue_init (0x3201c0)                            ── one shadow per pring (tx + rx)
  └─ sw_dma_queue_init (0x448f30)                        ── next_desc_idx=0, desc_ring_id=1, desc_count

hw_exec_queue_add_descriptors (0x3206f0)                ── submit a batch of N descriptor pairs
  ├─ sw_dma_queue_reserve_descriptors (0x448ce0)        ── claim N indices on txq AND rxq
  │    ├─ sw_dma_queue_get_next_desc_idx (0x448c80)     ── read next_desc_idx
  │    └─ sw_dma_queue_inc_next_desc_idx (0x448c90)     ── advance + flip phase on wrap
  └─ sw_dma_queue_set_descriptors (0x448d30)            ── stamp ring-id, copy into the prings
       ├─ sw_dma_queue_ring_id_get_at_idx (0x448cc0)    ── phase for a given descriptor index
       └─ dma_ring_copy_descriptors (0x22eca0)          ── device write (wrap-split into ≤2 runs)
```

### Algorithm — init, reserve, advance

```c
// sw_dma_queue_init @0x448f30 — one shadow, fresh. desc_count = pring allocated_desc_count.
function sw_dma_queue_init(swq, desc_count):
    swq.next_desc_idx = 0
    swq.desc_ring_id  = 1            // UDMA_INITIAL_RING_ID — first lap is phase 1, not 0
    swq.desc_count    = desc_count
    // {next_desc_idx, desc_ring_id} are cleared by one 8-byte store, then desc_ring_id := 1

// sw_dma_queue_reserve_descriptors @0x448ce0 — claim N slots on the TX and RX shadows.
// Called by hw_exec_queue_add_descriptors BEFORE it knows the descriptor bytes.
function sw_dma_queue_reserve_descriptors(txq, rxq, /*out*/ tx_idx, /*out*/ rx_idx, ndescs):
    *tx_idx = txq.next_desc_idx          // base index for this batch on TX
    *rx_idx = rxq.next_desc_idx          // base index for this batch on RX
    sw_dma_queue_inc_next_desc_idx(txq, ndescs)
    sw_dma_queue_inc_next_desc_idx(rxq, ndescs)
    return 0

// sw_dma_queue_inc_next_desc_idx @0x448c90 — advance the producer, flip phase on wrap.
function sw_dma_queue_inc_next_desc_idx(swq, count):
    if swq.desc_count < count: return -1          // a single batch cannot exceed the ring
    new = swq.next_desc_idx + count
    if new >= swq.desc_count:                      // this advance reaches/crosses the wrap
        swq.desc_ring_id = (swq.desc_ring_id + 1) & 3      // ADVANCE THE 2-BIT GENERATION
    swq.next_desc_idx = new % swq.desc_count        // wrap the producer index
    return 0
```

### Algorithm — commit (stamp the phase, wrap-split the copy)

The commit is where the shadow earns its keep: it walks the reserved index range, computes the phase **each descriptor** carries (which can differ within one batch if the batch straddles the wrap), ORs that 2-bit phase into the descriptor's `len_ctrl`, optionally patches the source/dest physical addresses, and copies the descriptors into the device prings — splitting the copy into the pre-wrap run and the post-wrap run because the ring buffer wraps but the source array does not.

```c
// sw_dma_queue_set_descriptors @0x448d30 — stamp ring-id into len_ctrl, copy to the prings.
// tx_descs/rx_descs are the ndescs host descriptors; src/dst_addrs optionally patch buf_ptr.
function sw_dma_queue_set_descriptors(txq, rxq, tx_ring, rx_ring, ndescs,
                                      tx_idx, rx_idx, tx_descs, rx_descs, src_addrs, dst_addrs):
    tx_phase = sw_dma_queue_ring_id_get_at_idx(txq, tx_idx)   // phase at the FIRST reserved index
    rx_phase = sw_dma_queue_ring_id_get_at_idx(rxq, rx_idx)
    tx_cap = txq.desc_count;  rx_cap = rxq.desc_count

    // (1) per-descriptor: re-derive the phase as the index crosses the wrap, stamp len_ctrl[25:24]
    for k in 0 .. ndescs:
        if (tx_idx + k) == tx_cap:  tx_phase = (tx_phase + 1) & 3   // TX wrapped at this slot
        if (rx_idx + k) == rx_cap:  rx_phase = (rx_phase + 1) & 3   // RX wrapped at this slot
        tx_descs[k].len_ctrl |= (tx_phase << 24) & 0x3000000        // bits 25:24 = TX ring-id
        rx_descs[k].len_ctrl |= (rx_phase << 24) & 0x3000000        // bits 25:24 = RX ring-id
        if src_addrs: tx_descs[k].buf_ptr = src_addrs[k]            // patch source PA
        if dst_addrs: rx_descs[k].buf_ptr = dst_addrs[k]            // patch dest PA

    // (2) copy into the device prings, SPLIT at the wrap: [idx, cap) then [0, remainder)
    tx_run0 = min(ndescs, tx_cap - tx_idx)                          // descriptors before the wrap
    rx_run0 = min(ndescs, rx_cap - rx_idx)
    dma_ring_copy_descriptors(tx_ring, tx_descs,           16 * tx_idx, tx_run0)   // 0x22eca0
    dma_ring_copy_descriptors(rx_ring, rx_descs,           16 * rx_idx, rx_run0)
    if tx_run0 < ndescs:                                            // wrapped: second run at offset 0
        dma_ring_copy_descriptors(tx_ring, &tx_descs[tx_run0], 0, ndescs - tx_run0)
        dma_ring_copy_descriptors(rx_ring, &rx_descs[rx_run0], 0, ndescs - rx_run0)
    return 0   // or NRT_FAILURE + "Cannot copy the descriptors in template to the ring"

// sw_dma_queue_ring_id_get_at_idx @0x448cc0 — the phase a descriptor at `idx` carries.
function sw_dma_queue_ring_id_get_at_idx(swq, idx):
    if (idx % swq.desc_count) >= swq.next_desc_idx:        // idx is "behind" the producer => last lap
        return (swq.desc_ring_id - 1) & 3                  // it belongs to the PREVIOUS generation
    return swq.desc_ring_id                                 // idx is in the current lap
```

> **QUIRK — the 2-bit generation counter and its wrap.** `desc_ring_id` is a **2-bit** value (`& 3`, values 0–3) that starts at **1**, not 0 (`UDMA_INITIAL_RING_ID`). It advances by one each time the producer index reaches or crosses `desc_count`, and is written into bits **25:24** of every descriptor's `len_ctrl` (`(phase << 24) & 0x3000000`). The hardware compares the phase in each fetched descriptor against the phase it expects for that ring slot; a mismatch means "this slot still holds a descriptor from the previous lap — do not execute it yet." Because the field is 2 bits, **the generation wraps every four laps** (`…→1→2→3→0→1→…`). Four distinct phases suffice because a slot written on lap *N* is always retired before the producer reaches it again on lap *N+1*, so at most two adjacent phases are ever live at one slot; the extra two states give margin against a producer racing ahead of a consumer that is mid-slot. A reimplementer who widens this to a full byte gains nothing and breaks the `0x3000000` mask the hardware decodes; one who starts it at 0 will, on the very first lap, write the same phase the hardware reads from a zero-initialized ring slot and execute uninitialized descriptors. The `ring_id_get_at_idx` helper is the subtle companion: a descriptor whose index is at-or-past `next_desc_idx` numerically is *behind* the producer (the producer already wrapped past it), so it carries the **previous** generation `(id-1) & 3` — the per-descriptor loop in the commit re-derives this rather than trusting a single batch-wide phase, because one batch can straddle the wrap and span two generations.

> **GOTCHA —** the descriptor write is `|=`, not `=`: the phase bits are **OR-ed into** an already-built `len_ctrl`. The descriptor's length, FIRST/LAST, and barrier bits were set by the [descriptor builder](../dma/descriptor-format.md) before the descriptor reached this layer; the `sw_dma_queue` owns only bits 25:24. The exact `len_ctrl` bit layout (length, ring-id, FIRST/LAST/CONCAT/DMB/SOW/INT/META) is owned by [descriptor-format §2](../dma/descriptor-format.md) and is not re-derived here — this layer contributes only the 2-bit ring-id at `[25:24]`. A reimplementer who writes `=` instead of `|=` will zero every other control bit and submit length-0, no-barrier descriptors.

### Function Map — the shadow

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `sw_dma_queue_init` | `0x448f30` | `next_desc_idx=0`, `desc_ring_id=1`, `desc_count=cap` | HIGH |
| `sw_dma_queue_reserve_descriptors` | `0x448ce0` | claim `ndescs` on TX+RX; return both base indices | HIGH |
| `sw_dma_queue_get_next_desc_idx` | `0x448c80` | read `next_desc_idx` | HIGH |
| `sw_dma_queue_inc_next_desc_idx` | `0x448c90` | advance producer `mod desc_count`; flip 2-bit phase on wrap | HIGH |
| `sw_dma_queue_ring_id_get_at_idx` | `0x448cc0` | phase for a descriptor index (`-1` generation if behind producer) | HIGH |
| `sw_dma_queue_set_descriptors` | `0x448d30` | per-desc phase stamp `len_ctrl[25:24]`; wrap-split device copy | HIGH |
| `hw_exec_queue_init` | `0x3201c0` | [caller] init the tx/rx shadows from the static prings' counts | HIGH |
| `hw_exec_queue_add_descriptors` | `0x3206f0` | [caller] reserve → commit a TX/RX descriptor batch | HIGH |

---

## 4. The `dma_queue` Register-Offset Resolvers

### Purpose

To read or write a queue's hardware tail/head/base/size registers (or a DMA engine's abort-cause register), a caller needs the BAR-relative byte offset of that register for a specific `(engine, queue, direction)`. The six `dma_queue_reg_offset.c` resolvers compute it. They are thin: each adds three terms — the engine's direction window, the per-queue block offset within the engine, and the register's offset within the queue block — selecting the **m2s** (TX) or **s2m** (RX) variant of each term by a `bool m2s`. They are the seam between this layer's host-side ring model and the Annapurna UDMA HAL (`aws_hal_udma_*`), which owns the actual register-map constants.

### Algorithm — the shared shape

All five queue-register resolvers share one structure; only the final per-register term differs. The abort-cause resolver is a two-term variant (no per-queue block — the abort cause is per-engine).

```c
// get_dma_queue_<reg>_offset @0x318a00..0x318b80 — BAR offset of a queue register.
// reg ∈ {tail @0x318a00, head @0x318a60, base_lo @0x318ac0, base_hi @0x318b20, size @0x318b80}
function get_dma_queue_<reg>_offset(dma_eng_offset, qid, m2s):
    if m2s:   // TX / memory-to-stream
        dir_off   = aws_hal_udma_get_m2s_offset(dma_eng_offset, qid)        // engine m2s window
        queue_off = aws_hal_udma_get_m2s_queue_offset(qid)                  // per-queue block
        reg_off   = aws_hal_udma_get_m2s_queue_<reg>_offset()              // register within block
    else:     // RX / stream-to-memory
        dir_off   = aws_hal_udma_get_s2m_offset(dma_eng_offset, qid)
        queue_off = aws_hal_udma_get_s2m_queue_offset(qid)
        reg_off   = aws_hal_udma_get_s2m_queue_<reg>_offset()
    return dma_eng_offset + dir_off + queue_off + reg_off

// get_dma_engine_abort_cause_bits_bar_offset @0x318be0 — per-ENGINE, not per-queue.
function get_dma_engine_abort_cause_bits_bar_offset(pcore, dma_eng, m2s):
    eng_bar = get_dma_engine_bar_offset(pcore, dma_eng)                     // 0x318740
    return eng_bar + (m2s ? aws_hal_udma_get_m2s_abort_cause_offset()
                          : aws_hal_udma_get_s2m_abort_cause_offset())
```

> **NOTE —** the per-register term is the only difference between the five queue resolvers: `tail → ..._queue_tail_ptr_offset`, `head → ..._queue_head_ptr_offset`, `base_low → ..._queue_base_ptr_lo_offset`, `base_high → ..._queue_base_ptr_hi_offset`, `size → ..._queue_len_offset`. These resolve, on the kernel side, to the `rings.drtp`/`drhp`/`drbp_low`/`drbp_high`/`size` registers in the per-queue MMIO block — the same block whose `drtp_inc` doorbell at `+0x38` is the launch register ([ring-cycle §3](../dma/ring-cycle.md#3-trigger-and-doorbell--launching-the-batch)). This layer computes *where* those registers are; it never writes the doorbell (which is BAR0-write-blocked from userspace and reached only via an ioctl).

### Function Map — register-offset resolvers

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `get_dma_queue_tail_offset` | `0x318a00` | BAR offset of the queue TAIL-ptr register | HIGH |
| `get_dma_queue_head_offset` | `0x318a60` | BAR offset of the queue HEAD-ptr register | HIGH |
| `get_dma_queue_base_low_offset` | `0x318ac0` | BAR offset of the ring base-ptr LO register | HIGH |
| `get_dma_queue_base_high_offset` | `0x318b20` | BAR offset of the ring base-ptr HI register | HIGH |
| `get_dma_queue_size_offset` | `0x318b80` | BAR offset of the queue LEN/size register | HIGH |
| `get_dma_engine_abort_cause_bits_bar_offset` | `0x318be0` | BAR offset of the engine abort-cause register (per-engine) | HIGH |

The callers spread across the encoder, sequencer, and queue-switch layers: `encd_ncfw_init` and `sequencer_dma_get_queue_tail_offset` read tail/head; `encd_load_executable`, `ioqs_create_swap_descriptors_v2`, `psqs_swap_queue_internal`, and `add_dma_config_{base,size}` read base/size for ring (re)programming; `cache_dma_abort_info` and `check_dma_queue_on_aborted_eng` read the abort cause.

---

## Related Components

| Name | Relationship |
|---|---|
| `dma_ring_info_t` ("pring", 32 B) | the device-bound ring this page allocates; bitfields of its descriptors owned by descriptor-format |
| `vring_t` / `vring_set_t` | the template host rings `ddrs_build_dma_rings` resolves and dumps into prings (virtual-rings) |
| `dma_dynamic_ring_set_t` | the per-instance, refcounted, PA-cached collection of prings `ddrs_build_dma_rings` produces |
| `sw_dma_queue_t` (12 B) | the producer/phase shadow the hardware-exec queue uses to reserve+commit descriptor pairs |
| `dmem_t` / `dmem_alloc` | the device-DRAM backing every pring's `ring_mem` (tdrv-dmem) |
| `aws_hal_udma_*` | [BOUNDARY] the Annapurna UDMA HAL that owns the register-map constants the §4 resolvers add up |
| `kbl_compute_build_compute_resources` | the KBL compute-resource builder that drives `ddrs_build_dma_rings` per TPB at load |

## Cross-References

- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the wire format that fills these rings; owns `len_ctrl` (the `sw_dma_queue` stamps only bits 25:24), the barrier modes, and the len0 trick
- [Virtual Rings (vring) and Packet Builders](../dma/virtual-rings.md) — the host-side template rings `ddrs_build_dma_rings` deep-copies, resolves, and dumps into the prings this page allocates
- [The Ring / Trigger / Doorbell / Completion Cycle](../dma/ring-cycle.md) — what runs *over* a pring after it is built: the producer/phase cursors (`next_desc_idx`/`desc_ring_id`) on the kernel side, the doorbell, and completion
- [DMA Rings and H2T Queues](../kernel/dma-rings.md) — the kernel ring container and the hardware `udma_q` whose phase model the `sw_dma_queue` shadows; descriptor-count rounding
- [NEFF: Compute-Resource Build](../neff/compute-resource-build.md) — `kbl_compute_build_compute_resources`, the load-time caller that invokes `ddrs_build_dma_rings` per TPB and consumes the `dynamic_ring_sets`
- [back to index](../index.md)
