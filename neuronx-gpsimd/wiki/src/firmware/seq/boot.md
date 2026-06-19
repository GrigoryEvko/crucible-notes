# SEQ Boot / Entry Path

This page reconstructs the **device-side boot/entry path of the NX *SEQ* engine** — the
instruction sequencer that fronts the TPB engines on the Cayman GPSIMD/Neuron SoC — from the
architectural reset vector at IRAM `0x0`, through the Cadence C-runtime `_ResetHandler`, into
the application `BEGIN`, and out the far side into the `enter_run` reset→run handshake that the
[main FSM loop](main-loop.md) spins on. Everything here is **byte-pinned to a shipped artifact
disassembled this session**: the carved `CAYMAN_NX_POOL` *DEBUG* firmware image (IRAM + DRAM
`.rodata`), decoded with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
`ConfigName=Xm_ncore2gp`, uarch "Cairo", `TargetHWVersion=NX1.1.4`), every log line resolved
against the firmware's own DRAM string image, and every CSR offset cross-referenced to the
`tpb_xt_local_reg` bundle map. Where prompt-level folklore or a sibling note disagrees with the
disassembly, **the binary wins**, and an in-place **CORRECTION** callout says so.

Confidence tags follow the project model: `OBSERVED` = a byte/string/instruction read from a
shipped artifact this session; `INFERRED` = reasoned over OBSERVED facts; `CARRIED` =
consolidated from a cited cross-page anchor; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a
naive reading), **NOTE** (orientation).

> **NOTE — which core this is, and which it is *not*.** The SEQ engine is the NX-core
> instruction sequencer; its firmware emits log lines prefixed **`S:`** (`S: BEGIN on cayman`,
> `S: enter_run: start`). It is a *distinct core and firmware* from the Q7/GPSIMD POOL compute
> engine, whose firmware emits **`P%i:`** lines and is documented in
> [Boot / Reset Sequence + Startup Config](../../uarch/boot-reset.md). The two prefixes are
> mutually exclusive in every image (verified: the `CAYMAN_NX_POOL` DEBUG DRAM holds **187**
> `S:` strings and **0** `P%i:`; the `CAYMAN_Q7_POOL` DEBUG DRAM holds **0** `S:` and **156**
> `P%i:`). When this page says "the SEQ engine releases the Q7 cores from run-stall", it is the
> NX sequencer reaching across to the eight Q7 compute sequencers — the handoff the uarch page
> describes from the Q7 side. `[HIGH/OBSERVED]`

---

## 0. The boot spine in one diagram

```
  ┌──────────────────── NX SEQ-engine on-device boot ────────────────────┐
  │  reset:  PC = IRAM 0x0   [NX run-stall already released by host/RTL]  │
  │     │                                                                 │
  │     ▼  reset vector (IRAM 0x0)                                        │
  │     j 0x1dc                                                           │
  │     │                                                                 │
  │     ▼  boot stub @0x1dc                                               │
  │     const16 a0,0x90 ; jx a0           ; -> _start                     │
  │     │                                                                 │
  │     ▼  _start / _ResetHandler @0x90   (Cadence Xtensa24 crt)          │
  │     ├─ wsr.isb a4=0x4ec0       ; instruction-cache state init         │
  │     ├─ wsr.vecbase a2          ; program VECBASE                      │
  │     ├─ iii loop 0xb9..0xc8     ; I-cache INVALIDATE 0x1000 region     │
  │     ├─ wsr.memctl 0xff08       ; enable I$/D$ ways                    │
  │     ├─ wsr.wb (1<<30)          ; initial WindowBase                   │
  │     ├─ MPU program 0x104..0x149; wptlb loop, wsr.mpuenb               │
  │     ├─ MEMCTL |= 0x08 @0x191   ; second way-enable RMW                │
  │     └─ call0 0x190c            ; -> crt0 C startup                    │
  │           │                                                           │
  │           ▼  crt0 startup @0x190c                                     │
  │           ├─ wsr.isl / wsr.ps 0x80 ; stack + PS init                  │
  │           ├─ __init_array ctors  (0x84cbc..0x84cc4)                   │
  │           ├─ const16 a12,0x85040 ; args/config struct                 │
  │           └─ call8 0x2378        ; *** -> BEGIN(args) ***             │
  │                 │                                                     │
  │                 ▼  BEGIN @0x2378  ("S: BEGIN on cayman")              │
  │                 ├─ mode pick: Sunda vs HW-Decode (state[+108])        │
  │                 ├─ program hw_decode.control (CSR 0x4000)             │
  │                 ├─ init chain (IRAM-cache, engine self-discovery)     │
  │                 └─ fall into RUN LOOP @0x2499                         │
  │                       │                                               │
  │                       ▼  run loop  2499..24e9  (j 0x2499 back-edge)   │
  │                       └─ call8 0x2c64  ; *** -> enter_run ***         │
  │                             │                                         │
  │                             ▼  enter_run @0x2c64                      │
  │                             ├─ "S: Pending StartCtrl"                 │
  │                             ├─ POLL nx.start_ctrl (CSR 0x0004)  <─── host asserts StartCtrl
  │                             ├─ "S: enter_run: start"                  │
  │                             ├─ read hw_decode / breakpoint CSRs       │
  │                             ├─ ACK start_ctrl=0 ; nx.run_state=1      │
  │                             ├─ dispatch                               │
  │                             └─ "S: enter_run: done" ; retw -> loop    │
  └───────────────────────────────────────────────────────────────────────┘
```

One-line verdict: a **textbook Cadence Xtensa24 reset handler** (ISB/VECBASE arm → I-cache
invalidate → MEMCTL/WB prime → MPU program → cache-enable → `call0` crt0) into a **crt0
C-runtime** (stack/ISL/PS → ctors → `call8 BEGIN`) whose `BEGIN` picks the runtime decode mode,
programs the HW-decode control CSR, and falls into a forever run loop that calls `enter_run`
once per iteration; `enter_run` is the **reset→run rendezvous** — it spins on the host-asserted
`nx.start_ctrl` bit, then acks it and publishes `nx.run_state`. Every surface below is OBSERVED
in the carved `CAYMAN_NX_POOL` DEBUG image this session.

---

## 1. Image identification + addressing model

The SEQ boot firmware is **not** in any of the dynamically-loaded GPSIMD custom-op kernel images
(those link `.text@0x01000000`, carry a ~`0x1ec`-byte `.rodata` with no log strings, no boot
stub, and no `enter_run`). It lives in the per-engine **base** firmware images carried inside the
static archive:

```
extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/
  gpsimd/custom_op/c10/lib/libnrtucode.a
```

