# CSR — Xtensa Q7 Debug / Trace / PMU / OCD

This page documents the **Q7 ("Cayman") DSP-core debug aperture** — the
memory-mapped register file that exposes the Cadence Tensilica Vision-Q7 core's
**Debug / Trace / Performance-Monitor / On-Chip-Debug / CoreSight** blocks to an
external debug host. It is the *silicon's own* debugger surface (driven over the
debug bus, not by Q7 firmware): a JTAG/APB master halts the core, injects
instructions, and reads back state through this 16 KiB window.

Everything below is read directly from the **shipped Cayman register descriptor**
`xtensa_q7.json` — a registry/SVD-style JSON (147,698 bytes, dated 2022-12-14)
that ships inside the `cayman-arch-regs` archive and is duplicated under the
customop package's `arch-headers/{mariana,mariana_plus,maverick}/.../csrs/`
trees. The descriptor is a plain-text generator artifact emitted by the build;
it is a citeable binary-derived source. Every register name, offset, bit
position, access type, reset value, and the *verbatim* field Description strings
quoted here are read from that JSON. No disassembly is needed for the register
map itself. The page default is `[HIGH/OBSERVED]`; the architectural tie-in (how
a debugger drives the block) is labelled **INFERRED** where it goes beyond the
descriptor text, and every claim that departs from the default carries an
explicit tag.

> **Generation:** This is the **`ncore2gp` (Cayman / NC-v3) Q7** debug aperture,
> byte-grounded from `xtensa_q7.json` as shipped in the customop package
> (`aws-neuronx-gpsimd-customop-lib_0.21.2.0`). The same descriptor appears under
> the `mariana`, `mariana_plus`, and `maverick/vpc-mirror` arch-header trees with
> identical content, so the block is carried forward across those packaged gens.
> Any **v5 / MAVERICK** projection is flagged **INFERRED** — it is the *same JSON*
> in the maverick tree, but a silicon-level v5 confirmation is not in hand.
> `[HIGH/OBSERVED; per-gen carry CARRIED]`

---

## 1. Aperture shape — five bundles, 78 registers, 296 fields

The descriptor is a single `RegFile{}` (`Type=REGFILE`, `UnitName=xtensa_q7`)
whose `RegistersBundleArrays[]` holds **five** bundles. Each bundle is an
`ArraySize=1` group with its own `AddressOffset` (the bundle base) and
`BundleSizeInBytes`; each register's `AddressOffset` is **relative to its bundle
base**, so the absolute offset is `bundle.base + register.offset`. Access flows
over **APB** (`InterfaceType=APB`), the file is `POSEDGE` (`RegfileFlavor`), and
the declared aperture is **14-bit-addr / 32-bit-data / 0x4000 bytes**.

> **CORRECTION — the RegFile scalars are populated, not null.** The RegFile-level
> `AddrWidth / DataWidth / SizeInBytes` are sometimes reported as *"ALL null"*,
> with the 0x4000 span only *implied* by the bundle layout. In the JSON these
> fields are in fact **populated**: `AddrWidth="14"`, `DataWidth="32"`,
> `SizeInBytes="0x4000"`, `InterfaceType="APB"`, `RegfileFlavor="POSEDGE"`. The
> 0x4000 size is therefore **stated explicitly**, not merely implied — and it
> agrees with the bundle arithmetic (`0x3F00 + 0x100 = 0x4000`). The count table
> below is unaffected.

| Bundle | Base | Size | Regs | Fields | Block |
|---|---|---|---|---|---|
| `Trax_Registers`                | `0x0000` | `0x1000` | 12 | 63  | TRAX instruction-trace unit (CoreSight ATB source) |
| `Performance_Monitor_Registers` | `0x1000` | `0x1000` | 26 | 107 | 8-counter PMU (on-core profiling) |
| `OCD_Registers`                 | `0x2000` | `0x1000` | 15 | 70  | On-Chip-Debug: halt / instruction-injection / DSR |
| `Miscellaneous_Registers`       | `0x3000` | `0x0F00` | 5  | 36  | power/reset, ECC/RAS fault info |
| `CoreSight_Registers`           | `0x3F00` | `0x0100` | 20 | 20  | CoreSight ROM-table identification |

**Verified totals:**
**5 bundles · 78 registers · 296 bitfields**; per-bundle field totals
`63 + 107 + 70 + 36 + 20 = 296`; register count `12 + 26 + 15 + 5 + 20 = 78`.
Register-level access split **RW=62 / RO=16** (no WO, no Reserved); field-level
**RW=147 / RO=149**. Every register has ≥1 field; no within-bundle offset
collision. The aperture is `[0x0000 .. 0x4000)`.

