# fiss Datapath — the 864-Leaf Value Oracle

This page is the **value-semantics ground truth** for the entire IVP / Vision-Q7 vector ISA. Where
the cycle-accurate ISS `libcas-core.so` decodes an instruction and schedules its timing, it does
**not** compute element values — it delegates them. The functions it delegates *to* live in
`libfiss-base.so`, and the **864 `module__xdref_*` leaves** of that library are the per-lane
datapath value primitives: the bit-exact `a OP b` (wrap, saturate, signedness, rounding, NaN) for
every IVP mnemonic. This is the central node of [Part 14 — the ISS as Executable Oracle](./cas-core-surface.md):
every cas *value* page ([arith](./cas-arith-sem.md), [MAC/FMAC](./cas-mac-fmac.md),
[convert/pack/fp](./cas-convert-pack-fp.md)) delegates here, and every VAL lane drives these leaves
live. It is also the origin of the `xdsem` / `xdref` model named on those pages.

> **Keystone — cas decodes, fiss values.** The cas core ([core surface](./cas-core-surface.md))
> computes DECODE + TIMING + REGISTER-HAZARD and forwards each per-lane `a OP b` to the host through
> 119 `nx_*_interface` callbacks. `libfiss-base.so` is **the value oracle on the other end**: its
> 864 `module__xdref_*` leaves are the element-arithmetic primitives — tiny scalar single-lane
> bodies that implement exactly one width-typed op. The vector execute realises an N-wide op as a
> **scalar per-lane loop** of the matching `xdref` leaf. This page documents the roster, how each
> leaf computes its lane value, the execute path that drives them, and drives a representative
> sample **live via `ctypes`** to certify the oracle end-to-end.

The page default is `[HIGH/OBSERVED]` (read-from-byte, proven by disassembly, or proven by a live
`ctypes` call); claims that depart from it carry an explicit tag, per
[the Confidence & Walls model](../reference/confidence-model.md).
All offsets are into the shipped `extracted/.../ncore2gp/config/libfiss-base.so` (**12 330 016 B**;
ELF64 LSB x86-64, SYSV, **not stripped**). Every count below is grounded by
`nm -D <abs-path> | rg -c`, never the decompile.

> **GOTCHA — VMA vs file offset is not uniform.** `.text` (VMA `0x190430`) and `.rodata`
> (VMA `0x88ff00`) are **VMA == file offset**, so the disassembly addresses in this page are raw
> VMA. The writable sections are **not**: `.data.rel.ro` (VMA `0xc17e80` → file `0xa17e80`) and
> `.data` (VMA `0xc8eb68` → file `0xa8eb68`) both carry a **`0x200000` delta** (`readelf -SW`). No
> value-leaf body touches `.data`; this caveat matters only if you `xxd` a writable struct.

---

## 1. The export census — 864 value leaves inside 20 384 symbols

`libfiss-base.so` is a **self-contained value oracle**: it imports nothing but libc/runtime stubs
and computes all element math in-process. The dynamic symbol table, grounded directly on the
binary:

| Sweep (`nm -D <abs> \| rg -c …`) | Count | Meaning |
|---|---:|---|
| total `nm -D` lines (`.dynsym` − null) | **20 384** | the full dynamic symbol table |
| defined exports (types `T/W/R/D/B`) | **20 379** | functions + a handful of objects |
| undefined imports (`UND`) | **5** | libc/runtime only (see below) |
| `slotfill__*` (decode → operand-slot fill) | **12 569** | the bitfield-extract decoders |
| `opcode__*` (execute bodies) | **1 708** | the per-mnemonic vector/scalar execute |
| **`module__xdref_*` (the value leaves)** | **864** | **the per-lane datapath primitives** |
| `nx_*_interface` host callbacks | **0** | none — value math is **in-process** |

Every one of the 864 `module__` exports **is** a `module__xdref_*` leaf (`nm -D | rg ' module__' |
rg -vc 'module__xdref_'` = **0**). The `864 / 12 569 / 20 379` reconciliation as a nested superset
chain is laid out on the [fiss surface page](./fiss-surface-exceptions.md); this page reads the
**bodies** of the 864.

> **NOTE — self-contained: exactly 5 undefined imports.** `readelf --dyn-syms | rg ' UND '` yields
> five distinct names, all libc/runtime: `__cxa_finalize@GLIBC_2.2.5`, `_Jv_RegisterClasses`,
> `_ITM_deregisterTMCloneTable`, `_ITM_registerTMCloneTable`, `__gmon_start__`. There are **zero**
> `nx_*_interface` callbacks (`libcas-core.so` had 119). The value math is therefore **computed in
> this DLL**, not delegated onward — which is exactly what makes it the oracle.

