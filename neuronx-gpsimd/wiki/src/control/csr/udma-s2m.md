# CSR — UDMA S2M (Stream-to-Memory)

The **`udma_s2m`** register file is the **inbound** descriptor-engine of an Annapurna
UDMA channel: it accepts packets from the application **stream** interface and
**writes** the received data into host/HBM buffers that software pre-supplies through
an RX descriptor ring, then posts completions to an RX completion ring. It is the
mirror image of the outbound [`udma_m2s`](udma-m2s.md) block — the two together form
one full-duplex SDMA channel (SDMA being the Cayman-era name for the Maverick
DDMA/CDMA/UDMA engine family). This page is reimplementation-grade: every claim is
anchored to a register, bitfield, offset, or schema line.

All facts below are derived by static analysis of the shipped, binary-derived register
schema `csrs/sdma/udma_s2m.json` (the Cayman arch-regs tarball) cross-checked
byte-for-byte against the generated Maverick C accessor header
`arch-headers/maverick/al_udma_s2m_regs.h`. Confidence is tagged
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`.

---

## 1. Regfile metadata

| Field | Value | Note |
|---|---|---|
| `UnitName` | `udma_s2m` | |
| `Type` / `RegfileFlavor` | `REGFILE` / `POSEDGE` | |
| `DataWidth` | `32` bits | |
| `AddrWidth` | `17` bits | → 0x20000 (128 KiB) addressable window |
| `SizeInBytes` | `98304` = **0x18000** (96 KiB) | **smaller** than M2S 0x20000 — see §6 |
| `InterfaceType` | `NONE` | M2S declares `APB`; S2M literally `NONE` |
| `HalName` / `Description` | empty | `Memories: []`, `Parameters: []` |

`[HIGH/OBSERVED]` — all literal from the regfile root of `udma_s2m.json`.

S2M prefetches RX descriptors (software-supplied **empty** receive buffers) from a host
descriptor ring, accepts a packet from the stream, writes the payload (and optionally a
split header) over an AXI **write** channel into the addressed buffer, then writes a
completion descriptor back to the RX completion ring and acks the stream. The key
asymmetry vs M2S: M2S **reads** memory and **pushes** to the stream; S2M **accepts** the
stream and **writes** memory. Therefore S2M owns an AXI **write-data** path, a packet /
header-split surface, and RX no-descriptor (drop / hint / wait) handling — none of which
have an M2S analogue — and it **drops** the four M2S egress-shaping bundles (MLA
enhanced-prefetch, DWRR, two rate-limiters), because a receiver neither schedules nor
rate-limits what it is given. `[HIGH/OBSERVED]` (bundle diff §6).

---

## 2. Bundle map and register count

The regfile is **8 bundle arrays**. Bundle base addresses come from each bundle's
`AddressOffset` (HEX string); the stride to compute per-instance bases of an array bundle
comes from `BundleSizeInBytes` (DECIMAL string). **These two encodings differ — that is
the schema gotcha** (callout below).

| Bundle | base | ArraySize | `BundleSizeInBytes` | #reg | group meaning |
|---|---|---|---|---|---|
| `AXI_S2M` | 0x100 | 1 | 256 (0x100) | 11 | AXI master params: AW data/comp **WRITE** + AR desc **READ**, burst-split, split rd/wr outstanding limits |
| `S2M` | 0x200 | 1 | 256 (0x100) | 20 | engine state machine, error log, FIFO status, stream cfg, `pref_queue_en` |
| `S2M_rd` | 0x300 | 1 | 64 (0x40) | 4 | descriptor-prefetch config + promotion + almost-full-to-app threshold |
| `S2M_wr` | 0x340 | 1 | 64 (0x40) | 2 | **DATA-WRITE FIFO** config + RX **DROP/HINT/WAIT** no-descriptor handling — *S2M-specific, no M2S analogue* |
| `S2M_comp` | 0x380 | 1 | 64 (0x40) | 3 | completion controller (`desc_size`, fifo depths, app-ack timeout) |
| `S2M_stat` | 0x500 | 1 | 256 (0x100) | 7 | statistics (`drop_pkt`, 64-bit `rx_bytes`, prefed/comp/ack counters) |
| `S2M_feature` | 0x600 | 1 | 256 (0x100) | 6 | feature / FIFO-size readback |
| `S2M_Q` | 0x1000 | **16** | 4096 (0x1000) | 20 | **per-queue** RX descriptor-ring programming surface |

**Verified counts** (re-parsed from `udma_s2m.json`, not carried):

- register **definitions** = **73** (53 in singleton bundles + 20 in `S2M_Q`) `[HIGH/OBSERVED]`
- bitfield **definitions** = **264** `[HIGH/OBSERVED]`
- register **instances** (defs × ArraySize) = 53·1 + 20·16 = **373** `[HIGH/OBSERVED]`
- field-level access: **RO = 146, RW = 102, WO = 16** (= 264) `[HIGH/OBSERVED]`
- reg-level access: **RW = 44, RO = 25, WO = 4** (= 73) `[HIGH/OBSERVED]`

No `ResetValue` ends in `0xb1` (the unrelated TPB ISA `SET_ORDERING_MODE` opcode); the
placeholder check is clean. `[HIGH/OBSERVED]`

> ### ⚠️ GOTCHA — `AddressOffset` is HEX, `BundleSizeInBytes` is DECIMAL
>
> The `S2M_Q` bundle has `AddressOffset = "0x01000"` (hex → 0x1000) and
> `BundleSizeInBytes = "4096"` (**decimal**, = 0x1000). Per-queue base for queue *i* is
> `0x1000 + i·stride`.
>
> - **Correct (decimal stride 0x1000):** `Q[0]@0x1000 … Q[15]@0x1000 + 15·0x1000 = 0x10000`.
>   The last register in a queue (`q_rx_pkt`) sits at bundle-offset `0x68`, so
>   `Q[15].q_rx_pkt = 0x10000 + 0x68 = 0x10068` — comfortably inside the `0x18000`
>   `SizeInBytes` and the `0x20000` addressable window. No bundle overlap.
> - **Wrong (if "4096" were parsed as HEX = 0x4096):** stride 0x4096 →
>   `Q[15]@0x1000 + 15·0x4096 = 0x3d8ca` — **outside** the 0x18000 window. A reimplementor
>   who treats the size as hex mislays every queue past Q[0]. Parse `AddressOffset` with
>   `int(s,16)` and `BundleSizeInBytes` with `int(s,10)`.

---

## 3. RX descriptor-ring programming (the inbound model)

Each of the 16 queues (`S2M_Q[i]`, base `0x1000 + i·0x1000`) owns **two** rings: the **RX
descriptor ring** (host → engine; software produces **empty** receive buffers) and the **RX
completion ring** (engine → host writeback). All pointers are **in units of descriptors,
not bytes** (each `descriptor_offset` field says so verbatim). The ring base is 64-byte
aligned (`RDRBP_low[5:0]` is RO-zero). The `0x00..0x50` layout is byte-for-byte the M2S ring
renamed `TDR→RDR` (Rx Descriptor) and `TCR→RCR` (Rx Completion) — the standard Annapurna
UDMA producer/consumer ring: the ring is the buffer pool the host **pushes** into and the
engine **pops** from. `[HIGH/OBSERVED]`

### RX descriptor ring (buffer-supply)

| off | register | field(s) | acc | meaning |
|---|---|---|---|---|
| 0x28 | `RDRBP_low` | `addr[31:6]`; `[5:0]` RO-0 | RW | ring base [31:6]; 64B alignment enforced (reset `addr=0x01`) |
| 0x2c | `RDRBP_high` | `addr[31:0]` | RW | ring base [63:32] |
| 0x30 | `RDRL` | `descriptor_offset[23:0]` | RW | ring **length** in descriptors |
| 0x34 | `RDRHP` | `ring_id[31:30]`, `descriptor_offset[23:0]` | RO | **head**: next descriptor the engine prefetches into the FIFO |
| 0x38 | `RDRTP_inc` | `val[23:0]` | **WO** | **doorbell** — write *N* to advance the tail by *N* descriptors: "I posted *N* more empty buffers" |
| 0x3c | `RDRTP` | `ring_id`, `descriptor_offset[23:0]` | RO | **tail**: next free descriptor slot in host memory |
| 0x40 | `RDCP` | `ring_id`, `descriptor_offset[23:0]` | RO | **current**: first descriptor in the prefetch FIFO (next for the data-writer) |

`RDRHP/RDRTP/RDCP` (and `RCRHP`/`RCRHP_internal`) each carry `ring_id[31:30]` with reset
`0x1` — the wrap/generation tag the engine matches against each descriptor's ring-id bit.
S2M has **no data-tail-pointer pair** (M2S's `TDRDTP_inc/TDRDTP@0xe0/0xe4` and the
`AXI_M2S_MLA` enhanced-prefetch are absent) — RX prefetch is single-tail. `[HIGH/OBSERVED]`

### RX completion ring (engine → host writeback)

| off | register | field(s) | acc | meaning |
|---|---|---|---|---|
| 0x44 | `RCRBP_low` | `addr[31:6]`; `[5:0]` RO-0 | RW | completion ring base [31:6] (64B) |
| 0x48 | `RCRBP_high` | `addr[31:0]` | RW | completion ring base [63:32] |
| 0x4c | `RCRHP` | `ring_id`, `descriptor_offset[23:0]` | RO | completion head: next descriptor the completion controller updates |
| 0x50 | `RCRHP_internal` | `ring_id`, `descriptor_offset[23:0]` | RO | internal mirror of 0x4c |

### Per-queue config / status / control

| off | register | key fields (reset) | acc | meaning |
|---|---|---|---|---|
| 0x00 | `desc_pref_cfg` | `fifo_start_addr[26:16]`, `fifo_depth[11:0]=0x080` | RW | per-queue slice of the shared descriptor-prefetch FIFO |
| 0x20 | `cfg` | `en_pref[16]`, `en_stream[17]`, `AXI_qos[30:28]`, `cmpl_force_full_line[31]`, `data_force_full_line[23]`, `axi_awcache_comp[27:24]=3`, `axi_awcache_data[7:4]=3`, `axi_awcache_hdr[3:0]=3`, `allow_lt_min_pref[20]` | RW | start/stop a queue; **three** AWCACHE fields (S2M writes comp + data + header) |
| 0x24 | `status` | `q_full[31]`, `rx[29]`, `prefetch[28]`, `q_used[24:0]` | RO | RX-active status (vs M2S `scheduler`) |
| 0x54 | `comp_cfg` | `en_comp_ring_update[0]=1`, `dis_comp_coal[1]=1`, `first_pkt_promotion[2]=1`, `buf2_len_location[3]=1` | RW | writeback ON by default; coalescing OFF; buf-2 length placement |
| 0x58 | `comp_cfg_2` | `timer[31:0]=0x0186A0` | RW | **per-queue** completion-coalescing timer (M2S kept this global) |
| 0x5c | `pkt_cfg` | `en_hdr_split[17]`, `force_hdr_split[16]`, `hdr_split_size[15:0]=0x40` | RW | **S2M-only: header split** — split the first *N* header bytes into a separate buffer |
| 0x60 | `qos_cfg` | `q_qos[7:0]` | RW | per-queue QoS (no DWRR weight reg — RX does not deficit-schedule) |
| 0x64 | `q_sw_ctrl` | `rst_q[8]`, `rst_prefetch[4]`, `rst_current_ptr[3]`, `rst_head_ptr[2]`, `rst_tail_ptr[1]` | **WO** | reset strobes; RX adds `rst_prefetch`, drops M2S `rst_data_tail_ptr`/`rst_dmb` (no DMB barrier on RX) |
| 0x68 | `q_rx_pkt` | `cnt[31:0]` | RO | per-queue **received**-packet counter |

`en_stream` ("Enables the reception of packets from the stream to this queue") is the RX
analogue of M2S `en_scheduling`. **Queue count = 16** (`S2M_Q.ArraySize=16`),
independently corroborated by the shipped interrupt-source table naming
"RX (S2M) Completion Queue 0…15" and by the Mariana reg-dump emitting
`q_rx_pkt<0>..q_rx_pkt<15>`. `[HIGH/OBSERVED]`

### C pseudocode — post an RX (empty) buffer and consume a completion

```c
/* Per-queue base: S2M_Q[i] at udma_s2m_base + 0x1000 + (i << 12).
 * Pointers are in UNITS OF DESCRIPTORS, not bytes (every descriptor_offset). */
