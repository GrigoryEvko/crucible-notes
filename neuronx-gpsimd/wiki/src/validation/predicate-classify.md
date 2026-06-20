# VAL — Predicate / Classify / Compare Family

> **Scope.** This page proves the **predicate / vbool + classify / compare**
> family — the boolean datapath that drives *every* predicated execution on the
> Vision-Q7 GPSIMD core — **bit-exact** against the 4-oracle differential of
> [four-oracle-method](four-oracle-method.md): GX-SEM (the IEEE / two's-complement
> RTL model), GX-FLIX (the device bundle-decode identity), nki-0.3.0 (the numpy
> simulator), and **`libfiss-base` LIVE via `ctypes`** — the shipped ISS binary
> *itself* computing every `vbool`, every 8-bit class mask, every FS-bank result.
> Each hex below is a byte-for-byte reproduction of an actual call into the
> shipped simulator, not a hand model. The committed
> [cas-predicate-boolean](../iss/cas-predicate-boolean.md) page is authoritative
> on the `vbool` register-file shape and the cas timing; this page does not
> re-derive them — it *exercises* the value half against the binary on the hard
> IEEE edges (NaN / sNaN / denorm / signed-zero) where the references *can*
> legitimately diverge, so a bit-exact result is a real proof rather than a
> tautology.

**Binary under test (nm-grounded, never the decompile):**

```
tools/ncore2gp/config/libfiss-base.so
  ELF 64-bit LSB shared object, x86-64, not stripped, 12,330,016 bytes
  nm -D --defined-only … | rg -c 'module__xdref_'   = 864   (== the static count: every leaf is dynamic ⇒ ctypes-callable)
  .text   VMA 0x190430 == file-offset 0x190430   (objdump addrs are file-exact)
  .rodata VMA 0x88ff00 == file-offset 0x88ff00
  .data.rel.ro / .data carry the +0x200000 delta (irrelevant here — every xdref body is in .text)
```

Every `module__xdref_*` value leaf named below is an exported `T` symbol present
in the **dynamic** table — so it is callable directly from Python `ctypes` with
no harness, which is exactly what makes the differential *live* rather than
re-modelled. The family fuses the two predicate xdref roots the live-oracle
passes opened — the predicate/vbool half and the classify/compare half — into
one differential.

> **NOTE.** Every "16f / 32f", "1/2/4-bit field", "FS0..FS7", "2NX8 / NX16 /
> N_2X32", "sNaN / mantissa-MSB" token below is an **IEEE-width / mask-field-width
> / flag-bank-index / datapath-lane axis of the single Cairo config**
> (`Xm_ncore2gp`, Xtensa24, NX1.1.4, RI-2022.9). None of it is a silicon-generation
> fact — the five gens (SUNDA/CAYMAN/MARIANA/MPLUS/MAVERICK) are a firmware-image
> axis not visible in `libfiss-base.so`. In particular **`FS0..FS7` are the EIGHT
> on-core flag-state registers of this config, not eight generations.**

---

## 1. What the oracle pins, and why this family is hard

The four oracles must agree on **every byte** of the boolean datapath. Unlike the
integer ALU of [four-oracle-method](four-oracle-method.md) — which has *no free
parameter* — the predicate family carries genuine IEEE subtleties where a naive
reimplementation silently diverges:

1. **NaN ordering.** An ordered compare (`oeq`/`olt`/`ole`) is **FALSE** on any
   NaN; an unordered compare (`un`/`ueq`/`ult`/`ule`) is **TRUE** on any NaN. Get
   the polarity backwards and a Dropout mask, a min-reduce sentinel, or a
   select-blend all flip on exactly the values where it matters.
2. **sNaN vs qNaN.** The *sole* discriminator is the **mantissa MSB** (bit 9 of
   fp16, bit 22 of fp32). A classifier that tests "exp==max && mant!=0" gets
   *nan* right but cannot tell signalling from quiet — and the 8-bit class mask
   ships both `nan` and `snan` bits.
3. **vbool field width.** The compare result is replicated across **bytes-per-lane**:
   an 8-bit-lane compare emits a **1-bit** field (`0x1`), a 16-bit lane a **2-bit**
   field (`0x3`), a 32-bit lane a **4-bit** field (`0xf`). This is *structural*
   (a distinct leaf per width), never a runtime mode.
4. **Signed vs unsigned compare.** `lt`/`le` sign-bias **both** operands then do
   an unsigned compare; `ltu`/`leu`/`eq`/`neq` are bare. Distinct opcodes, never a
   signedness flag. The separating cell is `lt(0x8000,1)=0x3` (signed −32768<1 TRUE)
   vs `ltu(0x8000,1)=0x0` (unsigned 32768<1 FALSE).
5. **`_t` merge, not zero-fill.** The predicated-tail (`_t`) forms keep the
   **prior destination** in killed lanes — a blend, proven by the `bitkillt`
   opposite-polarity mask (§6). A reimplementation that zero-fills killed lanes is
   wrong for *every* `_t` op on this core.

This page drives the real leaves through an edge corpus that lights up all five,
and shows the byte-exact output. The grand result: **7,274 grid checks + ~388,000
fuzz/exhaustive comparisons, 0 mismatch** — including the fp16 classify run
**EXHAUSTIVE over all 65,536 fp16 bit patterns** with zero divergence.

---

## 2. The leaf map (nm-grounded)

The family spans six op groups. All addresses are `.text` (VMA==file-offset, so
the nm address is the objdump address) and every leaf is a dynamic `T` export.

| Group | xdref leaf (representative) | addr | semantics | tag |
|---|---|---|---|---|
| INT compare → vbool | `module__xdref_lt_2_16_16` | `0x8585c0` | signed `i16 < i16` → 2-bit field | `[HIGH/OBSERVED]` |
| INT compare (unsigned) | `module__xdref_ltu_2_16_16` | `0x858660` | unsigned `u16 < u16` → 2-bit field | `[HIGH/OBSERVED]` |
| FP compare (ordered) | `module__xdref_olt_1_1_16f_16f` | `0x521e60` | ordered fp16 `<`, NaN ⇒ FALSE | `[HIGH/OBSERVED]` |
| FP compare (unordered) | `module__xdref_un_1_1_16f_16f` | `0x522010` | pure `isUnordered(a,b)` | `[HIGH/OBSERVED]` |
| CLASSIFY | `module__xdref_clsfy_16f_16f` | `0x524b00` | fp16 → 8-bit class mask | `[HIGH/OBSERVED]` |
| MASK LOGIC | `module__xdref_andb_64_64_64` | `0x856f80` | 64-bit 2-word `A & B` | `[HIGH/OBSERVED]` |
| PREDICATION (merge) | `module__xdref_bitkillt_16_2` | `0x85cc20` | kill-on-TRUE merge mask | `[HIGH/OBSERVED]` |
| FS FLAG BANK | `module__xdref_fs0ltu_64_8_8` | `0x8328d0` | unsigned i8 LT → `FS0` | `[HIGH/OBSERVED]` |
| FS REDUCE | `module__xdref_borfs_2n_…` | `0x8327f0` | OR-reduce 8 vbool → FS | `[HIGH/OBSERVED]` |
| BIT-PLANE | `module__xdref_extbi_1_8_32` | `0x5e92e0` | extract 1 lane bit | `[HIGH/OBSERVED]` |

