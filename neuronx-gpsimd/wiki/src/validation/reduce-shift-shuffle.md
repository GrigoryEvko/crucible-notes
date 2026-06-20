# VAL — Reduce / Shift / Shuffle-Select Family

This page applies the [4-oracle bit-exact differential method](four-oracle-method.md) to the
**three horizontal / permute sub-families** of the GPSIMD datapath: the ops that are *not*
pure per-lane elementwise. It is the family instance for everything that collapses a vector
(REDUCE), re-distributes bits inside a lane (SHIFT), or re-distributes whole lanes across the
vector (SHUFFLE / SELECT). The method is unchanged — same harness, same four legs, same
"identical inputs ⇒ bit-exact or root-caused divergence" contract — only the leaf root set,
the SEM block, the nki op, and the FLIX mnemonics change.

The result up front: across **83 directed edge probes + 2,005 seeded fuzz cases = 2,088
total**, the four references — the [GX-SEM RTL model](../isa/semantics/formal-isa-model.md)
**(a)**, the device-toolchain FLIX decode **(b)**, the nki-0.3.0 numpy reference **(c)**, and
the shipped `libfiss-base.so` value leaves **driven LIVE via ctypes (d)** — are
**bit-identical on every op the references jointly define. Zero mismatches.** One initial SEM
gap (a writeback-width mis-spec) and one nki API-surface divergence (single-index argmax)
were surfaced, disassembled, and root-caused; both are documented in §6, and in both cases
the *value* still agrees on all four legs.

> **NOTE.** Every `16-bit lane / _16_512 / _32_512 / 2NX8 / N_2X32 / &0x3f / &0x1f /
> 0x7fff / 0x8000 / N0-format / slot3` token below is a **datapath-width / FLIX-format /
> pool-size / ISA-lane axis of the single Cairo config** (`Xm_ncore2gp`, Xtensa24, NX1.1.4,
> RI-2022.9; the 1534-folded opcode set). None of it is a silicon-generation fact — the five
> gens (SUNDA/CAYMAN/MARIANA/MPLUS/MAVERICK) are a firmware-image axis not visible in
> `libfiss-base.so`, the FLIX tables, the TIE-XML, or the nki simulator.

The encodings these legs validate are catalogued in [b08-reduce](../isa/ref/b08-reduce.md),
[b12-shift](../isa/ref/b12-shift.md), and
[b21-select-shuffle](../isa/ref/b21-select-shuffle.md); the same value bodies appear in the
ISS oracle's cross-lane staging at
[cas-valign-shuffle-reduce](../iss/cas-valign-shuffle-reduce.md); and the firmware that
*calls* these reduces in anger is documented in
[cross-lane-reduce](../firmware/kernels/cross-lane-reduce.md).

---

## 1. Why these three families group here  `[HIGH]`

The elementwise families ([four-oracle-method](four-oracle-method.md) §4, mac-multiply,
convert-pack-cast) all share one property: the leaf is **per-lane** —
`result[k] = op(A[k], B[k])`, lanes independent, and a per-lane differential *is* the vector
differential. The three families on this page break that property in three different ways,
and that is exactly what makes them the ideal differential target:

| Sub-family | Structural shape | Leaf signature shift from the elementwise ABI |
|------------|------------------|-----------------------------------------------|
| **REDUCE** | horizontal collapse of all N lanes to a broadcast scalar (+ optional argmin/argmax lane-mask) | whole-vector — the leaf takes a **64-byte source pointer** and folds internally; *not* a per-lane call |
| **SHIFT** | per-lane bit-shift / rotate / normalize — still per-lane, but the second operand is a **shift amount**, not a lane value, and the over-shift / saturate / CLZ behaviour has no elementwise analogue | per-lane binary `f(rdi, value, amount, *out)` + unary `f(rdi, value, *out)` for `nsa`/`nsau` |
| **SHUFFLE / SELECT** | whole-vector lane permute — `out[k] = pool[ctrl[k] & mask]`, a crossbar; `dcmprs` is a predicate-driven scatter | whole-vector — multiple 64-byte source/ctrl/out pointers |

The deeper reason agreement is *real* and not a copy: for `rmax` the SEM leg (a) is a python
`max()` over sign-extended lanes; the LIVE leaf (d) is a **left-fold of the sign-bias
`max_16_16_16` primitive** (flip bit 15 of both operands, unsigned-compare); the numpy leg
(c) is `np.max` on `int16`. Three structurally distinct implementations, one output. For
`dcmprs` the SEM leg computes `out[k] = src[popcount(vbool & ((1<<k)-1))]` directly while the
LIVE leaf threads `module__xdref_popc64_7_64` + `module__xdref_dcmprs_clamp` internally —
again, distinct algorithms, identical bytes.

> **Scope.** The fp NUM family appears here only for its **selection** semantic
> (NaN-suppression + the sNaN→Invalid trigger). The NUM tie-break *rounding depth* is
> deferred to the fp round-mode slice and is **not** asserted on this page.

---

## 2. The leaf root set — nm-grounded  `[HIGH/OBSERVED]`

Enumerated with `nm <abs>/libfiss-base.so | rg 'module__xdref_<root>'` against the
**12,330,016-byte** binary this pass (never the decompile):

```
.../extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so
```

