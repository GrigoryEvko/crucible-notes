# The Ring / Trigger / Doorbell / Completion Cycle

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME `libnrt.so.1`, ELF64, **not stripped**, DWARF present; `.text` VMA == file offset, so every `0x4…`/`0x2…`/`0xc…` is an analysis VMA). Kernel citations are `file:line` into `aws-neuronx-dkms 2.27.4.0` (GPL-2.0), chiefly `neuron_dma.c`, `udma/udma_main.c`, `udma/udma_m2m.c`, `udma/udma.h`.*
> *Evidence grade: **Confirmed (byte- and source-anchored)** — the trigger, the doorbell register write, the host-marker completion model and the recycle CAS are each authored by two independent code lines (userspace KaenaHal + kernel DKMS) and reconciled (§7). Other versions will differ. · Part VIII — DMA & Descriptor Engine · [back to index](../index.md)*

## Abstract

The [16-byte descriptor](descriptor-format.md) is a static wire format; this page documents the **dynamic cycle** that runs *over* the descriptor rings — how a staged batch of those descriptors is launched on the silicon and how its completion is observed. The cycle has five phases that are identical in shape on both code lines: **stage → trigger → doorbell → poll-marker → ack/recycle**. A reader who knows any DMA-engine ring driver already owns the frame: a submission ring with a producer index and a consumer index, a tail-pointer doorbell the CPU rings to hand descriptors to the hardware, and a completion signal the CPU waits on. The Neuron UDMA cycle is that frame with one decisive Alpine twist — **there is no hardware completion queue on the production M2M path** (`cdesc_base == NULL` on every Neuron M2M queue; `udma_m2m_init_queue` is programmed with a NULL completion ring, [dma-rings §4.3](../kernel/dma-rings.md)). Completion is observed instead by a **host-memory marker**: a 4-byte self-copy descriptor appended after the data descriptors writes the sentinel `0xabcdef01` into a host buffer, and the CPU polls that one word. When it flips, every prior descriptor is guaranteed retired, and the driver advances a software completion counter (`udma_cdesc_ack`) to recycle the spent slots.

The doorbell is a **single `mb()`-guarded 32-bit MMIO write** of `num` (the count of descriptors just staged) into one per-queue register, `rings.drtp_inc` at offset `+0x38` in the per-queue MMIO block — Descriptor Tail-Ring Pointer *increment*. Writing `num` tells the engine "fetch and execute `num` more descriptors from your internal tail". Because that single register write *is* the launch, it is the highest-value MMIO offset in the whole DMA path, and the kernel **BAR0-write-blocks** it (and the ring base pointers) against userspace `mmap` — `udma_blocked[]` (`neuron_dma.c:869`) lists exactly `{drbp_low, drbp_high, crbp_low, crbp_high, drtp_inc}`, so a userspace-authored ring can never ring its own doorbell directly; it must ask the kernel to do it via an ioctl. That ioctl bridge — `ndl_dma_queue_copy_start` issuing `NEURON_IOCTL_DMA_QUEUE_COPY_START` (`0x80084e23`, nr 35) — is the seam between the two ring "worlds": userspace **authors** descriptors into a vring/pring, the kernel **owns** the doorbell.

This page documents five artifacts a reimplementer must reproduce: (1) the **ring producer/consumer/phase cursors** in `struct udma_q` (`next_desc_idx`, `next_cdesc_idx`, `desc_ring_id`, `size_mask`) and the 16-descriptor guard the wrap math reserves; (2) the **trigger** — `udma_m2m_copy_start` arming RX before TX, and the userspace ioctl bridge that reaches it; (3) the **doorbell** — the `mb()`-then-`reg_write32(drtp_inc, num)` sequence and its BAR0 gate; (4) the **host-marker completion** — the `0xabcdef01` write-back, the 1 µs poll, the per-arch wait budget, and `sync_threshold = 2031`; and (5) the **ack/recycle** — the `udma_cdesc_ack` lock-free CAS that frees descriptor slots. The 16-byte descriptor bitfields are owned by [descriptor-format](descriptor-format.md); the kernel ring struct and its lifecycle are owned by [dma-rings](../kernel/dma-rings.md); the host-side authoring of descriptor arrays is owned by [virtual-rings](virtual-rings.md) — none are re-derived here.

For reimplementation, the contract is:

- **The ring cursors and wrap rule**: `next_desc_idx` (producer) and `next_cdesc_idx` (consumer/completion counter) chase each other modulo `size_mask` (power-of-two ring), a 2-bit `desc_ring_id` (phase) flips on each wrap, and `available = (next_cdesc_idx - (next_desc_idx + 16)) & size_mask` permanently reserves a **16-descriptor guard gap**.
- **The trigger**: stage a TX+RX pair per chunk, append one `0xabcdef01` marker self-copy last, then `udma_m2m_copy_start(qid, m2s_count, s2m_count)` — which doorbells **RX first, then TX**, with `num = data_descs + 1` on both rings.
- **The doorbell**: `mb()` then one 32-bit `reg_write32(&q_regs->rings.drtp_inc, num)` at MMIO offset `+0x38`; reject `num == 0 || num > size`; the register is BAR0-write-blocked from userspace.
- **The host-marker completion**: poll the marker word every 1 µs against `0xabcdef01`; on match reset the marker words and `ack`; on `i > loop` return `-ETIMEDOUT`. The poll budget is a per-arch descriptor-count estimate, ×100000 on qemu/emu, ÷200 for intra-device copies.
- **The recycle**: `udma_cdesc_ack(rxq, count)` then `(txq, count)` CAS-advances `next_cdesc_idx` by `count` (`&size_mask`), which is exactly what `available` reads back as freed.

