# DMA & Descriptor Engine — Part Map

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME `libnrt.so.1`, ELF64, **not stripped**, DWARF present, KaenaHal-2.31.0.0 `al_hal_udma_m2m.c` statically linked; `.text` VMA == file offset, so every `0x45…`/`0x31…`/`0x22…` is an analysis VMA). Kernel citations are `file:line` into `aws-neuronx-dkms 2.27.4.0` (GPL-2.0), chiefly `udma/udma_m2m.c`, `udma/udma_main.c`, `udma/udma.h`, and `neuron_dma.c`.*
> *Evidence grade: **Confirmed (byte- and source-anchored)** — the descriptor, the doorbell, the marker and the recycle CAS are each authored by two independent code lines (userspace KaenaHal + kernel DKMS) and reconciled bit-for-bit on the deep pages. This page is the **section map**: it links the six Part-VIII pages and does not re-derive their byte-level material. Other versions will differ. · Part VIII — DMA & Descriptor Engine · [back to index](../index.md)*

## Abstract

This page is the **map of the Neuron DMA stack** — the AnnapurnaLabs/Alpine **UDMA** memory-to-memory copy engine as it is driven from both sides of the `libnrt.so` ↔ DKMS boundary. Every byte that moves between host and a NeuronCore, between two device regions, or between two host buffers, moves as a UDMA copy: a model's compiled DMA program, a collective's reduction stream, a driver-internal `memcpy`, and a `memset` are all the same primitive underneath. The whole subsystem is best understood as a textbook **scatter-gather DMA engine** with three Alpine-specific decisions that this Part documents to reimplementation accuracy.

First, there is **one wire format**: a fixed **16-byte hardware descriptor** (`union udma_desc`) that is authored *byte-for-byte identically* by two code lines that never share source — the userspace KaenaHal (`al_hal_udma_m2m.c`, builders at `0x45cac0`–`0x45cda0`) and the kernel DKMS (`udma_m2m.c`, builders at `:261`/`:286`/`:323`). A copy is never one descriptor: it is always a **pair** — an **m2s / Tx** descriptor that reads the *source*, placed on the engine's TX ring, and an **s2m / Rx** descriptor that writes the *destination*, placed on the RX ring of the same queue. Second, completion is **not interrupt-driven and has no hardware completion queue** on the production M2M path (`cdesc_base == NULL` on every Neuron M2M queue). Instead a 4-byte self-copy descriptor appended after the data descriptors writes the sentinel `0xabcdef01` into a host buffer, and the CPU **polls that one word**; when it flips, every prior descriptor is retired. Third, the runtime **authors descriptors in plain host memory first** — a paged, growable **virtual ring** (`vring`) it can edit after the fact — and only later *dumps* the finished program into a physical device ring, whose doorbell the kernel (not userspace) owns.

The six pages of this Part split along the static-versus-dynamic seam. The static half is the **wire format**: the [16-byte descriptor](descriptor-format.md) and its [meta-control overlays](meta-ctrl-overlays.md). The dynamic half is the **runtime cycle**: the [ring / trigger / doorbell / completion cycle](ring-cycle.md), the host-side [virtual rings](virtual-rings.md) that author descriptor arrays before they enter that cycle, and the [IOFIC](iofic.md) interrupt model — which exists, is initialized, and is then *deliberately not used for completion*. This map gives the unified frame, the at-a-glance anchors, the end-to-end cycle diagram, and the userspace ↔ kernel agreement, then hands each artifact to its owning page.

For reimplementation, the contract of the whole subsystem is:

