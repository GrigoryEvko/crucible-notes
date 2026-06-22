# Special-Function, Transcendental & Atomic Units

> *Recovered from the decoded per-architecture SASS instruction tables in
> nvdisasm V13.1.115 (CUDA 13.1), cross-validated against real ptxas v13.x SASS
> emitted for SM75 (Turing) → SM90a (Hopper) and confirmed to extend to SM100/SM120
> (Blackwell). Every opcode, modifier, and field family below was confirmed by
> assembling probe PTX and reading the resulting machine code, or by the exact
> ptxas diagnostic. Latency magnitudes are the dependency-edge weights the assembler
> schedules to; where a number is arch-specific it is called out.*

Three of the SM's functional units sit *off* the coupled integer/FP math pipe and are
driven through **scoreboards** instead of fixed dispatch stalls: the
**special-function unit** (`MUFU` — reciprocal, rsqrt, exp2, log2, sin, cos, sqrt,
tanh), the **conversion / extended-math unit** (`F2F`/`F2I`/`I2FP`/`FRND`, `POPC`,
`FLO`, `BREV`), and the **atomic / reduction unit** (`ATOMG`/`ATOMS`/`RED`/`REDG`/`REDS`,
`SUATOM`/`SURED`, and the warp-collective `REDUX`). The FP64 (double-precision) datapath
is a fourth: a redirected pipe that, on most consumer parts, is a *narrow* unit the
compiler builds full-precision reciprocal/sqrt/division *around* using `MUFU` seeds.

This page is the unit-level companion to the
[Execution Model](execution-model.md) (which fixes the scoreboard / `VIRTUAL_QUEUE`
mechanism these units plug into) and the
[Memory Model](memory-model.md) (which fixes the scope/ordering grading the atomics
carry). The coupled integer ALU's bit-exact semantics (`IADD3`/`LOP3`/`SHF`/`PRMT`)
are modelled separately in the functional checker and are summarised at the end.

---

## 1. The special-function unit — `MUFU`

### 1.1 The ten sub-functions

`MUFU` is a single SASS opcode with a 4-bit **sub-function selector**. nvdisasm
prints it as a suffix; the selector index → mnemonic mapping recovered from the
instruction tables is, in field order:

| index | suffix | function | operand |
|--:|---|---|---|
| 0 | `.COS` | cos(x), x in revolutions after range reduction | f32 |
| 1 | `.SIN` | sin(x), x in revolutions after range reduction | f32 |
| 2 | `.EX2` | 2^x | f32 |
| 3 | `.LG2` | log2(x) | f32 |
| 4 | `.RCP` | 1/x (single-`MUFU` ≈12-bit seed) | f32 |
| 5 | `.RSQ` | 1/√x (seed) | f32 |
| 6 | `.RCP64H` | high-word seed for the FP64 reciprocal NR loop | f64 hi |
| 7 | `.RSQ64H` | high-word seed for the FP64 rsqrt NR loop | f64 hi |
| 8 | `.SQRT` | √x | f32 |
| 9 | `.TANH` | tanh(x) | f32 |

All ten are present and identically encoded on SM75, SM80, SM86/89, SM90, and
SM100/SM120 — assembling `rcp/sqrt/rsqrt/sin/cos/lg2/ex2/tanh.approx.f32` and
`rcp/sqrt.rn.f64` and disassembling reproduces exactly this set on every target.
SM103 adds a packed-FP16 SIMD form of the special-function unit (a `MUFU` that
processes an `f16x2` pair per issue); it is absent on the consumer line.

The operand grammar has three forms — register, immediate, and constant-bank — with
the source operand carrying the usual **negate** (`-`) and **absolute-value** (`|x|`)
modifiers folded in by the assembler:

```
MUFU.func Rd, {-}{|}Rb{|}            // register
MUFU.func Rd, #imm32                 // immediate
MUFU.func Rd, {-}{|}c[bank][offs]{|} // constant bank
```

