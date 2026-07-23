# VAL — MAC / Multiply Family

> **Scope.** This page proves the **MAC / multiply family** (`ivp_sem_multiply`
> and its `MUL` / `MULA` / `MULS` / `MULP` / `SQRP` / `DECNEGW` / `PACK` leaves)
> **bit-exact** against the 4-oracle differential of
> [four-oracle-method](four-oracle-method.md) — SEM, FLIX, nki-0.3.0, and
> **libfiss-base LIVE via `ctypes`**. Every hex result below is a byte-for-byte
> reproduction of an actual call into the shipped simulator, not a hand model.
> The accumulator is the **1536-bit / 48-dword / 192-byte `wvec`** file; the
> committed [cas-mac-fmac](../iss/cas-mac-fmac.md) page is authoritative on its
> shape and this page does not re-derive it — it *exercises* it.

**Binary under test (nm-grounded, never the decompile):**

```
tools/ncore2gp/config/libfiss-base.so
  ELF 64-bit LSB shared object, x86-64, not stripped, 12,330,016 bytes
  .text   VMA 0x190430 == file-offset 0x190430  (objdump addrs are file-exact)
  .rodata VMA 0x88ff00 == file-offset 0x88ff00
```

`libfiss-base.so` is the **functional ISS** for the Vision-Q7 GPSIMD core,
compiled for the x86-64 host. Each ISA intrinsic lowers to one or more
`module__xdref_*` **reference leaves** ("xdref" = cross-difference reference):
small, side-effect-free C functions that compute the *exact* lane arithmetic.
Because they are exported `T` symbols with a stable System-V ABI, they are
callable **directly from Python `ctypes`** with no harness — which is precisely
what makes the differential *live* rather than re-modelled.

---

## 1. What the oracle pins, and why this family is hard

The four oracles ([four-oracle-method](four-oracle-method.md)) must agree on
**every byte** of the MAC datapath. The MAC family is the hardest to pin because
it has three independent sources of silent divergence, each of which a naive
reimplementation gets wrong:

1. **Accumulator headroom.** Products land in a *wider* accumulator than the
   source width (8→24, 16→48, 32→96 bits). Get the width wrong and a long
   `MULA` chain wraps at a different bit than the hardware.
2. **Read-out policy.** The *same* 24-bit accumulator can be drained with
   **`PACKL` (truncate, wrap)** or **`PACKVR` (round + saturate)**. These differ
   for *every* out-of-range value, and the difference is invisible until an
   accumulator overflows the destination width.
3. **Sign / complex bookkeeping.** `MULS` (subtract-accumulate), the complex
   (`…c…`) and conjugate (`…j…`) cross-adds, and `DECNEGW` all do two's-complement
   carry-correction across dword boundaries by hand. An off-by-one in the
   borrow propagation is a one-LSB error that only shows up on negative
   accumulators.

This page drives the real leaves through an **edge corpus** that lights up all
three, and shows the byte-exact output.

---

## 2. The leaf map (nm-grounded)

The family is exported as four tiers. Counts are from
`nm -D --defined-only libfiss-base.so \| rg <pat>` against the **one** binary; every leaf below
is `[HIGH/OBSERVED]`.

| ISA op | xdref leaf (representative) | semantics |
|---|---|---|
| `MUL` (widen, no acc) | `module__xdref_mul_24_8_8` | `(int8 a · int8 b) & 0xFFFFFF` |
| `MULA` (mul-add into acc) | `module__xdref_mula_24_24_8_8` | `acc = (acc + a·b) & 0xFFFFFF` |
| `MULS` (mul-sub into acc) | `module__xdref_muls_24_24_8_8` | `acc = (acc − a·b) & 0xFFFFFF` |
| `MULP` (sum-of-2-products) | `module__xdref_mulp_24_8_8_8_8` | `(a·b + c·d) & 0xFFFFFF` |
| `SQRP` (sum-of-squares) | `module__xdref_sqrp_24_8_8` | `(a·a + b·b) & 0xFFFFFF` |
| `DECNEGW` (carry fix-up) | `module__xdref_decnegw_24_24` | `if acc<0: acc=(acc−1)&mask` |
| `PACKL` (truncate read-out) | `module__xdref_packl_8_24` | `out = acc & 0xFF` (wrap) |
| `PACKVR` (round+sat read-out) | `module__xdref_packvr_8_24_32` | round by `sh`, saturate to dst |

