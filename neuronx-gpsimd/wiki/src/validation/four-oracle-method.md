# The 4-Oracle Bit-Exact Differential Method

This page is the **method** that the rest of Part 15 applies. Every VAL-family page
([fp-soft-float](fp-soft-float.md), [mac-multiply](mac-multiply.md),
[convert-pack-cast](convert-pack-cast.md), [reduce-shift-shuffle](reduce-shift-shuffle.md),
[gather-scatter](gather-scatter.md), [predicate-classify](predicate-classify.md),
[transcendental-seed](transcendental-seed.md),
[regfile-bridge-divergence](regfile-bridge-divergence.md),
[capstone-matrix](capstone-matrix.md)) is one *family instance* of the harness described
here. The method exists to do one thing: take a per-opcode semantic that the ISA pages
recovered by reading bytes (**OBSERVED**) and upgrade it to one the shipped vendor binary
itself certifies by running (**PROVEN-BY-EXECUTION**).

The keystone is reference **(d)**: instead of re-implementing the value function, the
harness `dlopen`s the real `libfiss-base.so` and *calls* its value leaves on every input.
The differential is anchored to the binary, not to an analyst's reading of it. This is the
value half of the Part-14 ISS oracle (`libcas-core` decode + timing /
[`libfiss-base` value, 864 leaves](../iss/fiss-datapath-oracle.md)); the harness design it
realizes is documented in [iss-oracle-synthesis](../iss/iss-oracle-synthesis.md), and the
RTL semantics it cross-checks against live in
[formal-isa-model](../isa/semantics/formal-isa-model.md).

> **NOTE.** Throughout this page every "16-bit lane / 0x7fff / 0x8000 / N0-format / slot3"
> token is a **datapath-width / FLIX-format / ISA-lane axis of the single Cairo config**
> (`Xm_ncore2gp`, Xtensa24, NX1.1.4, RI-2022.9). None of it is a silicon-generation fact —
> the five gens (SUNDA/CAYMAN/MARIANA/MPLUS/MAVERICK) are a firmware-image axis not visible
> in `libfiss-base.so`, the FLIX tables, the TIE-XML, or the nki simulator.

---

## 1. The four references

The method runs **four structurally independent models** on **identical** edge-case input
vectors and asserts bit-exactness lane-by-lane. Independence is the whole point — agreement
across four codebases that share no source is a real cross-check, not a tautology.

| Leg | Reference | What it is | Role | Tag |
|-----|-----------|-----------|------|-----|
| **(a)** | **SEM** | the bit-precise TIE-XML RTL semantics, lifted to python | value (RTL model) | OBSERVED / from formal-isa-model |
| **(b)** | **FLIX** | the firmware FLIX bundle, assembled + decoded by the device toolchain | identity / decode | OBSERVED |
| **(c)** | **nki-0.3.0** | the numpy reference simulator (`tensor_tensor_arith`, gpsimd native-int path) | value (framework, subset) | OBSERVED |
| **(d)** | **libfiss-base** | the shipped ISS value leaves, **driven LIVE via ctypes** | value (the vendor binary itself) | OBSERVED — the keystone |

The four are genuinely different algorithms for the *same* lane result. For a saturating
add: **(a)** uses a high-level clamp (`v>32767 -> 0x7fff`, `v<-32768 -> 0x8000`); **(d)**
uses the x86 bit-trick the leaf actually executes (sign-extend, detect 17-bit overflow as
`bit15 != bit16`, then `(s<<16) sar 31 & 0x7fff | (s>>1) & 0x8000`); **(c)** uses numpy's
native `int16` wrap. Three distinct implementations, one output ⇒ independent confirmation.

> **Why (d) is the strongest leg.** A python "lift" of the disassembly can carry the
> analyst's misreading; the live binary cannot. The harness runs **both** the lift (fast,
> inspectable, documents the bit-trick) *and* the live leaf (authoritative). Their agreement
> also self-checks the lift — a LIVE-vs-lift mismatch means *the lift mis-read the bytes*,
> and the binary wins.

### Provenance of the value oracle

```
libfiss-base.so   ELF64 x86-64, NOT stripped, 12,330,016 bytes
  nm -D | rg -c 'module__xdref_'                = 864   (value leaves)
  nm -D | rg -c 'opcode__ivp_.*__stage_5'       = 948   (device-context staging handlers)
```

