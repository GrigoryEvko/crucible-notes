# The XEA3 Interrupt / Exception Architecture

The GPSIMD **Vision-Q7** core (the AWS Annapurna "Cayman" Trainium tile compute
engine, `ncore2gp` config — Cadence Xtensa LX, "Cairo" microarchitecture, Xtensa24
ISA, RI-2022.9, `TargetHWVersion=NX1.1.4`) implements **Exception Architecture 3
(XEA3)**, not the older XEA2 model that most shipped Xtensa cores use. This page is the
**core-side** specification of that exception/interrupt unit: the architectural register
file, the single-dispatch vector mechanism, the indirect `IEVEC` vectoring register, the
priority-arbitration state, and how a SoC interrupt physically enters the core. It is
the architectural foundation that the firmware-side pages build on — those pages describe
what the *shipped firmware* does with the model (it polls); this page describes what the
*silicon* provides.

The single most important fact, and the one that overturns the framing used by the
sibling firmware pages, is established in §1: **the absence of leveled interrupt
registers on this core is not a "minimal XEA2" — it is the defining signature of XEA3.**
XEA3 deliberately removes XEA2's level-indexed `EPC[1..n]`/`EPS[2..n]` bank and its
`rsil`/`rfi`/`INTENABLE` machinery, replacing them with a *single* dispatch path
(`excw` → one vector → register-based demux through `MS`, `EXCCAUSE`, and `IEVEC`) plus a
hardware **priority-arbitration** state. A reader who sees "no `INTENABLE`, no
`EPC2-6`, no `rfi`" and concludes "old, cut-down XEA2" has the architecture exactly
backwards.

**Scope and the two-core caveat.** The "Q7" name appears on two unrelated cores in this
package. This page is about the **GPSIMD compute Vision-Q7** core (the one whose ISA is
`ncore2gp`, decoded from the shipped firmware and the shipped ISA config). It is **not**
about the management/"Pacific" survival core, which is a scalar Xtensa-LX core whose
exception model is decoded with the LX (non-FLIX) rule and may differ; where this page
contrasts the two it says so explicitly. The SEQ engine and the eight compute lanes are
both `ncore2gp` Vision-Q7 and share the XEA3 register file decoded here.

Related pages (cross-linked, not duplicated):
[INTC → Q7 firmware "surprises" binding](./q7-surprises-binding.md) ·
[The interrupt/exception handler bodies](./handler-bodies.md) ·
[SEQ surprises / IRQ poll (firmware)](../../firmware/seq/surprises-irq.md) ·
[CSR — Xtensa Q7 debug / OCD / TRAX / PMU](../csr/xtensa-q7.md) ·
[CSR — `tpb_xt_local_reg` (on-die IRQ surface)](../csr/tpb-xt-local-reg.md) ·
[PEB apex / CC / TOP_SP sources](./peb-cc-topsp-triggers.md) ·
[Physical INTC instances](./physical-intc-instances.md).

**Confidence legend.** `HIGH` = byte-exact from the ISA config / CSR schema / firmware
disasm or re-verified here. `MED` = strong inference from naming + cross-file. `LOW` =
plausible, flagged. `OBSERVED` = read from a shipped artifact. `INFERRED` = reasoned.
`CARRIED` = consolidated from a named sibling report/page, not re-derived here.

---

## 0. Provenance — what was read `[HIGH · OBSERVED]`

Every architectural fact below derives **solely** from static analysis of shipped
artifacts, under lawful interoperability RE (DMCA 17 U.S.C. 1201(f)). No vendor source
snapshot was consulted.