The **width grid** is the spine of the family. Every integer form ships at three
widths plus complex and floating-point variants (`nm` excerpt, sorted):

```
module__xdref_mul_24_8_8       module__xdref_mula_24_24_8_8     module__xdref_muls_24_24_8_8
module__xdref_mul_48_16_16     module__xdref_mula_48_48_16_16   module__xdref_muls_48_48_16_16
module__xdref_mul_96_32_32     module__xdref_mula_96_96_32_32   module__xdref_muls_96_96_32_32
module__xdref_mul_96c_32c_32c  module__xdref_mula_96c_96c_32c_32c   (complex)
module__xdref_mula_96c_96c_32j_32c                                  (conjugate, "j")
module__xdref_mula_96_96_64c_64c_c_0 / _c_1                         (64-bit complex, two halves)
module__xdref_mul_1_1_1_1_16f_16f_16f_2   module__xdref_mula_1_1_1_1_16f_16f_16f_16f_2   (fp16)
module__xdref_mul_1_1_1_1_32f_32f_32f_2   module__xdref_mula_1_1_1_1_32f_32f_32f_32f_2   (fp32)
```

> **NOTE — the width grid *is* the headroom rule.** Read the signature as
> `<accW>_<accW>_<srcW>_<srcW>` for `MULA`/`MULS`, `<accW>_<srcW>_<srcW>` for
> `MUL`. The accumulator is **3× the source width** in every integer row:
> `8→24`, `16→48`, `32→96`. That 3× is exactly enough to hold the full
> `srcW·srcW` product (`2·srcW` bits) **plus** ≥`srcW` bits of accumulation
> headroom before the modular mask bites. The fp forms carry width `1` — they
> are per-lane soft-float, **not** widened (see [fp-soft-float](fp-soft-float.md),
> [convert-pack-cast](convert-pack-cast.md)).

---

## 3. The calling convention (the live `ctypes` ABI)

Every leaf follows the same System-V pattern, recovered from the disassembly:

```
arg0 = %rdi   self / ISS context pointer — IGNORED by the pure leaves
arg1 = %rsi   first operand (or accumulator pointer for 48/96-bit forms)
arg2 = %rdx   operand
arg3 = %rcx   operand
arg4 = %r8    out* (24-bit forms) / out* (wide forms)
```

> **GOTCHA — `%rdi` is a dead parameter on the scalar leaves.** The 24-bit forms
> never touch `%rdi`; it is the ISS `this` pointer threaded through every leaf so
> the *same* call shape works for stateful intrinsics. From `ctypes` you may pass
> any value — `0`, or even garbage:
>
> ```python
> mula(0xdeadbeef, 0x100, 127, 127, byref(out))   # -> 0x4001, identical to mula(0, …)
> ```
>
> Do **not** mistake `%rdi` for the first operand: that off-by-one-arg error
> silently shifts every operand and is the most common way to mis-drive this
> binary live.

The wide (48/96-bit) accumulator forms take **`acc*` and `out*` as pointers** to
packed-dword arrays — 48-bit = `low dword + high word` (8 bytes), 96-bit =
`3 dwords` (12 bytes). The host-visible packing is little-endian dword order.

---

## 4. The 24-bit `MUL`/`MULA`/`MULS` core — disassembly → pseudocode

The 24-bit leaves are short enough to read whole. From
`objdump -d` at the nm address (`.text` VMA==file-offset, so the address is the
file address):

