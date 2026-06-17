# DMA Op Layer and the Completion-Marker Model

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0** (`/usr/src/aws-neuronx-2.27.4.0/`). The owned file is `neuron_dma.c` (1866 lines), read verbatim; the public API and the `DMA_COMPLETION_MARKER` macro live in `neuron_dma.h` (265 lines). Boundary structs are cited at their own definition site (`neuron_ring.h`, `udma/udma.h`, `share/neuron_driver_shared.h`, the `v2`/`v3` DHAL). The driver also ships as a stripped `neuron.ko`, but the GPL C is authoritative — every offset below is a struct field or a `#define`, never a recovered binary offset. Other driver versions renumber lines.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`neuron_dma.c` is the kernel driver's **high-level DMA operation layer**: the byte-moving primitives that everything above the AnnapurnaLabs/Alpine UDMA descriptor engine calls to move memory on a Neuron device. It sits **above** the UDMA HAL (`udma/*`, which owns the 16-byte descriptor, the ring cursors, the doorbell, and the recycle CAS) and **below** the IOCTL handlers in `neuron_cdev.c` (which marshal userspace requests into these calls). It exposes four directions of copy — host↔device, device↔device, host↔host, and a `memset` built on top of them — plus two completion models: a **classic chunked H2T memcpy** that splits an arbitrary transfer into ≤64 KiB descriptors and a **zero-copy** path that pins user pages and DMAs directly over them. Both completion models share one decisive Alpine fact: there is **no hardware completion interrupt and no hardware completion ring** — completion is observed by *polling a host-memory marker word* (`0xabcdef01`) that a 4-byte self-copy descriptor writes after all data descriptors retire.

The mental model is a textbook scatter-gather DMA submission layer with three twists this page documents to reimplementation accuracy. First, **a copy is a transfer split**: `ndma_memcpy_offset_move` (the classic entry core) loops emitting ≤`MAX_DMA_DESC_SIZE` (64 KiB) descriptor pairs until either the transfer is done or the batch hits `sync_threshold = 2031` data descriptors, then appends one completion descriptor and triggers — staging at most half the nominal 4096-descriptor ring per batch so the producer never overruns the consumer. Second, **completion is a host-memory marker poll**: `ndma_memcpy_wait_for_completion` busy-polls a `volatile u32` every 1 µs against `0xabcdef01`; on match it re-arms the marker and advances the UDMA software completion counter (`ndma_ack_completed_desc → udma_cdesc_ack`) to recycle the spent descriptor slots, because nothing else frees them. Third, **the V2 silicon has a DMA-hang bug inside a NeuronCore reset window**, and `_ndma_memcpy_wait_for_completion` works around it with a guarded retry loop (`ndma_retry_memcpy`) that re-initializes the ring and replays the staged chunks.

This page documents four artifacts a reimplementer must reproduce: (1) the **host-PA encoding rule** every primitive shares — a host VA becomes `virt_to_phys(va) | pci_host_base`, the OR-in that selects the host-BAR window, while a device chunk carries its PA directly; (2) the **classic transfer-split + marker-poll cycle** as C pseudocode, with `sync_threshold`, the `0xabcdef01` self-copy, and the 1 µs poll; (3) the **classic async double-buffered state machine** (the `NONE→ASYNC1→ASYNC2→ASYNC1` handle toggle that overlaps stage(N+1) with completion(N)); and (4) the **zero-copy ctx-queue path** — the 4-pointer circular context queue, the pin/submit/wait pipeline, the per-arch barrier policy, and the async H2D completion-queue (CQE ring) that posts results to userspace. The per-generation behavior — which engine hosts H2T, the per-arch wait-time estimate, the barrier-type choice — is fully delegated to the DHAL vtable (`ndhal->ndhal_ndma` / `ndhal_ndmar` / `ndhal_address_map`); `neuron_dma.c` carries **one** `narch_get_arch()` branch (the zero-copy barrier, `:1450`) and otherwise dispatches through function pointers.

For reimplementation, the contract is:

- **The host-PA encoding** — `ndma_mc_to_pa` (`neuron_dma.c:97`): `MEM_LOC_HOST → virt_to_phys(mc->va) | ndhal_address_map.pci_host_base`; `else → mc->pa`. Every `buf_*`/`buf2_*` host source/dest in this file OR-ins `pci_host_base`; device PAs never do.
- **The classic split + marker poll** — `ndma_memcpy_offset_move` (`:438`) → `ndma_memcpy_chunks` (`:311`, ≤64 KiB chunks, flush at `sync_threshold = 2031`) → `ndma_memcpy_add_completion_desc` (`:200`, the `0xabcdef01` self-copy) → `udma_m2m_copy_start` → `ndma_memcpy_wait_for_completion` (`:230`, 1 µs poll, reset-on-match, `ndma_ack_completed_desc`).
- **The async double-buffer** — `ndma_dma_ctx_get_next_handle` (`:70`) toggles `ASYNC1`/`ASYNC2`; `ndma_memcpy_mc_async` (`:586`) stages batch *N+1* and waits the *previous* ctx; `ndma_memcpy_mc_wait` (`:644`) drains it.
- **The zero-copy state machine** — `ndma_zerocopy_submit` (`:1603`): per op, pin pages → `ndma_build_n_issue_zc_descs` (`:1393`) → wait/release through the 4-pointer `ndma_ctx_queue`; async defers via captured `mm` + the SPMC `ndma_h2d_compl_queue` (`:1022`).
- **The V2 reset-window workaround** — `_ndma_memcpy_wait_for_completion` (`:378`): on `-ETIMEDOUT`, if `ndhal_ndma.ndma_retry_memcpy` *and* `nr_op_in_reset_wnd`, re-init the ring (`ndmar_h2t_ring_init`) and replay `ndma_memcpy_chunks`. Zero-copy is *disabled* on retry archs unless `zerocopy_trn1_override` is set.

