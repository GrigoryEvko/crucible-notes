# Boot / Reset Sequence + Startup Config

This page reconstructs the **complete cold-boot spine** of the Vision-Q7 "Cairo" GPSIMD
core (`ncore2gp`, XEA3 / Xtensa24, one per NeuronCore) — from the architectural reset PC at
`0x0`, through the `_ResetHandler` early hardware init, the crt1 C-runtime, the multi-core
PRID-gated shared reset vector, and out the far side into the on-device dispatch loop, where
it meets the host across a single device↔host rendezvous word. Everything here is
**byte-pinned to a shipped artifact**: the device reset/crt objects disassembled
with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`), the `core-isa.h` / `specreg.h`
config headers, the host `libnrtucode.so` boot/claim handshake disassembled with stock
`objdump`, and the **embedded device firmware ELF** carved out of `libnrtucode_internal.so`
(the image the host BAR0-writes into the core). Where a sibling note
disagrees with the binary, **the binary wins** — and an in-place **CORRECTION** callout says so.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/string/define read from a shipped artifact; `INFERRED` =
reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor; crossed
with `HIGH`/`MED`/`LOW`. Untagged claims default to `[HIGH/OBSERVED]`; only departures carry a
tag. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orientation).

Two corpus facts govern the anchors. (1) The device reset/crt objects are **relocatable
`.o`** with full symtab and reloc records; their `.ResetVector.text`/`.ResetHandler.text`
sections link at VMA `0x0`, so every offset below is a *section offset*, and the
`R_XTENSA_SLOT0_OP` reloc — not the stale symbol the listing prints — is the true `const16`
target. (2) The embedded firmware ELF and the host libs follow the standard VMA==file-offset
rule for `.text`/`.rodata`; the `ncore2gp` config DLLs' `.data`/`.data.rel.ro` carry a
**`0x200000`** VMA−fileoffset delta (not libtpu's `0x400000`) — confirm per-section with
`readelf -SW` before any `xxd`/`objdump` on a `.data`-resident struct.

---

## 0. The spine in one diagram

The core comes up in two cleanly separable phases that meet at one DRAM word:

```
  ┌──────────────────────── PHASE A — on-device XEA3 bringup ───────────────────────┐
  │  reset:   PC = 0x0  (== VECBASE_RESET, == RESET_VECTOR0)   [run-stall released]  │
  │     │                                                                            │
  │     ▼  _ResetVector  (3 bytes)                                                   │
  │     j  _ResetHandler                                                             │
  │     │                                                                            │
  │     ▼  _ResetHandler  (0x131-byte body)                                          │
  │     ├─ wsr.isb  _xt_interrupt_table   ;  ISB(236) ← interrupt-stack base 0x8FED0 │
  │     ├─ wsr.vecbase _DispatchVector    ;  VECBASE(231) ← unified DispatchVector   │
  │     ├─ iii loop 0..0x1000 stride 64   ;  invalidate the whole 16 KB I-cache      │
  │     ├─ wsr.memctl 0xFFFFFF08          ;  MEMCTL(97)  (I-cache still OFF)          │
  │     ├─ wsr.prefctl 0x00003044         ;  PREFCTL(40) (8-entry prefetcher)        │
  │     ├─ wsr.wb (1<<30 = 0x40000000)    ;  WB(72) initial WindowBase               │
  │     ├─ MPU program (16 fg entries)    ;  table path OR cacheattr fallback        │
  │     ├─ MEMCTL |= 0x09                 ;  *** I-cache ENABLE ***                   │
  │     ├─ [reset-vector-unpack only: ROM-store {dst,end,src} decompress]            │
  │     ├─ wsr.ms 0                       ;  MS(229) ← 0                              │
  │     └─ callx0 _start                  ;  *** enter crt1 ***                       │
  │           │                                                                      │
  │           ▼  crt1 _start                                                         │
  │           ├─ a1 = __stack ; wsr.isl align16(_stack_sentry+0x100)+1               │
  │           ├─ wsr.ps 0x80              ;  PS ← WindowOverflowEnable                │
  │           ├─ .bss clear (table-driven, loopnez-unrolled)                         │
  │           ├─ callx8 board_init        ;  (no-op stub on this config)             │
  │           ├─ PRID-gated callx8 _init_reent_bss                                   │
  │           ├─ callx8 __clibrary_init   ;  ctors / newlib reent                    │
  │           └─ callx8 main              ;  *** main == the dispatch loop ***        │
  └────────────────────────────────────────────────────────────────────────────────┘
            │  main initialises  .globstruct[0] = 0x6099CB34  (linked DRAM word)
            ▼
  ┌──────────────────────── PHASE B — host rendezvous ──────────────────────────────┐
  │  nrtucode_core_on_ucode_booted (host x86):                                       │
  │     read DRAM[0]  ──► == 0x6099CB34 ?  ──yes──►  write 0x502B2DA1, boot_state=1   │
  │                     └► == 0x502B2DA1 ?  ──yes──►  "claimed by another instance" 8 │
  │                     └► else            ─────────►  "magic mismatch / incompat" 8  │
  └──────────────────────────────────────────────────────────────────────────────────┘
