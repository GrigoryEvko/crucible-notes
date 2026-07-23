# RNG — Xorwow TIE Hardware Path

This page reconstructs the **`XorwowRng (TIE)`** path — the so-called "hardware-offload"
Xorwow PRNG that the **MARIANA / MARIANA_PLUS / MAVERICK** generations carry on their
**Q7 POOL** compute core, where the older CAYMAN carries only the software
`XorwowRng (SW)` path documented in
[RNG — Xorwow Software Path](rng-xorwow-sw.md). The reader arriving here expecting a
bespoke shift-register RNG instruction — a `xorwow.step` opcode, a hardware seed
register, an LFSR datapath — should read the **headline finding** first and recalibrate:

> **CORRECTION — there is no dedicated RNG / Xorwow / LFSR TIE *instruction*, and no
> hardware RNG state register.** The `(TIE)` suffix on the newer generations' RNG log
> strings is a **generation/build variant label** on the *same* Marsaglia Xorwow
> *software* kernel, not a bespoke opcode. The shipped `ncore2gp` ISA decode tables
> (`libisa-core.so`) contain **zero** rng/xorwow/lfsr/prng opcode and **zero** rng/seed
> state register; the shipped value oracle (`libfiss-base.so`) models **zero** RNG
> datapath primitive. The only `rand`-stemmed mnemonics are the **reduce-AND-bool**
> predicate-reduction family (`ivp_randbn` / `ivp_randb2n`), siblings of the
> reduce-OR-bool `ivp_rorbn` — nothing to do with random-number generation. `[HIGH/OBSERVED]`

What *did* move from CAYMAN to MARIANA is examined in detail below: the same five
Marsaglia seeds, the **byte-identical** Weyl constant, the same DRAM-scratch state
model, the same generic `TensorTensorArith` vector-ALU mix engine, the same `uint32`
output, the same `0x3F800000` float seam — **plus** a second algorithm (LFSR) wired
behind a `rand_algo` selector bit, a larger `Init` frame, and an extra driver-setup
block that is the *only* MED-confidence candidate for an actual newer-silicon hardware
hook.

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md):
**HIGH / MED / LOW** × **OBSERVED / INFERRED / CARRIED**. The disassembler is the
native Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, Vision-Q7 Xtensa24
FLIX/VLIW) that ships in the gpsimd-tools package; every address below is read out of a
carved DEBUG image with that tool. The flat DEBUG IRAM carries **no `.xt.prop` FLIX
property table**, so densely-scheduled vector bundles desync under the linear sweep —
that limitation (inherited from the SW page and the carve substrate) bounds the
per-step micro-op recovery to MED and is flagged inline wherever it bites.

> **NOTE — MAVERICK (v5) interiors are header-OBSERVED only.** The MARIANA TIE body is
> carved and disassembled here byte-by-byte; MARIANA_PLUS is corroborated from the
> identical string/byte layout; **MAVERICK's DEBUG IRAM symbol is nm-aliased onto its
> DEBUG DRAM address**, so its *interior* body was not separately disassembled. Every
> MAVERICK claim about the *body* is therefore **INFERRED** (identical-family from the
> byte-identical DRAM string layout), and tagged as such. MAVERICK *presence* of the
> `(TIE)` + LFSR string set is HIGH/OBSERVED.

---

## 1. Carve provenance and the per-generation payload matrix

The RNG-bearing container is the multi-generation firmware blob carried inside the
host customop library:

```
extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/
  custom_op/c10/lib/libnrtucode_internal.so
  sha256 b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b
  size   10,276,288 bytes
```

This sha256 and size match the
[rng-xorwow-sw.md](rng-xorwow-sw.md) and [dropout.md](dropout.md) anchors **exactly**.
`[HIGH/OBSERVED]`

The host `.so` carries a symbol table of the embedded device images
(`img_<GEN>_<ENGINE>_<FLAVOR>_<MEM>_contents.c` / `…_get.data`), read with
`nm --print-size --defined-only`. Five generations carry a `Q7_POOL` compute core — the
RNG-bearing core — confirmed independently by the runtime core-kind enum string
`NRTUCODE_CORE_{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL` (assert at container
strings offset `0x4b9a`). The Q7_POOL DEBUG image VAs (host `nm`; for `.text`/`.rodata`
the host file offset == device VA per the carve identity):

| GEN | DEBUG_IRAM (va / size) | DEBUG_DRAM (va / size) | this page |
|---|---|---|---|
| `SUNDA` | *(RELEASE-only — no DEBUG image)* | — | no RNG |
| `CAYMAN` | `0x249020` / `0x1ea40` | `0x267a60` / `0x15d00` | SW anchor |
| `MARIANA` | `0x4e2440` / `0x1ed40` | `0x501180` / `0x15d80` | **decoded here** |
| `MARIANA_PLUS` | `0x7adf40` / `0x1ef00` | `0x7cce40` / `0x15d80` | corroborated |
| `MAVERICK` | *(IRAM nm-aliased to DRAM)* | `0x962860` / `0x15480` | string-confirmed |

> **GOTCHA — these carve VAs name the `Q7_POOL` variant, *not* `NX_POOL`.** The container
> ships *two* POOL-core images per generation: the `NX_POOL` sequencer (the `'S:'` SEQ
> front-end) **and** the `Q7_POOL` compute core (the `'P%i:'` math kernels). The RNG math
> lives on **Q7_POOL**. The carve below is `MARIANA_Q7_POOL_DEBUG_IRAM_get.data`
> (`0x4e2440`), which is distinct from `MARIANA_NX_POOL_DEBUG_IRAM` (a different image at
> a different VA). When reproducing, select the **Q7_POOL** symbol — the hashes below pin
> it.