| | |
|---|---|
| **Source** | `neuron_dma.c` (1866 lines), `neuron_dma.h` (265 lines), aws-neuronx-dkms 2.27.4.0, GPL-2.0 |
| **Layer position** | above [UDMA HAL](udma-main.md) (`udma/*`), below the [IOCTL handlers](ioctl-dma.md) (`neuron_cdev.c`) |
| **Completion marker** | `DMA_COMPLETION_MARKER = 0xabcdef01` (`neuron_dma.c:153`); slot size `sizeof(u32) = 4` (`:152`) — **no completion IRQ** |
| **Poll interval** | `one_loop_sleep = 1` µs (`:248`); budget = per-arch estimate, ×100000 on qemu/emu (`:241`), ÷200 for d2d (`:245`) |
| **Chunk size** | `MAX_DMA_DESC_SIZE = 65536` (`udma/udma.h:479`); transfers split into ≤64 KiB descriptors |
| **Batch flush bound** | `sync_threshold = DMA_H2T_DESC_COUNT/2 − UDMA_MAX_NUM_CDESC_PER_CACHE_LINE − 1 = 4096/2 − 16 − 1 = 2031` (`:321`) |
| **Host-PA rule** | host: `virt_to_phys(va) \| pci_host_base`; device: `mc->pa` (`ndma_mc_to_pa`, `:97`) |
| **Classic ctx slots** | `h2t_dma_ctx[3]` per ring — `SYNC=0` / `ASYNC1=1` / `ASYNC2=2` (`share/neuron_driver_shared.h:92`) |
| **Zero-copy ctx queue** | `ndma_ctx_queue`, capacity **1024** (mask 1023), pin budget **524288** pages (`:1068–1069`) |
| **Async CQ** | `ndma_h2d_compl_queue`, capacity **1024** (`:974`), SPMC CQE ring mmapped to userspace |
| **Locking** | per-ring `ring->h2t_ring_lock` (mutex) serializes stage+trigger+poll; `memset` adds `nd->memset_lock` |
| **Module param** | `zerocopy_trn1_override = 0` (`:28`) — force zero-copy on V2 despite the HW-bug gate |
| **V2 HW-bug knob** | `ndhal_ndma.ndma_retry_memcpy` — reset-window timeout retry (`:394`) |

---

## 1. The Host-PA Encoding and Handle Plumbing

### Purpose

Every primitive in this file ends in a UDMA M2M copy that needs **physical addresses** for both source and destination, and a **NeuronCore id** to pick the engine. Two tiny helpers own that translation, and getting either wrong corrupts the descriptor before it is ever staged. They are documented first because every later section uses them.

### The host-BAR window OR-in

```c
// ndma_mc_to_pa — neuron_dma.c:97.  The single host-vs-device PA rule.
dma_addr_t ndma_mc_to_pa(struct mem_chunk *mc):
    if mc->mem_location == MEM_LOC_HOST:
        return virt_to_phys(mc->va) | ndhal->ndhal_address_map.pci_host_base   // :100
    else:
        return mc->pa                                                          // :102

// ndma_mc_pair_to_nc — neuron_dma.c:46.  Which NeuronCore owns the engine.
u32 ndma_mc_pair_to_nc(struct mem_chunk *src_mc, struct mem_chunk *dst_mc):
    if src_mc->mem_location != MEM_LOC_HOST:
        return src_mc->nc_id           // device source wins
    else:
        return dst_mc->nc_id           // host source ⇒ use the destination's NC (H2H uses dst's too)
```

The `| pci_host_base` is not a flag — it is the **host-BAR window selector** baked into the physical address the engine reads. A host buffer's `virt_to_phys` address has the `pci_host_base` bits OR-ed in so the Neuron device's DMA engine, looking at its own address space, routes the access back across PCIe into host memory; a device PA stands alone. This exact OR-in is open-coded — not via `ndma_mc_to_pa` — in five sites that take a raw `void *buffer` or a `mem_chunk` directly: `ndma_memcpy_add_completion_desc` (`:216`), `ndma_memcpy_mc` (`:613`/`:621`), `ndma_memcpy_buf_to_mc` (`:686`/`:690`), `ndma_memcpy_buf_from_mc` (`:707`/`:711`), and the zero-copy descriptor builder (`:1423`/`:1427`). The source author even flagged the redundancy: *"why isn't this already set???"* (`:100`).

> **NOTE —** `ndma_mc_pair_to_nc` resolves a **host→host** copy to `dst_mc->nc_id` (`:51`, comment at `:53`). The NC id only selects which engine's H2T ring is used; for an H2H copy neither end is on a NeuronCore, so the destination's NC is an arbitrary-but-consistent choice. A reimplementer who asserts "H2H has no NC" will trip the engine lookup. There is an acknowledged TODO at `:628` to give H2H a dedicated `nc_id == -1`.

### The async handle FSM

The classic async path double-buffers across three context slots per ring (`h2t_dma_ctx[3]`). The handle a transfer waits on is the *previous* transfer's handle, advanced by a tiny range-checked FSM:

```c
// ndma_dma_ctx_get_next_handle — neuron_dma.c:70.  prev handle -> next handle.
int ndma_dma_ctx_get_next_handle(int pdma_ctx_handle, int *dma_ctx_handle):
    if pdma_ctx_handle < NEURON_DMA_H2T_CTX_HANDLE_NONE       // -1
       or pdma_ctx_handle > NEURON_DMA_H2T_CTX_HANDLE_ASYNC2: // 2
        return -EINVAL                                         // :73
    switch pdma_ctx_handle:                                    // :76
        NONE   (-1): *dma_ctx_handle = ASYNC1   // start of an async chain
        SYNC    (0): *dma_ctx_handle = SYNC     // sync stays sync (wait on the xfer we just started)
        ASYNC1  (1): *dma_ctx_handle = ASYNC2   // toggle
        ASYNC2  (2): *dma_ctx_handle = ASYNC1   // toggle back
    return 0
```

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndma_mc_to_pa` | `neuron_dma.c:97` | `mem_chunk` → DMA PA (host OR-in `pci_host_base`, device raw) | CERTAIN |
| `ndma_mc_pair_to_nc` | `neuron_dma.c:46` | pick owning NC: device end wins; host source ⇒ dst's NC | CERTAIN |
| `ndma_dma_ctx_get_next_handle` | `neuron_dma.c:70` | async handle FSM `NONE→ASYNC1→ASYNC2→ASYNC1`; `SYNC→SYNC` | CERTAIN |
| `ndma_prefetch_user_pages` / `_ndma_prefetch_user_pages` | `neuron_dma.c:112 / 143` | best-effort fault-in of a user buffer (GUP then immediate `put_page`) | CERTAIN |
| `ndma_ack_completed_desc` | `neuron_dma.c:36` | recycle: `udma_cdesc_ack(rxq)` then `(txq)` (advances the SW completion counter) | CERTAIN |

> **NOTE —** `ndma_prefetch_user_pages` (`:112`) pins a user buffer with `get_user_pages_fast(FOLL_WRITE)` only to `put_page` every page immediately (`:130–132`) — it faults the pages in but does **not** keep them pinned. It is best-effort: every failure path is a bare `pr_info`, and the function returns `0` even after a partial pin (the `-ENOMEM` at `:124` on `kcalloc` failure is the *only* error actually returned). The classic path calls it once at offset 0 (`:499`) to warm the TLB ahead of a large copy; it is unrelated to the zero-copy *pinning* path (§4), which keeps its pages pinned.

---

## 2. The Classic Path — Transfer Split and Marker Poll

### Purpose

This is the canonical H2T (host-to-tensor) cycle and the heart of the page: an arbitrary-size copy is split into ≤64 KiB UDMA descriptor pairs, a `0xabcdef01` marker self-copy is appended, the ring is kicked, and the CPU polls the marker. It is the path behind `ndma_memcpy`, `ndma_memcpy_mc`, `ndma_memcpy_buf_to_mc`/`_from_mc`, the `memset` tail, and the descriptor copy-in validator. Per-ring it is serialized by `ring->h2t_ring_lock`.

### Entry Point

```text
ndma_memcpy_mc / _buf_to_mc / _buf_from_mc  (resolve src/dst PA + nc_id)
  └─ ndma_memcpy (neuron_dma.c:581)                      ── smove=true, dmove=true, SYNC,SYNC
       └─ ndma_memcpy_offset_move (neuron_dma.c:438)     ── THE classic entry core
            ├─ eng_id = ndhal_ndmar.ndmar_get_h2t_eng_id(nd, nc_id)   [DHAL]
            ├─ qid    = ndhal_ndmar.ndmar_get_h2t_def_qid(nc_id)      [DHAL: V2=0, V3=nc%2]
            ├─ mutex_lock(ring->h2t_ring_lock)
            ├─ LOOP:
            │    ├─ ndma_memcpy_chunks (neuron_dma.c:311)             ── STAGE + TRIGGER
            │    │    ├─ ndma_memcpy64k (:289) → udma_m2m_copy_prepare_one   [UDMA]
            │    │    ├─ ndma_memcpy_add_completion_desc (:200)       ── the 0xabcdef01 self-copy
            │    │    └─ udma_m2m_copy_start (:369)                   ── RX-then-TX doorbell  [UDMA]
            │    └─ _ndma_memcpy_wait_for_completion (:378)           ── POLL (+ V2 retry, §5)
            └─ mutex_unlock(ring->h2t_ring_lock)