#define S2M_Q(base, i)        ((base) + 0x1000u + ((uint32_t)(i) << 12))
#define RDRBP_LOW   0x28u   /* desc ring base [31:6] (64B aligned)             */
#define RDRBP_HIGH  0x2cu   /* desc ring base [63:32]                          */
#define RDRL        0x30u   /* ring length in descriptors                      */
#define RDRTP_INC   0x38u   /* WO doorbell: advance tail by N descriptors      */
#define RCRBP_LOW   0x44u   /* completion ring base [31:6]                     */
#define RCRBP_HIGH  0x48u   /* completion ring base [63:32]                    */
#define Q_CFG       0x20u   /* en_pref[16] | en_stream[17] | awcache fields    */
#define Q_SW_CTRL   0x64u   /* WO reset strobes                                */
#define COMP_CFG    0x54u   /* en_comp_ring_update[0]                          */

/* RX completion descriptor: desc_size = S2M_comp.cfg_1c.desc_size words (=4 by
 * reset). Software polls the completion ring head it tracks in DRAM; the engine
 * advances RCRHP and (if en_comp_ring_update) writes back the completion words. */
typedef struct {                       /* one completion entry, 4 words by reset */
    uint32_t w0_flags;                 /* status / last-of-packet / error bits   */
    uint32_t w1_len;                   /* received byte length (buf1 / payload)  */
    uint32_t w2_buf2;                  /* buf2 length if buf2_len_location[3]=1  */
    uint32_t w3;                       /* metadata                               */
} s2m_comp_desc_t;

