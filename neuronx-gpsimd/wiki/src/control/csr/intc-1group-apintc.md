# CSR — INTC 1-Group + ap_intc (IOFIC)

This page is a **delta page**. It documents only where the **1-group APB INTC**
(`intc_1grp_no_msix_unit` / `intc_1grp_msix_unit`) and the **`ap_intc` / IOFIC**
leaf aggregators (`ap_intc_{1,2,4}grp_unit` + the shared `ap_intc_grp_ctrl` body)
diverge from the 4-group baseline. **Read [`intc-4group.md`](./intc-4group.md) first** —
it carries the shared register vocabulary (the per-bit cause / mask / status / set /
clear / posedge semantics, the four severity wire-ORs, the MSI-X apparatus, the
`Sunda` abort-control bundle). This page does **not** re-derive that bank; it states
the exact bytes that change and nothing else.

All evidence is read directly out of the shipped register-description JSON, the
shipped Mako generator template (`intc_ngrp_unit.json.mako`), the shipped trigger
YAMLs, and the shipped address-map xref — all RTL-generated, binary-derived,
citeable artifacts. Every claim is tagged `[conf · prov]` (`HIGH`/`MED`/`LOW` ×
`OBSERVED`/`INFERRED`/`CARRIED`). The byte-grounded generation is **Cayman (NC-v3)**;
Maverick (v5 / MAVERICK) is **header-OBSERVED only**, so v5-interior claims are
flagged `INFERRED`.

Related: [`../interrupt/physical-intc-instances.md`](../interrupt/physical-intc-instances.md)
(the physical-instance census), [`../interrupt/io-fabric-triggers.md`](../interrupt/io-fabric-triggers.md),
[`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md),
[`../interrupt/schema-atlas.md`](../interrupt/schema-atlas.md),
[`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md),
and the address-side sibling [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md).

---

## 0. The two families on this page

| family | what it is | interface | the divergence in one line |
|--------|-----------|-----------|----------------------------|
| `intc_1grp_{no_msix,msix}_unit` | the 4-group APB INTC at **`ngrp=1`** | **APB**, `0x1000` aperture | *the 4grp template with `INTC_NUM_GROUPS=1`* — no register added, removed or re-bit |
| `ap_intc_{1,2,4}grp_unit` + `ap_intc_grp_ctrl` | a leaner **MEM-mapped IOFIC** (HalName `iofic_x{1,2,4}`) used **inside the PMDT** complex | **MEM** wrapper / **NONE** body | a *different* 9-register block: drops 3 registers + 3 control bits, and **resets `int_mask_grp` to `0` (UNMASKED)** |

> **NOTE — "1-group" is two unrelated things.** `intc_1grp` is the *same* APB INTC
> as the 4-group block, just instanced at one group. `ap_intc_1grp` is a *structurally
> different* block (the IOFIC group body) whose `1grp` width happens to also be one
> group. They share register *names* (`int_cause_grp`, `int_mask_grp`, …) and the
> W0C/W1S polarity, but `ap_intc` is **not** a resized `intc`. Keep them separate.
> `[HIGH · OBSERVED]`

---

## 1. `intc_1grp` — exactly two bytes-of-meaning vs `intc_4grp` `[HIGH · OBSERVED]`

A `jq -S` whole-file diff of `intc_1grp_*_unit.json` against `intc_4grp_*_unit.json`
returns **exactly two changed lines**, for *both* flavors:

```
intc_1grp_no_msix  vs  intc_4grp_no_msix :
  Parameters[INTC_NUM_GROUPS].Value :  "1"  vs  "4"
  RegFile.UnitName                  :  intc_1grp_no_msix_unit  vs  intc_4grp_no_msix_unit

intc_1grp_msix     vs  intc_4grp_msix    :   same two lines (Value, UnitName). Nothing else.
```

Everything else — every register offset, every bit position, every `AccessType`,
every `ResetValue`, every `Description`, the whole MSI-X apparatus *definition*, the
`Sunda` bundle, the `0x1000` aperture, `AddrWidth=12` — is **byte-identical**. The
1grp unit *is* the same Mako template rendered at `ngrp=1`. Consequently the regfile
metadata is unchanged from the 4-group baseline:

| field | `intc_1grp_no_msix` | `intc_1grp_msix` | vs 4grp |
|-------|---------------------|------------------|---------|
| `UnitName` | `intc_1grp_no_msix_unit` | `intc_1grp_msix_unit` | name only |
| `Type` · `RegfileFlavor` · `InterfaceType` | `REGFILE` · `POSEDGE` · `APB` | `REGFILE` · `POSEDGE` · `APB` | identical |
| `AddrWidth` · `DataWidth` · `SizeInBytes` | `12` · `32` · `0x1000` | `12` · `32` · `0x1000` | **UNCHANGED** |
| `HalName` / `HalFilenameUnitName` / `Description` | `""` (all empty) | `""` (all empty) | identical |
| `Parameters` | `INTC_NUM_GROUPS=1` | `INTC_NUM_GROUPS=1`, `NUM_OF_TRIGS=128` | only `Value` |