Counts grounded with `nm -D <abs> | rg -c` against the binary, never the decompile. Absolute
path:

```
.../extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so
```

The companion `libcas-core.so` (45,878,080 B, same `config/` dir) supplies the *decode +
timing* half of the Part-14 oracle; its per-instance simulator state is
**4,852,208 B (0x4a09f0)** `[CARRIED/Part-14]`. The 4-oracle method uses only the **value**
axis — `libfiss-base` — so the bare-leaf ctypes drive never instantiates the heavyweight
`cas` model (that is the escalation tier, §7).

---

## 2. The xdref leaf — name, ABI, and the per-lane contract

### 2.1 Naming

Each value leaf is named

```
module__xdref_<op>_<outW>_<inW0>[_<inW1>[_<inW2>...]]
```

where `xdref` is the cross-decode reference value function and the suffix is the **output
width** followed by each **input width** in bits. Width tokens:
`{8, 16, 32, 48, 16f, 32f, 64f, 1, ...}` (`f` = float, `1` = predicate/vbool). So
`module__xdref_add_16_16_16` is the all-`i16` add (→ `ivp_addnx16`), `module__xdref_abs_16_16`
is the `i16` unary `|x|` (→ `ivp_absnx16`), and `module__xdref_muluu_48_16_16` is the
`u16*u16 -> 48b` pack.

### 2.2 The ABI — verified per leaf, NOT assumed

The leaf reads its scalar operand(s) from GP integer registers, computes, and **stores the
result through an out-pointer** (it returns `void`). Disassembled at the recovered addresses:

```asm
; module__xdref_add_16_16_16  @0x858480   (BINARY, 4-arg)
  add    %esi,%edx              ; %esi = A, %edx = B
  and    $0xffff,%edx           ; mask to 16-bit lane
  mov    %edx,(%rcx)            ; STORE through %rcx = *out
  ret

; module__xdref_abs_16_16      @0x82d060   (UNARY, 3-arg)
  test   $0x8000,%esi           ; %esi = A ; sign bit?
  je     +0xd                   ; if non-negative, skip
  neg    %esi                   ; -A
  movzwl %si,%esi               ; zero-extend low 16
  mov    %esi,(%rdx)            ; STORE through %rdx = *out   <-- NOTE the register
  ret
```

This gives **two** ABIs, and they differ in *which register carries the out-pointer*:

```
BINARY value leaf : f(%rdi = xstate,  %esi = A,  %edx = B,  %rcx = *out)  -> void
UNARY  value leaf : f(%rdi = xstate,  %esi = A,             %rdx = *out)  -> void
```

> **CORRECTION (the single most important ABI fact).** Many leaves are 4-arg with **arg0 a
> dead `%rdi` context pointer**, but **the unary leaves are genuinely 3-arg** — the
> out-pointer moves from `%rcx` (binary) to `%rdx` (unary). A naive "every leaf is 4-arg"
> binding passes the out-pointer in `%rcx` to a unary leaf that reads it from `%rdx`, and
> the store lands in an uninitialised register's address → garbage or SIGSEGV. **Verify
> `nargs` per leaf from the width suffix** (one input width ⇒ unary ⇒ 3-arg) **and from the
> store register in the disassembly.** Never carry the 4-arg shape across the unary boundary.

> **GOTCHA.** `%rdi` (arg0, the ISS `xstate`) is **unused** by every callback-free
> integer/word leaf — which is *why* the bare ctypes drive can pass `NULL`. It is **not**
> universally dead: a leaf that delegates to the ISS soft-float dispatch *reads* `xstate`,
> and `NULL` there is the §7 wall. Treat arg0 as "dead unless the body dereferences it."

### 2.3 The per-lane contract

Each reference computes `result[k] = op(A[k], B[k])` for one lane and returns the
lane-width bit pattern as an int. A vector intrinsic is literally *N* invocations of the
scalar leaf — `IVP_ADDNX16` is 32 invocations of `xdref_add_16_16_16`, lanes independent
(the carry is broken at every lane edge). **So a per-lane differential over all lane-value
pairs IS a complete vector-op differential.** The harness validates the leaf; the vector op
follows by construction.

---

## 3. Input-vector construction

A "0 mismatch" verdict only means something if the corpus exercises the cases where a wrong
model *would* break. The harness uses **edge-cartesian + seeded fuzz**, lane-width aware.