```

### Algorithm — the transfer split

```c
// ndma_memcpy_chunks — neuron_dma.c:311.  Stage ≤64 KiB descriptors, append marker, trigger.
// const sync_threshold = DMA_H2T_DESC_COUNT/2 - UDMA_MAX_NUM_CDESC_PER_CACHE_LINE - 1
//                      = 4096/2 - 16 - 1 = 2031   (:321)
static int ndma_memcpy_chunks(eng, ring, dma_ctx):
    src        = dma_ctx->src                        // :323  original src PA
    dst        = dma_ctx->dst                         // :324
    remaining  = dma_ctx->remaining                   // :325
    offset     = dma_ctx->offset                      // :326
    done       = false
    pending_transfers = 0
    chunk_size = MAX_DMA_DESC_SIZE                     // :329  64 KiB

    while not done:                                   // :331
        if remaining <= MAX_DMA_DESC_SIZE:            // :335  last (short) chunk
            chunk_size = remaining
        if chunk_size == remaining or pending_transfers == sync_threshold:  // :339  flush boundary
            done = true

        // smove/dmove decide whether src/dst advance per chunk.
        // memset's d2d replicate uses smove=false (re-read the same 64 KiB), dmove=true.
        src_offset = dma_ctx->smove ? src + offset : src          // :343
        dst_offset = dma_ctx->dmove ? dst + offset : dst          // :344

        // barrier: WRITE_BARRIER on the last data desc (V2/V3), else NONE — DHAL-chosen
        ret = ndma_memcpy64k(eng, ring, src_offset, dst_offset, chunk_size,
                             ndhal->ndhal_ndma.ndma_get_m2m_barrier_type(done))   // :346
        if ret: return ret

        offset            += chunk_size               // :351
        remaining         -= chunk_size
        pending_transfers += 1

    // append the completion MARKER descriptor (BARRIER_NONE) — the 0xabcdef01 self-copy
    ret = ndma_memcpy_add_completion_desc(eng, ring, dma_ctx->completion_ptr,
                                          UDMA_M2M_BARRIER_NONE)    // :360
    if ret: return ret

    pending_transfers          += 1                   // :365  num = data descs + 1 completion
    dma_ctx->pending_transfers  = pending_transfers   // :366  doorbell count for both rings
    dma_ctx->outstanding        = dma_ctx->remaining - remaining   // :367  bytes moved THIS batch

    // TRIGGER: doorbell RX then TX with the SAME count (owned by udma-m2m / ring-cycle)
    return udma_m2m_copy_start(&eng->udma, ring->qid,
                               pending_transfers, pending_transfers)  // :369
```

The split is bounded *twice*. The per-descriptor bound is `MAX_DMA_DESC_SIZE = 65536` — the largest a single 16-bit length field can express (the `len==0 ⇒ 64 KiB` overload is handled in the [descriptor format](../dma/descriptor-format.md)). The per-*batch* bound is `sync_threshold = 2031` data descriptors: a batch plus its one completion descriptor (≤2032 total) always fits within half the nominal 4096-descriptor ring, so the producer (`next_desc_idx`) can never lap the consumer (`next_cdesc_idx`) before any ack arrives — the 16-descriptor guard gap (`UDMA_MAX_NUM_CDESC_PER_CACHE_LINE`) reserved by `udma_available_get` is preserved by construction. When a transfer exceeds `sync_threshold × 64 KiB`, `ndma_memcpy_offset_move`'s outer loop (`:491`) re-stages successive batches, advancing `offset`/`remaining` by `outstanding` each lap (`:523–524`).

### Algorithm — the completion descriptor and the marker poll

```c
// ndma_memcpy_add_completion_desc — neuron_dma.c:200.  The 4-byte host self-copy.
int ndma_memcpy_add_completion_desc(eng, ring, completion_buffer, barrier_type):
    dst = (volatile u32 *)(completion_buffer + DMA_COMPLETION_MARKER_SIZE)   // :209  poll slot @ +4
    src = (volatile u32 *) completion_buffer                                 // :210  marker slot @ +0
    WRITE_ONCE(*src, DMA_COMPLETION_MARKER)            // :213  0xabcdef01 pre-seeded
    WRITE_ONCE(*dst, 0)                                // :214  poll slot starts cleared
    addr = virt_to_phys(completion_buffer) | ndhal->ndhal_address_map.pci_host_base  // :216
    // a 4-byte host->host self-copy: executing it writes 0xabcdef01 from +0 into +4
    return udma_m2m_copy_prepare_one(&eng->udma, ring->qid,
                                     addr, addr + DMA_COMPLETION_MARKER_SIZE,
                                     DMA_COMPLETION_MARKER_SIZE, barrier_type, false)  // :217