### 2.1 The width grid is the spine

Every compare ships at three lane widths, the **width suffix `_<outW>_<inW>_<inW>`
naming bits**: `_1_8_8` (8-bit lane, 1-bit field), `_2_16_16` (16-bit lane, 2-bit
field), `_4_32_32` (32-bit lane, 4-bit field). The full int-compare grid
(`nm -D` excerpt, 18 leaves = {lt,ltu,le,leu,eq,neq} × {8,16,32}):

```
module__xdref_lt_1_8_8   @0x5bc450   module__xdref_lt_2_16_16  @0x8585c0   module__xdref_lt_4_32_32  @0x5c0cc0
module__xdref_ltu_1_8_8  @0x5bc4f0   module__xdref_ltu_2_16_16 @0x858660   module__xdref_ltu_4_32_32 @0x5c0d50
module__xdref_le_1_8_8   @0x5bc490   module__xdref_le_2_16_16  @0x858600   module__xdref_le_4_32_32  @0x5c0cf0
module__xdref_leu_1_8_8  @0x5bc500   module__xdref_leu_2_16_16 @0x858670   module__xdref_leu_4_32_32 @0x5c0d60
module__xdref_eq_1_8_8   @0x5bc4d0   module__xdref_eq_2_16_16  @0x858640   module__xdref_eq_4_32_32  @0x5c0d30
module__xdref_neq_1_8_8  @0x5bc4e0   module__xdref_neq_2_16_16 @0x858650   module__xdref_neq_4_32_32 @0x5c0d40
```

The fp compares ship as ordered {`oeq`,`olt`,`ole`} + unordered {`un`,`ueq`,`ult`,
`ule`}, each in the field-width forms `_1_1_16f_16f` (1-bit), `_1_2_16f_16f`
(2-bit), `_1_4_32f_32f` (4-bit), plus the predicated-tail `_2f_t` / `_4f_t`
variants — 42 distinct fp-compare leaves:

```
module__xdref_oeq_1_1_16f_16f @0x521f40   module__xdref_olt_1_1_16f_16f @0x521e60   module__xdref_ole_1_1_16f_16f @0x521d70
module__xdref_oeq_1_2_16f_16f @0x5b2f50   module__xdref_olt_1_2_16f_16f @0x5b2e40   module__xdref_ole_1_2_16f_16f @0x5b2d30
module__xdref_oeq_1_4_32f_32f @0x1aec50   module__xdref_olt_1_4_32f_32f @0x1aeab0   module__xdref_ole_1_4_32f_32f @0x1ae8e0
module__xdref_un_1_1_16f_16f  @0x522010   module__xdref_ueq_1_1_16f_16f @0x51bf10   module__xdref_ult_1_1_16f_16f @0x51be10   module__xdref_ule_1_1_16f_16f @0x51bcf0
module__xdref_oeq_1_2_16f_16f_2f_t @0x5b41f0   module__xdref_olt_1_4_32f_32f_4f_t @0x528fa0      (… the predicated-tail forms)
```

> **GOTCHA — the 16f compare has NO `_1_4` form; the 4-lane fp form is the 32f
> `_1_4_32f_32f`.** fp16 lanes are 16-bit (`_1_2`, 2-bit field) at the densest fp
> compare; fp32 lanes are 32-bit (`_1_4`, 4-bit field). The `_t` predicated tails
> exist only for `_1_2_16f_16f` (`_2f_t`) and `_1_4_32f_32f` (`_4f_t`) — there is
> no `_1_1` tail (a 1-bit field is the int-lane form, not an fp lane). A
> reimplementer who binds `oeq_1_4_16f_16f` finds **no symbol**. `[HIGH/OBSERVED]`

---

## 3. The calling convention (the live `ctypes` ABI)

The per-leaf ABI **must be derived per leaf** from the width suffix *and* confirmed
by the store register in the disassembly — a misread ABI segfaults or garbages
([four-oracle-method §2.2](four-oracle-method.md)). The predicate family has **four**
distinct ABI shapes, all System-V with `%rdi` the dead ISS `xstate`:

```
INT/unordered compare : f(rdi=xstate, esi=A, edx=B,            rcx=*result)   -> void   ; store via (%rcx)
ORDERED fp _1_1       : f(rdi=xstate, esi=A, edx=B, rcx=*aux,  r8=*result)   -> void   ; store via (%r8), aux via (%rcx)
classify / bitkill    : f(rdi=xstate, esi=A,                   rdx=*result)   -> void   ; store via (%rdx)   (UNARY, 3-arg)
mask logic (2-word)   : f(rdi=xstate, rsi=*A[2], rdx=*B[2],    rcx=*out[2])   -> void   ; 64-bit, two words
fsNltu                : f(rdi=xstate, esi=A, edx=B,            rcx=*FSn)      -> void   ; relays ltu_1_8_8
extbi                 : f(rdi=xstate, esi=val, edx=bit,        rcx=*out)      -> void
injbi (RMW)           : f(rdi=xstate, esi=b, edx=pred, ecx=bit, r8=*out)     -> void   ; 5-arg bit-inject
```

> **CORRECTION (the single most important ABI fact, proven this pass).** The
> **ordered** fp compares are 5-arg: they write a **reserved aux word to `(%rcx)`
> *and* the real predicate to `(%r8)`** — two stores. `module__xdref_olt_1_1_16f_16f`
> stores the aux at `0x521f00` (`mov %edx,(%rcx)`) and the **result** at
> `0x521f32` (`mov %eax,(%r8)`); `oeq_1_1` likewise stores the result at `(%r8)`
> (`0x521ffb`). A reader who stops at the first store (`(%rcx)`) mistakes the
> reserved aux for the result and reads back **0** for every ordered predicate.
> The **bare int compares and the pure-unordered `un`** are 4-arg and store the
> result directly at `(%rcx)`. Bind `result_ptr` to `%r8` for ordered fp, `%rcx`
> for everything else. `[HIGH/OBSERVED]`

> **GOTCHA — `%rdi` (the ISS `xstate`) is dead for every leaf in this family.**
> The classify, compare, mask-logic, bitkill, FS-bank and bit-plane bodies never
> dereference `%rdi` — so the bare ctypes drive passes `NULL`/`0` and they all
> compute correctly. None of these leaves route through the ISS soft-float
> dispatch, so none hit the `recipqli` wall of
> [four-oracle-method §7](four-oracle-method.md). `[HIGH/OBSERVED]`

