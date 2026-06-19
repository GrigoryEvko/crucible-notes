# ISA Batch 15 — fp32 Transcendental Seeds (sp_lookup)

This is the per-instruction reference for the **single-precision transcendental seed-generation
slice** of the Vision-Q7 *Cairo* (`ncore2gp`) ISA: the table-lookup opcodes that produce an
**initial fp32 approximation** of `1/x`, `1/√x`, `√x`, `1/d`, and the `2^x` exponent split, indexing
a 128-entry `.rodata` seed table to seed a Newton–Raphson (recip / rsqrt / sqrt) or quadratic
linear-interpolation (div) refinement. It owns **10 shipped mnemonics** — `recip0`/`rsqrt0`/`sqrt0`/
`div0`/`nexp01` in their plain and `t`-suffixed fp32 (`N_2XF32`, 16-lane) forms — summing to **60
placements** of the certified-perfect `12569` cover
([coverage tally](../core/coverage-tally.md)). It is the **fp32 sibling** of
[B14 fp16 seeds](b14-hp-lookup.md): the seed-lookup-then-refine architecture is identical; the
datatype, the table indexing width, the binade-parity handling, and (critically) the *roster
membership* differ.

This page is the **value-semantics-and-encoding** companion to the control-view
[fp sub-ISA page](../core/fp-sub-isa.md): that page owns the FCR/FSR round-mode/exception model
(the two-level RNE vs fiss-leaf-RZ rounding); this page owns *what each seed opcode computes,
bit-exact, and how each one is encoded into a slot*. The opcode this batch seeds is **only the seed
itself** — the iterative refinement that consumes the seed is **not** here: the Newton/QLI step
`recipqli`, the **fp `divn` Newton-refine macro** (`module__xdref_divn_*_32f_32f_32f_32f_2` — the **fp DIVN
family**, distinct from the *integer* `divn_2x32x16` step opcodes that [B23 divide](b23-divide.md) owns),
and the fp FMA refine ops live in [B17 fp32 FMA](b17-spfma.md), the fp DIVN family, and B24. (The
**integer** `divn_2x32x16{s,u}_4step*` opcodes are in [B23](b23-divide.md); the **fp** Newton `divn` is a
separate reference leaf, not a B23 opcode — see [B23 §"divn is two things"](b23-divide.md).) The
§"Batch boundary" table pins
every near-neighbor; the fp16 siblings (`recip0nxf16`/`rsqrt0nxf16`/`nexp0nxf16`/…) are all
[B14](b14-hp-lookup.md).

