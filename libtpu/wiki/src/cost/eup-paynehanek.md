# EUP Payne-Hanek Range Reduction

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. Section map: `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other libtpu builds will differ.*

## Abstract

The EUP `sinq`/`cosq` transcendentals cannot push a raw argument. A `sin`/`cos` push expects an argument already folded into a single quadrant — roughly `[−π/4, +π/4]` plus a 2-bit quadrant index — because the silicon polynomial that the push evaluates is only accurate over that narrow window. For `|x| ≫ 1` the naive fold `x − round(x/(π/2))·(π/2)` is catastrophic: the float representation of `π/2` is good to ~24 bits, so a large `x` has *no* surviving fraction bits after the subtraction, and the reduced argument is noise. The fix is the classic **Payne-Hanek** reduction: instead of subtracting a low-precision multiple of `π/2`, multiply `x` by a *very* high-precision fixed-point expansion of `1/(2π)`, keep only the fractional window of the product that the argument's binary exponent makes relevant, and reconstruct the reduced argument from that windowed fraction. This is the integer pre-pass that pairs with the EUP `sinq`/`cosq` push on the trig path; the push itself, its slot-3 encoding, and the deferred pop are the subject of [`slot-eup-transcendental`](../isa/slot-eup-transcendental.md).

The reduction lives in `PayneHanekRangeReduction` (`@0x1d5819c0`, in the `xla::jellyfish` anonymous namespace). It is *not* a `.rodata` constant-pool table — the `1/(2π)` expansion is materialized as six `mov esi, imm32` literals embedded directly in the emitter's `.text`, each fed to `LloModule::VectorU32Constant` (`@0x1d506400`), which lowers them into LLO `VectorU32Constant` ops in the generated kernel. The reduction is therefore *compiled into* the trig lowering, not loaded from a data segment. The function emits the integer windowed multiply, converts the integer turn-count to float (the quadrant index `k`), and reconstructs the fractional remainder `r` with one `VcomposeF32` of the float-`π/2` significand and a −30 binary-point exponent.

For reimplementation, the contract is:

- **The `1/(2π)` table.** Six contiguous 32-bit words (MSB-first), byte-exact in `.text`, reconstructing to `0.15915494309189535 = 1/(2π)` to fp64.
- **The windowed-multiply sequence.** Exponent → window index (`SubS32` against `0x20`), the `Shrl`/`Shll`/`Or` per-word window assembly, and the `VshllU64High` (`@0x1d583ac0`) + `VmulU64` 64-bit high-product extraction that keeps only the relevant fraction bits.
- **The quadrant index `k`.** The `VcvtF32ToS32`/`SimplifyConvertS32ToF32` (LLO unop `0x108`) of the integer turn-count and the 16 `SimplifySelect` arms that pick the per-exponent window; `k mod 4` selects the sign/swap.
- **The reconstruction.** `VcomposeF32(π/2 significand `0x00c90fdb`, exponent = −30 − window)` and the final `VmulF32` that forms `r = frac · (π/2)`.

| | |
|---|---|
| **Function** | `xla::jellyfish::(anon)::PayneHanekRangeReduction` `@0x1d5819c0` |
| **Constant loader** | `LloModule::VectorU32Constant` `@0x1d506400` (one LLO op per `imm32`) |
| **Table form** | 6 × `u32`, MSB-first, as `.text` immediates — **not** a `.rodata` pool |
| **Reconstructs to** | `Σ wᵢ·2^(−32(i+1)) = 0.15915494309189535 = 1/(2π)` (exact to fp64) |
| **High-product helper** | `VshllU64High` `@0x1d583ac0`; `VmulU64`/`VmulU32` on the wide-multiply path (`@lines 855–858`) |
| **Binary-point offset** | `0xffffffe2` (−30) → `SimplifySubS32` → `CreateVectorBinop(0x121)` |
| **π/2 significand** | `0x00c90fdb` (24-bit significand of float `π/2 = 0x3fc90fdb`) |
| **Reconstruction** | `VcomposeF32` `@0x1d555860`; final `VmulF32` `r = k_frac·(π/2)` |
| **Quadrant select** | 16 × `SimplifySelect` (window/quadrant fix-up); `k mod 4` sign/swap **INFERRED** |

---

## Why `1/(2π)`, Not `2/π`

### Purpose

The conventional name is "2/π reduction" because the goal is `r = x mod (π/2)` and `k = floor(x/(π/2))`, i.e. the argument measured in units of `π/2`. The table the binary actually uses is **`1/(2π)`** — the argument measured in *full turns* — and the quadrant index is recovered from the high bits of the fractional turn-count. A reimplementer who loads a `2/π` table will produce the wrong constant and a 4× scaling error.

### Evidence

The leading word `0x28be60db = 683565275`. Treating the six words as the fraction `0.w₀w₁w₂w₃w₄w₅` in base `2³²`:

```text
0x28be60db / 2³²  = 683565275 / 4294967296  = 0.15915494…
floor(2³² / (2π)) = floor(4294967296 / 6.283185…) = 683565275 = 0x28be60db   ✓
```

The full six-word reconstruction `Σᵢ wᵢ·2^(−32(i+1))` evaluates to `0.15915494309189535`, which is `1/(2π)` to the last fp64 bit. It is *not* `2/π = 0.6366197…` (whose leading word would be `0xa2f9836e`). The `π/2` multiply is restored at the *end* by `VcomposeF32` of the float-`π/2` significand: `x·(1/2π)` gives the turn-count whose fractional part `f ∈ [0,1)` is then scaled back by `2π·(π/2)`-equivalent reconstruction so that `r = f·(π/2)` after the quadrant fold. The net mapping is the standard `r = x − k·(π/2)`, reached through the turn-count representation.

> **NOTE —** the same `683565275 = 0x28be60db` leading word appears in the well-known fdlibm / glibc Payne-Hanek `two_over_pi[]` table as the *second* word (the fdlibm table begins the `2/π` expansion one digit earlier). libtpu's expansion is `1/(2π)` and self-contained at six words, sufficient for the fp32 argument range; it does not reuse the longer fdlibm `2/π` array.

---

## The `1/(2π)` Constant Table

### Layout

Six 32-bit words, loaded MSB-first (word 0 is the most-significant fraction limb). Each is an `imm32` operand of a distinct `VectorU32Constant` call; the `.text` offset is the address of the `mov esi, imm32` that materializes it (VMA == file offset). The decompiled order at lines 522–527 of `PayneHanekRangeReduction` is byte-identical to the disassembly.

| word | `u32` | `.text` off (imm) | bit range of `1/(2π)` |
|---|---|---|---|
| w0 | `0x28be60db` | `0x1d581b8a` | bits[191..160] (MSB limb) = `floor(2³²/(2π))` = 683565275 |
| w1 | `0x9391054a` | `0x1d581b9e` | bits[159..128] |
| w2 | `0x7f09d5f4` | `0x1d581bb3` | bits[127..96] |
| w3 | `0x7d4d3770` | `0x1d581bc8` | bits[95..64] |
| w4 | `0x36d8a566` | `0x1d581bdd` | bits[63..32] |
| w5 | `0x4f10e410` | `0x1d581bf2` | bits[31..0] (LSB limb) |

```text
1/(2π) ≈ 0.  28be60db 9391054a 7f09d5f4 7d4d3770 36d8a566 4f10e410   (base 2³², MSB→LSB)
        = Σ wᵢ · 2^(−32·(i+1))
        = 0.15915494309189535                                        (== 1/(2π), fp64-exact)