### 1.1 The `module__xdref_*` naming grammar

The leaf name *is* the operand-width descriptor. The grammar, OBSERVED across all 864 names and
confirmed against the disassembled bodies:

```
module__xdref_<op>[<post>]_<outW>_<inAW>[_<inBW>][_<extraW>…][<suffix>]
```

- **`<op>`** — operation root (446 distinct first-tokens; ~200 semantic roots once width
  disambiguators are folded).
- **widths** — decimal **bit-width** of each operand, in *output-first* order:
  `8 / 16 / 24 / 32 / 48 / 64 / 96`. `512` = a full 512-bit vector register (bulk bitwise). A
  leading `1_1_…` run on float ops is the lane-rate / IEEE-staging descriptor.
- **suffixes / tokens** (each corroborated by at least one disassembled body):
  `f` = IEEE float element (`16f`/`32f`/`64f`); `tf` = tagged float; `c` = complex (interleaved
  re/im 16-bit halves in a 32-bit slot); `j` = conjugate operand; `t` = predicated (per-lane
  `vbool` guard); `u` = unsigned interpretation; leading `s` = signed-narrowing / saturating;
  `packl`/`packm`/`packp`/`packq` = pack-low/-mid/-pair/-quad; `n` = narrowing/widening
  disambiguator; `r` = rounding.

> **CORRECTION — the float census is 180, not 184.** A direct name parse on the binary
> (`nm -D … | rg 'module__xdref_' | awk '{print $1}'`, then `rg -c '[0-9]+t?f(_\|$)'`) gives
> **180** leaves bearing an IEEE float-width token (`Nf` or `Ntf`) and **684** integer/structural.
> An earlier hand-count put this at 184 / 680; the on-binary truth is **180 / 684** (the two `tf`
> outliers — `divn_32tf_…` and `divn_64tf_…` — are the only names that need the `tf` alternation to
> be caught). Cite **180 float / 684 integer-structural**.

### 1.2 The lane-primitive ABI

The execute calls each leaf through the System V AMD64 C ABI. The **operand register layout is
shifted by one** from a naive `(A, B, *out)` reading: the leaf reserves the first integer slot
(`%rdi`) for a context/self pointer (unused by the simple ALU leaves), and the operands begin at
the *second* slot.

| Form | `%rdi` | `%rsi` | `%rdx` | `%rcx` | `%r8` |
|---|---|---|---|---|---|
| 3-operand binary (`add_16_16_16`) | ctx | A | B | result\* | — |
| unary (`abs_16_16`, `sext_32_16`) | ctx | A | result\* | — | — |
| 4-operand accumulate (`mula_*`) | ctx | acc/A | B | C | result\* |
| dual-out convert (`cvtf32_1_32f_16f`) | ctx | A | flag-out\* | result\* | — |

> **GOTCHA — a C caller must pass the leading `%rdi` context argument.** The body of
> `module__xdref_add_16_16_16` reads operand A from `%esi`, B from `%edx`, and stores through
> `%rcx`. If you declare the `ctypes` prototype as `(c_int, c_int, c_void_p)`, SysV places your A in
> `%rdi`, B in `%rsi`, result-ptr in `%rdx` — and the leaf's `mov %edx,(%rcx)` dereferences whatever
> garbage is in `%rcx`, **segfaulting at `add_16_16_16+0x8`** (observed). The correct prototype is
> `(c_void_p ctx, c_int A, c_int B, c_void_p result)` with `ctx = None`. This off-by-one is the one
> non-obvious step to driving the oracle; §6 uses it throughout.

---

## 2. The 864 leaves, taxonomized by family

The 864 leaves partition (with overlap at the boundaries — the grammar is multi-token, so these are
*representative* family slices, not a disjoint cover) into the families below. Counts are
`rg -c '^module__xdref_(<roots>)'` on the name list; each row names real symbols you can drive.