### 3.1 The edge set (16-bit lane)

```python
EDGE = [0x0000, 0x0001, 0xFFFF, 0x7FFF, 0x8000, 0x8001, 0x7FFE,
        0x0002, 0xFFFE, 0x4000, 0xC000, 0x00FF, 0xFF00, 0x5555, 0xAAAA]  # 15 values
```

Each value covers a required class — and the `{zero, denormal, saturation, sign, NaN}`
classes map onto the integer datapath as follows:

| Class | Integer-datapath analogue | Edge values |
|-------|---------------------------|-------------|
| **ZERO** | additive identity, min-magnitude | `0x0000` |
| **DENORMAL** | `INT16_MIN` = `0x8000`, the value whose `neg`/`abs` cannot be represented — the wrap/overflow trigger (the integer analogue of the float denormal) | `0x8000`, `0x8001`, `0xC000` |
| **SATURATION** | sums/differences that overflow the lane: `+max` & `+1`, `-min` & `-1` | `0x7FFF`+`0x0001`, `0x8000`+`0xFFFF` |
| **SIGN** | full `{pos,neg,zero}` × `{pos,neg,zero}` cross-product | the whole cartesian |
| **NaN** | *no NaN in the integer datapath* — there is no non-numeric encoding for `i16`. The NaN class is exercised in the **fp** families ([fp-soft-float](fp-soft-float.md), [transcendental-seed](transcendental-seed.md)) where the edge corpus adds `{±inf, qNaN, sNaN, signed zeros, denormals}`. | n/a (int) |
| *(plus)* **UNSIGNED-WRAP** | high-bit-set values that order differently signed vs unsigned (separates `min`/`max` from `minu`/`maxu`) | `0xFFFF`, `0xFF00`, `0xAAAA` |
| *(plus)* **EQUAL-OPERAND** | `a==b` cells (`min==max`, `abssub==0`, `avg==a`) | diagonal of the cartesian |

The **full cartesian** `EDGE × EDGE` (15×15 = 225 pairs) hits every (sign, magnitude,
boundary) cross-product — exactly where saturate/avg/abssub bit-tricks break if a model is
wrong.

### 3.2 Seeded fuzz

```python
rng = np.random.default_rng(SEED)           # FIXED seed -> byte-reproducible corpus
A = list(EDGE_CART_A) + list(rng.integers(0, 0x10000, 4096))
B = list(EDGE_CART_B) + list(rng.integers(0, 0x10000, 4096))   # 225 + 4096 = 4321 pairs
```

4096 random pairs catch any interior bit-pattern the edge set misses. The **fixed seed**
turns every re-run into a regression test rather than a fresh random sample.

### 3.3 Width scaling.  For an 8-bit family use
`{0x00,0x01,0xFF,0x7F,0x80,0x81,0x7E,0xFE,0x40,0xC0,0x55,0xAA}`; for 32-bit, the analogous
`{0,1,-1,INT32_MAX,INT32_MIN,±2,quarter-scale}` + fuzz; for fp, the
`{signed zeros, ±1, min/max denormal, max normal, ±inf, qNaN, sNaN, equal pairs}` corpus.

### 3.4 Coverage accounting.  Report the exact count `n_ops × n_pairs × n_legs`. The
elementwise-integer-ALU instance: **14 ops × 4321 pairs = 60,494** SEM-vs-FISS comparisons
+ **21,605** NKI-vs-SEM (the 5 int-primitive ops) + the full LIVE 4-way. *A count without an
edge-cartesian basis is not a proof — report both the basis and the number.*

---

## 4. The comparator skeleton

Two python files, reused verbatim across every family:

- **the models file** — the MODELS + the OP REGISTRY + the INPUT GENERATOR + the offline
  (lift-only) comparator. No binary needed; pure python, so it *is* the documentation of the
  bit-tricks.
- **the live file** — imports the models file, adds the ctypes binding to the real
  `libfiss-base.so`, and runs the LIVE N-way comparator.

### 4.1 The op registry — one source of truth per family

