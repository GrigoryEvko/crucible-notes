# sass-bitsem — typed, machine-checked bit semantics

A Lean 4 formalization of SASS instruction semantics as pure `BitVec` functions:
input bits to output bits, with the widths carried in the type. Each identity is
proved by `bv_decide`, which bit-blasts the goal to SAT and checks an LRAT
certificate, so the theorem holds for **all** inputs with a kernel-checked proof
term — not a sampled test.

This is the typed companion to the executable models under `../sass-tools/`
(`sass_sem_int.py`, `sass_sem_fp.py`, `sass_warp.py`). The Python models run
against the live GPU for differential validation; the Lean definitions mirror
them and carry the proofs. `bv_decide` and an external QF_BV solver discharge the
same obligation — equivalence of two bit-vector functions over every input — so a
Lean theorem here and a solver query there are interchangeable evidence, the Lean
side additionally giving dependent-width types and a proof object.

## Layout

- `SassBitsem/Logic.lean` — 3-input LUT (`lop3`), funnel shift (`funnelR`/`funnelL`),
  contiguous mask (`bmskWrap`), with the canonical-selector and split identities.
- `SassBitsem/Convert.lean` — width-typed casts (`zext16`/`sext16`/`trunc16`) and
  the bf16 ↔ f32 pair, where a bf16 word is exactly the high 16 bits of the f32
  word and both directions are exact bit surgery.

## Build

```
lake build
```

Requires only the pinned toolchain (`lean-toolchain`); no Mathlib or Batteries.

## Scope

Covered: the operations that are exact bit transformations — logic, shifts,
permutes, integer width casts, and the bf16/f32 pair. The conversions and
arithmetic that need rounding or subnormal normalization (f16/fp8/fp4, float↔int,
add/mul/fma) belong to a shared rounding core and are formalized in the
arithmetic layer, where the round-and-pack step is defined once and reused.
