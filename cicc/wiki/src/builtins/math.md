# Math Function Builtins

Math builtins cover floating-point rounding, transcendental approximations, reciprocal/square-root operations, type conversions, and precise arithmetic with explicit rounding modes. They span IDs 21--184 and 276--403, totaling over 230 entries. Unlike most other builtin categories, many math builtins fall through the dispatch switch entirely and resolve via the generic LLVM intrinsic path.

## Bit Manipulation (IDs 21--26)

These integer utility operations map directly to hardware instructions available on all SM targets.

| ID | Builtin | Operation |
|---|---|---|
| 21--22 | `__nvvm_clz_{i,ll}` | Count leading zeros (32/64-bit) |
| 23--24 | `__nvvm_popc_{i,ll}` | Population count (32/64-bit) |
| 25--26 | `__nvvm_brev_{i,ll}` | Bit reverse (32/64-bit) |

## Rounding and Absolute Value (IDs 27--46)

Float rounding and absolute value operations exist in three type variants: flush-to-zero single (`ftz_f`), IEEE single (`f`), and double (`d`).

| ID Range | Operation | Variants |
|---|---|---|
| 27--29 | `__nvvm_floor_{ftz_f,f,d}` | Floor |
| 30--32 | `__nvvm_ceil_{ftz_f,f,d}` | Ceiling |
| 33--35 | `__nvvm_abs_{ftz_f,f,d}` | Absolute value (integer-style) |
| 36--38 | `__nvvm_fabs_{ftz_f,f,d}` | Absolute value (float) |
| 39--41 | `__nvvm_round_{ftz_f,f,d}` | Round to nearest |
| 42--44 | `__nvvm_trunc_{ftz_f,f,d}` | Truncate toward zero |
| 45--46 | `__nvvm_saturate_{ftz_f,f}` | Clamp to [0.0, 1.0] |

## Transcendental Approximations (IDs 47--56)

Hardware-accelerated approximations for transcendental functions. These use the GPU's special function units (SFU) and are not IEEE-compliant.

| ID Range | Operation | Variants |
|---|---|---|
| 47--49 | `__nvvm_ex2_approx_{ftz_f,f,d}` | Base-2 exponential |
| 50--52 | `__nvvm_lg2_approx_{ftz_f,f,d}` | Base-2 logarithm |
| 53--55 | `__nvvm_sin_approx_{ftz_f,f,d}` | Sine |
| 56 | `__nvvm_cos_approx_ftz_f` | Cosine (FTZ only registered) |

## Reciprocal (IDs 57--69)

Full-precision reciprocal with all four IEEE rounding modes and three type variants.

| ID Range | Operation | Rounding Modes |
|---|---|---|
| 57--69 | `__nvvm_rcp_{rn,rz,rm,rp}_{ftz_f,f,d}` | RN (nearest), RZ (zero), RM (minus), RP (plus) |

The 13 entries cover 4 rounding modes x 3 types, with the FTZ single-precision variant adding one additional entry.

## Square Root and Reciprocal Square Root (IDs 70--87)

| ID Range | Operation | Description |
|---|---|---|
| 70--84 | `__nvvm_sqrt_{f,rn,rz,rm,rp}_{ftz_f,f,d}` | Square root (5 modes x 3 types) |
| 85--87 | `__nvvm_rsqrt_approx_{ftz_f,f,d}` | Reciprocal square root (SFU approximation) |

The `sqrt_f` variant (without rounding qualifier) uses the default hardware rounding. The `rsqrt_approx` variants use the SFU fast path.

## Type Conversions (IDs 88--184)

The largest math subcategory with 97 entries, covering every combination of source type, destination type, rounding mode, and FTZ flag.

### Double-to-Float (IDs 88--95)

`__nvvm_d2f_{rn,rz,rm,rp}_{ftz,}` — 4 rounding modes x 2 FTZ variants.

### Integer/Float Cross-Conversions (IDs 96--177)

82 entries covering all permutations of:
- Source types: `d` (double), `f` (float), `i` (int32), `ui` (uint32), `ll` (int64), `ull` (uint64)
- Destination types: same set
- Rounding modes: `rn`, `rz`, `rm`, `rp`

Pattern: `__nvvm_{src}2{dst}_{rounding}` (e.g., `__nvvm_d2i_rn`, `__nvvm_f2ull_rz`).

### Half Precision (IDs 178--180)

| ID | Builtin | Description |
|---|---|---|
| 178 | `__nvvm_f2h_rn_ftz` | Float to half (FTZ, round nearest) |
| 179 | `__nvvm_f2h_rn` | Float to half (round nearest) |
| 180 | `__nvvm_h2f` | Half to float |

### Bitcast (IDs 181--184)