/* One-time queue bring-up: program both ring bases, enable prefetch + stream. */
static void s2m_q_init(uintptr_t b, int i,
                       uint64_t rx_ring, uint32_t rx_len,
                       uint64_t comp_ring)
{
    uintptr_t q = S2M_Q(b, i);
    mmio_w32(q + Q_SW_CTRL, (1u<<8));                 /* rst_q: clean pointers   */
    mmio_w32(q + RDRBP_LOW,  (uint32_t)rx_ring   & ~0x3fu);   /* 64B aligned     */
    mmio_w32(q + RDRBP_HIGH, (uint32_t)(rx_ring  >> 32));
    mmio_w32(q + RDRL,       rx_len & 0x00ffffffu);          /* in descriptors  */
    mmio_w32(q + RCRBP_LOW,  (uint32_t)comp_ring & ~0x3fu);
    mmio_w32(q + RCRBP_HIGH, (uint32_t)(comp_ring >> 32));
    mmio_w32(q + COMP_CFG,   0x1u);                   /* en_comp_ring_update     */
    mmio_w32(q + Q_CFG, (1u<<16) | (1u<<17) |         /* en_pref | en_stream     */
                        (3u<<24) | (3u<<4) | (3u<<0));/* awcache comp/data/hdr=3 */
}