| Family | ~Count | Representative `module__xdref_*` symbols | What the leaf computes |
|---|---:|---|---|
| **Integer arith** (wrap) | ~37 | `add_16_16_16`, `sub_8_8_8`, `neg_16_16`, `abs_32_32`, `avg_16_16_16`, `avgr_16_16_16`, `abssub_16_16_16`, `absssub_16_16_16` | `(a OP b) mod 2^w`; sat & avg variants |
| **MAC / multiply** | ~241 | `mul_24_8_8`, `mul_48_16_16`, `muls_24_24_8_8`, `mula_…`, `muln_2x16x32_0_96_32_32`, `muluu_24_8_8`, `mulus_24_8_8`, `dmulq2n8dxr8_…` | widening signed/unsigned/mixed product; mul-add (`mula`), mul-sub (`muls`), complex (`c`/`j`) |
| **Min / max / compare** | ~95 | `min_16_16_16`, `max_16_16_16`, `minu_16_16_16`, `minnum_1_16f_16f_16f`, `oeq_1_1_16f_16f`, `olt_…`, `un_…`, `ult_…` | signed/unsigned min-max; IEEE NaN-order vs `minNum`; ordered/unordered fp predicates |
| **Convert / pack** | ~123 | `sext_32_16`, `zeroext_48_32`, `sats_16_32`, `cvt16s_16_24l`, `cvt32s_…`, `packl_16_48`, `packq_16_48`, `packv_…`, `cvtf32_1_32f_16f`, `cvtf16_…` | exact sext/zext; saturating narrow; truncating/saturating pack; int↔float, fp16↔fp32 |
| **Bitwise / shift** | ~61 | `and_512_512_512`, `xor_512_512_512`, `not_512_512`, `andb_64_64_64`, `andnotb_64_64_64`, `sll_u_32_32_5`, `sra_s_32_32_7`, `srl_u_…`, `widestshift_…` | full-vector 512-bit bitwise; 64-bit `vbool` logic; variable shift (signed/unsigned shamt) |
| **Bit-count / normalize** | ~23 | `popc_8_8`, `popc32_…`, `nsa_16_16`, `nsau_16_16`, `tzc_…`, `clsfy_16f_…`, `bitkill_moveqz`, `bitrev_…` | population count; normalize-shift-amount; trailing-zero count; fp classify; per-lane bit-kill |
| **Reduce** (`r`-prefixed) | ~68 | `radd_nx16_32_512`, `radds_…`, `rmin_16`, `rmax_…`, `rbminu_…`, `rband_…` | full-vector reduction-tree (sum/min/max/bool-and/or) over all 32 lanes |
| **Gather / permute / select** | ~27 | `extr_2nx8_32_512_32`, `dsel_nx16`, `dseli_h`, `expandbytemask`, `dcmprs_clamp`, `dextrpr_n` | lane extract / dual-select / compress / mask-expand |
| **Predicate-flag pack** | ~27 | `predflag2t_…`, `fs1ltu_64_8_8`, `borfs_…`, `bnorfs_…`, `bsubnorm`, `baddnorm` | pack lane results into a `vbool` register; fp-status flag accumulation |
| **Transcendental seeds** | ~21 | `recip0_…`, `rsqrt0_…`, `sqrt0_…`, `div0_…`, `divn_32tf_32tf_32tf_2f`, `nexp0_…`, `recipqli_…` | Newton / polynomial **seed** primitives for reciprocal/rsqrt/divide/exp |
| **Move / copy** | ~27 | `mov_16_32`, `mov_…`, `const_nx16f`, `const_n` | register-slot copy; constant materialization |

`[HIGH/OBSERVED]` for the symbols (all present in `nm -D`); `[MED/INFERRED]` for the family
*boundaries* (the multi-token grammar means a `dmulusqa2n8dxr8` legitimately belongs to several
slices — the counts are indicative, the symbols are exact).

---

## 3. How each leaf computes its lane value — annotated bodies

Three distinct families below, each anchored to a real disassembled leaf. Operand A = `%esi`,
B = `%edx`, result\* = `%rcx`, with the implicit `%rdi` context slot (§1.2).

### 3.1 Wrapping arithmetic — `module__xdref_add_16_16_16` @ `0x858480`

The integer base case: add, then mask to width. No saturation, no signedness dependence — modular
two's-complement.

```asm
; module__xdref_add_16_16_16  @0x858480  — int16 add, wrapping
858480:  add    %esi,%edx            ; sum = A + B   (32-bit ALU, full precision)
858482:  and    $0xffff,%edx         ; sum &= (1<<16)-1   <-- wrap to 2^16
858488:  mov    %edx,(%rcx)          ; *result = sum
85848a:  ret
```

```c
/* module__xdref_add_16_16_16 — per-lane int16 add, wrapping mod 2^16.
 * width mask = (1u << outW) - 1; the 32-bit form (add_32_32_32 @0x5bc340)
 * needs no mask because the native x86 add already wraps at 2^32. */
void xdref_add_16_16_16(void *ctx, int32_t a, int32_t b, uint32_t *result) {
    (void)ctx;
    *result = (uint32_t)(a + b) & 0xffffu;        /* (a + b) mod 2^16 */
}
```

