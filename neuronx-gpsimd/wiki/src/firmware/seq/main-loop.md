# SEQ Main FSM Loop

This page reconstructs the **per-iteration finite-state machine** that the NX
**sequencer (SEQ) engine** runs on-device: the fetch → decode → dispatch → advance
loop that reads the next TPB **sequencer-microcode word**, decodes its opcode,
routes to a handler through a 178-entry computed jump table, advances the program
state, and re-checks the run-state machine (RUNNING / PAUSED / HALT) before looping.
This is **the SEQ engine's own firmware** — windowed-ABI Xtensa code on the
`ncore2gp` "Cairo" core — *not* the microcode it interprets. Keeping those two levels
straight is the single most important thing on this page; see
[§0.1 Two levels](#01-two-levels-the-host-loop-vs-the-interpreted-word) before reading
anything else.

Everything below is **byte-pinned to a shipped artifact**. The anchor
image is the carved `CAYMAN_NX_POOL_DEBUG` device firmware extracted from
`libnrtucode.a` (member `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` for code,
`…_DRAM_contents.c.o` for strings), disassembled with the native
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Cairo µarch, Xtensa24, RI-2022.9,
`TargetHWVersion=NX1.1.4`) that ships inside the gpsimd-tools package. The DEBUG
build **keeps** the `'S:'` format strings; the PERF build strips them and re-lays-out
code, so PERF offsets do *not* map 1:1 onto the addresses here — see [§9](#9-debug-vs-perf-the-offsets-here-do-not-port). Where a sibling note or
prompt-level folklore disagrees with the disassembly, **the binary wins**, and an
in-place **CORRECTION** says so.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor;
crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading),
**NOTE** (orientation). The page default is `[HIGH/OBSERVED]`; claims that depart from it
carry an explicit tag.

---

## 0. The loop in one diagram

All addresses are **IRAM-image offsets == device IRAM VA** (`ResetVectorOffset=0`,
InstRAM base `0x0`). DRAM string offsets are **VA − 0x80000** (DataRAM base
`0x00080000`). `.text`/`.rodata` are VMA==file-offset.

```
                        ┌──────────────── LEVEL 1 — OUTER BOOT LOOP ──────────────────┐
   reset (§boot) ─────► │  0x2499  call8 enter_run            (runs the inner FSM)     │
                        │  0x24b6  post-dispatch + notify/timestamp work               │
                        │  0x24e9  j 0x2499      ◄── forever back-edge                 │
                        └──────────────────────┬──────────────────────────────────────┘
                                               │ call8 0x2c64
                        ┌──────────────── LEVEL 2 — enter_run @0x2c64 ────────────────┐
                        │  poll nx.start_ctrl(0x0004) until != 0 ; ack = 0            │
                        │  publish nx.run_state(0x0008) = 1 (RUNNING)                 │
                        │  set sw running-flag state[0x855e0+100] = 1                 │
                        │  a5 = Sunda-mode predicate ; FALL THROUGH (no retw)         │
                        └──────────────────────┬──────────────────────────────────────┘
                                               │ 0x2d6b beqz.n a5 → 0x2d7f
                        ┌──────────────── LEVEL 3a — SUNDA SEQ LOOP (sw fetch) ───────┐
                        │  0x2d81  call8 0x6af4   POLL-SURPRISES (work? → a10)        │
                        │  0x2d84  beqz a10 → 0x31a6 ─► retw.n  (LOOP EXIT)           │
                        │  0x2d9a  call8 0x6b0c   SURPRISE CHECK (sunda_check_surprises)│
                        │  0x2e3b  l8ui [a1+40]   STOP test → 0x31a9 retw on stop     │
                        │  0x2e46  call8 0x1c64   ADVANCE / surprise-consume          │
                        │  0x2e50  l32i [a4]      FETCH opcode word of microcode instr│
                        │  0x2e54  LOG "Dispatch opcode=0x%x"                         │
                        │  0x2e5f  addi a2,a2,-65 DECODE: idx = opcode_byte − 0x41    │
                        │  0x2e62  movi a3,177    RANGE: bgeu a3,a2 → 0x2e6b          │
                        │  0x2e68  j 0x3198       (out of range → default handler)    │
                        │  0x2e6b  const16 a3,0x80814 ; addx4 ; l32i ; jx a2 ─► HANDLER│
                        │  every handler: ... ; j 0x31a3                              │
                        │  0x31a3  j 0x2d81       ◄── SUNDA BACK-EDGE                 │
                        └──────────────────────┬──────────────────────────────────────┘
            opcode 0xa1 sync (0x2e89): RER[0x122000+idx] bit2
                  bit2=1 ─► Enter Pause (0x1cc0): run_state=2; memw ─► PAUSED
                  bit2=0 ─► Setup Halt  (0x1cf8): save resume PC; 0xa390 ─► HALTED

   (interrupt path)  DISPATCHER @0x4c5c reads nx.intr_info(0x001C):
        ==1 → call enter_run ; then call LEVEL-3b HW-DECODE FSM @0x31ac
        ==0 → retw ;  >=2 → assert "handle_interrupt_"
```

One-line verdict: the SEQ engine has **no single flat run loop**. It is a three-level
nested structure with **two distinct per-iteration FSM bodies** selected by execution
mode — a **Sunda software-fetch** body (3a, `0x2d81`, recovered instruction-exact
end-to-end) and a **HW-decode** body (3b, `0x31ac`, head + exits recovered, dispatch
body in a flagged FLIX desync span). Both are reached out of `enter_run`, which is
itself called every pass of the forever outer boot loop.

> **NOTE — provenance of the entry handoff.** Level 1/2 (reset → `_start` → `BEGIN`
> → boot loop → `enter_run`) is established on the boot page; this page picks up at
> `enter_run`'s fall-through into the inner FSM. See
> [SEQ Boot / Entry Path](boot.md).

### 0.1 Two levels: the host loop vs. the interpreted word

| Level | What it is | Tooling | Where documented |
|---|---|---|---|
| **(a) The loop itself** | The SEQ engine's **own firmware** — FLIX/VLIW Xtensa code on `ncore2gp` (`IsaMaxInstructionSize=32`). This *is* the fetch/decode/dispatch FSM. | native `xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp` | **this page** |
| **(b) The interpreted word** | The **64-byte TPB sequencer-microcode word** the loop fetches at `0x2e50` and dispatches on. Its first byte is the ASCII-coded opcode the decode reads. | NEFF per-engine microcode | [NEFF SEQ Microcode](../../neff/seq-microcode.md) *(Part 11, forward-link)* |