/* Replenish: the host has formatted n_new EMPTY-buffer descriptors at the ring
 * tail in DRAM; ring the doorbell so the engine may land that many packets. */
static inline void s2m_post_buffers(uintptr_t b, int i, uint32_t n_new)
{
    /* descriptor writes must be visible before the doorbell (DMA barrier) */
    dma_wmb();
    mmio_w32(S2M_Q(b, i) + RDRTP_INC, n_new & 0x00ffffffu);   /* WO doorbell     */
}

/* Drain one completion the engine wrote to the host completion ring. The engine
 * raises "RX (S2M) Completion Queue i" interrupt; firmware reads the ring it owns
 * in DRAM (not from MMIO) and refills via s2m_post_buffers. */
static int s2m_consume(volatile s2m_comp_desc_t *ring, uint32_t depth,
                       uint32_t *sw_head, uint32_t expect_ring_id)
{
    volatile s2m_comp_desc_t *cd = &ring[*sw_head % depth];
    if ((cd->w0_flags >> 30) != expect_ring_id) return 0; /* generation mismatch */
    process_received_buffer(cd->w1_len, cd->w2_buf2);
    *sw_head += 1;
    return 1;                                  /* caller then s2m_post_buffers()  */
}
```

`[HIGH/OBSERVED]` for the register set / offsets; the descriptor word layout is
`[MED/INFERRED]` (the completion `desc_size=4` words and `buf2_len_location` placement are
schema-grounded; precise word semantics are reconstructed).

---

## 4. Config / enable registers and the memory-write path

Received stream data flows: **stream ingress → S2M FIFOs → AXI write to the buffer
addressed by the head RX descriptor → completion writeback → ack to stream.**

### Engine state and stream config (`S2M` @ 0x200)

| off | register | fields | acc |
|---|---|---|---|
| 0x200 | `state` | `axi_wr_data[17:16]`, `desc_pref[13:12]`, `data_wr_ctrl[9:8]`, `stream_if[5:4]`, `comp_ctrl[1:0]` | RO |
| 0x204 | `change_state` | `normal[0]` / `dis[1]` / `abort[2]` (start / stop / abort all machines) | WO |
| 0x250 | `stream_cfg` | `disable_stream[0]`, `flush[4]`, `stop_prefetch[8]` | RW |
| 0x254 | `pref_queue_en` | `en[15:0] = 0xFFFF` (all 16 queues prefetch-enabled at reset) | RW |

The `state` register exposes **five** sub-machine states — one more than M2S — because the
`axi_wr_data` writer is the RX addition (the data-landing engine, mirror of M2S's
`data_rd_ctrl`). `stream_cfg.flush` "stops at end of packet reception and asserts ready to
the stream I/F"; `stop_prefetch` "stops descriptor prefetch when the stream is disabled and
the S2M is idle" — both RX-specific drains. M2S's `rd_mode`/`rd_th`
cut-through-vs-threshold egress fields are absent: RX has no read threshold. `[HIGH/OBSERVED]`

### Data-write FIFO + RX no-descriptor handling (`S2M_wr` @ 0x340) — *S2M-specific*

| off | register | fields (reset) |
|---|---|---|
| 0x340 | `data_cfg_1` | `data_fifo_depth[9:0]=0x40` beats, `max_pkt_limit[23:16]=0x20`, `fifo_margin[31:24]` |
| 0x344 | `data_cfg_2` | reset **0x30002710** — see bits below |

`data_cfg_2` is the inbound flow-control register and the heart of RX overflow policy:

- `direct_hdr_use_buf1[31]` — header-split / direct-placement buffer select.
- `full_line_mode[30]` — 0 = byte-enable write; 1 = always full AXI bus width (128-bit) DRAM-write optimization.
- `wait_for_pref[29] = 1` — drop condition: 1 = drop if no descriptor in the prefetch FIFO; 0 = drop if no descriptor anywhere in the system.
- `hint_if_no_desc[28] = 1` — *"Lack of descriptors hint. Generate interrupt when a packet is waiting but there are no available descriptors in the queue."* This is the RX no-descriptor interrupt firmware uses to refill the ring.
- `drop_if_no_desc[27] = 0` — enable packet drop if the queue has no descriptor.
- `desc_wait_timer[23:0] = 0x2710` (10000) — how long to wait for the host to post a fresh descriptor before dropping the in-process packet.

The trio `{hint_if_no_desc, drop_if_no_desc, wait_for_pref/desc_wait_timer}` is the
receiver's back-pressure / overflow policy and has **no TX counterpart** — M2S never
starves for descriptors mid-packet the same way. `[HIGH/OBSERVED]`

### AXI master parameters (`AXI_S2M` @ 0x100)

Three transaction classes, reflecting that S2M **writes** data + completions and **reads**
only descriptors:

| reg(s) | role | key resets |
|---|---|---|
| `data_wr_cfg_1/2` @0x100/0x104 | DATA **write** | `awid=3`, `awcache=3`, `awburst=1` (INCR), `awsize=5` (256-bit), `awprot=3`, `awuser=0x80000` |
| `comp_wr_cfg_1/2` @0x110/0x114 | COMPLETION **write** | `awid=2`, `awcache=3`, `awburst=1`, `awsize=5`, `awprot=3` |
| `desc_rd_cfg_4/5` @0x108/0x10c | DESCRIPTOR **read** | `arid=2`, `arcache=3`, `arburst=1`, `arqos=2`, `arsize=5`, `arprot=3` |
| `data_wr_cfg` / `desc_rd_cfg_3` / `desc_wr_cfg_1` @0x118/0x11c/0x120 | burst split | `max_AXI_beats=8` |
| `ostand_cfg_rd` @0x124 | read outstanding | `max_stream_ack=0x40`, `max_desc_rd_ostand=0x20` |
| `ostand_cfg_wr` @0x128 | write outstanding | `max_data_wr_ostand=0x20`, `max_data_beats_wr_ostand=0x40`, `max_comp_req=0x20`, `max_comp_data_wr_ostand=0x40` |

There is exactly **one read class** (descriptor read) — S2M never reads bulk data, so there
is no "data read" AXI class. The split `ostand_cfg_rd`/`ostand_cfg_wr` pair replaces M2S's
single read-dominated `ostand_cfg`. Per-queue AXI overrides come from
`S2M_Q.cfg.{AXI_qos, axi_awcache_data/hdr/comp}`. `[HIGH/OBSERVED]`

---

## 5. Completion sub-block, statistics, status

### Completion controller (`S2M_comp` @ 0x380)

| off | register | fields (reset) |
|---|---|---|
| 0x380 | `cfg_1c` | `desc_size[3:0]=4` (completion descriptor words), `q_promotion[12]=1`, `force_rr[16]`, `q_free_min[31:28]`, `cnt_words[8]` (count comp FIFO in words vs descriptors) |
| 0x384 | `cfg_2c` | `unack_fifo_depth[27:16]=0x80`, `comp_fifo_depth[11:0]=0x80` |
| 0x38c | `cfg_application_ack` | `tout[31:0]=0x0186A0` — ack-timeout (ACK from the application via the stream interface) |

There is **no `cfg_coal`** here — the coalescing timer moved per-queue to
`S2M_Q.comp_cfg_2.timer`; and **no `cfg_application_ack2`** V3-error handling that M2S
carried. Per queue, the completion path is gated by `comp_cfg.en_comp_ring_update` (reset
**1** — writeback ON by default, the opposite of M2S which reset 0),
`comp_cfg.dis_comp_coal` (reset 1 — coalescing OFF by default), and the byte-count
writeback location chosen by `comp_cfg.buf2_len_location` (reset 1: buffer-2 length in
completion WORD 2 [31:16] rather than WORD 1). The per-queue interrupt is named
"RX (S2M) Completion Queue *i*" in the shipped interrupt-source table. `[HIGH/OBSERVED]`

### Statistics (`S2M_stat` @ 0x500)

`drop_pkt`@0x504 (**RX-only** dropped-packet count), `rx_bytes_low/high`@0x508/0x50c
(64-bit received-byte counter), `prefed_desc`@0x510, `comp_pkt`@0x514, `comp_desc`@0x518,
`ack_pkts`@0x51c. There is **no** `cfg_st`/`use_extra_len` at 0x500 and **no** `tx_pkt`: the
first counter slot is `drop_pkt` at +0x504. `[HIGH/OBSERVED]`

### Error log and FIFO status (`S2M` @ 0x20c..0x254)

`err_log_mask`@0x20c is a **single** 32-bit field `internal[31:0]` (RW) — unlike M2S the
individual error-source bits are not enumerated in the schema, but the concrete S2M error
sources are visible in the shipped interrupt table ("S2M AXI data write data fifo parity
error", "…request fifo parity error", "S2M AXI write completion data/request fifo parity
error"), confirming the data-write and completion-write AXI domains. `log_0..log_3`
@0x214..0x220 capture the faulting descriptor's header words; `clear_err_log.clear`@0x224
is a WO strobe.

The RO FIFO-status registers (`{full[28], empty[24], used[15:0]}` each) are
`s_data_fifo_status`@0x228 (stream data), `s_header_fifo_status`@0x22c (RX header-split
path), `axi_data_fifo_status`@0x230 (the AXI-write data FIFO — RX-specific),
`unack_fifo_status`@0x234 (`empty=1` at reset). `indirect_ctrl.q_num[11:0]`@0x238 selects
which queue mirrors into `sel_pref_fifo_status`@0x23c / `sel_comp_fifo_status`@0x240; the
latter also exposes `coal_active_state[21:20]` (completion-coalescing FSM state, RX-specific).
`fifo_en`@0x24c reset `0xFFFFFFFF`; `check_en`@0x248; `clear_ctrl`@0x244.

`S2M_feature`@0x600 mirrors the FIFO/outstanding limits for readback
(`desc_preferch_fifo_depth=0x400`, data-write/comp/unack FIFO sizes, outstanding limits).
Unlike M2S there is **no `dma_version` register** — `reg_2`@0x604 is reserved. `[HIGH/OBSERVED]`

---

## 6. M2S (outbound) vs S2M (inbound)

The descriptor-ring + completion-ring + prefetch machinery is **symmetric and shared**
(the Annapurna UDMA core); the asymmetry is exactly the direction of data flow.

| | M2S (outbound) | S2M (inbound) |
|---|---|---|
| bundles | 11 | **8** |
| reg-defs | 100 | **73** |
| field-defs | 325 | **264** |
| reg-instances | 550 (70 + 30·16) | **373** (53 + 20·16) |
| queues | 16 | 16 |
| `SizeInBytes` / window | 0x20000 / 0x20000 | **0x18000** / 0x20000 |
| `InterfaceType` | `APB` | `NONE` |
| per-Q regs | 30 | **20** |

**Why S2M is 0x8000 smaller (0x18000 vs 0x20000):** M2S parks four **egress-shaping**
bundles in `0x000` and `0x340..0x3ff` — `AXI_M2S_MLA` (enhanced-prefetch chicken bits,
independent data/desc tail pointers), `M2S_dwrr` (DWRR scheduler), `M2S_rate_limiter`,
`M2S_stream_rate_limiter`. A receiver neither schedules nor rate-limits, so all four are
**dropped** on S2M; S2M **adds** exactly one bundle, `S2M_wr@0x340` (data-write FIFO + the
DROP/HINT/WAIT no-descriptor surface). The per-queue stride stays 0x1000 but with 20 regs
instead of 30, so the file fits in 0x18000. `[HIGH/OBSERVED]`

Per-queue field-semantic flips on the otherwise-shared registers:
`cfg.en_scheduling → cfg.en_stream`; `comp_cfg.en_comp_ring_update` reset `0 → 1`;
`comp_cfg.dis_comp_coal` reset `0 → 1`; `status.scheduler → status.rx`; `q_sw_ctrl` gains
`rst_prefetch` and drops `rst_data_tail_ptr`/`rst_dmb`; `cfg` gains
`data_force_full_line` + `axi_awcache_data` + `axi_awcache_hdr` (S2M writes data and
header). The 10 M2S_Q-only registers dropped on RX are the per-queue rate-limiter (5),
DWRR (4) and `q_tx_pkt`/`read_data_snp`/`TDRDTP_inc`/`TDRDTP` (egress / data-read
concerns); the 4 S2M_Q-only additions are `comp_cfg_2`, `pkt_cfg`, `qos_cfg`, `q_rx_pkt`.
`[HIGH/OBSERVED]`

### Physical placement (within one SDMA channel's APB sub-window)

```
+0x000000  UDMA_M2S      size 0x20000   (outbound)
+0x020000  UDMA_S2M      size 0x18000   (THIS block)
+0x038000  UDMA_GEN      size 0x04000
+0x03C000  UDMA_GEN_EX   size 0x04000
+0x040000  MISC          size 0x04000
```

e.g. `APB_SE_0_SDMA_0_UDMA_S2M` base `0x0000_0100_2020_0000`, size `0x18000`. Across the
SoC the address map counts **280 `*_UDMA_S2M`** (264 unicast + 16 broadcast), identical
multiplicity to M2S — every SDMA channel has exactly one M2S and one S2M, each owning 16
RX queues. `[HIGH/OBSERVED]`

> **Anchor consistency:** the committed [pkl-dma subtree](../address/pkl-dma-subtree.md)
> records the UDMA core geometry **M2S@+0x0 (0x40000) then S2M@+0x40000 (size 0x38000)**,
> GEN@+0x78000, GEN_EX@+0x7C000 — that is the *outer* UDMA-instance geometry where M2S/S2M
> are addressed at a coarser 0x40000 granularity. The SDMA-channel sub-window above (M2S
> 0x20000 + S2M 0x18000) is the *regfile-internal* placement from
> `address_map_flat.yaml`; the S2M **regfile** is the 0x18000-sized block in both views.

---

## 7. Cross-validation: schema vs `al_udma_s2m_regs.h`

Reset words reconstructed from the Cayman JSON bitfields vs the generated Maverick C
header (independent toolchain, same IP). Offsets are **byte-identical** across the board;
reset values either match exactly or diverge **per-silicon** (Cayman JSON is authoritative
for this page):

| register | Cayman (this page) | Maverick header | verdict |
|---|---|---|---|
| `S2M_WR.data_cfg_2`@0x344 | `0x30002710` | `0x30002710` | **EXACT** |
| `S2M_RD.desc_pref_cfg_2`@0x304 | `0x00010407` | `0x00010407` | **EXACT** |
| `S2M_comp.cfg_application_ack`@0x38c | `0x000186A0` | `0x000186a0` | **EXACT** |
| `AXI_S2M.ostand_cfg_rd`@0x124 | `0x00400020` (`max_desc_rd=0x20`) | `0x00400040` (`=0x40`) | divergence (per-silicon) |
| `S2M_comp.cfg_1c`@0x380 | `0x00001004` | `0x00001084` | divergence (per-silicon) |
| `S2M_Q.cfg`@0x1020 | `0x03000033` | `0xe3040033` (Maverick pre-sets force_full_line/AXI_qos) | divergence (per-silicon) |

Offset byte-identity verified for `data_wr_cfg_2`@0x104, `comp_wr_cfg_2`@0x114,
`ostand_cfg_rd`@0x124, `ostand_cfg_wr`@0x128, `S2M.state`@0x200, `stream_cfg`@0x250,
`desc_pref_cfg_2`@0x304, `data_cfg_2`@0x344, `cfg_application_ack`@0x38c.

Maverick is a **superset**: it adds `AXI_S2M_LMA_*`, `S2M_LMA_cfg_1c/2c`,
`desc_rd_cfg_5`/`data_cfg_3/4/5`, `comp_cfg_3c/4c`, `desc_pref_cfg_5`,
`S2M_Q_DESC_PREF_CFG2` — all **absent** in the Cayman schema (Cayman is the subset). Field
names are independently corroborated by the Mariana `udma_s2m_reg_dump.cpp`
(`q_rx_pkt<0..15>`, `drop_pkt`) and the queue count by the Cayman interrupt-source table
("RX (S2M) Completion Queue 0..15" plus the S2M AXI-write parity-error sources).
`[HIGH/OBSERVED]`

> **No CORRECTION to SX-CSR-08.** Every count (73 / 264 / 373; access RW=44/RO=25/WO=4 and
> RO=146/RW=102/WO=16), the hex/decimal bundle-base rule, the ring set, the completion
> sub-block, and all six reset-word comparisons reproduce exactly when re-parsed from
> `udma_s2m.json` and re-grepped from `al_udma_s2m_regs.h`.

---

## 8. Per-generation applicability

| Generation | Codename | `udma_s2m` schema | Grounding |
|---|---|---|---|
| NC-v2 | Sunda | 8 bundles, 73 regdefs, 264 flddefs, `SizeInBytes=0x18000`, `AddrWidth=17` — **byte-identical** to Cayman | `[HIGH/OBSERVED]` (`arch-headers/sunda/csrs/sdma/udma_s2m.json` re-parsed) |
| NC-v3 | Cayman | the schema documented on this page | `[HIGH/OBSERVED]` (cayman-arch-regs `udma_s2m.json`) |
| (Maverick) | — | superset (adds LMA + extra cfg regs), same offsets/field layout | `[HIGH/OBSERVED]` (`al_udma_s2m_regs.h`) |
| NC-v5 | Maverick-class | **INFERRED** — no NC-v5 `udma_s2m.json` in this corpus; the UDMA core is stable across the captured generations, so the S2M descriptor-ring/completion model is expected to carry forward, but bit-exact resets and any LMA-style additions are unconfirmed | `[MED/INFERRED]` |

The byte-identity of the Sunda (NC-v2) and Cayman (NC-v3) schemas establishes the wall:
both generations are byte-grounded. Any NC-v5 statement is extrapolation and is flagged
INFERRED.

---

## Cross-references

- [UDMA HW engine](../../dma/udma-hw-engine.md) — the DDMA/CDMA/UDMA engine this S2M regfile programs.
- [CSR — UDMA M2S](udma-m2s.md) — the outbound (memory-to-stream) sibling; full-duplex partner.
- [Host ↔ device descriptor handoff](../../runtime/host-device-descriptor-handoff.md) — how firmware fills the RX ring and drains completions.
- [SDMA windows (APB)](../../dma/sdma-windows-apb.md) — the APB sub-window placement of M2S/S2M/GEN/GEN_EX/MISC.
- [Descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) — the on-DRAM descriptor and completion word layouts.
- [pkl DMA-engine subtree](../address/pkl-dma-subtree.md) — outer UDMA-instance geometry anchor (S2M @ +0x40000, size 0x38000).