```

> **DISTINCTION —** these six words are **not** a `.rodata` table. A contiguous little-endian blob of the six words does **not** appear anywhere in `.rodata`; the search comes back empty. They exist *only* as instruction immediates in `PayneHanekRangeReduction`'s `.text`. Each `VectorU32Constant(immᵢ)` call emits a fresh LLO `VectorU32Constant` op into the kernel being compiled, so the table is replicated into every trig kernel rather than referenced from a shared constant pool. A reimplementer must hard-code the six immediates into the trig-lowering emitter, not into a data section.

### Reconstruction Constants

The same `VectorU32Constant` channel carries the scalar control constants for the windowed multiply and the final compose. All are `.text` immediates inside the same function; offsets are the materializing `mov`.

| `u32` | value | role | `.text` off |
|---|---|---|---|
| `0x00000020` | 32 | per-limb shift width; window index = `0x20 − exp_window` (`@line 528`) | `0x1d581c0a` |
| `0x0000001f` | 31 | low-5-bit exponent mask (`SimplifyAndU32`, `@line 509`) | — |
| `0x00000005` | 5 | `>>5` to split exponent into (limb-index, intra-limb shift) (`@line 495`) | — |
| `0x00000fff` | 4095 | 12-bit fraction window / round mask (`@line 911`) | — |
| `0x0000001e` | 30 | window-bit count for the fractional-product extraction (`@lines 1495,1508`) | — |
| `0xffffffe2` | −30 | binary-point exponent offset → `SubS32` → `CreateVectorBinop(0x121)` (`@line 1593`) | — |
| `0x20000000` | 2²⁹ | rounding / half-ULP bias for the fraction (`@line 1482`) | — |
| `0x00c90fdb` | — | 24-bit significand of float `π/2` (`π/2 = 0x3fc90fdb`) → `VcomposeF32` (`@line 1606`) | — |

The exponent-window split is the heart of the trick: the low 5 bits of the (biased) exponent become an intra-limb shift, and `exp >> 5` selects which 32-bit limb of the product window straddles the binary point. `0x20 − shift` is the complementary shift for the `Shll`/`Shrl`/`Or` limb-join, so the chosen 64-bit window of the `x · 1/(2π)` product is assembled regardless of how large `|x|` is — the fraction bits are never lost to cancellation.

---

## Algorithm

### The Windowed Multiply

The product `x · (1/2π)` is a fixed-point integer-times-fraction. The reduction never forms the full 192-bit product; it forms only the 64-bit window that brackets the binary point, selected by `x`'s exponent. The decompiled control flow at `@0x1d5819c0` is:

```c
// PayneHanekRangeReduction(builder, x, ...)   @0x1d5819c0
exp      = SimplifyAddS32(x_exponent, 1)                 // VectorU32Constant(1) seed
exp      = SimplifyMaxS32(exp, 0)                        // clamp negative exponents to 0
limb_idx = SimplifyShrlU32(exp, 5)                       // exp >> 5  → which u32 limb
shift    = SimplifyAndU32(exp, 0x1f)                     // exp & 31  → intra-limb shift
cshift   = SimplifySubS32(0x20, shift)                   // 32 - shift  (CreateVectorBinop 0x121)