> **GOTCHA — do not conflate the two opcode spaces.** The Xtensa `entry`/`call8`/`jx`
> you see in the disassembly are the *engine's instruction set*. The `'A'`,`'S'`,`0xa0`,
> `0xa1` bytes the decode at `0x2e5f` operates on are the *microcode word's opcode* —
> a completely separate encoding that the engine **interprets**. The 178-way table at
> DRAM `0x80814` maps microcode-opcodes → Xtensa handler addresses; it is *not* an
> Xtensa decode table.

---

## 1. Key facts

| Fact | Value | Anchor | Conf |
|---|---|---|---|
| Anchor image | `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` (in `libnrtucode.a`) | `ar t` member list | `[HIGH/OBSERVED]` |
| IRAM `.rodata` size (DEBUG) | `0x1c820` = **116,768 B** | `readelf -SW` | `[HIGH/OBSERVED]` |
| DRAM `.rodata` size (DEBUG) | `0x6f20` = 28,448 B | `readelf -SW` | `[HIGH/OBSERVED]` |
| IRAM `.rodata` size (PERF) | `0x17280` = **94,848 B** (smaller; strings stripped) | `readelf -SW` PERF member | `[HIGH/OBSERVED]` |
| `'S:'` strings in DEBUG DRAM | **178** records / **187** instances | `rg -c -a 'S: '` = 178 NUL-delimited records (1/dispatch slot); `rg -o`/`bytes.count` = 187 literal `S: ` substrings (9 extra from 4 multi-`S:` records) | `[HIGH/OBSERVED]` |
| `'S:'` strings in PERF DRAM | **0** | `rg -c -a 'S: ' perf_dram.bin` | `[HIGH/OBSERVED]` |
| Sunda FSM body | `0x2d81` (fetch) … `0x31a3 → 0x2d81` back-edge | disasm | `[HIGH/OBSERVED]` |
| HW-decode FSM body | function `@0x31ac` ("Seq Loop, iter") | disasm | `[HIGH/OBSERVED]` |
| Opcode dispatch table | DRAM `0x80814`, **178 entries**, span `0x814..0xadc` (712 B) | xxd + struct unpack | `[HIGH/OBSERVED]` |
| Real handlers / defaults | **55 real / 123 default (`0x3198`)** | struct-unpack count | `[HIGH/OBSERVED]` |
| Decode | `idx = opcode_byte − 0x41` (`'A'`); range `[0,177]` | `0x2e5f`/`0x2e62` | `[HIGH/OBSERVED]` |
| FSM source unit | `fsm.hpp` (assert at `enter_run` prologue) | string in DRAM | `[HIGH/OBSERVED]` |

The implementation file is named in the firmware's own assert strings:
`enter_run`'s prologue assert loads `"enter_run"` + `"fsm.hpp"`, confirming the
sequencer FSM is compiled from `fsm.hpp`. `[HIGH/OBSERVED]`

---

## 2. Level 1 — the outer boot loop `0x2499 .. 0x24e9`

The top of the control hierarchy is an **unconditional forever loop**. Each pass
invokes `enter_run` (which itself runs the inner FSM to quiescence), then services
post-dispatch and notification/timestamp work, then jumps back.

```c
// LEVEL 1 — outer boot loop. SEQ firmware (Xtensa), @0x2499. [HIGH/OBSERVED]
for (;;) {                                  // 0x24e9: j 0x2499 (back-edge)
    enter_run();                            // 0x2499: call8 0x2c64 (runs inner FSM)
    if (state[0x85f88 + 20] & 1)            // 0x249c: second per-engine descriptor
        post_dispatch_A();                  // 0x24b0: call8 0x13348 (conditional)
    post_dispatch_B();                      // 0x24b6: call8 0x1c130 (always)
    if (state[0x855e0 + 108] & 1) {         // 0x24bf: Sunda-mode flag
        notification_work(&state[0x855e0+100]); // 0x24e3: call8 0x3a44
        timestamp_work();                   // 0x24e6: call8 0x1c13c
    }
}
```

`state[0x855e0]` is the central per-engine SEQ state struct (mode flags at `+108`,
the SW running flag at `+100`); `state[0x85f88]` is a second per-engine descriptor
whose `[+20]` bit gates the conditional post-dispatch handler. `[HIGH/OBSERVED]`

---

## 3. Level 2 — `enter_run @0x2c64`: the boot/resume handshake that falls into the FSM

`enter_run` is **not** a one-shot call that returns immediately. After the
start-control handshake and the run-state publish, it **falls through** (no `retw`)
into the inner Sunda FSM body and spins it until "no work", at which point control
reaches the `retw.n` at `0x31a9` and returns to the outer boot loop.

The handshake + publish are byte-verified at `0x2d0d`:

```c
// enter_run StartCtrl-ack + run_state publish. @0x2d0d. [HIGH/OBSERVED]
// a3 = nx.start_ctrl base (CSR 0x0004); a2 = &state[0x855e0+100]
*(u32*)(NX + 0x0004) = 0;                   // 0x2d15: s32i 0  -> ACK start_ctrl
state[0x855e0 + 100]  = 1;                   // 0x2d19: s8i 1   -> SW running flag
*(u32*)(NX + 0x0008) = 1;                   // 0x2d22: s32i 1 -> [a3+4] = run_state = 1 (RUNNING)
// ... dispatch-context init (0x3cdc / 0x5524) ...
// 0x2d4e: LOG "S: enter_run: done"
// FALL THROUGH (no retw) into the Sunda FSM:
if (a5 == 0) goto fetch_0x2d81;             // 0x2d6b: beqz.n a5 → 0x2d7f → 0x2d81
log("S: Sunda seq Loop");                    // 0x2d70..0x2d76 (a5 != 0 path)
// → 0x2d81 FETCH
```

> **QUIRK — `run_state` is written through the `start_ctrl` pointer.** The store at
> `0x2d22` is `s32i.n a4, a3, 4` where `a3 = 0x0004` (`nx.start_ctrl`). `0x0004 + 4 =
> 0x0008 = nx.run_state`. The firmware reuses the `start_ctrl` base register and a `+4`
> displacement rather than re-materialising the `run_state` address. A reimplementation
> that hard-codes two separate base loads is *functionally* identical but will not
> byte-match. `[HIGH/OBSERVED]`

