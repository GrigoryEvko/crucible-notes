# Clock / Reset / Power Domains

This is the **honest-limits** page. It states what the shipped Vision-Q7 "Cairo" GPSIMD
configuration (`ncore2gp`, ConfigID `0xC4019686`, HW rev `NX1.1.4`, one per NeuronCore)
*actually says* about clocking, reset, and power — and, just as deliberately, what it does
**not** say. The discipline is the content: a reimplementer must be told, with the exact
symbol/field that proves it, where the boundary runs between **what the cycle-approximate
Instruction-Set Simulator (ISS) config models** and **what it leaves to the SoC host**.

The one-line reading: this is a **cycle-approximate ISS config, not an RTL/UPF deliverable**.
It models **ONE** synchronous functional clock — abstracted to an integer cycle count
(`clockPeriod = 10`, 50 % duty); **ONE** active reset (async-assert / sync-deassert flops,
reset PC `= VECBASE = 0x0`); and a power configuration that is **uniformly disabled** (no
power-shutoff domains, no retention, no DVFS), with architectural **clock-gating *capability*
flags asserted** in RTL but **not exercised** by the simulator. The real per-core
run-stall / power *sequencing* that brings the eight Q7 cores up lives in **SoC host CSRs**,
**outside** this single-core (`numOfCores = 1`) config — see §5.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/string/define read from a shipped artifact; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed
with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

> **NOTE — where the values live, so every tag is auditable.** The `ncore2gp` config ships in
> three complementary forms and each claim below names which one it came from. (1)
> **`core-isa.h`** (`…/ncore2gp/xtensa-elf/arch/include/xtensa/config/core-isa.h`) — the
> decodable `XCHAL_*` header, the authoritative *capability* record. (2) **`core.xparm`**
> (`…/ncore2gp/config/core.xparm`, 193,946 B) — the instantiated component hashes with **inline
> hex values** as XML attributes (e.g. `clockPeriod="0xa"`). (3) **`ncore2gp-params` /
> `default-params`** (`…/ncore2gp/config/`, 15,443 B, **byte-identical** — `diff`
> clean) — the human-readable **decimal** source. Where a value appears in more than one form
> they **agree** (hex↔dec verified: `clockPeriod 0xa = 10`, `ImplTargetSpeed 0x457 = 1111`,
> `ImplTargetPower 0x4b = 75`, `ImplTargetSize 0x12a6bd = 1222333`, `buildXTBoardFreq 0x1e = 30`).
> The ISS *behaviour* is cross-checked against the simulator binaries themselves —
> `libcas-core.so` (not stripped, full `.symtab`) and `libsimxtcore.so` (stripped, `.dynsym`
> with demangled C++ class names). `[HIGH/OBSERVED]`

> **GOTCHA — the `.data` VMA delta on these config DLLs is `0x200000`, not libtpu's
> `0x400000`.** `readelf -SW libcas-core.so`: `.text`/`.rodata` are
> VMA==file-offset, but `.data.rel.ro` (VMA `0x2070900`, file `0x1e70900`) and `.data` (VMA
> `0x2280ed8`, file `0x2080ed8`) both carry a **`0x200000`** VMA−file delta. Subtract it before
> any `xxd`/`objdump` on a `.data`-resident struct. (This page reads only `.text` symbols and
> `core-isa.h`/`core.xparm` text, so the delta does not bite here — but it bites the moment you
> touch a `.data` table.) `[HIGH/OBSERVED]`

---

## 0. The verdict table — every clock / reset / power knob, with source

The whole page in one grid. Every row is read directly from the source in its last column.

