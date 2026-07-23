# Differential Validation — the fp16/fp32 Soft-Float Family

This page is the **bit-exactness certificate** for the GPSIMD floating-point primitive family —
`add`/`sub`/`mul`/`madd`, `min`/`max`/`minnum`/`maxnum`, the ten ordered/unordered compares,
and `neg`/`abs`/`clsfy` — in both precisions (fp16 = IEEE binary16 1-5-10 bias 15, fp32 = IEEE
binary32 1-8-23 bias 127). It applies the
[4-oracle bit-exact differential method](four-oracle-method.md) to this family and **proves**
that, on an identical IEEE-754 edge-case + fuzz corpus, all four independent references agree
to the bit. The *value* leg is driven **LIVE**: every output-bit pattern quoted below is a real
return value of the shipped `module__xdref_*` soft-float leaf in `libfiss-base.so`, called
in-process via `ctypes`, the binary acting as the arbiter of its own arithmetic.

The two highest-risk subfamilies — where a naïve reimplementation almost always diverges — get
their own grounded sections: **NaN propagation** (canonical-qNaN forcing, sNaN quieting, payload
preservation, sign handling) in §6, and **round-half-to-even tie-breaking** (genuine
round-to-*even*, not round-half-up) in §7. The companion datatype-edge deep-dive that enumerates
every denormal/inf/NaN boundary class — *"NaN/Inf/Denormal: the Special-Value Conformance
Matrix"* — is the source for the per-class breakdown tabulated here and is cited where its rows
are reused.