Reinterpret-cast between integer and float types without value conversion. Lowered via `sub_12A7DA0` which emits opcode `0x31` (49, bitcast).

| ID | Builtin | Direction |
|---|---|---|
| 181 | `__nvvm_bitcast_f2i` | float -> int32 |
| 182 | `__nvvm_bitcast_i2f` | int32 -> float |
| 183 | `__nvvm_bitcast_ll2d` | int64 -> double |
| 184 | `__nvvm_bitcast_d2ll` | double -> int64 |

## Integer Min/Max and Multiply-High (IDs 276--293)

| ID Range | Operation | Types |
|---|---|---|
| 276--279 | `__nvvm_{min,max}_{i,ui}` | 32-bit signed/unsigned |
| 280--283 | `__nvvm_{min,max}_{ll,ull}` | 64-bit signed/unsigned |
| 284--289 | `__nvvm_f{min,max}_{f,ftz_f,d}` | Float min/max (with FTZ) |
| 290--293 | `__nvvm_mulhi_{i,ui,ll,ull}` | Upper half of multiplication |

## Precise Float Arithmetic (IDs 294--349)

These builtins provide IEEE-compliant arithmetic with explicit rounding mode control. Each operation exists in all four rounding modes and up to five type variants (ftz_f, f, ftz_f2, f2, d).

| ID Range | Operation | Entries |
|---|---|---|
| 294--313 | `__nvvm_mul_{rn,rz,rm,rp}_{ftz_f,f,ftz_f2,f2,d}` | 20 |
| 314--333 | `__nvvm_add_{rn,rz,rm,rp}_{ftz_f,f,ftz_f2,f2,d}` | 20 |
| 334--349 | `__nvvm_div_{rn,rz,rm,rp}_{ftz_f,f,d}` | 16 |

### FMA (IDs 383--402)

Fused multiply-add with all rounding/type combinations:

| ID Range | Operation | Entries |
|---|---|---|
| 383--402 | `__nvvm_fma_{rn,rz,rm,rp}_{ftz_f,f,d,ftz_f2,f2}` | 20 |

## Miscellaneous (IDs 350, 380--382, 403)

| ID | Builtin | Description |
|---|---|---|
| 350 | `__nvvm_lohi_i2d` | Compose double from two 32-bit halves |
| 380 | `__nvvm_prmt` | Byte permute (PRMT instruction) |
| 381--382 | `__nvvm_sad_{i,ui}` | Sum of absolute differences |
| 403 | `__nvvm_fns` | Find Nth set bit |

## Table-Based Lowering for Precise Arithmetic

The precise arithmetic builtins (mul, add, div, fma with rounding modes) are lowered through `sub_12B3540` (address `0x12B3540`, 10KB), which uses two lazily-initialized red-black trees (`std::map<int, triple>`) to map builtin IDs to IR opcode triples.

**Tree 1** serves three-operand builtins (FMA): maps ID ranges to opcode `0xF59` with variant codes encoding the rounding mode and type.

**Tree 2** serves two-operand builtins (mul, add, div): maps to opcodes `0xE3A`, `0xE3B`, `0x105E`, `0x1061` depending on the operation.

The lookup procedure:
1. Extract up to 4 operand arguments from the call expression
2. Find the builtin ID in the appropriate tree to obtain `(opcode, variant)`
3. Look up the IR function via `sub_126A190`
4. Emit the call instruction via `sub_1285290`
5. Generate the inline asm fragment via `sub_12A8F50`

## LLVM Intrinsic Fallback Path

Many standard math builtins (`floor`, `ceil`, `sin`, `cos`, `sqrt`, `fma`, `exp`, `log`) are **not** handled by the switch cases at all. When the builtin table lookup returns ID 0 (name not found), the dispatcher falls through to the generic LLVM intrinsic path at `LABEL_4` in `sub_955A70`. This path:

1. Checks if the name starts with `"llvm."` (prefix constant `0x6D766C6C`)
2. Looks up the intrinsic via `sub_B6ACB0` (LLVM intrinsic name-to-ID)
3. Lowers all arguments with type-cast insertion where needed
4. Emits a standard LLVM call via `sub_921880`

This means functions like `llvm.floor.f32`, `llvm.cos.f64`, and `llvm.fma.f32` bypass the builtin ID system entirely and map directly to LLVM's intrinsic infrastructure.

## Float Compatibility Wrappers (IDs 643--646)

Four C runtime float functions are registered as builtins for compatibility:

| ID | Builtin | Maps To |
|---|---|---|
| 643 | `__ceilf` | `__nvvm_ceil_f` equivalent |
| 644 | `__floorf` | `__nvvm_floor_f` equivalent |
| 645 | `__roundf` | `__nvvm_round_f` equivalent |
| 646 | `__truncf` | `__nvvm_trunc_f` equivalent |
