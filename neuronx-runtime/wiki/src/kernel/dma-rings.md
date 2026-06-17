# DMA Rings and H2T Queues

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0** (`usr/src/aws-neuronx-2.27.4.0/`). The core file is `neuron_ring.c` (873 lines) and `neuron_ring.h` (461 lines), read verbatim. Boundary structs are cited at their own definition site (`udma/udma.h`, `share/neuron_driver_shared.h`, `neuron_device.h`, the `v2`/`v3` DHAL). The driver also ships as a stripped `.ko`, but the GPL C is authoritative — every offset below is a struct field, not a recovered binary offset.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`neuron_ring` is the kernel driver's **DMA ring abstraction** layer. It does not build descriptors and it does not ring a doorbell; it owns the *container* hierarchy that wraps each AnnapurnaLabs/Alpine UDMA hardware engine (`struct udma`) into a driver-friendly nest of engines, queues, and descriptor rings, and it owns the lifecycle of the driver's own **H2T** ("host-to-tensor") memcpy rings. Think of it as the *queue-pair management* layer of a NIC or NVMe driver: the layer that knows "this device has N hardware DMA engines, each with M queues, each queue is a pair of submission rings backed by host or device memory" — but which delegates the actual descriptor wire format to a lower layer and the per-generation geometry to a hardware-abstraction vtable.

The mental model is a classic descriptor-ring driver with one Alpine twist. A NIC queue is one TX ring plus one RX ring of `{len, flags, addr}` entries, a head the hardware advances and a tail the driver writes via a doorbell. A Neuron UDMA queue is the same shape — but a *copy* is always a **pair**: a TX/M2S ring whose descriptors read the **source** and an RX/S2M ring whose descriptors write the **destination** (the [16-byte descriptor](../dma/descriptor-format.md) is owned by a sibling page). `neuron_ring` binds host-memory `mem_chunk` buffers into those two rings, programs them into the hardware through the UDMA HAL, and tracks ownership so a crashing process's queues get reset. The completion model is *not* a hardware completion ring — production M2M queues carry no CQ; completion is observed by a host-memory marker word (the [ring cycle](../dma/ring-cycle.md) page owns that mechanism). `neuron_ring` only carries the marker buffer and the ACK passthrough that recycles spent descriptors.

This page documents five artifacts a reimplementer must reproduce: (1) the **three-level nesting** `ndma_eng[132] → ndma_queue queues[16] → ndma_ring`, with the exact field layout and the TX/RX/RXC `udma_ring_ptr` pointers plus the H2T async machinery; (2) the **two ring flavors** that share one slot — the driver-owned H2T memcpy ring (`h2t_completion_mc != NULL`) versus the claimed-but-driver-untouched "service"/RT ring — and the `nx` ring the driver never resets; (3) the **descriptor-count rounding** contract (`ndmar_ring_get_desc_count`); (4) the **ring lifecycle** — allocate → bind → program → ACK/recycle → free, plus the dummy-ring **process-exit reset**; and (5) the **per-engine mutex** locking model with its two acquire variants. Arch gating (which engine is the H2T engine, which queue is reserved for collectives) is fully delegated to the DHAL vtable — `neuron_ring.c` contains **zero** `#ifdef`s for hardware generation.

For reimplementation, the contract is:

- **The nesting and its sizing**: 132 engines per device, 16 queues per engine, one `ndma_ring` per queue carrying three `udma_ring_ptr` submission pointers (TX/RX/RXC), each a `{dma_addr_t addr, void *ptr}` physical/virtual pair, with the host-vs-device address-tagging rule.
- **The H2T-ring discriminator and flavors**: `h2t_completion_mc != NULL` *is* the "this is an H2T ring" predicate (`neuron_ring.h:429`); the claim flag `h2t_allocated` is orthogonal (a service ring is claimed but not H2T); the `nx` ring is a DHAL-gated predicate the driver must never reset.
- **The lifecycle**: `mc_alloc_align` the host backing → `ndmar_ring_set_mem_chunk` to fill the ring pointers → `udma_m2m_init_queue` (NULL completion ring) to program hardware → `udma_cdesc_ack` to recycle → `mc_free` + ctx/CQ teardown to release. Plus the process-exit reset to a 64-descriptor dummy ring.
- **The locking model**: one `mutex` per engine (`ndma_eng.lock`), a locking `ndmar_acquire_engine` and a non-locking `ndmar_acquire_engine_nl` for scan paths, and the per-H2T-ring `h2t_ring_lock` that serializes stage+trigger+poll.

| | |
|---|---|
| **Source** | `neuron_ring.c` (873 lines), `neuron_ring.h` (461 lines), aws-neuronx-dkms 2.27.4.0, GPL-2.0 |
| **Nesting** | `nd->ndma_engine[132]` → `eng->queues[16]` → `queue->ring_info` (`neuron_ring.h:185–192 / 178–183 / 156–176`) |
| **Engine count** | `NUM_DMA_ENG_PER_DEVICE = 132` (`neuron_ring.h:13`) — "for v2 2 nc with each 16" |
| **Queues per engine** | `DMA_MAX_Q_MAX = DMA_MAX_Q_V4 = 16` (`udma/udma.h:12–13`) |
| **Ring pointers** | `tx` (M2S) · `rx` (S2M) · `rxc` (S2M completion) — each `struct udma_ring_ptr {addr, ptr}` (`udma/udma.h:481–484`) |
| **H2T discriminator** | `ndmar_h2t_ring_is_h2t(ring) ⇔ ring->h2t_completion_mc != NULL` (`neuron_ring.h:429–432`) |
| **H2T ring size** | `DMA_H2T_DESC_COUNT = 4096` descriptors nominal (`neuron_ring.h:12`); allocated count via `ndmar_ring_get_desc_count` |
| **Dummy reset ring** | `NDMA_QUEUE_DUMMY_RING_DESC_COUNT = 64` (`neuron_ring.h:15`) |
| **Per-engine lock** | `struct mutex ndma_eng.lock`, init in `ndmar_preinit` (`neuron_ring.c:692–704`) |
| **Public entry points** | `ndmar_init` · `ndmar_queue_init` · `ndmar_queue_copy_start` · `ndmar_ack_completed` · `ndmar_h2t_ring_request/_release` · `ndmar_handle_process_exit` · `ndmar_close` |
| **Down boundary** | descriptor build → [K-DMA / descriptor format](../dma/descriptor-format.md) |
| **Side boundary** | hardware program/trigger/recycle → [UDMA core](udma-main.md) / [UDMA M2M](udma-m2m.md) |

---

## 1. The Nesting Model

### Purpose

`neuron_ring` exists to turn a flat array of 132 hardware DMA engines into a navigable, ownership-tracked, lockable container hierarchy, and to give the driver a small number of **driver-owned** rings (the H2T rings) for its own host↔device copies without entangling them with the user-runtime-managed model rings that occupy the same physical queue slots. Every public function on this page begins by navigating this nest: engine → queue → ring.

### The three levels

