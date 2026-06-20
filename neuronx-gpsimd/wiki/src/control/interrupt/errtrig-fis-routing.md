# errtrig / FIS Error Routing

How a hardware error — a FIS firewall/isolation/security violation, a RAS/ECC
latch, a notification-buffer fault — is **captured**, **aggregated**, and
**routed** into an interrupt trigger, from the source pin all the way to the
Q7 / "Pacific" management core. This page decodes the `errtrig` primitive and
the L0→L3 error-trigger cascade in register, address-map, and trigger-YAML
detail.

All facts below are derived from static analysis of the shipped, RTL-generated
**Cayman** arch-regs artifacts (`cayman_golden_tapeout_candidate_2`,
`reg_map`-generated 2023): the `intc_4grp` schema JSONs, `notific_1_queue.json`,
the `sprot`/`fis` schema JSONs, the flat address-map YAML, and the per-domain +
apex trigger YAMLs. Confidence is tagged per claim as
`[HIGH/MED/LOW · OBSERVED/INFERRED/CARRIED]`.

> **Scope boundary.** This page covers the *error path*: source → latch →
> severity classify → summary → apex → IRQ. The **Abort-severity output** — the
> scan-dump / clock-stop / SRAM-write-protect freeze — is decoded only as far as
> the hand-off register; the freeze mechanics live on the
> [Abort / Scan-Dump / Clock-Stop](./abort-scandump-clockstop.md) page and are
> referenced, not duplicated, here. The register-level CSR layout of the FIS
> container itself lives on
> [CSR — FIS Control + errtrig + spad](../csr/fis-errtrig-spad.md).

---

## 0. Executive summary — the routing in one paragraph `[HIGH · OBSERVED]`

The `errtrig` is the SoC's universal error/RAS interrupt-aggregation primitive.
It is **not a single CSR** — it is an address-map *generator* that instantiates,
at every fabric master's FIS (Fabric Interface Slice), an `intc_4grp`
**PAIR** (`TRIG_0` + `TRIG_1` = 256-source capacity) plus one `notific_1_queue`,
inside a `0x3000` `ERRTRIG` sub-region of the per-master `FIS_0` container.
Hardware error sources — the FIS protection violations (remapper access-control
DENY, qos NTS no-target, nsm AXI-integrity), the per-block RAS/ECC latches, and
the notification buffer/AXI faults — drive the errtrig's `int_cause_grp` register
(latched **W0C**). Each errtrig has two delivery flavors, tracking **privilege,
not domain**: the **USER** (host-reachable) errtrig uses the `msix` variant and
delivers an MSI-X message straight to the PCIe host; the **AMZN**
(privileged/on-die) errtrig uses the `no_msix` variant and emits **four
classified severity wire-ORs** (`Error`/`Abort`/`Fatal`/`Log` =
`OR(Cause & !per-severity-Mask)`) plus a `Mask_msi_x` summary — one **`nmi_out`**
wire. Those `nmi_out` wires aggregate upward into the 128-input `peb_intc`
**apex** (also a flavor-pair), which delivers via MSI-X into the IOFIC/io_fabric
cascade to the Q7 / "Pacific" core / GIC. The **Abort** severity output drives
scan-dump / clock-stop / SRAM-write-protect — the boundary into the
[abort/scan-dump](./abort-scandump-clockstop.md) domain. Everything down to the
apex MSI-X + the firmware cause table is **OBSERVED**; the apex-pending-bit → Q7
vector hop is firmware/HW-owned and **INFERRED**.

---

## 1. The `errtrig` block format — the per-master generator

### 1.1 A generator, not an `errtrig` CSR `[HIGH · OBSERVED]`

`fd`/`rg` over `csrs/` for `*errtrig*` returns **zero** register-file JSONs. The
errtrig is an address-map *generator* leaf (`apb/intc_rdm/errtrig_{user,amzn}.yaml`)
that emits three sub-blocks into a `0x3000` container:

```
trig    count:2  ->  csrs/intc/intc_4grp_{msix|no_msix}_unit.json   (the PAIR)
notific          ->  csrs/notific/notific_1_queue.json
```

**SPOT — USER errtrig under an SDMA `FIS_0` (`address_map_flat.yaml`)
`[HIGH · OBSERVED]`:**

| sub-block | rel. offset | size | backing schema |
| --- | --- | --- | --- |
| `..._FIS_0_USER_ERRTRIG` | `+0x0000` | `0x3000` | *(container)* |
| `..._USER_ERRTRIG_TRIG_0` | `+0x0000` | `0x1000` | `intc_4grp_msix_unit.json` |
| `..._USER_ERRTRIG_TRIG_1` | `+0x1000` | `0x1000` | `intc_4grp_msix_unit.json` |
| `..._USER_ERRTRIG_NOTIFIC` | `+0x2000` | `0x1000` | `notific_1_queue.json` |

**SPOT — AMZN errtrig under the privileged `FIS_0` `[HIGH · OBSERVED]`:**

| sub-block | abs. `FIS_0` offset | size | backing schema |
| --- | --- | --- | --- |
| `..._FIS_0_CTL` | `+0x0000` | `0x2000` | `fis_control.json` |
| `..._FIS_0_AMZN_ERRTRIG` | `+0x2000` | `0x3000` | *(container)* |
| `..._AMZN_ERRTRIG_TRIG_0` | `+0x2000` | `0x1000` | `intc_4grp_no_msix_unit.json` |
| `..._AMZN_ERRTRIG_TRIG_1` | `+0x3000` | `0x1000` | `intc_4grp_no_msix_unit.json` |

So the USER errtrig stands alone (`0x3000`), and the AMZN errtrig is the
**middle `0x3000`** of the `FIS_0` container, behind the `0x2000`-wide
`FIS_0_CTL`. The intra-container `TRIG_0`/`TRIG_1`/`NOTIFIC` stride is a uniform
`+0x1000`; the absolute `FIS_0` framing (`CTL` first at `+0x0000`) makes the AMZN
`TRIG_0` land at `+0x2000`.

> **NOTE — two offset framings, reconciled `[HIGH · OBSERVED]`.** The *relative*
> stride inside any `ERRTRIG` container is `+0` / `+0x1000` / `+0x2000`
> (`TRIG_0`/`TRIG_1`/`NOTIFIC`). The *absolute* `FIS_0` offsets differ for AMZN
> because the `0x2000`-wide `CTL` precedes the errtrig. Both framings describe the
> same byte-exact flat map; do not read them as a contradiction.