The classify-MSB / NaN classification is **integer-only soft-float** — no
x87/SSE; fp16 uses `exp=(x>>10)&0x1f`, `mant=x&0x3ff`, sign=`x>>15`; fp32 uses
`0xff`/`0x7fffff`/`0x7f800000`. The bit-pattern is reinterpreted from an int, so
every leaf takes `c_uint`.

---

## 4. Compare → vbool — disassembly → pseudocode

### 4.1 The signed/unsigned split — the separating pair (byte-exact)

The 16-bit forms are short enough to read whole. The two leaves differ *only* in a
sign-bias prologue:

```asm
; module__xdref_lt_2_16_16 @0x8585c0   — SIGNED i16 < i16, 2-bit vbool field
  mov    %esi,%eax ; shr $0xf,%eax ; test %eax,%eax ; sete %al   ; !sign(A)
  and    $0x7fff,%esi ; movzbl %al,%eax ; shl $0xf,%eax ; or %eax,%esi   ; A ^= sign  (bias)
  mov    %edx,%eax ; shr $0xf,%eax ; test %eax,%eax ; sete %al   ; !sign(B)
  and    $0x7fff,%edx ; movzbl %al,%eax ; shl $0xf,%eax ; or %eax,%edx   ; B ^= sign  (bias)
  cmp    %edx,%esi ; sbb %eax,%eax        ; eax = -1 if (biased A) < (biased B) else 0
  and    $0x3,%eax                        ; pack into the 2-bit lane field
  mov    %eax,(%rcx)                       ; *result
  ret

; module__xdref_ltu_2_16_16 @0x858660   — UNSIGNED u16 < u16  (NO sign-bias)
  cmp    %edx,%esi ; sbb %eax,%eax ; and $0x3,%eax ; mov %eax,(%rcx) ; ret
```

```c
// module__xdref_lt_2_16_16(self, uint16 A, uint16 B, uint32 *result)
//   sign-bias maps signed order onto unsigned order: flip the MSB = invert the sign.
uint16 a = A ^ 0x8000, b = B ^ 0x8000;        // bias both operands
*result = (a < b) ? 0x3 : 0x0;                // 2-bit field replicate (0x3 = TRUE)

// module__xdref_ltu_2_16_16(self, uint16 A, uint16 B, uint32 *result)
*result = (A < B) ? 0x3 : 0x0;                // bare unsigned, no bias
```

> **QUIRK — the field width IS the bytes-per-lane, replicated.** The `and $0x3`
> packs the boolean into a **2-bit** field for the 16-bit lane. The 8-bit leaf
> ends `and $0x1` (1-bit, `0x1`), the 32-bit leaf `and $0xf` (4-bit, `0xf`). This
> is the `vbool` lane-density contract: one mask bit per 8-bit sub-lane of the
> 512-bit datapath ([cas-predicate-boolean §2.3](../iss/cas-predicate-boolean.md),
> [b11-vbool-alu §4.4](../isa/ref/b11-vbool-alu.md)), so a 16-bit lane occupies 2
> bits and a 32-bit lane 4. The width is structural — a distinct leaf — never a
> runtime field-width register. `[HIGH/OBSERVED]`

### 4.2 The ordered fp compare — NaN ⇒ FALSE (the `and`-in-not-NaN tail)

`module__xdref_olt_1_1_16f_16f @0x521e60` decomposes both operands' exp/mant to
classify NaN, computes the magnitude `<` on the sign-`0x7fff` form, and writes the
final predicate as the **AND of the raw compare with a both-operands-not-NaN
flag** — then stores it at `(%r8)`:

```asm
; head: classify each operand
  shr $0xa,%r9d ; and $0x1f,%r9d          ; expA  = (A>>10)&0x1f
  test $0x3ff,%esi ; sete %dil            ; mantA==0?
  cmp $0x1f,%r9d ; sete %al ; and %eax,%ebp  ; A is NaN  (exp==max && mant!=0)
  …                                        ; same for B
  or %ebp,%eax                            ; eax = isNaN(A) || isNaN(B)   (unordered)
  and $0x7fff,%edi ; … ; and $0x7fff,%edx ; |A|, |B|
  cmp %edx,%edi ; setb %dil               ; raw  |A| < |B|
  mov %edx,(%rcx)                         ; *aux  (reserved word — NOT the result)
  …                                        ; fold sign + the not-NaN flag
  and %edi,%eax                           ; result &= ORDERED (both-not-NaN)
  mov %eax,(%r8)                          ; *result  => FALSE if any operand NaN
  ret
```

```c
// module__xdref_olt_1_1_16f_16f(self, uint16 A, uint16 B, uint32 *aux, uint32 *result)
int unord = isnan16(A) || isnan16(B);
*aux    = /* reserved 0 */;
*result = unord ? 0 : (fcmp_lt16(A, B) ? 1 : 0);   // ordered: AND-in not-NaN  -> FALSE on NaN
```

`ole`/`oeq` share the shape (`oeq` adds a `+0==-0` path: `xor %edx,%esi; test
$0x7fff` so `oeq16(+0,-0)=1`). The **unordered** counterpart inverts the polarity —
it *ORs* the NaN flag into the result.

### 4.3 The pure-unordered test — `isNaN(A) || isNaN(B)` (byte-exact)

`module__xdref_un_1_1_16f_16f @0x522010` is the cleanest NaN proof in the family:
it computes nothing but the two `isNaN` flags and ORs them:

```asm
  shr $0xa,%eax ; and $0x1f,%eax ; cmp $0x1f,%eax ; sete %dil   ; expA==max?
  test $0x3ff,%esi ; setne %al ; and %eax,%edi                   ; isNaN(A) = expA==max && mantA!=0
  …                                                              ; isNaN(B) likewise
  or %edx,%eax                                                   ; isNaN(A) || isNaN(B)
  mov %eax,(%rcx)                                                ; *result
  ret
```

```c
// module__xdref_un_1_1_16f_16f(self, uint16 A, uint16 B, uint32 *result)
*result = (isnan16(A) || isnan16(B)) ? 1 : 0;     // pure isUnordered; ueq/ult/ule = un || (a OP b)
```

### 4.4 LIVE — the NaN split (byte-exact, fp16)

```python
import ctypes
L = ctypes.CDLL("tools/ncore2gp/config/libfiss-base.so")   # absolute path on your tree
u32 = ctypes.c_uint32; P = ctypes.POINTER(u32); u = ctypes.c_uint32
def bind(n, a): f = getattr(L, n); f.restype = None; f.argtypes = a; return f

# ordered fp16 _1_1 is 5-arg: (xstate, A, B, *aux, *result) — result at %r8
def ord16(name, A, B):
    f = bind(name, [u, u, u, P, P]); aux = u32(0); out = u32(0)
    f(0, A & 0xFFFF, B & 0xFFFF, ctypes.byref(aux), ctypes.byref(out)); return out.value
# unordered/un is 4-arg: (xstate, A, B, *result) — result at %rcx
def un16(name, A, B):
    f = bind(name, [u, u, u, P]); out = u32(0)
    f(0, A & 0xFFFF, B & 0xFFFF, ctypes.byref(out)); return out.value

QNAN, SNAN, ONE = 0x7e00, 0x7c01, 0x3c00   # qNaN (mant-MSB set), sNaN (mant-MSB clear), +1.0
```

