# Interrupts — SDMA Trigger Set

The **SDMA / uDMA descriptor-engine interrupt-source table**: a flat, RTL-ordered
bus of **254 trigger sources** covering the Memory-to-Stream (M2S) and
Stream-to-Memory (S2M) descriptor engines, the GEN common half, the per-channel
DMA completion/error matrix, the NOTIFIC instruction-notification queue, the FIS
fabric shim, and the internal compute/buffer fault engines (ELA/ERG/DRE/CCE).
This page is the leaf-source companion to the four CSR pages that describe the
engines these triggers *report on* — [M2S ring set](../csr/udma-m2s.md),
[S2M RX rings](../csr/udma-s2m.md), [GEN/GEN_EX/MISC_SDMA_APP](../csr/udma-gen-tdma.md) —
and the INTC unit they *latch into* — [the 4-group INTC](../csr/intc-4group.md).

Everything below is derived from one byte-exact shipped artifact — the
RTL-generated trigger table `sdma_triggers.yaml` — plus the INTC/CSR register
schemas in this repository. Recovered names, descriptions and field values quoted
here are the artifact's own verbatim text. No external source tree is referenced.

> **Confidence legend.** `HIGH` = literal value read from the YAML / re-counted by
> `yq`. `MED` = inference from naming + CSR cross-reference, well supported.
> `LOW` = plausible, flagged, not stated by the artifact. `OBSERVED` = read from a
> shipped file. `INFERRED` = reasoned from corroboration. `CARRIED` = forwarded
> from a sibling page.

> **CORRECTION — v1 contamination scrubbed (audit).** An earlier backing extract
> of this table was *defective*: its first ~208 lines had been overwritten with an
> unrelated Xtensa-NX 'Cayman' NeuronX-core CSR / debug-trace-OCD-CoreSight
> register map, with the real SDMA enumeration buried near the tail. **This page is
> authored fresh off the shipped `sdma_triggers.yaml` and contains ZERO of that
> contaminating content.** Provenance was re-grounded by counting entries directly
> (`yq 'length'` → `254`) and re-deriving every per-group count from the YAML
> bytes, never from a decompile grep. `[HIGH/OBSERVED]`

---

## 0. Provenance & the byte-exact source

| Role | Artifact | Count |
|------|----------|-------|
| **Primary authority** (cayman, this page) | `…/cayman-arch-regs_tgz/intc/sdma_triggers.yaml` | **254** |
| Cross-gen copy (v5/maverick header) | `…/arch-headers/maverick/intc/sdma_triggers.yaml` | 256 |
| Cross-gen copy (sunda) | `…/arch-headers/sunda/intc/sdma_triggers.yaml` | 239 |

`cayman` ships **only** inside the nested `cayman-arch-regs_tgz`; no
`mariana` / `mariana_plus` / `tonga` `sdma_triggers.yaml` ships (`fd`-verified).
The cayman copy is the byte-exact authority for this page.
`[HIGH/OBSERVED]`

> **NOTE — v5/MAVERICK status.** The maverick (NC-v5/MAVERICK) `sdma_triggers.yaml`
> header is **OBSERVED** in full (its 256-entry body parses cleanly), but the
> mapping of that 256-source set onto *v5 silicon* interrupt behaviour beyond the
> table itself is **INFERRED** — only the cayman table is the byte-exact authority
> validated against the M2S/S2M/GEN CSR schemas in this repo. Treat the cross-gen
> deltas in §8 as `[HIGH/OBSERVED]` for *table content* and `[INFERRED]` for v5
> *runtime semantics*.

---

## 1. The minimal per-entry schema (5 keys)

Every one of the 254 entries carries **exactly five keys** — no more, no fewer.
Key union enumerated directly (`yq '[.[] | keys[]] | unique'`):

```yaml
- trigger: sdma_notific_intr[0]
  name: notific_intr_0
  description: An instruction notification was unable to write due to a full
    instruction SW notification queue. Does NOT fire if SW_backpressure is 0 and
    ignore_full is 1. The CSR nq_full_status will indicate which SW NQs are full.
  needs_cdc: false
  edge_triggered: true
```