| Input | Path (under `neuronx-gpsimd/`) | What it gives |
|---|---|---|
| ISA config DLL | `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libisa-core.so` | the 81 architectural states + 34 structured sysregs + opcode roster (the XEA3 register file) |
| Simulator DLL | `.../ncore2gp/config/libcas-core.so` | the `dll_exception_table` (EXCCAUSE → name) + the 119 `nx_*` interrupt/exception ports |
| On-die IRQ CSRs | `extracted/nested/cayman-arch-regs_tgz/csrs/tpb/tpb_xt_local_reg.json` (dated 2022-12-14) | `nx.intr_ctrl`/`intr_info`, `q7.intr_ctrl`/`intr_info_0..7` — the custom per-core latch |
| Debug aperture | `.../cayman-arch-regs_tgz/csrs/xtensa_q7/xtensa_q7.json` | the external OCD/TRAX/PMU debug-interrupt surface (distinct path) |
| SEQ firmware | `extracted/.../c10/lib/libnrtucode.a` members `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` | the vector table head, the `wsr.vecbase` boot site, the dispatcher prologue |
| Disassembler | `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) | the shipped Cadence toolchain — the only tool that decodes this FLIX config |

> **GOTCHA — `extracted/` and `ida/` are gitignored.** `fd`/`rg` skip them by default;
> use `--no-ignore` or absolute paths. The IDA v3 sidecars (`*_functions.json` /
> `*_xrefs.json`) under `neuronx-gpsimd/ida/` are also gitignored.

> **GOTCHA — the FLIX-desync wall.** Stock `xtensa-elf-objdump` decodes the scalar core
> ISA correctly, including every exception/SR opcode this page cites, but loses bundle
> sync inside the dense FLIX/IVP wide-bundle spans (`op0 ∈ {0x7e, 0x7f}`,
> `IsaMaxInstructionSize=32`). It mis-prints those bundles as stray `j 0x21xxx` "jumps"
> to addresses *outside* the image. Every disasm claim here is anchored on the **scalar**
> exception/dispatch path, which sits outside those spans; no claim depends on a FLIX
> interior. `[HIGH/OBSERVED — image size vs the bogus targets.]`

> **QUIRK — `.data.rel.ro` VMA ≠ file offset.** The `libisa-core.so` ISA tables live in
> `.data.rel.ro` with VMA−fileoffset delta `0x200000` (NOT libtpu's `0x400000`);
> `.rodata` string targets are delta-0. Subtract `0x200000` before `xxd`-ing a table
> struct, or you read the wrong bytes.

---

## 1. XEA3, not XEA2 — the register-file proof `[HIGH · OBSERVED]`

The decisive evidence is the **architectural register file itself**, read byte-exact from
the `ncore2gp` ISA config (`libisa-core.so`: 81 states, 34 structured sysregs, and the
opcode roster). The Xtensa exception architecture is identified unambiguously by *which*
exception/interrupt special registers exist. The two architectures are mutually
exclusive on the registers below.

| Register / instruction | XEA2 (leveled) | XEA3 (single-dispatch) | Present on this core? |
|---|---|---|---|
| `EPC1` (single saved PC) | yes (one of `EPC1..7`) | **yes (the only EPC)** | **yes** — `EPC` SR `0xb1`, 1 entry |
| `EPC2`–`EPC7` (per-level saved PC bank) | **yes** | **removed** | **no** — 0 in roster & firmware |
| `EPS2`–`EPS7` (per-level saved PS bank) | **yes** | **removed** | **no** — 0 |
| `EXCSAVE1`–`EXCSAVE7` | yes | (folded) | not in roster |
| `INTERRUPT`/`INTSET`/`INTCLEAR` | **yes** | **removed** | **no** — 0 |
| `INTENABLE` | **yes** | **removed** | **no** — 0 |
| `rsil` (raise interrupt level) | **yes** | **removed (no levels)** | **no** — 0 in both images |
| `rfi` (return-from-interrupt) | **yes** | **removed** | **no** — 0 in both images |
| `MS` — dispatch-mode/state register | **no** | **yes (XEA3 core)** | **yes** — SR `0xe5` |
| `IEVEC` — indirect vector register | no | **yes** | **yes** — SR `0x74` |
| `ISB` — interrupt stack base | no | **yes** | **yes** — SR `0xec` |
| `ISL` — interrupt stack limit | no | **yes** | **yes** — SR `0xf8` |
| `KSL` — kernel stack limit | no | **yes** | **yes** — SR `0xf9` |
| `excw` — exception/dispatch wait | no | **yes (opcode 0)** | **yes** — `opcodes[0]` |
| `ActiveInterrupt`/`ActivePriority` arb. state | no (level-implicit) | **yes (priority arbitration)** | **yes** — states 25–31 |

The third column is not a partial match: **every** XEA2-only register is absent (count 0
in the roster *and* in both shipped firmware images), and **every** XEA3-distinctive
register is present. The clincher is structural: the ISA config groups eight of these SRs
into a package literally named **`xt_exception_dispatch`** —

```text
package xt_exception_dispatch  (8 SR triples = 24 rsr/wsr/xsr opcodes):
  EPC (0xb1)   EXCCAUSE (0xe8)   EXCVADDR (0xee)   ISB (0xec)
  ISL (0xf8)   KSL (0xf9)        MS (0xe5)         VECBASE (0xe7)