- **One 16-byte descriptor, two code lines, byte-identical.** `{len_ctrl, meta_ctrl/—, buf_ptr}` (16 B, `packed, aligned(16)`); a copy emits a paired Tx (source) + Rx (destination) descriptor on two rings of one queue; `length == 0` encodes a full 64 KiB transfer. Owned by [descriptor-format](descriptor-format.md).
- **Completion by host-memory marker, not interrupt.** A `0xabcdef01` 4-byte self-copy is staged last; the CPU busy-polls it; on match it advances a software completion counter to recycle slots. No hardware completion ring is provisioned. Owned by [ring-cycle](ring-cycle.md).
- **The doorbell is one BAR0-blocked register.** A single `mb()`-guarded 32-bit MMIO write of `num` to `rings.drtp_inc` at `+0x38` launches a batch; userspace cannot write it — it reaches it only through an ioctl bridge. Owned by [ring-cycle](ring-cycle.md).
- **Author-then-commit.** Descriptors are built into an editable host-side `vring`, coalesced/barriered/relocated, then *dumped* (with no-op padding) into a static device `pring`. Owned by [virtual-rings](virtual-rings.md).
- **Arch is policy, not format.** SUNDA (v2) / CAYMAN (v3) / MARIANA (v4) differ only in capability gates and queue tuning; not one bit position moves.

| | |
|---|---|
| **Engine** | AnnapurnaLabs/Alpine **UDMA** memory-to-memory (M2M) copy engine |
| **Descriptor** | `union udma_desc` — 16 B, `__attribute__((packed, aligned(16)))`, `udma/udma.h:38–58` |
| **Copy unit** | a **pair**: m2s/Tx (reads source) + s2m/Rx (writes destination), one queue |
| **Word map** | `len_ctrl` @0x0 · `meta_ctrl` @0x4 (Tx only) · `buf_ptr` @0x8 (64-bit) |
| **Default `meta_ctrl`** | `0x01080003` — byte-identical both code lines |
| **Length limit** | `MAX_DMA_DESC_SIZE = 65536` (`udma.h:479`); `length == 0` ⇒ 65536 |
| **Completion model** | **host-memory marker** `0xabcdef01`, polled — **no hardware completion queue** (`cdesc_base == NULL`) |
| **Doorbell register** | `rings.drtp_inc` @ **`+0x38`** in the per-queue MMIO block; write `num` to launch |
| **Trigger order** | RX (destination) before TX (source), both with `num = data_descs + 1` |
| **Recycle** | `udma_cdesc_ack` CAS-advances `next_cdesc_idx` (`+0x34`); userspace `lock cmpxchg %edx,0x34(%rbx)` `@0x46fb27` |
| **Ring geometry** | pow2 wraparound, 2-bit phase, 16-descriptor guard gap; classic flush at `sync_threshold = 2031` |
| **Host-side authoring** | `vring` (paged 16-byte-desc list) → `pring` (device DRAM static ring); `tdrv/vring.c` |
| **Engines / queues** | `NUM_DMA_ENG_PER_DEVICE = 132` (V3/V4) × up to 16 queues per engine |

---

## 1. The Two Halves and How They Agree

The defining fact of this subsystem is that the DMA wire format and its runtime cycle are implemented **twice, independently**, and reconciled to byte accuracy. A reimplementer who builds only one side and assumes the other matches is, in this case, *correct* — and this Part is the proof of why.

### The userspace half — KaenaHal in `libnrt.so`

The runtime authors descriptors entirely in host memory. The executor's pseudo-DMA translator and the collective `encd` emitter build descriptor programs into a **`vring`** (`tdrv/vring.c`), each copy delegated to the Alpine HAL builder `al_udma_m2m_build_copy_descriptor` (`0x45cca0`) that packs the 16-byte pair. The finished vring is **dumped** into a physical `pring` (`vring_dump_to_pring_descriptors_padded`, `0x311f10`) bound to device DRAM, padded to a fixed count for a static execution plan. Userspace then asks the kernel to launch it: `ndl_dma_queue_copy_start` (`0x000c3930`) issues the ioctl `0x80084e23` (`NEURON_IOCTL_DMA_QUEUE_COPY_START`, nr 35). Userspace **authors**; it cannot doorbell.

### The kernel half — DKMS `udma/`