| | |
|---|---|
| **Cycle** | stage → trigger → doorbell → poll-marker → ack/recycle (five phases, both code lines) |
| **Completion model** | **host-memory marker** `0xabcdef01`, polled — **no hardware completion queue** (`cdesc_base == NULL`) |
| **Completion marker** | `DMA_COMPLETION_MARKER = 0xabcdef01` (`neuron_dma.c:153`); userspace `movl $0xabcdef01` `@0x22df0e` / `@0x22e00a` |
| **Doorbell register** | `rings.drtp_inc` @ **`+0x38`** in the per-queue block; write `num` to launch |
| **Doorbell (kernel)** | `udma_desc_action_add` `udma_main.c:700` — `mb()` `:709`, `reg_write32(drtp_inc, num)` `:710` |
| **Doorbell (userspace)** | `al_udma_desc_action_add` `@0x461f60` — `al_local_data_memory_barrier` `@0x461fc3`, `add $0x38`, `al_reg_write32` `@0x461fd1` |
| **Trigger** | `udma_m2m_copy_start` `udma_m2m.c:416` — RX `:440` then TX `:445`; userspace `al_udma_m2m_copy_start` `@0x45ed60` |
| **ioctl bridge** | `ndl_dma_queue_copy_start` `@0x000c3930` → `0x80084e23` = `DMA_QUEUE_COPY_START` (nr 35) → `ndmar_queue_copy_start` (`neuron_ring.c:309`) |
| **BAR0 gate** | `udma_blocked[] = {drbp_low, drbp_high, crbp_low, crbp_high, drtp_inc}` (`neuron_dma.c:869`); `ndma_bar0_blocked_one_engine` `:872` |
| **Poll** | `ndma_memcpy_wait_for_completion` `neuron_dma.c:230` — 1 µs interval `:248`; userspace `dma_wait_for_completion_handle` `@0x22def0` (10 µs) |
| **Recycle** | `udma_cdesc_ack` `udma.h:452` — CAS `next_cdesc_idx@+0x34`; userspace `lock cmpxchg %edx,0x34(%rbx)` `@0x46fb27` |
| **Flow-control** | `sync_threshold = 4096/2 - 16 - 1 = 2031` (`neuron_dma.c:321`); 16-desc guard gap (`udma.h:365–367`) |

---

## 1. The Ring Cursors and the Wrap Rule

### Purpose

Before any phase of the cycle makes sense, the four software cursors in `struct udma_q` must be pinned: they are the producer index, the consumer/completion counter, the phase bit, and the wrap mask. Every phase reads or writes exactly these fields — stage advances the producer, the doorbell consumes nothing local (the *hardware* advances its own tail), poll observes the marker, and ack advances the consumer. The cursors live in `struct udma_q` (kernel `udma.h:145–172`, `__aligned(64)`); the offsets below are confirmed against the userspace `al_mla_udma_*` field accesses, so kernel and binary agree byte-for-byte.

### The cursors

`struct udma_q` carries the per-queue ring state. The relevant cursors and their byte offsets (the full struct is owned by [udma-main](../kernel/udma-main.md); only the cycle-relevant fields are pinned here):

| Field | Offset | Role | Disasm proof |
|---|---|---|---|
| `size_mask` | `0x00` | `size - 1` wrap mask (pow2 ring) | `and (%rbx)` in cdesc_ack `@0x46fb25` |
| `q_regs` | `0x08` | per-queue MMIO base (the doorbell lives at `+0x38` within it) | `mov 0x8(%rbx),%rbx` in doorbell `@0x461fbf` |
| `desc_base_ptr` | `0x10` | submission-ring base VA | — |
| `next_desc_idx` | `0x1c` | **PRODUCER** index — advanced by `udma_desc_get` | `mov 0x1c(%rdi),%edx` in ring_id_get `@0x46fdd5` |
| `desc_ring_id` | `0x20` | current **PHASE** (2-bit ring id) | `mov 0x20(%rdi)` / `and $0x3` `@0x46fdd8`/`@0x46fde2` |
| `cdesc_base_ptr` | `0x24` | **NULL** on Neuron M2M (no HW CQ) | — |
| `next_cdesc_idx` | `0x34` | **CONSUMER** / completion counter — advanced by `udma_cdesc_ack` | `lock cmpxchg %edx,0x34(%rbx)` `@0x46fb27` |
| `size` | `0x6c` | ring size in descriptors (pow2) | `cmp %ebp,0x6c(%rbx)` in doorbell `@0x461f74` |

> **QUIRK —** `cdesc_base_ptr` (`+0x24`) is **NULL** on every Neuron M2M queue, and `next_cdesc_idx` (`+0x34`) is therefore *not* an index into a hardware completion ring — it is a pure **software counter** the driver advances itself. On a conventional UDMA queue with a completion ring, the hardware writes completion descriptors and the driver reads them; here the engine writes the `0xabcdef01` marker into an ordinary host buffer instead, and `next_cdesc_idx` is bookkeeping that only `udma_cdesc_ack` ever touches. A reimplementer who wires `next_cdesc_idx` to a real CQ head pointer will diverge from the marker model the moment they try to read completions back from device memory that was never written.

### The wrap and guard-gap rule

The submission ring is a power-of-two wraparound ring. The producer (`next_desc_idx`) and consumer (`next_cdesc_idx`) chase each other modulo `size_mask`, and a 2-bit phase ("ring id") flips on each wrap so the hardware can tell a freshly-written descriptor from a stale one left over from the previous lap. The free-slot computation reserves a fixed 16-descriptor gap:

