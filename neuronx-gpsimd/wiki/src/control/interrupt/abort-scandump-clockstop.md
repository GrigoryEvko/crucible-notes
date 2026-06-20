# Abort / Scan-Dump / Clock-Stop Control

This page reconstructs the **SoC emergency-stop + debug-state-capture mechanism** of the
Cayman/Trainium GPSIMD die — the path by which a fatal/abort fault **freezes** a fabric
block, **captures** its flop state (scan dump), **gates** its functional clocks, and
**write-protects** its SRAM, both locally and across a die-to-die boundary. There is **no
dedicated "abort controller"**: the entire mechanism is a four-register **freeze bundle**
— generator-named **`Sunda`** — instantiated at `AddressOffset 0x300` inside *every* on-die
(`no_msix`) error-trigger (`intc_4grp_no_msix_unit`). Its two control registers,
`abort_cntl0` / `abort_cntl1`, are each split into four 8-bit per-target bitmaps that select,
**independently for a local and a remote abort**, three freeze actions plus a block-level
hand-off. The driver is the error-trigger's **Abort severity wire-OR** (decoded in
[`errtrig-fis-routing.md`](errtrig-fis-routing.md)); the actual state-capture engine is the
**ARM CoreSight ELA-500** embedded logic analyzer (`cxela500`, 1,318 instances).

