# CSR — INTC 4-Group (no_msix + msix)

This page reconstructs the **4-group interrupt-controller hardware unit** — the two
register-file variants `intc_4grp_no_msix_unit` and `intc_4grp_msix_unit` — from their
shipped Annapurna-Labs register-description schemas in the `cayman-arch-regs` CSR set.
This is the INTC block that the per-domain trigger sets (SDMA, IO-fabric, PCIe, TPB, HBM,
D2D, SP…) **latch into**: a small APB-mapped surface that captures, masks, moderates,
classifies and routes a flat bus of up to **128 hardware trigger inputs** into four 32-bit
interrupt *groups*. Both variants are emitted from one Mako template,
`intc_ngrp_unit.json.mako`, whose `UnitName = intc_${ngrp}grp_${type}_unit` parametrically
generates the `ngrp ∈ {1,4}` × `type ∈ {no_msix, msix}` matrix.

The *sources* that feed this block's trigger bus are enumerated in
[`../interrupt/io-fabric-triggers.md`](../interrupt/io-fabric-triggers.md) and the
schema-atlas; the *physical instance* census (which IP block instantiates which flavor) is
[`../interrupt/physical-intc-instances.md`](../interrupt/physical-intc-instances.md) and
[`../interrupt/schema-atlas.md`](../interrupt/schema-atlas.md); the **1-group / `ap_intc`
(IOFIC)** sibling is [`intc-1group-apintc.md`](intc-1group-apintc.md); the error-trigger
PAIR routing that pins two of these units to every errtrig generator is
[`../interrupt/errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md); the upward
summary tree is [`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md); and
the security-hardened, decentralized v5 view of this same fleet is in
[`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md).

> **PROVENANCE.** Every offset, bit-range, access type, reset value and verbatim
> description below was read byte-exact from the two shipped schema JSONs
> (`intc_4grp_no_msix_unit.json` md5 `d3e509688793b1a320b9e988dc77484d`;
> `intc_4grp_msix_unit.json` md5 `f6521dfefedd220dc971ca5b16277590`) and their RTL-generator
> Mako template — all RTL-derived descriptor artifacts, freely citeable. The page default
> is `[HIGH · OBSERVED]` = literal value read from the JSON/Mako; `[* · INFERRED]` = a
> semantic reconstruction; the per-bit→group ordering (`g*32+b`) and the group→domain cut
> are flagged INFERRED throughout — they are **not** encoded in the intc schema itself.
> The Cayman/NC-v3 schema is **structurally identical** to the Mariana / Mariana+ / Sunda /
> Maverick(v5) copies of these two files (full recursive name/offset/access/ArraySize diff);
> v5-interior *behavior* (what the silicon does behind an address) is INFERRED. The `Sunda`
> bundle name is a **template constant present in every generation**, not a per-SoC codename.

---

## 0. The unit at a glance

The INTC is an Alpine/AL-lineage interrupt aggregator. A 4 KiB (`0x1000`) APB aperture, 12-bit
address, 32-bit word-addressed data, `RegfileFlavor=POSEDGE`. The two variants share an
identical 4-group control core and diverge **only** in: (a) one extra `Parameter`, (b) two
control registers demoted to read-only, and (c) the entire `0x300+` tail — the on-die abort
machinery vs the host-facing PCIe MSI-X apparatus.

| Field | `no_msix` | `msix` |
|---|---|---|
| `UnitName` | `intc_4grp_no_msix_unit` | `intc_4grp_msix_unit` |
| `Type` / `RegfileFlavor` | `REGFILE` / `POSEDGE` | `REGFILE` / `POSEDGE` |
| `InterfaceType` | `APB` | `APB` |
| `AddrWidth` / `DataWidth` | 12 / 32 | 12 / 32 |
| `SizeInBytes` | `0x1000` (4 KiB) | `0x1000` (4 KiB) |
| `Parameters` | `INTC_NUM_GROUPS=4` | `INTC_NUM_GROUPS=4`, **`NUM_OF_TRIGS=128`** |
| `Memories` / `Includes` | `[]` / `[]` | `[]` / `[]` |
| top bundles | 2 (`ctrl`, `Sunda`) | 5 (`ctrl`, `Sunda`, `PBA`, `VecTable`, `MSIX_Vector_Table_Space`) + 1 nested (`VecTable/Val`) |
| register DEFS | 16 (ctrl 12 + Sunda 4) | 16 top + 2 nested |
| bitfield DEFS | 33 (ctrl 23 + Sunda 10) | 30 (recursive) |

All parameters are `NonOverridable=true`. `HalName` and `Description` are empty strings in
both. Every count above is read directly from the JSON.

The address space is sparse in both flavors:

```
no_msix:  ctrl 0x000–0x0FF | gap 0x100–0x2FF | Sunda 0x300–0x3EF | tail unused → 0xFFF
msix:     ctrl 0x000–0x0FF | gap 0x100–0x2FF | Sunda 0x300–0x3EF |
          PBA  0x3F0–0x3FF | VecTable 0x400–0x7FF | MSIX_VTS 0x800–0xFFF
```

The 512-byte `0x100–0x2FF` reserve is present in **both** variants; the
MSI-X table does **not** reuse it — the msix table instead starts at `0x400` (`reason MED ·
INFERRED`).

---

## 1. Capacity / stride model

The whole capacity model falls out of one bundle. `ctrl` is a **`RegistersBundleArray`** with
`ArraySize = INTC_NUM_GROUPS = 4` and `BundleSizeInBytes = 0x40`. Indexing the array steps the
base by `0x40`, so the four groups are byte-exact at:

```
group 0 → 0x000    group 1 → 0x040    group 2 → 0x080    group 3 → 0x0C0
```

Each group is an **identical 12-register per-group "unit"** of 32 cause bits. Therefore:

> **CAPACITY.** `4 groups × 32 bits = 128 trigger inputs per intc instance.`
> Corroborated three independent ways: (i) `NUM_OF_TRIGS=128` parameter in the msix variant;
> (ii) `MSIX_Vector_Table_Space.ArraySize = NUM_OF_TRIGS = 128` (one MSI-X entry per trigger);
> (iii) `PBA.ArraySize = 4` × 32 bits = a 128-bit pending image. The per-bit global index is
> taken as **`global_trigger = g*32 + b`** for bit `b` of group `g` `[arithmetic HIGH ·
> ordering INFERRED]` — the schema stores anonymous 32-bit words and never names a bit, so
> the `g*32+b` packing is inferred from the PBA "one word per group" layout and the
> per-trigger MSI-X table (§6).

### Per-group register bank (relative offset; absolute = `g*0x40 + rel`)

The 0x40 block is laid out on a deliberate 8-byte stride for the cause/set and mask/clear
*pairs* (each live word has a shadow word), then 4-byte spacing for the control / severity
block from `0x24` upward:

| rel | register | reg acc | field(s) | field acc | reset |
|---|---|---|---|---|---|
| `0x00` | `int_cause_grp` | RW | `val[31:0]` | RW | `0x0` |
| `0x08` | `int_cause_set_grp` | WO | `int_cs_set[31:0]` | WO | `0x0` |
| `0x10` | `int_mask_grp` | RW | `int_msk[31:0]` | RW | `0xffffffff` |
| `0x18` | `int_mask_clear_grp` | WO | `int_msk_clr[31:0]` | WO | `0x0` |
| `0x20` | `int_status_grp` | RO | `int_sts[31:0]` | RO | `0x0` |
| `0x24` | `int_cdc_bypass_grp` | RW † | `int_cdc_bypass[31:0]` | RW † | `0x0` |
| `0x28` | `int_control_grp` | RW | 12 control fields (§3) | mixed | mixed |
| `0x2C` | `int_error_msk_grp` | RW | `int_error_msk[31:0]` | RW | `0xffffffff` |
| `0x30` | `int_abort_msk_grp` | RW † | `int_abort_msk[31:0]` | RW † | `0xffffffff` |
| `0x34` | `int_fatal_msk_grp` | RW | `int_fatal_msk[31:0]` | RW | `0xffffffff` |
| `0x38` | `int_log_msk_grp` | RW | `int_log_msk[31:0]` | RW | `0xffffffff` |
| `0x3C` | `int_posedge_grp` | RW | `int_posedge[31:0]` | RW | `0x00000000` |

> † `int_cdc_bypass_grp` and `int_abort_msk_grp` are RW in `no_msix` but **demoted to RO
> "Unused"** in `msix` — see §2.

The word slots `0x04`, `0x0C`, `0x14`, `0x1C` and `0x21–0x23` are absent (the cause/set and
mask/clear words sit 8 apart, the rest on 4-byte centres) — the canonical Annapurna INTC
group layout `[layout HIGH · spacing-reason MED]`.

> **QUIRK — `BundleSizeInBytes` is mixed hex/decimal in the same file.** A reimplementor
> parsing the geometry **must not** assume one radix. Hex strings: `ctrl="0x40"`,
> `Sunda="0xf0"`, `VecTable="0x100"`. **Decimal** strings: `PBA="4"`,
> `MSIX_Vector_Table_Space="16"`, `VecTable/Val="8"`. Only the decimal reading closes the
> aperture: `0x800 + 128 × 16(dec) = 0x1000` (== `SizeInBytes`), whereas `16` read as hex
> (`0x16=22`) gives `0x800 + 128×22 = 0x1300`, a 768-byte overflow. Likewise the nested
> `VecTable/Val` span is `32 × 8(dec) = 0x100`, exactly the `VecTable` `BundleSizeInBytes`
> of `0x100`. **Parse `"16"` and `"8"` as decimal; parse `0x`-prefixed strings as hex.**

> **NOTE — no `0xb1` placeholder reset.** The CSR-lane placeholder-reset artifact seen in
> other units is **absent** here. Every `ResetValue` is drawn from `{0, 0x0, 0x00000000, 0x1,
> 1, 0xffffffff}`. The only `b1` substring in either file is the Verilog literal
> `13'b1_VVVV_VVVV_WWBB` inside the MSI-X table address-decode description (§6) — not a reset.

---

## 2. `no_msix` vs `msix` — the exact delta

The entire variant split reduces to four byte-level changes. Everything else is shared
byte-for-byte (the 10 unchanged ctrl registers — cause / cause_set / mask / mask_clear /
status / control / error_msk / fatal_msk / log_msk / posedge — are offset/access/reset/desc
identical).

**(a) Parameters.** `msix` adds `NUM_OF_TRIGS = "128"` (`NonOverridable`). `no_msix` carries
only `INTC_NUM_GROUPS`.

**(b) Two `ctrl` registers demoted RW → RO.** These are the *only* two registers inside the
group block that differ:

| reg @rel | `no_msix` | `msix` |
|---|---|---|
| `int_cdc_bypass_grp` @`0x24` | RW; *"Each bit … causes the corresponding trigger input CDC edge-gen and tog2pul to be bypassed. The CDC syncro is still in the path regardless."* field RW, reset `0x0` | **RO; desc `"Unused."`** field RO, reset `0x0` |
| `int_abort_msk_grp` @`0x30` | RW; *"… masks the corresponding cause bit for generating an Abort signal … (Abort = Wire-OR of Cause & !Interrupt_Abort_Mask)"* field RW, reset `0xffffffff` | **RO; desc `"Unused"`** field RO, reset `0xffffffff` (frozen) |

> **INTERPRETATION** `[MED · INFERRED]`: in the host-facing `msix` flavor the on-die *abort*
> wire-OR and the same-clock CDC-bypass are not exposed (the host receives MSI-X, not the
> on-die abort wire), so both registers are tied off RO. In the on-die `no_msix` flavor they
> are live: that block participates in the **local abort / clock-freeze** logic (the `Sunda`
> `abort_cntl` registers, §4) and may bypass CDC for sources already in the INTC clock domain.

**(c) `msix` adds three whole top-level bundles** (absent entirely in `no_msix`):

| bundle | base | ArraySize | BundleSize | role |
|---|---|---|---|---|
| `PBA` | `0x3F0` | `INTC_NUM_GROUPS` (=4) | `4` (dec) | MSI-X Pending-Bit-Array: `cause_access_image`, one RO 32-bit word per group |
| `VecTable` | `0x400` | `INTC_NUM_GROUPS` (=4) | `0x100` | per-group vector table; nests `Val[32]` × `{Timer, VMID}` |
| `MSIX_Vector_Table_Space` | `0x800` | `NUM_OF_TRIGS` (=128) | `16` (dec) | standard PCIe MSI-X table, one 16-byte entry per trigger |

**(d) The `Sunda` bundle is swapped, same base/name.** `Sunda @0x300`, `ArraySize=1`,
`BundleSizeInBytes=0xf0` in both, but its contents change:

- `no_msix` → 4 registers: `abort_cntl0`, `abort_cntl1`, `spare0`, `spare1` (the
  abort / clock-freeze / SRAM-write-protect controls + spares — §4).
- `msix` → 1 register: `MSIX_TC @ rel 0xE0` (abs `0x3E0`), field `val[2:0]` RW reset `0` —
  *"TC to be used by PCIe controller for MSI-X writes."* (the 3-bit PCIe traffic class, 0–7).

> **Net:** in `msix`, on-die abort handling is dropped from `Sunda` (it moves host-side) and a
> single PCIe-TC register takes the slot. Access-type census reflects this: `no_msix`
> register access is `RW:13 / WO:2 / RO:1`; the field-level recursive census of `msix` is
> `RW:22 / WO:3 / RO:5` (the extra RO from the two demoted ctrl regs, the RO `cause_access_image`,
> and the RO `int_status` — all `SpecialAccess: None`).

---

## 3. `int_control_grp` — the per-group control word (rel `0x28`)

Identical in both variants. 12 bitfields. Absolute address = `g*0x40 + 0x28`. Semantics are
the descriptor's own embedded strings, condensed.

| field | pos | acc | rst | semantics |
|---|---|---|---|---|
| `rev_id` | `29:28` | RO | `0x1` | INTC revision constant (= 1). |
| `Mod_res` | `27:24` | RW | `0` | Moderation-timer clock divider: decrement every `(N+1)×256` SB-clk (≈ µs). |
| `Mod_intv` | `23:16` | RW | `0` | Interrupt moderation interval; **writing 0 disables** moderation. |
| `AWID` | `11:8` | RW | `0` | MSI-X AXI write-ID; same ID for all cause bits in the group. |
| `Chicken_addrhi` | `7` | RW | `0` | Revert to the old single high/msb_low address scheme. |
| `Chicken_posedge` | `6` | RW | `0` | Revert to **whole-group** posedge enable instead of per-bit (`int_posedge_grp`). |
| `Mask_msi_x` | `5` | RW | `0` | `1` ⇒ no MSI-X sent from this group; set when a single summary MSI-X represents the group. |
| `Mod_rst` | `4` | WO | `0` | Self-negating: clears all moderation timers for immediate assertion. |
| `set_on_posedge` | `3` | RW | `0` | Global default edge mode: `1` ⇒ cause set on posedge (`src=1 & status=0`); `0` ⇒ on level. Per-bit override via `int_posedge_grp`. |
| `auto_clear` | `2` | RW | `0` | `1` ⇒ cause auto-cleared after MSI-X ACK. **Use only with MSI-X enabled.** |
| `auto_mask` | `1` | RW | `0` | `1` ⇒ mask bit auto-set on MSI-X ACK. **Use only with MSI-X enabled.** |
| `clear_on_read` | `0` | RW | `0` | `1` ⇒ **all** cause bits cleared on a CPU read of `int_cause_grp`. |

> **GOTCHA — dead MSI-X controls in the `no_msix` flavor.** `auto_clear`, `auto_mask` and
> `Mask_msi_x` are physically present in the `no_msix` control word too (the word is shared by
> the template), but there is **no MSI-X engine to ACK** in that flavor — they are dead-ends.
> Firmware on a `no_msix` instance must leave them `0` `[MED · INFERRED]`.

---

## 4. Bundle `Sunda` — abort/spare (`no_msix`) vs MSIX-TC (`msix`)

Base `0x300`, `ArraySize=1`, `BundleSizeInBytes=0xf0`, `Description="Sunda-specific
registers"`. The name `Sunda` is a **fixed template label across every generation**
(Cayman / Mariana / Mariana+ / Sunda / Maverick), not a per-SoC codename here.

### 4a. `no_msix` `Sunda` — abort + spare (register offsets relative to `0x300`)

| abs | register | field | pos | acc | rst | meaning (verbatim-derived) |
|---|---|---|---|---|---|---|
| `0x300` | `abort_cntl0` | `local_abort_scan_dump` | `7:0` | RW | `0` | Enter scan-dump mode on a **local** abort. |
| `0x300` | `abort_cntl0` | `remote_abort_scan_dump` | `15:8` | RW | `0` | Enter scan-dump mode on a **remote** abort. |
| `0x300` | `abort_cntl0` | `local_abort_clock_stop` | `23:16` | RW | `0` | Which functional clocks stop on a local abort. |
| `0x300` | `abort_cntl0` | `remote_abort_clock_stop` | `31:24` | RW | `0` | Which functional clocks stop on a remote abort. |
| `0x304` | `abort_cntl1` | `local_abort_to_block_level_logic` | `7:0` | RW | `0` | Let in-block abort logic handle a local abort. |
| `0x304` | `abort_cntl1` | `local_abort_sram_write_protect` | `15:8` | RW | `0` | SRAM write-protect on a local abort. |
| `0x304` | `abort_cntl1` | `remote_abort_sram_write_protect` | `23:16` | RW | `0` | SRAM write-protect on a remote abort. |
| `0x304` | `abort_cntl1` | `reserved` | `31:24` | RW | `0` | Unused. |
| `0x308` | `spare0` | `spare0` | `31:0` | RW | `0x0` | Spare bits. |
| `0x30C` | `spare1` | `spare1` | `31:0` | RW | `0xffffffff` | Spare bits. |

`abort_cntl0`/`abort_cntl1` desc (verbatim): *"Selects how local and remote ABORT events will
freeze this block."* Each abort control is **8 bits wide** — a per-lane / per-domain enable
bitmask, not a single flag — which is *why* the `no_msix` block keeps `int_abort_msk_grp` live
(§2b): the abort wire-OR drives these freeze controls. The abort/scan-dump/clock-stop routing
is the CSR side of the [`../interrupt/errtrig-fis-routing.md`](../interrupt/errtrig-fis-routing.md)
abort path.

### 4b. `msix` `Sunda` — MSIX traffic class only

| abs | register | field | pos | acc | rst |
|---|---|---|---|---|---|
| `0x3E0` | `MSIX_TC` | `val` | `2:0` | RW | `0` |

*"TC to be used by PCIe controller for MSI-X writes."* — a 3-bit PCIe TC selector (0–7).

---

## 5. Per-bit cause / mask / status / set / clear / severity semantics

All semantics below are the descriptor's own embedded strings. Bit `b` in any `*_grp` register
corresponds to trigger input `g*32 + b`.

**`int_status_grp` [RO, reset 0].** *"Latches the status of the interrupt source."* A pure RO
snapshot of the (synchronized) source level/edge state, **before masking**.

**`int_cause_grp` [RW, reset 0] — non-standard clear polarity.**

- **HW set:** when the source fires (level, or posedge per `set_on_posedge`/`int_posedge_grp`).
- **SW set:** write `1` to the matching bit in `int_cause_set_grp`.
- **SW clear:** *"Write-0 clears a bit. Write-1 has no effect."*

> **GOTCHA — WRITE-0-TO-CLEAR (W0C), not the usual W1C.** Clearing a cause bit by writing
> directly to `int_cause_grp` is **W0C**: you write `0` to the bit you want cleared and `1`
> elsewhere (a read-modify-write that writes back a stale `1` will *not* re-assert it, but a
> stale `0` *will* clear a bit hardware just set). This is the Annapurna convention; do **not**
> assume W1C. Auto paths: `auto_clear=1` (MSI-X only) self-clears on ACK; `clear_on_read=1`
> clears **all** bits on any CPU read. HW/SW same-cycle conflict resolves to **SET** (the
> interrupt is never lost).

**`int_cause_set_grp` [WO, reset 0].** *"Writing 1 … sets its corresponding cause bit …
Write 0 has no effect."* — **W1S**, the software-injection / doorbell / self-test path.

**`int_mask_grp` [RW, reset `0xFFFFFFFF`].** Per-bit mask. **Reset = all-ones ⇒ every source
masked at reset** (safe default; firmware unmasks what it wants). If `auto_mask=1`, a bit
self-**sets** (re-masks) on MSI-X ACK.

**`int_mask_clear_grp` [WO, reset 0].** *"Write 0 … clears its corresponding mask bit. Write 1
has no effect."* — a **W0C** side-door to unmask a single bit without a read-modify-write, used
when `auto_mask` races the CPU. (Note the inverted W0C polarity again.)

**`int_posedge_grp` [RW, reset 0].** Per-bit: `1` ⇒ that trigger is **posedge** (edge)
sensitive; `0` ⇒ **level** sensitive. Reset 0 ⇒ default is level for every bit, overridable
per-bit here (or whole-group via `Chicken_posedge` + `set_on_posedge`). This register *realizes*
the `edge_triggered` intent declared per source in the trigger YAML
([`../interrupt/io-fabric-triggers.md`](../interrupt/io-fabric-triggers.md)) `[data HIGH ·
binding MED]`.

**`int_cdc_bypass_grp` [RW in `no_msix`, RO "Unused" in `msix`, reset 0].** Per-bit: `1` ⇒
bypass the trigger-input CDC **edge-gen + tog2pul** (toggle-to-pulse) stage. *"The CDC syncro
is still in the path regardless of value of this bit."* Used for sources already in the INTC
clock domain.

### 5a. The four severity masks (error / abort / fatal / log)

Each is a 32-bit per-bit mask, reset `0xFFFFFFFF` (all masked), producing an **independent
wire-OR severity line**. One cause bit can simultaneously feed up to four classified summary
lines:

| register @rel | severity output |
|---|---|
| `int_error_msk_grp` @`0x2C` | `Error = OR( Cause & !Error_Mask )` |
| `int_abort_msk_grp` @`0x30` | `Abort = OR( Cause & !Abort_Mask )` *(RO/frozen in `msix`)* |
| `int_fatal_msk_grp` @`0x34` | `Fatal = OR( Cause & !Fatal_Mask )` |
| `int_log_msk_grp` @`0x38` | `Log_capture = OR( Cause & !Log_Mask )` *(gates the log-register capture)* |

> **NOTE — reset `0xFFFFFFFF` is the template default, not necessarily the silicon tie-off.**
> Each severity register's desc says *"Its default value is determined by unit instantiation."*
> The `0xFFFFFFFF` is the schema default; a real instance may tie specific bits differently in
> RTL `[value HIGH · silicon-default MED]`. These four wire-ORs are the up-tree summary that
> the `no_msix` aggregators feed into `peb_intc` — see
> [`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md).

---

## 6. MSI-X apparatus (`msix` variant only)

Three of the `msix` bundles use **two coexisting indexing schemes**: `PBA` and `VecTable` are
**group-aligned** (`g*32+b`); the MSI-X table itself is packed **consecutively** by trigger
index. A reimplementor must keep these straight.

### 6a. `PBA` — Pending-Bit-Array (base `0x3F0`, `ArraySize=4`, BundleSize `4` dec, RO)

One register `cause_access_image`, field `val[31:0]` RO reset 0. Bundle desc: *"Memory mapped
cause register access image. Each group is one word — for its up to 32 associated triggers."*
⇒ one 32-bit pending/cause image per group; 4 words = a 128-bit MSI-X PBA at
`grp0 0x3F0, grp1 0x3F4, grp2 0x3F8, grp3 0x3FC`.

### 6b. `VecTable` — per-group, per-trigger moderation + VMID (base `0x400`, `ArraySize=4`)

`BundleSizeInBytes=0x100`. Per group it **nests a second bundle array** `Val` (`ArraySize=32`,
BundleSize `8` dec):

| rel | register | field | pos | acc | rst | meaning |
|---|---|---|---|---|---|---|
| `+0x0` | `Timer_Settings` | `Val` | `7:0` | RW | `0` | 8-bit per-trigger moderation-timer seed. |
| `+0x4` | `VMID_Settings` | `Valid` | `31` | RW | `0` | Valid bit for the entry. |
| `+0x4` | `VMID_Settings` | `VMID` | `9:0` | RW | `0` | 10-bit virtual-machine ID for the MSI-X write. |

Group bases `grp0 0x400, grp1 0x500, grp2 0x600, grp3 0x700`; within a group, entry `e` is at
`base + e*8` (`e ∈ 0..31`). `4 × 32 = 128` entries, one per trigger, carrying
`{moderation-timer, VMID+valid}` `[layout HIGH · index = g*32+b MED]`.

### 6c. `MSIX_Vector_Table_Space` — the PCIe MSI-X table (base `0x800`, `ArraySize=128`)

`BundleSizeInBytes=16` (dec). The standard PCIe MSI-X table, but only **two of the four** 32-bit
words are register-modelled — `Word0`/`Word1` (the 64-bit message *address*) are absent from the
schema (the address is derived; see `Chicken_addrhi` + the decode format below):

| rel | register | field | pos | acc | rst | meaning |
|---|---|---|---|---|---|---|
| `+0x8` | `Word2` | `val` | `4:0` | RW | `0` | *"MSI-X Message Data … the MSI-X interrupt vector for each trigger. Values of 0 to 31 are supported"* — a **5-bit vector select** (32 distinct vectors). |
| `+0xC` | `Word3` | `val` | `0` | RW | **`1`** | *"Mask Bit – Vector Control. Only the LSB … mapped (back and forth) to the associated entry mask register bit."* **Reset = 1 ⇒ masked at reset.** |

Bundle address-decode format (verbatim): *"Each trigger maps a consecutive quad word (16 bytes
per trigger entry), using the following format: `13'b1_VVVV_VVVV_WWBB` (WW=Word[3:2],
BB=Byte In The Word[1:0], and VV=Vector Select — up to 256 triggers). Note allocation here is
consecutive … with all groups packed one-to-each-other, unlike the above parts where groups
were associated with structure-aligned maps."*

> **GOTCHA — two indexing schemes coexist.** The MSI-X table index **is the flat trigger
> index** (`0..127`, schema-capped at the 256 the `13'b1_…` format allows), packed
> consecutively across all groups. `PBA` and `VecTable` are **group-aligned** (`g*32+b`). The
> `13'b1_…` literal is the byte address decode — bit 12 = 1 selects the `MSIX_VTS` aperture,
> the upper `VVVV_VVVV` are the trigger/vector select, `WW`/`BB` pick the word/byte. Do not
> conflate the two index spaces when mapping a cause bit to its MSI-X entry.

---

## 7. The Mako generator — one template, the whole `{ngrp} × {type}` matrix

Both units on this page (and the 1-group siblings) are emitted from a single Mako template,
`intc_ngrp_unit.json.mako`. The parametric contract:

```mako
"UnitName": "intc_${ngrp}grp_${type}_unit"      ## ngrp ∈ {1,4}, type ∈ {no_msix, msix}
"Parameters": [ {INTC_NUM_GROUPS = "${ngrp}", NonOverridable} ,
% if type == 'msix':
                {NUM_OF_TRIGS = "128", NonOverridable} ,        ## ← literal 128
% endif
              ]
"ctrl": { ArraySize: "INTC_NUM_GROUPS", BundleSizeInBytes: "0x40", ... }
```

`ngrp` flows into `INTC_NUM_GROUPS`, which is the `ArraySize` of the `ctrl` (and, in `msix`,
the `PBA` and `VecTable`) bundles — so **`ngrp` directly scales the group count, the cause-word
count, the PBA word count and the per-group vector table**, while the per-group `0x40` stride
and the 12-register layout are fixed. `type` selects three `% if … % else … % endif` branch
points inside one template:

1. **`NUM_OF_TRIGS` parameter** — emitted only for `msix` (the second `Parameters` entry).
2. **`int_cdc_bypass_grp` @`0x24`** — RW + real description (`no_msix`) vs RO `"Unused."`
   (`msix`).
3. **`int_abort_msk_grp` @`0x30`** — RW + abort description (`no_msix`) vs RO `"Unused"`
   (`msix`).
4. **the entire `0x300+` tail** — `msix` emits `Sunda` (MSIX_TC only) + `PBA` + `VecTable` +
   `MSIX_Vector_Table_Space`; `no_msix` emits the abort/spare `Sunda` and stops.

> **QUIRK — `NUM_OF_TRIGS` is the literal `"128"`, NOT `${ngrp}*32`.** The template hardcodes
> `NUM_OF_TRIGS=128` (and thus `MSIX_Vector_Table_Space.ArraySize=128`) regardless of `ngrp`.
> Verified on disk: **`intc_1grp_msix_unit` has `INTC_NUM_GROUPS=1` but `NUM_OF_TRIGS=128`** —
> its `ctrl`/`PBA`/`VecTable` scale down to **one** 32-bit group (32 cause bits), yet its MSI-X
> table is still **128 entries** and still closes the `0x1000` aperture exactly (`0x800 + 128×16
> = 0x1000`). So the 1-group MSI-X unit is **over-provisioned**: 128 MSI-X table entries back
> only 32 live cause bits. The `no_msix` 1-group unit has no such mismatch
> (no MSI-X table at all; its aperture tail is simply unused past `Sunda`). The 1-group / IOFIC
> family is detailed in [`intc-1group-apintc.md`](intc-1group-apintc.md).

> **NOTE — only `ngrp ∈ {1,4}` are materialized in this CSR set.** The template can emit any
> `ngrp`, but the shipped `cayman-arch-regs/csrs/intc/` directory contains exactly four unit
> files: `intc_{1,4}grp_{no_msix,msix}_unit.json` plus the `.mako`. A structural `jq` diff of
> `1grp` vs `4grp` (normalizing `UnitName` and the `INTC_NUM_GROUPS` value) is **byte-identical**
> — confirming the 1grp/4grp pair differ *only* in those two strings, exactly as the template
> predicts. On **Maverick (v5)** the `intc/` schema dir drops `intc_1grp` entirely and keeps
> only the two `4grp` units (a real generational change, verified in
> [`../address/pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md) §2c).

---

## 8. Group → trigger-domain binding `[HIGH binding · MED interpretation]`

**What the schema does *not* encode** `[HIGH · OBSERVED]`: neither intc JSON contains any
per-bit source *name* or group→domain label. The cause/mask words are anonymous 32-bit
registers. The mapping from a physical trigger to `(instance, group, bit)` lives in the
**trigger-YAML order** and the **address-map instance binding**, never in the intc schema.
Every group→domain claim is therefore INFERRED from corroborating artifacts.

**The flavor choice is per-instance, not per-domain** `[HIGH · OBSERVED]`. The Cayman
`address_map_json_xref.yaml` binding (the byte-grounded NC-v3 census) instantiates:

| unit | instances |
|---|---|
| `intc_4grp_no_msix_unit.json` | **1,070** (on-die summary aggregators) |
| `intc_4grp_msix_unit.json` | **858** (host-facing MSI-X leaves) |
| `intc_1grp_msix_unit.json` | **4** (the RDM root-domain MSI-X: `{apb_io_0, apb_io_1, peb_apb_io_0, peb_apb_io_1}_user_io_intc_rdm_msix`) |

Total `1,070 + 858 + 4 = 1,932`; the `4grp` errtrig fabric alone is `1,928`. **Every** trigger
domain (SDMA, PCIe, FIS, IO-fabric, TPB, HBM, D2D, SP) appears with **both** flavors — the
flavor is chosen by the instance's *role*:

- **Direct user/host path** (`…_user_fis_…_errtrig_trig_{0,1}`) → **MSI-X** leaf, delivers
  straight to the PCIe host.
- **Aggregation/broadcast path** (`…_amzn_…`, `…_bcast_…`) → **no_msix** aggregator, whose four
  severity wire-ORs + `Mask_msi_x` summary feed **upward** into `peb_intc`.

> **NOTE — consistency with [`pkl-intc-sprot-security.md`](../address/pkl-intc-sprot-security.md)
> (#908).** No correction needed; the two views agree where they overlap. #908's primary census
> is the **Maverick (v5) decentralized PKL** (`type='INTC'` = 5,904 records, 13 schemas; the
> symmetric errtrig PAIR `TRIG_0 = TRIG_1 = 1,372`). #908 *also* cites the **Cayman flat
> address-map** binding — `1,070 no_msix + 858 msix + 4 1grp_msix RDM` — **identical** to the
> figures above. These are two different metrics on two different SoCs (the v3 instance binding
> vs the v5 record census), **not** a divergence. The errtrig PAIR (an on-die-summary `no_msix`
> half + its host-delivered `msix` twin) is the structural reason both flavors exist for every
> domain; the PAIR container, schema bindings, and the `int_sec_grp`/`int_regs_sec_grp` SWOM
> write-locks + `amzn`-fail-CLOSED / `user`-fail-OPEN trust boundary that wrap this fleet on v5
> are all in #908.

### 8a. Capacity arithmetic per domain `[arithmetic HIGH · binding MED · INFERRED]`

Each intc instance latches ≤ 128 sources (4 grp × 32). An errtrig has **two** intc instances
(`trig_0` + `trig_1`) ⇒ a **256-source** capacity pair. Aggregate fits (the exact bit-to-group
*cut* is **not** recoverable from any shipped artifact — only the aggregate fit is):

| domain (trigger YAML) | count | fits in |
|---|---|---|
| `sdma_triggers` | 254 | 1 errtrig (2 intc, 256 cap) |
| `io_fabric_triggers` | 243 | 1 errtrig pair |
| `pcie_triggers` | 228 | 1 errtrig pair |
| `hbm_triggers` | 223 | 1 errtrig pair |
| `tpb_triggers` | 216 | 1 errtrig pair |
| `d2d_triggers` | 216 | 1 errtrig pair |
| `cc_triggers` | 98 | 1 intc (4 grp) |
| `top_sp_triggers` | 82 | 1 intc |
| `peb_intc_triggers` | 128 | 1 intc (the apex) |

> **EXPLICIT NON-CLAIM.** The mapping of a domain's sub-blocks to specific *groups* (0/1/2/3) is
> **not** encoded in the shipped schema. Any "group 0 = SDMA, group 1 = IO-fabric"-style
> statement would be fabrication and is **not** made here. Only the aggregate capacity fit is
> asserted, at MED/LOW confidence on the cut, HIGH on the totals.

---

## 9. `ap_intc` — the MEM-mapped sibling (cross-reference)

For completeness (full treatment in [`intc-1group-apintc.md`](intc-1group-apintc.md)):
`ap_intc_4grp_unit.json` is a **wrapper** — top `RegistersBundleArrays=[]`, pulling the group via
`Includes → ap_intc_grp_ctrl.json` (`InclSizeInBytes 0x40`, `ArraySize=INTC_NUM_GROUPS`,
`InclType=DIRECT`). Key differences from the APB intc on this page:

- `InterfaceType = MEM` (not APB); `SizeInBytes = "0x40*INTC_NUM_GROUPS"` and
  `AddrWidth = "log2(0x40*INTC_NUM_GROUPS)"` — symbolic expressions, not literals.
- The included group has **9 registers** (vs the APB intc's 12). The three **absent** registers
  are `int_cdc_bypass_grp` (no CDC needed — MEM access is same-domain), `int_error_msk_grp` (no
  separate error severity line) and `int_posedge_grp` (no per-bit edge override).
- Comes in 1grp/2grp/4grp, all including the same `ap_intc_grp_ctrl`.

`ap_intc` is the on-chip-processor ("access port") view of an interrupt group — a leaner,
memory-mapped, same-clock INTC the GPSIMD/AP cores drive directly, while the APB `intc` units on
this page are the chip-fabric aggregators with full CDC + posedge + 4-way severity machinery
`[MED · INFERRED]`.

---

## 10. Verification summary

- `ctrl` `ArraySize=INTC_NUM_GROUPS=4`, BundleSize `0x40` → groups @ `0x00/0x40/0x80/0xC0`. ✓
- `no_msix` vs `msix` `ctrl` diff = **exactly 2** registers (`int_cdc_bypass_grp`@`0x24`,
  `int_abort_msk_grp`@`0x30`) demoted RW → RO `"Unused"`. ✓
- `msix`-only bundles `PBA`(`0x3F0`) / `VecTable`(`0x400`) / `MSIX_VTS`(`0x800`) + `Sunda`
  `MSIX_TC`(`0x3E0`); `NUM_OF_TRIGS=128 = 4 grp × 32 = MSIX_VTS.ArraySize`. ✓
- Aperture closes **only** with decimal `BundleSizeInBytes` for `"16"`/`"8"`: `0x800 + 128×16 =
  0x1000`; hex `0x16` overflows to `0x1300`. ✓
- Counts: `no_msix` 16 reg / 33 field defs (RW:13/WO:2/RO:1);
  `msix` 16 top + 2 nested reg defs, 30 field defs recursive. ✓
- No `0xb1` placeholder reset; the only `b1` is the `13'b1_…` Verilog literal in a description. ✓
- Cross-gen structural diff (Cayman / Mariana / Mariana+ / Sunda / Maverick) = **nil** for both
  variants — Cayman is authoritative and fully representative. ✓
- `1grp` vs `4grp` unit files differ **only** in `UnitName` + `INTC_NUM_GROUPS` value;
  `1grp_msix` carries the same literal `NUM_OF_TRIGS=128` (over-provisioned MSI-X table). ✓
- Address-map binding `1,070 no_msix + 858 msix + 4 1grp_msix` — consistent with
  [#908](../address/pkl-intc-sprot-security.md). ✓

**Honest uncertainty (explicitly NOT claimed):** the exact bit-to-group cut for each trigger
domain (which YAML sub-block lands in which of the 4 groups) is in no shipped artifact; §8a is
aggregate-capacity only. The `g*32+b` global ordering (§1, §6) is INFERRED from the PBA
"one word per group" layout + the MSI-X table's consecutive packing — corroborating, not
literally stated.
