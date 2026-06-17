# UDMA Core (Annapurna/Alpine Fork)

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0** (`usr/src/aws-neuronx-2.27.4.0/`). The two owned files are `udma/udma_main.c` (713 lines) and `udma/udma.h` (553 lines), read verbatim. Register layouts (offsets such as the `+0x38` doorbell) are cited at `udma/udma_regs.h`, which is owned by a sibling reg-map cell and pulled here as boundary evidence only. Every offset below is a struct field, not a recovered binary offset; the driver ships as a stripped `.ko` but the GPL C is authoritative.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. The doorbell register (`+0x38`), the `mb()`-then-write order, and the `next_cdesc_idx@+0x34` recycle CAS are each independently corroborated against the `libnrt.so` disassembly on the [ring-cycle](../dma/ring-cycle.md) page. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`udma_main.c` + `udma.h` is the in-kernel **`al_hal_udma_main` equivalent** — the AnnapurnaLabs/Alpine micro-DMA hardware-abstraction core, vendored into the Neuron driver tree. It models one **bi-directional DMA engine** (`struct udma`): a single hardware unit that carries up to 16 **M2S** (memory-to-stream, the TX direction that *reads the source*) submission queues and up to 16 **S2M** (stream-to-memory, the RX direction that *writes the destination*) submission queues. A *copy* is always a TX/RX pair on the same `qid`. The reader who knows any descriptor-ring NIC or NVMe driver already owns the frame: a per-queue **submission ring** with a producer index the driver advances and a tail-pointer **doorbell** the CPU rings to hand descriptors to the silicon, plus a **completion** signal the CPU waits on. UDMA is exactly that frame, programmed entirely through MMIO register writes, with one Alpine twist that shapes the whole layer — the completion ring is *optional*, and on Neuron's production path it is **switched off** (`cdesc_base == NULL`), so completion is observed by a host-memory marker one cell up rather than by a hardware completion queue.

This page documents the layer's three responsibilities. **Lifecycle**: `udma_init` brings up one engine (map register sub-blocks, set AXI/FIFO/stream defaults, unmask error interrupts, cache the slow CSR reset values), and `udma_q_init` brings up one queue (validate geometry, fill the software `udma_q` shadow, reset hardware pointers, program ring base addresses, enable the queue). **Producer primitives**: the inline `udma_desc_get`/`udma_ring_id_get`/`udma_available_get` hand out submission-ring slots and phase bits, and `udma_desc_action_add` is the **doorbell** — a single `mb()`-guarded 32-bit write of the descriptor count into one per-queue register, `rings.drtp_inc` at MMIO offset `+0x38`. **Consumer primitive**: the inline `udma_cdesc_ack` is the lock-free completion-counter CAS that recycles spent slots, advancing `next_cdesc_idx` at `+0x34`. Around all of this sits the engine state machine (`udma_state_set`/`udma_state_get`) and a hard single-revision gate.

That gate is the most consequential design fact for a reimplementer: this HAL only ever runs **one** hardware revision. `udma_handle_init_aux` hard-codes `rev_id = UDMA_REV_ID_4 = 4` and `num_of_queues_max = DMA_MAX_Q_V4 = 16` (`udma_main.c:284–285`); every register access goes through the `*_v4` register structs (`unit_regs_v4`, `udma_main.c:299–307`). There is no runtime revision dispatch — the comment is explicit: *"V1 hardware uses DMA rev4, no need to support other version"* (`udma_main.c:283`). A reimplementer can drop all multi-rev abstraction the original al_hal carried; only the rev-4 path is built and reachable.

For reimplementation, the contract is:

- **The engine handle and its bring-up**: one `struct udma` per hardware engine, with two `udma_q[16]` arrays (M2S/S2M), register sub-block pointers mapped from a single `unit_regs_v4` base, `rev_id=4`, `num_of_queues_max=16`. Queues are **enabled at handle-init and never disabled** in the normal path; `udma_q_pause` (the only disable) just calls `udma_q_enable(q, 0)`.
- **The cached-CSR shortcut**: CSR reads are slow, so `udma_cache_defaults` seeds the software shadow `q->cfg`/`q->rlimit_mask` from datasheet reset values, and enable/disable RMW the shadow and write it back — never reading the CFG register on the hot path.
- **The producer ring math**: a power-of-two submission ring, `next_desc_idx` advanced `& size_mask`, a 2-bit phase (`desc_ring_id`) that flips on wrap, a permanent 16-descriptor guard gap in the free-slot count, and the **doorbell** = `mb()` then `reg_write32(drtp_inc, num)` at `+0x38`.
- **The optional completion model**: `cdesc_base == NULL` sets `UDMA_Q_FLAGS_NO_COMP_UPDATE` and writes `comp_cfg = 0`; the consumer-side `udma_cdesc_ack` CAS-advances `next_cdesc_idx@+0x34` to recycle slots after a host-marker poll (owned by [ring-cycle](../dma/ring-cycle.md)).
- **The single-revision gate**: `UDMA_REV_ID_4=4`, `DMA_MAX_Q_V4=16`, all-`_v4` registers; no other revision is built.

| | |
|---|---|
| **Source** | `udma/udma_main.c` (713 lines), `udma/udma.h` (553 lines), aws-neuronx-dkms 2.27.4.0, GPL-2.0 |
| **Engine handle** | `struct udma` (`udma.h:175–194`) — bi-directional, `udma_q_m2s[16]` + `udma_q_s2m[16]` |
| **Per-queue shadow** | `struct udma_q` `__aligned(64)` (`udma.h:145–172`) — producer/consumer cursors + cached CFG |
| **Revision gate** | `rev_id = UDMA_REV_ID_4 = 4`, `num_of_queues_max = DMA_MAX_Q_V4 = 16` (`udma_main.c:284–285`; `udma.h:12,30`) |
| **Queue range** | `UDMA_MIN_Q_SIZE = 32` … `UDMA_MAX_Q_SIZE = (1<<24)-16` descriptors; 256-byte start/end alignment (`udma.h:23,26,27`) |
| **Doorbell** | `udma_desc_action_add` (`udma_main.c:700`) → `mb()` `:709`, `reg_write32(&rings.drtp_inc, num)` `:710` @ **`+0x38`** |
| **Recycle** | `udma_cdesc_ack` (`udma.h:452`) — lock-free CAS on `next_cdesc_idx` @ **`+0x34`** (`& size_mask`) |
| **Completion** | optional; `cdesc_base == NULL` ⇒ `UDMA_Q_FLAGS_NO_COMP_UPDATE` (`udma_main.c:35,245–246`), `comp_cfg = 0` |
| **Phase bit** | `desc_ring_id`, 2-bit, init `UDMA_INITIAL_RING_ID = 1`, flips `& DMA_RING_ID_MASK = 0x3` on wrap (`udma.h:33,35`) |
| **Public entry points** | `udma_init` · `udma_set_defaults` · `udma_cache_defaults` · `udma_q_init` · `udma_q_enable/pause` · `udma_q_handle_get` · `udma_state_set/get` · `udma_desc_action_add` |
| **Up boundary** | ring container + ownership → [dma-rings](dma-rings.md) |
| **Side boundary** | descriptor-pair builder + trigger → [udma-m2m](udma-m2m.md); error-int unmask → [udma-iofic](udma-iofic.md) |
| **Down boundary** | 16-byte descriptor wire format → [descriptor-format](../dma/descriptor-format.md) |