| predicate | `(1.0, qNaN)` | `(1.0, 1.0)` | class | rule |
|---|---|---|---|---|
| `oeq_1_1_16f_16f` | `0` | `1` | ORDERED | false-on-NaN (`!un && a==b`) |
| `olt_1_1_16f_16f` | `0` | `0` | ORDERED | false-on-NaN |
| `ole_1_1_16f_16f` | `0` | `1` | ORDERED | false-on-NaN |
| `un_1_1_16f_16f` | `1` | `0` | UNORDERED | true-on-NaN (`isNaN(a)\|\|isNaN(b)`) |
| `ueq_1_1_16f_16f` | `1` | `1` | UNORDERED | true-on-NaN (`un \|\| a==b`) |
| `ult_1_1_16f_16f` | `1` | `0` | UNORDERED | true-on-NaN |
| `ule_1_1_16f_16f` | `1` | `1` | UNORDERED | true-on-NaN |

Signed-zero / infinity edges, all reproduced live and agreeing across every leg:
`oeq16(+0,-0)=1`, `oeq16(+inf,+inf)=1`, `olt16(-inf,+inf)=1`. sNaN and qNaN both
route to the unordered branch (no trap in the value oracle; the `(%rcx)` aux word
is a reserved 0). `[HIGH/OBSERVED]`

### 4.5 LIVE — the signed/unsigned separator (byte-exact)

```python
def cmp16(name, A, B):                            # bare int compare: 4-arg, result at %rcx
    f = bind(name, [u, u, u, P]); out = u32(0)
    f(0, A & 0xFFFF, B & 0xFFFF, ctypes.byref(out)); return out.value
```

| call | result | meaning |
|---|---|---|
| `lt_2_16_16(0x8000, 0x0001)` | `0x3` | **signed** −32768 < 1 → TRUE |
| `ltu_2_16_16(0x8000, 0x0001)` | `0x0` | **unsigned** 32768 < 1 → FALSE |
| `olt_1_1_16f_16f(-2.0, +1.0)` field `_1_1` | `0x1` | true → 1-bit field |
| `olt_1_2_16f_16f(-2.0, +1.0)` field `_1_2` | `0x3` | true → 2-bit replicate |
| `olt_1_4_32f_32f(-2.0, +1.0)` field `_1_4` | `0xf` | true → 4-bit replicate |

The first two rows are the **separating cells**: the same `(0x8000, 0x0001)` input
gives opposite truth under signed vs unsigned — the only thing that distinguishes
the legs is the sign-bias prologue, which is a distinct opcode, not a flag.
`[HIGH/OBSERVED]`

---

## 5. The fp classify 8-bit class mask (byte-exact)

`module__xdref_clsfy_16f_16f @0x524b00` is a 65-instruction branchless decoder
that maps an fp16 bit pattern to an **8-bit class mask**. The bit layout
(confirmed by the final shift cascade `shl $0x3 / shl $0x4 / shl $0x5 / shl $0x7`
plus the `lea (%rax,%rax,1)` ×2 and `lea (,%rdi,4)` ×4):

```
bit0 sign    bit1 zero    bit2 denorm   bit3 normal
bit4 inf     bit5 nan     bit6 snan     bit7 finite
```

The decode (annotated against the body), store via `(%rdx)` (UNARY 3-arg):

```asm
  shr $0xa,%eax ; and $0x1f,%eax        ; exp = (x>>10)&0x1f
  sete %cl                              ; (exp==0) → zero/denorm candidate
  test $0x3ff,%esi ; sete %r10b         ; mant==0
  cmp $0x1f,%eax ; sete %r9b            ; exp==0x1f (inf/nan)
  mov %esi,%edi ; shr $0x9,%edi ; xor $0x1,%edi   ; !(mantissa MSB)  — the sNaN/qNaN bit (bit9 fp16)
  shr $0xf,%esi                         ; sign = x>>15  → bit0
  …  or/shl cascade assembles the 8 bits …
  mov %eax,(%rdx)                       ; *classmask
  ret
```

```c
// module__xdref_clsfy_16f_16f(self, uint16 x, uint32 *classmask)   [UNARY — store via (%rdx)]
int  exp = (x >> 10) & 0x1f, sign = (x >> 15) & 1;
int  mant0 = (x & 0x3ff) == 0, mantMSB = (x >> 9) & 1;     // bit9 = the sNaN/qNaN discriminator
int  is_zero = (exp == 0) &&  mant0;
int  is_den  = (exp == 0) && !mant0;
int  is_inf  = (exp == 0x1f) &&  mant0;
int  is_nan  = (exp == 0x1f) && !mant0;
int  is_snan = is_nan && !mantMSB;                         // sNaN = NaN with mantissa-MSB CLEAR
int  is_norm = !is_zero && !is_den && !is_inf && !is_nan;
int  is_fin  = is_zero || is_den || is_norm;               // finite excludes inf AND nan
*classmask = (sign<<0)|(is_zero<<1)|(is_den<<2)|(is_norm<<3)
           | (is_inf<<4)|(is_nan<<5)|(is_snan<<6)|(is_fin<<7);
```

### 5.1 LIVE — the sNaN/qNaN split and the class corpus (byte-exact)

```python
def clsfy16(x):                                  # UNARY 3-arg: (xstate, x, *classmask) — store at %rdx
    f = bind("module__xdref_clsfy_16f_16f", [u, u, P]); out = u32(0)
    f(0, x & 0xFFFF, ctypes.byref(out)); return out.value
```

| input | bits | live | class | note |
|---|---|---|---|---|
| sNaN | `0x7c01` | `0x60` | nan+snan | mant-MSB **clear** → snan; **NOT finite** |
| sNaN | `0x7d55` | `0x60` | nan+snan | mant-MSB clear |
| qNaN | `0x7e00` | `0x20` | nan | mant-MSB **set** → not snan |
| +inf | `0x7c00` | `0x10` | inf | mant==0 → not nan |
| −0 | `0x8000` | `0x83` | sign+zero+finite | `0x1\|0x2\|0x80` |
| +1.0 | `0x3c00` | `0x88` | normal+finite | `0x8\|0x80` |
| +denorm | `0x0001` | `0x84` | denorm+finite | `0x4\|0x80` |

> **GOTCHA — the sole sNaN/qNaN discriminator is the mantissa MSB, and an sNaN is
> NOT finite.** `0x7c01` (mant-MSB = 0) → `0x60` (nan **+** snan); `0x7e00`
> (mant-MSB = 1) → `0x20` (nan only). Both omit bit7 — a NaN is never `finite`. A
> classifier that copies "finite = !inf" forgets that NaN is also non-finite, and
> one that tests only "exp==max && mant!=0" can flag *nan* but never *snan*. The
> exhaustive sweep below catches both. `[HIGH/OBSERVED]`

