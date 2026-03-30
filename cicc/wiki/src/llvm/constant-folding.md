# Constant Folding: Math & Intrinsics

> **NVIDIA-modified pass.** GPU-specific changes (110+ math name variants, 60+ NVVM intrinsic IDs, exception-safe host evaluation) are documented throughout this page.
>
> **Upstream source:** `llvm/lib/Analysis/ConstantFolding.cpp` (LLVM 20.0.0). The upstream `ConstantFoldCall` function handles standard `llvm.*` intrinsics; NVIDIA's extensions (`sub_14D90D0` eligibility checker, `sub_14D1BC0` evaluator) are layered on top.
>
> **LLVM version note:** The upstream `ConstantFolding.cpp` in LLVM 20 handles approximately 30 standard math intrinsics (`llvm.sin`, `llvm.cos`, `llvm.sqrt`, etc.) and a small set of NVPTX-specific intrinsics (ceil, floor, fabs, sqrt in `nvvm.*` form). CICC extends this to 110+ math name variants (C, glibc `__*_finite`, C++ mangled `_Z*`) and 60+ NVVM intrinsic IDs. The upstream `disable-fp-call-folding` knob (`cl::Hidden`, default `false`) is preserved; NVIDIA adds a separate `FPFoldDisable` CiccOption for independent control.

CICC v13.0 extends LLVM's `ConstantFolding` analysis with two large custom functions that together enable compile-time evaluation of over 110 distinct math function name variants and 60+ NVVM intrinsic IDs. Upstream LLVM's `ConstantFoldCall` handles standard `llvm.sin`, `llvm.cos`, `llvm.sqrt`, and a handful of NVPTX-specific intrinsics (ceil, floor, fabs, sqrt in their `nvvm.*` forms, plus FP-to-integer conversion intrinsics). CICC goes far beyond this: it recognizes every C math library name (`sin`, `sinf`), every glibc `__*_finite` internal variant, every C++ mangled form (`_Z3cosf`, `_Z4acosd`), and the full set of NVVM approximate/FTZ math intrinsics -- then evaluates them using the host C math library with an exception-safe wrapper that refuses to produce results when the host FPU signals domain errors, overflow, or underflow.

The system is split into two cooperating functions. The eligibility checker `sub_14D90D0` (27 KB, called `nvvmIntrinsicConstantFold` in the sweep analysis) is a fast predicate that answers "can this call be constant-folded?" without touching operand values. The evaluator `sub_14D1BC0` (54 KB, called `nvvmConstantFoldLibCall`) performs the actual computation when all operands are constant. A third function, the NVVM InstCombine intrinsic folder `sub_1169C30` (87 KB), handles algebraic simplification of NVVM intrinsics and is documented separately on the [InstCombine](instcombine.md) page.

| | |
|---|---|
| **Eligibility checker** | `sub_14D90D0` (`0x14D90D0`, 27 KB, 282 basic blocks, 489 edges) |
| **Math evaluator** | `sub_14D1BC0` (`0x14D1BC0`, 54 KB) |
| **Constant extractor** | `sub_14D1620` (`0x14D1620`) |
| **Safe unary eval wrapper** | `sub_14D19F0` (`0x14D19F0`) |
| **Safe binary eval wrapper** | `sub_14D1A80` (`0x14D1A80`) |
| **ConstantFP builder** | `sub_14D17B0` (`0x14D17B0`) |
| **Custom fabs** | `sub_14D1280` (`0x14D1280`) -- SSE2 sign-bit mask |
| **Custom floor** | `sub_14D13B0` (`0x14D13B0`) -- truncation + sign correction |
| **Custom ceil** | `sub_14D1410` (`0x14D1410`) -- truncation + sign correction |
| **Custom sqrt** | `sub_14D1470` (`0x14D1470`) -- thin wrapper around libc `sqrt` |
| **Vector math mapping** | `sub_149E420` (`0x149E420`, 26 KB) |
| **LLVM knob** | `disable-fp-call-folding` (upstream, `cl::Hidden`, default `false`) |
| **NVIDIA knob** | `FPFoldDisable` (NVIDIA CiccOption, disables FP constant folding) |

## Two-Tier Architecture: Eligibility vs. Evaluation