---

## Entry Point — The Lifecycle Tree

Two independent lifecycles compose: the per-engine bring-up (`udma_init`, run once per hardware engine by the DHAL `ndma_init`) and the per-queue bring-up (`udma_q_init`, run per queue by `udma_m2m_init_queue`). The producer/consumer primitives run after both, on the hot path.

```text
udma_init (udma_main.c:335)                         ── ENGINE bring-up (per HW engine)
  ├─ udma_handle_init_aux (:280)                     ── rev_id=4, num_q_max=16, map _v4 regs
  │    ├─ map gen/gen_ex/m2s/s2m sub-blocks (:302-307)
  │    ├─ per-queue q_regs pointers (:314-318)
  │    ├─ udma_q_enable(all 16 m2s + 16 s2m, 1) (:325-326)   ── "enable on init, never disable"
  │    └─ state_m2s = state_s2m = UDMA_DISABLE (:329-330)
  ├─ udma_set_defaults (:67)                          ── AXI/FIFO/stream/timeout CSRs, addr_hi sels
  ├─ udma_iofic_m2s/s2m_error_ints_unmask (:350-351)  ── BOUNDARY: udma-iofic.md
  ├─ write s2m_comp.cfg_1c (cdesc_size>>2, Q_PROMOTION) (:354-356)
  └─ udma_cache_defaults (:184)                       ── seed q->cfg / q->rlimit_mask shadows

udma_q_init (udma_main.c:502)                        ── QUEUE bring-up (per queue)
  ├─ udma_q_init_validate (:368)                      ── size/align/qid/compl checks
  └─ udma_q_init_internal (:438)                      ── fill udma_q shadow (ring_id=1, status=DISABLED)
       ├─ [TX only] write rlimit.mask, clear INTERNAL_PAUSE_DMB (:471-480)   ── "enable DMB"
       ├─ udma_q_reset (:415)                          ── pause + SW_CTRL_RST_Q (needs cdesc_size==0)
       ├─ udma_q_set_pointers (:232)                   ── drbp_low/high, drl, crbp_*; udma_q_config_compl
       └─ udma_q_enable(q, 1) (:262)                   ── EN_PREF|EN_SCHEDULING into rings.cfg

[hot path — producer]                                ── after both lifecycles (callers in udma-m2m.md)
  udma_desc_get (udma.h:379)      ── pull slot, advance next_desc_idx & size_mask
  udma_ring_id_get (udma.h:423)   ── phase bit for that desc; flip on wrap
  udma_desc_action_add (:700)     ── mb(); reg_write32(&rings.drtp_inc, num)   ── DOORBELL @ +0x38

[hot path — consumer]
  udma_cdesc_ack (udma.h:452)     ── CAS next_cdesc_idx += num (& size_mask)   ── RECYCLE @ +0x34
```

---

## 1. The Engine and Queue Handles

### Purpose

Before any lifecycle step makes sense, the two software structures must be pinned. `struct udma` is the per-engine handle — a bi-directional unit holding both M2S and S2M register banks and both 16-queue arrays. `struct udma_q` is the per-queue shadow: the producer/consumer cursors, the cached CFG, and the pointers into MMIO and host ring memory. Every function on this page navigates `udma → udma_q_{m2s,s2m}[qid] → q_regs`.

### `struct udma` — the engine handle

`struct udma` (`udma.h:175–194`) is one hardware DMA engine. It is **bi-directional**: a single handle carries both the M2S (TX) and S2M (RX) register banks and both queue arrays, because a UDMA engine physically *is* one unit that drives both directions of a copy.

| Field | Type | Role |
|---|---|---|
| `name[25]` | `char[]` | engine instance name (`UDMA_INSTANCE_NAME_LEN = 25`, `udma.h:125`) |
| `state_m2s` / `state_s2m` | `enum udma_state` | shadow of the per-direction engine FSM, seeded `UDMA_DISABLE` at init (`:329–330`) |
| `num_of_queues_max` | `u8` | always `DMA_MAX_Q_V4 = 16` (`:285`) |
| `num_of_queues` | `u8` | queues actually used (`0xff` ⇒ max, `:288–291`) |
| `unit_regs_base` | `void __iomem *` | the `unit_regs_v4` MMIO base all sub-block pointers derive from |
| `udma_regs_m2s` / `_s2m` | `*_v4 __iomem *` | the TX / RX register banks (`udma_main.c:306–307`) |
| `gen_regs` / `gen_axi_regs` / `gen_int_regs` / `gen_ex_regs` | `__iomem` | gen / AXI / IOFIC / gen-ex register sub-blocks (`:302–305`) |
| `udma_q_m2s[16]` / `udma_q_s2m[16]` | `struct udma_q[]` | the per-queue shadows, **embedded by value** (`udma.h:189–190`) |
| `rev_id` | `unsigned int` | always `UDMA_REV_ID_4 = 4` (`:284`) |
| `cdesc_size` | `u32` | completion-descriptor size in bytes (per-engine; 0 ⇒ no HW completion) |
| `reserve_max_read_axi_id` | `bool` | a "V2 configuration workaround" flag consumed by `udma_set_defaults` |

