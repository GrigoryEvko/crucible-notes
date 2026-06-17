# UDMA Memory-to-Memory Builder

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0** (`/usr/src/aws-neuronx-2.27.4.0/`). The owned file is `udma/udma_m2m.c` (529 lines), read verbatim; the descriptor union, the `M2S_*`/`S2M_*` control macros, the barrier enum, and the producer inlines are cited at their definition site in `udma/udma.h` (owned by the [UDMA core](udma-main.md)), pulled here as boundary evidence only. The driver ships as a stripped `.ko`; the GPL C is authoritative — every offset below is a struct field or a macro, not a recovered binary offset. Other driver versions renumber lines.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. The 16-byte wire format these builders emit is independently cross-checked against the `libnrt.so` userspace twin on the [descriptor-format](../dma/descriptor-format.md) page and agrees bit-for-bit. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`udma_m2m.c` is the kernel driver's **memory-to-memory descriptor authoring layer** — the half of the UDMA stack that turns a `(src, dst, size, barrier)` copy request into the two hardware descriptors the silicon actually executes. It is the kernel sibling of the userspace KaenaHal `al_hal_udma_m2m.c`, and the two emit the **identical 16-byte wire format** (owned, and proven byte-for-byte, by the [16-byte UDMA descriptor](../dma/descriptor-format.md) page). This page documents only the *kernel build side*: how a copy becomes a descriptor **pair**, how each descriptor's fields are populated, and how the pair is launched. It does **not** re-derive the wire format — every bit position cited below is a cross-link, not a fresh decode.

The defining fact, stated verbatim in the TU header comment (`udma_m2m.c:14–20`), is that a UDMA copy is **never a single descriptor**: *"m2s (memory to stream) is the same as Tx (and source) and s2m (stream to memory) is the same as Rx (and destination) … always copy between two memory locations by creating paired m2s and s2m descriptors."* Every M2M copy emits **one m2s/Tx descriptor that reads the source** into the queue's TX ring, and **one s2m/Rx descriptor that writes the destination** into the same queue's RX ring. The two descriptors carry the same `size` and the same per-ring phase; they differ only in direction and in which address they name. The reader who already knows the [UDMA core](udma-main.md)'s producer ring (a power-of-two submission ring with a `next_desc_idx` cursor, a 2-bit phase, and a `drtp_inc` doorbell) owns the frame: this layer is the thing that fills `desc_base_ptr[next_desc_idx]` on *both* of a queue's rings and then rings *both* doorbells, RX before TX.

The layer's surface is small and sharply staged. `udma_m2m_copy_prepare_one` (`:365`, public) resolves the TX and RX queue handles, checks free space on both rings, pulls the next slot and phase from each, and calls the orchestrator `udma_m2m_build_descriptor` (`:323`), which seeds `meta_ctrl`, applies the barrier mode, and builds the Tx then the Rx descriptor via the two static field-packers `udma_m2m_build_tx_descriptor` (`:261`) and `udma_m2m_build_rx_descriptor` (`:286`). `udma_m2m_copy_start` (`:416`, public) then rings the two doorbells. Bring-up (`udma_m2m_init_engine`/`udma_m2m_init_queue`) and the AXI-error/ring-id-error plumbing are this file's other responsibilities and are summarized in the Function Map; the hot path is the pair build.

For reimplementation, the contract is:

- **The paired-ring model** — a copy emits exactly two descriptors, m2s/Tx (source-read) onto the TX ring and s2m/Rx (dest-write) onto the RX ring of *one* engine queue, each stamped with *its own ring's* phase, both advancing *their own* ring cursor.
- **The paired-descriptor build** — seed `meta_ctrl` from the fixed default, set `tx_flags = FIRST | LAST` (a single-descriptor packet), route the barrier mode into one of three locations, pack the Tx then the Rx word0, and `memcpy` each 16-byte descriptor into its ring slot.
- **The field-population rule** — which descriptor word each input lands in, and how the Tx and Rx values differ (Tx carries `meta_ctrl` + the source pointer; Rx carries no `meta_ctrl`, only the destination pointer).
- **The launch order** — `udma_desc_action_add(rxq)` always, then `udma_desc_action_add(txq)` only if `m2s_count > 0`: RX armed before TX.