| Key | Type | Semantics | `[grade]` |
|-----|------|-----------|-----------|
| `trigger` | string `bus[bit]` | The INTC trigger-bus identity. **YAML order == bit order**; the 254 entries fill the INTC trigger inputs contiguously (see §6). | `[HIGH/OBSERVED]` |
| `name` | string | Per-source identity (e.g. `sdma_main_intr_3`, `notific_intr_0`). | `[HIGH/OBSERVED]` |
| `description` | string \| `null` | Human prose; **4 entries are `null`** (the `sdma_ela_0..3` ELA tap points). | `[HIGH/OBSERVED]` |
| `needs_cdc` | bool \| `null` | "must this source cross a clock domain into the INTC?" **NEVER `true`** here — `0` true / `101` false / `153` null = **0 CDC crossings**. SDMA blocks do their own CDC internally before the trigger bus. | `[HIGH/OBSERVED]` |
| `edge_triggered` | bool | `true` = edge (one-shot event/completion/error pulse), `false` = level (sticky status). **223 edge / 31 level.** | `[HIGH/OBSERVED]` |

This is the **MINIMAL-5** schema variant shared by the FAMILY-A leaf tables
(SDMA + IO-fabric + d2d + the TOP_SP bulk; see [schema atlas](schema-atlas.md)). It
deliberately **omits** keys carried by richer tables:

* No `msix_mask` — MSI-X masking for SDMA lives in the **INTC vector table**, not at
  the leaf (one MSI-X entry per trigger; see [INTC 4-group §6c](../csr/intc-4group.md)).
* No `nmi_mask` / `nmi_msix_mask` — those are apex-rollup (RICH-9) keys only.
* No `source_clock` / `source_reset_n` — consistent with `needs_cdc` never firing
  (SDMA does its CDC internally).
* No `tog2pul_only`, `source_block`, `source_path`, `critical` — TPB/apex keys.

> **QUIRK — `needs_cdc` declared but never `true` (H2).** All 254 are `false`/`null`,
> yet the key exists on every entry. A tool that gates CDC synchronisation on the
> key's *presence* (rather than its *value*) would over-synchronise. **Trust the
> value (always false), not the presence.** The INTC's own
> `int_cdc_bypass_grp`/`int_cdc_bypass_active` registers note that *"the CDC syncro
> is still in the path regardless"* (see [INTC 4-group](../csr/intc-4group.md)), so
> the leaf can declare no crossing while the INTC still resynchronises. `[HIGH/OBSERVED]`

---

## 2. Sub-block grouping — the 9 banner sections

The YAML is partitioned by **9 `# NAME ===` banner comments**. Banner order **is**
the contiguous INTC trigger-bus bit order. Per-section index ranges (0-based,
`yq to_entries`), every count re-derived from the YAML and summed to 254:

| # | Banner | YAML idx | Cnt | Trigger bus(es) | Edge/Level |
|---|--------|----------|-----|-----------------|------------|
| 1 | **NOTIFIC** | `0..8` | 9 | `sdma_notific_intr[0..8]` | edge |
| 2 | **UDMA** | `9..57` | 49 | `sdma_main_intr[0..48]` (primary cause vector) | edge |
| | | `58..137` | 80 | `sdma_secondary_intr[0..79]` (error matrix) | edge |
| 3 | **FIS** | `138..142` | 5 | `fis_cntrl_intr[0..4]` | edge |
| | *(FIS_ERRTRIG)* | `143..192` | 50 | `fis_errtrig_intr[0..49]` | edge |
| | | `193..198` | 6 | `sdma_sprot_intr[0..5]` | edge |
| 4 | **ELA** | `199..202` | 4 | `sdma_ela[0..3]` | **level** |
| 5 | **ERG** | `203` | 1 | `sdma_ela_erg` | **level** |
| 6 | **DRE** | `204..206` | 3 | `sdma_dre_{erg,mem_collision,command_parse}_error` | **level** |
| 7 | **CCE** | `207..222` | 16 | `sdma_cce_*` (16 distinct) | **level** |
| 8 | **SDMA** | `223` | 1 | `sdma_m2s_cmpl_fifo_full` | **level** |
| | | `224..235` | 12 | `sdma_notific_wr_buffer_full[0..11]` | edge |
| | | `236..247` | 12 | `sdma_notific_wr_buffer_drop[0..11]` | edge |
| | | `248` | 1 | `sdma_rob_parity_error` | **level** |
| | | `249` | 1 | `sdma_sow_conflict_error` | **level** |
| | | `250` | 1 | `sdma_sow_underrun_error` | **level** |
| | | `251` | 1 | `sdma_access_missing_app_engine` | **level** |
| 9 | **SPARE** | `252..253` | 2 | `spare_trigger_{0,1}` | **level** |
| | **TOTAL** | | **254** | `9+49+80+5+50+6+4+1+3+16+1+12+12+1+1+1+1+2` ✓ | **223/31** |

