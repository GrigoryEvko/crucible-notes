# Interrupts — PCIe / HBM / TPB / D2D Sources

The **four large per-domain interrupt-trigger sets** of the Cayman SoC: the PCIe
host bridge (**228** sources), the HBM memory subsystem (**223**), the TPB tensor
cluster (**216**), and the D2D die-to-die fabric (**216**). Each is an RTL-generated
`*_triggers.yaml` table whose entries fill the bit positions of one **errtrig pair**
of [4-group INTC](../csr/intc-4group.md) instances and roll up into the
[PEB_INTC apex](peb-cc-topsp-triggers.md). The CSR blocks these triggers *report on*
are documented in the companion page [HBM / D2D / PCIe CSR blocks](../csr/hbm-d2d-pcie-blocks.md);
the INTC unit they *latch into* is the [4-group INTC](../csr/intc-4group.md).

The single highest-value finding on this page is **schema variation**: the four
domains' YAMLs have **four different shapes** — the per-entry key set, the
edge/level polarity, and the bus structure all differ per domain. A control-plane
reimplementation must parse each domain's table against its own schema, not a shared
one. This page documents each domain's schema, its edge/level/CDC handling, its
bus decomposition, and its routing into the INTC 4-group + apex.

Everything below is derived from four byte-exact shipped artifacts —
`pcie_triggers.yaml`, `hbm_triggers.yaml`, `tpb_triggers.yaml`, `d2d_triggers.yaml`
(all four banner-named for the Cayman / Sunda complex) — re-counted with `yq` and
cross-referenced to the INTC/CSR register schemas in this repository. Recovered
names, descriptions and field values quoted here are the artifacts' own verbatim
text. No external source tree is referenced.

> **Confidence legend.** `HIGH` = literal value read from the YAML / re-counted by
> `yq`. `MED` = inference from naming + CSR cross-reference, well supported.
> `LOW` = plausible, flagged, not stated by the artifact. `OBSERVED` = read from a
> shipped file. `INFERRED` = reasoned from corroboration. `CARRIED` = forwarded
> from a sibling page.

> **GENERATION WALL.** The detailed per-source tables on this page are the
> **Cayman / NC-v3** anchor — every count below `yq`-verified against the
> `cayman-arch-regs` package. The cross-gen deltas (§7) cite the **Maverick / NC-v5**
> and **Sunda** `arch-headers` variants; for those only the *header / entry count*
> is `OBSERVED` and the **v5 interior is `INFERRED`** (the Maverick 7-entry abstract
> D2D and the 217-entry TPB tail are header-observed, interior unverified). Do not
> read v5 facts as Cayman facts. Consistent with the same v3/v5 wall called out in
> [the HBM/D2D/PCIe CSR page](../csr/hbm-d2d-pcie-blocks.md).

---

## 0. The four counts and the schema-variation matrix

All four primary counts re-derived with `yq 'length'` against the
`cayman-arch-regs_tgz/intc/*_triggers.yaml` files. `[HIGH/OBSERVED]`

| Domain | Count | Distinct buses | Key-union size | Schema family |
|--------|------:|---------------:|---------------:|---------------|
| **PCIe** | **228** | 10 | 9 | rich `nmi_*` + `source_*` |
| **HBM** | **223** | 8 | 9 | rich `nmi_*` + `source_*` (same family as PCIe) |
| **TPB** | **216** | 4 | 9 | **third variant**: `msix_mask` + `tog2pul_only`, **no** `nmi_*` |
| **D2D** | **216** | 7 | **5** | **minimal** (SDMA/IO-fabric family) |

The **schema-variation matrix** — the central insight — is the per-key presence
across the four domains. `[HIGH/OBSERVED]` (each cell `yq '[.[]|keys[]]|unique'`):

| key | PCIe | HBM | TPB | D2D | note |
|-----|:----:|:---:|:---:|:---:|------|
| `trigger` | ✓ | ✓ | ✓ | ✓ | always present (228/223/216/216) |
| `name` | ✓ | ✓ | ✓ | ✓ | always present |
| `description` | ✓ | ✓ | ✓ | ✓ | PCIe has 1 null value; HBM has 31 placeholders |
| `edge_triggered` | ✓ | ✓ | ✓ | ✓ | bool, always present |
| `needs_cdc` | partial | ✓ | ✓ | partial | PCIe 212/228 · D2D 67/216 |
| `nmi_mask` | ✓ (149) | ✓ (all=1) | — | — | TPB/D2D **lack** it |
| `nmi_msix_mask` | ✓ (149) | ✓ (all=1) | — | — | co-occurs with `nmi_mask` |
| `source_clock` | ✓ (95) | ✓ (196) | ✓ (126) | — | D2D **lacks** it (CDC internal) |
| `source_reset_n` | ✓ (95) | ✓ (196) | ✓ (126) | — | mirrors `source_clock` |
| `msix_mask` | — | — | ✓ (131, all=0) | — | **only** TPB — refutes "msix_mask never present" |
| `tog2pul_only` | — | — | ✓ (4, all=true) | — | **only** TPB — exists in no sibling |

