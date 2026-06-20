# Interrupts — PEB Apex / CC / TOP_SP Sources

This page documents three interrupt-source sets that converge on the Cayman SoC's
management path: the **`peb_intc` apex** (the 128-input "everything → Q7" aggregator),
the **CC** / PREPROC compute-cluster trigger leaf (**98 sources**), and the **TOP_SP**
sequencer-processor trigger leaf (**82 sources**). All three are recovered from the
RTL-generated trigger-list artifacts (`intc/peb_intc_triggers.yaml`,
`intc/cc_triggers.yaml`, `intc/top_sp_triggers.yaml`) plus the intc / `top_sp_ram`
CSR schemas. Every count below is re-grounded to the YAML itself (`yq`/`rg`), never
a decompile grep.

The apex aggregation model, the critical-fast-path vs generic-summary split, and the
final `peb_intc → Q7/GIC` routing are the through-lines. The leaf domains feed the
apex; this page is where they meet.

Related:
[Physical INTC instances](./physical-intc-instances.md) ·
[Errtrig / FIS routing](./errtrig-fis-routing.md) ·
[NSM isolation flow (unified)](./nsm-flow-unified.md) ·
[Q7 surprises / binding](./q7-surprises-binding.md) ·
[CSR — `rdm_model` + `top_sp_ram`](../csr/rdm-top-sp.md) ·
[`ap_intc` 1-group apex cascade](../csr/intc-1group-apintc.md) ·
[PREPROC / Compute-Cluster address subtree](../address/preproc-cc.md).

**Confidence legend.** `HIGH` = literal value read from the YAML/JSON artifact.
`MED` = inference from naming + CSR cross-reference. `LOW` = plausible, flagged.
`OBSERVED` = read from a shipped artifact. `INFERRED` = reasoned from corroboration.
`CARRIED` = taken from a sibling page, not re-derived here.

---

## 0. The three sets at a glance `[HIGH · OBSERVED]`

| Set | Artifact | `yq 'length'` | Role |
|---|---|---|---|
| **PEB apex** | `intc/peb_intc_triggers.yaml` | **128** | the apex aggregator: every domain's summary/NMI + 32 PEB-local criticals → Q7/GIC |
| **CC (PREPROC)** | `intc/cc_triggers.yaml` | **98** | Compute-Cluster leaf: 4 FP-status + 2 Q7-mem ECC + notification/AXI + FIS shim |
| **TOP_SP** | `intc/top_sp_triggers.yaml` | **82** | TopSP sequencer leaf: semaphore + ERG-ECC + FIS shim + instr-notif + NX-core |

All three Cayman artifacts live under
`extracted/nested/cayman-arch-regs_tgz/intc/`. The 128/98/82 counts are the exact
`yq 'length'` of each file — re-verified here, not "~" estimates.

> **GOTCHA — `extracted/` and `ida/` are gitignored.** `fd`/`rg` skip them by
> default. Use `--no-ignore` or absolute paths when re-grounding any count on this
> page. The three trigger YAMLs also ship `maverick/` (v5) and `sunda/` variants under
> `arch-headers/`; those are the cross-gen taps cited in §8.

> **NOTE — v5/MAVERICK is header-OBSERVED only.** Where this page cites Maverick
> counts/keys (`functional_test_required`, the `*_vec_q` arrays, the collapsed
> `fis_cntrl`), the YAML *header and key set* are OBSERVED; any reading of v5
> *interior* silicon behaviour is `[INFERRED]` and flagged inline.

---

## 1. The apex aggregation model — 128 inputs → downstream domains `[HIGH · OBSERVED]`

The `peb_intc` apex is the master interrupt-routing rollup for the whole SoC fault
surface. It is the **4-group × 32 = 128-input** INTC: `[CARRIED · #923]`
`NUM_OF_TRIGS=128` (`NonOverridable`), MSI-X vector table = 128 entries. The 128 YAML
entries fill the 128-bit trigger bus **1:1, file order = bit order**. `yq 'length' = 128`
confirms the file saturates the bus exactly — no gaps, no reserved bits. `[HIGH · OBSERVED]`

The apex's job: every *other* domain's leaf INTC produces one per-domain
`*_summary` / `*_nmi` wire (its `u_amzn_errtrig` `nmi_out`). Those summary wires —
**plus** a set of direct PEB-local critical sources — are exactly the 128 apex entries.
The apex re-latches all 128 into a 4×32-bit pending image and presents **one** rolled-up
output upward. `[HIGH structure]`

### 1a. Per-source downstream-domain classification (the master rollup) `[HIGH · OBSERVED]`

All 128 apex sources classified by the downstream domain they summarize or represent.
Counts re-derived by `rg -c 'trigger: <base>'` over the YAML; the sum is 128.