> **GOTCHA — NOTIFIC double-listing (H3).** The NOTIFIC write-buffer conditions
> appear **three times** on three distinct trigger buses: in §1 NOTIFIC
> (`sdma_notific_intr`), in the §8 SDMA tail
> (`sdma_notific_wr_buffer_full[0..11]` / `_drop[0..11]`), **and** mirrored in the
> §3 FIS errtrig generator (`fis_errtrig_intr[0..15]`). These are **distinct INTC
> wires**, not duplicates — a naive de-dup by `description` would collapse real
> sources. `[OBSERVED]`

---

## 3. Per-group enumeration (semantic, not a raw dump)

### 3.1 NOTIFIC (9, edge) — instruction-notification queue events

`sdma_notific_intr[0..8]` — the NOTIFIC engine's SW/HW notification-queue events.
The two AXI-fault members are the severity outliers.

| idx | Condition (verbatim-derived) | Kind |
|-----|------------------------------|------|
| 0 | full instruction SW NQ (suppressed if `SW_backpressure==0 & ignore_full==1`; `nq_full_status` flags which SW NQs are full) | notify |
| 1 | notification to a **disabled** NQ → dropped | notify |
| 2 | notification to a **full** HW buffer → **STALL** (requires `hw_backpressure`) | notify/backpressure |
| 3 | notification to a full HW buffer → **DROP** (`hw_backpressure` disabled) | notify/backpressure |
| 4 | AXI master received a **WRITE RESPONSE ERROR** | **AXI-fault (HIGH)** |
| 5 | AXI master **STALLED** (max outstanding writes reached) | AXI-backpressure |
| 6 | threshold reached for a SW queue (one-shot; disables until `head_ptr` written) | notify |
| 7 | **OVERLAP** in ≥2 enabled SW NQ | notify |
| 8 | Coalescer hit to multiple streams → **AXI behaviour non-deterministic** | **HIGH** |

### 3.2 UDMA — M2S/S2M descriptor engines (129 entries, the bulk)

The largest functional area. It reconciles **1:1** against the M2S/S2M CSR error
registers — see [M2S `err_log_mask`](../csr/udma-m2s.md) and
[S2M error register](../csr/udma-s2m.md).

**`sdma_main_intr[0..48]` (49, edge) — the primary uDMA cause vector.** This is
the **per-channel DMA completion set** plus the aggregation summaries:

| `main_intr[]` | YAML idx | Meaning |
|---------------|----------|---------|
| `[0..2]` | 9..11 | Group **B/C/D** Summary |
| `[3..18]` | 12..27 | **RX (S2M) Completion Queue 0..15** — 16 inbound completions |
| `[19..34]` | 28..43 | **TX (M2S) Completion Queue 0..15** — 16 outbound completions |
| `[35..38]` | 44..47 | **Cross Mail Box 0..3** (the GEN `MISC_SDMA_APP` mailbox; [GEN page](../csr/udma-gen-tdma.md)) |
| `[39..42]` | 48..51 | **"Tied to zero in RTL"** — dead bus bits |
| `[43..46]` | 52..55 | Secondary Group **A/B/C/D** Summary (roll up the 80 secondary) |
| `[47]` | 56 | Software Timer |
| `[48]` | 57 | **"Tied to zero in RTL"** |

The **32 per-queue completions** (`[3..34]` = 16 S2M RX + 16 M2S TX) are the
**"per-channel DMA" completion sources**. The group summaries are
level-of-aggregation rollups carried as `edge` here. `[HIGH/OBSERVED]`