So `rcp.approx.f32 %f, |%x|` emits a single `MUFU.RCP Rd, |Rb|` — the `abs` does
not cost a separate instruction.

### 1.2 The approximation model

The hardware evaluates each transcendental by **piecewise polynomial interpolation**:
the input mantissa indexes a small **coefficient ROM** holding the per-interval
polynomial constants, and the unit evaluates a low-degree (quadratic-class) polynomial
on the in-interval fraction. The single-`MUFU` result is therefore a *bounded-accuracy
approximation* (~the `.approx` PTX flavour), not a correctly-rounded IEEE result — the
selector picks which ROM table and which post-scale the unit applies. This is why
`MUFU.RCP`/`.RSQ`/`.SQRT` alone carry only seed-grade precision (≈12 bits for the
single-precision reciprocal seed) and why anything needing more is *refined in
software* (§1.3).

Two ground-truth fingerprints of the ROM model are visible in emitted SASS:

* **Range reduction for sin/cos.** `sinf`/`cosf` lower to a pre-multiply by
  1/(2π) (`FMUL.RZ Rt, Rx, 0.15915494…`) feeding `MUFU.SIN`/`MUFU.COS` — the unit's
  ROM is indexed in *revolutions*, so the compiler reduces the argument to that
  domain before the `MUFU`.
* **Exponent fix-ups.** `rsqrtf`/`sqrtf`/`lg2f` on denormal/large inputs are
  pre-scaled (`@!P1 FMUL Rd, Rd, 4096`, `@!P1 FADD Rd, Rd, -24`, etc.) around the
  `MUFU`, compensating the ROM's limited input exponent range with a power-of-two
  pre/post-scale chosen by a range predicate.

### 1.3 Refined (IEEE) single-precision: one seed + a Newton step

`rcp.rn.f32` / `rsqrt`-derived division / `sqrt.rn.f32` do **not** emit a single
`MUFU`. ptxas emits the `MUFU` seed followed by a **Newton–Raphson correction** in the
coupled FMA pipe. The recovered IEEE reciprocal sequence is exactly:

```
MUFU.RCP   R3, R0            ; y0 ≈ 1/x  (12-bit seed)
FFMA       R2, R3, R0, -1    ; e  = x·y0 − 1
FADD.FTZ   R2, -R2, -RZ      ; −e (flush-to-zero)
FFMA       R4, R3, R2, R3    ; y1 = y0 + y0·(−e) = y0·(2 − x·y0)   → ~24-bit
```

— one `MUFU` plus one Newton iteration (2× `FFMA` + 1× `FADD.FTZ`). `sqrt.rn.f32`
uses the `RSQ` seed and a half-step Heron iteration (`FMUL.FTZ`, `FFMA`, `FFMA`).
The `.FTZ` on the correction adds is the unit's flush-to-zero path; it is emitted
unconditionally on the refinement, independent of the kernel's global FTZ flag, to
keep the iteration numerically well-behaved.

### 1.4 FP64 reciprocal / rsqrt: the `64H` seeds

`rcp.rn.f64` / `sqrt.rn.f64` / `div.rn.f64` cannot be seeded from a 32-bit `MUFU`.
The unit exposes **`MUFU.RCP64H`** and **`MUFU.RSQ64H`**: each takes the *high* 32-bit
word of a double and returns the high word of an FP64 seed (the low word is set to
zero, giving a coarse double approximation). ptxas then runs a **full FP64
Newton–Raphson refinement in `DFMA`** to reach 53-bit precision. The observed
`rcp.rn.f64` body is:

```
MUFU.RCP64H R5, R3          ; double seed (high word) ≈ 1/x
DFMA  R6, -R2, R4, 1        ; e  = 1 − x·y0
DFMA  R6, R6, R6, R6        ; e + e²   (Halley-style doubling)
DFMA  R6, R4, R6, R4        ; y1 = y0 + y0·(…)
DFMA  R8, -R2, R6, 1        ; second residual
DFMA  R4, R6, R8, R6        ; y2  → 53-bit
```