```asm
; module__xdref_mul_24_8_8 @ 0x68a800   ; out = (int8 a)·(int8 b), masked 24b
  movsbl %sil,%esi          ; a = sign_extend8(arg1)
  movsbl %dl,%edx           ; b = sign_extend8(arg2)
  imul   %esi,%edx          ; p = a*b   (32-bit, can't overflow: |p|<=16384)
  and    $0xffffff,%edx     ; p &= (2^24 - 1)
  mov    %edx,(%rcx)        ; *out = p
  ret

; module__xdref_mula_24_24_8_8 @ 0x68a820  ; acc += a·b, masked 24b
  movsbl %dl,%edx           ; a = sign_extend8(arg2)
  movsbl %cl,%ecx           ; b = sign_extend8(arg3)
  imul   %ecx,%edx          ; p = a*b
  add    %edx,%esi          ; acc(arg1) += p
  and    $0xffffff,%esi     ; acc &= 0xFFFFFF
  mov    %esi,(%r8)         ; *out = acc
  ret
```

Reproduced as annotated pseudocode naming the **real** symbols:

```c
// module__xdref_mul_24_8_8(self, int8 a, int8 b, uint32 *out)
*out = ((int32)(int8)a * (int32)(int8)b) & 0xFFFFFF;

// module__xdref_mula_24_24_8_8(self, uint32 acc, int8 a, int8 b, uint32 *out)
*out = (acc + (int32)(int8)a * (int32)(int8)b) & 0xFFFFFF;     // wrap, NO saturate

// module__xdref_muls_24_24_8_8(self, uint32 acc, int8 a, int8 b, uint32 *out)
*out = (acc - (int32)(int8)a * (int32)(int8)b) & 0xFFFFFF;
```

> **QUIRK — accumulate-time arithmetic wraps modulo 2²⁴; it never saturates.**
> Saturation is *exclusively* a read-out concern (`PACKVR`, §6). The 24-bit
> accumulator is a pure `Z/2²⁴` ring during the chain; this is why a `MULA` chain
> can transiently exceed the destination width and still be correct once
> `PACKVR`-clamped at the end. A reimplementation that saturates *inside* `MULA`
> will diverge on any chain that overshoots and comes back.

### LIVE — 24-bit corpus (byte-exact)

```python
import ctypes
L = ctypes.CDLL("tools/ncore2gp/config/libfiss-base.so")
u32 = ctypes.c_uint32; P = ctypes.POINTER(u32); i = ctypes.c_int32
def bind(n, a): f = getattr(L, n); f.restype = None; f.argtypes = a; return f

mul  = bind("module__xdref_mul_24_8_8",     [i, i, i, P])
mula = bind("module__xdref_mula_24_24_8_8", [i, i, i, i, P])
muls = bind("module__xdref_muls_24_24_8_8", [i, i, i, i, P])
o = u32(0)
```

| call | result | check |
|---|---|---|
| `mul_24_8_8(0, 0)` | `0x000000` | zero |
| `mul_24_8_8(7, -3)` | `0xffffeb` | sign mix: −21 in 24-bit two's-comp |
| `mul_24_8_8(127, 127)` | `0x003f01` | max-mag positive (16129) |
| `mul_24_8_8(-128, -128)` | `0x004000` | max-mag (16384) — fits in 24b |
| `mula_24_24_8_8(acc=0, 127, 127)` | `0x003f01` | seed accumulate |
| `muls_24_24_8_8(acc=0x100, 7, 3)` | `0x0000eb` | `0x100 − 21 = 0xeb` |

All four oracles return these exact bytes; the table is the libfiss-base column,
verified live.

---

## 5. Wide-accumulator `MULA`/`MULS` chains (48 / 96 bit)

The 48-bit forms keep the accumulator as `low32 ⊕ high16` and reconstruct the
add/sub by hand. From `module__xdref_muls_48_48_16_16 @ 0x68a840`, the borrow
fix-up idiom is the give-away:

```asm
  mov    (%rsi),%eax          ; acc_lo = acc[0]
  movswl %cx,%ecx ; movswl %dx,%edx ; imul %ecx,%edx   ; p = (int16)a * (int16)b
  test   %eax,%eax ; je .. ; sub $1,%eax ; lea 1(%rax),%rcx  ; zero-extend acc_lo to 64b
  mov    0x4(%rsi),%eax ; shl $0x20,%rax ; or %rcx,%rax       ; acc = acc_hi<<32 | acc_lo
  ...                                                          ; build sign-extended 64b p
  sub    %rdx,%rax            ; acc -= p
  mov    %eax,(%r8) ; shr $0x20,%rax ; and $0xffff,%eax ; mov %eax,4(%r8)  ; store lo32 + hi16
```

