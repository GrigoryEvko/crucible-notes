# The Interrupt / Exception Handler Bodies

This page decodes the **handler function bodies** behind the GPSIMD Q7 / NeuronCore-sequencer
firmware's two XTOS dispatch tables — the **37-entry `xtos_interrupt_table`** and the
**9-entry `xtos_exc_handler_table`** — directly from the shipped Cayman device firmware. Where
the sibling [XEA3 Interrupt / Exception Architecture](./xea3-interrupt-architecture.md) page
describes the *config-level* exception model (`XCHAL_HAVE_XEA3`, the EXCCAUSE encoding, the
vector layout), this page is about the **actual byte-decoded code** each table entry points
at: the reset/window vectors, the per-EXCCAUSE-nibble exception dispatcher, the custom SEQ
exception handler installed at boot, and the default break/halt/simcall fan-out. Every count
(37, 9) and every handler VA below is re-grounded from `core-isa.h`, the `ncore2gp` params,
and the carved firmware image — never from a decompile.

**Read this in tandem with [`q7-surprises-binding.md`](./q7-surprises-binding.md).** That page
proved — by re-carving both firmware images with `ncore2gp` — that the **leveled-interrupt
machinery is absent** in the shipped images: there is no `rsil`, no `rfi`, no
`INTENABLE`/`INTERRUPT` programming, and no `eps[2-6]`/`epc[2-6]`. The async path is **polled**,
not vectored, and the interrupt enable is armed only via the boot `wer` external-register path.
Consequently the two tables on this page are best understood as the **registry of handler
bodies** (real, byte-decoded code) that the XTOS runtime *links in* — while classic vectored
delivery into `xtos_interrupt_table` is **disabled** in these images. The one path that *does*
fire is the **synchronous exception** vector (`EXCCAUSE`-driven), which routes through
`xtos_exc_handler_table` to the SEQ fault subsystem. This distinction is made explicit at every
step below; this page never asserts that a leveled hardware interrupt fires.

Related:
[The XEA3 Interrupt / Exception Architecture](./xea3-interrupt-architecture.md) ·
[INTC → Q7 Firmware "Surprises" Binding](./q7-surprises-binding.md) ·
[SEQ Error-Handler / Fault Reporting](../../firmware/seq/error-handler.md) ·
[Device→Host Interrupt / Notification Path](./device-host-notification.md) ·
[SDMA Trigger Set](./sdma-triggers.md) ·
[CSR — Xtensa Q7 Debug / OCD](../csr/xtensa-q7.md) ·
[CSR — TPB Xtensa Local Reg](../csr/tpb-xt-local-reg.md).

**Confidence legend** (per the [confidence model](../../reference/confidence-model.md)):
`HIGH/MED/LOW` × `OBSERVED` (read from a shipped artifact — disasm bytes, config header,
RTL-generated param) / `INFERRED` (reasoned) / `CARRIED` (consolidated from a named sibling
page, not re-derived).

---

## 0. Provenance — what was disassembled `[HIGH · OBSERVED]`

Everything in §1–§7 derives from the **shipped device firmware** and the **shipped Cadence
Xtensa toolchain config**:

| Input | Source |
|---|---|
| Firmware archive | `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/.../c10/lib/libnrtucode.a` |
| SEQ (NX) image | members `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` — the sequencer engine that carries the XTOS tables |
| Q7 compute image | members `img_CAYMAN_Q7_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` — the compute slave (own vector shape, §6) |
| Disassembler | `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump` |
| Target config | `XTENSA_SYSTEM=.../XtensaTools/config`, `XTENSA_CORE=ncore2gp` (Vision-Q7 Cayman, Xtensa24, RI-2022.9, `XCHAL_HAVE_XEA3=1`) |
| Interrupt/exc counts | `tools/ncore2gp/xtensa-elf/arch/include/xtensa/config/core-isa.h` + `tools/XtensaTools/config/ncore2gp-params` |
| EXCCAUSE taxonomy | `tools/XtensaTools/xtensa-elf/include/xtensa/corebits.h` |

The `.c.o` members are x86-host ELF wrappers that embed each device image as a `.rodata` byte
array. Carve them with **host** `objcopy -O binary --only-section=.rodata`, then disassemble the
raw image with the Xtensa `objdump` driven `-b binary -m xtensa`:

```sh
objcopy -O binary --only-section=.rodata img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o nx_iram.bin
xtensa-elf-objdump -b binary -m xtensa -D nx_iram.bin   # XTENSA_CORE=ncore2gp in env
```