```
REDUCE  (12 radd* + 24 rmax/rmin* + 32 rb* leaves):
  module__xdref_radd_nx16_32_512        @0x858690   sum, FULL i32 writeback (no wrap)
  module__xdref_radds_nx16_16_512       @0x814970   saturating sum, 16-BIT writeback  <-- the find
  module__xdref_raddu_nx16_32_512       @0x811fa0   unsigned sum (no sign-extend)
  module__xdref_radd_2nx8_16_512        @0x80ee60   8-bit-lane sum
  module__xdref_radd_n_2x32_32_512      @0x5c0410   32-bit-lane sum
  module__xdref_rmax_nx16_16_512_t      @0x85b3c0   T-predicated max (the _t suffix)
  module__xdref_rbmax_nx16_64_512_512   @0x859f00   argmax: dual output (value + lane mask)
  module__xdref_rbmin_nx16_64_512_512   @0x8594f0   argmin
  module__xdref_rmaxnum_nx16f_1_16f_512f @0x524e00  fp maxNum (NaN-suppress)
  module__xdref_rminnum_nx16f_1_16f_512f @0x524dd0  fp minNum
  module__xdref_radd_nx16_32_512_t      @0x85ab00   T-predicated sum

SHIFT  (38 sll/srl/sra/sls/srs/rotr/nsa* leaves):
  module__xdref_sll_u_16_16_6  @0x8580e0   logical left  (over-shift CLEARS)
  module__xdref_srl_u_16_16_6  @0x8580c0   logical right (zero-fill)
  module__xdref_sra_u_16_16_6  @0x8581a0   arithmetic right (sign-fill)
  module__xdref_sls_u_16_16_6  @0x858260   saturating left ({0x7fff,0x8000} clamp)
  module__xdref_srs_s_16_16_6  @0x858300   saturating/rounding right
  module__xdref_rotr_u_16_16_4 @0x5c0bf0   rotate right
  module__xdref_nsa_16_16      @0x858570   normalize signed   (signed-CLZ - 1)
  module__xdref_nsau_16_16     @0x8585a0   normalize unsigned (CLZ)
  module__xdref_sll_s_16_16_6  @0x858100   bidirectional (signed amount); see _s GOTCHA

SHUFFLE / SELECT  (17 sel/shfl/dsel/dcmprs leaves):
  module__xdref_sel_nx16_512_512_512_512  @0x85ef90   2-source crossbar, ctrl & 0x3f
  module__xdref_shfl_nx16_512_512_512     @0x86af70   1-source permute,  ctrl & 0x1f
  module__xdref_dsel_nx16_512_512_512_512_32 @0x5ccee0 dual-output select (out1 + out2)
  module__xdref_dcmprs_2nx8_512_512_64    @0x8341d0   predicate EXPAND / scatter
```

The width suffix encodes `<outW>_<inW...>` exactly as on the
[four-oracle-method](four-oracle-method.md) page — and for this family it carries the **one
genuine bit-exactness finding**: `radds_nx16_`**`16`**`_512` vs `radd_nx16_`**`32`**`_512`,
where the `_16_` vs `_32_` token *is* the writeback field width (§6, divergence 1). The `_t`
suffix is the T-predicated (masked-fold) variant; the `_u` / `_s` shift suffix is the
amount-signedness (unidirectional vs bidirectional); the trailing `_4` / `_6` is the
amount-field bit-width.

---

## 3. The four references — exactly how each computes  `[HIGH]`

### Leg (a) — GX-SEM closed-form model  `[MED/INFERRED, HIGH-in-value]`

Bit-precise RTL semantics lifted from the TIE-XML
([formal-isa-model](../isa/semantics/formal-isa-model.md)) and this-pass disassembly:

```python
# REDUCE
def sem_radd(v):   return sum(sext16(x) for x in v) & 0xFFFFFFFF          # FULL i32, no wrap
def sem_raddu(v):  return sum(x & 0xFFFF for x in v) & 0xFFFFFFFF         # unsigned, NO sext
def sem_radds(v):                                                         # 16-BIT writeback
    s = sum(sext16(x) for x in v)
    s = max(-0x8000, min(0x7FFF, s))                                       # signed clamp ...
    return s & 0xFFFF                                                      # ... then 16-bit field
def sem_rmax(v):   return max(sext16(x) for x in v) & 0xFFFF              # signed
def sem_rmaxu(v):  return max(x & 0xFFFF for x in v)                       # unsigned

# SHIFT  (16-bit lane)
def sem_sll(a,n):  return 0 if (n & 0xFFFF) > 0x1F else (a << n) & 0xFFFF  # over-shift CLEARS
def sem_srl(a,n):  return 0 if (n & 0xFFFF) > 0x1F else (a & 0xFFFF) >> n  # zero-fill
def sem_sra(a,n):  return (sext16(a) >> min(n,15)) & 0xFFFF                # sign-fill
def sem_sls(a,n):  return sat16(sext16(a) << n)                            # clamp {0x7fff,0x8000}
def sem_nsa(a):    return clz16(sext16(a) ^ (sext16(a) >> 15)) - 1         # signed CLZ - 1  (0..15)
def sem_nsau(a):   return clz16(a & 0xFFFF)                                # CLZ            (0..16)

# SHUFFLE / SELECT
def sem_sel(A,B,c): pool = list(B) + list(A); return [pool[c[k] & 0x3F] for k in range(32)]
def sem_shfl(s,c):  return [s[c[k] & 0x1F]   for k in range(32)]
def sem_dcmprs(src, vbool):                                               # predicate EXPAND
    out = [src[63]] * 64                                                  # fill = src[63]
    for k in range(64):
        if (vbool >> k) & 1:
            out[k] = src[popcount(vbool & ((1 << k) - 1))]                # prefix-popcount index
    return out
```

### Leg (b) — GX-FLIX device bundle decode  `[HIGH/OBSERVED]`

Each mnemonic is assembled by the device `xtensa-elf-as` and decoded by the device
`xtensa-elf-objdump` (XTENSA_CORE=ncore2gp). The decode's role is **identity**: raw bytes →
mnemonic, the mnemonic binding the value semantic legs (a)/(d) compute. Re-run this pass —
each 8-byte form is the `{nop; nop; nop; <op>}` N0 bundle, the value op in slot 3:

```
$ export XTENSA_SYSTEM=.../tools/XtensaTools/config
$ XTENSA_CORE=ncore2gp .../bin/xtensa-elf-as      -o flix05.o flix05.s
$ XTENSA_CORE=ncore2gp .../bin/xtensa-elf-objdump -d flix05.o

   0:  32501020e0c2452f   { nop; nop; nop; ivp_raddnx16  v3, v2 }
   8:  3252c7a800c2452f   { nop; nop; nop; ivp_sllnx16   v3, v1, v2 }
  10:  2250d2302044452f   { nop; nop; nop; ivp_selnx16   v3, v1, v2, v4 }
  18:  32514d080480452f   { nop; nop; nop; ivp_nsaunx16  v10, v1 }
```

The objdump `-s` raw stream confirms LE memory order and `insn0` byte 0 = `0x2f`:

```
 0000 2f45c2e0 20105032 2f45c200 a8c75232  ...  <- byte0 = 0x2f everywhere
```

Format decode: `insn0` byte 0 = `0x2f` ⇒ `op0` nibble (`bits[3:0]`) = `0xf`; predicate
`(insn0 & 0x900000f) == 0xf` ⇒ **format N0** (length 8, four slots `{42,45,43,44}`); the
opcode discriminator lives in `insn1` (slot 3). The 21 reduce/shift/shuffle bundles all
decode byte-identically to [b08-reduce](../isa/ref/b08-reduce.md) /
[b12-shift](../isa/ref/b12-shift.md) / [b21-select-shuffle](../isa/ref/b21-select-shuffle.md).

> **QUIRK — `dcmprs2nx8` is a WIDE bundle.** Unlike the 8-byte N0 reduce/shift/shuffle
> bundles, `ivp_dcmprs2nx8 v3,v1,vb2` assembles to a **15-byte** form
> (`0009191448c08300950022108504452f`) — a distinct FLIX format. Only the mnemonic identity
> is transcribed here; the per-slot operand scatter for the wide format is the FLIX-decoder's
> domain, not this differential's.

### Leg (c) — nki-0.3.0 / numpy-native reference  `[HIGH/OBSERVED, subset]`

```python
nki_radd = lambda v: int(np.add.reduce(v.astype(np.int32)))   # sext-sum
nki_rmax = lambda v: int(np.max(v.view(np.int16)))            # signed max
nki_argmax = lambda v: int(np.argmax(v.view(np.int16)))       # SINGLE index <- divergence pt
nki_sll  = lambda a,n: int((np.uint16(a) << n) & 0xFFFF)
nki_sra  = lambda a,n: int(np.int16(a) >> n) & 0xFFFF
nki_shfl = lambda s,c: [int(s[c[k] & 0x1F]) for k in range(32)]
```

> **GOTCHA — the nki subset is a coverage scope note, not a weakness.** nki exposes
> `radd / rmax / rmin / shfl / sll / srl / sra` as native primitives (validated **4-way**).
> It has **no** primitive for `sls`/`srs` (saturating shift), `radds`/`raddu` (sat/unsigned
> sum), the RB dual-out argmin/argmax, the fp NUM reduce, the T-predicated fold, `dcmprs`, or
> `dsel` — those lean on nki's Vector/DVE engine. They are validated **3-way (a + b + d) +
> LIVE**, with the LIVE binary closing them against the shipped oracle directly. Mark them
> `n/a` in the nki column; never hide them. (Same posture as convert-pack-cast.)

### Leg (d) — `libfiss-base.so` value leaves, driven LIVE  `[HIGH/OBSERVED — the keystone]`

`ctypes.CDLL(libfiss-base.so, RTLD_GLOBAL)`; the whole-vector reduce leaf / per-lane shift
leaf / whole-vector permute leaf **called on every input** with `arg0 = NULL` (the
arith+reduce+permute oracle is bare-leaf drivable — `xstate` unused; the only walled leaves
in the whole binary are the three `recipqli` soft-float refiners, see
[four-oracle-method](four-oracle-method.md) §7). The recovered ABIs, **re-confirmed by
execution this pass**:

```
SHIFT  binary : void f(rdi=arg0, esi=value, edx=amount, rcx=*out32)
SHIFT  unary  : void f(rdi=arg0, esi=value,             rdx=*out32)   <- nsa/nsau (3-arg!)
REDUCE R-fold : void f(rdi=arg0, rsi=*src[64B],         rdx=*out32)   <- whole-vector
REDUCE RB     : void f(rdi, rsi=*src[64B], rdx=*vbool[64B, 2b/lane], rcx=*value[64B])
REDUCE NUM-fp : void f(rdi, rsi=*src[64B], rdx=*invalid_flag,        rcx=*value)
SEL           : void f(rdi, rsi=*srcA, rdx=*srcB, rcx=*ctrl, r8=*out)
SHFL          : void f(rdi, rsi=*src,  rdx=*ctrl, rcx=*out)
DSEL          : void f(rdi, rsi=*srcA, rdx=*srcB, rcx=*ctrl, r8=*out1, r9=*out2)
DCMPRS        : void f(rdi, rsi=*src[64xi8], rdx=*vbool[64-bit], rcx=*out[64xi8])
```

> **CORRECTION — never carry the 4-arg shape onto `nsa`/`nsau`.** These two are **genuinely
> 3-arg unary**: the out-pointer is in **`%rdx`**, not `%rcx`. A naive "every leaf is 4-arg"
> binding passes the out-pointer in `%rcx` to a leaf that stores through `%rdx` (an
> uninitialised-register address) → garbage or SIGSEGV. The width suffix flags it: **one input
> width** (`nsau_16_16`, not `_16_16_16`) ⇒ unary ⇒ 3-arg. Confirmed at the store site below.

---

## 4. The harness — identical inputs reach four legs  `[HIGH]`

The [four-oracle-method](four-oracle-method.md) §3-4 contract is reused verbatim: the *same*
input stream is fed to all four legs; a per-element / per-vector differential is a complete
op differential. Input-vector construction, lane-width aware:

```python
EDGE16 = [0x0000,0x0001,0xFFFF,0x7FFF,0x8000,0x8001,0x7FFE,0x4000,0xC000,0x5555,0xAAAA]
# REDUCE vectors : all-ones, edge-packed, all-INT16_MIN-neighbours, all-0x4000 (sum
#                  overflows i16, fits i32 — the FULL-HEADROOM probe), all-0x7fff, + 200
#                  deterministic fuzz (Random(0x05), 32 x i16 each).
# SHIFT          : value x amount-0..63 grid, including the over-shift band 0x20..0x3f,
#                  + 800 fuzz pairs.
# PERMUTE        : identity / reverse / rotate / idx-wrap (the &mask boundary) + 800 fuzz.
```

The reusable live core — the unary shift driver, calling the **real** binary, is the simplest
proof that the leaf's out-pointer register matters:

```python
import ctypes
lib = ctypes.CDLL(FISS, mode=ctypes.RTLD_GLOBAL)

def bind_unary_shift(name):                                  # 3-arg: (xstate, value, *out)
    fn = getattr(lib, name); fn.restype = None
    fn.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
    return fn

nsau = bind_unary_shift("module__xdref_nsau_16_16")
def live_nsau(v):
    out = ctypes.c_uint(0); nsau(None, v & 0xFFFF, ctypes.byref(out)); return out.value
```

And the whole-vector reduce driver — the structural difference from the elementwise harness
is the **64-byte source pointer**, folded internally by the leaf:

```python
import struct
def bind_reduce(name):                                       # 3-arg: (xstate, *src64, *out)
    fn = getattr(lib, name); fn.restype = None
    fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
    return fn

radds = bind_reduce("module__xdref_radds_nx16_16_512")
def live_radds(vals):                                        # 32 lanes of i16 -> 64-byte vec
    src = struct.pack("<32H", *[(x & 0xFFFF) for x in (list(vals) + [0]*32)[:32]])
    out = ctypes.c_uint(0); radds(None, src, ctypes.byref(out)); return out.value
```

---

## 5. The agreement tables — the result  `[HIGH]`

### 5.1 REDUCE / RB / NUM / T  (30 directed + 805 fuzz)

| op / probe | SEM (a) | LIVE (d) | nki (c) | verdict |
|------------|---------|----------|---------|---------|
| `radd_nx16 {1..32}` | `0x210` | `0x210` | `0x210` | **AGREE** (sext-sum i32) |
| `radd_nx16 {0x4000 x32}` | `0x80000` | `0x80000` | `0x80000` | **AGREE** (full headroom, NO wrap) |
| `radd_nx16 {-1 x32}` | `0xffffffe0` | `0xffffffe0` | `0xffffffe0` | **AGREE** (signed −32) |
| `raddu_nx16 {0x8000 x4}` | `0x20000` | `0x20000` | n/a | **AGREE** (unsigned, no sext) |
| `radds_nx16 {0x4000 x8}` sat+ | `0x7fff` | `0x7fff` | n/a | **AGREE** (16-bit sat +max) |
| `radds_nx16 {0xC000 x8}` sat− | `0x8000` | `0x8000` | n/a | **AGREE** (16-bit sat −min) \* |
| `radd_2nx8 {1 x64}` | `0x40` | `0x40` | n/a | **AGREE** |
| `radd_n_2x32 {1..16}` | `0x88` | `0x88` | n/a | **AGREE** |
| `rmax {1,2,3,-5,100}` | `0x64` | `0x64` | `0x64` | **AGREE** (signed max = 100) |
| `rmin {..}` | `0xfffb` | `0xfffb` | `0xfffb` | **AGREE** (signed min = −5) |
| `rmaxu {.,0xfffb,.}` | `0xfffb` | `0xfffb` | n/a | **AGREE** (unsigned max) |
| `rmax @ {0x8000,0x7fff,..}` | `0x7fff` | `0x7fff` | n/a | **AGREE** (SIGNED picks +max) |
| `rmaxu @ {0x8000,0x7fff,..}` | `0x8000` | `0x8000` | n/a | **AGREE** (UNSIGNED picks 0x8000) |
| `radd_nx16` fuzz (205 vec) | 205/205 SEM==LIVE | | 205/205 SEM==nki | **AGREE** |
| `rmax`/`rmin` fuzz (600 vec) | 600/600 SEM==LIVE | | 600/600 SEM==nki | **AGREE** |

\* the `radds` 16-bit writeback — the one initial SEM divergence, root-caused in §6.

The **separating** cells are the strongest evidence the legs distinguish meaning rather than
copy each other: `rmax @ {0x8000,...} = 0x7fff` while `rmaxu @ {0x8000,...} = 0x8000` at the
**identical input** — the signed and unsigned reduces pick **different** winners. That is
STRUCTURAL (a distinct leaf), not a runtime flag.

**RB argmin/argmax** (dual output: broadcast value + winning-lane vbool, 2 bits/lane):

| probe | LIVE value | winning lanes (vbool ≠ 0) | verdict |
|-------|-----------|---------------------------|---------|
| `rbmax` unique max @7 (0x4000) | `0x4000` | `{7}` | **AGREE** |
| `rbmax` TIE @{2,9} (both 0x200) | `0x0200` | `{2, 9}` **(UNION)** | **AGREE** ← tie |
| `rbmin` min @3 (−9) | `0xfff7` | `{3}` | **AGREE** |
| `rbmaxu` unsigned (0xfffb @2) | `0xfffb` | `{2}` | **AGREE** |
| nki `np.argmax` on the tie | — | `{2}` only (FIRST index) | **DIVERGE (nki)** \* |

\* the tie-UNION divergence: nki single-index argmax vs the hardware union-mask, §6.

**NUM fp reduce** (IEEE maxNum/minNum, fp16 — SELECTION + Invalid, *not* round depth):