The error-routing front end (cause latch, the four severity masks, `nmi_out` summary climb)
is [`errtrig-fis-routing.md`](errtrig-fis-routing.md); the `intc_4grp` unit and its `0x300+`
tail are [`../csr/intc-4group.md`](../csr/intc-4group.md); the Q7's **in-core** halt/break
surface is [`q7-surprises-binding.md`](q7-surprises-binding.md) +
[`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md) +
[`../csr/xtensa-q7.md`](../csr/xtensa-q7.md); the SEQ firmware self-fault and run-state paths
are [`../../firmware/seq/error-handler.md`](../../firmware/seq/error-handler.md) +
[`../../firmware/seq/run-state.md`](../../firmware/seq/run-state.md) +
[`../../firmware/seq/surprises-irq.md`](../../firmware/seq/surprises-irq.md).

> **PROVENANCE.** Every offset, bit-range, access type, reset value and verbatim
> description below was read byte-exact (jq / rg / `objdump`) from shipped Annapurna-Labs
> register-description artifacts in the `cayman-arch-regs` set and the per-gen
> `arch-headers/{sunda,mariana,mariana_plus,maverick}` mirrors. Primary inputs and their
> md5:
> `intc_4grp_no_msix_unit.json` `d3e509688793b1a320b9e988dc77484d`;
> `intc_4grp_msix_unit.json` `f6521dfefedd220dc971ca5b16277590`;
> `cxela500.json` `91e4d4886bed2d71415bc5004296a19f`;
> `xtensa_q7.json` `da231fbc982c8bef453e37a4d4e31fe4`;
> `tpb_xt_local_reg.json` `428ead76c5da251d1147343fdb21c71a`;
> plus `output/address_map/address_map_flat.yaml` and the carved Q7/NX firmware
> (`libnrtucode_internal.so`, `CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}` blobs, disassembled with
> the shipped Cadence `xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`).
>
> Confidence tags: `[HIGH · OBSERVED]` = literal value read from a shipped JSON/YAML/disasm,
> re-verified this pass; `[MED · INFERRED]` = semantic reconstruction from naming +
> cross-file corroboration; `[LOW]` = plausible reading, flagged; `[· CARRIED]` =
> consolidated from a named sibling page, not re-derived. **Generation scope:** the `Sunda`
> bundle's bitfields (positions, names, resets, access) are **byte-identical** across
> Cayman / Mariana / Mariana+ / Maverick (recursive `jq -S` diff = IDENTICAL); Sunda differs
> in exactly one *description* string (§5). **Maverick (v5)** is **header-OBSERVED only** —
> its `intc_4grp_no_msix_unit.json` schema and `al_address_map_db.json` carry the bundle and
> the fleet, but v5-*interior* placement/behavior behind an address is **INFERRED**.

---

## 0. The mechanism at a glance `[HIGH · OBSERVED]`

The abort/emergency-stop is a **4-register `Sunda` freeze bundle** sitting above the four
per-group control blocks inside the `no_msix` error-trigger:

| Bundle | `ArraySize` | `AddressOffset` | `BundleSizeInBytes` | `HalExists` | content |
|---|---|---|---|---|---|
| `ctrl`  | `INTC_NUM_GROUPS` (=4) | `0x000` | `0x40` | `NON_EX_ONLY` | per-group cause/mask/4-severity set |
| `Sunda` | 1 | `0x300` | `0xf0` | `NON_EX_ONLY` | `abort_cntl0/1`, `spare0/1` |

Four register definitions live in the bundle: `abort_cntl0` (`0x300`), `abort_cntl1`
(`0x304`), `spare0` (`0x308`), `spare1` (`0x30c`). The `0xf0` bundle size reserves the
`0x300..0x3f0` window (in the `msix` twin that window instead ends in `PBA`/MSI-X apparatus,
§1d). The whole bundle carries `HalExists=NON_EX_ONLY` — it is **excluded from the firmware
HAL** and is programmed by the SoC RAS/management infrastructure (or boot/fuse defaults),
**not** by the compute-engine firmware (§4a corroborates: zero abort strings in the SEQ
image).

> **QUIRK — the codename is a generation marker, not a per-SoC variant.** `Sunda` is the
> *generator name* of the bundle (Description: `"Sunda-specific registers"`) because the
> abort-freeze bundle was **introduced in the Sunda baseline** and carried forward unchanged.
> It is **not** a Cayman/Mariana/Maverick switch — the same `Sunda` bundle name appears in
> every generation's schema. Do not read it as "Sunda silicon".

---

## 1. The `abort_cntl` register set — the `Sunda` freeze bundle `[HIGH · OBSERVED]`

### 1a. `abort_cntl0` @ `0x300` — scan-dump + clock-stop select

`AccessType=RW`, Description (verbatim): *"Selects how local and remote ABORT events will
freeze this block."* Four 8-bit fields, each a **per-target bitmap** (§1e):

| field | bits | rst | access | description (verbatim) |
|---|---|---|---|---|
| `local_abort_scan_dump`   | `[7:0]`   | 0 | RW | `Enter scan dump mode on a local abort event` |
| `remote_abort_scan_dump`  | `[15:8]`  | 0 | RW | `Enter scan dump mode on a remote abort event` |
| `local_abort_clock_stop`  | `[23:16]` | 0 | RW | `Which functional clocks should be stopped on a local abort event` |
| `remote_abort_clock_stop` | `[31:24]` | 0 | RW | `Which functional clocks should be stopped on a remote abort event` |

### 1b. `abort_cntl1` @ `0x304` — block-logic hand-off + SRAM-write-protect

Same register Description. Four 8-bit fields:

| field | bits | rst | access | description (verbatim) |
|---|---|---|---|---|
| `local_abort_to_block_level_logic` | `[7:0]`   | 0 | RW | `Let abort logic in block handle a local abort event` |
| `local_abort_sram_write_protect`   | `[15:8]`  | 0 | RW | `SRAM write protect on a local abort event` |
| `remote_abort_sram_write_protect`  | `[23:16]` | 0 | RW | `SRAM write protect on a remote abort event` |
| `reserved`                         | `[31:24]` | 0 | RW | `Unused` |

`spare0` @ `0x308` (RW, rst `0x0`) and `spare1` @ `0x30c` (RW, rst `0xffffffff`) round out the
bundle; both carry the generic Description `"Spare bits"`.

> **GOTCHA — there is NO `remote_abort_to_block_level_logic` field.** The block-level-logic
> hand-off exists **only for the local abort** (`abort_cntl1[7:0]`). A remote (cross-die)
> abort can arm scan-dump, clock-stop and SRAM-write-protect, but it **cannot** invoke the
> block's own teardown logic. This is a deliberate asymmetry `[MED · INFERRED]`: a die
> handles its *own* abort with active block logic (drain/quiesce); a *peer* die's abort only
> drives the passive freeze actions locally.

> **NOTE — `spare1` reset `0xffffffff` is a bundle-tail sentinel, not a control.** It is the
> generic `Spare bits` register; do not interpret its all-ones reset as enabling anything.

### 1c. The per-target bitmap interpretation `[HIGH layout · MED map]`

Each freeze action is an **8-bit field, not a single enable**. The reading — strongly
supported by the verbatim plural *"Which functional **clocks** should be stopped"* and the
8-bit width — is that **each of the 8 bits selects one freeze target**:

* `*_abort_clock_stop[7:0]` → up to **8 functional clock domains** to gate (per-bit clock).
* `*_abort_scan_dump[7:0]`  → up to **8 scan chains / scan-dump partitions** to capture.
* `*_abort_sram_write_protect[7:0]` → up to **8 SRAM banks / partitions** to write-lock.

A block therefore programs *which subset* of its clocks/scan-chains/SRAM-banks react to an
abort, and does so **separately for a local vs a remote abort**.

> **OPEN (LOW) — the per-bit → silicon target map is RTL, not register-encoded.** Which exact
> clock tree / scan chain / SRAM bank each of the 8 bits selects is in the RTL netlist, not
> in this schema. The "8 bits = 8 targets" reading is `[MED · INFERRED]` (width + plural
> description); the concrete bit→domain assignment is `[LOW]`, a non-claim.

### 1d. The `no_msix`-only property — abort-freeze is privileged `[HIGH · OBSERVED]`

The host-facing `msix` error-trigger twin **also** has a bundle named `Sunda` at `0x300`,
but it contains **only one register** — `MSIX_TC` (bundle-relative `0xe0` → absolute
`0x3e0`, field `val[2:0]` = the TC for PCIe MSI-X writes). It has **zero** `abort_cntl` /
`scan_dump` / `clock_stop` / `sram_write_protect` fields (`rg -c` on the `msix` JSON for
those tokens = **0**). Consistently, the `msix` variant has two control registers **demoted
to read-only**:

| register | `no_msix` | `msix` |
|---|---|---|
| `int_abort_msk_grp` @ `0x30` | **RW** (the Abort wire-OR mask) | **RO** |
| `int_cdc_bypass_grp` @ `0x24` | **RW** | **RO** |
| `Sunda` @ `0x300` | `abort_cntl0/1` + `spare0/1` (freeze bundle) | `MSIX_TC` only |

| flavor | Cayman instances | abort path |
|---|---|---|
| **AMZN / `no_msix`** error-trigger | **1,070** | Abort wire-OR **live** + `abort_cntl` freeze bundle |
| **USER / `msix`** error-trigger | **858** | Abort mask **RO**; `Sunda` = `MSIX_TC` only |

> **CORRECTION — a `msix` (PCIe/host) error-trigger cannot arm a block freeze.** Earlier
> prose that listed `msix` as "also carrying the Sunda bundle" is true **by bundle name
> only** — the `msix` `Sunda` bundle holds no abort control. The abort / scan-dump /
> clock-stop mechanism is **exclusively the privileged on-die aggregation path**; a host is
> structurally locked out (its abort mask is RO and its `Sunda` tail is the MSI-X apparatus).
> `[HIGH · OBSERVED — direct jq diff of the two units' `Sunda` bundles.]`

---

## 2. The scan-dump mechanism — what captures state on abort `[HIGH · OBSERVED]`

### 2a. The capture hardware = ARM CoreSight ELA-500 (`cxela500`)

The scan-dump target is the **ARM CoreSight Embedded Logic Analyzer ELA-500**. The address
map binds **1,318** instances of `csrs/ela500/cxela500.json`. Identity is byte-confirmed from
the CoreSight ID block reset values:

| register | field | reset | meaning |
|---|---|---|---|
| `DEVARCH` | `ARCHITECT[31:21]` | `0x23b` | ARM JEP106 architect code |
| `DEVARCH` | `ARCHID[15:0]`     | `0xa75` | **ELA-500** architecture ID |
| `DEVTYPE` | `DEVTYPE[7:0]`     | `0x75`  | ELA device type |
| `PIDR0`   | `PART_0[7:0]`      | `0xb8`  | ELA-500 part-number low byte |

`cxela500` is a `REGFILE`, 12-bit / `0x1000` APB aperture, one bundle `ALL` of 32 registers
@ `0x0`. The capture/trigger register set (names + offsets byte-read):

| register | off | role |
|---|---|---|
| `CTRL`     | `0x00` | master control |
| `TIMECTRL` | `0x04` | timestamp control |
| `TSSR`     | `0x08` | trigger-state select |
| `PTACTION` | `0x10` | per-trigger action (capture/stop/…) |
| `CTSR`     | `0x20` | current trigger state |
| `CCVR` / `CAVR` | `0x24` / `0x28` | capture count / value |
| `RDCAPTID` | `0x2c` | read-capture ID |
| `RRAR` / `RRDR` | `0x40` / `0x44` | **RAM-Read** addr / data (read back the captured trace SRAM) |
| `RWAR` / `RWDR` | `0x48` / `0x4c` | RAM-Write addr / data |
| `ITTRIGOUT`/`ITTRIGIN`/`ITCTLR` | `0xee8`/`0xef8`/`0xf00` | integration / cross-trigger |
| `LAR`/`LSR`/`AUTHSTATUS`/`DEVARCH`/`DEV*`/`PIDR*`/`CIDR*` | `0xfb0+` | CoreSight ID block |

So the ELA-500 is a **trigger-state machine** that, on a trigger, captures selected signal
groups into a local trace SRAM, read back via `RRAR`/`RRDR`. When an abort fires and
`abort_cntl0.*_scan_dump` arms, the relevant block's ELA-500 enters capture and records the
**frozen** block state into its trace RAM.

> **NOTE — the ELA register Descriptions are generator-defaulted (terse).** `TSSR`, `PTACTION`
> etc. carry their own names as Descriptions; the concrete per-instance **trigger → capture**
> programming (which signal groups, which trigger states) is not in the JSON. The
> abort-trigger → ELA-`PTACTION`/`TSSR` wiring is the RTL netlist's `[MED · INFERRED]` (see
> §6 O2), not register-encoded here.