As pseudocode (the `48` accumulator is a logical 48-bit two's-comp value held in
`{lo:u32, hi:u16}`):

```c
// module__xdref_mula_48_48_16_16(self, uint32 *acc /*{lo,hi}*/, int16 a, int16 b, uint32 *out)
int64  A = ((int64)acc[1] << 32) | acc[0];      // assemble 48b acc (sign in bit47)
A = (A + (int32)(int16)a * (int32)(int16)b) & ((1LL<<48) - 1);   // MULS: A -= …
out[0] = (uint32) A;  out[1] = (uint32)(A >> 32) & 0xFFFF;
```

### LIVE — 48-bit and 96-bit accumulate chains

```python
mula48 = bind("module__xdref_mula_48_48_16_16", [i, P, i, i, P])
acc = (u32 * 2)(0, 0)
for a, b in [(100, 100), (200, 50), (-30000, -30000)]:
    mula48(0, ctypes.cast(acc, P), a, b, ctypes.cast(acc, P))
# 100*100 + 200*50 + 30000*30000 = 900,020,000   -> acc = 900020000 (exact, live)

mula96 = bind("module__xdref_mula_96_96_32_32", [i, P, i, i, P])
a96 = (u32 * 3)(0, 0, 0)
mula96(0, ctypes.cast(a96, P), 0x40000000, 0x40000000, ctypes.cast(a96, P))
# 0x40000000^2 = 0x1000000000000000   -> 96-bit acc holds it with 32b headroom to spare
```

| chain | accumulated value | encoding |
|---|---|---|
| 48b: `100·100 + 200·50 + (−30000)²` | `900020000` | fits 48b, no wrap |
| 96b: `(2³⁰)²` | `0x1000000000000000` (2⁶⁰) | fits 96b with 32-bit headroom |

> **NOTE — headroom is the whole point of the wide file.** A 16-bit `MULS` chain
> accumulating `N` products of `(2¹⁵)²` magnitude grows to `N·2³⁰`. The 48-bit
> accumulator survives `N` up to ≈`2¹⁷` before the modular mask is observable —
> that is the **24/48/96-bit wvec headroom**. The physical store of these
> per-lane accumulators is the **4-entry × 1536-bit `wvec`** file
> (`opnd_sem_wvec_addr = & 0x3`; `wvt_def` copies 48 dwords = 192 B); see
> [cas-mac-fmac §7](../iss/cas-mac-fmac.md) and [b10-wvec-pack](../isa/ref/b10-wvec-pack.md).
> One 1536-bit `wvec` entry holds **64 × 24-bit**, or **32 × 48-bit**, or
> **16 × 96-bit** lane accumulators — the xdref leaves above compute *one lane's*
> slice of that file.

---

## 6. The read-out: `PACKL` truncate **vs** `PACKVR` round + saturate

This is the divergence that breaks naive reimplementations, so it gets its own
proof. The accumulator is the same 24-bit value; the two read-out leaves differ
for **every** out-of-destination-range input.

### 6.1 `PACKL` — truncate (wrap), no saturation

```asm
; module__xdref_packl_8_24 @ 0x5e94d0
  and    $0xff,%esi         ; out = acc & 0xFF   — drop everything above bit 7
  mov    %esi,(%rdx)
  ret
```

```c
// module__xdref_packl_8_24(self, uint32 acc, uint8 *out)  -> low byte, WRAPS
*out = acc & 0xFF;
// module__xdref_packl_16_48_nosat: out = (uint16)acc[0]   — explicit "_nosat" twin
```

> **QUIRK — `PACKL` is a *bit-slice*, not a value-clamp.** It keeps the low `dstW`
> bits of the ring value and discards the sign. `+256` (`0x100`, in-range as a
> 24-bit value) reads out as `0x00`. There is an explicit `packl_16_48_nosat`
> sibling, whose `_nosat` name is itself the binary's confirmation that the
> default `PACK*` policy *does* clamp.

### 6.2 `PACKVR` — round, then saturate

