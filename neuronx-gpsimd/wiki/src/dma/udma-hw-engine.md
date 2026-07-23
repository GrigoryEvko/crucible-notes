# The al_udma Hardware DMA Engine

Register-level reference for the GPSIMD hardware DMA path: the `al_udma`
SoC-mastering tensor DMA (the **SDMA / uDMA**) and the Cadence Tensilica
**iDMA** built into the Vision-Q7 `ncore2gp` core. Both are decoded from the
authoritative RTL register-definition JSON (UDMA-v4 `al_udma_{m2s,s2m,gen}_regs`),
the per-field CSR schema (`udma_m2s.json` / `udma_s2m.json`), and the shipped
Cadence `libidma` source compiled into `libidma.a` — all binary-derived,
redistributable artifacts shipped in the `c10` arch-header tree and the
`ncore2gp` toolchain.

A senior implementer building a Vision-Q7-compatible GPSIMD engine needs **two**
disjoint register maps, two doorbells, two completion mechanisms, and two
interrupt paths. This page keeps them strictly separate — they share no
registers and no interrupts.

> **NOTE — there are two physically distinct DMA engines.**
> *Engine 1*, the **SDMA / uDMA** (`al_udma` v4): a SoC-mastering Annapurna
> core instantiated per DMA channel (DDMA/CDMA). 16 outbound (M2S) + 16 inbound
> (S2M) queues; descriptor ring + hardware completion ring; tail-pointer
> doorbell; the 254-source SDMA trigger set routes **outward** to the host
> MSI-X / management core — *not* the Q7's own interrupts. Moves model tensors,
> custom-op staging buffers, collective data.
> *Engine 2*, the **iDMA**: a Cadence iDMA option inside the `ncore2gp` core
> (`XCHAL_HAVE_IDMA=1`). ONE channel, 32-bit address, 128-bit datapath, 64-byte
> max descriptor, 32 outstanding; its done/error interrupts **are** the Q7
> core's interrupts 35/36. This is the local scratch / I-fetch DMA.

Confidence/evidence tags follow the wiki convention: **HIGH/MED/LOW ×
OBSERVED/INFERRED/CARRIED**. Offsets and field positions tagged HIGH/OBSERVED
are byte/field-exact in a named RTL regdef or per-field CSR; sequencing tagged
MED/INFERRED is reasoned from register semantics, not a cycle-accurate RTL
trace.

Cross-references: the per-bundle CSR field tables live at
[UDMA M2S CSR](../control/csr/udma-m2s.md),
[UDMA S2M CSR](../control/csr/udma-s2m.md), and
[UDMA_GEN / TDMA CSR](../control/csr/udma-gen-tdma.md); the descriptor-builder
and QoS model at [DGE Builder + SDMA QoS/Arbitration](dge-builder-qos.md); the
runtime descriptor handoff at
[Host↔Device Descriptor Handoff](../runtime/host-device-descriptor-handoff.md);
the interrupt triggers at [SDMA Trigger Set](../control/interrupt/sdma-triggers.md);
the descriptor/ring field layouts at
[Descriptor + Ring Field-Table Reference](descriptor-ring-field-tables.md).

---

## 1. SDMA engine layout — the `al_udma` v4 bundle map

The authoritative RTL regdef `al_udma_m2s_regs.json` declares
`UnitName = UDMA_M2S`, `DataWidth = 32`, `AddrWidth = 18`,
`SizeInBytes = 0x40000`, `InterfaceType = APB`, and enumerates **14** register
bundle-arrays. The 0x40000 window is the Maverick value; Cayman and Mariana ship
the same M2S unit at a 0x20000 window (§7). Bundle base offsets are arch-stable
across all generations. [HIGH/OBSERVED — `al_udma_m2s_regs.json`]

### 1.1 M2S (outbound) — 14 bundles

| base | bundle | role |
|---|---|---|
| `0x100` | `AXI_M2S` | AXI master attributes for the egress path (30 regs) |
| `0x200` | `M2S` | **engine-global** control: state machine + error log (§1.3) |
| `0x300` | `M2S_rd` | the prefetch / descriptor-read arbiter (§4) |
| `0x340` | `M2S_dwrr` | DWRR packet scheduler (§4) |
| `0x380` | `M2S_rate_limiter` | global token-bucket rate limiter |
| `0x3c0` | `M2S_stream_rate_limiter` | stream-side token bucket |
| `0x400` | `M2S_comp` | completion arbiter / completion-FIFO (§1.4, §4) |
| `0x500` | `M2S_stat` | statistics counters |
| `0x600` | `M2S_feature` | feature / capability bits |
| `0x700` | `M2S_shadow_access` | indirect (shadow) CSR access window |
| `0x900` | `M2S_resp_err` | AXI response-error capture |
| `0x1000` | `M2S_Q[NUM_OF_Q]` | the 16 per-queue ring banks, `0x1000` stride (§2) |
| `0x38000` | `M2S_DYN_MTU` | dynamic-MTU table |
| `0x39800` | `ap_trfc_gen` | AP traffic generator (test) |

### 1.2 S2M (inbound) — 12 bundles

`al_udma_s2m_regs.json` declares `UnitName = UDMA_S2M`, `SizeInBytes = 0x38000`
(Maverick; Cayman/Mariana 0x18000), 12 bundles. [HIGH/OBSERVED]

| base | bundle | role |
|---|---|---|
| `0x100` | `AXI_S2M` | AXI master attributes for the ingress (write) path |
| `0x200` | `S2M` | engine-global control (state / change_state / err_log) |
| `0x300` | `S2M_rd` | prefetch / descriptor-read arbiter |
| `0x340` | `S2M_wr` | the **write engine** (S2M writes data, not just descriptors) |
| `0x380` | `S2M_comp` | completion arbiter |
| `0x3c0` | `S2M_lma` | the **landing / memory-append** block — S2M-only (13 regs) |
| `0x500`/`0x600` | `S2M_stat` / `S2M_feature` | statistics / capability |
| `0x700`/`0x900` | `S2M_shadow_access` / `S2M_resp_err` | shadow / response-error |
| `0x1000` | `S2M_Q[NUM_OF_Q]` | the 16 per-queue ring banks (§2.2) |
| `0x33000` | `S2M_DYN_MTU` | dynamic-MTU table |