The `a5` register is the **Sunda-mode predicate** (`a5 != 0` ⇒ Sunda software-fetch
mode). It gates the "Sunda seq Loop" log here, and — as §4 shows — it *also* gates the
explicit fetch-from-cursor inside the loop body.

> **NOTE — the prologue assert pins `fsm.hpp`.** At `0x2c80` the prologue loads
> `"enter_run"` + `"fsm.hpp"` into the assert helper (`0xa304`) for a busy-flag sanity
> check on `state[0x855e0+100]`. This is the source-file anchor for the whole FSM.
> The StartCtrl poll/ack details (`0x2cb7` spin until `start_ctrl != 0`) are on the
> [boot page](boot.md). `[HIGH/OBSERVED]`

---

## 4. Level 3a — the Sunda software-fetch FSM (the per-iteration loop)

This is the body to reimplement first: it is recovered **instruction-exact
end-to-end**. The loop head is reached the first time via `enter_run`'s fall-through;
every subsequent iteration re-enters at the FETCH/POLL point `0x2d81` via the
back-edge `0x31a3 → 0x2d81`. The seven steps, all byte-verified:

```c
// LEVEL 3a — Sunda seq loop. SEQ firmware (Xtensa). [HIGH/OBSERVED]
// Loop-local regs: a4 = microcode-stream cursor (&instr_word);
//                  a1+40 = "instruction-ready / stop" flag byte;
//                  a5 = Sunda-mode predicate (set in enter_run).
sunda_loop:                                  // back-edge target 0x2d81

  // STEP 1 — POLL-SURPRISES (loop-continuation / exit test)
  if (poll_surprises() == 0)                 // 0x2d81: call8 0x6af4
      goto exit_retw;                         // 0x2d84: beqz a10 → 0x31a6 → 0x31a9 retw.n

  // STEP 2 — SURPRISE CHECK then software PC redirect (Sunda mode)
  if (sunda_check_surprises(ctx)) {          // 0x2d9a: call8 0x6b0c (nonzero ⇒ handled; see CORRECTION)
      if (redirect_flag_byte & 1) {          // 0x2daa: l8ui ; bbci a3,0 → 0x2df5
          compute_redirect_target();         // 0x2db3: call8 0x4c3c
          // ... 0x2dd3 0x5b74 ; 0x2dd6 0x6b70 ...
          log("S: sunda_fetch: redirecting to iram_pc=0x%x"); // 0x2ddd (DRAM 0x197b)
          clear_pending();                   // 0x2dea: call8 0x6bb0 ; s8i 0 -> [a2]
      } else {
          no_redirect_path();                // 0x2df5: call8 0x5e98
          // 0x2e2c: LOG "S: sunda_fetch pending_redirect=%d" (DRAM 0x19a8)
      }
      state[a1+40] = a5;                     // 0x2e35: mark "have instruction"
  } else {
      state[a1+40] = 0;                      // 0x2da4: s8i 0  (no-redirect/stop path)
  }

  // STEP 3 — EXIT / STOP test
  if (state[a1+40] == 0)                     // 0x2e3b: l8ui ; bnez.n → 0x2e46
      goto exit_retw;                         // 0x2e43: j 0x31a9 (retw.n) — stop requested

  // STEP 4 — ADVANCE / surprise-consume
  advance_consume();                          // 0x2e46: call8 0x1c64 (-> 0x3a78)

  // STEP 5 — FETCH the opcode word of the next microcode instruction (gated on Sunda pred)
  if (a5 == 0) {                             // 0x2e4e: bnez.n a5 → 0x2e62 (SKIP fetch+log)
      *(u32*)a4 = a2;                        // 0x2e50: s32i a2,[a4]   (publish cursor word)
      opword = *(u32*)a4;                    // 0x2e52: l32i a11,[a4]
      log("S: Dispatch opcode=0x%x", opword);// 0x2e54 (DRAM 0xe38)
  }
  a2 = *(u32*)a4;                            // 0x2e5d: l32i a2,[a4]  (opcode word)

  // STEP 6 — DECODE + DISPATCH (178-way computed jump)
  idx = (int)a2 - 65;                        // 0x2e5f: addi a2,a2,-65  (opcode_byte - 'A')
  if ((unsigned)idx > 177)                   // 0x2e62: movi a3,177 ; 0x2e65: bgeu a3,a2
      goto unknown_opcode;                    // 0x2e68: j 0x3198 (DEFAULT)
  void *tgt = ((u32*)0x80814)[idx];          // 0x2e6b: const16 a3,0x80814 ; addx4 ; l32i
  goto *tgt;                                  // 0x2e76: jx a2  → handler trampoline

  // STEP 7 — BACK-EDGE (every handler ends here)
  // each handler trampoline: call8 <impl> ; j 0x31a3
back_edge:                                    // 0x31a3: j 0x2d81 → STEP 1
  goto sunda_loop;

exit_retw:                                    // 0x31a6: j 0x31a9 ; 0x31a9: retw.n
  return;                                     // → back to outer boot loop (Level 1)
```

> **GOTCHA — the loop's `l32i` reads a 32-bit *opcode word*, not the whole 64-byte
> microcode instruction.** The fetch at `0x2e50`/`0x2e5d` is a single `l32i` of `[a4]`.
> The decode at `0x2e5f` then operates on the low byte (`addi −0x41`). The full
> **64-byte** sequencer-microcode instruction is the *interpreted word* (level (b));
> the SEQ firmware loads only its opcode-bearing first word here, and the matched
> handler consumes the remaining fields from the same `[a4]`-based descriptor (e.g.
> the `0xa1` sync handler at §6 reads `*(u32*)(a4+4)`). So "fetch the 64-byte word" is
> the *semantic* operation; the byte-exact instruction is a 32-bit `l32i` of the
> opcode word. `[HIGH/OBSERVED]`

> **CORRECTION — the `a5` predicate gates the explicit fetch, not just the log.** A
> naive reading of the backing trace treats `0x2e50` (FETCH) as unconditional. The
> disassembly shows `0x2e4e: bnez.n a5, 0x2e62`: when `a5 != 0` (pure Sunda mode) the
> engine **skips** the `s32i [a4] / l32i / "Dispatch opcode" log` block and jumps
> straight to the decode at `0x2e62`, because the opcode word is already live in `a2`
> from the redirect/advance path. The store-then-load at `0x2e50/0x2e52` and the
> `"Dispatch opcode"` log fire only when `a5 == 0`. Functionally the decode at `0x2e5f`
> always reads the word from `[a4]` via `0x2e5d` regardless; the gated block is the
> cursor-publish + debug log. **The binary wins.** `[HIGH/OBSERVED]`

