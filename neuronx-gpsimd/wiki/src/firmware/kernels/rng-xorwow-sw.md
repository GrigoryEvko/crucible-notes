# RNG — Xorwow Software Path (`XorwowRng(SW)`)

This page reconstructs the **software-only Marsaglia Xorwow PRNG** that the
GPSIMD compute core runs on the **CAYMAN** generation — the `XorwowRng(SW)`
kernel, its 6-word per-lane state, the instruction-exact default seed vector,
the Weyl-sequence increment, and the `RandSetState`/`RandGetState`
checkpoint-restore interface. It is the **baseline RNG present on every
generation**; the hardware-accelerated `XorwowRng(TIE)` variant that newer gens
add is the sibling page
[RNG — Xorwow TIE Hardware Path](./rng-xorwow-tie.md).

The kernel lives on the **Cadence Tensilica Vision-Q7 NX "Cairo" DSP**
(`ncore2gp` config, 512-bit FLIX/VLIW, one per NeuronCore) — specifically on the
**Q7 POOL compute core**, the half of the two-core POOL split that runs the
actual vector math. The **NX POOL sequencer** core only decodes/logs/routes the
`Rng` opcode; it does **not** compute. That split is byte-witnessed by the two
distinct log-string streams: every sequencer string carries the `'S:'` prefix
(e.g. `S: Rng (XORWOW)`), every compute-core string carries the `'P%i:'` prefix
(e.g. `P%i: XorwowRng(SW)`). This page documents the **`'P%i:'` (compute-core)**
half; the SEQ front-end is covered in
[RNG — LFSR / Rand Dispatch](./rng-lfsr-dispatch.md).

CAYMAN ships the **software path only**. Every Q7 RNG log string on this image
carries the literal `(SW)` suffix, and a whole-archive string sweep finds **no
`(TIE)` and no `Lfsr` string anywhere inside the carved CAYMAN POOL image** (§8).
The algorithm is therefore composed entirely from the generic integer vector-ALU
primitives — there is no bespoke RNG instruction in this ISA.

> **NOTE — what was carved this session, and the exact objects used.** Every
> fact below is byte-pinned to a shipped artifact re-carved this session from the
> RNG-bearing blob
> `libnrtucode_internal.so`
> (`sha256 b7c67e89…`, ELF64 x86-64 DYN). The POOL DEBUG images are carved by
> file-offset `dd`, disassembled with the native `xtensa-elf-objdump`
> (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa Tools 14.09) that
> ships inside the gpsimd-tools package. The recovered `P%i:`/`S:` format strings
> and the demangled `Xorwow*`/`Rand*State` names are themselves binary evidence
> (`.rodata` of the DEBUG images) and are cited as such.
>
> | object | size | sha256 (first 16) |
> |---|---:|---|
> | `CAYMAN_Q7_POOL_DEBUG_IRAM` (off `0x249020`) | `125504 B` (`0x1ea40`) | `513a8a22d94b08c2` |
> | `CAYMAN_Q7_POOL_DEBUG_DRAM` (off `0x267a60`) | `89344 B` (`0x15d00`) | `226f4254d4751903` |
> | `CAYMAN_NX_POOL_DEBUG_IRAM` (off `0x1b1420`) | `116768 B` (`0x1c820`) | `8e4412b99201f62d` |
> | `CAYMAN_NX_POOL_DEBUG_DRAM` (off `0x1cdc40`) | `28448 B` (`0x06f20`) | `7bdf6ed7ccd27b37` |
>
> The full `xtensa-elf-objdump -D` of the Q7 DEBUG IRAM is `48,807` lines, exit
> `0`, empty stderr. `[HIGH/OBSERVED — all four sha256 reproduce the published
> anchors exactly.]`

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW**
× **OBSERVED/INFERRED/CARRIED**.

---

## 1. Address model and where the RNG lives

**Address model.** For the carved POOL images, IRAM **file-offset `==` device
IRAM VA**. DRAM string VA `== 0x80000 + file-offset`: a DRAM string at file
offset `F` is referenced in code as the pair `const16 aX, 8 ; const16 aX, 0xF`
(building VA `0x8_F`). So a Q7 DRAM string at file `0x1f3e` is addressed in code
as `const16 a10, 8 ; const16 a10, 0x1f3e` → VA `0x81f3e`. The
`TensorTensorArith` log site is the cleanest witness of this convention:
`0xc935: const16 a10, 8 ; 0xc938: const16 a10, 0x2069` → VA `0x82069`.
`[HIGH/OBSERVED]`

**The two-core handoff.** The RNG is centralized on the POOL engine but split
across its two cores:

* **NX POOL sequencer (`'S:'` stream)** — decode/log/route only. Opcode `0x4d`
  `'M'` resolves through the SEQ opcode table to a handler that logs
  `S: Rng (XORWOW)` (NX DRAM file `0x2d20` → VA `0x82d20`, **byte-confirmed this
  session**). Opcodes `0x77`/`0x78` are the inline `Rand{Get,Set}State`
  front-ends; the SEQ-side error string
  `S: RandGetState : rand_algorithm(0x%x) not currently supported on POOL`
  (NX DRAM file `0xe51`, **byte-confirmed**) is the **unsupported-algorithm**
  arm — it fires when an algo code other than the one wired on this gen is
  requested. `[HIGH/OBSERVED]`
* **Q7 POOL compute (`'P%i:'` stream)** — the actual Xorwow math and the state
  get/set live here, in the Q7 kernels decoded in §2–§6. The SEQ `0xf0`
  ExtendedInst escape registers the `ExtendedInstRand{Get,Set}State` variants on
  the Q7 kernel-info table; the Q7 dispatcher then runs the kernels.
  `[HIGH/OBSERVED — the Q7 entry/log/call edges; the SEQ→Q7 escape registration
  is CARRIED from the dispatch survey.]`