This **directly confirms** the cas-arith reading of `IVP_ADDNX16` as signed-i16 add, wrapping mod
2^16 — see [cas Vector-Arithmetic Semantics](./cas-arith-sem.md). The 8-bit sibling masks `0xff`;
the 32-bit sibling omits the mask.

### 3.2 Saturating arithmetic — `module__xdref_adds_16_16_16` @ `0x85aa10`

The saturating signed-i16 add: sign-extend both operands, do a 32-bit signed sum, then detect
overflow by comparing **bit 15** against **bit 16** of the 17-bit sign-extended sum, and clamp to
exactly `0x7fff` / `0x8000`.

```asm
; module__xdref_adds_16_16_16  @0x85aa10  — saturating int16 add
85aa10:  movswl %si,%eax              ; A sign-extended to 32b
85aa13:  movswl %dx,%edx              ; B sign-extended to 32b
85aa16:  add    %eax,%edx             ; signed 32-bit sum
85aa18:  mov    %edx,%eax
85aa1a:  and    $0x1ffff,%eax         ; keep low 17 bits
85aa1f:  mov    %eax,%esi
85aa21:  mov    %eax,%edi
85aa23:  shr    $0xf,%esi             ; esi = bit15(sum)
85aa26:  shr    $0x10,%edi            ; edi = bit16(sum)
85aa29:  and    $0x1,%esi
85aa2c:  cmp    %edi,%esi             ; bit15 == bit16 ?
85aa2e:  je     85aa50                ;   yes -> no overflow
; --- overflow path: select the clamp by the sum's sign ---
85aa30:  mov    %eax,%edx
85aa32:  shr    $1,%eax
85aa34:  shl    $0x10,%edx
85aa37:  and    $0x8000,%eax          ; 0x8000 if sum negative
85aa3c:  sar    $0x1f,%edx            ; all-ones if sum<0
85aa3f:  and    $0x7fff,%edx          ; 0x7fff if sum<0 ...
85aa45:  or     %edx,%eax             ; ... -> +ovf:0x7fff  -ovf:0x8000
85aa47:  mov    %eax,(%rcx)
85aa49:  ret
85aa50:  movzwl %dx,%eax              ; no overflow: low 16, zero-extended
85aa53:  mov    %eax,(%rcx)
85aa55:  ret
```

```c
/* module__xdref_adds_16_16_16 — saturating signed int16 add.
 * Overflow iff bit15(sum) != bit16(sum) of the 17-bit sign-extended sum;
 * clamp is +0x7fff for a positive overflow, 0x8000 for a negative one. */
void xdref_adds_16_16_16(void *ctx, int32_t a, int32_t b, uint32_t *result) {
    (void)ctx;
    int32_t sum = (int16_t)a + (int16_t)b;        /* full-precision signed sum */
    uint32_t s17 = (uint32_t)sum & 0x1ffffu;      /* low 17 bits */
    if (((s17 >> 15) & 1u) != ((s17 >> 16) & 1u)) /* bit15 != bit16 -> overflow */
        *result = (sum < 0) ? 0x8000u : 0x7fffu;  /* clamp to int16 range */
    else
        *result = (uint16_t)sum;                  /* in range */
}
```

The same 17-bit overflow-detect/clamp skeleton drives `subs_16_16_16` (`sub` for `add`),
`negs_16_16`, `abss_16_16` (calls `negs` for negatives), and `absssub_16_16_16`. This **upgrades**
the cas-arith saturation claims from MED to HIGH with byte-exact `0x7fff`/`0x8000` constants.

### 3.3 Signed min — `module__xdref_min_16_16_16` @ `0x8584b0`

The signed compare uses a **sign-bias trick**: flip the sign bit of both operands to map the signed
order onto the *unsigned* order, then use the hardware unsigned `cmp` + `cmov`. The unsigned
siblings (`minu`/`maxu`) skip the bias and use a bare `cmp`/`cmov` — so signedness is **structural
in the body**, not a runtime flag.