```python
# (name, sem_fn, fiss_lift_fn, nki_fn_or_None, nargs, mnemonic)
OPS = [
  ("add",    sem_add,    fiss_add,    nki_add,   2, "ivp_addnx16"),
  ("adds",   sem_adds,   fiss_adds,   None,      2, "ivp_addsnx16"),   # no nki int prim
  ("min",    sem_min,    fiss_min,    nki_min,   2, "ivp_minnx16"),
  ("minu",   sem_minu,   fiss_minu,   None,      2, "ivp_minunx16"),
  ("abs",    sem_abs,    fiss_abs,    nki_abs,   1, "ivp_absnx16"),    # nargs=1 -> UNARY ABI
  ("neg",    sem_neg,    fiss_neg,    None,      1, "ivp_negnx16"),
  # ... abssub, abssubu, avg, avgr, sub, subs, max, maxu ...
]
```

- `sem_fn` — the bit-precise RTL model (leg **a**, derived from the *op definition*, not
  copied from the lift — independence is what makes the cross-check mean anything).
- `fiss_lift_fn` — the python lift of the leaf's exact x86 body (documents and self-checks
  the bit-trick; leg **a′**).
- `nki_fn` — the numpy lane fn, or `None` where nki has no int primitive (leg **c**).
- `nargs` — `1` (unary) or `2` (binary): **selects the ABI** (§2.2). This field is what keeps
  the 3-arg/4-arg boundary correct.
- `mnemonic` — the device mnemonic the decode leg (b) must reproduce, and the key that maps
  `name -> module__xdref_<...>` leaf.

### 4.2 The compare loop

```python
for name, semf, fissf, nkif, nargs, mn in OPS:
    for (a, b) in zip(A, B):                     # the §3 corpus, SAME stream for all legs
        rl = call_live(name, a, b)               # (d)  LIVE binary  — authoritative
        rs = semf(a, b)                          # (a)  bit-precise RTL model
        rf = fissf(a, b)                          # (a') disassembly lift  — self-check
        rn = nkif(a, b) if nkif else None         # (c)  nki numpy (subset)
        assert rl == rs == rf and (rn is None or rl == rn)   # bit-exact, all masked
```

All masks are **explicit** (`& 0xFFFF` for `i16`): the comparator never trusts python's
unbounded ints — it normalises to the lane width before comparing, exactly as the leaf's
final `and`/`movzwl` does. A single mismatch prints `(op, a, b, expected, got)` and flips the
verdict to **DIVERGENCE**, which is the root-cause trigger handled in §6.

### 4.3 The ctypes live driver — leg (d), the reusable core

```python
import ctypes
from ctypes import c_void_p, c_uint, POINTER, byref

FISS = ".../ncore2gp/config/libfiss-base.so"
lib  = ctypes.CDLL(FISS, mode=ctypes.RTLD_GLOBAL)   # RTLD_GLOBAL: see GOTCHA below

def bind_binary(name):                               # 4-arg leaf
    fn = getattr(lib, name); fn.restype = None
    fn.argtypes = [c_void_p, c_uint, c_uint, POINTER(c_uint)]   # (xstate, A, B, *out)
    return fn

def bind_unary(name):                                # 3-arg leaf  -- DIFFERENT shape
    fn = getattr(lib, name); fn.restype = None
    fn.argtypes = [c_void_p, c_uint, POINTER(c_uint)]           # (xstate, A, *out)
    return fn

add = bind_binary("module__xdref_add_16_16_16")
abs_= bind_unary ("module__xdref_abs_16_16")

def live_binary(fn, a, b):
    out = c_uint(0); fn(None, a & 0xFFFF, b & 0xFFFF, byref(out)); return out.value & 0xFFFF
def live_unary(fn, a):
    out = c_uint(0); fn(None, a & 0xFFFF, byref(out));            return out.value & 0xFFFF
```

> **GOTCHA — `RTLD_GLOBAL` is mandatory.** Some leaves tail-call an intra-library helper
> (e.g. a saturating-pack `sats_*` from a `cvt*` leaf). Loading with the default
> `RTLD_LOCAL` leaves those helper symbols unresolved at the first such call → SIGSEGV.
> `mode=ctypes.RTLD_GLOBAL` makes the whole `libfiss-base` symbol table resolvable at
> `dlopen` time.

### 4.4 The method end-to-end — a real leaf driven LIVE

The harness above, run against the shipped binary, calling the actual
`module__xdref_*` leaves on the hardest edge inputs — **the vendor binary itself computes
every value**:

```
LIVE binary leaf results (libfiss-base.so dlopen'd, leaves called):
  add (0x7fff,0x0001)  = 0x8000   (+max+1 -> wrap to -min)          add_16_16_16
  add (0xffff,0x0001)  = 0x0000   (-1 + 1 -> 0)
  add (0x8000,0x8000)  = 0x0000   (-min + -min -> wrap)
  adds(0x7fff,0x0001)  = 0x7fff   (SAT clamp +max)                  adds_16_16_16
  adds(0x8000,0xffff)  = 0x8000   (SAT -min)
  sub (0x0000,0x8000)  = 0x8000   (0 - (-min) -> wrap -min)         sub_16_16_16
  min (0x8000,0x7fff)  = 0x8000   (signed min(-min,+max) = -min)    min_16_16_16
  minu(0x8000,0x7fff)  = 0x7fff   (UNSIGNED min(32768,32767))       minu_16_16_16
  abs (0x8000)         = 0x8000   (|-min| -> wrap -min)             abs_16_16  [UNARY]
  neg (0x8000)         = 0x8000   (-(-min) -> wrap -min)            neg_16_16  [UNARY]
  abssub(0x8000,0x7fff)= 0xffff   (|-min - +max| = 65535 WRAP, no SAT) abssub_16_16_16
```

Every value is exactly what legs (a)/(c)/(a′) predict. The two *separating* cells are the
strongest evidence the legs distinguish meaning rather than copy each other:

1. **`minu(0x8000,0x7fff) = 0x7fff`** while **`min(0x8000,0x7fff) = 0x8000`** — the unsigned
   op picks the **opposite** operand from the signed op, proving the legs distinguish
   signedness exactly (and that the binary's `minu` is the bare `cmp; cmova` while `min` is
   the sign-bias `cmovae`).
2. **`abssub(0x8000,0x7fff) = 0xffff`** **WRAPS** rather than saturating — proving the
   non-`S` `abssub` has **no clamp**, a subtle, easily-mis-specified behaviour that only a
   differential against the real value-function catches.

---

## 5. The assemble → decode device leg — model (b)

Leg (b) is the **identity / decode** check. It does not compute a value; it certifies that
the mnemonic the value leaf (d) claims to implement is the mnemonic the **device decoder**
produces from the actual instruction bytes — the type-check on the value oracle, joining (d)
to (a) by **name** (not by execution).

```
export XTENSA_CORE=ncore2gp
BIN=.../extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin   # device toolchain
# one IVP intrinsic per line into <fam>.s under a .text/_start label:
$BIN/xtensa-elf-as       -o <fam>.o <fam>.s          # 1,431,696-byte device assembler
$BIN/xtensa-elf-objdump  -d  <fam>.o                 # raw bundle bytes -> mnemonic
```

The intrinsic spellings are the uppercase `IVP_*` forms (`IVP_ADDNX16`, …); the decoder
prints the lowercase `ivp_addnx16` in **slot 3** of the 64-bit FLIX bundle, the other slots
`nop`. Worked decode for the elementwise family (memory-order LE bytes; the objdump hex
*column* is byte-reversed):

```
ivp_addnx16     2f 45 80 00 08 89 51 32      ivp_subnx16     2f 45 80 00 18 c7 52 32
ivp_addsnx16    2f 45 80 00 08 0a 50 32      ivp_subsnx16    2f 45 80 00 28 c7 52 32
ivp_minnx16     2f 45 80 00 78 47 52 32      ivp_maxnx16     2f 45 80 00 08 8b 51 32
ivp_minunx16    2f 45 80 00 c8 47 52 32      ivp_maxunx16    2f 45 80 00 28 47 52 32
ivp_avgnx16     2f 45 80 00 08 4b 50 32      ivp_avgrnx16    2f 45 80 00 08 8b 50 32
ivp_absnx16     2f 45 93 00 90 86 52 32      ivp_negnx16     2f 45 96 00 70 87 52 32
ivp_abssubnx16  2f 45 80 00 08 89 50 32      ivp_abssubunx16 2f 45 80 00 08 09 51 32
```

Format decode: `insn[0] = 0x0080452f` (`0x0093452f` for `abs`, `0x0096452f` for `neg`); the
`op0` nibble (`bits[3:0]`) = `0xf`; the predicate `(insn0 & 0x900000f) == 0xf` ⇒ **format
N0** (length 8, four slots); the opcode discriminator lives in `insn[1]` (slot 3). The decode
leg's job is to **reject a mis-mapping** (wrong width, wrong signedness variant) — it is the
type-check, not a second value.