| Cnt | Downstream domain | Apex trigger(s) (rollup wire) | Kind |
|---:|---|---|---|
| **64** | **SDMA** (per-engine) | `se{0,1}_sdma_nmi[0..31]` — 32 queues × 2 SEngines | summary |
| 8 | SPI (`spis_0`/`spis_1`) | `spis_{0,1}_{mem_txn_dropped,spi_eng_tout,incomplete_cmd,invalid_cmd}_intr` | critical |
| 7 | PVT (process/volt/temp) | `pvt_intr_local[0..6]` (ctrl, 2× temp, 2× voltage, 2× process) | critical |
| 5 | FIS control | `fis_cntrl_intr[0..4]` — EP posted-wr slverr (AMZN/USER/USER-FIS chains) | shape-D |
| 5 | FIS sprot | `fis_sprot_intr[0..4]` — remapper-deny / R\>AR / tmu-tout / B\>AW / qos-pmu | shape-D |
| 4 | TPB | `se{0,1}_tpb_nmi[0,1]` (2 TPB / SEngine) — **no** critical | summary |
| 4 | HBM critical fast-path | `hbm_inttrig_{cattrip,temp_change}_r[0,1]` (cattrip/temp per stack) | critical |
| 4 | MISC axi2apb | `misc_axi2apb_{0,1}_{timeout_type_valid,pos_wr_nacc_intr}` | critical |
| 4 | APBBLK (per-SEngine) | `se{0,1}_apbblk_list_{0,1}_intr` | shape-D |
| 2 | APB flush handshake | `apb_outstding_flushed_{clr,set}_triggers_out` | critical |
| 2 | CC (Compute-Cluster) | `cc_top_{0,1}_nmi` (`CC_TOP[0]`,`[1]`) | summary |
| 2 | ERG uncorrectable | `erg_intr_trig_uncerr_r` + `io_fabric_erg_uncerr_intr_local` | critical |
| 2 | HBM summary | `hbm_{0,1}_nmi` (`seng_{0,1}_hbm_summary`) | summary |
| 2 | PCIe (M / A) | `pcie_m0_nmi` (master) + `pcie_a0_nmi` (admin/PF) — *carry `source_path`* | summary |
| 2 | PCIe_B (per-SEngine) | `pcie_se{0,1}_b_combined_nmi` (`PCIe_B[0-4]` per SEngine) | summary |
| 2 | SDMA host (D2H/H2D) | `sdma_d2h_nmi` + `sdma_h2d_nmi` | summary |
| 1 | D2D | `d2d_combined_nmi` (`D2D[0-7]`, 8 links) — **no** critical | summary |
| 1 | GPIO | `ring_io_top_gpio_intr_local` | critical |
| 1 | I2C | `i2c_intr` | critical |
| 1 | INTC (self / nested) | `intc_nmi` (`intc_summary`) | summary |
| 1 | IO_FABRIC apbblk | `io_fabric_apbblk_list_intr_local` | critical |
| 1 | MISC_ram | `misc_ram_intr_local` (host → "Pacific" mgmt-core IRQ) | critical |
| 1 | NSM / isolation | `intr_peb_nsm_axi_timeout` (PEB SPROT NSM) | critical |
| 1 | RDM | `rdm_nmi` (`rdm_summary`) | summary |
| 1 | **TOP_SP** | `top_sp_combined_nmi` (`TopSP[0-9]`, 10 blocks) | summary |
| **128** | **TOTAL** | | |

Keystone observations:

- **SDMA dominates** — 64/128 = half the apex. SDMA is the widest fan-in: 32 queues ×
  2 SEngines, each queue's leaf `nmi_out` gets a dedicated apex bit. `[HIGH · OBSERVED]`
- **The 2-SEngine partition is visible throughout**: TPB (2+2), HBM (1+1), SDMA (32+32),
  PCIe_B (1+1), APBBLK (2+2) — the Cayman 2-die / 2-PEB split. `[HIGH]`
- **High-fan-out fabric domains are pre-combined at the leaf** so they fit the 128-bus
  budget: `d2d_combined_nmi` = `D2D[0-7]` (8 links), `top_sp_combined_nmi` = `TopSP[0-9]`
  (10), `pcie_se{0,1}_b_combined_nmi` = `PCIe_B[0-4]` (5 each). The apex does **not** give
  every leaf its own bit. `[HIGH · OBSERVED]`
- `intc_nmi` (idx 79) is the apex aggregating *other* intc instances' internal errors —
  the INTC hierarchy is self-monitoring (an intc that watches intcs). `[HIGH name · MED role]`