Because `udma_q_m2s[16]` and `udma_q_s2m[16]` are embedded by value, the entire 32-queue shadow is one contiguous block inside the engine handle — there is no per-queue allocation. The DHAL allocates `struct udma` inside `struct ndma_eng` ([dma-rings §1](dma-rings.md#1-the-nesting-model)).

### `struct udma_q` — the per-queue shadow

`struct udma_q` (`udma.h:145–172`, `__aligned(64)` to sit on a cache line) is the software state for one queue. The cursor offsets below are the ones the hot path touches; they are confirmed byte-for-byte against the userspace `al_mla_udma_*` field accesses on the [ring-cycle](../dma/ring-cycle.md#1-the-ring-cursors-and-the-wrap-rule) page.

| Field | Offset | Role | Confidence |
|---|---|---|---|
| `size_mask` | `0x00` | `size - 1` wrap mask (pow2 ring) | CERTAIN |
| `q_regs` | `0x08` | per-queue MMIO base — the doorbell lives at `+0x38` within it | CERTAIN |
| `desc_base_ptr` | `0x10` | submission-ring base VA | CERTAIN |
| `next_desc_idx` | `0x1c` | **PRODUCER** index — advanced by `udma_desc_get` | CERTAIN |
| `desc_ring_id` | `0x20` | current **PHASE** (2-bit ring id) | CERTAIN |
| `cdesc_base_ptr` | `0x24` | completion-ring base; **NULL on Neuron M2M** ⇒ no HW CQ | CERTAIN |
| `next_cdesc_idx` | `0x34` | **CONSUMER** / completion counter — advanced by `udma_cdesc_ack` | CERTAIN |
| `size` | `0x6c` | ring size in descriptors (pow2) | CERTAIN |
| `cfg` | (cached) | cached CFG CSR — RMW'd by `udma_q_enable`, never read back | CERTAIN |
| `rlimit_mask` | (cached, m2s only) | cached rate-limit mask — written once in `udma_q_init_internal` | CERTAIN |

> **QUIRK —** the field-declaration order in `struct udma_q` is *not* the byte-offset order. The struct is `__aligned(64)` but its members include a `dma_addr_t` (`desc_phy_base`) and pointers whose alignment the compiler honors; the offsets in the table above are the *recovered* layout (cross-checked against the disassembly on [ring-cycle](../dma/ring-cycle.md)), which is why `next_cdesc_idx` lands at `+0x34` even though it is declared near the middle of the struct (`udma.h:154`). A reimplementer who computes offsets by counting declared field widths will diverge; trust the recovered offsets.

> **NOTE —** the cursors are why this layer needs no separate ring object. `next_desc_idx` (`+0x1c`) and `next_cdesc_idx` (`+0x34`) chase each other modulo `size_mask` (`+0x00`); `desc_ring_id` (`+0x20`) is the phase. The submission *ring* is just `desc_base_ptr[next_desc_idx]` with wrap. The full wrap/phase math — the 16-descriptor guard gap in `udma_available_get`, the phase flip — is the producer side of §3 and is owned by [ring-cycle §1](../dma/ring-cycle.md#1-the-ring-cursors-and-the-wrap-rule); it is not re-derived here.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_handle_init_aux` | `udma_main.c:280` | populate the handle, map `_v4` registers, enable all queues | CERTAIN |
| `udma_q_handle_get` | `udma_main.c:541` | bounds-check `qid`, return `&udma_q_{m2s,s2m}[qid]` | CERTAIN |
| `udma_q_is_enabled` | `udma_main.c:523` | read `rings.cfg`, true if `EN_PREF\|EN_SCHEDULING` set | CERTAIN |

---

## 2. The Engine and Queue Lifecycle

### Purpose

`udma_init` configures one hardware engine end-to-end; `udma_q_init` binds and programs one queue. The two are independent — an engine is brought up once (per `struct udma`), then queues are initialized into it on demand. The defining lifecycle decisions are the **cached-CSR shortcut** (read the datasheet reset values once into a software shadow rather than reading slow CSRs on every enable/disable) and the **enable-on-init policy** (all 32 queues are enabled the moment the handle is built and are never disabled in the normal path).

### Entry Point

```text
[DHAL ndma_init / udma_m2m_init_engine]              ── BOUNDARY: udma-m2m.md
  └─ udma_init (udma_main.c:335)
       └─ udma_handle_init_aux → udma_set_defaults → iofic unmask → cfg_1c → udma_cache_defaults

[ndmar_queue_init / ndmar_h2t_ring_init / udma_m2m_init_queue]   ── BOUNDARY: dma-rings.md, udma-m2m.md
  └─ udma_q_init (udma_main.c:502)
       └─ udma_q_init_validate → udma_q_init_internal
```

### Algorithm — engine bring-up

```c
// udma_init — udma_main.c:335.  Bring up one hardware DMA engine.
function udma_init(udma, params):
    udma_handle_init_aux(udma, params)                    // :340  map regs, enable queues (below)
    udma_set_defaults(udma)                               // :345  AXI/FIFO/stream/timeout CSRs (§Considerations)
    udma_iofic_m2s_error_ints_unmask(udma)                // :350  BOUNDARY: udma-iofic.md
    udma_iofic_s2m_error_ints_unmask(udma)                // :351

    udma->cdesc_size = params->cdesc_size                 // :353  per-engine completion-desc size
    // program S2M completion cfg_1c: desc size in WORDS (>>2) + Q_PROMOTION bit.
    val = (1 << UDMA_S2M_COMP_CFG_1C_Q_PROMOTION_SHIFT)                              // :354
        | (params->cdesc_size >> 2) << UDMA_S2M_COMP_CFG_1C_DESC_SIZE_SHIFT          // :355
    reg_write32(&udma->udma_regs_s2m->s2m_comp.cfg_1c, val)                          // :356
    udma_cache_defaults(udma)                             // :357  seed q->cfg / q->rlimit_mask
    return 0

// udma_handle_init_aux — udma_main.c:280.  Populate the SW handle + map _v4 registers.
function udma_handle_init_aux(udma, params):
    udma->rev_id            = UDMA_REV_ID_4               // :284  == 4, HARD-CODED (V1 hw == rev4)
    udma->num_of_queues_max = DMA_MAX_Q_V4               // :285  == 16
    udma->reserve_max_read_axi_id = params->reserve_max_read_axi_id   // :286
    udma->num_of_queues = (params->num_of_queues == UDMA_NUM_QUEUES_MAX)  // :288  0xff ⇒ max
                        ? udma->num_of_queues_max : params->num_of_queues
    if udma->num_of_queues > num_of_queues_max: return -EINVAL          // :293

    unit_regs = (struct unit_regs_v4 *) params->udma_regs_base          // :300  single _v4 base
    udma->gen_regs     = &unit_regs->gen                                 // :302  map sub-blocks
    udma->gen_axi_regs = &unit_regs->gen.axi
    udma->gen_int_regs = &unit_regs->gen.interrupt_regs                  // :304  IOFIC
    udma->gen_ex_regs  = &unit_regs->gen_ex
    udma->udma_regs_m2s = (udma_m2s_regs_v4 *) &unit_regs->m2s           // :306
    udma->udma_regs_s2m = (udma_s2m_regs_v4 *) &unit_regs->s2m           // :307

    for i in 0 .. num_of_queues_max-1:                                   // :314  ALL 16, not num_of_queues
        udma->udma_q_m2s[i].q_regs = &udma->udma_regs_m2s->m2s_q[i]      // :315  per-queue MMIO
        udma->udma_q_s2m[i].q_regs = &udma->udma_regs_s2m->s2m_q[i]      // :317
        udma->udma_q_m2s[i].status = QUEUE_ENABLED                      // :321
        udma->udma_q_s2m[i].status = QUEUE_ENABLED
        udma_q_enable(&udma->udma_q_m2s[i], 1)            // :325  *** enable on init, never disable ***
        udma_q_enable(&udma->udma_q_s2m[i], 1)            // :326

    udma->state_m2s = udma->state_s2m = UDMA_DISABLE      // :329-330  engine FSM starts DISABLED
    return 0

// udma_cache_defaults — udma_main.c:184.  Seed SW shadows from datasheet reset values.
function udma_cache_defaults(udma):
    for i in 0 .. num_of_queues-1:                        // :187
        udma->udma_q_m2s[i].cfg         = M2S_CFG_RESET_VALUE        // :189  3<<24
        udma->udma_q_m2s[i].rlimit_mask = M2S_RATE_LIMIT_RESET_VALUE // :190  0b111
        udma->udma_q_s2m[i].cfg         = S2M_CFG_RESET_VALUE        // :192  3<<24
    return 0
```

> **QUIRK —** `udma_handle_init_aux` enables *all 16* M2S and *all 16* S2M queues at init (`:325–326`, loop bound `num_of_queues_max`), and the only disable path in the entire HAL is `udma_q_pause` → `udma_q_enable(q, 0)` (`udma_main.c:407–410`). The source comment is explicit: *"We enable the DMA queues on init and never disable them"* (`:324`). A reimplementer who follows a conventional "enable a queue when it gets work, disable it when idle" discipline will diverge: here, `EN_PREF | EN_SCHEDULING` are set in `rings.cfg` for every queue before any descriptor exists, and the engine simply finds the rings empty until the doorbell rings. Pause is used only for explicit quiesce (per-arch DHAL engine teardown).

> **NOTE —** the cached-CSR shortcut is a real performance decision, not bookkeeping. The comment (`udma_main.c:178–183`) states CSR reads are *"very slow and only one application(neuron) is using the DMA"*, so `udma_cache_defaults` writes the **datasheet reset values** (`M2S_CFG_RESET_VALUE = 3<<24`, etc., `:174–176`) directly into the software `q->cfg`/`q->rlimit_mask`. Thereafter `udma_q_enable` (§Considerations) RMWs `q->cfg` in memory and writes it back to the CFG CSR — it **never reads the CFG CSR**. A reimplementer who reads-modify-writes the live register will be correct but slow; the whole point of the shadow is to make enable/disable a single MMIO *write*.

### Algorithm — queue bring-up

```c
// udma_q_init — udma_main.c:502.  Public wrapper: validate then init.
function udma_q_init(udma, qid, q_params):
    udma_q_init_validate(udma, qid, q_params)            // :506  (below)
    udma_q_init_internal(udma, qid, q_params)            // :509
    return 0

// udma_q_init_validate — udma_main.c:368.  Geometry + completion checks.
function udma_q_init_validate(udma, qid, q_params):
    if udma == NULL or q_params == NULL: return -EINVAL                  // :372
    if qid >= udma->num_of_queues:       return -EINVAL                  // :376
    if q_params->size < UDMA_MIN_Q_SIZE: return -EINVAL                  // :381  < 32
    if q_params->size > UDMA_MAX_Q_SIZE: return -EINVAL                  // :386  > (1<<24)-16
    // hardware requires ring START and END addr both 256-byte aligned (prefetch).
    bytes = q_params->size * sizeof(union udma_desc)                     // :392  size * 16
    if not (HAS_ALIGNMENT(desc_phy_base, 256) and
            HAS_ALIGNMENT(desc_phy_base + bytes, 256)): return -EINVAL   // :393
    udma_q = (type == TX) ? &udma_q_m2s[qid] : &udma_q_s2m[qid]          // :398
    if udma_q->cdesc_base_ptr and udma->cdesc_size == 0: return -EIO     // :399  compl req'd but engine has none
    return 0

// udma_q_init_internal — udma_main.c:438.  Fill SW shadow + program HW.
function udma_q_init_internal(udma, qid, q_params):
    udma_q = (type == TX) ? &udma_q_m2s[qid] : &udma_q_s2m[qid]          // :442
    udma_q->type          = q_params->type                              // :443
    udma_q->size          = q_params->size                              // :445
    udma_q->size_mask     = q_params->size - 1                          // :446  pow2 wrap mask
    udma_q->desc_base_ptr = q_params->desc_base                         // :447
    udma_q->desc_phy_base = q_params->desc_phy_base                     // :448
    udma_q->cdesc_base_ptr= q_params->cdesc_base                        // :449  NULL ⇒ no completion
    udma_q->cdesc_size    = (cdesc_base == NULL) ? 0 : udma->cdesc_size // :452-455  per-engine
    udma_q->next_desc_idx = 0; udma_q->next_cdesc_idx = 0               // :456,458  cursors reset
    udma_q->desc_ring_id  = UDMA_INITIAL_RING_ID                        // :463  == 1 (HW expects 1)
    udma_q->comp_ring_id  = UDMA_INITIAL_RING_ID                        // :464
    udma_q->flags  = 0; udma_q->status = QUEUE_DISABLED                 // :466-467

    if type == UDMA_TX:                                                 // :471  TX only
        val = udma_q->rlimit_mask                                       // :476  cached reset value
        val &= ~UDMA_M2S_Q_RATE_LIMIT_MASK_INTERNAL_PAUSE_DMB           // :478  clear bit2 == "enable DMB"
        reg_write32(&udma_q->q_regs->m2s_q.rlimit.mask, val)           // :479

    udma_q_reset(udma_q)                                                // :484  pause + SW_CTRL_RST_Q
    udma_q_set_pointers(udma_q)                                         // :490  program ring base CSRs (§3)
    udma_q_enable(udma_q, 1)                                            // :495  EN_PREF|EN_SCHEDULING
    return 0

// udma_q_reset — udma_main.c:415.  Only valid when no HW completion ring.
function udma_q_reset(udma_q):
    if udma_q->cdesc_size != 0: return -1                 // :419  *** see GOTCHA ***
    udma_q_pause(udma_q)                                  // :423  = udma_q_enable(q, 0)
    reg = (type == TX) ? &m2s_q.q_sw_ctrl : &s2m_q.q_sw_ctrl              // :426-431
    reg_write32(reg, SW_CTRL_RST_Q)                       // 1<<8  assert queue reset
    return 0
```

> **GOTCHA —** `udma_q_reset` returns `-1` (failure) when `cdesc_size != 0` (`udma_main.c:419`), and `udma_q_init_internal` propagates that failure (`:484–487`). The implication is structural: **a queue configured with a real hardware completion ring cannot be reset by this HAL** — `udma_q_init` of such a queue fails at the reset step. This is harmless on Neuron only *because* the production M2M path always passes `cdesc_base == NULL` (so `cdesc_size` is forced to 0 at `:453`), so every Neuron queue is resettable. A reimplementer who provisions a completion ring inherits a queue that `udma_q_init` refuses to initialize. The guard exists because `SW_CTRL_RST_Q` clears the descriptor pointers but not a live completion ring's state; the HAL forbids the combination rather than handling it.

> **NOTE —** `udma_q_init_validate` accepts **any** size in `[32, (1<<24)-16]` — it does *not* require a power of two (`:381–389`). But the producer/consumer inline helpers (`udma_available_get`, `udma_desc_get`, `udma_cdesc_ack`, §3) all `pr_err` and bail unless `IS_POWER_OF_TWO(size)`. The two are reconciled by the caller: the only path that uses the wraparound helpers is the H2T ring, and its size comes from `ndmar_ring_get_desc_count`, which rounds up to a power of two ([dma-rings §3](dma-rings.md#3-descriptor-count-rounding)). A non-pow2 queue can be *created* and *programmed* (base pointers, enable) but cannot be safely *driven* by the inline producer — a reimplementer must keep the pow2 invariant on any wraparound queue.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_init` | `udma_main.c:335` | engine bring-up: handle → defaults → iofic → cfg_1c → cache | CERTAIN |
| `udma_handle_init_aux` | `udma_main.c:280` | map `_v4` regs, enable all queues, seed `rev_id`/`num_q` | CERTAIN |
| `udma_set_defaults` | `udma_main.c:67` | one-shot AXI/FIFO/stream/timeout CSR config (§Considerations) | CERTAIN |
| `udma_cache_defaults` | `udma_main.c:184` | seed `q->cfg`/`q->rlimit_mask` from datasheet reset values | CERTAIN |
| `udma_q_init` / `_validate` / `_internal` | `udma_main.c:502 / 368 / 438` | queue bring-up: validate, fill shadow, program HW | CERTAIN |
| `udma_q_reset` | `udma_main.c:415` | pause + `SW_CTRL_RST_Q`; refuses `cdesc_size != 0` | CERTAIN |
| `udma_q_set_pointers` | `udma_main.c:232` | write ring base/length CSRs + `udma_q_config_compl` (§3) | CERTAIN |
| `udma_q_config_compl` | `udma_main.c:199` | program `comp_cfg`: `0` if no-update, else `EN_COMP_RING_UPDATE\|DIS_COAL` | CERTAIN |
| `udma_q_enable` / `udma_q_pause` | `udma_main.c:262 / 407` | set/clear `EN_PREF\|EN_SCHEDULING` in cached `cfg`; pause = enable(0) | CERTAIN |
| `udma_m2s_packet_size_cfg_set` | `udma_main.c:41` | program M2S `cfg_len` max packet size + `ENCODE_64K` | CERTAIN |

### Considerations

`udma_set_defaults` (`udma_main.c:67`) is the one-shot per-engine CSR program: M2S read-data FIFO depth (`ndhal->ndhal_udma.num_beats`, `:83`), M2S/S2M AXI outstanding-transaction limits with the `reserve_max_read_axi_id` branch (`:87–96`, `:138`), AXI timeout `100*1000*1000` (~118 ms, `:102`), application-ack = 0 (`:105`, `:127`), max packet size, M2S stream threshold mode (`:114`), per-queue `addr_hi` selectors set to `0xffffffff` across `num_queues` (`:119–135`), the V4-only `ostand_cfg_wr_2 = 256` branch (`:152–160`, gated on `arch == NEURON_ARCH_V4`), and finally enabling completion-ring head reporting by clearing bit0 of `gen_regs->spare_reg.zeroes0` (`:162–169`).

> **QUIRK —** the `reserve_max_read_axi_id` flag is described in the struct as *"a V2 configuration workaround"* (`udma.h:135,193`), and it switches the M2S outstanding-transaction limits to a conservative `111/8/0/0` (`:88–91`) and the S2M read-max-desc to `8` (`:138`). Yet this HAL only ever builds the rev-4 path. The flag is plumbed through `udma_params` from the DHAL, so whether the V2 branch is ever taken on a rev-4-only build depends on what the per-arch DHAL passes — the HAL itself does not gate it on revision. A reimplementer targeting a single generation can hard-select one branch; the conditional is a vestige of the multi-rev al_hal lineage. (Confidence that the branch *exists*: CERTAIN; that it is *reachable* on a given Neuron generation: MEDIUM — depends on the DHAL caller.)

---

## 3. Producer Primitives and the Doorbell

### Purpose

Once a queue is initialized, upper layers ([udma-m2m](udma-m2m.md)) fill descriptors and ring the doorbell. The producer side is three inline helpers in `udma.h` (slot allocation, phase bit, free-slot count) plus the one MMIO-writing function in `udma_main.c` — `udma_desc_action_add`, the **doorbell**. The doorbell is the single point where this layer hands work to the silicon: a `mb()` barrier followed by a 32-bit write of the descriptor count to one per-queue register.

### Entry Point

```text
[udma_m2m_copy_prepare_one — BOUNDARY: udma-m2m.md]   ── fills descriptors
  ├─ udma_desc_get (udma.h:379)        ── desc_base_ptr[next_desc_idx]; advance & size_mask
  └─ udma_ring_id_get (udma.h:423)     ── phase bit for that desc; flip on wrap
[udma_m2m_copy_start — BOUNDARY: udma-m2m.md]          ── arms RX then TX
  └─ udma_desc_action_add (udma_main.c:700)    ── THE DOORBELL
       └─ mb(); reg_write32(&q_regs->rings.drtp_inc, num)   ── +0x38
```

### Algorithm — slot allocation and phase

```c
// udma_desc_get — udma.h:379.  Producer: hand out the next submission slot.
function udma_desc_get(q):                               // union udma_desc* | NULL
    if q->desc_base_ptr == NULL: return NULL             // :389  caller manages ring externally
    if not q->is_allocatable:    return NULL             // :391
    idx  = q->next_desc_idx                              // :394
    desc = q->desc_base_ptr + idx                        // :395
    if not IS_POWER_OF_TWO(q->size): { pr_err; return NULL }   // :403  pow2 required for & wrap
    q->next_desc_idx = (idx + 1) & q->size_mask          // :408  advance with wrap
    return desc

// udma_ring_id_get — udma.h:423.  Phase bit for the descriptor just allocated.
function udma_ring_id_get(q):                            // u32  (2-bit phase)
    ring_id = q->desc_ring_id                            // :427  current phase (+0x20)
    if (unlikely(q->next_desc_idx) == 0):                // :431  *** see CORRECTION ***
        q->desc_ring_id = (q->desc_ring_id + 1) & DMA_RING_ID_MASK   // :432  & 0x3, flip on wrap
    return ring_id
```

> **CORRECTION (UDMA-1) — latent parenthesization bug in `udma_ring_id_get`.** The source reads `if (unlikely(udma_q->next_desc_idx) == 0)` (`udma.h:431`), with the `== 0` comparison *outside* the `unlikely()` argument. The compiler parses this as `if (unlikely(next_desc_idx) == 0)` — i.e. `unlikely(next_desc_idx)` (a value) is compared against `0`. Because `unlikely()` is the identity on its operand (`__builtin_expect(x, 0)` returns `x`), the expression *still* evaluates to the intended boolean `next_desc_idx == 0`, so **the phase bit advances correctly**. The intended form was clearly `if (unlikely(udma_q->next_desc_idx == 0))`. Confidence the phase advances correctly: **HIGH** (the value semantics are unchanged). Confidence the code is as-intended-written: **LOW** — a reimplementer copying this verbatim inherits a misplaced paren that defeats the branch-prediction hint (the `unlikely` no longer hints the comparison, only the value). Reproduce the *behavior*, not the typo.

> **NOTE —** the phase ("ring id") starts at `UDMA_INITIAL_RING_ID = 1` (`udma.h:33`, set in `udma_q_init_internal:463`) and is 2-bit (`DMA_RING_ID_MASK = 0x3`, `udma.h:35`). The hardware uses it to distinguish a freshly-written descriptor from a stale one left over from the previous lap of the ring — the producer stamps each descriptor with the current phase, the engine compares. The free-slot count and the 16-descriptor guard gap (`udma_available_get`, `udma.h:355`) are the consumer-facing half of this math and are owned by [ring-cycle §1](../dma/ring-cycle.md#1-the-ring-cursors-and-the-wrap-rule).

### Algorithm — the doorbell

```c
// udma_desc_action_add — udma_main.c:700.  THE DOORBELL.
// Increment the queue's tail-ring pointer, which launches `num` descriptors.
function udma_desc_action_add(q, num):
    if num == 0 or num > q->size: return -1              // :704  reject empty / over-size batch
    addr = &q->q_regs->rings.drtp_inc                    // :708  per-queue MMIO + 0x38 (DOORBELL)
    mb()                                                 // :709  publish descriptor writes BEFORE the doorbell
    reg_write32(addr, num)                               // :710  HW now fetches & executes `num` descriptors
    return 0
```

> **QUIRK —** the doorbell register `rings.drtp_inc` (`+0x38`) is **increment-on-write**, not an absolute index: writing `num` tells the engine *"fetch `num` more descriptors from your internal tail"*, not *"the tail is now at index `num`"*. This is why the function name is `desc_action_*add*` and why it rejects `num == 0` (a no-op write would be meaningless) and `num > size` (more than a full ring). The `mb()` at `:709` is the ordering hinge of the whole DMA path — the source comment is explicit: *"to make sure data written to the descriptors will be visible to the DMA"* (`:709`). Drop the barrier and the engine can fetch a half-written descriptor. This single register is also the **BAR0 security boundary**: the kernel write-blocks `drtp_inc` (and the ring base pointers) against userspace `mmap`, so a userspace-authored ring must cross an ioctl to reach this doorbell — owned by [ring-cycle §3](../dma/ring-cycle.md#3-trigger-and-doorbell--launching-the-batch).

> **NOTE —** the same `+0x38` offset, the `mb()`-then-write order, and the `num <= size` guard are independently confirmed in the `libnrt.so` userspace twin `al_udma_desc_action_add` (`@0x461f60`: `cmp %ebp,0x6c(%rbx)` for `num<=size`, `add $0x38` for the doorbell address, `al_local_data_memory_barrier` for the `mb()`) — see [ring-cycle §7](../dma/ring-cycle.md#7-userspace--kernel-agreement). Kernel and userspace agree byte-for-byte on this offset.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_desc_get` | `udma.h:379` | producer: hand out `desc_base_ptr[next_desc_idx]`, advance `& size_mask` | CERTAIN |
| `udma_ring_id_get` | `udma.h:423` | phase getter; flip `desc_ring_id` on wrap (carries UDMA-1 paren bug) | CERTAIN |
| `udma_available_get` | `udma.h:355` | free-slot count `(cidx - (pidx + 16)) & size_mask` (16-desc guard) | CERTAIN |
| `udma_desc_action_add` | `udma_main.c:700` | **doorbell**: `mb()` + `reg_write32(drtp_inc, num)` @ `+0x38` | CERTAIN |

---

## 4. Consumer Primitive — Completion and Recycle

### Purpose

A retired batch's descriptor slots are not automatically reclaimed — the producer would eventually run out. After completion is *observed* (on Neuron, by a host-memory marker poll, not a hardware completion ring), the upper layer calls `udma_cdesc_ack` to advance the software completion counter `next_cdesc_idx`, which is exactly the value `udma_available_get` subtracts to report free slots. The advance is a lock-free CAS so concurrent ack paths (sync + async double-buffered) can race safely.

### Algorithm

```c
// udma_cdesc_ack — udma.h:452.  Consumer: CAS-advance next_cdesc_idx by `num`.
function udma_cdesc_ack(q, num):
    if not IS_POWER_OF_TWO(q->size): { pr_err; return -1 }   // :458  pow2 required for & wrap
    cdesc_idx      = q->next_cdesc_idx                        // :463  (+0x34)
    next_cdesc_idx = (cdesc_idx + num) & q->size_mask         // :464  advance with wrap
    while not __sync_bool_compare_and_swap(&q->next_cdesc_idx, cdesc_idx, next_cdesc_idx):  // :465
        cpu_relax()                                          // :466  spin
        cdesc_idx      = q->next_cdesc_idx                    // :467  reload
        next_cdesc_idx = (cdesc_idx + num) & q->size_mask     // :468  recompute
    return 0
```

> **QUIRK —** the recycle is a **lock-free CAS** (`__sync_bool_compare_and_swap`, `udma.h:465`), not a plain store, because `next_cdesc_idx` can be advanced concurrently — the synchronous memcpy path and the async double-buffered path can both ack the same queue. The CAS retries on contention with a `cpu_relax()` spin (`:466`). After it lands, `udma_available_get` immediately reports the `num` freed slots — minus the permanent 16-descriptor guard gap. The `+0x34` field and the `lock cmpxchg %edx,0x34(%rbx)` userspace twin are confirmed on [ring-cycle §5](../dma/ring-cycle.md#5-ack-and-recycle--freeing-the-slots); kernel and userspace agree on the offset.

> **NOTE —** on the Neuron production M2M path there is **no hardware completion ring** to read. `udma_q_set_pointers` (`udma_main.c:232`) sets `UDMA_Q_FLAGS_NO_COMP_UPDATE` whenever `cdesc_base_ptr == NULL` (`:245–246`) and `udma_q_config_compl` (`:199`) then writes `comp_cfg = 0` (`:212–213`) — the engine never writes completion descriptors. `next_cdesc_idx` is therefore a **pure software counter**, touched only by `udma_cdesc_ack`; it is not an index into device-written memory. The actual completion *observation* (the `0xabcdef01` host marker poll) lives in `neuron_dma.c` and is owned by [ring-cycle §4](../dma/ring-cycle.md#4-poll--host-marker-completion). This HAL provides only the recycle, not the wait.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_cdesc_ack` | `udma.h:452` | CAS-advance `next_cdesc_idx` by `num` (`& size_mask`); pow2 required | CERTAIN |
| `udma_q_config_compl` | `udma_main.c:199` | `comp_cfg = 0` when `NO_COMP_UPDATE`; else enable ring-update + dis-coalesce | CERTAIN |

---

## 5. The Engine State Machine

### Purpose

`udma_state_set` drives the engine FSM (NORMAL / DISABLE / ABORT) by writing both the M2S and S2M `change_state` registers; `udma_state_get` reads the live hardware state and reduces four sub-FSMs into one coarse state. These are the quiesce/abort controls the ring layer ([dma-rings](dma-rings.md)) and reset path use; they do not touch descriptors.

### Algorithm

```c
// udma_state_set — udma_main.c:557.  Drive BOTH directions' FSM.
function udma_state_set(udma, state):
    switch state:                                        // :562
        case UDMA_DISABLE: reg = CHANGE_STATE_DIS        // :564  1<<1
        case UDMA_NORMAL:  reg = CHANGE_STATE_NORMAL     // :567  1<<0
        case UDMA_ABORT:   reg = CHANGE_STATE_ABORT      // :570  1<<2
        default:           return -EINVAL                // :573  RESET/IDLE not settable
    reg_write32(&udma->udma_regs_m2s->m2s.change_state, reg)   // :577  TX
    reg_write32(&udma->udma_regs_s2m->s2m.change_state, reg)   // :578  RX
    udma->state_m2s = udma->state_s2m = state            // :580-581  update shadow
    return 0

// udma_state_get — udma_main.c:639.  Read live state, reduce 4 sub-FSMs.
function udma_state_get(udma, type):                     // -> ABORT > NORMAL > IDLE
    if type == UDMA_TX:
        state_reg = read m2s.state; stream_enabled = true            // :649-650  TX ignores stream
    else:
        state_reg = read s2m.state                                  // :652
        stream_enabled = udma_s2m_stream_status_get(udma)           // :653  RX cross-checks stream
    comp_ctrl = (state_reg & 0x0003)        // shift 0   (:656)
    stream_if = (state_reg & 0x0030) >> 4   //           (:658)
    data_rd   = (state_reg & 0x0300) >> 8   //           (:660)
    desc_pref = (state_reg & 0x3000) >> 12  //           (:662)
    // ABORT dominates; if stream disabled, ignore stream_if (HW-bug workaround, :665-672)
    if any relevant sub-FSM == UDMA_STATE_ABORT(0x2):  return UDMA_ABORT    // :675-683
    if any relevant sub-FSM == UDMA_STATE_NORMAL(0x1): return UDMA_NORMAL   // :685-694
    return UDMA_IDLE                                                        // :696
```

> **QUIRK —** `udma_state_get` carries a documented hardware-bug workaround (`udma_main.c:665–672`): when the S2M stream is disabled but packets are still waiting to enter the UDMA, the `stream_if` sub-FSM can get *"stuck at 1"*. The reduction therefore **ignores `stream_if`** when the stream is disabled (`stream_enabled == false`, the `else` branches at `:680` and `:691` omit `stream_if` from the OR). The stream-enabled determination itself (`udma_s2m_stream_status_get`, `:591`) cross-checks the engine-level `s2m.stream_cfg` against every enabled RX queue's per-queue `EN_STREAM` bit, and on disagreement logs *"bad config…assuming stream is enabled"* and returns `true` (`:628–632`) — it fails *safe* (assume running) rather than risk reporting IDLE on a live engine. A reimplementer who reduces all four sub-FSMs unconditionally will spuriously report ABORT/NORMAL on a quiescing RX engine.

> **NOTE —** `udma_state_set` refuses `UDMA_RESET` and `UDMA_IDLE` with `-EINVAL` (`:572–574`) — only NORMAL/DISABLE/ABORT are *settable*; IDLE and RESET are *observed* states, not commanded ones. It writes both `m2s.change_state` and `s2m.change_state` (`:577–578`) so the whole bi-directional engine transitions together; there is no per-direction state command in this HAL.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_state_set` | `udma_main.c:557` | drive M2S+S2M `change_state` (NORMAL/DIS/ABORT); update shadow | CERTAIN |
| `udma_state_get` | `udma_main.c:639` | read live state, reduce 4 sub-FSMs to ABORT > NORMAL > IDLE | CERTAIN |
| `udma_s2m_stream_status_get` | `udma_main.c:591` | determine RX stream enabled; fail-safe on disagreement | CERTAIN |

---

## 6. The Single-Revision Gate

### Purpose

The original al_hal supported multiple UDMA revisions through runtime dispatch. The Neuron fork collapses this to exactly one. Every reimplementer should know they can delete the multi-rev machinery: only rev-4 is built, only `_v4` registers are accessed, and the queue count is a compile-time 16.

### The gate

```c
// udma_handle_init_aux — udma_main.c:283-285.  The whole gate, hard-coded.
udma->rev_id            = UDMA_REV_ID_4;     // == 4   (udma.h:30)   "V1 hardware uses DMA rev4"
udma->num_of_queues_max = DMA_MAX_Q_V4;      // == 16  (udma.h:12)
// ... and every register pointer is cast to its _v4 struct (udma_main.c:300-307):
unit_regs = (struct unit_regs_v4 *) params->udma_regs_base;
```

| Constant | Value | Source | Consumed by |
|---|---|---|---|
| `UDMA_REV_ID_4` | `4` | `udma.h:30` | `udma_handle_init_aux:284` — `rev_id` |
| `DMA_MAX_Q_V4` | `16` | `udma.h:12` | `num_of_queues_max:285`; `udma_q_{m2s,s2m}[16]` array bound |
| `UDMA_NUM_QUEUES_MAX` | `0xff` | `udma.h:21` | sentinel ⇒ use max (`:288`) |
| `UDMA_INITIAL_RING_ID` | `1` | `udma.h:33` | initial phase (`udma_q_init_internal:463`) |
| `DMA_RING_ID_MASK` | `0x3` | `udma.h:35` | phase wrap (`udma_ring_id_get:432`) |
| `UDMA_MIN_Q_SIZE` / `UDMA_MAX_Q_SIZE` | `32` / `(1<<24)-16` | `udma.h:26,27` | queue size validation (`:381,386`) |
| `UDMA_QUEUE_ADDR_BYTE_ALIGNMENT` | `256` | `udma.h:23` | ring start/end alignment (`:393`) |
| `UDMA_MAX_NUM_CDESC_PER_CACHE_LINE` | `16` | `udma.h:16` | guard gap in `udma_available_get` |
| `UDMA_CDESC_SIZE` | `16` | `udma.h:476` | completion-descriptor size |
| `MAX_DMA_DESC_SIZE` | `65536` | `udma.h:479` | 64K per-descriptor transfer limit |

> **QUIRK —** the gate is *not* a runtime check — there is no `if (rev == 4)` anywhere. `rev_id` is written but only read for `pr_debug`; the real gate is that `udma_handle_init_aux` *casts the register base to `unit_regs_v4`* and every sub-block dereference uses the `_v4` struct (`udma_main.c:300–307`). A reimplementer targeting a different revision must change the struct types, not a runtime flag. The `num_of_queues_max = 16` is likewise a hard ceiling: the `udma_q_m2s[DMA_MAX_Q_MAX]` / `udma_q_s2m[DMA_MAX_Q_MAX]` arrays are sized `DMA_MAX_Q_MAX = DMA_MAX_Q_V4 = 16` at compile time (`udma.h:13,189–190`), so even a `num_of_queues` request above 16 is rejected (`:293`) rather than growing the arrays.

---

## Related Components

| Name | Relationship |
|---|---|
| `struct udma` / `struct udma_q` | The engine handle and per-queue shadow this page documents (`udma.h:175 / 145`) |
| `udma_desc_action_add` | The doorbell — `mb()` + `reg_write32(drtp_inc, num)` @ `+0x38` (`udma_main.c:700`) |
| `udma_cdesc_ack` | The recycle CAS on `next_cdesc_idx` @ `+0x34` (`udma.h:452`) |
| `udma_m2m_init_queue` / `udma_m2m_copy_start` | The M2M builder that calls `udma_q_init` and the doorbell ([udma-m2m](udma-m2m.md)) |
| `ndma_eng` / `ndma_ring` | The ring container that embeds `struct udma` and owns ownership/locking ([dma-rings](dma-rings.md)) |
| `udma_iofic_*_error_ints_unmask` | The IOFIC error-interrupt unmask called from `udma_init` ([udma-iofic](udma-iofic.md)) |

## Cross-References

- [DMA Rings and H2T Queues](dma-rings.md) — the container layer above: `struct ndma_eng` embeds this page's `struct udma`, and `ndmar_*` owns ring allocation, ownership tracking, and locking
- [UDMA Memory-to-Memory Builder](udma-m2m.md) — the descriptor-pair builder and `udma_m2m_copy_start` (RX-then-TX trigger) that drive this core's `udma_q_init`, `udma_desc_get`, and doorbell
- [UDMA IOFIC Interrupt Controller](udma-iofic.md) — `udma_iofic_m2s/s2m_error_ints_unmask`, called from `udma_init`; the `gen_int_regs` block this page maps
- [The Ring / Trigger / Doorbell / Completion Cycle](../dma/ring-cycle.md) — the dynamic stage→trigger→doorbell→poll→ack cycle that drives these primitives; owns the wrap/phase math, the host-marker poll, and the userspace-disassembly corroboration of `+0x38`/`+0x34`
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the wire format of the `union udma_desc` entries these rings hold; owns the `len_ctrl`/`meta_ctrl` bitfields and barrier modes (not re-derived here)
- [back to index](../index.md)