// ndma_memcpy_wait_for_completion — neuron_dma.c:230.  Poll the host marker, no IRQ.
int ndma_memcpy_wait_for_completion(eng, ring, count, ptr, async, is_intra_device_dma):
    // per-arch budget: V2 est=4*(count-1); V3 est=2*(count-1)  (DHAL vtable)
    ndhal->ndhal_ndma.ndma_get_wait_for_completion_time(count, async,
                                                        &first_wait_time, &wait)   // :238
    if narch_is_qemu() or narch_is_emu():
        wait = wait * 100 * 1000                       // :241  virtual platforms: ×100000
    if is_intra_device_dma and not async:              // :243  d2d is fast (no PCIe round-trip)
        first_wait_time = 10                           // :244  fixed 10 µs first wait
        wait = wait / 200                              // :245  much shorter budget
    one_loop_sleep = 1                                 // :248  poll every 1 µs
    loop = wait / one_loop_sleep + 1                   // :249

    dst = (volatile u32 *)(ptr + DMA_COMPLETION_MARKER_SIZE)   // :251  poll slot @ +4
    src = (volatile u32 *) ptr                                 // :252  marker source @ +0

    udelay(first_wait_time)                            // :262
    for i = 0 .. loop:                                 // :263
        if READ_ONCE(*dst) == DMA_COMPLETION_MARKER:   // :266  0xabcdef01 -> every prior desc retired
            WRITE_ONCE(*dst, 0)                        // :268  re-arm poll slot
            WRITE_ONCE(*src, DMA_COMPLETION_MARKER)    // :269  re-seed marker source
            ndma_ack_completed_desc(eng, ring, count)  // :274  RECYCLE — advance the SW counter
            break
        udelay(one_loop_sleep)                         // :277  1 µs
    if i > loop:                                       // :279
        return -ETIMEDOUT                              // :281
    return 0
```

> **QUIRK — completion is a polled host-memory marker, not an interrupt.** There is no hardware completion ring on the Neuron M2M path (`cdesc_base == NULL` on every queue) and the driver never arms the descriptor's `INT_EN` bit for completion. Instead, `ndma_memcpy_add_completion_desc` stages a **4-byte host→host self-copy** as the last descriptor: source slot `+0` is pre-seeded with `0xabcdef01` (`:213`), destination slot `+4` is cleared (`:214`), and executing the copy writes `0xabcdef01` into `+4`. Because UDMA retires a ring **in order** and the last *data* descriptor carries the per-arch write barrier (`WRITE_BARRIER` on V2, `SOW` on V3+ — chosen by `ndma_get_m2m_barrier_type`), by the time `+4` reads `0xabcdef01`, every prior data write has landed. The CPU busy-polls `+4` every 1 µs (`:266`); on match it re-arms both words (`:268–269`, so the buffer can be reused and stale-reuse is detectable) and calls `ndma_ack_completed_desc` to advance the **software** completion counter via `udma_cdesc_ack` — without that ack the ring would run out of allocatable descriptors, since no hardware CQ recycles them (the in-source comment at `:270–273` says exactly this). A reimplementer who provisions a real completion ring or waits on `INT_EN` will find the engine never feeds either on this path. The cycle-level mechanics (doorbell, RX-then-TX order, the recycle CAS) are owned by [ring-cycle](../dma/ring-cycle.md).

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndma_memcpy_offset_move` | `neuron_dma.c:438` | classic entry core: resolve eng/qid, lock, loop chunks→prefetch→wait, unlock | CERTAIN |
| `ndma_memcpy_chunks` | `neuron_dma.c:311` | stage ≤64 KiB descriptors to `sync_threshold`, append marker, trigger | CERTAIN |
| `ndma_memcpy64k` | `neuron_dma.c:289` | one descriptor pair via `udma_m2m_copy_prepare_one` | CERTAIN |
| `ndma_memcpy_add_completion_desc` | `neuron_dma.c:200` | append the `0xabcdef01` 4-byte host self-copy completion descriptor | CERTAIN |
| `ndma_memcpy_wait_for_completion` | `neuron_dma.c:230` | 1 µs marker poll; reset + `ack` on match; `-ETIMEDOUT` on budget | CERTAIN |
| `ndma_memcpy` | `neuron_dma.c:581` | public `memcpy`: `offset_move(smove=dmove=true, SYNC, SYNC)` | CERTAIN |
| `ndma_memcpy_mc` | `neuron_dma.c:606` | `mem_chunk → mem_chunk`: resolve PA + NC, sync memcpy | CERTAIN |
| `ndma_memcpy_buf_to_mc` / `_buf_from_mc` | `neuron_dma.c:679 / 700` | raw host buffer ↔ `mem_chunk`, sync | CERTAIN |
| `ndma_memcpy_dma_copy_descriptors` | `neuron_dma.c:776` | IOCTL ring-copy: validate each desc PA (`ndma_validate_pa`) then copy | CERTAIN |
| `udma_m2m_copy_prepare_one` / `_start` | [UDMA M2M](udma-m2m.md) | stage a TX+RX pair / RX-then-TX doorbell | HIGH (boundary) |
| `ndma_get_m2m_barrier_type` / `ndma_get_wait_for_completion_time` | DHAL `v2`/`v3` | per-arch barrier choice / poll budget | HIGH (boundary) |

### Considerations

> **NOTE —** the poll **budget** is the most version-sensitive number on this page and is intentionally delegated. `ndma_get_wait_for_completion_time` is a DHAL vtable call (`:238`): V2 computes `est = 4*(count-1)` (`v2/neuron_dhal_v2.c:989`), V3 `est = 2*(count-1)` (`v3/neuron_dhal_v3.c:1293`). The op layer then applies three platform scalings on top — ×100000 on qemu/emu (`:241`), and for an intra-device (`d2d`) copy a fixed 10 µs first-wait with the budget ÷200 (`:244–245`). A reimplementer should treat the *shape* (count-scaled estimate, 1 µs poll, reset-on-match, `-ETIMEDOUT`) as the contract and re-derive the constants per generation. The `is_intra_device_dma` argument is named `is_d2d` in the header (`neuron_dma.h:176`) — same parameter.

> **NOTE —** `ndma_memcpy_dma_copy_descriptors` (`:776`) is the IOCTL ring-program path, not a byte copy: it walks an array of `union udma_desc` and validates every descriptor's `buf_ptr` (TX, `:787`) or `buf1_ptr` (RX, `:789`) through the DHAL `ndma_validate_pa` *before* committing — the security gate against a userspace-authored ring pointing at memory it does not own. Only after all PAs pass does it `memcpy` (host dest) or `ndma_memcpy_buf_to_mc` (device dest). A reimplementer must keep the validate-before-copy ordering; validating after the copy is too late.

---

## 3. The Classic Async Double-Buffer

### Purpose

For pipelined transfers the runtime issues a copy and continues, waiting later. `ndma_memcpy_mc_async` overlaps the *staging* of one transfer with the *completion* of the previous one by ping-ponging between two context slots (`ASYNC1`/`ASYNC2`), each with its own completion marker buffer. It is the same five-phase cycle as §2, only the wait targets the *previous* ctx instead of the just-staged one.

### Algorithm

