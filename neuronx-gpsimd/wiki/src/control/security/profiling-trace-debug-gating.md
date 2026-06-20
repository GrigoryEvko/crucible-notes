# Profiling / Trace / Debug + Access Gating

This page is the **observability-and-debug spine** of the GPSIMD engine — the
Cadence Tensilica Vision-Q7 NX "Cairo" DSP (config `ncore2gp`, CoreID
`ncore2gp`) embedded in each NeuronCore as the POOL engine — read **end-to-end
from the device side through the host side**, and then **gated**: it answers the
one operational question a security reviewer actually asks — *"is the debugger
locked in production?"*

It is a synthesis page. The byte-exact register maps, blob formats, the wire
record, and the per-instruction handler bodies live in dedicated sibling pages
(cross-linked, never restated). What is assembled *here* is the **three
physically-distinct observability stacks**, the **single trace egress** that
unifies the fault and profiling paths, and the **defense-in-layers soft lock**
that closes that surface in a shipped device.

Everything below is derived from static analysis of shipped artifacts:

* the `ncore2gp` Cadence toolchain config (`core-isa.h`, `specreg.h`,
  `libhal.a debug.o`/`debug_hndlr.o`), disassembled where needed with the
  shipped `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Binutils 2.34, Xtensa
  Tools 14.09);
* the per-`(arch, engine, flavor)` GPSIMD device firmware blobs carved from
  `libnrtucode.a`, device-disassembled / string-counted;
* the Cayman register descriptors (`xtensa_q7.json`, `tpb_xt_local_reg.json`,
  `qos_pmu.json`, `fis_control`) — registry/SVD-style JSON, citeable
  binary-derived sources;
* the host runtime `libnrt.so` (`nrt_profile_*` / `nrt_inspect_*` /
  `nrt_sys_trace_*` exports, DWARF enums/structs, per-function `objdump`).

Per-claim confidence is tagged **HIGH / MED / LOW** × **OBSERVED / INFERRED /
CARRIED**. Generation coverage: **Sunda (NC-v2)**, **Cayman (NC-v3)**,
**Mariana / Mariana+ (NC-v4 / v4+)** are byte-grounded; **Maverick (v5)** is
**header-OBSERVED only** (the descriptors appear identically in the `maverick`
arch-header tree but no silicon-level v5 confirmation is in hand), so every
v5-interior claim is flagged **INFERRED · CARRIED**.

---

## 0. The three observability stacks + the gating verdict `[HIGH]`

GPSIMD's debug/profiling surface is **three physically-distinct stacks**, each
with its own substrate, its own consumer, and — the part that matters for
security — **its own access gate**. They never share a register.

| # | Stack | Substrate | Consumer | Gate |
|---|---|---|---|---|
| **1** | Xtensa **silicon debug module** — OCD / TRAX / 8 PMU counters | the `xtensa_q7` 16 KiB debug aperture, reached over **ERI / debug-APB / JTAG** | an **external probe** (JTAG/APB master) — **never** the host runtime | physical TAP behind the SoC perimeter |
| **2** | On-core **KDB instruction-decode profiler + breakpoint machine** — `hw_decode` bundle | the `tpb_xt_local_reg` `hw_decode` bundle, base **`0x4000`** | the **polled surprises handler** (firmware), then the host trace ring | firmware **FLAVOR** + host **policy** |
| **3** | Fabric **`qos_pmu`** — SoC-AXI PMU | the FIS `DEBUG_FIS` region (8 counters, 4 AXI-matcher CAMs) | the master intc (`fis_sprot_intr[4]`) | privileged **region-enable** + boots-disabled |

The **trace egress** is the unifying spine: both the **fault** path and the
**profiling/trace** path emit the **same 16-byte `NEURON_ISA` notification
record** into the per-TPB notification ring (`sw_queue_num3` `errors_NT`), MSI-X
to the host; the host `nrt_profile_*` surface drains it and reformats it into the
`ntff::` Neuron-Trace-File schema.

> **>>> THE GATING VERDICT — "is OCD locked in production?" <<<**
> GPSIMD has **no silicon OCD-lock fuse** (`XCHAL_HAVE_SECURE=0`, no FlexLock,
> no `PSO_CDM`, `MPU_LOCK=0`). "Locked in production" is achieved by **three
> soft, independent gates** plus the **hard SoC perimeter one level out**:
> * **G1 — firmware FLAVOR**: the production **PERF** (Cayman/Mariana) / **RELEASE**
>   (Sunda) image strips the entire verbose debug path (NX_POOL **187 `'S:'`
>   strings → 0**, ~22 KB smaller IRAM); the KDB/OCD *hardware* still exists, the
>   firmware that drives it chattily is the **DEBUG flavor only**.
> * **G2 — fabric region enable**: `qos_pmu` lives in `DEBUG_FIS` gated by
>   `user_debug_en[24]` and **boots disabled** (all-0 reset).
> * **G3 — host runtime policy**: the on-core profiler is host-armed only under
>   `NEURON_RT_DBG_HW_DECODE_TOGGLE`; the trace ring is subscribed only under
>   `NEURON_RT_INSPECT_DEVICE_PROFILE` / `nrt_profile_*`.
> * **HARD perimeter**: the OCD/JTAG TAP and the `DEBUG_FIS` region sit behind the
>   sprot fabric (`amzn_remapper` fail-closed CAM + qos_prot/NSM + the PCIe
>   isolation SM — see [pkl-intc-sprot-security.md](../address/pkl-intc-sprot-security.md)).
>
> **One-line answer:** there is no fuse, but in practice the silicon OCD/TRAX/8-PMU
> are JTAG-only (never wired into the host), the verbose firmware debug path is
> compiled out of the shipped flavor, and the remaining host-usable profiler is
> host-policy-gated behind the perimeter. It is **"locked" by build-flavor +
> region-enable + host-policy + fabric perimeter — a defense-in-layers SOFT lock,
> not a hardware fuse.** `[HIGH facts / MED synthesis · OBSERVED]`

---

## 1. Stack 1 — the Xtensa silicon debug module (OCD / TRAX / PMU) `[HIGH/OBSERVED]`

The Cadence debug module is **present and fully featured** in `ncore2gp`, but it
is a **JTAG/external-probe** facility: **no `libnrt` symbol or string references
`trax`/`ocd`/`jtag`** (grep-negative, this session). It is the silicon's own
debugger, never reachable from the production host runtime.

This page does **not** reproduce the register map — the byte-exact `xtensa_q7`
aperture (TRAX `@0x0000`, PMU `@0x1000`, OCD `@0x2000`, Misc `@0x3000`,
CoreSight `@0x3F00`; 5 bundles / 78 regs / 296 fields; `AddrWidth=14 /
DataWidth=32 / SizeInBytes=0x4000`) is documented in
[xtensa-q7.md](../csr/xtensa-q7.md). The **security-relevant config facts** are:

| `core-isa.h` symbol | Value | Meaning |
|---|---|---|
| `XCHAL_HAVE_OCD` | 1 | On-Chip Debug present |
| `XCHAL_NUM_IBREAK` / `XCHAL_NUM_DBREAK` | 2 / 2 | 2 instr + 2 data HW breakpoints |
| `XCHAL_HAVE_OCD_LS32DDR` | 1 | `L32DDR`/`S32DDR` fast-OCD load/store via the `DDR` reg |
| `XCHAL_HAVE_DEBUG_ERI` / `_APB` / `_JTAG` | 1 / 1 / 1 | three transports to the debug module |
| `XCHAL_HAVE_TRAX` / `XCHAL_TRAX_MEM_SIZE` | 1 / 8192 | TRAX present, 8 KB on-core trace RAM |
| `XCHAL_TRAX_ATB_WIDTH` / `_TIME_WIDTH` | 32 / 64 | ARM ATB egress 32-bit, 64-bit trace timestamp |
| `XCHAL_NUM_PERF_COUNTERS` | 8 | 8 on-core Xtensa perf counters (§3A) |
| `XCHAL_HAVE_TAP_MASTER` | **0** | core is a TAP **slave**; an external probe is TAP master |
| `XCHAL_HAVE_PSO_CDM` | **0** | **no** core/debug/memory power-domain separation |
| `XCHAL_HAVE_SECURE` | **0** | **no** secure-debug / OCD-lock option |

The debug module surfaces **four core interrupts**, all level-1:

```text
INT 31  DBG_REQUEST   (XCHAL_DBG_REQUEST_INTERRUPT)   OCD halt-request
INT 32  BREAKIN       (XCHAL_BREAKIN_INTERRUPT)        OCD break-in
INT 33  TRAX          (XCHAL_TRAX_INTERRUPT)           TRAX buffer-full/event
INT 34  PROFILING     (XCHAL_PROFILING_INTERRUPT)      perf-counter overflow
```

> **CORRECTION — `HAVE_APB=0` is NOT `HAVE_DEBUG_APB=1`.** `core-isa.h:285`
> `XCHAL_HAVE_APB=0` is the **core's outbound system-bus APB** (absent — the core
> masters AXI/ACE-Lite). `XCHAL_HAVE_DEBUG_APB=1` (`:754`) is the **debug-module's
> APB slave port**. These are **different buses**. The debug module is reachable
> over `{ERI, debug-APB, JTAG}`; the core itself has no system-APB. Conflating the
> two mis-reads the device as having a system-APB it does not have.
> `[HIGH/OBSERVED]`

> **XEA3 NOTE — no XEA2 debug specregs.** `specreg.h` has `IBREAKA/IBREAKC ×2`,
> `DBREAKA/DBREAKC ×2`, `DDR=104`, `ERACCESS=95`, but **no `DEBUGCAUSE`, no
> `ICOUNT`/`ICOUNTLEVEL`, no `PM0..PM7`**. Those are XEA2 (LX) registers. On
> **NX / XEA3** the perf counters **and** the OCD status live in the debug
> module's **external-register (ERI) space**, accessed via `RER`/`WER` — confirmed
> by the device emitting `RER`/`WER` in the DEBUG-image disasm. The core is proven
> XEA3-single-dispatch (not XEA2-leveled) by the register file in
> [xea3-interrupt-architecture.md](../interrupt/xea3-interrupt-architecture.md).
> `[HIGH/OBSERVED]`

The HAL ships **software-breakpoint helpers** in `libhal.a debug.o`
(`Xthal_debug_configured=1`, `num_ibreak=2`, `num_dbreak=2`):
`xthal_set_soft_break(addr)` reads + saves the 3 instruction bytes at `addr`,
patches a `BREAK`-class opcode in, and returns the original;
`xthal_remove_soft_break(addr, saved)` restores them. The device DEBUG image
confirms both surfaces — literal `break.n 3` / `break 1` / `halt 0` instructions
in the IRAM disasm. So a **software** breakpoint = patch a `BREAK` into IRAM; a
**hardware** breakpoint = an `IBREAKA` address-match.

> **GOTCHA — INT 31–34 are HW-armable but the firmware does not vector them.**
> This config has **no leveled-interrupt vector array, no `rsil`/`INTENABLE`
> banks** — all 37 interrupts are level-1 and the firmware does **not** vector
> them. The DEBUG IRAM disasm has effectively no `rsil`/`intset`/`intenable`
> dispatch (one `waiti` idle loop only). The firmware's debug response is the
> **polled surprises path** (§4), not an INT-34 ISR. So the silicon debug module
> can *raise* INT 31–34, but those wires terminate at a core that polls.
> `[HIGH/OBSERVED]`

---

## 2. Stack 2 — the on-core KDB decode-profiler + breakpoint machine `[HIGH]`

This is the **firmware-visible** debug/profile unit: the `hw_decode` register
bundle of `tpb_xt_local_reg`, **base `0x4000`**. It is a **custom hardware block**
that watches the *micro-op / ucode PC* the SEQ executes (not the Xtensa PC) — the
"Layer B" of the two-layer debug model in
[uarch-debugger.md](../../firmware/seq/uarch-debugger.md).

> **CORRECTION — the two `0x4000`s are different objects.** There are **two**
> apertures whose maps both involve the token `0x4000`, and conflating them is the
> single most common error in this subsystem:
> 1. The **`xtensa_q7` silicon debug aperture** (§1) has a **total span** of
>    `SizeInBytes=0x4000` (TRAX/PMU/OCD/Misc/CoreSight bundles). *JTAG/APB, external
>    probe.*
> 2. The **`hw_decode` KDB bundle** (this section) is at **bundle base offset
>    `0x4000`** inside `tpb_xt_local_reg` (`AddrWidth=16`, `SizeInBytes=0x10000`,
>    `BundleSizeInBytes=0x1000`). *BAR0, host-armed, firmware-polled.*
>
> They share the literal `0x4000` but one is a *total aperture size* and the other
> is a *bundle base*. The firmware-built absolute `0x04004000` seen in the SEQ
> image is `hw_decode.base(0x4000) + breakpoint_ctrl(0x4004)`-class arithmetic on
> bundle #2 — see [uarch-debugger.md §5](../../firmware/seq/uarch-debugger.md#5-reconciling-the-firmware-built-0x04004000-against-the-shipped-map).
> `[HIGH/OBSERVED]`

### 2a. The `hw_decode` CSR bundle (base `0x4000`) `[HIGH/OBSERVED]`

Key registers (full per-field map in
[tpb-xt-local-reg.md](../csr/tpb-xt-local-reg.md); reset values shown):

| Off | Register | Security-relevant fields |
|---|---|---|
| `0x4000` | `control` | `[0] disable_hw_decode` (Instr-FIFO vs HW-decode chicken bit); `[1] instr_ordering_mode` (→ surprise bit9); `[18:2] iram_block_size_mask` rst `0x1FFF` |
| `0x4004` | `breakpoint_ctrl` | `[0] breakpoint_instr_enable` rst 1; `[1] breakpoint_profile_table_enable` rst 1 (bp sourced **from the PROFILE TABLE**, per-opcode without reload); `[2] ucode_force_pause_enable` rst 1; `[3] breakpoint_step_valid`; `[4]/[5] addr0/1_valid`; `[6]/[7] ic0/ic1_valid`; `[8] immediate_pause` |
| `0x4008` | `breakpoint_step.num_instr` | KDB "step N instructions" count |
| `0x400C` | `breakpoint_match` | `ic0_mask[7:0] / ic0_opcode[15:8] / ic1_mask[23:16] / ic1_opcode[31:24]` |
| `0x4010`/`0x4014` | `breakpoint_ic0/ic1.value` | instr-count threshold to match for IC0 / IC1 |
| `0x4018..0x4024` | `breakpoint_addr0/1_lo/hi` | 64-bit bp addresses (pause after this ucode PC) |
| `0x4028` | `profile_cam_search_vector` | `search_byte_0..3` (`[5:0]/[13:8]/[21:16]/[29:24]`) — which **4 bytes of the instruction word** the CAM keys on (range 0..63) |
| `0x402C` | `hw_decode_flush_cntr` | rst `0x10` — cycles to wait before re-issue |

The DEBUG POOL IRAM contains 6× `const16 0x4000` (loading this bundle base for
the RMW accesses), and the EXT_BREAK path reads+clears `nx.instr_halt_ctrl(0x14)`
before Setup-Halt (§4). `[HIGH/OBSERVED]`

### 2b. The profiler event model — `PROF_CAM` match → `PROF_TABLE` record → IC count

The on-die instruction-decode profiler binds **opcode → counter** through two
parallel per-engine blobs. The **byte-exact blob formats** are documented in
[prof-cam-table-formats.md](../../images/prof-cam-table-formats.md) (and the
**host programming** of them in
[hw-decode-cam-programming.md](../../runtime/hw-decode-cam-programming.md)) —
this page reproduces only the **match-and-arm mechanism**, because it is the
event source for the gated trace path:

* **`PROF_CAM`** — 64 slots × **16 B**, staged into `<ENG>_PROFILE_CAM (0x1000)`:
  `{ u32 opcode_id; u32 mask; u32 enable; u32 rsvd; }`. `mask 0xff` = exact-byte
  match, `0x00` = wildcard. Armed slots are contiguous `0..N-1`; the **last armed
  slot is the `{0,0,1}` wildcard "all other opcodes" bucket**.
* **`PROF_TABLE`** — 64 records × **128 B**, staged into
  `<ENG>_PROFILE_TABLE (0x2000)`, **parallel-indexed** to the CAM (populated count
  == armed CAM count): a sparse per-opcode descriptor (only `0x00..0x17` used)
  carrying packed cfg words + a per-opcode counter/latency **class byte** at
  `+0x14`.

```c
/* PROF_CAM match + IC arm — the per-instruction profiler core.            *
 * Runs in the HW decode front-end every time a ucode instruction is       *
 * decoded; reproduced from the device CSR semantics (§2a) + the byte-exact *
 * blob formats. [HIGH CSR/blob · MED exact internal count-vs-snap timing]  */