> **NOTE — why assemble rather than carve from a shipped object.** Assembling a known
> intrinsic gives a clean one-op-per-bundle corpus whose mapping to the value leaf is
> unambiguous. The decode also confirms the assembler *accepted* the op for the `ncore2gp`
> config — i.e. the op **exists** on this exact Q7-NX1.1.4 image. For a family already proven,
> the cached decode text is the regression artifact.

---

## 6. The model-disagreement policy

When the §4.2 assert fires, the divergence is classified — and three of the four cases mean
*a model is wrong, not the binary*:

| Disagreement | Meaning | Action |
|--------------|---------|--------|
| LIVE vs FISS-lift | the **lift mis-read** the bytes | fix the lift; **the LIVE binary is authoritative** |
| LIVE vs SEM | the RTL model is wrong, **or** the leaf encodes an ABI quirk (hi-16 garbage, sign-fold) the RTL ignores | investigate — usually the leaf is right (see the `muluu`/`mulus`/`mulsu` asymmetry in [mac-multiply](mac-multiply.md)) |
| LIVE vs NKI (where nki defined) | a genuine **ISS-vs-framework** divergence | owned by the bit-exactness audit ([regfile-bridge-divergence](regfile-bridge-divergence.md)); none found in the int-ALU set |
| LIVE SIGSEGV / fork-fault | the leaf needs a **populated xstate** | escalate to the heavy leg (§7) — this is the wall |

For the elementwise-integer-ALU family, **agreement is complete: 0 mismatches** across
SEM / FISS-lift / NKI / LIVE-binary on every op the references jointly define. The deeper
reason there is *nothing to root-cause*: the integer ALU has **no free parameter** — wrap is
`mod 2^16` (defined), saturation clamps to fixed constants `{0x7fff, 0x8000}`, min/max
signedness is **structural** (a distinct opcode, not a mode), and average rounding is a fixed
`+1`-or-not. No rounding-mode, NaN-tie, or accumulation-order freedom exists for the
references to disagree about. **That is precisely why the integer ALU is the right opener** —
it establishes the method with an unambiguous oracle *before* the fp/activation/reduce slices
([fp-soft-float](fp-soft-float.md), [transcendental-seed](transcendental-seed.md),
[reduce-shift-shuffle](reduce-shift-shuffle.md)), where references legitimately *can* diverge
on rounding and order.

> **GOTCHA — the nki subset is a coverage scope note, not a weakness.** The nki simulator
> (leg c) only exposes `add/sub/min/max/abs` as **integer** primitives
> (`engine == gpsimd ⇒ np_op(d1, d2)`, native-int, no fp cast; `engine.gpsimd = 3`). The
> other nine ops (`adds/subs/minu/maxu/avg/avgr/abssub/abssubu/neg`) have no nki int primitive
> — nki leans on the Vector/DVE fp engine for them. Those ops are validated **3-way**
> (a + a′ + d, with the device decode b) rather than 4-way. The **LIVE binary closes them
> against the shipped oracle directly**; mark them `n/a` in the nki column, never hide them.

---

## 7. The known walls

The harness has exactly **one structural wall class**, confirmed by execution (not inferred):

### The soft-float-dispatch wall — `recipqli` QLI-refine leaves  `[HIGH/OBSERVED-elsewhere, CARRIED here]`

```
module__xdref_recipqli_1_1_1_1_1_32f_32f         @0x87df20   (fp32)
module__xdref_recipqli_1_1_1_1_1_64f_64f__0      @0x1b07e0   (fp64, sub-stage 0)
module__xdref_recipqli_1_1_1_1_1_64f_64f__1      @0x1b0870   (fp64, sub-stage 1)
```

These leaves load their QLI coefficient tables **inline** (read bit-exact) but route the
value-producing fp multiply/FMA through the ISS soft-float dispatch:

```asm
mov  0x88(%rsp),%rax        ; %rax derives from the xstate (arg0)
call *0x38(%rax)            ; 0x38 is a fn-pointer slot in the ISS dispatch object
```

A bare ctypes drive cannot populate that dispatch table:

```
xstate = NULL                              -> SIGSEGV (signal 11)
xstate = zeroed 0x4000 (slot 0x38 = NULL)  -> SIGSEGV (signal 11)
```