```asm
; module__xdref_min_16_16_16  @0x8584b0  — signed int16 min (sign-bias)
8584b0:  mov    %esi,%eax
8584b2:  xor    %r8d,%r8d
8584b5:  mov    %edx,%edi
8584b7:  shr    $0xf,%eax             ; sign bit of A
8584ba:  test   %eax,%eax
8584bc:  mov    %esi,%eax
8584be:  sete   %r8b                  ; r8b = !sign(A)
8584c2:  and    $0x7fff,%eax          ; low 15 bits of A
8584c7:  shl    $0xf,%r8d
8584cb:  or     %eax,%r8d             ; keyA = (!sign(A) << 15) | (A & 0x7fff)
8584ce:  mov    %edx,%eax             ; ... same for B -> keyB in %eax
8584d0:  shr    $0xf,%eax
8584d3:  test   %eax,%eax
8584d5:  sete   %al
8584d8:  and    $0x7fff,%edi
8584de:  movzbl %al,%eax
8584e1:  shl    $0xf,%eax
8584e4:  or     %edi,%eax             ; keyB
8584e6:  cmp    %eax,%r8d             ; unsigned compare of the biased keys
8584e9:  cmovae %edx,%esi             ; if keyA >= keyB take B  -> signed MIN
8584ec:  mov    %esi,(%rcx)
8584ee:  ret
```

```c
/* module__xdref_min_16_16_16 — signed int16 min via sign-biased unsigned compare.
 * key(x) = (!sign(x) << 15) | (x & 0x7fff) maps the signed order onto the
 * unsigned order, so the hardware unsigned cmp+cmov yields a signed min.
 * minu_16_16_16 @0x8584f0 omits the bias: bare `cmp %edx,%esi ; cmova`. */
void xdref_min_16_16_16(void *ctx, int32_t a, int32_t b, uint32_t *result) {
    (void)ctx;
    uint32_t ka = (((a >> 15) & 1u) ? 0u : 0x8000u) | (a & 0x7fffu);
    uint32_t kb = (((b >> 15) & 1u) ? 0u : 0x8000u) | (b & 0x7fffu);
    *result = (uint16_t)((ka >= kb) ? b : a);     /* signed min */
}
```

This confirms the cas reading that `MINNX16` (signed) and `MINUNX16` (unsigned) are *distinct
implementations*, not a toggled mode bit.

### 3.4 Soft-float convert — `module__xdref_cvtf32_1_32f_16f` @ `0x5b77f0`

The float path carries **zero hardware FP**: every fp16→fp32 widen is integer field extraction,
classification, and repack. The body extracts the binary16 fields (5-bit exp via `shr $0xa & and
$0x1f`, 10-bit mantissa via `and $0x3ff`, sign), classifies NaN/Inf (`exp == 0x1f`), normalizes
subnormals (`bsr`), rebiases, repositions the mantissa (`shl $0xd`), and repacks into binary32 — and
it writes **two** outputs: a flag/exception word through `%rdx` and the fp32 result through `%rcx`.

```c
/* module__xdref_cvtf32_1_32f_16f — IEEE binary16 -> binary32 widen, integer-only.
 * Two outputs: *flag (exception bits) and *result (fp32 bit pattern).
 * Field masks observed: exp 0x1f, mantissa 0x3ff, NaN/Inf test exp==0x1f;
 * mantissa repositioned `<< 0xd`; default fp32 qNaN 0x7fc00000. */
void xdref_cvtf32_1_32f_16f(void *ctx, uint32_t h, uint32_t *flag, uint32_t *result) {
    (void)ctx;
    uint32_t sign = (h >> 15) & 1u;
    uint32_t exp  = (h >> 10) & 0x1fu;
    uint32_t mant =  h & 0x3ffu;
    /* exp==0 -> zero/subnormal (bsr-normalize); exp==31 -> Inf/NaN;
     * else -> normal: rebias exp (15 -> 127), mantissa << 13. */
    *flag   = /* exception bits, 0 for exact widens */ 0u;
    *result = /* repacked binary32, sign|exp'|mant', integer-only */ 0u;
}
```

The fp16/fp32 *add* leaves (`module__xdref_add_1_1_1_16f_16f_16f_2` @ `0x51c640`, `…_32f…` @
`0x871790`) are the same shape at larger scale (0x830 / 0x890 bytes, zero `xmm`/`x87`), with a
status-word output and a parameter-driven rounding context in `%r8`/`%r9`. See
[cas convert/pack/fp](./cas-convert-pack-fp.md) for how the cas decode binds these mnemonics.

> **GOTCHA — the fp-add leaves take an intricate multi-output argument frame.** Unlike the simple
> binary leaves, `add_1_1_1_16f_16f_16f_2` spills `%rcx`/`%r8`/`%r9` to the stack and writes its
> result through a *reloaded* pointer; a naive `(ctx, a, b, *out, round, *status)` prototype
> segfaults. Drive the *converts* (which have the stable dual-out `(ctx, a, *flag, *result)` frame —
> §6) to certify the soft-float path; trace the add frame from the spill slots before driving it.