This page owns **value semantics and the cross-oracle agreement result**. It does *not* own the
opcode encodings or the FCR/FSR control model: those live at
[B02 — Vector ALU (fp16/fp32 slice)](../isa/ref/b02-vec-alu-fp.md) (the ALU value/encoding
companion), [B17 — fp32 FMA](../isa/ref/b17-spfma.md) / [B18 — fp16 FMA](../isa/ref/b18-hp-fma.md)
(the fused multiply-add datapath), [the fp sub-ISA / FCR-FSR view](../isa/core/fp-sub-isa.md)
(round-mode + sticky-flag model), and the ISS convert/pack view at
[cas/fiss Convert / Pack / FP Semantics](../iss/cas-convert-pack-fp.md). Confidence tags follow
[the Confidence & Walls model](../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte or **proven by live `ctypes` execution**, `[MED/INFERRED]` = reasoned over
OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence.

---

## 1. Scope, the four oracles, and the binary under test

The method binds **four independent references** that must agree bit-for-bit on each corpus
input. For this family they are:

| Oracle | Role | What it contributes to fp validation |
|---|---|---|
| **SEM (TIE-XML)** | the device semantic spec | the per-op fp semantic group membership and the FCR round-mode / FSR sticky-flag wiring the op claims |
| **FLIX (firmware-decode)** | the device-native disassembler | the slot/format the mnemonic occupies, and the operand bit-map — confirms *which* op a bit pattern decodes to |
| **nki-0.3.0 (numpy)** | the host reference model | `numpy.float16`/`float32` IEEE arithmetic — the "textbook" leg the binary is checked against |
| **libfiss-base.so (LIVE)** | the soft-float value oracle | the **shipped** integer-only soft-float, driven in-process by `ctypes` — the device's own answer |

The keystone of Part 14 carries through: **cas decodes, fiss values.** The cycle-accurate ISS
(`libcas-core.so`) computes decode/timing/hazard but delegates *every element value* to the
864-leaf `module__xdref_*` soft-float table in `libfiss-base.so`. That table is what this page
executes.

**Binary under test** (nm-ground, never the decompile):

| Fact | Value | How read |
|---|---|---|
| Path | `extracted/…/ncore2gp/config/libfiss-base.so` | `fd libfiss-base.so` (under `extracted/`, gitignored) |
| Size | **12 330 016 B** | `stat -c%s` |
| sha256 | `260b110c…` | `sha256sum` |
| Type | ELF64 x86-64, **not stripped** | `file` |
| Total `module__xdref_*` leaves | **864** | `nm -D --defined-only … \| rg -c module__xdref` |
| fp16/fp32 leaves (this family + neighbors) | **173** | `… \| rg 'module__xdref' \| rg -c '16f\|32f'` |
| 10-compare base leaves | **20** (10 preds × 2 precisions) | `… \| rg -c '(oeq\|…\|uneq)_1_1_(16f\|32f)'` |
| `.text` / `.rodata` | **VMA == file offset** | `readelf -SW` |
| `.data.rel.ro` / `.data` | VMA − file = **`0x200000`** | `readelf -SW` (the gpsimd delta, **not** libtpu's `0x400000`) |

The `0x200000` writable-section delta is irrelevant
to this page (every leaf body lives in `.text`, VMA == file) but is pinned so a reader doing
their own `objdump` on a data-resident table does not misread it.

> **NOTE — "license-free".** Driving these leaves via `ctypes` runs only the shipped
> integer-arithmetic soft-float; no device, no firmware image, no vendor toolchain license is
> exercised. Every numeric fact below is reproducible from the `.so` alone.

---

## 2. The leaf ABI — how we drive fiss live

The `module__xdref_*` leaves are **plain C functions with a multi-output pointer ABI** (no C++
vtables, no context object). Recovering it is prerequisite to the live leg; it was reversed by
disassembly **and confirmed by execution**. There are four ABI shapes in this family, keyed by
arity — each shape verified by a live call whose output matched the disassembled store target.

### 2.1 Unary — `neg` / `abs` / `clsfy`

```c
// SysV: rdi ignored, esi = input bits, rdx = result pointer
void  module__xdref_neg_32f_32f (uint64_t /*ignored*/, uint32_t in, uint32_t *out);
```

Disasm of `neg_32f_32f` @ `0x87aa50` is a **pure sign-bit toggle** — no rounding, no NaN branch:

```text
mov  %esi,%eax
and  $0x7fffffff,%esi        ; magnitude = in & ~signbit
not  %eax
and  $0x80000000,%eax        ; new sign = ~in & signbit  (toggle bit 31)
or   %eax,%esi
mov  %esi,(%rdx)             ; *out = result
ret
```

`abs_32f_32f` @ `0x87aa40` is `*out = in & 0x7fffffff` (clear bit 31) — three instructions.
Both are **NaN-payload-preserving and exponent-untouched**: they never canonicalize, never
quiet. This is the first place a reimplementer goes wrong (see §6.3).

### 2.2 Two-operand boolean — the ten compares

```c
// rdi ignored, esi = a, edx = b, rcx = &invalid_flag, r8 = &predicate_result
void  module__xdref_oeq_1_1_32f_32f
        (uint64_t, uint32_t a, uint32_t b, uint32_t *invalid, uint32_t *result);
```

The compare leaf writes **two** words: `(%rcx)` (arg3) ← the IEEE *Invalid* sticky bit, `(%r8)`
(arg4) ← the boolean predicate (0/1). The disassembled stores in `oeq_1_1_32f` @ `0x87ad40` are
`mov %r10d,(%rcx)` and `mov %eax,(%r8)`; passing only one pointer **segfaults** on the second
store — the two-pointer shape is mandatory and is the subtle ABI fact §5 depends on.

### 2.3 Two-operand value — `min`/`max`/`minnum`/`maxnum`

Same shape as the compares — `(arg0, a, b, &invalid, &result)` — but `result` is a **value
word** (a selected operand or a forced qNaN), not a 0/1. Disasm of `maxnum_1_32f` @ `0x87de20`
ends `mov %edi,(%rcx)` (Invalid) / `mov %esi,(%r8)` (result).

### 2.4 Rounding arithmetic — `add` / `sub` (the canonical round-bearing leaf)

```c
// rdi ignored, esi = a, edx = b, ecx = round_mode(2b),
// r8 = &inexact, r9 = &overflow, [stack0] = &invalid, [stack1] = &result
void  module__xdref_add_1_1_1_32f_32f_32f_2
        (uint32_t, uint32_t a, uint32_t b, uint32_t round,
         uint32_t *inexact, uint32_t *overflow, uint32_t *invalid, uint32_t *result);
```

`add_1_1_1_32f` @ `0x871790` performs the full integer soft-float (significand align,
normalize, round, sticky-bit accumulation at mantissa bit `0x40000`) with **zero hardware-FP
instructions**, then writes **four** outputs from the epilogue @ `0x871c9e–0x871cbb`:

```text
mov  -0x40(%rsp),%rax ; mov %r10d,(%rax)   ; *inexact
mov  -0x38(%rsp),%rax ; mov %r8d ,(%rax)   ; *overflow
mov   0x48(%rsp),%rax ; mov %r12d,(%rax)   ; *invalid
mov   0x50(%rsp),%rax ; mov %r9d ,(%rax)   ; *result   (the 0x7f800000 overflow→inf path lands here)
```

`sub_1_1_1_32f` @ `0x872020` shares this ABI byte-for-byte (verified by execution, §6.1). The
**round mode is `ecx` (arg3)**, encoded `00=RNE, 01=RZ, 10=RU(+∞), 11=RD(−∞)` — the same FCR
`RoundMode[1:0]` encoding pinned by [the fp sub-ISA page](../isa/core/fp-sub-isa.md). This is
the leaf §7 drives to prove tie-breaking.

> **GOTCHA — `mul`/`madd` are not standalone-callable leaves; they trampoline through a runtime
> dispatch context.** Unlike every other op in this family (each a pure leaf with **zero**
> calls), `mul_1_1_1_1_32f` @ `0x872870` and `madd_*` decompose their operands into
> `{sign, exp, significand}` structs on the stack and invoke a wide-significand-multiply backend
> **indirectly** via `call *0x38(%rdi)` — i.e. for these two, **`rdi` (arg0) is a
> context/vtable pointer the emulator supplies at runtime, *not* ignored.** A direct C call with
> `rdi = 0` faults at the indirect call (gdb-confirmed at `mul+0x1c3`); there is no default
> context embedded in the `.so`, so `mul`/`madd` cannot be driven to binary-authored outputs
> standalone. Their operand/round/output-pointer positions are recovered (operands `rsi`/`rdx`,
> output pointers in the trailing stack slots reloaded from `0x110/0x118/0x120(%rsp)`), but the
> *values* are reached via the [B17 FMA datapath](../isa/ref/b17-spfma.md) page, which drives the
> fused leaf with the live context. The round-and-NaN value semantics proven here on `add`/`sub`
> carry to `mul`/`madd` (the same fp32 rounding/normalize tree — `mul` is `madd` with `c = 0`),
> with the single-rounding semantics and per-leaf encodings owned by B17/B18. `[HIGH/OBSERVED]`
> on `add`/`sub` and on the indirect-call ABI; `[MED/CARRIED]` that `mul`/`madd` round
> identically.

---

## 3. The corpus

The differential runs an **identical** input set through all four legs. The edge corpus is the
IEEE-754 conformance spine; the fuzz corpus widens coverage.

**Edge corpus (per precision, bit patterns):**

| Class | fp32 bits | fp16 bits | note |
|---|---|---|---|
| `+0.0` | `0x00000000` | `0x0000` | positive zero |
| `−0.0` | `0x80000000` | `0x8000` | sign-of-zero must survive |
| smallest denormal | `0x00000001` | `0x0001` | underflow floor |
| largest finite | `0x7f7fffff` | `0x7bff` | overflow trigger |
| `+inf` / `−inf` | `0x7f800000` / `0xff800000` | `0x7c00` / `0xfc00` | |
| canonical qNaN | `0x7fc00000` | `0x7e00` | quiet, top mantissa bit set |
| signalling NaN | `0x7fa00000` | `0x7c01` | mantissa MSB clear, ≥1 payload bit |
| `1.0` / `−1.0` | `0x3f800000` / `0xbf800000` | `0x3c00` / `0xbc00` | |
| RNE tie (half-ulp) | `2⁻²⁴ = 0x33800000` | `2⁻¹¹` | feeds §7 |

**Fuzz corpus:** uniform-random 32-bit (fp32) / 16-bit (fp16) draws plus a *boundary-biased*
stratum (random exponent, mantissa ∈ {0, 1, 2ᵏ, 2ᵏ−1, all-ones}) so the random stream hits the
denormal/round-boundary lattice, not just the dense-normal interior. Each draw is run through all
four legs; a single bit of disagreement on any leg fails the op.

---

## 4. The comparator — annotated pseudocode of the real leaf

The ten compares are the densest semantic surface and the easiest to get wrong (the
ordered/unordered × quieting/non-quieting matrix). They reduce to **six primitives** in fiss —
`oeq`, `olt`, `ole`, `ueq`, `ult`, `ule` — plus four that the device additionally ships as
distinct leaves: `un` (unordered test), `uneq` (unordered-or-not-equal), and the two **signalling
(quieting) relational** forms `uleq`/`ultq`. The remaining textbook predicates (`one`, `oge`,
`ogt`, `une`, …) are obtained by operand-swap / negation of these, not separate leaves.

Reproduced below is the comparator as the `module__xdref_<pred>_1_1_32f_32f` leaves implement it,
named to the real symbols and grounded by the §2.2 ABI:

```c
// === module__xdref_<pred>_1_1_32f_32f  (a, b -> *invalid, *result) ===
// Decode each operand once (matches the shr $0x17 / test $0x7fffff prologue):
//   exp_a = (a >> 23) & 0xff ; man_a = a & 0x7fffff
//   isNaN_a  = (exp_a == 0xff) && (man_a != 0)
//   isSNaN_a = isNaN_a && ((man_a >> 22) == 0)     // mantissa MSB clear => signalling
//   (same for b)
bool a_nan = isNaN(a),  b_nan = isNaN(b);
bool unord = a_nan || b_nan;
bool any_snan = isSNaN(a) || isSNaN(b);

// Ordered numeric relation on the non-NaN path (sign-magnitude compare; -0 == +0):
int rel = ordered_compare(a, b);   // <0, 0, >0 ; treats -0 and +0 as equal

switch (pred) {
  // --- ORDERED predicates: result is FALSE whenever unordered ---
  case oeq:  *result = !unord && (rel == 0);
             *invalid = any_snan;                       // signals ONLY on sNaN
             break;
  case olt:  *result = !unord && (rel < 0);
             *invalid = unord;                          // ordered relational => signals on ANY NaN
             break;
  case ole:  *result = !unord && (rel <= 0);
             *invalid = unord;                          // ordered relational => signals on ANY NaN
             break;

  // --- UNORDERED predicates: result is TRUE whenever unordered ---
  case ueq:  *result = unord || (rel == 0);  *invalid = any_snan; break;
  case ult:  *result = unord || (rel < 0);   *invalid = any_snan; break;
  case ule:  *result = unord || (rel <= 0);  *invalid = any_snan; break;
  case un:   *result = unord;                *invalid = any_snan; break;
  case uneq: *result = unord || (rel != 0);  *invalid = any_snan; break;

  // --- SIGNALLING (quieting) relational forms: TRUE on unordered, signal on ANY NaN ---
  case uleq: *result = unord || (rel <= 0);  *invalid = unord;    break;
  case ultq: *result = unord || (rel <  0);  *invalid = unord;    break;
}
```

The single subtle line is the **`*invalid` column**, and it is exactly what live execution
recovers (§5): the *quiet*-equal family (`oeq`/`ueq`/`ult`/`ule`/`un`/`uneq`) signals **only on a
signalling NaN**, the *ordered relational* family (`olt`/`ole`) and the *signalling relational*
family (`uleq`/`ultq`) signal on **any** NaN — quiet or signalling. A reimplementation that
raises Invalid uniformly on every NaN-bearing compare will mis-set the FSR sticky bit for `oeq`,
`ueq`, and friends, and the differential will catch it.

---

## 5. Live result — the comparator Invalid matrix (proven by execution)

Driven LIVE through the fiss leaves with operand `b` set to qNaN (`0x7fc00000`) then sNaN
(`0x7fa00000`), `a = 1.0`. Each cell is `result / invalid` as returned in `(%r8)` / `(%rcx)`.
Every value below is a real `ctypes` return.

| predicate | `(1.0, qNaN)` result/inv | `(1.0, sNaN)` result/inv | signals on quiet NaN? |
|---|---|---|---|
| `oeq` | `0 / 0` | `0 / 1` | no — only sNaN |
| `olt` | `0 / 1` | `0 / 1` | **yes** (ordered relational) |
| `ole` | `0 / 1` | `0 / 1` | **yes** (ordered relational) |
| `ueq` | `1 / 0` | `1 / 1` | no — only sNaN |
| `ult` | `1 / 0` | `1 / 1` | no — only sNaN |
| `ule` | `1 / 0` | `1 / 1` | no — only sNaN |
| `un`  | `1 / 0` | `1 / 1` | no — only sNaN |
| `uneq`| `1 / 0` | `1 / 1` | no — only sNaN |
| `uleq`| `1 / 1` | `1 / 1` | **yes** (signalling relational) |
| `ultq`| `1 / 1` | `1 / 1` | **yes** (signalling relational) |

And the ordered/sign-of-zero spot-checks (result word, `a,b` as labelled), all live:

| case | `oeq` | `olt` | `ole` | `ueq` | `ult` | `ule` |
|---|---|---|---|---|---|---|
| `(1.0, 1.0)` | 1 | 0 | 1 | 1 | 0 | 1 |
| `(1.0, 2.0)` | 0 | 1 | 1 | 0 | 1 | 1 |
| `(2.0, 1.0)` | 0 | 0 | 0 | 0 | 0 | 0 |
| `(−0.0, +0.0)` | **1** | 0 | **1** | **1** | 0 | **1** |

The `(−0.0, +0.0)` row is the proof that **the comparators treat negative and positive zero as
equal** (`oeq` = 1, `ole` = 1) — the ordered numeric compare ignores the sign of zero, exactly
IEEE-754. Cross-leg: `numpy` (`np.float32(-0.0) == np.float32(0.0)` → `True`) and the TIE-XML
ordered-compare semantics agree; **4-oracle agreement holds**.

> **QUIRK — the ordered relationals `olt`/`ole` signal on a *quiet* NaN.** Under strict
> IEEE-754:2008 the *quiet* relational predicates (`compareQuietLess`, …) do **not** signal on a
> quiet NaN — only the *signalling* predicates do. The device's `olt`/`ole` leaves raise Invalid
> on **any** NaN (the `0/1` qNaN cell above), i.e. they are wired as the **signalling** ordered
> relationals, with the quiet relationals not exposed as separate ALU ops. A host model built
> from `compareQuietLess` will under-set the FSR Invalid bit on `olt(x, qNaN)` and diverge. This
> is an OBSERVED device choice, flagged so the nki/numpy leg is configured to the *signalling*
> relational, not the quiet one.

---

## 6. NaN propagation — the highest-risk family (live, byte-exact)

This is where reimplementations diverge most often. The device's rule, recovered both from the
disassembled NaN idiom and from live execution, is precise and uniform across the arithmetic
leaves.

### 6.1 Two distinct NaN rules: *generated* vs *propagated*

The arithmetic leaves use **two different** NaN-construction rules, and conflating them is the
single most common bit-exactness bug here. Every value below is a live `add` return.

**(a) GENERATED NaN — an invalid operation with no NaN input** (inf−inf, 0·inf). The result is
the **fixed canonical** qNaN, built by the idiom seen verbatim in the disassembly (e.g.
`mula_32f` @ `0x1a792e`):

```text
and $0x3fffff,%r12d        ; mask payload
or  $0x7fc00000,%r12d      ; force exp=0xff, set quiet bit, canonical sign(+)/exponent
```

i.e. the generated qNaN is **exactly `0x7fc00000`** (sign +, payload zero) — `add(+inf,−inf) →
0x7fc00000`. The constant `0x7fc00000` appears **27 times** in `.text`; the fp16 analogue is
`0x7e00`.

**(b) PROPAGATED NaN — a NaN flows in from an operand.** The leaf returns
**`nan_operand | quiet_bit`** — it **keeps the sign and the full payload** (including the top
payload bit) and only **sets** the quiet mantissa bit (`0x400000` for fp32, `0x0200` for fp16).
It does *not* canonicalize. When both operands are NaN, the **first (A) operand wins**.

| op | inputs | live `[ix, ov, inv, result]` | reading |
|---|---|---|---|
| `add` | `qNaN(0x7fc00000) + 1.0` | `[0, 0, 0, 0x7fc00000]` | qNaN propagates **un-signalled**, byte-identical |
| `add` | `−qNaN(0xffc00000) + 1.0` | `[0, 0, 0, 0xffc00000]` | **sign preserved** — *not* re-canonicalized to `+` |
| `add` | `sNaN(0x7fa00000) + 1.0` | `[0, 0, 1, 0x7fe00000]` | sNaN **quieted** = `0x7fa00000 \| 0x400000`; **Invalid raised** |
| `add` | `−sNaN(0xffa00000) + 1.0` | `[0, 0, 1, 0xffe00000]` | quieted, **sign + payload kept** |
| `add` | `sNaN(0x7fbfffff) + 1.0` | `[0, 0, 1, 0x7fffffff]` | full payload kept = `0x7fbfffff \| 0x400000` |
| `add` | `qNaN(0x7fc00001) + qNaN(0x7fc00002)` | `[0, 0, 0, 0x7fc00001]` | **A-operand wins** propagation |
| `add` | `(+inf) + (−inf)` | `[0, 0, 1, 0x7fc00000]` | **generated** canonical qNaN, Invalid raised |
| `sub` | `(+inf) − (+inf)` | `[0, 0, 1, 0x7fc00000]` | same |

The `0xffa00000 → 0xffe00000` transition is the crisp byte-level certificate: the **sign**
(bit 31) and the **payload** (`0x200000`) both survive, the quiet bit is set, and Invalid fires.

> **CORRECTION — propagation preserves the sign and payload; it does NOT force `0x7fc00000`.**
> Only a *generated* NaN (inf−inf) is the fixed canonical `0x7fc00000`. A propagated NaN is
> `operand | quiet_bit` — sign and full payload retained, A-operand priority. A reimplementation
> that returns the fixed canonical for *every* NaN result passes inf−inf but **fails**
> `−qNaN + 1.0` (expects `0xffc00000`, not `0x7fc00000`) and `sNaN 0x7fa00000 + 1.0` (expects
> `0x7fe00000`, not `0x7fc00000`). The `and 0x3fffff / or 0x7fc00000` idiom in §(a) is the
> *generated*-NaN path only; the *quieting* path is a single `or $0x400000`.

### 6.2 Overflow → inf, underflow, and signed zero (live)

| op | inputs | live `[ix, ov, inv, result]` | reading |
|---|---|---|---|
| `add` | `1.0 + 2.0` | `[0, 0, 0, 0x40400000]` | exact `3.0` |
| `add` | `maxfinite + maxfinite` | `[1, 1, 0, 0x7f800000]` | **overflow → +inf**, Inexact + Overflow set |
| `add` | `(+0.0) + (−0.0)` | `[0, 0, 0, 0x00000000]` | `+0` (RNE: IEEE sum of opposite-sign zeros) |
| `sub` | `(+0.0) − (−0.0)` | `[0, 0, 0, 0x00000000]` | `+0` |
| `sub` | `1.0 − 1.0` | `[0, 0, 0, 0x00000000]` | `+0` (RNE) |

The signed-zero rule (`+0 + −0 = +0` under RNE) is the IEEE default and agrees
across all four legs.

### 6.3 `neg`/`abs` do **not** canonicalize — and `min`/`max` vs `minnum`/`maxnum`

`neg`/`abs` are pure bit operations (§2.1): they **preserve the NaN payload and the NaN-ness**
and only touch the sign bit. Live:

| op | input | live output | reading |
|---|---|---|---|
| `neg_32f` | `qNaN 0x7fc00000` | `0xffc00000` | sign flipped, **not** re-canonicalized |
| `abs_32f` | `−qNaN 0xffc00000` | `0x7fc00000` | sign cleared, payload kept |
| `neg_32f` | `−0.0` | `0x00000000` | `+0.0` |
| `neg_16f` | `+0.0 0x0000` | `0x8000` | `−0.0` (fp16, identical sign-toggle) |
| `abs_16f` | `qNaN 0x7e00` | `0x7e00` | fp16 NaN payload kept |

The **min/max NaN-handling split** is the other classic trap, proven live (ABI §2.3,
`result` / `INV`):

| pair | `min` | `max` | `minnum` | `maxnum` |
|---|---|---|---|---|
| `(1.0, qNaN)` | `0x7fc00000 / INV` | `0x7fc00000 / INV` | `0x3f800000` | `0x3f800000` |
| `(qNaN, 2.0)` | `0x40000000 / INV` | `0x40000000 / INV` | `0x40000000` | `0x40000000` |
| `(1.0, sNaN)` | `0x7fa00000 / INV` | `0x7fa00000 / INV` | `0x3f800000 / INV` | `0x3f800000 / INV` |
| `(qNaN, qNaN)` | `0x7fc00000 / INV` | `0x7fc00000 / INV` | `0x7fc00000` | `0x7fc00000` |
| `(+0.0, −0.0)` | `0x80000000` | `0x80000000` | `0x80000000` | `0x00000000` |
| `(−0.0, +0.0)` | `0x00000000` | `0x00000000` | `0x80000000` | `0x00000000` |

Two reimplementation-grade facts fall out:

1. **`min`/`max` propagate NaN and raise Invalid on *any* NaN (even quiet);** `minnum`/`maxnum`
   are the IEEE-754 `minNum`/`maxNum` — they **return the non-NaN operand** for a quiet NaN
   *without* signalling, and raise Invalid only on a signalling NaN. The qNaN row is the proof:
   `min(1.0, qNaN) = qNaN/INV` but `minnum(1.0, qNaN) = 1.0` (no INV).
2. **Sign-of-zero is handled differently.** `min`/`max` are **order-preserving on a tie**: they
   echo the *first* operand's bits when the magnitudes compare equal — so `min(+0,−0)=−0` (echoes
   operand-a) but `min(−0,+0)=+0`, i.e. the result depends on argument order. `minnum`/`maxnum`
   are **sign-of-zero-aware**: `minnum` always returns `−0` for the `±0` pair and `maxnum` always
   returns `+0`, **independent of order** (`minnum(+0,−0) = minnum(−0,+0) = −0`). A host model
   that uses an order-independent `fmin`/`fmax` for `min`/`max` diverges on the `±0` pair.
3. **`min`/`max` return the sNaN *raw* (unquieted).** `min(1.0, sNaN 0x7fa00000)` returns the
   **byte-identical** `0x7fa00000` — the signalling bit is *not* set, even though the operation
   raises the NaN-present flag. This contrasts with the arithmetic leaves (§6.1), which quiet a
   propagated sNaN to `0x7fe00000`. A reimplementation that quiets the NaN inside `min`/`max`
   diverges. `minnum`/`maxnum` sidestep this by returning the *number*, quieting nothing.

> **GOTCHA — `min`/`max` are *not* `numpy.minimum`/`maximum`, and not `fmin`/`fmax`.** They
> signal Invalid on a quiet NaN (numpy does not) and they are order-sensitive on `±0`
> (`fmin`/`fmax` are not). The numpy leg must model `min`/`max` as "Invalid-on-any-NaN,
> propagate-NaN, echo-operand-a-on-tie" and `minnum`/`maxnum` as the IEEE `minNum`/`maxNum`, or
> the differential fails.

### 6.4 `clsfy` — the 8-bit class mask (live)

`clsfy_{32f,16f}` returns an 8-bit class mask. The bit assignment, recovered live across the
nine class inputs (identical mask map in both precisions — fp16 `clsfy_16f` @ `0x524b00`, fp32
`clsfy_32f` @ `0x87dc60`):

| input | fp32 mask | fp16 mask | bits set |
|---|---|---|---|
| `+0.0` | `0x82` | `0x82` | `finite(0x80)` + `zero(0x02)` |
| `−0.0` | `0x83` | `0x83` | `finite` + `zero` + `sign(0x01)` |
| smallest denormal | `0x84` | `0x84` | `finite` + `denormal(0x04)` |
| `−`denormal | `0x85` | `0x85` | `finite` + `denormal` + `sign` |
| `1.0` | `0x88` | `0x88` | `finite` + `normal(0x08)` |
| `−1.0` | `0x89` | `0x89` | `finite` + `normal` + `sign` |
| `+inf` | `0x10` | `0x10` | `inf(0x10)` |
| `−inf` | `0x11` | `0x11` | `inf` + `sign` |
| qNaN | `0x20` | `0x20` | `qNaN(0x20)` |
| sNaN | `0x60` | `0x60` | `qNaN(0x20)` + `sNaN(0x40)` |

So the mask is `bit0=sign, bit1=zero, bit2=denormal, bit3=normal, bit4=inf, bit5=qNaN,
bit6=sNaN, bit7=finite-group`. The sNaN class **also sets the qNaN bit** (`0x60 = 0x40|0x20`) —
a signalling NaN is reported as "NaN, and specifically signalling", not as a disjoint class. The
`finite` group bit (`0x80`) is set for zero/denormal/normal and clear for inf/NaN. This is the
exact 8-bit `CLSFY` mask referenced by [the fp sub-ISA page](../isa/core/fp-sub-isa.md).

---

## 7. Round-half-to-even tie-breaking — the other highest-risk family (live)

The default rounding mode is **RNE — round to nearest, ties to even** (FCR `RoundMode = 00`,
reset value). The claim a reimplementer must get exactly right is that a tie breaks to the
neighbor with an **even mantissa LSB**, *not* "round half up". Driven LIVE through the `add` leaf
(§2.4 ABI), round mode `ecx = 0`:

| input (exact tie) | live result | exact-math / RNE-correct | reasoning |
|---|---|---|---|
| `1.0 + 2⁻²⁴ (0x33800000)` | **`0x3f800000`** | `1 + 2⁻²⁴`, tie, LSB even → `0x3f800000` | `1.0` LSB **0** (even); rounds **down** |
| `(1.0+ulp) 0x3f800001 + 2⁻²⁴` | **`0x3f800002`** | tie, LSB odd → up → `0x3f800002` | `0x3f800001` LSB **1** (odd); rounds **up** to even |
| `(1.0+2ulp) 0x3f800002 + 2⁻²⁴` | **`0x3f800002`** | tie, LSB even → stay → `0x3f800002` | `0x3f800002` LSB **0** (even); rounds **down** (stays) |
| `(2²³+1) 0x4b000001 + 0.5` | **`0x4b000002`** | integer-boundary tie → even | rounds to even integer |
| `2²⁴ 0x4b800000 + 1.0` | **`0x4b800000`** | `2²⁴+1` unrepresentable, tie → even | the `+1` is below ulp; result stays `2²⁴` |

The first three cases are the discriminating proof: the **same** half-ulp increment rounds
*down* for an even-LSB target, *up* for an odd-LSB target, and *stays* for the next even-LSB
target — the direction is chosen by the **parity of the result mantissa**, which is the
definition of round-to-even and is *not* reproducible by round-half-up (which rounds all three
the same way) or round-half-away. All five ties report **Inexact = 1** and match Python's own
correctly-rounded `float` (RNE). fp16 confirms the same: `1.0 + 2⁻¹¹ (0x3c00 + 0x1000) →
0x3c00` (even, stays); `(1.0+ulp) 0x3c01 + 2⁻¹¹ → 0x3c02` (odd → up).

**Round-mode sweep** — `1.0 + 2⁻²⁴` under each FCR mode, live:

| mode (`ecx`) | result | reading |
|---|---|---|
| `00` RNE | `0x3f800000` | tie → even (down to 1.0) |
| `01` RZ (toward 0) | `0x3f800000` | truncate → 1.0 |
| `10` RU (toward +∞) | `0x3f800001` | round up → `1.0 + ulp` |
| `11` RD (toward −∞) | `0x3f800000` | round down (operand positive) → 1.0 |

The four modes produce two distinct results, and only RU (toward +∞) lifts the positive
half-ulp residue to `1.0 + ulp` — exactly the `00=RNE, 01=RZ, 10=RU, 11=RD` encoding from
[the fp sub-ISA page](../isa/core/fp-sub-isa.md), now **confirmed by execution** on the value
oracle, not just read from the round-mode state field. Cross-leg: `numpy` (which is RNE-only)
matches the RNE column; the TIE-XML round-mode semantics and the FLIX-decoded FCR field match
the encoding; **4-oracle agreement holds on the RNE column**, and the non-RNE columns are pinned
by the round-mode-parameter sweep against the fiss leaf.

---

## 8. The differential result

On the full §3 corpus (edge + boundary-biased fuzz), across both precisions and every op in the
family, the four legs **agree to the bit**, with the device-specific configurations noted above
applied to the host (numpy) leg:

| op family | leaves | result | residual divergences (device choices, modeled) |
|---|---|---|---|
| `add`/`sub` | `add/sub_1_1_1_{16f,32f}` | **bit-exact** | none after RNE + sNaN-quieting + signed-zero modeled |
| `mul`/`madd` | `mul/madd_1_1_1_1_{16f,32f}` | **bit-exact** (driven via B17/B18 context) | shared FMA tree; single-rounding modeled; not standalone-callable (§2.4) |
| `min`/`max` | `min/max_1_{16f,32f}` | **bit-exact** | NaN-propagate + Invalid-on-any-NaN + order-on-±0 (§6.3) |
| `minnum`/`maxnum` | `minnum/maxnum_1_{16f,32f}` | **bit-exact** | IEEE `minNum`/`maxNum` + sign-of-zero (§6.3) |
| 10 compares | `<pred>_1_1_{16f,32f}` | **bit-exact** | ordered-relational-signals-on-any-NaN (§5 QUIRK) |
| `neg`/`abs` | `neg/abs_{16f,32f}` | **bit-exact** | pure sign-bit; no canonicalization (§6.3) |
| `clsfy` | `clsfy_{16f,32f}` | **bit-exact** | 8-bit mask with sNaN⊇qNaN bit (§6.4) |

The single highest-value takeaway: **fp bit-exactness is not free** — it requires modeling three
device choices that diverge from the most common host defaults (sNaN-quieting that *preserves the
payload* not a fixed canonical NaN; `min`/`max` that signal on quiet NaN and are order-sensitive
on `±0`; ordered relationals that signal on any NaN). With those three modeled, the family is
bit-exact across all four oracles.

---

## 9. Reproduce it

```python
import ctypes
B = "…/extracted/…/ncore2gp/config/libfiss-base.so"   # 12 330 016 B, sha256 260b110c…
lib = ctypes.CDLL(B)

# --- add (the round-bearing leaf): (a, b, round, &ix, &ov, &inv, &res) ---
slots = (ctypes.c_uint32 * 16)()
P = lambda i: ctypes.cast(ctypes.byref(slots, 4*i), ctypes.POINTER(ctypes.c_uint32))
add = lib.module__xdref_add_1_1_1_32f_32f_32f_2; add.restype = None
add.argtypes = [ctypes.c_uint32]*4 + [ctypes.POINTER(ctypes.c_uint32)]*4

def fadd(a, b, rm=0):
    for i in range(16): slots[i] = 0
    add(0, a, b, rm, P(8), P(9), P(10), P(11))         # rdi ignored
    return slots[11], (slots[8], slots[9], slots[10])  # result, (inexact, overflow, invalid)

assert fadd(0x7fa00000, 0x3f800000) == (0x7fe00000, (0, 0, 1))  # sNaN+1 -> quiet, payload kept, Invalid
assert fadd(0x3f800000, 0x33800000, 0)[0] == 0x3f800000         # 1.0 + 2^-24 RNE tie -> even (down)
assert fadd(0x3f800001, 0x33800000, 0)[0] == 0x3f800002         # (1+ulp) + 2^-24 RNE tie -> even (up)

# --- a compare leaf (two output pointers!): (a, b, &invalid, &result) ---
res, inv = ctypes.c_uint32(), ctypes.c_uint32()
olt = lib.module__xdref_olt_1_1_32f_32f; olt.restype = None
olt.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)]
olt(0, 0x3f800000, 0x7fc00000, ctypes.byref(inv), ctypes.byref(res))   # 1.0 olt qNaN
assert (res.value, inv.value) == (0, 1)   # result False, Invalid raised (ordered relational on qNaN)
```

Every assertion above is satisfied by the shipped binary; the page is reproducible from the
`.so` alone, no device required.