Both `INTC_NUM_GROUPS` and `NUM_OF_TRIGS` are `NonOverridable="true"`.

> **QUIRK — fixed aperture, shrunk payload.** `AddrWidth=12` / `SizeInBytes=0x1000`
> are **held constant** even with one group active. A `1grp` unit reserves the *same*
> 4 KiB APB window as a `4grp` unit; the extra group/MSI-X address space is simply
> unpopulated (§3b). The address decode does not depend on `ngrp`. `[HIGH · OBSERVED]`

**Re-derived definition counts (machine-recounted from the JSON, not grepped from a
decompile) — identical to the 4grp counts because the *definition set* is identical;
only the runtime `ArraySize` expansion differs:** `[HIGH · OBSERVED]`

| | `1grp_no_msix` | `1grp_msix` |
|--|---------------|-------------|
| register DEFS | **16** | **18** (incl. the nested `VecTable/Val`) |
| field DEFS | **33** | **30** |
| top bundles | 2 (`ctrl`, `Sunda`) | 5 (`ctrl`, `Sunda`, `PBA`, `VecTable`, `MSIX_Vector_Table_Space`) + 1 nested |

---

## 2. What collapses at `ngrp=1` — the capacity model `[HIGH · OBSERVED]`

The single behavioural consequence of `INTC_NUM_GROUPS=1` is a **runtime `ArraySize`
contraction**. Every bundle whose `ArraySize` is the symbolic string `"INTC_NUM_GROUPS"`
collapses from 4 instances to 1; bundles with a fixed `ArraySize` do not move. Read
straight from the bundle arrays:

| bundle | `AddressOffset` | `ArraySize` (string) | at `ngrp=1` | at `ngrp=4` |
|--------|-----------------|----------------------|-------------|-------------|
| `ctrl` | `0x000` | `INTC_NUM_GROUPS` | **1 group** (0x000–0x03F) | 4 groups (0x000/0x40/0x80/0xC0) |
| `Sunda` | `0x300` | `1` (fixed) | 1 | 1 (unchanged) |
| `PBA` *(msix)* | `0x3F0` | `INTC_NUM_GROUPS` | **1 word** | 4 words |
| `VecTable` *(msix)* | `0x400` | `INTC_NUM_GROUPS` | **1 group** (32 entries) | 4 groups |
| `MSIX_Vector_Table_Space` *(msix)* | `0x800` | `NUM_OF_TRIGS` | **128 entries** *(does not shrink!)* | 128 entries |

So the **source capacity** is `INTC_NUM_GROUPS × 32`: a 1grp instance latches up to
**32 triggers** (one 32-bit cause word), vs 128 for a 4grp instance. The `ctrl` group
layout at `ngrp=1` is byte-identical to group 0 of the 4grp block (group base = 0, so
absolute offsets are unchanged): the 12 registers `int_cause_grp@0x00`,
`int_cause_set_grp@0x08`, `int_mask_grp@0x10`, `int_mask_clear_grp@0x18`,
`int_status_grp@0x20`, `int_cdc_bypass_grp@0x24`, `int_control_grp@0x28`,
`int_error_msk_grp@0x2C`, `int_abort_msk_grp@0x30`, `int_fatal_msk_grp@0x34`,
`int_log_msk_grp@0x38`, `int_posedge_grp@0x3C` — see [`intc-4group.md`](./intc-4group.md)
for the per-bit semantics; they are unchanged. With `ngrp=1` there is only group 0, so
trigger bit *b* in any `*_grp` register is global trigger index *b* (0..31).
`[HIGH · OBSERVED]`

The `int_control_grp@0x28` register keeps all **12** fields at `ngrp=1`
(`rev_id[29:28]` RO reset `0x1`, `Mod_res[27:24]`, `Mod_intv[23:16]`, `AWID[11:8]`,
`Chicken_addrhi[7]`, `Chicken_posedge[6]`, `Mask_msi_x[5]`, `Mod_rst[4]` WO,
`set_on_posedge[3]`, `auto_clear[2]`, `auto_mask[1]`, `clear_on_read[0]`) — contrast
the 9-field `ap_intc` variant in §4c.

---

## 3. The `no_msix` / `msix` split at 1 group `[HIGH · OBSERVED]`

The split is driven by the Mako `${type}` variable, so it is **identical to the 4grp
`no_msix`/`msix` delta** — only re-confirmed here at `ngrp=1`:

1. **`msix` adds the `NUM_OF_TRIGS="128"` parameter** (`NonOverridable`), and the
   four msix-only bundles `MSIX_TC` (in `Sunda`, abs `0x3E0`, `val[2:0]`), `PBA`,
   `VecTable`, `MSIX_Vector_Table_Space`.