```

One-line verdict: a **textbook XEA3 reset vector** (ISB/VECBASE arm → I-cache invalidate →
MEMCTL/PREFCTL → WB prime → 16-entry MPU → cache-enable → optional ROM unpack → `_start`) into
a **crt1 C-runtime** (stack/ISL/PS/.bss/clibrary/`callx8 main`) whose `main` is the dispatch
loop; the host then **claims** the core via the DRAM[0] `0x6099CB34 → 0x502B2DA1` handshake.
Every surface below is OBSERVED in the shipped reset/crt objects, the config headers, the
carved firmware ELF, and the host lib.

> **NOTE — what runs *before* PC `0x0`.** The reset vector only executes after the core's
> run-stall is released. Out of SoC reset all 8 Q7 cores are stalled; the host clears the
> stall per-arch (§8). The IRAM/DRAM image (DispatchVector + ResetVector at `0x0`, firmware
> `.text`/`.data` at `0x01000000`/`0x02000000`) is installed by the host **before** the
> de-stall, so the very first fetch at `0x0` already sees the linked vectors. The arming of
> the SoC fabric perimeter is the privileged management core's job, not the compute Q7's reset
> vector — see [Clock / Reset / Power Domains](clock-reset-power.md).

---

## 1. Configuration grounding — pin these or you are not booting this core

All values read directly from `core-isa.h` / `specreg.h` (config
`ncore2gp`, at `…/ncore2gp/xtensa-elf/arch/include/xtensa/config/`), all `[HIGH/OBSERVED]`.

| Knob | Value | Read from (`core-isa.h` / `specreg.h` line) |
|---|---|---|
| Exception arch | **XEA3** | `core-isa.h:719` `XCHAL_XEA_VERSION 3`; `:726` `XCHAL_HAVE_XEA3 1` |
| VECBASE reset value | `0x00000000` | `:736` `XCHAL_VECBASE_RESET_VADDR` |
| Reset PC (VECTOR0) | `0x00000000` | `:740,741` `XCHAL_RESET_VECTOR0_VADDR/PADDR` |
| Alt reset (VECTOR1) | `0x00100000` | `:742,743` (system-RAM alternate; unused) |
| VECBASE↔reset overlap | `0` (UNUSED) | `:738` `XCHAL_RESET_VECBASE_OVERLAP` |
| ISB (interrupt-stack base) | `0x0008FED0` | `:706` `XCHAL_ISB_VADDR` |
| I-cache | **16384 B / 64 B line / 4-way** | `:296,291,369` SIZE/LINESIZE/WAYS |
| D-cache | **0** (none) | `:298` `XCHAL_DCACHE_SIZE 0` |
| `iii` (Icache test) | present | `:311` `XCHAL_HAVE_ICACHE_TEST 1` |
| Prefetch | **8 entries** | `:304,307` `HAVE_PREFETCH 1`, `PREFETCH_ENTRIES 8` |
| MPU | **16 fg + 2 bg, 4 KiB align, no lock** | `:800–809` `HAVE_MPU 1`, `ENTRIES 16`, `BACKGROUND_ENTRIES 2`, `ALIGN 4096`, `ALIGN_BITS 12`, `LOCK 0` |
| Legacy CACHEATTR reg | **absent** | `:784` `XCHAL_HAVE_CACHEATTR 0` |
| Local-mem ECC/parity | **0** (none) | `:733` `XCHAL_HAVE_MEM_ECC_PARITY 0` |
| Register window | **64 AR, windowed, no CALL4/12** | `:50,51` `HAVE_WINDOWED 1`, `NUM_AREGS 64`; `:74` `XCHAL_HAVE_CALL4AND12 0` |
| PRID core-id field | low 4 bits | `:332–334` `PRID_ID_SHIFT 0`, `_BITS 4`, `_MASK 0xF` |
| Vision type | **Q7** | `:208` `XCHAL_VISION_TYPE 7`; `:94` `CP_MAXCFG 7` |

Boot-relevant special-register numbers (`specreg.h`, all OBSERVED), cross-checked against the
`wsr.<reg>` opcodes decoded in §3/§4:

| SR | # | line | SR | # | line |
|---|---|---|---|---|---|
| `PREFCTL` | 40 | `:41` | `MS` | 229 | `:61` |
| `WB` (WindowBase) | 72 | `:42` | `PS` | 230 | `:62` |
| `MPUENB` | 90 | `:43` | `VECBASE` | 231 | `:63` |
| `MEMCTL` | 97 | `:45` | `PRID` | 235 | `:66` |
| `ISL` (stack limit) | — | (used by crt) | `ISB` | 236 | `:67` |

> **QUIRK — VECBASE and the reset PC coincide at `0x0`.** Both `XCHAL_VECBASE_RESET_VADDR` and
> `XCHAL_RESET_VECTOR0_VADDR` are `0x0`, and `RESET_VECBASE_OVERLAP` is `0`. The unified
> `_DispatchVector` sits at the bottom of IRAM0, and the 3-byte `_ResetVector` immediately
> follows it (the section order is `.DispatchVector.text`, then `.ResetVector.text`, then
> `.ResetHandler.text`). So the literal reset PC and the dispatch-vector base are the *same*
> address surface — see [the interrupt/dispatch model](../isa/ref/b28-xt-exc.md). The reset
> vector relocates VECBASE to *itself* (the DispatchVector), which is harmless and standard.

> **GOTCHA — `XCHAL_HAVE_CACHEATTR 0`.** There is **no** legacy CACHEATTR register on this
> core; the MPU is the *only* memory-attribute mechanism. The `_memmap_cacheattr_reset` word
> the §3.6 fallback consumes is therefore a **packed constant** used to *synthesise* MPU
> regions, never a value written to a CACHEATTR register. Reimplementers porting from an
> older CACHEATTR-based LSP must not emit a `wsr.cacheattr`.

---

## 2. The reset vector — `.ResetVector.text` (3 bytes at PC `0x0`)

`reset-vector.o` (member of `libhandler-reset.a`) section map, OBSERVED via
`xtensa-elf-objdump -h`:

| Section | Size | Flags |
|---|---|---|
| `.ResetVector.text` | `0x003` | RELOC, CODE — the reset entry |
| `.ResetHandler.text` | `0x145` | RELOC, CODE — `_xtos_mpu_attribs` prefix (0x14) + `_ResetHandler` body (0x131) |
| `.ResetHandler.literal` | `0` | (no literals — `const16` is used throughout) |

The reset entry, `objdump -dr`:

```asm
00000000 <_ResetVector>:
   0:  000006        j  4        ; reloc R_XTENSA_SLOT0_OP _ResetHandler
```

The architectural reset entry is a **single 3-byte density-form `j`** relocated to
`_ResetHandler`. Initial PC = `0x0`; first executed instruction = this jump. The 3-byte size
is exactly the slot budget between the DispatchVector and the ResetHandler. `_ResetVector` is
a GLOBAL symbol in `.ResetVector.text`. `[HIGH/OBSERVED]`

> **GOTCHA — the listing prints `j 4 <CS_SA_restore_label>`, not `<_ResetHandler>`.** The
> object carries a table of **absolute** context-save layout symbols (`CS_SA_*`, type `a` in
> `nm`); `CS_SA_restore_label` happens to be abs `0x4`, so `objdump` labels the branch target
> with it. **Ignore the printed symbol — trust the reloc.** The relocation record
> `00000000 R_XTENSA_SLOT0_OP _ResetHandler` is the truth: the `j` resolves to `_ResetHandler`
> at link time. This is a recurring pitfall in *every* object that pulls in the `CS_SA_*`
> context-save layout (the reset and crt objects all do).

The two dispatch paths converge here. On a cold (non-exception) entry the DispatchVector at
`VECBASE+0x00` also routes to a `JumpToResetHandler` tail; in `dispatch-vector.o`,
`DispatchHandler+0x2c` is OBSERVED as:

```asm
0000002c <JumpToResetHandler>:
  2c:  const16 a0, 0    ; reloc R_XTENSA_SLOT0_OP _ResetHandler
  32:  jx  a0