| | |
|---|---|
| **Source** | `udma/udma_m2m.c` (529 lines), aws-neuronx-dkms 2.27.4.0, GPL-2.0 |
| **TU convention** | m2s ≡ Tx ≡ **source-read**; s2m ≡ Rx ≡ **dest-write** (`udma_m2m.c:14–20`) |
| **Copy unit** | a **pair**: one Tx desc on the TX ring + one Rx desc on the RX ring, same `qid` |
| **Public entry — prepare** | `udma_m2m_copy_prepare_one` (`:365`) — resolve queues, check space, get phase, build pair |
| **Public entry — launch** | `udma_m2m_copy_start` (`:416`) — doorbell **RX then TX** |
| **Pair orchestrator** | `udma_m2m_build_descriptor` (`:323`, static) → Tx-build then Rx-build |
| **Field packers** | `udma_m2m_build_tx_descriptor` (`:261`) · `udma_m2m_build_rx_descriptor` (`:286`) — both `static`, both `memcpy` 16 B |
| **Descriptor** | `union udma_desc`, 16 B, `packed, aligned(16)` (`udma.h:38–58`) — wire format owned by [descriptor-format](../dma/descriptor-format.md) |
| **`meta_ctrl` seed** | `tdma_m2s_meta_ctrl_default_value = 0x01080003` (`udma_m2m.c:53–79`) — Tx only |
| **Length limit** | `MAX_DMA_DESC_SIZE = 65536` (`udma.h:479`); `size == 64 KiB ⇒ length 0` (len0 trick) |
| **Barrier modes** | NONE / DMB / WRITE_BARRIER / SOW (`udma.h:196–201`), routed at `:333–348` |
| **Up boundary** | ring container + ownership → [dma-rings](dma-rings.md); copy split + marker poll → [dma-op-layer](dma-op-layer.md) |
| **Side boundary** | engine/queue/doorbell primitives → [udma-main](udma-main.md) |
| **Down boundary** | 16-byte wire format (do **not** re-derive) → [descriptor-format](../dma/descriptor-format.md) |

---

## 1. The Paired-Ring Model

### Purpose

A UDMA engine queue is *two* submission rings, not one: a TX ring (the M2S direction) and an RX ring (the S2M direction). The [UDMA core](udma-main.md) brings both up — `udma_m2m_init_queue` (`udma_m2m.c:146`) builds two `struct udma_q_params` (`UDMA_TX` then `UDMA_RX`) and calls `udma_q_init` twice (`:146`, boundary into [udma-main](udma-main.md)). This layer's job on the hot path is to keep the two rings **in lockstep**: every copy puts exactly one descriptor on each, so the two rings advance one slot per copy and stay index-aligned. A reimplementer who builds one ring per copy, or who lets the two rings drift, breaks the model — the hardware pairs a TX-ring read with the same-index RX-ring write.

### The pairing invariant

The invariant is stated by the TU header (`udma_m2m.c:14–20`) and enforced structurally by `udma_m2m_copy_prepare_one`:

```text
one copy  ⇒  one m2s/Tx descriptor  +  one s2m/Rx descriptor
            └── reads SOURCE          └── writes DESTINATION
            └── into the TX ring       └── into the RX ring
            └── of the SAME engine queue (qid)

TX ring:  ... [ Tx desc N ] [ Tx desc N+1 ] ...   next_desc_idx advances by 1
RX ring:  ... [ Rx desc N ] [ Rx desc N+1 ] ...   next_desc_idx advances by 1
              ^ same index, same size, each stamped with ITS OWN ring's phase
```

Three properties make the pair a pair:

- **Same queue, two rings.** Both descriptors target one `qid`; `udma_m2m_copy_prepare_one` resolves *both* a TX and an RX handle for that `qid` (`udma_q_handle_get ×2`, `:365`, boundary into [udma-main](udma-main.md)).
- **Independent phase per ring.** The Tx descriptor is stamped with the TX ring's phase and the Rx descriptor with the RX ring's phase — `udma_ring_id_get` is called *once per ring* (`×2`, `:365`). The two rings wrap independently, so their phases are tracked separately even though they advance together. (Phase/ring-id semantics — the 2-bit `desc_ring_id`, init `UDMA_INITIAL_RING_ID = 1`, flip on wrap — are owned by [udma-main §3](udma-main.md#3-producer-primitives-and-the-doorbell).)
- **Shared `size`, split address.** Both descriptors carry the same transfer `size`; the Tx descriptor's pointer is the **source** address, the Rx descriptor's is the **destination** address (§3).

> **NOTE —** the two rings are checked for space *independently* before either descriptor is built. `udma_m2m_copy_prepare_one` calls `udma_available_get` on both the TX and the RX queue and returns `-ENOMEM` if **either** has fewer than one free slot (`:365`; the 16-descriptor guard gap inside `udma_available_get` is owned by [udma-main](udma-main.md)). A reimplementer must check both rings before committing — building the Tx descriptor and then discovering the RX ring is full would leave the pair half-emitted.

> **QUIRK —** this layer never touches a *completion* ring. `udma_m2m_init_queue` explicitly **rejects** any completion ring (`:171–174`, *"Completion ring not supported"*), so the only rings in play are the two submission rings. Completion on the Neuron M2M path is observed by a host-memory marker poll one layer up ([dma-op-layer](dma-op-layer.md)), not by a hardware CQ. The `s2m_compl_ring` NULL-check at `:176` is consequently dead code — `:171–174` has already returned `-1` for any non-NULL completion ring.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_m2m_init_queue` | `udma_m2m.c:146` | bring up one queue's TX+RX rings (`udma_q_init ×2`); rejects completion ring | HIGH |
| `udma_m2m_init_engine` | `udma_m2m.c:227` | engine bring-up: `udma_init` + prefetch/burst program | HIGH |
| `udma_set_max_descs_and_prefetch` | `udma_m2m.c:103` | program M2S/S2M prefetch + AXI read-burst regs; `8 ≤ max_descs ≤ 128` | HIGH |

---

## 2. The Paired-Descriptor Build

### Purpose

`udma_m2m_build_descriptor` (`:323`) is the orchestrator that turns one `(s_addr, d_addr, size, barrier_type, set_dst_int)` request into the two filled descriptors. It is called by `udma_m2m_copy_prepare_one` *after* the public layer has resolved both queue handles, confirmed both rings have space, and pulled the next slot pointer and phase from each ring. The orchestrator owns three decisions: the `meta_ctrl` seed, the `tx_flags` (this is always a single-descriptor packet, so `FIRST | LAST` together), and the barrier-mode routing.

### Entry Point

```text
ndma_memcpy_* (neuron_dma.c)                          ── BOUNDARY: dma-op-layer.md
  └─ udma_m2m_copy_prepare_one (udma_m2m.c:365)       ── PUBLIC
       ├─ udma_q_handle_get ×2 (TX, RX)               ── BOUNDARY: udma-main.md
       ├─ udma_available_get ×2  (≥1 free each, else -ENOMEM)
       ├─ udma_desc_get ×2       (next slot on each ring; advance next_desc_idx)
       ├─ udma_ring_id_get ×2    (phase for each ring; flip on wrap)
       └─ udma_m2m_build_descriptor (:323)            ── the PAIR orchestrator
            ├─ [barrier switch :333-348]
            │     └─ sdma_m2s_set_write_barrier (:96)  ── WRITE_BARRIER mode only
            ├─ udma_m2m_build_tx_descriptor (:261)     ── pack + memcpy 16 B into TX slot
            └─ udma_m2m_build_rx_descriptor (:286)     ── pack + memcpy 16 B into RX slot

[later, after the batch is staged]
  udma_m2m_copy_start (udma_m2m.c:416)                ── PUBLIC: doorbell RX then TX
```

### Algorithm — the pair orchestrator

```c
// udma_m2m_build_descriptor — udma_m2m.c:323.  Build BOTH descriptors of one copy.
// rx_desc_ptr / tx_desc_ptr already point at the next free slot on each ring;
// rx_ring_id / tx_ring_id are each ring's current phase (from udma_ring_id_get).
function udma_m2m_build_descriptor(rx_slot, tx_slot, rx_ring_id, tx_ring_id,
                                   s_addr, d_addr, size, barrier_type, set_dst_int):
    meta_ctrl = tdma_m2s_meta_ctrl_default_value          // :329  = 0x01080003 (Tx-only word)
    tx_flags  = M2S_DESC_FIRST | M2S_DESC_LAST            // :331  single-descriptor packet
    rx_flags  = 0

    switch barrier_type:                                  // :333  three different locations
        case UDMA_M2M_BARRIER_NONE:          break        //       no flag
        case UDMA_M2M_BARRIER_DMB:
            tx_flags |= M2S_DESC_DMB                       //       Tx word0  bit30   (:336)
            break
        case UDMA_M2M_BARRIER_WRITE_BARRIER:
            sdma_m2s_set_write_barrier(&meta_ctrl)         //       Tx meta_ctrl bit26 (:339)
            break
        case UDMA_M2M_BARRIER_SOW:
            rx_flags |= S2M_DESC_STRONG_ORDER_WR           //       Rx word0  bit29 (V3+) (:342)
            break
        default:
            pr_err("Invalid m2m barrier is given")         // :346
            return -EINVAL                                 // :347

    if set_dst_int:                                        // :356  interrupt-on-completion request
        rx_flags |= S2M_DESC_INT_EN                        // :357  Rx word0  bit28

    // build the m2s/Tx descriptor FIRST (carries meta_ctrl + the source address),
    // then the s2m/Rx descriptor (no meta_ctrl; carries the destination address).
    ret = udma_m2m_build_tx_descriptor(tx_slot, tx_ring_id, s_addr, size, meta_ctrl, tx_flags)  // :350
    if ret: return ret
    ret = udma_m2m_build_rx_descriptor(rx_slot, rx_ring_id, d_addr, size, rx_flags)             // :353
    return ret
```

```c
// udma_m2m_build_tx_descriptor — udma_m2m.c:261.  static.  Pack ONE m2s/Tx (source-read) slot.
function udma_m2m_build_tx_descriptor(tx_slot, ring_id, s_addr, size, meta_ctrl, flags):
    if size > MAX_DMA_DESC_SIZE: return -EINVAL            // :267  > 0x10000 illegal
    if size == MAX_DMA_DESC_SIZE: size = 0                 // :271-273 len0 trick (64K encoded as 0)

    union udma_desc tx = {}                                // stack scratch, zero-init
    len_ctrl  = flags                                      // already FIRST|LAST(|DMB)
    len_ctrl |= ring_id << M2S_DESC_RING_ID_SHIFT          // :275  << 24  (phase into bits 25:24)
    len_ctrl |= size & M2S_DESC_LEN_MASK                   // :276  [15:0] length
    tx.tx.len_ctrl  = len_ctrl
    tx.tx.meta_ctrl = meta_ctrl                            //        SDMA op / barrier / CRC word
    tx.tx.buf_ptr   = s_addr                               // :279  SOURCE physical address
    memcpy(tx_slot, &tx, sizeof(union udma_desc))          //        publish 16 bytes into the TX ring slot
    return 0
```

```c
// udma_m2m_build_rx_descriptor — udma_m2m.c:286.  static.  Pack ONE s2m/Rx (dest-write) slot.
function udma_m2m_build_rx_descriptor(rx_slot, ring_id, d_addr, size, flags):
    if size > MAX_DMA_DESC_SIZE: return -EINVAL            // :291  same guard as Tx
    if size == MAX_DMA_DESC_SIZE: size = 0                 // :296-298 same len0 trick

    union udma_desc rx = {}                                // :289  zero-init  ⇒  meta_ctrl slot stays 0
    len_ctrl  = flags                                      // FIRST|LAST were NOT set here (see GOTCHA)
    len_ctrl |= ring_id << M2S_DESC_RING_ID_SHIFT          // :300  << 24  (RX ring's phase)
    len_ctrl |= size & M2S_DESC_LEN_MASK                   // :301  [15:0] length
    rx.rx.len_ctrl  = len_ctrl
    rx.rx.buf1_ptr  = d_addr                               // :304  DESTINATION physical address
    memcpy(rx_slot, &rx, sizeof(union udma_desc))          //        publish 16 bytes into the RX ring slot
    return 0
```

> **GOTCHA —** the `FIRST | LAST` packet markers are set **only in `tx_flags`** (`:331`), and `rx_flags` starts at `0` (it carries only the optional `S2M_DESC_INT_EN` and `S2M_DESC_STRONG_ORDER_WR`). So the m2s/Tx descriptor is marked `FIRST + LAST` (a complete one-descriptor packet on the read side), while the s2m/Rx descriptor is **not** marked first/last on the kernel M2M path. A reimplementer who mirrors the FIRST|LAST bits onto the Rx descriptor diverges from the reference — the hardware drives the packet boundary from the m2s side. (The bit values are owned by [descriptor-format §5](../dma/descriptor-format.md#5-the-control-bit-table).)

> **NOTE —** the two field-packers `memcpy` a stack-built `union udma_desc` into the ring slot rather than writing fields piecemeal into device-visible memory — the whole 16-byte word is published atomically from scratch. The publish ordering against the doorbell is handled one layer down: `udma_desc_action_add` issues an `mb()` *before* the `drtp_inc` write so every descriptor `memcpy` is visible to the engine before it is told to fetch ([udma-main §3](udma-main.md#3-producer-primitives-and-the-doorbell)).

### Algorithm — the launch

```c
// udma_m2m_copy_start — udma_m2m.c:416.  PUBLIC.  Ring BOTH doorbells, RX before TX.
function udma_m2m_copy_start(udma, qid, m2s_count, s2m_count):
    if qid >= udma->num_of_queues: return -EINVAL          // :416  bound check
    txq = udma_q_handle_get(udma, qid, UDMA_TX)            //       BOUNDARY: udma-main.md
    rxq = udma_q_handle_get(udma, qid, UDMA_RX)
    if m2s_count > txq->size or s2m_count > rxq->size: return -EINVAL   // batch ≤ ring

    udma_desc_action_add(rxq, s2m_count)                   // :440  RX doorbell ALWAYS (drtp_inc += s2m_count)
    if m2s_count > 0:                                      // :447  comment :412-414
        udma_desc_action_add(txq, m2s_count)              // :449  TX doorbell only if there is read work
    return 0
```

> **QUIRK —** the doorbell order is **RX first, then TX**, and the TX doorbell is *conditional* on `m2s_count > 0` while the RX doorbell always fires (`:440` vs `:447–449`). The source comment (`:412–414`) explains the asymmetry: arming the RX (destination-write) ring before the TX (source-read) ring guarantees the engine has somewhere to put the data before it starts reading, and allowing `m2s_count == 0` lets a caller pre-arm the S2M ring for a future stream without launching any read yet. A reimplementer who rings TX first, or who unconditionally rings both, can launch a source read before its destination slot is published. The same RX-then-TX order appears in the userspace twin and in the ack/recycle path ([dma-op-layer](dma-op-layer.md), [dma-rings §4.4](dma-rings.md#44-ack--recycle--ndmar_ack_completed)). `udma_desc_action_add` itself (the `mb()` + `drtp_inc` write) is owned by [udma-main](udma-main.md).

> **NOTE —** `size` is converted from `64 KiB → 0` in *two* places: in `udma_m2m_copy_prepare_one` (`:380–382`) **and** again inside each field-packer (`:271–273`, `:296–298`). The double conversion is idempotent — `0` stays `0` — so it is redundant but harmless. A reimplementer needs the conversion exactly once, anywhere before the length is masked into `len_ctrl[15:0]`. The reason a full 64 KiB cannot be expressed directly is that the length field is 16 bits and `0x10000` does not fit; the overload is owned by [descriptor-format §2](../dma/descriptor-format.md#the-length--0--65536-rule).

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `udma_m2m_copy_prepare_one` | `udma_m2m.c:365` | **public**: resolve TX+RX handles, check space, get slot+phase ×2, build pair | HIGH |
| `udma_m2m_build_descriptor` | `udma_m2m.c:323` | **static**: seed `meta_ctrl`, `tx_flags=FIRST\|LAST`, route barrier, build Tx then Rx | HIGH |
| `udma_m2m_build_tx_descriptor` | `udma_m2m.c:261` | **static**: pack m2s/Tx word0 + `meta_ctrl` + source `buf_ptr`; `memcpy` 16 B | HIGH |
| `udma_m2m_build_rx_descriptor` | `udma_m2m.c:286` | **static**: pack s2m/Rx word0 + dest `buf1_ptr` (no `meta_ctrl`); `memcpy` 16 B | HIGH |
| `sdma_m2s_set_write_barrier` | `udma_m2m.c:96` | **static**: set `write_barrier` (`meta_ctrl` bit 26) via the `sdma_cme_desc_word` overlay | HIGH |
| `udma_m2m_copy_start` | `udma_m2m.c:416` | **public**: doorbell RX (always) then TX (if `m2s_count>0`) | HIGH |

---

## 3. Field Population

Every input to a copy lands in exactly one descriptor word. The table is the kernel build half of the wire format — *which* value each builder writes *where*. The bit positions inside `len_ctrl` and `meta_ctrl` (FIRST=26, LAST=27, RING_ID=25:24, LEN=15:0, write_barrier=meta bit 26, …) are **owned by [descriptor-format](../dma/descriptor-format.md)** and not restated here; this table maps the kernel *source variable* to the descriptor word and gives the m2s-vs-s2m delta.

| Input / field | Source | m2s / Tx value | s2m / Rx value | Confidence |
|---|---|---|---|---|
| `len_ctrl` ← `flags` | `:262`/`:287` | `FIRST \| LAST` (`\| DMB`) | `0` (`\| INT_EN \| SOW`) | HIGH |
| `len_ctrl` ← phase | `:275`/`:300` | `tx_ring_id << 24` | `rx_ring_id << 24` (own ring's phase) | HIGH |
| `len_ctrl` ← length | `:276`/`:301` | `size & 0xffff` (64K⇒0) | `size & 0xffff` (same `size`) | HIGH |
| `meta_ctrl` (word1) | `:277`/`:289` | `0x01080003` default (+WB bit26) | **absent** — zero-init (`buf2_ptr_lo` slot) | HIGH |
| `buf_ptr` (word2:3) | `:279`/`:304` | `s_addr` = **source** PA | `d_addr` = **destination** PA | HIGH |
| `M2S_DESC_DMB` (bit30) | `:336` | set iff `barrier == DMB` | — (Tx only) | HIGH |
| `write_barrier` (meta bit26) | `:339`,`:96` | set iff `barrier == WRITE_BARRIER` | — (Tx only — no `meta_ctrl` on Rx) | HIGH |
| `S2M_DESC_STRONG_ORDER_WR` (bit29) | `:342` | — | set iff `barrier == SOW` (**V3+ only**) | HIGH |
| `S2M_DESC_INT_EN` (bit28) | `:357` | — | set iff `set_dst_int` | HIGH |

Three deltas are the whole story of how a Tx descriptor differs from its paired Rx descriptor:

- **`meta_ctrl` is Tx-only.** The m2s/Tx descriptor carries the 32-bit `meta_ctrl` word (the SDMA op selector, the CRC/endian controls, the write-barrier). On the s2m/Rx descriptor that same byte range is the union member `buf2_ptr_lo`, which the M2M path leaves zero (`union udma_desc rx = {}`, `:289`). So the entire `meta_ctrl`-routed machinery — the default `0x01080003`, the write-barrier — exists only on the read side.
- **The address splits by direction.** Tx's `buf_ptr` is the source (`s_addr`, `:279`); Rx's `buf1_ptr` is the destination (`d_addr`, `:304`). This is the only place the `(src, dst)` pair physically separates.
- **The barrier mode picks the descriptor.** DMB and WRITE_BARRIER land on the Tx descriptor (word0 bit 30 / `meta_ctrl` bit 26); SOW and INT_EN land on the Rx descriptor (word0 bit 29 / bit 28). The four ordering modes therefore sit in three different bit locations across the two descriptors — the routing is in §2's switch.

> **NOTE —** `meta_ctrl` is *"currently unmodifiable"* on the kernel copy path — the comment at `udma_m2m.c:52` notes the same default is used for every descriptor, and the only mutation the kernel ever makes is flipping the single `write_barrier` bit. The `ssmae_op = 0b010` field inside that default is the SDMA plain-copy op-class; the wider op-class enumeration is *not* grounded by this TU *(LOW confidence)* and is owned by the [descriptor-format Considerations](../dma/descriptor-format.md#9-considerations).

> **CORRECTION (UDMA-M2M-1) —** an earlier note placed `write_barrier` at **bit 28** of `meta_ctrl`. That is wrong: it is **bit 26**, double-proven (the `sdma_cme_desc_word` packed bit-field widths sum to 26 before it, `udma_m2m.c:81–94`; and the userspace setter is `orb $0x4,0x3(%rdi)` ⇒ byte 3 bit 2 ⇒ meta bit 24+2 = 26). The full proof is owned by [descriptor-format §3](../dma/descriptor-format.md#overlay-b--struct-sdma_cme_desc_word-and-the-write-barrier); reproduced here only as a builder-level caution because `sdma_m2s_set_write_barrier` (`:96`) is this file's function. Note that `0x04000000` is bit 26 in **both** words but means `FIRST` in `len_ctrl` and `write_barrier` in `meta_ctrl` — always tag which word.

---

## 4. The Wire Format Is Byte-Identical to Userspace

> **NOTE —** the 16-byte descriptor these kernel builders emit is the **same wire format** the userspace KaenaHal (`al_hal_udma_m2m.c` in `libnrt.so`) emits. This is not an assertion to take on faith: the two code lines were reconciled field-by-field and proven bit-for-bit identical for the copy pair. The bit layout — `len_ctrl` (length, 2-bit phase, FIRST/LAST/CONCAT/DMB/SOW/INT/META), the `meta_ctrl` overlays, the default `0x01080003`, the write-barrier at bit 26, and the `length == 0 ⇒ 65536` overload — is **owned by [the 16-byte UDMA descriptor](../dma/descriptor-format.md)** and is **not re-derived on this page**.

What this page owns is the *kernel build side* of that shared format: the pairing rule (§1), the orchestration and field packing (§2), and the source-variable-to-word map (§3). What the [descriptor-format](../dma/descriptor-format.md) page owns is the *bytes*: the exact bit positions, the per-arch deltas (SOW is V3+, notification-mask is V4-only — neither a format change), and the full userspace↔kernel agreement table. For a kernel-built and a userspace-built descriptor with the same `(src, dst, size, phase, barrier)`, the two are identical to the bit. A reimplementer building the kernel side reproduces the algorithm here and the layout there.

The kernel M2M path exercises a **subset** of the format. It emits only plain copy pairs — `FIRST | LAST` on the Tx, optional DMB / WRITE_BARRIER / SOW / INT_EN — and never sets `CONCAT` (bit 31) or `META` (bit 23); those are userspace-HAL features with no kernel macro in `udma_m2m.c`. The hardware honors the same bits regardless of which code line authored them; the kernel simply never writes them on this path. Transfers larger than 64 KiB are split into multiple ≤64 KiB pairs by the layer above ([dma-op-layer](dma-op-layer.md)), not expressed as a `CONCAT` chain in one descriptor.

---

## Function Map (full file)

| Function | Source | Role | Confidence |
|---|---|---|---|
| `sdma_m2s_set_write_barrier` | `udma_m2m.c:96` | static: set `meta_ctrl` bit 26 via `sdma_cme_desc_word` overlay (WRITE_BARRIER mode) | HIGH |
| `udma_set_max_descs_and_prefetch` | `udma_m2m.c:103` | program M2S/S2M prefetch + AXI read-burst; `8 ≤ max_descs ≤ 128`; V3 DGE `min==max==8` workaround | HIGH |
| `udma_m2m_init_queue` | `udma_m2m.c:146` | bring up one queue's TX+RX rings (`udma_q_init ×2`); 256-B align; rejects completion ring | HIGH |
| `udma_m2m_init_engine` | `udma_m2m.c:227` | engine bring-up: `udma_init` (boundary) + `udma_set_max_descs_and_prefetch` | HIGH |
| `udma_m2m_build_tx_descriptor` | `udma_m2m.c:261` | static: pack m2s/Tx descriptor (`len_ctrl` + `meta_ctrl` + source `buf_ptr`) | HIGH |
| `udma_m2m_build_rx_descriptor` | `udma_m2m.c:286` | static: pack s2m/Rx descriptor (`len_ctrl` + dest `buf1_ptr`, no `meta_ctrl`) | HIGH |
| `udma_m2m_build_descriptor` | `udma_m2m.c:323` | static: orchestrate the pair — seed meta, `FIRST\|LAST`, barrier route, Tx-then-Rx | HIGH |
| `udma_m2m_copy_prepare_one` | `udma_m2m.c:365` | **public**: resolve handles, check space, get slot+phase ×2, build pair | HIGH |
| `udma_m2m_copy_start` | `udma_m2m.c:416` | **public**: doorbell RX (always) then TX (if `m2s_count>0`) | HIGH |
| `udma_m2m_set_axi_error_abort` | `udma_m2m.c:454` | program AXI-error→abort detection table + DMA timeout `0xffffffff`; unmask 6 IOFIC groups | HIGH |
| `udma_m2m_mask_ring_id_error` | `udma_m2m.c:490` | mask ring-id prefetch errors at secondary IOFIC, top-level INTC, and `err_log_mask` | HIGH |

> **NOTE —** the AXI-error and ring-id-error functions (`:454`, `:490`) hard-cast `udma->gen_regs` to `struct udma_gen_regs_v4` and are wired from the v2/v3 DHAL bring-up. Whether the V1 (`UDMA_REV_ID_4`) path uses them is out of scope for this TU — no caller exists in this file *(MEDIUM confidence on arch reach; HIGH that the V4 cast is what the code does)*. The single-revision gate that makes `_v4` the only register layout is owned by [udma-main §6](udma-main.md#6-the-single-revision-gate).

---

## Related Components

| Name | Relationship |
|---|---|
| `udma_m2m_build_descriptor` (kernel) / `al_udma_m2m_build_copy_descriptor` (userspace) | The two pair-orchestrators that author the identical 16-byte format |
| `udma_m2m_copy_start` | The RX-then-TX doorbell pair this page launches; rings the [udma-main](udma-main.md) `drtp_inc` |
| `struct udma` / `struct udma_q` | The engine handle and per-queue shadow this builder fills; owned by [udma-main](udma-main.md) |
| `ndma_eng` / `ndma_ring` | The ring container that embeds `struct udma` and owns the TX/RX submission buffers ([dma-rings](dma-rings.md)) |
| `ndma_memcpy_*` | The high-level split + marker-poll layer that calls `_copy_prepare_one` / `_copy_start` ([dma-op-layer](dma-op-layer.md)) |

## Cross-References

- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the wire format these builders emit; owns every `len_ctrl`/`meta_ctrl` bit position, the barrier-bit table, the `len0` overload, and the userspace↔kernel byte-agreement proof (do **not** re-derive it here)
- [UDMA Core (Annapurna/Alpine Fork)](udma-main.md) — the engine/queue bring-up, the `udma_desc_get`/`udma_ring_id_get` producer inlines, and the `udma_desc_action_add` doorbell this page builds into
- [DMA Rings and H2T Queues](dma-rings.md) — the `ndma_ring` container holding the TX/RX submission buffers, and `ndmar_queue_init`/`ndmar_queue_copy_start`, which call `udma_m2m_init_queue` and `udma_m2m_copy_start`
- [DMA Op Layer and the Completion-Marker Model](dma-op-layer.md) — the layer above: the ≤64 KiB transfer split, the `0xabcdef01` marker poll, and the `ndma_memcpy_*` callers of `udma_m2m_copy_prepare_one` / `_copy_start`
- [UDMA IOFIC Interrupt Controller](udma-iofic.md) — `udma_iofic_error_ints_unmask_one`, called 6× by `udma_m2m_set_axi_error_abort`
- [back to index](../index.md)
