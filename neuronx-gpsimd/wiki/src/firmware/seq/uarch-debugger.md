# SEQ Uarch Register Model + Single-Step Debugger

The NX sequencer (SEQ) that drives a GPSIMD pool engine ships a **DEBUG build**
(`img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}`) whose firmware exposes an on-device
**single-step / breakpoint debugger**. This page reconstructs, from the shipped
firmware image and the shipped register definitions, two things:

1. the **micro-architectural register model** the firmware uses for debug — the
   `hw_decode` breakpoint register bundle of `tpb_xt_local_reg`; and
2. the **single-step + breakpoint machinery** built on top of it, which is *not*
   the Xtensa core's own ICOUNT/IBREAK/DBREAK debugger but a custom hardware
   block whose firings are delivered as **polled surprises**
   ([surprises-irq.md](surprises-irq.md)) and halted through the run-state FSM's
   Setup-Halt ([run-state.md](run-state.md)).

All confidence tags follow the [confidence model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = byte-read from the shipped disasm or the shipped register
JSON; `[…/INFERRED]` = reasoned over those reads; `[…/CARRIED]` = consolidated
from a cited sibling page without re-reading the artifact. The page default is
`[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

---

## 0. Provenance — the image, the tool, the carve

The DEBUG sequencer image is carved from the static library `libnrtucode.a`,
member `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` (and its `_DRAM` sibling).
Those `.c.o` members are **host x86-64 relocatables** that wrap the firmware
bytes as a C array in `.rodata`; the firmware itself is carved with the host
`objcopy --only-section=.rodata` and then disassembled with the **native
Vision-Q7 toolchain** (`XTENSA_CORE=ncore2gp`, RI-2022.9 / NX1.1.4, FLIX/VLIW,
`IsaMaxInstructionSize=32`). `[HIGH/OBSERVED]`

| Carve | Size | sha256 (head) | Device VA |
| ----- | ---- | ------------- | --------- |
| `nx_iram.bin` | 116768 B | `8e4412b9…` | IRAM @ `0x0` |
| `nx_dram.bin` | 28448 B | `7bdf6ed7…` | DRAM @ `0x80000` |

Both hashes are **byte-identical** to the [boot](boot.md) / [run-state](run-state.md) /
[surprises](surprises-irq.md) anchors — the four SEQ-internals pages decode the
same image. The IRAM disassembly is 45 901 lines, exit 0. A DRAM string's
**file offset** = its device VA − `0x80000`. `[HIGH/OBSERVED]`

> **NOTE — code addresses are IRAM-image offsets (== device IRAM VA).** The
> `hw_decode` CSR offsets below are written as the firmware builds them
> (`0x04004000 + off`); §5 reconciles that firmware-built absolute against the
> shipped flat address map, where it resolves differently.

The debug **`__FILE__`-style trace strings** the DEBUG build bakes into DRAM
`.rodata` are read directly as firmware data (`strings -t x nx_dram.bin`, then
+`0x80000`): `[HIGH/OBSERVED]`

| DRAM VA | string |
| ------- | ------ |
| `0x81302` | `S: enter_run: start` |
| `0x81336` | `S: enter_run: done` |
| `0x81a0f` | `S: INS_BREAK` |
| `0x81a1d` | `S: EXT_BREAK` |
| `0x81a42` | `S: STEP_CNT=0` |
| `0x81a51` | `S: STEP_CNT=>%d` |
| `0x81efb` | `S: INS_FL` |
| `0x81f06` | `S: SET_OM` |
| `0x81f11` | `S: BREAK_CTRL=0x%x -> 0x%x` |
| `0x80f1e` | `S: NX in HW Decode mode` |

> **NOTE on the `S:` count discipline.** These are a subset of the engine's
> trace strings. The full table is conventionally counted two ways:
> **178** = dispatch-table entries = `S:` NUL-records (one per slot), and
> **187** = literal `S: ` (trailing space) substring instances —
> `rg -aoN 'S: ' nx_dram.bin | wc -l` = **187**. This
> page only asserts the individual debug strings above, each grounded by its
> own VA; it makes no bare "N strings" claim.

---

## 1. The two-layer debug model (the central reconciliation)

A GPSIMD SEQ silicon has **two physically distinct debug surfaces**, and the
firmware single-step debugger uses the *second*, never the first.

**Layer A — the Xtensa-core OCD / TRAX / PMU block** (the Q7 debug aperture;
[xtensa-q7.md](../../control/csr/xtensa-q7.md)). This is the standard Cadence
Xtensa **halt-mode debugger** reached over JTAG/APB by an *external host*. It
single-steps the *Xtensa core itself* by instruction injection (OCD
`DIR0EXEC`). It is the silicon's own debugger and is driven from the debug bus,
**not** by the SEQ firmware. Layer A also subsumes the Xtensa
**ICOUNT/IBREAK/DBREAK special registers** — likewise unused by the SEQ
firmware (§3). `[HIGH/OBSERVED — negative result, verified in §3]`

**Layer B — the SEQ `hw_decode` breakpoint/single-step engine** (the
`hw_decode` bundle of `tpb_xt_local_reg`; [tpb-xt-local-reg.md](../../control/csr/tpb-xt-local-reg.md)).
This is a **custom hardware block** that watches the *micro-op / ucode PC* the
SEQ executes (not the Xtensa PC) and raises a **surprise** when a
breakpoint / step-count / address / opcode-count condition fires. **The firmware
single-step debugger is Layer B.** `[HIGH/OBSERVED]`

```
        EXTERNAL HOST                          EXTERNAL HOST
            │ JTAG/APB                              │ arms hw_decode CSRs
            ▼                                       ▼  + start_ctrl doorbell
   ┌──────────────────┐                   ┌──────────────────────────┐
   │  LAYER A          │                   │  LAYER B                  │
   │  Xtensa OCD/TRAX/ │  (orthogonal,     │  hw_decode breakpoint/    │
   │  PMU + ICOUNT/    │   disjoint —      │  step block (custom HW)   │
   │  IBREAK/DBREAK    │   firmware never  │  watches the micro-op PC  │
   │  steps the CORE   │   touches it)     │  raises a SURPRISE        │
   └──────────────────┘                   └────────────┬─────────────┘
                                                        │ polled surprise word
                                                        ▼  (surprises-irq.md)
                                          ┌──────────────────────────┐
                                          │  Setup-Halt @0x1cf8       │
                                          │  save resume PC lr[3]/[4] │
                                          │  + reason code            │
                                          │       (run-state.md)      │
                                          └──────────────────────────┘
```

> **GOTCHA — "the debug exception" on a SEQ is not an Xtensa DEBUG-level
> exception.** It is a *polled software break* routed through the run-state
> FSM's Setup-Halt. The only true Xtensa exception in the loop is the fatal
> path (illegal op / OOB / div0, [error-handler.md](error-handler.md)), which is
> a different surface (§6). `[HIGH/OBSERVED]`

---

## 2. The uarch register model — the `hw_decode` breakpoint bundle

The micro-architectural register set the SEQ debugger exposes is the
**`hw_decode` register bundle of `tpb_xt_local_reg`**. The shipped register
definition (`tpb_xt_local_reg.json`, `UnitName: tpb_xt_local_reg`,
`InterfaceType: APB`, `AddrWidth: 16`, `SizeInBytes: 0x10000`) declares the
bundle verbatim as: `[HIGH/OBSERVED]`

```json
"Name": "hw_decode", "AddressOffset": "0x04000", "BundleSizeInBytes": "0x01000"
```

The firmware builds these CSR addresses as `0x04004000 + off` (the engine-local
view — see §5 for the global-map caveat). The full bundle, grounded against the
JSON: `[HIGH/OBSERVED]`

| Bundle off | abs (fw-built) | register | role |
| ---------- | -------------- | -------- | ---- |
| `0x000` | `0x04004000` | `control` | ordering-mode + IRAM-flush enable (§4) |
| `0x004` | `0x04004004` | `breakpoint_ctrl` | the arm-bit register (table below) |
| `0x008` | `0x04004008` | `breakpoint_step` | `num_instr` — the single-step countdown (§4) |
| `0x00C` | `0x0400400C` | `breakpoint_match` | IC0/IC1 opcode + mask |
| `0x010` | `0x04004010` | `breakpoint_ic0` | IC0 opcode-count threshold |
| `0x014` | `0x04004014` | `breakpoint_ic1` | IC1 opcode-count threshold |
| `0x018` / `0x01C` | `0x04004018` / `0x0400401C` | `breakpoint_addr0_lo` / `_hi` | instruction breakpoint address 0 |
| `0x020` / `0x024` | `0x04004020` / `0x04004024` | `breakpoint_addr1_lo` / `_hi` | instruction breakpoint address 1 |
| `0x028` | `0x04004028` | `profile_cam_search_vector` | (profile-table CAM; not used by the debugger path) |
| `0x02C` | `0x0400402C` | `hw_decode_flush_cntr` | (flush counter) |
| `0x030` / `0x034` | `0x04004030` / `0x04004034` | `spare0` / `spare1` | — |

### 2.1 `control` (off `0x000`) — bitfields verbatim from the JSON

| Position | field | reset | role |
| -------- | ----- | ----- | ---- |
| `0` | `disable_hw_decode` | `0x0` | select Instr-FIFO vs HW-decode path; chicken-bit on all engines but TPB SP |
| `1` | `instr_ordering_mode` | `0x0` | strong/relaxed ordering — *"NX programs this bit based on the SetOrderingMode (ctrl_om) instruction"* (the SET_OM write, §4.3) |
| `18:2` | `iram_block_size_mask` | `0x1FFF` | masks the cache-block index off the IRAM offset to form the full PC |
| `20` | `iram_ctrl_flush_en` | `0x0` | *"should only be set to 1 for the SP and TOP instance"* |

> **CORRECTION — the flush field is bit 20 only, but the firmware also writes
> bit 21.** The shipped JSON names a *single* flush bit, `iram_ctrl_flush_en` at
> Position `20`. The backing analysis described "bits 20/21"; the reg-file
> documents **only bit 20**. However, the SET_OM handler (§4.3) demonstrably
> computes a mask of **bit 20 *or* bit 21** selected by the exec-mode flag —
> `0x86af: slli a2,a2,20` vs `0x86b4: slli a3,a3,21`,
> `0x86b7: bnez.n a4` choosing bit 20 when the flag is set. So **bit 21 is a
> firmware-written `control` bit that the shipped reg-file does not name** — a
> genuine firmware-vs-spec divergence, not a documentation typo.
> `[HIGH/OBSERVED — JSON Position=20; disasm shows the bit-20/21 select]`

### 2.2 `breakpoint_ctrl` (off `0x004`) — the arm register, verbatim

| Position | field | reset | meaning (JSON description, condensed) |
| -------- | ----- | ----- | ------------------------------------- |
| `0` | `breakpoint_instr_enable` | `0x1` | enable/disable instruction breakpoint |
| `1` | `breakpoint_profile_table_enable` | `0x1` | breakpoint sourced from the profile table (debug without reloading entries) |
| `2` | `ucode_force_pause_enable` | `0x1` | enable use of NX's force-pause output |
| `3` | `breakpoint_step_valid` | `0x0` | *"num_instr is valid. uCode will set valid=0 then 1… rtl will begin counting instructions until num_instr matches, then pause sync_stage"* — **the single-step arm** |
| `4` | `breakpoint_addr0_valid` | `0x0` | enable breakpoint-address-0 check |
| `5` | `breakpoint_addr1_valid` | `0x0` | enable breakpoint-address-1 check |
| `6` | `ic0_valid` | `0x0` | clear HW IC0 counter on invalid, start counting on valid |
| `7` | `ic1_valid` | `0x0` | clear HW IC1 counter on invalid, start counting on valid |
| `8` | `immediate_pause` | `0x0` | immediate pause after the current instruction — *"Typically set by external host"* |

> **CORRECTION — `breakpoint_ctrl` bits 1 and 2 exist and the backing report
> omitted them.** The arm register has `breakpoint_profile_table_enable` (bit 1)
> and `ucode_force_pause_enable` (bit 2) in addition to the step/addr/ic/pause
> bits. `[HIGH/OBSERVED from the JSON]`

### 2.3 `breakpoint_match` (off `0x00C`) — opcode-match config, verbatim

| Position | field | role |
| -------- | ----- | ---- |
| `7:0`  | `ic0_mask`   | mask for the IC0 opcode counter |
| `15:8` | `ic0_opcode` | if `instr.opcode & ic0_mask == ic0_opcode`, increment the IC0 counter |
| `23:16`| `ic1_mask`   | mask for the IC1 opcode counter |
| `31:24`| `ic1_opcode` | if `instr.opcode & ic1_mask == ic1_opcode`, increment the IC1 counter |

`breakpoint_ic0` / `breakpoint_ic1` (off `0x010` / `0x014`) each carry a single
whole-word `value [31:0]` — the **count threshold** at which the matched-opcode
histogram breaks. Likewise every `breakpoint_addr{0,1}_{lo,hi}` is a single
`value [31:0]`, together forming a 64-bit micro-op address. `[HIGH/OBSERVED]`

---

## 3. What the SEQ debugger does *not* use — the Xtensa debug SRs / OCD

The Xtensa core's *own* debugger (Layer A) is **never touched by the SEQ
firmware**. This is the negative result that re-scopes the brief; it is a hard
census, not an absence of evidence.

**(a) No OCD aperture access.** A `const16`-build scan of the SEQ image for the
Q7 OCD/TRAX/PMU aperture finds *only* the run-state CSRs at `0x04000000` and the
`hw_decode` CSRs at `0x04004000`. The Q7 OCD aperture
(`OCD_Registers` base `0x2000`: `DCRSET 0x200C`, `DCRCLR 0x2008`, `DSR 0x2010`,
`DDR 0x2014`, `DIR0EXEC 0x201C`, `DIR0..7 0x2020..0x203C`) and TRAX
(`TRIGGERPC 0x14`, `PCMATCHCTRL 0x18`) — all documented in
[xtensa-q7.md](../../control/csr/xtensa-q7.md) — are **not addressed** by any
firmware site. `[HIGH/OBSERVED — negative]`

**(b) No Xtensa debug special-registers.** A full `rsr`/`wsr`/`xsr`
enumeration of the SEQ image shows the firmware uses only:
`br` (×15), `prefctl` (×11), `memctl`, `mpuenb`, `isl`, `prid`, `lbeg`, `wb`,
`ccount`, `vecbase`, `ps`, `ms`, `isb`, `excvaddr`, `exccause`, `cacheadrdis`.
**Every** Xtensa debug SR returns **zero** hits:

```
$ rg -ciN 'icount'       nx_iram.dis  →  0
$ rg -ciN 'icountlevel'  nx_iram.dis  →  0
$ rg -ciN 'ibreakenable' nx_iram.dis  →  0
$ rg -ciN 'dbreakc'      nx_iram.dis  →  0
$ rg -ciN 'dbreaka'      nx_iram.dis  →  0
$ rg -ciN 'debugcause'   nx_iram.dis  →  0
$ rg -ciN 'rfdo'         nx_iram.dis  →  0   (no DebugException return)
$ rg -ciN 'rfdd'         nx_iram.dis  →  0
$ rg -ciN 'wsr.intenable' nx_iram.dis →  0   (no vectored interrupts)
$ rg -ciN 'epc2|epc3'    nx_iram.dis  →  0   (no high-level EPC pairs)
```

> **QUIRK — the three `rsr.ibreaka1`/`rsr.ibreakc1` lines are FLIX misdecodes,
> not breakpoint programming.** The objdump prints `rsr.ibreakc1` at `0x119da`
> and `rsr.ibreaka1` at `0x127e6` / `0x131e6`. All three sit **inside** IVP wide
> bundles in the operator-library region (`0x11xxx`–`0x13xxx`), *not* the SEQ
> FSM (`0x2c00`–`0x8800`). Two of them are literally inside a brace-bundle the
> tool itself prints whole, e.g. at `0x127e0`:
> `{ bbci.w15 …; ivp_lsrn_2x32_x …; ivp_mul4tan16xr8 …; ivp_rep2nx8t …; ivp_bminu2nx8 … }`
> — the bytes `008103` that decode as `rsr.ibreaka1` are the *middle* of that
> bundle, not a standalone RSR. The third (`0x119da`) is flanked by `.byte`
> fragments and an `iii` op. They are noise. See
> [flix-decoding.md](../../reference/flix-decoding.md). `[HIGH/OBSERVED]`

Cross-check against the shipped Q7 register file: `xtensa_q7.json` is an
**offset-based APB aperture** and contains **no** `IBREAKA`/`DBREAKA`/`ICOUNT`/
`DEBUGCAUSE` register at all — those are *special-register numbers* (accessed by
RSR/WSR), absent from the memory-mapped block. So Layer A's breakpoint
machinery is doubly out of the firmware's reach: not in the aperture, not in the
SR set the firmware references. `[HIGH/OBSERVED]`

**Conclusion.** The SEQ single-step debugger is the custom `hw_decode`
breakpoint/step block (Layer B), delivered as a polled surprise and halted
through Setup-Halt — never the Xtensa core's ICOUNT/IBREAK/DBREAK + DEBUG-vector
machinery.

---

## 4. The run-entry snapshot + the single-step mechanism

### 4.1 The snapshot function `0x553c` (the uarch register-model read-back)

On every run/resume, `enter_run` ([run-state.md](run-state.md) §3a, @`0x2c64`)
calls the snapshot at `0x553c` (via wrapper `0x5504`, `call8` at `0x2cd0`). The
snapshot reads the **entire** `hw_decode` breakpoint set back into a composite
status word, and seeds the software single-step counter. Annotated decode (real
symbols, byte-exact): `[HIGH/OBSERVED]`

```c
// snapshot_breakpoints() @0x553c   (entry a1,112 — a large frame: it materialises
//                                   the whole register model into locals)
uint32_t snapshot_breakpoints(ctx_t *ctx) {
    uint32_t word = 0;

    // 5542: const16 a2,0x4008 ; 5545: l32i.n  — READ breakpoint_step.num_instr
    // 554a: const16 a3,0x5650 ; 554d: s32i.n  — MIRROR into DRAM step counter 0x85650
    uint32_t num_instr = *(u32*)0x04004008;
    *(u32*)0x00085650  = num_instr;          // <<< seeds the SW single-step countdown

    // 5552: const16 a2,0x4000 ; 5555: l32i.n  — READ control
    // 555d: and a2,2 ; 5560: srli a2,1        — extract control.instr_ordering_mode (bit1)
    uint32_t control   = *(u32*)0x04004000;
    uint8_t  ordering  = (control >> 1) & 1;

    // 5566: const16 a2,0x55e0 ; l8ui [+108] ; bbsi a2,0,0x5575
    //   state[0x855e0+108] exec-mode flag: bit0 set -> alternate aperture path (j 0x570c)
    if (state_exec_mode_bit0) goto alt_aperture; // (the Sunda/alt path; Cayman falls through)

    // 558d: const16 a2,0x4004 ; 5590: l32i.n  — READ breakpoint_ctrl
    uint32_t bctrl     = *(u32*)0x04004004;

    if (bctrl & (1<<0))               word |= 0x004;        // instr_enable  -> INS_BREAK (bit2)
    if ((bctrl & (1<<3)) && *(u32*)0x00085650 != 0) {       // step_valid & counter!=0
        word |= 0x100;                                      //   STEP_CNT (bit8)
        word |= (1<<20);                                    //   internal "step-active" marker
    }
    if (bctrl & (1<<4)) {                                   // addr0_valid
        word |= 0x001;                                      //   addr0 word bit0
        (void)*(u32*)0x0400401C;  (void)*(u32*)0x04004018;  //   read addr0_hi, addr0_lo
    }
    if (bctrl & (1<<5)) {                                   // addr1_valid
        word |= 0x002;                                      //   addr1 word bit1
        (void)*(u32*)0x04004024;  (void)*(u32*)0x04004020;  //   read addr1_hi, addr1_lo
    }
    // 5667: word |= (1<<13)  (ic0) ; 5676: read breakpoint_match(0x400c) -> ic0 mask/opcode
    // 56ad: test bctrl bit7 (ic1_valid) ; 56c6: word |= (1<<12) ; 56d4: read match(0x400c) -> ic1
    return word;     // 5704: l32i.n a2,[assembled word] ; j 0x5727 -> return
}
```

The status word the snapshot assembles **mirrors the surprise-word layout**:
`bit2`=INS_BREAK enable, `bit8`=STEP_CNT, `bit0/1`=addr0/1, `bit12/13`=IC1/IC0,
`bit20`=step-active. The first four match the surprise word
([surprises-irq.md](surprises-irq.md)) exactly; `bits 12/13/20` are internal
bookkeeping the snapshot carries that the surprise handler does not test.
`[HIGH/OBSERVED — every const16 CSR address and every OR-bit byte-read]`

A companion `0x573c` (wrapper `0x5514`, `call8` at `0x2cd5`) clears a byte in the
per-engine context struct (`ctx` at DRAM `0x85068`, `[ctx+0]=0`) — a
"debug-event consumed" reset on entry. `[HIGH/OBSERVED]`

> **GOTCHA — the breakpoint CSRs are firmware-read-only; `control` is the sole
> exception.** Re-checking the polarity of every `hw_decode` access in the FSM:
> `breakpoint_ctrl` (`0x4004`), `breakpoint_step` (`0x4008`),
> `breakpoint_match` (`0x400c`), and all four `addr` words are followed *only by*
> `l32i.n` (read). `control` (`0x4000`) is the only one with a following
> `s32i.n` (write) — at `0x2453` (HW-Decode-mode setup) and `0x86fd` (SET_OM).
> So **`num_instr` and every arm bit are host-programmed; the firmware merely
> reads them back.** `[HIGH/OBSERVED — a verified full-image negative]`

### 4.2 Single-step — the seed→countdown→consume chain

Single-step is a **hardware countdown** in `hw_decode`, mirrored to a software
counter and consumed as a polled surprise. The shipped JSON description of
`breakpoint_step_valid` states the hardware contract directly: *"uCode will set
valid=0 then 1… rtl will begin counting instructions until num_instr matches,
then pause sync_stage."* The full chain, instruction-exact:

```
(1) ARM   (host -> CSRs)        breakpoint_step.num_instr (0x04004008) = N
                                breakpoint_ctrl.breakpoint_step_valid (0x04004004 bit3) = 1
                                [firmware never writes 0x4008/0x4004 — read-only, §4.1]

(2) SEED  (enter_run -> 0x553c) num_instr read back; *(u32*)0x00085650 = num_instr
                                if (step_valid && counter!=0) word |= STEP_CNT(0x100) | step-active(1<<20)

(3) COUNT (hw_decode RTL)       num_instr decremented per executed micro-op;
                                at 0 the STEP_CNT surprise bit is raised  [HW behaviour per the JSON]

(4) CONSUME (surprises poll)    the STEP_CNT surprise handler reads DRAM 0x85650:
                                  == 0 -> Setup-Halt(reason=64)  (single-step complete)
                                  != 0 -> decrement 0x85650 and continue
```

The **consume** site, byte-exact in the surprises handler (the STEP_CNT arm
@`0x6db4`): `[HIGH/OBSERVED]`

```c
    // 6db7: const16 a2,0x5650 ; 6dba: l32i.n  — read DRAM step counter 0x85650
    uint32_t c = *(u32*)0x00085650;
    if (c == 0) {
        // 6dc7: const16 a10,0x1a42 ; 6dca: call8 0x18b84 — log "S: STEP_CNT=0"
        setup_halt(reason = 64);                 // single-step complete -> run-state.md Setup-Halt
    } else {
        // 6def: const16 a10,0x1a51 ; 6df2: call8 0x18b84 — log "S: STEP_CNT=>%d", c
        // 6e00: l32i a3,[0x85650] ; 6e02: addi.n a3,-1 ; 6e04: s32i a3,[0x85650]
        *(u32*)0x00085650 = c - 1;               // DECREMENT and resume
    }
```

So there are **two countdowns in lock-step**: the HW
`breakpoint_step.num_instr` (RTL) and the SW mirror at DRAM `0x85650`. The seed
(`0x554d s32i`) and the consume (`0x6e02 addi -1`) reference the **same**
counter — the linkage is proven by the shared `const16 0x5650` at `0x554a`,
`0x55be`, `0x6db7`, `0x6de7`, `0x6dfd`. `"Step 1"` is `num_instr = 1`.
`[HIGH/OBSERVED — the whole chain is byte-anchored except the RTL decrement, which is HW]`

### 4.3 `SET_OM` / `INS_FL` — the only firmware `hw_decode` writes

Two co-resident ucode opcode handlers manipulate `control` (`0x04004000`).

**`SET_OM` @`0x8684`** (the `SetOrderingMode` / `ctrl_om` handler). It logs
`S: SET_OM` (`0x81f06`), reads the exec-mode flag, selects the flush mask
(bit 20 vs bit 21, §2.1), reads `control`, clears `instr_ordering_mode` (bit 1),
then — depending on the argument — OR-sets bit 1 (strong order) via the
bit-log helper `0x8720` or AND-clears it via `0x874c`, and writes `control`
back. Byte-exact: `[HIGH/OBSERVED]`

```c
// set_ordering_mode(arg) @0x8684
// 868d: const16 a10,0x1f06 ; 8690: call8 0x18b84   — log "S: SET_OM"
// 86a7: l8ui state[0x855e0+108] ; 86aa: extui exec-mode bit0
// 86af: slli 20 / 86b4: slli 21 / 86b7: bnez -> select flush mask (bit20 if exec-mode set)
// 86c0: const16 a2,0x4000 ; 86c3: l32i.n           — READ control
// 86c5: movi a3,-3 ; 86c7: and                       — clear bit1 (instr_ordering_mode)
// 86ce: bnei a2,1,0x86e7:
//    arg==1 -> 86d7: call8 0x8720 (OR-set helper, logs "S: BREAK_CTRL=0x%x -> 0x%x")
//             86df: or a2,2                           — set bit1 (strong order)
//    else   -> 86ea: call8 0x874c (AND-clear helper)
// 86fa: const16 a3,0x4000 ; 86fd: s32i.n            — WRITE control back
// 870e: memw
```

This is the **write side** of `control.instr_ordering_mode`, matching the JSON's
*"NX programs this bit based on the SetOrderingMode (ctrl_om) instruction."*
The bit-log helpers `0x8720` (OR/set) and `0x874c` (AND/clear) both emit
`S: BREAK_CTRL=0x%x -> 0x%x` (`0x81f11`) — a generic "I changed a control/break
register from X to Y" trace, used here for the `control` ordering write.
`[HIGH/OBSERVED]`

**`INS_FL` @`0x85f4`** logs `S: INS_FL` (`0x81efb`), reads the exec-mode flag
(`l8ui [0x855e0+108]`), and calls into the ordering/flush path (`0x3ac4`,
`0x15344`). It shares the control-register write family with `SET_OM`. The call
graph is `[HIGH/OBSERVED]`; the precise flush semantics are `[MED/INFERRED]`.

> **NOTE — a second `control` R-M-W at run entry.** Independent of `SET_OM`, the
> region @`0x2402` (gated by a flag at DRAM `0x85070`, logging
> `S: NX in HW Decode mode`, `0x80f1e`) also reads-modifies-writes `control`:
> on one branch it sets **bit 20** (`0x2410: slli a4,20; or; 0x241a: s32i`), on
> the other it clears **bit 1** (`0x2440: and a2,-3; 0x2453: s32i`). This is the
> run-entry HW-Decode-mode setup; together with `SET_OM` it is the complete set
> of firmware `control` writes. `[HIGH/OBSERVED]`

---

## 5. Reconciling the firmware-built `0x04004000` against the shipped map

The firmware *builds* the `hw_decode` CSRs at absolute `0x04004000 + off`
(`movi a2,0x400; const16 a2,0x40XX; l32i/s32i`). That absolute is the
**engine-local APB view**: the bundle's own offset is `0x4000` within
`tpb_xt_local_reg`, and the firmware addresses the local register block at a
`0x04000000`-based window.

> **CORRECTION — `0x04004000` is *not* a global flat-map address.** The shipped
> `tpb_xt_local_reg` is instantiated **once per engine**, each at a distinct
> `*_LOCAL_REG` base in the flat address map (e.g.
> `TPB_0_ACT_LOCAL_REG = 0x2802460000`,
> `TPB_0_SP_LOCAL_REG = 0x2802860000`,
> `TPB_0_POOL_LOCAL_REG = 0x2803060000`), each `0x10000` in size. The global
> address of `hw_decode.control` is therefore
> `<engine LOCAL_REG base> + 0x4000` (e.g. `0x2802460000 + 0x4000` for ACT) —
> there is **no single `0x04004000`** in the shipped flat map. The
> reliable, file-grounded fact is the **bundle offset `0x4000`** within
> `tpb_xt_local_reg`; the per-engine absolute is resolved by the
> address-translation windows ([soc-window-manager.md](soc-window-manager.md)).
> `[HIGH/OBSERVED — bundle AddressOffset "0x04000"; engine bases from the flat map]`

Mapping the firmware's debug accesses to the real `tpb_xt_local_reg.hw_decode`
fields (the Layer-B reconciliation):

| firmware access (abs, local view) | reg-file field (bundle `hw_decode` + off) | bit / width | firmware R/W |
| --------------------------------- | ----------------------------------------- | ----------- | ------------ |
| `0x04004000` read/write | `control` (`+0x000`) | `instr_ordering_mode` @ `1`; `iram_ctrl_flush_en` @ `20` | R-M-W |
| `0x04004004` read | `breakpoint_ctrl` (`+0x004`) | `breakpoint_instr_enable@0`, `breakpoint_step_valid@3`, `breakpoint_addr0_valid@4`, `breakpoint_addr1_valid@5`, `ic0_valid@6`, `ic1_valid@7`, `immediate_pause@8` | READ only |
| `0x04004008` read | `breakpoint_step` (`+0x008`) | `num_instr [31:0]` | READ only |
| `0x0400400C` read | `breakpoint_match` (`+0x00C`) | `ic0_mask[7:0]`, `ic0_opcode[15:8]`, `ic1_mask[23:16]`, `ic1_opcode[31:24]` | READ only |
| `0x04004018/1C` read | `breakpoint_addr0_lo/hi` (`+0x018/01C`) | `value [31:0]` | READ only |
| `0x04004020/24` read | `breakpoint_addr1_lo/hi` (`+0x020/024`) | `value [31:0]` | READ only |

The Layer-A reconciliation is the *negative* of §3: none of the firmware's
debug accesses map to any `xtensa_q7.json` OCD/TRAX/PMU field. The Q7 debug
fields (`DSR @0x2010`, `DDR @0x2014`, `DIR0EXEC @0x201C`, `TRIGGERPC @0x14`,
`PCMATCHCTRL @0x18`) are reached only by the external JTAG host.
`[HIGH/OBSERVED]`

---

## 6. The breakpoints + the software-emulated "debug exception"

### 6.1 The breakpoint inventory (the IBREAK/DBREAK analogue)

| kind | count | registers | armed by | surprise raised |
| ---- | ----- | --------- | -------- | --------------- |
| instruction-address | **2** | `addr0` (`0x4018/1C`), `addr1` (`0x4020/24`) | `ctrl.bit4`/`bit5` | `INS_BREAK` (word bit2) |
| opcode-count | **2** | `match` (`0x400c`) + `ic0` (`0x4010`)/`ic1` (`0x4014`) | `ctrl.bit6`/`bit7` | (`-> INS_BREAK`) |
| single-step | 1 | `breakpoint_step` (`0x4008`) | `ctrl.bit3` | `STEP_CNT` (word bit8) |
| data (DBREAK) | **0** | — | — | — |

- **Instruction-address breakpoints (×2)** compare a 64-bit micro-op PC
  (`lo`+`hi`) against the executing ucode PC — directly analogous to Xtensa's
  two `IBREAKA` registers, but at micro-op granularity in the custom block.
  `[HIGH/OBSERVED]`
- **Opcode-count breakpoints (×2)** count instructions whose
  `(opcode & mask) == opcode` until the `breakpoint_ic{0,1}` threshold is hit —
  an opcode-histogram break with no Xtensa analogue. `[HIGH/OBSERVED]`
- **Data breakpoints (DBREAK): absent from the firmware-driven block.** The
  `hw_decode` bundle has no data-address register; a data watchpoint would
  require the Xtensa core's own `DBREAKA`/`DBREAKC` SRs via the external OCD
  (Layer A), which the firmware never touches (§3). `[HIGH/OBSERVED — negative]`

> **GOTCHA — every breakpoint/step register is host-written and
> firmware-read-only.** The single firmware-written `hw_decode` register is
> `control` (the ordering/flush write of §4.3). The host arms breakpoints; the
> firmware only reads them back through the snapshot. `[HIGH/OBSERVED]`

### 6.2 How a debug event is *taken* (the software-emulated EPC/DEBUGCAUSE)

There is **no Xtensa DEBUG-level exception** on the SEQ (§3 SR census:
`debugcause`/`rfdo`/`rfdd`/`intenable`/`epc[2-7]` all zero). The "debug
exception" is, on this engine, a **polled surprise** routed through Setup-Halt.
The take sequence, joining the surprise poll and the run-state FSM:

1. a `hw_decode` condition fires → the HW sets a **surprise word bit**:
   `INS_BREAK` (bit2) from an `addr0/1` or opcode match; `STEP_CNT` (bit8) from
   `breakpoint_step`; `EXT_BREAK` (bit3) from the host writing
   `nx.instr_halt_ctrl` (`0x04000014`, [run-state.md](run-state.md) §1).
2. the per-iteration poll notices it between micro-ops and the surprise handler
   dispatches it ([surprises-irq.md](surprises-irq.md)).
3. **Setup-Halt** (`0x1cf8`, [run-state.md](run-state.md)) saves the resume PC
   into the `lr[3]`/`lr[4]` pair (`0x04001060` lo / `0x04001080` hi), clears the
   running-flag, and stores the **reason byte** (`4`=INS_BREAK, `8`=EXT_BREAK,
   `64`=STEP_CNT-exhausted, `16`=fatal).

So the host's "EPC" and "DEBUGCAUSE" are **emulated in software**:

| Xtensa concept | SEQ software equivalent |
| -------------- | ----------------------- |
| `EPC[DEBUGLEVEL]` (debug PC) | resume PC in `lr[3]`/`lr[4]` (`0x04001060`/`0x04001080`), written by Setup-Halt |
| `DEBUGCAUSE` (why it broke) | the reason byte (`4`/`8`/`64`/`16`) at the running-flag struct |

`[HIGH/OBSERVED on the resume-PC pair and reason codes; the "EPC/DEBUGCAUSE"
naming is the INFERRED analogy — the values are observed, the Xtensa-term
mapping is the deduction]`

> **NOTE — the surprise *drive edge* into the debugger lives in
> [surprises-irq.md](surprises-irq.md).** This page characterizes the join — a
> `hw_decode` firing becomes a surprise-word bit that the polled handler
> dispatches to Setup-Halt — but does not re-derive the surprise poll itself.

### 6.3 Relation to the run-state machine — HALT, not PAUSE

A debug break does **not** publish `run_state = 2` (PAUSED). It takes the HALT
path: `[HIGH/OBSERVED]`

- **PAUSE** (`run_state = 2`) is reached only from the sync handler when the
  remote-status bit is set — a *data-sync* wait, not a debug event
  ([run-state.md](run-state.md) §4a).
- a **debug break** goes through Setup-Halt, which clears the running-flag and
  saves the resume PC + reason but **does not write `run_state`** — that CSR
  retains its prior value (`1`=RUNNING) until a fatal `Entering-HALT` writes the
  halt marker. A non-fatal debug break stops cleanly with a saved PC and no
  `run_state = 2`.

The host therefore distinguishes "paused for sync" (`run_state == 2`,
resume = doorbell) from "stopped at a breakpoint/step" (running-flag cleared +
reason byte + resume PC in `lr[3]`/`lr[4]`, resume = re-arm + doorbell) — two
stop mechanisms over the **same** run-state CSR surface. `[HIGH/OBSERVED]`

**Resume** after a step/break: the host re-arms (rewrites
`breakpoint_step.num_instr` for the next N, or clears the bp) and rings the
`start_ctrl` (`0x04000004`) doorbell; `enter_run` re-runs the snapshot `0x553c`
(re-seeding `0x85650`) and reloads the resume PC. A single-step session is a
loop of `{ StartCtrl → run N → STEP_CNT halt → host reads PC/state → re-arm →
StartCtrl }`. `[HIGH/OBSERVED]`

> **GOTCHA — the *fatal* exception is a different surface.** A bad/illegal
> opcode, OOB, or div0 reaches the fatal raise → Setup-Halt(reason=16) +
> `Entering-HALT` (writes the halt marker into `run_state`) → spin. The debug
> breaks (`4`/`8`/`64`) are **recoverable** (host re-arms + StartCtrl); the
> fatal (`16`) is terminal. See [error-handler.md](error-handler.md).
> `[HIGH/OBSERVED]`

---

## 7. Per-generation invariance

Carving the NX-POOL DEBUG IRAM `.rodata` for all four generations and
byte-searching the snapshot's CSR-read signatures: `[HIGH/OBSERVED]`

| Image | size | model |
| ----- | ---- | ----- |
| SUNDA | 59600 B | same taxonomy, `0x00100000` window, 16-bit step counter |
| CAYMAN | 116768 B | reference (this page) |
| MARIANA | 114816 B | byte-identical snapshot, relocated |
| MARIANA_PLUS | 119616 B | byte-identical snapshot, relocated |

For the three **Cayman-class** gens the snapshot is byte-identical (relocated
only): the breakpoint-CSR read sites and their intra-function deltas from the
`breakpoint_step` read are the same in all three (`control` +`0x10`, `ctrl`
+`0x4b`, `addr0_hi/lo` +`0xc1`/`0xcc`, `addr1_hi/lo` +`0xf9`/`0x104`, `match`
+`0x134`). The register set, the `0x04004000` aperture, the bit-extraction
sequence, and the status-word assembly are 100 % invariant. `[HIGH/OBSERVED]`

**SUNDA** is the same *model* with a different aperture and packaging: it builds
CSR addresses from the `0x00100000` window (not `0x04000000`), uses a 16-bit
step counter at a different DRAM offset, and packages the machinery in a
different translation unit. The debugger **model** — 2 instruction-address
breakpoints + 2 opcode-count breakpoints + the single-step countdown, all
host-armed / firmware-read, delivered as polled surprises, halting via
Setup-Halt with a reason code — is invariant across all four gens. The Xtensa
OCD/ICOUNT/IBREAK negative (§3) holds for all four. `[HIGH/OBSERVED]`

---

## 8. Toolchain boundary — the FLIX wide bundles

The snapshot `0x553c` and the `SET_OM` `0x8684` each contain FLIX wide bundles
the stock `ncore2gp` objdump mis-prints as `.byte` / bogus-`j` / IVP fragments
(e.g. `0x5608`, `0x5613`, `0x5640`, `0x564b` in the snapshot; `0x8725`,
`0x8731` in the OR-helper). These are **bundle bodies, not real ops** — they
fall in the dead-byte gaps *between* the scalar ops. Every CSR address and every
status-word bit-assignment anchored in §2–§6 is read from a **correctly-decoded
scalar instruction** (`const16` CSR addresses, `l32i`/`s32i`, `or`/`and`
bit-sets, `bbci`/`bbsi`). `[HIGH that the anchored scalar ops are correct]`

The uarch register *model* — which CSRs, which bits, the host/firmware R/W
polarity, the single-step chain, the halt path — is fully recovered from the
scalar ops. The only unrecovered detail is the exact micro-op-level field
packing *inside* the wide bundles (e.g. whether the status word is also consumed
by a vector lane), which the shipped objdump cannot expose. See
[flix-decoding.md](../../reference/flix-decoding.md). `[boundary]`

> **NOTE — forward link (Part 13).** Whether profiling / trace / debug access to
> this block is gated (e.g. a production fuse or capability bit disabling the
> single-step debugger or the OCD bus) is the subject of
> [profiling-trace-debug-gating.md](../../control/security/profiling-trace-debug-gating.md).
> The `breakpoint_ctrl` reset values
> (`breakpoint_instr_enable`, `…profile_table_enable`, `ucode_force_pause_enable`
> all reset to `0x1`) suggest the debugger is *enabled at reset*; any gating
> would be an external overlay, deferred to that page.

---

## Cross-references

- [run-state.md](run-state.md) — Setup-Halt `0x1cf8`, the resume-PC pair
  `lr[3]`/`lr[4]`, `run_state`/`start_ctrl`/`instr_halt_ctrl`, the StartCtrl
  handshake feeding the snapshot re-seed.
- [surprises-irq.md](surprises-irq.md) — the polled surprise word
  (`INS_BREAK`/`EXT_BREAK`/`STEP_CNT`), the poll/dispatch, and the `0x85650`
  consume; the *drive edge* into this debugger.
- [error-handler.md](error-handler.md) — the fatal (`reason=16`) exception path,
  distinct from the recoverable debug breaks.
- [soc-window-manager.md](soc-window-manager.md) — the address-translation
  windows that resolve the firmware-built `0x04004000` local view to the
  per-engine `*_LOCAL_REG` physical base.
- [../../control/csr/xtensa-q7.md](../../control/csr/xtensa-q7.md) — the Xtensa
  Q7 OCD/TRAX/PMU debug aperture (Layer A, JTAG-only).
- [../../control/csr/tpb-xt-local-reg.md](../../control/csr/tpb-xt-local-reg.md)
  — the `hw_decode` breakpoint bundle (Layer B, this page's register model).
- [../../control/security/profiling-trace-debug-gating.md](../../control/security/profiling-trace-debug-gating.md)
  — debug/trace access gating. *(forward, Part 13)*
- [../../reference/confidence-model.md](../../reference/confidence-model.md) —
  the confidence × provenance tagging used throughout.
- [../../reference/flix-decoding.md](../../reference/flix-decoding.md) — the
  FLIX/VLIW desync the §8 boundary inherits.
