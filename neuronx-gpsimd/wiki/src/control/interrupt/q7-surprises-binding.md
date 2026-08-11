# INTC → Q7 Firmware "Surprises" Binding

This page documents the **terminal hop** of the Cayman interrupt cascade: how a
hardware interrupt originating in the SoC INTC fabric actually reaches the **GPSIMD
Q7 / NeuronCore-sequencer firmware**, and what that firmware does when it arrives.
Every sibling page in this lane traces the SoC fault *down* to the `peb_intc` apex and
leaves the final apex-pending-bit → core-vector hop flagged firmware/HW-owned. This
page closes that gap **from the firmware side** — by disassembling the shipped Q7 /
NeuronCore-sequencer firmware and reconciling it against the shipped Q7 interrupt CSRs
— and where the binding cannot be proven from a shipped artifact, it says so.

The headline result is structural and, for a reader expecting a classic embedded ISR,
counter-intuitive: **the GPSIMD sequencer firmware is a *polled* async-event engine. It
takes no leveled Xtensa hardware interrupt.** A SoC interrupt destined for this engine
terminates in a custom per-core *metadata latch* CSR (`intr_info`), which the firmware
**reads at a dispatch boundary** — not in a vector. Fine-grained async control (break,
single-step, ordering change, pause) is surfaced through a firmware-owned **"surprises"
word** that the main FSM **polls between every instruction dispatch**. Understanding
this page means understanding why there is no `rfi` anywhere in the image.

**Scope.** Two distinct cores are named "Q7" in this lane and they must not be merged.
This page is about the **GPSIMD *compute* NeuronCore Q7** that runs the sequencer
firmware decoded here. The SoC-survival apex sink — the *management* "Pacific" core —
is a separate core whose ISR bodies do not ship in this package; its apex→vector map
remains the firmware/HW-owned gap (see §6). This page does **not** close that gap; it
closes the *different*, compute-Q7 side.

Related:
[PEB Apex / CC / TOP_SP sources](./peb-cc-topsp-triggers.md) ·
[Physical INTC instances](./physical-intc-instances.md) ·
[Trigger-YAML Schema Atlas + Source Map](./schema-atlas.md) ·
[errtrig / FIS routing](./errtrig-fis-routing.md) ·
[NSM isolation flow (unified)](./nsm-flow-unified.md) ·
[The XEA3 Interrupt / Exception Architecture](./xea3-interrupt-architecture.md) ·
[The Interrupt / Exception Handler Bodies](./handler-bodies.md) ·
[CSR — Xtensa Q7 Debug / OCD](../csr/xtensa-q7.md) ·
[CSR — TPB Xtensa Local Reg](../csr/tpb-xt-local-reg.md).

**Confidence legend.** The page default is `HIGH · OBSERVED`; claims that depart from
it carry an explicit tag. `HIGH` = byte-exact from the disasm/schema.
`MED` = strong inference from naming + cross-file. `LOW` = plausible, flagged.
`OBSERVED` = read from a shipped artifact (disasm bytes / CSR schema). `INFERRED` =
reasoned. `CARRIED` = consolidated from a named sibling page, not re-derived here.

---

## 0. Provenance — what was disassembled

Everything in §1–§5 derives from disassembling the **shipped device firmware** with the
**shipped Cadence Xtensa toolchain**:

| Input | Source |
|---|---|
| Firmware archive | `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/.../c10/lib/libnrtucode.a` |
| SEQ (NX) image | members `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` — the sequencer engine |
| Q7 compute image | members `img_CAYMAN_Q7_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` — the compute slave |
| Disassembler | `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump` |
| Target config | `XTENSA_SYSTEM=.../XtensaTools/config`, `XTENSA_CORE=ncore2gp` (Vision-Q7 Cayman, Xtensa24, RI-2022.9) |
| Interrupt CSRs | `cayman-arch-regs_tgz/csrs/tpb/tpb_xt_local_reg.json` (RTL-generated schema, dated 2022-12-14) |

The `.rodata` array of each `.c.o` is the raw image; carve it with
`objcopy -O binary --only-section=.rodata`. Address model: **IRAM offset == device IRAM
VA** (reset at byte 0); **DRAM string offset == device DRAM VA − 0x80000** (the DRAM
image lives at VA `0x80000`); a `const16 aX,8 ; const16 aX,0xNNNN` pair builds DRAM VA
`0x8NNNN`. NX IRAM ≈ 116768 B, NX DRAM ≈ 28448 B.

> **GOTCHA — `extracted/` and `ida/` are gitignored.** `fd`/`rg` skip them by default;
> use `--no-ignore` or absolute paths to re-ground anything here. The IDA v3 sidecars
> under `neuronx-gpsimd/ida/` are also gitignored.