---

## 4. The execute path that drives the leaves

The leaves do not float free — they are invoked by the `opcode__*` execute bodies, of which there
are **1 708**. There are two execute stages of interest:

- **`opcode__<m>__stage_5`** — the **vector** execute (1 140 of them). It realises an N-wide IVP op
  as a **scalar per-lane loop** over the matching `xdref` leaf.
- **`opcode__<m>__stage_14`** — the **scalar base-ISA** execute (212 of them). It computes the
  32-bit Xtensa AR op **natively in-line** — no `xdref` call.

### 4.1 Vector execute = a scalar per-lane loop

`opcode__ivp_addnx16t__stage_5` @ `0x256d50` is the predicated 16-bit vector add (an `NX16` op =
32 lanes of a 512-bit register). Disassembling its body and counting call targets:

```
$ objdump -d --start-address=0x256d50 --stop-address=0x2576b0 libfiss-base.so \
    | rg 'call' | rg -o '<[^>]+>' | sort | uniq -c
     32 <module__xdref_add_16_16_16@plt>      <-- 32 lanes x the int16 add leaf (§3.1)
     32 <module__xdref_bitkillt_16_2@plt>     <-- 32 lanes x the per-lane vbool predicate
```

**Exactly 32 calls** to `module__xdref_add_16_16_16` and **32** to `module__xdref_bitkillt_16_2` —
one per NX16 lane. The lanes are unrolled, not looped, and even though the leaves are in the same
DLL, the calls route through `@plt` (the leaves are dynamic exports). This is the concrete
realisation of "a vector op is N applications of a scalar value leaf."

### 4.2 Scalar base-ISA execute computes natively

The Xtensa-24 core AR ALU does **not** use `xdref` leaves — `opcode__<op>__stage_14` reads operand
slots `0x4c`/`0x50`/`0x54` of the per-op context (`%rdi`) and writes slot `0x28`:

```asm
; opcode__add__stage_14  @0x8875e0  — Xtensa AR add, native
8875e0:  mov    0x4c(%rdi),%eax      ; src1
8875e3:  add    0x54(%rdi),%eax      ; + src2
8875e6:  mov    %eax,0x28(%rdi)      ; -> result slot
8875e9:  xor    %eax,%eax            ; return 0 = no exception
8875eb:  ret
```

The trailing `xor %eax,%eax ; ret` is the "no exception" return. The full base ALU set
(`sub`/`and`/`or`/`xor`/`addi`/`neg`/`abs`/`addx2`/`slli`/`srai`/`extui`/`clamps`/`min`/`max`/`minu`)
follows this shape, computing in-line with no host handoff. The scalar `min`/`max` reuse the same
sign-bias trick as the vector `xdref_min`/`max` (§3.3).

---

## 5. This is the origin of the cas `xdsem` / fiss `xdref` model

The keystone resolves an asymmetry between the two ISS DLLs:

- **`libcas-core.so`** (the cycle-accurate ISS) decodes + schedules, then **delegates** each lane
  value to the host through 119 `nx_*_interface` callbacks. It *names* the `xdsem` model but does
  not contain the value bodies — see [cas arith](./cas-arith-sem.md), [cas MAC/FMAC](./cas-mac-fmac.md),
  [cas convert/pack/fp](./cas-convert-pack-fp.md), each of which records "the value half is fiss's"
  and links here.
- **`libfiss-base.so`** (this DLL) **is** that value half: the 864 `module__xdref_*` leaves are the
  in-process, bit-exact `a OP b` for every IVP mnemonic. There is **no further delegation** — the 5
  imports are libc, the 0 `nx_*_interface` callbacks prove the math lands here.

So the 864-leaf roster is the **common origin** of every cas value page and the **source of truth
the VAL lane drives live**. The downstream validation lane
([the 4-Oracle Bit-Exact Differential Method](../validation/four-oracle-method.md)) treats these
leaves as the differential-test arbiter: it generates random operand vectors, drives the candidate
reimplementation and the matching `module__xdref_*` leaf via `ctypes`, and asserts bit-equality. The
cas value pages have each driven a specific leaf live to ground their
semantics — `mula_24_24_8_8` / `mula_48_48_16_16` (MAC), `bitkillf_32_4` (predicate),
`cvtf32` / `packl_16_48` (convert/pack), `radd_nx16_32_512` / `rbmin_16` (reduce) — and §6 below is
their common certification harness. The [oracle synthesis capstone](./iss-oracle-synthesis.md) and
the [formal ISA model](../isa/semantics/formal-isa-model.md) both consume this page as the
element-semantics anchor.