> **CORRECTION — vs [`intc-1group-apintc.md`](../csr/intc-1group-apintc.md) (#923) §6 L2.**
> #923 states the apex has "**128 active triggers, each with a `source_path` to a leaf
> `nmi_out` wire and `nmi_mask`/`nmi_msix_mask` fields**". That is too strong, verified
> against the YAML bytes:
> `yq '[.[] | select(has("source_path"))] | length'` = **2** (only `pcie_m0_nmi` and
> `pcie_a0_nmi` carry `source_path`/`source_block`); the other 126 entries omit it (the
> leaf wire is implied by the trigger name). `yq '[.[] | select(has("nmi_mask"))] | length'`
> = **114**, not 128 — the 14 FIS/APBBLK shape-D entries (§1b) carry neither `nmi_mask`
> nor `nmi_msix_mask`. The #923 verbatim PCIe `source_path` examples are correct; the
> *generalization to all 128* is not. The framing "the apex aggregates leaf
> `*_nmi`/`*_summary` wires, file-order = bit-order, → Q7/GIC `[INFERRED]`" is otherwise
> consistent and retained here. `[HIGH · OBSERVED — bytes contradict the "all 128" claim]`

### 1b. Apex per-entry schema — four key-shapes `[HIGH · OBSERVED]`

The key union across all 128 entries is 10 keys: `trigger`, `name`, `description`,
`edge_triggered`, `nmi_mask`, `nmi_msix_mask`, `source_block`, `source_path`,
`needs_cdc`, `critical`. **No** `severity` / `polarity` / `msix_mask` / `source_clock`
key exists in the apex file. Four distinct keyset shapes:

| Shape | Keyset | Count | Meaning |
|---|---|---:|---|
| **A** | `{description, edge_triggered, name, nmi_mask, nmi_msix_mask, trigger}` | 80 | generic per-domain summaries |
| **B** | A + `critical` | 32 | **critical fast-path** (§2) |
| **C** | A + `{source_block, source_path}` | 2 | `pcie_m0/a0` summaries (the only named leaf wires) |
| **D** | `{description, edge_triggered, name, needs_cdc, trigger}` | 14 | FIS sprot/cntrl + SE apbblk (no nmi/crit) |

80 + 32 + 2 + 14 = 128. `[HIGH · OBSERVED]` Edge/level split: `edge_triggered=false`
(level) on **104**, `=true` (edge) on **24** — the 24 edge entries are the pulse-type
criticals (`spis_*`, `misc_ram`, `erg`, `io_fabric_*`, `axi2apb pos_wr_nacc`) plus the
10 FIS (`fis_sprot[0..4]` + `fis_cntrl[0..4]`). `[HIGH · OBSERVED]`

> **QUIRK — `nmi_mask = 0` on all 114 apex copies, vs `1` on the leaf copies.** The
> apex's rolled-up summaries carry `nmi_mask=0` / `nmi_msix_mask=0`; the corresponding
> *leaf* entries (e.g. the HBM leaf) carry `1`. Reading: `0` = "not separately
> NMI/MSI-X-delivered — this is a summary re-latch". The YAML does not define
> `1`=enabled vs `1`=masked, so the *polarity meaning* is `[MED]`; that the values
> *differ* leaf-vs-apex is `[HIGH · OBSERVED]`.

> **GOTCHA — SDMA name/index reversal (a real off-by-N hazard).** The trigger index
> counts **up** while the name's SDMA number counts **down**:
> `se0_sdma_nmi[0]` → name `se0_sdma_31_summary` (`SDMA[31]`), and
> `se0_sdma_nmi[31]` → `se0_sdma_0_summary` (`SDMA[0]`). Same reversal on SE1. So apex
> trigger bit `i` maps to physical SDMA queue `31-i`. Verified by
> `yq '.[7].name'` = `se0_sdma_31_summary` and `yq '.[38].name'` = `se0_sdma_0_summary`.
> Tools correlating apex bit ↔ physical queue must invert. `[HIGH · OBSERVED]`

---

## 2. Critical fast-path vs generic summary — the split `[HIGH · OBSERVED]`

Exactly **32 of 128** carry `critical:1`
(`yq '[.[] | select(.critical == 1)] | length'` = 32). These are the fast-path
sources — events urgent enough that firmware reacts without decoding a per-domain
summary. The remaining 96 have **no** `critical` key (80 shape-A summaries + 2 shape-C
PCIe + 14 shape-D FIS/APBBLK).

The 32 `critical:1` sources, grouped (sum verified = 32):

| Cnt | Critical group | Sources |
|---:|---|---|
| 4 | HBM thermal | `hbm_inttrig_{cattrip,temp_change}_r[0,1]` — **bypass** the generic `hbm_{0,1}_nmi` summary |
| 7 | PVT sensors | `pvt_intr_local[0..6]` — controller, temp A/B, voltage A/B, process A/B |
| 8 | SPI slave | `spis_{0,1}_{mem_txn_dropped,spi_eng_tout,incomplete_cmd,invalid_cmd}_intr` |
| 4 | axi2apb cfgbus | `misc_axi2apb_{0,1}_{timeout_type_valid,pos_wr_nacc_intr}` |
| 1 | NSM AXI timeout | `intr_peb_nsm_axi_timeout` (idx 111) — the NSM-isolation flow anchor (§4) |
| 2 | ERG uncorrectable | `erg_intr_trig_uncerr_r` + `io_fabric_erg_uncerr_intr_local` |
| 1 | io_fabric apbblk | `io_fabric_apbblk_list_intr_local` |
| 2 | APB flush handshake | `apb_outstding_flushed_{clr,set}_triggers_out` (reset/isolation drain) |
| 3 | GPIO / I2C / misc_ram | `ring_io_top_gpio_intr_local`, `i2c_intr`, `misc_ram_intr_local` |

4 + 7 + 8 + 4 + 1 + 2 + 1 + 2 + 3 = **32**. `[HIGH · OBSERVED]`

The 4 HBM-thermal criticals carry dedicated apex bits **per stack** and bypass the
generic `hbm_{0,1}_nmi` summary — the silicon's "react to a CATTRIP before the device
cooks" fast-path. The other 28 criticals are **direct PEB-local leaf signals**
(PVT/SPI/I2C/GPIO/axi2apb/erg/nsm/apb-flush/misc_ram) that originate inside the PEB
and wire straight to the apex — they are not rollups of a downstream domain INTC.
`[HIGH · OBSERVED]`

**Who has no fast-path (explicit non-claim):** TPB (4) — no critical (matches
[errtrig/FIS routing](./errtrig-fis-routing.md)); D2D (1) — no critical; and the
generic-summary-only domains: SDMA per-engine (64), HBM summary (2), PCIe (4), CC (2),
TOP_SP (1), RDM (1), SDMA host (2), INTC-self (1). These rely on the management core
decoding their leaf summary register; only thermal / RAS / management-bus events earn
a dedicated critical bit. `[HIGH · OBSERVED]`

---

## 3. The 14 shape-D FIS + APBBLK apex entries `[HIGH · OBSERVED]`

14 entries carry **neither** `nmi_mask`/`nmi_msix_mask` **nor** `critical` — just
`{trigger, name, description, needs_cdc:false, edge_triggered}`
(`yq '[.[] | select(has("needs_cdc"))] | length'` = 14). They are in-domain events
wired directly into the apex without the NMI-mask plumbing:

- **`se{0,1}_apbblk_list_{0,1}_intr` (4)** — per-SEngine APB-block list IRQs; level.
- **`fis_sprot_intr[0..4]` (5)** — FIS security/QoS protection, all edge:
  `[0]` amzn_remapper denied a transaction · `[1]` delta-mon: R rsp > AR req ·
  `[2]` tmu AXI timeout · `[3]` delta-mon: B rsp > AW req · `[4]` qos PMU (OR of 16 ctrs).
- **`fis_cntrl_intr[0..4]` (5)** — FIS control EP posted-write slave-errors (AMZN/USER/
  USER-FIS chains), all edge. Same `fis_cntrl` family as the per-leaf shim (§5, §6).

Interpretation: these are the PEB's own fabric-security / QoS / APB telemetry; they
land at the apex un-NMI-masked because they *are* the management core's direct security
telemetry. `[HIGH text · MED role naming]` See
[errtrig/FIS routing](./errtrig-fis-routing.md) for the shim family across leaves.

---

## 4. The PCIe isolation-SM sources — where they actually live `[HIGH · OBSERVED]`

The PCIe isolation state-machine sources (linkdown / FLR / SBR / NTS / PIR / NSM-AXI-
timeout / isolation enter / exit) are **NOT** in the `peb_intc` apex file:

```text
rg 'reset_handshake|isolation_sm|linkdown|FLR|SBR|NTS|PIR' peb_intc_triggers.yaml  →  0 hits
```

They live **one level down**, in the PCIe leaf (`intc/pcie_triggers.yaml`) as the
`reset_handshake_intr[8..15]` vector:

| Bit | Source |
|---|---|
| `[8]` | `isolation_sm_linkdown_detected` |
| `[9]` | `isolation_sm_flr_detected` (Function-Level Reset) |
| `[10]` | `isolation_sm_sbr_detected` (Secondary Bus Reset) |
| `[11]` | `isolation_sm_nsm_axi_timeout_detected` |
| `[12]` | `isolation_sm_nts_axi_timeout_detected` |
| `[13]` | `isolation_sm_pir_detected` (Pacific-Initiated Reset) |
| `[14]` | `isolation_sm_isolation_mode_enter` |
| `[15]` | `isolation_sm_isolation_mode_exit` |

These roll up at the PCIe leaf INTC and arrive at the apex via the **PCIe summary** bits
`pcie_m0_nmi` (idx 0) / `pcie_a0_nmi` (idx 1) / `pcie_se{0,1}_b_combined_nmi` (idx 5/6).
The isolation SM is **summarized** — it has no dedicated apex bit of its own.
`[HIGH · OBSERVED]`

**The one direct tie — NSM by two paths.** The NSM AXI-timeout event *also* gets a
direct `critical:1` apex bit `intr_peb_nsm_axi_timeout` (idx 111, verified `critical=1`).
So NSM's AXI watchdog reaches the management core **simultaneously**:

1. *summarized* — NSM event → isolation-SM source `[11]` → `isolation_mode_enter[14]`
   → PCIe leaf summary → `pcie_*_nmi` apex bit. `[MED — the leaf summary bit is not
   individually named at the apex]`
2. *direct fast-path* — NSM event → `intr_peb_nsm_axi_timeout` apex bit (`critical=1`).
   `[HIGH · OBSERVED]`

This dual `(a)+(b)` tie is the [NSM isolation flow](./nsm-flow-unified.md) anchor.

> **NOTE — Sunda promotes the PCIe reset/isolation handshake to direct apex criticals.**
> The Sunda apex (§8) exposes a set of direct `critical:1` reset-handshake bits Cayman
> folds into the PCIe summary: `misc_pacific_hs_pcie_peb_flr_sbr_req_local`,
> `..._axi_timeout_local`, `..._reset_done_ack_local`, `..._reset_ready_local`. So the
> isolation/reset surface is exposed *differently per generation* — Cayman summarizes +
> only NSM-timeout direct; Sunda promotes the whole handshake. `[HIGH · OBSERVED]`

---

## 5. CC (PREPROC Compute-Cluster) — 98 sources `[HIGH · OBSERVED]`

**`cc_triggers.yaml`** is the interrupt leaf of the **Compute-Cluster (CC)** =
`PREPROC`, a four-core Cadence Tensilica **Vision-Q7** ("GPSIMD") DSP cluster (the same
Q7-cluster IP as the 8-core TPB POOL, instanced with 4 cores; see
[PREPROC/CC address subtree](../address/preproc-cc.md)). The in-file evidence settles
the "CC" ambiguity decisively toward *compute-cluster*, not collective-comm: the
descriptions read "FP status of INVALID from various **CC computes**" and "Correctable/
Uncorrectable error from **Q7 memories**"; a grep for `collective|ring|allreduce|rank|
nccl` = **0 hits**. `[HIGH · OBSERVED]`

> **QUIRK — the CC file's banner is `# Cayman TPB Triggers` (a copy-paste artifact).**
> The CC reuses the TPB POOL Q7-cluster IP and its `tpb_xt_local_reg.json` control
> block, so the generator stamped the TPB banner onto the CC file. The *content* is
> CC-specific (`fp_stat_*_peng`, `cc_seq_mem_*`, `cc_notific_intr`). Treat the banner
> as lineage evidence, not domain truth. `[HIGH · OBSERVED]`

### 5a. Schema — a fourth distinct shape `[HIGH · OBSERVED]`

8-key union: `{trigger, name, description, edge_triggered, needs_cdc}` +
`{source_clock, source_reset_n, msix_mask}`. Two keyset shapes, perfectly correlated
with `needs_cdc`:

| Shape | Keyset | Count |
|---|---|---:|
| M (minimal-5) | `{trigger, name, description, edge_triggered, needs_cdc}` | **67** |
| S (CDC superset) | M + `{source_clock=clk_1p2, source_reset_n=rst_1p2_extended_n, msix_mask=0}` | **31** |

`needs_cdc==true ⇔ S-shape ⇔` the 31 cluster-domain (`clk_1p2`) sources;
`needs_cdc==false ⇔ M-shape ⇔` the 67 fabric-clock FIS sources. Verified:
`yq '[.[] | select(.needs_cdc == true)] | length'` = 31,
`yq '[.[] | select(has("source_clock"))] | length'` = 31. **No** `nmi_*` / `critical` /
`tog2pul_only` key (`has("critical")` = 0). The 31 native CC sources cross the 1.2 V
compute/Q7 clock domain into the INTC; the 67 FIS-shim sources are already in fabric
clock. `[HIGH values · MED `clk_1p2` voltage reading]`

### 5b. Sub-block grouping (file order = bit order) `[HIGH · OBSERVED]`

| Family (trigger base) | Cnt | idx range | E/L | CDC | Sub-block |
|---|---:|---|---|---|---|
| `fp_stat_{ie,nf,oe,ue}_peng` | 4 | 0–3 | L | yes | CC compute (peng FP datapath: invalid/NaN/overflow/underflow) |
| `cc_seq_mem_{cor,unc}err` | 2 | 4–5 | L | yes | Q7 sequencer/core memory **ECC** |
| `cc_notific_intr[0..8]` | 9 | 6–14 | E | yes | notification engine (SW-NQ + AXI master) |
| `notify_error_wr_buffer_{drop,full}[0..7]` | 16 | 15–30 | E | yes | notification write-buffer (8 drop + 8 full) |
| `fis_cntrl_intr[0..4]` | 5 | 31–35 | E | no | FIS control (EP posted-wr slverr) |
| `fis_sprot_intr[0..1][0..5]` | 12 | 36–47 | E | no | FIS sprot — **2** instances × 6 causes |
| `fis_errtrig_intr[0..49]` | 50 | 48–97 | E | no | FIS errtrig: 25 user + 25 amzn |

4 + 2 + 9 + 16 (= 31 CDC) + 5 + 12 + 50 (= 67 FIS) = **98**. `[HIGH · OBSERVED]`

### 5c. Classification — instruction-event vs error vs ECC `[HIGH counts · MED labels]`

| Class | Cnt | Entries |
|---|---:|---|
| ECC / RAS (memory) | 2 | `cc_seq_mem_{cor,unc}err` |
| FP-status (compute exception state) | 4 | `fp_stat_*_peng` |
| Notification-plumbing error | 25 | `cc_notific_intr[0..8]` + `notify_error_wr_buffer_*` |
| FIS posted-wr / sprot / timeout | 17 | `fis_cntrl(5)` + `fis_sprot(12)` |
| FIS errtrig (re-export of notific) | 50 | `fis_errtrig_intr[0..49]` |

2 + 4 + 25 + 17 + 50 = **98**. Key findings: **no instruction-DONE source exists**
(`grep done|complete|finish` = 0) — completion is signalled out-of-band via SW
notification queues, and the only interrupts are notification **fault** conditions
(full/dropped/threshold/overlap) plus AXI-writer faults. The 6 LEVEL-triggered entries
are exactly the 2 ECC + 4 FP-status (sticky **state**); the other 92 are edge **events**.
`[HIGH · OBSERVED]`

### 5d. Routing — CC → INTC → apex `[HIGH count · MED binding]`

98 ≤ 128, so CC fits **one** 4-group × 32 INTC (no errtrig pair — unlike the
216–254-source SDMA/IO/PCIe/HBM/TPB/D2D leaves). The PREPROC domain instantiates both
a host-reachable MSIX leaf path and an on-die NO_MSIX aggregation path; the NO_MSIX
summary rolls up to **exactly 2** apex entries:

- apex idx **112** `cc_top_0_nmi` / `cc_top_0_summary` / `CC_TOP[0]` — level, **no** critical
- apex idx **113** `cc_top_1_nmi` / `cc_top_1_summary` / `CC_TOP[1]` — level, **no** critical

Verified `yq '.[112].critical // "none"'` = `none`. So the CC's apex contribution is a
generic per-cluster **summary**, not a critical fast-path — even uncorrectable Q7-mem
ECC (`cc_seq_mem_uncerr`) rides the generic `cc_top_*_summary`, not a dedicated
critical wire. `[HIGH · OBSERVED]`

> **CORRECTION — `cc_top_{0,1}` is *Compute-Cluster*, not "collective/comm".** The apex
> §1a table here labels these two sources `CC = Compute-Cluster`. An earlier apex
> classification rendered `cc_top_{0,1}` as "(collective/comm)"; that label is wrong —
> the `cc_triggers.yaml` content (`CC computes` / `Q7 memories`, zero collective
> vocabulary) and the [PREPROC/CC address subtree](../address/preproc-cc.md)
> (`PREPROC` = the 4-core Vision-Q7 compute cluster) establish CC = Compute-Cluster.
> `[HIGH · OBSERVED]`

> **NOTE — 4 PREPROC clusters, but only 2 `cc_top_*` apex summaries.** The address map
> finds four `PREPROC` instances, yet the apex carries only two `cc_top` summaries. The
> four clusters likely pair-aggregate into 2 `CC_TOP` summaries (or only 2 are
> summarized at the apex). Unresolved from shipped artifacts; flagged `[MED/LOW]`.

---

## 6. TOP_SP (Top-level Sequencer-Processor) — 82 sources `[HIGH · OBSERVED]`

**`top_sp_triggers.yaml`** is the interrupt leaf of **TOP_SP** = "TopSP", an
Xtensa-NX **sequencer-processor** core wrapped by the TPB-side service fabric, with its
own semaphore / event-notification `tsync` substrate (it doubles as collective-sequencer
"engine 5"; see [`rdm_model` + `top_sp_ram`](../csr/rdm-top-sp.md) #928). The
`top_sp_ram.json` CSR confirms the identity: `top_sp_whoami` = "BlockID … top_sp0-9 =>
0x98-0xa1" (10 blocks), `top_sp_nx_spc_lo/hi` = "Program counter for the **NX core** in
top_sp", and an `sw_queue` carrying implicit/explicit/event-semaphore/error notification
routing. `[HIGH · OBSERVED]`

> **NOTE — naming harmonized with #928.** [`rdm-top-sp.md`](../csr/rdm-top-sp.md) calls
> TOP_SP the "top-level service/sync processor … Xtensa-NX scalar core with an
> event/semaphore `tsync` substrate … engine 5". The trigger-leaf evidence (NX-core PC,
> `sp_nx_nt` / `sp_explicit_nt` / `events_semaphores_nt` SW-queues, `nx_interrupt[0..4]`
> sub-block) sharpens "SP" to **Sequencer-Processor**. Both describe the same block — the
> NX sequencer core with a semaphore/event unit; no CORRECTION needed.

> **GOTCHA — `top_sp_triggers.yaml` has *no* `# …` header banner** (line 1 is blank),
> unlike `cc_triggers.yaml` (`# Cayman TPB Triggers`). Do not key any parser on a
> leading comment for this file.

### 6a. Schema — minimal-5 + a 2-entry ECC nmi variant `[HIGH · OBSERVED]`

80 entries: `{trigger, name, description, needs_cdc, edge_triggered}` (the minimal-5
SDMA/IO/D2D baseline; `needs_cdc` present, **all false**). 2 entries (the ERG-ECC rows)
**drop** `needs_cdc` and **add** `nmi_mask` + `nmi_msix_mask` (both 0). Verified
`yq '[.[] | select(has("nmi_mask"))] | length'` = 2,
`yq '[.[] | select(has("source_clock"))] | length'` = 0,
`yq '[.[] | select(has("critical"))] | length'` = 0. **Not** a sixth schema, and **not**
the TPB `msix_mask`/`tog2pul_only` variant. `edge_triggered`: 74 edge, **8 level** (the
8 level sources = `err_sem_overflow` + 2 ERG-ECC + 5 NX-core). `[HIGH · OBSERVED]`

### 6b. Sub-block grouping `[HIGH · OBSERVED]`

| Sub-block | Cnt | idx | E/L | Notes |
|---|---:|---|---|---|
| **Semaphore** | 1 | 0 | L | `err_sem_overflow_triggers` — "Overflow from the top_sp semaphore" |
| **ERG ECC** | 2 | 1–2 | L | `intr_trig_{uncerr,corerr}` (the `nmi_mask` variant; nmi-summary feed) |
| FIS cntrl | 5 | 3–7 | E | `fis_cntrl_intr[0..4]` — EP posted-wr slverr (AMZN/USER/USER-FIS) |
| FIS sprot | 6 | 8–13 | E | `fis_sprot_intr[0..5]` — **1** instance (vs CC's 2); incl `[5]` spare |
| FIS errtrig | 50 | 14–63 | E | `fis_errtrig_intr[0..49]` — 25 user + 25 amzn |
| Instruction notif | 13 | 64–76 | E | `top_sp_notific_intr[0..8]` (9) + `..._wr_buffer_{full,drop}[0..1]` (4) |
| **NX sequencer core** | 5 | 77–81 | L | `nx_interrupt[0..4]` (`nx_non_fatal`, `nx_fatal`, +3) |

1 + 2 + 5 + 6 + 50 + 13 + 5 = **82**. `[HIGH · OBSERVED]` The FIS shim (cntrl + sprot +
errtrig = 61) shares the same user/amzn errtrig 25-cause family as the D2D/SDMA leaves —
see [errtrig/FIS routing](./errtrig-fis-routing.md).

> **NOTE — TOP_SP is *not* the SoC peripheral aggregator.** Despite "Top-SP" sounding
> board-level, the file has **zero** watchdog/timer/GPIO/SPI/I2C/boot/reset/power/PVT/
> doorbell vocabulary. Those SoC-top peripherals are **direct apex bits** in `peb_intc`
> (`ring_io_top_gpio_intr_local`, `i2c_intr`, `spis_*`, `misc_axi2apb_*` — §1a/§2), not
> TopSP-leaf sources. TopSP is the **sequencer-service block**. `[HIGH · OBSERVED]`

### 6c. Global-sync / semaphore tie `[HIGH location · MED binding]`

TopSP is a **local** semaphore host. The sync tie runs through three surfaces: (a) the
TopSP-local semaphore — `err_sem_overflow_triggers` (idx 0) + `top_sp_ram.timestamp_inc`
("Increment value for the timestamp counters in TPB SP and semaphore blocks") +
`sw_queue.events_semaphores_nt`; (b) the `EVT_SEM` / `TPB_EVT_SEM` aperture (the global
event/semaphore `tsync` block — SET/INC/DEC/READ programming surface, see
[#928](../csr/rdm-top-sp.md)); (c) the TPB `events_semaphores` CSR bundle (same
`tpb_xt_local_reg.json` LOCAL_REG IP). The `err_sem_overflow` trigger is the
threshold/overflow interrupt of this bundle. `[HIGH loc · MED bind]` The NCFW
device-`tsync` software binding is deferred to the interrupt-map synthesis (not
assertable from this leaf alone) — flagged `[LOW]`, not fabricated.

### 6d. Routing — TOP_SP → INTC → apex `[HIGH · OBSERVED]`

82 ≤ 128, so the TopSP leaf fits **one** 4-group × 32 INTC (no errtrig pair). The leaf
`nmi_out` feeds **exactly one** apex bit:

- apex idx **77** `top_sp_combined_nmi` / `top_sp_combined_summary` / `TopSP[0-9]` —
  level, **`critical=0`** (verified). 10 TopSP blocks pre-OR'd at the leaf into one apex
  bit (same fan-in-folding as `d2d_combined_nmi`).

The 2 ERG-ECC rows (the `nmi_mask` feeders inside the leaf) surface through this combined
bit. **No critical fast-path at the apex** — the fastest TopSP escalation is `nx_fatal`
(idx 78, level) → leaf INTC → `top_sp_combined_nmi` summary; the SoC-survival fast-paths
(HBM cattrip, NSM timeout, PCIe reset) get the dedicated critical bits, TopSP does not.
`[HIGH absence · MED rationale]`

---

## 7. Final routing — 128 apex inputs → Q7 / management core / GIC

The consolidated chain, assembled from the apex YAML + the intc CSR schemas + the leaf
trigger reports:

```text
  LEAF DOMAIN INTCs (per-domain errtrig pairs)
    HBM / TPB / SDMA / PCIe (incl isolation-SM reset_handshake[8..15])
    D2D / CC / TOP_SP / RDM / NSM
        │  each leaf u_amzn_errtrig → one nmi_out / *_summary wire
        ▼
   ┌───────────────────────────────────────────────────────────┐
   │  peb_intc  =  THE APEX INTC                                │
   │  4 groups × 32 = 128-input bus (NUM_OF_TRIGS=128)          │
   │  96 summary wires + 32 direct PEB-local criticals          │
   │  YAML file order = bit order → 4×32 pending image → output │
   └───────────────────────────────────────────────────────────┘
        │  (one rolled-up apex output; the apex's own internal
        │   errors feed intc_nmi at idx 79 — self-monitoring)
        ▼
   ┌───────────────────────────────────────────────────────────┐
   │  IOFIC / io_fabric APB intc cascade (1-group ap_intc)      │
   └───────────────────────────────────────────────────────────┘
        ▼
   Q7 / management-core ("Pacific") IRQ line(s)  and/or a GIC    [INFERRED]
```

Routing certainty — what *is* and is *not* in the schema:

| Claim | Confidence |
|---|---|
| 128 apex sources enumerated, classified, file-order = bit-order | `HIGH · OBSERVED` |
| apex = 4×32 = 128-input INTC, sources fill the bus 1:1 (`NUM_OF_TRIGS=128`) | `HIGH · CARRIED (#923)` |
| apex `*_nmi`/`*_summary` feed upward into the IOFIC/io_fabric cascade | `HIGH chain` |
| **the exact `apex-pending-bit → Q7/GIC vector` hop** | `[INFERRED]` |
| a GIC *exists* (CXELA500 ELA outputs target "a Generic Interrupt Controller (GIC) as an IRQ input") | `HIGH GIC exists` |
| `peb→Q7` mapping · exact vectors | `MED · LOW` |

> **The `apex → Q7/GIC` final hop is INFERRED, kept flagged.** The exact map of which
> apex pending-bit drives which Q7/GIC vector line is **not register-encoded** in any
> shipped artifact — it lives in firmware/HW, not the YAML or intc JSON. The CXELA500
> ELA outputs confirm a GIC *exists*; the apex→core vector binding is not stated. This
> page does **not** assert the final hop as fact — consistent with
> [`intc-1group-apintc.md`](../csr/intc-1group-apintc.md) §6 (#923). `[INFERRED]`

> **NOTE — the apex→group-bit packing (idx>>5 = group, idx&31 = bit) is the 4×32 model,
> INFERRED from file order**, not literally stamped per entry. The blank-line clusters
> in the YAML do **not** align to 32-boundaries — use the `idx>>5` arithmetic, not the
> file's blank lines, to read the group cut. `[MED]`

---

## 8. Cross-gen diff (Cayman / Sunda / Maverick) `[HIGH · OBSERVED]`

| Set | Cayman | Sunda | Maverick (v5) |
|---|---:|---:|---:|
| PEB apex | **128** (32 crit) | 97 (35 crit) | 119 (79 crit) |
| CC | **98** | — (no `cc` leaf) | 96 |
| TOP_SP | **82** | 76 | 80 |

- **PEB apex.** Cayman = the canonical named 4×32 apex. Sunda (97) is a finer per-instance
  tap: SDMA 16/engine (32 total vs Cayman's 64), TPB 1/engine, PCIe_B/C per-instance,
  TOP_SP per-instance (`top_sp_{0..5}_nmi`), plus 4 direct PCIe reset/isolation
  handshake criticals (§4); it does **not** saturate a 4×32 bus.
  **Maverick (119)** is a security-hardened rewrite (header still `# Cayman …`): adds a
  `functional_test_required` per-source key, replaces named summaries with generic
  `*_vec_q` vector arrays (`blk_nmi_vec_q[0..31]`, `pvt_intr_vec_q[0..7]`, …), adds a wide
  parity/CRC RAS surface (`spis_*_parity_err`, `parity_{addr,data}_error_interrupt`),
  and marks **79** sources critical. `[v5 header/keys OBSERVED · v5 interior INFERRED]`
- **CC.** Cayman 98 → Maverick 96: Maverick collapses the 5-bit `fis_cntrl_intr[0..4]`
  into **one** `fis_cntrl_intr` and adds `parity_{addr,data}_error_interrupt` (−5 +1 +2 =
  −2). The 31-entry CDC compute/Q7/notific core is **gen-stable**. Sunda ships no `cc`
  leaf (different intc model); Mariana/Mariana+/Tonga ship no trigger-YAML dir.
- **TOP_SP.** Invariant 21-source core (sem 1 + ECC 2 + notif 9 + wr_buf 4 + NX 5) across
  all three; only the FIS-shim packaging differs. Maverick (80) collapses `fis_cntrl` to
  1 + adds 2 parity + repurposes `fis_sprot[5]` spare → `axi_checks`. Sunda (76) folds
  the entire FIS shim into one `top_sp_sprot_intr[0..54]` bus and exposes TopSP
  per-instance at the apex. `[HIGH · OBSERVED]`

---

## 9. Verified-fact recap

- **Counts** (re-grounded to `yq 'length'`): PEB apex **128**, CC **98**, TOP_SP **82**.
- **Apex aggregation model**: 4×32 = 128-input bus, file order = bit order; 96 summary
  wires (per-domain `*_nmi`/`*_summary`) + 32 direct PEB-local criticals = 128. Classified
  by downstream domain (SDMA 64 dominant; full table §1a).
- **Critical/summary split**: exactly **32** `critical:1` (HBM-thermal 4, PVT 7, SPI 8,
  axi2apb 4, NSM 1, ERG 2, io_fabric-apbblk 1, APB-flush 2, GPIO/I2C/misc_ram 3); the
  other 96 are non-critical (80 summary + 2 PCIe-path + 14 FIS/APBBLK shape-D). TPB and
  D2D have **no** fast-path; HBM-thermal **bypasses** the generic `hbm_*_nmi` summary.
- **PCIe isolation-SM** sources are in the PCIe **leaf** (`reset_handshake_intr[8..15]`),
  summarized into the apex via `pcie_*_nmi`; only NSM-AXI-timeout has a **direct**
  critical apex bit (`intr_peb_nsm_axi_timeout`, idx 111) — NSM reaches the core by two
  paths.
- **Apex → Q7/GIC** final hop is **`[INFERRED]`** (CXELA500 ELA confirms a GIC exists;
  the exact pending-bit → vector map is firmware-owned, not in the schema).
- **CORRECTIONS raised**: (1) vs #923 §6 L2 — the apex does **not** carry `source_path`
  on all 128 (only **2**) nor `nmi_mask` on all 128 (only **114**). (2) `cc_top_{0,1}`
  is **Compute-Cluster**, not "collective/comm".