The fp16 classify was driven **EXHAUSTIVELY over all 65,536 patterns** against
the GX-SEM model: **0 mismatch**. fp32 classify (`module__xdref_clsfy_32f_32f
@0x87dc60`, mant-MSB = bit 22) was fuzzed over 30,000 patterns: 0 mismatch.
`[HIGH/OBSERVED]`

---

## 6. Mask logic + the `_t` merge — disassembly → pseudocode

### 6.1 Mask logic — flat 64-bit, two 32-bit words

The boolean algebra leaves treat the `vbool` as **two 32-bit halves**; the
`andnotb`/`ornotb` forms invert the **second** operand:

```asm
; module__xdref_andb_64_64_64 @0x856f80   ; dst = A & B
  mov (%rsi),%eax ; and (%rdx),%eax ; mov %eax,(%rcx)        ; low word
  mov 0x4(%rsi),%eax ; and 0x4(%rdx),%eax ; mov %eax,0x4(%rcx) ; high word
  ret
; module__xdref_andnotb_64_64_64 @0x856fb0 ; dst = A & ~B   (B complemented)
  mov 0x4(%rdx),%eax ; mov (%rdx),%edx ; not %eax ; not %edx
  and 0x4(%rsi),%eax ; and (%rsi),%edx ; mov %eax,0x4(%rcx) ; mov %edx,(%rcx) ; ret
```

```c
// module__xdref_andb_64_64_64(self, uint32 A[2], uint32 B[2], uint32 out[2])
out[0] = A[0] & B[0];  out[1] = A[1] & B[1];          // pure per-bit, no element boundaries
// orb = A|B  xorb = A^B  notb = ~A (1-input, _64_64)  andnotb = A & ~B   ornotb = A | ~B  (B inverted)
```

The full roster (all dynamic): `andb_64_64_64`@0x856f80, `orb_64_64_64`@0x856f90,
`xorb_64_64_64`@0x856fa0, `notb_64_64`@0x856f70 (1-input), `andnotb_64_64_64`@0x856fb0,
`ornotb_64_64_64`@0x814fc0; plus the 1-bit `BR` siblings `notb1_1_1`@0x5b8020,
`andnotb1_1_1_1`@0x5b8030, `ornotb1_1_1_1`@0x5b8040.

### 6.2 The `bitkillt` / `bitkillf` opposite-polarity merge masks (byte-exact)

The predicated-tail (`_t`) merge is the central proof of "merge, not zero". The two
mask leaves are byte-short and **opposite polarity** — both store via `(%rdx)`
(UNARY 3-arg):

```asm
; module__xdref_bitkillt_16_2 @0x85cc20   — kill-on-TRUE (all-ones in the KILLED lane)
  and $0x1,%esi ; xor $0x1,%esi ; neg %esi ; and $0xffff,%esi ; mov %esi,(%rdx) ; ret
;   => mask = (pred ? 0x0000 : 0xFFFF)

; module__xdref_bitkillf_16_2 @0x82d000   — kill-on-FALSE / inverse (all-ones in the LIVE lane)
  shl $0x1f,%esi ; sar $0x1f,%esi ; and $0xffff,%esi ; mov %esi,(%rdx) ; ret
;   => mask = (pred ? 0xFFFF : 0x0000)
```

```c
// module__xdref_bitkillt_16_2(self, uint32 pred, uint32 *mask)   [UNARY]
*mask = (pred & 1) ? 0x0000 : 0xFFFF;     // ALL-ONES where the lane is KILLED (pred FALSE) -> keep dst
// module__xdref_bitkillf_16_2(self, uint32 pred, uint32 *mask)
*mask = (pred & 1) ? 0xFFFF : 0x0000;     // ALL-ONES where the lane is LIVE  (pred TRUE)  -> take result
```

> **The `_t` masked op is a MERGE, not a zero-fill.** `bitkillt` emits **all-ones
> in the predicate-FALSE (killed) lane** — the "keep the prior destination" mask.
> A killed lane retains its old `dst` value; a live lane takes the fresh op result.
> `bitkillf` is the inverse polarity (all-ones in the live lane). This is exactly
> the destination-MERGE semantics of [cas-predicate-boolean §6](../iss/cas-predicate-boolean.md)
> and the engine for [CastPredicated §7](../firmware/kernels/castpredicated.md) — a
> reimplementation that zero-fills killed lanes is wrong for *every* `_t`
> arithmetic / select / predicated-move / masked-store on this core. The
> identity-merge: an `_t` op under an all-FALSE predicate is a no-op (the
> destination survives unchanged), not a clear. `[HIGH/OBSERVED]`

The width grid (note the **asymmetry**): `bitkillt` ships `8_1`@0x5bc360,
`16_2`@0x85cc20, `32_4`@0x528dd0 — but `bitkillf` ships `16_1`@0x82d050,
`16_2`@0x82d000, `32_4`@0x82d010. There is **no `bitkillf_8_1`** and **no
`bitkillt_16_1`** — bind the exact width or get `AttributeError`.

### 6.3 The `_t` predicated-tail gate (write-mask, value-identical)

The fp predicated-tail leaves (e.g. `oeq_1_2_16f_16f_2f_t @0x5b41f0`) gate on an
incoming predicate before computing the same compare value:

```asm
; module__xdref_oeq_1_2_16f_16f_2f_t @0x5b41f0  (head)
  xor %edi,%edi ; and $0x1,%ecx ; je 5b42d0     ; if (pred&1)==0: short-circuit, write 0
  …                                              ; else: the SAME oeq_1_2 compare value
```

```c
// the _t tail is a write-mask gate: result = (incoming_pred & 1) ? compare(A,B) : 0
//   value-identical to the un-tailed leaf under an ACTIVE predicate; 0 under an inactive one.
```

This is the value-leg view of the `bitkillt` merge — under an inactive predicate
the lane contributes nothing; under an active one it carries the full compare.
`[HIGH/OBSERVED on the gate + value-identity]`

### 6.4 LIVE — mask logic + bitkill (byte-exact)

```python
def mask2(name, A, B):                            # 2-word vbool: (xstate, *A[2], *B[2], *out[2])
    f = bind(name, [u, P, P, P])
    a = (u32*2)(A & 0xffffffff, A >> 32); b = (u32*2)(B & 0xffffffff, B >> 32); o = (u32*2)(0, 0)
    f(0, ctypes.cast(a, P), ctypes.cast(b, P), ctypes.cast(o, P)); return (o[1] << 32) | o[0]
def bitkill(name, pred):                          # UNARY 3-arg: (xstate, pred, *mask) — store at %rdx
    f = bind(name, [u, u, P]); out = u32(0); f(0, pred & 1, ctypes.byref(out)); return out.value
```