Four key conclusions sit in that matrix:

1. **PCIe and HBM share the rich superset** (`nmi_mask`/`nmi_msix_mask` +
   `source_clock`/`source_reset_n`). HBM is the structural twin of PCIe.
2. **TPB is a third, distinct shape**: it drops the `nmi_*` pair and adds two keys
   found in *no other domain* — `msix_mask` (present on 131, all value `0`) and
   `tog2pul_only` (present on 4, all `true`).
3. **D2D collapses to the minimal 5-key shape** — the same family as
   [SDMA](sdma-triggers.md) / [IO-fabric](io-fabric-triggers.md). No `nmi_*`, no
   `source_*`, no `msix_mask`, no `tog2pul_only`.
4. **`source_clock`/`source_reset_n` always co-populate exactly the
   `needs_cdc==true` set** in every domain that carries them — they name the *INTC-side
   destination clock/reset* the CDC synchroniser samples the source into. D2D omits
   them because it does its CDC *inside the controller* (see §6).

> **QUIRK — `msix_mask` is NOT a package-wide invariant.** The SDMA/IO-fabric and
> HBM/PCIe enumerations report `msix_mask` as *absent*. TPB **carries it on all 131
> of its TPB-internal sources** (value `0` throughout). Any tool that assumes
> `msix_mask` is never present will silently mis-parse the TPB table. `[HIGH/OBSERVED]`

---

## 1. Edge / level / CDC — the per-domain polarity (and the HBM inversion)

`edge_triggered` re-counted per domain (`yq '[.[]|select(.edge_triggered==…)]|length'`):

| Domain | edge (`true`) | level (`false`) | who is which |
|--------|------:|------:|------|
| **PCIe** | **216** | **12** | level = 4 Core-ELA + 6 `fis_sprot` + 2 `nts_iso` timeouts |
| **HBM** | **31** | **192** | **INVERTED** — level = all 192 controller status bits |
| **TPB** | **158** | **58** | level = ECC/parity + NX-IRQ + per-engine FP-status |
| **D2D** | **67** | **149** | level = ERG + all 148 controller/PHY status bits |

> **GOTCHA — the HBM edge/level inversion.** Every sibling domain is *edge-majority*
> (PCIe 216/12, TPB 158/58, D2D 67/149-but-edge-FIS). **HBM is the only inverted
> table: 192 level / 31 edge.** The 192 `hbm_ctrl_interrupt_gen[ch][cause]` entries
> are **level** because they are persistent sticky status bits in the DDR
> controller's interrupt register (sticky ECC / temperature / parity state). The 31
> on-die events (ELA, scrubber-done, HPR-done, corr/uncorr OR-pulse, FIS-control
> pulse, xbar-error pulse) are **edge** one-shot completion/error pulses. A
> reimplementation that hard-codes "controller interrupts are edge" (true for PCIe)
> will mis-configure every HBM channel cause. `[HIGH/OBSERVED]`

The `edge_triggered` bit binds **directly** to the INTC `int_posedge_grp` register
(per-bit: `1` ⇒ posedge/edge, `0` ⇒ level) — see
[INTC §6.4](../csr/intc-4group.md). The trigger table is the source-of-truth for how
firmware must program `int_posedge_grp` for each domain's bit range. `[HIGH/OBSERVED leaf; CARRIED INTC]`

**`needs_cdc` and the source-clock pairing** (`yq` counts):

| Domain | cdc `true` | cdc `false` | cdc absent | the CDC-destination clock (on the `true` set) |
|--------|------:|------:|------:|------|
| **PCIe** | 95 | 117 | 16 | `intc_core_clk` / `intc_core_reset_n` |
| **HBM** | 196 | 27 | 0 | `dfi_hdr_clk_occ_out` / `dfi_hdr_rst_n` (DFI header clk) |
| **TPB** | 130 | 86 | 0 | `clk_core_gated` (×125) / `core_reset_extended_n`; `clk_mem_top` (×1, the lone `ham_intr`) |
| **D2D** | **0** | 67 | 149 | **none** — D2D declares `needs_cdc=true` on *zero* sources |

- **PCIe**: the 95 `needs_cdc==true` are *exactly* the 95 `intc_triggers_core_out`
  bus (the DWC-controller-clock sources that must cross into the INTC core clock);
  the 16 `needs_cdc`-absent entries are *exactly* the 16 `reset_handshake_intr` (the
  isolation FSM, already in the INTC clock domain). `[HIGH/OBSERVED]`