> **GOTCHA — S2M is not a renamed M2S.** S2M *lacks* the `M2S_dwrr` +
> `M2S_rate_limiter` + `M2S_stream_rate_limiter` bundles: inbound DMA is
> QoS-tagged but **not** bandwidth-shaped. It *adds* the `S2M_wr` write engine
> and the `S2M_lma` append block — the RX-only data-landing machinery. The
> per-queue `cfg` consequently carries **three** AXI-write-cache fields
> (`axi_awcache_hdr` / `_data` / `_comp`) because S2M writes header, data, and
> completion (§2.2).

### 1.3 M2S engine-global bundle `@0x200` — state machine + error log

This is the engine-wide arm/abort + error-logging control (not per-queue). The
38-register bundle's primary registers:

| off | register | R/W | meaning |
|---|---|---|---|
| `+0x00` | `state` | RO | four sub-engine state fields: `comp_ctrl[1:0]`, `stream_if[5:4]`, `data_rd_ctrl[9:8]`, `desc_pref[13:12]`, plus `last_change_state_cmd[18:16]` |
| `+0x04` | `change_state` | RW | **the engine arm**: `normal[0]`, `dis[1]`, `abort[2]` |
| `+0x0c` | `err_log_mask` | RW | per-error log/interrupt gate (20 named bits, §1.3a) — 0 = log enabled, 1 = masked |
| `+0x14..0x20` | `log_0..3` | RO | sampled packet header that caused the error |
| `+0x24` | `clear_err_log` | WO | clears the captured header log |
| `+0x28/2c/30` | `data_/header_/unack_fifo_status` | RO | engine FIFO depth |
| `+0x34` | `indirect_ctrl` | RW | selects a queue for the `sel_*` debug reads |
| `+0x38..0x44` | `sel_pref_/comp_fifo_status`, `sel_rate_limit_/dwrr_status` | RO | selected-queue FIFO / token / deficit status |
| `+0x48` | `clear_ctrl` | WO | state-machine + FIFO clear |
| `+0x5C` | `pref_queue_en` | RW | **`en[15:0]`** — the 16-bit per-queue prefetch-enable mask, **reset `0xFFFF`** (all 16 queues prefetch-enabled) |
| `+0x80` | `force_target_addr` | RW | force-target modification control |
| `+0x88` | `gen_cfg` | RW | general engine config |
| `+0xa4` | `queue_isolation` | RW | queue-isolation control |

> **QUIRK — `change_state` is a command, not a state.** Writing `normal[0]=1`
> puts the M2S into Normal/active; `dis[1]` disables; `abort[2]` "stops all
> machines (prefetch, scheduling, completion and stream interface)" (verbatim
> field description). The current per-sub-engine state is read back from `state`
> `@+0x00`, and the last command issued is echoed in
> `state.last_change_state_cmd[18:16]`. All `change_state` fields reset to 0, so
> a cold engine is *not* armed until software writes `normal=1`.
> [HIGH/OBSERVED — `change_state.BitFields`]

#### 1.3a `err_log_mask` — the 20-bit error matrix `@+0x0c`

Each bit gates one error class into the SDMA secondary-interrupt matrix, which
reconciles 1:1 with this register. All reset 0 (logging enabled):

`comp_pkt_mismatch[0]`, `stream_axi_parity[1]`, `stream_axi_response[2]`,
`stream_axi_tout[3]`, `comp_axi_parity[4]`, `comp_axi_response[5]`,
`comp_axi_tout[6]`, `data_axi_parity[7]`, `data_axi_response[8]`,
`data_axi_tout[9]`, `pref_axi_parity[10]`, `pref_axi_response[11]`,
`pref_axi_tout[12]`, `pref_pkt_len_overflow[13]`, `pref_max_desc_cnt[14]`,
`pref_first[15]`, `pref_last[16]`, **`pref_ring_id[17]`** (the BD generation-tag
mismatch), `data_buff_parity[18]`, `internal[31:19]`. [HIGH/OBSERVED —
`udma_m2s.json` M2S.err_log_mask.BitFields]

### 1.4 M2S_comp completion-arbiter bundle `@0x400`

| reg | fields (reset) |
|---|---|
| `cfg_1c` | `comp_fifo_depth[7:0]=0x40`, `unack_fifo_depth[19:8]=0x100`, `q_promotion[24]=1`, `force_rr[25]=0`, `q_free_min[31:28]=0` |
| `cfg_application_ack` | `tout[23:0]=0x0186A0` (= 100000) — the completion-ack timeout |
| `cfg_application_ack2` | `mask_err_indication_data[0]=0`, `mask_err_indication_tuser[1]=0`, `err_indication_data_offset[6:2]=0x1E` |
| `cfg_coal` | completion coalescing config |

The completion arbiter defaults to **QoS** (`force_rr=0`) with in-progress-queue
promotion (`q_promotion=1`). [HIGH/OBSERVED — `udma_m2s.json` M2S_comp]

### 1.5 `UDMA_GEN` channel-global bundle (`al_udma_gen_regs.json`)

`UnitName = UDMA_GEN`, `SizeInBytes = 0x4000`. Holds the channel-wide config the
M2S+S2M rings inherit:

| base | bundle | role |
|---|---|---|
| `0x2100` | `DMA_misc` | misc channel config (9 regs) |
| `0x2200` | `Mailbox[4]` | the cross-mailbox completion-signal path (the `Cross Mail Box 0..3` triggers) |
| `0x2300` | `AXI` | channel AXI config |
| `0x2560` | `vmid` | VMID config |
| `0x2580`/`0x25a0` | `axi_master_shaper` / `axi_slave_shaper` | bandwidth shapers |
| `0x2600` | `VMADDR[NUM_OF_Q]` | per-queue VM address base |
| `0x2900` | `axi_error_detection_table[NUM_OF_AXI_GENERATOR]` | AXI error table |
| `0x3400` | `pmu` | performance-monitor unit |
| `0x3500` | `INTC_shadow_access` | per-engine interrupt-controller window |
| `0x3700` | `axi_outstanding_cnt[NUM_OF_Q]` | per-queue AXI outstanding count |
| `0x3c80` | `axi_queue[NUM_OF_Q]` | per-queue AXI config |
| `0x3f00` | `queue_mapping` | queue→profile mapping |

---

## 2. The per-queue ring register bank — base / head / tail / cfg / enable