> **QUIRK — the NCFW management core is scalar Xtensa-LX, not FLIX.** If you ever touch
> the *management* image, `ncore2gp` mis-decodes its scalar `op0=e/f` bytes as Vision
> FLIX bundles. The SEQ/compute images decoded here *are* `ncore2gp` Vision-Q7, so
> `ncore2gp` is correct for them — except inside the dense FLIX/IVP spans, where stock
> objdump loses bundle sync (flagged at every such point below).

---

## 1. The determination — POLLED, not interrupt-DRIVEN

The decisive test is **negative evidence at the ISA level**. A core that takes leveled
Xtensa hardware interrupts *must* contain `rsil` (raise/lower interrupt level to mask)
and `rfi` (return-from-interrupt); it must program `INTENABLE`. A full mnemonic census
of both shipped images finds **none of them**:

| Instruction class | NX SEQ (`nx_iram`) | Q7 compute (`q7_iram`) | Meaning if present |
|---|---:|---:|---|
| `rsil` (set interrupt level) | **0** | **0** | mask/unmask leveled IRQs |
| `rfi` (return from interrupt) | **0** | **0** | return from a leveled vector |
| `wsr/rsr.intenable` | **0** | **0** | enable specific interrupt lines |
| `wsr/rsr.interrupt` | **0** | **0** | read/clear the interrupt latch |
| `eps2`–`eps6` / `epc2`–`epc6` | **0** | **0** | per-level saved PS/PC (legacy XEA2-leveled SRs; XEA3 omits them) |
| `waiti` (wait-for-interrupt) | **1** (off-path) | **3** (off-path) | sleep until IRQ |
| `wsr.vecbase` | **1** (boot) | **1** (boot) | program vector base once |

The total absence of `rsil`/`rfi`/`intenable` in **both** images is the strongest single
proof: the on-die Xtensa **never enters, and never returns from, a leveled interrupt
vector**. This is the firmware-side correlate of the architectural finding that the
`ncore2gp` sysreg table carries **no** `INTERRUPT`/`INTENABLE`/`EPS[2-6]`/`EPC[2-6]` SRs
— only the single-dispatch **XEA3** *exception* model (`EPC1`/`EXCCAUSE`/`EXCVADDR`/`VECBASE`
+ `MS`/`IEVEC`/`ISB`/`ISL`/`KSL`) plus the `ActiveInterrupt`/`ActivePriority`/`CurrentPriority`
arbitration **state** that the config exposes without leveled delivery.
`[CARRIED · XEA3 arch §1–§2; firmware census OBSERVED here]`