// per-limb window assembly: for each of the six 1/(2π) words wᵢ
//   hi = SimplifyShrlU32(wᵢ, cshift)      (CreateVectorBinop 0x19a = SHRL)
//   lo = SimplifyShllU32(wᵢ, shift)       (CreateVectorBinop 0x19c = SHLL)
//   window_limb = SimplifyOrU32(hi, lo)   (CreateVectorBinop 0x15e = OR)
// → six assembled limbs (w0..w5) realigned to the binary point

// wide-multiply path (target supports VmulU64, @line 838 capability query):
prod_lo = VmulU64(builder, x_mantissa, window_lo)        // @line 855 : low product
prod_hi = VmulU64(builder, x_mantissa, window_hi)        // @line 856
prod_x  = VmulU32(builder, x_mantissa, window_mid)       // @line 858, + AddCarryU32 @line 872
// limb-by-limb path otherwise: VshllU64High (@0x1d583ac0, lines 982/1052/1592)
//   feeds the 8-iteration SimplifyMulF32/VcvtF32ToS32 accumulation loop (@lines 1194-1337)
```

`VshllU64High` (`@0x1d583ac0`) is the unsigned 64×64→high-64 multiply that keeps the high product bits; the integer part of the turn-count is the quadrant carrier and the fractional part becomes the reduced argument. The `Shrl`/`Shll`/`Or` chain (LLO binops `0x19a`/`0x19c`/`0x15e`) appears once per limb to realign each `1/(2π)` limb against the exponent-selected binary point before the multiply.

> **NOTE —** the function carries two product paths gated by a target capability query (`@line 838`, the dispatch `(*vtbl+1568)(...,8)`). When the target supports the wide multiply, the windowed product is formed by two `VmulU64` calls plus a `VmulU32` (`@lines 855–858`) with explicit `AddCarryU32` carry propagation (`@line 872`). Otherwise the fraction is assembled limb-by-limb through `VshllU64High` (`@lines 982, 1052, 1592`) and the eight-iteration `SimplifyMulF32`/`VcvtF32ToS32` accumulation loop (`@lines 1194–1337`) that multiplies the float-converted mantissa nibbles against the window limbs. Both paths produce the same windowed `x · (1/2π)` product; the high/low integer-vs-fraction split of that product is **INFERRED** from the surrounding reconstruction, not an opcode flag.

### Quadrant Index and Reconstruction

After the windowed multiply, the integer turn-count is converted to float (the value the EUP push will use to choose the quadrant), and the fractional remainder is reassembled as a float with the `π/2` significand:

```c
// integer turn-count → float (the quadrant carrier)
k_int   = VcvtF32ToS32(...)                              // @line 1213 (round mode 0xffffffff)
k_float = SimplifyConvertS32ToF32(turns)                 // CreateVectorUnop 0x108