Each of the 16 M2S queues owns a `0x1000`-byte bank at `0x1000 + i*0x1000`
(`i = 0..15`). This is **the** software-visible ring control: software programs
the descriptor-ring pointers and writes the tail doorbell; the engine
hardware-advances the head pointers and writes the completion ring. Every offset
is HIGH/OBSERVED in the Maverick RTL regdef; every field is HIGH/OBSERVED in the
Mariana per-field CSR.

### 2.1 M2S_Q[i] — 39 registers

| off | register | R/W | role |
|---|---|---|---|
| `0x00` | `desc_pref_cfg` | RW | `fifo_depth[11:0]` (rst `0x080`), `fifo_start_addr[26:16]` — per-queue prefetch-FIFO carveout |
| `0x04` | `desc_pref_cfg2` | RW | prefetch policy 2 |
| `0x08`/`0x0c` | `desc_pref_fifo` / `hdr_pref_fifo` | RO | prefetch-FIFO state |
| `0x18` | `desc_rd_aruser` | RW | AXI ARUSER for the descriptor read |
| `0x1c` | `cmpl_wr_awuser` | RW | AXI AWUSER for the completion write |
| `0x20` | `cfg` | RW | `pkt_len_offset[15:0]`, `en_pref[16]`, `en_scheduling[17]`, `allow_lt_min_pref[20]`, `axi_awcache_comp[27:24]=3`, `AXI_qos[30:28]` (Mariana; see CORRECTION), `force_full_line[31]` — **`en_pref`+`en_scheduling` are the per-queue enable bits** |
| `0x24` | `status` | RO | `q_used[24:0]`, `q_isolated[26]`, `axi_rd_timeout[27]`, `prefetch[28]`, `scheduler[29]`, `q_dmb[30]` (suspended on Data-Memory-Barrier), `q_full[31]` |
| `0x28` | `TDRBP_low` | RW | TX Descriptor Ring Base Pointer `[31:6]` (64-byte aligned) |
| `0x2c` | `TDRBP_high` | RW | TX Descriptor Ring Base Pointer `[63:32]` |
| `0x30` | `TDRL` | RW | TX Descriptor Ring **length** — field `offset[23:0]`, in descriptors |
| `0x34` | `TDRHP` | RO | TX Descriptor Ring **head** = next descriptor the engine prefetches (hardware-advanced) |
| `0x38` | `TDRTP_inc` | RW | **THE DOORBELL** — field `val[23:0]`. Write `N` to advance the tail by `N` descriptors; this launches the outbound DMA |
| `0x3c` | `TDRTP` | RO | TX Descriptor Tail Pointer (reflects accumulated `_inc` writes) |
| `0x40` | `TDCP` | RO | TX Descriptor **current** = descriptor the engine is executing |
| `0x44`/`0x48` | `TCRBP_low` / `TCRBP_high` | RW | TX Completion Ring Base Pointer |
| `0x4c` | `TCRHP` | RO | TX Completion Ring **head** (hardware-advanced as completion entries are written — what software polls/coalesces) |
| `0x50` | `TCRHP_internal` | RO | internal completion head (pre-coalescing) |
| `0x54` | `gdma_pref` | RO | internal descriptor counter reflected to SW |
| `0x60..0x74` | `rate_limit_cfg_*` | RW | per-queue token-bucket rate limiter |
| `0x80..0x8c` | `dwrr_cfg_1/2/3`, `dwrr_sw_ctrl` | RW | per-queue DWRR weight/QoS |
| `0xa0` | `comp_cfg` | RW | `en_comp_ring_update[0]` (**rst 0 = OFF**), `dis_comp_coal[1]` |
| `0xb0` | `q_sw_ctrl` | WO | `rst_dmb[0]`, `rst_tail_ptr[1]`, `rst_head_ptr[2]`, `rst_current_ptr[3]`, `rst_data_tail_ptr[4]` (Mariana) / `q_isolation[4]` (Maverick), `rst_q[8]` |
| `0xc0` | `q_tx_pkt` | RO | transmitted-packet count |
| `0xd0` | `read_data_snp` | RW | snoop override |
| `0xd4`/`0xd8` | `TDRTP_set` / `TDRHP_set` | WO | absolute-set tail/head (Maverick; §7) |
| `0x100`/`0x104` | `force_target_q_mapping` / `out_of_order` | RW | per-queue mapping / OOO config |

> **CORRECTION — `cfg.AXI_qos` differs Mariana↔Maverick.** The Mariana
> per-field CSR (`udma_m2s.json`) defines `AXI_qos[30:28]` (3-bit, reset 0) with
> `force_full_line[31]`. The Maverick RTL regdef (`al_udma_m2s_regs.json`)
> defines `AXI_qos[31:28]` (**4-bit, reset `0xe`**) and relocates
> `force_full_line` to bit `[18]` (reset 1), inserting `vmid_check_en[19]`,
> `id_priority_filter_en_pref[21]`, `id_priority_filter_en_data[22]`,
> `bw_fix_tx_pkthdr[23]`. An implementer targeting Maverick must use the 4-bit
> `[31:28]` field; the Mariana 3-bit `[30:28]` layout is *not* forward-compatible
> for the top bits. [HIGH/OBSERVED — both regdefs]

> **CORRECTION — `q_sw_ctrl[4]` differs Mariana↔Maverick.** Mariana names bit 4
> `rst_data_tail_ptr` (a fifth per-pointer soft reset); Maverick names the same
> bit `q_isolation`. Trust the per-generation regdef, not a cross-gen carry.
> [HIGH/OBSERVED]

> **NOTE — the enhanced data-tail vs absolute-set delta.** The Mariana M2S_Q
> bank exposes `TDRDTP_inc` / `TDRDTP` (an enhanced *data*-tail pointer in place
> of `TDRTP_set`/`TDRHP_set`); Maverick drops those and adds the absolute-set
> `TDRTP_set@0xd4` / `TDRHP_set@0xd8`. The ring base/head/tail/completion offsets
> (`0x28..0x50`) are identical across both. [HIGH/OBSERVED]

### 2.2 S2M_Q[i] — the inbound ring bank

Byte-for-byte the M2S ring with `TDR→RDR` / `TCR→RCR`, plus S2M-specific
completion and packet-handler registers (Maverick S2M_Q has 36 registers):

