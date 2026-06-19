# SEQ Surprises / IRQ Poll

This page reconstructs the NX **sequencer (SEQ) engine**'s asynchronous-event
mechanism — the firmware calls it *"surprises"* — instruction-exact. A *surprise* is a
host/debugger-armed control event (instruction breakpoint, external breakpoint,
single-step countdown, ordering promotion) that the running sequencer must notice and
react to **between** microcode ops. This page owns the three functions of
`surprises.hpp` — the **poll** (`0x6af4`), the **check** (`0x6b0c`), and the bit-mask
**handler** (`0x6ce0`/`0x6cf4`) — plus the halt helper and the fatal-assert tail; how
the poll wires into the [main FSM loop](main-loop.md); the `STRONG_ORDER` tie to the
[memory-ordering model](../../uarch/atomics-ordering.md); and the framing of how a SoC
INTC interrupt physically reaches this firmware. This is **the SEQ engine's own
firmware** — windowed-ABI Xtensa on the `ncore2gp` "Cairo" core — *not* the microcode it
interprets.

Everything below is **byte-pinned to a shipped artifact this session**. The anchor image
is the carved `CAYMAN_NX_POOL_DEBUG` device firmware extracted from `libnrtucode.a`
(member `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` for code,
`…_DRAM_contents.c.o` for strings), disassembled with the native `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`, Cairo µarch, Xtensa24, RI-2022.9, `TargetHWVersion=NX1.1.4`)
that ships inside the gpsimd-tools package. The carve reproduces the sibling anchor
exactly: `nx_iram.bin` = **116,768 B** sha256 `8e4412b9…`, `nx_dram.bin` = **28,448 B**
sha256 `7bdf6ed7…`, disassembly **45,901 lines, exit 0**. The DEBUG build **keeps** the
`'S:'` log strings the recovery hinges on; the PERF build strips them and re-lays-out
code, so PERF offsets do *not* map 1:1 onto the addresses here. Where a sibling note or
the prompt-level report disagrees with the disassembly, **the binary wins**, and an
in-place **CORRECTION** says so.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string/JSON-field read from a shipped image this session; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor;
crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading),
**NOTE** (orientation).

> **NOTE — addressing convention.** All code addresses are **IRAM-image offsets == device
> IRAM VA** (`ResetVectorOffset=0`, InstRAM base `0x0`). DRAM string offsets are the low-16
> value built by `const16` (full DRAM VA = `0x80000 | value`, file offset = value). CSR /
> aperture addresses are the absolute `0x04000000`-windowed device-local addresses the
> instruction stream actually loads/stores (the CAYMAN-family `movi a,0x400 ; const16 a,OFF`
> idiom = `0x04000000 | OFF`). `.text`/`.rodata` are VMA==file-offset.

---

## 0. The mechanism in one diagram

The surprises subsystem is the **polled** async-control path: there is **no hardware
interrupt vector into the SEQ firmware** for it. Every iteration of both FSM loops calls
the poll first; only the Sunda-software-fetch loop additionally calls the check, which
computes a 2-byte *surprise word* and dispatches it through a fixed-order bit-mask chain.

```
   ┌──────────── per-iteration, at the TOP of BOTH FSM loops ────────────┐
   │  STEP 1  poll_surprises @0x6af4  → reads SW running-flag 0x85644.0   │
   │            flag==0  ─►  EXIT the loop (graceful end-of-program)      │
   │            flag!=0  ─►  continue                                     │
   │  STEP 2  (Sunda FSM only) sunda_check_surprises @0x6b0c              │
   │            ├─ compute surprise WORD (FLIX wide bundle @0x6b13)       │
   │            ├─ LOG "S: sunda_check_surprises: any=%d, surprises=0x%x" │
   │            ├─ sunda_handle_surprises(word) @0x6ce0 ─────────┐        │
   │            └─ fold handler's bool into loop-continue flag   │        │
   └────────────────────────────────────────────────────────────┼────────┘
                                                                 ▼
   ┌──────── sunda_handle_surprises body @0x6cf4 (linear bit chain) ──────┐
   │  result := 1 (handled)                                              │
   │  bit9 STRONG_ORDER → memw + reorder(0x3a84); result=0; ── FALL THRU ─┤ non-terminal
   │  bit2 INS_BREAK    → Setup-Halt(reason=4)  ─────────────── EXIT ─────┤ terminal
   │  bit3 EXT_BREAK    → read+clear 0x04000014; Setup-Halt(8) ─ EXIT ────┤ terminal
   │  bit8 STEP_CNT     → cnt@0x85650-- ; ==0 → Setup-Halt(64) ─ EXIT ────┤ terminal@0
   │  (else)            → re-test bit9; unset → "Unhandled surprise" FATAL │ hard fault
   └─────────────────────────────────┬───────────────────────────────────┘
                                      ▼
            Setup-Halt helper 0x6e48 → 0x1cf8 (the run-state FSM's Setup-Halt)
            FATAL assert wrapper 0xa304 → 0x13e00 (the FW-09 error/halt raise)
```

One-line verdict: surprises are a **debug/step/ordering** control set, host/debugger-armed
and **cooperatively polled** between ops. They are physically and architecturally distinct
from the SoC-INTC hardware interrupt surface (§7) and from the data-plane event/notific
queues (§5.2); the handler touches **no** interrupt CSR.

---

## 1. Carve + disasm provenance (re-verified this session)

`[HIGH/OBSERVED]` The exact carve and hash that every sibling SEQ page shares:

```text
ar x libnrtucode.a img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o
objcopy -O binary --only-section=.rodata  …            # fw embedded in host wrapper's .rodata
  nx_iram.bin  116768 B  sha256 8e4412b99201f62d9711…   # == run-state / main-loop / error-handler
  nx_dram.bin   28448 B  sha256 7bdf6ed7ccd27b376a65…
xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x0    # XTENSA_CORE=ncore2gp, IsaMaxInstructionSize=32 (FLIX)
  nx_iram.dis  45901 lines  exit 0
```

The three functions of `surprises.hpp` live in one packed IRAM region:

| symbol (recovered) | addr | role |
|---|---|---|
| `poll_surprises` | `0x6af4` | FSM STEP-1 gate — reads the SW running-flag |
| `sunda_check_surprises` | `0x6b0c` | compute surprise word → log → dispatch → fold result |
| `sunda_handle_surprises` (wrapper) | `0x6ce0` | thin arg-shuffle wrapper |
| `sunda_handle_surprises` (body) | `0x6cf4` | the fixed-order bit-mask dispatcher |
| Setup-Halt helper | `0x6e48` | tail-call into the run-state Setup-Halt `0x1cf8` |

The function name `"sunda_handle_surprises"` (DRAM `0x1a2b`) and the source file
`"surprises.hpp"` (DRAM `0x1930`) are recovered out of the assert-arg literals at `0x6d7e`
/ `0x6d84` and `0x6e20` / `0x6e26`. They are **firmware `.rodata`** read as data — the
DEBUG build bakes the `__FILE__`/function strings into the image. `[HIGH/OBSERVED]`

---

## 2. The poll — `poll_surprises @0x6af4` (FSM STEP 1)

The poll is a cheap "is the engine still running / is there async work to check?" gate. It
reads **bit 0** of the per-engine SW **running-flag** byte at DRAM `0x00085644`
(`state[0x855e0 + 100]`) — the *same* flag the [run-state FSM](run-state.md) sets to `1`
at `enter_run` and clears to `0` at Enter-Pause / Setup-Halt.

```c
// poll_surprises @0x6af4  (CAYMAN POOL DEBUG; IRAM offsets == device VA). [HIGH/OBSERVED]
bool poll_surprises(void) {
    void *flag = (void*)0x00085644;       // 6af7: const16 a2,8 ; 6afa: const16 a2,0x55e0 ; 6afd: addi a2,a2,100
    // 6b00: s32i.n a2,[a1+12] ; 6b02: l32i.n a2,[a1+12]   (spill/reload of the flag pointer)
    u8 f = *(u8*)flag;                     // 6b04: l8ui  a2,[a2]
    return f & 1;                          // 6b07: extui a2,a2,0,1 ; 6b0a: retw.n   → bool in a2/a10
}
```

`0x855e0 + 100 = 0x85644` is byte-confirmed from `const16 a2,8 ; const16 a2,0x55e0`
(materialises `0x000855e0`) `+ addi 100`. The return is bit-0 of that running-flag byte.
`[HIGH/OBSERVED — matches the run-state flag site exactly.]`

> **GOTCHA — "poll_surprises" reads the *running-flag*, not a surprise/IRQ register.** The
> name is misleading: `0x6af4` resolves to an `l8ui` of `state[0x855e0+100]` masked to
> bit 0 — the loop's *"is the engine still in the RUNNING state?"* test, **not** a read of
> the surprise word. The actual surprise word is computed one step later, inside
> `sunda_check_surprises` (§3). A reimplementer must wire `poll_surprises` as the
> loop-continuation gate and **not** mistake it for the event-fetch. `[HIGH/OBSERVED]`

### 2.1 The two call sites — both FSM loops, STEP 1

`poll_surprises` is the **first action of every FSM iteration** in both the
Sunda-software-decode loop and the HW-decode loop. There is no async vector; the poll is a
synchronous between-ops gate.

```asm
; SUNDA-decode FSM @0x2d81  (after the "S: Sunda seq Loop" log @0x2d73, DRAM 0x193e)
  2d81:  call8 0x6af4              ; *** POLL (STEP 1)
  2d84:  beqz  a10, 0x31a6         ; flag==0 -> EXIT loop to 0x31a6 -> 0x31a9 retw.n (end-of-program)
  2d8f:  const16 a2,0x5068         ; a2 = DRAM ctx ptr 0x00085068 (per-engine context struct)
  2d92:  s32i a2,[a1+36] ; l32i a2,[a1+36] ; mov.n a10,a2     ; stage the ctx ptr as the arg
  2d9a:  call8 0x6b0c              ; *** CHECK (STEP 2, only Sunda mode)
  2d9d:  bnez.n a10, 0x2daa        ; handled? -> 0x2daa ; else stop-flag path

; HW-decode FSM @0x31c0  (inside the "Seq Loop, iter" function, called from the dispatcher)
  31c0:  call8 0x6af4              ; *** POLL (STEP 1)
  31c3:  bnez.n a10, 0x31c8        ; flag set -> continue ; else
  31c5:  j 0x3a3c                  ;            -> 0x3a3f retw.n (EXIT)
```

`[HIGH/OBSERVED]` Both call sites are byte-exact. The Sunda FSM does **poll + check**; the
HW-decode FSM does **poll only** — its breakpoint/single-step machinery is handled in
hardware decode, so it does not invoke the software check (§3). This is the integration
the [main loop](main-loop.md) describes at its STEP 1.

> **CORRECTION — `0x6b0c` is `sunda_check_surprises`, not `pending_redirect`/`sunda_fetch`.**
> Sibling pages ([main-loop.md](main-loop.md) §4, [fetch-pc-redirect.md](fetch-pc-redirect.md),
> [run-state.md](run-state.md) §11) label the `0x6b0c` call as a *"pending-redirect /
> sunda_fetch"* helper. Re-disassembled this session, `0x6b0c` logs DRAM `0x819cc` =
> `"S: sunda_check_surprises: any=%d, surprises=0x%x"`, computes the surprise word, and tail-calls
> the bit-mask handler `0x6ce0` — it **is** the surprise check, in `surprises.hpp`, not a fetch
> redirect. The `0x2d9a` Sunda STEP-2 call is the surprise check; the fetch/PC-redirect work
> happens later in the loop body (`0x2db3`/`0x2df5` onward). **The binary wins:** treat
> `0x6b0c` as the surprise check. The two functions are adjacent in the same packed region,
> which is the likely source of the mislabel. `[HIGH/OBSERVED — log string + handler call
> byte-exact.]`

---

## 3. The check — `sunda_check_surprises @0x6b0c`