> **NOTE — SUNDA ships RELEASE-only.** Unlike CAYMAN/MARIANA/MARIANA_PLUS (which each
> ship both NX_POOL and Q7_POOL DEBUG images) and MAVERICK (Q7_POOL DEBUG only), SUNDA
> carries no DEBUG image at all — only a RELEASE Q7_POOL flavor. That, plus the empty
> string scan below, is why SUNDA's RNG absence is provable rather than merely
> log-stripped.

**Carved** (`dd if=<container> bs=1 skip=<va> count=<size>`, then native
disasm `xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x0`):

| object (nm symbol) | va / size | sha256 (first 12) | role |
|---|---|---|---|
| `MARIANA_Q7_POOL_DEBUG_IRAM` | `0x4e2440` / `0x1ed40` | `47f766292c90` | the TIE body decoded here |
| `MARIANA_Q7_POOL_DEBUG_DRAM` | `0x501180` / `0x15d80` | `02cacff07e19` | (string source) |
| `CAYMAN_Q7_POOL_DEBUG_IRAM` | `0x249020` / `0x1ea40` | `513a8a22d94b` | A/B reference |
| `CAYMAN_Q7_POOL_DEBUG_DRAM` | `0x267a60` / `0x15d00` | `226f4254d475` | (SW string source) |

The `CAYMAN_Q7_POOL_DEBUG_IRAM` sha `513a8a22…` and `…DRAM` sha `226f4254…` reproduce the
SW-path / IMG anchors **exactly**; the `MARIANA_Q7_POOL_DEBUG_IRAM` sha `47f76629…`
(full: `47f766292c90a1057b77f5f32b678750491c1d68f00326dbbe166c75b96ef63e`) is the new
MARIANA anchor for this page. The carve also re-confirms the SEQ-side `NX_POOL` anchors
`CAYMAN_NX_POOL_DEBUG_IRAM` (`8e4412b9…`, `116768 B`) and `…DRAM` (`7bdf6ed7…`,
`28448 B`).

> **NOTE — address model.** For these flat DEBUG images, IRAM file-offset == device IRAM
> VA. A DEBUG-DRAM string at container offset `F` lies at device VA `0x80000 + (F −
> dram_base)`, and is referenced from IRAM code as the const16 pair
> `const16 a,8 ; const16 a,0x<rel>`. The MARIANA Q7_POOL DRAM begins at container file
> offset `0x501180`; the TIE-Init string observed at container offset `0x502a2e` sits at
> DRAM-relative `0x502a2e − 0x501180 = 0x18ae` — exactly the const16 immediate the IRAM
> loads at `0x759e`.

---

## 2. The SW-vs-TIE boundary, string-offset-exact

A whole-container string sweep (`strings -t x` / `xtensa-elf-strings -t x`) places every
RNG log string in its generation container by file offset — the raw hits (container
file offsets):

| string | offsets (container file off) | count |
|---|---|---|
| `Xorwow(SW) : Initializing XORWOW state in DRAM scratch` | `0x26930e`, `0x2b6adb` | 2 |
| `Xorwow(TIE) : Initializing XORWOW state in DRAM scratch` | `0x502a2e`, `0x55051b`, `0x7ce6ee`, `0x81c39b`, `0x96400e` | 5 |
| `XorwowRng(SW)` | `0x26999e`, `0x2b6f32` | 2 |
| `XorwowRng(TIE)` | `0x5030e6`, `0x55099a`, `0x7ceda6`, `0x81c81a`, `0x9646c6` | 5 |
| `XorwowSetSeeds(TIE)` / `XorwowGetSeeds(TIE)` | (5 each, co-resident) | 5 / 5 |
| `LfsrSetSeeds` / `LfsrGetSeeds` | (5 each, co-resident with TIE) | 5 / 5 |

`[HIGH/OBSERVED]` The
two `(SW)` hits land in `CAYMAN_Q7_POOL_DEBUG_DRAM` (`0x26930e`) and the CAYMAN
dynamic-kernel-load DEBUG variant (`0x2b6adb`); the five `(TIE)` hits span MARIANA,
MARIANA_PLUS, and MAVERICK Q7_POOL DEBUG (+ their DKL variants), as range-mapped by nm.

Mapping the offsets to the payload matrix of §1 yields the **per-generation RNG
presence**, the answer to *"all generations or only the newer silicon?"*:

| GEN | RNG presence | proof |
|---|---|---|
| `SUNDA` | **no RNG handler at all** | no `xorwow`/`lfsr` string anywhere in the SUNDA payload region; **all** Xorwow-family offsets are ≥ `0x1d0960`, the lowest of which lies in `CAYMAN_NX_POOL_DEBUG_DRAM` — none in SUNDA. SUNDA is RELEASE-only. |
| `CAYMAN` | **SOFTWARE Xorwow only** | both `(SW)` strings (`0x26930e`, `0x2b6adb`) fall in the CAYMAN Q7 DEBUG / DKL-DEBUG DRAM; **no** `(TIE)`, **no** `Lfsr`. |
| `MARIANA` | **Xorwow(TIE) + LFSR** | `(TIE)` Init `0x502a2e`, `XorwowRng(TIE)` `0x5030e6`, plus `LfsrSetSeeds`/`LfsrGetSeeds`. |
| `MARIANA_PLUS` | **Xorwow(TIE) + LFSR** | the MPLUS build of MARIANA; same string set at `0x7ce6ee`/`0x7ceda6`/`0x81c39b`/`0x81c81a` (DEBUG + DKL). `[HIGH/OBSERVED strings]` |
| `MAVERICK` | **Xorwow(TIE) + LFSR** | `(TIE)` Init `0x96400e`, `XorwowRng(TIE)` `0x9646c6`, `Lfsr*` present. **String HIGH; body INFERRED** (IRAM nm-aliased to DRAM `0x962860`). |