### 4.1 The two helpers the fetch front-end calls

Both are byte-verified; full treatment is on the
[fetch / PC-redirect](fetch-pc-redirect.md) and
[surprises / IRQ poll](surprises-irq.md) pages.

```c
// poll_surprises @0x6af4 — "is there work?" → bool in a2/a10. [HIGH/OBSERVED]
bool poll_surprises(void) {
    // 0x6af7..0x6afd: a2 = &state[0x855e0 + 100]   (the SW running-flag byte)
    u8 f = *(u8*)&state[0x855e0 + 100];   // 0x6b04: l8ui
    return f & 1;                          // 0x6b07: extui a2,a2,0,1 ; 0x6b0a: retw.n
}
```

> **NOTE — "poll-surprises" reads the SW running-flag, not an IRQ register, in this
> path.** `0x6af4` resolves to a `l8ui` of `state[0x855e0+100]` masked to bit0 — the
> same SW running flag `enter_run` sets to 1 and Pause/Halt clear to 0. So the loop's
> top-of-iteration "work?" test is, concretely, *"is the engine still in the running
> state?"*. A separate, richer surprise/IRQ poller exists for the interrupt path
> ([surprises-irq.md](surprises-irq.md)); this loop's STEP-1 gate is the running flag.
> `[HIGH/OBSERVED · INFERRED on the "surprise" naming]`

```c
// sunda_check_surprises @0x6b0c — returns nonzero ⇒ a surprise was handled.
// Computes the surprise word from the per-engine context, logs DRAM 0x819cc
// ("S: sunda_check_surprises: any=%d, surprises=0x%x"), tail-calls the bit-mask
// handler 0x6ce0, and folds the "handled" bool back to bit0 to gate the redirect
// body. (body partially FLIX-interleaved; the bool-return contract is exact.)
// NOT pending_redirect — see CORRECTION below and surprises-irq.md §2/§3. [HIGH/OBSERVED]
```

> **CORRECTION — `0x6b0c` is `sunda_check_surprises`, not `pending_redirect`/`sunda_fetch`.**
> The STEP-2 gate call `0x2d9a: call8 0x6b0c` was earlier labelled "pending_redirect". Re-
> disassembled, `0x6b0c` logs DRAM `0x819cc` (`"S: sunda_check_surprises: any=%d, surprises=0x%x"`),
> computes the surprise word, and tail-calls the surprise bit-mask handler `0x6ce0` — it **is**
> the surprise check (`surprises.hpp`), not a fetch redirect. The PC-redirect / `sunda_fetch`
> work (the `"S: sunda_fetch: redirecting…"` strings @DRAM `0x8197b`/`0x819a8`) runs **after**
> the check returns "handled", in the body at `0x2db3`/`0x2df5` onward. The two functions are
> adjacent in the same packed region, which is the likely source of the mislabel. Full
> treatment: [surprises-irq.md](surprises-irq.md) §2–§3. `[HIGH/OBSERVED]`

---

## 5. Decode + the 178-entry dispatch table (DRAM `0x80814`)

The decode is a single subtract and an unsigned range check; the dispatch is a
computed jump through a flat table of 4-byte IRAM addresses.

```c
// DECODE + DISPATCH, byte-exact. [HIGH/OBSERVED]
idx = opcode_byte - 0x41;                 // 0x2e5f: addi a2,a2,-65  ('A' == 0x41)
if ((unsigned)idx > 177)                  // 0x2e62: movi a3,177 ; 0x2e65: bgeu a3,a2
    handler = 0x3198;                     //   out of range → default (unknown opcode)
else
    handler = *(u32*)(0x80814 + 4*idx);   // 0x2e6b/0x2e71/0x2e74: table[idx]
jx handler;                               // 0x2e76
```

**Table layout** (verified by `struct.unpack` over the carved DRAM image): 178
little-endian 4-byte IRAM targets, `index = opcode_byte − 0x41`, span
DRAM `0x814 .. 0xadc` (712 bytes), default target `0x3198`. Counted directly:
**55 real handlers, 123 default**. `[HIGH/OBSERVED]`

> **NOTE — `0x80814` is the Sunda SW-fetch table; a second table `0x80adc` serves HW-Decode.**
> The Sunda software-fetch FSM documented above (STEP 6) indexes `0x80814`. The HW-Decode FSM
> (`@0x31ac`, the "Seq Loop, iter" path) indexes a **second**, structurally identical 178-entry
> table at DRAM **`0x80adc`** (55 real / 123 default, default fill `0x3a04`) via `const16 a3,0x80adc`
> at `0x36ce`. Which table runs is a **per-runtime-mode** choice (mode flag `state[0x855e0+108]`
> bit0 / CSR `0x4000`[0] `disable_hw_decode`), **not** per-generation — both modes exist on every
> v3+ gen; SUNDA v2 has a single monolithic front-end. See
> [HW-Decode vs Sunda Dual Fetch](dual-fetch.md) for the full side-by-side and the
> HIGHER-`0x80adc`-is-HW-Decode resolution. `[HIGH/OBSERVED]`

Each *real* table entry points at an 8-byte **trampoline** in the packed region
`0x2e79..0x3198` of the form `call8 <impl> ; j 0x31a3` — i.e. call the opcode
implementation, then take the loop back-edge. (A few opcodes — `0xa1` sync — inline
more work before the back-edge; see §6.)

Opcodes are **ASCII-coded mnemonics** (`'A'..'~'`, `0x41..0x7e`) plus a block of
**binary opcodes** `>= 0x92`. Representative entries, all read from the table bytes:

| opcode | char | table → trampoline → impl | note |
|---|---|---|---|
| `0x41` | `A` | `0x3074 → 0x2124` | |
| `0x43` | `C` | `0x309d → 0x21b8` | |
| `0x46` | `F` | `0x2fee → 0x1fe8` | shares impl with `R` |
| `0x52` | `R` | `0x2fe6 → 0x1fe8` | shares impl with `F` |
| `0x53` | `S` | `0x30a5 → 0x21d4` | |
| `0x54` | `T` | `0x30b5 → 0x220c` | |
| `0x67` | `g` | `0x30bd → 0x2228` | |
| `0xa0` |     | `0x2f1d → 0x1e18` | **Event_Semaphore** (logs `"S: Event_Semaphore"`) |
| `0xa1` |     | `0x2e89 → 0x1ca4` | **inline sync** → Enter Pause OR Setup Halt (§6) |
| `0xa2` |     | `0x2e79 → 0x1c6c` | |
| `0xf0` |     | `0x3190 → 0x235c` | |
| `0xf2` |     | `0x3054 → 0x20b4` | |
| *(123 other indices)* | | `→ 0x3198` | unknown-opcode default |

> **GOTCHA — `'F'` and `'R'` share one impl (`0x1fe8`).** Two distinct table indices
> resolve to the same implementation address. A reimplementation must allow N:1
> opcode→handler mappings; the table is not a permutation. `[HIGH/OBSERVED]`

> **GOTCHA — six handler bodies live in a FLIX-desync span.** Opcodes `0x77`,`0x78`,
> `0xb8`,`0xbb`,`0xbd`,`0xf1` have **exact table targets** but their impl *bodies* land
> in FLIX/IVP vector regions where the stock linear sweep loses bundle sync; the bodies
> are not reconstructed here. The table entries and the trampoline back-edges are
> recovered. `[HIGH table / LOW body]`

> **NOTE — opcode → semantics is only partially pinned.** The ASCII coding (`'A'..'~'`)
> and the binary block (`>=0x92`) are directly observed from the table indices; the
> *meaning* of each letter is pinned only where a string anchors it
> (`0xa0`=Event_Semaphore, `0xa1`=sync→Pause/Halt, and `TensorStore` reached via a
> handler). Per-letter mnemonic semantics beyond those are `[MED/INFERRED]`. The
> microcode-word *layout* (the other 60 bytes after the opcode byte) is the
> [NEFF microcode page](../../neff/seq-microcode.md)'s subject.

---

## 6. PAUSE / HALT — the loop's quiescing transitions

Pause and Halt are **not separate opcodes**. They are reached *inside* the opcode-`0xa1`
inline handler (`0x2e89`), a sync/wait instruction. The handler reads an **external**
register over `RER` and branches on bit2 of the remote status:

```c
// opcode 0xa1 inline sync handler @0x2e89. [HIGH/OBSERVED]
sync_primary();                            // 0x2e89: call8 0x1ca4
u32 idx = *(u32*)(a4 + 4); *(u32*)(a4+4)=0;// 0x2e8f
if (idx > 38) idx = 0;                     // 0x2e94: movi a3,38 ; 0x2e96: bltu a3,a2 → clamp
u32 addr = 0x122000 + 4*idx;               // 0x2e9e: movi a3,145 ; 0x2ea1: slli a3,a3,13
                                           //          (145<<13 = 0x122000) ; addx4
u32 status = rer(addr);                    // 0x2eab: rer a2,a2  — read EXTERNAL reg
u32 bit2  = status & 4;                    // 0x2eae: movi a3,4 ; 0x2eb0: and
if (bit2) {                                // 0x2ec1: beqz a2 → 0x2ece
    enter_pause();                          // 0x2ec6: call8 0x1cc0   → PAUSED
} else {
    setup_halt(&state[0x855e0+100], 0);     // 0x2ed9: call8 0x1cf8   → HALTED
    halt_finalize();                        // 0x2edc: call8 0xa390
}
goto 0x31a3;                                // 0x2ee2: back-edge
```

> **GOTCHA — the bit2 mask uses a scratch register, not an immediate `and`.** The mask
> is `movi.n a3,4 ; and a2,a2,a3` (`0x2eae`/`0x2eb0`), not a single `andi`. Same effect
> (test bit2 of the remote status), but the two-instruction form is what the binary
> contains. `[HIGH/OBSERVED]`

> **QUIRK — the external status lives at `0x122000 + idx*4`, idx clamped to ≤ 38.** The
> address is built as `145 << 13` (= `0x0122_0000`) plus `idx*4`, with `idx` clamped to
> the range `[0,38]`. This is a `RER` (read-external-register) access into a remote
> aperture, not a local CSR — the pause/halt decision is driven by a *remote* engine's
> status word. `[HIGH on the address arithmetic / MED on which remote engine]`

### 6a. Enter Pause `@0x1cc0` — publishes `run_state = 2`

```c
// Enter Pause @0x1cc0 ("S: Enter Pause", DRAM 0xf37). [HIGH/OBSERVED]
void enter_pause(void) {
    // 0x1cc3..0x1cc9: a2 = &state[0x855e0 + 100]
    log("S: Enter Pause");                  // 0x1cd3..0x1cd9
    state[0x855e0 + 100] = 0;               // 0x1ce3: s8i 0  — clear SW running flag
    *(u32*)(NX + 0x0008) = 2;               // 0x1ce6: const16 a2,0x0008 ; 0x1cec: movi a3,2
                                            // 0x1cee: s32i — PUBLISH run_state = 2 (PAUSED)
    __memw();                               // 0x1cf0: memw — ordering barrier
    // 0x1cf3: retw.n
}
```

Pause writes `nx.run_state (0x0008) = 2`, barriers, and returns; the engine then
idles. The **resume** handshake lives in the dispatcher (§7): after a Pause, control
reaches `0x4bbe` and **spins on `nx.start_ctrl (0x0004)`** until the host re-asserts
it, then acks `start_ctrl = 0` — identical to the boot StartCtrl handshake.

### 6b. Setup Halt `@0x1cf8` — saves the resume PC, clears running

```c
// Setup Halt @0x1cf8 ("S: Setup Halt", DRAM 0xf47). [HIGH/OBSERVED]
void setup_halt(u8 *running_flag, u32 halt_reason) {
    log("S: Setup Halt");                   // 0x1d04..0x1d0a
    teardown_flush();                       // 0x1d18: call8 0x13c70
    u32 lo = resume_pc_lo();                // 0x1d24: call8 0x3ac4
    u32 hi = resume_pc_hi();                // 0x1d2a: call8 0x3ae8
    *(u32*)(GEN + 0x1060)      = lo;        // 0x1d3a: s32i a3,[a4+0]   (a4 = 0x1060)
    *(u32*)(GEN + 0x1060 + 32) = hi;        // 0x1d44: s32i a3,[a4+32]  (0x1060+0x20 = 0x1080)
    *running_flag = 0;                      // 0x1d48: s8i 0  — clear running flag
    *(u32*)(running_flag + 4) = halt_reason;// 0x1d4d: s32i halt_reason
    // 0x1d4f: retw.n
}
```