| off | register | role |
|---|---|---|
| `0x00`/`0x04` | `desc_pref_cfg` / `desc_pref_cfg2` | prefetch config |
| `0x18`/`0x1c` | `desc_rd_aruser` / `cmpl_wr_awuser` | AXI user fields |
| `0x20` | `cfg` | `axi_awcache_hdr[3:0]=3`, `axi_awcache_data[7:4]=3`, `en_pref[16]`, `en_stream[17]` ("accept pkts from stream"), `allow_lt_min_pref[20]`, `data_force_full_line[23]`, `axi_awcache_comp[27:24]=3`, `AXI_qos[30:28]`, `cmpl_force_full_line[31]` — **three** AXI-write-cache fields |
| `0x24` | `status` | RO ring state |
| `0x28`/`0x2c` | `RDRBP_low` / `RDRBP_high` | RX Descriptor Ring Base Pointer |
| `0x30` | `RDRL` | RX Descriptor Ring length |
| `0x34` | `RDRHP` | RO descriptor head |
| `0x38` | `RDRTP_inc` | **THE RX DOORBELL** — "I posted N empty rx buffers"; advances the S2M tail (the WRITE-leg launch) |
| `0x3c`/`0x40` | `RDRTP` / `RDCP` | RO tail / current |
| `0x44`/`0x48` | `RCRBP_low` / `RCRBP_high` | RX Completion Ring base |
| `0x4c`/`0x50` | `RCRHP` / `RCRHP_internal` | RO completion head |
| `0x54` | `comp_cfg` | `en_comp_ring_update[0]=1` (**rst ON — opposite M2S**), `dis_comp_coal[1]=1`, `first_pkt_promotion[2]=1`, `buf2_len_location[3]=1` |
| `0x58`/`0x5c` | `comp_cfg_2` / `pkt_cfg` | completion-2 / `en_hdr_split[17]`, `force_hdr_split[16]`, `hdr_split_size[15:0]=0x040` (S2M-only header split) |
| `0x60` | `qos_cfg` | `q_qos[7:0]` |
| `0x64`/`0x68` | `q_sw_ctrl` / `q_rx_pkt` | DMB soft control / RX packet count |
| `0x6c`/`0x70` | `RDRTP_set` / `RDRHP_set` | absolute-set (Maverick) |
| `0xd4` | `data_cfg` | RW data config |
| `0xe0..0xec` | `append_orig_addr_low/high`, `append_orig_len`, `append_status` | the `S2M_lma` append session — splice an original buffer addr/len into the landed packet |
| `0x100..0x124` | `write_engine`, `lma_cfg`, `auto_cmpl`, `fill_level_status`, `gdma_pref`, `force_target_q_mapping` | S2M write-engine / LMA control |

### 2.3 Where the software gets these offsets — the HAL ring

The runtime never hard-codes `0x34`/`0x38`; it caches them in the HAL ring
record. The per-queue record holds `{base, size, head_reg_ptr (→ TDRHP/RDRHP
@+0x34), tail_reg_ptr (→ TDRTP/RDRTP @+0x3c), tail_inc_reg_ptr (→
TDRTP_inc/RDRTP_inc @+0x38)}`, computed by the
`tdrv_arch_dma_get_queue_{head,tail,tail_inc}_offset(pcore, eng, ring)`
accessors. The keystone fact from the runtime structures: the head / tail /
tail_inc register pointers live in the **HAL ring**, not in the routing record —
the routing record addresses the HW queue indirectly (the KMD owns the
ring head/tail MMIO). The inline custom-op context caches the two MMIO doorbell
addresses (`m2s_inc_reg` / `s2m_inc_reg`, the `+0x38` doorbells normalized
`−0x1000` from the queue base). [CARRIED — runtime struct reports; see
[Host↔Device Descriptor Handoff](../runtime/host-device-descriptor-handoff.md)]

---

## 3. The submit → execute → complete hardware path (SDMA)

How a written tail-pointer drives the engine through prefetch → BD read →
data move → completion write-back. Each step is anchored to the register that
drives it; the inter-step ordering is RTL-implied dataflow (MED/INFERRED where
the silicon sequencing is reasoned from register semantics, not a cycle trace).

```c
/* ================= STEP 0 — ARM (once per queue, by the HAL/KMD) ===========
 * [HIGH/OBSERVED — every register below is in al_udma_m2s_regs.json] */
#define M2S_Q(i)   (0x1000u + (i)*0x1000u)   /* per-queue bank base */
#define M2S        (0x200u)                   /* engine-global bank  */

wr32(q + 0x28, ring_pa & 0xFFFFFFC0u);        /* TDRBP_low  [31:6] 64-B aligned */
wr32(q + 0x2c, ring_pa >> 32);                /* TDRBP_high [63:32]             */
wr32(q + 0x30, ring_len_descs);               /* TDRL.offset[23:0]              */
wr32(q + 0x44, comp_pa & 0xFFFFFFC0u);        /* TCRBP_low                      */
wr32(q + 0x48, comp_pa >> 32);                /* TCRBP_high                     */
wr32(q + 0x20, (1u<<16) | (1u<<17) | qos<<28);/* cfg.en_pref|en_scheduling|AXI_qos */
wr32(M2S + 0x5C, 0xFFFFu);                     /* pref_queue_en.en = all 16 (rst already 0xFFFF) */
wr32(M2S + 0x04, 0x1u);                        /* change_state.normal[0]=1 -> engine Normal/active */

/* ================= STEP 1 — PRODUCE the BDs in the ring ====================
 * The DGE/Q7 (or the inline dma_data_transfer path) writes 16-byte
 * SDMA_CME_BD_DESC entries at ring_base + tail_idx*16: buf_ptr (+0x08, the SoC
 * addr the engine READS on M2S / WRITES on S2M), WORD0 (+0x00, length_meta +
 * the 2-bit generation/ring tag at byte3 bits[1:0]), WORD1 (+0x04, the CME COPY
 * optype). See descriptor-ring-field-tables.md. [HIGH/OBSERVED desc shape] */

/* ================= STEP 2 — FIRE: the tail-pointer doorbell ================
 * [HIGH/OBSERVED — TDRTP_inc@0x38; the only CPU action that launches the DMA] */
__asm__ volatile ("memw; memw");              /* the FW barrier pair before MMIO */
wr32(q + 0x38, n_descs);                       /* TDRTP_inc.val += n_descs (DOORBELL) */
/* HW: TDRTP (+0x3c) advances by n; now TDRTP != TDRHP -> the engine has work. */
```