> **CORRECTION — this core is XEA3, not "single-level XEA2".** Earlier wording on
> this page called the model a "single-level **XEA2** exception model" and the
> `nx_iram` head a "standard `ncore2gp` **XEA2** vector region." That is the wrong
> architecture name. The `ncore2gp` Vision-Q7 core is **XEA3** (Exception
> Architecture 3) — proven in the `libisa-core.so` register file: the
> XEA3-distinctive SRs `MS` (`0xe5`), `IEVEC` (`0x74`), `ISB`, `ISL`, `KSL` are all
> present (the `xt_exception_dispatch` package), while *every* XEA2-only SR
> (`EPC2-7`/`EPS2-7`/`INTENABLE`/`INTERRUPT`/`rsil`/`rfi`) is absent from both the
> roster and both shipped firmware images. "No leveled interrupt registers" is the
> defining **signature of XEA3** (which removes XEA2's level ladder), *not* a
> stripped-down XEA2. The `(XEA2 leveled)` label on the absent `eps2-6`/`epc2-6`
> rows below is correct in the narrow sense that those SRs *belong to* the legacy
> XEA2 leveled model — it is precisely why XEA3 omits them. The polled-not-vectored
> firmware conclusion on every page is **unchanged**; only the architecture name is
> corrected. See [`xea3-interrupt-architecture.md`](./xea3-interrupt-architecture.md)
> §1 for the byte-grounded register-file proof.

The lone `waiti` per image sits in an idle/quiesce primitive inside a FLIX-desynced
fragment, **not** on the FSM run-loop path — it is not a run-loop interrupt-wait.

> **NOTE — three async surfaces, all polled or read, never vectored.** The firmware
> surfaces async events exactly three ways:
> 1. the **"surprises" word** — polled at FSM step 1 every iteration (§3);
> 2. the **EVT_SEM** hardware semaphore/event array — polled via `PollSem` /
>    `wait_ge_and_dec` (§4);
> 3. the custom **`intr_info`** CSR — *read once* at the dispatcher boundary (§4), a
>    metadata latch, not a vector entry.

### 1a. The vector table is exception + window only

The head of `nx_iram` is the standard `ncore2gp` XEA3 vector region: a reset jump, the
windowed-ABI overflow/underflow handlers (`l32e`/`s32e` runs), and an `EXCVADDR`-save
exception vector. There is **no dense block of leveled interrupt vectors** (no
Level-2..6 EXCVEC). `VECBASE` is programmed exactly **once** at boot (single
`wsr.vecbase` site in the whole image), inside `_start`, after I-cache invalidate and
before `MEMCTL`/`WindowBase`/MPU. The Q7 compute image has the *identical* single
`wsr.vecbase` and the same exception+window vector shape. Having no leveled interrupt SRs
(§1), the config consequently has no leveled interrupt vectors.

> **CORRECTION — the Q7 *debug* "vectors" are a different interface.** The
> `xtensa_q7` OCD aperture (`@0x4000`, see [#914](../csr/xtensa-q7.md)) exposes
> `DSR.DebugPend*`/`DebugInt*` "vectors" — but those are the **external** JTAG/APB
> debug-interrupt surface (host break / TRAX PTO / BreakIn), driven from *outside* the
> core by a debugger. They are **not** the SoC-INTC async path and must not be confused
> with it. The firmware's own `INS_BREAK`/`EXT_BREAK`/`STEP_CNT` surprises (§3) are the
> *in-core* reflection of breakpoint/step conditions armed via the hw-decode breakpoint
> CSRs. `[HIGH · CARRIED #914 + observed §3]`

---

## 2. The on-die Q7 IRQ CSR surface

A SoC interrupt destined for this engine does **not** land on an Xtensa SR. It lands on a
custom per-core CSR bundle in the TPB local-register aperture
(`tpb_xt_local_reg.json`). The address-decode for the relevant registers, re-grounded
from the JSON (block `nx` @ base `0x00000`, block `q7` @ base `0x03000`; each
register's `AddressOffset` added to its block base):

| Absolute | Register | Field | Verbatim Description |
|---:|---|---|---|
| `0x0008` | `nx.run_state` | `state` | run-state of the NX core |
| `0x0014` | `nx.instr_halt_ctrl` | `halt_req` [0] | "Request to go to ISA HALT state" |
| `0x0018` | `nx.intr_ctrl` | `en` [3:0] | **"Interrupt enable"** — 4 sources for the NX core |
| `0x001C` | `nx.intr_info` | `metadata` [31:0] | **"Interrupt metadata"** |
| `0x3028` | `q7.intr_ctrl` | `en` [31:0] | **"Interrupt enable, 4 bits per Q7 core"** (8×4 = 32) |
| `0x302C`–`0x3048` | `q7.intr_info_0`..`_7` | `metadata` [31:0] | per-Q7-core "Interrupt metadata" |

The structure is the load-path for this section: **exactly 4 interrupt sources per
core**, gated by `intr_ctrl`, with one metadata word per core. This custom Annapurna
per-core interrupt block is where the SoC INTC delivery terminates for this engine —
distinct from both the Xtensa SRs (none — §1) and the EVT_SEM data-plane array (§4).
`[HIGH · OBSERVED — offsets and verbatim Descriptions read from `tpb_xt_local_reg.json`]`

The eight `q7.intr_info_0..7` are the per-compute-core latches; the single `nx.intr_info`
is the sequencer's own. `[CARRIED · #914 / address pages corroborate per-Q7-core
`run_state_0..7` + `intr_info_0..7` living in the SP local-reg, separate from EVT_SEM]`

---

## 3. The "surprises" mechanism — poll + handler (instruction-exact)

A "surprise" is, in the firmware's own vocabulary, **an unexpected async event the
firmware checks for between instructions**. The naming comes straight from the DEBUG
build's embedded `S:`-format strings in `nx_dram` (source file `surprises.hpp`):

```
0x81930  "surprises.hpp"                                     (source file)
0x819cc  "S: sunda_check_surprises: any=%d, surprises=0x%x"  (check log-fmt anchor)
0x81a2b  "sunda_handle_surprises"                            (assert function-name arg)
0x81e55  "S: Unhandled surprise: surprises=0x%x"             (the FATAL path)
0x81923  "handle_no_op"   0x819fe "S: STRONG_ORDER"
0x81a0f  "S: INS_BREAK"   0x81a1d "S: EXT_BREAK"
0x81a42  "S: STEP_CNT=0"  0x81a51 "S: STEP_CNT=>%d"
```

> **NOTE — surprises live ONLY in the SEQ (NX) image.** The Q7 *compute* image
> (`q7_dram`) has **zero** `surprise`/`handle_interrupt`/`intr_info` strings. The
> surprises subsystem **is** the NeuronCore-*sequencer* firmware's async-event check;
> the compute engine (the `P%i:`-prefix image) is a slave the SEQ feeds (via
> EVT_SEM / `run_state`) and has no surprises subsystem of its own.
> `[HIGH · OBSERVED — string presence/absence]`

### 3a. The poll `poll-surprises @0x6af4` — FSM step 1

The cheap "is there any async work?" gate. It returns the per-engine work-pending /
running flag from DRAM state, and is the **first thing the FSM checks on every
iteration**.

```
6af4:  entry  a1, 48
6af7:  const16 a2,8 ; const16 a2,0x55e0 ; addi a2,a2,100   ; a2 = &state[0x855e0+100]
6b00:  s32i.n a2,[a1+12] ; l32i.n a2,[a1+12]
6b04:  l8ui   a2,[a2]    ; extui a2,a2,0,1                 ; a2 = state[+100] bit0
6b0a:  retw.n                                              ; -> work-pending FLAG
```

Called from **two** sites: `0x2d81` (the Sunda FSM) and `0x31c0` (the HW-decode FSM) —
both the loop continue/exit test. On a nonzero flag the FSM descends to `check_surprises`.
`[HIGH · OBSERVED — 2 call sites]`

### 3b. The check `sunda_check_surprises @0x6b0c`

```
6b0c:  entry a1,64 ; s32i a2,[a1+16] ; l32i a2,[a1+16]
6b15:  j 0x216ea               ; FLIX detour that COMPUTES the surprise word
                               ;   (lands in the dense FLIX/IVP span — see GOTCHA)
6b25:  a11 = any-bit (extui ...,0,1) ; a12 = the surprises word
6b2d:  const16 a10,8 ; const16 a10,0x19cc
6b33:  call8 0x18b84           ; LOG "S: sunda_check_surprises: any=%d, surprises=0x%x"
6b4f:  or a10,a2,a2 ; 6b52: call8 0x6ce0    ; -> sunda_handle_surprises(word)
6b55:  fold the handler's result back into the any/continue flag
```

Called from `0x2d9a`, the Sunda FSM redirect/advance step (1 call site). The log emits
`any` (was anything pending) and the `surprises` bitmask. `[HIGH · OBSERVED for the log
and the handle call]`

> **GOTCHA — the surprise-WORD source is inside the `0x216ea` FLIX detour and is *not*
> instruction-exact recoverable from stock objdump.** The `j 0x216ea` lands in the dense
> FLIX/IVP span where bundle sync is lost. Whether the word is built from an
> external-register (`rer`) read, the notification surface, or the `intr_info` latch is
> **`[INFERRED]` only — not asserted**. This is the explicit `surprises.hpp` poll-internals
> boundary (§5). `[MED non-claim]`

### 3c. The handler `sunda_handle_surprises @0x6cf4`

`0x6ce0` is a thin wrapper; the body is `0x6cf4` (`entry a1,48`). It is a **bit-mask
dispatcher** over a 2-byte surprise word at frame `[a1+8]`/`[a1+9]`, with named arms:

```c
// sunda_handle_surprises(word) — reconstructed dispatch (addresses inline)
int handled = 1;                              // 0x6cf4: default result -> [a1+12]
uint8_t hi = word[1];  // [a1+9]
uint8_t lo = word[0];  // [a1+8]

if (hi & (1<<1)) {                            // 0x6cfe: bbci a2,1 -> STRONG_ORDER
    log("S: STRONG_ORDER");                   // 0x6d0a (str 0x819fe)
    reprogram_ordering_mode();                // 0x6d22: read phase/ordering descriptor
}
// 0x6d34: log "S: INS_BREAK" (str 0x81a0f)   -- instruction breakpoint arm

if (lo & (1<<3)) {                            // 0x6d51: bbci a2,3 -> EXT_BREAK
    log("S: EXT_BREAK");                      // 0x6d5d (str 0x81a1d)
    // *** READ CSR 0x0014 == nx.instr_halt_ctrl ***
    if (csr_read(0x0014) == 0)                // 0x6d6e: movi a2,0x400; const16 a2,20; l32i
        ASSERT(surprises.hpp:0x1aa);          // 0x6d76: call8 0xa304 -> FATAL (unarmed)
}

if (hi & (1<<0)) {                            // 0x6dab: bbci a2,0 -> STEP_CNT path
    if (step_cnt == 0) log("S: STEP_CNT=0");  // 0x6dc7 (str 0x81a42)
    else { log("S: STEP_CNT=>%d", n);         // 0x6def (str 0x81a51)
           dram[0x85650]--; }                 // 0x6dfd: decrement step counter
}

if (!(hi & (1<<1)))                           // 0x6e11: bbsi a2,1 -> no recognized bit
    ASSERT(surprises.hpp:0x1b9);              // 0x6e1d: call8 0xa304 -> "Unhandled surprise"

return word[3] & 1;                           // 0x6e3e: l8ui [a1+12]; extui 0,1; retw.n
```

Every bit-test, log-string load, the `0x0014` CSR read (built as `const16 ...,20`), the
two `call8 0xa304` asserts, and the boolean return are **instruction-exact**. Both assert
sites load the function-name string (`sunda_handle_surprises` @ `0x6d7e`/`0x6e20`) and the
file string (`surprises.hpp` @ `0x6d84`/`0x6e26`), confirmed by a `const16`-immediate
scan.

### 3d. The surprise taxonomy `[HIGH · OBSERVED for names + reactions]`

| Surprise | Reaction | Policy |
|---|---|---|
| `STRONG_ORDER` | re-program instruction ordering mode | steer FSM |
| `INS_BREAK` | instruction-breakpoint hit → break/step path | steer FSM |
| `EXT_BREAK` | external-breakpoint hit; read `instr_halt_ctrl` (`0x0014`); assert if not armed | steer / halt |
| `STEP_CNT` (=0 / >N) | single-step countdown reached/updated → pause | steer FSM |
| `handle_no_op` | benign no-op surprise | continue |
| *(unrecognized)* | **"Unhandled surprise" assert → FATAL spin** (ErrorHandler) | HARD FAULT |

All arms are **debug / step / ordering** control events checked between ops. The
"surprises" mechanism is how the host/debugger **asynchronously perturbs the running
sequencer** — break, single-step, reorder — *without* a hardware interrupt: the host arms
a condition (via the hw-decode breakpoint CSRs / the external-register the surprise word
is built from), the FSM notices it at the next poll, and steers (pause / halt / reorder).
A surprise does **not** itself "report to host" — it steers the FSM. The hard-fault
*reporting* path (the TIE user-register notify + the error notify ring) is the separate
ErrorHandler translation unit, not this handler. `[HIGH for names/reactions; the
host-arms-via-which-CSR tie to the breakpoint bundle is MED/INFERRED, grounded in the
`EXT_BREAK` handler reading `instr_halt_ctrl` (`0x0014`)]`

---

## 4. The SoC-IRQ → Q7 binding `[HIGH · MED]`

### 4a. The firmware dispatcher `handle_interrupt_ @0x4c5c`

The top-level dispatcher **reads `nx.intr_info` (CSR `0x001C`)** and branches on its value:

```
4c5c:  entry a1,48
4c5f:  movi a2,0x400 ; const16 a2,28 ; l32i a2,[a2]   ; *** READ nx.intr_info (CSR 0x001C)
4c6b:  beqz.n a2,0x4c76    ; intr_info == 0  -> IDLE (retw, nothing to do)
4c70:  beqi   a2,1,0x4c79  ; intr_info == 1  -> RUN
4c73:  j 0x4ca9            ; intr_info >= 2  -> ASSERT "handle_interrupt_"
                           ;   (interrupt_handler.hpp:28) via 0xa304  (FATAL)
4c79:  call8 0x2c64        ; enter_run + Sunda FSM (the RUN path)
4c7c:  call8 0x31ac        ; HW-decode FSM
4c82:  read nx.run_state (0x0008) ; beqi 2 -> paused-handling
```

The `const16 ...,28` (= `0x1C`) read at `0x4c62` is the **only** `0x001C` site in the
whole image (byte-scan = 1). The source-file string `interrupt_handler.hpp`
(`0x811db`) and `handle_interrupt_` (`0x81203`) confirm this function's identity.

So `intr_info` is a **three-state metadata latch read at a boundary**: `0` = idle, `1` =
run, `≥2` = fatal-assert. There is no vector entry; the dispatcher polls the latch.

### 4b. The boot arm `setup_interrupts @0x2724` `[HIGH · OBSERVED for the WER]`

At boot the engine arms its async surface via a **`wer` (write-external-register)**:

```
2776:  l32i a3,[a1+28]    ; a3 = an external-register address literal
2778:  wer  a2,a3         ; *** write a2 = (1<<12) = 0x1000 to that external register
```

The `wer` and the `0x1000` (clean single-bit enable) value are `[HIGH · OBSERVED]`. The
function sits in `setup_interrupts` (asserts cite `interrupt_handler.hpp:86`/`:105`). That
this external register **is** the interrupt enable is `[MED · INFERRED]` — it is in
`setup_interrupts`, the value is a clean 1-bit enable, and the core's IRQ surface is
external-register based (matching the heavy `rer`/`wer` usage and the **complete absence**
— byte-verified, **0** `const16` sites — of any memory-mapped store to `0x0018` `intr_ctrl`
in the clean disasm).

> **CORRECTION — `const16` site census (byte-scan).** `nx.intr_ctrl`
> (`0x0018`) has **0** sites — the SEQ **never** issues a memory-mapped store to
> `intr_ctrl` at all (verified two ways: no `const16 …,24`/`…,0x18` in the disasm, and
> the only raw byte-scan hit `X4 18 00` is the false positive `const16 a3,0xe18`).
> `nx.intr_info` (`0x001C`) has **1** (the dispatcher read at `0x4c62`); `q7.intr_ctrl`
> (`0x3028`) and `q7.intr_info_0` (`0x302C`) have **0**. **This *strengthens* the §4b
> inference**: with no mem-mapped `intr_ctrl` write anywhere, the engine's interrupt
> enable can only be armed through the external-register (`wer`) path of §4b. The SEQ
> also **does not** program the eight Q7-*compute* cores' per-core intr CSRs — those are
> host/management-driven, consistent with the SEQ being the *sequencer* that feeds the
> compute cores via EVT_SEM / `run_state`, not their interrupt controller.
> `[HIGH · OBSERVED — the earlier "0x0018 = 1 site" figure was wrong]`

### 4c. EVT_SEM vs true-IRQ — the resolution

The firmware's *primary* cross-engine async surface is the **EVT_SEM hardware
semaphore/event array** (256 events + 256 32-bit semaphores, op-windowed
read/set/inc/dec), which the firmware **polls** (`PollSem` / `add_semaphore_wait_ge_and_dec`
reads `tpb_semaphores_read` until `>= target`). The window offsets carried from the
address lane are:

| EVT_SEM window | Offset | Operation |
|---|---:|---|
| read | `0x1000` | read semaphore/event value |
| set | `0x1400` | set value |
| inc | `0x1800` | increment |
| dec | `0x1C00` | decrement (used by `wait_ge_and_dec`) |

`[CARRIED · address pages — windowed read/set/inc/dec]`

So the GPSIMD firmware **predominantly polls semaphores (EVT_SEM) and polls the surprises
word**, rather than taking HW interrupts. The custom `intr_info` CSR — read once at the
dispatcher — is the **only** true-IRQ-style surface, and even it is consumed by a **read
at a dispatch boundary, not by a vector**. EVT_SEM and `intr_info` are **distinct**:
EVT_SEM is the *data-plane sync array* (poll); `intr_info` is the *per-core IRQ-metadata
latch* (dispatcher read). This is the EVT_SEM-vs-IRQ resolution. `[HIGH — EVT_SEM poll
model CARRIED; `intr_info` read OBSERVED]`

### 4d. The full chain `[chain HIGH · MED at the HW hop]`

```
SoC INTC source  (leaf -> errtrig -> peb_intc apex)            [CARRIED INT-11/apex HIGH]
  -> [HW/INTC delivery to this engine's per-core intr latch:
      intr_info CSR 0x001C / q7 0x302C.. , gated by intr_ctrl 4-source enable]   [MED]
  -> firmware dispatcher handle_interrupt_ @0x4c5c READS nx.intr_info (@0x4c62)
        -> 0 = idle / 1 = run / >=2 = FATAL assert                               [HIGH/OBS]
  -> the RUN path enters enter_run -> the FSM, which on EVERY iteration
        POLLS surprises @0x6af4 -> sunda_check_surprises @0x6b0c
        -> sunda_handle_surprises @0x6cf4                                        [HIGH/OBS]
  -> the surprise bit STEERS the FSM:
        STRONG_ORDER (reorder) / INS_BREAK / EXT_BREAK (read halt CSR 0x0014;
        assert if unarmed) / STEP_CNT (pause) / handle_no_op (continue)
        / UNHANDLED -> FATAL spin (ErrorHandler).                                [HIGH/OBS]
```

The async "interrupt" thus does **not vector**; it sets a latch the dispatcher reads at a
boundary, and the in-loop surprises poll handles the fine-grained async control between
ops. The firmware halves (dispatcher read + surprises poll + handler reactions) are
`[HIGH · OBSERVED]`; the **SoC-source → `intr_info` latch encoding** is the HW side and is
`[MED]`, **not** register-traced here.

> **CORRECTION — the apex-pending-bit → core-vector hop is `[INFERRED]`, firmware-owned,
> and NOT closed by this page.** The leaf → errtrig → `peb_intc` apex chain is
> `[CARRIED · HIGH]` from the INTC sources lane; the apex aggregates 128 inputs into a
> 4×32-bit pending image (see [schema atlas](./schema-atlas.md) and
> [PEB apex](./peb-cc-topsp-triggers.md)). But the final **apex-bit → which intr-source /
> which `intr_info[31:0]` metadata value** is **not in any shipped schema** — it is
> HW/INTC-internal. This page proves the *firmware reaction* to whatever lands in
> `intr_info`; it does **not** prove the bit-encoding into `intr_info`. Stated, not
> fabricated. `[MED non-claim — see §6/O2]`

---

## 5. The `surprises.hpp` poll-internals boundary `[explicit]`

This page decoded the **binding** and the **handler** directly to close the terminal gap
on the GPSIMD-Q7 side. The remaining poll-internals are explicitly out of reach of stock
objdump:

| Decoded here `[HIGH · OBSERVED]` | Left to the `surprises.hpp` poll internals |
|---|---|
| poll site `0x6af4` + its 2 callers; check `0x6b0c` + its log | the instruction-exact **source of the surprise WORD** inside the `0x216ea` FLIX detour (which external register / bit lanes feed each surprise bit) |
| handler `0x6cf4` bit-mask dispatch, all named arms, the `0x0014` read, the two asserts | the full surprise-bit → index map **beyond** the named arms |
| dispatcher `0x4c5c` `intr_info` read; boot `wer`-arm `0x2724` | the `handle_no_op` interior |
| the ISA census (polled-not-vectored); the EVT_SEM-vs-IRQ resolution; the two-Q7 split | — |

The undecoded items land in the dense FLIX/IVP span where stock objdump loses bundle
sync. This is the **explicit** boundary — not a fabricated claim.

---

## 6. INT-16 reconciliation — the two-Q7 split `[HIGH · MED]`

The unified cascade ends "apex MSI-X → IOFIC / io_fabric → Q7 / GIC", with the
apex-bit → vector map flagged firmware/HW-owned. **Two distinct "Q7"s must be separated**
to close it correctly — the key finding of this page:

1. **The MANAGEMENT / "Pacific" core** — the SoC-survival ISR sink that reads the
   `peb_intc` apex pending image and indexes the 32 critical fast-paths
   (NSM / thermal / SPI / …). This is a **separate** management core; its ISR bodies do
   **not** ship in this repo. `[CARRIED · NSM / unified-flow pages]`
2. **The GPSIMD COMPUTE NeuronCore Q7** — *this report's subject*. Its async surface is
   the **polled surprises word + the per-core `intr_info` CSR** (`q7.intr_info_0..7`),
   **not** the apex critical fast-path. Compute-leaf faults — however severe to a job —
   are caught by the *management* core decoding a per-domain summary; the GPSIMD Q7 is
   **not** a critical apex sink. It self-reports job faults via its notification ring and
   self-halts.

So the unified "terminal L3 sink" for the SoC-survival set is the **management Pacific
core** (its apex → vector map still firmware/HW-owned — **unchanged**, see O1); the GPSIMD
compute Q7 reached here is a **polled** engine that is the sink for its **own** engine's
surprises / job-faults. `[reconciliation HIGH for the two-core split + the polled GPSIMD
model; the apex-bit → management-Pacific-vector hop remains the gap, NOT closed here]`

### Open / non-claims (explicit)

- **O1.** The **apex-pending-bit → management-"Pacific"-vector** hop remains
  firmware/HW-owned and not register-encoded. This page does **not** close it; it closes
  the *different* GPSIMD-compute-Q7 side. `[stated]`
- **O2.** The **SoC-INTC-source → per-core `intr_info` bit encoding** (which apex/leaf bit
  lands in which of the 4 `intr_ctrl` sources / the `intr_info[31:0]` metadata) is the
  HW/INTC side and is **not register-traced** in any shipped artifact reachable here. The
  leaf → apex chain is `[CARRIED · HIGH]`; only the final bit-encoding is uninstrumented.
  `[MED non-claim]`
- **O3.** The **surprises-WORD source** (inside the `0x216ea` FLIX detour) is not
  instruction-exact recoverable; `rer` vs notification vs `intr_info` is `[INFERRED]` only.
  `[LOW · §5 boundary]`
- **O4.** The boot `wer` (`0x2778`, value `0x1000`) is OBSERVED to arm an external
  register; that this register **is** the interrupt enable is `[MED · INFERRED]`
  (placement + clean 1-bit value + external-register IRQ surface), not a named-CSR match.
- **O5.** Whether the eight Q7-compute cores ever take their `q7.intr_info_0..7` latch
  directly (host-armed) is **not observable** in the carved firmware (the SEQ never
  programs `0x3028`/`0x302C`). The *absence* is OBSERVED; the host-side arming is out of
  this blob's scope. `[MED]`

---

## 7. Per-generation stability `[HIGH · CARRIED]`

| Surface | Cross-gen behaviour |
|---|---|
| **EVT_SEM poll surface** | byte-identical across cayman / mariana / mariana_plus (256 events + 256 semaphores, read/set/inc/dec windows) — no geometry change. The dominant polled async surface is **gen-stable**. `[CARRIED · HIGH]` |
| **Per-core `intr_ctrl`/`intr_info` bundle** | part of `tpb_xt_local_reg`, dated 2022-12-14 (Cayman baseline); the "4 sources/core" + 8-core `intr_info` layout is the Cayman model. `[CARRIED · HIGH; cross-gen delta of this bundle not separately re-verified — MED that it is gen-stable]` |
| **SoC INTC fabric** | **DIVERGES at Maverick** (decentralized per-IP-block INTCs + `iofic_x8_msix` security IOFIC + per-die apex). But that is the SoC-*side* fabric feeding the **management** Pacific core, **not** the GPSIMD-Q7 polled surprises surface. `[CARRIED · HIGH split]` |

The GPSIMD firmware's **polled model** (surprises poll + EVT_SEM poll + `intr_info`
dispatcher read) is the gen-stable on-die-compute side; the apex / INTC growth is the
orthogonal management-delivery side.

> **NOTE — v5 / MAVERICK is header-OBSERVED only.** The polled model is OBSERVED on the
> **Cayman** image. Its gen-stability is `[INFERRED-strong]` from the gen-stable EVT_SEM
> surface and the frozen errtrig primitive — but any reading of v5 *interior* firmware
> behaviour is `[INFERRED]` and flagged: the Maverick artifacts are header-OBSERVED, the
> v5 *interior* is not in hand.

---

## 8. Function & CSR map

| Address / CSR | Identity | Role |
|---|---|---|
| IRAM `0x4c5c` | `handle_interrupt_` | dispatcher: reads `intr_info` (`0x001C`), branches 0/1/≥2 |
| IRAM `0x4c62` | `const16 ...,28` | the **only** `0x001C` read site (the `intr_info` poll) |
| IRAM `0x2724` | `setup_interrupts` | boot arm; `wer 0x1000` at `0x2778` |
| IRAM `0x6af4` | `poll-surprises` | FSM step-1 work-pending gate (DRAM `state[0x855e0+100]` bit0) |
| IRAM `0x6b0c` | `sunda_check_surprises` | computes word (FLIX detour `0x216ea`), logs, calls handler |
| IRAM `0x6cf4` | `sunda_handle_surprises` | bit-mask dispatcher (the handler body; wrapper at `0x6ce0`) |
| IRAM `0xa304` | assert/FATAL trampoline | both `sunda_handle_surprises` asserts target this |
| IRAM `0x90`/`0xad` | `_start` / `wsr.vecbase` | the single `wsr.vecbase` instruction (boot, `@0xad` inside `_start`) |
| CSR `0x0008` | `nx.run_state` | read after RUN path to detect paused state |
| CSR `0x0014` | `nx.instr_halt_ctrl` | read by the `EXT_BREAK` arm; `halt_req` bit |
| CSR `0x0018` | `nx.intr_ctrl` | `en[3:0]` — 4-source enable (1 `const16` site) |
| CSR `0x001C` | `nx.intr_info` | `metadata[31:0]` — the IRQ-metadata latch read at the dispatcher |
| CSR `0x3028` | `q7.intr_ctrl` | `en[31:0]` — 4 bits/core × 8 cores (0 SEQ sites) |
| CSR `0x302C`–`0x3048` | `q7.intr_info_0..7` | per-compute-core metadata latch (0 SEQ sites) |

---

## 9. Reimplementation notes

A from-scratch reimplementation of this engine's async-event surface must reproduce a
**polled** model, not an ISR-driven one:

1. **No leveled interrupt vectors.** Program `VECBASE` once; ship exception + windowed-ABI
   vectors only. Do **not** emit `rsil`/`rfi`/`INTENABLE` — there is no leveled delivery.
2. **A dispatch-boundary IRQ read.** The entry dispatcher reads the per-core `intr_info`
   metadata latch (`0x001C`) and branches `0 = idle / 1 = run / ≥2 = fatal`. The interrupt
   is delivered by **latch-and-read**, not by vectoring.
3. **An in-loop surprises poll.** Step 1 of every FSM iteration reads a work-pending flag
   (`0x6af4`); on pending, compute a surprise word and run a **bit-mask handler** with the
   arms `STRONG_ORDER` / `INS_BREAK` / `EXT_BREAK` / `STEP_CNT` / `handle_no_op`, and a
   **FATAL** default for any unrecognized bit. `EXT_BREAK` must validate against
   `instr_halt_ctrl` (`0x0014`) and assert if the breakpoint was not armed.
4. **EVT_SEM as the data plane.** Cross-engine sync is the polled 256-event / 256-semaphore
   array (`read@0x1000` / `set@0x1400` / `inc@0x1800` / `dec@0x1C00`), consumed by
   `wait_ge_and_dec` — **distinct** from the IRQ-metadata latch.

The one thing a reimplementation **cannot** reproduce from this page alone is the
**HW-side encoding** of a SoC-INTC source into the `intr_info` metadata word (O2) and the
exact lane composition of the surprise word inside the FLIX detour (O3). Those are the
firmware/HW-owned boundaries this page honestly flags rather than fabricates.