static int prof_decode_step(const uint8_t insn_word[64],
                            const hw_decode_csr_t *csr,
                            const cam_slot_t cam[64], int n_armed)
{
    /* (1) Select the 4 key bytes the CAM searches, per profile_cam_search. */
    uint8_t key[4] = {
        insn_word[ csr->search_byte_0 ],   /* 0x4028 [5:0]   */
        insn_word[ csr->search_byte_1 ],   /* 0x4028 [13:8]  */
        insn_word[ csr->search_byte_2 ],   /* 0x4028 [21:16] */
        insn_word[ csr->search_byte_3 ],   /* 0x4028 [29:24] */
    };
    uint32_t opcode = (uint32_t)key[0] | (key[1]<<8) | (key[2]<<16) | (key[3]<<24);

    /* (2) Associative match against the armed slots; first hit wins.       *
     *     The last armed slot ({0,0,1}) is the wildcard catch-all bucket.  */
    int hit = -1;
    for (int i = 0; i < n_armed; ++i)
        if (cam[i].enable && ((opcode ^ cam[i].opcode_id) & cam[i].mask) == 0) {
            hit = i; break;                /* slot index i selects PROF_TABLE[i] */
        }
    if (hit < 0) return 0;                 /* no slot armed for this opcode */

    /* (3) Event->counter binding: a CAM hit that matches ICn opcode&mask    *
     *     (0x400C) increments the armed ICn counter; on threshold OR a       *
     *     profile-table-sourced bp, raise a breakpoint surprise.             */
    bool raise = false;
    if (csr->ic0_valid && ((opcode ^ csr->ic0_opcode) & csr->ic0_mask) == 0)
        raise |= (++g_ic0 >= csr->ic0_threshold);          /* 0x4010 */
    if (csr->ic1_valid && ((opcode ^ csr->ic1_opcode) & csr->ic1_mask) == 0)
        raise |= (++g_ic1 >= csr->ic1_threshold);          /* 0x4014 */
    if (csr->breakpoint_profile_table_enable)              /* 0x4004 [1]   */
        raise |= prof_table_bp_armed(hit);                 /* per-opcode bp */

    if (raise) set_surprise(SURPRISE_INS_BREAK);           /* → §4 funnel  */
    return hit;
}
```

So the profiler is **both** a per-opcode event **counter** (IC0/IC1) **and** a
per-opcode conditional **breakpoint** source. `[HIGH CSR+blob / MED exact
count-vs-snapshot timing · OBSERVED]`

### 2c. Per-engine / per-gen arming — the profiler's reach `[HIGH/OBSERVED]`

| Engine | Arming (Mariana v4) | Role |
|---|---|---|
| ACT | 25 armed | activation + control opcodes |
| DVE | 48 armed | DVE tensor/transcendental + the one 9-bit opcode `0x1e3` |
| PE | 22 armed | matmul opcodes |
| POOL (GPSIMD) | **1 armed (wildcard only)** | per-opcode **disarmed** — single catch-all bucket |
| SP | no PROF table | runs off the Instr-FIFO (no HW-decode path) |

* **Cayman (v3)** = **one shared 47-arm blob** across all four engines (an
  engine-generic profiler on the first Trn-class gen); from **Mariana** it is
  per-`(gen, engine)`. **Mariana+** == Mariana byte-for-byte; **Maverick POOL** ==
  Mariana POOL (header-OBSERVED; v5 interior **INFERRED · CARRIED**).
* **Sunda (v2) has NO profiler at all** — its NX core uses the SW-fetch FSM ("NX
  in Sunda mode: HW decode disabled"), so the host `hw_decode_table_init` is a
  no-op stub and **zero v2 PROF blobs ship**. The on-die profiler is an artifact of
  the **v3/v4 HW-assisted fetch front-end** that v2 lacks.

> **QUIRK — Mariana POOL is a 1-armed empty placeholder.** The v4 POOL CAM is
> `slot0 = {0,0,1,0}` (pure wildcard) with **all remaining slots zero** — POOL
> opcode profiling is effectively **disarmed** on Mariana while PE/ACT/DVE are
> populated. Whether it is later armed via the host file-override path or is
> permanently disarmed is **unconfirmed (LOW)** — see
> [hw-decode-cam-programming.md §2c](../../runtime/hw-decode-cam-programming.md#2c-armed-slot-counts-high-libnrt-read--sx-img-26).
> `[HIGH/OBSERVED · the override question LOW]`

---

## 3. Stack 3 + the THREE "perf counter" substrates — disambiguated `[HIGH/OBSERVED]`

"Performance counter" is **overloaded** on GPSIMD. There are **three** distinct
things; conflating them is a category error:

| | Substrate | Where | Consumer | Host-exposed? |
|---|---|---|---|---|
| **A** | **on-core 8 Xtensa perf counters** (`XCHAL_NUM_PERF_COUNTERS=8`) | Xtensa debug module, programmed via ERI (`RER`/`WER`); raise INT 34 | external JTAG/TRAX probe | **No** — no `libnrt` symbol programs them |
| **B** | **KDB IC0/IC1** opcode counters (§2) | `hw_decode` bundle `0x400C/0x4010/0x4014` | the surprises funnel → Setup-Halt + the trace ring | **Yes** — host-armed via PROF_CAM |
| **C** | **fabric `qos_pmu`** (8 programmable counters, IP max 16) | FIS `DEBUG_FIS` region next to a CoreSight ELA-500 | `fis_sprot_intr[4]` → master intc | HW/FW, region-gated |

* **(A)** counts Xtensa micro-events (cycles, I-cache miss, stalls, retired
  instructions, branch mispredicts). The **register dictionary is present in the
  HAL** — `libnrt` embeds
  `AWS_REG_CAYMAN_TPB_PERFORMANCE_COUNTER_POOL_PERF_CNTR_{0,1,2,…}_{LSB,MSB}`
  (16-bit counter, mask `0xffff`, offsets `0xc10/0xc14/0xc18/…`) — but **no
  `nrt_profile_*` path programs them**. A register *dictionary* is not an *arming
  path*; they remain the JTAG/INT-34 facility.
  `[HIGH config / role + non-exposure HIGH-INFERRED from the libnrt grep-negative]`
* **(C)** measures the **on-die AXI fabric** (read/write txns, bytes,
  backpressure cycles), **not** the Q7 instruction stream; it boots **disabled**
  (all-0 reset). The byte-exact bundle map, the 528 Cayman `qos_pmu` nodes (all
  under `*_USER_FIS_*_DEBUG_FIS_0_SPROT_QOS_PMU`), the 8 counters with dual-window
  48-bit snapshots, and the `intr[4]` wiring are in
  [qos-pmu-hostvisible.md](../csr/qos-pmu-hostvisible.md). A companion
  always-on RO monitor `qos_host_visible` mirrors the qos_prot shaper.

> **GOTCHA — do not read the §2 IC0/IC1 as the §3A 8 perf counters.** They are
> different hardware: the 8 Xtensa counters are inside the *silicon debug module*
> and count micro-events; IC0/IC1 are **2** counters inside the *`hw_decode` KDB
> bundle* and count **opcode matches**. Only IC0/IC1 are host-reachable.
> `[HIGH/OBSERVED]`

---

## 4. The breakpoint / single-step path from the polled surprises handler `[HIGH/OBSERVED]`

The §2 KDB hardware's **firmware consumer** is the **polled** surprises handler —
**not** a vectored ISR. This is the determination of
[q7-surprises-binding.md §1](../interrupt/q7-surprises-binding.md#1-the-determination--polled-not-interrupt-driven-high--observed)
("POLLED, not interrupt-DRIVEN"); the per-instruction FSM bodies are in
[surprises-irq.md](../../firmware/seq/surprises-irq.md). The
**security-relevant taxonomy** (read verbatim from the Cayman DEBUG POOL image):

```text
Every SEQ FSM iteration: poll-surprises (running-flag gate)
  -> sunda_check_surprises("S: ... any=%d, surprises=0x%x")
  -> sunda_handle_surprises  (fixed-order bit-mask dispatch over the 2-byte word):

   bit9 STRONG_ORDER  re-program ordering (SetOrderingMode)            CONTINUE
   bit2 INS_BREAK     Setup-Halt(reason=4)                            [terminal]
   bit3 EXT_BREAK     read+clear nx.instr_halt_ctrl(0x14);
                      Setup-Halt(reason=8); assert if unarmed         [terminal]
   bit8 STEP_CNT      decrement step counter; at 0 Setup-Halt(reason=64)
   else  UNHANDLED    "Unhandled surprise: surprises=0x%x" -> FATAL spin