as members `img_<GEN>_NX_<ENGINE>_<MODE>_<SEG>_contents.c.o`, where `SEG ∈ {IRAM, DRAM, SRAM,
EXTRAM}`, `ENGINE ∈ {ACT, DVE, PE, POOL, SP}`, `MODE ∈ {PERF, DEBUG, TEST}`. Each `.c.o` is an
**x86 ELF** whose `.rodata` holds the raw device image — the device bytes are reached through a C
accessor (`<NAME>_get` / `<NAME>_get.data`). The only relocation in the `.c.o` is the x86
accessor's PC32 into `.rodata`; **the device bytes are fully linked** (the `const16` operands that
read `0` are real zero-immediates, not unresolved relocations). `[HIGH/OBSERVED]`

**Chosen trace image** (representative; the boot path is byte-identical across NX engines, see
§9.1):

| Member | Role | `.rodata` size |
|---|---|---|
| `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` | IRAM code image | `0x1c820` (116768 B) |
| `img_CAYMAN_NX_POOL_DEBUG_DRAM_contents.c.o` | DRAM string/data image | `0x6f20` (28448 B) |

IRAM head bytes are `06 76 00 00 ... 86 77 00 00` — i.e. `j <reset>` then `j <fault-stub>`.
`[HIGH/OBSERVED]`

### 1.1 Memory map (`ncore2gp-params`)

Read directly this session from `XtensaTools/config/ncore2gp-params`:

| Param | Value | Meaning |
|---|---|---|
| `ISSInstRAMInfo` | `[ 0x10000 0x00000000 … ]` | **IRAM base `0x0`, 64 KiB** |
| `ISSDataRAMInfo` | `[ 0x10000 0x00080000 … ]` | **DRAM base `0x80000`, 64 KiB** |
| `ResetVectorOffset` | `0x00000000` | reset vector at IRAM byte 0 |
| `StaticVectorSelect` / `DynVecBaseReset` | `0` / `0x00000000` | static vectors, base 0 |
| `IsaMaxInstructionSize` | `32` | **FLIX/VLIW** (32-byte max bundle) |
| `TargetHWVersion` | `NX1.1.4` | the NX engine target |

So the IRAM image is raw code at VA `0x0` with the reset vector at byte 0; the DRAM image is raw
data at VA `0x80000`. **A DRAM string's file offset in `dram.bin` is `VA − 0x80000`** — every log
string this page cites is at `dram.bin` offset `X`, i.e. device VA `0x80000+X`. `[HIGH/OBSERVED]`

> **GOTCHA — `.text`/`.rodata` VMA==fileoffset, but the SEQ image uses its own VA map.** Do not
> apply the `ncore2gp` config-DLL `.data` delta (`0x200000`) here, nor libtpu's `0x400000`. These
> are *device firmware images*: IRAM@`0x0`, DRAM@`0x80000`. The only arithmetic you need is
> "DRAM file offset = VA − `0x80000`". `[HIGH/OBSERVED]`

### 1.2 Pointer formation — the `const16` pair (why a literal-pool scan finds nothing)

The firmware builds every 32-bit constant (DRAM string pointers and local CSR offsets alike) with
a **`const16` PAIR**: a "high" `const16` setting bits[31:16], then a `const16` setting bits[15:0].
The CONST16 immediate is **bits[23:8] of the 3-byte little-endian instruction word** — verified
against the boot strings:

```
238b:  a4 08 00   const16 a10, 8        ; LE word 0x0008a4, bits[23:8] = 0x0008  -> a10[31:16] = 0x0008
238e:  a4 e1 0e   const16 a10, 0xee1    ; LE word 0x0ee1a4, bits[23:8] = 0x0ee1  -> a10[15:0]  = 0x0ee1
                                        ;  => a10 = 0x00080EE1  (DRAM VA of "S: BEGIN on cayman")
```

`0x80EE1 − 0x80000 = 0xEE1` is the `dram.bin` offset of the string (confirmed byte-exact, §4.1).
Because pointers are *immediate-built*, a naive 32-bit literal-pool scan of the image recovers
nothing — you must follow the `const16` pairs. `[HIGH/OBSERVED]`

> **NOTE — CSR aperture absolute base is ambiguous; register identity is not.** Local CSR
> accesses use the same pair, e.g. `movi a3,0x400 ; const16 a3,4 → nx.start_ctrl`. The low-16
> offset maps 1:1 onto the `tpb_xt_local_reg` bundle map (`nx@0x0000`, `general@0x1000`,
> `window@0x2000`, `q7@0x3000`, `hw_decode@0x4000`, `notific@0x6000`). The `movi`-supplied high
> half is the same for every CSR access in a function, so the *absolute* aperture base alias
> (`0x0` vs `0x00400000`) is **LOW** confidence, but the **register identity by low offset is
> HIGH** and is what this page reports. `[register-id HIGH/OBSERVED; absolute-base LOW/INFERRED]`

---

## 2. Reset vector + boot stub

```asm
;--- IRAM 0x0: the reset / fault vector table (first ~0x100 bytes) ---
   0:  06 76 00      j 0x1dc          ; ResetVector  -> boot stub
   3:  00 00 00      ill              ; (slot pad)
   6:  86 77 00      j 0x1e8          ; second vector -> fault/halt stub
  ...
  24:  80 01 09      l32e a8, a1, -64 ; windowed-ABI overflow/underflow handlers
  27:  90 01 09      l32e a9, a1, -64 ;   (standard ncore2gp vectors; linear-sweep clean)
```

The reset vector is a single `j 0x1dc`. The hand-written Xtensa vector table that follows holds
the windowed-ABI `l32e`/`s32e` spill handlers and the EXCVADDR-save path. `[HIGH/OBSERVED]`

```asm
;--- BOOT STUB @0x1dc ---
 1dc:  04 00 00   const16 a0, 0          ; a0[31:16] = 0
 1df:  04 90 00   const16 a0, 144        ; a0[15:0]  = 0x90  -> a0 = 0x00000090
 1e2:  a0 00 00   jx a0                  ; -> _start @0x90
 1e5:  00 00 00   ill
 1e8:  00 52 00   halt 0                 ; fault/halt stub (the 2nd vector lands here)
 1ec:  44 08 00   const16 a4, 8          ; --- alt dispatch-table path ---
 1ef:  44 90 4e   const16 a4, 0x4e90     ; a4 = DRAM 0x84e90 (addx4 dispatch table)
 1f2:  24 01 00   const16 a2, 1
 1f5:  24 ac c2   const16 a2, 0xc2ac     ; a2 = 0x1c2ac (default entry)
 1f8:  16 84 00   beqz a4, 0x204
 1fb:  30 30 34   extui a3, a3, 0, 4
 1fe:  ...        addx4 a4, a3, a4       ; index the table by a3
```

The primary boot stub is three instructions: `const16`-pair `a0 = 0x90`, `jx a0`. The alternate
path at `0x1ec` builds `a4 = 0x84e90` (an `addx4` dispatch table in DRAM) and a default entry
`0x1c2ac`. Confirmed: every entry of the DRAM table at file offset `0x4e90` reads `ac c2 01 00` =
`0x0001c2ac`, i.e. the table's default target *is* `0x1c2ac`. `[HIGH/OBSERVED]`

