# CSR — HBM / D2D / PCIe Blocks

This page reconstructs the three off-die-memory / off-die-link controller planes of
the Cayman SoC from their shipped RTL-generated register-description schemas
(`csrs/{hbm,d2d,pcie,erg}/*.json`) and the matching interrupt-trigger YAMLs
(`intc/{hbm,d2d,pcie}_triggers.yaml`). The goal is reimplementation depth: the
controller architecture, the *vendor IP identity* recoverable from the register/bundle
taxonomy, and the **key** control / init / ECC-scrub / link-state / error-capture
registers — not a mechanical dump of all 41 thousand HBM-PHY fields.

Every register cited below was read byte-exact from the JSON with `jq`. Each schema is
plain text (`file …json → JSON text data`), so the `.data` VMA/file-offset delta that
governs the ncore2gp config DLLs is **not applicable here** — there are no binary
struct offsets to correct, only schema `AddressOffset` strings (all hex, `0x`-prefixed,
re-verified for this batch). Counts are grounded in the JSON itself, never a decompile
grep.

> **SCHEMA NOTE — the `RegFile` container.** Each unit JSON is
> `{ "RegFile": { UnitName, AddrWidth, DataWidth, SizeInBytes, GenFlavor, Memories,
> Parameters, RegistersBundleArrays[] } }`. A bundle is
> `{ Name, AddressOffset, ArraySize, BundleSizeInBytes, Registers[] }`; a register is
> `{ Name, AddressOffset, AccessType, BitFields[] }`. Absolute register address =
> `bundle.AddressOffset + register.AddressOffset` (+ `ArraySize` stride for arrayed
> bundles). `[HIGH · OBSERVED]`

> **PROVENANCE LEGEND.** `HIGH` = value read byte-exact from the JSON
> (offset/bits/access/reset/name). `MED` = the byte value is HIGH but the *semantic*
> reading is inferred. `LOW` = meaning genuinely uncertain. `OBSERVED` = read from a
> shipped file; `INFERRED` = reasoned from corroboration; `CARRIED` = taken from a
> sibling page / earlier report.

> **`GenFlavor` is the vendor/Amazon discriminator.** Every unit carries a `GenFlavor`
> key: `"EXTERNAL_IP"` on imported vendor IP (DDR controller, DWC HBM PHY, DWC PCIe
> controller, Marvell MPCS/XSR), `null` on the Amazon-authored config/glue wrappers and
> RAS blocks. This is a clean, machine-readable separation of bought-IP vs in-house
> integration and is leaned on throughout. `[HIGH · OBSERVED]`

---

## 0. The three planes at a glance

| plane | controller | PHY | vendor IP (recovered) | trigger leaf |
|-------|-----------|-----|----------------------|--------------|
| **HBM** | `ddr_csr_apb` (DDR ctrl) | `dwc_hbmphy_top` | Cadence/Denali-class DDR ctrl · Synopsys DWC HBM PHY | 223 `hbm_triggers` |
| **D2D** | `snps_ctrl` (DWC PCIe, repurposed) | `mrvl_xsr_phy` + `mrvl_mpcs_x16` | Synopsys DWC PCIe ctrl · Marvell XSR SerDes · Marvell MPCS-x16 (ULFEC) | 216 `d2d_triggers` |
| **PCIe** (host bridge) | `pcie5_x8_DWC_pcie_ctl` | `dwc_e32mp_phy_x4_ns` | Synopsys DWC PCIe5 x8 · Synopsys DWC e32mp (Enterprise-32G) PHY | 228 `pcie_triggers` |

Trigger counts are `rg -c '^- trigger'` on the Cayman `intc/*_triggers.yaml` (223 / 216
/ 228). `[HIGH · OBSERVED]`

> **The single strongest structural finding of this batch — two SerDes vendors.** The
> **host PCIe** bridge rides a **Synopsys** PHY (`dwc_e32mp_phy_x4_ns`); the **die-to-die
> link** rides a **Marvell XSR** PHY (`mrvl_xsr_phy`) under a **Synopsys DWC PCIe
> controller** (`snps_ctrl`). So D2D is a *hybrid* stack: Synopsys controller over a
> Marvell PHY. The host-vs-D2D split is provable purely from the PCIe-capability bundle
> set (§II-1). `[HIGH · OBSERVED]`