### 2b. The per-block ELA placement — scan dump is distributed `[HIGH · OBSERVED]`

The 1,318 ELA instances attach by block type. The **terminal `_ELA` leaf token** census of
every `cxela500` leaf partitions **exactly** into four categories:

| terminal leaf token | count | attaches to |
|---|---|---|
| `SPROT_ELA`    | **528** | every `sprot` (firewall/remapper/qos/NTS security state) — the most-replicated |
| `INTERNAL_ELA` | **456** | per-block internal-fabric ELA |
| `APP_ELA`      | **218** | application / compute-block ELA |
| `NTS_ELA`      | **116** | no-target-responder ELA |
| **total**      | **1,318** | |

> **CORRECTION — there is NO separate `FIS_ELA` leaf category.** `528 + 456 + 218 + 116 =
> 1,318` **exactly** — these four terminal tokens already account for every `cxela500` leaf.
> `FIS_ELA_N` is a **parent path token** (the dedicated *DEBUG-FIS* subtree, e.g.
> `..._HBM_DEBUG_FIS_ELA_0_INTERNAL_ELA`); its actual ELA leaf is the `_INTERNAL_ELA`/
> `_SPROT_ELA` *under* it, already counted above. A regex for any `cxela500` leaf whose
> **terminal** token is `FIS_ELA` returns **empty**. Treating `FIS_ELA_0..9` as a fifth
> "8-each" category double-counts. `[HIGH · OBSERVED — terminal-token census, this pass.]`

### 2c. The Q7-core-level state capture (the on-core complement) `[HIGH · CARRIED]`

Distinct from the fabric ELA, each Q7/NX core has its **own** debug-state capture in the
`xtensa_q7` OCD aperture (full detail in [`../csr/xtensa-q7.md`](../csr/xtensa-q7.md)):

* **TRAX** (`Trax_Registers` bundle @ `0x0`): a compressed branch-trace unit into a local
  TraceRAM, read via `TRAXDATA`/`TRAXADDR`; a PC-match or cross-trigger can stop trace and
  raise `DSR`.
* **`FAULTINFOLO`** (`Miscellaneous_Registers` @ `0x3000` + `0x2c` = **`0x302c`**):
  `PFatalError[31]` *"Sticky fatal error notification signal"*, `Halted[30]`,
  `DE[29]`/`TE[28]` double/triple-exception, `HaltCode[27:24]`, `UserCode[23:20]` — the
  core's fault-cause capture.