The check computes the surprise word, logs it, dispatches to the handler, and folds the
handler's "handled" bool into the loop-continue flag. Its one caller is the Sunda FSM STEP 2
(`0x2d9a`), which passes the per-engine context pointer `0x00085068` in `a10`.

```c
// sunda_check_surprises @0x6b0c. [HIGH/OBSERVED except the FLIX word-source, §6]
bool sunda_check_surprises(ctx_t *ctx /*a2 = 0x00085068*/) {
    // 6b0f: s32i.n a2,[a1+16] ; 6b11: l32i.n a2,[a1+16]      save/reload the ctx ptr
    // 6b13: << FLIX WIDE BUNDLE — the SURPRISE WORD is computed here from the context
    //          struct and written to frame [a1+12]. The stock ncore2gp objdump cannot
    //          decode this op0=0x7f bundle and mis-prints a bogus `j 0x216ea` (§6). >>
    u8  any  = ctx_any_bit & 1;            // 6b1c: extui a3,a4,0,1 ; 6b1f: s8i a3,[a1+20]
    // 6b25: l8ui a3,[a1+20] ; 6b28: extui a11,a3,0,1          a11 = "any"
    u16 word = *(u16*)&frame[a1+12];        // 6b2b: l32i.n a12,[a1+12]   the SURPRISE WORD
    log("S: sunda_check_surprises: any=%d, surprises=0x%x",  // 6b2d: const16 a10,0x19cc ; 6b33: call8 0x18b84
        /*a11=*/any, /*a12=*/word);
    u8 handled = sunda_handle_surprises(word); // 6b4f: or a10,a2,a2 ; 6b52: call8 0x6ce0
    // 6b55: s8i a10,[a1+28]                                   capture handler result
    // 6b58: l8ui a2,[a1+28] ; movi.n a3,1 ; and a2,a3,a2 ; xor a2,a3,a2 ; s8i a2,[a1+24]
    //        fold = ~handled & 1  → "continue?" flag (0 when a halting arm fired)
    return frame[a1+24];                    // 6b69: l8ui a2,[a1+24] ; 6b6c: retw.n
}
```

The log format string at DRAM `0x819cc` is byte-confirmed:
`"S: sunda_check_surprises: any=%d, surprises=0x%x"`. The handler call target `0x6ce0` and
the `and/xor` fold that inverts the handler's `handled` bool into the loop-continue flag are
exact. `[HIGH/OBSERVED]`

> **NOTE — the fold semantics.** The handler returns `1` for a **handled-and-halting** arm
> (INS_BREAK / EXT_BREAK / STEP_CNT-at-zero) and `0` for a **consumed-but-continue** arm
> (STRONG_ORDER, STEP_CNT-nonzero). The `xor a2,a3(=1),a2` at `0x6b60` inverts that into the
> loop-continue flag: `result=1` (halting) → continue-flag `0`; `result=0` (continue) →
> continue-flag `1`. The check's return is the **software-level consume signal** the FSM uses
> to decide whether to keep running. `[HIGH/OBSERVED]`

---

## 4. The handler — `sunda_handle_surprises @0x6ce0` / `0x6cf4`

### 4.1 The wrapper `0x6ce0` (thin)

```c
// sunda_handle_surprises wrapper @0x6ce0. [HIGH/OBSERVED]
u8 sunda_handle_surprises(u16 word) {
    // 6ce3: s32i a2,[a1+12] ; 6ce6: s32i a3,[a1+8] ; 6ce9: l32i.n a2,[a1+12] ;
    // 6ceb: l32i.n a10,[a1+8] ; 6ced: call8 0x6cf4 ; 6cf0: mov.n a2,a10 ; 6cf2: retw.n
    return body_0x6cf4(word);
}
```

### 4.2 The body `0x6cf4` — fixed-order linear bit-test chain

The body tests a **2-byte word** held at the handler frame: `[a1+8]` = **low byte** (word
bits 0–7), `[a1+9]` = **high byte** (word bits 8–15). The default result is `1` (handled).
The four named arms are tested in a fixed linear order; an unrecognised word is fatal.