> **NOTE — bundle base ≠ first register offset.** Two bundles are *sparse* at
> their base. `Miscellaneous_Registers` is based at `0x3000` but its first
> register (`PWRCTL`) sits at rel `0x20` (abs `0x3020`); `CoreSight_Registers` is
> based at `0x3F00`, starts with `ITCTRL` @ rel `0x00` (`0x3F00`), then jumps to
> rel `0xA0`. Always compute `base + rel`.

---

## 2. Performance-Monitor (PMU) block — `0x1000`, 26 regs

This is the standard Xtensa **eight-counter** performance monitor — the on-core
profiling surface a firmware perf driver (or the host) programs to sample
GPSIMD/SEQ workload events (cycles, stalls, cache/RAM-ECC events, branch
events). The 26 registers decompose as **1 global enable + 1 PC snapshot + 8
counters + 8 selectors + 8 status**.

| Reg | Abs | Acc | Key fields (verbatim semantics) |
|---|---|---|---|
| `PMG`      | `0x1000` | RW | `PMEN[0]` — *"Overall enable for all performance counting"*. `[31:1]` reserved. |
| `INTPC`    | `0x1010` | RW¹ | `INTPC[31:0]` (RO field) — *"The PC (or the last-known good PC) at the time that the performance counter interrupt was asserted"*. |
| `PM0..PM7` | `0x1080..0x109c` | RW | `PMn[31:0]` — *"Performance monitor n value"*; eight free-running 32-bit event counters. |
| `PMCTRL0..7` | `0x1100..0x111c` | RW | one selector per counter (field table below). |
| `PMSTAT0..7` | `0x1180..0x119c` | RW | per-counter status: `OVFL[0]`, `INTASRT[4]`. |

¹ `INTPC` carries register-level access `RW` in the JSON but its single field is
RO — the value is hardware-latched.

**`PMCTRLn` selector layout** (identical across all eight; `0x4`-stride):

| Bits | Field | Acc | Verbatim Description |
|---|---|---|---|
| `[0]`     | `INTEN`      | RW | *"Enables assertion of PerfMonInt output when overflow happens."* |
| `[2:1]`   | `RESERVED0`  | RO | reserved |
| `[3]`     | `KRNLCNT`    | RW | *"Enables counting when EXECLEVEL > TRACESCOPE (… if set, counts only when EXECLEVEL > TRACESCOPE; if cleared, counts only when EXECLEVEL ≤ TRACESCOPE)."* |
| `[6:4]`   | `TRACESCOPE` | RW | *"Compares this value to EXECLEVEL when deciding whether to count."* |
| `[7]`     | `RESERVED1`  | RO | reserved |
| `[12:8]`  | `SELECT`     | RW | *"Selects input to be counted by the counter."* (5-bit event-class selector → up to 32 categories.) |
| `[15:13]` | `RESERVED2`  | RO | reserved |
| `[31:16]` | `MASK`       | RW | *"Selects input subsets to be counted (counter will increment only once even if more than one condition corresponding to a mask bit occurs)."* |

**`PMSTATn` status layout**: `OVFL[0]` — *"Counter Overflow. Sticky bit set when
a counter rolls over from 0xffffffff to 0x0"*; `INTASRT[4]` — *"This counter's
overflow caused PerfMonInt to be asserted"*; `[3:1]`, `[31:5]` reserved.

**Event model.** `SELECT` picks an event *category*, `MASK` refines it to a
bitwise subset within that category, and `{KRNLCNT, TRACESCOPE}` privilege-gate
counting against the core's `EXECLEVEL`. The eight counters are independent; any
subset can raise the shared **`PerfMonInt`**, with `PMSTATn.INTASRT`
distinguishing the source and `INTPC` giving the PC at assertion.

> **GOTCHA — no event catalogue in this JSON.** The field *layout* is byte-exact,
> but the concrete `SELECT`/`MASK` event encodings are **not** enumerated anywhere
> in the descriptor (no value table). The numeric meaning of, e.g., `SELECT=5` is
> **not recoverable from this file** — it is the Xtensa PMU event table, external
> to the register map. Treat any specific event number as **LOW / INFERRED**.
> `[layout HIGH/OBSERVED; encodings LOW]`

### 2.1 C pseudocode — arm a counter and read it