— one `RCP64H` seed plus ~2–3 `DFMA` Newton iterations. `sqrt.rn.f64` uses
`RSQ64H` plus a `DMUL`/`DFMA` Heron sequence, and `div.rn.f64` extends the reciprocal
sequence with a final `DFMA.RM`/`DFMA.RP`-bracketed last bit and a `DSETP`-driven
remainder correction. The whole thing is built by the code generator out of the seed +
DP-FMA primitives; the seed is the *only* part the special-function unit contributes.

### 1.5 Pipe, latency, and throughput

`MUFU` (and the conversions and `POPC`/`FLO`/`BREV`, §2) dispatch into the **`MUFU`
virtual queue** as **decoupled** ops with a **read+write scoreboard**: the producer
arms a write scoreboard on issue, and a dependent consumer waits on it via the
`req_bit_set` mask rather than eating a fixed stall. The latency the assembler
schedules to is the **band-13 magnitude** — the special-function/extended-math result
band — which in the unit model is a 16-cycle (extended 64-bit math unit class) result
delivered through the scoreboard. The execution-model page derives band-13 as `6 + 7` (the 6-cycle
scalar result band plus the 7-cycle read-scoreboard base on Turing/Volta; the
read-scoreboard base rises to 8 on Ampere); `MUFU`/`F2*`/`POPC` carry that magnitude
but **as a scoreboard, not a stall**.

Throughput: the special-function unit issues at a fraction of the FMA rate (the
classic **1/4-rate SFU** — one `MUFU` per 4 cycles per scheduler quadrant against
one `FFMA` per cycle), so a transcendental-heavy loop is SFU-bound, not issue-bound.
The scheduler models this by keeping the queue ordered and tracking per-queue
readiness; an independent `FFMA` does not block on an outstanding `MUFU` because
they live in different queues.

---

## 2. The conversion / extended-math unit

The same decoupled `MUFU`-class unit (the extended 64-bit math unit, read+write
scoreboard) hosts the **format conversions** and a handful of bit-manipulation ops.

### 2.1 Float ↔ int ↔ float

| SASS op | PTX | meaning |
|---|---|---|
| `F2F.<dst>.<src>` | `cvt.<rnd>.f*.f*` | float → float (different formats) |
| `FRND.<fmt>.<rnd>` | `cvt.<rnd>.f*.f*` same-format | float → float **same format**, just re-round |
| `F2I.<dst>.<src>` | `cvt.<rnd>i.s*/u*.f*` | float → integer |
| `I2F` (SM75/SM80) / `I2FP.<dst>.<src>` (SM86+) | `cvt.<rnd>.f*.s*/u*` | integer → float |
| `I2I.<dst>.<src>` / `I2IP.<dst>` | `cvt[.sat].s*/u*.s*/u*` | integer → integer (resize/saturate) |

The float↔float split is real and recovered from the lowering: when source and
destination format are **identical** the compiler emits **`FRND`** (a pure re-round —
e.g. `cvt.rzi.f32.f32`); when they differ it emits **`F2F`** (e.g.
`F2F.F32.F64` for double→float). The **`I2F` → `I2FP` rename at SM86** is a confirmed
binary fact: `cvt.rn.f32.s32` disassembles as `I2F` on SM75/SM80 and as
`I2FP.F32.S32` from SM86 onward (SM86/SM89/SM90/SM100/SM120). Consumer Blackwell
additionally exposes
**uniform-register** conversions `UF2F`/`UF2I`/`UI2F`/`UI2I`/`UFRND` that run the same
conversions on the uniform datapath.

### 2.2 Rounding and saturation

Conversions carry an explicit **rounding mode** and an optional **flush-to-zero** and
**saturate**:

* **FP rounding** (`F2F`/`FRND`/`I2F`): `.RN` (nearest-even), `.RM` (toward −∞),
  `.RP` (toward +∞), `.RZ` (toward zero) — the same four modes the FP ALU uses.
* **Integer rounding** (`F2I`): the float-to-int round is a *direction*, printed as
  `.TRUNC` (round-toward-zero, `cvt.rzi`), `.FLOOR` (`cvt.rmi`), `.CEIL` (`cvt.rpi`),
  or the default round-to-nearest. Probe SASS: `cvt.rzi.ftz.s32.f32` →
  `F2I.FTZ.TRUNC.NTZ`, `cvt.rmi` → `F2I.FLOOR.NTZ`, `cvt.rpi` → `F2I.CEIL.NTZ`.
* **`.FTZ`** flushes denormal inputs/outputs to zero (set by the kernel's denormal
  policy or `.ftz` on the PTX `cvt`).
* **`.NTZ`** ("no tininess") rides on `F2I` to fix the IEEE tininess-detection corner.
* **`.SAT`** clamps to the destination range; `cvt.sat.s8.s32` →
  `I2I.S8.S32.SAT`. Destination/source format suffixes come from the recovered
  enums — integer `{U8,S8,U16,S16,U32,S32,U64,S64}`, float `{F16,F32,F64}`.