```c
// sunda_handle_surprises body @0x6cf4. [HIGH/OBSERVED — every bbci/bbsi byte-exact,
//   several after manual FLIX re-sync past each printf-return.]
u8 body(u16 word /*lo=[a1+8], hi=[a1+9]*/) {
    u8 result = 1;                              // 6cf7: s32i.n a2,[a1+8] ; 6cf9: movi.n a2,1 ; 6cfb: s8i a2,[a1+12]

    // ARM 1 — STRONG_ORDER  (word bit 9 = byte9 bit1)  — NON-TERMINAL
    if (word & (1<<9)) {                        // 6cfe: l8ui a2,[a1+9] ; 6d01: bbci a2,1,0x6d28 (clear→skip)
        log("S: STRONG_ORDER");                  // 6d0a: const16 a10,0x19fe ; 6d10: call8 0x18b84
        __memw();                                // 6d18: memw
        reprogram_ordering();                    // 6d1b: call8 0x3a84   (-> movi a2,1 ; call8 0x3b00)
        result = 0;                              // 6d1e: movi.n a2,0 ; 6d20: s8i a2,[a1+12]   (consumed, non-fatal)
    }                                            // 6d23: j 0x6d28   *** FALL THROUGH to ARM 2 ***

    // ARM 2 — INS_BREAK  (word bit 2 = byte8 bit2)  — TERMINAL
    if (word & (1<<2)) {                        // 6d28: l8ui a2,[a1+8] ; 6d2b: bbci a2,2,0x6d51
        log("S: INS_BREAK");                      // 6d37: const16 a10,0x1a0f ; 6d3a: call8 0x18b84
        setup_halt_helper(/*reason=*/4);         // 6d42: movi a10,4 ; 6d45: call8 0x6e48
        result = 1;                              // 6d48: movi a2,1 ; 6d4b: s8i a2,[a1+12]
        goto ret;                                // 6d4e: j 0x6e3e   *** EXIT ***
    }

    // ARM 3 — EXT_BREAK  (word bit 3 = byte8 bit3)  — TERMINAL
    if (word & (1<<3)) {                        // 6d51: l8ui a2,[a1+8] ; 6d54: bbci a2,3,0x6dab
        log("S: EXT_BREAK");                      // 6d60: const16 a10,0x1a1d ; 6d63: call8 0x18b84
        u32 hc = *(u32*)0x04000014;              // 6d6e: movi a2,0x400 ; 6d71: const16 a2,20 ; 6d74: l32i.n  == nx.instr_halt_ctrl
        if (hc == 0)                             // 6d76: bnez.n a2,0x6d8d
            ASSERT_FAIL("sunda_handle_surprises", "surprises.hpp", 426); // 6d8a: call8 0xa304  (UNARMED → FATAL)
        *(u32*)0x04000014 = 0;                   // 6d92: movi a2,0x400 ; const16 a2,20 ; 6d98: movi a3,0 ; 6d9b: s32i  *** ACK ***
        setup_halt_helper(/*reason=*/8);         // 6d9e: movi.n a10,8 ; 6da0: call8 0x6e48
        result = 1;                              // 6da3: movi.n a2,1 ; 6da5: s8i a2,[a1+12]
        goto ret;                                // 6da8: j 0x6e3b   *** EXIT ***
    }

    // ARM 4 — STEP_CNT  (word bit 8 = byte9 bit0)  — single-step countdown
    if (word & (1<<8)) {                        // 6dab: l8ui a2,[a1+9] ; 6dae: bbci a2,0,0x6e11
        u32 cnt = *(u32*)0x00085650;             // 6db4: const16 a2,8 ; const16 a2,0x5650 ; 6dba: l32i.n
        if (cnt == 0) {                          // 6dbc: bnez.n a2,0x6de1
            log("S: STEP_CNT=0");                 // 6dc7: const16 a10,0x1a42 ; 6dca: call8 0x18b84
            setup_halt_helper(/*reason=*/64);    // 6dd2: movi a10,64 ; 6dd5: call8 0x6e48
            result = 1;                          // 6dd8: movi a2,1 ; 6ddb: s8i a2,[a1+12]
            goto ret;                            // 6dde: j 0x6e0e -> 0x6e38 (exit)
        } else {
            log("S: STEP_CNT=>%d", cnt);          // 6def: const16 a10,0x1a51 ; 6df2: call8 0x18b84
            *(u32*)0x00085650 = cnt - 1;         // 6dfd: const16 a2,0x5650 ; 6e00: l32i.n a3 ; 6e02: addi a3,-1 ; 6e04: s32i.n  *** DECREMENT ***
            result = 0;                          // 6e06: movi.n a2,0 ; 6e08: s8i a2,[a1+12]   (consumed, continue)
            goto ret;                            // 6e0b: j 0x6e0e -> 0x6e38 (exit)
        }
    }

    // ARM 5 — UNHANDLED / no recognised terminal bit
    if (!(word & (1<<9)))                        // 6e11: l8ui a2,[a1+9] ; 6e14: bbsi a2,1,0x6e35  (bit9 SET → skip assert)
        ASSERT_FAIL("sunda_handle_surprises", "surprises.hpp", 441); // 6e32: call8 0xa304  *** FATAL ***
ret:
    return result;                              // 6e3e: l8ui a2,[a1+12] ; 6e41: extui a2,a2,0,1 ; 6e44: retw.n
}
```

`[HIGH/OBSERVED]` Every `bbci`/`bbsi` arm test, every `const16` log address, the
`0x04000014` read+clear, both assert line numbers, and the three Setup-Halt reason codes are
instruction-exact, several recovered after manually re-syncing past a FLIX desync at each
`call8 0x18b84` return.

| word bit | frame | name | reaction (byte-exact) | terminal? |
|---|---|---|---|---|
| bit 9 | `byte9.1` | **STRONG_ORDER** | `memw` + `reprogram_ordering(0x3a84)`; result=0, **continue** | no |
| bit 2 | `byte8.2` | **INS_BREAK** | `Setup-Halt(reason=4)` → HALT | yes |
| bit 3 | `byte8.3` | **EXT_BREAK** | read+clear `instr_halt_ctrl(0x04000014)`; `Setup-Halt(8)`; assert :426 if unarmed | yes |
| bit 8 | `byte9.0` | **STEP_CNT** | decrement counter `@0x85650`; `Setup-Halt(64)` at 0, else continue | at cnt==0 |
| (none) | — | UNHANDLED | `"Unhandled surprise"` → assert :441 → FW-09 FATAL | hard fault |

> **QUIRK — `STRONG_ORDER` is the only non-terminal arm, and it is tested FIRST.** The chain
> is a fixed linear order `bit9 → bit2 → bit3 → bit8 → assert`, *not* a priority encoder. Of
> the four arms, only `STRONG_ORDER` (bit 9) falls through (`6d23: j 0x6d28`) instead of
> returning — so a word that sets `STRONG_ORDER` **and** a breakpoint bit re-programs ordering
> **and then** still takes the breakpoint, in the same call. The three halting arms are
> first-match-wins and mutually exclusive: among them the priority is INS_BREAK > EXT_BREAK >
> STEP_CNT, because the first to match halts the engine and exits before the later tests run.
> `[HIGH/OBSERVED — STRONG_ORDER `j 0x6d28`; INS_BREAK `j 0x6e3e`; EXT_BREAK `j 0x6e3b`.]`

> **GOTCHA — ARM 5 re-tests bit 9 as the "did we already handle something?" guard.** The
> unhandled-fallthrough at `0x6e11` does `bbsi a2,1,0x6e35` — if bit 9 (STRONG_ORDER) was set,
> it **skips** the fatal assert (the word *was* handled, benignly). Only a word with **no**
> recognised bit at all reaches the `"Unhandled surprise: surprises=0x%x"` (DRAM `0x81e52`)
> FATAL. A naive reading that treats ARM 5 as an unconditional assert is wrong: a pure
> `STRONG_ORDER` word is handled and returns cleanly. `[HIGH/OBSERVED]`