```c
// udma_available_get — udma.h:355-367 (pow2 ring required).
function udma_available_get(q):                          // u32 -> free descriptor slots
    return (q.next_cdesc_idx - (q.next_desc_idx + 16)) & q.size_mask
    //                          ^^^^^^^^^^^^^^^^^^^^^^  UDMA_MAX_NUM_CDESC_PER_CACHE_LINE = 16
    // The +16 permanently reserves a guard gap between producer and consumer:
    // the producer can never catch the consumer to within 16 descriptors.

// udma_ring_id_get — udma.h:423-434. Phase bit for the last-allocated descriptor;
// on a producer wrap (next_desc_idx returned to 0) advance the 2-bit phase.
function udma_ring_id_get(q):                             // models @0x46fdd0 (cayman)
    phase = q.desc_ring_id                                // +0x20, current 2-bit phase
    if q.next_desc_idx == 0:                              // wrapped this allocation
        q.desc_ring_id = (phase + 1) & DMA_RING_ID_MASK   // & 0x3  (@0x46fde2: and $0x3)
    return phase                                          // initial phase = UDMA_INITIAL_RING_ID = 1
```

The guard gap is why the classic stager flushes a batch at **`sync_threshold = DMA_H2T_DESC_COUNT/2 - 16 - 1 = 4096/2 - 16 - 1 = 2031`** data descriptors (`neuron_dma.c:321`): a batch of ≤2031 data descriptors plus its single completion descriptor always fits within half the nominal 4096-descriptor ring, so the producer never overruns the consumer even before any ack arrives.