> **CORRECTION — the Cayman PAIR count is 962, not 642
> `[HIGH · OBSERVED]`.** An earlier sibling decode (the pkl-subtree
> cross-check, report #908) listed **"642 pairs"** for Cayman. That figure is
> **wrong** and was corrected to **962** in the
> [FIS-errtrig-spad CSR decode](../csr/fis-errtrig-spad.md) (report #930). The
> corrected count is byte-grounded against the flat address map:
>
> ```text
> rg -c 'ERRTRIG_TRIG_0' address_map_flat.yaml   -> 962
> rg -c 'ERRTRIG_TRIG_1' address_map_flat.yaml   -> 962
> rg -c 'ERRTRIG_NOTIFIC' address_map_flat.yaml  -> 962
> ```
>
> The 962 splits **428 USER + 534 AMZN** for both `TRIG_0` and `TRIG_1`, i.e.
> **962 generator PAIRS** (one `notific_1_queue` each). The "642" is inconsistent
> with `rg -c ERRTRIG_TRIG_0` on the very same file. Cite **962**. (The Maverick
> `1,372` figure is a *different SoC* and is not in conflict.)

### 1.2 The `intc_4grp` register set — the per-group error-trigger regfile `[HIGH · OBSERVED]`

`intc_4grp_no_msix_unit.json`: `UnitName=intc_4grp_no_msix_unit`, `Type=REGFILE`,
`AddrWidth=12` (`0x1000` window), `DataWidth=32`, `InterfaceType=APB`,
`RegfileFlavor=POSEDGE`. Parameter `INTC_NUM_GROUPS=4` (NonOverridable,
*"Number of interrupt groups"*). The `ctrl` bundle is an
`ArraySize=INTC_NUM_GROUPS=4` array (`BundleSizeInBytes=0x40`); each group is
**32 trigger bits**, so one `intc_4grp` = `4 × 32 = 128` inputs and the PAIR
(`TRIG_0`+`TRIG_1`) = **256-source** capacity.

The 12 per-group registers (absolute byte offset = `group*0x40 + offset`), all
offsets / access / reset byte-read from the schema:

| register | off | acc | reset | role |
| --- | --- | --- | --- | --- |
| `int_cause_grp` | `0x00` | RW | `0x0` | **THE ERROR LATCH.** HW sets on a source fire. *"Write-0 clears a bit. Write-1 has no effect"* (verbatim) = **W0C** (Annapurna convention, **not** W1C). On a HW-set / SW-clear collision in the same cycle, **HW wins** — bit stays set, no lost interrupt. |
| `int_cause_set_grp` | `0x08` | WO | `0x0` | **W1S** software inject / doorbell (write 1 sets, write 0 no-op). |
| `int_mask_grp` | `0x10` | RW | `0xffffffff` | **THE MASK.** All masked at reset (safe); firmware unmasks what it wants. |
| `int_mask_clear_grp` | `0x18` | WO | `0x0` | W0C side-door to *un*-mask one bit race-free. |
| `int_status_grp` | `0x20` | RO | `0x0` | raw latched source status (pre-mask). |
| `int_cdc_bypass_grp` | `0x24` | RW *(no_msix)* / **RO** *(msix)* | `0x0` | per-bit CDC edge-gen bypass. |
| `int_control_grp` | `0x28` | RW | (see §1.3) | 12-field group control (moderation, clear_on_read, auto_clear/mask, set_on_posedge, `Mask_msi_x`, AWID…). |
| `int_error_msk_grp` | `0x2C` | RW | `0xffffffff` | `Error = OR(Cause & !Error_Mask)`. |
| `int_abort_msk_grp` | `0x30` | RW *(no_msix)* / **RO** *(msix)* | `0xffffffff` | `Abort = OR(Cause & !Abort_Mask)`. |
| `int_fatal_msk_grp` | `0x34` | RW | `0xffffffff` | `Fatal = OR(Cause & !Fatal_Mask)`. |
| `int_log_msk_grp` | `0x38` | RW | `0xffffffff` | `Log = OR(Cause & !Log_Mask)`. |
| `int_posedge_grp` | `0x3C` | RW | `0x0` | per-bit edge(1)/level(0) select; **realizes** the trigger-YAML `edge_triggered` key (default level). |

Plus a single-instance **`Sunda` bundle @`0x0300`** (`BundleSizeInBytes=0xf0`,
*"Sunda-specific registers"*) — the abort-freeze controls, decoded in §3.4.

Verbatim `int_cause_grp` description (byte-read from the schema), establishing the
latch lifecycle:

```text
Interrupt Cause Register Set by hardware
- If MSI-X is enabled and auto_clear control bit =TRUE, automatically cleared
  after MSI-X message associated with this specific interrupt bit is sent
  (MSI-X acknowledge is received).
- Software can set a bit in this register by writing 1 to the associated bit in
  the Interrupt Cause Set register   Write-0 clears a bit. Write-1 has no effect.
- On CPU Read - If clear_on_read control bit =TRUE, automatically cleared.
When there is a conflict and on the same clock cycle, hardware tries to set a
bit in the Interrupt Cause register, the specific bit is set to ensure the
interrupt indication is not lost.
```

> **GOTCHA — W0C, not W1C `[HIGH · OBSERVED]`.** The Annapurna `int_cause`
> convention is **Write-0-to-Clear**: an ISR clears a serviced bit by writing
> **0** to it (and **1** to the bits it wants to leave alone), or relies on
> `clear_on_read` / `auto_clear`-on-MSI-X-ack. A naive "write 1 to clear"
> (the ARM GIC habit) does **nothing** here. The HW-set-wins conflict rule
> guarantees no edge is lost across the read-modify-write.

### 1.3 `int_control_grp` — the per-group behavior knobs `[HIGH · OBSERVED]`

Twelve fields, byte-read from the schema:

| field | bits | acc | reset | meaning |
| --- | --- | --- | --- | --- |
| `rev_id` | `[29:28]` | RO | `0x1` | INTC revision (note: reset `0x1`, not 0). |
| `Mod_res` | `[27:24]` | RW | `0` | moderation-timer clock-speed selector. |
| `Mod_intv` | `[23:16]` | RW | `0` | interval between interrupts; writing **zero disables** moderation. |
| `AWID` | `[11:8]` | RW | `0` | MSI-X AXI write ID (same ID for all cause bits). |
| `Chicken_addrhi` | `[7]` | RW | `0` | revert to old single high/msb-low address path. |
| `Chicken_posedge` | `[6]` | RW | `0` | revert to old whole-group posedge enable (vs per-bit). |
| `Mask_msi_x` | `[5]` | RW | `0` | *"When mask_msi_x =1, no MSI-X from this group is sent"* — the no_msix summary enable that feeds `nmi_out`. |
| `Mod_rst` | `[4]` | WO | `0` | moderation reset (self-negating). |
| `set_on_posedge` | `[3]` | RW | `0` | latch on rising edge. |
| `auto_clear` | `[2]` | RW | `0` | auto-clear cause on MSI-X ack. |
| `auto_mask` | `[1]` | RW | `0` | auto-mask after fire. |
| `clear_on_read` | `[0]` | RW | `0` | clear all cause bits on CPU read. |

The cause bit's lifecycle (set-on-fire; clear-on-MSI-X-ack / clear-on-read /
W0C) and the edge/level + moderation are **all per-group programmable**. This is
the exact latch/clear policy the firmware ISR depends on.

### 1.4 The `msix` vs `no_msix` flavor — delivery axis, not domain `[HIGH · OBSERVED]`

Bundle diff (byte-read):

| variant | bundles present |
| --- | --- |
| `no_msix` | `{ctrl, Sunda}` → 4 severity wire-ORs + abort-freeze |
| `msix` | `{ctrl, Sunda, PBA, VecTable, MSIX_Vector_Table_Space}` (adds parameter `NUM_OF_TRIGS=128`) |

In the `msix` variant `int_abort_msk_grp` and `int_cdc_bypass_grp` are **RO** —
the abort-severity path and CDC bypass are `no_msix`-only summary concerns; the
`msix` variant instead carries the PBA / VecTable MSI-X apparatus. So:

- **USER errtrig → `intc_4grp_msix`** — latches the **same** cause bits, but
  delivers an MSI-X message straight to the PCIe host (the host-management path).
- **AMZN errtrig → `intc_4grp_no_msix`** — latches the cause bits and emits the
  four classified severity wire-ORs + the `Mask_msi_x` summary = the single
  `nmi_out` wire fed **upward** into the `peb_intc` apex (the on-die path).

Instance census (flat map, `rg -c`):

| instance class | count |
| --- | --- |
| `intc_4grp_no_msix_unit` refs | **1070** |
| `intc_4grp_msix_unit` refs | **858** |
| `USER_ERRTRIG_TRIG_0` / `TRIG_1` | 428 / 428 |
| `AMZN_ERRTRIG_TRIG_0` / `TRIG_1` | 534 / 534 |

The PAIR is **universal**: the generator always emits 2 `intc_4grp` units
regardless of whether the source count fills 256. The only single-`4grp`
instances are the `peb_intc` apex (one `TRIG_0`) and the `intc_1grp_msix` RDM
root. The `+2` over the bare-PAIR multiple is the apex flavor-pair.

> **QUIRK — flavor tracks privilege, not block `[HIGH · OBSERVED]`.** An SDMA, a
> PCIe master, and an HBM controller each get **both** a USER errtrig (`msix`,
> host-visible) and an AMZN errtrig (`no_msix`, on-die summary). The split is
> "who is allowed to see this interrupt" — the host (USER) vs the privileged Q7
> (AMZN) — never "which IP block raised it".

### 1.5 The notific source backing — `notific_1_queue` `[HIGH · OBSERVED]`

`notific_1_queue.json` (`AddrWidth 12` / `0x1000` / APB / POSEDGE). Two bundles:
`notific` (`+0x0000`, arr=1) and `notific_nq` (`+0x100`, arr=`NUM_SW_Q`); **47
register defs total** (41 + 6). The errtrig's notification buffer/AXI faults are
generated by this queue's status logic and latched as `int_cause` bits. Backing
registers (byte-read offsets):

| register | off | acc | role |
| --- | --- | --- | --- |
| `hw_backpressure_lo` | `0x0c` | RW | stall-vs-drop select (HW buffer) |
| `wr_buf_enable_lo` | `0x30` | RW | per-HW-buffer enable |
| `sw_backpressure` | `0x08` | RW | SW backpressure select |
| `nq_error_addr_lo` | `0x40` | RO | errored-notification address |
| `nq_full` | `0xc4` | RO | per-SW-NQ full |

So the wiring is:

```text
NOTIFIC status bit  ->  errtrig int_cause  ->  (severity wire-OR / MSI-X)  ->  apex / host
```

> **NOTE — the NOTIFIC ordinal map is inferred `[HIGH · OBSERVED regs / MED · INFERRED map]`.**
> The 50-cause `fis_errtrig_intr[0..49]` vector (25 USER + 25 AMZN NOTIFIC
> mirror; §2.3) is described in text, not a numbered schema table. The
> *registers* are OBSERVED; the exact `notific-bit → fis_errtrig_intr[N]`
> ordinal binding is INFERRED from description prose.

---

## 2. The FIS error sources — what a FIS block flags

Each `FIS_0` container contributes exactly **three** interrupt vectors, one per
sub-region (`CTL` / `ERRTRIG` / `SPROT`). On the canonical leaves they are
`needs_cdc:false`, `edge_triggered:true` (SPOT-verified). This is the "FIS shim"
source family found in every leaf.

### 2.1 `fis_cntrl_intr[0..4]` (5) — EP posted-write SLVERR (from `FIS_0_CTL`) `[HIGH · OBSERVED]`

SPOT (`cc_triggers.yaml` / `top_sp_triggers.yaml`, verbatim; `needs_cdc:false`,
`edge_triggered:true` on all five):

| idx | verbatim description |
| --- | --- |
| `[0]` | AMZN chain AMZN EP posted write slave error |
| `[1]` | AMZN chain USER EP posted write slave error |
| `[2]` | AMZN chain USER-FIS EP posted write slave error |
| `[3]` | USER chain USER EP posted write slave error |
| `[4]` | USER chain USER-FIS EP posted write slave error |

Produced by the `FIS_0_CTL` `apb_timeout.ctrl.limit` watchdog (reset `0x2000`;
`0=Disabled`, verbatim *"Limit for the APB timeouts on AMZN Chain EPs"*): a
posted APB write to a hung EP that never completes expires the watchdog →
SLVERR → these 5 triggers.

> **CORRECTION — HW-only in Cayman, registered in Mariana
> `[HIGH · OBSERVED]`.** In Cayman these 5 exist **only as HW triggers** — there
> is *no* cause/mask/status register backing them. Mariana promotes them into a
> first-class `fis_cntrl_intr` register bundle (mask / clr_on_read / status /
> `apb_blk_error_addr`). Do not look for a Cayman `fis_cntrl_intr` CSR — it does
> not exist (`fis_control` grows from 7 bundles in Cayman to 8 in Mariana). See
> [the cross-gen delta](../csr/fis-errtrig-spad.md).

### 2.2 `fis_sprot_intr[0..5]` (6, per sprot sub-block) — firewall / isolation / security `[HIGH · OBSERVED]`

SPOT (`top_sp_triggers.yaml`, the canonical 6-entry single-index set, verbatim;
all `needs_cdc:false`, `edge_triggered:true`):

| idx | verbatim description | source |
| --- | --- | --- |
| `[0]` | `amzn_remapper denied a transaction` | access-control DENY (firewall) |
| `[1]` | `delta monitor interrupt for R responses more than AR request` | AXI-integrity underflow (read) |
| `[2]` | `tmu detected an AXI timeout` | QoS/timeout monitor |
| `[3]` | `delta monitor interrupt for B responses more than AW request` | AXI-integrity underflow (write) |
| `[4]` | `qos pmu interrupt (OR of all 16 PMU counter interrrupts)` *(typo verbatim)* | QoS PMU OR |
| `[5]` | `fis_sprot_spare_0` | spare |

> **GOTCHA — the sprot vector is replicated per sprot sub-block
> `[HIGH · OBSERVED]`.** In `cc_triggers.yaml` / `d2d_triggers.yaml` /
> `io_fabric_triggers.yaml` the vector is a **2-D** `fis_sprot_intr[0..1][0..5]`
> (descriptions prefixed `sprot 0 …` / `sprot 1 …`) — `6 causes × 2 sprot
> sub-blocks = 12` entries per leaf, which is why the routing leaf count is
> **12** (§3.1). The canonical 6-entry single-index form (with the meaningful
> descriptions) lives in `top_sp_triggers.yaml`. Beware: `pcie_triggers.yaml`
> carries generic placeholders (*"FIS SPROT interrupt bit N"*) that are
> **level**-triggered, and `sdma`/`hbm` leaves carry **no** `fis_sprot_intr` at
> all.

**The source blocks** — the FIS enforcement stack that *generates* the errors
(remapper FIRST, qos SECOND in series at each master's egress):

- **`amzn_remapper` / `user_remapper`** (access-control CAM): `{masked-addr +
  AXI-ID}` → `rd/wr_pass` DENY, or a MISS under `pass_on_miss`; plus bound-check,
  txn-len cap, axi rd/wr timeout. A DENY captures `addr_denied`, increments the
  deny counters, and raises `fis_sprot_intr[0]`.
- **`delta_mon`** (resp > req underflow): `B>AW` → `fis_sprot_intr[3]`, `R>AR` →
  `fis_sprot_intr[1]` (the AXI transaction-integrity anomaly).
- **`qos_prot`** TMU `axi_rd/wr_timeout` → `fis_sprot_intr[2]`; QoS PMU OR-of-16 →
  `fis_sprot_intr[4]`.

**The fail-closed / fail-open trust boundary** (byte-read reset values):

| remapper | `rd_pass_on_miss` | `wr_pass_on_miss` | `arprot` | `awprot` | posture |
| --- | --- | --- | --- | --- | --- |
| `amzn_remapper` | `0x0` | `0x0` | `0x2` | `0x2` | **FAIL-CLOSED** — a CAM miss is **Deny** (whitelist-by-default, privileged); `master_prot` AxPROT non-secure-privileged. |
| `user_remapper` | `0x1` | `0x1` | *(absent)* | *(absent)* | **FAIL-OPEN** — a miss is **Pass** (guest); no `master_prot` register at all. |

Verbatim from `amzn_remapper.json`: *"0 - Reads that miss in the AMZN CAM are
marked Deny / 1 - Reads that miss in the AMZN CAM are marked Pass"*, reset `0x0`.

> **QUIRK — privileged fails closed, guest fails open `[HIGH · OBSERVED]`.** The
> AMZN remapper defaults to **deny-on-miss** (`pass_on_miss = 0x0`): an address
> the privileged firmware never whitelisted is blocked. The USER remapper
> defaults to **pass-on-miss** (`= 0x1`): the guest's address space is open
> unless explicitly fenced. Both the privileged DENY and the guest DENY surface
> through the **same** AMZN-named `fis_sprot_intr[0]` line — the privileged ISR
> sees both `[MED · INFERRED]`.

### 2.3 The 50-cause `fis_errtrig_intr` vector (NOTIFIC mirror) `[HIGH · OBSERVED]`

The `ERRTRIG` sub-region's own vector is `fis_errtrig_intr[0..49]` = **50**
entries, split **25 USER + 25 AMZN** (the two NOTIFIC error paths mirrored). All
`edge_triggered:true`, `needs_cdc:false`.

> **GOTCHA — count hazard `[HIGH · OBSERVED]`.** A naive
> `rg -c 'fis_errtrig_intr'` returns **100**, not 50 — each YAML entry carries
> *both* a `trigger:` and a `name:` line. The correct count anchors on the entry
> head: `rg -c '^- trigger: fis_errtrig_intr\['` = **50**.

### 2.4 The NSM AXI-integrity source — a separate PEB leaf `[HIGH · OBSERVED]`

`nsm.json` is an inline per-PCIe-master watchdog (schema-dir `sprot`, but a
standalone `AMZN_PEB_NSM` leaf; bundles `control`/`wr`/`rd`/`spare`). It does
**not** check AxPROT / master-ID / address (that is the remapper) — it watches
**protocol shape + timing**. The 9 causes (RO status bits, byte-read):

```text
rd: error_0_rlast_before_last_rdata  error_1_rlast_not_set  error_2_r_no_match_ar
    error_3_r_to_ar_timeout          error_4_arvalid_to_arready_timeout
wr: error_1_b_no_match_aw_ro         error_2_b_to_aw_timeout
    error_3_awvalid_to_awready_timeout   error_4_wvalid_to_wready_timeout
```

On a violation NSM (a) sets a sticky `wr/rd.status` → `control.report`; (b)
**injects** SLVERR/DECERR (`axi_bresp`/`axi_rresp` reset `0x2`, *"2'b10:
SLVERR"*) + 256-bit RDATA (**8× `0xDEADBEEF`** — byte-confirmed: `rd/error_data_0
.. error_data_7 .val`); (c) optionally drives isolation entry (**6**
`enter_isolation_mode_on` causes). **NSM reaches the apex by a DIRECT critical
bit (§3.3), not through a `fis_sprot_intr` line** — it is the security fast-path.

### 2.5 The qos NTS no-target responder `[HIGH · OBSERVED]`

`qos_prot.json`: `no_target_mode` + `read_response` + `write_response` +
`nts_isolation` present; **2× `0xDEADBEEF`** (byte-confirmed). When a target
slave is absent/flushed, the No-Target-Slave (NTS) responder terminates AXI with
**SLVERR** (`nts_amzn/read_response.val` / `write_response.val` reset `0x2`,
verbatim *"10-SLVERR"*) + a `0xDEADBEEF` `read_data.val` via a
`no_target_mode → flushing → flushed` FSM. The remapper DENY **reuses** this
response path (it has no own error-response field) `[MED · INFERRED]`. NTS
timeout → `fis_sprot` pathway / isolation-SM input.

> **NOTE — the NTS terminator family `[HIGH · OBSERVED]`.** Three blocks
> terminate AXI faults with the **same signature — `SLVERR (0x2)` + `0xDEADBEEF`
> read-data**: `qos_prot` (NTS no-target, 2× deadbeef), `amzn_remapper` /
> `user_remapper` (deny, reusing the NTS path), and `nsm` (8× deadbeef). When
> you see `0xDEADBEEF` on the read-data bus, the master hit one of these FIS
> guards. The FIS error sources all feed this fabric.

### 2.6 The per-IP RAS/ECC sources that feed the same errtrig `[HIGH · CARRIED]`

Beyond the 3 FIS vectors, each block's own RAS/ECC/parity latches drive its
errtrig cause bits as ordinary edge/level sources: **150 precise leaf RAS** (HBM
134 + TPB 12 + CC 2 + TOP_SP 2) plus ~77 domain-distributed parity ≈ **227 leaf
RAS total**. These ride the errtrig like any other source — **RAS is not a
separate controller, it is a source class on the errtrig `int_cause` bus.**
*(Carried from the RAS/INT-16 sibling decodes; HIGH for the 4 precise
components, MED aggregate.)*

---

## 3. The aggregation / routing — source → errtrig → `nmi_out` → apex → IRQ

The error path is the L0→L3 cascade, made concrete here for **error** sources.

### 3.1 L0 source → L1 errtrig latch (per-master) `[HIGH · OBSERVED]`

A hardware error (FIS deny / NTS / delta-mon / tmu / qos-PMU / EP-posted-wr /
notific buffer-full/AXI / RAS/ECC) sets a bit in the master's errtrig
`int_cause_grp` (**W0C**, HW-set-wins). `int_mask_grp` (`0xffffffff` at reset)
gates it; `int_posedge_grp` selects edge(event-pulse) vs level(sticky-status).
All FIS vectors and per-block RAS latches land on the same **256-input errtrig
PAIR**.

The five **routing leaf counts** (the per-leaf trigger-YAML vector widths) must
agree across the source-map decodes — byte-anchored from `io_fabric_triggers.yaml`
with the head-anchored `^- trigger:` form (to dodge the §2.3 count hazard):

| leaf vector | count | meaning |
| --- | --- | --- |
| `intc_top_retrigger[0..127]` | **128** | INTC mailbox retrigger (AXI re-assert of a vector bit) |
| `fis_errtrig_intr[0..49]` | **50** | FIS errtrig NOTIFIC (25 user + 25 amzn) |
| `notific` (per-chain NOTIFIC causes) | **25** | one chain of the 50 (user *or* amzn) |
| `fis_sprot_intr` | **12** | `6 causes × 2 sprot sub-blocks` per leaf (§2.2) |
| `fis_cntrl_intr[0..4]` | **5** | EP posted-write SLVERR |

```c
// L0->L1: source fire latched into the errtrig cause register (per group g, bit b).
// 'group' = source-region cut (0..3); one intc_4grp = 4 groups x 32 bits.
void errtrig_latch(ErrtrigUnit *u, unsigned g, unsigned b) {
    // HW set is unconditional and wins any same-cycle SW clear (no lost interrupt).
    u->grp[g].int_status |=  (1u << b);          // raw, pre-mask
    u->grp[g].int_cause  |=  (1u << b);          // W0C latch; HW-set wins
    // optional posedge gating: only latch on rising edge if set_on_posedge / posedge select.
    // delivery is gated downstream by int_mask (1 = masked at reset).
}

// ISR clear is WRITE-0-TO-CLEAR, not W1C: zero the serviced bits, keep the rest.
static inline void errtrig_clear(ErrtrigUnit *u, unsigned g, uint32_t serviced) {
    u->grp[g].int_cause = ~serviced;             // 0 clears, 1 leaves unchanged
}
```

### 3.2 L1 errtrig → severity classify → `nmi_out` (no_msix) OR MSI-X (msix) `[HIGH · OBSERVED]`

The `no_msix` errtrig produces **four independent classified summary lines**,
each a wire-OR of `(Cause & !per-severity-Mask)`: `Error` (`int_error_msk`),
`Abort` (`int_abort_msk`), `Fatal` (`int_fatal_msk`), `Log` (`int_log_msk`). One
cause bit can feed up to all four (independent masks). These plus the
`Mask_msi_x` summary = the single `nmi_out` wire
(`...u_amzn_errtrig_prot.nmi_out`). The `msix` errtrig instead emits an MSI-X
message to the host via its PBA/VecTable.

```c
// L1->L2: per-group severity classification, OR-reduced across the 4 groups of a unit,
// then OR-reduced across TRIG_0+TRIG_1 of the PAIR -> one nmi_out wire.
typedef struct { uint32_t error, abort, fatal, log, summary; } SevWires;

static SevWires classify_group(const ErrtrigGroup *g) {
    uint32_t live = g->int_cause & ~g->int_mask;            // mask: 1 = masked
    SevWires w = {
        .error = live & ~g->int_error_msk,                 // 1 in mask = NOT this severity
        .abort = live & ~g->int_abort_msk,                 // no_msix only (RO in msix)
        .fatal = live & ~g->int_fatal_msk,
        .log   = live & ~g->int_log_msk,
    };
    // summary participation gated by per-group Mask_msi_x (int_control[5]).
    w.summary = g->ctrl.mask_msi_x ? 0 : live;             // mask_msi_x=1 suppresses the MSI-X
    return w;
}

// nmi_out = OR over all groups of all units in the PAIR. Any nonzero severity asserts it.
bool errtrig_nmi_out(const ErrtrigUnit *pair, int n_units) {
    uint32_t any = 0;
    for (int u = 0; u < n_units; u++)
        for (int g = 0; g < INTC_NUM_GROUPS; g++) {        // INTC_NUM_GROUPS = 4
            SevWires w = classify_group(&pair[u].grp[g]);
            any |= w.error | w.abort | w.fatal | w.log | w.summary;
        }
    return any != 0;                                       // -> one apex input bit
}
```

> **NOTE — mask polarity is inferred `[HIGH · values / MED · INFERRED polarity]`.**
> The reset value `0xffffffff` on every mask + the schema's *"all masked at
> reset"* phrasing strongly implies **`1 = masked`** (and a 1 in a *severity*
> mask means "this bit does NOT feed that severity"). The `Mask_msi_x` field text
> (*"When mask_msi_x =1, no MSI-X … is sent"*) corroborates the active-high-mask
> reading. The cause/severity-mask polarity is not stated in-schema; this reading
> is well-corroborated but flagged.

### 3.3 L2 apex (`peb_intc`) — 96 summaries + 32 criticals fill 128 inputs `[HIGH · OBSERVED]`

The `peb_intc` apex is itself an `intc_4grp` flavor-pair —
`{PEB_INTC_TRIG_0 (no_msix, the 128-input summary apex) + PEB_INTC_MSIX (the
host-delivery twin)}`, one per PEB (×2). SPOT (`peb_intc_triggers.yaml`):
**total = 128, critical = 32, edge = 24** (the YAML encodes `critical: 1` as an
integer — the other 96 omit the key — and `edge_triggered: true`).

The 128 inputs = **96 no_msix-leaf `nmi_out` summaries** (file order = bit order)
+ **32 direct PEB-local critical sources**. The error-path criticals and the FIS
error vectors land at fixed apex indices (byte-read; `nmi_mask=0` on all of
these):

| apex input | idx | critical | edge | role |
| --- | --- | --- | --- | --- |
| `hbm_inttrig_cattrip_r[0]` / `[1]` | 80 / 82 | 1 | LEVEL | HBM thermal (cattrip) |
| `hbm_inttrig_temp_change_r[0]` / `[1]` | 81 / 83 | 1 | LEVEL | HBM thermal (temp change) |
| `erg_intr_trig_uncerr_r` | 106 | 1 | edge | uncorrectable RAS (PEB ERG) |
| `io_fabric_erg_uncerr_intr_local` | 107 | 1 | edge | uncorrectable RAS (io-fabric ERG) |
| `apb_outstding_flushed_clr_triggers_out` | 109 | 1 | LEVEL | isolation/recovery drain |
| `apb_outstding_flushed_set_triggers_out` | 110 | 1 | LEVEL | isolation/recovery drain |
| `intr_peb_nsm_axi_timeout` | **111** | 1 | **LEVEL** | **NSM security fast-path** |
| `fis_sprot_intr[0..4]` | 118–122 | — | edge | FIS sprot (no critical) |
| `fis_cntrl_intr[0..4]` | 123–127 | — | edge | FIS posted-wr SLVERR |

The 32 criticals are the **SoC-survival set**. Of the error path specifically:
NSM (security), `apb_outstding_flushed` (isolation drain), `erg`/`io_fabric_erg`
uncerr (uncorrectable RAS) are the error-path criticals. **All other errors
(including uncorrectable leaf RAS in TPB/CC/d2d/TOP_SP) ride the generic `*_nmi`
summary** — only SoC-survival errors earn a direct critical bit.

> **GOTCHA — the apex NSM index matches the firmware cause enum
> `[HIGH · OBSERVED]`.** `intr_peb_nsm_axi_timeout` sits at apex idx **111**,
> which is exactly the firmware NSM critical cause id (`intc_peb_intc_enums.h`
> NSM = 111, `.critical=true`). It is `edge_triggered:false` (**LEVEL**) and
> `nmi_mask:0` — a sticky, always-armed security input. (On the narrower Sunda
> 97-wide apex this binding is NSM = 96 instead — a per-gen offset, not a
> conflict.) At the apex only the **5** single-index `fis_sprot_intr[0..4]`
> entries appear (118–122); the 6th, `fis_sprot_spare_0`, is a leaf-only entry.

### 3.4 The Abort-severity output → scan-dump (the boundary) `[HIGH · OBSERVED]`

The `no_msix` errtrig's **`Sunda` bundle @`0x0300`** carries the abort-freeze
controls the `Abort` wire-OR drives (byte-exact fields):

| register | off | field | bits | acc |
| --- | --- | --- | --- | --- |
| `abort_cntl0` | `+0x0` | `local_abort_scan_dump` | `[7:0]` | RW |
| | | `remote_abort_scan_dump` | `[15:8]` | RW |
| | | `local_abort_clock_stop` | `[23:16]` | RW |
| | | `remote_abort_clock_stop` | `[31:24]` | RW |
| `abort_cntl1` | `+0x4` | `local_abort_to_block_level_logic` | `[7:0]` | RW |
| | | `local_abort_sram_write_protect` | `[15:8]` | RW |
| | | `remote_abort_sram_write_protect` | `[23:16]` | RW |
| | | `reserved` | `[31:24]` | RW |

When a cause bit fires the **Abort** severity, the errtrig can trigger
local/remote **scan-dump**, **clock-stop**, and **SRAM-write-protect** — the
post-mortem freeze. This is the **entry point** into the
[Abort / Scan-Dump / Clock-Stop](./abort-scandump-clockstop.md) domain. The
routing (`Cause → Abort wire-OR → abort_cntl scan_dump/clock_stop`) is OBSERVED
here; the scan-dump *capture* mechanics belong to that page and are **not**
re-decoded. The Abort path is **`no_msix`-only** (the `msix` variant has
`int_abort_msk` RO — §1.4).

> **CROSS-LINK.** The `Sunda` `0x300` bundle is the same FREEZE bundle every
> `no_msix` errtrig carries; the
> [abort/scan-dump page](./abort-scandump-clockstop.md) consumes this hand-off.
> Do not duplicate the freeze decode here.

### 3.5 L3 delivery → Q7 / "Pacific" / GIC `[chain HIGH · OBSERVED; final hop LOW · INFERRED]`

The apex `no_msix TRIG_0` rolls the 128 inputs into one output; the
`PEB_INTC_MSIX` twin delivers via MSI-X into the IOFIC/io_fabric cascade to the
Q7 / "Pacific" management core IRQ / a GIC. The firmware-facing cause tables
(`intc_peb_intc_enums.h`: NSM critical cause = 111; `intc_peb_intc_info.c`:
`.index=111 .critical=true`; `intc_info_struct.h`: `critical="*not a summary*"`,
"Pacific" = Q7) are the ISR dispatch metadata:

```text
apex MSI-X raises
  -> Q7 reads the pending image
     -> .critical = true  : fast-path handler, WITHOUT decoding a summary
     -> .critical = false : descend into the named leaf summary register
```

> **NOTE — final hop is firmware/HW-owned `[LOW · INFERRED]`.** The
> apex-pending-bit → Q7/GIC vector hop is **not register-encoded**; only the
> cause/info dispatch *tables* ship. Everything down to the apex MSI-X is
> OBSERVED; the vector hop and the ISR bodies are inferred.

### 3.6 The error-trigger routing diagram `[HIGH · OBSERVED]`

```text
 [FIS deny/NTS/delta/tmu]  [nsm AXI integrity]  [notific buf/AXI]  [per-blk RAS/ECC]
        |                          |                    |                  |
        | fis_sprot/fis_cntrl/fis_errtrig vectors + RAS latches            |          L0
        v                          |                    v                  v
 errtrig int_cause_grp (W0C latch; int_mask gates; int_posedge edge/level)            L1
        |                          |
   no_msix: 4 severity wire-ORs    | (NSM -> DIRECT apex critical, bypasses errtrig sum)
   Error/Abort/Fatal/Log + Mask_msi_x
        |  Abort -> abort_cntl (Sunda @0x300)
        |  scan_dump / clock_stop / sram_wp --+--> abort-scandump-clockstop.md
        v
   nmi_out summary  ---OR---  (USER errtrig: MSI-X straight to PCIe host)
        |
        v
 peb_intc APEX  (128-in: 96 nmi_out summaries + 32 direct criticals;                  L2
   NSM idx111 / flush 109-110 / ERG 106-107 / HBM-thermal 80-83)
   {TRIG_0 no_msix + MSIX twin}, per-PEB
        |  MSI-X
        v
 IOFIC / io_fabric cascade -> Q7 / "Pacific" / GIC   (apex-bit -> vector = FW/HW)     L3  [LOW]
```

---

## 4. errtrig vs functional trigger + the FATAL/MASKABLE classification

### 4.1 Same hardware, different source class `[HIGH · OBSERVED / MED · INFERRED]`

The errtrig is **not** a separate controller from the functional-interrupt fabric
— it is the **same `intc_4grp` primitive**. The distinction is *what drives the
cause bit* and *how it is severity-classified*:

- **Functional/event sources** (compute-done, SDMA queue completion, notific
  write completion): mostly **edge** (event pulse), routed to the host via the
  USER/`msix` errtrig and summarized on-die via the AMZN/`no_msix` errtrig. Not
  classified Abort/Fatal by default.
- **Error/RAS sources** (FIS violations, NSM/NTS isolation, RAS/ECC latches,
  notific buffer-full/drop/AXI): the errtrig's **error role**, classified by the
  per-severity masks; the SoC-survival ones promoted to direct apex criticals.

The name `errtrig` reflects that the aggregator was *designed around* the
error/RAS surface, but the same block carries functional notification triggers
too. The separation is the **severity-classification + critical-promotion**, not
the controller. *(HIGH for the shared-primitive fact; MED for the per-source
default-severity reading — the schema gives the mask *mechanism*; the per-source
*assignment* is instantiation-time and not register-encoded.)*

### 4.2 FATAL vs MASKABLE `[HIGH · mechanism / MED · per-source]`

- **Every errtrig cause bit is MASKABLE.** `int_mask_grp` (all-masked at reset)
  gates delivery; the four per-severity masks independently gate which severity
  wire-OR a cause feeds. So **"fatal" is not a hardware-fixed attribute** of a
  source — it is whichever cause bits the `Fatal` mask lets through (firmware
  policy).
- **The apex `critical:1` flag is the SoC-level fatal / fast-path marker** —
  `critical = "*not a summary*"` = an event firmware must handle without decoding
  a summary. The 32 apex criticals are the de-facto FATAL/SURVIVAL set: thermal
  (cattrip/PVT), config-bus hang (axi2apb), security/isolation (NSM, apb-flush),
  uncorrectable RAS (ERG), management bus. On the error path specifically: **NSM
  (security), `apb_outstding_flushed` (isolation drain), `erg`/`io_fabric_erg`
  uncerr (uncorrectable RAS)** are the error-path criticals.

### 4.3 RAS classification `[HIGH · CARRIED]`

150 precise leaf RAS sources (HBM 134 + TPB 12 + CC 2 + TOP_SP 2) + ~77
domain-distributed parity (~227 total). Correctable (SBE) rides `Error`/`Log` +
a generic summary; **uncorrectable** (DBE / ERG uncerr) rides `Fatal` and, for
ERG, earns a **direct apex critical** (`erg_intr_trig_uncerr_r` /
`io_fabric_erg_uncerr_intr_local`). HBM thermal (cattrip/temp_change)
**bypasses** the generic `hbm_{0,1}_nmi` summary with its own 4 direct apex
criticals (react before the device cooks). *(Carried from the RAS/INT-04/16
siblings; HIGH for the named components.)*

---

## 5. Per-generation deltas (Cayman authoritative)

### 5.1 The frozen primitive `[HIGH · OBSERVED]`

The `intc_4grp` errtrig (`TRIG_0`+`TRIG_1`, the 12-register per-group set, the 4
severity wire-ORs, W0C/W1S) is **byte-structurally identical** across
Sunda/Cayman/Mariana/Mariana+/Maverick. `notific_1_queue` likewise stable. The
remapper CAM core + qos_prot shaper + nsm watchdog enforcement core
(fail-closed/open, AxPROT `0x2`, the 9 nsm causes, 8× deadbeef) are **frozen
Cayman == Maverick**.

### 5.2 Cayman additions over Sunda `[HIGH · CARRIED]`

remapper widens AXI master-ID 8→10 bit + adds bypass-ID + txn-len cap; qos_prot
adds `nts_isolation` + NTS no-target/BLOCK mode + widens block-ID 7→9. The apex
grows Sunda 97 (35 critical) → Cayman 128 canonical (32 critical). NSM enum
binding: Sunda NSM = 96 vs Cayman/Mariana NSM = 111.

### 5.3 Mariana hardening `[HIGH · CARRIED]`

The 5 `fis_cntrl_intr` posted-wr SLVERRs that are HW-only triggers in Cayman are
**promoted** in Mariana into a first-class register bundle (mask / clr_on_read /
status / `apb_blk_error_addr`) — `fis_control` 7 bundles → 8. Mariana also adds
qos_prot AXI master/slave parity + AXI protocol checkers + remapper AXI-parity.

### 5.4 Maverick (v5) — decentralized rewrite `[HIGH · OBSERVED header / MED · INFERRED interior]`

> **NOTE — v5 is header-OBSERVED only; the interior is INFERRED
> `[HIGH · header / MED · INFERRED]`.** The Maverick deltas below are read from
> the `arch-headers` (the structural banner) and cross-gen pkl nomenclature; the
> v5 register *interior* is not byte-walked the way Cayman is. Treat
> Maverick-interior claims as INFERRED.

- The errtrig PAIR survives **frozen** (`TRIG_0 == TRIG_1 == 1,372` symmetric for
  that SoC; `ERRTRIG` keyword 5,488; `FIS` keyword 34,384 = 8,568 user + 25,816
  secure).
- **New per-IP embedded INTC fleet:** 13 distinct per-IP INTC schemas (5,904
  `type=INTC` nodes). **Cayman has zero per-IP INTCs** (verified absent). So
  Maverick **decentralizes** interrupt control into each IP block — the errtrig
  stops being the sole aggregator.
- New `iofic_x8_msix` 256-input security IOFIC; `ap_intc_grp_ctrl` gains
  `int_sec_grp` (SWOM secure-write-one-mask) + `int_regs_sec_grp` (per-register
  write-locks), gating which cause bits a non-secure master may **set**.
- 3-die plane (C_DIE/H_DIE/IO_DIE); the apex restructures to 119 entries (79
  criticals vs Cayman 32).
- Per-IP errtrig naming in the pkl is `short_name = ERRTRIG_INTC` (528); the
  Cayman flat-YAML uses `ERRTRIG_TRIG_0`/`TRIG_1` (`rg ERRTRIG_INTC` = 0 in
  Cayman). In Cayman the HBM FIS errtrig is
  `..._HBM_FIS_0_USER_ERRTRIG_TRIG_{0,1}` — the naming-gen reconciliation is
  itself a finding.

---

## 6. Provenance ledger

| claim | confidence | grounding |
| --- | --- | --- |
| errtrig generator = `intc_4grp` PAIR + `notific_1_queue` in `0x3000` container | `[HIGH · OBSERVED]` | `errtrig_*.yaml` + `address_map_flat.yaml` |
| **Cayman PAIR count = 962** (428 USER + 534 AMZN); `TRIG_0`/`TRIG_1`/`NOTIFIC` all = 962 | `[HIGH · OBSERVED]` | `rg -c` on flat map — **corrects #908's "642"** |
| census: 1070 no_msix / 858 msix `intc_4grp` refs | `[HIGH · OBSERVED]` | `rg -c` on flat map |
| 12-register `intc_4grp` set, offsets `0x00..0x3C`, `int_cause` W0C, `int_mask` rst `0xffffffff`, 4 severity wire-ORs | `[HIGH · OBSERVED]` | `intc_4grp_no_msix_unit.json` |
| `int_control_grp` 12 fields incl `Mask_msi_x[5]` / `clear_on_read[0]` / `auto_clear[2]`, `rev_id` rst `0x1` | `[HIGH · OBSERVED]` | `intc_4grp_no_msix_unit.json` |
| msix adds PBA/VecTable/`NUM_OF_TRIGS=128`; `int_abort_msk` + `int_cdc_bypass` RO in msix | `[HIGH · OBSERVED]` | bundle diff, both unit JSONs |
| Sunda `@0x300`; `abort_cntl0/1` scan_dump/clock_stop/sram_wp fields | `[HIGH · OBSERVED]` | `intc_4grp_no_msix_unit.json` |
| `notific_1_queue` 47 reg-defs; `nq_full`@0xc4 / `wr_buf_enable_lo`@0x30 / `hw_backpressure_lo`@0x0c | `[HIGH · OBSERVED]` | `notific_1_queue.json` |
| `fis_cntrl_intr[5]` / `fis_sprot_intr[6 single → 12 per-leaf]` / `fis_errtrig_intr[50]` verbatim, edge/cdc | `[HIGH · OBSERVED]` | `top_sp` / `cc` / `io_fabric` `_triggers.yaml` |
| routing leaf counts `intc_top_retrigger[128]` / `fis_errtrig(50)` / `notific(25)` / `fis_sprot(12)` / `fis_cntrl(5)` | `[HIGH · OBSERVED]` | `^- trigger:`-anchored `rg -c` on `io_fabric_triggers.yaml` |
| `apb_timeout.ctrl.limit` rst `0x2000`; `fis_control` 7 bundles Cayman | `[HIGH · OBSERVED]` | `fis_control.json` |
| amzn `pass_on_miss=0x0` fail-closed + `arprot/awprot=0x2`; user `pass_on_miss=0x1` fail-open | `[HIGH · OBSERVED]` | `amzn_remapper.json` / `user_remapper.json` |
| NTS family: qos `read/write_response.val=0x2` (SLVERR) + 2× deadbeef; nsm `axi_bresp/rresp=0x2` + 8× deadbeef + 9 causes + 6 isolation | `[HIGH · OBSERVED]` | `qos_prot.json` / `nsm.json` |
| apex 128 total / 32 critical / 24 edge; NSM idx111 LEVEL crit; fis_sprot 118-122; fis_cntrl 123-127 | `[HIGH · OBSERVED]` | `peb_intc_triggers.yaml` |
| 4-severity wire-OR semantics + errtrig-pair universal; NSM 2-path + FW cause table | `[HIGH · CARRIED]` | CSR-10 / INT-11 / INT-15/16 siblings |
| RAS 150 precise (HBM 134 + TPB 12 + CC 2 + TOP_SP 2) / ~227 aggregate | `[HIGH · CARRIED]` | INT-04/10/16 siblings |
| mask polarity `1=masked`; per-source default severity; remapper reuses NTS path; user deny on amzn line | `[MED · INFERRED]` | description prose, cross-file corroboration |
| Maverick interior (13 per-IP INTCs, iofic_x8_msix, int_sec_grp, 119-apex) | `[MED · INFERRED]` | arch-headers + pkl nomenclature (header-OBSERVED) |
| apex-pending-bit → Q7/GIC vector hop; ISR bodies | `[LOW · INFERRED]` | not register-encoded; firmware/HW-owned |