C reconstruction:

```c
/* Reset vector + boot stub. IRAM 0x0 / 0x1dc.  [HIGH/OBSERVED] */
__attribute__((naked, section(".ResetVector"))) void reset_vector(void) {
    asm volatile("j boot_stub");                  /* IRAM 0x0:  j 0x1dc                  */
}

__attribute__((naked)) void boot_stub(void) {     /* IRAM 0x1dc                          */
    void (*start)(void) = (void *)0x00000090;     /* const16-pair: a0 = _start            */
    ((void (*)(void))start)();                    /* jx a0  (tail-jump, no return frame)  */
    /* fault fallthrough: */ asm volatile("halt 0");  /* IRAM 0x1e8 (also the 2nd vector) */
}
```

---

## 3. `_start` / `_ResetHandler` @0x90 — early hardware init

The boot stub jumps to the Cadence Xtensa24 C-runtime reset handler at `0x90`. It decodes
in-sync (no FLIX desync in the crt region). Annotated:

```asm
 90:  const16 a2,0 ; const16 a2,0 ; beqz a2,0x9c ; callx0 a2    ; optional __reset_hook (a2==0 -> skipped)
 9e:  const16 a4,8 ; const16 a4,0x4ec0                          ; a4 = 0x4ec0 (ISB init value)
 a4:  wsr.isb  a4                                               ; instruction-cache state init
 a7:  const16 a2,0 ; const16 a2,0
 ad:  wsr.vecbase a2                                            ; program VECBASE (= 0)
 b0:  const16 a2,0 ; const16 a2,0x1000                          ; a2 = 0x1000  (I$-invalidate bound)
 b6:  movi a3,0
 b9:  iii a3,0   ; iii a3,64 ; iii a3,128 ; iii a3,192          ; invalidate 4 lines (0x100 span)
 c5:  addmi a3,a3,0x100                                         ; advance one 0x100 block
 c8:  bltu a3,a2,0xb9                                           ; loop while a3 < 0x1000  -> invalidate 0x1000
 cb:  isync
 ce:  const16 a2,0xffff ; const16 a2,0xff08                     ; a2 = 0xff08
 d4:  wsr.memctl a2                                             ; enable I$/D$ ways
 d7:  movi.n a2,1 ; slli a2,a2,30                               ; a2 = 1<<30 = 0x40000000
 dc:  wsr.wb a2                                                 ; initial WindowBase
 df:  rsync
 ;--- MPU program (16-entry, two paths) ---
 fd:  wsr.mpuenb a9 (=0)                                        ; disable MPU while programming
104: ... extui/xor/wptlb loop (a10=0..a3), srli/or build mpuenb mask
12e:  wsr.mpuenb a9                                             ; re-enable MPU with computed mask
135: ... wptlb second pass over the foreground entries
185:  rsr.memctl a2 ; const16 a3,8 ; or a2,a2,a3 ; wsr.memctl a2 ; *** I-cache ENABLE (MEMCTL |= 0x08) ***
194:  const16 a2,0 ; const16 a2,0 ; beqz.n a2,0x1a4 ; callx0 a2 ; optional user-init hook (skipped)
1a4:  wsr.ms a0 (=0) ; rsync                                    ; MS <- 0
1aa:  call0 0x190c                                              ; *** enter crt0 C startup ***
```

The handler is the standard `ncore2gp` reset spine: `wsr.isb` → `wsr.vecbase` → I-cache
invalidate → `wsr.memctl` → `wsr.wb` → MPU program → I-cache enable RMW → `call0` crt0.
`[HIGH/OBSERVED]`

> **CORRECTION — the NX SEQ `_ResetHandler` does *not* program `PREFCTL`.** A sketch of this path
> attributed a `wsr.prefctl 0x3044` step to it. Disassembly of `_start` (IRAM `0x90`–`0x1c0`)
> shows **no `wsr.prefctl`** anywhere; the only `prefctl` mnemonics in the whole IRAM image are
> `xsr.prefctl a0` decodes that are byte-pattern collisions inside data/FLIX spans (each preceded
> by a `.byte` desync, e.g. `0x5040`), not real reset-handler writes. The `PREFCTL 0x00003044`
> default belongs to the *sibling Q7 "Cairo" core's* `_ResetHandler`, documented in
> [boot-reset.md §3.4](../../uarch/boot-reset.md). The NX SEQ early-init programs
> ISB/VECBASE/I$-invalidate/MEMCTL/WB/MPU **but not PREFCTL**. `[HIGH/OBSERVED]`

> **CORRECTION — the I-cache invalidate region is `0x1000`, not `0x2000`.** The loop bound is
> `const16 a2,0x1000` (`0xb3`), and the body invalidates 4 cache lines (`iii a3,0/64/128/192`)
> then `addmi a3,a3,0x100` per iteration, looping `bltu a3,a2`. That sweeps exactly **`0x1000`
> bytes**, not `0x2000`. `[HIGH/OBSERVED]`

C reconstruction of the early-init:

```c
/* _ResetHandler @0x90 — Cadence Xtensa24 crt reset spine.  [HIGH/OBSERVED] */
void _ResetHandler(void) {
    if (reset_hook) reset_hook();          /* 0x90: optional, NULL on this image -> skipped */

    wsr_isb(0x4ec0);                       /* 0xa4: instruction-cache state init             */
    wsr_vecbase(0x0);                      /* 0xad: VECBASE = 0                              */

    for (uint32_t a = 0; a < 0x1000; a += 0x100) {   /* 0xb9..0xc8: I-cache INVALIDATE */
        iii(a + 0); iii(a + 64); iii(a + 128); iii(a + 192);
    }
    isync();

    wsr_memctl(0xff08);                    /* 0xd4: enable I$/D$ ways                        */
    wsr_wb(1u << 30);                      /* 0xdc: initial WindowBase                       */
    rsync();

    wsr_mpuenb(0);                         /* 0xfd: disable MPU while programming            */
    mpu_program_foreground_entries();      /* 0x104..0x149: wptlb loop over 32-entry table   */
    /* mpuenb is re-armed mid-loop (0x12e) with the computed region-valid mask               */

    wsr_memctl(rsr_memctl() | 0x08);       /* 0x191: *** I-cache ENABLE ***                  */
    wsr_ms(0); rsync();                    /* 0x1a4                                          */

    crt0_startup();                        /* 0x1aa: call0 0x190c  (no-return, into C)       */
}
```

---

## 4. crt0 startup → `BEGIN`

`call0 0x190c` enters the C startup (the `crt0`-equivalent). It primes the stack and PS, runs the
init-array constructors, then calls the application entry `BEGIN` with the args/config struct.