> **GOTCHA — the `0x1080` store reuses the `0x1060` base + 0x20, and `a5` is dead.** The
> hi-half store is `s32i.n a3, a4, 32` (`a4 = 0x1060`, `+0x20 = 0x1080`). The firmware
> *also* materialises `a5 = 0x1080` at `0x1d41` — but **never uses it** for the store.
> The general-LR bundle (base `0x1000`, 60 regs, stride `0x20`) makes `0x1060`/`0x1080`
> adjacent, so the `+0x20` displacement is exact. `[HIGH bundle / MED on "resume PC"
> semantics of those two LRs]`

> **CORRECTION — there is no direct write to `nx.instr_halt_ctrl` (`0x0014`) from this
> loop.** A naive model expects the halt path to poke the ISA halt-request bit at
> `0x0014`. No `const16`-built store to `0x0014` appears in the in-sync spans of this
> loop. The software-visible halt goes through Setup-Halt (`0x1cf8`) + `0xa390`; whether
> `0xa390` ultimately writes `0x0014` is inside a partially-desynced region. The
> `run_state = 2` / `start_ctrl` resume handshake **is** exact; the direct `0x0014`
> write is `[LOW]`. **The binary wins.**

---

## 7. Top-level run / interrupt dispatcher `@0x4c5c`

This is the `"handle_interrupt_"` / `interrupt_handler.hpp` entry — the **only** caller
of the HW-decode FSM (`0x31ac`) and of the resume path. It branches on the nx CSRs:

```c
// Dispatcher @0x4c5c. [HIGH/OBSERVED]
void handle_interrupt(void) {
    u32 info = *(u32*)(NX + 0x001C);        // 0x4c5f: const16 a2,28 ; l32i  = nx.intr_info
    if (info == 0) return;                   // 0x4c6b: beqz → 0x4c76 → 0x4cbe retw (idle)
    if (info != 1)                           // 0x4c70: beqi a2,1 → 0x4c79 (run)
        assert_fail("handle_interrupt_",     // 0x4c73: j 0x4ca9 → 0x4c93/0x4c99/0x4c9e
                    "interrupt_handler.hpp", 28); //   (info >= 2 → assert, line 28)
    // --- RUN (info == 1) ---
    enter_run();                             // 0x4c79: call8 0x2c64  (Level 2/3a)
    hw_decode_fsm();                         // 0x4c7c: call8 0x31ac  (Level 3b)
    u32 rs = *(u32*)(NX + 0x0008);           // 0x4c82: const16 a2,8 ; l32i = nx.run_state
    if (rs == 2) goto paused_path;           // 0x4c8a: beqi a2,2 → 0x4ca1
    // ... (rs != 2) assert path ...
}
```

The **resume-from-pause** handshake (also reached at `0x4bb5`) was byte-verified:

```c
// Resume from PAUSE @0x4bb5. [HIGH/OBSERVED]
enter_pause();                               // 0x4bb5: call8 0x1cc0
handle_interrupt();                          // 0x4bb8: call8 0x4c5c (re-dispatch)
while (*(u32*)(NX + 0x0004) == 0)            // 0x4bbe: const16 a2,4 ; l32i ; 0x4bc6 bnez
    ;                                        //   SPIN on nx.start_ctrl until host re-asserts
*(u32*)(NX + 0x0004) = 0;                    // 0x4bd6: s32i 0  — ACK start_ctrl
if (state[0x855e0 + 108] & 1) /* Sunda */    // 0x4bde: l8ui ; 0x4be1: bbci — resume by mode
    ...;
```

So three CSRs run the machine: **`intr_info` (0x001C)** selects run vs idle vs assert;
**`run_state` (0x0008)** reports `1`=RUNNING / `2`=PAUSED; **`start_ctrl` (0x0004)** is
the start/resume handshake (poll, then ack `0`). Full register treatment:
[SEQ Run-State Machine](run-state.md). `[HIGH/OBSERVED]`

---

## 8. Level 3b — the HW-decode FSM `@0x31ac` (head + exits recovered, body desynced)

When the dispatcher takes the RUN path it calls a **second, separate** FSM function at
`0x31ac` (`"S: Seq Loop, iter=0x%x"`). Same skeleton as Sunda — poll-surprises → log
iter → fetch-gate → dispatch — but with an explicit per-iteration **iter counter** at
`state[a7+0]`. The head, the iter log, the fetch-gate, and both exits are
instruction-exact; the dispatch body and the back-edge land in a FLIX/IVP desync span.

```c
// LEVEL 3b — HW-decode FSM @0x31ac. [HEAD/EXITS HIGH/OBSERVED · BODY/back-edge LOW]
void hw_decode_fsm(void) {
    // 0x31ac: entry a1,0x430 ; 0x31af: a6 = a1+0x400 ; a7 = state base
    state[a7 + 0] = 0;                       // 0x31bb: s32i 0 — iter = 0 (init)
hw_loop:
    if (poll_surprises() == 0)               // 0x31c0: call8 0x6af4 ; 0x31c3 bnez
        return;                              // 0x31c5: j 0x3a3c → 0x3a3f retw.n (EXIT)
    u32 iter = state[a7 + 0];                // 0x31d0: l32i a11,[a7]
    log("S: Seq Loop, iter=0x%x", iter);     // 0x31d5 (DRAM 0x1ad1)
    if (iter == a0) goto 0x3213;             // 0x31ed: beq a11,a0 — iter compare [a0 desync, MED]
    state[a7 + 0] = a2; (void)state[a7+0];   // 0x31f0/0x31f2 — iter update (a2 computed upstream)
    if (fetch_gate() == 0)                   // 0x31f4: call8 0x6e60 (reads state[+52] mask → bool)
        goto rtl_pc_fixup_0x3612;            // 0x31f7: beqz a10 → 0x3612 (no instr)
    /* DISPATCH body — FLIX/IVP desync span from ~0x3216 */
    // (also logs "S: RTL_PC_check_delta: ..." @0x326e during PC tracking)
    goto hw_loop;                            // back-edge BURIED in desynced bundles [LOW]
}
```