// reduced fraction → float with π/2 significand and a −30 binary point
bp_exp  = SimplifySubS32(0xffffffe2, window)             // -30 - window  (CreateVectorBinop 0x121)
pio2    = VectorU32Constant(0x00c90fdb)                  // 24-bit significand of π/2
r_compose = VcomposeF32(builder, bp_exp, pio2, 0,0,0)    // @0x1d555860 : assemble float r·(π/2)
frac_f    = SimplifyConvertS32ToF32(frac_int)            // CreateVectorUnop 0x108
r         = SimplifyMulF32(frac_f, r_compose)            // r = frac · (π/2)   ← reduced argument
```

`VcomposeF32` (`@0x1d555860`) builds a float from an integer exponent (`−30 − window`, accounting for the 30-bit fractional window and the limb position) and the fixed `π/2` significand `0x00c90fdb`. The final `VmulF32(frac, π/2-composed)` yields the reduced argument `r ∈ [−π/4, +π/4]` (post quadrant fold) that the EUP `sinq`/`cosq` push consumes. The function's return value (`@line 1615-1621`) is this `r`; the quadrant index `k` travels alongside through the 16-arm select network.

### The Quadrant Select Network

Sixteen `SimplifySelect` calls implement the per-exponent window/quadrant fix-up. Early in the function (lines 742–749) the window index is compared against `1`, `2`, `3`, `4` with `VcmpHelper` (comparison kind 4, predicate 5 = "<="), producing four masks; the `SimplifySelect` arms (lines 751, 759, 766, 773, …) chain these masks to pick which assembled limbs feed the high/low product — i.e. which 32-bit windows of the `1/(2π)` expansion straddle the binary point for this exponent bucket. The low two bits of the resulting integer turn-count are the quadrant index `k`; `k mod 4` selects the trig identity:

```text
k mod 4   sin(x) =        cos(x) =
  0       +sin(r)         +cos(r)
  1       +cos(r)         −sin(r)
  2       −sin(r)         −cos(r)
  3       −cos(r)         +sin(r)