2. **Two `ctrl` registers are demoted `RW → RO "Unused"` in `msix`** (template
   `% if type == 'no_msix'` branch):
   - `int_cdc_bypass_grp@0x24` — `no_msix`: RW (per-bit bypass of the trigger-input
     CDC edge-gen + tog2pul); `msix`: RO, `Description="Unused."`, reset `0x0`.
   - `int_abort_msk_grp@0x30` — `no_msix`: RW (`Abort = Wire-OR(Cause & !Abort_Mask)`,
     reset `0xffffffff`); `msix`: RO, `Description="Unused"`, reset `0xffffffff`.
   The other 10 `ctrl` registers are byte-identical across flavors.

### 3a. The 128-entry MSI-X over-provision (the 1grp-specific finding) `[HIGH · OBSERVED]`

`NUM_OF_TRIGS` is **hardcoded `"128"` in the Mako template** (`intc_ngrp_unit.json.mako`,
line 23, inside the `% if type == 'msix'` block) — it is **not** a function of `ngrp`.
The `MSIX_Vector_Table_Space` bundle is the *only* bundle sized by `NUM_OF_TRIGS`
(every other msix bundle scales by `INTC_NUM_GROUPS`). Therefore a `1grp_msix`
instance exposes a deeply **mismatched** surface:

```
control / cause / mask surface :  32 triggers   (1 group × 32)
VecTable                       :  32 entries     (1 group × 32; Timer_Settings + VMID_Settings per entry)
PBA (cause_access_image)       :  1 word         (1 group)
MSI-X vector table             :  128 ENTRIES    (NUM_OF_TRIGS, UNCHANGED)
```

So **96 of the 128 MSI-X table entries in a 1grp instance have no corresponding live
cause bit.** The MSI-X table is sized for the worst case — the bundle's own
`Description` documents the decode format `13'b1_VVVV_VVVV_WWBB` with *"VV = Vector
Select — up to 256 triggers"* — and left fixed regardless of group count.

> **GOTCHA — the table over-provision is a template constant, not a data bug.** The
> fact (128-entry table on a 32-trigger block) is `[HIGH · OBSERVED]`; whether this is
> deliberate worst-case sizing vs a template oversight is `[MED · INFERRED]` — the
> schema does not say. A reimplementer must size the `1grp_msix` MSI-X table at
> **128 entries** to be byte-compatible, even though only the first 32 are wired to
> cause bits. The PBA/VecTable, by contrast, *do* shrink to one group.

> **NOTE — no `0xb1` placeholder.** Unlike some sibling schemas, no reset uses the
> `0xb1` sentinel; the single `b1` hit in `1grp_msix` is the Verilog literal
> `13'b1_VVVV_VVVV_WWBB` inside the `MSIX_Vector_Table_Space` *Description* (an
> address-decode format), not a `ResetValue`. Resets are drawn from
> `{0, 0x0, 0x00000000, 1, 0x1, 0xffffffff}` only. `[HIGH · OBSERVED]`

### 3b. Aperture geometry at `ngrp=1` `[HIGH · OBSERVED]`

The aperture closes on `0x1000` **only** if `BundleSizeInBytes` strings are read in the
mixed base they are actually written in (the same hex/decimal mix as 4grp — see
[`intc-4group.md`](./intc-4group.md)): `ctrl="0x40"`, `Sunda="0xf0"` and
`VecTable="0x100"` are hex, but `PBA="4"`, `MSIX_Vector_Table_Space="16"` and
`VecTable/Val="8"` are **decimal**.

```
no_msix:  ctrl   0x000 + 1×0x40        = 0x040   (group 0 only)
          Sunda  0x300 + 1×0xf0        = 0x3F0   (tail 0x3F0..0xFFF unused)

msix:     ctrl       0x000..0x03F                (group 0 only)
          Sunda      MSIX_TC @0x3E0 ; bundle 0x300..0x3EF
          PBA        0x3F0 + 1×4(DEC)  = 0x3F4   (1 word @0x3F0)
          VecTable   0x400 + 1×0x100   = 0x500   (1 group, Val[32] @0x400..0x4FF)
          MSIX_VTS   0x800 + 128×16(DEC)= 0x1000 == SizeInBytes   ← decimal mandatory
                     (hex "16"=22 would overflow to 0x1300)
```

The MSI-X table is the **only** msix bundle that reaches the `0x1000` ceiling at
`ngrp=1` — precisely because it did not shrink. The `ctrl`/`PBA`/`VecTable` region now
carries large internal gaps that were populated in the 4grp instance. `[HIGH · OBSERVED]`