```

so both the literal reset PC and the dispatch "reset" path land on `_ResetHandler`. The boot
path and the steady-state interrupt path therefore **share VECBASE**. `[HIGH/OBSERVED]`

---

## 3. The reset handler — early hardware init

`_ResetHandler` lives at `.ResetHandler.text+0x14` (the first `0x14` bytes are the
`_xtos_mpu_attribs` data table, §3.6). Its body is **`0x131` bytes** (section `0x145` minus the
`0x14` prefix). The full byte-exact decode below is the boot order; every `wsr.<reg>` is
cross-checked to `specreg.h` (§1).

### 3.0 The `_xtos_mpu_attribs` table prefix (`.ResetHandler.text` `0x00..0x13`)

Not code — a **5-word lookup** the §3.6 cacheattr fallback indexes. Raw `-s` bytes and the
little-endian words:

```
 0000  08 60 00 00  08 77 0f 00  08 57 0d 00  08 47 0c 00     ← words:
 0010  08 67 00 00                                            0x00006008, 0x000f7708,
                                                              0x000d5708, 0x000c4708,
                                                              0x00006708
```

Each word is an MPU access-rights + memory-type encoding; the fallback selects one per
cache-attribute nibble. `[HIGH/OBSERVED]`

> **CORRECTION — the attrib words, byte order.** A prior note transcribed these as
> "`00 60 00 08 …`". The shipped little-endian words are
> `0x00006008 / 0x000f7708 / 0x000d5708 / 0x000c4708 / 0x00006708` (raw bytes
> `08 60 00 00 | …`). The low byte `0x08` is the constant access-rights nibble; the second
> byte (`0x60`, `0x77`, `0x57`, `0x47`, `0x67`) is the memtype/cache encoding.

### 3.1 `__reset_hook` (optional pre-init)

```asm
14:  const16 a2, __reset_hook
1a:  beqz    a2, 20            ; default __reset_hook = 0 → skipped
1d:  callx0  a2
20:  movi.n  a0, 0
```

A weak hook (default 0). Lets a board inject pre-everything code; absent on this build.

### 3.2 Interrupt-stack base + VECBASE relocate — arming the XEA3 surface

```asm
22:  const16 a4, _xt_interrupt_table   ; reloc → ISB target (links to 0x8FED0, §1)
28:  wsr.isb a4                        ; ISB(236)
2b:  const16 a2, _DispatchVector
31:  wsr.vecbase a2                    ; VECBASE(231) ← the unified DispatchVector
```

**This is the single act that arms the entire XEA3 exception/interrupt machine.** After
`wsr.vecbase`, every future trap/interrupt vectors to the DispatchVector (the 37-async /
9-sync dispatch surface). `_xt_interrupt_table` is the ISB target — the interrupt-stack/table
base the HW reads on dispatch — and links to `XCHAL_ISB_VADDR = 0x8FED0`. `[HIGH/OBSERVED]`

### 3.3 I-cache invalidate — the whole 16 KB, `iii` stride-64

```asm
34:  const16 a2, 0
37:  const16 a2, 0x1000      ; a2 = 0x1000 = 16384 = I-cache size
3a:  movi.n  a3, 0           ; a3 = byte index
<loop @3c>:
3c:  iii a3, 0               ; 4 lines invalidated per iteration:
3f:  iii a3, 64             ;   a3+0, a3+64, a3+128, a3+192
42:  iii a3, 128
45:  iii a3, 192
48:  addmi a3, a3, 0x100     ; advance 256 B = 4 × 64 B line
4b:  bltu  a3, a2, 3c        ; until a3 ≥ 0x1000
4e:  isync
```

`iii` = **Invalidate I-cache Index**. `0x1000 / 64 = 256` lines = the whole 16 KB / 64 B / 4-way
cache, invalidated *before* it is enabled. The 4-unrolled `iii` with a `0x100` stride covers 4
lines per iteration. The loop bound `0x1000` is an independent OBSERVED confirmation of the
I-cache size. `[HIGH/OBSERVED]`

### 3.4 MEMCTL / PREFCTL defaults

```asm
51:  const16 a2, __memctl_default    ; 57: wsr.memctl  a2   (MEMCTL = 97)
5a:  const16 a2, __prefctl_default   ; 60: wsr.prefctl a2   (PREFCTL = 40)
```

The symbol values are weak **ABS** definitions in `memctl_default.o`, OBSERVED via `nm`/`-t`:

| Symbol | Value | Role |
|---|---|---|
| `__memctl_default` | `0xFFFFFF08` | snoop/way-enable/parity config; **low byte `0x08` leaves I-cache OFF** |
| `__memctl_default_post` | `0x00000009` | OR-ed in at §3.7 to **enable** the I-cache |
| `__prefctl_default` | `0x00003044` | arms the 8-entry prefetcher (`PREFETCH_ENTRIES 8`) |

MEMCTL is first written `0xFFFFFF08` (I-cache *disabled* at this point), PREFCTL `0x3044`.
The cache comes on only after the MPU is programmed (§3.7). `[HIGH/OBSERVED — byte-exact]`

### 3.5 WindowBase prime

```asm
63:  movi.n a2, 1
65:  slli   a2, a2, 30       ; a2 = 1 << 30 = 0x40000000
68:  wsr.wb a2               ; WB(72) ← 0x40000000
6b:  rsync
6e:  movi.n a0, 0
```

WB is primed to **`0x40000000`** — the initial WindowBase for the 64-AR / 8-window file —
before any `entry`/`retw` runs. `[HIGH/OBSERVED for the value; bit-30 field semantics
INFERRED as the standard XEA3 reset WindowBase.]`

> **CORRECTION — WindowBase is `0x40000000`, not `1`.** A sibling note paraphrased this as
> "`wsr.wb a2(=1)`". The byte-exact sequence `movi.n a2,1; slli a2,a2,30` makes the *written*
> value `1<<30 = 0x40000000`. Pin `0x40000000`.

### 3.6 The 16-entry MPU program — two paths

The handler programs **16 MPU foreground regions** under MPUENB-disabled state, by one of two
paths depending on whether an explicit `__xt_mpu_init_table` is linked.

**Entry gate + MPUENB clear:**

```asm
70:  const16 a2, __xt_mpu_init_table
76:  const16 a3, __xt_mpu_init_table_size
7c:  beqz a2, e3            ; no table        → cacheattr fallback (0xe3)
7f:  beqz a3, e3            ; no size ptr     → fallback
82:  l32i.n a3, (a3)        ; a3 = *size = entry count
84:  beqz a3, e3            ; zero entries    → fallback
87:  movi.n a9, 0
89:  wsr.mpuenb a9          ; MPUENB(90) ← 0  (disable fg regions during reprogram)
8c:  movi.n a10, 0         ; a10 = MPU index
8e:  movi.n a11, 15        ; a11 = 15
90:  j a0
```

**Primary table-driven path** — a two-loop construct (this is more nuanced than the "one
16-iter loop" summary a quick read suggests):

```asm
; LOOP A @a0 — pre-fill entries 0..14 with the background attr (a9 = 0)
a0:  memw
a3:  wptlb a10, a9          ; write MPU entry[a10] ← a9
a6:  addi.n a10, a10, 1
a8:  bltu  a10, a11, a0     ; while a10 < 15
ab:  beqz.n a3, e0          ; no table entries → done
ad:  addx8 a2, a3, a2       ; a2 = table_base + 8*count  (point PAST end; 8 B/entry)
b0:  j  ca