The kernel both (a) services that ioctl — `ndmar_queue_copy_start` (`neuron_ring.c:309`) → `udma_m2m_copy_start` (`udma_m2m.c:416`) → the doorbell — and (b) runs its own H2T memcpy rings for driver-internal host↔device copies, fully end-to-end (`neuron_dma.c`: stage at `:311`, trigger at `:369`, poll at `:230`, ack at `:36`). The kernel builds the same 16-byte descriptor (`udma_m2m_build_descriptor`, `:323`), rings the same `drtp_inc` doorbell (`udma_desc_action_add`, `udma_main.c:700`), polls the same `0xabcdef01` marker, and recycles via the same `udma_cdesc_ack` CAS (`udma.h:452`).

### Why the kernel owns the doorbell

The seam between the two halves is a security boundary. The doorbell register `drtp_inc` and the ring base pointers are **BAR0-write-blocked** against userspace `mmap`: `udma_blocked[] = {drbp_low, drbp_high, crbp_low, crbp_high, drtp_inc}` (`neuron_dma.c:869`), enforced per queue, both directions, by `ndma_bar0_blocked_one_engine` (`:872`). A userspace process that maps the device BAR0 can read status and head pointers but can neither repoint a ring nor ring its own doorbell — so it must cross into the kernel via the ioctl bridge. This is exactly why "userspace authors, kernel launches" is structural, not stylistic.