> **NOTE — only the `msix` flavor is instantiated at 1 group.** The Cayman address-map
> xref carries **4 `intc_1grp_msix_unit` instances and 0 `intc_1grp_no_msix_unit`**
> (and 1,070 `4grp_no_msix` + 858 `4grp_msix`, totalling the **1,932** physical INTC
> census of [`../interrupt/physical-intc-instances.md`](../interrupt/physical-intc-instances.md)
> and [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md)).
> The `1grp_no_msix` schema is shipped but never placed; the 4 `1grp_msix` units are
> the RDM-root host-leaf controllers. `[HIGH · OBSERVED]`

---

## 4. `ap_intc` (IOFIC) — the genuinely different block `[HIGH · OBSERVED]`

`ap_intc` is **not** a resized `intc`. `HalName "iofic"` = *I/O Fabric Interrupt
Controller* (the canonical Annapurna-Labs / Alpine block name) — a hard string that
confirms the [`intc-4group.md`](./intc-4group.md) lineage inference. It is a leaner,
**MEM-mapped** controller reused as the interrupt-group layout *inside the PMDT*
(Performance-Monitoring / Debug-Trace) complex, not a chip-fabric APB aggregator.

### 4a. The three wrapper units `[HIGH · OBSERVED]`

`ap_intc_{1,2,4}grp_unit.json` are 963 B each, **identical except three lines**
(`HalName`, `INTC_NUM_GROUPS.Value`, `UnitName`):

| unit | `HalName` | `INTC_NUM_GROUPS` | at that width |
|------|-----------|-------------------|---------------|
| `ap_intc_1grp_unit` | `iofic_x1` | 1 | `SizeInBytes = 0x40` |
| `ap_intc_2grp_unit` | `iofic_x2` | 2 | `SizeInBytes = 0x80` |
| `ap_intc_4grp_unit` | `iofic_x4` | 4 | `SizeInBytes = 0x100` |

The wrapper has `InterfaceType = MEM` (not APB), an **empty `RegistersBundleArrays`**,
and pulls the body via an `Includes` entry. Its sizing fields are **unevaluated
symbolic expression strings**, not literals — a downstream generator resolves them once
`INTC_NUM_GROUPS` is bound:

```
"InterfaceType" : "MEM"
"AddrWidth"     : "log2(0x40*INTC_NUM_GROUPS)"      ← string, = 6 at ngrp=1
"SizeInBytes"   : "0x40*INTC_NUM_GROUPS"            ← string, = 0x40 at ngrp=1
"RegistersBundleArrays" : []
"Includes" : [ { "FileName":"ap_intc_grp_ctrl.json", "Name":"ctrl",
                 "AddressOffset":"0x0", "InclSizeInBytes":"0x40",
                 "ArraySize":"INTC_NUM_GROUPS", "InclType":"DIRECT" } ]
```

`[HIGH · OBSERVED]` for the strings; `[MED · INFERRED]` that the consumer evaluates
`log2(…)` / multiplies — the schema ships them as text.

> **NOTE — Maverick adds a fourth wrapper.** The MAVERICK `ap_intc/` dir adds
> `ap_intc_8grp_msix_unit.json` (`HalName "iofic_x8_msix"`, `Type "NODE"`,
> `SizeInBytes "0x2000"`) — the 256-input, MSI-X-capable security IOFIC absent on
> Cayman. Cross-referenced in [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md).
> `[HIGH · OBSERVED header]`

### 4b. The body `ap_intc_grp_ctrl.json` — 9 registers `[HIGH · OBSERVED]`

`UnitName ap_intc_grp_ctrl`, `HalName "iofic_grp_ctrl"`, `InterfaceType NONE`,
`AddrWidth 6`, `SizeInBytes 0x40`. One bundle `grp_ctrl` (base `0x0`, `ArraySize=1`,
`BundleSizeInBytes="0x40"` hex, `GenFlavor NORMAL`). **Nine** registers:

| rel | register | regacc | field reset | vs APB `intc` group |
|-----|----------|--------|-------------|---------------------|
| `0x00` | `int_cause_grp` | RW | `0` | same |
| `0x08` | `int_cause_set_grp` | WO | `0` | same |
| `0x10` | `int_mask_grp` | RW | **`0`** | **reset CHANGED** (APB intc = `0xffffffff`) |
| `0x18` | `int_mask_clear_grp` | WO | `0` | same |
| `0x20` | `int_status_grp` | RO | `0` | same |
| `0x28` | `int_control_grp` | RW | mixed | **9 fields** (vs 12) |
| `0x30` | `int_abort_msk_grp` | RW | `0xffffffff` | same |
| `0x34` | `int_fatal_msk_grp` | RW | `0xffffffff` | same |
| `0x38` | `int_log_msk_grp` | RW | `0xffffffff` | same |