All conversions take the source **negate**/**abs** modifiers and a byte/half
**selector** (`.selB`) on the narrow forms, identical to `MUFU`.

### 2.3 Bit-manipulation companions

`POPC` (population count), `FLO` (find-leading-one), `BREV` (bit-reverse), and `FCHK`
(division-validity check) share the **`MUFU` queue** and the read+write scoreboard —
they are *not* coupled ALU ops. This is why a `POPC`→consumer edge resolves through a
scoreboard like a `MUFU`, not as a fixed 4-cycle stall. (`PRMT`, the byte-permute, is
the exception: it *is* a coupled ALU op — see §5.)

---

## 3. The FP64 (double-precision) unit

### 3.1 Op set and pipe

The double-precision datapath is exposed as four SASS ops:

| SASS op | PTX | |
|---|---|---|
| `DADD` | `add.f64` / `sub.f64` | FP64 add |
| `DMUL` | `mul.f64` | FP64 multiply |
| `DFMA[.RM/.RP]` | `fma.rn.f64` | FP64 fused multiply-add (the workhorse of the NR loops) |
| `DSETP.<cmp>.<bop>` | `setp.<cmp>.f64` | FP64 compare → predicate(s) (`DSETP.GTU.AND`, `DSETP.NE.AND`, …) |

These dispatch into the **`FP64` / redirected** virtual queue as **redirected** ops
with a read+write scoreboard. "Redirected" means the SM funnels them to a shared FP64
resource rather than issuing them in the per-lane FMA pipe — the consequence is the
classic **FP64:FP32 throughput ratio**. The FP64 result latency is **8 cycles on
Turing/Volta-class and 6 cycles on Ampere-class** (vs 4 for an FP32 `FFMA`), and the
issue-rate model keeps the FP64 resource busy ~2× as long as the FP32 FMA, so a
DP-heavy loop is FP64-resource-bound. The
[architecture-evolution](architecture-evolution.md) diff shows the queue staying on the
**emulated/redirected** path through Hopper and gaining a **native `FP64` pipe only on
SM120/121** (consumer Blackwell), where the redirected-emulation queue population drops
to zero — that is the point at which double precision moves off the shared redirected
unit into its own pipe.

### 3.2 FP64 tensor (`DMMA`)

FP64 *matrix* multiply is a separate tensor path, **`DMMA`** (added at Ampere SM80,
the FP64 tensor core). It is one entry in the legacy `HMMA`/`IMMA`/`DMMA` tensor
family and is not part of the scalar FP64 unit; on the datacenter line it carries the
high FP64-tensor throughput, and on consumer Blackwell it migrates into the tcgen05 /
native tensor path. See the tensor coverage in the execution and architecture pages.

---

## 4. The atomic / reduction unit

### 4.1 The four memory-space forms

Atomics split by **memory space** and by **whether a value is returned**:

| SASS op | space | returns? | PTX |
|---|---|---|---|
| `ATOMG` / `ATOM` | global | **yes** (`Rd`) | `atom.global.*` |
| `REDG` / `RED` | global | **no** | `red.global.*` (or dead-result `atom`) |
| `ATOMS` | shared | **yes** | `atom.shared.*` |
| `REDS` | shared | **no** (SM100+) | `red.shared.*` |
| `SUATOM` / `SURED` | surface | yes / no | `suld`/`sured` surface atomics |

`ATOMG`/`ATOM` writes the pre-op value into `Rd`; the no-return forms (`REDG`/`RED`,
`REDS`) omit the destination entirely. **The compiler chooses the no-return form when
the returned value is dead.** This is a real optimisation, visible in ground truth:
assembling `atom.global.add.u32 %r, [p], %v` and then never reading `%r` makes ptxas
emit `REDG.E.ADD.STRONG.GPU` (no `Rd`), not `ATOMG`. The downgrade is gated on the
**global / surface** spaces — a dead-result *shared* atomic stays `ATOMS` (the
reduction-only shared opcode `REDS` is an SM100+ addition); and on the **operation**
having a reduction form (exchange and compare-and-swap never downgrade, because their
whole point is the read-back). A reduction is strictly cheaper because it never has to
forward a value back: the returning forms arm a **read+write** scoreboard (the consumer
waits on the write-back), the no-return forms arm only a **read** scoreboard.

Two further mappings ride the atomic unit, both Ampere-and-later and confirmed in
emitted SASS: `mbarrier.arrive` lowers to an **`ATOMS` arrive-count** atomic
(`ATOMS.POPC.INC.32` — an atomic population-count increment on the shared barrier
word), and the atomic op field carries an `ARRIVE` form for the barrier-arrival path.
These are the same functional unit as the data atomics, not a separate barrier engine.

### 4.2 The operation set

The atomic operation selector enumerates, in field order:

| index | `ATOM`/`ATOMG`/`ATOMS` | `RED`/`REDG`/`REDS` |
|--:|---|---|
| 0 | `.ADD` | `.ADD` |
| 1 | `.MIN` | `.MIN` |
| 2 | `.MAX` | `.MAX` |
| 3 | `.INC` | `.INC` |
| 4 | `.DEC` | `.DEC` |
| 5 | `.AND` | `.AND` |
| 6 | `.OR` | `.OR` |
| 7 | `.XOR` | `.XOR` |
| 8 | `.EXCH` | — (no exchange: it must return) |
| 9 | `.SAFEADD` (`ATOMG` only) | — |

`.INC`/`.DEC` are the *wrapping* counter ops (increment-modulo / decrement-modulo a
limit), distinct from `.ADD`. `.EXCH` (atomic exchange) exists only on the
**return** forms, because an exchange with no read-back would just be a store.
`.SAFEADD` is the `ATOMG`-only ECC-safe add. The compare-and-swap is a **separate
opcode** (`ATOMG.CAS` / `ATOMS.CAS`, §4.4), not an entry in this table.

### 4.3 Type support — INT and FP

The operation carries a **size/type** field:

* **Integer** — `.32` / `.64` (and sub-word for shared), signed where it matters:
  `.MIN`/`.MAX` print the signed form as `.S32` (`ATOMG.E.MIN.S32`) and default to
  unsigned otherwise; `.AND`/`.OR`/`.XOR`/`.EXCH`/`.CAS` are typeless bit ops; `.ADD`
  works on `u32`/`s32`/`u64`/`s64`.
* **Floating-point add** — `.F32` and `.F64` are native on the **global** path:
  `atom.global.add.f32` → `ATOMG.E.ADD.F32.FTZ.RN.STRONG.GPU` and
  `red.global.add.f64` → `REDG.E.ADD.F64.RN.STRONG.GPU`. FP atomics carry their own
  `.FTZ` and `.RN` rounding qualifier because the add is performed in the atomic unit.
* **Packed and vector FP** — a packed-half add (`atom.global.add.f16x2`) gets its own
  size encoding, and **vector** FP atomics (`atom.global.add.v2/.v4.f32`, the `MIN`/`MAX`
  half forms) are an **Ampere-and-later, global/generic-only** capability: the vector
  form supports `f32`/`f16` with `ADD`/`MIN`/`MAX`, but `f32` only with `ADD`.
* **`.E`** on the global forms is the *extended* (64-bit) address flag.

The shared-memory path is narrower: shared **FP** atomics are not native. An
`atom.shared.add.f32` is emitted as a **CAS-spin emulation** — `ATOMS.CAST.SPIN`
(a hardware compare-and-store-conditional that loops until it wins) — rather than a
native FP `ATOMS.ADD`. The shared integer ops (`ATOMS.ADD/.MIN.S32/.EXCH/.CAS`) are
native.

### 4.4 Compare-and-swap

CAS is its own opcode with an extra operand. The selector `casOpStr = {.CAS, .CAST}`
distinguishes plain compare-and-swap (`.CAS`) from the store-conditional `.CAST`
("compare-and-store") used by the spin-emulation. `ATOMG.CAS Rd, [Ra], Rb, Rc` takes
**both** a compare value (`Rb`) and a swap value (`Rc`) and returns the old value;
`atom.global.cas.b32 %r, [p], %cmp, %new` → `ATOMG.E.CAS.STRONG.GPU PT, Rd, [Ra], Rb, Rc`.
The shared form `ATOMS.CAS` / `ATOMS.CAST.SPIN` is what backs every emulated shared
atomic (FP add, 64-bit min/max, etc.).

### 4.5 Scope and ordering

Global atomics carry two graded modifier fields, matching the
[memory model's](memory-model.md) `CTA < SM < GPU < VC < SYS` scope lattice:

* a **scope** modifier — `.CTA` / `.SM` / `.GPU` / `.SYS` — for how far the atomic's
  ordering and visibility extend. `atom.cta.*` → `.SM`-scoped (this-SM) ; `atom.gpu.*` →
  `.GPU` ; `atom.sys.*` → `.SYS`. Default is `.GPU`.
* an **ordering** modifier — `.CONSTANT` / `.WEAK` / `.STRONG` / `.MMIO`.
  `.STRONG` is the ordered (acquire-on-load / release-on-store) flavour PTX
  `atom.acquire`/`atom.release`/`atom.acq_rel` lower to; `.WEAK` is relaxed.
  Probe SASS: `atom.relaxed.gpu` and `atom.acquire.cta`/`atom.release.sys` all render
  with `.STRONG` plus the requested scope, because the unit enforces the scope and the
  fence ordering is carried by the `MEM_SCBD` memory-pipe scoreboard and `MEMBAR`/`FENCE`
  rather than a per-atomic weak/strong bit in the common case — the
  acquire/release *fence* attaches around the atomic, the atomic itself stays
  `.STRONG.<scope>`.

**Shared atomics carry no scope/ordering modifier** — `ATOMS`/`REDS` are implicitly
CTA-scoped (the shared window is per-CTA), so neither field is present on the shared
forms. Surface atomics (`SUATOM`/`SURED`) carry their own
`{.cop}{.sz}{.sem}.sco{.PRIVATE}{.clamp}` modifier stack (cache-op / size / ordering /
scope / private / clamp) and a surface-specific weak/strong semantic.

### 4.6 Pipe and latency

Global atomics dispatch into the **`ATOM_UNORDERED`** virtual queue (a writeback-only,
unordered queue) and the `LSU` resource; the read-back form (`ATOMG`) arms a
read+write scoreboard, the no-return form (`REDG`) arms only a read scoreboard
(nothing to forward). Shared atomics go through the **`AGU`** queue (the
shared/local address-generation path). The atomic latency is a long, variable,
scoreboard-tracked latency (a round-trip to L2 / the shared bank), never a fixed
stall — a dependent consumer always waits on the scoreboard.

### 4.7 The warp-collective reduction — `REDUX`

`REDUX` is a **different unit**: a single-instruction **warp-wide reduction** (`redux.sync`)
that combines one value from every active lane and broadcasts the result, with no
memory traffic at all. It is not a memory atomic — it is a cross-lane datapath
operation. The recovered op set: `REDUX.SUM` (the disasm prints plain `REDUX` for the
default/AND or the additive form depending on op), `REDUX.MIN.S32`, `REDUX.MAX`,
`REDUX.AND` (default), `REDUX.OR`, `REDUX.XOR`. `redux.sync.add.u32` →
`REDUX.SUM UR<n>, R<n>` — note the **uniform-register destination**, because the
reduced scalar is identical across the warp. `REDUX` arrives at **Ampere (SM80)** and
the compiler will *prefer* it over an atomic-tree when it recognises a warp-uniform
reduction pattern. (`CREDUX`/`UREDGR` are later cluster/uniform extensions.)

---

## 5. The coupled integer/FP ALU (band-6)

The high-throughput integer and FP scalar ops — `IADD3`, `LOP3`, `SHF`, `IMAD`,
`LEA`, `BMSK`, `SGXT`, `PRMT`, `FADD`, `FMUL`, `FFMA`, `ISETP`/`FSETP`, `SEL`/`MOV` —
are **coupled** ops: they issue in the per-lane math pipe with a **fixed dispatch
stall** and **no scoreboard**. The producer→consumer stall is the band-6/coupled
base: **4 cycles** same-pipe (`IADD3`→`IADD3`, `FFMA`→`FFMA`), **5** cross-pipe
(`LOP3`/`FMUL`→consumer), and the **13** control band when the producer writes a
condition-code or predicate (`ISETP`→predicated op). `IMAD`/`IMAD.WIDE`/`FFMA`/`FADD`/
`FMUL`/`FSWZADD`/`IDP` use the FMA resource; `IADD3`/`LOP3`/`SHF`/`LEA`/`BMSK`/`SGXT`/
`PRMT`/`ISETP`/`FSETP`/`SEL`/`MOV`/`PLOP3` use the ALU resource — both coupled, both
4-cycle base.

The **bit-exact semantics** of the full integer/logic set are not re-derived here:
they are modelled, executably and in closed form, in
`decoded/sass-tools/sass_sem_int.py`, with each op a single function whose result
reproduces the silicon bit-for-bit. That model is **GPU-ground-truthed**: a companion
harness (`sass_gpu_probe.py`) emits PTX that ptxas lowers to the exact SASS op,
confirms the mnemonic+modifiers in the `cuobjdump -sass` decode, runs the op on the
sm_89 (Ada) device over an edge-case corpus (`0`, `±1`, `INT_MIN/MAX`, carry
boundaries, shift ≥ 32, every `PRMT` selector nibble), and asserts the model matches —
the committed gate passes **6007/6007** differential checks across 25 op-probes. A few
points worth recording from that exercise, because they are easy to get wrong:

- **`LOP3`** indexes its 8-bit LUT as `i = (a<<2)|(b<<1)|c` (the `a` bit is the MSB of
  the index); `immLut` is the one-line truth-table byte built from the canonical
  selectors `a=0xF0`, `b=0xCC`, `c=0xAA` — no 256-row table.
- **`SHF`** funnels `{hi:lo}` with the *low* word first and the shift count last; the
  `.W` (wrap) count masks `& 0x1f` and the clamp form saturates the count at `32` (not
  the 64-bit datapath width).
- **`PRMT`** has seven modes — the default index mode (low 3 bits pick a pool byte,
  bit 3 sign-replicates) plus `F4E`/`B4E`/`RC8`/`ECL`/`ECR`/`RC16`, each a fixed
  byte-pattern keyed by the low selector bits — all native SASS on this generation.
- **`BMSK`** wrap takes base and width each `mod 32`; clamp saturates base at 32 and
  width at `32-base`.
- **`BFE`/`BFI`** have **no native opcode** on Volta..Ada — ptxas synthesizes them from
  `PRMT`/`SHF`/`SGXT`/`BMSK`/`LOP3` — yet the synthesized result still matches the
  closed-form `bfe`/`bfi` model exactly. `ICMP` and the scalar 32-bit `VABSDIFF`
  likewise lower to `ISETP`+`SEL` and `PRMT`+`IADD3`; `VABSDIFF4` (4-way byte SAD) is
  native. The 64-bit `IADD3`+`IADD3.X` carry chain and the `SHF.R.S32` signed funnel
  are verified through `add.s64` / `shr.s64`.

This page covers only their **unit placement** (coupled, band-6, no scoreboard) so the
contrast with the decoupled special-function/atomic units above is explicit: the four
scoreboard-driven units (`MUFU`, conversions, FP64, atomics) are exactly the ones the
coupled ALU is *not*.

---

## 6. Unit summary

| unit | SASS ops | pipe / queue | scoreboard | latency class |
|---|---|---|---|---|
| **Special-function** | `MUFU.{RCP,RSQ,EX2,LG2,SIN,COS,SQRT,TANH,RCP64H,RSQ64H}` | `MUFU` (decoupled) | read+write | band-13 (16-cyc class), ~1/4 rate |
| **Conversion / ext-math** | `F2F`,`FRND`,`F2I`,`I2F`/`I2FP`,`I2I`/`I2IP`,`POPC`,`FLO`,`BREV`,`FCHK` | `MUFU` (decoupled) | read+write | band-13 |
| **FP64** | `DADD`,`DMUL`,`DFMA`,`DSETP` | `FP64`/redirected | read+write | 8 cyc (Volta) / 6 cyc (Ampere); native pipe SM120+; `DMMA` tensor |
| **Atomic (global)** | `ATOMG`/`ATOM`,`REDG`/`RED` `{ADD,MIN,MAX,INC,DEC,AND,OR,XOR,EXCH,SAFEADD,CAS}` × `{32,64,F32,F64}` × `{CTA,SM,GPU,SYS}` | `ATOM_UNORDERED` / `LSU` | rd(+wr) | long, variable, scoreboard |
| **Atomic (shared)** | `ATOMS`/`REDS`, `ATOMS.CAST.SPIN` (FP/wide emulation) | `AGU` | read+write | variable, scoreboard |
| **Surface atomic** | `SUATOM`/`SURED` `.sco{.sem}{.clamp}` | `SUATOM`/`TEX` | read(+write) | variable, scoreboard |
| **Warp reduction** | `REDUX.{SUM,MIN,MAX,AND,OR,XOR}` → uniform reg | datapath collective | — | fixed (no memory) |
| **Coupled ALU/FP** | `IADD3`,`LOP3`,`SHF`,`IMAD`,`LEA`,`PRMT`,`FADD`,`FMUL`,`FFMA`,`ISETP` | coupled math | **none** | band-6 (4/5 stall, 13 on CC/pred) |

The single structural rule: **the four scoreboard-driven units are precisely the ops
the coupled ALU is not** — every transcendental, every conversion, every double, and
every atomic is *decoupled* and resolved through a scoreboard, while the integer/FP
scalar core is *coupled* and resolved through a fixed stall. A code generator schedules
the first group by arming and waiting on scoreboards (`req_bit_set` / `dst_wr_sb` /
`src_rel_sb`) and the second by emitting the right `opex` stall count.