```c
// ndma_memcpy_mc_async — neuron_dma.c:586.  Submit; wait on the PREVIOUS ctx.
int ndma_memcpy_mc_async(nd, src_mc, dst_mc, src_off, dst_off, size,
                         prefetch_addr, pdma_ctx_handle, *dma_ctx_handle):
    ret = ndma_dma_ctx_get_next_handle(pdma_ctx_handle, dma_ctx_handle)   // :593  FSM toggle
    if ret: return ret
    nc_id  = ndma_mc_pair_to_nc(src_mc, dst_mc)                            // :599
    src_pa = ndma_mc_to_pa(src_mc) + src_off                               // :600
    dst_pa = ndma_mc_to_pa(dst_mc) + dst_off                               // :601
    // offset_move with pwait = the PREVIOUS handle, wait = THIS new handle:
    //   stage THIS xfer's chunks, then poll the PREVIOUS ctx (pipelining)
    return ndma_memcpy_offset_move(nd, nc_id, src_pa, dst_pa, size,
                                   true, true, prefetch_addr,
                                   pdma_ctx_handle, *dma_ctx_handle)       // :603

// inside ndma_memcpy_offset_move (:438), the async distinction:
//   dma_ctx  = ndma_get_dma_ctx(eng, ring, wait_handle)   // THIS transfer's ctx     :451
//   pdma_ctx = used_for_h2t ? ndma_get_dma_ctx(eng, ring, pwait_handle) : dma_ctx    :452
//   ... stage dma_ctx's chunks (ndma_memcpy_chunks) ...                              :493
//   ... then _ndma_memcpy_wait_for_completion(nd, nc_id, qid, eng, ring,
//                                              pdma_ctx, dma_ctx)                     :505
//   async = (pdma_ctx != dma_ctx)   // wait the PREVIOUS, not THIS                    :382

// ndma_memcpy_mc_wait — neuron_dma.c:644.  Drain a specific async ctx.
int ndma_memcpy_mc_wait(nd, src_mc, dst_mc, dma_ctx_handle):
    if not eng->used_for_h2t: return 0          // :656  non-H2T already sync under the covers
    dma_ctx = ndma_get_dma_ctx(eng, ring, dma_ctx_handle)
    if dma_ctx == NULL: return -EINVAL          // :663
    if not dma_ctx->inuse:                       // :666  validate state
        return -EINVAL
    ret = _ndma_memcpy_wait_for_completion(nd, nc_id, qid, eng, ring,
                                           dma_ctx, dma_ctx)   // :671  wait THIS ctx
    ndma_release_dma_ctx(eng, ring, dma_ctx)     // :673  clears inuse (H2T) / frees (non-H2T)
    return ret
```

The ctx slot is selected by `ndma_memcpy_get_completion_buf` (`:160`): for an H2T ring the completion buffer is a preallocated slice `ring->h2t_completion.ptr + handle * 2 * DMA_COMPLETION_MARKER_SIZE` (`:163`) — three handles × two `u32` slots = the 24-byte marker buffer the ring carries; for a non-H2T engine it falls back to `kmalloc` (`:165`). `ndma_get_dma_ctx` (`:168`) and `ndma_release_dma_ctx` (`:180`) mirror this split: H2T contexts are slots in `h2t_dma_ctx[3]` cleared by `inuse = false`, non-H2T contexts are heap-allocated and freed.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndma_memcpy_mc_async` | `neuron_dma.c:586` | submit one async transfer; toggle handle; pipeline-wait previous | CERTAIN |
| `ndma_memcpy_mc_wait` | `neuron_dma.c:644` | drain a specific async ctx; release it | CERTAIN |
| `ndma_memcpy_get_completion_buf` | `neuron_dma.c:160` | per-handle marker buffer (H2T preallocated slice / non-H2T `kmalloc`) | CERTAIN |
| `ndma_get_dma_ctx` / `ndma_release_dma_ctx` | `neuron_dma.c:168 / 180` | acquire/release a classic transfer ctx (H2T slot vs heap) | CERTAIN |

> **QUIRK —** the async chain is bounded to **one batch per ctx**. `ndma_memcpy_offset_move` breaks its restage loop with `-EINVAL` ("Async dma request … is too large", `:516`) if an async transfer (`dma_ctx != pdma_ctx`) does not complete in a single `outstanding == remaining` pass (`:511`). Only the SYNC path restages multiple batches; async transfers must each fit under `sync_threshold × 64 KiB` in one shot. A reimplementer who pipelines arbitrarily large async copies will hit this gate.

---

## 4. The Zero-Copy Path — Ctx Queue and Pinned Pages

### Purpose

Zero-copy avoids the bounce-buffer of the classic path: it pins the user's pages directly with `pin_user_pages*` and DMAs over their (non-contiguous) physical addresses, coalescing runs of physically contiguous pages into ≤64 KiB descriptors. To overlap pinning with DMA it drives a **4-pointer circular context queue** (`ndma_ctx_queue`) whose regions are `head ≤ first_pinned_unsubmitted ≤ first_unpinned ≤ tail`. Sync mode drains in-line; async mode defers via a captured `mm` and a shared H2D completion queue consumed by a separate kthread.

### The ctx-queue state machine

A `ndma_h2t_zcdma_context` (`neuron_dma.c:922`) moves through five states (`enum ndma_zcdma_state`, `:913`):

| State | Value | Meaning | Region |
|---|---|---|---|
| `NDMA_INVALID` | 0 | tombstone / unused slot | (skipped by `advance_to_valid`) |
| `NDMA_UNPINNED` | 1 | ctx created, pages not yet pinned (async deferred) | `[first_unpinned, tail)` |
| `NDMA_PINNED_UNSUBMITTED` | 2 | pages pinned, descriptors not built | `[first_pinned_unsubmitted, first_unpinned)` |
| `NDMA_SUBMITTED` | 3 | descriptors issued, awaiting the marker | `[head, first_pinned_unsubmitted)` |
| `NDMA_COMPLETED` | 4 | popped out of the queue | — |

The four cursors partition the circular queue into three live regions; the three `inc_*` helpers (`:1106`/`:1115`/`:1124`) advance them and lazily seed `first_pinned_unsubmitted`/`first_unpinned` when their region first becomes non-empty (`:1132–1146`), while `ndma_ctx_queue_advance_to_valid` (`:1072`) skips `INVALID` tombstones left by released contexts. The whole queue is capacity **1024** (power-of-two, mask 1023, `:1068`/`:1282`) with a global pin budget of **524288** pages (≈2 GB at 4 KiB, `:1069`).

### Algorithm — submit

```c
// ndma_zerocopy_submit — neuron_dma.c:1603.  THE zero-copy entry.  async = (sequence_num != 0).
int ndma_zerocopy_submit(nd, nc_id, ops, num_ops, dev_base, qid, direction, sequence_num):
    if not ndmar_h2t_ring_is_owner(ring, nc_id): return -ENOENT   // :1624  ownership gate
    mutex_lock(&ring->h2t_ring_lock)                              // :1630

    for op in ops[0 .. num_ops):                                  // :1632  nrt_tensor_batch_op_t
        op_ctx = { host_addr=op->buffer, dev_addr=dev_base+op->offset,
                   offset=host_addr & (PAGE_SIZE-1), remaining=op->size }
        op_ctx.pin_size = ndma_calc_zc_pin_size(op_ctx.remaining + op_ctx.offset)  // :1639

        while op_ctx.remaining:                                   // :1641
            // Step 1: submit any PINNED_UNSUBMITTED ctx that has descriptor room
            while (uc = peek_pinned_unsubmitted(ctx_queue)) and
                  _ndma_zc_descs_available(eng, qid, uc->nr_pages):       // :1647
                ndma_build_n_issue_zc_descs(uc)                  // :1654  coalesce+stage+trigger
                inc_first_pinned_unsubmitted(ctx_queue)          // :1660

            // Step 2: create the current ctx if there is room and pin budget
            nr_pages = DIV_ROUND_UP(op_ctx.pin_size, PAGE_SIZE)  // :1664
            can_pin  = nr_pinned_pages + nr_pages <= MAX_PINNED_PAGES  // :1665
            if async and ctx_queue_is_full: return -EBUSY        // :1668  async never blocks
            if (can_pin or async) and not full:                  // :1674
                cur = peek_tail(ctx_queue); fill cur from op_ctx
                cur->last = (cur->size == op_ctx.remaining && op is last)   // :1683
                if can_pin:
                    ndma_zerocopy_pin_pages(.., cur, use_remote=false)     // :1692  local pin now
                else if async:
                    mmget(current->mm); cur->mm = current->mm     // :1698  defer: capture mm
                inc_tail(ctx_queue)                               // :1706
                advance op_ctx cursors by cur->size               // :1709

            // Step 3 (SYNC only): relieve pressure — wait+release submitted ctxs
            if not async:                                         // :1718
                while ndma_zc_should_wait(eng, ring, ctx_queue, &threshold):  // :1721
                    s = pop_submitted(ctx_queue)
                    ndma_memcpy_wait_for_completion(.., s->nr_desc+1, s->completion_ptr, ..)
                    ndma_zc_release_ctx(s, &nr_pinned_pages)      // :1727  unpin

    if not async:
        // Step 4 (SYNC): submit remaining pinned + drain all submitted (loop until both empty)
        while true:                                              // :1739
            if (ts = peek_pinned_unsubmitted) and descs_available: build_n_issue(ts); inc_fpu
            if (tw = pop_submitted): wait_for_completion(tw); release(tw)
            if not ts and not tw: break