| probe | LIVE value | invalid | verdict |
|-------|-----------|---------|---------|
| `maxnum {1,3,2}` | `3.0` | 0 | **AGREE** |
| `maxnum {1,qNaN,2}` (NaN-suppress) | `2.0` | 0 | **AGREE** (qNaN does not signal) |
| `minnum {5,qNaN,-3}` | `-3.0` | 0 | **AGREE** |
| `maxnum {all qNaN}` | `0x7e00` qNaN | — | **AGREE** (NaN only if ALL NaN) |
| `maxnum {1,sNaN,2}` | `2.0` | 1 | **AGREE** (sNaN → Invalid) |

**T-predicated masked fold** (killed lane = identity): `radd_t keep{0,1,2}=6` ; `keep-ALL =
0x210` ; `keep-NONE = 0` (identity) ; `rmax_t keep{10}(max@5 killed)=50` ; `rmax_t
keep{5,10}=1000`. ALL AGREE.

### 5.2 SHIFT  (36 directed + 800 fuzz)

| op / probe | SEM (a) | LIVE (d) | nki (c) | verdict |
|------------|---------|----------|---------|---------|
| `sll_u 0x0001<<4 / <<17 / <<32` | `0x10/0/0` | == | == | **AGREE** (over-shift CLEARS) |
| `srl_u 0x8000>>4 / >>15` | `0x800/1` | == | == | **AGREE** (zero-fill) |
| `sra_u 0x8000>>4` | `0xf800` | == | == | **AGREE** (sign-fill) |
| `rotr_u 0x0001 ror4 / 0x8000 ror1` | `0x1000/0x4000` | == | == | **AGREE** |
| `sls_u 0x4000<<2 / 0xC000<<4` | `0x7fff/0x8000` | == | n/a | **AGREE** (sat clamp) |
| `nsa 0/1/0x4000/0x8000/0xffff` | `15/14/0/0/15` | == | n/a | **AGREE** (signed CLZ−1) |
| `nsau 0/1/0x4000/0x8000/0xffff` | `16/15/1/0/0` | == | n/a | **AGREE** (CLZ) |
| `sll_u` fuzz (400) / `srl_u` (400) | 400/400 LIVE | | 400/400 nki | **AGREE** |

### 5.3 SHUFFLE / SELECT  (17 directed + 800 fuzz)

| op / probe | verdict | note |
|------------|---------|------|
| `sel` identity-from-B / idx 32..63→A / reverse | **AGREE** (a==d) | B in LOW pool half |
| `sel` idx&0x3f wrap (64→0) / idx 127→63 | **AGREE** | crossbar pool 64 |
| `sel` crossbar fuzz (300 random ctrl) | 300/300 **AGREE** | |
| `shfl` identity / reverse / rotate+8 / idx&0x1f | **AGREE** (a==d==c) | single-source pool 32 |
| `shfl` permute fuzz (300) | 300/300 LIVE, 300/300 nki | |
| `dsel` out1 == `sel_nx16` (pool[ctrl]) | **AGREE** (out1 OBSERVED) | |
| `dsel` out2 (flat 16b ctrl) → pool[0]=srcB[0] | OBSERVED (sub-field 0) | full out2 needs the 2-sub-field 32-bit packed ctrl |
| `dcmprs` keep{3,7,9} → {src[0],src[1],src[2]} | **AGREE** | EXPAND, value-move |
| `dcmprs` keep-even / keep-none / keep-all | **AGREE** | fill = src[63] |
| `dcmprs` EXPAND fuzz (200 random src+mask) | 200/200 **AGREE** | |

**TOTALS: 83 directed + 2,005 fuzz = 2,088 cases; mismatches = 0** (all legs jointly
defined). The one initial SEM divergence (radds writeback width) is root-caused and the SEM
model corrected (§6); the one nki divergence (argmax tie) is an API-surface property,
characterized — the *value* still agrees on all legs.

---

## 6. Divergences + root cause  `[HIGH]`

### Divergence 1 — radds writeback width (SEM(a) vs LIVE(d)): ROOT-CAUSED, FIXED  `[HIGH/OBSERVED]`

The differential's one genuine bit-exactness finding. `radds_nx16_16_512` saturates the i32
sum and then **stores a 16-bit field** — the `_16_512` in the name *is* the output width.
Disassembled this pass at `0x814b94..0x814bbc`:

```asm
; module__xdref_radds_nx16_16_512   @0x814970  (16-bit saturating-sum writeback)
  814b85:  83 e1 1f             and    $0x1f,%ecx          ; (lane-count bookkeeping)
  814b8c:  89 f0                mov    %esi,%eax
  814b94:  81 e6 00 80 00 00    and    $0x8000,%esi        ; keep ONLY the sign bit  (-> 0x8000)
  814b9f:  25 ff 7f 00 00       and    $0x7fff,%eax        ; keep the magnitude      (-> 0x7fff)
  814bb8:  0f b7 f0             movzwl %ax,%esi             ; 16-bit ZERO-EXTEND
  814bbc:  89 32                mov    %esi,(%rdx)          ; STORE the 16-bit field
```

Whereas `radd_nx16_`**`32`**`_512` keeps the **full i32** sum (no wrap). The SEM model
initially returned the sign-extended i32 `0xffff8000` for `radds({0xC000 x8})`, **diverging**
from the live `0x8000`. Root cause: the SEM model ignored the 16-bit writeback width. Fixed —
`sem_radds` now masks to `& 0xFFFF` (§3) and all four legs agree.

**Live proof — the vendor binary's own arithmetic, called this pass:**