---

## 6. Live certification — driving the oracle via `ctypes`

The leaves are drivable directly. The harness below loads the binary by absolute path and drives a
representative sample across families — **9 distinct leaves over 9 families** — with the §1.2 ABI
(leading `ctx`, operands in `%rsi`/`%rdx`, result through `%rcx`).

```python
import ctypes, struct, math
BIN = ".../ncore2gp/config/libfiss-base.so"      # absolute path
lib = ctypes.CDLL(BIN); VP = ctypes.c_void_p

def d3(name, a, b):                              # 3-op: ctx, A=rsi, B=rdx, result*=rcx
    fn = getattr(lib, name); fn.restype = None
    fn.argtypes = [VP, ctypes.c_int, ctypes.c_int, VP]
    out = ctypes.c_uint32(0); fn(None, a, b, ctypes.byref(out)); return out.value

def d2(name, a):                                 # unary: ctx, A=rsi, result*=rdx
    fn = getattr(lib, name); fn.restype = None
    fn.argtypes = [VP, ctypes.c_int, VP]
    out = ctypes.c_uint32(0); fn(None, a, ctypes.byref(out)); return out.value
```

Every value below was produced by an actual call into the shipped binary; the `# expect` comments
are the independently-computed oracle truth, and all matched:

```
## FAMILY arith-wrap    module__xdref_add_16_16_16 @0x858480
  add_16_16_16(0x7000, 0x2000) = 0x9000     # 0x9000
  add_16_16_16(0xFFFF, 0x0002) = 0x0001     # wrap mod 2^16
  add_8_8_8   (0xF0,   0x20)   = 0x10       # wrap mod 2^8

## FAMILY sat-arith     module__xdref_adds_16_16_16 @0x85aa10
  adds_16_16_16(0x7000, 0x2000) = 0x7fff    # +overflow clamp -> INT16_MAX
  adds_16_16_16(0x8000, 0x8000) = 0x8000    # -overflow clamp -> INT16_MIN
  subs_16_16_16(0x8000, 0x0001) = 0x8000    # -overflow clamp

## FAMILY signed-minmax module__xdref_min_16_16_16 @0x8584b0
  min_16_16_16 (0x8000, 0x0001) = 0x8000    # signed: -32768 < 1
  max_16_16_16 (0x8000, 0x0001) = 0x0001    # signed: max = 1
  minu_16_16_16(0x8000, 0x0001) = 0x0001    # unsigned: 0x8000 > 1, min = 1

## FAMILY widen-mul     module__xdref_mul_24_8_8 @0x68a800
  mul_24_8_8  (0x7F, 0x7F) = 0x3f01         # signed 127*127 = 16129
  mul_24_8_8  (0xFF, 0x02) = 0xfffffe       # signed -1*2 = -2 (& 0xffffff)
  muluu_24_8_8(0xFF, 0x02) = 0x01fe         # unsigned 255*2 = 510

## FAMILY avg           module__xdref_avg_16_16_16 @0x81d170
  avg_16_16_16 (0x0003, 0x0006) = 0x4       # floor((3+6)/2)
  avgr_16_16_16(0x0003, 0x0006) = 0x5       # round((3+6)/2)

## FAMILY abs/neg       module__xdref_abs_16_16 @0x82d060 (unary)
  abs_16_16(0x8000) = 0x8000                # wrap: |INT16_MIN| stays INT16_MIN
  abs_16_16(0xFFFF) = 0x0001                # |-1| = 1
  neg_16_16(0x0001) = 0xffff                # -1

## FAMILY convert       module__xdref_sext_32_16 @0x858680 (unary)
  sext_32_16(0x8001) = 0xffff8001           # s16 -32767 -> 32-bit
  sext_32_16(0x0042) = 0x00000042           # 66

## FAMILY bitcount      module__xdref_popc_8_8 @0x832df0 (unary)
  popc_8_8(0xFF) = 0x8                       # 8 bits set
  popc_8_8(0x0F) = 0x4                       # 4 bits set

## FAMILY soft-float-convert  module__xdref_cvtf32_1_32f_16f @0x5b77f0 (dual-out)
  cvtf32(0x3c00 = 1.0)     = 0x3f800000 = 1.0      flag=0x0
  cvtf32(0xc100 = -2.5)    = 0xc0200000 = -2.5     flag=0x0
  cvtf32(0x7bff = 65504.0) = 0x477fe000 = 65504.0  flag=0x0   # max normal fp16
  cvtf32(0x7c00 = +Inf)    = 0x7f800000 = +Inf     flag=0x0
  cvtf32(0x7e00 = qNaN)    = 0x7fc00000            flag=0x0   # canonical fp32 qNaN
  cvtf32(-0.0)             = 0x80000000 = -0.0     flag=0x0   # sign preserved
```