; LOOP B @c0 — walk the linked table in REVERSE, decrement the fill index a11
c0:  memw
c3:  wptlb a10, a12         ; write MPU entry (index in a10, attrs in a12)
c6:  addi.n a11, a11, -1
c8:  beqz.n a3, e0
ca:  addi   a2, a2, -8      ; step back one 8-byte table entry
cd:  l32i.n a10, (a2+4)     ; a10 = entry.word1 (vaddr | index field)
cf:  l32i.n a12, (a2+0)     ; a12 = entry.word0 (attrs / access-rights)
d1:  addi.n a3, a3, -1
d3:  extui  a9, a10, 0, 5   ; a9 = a10[4:0]
d6:  xor    a10, a10, a9    ; clear low 5 bits of a10
d9:  or     a10, a10, a11   ; OR in the descending fill index a11
dc:  j  c0
e0:  j  117                 ; → I-cache enable finalize
```

Loop A pre-clears the foreground entries; loop B consumes the `{word0=attrs, word1=vaddr}`
pairs of `__xt_mpu_init_table` from the top down, re-deriving each entry index by masking the
low 5 bits of the table's vaddr field and OR-ing in the descending counter `a11`. This is the
standard `_xtos_mpu_init` table program. `[HIGH/OBSERVED]`

**Cacheattr fallback** (taken when no MPU table is linked — the LSP-default path):

```asm
e3:  const16 a2, _memmap_cacheattr_reset   ; packed 32-bit, 8 nibbles
e9:  const16 a3, _xtos_mpu_attribs          ; the §3.0 5-word table
ef:  const16 a4, 0x2000                     ; region stride seed
f5:  movi.n a6, 8                           ; 8 attr nibbles → 8 MPU regions
f7:  movi.n a7, 1
<loop @f9>:
f9:  extui a8, a2, 28, 4    ; top nibble = this 512-MiB region's attr
fc:  addx4 a5, a8, a3       ; index _xtos_mpu_attribs by nibble
ff:  addi  a8, a8, -5
102: movgez a5, a3, a8      ; clamp out-of-range nibbles to the default row
105: l32i.n a5, (a5)        ; a5 = attrib word
107: slli  a2, a2, 4        ; advance to next nibble
...  ; wptlb a5, a7 ; bnez a6, f9
111: wptlb a5, a7
114: bnez  a6, f9
```

It synthesises MPU regions from the 8-nibble `_memmap_cacheattr_reset` word, mapping each
512-MiB attribute nibble to a `wptlb` write through the `_xtos_mpu_attribs` lookup. (Recall
§1: there is no CACHEATTR register; this *constant* drives the MPU.) `[HIGH/OBSERVED]`

> **NOTE — which path the FW takes.** Whether the shipped image links an explicit
> `__xt_mpu_init_table` or falls through to the cacheattr builder is a *link-time* choice; the
> reset object supports both. Either way the result is 16 programmed foreground regions, under
> MPUENB-disabled state, before interrupts or C code run. The full nibble→region map is the
> subject of a follow-on uarch note; this page pins the *mechanism*.

### 3.7 I-cache enable, user-init, jump to `_start`

```asm
117: rsr.memctl a2
11a: const16 a3, __memctl_default_post   ; = 0x09
120: or  a2, a2, a3                       ; set the I-cache-enable bits
123: wsr.memctl a2                        ; *** the 16 KB I-cache is now LIVE ***
126: const16 a2, __reset_user_init
12c: beqz.n  a2, 136                       ; weak user-init hook (default 0)
12e: callx0  a2
133: bnez    a2, 136
<unpackdone @136>:
136: wsr.ms a0                             ; MS(229) ← 0
139: rsync
13c: const16 a0, _start
142: callx0  a0                            ; *** enter crt1 _start ***
```

After this `callx0 _start`, hardware init is complete: vectors armed, caches invalidated then
enabled, prefetch armed, WB primed, 16 MPU regions live, MS cleared. `[HIGH/OBSERVED]`

> **NOTE — the plain `reset-vector.o` has no ROM unpack.** Its `unpackdone` label is reached
> directly from §3.6/§3.7. The decompression preamble lives only in the `reset-vector-unpack.o`
> variant (§5.2), which a FW image links when it must self-decompress initialized data from a
> boot ROM before `_start`.

---

## 4. The C-runtime — crt1 `_start` (reset → ready)

Three crt1 variants ship: `crt1-boards.o` (full board), `crt1-tiny.o` (minimal), `crt1-sim.o`
(ISS). The device FW uses one of the first two; both are decoded byte-exact here.

### 4.1 `crt1-boards.o` `_start` — the full sequence (`0x12d` bytes)

```asm
00:  movi a0, 0
03:  const16 a2, __stack_init ; const16 a1, __stack
0f:  beqz.n a2, 16 ; callx0 a2          ; optional stack-init hook; a1 = SP = __stack
16:  const16 a3, _stack_sentry
1c:  addmi  a3, a3, 0x100               ; + 0x100 headroom
1f:  addi.n a3, a3, 15
21:  srli   a3, a3, 4 ; slli a3, a3, 4  ; align UP to 16
27:  addi   a3, a3, 1                   ; +1
2a:  wsr.isl a3                         ; ISL = stack LIMIT (low-water guard)
2d:  movi a3, 128 ; 30: wsr.ps a3       ; PS = 0x80 = WindowOverflowEnable
33:  rsync
36:  const16 a4, __memmap_init ; beqz a4, 44 ; callx8 a4   ; optional memmap hook
44:  <BSS CLEAR — table loop over [_bss_table_start.._bss_table_end)>
9d:  const16 a8, board_init ; callx8 a8                     ; board_init (no-op stub, §5.3)
a6:  const16 a10, _start_argc ; l32i.n a10, (a10)           ; argc
b4:  const16 a4, __xtos_simc ; beqz 0xd1
     rsr.prid a5 ; extui a5, a5, 0, 4 ; beqz 0xd1
     const16 a8, _init_reent_bss ; callx8 a8                ; PRID-GATED per-core reent bss