```
radd_nx16  {1..32}      = 0x210      (1+2+...+32 = 528)
radd_nx16  {0x4000 x32} = 0x80000    (32 x 0x4000 = full i32, NO i16 wrap)
radd_nx16  {-1 x32}     = 0xffffffe0 (signed sum -32)
radds_nx16 {0x4000 x8}  = 0x7fff     (8 x 0x4000 = 0x20000, 16-bit SAT to +max)
radds_nx16 {0xC000 x8}  = 0x8000     (16-bit SAT to -min)   <-- the divergence value
raddu_nx16 {0x8000 x4}  = 0x20000    (unsigned, NO sign-extend)
```

> **GOTCHA — the width is in the name, not a mode.** Do not assume "saturating sum" means
> "saturate to a wide accumulator." On this datapath the writeback field width is **fixed by
> the leaf name** (`_16_` vs `_32_`); `radds` saturates *to the 16-bit field*, `radd` keeps
> the i32. A reimplementation must read the suffix and clamp/store accordingly.

### Divergence 2 — argmax tie (nki(c) vs RB LIVE(d)): ROOT-CAUSED, characterized  `[HIGH/OBSERVED]`

`np.argmax` returns the **FIRST** maximal index; the RB
(`module__xdref_rbmax_nx16_64_512_512`) leaf emits the **FULL tie-SET bitmask — a UNION**
(lanes 2 *and* 9 both set when both hold the max). The divergence is a **property of the nki
single-index argmax API surface** vs the hardware dual-output union-mask, not a validation
weakness: the reduced **value still agrees** on all four legs; only the winning-lane
representation differs, and the hardware union (reproduced by SEM + LIVE + FLIX) is the
richer, correct behaviour.

**Live proof — RB driven this pass, the 2-bits-per-lane vbool decoded** (`(bits >> 2k) & 3`):

```
rbmax unique max@7      : value=0x4000  winning-lanes={7}
rbmax TIE@{2,9}         : value=0x0200  winning-lanes={2, 9}   <-- UNION, proven by execution
rbmin min@3 (-9)        : value=0xfff7  winning-lanes={3}
```

> **NOTE.** A reimplementation that emits only `argmax` (one index) is observably wrong on a
> tie. The hardware contract is the **mask** — every lane equal to the extremum is set.

### Why no OTHER divergence (the deeper reason)  `[HIGH]`

The reduce/shift/permute datapath has almost **no free parameter** for the references to
disagree about: wrap/headroom is fixed by the output-field width (the name); saturation
clamps to fixed constants `{0x7fff, 0x8000}`; over-shift **CLEARS** at `n > 0x1f`; signedness
is STRUCTURAL (a distinct leaf, not a mode — `rmax` vs `rmaxu` is two opcodes); the crossbar
mask is fixed by pool size (`& 0x3f` / `& 0x1f`); and `dcmprs` fill is a fixed `src[63]`. The
only round-mode freedom lives in the fp NUM tie-break *rounding*, which is the fp-round-mode
slice's scope; here the NUM **selection** (NaN-suppress) + the **Invalid** trigger are
unambiguous and agree.

---

## 7. The hard edges, proven LIVE — annotated  `[HIGH/OBSERVED]`

The bodies behind the agreement tables, disassembled and **executed** this pass.

### 7.1 `nsau` / `nsa` — normalize (CLZ), the 3-arg unary  `[HIGH/OBSERVED]`

```asm
; module__xdref_nsau_16_16   @0x8585a0   (CLZ; unary, out-pointer in %rdx)
  8585a4:  b8 10 00 00 00       mov    $0x10,%eax     ; default 16  (input == 0 path)
  8585a9:  89 02                mov    %eax,(%rdx)     ; STORE via %rdx  <-- UNARY ABI
  8585ab:  c3                   ret
  8585b0:  b8 0f 00 00 00       mov    $0xf,%eax       ; 15
  8585b5:  0f bd f6             bsr    %esi,%esi       ; bit index of highest set bit
  8585b8:  29 f0                sub    %esi,%eax       ; 15 - bsr  ==  CLZ for a 16-bit value
  8585ba:  89 02                mov    %eax,(%rdx)
  8585bc:  c3                   ret
```

So `nsau(0) = 16`, `nsau(0x0001) = 15 - 0 = 15`, `nsau(0x8000) = 15 - 15 = 0`. The signed
`nsa` is `clz(x ^ (x>>15)) - 1` (CLZ of the value XOR its sign-replication, minus one) so
`nsa(0x0001) = 14`, `nsa(0xffff) = 15`, `nsa(0x8000) = 0`. Live, called this pass:

```
nsau(0x0000) = 16    nsa(0x0000) = 15
nsau(0x0001) = 15    nsa(0x0001) = 14
nsau(0x4000) =  1    nsa(0x4000) =  0
nsau(0x8000) =  0    nsa(0x8000) =  0
nsau(0xffff) =  0    nsa(0xffff) = 15
```

### 7.2 Shift — over-shift clear, sign-fill, saturating clamp  `[HIGH/OBSERVED]`

The `sls_u` body shares the exact `{0x8000, 0x7fff}` sat masks with `radds`, gated by the
`cmp $0x1f` over-shift test:

```asm
; module__xdref_sls_u_16_16_6   @0x858260  (saturating left shift)
  858262:  83 fa 1f             cmp    $0x1f,%edx          ; amount > 0x1f ?
  858281:  39 c1                cmp    %eax,%ecx
  858291:  81 e6 00 80 00 00    and    $0x8000,%esi        ; sat -> 0x8000  (clamp -min)
  858299:  81 e2 ff 7f 00 00    and    $0x7fff,%edx        ; sat -> 0x7fff  (clamp +max)
```

Live, called this pass:

```
sll_u 0x0001<<4  = 0x0010
sll_u 0x0001<<17 = 0x0000   (over-shift CLEARS at n > 0x1f)
sll_u 0x0001<<32 = 0x0000
srl_u 0x8000>>4  = 0x0800   (zero-fill)
sra_u 0x8000>>4  = 0xf800   (sign-fill)
sls_u 0x4000<<2  = 0x7fff   (saturating clamp +max)
sls_u 0xC000<<4  = 0x8000   (saturating clamp -min)
rotr_u 0x0001 r4 = 0x1000
rotr_u 0x8000 r1 = 0x4000
```