### 4.3 The Setup-Halt helper `0x6e48`

All three halting arms route through one helper, which tail-calls the run-state FSM's
Setup-Halt:

```c
// setup_halt_helper @0x6e48 — shared by INS_BREAK / EXT_BREAK / STEP_CNT. [HIGH/OBSERVED]
void setup_halt_helper(u32 reason /*a10*/) {
    // 6e4b: s32i a2,[a1+12] ; 6e4e: l32i a11,[a1+12]    a11 = reason
    void *flag = (void*)0x00085644;            // 6e51: const16 a2,8 ; 6e54: const16 a2,0x55e0 ; 6e57: addi a10,a2,100
    setup_halt(flag, reason);                  // 6e5a: call8 0x1cf8   *** the run-state Setup-Halt ***
}                                              // 6e5d: retw.n
```

`0x1cf8` is byte-identically the [run-state FSM](run-state.md) §4b **Setup-Halt**: it saves
the resume PC into `lr[3]`/`lr[4]` (`0x04001060`/`0x04001080`), clears the SW running-flag,
and stores `halt_reason → flag+4`. So a debug surprise halts through the *same* path a
synchronous Pause/Halt op uses; the surprise only supplies the **reason code**: `4`=INS_BREAK,
`8`=EXT_BREAK, `64`=STEP_CNT-exhausted. `[HIGH/OBSERVED — `0x1cf8` == the run-state Setup-Halt;
reasons byte-exact.]`

### 4.4 The fatal-assert tail `0xa304 → 0x13e00`

```c
// assert wrapper 0xa304. [HIGH/OBSERVED]
void ASSERT_FAIL(const char *fn, const char *file, u32 line) {
    log("S: Assertion failure! %s(%s:%u)", fn, file, line); // 0xa31b: const16 a10,0x2210 ; 0xa31e: call8 0x18b84
    // 0xa332: call8 0x1405c (record)  ;  0xa337: call8 0x13e00   *** FW-09 FATAL raise (sev=2) ***
}
```

The assert format string at DRAM `0x82210` is `"S: Assertion failure! %s(%s:%u)"`. An
**unhandled surprise** (line 441) or an **unarmed EXT_BREAK** (line 426) is therefore a hard
fault routed straight into the [error-handler](error-handler.md) FW-09 FATAL raise `0x13e00`,
which spins forever after Setup-Halt(reason=16) + Entering-HALT. `[HIGH/OBSERVED — `0x13e00`
== the FW-09 raise.]`

---

## 5. The surprise-type taxonomy and its place among the async surfaces

### 5.1 The four arms mapped to their source class

| word bit | name | source class (who arms it) | arming surface |
|---|---|---|---|
| bit 9 | STRONG_ORDER | firmware/host control (ordering mode) | host/debugger sets the bit; reprogrammed via `0x3a84` |
| bit 2 | INS_BREAK | debugger (instruction breakpoint) | hw_decode breakpoint CSR family (`[MED/INFERRED]`, §5.3) |
| bit 3 | EXT_BREAK | debugger (external/host breakpoint) | host writes `instr_halt_ctrl(0x04000014)` (`[HIGH/OBSERVED]`) |
| bit 8 | STEP_CNT | debugger (single-step countdown) | hw_decode step CSR + counter `@0x85650` (`[MED/INFERRED]`) |

### 5.2 Three distinct async surfaces — surprises are not the others

`[HIGH/OBSERVED + CARRIED]` The GPSIMD SEQ engine has **three non-overlapping** asynchronous
surfaces; surprises are only one of them:

1. **Surprises** (this page) — host/debugger-armed **debug/step/ordering** control, polled
   between ops. The handler touches **no** interrupt CSR.
2. **The dispatcher `intr_info` latch** — `nx.intr_info(0x0400001C)` is the **SoC-INTC
   terminus**, read once at a dispatch boundary by the top dispatcher
   (`0`=idle / `1`=run / `>=2`=fault; [run-state.md](run-state.md) §6, [main-loop.md](main-loop.md)
   §7). This is the hardware interrupt surface (§7), a coarser, separate layer.
3. **Data-plane event/notific** — the event/semaphore array and the notific-queue CSRs are a
   third polled surface for data-plane signalling; neither is a surprise bit and neither is in
   `surprises.hpp`.

A surprise bit is **not** a SoC-INTC bit. This is byte-confirmed by what the handler does
*not* touch — see §6 (ACK) for the const16-scan proof.

> **NOTE — do not cross-wire surprises with `intr_info`.** The names invite confusion
> ("IRQ poll"), but the surprises path never reads `intr_info(0x001C)` or `intr_ctrl(0x0018)`.
> The poll reads the running-flag (§2); the only CSR the whole `surprises.hpp` touches is
> `instr_halt_ctrl(0x0014)`, and only in the EXT_BREAK arm. `[HIGH/OBSERVED]`

### 5.3 The arming side (interrupt-driven vs polled determination)

The firmware **never** receives a surprise via a hardware interrupt vector — it is delivered
purely by the cooperative poll (§2). The "interrupt-driven-vs-polled determination" is
therefore not a runtime config gate in this path; it is **architecturally fixed to polled**:

- `poll_surprises` reads a DRAM running-flag, not an interrupt-status register; both call sites
  invoke it as the synchronous top-of-loop continuation gate (§2.1).
- The handler issues **no** interrupt-ack, reads **no** `intr_info`/`intr_ctrl`, and installs
  **no** vector (§6).
- The **only** in-handler CSR read is `instr_halt_ctrl(0x04000014)` in EXT_BREAK — and even
  that is read **synchronously when the bit fires**, not via an ISR.

For the **arming** of the conditions whose firing the surprise word encodes: only EXT_BREAK's
arming CSR is read in-handler (the host writes `instr_halt_ctrl`; the handler reads+clears it,
§4.2). INS_BREAK and STEP_CNT are armed through the hw_decode breakpoint/step CSR family
(`enter_run` reads those at `0x5504`/`0x5514`, [run-state.md](run-state.md) §3a;
[uarch-debugger.md](uarch-debugger.md) scope) — **not** read in the surprises handler, so the
exact arm-CSR for those two is `[MED/INFERRED]`. The host-side *write* sequence that arms each
surprise does not appear in these firmware images (`[MED/INFERRED]`).

