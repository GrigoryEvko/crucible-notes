# VAL — fp Transcendental Seed/Refine Family

This is the Part-15 differential-validation instance for **Engine A** — the in-core
transcendental **seed / first-iterate** LUT (the *RECIP0 path*). It applies the
[4-oracle bit-exact method](four-oracle-method.md) to the Newton/QLI **0ᵗʰ iterates**
`recip0` / `rsqrt0` / `sqrt0` / `div0` / `nexp0` / `nexp01` plus the `mksadj` / `mkdadj`
mantissa-adjust adjuncts, **both fp16 and fp32**, and to the single-pass QLI reciprocal
refine `recipqli`. It is the validation companion to the per-instruction ISA references
[B14 fp16 `hp_lookup`](../isa/ref/b14-hp-lookup.md) and
[B15 fp32 `sp_lookup`](../isa/ref/b15-sp-lookup.md), and to the table-hardware view
[Activation + Transcendental Tables](../uarch/activation-transcendental-tables.md).

The headline this page delivers is a **wall NARROWING**. The [FW-42 wall](#7-the-fw-42-wall-and-its-narrowing)
("the seed coefficient bytes are not recoverable") was stated against the *literal source
coefficients* of the seed ROM. This pass NARROWS it: both seed tables are now
**PROVEN-BY-EXECUTION** — the shipped `.rodata` table is the validated truth, driven LIVE
out of `libfiss-base.so` and reproduced bit-exactly across every one of the 128 buckets at
**both** widths. What remains CARRIED is no longer "the whole table" — it is exactly **two
half-ULP boundary entries** where the recovered closed-form rounds one ULP away from the
`.rodata` byte, plus the literal coefficient lineage. The `.rodata` table is OBSERVED /
validated; the closed-form is INFERRED, and at those two boundaries the table wins.

> **NOTE — datapath-axis disclaimer (inherited from the method page).** Every `16f/32f`,
> `0x7c00`, `bucket`, `binade`, `N0/slot3` token here is a **datapath-width / FLIX-format /
> ISA-lane** axis of the single Cairo config (`Xm_ncore2gp`, Xtensa24, NX1.1.4). None of it
> is a silicon-generation fact; the five firmware gens are not visible in `libfiss-base.so`,
> its `.rodata`, or the seed leaves.

---

## 0. Headline findings

| # | Finding | Tag |
|---|---|---|
| 1 | **The seed tables are proven-by-execution.** `recip0`/`rsqrt0` reproduce `RECIP_Data8`/`RSQRT_Data8` bit-for-bit over all 128 buckets, **fp16 AND fp32**, driven LIVE via `ctypes`. **0 mismatches.** | `[HIGH/OBSERVED·exec]` |
| 2 | **RECIP closed-form `round(256/(1+(i+0.5)/128))` matches the `.rodata` table 127/128.** The **one** divergence is `i=127` (table `0x81`=129, closed-form `128`) — a half-ULP boundary; **the table is truth.** | `[HIGH/OBSERVED table; INFERRED form; CARRIED@i=127]` |
| 3 | **RSQRT is a parity-ordered two-range table; the two-range closed-form matches 127/128.** The **one** divergence is the `[2,4)` half at `idx=13` (table `0xa5`=165, form `164`, exact float `164.499`) — the second half-ULP boundary. | `[HIGH/OBSERVED table; INFERRED form; CARRIED@hi13]` |
| 4 | **2 half-ULP mispredictions total**, both at near-tie fractions; the `.rodata` table is the validated arbiter at both. | `[HIGH/OBSERVED]` |
| 5 | **All eight first-iterate leaves + `mksadj`/`mkdadj` are bare-leaf drivable LIVE** (fp16 + fp32). Special-value algebra (`1/±0=±Inf`+DivByZero, `1/inf=+0`, `x<0→Invalid`/qNaN) reproduced. | `[HIGH/OBSERVED·exec]` |
| 6 | **`recipqli` is the lone wall** — `call *0x38(%rax)` soft-float dispatch from `xstate`; SIGSEGV on NULL/zeroed, fork-isolation-proven this pass. Its value is reachable only via the heavy leg. | `[HIGH/OBSERVED·exec]` |
| 7 | **Seed accuracy ≈ 7.0 / 7.5 bits, refine doubles per Newton step (8→16→32).** Live 50k-sample sweep + a live Newton-convergence demo on the seed. | `[HIGH/OBSERVED·exec]` |

All addresses re-read with `nm`/`objdump`/`readelf` this pass; all value facts driven LIVE
in-process against `libfiss-base.so`; all table bytes read straight from `.rodata`
(`VMA == file-offset`). Provenance: lawful interoperability RE of shipped artifacts.

---

## 1. Scope and the leaf roster (nm-grounded)

The seed slice is the set of `module__xdref_*` value leaves that compute a transcendental
**first iterate** — a `~7`-bit table-indexed approximation that a downstream Newton (recip /
rsqrt / sqrt) or QLI / `divn` (div) refine consumes. Enumerated this pass with
`nm -D --defined-only <abs>/libfiss-base.so | rg 'module__xdref_(recip\|rsqrt\|sqrt\|div\|nexp\|mksadj\|mkdadj)'`:

| leaf | VMA | width | nargs · outs | role |
|---|---|---|---|---|
| `module__xdref_recip0_1_1_16f_16f` | `0x520110` | fp16 | unary · 3-out (seed in `*o2`) | `1/x` seed |
| `module__xdref_rsqrt0_1_1_16f_16f` | `0x520310` | fp16 | unary · 3-out (seed in `*o2`) | `1/√x` seed |
| `module__xdref_sqrt0_16f_16f` | `0x520060` | fp16 | unary · 1-out (`*o0`) | `√x` seed |
| `module__xdref_div0_16f_16f` | `0x51fff0` | fp16 | unary · 1-out (`*o0`) | `1/d` denom seed |
| `module__xdref_nexp0_16f_16f` | `0x521850` | fp16 | unary · 1-out (`*o0`) | `2^x` octave seed (plain) |
| `module__xdref_nexp01_16f_16f` | `0x521790` | fp16 | unary · 1-out (`*o0`) | `2^x` 0/1-octave split |
| `module__xdref_mksadj_1_16f_16f` | `0x521bd0` | fp16 | unary · 2-out (`*o0`,`*o1`) | single-precision adjust |
| `module__xdref_mkdadj_1_1_16f_16f_16f` | `0x521940` | fp16 | **binary** · 3-out (`*o0..2`) | double-precision adjust |
| `module__xdref_recip0_1_1_32f_32f` | `0x8785f0` | fp32 | unary · 3-out (seed in `*o2`) | `1/x` seed |
| `module__xdref_rsqrt0_1_1_32f_32f` | `0x878900` | fp32 | unary · 3-out (seed in `*o2`) | `1/√x` seed |
| `module__xdref_sqrt0_32f_32f` | `0x878490` | fp32 | unary · 1-out (`*o0`) | `√x` seed |
| `module__xdref_div0_32f_32f` | `0x878340` | fp32 | unary · 1-out (`*o0`) | `1/d` denom seed |
| `module__xdref_nexp01_32f_32f` | `0x87a4d0` | fp32 | unary · 1-out (`*o0`) | `2^x` 0/1-octave split |
| `module__xdref_mksadj_1_32f_32f` | `0x87a8b0` | fp32 | unary · 2-out | single-precision adjust |
| `module__xdref_mkdadj_1_1_32f_32f_32f` | `0x87a620` | fp32 | binary · 3-out | double-precision adjust |
| `module__xdref_recipqli_1_1_1_1_1_32f_32f` | `0x87df20` | fp32 | refine — **WALLED** (§7) | single-pass QLI reciprocal refine |

> **QUIRK — fp32 ships NO `nexp0`, only `nexp01`.** The fp16 roster carries **both**
> `nexp0_16f_16f` (plain octave seed) and `nexp01_16f_16f` (the 0/1 split); the fp32 roster
> has **only** `nexp01_32f_32f` (`nm` finds no `module__xdref_nexp0_32f_32f`). A reimplementer
> mirroring the fp16 roster into fp32 invents a non-existent `nexp0n_2xf32` opcode — the same
> negative control [B15 §1](../isa/ref/b15-sp-lookup.md) reports from the ISA side.
> `[HIGH/OBSERVED]`

> **GOTCHA — these are SEEDs, not the function.** `recip0(3.0)` returns the perturbed
> `0x3eaa0000 = 0.33203…`, not `1/3 = 0.33333…`; `sqrt0(2.0)` returns `0x3f340000 = 0.703125`,
> not `1.41421…`. The `0` in each mnemonic is the iteration index — they are the **0ᵗʰ
> approximation**, ~1 part in 128 off **every** input (including powers of two:
> `recip0(2.0)=0x3eff0000=0.498047`, not `0.5`). Treating a seed as the final value is the
> single most common mis-use. `[HIGH/OBSERVED·exec]`

---

## 2. The ABI — per-leaf, confirmed by the store register

The seed leaves do not return a value; they **store through out-pointers** and return `void`,
exactly the convention the [method page §2.2](four-oracle-method.md#22-the-abi--verified-per-leaf-not-assumed)
describes. But the seed family has a **richer** out-pointer convention than the int ALU: the
flag/status words mean the out-pointer count is **not** simply `nargs+1`. The shape is keyed
by the leaf-name flag-prefix (`_1` / `_1_1`) **and confirmed by the store register in the
disassembly** — never assumed.

```asm
; module__xdref_div0_16f_16f  @0x51fff0   (UNARY, 1-out)
  …
  mov  %esi,(%rdx)          ; STORE through %rdx = *o0   (arg-3)
  ret

; module__xdref_mksadj_1_16f_16f  @0x521bd0   (UNARY, 2-out)
  …
  mov  %edi,(%rdx)          ; *o0 (arg-3)  — status/adjust flag
  mov  %eax,(%rcx)          ; *o1 (arg-4)  — adjusted value
  ret

; module__xdref_mkdadj_1_1_16f_16f_16f  @0x521940   (BINARY, 3-out)
  mov  %edx,%r14d           ; arg-3 (%rdx) = B, cracked as fp16 (shr $0xa / and $0x3ff)
  …                          ; arg-2 (%rsi) = A
  mov  %r13d,(%rcx)         ; *o0 (arg-4)
  mov  %ebx ,(%r8)          ; *o1 (arg-5)
  mov  %eax ,(%r9)          ; *o2 (arg-6)  — the adjusted mantissa lands here
  ret
```

The resulting ctypes shapes (all bound + driven LIVE this pass):

| leaf class | C signature (out-pointers from the store regs) |
|---|---|
| `recip0`/`rsqrt0` `_1_1_*` | `void f(int lane, uint x, int *o0, int *o1, int *seed)` — **seed in `*o2`** (`%rcx`) |
| `sqrt0`/`div0`/`nexp0`/`nexp01` `_*f_*f` | `void f(int lane, uint x, int *o0)` — **seed in `*o0`** (`%rdx`) |
| `mksadj` `_1_*f_*f` | `void f(int lane, uint x, int *o0, int *o1)` — value in `*o1` (`%rcx`) |
| `mkdadj` `_1_1_*f_*f_*f` | `void f(int lane, uint A, uint B, int *o0, int *o1, int *o2)` — value in `*o2` (`%r9`) |

> **CORRECTION (the single most important ABI fact on this page).** The seed family's
> out-pointer position is **NOT** uniform and the flag-prefix **undercounts** the outputs.
> `recip0_1_1_16f_16f` carries `_1_1` (two flags) yet writes **three** pointers — the seed
> lands in the **third** (`*o2`, `%rcx`), with two status words ahead of it; whereas
> `sqrt0_16f_16f` (no flag prefix) writes **one** pointer and the seed lands in the **first**
> (`*o0`, `%rdx`). And `mkdadj_1_1_16f_16f_16f` carries `_1_1` (two flags) but writes
> **three** pointers (`%rcx`,`%r8`,`%r9`) — binding it with only two out-pointers leaves the
> `%r9` store target uninitialised and SIGSEGVs (observed: shapes A/B crashed, the 3-out
> shape C succeeded, fork-isolated). **Read the store registers; do not infer the out count
> from the flag prefix.** Confirmed by `recip0(1.0)→0x3bf8` landing in `*o2` and
> `sqrt0(4.0)→0x3bf8` landing in `*o0` (B14 §4.1). `[HIGH/OBSERVED·exec]`

> **GOTCHA — `RTLD_GLOBAL` and a fork-isolated child.** Load with `mode=ctypes.RTLD_GLOBAL`
> (the method-page rule — intra-library tail-calls else fault), and probe any **unconfirmed**
> ABI shape inside `os.fork()` so a wrong out-pointer count costs a child, not the harness.
> The `mkdadj` ABI was resolved this way (two segfaulting shapes, one clean). `[HIGH/OBSERVED]`

---

## 3. The two seed tables — read out of `.rodata`, validated by execution

The seeds are not polynomials; they are **128-entry `.rodata` ROM tables**, 4-byte stride,
8-bit seed in the low byte (upper three bytes zero — the `Data8` name). `.rodata` is
`VMA == file-offset` (§3.1 below), so `xxd -s <VMA>` reads them directly.

| symbol | VMA | entries | indexed by | structure |
|---|---|---|---|---|
| `table__recip_tab` ≡ `table__RECIP_Data8` | `0x9553c0` ≡ `0x958fc0` | 128 × u32 | `recip0`, `div0` | monotone `0xff → 0x81` |
| `table__rsqrt_tab` ≡ `table__RSQRT_Data8` | `0x9551c0` ≡ `0x958dc0` | 128 × u32 | `rsqrt0`, `sqrt0` | **two parity ranges** (not monotone) |

The fp16 leaves `lea` the `*_tab` alias; the fp32 leaves the `*_Data8` alias. A
`struct.unpack` comparison this pass shows `recip_tab == RECIP_Data8` and
`rsqrt_tab == RSQRT_Data8` — **byte-identical**: one ROM each, two symbol names.
`[HIGH/OBSERVED]`

### 3.1 Section layout — `.rodata` has no offset delta `[HIGH/OBSERVED]`

`objdump -h` this pass (the `0x200000` delta is the **writable** sections only — *not* the
seed ROMs):

| section | VMA | file-off | Δ |
|---|---|---|---|
| `.text` | `0x190430` | `0x190430` | 0 |
| `.rodata` | `0x88ff00` | `0x88ff00` | **0** |
| `.data.rel.ro` | `0xc17e80` | `0xa17e80` | `0x200000` |
| `.data` | `0xc8eb68` | `0xa8eb68` | `0x200000` |

> **GOTCHA — subtract nothing for the seed tables.** All seed/QLI ROMs are in `.rodata`
> (`VMA == file-offset`), so `xxd -s 0x958fc0` reads `RECIP_Data8` directly. The `0x200000`
> delta (gpsimd's, not libtpu's `0x400000`) applies only to `.data`/`.data.rel.ro`; applying
> it to a `.rodata` table reads the wrong bytes. Confirm per-section before trusting any
> address. `[HIGH/OBSERVED]`

### 3.2 `RECIP_Data8` — the closed-form, and its one boundary miss

The first / last bytes, read this pass:

```
RECIP_Data8 (0x958fc0):  ff fd fb f9 f7 f5 f4 f2 f0 ee ed eb e9 e8 e6 e4 …
                         … 84 83 83 82 82 81 81 81   (entries 120..127)
```

The recovered closed-form is the **bucket-midpoint reciprocal**:

```c
// RECIP_Data8[i] == round( 256 / (1 + (i + 0.5)/128) ),  i = 0..127     (x in [1,2), midpoint)
//   the 8-bit reciprocal of the bucket centre — the seed mantissa byte, implicit leading 1.
//   RECIP[0]=0xff (1/1.0⁺), RECIP[64]=0xaa (1/1.5), RECIP[127]=0x81 (1/2.0⁻).
```

Verified against the `.rodata` bytes this pass: **127 / 128 exact**. The lone divergence:

```
i = 127:  table = 0x81 (129)    closed-form round(256/1.99609) = round(128.250) = 128    Δ = +1
```

The exact unrounded value is `128.2505`. The naive `round()` gives `128`; the table stores
`129`. This is a **half-ULP boundary misprediction** — the `.rodata` byte is the validated
truth, the closed-form is INFERRED and wrong by one ULP here. `[HIGH/OBSERVED table; CARRIED@127]`

### 3.3 `RSQRT_Data8` — parity-ordered two-range, and its one boundary miss

The rsqrt seed depends on the **parity of the exponent** (`1/√(m·2^e)` lands in a different
unit interval for even vs odd `e`), so the table is **two stacked 64-entry sub-tables** — and
crucially the **odd-exponent (`[2,4)`) half is FIRST**, the even-exponent (`[1,2)`) half
second:

```
RSQRT_Data8 (0x958dc0):  b4 b3 b2 b0 af ae ac ab … 82 81 81 80   (entries 0..63  → x in [2,4))
                         ff fd fb f9 f7 f6 f4 f2 … b8 b7 b6 b5   (entries 64..127 → x in [1,2))
```

The `0x80 → 0xff` jump at index 64 is the **binade boundary**, not a table error. The
two-range closed-form (hi-range first):

```c
// idx 0..63   (odd-exp binade, x in [2,4)):  round( 256 / sqrt( 2·(1 + (j+0.5)/64) ) )   → 0xb4 … 0x80
// idx 64..127 (even-exp binade, x in [1,2)): round( 256 / sqrt(    1 + (j+0.5)/64    ) )   → 0xff … 0xb5
```

Verified against the `.rodata` bytes this pass: **127 / 128 exact**. The lone divergence:

```
idx = 13 (hi/[2,4) half):  table = 0xa5 (165)    closed-form = 164    exact float = 164.4993    Δ = +1
```

`164.4993` is an **almost-exact half-ULP tie** (fraction `0.4993`); the table rounds up to
`165`, naive `round()` lands on `164`. Second boundary, same epistemics: table OBSERVED /
validated, form INFERRED, this entry CARRIED. `[HIGH/OBSERVED table; CARRIED@hi13]`

> **CORRECTION — the RSQRT half-ordering is odd-binade-first; a "lo-first" assignment
> mispredicts ALL 128 entries.** The table's first 64 entries are the *smaller* seeds
> (`0xb4…0x80`, the `[2,4)` / odd-exponent range) and the second 64 are the *larger*
> (`0xff…0xb5`, `[1,2)` / even). Assigning the ranges the other way round (lo-first) yields
> 128/128 "mismatches" with deltas of 70–75 — a total miss, not a table error. The index is
> `((exp & 1) << 6) \| (mant_top6)`, with the parity LSB selecting which 64-entry half. Get
> the ordering wrong and the seed is garbage on every input. `[HIGH/OBSERVED·exec]`

---

## 4. The keystone — the seed leaves driven LIVE, bit-exact both widths

This is reference **(d)** of the method: instead of trusting a python lift of the
disassembly, the harness `dlopen`s the real `libfiss-base.so` and **calls the seed leaves**
on every input, then compares the emitted seed mantissa byte to the `.rodata` table read
independently via `struct.unpack`. Two structurally independent reads — a static byte and a
dynamic execution — must agree on all 128 buckets, at both widths.

```python
import ctypes, struct as S
FISS = ".../ncore2gp/config/libfiss-base.so"
data = open(FISS, "rb").read()
RECIP = S.unpack_from("<128I", data, 0x958fc0)         # table read straight from .rodata
RSQRT = S.unpack_from("<128I", data, 0x958dc0)
lib   = ctypes.CDLL(FISS, mode=ctypes.RTLD_GLOBAL)
PI    = ctypes.POINTER(ctypes.c_int)

def recip0_32(xb):                                      # _1_1_ → seed in *o2 (arg-5)
    f = lib.module__xdref_recip0_1_1_32f_32f; f.restype = None
    f.argtypes = [ctypes.c_int, ctypes.c_int, PI, PI, PI]
    o = [ctypes.c_int(0) for _ in range(3)]
    f(0, xb, *[ctypes.byref(c) for c in o]); return o[2].value & 0xffffffff

# fp32 128-bucket sweep, x in [1,2)  (exp = 127):
mism = sum(1 for i in range(128)
           if ((recip0_32((127 << 23) | (i << 16)) >> 16) & 0xff) != (RECIP[i] & 0x7f))
# -> mism == 0
```

Driven this pass, **the vendor binary computing every value**:

| certificate | result |
|---|---|
| **fp16** `recip0` seed-byte `==` `RECIP[(m>>3)]&0x7f`, all 128 buckets, x∈[1,2) | **0 mismatches** |
| **fp32** `recip0` seed-byte `==` `RECIP[(m>>16)]&0x7f`, all 128 buckets, x∈[1,2) | **0 mismatches** |
| **fp32** `rsqrt0` seed-byte `==` `RSQRT[((e&1)<<6)\|(m>>17)]&0x7f`, odd binade (exp 127), 64 | **0 mismatches** |
| **fp32** `rsqrt0` seed-byte `==` `RSQRT[((e&1)<<6)\|(m>>17)]&0x7f`, even binade (exp 128), 64 | **0 mismatches** |
| `recip_tab == RECIP_Data8`, `rsqrt_tab == RSQRT_Data8` (512-byte `struct` compare) | **identical** |

Spot seeds (LIVE), index ↔ table cross-checked:

```
fp32  recip0(1.0)  = 0x3f7f0000   (0x7f = RECIP[0]&0x7f  ✓)
      recip0(1.25) = 0x3f4c0000   (0x4c = RECIP[32]&0x7f = 0xcc&0x7f  ✓)
      recip0(1.5)  = 0x3f2a0000   (0x2a = RECIP[64]&0x7f = 0xaa&0x7f  ✓)
      recip0(2.0)  = 0x3eff0000   (perturbed 0.5 — seed error on a power of two)
fp16  recip0(1.0)  = 0x3bf8       (lands in *o2)        recip0(1.5) = 0x3950
```

> **GOTCHA — the canonical-binade reproduction is the certificate; the alternate binade
> *re-aligns*.** The leaf reflects the exponent (`0xfd − exp` fp32) and the mantissa
> placement alternates by binade parity. Re-running the 128-bucket extraction at an adjacent
> binade with the *same naive* `(seed>>16)&0xff` shows "128 mismatches" — **not** a wrong
> table, but the exponent-reflected re-alignment. The table value is still the source; the
> reconstruction is binade-dependent. The 0-mismatch certificate is stated for the canonical
> binade (recip x∈[1,2); rsqrt both halves at the matched parity). `[HIGH/OBSERVED·exec]`

### 4.1 Special-value algebra (LIVE)

| input | `recip0` (fp16) | `recip0` (fp32) | flags |
|---|---|---|---|
| `+0` | `0x7c00` (+Inf) | `0x7f800000` (+Inf) | `*o0 = 1` → DivByZero |
| `−0` | `0xfc00` (−Inf) | `0xff800000` (−Inf) | sign threaded |
| `+Inf` | `0x0000` (+0) | `0x00000000` (+0) | — |
| `qNaN` | `0x7ea8` (qNaN, payload mutated) | (qNaN) | — |

`rsqrt0(x<0) → qNaN + Invalid`, `rsqrt0(+0) → +Inf`, `rsqrt0(+Inf) → +0`; denormals are
**not** flushed — a `bsr`-normalize path indexes them before the table read. Matches the
[fp sub-ISA](../isa/ref/b15-sp-lookup.md) FCR/FSR model. `[HIGH/OBSERVED·exec]`

### 4.2 The derived seeds — `div0` / `sqrt0` / `nexp0` / `nexp01` (LIVE)

```
div0(1.0) = 0x3f7f0000   div0(4.0) = 0x3f7f0000   // share mantissa AND parity -> collide
div0(2.0) = 0x3eff0000   div0(3.0) = 0x3eaa0000   // 2.0 differs by exponent parity
sqrt0(1.0)= 0x3f7f0000   sqrt0(4.0)= 0x3f7f0000   sqrt0(2.0)=0x3f340000  sqrt0(9.0)=0x3f2a0000
nexp01(0.0)=0xbf800000 (-1)  nexp01(0.5)=0xc0000000 (-2)  nexp01(2.0)=0xc0000000   // fp32 staircase split
fp16 nexp0(0.5) =0xbc00      fp16 nexp01(0.5)=0xc000                                // nexp0 ≠ nexp01 (plain vs split)
```

`div0(1.0)==div0(4.0)` and `sqrt0(1.0)==sqrt0(4.0)` are the **shared-ROM parity collision**:
both index `RECIP_Data8` / `RSQRT_Data8` with a mantissa-domain + parity-only key, deferring
the full exponent to the `divn` / sqrt-Newton refine. `nexp01` is **table-free** (pure bit
manipulation, the `2^x = 2^n · 2^frac` split); the fp16 `nexp0` vs `nexp01` divergence at
`x=0.5` (`0xbc00` vs `0xc000`) is the live witness that fp16 carries both forms while fp32
carries only the split. `[HIGH/OBSERVED·exec]`

### 4.3 `mksadj` / `mkdadj` — the mantissa-adjust adjuncts (LIVE)

These produce the mantissa-domain operands the divide/sqrt refine consumes. Driven LIVE
(fp16):

```
mksadj(1.0) = [0x0, 0x3cf0]              // unary, 2-out: status in *o0, adjusted value in *o1
mkdadj(3.0, 2.0) = [0x0, 0x0, 0x3cf0]    // binary, 3-out: adjusted mantissa in *o2 (%r9)
mkdadj(1.0, 1.0) = [0x0, 0x0, 0x3cf0]
```

`mkdadj` takes **two** fp16 inputs (numerator-adjust, denominator-adjust) and the adjusted
mantissa lands in the third out-pointer — the ABI the §2 CORRECTION pins. Both are
bare-leaf drivable; `xstate` (arg0/lane) is unused. `[HIGH/OBSERVED·exec]`

---

## 5. Accuracy + seed → Newton-refine convergence (LIVE)

The seed is the **0ᵗʰ iterate**; the validation closes the loop by measuring the seed's bit
budget and demonstrating, **on the LIVE seed**, that a standard Newton step doubles the
correct-bit count — the property the refine ([B17](../isa/ref/b15-sp-lookup.md) FMA /
`divn`) relies on.

50,000-sample live sweep over x∈[1,2) (fixed seed `12569`, byte-reproducible):

```
recip0  max rel err = 0.007810 = 2^-7.00   (7.00 bits)
rsqrt0  max rel err = 0.005460 = 2^-7.52    (7.52 bits)
```

Live Newton-convergence demo, `y0 = recip0(3.0)` then `y' = y·(2 − x·y)` (the reciprocal
Newton step):

```
seed (recip0)   :  8.0 correct bits
after iter 1    : 16.0 bits     (Newton doubles)
after iter 2    : 32.0 bits     ( >= 24  -> fp32-accurate 1/x )
```

The seed reaches `8.0` bits at the bucket centre (worst-case `7.0` over the bucket), and two
Newton iterations clear fp32's 24-bit significand — exactly the 2-iteration budget B15 §6
states. The 8-bit seed-at-centre is the `Data8` (8-bit) table width; the 7-bit worst case is
that minus the bucket truncation. `[HIGH/OBSERVED·exec]`

> **NOTE — recip refines via QLI (one pass), rsqrt via Newton (1–2 passes).** A quadratic
> interpolant **cubes** the error (8→~24 in one step), so the reciprocal path spends a second
> LUT pair (`recipqli`, §7) to buy a single-pass refine; rsqrt/sqrt reuse the free FMA Newton
> iteration (`y·(1.5 − 0.5·x·y²)`), which only doubles per step. The Newton demo above is the
> rsqrt-style doubling; the recip QLI single-pass is the §7 wall. `[HIGH/OBSERVED structure;
> INFERRED iteration math — owned by B17/B23]`

---

## 6. The model-disagreement ledger

For the seed family, the four legs agree completely on every drivable leaf:

| leg | role | seed-family verdict |
|---|---|---|
| **(a) GX-SEM** | bit-precise RTL model of the table-index + exponent-frame | matches LIVE |
| **(a′) FISS-lift** | python lift of the leaf x86 body | matches LIVE byte-for-byte |
| **(c) nki numpy** | reference simulator | **n/a** — the seed ops are not nki int/fp primitives (the seed LUT is a Q7-specific datapath); validated 3-way (a + a′ + d) |
| **(d) libfiss LIVE** | the vendor binary itself | **the arbiter** — 0 mismatches vs the table, both widths |

The **only** residual disagreement is the §3 closed-form vs the `.rodata` table at the **two
half-ULP boundaries** (RECIP `i=127`, RSQRT `hi idx=13`). This is **not** a model-vs-binary
divergence in the §4.2-method sense — the LIVE leaf and the `.rodata` table agree perfectly;
it is the *analyst's recovered closed-form* (an INFERRED reconstruction of how the table was
generated) that misses by one ULP at a near-tie. **The table is the validated arbiter at both
boundaries.** Per the method-page policy, when the inferred model disagrees with the binary,
the binary wins — here the binary's own ROM byte is truth, and the closed-form is flagged
CARRIED at exactly those two entries. `[HIGH/OBSERVED]`

---

## 7. The FW-42 wall and its NARROWING

### 7.1 The narrowing (the headline)

The Open-Questions Register entry **"FW-42 seed coefficient bytes — CARRIED"** previously
held that the seed ROM's source coefficients are not recoverable from this corpus. This pass
**NARROWS** that wall along a precise seam:

* **PROVEN-BY-EXECUTION now (was CARRIED):** the entire seed-table *content* — all 128
  `RECIP_Data8` + 128 `RSQRT_Data8` bytes — is read from `.rodata` AND reproduced bit-exactly
  by the LIVE leaf at **both** widths. The table is **OBSERVED / validated**, not inferred.
* **STILL CARRIED (the narrow residual):** (i) the **literal source-coefficient lineage** —
  the generator program / fixed-point rounding that *produced* the bytes is not in the corpus;
  and (ii) **exactly two** `.rodata` entries — RECIP `i=127` and RSQRT hi-range `idx=13` —
  where the recovered closed-form mispredicts by one ULP at a half-ULP boundary. At those two
  entries the **`.rodata` table is the validated truth and the closed-form is INFERRED.**

So the wall moved from "the whole table is unverifiable lineage" to "the table is validated
truth; only the generator lineage and two boundary roundings are carried." That is a real
narrowing: 254 / 256 bytes are now closed-form-explained *and* execution-validated, and the
remaining 2 are execution-validated against the ROM even though the closed-form misses them.
`[HIGH/OBSERVED·exec — narrowing; CARRIED — lineage + 2 boundary entries]`

### 7.2 The one structural wall — `recipqli`

The single-pass QLI reciprocal refine `recipqli` is the lone leaf that is **not** bare-leaf
drivable. It loads its segment LUTs inline but routes the value-producing fp multiply/FMA
through the ISS soft-float dispatch object. Disassembled this pass at `0x87df20`:

```asm
; module__xdref_recipqli_1_1_1_1_1_32f_32f  @0x87df20
  mov    %esi,%eax
  shr    $0x19,%eax            ; seg = (x >> 25)
  and    $0x3f,%eax            ;       & 0x3f       — 6-bit segment index
  cmp    $0x3f,%eax            ; seg clamped 0..63
  shr    $0x17,%esi            ; mantissa crack …
  and    $0x7fffff,%esi
  mov    %rdi,0x68(%rsp)       ; SAVE xstate (arg0) to the frame
  …
  call   *0x38(%rax)           ; @0x87e518 — %rax derives from the saved xstate; 0x38 = a
  …                            ;             fn-ptr slot in the ISS soft-float dispatch object
  call   *0x38(%rax)           ; @0x87e5e9 — second dispatch (the refine multiply/FMA pair)
```

A bare ctypes drive cannot populate that dispatch table. Probed **fork-isolated** this pass
so the harness survives:

```
xstate = NULL                              -> SIGSEGV (signal 11)
xstate = zeroed 0x4000 (slot 0x38 = NULL)  -> SIGSEGV (signal 11)
```

> **The triage signature of a wall** is `call *<off>(%rax)` where `%rax` derives from the
> saved `xstate` (arg0), **inside** the leaf body. Triage every candidate before binding it:
> `objdump` the leaf and grep for `call *0x..(%rax)`. If present — `recipqli` is the only
> seed-family leaf that has it — schedule the leaf for the **heavy leg** (load the kernel into
> the `libcas-core` / `libsimxtcore` full-ISS model, register an `InstDone` single-step
> callback, read the destination vector register through state introspection), **not** bare
> ctypes. The fp64 `recipqli` sub-stages (`__0`/`__1` @`0x1b07e0`/`0x1b0870`) tail-call the
> same body and inherit the same wall. `[HIGH/OBSERVED·exec]`

> **CORRECTION — `recipqli` is the wall, NOT the seeds.** It is tempting to lump "the
> transcendental family" behind one wall. This pass separates them cleanly: **all eight
> first-iterate seed leaves + `mksadj`/`mkdadj`, fp16 and fp32, are bare-leaf drivable and
> bit-exactly validated against the `.rodata` ROM.** The wall is exactly one leaf — the QLI
> *refine* `recipqli` — because it alone delegates arithmetic to the soft-float dispatch. The
> seed ROM is validated truth; only the refine *interior* (the `{A,gx}` quadratic the dispatch
> evaluates) is carried. `[HIGH/OBSERVED·exec]`

---

## 8. Adversarial self-verification — the five strongest claims, re-challenged

Each headline re-tested against the binary this pass; a claim survives only if a second
independent witness agrees.

1. **The seed leaf reproduces the `.rodata` table bit-exactly, both widths.** *Challenge:*
   could the match be my harness reading the wrong out-pointer, or coincidence? *Re-test:* the
   seed pointer was pinned by the sanity `recip0(1.0)=0x3f7f0000` (≈1.0) **and** by the store
   register in the disassembly (`recip0_1_1` → `*o2`); the full 128-bucket sweep gave 0
   mismatches at **both** fp16 (`m>>3`) and fp32 (`m>>16`); the table was read **independently**
   from `.rodata` via `struct.unpack`, never via the leaf; and `recip_tab == RECIP_Data8`
   (512-byte `struct` compare). Two independent reads (static byte + dynamic execution) agree
   across all 128. **Survives.** `[HIGH/OBSERVED·exec]`

2. **RECIP closed-form matches 127/128; the one miss (i=127) is a half-ULP boundary, table
   is truth.** *Challenge:* maybe my closed-form is simply wrong, or i=127 indicates the form
   is mis-derived? *Re-test:* the form `round(256/(1+(i+0.5)/128))` reproduces 127 entries
   exactly; at i=127 the exact float is `128.2505`, naive round `128`, table `0x81=129`. The
   exact value sits a quarter-ULP above the tie, so it is a genuine rounding-mode boundary, not
   a structural error — and the **table is validated by the LIVE leaf** independently of the
   closed-form. The form explains 127 entries and the binary arbitrates the 128th. **Survives**
   (form INFERRED; i=127 CARRIED; table OBSERVED). `[HIGH/OBSERVED table]`

3. **RSQRT is parity-ordered two-range, odd-binade-first; the one miss (hi idx=13) is a
   half-ULP tie.** *Challenge:* could the two-range structure be an artifact, or the ordering
   reversed? *Re-test:* a "lo-first" range assignment mispredicts **all 128** entries with
   deltas 70–75 (a total miss); the "hi-first" assignment mispredicts **1**. The `0x80→0xff`
   jump at index 64 is the binade boundary the parity LSB selects. At hi idx=13 the exact float
   is `164.4993` (fraction `0.4993` — an almost-exact tie); table `0xa5=165`, form `164`. The
   LIVE `rsqrt0` reproduces the table over **both** halves with 0 mismatches. **Survives** (form
   INFERRED; hi13 CARRIED; table OBSERVED·exec). `[HIGH/OBSERVED table]`

4. **The FW-42 wall NARROWS — table proven-by-execution, only lineage + 2 boundaries
   carried.** *Challenge:* is this a real narrowing or a re-label? *Re-test:* before, the
   table content was CARRIED behind FW-42; now all 256 seed bytes are read from `.rodata`
   **and** reproduced by the LIVE leaf (0 mismatches, both widths) — that is a strict gain of
   ground (254/256 closed-form-explained, 256/256 execution-validated). What stays CARRIED is
   strictly smaller: the generator lineage and 2 boundary roundings. The narrowing is
   *bounded and named*, not asserted. **Survives.** `[HIGH/OBSERVED·exec]`

5. **`recipqli` is the lone wall; the seeds are not.** *Challenge:* could other seed leaves
   also fault, or could `recipqli` be drivable with a different shape? *Re-test:* `objdump` of
   all eight seed leaves + `mksadj`/`mkdadj` shows **no** `call *(%rax)` from xstate — and all
   were driven LIVE without fault (fork-isolated for the unconfirmed `mkdadj` shape). `recipqli`
   alone carries `call *0x38(%rax)` (two sites, `%rax` from the saved `0x68(%rsp)` xstate) and
   SIGSEGVs on both NULL and zeroed xstate, fork-isolation-proven. One leaf walled, fifteen
   drivable. **Survives.** `[HIGH/OBSERVED·exec]`

No claim here rests on a raw dump, an unnamed symbol, or a single uncorroborated witness: the
seed-table reproduction carries a 128-bucket differential-execution certificate at **both**
widths against the shipped `.rodata` bytes, the two boundary misses are pinned to their exact
float values, the wall is execution-proven by a fork-isolated SIGSEGV, and every leaf address
/ count names its `nm` / `objdump` witness.

---

## 9. Confidence ledger

**HIGH / OBSERVED (driven LIVE + `.rodata` read this pass):**

* `recip0`/`rsqrt0` seed byte `==` `{RECIP,RSQRT}_Data8[idx] & 0x7f`, bit-exact over 128
  buckets each, **fp16 and fp32**, 0 mismatches; the index extract (recip top-7 mantissa;
  rsqrt top-6 ++ exp-parity); `recip_tab≡RECIP_Data8` / `rsqrt_tab≡RSQRT_Data8` identity.
* The RECIP monotone `0xff→0x81` and RSQRT parity-ordered two-range (odd-binade first,
  `0x80→0xff` boundary at idx 64) byte content.
* Special-value algebra (`1/±0=±Inf`+DivByZero, `1/inf=+0`, sign threaded, `x<0→Invalid`,
  denormal `bsr`-normalize); the `div0`/`sqrt0` shared-ROM parity collision
  (`div0(1.0)==div0(4.0)`); fp16 `nexp0`≠`nexp01`; fp32 has only `nexp01`.
* The per-leaf out-pointer ABI (seed in `*o2` for `_1_1`, `*o0` for plain; `mkdadj` binary
  3-out via `%rcx`/`%r8`/`%r9`), confirmed by the store registers.
* Seed accuracy 7.00 / 7.52 bits (50k live sweep); the seed→Newton doubling 8→16→32.
* The `recipqli` soft-float-dispatch wall (`call *0x38(%rax)` ×2 from saved xstate; SIGSEGV
  on NULL/zeroed, fork-isolated).

**HIGH / OBSERVED (table) + INFERRED (closed-form):**

* RECIP `round(256/(1+(i+0.5)/128))` (127/128 exact) and RSQRT two-range `256/√(…)`
  (127/128 exact) — the *forms* are INFERRED; the *table bytes* are OBSERVED·exec.

**CARRIED (FW-42 — NARROWED):**

* The literal seed-coefficient **lineage** (the generator that produced the bytes).
* **Exactly two** `.rodata` entries — RECIP `i=127` (`0x81` vs form `128`) and RSQRT hi
  `idx=13` (`0xa5` vs form `164`) — where the closed-form mispredicts at a half-ULP boundary.
  **The `.rodata` table is the validated truth at both.**
* The `recipqli` QLI refine **polynomial interior** (the `{A,gx}` quadratic the soft-float
  dispatch evaluates) — the seg-extract + LUT addresses are OBSERVED, the interior is CARRIED;
  the `recipqli` soft-float seed *value* is the residual reachable only via the heavy leg.

**INFERRED (owned elsewhere):**

* The Newton/QLI refine coefficient math (§5 NOTE) — IEEE-standard form, owned by
  [B17/B23](../isa/ref/b15-sp-lookup.md).

All facts read as derived from shipped-artifact static analysis and license-free in-process
execution of the binary's own value leaves + direct `.rodata` reads (lawful interoperability
RE). No silicon-generation / gen-count / codename is inferred anywhere: every `16f/32f`,
`bucket`, `binade`, `parity-half` token is a datapath-width / FLIX-format / ISA-lane axis of
the single Cairo config.