```

The terminal handlers funnel to **`Setup-Halt`** → the halt CSR write
(`"Entering HALT"`) → spin; the force-pause variant logs `"Enter Pause"`. The
`breakpoint_ctrl` RMW is logged verbatim (`"BREAK_CTRL=0x%x -> 0x%x"` —
read-modify-write of `0x4004`). Mapping back to the §2 CSRs:

* **single-step** = `breakpoint_step_valid[3]` + `breakpoint_step.num_instr`
  (`0x4008`); the `STEP_CNT` surprise decrements toward 0 and halts;
* **HW-address bp** = `addr0/1_valid[4]/[5]` + the 64-bit `addr0/1`
  (`0x4018..0x4024`);
* **instr bp** = `breakpoint_instr_enable[0]` (a patched `BREAK` or an `IBREAKA`
  match) → `INS_BREAK`.

So the **2-byte surprises word is the single funnel** for the §2 KDB hardware
**and** the §1 OCD `IBREAK`/`DBREAK` + soft-break events — all roads lead to
`Setup-Halt`. Per-gen: Cayman/Mariana/Mariana+ byte-identical; Sunda same
taxonomy at a different halt aperture (`0x00100808` vs `0x04000014`).

> **NOTE — the "debug exception" on a SEQ is not an Xtensa DEBUG-level
> exception.** It is a *polled software break* routed through the run-state FSM's
> Setup-Halt. The only true Xtensa exception in this loop is the fatal path
> (illegal op / OOB / div0), a different surface entirely. `[HIGH/OBSERVED]`

> **GOTCHA — the HW-IBREAK → `INS_BREAK` wiring is INFERRED.** The surprise *bits*
> are OBSERVED; that an HW `IBREAKA` address-match funnels into the **same**
> `Setup-Halt` as a SW `BREAK` (rather than a separate debug exception) is
> inferred from the shared egress, not byte-pinned. `[MED/INFERRED]`

---

## 5. The trace egress — device notification ring → `ntff::` file `[HIGH/OBSERVED]`

Both faults and profiling/trace events leave the device as the **same 16-byte
`NEURON_ISA` notification record**. The **wire format** of that record
(`NEURON_ISA_NOTIFICATION_NBYTES = 0x10`; byte `+0x03` = `header
{notific_type:5, sw_ovf:1, hw_ovf:1, phase:1}`; `+0x08` = 64-bit 1-ps timestamp)
and the **full `notific_type` enum** (POOL = `0x0c..0x0f`) are documented in
[device-host-notification.md §3](../interrupt/device-host-notification.md#3-the-16-byte-neuron_isa-notification-record-high--observed)
— this page does **not** restate them. What matters for the observability spine:

* the GPSIMD/POOL engine's records are device ISA `notific_type` **12–15**
  (`TPB_POOL_{INST_START, INST_END, EXPLICIT, EVT_SEM}` — the **= GPSIMD** quad,
  the stride-4 slot after PE `0x04` and ACT `0x08`); the `NBYTES=16` and this enum
  are independently **OBSERVED** in the host DWARF (`NEURON_ISA_NOTIFICATION_TYPE`,
  values 2..31);
* the picosecond timestamp is sourced device-side from the per-engine
  `cfg_timestamp_inc` (the host reads it via `get_timestamp_inc` to scale device
  ticks → wall time);
* both faults and trace use the **one ring** — `tpb_notific sw_queue_num3
  errors_NT` — and **one MSI-X**.

### 5a. The host drain → reformat → serialize (libnrt) `[HIGH/OBSERVED]`

The host end is the `nrt_profile_*` device-NQ trace family (one of four
disambiguated observability families in `libnrt` — see §6 and
[public-api-table.md §8](../../runtime/public-api-table.md#8-profile--trace--inspect--sys_trace--debug-medhighobserved--one-nq-capture-path)).
The **subscribe → drain → reformat → serialize** spine, byte-decoded:

```c
/* HOST device-NQ trace path (libnrt). Symbols + addresses are OBSERVED;     *
 * the structure is the objdump call graph of nrt_profile_*.                 */