```asm
; module__xdref_packvr_8_24_32 @ 0x5e94e0
  cmp    $0x17,%edx ; cmova %ecx,%edx    ; sh = min(shift, 23)         — clamp shift
  mov    $1,%eax ; shl %cl,%eax ; shr $1,%eax ; add %eax,%esi          ; acc += (1<<sh)>>1  (round bit)
  shl/sar ...                                                          ; arithmetic shift acc>>sh
  ... cmp %cx,%dx ; je ...                                             ; overflow test vs dst range
  ...                                                                  ; on overflow: replace with signed max/min
```

```c
// module__xdref_packvr_8_24_32(self, int24 acc, int sh, int8 *out)
sh   = min(sh, 23);                       // shift saturates at acc width − 1
acc += (1 << sh) >> 1;                     // round-to-nearest (add half-ULP)
v    = arith_shift_right(acc, sh);         // scale down
*out = sat_signed8(v);                     // clamp to [-128, +127]
```

### 6.3 LIVE — the same accumulator, two read-outs

```python
packl    = bind("module__xdref_packl_8_24",      [i, i, P])      # truncate
packvr   = bind("module__xdref_packvr_8_24_32",  [i, i, i, P])   # round+sat (8-bit dst)
packvr16 = bind("module__xdref_packvr_16_24_32", [i, i, i, P])   # round+sat (16-bit dst)
```

| accumulator | `PACKL` (truncate) | `PACKVR` (round + sat) | divergence |
|---|---|---|---|
| `0x100` = +256 | `0x00` | `0x7f` (=+127) | **256 vs −clamp** |
| `0x80` = +128 | `0x80` (=−128!) | `0x7f` | sign flip vs clamp |
| `0x7f` = +127 | `0x7f` | `0x7f` | agree (in range) |
| `0xE000` = +57344 → 16-bit dst | — | `0x7fff` | `packvr_16_24_32` sat |

Reading the first row aloud: `PACKL(0x100) = 0x00` silently throws the value
away; `PACKVR(0x100, sh=0) = 0x7f` correctly reports "overflow, clamped to max".
The hardware default for a saturating store is `PACKVR`; `PACKL` is the
deliberate "I want the raw low bits" path. **The worked saturate example
— two operands of `0x7000` summing past the 16-bit window — resolves to
`0x7fff` through the `PACKVR_*_16` read-out**, which is exactly the
`packvr_16_24_32(0xE000) = 0x7fff` row above.

> **CORRECTION — `PACKVR` is *not* "`PACKL` plus a round bit".** It is
> `round → scale → saturate`; for any value outside the destination range the
> two produce *unrelated* bytes (`0x00` vs `0x7f` for `+256`). Modelling
> `PACKVR` as truncate-with-rounding — a common shortcut — is wrong on every
> overflow and on the `+128`→sign-flip boundary. See [b10-wvec-pack](../isa/ref/b10-wvec-pack.md).

---

## 7. `SQRP`, `MULP`, complex / conjugate, and `DECNEGW`

### 7.1 `MULP` / `SQRP` — sum of products

`SQRP` is built on `MULP`. From `module__xdref_sqrp_24_8_8 @ 0x833140`, it calls
`module__xdref_mulp_24_8_8_8_8`, passing `arg1→pair0` and `arg2→pair1`:

```asm
; module__xdref_mulp_24_8_8_8_8 @ 0x8164d0   ; out = a·b + c·d
  call module__xdref_mul_24_8_8      ; t0 = mul(c, d)   (saved at 8(%rsp))
  call module__xdref_mul_24_8_8      ; t1 = mul(a, b)
  mov 0x8(%rsp),%eax ; add 0xc(%rsp),%eax ; and $0xffffff,%eax   ; out = (t0 + t1) & mask
```

```c
// module__xdref_mulp_24_8_8_8_8(self, a, b, c, d, out) -> a·b + c·d  (sum of two products)
// module__xdref_sqrp_24_8_8(self, a, b, out)          -> a·a + b·b   (sum of squares)
```