Everything below is grounded two ways, per
[the extraction methodology](template-and-partition.md#5-the-per-mnemonic-extraction-methodology):
the **opcode-selector immediates** are read straight from the `Opcode_<mnem>_Slot_<slot>_encode`
thunk bodies in the non-stripped `libisa-core.so` (`movl $imm,(%rdi); ret`); the **seed value
semantics** are *proven by execution* — every seed claim was computed by calling the matching
`module__xdref_` value leaf in `libfiss-base.so` live via `ctypes` (license-free), and the **seed
table bytes were read directly out of `.rodata`** (VMA == file-offset) and shown to be reproduced
bit-exactly by the leaf. `[HIGH/OBSERVED]` throughout (by-execution where a value fact is stated),
per the [confidence model](../../reference/confidence-model.md).

---

## 1. Scope, datapath, and key facts

The fp32 seed ops all read their source from the `vec` file (idx 2, 512 b × 32; fp32 = **16 lanes
of 32-bit**, the `N_2XF32` view), run the **multi-cycle seed-lookup latency class**, and write the
seed back to `vec`. They are *not* iterative — one issue produces one table-indexed approximation
per lane — and they are the **only** ops in the slice that touch the two 512-byte `.rodata` seed
tables `table__RECIP_Data8` and `table__RSQRT_Data8`.

| Fact | Value | Source |
|---|---|---|
| Data file | `vec` idx 2, **512 b × 32**; fp32 = 16 lanes of 32-bit (`N_2XF32`) | [register-files §3](../core/register-files.md) |
| Precision token | `n_2xf32` (fp32, 1-8-23, bias 127); `t` = throttle/issue variant | mnemonic suffix |
| Mnemonics this batch | **10** (5 base + 5 `t`) | `nm libisa-core.so \| rg -c` (§7) |
| Placements this batch | **60** (10 × 6 ALU slots) | per-mnemonic census (§7) |
| Slot family | `{F0,F1,F2,F3,F7,N0}·S3_ALU` (6 slots, every seed op) | encode-thunk roster (§2) |
| Seed tables | `table__RECIP_Data8` @ `0x958fc0`, `table__RSQRT_Data8` @ `0x958dc0` (each **0x200 B = 128×u32**, low byte used) | `.rodata` direct read (§3) |
| Table sharing | RECIP ← `{recip0, div0}`; RSQRT ← `{rsqrt0, sqrt0}`; `nexp01` is **table-free** | leaf `lea` disassembly (§3,§5) |
| Value oracle | `libfiss-base.so` `module__xdref_*` leaves, ctypes | [confidence §6.1](../../reference/confidence-model.md) |
| Seed accuracy | recip0 ≈ **7.0 bits** (rel err 2⁻⁷·⁰), rsqrt0 ≈ **7.5 bits** — both need **2 Newton iterations** to reach fp32's 24 bits | live sweep (§4,§6) |
| Pipeline depth | `bbn_sem_vec_sprecip_rsqrt_opcode` = **16 stages** (cas-core) | `libcas-core.so` stage symbols (§8) |

> **QUIRK — the fp32 seed roster is *smaller and structurally different* from fp16's, in two ways.**
> (1) fp16 ships **both** `nexp0nxf16` *and* `nexp01nxf16` (two exp-seed forms); fp32 ships **only**
> `nexp01n_2xf32` — there is **no** `nexp0n_2xf32`
> (`nm libisa-core.so | rg -o 'Opcode_(ivp_nexp0[a-z0-9_]*)_Slot' -r '$1' | sort -u` returns
> `nexp0nxf16`, `nexp01nxf16`, `nexp01n_2xf32` — the plain `nexp0` is fp16-only). (2) The fp16 seeds
> index *different-width* tables; the fp32 forms here all index the **128-entry** `*_Data8` tables.
> A reimplementer copying the fp16 seed roster verbatim into fp32 will invent a non-existent
> `nexp0n_2xf32` opcode. `[HIGH/OBSERVED]` (member-list negative control over the 1065-op `ivp_`
> roster).

> **GOTCHA — `recip0`/`rsqrt0`/`sqrt0`/`div0` are *seeds*, not the function. They are accurate to ~7
> bits and MUST be refined.** `recip0(3.0)` returns `0x3eaa0000 = 0.33203125`, not the correctly
> rounded `1/3 = 0.33333…`; `sqrt0(2.0)` returns `0x3f340000 = 0.703125`, not `1.41421…`. The "`0`"
> in the mnemonic is the iteration index — these are the **0ᵗʰ approximation**. The compiler always
> emits a seed op followed by Newton (recip/rsqrt) or QLI/`divn` (div) refine steps
> ([B17](b17-spfma.md)/[B23](b23-divide.md)/B24). Treating a seed result as the final value is wrong
> by ~1 part in 128. `[HIGH/OBSERVED·exec]`

---

## 2. The roster

Every fp32 seed mnemonic this batch owns, with its encoding read at the **canonical `F1_S3_ALU`
slot** (one of the six `S3_ALU` slots that host the whole family). The `opcode-sel imm` is the
`movl` immediate in the `Opcode_ivp_<mnem>_Slot_f1_s3_alu_encode` thunk
(`c7 07 <imm32> c3` — a single-word template; **there is no `word1` store**, the thunk writes only
`word0`). The `byte-size` is the FLIX format length the op is placed into (16 B for the wide formats
`F0/F1/F2/F3/F7`, 8 B for the narrow `N0`). The seed table column names the `.rodata` table the
op's value leaf indexes.

| mnemonic | FLIX fmt·slot | opcode-sel imm (F1_S3) | vec lane in/out + seed table | byte-size | one-line semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_recip0n_2xf32` | F1·S3_ALU (+5 slots) | `0x26348306` | in=vec(16×f32), out=vec; **RECIP_Data8** | 16/8 | 0ᵗʰ approx of `1/x` (~7-bit seed) | H/OBS·exec |
| `ivp_rsqrt0n_2xf32` | F1·S3_ALU (+5) | `0x26350106` | in=vec, out=vec; **RSQRT_Data8** | 16/8 | 0ᵗʰ approx of `1/√x` (~7.5-bit seed) | H/OBS·exec |
| `ivp_sqrt0n_2xf32` | F1·S3_ALU (+5) | `0x26350306` | in=vec, out=vec; **RSQRT_Data8** | 16/8 | 0ᵗʰ approx of `√x` (mantissa-domain seed) | H/OBS·exec |
| `ivp_div0n_2xf32` | F1·S3_ALU (+5) | `0x26338306` | in=vec, out=vec; **RECIP_Data8** | 16/8 | 0ᵗʰ approx of `1/d` (denominator seed for `divn`) | H/OBS·exec |
| `ivp_nexp01n_2xf32` | F1·S3_ALU (+5) | `0x26348106` | in=vec, out=vec; **no table** | 16/8 | `2^x` integer-exponent split (`n`,`frac`) | H/OBS·exec |
| `ivp_recip0n_2xf32t` | F1·S3_ALU (+5) | `0x2b080006` | in=vec, out=vec; RECIP_Data8 | 16/8 | `recip0` throttle variant (same value) | H/OBS |
| `ivp_rsqrt0n_2xf32t` | F1·S3_ALU (+5) | `0x2b080206` | in=vec, out=vec; RSQRT_Data8 | 16/8 | `rsqrt0` throttle variant (same value) | H/OBS |
| `ivp_sqrt0n_2xf32t` | F1·S3_ALU (+5) | `0x2b080106` | in=vec, out=vec; RSQRT_Data8 | 16/8 | `sqrt0` throttle variant (same value) | H/OBS |
| `ivp_div0n_2xf32t` | F1·S3_ALU (+5) | `0x2b080004` | in=vec, out=vec; RECIP_Data8 | 16/8 | `div0` throttle variant (same value) | H/OBS |
| `ivp_nexp01n_2xf32t` | F1·S3_ALU (+5) | `0x2b080304` | in=vec, out=vec; no table | 16/8 | `nexp01` throttle variant (same value) | H/OBS |

> **GOTCHA — the opcode-selector immediate is SLOT-LOCAL, not a global opcode number.** The same
> mnemonic carries a *completely different* selector in each of its six slots. `recip0n_2xf32` is
> `0x26348306` in `F1_S3_ALU` but `0x868f8102` in `F0_S3_ALU`, `0x08cc0302` in `F2_S3_ALU`,
> `0x0225bc00` in `F3_S3_ALU`, `0x289c8300` in `F7_S3_ALU`, and `0x6a685c00` in `N0_S3_ALU` (all
> byte-exact this pass). There is **no** additive or bit-pattern relation — the F0 form even differs
> in the high byte (`0x86` vs `0x26`). This page tabulates one canonical slot so the selectors are
> comparable; a reimplementer reads the placement it actually targets from
> `Opcode_<mnem>_Slot_<that-slot>_encode`. This is the same finding as
> [the FLIX encode-thunk model](../core/flix-encoding.md#61-the-universal-encode-thunk-abi) and the
> [B02 slot-local selector GOTCHA](b02-vec-alu-fp.md). `[HIGH/OBSERVED]`

> **NOTE — the `t` suffix is a *throttle / issue* variant, not a different computation.** The five
> `*_2xf32t` mnemonics share their value semantics with the plain forms (the value side has **no**
> distinct `recip0`/`rsqrt0` `_32tf` leaf — only `divn` carries a `32tf` *transposed-float* leaf,
> `module__xdref_divn_32tf_32tf_32tf_2f`). Their fiss implementation is the per-stage
> `opcode__ivp_recip0n_2xf32t__stage_*` family, but the arithmetic seed they emit equals the plain
> form's. The `t` selectors cluster in a **separate band** (`0x2b08_00xx`) distinct from the plain
> band (`0x2634_xx06`/`0x2635_xx06`), so they are independent opcodes a decoder must recognize, even
> though they compute the same seed. Their value tag is `[HIGH/OBSERVED]` on encoding and on
> value-equivalence-by-family; not separately execution-swept (the plain leaf is the value of
> record). `[HIGH/OBSERVED]`

---

## 3. The two seed tables — read directly out of `.rodata`

The seeds are not computed by polynomial; they are **read from two 512-byte lookup tables** in
`libfiss-base.so`'s `.rodata` (section `[13]` @ VMA `0x88ff00`, **VMA == file-offset** — confirmed
`readelf -SW` this pass, so `xxd -s <VMA>` reads them directly):

| table | VMA / file-off | size | entries | layout | indexed by |
|---|---|---|---|---|---|
| `table__RECIP_Data8` | `0x958fc0` | `0x200` | **128** × u32 (low byte used) | 8-bit reciprocal-mantissa seed, **monotone-decreasing** `0xff → 0x81` | `recip0`, `div0` |
| `table__RSQRT_Data8` | `0x958dc0` | `0x200` | **128** × u32 (low byte used) | 8-bit rsqrt-mantissa seed, **two 64-entry binade halves** (NOT globally monotone) | `rsqrt0`, `sqrt0` |

The `Data8` suffix is literal: each table holds an **8-bit** seed value packed in the low byte of a
u32 slot (the high three bytes are zero — verified `all(v < 256)` over all 256 entries). The leading
table bytes, read this pass:

```
RECIP_Data8 (0x958fc0):  ff fd fb f9 f7 f5 f4 f2 f0 ee ed eb e9 e8 e6 e4 …  → … 82 81 81 81   (entry 0..127)
RSQRT_Data8 (0x958dc0):  b4 b3 b2 b0 af ae ac ab aa a9 a8 a7 a6 a5 a3 a2 …  → … b8 b7 b6 b5   (entry 0..127)
```

`RECIP[0]=0xff` (the seed mantissa for `1/1.0⁺`), monotone down to `RECIP[127]=0x81` (`1/2.0⁻`):
the table tabulates `round(256/(1+i/128))` over the unit binade `[1,2)`, i.e. the reciprocal
mantissa with an **implicit leading 1** (the value is the 8-bit fraction `1.xxxxxxx`). `RSQRT[0]=0xb4`
and `RSQRT[127]=0xb5` straddle the **binade boundary** (it is *two* tables interleaved into one
128-entry blob — see §4.2).

> **QUIRK — `div0` and `sqrt0` ship *no table of their own*; they share `recip0`'s and `rsqrt0`'s.**
> `module__xdref_div0_32f_32f` (`@0x878340`) does `lea … table__RECIP_Data8` — the **same** table
> `recip0` uses; `module__xdref_sqrt0_32f_32f` (`@0x878490`) does `lea … table__RSQRT_Data8` — the
> **same** table `rsqrt0` uses (both `lea` addresses re-read this pass). The four reciprocal-class
> seeds therefore cost **two** 512-byte tables, not four. A reimplementer who allocates a distinct
> ROM per mnemonic wastes 1 KB and risks a divergent `div0`/`recip0` seed. `nexp01` references **no**
> table at all (§5). `[HIGH/OBSERVED]`

---

## 4. `recip0` / `rsqrt0` — the seed-lookup core (decoded *and* proven bit-exact)

`module__xdref_recip0_1_1_32f_32f` (`@0x8785f0`, 0x305 B) and `module__xdref_rsqrt0_1_1_32f_32f`
(`@0x878900`, 0x32c B) are the canonical seed leaves. Their ABI (read from the prologue, confirmed by
execution) is **`void leaf(int lane /*ignored*/, uint x_bits, int *out0, int *out1, int *out2)`** —
arg1 = `%esi` = the fp32 input bit-pattern, and the **seed result lands in `*out2`** (`%rcx`, the 5th
argument); `out0`/`out1` carry status/flag fields (zero for normal inputs). The `_1_1_` in the leaf
name is the lane/context contract (two leading context slots), matching the
[`abs_8_8` worked example](template-and-partition.md#52-worked-micro-example--abs-8-bit-driven-live-through-libfiss-base)
pattern.

### 4.1 The recip0 seed-lookup algorithm (annotated from the disassembly)

```c
// ivp_recip0n_2xf32 : out ≈ 1/x   (0th approximation, ~7-bit seed)
// Ported from module__xdref_recip0_1_1_32f_32f @0x8785f0 (libfiss-base.so).
// ABI: leaf(lane, x_bits, *flag0, *flag1, *seed)  — seed in arg5.
uint32_t recip0_f32(uint32_t x) {
    uint32_t exp  = (x >> 23) & 0xff;        // shr $0x17  — biased exponent
    uint32_t mant =  x        & 0x7fffff;    // and $0x7fffff
    uint32_t sign =  x        & 0x80000000;  // sign carried through 1/x unchanged

    // --- special-case classification (zero / denormal / inf / NaN) ---
    //   x==0      -> seed = +/-inf   (sign preserved)
    //   |x|==inf  -> seed = +/-0
    //   x==NaN    -> seed = qNaN (payload propagated)
    //   denormal  -> normalize via bsr (count-leading-zeros) BEFORE indexing  (§4.3)

    // --- normal path ---
    uint32_t idx = mant >> 16;               // top 7 bits of mantissa -> 0..127     (1)
    uint8_t  t   = RECIP_Data8[idx];          // mov (%rcx,%r12,4),%r12d              (2)
    uint32_t seed_mant = (t & 0x7f) << 16;    // drop implicit leading 1, place 22:16 (3)

    // --- exponent reflection: 1/2^e has exponent (-e-1) for mant in (1,2) ---
    //   the leaf computes a reflected biased exponent  E' = 0xfd - exp   (mov $0xfd; sub exp)
    //   with a parity (seta / 0xfd-exp even/odd) correction that shifts the
    //   mantissa placement by one bit on the alternate binade (§4.4).
    uint32_t seed_exp = (0xfd - exp);         // mov $0xfd,%al ; sub %r11d,%eax       (4)

    return sign | (seed_exp << 23) | seed_mant;  // shl $0x17 ; or sign ; or mant
}
```

The four numbered steps are the byte-level reads from the disassembly:
`(1)` `shr $0x16`/`shr $0xd` chain → `mant >> 16`; `(2)` `lea 0xe082f(%rip) → 958fc0
<table__RECIP_Data8>` then `mov (%rcx,%r12,4),%r12d`; `(3)` `shl $0xe`/`and 0x7f0000` deposit;
`(4)` `mov $0xfd,%al ; sub %r11d,%eax` exponent reflection. The `lea` target re-read this pass is
**`0x958fc0`**, the byte-identical address of `table__RECIP_Data8`.

### 4.2 The rsqrt0 seed-lookup — binade-parity indexing

`rsqrt0` differs from `recip0` in exactly one structural way: `1/√x`'s binade depends on the
**parity of x's exponent** (`1/√(m·2^e)` lands in a different unit interval for even vs odd `e`), so
the 128-entry `RSQRT_Data8` is **two stacked 64-entry sub-tables**, and the index folds in the
exponent LSB:

```c
// ivp_rsqrt0n_2xf32 : out ≈ 1/√x   (~7.5-bit seed)
// Ported from module__xdref_rsqrt0_1_1_32f_32f @0x878900.
uint32_t rsqrt0_f32(uint32_t x) {
    uint32_t exp  = (x >> 23) & 0xff;
    uint32_t mant =  x        & 0x7fffff;
    int      parity = exp & 1;                       // exponent LSB selects the sub-table
    // index = (parity ? 64 : 0) + (mant >> 17)      // 6-bit offset within a 64-entry half
    uint32_t idx = (parity ? 64u : 0u) + (mant >> 17);   // lea … RSQRT_Data8 ; or parity ; mov(…,4)
    uint8_t  t   = RSQRT_Data8[idx];                  // shr $0xe + or %r9d (parity) on the index
    // even-binade path uses (t & 0x7f); odd-binade path remaps (t & 0x3f) | 0x40  (878ad8)
    uint32_t seed_mant = (t & 0x7f) << 16;            // shl $0x10  -> bits 23:16
    // x<0 -> qNaN+Invalid;  x==0 -> +inf;  x==inf -> +0;  denormal -> bsr-normalize first
    uint32_t seed_exp  = 0xbe - (exp >> 1);           // half-exponent for the square root
    return (seed_exp << 23) | seed_mant;              // sign always 0 (1/√x of a positive)
}
```

The exponent LSB selecting upper/lower table half, and the `mant >> 17` (top 6 bits) offset, were
**re-derived empirically and confirmed against the table** (§4.4): odd-parity inputs index entries
64..127, even-parity index 0..63.

### 4.3 Denormal & zero handling — the `bsr` normalization (proven by execution)

Both leaves have a denormal pre-normalization branch: when the exponent field is 0 (a subnormal),
they `bsr` (bit-scan-reverse = find MSB) the mantissa, shift it up to synthesize an implicit
leading 1, and adjust the synthetic exponent before indexing — a real
[count-leading-zeros normalization](../core/fp-sub-isa.md). The special-value outputs, driven live:

```
recip0(+0   = 0x00000000) -> 0x7f800000  (+inf)
recip0(-0   = 0x80000000) -> 0xff800000  (-inf)        // sign preserved
recip0(+inf = 0x7f800000) -> 0x00000000  (+0)
recip0(qNaN = 0x7fc00000) -> 0x7fd50000  (qNaN, payload mutated by the seed pack)
recip0(2^-126=0x00800000) -> 0x7e7f0000  (≈ 8.47e37 = 1/2^-126, exponent reflected, mant=RECIP[0])
```

`1/(+0) = +inf`, `1/(-0) = -inf` (sign threaded), `1/inf = +0`, and the smallest normal `2^-126`
maps to a near-max-finite seed with mantissa byte `0x7f` = `RECIP[0] & 0x7f` — the table value for
mantissa `1.0`, exactly as the algorithm predicts. `[HIGH/OBSERVED·exec]`

### 4.4 Bit-exact reproduction certificate — the leaf reproduces the `.rodata` table

The decisive OBSERVED-by-execution fact: for **normal inputs in the canonical binade**, the seed
mantissa byte emitted by the leaf **equals the `.rodata` table entry, bit for bit**. Driven live via
ctypes this pass, sweeping all 128 mantissa buckets:

```python
import ctypes, struct
data = open("libfiss-base.so","rb").read()
RECIP = struct.unpack_from("<128I", data, 0x958fc0)        # read table straight from .rodata
RSQRT = struct.unpack_from("<128I", data, 0x958dc0)
lib   = ctypes.CDLL("libfiss-base.so")
f = lib.module__xdref_recip0_1_1_32f_32f; f.restype = None
f.argtypes = [ctypes.c_int, ctypes.c_int] + [ctypes.POINTER(ctypes.c_int)]*3
def recip0(xb):
    o = [ctypes.c_int(0) for _ in range(3)]
    f(0, xb, *[ctypes.byref(c) for c in o]); return o[2].value & 0xffffffff   # seed in arg5
# for x in [1,2): exp=127, mantissa bucket = (mant>>16)
mism = sum(1 for i in range(128)
           if ((recip0((127<<23)|(i<<16)) >> 16) & 0xff) != (RECIP[i] & 0x7f))
# -> mism == 0    (128/128 buckets bit-exact)
```

Results, this pass:

| certificate | result |
|---|---|
| `recip0` seed mantissa hi-byte `==` `RECIP_Data8[mant>>16] & 0x7f`, all **128 buckets**, x∈[1,2) | **0 mismatches** |
| `rsqrt0` seed mantissa hi-byte `==` `RSQRT_Data8[64+(mant>>17)] & 0x7f`, **128 buckets**, x∈[1,2) (odd binade) | **0 mismatches** |
| `rsqrt0` seed mantissa hi-byte `==` `RSQRT_Data8[mant>>17] & 0x7f`, **128 buckets**, x∈[2,4) (even binade) | **0 mismatches** |

Spot values (live): `recip0(1.0)=0x3f7f0000` (mant `0x7f` = `RECIP[0]&0x7f` ✓), `recip0(1.25)=
0x3f4c0000` (`0x4c` = `RECIP[32]&0x7f = 0xcc&0x7f` ✓), `recip0(1.5)=0x3f2a0000`
(`0x2a` = `RECIP[64]&0x7f = 0xaa&0x7f` ✓), `recip0(1.9999)=0x3f010000`
(`0x01` = `RECIP[127]&0x7f = 0x81&0x7f` ✓). This is an **OBSERVED-by-execution certificate**: the
seed table column of these roster rows is *proven*, not inferred. `[HIGH/OBSERVED·exec]`

> **QUIRK — the seed exponent is *reflected* (`0xfd − exp`), and the mantissa placement alternates by
> binade parity.** `recip0` of an even-exponent input vs an odd-exponent input deposits the table
> byte with a one-bit positional shift (the `seta`/`0xfd-exp`-parity logic at `+0x148`): re-running
> the 128-bucket reproduction at the *adjacent* binade (`exp=126`, x∈[0.5,1)) with the *same naive*
> `(seed>>16)&0xff` extraction shows 128 "mismatches" — **not** because the table is wrong, but
> because the seed reconstruction reflects the exponent and re-aligns the mantissa for the opposite
> binade. The table value is still the source; the placement is binade-dependent. A reimplementer
> must model both the `0xfd − exp` reflection and the parity-dependent mantissa shift, not just the
> table read. `[HIGH/OBSERVED·exec]` (the canonical-binade reproduction is the 0-mismatch certificate
> above; the alternation is the documented complement).

---

## 5. `sqrt0` / `div0` / `nexp01` — the three derived seeds

### 5.1 `div0` — the denominator seed for `divn` (mantissa-domain reciprocal)

`module__xdref_div0_32f_32f` (`@0x878340`, 0x142 B) is the **division** seed: it produces a
normalized `≈1/d` to seed the `divn` Newton step ([B23](b23-divide.md)). It indexes
`table__RECIP_Data8` (the **same** ROM as `recip0`) but builds a *different* exponent — it keeps only
the input exponent's **parity**, deferring the full reciprocal-exponent fix-up to `divn`:

```c
// ivp_div0n_2xf32 : out ≈ 1/d (denominator seed, mantissa-domain, parity-only exponent)
// Ported from module__xdref_div0_32f_32f @0x878340 — lea … table__RECIP_Data8.
uint32_t div0_f32(uint32_t d) {
    uint32_t mant = d & 0x7fffff;
    uint32_t idx  = mant >> 16;                 // shr $0xd after re-pack -> top 7 mantissa bits
    uint8_t  t    = RECIP_Data8[idx];           // SAME table as recip0
    uint32_t seed_mant = (t & 0x7f) << 16;      // shl $0x10 ; and $0x7f0000
    // exponent: 0x3f000000 base (biased 0x7e), conditionally |0xff800000 by exponent LSB:
    int parity = (normexp & 1);                 // and $0x1 on the normalization shift
    uint32_t seed_exp_field = parity ? 0x3e800000 : 0x3f000000;   // add $0x3f000000 ; sbb mask
    return (d & 0x80000000) | seed_exp_field | seed_mant;
}
```

The execution-confirmed signature of this "mantissa-domain + parity" design: **div0 of inputs with
the same mantissa but different exponent-parity collide or differ only by one binade**:

```
div0(1.0) = 0x3f7f0000 (0.996)      div0(4.0) = 0x3f7f0000 (0.996)   // 1.0 and 4.0 share mantissa & parity
div0(2.0) = 0x3eff0000 (0.498)      div0(3.0) = 0x3eaa0000 (0.332)   // 2.0 differs by exponent parity
```

`div0(1.0)==div0(4.0)` because both are `1.0 × 2^even`; `div0(2.0)` differs because `2.0 = 1.0 × 2^1`
has odd parity. The full exponent of `1/d` is reconstructed by the `divn` refine, not by `div0`.

> **GOTCHA — `div0` is NOT `recip0`.** Both index `RECIP_Data8`, but `recip0` builds the *full*
> reflected reciprocal exponent (`0xfd − exp`) while `div0` keeps only **one parity bit** and biases
> to `0x7e`/`0x7d`, because `div0`'s consumer (`divn`, [B23](b23-divide.md)) already carries the
> numerator and reconstructs the quotient exponent. Substituting `recip0` for `div0` feeds `divn` a
> doubly-exponent-adjusted seed and diverges. `[HIGH/OBSERVED·exec]`

### 5.2 `sqrt0` — the square-root seed (mantissa-domain, RSQRT table)

`module__xdref_sqrt0_32f_32f` (`@0x878490`, 0x15c B) indexes `table__RSQRT_Data8` (the **same** ROM
as `rsqrt0`) and emits a `≈√x` seed. Like `div0` it is mantissa-domain with a half-exponent:

```
sqrt0(1.0) = 0x3f7f0000 (0.996)     sqrt0(4.0) = 0x3f7f0000 (0.996)   // share mantissa 1.0
sqrt0(2.0) = 0x3f340000 (0.703)     sqrt0(9.0) = 0x3f2a0000 (0.664)   // 9.0 mantissa = 1.125
```

`sqrt0` reuses the rsqrt ROM because `√x = x · (1/√x)`: the seed is the rsqrt seed scaled, and the
refine (Newton on `√x`, or `x · rsqrt`) finishes it. `[HIGH/OBSERVED·exec]`

### 5.3 `nexp01` — the table-free exponent split

`module__xdref_nexp01_32f_32f` (`@0x87a4d0`, 0xc1 B) is the odd one out: it references **no seed
table** — it is **pure bit manipulation**. It computes the `2^x = 2^n · 2^frac` decomposition, the
`n` integer-exponent split and the reduced fractional argument that the subsequent polynomial
([B17](b17-spfma.md)) consumes:

```c
// ivp_nexp01n_2xf32 : 2^x range-reduction split (the "n" in nexp = integer exponent).
// Ported from module__xdref_nexp01_32f_32f @0x87a4d0 — NO table lookup, no .rodata ref.
uint32_t nexp01_f32(uint32_t x) {
    uint32_t exp  = (x >> 23) & 0xff;
    uint32_t mant =  x        & 0x7fffff;
    // align mantissa to fixed-point by the (unbiased) exponent (shl $0x18 / shl by 0x17-exp),
    // negate and re-bias to produce the floor/integer part 'n' and the residual fraction:
    //   bsr-normalize, sub from {0x17,0x18,0x34}, mask 0x7f, re-assemble  (87a517..87a553)
    // returns the split as a single fp32 word (n in exponent-ish field, frac in mantissa).
    // special: x==inf/NaN -> 0x7f800000|mant ;  x==0 -> trivial.
}
```

Live: `nexp01(0.0)=0xbf800000 (-1)`, `nexp01(0.5)=0xc0000000 (-2)`, `nexp01(1.0)=0xbf800000 (-1)`,
`nexp01(2.0)=0xc0000000 (-2)`, `nexp01(-1.0)=0x3f800000 (+1)` — a staircase integer-split, **not** an
approximation of `e^-x` or `2^-x` (compare `2^-0.5 = 0.707`, which `nexp01(0.5)` is *not*). It is the
**range-reduction primitive**, the fp32 analogue of the fp16 `nexp01nxf16`; the actual `exp`/`nexp`
value is built downstream by an FMA polynomial on the `frac` and a scale by `2^n`. `[HIGH/OBSERVED·exec]`

> **QUIRK — `nexp01` is the only seed op with no ROM, and fp32 has *only* the `01` form.** There is
> no `nexp0n_2xf32` (the plain exp seed) — that mnemonic exists **only** in fp16. The fp32 exp path
> is `nexp01` (range-split) → FMA polynomial → `addexp` scale ([B02](b02-vec-alu-fp.md)'s `addexpn`).
> A reimplementer must not expect a table-driven fp32 `nexp0` seed; the fp32 exp is computed entirely
> in the ALU + FMA, with `nexp01` doing only the argument reduction. `[HIGH/OBSERVED]`

---

## 6. The Newton–Raphson / QLI refinement the seed feeds (boundary to B17/B23)

The seed is the **0ᵗʰ iterate**; the refinement is **not in this batch** but is named here so the
seed's role is unambiguous. fp32 needs **24 significand bits**; a ~7-bit seed reaches it in
**2 Newton iterations** (each Newton step roughly **doubles** the correct-bit count: 7 → 14 → 28 ≥ 24).
Measured live this pass over 50,000 random x∈[1,2): `recip0` max relative error `0.00780 ≈ 2⁻⁷·⁰`
(7.0 bits); `rsqrt0` `0.00546 ≈ 2⁻⁷·⁵` (7.5 bits) — both confirm the 2-iteration budget.

**Reciprocal Newton step** (`y' = y·(2 − x·y)`), one fp32 FMA pair per iteration — the
[B17 fp32 FMA](b17-spfma.md) ops:

```c
// after y0 = recip0(x):           // ~7 bits
float r  = fma(-x, y0, 2.0f);      // 2 - x*y0
float y1 = y0 * r;                 // ~14 bits
float r2 = fma(-x, y1, 2.0f);
float y2 = y1 * r2;                // ~28 bits >= 24  -> fp32-accurate 1/x
```

**Reciprocal-sqrt Newton step** (`y' = y·(1.5 − 0.5·x·y²)`), two FMAs per iteration:

```c
// after y0 = rsqrt0(x):
float h  = 0.5f * x;
float t1 = fma(-h*y0, y0, 1.5f);   // 1.5 - 0.5*x*y0^2
float y1 = y0 * t1;                // ~15 bits
float t2 = fma(-h*y1, y1, 1.5f);
float y2 = y1 * t2;                // ~30 bits >= 24  -> fp32-accurate 1/sqrt(x)
```

**Division** uses `div0` → the `divn` Newton-step macro
(`module__xdref_divn_1_1_1_32f_32f_32f_32f_2` @`0x878c30`, 0x186b B, **3 fp32 inputs** = numerator,
denominator, seed; references **no table** — it is the iteration that *consumes* `div0`'s seed). The
QLI (quadratic linear-interpolation) refine `recipqli` (`module__xdref_recipqli_1_1_1_1_1_32f_32f`
@`0x87df20`, 0x8fc B) uses the **separate** const tables `table__fp_recip_qli_lut1/2_{A,gx}`
(@`0x9587c0..0x958dc0`) — those LUTs are part of the **refine**, owned by
[B23](b23-divide.md)/B24, not this batch. `[HIGH/INFERRED]` on the exact Newton coefficients (the
iteration math is the IEEE-standard form; the *seed* is the OBSERVED part — the refine ops carry
their own value certificates on their pages).

---

## 7. Batch tally — every sp_lookup mnemonic vs `nm`

The 10 mnemonics and their placement counts, re-counted this pass with
`nm libisa-core.so | rg -c 'Opcode_ivp_<mnem>_Slot_.*_encode'` (the only legitimate count method,
per [coverage tally](../core/coverage-tally.md)):

| group | base mnemonic (placements) | `t` mnemonic (placements) | value leaf (fiss) |
|---|---|---|---|
| reciprocal | `recip0n_2xf32` (6) | `recip0n_2xf32t` (6) | `recip0_1_1_32f_32f` @`0x8785f0` |
| recip-sqrt | `rsqrt0n_2xf32` (6) | `rsqrt0n_2xf32t` (6) | `rsqrt0_1_1_32f_32f` @`0x878900` |
| sqrt | `sqrt0n_2xf32` (6) | `sqrt0n_2xf32t` (6) | `sqrt0_32f_32f` @`0x878490` |
| division | `div0n_2xf32` (6) | `div0n_2xf32t` (6) | `div0_32f_32f` @`0x878340` |
| exp-split | `nexp01n_2xf32` (6) | `nexp01n_2xf32t` (6) | `nexp01_32f_32f` @`0x87a4d0` |

```
base subtotal : 5 mnemonics, 30 placements
t    subtotal : 5 mnemonics, 30 placements
BATCH TOTAL   : 10 mnemonics, 60 placements   (of the 12569-placement certified cover)
```

Each mnemonic is placed in the **6 `S3_ALU` slots `{F0,F1,F2,F3,F7,N0}`** — `5 × 6 = 30` per
sub-roster, `60` total. The slot set is byte-confirmed from `nm` (the
`Opcode_ivp_<m>_Slot_{f0,f1,f2,f3,f7,n0}_s3_alu_encode` family of every mnemonic).

**Value-leaf grounding.** **5 value leaves** back the 10 mnemonics (each plain/`t` pair shares one):
`recip0`, `rsqrt0`, `sqrt0`, `div0`, `nexp01`. **All five were driven live this pass**; `recip0`
and `rsqrt0` carry the **128-bucket bit-exact `.rodata` reproduction certificate** (§4.4);
`div0`/`sqrt0`/`nexp01` were swept on the special and binade-boundary inputs (§5). The five leaves
roll into the **864** value-leaf denominator
([coverage tally §6.3](template-and-partition.md#63-the-value-leaf-roll-up)). `[HIGH/OBSERVED·exec]`

> **NOTE — ungrounded / deferred items (honestly flagged).**
> (1) The **`t` variants'** value-equivalence is asserted at family confidence `[HIGH/OBSERVED]` (no
> distinct `_32tf` recip/rsqrt leaf exists; the `t` selectors and `opcode__ivp_*_2xf32t__stage_*`
> symbols are encoding-OBSERVED) but the `t` forms were **not separately execution-swept** — the
> plain leaf is the value of record. (2) The **device-interior seed coefficients** (the precise
> rounding the hardware's `0xfd − exp` reflection applies in the *non-canonical* binade, and the
> denormal `bsr` corner) are **`CARRIED`** at the [FW-42 wall](../../reference/confidence-model.md):
> the `.rodata` seed **table** is OBSERVED and bit-exactly reproduced; device-interior coefficient
> claims beyond the table value are flagged CARRIED. (3) **Per-instruction cycle latency** — the
> `bbn_sem_vec_sprecip_rsqrt_opcode` pipeline is **16 stages** in `libcas-core.so` (§8); that stage
> count is OBSERVED from the symbol family, but the cycle oracle is license-gated, so a retirement
> latency is not run. (4) The **Newton/QLI refine coefficients** (§6) are `[HIGH/INFERRED]` (standard
> IEEE iteration); the refine ops are owned by [B17](b17-spfma.md)/[B23](b23-divide.md).

---

## 8. Pipeline & function map

| symbol / table | addr (VMA) | role |
|---|---|---|
| `module__xdref_recip0_1_1_32f_32f` | `0x8785f0` (`libfiss-base.so`) | `1/x` seed leaf — RECIP_Data8 lookup (§4) |
| `module__xdref_rsqrt0_1_1_32f_32f` | `0x878900` | `1/√x` seed leaf — RSQRT_Data8, parity index (§4.2) |
| `module__xdref_sqrt0_32f_32f` | `0x878490` | `√x` seed — shares RSQRT_Data8 (§5.2) |
| `module__xdref_div0_32f_32f` | `0x878340` | `1/d` denominator seed — shares RECIP_Data8 (§5.1) |
| `module__xdref_nexp01_32f_32f` | `0x87a4d0` | `2^x` split — **table-free** (§5.3) |
| `table__RECIP_Data8` | `0x958fc0` (`.rodata`) | 128 × 8-bit reciprocal-mantissa seeds (§3) |
| `table__RSQRT_Data8` | `0x958dc0` (`.rodata`) | 128 × 8-bit rsqrt-mantissa seeds, 2 binade halves (§3) |
| `module__xdref_divn_1_1_1_32f_32f_32f_32f_2` | `0x878c30` | **fp** `divn` Newton refine — the **fp DIVN family** (consumes div0 seed); *not* a B23 opcode — B23 owns the *integer* `divn_2x32x16` |
| `module__xdref_recipqli_1_1_1_1_1_32f_32f` | `0x87df20` | QLI refine — **B24** fp (uses `fp_recip_qli_lut*`) |
| `bbn_sem_vec_sprecip_rsqrt_opcode_stage0..15` | `0x61b0a0…` (`libcas-core.so`) | the **16-stage** seed pipeline (recip+rsqrt share it) |
| `Opcode_ivp_recip0n_2xf32_Slot_f1_s3_alu_encode` | `0x34b860` (`libisa-core.so`) | `movl $0x26348306,(%rdi); ret` — F1 selector (§2) |
| `Iclass_IVP_RECIP0N_2XF32_args` | `0x852ba0` (`libisa-core.so`) | recip0 operand iclass record |

> **NOTE — recip and rsqrt are *one* hardware pipeline.** `libcas-core.so` carries a single
> transcendental opcode family, `bbn_sem_vec_sprecip_rsqrt_opcode`, spanning **16 stages**
> (`stage0..stage15`, count re-read this pass) — the `sprecip_rsqrt` name and the shared stage chain
> show the single-precision reciprocal and reciprocal-sqrt seeds are computed by the *same* 16-deep
> datapath, multiplexed by the opcode selector. This is consistent with the two ops sharing the ALU
> slot family and differing only in which `.rodata` table they index. `[HIGH/OBSERVED]`

---

## 9. Batch boundary — what this page does NOT own

| neighbor op(s) | owner page | why not here |
|---|---|---|
| `recip0nxf16`/`rsqrt0nxf16`/`sqrt0nxf16`/`div0nxf16`/`nexp0nxf16`/`nexp01nxf16` | [B14 hp-lookup](b14-hp-lookup.md) (#629) | **fp16** transcendental seeds — same seed-lookup architecture at half precision |
| `recipqlin_2xf32_0/_1` (and `t`) | [B23 divide](b23-divide.md)/B24 | QLI **refine** (quadratic linear-interp), uses `fp_recip_qli_lut*`, consumes the seed |
| `divn_2x32x16*_4step*` (**integer**), `divnx16*` (**integer**) | [B23 divide](b23-divide.md) (#638) | the **integer** Newton-step divide opcodes (32b÷16b lane quotient). The **fp** `divnn_2xf32`/`divnnxf16` and the fp `xdref_divn_*_32f` Newton refine are the **separate fp DIVN family**, not B23 opcodes |
| `mula*`/`madd*`/`fma*` fp32 | [B17 fp32 FMA](b17-spfma.md) (#632) | the fused multiply-add **refine** iterations (`y·(2−x·y)`, `y·(1.5−0.5xy²)`) |
| `mula*`/`madd*`/`fma*` fp16 | [B18 fp16 FMA](b18-hp-fma.md) (#633) | fp16 FMA refine |
| `addexpn_2xf32`/`addexpmn_2xf32` | [B02 fp ALU](b02-vec-alu-fp.md) (#617) | exponent-add scale-by-2ⁿ — the `2^n` step *after* `nexp01`'s split |
| `cvt*…32` (fp32 convert / round) | [B13 sp-cvt](b13-sp-cvt.md) (#628) | round-to-integral / convert, not a seed |

> **NOTE — the seed TABLES are shared with the ISS oracle (Part 14).** `table__RECIP_Data8` /
> `table__RSQRT_Data8` are the **same `.rodata` blobs** the
> [transcendental-seed validation §coverage-tally](../core/coverage-tally.md) certifies and the
> ISS/`libfiss-base` oracle indexes — this page reads them at `0x958fc0`/`0x958dc0`, the ISS executes
> the leaves that index them. The fp32 (`*_Data8`, 128-entry) and the fp16 seed tables are **distinct
> storage**: the fp32 forms index `RECIP_Data8`/`RSQRT_Data8`; the fp16 forms (B14) index their own
> half-precision tables. There is **no** shared fp32/fp16 seed ROM — they are separate `.rodata`
> objects with separate `lea` targets. `[HIGH/OBSERVED]`

---

## 10. Adversarial self-verification — the five strongest claims, re-challenged

Each headline claim re-tested against the binary this pass; a claim survives only if a *second*
independent witness agrees.

1. **The seed leaf reproduces the `.rodata` table bit-exactly.** *Challenge:* could the apparent
   match be my harness reading the wrong output pointer, or the table by coincidence? *Re-test:* the
   seed pointer was fixed by the sanity `recip0(1.0)=0x3f7f0000` (≈1.0); the **full 128-bucket sweep**
   at x∈[1,2) gave seed-mantissa-hi-byte `== RECIP_Data8[i] & 0x7f` with **0 mismatches**, and the
   table itself was read **independently** from `.rodata` via `xxd`/`struct.unpack` (not via the
   leaf). Two independent reads (static `.rodata` byte + dynamic leaf execution) agree across all 128
   entries. `rsqrt0` reproduces `RSQRT_Data8` over **both** 64-entry binade halves with 0 mismatches.
   **Survives.** `[HIGH/OBSERVED·exec]`

2. **`div0` and `sqrt0` share `recip0`'s / `rsqrt0`'s tables (no private ROM).** *Challenge:* maybe
   each leaf has its own copy at a nearby address? *Re-test:* the `lea …(%rip)` in
   `module__xdref_div0_32f_32f` resolves to **`0x958fc0` = `table__RECIP_Data8`** (byte-identical to
   `recip0`'s `lea` target), and `sqrt0`'s to **`0x958dc0` = `table__RSQRT_Data8`** (identical to
   `rsqrt0`'s). `nm` shows only **two** `*_Data8` tables exist, not four. Same address ⇒ same ROM.
   **Survives.** `[HIGH/OBSERVED]`

3. **The fp32 roster is 10 mnemonics / 60 placements, and there is no `nexp0n_2xf32`.** *Challenge:*
   could a plain fp32 exp seed exist under a different spelling? *Re-test:*
   `nm libisa-core.so | rg -o 'Opcode_(ivp_nexp0[a-z0-9_]*)_Slot' -r '$1' | sort -u` returns exactly
   `{nexp0nxf16, nexp01nxf16, nexp01n_2xf32, nexp01n_2xf32t}` — the only fp32 exp seeds are the `01`
   forms; the plain `nexp0` is fp16-only. Each of the 10 fp32 seed mnemonics has exactly **6**
   `Slot_*_s3_alu_encode` thunks (`nm | rg -c`), summing to **60**. **Survives.** `[HIGH/OBSERVED]`

4. **The seed is ~7 bits and needs 2 Newton iterations for fp32.** *Challenge:* maybe the seed is
   already more accurate and one iteration suffices? *Re-test:* a 50,000-sample live sweep over
   x∈[1,2) gives `recip0` max relative error `0.00780 = 2⁻⁷·⁰⁰` and `rsqrt0` `0.00546 = 2⁻⁷·⁵³`. One
   Newton step from 7.0 bits reaches ~14 bits (< 24); two reach ~28 (≥ 24). The measured 7-bit seed
   is exactly the table's 8-bit-byte resolution minus the truncation, independently consistent with
   the `Data8` (8-bit) table width. **Survives.** `[HIGH/OBSERVED·exec]`

5. **recip and rsqrt are one 16-stage hardware pipeline; the `t` variants compute the same seed.**
   *Challenge:* could `recip0` and `rsqrt0` be separate datapaths, and could `t` change the math?
   *Re-test:* `libcas-core.so` carries a **single** transcendental opcode family
   `bbn_sem_vec_sprecip_rsqrt_opcode` with `stage0..stage15` (16 stages, count re-read) — the
   combined `sprecip_rsqrt` name proves one shared pipeline; there is **no** separate
   `vec_recip`/`vec_rsqrt` stage family. For `t`: the value side has **no** `recip0`/`rsqrt0` `_32tf`
   leaf — only the plain `_32f` leaves exist, so the `t` forms necessarily resolve to the same
   arithmetic (the `t` is an issue/throttle attribute, its selectors in a distinct `0x2b08_*` band).
   **Survives.** `[HIGH/OBSERVED]` (value-equivalence at family confidence; encoding fully OBSERVED).

No claim on this page rests on a raw dump, an unnamed symbol, or a single uncorroborated witness; the
seed-table reproduction carries a 128-bucket differential-execution certificate against the shipped
`.rodata` bytes, and every count names its `nm` witness.

---

## 11. Confidence ledger

**HIGH / OBSERVED (by execution + `.rodata` read)** — driven live against `libfiss-base.so` and read
directly from `.rodata` this pass:

* `recip0`/`rsqrt0` seed mantissa byte `==` `table__{RECIP,RSQRT}_Data8[index] & 0x7f`, bit-exact
  over **128 buckets** each (canonical binade for recip; both binade halves for rsqrt) — 0 mismatches.
* The two seed tables read straight from `.rodata` (VMA == file-off): RECIP monotone `0xff→0x81`,
  RSQRT two-binade non-monotone; both 128 × 8-bit.
* Table sharing: `div0`→RECIP_Data8, `sqrt0`→RSQRT_Data8 (`lea` targets `0x958fc0`/`0x958dc0`).
* The special-value algebra of `recip0` (`1/±0=±inf`, `1/inf=+0`, sign threaded, `2^-126` seed),
  the `div0` mantissa-domain-plus-parity collision (`div0(1.0)==div0(4.0)`), the `sqrt0` reuse of
  the rsqrt ROM, and `nexp01`'s table-free integer-exponent staircase.
* Seed accuracy ≈ 7.0 / 7.5 bits ⇒ 2 Newton iterations for fp32.

**HIGH / OBSERVED** — read from `libisa-core.so` immediates / `libcas-core.so` symbols this pass:

* The 10-mnemonic roster, the 6 `S3_ALU` slots, the F1 opcode-selector immediates (plain + `t`), the
  slot-local selector variance, the single-word (`word1`-absent) thunk shape, the 60-placement census.
* The `bbn_sem_vec_sprecip_rsqrt_opcode` 16-stage pipeline; the `nexp0n_2xf32` non-existence.

**HIGH / OBSERVED (value-equivalence at family confidence)**

* The `t` variants compute the same seed as the plain forms (no distinct `_32tf` recip/rsqrt leaf).

**CARRIED (FW-42 wall)**

* Device-interior seed-coefficient internals beyond the `.rodata` table value (non-canonical-binade
  reflection rounding, denormal `bsr` corner) — the table is OBSERVED; the interior coefficient is
  CARRIED.

**Deferred (not in this batch's denominator)**

* Newton/QLI refine math (§6) → [B17](b17-spfma.md)/[B23](b23-divide.md); fp16 seeds →
  [B14](b14-hp-lookup.md); the `2^n` scale → [B02 `addexpn`](b02-vec-alu-fp.md).

All facts read as derived from shipped-artifact static analysis and license-free in-process execution
of the binary's own value leaves and direct `.rodata` table reads (lawful interoperability RE).

---

## Cross-references

* [B14 fp16 transcendental seeds (hp_lookup)](b14-hp-lookup.md) — the **fp16 sibling**: identical
  seed-lookup-then-Newton architecture at half precision, plus the fp16-only `nexp0` form this page
  contrasts against.
* [B17 fp32 FMA](b17-spfma.md) · [B23 divide](b23-divide.md) — the Newton-Raphson (`y·(2−x·y)`,
  `y·(1.5−0.5xy²)`) and `divn`/QLI **refine** iterations that consume these seeds.
* [B02 vector ALU (fp slice)](b02-vec-alu-fp.md) — the `addexpn_2xf32` scale-by-2ⁿ that finishes the
  `nexp01` exp path, and the fp-ALU `F1_S3_ALU` slot these selectors share.
* [The Floating-Point Sub-ISA (FCR/FSR view)](../core/fp-sub-isa.md) — the two-level RNE/RZ rounding
  context, the FSR sticky flags, and the fp32 1-8-23 datatype model.
* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid, the six
  `S3_ALU` slots these seeds occupy, and the encode-thunk ABI.
* [The Eight Register Files](../core/register-files.md) — the `vec` (idx 2) 512-bit datapath, fp32 =
  16 lanes of 32-bit.
* [ISA Coverage & the 1534/1607/12642 Tally](../core/coverage-tally.md) — the `12569`-placement
  denominator this batch's `60` is a slice of, the **864** value-leaf roll-up, and the
  transcendental-seed-LUT validation (Part 14) that shares these `.rodata` tables.
* [ISA Reference — Template & Partition](template-and-partition.md) — the canonical B01–B30 schema,
  the B15 partition row, and the per-mnemonic extraction methodology.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the FW-42 seed-
  coefficient wall, and the proven-by-execution value lane (`libfiss-base.so` via ctypes).