The constant folding system operates as a two-phase protocol. The caller (from the ConstantFolding pass or InstCombine `visitCallInst` path) first invokes the eligibility checker to determine whether a call instruction is a candidate, then invokes the evaluator to produce the folded constant. This split exists for performance: the eligibility check is cheap (no operand extraction, no FP computation), while the evaluator is expensive (extracts APFloat values, calls host math library, checks FP exceptions).

### Eligibility Checker: `sub_14D90D0`

The function takes a tagged IR node pointer and a context (intrinsic descriptor). The node pointer carries a 3-bit tag in its low bits; the function masks with `~7` to recover the aligned base. Before examining intrinsic IDs, it performs three attribute pre-filter checks on the callee:

1. **Speculatable/ReadNone** (attribute kind `0x15` = 21): The callee must be safe to speculatively execute. If the direct callee lacks this attribute, the function follows one level of indirection through the resolved function target at `[callee + 0x70]` and re-checks.

2. **NoUnwind** (attribute kind `5`): The callee must not throw. Same indirection chain.

3. **Convergent gate** (attribute kind `0x34` = 52): If the callee is marked `convergent`, the function returns 0 immediately. This is the critical safety check for GPU code -- convergent intrinsics like `__syncthreads()`, `__ballot_sync()`, and warp shuffle operations have warp-synchronous semantics that would be violated by folding them away, even when all arguments happen to be constant.

After attribute filtering, the function reads the intrinsic ID from `[context + 0x24]` (offset +36, unsigned 32-bit enum) and dispatches through a two-level scheme.

### Evaluation: `sub_14D1BC0`

The evaluator receives the function name string, its length, an opcode/intrinsic-ID enum, a return type descriptor, an array of constant operand IR nodes, the operand count (1, 2, or 3), a flag enabling name-based matching, and a context pointer. It returns a `ConstantFP` or `ConstantInt` IR node on success, or null on failure.

The top-level dispatch is on operand count:

- **Unary** (count = 1): Trigonometric, exponential, logarithmic, rounding, and absolute value functions.
- **Binary** (count = 2): `pow`, `fmod`, `atan2`, `copysign`, `fmin`, `fmax`.
- **Ternary** (count = 3): FMA / fused multiply-add (opcodes 99 and 100 only).

## Foldable Intrinsics Master Table

### Standard LLVM Intrinsic IDs (0--211)

These are dispatched via a jump table at `jpt_14D91F0` in the eligibility checker. The evaluator handles them via cascading opcode comparisons.

| ID | Hex | Intrinsic | Category |
|----|------|-----------|----------|
| 5 | `0x05` | `llvm.bswap` | Bitwise |
| 6 | `0x06` | `llvm.ceil` | Rounding |
| 8 | `0x08` | `llvm.copysign` | Sign |
| 11 | `0x0B` | `llvm.cos` | Trig |
| 12 | `0x0C` | `llvm.ctlz` | Bitwise |
| 13 | `0x0D` | `llvm.ctpop` | Bitwise |
| 30 | `0x1E` | `llvm.exp` | Exponential |
| 31 | `0x1F` | `llvm.exp2` | Exponential |
| 32 | `0x20` | `llvm.fabs` | Absolute |
| 33 | `0x21` | `llvm.floor` | Rounding |
| 54 | `0x36` | `llvm.fma` | Ternary |
| 55 | `0x37` | `llvm.fmuladd` | Ternary |
| 96 | `0x60` | `llvm.log` | Logarithmic |
| 97 | `0x61` | `llvm.log10` | Logarithmic |
| 99 | `0x63` | `llvm.log2` | Logarithmic |
| 100 | `0x64` | `llvm.lround` | Rounding |
| 115 | `0x73` | `llvm.maxnum` | MinMax |
| 122 | `0x7A` | `llvm.minnum` | MinMax |
| 123 | `0x7B` | `llvm.nearbyint` | Rounding |
| 124 | `0x7C` | `llvm.pow` | Power |
| 129 | `0x81` | `llvm.powi` | Power |
| 132 | `0x84` | `llvm.rint` | Rounding |
| 139 | `0x8B` | `llvm.round` | Rounding |
| 140 | `0x8C` | `llvm.roundeven` | Rounding |
| 146 | `0x92` | `llvm.sin` | Trig |
| 147 | `0x93` | `llvm.tan` | Trig |
| 187 | `0xBB` | `llvm.sqrt` | Root |
| 188 | `0xBC` | `llvm.trunc` | Rounding |
| 189--211 | `0xBD`--`0xD3` | Integer ops (umax, sadd.with.overflow, etc.) | Integer |