| Domain | Field (as named) | Value | Source `file:line` | Conf |
|---|---|---|---|---|
| **CLOCK** | functional `<CLK>` signals | **1** | `core.xparm:117` `GlobalIntfc` | `[HIGH/OBS]` |
| | `clockPeriod` | `0xa` (= **10**) | `core.xparm:3551` | `[HIGH/OBS]` |
| | `clockDutyCycle` | `0.500000` | `core.xparm:3551` | `[HIGH/OBS]` |
| | `asyncClock` (all 7 sys-intf) | **0** | `core.xparm:3538,3541,3646,3649,3652,3655,3658` | `[HIGH/OBS]` |
| | `pifAsync` | **0** | `core.xparm:3546` | `[HIGH/OBS]` |
| | `buildXTBoardFreq` | `0x1e` (=30, board only) | `core.xparm:3548` (`buildXTBoard="no"`) | `[HIGH/OBS]` |
| | (no Hz/MHz/GHz anywhere) | — | exhaustive `rg` empty | `[HIGH/OBS]` |
| | `XCHAL_HAVE_CCOUNT` (cycle ctr) | **1** | `core-isa.h:455` | `[HIGH/OBS]` |
| **RESET** | functional `<Reset>` signals | **1** | `core.xparm:118` `GlobalIntfc` | `[HIGH/OBS]` |
| | `XCHAL_HAVE_FULL_RESET` | **1** | `core-isa.h:79` | `[HIGH/OBS]` |
| | `ImplResetFlops` | **1** | `core.xparm:3549` / `params:320` | `[HIGH/OBS]` |
| | `ImplAsyncReset` | **1** | `core.xparm:3549` / `params:321` | `[HIGH/OBS]` |
| | `ImplCDCSyncStages` | **3** | `core.xparm:3549` | `[HIGH/OBS]` |
| | reset PC `= VECBASE = VECTOR0` | `0x00000000` | `core-isa.h:736,740` | `[HIGH/OBS]` |
| | `XSHAL_RESET_VECTOR_SIZE` | `0x1000` (4096) | `system.h:223` | `[HIGH/OBS]` |
| | `XSHAL_RESET_VECTOR_ISROM` | **0** (RAM, not ROM) | `system.h:224` | `[HIGH/OBS]` |
| **POWER** | `lowPower` | **0** | `core.xparm:3546` | `[HIGH/OBS]` |
| | `XCHAL_HAVE_PSO` | **0** | `core-isa.h:88` | `[HIGH/OBS]` |
| | `XCHAL_HAVE_PSO_CDM` | **0** | `core-isa.h:89` | `[HIGH/OBS]` |
| | `XCHAL_HAVE_PSO_FULL_RETENTION` | **0** | `core-isa.h:90` | `[HIGH/OBS]` |
| | `XCHAL_HAVE_FUSION_LOW_POWER` | **0** | `core-isa.h:104` | `[HIGH/OBS]` |
| | `PSODomains` | **none** | `core.xparm:3546` | `[HIGH/OBS]` |
| | `PSOCoreRetention` | **none** | `core.xparm:3546` | `[HIGH/OBS]` |
| | `mpPSO` / `psoAllMemPreservation` | **0 / 0** | `core.xparm:3530` | `[HIGH/OBS]` |
| | `semDataGating` / `memDataGating` | **none / 0** | `core.xparm:3546` | `[HIGH/OBS]` |
| | `IRdropPort` / `ActivityPort` | **0 / 0** | `core.xparm:3546` | `[HIGH/OBS]` |
| | `XCHAL_CLOCK_GATING_GLOBAL` | **1** *(RTL only)* | `core-isa.h:232` | `[HIGH/OBS]` |
| | `XCHAL_CLOCK_GATING_FUNCUNIT` | **1** *(RTL only)* | `core-isa.h:233` | `[HIGH/OBS]` |
| | `busClkEnable` | **1** *(RTL)* | `core.xparm:3546` | `[HIGH/OBS]` |
| | `ISSHasTCMClkGating` / `ISSTCMClkGating` | **0 / 0** *(ISS off)* | `params:155,156` | `[HIGH/OBS]` |
| **SEQUENCING** | `XCHAL_HAVE_MP_RUNSTALL` (port) | **0** | `core-isa.h:87` | `[HIGH/OBS]` |
| | `XCHAL_HAVE_MP_INTERRUPTS` | **0** | `core-isa.h:86` | `[HIGH/OBS]` |
| | `MPRunStall` | **0** | `core.xparm:3554` | `[HIGH/OBS]` |
| | `numOfCores` / `NumOfCores` | **1** | `core.xparm:3530` / `params:210` | `[HIGH/OBS]` |
| | `WWDT[Configured]` | **0** (not built) | `params:213–216` | `[HIGH/OBS]` |
| **SYNTHESIS** *(not a domain — §6)* | `ImplTargetSpeed` | `1111` (read MHz) | `core.xparm:3549` / `params:322` | `[HIGH/OBS]` |
| | `ImplTargetPower` | `0x4b` (= 75) | `core.xparm:3549` | `[HIGH/OBS]` |
| | `ImplTargetTechnology` | `tsmc_28hpcplus_35_9t` | `core.xparm:3549` / `params:324` | `[HIGH/OBS]` |
| | `ImplOperatingCondition` / `…Voltage` | `WCZ` / `Nominal` | `core.xparm:3549` / `params:325,326` | `[HIGH/OBS]` |
| **SoC** *(external — §5)* | `q7.release_run_stall` CSR | `@0x3000 [7:0]`, reset `0xFF` | cross-page (runtime/control) | `[HIGH/CARRIED]` |

---

## 1. Clock — one functional domain, modeled as a cycle count

### 1.1 One functional clock signal, end to end

The processor's `GlobalIntfc` declares exactly **one** functional clock and **one** reset
(`core.xparm:117–118`):

```xml
<CLK   b="refh" link="types.signal"/>     <!-- the one functional clock -->
<Reset b="refh" link="types.signal"/>     <!-- the one functional reset -->
```

There is **no** second functional clock pin, no clock-divider field, no PLL/DLL model, no
multi-clock crossing inside the core. The Vision-Q7 datapath, its coprocessors
(`XCHAL_CP_MAXCFG 7`, `core-isa.h:94`), the two load/store units
(`XCHAL_NUM_LOADSTORE_UNITS 2`, `core-isa.h:226`), the 16 KB I-cache, and the local RAMs all
run on this one `CLK`. **One core clock domain.** `[HIGH/OBSERVED]`

> **GOTCHA — the `<CLK>` token appears *twice* in `core.xparm`; that is two schema
> *definitions*, not two clock domains.** A naive `rg -c '<CLK b="refh"'` returns **2**:
> `GlobalIntfc` at `:117` and the standalone `ClkIntfc` type at `:132`. The second is the
> reusable *clock-interface type definition*, re-referenced — not a second functional clock.
> Anchor "one functional clock" to the **`GlobalIntfc`** declaration (`:117`), and corroborate
> with `asyncClock="0"` on *every* bus (§1.3). Counting the raw token gives the wrong answer.
> `[HIGH/OBSERVED]`

The only **other** clocks anywhere in the config are **test/debug** clocks, which are **not**
functional-datapath domains. The JTAG TAP declares `TClockDR` (TCK) and `TResetB` (TRST) in
both `TapOCDIntfcOut` (`core.xparm:755–786`) and `TapOCDIntfcIn` (`:787–800`), with
`jtagAccess="1"`, `erDebugAccess="1"`, `coresightDAP="0"` (`:3546`). The TAP is its own clock
by construction (true of every Xtensa TAP), but it carries **no frequency** and the cycle-ISS
does not co-simulate it as a clock-domain crossing. `[HIGH/OBSERVED for existence; INFERRED
that it is a separate test domain — architectural, not measured here.]`