```text
=================  STEP 3 — PREFETCH: the engine reads the BDs ahead  ========
The M2S_rd block (@0x300) detects TDRTP > TDRHP and issues AXI descriptor-READ
bursts to fetch BDs from TDRBP + TDRHP*16 into the per-queue prefetch FIFO
(desc_pref_cfg.fifo_depth[11:0]=0x80). As BDs are fetched, TDRHP (+0x34) advances
and the in-flight descriptor is at TDCP (+0x40). The 16-queue prefetch arbiter
(M2S_rd.desc_pref_cfg_2.pref_force_rr[16], reset 1 -> round-robin) picks the next
queue. A BD generation-tag mismatch against the queue's wrap generation latches
err_log_mask.pref_ring_id[17].
  [regs HIGH/OBSERVED; the TDRTP>TDRHP trigger condition INFERRED-HIGH from the
   head/tail/current pointer semantics]

=================  STEP 4 — BD READ -> DATA MOVE  ============================
Per prefetched BD: M2S reads length_meta bytes from buf_ptr over the AXI master
and streams them to the stream/CME path; S2M writes the landed bytes to its
buf_ptr (the S2M_wr write engine @0x340). The DWRR packet scheduler
(M2S_dwrr.cfg_sched: en_dwrr[0] rst 0 -> RR among equal QoS; inc_factor[19:16]=7,
weight_inc[9:8]=2) serves the 16 queues by weight when armed. AXI data faults
latch into the err_log_mask data_axi_{parity,response,tout}[7..9] bits.
  [regs HIGH/OBSERVED; per-BD read->move ordering INFERRED-HIGH]

=================  STEP 5 — COMPLETION WRITE-BACK  ==========================
The M2S_comp/S2M_comp arbiter (@0x400 / @0x380) writes a completion entry to the
completion ring (TCRBP/RCRBP base) and advances TCRHP/RCRHP (+0x4c). Write-back
is GATED by comp_cfg.en_comp_ring_update[0]: S2M default ON (rst 1), M2S default
OFF (rst 0). Coalescing (dis_comp_coal[1]) + cfg_application_ack.tout=0x186A0
batch completions. The completion arbiter defaults to QoS
(M2S_comp.cfg_1c.force_rr[25]=0, q_promotion[24]=1).
  [HIGH/OBSERVED — comp_cfg + cfg_1c fields]
```

> **QUIRK — the inline custom-op path bypasses the completion ring.** For the
> GPSIMD custom-op memcpy staging path, `comp_cfg.en_comp_ring_update` is left at
> its M2S reset 0: no completion-ring entry is written. Instead the engine leaves
> the 2-bit generation tag in the BD's WORD0 byte3 bits[1:0], and software
> **busy-polls** `comp_BD byte3 & 0x3 == expected` before reusing the ring slot —
> synchronous, no interrupt, no notification. The DGE/execute path instead turns
> write-back on and consumes the completion ring + the NOTIFIC notification (§5).

**One-line hardware trace:** `write TDRTP_inc(+0x38)` → `TDRTP` advances → engine
sees `TDRTP > TDRHP` → `M2S_rd` prefetch bursts BDs (`TDRHP` advances, `TDCP` =
in-flight) → data engine reads `buf_ptr`, moves `length_meta` bytes →
`M2S_comp` writes a completion entry (`TCRHP` advances) **or** leaves the gen-tag
→ semaphore / notification (§5) **or** gen-tag busy-poll.

---

## 4. The per-engine arbiters (scheduling hardware)

Field-exact off the Mariana per-field CSR. The reset bias is toward fairness;
QoS is *armed* by clearing `pref_force_rr`, setting `en_dwrr`, and programming
`AXI_qos`/`q_qos`/weight from the DMAQoSClass map (see
[DGE Builder + SDMA QoS](dge-builder-qos.md)).

| arbiter | bundle | RR↔QoS knob (reset) | QoS source |
|---|---|---|---|
| prefetch | `M2S_rd@0x300` | `desc_pref_cfg_2.pref_force_rr[16]` (rst 1 = RR) | `AXI_qos`/`q_qos` |
| pkt sched | `M2S_dwrr@0x340` | `cfg_sched.en_dwrr[0]` (rst 0 = RR), DWRR when 1; `inc_factor[19:16]=7` (128 B), `weight_inc[9:8]=2`, `pkt_mode_en[4]` | dwrr `q_qos`+weight (M2S only) |
| completion | `M2S_comp@0x400` | `cfg_1c.force_rr[25]` (rst 0 = QoS), `q_promotion[24]=1` | `AXI_qos` |
| rate limit | `M2S_rate_limiter@0x380` + `M2S_stream_rate_limiter@0x3c0` | two-level token bucket, masked at reset (M2S only) | — |

> **NOTE — S2M arbiters differ.** S2M keeps `AXI_qos`+`q_qos` but drops the DWRR
> and rate-limiter bundles entirely. Its `S2M_comp.cfg_1c` also lays out
> differently: `desc_size[3:0]=0x4`, `q_promotion[12]` (not bit 24),
> `force_rr[16]` (not bit 25). An implementer cannot copy the M2S_comp field
> positions onto S2M_comp. [HIGH/OBSERVED — `udma_s2m.json`]

---

## 5. Completion signalling — semaphore / notification (SDMA)

Beyond the completion-ring write-back, an SDMA transfer signals software/peers
three ways; the choice is per-path:

**5.1 Generation-tag busy-poll** (custom-op inline staging) — no interrupt, no
notification. The BD's WORD0 byte3 bits[1:0] carry the 2-bit generation tag;
software polls `comp_BD byte3 & 0x3 == expected` before reusing the slot. The
synchronous default for the GPSIMD custom-op memcpy path. [HIGH/OBSERVED — §3
STEP 5]

**5.2 Completion semaphore** (the DGE GENERATE `sem_num`) — the DGE GENERATE op
carries a `sem_num`; on completion the engine increments that semaphore in the
EVT_SEM block (4 sema-ops: READ/SET/INC/DEC). For the P2P RDMA path two
semaphores fire: a local semaphore on local-engine-done (source release) and a
remote semaphore on data-landed-at-peer. [CARRIED — DGE/QoS report; see
[DGE Builder](dge-builder-qos.md)]