### NVVM-Specific Intrinsic IDs (>211)

These are dispatched via cascading range checks with bitmask tests in the eligibility checker.

| ID Range | Hex | Intrinsic | Category |
|----------|-----|-----------|----------|
| 3637--3639 | `0xE35`--`0xE37` | `nvvm.bitcast.*` / `nvvm.move.*` | Bitwise |
| 3660 | `0xE4C` | `nvvm.ptr.gen.to.*` | Pointer |
| 3764--3765 | `0xEB4`--`0xEB5` | `nvvm.ceil.f` / `nvvm.ceil.d` | Rounding |
| 3778--3779 | `0xEC2`--`0xEC3` | `nvvm.ctlz.i` / `nvvm.ctlz.ll` | Bitwise |
| 3787 | `0xECB` | `nvvm.cos.approx.ftz.f` | Trig |
| 3811 | `0xEE3` | `nvvm.div.*` / `nvvm.fabs` variant | Arith |
| 3870--3871 | `0xF1E`--`0xF1F` | `nvvm.exp2.approx.ftz.f` / `.d` | Exponential |
| 3911--3912 | `0xF47`--`0xF48` | `nvvm.fabs.f` / `.d` | Absolute |
| 3924--3925 | `0xF54`--`0xF55` | `nvvm.floor.f` / `.d` | Rounding |
| 3944 | `0xF68` | `nvvm.log.approx.ftz.f` | Logarithmic |
| 3946 | `0xF6A` | `nvvm.log2.approx.ftz.f` | Logarithmic |
| 3948 | `0xF6C` | `nvvm.log10.approx.ftz.f` | Logarithmic |
| 3950 | `0xF6E` | `nvvm.rcp.approx.ftz.d` | Reciprocal |
| 3952 | `0xF70` | `nvvm.rsqrt.approx.ftz.f` | Root |
| 3954 | `0xF72` | `nvvm.sqrt.f` / `.approx.ftz.f` | Root |
| 4072--4074 | `0xFE8`--`0xFEA` | `nvvm.sin`/`cos.approx.ftz` variants | Trig |
| 4114--4115 | `0x1012`--`0x1013` | `nvvm.max.i` / `.ui` | MinMax |
| 4118--4119 | `0x1016`--`0x1017` | `nvvm.min.i` / `.ui` | MinMax |
| 4167--4168 | `0x1047`--`0x1048` | `nvvm.max.ll` / `.ull` | MinMax |
| 4170--4172 | `0x104A`--`0x104C` | `nvvm.min.ll` / `.ull` | MinMax |
| 4230--4231 | `0x1086`--`0x1087` | `nvvm.mul.hi.*` | Multiply |
| 4413 | `0x113D` | `nvvm.sin.approx.ftz.f` | Trig |
| 4475, 4478 | `0x117B`, `0x117E` | `nvvm.sqrt.f` / `.rn.d` | Root |
| 4483--4484 | `0x1183`--`0x1184` | `nvvm.sqrt.approx.f` / `.ftz.f` | Root |
| 5293 | `0x14AD` | `nvvm.f2i` / `nvvm.d2i` | Conversion |
| 5300 | `0x14B4` | `nvvm.i2f` / `nvvm.i2d` | Conversion |
| 7297--7298 | `0x1C81`--`0x1C82` | `nvvm.fmax.f` / `.d` | MinMax |
| 7301--7302 | `0x1C85`--`0x1C86` | `nvvm.fmin.f` / `.d` | MinMax |
| 7334--7335 | `0x1CA6`--`0x1CA7` | `nvvm.fmax.ftz.f` / `.ftz.nan.f` | MinMax |
| 7339--7340 | `0x1CAB`--`0x1CAC` | `nvvm.fmin.ftz.f` / `.ftz.nan.f` | MinMax |