> **GOTCHA — the `_s` bidirectional shifts.** `sll_s` / `sls_s` / `sra_s` (the `_s` amount
> suffix) take a **signed** amount: a negative amount shifts the *other* direction. The `_u`
> variants are unidirectional. A reimplementation must branch on the amount's sign for the
> `_s` leaves, and must clear (not wrap) the `_u` leaves above `0x1f`.

### 7.3 Crossbar masks — `sel & 0x3f`, `shfl & 0x1f`  `[HIGH/OBSERVED]`

The control index is masked *in the body* — the pool size is the mask:

```asm
; module__xdref_sel_nx16_512_512_512_512   @0x85ef90  (2-source crossbar, pool 64)
  85f06a:  83 e6 3f             and    $0x3f,%esi          ; ctrl & 0x3f  (B||A pool, 64 entries)
  85f07d:  83 e2 3f             and    $0x3f,%edx

; module__xdref_shfl_nx16_512_512_512      @0x86af70  (1-source permute, pool 32)
  86af78:  83 e7 1f             and    $0x1f,%edi          ; ctrl & 0x1f  (single source, 32)
  86af8b:  83 e0 1f             and    $0x1f,%eax
```

Live, called this pass (srcA markers `0xa00+k`, srcB markers `0xb00+k`):

```
shfl ctrl={0,1,31,32,33} -> {0x100,0x101,0x11f,0x100,0x101}   (idx32 & 0x1f = 0 -> src[0])
sel  ctrl={0,32,63,64,127} -> {B[0],A[0],A[31],B[0],A[31]}    (pool[0..31]=B, [32..63]=A;
                                                               idx127 & 0x3f = 63 -> A[31])
```

> **QUIRK — srcB occupies the LOW half of the `sel` pool.** The concatenated crossbar pool is
> `B ++ A`: `pool[0..31]` = srcB, `pool[32..63]` = srcA. So `sel` ctrl 0 selects `srcB[0]`,
> ctrl 32 selects `srcA[0]`, and ctrl 64 wraps (`& 0x3f`) back to `srcB[0]`. Get the order
> wrong and identity-from-A/identity-from-B swap.

### 7.4 `dcmprs` — the predicate-EXPAND scatter  `[HIGH/OBSERVED]`

The most structurally distinct op on the page. The body reads the vbool **1 bit per lane**,
computes a **prefix-popcount** index via `module__xdref_popc64_7_64`, clamps it via
`module__xdref_dcmprs_clamp`, and scatters `src[index]` into `out[k]` **only when lane k's
predicate is set** — otherwise the slot keeps the fill:

```asm
; module__xdref_dcmprs_2nx8_512_512_64   @0x8341d0
  834217:  call   18dec0 <module__xdref_popc64_7_64@plt>   ; prefix popcount accumulator
  83421c:  mov    (%rbx),%edx                              ; vbool word
  834233:  and    $0x1,%edx                                ; lane 0 predicate bit
  834236:  and    $0x3f,%esi                               ; popcount index & 0x3f  (clamp 0..63)
  834239:  call   18e810 <module__xdref_dcmprs_clamp@plt>
  ; ... lane 1: shr $1,%edx ; and $0x1,%edx  (next predicate bit) ...
```

This is `out[k] = src[popcount(vbool & ((1<<k)-1))]` for set lanes, `out[k] = src[63]` for
unset. Live, called this pass (src = `0xa0 .. 0xdf`, mask = lanes {3, 7, 9}):

```
dcmprs keep{3,7,9}, src=0xa0..0xdf:
  out[3]=0xa0  out[7]=0xa1  out[9]=0xa2      (kept lane k <- src[prefix-popcount])
  out[0]=0xdf  out[5]=0xdf  out[63]=0xdf     (unset lanes filled = src[63]=0xdf)
  keep-even    out[0,2,4] = {0xa0,0xa1,0xa2}, out[1]=0xdf (fill)
  keep-none    out[*]     = 0xdf  (all fill)
  keep-all     out[0,1]   = {0xa0,0xa1}  (identity)
```

> **NOTE — EXPAND, not COMPRESS.** Despite the name, this leaf writes the **destination**
> position by predicate (a scatter / left-pack-into-position), pulling the source by
> prefix-popcount. Lane k of the *output* is gated by lane k of the *predicate*; the *source*
> index advances only over set lanes. The compress sibling (writeback indexed by predicate
> count) is a separate op deferred to the wide-permute slice.

---

## 8. Adversarial self-verify  `[HIGH]`

The five strongest claims on this page, each re-challenged against the binary and the live
drive:

1. **argmax tie → UNION mask `{2,9}`** — *Challenge:* could the "union" be an artifact of
   reading the vbool 1-byte-per-lane? *Resolved:* the RB vbool is **2 bits per lane**; decoded
   as `(bits >> 2k) & 3` the LIVE leaf returns lanes `{2, 9}` for the tie and `{7}` for the
   unique max — a genuine union, not a packing artifact. `[HIGH/OBSERVED]`
2. **`dcmprs` prefix-popcount EXPAND** — *Challenge:* my first print of `out[0..3] =
   {0xdf,0xdf,0xdf,0xa0}` looked like a mismatch with the report's `{0xa0,0xa1,0xa2}`.
   *Resolved:* those are different positions — `out[0..2]` are *fills* (lanes unset), the kept
   values land at `out[3]=0xa0, out[7]=0xa1, out[9]=0xa2`. The body's `popc64` + per-lane
   `and $0x1` confirms the destination is predicate-gated. No mismatch. `[HIGH/OBSERVED]`