> **QUIRK — the SW/TIE choice is a *compile-time* generation/build object select, not a
> runtime branch.** The `(SW)` and `(TIE)` strings are **never co-resident** in one
> Q7_POOL image: CAYMAN ships the SW kernel object, MARIANA+ ship the "TIE"-build kernel
> object. *Within* a TIE image the only runtime RNG branch is the Xorwow-vs-LFSR
> `rand_algo` select (§8). There is no per-image SW↔TIE runtime fork.

The boundary therefore reads: **`(TIE)` + LFSR appear on MARIANA and newer; CAYMAN is
SW-only; SUNDA has no RNG.** The newer label is generation+build, not a new RNG opcode.

---

## 3. The MARIANA TIE function map

All addresses are MARIANA Q7_POOL DEBUG IRAM; the enclosing function entry and the
self-naming `'P%i:'` log string are read directly from the carved disasm. The
DRAM-relative string offset is the const16 immediate loaded at the xref site.

| DRAM rel | `'P%i:'` string | xref site | enclosing fn (entry) | role |
|---|---|---|---|---|
| `0x18ae` | `Xorwow(TIE) : Initializing XORWOW state in DRAM scratch` | `0x759e` | `0x7588` (`entry a1,0x400`) | TIE STATE INIT |
| `0x1a3f` | `Decode: ExtendedInstRandGetState` | `0x869a` | `0x8688` (`entry a1,48`) | ext-inst decode |
| `0x1aa1` | `Decode: ExtendedInstRandSetState` | `0x87d2` | `0x87c0` (`entry a1,64`) | ext-inst decode |
| `0x1e80` | `RandSetState : num_chans / rand_algo` | `0xb61e` | `0xb5f4` (`entry 48`) | RandSetState dispatcher |
| `0x1eb8` | `LfsrSetSeeds` | `0xb716` | `0xb6dc` (`entry 48`) | **shared** SetSeeds (algo fork) |
| `0x1ecb` | `XorwowSetSeeds(TIE)` | `0xb75a` | `0xb6dc` (**same entry**) | the Xorwow arm of the fork |
| `0x1ee5` | `RandGetState : num_chans / rand_algo` | `0xb95a` | `0xb930` (`entry 48`) | RandGetState dispatcher |
| `0x1f1d` | `LfsrGetSeeds` | `0xba26` | `0xb9ec` (`entry 48`) | **shared** GetSeeds |
| `0x1f30` | `XorwowGetSeeds(TIE)` | `0xba6e` | `0xba58` (`entry a1,0x580`) | the Xorwow arm |
| `0x1f4a` | `Rng : num_chans = %0d` | `0xbf90` | `0xbf74` (`entry 48`) | Rng top dispatcher |
| `0x1f66` | `XorwowRng(TIE)` | `0xbd57` | `0xbc78` (`entry a1,0x100`) | **the DRIVER** |

`[HIGH/OBSERVED]`

The call graph (HIGH/OBSERVED at every `call8` edge):

```
Rng dispatcher 0xbf74 --(call8 0x7fd0 decode ; 0x44f0 setup)--> 0xbed8 --> DRIVER 0xbc78
DRIVER 0xbc78  self-names "XorwowRng(TIE)" (0xbd57)
              loops, calling vector setup/core 0xcda8
                (which logs "TensorTensorArith num_chans" @DRAM 0x2092)
              + step calls 0x15af8/0x15b28, 0x1da00/0x1da30, 0x25908/0x25938, 0xcda8
              Weyl add at 0xbe08/0xbe0a : movi.n a2,5 ; const16 a2,0x87c5
RandSetState 0xb5f4 --(log 0x1e80)--> 0x7fd0,0x44f0 --> dispatcher 0xb640
              --> ALGO FORK (rand_algo bit) --> shared SetSeeds 0xb6dc
                  { log Lfsr 0xb716  OR  log Xorwow(TIE) 0xb75a }
                  --> 6 unrolled FLIX state-word writes (0xb769..)
RandGetState 0xb930 --(log 0x1ee5)--> mirror --> shared GetSeeds 0xb9ec / 0xba58
TIE Init 0x7588 --(log 0x18ae)--> 5 seed const16 + 5 broadcast call8
                  (0x8820/0x8838/0x884c/0x8864/0x8878) + d-init const16 a6,0xc924
```

**Structural deltas vs the CAYMAN SW map** (cf [rng-xorwow-sw.md](rng-xorwow-sw.md) §2):

- **TIE Init frame is LARGER**: `0x7588: entry a1,0x400` (1024 B) vs SW `0x749c: entry
  a1,0x200` (512 B). The larger
  frame is working space, not state (the 6-word state size is inherited — §7).
- **The Set/Get bodies are now SHARED entries** (`0xb6dc` / `0xb9ec`) that fork
  *internally* to the Lfsr or Xorwow log. On CAYMAN, `XorwowSetSeeds`/`GetSeeds` had their
  own dedicated entries — the LFSR second algo is what merged them.
- **The TIE DRIVER `0xbc78` carries early extra setup** — calls `0x9f40`/`0x9f90` and
  `const16 a0,0x8362` (twice), plus slot-0 bytes the linear sweep renders as
  `break 0,12 / break 0,2 / break 0,0`. These are **not** present in the SW driver. They
  are the most plausible locus of a newer-silicon configuration / coprocessor-mode setup,
  but they sit inside FLIX-desync'd spans and are **MED, not byte-pinned**.
  `[MED/INFERRED]`

---

## 4. Seed init — instruction-exact, identical to the SW path