opcodes[0..2] = excw / syscall / halt        (the XEA3 dispatch entry instructions)
```

"Exception **dispatch**" is the XEA3 design name — XEA2's package would be
"exception" with the leveled-interrupt registers alongside. The presence of
`xt_exception_dispatch` as the package, with `MS`/`ISB`/`ISL`/`KSL` as members and zero
leveled registers, is conclusive. `[HIGH/OBSERVED — package membership read from the
opcode roster; SR ids cross-checked byte-exact against the encode templates.]`

> **CORRECTION — this core is XEA3; the sibling pages' "XEA2 single-level exception
> model" label is imprecise.** The
> [q7-surprises-binding](./q7-surprises-binding.md) page and the firmware
> [surprises-irq](../../firmware/seq/surprises-irq.md) page both describe the config as
> the "single-level XEA2 *exception* model" because they (correctly) observed *no leveled
> interrupt SRs* and reasoned "therefore not the interrupt half of XEA2, so it's the
> exception half." That inference reached the right firmware conclusion (the core takes
> no leveled HW interrupt) for the wrong architectural reason. The byte evidence —
> `MS`, `IEVEC`, `ISB`, `ISL`, `KSL`, `excw`, the priority-arbitration states, and the
> `xt_exception_dispatch` package name — is the **XEA3** register file. XEA3 *unifies*
> interrupts and exceptions into one dispatch; "no leveled interrupt registers" is what
> XEA3 *looks like*, not evidence of a stripped XEA2. Treat this page as the authority on
> the architecture name; the firmware conclusion (polled, never vectored) is unchanged.
> `[HIGH/OBSERVED — full register-file census; this is the strongest correction on the page.]`

> **NOTE — IEVEC iclass name-skew.** `rsr/wsr/xsr.ievec` (SR `0x74`) carry the iclass
> *name* `xt_iclass_{rsr,wsr,xsr}.gserr` in this config, not `…IEVEC`. No `gserr` *opcode*
> exists; the GPSIMD config aliases the IEVEC SR slot onto a `gserr` iclass row (a
> config-time naming artifact). The encoding is genuine IEVEC: `rsr.ievec` =
> `0x00037400` (RSR template `0x00030000 | (0x74<<8)`), `wsr.ievec` = `0x00137400`,
> `xsr.ievec` = `0x00617400`. Decode by SR byte `0x74`, not by the skewed iclass name.
> `[HIGH/OBSERVED — encode templates; MED on the alias *interpretation*.]`

> **NOTE — "present in the ISA config" vs "exercised by the firmware."** All five
> XEA3-distinctive SRs (`MS`, `IEVEC`, `ISB`, `ISL`, `KSL`) exist in the `ncore2gp` ISA
> config with full `rsr`/`wsr`/`xsr` opcodes — that is what proves the *architecture* is
> XEA3. Of the five, only **`MS`, `ISB`, and `ISL` actually appear as live `wsr`/`rsr`
> operands in the shipped firmware**; `IEVEC` and `KSL` are defined by the architecture but
> are **not** touched by the SEQ/compute images (the firmware sets up the interrupt stack
> via `ISB`/`ISL` but never programs the indirect vector `IEVEC` or the kernel-stack limit
> `KSL` — consistent with a polled firmware that never installs a dispatch handler).
> Re-verified firmware sites: `wsr.ms` @`0x01a4` (NX) / `0x01c8` (Q7) — encoding `00e513`,
> SR `0xe5`; `wsr.isb` @`0x00a4` (both, `const16 a4,0x4ec0 ; wsr.isb a4`) — `40ec13`, SR
> `0xec`; `wsr.isl` @`0x1935` + `rsr.isl` @`0x1c0b4` (NX) — `30f813`, SR `0xf8`. Even
> `EPC` itself is **not** read/written as an operand in either image — the firmware never
> takes a dispatch, so it never restores `EPC`. `[HIGH/OBSERVED — re-verified this session
> against the shipped `ncore2gp` disasm; the ISA-table existence of `IEVEC`/`KSL`/`EPC` is
> the architecture, the firmware non-use is the polled-model corollary.]`

---

## 2. The architectural exception/dispatch state `[HIGH · OBSERVED]`

The exception machinery is decomposed into named **states** (the architectural storage)
that the **sysregs** map onto via bit-field `contents[]` descriptors. Reading the state
list is reading the XEA3 model directly. The exception-relevant states (`xt_exceptions`
package) and their SR homes:

| State(s) | bits | Backing SR (id) | Role |
|---|---|---|---|
| `VECBASE` | 26 | `VECBASE` (`0xe7`) | vector base; the `[31:6]` page that all dispatch vectors hang off |
| `EPC` | 32 | `EPC` (`0xb1`) | the **single** exception PC (no per-level bank) |
| `EXCCAUSE` + `EXCINF` | 16 + 5 | `EXCCAUSE` (`0xe8`) | dispatch cause code + 5-bit info |
| `EXCVADDR` | 32 | `EXCVADDR` (`0xee`) | faulting virtual address |
| `MS_DISPST` + `MS_DE` | 5 + 1 | `MS` (`0xe5`) | **dispatch state** (5-bit) + **dispatch-enable** (1-bit) |
| `ISB` | 32 | `ISB` (`0xec`) | interrupt-stack base |
| `ISL` / `KSL` | 29 / 29 | `ISL` (`0xf8`) / `KSL` (`0xf9`) | interrupt-stack limit / kernel-stack limit |
| `PSDIEXC`,`PSDI`,`PSRING`,`PSSTACK`,`PSSS`,`PSENTRYNR` | 1,1,1,3,2,1 | `PS` (`0xe6`) | decomposed processor-status bits (ring, stack, DI/DIEXC mode) |
| `WB_{S,P,C,N,T,M}` | 2,3,3,3,3,3 | `WB` (`0x48`) | window-base decomposition |
| `ActivePriority` / `ActiveInterrupt` / `ActiveFairness` | 8 / 8 / 1 | (arb. state) | the interrupt currently being serviced + its priority + fairness |
| `SecondPriority` / `SecondInterrupt` / `SecondFairness` | 8 / 8 / 1 | (arb. state) | the runner-up in the priority arbitration |
| `CurrentPriority` | 8 | (arb. state) | the priority threshold currently masking lower-priority requests |

The shape of the `MS` register is the heart of XEA3 dispatch. It packs a **5-bit
dispatch-state** (`MS_DISPST`) and a **1-bit dispatch-enable** (`MS_DE`) into a single SR
(`MS` = `0xe5`, layout `[5:0]=MS_DISPST, [5]=MS_DE, [31:6]=const0`). Where XEA2 encoded
"which interrupt level am I at" implicitly in `PS.INTLEVEL` and selected one of seven
vector slots, XEA3 encodes "what dispatch state am I in" explicitly in `MS` and uses a
*single* dispatch path that reads the cause and demuxes in software/microcode.
`[HIGH/OBSERVED — state list + `MS` `contents[]` map.]`

The **priority arbitration** states (`ActiveInterrupt`/`ActivePriority`/`SecondInterrupt`/
…/`CurrentPriority`, 8-bit each) are the XEA3 replacement for XEA2's fixed 1..7 level
ladder: instead of seven hardwired levels with one vector each, XEA3 carries an 8-bit
priority and an 8-bit active-interrupt id, arbitrates the highest-priority pending
request against `CurrentPriority`, and records the winner (`Active*`) and runner-up
(`Second*`). The `*Fairness` bits implement round-robin tie-breaking among equal-priority
requests. This is the "many-interrupt extension": up to 256 distinguishable interrupt
ids and 256 priorities, arbitrated by hardware, vs XEA2's 7 levels.
`[HIGH/OBSERVED on the state widths; the arbitration *algorithm* is INFERRED from the
state names + widths — there is no firmware that exercises it, see §6.]`

> **NOTE — `InOCDMode` is the architectural reflection of debug entry.** State `InOCDMode`
> (1 bit) is the core's own view of "I am in OCD/debug mode," set when the external debug
> aperture forces a debug interrupt. It is the in-core mirror of the
> [Q7 OCD `DSR.Stopped`](../csr/xtensa-q7.md#3-ocd--debug-block--0x2000-15-regs) bit, and
> the architectural tie for the `rfdo` (return-from-debug-operation) instruction
> (opcode 235, `xt_debug` package). `[HIGH/OBSERVED — state present; tie CARRIED CSR-Q7.]`

---

## 3. The vector base and the vector table `[HIGH · OBSERVED]`

XEA3 keeps the relocatable **`VECBASE`** mechanism: all dispatch vectors live at a
`VECBASE`-relative page (`VECBASE` is `[31:6]`, so vectors are 64-byte-aligned within the
base). The boot path programs `VECBASE` **exactly once** — there is a single
`wsr.vecbase` site in the entire SEQ image, inside `_start` (`0x90`), executed after the
I-cache invalidate and before `MEMCTL`/`WindowBase`/MPU bring-up. The Q7 compute image is
identical: one `wsr.vecbase`, same exception/window vector shape.
`[HIGH/OBSERVED — `wsr.vecbase` census = 1 per image; boot order CARRIED FW-01.]`

The vector table head at `VECBASE` (== IRAM `0x0000` on this reset model) is
**exception + window only**:

| IRAM addr | Vector | Contents |
|---|---|---|
| `0x0000` | reset / dispatch entry | `j 0x1dc` → boot stub → `_start` |
| `0x0006` | second hand-written vector | `j 0x1e8` |
| `0x000c`–`0x0023` | window-overflow handlers | windowed-ABI spill (`s32e` runs) |
| `0x0024`–`0x005d` | window-underflow handlers | windowed-ABI fill (`l32e` runs) |
| `0x006c` | `EXCVADDR`-save exception vector | `rsr.excvaddr a2 ; s32i a2,… ; j 0x1b0` |

The structural correlate of §1 is visible here: **there is no dense block of leveled
interrupt vectors** (no Level-2..6 `EXCVEC` slots). A XEA2 core would have a contiguous
ladder of per-level interrupt vectors after the exception/window vectors; this table ends
the vector region and linear-sweeps cleanly into ordinary code. The config has no leveled
interrupt SRs (§1), so it has no leveled interrupt vectors — exactly the XEA3 single-entry
shape. `[HIGH/OBSERVED — disassembled vector head; "no leveled block" INFERRED-strong from
the clean sweep + the SR census.]`

> **QUIRK — only TWO hand-written vectors precede the window handlers.** XEA2 cores
> typically front-load the vector page with a reset vector, a user/kernel exception
> vector, a double-exception vector, *and* a run of interrupt vectors. Here the page is
> reset + one secondary vector + window-spill/fill + a single `EXCVADDR`-save exception
> vector. The compactness is itself diagnostic of XEA3's single-dispatch model: all
> non-window exceptions funnel through the one `EXCCAUSE`-demuxed dispatch path rather than
> through separate hardware vectors. `[HIGH/OBSERVED — vector-head disasm.]`

---

## 4. The dispatch mechanism — register-based, not level-indexed `[HIGH · OBSERVED / MED]`

This is the conceptual delta a reimplementer must get right. In XEA2, hardware picks a
vector by interrupt level: a level-N interrupt fires, hardware saves PC→`EPC[N]`,
PS→`EPS[N]`, raises `PS.INTLEVEL` to N, and jumps to the level-N vector; the handler runs
at that level and returns with `rfi N` (which restores `EPC[N]`/`EPS[N]`). There are as
many save banks and vectors as there are levels.

In **XEA3**, there is *one* dispatch. The core-side mechanism (architectural — what the
silicon provides) is:

1. An exception or interrupt-class event raises a dispatch. Hardware saves the
   interrupted PC into the **single** `EPC` and records the cause in `EXCCAUSE`
   (`[15:0]`) + `EXCINF` (`[20:16]`); for memory faults it also latches `EXCVADDR`.
2. Hardware sets the **dispatch state** in `MS` (`MS_DISPST`) and clears **dispatch
   enable** (`MS_DE`) so a second dispatch cannot re-enter until software re-arms it
   (the XEA3 analogue of "interrupts masked while in the handler").
3. Hardware vectors to the dispatch entry derived from `VECBASE` and, for the indirect
   path, the **`IEVEC`** register (`0x74`) — the software-programmable vector that lets
   the dispatch land on a handler table entry rather than a fixed-level slot.
4. The handler reads `EXCCAUSE`/`MS`/`IEVEC`, **demultiplexes in software** (the
   `EXCCAUSE` → handler map is the simulator's `dll_exception_table`: entry 0
   `NoException`, then `L32ETailchainException`, `TailchainException`, `LoadStoreError`,
   …), services it on the interrupt stack bounded by `ISB`/`ISL` (or the kernel stack
   bounded by `KSL`), and returns by restoring `EPC`/`PS` and re-enabling dispatch
   (`MS_DE`).

The `EXCCAUSE`-named **`TailchainException`** entries in `dll_exception_table` are a XEA3
tell: *tail-chaining* (servicing the next-highest-priority pending interrupt on the way
out of a handler, without a full restore/re-dispatch) is an XEA3 feature, encoded as a
dispatch cause rather than a separate vector. `[exception-table contents HIGH/OBSERVED
from `libcas-core.so`; the step-by-step dispatch sequence is INFERRED from the XEA3
register set + the table — see §6 for why the firmware does not exercise it.]`

```c
/* ARCHITECTURAL dispatch model (what the silicon provides; the SHIPPED firmware does
 * NOT run this — see §5/§6). Reconstructed from the XEA3 register file + the
 * EXCCAUSE→name table; register ids are byte-exact, the control flow is the XEA3
 * specification, not a decoded firmware routine.  [HIGH on regs · INFERRED on flow] */