> **GENERATION WALL — this page is Cayman / NC-v3.** The two committed address-map
> siblings, [`pkl-hbm-subtree`](../address/pkl-hbm-subtree.md) (#906) and
> [`pkl-pcie-d2d-fabric`](../address/pkl-pcie-d2d-fabric.md) (#907), are carved from the
> **Maverick / NC-v5** `al_address_map_db.pkl`. They describe the *successor*
> architecture (§V below). Do **not** read their `32 HBM_CTRL_DP`, `HBM_XBAR_8X32`,
> `39 UCIE links`, or `128 GiB` aperture as Cayman facts — those are v5. The Cayman CSR
> schemas on this page and the Cayman flat band (4 × 64 GiB stacks; DWC-PCIe-derived
> D2D) are the v3 anchor that the siblings cross-reference back to. `[HIGH · OBSERVED]`

---

## I. HBM — the DDR controller / PHY / scrubber / repair / crossbar

Six `csrs/hbm/*.json` units, byte-exact metadata (`jq` from scratch):

| unit | `GenFlavor` | AddrW | `SizeInBytes` | bundles | regs | fields |
|------|------------|:-----:|:-------------:|:-------:|:----:|:------:|
| `ddr_csr_apb` | EXTERNAL_IP | 16 | `0x10000` | 27 | 637 | 1078 |
| `dwc_hbmphy_top` | EXTERNAL_IP | 25 | `0x2000000` | 273 | 5099 | 28820 |
| `hbm_scbr` | null | 12 | `0x1000` | 5 | 36 | 60 |
| `hbm_hpr` | null | 12 | `0x1000` | 1 | 10 | 10 |
| `hbm_xbar_cfg` | null | 12 | `0x1000` | 5 | 9 | 25 |
| `hbm_cfg` | null | 12 | `0x1000` | 3 | 6 | 15 |

All `DataWidth = 32`. The two EXTERNAL_IP units are the bought controller + PHY; the
four `null` units are Amazon RAS/config glue. `[HIGH · OBSERVED]`

### I-1. `ddr_csr_apb` — the HBM/DDR controller (Cadence/Denali-class)

A DDR memory-controller core, `GenFlavor=EXTERNAL_IP`, `0x10000` (64 KiB) APB aperture,
27 bundles / 637 regs / 1078 fields. The bundle bases **start at `0x4000`** — the
`0x0000–0x3fff` sub-region is the PHY/DFI direct-access window, not a CSR bundle.

**Pseudo-channel split.** Eight register groups appear as `_PS0`/`_PS1` pairs
(`MC_BASE4`, `ECC_STAT_ERR`, `PARITY_ERROR`, `AXI_IF`, `MEM_TEST`, `ADV_MEM_TEST`,
`MTA`, `ACT_MON`). One controller instance manages **2 pseudo-channels**; the trigger
list ORs them — the HBM trigger family names literally read
`OR_pseudo_ch0_ch1_*` (verified in the YAML). This is the in-schema confirmation of the
pseudo-channel reading. `[HIGH that 8 bundles are PS-paired · OBSERVED; MED that PS ==
pseudo-channel]`

Bundle map (base / ArraySize / BundleSizeInBytes / nregs), `jq`-verified:

| bundle | base | arr | bsz | nregs | | bundle | base | arr | bsz | nregs |
|--------|------|:---:|-----|:-----:|-|--------|------|:---:|-----|:-----:|
| `MC_BASE1` | `0x4000` | 1 | `0x1fc` | 24 | | `MC_BASE2` | `0x4400` | 1 | `0x2d0` | 34 |
| `MC_BASE3` | `0x4800` | 1 | `0x2f4` | 27 | | `MC_BASE4_PS0/1` | `0x4c00`/`0x4e00` | 1 | `0x3c` | 13 |
| `DFI` | `0x5000` | 1 | `0xb8` | 33 | | `MPFE`/`REORDER`/`RMW` | `0x5800`/`0x5c00`/`0x6000` | 1 | — | 7/7/2 |
| `ECC_CONFIG` | `0x6400` | 1 | `0x2c` | 9 | | `ECC_STAT_ERR_PS0/1` | `0x6800`/`0x6a00` | 1 | `0x2c` | 11 |
| `PARITY_ERROR_PS0/1` | `0x6c00`/`0x7000` | 1 | `0x30` | 12 | | `DATA_INT` | `0x7400` | 1 | `0x28` | 9 |
| `AXI_IF_PS0/1` | `0x7800`/`0x7c00` | 1 | `0x60` | 7 | | `MEM_TEST_PS0/1` | `0x8000`/`0x8400` | 1 | `0x114` | 69 |
| `ADV_MEM_TEST_PS0/1` | `0x8800`/`0x8a00` | 1 | `0x44` | 17 | | `MTA_PS0/1` | `0x8c00`/`0x9000` | 1 | `0xb8` | 46 |
| `MC_BASE5` | `0x9400` | 1 | `0x244` | 40 | | `ACT_MON_PS0/1` | `0xac00`/`0xcc00` | 1 | `0xc0` | 44 |
| `csr_custom` | `0xe800` | 1 | `0x1c` | 7 | | | | | | |

`[HIGH · OBSERVED]`

#### Key registers — the controller interrupt-gen family

The 192 `hbm_ctrl_interrupt_gen[0..15][0..11]` triggers are the curated projection of a
single 32-bit per-channel interrupt vector. The vector and its drive/mask siblings live
in `MC_BASE3` (abs = `0x4800 + off`):

| abs | register | bits | acc | rst | role |
|-----|----------|------|:---:|:---:|------|
| `0x4934` | `MC_BASE3.STAT_INTERRUPT_0` | `31:0` | RO | `0x0` | raw 32-bit interrupt vector; **write-1-to-clear** per bit |
| `0x4938` | `MC_BASE3.INIT_INTERRUPT_MASK_0` | `31:0` | RW | `0x0` | per-bit mask of `stat_interrupt` |
| `0x493c` | `MC_BASE3.INIT_INTERRUPT_GEN_0` | `31:0` | RW | `0x0` | write-1-to-**force** an interrupt bit (SW inject) |

The 12 exposed causes (M=0..10 specific bits, M=11 = OR of the rest) are a vendor
projection of `STAT_INTERRUPT_0[31:0]`; the exact bit↔cause map is in the controller
User Guide, not the JSON. The Cayman `hbm_ctrl_triggers.yaml` does, however, name the
first bits explicitly: `stat_interrupt[0]` = AXI out-of-range, `[1]`/`[2]` = DQ
write/read parity, `[3]` = 1-bit ECC, `[4]` = 1-bit ECC above
`cfg_ecc_1bit_int_thresh`. `[HIGH cause↔register · OBSERVED; MED exact bit map]`

#### Key registers — ECC / scrub / parity / thermal (the RAS surface)

`ECC_CONFIG` (base `0x6400`) is the controller's built-in ECC + background ECS-scrub
control — full byte-exact register list:

| off | register | acc | role |
|-----|----------|:---:|------|
| `0x00` | `CFG_ECC_CORRECTION_EN` | RW | enable SBE correction |
| `0x04` | `INIT_ECC_SCRUB_EN` | RW | enable background ECS scrub |
| `0x08` | `INIT_ECC_SCRUB_INIT_EN` | RW | scrub-init (memory zero/clean) enable |
| `0x0c` | `CFG_ECC_SCRUB_INIT_RMW` | RW | read-modify-write during init |
| `0x14`/`0x18` | `CFG_ECC_SCRUB_{MIN,MAX}_ADDR_0` | RW | scrub address window |
| `0x1c` | `INIT_ECC_SCRUB_ERROR_CLR` | RW | clear scrub error counters |
| `0x20` | `CFG_ECC_BYPASS` | RW | ECC bypass |
| `0x24` | `CFG_ECC_1BIT_INT_THRESH` | RW | **SBE interrupt threshold** (below) |

`CFG_ECC_1BIT_INT_THRESH` @ `0x6424` has two fields: `cfg_ecc_1bit_int_thresh[7:0]` RW
(rst `0x0`) + `reserved_31_8[31:8]` RO. An 8-bit count of correctable errors that must
be *exceeded* before `stat_int_ecc_1bit_thresh` (and the interrupt pin) assert; `0x00`
= interrupt on every correctable error, `0xff` = disable. This is the exact register the
M=3 / `one_bit_ecc_error_on_read_above_threshold` trigger names. `[HIGH · OBSERVED]`

The scrub period lives separately: `MC_BASE5.CFG_ECC_SCRUB_PERIOD` @ `0x94e0` RW.

ECC status (per pseudo-channel, `ECC_STAT_ERR_PS0` base `0x6800`):

| abs | register | acc | feeds cause |
|-----|----------|:---:|-------------|
| `0x6800`/`0x6804` | `STAT_ECC_SCRUB_ERROR_{1BIT,2BIT}_CNT` | RO | scrub error counts |
| `0x6808` | `STAT_ECC_SCRUB_INIT_DONE` | RO | scrub-init complete |
| `0x680c`/`0x6810` | `INIT_WRITE_DATA_{1B,2B}_ECC_ERROR_GEN` | RW | error injection |
| `0x6814` | `STAT_INT_ECC_1BIT_THRESH` | RO | M=2 SBE (the bit the threshold sets) |
| `0x6818` | `STAT_ECC_1BIT_ERROR_ADDR` | RO | SBE capture address (26-bit) |
| `0x681c`/`0x6820` | `STAT_ECC_1BIT_POS` / `_RMW` | RO | SBE bit-position / RMW flag |
| `0x6824` | `STAT_ECC_2BIT_ERROR_ADDR` | RO | M=4 DBE capture address |
| `0x6828` | `STAT_ECC_2BIT_RMW` | RO | DBE RMW flag |

DQ/CA parity enables live in `MC_BASE1` (`CFG_RD_DQ_PARITY_EN` @ `0x40a0`,
`CFG_WR_DQ_PARITY_EN` @ `0x40a4`, `CFG_CA_PARITY_EN` @ `0x40a8`); the status side is in
`PARITY_ERROR_PS0` (base `0x6c00`): `STAT_{DI_WRITE,WRITE,READ}_DATA_PARITY_ERROR` +
`STAT_CA_PARITY_ERROR`. These feed M=0 (write-parity) / M=1 (read-parity). `[HIGH ·
OBSERVED]`

**Thermal (critical fast-path).** Two DFI status bits get dedicated `critical=1` apex
handling:

| abs | register | bits | acc | role |
|-----|----------|------|:---:|------|
| `0x504c` | `DFI.STAT_DFI_CATTRIP` | `stat_dfi_cattrip[0]`, `reserved_31_1[31:1]` | RO | M=5 catastrophic temperature trip (sticky) |
| `0x5048` | `DFI.STAT_DFI_TCR_TEMP` | `stat_dfi_tcr_temp[2:0]` | RO | M=6 temperature-change status (3-bit code) |

> **QUIRK — stale "HBM2" databook text.** Both `STAT_DFI_CATTRIP` and
> `STAT_DFI_TCR_TEMP` descriptions say "see **HBM2** JEDEC specification" even though the
> M=9/M=10 on-die-ECC triggers describe an **HBM3** device. The CATTRIP/TCR_TEMP DFI pins
> are JEDEC-common across HBM2/HBM3; the "HBM2" string is a stale databook reference, not
> a contradiction of the HBM3 part. `[MED · OBSERVED]`

**DFI init / training** (base `0x5000`): `STAT_DFI_INIT_COMPLETE` @ `0x5034` (M=8
`dfi_init_complete`), `STAT_DFI_TRAINING_COMPLETE` @ `0x5038` (M=7
`dfi_training_complete`), `STAT_DFI_TRAINING_ERROR` @ `0x5024`, plus
`PHY_CA_TRAINING_{START,EN,COMPLETE}` @ `0x508c`/`0x5090`/`0x5094` — the host's handle on
PHY command-address training. The Amazon-added `csr_custom` bundle (`0xe800`, the only
`null`-flavored bundle in this vendor file) bridges the host to PHY reset + the DFI
control-message sideband (`PHY_RESET_CONTROL`, `DFI_FREQUENCY`,
`DFI_CTRLMSG_REQ/CTRLMSG/ACK`, `PHY_PHYMSTR_BYPASS`) used to drive `dwc_hbmphy_top`
training. `[HIGH names · OBSERVED; MED role]`

> **NOTE — on-die ECC has no config register here.** M=9/M=10 (HBM3 one-bit / two-bit
> on-die ECC) are *device-side*; the controller surfaces them only as bits in
> `STAT_INTERRUPT_0`. There is no on-die-ECC config register in this JSON — confirmed by
> absence across all 637 regs. `[HIGH absence · OBSERVED]`

### I-2. `dwc_hbmphy_top` — Synopsys DWC HBM PHY

The Synopsys DesignWare HBM PHY, `GenFlavor=EXTERNAL_IP`, AddrWidth 25, a **32 MiB**
(`0x2000000`) aperture, 273 bundles / 5099 regs / 28820 fields — overwhelmingly RW
config (the PHY is *trained*, not interrupt-driven). The bundle-name families are the
canonical DWC HBM PHY taxonomy (`jq | rg | uniq -c`-verified):

| family | count | role |
|--------|:-----:|------|
| `DWORD_Pn` | 128 | per-instance per-lane **data** word slices |
| `AWORD_Pn` | 64 | **address/command** word slices |
| `DWORD_PALL` / `AWORD_PALL` | 32 / 16 | broadcast-write-all aliases |
| `MASTER_Pn` (+`_PALL`) | 4 (+1) | per-PHY PLL / calibration / global |
| `INITENG_Pn` (+`_PALL`) | 4 (+1) | **training sequencer** (INITialization ENGine) |
| `ACSM_Pn` (+`_PALL`, +`ACSMIM`) | 4 (+1, +1) | Address-Command Sequencer Machine |
| `BCAST` | 16 | broadcast apertures |
| `PPGC` | 1 | PHY Pattern Generator/Checker (BIST) |

`Pn = P0..P3` = the 4 PHY instances (one per HBM channel-quad); `PALL` = the broadcast
write-all alias. `[HIGH · OBSERVED]`

> **CORRECTION — the PHY file exposes NO trigger CSR.** All 273 bundles are config;
> there is no interrupt/status register. The "PHY-side" causes (DQ-parity, init/training
> complete) are **DFI sideband signals**, not PHY registers — they surface through the
> `ddr_csr_apb.DFI` `STAT_DFI_*` registers (§I-1), which is where the triggers latch. The
> PHY-side names `AwDfiInitComplete` / `DqParity` are PHY *port/signal* names, not
> register names in this schema. `[HIGH for absence · OBSERVED; MED signal-vs-register]`

### I-3. `hbm_scbr` — the BIST / March scrubber

An **Amazon-authored** programmable read/write-pattern scrubber (`GenFlavor=null`,
`0x1000`, 5 bundles / 36 regs / 60 fields) — **distinct** from the controller's ECS
scrub in `ddr_csr_apb.ECC_CONFIG`. This is a March/BIST engine: LFSR random patterns,
expected-vs-actual data compare. SOURCE of the 3 `hbm_sbr_int_trigger[0..2]`.

Bundles: `sbr` @ `0x0` (22 regs), `sbr_wr_pattern` @ `0x100` (Arr8), `sbr_rd_actual_data`
@ `0x180` (Arr4), `sbr_rd_expected_data` @ `0x1c0` (Arr4), `spare` @ `0x200` (Arr2). The
`sbr` bundle is per-pseudo-channel (`*_ch_0` / `*_ch_1`).

The control+status word is `sbr.cfg_ctl_ch_0` (and `_ch_1`), byte-exact bitfields — the
three trigger fields are bits **inside** this register:

| bits | field | acc | note |
|------|-------|:---:|------|
| `0` | `enable_scrbr` | RW | enable the scrubber |
| `2:1` | `op_mode` | RW | 0=read, 1=write, 2=write-then-read |
| `3` | `stop_on_error` | RW | halt after first mismatch |
| `4` | `disable_read_data_check` | RW | skip expected-vs-actual compare |
| `5` | `enable_rand_wr_pattern` | RW | 64-bit LFSR random write patterns |
| `6` | `sbr_is_busy` | RO | **TRIGGER** — operation in progress |
| `7` | `sbr_is_done` | RO | **TRIGGER** — set by HW on completion |
| `15:8` | `sbr_rd_err_vec` | RO | **TRIGGER** — 8 bits, one per pattern: read mismatch |
| `23:16` | `sbr_rand_d0..d3_transform_opcode` | RW | LFSR transform (swap @16/32B boundary) |

Plus `done_clr_ch_0` @ `+0x28` (write-1-to-clear `sbr_is_done`), `err_cnt_ch_0[31:0]`
@ `+0x40` RO, and `mismatch_addr_{low,high}_ch_0` @ `+0x30`/`+0x38` (first-mismatch
address). `[HIGH field existence · OBSERVED; MED which trigger = which field]`

### I-4. `hbm_hpr` — hardware page retirement (post-package repair)

Amazon-authored (`GenFlavor=null`, `0x1000`, 1 bundle / 10 regs / 10 fields). SOURCE of
the single `hbm_hpr_done_int_trigger`. Full register list (per pseudo-channel):

| off | register | bits | acc | role |
|-----|----------|------|:---:|------|
| `0x00` | `swap_start_ch_0` | `on[0]` | RW | trigger a page-swap action |
| `0x04` | `swap_rsvd_base_addr_ch_0` | `[31:0]` | RW | start addr of swap **write** (reserved) page |
| `0x08` | `swap_old_addr_ch_0` | `[31:0]` | RW | start addr of swap **read** (old) page |
| `0x0c` | `swap_page_full_ch_0` | `done[0]` | RO | **all reserved pages exhausted** |
| `0x10` | `swap_done_ch_0` | `done[0]` | RO | recent swap complete → feeds trigger |
| `0x14..0x24` | `swap_*_ch_1` | | | identical for channel 1 |

> **NOTE — page retirement, not row/lane hard-repair.** The descriptions describe
> *swapping a bad page to a reserved page* (a soft, page-granular repair), not
> JEDEC row/column hard-repair. `[HIGH · OBSERVED]` The trigger ORs `swap_done_ch_0 |
> swap_done_ch_1`; the `swap_page_full_*` "no spare pages left" status is also present
> (an unanticipated terminal condition). `[HIGH presence · OBSERVED; MED OR-aggregation]`

### I-5. `hbm_xbar_cfg` — the HBM crossbar (address swizzle + RAS roll-up)

Amazon-authored (`GenFlavor=null`, `0x1000`, 5 bundles / 9 regs / 25 fields). SOURCE of
`intr_trig_{corerr,uncerr}_xbar`. Bundles: `global_cfg` @ `0x0` (3 regs),
`ctrl_addr_transl_map` @ `0x100` (**Arr36**), `data_addr_transl_map` @ `0x200` (**Arr36**),
`xbar_top_32` @ `0x600` (2 regs), `spare` @ `0x700` (Arr2).

The two 36-entry `addr_transl_map` arrays are a **per-output-bit programmable address
swizzle**: each entry's `addr_bit_cfg` has `bit_select[5:0]` (input addr bit index),
`force0[6]`/`force1[7]`, `bit_select_1[13:8]`, `enable_xor[14]` — output =
`input[sel]`, or `XOR(input[sel], input[sel_1])`, or forced 0/1. This is the
channel-interleave / address-hash remapper. `global_cfg.addr_transl_cfg` @ `0x0` carries
`en_1G_per_ch[0]` (500 MB↔1 GB-per-channel translation). `xbar_top_32.ok_to_fail` @
`0x0` (enable + 8-bit data + credit-oversubscribe @ `0x4`) is the degraded-mode /
poison-on-fail RAS config. `[HIGH · OBSERVED]`

> **NOTE — no readable corr/uncorr status register.** `hbm_xbar_cfg` exposes the
> `ok_to_fail` RAS config + the address maps, but **no** error-status register —
> confirmed by absence. `intr_trig_{corerr,uncerr}_xbar` are therefore hardware-OR
> output wires (the top OR of the 16 per-channel `intr_trig_corerr_uncerr_or`), not
> readable status. `[HIGH absence · OBSERVED; MED OR-of-16]`

### I-6. `hbm_cfg` — the 16-channel keystone

Amazon-authored (`GenFlavor=null`, `0x1000`, 3 bundles / 6 regs / 15 fields). `global_cfg`
@ `0x0` (3 regs), **`hbm_ctrl_debug` @ `0x300` (ArraySize=16, 1 reg)**, `spare` @ `0x200`
(Arr2). The `hbm_ctrl_debug` array of **16** is the strongest in-schema evidence that
the single-channel `ddr_csr_apb` controller is replicated ×16 — exactly the
`hbm_ctrl_interrupt_gen[0..15]` trigger dimension. `[HIGH · OBSERVED]`

> **HBM RAS is rich** — controller SBE+threshold (`CFG_ECC_1BIT_INT_THRESH`) + DBE +
> DQ/CA parity + **dual** scrubber (ECS in `ECC_CONFIG` *and* the BIST `hbm_scbr`) + HW
> page-retirement (`hbm_hpr`) + xbar OR roll-up + HBM3 on-die ECC (device) +
> dedicated `critical=1` thermal fast-paths (`STAT_DFI_CATTRIP` / `STAT_DFI_TCR_TEMP`).
> `[HIGH · OBSERVED]`

---

## II. D2D — the die-to-die link (DWC PCIe ctrl + Marvell XSR PHY)

Eight `csrs/d2d/*.json` units, byte-exact:

| unit | `GenFlavor` | `SizeInBytes` | bundles | regs |
|------|------------|:-------------:|:-------:|:----:|
| `snps_ctrl` | EXTERNAL_IP | `0x4000` | 15 | 327 |
| `mrvl_mpcs_x16` | EXTERNAL_IP | `0x1000` | 1 | 31 |
| `mrvl_xsr_phy` | EXTERNAL_IP | `0x10000` | 1 | 1965 |
| `mrvl_xsr_pram` | null | `0x10000` | 0 | 0 (raw PRAM window) |
| `d2d_ctrl_axi_cfg` | null | `0x1000` | 3 | 53 |
| `d2d_ctrl_core_cfg` | null | `0x1000` | 3 | 14 |
| `d2d_mpcs_cfg` | null | `0x1000` | 2 | 4 |
| `d2d_xsr_cfg` | null | `0x1000` | 6 | 34 |

`erg_ecc_model` (the shared SRAM-ECC RAS block, §II-5) sits under `csrs/erg/`. `[HIGH ·
OBSERVED]`

### II-1. `snps_ctrl` — Synopsys DWC PCIe controller, repurposed die-to-die

The DWC PCIe controller, `GenFlavor=EXTERNAL_IP`, `0x4000` (16 KiB) DBI aperture, 15
bundles / 327 regs / 1456 fields. The bundle set **is** the PCIe config-space capability
taxonomy — definitive IP identification. Bundle bases (`jq`-verified): `PF0_TYPE0_HDR`
@ `0x0`, `PF0_PM_CAP` @ `0x40`, `PF0_PCIE_CAP` @ `0x70`, `PF0_AER_CAP` @ `0x100`,
`PF0_VC_CAP` @ `0x148`, `PF0_TPH_CAP` @ `0x170`, `PF0_RAS_DES_CAP` @ `0x1fc` (39 regs),
`PF0_VSECRAS_CAP` @ `0x2fc` (RASDP, 14 regs), `PF0_PTM_CAP`/`PTM_REQ_CAP` @ `0x334`/`0x340`,
`PF0_PORT_LOGIC` @ `0x700` (54 regs), `PF0_ATU_CAP` @ `0x3000` (**128 regs**, iATU),
plus DBI2 shadows.

> **The cap-set delta is the proof it is a D2D controller, not a host port.** A `jq comm`
> against the host `pcie5` (§III) shows:
> - **Only in D2D `snps_ctrl`:** `PF0_ATU_CAP`, `PF0_VC_CAP`, `PF0_TPH_CAP` — the iATU
>   address-translation that routes traffic to the remote die, plus VC/TPH.
> - **Only in host `pcie5`:** `PF0_MSI_CAP`, `PF0_MSIX_CAP`, `PF0_SPCIE_CAP`,
>   `PF0_PL16G_CAP`, `PF0_PL32G_CAP`, `PF0_MARGIN_CAP`, `PF0_SN_CAP`, `PF0_VPD_CAP`,
>   `PF0_DLINK_CAP` — host-enumeration / MSI-X / speed-margining caps.
> - **Common:** AER, PCIE_CAP, PM, PORT_LOGIC, PTM, RAS_DES, TYPE0_HDR, VSECRAS.
>
> The D2D variant **drops** host-enumeration/MSI and **adds** iATU — the same DWC PCIe
> IP family configured as an internal die-to-die link. `[HIGH structural · OBSERVED]`

**Link state (LTSSM).** `PF0_RAS_DES_CAP.SD_STATUS_L1LTSSM_REG` @ `0xb4` (abs `0x2b0`),
RO: `LTSSM_VARIABLE[31:16]` ("internal LTSSM variables, sticky"), `LANE_REVERSAL[15]`,
`PIPE_POWER_DOWN[10:8]` (rst `0x2`). The two `ctrlN_ltssm_cmp_match` triggers compare the
live LTSSM state vs a programmable compare value (programmed via `SD_CONTROL1/2_REG`).
`PF0_PCIE_CAP.LINK_CONTROL_LINK_STATUS_REG` @ `0x10` surfaces the standard
link-up/training bits: `PCIE_CAP_DLL_ACTIVE[29]`, `PCIE_CAP_LINK_TRAINING[27]`,
`PCIE_CAP_NEGO_LINK_WIDTH[25:20]`, `PCIE_CAP_LINK_SPEED[19:16]`,
`PCIE_CAP_RETRAIN_LINK[5]` — reflected by the `smlh_link_{up,down}` / `rdlh_link_{up,down}`
triggers (PHY-link / data-link state machines). `[HIGH · OBSERVED]`

**RAS — the cleanest 1:1 reconciliation in this batch.** `PF0_AER_CAP.UNCORR_ERR_STATUS_OFF`
@ `0x4` (abs `0x104`, RW) maps bit-for-bit to the `ctrl.core` triggers:

| AER UNCORR bit | field | → trigger |
|:--------------:|-------|-----------|
| `22` | `INTERNAL_ERR_STATUS` | `uncorrectable_internal_error` |
| `19` | `ECRC_ERR_STATUS` | `ecrc_err` |
| `18` | `MALF_TLP_ERR_STATUS` | `mlf_tlp_err` |
| `17` | `REC_OVERFLOW_ERR_STATUS` | `receiver_overflow_error` |
| `14` | `CMPLT_TIMEOUT_ERR_STATUS` | `cmpl_timeout` |
| `13` | `FC_PROTOCOL_ERR_STATUS` | `fc_protocol_err` |
| `5` | `SURPRISE_DOWN_ERR_STATUS` | `surprise_down_error` |
| `4` | `DL_PROTOCOL_ERR_STATUS` | `dl_protocol_err` |

`PF0_AER_CAP.CORR_ERR_STATUS_OFF` @ `0x10` (abs `0x110`, RW) maps the corrected set
(`RPL_TIMER_TIMEOUT` → `replay_timer_timeout_error`, `REPLAY_NO_ROLEOVER` →
`replay_number_rollover_error`, `BAD_DLLP`/`BAD_TLP`/`RX_ERR` → the matching triggers).
Companions: `UNCORR/CORR_ERR_{MASK,SEV}_OFF` (per-bit mask/severity) + `HDR_LOG_0..3_OFF`
@ `0x1c..0x28` RO (the offending TLP header capture). `[HIGH · OBSERVED]`

**RASDP (datapath ECC/parity over the controller's internal RAMs).** `PF0_VSECRAS_CAP`
@ `0x2fc` is the DWC "RAS Data Path" vendor cap — SOURCE of `{mstr,slv}_rasdp_error_mode`.
Full register list includes `RASDP_ERROR_PROT_CTRL_OFF` @ `0x8`, `RASDP_{CORR,UNCORR}_COUNT_REPORT_OFF`
@ `0x10`/`0x18` RO, `RASDP_ERROR_INJ_CTRL_OFF` @ `0x1c`, `RASDP_{CORR,UNCORR}_ERROR_LOCATION_OFF`
@ `0x20`/`0x24` RO, and `RASDP_ERROR_MODE_{EN,CLEAR}_OFF` @ `0x28`/`0x2c` RW — the latter
two driving the `rasdp_error_mode` entry/exit the triggers report. `[HIGH · OBSERVED]`

`PF0_PORT_LOGIC` (@ `0x700`, 54 regs) carries `PORT_LINK_CTRL_OFF`, `PL_DEBUG0/1_OFF`
(link-state debug), `AMBA_LINK_TIMEOUT_OFF` (the AXI/AMBA timeout behind the queue/counter
errors). `PF0_ATU_CAP` (@ `0x3000`) holds the outbound iATU regions
(`IATU_REGION_CTRL_1/2_OFF_OUTBOUND_n` + `IATU_LWR/UPPER_BASE_ADDR_OFF_OUTBOUND_n`) that
remap local AXI addresses to remote-die PCIe addresses — the die-to-die routing plane,
the controller-side counterpart of the RDMA cross-die path
([`rdma-cross-die`](../../dma/rdma-cross-die.md)). `PF0_PTM_CAP`/`PTM_REQ_CAP` provide
Precision Time Measurement for inter-die clock alignment. `[HIGH names · OBSERVED]`

> **ACCESS-TYPE NOTE.** `snps_ctrl` is the only block in this batch to use the full
> `{RW, RO, Reserved, WO}` vocabulary at bitfield level (the DBI write-once /
> event-counter-clear semantics). The HBM blocks use only `{RO, RW}`. `[HIGH ·
> OBSERVED]`

### II-2. `mrvl_mpcs_x16` — Marvell MPCS x16 (PIPE coding + ULFEC)

The Marvell Multi-Protocol Coding Sublayer x16, `GenFlavor=EXTERNAL_IP`, `0x1000`, 1
bundle / 31 regs / 56 fields. The single bundle is `ulfec120_addr_block` (ArraySize=4):
**ULFEC** = Ultra-Low-latency FEC, the D2D forward-error-correction coding layer.
Registers are all `ULFEC120_*`: `CONTROL`, `STATUS`, `RX/TXCONTROL`, `RECEIVED_CW_{L,H}`,
`GOOD_CW_{L,H}`, `CORRECTED_CW`, `UNCORRECTED_CW`, `SYMBOL_ERR_*`, `PAM4_ERR_*` (FEC
codeword + symbol statistics). `[HIGH · OBSERVED]`

> **The MPCS CSR file and its trigger set are complementary surfaces.** The 12 mpcs
> triggers per instance (`pipe_phystatus_{rising,falling}`, `pipe_txdetectrx_*`,
> `pipe_powerdown_p0/p0s/p1/p2`, spares) are **PIPE-interface status edges**, not ULFEC
> registers. D2D FEC errors are corrected silently (no FEC-error trigger exists); only
> PIPE state transitions interrupt. The ULFEC counters are read for statistics. The
> Amazon `d2d_mpcs_cfg` (2 bundles / 4 regs) holds the MPCS clock/reset glue. `[HIGH ·
> OBSERVED]`

### II-3. `mrvl_xsr_phy` — Marvell XSR die-to-die SerDes PHY

The Marvell XSR SerDes PHY, `GenFlavor=EXTERNAL_IP`, AddrWidth 16, `0x10000` (64 KiB), a
single `addrblock` bundle of **1965 regs / 8360 fields**. The PHY is a multi-lane SerDes
with per-lane eye-metric registers (`RX{A..P}_PDF_Eye_Metrics_Register`,
`RX{A..P}_Waveform_Eye_Metrics_Register_1/2`) plus an **embedded MCU complex**
(`mcu_control_0/1`, `MCU_Debug0/1`, `mcu_info_0..3`, `mcu_addr_reg`) and a common-block
interrupt set (`cmn_mcu_int_reg0..12`) + internal memory ECC (`mem_cmn_ecc_err_address0`).
The MCU runs microcode loaded from `mrvl_xsr_pram`. `[HIGH · OBSERVED]`

The 10 `xsr` triggers reconcile by concept: `hsseyequality` → eye-metrics, `hssplllocka`
→ HS PLL lock, `hssprtreadya` → port ready, `phy_int` → `cmn_mcu_int_reg*` aggregation,
`mem_ecc_err` → `mem_cmn_ecc_err_address0` (internal-memory ECC), and the **critical**
`mcu_wdt` → the MCU watchdog (a hung PHY MCU forces a full PHY reset/reinit). The two XSR
RAS triggers are the memory ECC and the MCU watchdog. `[HIGH concept · OBSERVED; MED
signal↔register binding]`

### II-4. `d2d_ctrl_axi_cfg` / `d2d_ctrl_core_cfg` — the AXI / core domain wrappers

Amazon wrappers (`GenFlavor=null`) around `snps_ctrl` implementing the `.axi.*` / `.core.*`
trigger domain split. `d2d_ctrl_axi_cfg` (`0x1000`, 3 bundles / 53 regs): `dw_ctrl`
@ `0x0`, **`wrapper` @ `0x600` (49 regs)**, `spare` @ `0x800`. The `wrapper` bundle is
the source of the AXI-domain queue/counter triggers: `bresp_queue` + `bresp_queue_error`,
`msg_gen_queue` + `msg_gen_queue_error`, `write_ro`, and the **8 outstanding counters**
`axi_{mstr,slv}_{ctrl,fab}_outstanding_{wr,rd}_counter` (→ the
`outstanding_{wr,rd}_counter_{over,under}flow` triggers). `d2d_ctrl_core_cfg` (3 bundles /
14 regs) is the core-domain glue; the core-domain RAS triggers bind to `snps_ctrl` AER
(§II-1). `[HIGH queue/counter · OBSERVED; MED that the parity triggers — `axi_if_parity`,
`ram_addr_parity` — are signals, not registers]`

### II-5. `mrvl_xsr_pram` / `d2d_xsr_cfg` / `erg_ecc_model`

`mrvl_xsr_pram`: `0x10000` aperture, **0 bundles / 0 regs** — a raw PRAM (microcode store)
window for the XSR MCU, which is why it carries no triggers. `d2d_xsr_cfg` (6 bundles /
34 regs) is the Amazon PHY bring-up glue: `pram_ctrl` + `pram_checksum`, `hs_pll`,
clock-gen, `reset_extend_sw_reset`, and analog config (`charge_pump`, `bandgap`,
`xtal_osc`). `[HIGH names · OBSERVED]`

`erg_ecc_model` (`csrs/erg/`, `GenFlavor=null`, `0x40`, 15 regs) is the shared SRAM-ECC
RAS block (referenced by D2D, PCIe and SDMA). SOURCE of `erg_intr_trig_uncerr`. Full
register list:

| off | register | acc | role |
|-----|----------|:---:|------|
| `0x00`–`0x0c` | `init_cfg` / `init_status` / `mem_cfg` / `cfg` | RW | block config/init |
| `0x10` | `eg_sram_uncerr` | RW | per-SRAM uncorrectable-error **gen** enable `[31:0]` |
| `0x14` | `uncerr_sram_mask` | RW | per-SRAM mask |
| `0x18` | `uncerr_cnt` | RO | uncorrectable count, saturates at `0xFF` |
| `0x1c` | `uncerr_sram_status` | RO | per-SRAM error status `[31:0]` |
| `0x20` | `uncerr_stat_clear` | WO | clear uncerr status |
| `0x24`–`0x34` | `corerr_*` (gen/mask/cnt/status/clear) | RW/RO/WO | the **correctable** path |
| `0x38` | `spare_reg` | RW | spare |

> **NOTE — the correctable ERG path is counted, not exposed.** Only the **un**correctable
> line (`uncerr_cnt` / `uncerr_sram_status`) feeds a D2D trigger; the `corerr_*` path is
> counted internally with no D2D trigger leaf. `[HIGH · OBSERVED]`

> **D2D RAS is PCIe-controller-integrity-dominated** — AER protocol/TLP/DLLP errors +
> RASDP datapath ECC + axi/ram parity + the XSR PHY internal ECC + MCU watchdog + ERG
> uncorrectable. Unlike HBM, **D2D has no `critical=1` apex fast-path**: even
> `uncorrectable_internal_error` / `mcu_wdt` roll into the single `d2d_combined_nmi`
> summary. `[HIGH · OBSERVED]`

---

## III. PCIe — the host bridge

`csrs/pcie/*.json` is the **host** PCIe bridge (BAR0/BAR4 host interface), distinct from
the D2D `snps_ctrl` (which repurposes the same DWC PCIe IP die-to-die, §II).

| unit | `GenFlavor` | AddrW | `SizeInBytes` | bundles | regs |
|------|------------|:-----:|:-------------:|:-------:|:----:|
| `pcie5_x8_DWC_pcie_ctl` | null | 13 | `0x2000` | 19 | 260 |
| `pcie_appaxi_model` | null | 12 | `0x1000` | 1 | 224 |
| `pcie_appcore_model` | null | 12 | `0x1000` | 1 | 63 |
| `pcie_phy_model` | null | — | `0x1000` | 1 | 194 |
| `pcie_user` | null | 12 | `0x1000` | 2 | 4 |
| `dwc_e32mp_phy_x4_ns` | null | 17 | `0x20000` | 1 | (PHY) |

**`pcie5_x8_DWC_pcie_ctl`** is the host-facing Synopsys DWC PCIe5 x8 controller, `0x2000`,
19 bundles / 260 regs / 1305 fields — the **richer** cap-set: it ADDS `PF0_MSI_CAP`,
`PF0_MSIX_CAP`, `PF0_SPCIE_CAP`, `PF0_PL16G_CAP`, `PF0_PL32G_CAP`, `PF0_MARGIN_CAP`,
`PF0_SN_CAP`, `PF0_VPD_CAP`, `PF0_DLINK_CAP` over the D2D variant, while KEEPING the same
`PF0_AER_CAP` / `PF0_PCIE_CAP` / `PF0_RAS_DES_CAP` / `PF0_VSECRAS` (RASDP) / `PF0_PTM` /
`PF0_PORT_LOGIC` and the **same** LTSSM register (`SD_STATUS_L1LTSSM_REG` @ `0xb4` in
RAS_DES). Same DWC PCIe IP family, configured as a host endpoint/RC. `[HIGH · OBSERVED]`

`pcie_appaxi_model` (224 regs) / `pcie_appcore_model` (63 regs) are the Amazon
application-AXI / application-core wrappers (BAR decode, doorbell/MSI generation, host-DMA
glue); `pcie_user` (4 regs) is user scratch. The host PHY is a Synopsys **DWC e32mp**
(Enterprise 32G multi-protocol) x4 (`dwc_e32mp_phy_x4_ns`, `0x20000`, AddrWidth 17) —
**distinct** from the Marvell XSR die-to-die PHY. `[HIGH · OBSERVED]`

For the PCIe-trigger detail itself (228 leaves), see
[`pcie-hbm-tpb-d2d-triggers`](../interrupt/pcie-hbm-tpb-d2d-triggers.md).

---

## IV. Instance placement and the address map

The HBM CSR blocks (`ddr_csr_apb` / PHY / scbr / hpr / xbar / cfg) live in the BAR0
control plane, **not** in the DRAM band. The Cayman DRAM band is **4 × 64 GiB stacks**
(2 stacks/die × 2 dies/package), sparse in SoC space; BAR4 (host view) compacts them
contiguous. The 16-channel CSR dimension (`hbm_cfg.hbm_ctrl_debug` ArraySize=16) is the
per-channel replication of the single-channel `ddr_csr_apb` controller; the PS0/PS1
bundle split inside `ddr_csr_apb` is the 2 pseudo-channels per channel. `[HIGH instance ·
OBSERVED; MED 8ch/stack arithmetic · INFERRED]`

D2D placement: `IO_D2D_SUBSYS_0..7` = 8 D2D subsystems per die, each = 2 `snps_ctrl` +
2 `mrvl_mpcs_x16` + 1 `mrvl_xsr_phy` + FIS shims + ERG; the 216-trigger leaf describes
**one** subsystem. ×2 dies = 16 D2D subsystems per package. The 8 subsystems are 8
die-to-die links in the 64-die `CAYMAN_ID[53:48]` mesh. `[HIGH instance · OBSERVED; MED
per-link↔neighbour direction · INFERRED]`

> **CORRECTION / DISAMBIGUATION vs the address-map siblings (#906, #907).** Those pages
> are **Maverick / NC-v5**, the *successor* SoC, and describe a different memory/link
> microarchitecture:
> - [`pkl-hbm-subtree`](../address/pkl-hbm-subtree.md) (#906) reports **2 `AMZN_HBM`
>   stack controllers per die** and **32 `HBM_CTRL_DP` datapath controllers per stack**,
>   plus an `HBM_XBAR_8X32` crossbar and a **128 GiB** engine-view aperture. The "2
>   stacks/die" is *consistent* with this page's "4 stacks (2/die × 2 dies)"; the 32-DP
>   fan-out and the 8×32 xbar are **v5 structures** (Cayman expresses the same channel
>   dimension as the `hbm_cfg.hbm_ctrl_debug` **ArraySize=16** CSR array, not 32 explicit
>   DP nodes). #906 itself flags the **128 GiB v5 aperture as `[MED · INFERRED]`** (header
>   value OBSERVED; v5 interior DRAM geometry inferred); this page does not assert it for
>   Cayman.
> - [`pkl-pcie-d2d-fabric`](../address/pkl-pcie-d2d-fabric.md) (#907) reports **39
>   distinct UCIE links** (UCIE-A EW18/NS8 + UCIE-S EW8/NS5) over a **native UCIE** TL/LL/
>   2nm-PHY/LTSM stack — Maverick **replaced** the Cayman "DWC-PCIe-as-D2D + Marvell XSR"
>   stack (this page) with native UCIE. #907 explicitly notes the PCIe/UCIE *controller*
>   register files are **not** in its DB and points back here for them.
>
> Net: this page's `snps_ctrl`/`mrvl_xsr` D2D stack and 16-channel HBM controller are
> **Cayman (v3)**; the siblings' `32 HBM_CTRL_DP` / `8×32 XBAR` / `39 UCIE links` /
> `128 GiB` are **Maverick (v5)**. Keep the generations separate. `[HIGH · OBSERVED for
> the gen attribution; MED for the v5 interior, which the siblings flag INFERRED]`

See [`unified-soc-memory-map`](../address/unified-soc-memory-map.md) for the consolidated
aperture layout.

---

## V. Cross-generation divergences (Cayman / Mariana / Sunda / Maverick)

The customop-lib `arch-headers/{sunda,mariana,mariana_plus,maverick}` carry the same CSR
schemas for other gens; the deltas (all `[HIGH · OBSERVED]` from the JSON):

- **`ddr_csr_apb`:** Cayman/Mariana use the **pseudo-channel-split** layout (PS0/PS1
  pairs, `0x10000` aperture; Cayman 27 bundles/637 regs, Mariana 27/676). **Sunda** uses
  a **flat single-channel** layout with a larger `0x40000` aperture (16 bundles / 496
  regs, no PS pairs). **Maverick** `ddr_csr_apb` (per #906) = `EXTERNAL_IP`, `0x10000`,
  **19 bundle arrays, no PS pairs** — the v5 controller dropped the pseudo-channel split.
- **HBM xbar:** Mariana(+) add `hbm_xbar_crc_hash` (3 regs), `hbm_xbar_ctrl` (26 regs),
  and `hbm_xbar_port_hbm` (14 regs) — a much richer crossbar than Cayman's single
  `hbm_xbar_cfg`. Sunda has only `hbm_xbar_cfg`.
- **Scrubber / HPR:** present in Cayman + Mariana(+); **absent in Sunda** (no `hbm_scbr`,
  no `hbm_hpr`). The BIST-scrubber + page-retirement RAS blocks are a Cayman/Mariana
  addition.
- **D2D:** Cayman **itemizes** the full DWC-PCIe + Marvell-XSR stack (8 units). Mariana(+)
  collapse it to a single black-box `d2d_wrap.json` (`0x2000`, 3 bundles / 10 regs).
  Sunda has **no** D2D at all. Maverick switched to **native UCIE** (§IV). D2D is the
  least stable domain across gens.
- **Host PCIe:** Cayman/Sunda = DWC PCIe5 ctl + e32mp/phy_model. Mariana(+) model the host
  PCIe differently (`pcie_addr_swizzle`, `pcie_oatb`, `pcie_scratchpad`,
  `pcie_sys_axi_model`, `pcie_hard_reset_domain` — no DWC-ctl JSON in that view).

> **GOTCHA — the decimal `SizeInBytes` recurs only in Mariana.** All Cayman-target files
> carry hex `0x`-prefixed `SizeInBytes`/`BundleSizeInBytes`. The Mariana
> `hbm_xbar_port_hbm.json` carries `SizeInBytes "1024"` (decimal = `0x400`). Parse the
> size-key per file. `[HIGH · OBSERVED]`

---

## VI. Trigger-leaf reconciliation summary

| HBM (223) | binding |
|-----------|---------|
| `hbm_ctrl_interrupt_gen[0..15][0..11]` (192) | `ddr_csr_apb.STAT_INTERRUPT_0[31:0]` projection (M=0..11) |
| `hbm_sbr_int_trigger[0..2]` (3) | `hbm_scbr.cfg_ctl` `{sbr_is_done, sbr_is_busy, sbr_rd_err_vec}` |
| `hbm_hpr_done_int_trigger` (1) | `hbm_hpr.swap_done_ch_{0,1}` (OR) |
| `intr_trig_corerr_uncerr_or[0..15]` (16) | per-channel hardware OR (no status reg) |
| `intr_trig_{uncerr,corerr}_xbar` (2) | `hbm_xbar_cfg.ok_to_fail` + OR outputs (no status reg) |
| `hbm_ela_interrupt[0..3]` (4) / `fis_cntrl_intr_hbm_top_stg[0..4]` (5) | CoreSight ELA / FIS shim |

| D2D (216) | binding |
|-----------|---------|
| `ctrlN.core.*` (RAS/protocol) | `snps_ctrl.PF0_AER_CAP.{UNCORR,CORR}_ERR_STATUS` (exact 1:1) + LTSSM + link status |
| `ctrlN.axi.*` (queue/counter) | `d2d_ctrl_axi_cfg.wrapper` queue/error/counter regs |
| `ctrlN.{mstr,slv}_rasdp_error_mode` | `snps_ctrl.PF0_VSECRAS_CAP` (RASDP) |
| `mpcsN.*` (24) | `mrvl_mpcs_x16` PIPE edges (CSRs = ULFEC FEC counters) |
| `xsr[0].*` (10) | `mrvl_xsr_phy` eye/PLL/port/MCU-int/mem-ECC/MCU-WDT |
| `erg_intr_trig_uncerr` (1) | `erg_ecc_model` uncerr path |
| `fis_*` (67) | FIS shim (separate CSR domain) |

The full HBM/PCIe/D2D trigger taxonomy is documented in
[`pcie-hbm-tpb-d2d-triggers`](../interrupt/pcie-hbm-tpb-d2d-triggers.md).

> **EXPLICIT NON-CLAIMS.** (1) The exact `STAT_INTERRUPT_0` bit↔M-cause map is in the
> vendor User Guide, not the JSON — only cause↔register-name is recoverable `[MED]`. (2)
> The 3 SBR triggers ↔ `{done,busy,rd_err}` ordering is plausible but not stated `[MED]`.
> (3) `intr_trig_corerr_uncerr_or` / xbar corr/uncorr triggers have **no** readable
> status register (hardware OR) `[HIGH absence]`. (4) mpcs/xsr triggers are PIPE/PHY
> *signals*, complementary to the FEC/eye CSRs in those files, not 1:1 `[HIGH/MED]`. (5)
> The 8ch/stack and per-link↔neighbour direction are inferred from counts `[MED ·
> INFERRED]`.