| call | result | check |
|---|---|---|
| `andb(0xf0f0…, 0x00ff…)` | `0x00f000f0…` | `A & B` |
| `andnotb(0xf0f0…, 0x00ff…)` | `0xf000f000…` | `A & ~B` (B inverted) |
| `notb(0xf0f0…)` | `0x0f0f0f0f…` | `~A` |
| `bitkillt_16_2(pred=1)` | `0x0000` | live lane → 0 mask |
| `bitkillt_16_2(pred=0)` | `0xffff` | **killed → all-ones (keep dst)** |
| `bitkillf_16_2(pred=1)` | `0xffff` | live → all-ones (take result) |
| `bitkillf_16_2(pred=0)` | `0x0000` | killed → 0 |

The bitkill rows are the merge proof: `bitkillt` and `bitkillf` return *swapped*
masks for the same predicate. `[HIGH/OBSERVED]`

---

## 7. The FS0..FS7 flag bank + bit-spread / reduce

### 7.1 FSnLTU — 8 wrappers over one shared `ltu_1_8_8`

The eight FS-compare ops are **byte-identical thin wrappers** (0x20 bytes apart,
a wrapper table) each relaying the **shared** `module__xdref_ltu_1_8_8 @0x5bc4f0`
(unsigned i8 LT → 1 bit); the FSn index is **opcode identity** (a distinct
destination state), not a body difference:

```asm
; module__xdref_fs0ltu_64_8_8 @0x8328d0   (fs7ltu @0x8329b0 differs ONLY in the call rel-offset)
  push %rbx ; mov %rcx,%rbx ; sub $0x10,%rsp
  lea 0xc(%rsp),%rcx ; call module__xdref_ltu_1_8_8@plt   ; compute the i8 unsigned LT
  mov 0xc(%rsp),%eax ; mov %eax,(%rbx)                    ; relay into the FSn dest
  add $0x10,%rsp ; pop %rbx ; ret

; module__xdref_ltu_1_8_8 @0x5bc4f0   (the shared base)
  xor %eax,%eax ; cmp %edx,%esi ; setb %al ; mov %eax,(%rcx) ; ret
```

```c
// module__xdref_fsNltu_64_8_8(self, uint8 A, uint8 B, uint32 *FSn)  for N=0..7
*FSn = (A < B) ? 1 : 0;     // every FSn shares ltu_1_8_8; only the dest (FS index) is opcode identity
```

### 7.2 BORFS / BNORFS — OR/NOR-reduce eight vbool → one FS

`module__xdref_borfs_2n_… @0x8327f0` reads **eight** `vbool` registers (5 in
registers, 3 on the stack) and ORs them word-by-word into one FS register;
`bnorfs @0x832870` is the same tree with a final complement. This is an
**inter-register** fold (distinct from the within-mask `randbn`/`rorbn` of
[b11-vbool-alu §7](../isa/ref/b11-vbool-alu.md)):

```c
// borfs_2n:  FS[w] = vb0[w] | vb1[w] | … | vb7[w]      (w = 0,1)   — collapse 8 masks
// bnorfs_2n: FS[w] = ~(vb0[w] | … | vb7[w])            — the "no register set this lane" sentinel
```

### 7.3 MOVFSV — per-lane bit-spread harvest

`module__xdref_movfsv_64_64_64_64_64_64_64_64_512 @0x82e2b0` is a per-lane
bit-spread: it harvests the 512-bit `vec`'s per-lane bits and scatters them across
the eight FS banks via a `shr`/`shl`/`and` permutation tree (the inverse
`movvfs_512_… @0x830970` packs the other way). The probed mappings: `vec` word0
bit7 → FS7 bit0, bit14 → FS6 bit1. The full lane→bank→bit permutation is the live
binary's domain — `movfsv` is its own authoritative oracle.

> **NOTE — `FS0..FS7` are 8 on-core flag-state registers, not 8 generations.** The
> FS bank is 8 independent predicate-state destinations over **one** shared
> unsigned-i8-LT compute. The single-hot `BORFS` reduce of eight banks each set to
> a distinct bit `{bit0..bit7}` → `0xff`; `BNORFS` → its complement. None of this
> is a silicon-gen axis. `[HIGH/OBSERVED]`

### 7.4 LIVE — FS bank + reduce (byte-exact)

```python
def fsltu(name, A, B):                              # (xstate, A, B, *FSn) — store at %rcx
    f = bind(name, [u, u, u, P]); out = u32(0); f(0, A & 0xff, B & 0xff, ctypes.byref(out)); return out.value
```

| call | result |
|---|---|
| `fs3ltu_64_8_8(1, 2)` | `1` (1 < 2 TRUE) |
| `fs3ltu_64_8_8(2, 1)` | `0` (2 < 1 FALSE) |
| `fs0ltu … fs7ltu (3, 5)` | `1` each (shared compute, distinct dest) |
| `fs0ltu … fs7ltu (9, 2)` | `0` each |
| `BORFS` of 8 single-hot banks `{1<<0 … 1<<7}` | `0xff` |
| `BNORFS` of the same | `0xffffffffffffff00` |

All eight FSnLTU agree because they share `ltu_1_8_8`; the FSn index is the
destination, not the compute. `[HIGH/OBSERVED]`

---

## 8. The bit-plane move — EXTBI (extract) / INJBI (RMW inject)

`extbi` extracts one lane bit and replicates it into the lane field width; `injbi`
is a read-modify-write single-bit inject. Both byte-read this pass:

```asm
; module__xdref_extbi_1_8_32 @0x5e92e0   — extract bit i, 1-bit field (returns 0/1)
  and $0x7,%edx ; mov $1,%eax ; mov %edx,%ecx ; shl %cl,%eax   ; mask = 1<<(i&7)
  test %esi,%eax ; setne %al ; movzbl %al,%eax ; mov %eax,(%rdi) ; ret

; module__xdref_extbi_2_16_32 @0x5e9330   — 2-bit field: returns 0/3
  and $0xf,%edx ; mov $1,%eax ; shl %cl,%eax ; test %esi,%eax ; setne %al
  movzbl %al,%eax ; neg %eax ; and $0x3,%eax ; mov %eax,(%rdi) ; ret

; module__xdref_injbi_8_8_1_32 @0x5e92b0   — RMW: set/clear bit i per predicate
  and $0x7,%ecx ; mov $1,%eax ; shl $0x1f,%edx ; shl %cl,%eax  ; mask = 1<<i ; pred broadcast
  sar $0x1f,%edx ; … ; not %ecx ; and %edx,%eax ; and %ecx,%esi ; or %esi,%eax
  mov %eax,(%r8) ; ret
```

```c
// module__xdref_extbi_1_8_32(self, uint32 val, uint32 bit, uint32 *out)   -> 0/1   (1-bit field)
*out = (val >> (bit & 7)) & 1;
// module__xdref_extbi_2_16_32 -> 0/3 (the bit REPLICATED to its 2-bit lane field), _4_32_32 -> 0/0xf
// module__xdref_injbi_8_8_1_32(self, uint32 base, uint32 pred, uint32 bit, uint32 *out)   [RMW, 5-arg]
uint32 m = 1u << (bit & 7);
*out = (base & ~m) | ((pred ? 0xFFFFFFFF : 0) & m);   // inject the predicate into bit i, keep the rest
```