```c
/* Q7 PMU aperture (this RegFile, bundle Performance_Monitor @ +0x1000).
 * APB-mapped; debug host or a privileged firmware perf driver writes these.
 * Offsets are byte-exact from xtensa_q7.json. `csr` is a u32* base pointer. */
#define PMU            0x1000u
#define PMU_PMG        (PMU + 0x000u)              /* global enable          */
#define PMU_INTPC      (PMU + 0x010u)              /* PC at overflow (RO)    */
#define PMU_PM(n)      (PMU + 0x080u + 4u*(n))     /* counter value   n=0..7 */
#define PMU_PMCTRL(n)  (PMU + 0x100u + 4u*(n))     /* selector        n=0..7 */
#define PMU_PMSTAT(n)  (PMU + 0x180u + 4u*(n))     /* status          n=0..7 */

#define PMCTRL_INTEN        (1u << 0)
#define PMCTRL_KRNLCNT      (1u << 3)
#define PMCTRL_TRACESCOPE(v)(((v) & 0x7u) << 4)
#define PMCTRL_SELECT(v)    (((v) & 0x1Fu) << 8)    /* event-class selector  */
#define PMCTRL_MASK(v)      (((v) & 0xFFFFu) << 16) /* sub-event mask        */
#define PMSTAT_OVFL         (1u << 0)
#define PMSTAT_INTASRT      (1u << 4)

/* Arm counter `n` for one event class, optionally raising PerfMonInt on
 * overflow, and gating by privilege level (count only when EXECLEVEL>scope). */
static void pmu_arm(volatile uint32_t *csr, unsigned n,
                    uint32_t select, uint32_t mask,
                    unsigned scope, bool kernel_only, bool irq_on_ovf)
{
    csr[PMU_PMSTAT(n)/4] = 0;                 /* clear sticky OVFL/INTASRT   */
    csr[PMU_PM(n)/4]     = 0;                 /* zero the counter            */
    uint32_t ctl = PMCTRL_SELECT(select) | PMCTRL_MASK(mask)
                 | PMCTRL_TRACESCOPE(scope);
    if (kernel_only) ctl |= PMCTRL_KRNLCNT;  /* count only above `scope`    */
    if (irq_on_ovf)  ctl |= PMCTRL_INTEN;    /* PerfMonInt on 2^32 rollover */
    csr[PMU_PMCTRL(n)/4] = ctl;
    csr[PMU_PMG/4]       = 1u;               /* PMEN: master-enable ALL     */
}

/* Read counter `n`; report sticky overflow (and the latched PC if it fired). */
static uint32_t pmu_read(volatile uint32_t *csr, unsigned n,
                         bool *overflowed, uint32_t *ovf_pc)
{
    uint32_t v  = csr[PMU_PM(n)/4];
    uint32_t st = csr[PMU_PMSTAT(n)/4];
    *overflowed = (st & PMSTAT_OVFL) != 0;
    if (*overflowed && (st & PMSTAT_INTASRT))
        *ovf_pc = csr[PMU_INTPC/4];           /* PC at PerfMonInt assert     */
    return v;
}
```

---

## 3. OCD / Debug block — `0x2000`, 15 regs

On-Chip-Debug is the **halt-mode** debugger driven by the external JTAG/APB
master. Its core mechanism is **instruction injection**: halt the core, stuff an
arbitrary Xtensa instruction word into `DIR0..7`, then trigger execution; the
stopped core runs it and exchanges operands through `DDR`. This triad —
**`DIR` + `DDR` + `DSR`** — is how a host single-steps and reads/writes the
core's address/special registers and memory.

| Reg | Abs | Acc | Role (verbatim where quoted) |
|---|---|---|---|
| `OCDID`     | `0x2000` | RW | ID/version: `CFGID=1538`, `STDCFG=1`, `MAJVER=4`, `MINVER=1`, `PRODNO=0` (OCD). |
| `DCRCLR`    | `0x2008` | RW | write-1-to-**clear** alias of the Debug Control Register. |
| `DCRSET`    | `0x200c` | RW | write-1-to-**set** alias of the Debug Control Register. |
| `DSR`       | `0x2010` | RW | Debug Status Register — the richest reg (25 fields). |
| `DDR`       | `0x2014` | RW | *"Debug data register, for host to/from target transfers"*. |
| `DDREXEC`   | `0x2018` | RW | *"Alias to DDR, executes DIR when accessed… Writable only when in OCD mode and DSR.Stopped is set"*. |
| `DIR0EXEC`  | `0x201c` | RW | *"Alias to DIR0, executes the instruction when written… Writable only when in OCD mode and DSR.Stopped is set"*. |
| `DIR0`      | `0x2020` | RW | *"Debug instruction register, first 32 bits…"*. |
| `DIR1..DIR7`| `0x2024..0x203c` | RW | *"Debug instruction register, remaining bits."* (wide instructions). |