/* (1) ARM — subscribe the per-type HW notification-queue rings.             */
int nrt_profile_notification_subscribe_nc(const virtual_core *vc)        /*@0xae640*/
{
    for (engine in tdrv_arch_get_num_tpb() + tdrv_arch_get_num_topsp()) {
        for (type in requested_notification_types) {
            void    *nq_ids; uint32_t count; bool valid;
            notifications_get_nq_ids_for_type(type, &nq_ids, &count, &valid); /*@0x2fd160*/
            /* TRACE(0) -> count 0 : instruction trace is NOT a ring; it is   *
             * reconstructed from the per-engine INSTRUCTION-BLOCK path.       *
             * EVENT{1,2}, ERROR, THROTTLE, INFER_STATUS{4} are rings;         *
             * DMA's id set is .bss-resolved at bring-up (per-arch DMA layout).*/
            for (id in nq_ids[0..count])
                kbl_notification_subscribe(...);                              /*@0x307790*/
                /* -> db_physical_core_get_mla_and_tpb                         *
                 * -> notification_subscribe (ring @ mla_base + tpb*0x800*8    *
                 *      + 0x4cf60) -> ring_buffer_init / notification_configure*/
        }
    }
}

/* (2) DRAIN — pull the raw 16-byte records out of one ring.                 */
int nrt_profile_notification_read(const physical_core *pc, uint eng,        /*@0xab400*/
                                  notification_type type,
                                  char **out_buf, uint *out_len)
{
    size_t n   = kbl_notification_enqueued_bytes(...);   /* bytes pending     */
    char  *buf = malloc(n + 0x1000);                     /* +4 KB drain slack  */
    kbl_notification_read(..., buf, n);                  /* copy 16-B records  */
    for (rec = buf; rec < buf + n; rec += 16) {          /* NBYTES=16 stride   */
        uint8_t header = (uint8_t)rec[3];                /* notific_type:5 +   */
        classify(header);                                /*  ovf/phase bits    */
    }
    *out_buf = buf; *out_len = n;
}