Re-derived counts: register DEFS **9**, field DEFS **17**, field-`AccessType`
{RW 13, WO 3, RO 1}, `ResetValue` dist `{0:14, 0xffffffff:3}`, `SpecialAccess` all
`None`, single `BundleSizeInBytes="0x40"`. No `0xb1` placeholder, **no MSI-X table /
PBA / VecTable, no `Sunda`, no `int_posedge_grp`, no `int_cdc_bypass_grp`, no
`int_error_msk_grp`.** `[HIGH · OBSERVED]`

### 4c. `ap_intc` vs one group of the APB `intc` — the precise diff `[HIGH · OBSERVED]`

| dimension | the divergence |
|-----------|----------------|
| registers **removed** (3) | `int_cdc_bypass_grp@0x24`, `int_error_msk_grp@0x2C`, `int_posedge_grp@0x3C` |
| `int_control_grp` bits **removed** (3) | `rev_id[29:28]`, `Chicken_addrhi[7]`, `Chicken_posedge[6]` → **9 fields**, not 12 |
| `int_mask_grp` reset | `0xffffffff` (all masked) → **`0` (all UNMASKED)** |
| severity wire-ORs | keeps `abort`/`fatal`/`log` (3); **drops `error`** (no `int_error_msk_grp`) |
| interface / sizing | **MEM** wrapper / **NONE** body; symbolic `Size`/`AddrWidth` |

The surviving `int_control_grp` fields are `Mod_res[27:24]`, `Mod_intv[23:16]`,
`AWID[11:8]`, `Mask_msi_x[5]`, `Mod_rst[4]` WO, `set_on_posedge[3]`, `auto_clear[2]`,
`auto_mask[1]`, `clear_on_read[0]`. The cause / cause_set / mask / mask_clear / status
registers and their **W0C-cause / W1S-cause-set / W0C-mask-clear** polarity are
byte-identical to the APB `intc` — see [`intc-4group.md`](./intc-4group.md).

> **QUIRK — `ap_intc` boots UNMASKED.** `int_mask_grp` resets to `0` in `ap_intc`,
> the **opposite** of the APB `intc`'s safe-by-default `0xffffffff`. An `ap_intc` group
> comes out of reset with **all 32 sources unmasked**, so firmware **must mask before
> enabling sources** — the reverse contract from the APB INTC. (The `abort`/`fatal`/`log`
> masks *do* reset to `0xffffffff` in both — severity *routing* is masked at reset
> regardless.) For a reimplementer this is the single most consequential `ap_intc`
> divergence. `[HIGH · OBSERVED]`

> **NOTE — why the three control bits vanish.** `[MED · INFERRED]` No `rev_id`: it is
> an *included* sub-block, not a standalone versioned unit. No `Chicken_posedge`: there
> is no `int_posedge_grp` register to fall back to (per-bit posedge is gone), though
> `set_on_posedge[3]` survives so **whole-group posedge mode is still configurable**.
> No `Chicken_addrhi`: the same-domain MEM interface needs no legacy high-address
> chicken bit.

### 4d. The PMDT include fragment `[HIGH · OBSERVED]`

`pmdtu/ap_intc_grp_ctrl.json.inc` (14,741 B) is a Mako **include fragment** — the
`"Registers":[…]` body only, no `RegFile` wrapper. Its 9 registers are
byte-semantically identical (same names, same order: `int_cause_grp`,
`int_cause_set_grp`, `int_mask_grp`, `int_mask_clear_grp`, `int_status_grp`,
`int_control_grp`, `int_abort_msk_grp`, `int_fatal_msk_grp`, `int_log_msk_grp`) to the
standalone `ap_intc_grp_ctrl.json`. It exists so the PMDT regfiles can
`<%include%>` the IOFIC group body inline (§5).

---

## 5. What `ap_intc` serves — PMU-local + CCTM-global `[HIGH · OBSERVED]`

The standalone `ap_intc_{1,2,4}grp_unit.json` files have **zero instances** in the
address-map xref (the APB intc has 1,070 + 858 + 4; `ap_intc` has none). The live use
is the **PMDT include**: the `ap_intc_grp_ctrl` body is `<%include%>`'d into the
PMU-local and CCTM-global regfiles. `[HIGH · OBSERVED]`

| consumer | bundle | base | `ArraySize` | `Description` (verbatim) | role |
|----------|--------|------|-------------|--------------------------|------|
| `pmdt_pmu.json.inc` | `interrupt_ctl` | `0x80` | **2** | *"Local interrupt controller. Group A presents the events violations, while Group B presents errors assembled from the local complex."* | per-tile PMU: A = event/threshold violations, B = local-complex errors |
| `pmdt_cctm.json.mako` | `interrupt_ctl0` | `0x000` | **4** | *"Global Interrupt Controller0"* | CCTM central aggregator, bank 0 |
| `pmdt_cctm.json.mako` | `interrupt_ctl1` | `0x400` | **2** | *"Global Interrupt Controller1"* | CCTM central aggregator, bank 1 |