**Address model.** IRAM file offset **==** device IRAM VA (the reset vector is byte 0). DRAM
lives at VA `0x80000`, so a DRAM datum's *file offset* = VA − `0x80000`; the firmware
materialises a DRAM VA with a `const16 aX,8 ; const16 aX,0xNNNN` pair (high half = `0x0008`).
NX IRAM = **116768 B**, NX DRAM = **28448 B** (both re-measured this session, matching the
sibling pages' anchor).

> **GOTCHA — `extracted/` and `ida/` are gitignored.** `fd`/`rg` skip them by default; use
> `--no-ignore` or absolute paths to re-ground anything here. The IDA v3 sidecars under
> `neuronx-gpsimd/ida/` are also gitignored.

> **QUIRK — the FLIX-desync wall on Vision interiors.** `ncore2gp` is a Vision-Q7 FLIX/VLIW
> core (`IsaMaxInstructionSize = 32`). A linear sweep desyncs across the dense FLIX/IVP bundles
> (the [NCFW core is scalar LX, the Vision interiors are not](./q7-surprises-binding.md)).
> **Every body on this page sits in the scalar vector/XTOS region**, which decodes cleanly with
> stock `-m xtensa`; the handlers do **not** straddle a FLIX span. Where a body tail-calls into
> a FLIX interior (e.g. the logger `call8`), the re-synced boundary is flagged. Do not trust a
> linear-sweep rendering of any code that follows a `call8` into the dense span.

> **NOTE — v5 / Maverick is header-OBSERVED only.** Everything decoded here is the **Cayman**
> image. The 37/9 table *shape* is `[HIGH · OBSERVED]` for Cayman and `[INFERRED-strong]`
> gen-stable (the `ncore2gp` config and the XTOS dispatch shape are frozen across the carved
> generations). Any reading of v5 *interior* firmware behaviour is `[INFERRED]` and flagged.

---

## 1. The two tables — sizes are config-pinned, not grepped `[HIGH · OBSERVED]`

The table sizes are **not** counted from a decompile. They are fixed by the `ncore2gp` build
config and independently confirmed by the runtime table bytes and the dispatcher bounds-checks.

| Table | Size | Config anchor | Firmware anchor |
|---|---:|---|---|
| `xtos_interrupt_table` | **37** entries | `core-isa.h: XCHAL_NUM_INTERRUPTS == 37`; `params: InterruptCount = 37` (and a 37-element `ISSInterruptTypes` array) | not placed (see §2 — no leveled vectors) |
| `xtos_exc_handler_table` | **9** entries | `core-isa.h: XCHAL_EXCCAUSE_NUM == 9` | runtime table @ DRAM VA `0x84e90` (file `0x4e90`), indices `0..8` populated; dispatcher accessor bounds-checks index `≤ 8` |

The XEA3 model in this config is `XCHAL_HAVE_XEA3 = 1`, `XCHAL_HAVE_XEA2 = 0`,
`XCHAL_HAVE_NMI = 0`, `XCHAL_HAVE_VECBASE = 1`, `XCHAL_HAVE_CCOUNT = 1`, `XCHAL_NUM_TIMERS = 3`,
`XCHAL_HAVE_IMPRECISE_EXCEPTIONS = 1`. The **9** in `XCHAL_EXCCAUSE_NUM` is the XEA3 *cause*
count (the `EXCCAUSE[3:0]` nibble has 9 defined values, §4); the **37** in
`XCHAL_NUM_INTERRUPTS` is the full interrupt-line count (§3). `[HIGH · OBSERVED — both numbers
read directly from `core-isa.h`.]`

---

## 2. Why the interrupt table is a registry, not a live vector `[HIGH · OBSERVED]`

The `xtos_interrupt_table` is the XTOS C-level dispatch array the **leveled-interrupt vector**
would index — `_xtos_handler` + `arg` per line. In a normal XTOS image, the Level-1 interrupt
vector reads `INTERRUPT`, finds the highest-priority pending bit, and `callx`'s
`xtos_interrupt_table[n].handler`. **None of that machinery is present in the shipped image:**

| Negative evidence (full mnemonic census, both NX + Q7 images) | Count | Meaning if present |
|---|---:|---|
| `rsil` (set interrupt level) | **0** | mask/unmask leveled IRQs |
| `rfi` (return from interrupt) | **0** | return from a leveled vector |
| `wsr/rsr.intenable` | **0** | enable specific interrupt lines |
| `wsr/rsr.interrupt` / `intset` / `intclear` | **0** | read/clear/post the interrupt latch |
| `eps2`–`eps6` / `epc2`–`epc6` | **0** | per-level saved PS/PC |
| `wsr.ccompare` (arm a timer IRQ) | **0** | fire CCOMPARE0/1/2 (ints 28–30) |
| `wsr.vecbase` | **1** / image | program the vector base (boot only) |

`params: InterruptVectorOffsets = [0×8]` and `Level2..7InterruptVectorOffset = 0` corroborate
from the config side: **no leveled interrupt vector is placed**. The boot path programs
`VECBASE` exactly once (`0xad: wsr.vecbase a2`, `a2 = 0`) and then never touches an interrupt
SR again. `[HIGH · OBSERVED — census re-run on both images this session; the lone `waiti` is
off the FSM run-loop path, CARRIED from the surprises page.]`

> **QUIRK — leveled delivery is *possible* in hardware; the firmware deliberately uses none of
> it.** The `ncore2gp` config genuinely supports leveled interrupts: `XCHAL_HAVE_INTERRUPTS = 1`,
> `XCHAL_NUM_INTLEVELS = 7`, all 37 lines at `intlevel 1`. The absence of any `rsil`/`rfi`/
> `INTENABLE` is therefore **not a config limitation** — it is a deliberate firmware choice to
> run with leveled delivery effectively disabled and reach async events only through the polled
> path and the synchronous-exception vector. That makes the "polled, not vectored" finding a
> substantive one, not an artifact of a cut-down core. `[HIGH · OBSERVED — `XCHAL_NUM_INTLEVELS`
> read from `core-isa.h`; instruction census re-run.]`

> **QUIRK — the timer interrupts are configured but never armed.** Ints 28/29/30 are
> `XTHAL_INTTYPE_TIMER` (CCOMPARE0/1/2) and `ISSTimerInterrupts = [28 29 30]`, but the firmware
> issues **zero** `wsr.ccompare`. It reads `rsr.ccount` directly (e.g. `0x897d`, `0x89bb`) for
> *polled* timestamping. So the timer "interrupt" lines are real config lines with **no
> firmware that fires them** — the timing model is polled CCOUNT, consistent with the
> [polled async model](./q7-surprises-binding.md). `[HIGH · OBSERVED]`

**The consequence for this page.** The 37 interrupt-table entries are documented below from the
config (§3) as the *defined registry* — what each line *means* and what handler *class* it
would dispatch — but the runtime array is not placed and **no entry is reached by a hardware
vector** in these images. The async events those lines represent reach the firmware instead
through the [polled surprises word, the EVT_SEM array, and the `intr_info` latch
read](./q7-surprises-binding.md). The **exception** table (§4), by contrast, *is* live: a
synchronous `EXCCAUSE` fault does take the exception vector.

---

## 3. The 37-entry `xtos_interrupt_table` registry `[HIGH · OBSERVED]`

The 37 lines and their types are byte-exact from `core-isa.h` (`XCHAL_INTnn_TYPE`,
`XCHAL_TIMERn_INTERRUPT`, `XCHAL_BREAKIN/TRAX/PROFILING_INTERRUPT`) and the params'
37-element `ISSInterruptTypes = [0×25, 1×3, 2×3, 3×6]`. All 37 are `intlevel 1`
(`XCHAL_INTnn_LEVEL = 1` for every `n`). The grouping:

| Lines | Count | Xtensa type (`XCHAL_INTnn_TYPE`) | Role of the handler body it would dispatch |
|---:|---:|---|---|
| `0`–`24` | 25 | `XTHAL_INTTYPE_EXTERN_EDGE` | **EXTERN** engine/fabric edge lines — the 25 external IRQ inputs (`XCHAL_NUM_EXTINTERRUPTS = 25`, ext-num 0..24). Each is a SoC-INTC-fed line that *would* dispatch a registered C-handler; in this image they are surfaced via the polled latch / surprises path instead (§2). |
| `25`–`27` | 3 | `XTHAL_INTTYPE_SOFTWARE` | **SOFTWARE** ints — self-posted (`INTSET`), no external wire. No firmware posts them in this image. |
| `28`–`30` | 3 | `XTHAL_INTTYPE_TIMER` | **CCOMPARE0/1/2 TIMER** ints (`XCHAL_TIMER0/1/2_INTERRUPT = 28/29/30`). Never armed (§2 QUIRK); CCOUNT is polled. |
| `31` | 1 | `XTHAL_INTTYPE_DBG_REQUEST` | **OCD external debug-request** (`XCHAL_DBG_REQUEST_INTERRUPT = 31`, `XCHAL_HAVE_DEBUG_EXTERN_INT = 1`). Driven from the JTAG/APB debug surface (see [#914 Xtensa-Q7 OCD](../csr/xtensa-q7.md)), *not* the SoC-INTC async path. |
| `32` | 1 | `XTHAL_INTTYPE_BREAKIN` | **BreakIn** (`XCHAL_BREAKIN_INTERRUPT = 32`) — external debug break. |
| `33` | 1 | `XTHAL_INTTYPE_TRAX` | **TRAX** trace-buffer interrupt (`XCHAL_TRAX_INTERRUPT = 33`). |
| `34` | 1 | `XTHAL_INTTYPE_PROFILING` | **PROFILING** counter-overflow (`XCHAL_PROFILING_INTERRUPT = 34`). |
| `35` | 1 | `XTHAL_INTTYPE_IDMA_DONE` | **on-core iDMA channel-0 done** (`XCHAL_INT35_TYPE`). |
| `36` | 1 | `XTHAL_INTTYPE_IDMA_ERR` | **on-core iDMA channel-0 error** (`XCHAL_INT36_TYPE`). |

`ISSiDMADoneInterrupts = [35]`, `ISSiDMAErrInterrupts = [36]` in the params confirm the iDMA
pair from the config's other side; 25 + 3 + 3 + 1 + 1 + 1 + 1 + 1 + 1 = **37**. `[HIGH ·
OBSERVED — every type read from `core-isa.h`; the breakdown re-grounded against the 37-element
params array, sum verified.]`

> **CORRECTION / DISAMBIGUATION — iDMA ints 35/36 are Q7-CORE lines, NOT SDMA triggers.**
> Lines 35/36 are the **Tensilica on-core iDMA** (`XCHAL_IDMA_CH0_DONE/ERR_INTERRUPT`), a
> *direct, dedicated pair* on the Q7 core's own 37-line vector. They are **not** in the
> 254-source SDMA/uDMA trigger set documented in [`sdma-triggers.md`](./sdma-triggers.md) §5 —
> that set is the descriptor-DMA engine that fans up through an errtrig/apex as an aggregated
> IRQ. Conflating the on-core iDMA's 35/36 with the SDMA trigger table is a known trap; the two
> are different engines. `[HIGH · OBSERVED — CARRIED from #933 sdma-triggers §5, re-confirmed
> against `core-isa.h`.]`

> **NOTE — two encodings of the type field, both self-consistent.** `core-isa.h` /
> `hal-certified.h` use the canonical Xtensa HAL type enum (e.g. `SOFTWARE = 1`,
> `EXTERN_EDGE = 2`, `TIMER = 4`, `IDMA_DONE = 11`, `IDMA_ERR = 12`); the
> `params: ISSInterruptTypes` array uses a compressed simulator encoding
> (`0 = ext, 1 = sw, 2 = timer, 3 = ISS-internal`). They agree on every line index — the
> grouping above is the same under both. The "37 entries" figure refers to the params array
> length (and `XCHAL_NUM_INTERRUPTS`). `[HIGH · OBSERVED]`

> **NOTE — there is no NMI line.** `XCHAL_HAVE_NMI = 0`. The "NMI" climb on the SoC side
> (errtrig `nmi_out`) terminates in the *management* core's fabric, not in a Q7-core NMI vector.
> `[HIGH · OBSERVED]`

---

## 4. The 9-entry `xtos_exc_handler_table` — the live path `[HIGH · OBSERVED]`

Unlike the interrupt table, the **synchronous exception** path is real: an `EXCCAUSE`-classed
fault (illegal instruction, address/load-store error, etc.) takes the exception vector, which
indexes the 9-entry `xtos_exc_handler_table`. This is the **default break/halt fan-out** the
SEQ fault subsystem hooks into.

### 4a. The XEA3 EXCCAUSE taxonomy `[HIGH · OBSERVED]`

`EXCCAUSE` in XEA3 is a structured word (`corebits.h`): `[3:0]` = **CAUSE** (the 9-value nibble
the dispatcher indexes), `[7:4]` = TYPE, `[11:8]` = SUBTYPE, `[13:12]` = LSFO, `[15:14]` =
IMPR. The 9 CAUSE values (`XCHAL_EXCCAUSE_NUM = 9`):

| `EXCCAUSE[3:0]` | `corebits.h` name | Meaning |
|---:|---|---|
| `0` | `EXCCAUSE_NONE` | no exception |
| `1` | `EXCCAUSE_INSTRUCTION` | instruction usage (illegal / unsupported opcode) |
| `2` | `EXCCAUSE_ADDRESS` | addressing usage (load/store, alignment) |
| `3` | `EXCCAUSE_EXTERNAL` | external causes (bus / fetch error) |
| `4` | `EXCCAUSE_DEBUG` | debug exception |
| `5` | `EXCCAUSE_SYSCALL` | syscall |
| `6` | `EXCCAUSE_HARDWARE` | hardware failure |
| `7` | `EXCCAUSE_MEMORY` | memory management |
| `8` | `EXCCAUSE_CP_DISABLED` | coprocessor disabled |

`[HIGH · OBSERVED — the 9 names read from `corebits.h` lines 61–69; the field layout from
lines 40–57.]`

### 4b. The exception vector → dispatcher chain `[HIGH · OBSERVED]`

A synchronous exception enters the vector at IRAM `0x6c`, which saves `EXCVADDR` and a couple
of registers onto the frame and jumps to the dispatcher entry at `0x1b0` → `0x1ec`:

```text
0000006c <exc-vector-body>:
    6c:  20ee03      rsr.excvaddr a2          ; a2 = faulting VA
    6f:  2971        s32i.n a2, a1, 28        ; frame[+28] = EXCVADDR
    71:  3961        s32i.n a3, a1, 24        ; frame[+24] = saved a3 (exc context)
    73:  464e00      j      0x1b0             ; -> dispatcher entry
   ...
   1b0:  060e00      j      0x1ec             ; -> the EXCCAUSE dispatcher
```

The dispatcher at `0x1ec` indexes the 9-entry table by the **EXCCAUSE nibble** and falls back
to the default handler `0x1c2ac` when the table slot is zero:

```c
// exc-dispatcher @0x1ec  (instruction-exact; addresses inline)
exc_handler_t *tab  = (void*)0x84e90;        // 0x1ec: const16 a4,8 ; a4,0x4e90
exc_handler_t  dflt = (void*)0x1c2ac;        // 0x1f2: const16 a2,1 ; a2,0xc2ac  (= 0x1c2ac)
if (tab == 0) goto use_default;              // 0x1f8: beqz a4,0x204
int n = exccause & 0xf;                      // 0x1fb: extui a3,a3,0,4   *** the NIBBLE ***
exc_handler_t h = tab[n];                    // 0x1fe: addx4 a4,a3,a4 ; 0x201: l32i a4,[a4]
h = (h ? h : dflt);                          // 0x204: moveqz a4,a2,a4  (slot 0 -> default)
a2 = frame_ptr;                              // 0x207: mov.n a2,a1
goto *h;                                     // 0x209: jx a4
```

`extui a3,a3,0,4` is the literal `EXCCAUSE & 0xf` — the dispatcher keys on the CAUSE nibble of
§4a, so the table is exactly **9 wide** (causes 0..8). `moveqz a4,a2,a4` installs the default
`0x1c2ac` for any slot that is still zero. `[HIGH · OBSERVED — every instruction decoded;
the `0x84e90` and `0x1c2ac` literals confirmed by a `const16`-immediate scan.]`

### 4c. The table bytes and the accessor's bounds-check `[HIGH · OBSERVED]`

The runtime table at DRAM `0x84e90` (file `0x4e90`), 16 words dumped, with indices `0..8`
populated to the default and `≥9` not part of the table:

```text
exc_table[0..8] = 0x0001c2ac  (×9)   ; the default handler, every CAUSE
exc_table[9]    = 0x00000000         ; past the 9-entry table
...
```

The install/lookup accessor at `0x1c274` (source `exception_handler.hpp`) bounds-checks the
index against **8** before touching the table — proving the **9-entry** size from the code
side, not just the data:

```c
// _xtos_set_exception_handler-style accessor @0x1c274
int  i = a2;                                  // 0x1c277: mov.n a5,a2 (cause index)
void *ret = (void*)-1;                         // 0x1c27b: movi.n a2,-1
if (8u < (unsigned)i) return ret;              // 0x1c27d: bltu a6(=8),a5,0x1c2a2  *** i>8 -> reject
exc_handler_t *slot = &((void**)0x84e90)[i];   // 0x1c280: const16 a2,8;a2,0x4e90 ; addx4 a15,a5,a2
void *old = *slot;                             // 0x1c28f: l32i.n a5,[a15]
void *nw  = (a3 ? a3 : (void*)0x1c2ac);        // 0x1c291: moveqz a3,0xc2ac-base,a3 (0 -> default)
*slot = nw;                                     // 0x1c296: s32i.n a3,[a15]
if (out_old) *out_old = (old==dflt ? 0 : old);  // 0x1c29a: report previous (default reads back 0)
return 0;
```

The `movi.n a6,8` + `bltu a6,a5` guard is the canonical "index must be `≤ 8`" check — a
**9-entry** table indexed `0..8`. `[HIGH · OBSERVED — disassembled this session.]`

### 4d. The boot install — custom SEQ handler for causes 1 / 3 / 4 `[HIGH · OBSERVED]`

At boot, `register_exception_handlers @0x26ac` calls the accessor **three** times, overriding
the default with a **custom SEQ exception handler at `0x1a64`** for exactly three causes, and
asserting `exception_handler.hpp:82/84/86 !ret_val` if any install fails:

```c
// register_exception_handlers @0x26ac
install(/*cause=*/1, /*handler=*/0x1a64);    // 0x26bb: call8 0x1c274 ; assert hpp:82 on fail
install(/*cause=*/3, /*handler=*/0x1a64);    // 0x26df: call8 0x1c274 ; assert hpp:84 on fail
install(/*cause=*/4, /*handler=*/0x1a64);    // 0x2703: call8 0x1c274 ; assert hpp:86 on fail
```

So the **live** exception map is:

| `EXCCAUSE[3:0]` | Installed handler | Body |
|---:|---|---|
| `1` `EXCCAUSE_INSTRUCTION` | `0x1a64` (custom SEQ) | → early FATAL emitter (§4e) |
| `3` `EXCCAUSE_EXTERNAL` | `0x1a64` (custom SEQ) | → early FATAL emitter (§4e) |
| `4` `EXCCAUSE_DEBUG` | `0x1a64` (custom SEQ) | → early FATAL emitter (§4e) |
| `0,2,5,6,7,8` | `0x1c2ac` (XTOS default) | break / save / `simcall` (§4f) |

The handler address literal is `const16 a11,0 ; a11,0x1a64` → VA `0x1a64` (note: built without
the `0x0008` DRAM high-half — it is an **IRAM** code address, not DRAM). `[HIGH · OBSERVED]`

> **QUIRK — only three of the nine causes are firmware-handled.** The sequencer overrides
> CAUSE = 1 (instruction usage), 3 (external/bus), 4 (debug) — the three a running sequencer can
> realistically take — and leaves the other six on the XTOS default. CAUSE = 5 (syscall) and
> CAUSE = 6/7/8 (hardware/memory/coprocessor) are not separately handled because the engine
> issues no `syscall`, has no MMU paging surface it would fault on, and disables nothing it then
> uses. `[HIGH · OBSERVED for the three installs; INFERRED for the why.]`

### 4e. The custom SEQ handler `0x1a64` → the early FATAL emitter `0x1a80` `[HIGH · OBSERVED]`

`0x1a64` extracts the **12-bit EXCCAUSE FULLTYPE** field from the saved exception context and
tail-calls the early FATAL emitter, which raises a `notification_t` with code `'B'` (`0x42`)
and spins forever:

```c
// SEQ exception handler @0x1a64
void seq_exc_handler(frame_t *f) {            // 0x1a64: entry a1,48
    uint32_t cause_ctx = f->saved[24];        // 0x1a6f: l32i.n a2,[a2+24]
    uint32_t fulltype  = cause_ctx & 0xfff;   // 0x1a71: extui a2,a2,0,12  *** EXCCAUSE FULLTYPE
    emit_early_fatal(fulltype);               // 0x1a7b: call8 0x1a80
}

// early FATAL emitter @0x1a80  (= the "code 'B'" pre-TU emitter named in error-handler §1)
void emit_early_fatal(uint32_t aux) {         // 0x1a80: entry a1,48
    uint8_t code = 'B';                       // 0x1a85: movi.n a2,66
    int block_id = get_block_id();            // 0x1a8a: call8 0x13d90   (which engine faulted)
    notification_t rec = pack(block_id, code, aux);  // same packer shape as build_error_record
    raise_FATAL(rec);                         // 0x1ac4: call8 0x13e00 -> Halt -> SPIN forever
}
```

This is the **direct tie to the SEQ fault subsystem**: `0x1a80`'s `call8 0x13e00` is the
*FATAL raise wrapper (severity 2)* documented on [`error-handler.md`](../../firmware/seq/error-handler.md)
§5d — it calls `raise_error(2, record)`, then the halt-dispatch `0xa2e0`, then spins at
`j 0x13e14`. The `get_block_id` call (`0x13d90`) and the packing are byte-identical to that
page's `build_error_record`. `[HIGH · OBSERVED — `0x1a80` is the same `code 'B'` early emitter
named in error-handler §1; the `0x13e00`/`0x13d90` targets match.]`

### 4f. The XTOS default handler `0x1c2ac` and break handler `0x1c2a4` `[HIGH · OBSERVED]`

For the six causes left on the default, the table points at `0x1c2ac`, the XTOS default
exception handler. It re-publishes `EXCCAUSE`/`EXCVADDR`, stages a full register-save area at
DRAM `0x84ff8`, and **the continuation block reaches a `simcall 0`** (the XTOS "report to the
host/simulator" path; on real silicon this is the trap-to-debugger / halt door):

```c
// XTOS default exception handler @0x1c2ac  (two blocks; the first retw.n's into the second)
void xtos_default_exc(frame_t *f) {
    WSR_EXCCAUSE(f->exccause);                 // 0x1c2ac: l32i a0,[a2+24] ; wsr.exccause a0
    WSR_EXCVADDR(f->excvaddr);                 // 0x1c2b1: l32i a0,[a2+28] ; wsr.excvaddr a0
    save_area_t *sa = (void*)0x84ff8;          // 0x1c2b6: const16 a1,8 ; a1,0x4ff8
    sa->a0 = f->saved_a0; sa->a1 = f->saved_a1; sa->ps = f->ps;   // 0x1c2bc..0x1c2c8
    sa->frame = f;                             // 0x1c2c8: s32i.n a2,[a1+8]
    /* 0x1c2d0: retw.n with return-addr a0 = 0x1c2d2 -> the continuation block: */
    /* 0x1c2d2..0x1c2ee: reload a8..a15 from the save area @0x84ff8 */
    a2 = -8;                                   // 0x1c2f0: movi.n a2,-8
    simcall();                                 // 0x1c2f2: simcall 0   *** report / halt door
    /* 0x1c2f5: ill (unreachable) */
}
```

The sibling **break** handler at `0x1c2a4` is the minimal break-to-debugger stub (it is the
default C-handler for the per-cause array of §4g):

```text
0001c2a4 <xtos_break_handler>:
    1c2a4:  364100      entry  a1, 32
    1c2a7:  f04100      break  1, 15        ; raise a debug break (BREAK 1,15)
    1c2aa:  1df0        retw.n
```

`[HIGH · OBSERVED]`

### 4g. The per-cause C-handler array `0x84ec0` `[HIGH · OBSERVED]`

Alongside the 9-entry primary table, the image carries the XTOS **per-cause C-handler array**
at DRAM `0x84ec0` — `{handler, arg}` pairs, accessed by a second accessor at `0x1c240`
(`addx8` = 8-byte stride). All **39** pairs default to `{handler = 0x1c2a4 (break), arg = k}`
for `k = 0..38` — the wider XEA2-compatible cause range that XTOS keeps for the legacy
`_xtos_c_handler_table` interface, every slot defaulting to the break stub:

```text
exc_c_table[k] = { 0x0001c2a4, k }   for k = 0..38   (39 pairs, all break-stub)
```

The boot path also `wsr.isb`'s this base (`0xa4: const16 a4,0x4ec0 ; wsr.isb a4`) — the
instruction-breakpoint scratch wiring of the debug surface. This array is the **per-cause**
companion to the **per-nibble** primary table of §4b: the primary 9-entry table is what the
live exception vector indexes; the 39-pair array is the legacy C-handler registry, default
break. `[HIGH · OBSERVED — 39 `{0x1c2a4, k}` pairs decoded; accessor `0x1c240` uses an 8-byte
stride.]`

---

## 5. Reconciliation with the SEQ ErrorHandler and the FP-only policy `[HIGH · CARRIED + OBSERVED]`

The exception fan-out of §4 is the **HW-vector entry** to the same fault subsystem the
[SEQ Error-Handler page](../../firmware/seq/error-handler.md) reconstructs from the *firmware*
side. The two meet exactly:

```
HW synchronous exception (EXCCAUSE classed)
  -> exc vector 0x6c -> dispatcher 0x1ec -> exc_table[EXCCAUSE & 0xf]
       -> cause 1/3/4 : custom SEQ handler 0x1a64 -> early emitter 0x1a80 (code 'B')
                         -> raise_FATAL 0x13e00 -> Halt 0xa2e0 -> SPIN          [FATAL]
       -> cause 0/2/5/6/7/8 : XTOS default 0x1c2ac -> simcall / break door      [FATAL/halt]
```

That subsystem has **six named fault entry points** — `HandleBadOpcode @0x13f58`,
`HandleIllegalInstr @0x13f80`, `HandleIntDivZero @0x13f34`, `HandleFPError @0x13eb0`,
`signal_handler @0x14014`, `assert_fail @0xa304` — and a **binary recoverable-vs-fatal policy**
in which **exactly one (FP arithmetic) is recoverable** and the other five spin forever. The
exception-vector path of §4 lands in the *FATAL* class (it reaches `0x13e00`, the
severity-2 wrapper). The **only** recoverable fault is the FP one, which never reaches an
exception vector at all — it is taken via the FCR-trapped FP status read inside `HandleFPError`
(`raise_RECOVERABLE @0x13e30`, severity 1, *returns*). `[FP-only-recoverable policy CARRIED ·
#672 error-handler §1/§5d; the exception-vector → `0x13e00` FATAL coupling OBSERVED here.]`

The `signal_handler` couples the device's HW-exception model to a POSIX-signal abstraction:
`register_signal_handlers @0x13fa8` installs `0x14014` for the six signals
`{6, 2, 4, 8, 0xb, 0xf}` = `{SIGABRT, SIGINT, SIGILL, SIGFPE, SIGSEGV, SIGTERM}`, and every
delivered signal hard-faults with code `'A'`. `[CARRIED · #672 §4e]`

> **NOTE — two FATAL emitters, two codes, by design.** A fault that arrives through the
> **exception vector** (§4e) raises code **`'B'`** via the early emitter `0x1a80`; a fault that
> arrives through the **signal** abstraction (§5, `signal_handler`) raises code **`'A'`**; a
> *decoded* fault (bad opcode, div0) raises its class byte (`0`, `2`). All four build the same
> `notification_t` shape and converge on `raise_FATAL 0x13e00`. The code byte is what tells a
> host runtime *which door* the fault came through. `[HIGH · OBSERVED for `'B'`@`0x1a80`;
> CARRIED for `'A'`/`0`/`2`.]`

---

## 6. The Q7 compute image — same shape, table elsewhere `[MED · OBSERVED]`

The **NX/SEQ** image is the authoritative carrier of the XTOS tables (it runs the sequencer and
the [surprises subsystem](./q7-surprises-binding.md)). The **Q7 compute** image
(`img_CAYMAN_Q7_POOL_DEBUG_*`) has the **identical boot shape** — one `wsr.vecbase` at `0xad`,
`a2 = 0` — but its vector bodies and exception table live at different offsets (the `q7_dram`
region at file `0x4e90`/`0x4ec0` is **string data**, not the SEQ tables). The Q7 image is a
compute *slave* fed by the SEQ via EVT_SEM / `run_state` and carries no surprises subsystem of
its own; its exception handling exists but is a separate decode not pinned on this page.
`[MED · OBSERVED — Q7 boot shape decoded; its table offsets differ and are not re-decoded
here.]`

> **GOTCHA — do not merge the two "Q7"s.** The *compute* NeuronCore Q7 (this image) and the
> SoC-survival *management* "Pacific" core are different cores; the management core's ISR bodies
> do **not** ship in this package, and its apex-pending-bit → vector map remains firmware/HW-owned
> (see [surprises §6](./q7-surprises-binding.md)). This page decodes the **compute/SEQ** side
> only. `[CARRIED · #941 §6]`

---

## 7. Function & table map `[HIGH · OBSERVED]`

| Address / table | Identity | Role |
|---|---|---|
| IRAM `0x1dc` | `_start` reset entry | `const16 a0,144 ; jx a0` → boot body `0x90` |
| IRAM `0x90`/`0xad` | boot body / `wsr.vecbase` | the **single** `wsr.vecbase a2` (a2 = 0); then I-cache invalidate, `MEMCTL`, `WindowBase` |
| IRAM `0x1e8` | halt vector | `halt 0` |
| IRAM `0x6c` | exception-vector body | `rsr.excvaddr` save; `j 0x1b0` → `j 0x1ec` |
| IRAM `0x1ec` | EXCCAUSE dispatcher | indexes `exc_table[EXCCAUSE & 0xf]` @ `0x84e90`; default `0x1c2ac` |
| IRAM `0x1c274` | exc-table accessor | install/lookup; bounds-check index `≤ 8` (9-entry) |
| IRAM `0x1c240` | per-cause C-handler accessor | 8-byte-stride over `0x84ec0` |
| IRAM `0x26ac` | `register_exception_handlers` | installs `0x1a64` for causes 1/3/4 (boot) |
| IRAM `0x1a64` | custom SEQ exception handler | extracts EXCCAUSE FULLTYPE; `call8 0x1a80` |
| IRAM `0x1a80` | early FATAL emitter | code `'B'`; `get_block_id`; `raise_FATAL 0x13e00` → SPIN |
| IRAM `0x1c2ac` | XTOS default exception handler | publish `EXCCAUSE`/`EXCVADDR`; save area `0x84ff8`; `simcall 0` |
| IRAM `0x1c2a4` | XTOS break handler | `entry ; break 1,15 ; retw.n` |
| IRAM `0x13e00`/`0x13e30` | SEQ FATAL / RECOVERABLE raise wrappers | sev 2 (spin) / sev 1 (return) — [#672](../../firmware/seq/error-handler.md) |
| DRAM `0x84e90` | `xtos_exc_handler_table` | **9** entries (CAUSE 0..8); 0..8 → `0x1c2ac` (default), 1/3/4 overridden → `0x1a64` |
| DRAM `0x84ec0` | per-cause C-handler array | **39** `{0x1c2a4, k}` pairs (break-stub default) |
| DRAM `0x84ff8` | exception save area | register/PS/EXCCAUSE/EXCVADDR snapshot for the default handler |
| *(config)* | `xtos_interrupt_table` | **37** lines (§3); registry only — **not placed** (no leveled vectors, §2) |

---

## 8. Reimplementation notes

A from-scratch reimplementation of this engine's trap surface must reproduce a **synchronous-
exception-only** model with a **registry-but-no-vector** interrupt table:

1. **Program `VECBASE` once; ship exception + windowed-ABI vectors only.** No leveled interrupt
   vectors. Do **not** emit `rsil`/`rfi`/`INTENABLE`; do **not** arm `CCOMPARE`. The 37-line
   interrupt registry is config metadata, not a live dispatch path — even though the core
   *supports* 7 interrupt levels, the firmware uses none (§2).
2. **A 9-wide exception dispatcher keyed on `EXCCAUSE & 0xf`.** Index a 9-entry handler table by
   the XEA3 CAUSE nibble; `moveqz` to a default handler for empty slots (§4b). Bounds-check the
   install accessor at `≤ 8` (§4c).
3. **Override three causes at boot.** Install a single custom handler for CAUSE = 1
   (instruction), 3 (external), 4 (debug); leave 0/2/5/6/7/8 on the XTOS default (§4d). The
   custom handler extracts the 12-bit EXCCAUSE FULLTYPE and routes to a FATAL raise that emits a
   `notification_t` (code `'B'`) and spins (§4e).
4. **One recoverable fault.** Only the FP-arithmetic fault returns; every exception-vector path
   is FATAL and lands on the severity-2 raise → halt → infinite spin (§5).
5. **Surface async events by polling, not vectoring.** The 25 EXTERN lines / SW / iDMA "interrupt"
   semantics are delivered via the polled `intr_info` latch + the surprises word + EVT_SEM — see
   [`q7-surprises-binding.md`](./q7-surprises-binding.md).

The pieces a reimplementation **cannot** reproduce from this page alone are the HW-side encoding
of a SoC-INTC source into the polled latch (firmware/HW-owned, see surprises §6/O2) and the
*management*-core ISR bodies (a different core, not shipped). Those are honestly flagged
boundaries, not gaps this page papers over.