The MARIANA TIE Init at `0x7588` ("`Xorwow(TIE) : Initializing XORWOW state in DRAM
scratch`") is reproduced below from the carved disasm. The seed words are recovered from
the `const16` immediates; the 32-bit values are the unique Marsaglia defaults, so the
desync-hidden high half is determined by arithmetic identity.

```c
/* MARIANA Q7_POOL DEBUG IRAM @0x7588 — Xorwow(TIE) state Init (defaults).
 * Bytes shown are the exact const16 encodings read from MAR_IRAM.dis. */
void xorwow_tie_init(state_t *st /* DRAM scratch */) {
    /* 7588: entry  a1, 0x400        (360108)  -- 1024-byte vector frame; cf SW 0x200 */
    /* 758b..7591: movi a10,-64 ; and ; movsp  -- align SP to 64 B (vector alignment) */
    /* 759b: const16 a10, 8                                                            */
    /* 759e: const16 a10, 0x18ae     (a4ae18)  -- TIE Init log string                  */
    /* 75a1: call8   0x18d0c                    -- LOG "Xorwow(TIE): Initializing ..."  */

    /* 75a8: movi   a2, 0x75b        (22a75b)                                          */
    /* 75ab: const16 a2, 0xcd15      (2415cd)  -> x = 0x075BCD15 = 123456789           */
    broadcast(st->x, 0x075BCD15);   /* 75af: call8 0x8820 */

    /* 75c1: const16 a2, 0x55e5      (24e555)  -> y = 0x159A55E5 = 362436069           */
    broadcast(st->y, 0x159A55E5);   /* 75c5: call8 0x8838 */

    /* 75d7: const16 a2, 0x3bb5      (24b53b)  -> z = 0x1F123BB5 = 521288629           */
    broadcast(st->z, 0x1F123BB5);   /* 75db: call8 0x884c */

    /*                                          w = 0x05491333 = 88675123              */
    broadcast(st->w, 0x05491333);   /* 75ef: call8 0x8864 */

    /* 7602: const16 a2, 0x3f19      (24193f)  -> v = 0x00583F19 = 5783321             */
    broadcast(st->v, 0x00583F19);   /* 7606: call8 0x8878 */

    /* 7617: const16 a6, 0xc924      (6424c9)  -- d (Weyl counter init; distinct reg a6) */
    st->d = /* low 0xc924, high desync-hidden — MED */;
}
```

**Arithmetic identity (the smoking gun) — all const16 immediates byte-read**
`[HIGH/OBSERVED]`**:**

| seed | low16 const16 | bytes | value | Marsaglia default | verdict |
|---|---|---|---|---|---|
| x | `0xcd15` | `2415cd` | `0x075BCD15` | 123456789 | MATCH |
| y | `0x55e5` | `24e555` | `0x159A55E5` | 362436069 | MATCH |
| z | `0x3bb5` | `24b53b` | `0x1F123BB5` | 521288629 | MATCH |
| w | *(via 0x8864)* | — | `0x05491333` | 88675123 | MATCH (4th broadcast) |
| v | `0x3f19` | `24193f` | `0x00583F19` | 5783321 | MATCH |

> **NOTE — the seed VECTOR is unchanged from CAYMAN SW.** These are byte-for-byte the
> same five canonical Marsaglia Xorwow default seeds the [SW path](rng-xorwow-sw.md) §3
> loads. The TIE build did **not** re-seed.

> **GOTCHA — the `d` (Weyl counter) init constant diverges by one nibble between SW and
> TIE, and it is MED in *both*.** MARIANA loads `const16 a6,0xc924` (`6424c9` @`0x7617`);
> CAYMAN loads `const16 a6,0xc934` (`6434c9` @`0x7538`). The low half is clean; the high
> half is hidden by FLIX desync and `a6` is a separate register, so the *exact* d-init
> value is MED in each image. The *role* (Weyl counter `d`, advanced by `+362437`) is HIGH
> from the Weyl step in §5. Do **not** hard-code the d-init seed from this read — it is a
> single-nibble-divergent MED value behind the desync line. `[MED/OBSERVED low half / HIGH
> role]`

The five seed-broadcast helpers (`0x8820`/`0x8838`/`0x884c`/`0x8864`/`0x8878`, each
`0x14` apart) splat each scalar 32-bit seed across the SIMD lanes into the DRAM-scratch
state — the same splat-then-per-lane-decorrelate model as the SW path.
`[HIGH the 5 calls / MED splat semantics]`

---

## 5. The Weyl step and the mix engine — same algorithm, generic vector ALU

### 5.1 The Weyl constant — byte-identical to CAYMAN

```c
/* MARIANA TIE driver 0xbe08 — the canonical Xorwow Weyl-sequence increment */
/* be08: movi.n  a2, 5            (0c52)                                     */
/* be0a: const16 a2, 0x87c5       (24c587)  -> a2 = 0x000587C5 = 362437      */
st->d += 362437;                /* "d += 362437" applied to all lanes        */
```

The same `movi.n a2,5 ; const16 a2,0x87c5` pair appears in the CAYMAN SW driver at
`0xba5d`/`0xba5f`. Both byte-sequences:

| image | addr | bytes | decode |
|---|---|---|---|
| MARIANA TIE | `0xbe08` / `0xbe0a` | `0c52` / `24c587` | `movi.n a2,5 ; const16 a2,0x87c5` |
| CAYMAN SW | `0xba5d` / `0xba5f` | `0c52` / `24c587` | `movi.n a2,5 ; const16 a2,0x87c5` |

> **GOTCHA — `362437` is never a 4-byte literal; a raw byte-search for it returns zero.**
> `0x000587C5` is built **only** via this `movi+const16` pair, in *both* the SW and TIE
> images. The encoding bytes `24c587` are byte-identical across the two generations —
> this is the single strongest piece of evidence that the TIE path performs the **exact
> same Weyl step**. `[HIGH/OBSERVED]`

### 5.2 The mix engine is the generic `TensorTensorArith`, not a bespoke RNG op

The TIE driver's vector setup/core `0xcda8` logs DRAM `0x2092` =
`TensorTensorArith num_chans = %0d` — it enters the **same** generic parameterized
tensor_tensor vector-ALU engine the CAYMAN SW path enters (CAYMAN's core `0xc91c` logs
the identical `TensorTensorArith`; the MARIANA hit is at
container offset `0x503212`, DRAM-relative `0x2092`). The xorshift is therefore
**composed from the generic int vector ALU primitives** (xor / sll / srl / add — the
ISS value grid `xor_512 / sll_u / srl_u / add_32`), exactly as on CAYMAN. **No dedicated
RNG / shift-register instruction is issued.**

The Marsaglia inner loop the six ALU steps + one Weyl add realize is the textbook form
(operation count matches the SW page's six-step decomposition):

```c
/* The Xorwow xorshift+Weyl draw — same as the SW path, run through the generic
 * TensorTensorArith vector engine (NOT a bespoke RNG op). Per lane: */
uint32_t xorwow_draw(state_t *st) {
    uint32_t t = st->x ^ (st->x >> 2);          /* 1 shift + 1 xor             */
    st->x = st->y; st->y = st->z;               /* rotate-down of the 5-word   */
    st->z = st->w; st->w = st->v;               /*   xorshift window           */
    st->v = (st->v ^ (st->v << 4)) ^ (t ^ (t << 1));  /* 2 shifts + 3 xor      */
    st->d += 362437;                            /* the Weyl add  (0x000587C5)  */
    return st->v + st->d;                        /* the uint32 draw            */
}
```

> **GOTCHA — the per-step shift amounts and exact ivp mnemonics are MED, the same wall as
> the SW path.** A band-local ivp-vocabulary diff (CAYMAN SW band `0xb700..0xc120` vs
> MARIANA TIE band `0xbc00..0xc260`) yields only **generic-multiply** differences
> (`ivp_muln_2xf32t`, `ivp_muluuqn16xr16` "extra" in the TIE band); the two bands share
> the bulk vocabulary, and the mid-bundle ivp mnemonics are FLIX-desync artifacts. This
> diff is therefore **scheduling noise, not a bespoke RNG op** — no RNG-specific mnemonic
> appears in either band. `[HIGH no-RNG-op / MED the diff itself]`

---

## 6. The ISA / value-oracle boundary — no RNG TIE op exists

This is the decisive negative, proven two independent ways.

### 6.1 ISA decode tables (`libisa-core.so`, `ncore2gp` config)

`nm libisa-core.so | rg -i 'xorwow|lfsr|prng'` returns **zero**. There is **no** opcode
named rng/xorwow/lfsr/prng and **no** rng/seed/xorwow state-register or user-register.
The only `rand`-stemmed symbols are the **reduce-AND-bool** predicate-reduction family:

```
Iclass_IVP_RANDBN_args      Iclass_IVP_RANDBN_2_args      Iclass_IVP_RANDBN_stateArgs
Iclass_IVP_RANDB2N_args     Iclass_IVP_RANDB2N_stateArgs
Iclass_IVP_RORBN_args       Iclass_IVP_RORBN_2_args       (reduce-OR-bool siblings)
Opcode_ivp_randb2n_Slot_f0_s1_ld_encode   ... (per-FLIX-slot encode functions)
```

> **NOTE — `ivp_randbn` is REDUCE-**AND**-BOOL, not random.** It sits in the `r*`
> reduction family next to `ivp_radd*` (reduce-add), `ivp_rmax*`/`rmin*` (reduce-max/min),
> and crucially `ivp_rorbn` (reduce-**OR**-bool): `rand` = reduce-AND-bool, `rorb` =
> reduce-OR-bool. Its iclass is `Iclass_IVP_RANDBN_args`. It operates on a **64-bit vbool
> predicate register**, not 32-bit data lanes, and has nothing to do with RNG. A
> reimplementer searching for "the rng op" by the `rand` stem will find this and must
> *not* mistake it. `[HIGH/OBSERVED]`

> **NOTE — the ISA mnemonic total.** The nm-grounded distinct-opcode count is roughly
> **1517** (`Opcode_*_args`) / **1432** iclasses; an earlier synthesis cited 1534. The
> exact roster size is immaterial to this page — what matters is that **none** of the
> opcodes is an RNG primitive — but the discrepancy is flagged as unresolved.
> `[HIGH zero-RNG-opcode; count unresolved]`

### 6.2 The value oracle (`libfiss-base.so`)

`nm libfiss-base.so | rg -i 'xdref.*(xorwow|lfsr|prng|weyl|rng_)'` returns **zero**: no
RNG datapath primitive is in the value contract. The `randbn` family bodies are the bool
bit-reductions, at these addresses:

| symbol | addr | semantics |
|---|---|---|
| `module__xdref_randbn_64_64` | `0x81cc90` | `shr`/`and`/`or` bit-reduce over a 64-bit vbool register |
| `module__xdref_randb2n_64_64` | `0x81cc40` | all-set test (`cmpl $0xffffffff`) |
| `module__xdref_rorbn_64_64` | `0x81ce30` | the OR-reduction sibling |
| `module__xdref_rorb2n_64_64` | `0x81cc70` | the OR-2 reduction sibling |

> **CORRECTION (vs an earlier synthesis) — the OR-bool reduction body is at `0x81ce30`,
> not `0x81cc70`.** Address `0x81cc70` actually holds `module__xdref_rorb2n_64_64` (the
> `rorb2n` sibling); the true `module__xdref_rorbn_64_64` is at `0x81ce30`. Neither is an
> RNG primitive.

> **NOTE — the `xdref` symbol count.** `nm | rg -c 'xdref'` reports **866** symbols
> bearing the `xdref` stem (an earlier synthesis cited 864). Either way the count is
> irrelevant to the finding: **none** of them is an RNG primitive.

> **WALL — the libtie state-descriptor question is *moot* for RNG.** A future libtie /
> ISS extraction can only describe a *state descriptor* for an opcode that exists. With no
> RNG opcode in `libisa-core.so` and no RNG datapath in `libfiss-base.so`, there is **no
> RNG hardware state for a libtie descriptor to describe**. If a later extraction surfaces
> a vendor "XorwowRng TIE" descriptor, it would describe this *generic vector-ALU
> composition*, not a new opcode — cite this boundary.
> `[HIGH/OBSERVED; closable-with-license]`

---

## 7. State model: DRAM scratch, no hardware RNG state register

The TIE path keeps the **5×u32 xorshift words + the Weyl counter `d`** in the per-lane
**DRAM-scratch** state buffer — the *same* struct as the SW path
([rng-seed-state-ops.md](rng-seed-state-ops.md) and
[rng-xorwow-sw.md](rng-xorwow-sw.md) §4). It does **not** use a hardware RNG state
register. Three independent witnesses:

1. **The TIE Init log string is literally `"Initializing XORWOW state in DRAM scratch"`**
   — same phrasing as the SW path. The state is in DRAM, not a state register.
   `[HIGH/OBSERVED]`
2. **No RNG state register exists in the ISA** (§6). The only UR/SR ops anywhere near the
   MARIANA POOL RNG band are the standard `wur.fsr` / `wur.fcr` (FP status/control), far
   from the RNG band.
3. **The Set/Get checkpoint interface reads/writes the 6-word state from/to the
   DRAM-scratch operand** exactly as the SW path does (§3, the shared `0xb6dc`/`0xb9ec`
   bodies). If hardware held the state there would be a state-register save/restore op
   instead — there is none.

**Conclusion:** only the **mix** (the per-draw xorshift+Weyl compute) is "offloaded", and
even that mix runs through the **generic `TensorTensorArith`** vector engine, not a
dedicated RNG datapath. So `(TIE)` here is a build/generation variant of the *same*
software composition — possibly with a different vector-ALU schedule/mode on the newer
silicon (the `0x8362` config setup of §3), but **not** a hardware state machine. The
exact acceleration the build enables is inside the FLIX-desync'd driver setup and is not
byte-pinned. `[HIGH/OBSERVED state; MED schedule]`

> **GOTCHA — the exact TIE state-buffer byte size is MED here.** On the SW path a clean
> `movi a12,0x180 ; call8 <memset>` pinned the state buffer at **384 bytes** (= 6 words ×
> 16 lanes × 4 B). In the TIE Init/SetSeeds bodies that `0x180` operand appears only
> *inside* desync'd ivp bundles, not as a clean `movi`, so the exact TIE byte-size is MED.
> The **6-word model is HIGH-inherited** (the get/set bodies show the 6 unrolled per-word
> writes); the larger `0x400` Init frame is working space, **not** state. Treat 384 B as
> the inherited expectation, not a fresh TIE read.
> `[MED byte-size; HIGH 6-word model]`

---

## 8. The TIE-arm selection (Xorwow vs LFSR)

The SEQ front-end is **unchanged** across CAYMAN / MARIANA / MARIANA_PLUS: the NX_POOL
sequencer still carries `"S: Rng (XORWOW)"` (the decode/log/route front-end) and
`"S: Rand{Get,Set}State : rand_algorithm(0x%x) not currently supported on POOL"` (the
unsupported-algo error arm), at container offsets `0x1d0960`
(`Rng (XORWOW)`), `0x1cea91`/`0x1cead9` (CAYMAN not-supported arms), `0x468401`/`0x468449`
(MARIANA), `0x732881`/`0x7328c9` (MARIANA_PLUS), etc. The SEQ-side `(XORWOW)` label has
**no** SW/TIE qualifier — the SW/TIE distinction is purely on the Q7 compute-core
`'P%i:'` stream, where the **build** selects the variant. `[HIGH/OBSERVED]`

**The Q7-side algo fork** (the TIE arm pinned here; the full dispatch tree is owned by
[rng-lfsr-dispatch.md](rng-lfsr-dispatch.md)):

```c
/* MARIANA shared SetSeeds body 0xb6dc — the rand_algo fork.  entry a1,48 */
void rand_set_seeds(args_t *a) {
    uint8_t algo = a->byte12;            /* l8ui a2,[a1+12]                       */
    if (algo & 1)                        /* bbci a2,0,0xb6f5  (skip-if-bit0-clear) */
        log("XorwowSetSeeds(TIE)");      /* 0xb75a -> the Xorwow arm               */
    else
        log("LfsrSetSeeds");             /* 0xb716 -> the LFSR arm                 */
    /* ... 6 unrolled FLIX state-word writes @0xb769.. */
}
/* RandSetState dispatcher 0xb640: l8ui [a1+36] ; extui ...,0,1 -> drives the
 * algo-select arg into 0xb6dc. */
```

So `rand_algo` **bit0** selects Xorwow vs LFSR on MARIANA+ (vs CAYMAN, where bit0=Xorwow
was the *only* wired arm).

> **GOTCHA — the exact Lfsr-vs-Xorwow bit polarity is MED.** Both log calls are reached
> under the FLIX-desync'd selector, so *which* bit0 value selects *which* algorithm is
> MED. [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md) owns the full decode.
> `[HIGH fork; MED polarity]`

---

## 9. Output dtype contract and the Dropout consumer

### 9.1 `uint32` output, byte-identical float seam

The TIE Xorwow draw is a **`uint32` per lane** (`out = v + d`, both 32-bit) — the 32-bit
state words + the 32-bit Weyl add are inherited from the SW path unchanged.
`[HIGH/OBSERVED]`

The `0x3F800000` (1.0f) float seam is **present and byte-identical** to CAYMAN:

| image | addr | bytes | decode |
|---|---|---|---|
| MARIANA TIE | `0xbf2d` / `0xbf30` | `24803f` / `24803f` | `const16 a2,0x3f80` ×2 |
| CAYMAN SW | `0xb88d` / `0xb890` | `24803f` / `24803f` | `const16 a2,0x3f80` ×2 |

`0x3f80` is the high half of `0x3F800000`, the IEEE-754 `1.0f` exponent/leading bits —
the same optional `uint32 → float[0,1)` cast seam. `[HIGH constant; MED mantissa cast]`

### 9.2 The Dropout consumer relationship

The DVE **Dropout** kernel ([dropout.md](dropout.md)) is a **pure consumer** of this
RNG: its handler body carries **no** Xorwow state, **no** seed, **no** Weyl counter, and
makes **no** cross-engine call to the POOL RNG (proven by exhaustive xref scan in the
Dropout decode). The per-element random values arrive as a **precomputed `uint32`
random-tensor operand** — the POOL Xorwow RNG (SW on CAYMAN, "TIE"-build on MARIANA+)
stages a `uint32` random tensor; the runtime hands it to DVE Dropout as an operand.

On the DVE side, the bridge that turns the staged `uint32` draw into the float the
threshold compare uses is **`ivp_ufloatn_2x32t`** (uint32 → fp32, predicated) — this is
the convert op that realizes the `uint32 → float[0,1)` seam that the `0x3F800000`
constant above points at. Dropout then does `random < p → vbool predicate`
(`ivp_oltn_2xf32t`), mask-select (`ivp_dsel*/sel*..t`), and survivor scale
(`ivp_muln_2xf32t`).

> **NOTE — the consumer contract is generation-invariant.** The SW-vs-TIE *producer*
> choice does **not** change the `uint32` output ABI. Whether CAYMAN's SW kernel or
> MARIANA+'s "TIE"-build kernel stages the random tensor, the Dropout consumer sees the
> same `uint32` contract and applies the same `ivp_ufloatn_2x32t` cast. The producer
> build is invisible to the consumer. `[HIGH/OBSERVED invariance; INFERRED path]`

---

## 10. Reconciliation summary (SW vs TIE vs consumer)

| property | CAYMAN SW ([sw](rng-xorwow-sw.md)) | MARIANA+ "TIE" (this page) | verdict |
|---|---|---|---|
| algorithm | Marsaglia Xorwow | Marsaglia Xorwow | IDENTICAL |
| default seeds x..v | `cd15`/`55e5`/`3bb5`/`../3f19` | `cd15`/`55e5`/`3bb5`/`../3f19` | IDENTICAL |
| Weyl increment | `362437=0x587C5` (`24c587`) | `362437=0x587C5` (`24c587`) | BYTE-IDENTICAL |
| state location | DRAM scratch, 6-word/lane | DRAM scratch, 6-word/lane | IDENTICAL |
| HW state register | none | none | none |
| mix engine | generic `TensorTensorArith` | generic `TensorTensorArith` | IDENTICAL |
| dedicated RNG opcode | NONE | NONE | NONE |
| RNG fiss primitive | NONE | NONE | NONE |
| output dtype | `uint32` (`+0x3f80` seam) | `uint32` (`+0x3f80` seam) | IDENTICAL |
| second algo (LFSR) | absent | present (`Lfsr Set/GetSeeds`) | TIE-NEW |
| Init frame | `entry a1,0x200` | `entry a1,0x400` | TIE LARGER |
| driver extra setup | — | `0x9f40`/`0x9f90`, `const16 0x8362` ×2 | TIE-NEW `[MED — desync]` |
| consumer (Dropout) | reads `uint32` rand tensor → `ivp_ufloatn_2x32t` | SAME `uint32` contract | INVARIANT |
| SW/TIE select | n/a (SW only) | GENERATION/BUILD object | compile-time |

**The bottom line:** `(TIE)` is a generation/build variant **label** on the *same*
software Xorwow algorithm — same seeds, same Weyl, same DRAM state, same generic vector
engine, same `uint32` output. It is **not** a dedicated RNG TIE instruction (none exists
in the ISA), **not** a hardware RNG state register (none exists), and **not** a different
output ABI. The newer generations additionally wire a second algorithm (LFSR) behind the
`rand_algo` bit, and the TIE-build driver does extra setup (the `0x8362` config /
`0x9f40` calls) — the only MED-confidence candidate for a real newer-silicon hardware
hook, inside the FLIX-desync'd span.

---

## 11. Honesty ledger

**HIGH / OBSERVED** (direct disasm, byte read, symtab read, or arithmetic identity):

- Container sha256 `b7c67e89…` / 10,276,288 B; MARIANA Q7_POOL DEBUG IRAM carve sha
  `47f76629…`; CAYMAN Q7_POOL DEBUG IRAM carve sha
  `513a8a22…` (A/B reference); SEQ-side NX_POOL anchors `8e4412b9…` / `7bdf6ed7…`.
- The RNG-string sweep placing every `(SW)`/`(TIE)`/`Lfsr` string in its generation
  container by file offset; per-gen presence (SUNDA none / CAYMAN SW-only / MARIANA+
  TIE+LFSR).
- The MARIANA TIE function map (Init `0x7588`, RandSetState `0xb5f4`, shared SetSeeds
  `0xb6dc` forking Lfsr/Xorwow(TIE), RandGetState `0xb930`, Rng dispatch `0xbf74`,
  XorwowRng(TIE) driver `0xbc78`) — every entry/log/`call8` edge read.
- The five canonical Marsaglia seeds from the TIE Init const16 immediates, IDENTICAL to
  the CAYMAN SW seeds.
- The Weyl `362437=0x000587C5` `movi+const16` byte-IDENTICAL (`24c587`) in both the
  CAYMAN SW driver (`0xba5f`) and the MARIANA TIE driver (`0xbe0a`).
- The `0x3F800000` (1.0f) float seam byte-IDENTICAL (`24803f`) in both (CAYMAN `0xb88d`,
  MARIANA `0xbf2d`).
- The TIE mix entering the generic `TensorTensorArith` engine (DRAM `0x2092`), same engine
  as the SW path.
- NO rng/xorwow/lfsr opcode and NO RNG state register in `libisa-core.so`; the
  `ivp_randbn`/`randb2n` family is REDUCE-AND-BOOL (iclass `Iclass_IVP_RANDBN_args`).
- NO xorwow/lfsr/prng/weyl primitive in `libfiss-base.so`; the `randbn` bodies are bool
  bit-reductions at `0x81cc90`/`0x81cc40`, `rorbn` at `0x81ce30`.
- The SEQ front-end (`Rng (XORWOW)` + `not currently supported on POOL`) present and
  identical across CAYMAN/MARIANA/MARIANA_PLUS NX_POOL.
- TIE Init frame `entry a1,0x400` vs SW `entry a1,0x200`.

**MED / INFERRED:**

- The `d` (Weyl) init constant (`const16 a6,0xc924`): high half desync-hidden, `a6`
  distinct reg — exact value MED (role HIGH). (CAYMAN's `0xc934` is MED for the same
  reason.)
- The exact TIE state-buffer byte size: the clean `0x180`=384-byte memset that pinned the
  SW size is obscured by FLIX desync in the TIE bodies; the 6-word model is inherited
  (HIGH), the exact TIE byte-count MED.
- The "HW offload" locus: the TIE driver's extra setup (`0x9f40`/`0x9f90`, `const16
  0x8362` ×2, the break-rendered slot-0 bytes) is the most likely place a newer-silicon
  HW path is configured, but it is inside a FLIX-desync'd span and not byte-pinned.
- The Xorwow-vs-LFSR `rand_algo` bit polarity: the `bbci`/`extui` fork is OBSERVED; the
  exact polarity is MED ([rng-lfsr-dispatch.md](rng-lfsr-dispatch.md) owns the full tree).
- The `uint32 → float[0,1)` exact mantissa-fill cast (the `0x3f80` constant is HIGH; the
  cast sequence is MED through the desync).
- **All MAVERICK (v5) interior claims** about the TIE body: its DEBUG IRAM symbol is
  nm-aliased onto the DEBUG DRAM address (`0x962860`), so the IRAM body was not separately
  disassembled. MAVERICK TIE+LFSR *presence* is HIGH by string; the *body* is INFERRED
  identical-family from the byte-identical DRAM string layout. `[INFERRED — every v5
  interior]`

**LOW / UNRECOVERED:**

- The exact per-step ivp vector mnemonics of the TIE xorshift mix (FLIX desync, no
  `.xt.prop` table in the flat DEBUG Q7 IRAM — the carve-substrate limitation; the SW path
  had the same wall).
- The LFSR algorithm itself (taps/width/seed) — a separate second algo, owned by
  [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md), out of this page's Xorwow-TIE scope.
- Whether the "TIE" build actually executes the mix on a different silicon datapath vs the
  SW build at runtime — **not** statically decidable from the firmware image alone; it
  would manifest only in the silicon's IVP-unit microarchitecture.

---

## 12. Reproduction

```sh
export XTENSA_SYSTEM=.../gpsimd_tools_tgz/tools/XtensaTools/config
export XTENSA_CORE=ncore2gp
F=.../aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/.../libnrtucode_internal.so
ISA=.../gpsimd_tools_tgz/tools/ncore2gp/config/libisa-core.so
FISS=.../gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so

# RNG string sweep (the SW/TIE boundary):
xtensa-elf-strings -t x "$F" | rg -i 'xorwow|lfsr|\(TIE\)|\(SW\)'
#   -> XorwowRng(TIE) x5 @ 0x5030e6/0x55099a/0x7ceda6/0x81c81a/0x9646c6
#      XorwowRng(SW)  x2 @ 0x26999e/0x2b6f32 ; Lfsr{Set,Get}Seeds x10

# carve + disasm the MARIANA *Q7_POOL* DEBUG IRAM (not NX_POOL):
dd if="$F" bs=1 skip=$((0x4e2440)) count=$((0x1ed40)) of=MAR_IRAM.bin   # sha 47f76629..
xtensa-elf-objdump -D -b binary -m xtensa --adjust-vma=0x0 MAR_IRAM.bin > MAR_IRAM.dis

# the byte-identical seams (re-grep both images):
rg '24c587' MAR_IRAM.dis   # -> 0xbe0a  movi.n a2,5 ; const16 a2,0x87c5  (Weyl 362437)
rg '24803f' MAR_IRAM.dis   # -> 0xbf2d/0xbf30  const16 a2,0x3f80         (1.0f seam)
rg 'const16 a10, 0x18ae' MAR_IRAM.dis   # -> 0x759e in fn 0x7588 (entry a1,0x400)

# ISA / fiss: prove NO RNG op / NO RNG primitive:
nm "$ISA"  | rg -i 'xorwow|lfsr|prng'                 # -> (empty)
nm "$ISA"  | rg -i 'randbn|rorbn'                      # -> ivp_randbn = REDUCE-AND-BOOL
nm "$FISS" | rg -i 'xdref.*(xorwow|lfsr|prng|weyl)'    # -> (empty)
nm "$FISS" | rg -i 'xdref_(randbn|rorbn)'              # -> bool bit-reduce @0x81cc90 / 0x81ce30
```

---

## See also

- [RNG — Xorwow Software Path](rng-xorwow-sw.md) — the CAYMAN SW reference path this page
  reconciles against (seeds, Weyl, 6-word state, the 6-step ALU mix).
- [RNG — LFSR + rand_algo Dispatch Tree](rng-lfsr-dispatch.md) — owns the full
  Xorwow-vs-LFSR selector decode and the LFSR second algorithm.
- [RNG Seed-State Opcodes (0x77/0x78)](rng-seed-state-ops.md) — the
  RandGetState/RandSetState checkpoint interface this page's Set/Get bodies implement.
- [Dropout](dropout.md) — the DVE consumer that reads the staged `uint32` random tensor
  and casts it via `ivp_ufloatn_2x32t`.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the normative
  definition of the `[HIGH/MED/LOW] × [OBSERVED/INFERRED/CARRIED]` tags used throughout.