#define SR_EPC       0xb1   /* single exception PC      */
#define SR_EXCCAUSE  0xe8   /* cause[15:0] | inf[20:16] */
#define SR_EXCVADDR  0xee   /* faulting vaddr           */
#define SR_MS        0xe5   /* [4:0]=DISPST [5]=DE      */
#define SR_IEVEC     0x74   /* indirect vector          */
#define SR_VECBASE   0xe7   /* [31:6] vector page       */
#define SR_ISB       0xec   /* interrupt-stack base     */
#define SR_ISL       0xf8   /* interrupt-stack limit    */
#define SR_KSL       0xf9   /* kernel-stack limit       */

#define MS_DE        (1u << 5)            /* dispatch-enable bit      */
#define MS_DISPST(m) ((m) & 0x1Fu)        /* 5-bit dispatch state     */

/* On a dispatch, HARDWARE does (atomically, before the first handler insn):
 *   EPC      <- interrupted PC
 *   EXCCAUSE <- cause code (| EXCINF)
 *   EXCVADDR <- faulting vaddr      (memory faults only)
 *   MS.DE    <- 0                   (re-entry blocked: XEA3 "mask")
 *   MS.DISPST<- dispatch state
 *   PC       <- VECBASE-relative dispatch entry  (one entry, not per-level)
 */