### 1.2 The clock *is* a cycle count — the ISS artifact, stated plainly

The config gives the simulator a **nominal** clock, not a real frequency (`core.xparm:3551`):

```xml
<verification … clockPeriod="0xa" clockDutyCycle="0.500000"/>
```

`clockPeriod = 10` is **dimensionless time units per cycle**, duty 50 %. It lives under the
`verification` (design-verification) sub-hash — a cycle-model knob, not a silicon spec. It
exists so the cycle-accurate ISS can turn an **edge count into a "time"**; `10` is the
canonical default. There is **no** frequency in Hz/MHz/GHz anywhere in `core.xparm` or the
`.params` (exhaustive `rg -i 'mhz|ghz|hz|nanosec|picosec|corefreq|systemclock|frequency'`
returned nothing). **The model abstracts all real clocking to integer cycle
counts; a "cycle" has no wall-clock meaning in this config.** `[HIGH/OBSERVED]`

This is not just a text-config statement — it is what the **simulator binary does**. In
`libcas-core.so` (not stripped), the cycle engine's exported entry points are:

| Symbol (`nm libcas-core.so`) | VMA | Role |
|---|---|---|
| `dll_cycle_advance` | `0x17aa3c0` | advance the model **one cycle** |
| `dll_reset_cycle_advanced` | `0x17aa3b0` | clear the "cycle advanced" flag |
| `dll_reset_states` | `0x17b5810` | reset architectural state |
| `dll_get_stall_functions` | `0x17aa2e0` | hand back the pipeline-interlock table |

Disassembling the first two (`.text`, VMA==file-offset) shows the clock is literally a
**state bit toggled per step**:

```asm
; dll_cycle_advance @0x17aa3c0  (rdi = core state, esi = arg)
17aa3db:  orb  $0x8, 0x5a8(%rdi)      ; SET bit 3 of core[+0x5a8] = "cycle advanced this step"
; dll_reset_cycle_advanced @0x17aa3b0
17aa3b0:  andb $0xf7, 0x5a8(%rdi)     ; CLEAR bit 3 of core[+0x5a8]
17aa3b7:  ret
```

In `libsimxtcore.so` (the ISS core, stripped, demangled `.dynsym`) the central engine is
**`class CycleCore`** (its `this`-pointer `P9CycleCore` threads through the whole library), and
the clock-period accessor is the plain function **`xtlog_GetSystemClockPeriod()`**
(`_Z26xtlog_GetSystemClockPeriodv` @`0x20dc70`). The "system clock" the simulator knows is the
`clockPeriod=10` knob, nothing more. `[HIGH/OBSERVED]`

> **QUIRK — the cycle count *is* architectural: `CCOUNT`.** `XCHAL_HAVE_CCOUNT 1`
> (`core-isa.h:455`) gives the core a `CCOUNT` cycle-counter register that increments once per
> `CLK`, with three `CCOMPARE` comparators (`XCHAL_NUM_TIMERS 3`, `:456`) firing timer
> interrupts **28 / 29 / 30** (`XCHAL_TIMER0/1/2_INTERRUPT`, `:548–550`; corroborated by
> `ISSTimerInterrupts = [ 28 29 30 ]`, `params:423`). So the firmware's *only* notion of time is
> also a cycle count — the same abstraction the ISS uses, surfaced as a guest register. There is
> no separate real-time/low-frequency tick. `[HIGH/OBSERVED]`

### 1.3 Every interface is synchronous to the core clock

No asynchronous clock crossing is modeled on **any** bus. `rg -o 'asyncClock="[^"]*"'` returns
**seven** occurrences, **all `"0"`**, at `core.xparm:3538, 3541, 3646, 3649, 3652, 3655, 3658`.
`pifAsync="0"` on the core (`:3546`) turns the legacy PIF async bridge **off**. The two SoC
system interfaces (`coreconnect`, `numOfSystemInterfaces="2"`, `:3530`) are an **ACELite master**
(`:3538`, `type="ACELite"`) and an **AXI4 slave** (`:3541`, `type="AXI4"`), both `width="0x80"`
(128-bit), both `asyncClock="0"`. The five further sys-intf instances (`:3646–3658`,
ACELite/AXI4 mix) are likewise synchronous.

**The config models a single synchronous clock domain end to end; any real CDC to the SoC
fabric is abstracted away — a cycle-ISS does not need it.** `[HIGH/OBSERVED]`

> **NUANCE — `.params` *requests* an async AXI bridge; `core.xparm` *resolves* none.**
> `ncore2gp-params:337–338` say `PIFBridgeType = AXI` and `PIFAsyncBusBridge = 1`, but the
> instantiated core hash `core.xparm:3546` resolves `pifBridgeType="none"` and `pifAsync="0"`,
> because GPSIMD uses the **ACELite + AXI4 system interfaces** (§1.3) rather than the legacy
> single-PIF bridge. **Read the `core.xparm` value, not the `.params` request, for "what is
> modeled"** — and it is consistent with `asyncClock="0"` on the actual interfaces.
> `[MED/OBSERVED; INFERRED reconciliation — the only direction consistent with the resolved
> async flags.]`

---

## 2. Reset — one active reset, vector at PC `0x0`

### 2.1 Reset implementation style (synthesis level)

```
core-isa.h:79    XCHAL_HAVE_FULL_RESET   1   ; all regs/state reset
core.xparm:3549  ImplResetFlops="1"  ImplAsyncReset="1"  ImplCDCSyncStages="3"
params:320–321   ImplResetFlops = 1  ;  ImplAsyncReset = 1
```