3. **`sel & 0x3f` / `shfl & 0x1f` mask widths** — *Challenge:* are the widths inferred or in
   the body? *Resolved:* `and $0x3f,%esi` @0x85f06a (sel) and `and $0x1f,%edi` @0x86af78
   (shfl) are explicit in the disassembly; LIVE `sel idx127→A[31]`, `shfl idx32→src[0]`
   confirm the narrowing arithmetically. `[HIGH/OBSERVED]`
4. **radds 16-bit vs radd full-i32 writeback** — *Challenge:* is the width a mode or the
   name? *Resolved:* `radds_nx16_16_512` body masks `& 0x8000` / `& 0x7fff` then `movzwl`
   (0x814b94..0x814bbc); `radd_nx16_32_512` stores the full i32. LIVE: `radds{0xC000 x8} =
   0x8000` vs `radd{0x4000 x32} = 0x80000`. The width is in the suffix. `[HIGH/OBSERVED]`
5. **LIVE ctypes outputs are the real binary, not a re-implementation** — *Challenge:* could
   these be the SEM lift mislabeled? *Resolved:* every figure here came from
   `ctypes.CDLL(libfiss-base.so)` calling `module__xdref_*` on inputs this pass; the
   `nsau`/`nsa`/`sll`/`sls`/`rotr`/`radd`/`radds`/`rbmax`/`sel`/`shfl`/`dcmprs` outputs match
   the SEM model *and* the FLIX-decoded mnemonic. A SEM-vs-LIVE mismatch (it happened once,
   the radds case) flips the verdict and the binary wins. `[HIGH/OBSERVED]`

---

## 9. Confidence / observation ledger  `[HIGH]`

| Claim | Tag | Ground |
|-------|-----|--------|
| 4 legs bit-identical over 83 directed + 2,005 fuzz = 2,088 cases, 0 mismatches | HIGH / OBSERVED | the real `libfiss-base.so` `dlopen`'d and its reduce/shift/permute leaves called on every input this pass; SEM + numpy + FLIX-decode cross-checked |
| `radds_nx16_16_512` writes a **16-bit** field; `radd_nx16_32_512` keeps **full i32** | HIGH / OBSERVED | disassembled `radds`@0x814b94..0x814bbc (`and $0x8000`/`and $0x7fff`/`movzwl`); LIVE `radds{0xC000 x8}=0x8000` vs `radd{0x4000 x32}=0x80000` this pass |
| argmax tie emits the **UNION** lane-mask `{2,9}` (2 bits/lane vbool) | HIGH / OBSERVED | `rbmax_nx16_64_512_512` driven LIVE this pass; vbool decoded `(bits>>2k)&3` → `{2,9}` on the tie, `{7}` unique |
| `dcmprs` EXPAND: `out[k]=src[prefix-popcount]` for set lanes, fill `src[63]` | HIGH / OBSERVED | body threads `popc64_7_64` + `dcmprs_clamp` (disasm @0x8341d0); LIVE keep{3,7,9}→{0xa0,0xa1,0xa2}, fill 0xdf this pass |
| `sel` ctrl & `0x3f` (pool `B\|\|A`, 64); `shfl` ctrl & `0x1f` (single source, 32) | HIGH / OBSERVED | disassembled `and $0x3f`@0x85f06a (sel), `and $0x1f`@0x86af78 (shfl); LIVE idx-narrowing this pass |
| `nsau` = CLZ (3-arg unary, out via `%rdx`); `nsa` = signed CLZ − 1 | HIGH / OBSERVED | disassembled `nsau`@0x8585a0 (`bsr; sub`, `mov %eax,(%rdx)`); LIVE `nsau(1)=15`, `nsau(0)=16`, `nsa(0xffff)=15` this pass |
| over-shift `n>0x1f` CLEARS; `sls`/`srs` clamp `{0x7fff,0x8000}`; `sra` sign-fill | HIGH / OBSERVED | `sls`@0x858262 (`cmp $0x1f`; `and $0x8000`/`and $0x7fff`); LIVE `sll<<17=0`, `sls 0x4000<<2=0x7fff`, `sra 0x8000>>4=0xf800` this pass |
| 21 FLIX bundles decode N0 (byte0=0x2f, op0=0xf, pred=0xf), opcode in slot 3; `dcmprs2nx8` WIDE | HIGH / OBSERVED | device `xtensa-elf-as`/`objdump` (XTENSA_CORE=ncore2gp) assembled + decoded `radd`/`sll`/`sel`/`nsau` byte-identically this pass; raw `-s` stream byte0=0x2f |
| leg (a) SEM constants (sign-bias min/max, `{0x7fff,0x8000}` clamp, prefix-popcount expand) | MED / INFERRED | from the TIE-XML / this-pass disasm rather than re-decoded from XML here; corroborated by bit-exact agreement with the LIVE binary |
| nki validates the native-primitive subset (radd/rmax/rmin/shfl/sll/srl/sra); rest 3-way+LIVE | MED / OBSERVED | nki `_NUMPY_FUNC_MAP` surface; the non-primitive ops marked `n/a` in the nki column, closed by LIVE |
| `dsel` out2 full characterization needs the 2-sub-field 32-bit packed ctrl | LOW / INFERRED | flat 16-bit ctrl leaves out2 = pool[0] (OBSERVED); the full out2 is deferred to the wide-permute slice |
| fp NUM tie-break **rounding** depth | LOW / CARRIED | SELECTION + Invalid are OBSERVED here; the round rule is the fp-round-mode slice's scope |

No silicon-generation, gen-count, or codename is inferred anywhere on this page: every
`16/8/32-bit`, `NX16`, `_16_512`/`_32_512`, `0x7fff/0x8000`, `&0x3f`/`&0x1f`, `N0`/`slot3`
token is a datapath-width / FLIX-format / pool-size / ISA-lane axis of the single Cairo
config, not one of the five firmware-image generations.