void xea3_dispatch_handler(void) {            /* the single software dispatch root */
    uint32_t cause = read_sr(SR_EXCCAUSE) & 0xFFFFu;
    uint32_t ms    = read_sr(SR_MS);
    /* XEA3 demuxes by CAUSE in software, NOT by a hardware level/vector index. */
    switch (cause) {
        case 0x00: /* NoException                — spurious; just return            */ break;
        case /*…*/ LoadStoreError:        handle_loadstore_fault(read_sr(SR_EXCVADDR)); break;
        case /*…*/ TailchainException:    /* service next pending without full restore */
                   tail_chain_next();     break;
        case /*…*/ L32ETailchainException:/* window-fill tail-chain                  */
                   tail_chain_window();   break;
        default:   fatal_error_handler(cause);   /* ErrorHandler → notify ring + halt */
    }
    /* return: restore EPC/PS, re-arm dispatch */
    write_sr(SR_MS, ms | MS_DE);          /* MS.DE <- 1  (the XEA3 "unmask")          */
    /* … architectural dispatch-return (restores EPC→PC); no `rfi N`, no level index. */
}
```

> **GOTCHA — there is no `rfi`, and that is correct.** A reimplementer porting XEA2 code
> will reach for `rfi <level>` to return from a handler. XEA3 has no `rfi` and no level
> operand: dispatch return restores the single `EPC` and re-enables dispatch via `MS.DE`.
> The `rsil`/`rfi` count is **0** in both shipped images precisely because the
> architecture has no such instructions. `[HIGH/OBSERVED — mnemonic census.]`

---

## 5. How the shipped firmware actually uses the model — it polls `[HIGH · OBSERVED]`

The architecture in §4 is what the silicon *can* do. The shipped GPSIMD firmware
deliberately does **not** use the dispatch vector for async SoC events. This is the
critical reimplementation note: **a Vision-Q7-compatible exception unit must implement the
XEA3 dispatch machinery, but the GPSIMD firmware that runs on it bypasses async vectoring
in favor of polling.** The full firmware-side decode lives on the sibling pages; the
core-side summary:

- **Negative ISA census (the proof):** `rsil`=0, `rfi`=0, `wsr/rsr.intenable`=0,
  `wsr/rsr.interrupt`=0, `eps2..6`=0, `epc2..6`=0 in **both** the SEQ (`nx_iram`) and Q7
  compute (`q7_iram`) images. `waiti` appears once (NX) / three times (Q7), all off the
  run-loop path; `wsr.vecbase` once per image (boot). A core that took leveled interrupts
  would *need* `rsil` to mask and `rfi` to return — their total absence means the firmware
  never enters or returns from any async vector. `[HIGH/OBSERVED — see
  [surprises-irq §1](../../firmware/seq/surprises-irq.md), re-verified this session.]`

- **Three async surfaces, all polled or read at a boundary, never vectored:**
  (1) the firmware-owned **"surprises" word**, polled at FSM step 1 every iteration
  (`poll_surprises @0x6af4` → `sunda_check_surprises @0x6b0c` →
  `sunda_handle_surprises @0x6cf4`); (2) the **EVT_SEM** hardware semaphore/event array,
  polled via `PollSem`/`wait_ge_and_dec`; (3) the custom **`intr_info`** CSR, *read once*
  at the dispatcher boundary `handle_interrupt_ @0x4c5c`. The full poll/handler decode is
  on the [SEQ surprises-irq](../../firmware/seq/surprises-irq.md) page; the
  EXCCAUSE→handler bodies are on the
  [handler-bodies](./handler-bodies.md) page; the SoC-INTC→firmware terminal hop is on the
  [q7-surprises-binding](./q7-surprises-binding.md) page. `[CARRIED — not re-duplicated here.]`

> **NOTE — why a polled firmware on a vectoring core?** XEA3's dispatch path is for
> *exceptions* (load/store faults, window tail-chains, illegal instructions) and for
> *general-purpose* interrupt handling. A GPSIMD tile sequencer is a tight fetch-decode-
> dispatch loop with hard real-time ordering constraints; taking an async vector mid-bundle
> would corrupt the FLIX issue state and the strong-ordering guarantees. The firmware
> therefore handles fine-grained async control (break/step/order) cooperatively at safe
> points (the surprises poll) and uses the XEA3 *exception* dispatch only for genuine
> faults (which terminate the job). `[INFERRED — design rationale, grounded in the
> ordering-mode surprise + the real-time FSM.]`

---

## 6. The on-die SoC-IRQ surface — a custom CSR latch, not the Xtensa SRs `[HIGH/MED · OBSERVED/INFERRED]`

Because the firmware never arms `INTENABLE` (it does not exist) and never vectors, a SoC
INTC interrupt destined for this engine cannot enter through the Xtensa architectural
interrupt path. Annapurna provides a **custom per-core interrupt block** in the TPB
local-register aperture (`tpb_xt_local_reg`, APB, 64 KiB) — this is the on-die IRQ
*surface*, separate from both the XEA3 SRs and the EVT_SEM data-plane array:

| CSR (abs offset) | Field | Reset | Meaning |
|---|---|---|---|
| `nx.intr_ctrl` `0x0018` | `en[3:0]` | `0x0` | interrupt **enable** — exactly **4 sources** for the NX core |
| `nx.intr_info` `0x001C` | `metadata[31:0]` | `0x0` | interrupt **metadata** latch (the SEQ core) |
| `q7.intr_ctrl` `0x3028` | `en[31:0]` | `0x0` | enable, **4 bits per Q7 core** (8 × 4 = 32) |
| `q7.intr_info_0..7` `0x302C..0x3048` | `metadata[31:0]` | `0x0` | per-compute-core interrupt metadata (one word each) |

So the on-die model is **4 interrupt sources per core**, gated by `intr_ctrl`, with a
32-bit metadata word per core. A SoC INTC interrupt destined for this engine is
latched/encoded into `intr_info`, gated by the 4-source `intr_ctrl` enable. The firmware
**reads** `nx.intr_info` (CSR `0x001C`) at the top-level dispatcher boundary and branches
on its value — it does **not** vector. The dispatcher prologue, decoded instruction-exact:

```c
/* `handle_interrupt_` @ IRAM 0x4c5c  — the SEQ firmware's top-level SoC-IRQ dispatcher.
 * Symbol from the DEBUG build's own strings: "interrupt_handler.hpp" (DRAM 0x811db) +
 * "handle_interrupt_" (DRAM 0x81203). This is the ONLY `const16 …,0x001C` site in the
 * whole image (byte-scan = 1 hit @0x4c62) — a single read of the intr_info latch.
 *   [HIGH/OBSERVED — every instruction below is byte-exact from nx_iram.dis]            */