- `XCHAL_HAVE_FULL_RESET 1` — reset clears **all** architectural registers/state (not a partial
  warm reset). One reset network reaches the full flop array. `[HIGH/OBSERVED]`
- `ImplResetFlops 1` — state flops are reset-able (a reset network reaches them), vs relying on
  memory-init only.
- `ImplAsyncReset 1` — the flop reset is **asynchronously asserted** (and synchronously
  de-asserted — the standard async-assert/sync-deassert reset). This is the **one** place the
  config admits an async event, and it is the **reset**, not a clock.
- `ImplCDCSyncStages 3` — three synchronizer flop stages for any clock-/reset-domain crossing
  into the core (reset de-assert sync, JTAG/debug crossings). `[HIGH/OBSERVED]`

These are **RTL/synthesis attributes**: the cycle-ISS does not simulate metastability or
synchronizer latency. They document the silicon, not the sim behaviour.

> **NUANCE — "synchronous reset" with care.** The *core reset network* is a single active reset
> (`<Reset>` count = 1, `core.xparm:118`), and every **bus** is synchronous (`asyncClock=0`,
> §1.3). But the **reset assertion itself is async** (`ImplAsyncReset=1`) — the textbook
> async-assert / sync-deassert flop reset. Do not over-read "synchronous": the *de-assert* is
> synchronized (3 CDC stages); the *assert* edge is asynchronous to `CLK`. The ISS sidesteps the
> whole timing question by treating reset as an **instantaneous architectural state-clear**
> (the `dll_reset_states` / per-unit `reset()` methods in §2.3). `[HIGH/OBSERVED for the fields;
> INFERRED for the standard async-assert/sync-deassert reading.]`

### 2.2 The reset vector address — PC `= VECBASE = 0x0`

```
core-isa.h:736  XCHAL_VECBASE_RESET_VADDR  0x00000000
core-isa.h:740  XCHAL_RESET_VECTOR0_VADDR  0x00000000   (PADDR :741 also 0)
core-isa.h:742  XCHAL_RESET_VECTOR1_VADDR  0x00100000   (alt / system-RAM, unused)
core-isa.h:738  XCHAL_RESET_VECBASE_OVERLAP 0           (UNUSED)
params:367      ResetVectorOffset = 0x00000000
params:359      DynVecBaseReset   = 0x00000000   (DV_DynVecBaseReset :609 also 0)
core.xparm:3581 <vectors numOfVectors="0" relocatableVectorOption="1"
                 stationaryVectorBase0="0" stationaryVectorBase1="0x100000"
                 SW_stationaryVectorBaseSelect="0" vecBase1FromPins="0"
                 RvNMIVector="0" RvMtvecResetVal="0" RvStvecResetVal="0"/>
```

- **Reset PC = VECBASE reset value = `0x00000000`.** The first instruction is fetched from `0x0`.
  This **matches** [Boot / Reset Sequence](boot-reset.md) byte-for-byte: the 3-byte
  `j _ResetHandler` of `.ResetVector.text` sits at `0x0`, and `VECBASE` reset and reset PC
  coincide there (`XCHAL_RESET_VECBASE_OVERLAP 0`). The boot spine owns the *sequence*; this page
  owns the *config that pins the vector*. `[HIGH/OBSERVED; cross-ref boot-reset.md §1–§3.]`
- **Relocatable, unified DispatchVector.** `relocatableVectorOption=1` /
  `RelocatableISB=1` (`params:362`): the `_ResetHandler` does `wsr.vecbase` to move the vector
  surface and `wsr.isb` to set the interrupt-stack base to **`0x8FED0`** (`InterruptStackBase =
  0x0008fed0`, `params:361`; cross-ref boot-reset.md §3.2). **All** non-reset vector offsets are
  `0x0` (`params:368–380`) — the XEA3 single relocatable DispatchVector model, where every trap
  funnels through one vector surface rather than fixed per-cause vectors.
- `stationaryVectorBase1 = 0x00100000` (1 MiB) is the alternate/system-RAM base — **not
  selected** (`SW_stationaryVectorBaseSelect=0`, `vecBase1FromPins=0`); the `0x0` base is used.
- `RvNMIVector=0`, `RvMtvec/RvStvecResetVal=0`: NMI is not configured (`XCHAL_HAVE_NMI 0`,
  `core-isa.h:454`; `IsNMIConfigured = 0`, `params:395`), and the RISC-V `mtvec`/`stvec` reset
  fields are vestigial schema slots (this is an Xtensa, not RISC-V). `[HIGH/OBSERVED]`

> **QUIRK — the reset vector is in *RAM*, not ROM.** `XSHAL_RESET_VECTOR_ISROM 0`
> (`system.h:224`) with `XSHAL_RESET_VECTOR_SIZE 0x1000` (4096 B, = the `VectorSizes=[4096]` of
> `params:382`). The reset/dispatch image is **installed into IRAM by the SoC host before the
> core's run-stall is released** (boot-reset.md §8), so the very first fetch at `0x0` already
> sees the linked vectors. A reimplementer must **not** model a self-contained boot ROM at
> `0x0`; the vector is a writable surface filled externally. `[HIGH/OBSERVED]`

### 2.3 Reset as the ISS models it — instantaneous state-clear, not a timed power-up

The simulator implements reset as a **method call that clears architectural state**, with no
timed sequence. `libcas-core.so` exports `dll_reset_states` (`0x17b5810`); `libsimxtcore.so`
carries a `reset()` (or `tap_reset()` / `reset_DIR()`) method on every stateful unit —
demangled `.dynsym` shows `XTCORE_HOST::reset()`, `IntrControl::reset()`,
`NXPrefetch::reset()`, `SG_Request::reset()`, `XTCORE_OCD::tap_reset()`,
`XTCORE_OCD::reset_DIR()`, `XTCORE_TRAP::reset_mstatus()`, among others. **None** of these
takes a latency argument; reset is architectural, not temporal.