**`sdma_secondary_intr[0..79]` (80, edge) — the M2S/S2M error matrix.** Reconciles
1:1 with the M2S `err_log_mask` ("4 AXI domains × {tout,response,parity} +
prefetch desc-integrity + completion-serial mismatch") and the S2M error register:

| `secondary_intr[]` | Block | Representative content |
|--------------------|-------|------------------------|
| `[0..27]` | **M2S OUTBOUND** | stream completion-mismatch / parity / response / timeout `[0..3]`; Completion/Data/Prefetch AXI `{parity,response,timeout}` `[4..12]`; packet-length, prefetch max-desc/first/last/ring-ID `[13..17]`; 8 buffer-parity `[18..25]`; MSIX `{timeout,response}` `[26..27]` |
| `[28..72]` | **S2M INBOUND** | `[28]` Reserved; Data AXI `[29..31]`; header-split / promotion / no-descriptor `[32..36]`; Prefetch AXI + ring-ID + saturate `[37..41]`; Completion AXI + stream + UNACK `[42..46]`; stream header/data parity/first/last `[47..52]`; packet-length `[53]`; 6 buffer-parity `[54..59]`; **`[60]` Transaction-table info parity (V4)**; 6 M2S AXI-fifo parity `[61..66]`; 6 S2M AXI-fifo parity `[67..72]` |
| `[73..79]` | **AXI-GENERATOR summaries** | the 7 dedicated per-engine AXI INTC rollups: `M2S_DESC_READ`, `M2S_DATA_READ`, `M2S_CMPL_WRITE`, `S2M_DESC_READ`, `S2M_DATA_WRITE`, `S2M_CMPL_WRITE`, `MSIX_WRITE` (verbatim *"Summary of the dedicated … AXI generator INTC controller"*) |

AXI-fault (parity/response/timeout) dominates; the 30+ buffer/fifo-parity members
are RAS sources; the 7 AXI-generator summaries are level-of-aggregation. `[28]`
"Reserved" and the V4-tagged `[60]` are OBSERVED verbatim. `[HIGH/OBSERVED]`

### 3.3 FIS — fabric-interface-shim family (61 entries, edge)

The SoC-fabric concern bolted onto the SDMA block. Three buses:

* **`fis_cntrl_intr[0..4]` (5)** — EP posted-write `SLVERR` per AMZN/USER chain:
  AMZN-chain {AMZN, USER, USER-FIS} EP `[0..2]`, USER-chain {USER, USER-FIS} EP
  `[3..4]`. Kind = AXI-fault (posted-write slave error). Severity HIGH.
* **`fis_errtrig_intr[0..49]` (50)** — the **user/amzn errtrig NOTIFIC mirror**:
  `[0..7]` user_errtrig `wr_buffer[0..7]` **full**; `[8..15]` user_errtrig
  `wr_buffer[0..7]` **drop**; `[16..24]` user_errtrig **the 9 NOTIFIC causes**
  (mirror of §3.1); then the amzn_errtrig half `[25..49]`, ending `[45]`
  *"amzn_errtrig notific -- The AXI master received a write response error."* and
  `[49]` *"…Coalescer detected hit to multiple streams-> AXI behavior
  non-deterministic"*. Kind = notification mirror (edge). `[HIGH/OBSERVED]`
* **`sdma_sprot_intr[0..5]` (6)** — the fis_sprot security/QoS-protection vector
  (HW backing in the qos_prot / remapper CSRs — already enumerated here, not
  separate sources): `[0]` amzn_remapper **DENIED** a transaction; `[1]` delta
  monitor R-resp > AR-req; `[2]` **tmu AXI TIMEOUT**; `[3]` delta monitor B-resp >
  AW-req; `[4]` qos PMU interrupt *(OR of all 16 PMU counter "interrrupts" — typo
  verbatim)*; `[5]` `fis_sprot_spare_0`. `[HIGH/OBSERVED]`

### 3.4 ELA / ERG / DRE / CCE — internal compute/buffer faults (24, **all level**)

The SDMA's own compute/replay/buffer engines. **Every entry is level** (sticky:
latches a fault state, not a one-shot event).

* **ELA (4)** `sdma_ela[0..3]` — **`description: null`** (Embedded Logic Analyzer
  trigger taps; names only). These are the 4 null-description entries.
* **ERG (1)** `sdma_ela_erg` — *"ERG error in one of the ELA memories"* (RAS/ECC).
* **DRE (3)** `sdma_dre_erg_error` (ERG error in DRE buffer memories, RAS);
  `sdma_dre_mem_collision_error` (read+write same DRE address);
  `sdma_dre_command_parse_error` (invalid packet).