```asm
;--- crt0 startup @0x190c ---
190c:  movi a0,0
190f:  const16 a2,0 ; const16 a2,0 ; beqz.n a2,0x1922 ; callx0 a2   ; pre-init hook (skipped)
1922:  const16 a3,8 ; const16 a3,0x6f20 ; addmi a3,a3,0x100         ; a3 = 0x87020 (stack/sentry calc)
192d:  ... srli/slli align16 ; addi.n a3,a3,1 ; wsr.isl a3          ; interrupt-stack limit
1938:  movi a2,0x3e9 ; simcall 0                                     ; (sim heap query)
194a:  movi a3,128 ; wsr.ps a3 ; rsync                              ; PS <- 0x80 (WindowOverflowEnable)
1960:  const16 a6,0x84cbc ; const16 a7,0x84cc4                      ; __init_array_start / __init_array_end
1966:  bgeu a6,a7,0x19a1                                            ; ctor loop guard
1972:  ... callx8 (*ctor)()                                          ; run constructors
;--- the BEGIN call site ---
19e6:  const16 a12,8 ; const16 a12,0x5040                           ; a12 = DRAM 0x85040 (args/config struct)
19e9:  (const16 low half)
19ec:  call8 0x2378                                                 ; *** -> BEGIN(args) ***
```

The args struct lives at **DRAM `0x85040`** (zeroed in the static image — the host populates it
before boot). The init-array range `0x84cbc..0x84cc4` is the C++ constructor table. `BEGIN` has
exactly **one** caller (`0x19ec`). `[HIGH/OBSERVED]`

### 4.1 `BEGIN` @0x2378 — mode pick + HW-decode CSR program

```asm
2378:  entry a1, 64
237b:  movi a4,0 ; s32i a4,[a1+28] ; s32i.n a2,[a1+24] ; s32i.n a3,[a1+20]   ; save args
2385:  call8 0x24ec                                          ; sub-init (IRAM geometry helper, §5)
238b:  const16 a10,8 ; const16 a10,0xee1                     ; a10 = DRAM 0x80EE1
2391:  call8 0x18b84                                         ; LOG "S: BEGIN on cayman"
239a:  ... extui a2,a2,0,1 ; const16 a3,0x855e0 ; s8i a2,[a3+108]              ; store mode flag
23b5:  bbci a2,0,0x2422                                      ; branch on Sunda-mode flag
;=== Sunda-mode taken (flag set) ===
23be:  const16 a10,0xef5 ; call8 0x18b84                     ; LOG "S: NX in Sunda mode: HW decode disabled"
23cf:  l32i [a1+8] ; or 0x40   ; s32i [a1+8]                 ; config word: SET  bit6
23da:  l32i [a1+8] ; and ~0x100; s32i [a1+8]                 ; config word: CLR  bit8
23e4:  l32i [a1+8] ; and ~0x200; s32i [a1+8]                 ; config word: CLR  bit9
23f6:  const16 a2,0x5070 ; l8ui ; bbci -> 0x241f             ; read [DRAM 0x85070] flag
2402:  movi a2,0x400 ; const16 a2,0x4000                     ; a2 = hw_decode.control (CSR 0x4000)
2408:  l32i.n a3,[a2]                                        ; read control
240e:  movi.n a4,1 ; slli a4,a4,20 ; or a3,a3,a4             ; SET bit20  (iram_ctrl_flush_en)
241a:  s32i.n a3,[a2]                                        ; write control, bit20=1
;=== HW-Decode mode (0x2422) ===
2425:  const16 a10,0xf1e ; call8 0x18b84                     ; LOG "S: NX in HW Decode mode"
2436:  const16 a2,0x5070 ; movi.n a3,1 ; s8i a3,[a2]         ; mark [DRAM 0x85070] = 1 (HW-decode active)
243e:  l32i [a1+12] ; and ~0x2 ; s32i [a1+12]                ; config word: CLR bit1
244d:  movi a3,0x400 ; const16 a3,0x4000                     ; a3 = hw_decode.control (CSR 0x4000)
2453:  s32i.n a2,[a3]                                        ; write the prepared control word
;=== join @0x2458 ===
2458:  call8 0x26ac ; call8 0x2724                           ; init A / init B (entry-prologue fns)
245e:  call8 0x13e98 ; call8 0x13fa8                         ; init C / init D
2464:  call8 0x27ac ; call8 0x27f0                           ; init E / init F
246a:  const16 a2,0x855e0 ; l8ui a2,[+108] ; bbci 0x2484     ; re-read Sunda flag
2479:  movi.n a10,0 ; call8 0x2a84 ; call8 0x2c48            ; HW-decode branch: IRAM-cache init (a10=0)
2486:  movi.n a10,1 ; call8 0x2a84                           ; Sunda branch: IRAM-cache init (a10=1)
2496:  j 0x2499                                              ; *** fall into RUN LOOP ***
```

The runtime decode-mode decision is the **Sunda-mode vs HW-Decode-mode** branch at `0x23b5`,
driven by the args-supplied flag stored to the central state struct at **DRAM `0x855e0 + 108`**:

* **flag set → Sunda mode.** Logs `S: NX in Sunda mode: HW decode disabled` (`0xef5`); sets
  `hw_decode.control` bit20 (`iram_ctrl_flush_en`); the NX HW instruction-decode path is bypassed
  and the SEQ core software-fetches/redirects (the `sunda_fetch` / `sunda_redirect` family —
  forward-linked from [fetch-pc-redirect.md](fetch-pc-redirect.md)).
* **flag clear → HW-Decode mode.** Logs `S: NX in HW Decode mode` (`0xf1e`); writes the prepared
  `hw_decode.control` word so the hardware instruction-decode FIFO drives the engine.

`[HIGH/OBSERVED]`

> **CORRECTION — "per-generation dispatch at boot" is *compile-time*, not a runtime switch.**
> `S: BEGIN on cayman` is a single log line naming which gen-specific build is executing; the
> `CAYMAN` build holds only `BEGIN on cayman` and the `MARIANA` build only `BEGIN on mariana`
> (verified: each image carries its own gen token, never the other). The host
> `libnrtucode_internal.so` `.rodata` carries the full set `{cayman, mariana, mariana_plus,
> maverick}` only because it links every build for the host-side log decoder. The **runtime**
> mode dispatch is Sunda vs HW-Decode, above — not a generation switch. `[HIGH/OBSERVED]`

The four boot/mode strings, byte-verified at their `dram.bin` offsets (device VA `0x80000+off`):

| `dram.bin` off | Device VA | String |
|---|---|---|
| `0xee1` | `0x80ee1` | `S: BEGIN on cayman\n` |
| `0xef5` | `0x80ef5` | `S: NX in Sunda mode: HW decode disabled\n` |
| `0xf1e` | `0x80f1e` | `S: NX in HW Decode mode\n` |
| `0xf9c` | `0x80f9c` | `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` |

