# HW-Decode vs Sunda Dual Fetch

This page is the **definitive reference** for the GPSIMD **SEQ engine's** dual
instruction-fetch/decode front-end: the **two** co-resident fetch/decode finite-state
machines — a hardware-decode-FIFO-assisted **HW-Decode** path and a legacy
software-fetch **Sunda-mode** fallback — that every CAYMAN-and-later NX firmware image
carries side by side, the boot-time flag that selects between them, the **two parallel
dispatch tables** they index, and the per-generation presence of the whole construct.

The surrounding poll/stop loop is [SEQ Main FSM Loop](main-loop.md); the software-fetch
front-end body is [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md); the
178-entry table the Sunda path indexes is fully enumerated in
[SEQ Decode / Dispatch Hub](dispatch-hub.md). Those siblings each *forward-reference this
page* for the mode pick; **this page owns it** — both FSM bodies side by side, the
mode-SELECT mechanism instruction-exact, the per-mode dispatch-table contrast (the
HIGHER table `@0x80adc` characterized here the same way the hub characterized the LOWER
`@0x80814`), the per-gen presence proof, and — as its spine — the **authoritative
resolution of the O1 contradiction**: which physical table slot is HW-Decode.

The SEQ runs on the Vision-Q7 NX `ncore2gp` "Cairo" datapath core
(`IsaMaxInstructionSize = 32`, `XCHAL_HAVE_VISION = 1`, `XCHAL_VISION_TYPE = 7` — this
carries the FLIX/VLIW layer; `XCHAL_HAVE_FLIX3 = 0` is **not** "scalar"). Decode the SEQ
image with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the scalar-LX rule
decodes the *different* NCFW management core and is wrong here.

> **NOTE — what was re-carved and re-disassembled this session.** Every fact below was
> re-derived from a fresh independent carve of the SEQ firmware out of the static archive
> `libnrtucode.a`. The anchor is `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o`; the
> carve `objcopy -O binary --only-section=.rodata` reproduces **iram.bin = 116,768 B
> (`0x1c820`), sha256 `8e4412b9…ab9ed70a`** and **dram.bin = 28,448 B (`0x6f20`), sha256
> `7bdf6ed7…d6816ecd`**. The two dispatch sites, the two tables, the two trampolines, the
> uniqueness census, the BEGIN mode-pick, and the SUNDA negative control were all
> re-decoded/re-parsed against this carve. Three further images were carved for the
> per-gen and negative-control evidence: `SUNDA_NX_POOL_DEBUG` (iram **59,600 B**),
> `MARIANA_NX_POOL_DEBUG` (iram **114,816 B**), `MARIANA_PLUS_NX_POOL_DEBUG` (iram
> **119,616 B**). All IRAM offsets equal device IRAM VA (reset vector at byte 0); DRAM
> string offset = device DRAM VA − `0x80000`. `[HIGH/OBSERVED]`

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string read from the shipped image this session; `INFERRED` =
reasoned over OBSERVED facts (often across a FLIX/literal-pool desync); `CARRIED` =
consolidated from a cited cross-page anchor at its original confidence. Crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation).

> **GOTCHA — "Sunda-mode" (the runtime path) ≠ "SUNDA" (the v2 generation).** They share
> a codename root and are easy to conflate, but they are *different things*. **SUNDA (v2)**
> is an older, wholly *monolithic* firmware that has **neither** mode and **no** dispatch
> table at all (§5). **"Sunda-mode"** is a runtime software-fetch *fallback* present only
> on CAYMAN-and-later images, named after the legacy SUNDA-style software fetch and
> retained as a boot-selectable alternative to the new HW-Decode path. This is the
> [correction-ledger §8](../../reference/correction-ledger.md) distinction; keep it
> straight or every per-gen claim below inverts. `[HIGH/OBSERVED]`

---

## 0. The dual front-end in one diagram

```
                         boot (BEGIN @0x2378)
                                 │
       0x23af  s8i flag,[state 0x855e0 + 108]      store the Sunda-mode flag bit0
       0x23b5  bbci a2,0,0x2422   ◄── THE MODE BRANCH on flag bit0 ───────────┐
                                 │                                            │
            flag bit0 == 1 (fall through 0x23be)         flag bit0 == 0 (→ 0x2422)
                    SUNDA-MODE                                   HW-DECODE MODE
       LOG @0xef5 "NX in Sunda mode:               LOG @0xf1e "NX in HW Decode mode"
            HW decode disabled"                    state[0x85070] = 1 (HW-active)
       set hw_decode.control[20]                   write hw_decode.control (CSR 0x4000)
       (IRAM-ctrl flush; HW decode bypassed)       (disable_hw_decode bit0 cleared)
                                 │                                            │
   ┌─────────────────────────────────┐         ┌──────────────────────────────────────┐
   │  LEVEL 3a — SUNDA SW-FETCH FSM   │         │  LEVEL 3b — HW-DECODE FSM             │
   │  head 0x2d81  back-edge 0x31a3   │         │  head 0x31ac  back-edge 0x3a0f       │
   │  0x2d81 call8 0x6af4 poll(beqz)  │         │  0x31bb s32i 0,[a7+0]  iter = 0      │
   │  0x2d9a call8 0x6b0c PEND-REDIR  │         │  0x31c0 call8 0x6af4 poll(bnez)      │
   │   (SW cursor a4 ; SW PC redirect)│         │  0x31d5 LOG "Seq Loop, iter=0x%x"    │
   │                                  │         │  0x31f4 call8 0x6e60 FETCH-GATE      │
   │                                  │         │  0x326e LOG "RTL_PC_check_delta…"    │
   │  0x2e5f addi a2,a2,-65           │         │  0x36c2 addi a2,a2,-65               │
   │  0x2e62 movi a3,177 ; bgeu       │         │  0x36c5 movi a3,177 ; bgeu           │
   │  0x2e68 j 0x3198  DEFAULT        │         │  0x36cb j 0x3a04  DEFAULT            │
   │  0x2e6b const16 a3,0x80814 ◄LOWER│         │  0x36ce const16 a3,0x80adc ◄HIGHER   │
   │  jx → tramp 0x30xx → j 0x31a3    │         │  jx → tramp 0x38xx → j 0x3a0f        │
   └──────────────┬───────────────────┘         └──────────────┬───────────────────────┘
                  │     both call8 0x2124 (SHARED impl 'A')     │
                  └─────────────────► Handler::execute() ◄──────┘
                         (SHARED per-opcode handler bodies)
```