```

> **INFERRED —** the `k mod 4 → {sign, sin↔cos swap}` truth table above is the standard quadrant mapping and is the *expected* meaning of the two low turn-count bits, but it was **not** byte-transcribed from the 16 `SimplifySelect` arms. The select network was decoded structurally (window selection by exponent bucket), and the integer-to-quadrant binding pairs with the EUP `sinq`/`cosq` push rather than living entirely inside this function. Treat the truth table as a documented convention, not a binary-confirmed fact; the sign/swap arms are most plausibly applied at the `Vsinq`/`Vcosq` builder that wraps the EUP push (see [`slot-eup-transcendental`](../isa/slot-eup-transcendental.md)). **LOW** confidence on the exact arm-to-quadrant assignment.

### Worked Example — Why the Window Survives for Large `|x|`

Take `x = 1e20` (binary exponent ~66). A naive fold computes `x − round(x · 2/π)·(π/2)` in fp32; but `x` has zero fraction bits at exponent 66, and `π/2` is good to 24 bits, so the subtraction is `(garbage) − (garbage)` and the result has no correct bits. Payne-Hanek instead computes the product `x · (1/2π)` to 192-bit fixed-point precision and keeps the window around the binary point:

```text
exponent of x ≈ 66           → limb_idx = 66 >> 5 = 2,  shift = 66 & 31 = 2
                               → the binary point of x·(1/2π) lands inside limb w2 (bits 127..96)
window  = realign(w1, w2, w3) by (shift=2, cshift=30) via Shll/Shrl/Or
                               → a 64-bit slice of the 1/(2π) expansion straddling the point