C reconstruction of `BEGIN`'s mode pick:

```c
/* BEGIN @0x2378 — application entry: mode pick + hw_decode.control program.  [HIGH/OBSERVED]
 * args: a config/args struct mapped at DRAM 0x85040. */
void BEGIN(struct boot_args *args) {
    iram_geometry_helper();                          /* 0x2385: call8 0x24ec */
    LOG("S: BEGIN on cayman");                        /* DRAM 0xEE1 */

    uint8_t sunda = args->mode_flag & 1;
    g_seq_state[108] = sunda;                         /* DRAM 0x855e0 + 108: central mode byte */

    volatile uint32_t *hw_ctrl = CSR(0x4000);        /* hw_decode.control */
    if (sunda) {
        LOG("S: NX in Sunda mode: HW decode disabled");   /* DRAM 0xEF5 */
        args->cfg8  |=  0x40;                         /* set bit6  */
        args->cfg8  &= ~0x100;                        /* clr bit8  */
        args->cfg8  &= ~0x200;                        /* clr bit9  */
        *hw_ctrl |= (1u << 20);                       /* 0x241a: SET iram_ctrl_flush_en (bit20) */
    } else {
        LOG("S: NX in HW Decode mode");               /* DRAM 0xF1E */
        *(uint8_t *)CSR_DATA(0x85070) = 1;            /* mark HW-decode active */
        args->cfg12 &= ~0x2;                          /* clr bit1 */
        *hw_ctrl = args->prepared_control;            /* 0x2453: write the HW-decode config word */
    }

    init_a(); init_b(); init_c(); init_d(); init_e(); init_f();   /* 0x2458..0x2467 */

    iram_cache_init(/*sunda=*/ g_seq_state[108] ? 1 : 0);          /* 0x2a84, §5 */
    /* fall through to run_loop() @0x2499  (§7) */
}
```

### 4.2 Engine self-discovery @0x4040

Reached from the init chain, the engine reads its own base aperture and decodes its placement:

```asm
4040:  ... build descriptor on stack
4051:  const16 a10,8 ; const16 a10,0xf9c ; call8 0x18b84
       ; LOG "S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u"
405d:  ... s32i.n zeros ; movi.n a3,2 ; slli a3,a3,24 ...     ; descriptor field build
4074:  addmi a3,a1,0x100 ; .byte 0x8f ...                     ; (partial desync past here, see §11)
```

At boot the SEQ engine determines whether it sits on a TPB, which die, and its engine index. The
LOG call and the `is_tpb`/`is_die_0`/`engine_idx` fields are recoverable; the trailing on-stack
descriptor build partially desyncs at `0x4077`. `[LOG HIGH/OBSERVED; trailing struct MED/INFERRED]`

---

## 5. IRAM-cache init @0x2a84

`iram_cache_init` computes the IRAM instruction-cache geometry from a DRAM-resident `block_size`
and programs the IRAM-control block-size field.

```asm
2a84:  entry a1, 64
2a87:  const16 a3,0x855e0 ; addi a3,a3,24 ; s32i.n a3,[a1+24]  ; &state[+24]
2a9a:  const16 a3,8 ; const16 a3,0x5654 ; l32i.n a11,[a3]      ; a11 = block_size = [DRAM 0x85654]
2aa2:  const16 a10,8 ; const16 a10,0x12ab ; call8 0x18b84      ; LOG "S: IRAM cache init, block_size=0x%x"
2ab3:  ... l32i block_size ; movi.n a4,1 ; slli a4,a4,15       ; a4 = (1<<15) = 32 KiB
2abd:  ... (1<<15) % block_size  and  (1<<15) / block_size     ; cache-line / index geometry
       ; assert power-of-two: if remainder != 0 -> call8 0xa304 (assert helper)
2afa:  ... num_blocks = floor((1<<15)/line) ; clamp
2b1d:  ... log2(block_size) selector chain (bnei 16/8/4/2 -> code 14/13/12/11/10)  ; block-size exponent
2bcc:  ... [0x85654] >> 6  -> derived "PC lower bits" field
2be5:  rsr.wb a4 ; movi a6,1 ; ssl a5 ; sll ; ... ; or          ; per-way enable mask
       ; write back IRAM-ctrl block-size word
       ;   (pairs with hw_decode.control.iram_block_size_mask, CSR 0x4000[18:2], reset 0x1FFF)
2c2a:  call8 0x18a64 ; call8 0x1a084                            ; allocate/map IRAM-cache backing
```

The global at **DRAM `0x85654`** is the IRAM block_size; the central SEQ state struct at **DRAM
`0x855e0`** (61 xrefs) holds the mode flags (`+108`), derived geometry, and the run-state byte
(`+100`). `[HIGH/OBSERVED]`

> **NOTE — source-module names leak through the assert strings.** The cache string is immediately
> followed in DRAM by `cache.hpp\0`, and `enter_run`'s assert (§6) by `enter_run\0fsm.hpp\0`. The
> assert helper at `0xa304` formats `S: Assertion failure! %s(%s:%u)` (DRAM `0x2210`) — so the
> SEQ front-end's source modules are named `cache.hpp` (IRAM cache) and `fsm.hpp` (the run FSM).
> `[HIGH/OBSERVED]`

---

## 6. `enter_run` @0x2c64 — the reset→run handshake

This is the boot→run rendezvous. It logs `Pending StartCtrl`, **spins** until the host asserts
`nx.start_ctrl`, then acks it, reads the HW-decode/breakpoint config, publishes `nx.run_state`,
and dispatches.

> **GOTCHA — `enter_run`'s real entry is `0x2c64`; the linear sweep mis-decodes `0x2c63`.** A
> straight disassembly shows `retw.n` at `0x2c61` (end of the prior function), then 2 padding
> bytes mis-decoded as `src a3,a6,a0` at `0x2c63`. Forcing `--start-address=0x2c64` yields a clean
> `entry a1,64`. This is a sweep artifact, **not real code**. `[HIGH/OBSERVED]`