One-line verdict: **every CAYMAN-and-later NX image ships TWO complete fetch/decode FSMs
and TWO parallel 178-entry dispatch tables.** They funnel to the **same** per-opcode
`Handler::execute()` bodies through mode-specific trampolines that differ only in their
loop back-edge. A boot-time flag picks which FSM does real work. **SUNDA (v2) has
neither** — it predates the dual front-end (§5). The HIGHER table `@0x80adc` is the
HW-Decode path; the LOWER `@0x80814` is the Sunda fallback (§6, the O1 resolution).

---

## 1. Key facts

| Fact | Value | Anchor | Conf |
|---|---|---|---|
| Anchor image | `img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}_contents.c.o` (in `libnrtucode.a`) | `ar t` member list | `[HIGH/OBSERVED]` |
| Carve `iram.bin` | 116,768 B (`0x1c820`), sha256 `8e4412b9…ab9ed70a` | `wc -c` + `sha256sum` | `[HIGH/OBSERVED]` |
| Carve `dram.bin` | 28,448 B (`0x6f20`), sha256 `7bdf6ed7…d6816ecd` | `wc -c` + `sha256sum` | `[HIGH/OBSERVED]` |
| Two fetch/decode FSMs | Sunda SW-fetch `@0x2d81` / HW-Decode `@0x31ac` | disasm | `[HIGH/OBSERVED]` |
| Two `poll-surprises` callers | exactly **2** `call8 0x6af4`: `@0x2d81` / `@0x31c0` | `rg -c` over disasm | `[HIGH/OBSERVED]` |
| Two dispatch sites | `addi a2,a2,-65` `@0x2e5f` (Sunda) / `@0x36c2` (HW-decode, a7-frame) | disasm | `[HIGH/OBSERVED]` |
| LOWER table (Sunda) | DRAM `0x80814`, **178** entries, **55** real / **123** default `0x3198` | `struct.unpack` | `[HIGH/OBSERVED]` |
| HIGHER table (HW-Decode) | DRAM `0x80adc`, **178** entries, **55** real / **123** default `0x3a04` | `struct.unpack` | `[HIGH/OBSERVED]` |
| Tables abut | LOWER `0x814..0xadc` (712 B); HIGHER starts at `0xadc` | offset arithmetic | `[HIGH/OBSERVED]` |
| Mode flag | `state[0x855e0+108]` bit0: `1`=Sunda, `0`=HW-Decode | BEGIN `0x23af`/`0x23b5` | `[HIGH/OBSERVED]` |
| HW-active flag | `state[0x85070]` = `1` in HW-Decode arm only | BEGIN `0x2436` | `[HIGH/OBSERVED]` |
| Mode selector CSR | `hw_decode.control.disable_hw_decode` = CSR `0x4000`[0] (chicken bit) | CSR-01 + BEGIN `0x4000` write | `[HIGH/OBSERVED + CARRIED]` |
| HW-decode-only telemetry | `RTL_PC_check_delta` `@0x326e`, iter-log `@0x31d5` — **1 site each**, both in `0x31ac` body | const16 census | `[HIGH/OBSERVED]` |
| SUNDA (v2) | **NO** dual front-end: 0 mode strings, 0 `addi-65/48` sites, monolithic | SUNDA carve probes | `[HIGH/OBSERVED]` |
| O1 binding | **HIGHER `0x80adc` = HW-Decode**, LOWER `0x80814` = Sunda | uniqueness census (§6) | `[HIGH/OBSERVED]` |

---

## 2. The two FSMs side by side