**5.3 The NOTIFIC engine → SW notification queue** (the DGE/execute path) — the
DDMA channel embeds a NOTIFIC 10-queue block. On completion the engine emits a
notification record `{dma_map:16 (which of the 16 M2S queues), num_packets:12,
dma_group_id:1}` into a HW notification queue, mirrored by the runtime as a
phase-bit ring. The NOTIFIC engine's own faults (full SW NQ, disabled NQ, full HW
buffer stall/drop, AXI write-resp error, threshold reached, NQ overlap,
multi-stream coalescer) are its 9 edge triggers. This async path is what the
runtime execute flow consumes; the inline custom-op copy polls instead.
[CARRIED — runtime struct + trigger reports; see
[SDMA Trigger Set](../control/interrupt/sdma-triggers.md)]

---

## 6. The iDMA (Xtensa core) engine — register map + submit → complete + ints 35/36

The second DMA engine: the Cadence iDMA built into the `ncore2gp` core. Decoded
from the shipped `libidma` source compiled into the `ncore2gp` `libidma.a`
(6 object members — `idma.o`, `idma_legacy.o`, `idma_os.o`, `data.o`,
`idma_logbuf.o`, `idma_print.o`; 41 defined symbols, 31 text functions, 29
matching `idma`). `core-isa.h` pins the option:

```c
/* ncore2gp xtensa/config/core-isa.h  [HIGH/OBSERVED] */
#define XCHAL_HAVE_IDMA               1
#define XCHAL_IDMA_NUM_CHANNELS       1   /* ONE channel */
#define XCHAL_IDMA_ADDR_WIDTH         32  /* 32-bit address */
#define XCHAL_IDMA_DATA_WIDTH         128 /* 128-bit datapath */
#define XCHAL_IDMA_DESC_SIZE          64  /* 64-byte max descriptor */
#define XCHAL_IDMA_MAX_OUTSTANDING_REQ 32 /* 32 outstanding */
#define XCHAL_DATARAM0_HAVE_IDMA      1   /* targets DataRAM0 */
#define XCHAL_IDMA_CH0_DONE_INTERRUPT 35  /* int 35 = DONE */
#define XCHAL_IDMA_CH0_ERR_INTERRUPT  36  /* int 36 = ERROR */
#define XCHAL_HAVE_XEA3               1   /* XEA2=0 -> the WIDE iDMA error set */
```

### 6.1 iDMA MMIO register map (`idma.h` `IDMA_REG_*`)

| off | register | role |
|---|---|---|
| `0x00` | `SETTINGS` | `max_block_size[3:2]`, `halt_on_ocd[6]`, `FETCH_START[7]`, `outstanding[13:8]` (mask `0x3F`), `bundle[19:16]=4` |
| `0x04` | `TIMEOUT` | `clock[2:0]`, `threshold[28:3]` |
| `0x08` | `DESC_START` | descriptor-ring start address (the analogue of `TDRBP`) |
| `0x0C` | `NUM_DESC` | RO — remaining descriptors; **the engine counts this DOWN** |
| `0x10` | `DESC_INC` | **THE iDMA DOORBELL** — advance the descriptor count by N |
| `0x14` | `CONTROL` | `0x1` = enable/arm, `0x2` = pause, `0x0` = clear/disable |
| `0x18` | `USERPRIV` | user-privilege attributes |
| `0x40` | `STATUS` | `state[2:0]` (mask `0x7`) + the error-codes field (upper bits) |
| `0x44` | `CURR_DESC` | descriptor currently executing (analogue of `TDCP`) |
| `0x48`/`0x4C`/`0x50` | `DESC_TYPE` / `SRC_ADDR` / `DST_ADDR` | live transfer type/addresses |

`STATUS.state`: `IDLE=0` (done + disabled), `STANDBY=1`, `BUSY=2`, `DONE=3`
(done + enabled), `HALT=4` (disabled mid-copy), `ERROR=5`, mask `0x7`.
[HIGH/OBSERVED — `idma.h` `IDMA_STATE_*`]

> **NOTE — SETTINGS field encoding** (`idma_internal.h`): `MAX_BLOCK_SIZE_SHIFT
> 2`/mask 3 (`MAX_BLOCK_2/4/8/16`), `OUTSTANDING_REG_SHIFT 8`/mask `0x3F`,
> `HALT_ON_OCD_SHIFT 6`, `FETCH_START 7`, `BUNDLE_SHIFT 16`/mask `0xF`. TIMEOUT:
> `CLOCK_SHIFT 0`/mask 7, `THRESHOLD_SHIFT 3`/mask `0x1FFFFFFF`. [HIGH/OBSERVED]

### 6.2 iDMA descriptor formats (`idma_desc_struct`)

```c
/* 1D, 16 bytes */            struct idma_desc_struct   { u32 control, src, dst, size; };
/* 2D, 32 bytes */            struct idma_2d_desc_struct { ... + u32 src_pitch, dst_pitch, nrows, reserved1; };
/* 64-byte (the ncore2gp config) */
struct idma_desc64_struct {   u32 control, src, dst, size;
                              u32 src_pitch, dst_pitch, nrows, reserved1;
                              i32 src_tile_pitch, dst_tile_pitch; u32 ntiles, pred_mask;
                              u32 reserved2, reserved3, ext_src, ext_dst; };  /* 16 words */
```

The `control` word bits (`idma.h`):
- `DESC_NOTIFY_W_INT = 0x80000000` — **trigger interrupt on completion** (the
  per-descriptor bit that raises int 35 when this BD finishes)
- `DESC_IDMA_TRIG_OUT = 0x40000000`, `DESC_IDMA_TRIG_WAIT = 0x20000000` —
  send/await an external trigger
- `DESC_IDMA_PRIOR_H = 0x08000` (QoS high), `DESC_IDMA_NOPRIV_SRC/DST` —
  privilege/QoS attributes
- `DESC64_IDMA_AXI_ATTRIBUTE(val) = ((val & 0xF) << 24)` — bits `[27:24]` AXI
  attribute; `DESC64_IDMA_LONG_ADDR = 0x400` (bit 10) selects the `ext_src`/
  `ext_dst` long-address words. [HIGH/OBSERVED]

### 6.3 iDMA submit → complete sequence (`idma.c`)