> **QUIRK — there is no read-back "DCR" register.** The Debug Control Register is
> exposed only as the **`DCRSET` / `DCRCLR`** set/clear pair (abs `0x200c` /
> `0x2008`). To toggle a control bit you write a 1 to the matching position in the
> appropriate alias; there is no single readable DCR in this aperture. Both
> aliases carry the **same 14-field** layout.

**`DCRSET` / `DCRCLR` control fields** (identical 14 fields each):

| Bit | Field | Reset | Meaning (verbatim) |
|---|---|---|---|
| `0`  | `EnableOCD`         | 0 | *"Set to activate the OCD."* |
| `1`  | `DebugInterrupt`    | 0 | *"Set to break the core (same as DSR.DebugPendHost)"* — the host-forced halt. |
| `2`  | `InterruptAllConds` | 0 | *"Set to allow debug interrupts to supersede all conditions"*. |
| `16` | `BreakInEn`         | 0 | *"Enable BreakIn"* (cross-core break pin). |
| `17` | `BreakOutEn`        | 0 | *"Enable BreakOut"*. |
| `20` | `DebugSwActive`     | 0 | *"A software-set flag that indicates user-controlled debug mode"*. |
| `21` | `OCDDebugStallInEn` | **1** | *"Enable the OCDDebugStall input"*. |
| `22` | `DebugModeOutEn`    | **1** | *"Enable the XOCDMode output"*. |
| `24` | `BreakOutITO`       | 0 | *"Enable BreakOutITO"*. |
| `25` | `BreakInAckITO`     | 0 | *"BreakInAck topology detection control bit"*. |

(`[15:3]`, `[19:18]`, `[23]`, `[31:26]` reserved.)

**`DSR` — Debug Status Register** (abs `0x2010`, 25 fields). The handshake +
cause vector the host polls after halting:

| Bit | Field | Meaning (verbatim, condensed) |
|---|---|---|
| `0`     | `ExecDone`        | *"Instruction in DIR completed execution (w/ or w/o exception)"*. |
| `1`     | `ExecException`   | *"A previous instruction in DIR completed with an exception"*. |
| `2`     | `ExecBusy` (RO)   | *"Core is executing DIR (meaningful while Stopped is 1)"*. |
| `3`     | `ExecOverrun`     | *"DIR execution attempted while previous execute still busy"*. |
| `4`     | `Stopped` (RO)    | *"Core is under OCD debug control, in Stopped or executing DIR"*. |
| `8:5`   | `StopCause` (RO)  | *"Reason for stopping."* (encodings **not** enumerated → LOW). |
| `10`    | `CoreWroteDDR`    | core executed `WSR.DDR`/`XSR.DDR`. |
| `11`    | `CoreReadDDR`     | core executed `RSR.DDR`/`XSR.DDR`. |
| `14`    | `HostWroteDDR`    | host wrote DDR (via JTAG/APB; incl. `DDREXEC`). |
| `15`    | `HostReadDDR`     | host read DDR. |
| `16`    | `DebugPendBreak`  | *"Debug interrupt pending due to BreakIn signal"*. |
| `17`    | `DebugPendHost`   | *"…due to DCR.DebugInterrupt"*. |
| `18`    | `DebugPendTrax`   | *"…due to TRAX PTO"* — a trace trigger breaking the core. |
| `20`    | `DebugIntBreak`   | debug interrupt **taken** due to BreakIn. |
| `21`    | `DebugIntHost`    | taken due to `DCR.DebugInterrupt`. |
| `22`    | `DebugIntTrax`    | taken due to TRAX PTO. |
| `23`    | `RunStallToggle`  | RunStall polarity changed. |
| `24`    | `OCDDebugStallInputValue` (RO) | real-time RunStall input value. |
| `25/26` | `BreakOutAckITI` / `BreakInITI` (RO) | break-topology detection bits. |
| `31`    | `DebugOn` (RO, reset 1) | *"Always 1. (Read as zero when the Debug module is powered off.)"* |