Both FSMs share the same skeleton — *poll-surprises → (per-mode middle) → fetch →
`opcode − 0x41` decode → 178-way computed jump → handler → back-edge* — but differ in the
HW resource they consume, the middle step, the table they index, and the back-edge they
return to. The Sunda body is recovered instruction-exact end-to-end
([fetch-pc-redirect.md §2](fetch-pc-redirect.md#2-the-sunda-software-fetch-fsm-body-instruction-exact));
the HW-decode body's *head and exits* are exact and its *dispatch tail* is exact, with a
flagged FLIX-desync span between them (§4).

### 2a. The Sunda software-fetch FSM `@0x2d81` (the SW path)

```c
/* LEVEL 3a — Sunda software-fetch FSM. SEQ firmware (Xtensa). [HIGH/OBSERVED]
 * head 0x2d81 ; back-edge 0x31a3 -> 0x2d81 ; indexes LOWER table 0x80814.
 * a4 = SW instruction-stream cursor ; NO iter counter ; NO RTL coherence telemetry. */
sunda_loop:                                  /* back-edge 0x31a3 -> 0x2d81 */
  if (poll_surprises() == 0)                 /* 0x2d81: call8 0x6af4 */
      return;                                 /* 0x2d84: beqz a10,0x31a6 -> retw (EXIT) */
  seq_check_pending_redirect(&redir);        /* 0x2d9a: call8 0x6b0c — SW PC redirect (sunda_fetch) */
  if (!stack.ready) return;                  /* 0x2e3b: stop test -> retw */
  uint32_t word = *(uint32_t *)cursor_a4;    /* 0x2e50/0x2e5d: s32i/l32i [a4] — SW FETCH */
  log("S: Dispatch opcode=0x%x", word);      /* 0x2e54 (DRAM 0x80e38) */
  uint32_t idx = word - 0x41u;               /* 0x2e5f: addi a2,a2,-65 */
  if (idx > 177u) goto *(void *)0x3198;       /* 0x2e62/0x2e65/0x2e68: bgeu 177 ; j 0x3198 DEFAULT */
  void (*tr)(void) = ((void(**)(void))0x80814)[idx];  /* 0x2e6b: const16 0x814 ; addx4 ; l32i */
  tr();                                       /* 0x2e76: jx a2 -> trampoline 0x30xx -> j 0x31a3 */
```

The Sunda middle step is `pending_redirect` (`0x6b0c`): a **software** PC redirect that
re-reads the architectural 64-bit PC from CSR `0x1060`/`0x1080`, recomputes a target, and
**software-writes** it back through the redirect machine `0x5750`. The fetch cursor `a4`
software-reads the opcode word (`l32i.n [a4]`). **There is no hardware decode FIFO in this
loop.** `[HIGH/OBSERVED — full body in [fetch-pc-redirect.md](fetch-pc-redirect.md)]`

### 2b. The HW-Decode FSM `@0x31ac` (the FIFO-assisted path)

```c
/* LEVEL 3b — HW-Decode FSM. SEQ firmware (Xtensa). [HEAD/EXITS/TAIL HIGH ; MID-BODY desync]
 * head 0x31ac ; back-edge 0x3a0f ; indexes HIGHER table 0x80adc ; a7-frame.
 * Carries the iter counter + the RTL_PC_check coherence telemetry — UNIQUE to this body. */
void hw_decode_fsm(void) {
    /* 0x31ac: entry a1,0x430 ; a7 = HW-decode FSM state base */
    state[a7 + 0] = 0;                         /* 0x31bb: s32i 0,[a7+0] — iter = 0 (init) */
hw_loop:
    if (poll_surprises() == 0)                 /* 0x31c0: call8 0x6af4 ; 0x31c3 bnez.n a10 (continue) */
        return;                                /* 0x31c5: j 0x3a3c -> 0x3a3f retw (EXIT) */
    uint32_t iter = state[a7 + 0];             /* 0x31d0: l32i a11,[a7+0] */
    log("S: Seq Loop, iter=0x%x", iter);       /* 0x31d5 (DRAM 0x81ad1) — UNIQUE iter log */
    state[a7 + 0] = next_iter;                 /* 0x31f0: s32i (next_iter computed upstream, desync) */
    if (fetch_gate() == 0)                     /* 0x31f4: call8 0x6e60 — HW FETCH-GATE (state-mask) */
        goto rtl_pc_fixup_0x3612;              /* 0x31f7: beqz a10,0x3612 (no instr) */
    /* RTL_PC_check_delta telemetry @0x326e — HW-FIFO <-> SW-cache COHERENCE (UNIQUE) */
    /* DISPATCH tail @0x36b0 (a7-frame): addi -65 ; bgeu 177 ; const16 0x80adc ; jx */
    /* handler tail -> j 0x3a0f -> hw_loop */
}
```

The HW-Decode middle step is the **fetch-gate** (`0x6e60`): a state-mask read that gates
dispatch on the hardware decoder having an instruction ready (replacing the Sunda FSM's
software `pending_redirect`). The path then reconciles the RTL hardware decoder's
DMA/cache index against the firmware's target cache index via the
`RTL_PC_check_delta` telemetry (§4) — the **coherence point between the HW decode FIFO and
the SW cache**, which exists *only* in this body. `[head/exits/tail HIGH/OBSERVED; the
iter increment and the back-edge interior land in the FLIX-desync span, §4]`

### 2c. The structural difference table

| | **Sunda-mode FSM** (`0x2d81`) | **HW-Decode FSM** (`0x31ac`) |
|---|---|---|
| Head string | `"S: Sunda seq Loop"` (DRAM `0x8193e`) | `"S: Seq Loop, iter=0x%x"` (DRAM `0x81ad1`) |
| Poll-surprises | `call8 0x6af4` `@0x2d81`, **`beqz`** exit | `call8 0x6af4` `@0x31c0`, **`bnez`** continue |
| Middle step | `pending_redirect` `0x6b0c` (SW PC redirect) | `fetch_gate` `0x6e60` (HW state-mask) |
| HW resource consumed | **none** (pure SW fetch) | the **HW decode FIFO** + RTL decoder index |
| Coherence telemetry | none | `RTL_PC_check_delta` `@0x326e` (HW↔SW) |
| Per-iteration counter | none | iter at `state[a7+0]` (init `0`, logged) |
| Fetch | SW cursor `a4` `l32i.n [a4]` | RTL-decoder-fed (gate `0x6e60`) |
| Stack frame | `a1`-frame | **`a7`-frame** (`l32i [a7+0x3e4]` at dispatch) |
| Dispatch site | `0x2e5f` | `0x36c2` |
| Table indexed | DRAM **`0x80814`** (LOWER) | DRAM **`0x80adc`** (HIGHER) |
| Default arm | `0x3198` (reload via `a4`) | `0x3a04` (reload via `a6`) |
| Back-edge | `j 0x31a3` → `0x2d81` | `j 0x3a0f` → `hw_loop` |
| No-instr exit | stop-flag `retw` | `0x3612` (RTL_PC fixup) |

> **GOTCHA — the poll test is mirrored, same helper.** Both FSMs call the *same*
> poll-surprises helper `0x6af4`, but the Sunda body takes the **`beqz`** sense (`a10==0`
> ⇒ exit) and the HW-decode body the **`bnez`** sense (`a10!=0` ⇒ continue). A
> reimplementation that copies one branch sense into both FSMs inverts the HW-decode
> loop's exit. The two `call8 0x6af4` sites — exactly two, `@0x2d81` and `@0x31c0` — *are*
> the two modes (this is the dual-caller fact the surprises-poll page anchors). `[HIGH/OBSERVED]`

> **QUIRK — what the HW-Decode path consumes that the SW path does not: the decode FIFO /
> RTL decoder index.** The HW-Decode FSM does **not** software-read an opcode word through
> a cursor. It gates on `fetch_gate` (`0x6e60`) — a read of a state mask the **hardware
> decode FIFO** drives — and reconciles the RTL decoder's `curr_dma_idx_vld` against the
> firmware's `tgt_cache_idx` (the `RTL_PC_check_delta` log). The hardware
> instruction-decode FIFO is the resource the chicken bit `disable_hw_decode` (§3) gates;
> when it is enabled, the FIFO feeds the fetch and the FSM merely supervises it
> (poll between ops, coherence-check, gate, dispatch). The cache backend follows the same
> split — HW-indexed tag + HW IRAM-DMA in HW-Decode mode vs SW associative scan + DramRing
> in Sunda mode ([iram-cache.md](iram-cache.md), descr[+60] HW/SW flag). `[HIGH on the
> presence of the gate + the coherence telemetry / MED on the exact RTL FIFO semantics]`

---

## 3. The mode-SELECT mechanism (boot-time, instruction-exact)

The mode is picked **once at boot** in `BEGIN` from an args-supplied flag — **not** by
silicon generation. The branch was re-decoded byte-exact this session at `0x23b5`.

### 3a. The BEGIN boot mode-pick `@0x23af..0x2453`

```
23af:  s8i a2,[a3+108]            ; a3 = state 0x855e0 -> store mode flag bit0 to [+108]
23b5:  bbci a2,0,0x2422           ; *** THE MODE BRANCH on flag bit0 ***
--- flag bit0 == 1 (fall through 0x23be) -> SUNDA-MODE ---
23be:  const16 a10,8 ; a10,0xef5 ; call8 0x18b84   ; LOG "NX in Sunda mode: HW decode disabled"
23cf:  <RMW config word [a1+8]: OR 0x40 ; AND ~0x100 ; AND ~0x200>
23f6:  const16 a2,0x5070 ; bbci a2,0,0x241f         ; read state[0x85070]
2405:  const16 a2,0x4000 ; l32i a3,[a2]             ; a3 = hw_decode.control (CSR 0x4000)
240e:  movi.n a4,1 ; slli a4,a4,20 ; or a3,a3,a4    ; set bit20 (iram_ctrl_flush_en)
241a:  s32i.n a3,[a2]                               ; *** WRITE hw_decode.control[20]
--- flag bit0 == 0 (-> 0x2422) -> HW-DECODE MODE ---
2425:  const16 a10,8 ; a10,0xf1e ; call8 0x18b84    ; LOG "NX in HW Decode mode"
2436:  const16 a2,0x5070 ; movi.n a3,1 ; s8i a3,[…] ; *** state[0x85070] = 1 (HW-active)
2444:  const16 a3,0x4000 ; … ; s32i a2,[a3]         ; *** WRITE hw_decode.control (CSR 0x4000)
```

```c
/* BEGIN boot mode-pick. SEQ firmware (Xtensa) @0x23af. [HIGH/OBSERVED]
 * MODE_FLAG = state[0x855e0 + 108] (bit0) : 1=Sunda-mode, 0=HW-Decode.
 * HW_ACTIVE = state[0x85070]              : set 1 only in the HW-Decode arm.
 * CSR_HWDEC_CONTROL = 0x4000 (hw_decode.control; disable_hw_decode = bit0). */
void seq_begin_mode_pick(uint32_t args_flag) {
    state[0x855e0 + 108] = (uint8_t)args_flag;       /* 0x23af: s8i flag bit0 */
    if (args_flag & 0x1) {                            /* 0x23b5: bbci a2,0 -> 0x2422 */
        /* --- SUNDA-MODE (HW decode disabled) --- */
        log("S: NX in Sunda mode: HW decode disabled");          /* @0xef5 */
        config_word(0x40, ~0x100, ~0x200);            /* 0x23cf RMW [a1+8] */
        *(volatile u32 *)0x4000 |= (1u << 20);        /* 0x241a: set iram_ctrl_flush_en */
    } else {
        /* --- HW-DECODE MODE (default on v3/v4) --- */
        log("S: NX in HW Decode mode");                          /* @0xf1e */
        state[0x85070] = 1;                           /* 0x2436: HW-active flag */
        *(volatile u32 *)0x4000 = hw_decode_control;  /* 0x2444: write disable_hw_decode/ordering */
    }
}
```

> **NOTE — the runtime/host side that supplies the flag.** The host runtime writes the
> selector `disable_hw_decode = CSR 0x4000[0]` per engine; its policy default is **HW
> decode ON** on v3/v4 (the host config default is non-zero for CAYMAN/MARIANA), forced
> **off** under strict ordering, and **unsupported on v2/SUNDA**. So the firmware *reads*
> what the host *sets*; the firmware's BEGIN branch above is the device-side consumer of
> that boot flag. The host-side CAM/profiler programming that rides the same CSR bundle is
> [HW-Decode CAM-Table Programming](../../runtime/hw-decode-cam-programming.md) *(Part 8
> forward-link — not yet authored at time of writing)*. `[the BEGIN branch HIGH/OBSERVED;
> the host polarity CARRIED from the runtime cross-reference]`

### 3b. The two complementary state flags

| Flag | Location | Sunda-mode arm | HW-Decode arm |
|---|---|---|---|
| Mode flag | `state[0x855e0+108]` bit0 | set `1` | set `0` |
| HW-active flag | `state[0x85070]` | left `0` | set `1` |
| `hw_decode.control` | CSR `0x4000` | bit20 set (IRAM-ctrl flush) | full control word written |

The mode flag `state[0x855e0+108]` is re-read on the outer loop (`0x24bf`), the
resume-from-pause path (`0x4bde`), and `enter_run` (`0x556c`) — see
[main-loop.md §2/§7](main-loop.md#2-level-1--the-outer-boot-loop-0x2499--0x24e9). The
HW-active flag `state[0x85070]` is the SW-vs-HW cache-fill fork the redirect machine reads
([fetch-pc-redirect.md §4](fetch-pc-redirect.md#4-the-pc-redirect-execution-machine-0x5750)).
`[HIGH/OBSERVED — each BEGIN arm sets exactly one of the two flags]`

### 3c. How the two FSMs are invoked — the run dispatcher `@0x4c79`

```
4c79:  call8 0x2c64       ; enter_run (+ the Sunda FSM, runs to quiescence)
4c7c:  call8 0x31ac       ; the HW-Decode FSM
4c85:  const16 a2,8 ; l32i ; beqi a2,2,0x4ca1   ; read nx.run_state, paused-handling
```

> **GOTCHA — both FSM entry points are *called* on the run path; they are not
> mutually-exclusive at the call site.** The dispatcher invokes `enter_run` (which spins
> the Sunda FSM to quiescence) **and then** `call8 0x31ac` (the HW-Decode FSM). The
> *actual* per-iteration work is gated **inside** each FSM by the boot mode flag: the
> HW-Decode FSM's entry predicate `0x31b7 beqz.n a5,0x31cb` and the Sunda body's `a5`
> gate select whether that body's loop runs or no-ops. So a reimplementation must call
> both entry points and let each no-op when it is not the active mode — *not* branch to
> one FSM at the dispatcher. The honest reading: the boot flag + `hw_decode.control`
> select which path does real work; the dispatcher invokes both and each quiesces
> immediately when not its mode. `[HIGH that both are called / MED on the exact `a5`
> predicate source, §4]`

---

## 4. The MED residual — the FLIX-desync mode-gate at `0x31ac`

One step in the HW-Decode FSM body is **not** instruction-exact, and it is flagged
honestly here rather than overstated.

The per-FSM **mode-gate predicate** — the `beqz.n a5` that decides whether the HW-Decode
body's loop runs — sits at the FSM entry. Its scalar form decodes cleanly
(`0x31b7 beqz.n a5,0x31cb`), but the **source of `a5`** (which boot-flag bit/register it
reads, and the iter-increment data flow around `0x31f0`) lives in an 8-byte FLIX bundle
(`@~0x31b2`, format-N2) whose slot opcode is past the shipped decoder's slot-table
coverage. The native `xtensa-elf-objdump` invoked as a scalar disassembler mis-decodes
this bundle (it emits `.byte 0xcf`/`.byte 0xa3` at the bundle boundary and phantoms 3–4
ops); a FLIX-aware sweep resyncs the *bundle length* (8 bytes, clean resync on both
sides) but the predicate's register-source slot decodes to `<undef>`.

> **NOTE (MED/INFERRED) — the un-decoded gate does NOT reopen the table binding.** The
> single un-decoded hop is the *boot-flag-value → which-FSM-does-work* gate. It selects
> the **mode**, and the **mode→table** binding is already byte-fixed in scalar code
> (§6): the HW-Decode FSM hard-codes `const16 a3,0x0adc @0x36ce`, the Sunda FSM hard-codes
> `const16 a3,0x0814 @0x2e6e`. The un-decoded gate **cannot re-route an FSM to the other
> table** — each FSM builds its own table base in fully-decoded scalar instructions,
> regardless of how the gate slot reads the flag. So the residual is a documented FLIX
> slot-table-coverage wall on the *data flow of the device data step* in the `0x31ac`
> body, not a fact missing from the binding. Tagged **MED/INFERRED** and **`[BOUNDED]`**,
> per [correction-ledger §9](../../reference/correction-ledger.md). `[MED/INFERRED]`

---

## 5. Per-mode dispatch-table difference

The two FSMs index **two parallel direct-indexed dispatch tables** that are co-resident in
one image and **abut** in DRAM. The LOWER table `@0x80814` is fully enumerated by
[dispatch-hub.md](dispatch-hub.md); the HIGHER table `@0x80adc` is characterized **the same
way here** (entry count, real/default split, decode idiom), re-parsed from `dram.bin` this
session.

### 5a. The HIGHER table `@0x80adc` — characterized

Re-parsed directly from `dram.bin` offset `0xadc`: **178 × 4-byte LE absolute IRAM
targets**, span `0xadc..0xda4` (712 B); index = `opcode_byte − 0x41`; default = `0x3a04`.
**55 real / 123 default**, all 55 real targets `< 0x1c820` (in-range IRAM). The real-slot
*set* is identical to the LOWER table's (the same 55 opcodes carry handlers, since the
per-opcode bodies are shared). `[HIGH/OBSERVED]`

The decode idiom is byte-identical to the LOWER table's, re-decoded this session from
`iram.bin` at `0x36b0..0x36d9` (raw bytes `2267f9 b227f9 … 22c2bf 32a0b1 27b302 46cd00
340800 34dc0a 3022a0 2802 a00200`):

```
36b0:  s32i a2,[a7+0x3e4] ; l32i a11,[a7+0x3e4]   ; a7-frame (HW-decode FSM stack frame)
36b6:  const16 a10,8 ; a10,0xe38 ; call8 0x18b84  ; LOG "S: Dispatch opcode=0x%x" (SAME log)
36bf:  l32i a2,[a7+0x3e4]                          ; opcode word
36c2:  addi a2,a2,-65                              ; index = opcode_byte - 0x41   (SAME idiom)
36c5:  movi a3,177                                 ; bound (178 entries 0..177)   (SAME)
36c8:  bgeu a3,a2,0x36ce                           ; single unsigned bound (both ends)  (SAME)
36cb:  j 0x3a04                                    ; DEFAULT (HW-decode Bad-Opcode arm)
36ce:  const16 a3,8 ; a3,0xadc                     ; *** a3 = DRAM 0x80adc (HIGHER table)
36d4:  addx4 a2,a2,a3 ; l32i.n a2,[a2] ; jx a2     ; -> HW-decode trampoline (0x38xx)
```

> **QUIRK — the *only* differences between the two dispatch sites are the operand frame,
> the table base, and the default arm.** The HW-Decode site reads its opcode word from the
> **a7-frame** (`l32i [a7+0x3e4]`) rather than the Sunda site's `a4` cursor — the a7-frame
> is the HW-Decode FSM's stack frame, the same `entry a1,0x430` frame the FSM head builds.
> The `addi −65`, the `movi 177`/`bgeu` bound, the `addx4`/`l32i`/`jx` indexing, and the
> per-fetch `"S: Dispatch opcode=0x%x"` log (DRAM `0x80e38`, the *same* string) are
> **identical** to the Sunda site. Only `const16 0x0adc` (vs `0x0814`) and `j 0x3a04`
> (vs `0x3198`) differ. `[HIGH/OBSERVED]`

### 5b. The two tables contrasted

| Property | **Sunda table** (LOWER) | **HW-Decode table** (HIGHER) |
|---|---|---|
| DRAM base | `0x80814` (`dram.bin` `0x814`) | `0x80adc` (`dram.bin` `0xadc`) |
| Entry count | **178** (`0x41..0xf2`, idx = byte − `0x41`) | **178** (same) |
| Real / default | **55** real / **123** default | **55** real / **123** default |
| Default target | `0x3198` | `0x3a04` |
| Default reload reg | `a4` (`l8ui [a4+0]`) | `a6` (`l8ui [a6+0]`) |
| Default → ErrorHandler | `0x13f58` "Bad Opcode" → spin | `0x13f58` "Bad Opcode" → spin (**SAME**) |
| Decode idiom | `addi −65 ; bgeu 177 ; addx4/l32i/jx` | `addi −65 ; bgeu 177 ; addx4/l32i/jx` (**SAME**) |
| Dispatch site | `0x2e5f` (`a4` cursor) | `0x36c2` (**a7-frame** `[a7+0x3e4]`) |
| Trampoline block | `0x2e79..0x3190` (the `0x30xx` block) | `0x36dc..0x39fc` (the `0x38xx` block) |
| `table[0]` (`'A'`) | `0x3074` | `0x38dd` |
| Trampoline shape | `call8 <impl> ; j 0x31a3` | `call8 <impl> ; j 0x3a0f` |
| Handler back-edge | `j 0x31a3` → `0x2d81` (Sunda loop) | `j 0x3a0f` → HW-decode loop |
| Per-opcode impls | **SHARED** with HIGHER | **SHARED** with LOWER |

### 5c. The shared-impl subtlety

Both `table[0]` trampolines (`0x3074` Sunda / `0x38dd` HW-decode) were re-decoded this
session and **both call the same impl `0x2124`** (`'A'` = Tensor-Tensor), differing only
in the back-edge:

```
3074 (Sunda):     call8 0x2124  ;  j 0x31a3     ; e5 0a ff | 06 4a 00
38dd (HW-decode):  call8 0x2124  ;  j 0x3a0f     ; 65 84 fe | c6 4a 00
```

> **CORRECTION — the two MODES differ *only* in the fetch FSM + the trampoline back-edge;
> the per-opcode `Handler::execute()` bodies are SHARED.** A reader might assume the two
> tables route to two *different* handler implementations. They do not: the LOWER and
> HIGHER trampolines for the same opcode call the *same* impl (`0x2124` for `'A'`),
> wrapped only by a different loop back-edge (`0x31a3` vs `0x3a0f`). The dispatch HUB
> (`Handler`/`execute()` machinery, [dispatch-hub.md](dispatch-hub.md)) is common to both
> modes; only the front-end FSM and the trampoline/back-edge wrapper are per-mode. A
> reimplementation builds **one** set of handler bodies and **two** trampoline blocks
> over them. `[HIGH/OBSERVED — both entry-0 trampolines re-decoded, same impl `0x2124`,
> mirrored back-edges]`

---

## 6. The O1 resolution — HIGHER `@0x80adc` = HW-Decode (the page's spine)

The corpus carried one live contradiction (O1) about *which physical table slot* is the
HW-Decode path. It is **RESOLVED** here, and the prior mis-label is folded as an explicit
**CORRECTION**.

> **CORRECTION — the "lower = HW-Decode" label was a positional authorship artifact;
> HIGHER `@0x80adc` is HW-Decode.** Three earlier MARIANA carve reports labelled the
> **lower-address** dispatch site/table "SITE A = HW-Decode" and the **higher** "Sunda",
> uniformly across engines — but **by IRAM position only**, with no FSM-trait proof
> ("SITE A" is simply the first dispatch site encountered in IRAM address order). The
> binary inverts that label. The **resolved binding**:
>
> | Table | DRAM base | FSM | Mode |
> |---|---|---|---|
> | **LOWER** | `0x80814` | `0x2d81` (SW cursor, `pending_redirect`, no coherence telemetry) | **Sunda-mode** (SW fallback) |
> | **HIGHER** | `0x80adc` | `0x31ac` (a7-frame, iter counter, RTL_PC_check coherence, fetch-gate) | **HW-Decode** (FIFO-assisted) |
>
> The IMG "lower = HW-Decode" label is **refuted as a positional artifact**, per
> [correction-ledger §9](../../reference/correction-ledger.md). `[HIGH/OBSERVED]`

**The binary evidence that HIGHER = HW-Decode** — a whole-IRAM `const16`-immediate
uniqueness census, re-run this session over the 116,768-byte `iram.bin`:

| String (DRAM imm) | const16 sites | Location | Body |
|---|---|---|---|
| `"Seq Loop, iter=0x%x"` (`0x1ad1`) | **exactly 1** | `@0x31d5` | HW-decode FSM only |
| `"RTL_PC_check_delta…"` (`0x1ae9`) | **exactly 1** | `@0x326e` | HW-decode FSM only |
| `"Dispatch opcode=0x%x"` (`0x0e38`) | **exactly 2** | `@0x2e57` + `@0x36b9` | one per FSM |
| table base `0x0814` | **exactly 1** | `@0x2e6e` | Sunda dispatch site |
| table base `0x0adc` | **exactly 1** | `@0x36d1` | HW-decode dispatch site |

The reasoning chain, each link byte-grounded:

1. The host runtime proves **HW-Decode = the hardware-FIFO-assisted path** (the chicken
   bit `disable_hw_decode = CSR 0x4000[0]` selects "FIFO vs HW-decode path"; default ON on
   v3/v4). `[CARRIED from the runtime cross-reference]`
2. The `RTL_PC_check_delta` telemetry is the **HW-FIFO ↔ SW-cache coherence** reconciliation
   (`curr_dma_idx_vld` vs `tgt_cache_idx`). The census finds it at **exactly one** site
   `@0x326e`, inside the `0x31ac` body. So the FSM carrying the HW-FIFO coherence telemetry
   **is** the HW-Decode FSM. `[HIGH/OBSERVED]`
3. That same `0x31ac` body builds **`0x80adc`** (`const16 a3,0x0adc @0x36d1`, the only such
   site) and indexes the HIGHER table. The Sunda FSM `0x2d81` has *neither* the iter log
   nor the RTL_PC_check, and builds **`0x80814`** (the only `0x0814` site `@0x2e6e`).
   `[HIGH/OBSERVED]`

Therefore: **HIGHER `0x80adc` = HW-Decode**, **LOWER `0x80814` = Sunda**. The lone MED
residual (§4) — the un-decoded boot-flag → gate-predicate register — does **not** affect
this binding, because each FSM hard-codes its own `const16` table base in scalar code.

> **NOTE — cross-engine corroboration.** The same lower=Sunda / higher=HW-Decode geometry
> appears on a second CAYMAN engine and on later generations: MARIANA POOL builds the dual
> tables at `0x80800` (LOWER) `@0x2d2e` and `0x80ac8` (HIGHER) `@0x35b4`; MARIANA_PLUS at
> `0x80800` `@0x2d51` and `0x80ac8` `@0x360f` (re-parsed this session). The base addresses
> shift across gens (and the opcode normalization base shifts on some engines) but the
> lower=Sunda / higher=HW-Decode structure is stable. `[HIGH/OBSERVED on the addresses;
> the per-engine normalize-base shift CARRIED from the IMG cross-references]`

---

## 7. Per-generation presence

The dual front-end is a **CAYMAN(v3)-onward** construct. SUNDA(v2) predates it and is
**monolithic** — the decisive discriminator that proves the split is per-runtime-mode,
not per-generation. All four POOL DEBUG images were carved and probed this session.

### 7a. The SUNDA(v2) negative control

`img_SUNDA_NX_POOL_DEBUG` (iram **59,600 B**, sha256 `d97d1a8e…`) — **none** of the
dual-mode construct exists:

| Probe | SUNDA result |
|---|---|
| `"NX in Sunda mode"` | **0** (absent) |
| `"NX in HW Decode mode"` | **0** (absent) |
| `"Sunda seq Loop"` | **0** (absent) |
| `"Seq Loop, iter"` | **0** (absent) |
| `"RTL_PC_check"` | **0** (absent) |
| `sunda_fetch` / `sunda_handle_surprises` | **0** / **0** (uses `fast_fetch` / `handle_surprises` instead, **1** each) |
| `addi a2,a2,-65` dispatch sites | **0** |
| `addi a2,a2,-48` dispatch sites | **0** |
| `const16 *,0xadc` table-base | **0** |

> **CORRECTION — SUNDA(v2) is NOT "the Sunda-mode firmware".** If "Sunda-mode" *were* the
> SUNDA generation, the SUNDA image would *be* the Sunda-mode firmware. It is not: the
> SUNDA SEQ engine has **no** direct-indexed dispatch table, **no** dual mode, and names
> its software-fetch/surprises functions plainly (`fast_fetch`, `handle_surprises`). The
> CAYMAN+ build renamed those to `sunda_fetch` / `sunda_handle_surprises` precisely *when
> it added the HW-decode path*, to mark the retained legacy path as "the old Sunda-style
> fetch". So "Sunda-mode" is a CAYMAN+ runtime fallback name, not the silicon generation —
> [correction-ledger §8](../../reference/correction-ledger.md). This single fact kills the
> per-generation hypothesis. `[HIGH/OBSERVED]`

### 7b. The per-gen presence table

Re-probed this session across the four POOL DEBUG carves (`addi-65`/`addi-48` =
dispatch-site bytes; mode strings + `RTL_PC_check` = dual-FSM markers):

| GEN (NC-ver) | iram size | `addi-65` / `-48` | `RTL_PC_check` | both mode strings | dual table | Conf |
|---|---|---|---|---|---|---|
| **SUNDA** (v2) | 59,600 B | 0 / 0 | 0 | NO | **NO** (monolithic) | `[HIGH/OBSERVED]` |
| **CAYMAN** (v3) | 116,768 B | 2 / 1 | 2 | YES | YES (`0x80814` + `0x80adc`) | `[HIGH/OBSERVED]` |
| **MARIANA** (v4) | 114,816 B | 2 / 1 | 2 | YES | YES (`0x80800` + `0x80ac8`) | `[HIGH/OBSERVED]` |
| **MARIANA_PLUS** (v4+) | 119,616 B | 2 / 1 | 2 | YES | YES (`0x80800` + `0x80ac8`) | `[HIGH/OBSERVED]` |
| **MAVERICK** (v5) | — | — | — | — | — | `[INFERRED — no NX image in archive]` |

- **CAYMAN/MARIANA/MARIANA_PLUS** each carry **both** mode strings (`@0xef5`/`@0xf1e` on
  CAYMAN; `@0xeee`/`@0xf17` on MARIANA_PLUS), **both** FSM-loop strings, `sunda_fetch` +
  `sunda_handle_surprises`, the 2 `addi-65` dispatch sites + 1 `addi-48`, and 2
  `RTL_PC_check` references. `[HIGH/OBSERVED]`
- **MAVERICK(v5)** ships **no** NX firmware image in this archive (twin-only); its
  front-end mode shape is **not observable** here and is **not claimed** — `[INFERRED]`
  only by extrapolation from the v3/v4 pattern, never byte-grounded.

> **GOTCHA — the per-engine `addi`-normalize-vs-raw-compare table shape is orthogonal to
> the mode.** On engines that normalize with `addi −N` (POOL/ACT/DVE), *both* mode-sites
> use the same `addi`; on engines that use a raw-opcode compare chain (PE), *both*
> mode-sites do — the table-shape choice is **per-engine**, applied identically to both
> the Sunda and HW-Decode sites of that engine. Do not read the `addi-vs-raw` distinction
> as a mode distinction. `[CARRIED from the IMG cross-references]`

---

## 8. Reimplementation checklist

To rebuild the SEQ dual fetch front-end:

1. **Two FSM bodies** over a shared skeleton (§2): a Sunda SW-fetch FSM (cursor `a4`,
   `pending_redirect 0x6b0c`, no iter counter, no coherence telemetry, back-edge `0x31a3`)
   and a HW-Decode FSM (a7-frame, iter counter at `state[a7+0]`, `fetch_gate 0x6e60`,
   `RTL_PC_check_delta` coherence reconciliation, back-edge `0x3a0f`). Mirror the
   poll-surprises branch sense (Sunda `beqz` exit / HW-decode `bnez` continue) — same
   helper `0x6af4`, two sites.
2. **Boot mode-pick** (§3): read the args flag bit0 → `state[0x855e0+108]`;
   `bbci flag,0` selects. Sunda arm sets `hw_decode.control[20]` (IRAM-ctrl flush);
   HW-Decode arm sets `state[0x85070]=1` and writes `hw_decode.control` (CSR `0x4000`;
   `disable_hw_decode` = bit0 is the host-set chicken bit). Call *both* FSM entry points on
   the run path (`0x4c79`); each no-ops when not its mode.
3. **Two parallel dispatch tables** (§5), abutting in DRAM: LOWER `0x80814` (Sunda,
   default `0x3198`) and HIGHER `0x80adc` (HW-Decode, default `0x3a04`), each **178 × 4-byte
   absolute IRAM targets**, **55 real / 123 default**, index = `opcode − 0x41`, single
   `bgeu 177` bound. Identical decode idiom; only the table base, default arm, and operand
   frame differ.
4. **One set of handler bodies, two trampoline blocks** (§5c): the same per-opcode
   `Handler::execute()` impls (e.g. `0x2124` for `'A'`) wrapped by two trampoline blocks
   (`0x30xx` Sunda → `j 0x31a3`; `0x38xx` HW-decode → `j 0x3a0f`). Do **not** duplicate the
   handler bodies per mode.
5. **HW resource** (§2c): in HW-Decode mode the hardware instruction-decode FIFO feeds the
   fetch (gated by `disable_hw_decode`), and the FSM supervises it with the fetch-gate +
   the RTL_PC_check coherence reconciliation. In Sunda mode there is no FIFO — a pure
   software cursor fetch. The cache backend follows (HW-indexed + HW IRAM-DMA vs SW
   associative scan + DramRing).
6. **The binding** (§6): HIGHER `0x80adc` = HW-Decode, LOWER `0x80814` = Sunda — *not* the
   reverse. The HW-decode-only telemetry markers are byte-unique to the FSM that builds the
   HIGHER table.
7. **No SUNDA(v2) dual front-end** (§7): a v2-targeting rebuild has a single monolithic
   fetch path (`fast_fetch`/`handle_surprises`, no dispatch table) — the dual construct
   begins at v3.

---

## 9. Adversarial self-verification

Five strongest claims, each re-challenged against the re-carved images this session
(carve SHAs match: `cayman_iram 8e4412b9…`, `cayman_dram 7bdf6ed7…`; SUNDA/MARIANA/M+
carved fresh):

| # | Claim | Challenge | Verdict |
|---|---|---|---|
| 1 | **HIGHER `0x80adc` = HW-Decode** (O1 resolution) | re-disassemble `0x36b0..0x36d9`; run whole-IRAM const16 census for the telemetry strings | **HOLDS.** `0x36ce const16 a3,0x0adc` confirmed; iter-log `0x1ad1` = **1** site `@0x31d5`, RTL_PC_check `0x1ae9` = **1** site `@0x326e`, **both** in the `0x31ac` body that builds `0x80adc`. The Sunda FSM `0x2d81` builds `0x80814` (`0x0814` = **1** site `@0x2e6e`) and has neither marker. OBSERVED. |
| 2 | **HIGHER table = 178 entries, 55 real / 123 default, default `0x3a04`** | `struct.unpack` 178×u32 from `dram.bin[0xadc:]`; count `!= 0x3a04`; range-check | **HOLDS.** Parsed 178; **55** real / **123** default; **0** real targets `≥ 0x1c820`. Tables abut: LOWER `0x814..0xadc` (712 B), HIGHER starts at `0xadc`. `table[0]` = `0x38dd`. OBSERVED. |
| 3 | **Two FSMs, mirrored poll, shared impls** | `rg -c 'call8 0x6af4'`; decode both `table[0]` trampolines | **HOLDS.** Exactly **2** `call8 0x6af4`: `@0x2d81` (Sunda, `beqz` exit) / `@0x31c0` (HW-decode, `bnez` continue). Trampolines `0x3074` (`call8 0x2124; j 0x31a3`) and `0x38dd` (`call8 0x2124; j 0x3a0f`) — **same impl `0x2124`**, mirrored back-edges. OBSERVED. |
| 4 | **BEGIN mode-pick** `bbci flag,0` → Sunda sets `0x4000[20]` / HW-decode sets `state[0x85070]=1` + writes `0x4000` | re-decode `0x23af..0x2453` | **HOLDS.** `0x23af s8i a2,[a3+108]`; `0x23b5 bbci a2,0,0x2422`; Sunda arm LOG `@0xef5`, `0x241a` sets `0x4000` bit20 (`movi 1; slli 20; or`); HW-decode arm LOG `@0xf1e`, `0x2436` sets `state[0x85070]=1`, `0x2444` writes `0x4000`. Strings `@0xef5`/`@0xf1e` dereferenced byte-exact. OBSERVED. |
| 5 | **SUNDA(v2) has neither mode** (per-gen, not per-runtime) | probe SUNDA carve for mode strings + dispatch-site bytes | **HOLDS.** SUNDA iram **59,600 B**: `addi-65`=0, `addi-48`=0, `const16 *,0xadc`=0, `RTL_PC_check`=0, both mode strings=0; uses `fast_fetch`(1)/`handle_surprises`(1). CAYMAN/MARIANA/M+ each: 2/1 dispatch sites, 2 RTL_PC_check, both mode strings. OBSERVED. |

> **CORRECTION folded in during self-verify.** The HW-Decode default arm `0x3a04` and the
> FSM-entry mode-gate bundle `@~0x31b2` decode as garbage under a naive linear sweep
> (mid-bundle entry / FLIX desync). The §5b default path is recovered from an *aligned*
> decode (`0x3a04: l8ui a10,[a6+0]; call8 0x13f58; j 0x3a0f` — note the **a6** reload, vs
> the Sunda default's `a4`); the §4 mode-gate `a5`-source is honestly flagged MED/INFERRED
> across the desync. Both noted in place, never silently smoothed.

---

## 10. Honesty ledger

**HIGH / OBSERVED (re-run this session):**

- Carve reproduced: `cayman_iram` 116,768 B / sha256 `8e4412b9…`, `cayman_dram` 28,448 B /
  sha256 `7bdf6ed7…`; SUNDA (59,600 B), MARIANA (114,816 B), MARIANA_PLUS (119,616 B)
  carved fresh — per-gen IRAM sizes confirmed.
- Both dispatch sites re-decoded byte-exact: Sunda `0x2e5f` builds `0x80814` (default
  `0x3198`); HW-decode `0x36c2` (a7-frame `[a7+0x3e4]`) builds `0x80adc` (default `0x3a04`).
- Both tables re-parsed: 178 entries each, 55 real / 123 default, all real in-range; tables
  abut; `table[0]` = `0x3074` (LOWER) / `0x38dd` (HIGHER).
- Both trampolines re-decoded: both call **shared impl `0x2124`**, mirrored back-edges
  `0x31a3` (Sunda) / `0x3a0f` (HW-decode).
- The uniqueness census: iter-log `0x1ad1` = 1 site `@0x31d5`; `RTL_PC_check 0x1ae9` = 1
  site `@0x326e` (both HW-decode-only); dispatch-log `0x0e38` = 2 sites (one per FSM);
  table-base `0x0814` / `0x0adc` = 1 site each.
- Two `poll-surprises` callers (`0x6af4` `@0x2d81` Sunda / `@0x31c0` HW-decode), mirrored
  `beqz`/`bnez`; one `fetch-gate 0x6e60` site `@0x31f4` (HW-decode-only); one
  `pending_redirect 0x6b0c` site `@0x2d9a` (Sunda-only).
- BEGIN mode-pick `0x23b5 bbci flag,0`: Sunda arm sets `0x4000[20]`; HW-decode arm sets
  `state[0x85070]=1` + writes `0x4000`. Mode strings `@0xef5`/`@0xf1e` dereferenced exact.
- Run dispatcher `0x4c79` calls **both** FSMs (`enter_run 0x2c64` + HW-decode `0x31ac`).
- SUNDA negative control: 0 mode strings, 0 dispatch-site bytes, monolithic
  `fast_fetch`/`handle_surprises`.
- MARIANA / MARIANA_PLUS dual tables at `0x80800` + `0x80ac8`.

**MED / INFERRED:**

- The HW-Decode FSM entry **mode-gate predicate** `a5` *source* (the `0x31b7 beqz.n a5`
  decodes clean, but the bundle `@~0x31b2` carrying `a5`'s source slot is FLIX-desync,
  `<undef>` slot) and the iter-increment data flow around `0x31f0` — the §4 residual.
  Does **not** affect the table binding (each FSM hard-codes its `const16` table base in
  scalar code). `[BOUNDED]`
- That both FSM entry points are *called but each no-ops when not its mode* — the call
  sites are HIGH; the exact per-FSM internal mode-gate predicate is the desynced step.
- The "sunda_* = deliberate legacy-Sunda-style fallback name" interpretation (strong, from
  SUNDA=monolithic + the CAYMAN+ rename coinciding with the new HW-decode path; not a
  comment-proof).

**CARRIED (not re-derived this session):**

- The host-side polarity (`disable_hw_decode = CSR 0x4000[0]`; HW decode default ON on
  v3/v4; unsupported on v2) — from the runtime cross-reference and
  [correction-ledger §8/§9](../../reference/correction-ledger.md).
- The cache backend HW/SW split (descr[+60]) — [iram-cache.md](iram-cache.md).
- The per-engine `addi`-normalize-vs-raw-compare table shape — from the IMG cross-refs.

**LOW / NOT CLAIMED:**

- MAVERICK(v5) NX front-end mode shape — no NX image in this archive; `[INFERRED]` only.
- The exact RTL semantics of the HW decode FIFO behind `disable_hw_decode` (RTL-side).
- The CSR aperture absolute-base alias (`0x0` vs `0x00400000`) — inherited LOW, never
  changes a register identity.

---

## See also

- [SEQ Main FSM Loop](main-loop.md) — the three-level nested FSM structure the two bodies
  (3a Sunda / 3b HW-decode) live in; the `a5` mode predicate that gates Level 3a vs 3b.
- [SEQ Decode / Dispatch Hub](dispatch-hub.md) — the LOWER table `0x80814` fully enumerated
  (55 real / 123 default) and the `Handler`/`execute()` machinery both modes share.
- [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — the Sunda software-fetch body
  instruction-exact (the SW path this page contrasts against the HW-Decode FSM).
- [HW-Decode CAM-Table Programming](../../runtime/hw-decode-cam-programming.md) — the
  host-side profiler-CAM/table programming over the same `hw_decode` CSR bundle *(Part 8
  forward-link — **not yet authored** at time of writing)*.
- [The Do-Not-Repeat Correction Ledger](../../reference/correction-ledger.md) — §8
  ("Sunda-mode" ≠ SUNDA gen) and §9 (the O1 polarity resolution) this page implements.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tagging used throughout.