> **NOTE — the config encodes the reset *vector + flop style*, never the reset *sequence*.** The
> multi-step cold-boot spine (I-cache invalidate → `MEMCTL`/`PREFCTL` → `WindowBase` prime → MPU
> program → cache enable → optional ROM unpack → `_start`) is **code** in `reset-vector.o` /
> `crt1`, decoded byte-exact in [Boot / Reset Sequence](boot-reset.md). The config does not say
> "do these steps in this order"; it pins the PC the spine starts from (`0x0`) and the reset-flop
> network it runs under. `[HIGH/INFERRED — absence of a sequence field is OBSERVED; the
> code-owns-the-sequence reading is cross-referenced.]`

---

## 3. Power — uniformly disabled (no PSO, no retention, no DVFS)

### 3.1 Every power field is `none` or `0`

Authoritative values from the core hash (`core.xparm:3546`, the `<xt …>` instance) and the
`coreconnect` level (`:3530`), each cross-confirmed in `core-isa.h`:

| Field | Value | Source | Capability define | Source |
|---|---|---|---|---|
| `lowPower` | `0` | `core.xparm:3546` | `XCHAL_HAVE_FUSION_LOW_POWER 0` | `core-isa.h:104` |
| `PSODomains` | **none** | `core.xparm:3546` | `XCHAL_HAVE_PSO 0` | `core-isa.h:88` |
| `PSOCoreRetention` | **none** | `core.xparm:3546` | `XCHAL_HAVE_PSO_FULL_RETENTION 0` | `core-isa.h:90` |
| `mpPSO` | `0` | `core.xparm:3530` | `XCHAL_HAVE_PSO_CDM 0` | `core-isa.h:89` |
| `psoAllMemPreservation` | `0` | `core.xparm:3530` | — | — |
| `semDataGating` | **none** | `core.xparm:3546` | — | — |
| `memDataGating` | `0` | `core.xparm:3546` | — | — |
| `IRdropPort` | `0` | `core.xparm:3546` | — | — |
| `ActivityPort` | `0` | `core.xparm:3546` | — | — |

**The config defines ZERO power-shutoff domains, ZERO isolation cells, ZERO retention
registers, ZERO UPF/CPF intent.** There is no "always-on" vs "switchable" partition in this
model. A reimplementer gets **no power-domain map from this config because there isn't one.**
This is independently confirmed in the simulator binaries: a symbol/string sweep of
`libcas-core.so`, `libsimxtcore.so`, and `libisa-core.so` for
`pso|lowpower|retention|powerdomain|powergat` returned **nothing** — the ISS has no
power-domain object at all. `[HIGH/OBSERVED]`

### 3.2 Clock-gating — a synthesis *capability*, not a simulated behaviour

There are **two** categories here, and the distinction is the whole point of the page:

**(A) Power-shutoff / retention — NOT MODELED AT ALL** (§3.1). Done. Nothing to recover.

**(B) Clock-gating — ENABLED as an RTL capability, NOT simulated.**

```
core-isa.h:232  XCHAL_CLOCK_GATING_GLOBAL    1   ; global clock gating
core-isa.h:233  XCHAL_CLOCK_GATING_FUNCUNIT  1   ; functional-unit clock gating
core.xparm:3546 ClkGateGlobal="1"  ClkGateFuncUnit="1"  busClkEnable="1"
params:327–328  ClkGateGlobal = 1  ;  ClkGateFuncUnit = 1
```

`ClkGateGlobal` / `ClkGateFuncUnit` are **RTL elaboration flags**: they tell the back-end to
**insert clock-gating cells** (a global gate plus per-functional-unit fine-grain gates) so idle
logic stops toggling, and `busClkEnable=1` adds a bus clock-enable. These reduce **dynamic power
in silicon**. The cycle-accurate ISS does **not** model the power saved, nor any gating latency —
it counts cycles regardless of whether a real flop would have been gated.

Crucially, the **only** gating with an ISS-visible knob is explicitly turned **off** on the
simulator side:

```
params:155  ISSHasTCMClkGating = 0     ; the ISS does NOT model TCM clock gating …
params:156  ISSTCMClkGating    = 0     ; … and the knob is disabled
```

> **CORRECTION — "clock gating is on" is true of the RTL and *false* of the simulator.** A naive
> read of `ClkGate*=1` would have the ISS power-gating idle units. It does not. The RTL-insertion
> flags (`XCHAL_CLOCK_GATING_*`, `ClkGate*`) and the **only** ISS-side gating knob
> (`ISSTCMClkGating`) point in **opposite** directions: capability asserted in synthesis (`1`),
> behaviour disabled in simulation (`0`). The simulator's cycle counts ignore gating entirely.
> Pin: **gating = an RTL deliverable the ISS never exercises.** `[HIGH/OBSERVED for the flags;
> INFERRED for "RTL-only" — but `ISSTCMClkGating=0` makes the simulator side explicit.]`

**NET:** the only power feature with any ISS-visible knob (TCM clock gating) is **disabled** in
the ISS; everything else is either an RTL-only capability flag (`ClkGate*`) or entirely absent
(PSO / retention). `[HIGH/OBSERVED+INFERRED]`

---

## 4. No multi-domain power / clock sequencing in *this* config