```asm
2c64:  entry a1, 64
2c67:  const16 a2,0x855e0 ; addi a2,a2,100 ; s32i.n a2,[a1+16]  ; a2 = &state[+100] (run-state byte)
2c77:  l8ui a3,[a2] ; bbci a3,0,0x2c91                          ; if "claimed/busy" bit0 already set ->
2c80:    const16 a10,0x12da ; const16 a11,0x12e4 ; movi.n a12,39 ; call8 0xa304
         ; ASSERT  enter_run(fsm.hpp:39)   <- re-entry into a running engine is illegal
2c99:  const16 a10,0x12ec ; call8 0x18b84                       ; LOG "S: Pending StartCtrl"
;--- StartCtrl POLL ---
2cad:  movi a3,0x400 ; const16 a3,4                             ; a3 = nx.start_ctrl (CSR 0x0004)
2cb3:  l32i.n a3,[a3] ; s32i.n a3,[a1+20]                       ; read start_ctrl.ctrl
2cb7:  beqz a3,0x2caa                                           ; *** SPIN until start_ctrl != 0 ***
2cc0:  const16 a10,0x1302 ; call8 0x18b84                       ; LOG "S: enter_run: start"
;--- read HW-decode / breakpoint config ---
2cd0:  call8 0x5504  (-> 0x553c)                                ; HW-decode/breakpoint CSR reader (§6.1)
2cd5:  call8 0x5514  (-> 0x573c)                                ; further setup
2cd8:  movi a3,0x400 ; const16 a3,0x14c0 ; l32i.n a3,[a3]       ; read general@0x14c0 (DVE perf flag)
2ce0:  const16 a4,0x855e0 ; s32i a3,[a4+96]                     ; store to state[+96]
2cf2:  l32i state[+96] ; extui a11, bit0                        ; a11 = DVE-perf-support bit
2cf8:  const16 a10,0x1317 ; call8 0x18b84                       ; LOG "S: DVE perf mode support = %d"
;--- StartCtrl ACK + publish run_state ---
2d0d:  movi a3,0x400 ; const16 a3,4                             ; a3 = nx.start_ctrl (CSR 0x0004)
2d13:  movi.n a4,0 ; s32i.n a4,[a3+0]                           ; *** WRITE 0 -> nx.start_ctrl (ACK) ***
2d17:  movi.n a4,1 ; s8i a4,[a2+0]                              ; set sw "running" flag state[+100]=1
2d22:  s32i.n a4,[a3+4]                                         ; *** WRITE 1 -> [start_ctrl+4] = nx.run_state (0x0008) ***
2d2d:  call8 0x3cdc                                             ; instruction-dispatch entry
2d30:  ... l32i general@0x1060/0x1080 -> state[+24]/[+28]       ; post-dispatch status snapshot
2d48:  call8 0x5524                                             ; dispatch tail
2d4e:  const16 a10,0x1336 ; call8 0x18b84                       ; LOG "S: enter_run: done"
       retw                                                     ; -> back to run loop
```

The poll-then-ack is the **post-reset "exit Halt / begin executing" handshake**: the host asserts
`nx.start_ctrl.ctrl` (reset `0x0`; "1: start_addr valid, exit Halt, begin executing"); the
firmware spins on it (`Pending StartCtrl`), then writes `0` to ack and writes `1` to the adjacent
`nx.run_state` (CSR `0x0008`) word to publish that it is running. `[HIGH/OBSERVED]`

The `enter_run` strings, byte-verified at their `dram.bin` offsets:

| `dram.bin` off | Device VA | String |
|---|---|---|
| `0x12ab` | `0x812ab` | `S: IRAM cache init, block_size=0x%x\n` (then `cache.hpp\0`) |
| `0x12da` | `0x812da` | `enter_run\0` (assert func name) |
| `0x12e4` | `0x812e4` | `fsm.hpp\0` (assert file name) |
| `0x12ec` | `0x812ec` | `S: Pending StartCtrl\n` |
| `0x1302` | `0x81302` | `S: enter_run: start\n` |
| `0x1317` | `0x81317` | `S: DVE perf mode support = %d\n` |
| `0x1336` | `0x81336` | `S: enter_run: done\n` |

C reconstruction:

```c
/* enter_run @0x2c64 — boot->run rendezvous (one dispatch iteration).  [HIGH/OBSERVED] */
void enter_run(void) {
    uint8_t *running = &g_seq_state[100];            /* DRAM 0x855e0 + 100 */
    if (*running & 1)                                /* 0x2c77: re-entry into a running engine */
        assert_fail("enter_run", "fsm.hpp", 39);     /* 0x2c8e: call8 0xa304 */

    LOG("S: Pending StartCtrl");                      /* DRAM 0x12EC */

    volatile uint32_t *start_ctrl = CSR(0x0004);     /* nx.start_ctrl */
    uint32_t sc;
    do { sc = *start_ctrl; } while (sc == 0);        /* 0x2cb7: SPIN — wait for host StartCtrl */

    LOG("S: enter_run: start");                       /* DRAM 0x1302 */

    hw_decode_breakpoint_read();                      /* 0x2cd0 -> 0x553c (§6.1) */
    further_setup();                                  /* 0x2cd5 -> 0x573c */

    uint32_t dve = *CSR(0x14c0);                      /* general@0x14c0: DVE perf-mode flag */
    g_seq_state_u32[96/4] = dve;
    LOG("S: DVE perf mode support = %d", dve & 1);    /* DRAM 0x1317 */

    *start_ctrl     = 0;                              /* 0x2d13: ACK  start_ctrl <- 0          */
    *running        = 1;                              /* 0x2d17: sw running flag               */
    *(start_ctrl+1) = 1;                              /* 0x2d22: nx.run_state (CSR 0x0008) <- 1 */

    dispatch();                                       /* 0x2d2d / 0x2d48 */
    LOG("S: enter_run: done");                        /* DRAM 0x1336 */
}
```

### 6.1 HW-decode / breakpoint CSR reader @0x553c

Called from `enter_run` (`0x2cd0 → 0x5504 → 0x553c`), this snapshots the `hw_decode` bundle into a
per-iteration config:

```asm
553c:  entry a1, 112
5542:  const16 a2,0x4008 ; l32i.n a2,[a2] ; store to [DRAM 0x85650]   ; breakpoint_step.num_instr
5552:  const16 a2,0x4000 ; l32i.n a2,[a2] ; and 0x2 ; srli 1          ; control.instr_ordering_mode [1]
556c:  l8ui state[+108] (Sunda flag) ; bbsi -> branch
558d:  const16 a2,0x4004 ; l32i.n a2,[a2] ; extui bit0 / bit3         ; breakpoint_ctrl.{instr_enable[0],step_valid[3]}
5603:  const16 a2,0x401c -> breakpoint_addr0_hi
560e:  const16 a2,0x4018 -> breakpoint_addr0_lo
5646:  const16 a2,0x4020 -> breakpoint_addr1_lo
563b:  const16 a2,0x4024 -> breakpoint_addr1_hi
5676/56d1: const16 a2,0x400c -> breakpoint_match (ic0/ic1 opcode+mask)
```

Every offset matches the `hw_decode` bundle (`@0x4000`, §8) exactly. `[HIGH/OBSERVED]`

---

## 7. The run loop @0x2499 — back-edge @0x24e9

`BEGIN` falls through into the SEQ main run loop. It calls `enter_run` once per iteration, services
the post-dispatch and notification work, and loops forever.