So the **same 9-register IOFIC body** is the aggregation layout for **both** the per-tile
PMU (a *local* 2-group controller) and the chip-central CCTM (the cross-tile
timestamp/event master, a *global* 4-group + 2-group controller). Source capacity:

```
PMU local   :  2 × 32 = 64 sources
CCTM global :  (4 + 2) × 32 = 192 sources across its two banks
```

`[HIGH · OBSERVED]` for the include + `ArraySize`; `[MED · INFERRED]` that every cause
bit is populated, and `[MED · INFERRED]` that the standalone wrappers are
catalog/HAL-only (the address map shows 0, but a generator the xref does not cover
could place them).

> **NOTE — `ap_intc` is "application-processor / access-port" view, not host-facing.**
> The PMDT complex monitors and time-correlates the GPSIMD/Neuron tile array (PMU per
> tile, CCTM central). `ap_intc` therefore serves the **on-die profiling/debug
> infrastructure** and the local-complex error path — a same-clock MEM-mapped
> aggregator, NOT a host-facing or chip-fabric APB INTC. `[HIGH PMU/CCTM binding ·
> MED "ap" gloss]` (corroborated by `HalName "iofic"` + MEM interface + symbolic sizing.)

> **MSI-X in `ap_intc`.** `ap_intc` has **no** per-vector MSI-X table / PBA / VecTable,
> but it *keeps* the MSI-X **control** bits (`Mask_msi_x`, `auto_clear`, `auto_mask`,
> `AWID`) in `int_control_grp`. This implies it can drive an MSI-X write through a
> *shared/external* MSI-X engine (`AWID` selects the AXI write-ID) but does not own a
> per-vector table — the opposite of `intc_msix`, which owns the full table.
> `[MED · INFERRED]` from the surviving control bits + the absent table.

---

## 6. The cascade — leaf → aggregator → apex → Q7/GIC

Assembled from the `ap_intc` severity wire-ORs (§4), the trigger-YAML cascade
fingerprints, and the `peb_intc` apex source paths. Per-hop confidence marked.

```
[block events]  +  [PMU/CCTM events]
      |                   |
      |        L1a  ap_intc / IOFIC  (PMDT: PMU-local 2grp + CCTM-global 4grp+2grp)
      |             latches 32b/group cause → abort/fatal/log wire-ORs
      |             (NO error line; int_mask reset = UNMASKED)
      |             emits  "pmdtu_to_cctm[*].local_abort" + "pmuc_interrupt"
      v                                                          |
L1b  intc_1grp / intc_4grp  (per-domain APB leaf)  <-------------+
      |  msix  → MSI-X straight to PCIe host (host leaf)
      |  no_msix → 4 severity wire-ORs (error/abort/fatal/log) + Mask_msi_x summary
      |                                                          |
      +---- (host)                                               v
                                              L2  peb_intc  (128-input apex)
                                                   aggregates leaf "*_nmi"/"*_summary"
                                                          |
                                              L3  Q7 / GIC IRQ   [INFERRED]
```

- **L1a — `ap_intc` leaf outputs.** The `int_abort_msk_grp` / `int_fatal_msk_grp` /
  `int_log_msk_grp` wire-ORs (`Severity = OR(Cause & !Mask)`) are the propagating
  outputs. `io_fabric_triggers.yaml` carries the matching **(commented-out)** input
  names — the PMDT→fabric cascade fingerprint:
  ```
  #- trigger: pmdtu_to_cctm[127].local_abort   # name block_abort_127, "block abort indication"
  #- trigger: pmuc_interrupt                    # "CCTM signalled interrupt"
  ```
  `[HIGH · OBSERVED that the names exist; MED · INFERRED that these are this block's
  outputs — they are commented template lines, design-intent evidence, not a live
  binding.]`