> **GOTCHA — IBREAK / DBREAK are NOT in this aperture.** The brief asks for
> "IBREAK/DBREAK-style breakpoint/watchpoint registers." On Vision-Q7 the
> hardware instruction-breakpoint (`IBREAKA`/`IBREAKENABLE`) and data-watchpoint
> (`DBREAKA`/`DBREAKC`) registers are core **special registers (SRs)**, reached
> with `RSR`/`WSR`, **not** memory-mapped CSRs — so they do **not** appear in
> `xtensa_q7.json`. The breakpoint mechanism *visible in this aperture* is twofold:
> (a) the **TRAX PC-match** trigger (`TRIGGERPC` + `PCMATCHCTRL` @ `0x14`/`0x18`,
> §4) which can raise a debug interrupt via `DSR.DebugPendTrax`, and (b) the host
> setting an SR-based breakpoint **by injecting a `WSR.IBREAKA`** through the
> `DIR`/`DDREXEC` path. A debugger that wants an IBREAK programs it *through* this
> OCD surface, not *in* it. `[register absence HIGH/OBSERVED; SR mechanism INFERRED]`

### 3.1 C pseudocode — halt, inject, and set a hardware breakpoint

```c
#define OCD            0x2000u
#define OCD_DCRSET     (OCD + 0x00cu)
#define OCD_DCRCLR     (OCD + 0x008u)
#define OCD_DSR        (OCD + 0x010u)
#define OCD_DDR        (OCD + 0x014u)
#define OCD_DDREXEC    (OCD + 0x018u)
#define OCD_DIR0EXEC   (OCD + 0x01cu)
#define OCD_DIR0       (OCD + 0x020u)

#define DCR_EnableOCD       (1u << 0)
#define DCR_DebugInterrupt  (1u << 1)   /* host-forced break               */
#define DSR_ExecDone        (1u << 0)
#define DSR_Stopped         (1u << 4)
#define DSR_StopCause(d)    (((d) >> 5) & 0xFu)

/* Halt the core and confirm it is stopped (poll DSR.Stopped). */
static void ocd_halt(volatile uint32_t *csr)
{
    csr[OCD_DCRSET/4] = DCR_EnableOCD | DCR_DebugInterrupt;
    while (!(csr[OCD_DSR/4] & DSR_Stopped)) { /* spin on debug bus */ }
}

/* Inject one Xtensa instruction (DIR0..n) and run it on the stopped core.
 * `inj_load_ddr` pre-loads an operand into DDR (so e.g. an injected RSR.DDR
 * can move a host value into an AR before the next op consumes it). */
static uint32_t ocd_exec(volatile uint32_t *csr, uint32_t instr,
                         bool inj_load_ddr, uint32_t ddr_in)
{
    if (inj_load_ddr) csr[OCD_DDR/4] = ddr_in;
    csr[OCD_DIR0/4]     = instr;        /* (load DIR1..7 first for wide ops) */
    csr[OCD_DIR0EXEC/4] = instr;        /* write to *EXEC alias triggers run */
    while (!(csr[OCD_DSR/4] & DSR_ExecDone)) { /* wait for ExecDone */ }
    return csr[OCD_DDR/4];              /* result the injected op wrote to DDR */
}

/* Program an instruction breakpoint at `pc` into the core's IBREAK SRs by
 * injecting WSR/RSR.DDR sequences. IBREAKA/IBREAKENABLE are SRs (not CSRs),
 * so they are set THROUGH this OCD aperture, not by an MMIO write. */
static void ocd_set_ibreak(volatile uint32_t *csr, unsigned slot, uint32_t pc)
{
    ocd_halt(csr);
    ocd_exec(csr, XT_RSR_DDR_A3,  /*load*/true, pc);        /* a3 <- pc       */
    ocd_exec(csr, XT_WSR_IBREAKA(slot, /*ar=*/3), false, 0);/* IBREAKA[slot]  */
    ocd_exec(csr, XT_RSR_DDR_A3,  true, (1u << slot));      /* a3 <- 1<<slot  */
    ocd_exec(csr, XT_WSR_IBREAKENABLE_A3, false, 0);        /* enable slot    */
    csr[OCD_DCRCLR/4] = DCR_DebugInterrupt;                 /* resume; self-  */
    /* core now self-breaks (DSR.StopCause) when PC == pc.    breaks on hit  */
}
```

> The exact `WSR.IBREAKA` / `RSR.DDR` opcode words (`XT_*`) are the Xtensa
> base-ISA encodings and are not in this CSR descriptor; the *sequence shape*
> (DDR-handshake → DIR-inject → `ExecDone` poll) is byte-grounded by the `DDR`/`DIR`
> field semantics above. `[sequence INFERRED; field semantics HIGH/OBSERVED]`