(proven by a **fork-isolated** probe so the harness survives; the fp64 `__0`/`__1` sub-stages
tail-call the fp32 body and inherit the same wall).

> **The structural signature of a wall** is `call *<off>(%rax)` where `%rax` came from
> `xstate`, **inside** the leaf. *Triage a new family before binding it:* disassemble each
> candidate leaf and check for `call *0x..(%rax)`. If present, schedule the leaf for the
> heavy leg, not bare ctypes.

**What is / isn't walled (the honest boundary).** Every callback-free integer / word /
select leaf is **bare-leaf drivable** — it stores its result in-line, `xstate` unused,
ctypes-OK (the ALU, MAC/mul incl. ext-ABI asymmetry, convert/pack word-selects + sats clamp,
shift, reduce, compare/classify, predicate-tail masked write, move, sel/dsel crossbar). The
**only** walled leaves are the three `recipqli` QLI-refine leaves — the sole ones that
delegate arithmetic to the ISS soft-float dispatch object. Their value is reachable only via
the **heavy leg (model D)**: load the kernel object into the `libcas-core` / `libsimxtcore`
model, register a single-step / `InstDone` callback, and read the destination vector register
through the state-introspection API — the full-ISS escalation tier described in
[iss-oracle-synthesis](../iss/iss-oracle-synthesis.md). The carry-forward residual is the
`recipqli` soft-float value; it surfaces in [transcendental-seed](transcendental-seed.md).

### Secondary hazards (handled, not walls)  `[HIGH/OBSERVED-elsewhere]`

- **Hi-16 garbage in operand registers.** `muluu` does a bare `imul` (propagates the high-16
  garbage); `mulus`/`mulsu` do `shl15`/`sar15` (sign-fold). The corpus **must** include
  values with set high-16 bits to exercise the ABI — see [mac-multiply](mac-multiply.md).
- **Two-word (48-bit) outputs.** Bind the out-pointer as **two** 32-bit words
  (`POINTER(c_uint*2)`, or read `out0 = *(out)`, `out1 = *(out+1)`) — see
  [convert-pack-cast](convert-pack-cast.md) / [mac-multiply](mac-multiply.md).
- **Scalar base-ISA vs vector lanes.** The Xtensa24 AR datapath and the bridge between
  scalar registers and vector lanes are validated separately in
  [regfile-bridge-divergence](regfile-bridge-divergence.md).

---

## 8. Adding a NEW kernel family — the recipe

Six steps; the elementwise-integer-ALU instance is the template. Each VAL-family page in
this Part is one application of these steps; the cross-Part synthesis lives in
[capstone-matrix](capstone-matrix.md).

1. **Enumerate the leaves.** `nm -D --defined-only <abs>/libfiss-base.so | rg
   'module__xdref_<root>' | sort`. The width suffix gives `nargs` (one input width ⇒ unary
   ⇒ 3-arg) **and** the ctypes types.
2. **Disassemble each body (the ground truth).** `objdump -d
   --disassemble=module__xdref_<leaf> <abs>/libfiss-base.so`. Read the `movswl`/`movzwl`
   (signedness fix), the `and`-masks (output-width fix), the `cmov`/branch (select/saturate).
   Note **which register holds the out-pointer** to confirm the ABI shape. Lift it
   **literally** into `fiss_<op>` (no paraphrase).
3. **Derive the RTL model `sem_<op>` independently** from the op definition
   (carry-break wrap, `GRP_SAT` clamp `{0x7fff,0x8000}`, AVG/AVGR rounding, sign-bias
   compare) — *not* by copying the lift. Independence is what makes the cross-check
   meaningful; cite [formal-isa-model](../isa/semantics/formal-isa-model.md).
4. **Bind the live leaf.** Binary → `argtypes=[c_void_p,c_uint,c_uint,POINTER(c_uint)]`;
   unary → `argtypes=[c_void_p,c_uint,POINTER(c_uint)]`. For 48-bit/two-word outputs bind two
   words (§7). For fp leaves present the bit-pattern as `c_uint` and reinterpret
   (`struct.pack`/`unpack`) — but **triage the wall first** (§7: grep the body for
   `call *0x..(%rax)`).
5. **Wire the nki leg if a primitive exists.** Check `_NUMPY_FUNC_MAP` in
   `nki/backends/simulator/tensor_ops.py`; add the `nki_lane` wrapper, or pass `None`
   (documented "no int prim").