> **NOTE —** the two halves are not a producer/consumer pair authoring different formats — they author the *same* format. For the copy descriptor pair, a kernel-built and a userspace-built descriptor with the same `(src, dst, size, phase, barrier)` are bit-for-bit identical (full field-by-field reconciliation in [descriptor-format §8](descriptor-format.md#8-userspace--kernel-byte-agreement) and [ring-cycle §7](ring-cycle.md#7-userspace--kernel-agreement)). The userspace HAL merely *exercises more of the format* — `CONCAT`, `META`, the notification-mask, the CCE/transpose op-classes — which the kernel M2M path never sets because it only ever emits plain copy pairs.

### The agreement at a glance

The four primitives every page leans on, and where each side proves the offset/constant:

| Primitive | Userspace (`libnrt.so`) | Kernel (DKMS) | Agree |
|---|---|---|---|
| 16-byte descriptor | `al_copy_descriptor` = `memcpy` (`0x265980`) | `sizeof(union udma_desc) = 16` (`udma.h:38`) | **yes** |
| `meta_ctrl` default | `0x01080003` @`0x9e8828` | `0x01080003` (`udma_m2m.c:53–79`) | **yes** |
| doorbell register | `add $0x38` (drtp_inc) `@0x461fca` | `&q_regs->rings.drtp_inc` (`udma_main.c:708`) | **yes** (`+0x38`) |
| `mb()` before doorbell | `al_local_data_memory_barrier` `@0x461fc3` | `mb()` (`udma_main.c:709`) | **yes** |
| trigger order | RX then TX `@0x45ed60` | RX (`:440`) then TX (`:445`) | **yes** |
| completion marker | `movl $0xabcdef01` `@0x22df0e` | `DMA_COMPLETION_MARKER 0xabcdef01` (`neuron_dma.c:153`) | **yes** |
| recycle CAS field | `lock cmpxchg %edx,0x34(%rbx)` `@0x46fb27` | `__sync_bool_compare_and_swap(&next_cdesc_idx)` (`udma.h:465`) | **yes** (`+0x34`) |
| phase wrap | `lea 0x1(%rax); and $0x3` `@0x46fde2` | `(id+1) & DMA_RING_ID_MASK` (`udma.h:423`) | **yes** |

---

## 2. The Static Wire Format (at a Glance)

The two static-half pages own every bit; this section gives only the shape so the dynamic-half pages make sense. The full derivation, the byte proofs, and the per-arch table live in [descriptor-format](descriptor-format.md) and [meta-ctrl-overlays](meta-ctrl-overlays.md) — **this map does not reproduce the bitfields**.

```text
union udma_desc  — 16 bytes, packed, aligned(16)               (udma.h:38-58)
  +0x0  u32 len_ctrl   length[15:0] · ring-id/phase[25:24] · FIRST/LAST/CONCAT/DMB/SOW/INT/META flags
  +0x4  u32 meta_ctrl  Tx only: SDMA op-class · CRC/endian · write-barrier   (Rx: buf2_ptr_lo, = 0)
  +0x8  u64 buf_ptr    Tx = source physical addr   ·   Rx = destination physical addr
```

Three facts a reader needs to follow the cycle:

- **The pair.** One copy = one Tx (source) on the TX ring + one Rx (destination) on the RX ring of the same queue. The Tx descriptor's `meta_ctrl` carries the SDMA op-class; on the Rx descriptor that slot is an unused `buf2_ptr_lo`, left zero.
- **`length == 0 ⇒ 65536`.** The 16-bit length cannot express a full 64 KiB transfer, so `0` is overloaded to mean 65536 (the "len0 trick"). Transfers larger than 64 KiB are split into a `FIRST … CONCAT … LAST` chain by the packing layer, not expressed in one descriptor.
- **Four ordering modes, three bit locations.** `NONE` / `DMB` (Tx `len_ctrl` bit 30) / `WRITE_BARRIER` (Tx `meta_ctrl` bit 26) / `SOW` (Rx `len_ctrl` bit 29, V3+). The write-barrier at `meta_ctrl` bit 26 is the single most error-prone position in the format — see the in-place correction on [descriptor-format §3](descriptor-format.md#overlay-b--struct-sdma_cme_desc_word-and-the-write-barrier).

> **GOTCHA —** the same constant means two things depending on the word it sits in. `0x04000000` (bit 26) is `FIRST` in `len_ctrl` but `write_barrier` in `meta_ctrl`; `0x10000000` (bit 28) is `INT_EN` in `len_ctrl` but the V4 `notification-mask` in `meta_ctrl`. Always tag which word a `0x0?000000` constant belongs to. The `meta_ctrl` op-class enum beyond the plain-copy default `0b010` (the CCE/TDG compute ops) is the one part of the format not fully grounded by the M2M path — *(LOW confidence)*, deferred to [meta-ctrl-overlays](meta-ctrl-overlays.md).

---

## 3. The Dynamic Cycle (at a Glance)

The runtime counterpart of the static descriptor is a **five-phase cycle** that is identical in shape on both code lines: **stage → trigger → doorbell → poll-marker → ack/recycle**. A reader who knows any DMA-ring driver already owns the frame — a producer index, a consumer index, a tail-pointer doorbell, and a completion wait — with the one Alpine twist that the completion wait is a *host-memory poll*, not a hardware completion ring. The full algorithm, the cursor offsets, the per-arch poll budget, and the BAR0 gate are owned by [ring-cycle](ring-cycle.md); this is the orientation diagram.

```text
                ┌──────────────────────────────────────────────────────────────┐
                │  per BATCH  (≤ sync_threshold = 2031 data descs  +  1 marker) │
                └──────────────────────────────────────────────────────────────┘

  STAGE       build the TX+RX descriptor pair(s) into ring memory                   [virtual-rings.md
   ↓            udma_desc_get → next_desc_idx (+0x1c)  ·  udma_ring_id_get → phase    | ring-cycle §2]
   ↓            append ONE marker self-copy LAST:  WRITE_ONCE(src,0xabcdef01), copy +0 → +4
   ↓            pending = data_descs + 1
   ↓
  TRIGGER     udma_m2m_copy_start(qid, pending, pending)            (udma_m2m.c:416)  [ring-cycle §3]
   ↓            userspace path:  ndl_dma_queue_copy_start → ioctl 0x80084e23 (nr 35)
   ↓                              → ndmar_queue_copy_start → udma_m2m_copy_start
   ↓
  DOORBELL    udma_desc_action_add(rxq, pending)   ── RX (destination) FIRST  (:440)  [ring-cycle §3]
   ↓            mb();  reg_write32(&rings.drtp_inc, pending)        ── +0x38  (main.c:709-710)
   ↓          udma_desc_action_add(txq, pending)   ── THEN TX (source)        (:445)
   ↓            × BAR0-blocked from userspace:  udma_blocked[]  (neuron_dma.c:869)
   ↓          ═══ HW fetches `pending` descriptors from its internal tail, retires in order ═══
   ↓            (last data desc carries WRITE_BARRIER / SOW;  marker copy runs last)
   ↓
  POLL        ndma_memcpy_wait_for_completion (neuron_dma.c:230)                      [ring-cycle §4
   ↓            udelay(first_wait);  every 1 µs:  READ_ONCE(*(ptr+4)) == 0xabcdef01 ?  | iofic.md]
   ↓              yes → re-arm (clear +4, re-seed +0)  → ACK
   ↓              no  → udelay(1);   i > loop → -ETIMEDOUT
   ↓
  ACK /       ndma_ack_completed_desc (neuron_dma.c:36)                               [ring-cycle §5]
  RECYCLE       udma_cdesc_ack(rxq, count)  ── CAS next_cdesc_idx += count  ── RX FIRST
   ↓            udma_cdesc_ack(txq, count)  ──  (+0x34, & size_mask)        ── THEN TX
   ↓
              udma_available_get reports `count` slots free (minus the 16-desc guard gap)
                └──► more remaining? restage next batch   else done
```

Three orientation facts:

- **The doorbell is the whole launch.** A single `mb()`-then-`reg_write32(drtp_inc, num)` hands `num` descriptors to the engine. Because that one write *is* the trigger, it is the highest-value MMIO offset in the path and the reason the kernel BAR0-blocks it. RX is armed before TX so the destination ring is ready before the source read begins.
- **The marker is a 4-byte self-copy, ordered by barrier policy.** The completion descriptor copies `0xabcdef01` from `+0` to `+4` in a host buffer; because UDMA retires a ring in order and the last *data* descriptor carries `WRITE_BARRIER` (V2) or `SOW` (V3+), the marker at `+4` cannot read `0xabcdef01` until every prior data write has landed. The ordering is *enforced*, not assumed.
- **Recycle is a lock-free CAS.** `next_cdesc_idx` is a pure software counter (no hardware completion ring feeds it); ack CAS-advances it, and `udma_available_get` immediately reports the freed slots, minus a permanent 16-descriptor guard gap.

> **QUIRK —** there is **no hardware completion queue** on the production M2M path: `cdesc_base == NULL` on every Neuron M2M queue, so `next_cdesc_idx` is bookkeeping the driver advances itself, never an index into a CQ the engine wrote. A reimplementer who provisions a real completion ring will find the engine never writes it on this path, and a marker poll will never match a CQ it was never fed. The descriptor even *has* an `INT_EN` bit — the engine can raise an interrupt — but the driver chooses a tight 1 µs busy-poll instead, because a small host↔device copy completes in microseconds and an interrupt round-trip would dominate. See [iofic](iofic.md) for the interrupt controller that is initialized but kept off the completion path.

---

## 4. Where Each Producer Enters the Stack

The DMA engine has many callers, but they all funnel through one of two authoring entries before reaching the same cycle. This is the orientation a reader needs to place any DMA-using subsystem.

| Producer | Authoring entry | Path to the engine |
|---|---|---|
| Model exec — pseudo-DMA program | `vring_add_desc_transfer` / packet builders (`tdrv/vring.c`) | vring → dump → pring → ioctl doorbell |
| Collective `encd` — devmem load + CCE reductions | `encd_devmem_load_with_dma_add_descriptor`, `vring_add_dma_packet_cce` | vring (template) → resolve → dump → ioctl doorbell |
| Driver-internal H2T memcpy (`ndma_memcpy*`) | `ndma_memcpy_chunks` (`neuron_dma.c:311`) | kernel rings, end-to-end (stage→trigger→poll→ack) |
| Zero-copy H2D (`ndma_zerocopy_submit`) | `ndma_build_n_issue_zc_descs` (`neuron_dma.c:1393`) | kernel rings, pinned user pages, async CQE ring |
| `memset` | `ndma_memset` (`neuron_dma.c:547`) | host→device fill, then device→device replicate (d2d fast path) |

The userspace producers (exec, collectives) all share the **author-then-commit** shape: a producer authors copies/packets into a vring, the packing layer coalesces and barriers them, a template pass resolves symbolic addresses to physical ones, a dump commits the result to a static pring, and the ring cycle doorbells it through the ioctl bridge. The kernel producers (`memcpy`, zero-copy, `memset`) run the cycle directly on driver-owned host-memory rings, because the kernel both authors *and* owns the doorbell on that path. Both ultimately drive the identical 16-byte descriptors through the identical `drtp_inc` doorbell — the difference is only who rings it. The kernel-side op layer (the classic/async/zero-copy state machines, the host PA encoding `virt_to_phys(va) | pci_host_base`, the BAR0 block table) is owned by [dma-op-layer](../kernel/dma-op-layer.md) and [dma-rings](../kernel/dma-rings.md).

---

## 5. The Six Part-VIII Pages

This Part is six pages: this map, two static-format pages, two dynamic-runtime pages, and the interrupt model. The split is the static-versus-dynamic seam — what a descriptor *is* versus what *happens to a ring of them*.

| Page | Owns | Half | Key anchors |
|---|---|---|---|
| **(this map)** `overview.md` | the unified frame; userspace↔kernel agreement; the end-to-end cycle | — | `union udma_desc`, `drtp_inc@+0x38`, `0xabcdef01`, `next_cdesc_idx@+0x34` |
| [The 16-Byte Descriptor](descriptor-format.md) | the `union udma_desc` layout, `len_ctrl` bitfield, `meta_ctrl` default, barrier-mode bit positions, len0 trick, per-arch deltas, byte-agreement | static | builders `0x45cac0`–`0x45cda0` / `udma_m2m.c:261`–`:323`; `udma.h:38–72` |
| [SDMA / CCE / TDG Meta-Control Overlays](meta-ctrl-overlays.md) | the `meta_ctrl` op-class enum (the SDMA/CCE/TDG compute ops set by the CAYMAN combo-op / transpose / CRC builders) beyond the plain-copy default | static | `meta_ctrl[25:23]` `ssmae_op`; `aws_cayman_sdma_m2m_build_*` |
| [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md) | the five-phase dynamic cycle; ring cursors in `struct udma_q`; the doorbell + BAR0 gate; the `0xabcdef01` host-marker poll; the recycle CAS | dynamic | `udma_m2m_copy_start:416`, `udma_desc_action_add:700`, `ndma_memcpy_wait_for_completion:230`, `udma_cdesc_ack:452` |
| [Virtual Rings (vring) and Packet Builders](virtual-rings.md) | the host-side paged descriptor list; the next-desc allocator; the copy/event/CCE/transpose builders; post-hoc pack/barrier/rewrite edits; the dump to pring | dynamic | `vring.c`; `vring_add_desc_transfer 0x312290`, `vring_dump_to_pring_descriptors_padded 0x311f10` |
| [IOFIC Interrupt Model](iofic.md) | the AnnapurnaLabs interrupt controller — initialized at `udma_init`, used for *error* interrupts, **not** completion — and why completion is polled instead | dynamic | `udma_iofic_*`; `INT_EN` bit unused on M2M completion |

> **NOTE —** read the two static pages before the dynamic ones if you are reimplementing from scratch: the cycle's marker descriptor, the barrier policy that orders it, and the `vring` element are all the 16-byte descriptor under different names. A reader who only needs the *runtime* model can start at [ring-cycle](ring-cycle.md) and refer back to [descriptor-format](descriptor-format.md) for any bit. The [meta-ctrl-overlays](meta-ctrl-overlays.md) and [iofic](iofic.md) pages are the two least-grounded corners of the Part — the op-class enum and the (unused-for-completion) interrupt path — and are the places a reimplementer should expect to re-derive against newer silicon.

---

## 6. Per-Arch Posture

The wire format and the cycle are **arch-invariant**; the per-arch deltas are capability gating and queue tuning, never a format or protocol change. A descriptor built for v2 and one for v4 with the same `(src, dst, size, phase, barrier)` are bit-for-bit identical. The deltas a reimplementer must honor (full table on [descriptor-format §7](descriptor-format.md#7-per-arch-deltas)):

| Dimension | SUNDA (v2 / Trn1·Inf2) | CAYMAN (v3 / Trn2) | MARIANA (v4 / Trn3) |
|---|---|---|---|
| Strong-ordered write (Rx bit 29) | **forbidden** — build returns −1 | allowed | allowed |
| Notification-mask (`meta_ctrl` bit 28) | — | — | **required-capable** |
| Max TX descs / packet | `0x80` = 128 | `0x41` = 65 | `0x41` = 65 |
| Barrier on chunked memcpy | `WRITE_BARRIER` on last data desc | `SOW` on completion desc | `SOW` on completion desc |
| Completion-wait estimate | `est = 4·(count−1)` | `est = 2·(count−1)` | (V3 family) |
| H2T default qid | `0` | `nc_id % 2` | (per-DHAL) |

> **GOTCHA —** requesting a strong-ordered write on SUNDA is **not silently ignored** — the userspace builder logs *"udma strong ordering not supported in TRN1!"* and the packet build returns −1 (`build_copy_descriptor` arch `<= 2` branch `@0x45cd19`). The per-arch behavior is dispatched through the kernel DHAL vtable (`ndhal_ndma` / `ndhal_ndmar`); the userspace counterpart is the `al_mla_udma_*_{sunda,cayman,mariana}` family. Neither moves a bit — they choose *whether* a capability is available and *which* queue-tuning constant applies. See [dhal-v2](../kernel/dhal-v2.md) / [dhal-v3](../kernel/dhal-v3.md) for the kernel dispatch and [arch-sdma](../runtime/arch-sdma.md) for the userspace per-arch SDMA layer.

---

## Related Components

| Name | Relationship |
|---|---|
| `union udma_desc` (`udma.h:38–58`) | the 16-byte submission descriptor every page is about |
| `al_udma_m2m_build_copy_descriptor` (`0x45cca0`) / `udma_m2m_build_descriptor` (`:323`) | the two pair-orchestrators that author the format byte-identically |
| `udma_m2m_copy_start` / `al_udma_m2m_copy_start` | the trigger; arms RX then TX |
| `udma_desc_action_add` / `al_udma_desc_action_add` | the doorbell; `mb()` + `reg_write32(drtp_inc, num)` |
| `ndma_memcpy_wait_for_completion` / `dma_wait_for_completion_handle` | the host-marker poll (kernel 1 µs / userspace 10 µs) |
| `udma_cdesc_ack` / `al_mla_udma_cdesc_ack_*` | the recycle CAS on `next_cdesc_idx` |
| `vring` / `pring` (`tdrv/vring.c`) | the host-side author-then-commit staging the userspace path pivots on |
| `udma_blocked[]` / `ndma_bar0_blocked_one_engine` | the BAR0 write-block that makes the kernel own the doorbell |

## Cross-References

- [The 16-Byte Descriptor](descriptor-format.md) — the static wire format: `len_ctrl`/`meta_ctrl`, barrier-mode bit positions, the len0 trick, per-arch deltas, byte-agreement
- [The Ring / Trigger / Doorbell / Completion Cycle](ring-cycle.md) — the dynamic five-phase cycle: ring cursors, the `drtp_inc` doorbell, the `0xabcdef01` marker poll, the recycle CAS
- [Virtual Rings (vring) and Packet Builders](virtual-rings.md) — host-side descriptor authoring, packet/CCE/transpose builders, post-hoc edits, and the dump to a static pring
- [SDMA / CCE / TDG Meta-Control Overlays](meta-ctrl-overlays.md) — the `meta_ctrl` op-class enum beyond the plain-copy default
- [IOFIC Interrupt Model](iofic.md) — the interrupt controller that is initialized for errors but kept off the completion path
- [DMA Rings and H2T Queues](../kernel/dma-rings.md) — the kernel ring container, the NULL-completion-ring program, and descriptor-count rounding
- [DMA Op Layer and the Completion-Marker Model](../kernel/dma-op-layer.md) — the kernel high-level DMA primitives (classic / async / zero-copy / memset) that drive the cycle
- [back to index](../index.md)