### Name-Based Foldable Functions (Case 0 Fallthrough)

When the intrinsic ID is 0 (unrecognized LLVM intrinsic), both the eligibility checker and the evaluator fall through to string-based matching. The evaluator uses a two-tier name matching system: fast-path intrinsic ID dispatch, then slow-path name comparison when the `a7` flag is set.

**Plain C library names (44 entries):**

| Category | Functions |
|----------|-----------|
| Trigonometric | `sin`, `sinf`, `cos`, `cosf`, `tan`, `tanf` |
| Inverse trig | `acos`, `acosf`, `asin`, `asinf`, `atan`, `atanf`, `atan2`, `atan2f` |
| Hyperbolic | `sinh`, `sinhf`, `cosh`, `coshf`, `tanh`, `tanhf` |
| Exponential | `exp`, `expf`, `exp2`, `exp2f` |
| Logarithmic | `log`, `logf`, `log10`, `log10f` |
| Rounding | `ceil`, `ceilf`, `floor`, `floorf`, `round`, `roundf` |
| Absolute / Root | `fabs`, `fabsf`, `sqrt`, `sqrtf` |
| Binary | `pow`, `powf`, `fmod`, `fmodf`, `atan2`, `atan2f` |

**Glibc `__*_finite` variants (20 entries):**

`__acos_finite`, `__acosf_finite`, `__asin_finite`, `__asinf_finite`, `__atan2_finite`, `__atan2f_finite`, `__cosh_finite`, `__coshf_finite`, `__exp_finite`, `__expf_finite`, `__exp2_finite`, `__exp2f_finite`, `__log_finite`, `__logf_finite`, `__log10_finite`, `__log10f_finite`, `__pow_finite`, `__powf_finite`, `__sinh_finite`, `__sinhf_finite`

**C++ mangled names (~48 entries):** `_Z3cosf`, `_Z3cosd`, `_Z3sinf`, `_Z3sind`, `_Z3tanf`, `_Z3tand`, `_Z3expf`, `_Z3expd`, `_Z3logf`, `_Z3logd`, `_Z4acosf`, `_Z4acosd`, `_Z4asinf`, `_Z4asind`, `_Z4atanf`, `_Z4atand`, `_Z4ceilf`, `_Z4ceild`, `_Z4coshf`, `_Z4coshd`, `_Z4exp2f`, `_Z4exp2d`, `_Z4fabsf`, `_Z4fabsd`, `_Z4sinhf`, `_Z4sinhd`, `_Z4sqrtf`, `_Z4sqrtd`, `_Z4tanhf`, `_Z4tanhd`, `_Z4fmodff`, `_Z4fmoddd`, `_Z5floorf`, `_Z5floord`, `_Z5log10f`, `_Z5log10d`, `_Z5atan2ff`, `_Z5atan2dd`, `_Z5powff`, `_Z5powdd`, `_Z5roundf`, `_Z5roundd`

Total across all three name forms: approximately 112 distinct recognized strings.

## Name Matching Algorithm

The evaluator's name matching is a hand-tuned trie-like dispatch optimized for the specific set of math function names. It avoids hash tables or sorted arrays in favor of cascading character comparisons:

```
nameMatch(name, length):
    // Strip C++ mangling prefix
    if name[0] == '_' and name[1] == 'Z':
        dispatch on name[2]:  // length digit
            '3' -> match 3-char base: cos, sin, tan, exp, log
            '4' -> match 4-char base: acos, asin, atan, ceil, cosh, exp2, fabs, sinh, sqrt, tanh, fmod
            '5' -> match 5-char base: floor, log10, atan2, pow, round
        verify trailing type suffix: 'f' = float, 'd' = double
        return FOUND

    // Strip glibc __finite prefix
    if name[0] == '_' and name[1] == '_':
        dispatch on name[2]:
            'a' -> __acos_finite, __acosf_finite, __asin_finite, __asinf_finite,
                   __atan2_finite, __atan2f_finite
            'c' -> __cosh_finite, __coshf_finite
            'e' -> __exp_finite, __expf_finite, __exp2_finite, __exp2f_finite
            'l' -> __log_finite, __logf_finite, __log10_finite, __log10f_finite
            'p' -> __pow_finite, __powf_finite
            's' -> __sinh_finite, __sinhf_finite
        verify with memcmp against string constant
        return FOUND

    // Plain C library name
    dispatch on name[0]:
        'a' -> acos, asin, atan + 'f' variants
        'c' -> cos, cosf, ceil, ceilf, cosh, coshf
        'e' -> exp, expf, exp2, exp2f
        'f' -> fabs, fabsf, floor, floorf
        'l' -> log, logf, log10, log10f
        'p' -> pow, powf
        'r' -> round, roundf
        's' -> sin, sinf, sinh, sinhf, sqrt, sqrtf
        't' -> tan, tanf, tanh, tanhf

    // Within each group, dispatch on name length:
    length 3: direct 3-byte compare ("sin", "cos", "tan", "exp", "log", "pow")
    length 4: DWORD compare (4-byte integer, little-endian):
        0x736F6361 = "acos"    0x6E697361 = "asin"
        0x6E617461 = "atan"    0x6C696563 = "ceil"
        0x68736F63 = "cosh"    0x73626166 = "fabs"
        0x66736F63 = "cosf"    0x686E6973 = "sinh"
        0x74727173 = "sqrt"    0x686E6174 = "tanh"
        0x32707865 = "exp2"    0x66707865 = "expf"
        ...
    length 5+: memcmp against literal string constant
    return FOUND or NOT_FOUND
```

The 4-byte integer comparison trick deserves attention: instead of calling `memcmp` for 4-character names, the code loads the name as a `uint32_t` and compares against a pre-computed little-endian constant. For example, `*(uint32_t*)name == 0x736F6361` checks for "acos" (`'a'=0x61, 'c'=0x63, 'o'=0x6F, 's'=0x73`). This micro-optimization eliminates function call overhead for the most common name lengths.

## Exception-Safe Host Evaluation

The core safety mechanism is the FP exception wrapper used for all transcendental evaluation. Both the unary wrapper (`sub_14D19F0`) and binary wrapper (`sub_14D1A80`) follow the same protocol:

```c
Value* safeMathEval(double (*mathFunc)(double), Type* resultType, double arg) {
    feclearexcept(FE_ALL_EXCEPT);        // clear all FP exception flags
    *__errno_location() = 0;             // clear errno

    double result = mathFunc(arg);       // call host C library

    // Check errno for domain/range error
    int e = *__errno_location();
    if (e == EDOM || e == ERANGE) {      // errno 33 or 34
        feclearexcept(FE_ALL_EXCEPT);
        *__errno_location() = 0;
        return nullptr;                  // refuse to fold
    }

    // Check FP exception flags (mask = 0x1D = 29)
    // FE_INVALID(1) | FE_DIVBYZERO(4) | FE_OVERFLOW(8) | FE_UNDERFLOW(16)
    if (fetestexcept(FE_INVALID | FE_DIVBYZERO | FE_OVERFLOW | FE_UNDERFLOW)) {
        feclearexcept(FE_ALL_EXCEPT);
        *__errno_location() = 0;
        return nullptr;                  // refuse to fold
    }

    // FE_INEXACT (32) is intentionally NOT checked --
    // most transcendentals produce inexact results and that is acceptable.

    return createConstantFP(resultType, result);
}
```

This design means the folder **refuses to produce a result** whenever the host FPU signals any exceptional condition other than inexact. The implications:

- `sin(1e308)` might overflow on the host -- not folded, left in IR for runtime evaluation.
- `log(-1.0)` produces a domain error -- not folded.
- `sqrt(-0.01)` triggers `FE_INVALID` -- not folded.
- `sin(0.5)` produces an inexact result (since sin(0.5) is irrational) -- folded normally.

### Domain Pre-Checks

In addition to the post-evaluation exception check, certain functions have explicit domain guards before calling the host math library:

| Function | Precondition | Rationale |
|----------|-------------|-----------|
| `log`, `logf`, `log10`, `log10f` | argument > 0.0 | Negative inputs produce NaN |
| `sqrt`, `sqrtf` | argument >= 0.0 | Negative inputs produce NaN |
| `acos`, `asin` | *no pre-check* | Relies on FP exception mechanism |