prod_hi = VshllU64High(mantissa(x), window)   → integer-turn bits  → k (quadrant)
prod_lo = VmulU64(mantissa(x), window)         → fractional-turn bits → reduced fraction f
r       = VcomposeF32(exp = −30 − window, sig = 0x00c90fdb) · convert(f)
```

The fraction `f ∈ [0,1)` carries the full 24 surviving significand bits *regardless* of how large `|x|` is, because the relevant `1/(2π)` limbs are selected by `x`'s exponent rather than truncated by it. This is the entire reason the table is 192 bits wide: it must supply enough fraction bits below the binary point that the windowed product still has 24 good bits after the largest fp32 exponent slides the point all the way to limb `w5`.

---

## Data Structures and LLO Opcodes

The reduction is built entirely from generic `LloRegionBuilder` simplifiers and `CreateVectorBinop`/`CreateVectorUnop` ops — there is no dedicated "PayneHanek" LLO opcode. The opcodes touched, all confirmed in the decompile:

| LLO op | helper | meaning |
|---|---|---|
| `0x108` | `CreateVectorUnop` / `SimplifyConvertS32ToF32` | `s32 → f32` (turn-count and fraction to float) |
| `0x11b` | `CreateVectorBinop` / `SimplifyAddS32` | `+` (exponent seed, `@line 473`) |
| `0x121` | `CreateVectorBinop` / `SimplifySubS32` | `−` (window index and binary-point offset) |
| `0x15c` | `CreateVectorBinop` / `SimplifyAndU32` | bitwise AND (exponent mask `0x1f`) |
| `0x15e` | `CreateVectorBinop` / `SimplifyOrU32` | bitwise OR (limb window join) |
| `0x19a` | `CreateVectorBinop` / `SimplifyShrlU32` | logical shift right (limb realign) |
| `0x19c` | `CreateVectorBinop` / `SimplifyShllU32` | logical shift left (limb realign) |

The argument enters as an `LloValue*` (the float `x`); its mantissa and exponent are decomposed by the `CastTo(0x12, …)` (`@line 470`) and the exponent arithmetic above. No state is held across calls — the function is a pure SSA expander emitting a fixed DAG of these ops into the current `LloRegion`.

### Reimplementation Note: Immediate Stream vs Constant Pool

The single most important structural fact for a reimplementer is *where* the table lives. A from-scratch LLVM-based trig lowering would naturally place a 192-bit constant in a `.rodata` array and reference it with a `GlobalAddress`. libtpu does **not**: each of the six words is a `mov esi, imm32` immediate consumed by `VectorU32Constant`, which constructs a fresh per-kernel LLO constant op. The consequences are concrete:

- **No relocation, no constant-pool entry.** A `.rodata` scan for the six-word blob returns empty — the words are only in the emitter's `.text`. Searching a captured kernel's data segment for `1/(2π)` will also fail; the constants are LLO ops, folded by the simplifier (`SimplifyShrlU32` etc. constant-fold at emit time when the exponent is statically known).
- **Replicated per trig kernel.** Because the constants are emitted rather than referenced, every `sin`/`cos` lowering that hits the large-argument path carries its own copy of the six words. There is no shared `two_over_pi` symbol to deduplicate against.
- **The control constants are equally inlined.** `0x20`, `0x1f`, `0x05`, `0xfff`, `0x1e`, `0xffffffe2`, `0x20000000`, `0x00c90fdb` are all `mov`-immediate operands in the same `.text` window, not loaded from memory.

A faithful reimplementation must therefore hard-code the eight constants into the trig-lowering pass itself and emit them as IR constants, matching libtpu's "compiled-in, not loaded" model.

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `PayneHanekRangeReduction` | `0x1d5819c0` | the full trig argument reduction (emits the windowed multiply + reconstruction) |
| `LloModule::VectorU32Constant` | `0x1d506400` | materializes each `imm32` (the six `1/(2π)` words + control constants) as an LLO op |
| `VshllU64High` | `0x1d583ac0` | unsigned 64×64 → high-64 product (limb-by-limb windowed multiply) |
| `LloRegionBuilder::VcomposeF32` | `0x1d555860` | assembles the reduced argument float from (exponent, π/2 significand) |
| `SimplifyConvertS32ToF32` | (inline) | `s32 → f32` for turn-count `k` and the fraction |
| `LloRegionBuilder::VcmpHelper` | (inline) | the four window comparisons (`<= 1,2,3,4`) feeding the selects |
| `VsinReduced` / `Vsinq` / `VcosqDecomposed` | (trig builders) | wrap the EUP `sinq`/`cosq` push around `r`; apply the `k`-quadrant sign/swap |

### Considerations

The reduction is invoked from the `sin`/`cos` trig lowering (`Vtrigfun` / the `TrigFlavor` builder), which decides whether the argument magnitude warrants the full Payne-Hanek path or a cheaper in-range fold; that gating threshold was not pinned here. The six-word `1/(2π)` expansion is sufficient for the full fp32 exponent range (`|x| < 2¹²⁸`) — the 192-bit fraction covers more than the 24-bit fp32 significand plus the 8-bit exponent slide. For an fp64 trig path the table would need to widen, but libtpu's EUP trig is fp32/bf16, so six words is the production width. The cost-model consequence is documented in [`eup-latency-overview`](eup-latency-overview.md): the reduction's `~50` LLO ops execute on the VALU *before* the `sinq`/`cosq` push, so the Payne-Hanek DAG is part of the VALU-correction window that hides the EUP push→pop latency rather than being a separate pipeline stage.

---

## Cross-References

- [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) — the `sinq`/`cosq` push encoding (VALU slot 3, function selectors `0x17`/`0x18` F32, `0x1e`/`0x1f` BF16), the deferred pop, and where the quadrant sign/swap is applied
- [EUP Latency Overview](eup-latency-overview.md) — how the Payne-Hanek VALU DAG fills the push→pop software-pipeline window
- [EUP Correction Coefficients](eup-correction-coeffs.md) — the sibling correction-polynomial catalog (tanh rational, Newton refinements, `*NoEupF32` fallbacks)
- [EUP Per-Gen Latency Integers](eup-per-gen-integers.md) — the per-gen push→pop latency edge (PF 7 / VF 6 / GL 13·F32 / 14·BF16) the trig path must hide
- [back to index](../index.md) — Part VII — Cost & Latency Model / EUP / transcendental latency