`numOfCores="1"` (`core.xparm:3530`; `NumOfCores = 1`, `params:210`). This config describes a
**single** Q7 core. The GPSIMD block instantiates **eight** such Q7 cores per NeuronCore, but
their power/clock **sequencing** (who is powered, who is stalled) is **not** in this per-core
config — it is SoC-level (§5). Corroborating "no inter-core fabric modeled here":

- `MPCoherencySupport="0"` (`core.xparm:3546`), `InterruptDistributor="0"` (`:3554`),
  `XCHAL_HAVE_MP_INTERRUPTS 0` (`core-isa.h:86`) — no inter-core coherence / interrupt
  distributor.
- `MPRunStall="0"` (`core.xparm:3554`) and `XCHAL_HAVE_MP_RUNSTALL 0` (`core-isa.h:87`) — the
  multiprocessor **RunStall input port** is **not built** in this single-core config. There is no
  `RunStall`/`PWaitMode`/sleep param (`rg` empty). **Within this config, the core has no modeled
  stall/sleep power state.** `[HIGH/OBSERVED]`

> **GOTCHA — the ISS *binary* has generic run-stall and WAITI machinery; this *config* does not
> drive it.** `libsimxtcore.so` (`.dynsym`, demangled) carries `NXCORE::check_RunStall()`,
> `XTCORE::check_RunStall()`, `XTCORE_OCD::evaluate_run_stall()`, `XTCORE_OCD::set_stallin(bool)`,
> and `CycleCore::invokeWaitICallBack(bool)` / `waiti_switch_pending()` /
> `clear_waiti_switch()`. **Do not read these as "this Q7 has run-stall / a sleep state."** They
> are generic ISS capabilities compiled for **all** Xtensa configs: `check_RunStall` is gated by
> `XCHAL_HAVE_MP_RUNSTALL`, which is **0** here, so no run-stall *port* exists to drive it; and
> the `XTCORE_OCD::*_run_stall` / `set_stallin` pair is the **OCD debug single-step/halt** path
> (the JTAG/APB debug module, §1.1), **not** a power state. The `WAITI` hooks model
> wait-for-interrupt as an idle-**cycle-accounting** skip (`XCHAL_HAVE_INTERRUPTS 1`,
> `core-isa.h:453`), **not** a low-power retention state. Capability present in the binary ≠ this
> config exercising it. `[HIGH/OBSERVED — symbols present + config gate values read.]`

---

## 5. Where the run-stall / sequencing actually lives — SoC host CSRs (the boundary)

The mechanism that gates **whether each Q7 ever runs** is a **SoC host CSR**, **outside** this
Cadence per-core config. Out of SoC reset all eight Q7 cores are held stalled; the host clears a
per-engine mask to release them, **after** it has installed the IRAM/DRAM image (so the first
fetch at `0x0` already sees the linked vectors — §2.2, boot-reset.md §8):

| Mechanism | Where | Reset / value |
|---|---|---|
| `q7.release_run_stall` (per-engine 8-bit mask) | SoC host CSR `@0x3000`, bits `[7:0]` | reset `0xFF` (all 8 held); host clears to release |
| `nx.release_run_stall` (NX/SEQ sequencer) | SoC host CSR | reset `1` (SEQ held) |
| boot rendezvous (device→host / host→device) | DRAM[0] one-word mailbox | `0x6099CB34` → `0x502B2DA1` claim |

This per-engine run-stall mask **is** the GPSIMD power/clock **sequencing** layer, and it is
**intentionally** outside the Cadence per-core config: the config models the core's internal
behaviour **once it is clocked and un-stalled**; the **SoC decides when that happens**. A
reimplementer needs **both** — this page (+ boot-reset.md) for the core, and the SoC-CSR pages
for the bring-up sequencing. `[HIGH/CARRIED — the CSR offsets/constants are consolidated from
the host-image cross-pages, not derived in this config.]`

The SoC-side ownership is detailed in two **forward** Parts:

- [The `aws_hal_q7_*` HAL](../runtime/aws-hal-q7.md) *(Part 8)* — the host-side run-stall release
  and per-arch de-stall hooks (Cayman/Mariana broadcast-clear vs Sunda SEQ rendezvous).
- [CSR — `tpb_xt_local_reg`](../control/csr/tpb-xt-local-reg.md) *(Part 13)* — the per-core local
  register block that carries the `release_run_stall` / power-sequencing fields.

See also [Boot / Reset Sequence](boot-reset.md) §8 for the run-stall *release* that gates the
reset vector, and its §7 for the DRAM[0] claim handshake. **This page owns the
clock/reset/power-domain *characterization*; boot-reset.md owns the boot *spine*; they agree on
the reset vector (`PC = VECBASE = 0x0`) and the run-stall boundary.**

---

## 6. Synthesis / physical context — informative, *not* a clock/power domain

These fields describe the **back-end implementation target**. They are **not** domains the ISS
clocks against, but they bound the silicon and are the closest the config comes to a
"frequency / power / voltage" statement. From `core.xparm:3549` (`<implementation …>`) and
`params:319–328`:

| Field | Value | Reading |
|---|---|---|
| `ImplTargetSpeed` | `1111` (`0x457`) | synthesis **speed goal** (read MHz, Cadence convention) |
| `ImplTargetSize` | `1222333` (`0x12a6bd`) | area / gate goal |
| `ImplTargetPower` | `75` (`0x4b`) | relative **power goal** |
| `ImplTargetTechnology` | `tsmc_28hpcplus_35_9t` | TSMC 28 nm HPC+, 9-track std cell |
| `ImplTargetLibraryType` | `TENSILICA_STD` | — |
| `ImplOperatingCondition` | `WCZ` | **W**orst-**C**ase @ **Z**ero (worst-case timing corner) |
| `ImplVoltageCondition` | `Nominal` | nominal voltage corner |
| `ImplSpeedPriority` / `…Power` / `…Size` | `High` / `Medium` / `Low` | speed-first synthesis |
| `ImplRegFileBuildingBlock` | `FlipFlop` | (vs latch array) |
| `ImplDecoder` | `and_or_mux` | — |