The asymmetry is deliberate: `log`/`sqrt` get explicit checks because their domain violations are common and cheap to detect, while `acos`/`asin` rely on the post-evaluation `FE_INVALID` check.

## Host FPU vs. GPU Precision

The constant folder evaluates using the **host CPU's math library** (`j_sin`, `j_cos`, `j_exp`, etc. -- PLT stubs to glibc). This creates a potential precision mismatch: the folded constant may not be bit-identical to what the GPU hardware would compute. NVIDIA mitigates this through several mechanisms:

1. **Custom implementations for exact functions.** `fabs`, `floor`, `ceil`, and `round` have custom host-side implementations that match GPU rounding semantics exactly:

   - **fabs** (`sub_14D1280`): Pure SSE2 bitwise AND with `0x7FFFFFFFFFFFFFFF` (clear sign bit). Bit-exact regardless of platform.
   - **floor** (`sub_14D13B0`): Custom truncation: for `|x| < 2^52`, truncate to integer, subtract 1.0 if truncation rounded toward zero for negative values, preserve sign bit. For `|x| >= 2^52`, return unchanged (already integral).
   - **ceil** (`sub_14D1410`): Mirror of floor: truncate to integer, add 1.0 if truncation rounded toward zero for positive values.
   - **round** (`j__round`): Uses libc `round()` directly (round-half-away-from-zero, matching PTX `round.rni`).

2. **Exception rejection for transcendentals.** For `sin`, `cos`, `exp`, `log` and other transcendentals, CICC accepts the host result because IEEE-754 guarantees these are correctly rounded within 1 ULP on both host and device. The exception wrapper catches cases where host and device behavior might diverge (denormals, overflow boundary).

3. **exp2(x) folded as pow(2.0, x).** Rather than calling `exp2()` directly (which might differ between host and device implementations), the evaluator computes `pow(2.0, x)` through the binary wrapper, ensuring consistent behavior.

4. **No half-precision transcendental folding.** The type check at the evaluator's entry rejects type byte 1 (half) for all trig/exp/log functions. Only basic operations (convert, compare) work on fp16. This is safe because half-precision math functions are implemented as promote-to-float, compute, demote-to-half -- by the time the constant folder runs, the promotion has already been inlined.

### FTZ and Approximate Intrinsics

NVVM intrinsics like `nvvm.exp2.approx.ftz.f` and `nvvm.sin.approx.ftz.f` carry `.approx` (reduced precision) and `.ftz` (flush-to-zero for denormals) modifiers. These are present in the foldable ID list, which may seem surprising -- folding an "approximate" intrinsic with exact host math could produce a different value than the hardware.

The rationale: constant folding evaluates the **mathematical function**, not the hardware instruction. If the input is a normal float and the result is a normal float, the folded value is correct regardless of FTZ or approximation quality. The FTZ modifier only affects denormal inputs (which the exception wrapper would catch via `FE_UNDERFLOW`), and the `.approx` modifier only matters for runtime execution speed. For compile-time constants, exact evaluation is strictly better.

## Comparison with Upstream LLVM

Upstream LLVM's `ConstantFolding.cpp` (as of LLVM 19.x) handles NVPTX intrinsics in `canConstantFoldCallTo` and `ConstantFoldCall`. The overlap and gaps:

| Capability | Upstream LLVM | CICC v13.0 |
|------------|---------------|------------|
| `llvm.sin`, `llvm.cos`, `llvm.exp`, `llvm.log`, etc. | Yes | Yes |
| `nvvm.ceil.f`, `nvvm.floor.f`, `nvvm.fabs`, `nvvm.sqrt.*` | Yes | Yes |
| `nvvm.fmax.*`, `nvvm.fmin.*` (all variants) | Yes (including `.xorsign_abs`) | Yes (subset: `.f`, `.d`, `.ftz`, `.ftz.nan`) |
| `nvvm.f2i_*`, `nvvm.d2i_*` (FP-to-int with rounding modes) | Yes (all 32 variants) | Partial (IDs 5293, 5300 only) |
| Plain C math names (`sin`, `cosf`, `exp2f`, etc.) | Via TargetLibraryInfo | Direct name matching (44 entries) |
| Glibc `__*_finite` variants | No | Yes (20 entries) |
| C++ mangled `_Z3cosf`, `_Z4acosd`, etc. | No | Yes (~48 entries) |
| `nvvm.cos.approx.ftz.f`, `nvvm.exp2.approx.ftz.f`, etc. | No | Yes |
| `nvvm.rcp.approx.ftz.d`, `nvvm.rsqrt.approx.ftz.f` | No | Yes |
| `nvvm.mul.hi.*` | No | Yes |
| Convergent intrinsic rejection | Implicit (no fold path) | Explicit attribute check |
| FMA constant fold | Yes (via APFloat) | Yes (opcodes 99/100, APFloat fma) |
| Integer min/max/ctlz/cttz | Partial | Yes (full NVVM ID coverage) |