* **`FAULTINFOHI`** (`0x3000` + `0x30` = **`0x3030`**): **17 fields** of ECC/RAS flags
  (`DRamCorr`/`DRamUnc`, `IRamCorr`/`IRamUnc`, `ICache*`, `DPRm*`, `IPRm*`, `ECCTstMde`).

So the SoC fabric scan-dump (ELA-500) and the per-core trace/fault capture (TRAX +
FAULTINFO) are **two complementary state-capture surfaces**: an abort freezes the fabric
block and arms its ELA; the OCD debugger reads the core's TRAX/FAULTINFO over the JTAG/APB
debug bus afterward. `[two-surface split HIGH · OBSERVED; which surface a post-mortem tool
reads is the tool's choice — MED.]`

---

## 3. Clock-stop + SRAM-write-protect — gating + locking on fatal `[HIGH · OBSERVED]`

### 3a. Clock-stop (`abort_cntl0[23:16]` local / `[31:24]` remote)

The 8-bit `clock_stop` bitmap gates the block's *functional clocks* on an abort. The clock
**sources** are the SoC PLLs — the address map exposes `PEB_PLL_HBM0`, `PEB_PLL_HBM1`,
`PEB_PLL_SOC` feeding per-block clock trees — and the per-bit `clock_stop` field selects
which of (up to) 8 functional clock domains of *this* block stop. This is the emergency
clock-gate that **freezes the block's pipeline** so the scan dump captures a coherent,
non-advancing state. `[field layout HIGH · OBSERVED; "8 bits = 8 clock domains" MED; the
specific PLL/clock-tree → bit map LOW/RTL.]`

### 3b. SRAM-write-protect (`abort_cntl1[15:8]` local / `[23:16]` remote)

On an abort, the `sram_write_protect` bitmap **locks (up to 8) SRAM banks** against further
writes — preserving memory contents for post-mortem while clock-stop freezes logic and the
ELA captures flops.

### 3c. The block-level-logic hand-off (`abort_cntl1[7:0]`)

`local_abort_to_block_level_logic` = *"Let abort logic in block handle a local abort event"*.
Beyond the centralized freeze, the bundle can **delegate to the block's own abort logic** (a
per-IP teardown/drain) — e.g. a DMA/AXI master quiescing its outstanding transactions.

> **GOTCHA — the Sunda baseline coupled write-protect to block-logic; Cayman+ decoupled it.**
> The **sole** byte difference between the `sunda` and `cayman` `Sunda` bundles (jq -S diff)
> is the Description of `local_abort_to_block_level_logic`:
>
> | gen | `local_abort_to_block_level_logic` description |
> |---|---|
> | `sunda` | `Let abort logic in block handle a local abort event, also forces SRAM to write protect mode` |
> | `cayman`+ | `Let abort logic in block handle a local abort event` |
>
> In the Sunda baseline, enabling block-level abort logic **implicitly forced SRAM
> write-protect**; from Cayman on the two are **decoupled** — `sram_write_protect` is its own
> explicit bitmap. The **bit layout did not change** (the explicit fields existed in Sunda
> too); only the side-effect coupling text was removed. `[HIGH · OBSERVED — literal string
> diff; bit positions byte-identical all gens.]`

### 3d. The freeze ordering (post-mortem coherence) `[MED · INFERRED]`

The coherent post-mortem sequence implied by the four actions is: (1) `clock_stop` gates the
clocks (logic stops advancing); (2) `sram_write_protect` locks the SRAMs; (3) `scan_dump`
captures the frozen flop/signal state into the ELA RAM; with `block_level_logic` optionally
draining outstanding transactions first. The schema gives the four **enable bitmaps** but
**not** their relative timing/ordering — that is RTL. `[four actions HIGH · OBSERVED; ORDERING
INFERRED-from-purpose.]`

```c
// Conceptual abort -> freeze -> scan-dump -> clock-stop -> SRAM-WP sequence.
// All four actions are HARDWARE responses to the Abort wire-OR; firmware does NOT
// drive them (Sunda bundle is HalExists=NON_EX_ONLY, §1; see §4a string-absence).
// Bit i of each field == freeze target i (clock domain / scan chain / SRAM bank);
// the bit -> silicon-target map is RTL, NOT register-encoded (§1c, O1).

void on_abort_wire_or(errtrig_t *e, bool is_remote) {
    uint8_t scan = is_remote ? FIELD(e->Sunda.abort_cntl0, remote_abort_scan_dump)   // [15:8]
                             : FIELD(e->Sunda.abort_cntl0, local_abort_scan_dump);   // [7:0]
    uint8_t clk  = is_remote ? FIELD(e->Sunda.abort_cntl0, remote_abort_clock_stop)  // [31:24]
                             : FIELD(e->Sunda.abort_cntl0, local_abort_clock_stop);  // [23:16]
    uint8_t wp   = is_remote ? FIELD(e->Sunda.abort_cntl1, remote_abort_sram_write_protect) // [23:16]
                             : FIELD(e->Sunda.abort_cntl1, local_abort_sram_write_protect);  // [15:8]

    // (0) LOCAL only: optionally let the block's own logic drain/quiesce first.
    if (!is_remote && FIELD(e->Sunda.abort_cntl1, local_abort_to_block_level_logic))
        block_abort_drain(e->block);            // per-IP teardown (no remote equivalent, §1b)

    // (1) gate the selected functional clock domains -> pipeline freezes
    for (int i = 0; i < 8; i++) if (clk  & (1u << i)) clock_domain_gate(e->block, i);
    // (2) write-protect the selected SRAM banks -> contents preserved
    for (int i = 0; i < 8; i++) if (wp   & (1u << i)) sram_bank_write_protect(e->block, i);
    // (3) arm scan dump -> the block's ELA-500 captures the frozen state into trace RAM
    for (int i = 0; i < 8; i++) if (scan & (1u << i)) ela500_enter_capture(e->block, i);
    // ordering (1)->(2)->(3) is INFERRED for coherence; the schema gives enables only (§3d).
}
```