```text
struct neuron_device
  └─ ndma_engine[NUM_DMA_ENG_PER_DEVICE = 132]   (neuron_device.h:83)   one per HW DMA engine
        struct ndma_eng                          (neuron_ring.h:185-192)
          ├─ lock          struct mutex          ── ONE mutex guards the whole engine
          ├─ nd            *neuron_device         ── back-pointer (used by release_engine)
          ├─ eng_id        u32                    ── index 0..131 (self)
          ├─ udma          struct udma            ── the HW handle (K-UDMA owns it)
          ├─ used_for_h2t  bool                   ── set once this engine hosts an H2T ring
          └─ queues[DMA_MAX_Q_MAX = 16]           (udma/udma.h:12-13)   per-queue ownership wrapper
                struct ndma_queue                 (neuron_ring.h:178-183)
                  ├─ eng_id    u32                ── redundant copy of owning eng
                  ├─ qid       u32
                  ├─ owner     pid_t              ── tgid that ran ndmar_queue_init (0 = driver)
                  └─ ring_info struct ndma_ring   (neuron_ring.h:156-176)   THE descriptor-ring container
                        ├─ tx   udma_ring_ptr{addr,ptr}   ── M2S submission ring (reads source)
                        ├─ rx   udma_ring_ptr{addr,ptr}   ── S2M submission ring (writes dest)
                        ├─ rxc  udma_ring_ptr{addr,ptr}   ── S2M completion ring (optional)
                        ├─ h2t_completion / _mc          ── host MARKER buffer (NULL ⇒ not H2T)
                        └─ dma_ctx_queue / dma_compl_queue / h2t_dma_ctx[3]  ── async machinery
```

Three counts pin the geometry: **132** engines (`NUM_DMA_ENG_PER_DEVICE`, `neuron_ring.h:13`), **16** queues per engine (`DMA_MAX_Q_MAX`, `udma/udma.h:12–13`), and **3** descriptor-ring pointers per queue (`tx`, `rx`, `rxc`). The full per-device DMA-queue space is therefore 132 × 16 = 2112 queue slots, of which only a handful per NeuronCore are ever H2T rings; the rest are model/service rings whose descriptor management lives in the user runtime.

> **NOTE —** the engine count 132 is **architecture-generous**, sized for the largest generation. The number of engines actually used per NeuronCore is a DHAL value (`ndhal_address_map.dma_eng_per_nc`, `seng_dma_eng_per_nd`); `neuron_ring.c` never assumes a generation. On V2 the H2T engines are the two reserved indices 32 (`V2_D2H_IDX`) and 33 (`V2_H2D_IDX`, `v2/address_map.h:96–97`); on V3 they are 128–131 (`V3_{D2H,H2D}_{0,1}_IDX`, `v3/address_map.h:173–176`), each shared between two cores.

### Considerations

`ndma_engine[]` is embedded by value in `struct neuron_device` (`neuron_device.h:83`), so the entire 132-engine array — including every `udma` handle and all 16×132 `ndma_ring` containers with their three context slots each — is one large contiguous allocation per device. `ndmar_preinit` (`neuron_ring.c:692–704`) zeroes each `ndma_eng` and `mutex_init`s its lock exactly once at device bring-up; nothing else in the layer allocates the engine array.

---

## 2. The Ring Container and Its Two Flavors

### Purpose

A single `ndma_ring` slot is time-shared between two unrelated uses, and a third predicate (`nx`) marks slots the driver must leave alone. A reimplementer who treats "allocated" and "is H2T" as the same bit will free buffers that were never allocated, or fail to free buffers that were. The discriminators are precise and orthogonal.

### `struct ndma_ring` layout