#define LOCAL_REG_BASE   0x00400000u                  /* movi a2,0x400 builds 0x400<<? → */
#define NX_INTR_INFO     (LOCAL_REG_BASE | 0x001Cu)   /* const16 a2,28  (28 = 0x1C)      */

void handle_interrupt_(void) {                        /* 4c5c: entry a1,48               */
    uint32_t info = *(volatile uint32_t *)NX_INTR_INFO;/* 4c5f movi a2,0x400 ; 4c62       */
                                                       /* const16 a2,28 ; 4c65 l32i.n     */

    if (info == 0) {                                   /* 4c6b: beqz.n a2,0x4c76          */
        return;                                        /*   → 0x4cbe retw  — IDLE         */
    }
    if (info == 1) {                                   /* 4c70: beqi a2,1,0x4c79          */
        enter_run_and_sunda_fsm();                     /* 4c79: call8 0x2c64  — RUN path  */
        hw_decode_fsm();                               /* 4c7c: call8 0x31ac              */
        /* 4c82: read nx.run_state (0x0008); beqi 2 → paused-handling                     */
        return;
    }
    /* info >= 2 : 4c73: j 0x4ca9 → ASSERT("handle_interrupt_", interrupt_handler.hpp:28)
     *             via 0xa304 → the FATAL ErrorHandler spin                               */
    fatal_assert("handle_interrupt_", /*line=*/28);
}
```

The RUN path drives the FSM, which polls the surprises word between ops (§5). So the
end-to-end binding is: **SoC INTC leaf → errtrig → `peb_intc` apex → [HW latch into the
per-core `intr_info` CSR, gated by the 4-source `intr_ctrl`] → firmware reads `intr_info`
at the dispatch boundary → run/idle/fatal → FSM polls surprises.** The async "interrupt"
sets a latch the dispatcher reads at a safe point; it does **not** enter an XEA3 dispatch
vector.

> **NOTE — the boot arm is a `wer`, not a mem-mapped store.** `setup_interrupts @0x2724`
> arms the engine's async surface at boot via a **`wer` (write external register)** of
> value `0x1000` (a single-bit enable, `1<<12`) to an external-register address literal —
> not a store to `nx.intr_ctrl` `0x0018`. The clean disasm has **no** mem-mapped store to
> `0x0018`; the core reaches its interrupt-enable through the external-register bus
> (consistent with the heavy `wer`/`rer` usage). `const16`-site census confirms the SEQ
> does **not** program the eight Q7-compute cores' per-core intr CSRs:
> `0x3028`(q7.intr_ctrl)=0, `0x302C`(q7.intr_info_0)=0, `0x0018`(nx.intr_ctrl)=1,
> `0x001C`(nx.intr_info)=1. The SEQ feeds the compute cores via EVT_SEM/run_state, not as
> their IRQ controller. `[the `wer` + `0x1000` value HIGH/OBSERVED; "this external reg IS
> the intr enable" MED/INFERRED.]`

> **INFERRED — the SoC-source → `intr_info` bit encoding.** Which apex/leaf bit lands in
> which of the 4 `intr_ctrl` sources / which `intr_info[31:0]` metadata bits is the
> HW/INTC side and is **not** register-traced in any shipped artifact reachable here. The
> leaf→apex chain is `[CARRIED · HIGH]` from the
> [PEB apex / physical-INTC](./peb-cc-topsp-triggers.md) pages; only the final
> apex-bit→`intr_info` encoding is uninstrumented. Mark this hop **INFERRED** in any
> reimplementation. `[MED · the gap is real and stated, not fabricated.]`

---

## 7. Masking and re-entry — `MS.DE`, not `INTENABLE`/`rsil` `[HIGH/MED · OBSERVED/INFERRED]`

A reimplementer needs the masking model. XEA3 has **no** `INTENABLE` register and **no**
`rsil` instruction (§1). The architectural masking primitives are:

- **`MS.DE`** (dispatch-enable, `MS[5]`) — the global "can a dispatch re-enter" gate.
  Hardware clears it on dispatch entry; the handler re-arms it on return. This is the
  XEA3 equivalent of XEA2's `PS.INTLEVEL` raise/lower, but binary (enabled/disabled)
  rather than a 3-bit level.
- **`CurrentPriority`** (8-bit arbitration state) — the priority *threshold*. The
  hardware arbiter only dispatches a pending request whose priority exceeds
  `CurrentPriority`; raising it masks lower-priority interrupts without disabling dispatch
  entirely. This is the fine-grained mask that replaces XEA2's level ladder.
- **`intr_ctrl.en`** (the custom CSR, §6) — the *per-source* enable, 4 bits per core.
  This gates which SoC sources are even allowed to set the `intr_info` latch. It is
  orthogonal to the XEA3 `MS.DE`/`CurrentPriority` masks: `intr_ctrl` is the on-die
  interrupt-block gate, `MS.DE`/`CurrentPriority` are the core's architectural masks.

In the shipped firmware, none of the `MS.DE`/`CurrentPriority` machinery is exercised on
the run path — the firmware is polled, so it never raises/lowers an architectural mask
and never re-enables a dispatch. The masking surface the firmware *does* touch is the
single boot `wer` arm (§6) and the per-source `intr_ctrl` gate. `[the architectural mask
semantics are HIGH/OBSERVED on the register existence + widths; that the firmware leaves
them unexercised is HIGH/OBSERVED from the census; the arbitration *threshold* behavior is
INFERRED from the state name/width — no firmware drives it.]`

---

## 8. The debug-interrupt surface is a separate path `[HIGH · CARRIED]`

XEA3 dispatch is **not** how the host debugger breaks the core. The
[Q7 OCD/TRAX/PMU aperture](../csr/xtensa-q7.md) (`xtensa_q7`, external APB/JTAG debug bus,
base `0x4000` in the local view / its own component bases) is a *separate* interface. Its
`DSR.DebugPend*`/`DebugInt*` "vectors" (host-forced `DCR.DebugInterrupt`, the BreakIn pin,
the TRAX PC-match `PTO`, the PMU overflow `PerfMonInt`) are the **debug-interrupt** surface
— driven from *outside* the core by the debugger — not the SoC-INTC async path.

The two surfaces are disjoint by design and do not collide:

| Surface | Driven by | Entry mechanism | Architectural reflection |
|---|---|---|---|
| XEA3 exception dispatch | the core itself (faults) | `excw` → single dispatch → `EXCCAUSE` demux | `EPC`/`MS`/`EXCCAUSE` SRs |
| Custom SoC-IRQ latch | the SoC INTC fabric | `intr_info` CSR latch → dispatcher *read* | `nx.intr_info` (not an SR) |
| OCD debug interrupt | the external debugger | `DCR.DebugInterrupt`/BreakIn/TRAX-PTO → `DSR.DebugPend*` | `InOCDMode` state; `rfdo` return |

The firmware's own `INS_BREAK`/`EXT_BREAK`/`STEP_CNT` *surprises* (decoded on the
[handler-bodies](./handler-bodies.md) and [surprises-irq](../../firmware/seq/surprises-irq.md)
pages) are the **in-core reflection** of breakpoint/step events armed via the `hw_decode`
breakpoint CSRs (`0x4004…`) — distinct again from both the architectural `IBREAK`/`DBREAK`
SRs and the external OCD aperture. `[HIGH · CARRIED — three-surface split from CSR-Q7 §7.1
and ISA-05 §5.4; do not conflate the OCD `0x2000` base with the SEQ `0x04004000`
breakpoint CSRs or with the XEA3 dispatch SRs.]`