- **HBM**: the 196 `needs_cdc==true` = the 192 controller + 4 ELA (DFI-clock origin);
  the 27 `false` are the on-die aggregators (scrubber/HPR/per-ch-OR/FIS/xbar). `[HIGH/OBSERVED]`
- **TPB**: `source_clock` present on 126 of the 130 CDC sources; the 4 missing carry
  `tog2pul_only` instead (the PE-array FP-summary edge-detect path replaces the simple
  CDC sampler). `[HIGH/OBSERVED]`
- **D2D**: `needs_cdc` is **present on idx 0..66 only (the 67 FIS sources, all `false`)
  and absent on idx 67..215**. *No* D2D source is marked CDC-needed. The artifact's
  own verbatim NOTE comments explain why — *"although on core domain CDC happens at
  controller (rather than FIS) so no CDC needed here"* and *"although on different
  clock domain CDC happens at MPCS (rather than FIS) so no CDC needed here"*. The D2D
  blocks do their **own** clock-domain crossing internally and present pre-synchronised
  triggers, so the INTC needs no per-trigger CDC metadata (and hence no `source_clock`).
  `[HIGH/OBSERVED]`

---

## 2. PCIe — 228 sources, the Synopsys DWC pcie5 + e32mp PHY surface

`pcie_triggers.yaml` (banner `# Sunda PCIe Triggers`), 228 entries on 10 buses.
Controller IP = `pcie5_x8_DWC_pcie_ctl` (DWC PCIe5 **x8**), PHY =
`dwc_e32mp_phy_x4_ns` (DWC e32mp x4, ×2 → x8 lanes) — see
[CSR §III](../csr/hbm-d2d-pcie-blocks.md). `[HIGH/OBSERVED]`

| bus | count | idx | function |
|-----|------:|-----|----------|
| `intc_triggers_core_out` | **95** | 0..94 | DWC controller core IRQs (the "Tonga A/B/C/D" groups) |
| `fis_errtrig_intr` | 50 | 0..49 | FIS instruction-notification errors (user 0..24 + amzn 25..49) |
| `serdes_int_triggers_out` | 38 | 0..41 | 2 PHYs × 4 lanes — **gaps at idx 2,3,23,24** |
| `reset_handshake_intr` | 16 | 0..15 | FLR/SBR/link-down isolation FSM (CDC-absent) |
| `intc_triggers_a_axi_out` | 15 | 0..14 | PCIe AXI-bridge parity/completion/RASDP errors |
| `fis_sprot_intr` | 6 | 0..5 | FIS stream-protection vector (LEVEL) |
| `fis_cntrl_intr` | 5 | 0..4 | FIS EP posted-write slave errors |
| `intr_trig_uncerr` | 1 | scalar | `erg_uncerr` — ERG uncorrectable error |
| `nts_iso_r_timeout` | 1 | scalar | NTS isolation R-channel (read-data) timeout (LEVEL) |
| `nts_iso_b_timeout` | 1 | scalar | NTS isolation B-channel (write-resp) timeout (LEVEL) |

The 95 `core_out` triggers decompose (verbatim names, by sub-function): 2 AER
pended-status, 24 per-lane PHY-EQ (8 lanes × 3 of rxeqeval/txdeemph/phy_status), 4
config/VPD/FLR, 3 AER root-error, 8 PME/power, 4 RX-queue/CRC diag, 3 inbound
err-messages, 3 vendor/unlock, 3 outbound err-message-sent, 4 link-mgmt/BW/EQ, 1
completion-timeout, 3 core-RAM parity, 1 surprise-down, 3 LTSSM, 2 rate/preset, 1
CDM-ack, 3 link up/down, 2 core-reset, 3 hot-plug, 1 AER-hdr-log, 13 `cfg_*` AER
detail, and **4 Core-ELA GP outputs** (`core_ela_output_0..3` at idx 91..94, the
only LEVEL core_out entries — ARM CXELA500 ELA-500 outputs). `[HIGH/OBSERVED]`

> **NOTE — no doorbells, no PTM.** `pcie_triggers.yaml` is the PCIe
> *controller/PHY/isolation* interrupt map. DMA-completion doorbells live in
> [`sdma_triggers.yaml`](sdma-triggers.md), not here. PTM triggers are absent
> (`rg` → 0). State this explicitly; do not fabricate doorbell sources for PCIe. `[HIGH/OBSERVED]`

PCIe verbatim-artifact quirks: `serdes` index gaps at 2,3,23,24 (omitted, mirrored
across the phy0/phy1 split); one **null description** at `core_out[46]` =
`msg_unlock`; a name typo `isolatio_sm_nsm_axi_timeout_detected` (missing the final
`n`); a section-comment typo `# All available FIS_ERRRIG triggers` (triple-R). `[HIGH/OBSERVED]`

---

## 3. HBM — 223 sources, the 16×12 controller matrix + on-die RAS