### 8.1 LIVE — extbi field replication (byte-exact)

```python
def extbi(name, val, bit):                          # (xstate, val, bit, *out) — store at %rcx
    f = bind(name, [u, u, u, P]); out = u32(0); f(0, val, bit, ctypes.byref(out)); return out.value
```

| call | result | check |
|---|---|---|
| `extbi_1_8_32(0b10110, i=0..5)` | `[0,1,1,0,1,0]` | raw bit (1-bit field) |
| `extbi_2_16_32(0b10110, i=0..5)` | `[0,3,3,0,3,0]` | bit replicated into the 2-bit lane field |
| `extbi_4_32_32(0b10110, i=2)` | `0xf` | replicated into the 4-bit field |
| `injbi_8_8_1_32(base=0x00, pred=1, bit=3)` | `0x08` | bit 3 set |
| `injbi_8_8_1_32(base=0xff, pred=0, bit=3)` | `0xf7` | bit 3 cleared, rest kept |

`extbi`'s replication mirrors the compare-leaf field width (§4.1): the extracted
predicate carries its lane-field encoding. `[HIGH/OBSERVED]`

---

## 9. The device decode leg (b) and the agreement table

### 9.1 Leg (b) — assemble → decode identity

33 single-op FLIX bundles for the family — int `eq`/`neq`/`lt`/`le`/`ltu`/`leu`
(nx16/2nx8/n_2x32), fp `oeq`/`olt`/`ole`/`un`/`ueq`/`ult`/`ule` (nxf16/n_2xf32),
`clsfynxf16`/`clsfyn_2xf32`, `andb`/`orb`/`xorb`/`notb`/`andnotb`, `extbinx16`/
`injbinx16`, `fs0ltu2nx8`/`fs7ltu2nx8`, `borfs2n`/`bnorfs2n`, `movfsv` — were
assembled with the device `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) and decoded
with the device `xtensa-elf-objdump`: **33/33 raw-bytes → mnemonic** round-trip,
all N0 format (`op0` byte `0x2f`). Crucially, `fs0ltu2nx8` and `fs7ltu2nx8` decode
to **different bytes** (the FSn index lives in the slot stream), confirming the
index is opcode identity, exactly as the eight byte-identical wrapper bodies (§7.1)
imply. `[HIGH/OBSERVED]`

### 9.2 The agreement table (the result)

| section | cases | LIVE-vs-SEM | LIVE-vs-NKI |
|---|---:|---|---|
| [1] int compares {eq,neq,lt,le,ltu,leu} × {8,16,32} | 1836 | 0/1836 | 0/1836 |
| [2] fp `_1_1` {oeq,olt,ole,un,ueq,ult,ule} × {16f,32f} | 2366 | 0/2366 | 0/2366 |
| [3] fp field-width `_1_2`(0x3) / `_1_4`(0xf) | 2366 | 0/2366 | n/a (no nki field) |
| [4] fp classify 8-bit mask {16f,32f} | 32 | 0/32 | n/a (no nki classify) |
| [5] mask logic {and,or,xor,notb,andnotb} | 264 | 0/264 | 0/192 |
| [6] bitkillt/f predicated merge masks | 12 | 0/12 | n/a |
| [7] FS-bank + extbi/injbi | 398 | 0/398 | n/a |
| **TOTAL** | **7274** | **0 FAIL** | (all jointly-defined) |

Scale differential (same `(A,B)` stream to every leg, seed `20260617`): fp16
compare 140,000 · fp32 compare 56,000 · **fp16 classify EXHAUSTIVE 65,536** · fp32
classify 30,000 · int16 compare 90,000 = **381,536, 0 mismatch**. Grand total
**~388,810, 0 mismatch**.

> **GOTCHA — the nki leg covers a strict subset, by design.** nki-0.3.0 exposes
> the int compares (dtype-typed two's-complement `view()`), the fp compares (numpy
> IEEE ufuncs, unordered built as `un || ordered`), and AND/OR/XOR mask logic — but
> has **no primitive** for the fp field-width replication (`_1_2`/`_1_4`), the 8-bit
> classify mask, `bitkillt/f`, the FS bank, or extbi/injbi. Those are validated
> **3-way** (SEM + FLIX-decode + LIVE) and the LIVE binary closes them against the
> shipped oracle directly — mark `n/a` in the nki column, never hide them.
> `[HIGH/OBSERVED]`

---

## 10. Reproducible driver (one self-contained script)

The entire corpus is one file; running it prints the bytes embedded in this page.
Nothing is pickled; the binary is loaded directly.

```python
import ctypes
BIN = "tools/ncore2gp/config/libfiss-base.so"   # absolute path on your tree
L   = ctypes.CDLL(BIN)
u   = ctypes.c_uint32; P = ctypes.POINTER(u)
def b(n, a): f = getattr(L, n); f.restype = None; f.argtypes = a; return f
o = u(0); aux = u(0)

# ordered fp16 _1_1: 5-arg, result at %r8 (aux at %rcx is reserved)
oeq = b("module__xdref_oeq_1_1_16f_16f", [u, u, u, P, P])
un_ = b("module__xdref_un_1_1_16f_16f",  [u, u, u, P])           # 4-arg, result at %rcx
clsfy = b("module__xdref_clsfy_16f_16f", [u, u, P])             # UNARY 3-arg, result at %rdx
lt  = b("module__xdref_lt_2_16_16",      [u, u, u, P])           # signed,   result at %rcx
ltu = b("module__xdref_ltu_2_16_16",     [u, u, u, P])           # unsigned, result at %rcx
bkt = b("module__xdref_bitkillt_16_2",   [u, u, P])             # merge mask, result at %rdx