---

## 6. ACK + ordering: how a surprise self-clears

`[HIGH/OBSERVED]` Surprises are **not** acked through `intr_info(0x001C)`/`intr_ctrl(0x0018)`
— those belong to the dispatcher latch. The ack is **per-arm and in-band**:

| arm | ack mechanism | site |
|---|---|---|
| EXT_BREAK | explicit CSR ack: `s32i a3(=0),[0x04000014]` clears `instr_halt_ctrl` before halting | `0x6d92`/`0x6d9b` |
| STEP_CNT | the DRAM counter `@0x85650` is the latch; each surprise **decrements** it; at 0 it self-clears by halting | `0x6e04` |
| STRONG_ORDER | consumed by re-programming the ordering descriptor (`call8 0x3a84`) and returning result=0 | `0x6d1b` |
| (all) | the `handled` bool is folded into the FSM continue flag by the check (§3) — the software consume signal | `0x6b58` |

There is **no** write to a generic interrupt-ack CSR anywhere in the surprises path. A
`const16` scan of the whole span `0x6af4–0x6e60` finds **zero** references to `0x001C`
(intr_info) or `0x0018` (intr_ctrl); the **only** CSR-window access is a single
`movi a,0x400 ; const16 a,20` → `0x04000014` (instr_halt_ctrl) used by EXT_BREAK to read
(`0x6d6e`) and ack (`0x6d92`). `[HIGH/OBSERVED — span const16-scan: 0x1C/0x18 absent; only 0x14
present.]`

---

## 7. The STRONG_ORDER tie + the terminal-hop framing

### 7.1 STRONG_ORDER ↔ the memory-ordering model

`STRONG_ORDER` (bit 9) is where the surprise path reaches into the
[atomic + memory-ordering model](../../uarch/atomics-ordering.md). It is **not** an
instruction and **not** a kernel fence — it is the **host/debugger-armed ordering-promotion
lever** the SEQ management core handles. When the host sets bit 9, the handler issues
`memw` (the heavy full-memory barrier — `0x0020c0`, [§4.1 of the ordering page](../../uarch/atomics-ordering.md))
and calls `reprogram_ordering(0x3a84)`, which re-programs the core's ordering/strength policy
— the same machinery `ATOMCTL` and the fences expose at the ISA level (e.g. forcing
strongly-ordered AXI attributes / disabling write-buffer reordering for a bring-up window) —
then **continues** execution.

The ordering page's `STRONG_ORDER` section (§7 there) is the canonical home; this page anchors
the byte-exact dispatch site. Two consistency points to keep aligned:

- **STRONG_ORDER is non-terminal (continue), bit 9** — matches the ordering page's table
  exactly. `[HIGH/OBSERVED here + CARRIED.]`
- The `memw` at `0x6d18` is the genuine full-memory barrier; the `reprogram_ordering` body
  (`0x3a84 → movi a2,1 ; call8 0x3b00`) sets the promoted policy. That "re-program ordering =
  strong-ordering AXI/write-buffer policy promotion" reading is `[INFERRED]`, consistent with
  the ordering page's same `[INFERRED]` tag — the firmware emits the `memw` + helper call
  byte-exactly, but the helper's exact policy write is not decoded here.

> **NOTE — STRONG_ORDER uses `memw`, not the exclusives' `XTSYNC`.** The ordering tie here is a
> plain `MEMW` full barrier plus a policy-reprogram call. It is **not** an `L32EX`/`S32EX`
> exclusive and does **not** assert the `XTSYNC` register-pipeline barrier; consistent with the
> ordering page, the `XTSYNC@N` tags on the fences are scheduler USE-stage annotations, not
> realised barriers on this path. `[HIGH/OBSERVED `memw`; CARRIED on the XTSYNC distinction.]`

### 7.2 The terminal hop — how a SoC INTC interrupt reaches this firmware

The surprises subsystem is polled, so the *surprise word itself* never arrives via a hardware
vector. But the broader question — how a **SoC INTC** interrupt physically reaches this Q7
firmware — has a concrete answer that this page frames and forward-links:

- A SoC INTC source is **latched into `nx.intr_info(0x0400001C)`** and read at the dispatcher
  boundary (`0`=idle / `1`=run / `>=2`=fault). That latch is the **terminal hop**: the binding
  point where the hardware interrupt surface meets the Q7 firmware. It is a **separate, coarser
  layer** from the per-iteration surprises poll — two layers, not one
  ([run-state.md](run-state.md) §6, [main-loop.md](main-loop.md) §7). `[HIGH/OBSERVED on the
  `intr_info` read; the SoC-INTC → `intr_info` wiring is the forward-linked Part-13 subject.]`
- The full physical binding (which SoC INTC lines map to the Q7, the apex/leaf interrupt-source
  taxonomy, and the latch wiring) is documented separately:

> **NOTE — forward-link (planned).** The SoC-INTC → Q7-firmware binding lives at
> [`../../control/interrupt/q7-surprises-binding.md`](../../control/interrupt/q7-surprises-binding.md)
> (Part 13, *"INTC → Q7 Firmware Surprises Binding"*). That target is **not yet authored** —
> it is listed in the wiki table of contents as a planned page. The path is given so the link
> resolves once the page lands. From the surprises side, the binding point is the
> `intr_info(0x0400001C)` latch (§5.2); a surprise bit is *not* a SoC-INTC bit (§6).

---

## 8. The surprise-word source — the FLIX boundary

`[HIGH/OBSERVED that it is a FLIX bundle + that the word lands in `[a1+12]`; the per-bit lane
derivation is the explicit boundary.]` The surprise word is **computed inside a FLIX wide
bundle** at `0x6b13` in `sunda_check_surprises`, from the per-engine context struct
(CAYMAN DRAM ptr `0x00085068`, passed by the Sunda FSM at `0x2d8f`), and written to handler
frame `[a1+12]`.