> **GOTCHA — `SQRP(a, b)` is `a² + b²`, *not* `a²` and *not* `2a²`.** The leaf
> wires `arg1` into both factors of product-0 and `arg2` into both factors of
> product-1, so it is the **sum-of-squares** primitive (complex magnitude
> `re² + im²`). Naming it after a single square is the trap. Verified live:
> `sqrp_24_8_8(5, 12) = 0x0000a9 = 169 = 5² + 12²`.

### 7.2 Complex / conjugate cross-add

The complex MAC `module__xdref_mula_96c_96c_32c_32c @ 0x8336f0` calls
`module__xdref_mul_96c_32c_32c`, then carry-corrects a **3-dword** add for the
real and imaginary halves separately (the `test/je/sub/lea` borrow idiom per
dword). The conjugate form `…_32j_32c` flips the sign of the cross term so that
`(a+bi)·conj(c+di) = (ac+bd) + (bc−ad)i` instead of the straight product. Both
are byte-checked against SEM / nki-0.3.0 on the complex corpus.

```c
// straight   mula_96c…32c_32c : acc.re += a.re·b.re − a.im·b.im
//                               acc.im += a.re·b.im + a.im·b.re
// conjugate  mula_96c…32j_32c : acc.re += a.re·b.re + a.im·b.im
//                               acc.im += a.im·b.re − a.re·b.im
```

`[MED/INFERRED]` for the exact term-to-cross-add wiring (read from the call
graph and the dword borrow chains; the per-term sign is confirmed against the
conjugate leaf's differing `sub`/`add` placement, not yet byte-traced lane by
lane).

### 7.3 `DECNEGW` — the wide-accumulator carry fix-up

```asm
; module__xdref_decnegw_24_24 @ 0x834110
  test   $0x800000,%esi      ; is bit 23 (sign) set?
  je     .done               ; no -> pass through unchanged
  sub    $1,%esi             ; yes -> acc -= 1
  and    $0xffffff,%esi      ;        re-mask to 24b
.done:
  mov    %esi,(%rdx)
  ret
```

```c
// module__xdref_decnegw_24_24(self, uint32 acc, uint32 *out)
*out = (acc & 0x800000) ? ((acc - 1) & 0xFFFFFF) : acc;   // DEC if NEG, on the Wide acc
```

> **NOTE — `DECNEGW` = "decrement if negative, in the wide file".** It is the
> rounding/carry correction applied to a `wvec` accumulator before read-out
> (the magnitude-correction step in the round-half-away-from-zero path). The
> 48/96-bit forms apply the same `−1` across the multi-dword value with explicit
> borrow propagation. Verified live: `decnegw_24_24(0x800000) = 0x7fffff`
> (the most-negative 24-bit value decrements to `+max` after masking),
> `decnegw_24_24(0xfffffb /*−5*/) = 0xfffffa /*−6*/`,
> `decnegw_24_24(0x000005 /*+5*/) = 0x000005` (positive untouched).

---

## 8. Reproducible driver (one self-contained script)

The entire corpus is one file; running it prints the bytes embedded in this
page. Nothing is pickled; the binary is loaded directly.

```python
import ctypes
BIN = "tools/ncore2gp/config/libfiss-base.so"   # absolute path on your tree
L   = ctypes.CDLL(BIN)
u32 = ctypes.c_uint32; P = ctypes.POINTER(u32); i = ctypes.c_int32
def b(n, a): f = getattr(L, n); f.restype = None; f.argtypes = a; return f
o = u32(0)

for nm_, args in [
    ("module__xdref_mul_24_8_8",      (0, 7, -3)),       # 0xffffeb
    ("module__xdref_mula_24_24_8_8",  (0, 0, 127, 127)), # 0x003f01
    ("module__xdref_muls_24_24_8_8",  (0, 0x100, 7, 3)), # 0x0000eb
    ("module__xdref_sqrp_24_8_8",     (0, 5, 12)),       # 0x0000a9
    ("module__xdref_decnegw_24_24",   (0, 0x800000)),    # 0x7fffff
    ("module__xdref_packl_8_24",      (0, 0x100)),       # 0x000000 (wrap)
    ("module__xdref_packvr_8_24_32",  (0, 0x100, 0)),    # 0x00007f (sat)
]:
    f = b(nm_, [i]*len(args) + [P]); f(*args, ctypes.byref(o))
    print(f"{nm_:32s}{args} = 0x{o.value:06x}")
```