`struct ndma_ring` (`neuron_ring.h:156–176`) is the descriptor-ring container, one per queue slot. The C source declares fields in this order (offsets are *ordinal* — the struct is not `__packed`, so absolute byte offsets depend on the compiler's alignment of the embedded `mutex`, the three `ndma_h2t_dma_context`, the `ndma_ctx_queue`, and the `ndma_h2d_compl_queue`; treat the table as field-presence + role, not a byte map):

| Field | Type | Role |
|---|---|---|
| `h2t_ring_lock` | `struct mutex` | Serializes stage+trigger+poll for *this* H2T ring; `mutex_init` in `_alloc` (`neuron_ring.c:408`) |
| `h2t_completion` | `struct udma_ring_ptr {addr, ptr}` | Host **marker** buffer — the word the CPU polls for completion |
| `h2t_completion_mc` | `struct mem_chunk *` | **The discriminator**: `!= NULL` ⇒ this slot is an H2T memcpy ring (`neuron_ring.h:429–432`) |
| `h2t_dma_ctx[3]` | `struct ndma_h2t_dma_context` | `SYNC(0)/ASYNC1(1)/ASYNC2(2)` transfer-cursor slots (`NEURON_DMA_H2T_CTX_HANDLE_CNT=3`, `share:97`) |
| `dma_ctx_queue` | `struct ndma_ctx_queue` | Async zero-copy 4-index context ring (body owned by [K-DMA](../dma/descriptor-format.md)) |
| `dma_compl_queue` | `struct ndma_h2d_compl_queue` | Async H2D completion-queue driver handle (SPMC, mmapped to userspace) |
| `h2t_nc_id` | `u32` | Owning NeuronCore (`-1` = none); set by `_alloc`, cleared by `_state_clr` |
| `h2t_allocated` | `bool` | Slot is **claimed** (H2T *or* service ring); orthogonal to the H2T discriminator |
| `qid` | `u32` | Queue index within the engine |
| `size` | `u32` | Total ring bytes = `num_desc * sizeof(union udma_desc)` |
| `has_compl` | `bool` | RXC completion ring is present (set by `ndmar_ring_set_mem_chunk` COMPLETION case) |
| `tx` | `struct udma_ring_ptr {addr, ptr}` | **M2S** descriptor ring — descriptors read the source |
| `rx` | `struct udma_ring_ptr {addr, ptr}` | **S2M** descriptor ring — descriptors write the destination |
| `rxc` | `struct udma_ring_ptr {addr, ptr}` | **S2M completion** descriptor ring (NULL on the host-marker H2T path) |
| `tx_mc` / `rx_mc` / `rxc_mc` | `struct mem_chunk *` | Backing memory chunks for the three rings |
| `dram_channel` | `u32` | DRAM channel selector — declared here, **never written in `neuron_ring.c`** (set by a caller / K-DMA) |

`struct udma_ring_ptr` (`udma/udma.h:481–484`) is the ring pointer itself: `{ dma_addr_t addr; void *ptr; }` — the physical address the hardware reads from and the kernel virtual address the driver writes to. The `ndma_queue` wrapper (`neuron_ring.h:178–183`) adds `{eng_id, qid, owner}` around the ring; `owner` is the **tgid** that called `ndmar_queue_init`, or `0` for driver-owned H2T rings.

### The three discriminators

```c
// neuron_ring.h:429-446 — the three orthogonal predicates.
static inline bool ndmar_h2t_ring_is_h2t(ring):        // :429
    return ring->h2t_completion_mc != NULL;            // has a marker buffer ⇒ driver memcpy ring

static inline bool ndmar_h2t_ring_is_allocated(ring):  // :443
    return ring->h2t_allocated;                        // claimed (H2T OR service ring)

static inline bool ndmar_h2t_ring_is_owner(ring, nc_id): // :438
    return (nc_id == ring->h2t_nc_id) && is_h2t(ring); // h2t owned by this NC

static inline void ndmar_h2t_ring_state_clr(ring):     // :448
    ring->h2t_nc_id     = -1;
    ring->h2t_allocated = false;                        // NB: does NOT clear h2t_completion_mc
```

The three ring "flavors" sit on the same `ndma_ring` slots:

| Flavor | `h2t_allocated` | `h2t_completion_mc` | Owner tag | Driver resets it? | Backing |
|---|---|---|---|---|---|
| **H2T ring** | `true` | `!= NULL` | `owner=0`, `h2t_nc_id=nc` | freed via H2T free paths | driver-allocated host `tx_mc`/`rx_mc` + marker + async ctx/CQ |
| **Service / RT ring** | `true` | `NULL` | `owner=tgid`, `h2t_nc_id=nc` | state-cleared only, never freed | descriptors managed by user runtime |
| **`nx` ring** | (claimed by HW use) | n/a | n/a | **never** (DHAL `ndmar_is_nx_ring`) | HW-reserved (collectives / NX cores) |

> **GOTCHA —** "allocated" ≠ "is H2T". `h2t_allocated` is set by `ndmar_h2t_ring_claim` for *both* H2T rings and service rings (`neuron_ring.c:455–458`), but only an H2T ring has `h2t_completion_mc != NULL`. The teardown paths branch on `is_h2t`: an H2T ring gets `ndmar_h2t_ring_free` (mc_free everything), a service ring gets only `ndmar_h2t_ring_state_clr` (`neuron_ring.c:558–563`, `833–840`). Confusing the two leaks the service-ring path or double-frees the H2T path.

> **QUIRK —** `ndmar_h2t_ring_state_clr` clears `h2t_nc_id` and `h2t_allocated` but **not** `h2t_completion_mc`. The H2T discriminator is only cleared by `ndmar_queue_init` (`:172`, which explicitly sets `ring->h2t_completion_mc = NULL`) or by `ndmar_h2t_ring_free` (`:802–805`, which `mc_free`s and NULLs it). A bare `state_clr` on an H2T ring leaves it *still answering true to `is_h2t`* — which is exactly why `ndmar_h2t_ring_free_all` calls `is_h2t` **before** `state_clr`, never after.

### The `nx` ring

The `nx` ring is a purely arch-gated predicate (`ndhal->ndhal_ndmar.ndmar_is_nx_ring(eng_id, qid)`), checked only in `ndmar_handle_process_exit` (`neuron_ring.c:260`). It identifies HW-reserved queues used by collectives / NX cores that never touch host memory; the process-exit reset path skips them so the driver never resets a queue the hardware is actively driving. On V2 the predicate is `q == (V2_DMA_QUEUE_PER_ENG-2) && (eng%16) < (V2_TPB_ENG_PER_NC + V2_TS_PER_DEVICE)`; on V3 it adds engines 7 and 8 — but these live in the DHAL, not here.

---

## 3. Descriptor-Count Rounding

### Purpose

Every UDMA ring must be a **power-of-two** number of descriptors (the wrap arithmetic in `udma_available_get`/`udma_desc_get`/`udma_cdesc_ack` masks indices with `size-1`). `ndmar_ring_get_desc_count` is the single function that turns a requested descriptor count into a legal allocated count, and it bakes in the 16-descriptor cache-line guard that the flow-control math later relies on.

### Algorithm

```c
// ndmar_ring_get_desc_count(v) — neuron_ring.c:67-81. PUBLIC (also declared neuron_ring.h:403).
function ndmar_ring_get_desc_count(v):           // u32 -> u32
    if v < 32:                                    // :69  tiny rings get a fixed floor
        return 64                                 // :70  (UDMA_MIN_Q_SIZE is 32; 64 gives headroom)
    v += UDMA_MAX_NUM_CDESC_PER_CACHE_LINE        // :72  add 16 — reserve the guard-gap descriptors
    v--                                           // :73  classic bit-smear round-up-to-pow2:
    v |= v >> 1; v |= v >> 2; v |= v >> 4         // :74-76
    v |= v >> 8; v |= v >> 16                     // :77-78
    v++                                           // :79  v is now the next power of two >= (orig+16)
    return v
```

The `+16` (`UDMA_MAX_NUM_CDESC_PER_CACHE_LINE`, `udma/udma.h:16`) before the power-of-two round is deliberate: `udma_available_get` computes free slots as `(next_cdesc_idx - (next_desc_idx + 16)) & size_mask` (`udma/udma.h:365–367`), permanently reserving a 16-descriptor gap between producer and consumer. Adding 16 *before* rounding guarantees the allocated ring has room for the requested count **plus** that guard gap even after the round. For the H2T ring this turns the nominal `DMA_H2T_DESC_COUNT = 4096` into an allocated `4096` (4096 is already a power of two; `4096+16 = 4112` rounds up to `8192`).

> **CORRECTION (K-RING-1) —** an earlier read claimed the H2T ring allocates exactly 4096 descriptors. The `+16` happens *before* the round-up, so `ndmar_ring_get_desc_count(4096)` returns **8192**, not 4096 — the `4096+16 = 4112` is rounded up to the next power of two `0x2000`. The *nominal* count fed to `udma_m2m_init_queue` in `ndmar_h2t_ring_init` is this rounded `alloced_desc` (`neuron_ring.c:440`), and `ring->size` in `_alloc` is `rounded * sizeof(union udma_desc)` (`:353`). The flow-control batch threshold (`sync_threshold = 4096/2 - 16 - 1 = 2031`, owned by the [ring cycle](../dma/ring-cycle.md) page) is computed from the *nominal* 4096, not the allocated 8192 — they are two different numbers and must not be conflated.

---

## 4. Ring Lifecycle

The H3 vocabulary below repeats for each lifecycle stage: **Purpose / Entry Point / Algorithm / Function Map / Considerations**.

### 4.1 Allocation and Sizing — `ndmar_h2t_ring_alloc`

#### Purpose

Allocate every host-memory resource a driver-owned H2T memcpy ring needs: the TX and RX submission-ring buffers, the small completion-marker buffer, and the two async helper queues. This is the only function that turns a free slot into a fully-resourced H2T ring; it does **not** program hardware (that is `_init`, §4.3).

#### Entry Point

```text
ndmar_init / ndmar_h2t_ring_request                       ── the two callers
  └─ ndmar_h2t_ring_alloc (neuron_ring.c:344)             ── acquire eng (LOCKED)
       ├─ ndhal_ndmar.ndmar_get_h2t_eng_id (DHAL)         ── which engine hosts H2T
       ├─ ndmar_ring_get_desc_count(DMA_H2T_DESC_COUNT)   ── ring_size in bytes (§3)
       ├─ mc_alloc_align rx_mc / tx_mc  (K-MEMPOOL, HOST) ── two submission buffers
       ├─ ndmar_ring_set_mem_chunk(TX) / (RX)             ── fill ring->tx / ring->rx {addr,ptr}
       ├─ mc_alloc_align h2t_completion_mc (u32*2*3)      ── the marker buffer
       ├─ ndma_h2d_compl_queue_init (K-DMA)               ── async H2D CQ handle
       └─ ndma_ctx_queue_init (K-DMA)  + mutex_init       ── async zero-copy ctx ring + ring lock
```

#### Algorithm

```c
// ndmar_h2t_ring_alloc(nd, nc_id, qid) — neuron_ring.c:344-432.  static.
function ndmar_h2t_ring_alloc(nd, nc_id, qid):
    eng_id    = ndhal_ndmar.ndmar_get_h2t_eng_id(nd, nc_id)         // :351  DHAL: V2 32/33, V3 128..131
    ndesc     = DMA_H2T_DESC_COUNT                                   // :352  = 4096 nominal
    ring_size = ndmar_ring_get_desc_count(ndesc) * sizeof(union udma_desc) // :353  rounded bytes (§3)

    eng = ndmar_acquire_engine(nd, eng_id)                          // :355  LOCKS engine mutex
    if eng == NULL: return -EINVAL
    ring = &eng->queues[qid].ring_info

    eng->used_for_h2t   = true                                      // :362  this engine now hosts H2T
    queue->{eng_id,qid} = {eng_id, qid}; queue->owner = 0           // :363-365  driver-owned
    ring->{qid, h2t_nc_id, size, has_compl} = {qid, nc_id, ring_size, false}  // :366-369

    if mc_alloc_align(HOST, ring_size, ...) -> rx_mc fails: goto error  // :371  RX submission buffer
    if mc_alloc_align(HOST, ring_size, ...) -> tx_mc fails: goto error  // :377  TX submission buffer
    ndmar_ring_set_mem_chunk(eng, qid, tx_mc, port=0, TX)          // :383  fill ring->tx
    ndmar_ring_set_mem_chunk(eng, qid, rx_mc, port=0, RX)          // :384  fill ring->rx

    // marker buffer: sizeof(u32)*2*NEURON_DMA_H2T_CTX_HANDLE_CNT = 4*2*3 = 24 bytes
    if mc_alloc_align(HOST, 24, ...) -> h2t_completion_mc fails: goto error  // :386
    ring->h2t_completion_mc  = h2t_completion_mc                    // :392  ⇒ ring is NOW "is_h2t"
    ring->h2t_completion.ptr  = h2t_completion_mc->va
    ring->h2t_completion.addr = virt_to_phys(ptr) | pci_host_base  // :394  host PA tagging

    if ndma_h2d_compl_queue_init(nd, &ring->dma_compl_queue) fails: goto error      // :396  K-DMA
    if ndma_ctx_queue_init(&ring->dma_ctx_queue) fails: goto error_ctx_queue        // :402  K-DMA
    mutex_init(&ring->h2t_ring_lock)                               // :408

    ndmar_release_engine(eng); return 0                            // :410-412  success

error_ctx_queue:                                                  // :414
    ndma_h2d_compl_queue_destroy(&ring->dma_compl_queue)
error:                                                            // :416  ordered unwind
    ring->h2t_nc_id = -1
    ring->tx_mc = ring->rx_mc = ring->h2t_completion_mc = NULL    // :417-420
    ndmar_release_engine(eng)                                     // :422  unlock BEFORE mc_free (no lock held)
    if rx_mc:  mc_free(&rx_mc)                                    // :424-429
    if tx_mc:  mc_free(&tx_mc)
    if h2t_completion_mc: mc_free(&h2t_completion_mc)
    return ret
```

The address-tagging rule in `ndmar_ring_set_mem_chunk` (`neuron_ring.c:91–138`) is the one piece of physical-address logic the ring layer owns:

```c
// ndmar_ring_set_mem_chunk — per queue_type (TX/RX/COMPLETION), neuron_ring.c:91-138.
ring->{tx,rx,rxc}.ptr = mc->va                                    // kernel VA
if mc->mem_location == MEM_LOC_HOST:
    ring->{...}.addr = virt_to_phys(mc->va) | pci_host_base       // :102/114/127  HOST: PA | host-base
else:                                                             // device memory
    ring->{...}.addr = mc->pa
    if port != 0: ring->{...}.addr |= port_1_base                 // :106/118/131  AXI port-1 select
// COMPLETION also sets ring->has_compl = true                    // :123
```

#### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndmar_h2t_ring_alloc` | `neuron_ring.c:344` | Allocate all host resources for an H2T ring | CERTAIN |
| `ndmar_ring_set_mem_chunk` | `neuron_ring.c:91` | Bind a `mem_chunk` into `ring->{tx,rx,rxc}` with PA tagging | CERTAIN |
| `ndmar_ring_get_desc_count` | `neuron_ring.c:67` | Round descriptor count to pow2 + 16-guard (§3) | CERTAIN |
| `mc_alloc_align` | K-MEMPOOL | Allocate host backing chunk | HIGH (boundary) |
| `ndma_h2d_compl_queue_init` | K-DMA (`neuron_ring.h:55`) | Init async H2D CQ handle | HIGH (boundary) |
| `ndma_ctx_queue_init` | K-DMA (`neuron_ring.h:130`) | Init async zero-copy ctx ring | HIGH (boundary) |

#### Considerations

The error unwind is **two-label and order-sensitive**: a failure after `ndma_ctx_queue_init` would have to destroy the already-initialized compl queue (`error_ctx_queue`), whereas a failure before it jumps straight to `error`. Critically, `ndmar_release_engine` is called **before** the `mc_free` calls (`neuron_ring.c:422` then `424–429`) so the freeing happens outside the engine mutex — `mc_free` may sleep and must not run under the lock. The marker buffer is `sizeof(u32) * 2 * 3 = 24` bytes: two `u32` slots (src marker, dst marker) per context-handle type, three handle types (`SYNC`/`ASYNC1`/`ASYNC2`).

---

### 4.2 Generic Queue Bind — `ndmar_queue_init`

#### Purpose

Bind a *caller-supplied* set of `mem_chunk`-backed TX/RX/RXC descriptor rings into a UDMA queue and program the hardware. This is the path the IOCTL layer uses to set up user-runtime **model** queues (the runtime authors the descriptors; the kernel only programs the ring base pointers). It is also reused to *reset* a queue to a dummy ring on process exit (§4.5).

#### Entry Point

```text
[IOCTL DMA_QUEUE_INIT — K-CDEV] / ndmar_handle_process_exit
  └─ ndmar_queue_init (neuron_ring.c:140)                  ── acquire eng (LOCKED)
       ├─ guard: qid >= num_queues ⇒ -EINVAL              ── DHAL ndhal_udma.num_queues
       ├─ guard: is_h2t(ring) && tx_mc != dummy ⇒ -EALREADY  ── H2T collision
       ├─ ndmar_ring_set_mem_chunk TX / RX / (RXC)
       └─ udma_m2m_init_queue (K-UDMA)                     ── program HW ring base pointers
```

#### Algorithm

```c
// ndmar_queue_init(nd, eng_id, qid, tx_cnt, rx_cnt, tx_mc, rx_mc, rxc_mc, port, allocatable)
// neuron_ring.c:140-209.  PUBLIC.
function ndmar_queue_init(...):
    eng = ndmar_acquire_engine(nd, eng_id)                  // :149  LOCKS; NULL ⇒ -EINVAL
    if qid >= ndhal_udma.num_queues: { ret=-EINVAL; goto done }  // :153  DHAL queue bound

    ring = &eng->queues[qid].ring_info
    // collision guard: refuse to clobber an H2T ring unless this is the dummy-reset path
    if ndmar_h2t_ring_is_h2t(ring) && (tx_mc != nd->ndma_q_dummy_mc):  // :162
        ret = -EALREADY; goto done

    queue->{eng_id,qid} = {eng_id,qid}; queue->owner = task_tgid_nr(current)  // :168-170
    ring->qid = qid
    ring->h2t_completion_mc = NULL                          // :172  ⇒ ring is no longer "is_h2t"

    if tx_mc:                                                // :177
        // SIZE CHECK COMMENTED OUT in source (:178-183) — see GOTCHA
        ndmar_ring_set_mem_chunk(eng, qid, tx_mc, port, TX)
    if rx_mc:                                                // :186
        // SIZE CHECK COMMENTED OUT in source (:187-192)
        ndmar_ring_set_mem_chunk(eng, qid, rx_mc, port, RX)
    if rxc_mc:                                               // :195
        if rx_cnt * sizeof(union udma_desc) > rxc_mc->size: { ret=-EINVAL; goto done }  // :196 ONLY rxc checked
        ndmar_ring_set_mem_chunk(eng, qid, rxc_mc, port, COMPLETION)

    ret = udma_m2m_init_queue(&eng->udma, qid, eng_id, tx_cnt, rx_cnt, allocatable,  // :203  K-UDMA
                              tx_mc ? &ring->tx : NULL,
                              rx_mc ? &ring->rx : NULL,
                              rxc_mc ? &ring->rxc : NULL)
done:
    ndmar_release_engine(eng)                               // :207  always unlocks
    return ret
```

#### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndmar_queue_init` | `neuron_ring.c:140` | Bind caller rings + program HW; also the dummy-reset path | CERTAIN |
| `udma_m2m_init_queue` | K-UDMA (`udma/udma.h:515`) | Program TX/RX/RXC ring base pointers into hardware | HIGH (boundary) |
| `ndmar_h2t_ring_is_h2t` | `neuron_ring.h:429` | H2T collision guard predicate | CERTAIN |

#### Considerations

> **GOTCHA —** the TX and RX **descriptor-count-vs-buffer-size validation is commented out** in the shipped source (`neuron_ring.c:178–183` and `187–192`). Only the RXC completion ring is bounds-checked (`:196`). A caller that passes `tx_desc_count` larger than `tx_mc->size / sizeof(union udma_desc)` can program the hardware with a ring length that overruns its backing buffer — the kernel will not reject it. A reimplementer who *re-enables* these checks restores the intended safety; one who copies the source verbatim inherits a latent over-run. This is the validation a hostile or buggy userspace IOCTL could exploit, so it is worth flagging at the IOCTL boundary too.

> **NOTE —** the collision guard at `:162` is what keeps the IOCTL path from stomping a driver-owned H2T ring: `is_h2t(ring) && tx_mc != ndma_q_dummy_mc ⇒ -EALREADY`. The *only* legitimate caller that may overwrite an H2T-flagged slot is the process-exit reset, which passes the shared `ndma_q_dummy_mc` and so slips past the guard.

---

### 4.3 Hardware Program — `ndmar_h2t_ring_init`

#### Purpose

Program an already-allocated H2T ring's TX and RX submission rings into the UDMA hardware, with **no completion ring** — the H2T path uses the host-memory marker model, not a hardware CQ.

#### Algorithm

```c
// ndmar_h2t_ring_init(eng, qid) — neuron_ring.c:434-447.  PUBLIC (declared neuron_ring.h:401).
function ndmar_h2t_ring_init(eng, qid):
    alloced_desc = ndmar_ring_get_desc_count(DMA_H2T_DESC_COUNT)   // :440  rounded count (§3)
    ring = &eng->queues[qid].ring_info
    return udma_m2m_init_queue(&eng->udma, qid, eng->eng_id,        // :444  K-UDMA
                               alloced_desc, alloced_desc, /*allocatable=*/true,
                               &ring->tx, &ring->rx, /*s2m_compl=*/NULL)  // NULL ⇒ host-marker completion
```

The `NULL` completion-ring argument is the structural commitment to the host-marker model: `udma_m2m_init_queue` refuses a completion ring on the production M2M path (it is documented at the [ring cycle](../dma/ring-cycle.md) boundary). `allocatable=true` means descriptors can be added after queue init, which the staging path relies on.

> **QUIRK —** `ndmar_h2t_ring_init` takes a *locked* `struct ndma_eng *` (the caller holds the engine mutex), whereas `ndmar_h2t_ring_alloc` takes `(nd, nc_id, qid)` and locks internally. In `ndmar_init_nc` the two are called separately: `_alloc` locks-allocs-unlocks, then the caller re-acquires the lock and calls `_init` (`neuron_ring.c:738–749`). This split exists so the sleeping `mc_alloc_align` in `_alloc` never runs while the lock is held across the hardware-programming step.

---

### 4.4 ACK / Recycle — `ndmar_ack_completed`

#### Purpose

After the completion marker flips (poll owned by [K-DMA](../dma/ring-cycle.md)), the spent TX and RX descriptors must be returned to the ring so the producer can reuse those slots. `ndmar_ack_completed` is the public IOCTL passthrough that advances the UDMA completion counter on both rings.

#### Algorithm

```c
// ndmar_ack_completed(nd, eng_id, qid, count) — neuron_ring.c:272-307.  PUBLIC.
function ndmar_ack_completed(nd, eng_id, qid, count):
    eng = ndmar_acquire_engine(nd, eng_id)                 // :278  LOCKS
    if eng == NULL: return -EINVAL

    if udma_q_handle_get(&eng->udma, qid, UDMA_TX, &txq) fails || !txq: goto done  // :284
    if udma_q_handle_get(&eng->udma, qid, UDMA_RX, &rxq) fails || !rxq: goto done  // :289

    if udma_cdesc_ack(rxq, count) fails: goto done          // :295  recycle RX descriptors FIRST
    if udma_cdesc_ack(txq, count) fails: goto done          // :299  then TX
done:
    ndmar_release_engine(eng); return ret                   // :305
```

`udma_cdesc_ack` (`udma/udma.h:452`) CAS-advances the queue's `next_cdesc_idx` by `count` (mod `size_mask`), which is exactly what `udma_available_get` reads to report free slots. The RX-before-TX ordering mirrors the RX-first doorbell ordering on the trigger side.

> **NOTE —** `ndmar_ack_completed` does **no** `qid` bounds check of its own — it relies on `udma_q_handle_get` to reject an out-of-range queue. `ndmar_queue_init` checks `qid < num_queues` explicitly (`:153`); the ack and copy-start paths do not (`ndmar_get_queue`/`ndmar_get_ring` at `:57–65` also have no bounds check). A reimplementer must keep `udma_q_handle_get` as the backstop or add the check.

---

### 4.5 Copy-Start and Process-Exit Reset

#### Purpose

`ndmar_queue_copy_start` is the trigger passthrough (the actual doorbell is in K-UDMA); `ndmar_handle_process_exit` is the crash/exit cleanup that resets every non-H2T, non-`nx` queue owned by an exiting process back to a harmless dummy ring.

#### Algorithm — copy-start

```c
// ndmar_queue_copy_start(nd, eng_id, qid, tx_cnt, rx_cnt) — neuron_ring.c:309-336.  PUBLIC (IOCTL nr 35).
function ndmar_queue_copy_start(...):
    eng = ndmar_acquire_engine(nd, eng_id)                 // :315  LOCKS
    ret = udma_m2m_copy_start(&eng->udma, qid, tx_cnt, rx_cnt)  // :321  K-UDMA: rings RX then TX doorbell
    // legacy "V1" model-start tracking: if the RX buffer carried PE-IRAM instructions,
    // this copy_start is the first model start for that NeuronCore.
    rx_mc = eng->queues[qid].ring_info.rx_mc               // :328
    if !ret && rx_mc->model_start_tracker.has_pe_iram_inst:  // :329
        nd->nc_model_started_count[rx_mc->model_start_tracker.nc_id]++
    ndmar_release_engine(eng); return ret                  // :333
```

#### Algorithm — process-exit reset

```c
// ndmar_handle_process_exit(nd, pid) — neuron_ring.c:211-270.  PUBLIC.
function ndmar_handle_process_exit(nd, pid):
    if dma_teardown_on_exit == 0: return                   // :216  module-param gate (default 1)
    mc         = nd->ndma_q_dummy_mc                        // :220  the shared dummy ring backing
    desc_count = NDMA_QUEUE_DUMMY_RING_DESC_COUNT           // :221  = 64

    for eng_id in 0 .. seng_dma_eng_per_nd-1:              // :222  DHAL count
      for qid in 0 .. num_queues-1:                        // :223
        eng = ndmar_acquire_engine_nl(nd, eng_id)          // :224  NO LOCK (scan path)
        if eng == NULL: continue
        ring = &eng->queues[qid].ring_info
        if queue->owner != pid: continue                   // :235  only this process's queues

        if ndmar_is_h2t_def_q(nd, eng_id, qid):            // :244  DHAL: never reset the default H2T queue
            pr_err_once("unexpected pid on default h2t ring"); continue
        if ndmar_h2t_ring_is_h2t(ring):                    // :250  H2T rings freed on a different path
            pr_err_once("h2t ring should not be bound to process"); continue

        ndmar_h2t_ring_state_clr(ring); queue->owner = 0   // :255-256  release ownership
        if ndmar_is_nx_ring(eng_id, qid): continue         // :260  DHAL: never reset NX/collective rings

        // reset the queue to a 64-desc dummy ring (dummy_mc slips past the H2T collision guard)
        ndmar_queue_init(nd, eng_id, qid, 64, 64, mc, mc, NULL, port=0, allocatable=false)  // :264
```

#### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndmar_queue_copy_start` | `neuron_ring.c:309` | Trigger passthrough → `udma_m2m_copy_start`; V1 model-start tracking | CERTAIN |
| `ndmar_handle_process_exit` | `neuron_ring.c:211` | Reset exiting-pid queues to a 64-desc dummy ring | CERTAIN |
| `ndmar_queue_release` | `neuron_ring.c:338` | **Stub** — trace + `return 0`; no real teardown | CERTAIN |
| `udma_m2m_copy_start` | K-UDMA (`udma/udma.h`) | Ring RX then TX doorbell (`drtp_inc`) | HIGH (boundary) |

#### Considerations

> **GOTCHA —** `ndmar_queue_release` (`neuron_ring.c:338–342`) is a **no-op stub**: it emits a trace event and returns 0. It frees *nothing*. Real teardown happens only via the H2T free paths (`ndmar_h2t_ring_free*`) or the process-exit dummy reset. A reimplementer who assumes "release frees the queue's resources" is wrong — the ring's `mem_chunk`s are reclaimed by the mempool's lifespan tracking and by `ndmar_close`, not by `release`.

> **QUIRK —** the process-exit scan uses `ndmar_acquire_engine_nl` — the **non-locking** acquire (`neuron_ring.c:45–50`, comment: *"Use for scanning."*) — so the whole nested scan runs without holding any engine mutex, but the per-queue `ndmar_queue_init` call inside it *does* acquire and release the lock. The scan is safe because it only resets queues whose owner is the exiting (now-dead) process; nothing else races for them.

---

### 4.6 Dynamic Request, Release, and Teardown

#### Purpose

Beyond the default H2T ring allocated at NeuronCore bring-up, the runtime can dynamically **request** an extra H2T ring (or a service ring) and later **release** it; device teardown frees them all.

#### Algorithm — claim and request

```c
// ndmar_h2t_ring_claim(nd, eng_id, ring) — neuron_ring.c:449-462.  static.  Atomic free-slot claim.
function ndmar_h2t_ring_claim(nd, eng_id, ring):
    eng = ndmar_acquire_engine(nd, eng_id)                 // :452  LOCKS
    claimed = false
    if !ring->h2t_allocated:                               // :455
        ring->h2t_nc_id = -1; ring->h2t_allocated = true   // :456-457  claim the slot
        claimed = true
    ndmar_release_engine(eng); return claimed              // :460-461

// ndmar_h2t_ring_request(nd, nc_id, h2t, *rqid) — neuron_ring.c:473-526.  PUBLIC.
function ndmar_h2t_ring_request(nd, nc_id, h2t, *rqid):
    eng_id = ndhal_ndmar.ndmar_get_h2t_eng_id(nd, nc_id)   // :476
    eng = ndmar_acquire_engine_nl(nd, eng_id)              // :482  NON-locking (claim re-locks per slot)
    for qid in 0 .. num_queues-1:                          // :486
        if ndmar_is_h2t_def_q(nd, eng_id, qid): continue   // :487  skip the driver's default H2T queue
        ring = &eng->queues[qid].ring_info
        if ndmar_h2t_ring_claim(nd, eng_id, ring):         // :493  first free slot wins
            if h2t:                                         // :498  driver-memcpy ring
                if ndmar_h2t_ring_alloc(nd, nc_id, qid) fails: { ring->h2t_allocated=false; goto done }  // :500
                if ndmar_h2t_ring_init(eng, qid) fails:                                                  // :506
                    ndmar_h2t_ring_free(eng, ring); ring->h2t_allocated=false; goto done
            else:                                           // :513  service / RT ring
                queue->owner = task_tgid_nr(current); ring->h2t_nc_id = nc_id; ret = 0  // :515-517
            *rqid = qid; break                              // :519-520
done:
    return ret                                             // :524
```

#### Algorithm — release and free

```c
// ndmar_h2t_ring_release(nd, nc_id, qid) — neuron_ring.c:528-568.  PUBLIC.
function ndmar_h2t_ring_release(nd, nc_id, qid):
    if qid >= num_queues: return -EINVAL                   // :536
    if ndmar_is_h2t_def_q(nd, eng_id, qid): return 0       // :540  default H2T queue: no-op (driver keeps it)
    eng = ndmar_acquire_engine(nd, eng_id)                 // :544  LOCKS
    ring = &eng->queues[qid].ring_info
    if !is_allocated(ring) || ring->h2t_nc_id != nc_id:    // :552  wrong owner / not allocated
        ret = -ENXIO; goto done
    if ndmar_h2t_ring_is_h2t(ring):                        // :558
        ndmar_h2t_ring_free(eng, ring)                     //       free all H2T resources
    else:
        ndmar_h2t_ring_state_clr(ring); queue->owner = 0   // :561-562  service ring: clear state only
done:
    ndmar_release_engine(eng); return ret                  // :566

// ndmar_h2t_ring_free(eng, ring) — neuron_ring.c:785-811.  static.  Free every H2T resource.
function ndmar_h2t_ring_free(eng, ring):
    mc_free(&ring->tx_mc);  mc_free(&ring->rx_mc)          // :787-795  (each NULLed)
    mc_free(&ring->rxc_mc); mc_free(&ring->h2t_completion_mc)  // :797-805
    ndma_ctx_queue_free(eng, ring, &ring->dma_ctx_queue)  // :807  K-DMA
    ndma_h2d_compl_queue_destroy(&ring->dma_compl_queue)  // :808  K-DMA
    ndmar_h2t_ring_state_clr(ring)                         // :810
```

#### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndmar_h2t_ring_request` | `neuron_ring.c:473` | Dynamically claim+alloc+init a free H2T/service ring | CERTAIN |
| `ndmar_h2t_ring_release` | `neuron_ring.c:528` | Release one requested ring (free if H2T, clear if service) | CERTAIN |
| `ndmar_h2t_ring_claim` | `neuron_ring.c:449` | Atomic claim of a free slot under the engine lock | CERTAIN |
| `ndmar_h2t_ring_free` | `neuron_ring.c:785` | Free all H2T host resources + async queues | CERTAIN |
| `ndmar_h2t_ring_free_all` | `neuron_ring.c:816` | Free/clear every allocated ring owned by an NC | CERTAIN |
| `ndmar_close` / `ndmar_close_ncs` / `ndmar_close_nc` | `neuron_ring.c:869 / 856 / 847` | Device/NC teardown wrappers | CERTAIN |

#### Considerations

> **GOTCHA —** the driver **only tracks H2T rings**, never model rings (kerneldoc at `neuron_ring.c:464–471`). Because `ndmar_h2t_ring_request` walks queue slots looking for the first one with `h2t_allocated == false`, any H2T ring a process needs must be requested **before** the runtime allocates model rings into the same engine's queues — otherwise those slots already read as claimed and the request walks past them or fails. This ordering constraint is invisible in the type signatures; it is a hard sequencing requirement on the caller.

---

## 5. Engine Init, Bring-Up, and Locking

### Purpose

Before any ring exists, each hardware engine must be `memset`+lock-initialized once (`ndmar_preinit`), then UDMA-initialized per NeuronCore (`ndmar_eng_init` → DHAL `ndma_init`), and the default H2T ring allocated and programmed (`ndmar_init_nc`). The whole layer is guarded by a single per-engine mutex.

### Algorithm — per-NC bring-up

```c
// ndmar_init(nd) -> ndmar_init_ncs(nd, -1)  — neuron_ring.c:780-783.  -1 == NEURON_NC_MAP_DEVICE (all cores).
function ndmar_init_nc(nd, nc_idx, init_h2t_eng):           // :706-758  static
    if nd->dmar_init_done[nc_idx]: return 0                 // :710  idempotent
    start = nc_idx * dma_eng_per_nc                          // :716  DHAL geometry
    end   = (nc_idx+1) * dma_eng_per_nc - 1                  // :717
    for eng_id in start..end:                                // :718  init every SENG engine in this NC
        ndmar_eng_init(nd, eng_id)                           // :719  -> DHAL ndma_init(bar0, &udma, eng_id)
    h2t_eng = ndmar_get_h2t_eng_id(nd, nc_idx)               // :727  DHAL
    if (h2t_eng outside [start,end]) && init_h2t_eng:        // :728  H2T engine may be shared/external
        ndmar_eng_init(nd, h2t_eng)                          // :729
    qid = ndmar_get_h2t_def_qid(nc_idx)                      // :736  DHAL: V2=0, V3=nc%2
    ndmar_h2t_ring_alloc(nd, nc_idx, qid)                    // :738  alloc (locks internally)
    eng = ndmar_acquire_engine(nd, h2t_eng)                  // :744  re-acquire for the program step
    ndmar_h2t_ring_init(eng, qid); ndmar_release_engine(eng) // :748-749
    nd->dmar_init_done[nc_idx] = true                        // :755
```

### Algorithm — locking primitives

```c
// neuron_ring.c:36-55 — the engine-acquire family.
struct ndma_eng *ndmar_acquire_engine(nd, eng_id):          // :36  PUBLIC, LOCKING
    if eng_id >= NUM_DMA_ENG_PER_DEVICE: return NULL         // :38  bounds check (132)
    mutex_lock(&nd->ndma_engine[eng_id].lock)                // :40
    return &nd->ndma_engine[eng_id]

static struct ndma_eng *ndmar_acquire_engine_nl(nd, eng_id): // :45  NON-locking ("Use for scanning.")
    if eng_id >= NUM_DMA_ENG_PER_DEVICE: return NULL
    return &nd->ndma_engine[eng_id]

void ndmar_release_engine(eng):                              // :52  PUBLIC
    mutex_unlock(&eng->nd->ndma_engine[eng->eng_id].lock)    // :54  unlocks via nd lookup, not &eng->lock
```

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ndmar_preinit` | `neuron_ring.c:692` | One-time: memset + `mutex_init` all 132 engines | CERTAIN |
| `ndmar_eng_init` | `neuron_ring.c:674` | Init one engine via DHAL `ndma_init(bar0, &udma, eng_id)` | CERTAIN |
| `ndmar_init` / `ndmar_init_ncs` / `ndmar_init_nc` | `neuron_ring.c:780 / 760 / 706` | Device/NC bring-up + default H2T ring | CERTAIN |
| `ndmar_acquire_engine` / `_nl` / `ndmar_release_engine` | `neuron_ring.c:36 / 45 / 52` | Per-engine lock acquire (locking / scan) + release | CERTAIN |
| `ndmar_eng_set_state` / `ndmar_eng_get_state` | `neuron_ring.c:570 / 584` | UDMA state set/get passthrough | CERTAIN |
| `ndmar_queue_get_descriptor_info` | `neuron_ring.c:612` | Read HW ring base PA + length back from registers | CERTAIN (carries a bug — §6) |

### Considerations

The locking contract is: **one mutex per engine** (`ndma_eng.lock`, `neuron_ring.h:186`), initialized once in `ndmar_preinit`. Most public entry points pair `acquire`/`release`. Two paths use the non-locking variant: `ndmar_handle_process_exit` (scans without a lock, §4.5) and `ndmar_h2t_ring_request` (walks slots without a lock, then `ndmar_h2t_ring_claim` re-acquires *with* the lock per slot). The per-H2T-ring `h2t_ring_lock` is a *second*, finer lock that serializes the stage+trigger+poll of a single H2T ring's async machinery; it is taken by the K-DMA memcpy paths, not by this layer.

> **NOTE —** `ndmar_release_engine` unlocks `eng->nd->ndma_engine[eng->eng_id].lock` — it re-derives the lock from the back-pointer rather than using `&eng->lock` directly (`neuron_ring.c:54`). It is functionally identical because `eng` *is* `&nd->ndma_engine[eng->eng_id]`, but it is fragile: it depends on the back-pointer and `eng_id` being intact. A simpler reimplementation should just `mutex_unlock(&eng->lock)`.

---

## 6. Known Defects (Reimplementer Warnings)

These are confirmed in the shipped GPL source and a reimplementer should *not* reproduce them.

> **GOTCHA (K-RING-2) — mutex leak on register-read failure.** In `ndmar_queue_get_descriptor_info` (`neuron_ring.c:612–672`), every `reg_read32` error path is written as:
> ```c
> if (reg_read32(&hw_q->q_regs->rings.drbp_high, &high)) {
>     return -EIO;      // :633  — returns WITH the engine mutex still held
>     goto done;        // :634  — dead code, never reached
> }
> ```
> The bare `return -EIO` (lines 633, 637, 641, 651, 655, 659) executes **before** the unreachable `goto done`, so `ndmar_release_engine(eng)` at `:670` is bypassed and the engine mutex is **never unlocked** — a deadlock for every subsequent acquire of that engine. The `goto done` after each `return` is dead code. Only the two `udma_q_handle_get` failure paths (`:629`, `:647`) correctly `goto done`. The intent was clearly `ret = -EIO; goto done;` on all six. HIGH confidence; the bug is on a rare diagnostic/introspection path (descriptor-info IOCTL), not the hot copy path, which is why it survives. **Confidence: CERTAIN that the source reads this way; HIGH that it is a genuine latent deadlock.**

> **NOTE —** the commented-out TX/RX size checks in `ndmar_queue_init` (§4.2 GOTCHA) and the absent `qid` bounds checks in the ack/copy-start paths (§4.4 NOTE) are the other two soft spots; both are documented in place above.

---

## 7. Boundaries

`neuron_ring` is a thin orchestration layer; almost every real operation crosses into a neighbor.

### Down — descriptor building (K-DMA)

`neuron_ring` never builds a `union udma_desc`. The async helper-queue handles it carries — `dma_ctx_queue` (`ndma_ctx_queue`), `dma_compl_queue` (`ndma_h2d_compl_queue`), and `h2t_dma_ctx[3]` — are *declared* in `neuron_ring.h` but **operated on** in `neuron_dma.c`: `ndma_h2d_compl_queue_init/_destroy`, `ndma_ctx_queue_init/_free`, and `ndmar_queue_get_state` are all defined there (declared at `neuron_ring.h:55/56/130/131/391`). The element type `ndma_h2t_zcdma_context` and the marker-write / poll machinery live entirely in K-DMA. The [16-byte descriptor wire format](../dma/descriptor-format.md) is its own page. `neuron_ring`'s `ndmar_queue_copy_start` and `ndmar_ack_completed` are *called by* K-DMA's memcpy paths.

### Side — hardware program / trigger / recycle (UDMA HAL)

The actual hardware operations are all `udma_*` calls into the [UDMA core](udma-main.md) and [UDMA M2M builder](udma-m2m.md): `udma_m2m_init_queue` (program ring base pointers), `udma_m2m_copy_start` (RX-then-TX doorbell), `udma_cdesc_ack` (CAS-recycle `next_cdesc_idx`), `udma_q_handle_get`, `udma_state_set/get`, and the `struct udma` / `struct udma_q` / `struct udma_ring_ptr` types plus the `q_regs->rings.{drbp_high,drbp_low,drl}` register block read in `ndmar_queue_get_descriptor_info`.

### Up — arch gating (DHAL) and callers (IOCTL / PID)

All generation-specific decisions are DHAL vtable calls: `ndhal_ndmar.{ndmar_get_h2t_eng_id, ndmar_get_h2t_def_qid, ndmar_is_h2t_def_q, nr_init_h2t_eng, ndmar_is_nx_ring}`, `ndhal_ndma.ndma_init`, `ndhal_udma.num_queues`, and `ndhal_address_map.{pci_host_base, port_1_base, nc_per_device, dev_nc_map, dma_eng_per_nc, seng_dma_eng_per_nd}`. The public entry points are reached from the [DMA op layer](dma-op-layer.md) and IOCTL handlers (`ndmar_queue_init/_copy_start/_ack_completed`, `ndmar_h2t_ring_request/_release`) and the process-release path (`ndmar_handle_process_exit`).

---

## Related Components

| Name | Relationship |
|---|---|
| `struct ndma_eng` / `ndma_queue` / `ndma_ring` | The three nesting levels this page documents (`neuron_ring.h:185 / 178 / 156`) |
| `struct udma` / `udma_q` | The HW handle embedded in `ndma_eng`; owned by the UDMA core |
| `struct udma_ring_ptr` | The `{addr, ptr}` ring pointer type filled by `ndmar_ring_set_mem_chunk` (`udma/udma.h:481`) |
| `ndma_ctx_queue` / `ndma_h2d_compl_queue` | Async helper queues carried in `ndma_ring`; body in K-DMA |
| DHAL `ndhal_ndmar` vtable | Resolves H2T engine/queue and `nx`-ring predicates per generation |

## Cross-References

- [DMA Op Layer and the Completion-Marker Model](dma-op-layer.md) — the layer above; calls `ndmar_queue_copy_start` / `ndmar_ack_completed`, owns the host-marker poll
- [UDMA Core (Annapurna/Alpine Fork)](udma-main.md) — `udma_m2m_init_queue`, `udma_desc_action_add` (doorbell), `udma_cdesc_ack`, `struct udma_q` and the `drtp_inc` register
- [UDMA Memory-to-Memory Builder](udma-m2m.md) — `udma_m2m_copy_start` (RX-then-TX trigger) and the descriptor-pair builder this ring carries
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the wire format of the entries these TX/RX rings hold; do not re-derive it here
- [The Ring / Trigger / Doorbell / Completion Cycle](../dma/ring-cycle.md) — the dynamic stage→trigger→poll→ack cycle that consumes this ring abstraction; owns `sync_threshold`, the marker `0xabcdef01`, and the wrap/phase math