The stock `ncore2gp` objdump cannot decode that bundle and mis-prints it:

```text
  6b13:  7f                .byte 0x7f       ; op0=0x7f → a FLIX wide bundle (IsaMaxInstructionSize=32)
  6b14:  63                .byte 0x63
  6b15:  46f46a            j 0x216ea        ; BOGUS — 0x216ea is OUT OF the 0x1c820 image (max 0x1c814)
```

> **CORRECTION — the `j 0x216ea` "detour" is a J-misdecode, not a real jump.** A naive reading
> (and an earlier sibling note) treated `0x216ea` as a real branch target. It is not: `0x216ea`
> is **out of the 116,768-byte (0x1c820) IRAM image**. The bytes `7f 63 46 f4 6a` are a FLIX
> wide bundle (`op0=0x7f`); the stock objdump, lacking the wide-bundle slot decode for that op0,
> falls back to reading the embedded `46` byte as a `j` opcode whose 18-bit displacement lands
> far out of bounds (the same artifact produces other bogus `0x217xx` "targets" in this TU).
> **The real instruction computes the surprise word**, which lands in `[a1+12]` and is logged +
> dispatched downstream. `[HIGH/OBSERVED — image size + the J-decode arithmetic both confirm the
> misdecode.]`

This is a **tooling boundary, not a missing-code gap**: the shipped ncore2gp objdump config
lacks the FLIX/IVP wide-bundle slot decode for these op0 values, so the exact per-bit lane
derivation (which context sub-field feeds each of bits 2/3/8/9) is not recoverable with this
toolchain. It is gen-stable: the same wide-bundle word-source desync is present in the older
SUNDA check function as well (§9). The slot decode that would expose the per-bit lanes is a
[uarch-debugger](uarch-debugger.md) concern. `[MED/UNRECOVERABLE with this toolchain.]`

---

## 9. Per-generation invariance

`[HIGH/OBSERVED]` Method: carve the `NX_POOL_DEBUG` IRAM `.rodata` for each generation and
byte-search the handler's bit-test instruction signatures (the `l8ui`+`bbci` pairs are
aperture-independent, so they survive relocation). Image sizes this session: **SUNDA 59,600 /
CAYMAN 116,768 / MARIANA 114,816 / MARIANA_PLUS 119,616 B**.

**CAYMAN family (CAYMAN / MARIANA / MARIANA_PLUS) — byte-identical, relocated only.** The
handler-prologue signature `36610029210c1222410c` (`entry ; s32i.n [a1+8] ; movi.n a2,1 ;
s8i [a1+12]`) was located in each carved image this session:

| signature | CAYMAN | MARIANA | MARIANA_PLUS |
|---|---|---|---|
| `sunda_handle_surprises` body prologue | `0x6cf4` | `0x6e74` | `0x6f10` |

⇒ the bit→surprise map (bit 9/2/3/8), the frame layout (`[a1+8]`/`[a1+9]`), the reason codes
(4/8/64), the `0x04000014` read+clear, the linear test order, and the two assert lines
(426/441) are 100% invariant across the three Cayman-class gens; only the relocation offset
differs. `[HIGH/OBSERVED — signature byte-search hits.]`

**SUNDA (older gen) — same algorithm, materially different packaging.** The CAYMAN signature
gets **0 hits** in SUNDA; SUNDA uses a different aperture and frame:

| aspect | SUNDA | CAYMAN family |
|---|---|---|
| function / source file | `handle_surprises` / `fetch.hpp` | `sunda_handle_surprises` / `surprises.hpp` |
| halt / breakpoint CSR | abs `0x00100808` (window `0x00100000` + `0x808`) | `0x04000014` (window `0x04000000` + `0x14`) |
| word frame | `[a1+4]` (lo) / `[a1+5]` (hi) | `[a1+8]` / `[a1+9]` |
| step counter | 16-bit `@DRAM 0x29b0+96` | 32-bit `@0x85650` |
| word-bit positions | lo bit2/bit3, hi bit0/bit1 — **identical** | lo bit2/bit3, hi bit0/bit1 |

> **QUIRK — the Cayman class refactored surprises out of `fetch.hpp` into `surprises.hpp`.** The
> older SUNDA firmware carries the surprises handler inside `fetch.hpp` as plain
> `handle_surprises`; the Cayman class split it into a dedicated `surprises.hpp` TU and — somewhat
> ironically — named the function `sunda_handle_surprises`. The **taxonomy** (the four named
> types, their bit positions, the reason codes, the EXT_BREAK-reads-halt-CSR tie, the FATAL-on-
> unhandled) is invariant across **all four** gens; only the aperture, frame offsets, counter
> width, and source-file packaging changed. The FLIX word-source desync (§8) is present in both
> the SUNDA check and the Cayman check — gen-stable. `[HIGH/OBSERVED]`

---

## 10. Reimplementation checklist

To rebuild a Vision-Q7-compatible surprises subsystem:

1. **`poll_surprises`** — read bit 0 of the SW running-flag at `state[0x855e0+100]`
   (= `0x85644`); return it. Call it **first** in every FSM iteration; `flag==0` ⇒ graceful
   loop exit (§2).
2. **`sunda_check_surprises`** (Sunda-mode loops only) — compute the 2-byte surprise word from
   the per-engine context struct, log `(any, word)`, call the handler, and fold `~handled` into
   the loop-continue flag (§3).
3. **`sunda_handle_surprises`** — a fixed-order **linear** bit chain over the 2-byte word
   (lo=`[a1+8]`, hi=`[a1+9]`): `STRONG_ORDER(bit9, non-terminal, memw+reorder)` → `INS_BREAK(bit2,
   Setup-Halt 4)` → `EXT_BREAK(bit3, read+clear instr_halt_ctrl, Setup-Halt 8, assert if unarmed)`
   → `STEP_CNT(bit8, decrement @0x85650, Setup-Halt 64 at 0)` → re-test bit 9 → FATAL if no
   handled bit (§4). Default result = 1 (handled).