done:
    if ret: ndma_ctx_queue_drain(eng, ring, ctx_queue)            // :1771  unpin everything on error
    mutex_unlock(&ring->h2t_ring_lock)
    return ret
```

### Algorithm — build and issue zero-copy descriptors

```c
// ndma_build_n_issue_zc_descs — neuron_dma.c:1393.  Coalesce contiguous pinned pages.
static int ndma_build_n_issue_zc_descs(dma_ctx):
    offset = host_addr & (PAGE_SIZE-1)                  // may start mid-page  :1396
    while i < nr_pages:                                  // :1405
        contig_start = page_to_phys(page_list[i++])
        contig_size  = PAGE_SIZE - offset
        while next page is physically contiguous: extend contig_size += PAGE_SIZE   // :1413
        if direction (write to device):                 // :1422
            src = (contig_start + offset) | pci_host_base ; dst = dev_addr
        else (read from device):
            src = dev_addr ; dst = (contig_start + offset) | pci_host_base
        offset = 0                                       // only the first page is unaligned
        while contig_size > 0 and remaining > 0:         // :1433
            chunk = min(remaining, contig_size, MAX_DMA_DESC_SIZE)
            // V2: WRITE_BARRIER on the LAST data desc iff write+last; else NONE   :1450-1451
            // V3+: NONE here (SOW goes on the completion desc below)              :1452-1453
            barrier = (V2 && remaining==chunk && direction && last) ? WRITE_BARRIER : NONE
            udma_m2m_copy_prepare_one(.., src, dst, chunk, barrier, false)   // :1455
            advance dev_addr/src/dst, remaining/contig_size -= chunk; pending++
    dma_ctx->nr_desc = pending                           // :1469
    // V3+: SOW on the COMPLETION desc iff write+last; V2: NONE (barrier was on last data desc)
    barrier = (V3+ && direction && last) ? UDMA_M2M_BARRIER_SOW : NONE        // :1471-1474
    ndma_memcpy_add_completion_desc(.., completion_ptr, barrier)             // :1475
    udma_m2m_copy_start(.., pending+1, pending+1)        // :1482  TRIGGER
    dma_ctx->state = NDMA_SUBMITTED                       // :1486
```

The barrier policy is the one place data-fabric ordering is argued in source (`:1437–1453`): on the **read** path the in-order S2M completion write already follows all data writes, so no barrier is needed; on the **write** path HBM data and host completion take *different* fabric routes, so the completion could overtake the data — hence `WRITE_BARRIER` on the last data descriptor (V2) or `SOW` on the completion descriptor (V3+), and only for the *last* set of pinned pages of a write request. Unpinning is always safe regardless of barrier: S2M descriptors execute in order, so the completion write executing implies all M2S reads executed.

### Algorithm — async completion (the CQE producer)

```c
// ndma_zerocopy_complete — neuron_dma.c:1778.  __maybe_unused here (driven by a kthread, BOUNDARY).
static int ndma_zerocopy_complete(nd, eng, ring, *did_work):
    mutex_lock(&ring->h2t_ring_lock)
    // 1) wait submitted ctxs; on success&last post a success CQE; on error post err + drain seq
    while not submitted_empty and (first pass or should_wait):   // :1798
        s = pop_submitted(ctx_queue)
        ret = ndma_memcpy_wait_for_completion(.., s->nr_desc+1, s->completion_ptr, true, false)
        if ret: ndma_h2d_compl_queue_put(seq=s->seq, ret, NULL); drain_sequence(s->seq)   // :1811
        else if s->last: ndma_h2d_compl_queue_put(seq=s->seq, 0, NULL)                      // :1814
        ndma_zc_release_ctx(s, &nr_pinned_pages)
    // 2) submit pinned-unsubmitted ctxs   // :1823
    // 3) remote-pin UNPINNED ctxs via the captured mm (pin_user_pages_remote)   // :1844

// ndma_h2d_compl_queue_put — neuron_dma.c:1022.  Lock-free SPMC producer into the mmapped ring.
static void ndma_h2d_compl_queue_put(compl_queue, sequence_num, compl_ret, context):
    head = smp_load_acquire(&shared->head)               // :1032  runtime consumer index
    while (tail - head) >= (capacity_mask + 1):          // :1035  full → block
        pr_warn_once(...); msleep(1); head = smp_load_acquire(&shared->head)   // :1037-1038
    entry = &shared->entries[tail & capacity_mask]       // :1042
    entry->{compl_ret, context, sequence_num} = ...      // :1045-1047
    compl_queue->tail = tail + 1
    smp_store_release(&shared->tail, compl_queue->tail)   // :1051  publish to userspace