> **CORRECTION (RING-1) —** `sync_threshold` is computed from the **nominal** `DMA_H2T_DESC_COUNT = 4096`, *not* the allocated ring size. `ndmar_ring_get_desc_count(4096)` actually rounds the allocated descriptor count up to **8192** (the `+16` guard is added *before* the power-of-two round — see [dma-rings §3](../kernel/dma-rings.md#3-descriptor-count-rounding)). The threshold (2031) and the allocated size (8192) are two different numbers and must not be conflated: the flush threshold is deliberately computed against the *logical* half-ring, not the physical one, leaving extra physical headroom.

### Function Map

| Function | Address / Source | Role | Confidence |
|---|---|---|---|
| `udma_available_get` | `udma.h:355` | free-slot count = `(cidx - (pidx + 16)) & size_mask` | CERTAIN |
| `udma_desc_get` | `udma.h:379` | producer: hand out `desc_base_ptr[next_desc_idx]`, advance `&size_mask` | CERTAIN |
| `udma_ring_id_get` | `udma.h:423` / `@0x46fdd0` | phase getter; advance `desc_ring_id` on wrap | CERTAIN |
| `al_mla_udma_ring_id_get_{cayman,mariana,sunda}` | `@0x46fdd0`/`@0x463410`/`@0x469790` | userspace phase getter (field `+0x20`, `&0x3` on `+0x1c == 0`) | HIGH |

---

## 2. Stage — Building the Batch

### Purpose

Staging fills the ring's descriptor memory with the TX+RX pairs for the transfer and appends exactly one **completion descriptor** — the 4-byte host self-copy whose execution writes `0xabcdef01` into the poll slot. This is the producer side of the cursors from §1; it does not touch hardware. (The per-descriptor 16-byte wire format is owned by [descriptor-format](descriptor-format.md); the host-side vring authoring by [virtual-rings](virtual-rings.md). This section documents only the *cycle's* use of staging — the marker append and the batch boundary.)

### Algorithm

```c
// Classic synchronous stager — ndma_memcpy_chunks, neuron_dma.c:311.
// Stages ≤64 KiB data chunks until the transfer is done OR the batch hits sync_threshold,
// then appends the marker descriptor and triggers.
function ndma_memcpy_chunks(eng, ring, ctx):              // neuron_dma.c:311
    pending = 0                                            // = data descs staged this batch
    while ctx.remaining > 0:
        chunk   = min(ctx.remaining, MAX_DMA_DESC_SIZE)    // 0x10000 (len0 trick: 64K -> len 0)
        is_last = (ctx.remaining - chunk == 0)
        barrier = ndma_get_m2m_barrier_type(is_last)       // DHAL: WRITE_BARRIER on last data desc
        // stage ONE tx+rx pair (udma_desc_get rx+tx, udma_ring_id_get phase, build_descriptor)
        ndma_memcpy64k(eng, ring, ctx.src+off, ctx.dst+off, chunk, barrier)  // :289
        ctx.remaining -= chunk; off += chunk; pending += 1
        if is_last or pending == sync_threshold:           // 2031 — flush the batch (:339)
            break

    // append the completion MARKER descriptor — a 4-byte host self-copy
    ndma_memcpy_add_completion_desc(eng, ring, ctx.completion_ptr, BARRIER_NONE)  // :200
    pending += 1                                           // num = data descs + 1 completion

    // TRIGGER (see §3): doorbell both rings with the SAME count
    udma_m2m_copy_start(&eng->udma, ring->qid, pending, pending)   // :369

// The marker append — ndma_memcpy_add_completion_desc, neuron_dma.c:200.
function ndma_memcpy_add_completion_desc(eng, ring, compl_ptr, barrier):
    src = (u32*) compl_ptr                                 // marker source slot @ +0
    dst = (u32*)(compl_ptr + DMA_COMPLETION_MARKER_SIZE)   // poll slot @ +4  (:209)
    WRITE_ONCE(*src, DMA_COMPLETION_MARKER)                // 0xabcdef01  (:213)
    WRITE_ONCE(*dst, 0)                                    // poll slot starts cleared
    // a 4-byte host->host self-copy: executing it copies src(+0) -> dst(+4),
    // i.e. writes 0xabcdef01 into the poll slot the CPU waits on (:218-219)
    udma_m2m_copy_prepare_one(eng, ring->qid, compl_ptr, compl_ptr + 4, 4, barrier, set_int=0)
```

> **NOTE —** the marker is a **4-byte memory-to-memory self-copy**, not a hardware fence or interrupt. The completion buffer holds two `u32` slots: source `+0` is pre-seeded with `0xabcdef01`, destination `+4` is cleared. The marker descriptor is staged *last*, after every data descriptor; because UDMA retires descriptors in order on a ring (and the last data descriptor carries the per-arch write barrier — `WRITE_BARRIER` on V2, `SOW` on V3+), by the time the marker copy executes and `+4` reads `0xabcdef01`, every prior data write has landed. The ordering guarantee is *enforced by the barrier policy*, not assumed; see [descriptor-format §6](descriptor-format.md#6-barrier-modes).

---

## 3. Trigger and Doorbell — Launching the Batch

### Purpose

The trigger converts a staged batch into hardware work. `udma_m2m_copy_start` resolves the TX and RX queue handles and rings each ring's doorbell with the batch count. The doorbell itself (`udma_desc_action_add`) is the single `mb()`-guarded MMIO write that hands `num` descriptors to the engine. This is the one place the cycle touches device registers, and the one register userspace is forbidden to write directly.

### Entry Point

Two paths reach the same doorbell. The kernel-internal path (driver memcpy rings) calls the trigger inline:

```text
ndma_memcpy_chunks (neuron_dma.c:311)              ── stager (§2)
  └─ udma_m2m_copy_start (udma_m2m.c:416)          ── THE TRIGGER
       ├─ udma_desc_action_add(rxq, s2m_count)     ── DOORBELL RX first (:440)
       │    └─ mb(); reg_write32(&rings.drtp_inc, num)   ── udma_main.c:709-710
       └─ udma_desc_action_add(txq, m2s_count)     ── DOORBELL TX second (:445)
```

The userspace-authored path cannot ring the doorbell itself (§3 BAR0 gate); it crosses into the kernel via an ioctl:

```text
[runtime: vring dumped to pring — see virtual-rings.md]
  └─ ndl_dma_queue_copy_start (0x000c3930)         ── packs (eng, qid, tx_cnt, rx_cnt)
       └─ ioctl(fd, 0x80084e23, &args)             ── NEURON_IOCTL_DMA_QUEUE_COPY_START (nr 35)
            └─ [kernel] ncdev_dma_copy_start
                 └─ ndmar_queue_copy_start (neuron_ring.c:309)
                      └─ udma_m2m_copy_start        ── same trigger, same doorbell
```

### Algorithm — the trigger

```c
// udma_m2m_copy_start — udma_m2m.c:416.  Arms RX before TX.
function udma_m2m_copy_start(udma, qid, m2s_count, s2m_count):   // m2s=TX, s2m=RX
    udma_q_handle_get(udma, qid, UDMA_TX, &txq)
    udma_q_handle_get(udma, qid, UDMA_RX, &rxq)
    if m2s_count > txq.size: return -EINVAL                       // :431
    if s2m_count > rxq.size: return -EINVAL                       // :437

    ret = udma_desc_action_add(rxq, s2m_count)                    // :440  RX DOORBELL FIRST
    if ret: return ret
    if m2s_count > 0:                                             // :444  (m2s==0 => RX-prefetch-only)
        ret = udma_desc_action_add(txq, m2s_count)               // :445  THEN TX DOORBELL
    return ret
```

> **QUIRK —** the doorbell ordering is **RX (destination) before TX (source)**, both with `num = data_descs + 1`. The destination ring is armed *before* the source read is launched, so the engine is ready to write the moment the first source bytes arrive. The symmetric ordering on the recycle side is also RX-before-TX (§5). A reimplementer who doorbells TX first introduces a window where the source read can outrun the unarmed destination ring. The `m2s_count == 0` case is a deliberate **RX-prefetch-only** trigger (arm the destination ring ahead of a later source-side launch).

### Algorithm — the doorbell

```c
// udma_desc_action_add — udma_main.c:700.  THE DOORBELL.  Models @0x461f60 (userspace).
function udma_desc_action_add(q, num):
    if num == 0 or num > q.size: return -1                 // :704  (userspace: cmp %ebp,0x6c(%rbx))
    addr = &q.q_regs->rings.drtp_inc                       // :708  per-queue MMIO + 0x38
    mb()                                                   // :709  publish descriptors before the write
    reg_write32(addr, num)                                 // :710  HW now fetches `num` descriptors
    return 0
```

The userspace twin (`al_udma_desc_action_add` `@0x461f60`) is byte-identical in shape, confirmed in disassembly:

```asm
461f74:  cmp    %ebp,0x6c(%rbx)            ; num <= q->size  (size @ +0x6c)
461f77:  jae    461fbf                     ; ok
461fbf:  mov    0x8(%rbx),%rbx             ; rbx = q->q_regs   (+0x8)
461fc3:  call   al_local_data_memory_barrier   ; mb()
461fca:  add    $0x38,%rbx                 ; rbx = &q_regs->rings.drtp_inc  (+0x38)
461fd1:  call   al_reg_write32             ; *drtp_inc = num   ; THE DOORBELL
```

> **QUIRK —** the `mb()` (kernel) / `al_local_data_memory_barrier` (userspace) **before** the `drtp_inc` write is not optional — it is the ordering hinge of the whole cycle. The kernel comment is explicit (`udma_main.c:709`: *"to make sure data written to the descriptors will be visible to the DMA"*). The barrier ensures every descriptor byte the CPU just wrote into ring memory is globally visible before the register write that tells the engine to fetch them. Drop it and the engine can fetch a half-written descriptor. The barrier orders **descriptor writes → doorbell write**; it is a separate concern from the per-descriptor `WRITE_BARRIER`/`SOW` that orders **data writes → marker write** (§2).

### The BAR0 doorbell gate

The doorbell register, the ring base pointers, and the completion-ring base are all **write-blocked** against userspace BAR0 `mmap`. This is what forces the ioctl bridge: a userspace process that maps the device BAR0 cannot write `drtp_inc` to launch its own ring.

```c
// neuron_dma.c:869-871 — the blocked per-queue register offsets.
static const u64 udma_blocked[] = {
    offsetof(struct udma_rings_regs, drbp_low),   // ring base ptr [31:4]
    offsetof(struct udma_rings_regs, drbp_high),  // ring base ptr [63:32]
    offsetof(struct udma_rings_regs, crbp_low),   // completion ring base [31:4]
    offsetof(struct udma_rings_regs, crbp_high),  // completion ring base [63:32]
    offsetof(struct udma_rings_regs, drtp_inc),   // *** THE DOORBELL ***  (+0x38)
};

// ndma_bar0_blocked_one_engine — neuron_dma.c:872.  For every queue, both m2s & s2m:
function ndma_bar0_blocked_one_engine(base, off):
    for q in queues:
        q_off = per-queue block base
        for blocked in udma_blocked[]:            // :889
            if off == q_off + blocked:            // :890
                return BLOCKED                    // reject the userspace BAR0 write
    return ALLOWED
```

> **GOTCHA —** the security boundary is precisely the doorbell plus the ring base pointers — five offsets, not the whole register block. A reimplementer of the kernel BAR0 mmap handler who blocks only the doorbell leaves the ring base pointers writable (a userspace process could repoint a queue at arbitrary memory); one who blocks the whole block breaks legitimate status/head-pointer reads. The list is exactly `{drbp_low, drbp_high, crbp_low, crbp_high, drtp_inc}` for *every* queue of *both* directions. This is why userspace authors descriptors but the kernel owns the launch — the ioctl bridge (`0x80084e23`) is the only sanctioned path to `drtp_inc`.

### Function Map

| Function | Address / Source | Role | Confidence |
|---|---|---|---|
| `udma_m2m_copy_start` | `udma_m2m.c:416` | trigger; RX-then-TX doorbell, `num` per ring | CERTAIN |
| `al_udma_m2m_copy_start` | `@0x45ed60` | userspace trigger; same RX/TX doorbell pair | HIGH |
| `udma_desc_action_add` | `udma_main.c:700` | doorbell; `mb()` + `reg_write32(drtp_inc, num)` | CERTAIN |
| `al_udma_desc_action_add` | `@0x461f60` | userspace doorbell; `add $0x38` + `al_reg_write32` | HIGH |
| `ndl_dma_queue_copy_start` | `@0x000c3930` | userspace→kernel trigger bridge; ioctl `0x80084e23` (nr 35) | HIGH |
| `ndmar_queue_copy_start` | `neuron_ring.c:309` | kernel ioctl handler → `udma_m2m_copy_start` | CERTAIN |
| `ndma_bar0_blocked_one_engine` | `neuron_dma.c:872` | rejects userspace BAR0 writes to the 5 blocked offsets | CERTAIN |

---

## 4. Poll — Host-Marker Completion

### Purpose

With no hardware completion queue, completion is observed by **polling the host marker word**. After the doorbell, the CPU sleeps a per-arch first-wait estimate, then reads the poll slot every 1 µs until it equals `0xabcdef01` (success) or the loop budget expires (`-ETIMEDOUT`). On success it resets the marker words and calls ack to recycle. The poll budget is the only knob with real per-arch and per-platform variation.

### Algorithm

```c
// ndma_memcpy_wait_for_completion — neuron_dma.c:230.
function ndma_memcpy_wait_for_completion(eng, ring, count, ptr, async, is_d2d):
    // (1) per-arch wait estimate: V2 est = 4*(count-1); V3 est = 2*(count-1) (dhal_v{2,3}.c)
    ndma_get_wait_for_completion_time(count, async, &first_wait, &wait)  // :238  DHAL vtable
    if narch_is_qemu() or narch_is_emu():
        wait = wait * 100 * 1000                            // :241  virtual platforms: ×100000
    if is_d2d and not async:                                // :244  intra-device copies are fast
        first_wait = 10                                     //       fixed 10 µs first wait
        wait = wait / 200                                   // :245  much shorter budget
    one_loop_sleep = 1                                      // :248  poll every 1 µs
    loop = wait / one_loop_sleep + 1                        // :249

    dst = (volatile u32*)(ptr + DMA_COMPLETION_MARKER_SIZE) // :251  poll slot @ +4
    src = (volatile u32*) ptr                               //       marker source @ +0

    // (2) poll
    udelay(first_wait)                                      // :263
    for i in 0 .. loop:                                     // :264
        if READ_ONCE(*dst) == DMA_COMPLETION_MARKER:        // :266-268  0xabcdef01 -> all descs retired
            WRITE_ONCE(*dst, 0)                             // :269  re-arm: clear poll slot
            WRITE_ONCE(*src, DMA_COMPLETION_MARKER)         // :270  re-seed marker source
            ndma_ack_completed_desc(eng, ring, count)       // :274  RECYCLE (§5)
            return 0
        udelay(one_loop_sleep)                              // 1 µs
    return -ETIMEDOUT                                       // i > loop: timeout
```

> **QUIRK —** the marker is **polled, never interrupt-driven**, even though the descriptor format has an `INT_EN` bit. The driver chooses a tight 1 µs busy-poll with a descriptor-count-scaled budget because a small host↔device copy completes in microseconds and an interrupt round-trip would dominate. The per-arch `est` (V2 `4*(count-1)`, V3 `2*(count-1)`) reflects per-descriptor latency; the budget is then inflated ×100000 on qemu/emu (the marker can take milliseconds under emulation) and divided by 200 for intra-device (`d2d`) copies (no PCIe round-trip). See [iofic](iofic.md) for why completion is polled, not interrupt-driven.

### The userspace software poll (qemu / emulation)

A third, non-hardware path exists in userspace for qemu/emulation: it copies descriptors via `dmem_buf_copyin` and polls the *same* `0xabcdef01` marker via `dmem_buf_copyout`, proving the marker model is shared end-to-end. The marker constant and the reset-on-match are byte-identical to the kernel:

```c
// dma_wait_for_completion_handle — @0x22def0 (userspace software/qemu poll).
function dma_wait_for_completion_handle(handle, ...):
    *(u32*)(rsp+0xc) = 0xabcdef01                          // @0x22df0e  stage marker source
    *(u32*)(rsp+0x8) = 0                                   // @0x22df06  clear poll slot
    // platform branch: emu (cmp $2) inflates the loop budget ×0x2710 (10000); cmp $1 ...
    for i in 0 .. N:                                       // N = platform-scaled desc-count estimate
        v = dmem_buf_copyout(poll_slot, 4)                 // @0x22dfa0  read marker word back
        if v == 0xabcdef01:                                // @0x22dfa9  cmp $0xabcdef01,%eax
            // reset (copyin 0 / 0xabcdef01) and return success
            return 0
        usleep(10)                                         // @0x22dfb9  10 µs poll interval
    return NRT_TIMEOUT                                     // 5
    // strings: "DMA completed %u descriptors" / "Error: DMA completion timeout in: %lu usec"
```

> **NOTE —** the userspace poll interval is **10 µs** (`usleep(10)` `@0x22dfb9`), versus the kernel's 1 µs (`one_loop_sleep` `:248`); both poll the identical `0xabcdef01` marker against the identical host word. The exact iteration budget `N` in the userspace handle is a platform-scaled descriptor-count estimate (`imul $0x190` = ×400, a magic-divide by 10, then a platform multiplier `0x2710` on emu) — the *scaling* is MEDIUM confidence, but the *marker semantics* (write `0xabcdef01`, poll, reset, timeout) are HIGH and identical on both code lines. Real-HW exec rings instead signal completion via an embedded semaphore on the last RX descriptor ([virtual-rings §5](virtual-rings.md#5-post-authoring-edits--pack-barrier-embedded-sem-rewrite)); the host-marker poll documented here covers the kernel's driver-internal copies and the qemu/emulation fallback.

### Function Map

| Function | Address / Source | Role | Confidence |
|---|---|---|---|
| `ndma_memcpy_wait_for_completion` | `neuron_dma.c:230` | kernel poll; 1 µs interval, per-arch budget, reset + ack on match | CERTAIN |
| `ndma_get_wait_for_completion_time_v2` | `neuron_dhal_v2.c:989` | V2 budget: `est = 4*(count-1)` | HIGH |
| `ndma_get_wait_for_completion_time_v3` | `neuron_dhal_v3.c:1293` | V3 budget: `est = 2*(count-1)` | HIGH |
| `dma_wait_for_completion_handle` | `@0x22def0` | userspace software/qemu poll; same `0xabcdef01` marker | HIGH (budget MED) |
| `dma_alloc_completion_handle` | `@0x22de20` | alloc 8-byte dmem completion slot | HIGH |

---

## 5. Ack and Recycle — Freeing the Slots

### Purpose

Without a hardware completion ring, the descriptor slots a retired batch occupied are *not* automatically reclaimed — the producer would eventually run out of slots. On a successful poll, the driver advances the software completion counter `next_cdesc_idx` by the batch count, which is exactly the value `udma_available_get` subtracts to report free slots. The advance is a lock-free CAS, identical on both code lines.

### Algorithm

```c
// ndma_ack_completed_desc — neuron_dma.c:36.  Called from the poll on marker match (:274).
function ndma_ack_completed_desc(eng, ring, count):
    udma_q_handle_get(&eng->udma, ring->qid, UDMA_TX, &txq)
    udma_q_handle_get(&eng->udma, ring->qid, UDMA_RX, &rxq)
    udma_cdesc_ack(rxq, count)                              // recycle RX descriptors FIRST
    udma_cdesc_ack(txq, count)                              // then TX  (mirrors RX-first doorbell)

// udma_cdesc_ack — udma.h:452.  CAS-advance next_cdesc_idx by `num`.  Models @0x46fb00.
function udma_cdesc_ack(q, num):
    if not is_power_of_2(q.size): return -EINVAL            // pow2 required (size @ +0x6c)
    do:
        old = q.next_cdesc_idx                              // +0x34
        new = (old + num) & q.size_mask                     // &0x3fff... (size_mask @ +0x0)
    while not __sync_bool_compare_and_swap(&q.next_cdesc_idx, old, new)  // udma.h:465; cpu_relax()
    return 0
```

The userspace twin (`al_mla_udma_cdesc_ack_cayman` `@0x46fb00`) is the same lock-free CAS on the same field, confirmed in disassembly:

```asm
46fb0b:  mov    0x6c(%rdi),%eax        ; size       (+0x6c)
46fb17:  lea    -0x1(%rax),%edx        ; size_mask = size - 1
46fb1e:  mov    0x34(%rbx),%eax        ; old = next_cdesc_idx   (+0x34)
46fb21:  lea    0x0(%rbp,%rax,1),%edx  ; old + num
46fb25:  and    (%rbx),%edx            ; & size_mask   (size_mask @ +0x0)
46fb27:  lock cmpxchg %edx,0x34(%rbx)  ; CAS next_cdesc_idx   ; THE RECYCLE
```

> **QUIRK —** the recycle is a **lock-free CAS** (`__sync_bool_compare_and_swap` / `lock cmpxchg`), not a plain store, because the completion counter can be advanced concurrently — the async double-buffered path (`ndma_memcpy_mc_async`) and the zero-copy completion kthread both ack independently. After the CAS, `udma_available_get` (§1) immediately reports the `count` slots as free, *minus* the permanent 16-descriptor guard gap. The RX-before-TX ack ordering mirrors the RX-before-TX doorbell ordering (§3) — the same directional discipline on both ends of the cycle.

### Function Map

| Function | Address / Source | Role | Confidence |
|---|---|---|---|
| `ndma_ack_completed_desc` | `neuron_dma.c:36` | poll-side recycle: cdesc_ack RX then TX | CERTAIN |
| `ndmar_ack_completed` | `neuron_ring.c:272` | public ioctl passthrough (`DMA_ACK_COMPLETED`) | CERTAIN |
| `udma_cdesc_ack` | `udma.h:452` | CAS-advance `next_cdesc_idx` by `count` (`&size_mask`) | CERTAIN |
| `al_mla_udma_cdesc_ack_{cayman,mariana,sunda}` | `@0x46fb00`/`@0x463140`/`@0x4694c0` | userspace recycle; `lock cmpxchg %edx,0x34(%rbx)` | HIGH |

---

## 6. The Full Cycle End-to-End

The five phases compose into one loop per batch. The classic synchronous H2T memcpy (kernel `neuron_dma.c`) is the canonical instance; the userspace-authored path differs only in that the doorbell is reached through the ioctl bridge (§3), and the real-HW exec path differs only in that completion is an embedded semaphore rather than the host marker (§4).

```text
                       ┌──────────────────────────────────────────────────────┐
                       │  per BATCH (≤ sync_threshold = 2031 data descs + 1)   │
                       └──────────────────────────────────────────────────────┘
   STAGE      ndma_memcpy_chunks (dma.c:311)
   (§2)         loop: ndma_memcpy64k -> udma_m2m_copy_prepare_one
                        udma_desc_get(rx)+(tx)   ── advance next_desc_idx (+0x1c)
                        udma_ring_id_get          ── phase bit (+0x20, &0x3)
                      append marker self-copy: WRITE_ONCE(src,0xabcdef01), copy +0 -> +4
                      pending = data_descs + 1
                                   │
                                   ▼
   TRIGGER    udma_m2m_copy_start(qid, pending, pending)    (m2m.c:416)
   (§3)         │  [userspace: ndl_dma_queue_copy_start -> ioctl 0x80084e23 -> ndmar_queue_copy_start]
                ▼
   DOORBELL   udma_desc_action_add(rxq, pending)   ── RX FIRST  (m2m.c:440)
   (§3)         mb(); reg_write32(&rings.drtp_inc, pending)   ── +0x38  (main.c:709-710)
              udma_desc_action_add(txq, pending)   ── THEN TX  (m2m.c:445)
                ▼   [BAR0-blocked from userspace: udma_blocked[] dma.c:869]
              ═══ HW fetches `pending` descriptors from its internal tail, executes in order ═══
                ▼   (last data desc carries WRITE_BARRIER/SOW; marker copy runs last)
   POLL       ndma_memcpy_wait_for_completion (dma.c:230)
   (§4)         udelay(first_wait);  every 1 µs:  READ_ONCE(*(ptr+4)) == 0xabcdef01 ?
                  yes → WRITE_ONCE(*(ptr+4),0); WRITE_ONCE(*ptr,0xabcdef01)   ── re-arm
                  no  → udelay(1);  i > loop → -ETIMEDOUT
                ▼ (marker matched)
   ACK /      ndma_ack_completed_desc (dma.c:36)
   RECYCLE      udma_cdesc_ack(rxq, count)  ── CAS next_cdesc_idx += count   ── RX FIRST
   (§5)         udma_cdesc_ack(txq, count)  ──  (+0x34, &size_mask)          ── THEN TX
                ▼
              udma_available_get now reports `count` slots free (minus 16-desc guard)
                │
                └──► more remaining? restage next batch (offset/remaining advanced)  else done
```

> **NOTE —** the async double-buffered variant (`ndma_memcpy_mc_async`, `neuron_dma.c:586`) pipelines the loop: it stages-and-triggers batch *N+1* while polling the completion of batch *N* (overlapping stage with completion), using three context slots (`SYNC`/`ASYNC1`/`ASYNC2`). The zero-copy path (`ndma_build_n_issue_zc_descs`) and the async H2D completion-queue kthread are further variations on the same five phases — the cycle shape is invariant; only the overlap and the completion-signalling change. Those overlap state machines are owned by the kernel DMA op layer ([dma-op-layer](../kernel/dma-op-layer.md)).

---

## 7. Userspace ↔ Kernel Agreement

The cycle is authored twice — userspace KaenaHal (`libnrt.so`) and kernel DKMS — and the core primitives agree byte-for-byte:

| Aspect | Userspace | Kernel | Agree |
|---|---|---|---|
| completion marker | `movl $0xabcdef01` `@0x22df0e`/`@0x22e00a` | `DMA_COMPLETION_MARKER 0xabcdef01` (`:153`) | **yes** |
| marker poll-on-`==0xabcdef01` | `cmp $0xabcdef01,%eax` `@0x22dfa9` | `READ_ONCE(*dst)==MARKER` (`:266`) | **yes** |
| doorbell register | `add $0x38` (drtp_inc) `@0x461fca` | `&q_regs->rings.drtp_inc` (`:708`) | **yes** (`+0x38`) |
| `mb()` before write | `al_local_data_memory_barrier` `@0x461fc3` | `mb()` (`:709`) | **yes** |
| doorbell guard | `cmp %ebp,0x6c(%rbx)` (num≤size) `@0x461f74` | `num==0 \|\| num>size` (`:704`) | **yes** |
| doorbell order | RX then TX `@0x45ed60` | RX (`:440`) then TX (`:445`) | **yes** |
| recycle CAS field | `lock cmpxchg %edx,0x34(%rbx)` `@0x46fb27` | `__sync_bool_compare_and_swap(&next_cdesc_idx)` (`:465`) | **yes** (`+0x34`) |
| phase wrap | `lea 0x1(%rax); and $0x3` on `+0x20` `@0x46fde2` | `(id+1) & DMA_RING_ID_MASK` (`udma.h:423`) | **yes** |
| poll interval | `usleep(10)` `@0x22dfb9` | `udelay(1)` (`:248`) | **interval differs**, marker identical |

> **Verification —** the doorbell offset (`+0x38`), the `mb()`-then-write ordering, the RX-first launch order, the `0xabcdef01` marker, and the `next_cdesc_idx@+0x34` CAS recycle are each confirmed *independently* in the kernel C source and the `libnrt.so` disassembly. The only intentional divergence is the poll interval (kernel 1 µs, userspace software path 10 µs) — a tuning difference, not a protocol difference; both poll the identical host word for the identical sentinel.

---

## 8. Considerations

- **No HW completion queue is the defining property.** Every queue carries `cdesc_base == NULL`; `next_cdesc_idx` is a software counter, and `udma_cdesc_ack` is the only mutator. A reimplementer who provisions a real completion ring will find the engine never writes it on this path, and the marker poll never matches a CQ it isn't fed.
- **The doorbell write is irrevocable and order-critical.** Once `drtp_inc` is written, `num` descriptors are in flight; the `mb()` must precede it and the descriptors must be fully written. The RX-before-TX order is a hardware requirement, not a convenience.
- **The poll budget is the soft edge.** The per-arch `est` formulas and the qemu/emu and d2d scaling are the most version-sensitive numbers on this page; a reimplementer should treat the *shape* (count-scaled estimate, 1 µs poll, reset-on-match, `-ETIMEDOUT`) as the contract and re-derive the constants per generation.
- **The drhp/drtp hardware pointers are off the data path.** `drtp_inc` (`+0x38`) is write-to-increment; `drtp` (`+0x3c`) is the absolute tail; `drhp` (`+0x34` in the *MMIO* block, distinct from the SW `next_cdesc_idx`) is the HW-advanced head, read only by `ndmar_queue_read_state` for introspection. The data path never reads `drhp` — completion is observed solely via the host marker. The exact HW timing of `drhp` advance relative to the marker write is inferred from in-order retirement plus the barrier policy (LOW confidence on the cycle-accurate timing; the *ordering guarantee* itself is grounded in the `WRITE_BARRIER`/`SOW` policy).
- **The embedded-semaphore completion of real-HW exec rings is a separate signal**, set by `vring_set_embedded_semaphore_inc_on_last_desc` ([virtual-rings §5](virtual-rings.md#5-post-authoring-edits--pack-barrier-embedded-sem-rewrite)); its 6-byte CSR blob wire format is not characterized here. The host-marker poll covers the kernel's driver-internal copies and the qemu/emulation fallback.

---

## Related Components

| Name | Relationship |
|---|---|
| `udma_m2m_copy_start` (kernel) / `al_udma_m2m_copy_start` (userspace) | The trigger; arms RX then TX |
| `udma_desc_action_add` / `al_udma_desc_action_add` | The doorbell; `mb()` + `reg_write32(drtp_inc, num)` |
| `ndma_memcpy_wait_for_completion` / `dma_wait_for_completion_handle` | The host-marker poll (kernel 1 µs / userspace 10 µs) |
| `udma_cdesc_ack` / `al_mla_udma_cdesc_ack_*` | The recycle CAS on `next_cdesc_idx` |
| `ndl_dma_queue_copy_start` (ioctl `0x80084e23`) | The userspace→kernel trigger bridge to the BAR0-blocked doorbell |
| `udma_blocked[]` / `ndma_bar0_blocked_one_engine` | The BAR0 write-block that forces the ioctl bridge |

## Cross-References

- [The 16-Byte UDMA Descriptor](descriptor-format.md) — the static wire format of the descriptors this cycle stages and launches; owns `len_ctrl`/`meta_ctrl`, the barrier-mode bit positions, and the len0 trick
- [Virtual Rings (vring) and Packet Builders](virtual-rings.md) — the host-side authoring of the descriptor arrays this cycle doorbells, and the dump from vring to the physical pring
- [DMA Rings and H2T Queues](../kernel/dma-rings.md) — the kernel ring container, the NULL-completion-ring program (`udma_m2m_init_queue`), and the descriptor-count rounding that feeds `sync_threshold`
- [The Completion Engine (NQ Harvest & Error Decode)](../exec/completion-engine.md) — the execution-engine completion path for model exec, contrasted with this DMA-internal marker poll
- [IOFIC Interrupt Model](iofic.md) — why completion is polled rather than interrupt-driven, even though the descriptor has an `INT_EN` bit
- [back to index](../index.md)