---

## 4. TRAX — instruction-trace block — `0x0000`, 12 regs

TRAX is the **compressed branch-trace** unit. It writes a local **TraceRAM** and,
when an ATB is configured, streams onto the ARM **CoreSight ATB** trace bus. Its
PC-match comparator doubles as a trace-stop trigger that can break the core into
the OCD debugger (`DSR.DebugPendTrax`, §3).

| Reg | Abs | Acc | Role / key fields |
|---|---|---|---|
| `TRAXID`       | `0x00` | RW | ID/version: `CFGID=27136` (`0x6A00`), `MAJVER=4`, `MINVER=1`, `PRODNO=0` (TRAX). |
| `TRAXCTRL`     | `0x04` | RW | 24 fields: `TREN[0]` start, `TRSTP[1]` stop, `PCMEN[2]` PC-match stop trigger, `PTIEN[4]`/`CTIEN[5]` proc/cross-trigger inputs, `TMEN[7]` TraceRAM enable (reset 1), `CNTU[9]` count-units, `TSEN[11]` timestamps, `SMPER[14:12]` sync period, `PTOWT/PTOWS/CTOWT/CTOWS` trigger-output timing, `ITCTO/ITCTIA` integration, `ATID_0[24]`/`ATID6_1[30:25]`(reset 60=0x3C)/`ATEN[31]`(reset 1) ATB source-ID + enable. |
| `TRAXSTAT`     | `0x08` | RW | 16 fields: `TRACT[0]` trace-active, `TRIG[1]` stop-triggered, `PCMTG[2]`/`PTITG[4]`/`CTITG[5]` trigger-source flags, `PJTR[3]` JTAG-txn result, `MEMSZ[12:8]`=13 (TraceRAM = 2^13 B = **8 KiB**), `PTO[16]`/`CTO[17]` trigger-output, `ITCTOA/ITCTI/ITATR` integration observation. |
| `TRAXDATA`     | `0x0c` | RW | 32-bit TraceRAM word at `TRAXADDR.TADDR`. |
| `TRAXADDR`     | `0x10` | RW | `TADDR[10:0]` word index (2048 words ⇔ 8 KiB), `TWRAP[30:21]` wrap count, `TWSAT[31]` wrap-overflow. |
| `TRIGGERPC`    | `0x14` | RW | *"Trax Trigger PC value"* — the PC-match breakpoint value. |
| `PCMATCHCTRL`  | `0x18` | RW | `PCML[4:0]` match-mask length (low bits to ignore), `PCMS[31]` sense (in-range vs out-of-range). |
| `DELAYCNT`     | `0x1c` | RW | `DELAYCNT[23:0]` post-trigger countdown. |
| `MEMADDRSTART` | `0x20` | RW | `[10:0]` TraceRAM window start. |
| `MEMADDREND`   | `0x24` | RW | `[10:0]` TraceRAM window end (reset **2047**, = 8 KiB top). |
| `EXTTIMELO`    | `0x40` | RW¹ | RO field: external-timestamp low 32 bits (`DebugExtTime` pins). |
| `EXTTIMEHI`    | `0x44` | RW¹ | RO field: external-timestamp high 32 bits. |

¹ register-level `RW`, single field `RO` (hardware-driven from the timestamp
pins).

**Trace → debug coupling.** `TRAXCTRL.PTOWT`/`PTOWS` raise the Processor Trigger
Output (`TRAXSTAT.PTO`), which the descriptor's own text says *"is latched into
OCD register bit DSR.DebugPendTrax when a TRAX trigger causes a debug
interrupt."* So a PC-range trace trigger (`TRIGGERPC` + `PCMATCHCTRL`) is, in
effect, the aperture's **hardware PC breakpoint** — it both stops the trace and
can halt the core through §3's DSR vector.

---

## 5. Miscellaneous — power / reset / ECC-RAS — `0x3000`, 5 regs

Sparse bundle (first reg at rel `0x20`). It carries the **debug-side reset
controls** and the **ECC/RAS fault-reporting** surface.