The critical CICC-only capabilities are the `__*_finite` variants (needed when code is compiled with `-ffinite-math-only`), the C++ mangled names (emitted by device-side C++ math overloads), and the `.approx.ftz` intrinsic family.

## Integer Constant Folding

The evaluator also handles integer-domain operations when operands have type tag 13 (`ConstantInt`) or when FP operands encode integer comparisons:

**Binary integer ops** (operand count = 2, both `ConstantInt`):
- Opcodes 189, 195, 198, 209, 210, 211: APInt binary operations (add, sub, mul, sdiv, udiv, srem) via `sub_16A7290` and related APInt helpers.
- Opcodes `0xEC2`/`0xEC3` (3778/3779): `ctlz` (count leading zeros).
- Opcodes `0x1014`/`0x1015`, `0x1016`/`0x1017`: Signed/unsigned min/max via APInt comparison.
- Opcodes `0x104B`/`0x104C`, `0x1087`/`0x1088`: Additional signed/unsigned min/max encodings.
- Opcode 3811: Division where divisor is known zero -- returns `UndefValue`.

**Integer comparison fold** (type tag 14 with integer-domain opcodes):
- Opcode `0xBB` (187), `0x8C` (140): `icmp eq/ne` -- predicate 0.
- Opcode `0x61` (97): `icmp slt` -- predicate 2.
- Opcode `0xBC` (188): `icmp sgt` -- predicate 4.
- Opcode `0xCE` (206): `icmp uge` -- predicate 3.
- Opcode `0x08` (8): `icmp ult` -- predicate 1.

These produce `ConstantInt` 0 or 1 via `sub_169EBA0`/`sub_169D440`.

## Libdevice Integration

NVIDIA's libdevice (`libdevice.10.bc`) provides optimized LLVM bitcode implementations of math functions. After linking libdevice, calls like `__nv_sinf` are typically inlined and disappear before constant folding runs. However, if inlining fails or is disabled, residual `__nv_*` calls may survive.

The constant folder does **not** recognize `__nv_*` prefixed names directly. The `__` name-matching path only handles glibc `__*_finite` patterns, not NVIDIA's `__nv_*` convention. Un-inlined libdevice residuals are handled upstream by the NVVM InstCombine intrinsic canonicalizer (`sub_1169C30`), which recognizes `__nv_*` prefixes and may convert them to standard LLVM intrinsics that the constant folder can then process.

The `__nvvm_reflect` mechanism (used for `__CUDA_ARCH` queries) is resolved by a separate earlier pass (`NVVMReflect`) that replaces `__nvvm_reflect("__CUDA_ARCH")` with a constant integer based on the target SM. By the time the constant folder runs, all `__nvvm_reflect` calls have been eliminated.

## Configuration Knobs

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `disable-fp-call-folding` | `cl::opt<bool>` | `false` | Upstream LLVM hidden flag. When true, prevents constant folding of any function returning or accepting floating-point types. Checked in `canConstantFoldCallTo`. |
| `FPFoldDisable` | NVIDIA CiccOption | `false` | NVIDIA-specific flag that disables FP constant folding at the NVVM level. |
| `instcombine-negator-enabled` | `cl::opt<bool>` | `true` | Controls the negation propagation system in `sub_1169C30` (InstCombine intrinsic folder). |
| `instcombine-negator-max-depth` | `cl::opt<int>` | platform-dependent | Depth limit for the negator chain in InstCombine intrinsic folding. Prevents exponential blowup when pushing negation through deep arithmetic chains. |