```

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndma_zerocopy_submit` | `neuron_dma.c:1603` | zero-copy entry: per-op pin/submit/wait pipeline; sync drain / async defer | CERTAIN |
| `ndma_build_n_issue_zc_descs` | `neuron_dma.c:1393` | coalesce contiguous pinned pages → ≤64 KiB descs; barrier; trigger | CERTAIN |
| `ndma_zerocopy_pin_pages` | `neuron_dma.c:1536` | local (`pin_user_pages_fast`, fallback `pin_user_pages`) / remote (`_remote`) pin | CERTAIN |
| `ndma_zc_release_ctx` | `neuron_dma.c:949` | unpin (dirty-lock on read), `mmput`, tombstone the ctx | CERTAIN |
| `ndma_calc_zc_pin_size` | `neuron_dma.c:1359` | per-step pin size: cap at 64 pages, else `round_up(size/2)` | CERTAIN |
| `ndma_zc_should_wait` | `neuron_dma.c:1515` | back-pressure: pinned-at-max / desc-ring-full / ctx-queue-full; raises threshold to HI(520) | CERTAIN |
| `ndma_ctx_queue_*` (init/free/inc/peek/pop/drain) | `neuron_dma.c:1072–1357` | 4-pointer circular ctx-queue FSM | CERTAIN |
| `ndma_h2d_compl_queue_init` / `_put` / `_destroy` | `neuron_dma.c:975 / 1022 / 1011` | SPMC CQE ring (cap 1024) mmapped to userspace | CERTAIN |
| `ndma_zerocopy_complete` | `neuron_dma.c:1778` | async completion kthread body (wait→post CQE, submit, remote-pin) | HIGH (kthread caller BOUNDARY) |
| `ndma_zerocopy_supported` | `neuron_dma.c:1375` | `!ndma_retry_memcpy \|\| zerocopy_trn1_override` | CERTAIN |

> **NOTE —** `ndma_zerocopy_complete` is `__maybe_unused` *in this translation unit* (`:1778`) — the per-ND completion kthread that calls it lives in another cell ([K-RING / K-CDEV](dma-rings.md)), and its scheduling and interaction with process exit (a captured `mm` outliving the submitting task) are a boundary not confirmed here. The CQE ring itself is fully grounded: a single driver writer per ND (`tail`, `smp_store_release`) and many runtime readers (`head`, consumed in userspace) — a textbook SPMC ring requiring exactly the acquire/release barriers at `:1032`/`:1051`.

---

## 5. The V2 Reset-Window HW-Bug Workaround

### Purpose

V2 silicon (Trn1 / Inf2) can **hang a DMA** if a memcpy starts inside a NeuronCore reset window, and the host marker then never flips. The classic wait path detects this specific timeout and recovers by re-initializing the ring and replaying the staged chunks, rather than failing the copy. This is the single most surprising control-flow wrinkle in the file, and it is why zero-copy is disabled on these archs.

### Algorithm

```c
// _ndma_memcpy_wait_for_completion — neuron_dma.c:378.  Wait + V2 reset-window retry.
static int _ndma_memcpy_wait_for_completion(nd, nc_id, qid, eng, ring, dma_ctx, ndma_ctx):
    async = (dma_ctx != ndma_ctx)                        // :382
    while true:
        ret = ndma_memcpy_wait_for_completion(eng, ring, dma_ctx->pending_transfers,
                                              dma_ctx->completion_ptr, async, false)   // :386
        if ret == 0: return 0                            // :388  marker matched

        // timeout — only retry on the specific V2 reset-window condition:
        if not ndhal->ndhal_ndma.ndma_retry_memcpy: break          // :394  arch doesn't need it
        if not nr_op_in_reset_wnd(dma_ctx->start_time, nd): break   // :398  not a reset-window hang

        dma_ctx->start_time = get_jiffies_64()           // :405  restart the reset-window clock
        ret = ndmar_h2t_ring_init(eng, qid)              // :407  RE-INIT the ring  [K-RING]
        if ret: break                                    // :409
        ret = ndma_memcpy_chunks(eng, ring, dma_ctx)     // :416  REPLAY this ctx's chunks
        if ret: break
        if dma_ctx != ndma_ctx:                          // :420  async: replay the OTHER ctx too
            ret = ndma_memcpy_chunks(eng, ring, ndma_ctx)
            if ret: break
        async = false                                    // :426  next wait is synchronous
    return ret
```

> **GOTCHA — the V2 `ndma_retry_memcpy` workaround re-stages descriptors a *second* time and is gated by two conditions, not one.** A `-ETIMEDOUT` from the marker poll is only retried when **both** `ndhal_ndma.ndma_retry_memcpy` is true (a per-arch DHAL flag — V2 only) **and** `nr_op_in_reset_wnd(dma_ctx->start_time, nd)` reports the copy began inside a NeuronCore reset window (`:394`/`:398`). When it fires, the recovery is *not* a re-poll: it re-initializes the entire H2T ring (`ndmar_h2t_ring_init`, `:407`) and re-runs `ndma_memcpy_chunks` to **stage and trigger the same chunks again** (`:416`), then loops back to poll once more. For an async transfer it replays *both* the waited ctx and the staging ctx (`:420–421`) and demotes the next iteration to synchronous (`async = false`, `:426`). A reimplementer must reproduce three things or risk either a spurious double-DMA or an undetected hang: (1) the *conjunction* of the two gates — retrying on every timeout would replay descriptors after a genuine failure; (2) the ring re-init *before* the replay — replaying onto a hung ring repeats the hang; (3) resetting `start_time` each lap (`:405`) so a stuck transfer eventually exits the reset window and the loop terminates. On archs without the bug, the first `break` (`:394`) returns the timeout unchanged. The companion gate is at the format level: **zero-copy is disabled on retry archs** — `ndma_zerocopy_supported` (`:1375`) returns `!ndma_retry_memcpy || zerocopy_trn1_override`, so V2 only does zero-copy when the `zerocopy_trn1_override` module param (`:28`) is explicitly set, because the pin/replay interaction is not validated against the HW bug.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `_ndma_memcpy_wait_for_completion` | `neuron_dma.c:378` | wait wrapper + V2 reset-window retry (re-init ring, replay chunks) | CERTAIN |
| `ndma_retry_memcpy` (DHAL flag) | `ndhal_ndma` | per-arch: true on V2, gates the retry | HIGH (boundary) |
| `nr_op_in_reset_wnd` | K-RESET | did this copy start inside a NeuronCore reset window? | HIGH (boundary) |
| `ndmar_h2t_ring_init` | [K-RING](dma-rings.md) `neuron_ring.c:434` | re-program the H2T ring (NULL completion ring) before replay | HIGH (boundary) |
| `ndma_zerocopy_supported` | `neuron_dma.c:1375` | disables zero-copy on retry archs unless `zerocopy_trn1_override` | CERTAIN |