| Reg | Abs | Acc | Key fields (verbatim where quoted) |
|---|---|---|---|
| `PWRCTL`      | `0x3020` | RW | `CoreReset[16]` *"asserts reset to the core"*, `DebugReset[28]` *"asserts reset to the Xtensa Debug module"*. |
| `PWRSTAT`     | `0x3024` | RW | `reserved[15:0]` reset **0x1111** (*"bit 0,4,8,12 tied high in configs without PSO"*), `CoreWasReset[16]`(reset 1), `DebugWasReset[28]`(reset 1). |
| `ERISTAT`     | `0x3028` | RW | `WRISUC[0]` (RO) *"ERI write success indication"*. |
| `FAULTINFOLO` | `0x302c` | RW | `UserCode[23:20]`(RW), `HaltCode[27:24]` *"provided by HALT instruction"*, `TE[28]` *"Triple exception"*, `DE[29]` *"Double exception"*, `Halted[30]`, `PFatalError[31]` *"Sticky fatal error"*. |
| `FAULTINFOHI` | `0x3030` | RW | **17 ECC flags** across DataRAM/InstrRAM/ICache/DPref-RAM/IPref-RAM (Corr/Unc/Refl/Cln/Drt variants), plus `ECCTstMde[19]`. The ECC/RAS reporting register. |

---

## 6. CoreSight identification — `0x3F00`, 20 regs

Standard CoreSight management + ROM-table identity; each register is a single
32-bit `val` field. The meaningful contents are the **ID registers**:

| Reg | Abs | reset | Reg | Abs | reset |
|---|---|---|---|---|---|
| `ITCTRL`     | `0x3F00` | `0xb1`† | `Peripheral_ID4` | `0x3FD0` | `0x24` |
| `CLAIMSET`   | `0x3FA0` | `0xb1`† | `Peripheral_ID5` | `0x3FD4` | `0x00` |
| `CLAIMCLR`   | `0x3FA4` | `0xb1`† | `Peripheral_ID6` | `0x3FD8` | `0x00` |
| `LOCKACCESS` | `0x3FB0` | `0xb1`† | `Peripheral_ID7` | `0x3FDC` | `0x00` |
| `LOCKSTATUS` | `0x3FB4` | `0xb1`† | `Peripheral_ID0` | `0x3FE0` | `0x03` |
| `AUTHSTATUS` | `0x3FB8` | `0xb1`† | `Peripheral_ID1` | `0x3FE4` | `0x21` |
| `DEVID`      | `0x3FC8` | `0xb1`† | `Peripheral_ID2` | `0x3FE8` | `0x0f` |
| `DEVTYPE`    | `0x3FCC` | `0xb1`† | `Peripheral_ID3` | `0x3FEC` | `0x00` |
| `Component_ID0` | `0x3FF0` | `0x0d` | `Component_ID1` | `0x3FF4` | `0x90` |
| `Component_ID2` | `0x3FF8` | `0x05` | `Component_ID3` | `0x3FFC` | `0xb1` |

`ITCTRL`, `CLAIMSET`, `CLAIMCLR`, `LOCKACCESS` are **RW**; the remaining 16 are
**RO**. The 12 `*_ID` registers' resets decode to a coherent CoreSight identity:

- **Component-ID preamble** `0x0D,0x90,0x05,0xB1` → valid CoreSight component
  (CID1[7:4]=0x9 = "CoreSight component").
- **PIDR decode:** part number `0x103`; JEP106 = continuation 4, code `0x72`
  (**Tensilica / Cadence** JEDEC DES); 4 KiB block count = `2^2` = four 4 KiB
  regions. `[decode HIGH/OBSERVED; vendor-name mapping of JEP106 0x72 MED]`

> **CORRECTION-class NOTE — eight placeholder resets.** `ITCTRL`, `CLAIMSET`,
> `CLAIMCLR`, `LOCKACCESS`, `LOCKSTATUS`, `AUTHSTATUS`, `DEVID`, `DEVTYPE` (the `†`
> rows above) all carry reset `0x000000b1` — the **same byte as the legitimate
> `Component_ID3`**. This is almost certainly a **generator default** (the JSON had
> no real reset and reused `0xB1`), **not** a true silicon reset. Treat those eight
> resets as **UNVERIFIED / LOW**; their offsets/names/access remain **HIGH**.
> `[reset LOW; offset/name/access HIGH/OBSERVED]`

---

## 7. Architectural tie-in & relationships

### 7.1 Three trigger surfaces, one debug-pending vector