The `FPFoldDisable` knob is significant for debugging precision issues: when a kernel produces different results with `-O0` vs `-O2`, disabling FP folding isolates whether constant-folded values are the source of the discrepancy.

## ConstantFP Result Creation

The result builder `sub_14D17B0` creates the final LLVM `ConstantFP` IR node from the evaluated `double` result. It dispatches on the return type byte at `*(type + 8)`:

| Type byte | Precision | Behavior |
|-----------|-----------|----------|
| 1 | `half` | Not reached from math folder (filtered at entry). Infrastructure exists: converts through `APFloat` semantics. |
| 2 | `float` | Truncates `double` to `float` via C cast, then converts `float` to `APFloat` via `sub_169D3B0`. |
| 3 | `double` | Stores full double precision via `sub_169D3F0` (double to APFloat). |

Both paths finish with `sub_159CCF0(*type, &storage)` which constructs the `ConstantFP` node from the `APFloat` storage. The float path's truncation via C cast means the folded float value matches what `(float)host_result` produces -- this is IEEE-754 correct because the cast performs round-to-nearest-even.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `nvvmIntrinsicConstantFold` | `0x14D90D0` | 27 KB | Eligibility predicate: can this intrinsic be constant-folded? |
| `nvvmConstantFoldLibCall` | `0x14D1BC0` | 54 KB | Math evaluator: compute constant result from constant args |
| `extractDoubleFromConstantFP` | `0x14D1620` | -- | Extract `double` from `ConstantFP` IR node |
| `safeMathEvalUnary` | `0x14D19F0` | -- | Exception-safe unary evaluation wrapper |
| `safeMathEvalBinary` | `0x14D1A80` | -- | Exception-safe binary evaluation wrapper |
| `createConstantFPResult` | `0x14D17B0` | -- | Build `ConstantFP` from evaluated double |
| `customFabs` | `0x14D1280` | -- | SSE2 sign-bit clear |
| `customFloor` | `0x14D13B0` | -- | Truncation + sign correction |
| `customCeil` | `0x14D1410` | -- | Truncation + sign correction |
| `customSqrt` | `0x14D1470` | -- | Thin wrapper around libc `sqrt` |
| `fptoui_fptosi_fold` | `0x14D1500` | -- | FP-to-integer conversion fold |
| `apintMoveTransfer` | `0x14D15E0` | -- | APInt move/transfer helper |
| `vectorMathLibMapping` | `0x149E420` | 26 KB | Scalar-to-vectorized math mapping table |
| `platformFuncCanonicalize` | `0x149FA60` | 15 KB | Platform-specific name canonicalization |
| `constantExprFoldSCEV` | `0x14D44C0` | 20 KB | ConstantExpr fold / SCEV integration |
| `constantFoldAggregate` | `0x14D5510` | 16 KB | ConstantFold for aggregate types |
| `constantFoldGEPExtract` | `0x14D66F0` | 17 KB | ConstantFold for GEP and extract |
| `constantExprSCEVBuild` | `0x14DBA90` | 22 KB | ConstantExpr + SCEV builder |
| `AttributeList::hasAttribute` | `0x1560260` | -- | Attribute query (used 8 times in eligibility checker) |
| `Value::getName` | `0x1649960` | -- | Name string extraction (case 0 path) |
| NVVM InstCombine intrinsic fold | `0x1169C30` | 87 KB | Algebraic simplification of NVVM intrinsics (see [InstCombine](instcombine.md)) |

## Cross-References

- **[InstCombine](instcombine.md)** -- The NVVM intrinsic canonicalizer (`sub_1169C30`) handles algebraic simplification, negation propagation, and operand folding for NVVM intrinsics. It calls constant folding as a sub-step.
- **[Pipeline & Ordering](pipeline.md)** -- Where constant folding sits in the optimization pipeline (runs within InstCombine and as a standalone analysis).
- **[Builtin Table: Math Functions](../builtins/math.md)** -- The complete list of CUDA math builtins and their mapping to NVVM intrinsics.
- **[CLI Flags](../config/cli-flags.md)** -- `FPFoldDisable` and other optimization control flags.
- **[LLVM Knobs](../config/knobs.md)** -- The full `disable-fp-call-folding` flag and related InstCombine depth limits.