---

## 9. Per-generation stability `[MED · CARRIED]`

- The **XEA3 register file** (the `xt_exception_dispatch` package, `MS`/`IEVEC`/`ISB`/
  `ISL`/`KSL`, the priority-arbitration states) is the `ncore2gp` Cayman architectural
  config; the `xtensa_q7`/`xtensa_nx` arch-header JSON is carried verbatim under the
  `mariana` / `mariana_plus` / `maverick` arch-header trees, so the same architecture
  applies across those packaged gens **[CARRIED]**; any v5/MAVERICK *silicon-level* claim
  beyond the identical packaged JSON is **INFERRED**.
- The **on-die `intr_ctrl`/`intr_info` CSR bundle** is part of `tpb_xt_local_reg`, dated
  2022-12-14 (the Cayman baseline); the "4 sources/core" + 8-core `intr_info` layout is the
  Cayman model. Cross-gen delta of this bundle is not separately re-verified here — **MED**
  that it is gen-stable.
- The **SoC INTC fabric** diverges at Maverick (decentralized per-IP-block INTCs +
  `iofic_x8_msix` security IOFIC + per-die apex), but that is the SoC-side fabric feeding
  the *management* core, **not** the GPSIMD-Q7 core-side dispatch model decoded here.
  `[CARRIED · INT-11/16.]`

---

## 10. Reimplementation checklist

To build a Vision-Q7-compatible exception unit, implement the **XEA3** model, not XEA2:

1. **Register file (HIGH):** single `EPC` (`0xb1`); `EXCCAUSE`(`0xe8`)+`EXCINF`;
   `EXCVADDR`(`0xee`); `MS`(`0xe5`) = `{DISPST[4:0], DE[5]}`; `IEVEC`(`0x74`) indirect
   vector; `VECBASE`(`0xe7`) `[31:6]`; `ISB`(`0xec`); `ISL`(`0xf8`); `KSL`(`0xf9`). Provide
   `rsr`/`wsr`/`xsr` for each (the `xt_exception_dispatch` package). **Do not** implement
   `EPC2-7`/`EPS2-7`/`INTENABLE`/`INTERRUPT`/`rsil`/`rfi`.
2. **Dispatch entry (HIGH regs / INFERRED flow):** on a fault or interrupt, save PC→`EPC`,
   cause→`EXCCAUSE`, vaddr→`EXCVADDR` (faults), set `MS.DISPST`, clear `MS.DE`, vector to
   the single `VECBASE`-relative dispatch entry (indirect via `IEVEC` for the table path).
3. **Demux in software (HIGH table):** the handler reads `EXCCAUSE` and dispatches via the
   `EXCCAUSE`→handler map (`NoException`/`LoadStoreError`/`TailchainException`/
   `L32ETailchainException`/…). Support **tail-chaining** as a dispatch cause.
4. **Return (HIGH):** restore `EPC`/`PS`, set `MS.DE` to re-arm. **No `rfi`, no level
   operand.**
5. **Masking (HIGH regs):** `MS.DE` (binary global mask) + `CurrentPriority` (8-bit
   threshold) + per-source `intr_ctrl.en`. No `INTENABLE` bitmap.
6. **Vector table (HIGH):** reset + one secondary vector + window spill/fill + a single
   `EXCVADDR`-save exception vector. **No** leveled-interrupt vector block.
7. **On-die IRQ surface (HIGH schema / INFERRED encoding):** 4 sources per core, gated by
   `intr_ctrl`, latched into a 32-bit `intr_info` metadata word; the firmware *reads* it
   at a dispatch boundary (it does not vector). Arm via the external-register bus, not a
   mem-mapped store.