```c
/* ===== INIT/ARM (idma_init ~:615-628) ===== [HIGH/OBSERVED] */
settings  = (bsz       & 3u)    << 2;          /* IDMA_MAX_BLOCK_SIZE_SHIFT */
settings |= (maxpifreq & 0x3Fu) << 8;          /* IDMA_OUTSTANDING_REG_SHIFT */
settings |= (haltable  & 1u)    << 6;          /* IDMA_HALT_ON_OCD_SHIFT */
settings |= (4u                 << 16);        /* IDMA_BUNDLE_SHIFT, value 4 */
WRITE_IDMA_REG(ch, IDMA_REG_SETTINGS, settings);
WRITE_IDMA_REG(ch, IDMA_REG_TIMEOUT,  (ticks & 7u) | ((timeout & 0x1FFFFFFFu) << 3));

/* fetch-start arm (idma_set_start_address_i ~:171-177) */
settings  = READ_IDMA_REG(ch, IDMA_REG_SETTINGS);
settings |= (1u << IDMA_FETCH_START);          /* bit 7 */
WRITE_IDMA_REG(ch, IDMA_REG_SETTINGS,   settings);
WRITE_IDMA_REG(ch, IDMA_REG_DESC_START, (u32)&task->desc);   /* ring base */
/* CONTROL=0x1 arms; 0x0 clears; 0x2 pauses */

/* ===== FIRE (idma_schedule_task ~:507-535) ===== [HIGH/OBSERVED] */
task->status = task->num_descs;
g_idma_cntrl[ch].num_outstanding += (u32)task->num_descs;
XT_MEMW();                                      /* the memory-write barrier before the doorbell */
WRITE_IDMA_REG(ch, IDMA_REG_DESC_INC, (u32)task->status);   /* *** THE DOORBELL = num_descs *** */

/* ===== POLL (idma_task_processing_i ~:278-281) ===== [HIGH/OBSERVED] */
rem_descs   = READ_IDMA_REG(ch, IDMA_REG_NUM_DESC);          /* counts DOWN */
idma_status = READ_IDMA_REG(ch, IDMA_REG_STATUS);
if ((idma_status & IDMA_STATE_MASK) == IDMA_STATE_ERROR) idma_error = 1;
num_completed = num_outstanding - rem_descs;                /* descriptors retired since last poll */
```

The hardware fetches BDs from `DESC_START` (`NUM_DESC` counts down, `CURR_DESC`
tracks the in-flight BD), moves `size` bytes src→dst over the 128-bit AXI path
(up to 32 outstanding), and on the last BD — or any BD with `DESC_NOTIFY_W_INT`
set — raises int 35.

> **NOTE — `XT_MEMW()` is the iDMA's barrier-before-doorbell**, the structural
> analogue of the SDMA `memw; memw` pair before `TDRTP_inc`. It is emitted only
> on XEA2/XEA3 cores (`#if XCHAL_HAVE_XEA2 || XCHAL_HAVE_XEA3`) — i.e. it *is*
> present on `ncore2gp`. [HIGH/OBSERVED — `idma.c:531`]

### 6.4 iDMA done/error interrupt path — ints 35/36 (`idma_xtos.c`)

```c
/* REGISTRATION (idma_register_interrupts, idma_xtos.c:33-49) [HIGH/OBSERVED] */
doneint = IDMA_DONE_INT(ch);   /* = XCHAL_IDMA_CH0_DONE_INTERRUPT(35) + ch */
errint  = IDMA_ERR_INT(ch);    /* = XCHAL_IDMA_CH0_ERR_INTERRUPT (36) + ch */
xtos_set_interrupt_handler(doneint, done_handler, (void*)ch, NULL);
xtos_interrupt_enable(doneint);
xtos_set_interrupt_handler(errint,  err_handler,  (void*)ch, NULL);
xtos_interrupt_enable(errint);
```

Both handlers funnel into one routine:

```c
/* int 35 -> idma_task_done_intr_handler (idma.c:355) */
/* int 36 -> idma_task_err_intr_handler  (idma.c:365) */
/* both call: idma_task_processing_i(ch) (idma.c:266) */

/* completion walk (idma_task_processing_i / complete_desc_i) [HIGH/OBSERVED] */
num_completed = num_outstanding - rem_descs;       /* from NUM_DESC */
for (task = oldest_task; num_completed && !idma_error; task = task->next_task) {
    task->status -= consumed;                      /* per-task remaining-desc count */
    if (task->cb_func != NULL) (*task->cb_func)(task->cb_data);   /* per-task callback */
    if (task->status == IDMA_TASK_DONE)            /* == 0 */
        idma_thread_unblock(task->thread_id);      /* wake the sleeping owner thread */
}

/* error path (idma_process_error_i / set_err_details, idma.c:230-257) */
error->err_type = (STATUS >> IDMA_ERRCODES_SHIFT); /* shift = 12 on XEA3 */
error->currDesc = READ_IDMA_REG(ch, IDMA_REG_CURR_DESC);  /* 0x44 */
error->srcAddr  = READ_IDMA_REG(ch, IDMA_REG_SRC_ADDR);   /* 0x4C */
error->dstAddr  = READ_IDMA_REG(ch, IDMA_REG_DST_ADDR);   /* 0x50 */
task->status = IDMA_TASK_ERROR;
if (g_idma_cntrl[ch].err_cb_func != NULL)          /* a SEPARATE per-channel error callback */
    g_idma_cntrl[ch].err_cb_func(&g_idma_cntrl[ch].error);
abort_tasks(task->next_task);                      /* abort the rest of the chain */
```

> **CORRECTION — XEA3 has the WIDE 20-code error set, not 14.** Because
> `ncore2gp` is `XCHAL_HAVE_XEA3=1`, `idma.h` compiles the `#else` (non-XEA2)
> branch: `IDMA_ERRCODES_SHIFT = 12`, `IDMA_ERRORS_MASK = 0xFFFFF000`, and
> **20** error codes (the XEA2 branch shifts by 18 / masks `0xFFFC0000` and
> defines only 14). The XEA3 codes, as masked field values, are:
> `FETCH_ADDR=0x80000`, `FETCH_DATA=0x40000`, `READ_ADDR=0x20000`,
> `READ_DATA=0x10000`, `WRITE_ADDR=0x08000`, `WRITE_DATA=0x04000`,
> `REG_TIMEOUT=0x02000`, `TRIG_OVFL=0x01000`, `DESC_OVFL=0x00800`,
> `DESC_UNKNW=0x00400`, `DESC_UNSUP_DIR=0x00200`, `DESC_BAD_PARAMS=0x00100`,
> `DESC_NULL_ADDR=0x00080`, `DESC_PRIVILEGE=0x00040`, `DESC_MMUMPU=0x00020`,
> `BIT_VECT=0x00010`, `VECT_OTHER=0x00008`, `VECT_DATA=0x00004`,
> `SRC_MMUMPU=0x00002`, `DST_MMUMPU=0x00001`. [HIGH/OBSERVED — `idma.h`,
> `idma_internal.h`]