---

## 4. The Q7 halt / break connection — abort vs the in-core debug path `[HIGH · MED]`

### 4a. The firmware does NOT drive abort — abort is a fabric/HW mechanism `[HIGH · OBSERVED]`

The carved GPSIMD NX (SEQ) firmware contains **zero** `abort` / `scan_dump` / `clock_stop` /
`freeze` / `sram_write` / `write_protect` strings — a `strings -n4` + `grep -i` over the
**embedded firmware span only** (the `.rodata` blob, file `0x58f40..0x30b8a0`, where
VMA == file offset) and over the POOL/SEQ DEBUG blob returns **0** for every term. (The
2 `abort` hits in the whole `.so` are **x86 host artifacts** — the `abort@GLIBC_2.2.5` PLT
import and a static symbol — *outside* the firmware blob; do not confuse them.) The firmware
carries only the **HALT + ErrorHandler self-fault** strings — `"S: Setup Halt"` (DRAM-local
`0xf47`), `"S: Entering HALT"` (`0x1e79`), `"S: Halt"`, `"S: ErrorHandler : FP Error(%d)"`,
and the fault-cause text (`Int Div Zero`, `Bad Opcode(0x%x)`, `Illegal Instruction(0x%x)`).
The Q7 **compute** image carries none of even these (it is the slave engine).

So abort / scan-dump / clock-stop is **not a firmware operation** — it is the SoC RAS /
management hardware acting on the fabric (consistent with `HalExists=NON_EX_ONLY`, §0/§1).
The firmware's own fault response is to **self-halt** (its ISA `HALT` + `ErrorHandler`),
independently of the fabric freeze — see
[`../../firmware/seq/error-handler.md`](../../firmware/seq/error-handler.md) and
[`../../firmware/seq/run-state.md`](../../firmware/seq/run-state.md).

### 4b. The Q7 per-core halt surface (the in-core path abort sits alongside) `[HIGH · CARRIED]`