```asm
2499:  call8 0x2c64                                  ; -> enter_run (one dispatch iteration)
249c:  const16 a2,0x85f88 ; l8ui a2,[+20] ; bbci 0x24b6 ; else call8 0x13348
24b6:  call8 0x1c130                                 ; post-dispatch handler
24bf:  const16 a2,0x855e0 ; l8ui a2,[+108] ; bbci    ; re-read Sunda flag
24dd:  const16 a2,0x855e0 ; addi a10,a2,100          ; a10 = &state[+100]
24e3:  call8 0x3a44 ; call8 0x1c13c                  ; notification / timestamp-increment work
24e9:  j 0x2499                                       ; *** RUN-LOOP BACK-EDGE ***
```

`enter_run` is also reachable from a second call site at `0x4c79` (a redirect / re-entry path);
the loop's `0x2499` site is the primary boot path. `[HIGH/OBSERVED]`

```c
/* run_loop @0x2499..0x24e9 — the SEQ main loop, entered by falling through BEGIN.  [HIGH/OBSERVED] */
void run_loop(void) {
    for (;;) {                                       /* 0x24e9: j 0x2499 back-edge */
        enter_run();                                 /* 0x2499 */
        if (g_flags_85f88[20] & 1) handle_special(); /* 0x249c..0x24b0 */
        post_dispatch();                             /* 0x24b6: call8 0x1c130 */
        notify_and_tick(&g_seq_state[100]);          /* 0x24e3/0x24e6 */
    }
}
```

Forward continuation: the FSM state machine, decode/dispatch hub, and notification servicing are
covered in [SEQ Main FSM Loop](main-loop.md).

---

## 8. CSRs programmed at boot (cross-ref `tpb_xt_local_reg`)

Every offset below is the **low-16 CSR offset** within the SEQ engine's local register aperture;
the bundle bases are `nx@0x0000`, `general@0x1000`, `window@0x2000`, `q7@0x3000`,
`hw_decode@0x4000`.

| Local CSR offset | Register | Site / action |
|---|---|---|
| `0x0004` `nx.start_ctrl[0]` | `start_ctrl.ctrl` (start / exit-Halt) | `enter_run` `0x2cb3` **POLL** ; `0x2d13` **ACK=0** |
| `0x0008` `nx.run_state[31:0]` | `run_state` (opaque status) | `enter_run` `0x2d22` **WRITE=1** |
| `0x4000` `hw_decode.control` | `iram_ctrl_flush_en[20]` | `BEGIN` `0x241a` **SET bit20** (Sunda) |
| | (RMW; HW-decode config word) | `BEGIN` `0x2453` **WRITE word** (HW-decode) |
| | `instr_ordering_mode[1]` (read) | `0x5552` READ |
| `0x4004` `breakpoint_ctrl` | `instr_enable[0]` / `step_valid[3]` (read) | `0x558d` READ |
| `0x4008` `breakpoint_step` | `num_instr` (read) | `0x5542` READ |
| `0x400c` `breakpoint_match` | ic0/ic1 opcode+mask (read) | `0x5676`/`0x56d1` READ |
| `0x4018` `breakpoint_addr0_lo` | bp addr0 low | `0x560e` READ |
| `0x401c` `breakpoint_addr0_hi` | bp addr0 high | `0x5603` READ |
| `0x4020` `breakpoint_addr1_lo` | bp addr1 low | `0x5646` READ |
| `0x4024` `breakpoint_addr1_hi` | bp addr1 high | `0x563b` READ |

### 8.1 The window / address-translation CSRs