> **NOTE — int 35/36 are the only DMA interrupts the Q7 core takes.** In the
> 37-interrupt XEA3 table both are level-1 EXTERN edges
> (`XCHAL_INT35_TYPE=IDMA_DONE`, `INT36=IDMA_ERR`); the XEA3 dispatch routes both
> to the xtos handler table above. The SDMA's 254 triggers never reach the Q7 —
> they route to the host MSI-X / management core (§7).

---

## 7. The SDMA interrupt path + cross-generation register deltas

The SDMA does **not** interrupt the Q7 core; its 254-source trigger set routes
outward. The completion + error structure, reconciled with the register map:

- **Completion triggers.** The primary cause vector carries the per-queue done
  events: 16 RX/S2M completion-queue bits (the `RCRHP` advances) and 16 TX/M2S
  completion-queue bits (the `TCRHP` advances), plus the `Cross Mail Box 0..3`
  bits (the `gen_regs` `Mailbox[4]` @0x2200) and a software timer. Each
  completion-ring write-back (§3 STEP 5) that crosses a queue's coalescing
  threshold raises its completion bit. [CARRIED — trigger report]
- **Error triggers.** The M2S/S2M error matrix reconciles 1:1 with the
  `err_log_mask` register (§1.3a) + the S2M error register: AXI
  {parity,response,timeout} across completion/data/prefetch/stream domains,
  buffer parity, the `pref_ring_id[17]` gen-tag mismatch, plus the 9 NOTIFIC
  faults. All gated by the per-error `err_log_mask` bits. [CARRIED]
- **Routing.** The 254 triggers fill an errtrig pair, delivered as MSI-X to the
  PCIe host or wire-ORed (NO_MSIX) upward into the management interrupt apex.
  See [SDMA Trigger Set](../control/interrupt/sdma-triggers.md) for the full
  taxonomy and the apex bit-reversal hazard. [CARRIED — final apex→core hop is
  firmware-owned, MED]

**Cross-generation register deltas** (Sunda v2 → Cayman → Mariana → Maverick).
The per-queue ring offsets (`0x28 TDRBP` … `0x38 TDRTP_inc` … `0x44 TCRBP`) are
arch-stable; the deltas observed in the regdef JSON:

| delta | Cayman/Mariana | Maverick |
|---|---|---|
| M2S window | `0x20000` | `0x40000` (doubled) |
| S2M window | `0x18000` | `0x38000` |
| tail-set regs | `TDRDTP_inc`/`TDRDTP` (enhanced data tail) | `TDRTP_set@0xd4`/`TDRHP_set@0xd8` (absolute set) |
| `cfg.AXI_qos` | `[30:28]` 3-bit, rst 0 | `[31:28]` 4-bit, rst `0xe` (§2.1 CORRECTION) |
| `q_sw_ctrl[4]` | `rst_data_tail_ptr` | `q_isolation` |
| per-queue bank stride | `0x1000` | `0x1000` (unchanged) |

The iDMA core option is identical across generations (it is a `core-isa`
property; ints 35/36 fixed). The SDMA trigger *count* drifts per generation but
the M2S/S2M error→completion register surface is stable; the deltas live in the
fabric-shim packaging and silicon extras. [HIGH/OBSERVED — regdef windows;
trigger-count drift CARRIED]

---

## 8. Confidence summary

**HIGH/OBSERVED.** The M2S/S2M/gen bundle layout (14/12/24 bundles); the
39-register M2S_Q and 36-register S2M_Q per-queue ring banks with every offset
(`al_udma_{m2s,s2m,gen}_regs.json` RTL regdef); every key bitfield — `cfg`
{`en_pref[16]`, `en_scheduling[17]`, `AXI_qos`, `force_full_line`}, `status`
{`prefetch[28]`, `scheduler[29]`, `q_dmb[30]`, `q_full[31]`}, `comp_cfg`
{`en_comp_ring_update`: M2S rst0 / S2M rst1}, `q_sw_ctrl` {`rst_*`},
`change_state` {`normal[0]`, `dis[1]`, `abort[2]`}, `state` (4 sub-engine
fields), `pref_queue_en` {`en[15:0]`=0xFFFF}, `M2S_comp.cfg_1c` {`force_rr[25]`=0,
`q_promotion[24]`=1, `comp_fifo_depth`=0x40}, `M2S_rd.desc_pref_cfg_2.pref_force_rr`
[16]=1, `M2S_dwrr.cfg_sched` {`en_dwrr[0]`=0, `inc_factor`=7, `weight_inc`=2},
the 20-bit `err_log_mask` (Mariana per-field CSR); the doorbell semantics
`TDRTP_inc`/`RDRTP_inc@+0x38`; the entire iDMA MMIO map + descriptor formats +
the `SETTINGS`/`CONTROL`/`DESC_INC` submit sequence + the `STATUS` state machine
+ the int-35/36 registration→callback chain + the 20-code XEA3 error set
(`libidma` source, `core-isa.h`, the `ncore2gp` `libidma.a`).

**MED/INFERRED.** The §3 step-to-step hardware sequencing (each driving register
is OBSERVED; the RTL ordering `TDRTP>TDRHP → prefetch → data move → comp
write-back` is register-semantics-implied dataflow, not a cycle-accurate trace);
the Maverick-vs-Cayman tail-set identity.

**CARRIED.** The HAL-ring location of head/tail/tail_inc and the
`tdrv_arch_dma_get_queue_*` accessors (runtime struct reports); the §5.2/5.3
semaphore + NOTIFIC paths; the §7 trigger taxonomy and the apex→management-core
final hop (firmware-owned).