> **NOTE (honest gap) — the HW-decode back-edge and iter increment are not
> instruction-exact.** From ~`0x3216` onward the body interleaves FLIX/IVP vector
> bundles; stock `objdump` never resyncs to a clean `j 0x31c0/0x31d0`. **Recovered
> exact:** head init (`0x31bb`), poll (`0x31c0`), iter log (`0x31d5`), fetch-gate
> (`0x31f4`), and both exits (`0x3a3f` retw, `0x3612` RTL_PC fixup). **Not recovered:**
> the `+1` increment site (the `0x31f0` store writes `a2`, computed upstream in the
> desynced region) and the back-edge address. The Sunda↔HW-decode mode pick is the
> [dual-fetch](dual-fetch.md) page's subject. `[HEAD HIGH / BODY LOW]`

---

## 9. DEBUG vs PERF — the offsets here do *not* port

`img_CAYMAN_NX_POOL_PERF_IRAM` is a distinct, **smaller** build (`.rodata` =
`0x17280` = 94,848 B vs `0x1c820` = 116,768 B for DEBUG). The
PERF DRAM image carries **0** `'S:'` strings; DEBUG carries **178** records (one
NUL-delimited `S:` record per dispatch slot; the same population counts as **187** literal
`S: ` substrings — see §1 metric note). Removing every
`call8 0x18b84` log call (each a 6-byte `const16`-pair + a `call8`) shifts **all**
downstream code offsets, so the DEBUG addresses on this page **do not map 1:1 onto
PERF**. `[HIGH/OBSERVED]`

The *algorithm* is identical in PERF: poll → redirect → fetch → decode(`−0x41`) →
178-way dispatch → back-edge, plus `run_state` `1`/`2` and the `start_ctrl` handshake.
Only the strings and the layout differ. This page anchors entirely to **DEBUG** so
every step has a named string xref. A PERF-targeting reimplementation should match the
*control structure and CSR semantics*, never the literal addresses.

---

## 10. FSM state-transition table

State variables (all OBSERVED unless noted):

| Variable | Location | Role |
|---|---|---|
| `nx.start_ctrl` | CSR `0x0004` [0] | host→fw start/resume handshake (poll, ack `0`) |
| `nx.run_state` | CSR `0x0008` [31:0] | fw→host status: `1`=RUNNING, `2`=PAUSED |
| `nx.intr_info` | CSR `0x001C` [31:0] | dispatcher selector: `0`=idle, `1`=run, `>=2`=assert |
| SW running flag | `state[0x855e0+100]` | `1` on enter_run, `0` on pause/halt; read by poll-surprises |
| Sunda-mode flag | `state[0x855e0+108]` | selects 3a (sw fetch) vs HW-decode logging |
| iter | `state[a7+0]` (3b) | HW-decode per-iteration counter (init `0`; logged) |

| FROM | EVENT / CONDITION | ACTION | → TO |
|---|---|---|---|
| RESET/boot | boot chain ([boot.md](boot.md)) | → outer loop `0x2499` | BOOT_LOOP |
| BOOT_LOOP | each pass | `call enter_run @0x2c64` | ENTER_RUN |
| ENTER_RUN | `start_ctrl(0x0004) == 0` | SPIN (poll) | ENTER_RUN (wait) |
| ENTER_RUN | `start_ctrl(0x0004) != 0` | ack `start_ctrl=0`; `run_state=1`; set running flag | SEQ_LOOP (3a) |
| SEQ_LOOP (3a) | poll-surprises(`0x6af4`)`==0` | `retw.n` (`0x31a9`) | → BOOT_LOOP |
| SEQ_LOOP (3a) | stop flag `[a1+40]==0` | `retw.n` (`0x31a9`) | → BOOT_LOOP |
| SEQ_LOOP (3a) | `sunda_check_surprises` (`0x6b0c`) handled | then sunda_fetch redirect; clear flag | SEQ_LOOP (fetch) |
| SEQ_LOOP (3a) | have instr | fetch word; `opcode = byte−0x41` | DISPATCH |
| DISPATCH | `idx ∈ [0,177]` & handler ≠ default | `jx table[idx]`; `j 0x31a3` → `0x2d81` | (handler) → SEQ_LOOP |
| DISPATCH | out of range / default | `j 0x3198` (unknown-opcode) | (default) → SEQ_LOOP |
| handler `0xa1` | `RER[0x122000+idx]` bit2 `== 1` | Enter Pause: `run_state=2`; `memw` | PAUSED |
| handler `0xa1` | `RER` bit2 `== 0` | Setup Halt: save resume PC `0x1060`/`0x1080`; clear running; `0xa390` | HALTED |
| PAUSED | dispatcher (`0x4c5c`/`0x4bbe`) | SPIN on `start_ctrl(0x0004)`; ack `0`; resume by mode | SEQ_LOOP (resume) |
| RUN (`intr_info==1`) | after enter_run | `call HW-decode FSM @0x31ac` | SEQ_LOOP_HW (3b) |
| SEQ_LOOP_HW (3b) | poll-surprises `==0` | `retw.n` (`0x3a3f`) | → dispatcher |
| SEQ_LOOP_HW (3b) | fetch-gate(`0x6e60`)`==0` | → `0x3612` (RTL_PC fixup) | SEQ_LOOP_HW |
| SEQ_LOOP_HW (3b) | have instr | dispatch (desynced body); `iter++` | SEQ_LOOP_HW `[back-edge LOW]` |
| (any) | `intr_info(0x001C)==0` | dispatcher returns | IDLE |
| (any) | `intr_info(0x001C)>=2` | ASSERT `"handle_interrupt_"` | (fault) |

---

## 11. Diagnostic strings (DEBUG DRAM)

DRAM offset = VA − `0x80000`. All 15 anchors below were dereferenced out of the carved
DRAM image and matched their expected text byte-for-byte. `[HIGH/OBSERVED]`