`hbm_triggers.yaml` (banner `# Sunda HBM Triggers`), 223 entries on 8 buses.
Controller = `ddr_csr_apb`, PHY = `dwc_hbmphy_top` — see
[CSR §I](../csr/hbm-d2d-pcie-blocks.md). HBM3 is confirmed in-text (the on-die-ECC
cause descriptions name "HBM3 on-die ECC logic"). `[HIGH/OBSERVED]`

| bus | count | idx | function |
|-----|------:|-----|----------|
| `hbm_ctrl_interrupt_gen` | **192** | 0..191 | **16 channels × 12 error-types** (channel-major) |
| `intr_trig_corerr_uncerr_or` | 16 | 200..215 | per-channel correctable+uncorrectable OR rollup |
| `fis_cntrl_intr_hbm_top_stg` | 5 | 216..220 | HBM-top FIS shim EP slave-write errors |
| `hbm_ela_interrupt` | 4 | 192..195 | HBM embedded logic analyzer (ARM CoreSight ELA) |
| `hbm_sbr_int_trigger` | 3 | 196..198 | scrubber: done / busy / read-error (`hbm_scbr`) |
| `intr_trig_uncerr_xbar` | 1 | 221 | HBM crossbar uncorrectable RAS rollup (`hbm_xbar_cfg`) |
| `intr_trig_corerr_xbar` | 1 | 222 | HBM crossbar correctable RAS rollup |
| `hbm_hpr_done_int_trigger` | 1 | 199 | hardware post-package-repair done (`hbm_hpr`) |

The dominant bus is a **dense 16×12 matrix** (`yq` confirms exactly 12 entries per
channel for all 16 channels). The second index `M` selects one of **12 controller
error-types**, with descriptions identical across channels modulo the `_chN`
substitution `[HIGH/OBSERVED]`:

| M | cause | class |
|--:|-------|-------|
| 0 | uncorrectable DQ-parity error on **write** | RAS (parity) |
| 1 | uncorrectable DQ-parity error on **read** | RAS (parity) |
| 2 | 1-bit ECC error on read | RAS (SBE) |
| 3 | 1-bit ECC error **above `cfg_ecc_1bit_int_thresh`** | RAS (SBE) |
| 4 | 2-bit ECC error on read | RAS (DBE) |
| 5 | **catastrophic temperature trip** (`CATTRIP` pin; sticky, power-cycle only) | THERMAL/CRIT |
| 6 | temperature-sensor output change (`stat_dfi_tcr_temp`) | THERMAL |
| 7 | `dfi_training_complete` low→high | TRAINING |
| 8 | `dfi_init_complete` low→high | INIT |
| 9 | 1-bit **HBM3 on-die** ECC error (prior-set read) | RAS (SBE) |
| 10 | 2-bit **HBM3 on-die** ECC error (returning read) | RAS (DBE) |
| 11 | OR-of-all-other controller interrupts (`interrupts[31:0]` catch-all) | catch-all |

So `M=11` confirms the exposed 12 causes are a **curated projection of a wider
32-bit-per-channel** controller interrupt register — only 11 specific causes are
individually wired out, with all remaining bits collapsed into the catch-all.
**RAS coverage = 7 of the 12 causes per channel** (M=0,1 DQ-parity; M=2,3 SBE;
M=4 DBE; M=9,10 on-die SBE/DBE), i.e. 7 × 16 = 112 RAS leaf bits, plus scrubber(3)
+ HPR(1) + per-channel-OR(16) + xbar(2) = **134 of 223 HBM triggers are
RAS/ECC/parity/repair/scrub**. `[HIGH/OBSERVED]`

The supporting silicon-side registers are in `ddr_csr_apb`: `CFG_ECC_1BIT_INT_THRESH`
(cited verbatim by M=3), `STAT_DFI_CATTRIP` (M=5), `STAT_DFI_TCR_TEMP` (M=6), and
the `INIT_INTERRUPT_MASK_0`/`INIT_INTERRUPT_GEN_0`/`STAT_INTERRUPT_0` register family
that the bus name `hbm_ctrl_interrupt_gen` mirrors. `[HIGH/OBSERVED]`

> **CORRECTION vs the CSR sibling — DFI completion is sideband, not a PHY register.**
> The M=7 (`dfi_training_complete`) and M=8 (`dfi_init_complete`) triggers come from
> the PHY *via DFI sideband*, **not** from `dwc_hbmphy_top` registers: per
> [CSR §I-2](../csr/hbm-d2d-pcie-blocks.md) the PHY exposes **no trigger CSR** — the
> DFI sideband is latched in `ddr_csr_apb`'s `STAT_DFI_*` registers, which is where
> these two triggers actually originate. The controller-vs-PHY source binding here is
> therefore `ddr_csr_apb` (latch), not `dwc_hbmphy_top` (signal). `[HIGH/OBSERVED]`