---

## 6. memset, Host-Memory Validation, and BAR0 Write-Block

### Purpose

Three remaining surfaces ride on the primitives above: `memset` (a host-fill then a device-to-device replicate), a host-memory PA validator used when a userspace-authored descriptor ring is copied in, and a BAR0 write-block table that prevents a userspace `mmap` of the device registers from ringing its own doorbell or repointing a ring.

### Algorithm — memset

```c
// ndma_memset — neuron_dma.c:547.  Fill <=64 KiB via a host buffer, replicate the rest d2d.
int ndma_memset(nd, mc, offset, value, size):
    mutex_lock(&nd->memset_lock)                         // :553  (in addition to h2t_ring_lock)
    transfer_size = min(size, MEMSET_HOST_BUF_SIZE)      // :556  MEMSET_HOST_BUF_SIZE = 64 KiB
    memset(nd->memset_mc->va, value, transfer_size)      // :557  fill the preallocated host buf
    ndma_memcpy_mc(nd, memset_mc, mc, 0, offset, transfer_size)   // :560  host -> device (first chunk)
    remaining = size - transfer_size
    if remaining:                                        // :566  replicate the just-written region
        // device->device: src does NOT advance (smove=false), dst advances (dmove=true)
        ndma_memcpy_offset_move(nd, mc->nc_id, mc->pa+offset, mc->pa+offset+transfer_size,
                                remaining, /*smove=*/false, /*dmove=*/true, 0, SYNC, SYNC)   // :568
    mutex_unlock(&nd->memset_lock)
```

`memset` is a two-stage trick: it fills *one* ≤64 KiB host buffer with the byte pattern, DMAs it to the device once (`:560`), then — for anything larger — replicates that just-written device region forward with a device-to-device copy whose source pointer is held fixed (`smove=false`) while the destination advances (`dmove=true`, `:568`). The d2d replicate takes the fast `is_intra_device_dma` poll path (§2). This is the one caller that exercises the `smove=false` mode of `ndma_memcpy_chunks`.

### Algorithm — validation and BAR0 block

```c
// ndma_is_valid_host_mem — neuron_dma.c:744.  Is `pa` a host buffer we allocated?
bool ndma_is_valid_host_mem(nd, pa):
    if ndma_is_valid_host_mem_from_nd(nd->device_index,     pa): return true   // current ND
    if ndma_is_valid_host_mem_from_nd(nd->device_index - 1, pa): return true   // neighbor -1
    if ndma_is_valid_host_mem_from_nd(nd->device_index + 1, pa): return true   // neighbor +1
    for i in all devices (skipping the three already checked):                 // :761
        if ndma_is_valid_host_mem_from_nd(i, pa): return true
    pr_err("invalid host memory in DMA descriptor"); return false              // :772
// _from_nd (:724): bounds nd_index; npid_is_attached; mpset_search_mc(pa) under rblock  [K-MEMPOOL]

// neuron_dma.c:869 — the BAR0 write-block set: ring base ptrs + the doorbell.
static const u64 udma_blocked[] = { drbp_low, drbp_high, crbp_low, crbp_high, drtp_inc };
// ndma_bar0_blocked_one_engine (:872): for both dirs (m2s/s2m) × every queue, reject a
//   userspace BAR0 write whose offset equals a per-queue blocked register. Returns -1 if blocked.
```

`ndma_is_valid_host_mem` checks the current ND first, then its two neighbors, then every device — the "chaining" search reflects that a collective can place host buffers on a neighbor's mempool. Each check requires the owning ND to have an attached PID (`npid_is_attached`, `:734`) and the PA to resolve in that ND's mempool rbtree (`mpset_search_mc`, `:738`). The BAR0 block table and its enforcement are owned by the [ring-cycle](../dma/ring-cycle.md#3-trigger-and-doorbell--launching-the-batch) page (the doorbell is the protected register); they are *defined* here because the offsets are computed from the UDMA register structs this layer already includes.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndma_memset` | `neuron_dma.c:547` | host-fill ≤64 KiB then device→device replicate; adds `memset_lock` | CERTAIN |
| `ndma_is_valid_host_mem` | `neuron_dma.c:744` | validate a descriptor host PA across this ND + neighbors + all devices | CERTAIN |
| `ndma_is_valid_host_mem_from_nd` | `neuron_dma.c:724` | per-ND check: attached-PID gate + mempool rbtree lookup | CERTAIN |
| `ndma_bar0_blocked_one_engine` | `neuron_dma.c:872` | reject userspace BAR0 writes to the 5 blocked per-queue registers | CERTAIN |
| `ndmar_queue_read_state` / `ndmar_queue_get_state` | `neuron_dma.c:810 / 851` | read masked TX/RX ring registers for sysfs/state plane | CERTAIN |

---

## Related Components

| Name | Relationship |
|---|---|
| `struct ndma_h2t_dma_context` (`neuron_ring.h:139`) | the classic SYNC/ASYNC transfer cursor; 3 per ring; operated on entirely in this file |
| `struct ndma_ctx_queue` (`neuron_ring.h:118`) | the 4-pointer zero-copy ctx queue this file's FSM drives |
| `struct ndma_h2d_compl_queue` / `neuron_h2d_dma_compl_queue_t` | the SPMC CQE ring (driver writer / runtime reader) posted by `ndma_h2d_compl_queue_put` |
| `nrt_tensor_batch_op_t` (`share/…tensor_batch_op.h:18`) | the `{offset, size, buffer}` input op descriptor `ndma_zerocopy_submit` consumes |
| DHAL `ndhal_ndma` / `ndhal_ndmar` / `ndhal_address_map` | per-arch barrier/wait-time/retry, H2T eng+qid, and `pci_host_base` |

## Cross-References

- [DMA Rings and H2T Queues](dma-rings.md) — the ring container this op layer drives; owns `ndmar_h2t_ring_init` (re-init on V2 retry), `ndmar_queue_copy_start`/`ndmar_ack_completed`, and the H2T-ring discriminator
- [UDMA Core (Annapurna/Alpine Fork)](udma-main.md) — `udma_m2m_copy_prepare_one`/`_start`, `udma_cdesc_ack` (the recycle CAS this layer calls), `udma_available_get`, and `struct udma_q`
- [The Ring / Trigger / Doorbell / Completion Cycle](../dma/ring-cycle.md) — the dynamic stage→trigger→doorbell→poll→ack cycle this layer's primitives execute; owns `sync_threshold`, the `0xabcdef01` marker, the `drtp_inc` doorbell, and the BAR0 gate
- [DMA & Descriptor Engine — Part Map](../dma/overview.md) — the unified DMA frame; where this kernel op layer sits relative to the userspace `vring`/`pring` authoring path