d1:  const16 a12,_start_envp; a13,_init; a14,_fini
     const16 a8, __clibrary_init ; callx8 a8                ; *** C library init (ctors/reent) ***
ec:  const16 a10,_start_argc; l32i.n a10; a11,_start_argv; a12,_start_envp
100: const16 a8, main ; callx8 a8                            ; *** callx8 main(argc,argv,envp) ***
109: const16 a4, __xtos_simc ; beqz 0x124 ; rsr.prid…       ; sim-only _exit
11b: const16 a8, _exit ; callx8 a8
124: const16 a8, exit  ; callx8 a8                           ; main returned → exit
```

The **BSS clear** (`0x44..0x9c`) is table-driven: it walks `[_bss_table_start.._bss_table_end)`
as a list of `{start,end}` pairs and zeroes each, with a 16-byte unrolled inner body driven by
the **zero-overhead loop buffer** (`loopnez`, four `s32i.n` per turn) plus `bbci`-tested 4- and
8-byte tails. An optional `__bss_init` hook short-circuits the whole loop.

> **QUIRK — `_init_reent_bss` is PRID-gated.** Only core instances with **PRID low-4 ≠ 0** run
> `_init_reent_bss` (the simcall/newlib reentrancy bss); the PRID-0 core skips it. This is the
> multi-core (8-Q7) re-entrancy split — the C-runtime half of the same per-core PRID selection
> the SharedResetVector uses at boot (§5.1). `[HIGH/OBSERVED]`

The flow: reset → [§3 hw init] → SP/ISL/PS → `.bss` clear → `board_init` → PRID-gated reent bss
→ `__clibrary_init` → `callx8 main`. **The core is "ready" the instant `main` is entered.**
`[HIGH/OBSERVED]`

### 4.2 `crt1-tiny.o` `_start` — minimal (`0x51` bytes)

```asm
00:  movi.n a0, 0 ; const16 a1, __stack
08:  const16 a3, _stack_sentry ; addmi a3,a3,0x100 ; align16 ; addi.n a3,a3,1 ; wsr.isl a3
1e:  movi a3, 128 ; wsr.ps a3 ; rsync
27:  <BSS clear [_bss_start.._bss_end) by word — simple bltu loop>
3d:  const16 a8, main ; callx8 a8
46:  movi.n a2, 1 ; simcall 0 ; break 1,15 ; j 46   ; main never returns → trap + spin
```

Same skeleton (SP/ISL/PS/`.bss`/`main`) minus `board_init` / clibrary / reent / argv. If `main`
returns it traps (`break 1,15`) and spins. On the on-device Q7 the dispatch-loop `main` never
returns, so this tail is unreached. `[HIGH/OBSERVED]`

### 4.3 PS / ISL / MS at boot — the three state words

| SR | Set where | Value | Meaning |
|---|---|---|---|
| `MS` (229) | `_ResetHandler` §3.7 | `0` | single-context XEA3 dispatch live |
| `PS` (230) | `_start` §4.1/§4.2 | `0x80` | **WindowOverflowEnable** — windowed `entry`/`retw` spill/fill works once C runs |
| `ISL` | `_start` | `align16(_stack_sentry+0x100) + 1` | low-water **stack limit** guard |

The §3.5 `WB = 0x40000000` prime is the matching WindowBase for the PS-`WOE` window machine; a
store below `ISL` raises the stack-limit exception through the DispatchVector armed in §3.2.
`[HIGH/OBSERVED]`

> **NOTE — ENTRY / window facts that gate the ABI.** `XCHAL_HAVE_CALL4AND12 = 0` (§1) means
> only `CALL0` and `CALL8` forms assemble — `CALL4`/`CALL12` are **unassemblable** on this
> core, and indeed no `call4`/`call12`/`callx4`/`callx12` appears anywhere in the reset/crt
> objects. Every windowed call in crt1 is `callx8` (`+8` window rotation); the stub `entry`
> insns are `entry a1, 32` (the min-32 frame). With `NUM_AREGS 64` this is the **8-window**
> file (64 / 8). Get this wrong and the windowed spill/fill desyncs at the first `retw`.

---

## 5. Multi-core + ROM-unpack boot variants

### 5.1 The shared, PRID-gated reset vector — `shared-reset-vector.o`

The 8 Q7 pool cores share one IRAM image, so the reset entry fans each core to its own
ResetHandler. OBSERVED, byte-exact:

```asm
00000000 <_SharedResetVector>:
   0:  j  8                       ; → rtbase+4
00000004 <rtbase>:
   4:  .word _ResetTable_base     ; reloc R_XTENSA_32 — per-core entry-pointer table
00000008:
   8:  l32r   a1, 4               ; a1 = _ResetTable_base
   b:  rsr.prid a0                ; a0 = PRID
   e:  extui  a0, a0, 0, 4        ; a0 = PRID[3:0] = CORE INSTANCE ID (0..7/15)
  11:  addx4  a1, a0, a1          ; index the table by 4*core_id
  14:  l32i.n a1, (a1)            ; a1 = this core's ResetHandler entry
  16:  jx     a1                  ; dispatch to the per-core handler