8. **Debug surface (HIGH):** keep the OCD/TRAX/PMU debug-interrupt path
   (`DCR.DebugInterrupt`/BreakIn/PTO → `DSR.DebugPend*` → `InOCDMode`, `rfdo` return)
   **separate** from both the XEA3 dispatch and the SoC-IRQ latch.

---

## 11. Verification ledger

`HIGH / OBSERVED` (byte-read from the ISA config / CSR schema / firmware disasm):

- The XEA3 register-file census: every XEA2-only register (`EPC2-7`/`EPS2-7`/`INTENABLE`/
  `INTERRUPT`/`rsil`/`rfi`) is **absent** (0 in the `libisa-core.so` roster and 0 in both
  shipped firmware images); every XEA3-distinctive register (`MS` `0xe5`, `IEVEC` `0x74`,
  `ISB` `0xec`, `ISL` `0xf8`, `KSL` `0xf9`, `excw`, the `ActiveInterrupt`/`ActivePriority`
  arbitration states) is **present in the ISA config**. Of the five distinctive SRs,
  `MS`/`ISB`/`ISL` are additionally **observed as live `wsr`/`rsr` operands in the
  firmware** (`wsr.ms` @`0x01a4`, `wsr.isb` @`0x00a4`, `wsr.isl` @`0x1935`); `IEVEC`/`KSL`
  are ISA-present but firmware-unused; `EPC` itself is not an operand in either image (no
  dispatch is ever taken). Re-verified this session against the `ncore2gp` disasm.
- The `xt_exception_dispatch` package membership (24 rsr/wsr/xsr opcodes over 8 SRs;
  `opcodes[0..2]` = `excw`/`syscall`/`halt`) and the SR ids cross-checked against the
  encode templates (`rsr.ievec` = `0x00037400`, etc.).
- The `MS` `contents[]` map (`MS_DISPST[4:0]`, `MS_DE[5]`); the exception-state list
  (`VECBASE`/`EPC`/`EXCCAUSE`+`EXCINF`/`EXCVADDR`/`ISB`/`ISL`/`KSL`).
- The vector-table head (reset `0x0`→`0x1dc`; window spill/fill; `EXCVADDR`-save exception
  vector @`0x6c`; no leveled-vector block); the single `wsr.vecbase` boot site per image.
- The dispatcher `handle_interrupt_ @0x4c5c` reading `nx.intr_info` CSR `0x001C` (the only
  `0x001C` `const16` site, @`0x4c62`) and branching `0`=idle/`1`=run/`≥2`=FATAL; the
  `interrupt_handler.hpp` + `handle_interrupt_` source strings.
- The on-die IRQ CSR surface (`nx.intr_ctrl` `0x0018` `en[3:0]`, `nx.intr_info` `0x001C`,
  `q7.intr_ctrl` `0x3028`, `q7.intr_info_0..7` `0x302C..0x3048`) from `tpb_xt_local_reg.json`.
- The boot `wer` arm @`0x2724` (write `0x1000`); the `const16` census (`0x3028`=0,
  `0x302C`=0, `0x0018`=1, `0x001C`=1).

`MED / INFERRED`:

- The XEA3 dispatch **control flow** (entry/demux/return sequence) — reconstructed from the
  register set + the `EXCCAUSE`→name table; the firmware does not exercise it, so the
  step-by-step is INFERRED-strong, not decoded from a routine.
- The priority-arbitration **algorithm** (`Current/Active/Second` interaction) — INFERRED
  from state names + 8-bit widths; no firmware drives it.
- The SoC-source → `intr_info` **bit encoding** (HW/INTC side; leaf→apex CARRIED HIGH, the
  apex-bit→`intr_info` map not traced); the boot `wer` target = intr-enable.
- The `IEVEC`→`gserr` iclass **alias** interpretation (config-time naming artifact).

`CARRIED` (consolidated, not re-derived): the firmware polled-not-vectored decode + the
surprises poll/handler chain ([surprises-irq](../../firmware/seq/surprises-irq.md),
[q7-surprises-binding](./q7-surprises-binding.md), [handler-bodies](./handler-bodies.md));
the SoC leaf→apex cascade ([physical-intc](./physical-intc-instances.md),
[peb-cc-topsp](./peb-cc-topsp-triggers.md)); the OCD/TRAX/PMU debug aperture
([xtensa-q7](../csr/xtensa-q7.md)).

`OPEN / LOW`: the FLIX/IVP wide-bundle interiors (where stock objdump loses sync) — no
claim on this page depends on a FLIX interior; the apex-bit→management-core-vector hop
(inherited, NOT closed here — it concerns the *other* "Q7," the management core).

### Divergences recorded for the per-Part reconcile pass

1. **XEA2-vs-XEA3 label.** The [q7-surprises-binding](./q7-surprises-binding.md) page
   (§1) and the [surprises-irq](../../firmware/seq/surprises-irq.md) page (§1) call the
   config "single-level XEA2 exception model." This page corrects that to **XEA3** (§1
   CORRECTION). The *firmware conclusion* (polled, never vectored) is unchanged on all
   three pages; only the architecture **name** diverges. Reconcile by updating the two
   firmware pages' "XEA2" wording to "XEA3 (single-dispatch)" and pointing them here.

---

### Cross-references

- [INTC → Q7 firmware "surprises" binding](./q7-surprises-binding.md) — the firmware-side
  terminal hop (SoC IRQ → `intr_info` → poll).
- [The interrupt/exception handler bodies](./handler-bodies.md) — the actual handler code
  the dispatch lands on.
- [SEQ surprises / IRQ poll (firmware)](../../firmware/seq/surprises-irq.md) — the
  poll/check/handle chain instruction-exact.
- [CSR — Xtensa Q7 debug / OCD / TRAX / PMU](../csr/xtensa-q7.md) — the separate external
  debug-interrupt surface.
- [CSR — `tpb_xt_local_reg`](../csr/tpb-xt-local-reg.md) — the on-die `intr_ctrl`/
  `intr_info` IRQ latch.
