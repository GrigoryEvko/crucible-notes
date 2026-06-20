# CSR — UDMA M2S (Memory-to-Stream Descriptor Engine)

The **UDMA M2S** block is the *outbound* half of a UDMA DMA instance: it reads a
host-produced **TX descriptor ring**, fetches the referenced data over AXI,
pushes it onto the application **stream** interface, and writes a **TX completion
ring** back to memory. On this Trainium SoC it is the SDMA programming surface
for **device → stream** transfers (the firmware's HBM/host outbound path). The
sibling inbound half is [UDMA S2M](./udma-s2m.md).

This page is reconstructed byte-for-byte from the shipped Cayman arch-regs
schema `csrs/sdma/udma_m2s.json` (RegFile → RegistersBundleArrays → Registers →
BitFields; register `AddressOffset` is **relative to its bundle base**), and
cross-validated field-for-field against the generated C accessor headers
`maverick/al_udma_m2s_regs.h` and `sunda/udma_m2s_tr.h`. All offsets/resets
below were re-parsed with `python int(.,16)` directly from the schema; counts
were recomputed from scratch. This is the Annapurna/Alpine UDMA M2S IP — a
`reg_map`-generated register file, not a hand-written firmware struct.

> **Naming.** This is the *renamed-but-same* `al_udma_m2s_regs` block carried
> across silicon generations (see [pkl-dma geometry](../address/pkl-dma-subtree.md)).
> The Maverick DMA engines are DDMA/CDMA/UDMA; "SDMA" is the Cayman-era cluster
> name, and the schema lives under `csrs/sdma/`. This M2S block is the outbound
> half of one **UDMA instance**. Within an instance the UDMA core geometry is
> **M2S @ +0x0 (size 0x40000)**, then S2M @ +0x40000, GEN @ +0x78000, GEN_EX @
> +0x7C000; the M2S *register file itself* spans only the first **0x20000** of
> that M2S window (HIGH/OBSERVED).

---

## 1. RegFile metadata `[HIGH/OBSERVED]`

Literal from the `RegFile` object of `udma_m2s.json`:

| Property        | Value                                   |
|-----------------|-----------------------------------------|
| `UnitName`      | `udma_m2s`                              |
| `Type`          | `REGFILE`                              |
| `RegfileFlavor` | `POSEDGE`                              |
| `DataWidth`     | `32` bits                              |
| `AddrWidth`     | `17` bits → `0x20000` (128 KiB window) |
| `SizeInBytes`   | `131072` = `0x20000`                   |
| `InterfaceType` | `APB`                                  |
| `HalName` / `Description` | *(empty)* — `Memories: []`, `Parameters: []` |

The control plane is **APB**; the engine itself masters **AXI** for descriptor
read, data read, completion write, and MSI-X. `0x20000` matches the
`address_map_flat.yaml` `size` for every `*_UDMA_M2S` instance.

### Verified counts `[HIGH/OBSERVED]`

Recomputed independently from the schema (not carried from the report):

| Metric | Value | Derivation |
|--------|-------|------------|
| Bundle arrays            | **11**  | `len(RegistersBundleArrays)` |
| Register **definitions** | **100** | `70` singleton + `30` in `M2S_Q` |
| Bitfield **definitions** | **325** | sum over all registers |
| Register **instances**   | **550** | `70·1 + 30·16` |
| Register access (def-level)  | `RW=64  RO=27  WO=9`   | |
| Bitfield access (field-level)| `RW=148 RO=152 WO=25`  | |
| `M2S_Q` queues           | **16** (`ArraySize`)   | stride `0x1000` |
| Placeholder-`0xb1` reset | **ABSENT** | 0 hits of `0xb1` in the JSON |

The `0xb1` that appears elsewhere in the corpus is the TPB ISA opcode
`SET_ORDERING_MODE = 0xb1`, unrelated to this CSR block.

---

## 2. Bundle map `[HIGH/OBSERVED]`

Eleven bundle arrays; `M2S_Q` is a 16-deep array, all others singletons. Bundle
base = `AddressOffset`; per-queue absolute = `base + i·0x1000`.

| Bundle base | Bundle | Arr | Bundle size | #reg | Group |
|-------------|--------|-----|-------------|------|-------|
| `0x00000` | `AXI_M2S_MLA`             | 1  | `0x100`  | 3  | MLA outstanding/prefetch **chicken bits** (NC-v3-new) |
| `0x00100` | `AXI_M2S`                | 1  | `0x100`  | 11 | AXI master params (AR/AW for desc-rd / data-rd / comp-wr), outstanding limits |
| `0x00200` | `M2S`                    | 1  | `0x100`  | 23 | engine state machine, error log, FIFO status, stream cfg, pkt-len |
| `0x00300` | `M2S_rd`                 | 1  | `0x040`  | 4  | descriptor-prefetch + data-read FIFO config |
| `0x00340` | `M2S_dwrr`               | 1  | `0x040`  | 2  | DWRR scheduler (byte/pkt mode) |
| `0x00380` | `M2S_rate_limiter`       | 1  | `0x040`  | 3  | global token-bucket rate limiter |
| `0x003c0` | `M2S_stream_rate_limiter`| 1  | `0x040`  | 6  | stream-side token-bucket limiter |
| `0x00400` | `M2S_comp`               | 1  | `0x100`  | 4  | completion controller |
| `0x00500` | `M2S_stat`               | 1  | `0x100`  | 8  | statistics counters |
| `0x00600` | `M2S_feature`            | 1  | `0x100`  | 6  | feature/version + FIFO-size readback |
| `0x01000` | `M2S_Q[16]`              | 16 | `0x1000` | 30 | **per-queue** descriptor-ring programming surface |

`M2S_Q[15]` last register (`TDRDTP` @ bundle-offset `0xe4`) lands at abs
`0x100e4`, well inside the `0x20000` window; **no bundle overlap**
(HIGH/OBSERVED).

---

## 3. Physical placement & multiplicity `[HIGH/OBSERVED]`

From `address_map_flat.yaml`. One UDMA channel occupies a contiguous APB
sub-window; M2S is always its first `0x20000`:

```
+0x000000  UDMA_M2S      size 0x20000   (THIS block)
+0x020000  UDMA_S2M      size 0x18000
+0x038000  UDMA_GEN      size 0x04000
+0x03C000  UDMA_GEN_EX   size 0x04000
+0x040000  MISC          size 0x04000
```

Sample absolute base: `APB_SE_0_SDMA_0_UDMA_M2S = 0x0000_0100_2000_0000`.
The SoC instantiates **280** `*_UDMA_M2S` blocks (264 unicast: 256 SE-attached
+ 8 IO H2D/D2H; 16 broadcast: 2 per cluster × 8 clusters). Each instance owns
its own **16** queues → a very large pool of outbound DMA rings. See the SDMA
address windows in [sdma-windows-apb](../../dma/sdma-windows-apb.md).

---

## 4. Descriptor-ring programming (the per-queue surface, `M2S_Q[i]`) `[HIGH/OBSERVED]`

Each of the 16 queues owns **two rings**: a **TX descriptor ring**
(host→engine, software-produced) and a **TX completion ring** (engine→host).
**All pointers are in units of descriptors, not bytes** (every
`*_descriptor_offset` field says so). Ring base addresses enforce **64 B
alignment** (`TDRBP_low[5:0]` is read-only zero). Per-queue absolute offset =
`0x1000 + i·0x1000 + reg_off`.

### 4.1 TX descriptor ring (outbound work ring)

| `+off` (Q-rel) | Register | Field | Acc | Role |
|--------|----------|-------|-----|------|
| `0x28` | `TDRBP_low`  | `addr[31:6]` (`[5:0]`=0, 64 B) | RW | ring base `[31:6]` |
| `0x2c` | `TDRBP_high` | `addr[31:0]`  | RW | ring base `[63:32]` |
| `0x30` | `TDRL`       | `descriptor_offset[23:0]` | RW | ring **length** (descriptors) |
| `0x34` | `TDRHP`      | `ring_id[31:30]`, `descriptor_offset[23:0]` | **RO** | **head**: next descriptor the engine prefetches; advances as the DMA reads valid descriptors from host RAM |
| `0x38` | `TDRTP_inc`  | `val[23:0]` | RW | write **N** to advance the tail by N descriptors → **THIS IS THE DOORBELL** |
| `0x3c` | `TDRTP`      | `ring_id`, `descriptor_offset[23:0]` | **RO** | **tail**: next free descriptor slot in host memory |
| `0x40` | `TDCP`       | `ring_id`, `descriptor_offset[23:0]` | **RO** | **current**: first descriptor in the prefetch FIFO (next for the scheduler) |
| `0xe0` | `TDRDTP_inc` | `val[23:0]` | **WO** | advance the independent **DATA** tail pointer (NC-v3 "enhanced prefetch") |
| `0xe4` | `TDRDTP`     | `ring_id`, `descriptor_offset[23:0]` | **RO** | **data tail**: last descriptor whose data is ready to read |

`TDRTP_inc` is **write-N-to-advance** (an increment register), not an absolute
write: the hardware computes `Q_TDRTP += val`. This is the launch primitive —
after a producer writes N descriptors into the ring, a single write of `N` to
`TDRTP_inc` kicks the engine. `TDRHP`, `TDRTP`, `TDCP`, `TCRHP`, `TDRDTP` all
carry `ring_id[31:30]` (reset `0x1`); the ring-id is the wrap/generation tag the
engine checks against the descriptor's ring-id bit
(`M2S.err_log_mask.pref_ring_id` flags a mismatch).

> **`ring_id` reset, expressed two ways.** The schema writes the field-relative
> reset `ring_id = 0x1` ([31:30]); the C headers write the full word
> `TDRHP_RESET_VALUE = 0x40000000` (= `1 << 30`). Same value, different
> projection — not a divergence (HIGH/OBSERVED, both headers agree).

### 4.2 TX completion ring (engine → host writeback)

| `+off` | Register | Field | Acc | Role |
|--------|----------|-------|-----|------|
| `0x44` | `TCRBP_low`      | `addr[31:6]` | RW | completion ring base `[31:6]` (64 B) |
| `0x48` | `TCRBP_high`     | `addr[31:0]` | RW | completion ring base `[63:32]` |
| `0x4c` | `TCRHP`          | `ring_id`, `descriptor_offset[23:0]` | **RO** | completion head: next descriptor the completion controller updates |
| `0x50` | `TCRHP_internal` | `ring_id`, `descriptor_offset[23:0]` | **RO** | internal mirror of `0x4c` |

### 4.3 Per-queue config / status / control

| `+off` | Register | Key fields (reset) | Acc | Role |
|--------|----------|--------------------|-----|------|
| `0x00` | `desc_pref_cfg` | `fifo_start_addr[26:16]`, `fifo_depth[11:0]`=`0x080` | RW | per-queue slice of the shared prefetch FIFO |
| `0x20` | `cfg` | `en_pref[16]`, `en_scheduling[17]` (queue **ENABLE**), `AXI_qos[30:28]`, `axi_awcache_comp[27:24]`=3, `force_full_line[31]`, `allow_lt_min_pref[20]`, `pkt_len_offset[15:0]` | RW | **start/stop a queue here** |
| `0x24` | `status` | `q_full[31]`, `q_dmb[30]`, `scheduler[29]`, `prefetch[28]`, `q_used[24:0]` | **RO** | queue status |
| `0x60`–`0x74` | `rate_limit_cfg_*` / `sw_ctrl` / `mask` | `mask.internal_pause_dmb[2]`=1 | RW/WO | per-queue token-bucket limiter + DMB pause path |
| `0x80`–`0x8c` | `dwrr_cfg_1..3` / `dwrr_sw_ctrl` | `dwrr_cfg_2.q_qos[7:0]`, `dwrr_cfg_3.weight[7:0]`, `dwrr_cfg_1.strict[24]` | RW/WO | per-queue DWRR weight/QoS/strict |
| `0xa0` | `comp_cfg` | `en_comp_ring_update[0]`, `dis_comp_coal[1]` | RW | enable/coalesce completion writeback |
| `0xb0` | `q_sw_ctrl` | `rst_q[8]`, `rst_data_tail_ptr[4]`, `rst_current_ptr[3]`, `rst_head_ptr[2]`, `rst_tail_ptr[1]`, `rst_dmb[0]` | **WO** | reset strobes; `rst_dmb` re-enables a DMB-suspended queue |
| `0xc0` | `q_tx_pkt` | `cnt[31:0]` | **RO** | per-queue transmitted-packet counter |
| `0xd0` | `read_data_snp` | `override_snoop_en[0]`, `snoop_val[1]`=1 | RW | per-queue AXI snoop/coherency override for data reads |

The 16-queue count is corroborated independently by the DGE completion
notification record: `dma_map:16` is a 16-bit bitmap "of 16 DMA engine IDs
used" — exactly one bit per M2S queue
(see [notific-queue](./notific-queue.md), and the
[host descriptor handoff](../../runtime/host-device-descriptor-handoff.md)).

---

## 5. Posting a descriptor + kicking the doorbell (C pseudocode) `[HIGH+MED / OBSERVED+INFERRED]`

Offsets are byte-exact from the schema; the producer side (who writes the
descriptor body) is the **DGE** — the TPB DMA pseudo-ops `GENERATE` a descriptor
into the ring, then a register write bumps `TDRTP_inc` (the tail-pointer-update
binding is stated verbatim in the Cayman TPB ISA; the *exact* GENERATE→DIMPUSH
opcode sequence is INFERRED).

```c
/* M2S register-file base for UDMA instance `n`, queue `q` (0..15).
 * AXI control plane is APB; these are 32-bit MMIO writes. */
#define M2S_Q_BASE(base, q)      ((base) + 0x1000u + (uint32_t)(q) * 0x1000u)

/* Per-queue register offsets (schema-exact, bundle-relative + queue stride). */
enum {
    Q_DESC_PREF_CFG = 0x00, Q_CFG       = 0x20, Q_STATUS    = 0x24,
    Q_TDRBP_LOW     = 0x28, Q_TDRBP_HI  = 0x2c, Q_TDRL      = 0x30,
    Q_TDRHP         = 0x34, Q_TDRTP_INC = 0x38, Q_TDRTP     = 0x3c,
    Q_TCRBP_LOW     = 0x44, Q_TCRBP_HI  = 0x48,
    Q_COMP_CFG      = 0xa0, Q_SW_CTRL   = 0xb0,
};
enum { CFG_EN_PREF = 1u<<16, CFG_EN_SCHED = 1u<<17 };  /* M2S_Q.cfg enables */

/* One-time queue bring-up: program both ring bases, length, completion cfg. */
static void m2s_q_init(volatile char *m2s, unsigned q,
                       uint64_t txr_base, uint32_t ring_len_desc,
                       uint64_t tcr_base)
{
    volatile char *Q = M2S_Q_BASE(m2s, q);

    /* 64 B-aligned descriptor-ring base: [5:0] are read-only zero. */
    mmio_w32(Q + Q_TDRBP_LOW, (uint32_t)txr_base  & 0xffffffc0u);
    mmio_w32(Q + Q_TDRBP_HI,  (uint32_t)(txr_base  >> 32));
    mmio_w32(Q + Q_TDRL,      ring_len_desc & 0x00ffffffu);   /* descriptors */

    /* Completion ring base + enable writeback (coalescing left default-on). */
    mmio_w32(Q + Q_TCRBP_LOW, (uint32_t)tcr_base  & 0xffffffc0u);
    mmio_w32(Q + Q_TCRBP_HI,  (uint32_t)(tcr_base >> 32));
    mmio_w32(Q + Q_COMP_CFG,  0x1u);                          /* en_comp_ring_update */

    /* Enable prefetch + scheduling: queue goes live. */
    mmio_w32(Q + Q_CFG, CFG_EN_PREF | CFG_EN_SCHED);
}

/* Launch: the DGE has already written `n` descriptors into the TX ring at the
 * tail.  Advancing the tail by `n` is the doorbell — the engine prefetches and
 * begins outbound DMA.  TDRTP_inc is increment-by-N, not absolute. */
static inline void m2s_q_ring_doorbell(volatile char *m2s, unsigned q, uint32_t n)
{
    mmio_w32(M2S_Q_BASE(m2s, q) + Q_TDRTP_INC, n & 0x00ffffffu);
}

/* Drain check: head==tail means the engine consumed everything we posted. */
static inline bool m2s_q_idle(volatile char *m2s, unsigned q)
{
    volatile char *Q = M2S_Q_BASE(m2s, q);
    return (mmio_r32(Q + Q_TDRHP) & 0x00ffffffu) ==
           (mmio_r32(Q + Q_TDRTP) & 0x00ffffffu);
}
```

---

## 6. Config / enable & stream routing `[HIGH/OBSERVED]`

**Engine state machine** (`M2S` @ `0x200`):

* `state` (RO): `desc_pref[13:12]`, `data_rd_ctrl[9:8]`, `stream_if[5:4]`,
  `comp_ctrl[1:0]` — the four sub-machine states.
* `change_state` (WO): `normal[0]` (start) / `dis[1]` (stop) / `abort[2]`
  (stop all machines).
* `pref_queue_en.en[15:0]` = `0xFFFF` — all 16 queues prefetch-enabled at reset.
* `cfg_len.max_pkt_size[19:0]` = **`0x10000`** (64 KiB, Cayman); `encode_64k[24]`
  = 1 (length `0x0000` means 64 KiB, not 0).
* `fifo_en` (`0x250`) = `0xFFFFFFFF`; `check_en` (`0x24c`); `clear_ctrl`
  (`0x248`).

**Stream interface** (the M2S pushes read data onto the application stream):

* `stream_cfg.disable_stream[0]`, `rd_mode[1]` (0=cut-through, 1=threshold),
  `rd_th[20:8]`=1 (min beats to start transmission).
* `stream_cfg2.mask_data_over_bcnt[0]` = 1 — zero-fill stream bytes beyond the
  current per-stream byte count.

**Data/packet mode** is selected in the scheduler/limiters:
`M2S_dwrr.cfg_sched.pkt_mode_en[4]` (0=byte, 1=packet),
`M2S_rate_limiter.gen_cfg.pkt_mode_en[24]`. On the SoC side the stream connects
the SDMA to the DGE stream↔AXI bridge and into the TPB engines (see
[udma-hw-engine](../../dma/udma-hw-engine.md)).

---

## 7. Prefetch sub-block `[HIGH/OBSERVED]`

The descriptor-prefetch path is split between the **shared** `M2S_rd` (@`0x300`)
and **per-queue** slices in `M2S_Q.desc_pref_cfg`:

| Reg | Field (reset) | Meaning |
|-----|---------------|---------|
| `M2S_rd.desc_pref_cfg_2` | `max_desc_per_pkt[11:0]`=`0x040`, `pref_force_rr[16]`=1 | max descriptors/packet; force-RR in the prefetch arbiter |
| `M2S_rd.desc_pref_cfg_3` | `pref_thr[15:8]`=`0x10`, `min_burst_above_thr[7:4]`=4, `min_burst_below_thr[3:0]`=1 | fetch threshold + min burst sizes |
| `M2S_rd.data_cfg` | `max_pkt_limit[27:16]`=`0x100`, `data_fifo_depth[12:0]`=`0x8F8` | data-read FIFO sizing (do **not** set depth `0x900`) |
| `M2S_Q.desc_pref_cfg` | `fifo_start_addr[26:16]`, `fifo_depth[11:0]`=`0x080` | per-queue slice of the prefetch FIFO |
| `M2S_feature.reg_1` | `desc_preferch_fifo_depth[11:0]`=`0x400` | HW-capability readback: 1024-descriptor prefetch FIFO |

The shared prefetch FIFO (1024 descriptors total, `M2S_feature.reg_1`) is
carved per-queue via `M2S_Q.desc_pref_cfg.fifo_start_addr` / `fifo_depth`. FIFO
occupancy is observable through `M2S.sel_pref_fifo_status` after selecting the
queue with `M2S.indirect_ctrl.q_num[11:0]`.

### NC-v3 "enhanced prefetch" (`AXI_M2S_MLA` @ `0x0`) `[HIGH/OBSERVED]`

The `AXI_M2S_MLA` bundle is **chicken-bit** tuning, present only in the NC-v3
schema (see §10 per-gen note):

| Reg | Field (reset) | Meaning |
|-----|---------------|---------|
| `cfg_outstanding` | `max_enable[0]`=1 | enables programming `enhanced_ostand_cfg.max_data_rd` |
| `cfg_prefetch` | `enhanced_enable[15:0]`=`0xFFFF`, `data_tail_pointer_enable[31:16]`=0 | turn on **independent descriptor-tail vs data-tail pointers** per queue (drives `TDRDTP_inc`/`TDRDTP`) |
| `cfg_tdr_req_candidate_high_priority` | `enable[15:0]`=0 | per-queue high-priority prefetch candidate |

When `cfg_prefetch.enhanced_enable` is set for a queue, the engine separates the
descriptor tail pointer (`TDRTP`) from the data tail pointer (`TDRDTP`),
allowing software to publish descriptors before their data is ready and advance
the data tail (`TDRDTP_inc`) independently.

---

## 8. Completion sub-block `[HIGH/OBSERVED]`

The completion controller (`M2S_comp` @ `0x400`) coalesces completions per
queue, writes them into the TX completion ring (`TCRBP`/`TCRHP`), then awaits
the application ACK on the stream:

| Reg | Field (reset) | Meaning |
|-----|---------------|---------|
| `cfg_1c` | `comp_fifo_depth[7:0]`=`0x40`, `unack_fifo_depth[19:8]`=`0x100`, `q_promotion[24]`=1, `force_rr[25]`, `q_free_min[31:28]` | completion/unack FIFO sizing + arbiter (reset `0x01010040`) |
| `cfg_coal` | `timer[31:0]`=`0x0186A0` (100000) | completion-coalescing timer (per-queue, same value) |
| `cfg_application_ack` | `tout[23:0]`=`0x0186A0` | ACK timeout from the application stream |
| `cfg_application_ack2` | `err_indication_data_offset[6:2]`=`0x1E`, `mask_err_indication_{tuser,data}` | V3-mode error-bit handling (backward-compat masks) |

Per-queue: `M2S_Q.comp_cfg.en_comp_ring_update[0]` enables writeback for that
queue; `dis_comp_coal[1]` disables coalescing for that queue. The completion
chain is **completion-write → coalesce → application ACK → notification**:
`M2S.err_log_mask.comp_pkt_mismatch[0]` flags a serial-number mismatch between
the head of the unack FIFO and the received ACK. The firmware-visible DGE/DMA
completion is then surfaced as a TPB explicit notification (`dma_map:16`,
`num_packets:12`, `dma_group_id:1`) into a SW notification queue — see
[notific-queue](./notific-queue.md) and the
[descriptor field tables](../../dma/descriptor-ring-field-tables.md).

Statistics (`M2S_stat` @ `0x500`): `tx_pkt`, `tx_bytes_low/high` (64-bit byte
counter), `prefed_desc`, `comp_pkt`, `comp_desc`, `ack_pkts`. Version readback
(`M2S_feature` @ `0x600`): `dma_version.version` = `0x04` in the schema (see the
CORRECTION in §10 — the Maverick C header encodes this word differently).

---

## 9. AXI parameters & error log `[HIGH/OBSERVED]`

`AXI_M2S` (@`0x100`) holds one AR/AW config pair per traffic class. Reset words
cross-checked against the C headers:

| Register pair | Reset (cfg_1 / cfg_2) | Decode |
|---------------|-----------------------|--------|
| `comp_wr_cfg_1/2` (completion **write**) | `0x01030000` / `0x30500000` | awid=0, awcache=3, awburst=1(INCR), awqos=0, awsize=5(256-bit), awprot=3 |
| `data_rd_cfg_1/2` (data **read**) | `0x01030001` / `0x32500000` | arid=1, arcache=3, arburst=1, arqos=2, arsize=5, arprot=3 |
| `desc_rd_cfg_1/2` (descriptor **read**) | `0x01030000` / `0x32500000` | arid=0, arcache=3, arburst=1, arqos=2, arsize=5, arprot=3 |
| `ostand_cfg` (@`0x124`) | `0x20402040` | max_comp_data_wr=0x20, max_comp_req=0x20, max_desc_rd=0x20, max_data_rd=0x40 |
| `enhanced_ostand_cfg` (@`0x128`, NC-v3-new) | `max_data_rd[9:0]`=`0x11F` | cap **287** in NC-v3 (writes above are clamped) |

`comp_wr_cfg_1.awcache` is "re-allocated as unused for completions" — the
*effective* per-queue completion AWCACHE is `M2S_Q.cfg.axi_awcache_comp[27:24]`
(reset 3) instead. Burst-split is governed by `data_rd_cfg.max_AXI_beats`=8 and
`desc_rd_cfg_3.max_AXI_beats`=8 / `desc_wr_cfg_1.min_AXI_beats`=4.

**Error log** (`M2S` @ `0x20c`): `err_log_mask` is a 20-bit enable word covering
four AXI domains × {timeout, response, parity} (prefetch/data/completion/stream),
plus descriptor-integrity errors (`pref_ring_id`, `pref_last`, `pref_first`,
`pref_max_desc_cnt`, `pref_pkt_len_overflow`), `data_buff_parity`, and the
completion serial mismatch `comp_pkt_mismatch[0]`. The faulting descriptor's
four header words are captured in `log_0..log_3` (@`0x214`–`0x220`, RO);
`clear_err_log.clear[0]` (@`0x224`, WO) clears the log.

---

## 10. Per-gen applicability & schema-vs-header cross-validation

### Cross-validation: schema offsets vs C headers `[HIGH/OBSERVED]`

Every common register's `OFFSET_START` in `maverick/al_udma_m2s_regs.h` and
`sunda/udma_m2s_tr.h` is **byte-identical** to the Cayman schema. Verified
ring-pointer offsets (all three sources agree): `desc_pref_cfg`=`0x1000`,
`cfg`=`0x1020`, `status`=`0x1024`, `TDRBP_low`=`0x1028`, `TDRBP_high`=`0x102c`,
`TDRL`=`0x1030`, `TDRHP`=`0x1034`, `TDRTP_inc`=`0x1038`, `TDRTP`=`0x103c`,
`TDCP`=`0x1040`, `TCRBP_low`=`0x1044`, `TCRBP_high`=`0x1048`, `TCRHP`=`0x104c`,
`TCRHP_internal`=`0x1050`, `comp_cfg`=`0x10a0`, `q_sw_ctrl`=`0x10b0`. Reset
words matched across the schema and the generated headers:

| Register | Reset | Cross-check |
|----------|-------|-------------|
| `AXI_M2S.ostand_cfg`   | `0x20402040` | = Maverick `AXI_M2S_OSTAND_CFG_RESET_VALUE` |
| `M2S_comp.cfg_1c`      | `0x01010040` | = Maverick `M2S_COMP_CFG_1C_RESET_VALUE` |
| `M2S_dwrr.cfg_sched`   | `0x00070200` | = Maverick & Sunda `M2S_DWRR_CFG_SCHED_RESET_VALUE` |
| `M2S.pref_queue_en`    | `0x0000FFFF` | = Maverick `M2S_PREF_QUEUE_EN_RESET_VALUE` |
| `M2S_Q.rate_limit_cfg_1` | `0x00FFFFFF` | `max_burst_size` |

### CORRECTION vs SX-CSR-07 — Maverick C header omits the NC-v3 prefetch features `[HIGH/OBSERVED]`

> SX-CSR-07's preamble cites `maverick/al_udma_m2s_regs.h` as the "same block,
> Maverick silicon; identical offsets" corroborating source. That is true for
> the **common** registers, but a direct `rg` of the Maverick header (generated
> 2026/02/20) returns **zero** hits for `M2S_MLA`, `TDRDTP`, `data_tail`,
> `enhanced_ostand`, `cfg_prefetch`, and `cfg_outstanding`. **The
> `AXI_M2S_MLA` bundle and the independent data-tail pointer
> (`TDRDTP_inc`/`TDRDTP`) do not exist in the Maverick C header at all** — they
> are present only in the Cayman/Mariana/Mariana+ JSON schemas. The §10 cross-
> validation here is therefore byte-exact for the **common** register set; the
> NC-v3 enhanced-prefetch registers in §7 are grounded **solely** on the Cayman
> schema (which is the authoritative source for this page) and are *not*
> Maverick-corroborated.

### Per-gen register-map deltas `[HIGH/OBSERVED]`

Re-parsed the four shipped `udma_m2s.json` variants (Cayman + customop
arch-headers Sunda / Mariana / Mariana+):

| Variant | Bundles | Reg defs | Bitfields | `AXI_M2S_MLA` | `TDRDTP` | `M2S_Q` regs |
|---------|---------|----------|-----------|---------------|----------|--------------|
| **Cayman** (NC-v3) | 11 | 100 | 325 | yes | yes | 30 |
| **Mariana** | 11 | 100 | 325 | yes | yes | 30 |
| **Mariana+** | 11 | 100 | 325 | yes | yes | 30 |
| **Sunda** (NC-v2) | 10 | 94 | 316 | **no** | **no** | 28 |

The Cayman schema is **byte-identical to the Mariana / Mariana+ schemas** — this
M2S register file is one shared NC-v3-family map. **Sunda (NC-v2)** is the
older cut: it lacks the `AXI_M2S_MLA` bundle (3 regs), `AXI_M2S.enhanced_ostand_cfg`
(1 reg), and `M2S_Q.TDRDTP_inc`/`TDRDTP` (2 regs) — exactly the 6-register
difference (`94 = 100 − 6`). Sunda ring/AXI offsets and core resets are
otherwise identical (`TDRBP_low`=`0x1028` … `TCRHP`=`0x104c`;
`comp_cfg_1c`=`0x01010040`; `dwrr_cfg_sched`=`0x00070200`).

Confirmed per-silicon reset divergences (the Cayman schema is authoritative for
this page):

| Field | Cayman (this page) | Maverick C header | Sunda C header |
|-------|--------------------|-------------------|----------------|
| `M2S.cfg_len` (`max_pkt_size`) | `0x10000` (64 KiB) | `0x01040000` → 256 KiB | `0x01010000` → 64 KiB |
| `AXI_M2S.ostand_cfg` | `0x20402040` | `0x20402040` | `0x20202040` (lower comp-req) |
| `M2S_Q.cfg` | all-zero | `0xe3640000` (pre-sets force_full_line/qos/awcache) | all-zero |
| `M2S_feature.dma_version` | `version=0x04` | full word `0x10001880` (rev/date packing) | — |

> **CORRECTION (`dma_version`).** SX-CSR-07 §7 reports
> `M2S_feature.dma_version.version = 0x04`, which is literal-correct for the
> Cayman schema. The Maverick C header, however, defines
> `M2S_FEATURE_DMA_VERSION_RESET_VALUE = 0x10001880` — a packed rev/date word,
> not `4`. Pin the version to the **per-silicon build word**, not a bare `0x04`,
> when reasoning across generations.

**v5 / later silicon: INFERRED.** No v5 `udma_m2s` schema or header is present
in the shipped sources. The UDMA M2S block is a renamed-but-structurally-stable
Annapurna IP across the observed generations (NC-v2 Sunda → NC-v3
Cayman/Mariana), so v5 is expected to carry the same ring-pointer geometry and
descriptor model; any v5-specific bundle/reset is **INFERRED/CARRIED** and not
byte-grounded here.

---

## See also

* [UDMA hardware engine](../../dma/udma-hw-engine.md) — the engine behind this CSR surface
* [CSR — UDMA S2M](./udma-s2m.md) — the inbound (stream→memory) sibling block
* [Host ↔ device descriptor handoff](../../runtime/host-device-descriptor-handoff.md) — who produces ring descriptors
* [SDMA address windows](../../dma/sdma-windows-apb.md) — APB placement of the UDMA sub-blocks
* [Descriptor-ring field tables](../../dma/descriptor-ring-field-tables.md) — the descriptor word layout
* [pkl-dma subtree](../address/pkl-dma-subtree.md) — UDMA core geometry / renamed-block anchor