```

`.ResetTable.rodata[0]` relocates to `_ResetHandler` (the default slot). The low 4 PRID bits
(`PRID_ID_SHIFT 0 / BITS 4 / MASK 0xF`, §1) select a per-core boot entry from
`_ResetTable_base`. This is the boot-time half of the same per-core PRID split crt1-boards uses
for `_init_reent_bss` (§4.1). The 8 Q7 cores boot the **same** vector but land on per-PRID
handlers. `[HIGH/OBSERVED]`

### 5.2 The ROM-store unpack — `reset-vector-unpack.o`

This variant is `reset-vector.o` plus a self-decompress preamble that copies initialized
sections out of a ROM store (`_rom_store_table`) into RAM before `_start`. Its
`.ResetHandler.text` is **`0x170` bytes** (the `_ResetHandler` body is `0x15c` — section minus
the `0x14` attrib prefix), vs `0x145`/`0x131` for the plain variant. The early body
(`0x14..0x117`) is identical; the tail differs:

```asm
; after I-cache enable (§3.7) and __reset_user_init:
135: const16 a2, _rom_store_table
13b: beqz.n  a2, 15e               ; no ROM store → straight to _start
<unpack @13d>:
13d: l32i.n a3, (a2+0)   ; dst
13f: l32i.n a4, (a2+4)   ; end
141: l32i.n a5, (a2+8)   ; src
143: addi.n a2, a2, 12   ; next descriptor (12-byte {dst,end,src} triples)
145: bgeu   a3, a4, 158  ; dst ≥ end → this triple empty → next
<uploop @148>:
148: l32i.n a6, (a5) ; addi.n a5,a5,4
14c: s32i.n a6, (a3) ; addi.n a3,a3,4
150: bltu   a3, a4, 148
153: j  13d
<upnext @158>:
158: bnez a3, 13d ; bnez a5, 13d   ; a {0,0,0} triple terminates
<unpackdone @15e>:
15e: isync
161: wsr.ms a0
164: rsync
167: const16 a0, _start ; 16d: callx0 a0
```

A classic `{dst, end, src}` triple-walk ROM-image decompressor; a zero triple terminates. The
unpack variant adds an `isync` before `wsr.ms` (the plain variant does not). This is how a FW
image whose `.data`/`.rodata` is stored packed in a boot ROM lands its writable sections in DRAM
before the C runtime touches them. `[HIGH/OBSERVED]`

> **CORRECTION — the unpack ResetHandler is `0x170` (section) / `0x15c` (body).** A sibling note
> gave "`0x15c`" as the handler size; that is the `_ResetHandler` *symbol body* (after the
> `0x14`-byte `_xtos_mpu_attribs` prefix). The section size is `0x170`. Both numbers are
> correct under their respective framing — section vs symbol-body — and `0x170 − 0x14 = 0x15c`.

### 5.3 Boot stubs that are no-ops on this config

```asm
<board_init>:           entry a1, 32 ; retw.n      ; libminrt.a — empty
<xtos_memep_initrams>:   entry a1, 32 ; retw.n      ; libhandlers-board.a — empty
```

Both are the RAM-ECC / memory-error-protection init that this `MEM_ECC_PARITY = 0` config (§1)
does not need; they ship as windowed no-op stubs. There is **no ECC scrub** in the Q7 boot
path. (The `entry a1, 32` is itself an OBSERVED witness of the 8-window / min-32-frame ABI.)
`[HIGH/OBSERVED]`

---

## 6. The `.globstruct` boot structure + the DRAM[0] ready sentinel

The on-device firmware image — the one the host BAR0-writes — is **embedded in
`libnrtucode_internal.so`**. Scanning that lib for `\x7fELF` with `e_machine = 94`
(Xtensa) finds six device ELF32-LE images; the first, at file offset `0x2ef7e0`, has
**`e_entry = 0x01005610`** — the dispatch-loop entry. Carving it and reading its section
headers (native `xtensa-elf-objdump -h`, VMA==file-offset for `.text`/`.rodata`/`.data`):

| Section | VMA | Size | Flags |
|---|---|---|---|
| `.text` | `0x01000000` | `0x6f1e` | CONTENTS, ALLOC, LOAD, RO, CODE |
| `.rodata` | `0x02000000` | `0x1ec` | CONTENTS, ALLOC, LOAD, DATA |
| `.data` | `0x02000240` | `0x140` | CONTENTS, ALLOC, LOAD, DATA |
| `kernel_info_table` | `0x02000380` | `0x88` | CONTENTS, ALLOC, LOAD, DATA |
| `.globstruct` | `0x02000408` | `0x48` | CONTENTS, ALLOC, LOAD, DATA |
| `.bss` | `0x02000450` | `0x3c` | ALLOC |

The program headers place `.rodata .data kernel_info_table .globstruct .bss` in **PT_LOAD
segment 01** (VirtAddr `0x02000000`, `FileSiz 0x450`, `RWE`). `.globstruct` is `CONTENTS, LOAD`
— i.e. it carries a **linked initializer** in the loadable image.

The first word, read directly from the carved file:

```
.globstruct[0] (VMA 0x02000408) = 0x6099CB34      ← the READY SENTINEL
```

The full `0x48`-byte structure:

| Off | Word | Off | Word | Off | Word |
|---|---|---|---|---|---|
| `+0x00` | `0x6099CB34` | `+0x18` | `0x00001000` | `+0x30` | `0xFFFFFF00` |
| `+0x04` | `0x00000000` | `+0x1c` | `0x00001000` | `+0x34` | `0xFFFFFF00` |
| `+0x08` | `0x00000000` | `+0x20` | `0x00001000` | `+0x38` | `0x00000000` |
| `+0x0c` | `0x00000000` | `+0x24` | `0x00001000` | `+0x3c` | `0x00000000` |
| `+0x10` | `0x00000000` | `+0x28` | `0xFFFFFF00` | `+0x40` | `0xFFFFFFFF` |
| `+0x14` | `0x00000000` | `+0x2c` | `0xFFFFFF00` | `+0x44` | `0xFFFFFFFF` |

`[HIGH/OBSERVED — value + location + full word map all read from the carved ELF.]`

> **CORRECTION — the ready sentinel is a *linked image initializer*, not a runtime store.**
> Because `.globstruct` is `CONTENTS, ALLOC, LOAD` inside the `0x02000000` PT_LOAD segment, the
> word `0x6099CB34` is **part of the image the host BAR0-writes into DRAM** — it is present in
> DRAM the moment the segment is installed, independent of whether the core has executed a
> single instruction. The firmware does not need a pre-loop store to publish it. This reframes
> the device→host "ready" word as an **image-identity magic** (which is exactly how the host
> treats it — the mismatch path reads literally *"booted image is incompatible"*, §7). The
> earlier framing ("the FW writes 0x6099CB34 before the dispatch loop") was an inference made
> before the loadable initializer was confirmed; pin the loadable-initializer model.

> **NOTE — why the FLIX store site can't be disassembled.** The `ncore2gp` `objdump` emits
> this image's `.text` FLIX bundles as **raw 4-byte words** (e.g. at `e_entry` the bytes decode
> as `bf004136 20003200 a4015083 …`, not instructions) — a known decode limitation for this
> Vision-Q7 FLIX config. The carving above sidesteps it entirely: the sentinel is a *data*
> word, read directly. (The scalar-LX management-core firmware is a different decode problem —
> see [The NCFW Scalar-LX Management Core](ncfw-lx-core.md).)

The `kernel_info_table` (17 entries × 8 B, `{pad,pad, u8 spec@+2, u8 opcode@+3, u32 funcVA@+4}`)
is the dispatch table the §9 loop scans; the first carved rows are
`{spec 0x00, op 0x7e → 0x01000080}`, `{0x00, 0x7c → 0x010003f8}`, … with the `op 0xf0` family
at entries 6–10 carrying `spec 0..4` (the POOL extended-opcode fan-out). `[HIGH/OBSERVED]`

---

## 7. The device↔host boot / claim handshake

`nrtucode_core_on_ucode_booted` — disassembled from the host x86
`libnrtucode.so` at **`0x308F90`** (`nm: T nrtucode_core_on_ucode_booted`). It is the host
counterpart of the device "ready" word, bound into the runtime as the
`on_ucode_booted = START` step of `ucode_core_create`. The exact flow:

```c
// nrtucode_core_on_ucode_booted(core)  — annotated from objdump @0x308f90
int nrtucode_core_on_ucode_booted(nrtucode_core_t *core /*rbx*/) {
    if (!core) return ...;                       // 0x308f98 null guard
    uint32_t v = 0;                              // 0x308fa1 movl $0, 0xc(%rsp)
    rw_impl_t *rw = core->rw_impl;               // (%rbx)  vtable
    void     *tgt = core->target;                // 0x20(%rbx)  = DRAM[0] addr
    // vtable slot 0 = read_device(rw, tgt, len=4, &v)
    if (rw->vtbl->read_device(...) != 0)         // 0x308fbd call *(%rax)
        return /*read error*/;                    // 0x308fc8 ret

    if (v == 0x6099CB34) {                        // 0x308fce cmp $0x6099cb34
        v = 0x502B2DA1;                           // 0x30900f stage CLAIM word
        // vtable slot +8 = write_device(rw, tgt, len=4, &v)
        if (rw->vtbl->write_device(...) != 0)     // 0x30902b call *0x8(%rax)
            return /*write error*/;               // 0x309030 jne bail
        core->boot_state = 1;                     // 0x309032 movl $1, 0x30(%rbx)
        return 0;                                 // success — core CLAIMED
    }
    if (v == 0x502B2DA1) {                        // 0x308fd7 already-claimed
        log("…Core is claimed by another nrtucode_core_t instance", "claim_core");
        return 8;                                 // 0x308fff call; 0x309004 mov $8
    }
    log("…Magic value `%x` mismatch (booted image is incompatible)", "claim_core", v);
    return 8;                                      // 0x309041 path
}
```

OBSERVED corroboration:
- the two compares are byte-exact: `41 81 f9 34 cb 99 60  cmp $0x6099cb34,%r9d` and
  `41 81 f9 a1 2d 2b 50  cmp $0x502b2da1,%r9d`;
- the claim write stages `c7 44 24 0c a1 2d 2b 50  movl $0x502b2da1,0xc(%rsp)` and calls the
  `+8` vtable slot;
- `boot_state` is `0x30(%rbx)` and set to `1` only on the success path;
- the log format strings carved from `.rodata` are exactly
  *"nrtucode: invalid API usage in `%s`: %s: Core is claimed by another nrtucode_core_t instance"*
  and *"…Magic value `%x` mismatch (booted image is incompatible)"*, with `%s` arg
  **`"claim_core"`** (the API name the runtime reports).

Immediate byte-counts (python3 scan):

| Constant | Bytes (LE) | `libnrtucode.so` | `libnrtucode_internal.so` |
|---|---|---|---|
| READY `0x6099CB34` | `34 cb 99 60` | 41 | **99** (host code + the embedded device images) |
| CLAIM `0x502B2DA1` | `a1 2d 2b 50` | 2 (the `cmp` + the `mov`) | 2 |

**Semantics:**

| Word | Direction | Meaning |
|---|---|---|
| `0x6099CB34` | device → host | "the booted image's identity magic is present at DRAM[0]" |
| `0x502B2DA1` | host → device | "this core is now **claimed** by THIS `nrtucode_core_t` handle" |
| any other | — | "booted image is incompatible" → fail 8 (version/identity gate) |
| read `0x502B2DA1` | — | "already claimed by another handle" → fail 8 (single-owner lock) |

This is a **one-word spin-mailbox claim**, not a hardware CAS: the host reads, branches, then
writes; serialization comes from the single-host-thread per-core bring-up ordering, not an
atomic. `[HIGH/OBSERVED — disasm + carved strings + byte counts.]`

> **GOTCHA — the read/write target is `core->target` at `0x20(%rbx)`, the boot_state is at
> `0x30(%rbx)`.** Do not conflate them. The handshake reads/writes 4 bytes at the *device*
> DRAM address held in `core->target`; `boot_state` is a *host-side* field of the
> `nrtucode_core_t`. A reimplementer wiring the `rw_impl` vtable must map slot 0 →
> `read_device` and slot +8 → `write_device`, both taking `(impl, target, len, buf)`.

---

## 8. Run-stall release — what lets the Q7 execute its reset vector

The reset vector (§2–§5) only runs after the core's **run-stall** is released. Out of SoC reset
all 8 Q7 cores are stalled. The release mechanism is the *only* per-arch difference in the
whole boot path, and it is strictly **upstream** of PC `0x0`:

| Generation | De-stall trigger | Boot vector |
|---|---|---|
| Cayman (v3) / Mariana (v4) | host writes the Q7 run-stall CSR `0xFF → 0x00` (broadcast-clear all 8 cores) after the BAR0 IRAM/DRAM install | identical |
| Sunda (v2) | host Q7 release hook is a no-op; the cluster is brought online by the SEQ/SP through the EVT_SEM fabric rendezvous gating the per-core run-state | identical |

`[CARRIED from the run-stall / bring-up cross-pages; the constants and CSR offsets are detailed
there.]` The image bytes (IRAM/DRAM) are installed **before** release, so the very first fetch
at `0x0` sees the linked DispatchVector + ResetVector. The on-device boot (§2–§7) is therefore
**arch-invariant**; only the de-stall differs.

See [The nrtucode Subsystem + Device Bring-Up](../runtime/nrtucode-bringup.md) for the host
install/release ordering, and [SEQ Boot / Entry Path](../firmware/seq/boot.md) for the Sunda
SEQ-side rendezvous (both authored in their own Parts).

---

## 9. Ready → main-dispatch-loop transition

On the Q7 firmware image, crt1 `main` **is** the per-instruction dispatch loop (`e_entry =
0x01005610`, OBSERVED §6). It linear-scans the 17-entry × 8-byte `kernel_info_table`
(`{spec@+2, opcode@+3, funcVA@+4}`, §6), matches the packed `(opcode, spec)` key of the
incoming instruction record, and `callx8`'s the matched `funcVA`. Its log string is
*"P%i: In dispatch, CPU ID: %0d, got opcode 0x%x."* (carved from the embedded image). The full
transition is:

```
reset (PC=0x0, run-stall released §8)
  → _ResetVector:  j _ResetHandler                                          §2
  → _ResetHandler: ISB+VECBASE arm → I-cache invalidate → MEMCTL/PREFCTL
                   → WB prime → 16-entry MPU → I-cache enable
                   → [ROM unpack §5.2] → MS:=0 → callx0 _start              §3,§5
  → crt1 _start:   SP/ISL/PS → .bss clear → board_init → reent (PRID)
                   → __clibrary_init → callx8 main                          §4
  → main == dispatch loop: .globstruct[0]=0x6099CB34 present in the loaded
                   image, scan kernel_info_table forever, callx8 kernels    §6,§9
  → [host] on_ucode_booted reads 0x6099CB34, writes 0x502B2DA1,
                   boot_state=1 → the core is CLAIMED and live              §7