4. **Setup-Halt helper** — load `&running-flag` + reason, tail-call the run-state Setup-Halt
   (`0x1cf8`): saves resume PC, clears the running-flag, stores the reason (§4.3).
5. **Wire it as polled**, not as a hardware ISR: no `intr_info`/`intr_ctrl` access, no vector,
   per-arm in-band ack only (§5.3, §6).
6. **The surprise word source is a FLIX bundle** — reproduce the per-bit lane assembly from the
   context struct; the exact slot decode is a uarch/debugger detail (§8).

---

## 11. Verification ledger

| # | claim | how grounded this session | verdict |
|---|---|---|---|
| 1 | carve `8e4412b9…`(IRAM 116768) / `7bdf6ed7…`(DRAM 28448); disasm 45,901 lines exit 0 | `objcopy`+`sha256sum`+`xtensa-elf-objdump` | **HIGH/OBSERVED** |
| 2 | `poll_surprises @0x6af4` reads running-flag `0x85644` bit0; 2 call sites (`0x2d81` Sunda, `0x31c0` HW-decode) | resync disasm; `const16 8/0x55e0 + addi 100`; both call sites byte-shown | **HIGH/OBSERVED** |
| 3 | `0x6b0c` = `sunda_check_surprises` (log DRAM `0x819cc`, call handler `0x6ce0`) — **not** pending_redirect | disasm + DRAM string dump | **HIGH/OBSERVED (CORRECTION vs siblings)** |
| 4 | handler bit map STRONG_ORDER=bit9 / INS_BREAK=bit2 / EXT_BREAK=bit3 / STEP_CNT=bit8; reasons 4/8/64; asserts 426/441 | 5 `bbci`/`bbsi` arms + 3 `movi a10,N;call8 0x6e48` + 2 `movi a12,0x1aa/0x1b9` byte-shown | **HIGH/OBSERVED** |
| 5 | only CSR touched is `0x04000014` (EXT_BREAK read+ack); `0x001C`/`0x0018` absent from `0x6af4–0x6e60` | const16-scan of the span: only `const16 ,20` present | **HIGH/OBSERVED** |
| 6 | Setup-Halt helper `0x6e48 → 0x1cf8` (run-state Setup-Halt); assert `0xa304 → 0x13e00` (FW-09) | disasm of `0x6e48`, `0xa304`; targets cross-checked vs siblings | **HIGH/OBSERVED** |
| 7 | per-gen: CAYMAN `0x6cf4` / MARIANA `0x6e74` / MARIANA_PLUS `0x6f10` byte-identical; SUNDA 0 hits | signature byte-search over four carved images | **HIGH/OBSERVED** |
| 8 | `j 0x216ea` is a FLIX-bundle J-misdecode (out of `0x1c820` image), word lands in `[a1+12]` | image-size + decode arithmetic; resync past it | **HIGH/OBSERVED (boundary)** |
| 9 | STRONG_ORDER `memw + reprogram_ordering(0x3a84)` = ordering-promotion lever | `0x6d18` `memw`; `0x6d1b` `call8 0x3a84` (helper body INFERRED) | **HIGH/OBSERVED; policy INFERRED** |
| 10 | per-bit FLIX lane derivation; INS_BREAK/STEP_CNT arm-CSR identity | tooling lacks wide-bundle slot decode; arm-CSR not read in-handler | **MED/INFERRED (boundary)** |

### Divergences recorded on this page

1. **`0x6b0c` identity (§2.1 CORRECTION).** Sibling pages label `0x6b0c` as
   *"pending_redirect / sunda_fetch"*; the binary shows it is `sunda_check_surprises`
   (logs `"S: sunda_check_surprises: …"`, calls the bit-mask handler). Flag for the per-Part
   reconcile: `main-loop.md` §4 STEP 2, `fetch-pc-redirect.md`, and `run-state.md` §11/§ See-also
   carry the older label and should be reconciled to *surprise check* (the fetch/PC-redirect
   work is a later, separate step in the Sunda loop body).
2. **`'S:'` count discipline.** The `'S: '` substring census in the POOL DEBUG DRAM is **187**
   (`python3` regex over the binary), not the 178 dispatch-table entries — and not the
   line-collapsed `rg -c` figure of 178 (which `main-loop.md` §1/§11 cites as "178 'S:' strings";
   that count is the dispatch-table population mis-attributed). The surprise log strings counted
   here are a subset of the 187 `'S: '` format-string population; **178 is the dispatch-table
   entry count, a different population**. Flagged for reconcile.

---

## See also

- [SEQ Run-State Machine](run-state.md) — the SW running-flag (`0x85644`) the poll reads, and
  the Setup-Halt (`0x1cf8`) every surprise halt routes through; the `intr_info` dispatcher latch
  (the separate, coarser interrupt layer).
- [SEQ Main FSM Loop](main-loop.md) — where the poll (STEP 1) and check (STEP 2) sit at the top
  of the Sunda and HW-decode loops; the §2.1 CORRECTION on the `0x6b0c` label applies to its §4.
- [SEQ Error-Handler / Fault Reporting](error-handler.md) — the FW-09 FATAL raise
  `0x13e00` that an unhandled surprise or an unarmed EXT_BREAK lands in (§4.4).
- [Atomic + Memory-Ordering Model](../../uarch/atomics-ordering.md) — the canonical home of
  `STRONG_ORDER` (§7 there); `ATOMCTL` is the 10-bit `[9:0]` field, and `RSR.ATOMCTL` serialises
  via the ATOMCTL pipeline (the `XTSYNC@4` tag is a scheduler annotation, not a real barrier),
  consistent with §7.1 here.
- [INTC → Q7 Firmware "Surprises" Binding](../../control/interrupt/q7-surprises-binding.md)
  *(Part 13, forward-link — target not yet authored)* — the physical SoC-INTC → `intr_info`
  binding referenced in §7.2.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the `OBSERVED` /
  `INFERRED` / `CARRIED` × `HIGH` / `MED` / `LOW` tags used throughout.