HBM data-quality: all 31 non-controller entries (idx 192..222) carry **placeholder
descriptions** (`description == name`, no prose); the `OR_pseudo_ch0_ch1_` name prefix
is a frozen 2-pseudo-channel template artifact (the real channel is the trailing
`_chN`); `fis_cntrl_intr_hbm_top_stg` uses bracketed `trigger` but unbracketed `name`. `[HIGH/OBSERVED]`

---

## 4. TPB — 216 sources, the flat `errtrig_in` bus + FIS tail

`tpb_triggers.yaml` (banner `# Sunda TPB Triggers`), 216 entries on **only 4** buses.
The defining structural difference from HBM/PCIe: the **131 TPB-internal sources are
all carried on a single flat `errtrig_in[N]` bus** — the per-source identity lives in
the `name` field, and the bus index `N` is the INTC trigger-bit position. `[HIGH/OBSERVED]`

| bus | count | idx | function |
|-----|------:|-----|----------|
| `errtrig_in` | **131** | 0..130 | flat TPB-internal bus (ECC/parity, NX-IRQ, FP-status, NOTIFIC, HAM) |
| `fis_errtrig_intr` | 50 | 131..165 | FIS instruction-notification (user 25 + amzn 25) |
| `fis_sprot_intr` | 30 | 136..165 | **5 sprot instances × 6 causes** (AXI-protection) |
| `fis_cntrl_intr` | 5 | 131..135 | FIS EP posted-write slave errors |

The 131 `errtrig_in` sources group by `name` into ~16 sub-engines:

| family | count | sources |
|--------|------:|---------|
| memory ECC/parity | 12 | SBUF/PSUM parity, TPB_TOP + DVE/ACT/POOL seq-mem corerr/uncerr, ACT-engine PWL parity, PSUM collision |
| NX-core IRQ lines | 25 | 5 each for **SP / DVE / ACT / POOL / PE** (`*_nx_interrupt_0..4`) |
| FP-exception status | 20 | 4 causes {INVALID, NaN, OVERFLOW, UNDERFLOW} × 5 domains {PSUM, ACT, POOL, DVE, **PE-array**} |
| NOTIFIC engine | 69 | 9 `tpb_notific_intr_0..8` + 30 wr-buffer-drop + 30 wr-buffer-full |
| event / semaphore | 2 | `err_event_summary` (double set/clear) + `err_sem_summary` (over/underflow) |
| AXI / fake / HAM | 3 | `axi_resp_err`, SW-injected `fake_error`, `ham_intr` (activity monitor) |

The TPB schema carries two keys found in no other domain. **`msix_mask`** is present
on exactly the 131 TPB-internal entries (idx 0..130), value `0` throughout, and absent
on the 85 FIS entries — a clean schema split at idx 130/131. **`tog2pul_only`** is
present on exactly the 4 `fp_state_*_array_summary` entries (idx 53..56), value `true`,
and these 4 are the *only* `needs_cdc==true` entries that omit `source_clock` (the
toggle-to-pulse edge-detect path replaces the simple CDC sampler — they are forced
`edge` while their 16 per-engine FP-status siblings are `level`). `[HIGH/OBSERVED]`

The lone `clk_mem_top`/`pe_reset_extended_n` source-clock entry is `ham_intr`
(idx 130) — every other CDC source samples into `clk_core_gated`/`core_reset_extended_n`.
`[HIGH/OBSERVED]`

> **GOTCHA — the NX-IRQ description copy-paste bug.** All 20 `dve_/act_/pool_/pe_
> nx_interrupt_0..4` entries carry the **wrong** description text — *"SP NX Interrupt
> 0..4"* was duplicated from the SP block without renaming. Only the 5
> `sp_nx_interrupt_*` (idx 12..16) descriptions match their names. The `name` field is
> authoritative; the `description` is wrong for 20 of the 25 NX-IRQ entries. `[HIGH/OBSERVED]`

Routing of the TPB internals binds tightly to TPB CSRs: the 9 NOTIFIC bits map 1:1
to `notific_10_queue` bit0..8 (see [notific-queue](../csr/notific-queue.md)),
`fake_error` (idx 0) ↔ `tpb.fake_error_ctrl/_data`, and the **256-bit
`tpb.intc_bypass` map** (`8 × 32`, base `0xE00`) gives the firmware mask for every
trigger and proves the 256-bit (= one errtrig pair) capacity (see [tpb CSR](../csr/tpb.md)). `[HIGH/OBSERVED]`

---

## 5. D2D — 216 sources, the structured `subsys_intr` path (Synopsys + Marvell)

`d2d_triggers.yaml` (banner `# FIS`), 216 entries. Two structural oddities set D2D
apart: (a) the file **leads** with the FIS shim (idx 0..66) rather than tailing it,
and (b) the 148 D2D-subsystem sources use a **structured hierarchical trigger path**
`subsys_intr.<block>[<inst>].<domain>.<signal>` (e.g.
`subsys_intr.ctrl[0].axi.core_rst_n_assert`) — not a flat `errtrig_in[N]` (TPB) nor
per-block named buses (HBM). `[HIGH/OBSERVED]`