These say: synthesized **for speed** at a TSMC 28HPC+ **worst-case/zero** corner, **nominal**
voltage — a **single-corner, single-voltage** target. There is **no multi-Vdd, no DVFS table,
no voltage-domain partition** in the config. The `ImplTargetSpeed=1111` (read MHz) is a
**synthesis goal**, not the on-silicon operating frequency (which this config does not state).

> **NOTE — `buildXTBoardFreq=0x1e` (=30) is the FPGA-board knob, not the ASIC clock.** It lives
> under `core.xparm:3548` alongside `buildXTBoard="no"` / `buildEmul="no"` — the XTBoard
> emulation board is **unused**, so its 30 MHz is irrelevant to the silicon. Do not mistake it
> for a core frequency. `[HIGH/OBSERVED]`

---

## 7. The honest gap list — what this config does **not** model

A cycle-approximate ISS config legitimately abstracts most of clocking/power. Spelling out the
gaps so no downstream page over-claims a "power-domain map":

| # | Gap | Proof | Conf |
|---|---|---|---|
| G1 | **No real clock frequency.** Only `clockPeriod=10` (dimensionless) + synthesis `ImplTargetSpeed=1111`. On-silicon MHz unknown from config. | `rg` for Hz/MHz/GHz empty | `[HIGH/OBS]` |
| G2 | **No multiple functional clock domains.** One `CLK`; no PLL/DLL/divider/CDC inside the core (`asyncClock=0` ×7, `pifAsync=0`). Only "other" clock = JTAG TAP test clock. | `core.xparm:117,3538…3658,3546,755–800` | `[HIGH/OBS]` |
| G3 | **No PSO domains, no isolation, no retention.** `PSODomains=none`, `PSOCoreRetention=none`, `lowPower=0`, `mpPSO=0`, `XCHAL_HAVE_PSO*=0`. No UPF/CPF intent. | `core.xparm:3546,3530` / `core-isa.h:88–90,104` | `[HIGH/OBS]` |
| G4 | **No simulated clock gating.** `XCHAL_CLOCK_GATING_*=1` / `ClkGate*=1` are RTL insertion flags; `ISSTCMClkGating=0` disables the only ISS knob. Cycle counts ignore gating. | `core-isa.h:232,233` / `params:155,156` | `[HIGH/OBS]` |
| G5 | **No voltage / DVFS.** Single corner (`WCZ` / `Nominal`). No voltage domain, no AVS/DVFS table, no level shifters. | `core.xparm:3549` | `[HIGH/OBS]` |
| G6 | **No multi-core power/clock sequencing.** `numOfCores=1`, `MPRunStall=0`, `XCHAL_HAVE_MP_RUNSTALL=0`. The 8-core run-stall mask is a SoC host CSR (§5). | `core.xparm:3530,3554` / `core-isa.h:87` | `[HIGH/OBS]` |
| G7 | **No sleep / `WAITI` power state.** No `PWaitMode` value; the ISS `WAITI` hooks are idle-cycle accounting (`XCHAL_HAVE_INTERRUPTS 1`), not a retention state. | `params` (no `PWaitMode`); `libsimxtcore.so` `invokeWaitICallBack` | `[HIGH/OBS]` |
| G8 | **No reset sequence / timing.** Reset gives the vector (`PC=0x0`) and flop style; the boot spine is code (boot-reset.md). ISS reset = instantaneous architectural clear. | `core-isa.h:736,740`; `dll_reset_states` | `[HIGH/INF]` |
| G9 | **No watchdog clock domain.** `WWDT[Configured]=0` — the windowed watchdog (which would imply a separate always-on/low-freq tick) is **not built**. The three CCOMPARE timers run on the single `CLK`. | `params:213–216` | `[HIGH/OBS]` |

> **CORRECTION — `WWDT` is a *structured* field that says "not configured", not a deferred
> `"*"` stub.** A quick read might leave the windowed watchdog as an unresolved schema stub. The
> `.params` are explicit: `WWDT = [ # Configured \n 0 ]` (`params:213–216`) — the watchdog's
> `Configured` sub-value is **`0`**. So G9 is an OBSERVED *disabled* fact, not an inference from
> absence. No separate watchdog tick exists. `[HIGH/OBSERVED]`

A reimplementer should therefore source clocking / power / voltage / sequencing facts from
(a) the SoC-CSR pages (§5: run-stall + bring-up), (b) the Trainium/Inferentia SoC documentation
**outside** this corpus, and (c) the synthesis context (§6) for the silicon target — **not** from
a "power-domain table," because **this config has none.**

---

## 8. Don't conflate domains — the *other* (NCFW) core

There is a **second, distinct** Xtensa core in the GPSIMD stack: the **NCFW management core**, a
**scalar Xtensa-LX** (`XCHAL_HAVE_LX`-class, windowed, no Vision/FLIX), a **separate**
clock/reset/power domain from the Vision-Q7 datapath core characterized here. It is **not**
described by `ncore2gp/config/*` — that tree is the **Q7 `ncore2gp` config only** (this very
header reads `XCHAL_HAVE_NX 1` / `XCHAL_HAVE_LX 0`, `core-isa.h:96–97`; `XCHAL_CORE_ID
"ncore2gp"`, `:249`). Applying *this* config to the NCFW core is exactly the mis-decode trap
debunked in [The NCFW Scalar-LX Management Core](ncfw-lx-core.md).