```

The XEA3 DispatchVector (VECBASE, armed at §3.2) handles every interrupt/exception that arrives
*while* the dispatch loop runs: async interrupts fan out through the dispatch table; synchronous
faults through the cause table; the `JumpToResetHandler` tail (§2) re-enters `_ResetHandler` on
a reset-class event. **The boot path and the steady-state interrupt path share VECBASE.**
`[HIGH/OBSERVED + CARRIED for the dispatch-loop body.]`

---

## 10. Adversarial self-verification ledger

The five strongest boot-spine claims and their verdicts against the disassembly:

| # | Claim | Re-challenge | Verdict |
|---|---|---|---|
| 1 | Reset vector = 3-byte `j _ResetHandler` at PC `0x0` | `objdump -dr reset-vector.o`: `.ResetVector.text` is 3 bytes `00 00 06`, reloc `R_XTENSA_SLOT0_OP _ResetHandler`. Stale `<CS_SA_restore_label>` symbol disproved by the reloc. | **OBSERVED — holds** |
| 2 | 16-entry MPU program | `wptlb` loops at `@a0` (pre-fill) and `@c0` (table walk), with `movi.n a11,15` and `wsr.mpuenb a9(=0)`. Cacheattr fallback at `@e3` synthesises 8 regions from `_memmap_cacheattr_reset`. `MPU_ENTRIES 16` in `core-isa.h:801`. The "one 16-iter loop" summary is **imprecise** — corrected to the two-loop reality in §3.6. | **OBSERVED — refined** |
| 3 | DRAM[0] `0x6099CB34` → `0x502B2DA1` claim | carved firmware ELF `.globstruct[0] = 0x6099CB34` (VMA `0x02000408`); host `0x308f90` has both `cmp` immediates + the claim write + `boot_state=1`; strings match; counts 41/2 (host) and 99/2 (internal). | **OBSERVED — holds** |
| 4 | CALL8 / 8-window file | `XCHAL_HAVE_CALL4AND12 0` + `NUM_AREGS 64` → 8 windows; zero `call4`/`call12` in the objects; every windowed call is `callx8`; stubs use `entry a1,32`. | **OBSERVED — holds** |
| 5 | crt1 order: stack/ISL/PS → .bss → clibrary → main | `crt1-boards.o`: `__stack`→`wsr.isl`(align16(_stack_sentry+0x100)+1)→`wsr.ps 0x80`→table-driven `.bss`→`board_init`→PRID-gated `_init_reent_bss`→`__clibrary_init`→`callx8 main`. | **OBSERVED — holds** |

Two prior-note discrepancies were checked and **corrected in place**: the WindowBase value
(`0x40000000`, not `1`; §3.5) and the `_xtos_mpu_attribs` word byte-order (§3.0). Two sibling
"size" figures (`0x131` reset body, `0x15c` unpack body) were reconciled as *symbol-body* sizes
vs *section* sizes (`0x145` / `0x170`) — both correct, framed differently (§2, §5.2). The one
honest decode limit is the **FLIX `.text` of the firmware image**: the `ncore2gp` `objdump`
emits its bundles as raw words, so the in-loop kernel bodies are not disassembled here —
but every *boot-spine* fact (reset vector, handler, crt, sentinel value/location, handshake) is
read from a decodable object, a config header, a data section, or x86 host code. The
scalar-LX-vs-FLIX ambiguity does **not** touch the reset/crt objects (those are scalar XEA3 and
decode cleanly); it touches only the firmware image's vector kernels, which are out of scope
here.

---

## See also

- [The NCFW Scalar-LX Management Core](ncfw-lx-core.md) — the *other* core's firmware, decoded
  with the LX `op0 e/f`=3-byte-length rule (a distinct decode problem from the Q7's FLIX `.text`).
- [Clock / Reset / Power Domains](clock-reset-power.md) — the SoC-fabric reset/power domains and
  the privileged-core perimeter arming that sits upstream of the Q7 run-stall release.
- [Pipeline Timing Model](pipeline-timing.md) · [FLIX Co-Issue Matrix](co-issue-matrix.md) —
  the steady-state datapath the dispatch loop drives once `main` is live.
- [The nrtucode Subsystem + Device Bring-Up](../runtime/nrtucode-bringup.md) *(Part 8)* — the
  host install/release/claim ordering whose `on_ucode_booted` is §7.
- [SEQ Boot / Entry Path](../firmware/seq/boot.md) *(Part 5)* — the SEQ-side boot/rendezvous on
  Sunda that gates the §8 run-stall release.
- [Core Identity & Configuration](../isa/core/identity-config.md) — the full identity card the
  §1 boot knobs are a slice of.