Captured output (this is the libfiss-base **LIVE** column — every other oracle
returns identical bytes):

```
module__xdref_mul_24_8_8        (0, 7, -3) = 0xffffeb
module__xdref_mula_24_24_8_8    (0, 0, 127, 127) = 0x003f01
module__xdref_muls_24_24_8_8    (0, 0x100, 7, 3) = 0x0000eb
module__xdref_sqrp_24_8_8       (0, 5, 12) = 0x0000a9
module__xdref_decnegw_24_24     (0, 0x800000) = 0x7fffff
module__xdref_packl_8_24        (0, 0x100) = 0x000000
module__xdref_packvr_8_24_32    (0, 0x100, 0) = 0x00007f
```

---

## 9. Adversarial self-verify

The five strongest claims and their grounding:

1. **The accumulator is 1536-bit / 48-dword / 192 B.** `[HIGH/CARRIED]` —
   Carried from [cas-mac-fmac §7](../iss/cas-mac-fmac.md)
   (`wvt_def @0x17a80b0`, 48-dword copy, off-by-one 48-not-47 GOTCHA), the
   authoritative page; this page does not re-derive it. The **per-lane**
   xdref leaves here operate on the 24/48/96-bit *slice* of that file, never on
   the full 1536 bits at once (§5 NOTE); the two views are consistent
   (64×24 = 32×48 = 16×96 = 1536).
2. **`PACKL` truncates, `PACKVR` rounds+saturates.** Live:
   `packl_8_24(0x100)=0x00`, `packvr_8_24_32(0x100,0)=0x7f`,
   `packvr_16_24_32(0xE000,0)=0x7fff`. Disassembly shows `and $0xff` (slice) vs
   `cmova` shift-clamp + round-bit + overflow `cmp` (sat).
3. **The live `ctypes` outputs are byte-exact.** Every hex
   in this page is a captured `ctypes` call into the shipped `.so`; the §8 block
   is reproducible verbatim. `.text` VMA==file-offset confirms the objdump
   addresses are file-accurate.
4. **`SQRP` is `a²+b²`.** The live call `sqrp(6,9)=117=36+81` (vs `sqrp(6,0)=36`)
   proves the second operand is a real factor, and `mulp` disassembly confirms
   `a·b + c·d` with `arg1→pair0, arg2→pair1`.
5. **`%rdi` is ignored on the scalar leaves.**
   `mula(0xdeadbeef, …) == mula(0, …)`; the 24-bit disassembly never reads
   `%rdi`. (It is *not* ignored on stateful intrinsics — only
   on these pure reference leaves.)

**Single strongest CORRECTION:** `SQRP` is **`a² + b²` (sum of squares), not
`a²` and not `2·a²`** — the leaf feeds `arg1` and `arg2` as the two independent
squared terms of the underlying `MULP` (`a·b + c·d`) primitive. Proven live by
`sqrp_24_8_8(5,12) = 0x0000a9 = 169 = 5² + 12²`.

---

## 10. Cross-references

- [four-oracle-method](four-oracle-method.md) — the SEM / FLIX /
  nki-0.3.0 / libfiss-base LIVE differential this page instantiates.
- [b04-mac-integer](../isa/ref/b04-mac-integer.md) — integer `MUL`/`MULA`/`MULS`
  ISA reference (width grid, signed forms).
- [b05-mac-mixed](../isa/ref/b05-mac-mixed.md) — mixed-width and FP MAC forms.
- [cas-mac-fmac](../iss/cas-mac-fmac.md) — **authoritative** on the 1536-bit
  4-entry `wvec` file, `wvt_def` 48-dword copy, and the `ivp_sem_multiply`
  four-host-port lowering.
- [b10-wvec-pack](../isa/ref/b10-wvec-pack.md) — the `PACKL` / `PACKVR` read-out
  ISA reference.
- [pe-matmul](../firmware/kernels/pe-matmul.md) — the firmware matmul kernel that
  drives this MAC family at full-tile (1536-bit) granularity.