oeq(0, 0x3c00, 0x3c00, ctypes.byref(aux), ctypes.byref(o)); print("oeq(1,1)        =", o.value)        # 1
oeq(0, 0x3c00, 0x7e00, ctypes.byref(aux), ctypes.byref(o)); print("oeq(1,qNaN)     =", o.value)        # 0
un_(0, 0x3c00, 0x7e00, ctypes.byref(o));                    print("un(1,qNaN)      =", o.value)        # 1
clsfy(0, 0x7c01, ctypes.byref(o));  print("clsfy(sNaN 0x7c01) = 0x%02x" % o.value)                     # 0x60
clsfy(0, 0x7e00, ctypes.byref(o));  print("clsfy(qNaN 0x7e00) = 0x%02x" % o.value)                     # 0x20
clsfy(0, 0x3c00, ctypes.byref(o));  print("clsfy(+1.0)        = 0x%02x" % o.value)                     # 0x88
lt (0, 0x8000, 0x0001, ctypes.byref(o)); print("lt (0x8000,1)  = 0x%x" % o.value)                      # 0x3
ltu(0, 0x8000, 0x0001, ctypes.byref(o)); print("ltu(0x8000,1)  = 0x%x" % o.value)                      # 0x0
bkt(0, 1, ctypes.byref(o)); print("bitkillt(pred=1) = 0x%04x" % o.value)                               # 0x0000
bkt(0, 0, ctypes.byref(o)); print("bitkillt(pred=0) = 0x%04x" % o.value)                               # 0xffff (keep dst)
```

Captured output (the libfiss-base **LIVE** column — every other oracle returns
identical bytes):

```
oeq(1,1)        = 1
oeq(1,qNaN)     = 0
un(1,qNaN)      = 1
clsfy(sNaN 0x7c01) = 0x60
clsfy(qNaN 0x7e00) = 0x20
clsfy(+1.0)        = 0x88
lt (0x8000,1)  = 0x3
ltu(0x8000,1)  = 0x0
bitkillt(pred=1) = 0x0000
bitkillt(pred=0) = 0xffff
```

---

## 11. Adversarial self-verify

The five strongest claims, re-challenged against the binary:

1. **"The vbool field width is 1/2/4-bit, structural per lane width."**
   `[HIGH/OBSERVED]` — `lt_2_16_16` ends `and $0x3` (2-bit), the 8-bit leaf
   `and $0x1`, the 32-bit `and $0xf`; `extbi_1_8_32` returns 0/1 while
   `extbi_2_16_32` returns 0/3 (the `neg; and $0x3` replicate) — driven live.
   There is a *distinct leaf per width* (no `_1_4` 16f symbol exists), so it cannot
   be a runtime mode. Challenge passed.
2. **"The `_t` is a destination-MERGE, not a zero-fill — proven by `bitkillt`."**
   `[HIGH/OBSERVED]` — `bitkillt_16_2(pred=0)=0xFFFF` (all-ones in the KILLED lane =
   "keep dst"), `bitkillt_16_2(pred=1)=0x0000`; `bitkillf_16_2` returns the
   *swapped* masks. The `_t` tail gate (`and $1,%ecx; je`) short-circuits to a
   no-write under an inactive predicate — identity-merge. A zero-fill model is wrong
   for every `_t` op. Challenge passed (and matches
   [cas-predicate-boolean §6](../iss/cas-predicate-boolean.md)).
3. **"The fp classify 8-bit mask discriminates sNaN/qNaN by the mantissa MSB, and
   sNaN is not finite."** `[HIGH/OBSERVED]` — the body's `shr $0x9; xor $0x1` on
   the raw fp16 isolates bit 9 (mant MSB); live `clsfy16(0x7c01)=0x60` (nan+snan,
   no bit7) vs `clsfy16(0x7e00)=0x20` (nan only). Both omit `finite` (bit7). The
   fp16 classify was swept **EXHAUSTIVELY over all 65,536 patterns**: 0 mismatch.
   Challenge passed.
4. **"The live-ctypes outputs are byte-exact, and the ordered fp ABI result is at
   `%r8` not `%rcx`."** `[HIGH/OBSERVED]` — *corrected during authoring.* A partial
   read of `olt_1_1_16f_16f` saw the `(%rcx)` store at `0x521f00` and nearly bound
   the result there; the FULL body shows `(%rcx)` is the reserved **aux** word and
   the final ordered predicate stores at `(%r8)` (`0x521f32`). Binding the result to
   `%rcx` reads back **0** for every ordered predicate. The bare int / `un` leaves
   *do* store at `(%rcx)`. Every hex in this page is a captured ctypes call; `.text`
   VMA==file-offset makes the objdump addresses file-accurate. Challenge passed
   after fix.
5. **"The 8 FSnLTU ops share one `ltu_1_8_8`; the FSn index is opcode identity."**
   `[HIGH/OBSERVED]` — `fs0ltu`@0x8328d0 and `fs7ltu`@0x8329b0 are byte-identical
   except the `call` rel-offset (both relay `ltu_1_8_8@plt`); live `fs0ltu…fs7ltu
   (3,5)=1` all agree. The device decode (leg b) confirms `fs0ltu2nx8` vs
   `fs7ltu2nx8` produce *different* bundle bytes (index in the slot stream) — i.e.
   the index is in the opcode, not the body. Challenge passed.

**Ungrounded / flagged.** The full `MOVFSV` per-lane harvest *permutation* beyond
the two probed mappings (`word0 bit7→FS7 bit0`, `bit14→FS6 bit1`) is `[MED/INFERRED]`
— the bit-spread `shr`/`shl` tree is OBSERVED in the body, but the complete
lane→bank→bit map is the live binary's domain (live = oracle). The fp
predicated-tail (`_2f_t`) value-identity under a *sweep* of incoming predicates is
`[HIGH struct]` (the gate + value-identity is OBSERVED; the full mask sweep is a
focused note, not transcribed per-tail).

**Single strongest CORRECTION:** the **ordered** fp `_1_1` compare leaf is 5-arg
and writes its real predicate to **`(%r8)`**, while the reserved aux word goes to
`(%rcx)`. `module__xdref_olt_1_1_16f_16f` stores aux at `0x521f00` (`mov %edx,(%rcx)`)
and the ordered result at `0x521f32` (`mov %eax,(%r8)`); `oeq_1_1` stores its result
at `(%r8)` (`0x521ffb`). The **bare int compares and the pure-unordered `un`** are
4-arg and store the result at `(%rcx)`. Binding the ordered-fp result pointer to
`%rcx` — the natural "4th arg = out" assumption that is correct for every other
compare in the family — reads back the reserved-zero aux for *every* ordered
predicate, silently turning `oeq`/`olt`/`ole` into a constant 0.

---

## 12. Cross-references

- [four-oracle-method](four-oracle-method.md) — the GX-SEM / GX-FLIX / nki-0.3.0 /
  libfiss-base LIVE differential this page instantiates (the per-leaf ABI, the
  `RTLD_GLOBAL` rule, the wall triage).
- [b11-vbool-alu](../isa/ref/b11-vbool-alu.md) — the per-instruction ISA reference
  for the boolean algebra (`andb`/…/`notb`), the `borfs`/`bnorfs` fold, the
  `movvfs`/`movfsv` bridge, `extbi`/`ext0ib`, and the 1-bit-per-8-bit-sub-lane
  field-width contract.
- [cas-predicate-boolean](../iss/cas-predicate-boolean.md) — **authoritative** on
  the `vbool` (16×64) register file, the ordered/unordered NaN polarity, the signed
  sign-bias vs unsigned bare-cmp, and the `_t` destination-MERGE these leaves
  realize.
- [castpredicated](../firmware/kernels/castpredicated.md) — the predicate-gated
  firmware kernel (opcode `0x99`) whose `compare → bitkillt-merge →
  predicated-op` chain is built entirely from the primitives proven here.