Any "two clock domains" the SoC has are at minimum `{Q7 datapath core(s)}` and `{NCFW mgmt
core}`, but **neither this config nor this page quantifies their relative frequencies** — that is
SoC-level and out of corpus. `[HIGH/INFERRED — the two-core split is OBSERVED in the NCFW page;
the domain-separation claim is the standard consequence, not measured here.]`

---

## 9. Adversarial self-verification ledger

The five strongest claims of this page, against the actual config symbols/fields:

| # | Claim | Re-challenge | Verdict |
|---|---|---|---|
| 1 | **One functional clock, modeled as a cycle count** | `GlobalIntfc <CLK>` = 1 (`core.xparm:117`); `clockPeriod="0xa"` duty `0.5` (`:3551`); `asyncClock="0"` ×7 / `pifAsync=0`; ISS `dll_cycle_advance` toggles core[+0x5a8] bit 3, `xtlog_GetSystemClockPeriod()` is the only clock the sim knows. **Refined:** the raw `<CLK>` token count is **2** (schema dup at `:132`); anchor to `GlobalIntfc` and the async sweep, not the token count. | **OBSERVED — refined** |
| 2 | **One active reset, vector at PC `0x0`** | `XCHAL_VECBASE_RESET_VADDR`/`RESET_VECTOR0_VADDR = 0x0` (`core-isa.h:736,740`); `<Reset>` = 1 (`:118`); `ImplResetFlops=1`/`ImplAsyncReset=1`/`ImplCDCSyncStages=3` (`:3549`); `XCHAL_HAVE_FULL_RESET=1`. **Nuance:** `ImplAsyncReset=1` ⇒ async-assert/sync-deassert, not a pure synchronous reset — stated in §2.1. Matches boot-reset.md byte-for-byte. | **OBSERVED — refined** |
| 3 | **Power uniformly disabled** | Every power field is `none`/`0`: `lowPower=0`, `PSODomains=none`, `PSOCoreRetention=none`, `mpPSO=0`, `psoAllMemPreservation=0`, `sem/memDataGating`, `IRdrop/ActivityPort=0` (`core.xparm:3546,3530`); `XCHAL_HAVE_PSO/PSO_CDM/PSO_FULL_RETENTION/FUSION_LOW_POWER` all `0` (`core-isa.h:88–90,104`); ISS power-symbol sweep empty. | **OBSERVED — holds** |
| 4 | **Clock-gating flags asserted, not exercised** | `XCHAL_CLOCK_GATING_GLOBAL/FUNCUNIT=1` (`core-isa.h:232,233`), `ClkGate*=1` / `busClkEnable=1` (`:3546`) — RTL flags; `ISSHasTCMClkGating=0` / `ISSTCMClkGating=0` (`params:155,156`) — ISS off; no `clkgate` symbol in `libcas-core.so`. RTL vs ISS point opposite ways (CORRECTION §3.2). | **OBSERVED — holds** |
| 5 | **Run-stall / sequencing is SoC-CSR, not this config** | `XCHAL_HAVE_MP_RUNSTALL=0` (`core-isa.h:87`), `MPRunStall=0` (`:3554`), `numOfCores=1` (`:3530`). ISS `check_RunStall`/`evaluate_run_stall`/`invokeWaitICallBack` exist as **generic** capabilities, gated off here (GOTCHA §4). The 8-core mask is `q7.release_run_stall @0x3000` (CARRIED, §5). | **OBSERVED — holds** |

Two readings were **corrected in place**: clock-gating "on" is RTL-true / ISS-false (§3.2), and
`WWDT` is a structured `Configured=0` field, not a deferred stub (§7 G9). One claim was
**refined** for honesty: the reset is **async-assert / sync-deassert** (`ImplAsyncReset=1`), not
a pure synchronous reset (§2.1) — the ISS sidesteps the timing by treating reset as an
instantaneous architectural clear. The single honest **limit** of this page is structural and
stated throughout: this is a **cycle-approximate ISS config**, so RTL/UPF artifacts (real clock
tree, power domains, retention cells, DVFS, the multi-core de-stall sequence) are **not in the
corpus** — they are silicon/SoC deliverables, sourced from §5's forward pages and out-of-corpus
SoC documentation, never from a power-domain table this config does not contain.

---

## See also

- [Boot / Reset Sequence + Startup Config](boot-reset.md) — the cold-boot **spine** from PC `0x0`
  (reset vector → `_ResetHandler` → crt1 → dispatch loop) and the §8 run-stall release; this page
  owns the clock/reset/power-domain characterization, boot-reset.md owns the sequence, and they
  agree on `PC = VECBASE = 0x0`.
- [The `aws_hal_q7_*` HAL](../runtime/aws-hal-q7.md) *(Part 8)* — the SoC host-side run-stall
  release / per-arch de-stall, the real power/clock *sequencing* layer for the 8 Q7 cores.
- [CSR — `tpb_xt_local_reg`](../control/csr/tpb-xt-local-reg.md) *(Part 13)* — the per-core local
  register block carrying `release_run_stall` and the power-sequencing fields.
- [The NCFW Scalar-LX Management Core](ncfw-lx-core.md) — the *other* core, a **separate**
  clock/reset/power domain not described by this `ncore2gp` config.
- [Pipeline Timing Model](pipeline-timing.md) · [FLIX Co-Issue Matrix](co-issue-matrix.md) — the
  cycle-count datapath the `dll_cycle_advance` clock model drives.
- [Core Identity & Configuration](../isa/core/identity-config.md) — the full identity card
  (ConfigID `0xC4019686`, `NX1.1.4`) the clock/reset/power knobs are a slice of.