---

## Cross-references

* [The 4-Oracle Bit-Exact Differential Method](four-oracle-method.md) — the harness, the
  GX-SEM / GX-FLIX / nki / libfiss-LIVE legs, the `RTLD_GLOBAL` + fork-isolation rules, and
  the model-disagreement policy this page applies.
* [ISA Batch 14 — fp16 Transcendental Seeds (`hp_lookup`)](../isa/ref/b14-hp-lookup.md) — the
  per-instruction fp16 view: the roster, the `recip0(1.0)→0x3bf8`-in-`*o2` ABI, the encode
  thunks, the `nexp0`+`nexp01` fp16 pair this page validates.
* [ISA Batch 15 — fp32 Transcendental Seeds (`sp_lookup`)](../isa/ref/b15-sp-lookup.md) — the
  fp32 view: the `recip0`/`rsqrt0`/`sqrt0`/`div0`/`nexp01` `n_2xf32` forms, the 7.0/7.5-bit
  accuracy, the `nexp0n_2xf32` non-existence, the Newton/QLI refine boundary to B17/B23.
* [Activation + Transcendental Table Engine](../uarch/activation-transcendental-tables.md) —
  the table-hardware view: Engine A (this seed LUT) vs Engine B (the host-loaded ACT PWP
  cubic), the QLI refine LUT structure, the §1.5 `recipqli` seg-extract.
* [cas/fiss Convert / Pack / FP Semantics](../iss/cas-convert-pack-fp.md) — the sibling fp
  convert/pack value leaves driven through the same `libfiss-base` oracle, and the fp16↔fp32
  width-convert path the seed inputs ride.

The **Open-Questions Register** ("FW-42 seed coefficient bytes — CARRIED") is the entry this
page narrows; it is referenced by title here (the appendix register is Part 16).