| family | count | idx | IP / source |
|--------|------:|-----|-------------|
| `fis_cntrl_intr` | 5 | 0..4 | FIS EP posted-write slave errors |
| `fis_errtrig_intr` | 50 | 5..54 | FIS instruction-notification (user 25 + amzn 25) |
| `fis_sprot_intr` | 12 | 55..66 | **2 sprot instances × 6 causes** (vs TPB's 5 instances) |
| `erg_intr_trig_uncerr` | 1 | 67 | ERG uncorrectable error (`erg_ecc_model`) |
| `subsys_intr.ctrl[0..1]` | **114** | 68..173 + 208..215 | 2× `snps_ctrl` (DWC PCIe) — 26 AXI + 31 core each |
| `subsys_intr.mpcs[0..1]` | 24 | 174..197 | 2× `mrvl_mpcs_x16` (Marvell MPCS, 12 each) |
| `subsys_intr.xsr[0]` | 10 | 198..207 | 1× `mrvl_xsr_phy` (Marvell XSR SerDes) |

`ctrl[0]` and `ctrl[1]` are **byte-symmetric** (the 57-source suffix sets are identical
under `diff`, differing only in the `ctrl0_`/`ctrl1_` name prefix); likewise
`mpcs[0]≡mpcs[1]`. So the D2D subsystem = **2 DWC-PCIe controllers + 2 Marvell MPCS
coding instances + 1 Marvell XSR PHY**. The controller IP is `snps_ctrl` — the *same*
DWC PCIe controller family as the host PCIe bridge (§2), **repurposed as the
die-to-die link controller**; its trigger surface is therefore PCIe-controller
link/LTSSM/RASDP/TLP/DLLP/parity signals plus Marvell SerDes PHY health signals. See
[CSR §II](../csr/hbm-d2d-pcie-blocks.md). `[HIGH/OBSERVED]`

The D2D controller error/link surface (level, all CDC-absent): per-`ctrl` link-state
(`smlh_link_up/down`, `rdlh_link_up/down`, `link_down`, `link_req_rst_assert`,
`ltssm_cmp_match[0..1]`), RAS (`axi_if_parity_error`, AXI+core `ram_addr_parity`,
`datapath_parity_error`, the `uncor_internal`/`mlf_tlp`/`ecrc`/`bad_dllp`/`bad_tlp`
PCIe-protocol set, `mstr/slv_rasdp_err_mode`), and the **8 late-tail** sources
(`replay_timer_timeout` + `msg_send_{corr,nf,f}` per ctrl, appended at idx 208..215).
The XSR PHY contributes eye-quality, HS-PLL-lock, port-ready, PHY-MCU-int, memory-ECC,
and the **MCU watchdog** (`mcu_wdt`, a hung PHY MCU forcing a full PHY reset/reinit). `[HIGH/OBSERVED]`

> **NOTE — the late-tail proves YAML-order == INTC-bit-order.** The file's verbatim
> comment at the 8 late-tail sources reads *"these were added late in Cayman so adding
> at end rather than with other controller interrupts to prevent existing interrupt
> indexes changing"*. This is the artifact author confirming that the YAML order **is**
> the INTC bit index and that the index ordering is a stable contract — new sources are
> appended, never inserted. The same ordering inference for the other three domains is
> corroborated by this NOTE. `[HIGH/OBSERVED]`

> **GOTCHA — D2D carries NO isolation-SM surface.** Unlike PCIe (§2), the D2D file
> has **no** `reset_handshake_intr`, **no** `isolation_sm_*`, **no** `nts_iso_*_timeout`
> (`rg "isolation"` → 0 hits). The PCIe-style isolation/quiesce state machine is a
> host-PCIe-interface concern; the die-to-die link presents only the controller's own
> level reset/link-fault signals (`core_rst_n_assert/deassert`, `sticky_rst_n`,
> `link_down`, `link_req_rst_assert`). Do not expect a linkdown→isolation FSM on D2D. `[HIGH/OBSERVED]`

D2D verbatim typos: name-field typos mirrored on both ctrls (`core_reset_dassertion`,
`breap_queue_error`, `surpise_down_error`, `corrected_interna_eerror`) — the `trigger`
*path* is correctly spelled in every case, so the path is the more reliable identifier;
plus the same `# All available FIS_ERRRIG triggers` section-comment typo seen in PCIe. `[HIGH/OBSERVED]`

---

## 6. Routing into the INTC 4-group and the PEB_INTC apex

Every domain's leaf table fills the bit positions of **one errtrig pair** of
[4-group INTC](../csr/intc-4group.md) instances. Each INTC instance latches
`4 groups × 32 bits = 128` trigger inputs; an *errtrig generator* pairs two instances
(`TRIG_0` + `TRIG_1`) = **256-source capacity**. All four domains fit one pair: PCIe
228 ≤ 256, HBM 223 ≤ 256, TPB 216 ≤ 256, D2D 216 ≤ 256. The YAML order is the bit-fill
order (proven for D2D by the §5 late-tail NOTE; corroborated for TPB by the 256-bit
`intc_bypass` map). `[HIGH/OBSERVED leaf; CARRIED INTC]`

> **The exact group cut is not recoverable.** Which 32-bit group of which instance a
> sub-block lands in is **not** in any shipped artifact — the INTC schema carries no
> per-bit source names (see [INTC §8](../csr/intc-4group.md)). Only the *fill order*
> (= YAML order) and the *aggregate fit* (≤ 256) are observable. Treat the
> per-bit→(instance, group, bit) mapping as an explicit non-claim. `[HIGH aggregate fit; LOW exact cut]`

Each domain instantiates **both** INTC flavors — `intc_4grp_msix_unit` (host-reachable
MSI-X leaves) and `intc_4grp_no_msix_unit` (on-die aggregators whose severity wire-ORs
feed **upward** into the apex). The 4-group leaves then roll up into the
**128-entry PEB_INTC apex** (`peb_intc_triggers.yaml`), where the per-domain
aggregation shape — re-read verbatim — is itself a per-domain signature `[HIGH/OBSERVED]`:

| Domain | apex entries | shape | `critical:1` fast-path? |
|--------|------|-------|:----:|
| **PCIe** | `pcie_m0_nmi` (PCIe_M), `pcie_a0_nmi` (PCIe_A), `pcie_se{0,1}_b_combined_nmi` (PCIe_B[0-4]) | 4 summaries (M / A / 2× B-combined) | no |
| **HBM** | `hbm_{0,1}_nmi` (SEngine[0]/[1] HBM) **+** `hbm_inttrig_cattrip_r[0,1]` **+** `hbm_inttrig_temp_change_r[0,1]` | 2 per-SEngine summaries + **4 dedicated** | **YES (4 entries, `critical:1`)** |
| **TPB** | `se{0,1}_tpb_nmi[0,1]` (SEngine × TPB-tile) | 4 per-tile summaries | no |
| **D2D** | `d2d_combined_nmi` (D2D[0-7]) | **1 combined** for all 8 links | no |

> **The HBM critical fast-path is unique.** **HBM is the only domain whose apex
> carries dedicated `critical:1` fast-path triggers** — `hbm_inttrig_cattrip_r[0/1]`
> and `hbm_inttrig_temp_change_r[0/1]`, one pair per stack. These bypass the generic
> `hbm_{0,1}_nmi` summary so firmware can react to a catastrophic-temperature trip
> (the M=5 cause) without decoding the per-channel summary. **TPB and D2D carry NO
> `critical` key at the apex** — every TPB/D2D source (even uncorrectable ECC or a
> link-down) rolls up only into the generic per-tile / combined summary, and firmware
> must decode the leaf errtrig to find the cause. This is a real, exploitable
> structural difference: a CATTRIP gets a hardware fast-path; a D2D link-down does not.
> `[HIGH/OBSERVED]`

Apex copies of these summaries carry `nmi_mask=0`/`nmi_msix_mask=0` (the apex-gated
form), whereas the leaf HBM/PCIe entries carry `=1` — i.e. the mask polarity differs
between leaf and apex (HIGH that the values differ; the YAML does not define whether
`1`=enabled or `1`=masked, so the directional reading is `[MED]`).

**D2D CDC routing detail.** Because the D2D blocks do their own CDC internally (§1),
no D2D trigger declares a `source_clock` — contrast HBM (`dfi_hdr_clk_occ_out`) and
PCIe (`intc_core_clk`), which name the INTC-side CDC destination. D2D presents
pre-synchronised triggers. `[HIGH/OBSERVED]`

---

## 7. Cross-generation deltas (Maverick / Sunda)

Cross-gen counts re-derived with `yq 'length'` against the `arch-headers/{maverick,sunda}`
variants. `[HIGH/OBSERVED for the counts; INFERRED for the v5 interior]`

| Domain | Cayman (v3) | Maverick (v5) | Sunda |
|--------|------:|------:|------:|
| PCIe | 228 | 125 | 207 |
| HBM | 223 | 256 | 187 |
| **TPB** | **216** | **217** | **186** |
| **D2D** | **216** | **7** | (no file) |

**TPB cross-gen — stable compute core, varying FIS/xbar tail.** The 131-entry
`errtrig_in` TPB-internal core is **byte-identical** between Cayman and Maverick (same
names, same order). They differ only in the tail: Maverick **collapses** the 5
`fis_cntrl_intr_0..4` into a single `fis_cntrl_intr` bus entry and **adds 5 first-class
triggers** (`xbar_intr_trig_corerr`, `xbar_intr_trig_uncerr`, `fis_intr_trig_uncerr`,
`parity_addr_error_interrupt`, `parity_data_error_interrupt`) — net **+1 → 217**
(`yq`-verified: 131 + 50 + 30 + 1-collapsed + 5-new = 217). The **Sunda (186)** variant
keeps the same 131-internal core but renames `fp_state_*`→`fp_stat_*` and replaces the
entire 85-entry FIS family with a single flat `sprot_intr[0..54]` bus (55 entries):
216 − 85 + 55 = 186. So the TPB *compute* interrupt surface is stable; only the
fabric-shim tail varies. `[HIGH/OBSERVED]`

> **The D2D Maverick degeneration — a completely different model.** The Maverick D2D
> file is **not** a tail-diff; it is **7 entries** of an *abstract "D2D IP"* form that
> does not enumerate the controller/PHY signals at all. Its entries (`yq`-verified
> names): `fis_cntrl_intr`, `parity_addr_error_interrupt`, `parity_data_error_interrupt`,
> `erg_intr_trig_uncerr`, and **three pre-aggregated severity lines** —
> `d2d_intr_fatal`, `d2d_intr_error`, `d2d_intr_info`. Maverick presents the *entire*
> D2D block as a black-box exporting only fatal/error/info summary interrupts, whereas
> Cayman wires out all 148 individual `snps_ctrl`/`mrvl_mpcs`/`mrvl_xsr` sources plus
> the full FIS family. The Sunda complex has **no D2D file at all** (die-to-die is a
> package-level concern, absent from the Sunda HBM/TPB/PCIe complex). **A byte-level
> D2D source diff across gens is meaningless — the underlying IP differs by
> generation, not merely the trigger projection.** The 7-entry interior is
> header-OBSERVED; its delivery semantics are `[INFERRED]`. `[HIGH/OBSERVED count; INFERRED interior]`

---

## 8. The minimal reimplementation checklist

A Vision-Q7 control plane consuming these four tables must:

1. **Parse each domain against its own schema** — PCIe/HBM 9-key rich, TPB 9-key
   `msix_mask`+`tog2pul_only`, D2D 5-key minimal. Do not assume a shared key set;
   `msix_mask` and `tog2pul_only` exist *only* in TPB, `nmi_*` exists *only* in
   PCIe/HBM. `[HIGH/OBSERVED]`
2. **Program `int_posedge_grp` from `edge_triggered` per bit** — and remember the
   **HBM inversion**: HBM's 192 controller causes are LEVEL, not edge. `[HIGH/OBSERVED]`
3. **Configure CDC from `needs_cdc`/`source_clock`** — sample HBM's 196 CDC sources
   into `dfi_hdr_clk_occ_out`, PCIe's 95 into `intc_core_clk`, TPB's 126 into
   `clk_core_gated` (+ the lone `ham_intr` into `clk_mem_top`); for D2D do nothing —
   CDC is internal to the controller/MPCS. `[HIGH/OBSERVED]`
4. **Fill the INTC errtrig pair in YAML order** (≤ 256 per pair for all four domains);
   treat the per-group cut as unknown. `[HIGH aggregate; LOW exact cut]`
5. **Hook the HBM `critical:1` apex fast-paths** (`cattrip`/`temp_change` per stack)
   separately from the generic summaries; TPB/D2D have no such fast-path and require
   leaf decode on any apex assertion. `[HIGH/OBSERVED]`

---

### Cross-references

- [HBM / D2D / PCIe CSR blocks](../csr/hbm-d2d-pcie-blocks.md) — the controllers/PHYs these triggers report on (`ddr_csr_apb`, `dwc_hbmphy_top`, `snps_ctrl`, `mrvl_xsr_phy`, `pcie5_x8_DWC_pcie_ctl`).
- [4-group INTC](../csr/intc-4group.md) — the `4×32=128` INTC unit, `int_posedge_grp`, the errtrig pair (256 cap), the apex feed.
- [TPB top-level CSR](../csr/tpb.md) — `intc_bypass` (256-bit), `fake_error_ctrl`, `events_semaphores`.
- [NOTIFIC queue CSR](../csr/notific-queue.md) — the `tpb_notific_intr_0..8` bit-map.
- [PEB Apex / CC / TOP_SP sources](peb-cc-topsp-triggers.md) — the apex side of the rollup.
- [Schema atlas](schema-atlas.md) — the cross-domain trigger-YAML schema taxonomy.
- [Physical INTC instances](physical-intc-instances.md) — the instance/aperture placement.
- [SoC master map](../address/soc-master-map.md) — HBM stack / PCIe BAR / D2D subsystem placement.