The soft-float convert closes the loop: `1.0`, `-2.5`, `65504.0`, `+Inf` all widen exactly, `qNaN`
maps to the canonical `0x7fc00000`, and `-0.0` preserves its sign bit — a full IEEE-754
binary16→binary32 widen with zero hardware FP, computed in-process. These calls are the live proof
that `libfiss-base.so` **is** the executable value oracle.

> **QUIRK — the leaves are pure and re-entrant.** Every body read here has no global state, no
> `.data` access, and an unused `%rdi` context slot; the result is a deterministic function of the
> operand bits alone. That is precisely what makes the 864 leaves usable as a differential-test
> arbiter: the same operand vector drives the same lane value on every call, every run, every
> machine.

---

## 7. Honesty ledger

**`[HIGH/OBSERVED]`** (disassembled bytes and/or live `ctypes` calls):

- 864 `module__xdref_*` leaves; 20 384 dynsym lines / 20 379 defined / 5 libc-only imports;
  12 569 `slotfill__` decoders; 1 708 `opcode__` execute bodies; 0 `nx_*_interface` callbacks.
- The naming grammar `<op>_<outW>_<inAW>[_inBW]…[suffix]` with widths in output-first order;
  confirmed against `add`/`adds`/`min`/`mul_24_8_8`/`sext`/`cvtf32` bodies.
- The lane ABI: `%rdi` = context, `%rsi` = A, `%rdx` = B, `%rcx` = result (3-op); `%rdx` = result
  (unary); dual-out converts write a flag through `%rdx` and the result through `%rcx`.
- Bodies: wrap = mask-to-width; saturate = 17-bit overflow-detect + `0x7fff`/`0x8000` clamp; signed
  min/max = sign-bias + unsigned cmp; unsigned = bare cmp; widening multiply with signedness in the
  extension prologue; soft-float convert = integer-only IEEE field logic.
- Vector execute = scalar per-lane loop: `addnx16t__stage_5` issues 32× `add_16_16_16` + 32×
  `bitkillt_16_2`. Scalar `opcode__add__stage_14` computes natively (slots 0x4c/0x54 → 0x28).
- 9 leaves driven live across 9 families; every value matched the independently-computed oracle.

**`[MED/INFERRED]`**:

- Family-boundary counts in §2 are indicative (the multi-token grammar makes a leaf belong to
  several slices); the *symbols* listed are exact.
- The exact fp rounding-mode source for the fp-add leaves is the `%r8`/`%r9` status/round context
  (the OUT status word and ctx arg are OBSERVED; the full round-tie-break table was not enumerated).

**`[LOW]`**:

- The `recip0`/`rsqrt0`/`sqrt0`/`div0`/`nexp0` Newton-seed exact polynomial coefficients (named as
  SEED primitives; bodies not disassembled).
- The reduction-tree exact lane-pairing order in `radd`/`rmin`/`rmax` bodies (confirmed to load all
  32 lanes; pairing order not enumerated).

> **CORRECTION (restated).** The float census is **180 / 684**, not the 184 / 680 of an earlier
> hand-count — grounded on the binary by `nm -D … | rg -c '[0-9]+t?f(_\|$)'`. Cite 180 float-token
> leaves.

---

### See also

| Page | Relationship |
|---|---|
| [fiss Surface & Exceptions](./fiss-surface-exceptions.md) | the `864 / 12 569 / 20 379` superset reconciliation; the fiss surface map |
| [cas Vector-Arithmetic Semantics](./cas-arith-sem.md) | delegates `add`/`sub`/`min`/`max`/`abs` values here |
| [cas MAC / FMAC](./cas-mac-fmac.md) | delegates the multiply / mul-add / mul-sub matrix here |
| [cas Convert / Pack / FP](./cas-convert-pack-fp.md) | delegates the convert / pack / soft-float values here |
| [ISS Oracle Synthesis](./iss-oracle-synthesis.md) | the capstone that consumes this page as the value anchor |
| [Formal ISA Model](../isa/semantics/formal-isa-model.md) | the element-semantics specification this oracle realises |