6. **Decode-confirm + run.** Add the `IVP_<MNEMONIC>` line to `<fam>.s`, assemble + objdump
   to confirm the mnemonic (§5); run the live comparator. **Demand:** LIVE == lift == SEM
   (== NKI where defined), bit-exact over edge-cartesian + fuzz. Print the comparison count.
   Any mismatch → §6 policy.

### The name ↔ mnemonic map (the binding key)  `[HIGH/OBSERVED]`

```
module__xdref_<op>_<outW>_<inW...>   <->   ivp_<op>n<x|_>W<...>
```

The xdref suffix is bit-widths; the IVP mnemonic encodes lanes-×-width (`nx16` = N lanes of
16 bits; `n_2x32` = N/2 lanes of 32). The op root is shared (`add ↔ addnx16`,
`adds ↔ addsnx16`, `abssub ↔ abssubnx16`). For an ambiguous root, the decode leg (§5)
disambiguates — assemble the candidate `IVP_*` and confirm it is accepted and decodes to the
intended mnemonic.

---

## 9. Confidence / observation ledger

| Claim | Tag | Ground |
|-------|-----|--------|
| 864 `module__xdref_*` value leaves; 948 `opcode__ivp_*__stage_5` handlers | HIGH / OBSERVED | `nm -D \| rg -c` against the 12,330,016-byte `libfiss-base.so` |
| BINARY ABI `f(rdi,esi,edx,rcx*out)`; UNARY ABI `f(rdi,esi,rdx*out)`; out-pointer register differs | HIGH / OBSERVED | disassembled `add_16_16_16`@0x858480 (store via `%rcx`) and `abs_16_16`@0x82d060 (store via `%rdx`) |
| arg0 `%rdi` (xstate) dead for callback-free int/word leaves | HIGH / OBSERVED | live ctypes drive passed `None` and the leaves computed correctly |
| live ctypes drive reproduces every §4.4 edge value (add/adds/sub/min/minu/abs/neg/abssub) | HIGH / OBSERVED | real `libfiss-base.so` `dlopen`'d and 8 leaves called |
| the `adds` 17-bit-overflow bit-trick (`bit15!=bit16`, `(s<<16)sar31 & 0x7fff \| (s>>1)&0x8000`) | HIGH / OBSERVED | disassembled `adds_16_16_16`@0x85aa10 — matches the lift byte-for-byte |
| `min`/`max` sign-bias (`!sign<<15 \| (v&0x7fff)`, `cmovae`/`cmovbe`); `minu`/`maxu` bare `cmp;cmova/cmovb` | HIGH / OBSERVED | disassembled `min`@0x8584b0, `minu`@0x8584f0 |
| four legs are structurally distinct algorithms (clamp vs bit-trick vs numpy-wrap) | HIGH / OBSERVED | the disassembled bodies vs the lifted RTL vs nki `_NUMPY_FUNC_MAP` |
| device FLIX decode: 14 bundles, op0=0xf, N0 format, slot3 op | HIGH / OBSERVED-elsewhere | the device `xtensa-elf-as`/`objdump` (XTENSA_CORE=ncore2gp), cached decode |
| the `recipqli` soft-float-dispatch wall (`call *0x38(%rax)` from xstate; SIGSEGV on NULL) | HIGH / CARRIED | the three `recipqli` leaf addresses confirmed by `nm -D`; the SIGSEGV is fork-isolation-proven in the residual-closure pass |
| `libcas-core` per-instance state = 4,852,208 B (0x4a09f0); decode+timing half of the oracle | HIGH / CARRIED | `0x4a09f0 = 4852208` arithmetic confirmed; the size is the Part-14 oracle datum |
| `LIBFISS_VERSION` value | MED / CARRIED | the version lives inside `libfiss_config_metadata`@0x951940, not a standalone exported symbol; the numeric value is carried from the provenance ledger, not re-decoded here |
| the >1M aggregate comparison figure across the VAL lane | MED / INFERRED | summed from the per-family runs, not re-counted leaf-by-leaf |

No silicon-generation, gen-count, or codename is inferred anywhere on this page: every
`16/8/32-bit`, `NX16`, `0x7fff/0x8000`, `N0/slot3` token is a datapath-width / FLIX-format /
ISA-lane axis of the single Cairo config, not one of the five firmware-image generations.