| DRAM off | VA | String | Where emitted |
|---|---|---|---|
| `0x193e` | `0x8193e` | `"S: Sunda seq Loop\n"` | Sunda body entry `0x2d73` |
| `0x1ad1` | `0x81ad1` | `"S: Seq Loop, iter=0x%x\n"` | HW-decode iter log `0x31d5` |
| `0xe38` | `0x80e38` | `"S: Dispatch opcode=0x%x\n"` | FETCH step `0x2e54` (every opcode) |
| `0x197b` | `0x8197b` | `"S: sunda_fetch: redirecting to iram_pc=0x%x\n"` | redirect `0x2ddd` |
| `0x19a8` | `0x819a8` | `"S: sunda_fetch pending_redirect=%d\n"` | no-redirect `0x2e2c` |
| `0xf37` | `0x80f37` | `"S: Enter Pause\n"` | Enter Pause `0x1cd3` |
| `0xf47` | `0x80f47` | `"S: Setup Halt\n"` | Setup Halt `0x1d04` |
| `0xf56` | `0x80f56` | `"S: Event_Semaphore\n"` | opcode `0xa0` impl `0x1e18` |
| `0xe28` | `0x80e28` | `"S: TensorStore\n"` | impl `0x1b0c` (via a handler) |
| `0x1302` | `0x81302` | `"S: enter_run: start\n"` | enter_run `0x2cc0` |
| `0x1336` | `0x81336` | `"S: enter_run: done\n"` | enter_run `0x2d4e` |
| `0x12ec` | `0x812ec` | `"S: Pending StartCtrl\n"` | enter_run `0x2c99` |
| `0x1203` | `0x81203` | `"handle_interrupt_"` | dispatcher assert arg `0x4c93` |
| `0x11db` | `0x811db` | `"interrupt_handler.hpp"` | dispatcher assert arg `0x4c99` |
| `0x1ae9` | `0x81ae9` | `"S: RTL_PC_check_delta: curr_dma_idx_vld=…"` | HW-decode PC track `0x326e` |

The `'S:'` logger is `0x18b84` (varargs printf-class; `a10` carries the format-string
pointer built by a `const16` pair). The assert helper is `0xa304` (takes file-name ptr
+ line in `a10`/`a11`/`a12`).

---

## 12. CSR cross-reference

| CSR off | Name | Role in the loop | Site |
|---|---|---|---|
| `0x0004` | `nx.start_ctrl[0]` | poll (boot+resume), ack `0` | `0x2cb7` poll / `0x2d15` ack / `0x4bbe` poll / `0x4bd6` ack |
| `0x0008` | `nx.run_state[31:0]` | publish `1`=RUNNING / `2`=PAUSED, read `==2` test | `0x2d22` =1 / `0x1cee` =2 / `0x4c82` read |
| `0x0014` | `nx.instr_halt_ctrl[0]` | ISA halt request — **no direct in-sync write found** | via Setup-Halt + `0xa390` `[LOW]` |
| `0x001C` | `nx.intr_info[31:0]` | dispatcher selector (`0`/`1`/`>=2`) | `0x4c5f` read |
| `0x1060` | general LR (`0x1000` bundle) | Setup-Halt resume PC lo | `0x1d3a` write `[MED]` |
| `0x1080` | general LR (`0x1000` bundle) | Setup-Halt resume PC hi | `0x1d44` write `[MED]` |
| `0x80814` | (DRAM, not a CSR) | 178-entry opcode dispatch table | `0x2e6b` base / `0x2e76` `jx` |
| `RER 0x122000+idx*4` | remote/external status | opcode-`0xa1` sync (drives Pause vs Halt) | `0x2eab` `rer` `[MED]` |

Full register-bundle map: [SEQ Run-State Machine](run-state.md) and the CSR pages.

---

## 13. Reimplementation checklist

To rebuild a Vision-Q7-compatible SEQ main loop, the structure to reproduce is:

1. **Outer forever loop** that calls `enter_run` each pass and services post-dispatch
   + notification work (§2).
2. **`enter_run`**: poll `start_ctrl (0x0004)` until non-zero, ack `0`, publish
   `run_state (0x0008) = 1`, set the SW running flag, *fall through* (not return) into
   the inner FSM (§3).
3. **Sunda FSM body** (§4): per iteration — poll-surprises → pending-redirect →
   stop-test → advance → (mode-gated) fetch 64-byte word → decode `idx = op − 0x41` →
   range-check `[0,177]` → `jx DRAM[0x80814 + idx*4]` → handler → back-edge to fetch.
   Exit `retw` on no-work / stop.
4. **178-entry dispatch table** (§5): 55 real handlers (8-byte `call8;j back-edge`
   trampolines), 123 default → `0x3198`. ASCII (`'A'..'~'`) + binary (`>=0x92`) opcodes.
5. **Pause/Halt as a sync handler** (§6), *not* opcodes: read remote status over `RER`,
   bit2 ⇒ Pause (`run_state=2`, `memw`) else Halt (save resume PC to the `0x1060`/`0x1080`
   general LRs, clear running, finalize).
6. **Dispatcher** (§7) branching on `intr_info (0x001C)`: `0`=idle, `1`=run (calls
   `enter_run` then the HW-decode FSM), `>=2`=fault.
7. **HW-decode FSM** (§8) as a *separate* function with a per-iteration iter counter —
   the dispatch body is hardware-decode-fed, not software-fetched.

---

## 14. Cross-references

- [SEQ Boot / Entry Path](boot.md) — reset → `_start` → `BEGIN` → boot loop → the
  `enter_run` handoff this page picks up at.
- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — `poll_surprises`
  (`0x6af4`), the surprise check `sunda_check_surprises` (`0x6b0c`), and the `sunda_fetch`
  redirect family (STEP 1/2 above) in full.
- [SEQ Surprises / IRQ Path](surprises-irq.md) — the authoritative treatment of
  `sunda_check_surprises` (`0x6b0c`) and the surprise bit-mask handler `0x6ce0`.
- [SEQ Decode / Dispatch Hub](dispatch-hub.md) — the 178-entry table, the trampoline
  region, and the per-opcode handler implementations (STEP 6 above) in full.
- [SEQ Run-State Machine](run-state.md) — `start_ctrl` / `run_state` / `intr_info` and
  the Pause/Halt/resume transitions (§6/§7/§10) as a standalone CSR-level machine.
- [SEQ Surprises / IRQ Poll](surprises-irq.md) — the surprise/IRQ poller used on the
  interrupt path (distinct from STEP-1's running-flag gate).
- [HW-Decode vs Sunda Dual Fetch](dual-fetch.md) — the mode pick (`a5` predicate) that
  selects Level 3a vs 3b.
- [NEFF SEQ Microcode](../../neff/seq-microcode.md) *(Part 11, forward-link)* — the
  layout of the 64-byte sequencer-microcode word this loop fetches and dispatches.