/* (3) REFORMAT — normalize device type, then collapse to the ntff pair.     */
void nrt_profile_notification_read_all_nc(const virtual_core *vc,           /*@0xb1850*/
        ntff::ntff_info *out, std::vector<pair<char*,uint>> &instr_blocks,
        unsigned long &total)
{
    for (engine in tdrv_arch_get_num_tpb() + topsp) {
        /* TWO record streams per engine: the INSTRUCTION-block class and the *
         * EVENT/ERROR/DMA/THROTTLE class.                                     */
        nrt_profile_notification_read(pc, engine, INSTR_CLASS, ...);
        nrt_profile_notification_read(pc, engine, EVENT_CLASS, ...);
        for (each drained record) {
            ntff::notification_trace_type trace; ntff::block_type block;
            nrt_profile_convert_trace_type_from_ntff_params(host_type,         /*@0xaaf40*/
                                                            trace, block);
            RepeatedPtrFieldBase::AddMessageLite(out, ...);  /* into ntff_info */
        }
        /* instruction-block bytes pushed into instr_blocks (the raw stream).  */
    }
    /* Arena-DefaultConstruct the subtrees: nc_timestamp_info(tpb_pool_=GPSIMD),*
     * block_timestamp_info, version_info, collectives_op_info, ultraserver_info*/
}

/* (4) SERIALIZE — write the assembled tree to a .ntff Neuron-Trace-File.    */
nrt_profile_session_serialize(...)                                          /*@0xb61d0*/
    -> nrt_profile_serialize(file, ntff_info, instr_blocks, ...)
    -> MessageLite::SerializeToOstream  -> .ntff
```

The **device-type → `ntff::(trace_type, block_type)`** collapse
(`convert_trace_type_from_ntff_params @0xaaf40`, a 9-entry jump table) is the
crux of the reformat. It is **not a strict 1:1 identity** — it is a
**collapse-with-block-tagging**:

| Host `notification_type` | → `ntff::notification_trace_type` | `ntff::block_type` |
|---|---|---|
| `0 TRACE` | `INSTRUCTION (0)` | `NC (0)` |
| `1 EVENT` | `EVENT (1)` | `NC (0)` |
| `2 ERROR` | `ERROR (2)` | `NC (0)` |
| `3 INFER_STATUS` | `INSTRUCTION (0)` | `NC (0)` *collapse* |
| `4 DMA` | `DMA (4)` | `DMA (1)` |
| `5 THROTTLE` | `THROTTLE (5)` | `NC (0)` |
| `6 TOPSP_TRACE` | `INSTRUCTION (0)` | `TOPSP (2)` |
| `7 TOPSP_EVENT` | `EVENT (1)` | `TOPSP (2)` |
| `8 TOPSP_ERROR` | `ERROR (2)` | `TOPSP (2)` |
| `>8` | `nlog WARN`, return `2` (ERROR), no out written | — |

The full byte-decoded jump table (with `.rodata` case addresses) and the field
numbers live in
[ntff-trace-parse-state.md §3](../../neff/ntff-trace-parse-state.md#3-device-notification-type--ntfftrace_type-block_type-jump-table-highobs).

> **CORRECTION — "1:1 device `notific_type` → `ntff` trace_type" is too strong.**
> It is a **collapse with block-tagging**: `INFER_STATUS(3)` folds onto
> `INSTRUCTION(0)`; the three `TOPSP_*` host types **reuse** the
> `{INSTRUCTION, EVENT, ERROR}` trace_types but stamp `block_type=TOPSP(2)`;
> `EXPLICIT(3)` is reachable only from the lower-level device instruction type and
> is **never** produced by this host normalizer. The `block_type` is what
> separates a NeuronCore-engine record (`NC` — including the **GPSIMD/POOL** engine)
> from a DMA record and a Top-SP record. `[HIGH/OBSERVED — jump table read byte-exact]`

> **NOTE — the GPSIMD identity is carried by `block`+engine, not by `trace_type`.**
> A POOL instruction record (device ISA `0x0c..0x0f`) normalizes to host
> `TRACE`/`EVENT` → `ntff` `INSTRUCTION`/`EVENT` with `block=NC`. The fact that it is
> **GPSIMD** is carried in `ntff::engine_instruction_info.nc_engine_type_ == POOL(2)`
> and the `nc_timestamp_info.tpb_pool_` channel — see
> [ntff-trace-parse-state.md §2](../../neff/ntff-trace-parse-state.md#2-ntff-trace-file-format--message-schema-highobs).
> `[HIGH/OBSERVED tie]`

### 5b. The Q7 stdout is a separate path — `sys_trace`, not `ntff` `[HIGH/OBSERVED]`

The Q7 `printf`/stdout the host drains (`exec_consume_gpsimd_stdio` /
`exec_consume_pool_stdio`) is **NOT** a `NEURON_ISA` record — it is the
`pool_stdio` ring, and it enters the **host CPU timeline** via the **separate**
`nrt_sys_trace_*` subsystem (Rust, `neuron_rustime::sys_trace`, 46 event types),
**not** the `ntff` notification path. Do not conflate the two. `[HIGH/OBSERVED]`

---

## 6. The four host observability families — disambiguated `[HIGH/OBSERVED]`

`libnrt` exposes **four overlapping** host-facing families. They are **not one
path**:

| Family | What it is | GPSIMD relevance |
|---|---|---|
| `nrt_profile_*` (+ `notification_*` primitives) | the **device-NQ trace** — subscribe rings, drain 16-byte records, build `ntff::` (§5) | **the GPSIMD path** |
| `nrt_inspect_*` | the **policy/orchestration** layer above `nrt_profile_*` — owns the config (output dir, per-NC/per-event filters, `device_profile_mode` @ `config+0x16c`) | decides whether device profiling runs |
| `nrt_sys_trace_*` | a **host-CPU event timeline** (Rust) — logs runtime-API spans + the device-drain consumers; **does not** parse `NEURON_ISA` records | the Q7-stdio-drain mirror (§5b) |
| `nrt_debug_client_*` | a legacy single-event debug channel (3 syms) | minimal |

The full export catalog, addresses, and the status-code contract are in
[public-api-table.md §8](../../runtime/public-api-table.md#8-profile--trace--inspect--sys_trace--debug-medhighobserved--one-nq-capture-path)
and [libnrt-surface.md §5](../../runtime/libnrt-surface.md#5--the-gpsimd-custom-op--device-dispatch-path).

---

## 7. The gating — "is debug/profiling LOCKED in production?" `[HIGH/OBSERVED]`

There is **no silicon OCD-lock fuse** (`XCHAL_HAVE_SECURE=0`, no FlexLock, no
`PSO_CDM`, `MPU_LOCK=0`). "Locked in production" = **three soft, independent gates
+ the hard SoC perimeter one level out**. This is consistent with the
**integrity-rooted, armed-not-default-secure** posture of the firmware trust chain
(see [trust-chain-threat-model.md](trust-chain-threat-model.md)).

### G1 — firmware FLAVOR (the decisive production gate) `[HIGH/OBSERVED]`

The device firmware ships per `(arch, engine)` in flavors
(`nrtucode_flavors_t {DEFAULT=0, RELEASE=1, DEBUG=2, TEST=3, CHICKEN=8,
DYNAMIC_KERNEL_LOAD=16}`). **"PERF" is the image label for the production build on
v3/v4.** The production image **strips the entire verbose debug path**. Byte-exact
delta — **CAYMAN `NX_POOL`** (the SEQ-engine firmware, the §4 image):

| Flavor | IRAM (B) | DRAM (B) | `'S:'` strings | Note |
|---|---|---|---|---|
| **DEBUG** | 116,768 | 28,448 | **187** | full surprises/break/step/HALT/profile trace |
| **PERF** | 94,848 | 12,320 | **0** | **PRODUCTION** — `Δ` IRAM −21,920 B (21.4 KB), DRAM −16,128 B |
| **TEST** | 92,672 | 13,088 | **0** | validation: named asserts, silent trace |

The strip **generalizes**:

* across gens — **Mariana** `'S:' 177 → 0` (IRAM `Δ` 31.6 KB); **Mariana+**
  `176 → 0`;
* to a **second POOL image family** — **`Q7_POOL`**, the GPSIMD Cadence-Q7 compute
  backend, with its **own** `'P%i:'`-prefixed dispatcher tracer: **CAYMAN
  `Q7_POOL` `'P%i:' 156 → 0`** (IRAM `Δ` 33.7 KB). This is the GPSIMD dispatch
  trace — including the dispatch string `"P%i: In dispatch, CPU ID: %0d, got
  opcode 0x%x."`, present **only** in DEBUG. The production `Q7_POOL` image runs
  the **same dispatch loop silently**.