* **CCE (16)** the Compression/Conversion-Engine fault set: `erg_error` (RAS),
  `mem_collision_error`, `command_parse_error`, `metadata_fifo_{underflow,overflow}`,
  `input_data_converter_error`, `output_data_converter_error`,
  `output_data_converter_state_{inf,nan,overflow,underflow}`,
  `output_data_converter_non_fp_sr`, `fma_error`,
  `decompression_data_{underflow,overflow}`, and the last entry whose **`name` is
  `sdma_cce_a_and_b_self_buffer_error`** (its description is the "A&B both from
  buffer" case — name/description disagree on `self` vs `sel`, OBSERVED).
  Kind = compute-error + RAS, severity HIGH (data corruption / numeric status).

### 3.5 SDMA tail + SPARE (28 entries)

The trailing **SDMA** banner regroups the per-channel DMA **top-level errors** plus
a second copy of the notific write-buffer arrays:

* `sdma_m2s_cmpl_fifo_full` (1, level) — *"The M2S completion buffer in from the
  application engines was full. This may be expected in normal operation, but
  indicates reduced throughput."* The back-pressure / rate-limiter indicator
  (severity LOW/MED).
* `sdma_notific_wr_buffer_full[0..11]` (12, edge) — *"NOTIFIC this write buffer is full"*.
* `sdma_notific_wr_buffer_drop[0..11]` (12, edge) — *"NOTIFIC dropped a notification
  because it was generated when this write buffer was full"*.
* `sdma_rob_parity_error` (1, level) — Read-reorder-buffer parity error (RAS, HIGH).
* `sdma_sow_conflict_error` / `sdma_sow_underrun_error` (1+1, level) — strongly-
  ordered-write block conflict / underrun.
* `sdma_access_missing_app_engine` (1, level) — *"a DMA transaction was decoded to
  an application engine that doesn not exist in this DMA instance"* (typo verbatim);
  the ABORT/decode-fault source (severity HIGH).
* **SPARE (2)** `spare_trigger_0`, `spare_trigger_1` (level; reserved bus bits).

---

## 4. Completion vs error vs AXI-fault classification

Intent-mapping of the 254 (derived from the verbatim descriptions; the §2/§3
per-entry layout is the `[HIGH/OBSERVED]` ground truth — these counts are `[MED]`
because the AXI-fault / buffer-parity boundary overlaps):

| Class | ≈ | Constituents |
|-------|---|--------------|
| **Completion** (per-queue/mailbox) | ~38 | `main_intr` S2M-CQ(16) + M2S-CQ(16) + CrossMailBox(4) + SW-Timer(1) + wr_buffer back-pressure |
| **Notification** (SW/HW NQ status) | ~33 | `notific_intr`(9) + wr_buffer_full(12) + wr_buffer_drop(12) |
| **Error — AXI-fault** (parity/response/timeout/slverr) | ~70 | `secondary_intr` AXI domains, MSIX tout/resp, `fis_cntrl` slverr(5), sprot tmu-timeout, `notific_intr[4]` write-resp error |
| **Error — buffer/memory PARITY (RAS)** | ~45 | `secondary_intr` buffer-parity(~16) + AXI-fifo-parity(~12) + `rob_parity` + ELA/DRE/CCE ERG + txn-table parity *(approximate — RAS groups by AXI/buffer, no single ECC total)* |
| **Error — compute/numeric** (CCE/DRE) | ~16 | CCE converter inf/nan/over/underflow, fma, decompression, parse |
| **Abort / decode / security** | ~9 | `access_missing_app_engine`, sow conflict/underrun, sprot remapper-deny, delta-monitor(2), tmu-timeout |
| **Summary rollups** (level-of-aggregation) | ~14 | `main_intr` group + secondary-group summaries(7) + secondary AXI-generator summaries(7) |
| **Spare / tied-zero** | ~7 | 2 spares + 5 "tied to zero in RTL" |

**EDGE vs LEVEL** `[HIGH/OBSERVED]`: **223 edge / 31 level**. The 31 level entries
are *precisely* ELA(4) + ELA_ERG(1) + DRE(3) + CCE(16) + `m2s_cmpl_fifo_full`(1) +
`rob_parity`(1) + `sow_conflict`(1) + `sow_underrun`(1) +
`access_missing_app_engine`(1) + spare(2) = **31**. The internal compute/buffer/RAS
fault state is **sticky (level)**; completions/notifications/AXI events are
**one-shot (edge)**.

---

## 5. The iDMA done/err interrupts (35 / 36) — a DIFFERENT engine

> **CORRECTION / DISAMBIGUATION — `IDMA_DONE`/`IDMA_ERR` (INT 35/36) are NOT in
> this 254-source table.** The Tensilica Xtensa **on-core iDMA** is a *separate*
> single-channel DMA engine **inside** the Q7/NX GPSIMD core, distinct from the
> Annapurna **uDMA/SDMA** descriptor engine enumerated above. Its completion/error
> lines land on the **Q7 core's own 37-line interrupt vector** (`INT0..INT36`),
> *not* on the SDMA INTC trigger bus. `[HIGH/OBSERVED]`

In the Q7 core's interrupt-level table (all 37 ints at level 1 in this config):

| Core INT | Source | Macro |
|----------|--------|-------|
| 0..24 | EXTERN_EDGE — external `BInterrupt` pins (SoC-fabric / engine-completion / DMA-ring / sync doorbells) | `EXTINTn_NUM == n` |
| 25,26,27 | SOFTWARE (inter-thread / self-IPI) | — |
| 28,29,30 | TIMER (`CCOMPARE0/1/2`) | — |
| 31,32,33,34 | OCD `DBG_REQUEST` / `BREAKIN` / `TRAX` / `PROFILING` | — |
| **35** | **`IDMA_DONE`** — on-core iDMA channel-0 completion | `XCHAL_IDMA_CH0_DONE_INTERRUPT=35` |
| **36** | **`IDMA_ERR`** — on-core iDMA channel-0 error | `XCHAL_IDMA_CH0_ERR_INTERRUPT=36` |

The `libidma*.a` / `src/libidma/idma.c` in the GPSIMD toolchain drive *this*
on-core iDMA; the firmware's outbound HBM/host DMA instead goes through the **uDMA
M2S via the DGE**, whose completion/error sources are the 254 triggers above.
("`legacy_dma`" strings in the corpus belong to the Synopsys PCIe controller, also
unrelated.) See [the uDMA HW engine](../../dma/udma-hw-engine.md). `[HIGH/OBSERVED]`

> **GOTCHA.** A reimplementation that wires the SDMA descriptor-engine completions
> into core INT 35/36 conflates two engines. The 254-source SDMA set fans up through
> the **INTC errtrig pair → peb apex → IOFIC cascade** (§6), reaching the Q7 only
> as an *aggregated* IRQ; the on-core iDMA's 35/36 are a direct, dedicated pair on
> the core vector.

---

## 6. Routing — how the 254 latch into the INTC 4-group hardware

### 6a. Leaf → INTC errtrig pair (256-source capacity)

Each APB INTC instance classifies **4 groups × 32 = 128 trigger inputs**
(`NUM_OF_TRIGS=128`, one MSI-X vector + one PBA bit per trigger; see
[INTC 4-group §CAPACITY](../csr/intc-4group.md)) `[CARRIED HIGH]`. An **errtrig
generator PAIRS two instances** (`trig_0` + `trig_1`) → **256-source capacity**.
The 254 SDMA triggers **fit one errtrig pair** (254 ≤ 256):

```
SDMA  254 sources  ->  errtrig PAIR (2 x intc, 128 each)  =  256 cap   (254 <= 256 OK)
```

YAML order = the fill order: group0 bit0.. then group1.. across `trig_0` then
`trig_1`. The errtrig binds two MSIX INTC instances on the host-reachable path
(verbatim instance names):

```
peb_apb_io_{0,1}_user_fis_se_1_sdma_0_fis_0_user_errtrig_trig_0  -> MSIX
peb_apb_io_{0,1}_user_fis_se_1_sdma_0_fis_0_user_errtrig_trig_1  -> MSIX
```

The **MSIX** flavor delivers MSI-X straight to the PCIe host; the **NO_MSIX** flavor
emits 4 severity wire-ORs (error/abort/fatal/log) + a `Mask_msi_x` summary *upward*
as the SDMA `*_nmi` / `*_summary`. See
[errtrig/FIS routing](errtrig-fis-routing.md).

> **NOTE — exact group cut is LOW.** The aggregate "254 fit in a 256-cap pair" is
> `[HIGH]`, but the exact **group(0/1/2/3) cut** of each sub-block across `trig_0` /
> `trig_1` is **not encoded in any shipped artifact** — `[LOW]` on the precise cut,
> `[HIGH]` on the fit. Fill order = YAML order is the only invariant to rely on.

### 6b. INTC apex rollup — SDMA dominates the peb apex

At the peb apex (`peb_intc_triggers.yaml`, **128 apex bits**), **64 of the 128 bits
(half) are per-SDMA summaries** — the SDMA block is the single dominant apex
contributor `[HIGH/OBSERVED]`:

| apex idx | apex trigger | covers |
|----------|--------------|--------|
| 7..38 | `se0_sdma_nmi[0..31]` | SEngine[0], 32 SDMA queues |
| 42..73 | `se1_sdma_nmi[0..31]` | SEngine[1], 32 SDMA queues |
| 75 | `sdma_d2h_nmi` (name `sdma_d2h_summary`) | device→host |
| 76 | `sdma_h2d_nmi` (name `sdma_h2d_summary`) | host→device |

All level at the apex (each summary re-latches a downstream sticky image).

### 6c. The bit-reversal keystone hazard

> **CORRECTION / HAZARD — apex bit index ≠ SDMA queue number (H1).** The apex SDMA
> trigger **index counts UP** while the NAME's SDMA queue number **counts DOWN**.
> Re-verified directly against `peb_intc_triggers.yaml`:
>
> ```
> se0_sdma_nmi[0]  -> name "se0_sdma_31_summary"  (SEngine[0] SDMA[31])
> se0_sdma_nmi[31] -> name "se0_sdma_0_summary"   (SEngine[0] SDMA[0])
> ```
>
> So apex bit-position *i* maps to **physical queue `[31 - i_local]`**:
> `physical_queue = 31 - (apex_idx - 7)` for SE0, `31 - (apex_idx - 42)` for SE1
> (identical reversal on both SEngines). **A tool that reads the apex bit index as
> the queue number is wrong — it is the complement.** Always trust the NAME string
> `se{e}_sdma_{q}_summary`, never the bit index. `[HIGH/OBSERVED]`

### 6d. Full cascade

```
254 SDMA leaf triggers
  -> errtrig PAIR INTC (latch; 4x32 per instance; MSIX-to-host or NO_MSIX wire-ORs)
  -> peb_intc apex (64 se{0,1}_sdma_nmi[31-i] + 2 host summaries)
  -> IOFIC cascade
  -> Q7 / management-core IRQ and/or GIC
```

The leaf→apex chain is `[HIGH]`. The final apex→Q7-vector hop is firmware-owned and
not register-encoded `[MED]`. See [physical INTC instances](physical-intc-instances.md)
and [io-fabric triggers](io-fabric-triggers.md).

---

## 7. Why `needs_cdc` is always false — the INTC's CDC stage owns it

`needs_cdc` is `false`/`null` on all 254 because **the SDMA leaves resync their own
clock domain before the trigger bus**. The INTC still carries a per-trigger CDC
edge-gen + tog2pul stage (gated by `int_cdc_bypass_grp`), whose `no_msix` variant
notes *"the CDC syncro is still in the path regardless"* — so a leaf can declare no
crossing while the INTC unconditionally resynchronises. Cross-reference
[INTC 4-group §CDC bypass](../csr/intc-4group.md). `[HIGH/OBSERVED leaf; CARRIED INTC]`

---

## 8. Cross-gen differences — cayman / maverick / sunda

All three gens use the **same MINIMAL-5 schema** (key-union identical, `yq`-verified);
`needs_cdc` is **never `true`** in any gen.

| Gen | Count | Edge/Level | FIS-family form |
|-----|-------|------------|-----------------|
| **cayman** (authority) | **254** | 223/31 | `fis_cntrl[5]` + `fis_errtrig[50]` + `sprot[6]` |
| **maverick** (v5) | 256 | 225/31 | `fis_cntrl` (1 **flat**) + `fis_errtrig[50]` + `sprot[6]` |
| **sunda** | 239 | 217/22 | **no FIS banner**; `sprot[0..54]` (55) folds it in |

* **maverick (256, +2 vs cayman)** — ADDS 6: `wob_axi_{timeout,slverr,decerr}_interrupt`
  (Write-Ordered-Buffer AXI faults, *"not the udma timeout interrupts"*),
  `parity_{addr,data}_error_interrupt`, `sdma_emb_sem_unaligned_4B_transaction`;
  DROPS 4 by collapsing `fis_cntrl_intr[0..4]` → one flat `fis_cntrl_intr`
  (net `254 - 4 + 6 = 256` ✓). *(The maverick `wob_axi_slverr` description carries a
  trailing space — cosmetic artifact anomaly, OBSERVED.)* `[HIGH/OBSERVED table; INFERRED v5 runtime]`
* **sunda (239, −15 vs cayman)** — replaces the FIS banner (`cntrl 5 + errtrig 50 = 55`)
  with a SPROT banner (`sdma_sprot_intr[0..54]`, 55) that folds the errtrig NOTIFIC
  causes into the sprot bus; `sprot[0..4]` are the security/QoS causes. DROPS the
  cayman tail extras (the 4 CCE `output_data_converter_state_*`, sow conflict/underrun,
  `access_missing_app_engine`, the 2 spares; `fis_cntrl` absent) — hence the
  fewer-level count (22 vs 31). `[HIGH/OBSERVED]`

**Stable core across all gens:** `sdma_main_intr`(49) + `sdma_secondary_intr`(80) +
NOTIFIC(33) + the ELA/DRE/CCE compute faults. The deltas are entirely in the
FIS/SPROT fabric-shim packaging and silicon-specific extras.

> **NOTE — H7 packaging drift.** The same errtrig NOTIFIC causes live on
> `fis_errtrig_intr` (cayman/maverick) vs `sdma_sprot_intr` (sunda) — a per-gen
> bus-rename, not a source change.

---

## 9. Hazard / anomaly ledger

| # | Hazard | Grade |
|---|--------|-------|
| H1 | **Apex bit-reversal**: `se{0,1}_sdma_nmi[i]` → physical queue `[31 - i_local]`. Off-by-`(31-i)` for any apex-bit↔queue correlation. Trust the NAME, not the index. | `[HIGH/OBSERVED]` |
| H2 | **`needs_cdc` declared but never true**: all 254 false/null → 0 CDC crossings, yet the key exists. Gate sync on the *value*, not the *presence*. | `[HIGH/OBSERVED]` |
| H3 | **NOTIFIC triple-listing**: same condition on 3 buses (`notific_intr`, `wr_buffer_full/drop`, `fis_errtrig`) — distinct wires, do not de-dup by description. | `[OBSERVED]` |
| H4 | **Null descriptions**: `sdma_ela[0..3]` carry `description: null` (name-only ELA taps). A consumer asserting non-null desc trips. | `[OBSERVED]` |
| H5 | **"Tied to zero in RTL"**: `sdma_main_intr[39..42,48]` (5) are dead bus bits — never fire, counted in the 254 but inert. | `[OBSERVED]` |
| H6 | **Verbatim typos preserved**: `"interrrupts"` (3 r's) in `sdma_sprot_intr[4]`; `"doesn not exist"` in `access_missing_app_engine`. Do NOT "fix" when string-matching across the corpus. | `[OBSERVED]` |
| H7 | **Cross-gen FIS/SPROT drift**: `fis_errtrig_intr` (cayman/maverick) vs `sdma_sprot_intr` (sunda) — bus rename, not a source change. | `[HIGH/OBSERVED]` |
| H8 | **iDMA confusion**: core `INT 35/36` (`IDMA_DONE`/`IDMA_ERR`) are the *on-core Xtensa iDMA*, NOT this 254-source uDMA/SDMA set (§5). | `[HIGH/OBSERVED]` |

---

## See also

* [CSR — UDMA M2S](../csr/udma-m2s.md) — the outbound engine whose `err_log_mask` the `secondary_intr` matrix reports.
* [CSR — UDMA S2M](../csr/udma-s2m.md) — the inbound RX-ring engine.
* [CSR — UDMA GEN / GEN_EX / TDMA_MODEL](../csr/udma-gen-tdma.md) — the common half + the `MISC_SDMA_APP` cross-mailbox (Cross Mail Box 0..3).
* [CSR — INTC 4-group](../csr/intc-4group.md) — the 4×32=128-trigger unit these latch into; the errtrig pair.
* [Schema atlas](schema-atlas.md) — the per-table key-set families (MINIMAL-5 vs RICH-9).
* [Physical INTC instances](physical-intc-instances.md) — the named instance map of the cascade.
* [errtrig / FIS routing](errtrig-fis-routing.md) — the MSIX / NO_MSIX errtrig generator.
* [IO-fabric triggers](io-fabric-triggers.md) — the sibling FAMILY-A leaf table.
* [uDMA HW engine](../../dma/udma-hw-engine.md) — the descriptor engine vs the on-core Xtensa iDMA.