The `window` bundle (`@0x2000`, 40 instances, stride `0x1C`) **is** programmed at boot: `const16`
sites build window CSR offsets `0x2005`/`0x200a`/`0x2013` (at IRAM `0x9bac`/`0x9bb2`/`0x9c0d`),
plus `0x20d0`/`0x2150`/`0x2160`/`0x21c0` — per-window control/mask/match/replace writes that back
the `S: WIN: @%llx -> %x` log line (`DRAM 0x1232`, xref'd at `0x5036`/`0x722b`) and set up the
NX-vs-Q7 address-remapping windows. `[HIGH/OBSERVED]`

### 8.2 Run-stall release — what the SEQ core does and does not write

> **NOTE — the NX SEQ core does not release its own run-stall.** `nx.release_run_stall` (CSR
> `0x0000`, reset `0x1` = NX held stalled) is **not** written by this IRAM code. By the time the
> IRAM executes, the NX core is already un-stalled — the host/RTL cleared `nx.release_run_stall`
> *before* fetch began at the reset vector. The firmware therefore manages `start_ctrl` (CSR
> `0x0004`) — the post-reset "exit Halt" handshake (§6) — not its own `0x0000`. This is the SEQ
> side of the [boot-reset.md §8](../../uarch/boot-reset.md) handoff: the image is installed and
> the core de-stalled before PC `0x0`. `[HIGH/OBSERVED]`

> **CORRECTION / DESYNC — the Q7 `release_run_stall` (CSR `0x3000`) sequence is *not*
> instruction-exact from stock objdump.** Releasing the eight Q7/GPSIMD compute sequencers from
> stall is reached far later than the boot spine, in the densely-scheduled FLIX region. The only
> four `const16`-to-`0x3000` sites in the whole image are IRAM `0x1aaf7`, `0x1af66`, `0x1afb1`,
> `0x1bb2b` — all **inside the FLIX/IVP vector body** with interleaved literal pools and selector
> bytes, where `xtensa-elf-objdump` loses bundle sync (e.g. `0x1aaf7`'s neighbourhood shows
> `.byte` runs, `l32r` into mid-data, and `ivp_*` fragments). The CSR-`0x3000` **constant** is
> confirmed referenced there; the **read-modify-write** that clears the per-core stall bits
> cannot be reliably reconstructed from this disassembly. The Q7-release is **flagged as a desync
> span** — recovered only to "`q7.release_run_stall` (`0x3000`) is touched here", not
> instruction-exact. `q7.release_run_stall` resets to `0xFF` (all 8 cores held); the host/SEQ
> clears it `0xFF → per-core 0`. `[constant-ref HIGH/OBSERVED; RMW LOW/desynced]`

---

## 9. The `S:` logger @0x18b84

`0x18b84` is the varargs `S:`/`P%i:` log/printf routine — the anchor for every string xref on this
page. It spills `a4..a7` (the varargs) to stack, builds an arg pointer, loads a conversion pivot
table at **DRAM `0x84d28`**, calls `0x19ae4` then `0x18d14` (a `vsnprintf`-class formatter, with a
conversion callback at `0x88bcc`), then `0x19b3c` (emit to the TPB log ring). The format-string
pointer arrives in `a10` (the `const16`-pair DRAM address) and is forwarded into the formatter.

```c
/* LOG @0x18b84 — varargs 'S:' logger.  [HIGH/OBSERVED] */
int LOG(const char *fmt /* a10: DRAM const16-pair ptr */, ...) {
    va_list ap; va_start(ap, fmt);                   /* spill a4..a7 to stack frame */
    char buf[...];
    const struct conv_tab *piv = (void *)0x00084d28; /* DRAM 0x84d28 conversion pivot */
    prep(piv);                                        /* 0x19ae4 */
    int n = vsnprintf_class(buf, fmt, ap, 0x00088bcc); /* 0x18d14 */
    emit_to_tpb_log_ring(buf, n);                     /* 0x19b3c */
    return n;
}
```

This is why the boot strings live in the firmware's own DRAM image at VA `0x80000+off` — the
device dereferences them locally. The host copy in `libnrtucode_internal.so` is the same strings
linked into all gen-builds for the host-side log *decoder*. The assert helper is `0x96d4` / `0xa304`
(takes file-name ptr + line in `a10`/`a11`/`a12`; it formats `S: Assertion failure! %s(%s:%u)` at
DRAM `0x2210`). `[HIGH/OBSERVED]`

### 9.1 Cross-engine parity + DEBUG-vs-PERF

The boot path is **byte-identical across all NX engines** — every `CAYMAN_NX_*` DEBUG DRAM carries
the same `BEGIN on cayman` / `Pending StartCtrl` / `enter_run: start` strings:

| Engine (CAYMAN NX DEBUG DRAM) | `S:` count | `BEGIN on cayman` | `enter_run` | `Pending StartCtrl` |
|---|---|---|---|---|
| ACT | 150 | ✓ | ✓ | ✓ |
| DVE | 182 | ✓ | ✓ | ✓ |
| PE | 148 | ✓ | ✓ | ✓ |
| **POOL** (traced here) | **187** | ✓ | ✓ | ✓ |
| SP | 142 | ✓ | ✓ | ✓ |

The `CAYMAN_Q7_POOL` DEBUG DRAM, by contrast, holds **0** `S:` strings and **156** `P%i:` strings
(`P%i: Pending StartCtrl`, `P%i: Start Enter Run`, …) — confirming the `S:` SEQ engine and the
`P%i:` Q7 compute engine are distinct cores. `[HIGH/OBSERVED]`

> **GOTCHA — PERF builds strip the strings and re-lay-out DRAM; offsets differ.** The `*_DEBUG_*`
> image keeps the format strings in DRAM; the `*_PERF_*` image is the optimized/stripped build and
> drops them — the device emits log-record IDs instead. Concretely, measured this session:
>
> | | DEBUG | PERF |
> |---|---|---|
> | DRAM `.rodata` | `0x6f20` (28448 B) | `0x3020` (12320 B) |
> | IRAM `.rodata` | `0x1c820` (116768 B) | `0x17280` (94848 B) |
> | `S:` strings in DRAM | **187** | **0** |
>
> Only **14** strings survive in PERF DRAM — assertion-path `file:line` literals (`fsm.hpp`,
> `cache.hpp`, `branch.cpp`, `move.cpp`, `alu_op.cpp`, `exception_handler.hpp`,
> `signal_handler.cpp`) plus `Assertion failure! %s(%s:%u)`. The PERF reset vector at IRAM `0x0` is
> **byte-identical** to DEBUG (`j 0x1dc`): the **control flow is the same**, only the strings and
> code size differ. **Shared survivors are re-laid-out** — e.g. `Assertion failure! %s(%s:%u)`
> sits at DRAM offset `0x2213` in DEBUG but `0xbd0` in PERF. *This page anchors to the DEBUG image
> so every boot step has a named string xref; every cited DRAM offset above is a DEBUG offset and
> will differ in PERF.* `[HIGH/OBSERVED]`

---

## 10. Confirmed entry → `enter_run` chain (one line per hop)

| IRAM addr | Instruction | Role |
|---|---|---|
| `0x0` | `j 0x1dc` | reset vector |
| `0x1dc` | `const16 a0=0x90 ; jx a0` | boot stub → `_start` |
| `0x90` | `_start`/`_ResetHandler` | I$ inval, VECBASE, MEMCTL, WB, MPU, I$-enable |
| `0x190c` | crt0 startup (`call0`) | stack/PS, ctors; loads args @DRAM `0x85040` |
| `0x19ec` | `call8 0x2378` | → `BEGIN(args)` |
| `0x2378` | `BEGIN` | LOG `BEGIN on cayman`; Sunda/HW-decode pick; `hw_decode.control` program; init chain |
| `0x2499` | `call8 0x2c64` (in run loop) | → `enter_run` |
| `0x24e9` | `j 0x2499` | **run-loop back-edge** |
| `0x2c64` | `enter_run` | LOG `Pending StartCtrl`; **poll** `nx.start_ctrl(0x0004)`; LOG `enter_run: start`; read `hw_decode`/breakpoint CSRs; **ack** `start_ctrl=0`; set `nx.run_state(0x0008)`; dispatch; LOG `enter_run: done` |

---

## 11. Desync spans / honest gaps

* **`0x2c63`** (1-byte sweep desync before `enter_run`'s `entry`): ARTIFACT — real entry `0x2c64`
  verified by forced `--start-address`. Not real code. `[HIGH/OBSERVED]`
* **The FLIX/IVP vector body** (broadly `0xb500..`, `0x13b00..`, `0x1a900..0x1bb00`, and other
  `ivp_*` dense regions) loses bundle sync across interleaved literal pools and selector bytes.
  The Q7 `release_run_stall` (CSR `0x3000`) sequence lands in that span (§8.2) and is recovered
  only to constant-reference level — instruction-exact reconstruction is **not** possible from
  stock objdump. FLAGGED. `[LOW/desynced]`
* **The engine self-discovery descriptor build past `0x4074`** partially desyncs (`.byte` runs at
  `0x4077`+); the LOG call and the `is_tpb`/`engine_idx` fields are recoverable, the trailing
  struct build is partial. `[MED/INFERRED]`
* **CSR aperture absolute base alias** (`0x0` vs `0x00400000`): ambiguous from the `movi`/`const16`
  high half. Register *identities* (by low offset) are HIGH; the absolute base is LOW. Does not
  affect any register mapping above. `[register-id HIGH; absolute-base LOW]`

---

## See also

* [SEQ Main FSM Loop](main-loop.md) — what `enter_run`'s dispatch hands into, and the post-dispatch
  / notification servicing the run loop performs each iteration.
* [SEQ Fetch + PC-Redirect Front-End](fetch-pc-redirect.md) — the Sunda-mode software fetch /
  redirect path that the `BEGIN` mode pick selects.
* [SEQ IRAM Instruction Cache / Overlay](iram-cache.md) — the `0x2a84` geometry programming in full.
* [Boot / Reset Sequence + Startup Config](../../uarch/boot-reset.md) — the *Q7 "Cairo"* core's
  cold-boot spine and the host↔device `DRAM[0] 0x6099CB34 → 0x502B2DA1` claim handshake; §8 there
  is the run-stall release this SEQ page picks up from the NX side.