The verbose `'S:'`/`'P%i:'` surface that the production build strips **is** the
surprises/break/step handler of §4 verbatim, the IRAM-cache machine, the
fetch/redirect FSM, and per-opcode disassembly tracing of the GPSIMD ISA. What
production **retains** is a minimal safety surface: the FATAL assert path
(`"Assertion failure! %s(%s:%u)"` + `exception_handler.hpp` / `alu_op.cpp` etc.),
**one** non-fatal cache `WARNING` (`ok_to_evict`'s out-of-bounds prefetch_pc), and
the build version string.

```text
THREE-FLAVOR SEMANTICS (what ships):
  DEBUG          = full verbose trace (S:/P%i:) + func-name/file asserts + condition text
  TEST           = func-name/file asserts + condition text, NO verbose trace
                   ("named-assert, silent-trace" validation build)
  PERF / RELEASE = FATAL asserts + the one cache WARNING ONLY        *** PRODUCTION ***
```

> **CORRECTION — Sunda has ZERO `'S:'` strings in BOTH flavors.** The `'S:'`
> verbose tracer is the **IRAM-cache / HW-decode / surprises tracer**, which only
> exists on the **v3/v4 HW-decode front-end**. Sunda's SW-fetch FSM never had it.
> So on Sunda, "production" (RELEASE) differs from DEBUG only in **func-name/assert
> presence** (DEBUG keeps build-path function-name strings), **not** in a
> verbose-trace layer that was never there. DX-SEC-04's "187 `'S:'` → 0" is a
> **v3/v4 story**; do not project it onto Sunda. `[HIGH/OBSERVED]`

The host selects the flavor: `nrtucode_get_memory_image` calls
`getenv("NEURON_UCODE_FLAVOR")` and `strcmp`s vs `"debug"`/`"DEBUG"`/`"test"`/
`"TEST"` to promote the selector; **else** the default getter table is the
PERF/RELEASE (production) one. A field device with **no env override loads the
stripped production image**; an operator must explicitly set
`NEURON_UCODE_FLAVOR=debug` to get the verbose firmware. **This is gate G1, located
host-side.** `[HIGH/OBSERVED]`

### G2 — fabric region enable `[HIGH/OBSERVED]`

The privileged `fis_control.apb_user_decode` word carries two independent region
enables: `user_fis_en[16]` (rst 1) gates the USER FIS region (the always-on
`qos_host_visible` monitor); `user_debug_en[24]` (rst 1) gates the USER
`DEBUG_FIS` region (`qos_pmu` + the CoreSight ELA-500). Privileged firmware can
clear `user_debug_en` to region-disable the **entire deep-debug/PMU surface**
without touching the always-on monitor or the compute path. And `qos_pmu` itself
**boots disabled** (all-0 reset; SW must arm `event_select` + matchers +
thresholds). So the fabric PMU is **OFF until explicitly armed AND its region is
enabled** — see [qos-pmu-hostvisible.md §8](../csr/qos-pmu-hostvisible.md#8-interrupt-wiring--how-qos_pmu-reaches-the-intc-high--observed).
`[HIGH/OBSERVED]`

### G3 — host runtime policy `[HIGH/OBSERVED]`

There are **two independent host "arms"**, both gated by host policy:

1. the **notification-ring subscribe** (§5a) — what makes device records reach the
   host — driven by `nrt_profile_*` under `NEURON_RT_INSPECT_DEVICE_PROFILE` /
   `NEURON_RT_INSPECT_ON_FAIL`;
2. the **on-core KDB profiler arm over BAR0** (`hw_decode_table_init`) — gated by
   `NEURON_RT_DBG_HW_DECODE_TOGGLE` / `_BINS_DIR`.

`nrt_inspect_config_set_inspect_device_profile_mode @0xa8320` stores the mode at
`config+0x16c`; `nrt_profile_continuous_start @0xacee0` checks **both**
`continuous_is_enabled` **and** `inspect_device_profile_is_enabled` and **refuses**
if device-profile is on (`"Please disable inspect device profiling to use
continuous profiling. Unset NEURON_RT_INSPECT_DEVICE_PROFILE or set to 0."`) — the
two are **mutually exclusive**. With profiling disabled, `hw_decode_table_init`
still stages the CAM/TABLE on v3/v4 as part of bring-up, but **the trace ring is
not subscribed, so no trace egresses**. `[HIGH/OBSERVED]`

### The HARD perimeter (one level out) `[CARRIED HIGH]`

The OCD/JTAG TAP and the `DEBUG_FIS` region sit **behind the SoC sprot fabric**:
the `amzn_remapper` fail-closed CAM (deny-by-default: privileged `amzn_remapper`
resets `pass_on_miss=0x0`), `qos_prot`/NSM, feeding the PCIe isolation SM. A PCIe
host cannot reach core debug registers except through addresses the remapper
permits; the AWS-controlled host runtime + the physical JTAG probe are the only
debug entry points. There is **no on-core privilege ring to bypass** (single ring,
identity-mapped), so **the perimeter IS the access control** — see
[pkl-intc-sprot-security.md §5](../address/pkl-intc-sprot-security.md#5-the-amzn-fail-closed--user-fail-open-trust-boundary).
`[CARRIED HIGH]`

### The net answer `[HIGH facts / MED synthesis]`

> **Is OCD locked in production?** There is **no OCD-lock fuse**, but in practice:
> * the silicon OCD/TRAX/8-perf-counters are **JTAG-only** (grep-negative on
>   `libnrt`), so a production **host** cannot use them at all;
> * the verbose firmware debug path is **compiled out** of the production
>   PERF/RELEASE flavor (G1);
> * the fabric PMU/ELA region is **privileged-region-gated and boots disabled**
>   (G2);
> * the remaining host-usable profiler (KDB IC0/IC1 + the notification trace) is
>   **host-policy-gated behind the sprot perimeter** (G3 + perimeter).
>
> Production debug access is effectively closed to all but **(i)** a physical JTAG
> probe on the silicon TAP, or **(ii)** the AWS host runtime opting in. It is
> "locked" by **build-flavor + region-enable + host-policy + the fabric
> perimeter** — a **defense-in-layers soft lock, not a hardware fuse**.

> **INFERRED — the production-lock bit is not byte-pinned.** There is no single
> "OCD-disable" register read from a shipped artifact that proves the TAP is dead
> on a deployed device. The "locked" conclusion is a **synthesis of four observed
> soft gates + the silicon's `HAVE_SECURE=0` / `TAP_MASTER=0` config**; whether a
> JTAG probe is physically present/enabled on a deployed AWS board is **out of
> scope** (a board-level fact, not a firmware fact). Treat the "effectively locked"
> verdict as **MED/INFERRED**, and each underlying gate as **HIGH/OBSERVED**.

---

## 8. The complete picture — one diagram `[structure HIGH]`

```text
 ┌──────────────────── SILICON DEBUG MODULE (§1) — xtensa_q7 aperture ────────────────┐
 │ OCD 2 IBREAK + 2 DBREAK + DDR(L32DDR fast) │ TRAX 8 KB / ATB32 / ts64               │
 │ 8 Xtensa perf counters (§3A)               │ transports: ERI / debug-APB / JTAG     │
 │ ints DBG_REQ 31 / BREAKIN 32 / TRAX 33 / PROFILING 34   (TAP slave; SECURE=0)       │
 │   -> reached ONLY via JTAG/external probe (NOT in libnrt). G1/perimeter lock.       │
 └───────────────┬─────────────────────────────────────────────────────────────────────┘
                 │ (HW IBREAK/DBREAK + SW BREAK patch -> debug event)   [INFERRED wiring]
 ┌───────────────▼──── ON-CORE KDB / DECODE PROFILER (§2) — hw_decode @0x4000 ──────────┐
 │ control 0x4000 (disable_hw_decode) │ breakpoint_ctrl 0x4004 (instr/profile-table bp, │
 │   force-pause, step, addr0/1, ic0/ic1) │ step 0x4008 │ match 0x400C │ ic thr 0x4010/14│
 │   │ addr 0x4018.. │ profile_cam_search 0x4028 │ flush 0x402C                          │
 │ PROF_CAM 64x16B {opcode,mask,enable} -> match -> PROF_TABLE 64x128B descriptor        │
 │   -> IC0/IC1 increment + bp-from-table  (host-armed via BAR0; v3/v4 only; Sunda none) │
 └───────────────┬─────────────────────────────────────────────────────────────────────┘
                 │ INS_BREAK / EXT_BREAK / STEP_CNT   (surprise bits 2 / 3 / 8)
 ┌───────────────▼─────────── POLLED SURPRISES HANDLER (§4) — not vectored ──────────────┐
 │ poll -> sunda_check_surprises -> sunda_handle_surprises:                              │
 │   STRONG_ORDER(9) continue │ INS_BREAK(2) Halt r4 │ EXT_BREAK(3) clr 0x14 Halt r8 │   │
 │   STEP_CNT(8) dec->Halt r64 │ else "Unhandled surprise" FATAL spin                    │
 │   -> Setup Halt -> "Entering HALT" (BREAK_CTRL RMW @0x4004)   [DEBUG-flavor logs only] │
 └───────────────┬─────────────────────────────────────────────────────────────────────┘
                 │ 16-byte NEURON_ISA record (ps ts = cfg_timestamp_inc; POOL type 0x0c-0x0f)
 ┌───────────────▼─────────────── TRACE EGRESS (§5) — one ring, one MSI-X ───────────────┐
 │ tpb_notific sw_queue_num3 errors_NT -> MSI-X -> host nrt_profile_*:                   │
 │   subscribe_nc (get_nq_ids_for_type) -> read (16-B stride) -> read_all_nc ->          │
 │   convert_trace_type_from_ntff_params (COLLAPSE-with-block-tagging, NOT 1:1) ->        │
 │   ntff::ntff_info { nc_timestamp_info.tpb_pool_ = GPSIMD ; engine_info nc_type=POOL }  │
 │   -> nrt_profile_serialize -> .ntff Neuron-Trace-File                                 │
 │   (PARALLEL host-CPU timeline: nrt_sys_trace_* drains the Q7 pool_stdio ring)          │
 └───────────────────────────────────────────────────────────────────────────────────────┘
 ┌──────────────── FABRIC qos_pmu (separate, §3C) — DEBUG_FIS region ────────────────────┐
 │ gated user_debug_en[24], boots disabled, next to CoreSight ELA-500:                   │
 │ 8 counters + 4 AXI matchers (640b) + 64b thr + dual-win 48b snap -> fis_sprot_intr[4] │
 │ (qos_host_visible: always-on USER-FIS monitor — a separate AXI-observability domain)   │
 └───────────────────────────────────────────────────────────────────────────────────────┘
```

The three stacks **never share a register**: the silicon debug module (top) is
JTAG-only; the KDB profiler + surprises + notification trace (middle) is the
host-reachable production path; the fabric PMU (bottom) is a separate AXI-
observability domain.

---

## 9. Reimplementation checklist `[HIGH unless noted]`

1. **Two debug surfaces, two address spaces.** The Xtensa silicon debugger is the
   `xtensa_q7` 0x4000-span aperture (TRAX/PMU/OCD/Misc/CoreSight, JTAG/APB). The
   firmware-driven KDB is the `hw_decode` bundle at base `0x4000` inside
   `tpb_xt_local_reg` (16-bit addr, 0x10000 span). **Do not merge them on the
   shared `0x4000` token.**
2. **The profiler is opcode-match-then-count.** `PROF_CAM` (64×16 B opcode CAM)
   selects 4 instruction bytes per `profile_cam_search` (0x4028); a hit indexes
   the parallel `PROF_TABLE` (64×128 B); IC0/IC1 increment on the matched opcode
   and raise a `breakpoint` surprise on threshold or a profile-table bp.
3. **The debug consumer is polled, not vectored.** INT 31–34 are HW-armable but
   the firmware never installs an ISR; all breakpoint/step events land in the
   2-byte surprises word and funnel to `Setup-Halt`.
4. **One record, one ring, two paths.** Faults and trace both emit the 16-byte
   `NEURON_ISA` record into `sw_queue_num3 errors_NT`. The host classifies on byte
   `+0x03` (the 5-bit `notific_type`); POOL = `0x0c..0x0f`.
5. **The reformat is a collapse, not an identity.** `convert_trace_type_from_
   ntff_params` is a 9-entry jump table; `INFER_STATUS→INSTRUCTION`,
   `TOPSP_*→block=TOPSP`. The GPSIMD identity rides in
   `block=NC + nc_engine_type=POOL + tpb_pool_`.
6. **Production is gated by flavor + region + policy + perimeter, not a fuse.** A
   field device loads the PERF/RELEASE image (0 verbose strings); the host must
   opt into profiling via `NEURON_RT_INSPECT_DEVICE_PROFILE` /
   `NEURON_RT_DBG_HW_DECODE_TOGGLE`; the fabric debug region boots disabled; the
   TAP/region sit behind the fail-closed sprot fabric.
7. **`v5` / Maverick:** header-OBSERVED only — the descriptors appear identically
   in the `maverick` tree, but every v5-interior behaviour is **INFERRED · CARRIED**
   from Cayman/Mariana.

---

## 10. Confidence & provenance ledger

**HIGH / OBSERVED:** the `ncore2gp` debug-module config (OCD 2 IBREAK/2 DBREAK/DDR
L32DDR, TRAX 8 KB/ATB32/ts64, 8 perf counters, 3 transports, `TAP_MASTER=0`,
`PSO_CDM=0`, `SECURE=0`, ints 31–34); the `HAVE_APB=0` vs `HAVE_DEBUG_APB=1`
disambiguation; `specreg.h` (no XEA2 debug regs; `DDR=104`, `ERACCESS=95`); the
HAL soft-break helpers; the `hw_decode` `0x4000` field map; the device DEBUG-image
surprises taxonomy + `halt`/`break.n`/`const16-0x4000`/`RER`/`WER` disasm; the
flavor census (187/177/176 `'S:'` DEBUG → 0 PERF, the 21.4/31.6 KB IRAM deltas, the
`Q7_POOL` 156 `'P%i:'` → 0); the `libnrt` `nrt_profile_*` subscribe/drain/convert/
serialize spine with addresses; the `convert_trace_type_from_ntff_params` 9-entry
jump table; `NBYTES=16` + the device ISA `notific_type` enum (POOL = 12–15);
`notifications_get_nq_ids_for_type`; `NEURON_UCODE_FLAVOR` host select; the
mutual-exclusion gate.

**CARRIED / HIGH (cross-linked, not re-derived):** the `xtensa_q7` aperture map
([xtensa-q7.md](../csr/xtensa-q7.md)); the `PROF_CAM`/`PROF_TABLE` byte formats
([prof-cam-table-formats.md](../../images/prof-cam-table-formats.md)); the host
CAM programming ([hw-decode-cam-programming.md](../../runtime/hw-decode-cam-programming.md));
the polled-surprises determination + FSM bodies
([q7-surprises-binding.md](../interrupt/q7-surprises-binding.md),
[surprises-irq.md](../../firmware/seq/surprises-irq.md)); the two-layer SEQ debug
model + single-step ([uarch-debugger.md](../../firmware/seq/uarch-debugger.md));
the 16-byte record wire format
([device-host-notification.md](../interrupt/device-host-notification.md)); the
`ntff::` schema + jump table ([ntff-trace-parse-state.md](../../neff/ntff-trace-parse-state.md));
`qos_pmu` geometry ([qos-pmu-hostvisible.md](../csr/qos-pmu-hostvisible.md)); the
XEA3 register-file proof ([xea3-interrupt-architecture.md](../interrupt/xea3-interrupt-architecture.md));
the fail-closed sprot perimeter ([pkl-intc-sprot-security.md](../address/pkl-intc-sprot-security.md)).

**MED / INFERRED (flagged inline):** the on-core 8 Xtensa perf counters'
non-exposure to the host (HIGH config; "JTAG-only" is from the libnrt
grep-negative — a strong negative); the HW-`IBREAK`→`INS_BREAK` surprise wiring
(shared egress, not byte-pinned); TRAX's PC/branch-trace role (standard-Tensilica
inference); the "effectively locked in production" synthesis; the TRACE-type empty
NQ-set ⇒ instruction-block capture reading; the DMA NQ-id `.bss` runtime fill.

**LOW / OPEN:** the per-counter event ENUM behind the on-core 8 perf counters
(lives in ERI space, not in the shipped headers); the `PROF_TABLE` 128-B descriptor
beyond the `+0x14` class byte; the `qos_pmu` `event_select` one-hot / 3-bit cmp
enums; whether Mariana POOL is later armed via the file-override path; whether a
JTAG probe is physically present/enabled on a deployed board (board-level, out of
scope).

---

## See also

* [CSR — Xtensa Q7 Debug / Trace / PMU / OCD](../csr/xtensa-q7.md) — the silicon
  debug aperture (Layer A), byte-exact.
* [PROF_CAM / PROF_TABLE Blob Formats](../../images/prof-cam-table-formats.md) — the
  profiler blobs; [HW-Decode CAM-Table Programming](../../runtime/hw-decode-cam-programming.md)
  — the host arm.
* [INTC → Q7 Firmware "Surprises" Binding](../interrupt/q7-surprises-binding.md) /
  [SEQ Surprises / IRQ Poll](../../firmware/seq/surprises-irq.md) — the polled
  consumer; [SEQ Uarch Register Model + Single-Step Debugger](../../firmware/seq/uarch-debugger.md)
  — the two-layer model.
* [Device→Host Interrupt / Notification Path](../interrupt/device-host-notification.md)
  — the 16-byte record; [ntff Trace Protobuf + NEFF Parse State](../../neff/ntff-trace-parse-state.md)
  — the trace-file schema + jump table.
* [CSR — qos_pmu + qos_host_visible](../csr/qos-pmu-hostvisible.md) — the fabric PMU;
  [The XEA3 Interrupt / Exception Architecture](../interrupt/xea3-interrupt-architecture.md)
  — the core dispatch model.
* [The `nrt` Host API Surface Reference](../../runtime/public-api-table.md) /
  [The libnrt Surface Map](../../runtime/libnrt-surface.md) — the host export
  catalog.
* Security siblings (pending authoring):
  [The SoC-Fabric Perimeter](soc-fabric-perimeter.md),
  [Firmware Trust Chain + Threat Model](trust-chain-threat-model.md),
  [Custom-Op Reachability / Isolation Model](reachability-isolation.md),
  [Information-Leakage / Side-Channel Surface](side-channel-leakage.md),
  [SEC-Lane Synthesis](security-synthesis.md).