The Q7/NX halt/break is the **separate** `tpb_xt_local_reg` + OCD surface — do not duplicate
[`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md) and
[`../csr/xtensa-q7.md`](../csr/xtensa-q7.md):

| surface | register / SR | field | meaning |
|---|---|---|---|
| `tpb_xt_local_reg` | `instr_halt_ctrl` @ `0x14` | `halt_req[0]` | *"Request to go to ISA HALT state"* (rst 0) |
| `tpb_xt_local_reg` | `hw_decode.breakpoint_ctrl` @ `0x4004` | `immediate_pause[8]`, `breakpoint_step_valid[3]`, `breakpoint_instr_enable[0]` | host-debug PC breakpoints |
| Xtensa OCD | `DCRSET` @ `0x200c` | `DebugInterrupt[1]` | break the core |
| Xtensa OCD | `DSR` @ `0x2010` | `Stopped[4]`, `StopCause[8:5]` | core under OCD control / stop reason |
| Xtensa OCD | `PWRCTL` @ `0x3020` | `CoreReset[16]`, `DebugReset[28]` | per-core reset / debug-module reset |
| architectural | `DBREAKA0/C0`, `IBREAKA0/C0` (SRs `0x90/0xa0`, `0x80/0xc0`) | — | in-core data/instruction HW breakpoints |

### 4c. The firmware tie — EXT_BREAK reads `instr_halt_ctrl` `0x14` `[HIGH · OBSERVED]`

The SEQ surprises handler `sunda_handle_surprises` (entry `entry a1,48` @ IRAM `0x6cf4`;
[`q7-surprises-binding.md`](q7-surprises-binding.md),
[`../../firmware/seq/surprises-irq.md`](../../firmware/seq/surprises-irq.md)) has an
`EXT_BREAK` arm (selected by `bbci a2,3,…` at `0x6d54`) that reads the halt CSR. The
byte-exact disassembly (NX IRAM, decoded with the shipped `xtensa-elf-objdump`,
`XTENSA_CORE=ncore2gp`):

```asm
6d5d:  a4 08 00   const16 a10, 8        ; \ load DRAM ptr 0x0008_1a1d
6d60:  a4 1d 1a   const16 a10, 0x1a1d   ; / == "S: EXT_BREAK" (DRAM-local 0x1a1d)
6d63:  25 e7 11   call8   0x18b84       ;   print("S: EXT_BREAK")
  ...
6d6e:  22 a4 00   movi    a2, 0x400     ; NX LOCAL_REG base selector (0x400)
6d71:  24 14 00   const16 a2, 20        ; + 0x14  (= nx.instr_halt_ctrl @ 0x0014)
6d74:  28 02      l32i.n  a2, a2, 0     ; READ instr_halt_ctrl.halt_req
6d76:  dc 32      bnez.n  a2, 0x6d8d    ; branch on whether HALT was armed
```

On an external-breakpoint surprise the firmware logs `"S: EXT_BREAK"` (DRAM-local `0x1a1d`),
checks the halt-request CSR, and escalates (FATAL) if not armed. The `0x14` here is the
per-register dword offset within the NX `xt_local_reg` block — the same register the schema
places at `instr_halt_ctrl @ 0x14` (the Sunda HAL YAML's full local-reg-space address for it
is `0x808`; both consistent). `[HIGH · OBSERVED — re-disassembled this pass, bytes
`22 a4 00 / 24 14 00 / 28 02 / dc 32`.]`

### 4d. The relationship — abort FREEZES, break/halt STEERS `[MED · INFERRED, well-grounded]`

The abort-freeze and the Q7 break/halt are **disjoint mechanisms at different layers**:

* **ABORT** (this page's `Sunda` bundle): a **fabric-level** emergency stop — gates the whole
  block's clocks, captures via ELA, locks SRAM. Triggered by the error-trigger Abort severity
  (a RAS/security fault), management-driven, `no_msix`-only. It is **not** a Q7
  instruction-level event; it freezes the block the Q7 *lives in*.
* **Q7 HALT/BREAK**: an **in-core** debug event — ISA `HALT` (`instr_halt_ctrl`), a
  `hw_decode`/`DBREAK`/`IBREAK` breakpoint, single-step, or an OCD `DebugInterrupt`. It is
  host/debugger-driven (or a self-fault) and the firmware notices it at a **poll** (the
  GPSIMD Q7 is a polled, not vectored, engine — [`q7-surprises-binding.md`](q7-surprises-binding.md)).

The **debug path can feed the abort path**: an in-core fatal (`FAULTINFOLO.PFatalError`) or
an uncorrectable ECC (`FAULTINFOHI`) is itself a RAS source that can be classified Abort and
trigger the block freeze; the OCD/ELA capture is read **after** the freeze. But abort does
**not** "halt the Q7 core" via the Q7 halt CSR — it stops the block's *clock* (which
incidentally stops the Q7 with it). `[two-layer split HIGH (different register files,
different drivers); the "fatal Q7 fault → errtrig Abort → freeze" feedback MED · INFERRED —
`FAULTINFO` is a RAS source per `errtrig-fis-routing.md`, but its specific Abort-severity
binding is instantiation-time, not register-encoded — §6 O3.]`

> **NOTE — the final apex→Q7/GIC hop is INFERRED.** A GIC exists in the SoC; the exact wiring
> by which the PEB apex summary reaches a specific Q7/GIC sink is not register-encoded here.
> The GPSIMD compute Q7 is **not** an apex-critical sink — it self-reports via its
> notification ring and self-halts; the fabric freeze stops it via clock-stop.

---

## 5. The `Sunda` bundle across generations `[HIGH · OBSERVED]`

| gen | `Sunda` bitfields | `no_msix` instances | notes |
|---|---|---|---|
| `sunda`        | layout identical; **one description** couples block-logic→SRAM-WP (§3c) | — | baseline that introduced the bundle |
| **`cayman`**   | **authoritative** layout (§1) | **1,070** | decoupled the write-protect side-effect |
| `mariana`      | byte-identical to Cayman | **1,358** | fleet grows; mechanism frozen |
| `mariana_plus` | byte-identical to Cayman | **1,358** | fleet grows; mechanism frozen |
| `maverick` (v5) | byte-identical to Cayman *(schema header-OBSERVED)* | header-present | decentralized INTC fleet, abort stays central |

* **Cross-gen byte-identity** `[HIGH · OBSERVED]`: `jq -S` diff of the `Sunda` bundle gives
  Cayman == Mariana == Mariana+ == Maverick (**IDENTICAL**); `sunda` **differs only** in the
  one Description string of §3c. Positions, names, resets and access are byte-identical
  everywhere.

* **Maverick keeps abort central despite per-IP decentralization** `[HIGH header · MED
  interior]`: Maverick adds a per-IP INTC fleet (`d2d_ll_phy_intc`, `d2d_tl_intc`, …) and an
  `ap_intc` `8grp_msix` IOFIC, **but** `abort_cntl` appears in **only** the central
  `intc_4grp_no_msix_unit` schema — the per-IP INTCs carry **none**. The same bundle is
  re-emitted into the Maverick HAL header `intc_nmi.{yaml,hpp,h}` (the C/header projection of
  the *same* `intc_4grp_no_msix_unit` block — its `sunda:` section sits at
  `_section_offset: 768` = `0x300`, abort_cntl0 @ `0x0`, abort_cntl1 @ `0x4`, identical
  fields), so the freeze bundle is **one centralized unit projected into JSON + HAL header**,
  not a new per-IP variant.

  > **CORRECTION — refine "abort_cntl present ONLY in the JSON".** Maverick's
  > `intc_nmi.{yaml,hpp,h}` HAL headers **do** define `abort_cntl0/1` — but they are the
  > generated HAL *projection* of the same central `intc_4grp_no_msix_unit` block
  > (`_section_offset 768`=`0x300`, byte-identical fields), present in **every** generation's
  > headers (`sunda`/`mariana`/`mariana_plus`/`maverick`). The substantive claim holds: there
  > is exactly **one** abort-freeze unit, not a replicated per-IP fleet. v5-*interior*
  > behavior is INFERRED (header-OBSERVED only).

### 5b. The cross-die (remote) abort substrate = D2D `[HIGH · OBSERVED]`

The local/remote split in `abort_cntl0/1` is physically realized by the **D2D (die-to-die)**
subsystem. The Cayman address map has **288** D2D AMZN (`no_msix`) error-trigger instances
(e.g. `PEB_APB_IO_0_AMZN_IO_D2D_SUBSYS_*_FIS_*_AMZN_ERRTRIG_TRIG_*` →
`intc_4grp_no_msix_unit.json`), **each carrying the `abort_cntl` bundle**. The mechanism: an
abort raised on die A drives a *remote-abort* signal across the D2D link to die B's
error-trigger blocks, whose `remote_abort_scan_dump` / `remote_abort_clock_stop` /
`remote_abort_sram_write_protect` bitmaps then freeze die B's blocks. Maverick (3-die
C_DIE/H_DIE/IO_DIE) generalizes this to a 3-plane remote-abort fabric. `[D2D instances +
remote_* fields HIGH · OBSERVED; the cross-die signal wiring is RTL — MED.]`

---

## 6. What triggers an abort + the fatal-recovery flow `[HIGH · MED]`

### 6a. The trigger — the error-trigger Abort severity wire-OR `[HIGH · OBSERVED · CARRIED]`

The `abort_cntl` freeze is driven by the error-trigger's **Abort severity output** — full
decode in [`errtrig-fis-routing.md`](errtrig-fis-routing.md). The gate register:

* `int_abort_msk_grp` @ `0x30` (RW, rst `0xffffffff`), Description (verbatim): *"Each bit in
  this register masks the corresponding cause bit for generating an Abort signal. Its default
  value is determined by unit instantiation. (Abort = Wire-OR of Cause & !Interrupt_Abort_Mask)"*.

A hardware error sets a bit in `int_cause_grp` (W0C latch, HW-set-wins); if that bit is **not**
masked in `int_abort_msk_grp`, it contributes to the block's **Abort wire-OR**, which drives
the `abort_cntl0/1` freeze logic. Sources that can be classified Abort (per
[`errtrig-fis-routing.md`](errtrig-fis-routing.md)): FIS remapper-DENY (firewall), qos NTS
no-target, delta-mon AXI-integrity, TMU timeout, NSM AXI-integrity (the security fast-path),
notification buffer/AXI faults, and per-block RAS/ECC (incl. uncorrectable DBE / ERG uncerr).

> **NOTE — the per-source Abort assignment is POLICY, not a fixed attribute.** The four
> per-severity masks (error/abort/fatal/log) are **independent**, and the abort mask's
> *"default value is determined by unit instantiation"* (verbatim). Which sources are Abort
> vs Error vs Fatal vs Log is firmware/instantiation policy, not hardware-fixed (§6 O3).

### 6b. Host-initiated + watchdog triggers `[MED · CARRIED]`

* **Host-initiated**: a write to `int_cause_set_grp` @ `0x08` (W1S doorbell) on a `no_msix`
  error-trigger injects a cause; if abort-masked-in, it raises the Abort wire-OR — so a
  privileged management write **can** force an abort. `[W1S inject HIGH · OBSERVED; "used as a
  deliberate host-abort" INFERRED.]`
* **Watchdog**: the FIS `apb_timeout` watchdog and the NSM/qos AXI timeouts, classified Abort,
  freeze a hung/timed-out master.
* **Q7 self-fault**: `FAULTINFOLO.PFatalError` / uncorrectable ECC `FAULTINFOHI` is a RAS
  source that can feed an Abort — the in-core fatal can escalate to the fabric freeze.

### 6c. The fatal-recovery flow `[chain HIGH · ordering MED]`

```c
// Reconstructed end-to-end abort -> freeze -> capture -> reset flow.
// Registers exist and are OBSERVED; the SEQUENCE/ordering and the "recovery = reset"
// conclusion are INFERRED (the schema gives enables + PWRCTL reset, not a scripted
// recovery routine, §6 O4).

void fatal_recovery(void) {
    // 1. SOURCE FIRES: a RAS/security/timeout error sets an errtrig int_cause bit (W0C latch)
    // 2. SEVERITY CLASSIFY: if abort-unmasked, the Abort wire-OR asserts
    //    (int_abort_msk_grp @0x30; see errtrig-fis-routing.md)

    // 3. FREEZE (abort_cntl0/1, §1-§3):
    //      clock_stop  -> gate the block's functional clocks (PEB_PLL_{HBM0,HBM1,SOC} trees)
    //      sram_write_protect -> lock the block's SRAM banks
    //      block_level_logic  -> (local only) drain outstanding transactions
    //      remote_* fields    -> via D2D, propagate a remote-abort to peer dies (§5b)

    // 4. SCAN-DUMP (§2):
    //      scan_dump arms the block's ELA-500 -> frozen state captured into trace RAM
    //      (per-core TRAX + FAULTINFO available over OCD)

    // 5. NOTIFY (in parallel, see errtrig-fis-routing.md / nsm-flow-unified.md):
    //      errtrig nmi_out summary climbs to the PEB apex -> MSI-X -> management core
    //      the GPSIMD compute Q7 self-halts (ErrorHandler); it is NOT an apex-critical sink,
    //      it self-reports via its notification ring; the fabric freeze stops it via clock-stop

    // 6. RECOVER (no single "abort-clear" register; recovery == reset + re-init):
    //      post-mortem readback: ELA RAM via RRAR/RRDR; per-core FAULTINFO/TRAX/DSR over JTAG/APB
    //      reset:  xtensa_q7 PWRCTL.CoreReset[16] / DebugReset[28]   (per-core)
    //      SoC re-enables clocks (un-gate clock_stop) + clears latched int_cause (W0C) + re-arms
}
```

```text
ABORT / SCAN-DUMP / CLOCK-STOP CONTROL
  [RAS/ECC | FIS deny/NTS | nsm AXI | timeout | host W1S inject | Q7 PFatalError]
                                 |
                                 v   errtrig int_cause_grp (W0C latch)
                    int_abort_msk_grp@0x30 gates  ->  ABORT WIRE-OR
                                 |
              +------------------+----------------------------- D2D link -> remote die
              v                                                            |
   Sunda bundle @0x300 (no_msix ONLY; HalExists=NON_EX_ONLY)               v
     abort_cntl0: scan_dump(L/R)[7:0]/[15:8] + clock_stop(L/R)[23:16]/[31:24]
     abort_cntl1: block_level_logic(L)[7:0]  + sram_write_protect(L/R)[15:8]/[23:16]
              |            |                  |                |
              v            v                  v                v
        CLOCK-STOP    SCAN-DUMP          SRAM-WRITE-       BLOCK-LEVEL
        (gate the     -> ELA-500         PROTECT (lock     LOGIC (drain
        block clocks) capture RAM        SRAM banks)       outstanding txns)
              |            |  (per-core: TRAX + FAULTINFO over OCD)
              |            v
              |     post-mortem readback (RRAR/RRDR + DSR/StopCause over JTAG/APB)
              v
        RECOVER: PWRCTL.CoreReset/DebugReset + SoC clock un-gate + W0C cause clear
```

---

## 7. Confidence / provenance ledger

**`[HIGH · OBSERVED]`** (byte-read this pass via jq / rg / objdump):

* The `Sunda` bundle @ `0x300` (`BundleSizeInBytes 0xf0`, `ArraySize 1`,
  `HalExists NON_EX_ONLY`) inside `intc_4grp_no_msix_unit`; `abort_cntl0` @ `0x300`
  {`local/remote_abort_scan_dump`[7:0]/[15:8], `local/remote_abort_clock_stop`[23:16]/[31:24]}
  all RW rst 0; `abort_cntl1` @ `0x304` {`local_abort_to_block_level_logic`[7:0],
  `local/remote_abort_sram_write_protect`[15:8]/[23:16], `reserved`[31:24]} all RW rst 0;
  `spare0`/`spare1` @ `0x308`/`0x30c` (rst `0x0` / `0xffffffff`).
* The `no_msix`-only property: the `msix` twin's `Sunda` bundle holds **only** `MSIX_TC` @
  `0x3e0` (no abort control); `msix` `int_abort_msk_grp` + `int_cdc_bypass_grp` = **RO**,
  `no_msix` = RW.
* `int_abort_msk_grp` @ `0x30` (rst `0xffffffff`) = the Abort wire-OR mask.
* ELA-500 identity (`ARCHID 0xa75`, `DEVTYPE 0x75`, `PIDR0 0xb8`, `ARCHITECT 0x23b`) + the
  capture register set; **1,318** instances partitioning **exactly** into `SPROT_ELA 528` /
  `INTERNAL_ELA 456` / `APP_ELA 218` / `NTS_ELA 116`.
* Cross-gen byte-identity of the `Sunda` bundle (Cayman == Mariana == Mariana+ == Maverick;
  `sunda` differs in one Description string); `no_msix` census (Cayman 1,070 / Mariana 1,358 /
  Mariana+ 1,358); Maverick: `abort_cntl` only in the central unit (+ its HAL-header
  projection), not in the per-IP INTCs.
* The D2D substrate: **288** D2D AMZN `no_msix` error-trigger instances each carrying
  `abort_cntl`; the `PEB_PLL_{HBM0,HBM1,SOC}` clock sources.
* `FAULTINFOLO` @ `0x302c` (`PFatalError[31]` …) / `FAULTINFOHI` @ `0x3030` (17 ECC fields);
  `PWRCTL.CoreReset[16]`/`DebugReset[28]`; `DSR.Stopped[4]`/`StopCause[8:5]`;
  `instr_halt_ctrl` @ `0x14` `halt_req[0]`; the EXT_BREAK disasm reading `0x14`.

**`[· CARRIED]`** (consolidated, not re-derived): the Abort wire-OR + 4 severity masks +
W0C/W1S cause semantics, the apex `nmi_out` summary climb
([`errtrig-fis-routing.md`](errtrig-fis-routing.md),
[`nsm-flow-unified.md`](nsm-flow-unified.md)); the polled-not-vectored GPSIMD Q7 + the two-Q7
split ([`q7-surprises-binding.md`](q7-surprises-binding.md)); the SEQ error-handler/run-state
([`../../firmware/seq/error-handler.md`](../../firmware/seq/error-handler.md),
[`../../firmware/seq/run-state.md`](../../firmware/seq/run-state.md)); the **error-trigger PAIR
= 962 for Cayman** (carried from the errtrig page, **not** re-derived here).

**`[MED · INFERRED]`**: the "8-bit field = up to 8 freeze targets" reading; the abort →
ELA-trigger-input wiring (RTL); the freeze ordering (clock-stop → write-protect → scan-dump);
the "recovery = reset + clock un-gate + W0C clear" flow; host-W1S-as-abort and
watchdog-as-abort; the "fatal Q7 fault → errtrig Abort → freeze" escalation; the apex→Q7/GIC
final hop.

**`[LOW]` / OPEN (explicit non-claims):**

* **O1** — the per-bit **silicon target map** (which clock domain / scan chain / SRAM bank
  each of the 8 bits selects) is RTL, not register-encoded.
* **O2** — the **abort-trigger → ELA-500-trigger-input** netlist wiring (which abort drives
  which ELA's `PTACTION`/`TSSR`) is RTL, not in the address map or schema.
* **O3** — the **per-source Abort-severity assignment** (which cause bits are abort-masked-in
  by default) is instantiation-time, not register-encoded.
* **O4** — the exact **fatal-recovery sequence** (the scripted order of reset / clock un-gate /
  cause-clear / re-arm) is firmware/management-owned, not register-encoded.

**v5/Maverick** = schema header + `al_address_map_db.json` **OBSERVED**; v5-*interior*
placement/behavior behind an address is **INFERRED**.