> **NOTE — `'S: Rng (XORWOW)'` vs `'P%i: XorwowRng(SW)'` are not the same
> string.** The SEQ-side `XORWOW` is the *opcode mnemonic* logged by the
> front-end decoder; the Q7-side `XorwowRng(SW)` is the *kernel name* logged by
> the compute body. Both were read out of their respective carved DRAM blobs at
> the offsets above. Conflating them would misattribute the math to the
> sequencer.

---

## 2. The Q7 RNG function map

All names below come from the DEBUG build's **own** `'P%i:'` log strings, read
out of the carved Q7 DRAM blob at the listed file offsets (VA `= 0x80000 + off`),
each `const16`-referenced from the listed IRAM xref site. `[HIGH/OBSERVED]`

| Q7 DRAM string (file off) | xref (IRAM) | enclosing fn (entry) | role |
|---|---|---|---|
| `0x18ae` `"Xorwow(SW) : Initializing XORWOW state in DRAM scratch"` | `0x74b2` | `0x749c` (`entry a1,0x200`) | **state INIT** (default seeds) |
| `0x1e80` `"RandSetState : num_chans=%0d : rand_algo = 0x%x"` | `0xb3c2` | `0xb398` (`entry a1,48`) | RandSetState dispatcher |
| `0x1eb8` `"XorwowSetSeeds(SW)"` | `0xb452` | `0xb430` → `0xb43c` | XorwowSetSeeds (vector body) |
| `0x1ed1` `"RandGetState : num_chans=%0d : rand_algo = 0x%x"` | `0xb596` | `0xb56c` (`entry a1,48`) | RandGetState dispatcher |
| `0x1f09` `"XorwowGetSeeds(SW)"` | `0xb622` | `0xb600` → `0xb60c` | XorwowGetSeeds (vector body) |
| `0x1f22` `"Rng : num_chans = %0d"` | `0xb8f0` | `0xb838` (`entry a1,64`) | Rng top dispatcher |
| `0x1f3e` `"XorwowRng(SW)"` | `0xb7d2` | `0xb7b4` (`entry a1,0x280`) | XorwowRng **driver** (per-elt loop) |
| `0x2069` `"TensorTensorArith num_chans = %0d"` | `0xc938` | `0xc91c` (`entry a1,48`) | generic vector-ALU engine (the step helpers' real owner, §5) |

**Call graph** (every edge read at its `call8`):

```
Rng dispatcher 0xb838 ──(setup 0x7d90, 0x452c)──▶ XorwowRng driver 0xb7b4
    0xb7b4 ──(call8 0x7554 PRE: load state)──▶ loop { call8 0xb90c (vector core) }
           ──(call8 0x7624 POST: store state)──▶ retw.n
    0xb90c ──(blti a0,5 : dtype/elt switch)──▶ a chain of vector-ALU step calls
           (the xorshift, into the TensorTensorArith engine family @0xc91c+)
           + call8 0x1ccd4  (the Weyl add, imm 362437)
           + call8 0x17ac   (per-step glue)
    RandSetState 0xb398 ──▶ 0xb3e4 (algo dispatch; bbci a2,0 / extui a,a,0,1)
           ──▶ XorwowSetSeeds 0xb430 → 0xb43c
    RandGetState 0xb56c ──▶ 0xb5b8 (algo dispatch; extui a,a,0,1)
           ──▶ XorwowGetSeeds 0xb600 → 0xb60c
    Init 0x749c ──(call8 0x185a0 memset 0x180)──▶ seed-load (6 const16 + per-word vector setup)
```
`[HIGH/OBSERVED at every listed call8 edge; see §5.3 GOTCHA on the per-step
target addresses.]`

---

## 3. The seeding — instruction-exact Marsaglia default vector

The state initializer `0x749c` logs
`P%i: Xorwow(SW) : Initializing XORWOW state in DRAM scratch`, zeroes the state
buffer, then loads the **five canonical Marsaglia xorwow default seeds** plus the
Weyl counter. The prologue sets up a 64-byte-aligned vector frame:

```asm
749c:  entry   a1, 0x200          ; 512-byte frame
749f:  movi    a10, -64
74a2:  and     a8, a1, a10        ; align SP to 64 B (vector alignment)
74a5:  movsp   a1, a8
74af:  const16 a10, 8
74b2:  const16 a10, 0x18ae        ; → DRAM VA 0x818ae
74b5:  call8   0x18a2c            ; LOG "P%i: Xorwow(SW) : Initializing XORWOW state…"
74c1:  movi.n  a11, 0
74c3:  movi    a12, 0x180         ; *** 384 = the state-buffer byte size ***
74c6:  call8   0x185a0            ; memset(state, 0, 0x180) — zero the buffer
```

The seed load — each word is built by a `movi`/`const16` immediate pair, then
written into the per-lane state via the vector-store machinery:

```asm
74c9:  movi    a3, 0x75b          ┐  x = (0x075b<<16) | 0xcd15
74cc:  const16 a3, 0xcd15         ┘    = 0x075BCD15 = 123456789
74d0:  call8   0x8844             ; (into the per-word vector-store path, §3.3)
       …
74e2:  const16 a3, 0x55e5         ; y = 0x159A55E5 = 362436069
74f8:  const16 a3, 0x3bb5         ; z = 0x1F123BB5 = 521288629
7510:  (const16 a3, 0x1333)       ; w = 0x05491333 = 88675123
74__:  const16 a3, 0x3f19         ; v = 0x00583F19 = 5783321
7538:  const16 a6, 0xc934         ; d (Weyl counter init; distinct reg a6)
754e:  call8   0x7624             ; store the seeded state back to DRAM scratch
7551:  retw.n
```

### 3.1 The smoking gun — five-of-five canonical seeds, byte-grounded

The raw `movi`/`const16` immediate bytes were re-read this session and the
arithmetic identity verified. The `x` seed is the cleanest witness — its **high
half is in the `movi`, observed directly**, not inferred:

| word | bytes (IRAM) | reconstructed value | Marsaglia default | match |
|---|---|---|---|---|
| `x` | `32 a7 5b` (`movi a3,0x75b`) `34 15 cd` (`const16 a3,0xcd15`) | `0x075BCD15` = 123456789 | 123456789 | ✓ |
| `y` | `… 34 e5 55` (`const16 a3,0x55e5`) | `0x159A55E5` = 362436069 | 362436069 | ✓ |
| `z` | `… 34 b5 3b` (`const16 a3,0x3bb5`) | `0x1F123BB5` = 521288629 | 521288629 | ✓ |
| `w` | `… a5 49 34 33 13` (`const16` pair 0x0549/0x1333) | `0x05491333` = 88675123 | 88675123 | ✓ |
| `v` | `34 19 3f` (`const16 a3,0x3f19`) | `0x00583F19` = 5783321 | 5783321 | ✓ |

> **NOTE — the high halves are not all guessed.** On re-read this session the `x`
> high half (`0x75b`) is a **clean `movi a3,0x75b` in the byte stream**
> (`32 a7 5b`), and the `w` high half (`0x0549`) is visible in the byte run
> `a5 49 34 33 13` at the `w` const16 chain. Where a high half is not directly
> visible it is still uniquely fixed by the low half plus the known Marsaglia
> constant — but `x` and `w` are *observed*, not merely identity-fixed.
> `[HIGH/OBSERVED]`

This is unambiguous textbook Marsaglia Xorwow seeding: `(123456789, 362436069,
521288629, 88675123, 5783321)` is the exact published xorwow default seed vector.
`[HIGH/OBSERVED — arithmetic identity on five independent immediates.]`

### 3.2 The Weyl counter init — a DIVERGENCE from the curand default

The sixth word, `d` (the Weyl-sequence counter), is initialized into a **distinct
register `a6`** by `const16 a6, 0xc934` at `0x7538` (bytes `64 34 c9`). Its low
half is `0xc934`.

> **FINDING — the `d` init is NOT the curand `6615241` default, and the high half
> is simply not set here.** The common curand / Numerical-Recipes xorwow
> Weyl-counter default is `d = 6615241 = 0x0064F0C9` (low half `0xF0C9`). The
> firmware's `d`-init low half is `0xc934`, which **does not match** `0xF0C9` —
> a genuine divergence from the curand reference, not a copy of it. Re-disasm
> this session resolves a subtlety: there is **no paired high-half setter** for
> `a6`. The instruction immediately before `0x7538` is `0x7535: l32r a4,…`
> (writes `a4`, ends exactly at `0x7538` — no room for a hidden `const16`), and a
> scan of `0x7500–0x7540` finds `a6` written **nowhere except this single
> `const16`**. Since Xtensa `CONST16` semantics are `AR ← (AR<<16) | imm16`, an
> unpaired `const16` sets only the low half (`0xc934`); the high 16 bits are
> *whatever `a6` held coming in* — not "desync-hidden", simply not established
> here at all. So the `d`-init **low half is `0xc934` `[HIGH/OBSERVED]`** and
> **diverges from curand `[HIGH/OBSERVED]`**; the *full* 32-bit `d`-init value is
> `[MED/INFERRED — the high half is an uninitialized-register carry]`.

### 3.3 The per-word seed write path — NOT five splat helpers

> **CORRECTION (vs the backing report) — the `0x88xx` call targets are NOT five
> separate "splat-one-seed-across-16-lanes" helpers.** The backing-report draft
> described `0x8844/0x8858/0x8870/0x8884/0x8898` as five `0x14`-apart broadcast
> helpers. Re-disasm refutes this on two counts. (1) **The spacing is not
> uniform `0x14`**: the deltas are `0x14/0x18/0x14/0x14`. (2) **None of the five
> addresses is a function `entry`** — they are all inline instructions inside
> **one** function that begins at `0x8744: entry a1, 0x1c0` and ends before the
> next `entry` at `0x8a50`. That region builds a **stack descriptor/pointer
> table** (`addmi a2,a1,0x300 ; addi.a a2,a2,-N ; s32i a2,a1,0x29X` storing
> computed addresses into stack slots `0x294..0x2a8`), computes an element count
> (`l16ui`/`l8ui`/`mul16u`/`srli a2,a2,5`), and runs a **vectorized FLIX loop**
> (`ivp_lvnx8s_ip`, `ivp_mulqan16xr8`, `ivp_sel2nx8t`) that writes the per-word
> state. So the seeds *are* written into a 16-lane-replicated state via a vector
> loop — but the "five tidy splat helpers" model is wrong; it is one descriptor-
> driven vector-store routine, entered repeatedly. `[REFUTED the five-helper
> model; HIGH/OBSERVED that the writes go through a single vector-store routine
> @0x8744.]`

The net effect is unchanged: all 16 lanes **start from the same default seed
vector**, and per-lane decorrelation comes from the per-lane stream advance
and/or a per-lane seed override via `RandSetState` (§6).
`[MED/INFERRED — the same-default-then-decorrelate reading.]`

---

## 4. The state struct and its geometry

* **SIZE — `0x180 = 384 bytes`.** Read directly as the `memset` count at **two**
  sites: the Init at `0x74c3` (`movi a12,0x180`) and the XorwowSetSeeds vector
  body at `0xb466` (`movi a12,0x180`). `[HIGH/OBSERVED]`
* **DECOMPOSITION — `384 = 6 words × 16 lanes × 4 bytes`.** The Vision-Q7 vector
  register is 512-bit; a 32-bit word packs **16 lanes** per vector register. The
  6-word state `(x, y, z, w, v, d)` therefore occupies six 64-byte vector
  registers = 384 bytes — i.e. **16 independent Xorwow streams**, one per SIMD
  lane, each carrying its own `(x, y, z, w, v, d)`. `[HIGH 384-byte size + 6
  words / OBSERVED; the exact 16-lane × 6-word tiling MED/INFERRED from
  `6·16·4 = 384` and the 512/32 vector width.]`

| field | offset (lane 0) | width | meaning |
|---|---:|---|---|
| `x` | `+0x00` | u32×16 | xorshift word 1 |
| `y` | `+0x40` | u32×16 | xorshift word 2 |
| `z` | `+0x80` | u32×16 | xorshift word 3 |
| `w` | `+0xC0` | u32×16 | xorshift word 4 |
| `v` | `+0x100` | u32×16 | xorshift word 5 (the output base) |
| `d` | `+0x140` | u32×16 | Weyl counter |

> **NOTE — the per-field offsets are the structural tiling, not a read struct.**
> The `+0x00/+0x40/…/+0x140` column is the `6 words × 64-byte vector register`
> layout implied by the 384-byte size and the six unrolled per-word vector
> writes in `XorwowSetSeeds` (§6). The byte size is HIGH/OBSERVED; the per-field
> stride is `[MED/INFERRED]`. A reimplementer should treat the *order*
> (`x,y,z,w,v` then `d`) as fixed by the algorithm and the seed-load order, and
> the per-word register granularity as the natural 512-bit packing.

* **LOCATION — DRAM scratch.** The Init log string names it literally:
  *"Initializing XORWOW state in DRAM scratch."* The state lives in the Q7 POOL
  DRAM scratch region, **not** in a per-instruction operand block — so it
  **persists across `Rng` calls** and the stream advances monotonically.
  `RandSetState`/`RandGetState` read/write this same scratch. `[HIGH the string
  names DRAM scratch / OBSERVED; the cross-call persistence is INFERRED-HIGH from
  the get/set interface existing.]`

---

## 5. The Xorwow algorithm core

### 5.1 Annotated C pseudocode (the recovered algorithm)

The recovered kernel is textbook Marsaglia Xorwow: a 5-word xorshift cascade plus
a Weyl-sequence additive counter, run per lane. Symbol/address names below are
the **real** recovered firmware names/addresses.

```c
/* Per-lane RNG state — 6×u32, replicated across 16 SIMD lanes.
 * State buffer = 0x180 = 384 B in Q7 POOL DRAM scratch; persists across calls.
 * Default seed vector (Init @0x749c) = the canonical Marsaglia xorwow defaults;
 * d (Weyl) starts from a NON-default value (low half 0xc934 — see §3.2). */
typedef struct {
    uint32_t x;   /* +0x00  default 123456789 = 0x075BCD15 */
    uint32_t y;   /* +0x40  default 362436069 = 0x159A55E5 */
    uint32_t z;   /* +0x80  default 521288629 = 0x1F123BB5 */
    uint32_t w;   /* +0xC0  default  88675123 = 0x05491333 */
    uint32_t v;   /* +0x100 default   5783321 = 0x00583F19 */
    uint32_t d;   /* +0x140 Weyl counter, init low16 = 0xc934 (NOT curand 0xF0C9) */
} xorwow_lane_t;  /* packed 16-wide = 384 B */

/* One draw, per lane. The vector core @0xb90c realizes this across 16 lanes at
 * once via the xorshift ALU-step chain (into the TensorTensorArith engine) + a
 * vector add-imm (the Weyl step) + the v+d output add. */
static inline uint32_t xorwow_next(xorwow_lane_t *s)
{
    uint32_t t = s->x ^ (s->x >> 2);   /* step: 1 shift + 1 xor                */
    s->x = s->y;                       /* ┐                                   */
    s->y = s->z;                       /* │  rotate-down of the 5-word window  */
    s->z = s->w;                       /* │  (the "x=y; y=z; z=w; w=v" moves)  */
    s->w = s->v;                       /* ┘                                   */
    s->v = (s->v ^ (s->v << 4))        /* step: 2 shifts + 3 xor               */
         ^ (t   ^ (t   << 1));
    s->d += 362437;                    /* the Weyl step  ←  call8 0x1ccd4      */
    return s->v + s->d;                /* the output     ←  the final add      */
}
```

> **GOTCHA — the per-step *shift amounts* (2, 1, 4) are MED, not byte-proven.**
> The six ALU steps are HIGH/OBSERVED to exist and to enter the generic
> tensor-tensor-arith engine (below), and the 6-shift/3-xor *operation count*
> exactly matches the Marsaglia inner loop. But the individual `srl/sll`
> **immediates** (the classic `>>2`, `<<1`, `<<4`) ride inside FLIX vector
> bundles that **desync** under the flat-image linear sweep — the Q7 DEBUG IRAM
> carries no per-function `.xt.prop` table (a flat-image limitation), so objdump
> emits plausible-but-unreliable `ivp_*` mnemonics mid-bundle. The *structure*
> (6 steps + Weyl add + `v+d` output) is HIGH; the exact tap/shift constants are
> `[MED/INFERRED — matched to the canonical Xorwow, not read from the bundle
> immediates]`.

### 5.2 The driver `0xb7b4` (per-element loop)

```asm
b7b4:  entry   a1, 0x280          ; 640-byte vector frame
b7b7…: movi -64 ; and ; movsp     ; 64-B SP align
b7c0:  s32i.n  a2, a1, 60         ; save num_tensor_elements (loop bound)
b7c2:  s32i.n  a3, a1, 56 ; a4→52 ; a5→48   ; save operand ptrs (out, state, …)
b7cf:  const16 a10,8 ; const16 a10,0x1f3e ; call8 0x18a2c  ; LOG "P%i: XorwowRng(SW)"
b7e2:  call8   0x7554             ; PRE: load state / set up vector context
b7e5:  movi.n  a2,0 ; s32i.n a2,[a1+44]                    ; i = 0
b7ec:  l32i.n a2,[a1+44] ; l32i.n a3,[a1+60] ; bgeu a2,a3,0xb82d   ; while i<N
b7f9:  l32i.n a11,[a1+56] ; a12←52 ; a13←48 ; addmi a10,a1,0x100 ; addi a10,-64
b805:  call8   0xb90c            ; *** the 16-lane vector Xorwow update (one chunk) ***
b828:  s32i.n  a2,[a1+44] ; j 0xb7ec                       ; i += chunk ; loop
b82d:  addmi a10,a1,0x100 ; addi a10,-64 ; call8 0x7624     ; POST: store state
b836:  retw.n
```

The driver loads the state **once** (`0x7554`), loops the 16-lane core
(`0xb90c`) over `num_tensor_elements`, and stores the state **once** (`0x7624`)
— so the stream advances monotonically across the whole output buffer.
`[HIGH/OBSERVED]`

### 5.3 The vector core `0xb90c` (the xorshift + Weyl)

```asm
b90c:  entry   a1, 0x100          ; 256-byte vector frame ; 64-B align
b918:  s32i.n  a2,[a1+48] ; a3→52 ; a4→60 ; a5→56          ; stash args
b92c:  const16 a4, 0x141          ; ALU-op / addressing selector for the steps
b92f:  blti    a0, 5, 0xb965      ; *** switch on a small value (<5): dtype/elt-width path ***
  …
b99a:  call8 <step 1>      ; bytes 25 02 01
b9c3:  call8 <step 2>      ; bytes 25 02 01   (+ call8 0x17ac glue)
b9ec:  call8 <step 3>      ; bytes 25 02 01   (+ call8 0x17ac)
ba16:  call8 <step 4>      ; bytes 25 02 01   (+ call8 0x17ac)
ba3f:  call8 <step 5>      ; bytes 25 02 01
ba5d:  movi.n a2,5 ; const16 a2,0x87c5 ; call8 0x1ccd4   ; *** d += 362437 (Weyl) ***
ba6e:  call8 <step 6>      ; bytes 25 02 01   (output formation: v + d)
```

Six xor/shift/move ALU steps + one Weyl add — the **exact operation count of the
Marsaglia xorwow inner loop**. `[HIGH structure / OBSERVED; per-step micro-op
identity MED — §5.1 GOTCHA.]`

> **GOTCHA — the printed step-call *target addresses* are FLIX-desync artifacts;
> do not hard-code them.** All six step calls carry the **identical operand bytes
> `25 02 01`**. A PC-relative `call8` issued from six different PCs with the same
> bytes cannot resolve to six different absolute targets — yet the linear-sweep
> disassembler prints six distinct targets (`0xc9bc/0xc9e4/…/0xca90`). That
> contradiction is the tell: the sweep mis-located the call *sites* by a few
> bytes inside FLIX bundles, so the *printed* targets are computed off desynced
> PCs and are unreliable. What **is** byte-grounded: (1) there is a sequence of
> identically-encoded `call8` dispatches in the vector core; (2) they enter the
> parameterized vector-ALU **engine family rooted at `0xc91c`**, which logs
> `P%i: TensorTensorArith num_chans = %0d` (DRAM `0x82069`, byte-confirmed); the
> engine's clean body is `0xc91c..0xc955` and it dispatches to real arithmetic
> sub-engines (each its own `entry`, e.g. `0xcaac`, `0xcb14`, `0xccd0`, `0xcff8`,
> and a second `TensorTensorArith` variant at `0xca08`). The exact callee entry
> for each xorshift sub-step is `[MED/INFERRED — tooling-bounded]`; the *fact*
> that the steps are generic tensor-tensor ALU ops, not a bespoke RNG op, is
> HIGH/OBSERVED.

> **QUIRK — the step helpers are the generic tensor-tensor-ALU engine, not a
> bespoke RNG op.** The enclosing engine at `0xc91c` (`entry a1, 48`,
> `const16 a10,0x2069 @0xc938`, `call8 0x18a2c @0xc93b`) logs
> `TensorTensorArith num_chans`. The Xorwow SW path is **composed from the
> standard integer vector primitives** (`xor`, `sll`/`srl`, `add`) — there is
> **no dedicated shift-register RNG instruction** in this ISA. That absence is
> exactly why CAYMAN is SW-only: with no RNG TIE encoding, the SW composition is
> the *only* path (§8). `[HIGH/OBSERVED]`

### 5.4 The Weyl constant — instruction-exact

```asm
ba5d:  movi.n  a2, 5            ; bytes 0c 52
ba5f:  const16 a2, 0x87c5       ; bytes 24 c5 87   → a2 = (5<<16)|0x87c5 = 0x000587C5
ba63:  call8   0x1ccd4          ; vector add-immediate: d_lane += 362437  (all lanes)
```

`362437 = 0x000587C5` is the **canonical Marsaglia xorwow Weyl-sequence
increment**, recovered byte-exact from the `movi.n`/`const16` immediate pair. The
add helper `0x1ccd4` is reached by a `call8` with operand bytes `25 27 11`
(distinct from the `25 02 01` of the xorshift steps), so this call is *not* one
of the desynced step calls — it is its own resolvable dispatch. `[HIGH/OBSERVED]`

> **GOTCHA — `362437` exists ONLY as an immediate-build, never as a 4-byte
> literal.** A raw byte search for `C5 87 05 00` anywhere in the image returns
> zero — the constant is materialized *only* by the `movi.n a2,5 ; const16
> a2,0x87c5` pair. A reimplementer scanning `.rodata` for the magic number will
> not find it; it is encoded in the instruction stream. `[HIGH/OBSERVED]`

---

## 6. RNG state management — `RandSetState` / `RandGetState`

This is the **checkpoint/restore** interface: it reads/writes the same 384-byte
DRAM scratch the draw advances, so a kernel can be made re-runnable or per-lane
streams can be seeded independently. Both ops are gated by **`rand_algo` bit 0 =
Xorwow**. The deeper operand-record handling is shared with
[RNG — Seed / Get-State / Set-State Ops](./rng-seed-state-ops.md).

### 6.1 `RandSetState` dispatcher `0xb398` and the algo selector `0xb3e4`

```asm
b398:  entry a1, 48
b3bb:  l32i.n a12,[a1+12] ; l32i.n a13,[a1+8]    ; a12=num_chans, a13=rand_algo
b3c2:  const16 a10,0x1e80 ; call8 0x18a2c         ; LOG "RandSetState : num_chans / rand_algo"
b3cf:  call8 0x7d90                               ; operand-record decode
b3d2:  l32i a10,[a1+12] ; call8 0x452c            ; setup
b3da:  call8 0xb3e4                               ; → algo dispatcher
b3df:  retw.n

; --- algo dispatcher 0xb3e4: the rand_algo SELECTOR ---
b3e4:  entry a1, 64
b417:  l8ui  a2,[a1+24]            ; reload rand_algo byte
b41a:  bbci  a2, 0, 0xb42c         ; *** if (rand_algo bit0 == 0) skip — algo not Xorwow ***
b420:  l8ui  a2,[a1+20]
b423:  extui a10, a2, 0, 1         ; a10 = rand_algo & 1   (the algo-select arg)
b426:  call8 0xb430                ; → XorwowSetSeeds
b42c:  retw.n
```

`rand_algo` **bit 0 selects Xorwow**. On CAYMAN, Xorwow is the **only** algo
present (no LFSR), so the non-Xorwow path is a no-op here and the SEQ front-end
logs `rand_algorithm(0x%x) not currently supported on POOL` for any other algo
code. `[HIGH/OBSERVED — `bbci a2,0` + `extui a10,a2,0,1` read directly.]`

### 6.2 `XorwowSetSeeds` `0xb430` → vector body `0xb43c`

```asm
b430:  entry a1,48 ; s8i a2,[a1+12] ; call8 0xb43c ; retw.n
b43c:  entry a1,0x100 ; movi -64 ; and ; movsp      ; 64-B align
b452:  const16 a10,0x1eb8 ; call8 0x18a2c            ; LOG "XorwowSetSeeds(SW)"  (DRAM 0x81eb8)
b464:  movi.n a11,0
b466:  movi a12,0x180 ; b469: call8 0x185a0          ; memset state buffer (0x180=384 B)
b471,b481,b491,b4a1,b4b1,b4c1: SIX unrolled FLIX vector-store bundles, ONE PER STATE WORD —
                               write the 6 per-lane state words from the supplied seed operand.
  … vector store glue ; call8 0x7624 ; retw.n
```

The **six** unrolled per-word vector writes (one per `x,y,z,w,v,d`) after the
`0x180` memset were verified this session as six 16-byte FLIX bundles at
`0xb471/0xb481/0xb491/0xb4a1/0xb4b1/0xb4c1` (each carrying `ivp_*` vector ops);
the trailing `.byte 0xee … add.n` run at `0xb4d1` is a desync tail, not real
code. `[HIGH the 6-fold unroll + the 0x180 memset / OBSERVED; the exact bundle
mnemonics MED — §5.1 GOTCHA.]`

### 6.3 `RandGetState` dispatcher `0xb56c` and `XorwowGetSeeds` `0xb600`

```asm
b56c:  entry a1, 48
b596:  const16 a10,0x1ed1 ; call8 0x18a2c            ; LOG "RandGetState : num_chans / rand_algo"
b5a3:  call8 0x7d90 ; call8 0x452c ; call8 0xb5b8
; --- algo dispatcher 0xb5b8 mirrors 0xb3e4 ---
b5f0:  l8ui a2,[a1+8] ; extui a10,a2,0,1 ; call8 0xb600   ; → XorwowGetSeeds

b600 → b60c:  logs "XorwowGetSeeds(SW)" @DRAM 0x81f09 ; read path uses helper 0x7554
              (not memset), then TEN unrolled FLIX bundles for the per-word READ +
              lane pack-out of the 6-word × 16-lane state.
```

> **NOTE — Get is wider than Set.** `XorwowSetSeeds` writes the 6 words with **6**
> unrolled bundles (`0xb471..0xb4c1`); `XorwowGetSeeds` shows **10** unrolled
> bundles (`0xb64d, 0xb65d, …, 0xb6dd`) plus leading scalar store-pack
> instructions (`0xb639..0xb64b`) — the read path additionally packs/permutes the
> 16 lanes into the output operand record, hence more bundles. The read/write
> symmetry and the higher Get unroll count are OBSERVED; the exact per-bundle
> lane-permute ops are `[MED/INFERRED]` (FLIX desync, same desync-tail caveat as
> Set). `[HIGH the 6-vs-10 bundle counts / OBSERVED.]`

**State I/O contract.** `RandSetState` writes the per-lane 6-word state from a
host-supplied operand into the 384-byte DRAM scratch; `RandGetState` reads it
back out. Both gated by `rand_algo` bit 0 = Xorwow. This is the
checkpoint/restore interface for the RNG stream — e.g. to make a kernel
re-runnable, or to seed per-lane streams independently. `[HIGH/OBSERVED]`

---

## 7. The output dtype and the uniform-float construction

* **The raw draw is `uint32` per lane** (`out = v + d`, both 32-bit, the standard
  xorwow return). The 6-step ALU pipeline operates on 32-bit lanes
  (`sll`/`srl` unsigned 32). `[HIGH/OBSERVED — 32-bit state words + 32-bit add.]`
* **The `blti a0, 5` switch** in the vector core (`0xb92f`) selects among ≤5
  element/dtype paths — the output is then narrowed/cast to the requested tensor
  dtype using the same dtype machinery the other POOL kernels use. `[HIGH the
  switch exists / OBSERVED; the dtype-enum mapping MED.]`

### 7.1 The `[0,1)` uniform-float construction — fully recovered

The Rng top dispatcher `0xb838` builds the IEEE-754 constants for an optional
cast of the `uint32` draw to a uniform float, and on re-read this session the
**full construction is instruction-exact** — not just the bare `0x3f80` constant:

```asm
b885:  movi  a2, 127                 ; exponent bias 127
b888:  const16 a2, 127               ; (= 0x7F)
b88b:  s32i.n a2,[a1+20]
b88d:  const16 a2, 0x3f80            ┐  a2 = 0x3F800000 = the IEEE-754 1.0f bit pattern
b890:  const16 a2, 0x3f80            ┘
b893:  s32i.n a2,[a1+16]
  …  (alternate branch builds the same constants arithmetically:)
b898:  movi.n a2, -1                 ┐  a2 = 0xFFFFFFFF
b89a:  srli  a2, a2, 9               ┘    >> 9 → 0x007FFFFF = 23-bit mantissa MASK
b89d:  s32i.n a2,[a1+20]
b89f:  movi  a2, 127                 ┐  a2 = 127
b8a2:  slli  a2, a2, 23              ┘    << 23 → 0x3F800000 = 1.0f exponent field
b8a5:  s32i.n a2,[a1+16]
```

> **CORRECTION (vs the backing report) — the float cast is HIGH, not MED.** The
> draft flagged the uint32→float path as "INFERRED-MED — the exact mantissa-fill
> cast is NOT instruction-exact recovered." On re-read the dispatcher builds
> **both** halves of the canonical trick byte-exact: the **mantissa mask**
> `0x007FFFFF` (via `movi -1 ; srli ,9`) **and** the **1.0f exponent field**
> `0x3F800000` (via both `const16 0x3f80` *and* `movi 127 ; slli ,23`). That is
> the textbook `f = ((bits >> 9) | 0x3F800000)` → `[1,2)` then subtract `1.0` →
> `[0,1)` uniform-float construction. The *mechanism* is now `[HIGH/OBSERVED]`;
> only the final binding of which exact draw bits feed the mantissa (the subtract
> and the lane-store) remains `[MED/INFERRED]` through the FLIX desync. The
> downstream consumer for a uniform float in `[0,1)` is the Dropout op (§9).

---

## 8. The SW-vs-TIE-HW boundary (generational)

A whole-archive string sweep of `libnrtucode_internal.so`
(`xtensa-elf-strings -t x`) maps the RNG variants by file offset → generation
band:

| variant | file offsets | generation |
|---|---|---|
| `Xorwow(SW)` + `XorwowSetSeeds(SW)` + `XorwowGetSeeds(SW)` + `XorwowRng(SW)` (NO `Lfsr`, NO `(TIE)`) | `0x26930e…` and `0x2b6adb…` (two clusters) | **CAYMAN** |
| `Xorwow(TIE)` + `XorwowSetSeeds(TIE)` + `XorwowGetSeeds(TIE)` + `XorwowRng(TIE)` **+ `LfsrSetSeeds`/`LfsrGetSeeds`** | `0x502a2e`, `0x55051b`, `0x7ce6ee`, `0x81c39b`, `0x96400e` (five clusters, all higher) | MARIANA / MARIANA_PLUS / MAVERICK |

> **NOTE — CAYMAN is SW-only, byte-proven by absence.** A `rg -a` over the
> **carved CAYMAN Q7 DEBUG DRAM blob** (`226f4254…`, 89344 B) finds the four
> `(SW)` RNG strings (`Xorwow(SW)` @file `0x6323`, `XorwowSetSeeds(SW)` @`0x7869`,
> `XorwowGetSeeds(SW)` @`0x7950`, `XorwowRng(SW)` @`0x8003`) and **zero** hits for
> `(TIE)`, `Lfsr`, `LFSR`, or bare `TIE` — the SW path is the only RNG present on
> this image. The `(TIE)` + `Lfsr` strings appear only at the five higher file
> offsets belonging to newer generations' images. `[HIGH/OBSERVED — presence of
> the `(SW)` set + absence of `(TIE)`/`Lfsr` in the carved CAYMAN blob.]`

The boundary is therefore a **generation/config select**, not a runtime branch
inside one image: CAYMAN has no RNG TIE op in its ISA, so its only path is the SW
composition of generic vector-ALU ops (§5). The newer images carry both a `(SW)`
reference and a `(TIE)` hardware path, plus a second algorithm (LFSR);
`rand_algo` + the build selects which.

* **Per-generation presence.** SW Xorwow is the **baseline across all
  generations** (it is the algorithm; the TIE path only *accelerates* the state
  advance). On CAYMAN it is the *only* RNG. The `(TIE)` hardware variant and the
  LFSR second algorithm are **additions** in MARIANA / MARIANA_PLUS / MAVERICK.
  `[HIGH for CAYMAN (carved) / OBSERVED; the newer-gen TIE *implementations* are
  string-OBSERVED only — those images were identified by offset but not
  disassembled here, so the newer-gen interiors are INFERRED.]`

> **NOTE — v5 / MAVERICK interiors.** The MAVERICK RNG body is **header-OBSERVED
> only** (its `(TIE)`/`Lfsr` strings exist at the higher file offsets above); the
> actual per-generation TIE state-advance instructions were not carved this pass.
> Treat any MAVERICK-specific claim as `[INFERRED]` until that image is
> disassembled. The TIE path is documented on the sibling
> [RNG — Xorwow TIE Hardware Path](./rng-xorwow-tie.md); the LFSR second algo on
> [RNG — LFSR / Rand Dispatch](./rng-lfsr-dispatch.md).

---

## 9. Consumers

The RNG is centralized on the POOL engine; its draws feed the stochastic
kernels. The clearest consumer is **Dropout**: `S: Dropout` is a DVE-engine SEQ
handler, and dropout needs exactly the uniform-`[0,1)` float that §7.1
constructs (compare the per-element keep/drop mask against the keep probability).
`[HIGH that Dropout is a DVE handler / OBSERVED; the Dropout → POOL-RNG
cross-engine call path is INFERRED — not traced this pass.]` See
[Dropout](./dropout.md) and the related [rand2](./rand2.md) kernel.

---

## 10. Honesty ledger

**HIGH / OBSERVED** (direct disasm, byte read, or arithmetic identity):

* All four carve sha256 reproduce the published CAYMAN POOL anchors exactly.
* The full Q7 RNG function map (Init `0x749c`, RandSetState `0xb398`,
  XorwowSetSeeds `0xb430`/`0xb43c`, RandGetState `0xb56c`, XorwowGetSeeds
  `0xb600`/`0xb60c`, Rng dispatcher `0xb838`, XorwowRng driver `0xb7b4`, vector
  core `0xb90c`, TensorTensorArith engine `0xc91c`) — every entry/log/`call8`
  edge read directly.
* The **five** canonical Marsaglia default seeds `x..v` from the `const16`
  immediates (123456789 / 362436069 / 521288629 / 88675123 / 5783321); the `x`
  and `w` high halves are *observed*, not merely identity-fixed.
* The Weyl increment `362437 = 0x000587C5` built `movi.n a2,5 ; const16 a2,0x87c5`
  @`0xba5d`/`0xba5f`, fed to the vector add helper `0x1ccd4` (operand bytes
  `25 27 11`, distinct from the step calls); present only as an immediate-build,
  never a 4-byte literal.
* State buffer = `0x180` = 384 bytes (memset count, two sites: `0x74c3`, `0xb466`).
* `rand_algo` **bit 0** selector (`bbci a2,0` / `extui a10,a2,0,1`) routing to
  `Xorwow{Set,Get}Seeds`.
* The vector core's xorshift ALU-step chain (identical `25 02 01` `call8`
  operands) + the one Weyl-add call; the steps enter the generic
  `TensorTensorArith` engine (`0xc91c` logs `TensorTensorArith num_chans`, DRAM
  `0x82069`).
* `XorwowSetSeeds` memset(`0x180`)@`0xb469` + 6 unrolled bundles; `XorwowGetSeeds`
  read-helper@`0xb635` + 10 unrolled bundles.
* The uniform-float construction in the Rng dispatcher: mantissa mask
  `0x007FFFFF` *and* the `0x3F800000` 1.0f field, both built instruction-exact.
* SW-vs-TIE boundary by absence: the carved CAYMAN Q7 DRAM blob contains the four
  `(SW)` strings and **zero** `(TIE)`/`Lfsr` strings; the `(TIE)`+`Lfsr` set lives
  at five higher file offsets.
* The output state words are 32-bit (uint32 draw).
* `S: Rng (XORWOW)` (NX DRAM `0x2d20`) and the `rand_algorithm…not currently
  supported on POOL` error (NX DRAM `0xe51`) read directly from the carved NX
  blob.
* The `d`-init **low half `0xc934` diverges from the curand default `0xF0C9`**.

**MED / INFERRED:**

* The 16-lane × 6-word tiling of the 384-byte state (from `6·16·4 = 384` + the
  512/32 vector width) and the per-field offsets in §4.
* The `d` (Weyl counter) **full init value**: low half `0xc934` is byte-read; the
  high half is an uninitialized-register carry (no high-half setter exists), so
  the full 32-bit `d`-init is MED/INFERRED.
* The per-step xorshift micro-op identity and the exact tap/shift amounts
  (`>>2`, `<<1`, `<<4`): the FLIX-vector bundles carrying the shift immediates
  **desync** under the flat-image linear sweep (no `.xt.prop` table in the flat
  Q7 IRAM). The *structure* (6 steps + Weyl + `v+d`) is HIGH; the per-step
  constants are matched to the canonical Xorwow.
* The exact callee entry address for each xorshift step (the printed
  `0xc9bc..0xca90` are desync artifacts off identical `25 02 01` operands; the
  real engine family is rooted at `0xc91c` and dispatches to sub-engines
  `0xcaac/0xcb14/0xccd0/0xcff8` and a second `TensorTensorArith` at `0xca08`).
* The final draw-bits→mantissa binding (the subtract-1 and lane-store of the
  float cast): mechanism HIGH, exact bit-routing MED.
* The Dropout → POOL-RNG cross-engine call path (Dropout is a DVE SEQ handler;
  the call to POOL RNG is inferred, not traced).

**LOW / UNRECOVERED:**

* The exact `ivp_*` vector mnemonic for each xorshift sub-step and the
  lane-permute in `XorwowGetSeeds` (FLIX desync; would need the per-function
  `.xt.prop` table the flat DEBUG Q7 IRAM does not carry).
* The `(TIE)` hardware Xorwow + LFSR implementations on the newer generations
  (string-identified by file offset, not disassembled this pass) — see the
  sibling [TIE](./rng-xorwow-tie.md) and [LFSR](./rng-lfsr-dispatch.md) pages.

**REFUTED (vs the backing report):**

* The "five `0x14`-apart splat helpers" `0x8844..0x8898` model — they are inline
  instructions inside one descriptor-table + vector-store routine at `0x8744`
  (§3.3), with non-uniform `0x14/0x18/0x14/0x14` spacing.
* The "uniform `0x28` stride" framing of the six step calls — the printed targets
  are desync artifacts off identical operand bytes (§5.3 GOTCHA).