- **L1b — APB INTC leaf trigger sources.** The `io_fabric_triggers.yaml` active set
  (243 triggers) is the source vocabulary that latches into the io-fabric APB INTC
  units: `intc_top_retrigger[0..127]` (128, *"re-triggers from axi-write to trigger
  mail box"*), `fis_errtrig_intr[*]` (50, the FIS errtrig summaries),
  `intc_notific_intr[*]` (25, dropped-notification requests), `fis_sprot_intr[*]` (12),
  `fis_cntrl_intr[*]` (5), plus the cfgbus-master / `iofab_axi2apb` timeout and
  posted-write-no-acc interrupts. The SDMA source set
  ([`../interrupt/io-fabric-triggers.md`](../interrupt/io-fabric-triggers.md),
  `sdma_triggers.yaml`, 254 triggers) leads with `notific_intr_0..` queue-drop causes;
  the PCIe set (`pcie_triggers.yaml`, 228) and HBM/D2D/TPB sets
  ([`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md),
  223/216/216) feed their per-domain leaves. `[HIGH · OBSERVED trigger names · MED ·
  INFERRED exact bit-map]`

- **L2 — `peb_intc` apex.** `peb_intc_triggers.yaml` has exactly **128 active
  triggers** (the apex 128-input), each with a `source_path` to a leaf `nmi_out` wire
  and `nmi_mask` / `nmi_msix_mask` fields. Verbatim:
  ```
  pcie_m0_nmi  ← pcie_x8_sprot.ERRTRIG_GEN.u_errtrig.u_amzn_errtrig_prot.nmi_out
  pcie_a0_nmi  ← pcie_nmi_out
  se0_tpb_nmi[0..]  ← per-tile TPB errtrig summaries
  hbm_0_nmi    ← hbm errtrig summary
  ```
  `peb_intc` is the on-die summary-tree root; its inputs are the leaf INTC `nmi_out`
  severity wires. `[HIGH · OBSERVED]`

- **L3 — delivery to Q7/GIC.** Not register-encoded → `[MED · INFERRED]`. The exact
  `peb_intc → Q7/GIC` vector map is not in the shipped schema. Corroboration that a GIC
  *exists*: the CXELA500 ELA outputs (`core_ela_output_*`, `pcie_triggers.yaml` ~L1426)
  are documented verbatim to *"Connect to ELAOUTPUT signals of CXELA500 module.
  Intended to be connected to any external device, for example a Generic Interrupt
  Controller (GIC) as an Interrupt Request (IRQ) input …"*. `[HIGH GIC exists · MED
  peb→Q7 · LOW exact vectors]`

---

## 7. Cross-generation — Cayman authoritative, Maverick = security fork `[HIGH · OBSERVED]`

| block | Cayman | Mariana | Mariana+ | Sunda | Maverick |
|-------|--------|---------|----------|-------|----------|
| `intc_1grp_no_msix` | **AUTH** | == | == | == (1 desc-text expansion\*) | *(absent)* |
| `intc_1grp_msix` | **AUTH** | == | == | == | *(absent)* |
| `ap_intc_grp_ctrl` | **AUTH** (9 reg, MEM/NONE) | == | == | == | **SEC-FORK** (12 reg, APB) |
| `ap_intc_{1,2,4}grp` | **AUTH** (MEM, `iofic_x{1,2,4}`) | == | == | == | + `iofic_x8_msix` |

\* Sunda's only `intc_1grp` delta is one `Description`-text expansion (the
`local_abort_to_block_level_logic` field adds *"…also forces SRAM to write protect
mode"*) — structure (name/offset/access/position/reset) is identical across all four
gens. **Maverick ships no `intc_1grp`** (only `intc_4grp_{no_msix,msix}`), so the
1-group APB INTC is a Cayman/Mariana/Mariana+/Sunda construct; **Cayman is
authoritative**.

**The Maverick `ap_intc_grp_ctrl` security fork** (header-OBSERVED; v5 interior
`[INFERRED]`) grows the 9-register Cayman body to **12 registers** and becomes
**APB-interfaced**:

- **ADDED** `int_sec_grp@0xC` (RW, `int_sec[31:0]`, reset `0`) — the `SecureReg "SWOM"`
  apparatus; the cause register's verbatim note reads *"SWOM with `int_sec_grp` as
  mask … Available from V3"*.
- **ADDED** `int_regs_sec_grp@0x24` (RW, 4 per-register **write-lock** fields:
  `mask[0]`, `err_mask[1]`, `fatal_mask[2]`, `ctrl[31]`).
- **RESTORED** `int_error_msk_grp@0x2C` (present in Maverick, absent in Cayman `ap_intc`).
- `int_control_grp` grows to **14 fields**, `rev_id[29:28]` reset = **`2`** (Cayman APB
  intc had `rev_id=1`; Cayman `ap_intc` had *no* `rev_id`); bracketed `[..]` positions.
- `int_mask_grp` reset stays **`0`** (UNMASKED — same as Cayman `ap_intc`).

This is a SWOM (secure-write-one-mask) hardening: `int_sec_grp` gates which cause bits
a non-secure master may set, `int_regs_sec_grp` write-protects the mask/severity/control
registers behind a security context. It does **not** change the cause/status/W0C/W1S
core semantics. `[HIGH · OBSERVED header structure · MED · INFERRED v5 silicon behaviour]`

> **CORRECTION — consistency check vs [`pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) (#908).**
> No divergence found. This page and #908 agree byte-for-byte on the Maverick fork:
> `int_sec_grp@0xC` SWOM + `int_regs_sec_grp@0x24` write-locks, `InterfaceType=APB`,
> the *"Available from V3"* string, `iofic_x8_msix` (Type `NODE`, `0x2000`), and the
> Cayman physical-INTC census **1,932 = 1,070 `4grp_no_msix` + 858 `4grp_msix` + 4
> `1grp_msix`**. #908 reads these out of the Maverick `al_address_map_db.pkl` /
> schema dir; this page reads the same fields out of the Cayman + Maverick `csrs/`
> JSONs. Counts and naming are kept identical (no edit to #908 required).

---

## 8. Reimplementer's checklist `[HIGH · OBSERVED unless noted]`

1. `intc_1grp` is the 4grp APB INTC with `INTC_NUM_GROUPS=1`. Hold the aperture at
   `0x1000` / `AddrWidth=12`; populate only group 0 (`ctrl` 0x000–0x03F, `Sunda` from
   0x300). Source capacity = 32 triggers.
2. For `1grp_msix`, size the MSI-X vector table at **128 entries** (`NUM_OF_TRIGS`,
   hardcoded) even though only 32 cause bits are live; `PBA`/`VecTable` shrink to one
   group. Aperture closes on `0x1000` only with **decimal** `BundleSizeInBytes` reads.
3. Only the **`msix`** flavor is placed at 1 group (4 RDM-root instances); `1grp_no_msix`
   is shipped-but-unplaced.
4. `ap_intc` (IOFIC) is a **separate** 9-register MEM block — *not* a resized `intc`.
   Drop `int_cdc_bypass_grp` / `int_error_msk_grp` / `int_posedge_grp` and the
   `rev_id` / `Chicken_addrhi` / `Chicken_posedge` control bits; keep only
   `abort`/`fatal`/`log` severity (no `error`).
5. **Reset `ap_intc`'s `int_mask_grp` to `0` (UNMASKED)** — the reverse of the APB INTC.
   Mask before enabling sources. Reset `abort`/`fatal`/`log` masks to `0xffffffff`.
6. Instantiate `ap_intc` via the PMDT include: PMU-local `interrupt_ctl` (ArraySize 2,
   A=violations / B=complex-errors), CCTM-global `interrupt_ctl0` (4) + `interrupt_ctl1`
   (2). The standalone `iofic_x{1,2,4}` wrappers are catalog/HAL entries `[MED]`.
7. Wire the `ap_intc` `abort`/`fatal`/`log` wire-ORs and `pmuc_interrupt` into the
   io-fabric APB INTC trigger inputs; route the no_msix APB leaf `nmi_out` summaries to
   `peb_intc` (128-input apex); the apex → Q7/GIC hop is `[INFERRED]`.
8. For a v5 target, add the Maverick SWOM layer (`int_sec_grp`, `int_regs_sec_grp`,
   restored `int_error_msk_grp`, 14-field control, `rev_id=2`, APB) — see #908.

---

## 9. Confidence ledger

**`[HIGH · OBSERVED]`** — the 2-line `intc_1grp`↔`intc_4grp` diff (both flavors); the
definition counts (16/18 regs, 33/30 fields); the `ngrp`-collapse bundle table
(`ctrl`/`PBA`/`VecTable` by `INTC_NUM_GROUPS`, `MSIX_VTS` by `NUM_OF_TRIGS=128`); the
hardcoded `128` at Mako line 23; the aperture closure on decimal `BundleSizeInBytes`;
the address-map census (1,070 / 858 / 4 / 0); the `ap_intc` 9-register body, its
removed-3-registers / removed-3-bits diff, and the `int_mask_grp` reset = `0`; the
three wrapper `iofic_x{1,2,4}` HalNames + symbolic sizing; the PMDT include
(`interrupt_ctl` ArraySize 2 / CCTM 4 + 2) with verbatim descriptions; the trigger-YAML
source families and the 128-input `peb_intc` apex with `nmi_out` source paths; the
Maverick header structure (12 reg, APB, `int_sec_grp`/`int_regs_sec_grp`, `rev_id=2`,
*"Available from V3"*, `iofic_x8_msix`); the cross-gen Cayman==Mariana==Mariana+==Sunda
equivalence.

**`[MED · INFERRED]`** — that the 128-entry MSI-X table is intentional worst-case
sizing vs a template oversight; that the symbolic wrapper sizing expressions are
generator-evaluated; that the commented `pmdtu_to_cctm`/`pmuc_interrupt` names are this
block's live outputs (they are template comments); that the standalone `ap_intc`
wrappers are catalog-only; the `ap_intc`-drives-shared-MSI-X-engine reading; the v5
silicon behaviour behind the Maverick fork's added registers.

**`[LOW · OPEN]`** — the exact per-bit source-name → (instance, group, bit) map for any
of these blocks (not in the schema; the PMU "Group A = violations / Group B = errors"
split is the only explicit group→meaning statement found); the final `peb_intc → Q7/GIC`
vector assignment (a GIC is confirmed to exist via the CXELA500 ELA description, but the
vector map is not shipped).