The aperture has **three** ways to break the core, and all converge on `DSR`'s
pending/taken vectors (§3): the **OCD** host-forced `DCR.DebugInterrupt` (→
`DSR.DebugPendHost`/`DebugIntHost`) and the BreakIn pin (→ `DebugPendBreak`); the
**TRAX** PC-match output `PTO` (→ `DebugPendTrax`/`DebugIntTrax`); and indirectly
the **PMU** overflow `PerfMonInt`. A halt-mode debugger built on this block: (1)
arm a stop (`DCRSET.DebugInterrupt`, a TRAX PC-match, or an injected `IBREAK`);
(2) poll `DSR.Stopped`; (3) read/write core state through `DIR`+`DDR` injection;
(4) single-step by injecting one instruction at a time via `DIR0EXEC`/`DDREXEC`.
`[INFERRED — interpretation of the field set]`

### 7.2 Q7 vs NX

This is the **Cayman / NC-v3 Q7** debug aperture. The diff variant — the NX core
register file — is documented separately; the two share the Cadence OCD/TRAX/PMU
heritage but differ in counts and a handful of fields. See
[xtensa-nx.md](xtensa-nx.md) for the **NX-vs-Q7** delta.

### 7.3 This aperture vs the SEQ firmware debugger

> **NOTE — two unrelated "debuggers."** The SEQ micro-engine has its *own*
> firmware single-step / breakpoint engine (the `hw_decode` CSRs at `0x04004000`),
> reconstructed in the [SEQ uarch debugger page](../../firmware/seq/uarch-debugger.md).
> That engine is the **custom** Annapurna design and — per a `const16`-build scan of
> the SEQ image — does **not** touch this Q7 OCD/TRAX/PMU aperture at all. This
> page's block is the **silicon's own** Cadence debugger, driven over the external
> debug bus (APB/JTAG), orthogonal to the SEQ firmware debugger. Do not conflate
> the `0x2000` OCD base here with the SEQ `0x04004000` breakpoint CSRs.

### 7.4 Security gating

> **NOTE — debug/trace is access-gated.** The CoreSight `AUTHSTATUS` /
> `LOCKACCESS` / `LOCKSTATUS` / `CLAIMSET` / `CLAIMCLR` registers (§6) are the
> *self-description* of the authentication/lock state, but the policy that decides
> whether a host may **reach** this aperture (and whether TRAX may stream
> off-chip, and whether the PMU is host-visible) is enforced at the SoC fabric
> perimeter, not in this register file. Which of these debug/trace registers are
> exposed under which trust level — and how host profiling/trace is fenced off in
> production parts — is covered on the
> [Profiling / Trace / Debug + Access Gating](../security/profiling-trace-debug-gating.md)
> page. The host-visible PMU mirror is a separate block; see also the
> [tpb_xt_local_reg](tpb-xt-local-reg.md) Q7-local CSR window.

---

## 8. Confidence summary

| Block | Offsets / names / positions / access | Reset values | Semantics |
|---|---|---|---|
| Trax (`0x0`)        | **HIGH/OBSERVED** | HIGH (descriptor-consistent) | HIGH (verbatim Descriptions) |
| PMU (`0x1000`)      | **HIGH/OBSERVED** | HIGH | layout HIGH; **event encodings LOW** (not enumerated) |
| OCD (`0x2000`)      | **HIGH/OBSERVED** | HIGH | layout HIGH; **`StopCause`/version enums LOW** |
| Misc (`0x3000`)     | **HIGH/OBSERVED** (rel-0x20 base) | HIGH | HIGH |
| CoreSight (`0x3F00`)| **HIGH/OBSERVED** | ID regs HIGH/MED; **8 placeholder `0xb1` LOW** | ID decode HIGH; JEP106 vendor MED |

**Per-gen applicability:** byte-grounded for **Cayman / NC-v3 Q7** from
`xtensa_q7.json`. The descriptor is carried *verbatim* under the `mariana`,
`mariana_plus`, and `maverick/vpc-mirror` arch-header trees, so the same aperture
applies across those packaged gens **[CARRIED]**; any **v5 / MAVERICK**
silicon-level claim beyond the identical packaged JSON is **INFERRED**.

---

### Cross-references

- [SEQ Uarch Register Model + Single-Step Debugger](../../firmware/seq/uarch-debugger.md) — the *firmware* debugger (Layer B), orthogonal to this aperture.
- [CSR — Xtensa NX (NX-vs-Q7 diff)](xtensa-nx.md) — the NX-core delta.
- [Profiling / Trace / Debug + Access Gating](../security/profiling-trace-debug-gating.md) — what gates these debug/trace registers.
- [CSR — tpb_xt_local_reg](tpb-xt-local-reg.md) — the Q7-local CSR window / host-visible mirrors.
